"""W2 semantic layer: the LLM judge, its prompt, and its failure policy."""

from __future__ import annotations

import csv
import json
import re

import pytest
from conftest import REPO_ROOT

from intentradar.config import ProjectConfig, Watchlist
from intentradar.errors import ProviderError
from intentradar.judge import get_judge
from intentradar.judge.llm import (
    CANDIDATE_MIN_SCORE,
    SYSTEM_PROMPT,
    LLMJudge,
    LLMVerdict,
    build_user_prompt,
    parse_verdict,
)
from intentradar.models import LAYER_RULE_V3, LAYER_RULE_V3_LLM, Post

WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"


class _FakeClient:
    """Returns a canned JSON reply; records what it was asked."""

    def __init__(self, replies: list[str] | None = None, raises: Exception | None = None) -> None:
        """Initialise with queued replies or an exception to raise."""
        self.replies = list(replies or [])
        self.raises = raises
        self.calls: list[tuple[str, str]] = []
        self.is_mock = False

    def complete(self, system: str, user: str, tier: str = "everyday") -> object:
        """Return the next canned reply, or raise."""
        self.calls.append((system, user))
        if self.raises is not None:
            raise self.raises
        content = self.replies.pop(0) if self.replies else '{"is_actionable": false}'
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


def _verdict(flag: bool, confidence: float = 0.9, reason: str = "because") -> str:
    """A well-formed model reply."""
    return json.dumps({"is_actionable": flag, "confidence": confidence, "reason": reason})


def _flat(text: str) -> str:
    """Lower-cased with whitespace collapsed.

    The prompt is hard-wrapped at ~78 columns, so a phrase can straddle a
    newline. Assertions must not fail (or pass) because of a re-wrap.
    """
    return " ".join(text.lower().split())


@pytest.fixture
def project() -> ProjectConfig:
    """The real Einprag project config (so scores are realistic)."""
    return Watchlist.load(WATCHLIST_PATH).get("Einprag")


def _post(title: str, body: str = "", pid: str = "p1") -> Post:
    """A post in r/Anki."""
    return Post(id=pid, sub="Anki", title=title, selftext=body, upvotes=2, num_comments=1)


# ── prompt ─────────────────────────────────────────────────────────────────


def test_criteria_are_hard_coded_in_the_prompt() -> None:
    """The judgment rule lives in the prompt, so anyone can read and recompute it."""
    for phrase in (
        "CATEGORY IS EXPLICIT",
        "THE NEED IS UNMET",
        "IT IS THE AUTHOR'S OWN DECISION",
        "Pure complaints or venting",
        "Bug reports",
        "Academic, theoretical, or conceptual",
        "pure method consultation",
        "residency, med-school, or admissions consulting",
        "If you are unsure, answer false",
        "is_actionable",
        "confidence",
        "reason",
    ):
        assert phrase in SYSTEM_PROMPT, phrase


def test_prompt_forbids_rewarding_style_over_substance() -> None:
    """A question mark must not be treated as intent (v3 parity)."""
    assert "question mark" in SYSTEM_PROMPT.lower()


def test_prompt_implements_the_authoritative_standard() -> None:
    """The prompt must agree with `label_judgement_standard` in meta.json.

    That field is the source of truth; this prompt is only an implementation of
    it. An earlier version was written from a paraphrase and added a "must name
    a specific product" rule the standard explicitly forbids, which silently cut
    recall — so both directions are pinned: the required wording must be there,
    and the forbidden wording must not come back.
    """
    lowered = SYSTEM_PROMPT.lower()
    for phrase in (
        "category is explicit",
        "a way / approach / solution / method",
        "a capability",
        "naming a specific product is never required",
        "actively seeking",
        "it is the author's own decision",
    ):
        assert phrase in lowered, phrase

    # The bug: rejecting a post for naming no product, or for asking "how".
    assert "that name NO" not in SYSTEM_PROMPT
    assert "names NO" not in SYSTEM_PROMPT


