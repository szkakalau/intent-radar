"""Nemotron / OpenAI-compatible LLM client.

Everything (cache, retries, timeout, budget gate, cost accounting, call
counting) lives in :class:`~intentradar.llm.client.NemotronClient`. The mock and
HTTP backends only implement ``_chat()``, so swapping the mock for a real API is
a matter of filling in a key — no code change.

The endpoint is configurable: ``INTENTRADAR_LLM_BASE_URL`` /
``INTENTRADAR_LLM_API_KEY`` / ``INTENTRADAR_LLM_MODEL`` override the
``NEBIUS_*`` defaults. The wire protocol is always OpenAI-compatible
``POST /v1/chat/completions``, so any provider speaking it will work.
"""

from __future__ import annotations

from typing import Any

from intentradar.llm.client import (
    DEFAULT_BASE_URL,
    MODEL_PRICING,
    PRICING_FALLBACK_PER_1M,
    HttpBackend,
    LLMConfig,
    LLMResponse,
    MockBackend,
    NemotronClient,
)

__all__ = [
    "NemotronClient",
    "LLMConfig",
    "LLMResponse",
    "MockBackend",
    "HttpBackend",
    "MODEL_PRICING",
    "PRICING_FALLBACK_PER_1M",
    "DEFAULT_BASE_URL",
    "build_client",
]


def build_client(
    settings: Any,
    budget: Any = None,
    gates: Any = None,
    backend: Any = None,
) -> NemotronClient:
    """Build a client from :class:`~intentradar.config.Settings`.

    Args:
        settings: resolved settings (``Settings.from_env()``).
        budget: optional :class:`~intentradar.budget.BudgetGuard`. Without it the
            monthly breaker does not see LLM spend — always pass one in the CLI.
        gates: optional :class:`~intentradar.budget.RunGates` for ``MAX_LLM_CALLS``.
        backend: optional explicit backend (tests inject one).
    """
    return NemotronClient(
        config=LLMConfig.from_settings(settings),
        budget=budget,
        gates=gates,
        backend=backend,
    )
