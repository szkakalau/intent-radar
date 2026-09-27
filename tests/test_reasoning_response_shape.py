"""Reasoning-model response shapes on the OpenAI-compatible backend.

Nemotron 3 is a reasoning model: it may emit the chain of thought in a field
whose name we do not control, and may leave ``content`` empty. The failure mode
matters because quoting a model's private reasoning as its verdict produces a
run that *looks* healthy — plausible prose, no exception — while every judgment
in it is nonsense.

So the client must either find a real answer in ``content`` or fail loudly, and
it must never fall back to a reasoning field.
"""

from __future__ import annotations

import json

import pytest

from intentradar.errors import ProviderError
from intentradar.llm.client import HttpBackend, _extract_openai_content


def _backend(handler: object) -> HttpBackend:
    """An HttpBackend whose transport is an httpx MockTransport."""
    import httpx

    return HttpBackend(
        api_key="test",
        base_url="http://stub/v1",
        client=httpx.Client(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
    )


def _payload(message: dict[str, object]) -> str:
    """A chat-completion body wrapping ``message``."""
    return json.dumps(
        {
            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    )


def test_plain_content_is_used() -> None:
    """The normal case: the answer is in ``content``."""
    text = _extract_openai_content(json.loads(_payload({"content": "hello"})))
    assert text == "hello"


def test_reasoning_alongside_content_is_ignored() -> None:
    """A reasoning field is populated but ``content`` wins — and is returned."""
    data = json.loads(_payload({"content": "the answer", "reasoning_content": "thinking…"}))
    assert _extract_openai_content(data) == "the answer"


def test_reasoning_only_response_is_refused() -> None:
    """The trap: answer-shaped text in a reasoning field, ``content`` empty."""
    data = json.loads(_payload({"reasoning_content": "thinking…", "content": ""}))
    with pytest.raises(ProviderError) as excinfo:
        _extract_openai_content(data)
    message = excinfo.value.message
    assert "reasoning-only" in message
    # The field name is reported, so the shape is identifiable rather than guessed.
    assert "reasoning_content" in message
    # And the reasoning text is explicitly not used.
    assert "NOT used" in message


@pytest.mark.parametrize("field", ["reasoning", "reasoning_text", "thinking"])
def test_every_known_reasoning_field_name_is_recognised(field: str) -> None:
    """Providers name this field differently; all known names are detected."""
    data = json.loads(_payload({field: "thinking…", "content": None}))
    with pytest.raises(ProviderError) as excinfo:
        _extract_openai_content(data)
    assert "reasoning-only" in excinfo.value.message


def test_empty_content_with_no_reasoning_is_also_an_error() -> None:
    """An empty reply is an error, not an empty verdict."""
    data = json.loads(_payload({"content": ""}))
    with pytest.raises(ProviderError) as excinfo:
        _extract_openai_content(data)
    assert "empty content" in excinfo.value.message


def test_missing_choices_is_reported_with_the_payload() -> None:
    """An unexpected shape is shown, not swallowed."""
    with pytest.raises(ProviderError) as excinfo:
        _extract_openai_content({"unexpected": True})
    assert "unexpected response shape" in excinfo.value.message
    assert "unexpected" in excinfo.value.message


def test_backend_keeps_the_raw_payload_for_shape_inspection() -> None:
    """`scripts/dump_raw_response.py` needs it; nothing else should rely on it."""
    body = _payload({"content": "hi"})
    backend = _backend(lambda request: __import__("httpx").Response(200, text=body))
    content, prompt_tokens, completion_tokens = backend._chat(
        [{"role": "user", "content": "x"}], "m"
    )
    assert content == "hi"
    assert prompt_tokens == 10 and completion_tokens == 5
    assert backend.last_raw is not None
    assert backend.last_raw["choices"][0]["message"]["content"] == "hi"


def test_a_reasoning_only_call_fails_through_the_backend() -> None:
    """End to end through the client path, not just the helper."""
    body = _payload({"reasoning_content": "thinking…", "content": ""})
    backend = _backend(lambda request: __import__("httpx").Response(200, text=body))
    with pytest.raises(ProviderError) as excinfo:
        backend._chat([{"role": "user", "content": "x"}], "m")
    assert "reasoning-only" in excinfo.value.message