def test_prompt_implements_the_demand_domain_rule() -> None:
    """Criterion (1) is judged by DEMAND DOMAIN, not by an exact SKU match.

    The labeler added this to `label_judgement_standard`; a prompt that still
    required a SKU match would reproduce the v4.2.0 recall bug in a new form.
    """
    lowered = _flat(SYSTEM_PROMPT)
    for phrase in (
        "demand domain",
        "not by sku",
        "does not have to match the monitored product's own sku",
        "is there any way this product could serve this person",
    ):
        assert phrase in lowered, phrase


def test_prompt_implements_rules_a_b_and_c() -> None:
    """Rules (a) free-only, (b) research stage, (c) build-vs-buy.

    (c) is the buy-vs-build rule, and it is here because it is in the standard
    — not because it was requested in a chat message. If the standard ever drops
    it, this test is the thing that should fail first.
    """
    lowered = SYSTEM_PROMPT.lower()
    # (a) explicit ask for something free -> not purchase intent
    assert "free-only requests" in lowered
    assert "willingness to pay" in lowered
    # (b) research stage does not satisfy criterion (3)
    assert "research stage" in lowered
    assert "anyone have experiences" in lowered
    # (c) build-vs-buy DOES satisfy criterion (2)
    assert "build-vs-buy" in lowered
    assert "satisfy criterion (2)" in lowered
    assert "roll your own" in lowered
    assert "off the shelf" in lowered
    # (c) is explicitly distinguished from (b), or the two collide.
    assert "rules (b) and (c) ask different questions" in lowered
    # The three rules are labelled (a)/(b)/(c) so a reader can map them back.
    for marker in ("(a)", "(b)", "(c)"):
        assert marker in SYSTEM_PROMPT, marker


def test_rule_b_tests_the_decision_not_the_conversation() -> None:
    """Rule (b)'s test is "deciding for themselves", not "talking to people".

    v4.4.0's wording made the model reject 1wpsql7 — someone weighing a paid
    subscription who asks existing users "is it worth it?" — as "research
    stage". That reading is wrong on the standard's own terms: the author is
    making an adoption decision for themselves. Team-lead ruled: replace the
    test sentence, do not bolt on a carve-out. Both directions are pinned.
    """
    lowered = _flat(SYSTEM_PROMPT)
    # The new test sentence, stated as what it is AND what it is not.
    assert "making an adoption / purchase decision for themselves" in lowered
    assert 'the test is not "is the author talking to other people"' in lowered
    assert "am i testing whether the author is deciding for themselves" in lowered
    assert "the first is the test; the second is not" in lowered

    # Asking existing users about a NAMED product is how you decide to adopt it.
    assert "is it worth it?" in lowered
    assert "does satisfy criterion (3)" in lowered

    # The two things that must STILL be false.
    assert "asking on behalf of someone else" in lowered
    assert "market research" in lowered

    # The standard's canonical FALSE example is still there — but labelled as
    # false because of the missing decision, not because of the phrasing.
    assert "anyone have experiences with x?" in lowered
    assert "no adoption" in lowered and "decision of the author's own" in lowered
    # The old rule ("asking others about experience -> false, full stop").
    assert '"anyone have experiences" = no' not in lowered


def test_community_opinion_bullet_agrees_with_rule_b() -> None:
    """The older bullet used to be the other half of the 1wpsql7 bug.

    "Asking a community for opinions is only intent if the opinion is 'which
    thing should I get/use'" excludes "is X worth it?", which rule (b) now
    accepts. Two bullets in one prompt must not say opposite things.
    """
    lowered = _flat(SYSTEM_PROMPT)
    assert "is x worth it?" in lowered
    assert "deciding whether to adopt something themselves" in lowered
    # The old, narrower rule is gone.
    assert "is only intent if the opinion being asked" not in lowered


def test_prompt_forbids_private_exclusion_reasons() -> None:
    """Every rejection must map to one of the three criteria.

    The standard ends with: "a reason that is not in the standard is a private
    standard and is not allowed". The prompt has to say so, otherwise the model
    supplies its own.
    """
    lowered = _flat(SYSTEM_PROMPT)
    assert "private standard" in lowered
    assert "must map back to one of the three criteria" in lowered


