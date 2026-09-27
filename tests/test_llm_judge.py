"""W2 semantic layer: the LLM judge, its prompt, and its failure policy."""

from __future__ import annotations

import json

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
        "INTENT",
        "CATEGORY",
        "Pure complaints or venting with no replacement need",
        "Bug reports",
        "Academic, theoretical, conceptual",
        "residency, med-school, or admissions consulting",
        "If you are unsure, answer false",
        "is_actionable",
        "confidence",
        "reason",
    ):
        assert phrase in SYSTEM_PROMPT


def test_prompt_forbids_rewarding_style_over_substance() -> None:
    """A question mark must not be treated as intent (v3 parity)."""
    assert "question mark" in SYSTEM_PROMPT.lower()


def test_prompt_carries_the_adjudicated_bootstrap_ruling() -> None:
    """The reviewer's ruling is in the rule itself, not just in a changelog.

    "Creative ways to find customers" names no product, so it is not actionable
    even though the pain is real; a first-person buy-vs-build question is, even
    when the asker is a founder. Both halves have to be in the prompt or the
    measured numbers cannot be reproduced by anyone reading it.
    """
    for phrase in (
        "QUOTABLE sentence",
        "undecided between",
        "buying it and building it themselves",
        "ways, strategies, methods, tips",
        "name NO",
        "Do NOT reject merely because the author is a founder, builder",
        "exact category being monitored",
    ):
        assert phrase in SYSTEM_PROMPT, phrase


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
