"""Evaluation tests: offline recomputation, exports, snapshot (P0-6)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT, TESTSET_DIR

from intentradar.collect import SourceResult
from intentradar.config import Watchlist
from intentradar.eval import CSV_COLUMNS, EvalDataset, EvalRunner
from intentradar.models import Post

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"


class _FakeProvider:
    """Deterministic stand-in for a live provider (no network)."""

    def __init__(self, posts: list[Post]) -> None:
        """Initialise with the posts to return."""
        self.posts = posts
        self.calls: list[str] = []

    def fetch_subreddit(self, sub: str, pages: int = 2, cache: str | None = "1d") -> SourceResult:
        """Return the posts whose sub matches."""
        self.calls.append(sub)
        matched = [p for p in self.posts if p.sub == sub]
        return SourceResult(posts=matched, credits_charged=2, credits_remaining=50)


def _posts() -> list[Post]:
    """A tiny deterministic corpus."""
    return [
        Post(
            id="aaa111",
            sub="Anki",
            title="Which flashcard app should I use?",
            selftext="quizlet is too expensive and anki storage is annoying",
            created_utc=1_790_400_000,
            upvotes=3,
            num_comments=2,
        ),
        Post(
            id="bbb222",
            sub="Anki",
            title="Daily megathread",
            selftext="anki storage slow",
            created_utc=1_790_400_100,
            upvotes=1,
            num_comments=0,
        ),
        Post(
            id="ccc333",
            sub="Anki",
            title="Just finished my reviews",
            selftext="nothing to see here",
            created_utc=1_790_400_200,
            upvotes=8,
            num_comments=1,
        ),
    ]


@pytest.fixture
def dataset_id() -> str:
    """The frozen dataset id used by the offline tests."""
    return "einprag-2026-09-27"


def test_eval_run_reports_the_four_numbers(dataset_id: str) -> None:
    """`eval run` prints total / hits / hit rate and matches the frozen expectation."""
    if not (TESTSET_DIR / dataset_id / "posts.jsonl").exists():
        pytest.skip("frozen dataset not present")

    dataset = EvalDataset.load(dataset_id, TESTSET_DIR)
    report = EvalRunner(watchlist_path=WATCHLIST_PATH).run(dataset)

    assert report.total > 0
    assert report.hit_count == 6
    assert report.hit_rate_pct == 2.8
    assert report.layer == "rule_v3"
    assert report.judge_version == "v3.0.0"
    assert report.expected_match is True

    rendered = report.render()
    for token in (str(report.total), str(report.hit_count), "2.8%", "rule_v3", "v3.0.0"):
        assert token in rendered


def test_eval_run_reports_real_review_coverage(dataset_id: str) -> None:
    """`eval run` states the review it actually has, not a W1 placeholder.

    The old block hard-printed "no manual review included in this run (…W2)"
    long after labels.csv had been filled in. It is exactly the kind of stale
    string that ends up quoted verbatim in a README, so it is pinned.
    """
    if not (TESTSET_DIR / dataset_id / "posts.jsonl").exists():
        pytest.skip("frozen dataset not present")

    dataset = EvalDataset.load(dataset_id, TESTSET_DIR)
    report = EvalRunner(watchlist_path=WATCHLIST_PATH).run(dataset)
    rendered = report.render()

    assert report.label_set is not None
    assert report.label_set.rows > 0
    assert "no manual review included" not in rendered
    assert "delivered in W2" not in rendered

    decided = [h.post.id for h in report.hits if h.post.id in report.label_set.labels]
    assert "reviewed hits" in rendered
    assert f"{len(decided)}/{report.hit_count}" in rendered
    # Undecided hits must be visible rather than silently folded into the rate.
    undecided = report.hit_count - len(decided)
    assert f"({undecided} undecided" in rendered
    assert "eval score --testset einprag-2026-09-27" in rendered
    # The ground-truth line must agree with eval score's own counts.
    assert f"{report.label_set.positives} actionable" in rendered


def test_eval_run_says_so_when_there_are_no_labels() -> None:
    """With no labels.csv the report says the hit rate is un-scored, not 0% noise."""
    if not (TESTSET_DIR / "einprag-2026-09-27" / "posts.jsonl").exists():
        pytest.skip("frozen dataset not present")

    dataset = EvalDataset.load("einprag-2026-09-27", TESTSET_DIR)
    report = EvalRunner(watchlist_path=WATCHLIST_PATH).run(dataset)
    report.label_set = None

    rendered = report.render()
    assert "no labels.csv" in rendered
    assert "ground truth:" not in rendered
    assert "reviewed hits" not in rendered


def test_dataset_roundtrip(tmp_path: Path) -> None:
    """A dataset loaded from disk exposes its metadata and posts."""
    dataset = EvalDataset.load("einprag-2026-09-27", TESTSET_DIR)
    assert dataset.meta["project"] == "Einprag"
    assert dataset.testset_id == "einprag-2026-09-27"
    assert dataset.posts
    assert dataset.expected_hits
    assert dataset.cutoff_utc > 0
    assert len(dataset.in_window()) == dataset.meta["total_in_window"]


def test_list_available() -> None:
    """Listing datasets finds the frozen ones."""
    available = EvalDataset.list_available(TESTSET_DIR)
    assert "einprag-2026-09-27" in available


def test_export_csv_columns_and_rows(tmp_path: Path) -> None:
    """CSV export is directly openable and has one row per hit."""
    if not (TESTSET_DIR / "einprag-2026-09-27" / "posts.jsonl").exists():
        pytest.skip("frozen dataset not present")
    dataset = EvalDataset.load("einprag-2026-09-27", TESTSET_DIR)
    report = EvalRunner(watchlist_path=WATCHLIST_PATH).run(dataset)
    out = tmp_path / "hits.csv"
    EvalRunner(watchlist_path=WATCHLIST_PATH).export(report, "csv", out)

    with out.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == CSV_COLUMNS
    assert len(rows) == report.hit_count
    for row in rows:
        assert row["layer"] == "rule_v3"
        assert json.loads(row["evidence_json"])  # machine-readable evidence survives


def test_export_jsonl(tmp_path: Path) -> None:
    """JSONL export has one valid contract object per line."""
    if not (TESTSET_DIR / "einprag-2026-09-27" / "posts.jsonl").exists():
        pytest.skip("frozen dataset not present")
    dataset = EvalDataset.load("einprag-2026-09-27", TESTSET_DIR)
    report = EvalRunner(watchlist_path=WATCHLIST_PATH).run(dataset)
    out = tmp_path / "hits.jsonl"
    EvalRunner(watchlist_path=WATCHLIST_PATH).export(report, "jsonl", out)

    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == report.hit_count
    for payload in lines:
        assert payload["evidence"]
        assert payload["judge_version"] == "v3.0.0"


def test_snapshot_is_deterministic_and_strips_author(tmp_path: Path) -> None:
    """Freezing twice yields the same posts/hits; author is never persisted."""
    watchlist = Watchlist.load(WATCHLIST_PATH)
    project = watchlist.get("Einprag")

    posts = [
        Post(
            id="aaa111",
            sub="Anki",
            title="Which flashcard app should I use?",
            selftext="quizlet is too expensive and anki storage is annoying",
            created_utc=1_790_400_000,
            upvotes=3,
            num_comments=2,
        ),
        Post(
            id="ccc333",
            sub="Anki",
            title="Just finished my reviews",
            selftext="nothing to see here",
            created_utc=1_790_400_200,
            upvotes=8,
            num_comments=1,
        ),
    ]
    # Inject an author to prove it is stripped on write.
    for post in posts:
        post.__dict__["author"] = "some_user"

    from intentradar.judge import get_judge

    judge = get_judge("rule_v3")
    provider = _FakeProvider(posts)

    first = EvalDataset.snapshot(
        project=project,
        provider=provider,
        judge=judge,
        root=tmp_path,
        window_days=3,
        min_score=5,
        testset_id="unit-1",
    )
    second = EvalDataset.snapshot(
        project=project,
        provider=provider,
        judge=judge,
        root=tmp_path,
        window_days=3,
        min_score=5,
        testset_id="unit-1",
    )

    assert first.meta["total_in_window"] == second.meta["total_in_window"]
    assert first.expected_hits == second.expected_hits

    written = (tmp_path / "unit-1" / "posts.jsonl").read_text(encoding="utf-8")
    assert "some_user" not in written
    assert "author" not in written
    assert (tmp_path / "unit-1" / "labels.csv").exists()
    assert (tmp_path / "unit-1" / "meta.json").exists()
    assert first.meta["hit_rate_pct"] >= 0


def test_unknown_testset_is_a_config_error(tmp_path: Path) -> None:
    """A typo in --testset fails with a helpful list of what exists."""
    from intentradar.errors import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        EvalDataset.load("does-not-exist", TESTSET_DIR)
    assert "einprag-2026-09-27" in excinfo.value.message