def test_prompt_matches_the_committed_standard_text() -> None:
    """Cross-check against meta.json so the two cannot drift silently."""
    meta_path = REPO_ROOT / "data" / "testset" / "einprag-2026-09-27" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    standard = str(meta.get("label_judgement_standard", "")).lower()
    assert "specific product name is not required" in standard
    # The standard's three conditions must all appear in the prompt.
    assert "category" in SYSTEM_PROMPT.lower()
    assert "seeking" in SYSTEM_PROMPT.lower()
    assert "decision" in SYSTEM_PROMPT.lower()

    # Every extra rule the standard defines must be honoured by the prompt.
    for phrase in ("demand domain", "build-vs-buy", "willingness to pay"):
        assert phrase in standard, phrase
        assert phrase in SYSTEM_PROMPT.lower(), phrase


_POST_ID_RE = re.compile(r"\b1[a-z0-9]{6}\b")


def _standard_ids(standard: str, start: str, tail: str) -> list[str]:
    """Every post id the standard cites between ``start`` and ``tail``.

    The standard is prose, so the ids are scraped rather than parsed: it quotes
    some in parentheses and some inline. What matters is that the sentence is
    still there and still names the same rows.
    """
    match = re.search(re.escape(start) + r".*?" + re.escape(tail), standard, re.IGNORECASE)
    assert match is not None, f"standard lost the sentence {start!r} ... {tail!r}"
    ids = _POST_ID_RE.findall(match.group(0))
    assert ids, f"no post ids found in {start!r} ... {tail!r}"
    return ids


def test_standard_precedents_agree_with_the_ground_truth() -> None:
    """The ids the standard cites as settled must still be labelled that way.

    The standard is prose that quotes example ids; labels.csv is the truth.
    They drift apart the moment someone relabels a row and forgets the
    precedent — and a precedent that contradicts its own ground truth is worse
    than no precedent, because the judge prompt is synced from it.
    """
    meta_path = REPO_ROOT / "data" / "testset" / "einprag-2026-09-27" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    standard = str(meta.get("label_judgement_standard", ""))
    if "PRECEDENT 1" not in standard:
        pytest.skip("standard has no precedents yet")

    labels_path = REPO_ROOT / "data" / "testset" / "einprag-2026-09-27" / "labels.csv"
    truth: dict[str, str] = {}
    with labels_path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            truth[str(row.get("id") or "").strip()] = str(row.get("label") or "").strip()

    actionable_ids = _standard_ids(
        standard, "every confirmed actionable", "names something acquirable"
    )
    assert len(actionable_ids) >= 8  # a truncated list would silently weaken this
    for pid in actionable_ids:
        assert truth.get(pid) == "actionable", (
            f"standard cites {pid} as a confirmed actionable but labels.csv says "
            f"{truth.get(pid)!r} — the precedent and the ground truth disagree"
        )

    family_ids = _standard_ids(standard, "1wq876x", "are ONE family")
    for pid in family_ids:
        assert truth.get(pid) == "borderline", (
            f"standard cites {pid} as one method-request family but labels.csv says "
            f"{truth.get(pid)!r} — promoting one alone is the inconsistency we reject"
        )


def test_demand_domain_reaches_the_model(project: ProjectConfig) -> None:
    """The per-project demand domain is sent, so criterion (1) has something
    to be judged against."""
    post = _post("Which flashcard app should I use?", "quizlet is expensive")
    base = get_judge(LAYER_RULE_V3).judge(post, project)
    prompt = build_user_prompt(post, project, base, "a spaced-repetition app")

    assert "exam prep / studying / memorization" in prompt
    assert project.demand_domain  # configured, not silently empty
    assert "Demand domain" in prompt


def test_demand_domain_falls_back_to_keywords() -> None:
    """A project predating the field still sends a usable domain."""
    legacy = ProjectConfig(name="Legacy", keywords=["widget", "gadget"])
    assert legacy.demand_domain == ""
    assert legacy.domain_or_keywords() == "widget, gadget"


