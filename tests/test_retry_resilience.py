"""Retry behaviour of both HTTP backends.

Why this file exists
--------------------
``llm/client.py::HttpBackend._post`` and ``collect/scrapecreators.py::
ScrapeCreatorsProvider._request`` raise :class:`ProviderError` **inside** their
retry loop and immediately re-raise it, so *any* HTTP status ``>= 400`` aborts
on the first attempt: ``max_retries`` is silently inert for status codes.
Transport-level exceptions *are* retried, which is why the gap survived — they
are the only failures the existing test exercises.

The contract pinned down here:

===========================  =====================  ==================
failure                      attempts               outcome
===========================  =====================  ==================
4xx (bad key / bad params)   ``1``                  ProviderError, exit 4
5xx (provider blip)          ``max_retries + 1``    ProviderError, exit 4
transport error              ``max_retries + 1``    ProviderError, exit 4
===========================  =====================  ==================

The intent is even written down in ``collect/scrapecreators.py``: the comment
"4xx is a client error (bad key / bad params): do not retry" implies 5xx *should*
be retried — the 4xx/5xx distinction itself was never implemented.
"""

from __future__ import annotations

import httpx
import pytest

from intentradar.collect.scrapecreators import ScrapeCreatorsProvider
from intentradar.errors import ProviderError
from intentradar.llm import LLMConfig, NemotronClient
from intentradar.llm.client import HttpBackend

MAX_RETRIES = 2
EXPECTED_ATTEMPTS = MAX_RETRIES + 1


# ── helpers ────────────────────────────────────────────────────────────────


def _status_handler(status: int, attempts: list[int]):
    """Count every request the backend makes and always answer ``status``."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        attempts.append(1)
        return httpx.Response(status, json={"error": "provider says no"})

    return handler


def _boom_handler(attempts: list[int]):
    """Fail at the transport layer instead of returning a status code."""

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        attempts.append(1)
        raise httpx.ConnectError("connection refused")

    return handler


def _nemotron_client(handler) -> NemotronClient:
    """Build a client whose real HTTP backend answers via ``handler``."""
    backend = HttpBackend(
        api_key="fake-key",
        base_url="https://api.example.test/v1",
        max_retries=MAX_RETRIES,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep_fn=lambda _seconds: None,  # keep the suite fast
    )
    return NemotronClient(
        config=LLMConfig(api_key="fake-key", model="nvidia/nemotron-3-super-120b-a12b",
                         cache_dir=None, mock=False),
        backend=backend,
    )


def _scrapecreators(handler, **kwargs) -> ScrapeCreatorsProvider:
    """Build a collection provider whose HTTP layer is a mock transport."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ScrapeCreatorsProvider(
        api_key="test-key", client=client, page_sleep_s=0,
        max_retries=MAX_RETRIES, **kwargs
    )


# ── Nemotron / HttpBackend ─────────────────────────────────────────────────


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_5xx_is_retried_before_failing(status: int) -> None:
    """A server-side blip must be retried, not fatal on the first response."""
    attempts: list[int] = []
    client = _nemotron_client(_status_handler(status, attempts))

    with pytest.raises(ProviderError) as excinfo:
        client.complete("system", "user", use_cache=False)

    assert excinfo.value.exit_code == 4
    assert len(attempts) == EXPECTED_ATTEMPTS, (
        f"HTTP {status} was attempted {len(attempts)}x, expected {EXPECTED_ATTEMPTS} "
        f"(initial attempt + {MAX_RETRIES} retries) — see llm/client.py::_post"
    )


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_4xx_is_not_retried(status: int) -> None:
    """A bad key or bad params will not fix itself — fail fast, once."""
    attempts: list[int] = []
    client = _nemotron_client(_status_handler(status, attempts))

    with pytest.raises(ProviderError):
        client.complete("system", "user", use_cache=False)

    assert len(attempts) == 1, f"HTTP {status} should not be retried"


def test_transport_error_is_still_retried() -> None:
    """Control case: the class of failure the retry loop does handle today."""
    attempts: list[int] = []
    client = _nemotron_client(_boom_handler(attempts))

    with pytest.raises(ProviderError):
        client.complete("system", "user", use_cache=False)

    assert len(attempts) == EXPECTED_ATTEMPTS


# ── ScrapeCreators ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_provider_transient_5xx_is_retried_before_failing(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same contract for collection: one 500 must not lose a whole daily run."""
    # _request() sleeps with time.sleep; keep the assertion instant.
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    attempts: list[int] = []
    provider = _scrapecreators(_status_handler(status, attempts))

    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_subreddit("Anki", pages=1)

    assert excinfo.value.exit_code == 4
    assert len(attempts) == EXPECTED_ATTEMPTS, (
        f"HTTP {status} was attempted {len(attempts)}x, expected {EXPECTED_ATTEMPTS} "
        f"— see collect/scrapecreators.py::_request"
    )


@pytest.mark.parametrize("status", [400, 401, 404])
def test_provider_4xx_is_not_retried(status: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected request must not burn extra credits retrying."""
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    attempts: list[int] = []
    provider = _scrapecreators(_status_handler(status, attempts))

    with pytest.raises(ProviderError):
        provider.fetch_subreddit("Anki", pages=1)

    assert len(attempts) == 1
