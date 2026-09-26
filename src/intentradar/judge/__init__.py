"""Judgment layer: a Strategy protocol plus a factory.

W1 ships a single implementation (``rule_v3``). W2 adds ``rule_v3+llm`` by
registering it in :func:`get_judge` — the pipeline does not change.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from intentradar.config import ProjectConfig
from intentradar.models import JUDGE_VERSION, LAYER_RULE_V3, Judgment, Post

log = logging.getLogger(__name__)

__all__ = ["Judge", "get_judge", "LAYER_RULE_V3"]


@runtime_checkable
class Judge(Protocol):
    """Anything that can turn a post into a judgment."""

    layer: str
    judge_version: str

    def judge(self, post: Post, project: ProjectConfig) -> Judgment:
        """Score one post. Pure function: no network, no file I/O."""
        ...


def get_judge(layer: str = LAYER_RULE_V3) -> Judge:
    """Return the judge implementation for ``layer``.

    Raises:
        ConfigError: if the layer is unknown.
    """
    from intentradar.errors import ConfigError
    from intentradar.judge.rule_v3 import RuleV3Judge

    registry: dict[str, Judge] = {
        LAYER_RULE_V3: RuleV3Judge(),
    }
    judge: Judge | None = registry.get(layer)
    if judge is None:
        raise ConfigError(
            f"judge layer {layer!r} is not implemented "
            f"(available: {', '.join(sorted(registry))})"
        )
    log.debug("judge layer=%s version=%s", judge.layer, judge.judge_version)
    return judge


def current_judge_version() -> str:
    """Version of the default judge, printed next to every published number."""
    return JUDGE_VERSION
