"""Nebius Token Factory / NVIDIA Nemotron client.

Everything (cache, retries, timeout, budget gate, cost accounting, call
counting) lives in :class:`~intentradar.llm.client.NemotronClient`. The mock and
HTTP backends only implement ``_chat()``, so swapping the mock for the real API
is a matter of filling in ``NEBIUS_API_KEY`` — no code change.
"""

from __future__ import annotations

from intentradar.llm.client import (
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
]
