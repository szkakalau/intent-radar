"""rule_v4 — the high-recall candidate net (P0-A).

v3 stays frozen at its published number; v4 is the layer that unblocks the
semantic layer by actually surfacing the posts a human calls actionable.
"""

from __future__ import annotations

import csv

import pytest
from conftest import REPO_ROOT, TESTSET_DIR

from intentradar.config import ProjectConfig, Watchlist
from intentradar.eval import EvalDataset
from intentradar.judge import get_judge
from intentradar.judge.rule_v4 import (
    ASK_RE,
    CANDIDATE_MIN_SCORE,
    CATEGORY_HINT_RE,
    SWITCH_RE,
    WEAK_SENTIMENT_RE,
    RuleV4Judge,
    is_noise,
    score,
)
from intentradar.models import LAYER_RULE_V3, LAYER_RULE_V4, LAYER_RULE_V4_LLM, Post

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"


@pytest.fixture
def project() -> ProjectConfig:
    """The real Einprag project config."""
    return Watchlist.load(WATCHLIST_PATH).get("Einprag")


def _post(title: str, body: str = "", pid: str = "x1") -> Post:
    """A post in r/Anki."""
    return Post(id=pid, sub="Anki", title=title, selftext=body, upvotes=2, num_comments=1)


# ── the three changes versus v3 ────────────────────────────────────────────


def test_sentiment_words_are_demoted_out_of_the_switch_signal() -> None:
    """suck / hate / frustrated / tired of generated every v3 false positive."""
    for pat in (r"suck", r"hate", r"frustrat", r"tired"):
        assert not any(pat in p for p in SWITCH_RE), f"{pat} must not be a +4 signal"
        assert any(pat in p for p in WEAK_SENTIMENT_RE), f"{pat} should be a weak signal"


def test_real_switch_signals_are_kept() -> None:
    """The phrases that genuinely mean 'I am leaving this product'."""
    joined = "\n".join(SWITCH_RE)
    for phrase in ("alternative", "switch", "instead\\s+of", "replacement", "worth",
                   "too\\s+expensive", "cancel", "migrat", "thinking\\s+about"):
        assert phrase in joined
    # "look for" is written with an optional "ing" group, so match the pattern
    # as authored rather than a flattened literal that does not exist.
    assert r"look(?:ing)?\s+for" in joined
    # ...and prove it fires on the real-world wording but only when "look for"
    # is followed by a switching object — bare "look for" is not intent.
    assert score("looking for another app", {"keywords": ["anki"], "competitors": []})[0] >= 3
    assert score("look for my keys", {"keywords": ["anki"], "competitors": []})[0] < 3


def test_v3_ask_bug_is_fixed() -> None:
    """`what (do|are) you use` could not match "what apps do you use"."""
    joined = "\n".join(ASK_RE)
    # A noun slot is now allowed between "what" and the auxiliary verb.
    assert r"what\s+(?:[\w/]+\s+){0,3}" in joined
    assert score("what apps do you use?", {"keywords": ["anki"], "competitors": []})[0] >= 3
    # Slash-joined nouns ("apps/resources") and progressive forms ("using").
    assert score("what apps/resources are you using?", {"keywords": ["anki"], "competitors": []})[0] >= 3


def test_new_ask_patterns_are_present() -> None:
    """The phrasings v3 missed, taken from the labelled misses."""
    joined = "\n".join(ASK_RE)
    for phrase in (r"does\s+any", r"can(?:not|'t)\s+find", r"suggest(?:ion)?s?\s+for",
                   r"do\s+you\s+use", r"ways\s+to", r"workbook|resource|deck"):
        assert phrase in joined


def test_support_posts_are_excluded() -> None:
    """A bug report is not a lead, even when it trips a switching phrase."""
    proj = {"keywords": ["anki"], "competitors": ["ankiapp"]}
    # v3 false positive: "taking up all my phone storage".
    assert score("Need Solution for AnkiApp taking up all my phone storage !", proj)[0] == 0
    # v3 false positive shape: TTS configuration help.
    assert score("how do i fix the anki mobile TTS, the default one sucks", proj)[0] == 0


def test_support_filter_does_not_eat_real_demand() -> None:
    """"cannot find" is a need, not a fault — it must stay a candidate."""
    proj = {"keywords": ["anki"], "competitors": []}
    total, why, _ = score("i cannot find a good deck", proj)
    assert total >= 3
    assert not any("技术支持" in w for w in why)


def test_category_gate_is_kept_but_widened() -> None:
    """No category anchor at all -> still rejected (v3's proven behaviour).

    The anchor-free sample deliberately avoids "alternative"/"recommendation":
    those words are themselves in the hint list, so a phrase built on them would
    satisfy the gate by construction instead of testing it.
    """
    assert score("switching from studying harder", {"keywords": [], "competitors": []})[0] == 0
    assert score("tired of feeling behind in class", {"keywords": [], "competitors": []})[0] == 0
    # But an intent phrase that names a purchasable thing passes.
    assert score("any workbooks i can use?", {"keywords": [], "competitors": []})[0] >= 3
    assert CATEGORY_HINT_RE

    # Known looseness, recorded rather than hidden: "alternative"/"replacement"/
    # "recommendation" are intent words that also appear in the hint list, so a
    # phrase built on one of them satisfies the gate by itself. The candidate
    # budget absorbs it today (42/211), but it is the first thing to tighten if
    # the volume ever grows.
    assert score("alternative to studying harder", {"keywords": [], "competitors": []})[0] > 0


