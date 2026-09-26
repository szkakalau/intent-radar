"""Data-source providers.

W1 ships one implementation (ScrapeCreators). The :class:`SourceProvider`
protocol is the seam where a fallback provider will be plugged in before W3 —
the pipeline must not change when that happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from intentradar.models import Post

__all__ = ["SourceProvider", "SourceResult"]


@dataclass
class SourceResult:
    """What a provider returns for one subreddit."""

    posts: list[Post] = field(default_factory=list)
    credits_charged: int = 0
    credits_remaining: int | None = None


@runtime_checkable
class SourceProvider(Protocol):
    """Anything that can return the recent posts of a subreddit."""

    name: str

    def fetch_subreddit(
        self, sub: str, pages: int = 2, cache: str | None = "1d"
    ) -> SourceResult:
        """Fetch up to ``pages`` pages of `sort=new` posts for ``sub``."""
        ...