def test_user_prompt_is_deterministic(project: ProjectConfig) -> None:
    """Same post → same prompt, twice. No timestamps, no set iteration."""
    post = _post("Which flashcard app should I use?", "quizlet is expensive")
    base = get_judge(LAYER_RULE_V3).judge(post, project)
    first = build_user_prompt(post, project, base, "a spaced-repetition app")
    second = build_user_prompt(post, project, base, "a spaced-repetition app")
    assert first == second
    assert "Which flashcard app should I use?" in first
    assert "flashcard" in first


# ── parsing ────────────────────────────────────────────────────────────────


def test_parse_verdict_accepts_the_documented_shape() -> None:
    """The happy path."""
    verdict = parse_verdict('{"is_actionable": true, "confidence": 0.8, "reason": "shopping"}')
    assert verdict.is_actionable is True
    assert verdict.confidence == pytest.approx(0.8)
    assert verdict.reason == "shopping"
    assert verdict.error == ""


def test_parse_verdict_tolerates_wrapping_and_alternate_keys() -> None:
    """A fenced block, prose around the JSON, and ``intent`` instead of the key."""
    verdict = parse_verdict(
        'Sure! ```json\n{"intent": true, "confidence": 0.4, "reason": "asks for alternatives"}\n```'
    )
    assert verdict.is_actionable is True
    assert verdict.confidence == pytest.approx(0.4)


def test_parse_verdict_clamps_confidence() -> None:
    """Out-of-range confidence is clamped, never propagated."""
    assert parse_verdict('{"is_actionable": true, "confidence": 5}').confidence == 1.0
    assert parse_verdict('{"is_actionable": true, "confidence": -2}').confidence == 0.0
    assert parse_verdict('{"is_actionable": true}').confidence == 0.0


@pytest.mark.parametrize(
    "content",
    ["", "I cannot help with that", "{not json", '{"is_actionable": "maybe"}', "[]"],
)
def test_unusable_replies_become_error_verdicts(content: str) -> None:
    """Never guess: an unreadable reply is an error, not a silent 'no'."""
    verdict = parse_verdict(content)
    assert verdict.error
    assert verdict.is_actionable is False


# ── the judge ──────────────────────────────────────────────────────────────


def test_confirmed_candidate_keeps_its_score(project: ProjectConfig) -> None:
    """LLM says actionable → the rule score survives and the verdict is recorded."""
    client = _FakeClient([_verdict(True, 0.95, "asking which app to buy")])
    judge = LLMJudge(client=client)
    post = _post("Which flashcard app should I use instead of quizlet?", "storage is expensive")

    judgment = judge.judge(post, project)

    assert judgment.layer == LAYER_RULE_V3_LLM
    assert judgment.judge_version.endswith("llm")
    assert judgment.score > 0
    assert judgment.llm_verdict is True
    assert judgment.llm_confidence == pytest.approx(0.95)
    assert judgment.llm_reason == "asking which app to buy"
    assert judgment.llm_model == "fake/model"
    assert judgment.llm_error == ""
    assert len(client.calls) == 1
    # The criteria prompt is what was actually sent.
    assert client.calls[0][0] == SYSTEM_PROMPT


def test_rejected_candidate_is_scored_zero(project: ProjectConfig) -> None:
    """LLM says not actionable → the post leaves the hit set, evidence kept."""
    client = _FakeClient([_verdict(False, 0.99, "venting, no replacement need")])
    judge = LLMJudge(client=client)
    post = _post("Which flashcard app should I use instead of quizlet?", "storage is expensive")

    judgment = judge.judge(post, project)

    assert judgment.score == 0
    assert judgment.llm_verdict is False
    assert judgment.llm_reason == "venting, no replacement need"
    assert judgment.evidence  # the audit trail survives the rejection


def test_weak_posts_never_reach_the_llm(project: ProjectConfig) -> None:
    """Below the candidate bar there is no LLM spend at all."""
    client = _FakeClient([_verdict(True)])
    judge = LLMJudge(client=client)
    post = _post("hello world", "nothing relevant")

    judgment = judge.judge(post, project)

    assert judgment.score == 0
    assert client.calls == []
    assert judgment.llm_verdict is None  # "not asked", not "said no"


