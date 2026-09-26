"""LLM client tests (constraint 4): mock and real share one shell."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from intentradar.budget import BudgetGuard, RunGates
from intentradar.errors import ConfigError, GateExceeded, ProviderError
from intentradar.llm import (
    MODEL_PRICING,
    PRICING_FALLBACK_PER_1M,
    HttpBackend,
    LLMConfig,
    LLMResponse,
    MockBackend,
    NemotronClient,
)

REAL_PAYLOAD = {
    "id": "chatcmpl-1",
    "choices": [{"message": {"role": "assistant", "content": "hello from nemotron"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 7},
}


def _http_client(
    config: LLMConfig,
    budget=None,
    gates=None,
    payload=None,
    handler=None,
    max_retries=None,
) -> NemotronClient:
    """A client whose transport is mocked, exercising the real HttpBackend path.

    Nothing in this module ever touches the network: every "real" call is served
    by an httpx MockTransport.
    """
    body = payload or REAL_PAYLOAD
    if handler is None:
        handler = lambda request: httpx.Response(200, json=body)  # noqa: E731
    kwargs = {"max_retries": max_retries} if max_retries is not None else {}
    backend = HttpBackend(
        api_key="test-key",
        base_url=config.base_url,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep_fn=lambda _: None,
        **kwargs,
    )
    return NemotronClient(config=config, backend=backend, budget=budget, gates=gates)


def _counting_handler(status: int, text: str = "boom") -> tuple[Any, dict[str, int]]:
    """A handler that always fails with ``status`` and counts the attempts."""
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(status, text=text)

    return handler, counter


def _fields(response: LLMResponse) -> set[str]:
    """The field set every backend must return."""
    return set(response.to_dict().keys())


def test_mock_and_real_return_the_same_shape(tmp_data_dir: Path) -> None:
    """Both backends produce an identical LLMResponse structure."""
    mock_cfg = LLMConfig(mock=True, cache_dir=tmp_data_dir / "cache")
    real_cfg = LLMConfig(
        api_key="test-key",
        model="nvidia/nemotron-3-super-120b-a12b",
        cache_dir=tmp_data_dir / "cache",
    )
    mock_resp = NemotronClient(config=mock_cfg).complete("sys", "hi")
    real_resp = _http_client(real_cfg).complete("sys", "hi")

    assert _fields(mock_resp) == _fields(real_resp)
    assert _fields(mock_resp) == {
        "content",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "cached",
        "mock",
    }
    assert mock_resp.mock is True
    assert real_resp.mock is False
    assert real_resp.content == "hello from nemotron"
    assert real_resp.prompt_tokens == 11
    assert real_resp.completion_tokens == 7


def test_no_key_never_crashes_and_uses_mock(tmp_data_dir: Path) -> None:
    """Keyless configuration silently falls back to the deterministic mock."""
    config = LLMConfig(api_key="", cache_dir=tmp_data_dir / "cache")
    client = NemotronClient(config=config)
    assert client.is_mock is True
    response = client.hello()
    assert response.mock is True
    assert response.content


def test_mock_is_deterministic(tmp_data_dir: Path) -> None:
    """Same input -> same output, every time (needed for reproducible tests)."""
    config = LLMConfig(mock=True, cache_dir=tmp_data_dir / "cache")
    first = NemotronClient(config=config).complete("sys", "same input")
    second = NemotronClient(config=config).complete("sys", "same input")
    assert first.content == second.content
    assert first.prompt_tokens == second.prompt_tokens


def test_cache_hit_is_free(tmp_data_dir: Path) -> None:
    """A cache hit costs nothing and is flagged as cached."""
    config = LLMConfig(mock=True, cache_dir=tmp_data_dir / "cache")
    client = NemotronClient(config=config)
    first = client.complete("sys", "cache me")
    second = client.complete("sys", "cache me")
    assert first.cached is False
    assert second.cached is True
    assert second.cost_usd == 0.0
    assert second.content == first.content
    assert (tmp_data_dir / "cache" / "llm_cache.jsonl").exists()


def test_cost_uses_pricing_table(tmp_data_dir: Path) -> None:
    """Known models use the published rate; unknown ones fall back conservatively."""
    known = LLMConfig(
        api_key="k", model="nvidia/nemotron-3-super-120b-a12b", cache_dir=tmp_data_dir / "cache"
    )
    response = _http_client(known).complete("sys", "hi")
    expected = 11 / 1e6 * 0.30 + 7 / 1e6 * 0.90
    assert response.cost_usd == pytest.approx(expected)

    unknown = LLMConfig(api_key="k", model="nvidia/some-unpriced-model", cache_dir=tmp_data_dir / "cache")
    response = _http_client(unknown).complete("sys", "hi")
    expected_fallback = 11 / 1e6 * PRICING_FALLBACK_PER_1M["in"] + 7 / 1e6 * PRICING_FALLBACK_PER_1M["out"]
    assert response.cost_usd == pytest.approx(expected_fallback)
    assert PRICING_FALLBACK_PER_1M["in"] > 0 and PRICING_FALLBACK_PER_1M["out"] > 0
    assert MODEL_PRICING  # the table is populated, not a placeholder


def test_gate_hard_stops_the_fourth_call(tmp_data_dir: Path) -> None:
    """MAX_LLM_CALLS=3 → the 4th *billed* completion raises before any spend.

    Cache hits are deliberately not counted: they cost nothing, so counting them
    would make the gate fire on free work.
    """
    config = LLMConfig(mock=True, cache_dir=tmp_data_dir / "cache")
    gates = RunGates(max_llm_calls=3)
    client = NemotronClient(config=config, gates=gates)
    for index in range(3):
        client.complete("sys", f"distinct prompt {index}")
    assert gates.llm_calls == 3
    with pytest.raises(GateExceeded):
        client.complete("sys", "one call too many")


def test_budget_is_charged_and_recorded(tmp_data_dir: Path) -> None:
    """Every real call is accounted for on the persisted monthly budget."""
    config = LLMConfig(
        api_key="k", model="nvidia/nemotron-3-super-120b-a12b", cache_dir=tmp_data_dir / "cache"
    )
    budget = BudgetGuard(path=tmp_data_dir / "usage.json", monthly_budget_usd=20.0)
    client = _http_client(config, budget=budget)
    client.complete("sys", "hi")
    assert budget.usage.llm_calls == 1
    assert budget.usage.cost_usd > 0


def test_budget_blocked_prevents_new_calls(tmp_data_dir: Path) -> None:
    """At 100% of the monthly budget the client refuses to call out."""
    budget = BudgetGuard(path=tmp_data_dir / "usage.json", monthly_budget_usd=1.0)
    budget.record_llm(type("R", (), {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 1.0})())
    config = LLMConfig(mock=True, cache_dir=tmp_data_dir / "cache")
    client = NemotronClient(config=config, budget=budget)
    with pytest.raises(Exception) as excinfo:
        client.complete("sys", "hi")
    assert excinfo.value.__class__.__name__ == "BudgetExceeded"


def test_5xx_is_retried_before_giving_up(tmp_data_dir: Path) -> None:
    """5xx is transient: max_retries=2 must produce 3 attempts, then ProviderError."""
    handler, counter = _counting_handler(500)
    config = LLMConfig(api_key="k", model="nvidia/nemotron-3-super-120b-a12b",
                       cache_dir=tmp_data_dir / "cache")
    client = _http_client(config, handler=handler, max_retries=2)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("sys", "hi")
    assert counter["n"] == 3, "5xx must be retried max_retries+1 times"
    assert "HTTP 500" in excinfo.value.message


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_is_not_retried(tmp_data_dir: Path, status: int) -> None:
    """4xx is a client error: one attempt, immediate ProviderError."""
    handler, counter = _counting_handler(status)
    config = LLMConfig(api_key="k", model="nvidia/nemotron-3-super-120b-a12b",
                       cache_dir=tmp_data_dir / "cache")
    client = _http_client(config, handler=handler, max_retries=3)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("sys", "hi")
    assert counter["n"] == 1, f"HTTP {status} must not be retried"
    assert f"HTTP {status}" in excinfo.value.message


def test_5xx_that_recovers_returns_the_response(tmp_data_dir: Path) -> None:
    """A 500 followed by a 200 must succeed rather than raise."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json=REAL_PAYLOAD)

    config = LLMConfig(api_key="k", model="nvidia/nemotron-3-super-120b-a12b",
                       cache_dir=tmp_data_dir / "cache")
    response = _http_client(config, handler=handler, max_retries=2).complete("sys", "hi")
    assert calls["n"] == 2
    assert response.content == "hello from nemotron"


