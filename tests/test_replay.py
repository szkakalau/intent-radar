"""Frozen responses: a stranger can recompute the LLM column without an endpoint.

The promise "a stranger can recompute our number" is only true of the rule
layer until the model responses are frozen. These tests pin the whole
record → replay loop, and — most importantly — pin the replayed table against
the table published in the README, which is the exact place a stale or
hand-edited number went unnoticed before.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO_ROOT, TESTSET_DIR

from intentradar.config import Watchlist
from intentradar.errors import ConfigError
from intentradar.eval import EvalDataset, EvalScorer
from intentradar.judge import get_judge
from intentradar.replay import (
    CALLS_NAME,
    MANIFEST_NAME,
    FrozenCall,
    ReplayRecorder,
    ReplayStore,
    prompt_digest,
)

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"
DATASET_ID = "einprag-2026-09-27"
COMMITTED_REPLAY = REPO_ROOT / "data" / "replay" / DATASET_ID / "rule_v4_llm"

# The fields the replay must reproduce. `why`/evidence are deliberately NOT
# compared: they are explanatory text, and comparing them would turn "the
# wording changed" into "the result changed", diluting what this proves.
COMPARED_FIELDS = ("predicted", "tp", "fp", "fn", "precision", "recall")


class _ScriptedClient:
    """Deterministic stand-in whose answer depends on the post being judged.

    Deliberately NOT all-``false``: a replay of an all-negative run proves
    nothing, because "everything rejected" reproduces regardless of whether the
    frozen responses were actually used.
    """

    def __init__(self) -> None:
        """Initialise with no calls made yet."""
        self.is_mock = False
        self.n = 0
        self.config = type("C", (), {"base_url": "http://example.invalid/v1", "model": "fake/model"})()

    def complete(self, system: str, user: str, tier: str = "everyday") -> Any:
        """Answer true for every third post, so the table is non-trivial."""
        self.n += 1
        flag = self.n % 3 == 0
        content = json.dumps(
            {"is_actionable": flag, "confidence": 0.8, "reason": f"scripted {self.n}"}
        )
        return type(
            "R",
            (),
            {
                "content": content,
                "model": "fake/model",
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost_usd": 0.0,
                "cached": False,
                "mock": False,
            },
        )()


def _scorer(store: Any = None, recorder: Any = None) -> tuple[EvalScorer, Any]:
    """Build the v4 funnel scorer (both stages), offline, for the frozen dataset."""
    client = None if store is not None else _ScriptedClient()
    judge = get_judge(
        "rule_v4+llm", client=client, store=store, candidate_min_score=3
    )
    if recorder is not None:
        judge.recorder = recorder
    # rule_v4 is the wide net under it; the published table shows both columns.
    return EvalScorer([get_judge("rule_v4"), judge]), judge


def _table(scores: list[Any]) -> dict[str, tuple[Any, ...]]:
    """The fields the replay must reproduce, keyed by field name."""
    return {name: tuple(getattr(s, name) for s in scores) for name in COMPARED_FIELDS}


@pytest.mark.skipif(
    not (TESTSET_DIR / DATASET_ID / "posts.jsonl").exists(),
    reason="frozen dataset not present",
)
def test_record_then_replay_reproduces_the_table_field_for_field(tmp_path: Path) -> None:
    """Replay reproduces predicted / tp / fp / fn / precision / recall exactly."""
    dataset = EvalDataset.load(DATASET_ID, TESTSET_DIR)
    project = Watchlist.load(WATCHLIST_PATH).get("Einprag")

    recorder = ReplayRecorder(
        directory=tmp_path / "rec",
        testset_id=DATASET_ID,
        layer="rule_v4+llm",
        judge_version="v4.5.0+llm",
    )
    live_scorer, _ = _scorer(recorder=recorder)
    live = live_scorer.score(dataset, project=project, min_score=3)
    recorder.close()
    assert recorder.call_count > 0, "nothing was recorded — the test proves nothing"

    store = ReplayStore.load(tmp_path / "rec")
    replay_scorer, _ = _scorer(store=store)
    replayed = replay_scorer.score(
        dataset, project=project, min_score=3, replay_banner=store.banner()
    )

    assert _table(live.scores) == _table(replayed.scores)
    # Non-trivial, or the equality above is vacuous.
    assert any(s.tp for s in live.scores) and any(s.fp for s in live.scores)


@pytest.mark.skipif(
    not (TESTSET_DIR / DATASET_ID / "posts.jsonl").exists(),
    reason="frozen dataset not present",
)
def test_replay_never_falls_back_to_a_live_call(tmp_path: Path) -> None:
    """A gap in the recording fails loudly instead of quietly calling out."""
    dataset = EvalDataset.load(DATASET_ID, TESTSET_DIR)
    project = Watchlist.load(WATCHLIST_PATH).get("Einprag")

    recorder = ReplayRecorder(directory=tmp_path / "rec", testset_id=DATASET_ID)
    scorer, _ = _scorer(recorder=recorder)
    scorer.score(dataset, project=project, min_score=3)
    recorder.close()

    # Drop one call: the replay now has a hole where a post needs an answer.
    calls = (tmp_path / "rec" / CALLS_NAME).read_text(encoding="utf-8").splitlines()
    (tmp_path / "rec" / CALLS_NAME).write_text("\n".join(calls[1:]) + "\n", encoding="utf-8")

    store = ReplayStore.load(tmp_path / "rec")
    replay_scorer, _ = _scorer(store=store)
    with pytest.raises(ConfigError, match="replay miss"):
        replay_scorer.score(dataset, project=project, min_score=3)


def test_a_record_without_provenance_is_refused(tmp_path: Path) -> None:
    """Missing `model` or `prompt_sha256` makes the file a cache, not evidence."""
    directory = tmp_path / "rec"
    directory.mkdir()
    (directory / CALLS_NAME).write_text(
        json.dumps(
            {
                "post_id": "abc",
                "judge_version": "v4.5.0+llm",
                "recorded_at": "2026-01-01T00:00:00Z",
                "response": "{}",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="missing required field"):
        ReplayStore.load(directory)


def test_replay_of_a_prompt_that_changed_fails(tmp_path: Path) -> None:
    """A changed prompt must not silently reuse a response to the old one."""
    directory = tmp_path / "rec"
    recorder = ReplayRecorder(directory=directory, testset_id="t")
    recorder.add(
        FrozenCall(
            post_id="abc",
            judge_version="v4.5.0+llm",
            model="fake/model",
            endpoint="http://example.invalid/v1",
            recorded_at="2026-01-01T00:00:00Z",
            prompt_sha256=prompt_digest("old system", "old user"),
            response='{"is_actionable": true, "confidence": 0.9, "reason": "x"}',
        )
    )
    recorder.close()
    store = ReplayStore.load(directory)
    with pytest.raises(ConfigError, match="DIFFERENT prompt"):
        store.lookup("abc", "v4.5.0+llm", prompt_digest("new system", "new user"))


def test_banner_names_the_model_and_denies_being_live(tmp_path: Path) -> None:
    """`REPLAYED — frozen model responses from <date>, model=<name>. Not a live run.`"""
    directory = tmp_path / "rec"
    recorder = ReplayRecorder(directory=directory, testset_id="t")
    recorder.add(
        FrozenCall(
            post_id="abc",
            judge_version="v4.5.0+llm",
            model="deepseek-v4-flash",
            endpoint="http://127.0.0.1:8787/v1",
            recorded_at="2026-09-27T07:28:41Z",
            prompt_sha256="0" * 64,
            response="{}",
        )
    )
    recorder.close()
    banner = ReplayStore.load(directory).banner()
    # The date is the manifest's — when the recording was made, not when the
    # individual call was served — so it is matched by shape, not by value.
    assert re.match(
        r"^REPLAYED — frozen model responses from \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z, "
        r"model=deepseek-v4-flash\. Not a live run\.$",
        banner,
    ), banner


def _one_call_store(tmp_path: Path) -> ReplayStore:
    """A recording holding exactly one frozen response."""
    directory = tmp_path / "rec"
    recorder = ReplayRecorder(directory=directory, testset_id="t")
    recorder.add(
        FrozenCall(
            post_id="abc",
            judge_version="v4.5.0+llm",
            model="deepseek-v4-flash",
            endpoint="http://127.0.0.1:8787/v1",
            recorded_at="2026-09-27T07:28:41Z",
            prompt_sha256="0" * 64,
            response='{"is_actionable": true, "confidence": 0.9, "reason": "x"}',
        )
    )
    recorder.close()
    return ReplayStore.load(directory)


def test_replay_is_not_labelled_as_a_mock(tmp_path: Path) -> None:
    """A replay answers with real frozen output; it must not disclaim itself.

    If a replay inherited the mock's "NOT a measurement" label, the one form of
    offline reproduction we can actually publish would talk itself down.
    """
    judge = get_judge("rule_v4+llm", store=_one_call_store(tmp_path))
    assert judge.is_replay is True
    assert judge.is_mock is False
    assert "REPLAYED" in judge.describe_backend()
    assert "deepseek-v4-flash" in judge.describe_backend()


def _fenced_blocks(text: str) -> list[str]:
    """Every ```-fenced block in a markdown document."""
    blocks: list[str] = []
    current: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if inside:
                blocks.append("\n".join(current))
                current = []
            inside = not inside
            continue
        if inside:
            current.append(line)
    return blocks


def _metric_lines(block: str) -> dict[str, str]:
    """The score-table rows of a report block, keyed by row name, right-trimmed."""
    names = (
        "predicted",
        "true positive",
        "false positive",
        "false negative",
        "precision",
        "precision 95% CI",
        "recall",
        "recall 95% CI",
        "F1",
        "noise rate",
        "unverified hits",
        "positives (n)",
    )
    rows: dict[str, str] = {}
    # Longest first: "precision 95% CI" also starts with "precision", and
    # matching the short name first would let the CI row overwrite the rate row
    # — i.e. the guard would silently not be checking the number that matters.
    ordered = sorted(names, key=len, reverse=True)
    for line in block.splitlines():
        for name in ordered:
            if line.startswith(name) and not line[len(name) : len(name) + 1].isalnum():
                rows[name] = line.rstrip()
                break
    return rows


def test_metric_rows_distinguish_a_rate_from_its_interval() -> None:
    """`precision 95% CI` must not be mistaken for the `precision` row.

    Without this the README comparison below would check the interval and never
    the rate — silently guarding the one number nobody misquotes.
    """
    rows = _metric_lines(
        "precision                      100.0%  (9/9)\n"
        "precision 95% CI                  [70%, 100%]\n"
    )
    assert rows["precision"].startswith("precision  ")
    assert rows["precision 95% CI"].startswith("precision 95% CI")


# Every published table must be backed by a committed recording. Both the
# shipping default and the v4 funnel are covered: the README's most prominent
# command is the bare `eval score`, and leaving that one un-reproducible would
# keep the loudest claim false for the table a stranger sees first.
PUBLISHED_TABLES = (
    # (recording dir, layers, min_score, a string that identifies its block)
    ("rule_v3_llm", ("rule_v3", "rule_v3+llm"), None, "rule_v3+llm (v3.1.0+llm)"),
    ("rule_v4_llm", ("rule_v4", "rule_v4+llm"), 3, "rule_v4+llm (v4.5.0+llm)"),
)


def _scorer_for(layers: tuple[str, ...], store: Any) -> EvalScorer:
    """Build a scorer for ``layers`` with the LLM stage served from ``store``.

    Deliberately passes NO tuning arguments: the candidate bar and every other
    knob stay at the CLI's defaults, so this reproduces what `eval score` does
    rather than what a test author thinks it does.
    """
    judges: list[Any] = []
    for layer in layers:
        judges.append(get_judge(layer, store=store) if layer.endswith("+llm") else get_judge(layer))
    return EvalScorer(judges)


@pytest.mark.parametrize(("directory", "layers", "min_score", "marker"), PUBLISHED_TABLES)
def test_every_published_table_matches_its_committed_replay(
    directory: str, layers: tuple[str, ...], min_score: int | None, marker: str
) -> None:
    """The table in the README is the table the replay actually prints.

    This is the guard for a defect already made once: a report block in the
    README had been generated by a run with a real endpoint configured and then
    described as the offline one, so the published number was the one a stranger
    could not reproduce. Comparing the replayed output to the published block
    line by line makes that class of drift impossible to ship quietly.
    """
    recording = REPO_ROOT / "data" / "replay" / DATASET_ID / directory
    if not recording.exists():  # pragma: no cover - recordings ship with the repo
        pytest.skip(f"no committed replay recording at {recording}")

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    matching = [
        block
        for block in _fenced_blocks(readme)
        if "REPLAYED —" in block and marker in block
    ]
    assert matching, (
        f"README has no replay block for {marker} — that table cannot be checked"
    )
    expected = _metric_lines(matching[0])
    assert expected, f"the {marker} block has no score table in it"

    dataset = EvalDataset.load(DATASET_ID, TESTSET_DIR)
    project = Watchlist.load(WATCHLIST_PATH).get("Einprag")
    store = ReplayStore.load(recording)
    actual = _metric_lines(
        _scorer_for(layers, store)
        .score(dataset, project=project, min_score=min_score, replay_banner=store.banner())
        .render()
    )

    for name, expected_line in expected.items():
        assert name in actual, f"the {marker} replay no longer prints a {name!r} row"
        assert actual[name] == expected_line, (
            f"README publishes\n  {expected_line!r}\nbut the committed replay prints\n"
            f"  {actual[name]!r}\nThe published table and the reproducible one have "
            "drifted apart. Re-record and update the README in the same commit."
        )


def test_every_replay_block_in_the_readme_is_checked() -> None:
    """No published replay block may sit outside :data:`PUBLISHED_TABLES`.

    Otherwise adding a third table is a silent way to publish an un-checkable
    number while the suite stays green.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    blocks = [b for b in _fenced_blocks(readme) if "REPLAYED —" in b]
    assert blocks, "README has no replay block at all"
    markers = [case[3] for case in PUBLISHED_TABLES]
    for block in blocks:
        assert any(marker in block for marker in markers), (
            "a replay block in the README is not covered by PUBLISHED_TABLES — "
            "it would be published without being checked"
        )


@pytest.mark.skipif(
    not COMMITTED_REPLAY.exists(), reason="no committed replay recording"
)
def test_the_committed_replay_says_which_model_froze_it() -> None:
    """The frozen responses must be attributable — and not to Nemotron."""
    manifest = json.loads(
        (COMMITTED_REPLAY / MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert manifest["model"], "the recording does not name the model"
    assert manifest["endpoint"], "the recording does not name the endpoint"
    assert manifest["calls"] > 0


def test_the_manifest_records_what_the_frozen_responses_are(tmp_path: Path) -> None:
    """Model, endpoint, judge version and field list are stated next to the data."""
    _one_call_store(tmp_path)
    manifest = json.loads((tmp_path / "rec" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["model"] == "deepseek-v4-flash"
    assert manifest["endpoint"] == "http://127.0.0.1:8787/v1"
    assert manifest["calls"] == 1
    assert "prompt_sha256" in manifest["fields"]
    assert manifest["judge_version"] == "v4.5.0+llm"
