"""Nemotron client — one shell, two backends (constraint 4).

The OpenAI-compatible endpoint is called with a bare POST to
``/v1/chat/completions``; no vendor SDK is involved, so there is exactly one
retry/caching/accounting code path for both mock and real traffic.

Price table: ``MODEL_PRICING`` holds the published $/1M-token rates. Unknown
models fall back to ``PRICING_FALLBACK_PER_1M`` — a *conservative* estimate,
never 0.0, because a zero price would make the monthly gate silently useless.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from intentradar.errors import ConfigError, ProviderError

log = logging.getLogger(__name__)

# Published Nebius Token Factory rates, $ per 1M tokens.
MODEL_PRICING: dict[str, dict[str, float]] = {
    "nvidia/nemotron-3-ultra-550b-a55b": {"in": 1.00, "out": 3.00},
    "nvidia/nemotron-3-super-120b-a12b": {"in": 0.30, "out": 0.90},
}
# Conservative stand-in used until the real rate for a model is known.
PRICING_FALLBACK_PER_1M: dict[str, float] = {"in": 0.30, "out": 1.20}

MOCK_MODEL = "mock/nemotron-everyday"
MOCK_REASONING_MODEL = "mock/nemotron-reasoning"

# Default endpoint. Any OpenAI-compatible server works — override it with
# INTENTRADAR_LLM_BASE_URL / NEBIUS_BASE_URL.
DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"

# Anthropic Messages API version header.
ANTHROPIC_VERSION = "2023-06-01"

# Which wire protocol to speak. "auto" sniffs the configured base URL.
BACKEND_OPENAI = "openai"
BACKEND_ANTHROPIC = "anthropic"


def resolve_backend(base_url: str, setting: str = "auto") -> str:
    """Decide which wire protocol a base URL needs.

    Args:
        base_url: the configured endpoint.
        setting: ``INTENTRADAR_LLM_BACKEND`` — ``openai``, ``anthropic`` or
            ``auto``. An explicit value always wins.

    Returns:
        ``"openai"`` or ``"anthropic"``.

    ``auto`` sniffs one thing only: an endpoint whose path ends in ``/messages``
    is an Anthropic Messages endpoint, because that is the path the protocol is
    named after. Everything else is treated as OpenAI-compatible, which is what
    Nebius Token Factory speaks.
    """
    choice = (setting or "auto").strip().lower()
    if choice in (BACKEND_OPENAI, BACKEND_ANTHROPIC):
        return choice
    if choice not in ("", "auto"):
        raise ConfigError(
            f"INTENTRADAR_LLM_BACKEND: expected openai|anthropic|auto, got {setting!r}"
        )
    return BACKEND_ANTHROPIC if base_url.rstrip("/").endswith("/messages") else BACKEND_OPENAI


@dataclass
class LLMResponse:
    """What every backend returns. Same shape for mock and real traffic."""

    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    cached: bool = False
    mock: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialise for logs / tests."""
        return asdict(self)


@dataclass
class LLMConfig:
    """Connection + behaviour settings for one client instance."""

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = ""
    model_reasoning: str = ""
    timeout_s: float = 60.0
    max_retries: int = 2
    temperature: float = 0.0
    max_tokens: int = 1024
    cache_dir: Path | None = None
    mock: bool = False
    allow_unpriced: bool = True
    backend: str = "auto"  # openai | anthropic | auto

    def model_for(self, tier: str) -> str:
        """Resolve the slug for ``tier`` (``everyday`` | ``reasoning``)."""
        if self.mock:
            return MOCK_REASONING_MODEL if tier == "reasoning" else MOCK_MODEL
        if tier == "reasoning":
            return self.model_reasoning or self.model
        return self.model

    @classmethod
    def from_settings(cls, settings: Any) -> LLMConfig:
        """Build from a :class:`~intentradar.config.Settings`.

        ``getattr`` on purpose: it keeps ``llm/`` free of an import cycle and
        lets a duck-typed settings object be used in tests.
        """
        return cls(
            api_key=str(getattr(settings, "llm_api_key", "") or ""),
            base_url=str(getattr(settings, "llm_base_url", "") or DEFAULT_BASE_URL),
            model=str(getattr(settings, "llm_model", "") or ""),
            model_reasoning=str(getattr(settings, "model_reasoning", "") or ""),
            cache_dir=getattr(settings, "cache_dir", None),
            mock=bool(getattr(settings, "mock_enabled", True)),
            allow_unpriced=bool(getattr(settings, "allow_unpriced", True)),
            backend=str(getattr(settings, "llm_backend", "auto") or "auto"),
            timeout_s=float(getattr(settings, "llm_timeout_s", 60.0) or 60.0),
            max_tokens=int(getattr(settings, "llm_max_tokens", 1024) or 1024),
        )


