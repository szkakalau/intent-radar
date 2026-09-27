"""A local OpenAI-compatible stub, so the Nemotron switch can be rehearsed.

Why this exists
---------------
The Nemotron key can arrive at any moment, and the failure modes we care about
only appear on a real run: does the identity line print the model we pointed
at, and does a reasoning-first model break the answer extraction?

This script stands up ``POST /v1/chat/completions`` on localhost so the whole
``eval score`` path can be exercised *now*, with the real env var names and the
real model slug. It answers with a canned verdict so the numbers it produces are
meaningless — what it verifies is plumbing and identity, not accuracy.

Run:
    uv run python scripts/nemotron_stub.py            # serves :8791
    INTENTRADAR_LLM_BASE_URL=http://127.0.0.1:8791/v1 \\
    INTENTRADAR_LLM_MODEL=nvidia/nemotron-3-super-120b-a12b \\
    INTENTRADAR_LLM_BACKEND=openai \\
    INTENTRADAR_LLM_API_KEY=stub \\
    uv run python -m intentradar eval score --testset einprag-2026-09-27 \\
        --layers rule_v4,rule_v4+llm --min-score 3

``--reasoning-only`` reproduces the dangerous shape: the answer text moved into
``reasoning_content`` and ``content`` is empty. The client must refuse it rather
than quote the reasoning back as a verdict.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8791
VERDICT = {"is_actionable": False, "confidence": 0.5, "reason": "stub response"}
# A plausible-looking thinking trace. It must never be mistaken for the answer.
REASONING = "The author names a category. Let me check criterion (2) next..."


def _reply(model: str, reasoning_only: bool) -> dict[str, object]:
    """The chat-completion payload this stub returns."""
    message: dict[str, object] = {"role": "assistant"}
    if reasoning_only:
        # The trap: answer-shaped text in a reasoning field, content empty.
        message["reasoning_content"] = REASONING + " " + json.dumps(VERDICT)
        message["content"] = ""
    else:
        message["content"] = json.dumps(VERDICT)
        message["reasoning_content"] = REASONING
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
    }


class _Handler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible surface: /v1/chat/completions and /v1/models."""

    reasoning_only = False

    def log_message(self, fmt: str, *args: object) -> None:
        """Quiet by default; the command's own output is what matters."""
        return

    def _send(self, code: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's API
        """/v1/models — reachability check for `config check`."""
        if self.path.rstrip("/").endswith("/models"):
            self._send(200, {"data": [{"id": "nvidia/nemotron-3-super-120b-a12b"}]})
            return
        self._send(404, {"error": f"no route for {self.path}"})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's API
        """/v1/chat/completions — echo the requested model back in the payload."""
        if not re.search(r"/chat/completions$", self.path):
            self._send(404, {"error": f"no route for {self.path}"})
            return
        length = int(self.headers.get("content-length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "invalid JSON"})
            return
        model = str(request.get("model") or "")
        self._send(200, _reply(model, self.reasoning_only))


def main() -> int:
    """Serve the stub until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--reasoning-only",
        action="store_true",
        help="return the answer in reasoning_content with empty content (the trap)",
    )
    args = parser.parse_args()

    _Handler.reasoning_only = args.reasoning_only
    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    mode = "reasoning-only" if args.reasoning_only else "normal"
    print(f"stub listening on http://127.0.0.1:{args.port}/v1  mode={mode}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
