"""Cost gates and the monthly budget circuit-breaker (P0-5).

Two independent mechanisms:

* :class:`RunGates` — per-run limits. ``MAX_POSTS_PER_SOURCE`` is a *soft* cap
  (stop paginating, warn, keep going); ``MAX_LLM_CALLS`` is a **hard stop**.
* :class:`BudgetGuard` — a monthly USD budget persisted to ``data/usage.json`` so
  it accumulates across processes. 90% warns, 100% refuses to run.

Persistence is atomic (write ``.tmp`` then ``os.replace``) and guarded by a
``threading.Lock``. A month rollover (local time) resets the counters.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from intentradar.errors import BudgetExceeded, GateExceeded

log = logging.getLogger(__name__)

WARN_RATIO = 0.9  # 90% -> WARNING
BLOCK_RATIO = 1.0  # 100% -> refuse to run


@dataclass
class Usage:
    """Persisted monthly usage counters."""

    month: str = ""
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    scrape_credits: int = 0

    @staticmethod
    def current_month() -> str:
        """Local-time month key, e.g. ``2026-09``."""
        return datetime.now().strftime("%Y-%m")

    @classmethod
    def fresh(cls) -> Usage:
        """A zeroed usage record for the current month."""
        return cls(month=cls.current_month())

    def to_dict(self) -> dict[str, Any]:
        """Serialise for disk."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Usage:
        """Deserialise, tolerating missing keys."""
        return cls(
            month=str(raw.get("month", "")),
            llm_calls=int(raw.get("llm_calls", 0) or 0),
            prompt_tokens=int(raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(raw.get("completion_tokens", 0) or 0),
            cost_usd=float(raw.get("cost_usd", 0.0) or 0.0),
            scrape_credits=int(raw.get("scrape_credits", 0) or 0),
        )


class BudgetState(StrEnum):
    """Result of a budget check."""

    OK = "ok"
    WARN = "warn"
    BLOCKED = "blocked"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a temp file + os.replace so readers never see a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class BudgetGuard:
    """Monthly spend guard. Persisted, cross-process, auto-resetting monthly."""

    path: Path
    monthly_budget_usd: float = 20.0
    warn_ratio: float = WARN_RATIO
    block_ratio: float = BLOCK_RATIO
    usage: Usage = field(default_factory=Usage.fresh)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        """Load (or reset) usage from disk."""
        self.path = Path(self.path)
        self.usage = self._load()

    # ── persistence ────────────────────────────────────────────────────────
    def _load(self) -> Usage:
        """Read usage.json; reset when the month has rolled over."""
        if not self.path.exists():
            return Usage.fresh()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("usage file unreadable (%s), starting fresh", exc)
            return Usage.fresh()
        usage = Usage.from_dict(raw)
        if usage.month != Usage.current_month():
            log.info("month rolled over (%s -> %s), resetting usage", usage.month, Usage.current_month())
            return Usage.fresh()
        return usage

    def _persist(self) -> None:
        """Atomically write the current counters to disk."""
        _atomic_write_json(self.path, self.usage.to_dict())

    # ── accounting ─────────────────────────────────────────────────────────
    def record_llm(self, response: Any) -> None:
        """Add one LLM call's tokens and cost to the monthly tally."""
        with self._lock:
            self.usage.llm_calls += 1
            self.usage.prompt_tokens += int(getattr(response, "prompt_tokens", 0) or 0)
            self.usage.completion_tokens += int(getattr(response, "completion_tokens", 0) or 0)
            self.usage.cost_usd += float(getattr(response, "cost_usd", 0.0) or 0.0)
            self._persist()

    def record_credits(self, n: int) -> None:
        """Add ScrapeCreators credits to the monthly tally."""
        if not n:
            return
        with self._lock:
            self.usage.scrape_credits += int(n)
            self._persist()

    def reset(self) -> None:
        """Zero the counters (``intentradar budget --reset``)."""
        with self._lock:
            self.usage = Usage.fresh()
            self._persist()

    # ── checks ─────────────────────────────────────────────────────────────
    @property
    def ratio(self) -> float:
        """Fraction of the monthly budget consumed."""
        if self.monthly_budget_usd <= 0:
            return 0.0
        return self.usage.cost_usd / self.monthly_budget_usd

    def check(self) -> BudgetState:
        """Return the current budget state (BLOCKED only at >= 100%)."""
        ratio = self.ratio
        if ratio >= self.block_ratio:
            return BudgetState.BLOCKED
        if ratio >= self.warn_ratio:
            return BudgetState.WARN
        return BudgetState.OK

    def require_ok(self) -> BudgetState:
        """Check and raise :class:`BudgetExceeded` when the budget is exhausted."""
        state = self.check()
        if state is BudgetState.BLOCKED:
            raise BudgetExceeded(
                self.usage.cost_usd,
                self.monthly_budget_usd,
                "run `intentradar budget --reset` after topping up, or raise "
                "INTENTRADAR_MONTHLY_BUDGET_USD",
            )
        if state is BudgetState.WARN:
            log.warning(
                "monthly budget at %.0f%% ($%.2f / $%.2f)",
                self.ratio * 100,
                self.usage.cost_usd,
                self.monthly_budget_usd,
            )
        return state

    def summary(self) -> str:
        """One-line human summary for the terminal."""
        return (
            f"本月已用 ${self.usage.cost_usd:.2f} / ${self.monthly_budget_usd:.2f}"
            f" ({self.ratio * 100:.0f}%) · LLM {self.usage.llm_calls} calls"
            f" · {self.usage.prompt_tokens}in/{self.usage.completion_tokens}out tokens"
            f" · credits {self.usage.scrape_credits}"
        )


@dataclass
class RunGates:
    """Per-run limits. Only ``max_llm_calls`` is a hard stop."""

    max_posts_per_source: int = 200
    max_llm_calls: int = 50
    posts_seen: int = 0
    llm_calls: int = 0

    def count_post(self) -> bool:
        """Account for one collected post.

        Returns:
            ``False`` when the soft cap is reached (caller should stop
            paginating); the run itself is not aborted.
        """
        self.posts_seen += 1
        if self.max_posts_per_source > 0 and self.posts_seen > self.max_posts_per_source:
            log.warning(
                "MAX_POSTS_PER_SOURCE=%d reached, stopping collection early",
                self.max_posts_per_source,
            )
            return False
        return True

    def count_llm_call(self) -> None:
        """Account for one LLM call. Raises :class:`GateExceeded` past the limit."""
        if self.max_llm_calls >= 0 and self.llm_calls >= self.max_llm_calls:
            raise GateExceeded(
                "MAX_LLM_CALLS",
                self.max_llm_calls,
                f"{self.llm_calls} calls already made this run — raise "
                "INTENTRADAR_MAX_LLM_CALLS if this was intentional",
            )
        self.llm_calls += 1

    def summary(self) -> str:
        """One-line human summary for the terminal."""
        return f"posts {self.posts_seen}/{self.max_posts_per_source} · LLM {self.llm_calls}/{self.max_llm_calls}"


def load_usage(path: Path) -> Usage:
    """Read usage.json without constructing a guard (useful for reporting)."""
    guard = BudgetGuard(path=Path(path))
    return guard.usage


def credits_remaining_hint(value: int | None) -> str:
    """Format the provider's remaining-credits counter, or '' when unknown."""
    if value is None:
        return ""
    return f" · 余额 {value}"
