"""IntentRadar — find Reddit posts that carry buying intent, and publish the accuracy."""

from __future__ import annotations

__version__ = "0.1.0"

# Re-exported so callers can pin a number to a judge version without importing models.
from intentradar.models import (  # noqa: F401
    JUDGE_VERSION,
    JUDGE_VERSION_LLM,
    LAYER_RULE_V3,
    LAYER_RULE_V3_LLM,
)

__all__ = [
    "__version__",
    "JUDGE_VERSION",
    "JUDGE_VERSION_LLM",
    "LAYER_RULE_V3",
    "LAYER_RULE_V3_LLM",
]