def test_candidate_threshold_is_the_documented_constant() -> None:
    """CANDIDATE_MIN_SCORE is part of the published method; pin it."""
    assert CANDIDATE_MIN_SCORE == 3
    assert LLMJudge(client=_FakeClient()).candidate_min_score == 3


def test_noise_posts_never_reach_the_llm(project: ProjectConfig) -> None:
    """Official / megathread posts are dropped before any spend."""
    client = _FakeClient([_verdict(True)])
    judge = LLMJudge(client=client)
    post = _post("Weekly Thread: beta feedback", "flashcard storage cost")

    judgment = judge.judge(post, project)
    assert client.calls == []
    assert judgment.llm_verdict is None


def test_llm_failure_is_recorded_not_swallowed(project: ProjectConfig) -> None:
    """A transport failure scores 0 and is counted — recall collapse stays visible."""
    client = _FakeClient(raises=ProviderError("fake", "HTTP 500"))
    judge = LLMJudge(client=client)
    post = _post("Which flashcard app should I use instead of quizlet?", "storage is expensive")

    judgment = judge.judge(post, project)

    assert judgment.score == 0
    assert judgment.llm_verdict is False
    assert "ProviderError" in judgment.llm_error
    assert judge.llm_errors == 1
    assert judge.llm_calls == 0


def test_unparseable_reply_counts_as_an_error(project: ProjectConfig) -> None:
    """An unusable reply is not a judgement; it is an error."""
    client = _FakeClient(["maybe?"])
    judge = LLMJudge(client=client)
    post = _post("Which flashcard app should I use instead of quizlet?", "storage is expensive")

    judgment = judge.judge(post, project)
    assert judgment.score == 0
    assert judgment.llm_error
    assert judge.llm_errors == 1


def test_min_confidence_can_reject_a_hedged_answer(project: ProjectConfig) -> None:
    """A confident-enough bar is configurable and enforced."""
    client = _FakeClient([_verdict(True, 0.4, "weak signal")])
    judge = LLMJudge(client=client, min_confidence=0.6)
    post = _post("Which flashcard app should I use instead of quizlet?", "storage is expensive")

    assert judge.judge(post, project).score == 0


def test_cache_hits_do_not_count_as_calls(project: ProjectConfig) -> None:
    """A cached verdict costs nothing and must not be reported as a call."""
    class _CachedClient(_FakeClient):
        is_mock = False

        def complete(self, system: str, user: str, tier: str = "everyday") -> object:
            self.calls.append((system, user))
            return type(
                "R", (), {"content": _verdict(True), "model": "fake/model", "cached": True,
                          "mock": False}
            )()

    judge = LLMJudge(client=_CachedClient())
    post = _post("Which flashcard app should I use instead of quizlet?", "storage is expensive")
    judge.judge(post, project)
    assert judge.llm_calls == 0
    assert judge.cache_hits == 1


def test_get_judge_registers_the_semantic_layer() -> None:
    """``get_judge("rule_v3+llm")`` works and stays a Judge."""
    judge = get_judge(LAYER_RULE_V3_LLM, client=_FakeClient())
    assert judge.layer == LAYER_RULE_V3_LLM
    assert judge.is_noise(_post("Weekly Thread: beta feedback")) is True


def test_unknown_layer_is_still_a_config_error() -> None:
    """A typo in the layer name fails loudly."""
    from intentradar.errors import ConfigError

    with pytest.raises(ConfigError) as excinfo:
        get_judge("gpt-5")
    assert "rule_v3" in excinfo.value.message


def test_is_mock_is_true_without_a_real_client() -> None:
    """No client configured → the judge knows it cannot produce real numbers."""
    judge = LLMJudge()
    assert judge.is_mock is True
    assert LLMJudge(client=_FakeClient()).is_mock is False


def test_verdict_round_trips_to_dict() -> None:
    """The verdict is serialisable so it can be audited later."""
    payload = LLMVerdict(True, 0.5, "r", "m", False, True, "").to_dict()
    assert payload["is_actionable"] is True
    assert payload["mock"] is True
