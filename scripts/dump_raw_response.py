"""Dump ONE raw chat-completion response, sanitised, for shape inspection.

Why this exists
---------------
Reasoning models (Nemotron 3) may return the chain of thought in a field we do
not know the name of, and may leave ``content`` empty. That is the one failure
mode that cannot be rehearsed offline: we can guess the shape, but only a real
response tells us what the endpoint actually sends.

So on the first run against a real key, run this FIRST and read the output
before trusting any number:

    INTENTRADAR_LLM_BASE_URL=https://api.tokenfactory.nebius.com/v1 \\
    INTENTRADAR_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b \\
    INTENTRADAR_LLM_BACKEND=openai \\
    INTENTRADAR_LLM_API_KEY=<key> \\
    uv run python scripts/dump_raw_response.py

It prints the response with the message bodies elided and then reports, for the
first choice, which fields are populated and how long they are. **Nothing
secret is printed**: no headers, no key, and message bodies are truncated.

Read the verdict as:
  * ``content`` populated and looks like JSON  — fine, run `eval score`.
  * ``content`` empty, some ``*reasoning*`` populated — the answer is being
    spent on thinking; raise INTENTRADAR_LLM_MAX_TOKENS and re-run.
  * anything else — the extraction in llm/client.py needs a new shape.
"""

from __future__ import annotations

import json
from typing import Any

MAX_SNIPPET = 400


def _sanitise(node: Any, depth: int = 0) -> Any:
    """Recursively shorten long strings so a dump stays readable."""
    if isinstance(node, str):
        return node if len(node) <= MAX_SNIPPET else node[:MAX_SNIPPET] + f"…[{len(node)} chars]"
    if isinstance(node, list):
        return [_sanitise(item, depth + 1) for item in node[:3]]
    if isinstance(node, dict):
        return {key: _sanitise(value, depth + 1) for key, value in node.items()}
    return node


def main() -> int:
    """Make one call, print the sanitised response, and report the shape."""
    from intentradar.config import Settings
    from intentradar.llm import build_client

    settings = Settings.from_env()
    if not settings.llm_api_key:
        print("no API key configured — set INTENTRADAR_LLM_API_KEY (or NEBIUS_API_KEY)")
        return 2

    client = build_client(settings)
    model = settings.llm_model or "(unset)"
    print(f"endpoint : {settings.llm_base_url}")
    print(f"model    : {model}")
    print(f"backend  : {settings.llm_backend}")
    print("-" * 70)

    try:
        response = client.complete(
            system="Reply with ONE JSON object and nothing else.",
            user='Reply exactly: {"is_actionable": false, "confidence": 0.5, "reason": "shape probe"}',
            tier="everyday",
        )
    except Exception as exc:  # noqa: BLE001 - the point is to show what came back
        print(f"call failed: {type(exc).__name__}: {exc}")
        return 1

    print("--- parsed content (what the judge would use as the verdict) ---")
    print(getattr(response, "content", ""))
    print("-" * 70)
    print(f"model returned by the endpoint: {getattr(response, 'model', '')!r}")

    raw = getattr(client, "last_raw_response", None)
    if raw is None:
        print(
            "\nNOTE: no raw payload retained (the call was served from cache, or the\n"
            "backend does not keep one). Clear data/cache/llm_cache.jsonl and re-run\n"
            "if you need the per-field report."
        )
        return 0

    print("\n--- raw response (sanitised) ---")
    print(json.dumps(_sanitise(raw), ensure_ascii=False, indent=2)[:4000])
    try:
        message = raw["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        print("\nunexpected shape — no choices[0].message")
        return 0
    print("\n--- populated fields on choices[0].message ---")
    for key, value in message.items():
        if isinstance(value, str) and value.strip():
            print(f"  {key}: {len(value)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
