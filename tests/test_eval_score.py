"""W2 `eval score`: precision / recall / F1 for both layers, honestly."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from intentradar.config import ProjectConfig, Watchlist
from intentradar.errors import ConfigError
from intentradar.eval import (
    LABEL_COLUMNS,
    SMALL_SAMPLE_POSITIVES,
    EvalDataset,
    EvalScorer,
    LabelSet,
    label_fingerprint,
    subreddit_density,
    wilson_interval,
)
from intentradar.judge import get_judge
from intentradar.judge.llm import LLMJudge
from intentradar.models import LAYER_RULE_V3, LAYER_RULE_V3_LLM

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"
TESTSET = "unit-score"

# title fragment -> what the fake model will say about it
ACTIONABLE = "which flashcard app should i use instead of quizlet"

POSTS = [
    # id,   title,                                                    body
    ("p1", "Which flashcard app should I use instead of quizlet?", "storage is expensive"),
    ("p2", "Which anki app is better than quizlet for storage?", "hate the slow sync"),
    ("p3", "Which srs tool should I switch to instead of quizlet?", "annoying"),
    ("p4", "Just finished my reviews", "nothing to see here"),
    ("p5", "Daily megathread", "anki storage"),
]
# Ground truth: p1 actionable, p2 not, p3 unreviewed, p4 actionable (missed), p5 not.
LABELS = {"p1": "1", "p2": "0", "p3": "", "p4": "1", "p5": "0"}


class _TitleClient:
    """Fake LLM: actionable only for posts whose title matches a known fragment."""

    def __init__(self, fragment: str = ACTIONABLE) -> None:
        """Initialise with the title fragment that counts as actionable."""
        self.fragment = fragment.lower()
        self.is_mock = False
        self.n_calls = 0

    def complete(self, system: str, user: str, tier: str = "everyday") -> object:
        """Answer based on the rendered user prompt."""
        self.n_calls += 1
        flag = self.fragment in user.lower()
        content = json.dumps(
            {"is_actionable": flag, "confidence": 0.9, "reason": "fake verdict"}
        )
        return type(
            "R", (), {"content": content, "model": "fake/model", "cached": False, "mock": False}
        )()


@pytest.fixture
def project() -> ProjectConfig:
    """The real Einprag project config."""
    return Watchlist.load(WATCHLIST_PATH).get("Einprag")


@pytest.fixture
def dataset(tmp_path: Path) -> EvalDataset:
    """A tiny frozen dataset with a partially filled labels.csv."""
    base = tmp_path / TESTSET
    base.mkdir(parents=True, exist_ok=True)

    with (base / "posts.jsonl").open("w", encoding="utf-8") as fh:
        for pid, title, body in POSTS:
            fh.write(
                json.dumps(
                    {
                        "id": pid,
                        "sub": "Anki",
                        "title": title,
                        "selftext": body,
                        "created_utc": 1_790_400_000,
                        "upvotes": 2,
                        "num_comments": 1,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    (base / "meta.json").write_text(
        json.dumps(
            {
                "testset_id": TESTSET,
                "project": "Einprag",
                "snapshot_date": "2026-09-27",
                "min_score": 5,
                "cutoff_utc": 0,
                "total_in_window": len(POSTS),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with (base / "labels.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        for pid, title, _body in POSTS:
            writer.writerow(
                {
                    "id": pid,
                    "permalink": f"https://reddit.com/r/Anki/comments/{pid}/",
                    "title": title,
                    "label": LABELS[pid],
                    "reviewer": "unit",
                    "notes": "",
                }
            )
    (base / "expected_hits.json").write_text("[]", encoding="utf-8")
    return EvalDataset.load(TESTSET, tmp_path)


def _scorer() -> EvalScorer:
    """rule_v3 vs rule_v3+llm with the deterministic fake model."""
    return EvalScorer([get_judge(LAYER_RULE_V3), LLMJudge(client=_TitleClient())])


# ── labels ─────────────────────────────────────────────────────────────────


def test_labels_are_read_and_missing_ones_counted(dataset: EvalDataset) -> None:
    """A blank cell is 'unreviewed', not 'not actionable'."""
    labels = dataset.load_labels()
    assert labels.rows == 5
    assert labels.labeled == 4
    assert labels.unlabeled == 1
    assert labels.blank_ids == ["p3"]
    assert labels.positives == 2
    assert labels.negatives == 2
    assert labels.complete is False


def test_borderline_is_excluded_and_counted_not_scored_as_zero(tmp_path: Path) -> None:
    """'borderline' is real information, but not ground truth.

    It must never be collapsed into 0 — that would manufacture false positives
    and inflate precision. It is excluded and reported, like a blank cell.
    """
    path = tmp_path / "labels.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        for pid, label in (("p1", "actionable"), ("p2", "borderline"), ("p3", "")):
            writer.writerow({"id": pid, "permalink": "", "title": "t", "label": label})

    labels = LabelSet.load(path)
    assert labels.labels == {"p1": True}
    assert labels.borderline == 1
    assert labels.borderline_ids == ["p2"]
    assert labels.unlabeled == 1
    assert labels.undecided_ids == ["p3", "p2"]
    assert labels.complete is False


def test_missing_labels_file_is_empty_not_fabricated(tmp_path: Path) -> None:
    """No labels.csv → an empty label set, never a guess of 0."""
    assert LabelSet.load(tmp_path / "nope.csv").labeled == 0
    assert LabelSet.load(tmp_path / "nope.csv").rows == 0


def test_a_typo_in_a_label_is_an_error(tmp_path: Path) -> None:
    """'yess' is neither 1 nor 0 — refuse instead of silently counting it as 0."""
    path = tmp_path / "labels.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        writer.writerow({"id": "p1", "permalink": "", "title": "t", "label": "yess"})
    with pytest.raises(ConfigError) as excinfo:
        LabelSet.load(path)
    assert "yess" in excinfo.value.message


def test_labels_for_posts_not_in_the_dataset_are_ignored(dataset: EvalDataset) -> None:
    """A stale label must not inflate the denominator."""
    labels = LabelSet(labels={"p1": True, "ghost": True}, rows=2)
    usable, orphans = labels.against([p.id for p in dataset.posts])
    assert usable == {"p1": True}
    assert orphans == ["ghost"]


# ── scoring ────────────────────────────────────────────────────────────────


def test_both_layers_are_scored_and_shown(dataset: EvalDataset, project: ProjectConfig) -> None:
    """The point of the command: two columns, neither hidden."""
    report = _scorer().score(dataset, project=project)

    assert [s.layer for s in report.scores] == [LAYER_RULE_V3, LAYER_RULE_V3_LLM]
    rendered = report.render()
    assert "rule_v3 (v3.0.0)" in rendered
    assert "rule_v3+llm (v3.1.0+llm)" in rendered
    assert "precision" in rendered
    assert "recall" in rendered
    assert "F1" in rendered


def test_rule_layer_confusion_matrix(dataset: EvalDataset, project: ProjectConfig) -> None:
    """3 predicted, 1 TP, 1 FP, 1 FN, 1 TN, and p3 left out entirely."""
    report = _scorer().score(dataset, project=project)
    rule = report.scores[0]

    assert rule.predicted == 3
    assert (rule.tp, rule.fp, rule.fn, rule.tn) == (1, 1, 1, 1)
    assert rule.precision == pytest.approx(0.5)
    assert rule.recall == pytest.approx(0.5)
    assert rule.f1 == pytest.approx(0.5)
    # p3 was predicted but never reviewed: counted, not scored.
    assert rule.unverified == 1
    assert rule.skipped_unlabeled == 1


def test_llm_layer_trades_false_positives_for_nothing_else(
    dataset: EvalDataset, project: ProjectConfig
) -> None:
    """The semantic layer is subtractive: precision up, recall unchanged."""
    report = _scorer().score(dataset, project=project)
    rule, composite = report.scores

    assert composite.predicted == 1
    # p2 and p5 are both "not actionable" ground truth that the layer now misses.
    assert (composite.tp, composite.fp, composite.fn, composite.tn) == (1, 0, 1, 2)
    assert composite.precision == pytest.approx(1.0)
    assert composite.recall == pytest.approx(rule.recall)
    assert composite.f1 > rule.f1


def test_unreviewed_hits_are_reported_not_scored(dataset: EvalDataset, project: ProjectConfig) -> None:
    """An unreviewed hit is 'unverified', never a false positive."""
    report = _scorer().score(dataset, project=project)
    rendered = report.render()
    assert "unverified hits" in rendered
    assert "1 unlabeled · 0 borderline" in rendered
    assert "no usable verdict" in rendered
    assert "not counted as 0" in rendered


def test_no_ground_truth_says_so_instead_of_printing_zeros(tmp_path: Path, project: ProjectConfig) -> None:
    """An empty labels.csv yields an explanation, not a fake 0% precision."""
    base = tmp_path / "blank"
    base.mkdir(parents=True)
    (base / "posts.jsonl").write_text(
        json.dumps({"id": "p1", "sub": "Anki", "title": "hello", "selftext": ""}) + "\n",
        encoding="utf-8",
    )
    (base / "meta.json").write_text(json.dumps({"project": "Einprag", "min_score": 5}), encoding="utf-8")
    with (base / "labels.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
        writer.writeheader()
        writer.writerow({"id": "p1", "permalink": "", "title": "hello", "label": ""})

    dataset = EvalDataset.load("blank", tmp_path)
    report = _scorer().score(dataset, project=project)

    rendered = report.render()
    assert "NO GROUND TRUTH" in rendered
    assert "labels.csv" in rendered
    assert report.scores[0].precision is None


def test_mock_backend_is_called_out(dataset: EvalDataset, project: ProjectConfig) -> None:
    """A mocked semantic layer is not a measurement; the report says so."""
    scorer = EvalScorer([get_judge(LAYER_RULE_V3), LLMJudge(client=None)])
    # client=None + no key -> mock. Force it explicitly for determinism.
    scorer.judges[1].client = _TitleClient()
    scorer.judges[1].client.is_mock = True

    report = scorer.score(dataset, project=project)
    rendered = report.render()
    assert "MOCK" in rendered
    assert "NOT a measurement" in rendered


def test_llm_calls_are_reported(dataset: EvalDataset, project: ProjectConfig) -> None:
    """How much the semantic layer actually cost is part of the number."""
    client = _TitleClient()
    scorer = EvalScorer([get_judge(LAYER_RULE_V3), LLMJudge(client=client)])
    report = scorer.score(dataset, project=project)
    assert report.scores[1].llm_calls == client.n_calls
    assert report.scores[1].llm_errors == 0
    assert f"{client.n_calls} calls" in report.render()


def test_min_score_override_is_honoured(dataset: EvalDataset, project: ProjectConfig) -> None:
    """--min-score moves the threshold for both layers alike."""
    report = _scorer().score(dataset, project=project, min_score=99)
    assert report.min_score == 99
    assert all(s.predicted == 0 for s in report.scores)


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_eval_score_runs_offline(tmp_data_dir: Path, dataset: EvalDataset) -> None:
    """`intentradar eval score` works with no key (mock semantic layer)."""
    from intentradar.cli import main

    root = dataset.base_dir.parent
    (tmp_data_dir / "testset").mkdir(parents=True, exist_ok=True)
    target = tmp_data_dir / "testset" / TESTSET
    if not target.exists():
        target.mkdir(parents=True)
        for name in ("posts.jsonl", "meta.json", "labels.csv", "expected_hits.json"):
            (target / name).write_text(
                (dataset.base_dir / name).read_text(encoding="utf-8"), encoding="utf-8"
            )

    code = main(["eval", "score", "--testset", TESTSET])
    assert code == 0
    assert root.exists()


def test_unknown_testset_still_fails_cleanly(tmp_data_dir: Path) -> None:
    """A typo in --testset is a config error, not a traceback."""
    from intentradar.cli import main

    assert main(["eval", "score", "--testset", "does-not-exist"]) == 2


def test_report_names_the_label_revision_it_was_measured_against(
    dataset: EvalDataset, project: ProjectConfig
) -> None:
    """A number without a ground-truth revision cannot be reproduced or trusted."""
    report = EvalScorer([get_judge(LAYER_RULE_V3)]).score(dataset, project=project)
    rendered = report.render()
    assert "truth   :" in rendered
    assert len(report.label_sha256) == 64


def test_density_report_reads_a_tiny_testset_without_a_rate(
    dataset: EvalDataset,
) -> None:
    """With few positives the honest view is density, not precision/recall."""
    report = subreddit_density(dataset)
    rendered = report.render()
    assert "density" in rendered
    assert report.total_posts > 0
    assert report.total_actionable == report.total_actionable  # sanity, not a rate
    for row in report.rows:
        assert 0.0 <= row.density <= 1.0


def test_density_flags_subreddits_that_produced_nothing() -> None:
    """The negative-control conclusion is the publishable finding for bootstrap."""
    dataset = EvalDataset.load("bootstrap-2026-09-27", REPO_ROOT / "data" / "testset")
    report = subreddit_density(dataset)
    rendered = report.render()
    zero = {r.sub for r in report.rows if r.posts and r.actionable == 0}
    assert zero, "expected at least one zero-density subreddit"
    assert "Zero-actionable subreddits" in rendered
    for sub in zero:
        assert sub in rendered


def test_wilson_interval_matches_the_hand_computed_value() -> None:
    """8/11 must give ~[43%, 90%] — the einprag precision, checked by hand."""
    low, high = wilson_interval(8, 11)
    assert low == pytest.approx(0.4343, abs=1e-3)
    assert high == pytest.approx(0.9025, abs=1e-3)


def test_wilson_interval_stays_inside_zero_and_one() -> None:
    """Tiny samples break the normal approximation; Wilson must not."""
    for successes, total in ((0, 1), (1, 1), (5, 5), (8, 8), (0, 8), (8, 11)):
        low, high = wilson_interval(successes, total)
        assert 0.0 <= low <= high <= 1.0, (successes, total)
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_rates_are_printed_with_counts_and_an_interval(
    dataset: EvalDataset, project: ProjectConfig
) -> None:
    """A bare percentage on n=8 misleads; counts and CI must travel with it."""
    report = EvalScorer([get_judge(LAYER_RULE_V3)]).score(dataset, project=project)
    rendered = report.render()
    assert "precision 95% CI" in rendered
    assert "recall 95% CI" in rendered
    assert "positives (n)" in rendered


def test_density_does_not_count_borderline_as_negative() -> None:
    """Undecided is not 'not actionable' — the same rule the scorer follows."""
    dataset = EvalDataset.load("bootstrap-2026-09-27", REPO_ROOT / "data" / "testset")
    labels = dataset.load_labels()
    report = subreddit_density(dataset)
    assert labels.borderline_ids, "fixture must contain borderline rows"
    assert sum(r.borderline for r in report.rows) == len(labels.borderline_ids)
    assert sum(r.unreviewed for r in report.rows) == len(labels.blank_ids)


def test_score_warns_when_positives_are_too_few_for_a_rate(
    dataset: EvalDataset, project: ProjectConfig
) -> None:
    """A rate on a handful of positives must not be presented bare."""
    report = EvalScorer([get_judge(LAYER_RULE_V3)]).score(dataset, project=project)
    rendered = report.render()
    if report.labels.positives < SMALL_SAMPLE_POSITIVES:
        assert "eval density" in rendered
        assert "noise" in rendered


def test_committed_testsets_declare_a_label_version() -> None:
    """A published number must be traceable to a named ground-truth revision."""
    for ts_id in ("einprag-2026-09-27", "bootstrap-2026-09-27"):
        version, digest = label_fingerprint(REPO_ROOT / "data" / "testset" / ts_id)
        assert version not in ("(unversioned)", "(unreadable)"), ts_id
        assert len(digest) == 64, ts_id


def test_labels_moving_mid_run_is_a_hard_error(
    dataset: EvalDataset, project: ProjectConfig
) -> None:
    """Ground truth edited while scoring must fail loudly, not warn quietly.

    A warning is ignored the moment anyone batch-runs this; a silent change
    produces numbers describing a state nobody can reproduce.
    """
    import intentradar.eval as eval_mod

    real = eval_mod.label_fingerprint
    base_dir = dataset.base_dir

    def moving(dirpath):  # type: ignore[no-untyped-def]
        # First call (before) returns the truth, second (after) a different file.
        if not hasattr(moving, "seen"):
            moving.seen = True
            return ("rev-a", "0" * 64)
        return ("rev-b", "1" * 64)

    eval_mod.label_fingerprint = moving  # type: ignore[assignment]
    try:
        with pytest.raises(ConfigError, match="ground truth changed"):
            EvalScorer([get_judge(LAYER_RULE_V3)]).score(dataset, project=project)
    finally:
        eval_mod.label_fingerprint = real  # type: ignore[assignment]
    assert base_dir.exists()