def test_allow_unpriced_zero_refuses_an_unpriced_model(tmp_data_dir: Path) -> None:
    """INTENTRADAR_ALLOW_UNPRICED=0: unknown model is refused *before* the call."""
    handler, counter = _counting_handler(200)
    config = LLMConfig(api_key="k", model="nvidia/some-unpriced-model",
                       cache_dir=tmp_data_dir / "cache", allow_unpriced=False)
    client = _http_client(config, handler=handler, max_retries=1)
    with pytest.raises(ConfigError) as excinfo:
        client.complete("sys", "hi")
    assert counter["n"] == 0, "must refuse before spending anything"
    assert "INTENTRADAR_ALLOW_UNPRICED" in excinfo.value.message
    assert excinfo.value.exit_code == 2


def test_allow_unpriced_zero_allows_a_priced_model(tmp_data_dir: Path) -> None:
    """A model present in MODEL_PRICING still runs with the switch off."""
    config = LLMConfig(api_key="k", model="nvidia/nemotron-3-super-120b-a12b",
                       cache_dir=tmp_data_dir / "cache", allow_unpriced=False)
    response = _http_client(config).complete("sys", "hi")
    assert response.content == "hello from nemotron"


def test_allow_unpriced_one_uses_the_fallback_estimate(tmp_data_dir: Path) -> None:
    """With the switch on (default), an unpriced model bills the fallback."""
    config = LLMConfig(api_key="k", model="nvidia/some-unpriced-model",
                       cache_dir=tmp_data_dir / "cache", allow_unpriced=True)
    response = _http_client(config).complete("sys", "hi")
    expected = 11 / 1e6 * PRICING_FALLBACK_PER_1M["in"] + 7 / 1e6 * PRICING_FALLBACK_PER_1M["out"]
    assert response.cost_usd == pytest.approx(expected)


