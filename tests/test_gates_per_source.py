"""MAX_POSTS_PER_SOURCE must be per source, not per run.

Regression: the counter used to be a single global ``posts_seen`` compared
against a per-source cap, so five subreddits of ~40 posts (200 total) tripped a
cap of 200 and silently truncated the *last* subreddit of the project.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from conftest import REPO_ROOT

from intentradar.budget import RunGates
from intentradar.collect import SourceResult
from intentradar.config import Settings, Watchlist
from intentradar.models import Post
from intentradar.pipeline import Pipeline

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"
FIXED_NOW = datetime(2026, 9, 27, 12, 0, 0).timestamp()
SUBS = ["medicalschool", "languagelearning", "Anki", "GetStudying", "premed"]


def _posts(per_sub: int, subs: list[str]) -> list[Post]:
    """``per_sub`` fresh posts in every subreddit of ``subs``."""
    out: list[Post] = []
    for sub in subs:
        for i in range(per_sub):
            out.append(
                Post(
                    id=f"{sub}-{i}",
                    sub=sub,
                    title=f"post {i} in {sub}",
                    selftext="nothing interesting",
                    created_utc=int(FIXED_NOW - 60 * 60),
                    upvotes=1,
                    num_comments=0,
                )
            )
    return out


class _FlatProvider:
    """Returns ``per_sub`` posts for every subreddit it is asked about."""

    def __init__(self, per_sub: int) -> None:
        """Initialise with the number of posts per subreddit."""
        self.per_sub = per_sub

    def fetch_subreddit(self, sub: str, pages: int = 2, cache: str | None = "1d") -> SourceResult:
        """Return a deterministic batch of posts."""
        return SourceResult(
            posts=_posts(self.per_sub, [sub]), credits_charged=2, credits_remaining=50
        )


def test_five_sources_of_forty_do_not_trip_the_cap() -> None:
    """5 x 40 = 200 posts under a cap of 200 → every one is accepted."""
    gates = RunGates(max_posts_per_source=200)
    for sub in SUBS:
        for _ in range(40):
            assert gates.count_post(sub) is True

    assert gates.posts_seen == 200
    assert gates.posts_for("Anki") == 40
    assert len(gates.posts_per_source) == 5


def test_one_source_over_the_cap_still_trips() -> None:
    """A single source past the cap stops — that is what the cap is for."""
    gates = RunGates(max_posts_per_source=200)
    for _ in range(200):
        assert gates.count_post("bigsub") is True
    assert gates.count_post("bigsub") is False
    assert gates.posts_seen == 201
    assert gates.posts_for("bigsub") == 201


def test_a_fresh_source_keeps_its_full_budget() -> None:
    """Capping one source must not consume another source's allowance."""
    gates = RunGates(max_posts_per_source=200)
    for i in range(200):
        assert gates.count_post("first") is bool(i < 200)
    assert gates.count_post("first") is False

    # The second source has not been touched yet, so it gets its full 200.
    for _ in range(200):
        assert gates.count_post("second") is True
    assert gates.posts_seen == 401


def test_pipeline_collects_every_subreddit(tmp_data_dir: Path) -> None:
    """5 subreddits x 40 posts: all 200 reach the judge, no truncation."""
    settings = Settings.from_env(root=REPO_ROOT)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    gates = RunGates(max_posts_per_source=200, max_llm_calls=50)
    pipeline = Pipeline(
        settings=settings,
        watchlist=watchlist,
        provider=_FlatProvider(40),
        gates=gates,
        now_fn=lambda: FIXED_NOW,
    )

    result = pipeline.run_project(watchlist.get("Einprag"))
    assert result.scanned == 200, result.scanned
    assert gates.posts_seen == 200
    assert all(count == 40 for count in gates.posts_per_source.values())


def test_pipeline_caps_each_subreddit_not_the_run(tmp_data_dir: Path) -> None:
    """A cap of 10 with 5 subreddits yields 50 posts, not 10."""
    settings = Settings.from_env(root=REPO_ROOT)
    watchlist = Watchlist.load(WATCHLIST_PATH)
    gates = RunGates(max_posts_per_source=10, max_llm_calls=50)
    pipeline = Pipeline(
        settings=settings,
        watchlist=watchlist,
        provider=_FlatProvider(40),
        gates=gates,
        now_fn=lambda: FIXED_NOW,
    )

    result = pipeline.run_project(watchlist.get("Einprag"))
    assert result.scanned == 50, result.scanned
    assert gates.posts_per_source == {sub: 11 for sub in SUBS}


def test_summary_does_not_read_as_a_global_budget() -> None:
    """The old ``posts 201/200`` string implied the run was over budget."""
    gates = RunGates(max_posts_per_source=200, max_llm_calls=50)
    for sub in SUBS:
        for _ in range(40):
            gates.count_post(sub)

    summary = gates.summary()
    assert "201" not in summary
    assert "200" in summary
    assert "max/source 200" in summary
    assert "5 sources" in summary
    assert "/200" not in summary  # the misleading "total / per-source-cap" shape