def _estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Cost in USD. Unknown models use the conservative fallback (with a warning)."""
    price = MODEL_PRICING.get(model.strip().lower())
    if price is None:
        log.warning(
            "no pricing configured for %s, using fallback $%.2f/$%.2f per 1M tokens",
            model,
            PRICING_FALLBACK_PER_1M["in"],
            PRICING_FALLBACK_PER_1M["out"],
        )
        price = PRICING_FALLBACK_PER_1M
    return prompt_tokens / 1e6 * price["in"] + completion_tokens / 1e6 * price["out"]


class _Backend(Protocol):
    """The only thing a backend has to implement."""

    def _chat(self, messages: list[dict[str, str]], model: str) -> tuple[str, int, int]:
        """Return ``(content, prompt_tokens, completion_tokens)``."""
        ...


class MockBackend:
    """Deterministic offline backend. Same input -> same output, always."""

    def __init__(self, max_tokens: int = 1024) -> None:
        """Initialise with a token budget used for the fake usage numbers."""
        self.max_tokens = max_tokens

    def _chat(self, messages: list[dict[str, str]], model: str) -> tuple[str, int, int]:
        """Produce a stable, structure-complete stand-in response."""
        blob = "\n".join(m.get("content", "") for m in messages)
        digest = hashlib.sha256(f"{model}\n{blob}".encode()).hexdigest()
        content = (
            '{"intent": false, "confidence": 0.5, "reason": "mock response", '
            f'"echo_sha256": "{digest[:16]}"}}'
        )
        # Rough but stable token estimate: ~4 characters per token.
        prompt_tokens = max(1, len(blob) // 4)
        completion_tokens = max(1, len(content) // 4)
        return content, prompt_tokens, completion_tokens


# Reasoning models (Nemotron 3, DeepSeek-R1 class) may emit the chain of
# thought in a separate field and leave ``content`` empty. The names differ per
# provider, so they are all probed — but ONLY to explain a failure. The
# reasoning text is never returned as the answer: quoting a model's private
# reasoning as its verdict would be worse than no verdict, and it would look
# like a working run.
_REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text", "thinking")


def _extract_openai_content(data: dict[str, Any]) -> str:
    """Return the answer from an OpenAI-shaped response, or fail loudly.

    Three shapes are handled:

    * ``content`` is a non-empty string — the normal case, used as-is.
    * ``content`` is empty/absent but a reasoning field is populated — the
      answer was spent on thinking. Raising names the field, so the operator
      sees *which* shape the endpoint returned instead of guessing. Raising the
      token budget is the usual fix.
    * anything else — an unexpected shape, reported with the payload.

    Never returns a reasoning field's contents.
    """
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("nemotron", f"unexpected response shape: {json.dumps(data)[:200]}") from exc
    if not isinstance(message, dict):
        raise ProviderError("nemotron", f"unexpected message type: {json.dumps(data)[:200]}")

    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    # content is empty: is this a reasoning model that spent the budget?
    populated = [
        name
        for name in _REASONING_FIELDS
        if isinstance(message.get(name), str) and message[name].strip()
    ]
    if populated:
        raise ProviderError(
            "nemotron",
            "reasoning-only response: content is empty but the reasoning field(s) "
            f"{populated} are populated — the answer was spent on thinking. Raise "
            "INTENTRADAR_LLM_MAX_TOKENS and retry. The reasoning text is NOT used "
            "as the verdict.",
        )
    raise ProviderError(
        "nemotron",
        f"empty content in response: {json.dumps(data)[:200]}",
    )


class HttpBackend:
    """Real backend: POST ``/v1/chat/completions`` (OpenAI compatible)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        client: httpx.Client | None = None,
        sleep_fn: Any = None,
    ) -> None:
        """Initialise. ``client`` is injectable for tests (mock transport)."""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = client
        self._sleep = sleep_fn or time.sleep
        # Last raw payload, kept only so `scripts/dump_raw_response.py` can show
        # the real shape of an endpoint's reply. Never part of a judgment.
        self.last_raw: dict[str, Any] | None = None

    # ── helpers ────────────────────────────────────────────────────────────
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One POST with retry on transient errors."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                if self._client is not None:
                    response = self._client.post(url, json=payload, headers=headers)
                else:
                    with httpx.Client(timeout=self.timeout_s) as client:
                        response = client.post(url, json=payload, headers=headers)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    break
                self._sleep(1.5 * (attempt + 1))
                continue

            code = response.status_code
            if 400 <= code < 500:
                # 4xx is a client error (bad key / bad payload): retrying cannot help.
                raise ProviderError("nemotron", f"HTTP {code}: {response.text[:200]}")
            if code >= 400:
                # 5xx is transient — fall through to the backoff below.
                last_error = f"HTTP {code}: {response.text[:200]}"
                if attempt >= self.max_retries:
                    break
                self._sleep(1.5 * (attempt + 1))
                continue
            try:
                return response.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
                if attempt >= self.max_retries:
                    break
                self._sleep(1.5 * (attempt + 1))

        raise ProviderError(
            "nemotron", f"failed after {self.max_retries + 1} attempt(s): {last_error}"
        )

    def list_models(self) -> list[str]:
        """``GET /models`` — used to verify base_url and key reachability."""
        url = f"{self.base_url}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if self._client is not None:
            response = self._client.get(url, headers=headers)
        else:
            with httpx.Client(timeout=self.timeout_s) as client:
                response = client.get(url, headers=headers)
        if response.status_code >= 400:
            raise ProviderError("nemotron", f"HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        return [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict)]

    # ── backend contract ───────────────────────────────────────────────────
    def _chat(self, messages: list[dict[str, str]], model: str) -> tuple[str, int, int]:
        """Call the real endpoint and extract content + usage."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        data = self._post(payload)
        self.last_raw = data
        content = _extract_openai_content(data)
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if not prompt_tokens and not completion_tokens:
            # Endpoint omitted usage: fall back to a ~4 chars/token estimate so
            # the budget gate still has something to count.
            prompt_tokens = max(1, sum(len(m.get("content", "")) for m in messages) // 4)
            completion_tokens = max(1, len(content) // 4)
        return content, prompt_tokens, completion_tokens


class AnthropicBackend:
    """Anthropic Messages backend (``POST /v1/messages``).

    Development-time verification channel: it lets the semantic layer be
    measured before a Nebius key exists. Three protocol differences from the
    OpenAI-compatible backend:

    1. the path is ``/messages``, not ``/chat/completions``;
    2. ``system`` is a **top-level field**, not a ``role: "system"`` message;
    3. the reply is ``{"content": [{"type": "text", "text": ...}]}``, not
       ``choices[0].message.content``.

    A fourth, easy-to-miss detail: reasoning models emit a ``{"type":"thinking"}``
    block *before* the text block, so the answer is **not** ``content[0]``. The
    first block whose ``type == "text"`` is the answer. Reading ``content[0]``
    silently yields the model's private reasoning — or nothing at all when the
    token budget is spent on thinking.

    Retry semantics match :class:`HttpBackend`: 4xx is final (1 attempt),
    5xx/transport errors back off up to ``max_retries`` times.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        client: httpx.Client | None = None,
        sleep_fn: Any = None,
    ) -> None:
        """Initialise. ``client`` is injectable for tests (mock transport)."""
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max(0, int(max_retries))
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = client
        self._sleep = sleep_fn or time.sleep
        # Last raw payload, kept only so `scripts/dump_raw_response.py` can show
        # the real shape of an endpoint's reply. Never part of a judgment.
        self.last_raw: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        """The messages endpoint, whether or not ``/messages`` was configured."""
        base = self.base_url.rstrip("/")
        if base.endswith("/messages"):
            return base
        return f"{base}/messages"

    # ── helpers ────────────────────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        """Anthropic auth headers (no ``Bearer`` prefix, unlike OpenAI)."""
        return {
            "content-type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """Return the first ``text`` block — **not** ``content[0]``."""
        blocks = data.get("content") or []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text") or "")
        raise ProviderError("anthropic", f"no text block in response: {json.dumps(data)[:200]}")

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """One POST with retry on transient errors (same policy as HttpBackend)."""
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                if self._client is not None:
                    response = self._client.post(self.url, json=payload, headers=self._headers())
                else:
                    with httpx.Client(timeout=self.timeout_s) as client:
                        response = client.post(self.url, json=payload, headers=self._headers())
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt >= self.max_retries:
                    break
                self._sleep(1.5 * (attempt + 1))
                continue

            code = response.status_code
            if 400 <= code < 500:
                # 4xx is a client error (bad key / bad payload): retrying cannot help.
                raise ProviderError("anthropic", f"HTTP {code}: {response.text[:200]}")
            if code >= 400:
                last_error = f"HTTP {code}: {response.text[:200]}"
                if attempt >= self.max_retries:
                    break
                self._sleep(1.5 * (attempt + 1))
                continue
            try:
                return response.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
                if attempt >= self.max_retries:
                    break
                self._sleep(1.5 * (attempt + 1))

        raise ProviderError(
            "anthropic", f"failed after {self.max_retries + 1} attempt(s): {last_error}"
        )

    def list_models(self) -> list[str]:
        """``GET /models`` against an Anthropic-compatible endpoint."""
        url = self.base_url.rsplit("/messages", 1)[0] + "/models"
        if self._client is not None:
            response = self._client.get(url, headers=self._headers())
        else:
            with httpx.Client(timeout=self.timeout_s) as client:
                response = client.get(url, headers=self._headers())
        if response.status_code >= 400:
            raise ProviderError("anthropic", f"HTTP {response.status_code}: {response.text[:200]}")
        data = response.json()
        return [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict)]

    # ── backend contract ───────────────────────────────────────────────────
    def _chat(self, messages: list[dict[str, str]], model: str) -> tuple[str, int, int]:
        """Call the Messages endpoint and extract content + usage."""
        system = ""
        turns: list[dict[str, str]] = []
        for message in messages:
            if message.get("role") == "system":
                # Anthropic takes the system prompt out of band.
                system = message.get("content", "")
            else:
                turns.append(message)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": turns or [{"role": "user", "content": ""}],
        }
        if system:
            payload["system"] = system
        if self.temperature:
            payload["temperature"] = self.temperature

        data = self._post(payload)
        self.last_raw = data
        content = self._extract_text(data)
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        if not prompt_tokens and not completion_tokens:
            prompt_tokens = max(1, sum(len(m.get("content", "")) for m in messages) // 4)
            completion_tokens = max(1, len(content) // 4)
        return content, prompt_tokens, completion_tokens


class NemotronClient:
    """The shell: cache, gate, accounting, retries — identical for mock and real."""

    def __init__(
        self,
        config: LLMConfig,
        budget: Any = None,
        gates: Any = None,
        backend: _Backend | None = None,
    ) -> None:
        """Initialise.

        Args:
            config: connection + model settings.
            budget: optional :class:`~intentradar.budget.BudgetGuard`.
            gates: optional :class:`~intentradar.budget.RunGates`.
            backend: optional explicit backend (tests inject one).
        """
        self.config = config
        self.budget = budget
        self.gates = gates
        self._backend: _Backend = backend or self._default_backend()
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_loaded = False
        self._cache_path: Path | None = (
            Path(config.cache_dir) / "llm_cache.jsonl" if config.cache_dir else None
        )

    @property
    def last_raw_response(self) -> dict[str, Any] | None:
        """The last raw payload the backend saw, for shape inspection only.

        Exposed so ``scripts/dump_raw_response.py`` can show what an endpoint
        actually returns before we trust any number from it — the one failure
        mode (a reasoning-first model) cannot be rehearsed offline.
        """
        return getattr(self._backend, "last_raw", None)

    def _default_backend(self) -> _Backend:
        """Mock when configured (or keyless), otherwise the configured wire protocol."""
        if self.config.mock or not self.config.api_key:
            return MockBackend(max_tokens=self.config.max_tokens)
        if resolve_backend(self.config.base_url, self.config.backend) == BACKEND_ANTHROPIC:
            return self._anthropic_backend()
        return self._http_backend()

    def _anthropic_backend(self) -> AnthropicBackend:
        """Build the Anthropic Messages backend."""
        if not self.config.api_key:
            raise ConfigError(
                "no LLM API key for live calls — set INTENTRADAR_LLM_API_KEY "
                "(or NEBIUS_API_KEY) in .env, or leave both unset to run in mock mode"
            )
        return AnthropicBackend(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout_s=self.config.timeout_s,
            max_retries=self.config.max_retries,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

    def _http_backend(self) -> HttpBackend:
        """Build the real backend (Nebius)."""
        if not self.config.api_key:
            raise ConfigError(
                "no LLM API key for live calls — set INTENTRADAR_LLM_API_KEY "
                "(or NEBIUS_API_KEY) in .env, or leave both unset to run in mock mode"
            )
        return HttpBackend(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout_s=self.config.timeout_s,
            max_retries=self.config.max_retries,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

    # ── cache ──────────────────────────────────────────────────────────────
    @staticmethod
    def _cache_key(model: str, temperature: float, messages: list[dict[str, str]]) -> str:
        """sha256 over model + temperature + messages."""
        blob = json.dumps(
            {"model": model, "temperature": temperature, "messages": messages},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _load_cache(self) -> None:
        """Read the JSONL cache once, lazily."""
        if self._cache_loaded or self._cache_path is None:
            return
        self._cache_loaded = True
        if not self._cache_path.exists():
            return
        try:
            with self._cache_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    key = record.get("key")
                    if key:
                        self._cache[str(key)] = record.get("response", {})
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("LLM cache unreadable (%s), starting empty", exc)

    def _write_cache(self, key: str, response: LLMResponse) -> None:
        """Append one entry to the JSONL cache."""
        if self._cache_path is None:
            return
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = response.to_dict()
        with self._cache_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"key": key, "response": payload}, ensure_ascii=False) + "\n")
        # Keep the in-memory view in sync so a repeat call in this process is a hit.
        self._cache[key] = payload

    # ── public API ─────────────────────────────────────────────────────────
    @property
    def is_mock(self) -> bool:
        """Whether this client is running against the deterministic mock."""
        return isinstance(self._backend, MockBackend)

    def _require_known_price(self, model: str) -> None:
        """Honour ``INTENTRADAR_ALLOW_UNPRICED``.

        With the switch off, an unpriced model is refused *before* any request is
        sent — otherwise the operator would silently accept a billable call whose
        cost the budget gate cannot compute. Mock traffic is always exempt: it is
        offline and costs nothing.
        """
        if self.config.allow_unpriced or self.is_mock:
            return
        if model.strip().lower() in MODEL_PRICING:
            return
        raise ConfigError(
            f"model {model!r} has no configured price and INTENTRADAR_ALLOW_UNPRICED=0 — "
            f"add it to MODEL_PRICING (in/out USD per 1M tokens) in "
            f"src/intentradar/llm/client.py, or set INTENTRADAR_ALLOW_UNPRICED=1 to "
            f"bill it at the conservative fallback estimate "
            f"(${PRICING_FALLBACK_PER_1M['in']:.2f}/${PRICING_FALLBACK_PER_1M['out']:.2f})"
        )

    def complete(
        self,
        system: str,
        user: str,
        tier: str = "everyday",
        temperature: float | None = None,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> LLMResponse:
        """Run one chat completion through the full shell.

        Order: resolve model → cache lookup → gate → budget → call → account.
        """
        model = self.config.model_for(tier)
        self._require_known_price(model)
        temp = self.config.temperature if temperature is None else temperature
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        key = self._cache_key(model, temp, messages)

        if use_cache:
            self._load_cache()
            hit = self._cache.get(key)
            if hit is not None:
                log.debug("cache hit for %s", key[:12])
                return LLMResponse(
                    content=str(hit.get("content", "")),
                    model=str(hit.get("model", model)),
                    prompt_tokens=int(hit.get("prompt_tokens", 0)),
                    completion_tokens=int(hit.get("completion_tokens", 0)),
                    cost_usd=0.0,  # a cache hit never bills twice
                    cached=True,
                    mock=bool(hit.get("mock", self.is_mock)),
                )

        # Hard gate BEFORE spending anything.
        if self.gates is not None:
            self.gates.count_llm_call()
        if self.budget is not None:
            self.budget.require_ok()

        content, prompt_tokens, completion_tokens = self._backend._chat(messages, model)
        cost = _estimate_cost(model, prompt_tokens, completion_tokens)
        response = LLMResponse(
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            cached=False,
            mock=self.is_mock,
        )

        if self.budget is not None:
            self.budget.record_llm(response)
        if use_cache and self._cache_path is not None:
            self._write_cache(key, response)
        return response

    def hello(self, tier: str = "everyday") -> LLMResponse:
        """Smoke-test prompt used by ``intentradar llm hello``."""
        return self.complete(
            system="You are a connectivity probe. Reply with one short sentence.",
            user="Say hello and name the model you are.",
            tier=tier,
        )

    def list_models(self) -> list[str]:
        """Probe ``GET /models``. Raises when unreachable or in mock mode."""
        backend = self._backend
        if isinstance(backend, (HttpBackend, AnthropicBackend)):
            return backend.list_models()
        raise ProviderError("nemotron", "list_models requires a real API key and a live endpoint")

    @property
    def backend_protocol(self) -> str:
        """Which wire protocol this client actually speaks (for honest reporting)."""
        if isinstance(self._backend, AnthropicBackend):
            return BACKEND_ANTHROPIC
        if isinstance(self._backend, HttpBackend):
            return BACKEND_OPENAI
        return "mock"
