"""Judgment layer: a Strategy protocol plus a factory.

W1 shipped a single implementation (``rule_v3``). W2 adds ``rule_v3+llm`` by
registering it in :func:`get_judge` — the pipeline does not change.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from intentradar.config import ProjectConfig
from intentradar.models import (
    JUDGE_VERSION,
    JUDGE_VERSION_LLM,
    JUDGE_VERSION_V4,
    JUDGE_VERSION_V4_LLM,
    LAYER_RULE_V3,
    LAYER_RULE_V3_LLM,
    LAYER_RULE_V4,
    LAYER_RULE_V4_LLM,
    Judgment,
    Post,
)

log = logging.getLogger(__name__)

__all__ = [
    "Judge",
    "get_judge",
    "AVAILABLE_LAYERS",
    "LAYER_RULE_V3",
    "LAYER_RULE_V3_LLM",
    "LAYER_RULE_V4",
    "LAYER_RULE_V4_LLM",
    "JUDGE_VERSION",
    "JUDGE_VERSION_LLM",
    "JUDGE_VERSION_V4",
    "JUDGE_VERSION_V4_LLM",
]

AVAILABLE_LAYERS = (LAYER_RULE_V3, LAYER_RULE_V3_LLM, LAYER_RULE_V4, LAYER_RULE_V4_LLM)

# Which rule layer sits under each composite "+llm" layer, and which version the
# composite publishes. Explicit, so a new rule layer cannot silently inherit a
# version that belongs to another one.
COMPOSITE_LAYERS: dict[str, tuple[str, str]] = {
    LAYER_RULE_V3_LLM: (LAYER_RULE_V3, JUDGE_VERSION_LLM),
    LAYER_RULE_V4_LLM: (LAYER_RULE_V4, JUDGE_VERSION_V4_LLM),
}


@runtime_checkable
class Judge(Protocol):
    """Anything that can turn a post into a judgment."""

    layer: str
    judge_version: str

    def judge(self, post: Post, project: ProjectConfig) -> Judgment:
        """Score one post. Pure function: no network, no file I/O."""
        ...

    def is_noise(self, post: Post) -> bool:
        """Whether the post is an official / megathread post to drop silently."""
        ...


def get_judge(layer: str = LAYER_RULE_V3, client: Any = None) -> Judge:
    """Return the judge implementation for ``layer``.

    Args:
        layer: ``rule_v3`` or ``rule_v3+llm``.
        client: optional pre-built LLM client for the semantic layer. When it is
            ``None`` the judge builds one from the environment on first use
            (mock when no key is configured).

    Raises:
        ConfigError: if the layer is unknown.
    """
    from intentradar.errors import ConfigError
    from intentradar.judge.llm import LLMJudge
    from intentradar.judge.rule_v3 import RuleV3Judge
    from intentradar.judge.rule_v4 import RuleV4Judge

    judge: Judge
    if layer in COMPOSITE_LAYERS:
        base_layer, _version = COMPOSITE_LAYERS[layer]
        base: Judge = RuleV4Judge() if base_layer == LAYER_RULE_V4 else RuleV3Judge()
        judge = LLMJudge(client=client, rule=base)
    elif layer == LAYER_RULE_V3:
        judge = RuleV3Judge()
    elif layer == LAYER_RULE_V4:
        judge = RuleV4Judge()
    else:
        raise ConfigError(
            f"judge layer {layer!r} is not implemented "
            f"(available: {', '.join(AVAILABLE_LAYERS)})"
        )
    log.debug("judge layer=%s version=%s", judge.layer, judge.judge_version)
    return judge


def current_judge_version() -> str:
    """Version of the default judge, printed next to every published number."""
    return JUDGE_VERSION
