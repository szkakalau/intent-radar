"""Anthropic Messages backend (P0-B) — protocol shape, offline via MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from intentradar.errors import ProviderError
from intentradar.llm import (
    BACKEND_ANTHROPIC,
    BACKEND_OPENAI,
    AnthropicBackend,
    LLMConfig,
    NemotronClient,
    resolve_backend,
)


def _client(handler: object, **kwargs: object) -> AnthropicBackend:
    """An AnthropicBackend whose transport is an httpx MockTransport."""
    return AnthropicBackend(
        api_key="test-key",
        base_url="http://127.0.0.1:8787/v1",
        sleep_fn=lambda _s: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


# ── backend selection ──────────────────────────────────────────────────────


def test_explicit_backend_setting_wins() -> None:
    """An explicit INTENTRADAR_LLM_BACKEND is never second-guessed."""
    assert resolve_backend("https://api.tokenfactory.nebius.com/v1", "anthropic") == BACKEND_ANTHROPIC
    assert resolve_backend("http://127.0.0.1:8787/v1/messages", "openai") == BACKEND_OPENAI


def test_auto_sniffs_the_messages_path() -> None:
    """`/messages` is the path the Anthropic protocol is named after."""
    assert resolve_backend("http://127.0.0.1:8787/v1/messages") == BACKEND_ANTHROPIC
    assert resolve_backend("http://127.0.0.1:8787/v1") == BACKEND_OPENAI
    assert resolve_backend("https://api.tokenfactory.nebius.com/v1") == BACKEND_OPENAI


def test_unknown_backend_setting_is_a_config_error() -> None:
    """A typo fails loudly instead of silently picking a protocol."""
    from intentradar.errors import ConfigError

    with pytest.raises(ConfigError):
        resolve_backend("http://x/v1", "gemini")


def test_url_accepts_base_with_or_without_messages() -> None:
    """Both spellings of the base URL resolve to the same endpoint."""
    assert AnthropicBackend("k", "http://h:8787/v1").url == "http://h:8787/v1/messages"
    assert AnthropicBackend("k", "http://h:8787/v1/messages").url == "http://h:8787/v1/messages"
    assert AnthropicBackend("k", "http://h:8787/v1/").url == "http://h:8787/v1/messages"


# ── request shape ──────────────────────────────────────────────────────────


def test_system_prompt_travels_in_its_own_field() -> None:
    """The whole point: `system` is top level, not a role in `messages`."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    backend = _client(handler)
    backend._chat([{"role": "system", "content": "SYS"}, {"role": "user", "content": "USR"}], "m")

    assert captured["system"] == "SYS"
    assert captured["messages"] == [{"role": "user", "content": "USR"}]
    assert "role" not in captured  # no stray top-level role


def test_anthropic_headers_are_sent() -> None:
    """x-api-key + anthropic-version, and no Bearer prefix."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    _client(handler)._chat([{"role": "user", "content": "hi"}], "m")

    assert seen["x-api-key"] == "test-key"
    assert seen["anthropic-version"] == "2023-06-01"
    assert "bearer" not in seen.get("authorization", "").lower()


def test_request_goes_to_the_messages_path() -> None:
    """Not /chat/completions."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "{}"}]})

    _client(handler)._chat([{"role": "user", "content": "hi"}], "m")
    assert seen[0].endswith("/v1/messages")


# ── response parsing ───────────────────────────────────────────────────────


def test_text_is_taken_from_the_text_block_not_content_zero() -> None:
    """Reasoning models emit a `thinking` block first — content[0] is not the answer."""
    payload = {
        "content": [
            {"type": "thinking", "thinking": "let me reason about this…"},
            {"type": "text", "text": '{"is_actionable": true}'},
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    content, p_tokens, c_tokens = _client(handler)._chat([{"role": "user", "content": "hi"}], "m")
    assert content == '{"is_actionable": true}'
    assert "let me reason" not in content
    assert (p_tokens, c_tokens) == (10, 5)


def test_thinking_only_response_is_an_error_not_a_guess() -> None:
    """Budget spent entirely on thinking: raise, never return the reasoning."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "thinking", "thinking": "x"}]})

    with pytest.raises(ProviderError) as excinfo:
        _client(handler)._chat([{"role": "user", "content": "hi"}], "m")
    assert "no text block" in excinfo.value.message


def test_usage_falls_back_to_a_char_estimate() -> None:
    """No usage block -> estimate, so the budget gate still has a number."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": [{"type": "text", "text": "hello"}]})

    _, p_tokens, c_tokens = _client(handler)._chat([{"role": "user", "content": "hi"}], "m")
    assert p_tokens >= 1 and c_tokens >= 1


def test_4xx_is_final_and_5xx_is_retried() -> None:
    """Same retry policy as the OpenAI backend: 4xx once, 5xx backed off."""
    calls = {"n": 0}

    def bad_request(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    with pytest.raises(ProviderError):
        _client(bad_request, max_retries=2)._chat([{"role": "user", "content": "hi"}], "m")
    assert calls["n"] == 1

    calls["n"] = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    with pytest.raises(ProviderError):
        _client(flaky, max_retries=2)._chat([{"role": "user", "content": "hi"}], "m")
    assert calls["n"] == 3  # initial + 2 retries


def test_transport_errors_are_retried() -> None:
    """Connection failures are transient."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down")

    with pytest.raises(ProviderError):
        _client(handler, max_retries=1)._chat([{"role": "user", "content": "hi"}], "m")
    assert calls["n"] == 2


# ── through the client shell ───────────────────────────────────────────────


def test_client_selects_the_anthropic_backend_from_settings() -> None:
    """Configured as an Anthropic endpoint -> the Anthropic backend is built."""
    config = LLMConfig(
        api_key="k",
        base_url="http://127.0.0.1:8787/v1/messages",
        model="deepseek-v4-flash",
        backend="auto",
    )
    client = NemotronClient(config=config)
    assert isinstance(client._backend, AnthropicBackend)
    assert client.backend_protocol == BACKEND_ANTHROPIC


def test_client_reports_which_protocol_it_speaks() -> None:
    """Numbers must be attributable to the model that produced them."""
    anthropic = NemotronClient(
        config=LLMConfig(api_key="k", base_url="http://h/v1/messages", model="m")
    )
    assert anthropic.backend_protocol == BACKEND_ANTHROPIC
    openai = NemotronClient(config=LLMConfig(api_key="k", base_url="http://h/v1", model="m"))
    assert openai.backend_protocol == BACKEND_OPENAI
    mock = NemotronClient(config=LLMConfig(mock=True))
    assert mock.backend_protocol == "mock"


def test_mock_still_wins_over_a_configured_endpoint() -> None:
    """Forced mock never reaches the network, whatever the base URL says."""
    client = NemotronClient(
        config=LLMConfig(mock=True, base_url="http://127.0.0.1:8787/v1/messages", model="m")
    )
    assert client.is_mock is True
    assert client.backend_protocol == "mock"
