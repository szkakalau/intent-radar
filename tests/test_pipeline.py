"""Pipeline integration: collect → judge → noise filter → dedupe → report."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from conftest import REPO_ROOT

from intentradar.budget import BudgetGuard, RunGates
from intentradar.collect import SourceResult
from intentradar.config import Settings, Watchlist
from intentradar.models import Lead, Post
from intentradar.pipeline import Pipeline, ProjectRunResult, dedupe_leads, normalize_title
from intentradar.report import ReportWriter, format_evidence

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"
FIXED_NOW = datetime(2026, 9, 27, 12, 0, 0).timestamp()


class _StubProvider:
    """Returns a fixed corpus per subreddit; never touches the network."""

    def __init__(self, posts: list[Post], fail_sub: str = "") -> None:
        """Initialise with corpus and an optional subreddit to fail on."""
        self.posts = posts
        self.fail_sub = fail_sub
        self.credits_charged = 2

    def fetch_subreddit(self, sub: str, pages: int = 2, cache: str | None = "1d") -> SourceResult:
        """Return posts for one subreddit."""
        if sub == self.fail_sub:
            from intentradar.errors import ProviderError

            raise ProviderError("scrapecreators", f"HTTP 500 for r/{sub}")
        matched = [p for p in self.posts if p.sub == sub]
        return SourceResult(posts=matched, credits_charged=2, credits_remaining=50)


def _settings(tmp_data_dir: Path) -> Settings:
    """Settings whose data dir is the per-test temp dir (via conftest fixture)."""
    return Settings.from_env(root=REPO_ROOT)


def _post(pid: str, sub: str, title: str, body: str = "", minutes_ago: int = 60,
          upvotes: int = 1) -> Post:
    """Build a post N minutes before the fixed 'now'."""
    return Post(
        id=pid,
        sub=sub,
        title=title,
        selftext=body,
        created_utc=int(FIXED_NOW - minutes_ago * 60),
        upvotes=upvotes,
        num_comments=1,
    )


CORPUS = [
    # strong intent + category + pain -> hit
    _post("hit1", "Anki", "Which flashcard app instead of quizlet?", "storage is expensive", 30, 5),
    # crosspost of hit1 in another sub, fewer upvotes -> deduped away
    _post("hit1x", "GetStudying", "Which flashcard app instead of quizlet?", "storage is expensive", 31, 1),
    # official post with intent words -> filtered by NOISE_TITLE_RE
    _post("noise1", "Anki", "Weekly Thread: beta feedback", "flashcard storage cost", 40, 9),
    # no category word -> category gate returns 0
    _post("gate1", "Anki", "I hate this so much", "looking for a better than alternative", 50, 3),
    # outside the 3-day window
    _post("old1", "Anki", "recommend an anki app for storage", "too expensive", 60 * 24 * 5, 4),
    # too weak
    _post("weak1", "Anki", "hello world", "nothing relevant", 55, 2),
]


def test_pipeline_end_to_end(tmp_data_dir: Path) -> None:
    """A full run produces exactly the expected hits and writes a report."""
    settings = _settings(tmp_data_dir)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    pipeline = Pipeline(
        settings=settings,
        watchlist=watchlist,
        provider=_StubProvider(CORPUS),
        now_fn=lambda: FIXED_NOW,
    )

    results = pipeline.run(write_report=True)
    assert len(results) == 2  # Einprag + IntentRadar_自举 (AimFast disabled)

    einprag = next(r for r in results if r.name == "Einprag")
    ids = [lead.post.id for lead in einprag.leads]
    assert ids == ["hit1"], ids
    assert einprag.scanned == 5  # everything except the out-of-window post
    assert einprag.hit_rate == 1 / 5
    assert einprag.layer == "rule_v3"
    assert einprag.judge_version == "v3.0.0"

    today = datetime.now().strftime("%Y-%m-%d")
    md = (Path(settings.reports_dir) / f"{today}.md").read_text(encoding="utf-8")
    assert "### [10分] Which flashcard app instead of quizlet?" in md
    assert "证据：" in md  # every lead shows machine-readable evidence
    assert "switch[instead\\s+of] → \"instead of\"" in md
    assert "信噪过滤" not in md  # noise gate is silent, hits are what matter

    payload = json.loads((Path(settings.reports_dir) / f"{today}.json").read_text(encoding="utf-8"))
    assert payload["projects"]["Einprag"]["leads"][0]["judge_version"] == "v3.0.0"


def test_provider_failure_does_not_abort_the_run(tmp_data_dir: Path) -> None:
    """One failing subreddit is logged; the rest of the project still runs."""
    settings = _settings(tmp_data_dir)
    pipeline = Pipeline(
        settings=settings,
        watchlist=Watchlist.load(WATCHLIST_PATH),
        provider=_StubProvider(CORPUS, fail_sub="Anki"),
        now_fn=lambda: FIXED_NOW,
    )
    result = pipeline.run_project(Watchlist.load(WATCHLIST_PATH).get("Einprag"))
    # Anki failed, so the crosspost in GetStudying survives and becomes the hit.
    assert [lead.post.id for lead in result.leads] == ["hit1x"]


def test_state_suppresses_already_reported_leads(tmp_data_dir: Path) -> None:
    """Seen ids are not reported twice.

    v3 semantics preserved: only *hits* are marked seen (monitor.py did
    ``seen | {hit_ids}``), so the crosspost that lost de-duplication is still
    eligible next run — once the winner is seen, the runner-up surfaces.
    """
    settings = _settings(tmp_data_dir)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    first = Pipeline(settings=settings, watchlist=watchlist, provider=_StubProvider(CORPUS),
                     now_fn=lambda: FIXED_NOW)
    assert [lead.post.id for lead in first.run(write_report=False)[0].leads] == ["hit1"]

    second = Pipeline(settings=settings, watchlist=watchlist, provider=_StubProvider(CORPUS),
                      now_fn=lambda: FIXED_NOW)
    einprag = next(r for r in second.run(write_report=False) if r.name == "Einprag")
    assert [lead.post.id for lead in einprag.leads] == ["hit1x"]  # crosspost takes over
    assert einprag.scanned == 5  # the corpus is still scanned in full


def test_dry_run_does_not_persist_state(tmp_data_dir: Path) -> None:
    """--dry leaves state untouched, so the next run sees the same posts."""
    settings = _settings(tmp_data_dir)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    pipeline = Pipeline(settings=settings, watchlist=watchlist, provider=_StubProvider(CORPUS),
                        now_fn=lambda: FIXED_NOW)
    pipeline.run(dry=True, write_report=False)
    assert not Path(settings.state_path).exists()


def test_soft_post_cap_stops_collection(tmp_data_dir: Path) -> None:
    """MAX_POSTS_PER_SOURCE is a soft cap: fewer posts, no exception."""
    settings = _settings(tmp_data_dir)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    gates = RunGates(max_posts_per_source=2, max_llm_calls=50)
    pipeline = Pipeline(settings=settings, watchlist=watchlist, provider=_StubProvider(CORPUS),
                        gates=gates, now_fn=lambda: FIXED_NOW)
    result = pipeline.run_project(watchlist.get("Einprag"))
    assert result.scanned <= 2


def test_budget_exhausted_refuses_to_run(tmp_data_dir: Path) -> None:
    """At >= 100% of the monthly budget the pipeline stops before collecting."""
    from intentradar.errors import BudgetExceeded

    settings = _settings(tmp_data_dir)
    budget = BudgetGuard(path=settings.usage_path, monthly_budget_usd=1.0)
    budget.record_llm(type("R", (), {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 1.0})())
    pipeline = Pipeline(settings=settings, watchlist=Watchlist.load(WATCHLIST_PATH),
                        provider=_StubProvider(CORPUS), budget=budget, now_fn=lambda: FIXED_NOW)
    try:
        pipeline.run(write_report=False)
    except BudgetExceeded as exc:
        assert exc.exit_code == 3
    else:  # pragma: no cover
        raise AssertionError("expected BudgetExceeded")


def test_unknown_project_name_is_a_config_error(tmp_data_dir: Path) -> None:
    """A typo in --project fails before any network call."""
    from intentradar.errors import ConfigError

    settings = _settings(tmp_data_dir)
    pipeline = Pipeline(settings=settings, watchlist=Watchlist.load(WATCHLIST_PATH),
                        provider=_StubProvider([]), now_fn=lambda: FIXED_NOW)
    try:
        pipeline.run(project_names=["Nope"], write_report=False)
    except ConfigError as exc:
        assert "Nope" in exc.message
    else:  # pragma: no cover
        raise AssertionError("expected ConfigError")


def test_dedupe_keeps_the_hottest_crosspost() -> None:
    """Same normalized title -> keep the highest-upvote post, stable position."""
    from intentradar.judge import get_judge

    judge = get_judge("rule_v3")
    project = Watchlist.load(WATCHLIST_PATH).get("Einprag")
    a_post = _post("a", "Anki", "Need Solution for Storage!", upvotes=1)
    b_post = _post("b", "medicalschool", "Need Solution for Storage!", upvotes=7)
    leads = [Lead(post=p, judgment=judge.judge(p, project)) for p in (a_post, b_post)]

    result = dedupe_leads(leads)
    assert [lead.post.id for lead in result] == ["b"]
    # Punctuation and case are stripped before truncation.
    # Punctuation is removed (not replaced) before truncation.
    assert normalize_title("Need Solution—Storage!") == normalize_title("need solutionstorage")
    assert normalize_title("NEED Solution STORAGE") == "need solution storage"


def test_credits_are_charged_to_the_budget(tmp_data_dir: Path) -> None:
    """The provider's credits_charged lands on the persisted usage record."""
    settings = _settings(tmp_data_dir)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    budget = BudgetGuard(path=settings.usage_path, monthly_budget_usd=20.0)
    pipeline = Pipeline(settings=settings, watchlist=watchlist, provider=_StubProvider(CORPUS),
                        budget=budget, now_fn=lambda: FIXED_NOW)
    pipeline.run_project(watchlist.get("Einprag"))
    assert budget.usage.scrape_credits > 0


def test_report_render_includes_evidence_line(tmp_path: Path) -> None:
    """The report always prints the auditable evidence line (PRD §3.3)."""
    lead = Lead(
        post=_post("hit1", "Anki", "Which flashcard app instead of quizlet?"),
        judgment=type(
            "J",
            (),
            {
                "score": 9,
                "signals": ["求助选型"],
                "why": ["求助:which app"],
                "evidence": [
                    type("E", (), {"type": "ask", "pattern": r"which\s+(?:app|tool|one)\b",
                                   "matched": "which app", "value": None, "cooccur": None})()
                ],
            },
        )(),  # minimal duck-typed judgment
    )
    md = ReportWriter(tmp_path).render_markdown(
        "2026-09-27",
        [type("S", (), {"name": "Einprag", "scanned": 1, "leads": [lead],
                        "layer": "rule_v3", "judge_version": "v3.0.0"})()],
    )
    assert "### [9分]" in md
    assert "命中理由：求助:which app" in md
    assert 'ask[which\\s+(?:app|tool|one)\\b] → "which app"' in md
    assert ProjectRunResult(name="Einprag").hit_rate == 0.0
    assert format_evidence([]) == ""
