"""The pipeline: collect → judge → de-noise → dedupe → sort → report.

One-way flow, no loops back. Both collaborators (``SourceProvider`` and
``Judge``) are protocols, so adding an LLM judge or a fallback provider later
does not touch this file.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from intentradar.budget import BudgetGuard, RunGates
from intentradar.collect import SourceProvider
from intentradar.config import ProjectConfig, Settings, Watchlist
from intentradar.errors import ProviderError
from intentradar.judge import Judge, get_judge
from intentradar.models import LAYER_RULE_V3, Lead, Post
from intentradar.report import ProjectSection, ReportWriter
from intentradar.state import StateStore

log = logging.getLogger(__name__)


@dataclass
class ProjectRunResult:
    """Outcome of running one project."""

    name: str
    scanned: int = 0  # posts inside the time window
    leads: list[Lead] = field(default_factory=list)
    credits_charged: int = 0
    credits_remaining: int | None = None
    layer: str = LAYER_RULE_V3
    judge_version: str = ""
    min_score: int = 0

    @property
    def hit_rate(self) -> float:
        """hits / scanned, 0.0 when nothing was scanned."""
        if not self.scanned:
            return 0.0
        return len(self.leads) / self.scanned

    def to_section(self) -> ProjectSection:
        """Convert to the report section shape."""
        return ProjectSection(
            name=self.name,
            scanned=self.scanned,
            leads=self.leads,
            layer=self.layer,
            judge_version=self.judge_version,
        )


def normalize_title(title: str) -> str:
    """Crosspost de-duplication key (verbatim from monitor.py)."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower())[:60]


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    """Keep one lead per normalized title — the one with the most upvotes."""
    deduped: list[Lead] = []
    seen: dict[str, Lead] = {}
    for lead in leads:
        key = normalize_title(lead.post.title)
        if key in seen:
            if (lead.post.upvotes or 0) > (seen[key].post.upvotes or 0):
                deduped[deduped.index(seen[key])] = lead
                seen[key] = lead
            continue
        seen[key] = lead
        deduped.append(lead)
    return deduped


class Pipeline:
    """Wires the stages together and enforces the gates along the way."""

    def __init__(
        self,
        settings: Settings,
        watchlist: Watchlist | None = None,
        provider: SourceProvider | None = None,
        judge: Judge | None = None,
        budget: BudgetGuard | None = None,
        gates: RunGates | None = None,
        state: StateStore | None = None,
        reporter: ReportWriter | None = None,
        now_fn: Any = None,
    ) -> None:
        """Build the pipeline, defaulting every collaborator from ``settings``."""
        self.settings = settings
        self.watchlist = watchlist or Watchlist.load(settings.watchlist_path)
        self.provider = provider
        self.judge = judge or get_judge(LAYER_RULE_V3)
        self.budget = budget or BudgetGuard(
            path=settings.usage_path, monthly_budget_usd=settings.monthly_budget_usd
        )
        self.gates = gates or RunGates(
            max_posts_per_source=settings.max_posts_per_source,
            max_llm_calls=settings.max_llm_calls,
        )
        self.state = state or StateStore(settings.state_path)
        self.reporter = reporter or ReportWriter(settings.reports_dir)
        self._now = now_fn or time.time

    # ── stages ─────────────────────────────────────────────────────────────
    def _min_score(self, project: ProjectConfig, override: int | None) -> int:
        """v3 behaviour: ``--min 0`` is falsy and falls back to the config."""
        return override or self.watchlist.min_score_for(project)

    def _collect(self, project: ProjectConfig, cache: str | None) -> tuple[list[Post], int, int | None]:
        """Fetch every subreddit of a project, honouring the soft post cap."""
        if self.provider is None:
            raise ProviderError("collect", "no SourceProvider configured")
        pages = self.watchlist.global_config.pages_per_sub
        posts: list[Post] = []
        charged = 0
        remaining: int | None = None
        for sub in project.subreddits:
            try:
                result = self.provider.fetch_subreddit(sub, pages=pages, cache=cache)
            except ProviderError as exc:
                # Never swallow: report and continue with the next subreddit,
                # exactly like the original script did.
                log.error("%s", exc.message)
                continue
            charged += result.credits_charged
            if result.credits_remaining is not None:
                remaining = result.credits_remaining
            for post in result.posts:
                if not self.gates.count_post():
                    break
                posts.append(post)
        if charged:
            self.budget.record_credits(charged)
        return posts, charged, remaining

    def run_project(
        self, project: ProjectConfig, min_score_override: int | None = None, cache: str | None = "1d"
    ) -> ProjectRunResult:
        """Run the full flow for one project and return its result."""
        min_score = self._min_score(project, min_score_override)
        window_days = self.watchlist.global_config.window_days
        cutoff = self._now() - window_days * 86400

        posts, charged, remaining = self._collect(project, cache)
        seen = self.state.get_seen(project.name)

        leads: list[Lead] = []
        scanned = 0
        for post in posts:
            if not post.id:
                continue
            if post.created_utc and post.created_utc < cutoff:
                continue
            scanned += 1
            if post.id in seen:
                continue
            judgment = self.judge.judge(post, project)
            if judgment.score < min_score:
                continue
            if self.judge.is_noise(post):
                continue
            leads.append(Lead(post=post, judgment=judgment))

        leads = dedupe_leads(leads)
        leads.sort(key=lambda lead: -lead.judgment.score)  # stable: ties keep discovery order

        return ProjectRunResult(
            name=project.name,
            scanned=scanned,
            leads=leads,
            credits_charged=charged,
            credits_remaining=remaining,
            layer=self.judge.layer,
            judge_version=self.judge.judge_version,
            min_score=min_score,
        )

    # ── entry point ────────────────────────────────────────────────────────
    def run(
        self,
        project_names: list[str] | None = None,
        min_score_override: int | None = None,
        cache: str | None = "1d",
        dry: bool = False,
        reset_state: bool = False,
        write_report: bool = True,
    ) -> list[ProjectRunResult]:
        """Run every enabled project (or the named ones) and write the report."""
        self.budget.require_ok()

        projects = self.watchlist.enabled_projects()
        if project_names:
            wanted = set(project_names)
            projects = [p for p in projects if p.name in wanted]
            missing = wanted - {p.name for p in projects}
            if missing:
                from intentradar.errors import ConfigError

                raise ConfigError(
                    f"watchlist.projects: unknown or disabled project(s) {sorted(missing)}"
                )

        if reset_state:
            self.state.clear()

        results: list[ProjectRunResult] = []
        for project in projects:
            result = self.run_project(project, min_score_override, cache)
            results.append(result)
            if not dry:
                self.state.mark_seen(project.name, [lead.post.id for lead in result.leads])
            print(
                f"[{result.name}] {result.scanned} posts in window → "
                f"{len(result.leads)} over threshold "
                f"(layer={result.layer}, judge={result.judge_version})"
            )

        if write_report and results:
            date = datetime.now().strftime("%Y-%m-%d")
            paths = self.reporter.write(date, [r.to_section() for r in results])
            log.info("report written: %s", paths.markdown)
            print(f"report: {paths.markdown}")

        return results