def test_candidate_threshold_is_documented() -> None:
    """Pinned: the LLM only ever sees posts at/above this score."""
    assert CANDIDATE_MIN_SCORE == 3


# ── judge behaviour ────────────────────────────────────────────────────────


def test_score_and_analyze_agree(project: ProjectConfig) -> None:
    """The reference scorer and the evidence-emitting judge never diverge."""
    judge = RuleV4Judge()
    samples = [
        "Which flashcard app should I use instead of quizlet? storage is expensive",
        "hello world nothing here",
        "cannot find a good deck for italian",
        "anki taking up all my phone storage, how do i fix",
        "thinking about subscribing to superfluent",
        "anyone have suggestions on resources to use",
        "I hate this so much",
    ]
    for text in samples:
        total, _why, signals = score(text, project.to_mapping())
        post = _post(text[:40], text)
        judgment = judge.judge(post, project)
        assert judgment.score == total, text
        assert judgment.signals == signals, text


def test_v4_surfaces_posts_v3_missed(project: ProjectConfig) -> None:
    """The concrete regressions v4 exists to fix."""
    v3 = get_judge(LAYER_RULE_V3)
    v4 = RuleV4Judge()
    cases = [
        _post("Best Italian Anki Card Deck",
              "I want to learn the most common 1000 words in italian, but i cannot find a "
              "good deck. Does anyone have a good deck?", pid="p1"),
        _post("microbio questions",
              "if anyone knows any workbooks I can use to study these type of problems?",
              pid="p2"),
        _post("Superfluent app?", "I’m thinking about subscribing to Superfluent", pid="p3"),
    ]
    for post in cases:
        assert v3.judge(post, project).score < 5, post.id  # v3 misses it
        assert v4.judge(post, project).score >= CANDIDATE_MIN_SCORE, post.id  # v4 catches it


def test_v4_identifies_itself(project: ProjectConfig) -> None:
    """Every published number is pinned to (layer, judge_version)."""
    judge = RuleV4Judge()
    judgment = judge.judge(_post("which app should i use"), project)
    assert judgment.layer == LAYER_RULE_V4
    assert judgment.judge_version == "v4.0.0"


def test_noise_gate_matches_v3() -> None:
    """Official / megathread posts are dropped by v4 too."""
    assert is_noise("Weekly Thread: beta feedback") is True
    assert is_noise("Daily megathread") is True
    assert is_noise("Which flashcard app should I use?") is False


def test_composite_layer_is_v4_based() -> None:
    """`rule_v4+llm` must publish v4 numbers, never v3's."""
    judge = get_judge(LAYER_RULE_V4_LLM)
    assert judge.layer == LAYER_RULE_V4_LLM
    assert judge.judge_version == "v4.1.0+llm"
    assert judge.rule.layer == LAYER_RULE_V4


# ── acceptance against the labelled ground truth ───────────────────────────


def _actionable_ids(testset_id: str) -> list[str]:
    """Ids a reviewer marked actionable in this testset."""
    path = TESTSET_DIR / testset_id / "labels.csv"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [
            str(r["id"])
            for r in csv.DictReader(fh)
            if str(r.get("label") or "").strip().lower() == "actionable"
        ]


@pytest.mark.parametrize(
    "testset_id,project_name,min_recall,max_candidates",
    [("einprag-2026-09-27", "Einprag", 6, 60), ("bootstrap-2026-09-27", "IntentRadar_自举", 2, 60)],
)
def test_v4_acceptance_targets(
    testset_id: str, project_name: str, min_recall: int, max_candidates: int
) -> None:
    """v4 must surface the actionable posts within the LLM candidate budget."""
    base = TESTSET_DIR / testset_id
    if not (base / "posts.jsonl").exists():
        pytest.skip(f"frozen dataset {testset_id} not present on disk")

    dataset = EvalDataset.load(testset_id, TESTSET_DIR)
    project = Watchlist.load(WATCHLIST_PATH).get(project_name)
    actionable = set(_actionable_ids(testset_id))
    if not actionable:
        pytest.skip("no actionable labels yet")

    judge = RuleV4Judge()
    candidates = {
        p.id
        for p in dataset.in_window()
        if p.id and judge.judge(p, project).score >= CANDIDATE_MIN_SCORE
        and not judge.is_noise(p)
    }
    caught = candidates & actionable

    assert len(caught) >= min_recall, (
        f"{testset_id}: v4 recall {len(caught)}/{len(actionable)} < {min_recall}; "
        f"missed {sorted(actionable - candidates)}"
    )
    assert len(candidates) <= max_candidates, (
        f"{testset_id}: {len(candidates)} candidates exceeds the {max_candidates} LLM budget"
    )


def test_v3_number_is_untouched_by_v4() -> None:
    """v4 must not change v3's published hit set — that is the whole point."""
    base = TESTSET_DIR / "einprag-2026-09-27"
    if not (base / "posts.jsonl").exists():
        pytest.skip("frozen dataset not present on disk")
    dataset = EvalDataset.load("einprag-2026-09-27", TESTSET_DIR)
    project = Watchlist.load(WATCHLIST_PATH).get("Einprag")
    v3 = get_judge(LAYER_RULE_V3)

    hits = [
        p.id
        for p in dataset.in_window()
        if p.id and v3.judge(p, project).score >= 5 and not v3.is_noise(p)
    ]
    assert len(hits) == 7, hits  # 7 pre-dedup; dedupe by title leaves the published 6
