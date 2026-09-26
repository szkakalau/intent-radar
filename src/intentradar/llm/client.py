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
    base_url: str = "https://api.tokenfactory.nebius.com/v1"
    model: str = ""
    model_reasoning: str = ""
    timeout_s: float = 60.0
    max_retries: int = 2
    temperature: float = 0.0
    max_tokens: int = 1024
    cache_dir: Path | None = None
    mock: bool = False
    allow_unpriced: bool = True

    def model_for(self, tier: str) -> str:
        """Resolve the slug for ``tier`` (``everyday`` | ``reasoning``)."""
        if self.mock:
            return MOCK_REASONING_MODEL if tier == "reasoning" else MOCK_MODEL
        if tier == "reasoning":
            return self.model_reasoning or self.model
        return self.model


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
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("nemotron", f"unexpected response shape: {json.dumps(data)[:200]}") from exc
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if not prompt_tokens and not completion_tokens:
            # Endpoint omitted usage: fall back to a ~4 chars/token estimate so
            # the budget gate still has something to count.
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

    def _default_backend(self) -> _Backend:
        """Mock when configured (or keyless), otherwise the HTTP backend."""
        if self.config.mock or not self.config.api_key:
            return MockBackend(max_tokens=self.config.max_tokens)
        return self._http_backend()

    def _http_backend(self) -> HttpBackend:
        """Build the real backend (Nebius)."""
        if not self.config.api_key:
            raise ConfigError(
                "env NEBIUS_API_KEY: expected a key for live calls, got empty — "
                "set it in .env, or leave it unset to run in mock mode"
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
        if isinstance(backend, HttpBackend):
            return backend.list_models()
        raise ProviderError("nemotron", "list_models requires a real NEBIUS_API_KEY")
