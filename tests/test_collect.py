"""Collection layer tests: parsing, credits accounting, and the no-proxy rule."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from intentradar.collect import SourceResult
from intentradar.collect.scrapecreators import SC_BASE, ScrapeCreatorsProvider
from intentradar.errors import MissingCredential, ProviderError

FAKE_RESPONSE = {
    "posts": [
        {
            "id": "1wp6ji1",
            "name": "t3_1wp6ji1",
            "title": "Flashcard decks?",
            "selftext": "Do you make different decks? <b>cost</b> is a problem",
            "created_utc": 1790344560,
            "score": 2,
            "num_comments": 14,
        },
        {
            "id": "1wqjy9p",
            "name": "t3_1wqjy9p",
            "title": "I hate forgetting anatomy",
            "selftext": "",
            "created_utc": 1790400000,
            "score": 9,
            "num_comments": 7,
        },
    ],
    "credits_charged": 2,
    "credits_remaining": 56,
    "cached": False,
}


def _provider(handler, **kwargs) -> ScrapeCreatorsProvider:
    """Build a provider whose HTTP layer is a mock transport."""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ScrapeCreatorsProvider(api_key="test-key", client=client, page_sleep_s=0, **kwargs)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    """Always return the canned payload."""
    return httpx.Response(200, json=FAKE_RESPONSE)


def test_fetch_parses_posts_and_credits() -> None:
    """Posts are normalised and credits are surfaced, not swallowed."""
    provider = _provider(_ok_handler)
    result: SourceResult = provider.fetch_subreddit("Anki", pages=1)

    assert len(result.posts) == 2
    assert result.credits_charged == 2
    assert result.credits_remaining == 56

    post = result.posts[0]
    assert post.id == "1wp6ji1"
    assert post.sub == "Anki"
    assert post.upvotes == 2
    assert post.num_comments == 14
    # The provider does not return permalink; it must be rebuilt.
    assert post.permalink == "https://reddit.com/r/Anki/comments/1wp6ji1/"
    # HTML is stripped by clean().
    assert "<b>" not in post.text


def test_pagination_uses_fullname_cursor() -> None:
    """Second page must paginate with the Reddit fullname, like monitor.py did."""
    seen_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_params.append(dict(request.url.params))
        if len(seen_params) == 1:
            return httpx.Response(200, json=FAKE_RESPONSE)
        return httpx.Response(200, json={"posts": [], "credits_charged": 2, "credits_remaining": 54})

    provider = _provider(handler)
    result = provider.fetch_subreddit("Anki", pages=2)

    assert len(seen_params) == 2
    assert seen_params[0]["sort"] == "new"
    assert seen_params[0]["trim"] == "true"
    assert seen_params[1]["after"] == "t3_1wqjy9p"
    assert result.credits_charged == 4
    assert result.credits_remaining == 54


def test_http_error_raises_provider_error() -> None:
    """HTTP failures surface with status and body, never silently empty."""
    provider = _provider(lambda request: httpx.Response(401, text='{"error":"bad key"}'))
    with pytest.raises(ProviderError) as excinfo:
        provider.fetch_subreddit("Anki", pages=1)
    assert "HTTP 401" in excinfo.value.message
    assert excinfo.value.exit_code == 4


def test_transport_error_retries_then_raises() -> None:
    """Transport errors are retried, then reported as ProviderError."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("boom")

    provider = _provider(handler, max_retries=1)
    with pytest.raises(ProviderError):
        provider.fetch_subreddit("Anki", pages=1)
    assert calls["n"] == 2  # initial attempt + 1 retry


def test_missing_key_is_a_clear_credential_error() -> None:
    """Empty key fails immediately with the env var name."""
    with pytest.raises(MissingCredential) as excinfo:
        ScrapeCreatorsProvider(api_key="")
    assert "SCRAPECREATORS_API_KEY" in excinfo.value.message


def test_no_hardcoded_proxy_anywhere_in_source(src_dir: Path) -> None:
    """The old hardcoded proxy must never come back (constraint 2)."""
    offenders: list[str] = []
    for path in src_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "10808" in text:
            offenders.append(str(path))
        if "127.0.0.1" in text:
            offenders.append(f"{path}:127.0.0.1")
        if "PROXY = " in text or 'ProxyHandler' in text:
            offenders.append(f"{path}:ProxyHandler")
    assert not offenders, f"hardcoded proxy/secret found in: {offenders}"


def test_no_api_key_literal_in_repo_files(src_dir: Path) -> None:
    """Defensive scan: nothing that looks like a committed credential."""
    import re

    pattern = re.compile(r"(sk-[A-Za-z0-9]{16,}|api[_-]?key\s*=\s*['\"][A-Za-z0-9]{16,})", re.I)
    offenders: list[str] = []
    root = src_dir.parent
    for path in list(root.rglob("*.py")) + list(root.rglob("*.json")) + list(root.rglob("*.md")):
        if ".venv" in path.parts or "data" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if pattern.search(text):
            offenders.append(str(path))
    assert not offenders, f"possible key literal in: {offenders}"


def test_base_url_is_configurable() -> None:
    """base_url is a constructor argument, never a hardwired constant in the call path."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(200, json=FAKE_RESPONSE)

    provider = _provider(handler)
    provider.base_url = "https://example.test/v1"
    provider.fetch_subreddit("Anki", pages=1)
    assert captured and captured[0].startswith("https://example.test/v1")
    assert SC_BASE.startswith("https://")
    assert json.dumps(FAKE_RESPONSE)  # sanity: payload is serialisable