def test_mock_mode_is_exempt_from_the_unpriced_check(tmp_data_dir: Path) -> None:
    """Offline mock traffic must never be blocked by ALLOW_UNPRICED=0."""
    config = LLMConfig(mock=True, cache_dir=tmp_data_dir / "cache", allow_unpriced=False)
    response = NemotronClient(config=config).complete("sys", "hi")
    assert response.mock is True
    assert response.content


def test_http_error_surfaces_as_provider_error(tmp_data_dir: Path) -> None:
    """A 401 is wrapped with the status code and body, not swallowed."""
    backend = HttpBackend(
        api_key="bad",
        base_url="https://api.tokenfactory.nebius.com/v1",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="nope"))),
        sleep_fn=lambda _: None,
        max_retries=0,
    )
    client = NemotronClient(config=LLMConfig(api_key="bad"), backend=backend)
    with pytest.raises(ProviderError) as excinfo:
        client.complete("sys", "hi")
    assert "HTTP 401" in excinfo.value.message


def test_tier_selects_the_reasoning_model(tmp_data_dir: Path) -> None:
    """`tier='reasoning'` routes to the reasoning slug."""
    config = LLMConfig(
        api_key="k",
        model="nvidia/nemotron-3-super-120b-a12b",
        model_reasoning="nvidia/Nemotron-3-Ultra-550b-a55b",
        cache_dir=tmp_data_dir / "cache",
    )
    assert config.model_for("everyday") == "nvidia/nemotron-3-super-120b-a12b"
    assert config.model_for("reasoning") == "nvidia/Nemotron-3-Ultra-550b-a55b"


def test_list_models_requires_a_real_backend(tmp_data_dir: Path) -> None:
    """Probing GET /models in mock mode is an explicit error, not a silent no-op."""
    client = NemotronClient(config=LLMConfig(mock=True))
    with pytest.raises(ProviderError):
        client.list_models()


def test_mock_backend_directly() -> None:
    """The mock backend respects the backend contract signature."""
    content, pin, pout = MockBackend()._chat([{"role": "user", "content": "x" * 40}], "m")
    assert content and pin >= 1 and pout >= 1
