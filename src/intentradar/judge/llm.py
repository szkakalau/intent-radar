"""W2 semantic layer: rule-layer candidates confirmed by an LLM.

Why this file exists
--------------------
``rule_v3`` is a regex scorer. It is cheap and fully auditable, but it counts
phrases without understanding them, so a share of its hits are not actually
buying intent. This layer asks a model the question the regexes cannot ask:
*"does this person want to acquire something?"*

Three rules keep the result reproducible, which is the whole product promise:

1. **The criteria are hard-coded here** (:data:`SYSTEM_PROMPT`), not passed in as
   free-form instructions and not read from a config file. Two people on the same
   commit ask the identical question, so the published precision number can be
   recomputed by a stranger.
2. **The layer is subtractive.** It can drop a rule hit; it can never invent one.
   Recall is therefore bounded by the rule layer's recall, and ``eval score``
   prints both layers side by side so the trade-off is visible rather than hidden.
3. **Every verdict is persisted** on the judgment (verdict / confidence / reason /
   model / error), so one lead can be audited without re-running anything.

Failure policy
--------------
If the model cannot be reached, or the reply cannot be parsed, the post is
**not** silently kept and **not** silently dropped: the verdict is recorded with
``error`` set, the score goes to 0, and :attr:`LLMJudge.llm_errors` is surfaced
by ``eval score``. A broken LLM collapses recall in a way you can see — it never
quietly relabels the rule layer's numbers as "LLM-verified".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from intentradar.config import ProjectConfig
from intentradar.errors import ConfigError
from intentradar.judge.rule_v3 import RuleV3Judge
from intentradar.models import (
    JUDGE_VERSION_LLM,
    JUDGE_VERSION_V4_LLM,
    LAYER_RULE_V3,
    LAYER_RULE_V3_LLM,
    LAYER_RULE_V4,
    LAYER_RULE_V4_LLM,
    Judgment,
    Post,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a package cycle
    from intentradar.judge import Judge

log = logging.getLogger(__name__)

# A post needs at least this much rule evidence before we spend money asking the
# model about it. 3 = one "求助选型" (ask) or one competitor mention plus a nudge.
CANDIDATE_MIN_SCORE = 3

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Which composite layer + version each rule layer maps to. The semantic layer is
# subtractive, so it inherits its identity from the net underneath it — a v4 net
# must publish v4 numbers, not v3's.
_COMPOSITE_BY_RULE_LAYER: dict[str, tuple[str, str]] = {
    LAYER_RULE_V3: (LAYER_RULE_V3_LLM, JUDGE_VERSION_LLM),
    LAYER_RULE_V4: (LAYER_RULE_V4_LLM, JUDGE_VERSION_V4_LLM),
}


# ── the criteria ───────────────────────────────────────────────────────────
# Hard-coded on purpose: this prompt IS the judgment rule. Changing it changes
# the published precision, so changes belong in a commit with a re-run of
# `eval score`, not in a runtime flag.
#
# AUTHORITATIVE SOURCE, READ IT BEFORE EDITING THIS:
#   data/testset/<id>/meta.json -> "label_judgement_standard"
# This prompt is an implementation of that text, not the other way round. An
# earlier version was written from a paraphrase in chat and added a
# "must name a specific product" constraint that the standard explicitly
# forbids ("naming the CATEGORY is enough"), which silently cut recall.
SYSTEM_PROMPT = """\
You are a strict classifier for a Reddit lead-generation tool.

You are given ONE post. Decide whether it expresses buying intent worth sending
to a sales team.

Answer is_actionable = true ONLY IF ALL THREE of these hold:

  (1) CATEGORY IS EXPLICIT — the post tells you what KIND of thing the author
      needs. A need counts as naming a category if it is any of:
        * a product, tool or app (Anki, a flashcard app, a CRM, a deck);
        * a service (a course, tutoring, consulting, a subscription);
        * a way / approach / solution / method ("a way to find customers",
          "how to reduce support tickets");
        * a capability ("being able to scrape and monitor posts").
      NAMING A SPECIFIC PRODUCT IS NEVER REQUIRED — naming the category is
      sufficient. Never reject a post merely because it asks "how" or asks for
      "a way" instead of naming a tool.
      CRITERION (1) IS JUDGED BY DEMAND DOMAIN, NOT BY SKU: the category named
      does NOT have to match the monitored product's own SKU exactly, but it
      MUST fall inside the demand domain that product can serve (given below
      per project). "A slightly different product in the same problem space"
      is IN; "a product for a completely different job" is OUT.
      Criterion (1) fails in two different ways, and BOTH are false:
        (i)  no category is named at all — the author only complains or asks
             "what should I do?";
        (ii) a category IS named but falls OUTSIDE the demand domain, so the
             monitored product cannot serve this person at all.
      The test sentence: is there ANY way this product could serve this
      person? Yes -> criterion (1) holds; No -> it does not.

  (2) THE NEED IS UNMET AND THEY ARE ACTIVELY SEEKING — the current approach
      is not working and the author is looking for a solution: complaining
      about it AND asking, or actively asking for recommendations,
      alternatives, or how to choose.

  (3) IT IS THE AUTHOR'S OWN DECISION — the author holds or shares the
      purchase decision.

Answer is_actionable = false if ANY of the three is missing. EVERY rejection
reason must map back to one of the three criteria — a reason that is not in
this prompt is a private standard and is not allowed. In particular:
  * Pure complaints or venting that ask for nothing — complaining alone is not
    seeking ("this is so slow, ugh"). [fails (2)]
  * Watching the market, researching on behalf of someone else, hunting for
    content ideas, surveys, or research requests — the author does not hold
    the decision. [fails (3)]
  * Self-promotion of the author's own product — they are PITCHING it: sharing
    a link, advertising it, asking for users or feedback on it, or hiring for
    it. Merely mentioning that they build something is NOT promotion. [fails
    (3)]
  * Posts where the author is offering, selling, or hiring rather than
    looking. [fails (2)/(3)]
  * Bug reports, error reports, crash logs, and support questions about a
    product the author already owns and wants to keep using — they are not
    seeking a replacement. [fails (2)]
  * Academic, theoretical, or conceptual discussion with nothing to acquire
    ("how does the algorithm work?"), and pure method consultation that asks
    only HOW to do something with no resource request ("how long should I
    study?"). [fails (1)/(2)]
  * Career, job, internship, residency, med-school, or admissions consulting
    questions — the person wants advice, not a product, and these are outside
    the demand domain. [fails (1)]
  * Meta discussion about Reddit itself. [fails (1)]

Additional rules, all of them part of the standard:
  (a) FREE-ONLY REQUESTS — if the author explicitly asks for something FREE
      (free sites, free tools, free resources) there is no current willingness
      to pay, so this is NOT purchase intent: answer false. Do not treat it as
      "clearly unrelated": the need may be real and in domain, only the
      willingness to pay is missing.
  (b) RESEARCH STAGE — the test is NOT "is the author talking to other
      people", it is "is the author making an adoption / purchase decision
      FOR THEMSELVES". The same wording can go either way, so decide on the
      decision, not on the phrasing:
        FALSE — asking whether an approach works at all, with no adoption
                decision of the author's own behind it ("does this method
                work?", "anyone have experiences with X?", "did it work for
                you?"). [fails (3)]
        TRUE  — asking existing users what a specific named product is like
                while the author is weighing adopting it ("have any of you
                used X — is it worth it?"). That IS the author's own
                adoption decision, and DOES satisfy criterion (3).
      Self-check before answering: "am I testing whether the author is
      deciding for themselves, or whether they are talking to someone?"
      The first is the test; the second is not.
      Still false: asking on behalf of someone else, and market research
      ("what do people want?") — the author holds no decision of their own.
  (c) BUILD-VS-BUY — a first-person build-vs-buy question ("do you use a tool
      for it, or roll your own?", "should I build this or buy it?", "is there
      something off the shelf?") DOES satisfy criterion (2): the author is
      actively seeking a solution to their own unmet need, because they are
      explicitly weighing the purchase option. The test is whether the ask
      puts BUYING on the table as one of the options the author is choosing
      between. Rules (b) and (c) ask different questions: (b) is whether the
      author is deciding FOR THEMSELVES, (c) is whether BUYING is one of the
      options on the table. Both can hold at once.
  * Do NOT reject merely because the author is a founder, builder, or has
    already started building — see rule (c). The line is whether THEY hold
    the purchase decision. Asking others what the market wants is research.
  * A question mark, urgency, or exclamation marks are NOT evidence of intent.
  * Asking a community for opinions is intent when the author is deciding
    whether to adopt something THEMSELVES — "which one should I get?" and also
    "is X worth it?" about a named product. It is NOT intent when they are
    surveying what other people want (see rule (b)).
  * If you are unsure, answer false. Precision matters more than recall here.

Reply with ONE JSON object and nothing else — no markdown, no prose:
{"is_actionable": <true|false>, "confidence": <0.0-1.0>, "reason": "<one short sentence; quote the decisive phrase from the post>"}
"""

USER_TEMPLATE = """\
Project being monitored: {project}
Project description: {description}
Demand domain this product can serve (criterion 1 is judged against THIS, not
against an exact SKU match): {demand_domain}
Product category keywords: {keywords}
Known competitors: {competitors}

Subreddit: r/{sub}

Rule-layer signals already matched: {signals}
Rule-layer evidence: {evidence}
Rule-layer score: {score}

Post title: {title}

Post body:
{body}
"""


@dataclass
class LLMVerdict:
    """One model answer, normalised. ``error`` non-empty means "we never got one"."""

    is_actionable: bool = False
    confidence: float = 0.0
    reason: str = ""
    model: str = ""
    cached: bool = False
    mock: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the audit trail."""
        return asdict(self)


_TRUE_TOKENS = {"1", "true", "yes", "y", "t", "actionable", "pos", "positive", "是"}
_FALSE_TOKENS = {"0", "false", "no", "n", "f", "not_actionable", "neg", "negative", "否"}


def _as_bool(value: Any) -> bool | None:
    """Coerce a JSON value to bool, or ``None`` when it is not interpretable."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return None


def parse_verdict(content: str) -> LLMVerdict:
    """Parse the model reply into an :class:`LLMVerdict`.

    Tolerates a fenced code block or a sentence wrapped around the JSON, and
    accepts ``is_actionable`` or ``intent`` as the key. Anything else is an
    error verdict — never a guess.
    """
    if not content or not content.strip():
        return LLMVerdict(error="empty response from the model")

    match = _JSON_OBJECT_RE.search(content)
    if match is None:
        return LLMVerdict(error=f"no JSON object in response: {content[:120]!r}")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return LLMVerdict(error=f"unparseable JSON ({exc}): {match.group(0)[:120]!r}")
    if not isinstance(payload, dict):
        return LLMVerdict(error=f"expected a JSON object, got {type(payload).__name__}")

    raw_flag = payload.get("is_actionable")
    if raw_flag is None:
        raw_flag = payload.get("intent")
    flag = _as_bool(raw_flag)
    if flag is None:
        return LLMVerdict(error=f"missing/unusable is_actionable: {raw_flag!r}")

    raw_confidence = payload.get("confidence", 0.0)
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(1.0, max(0.0, confidence))

    return LLMVerdict(
        is_actionable=flag,
        confidence=confidence,
        reason=str(payload.get("reason") or "").strip()[:300],
    )


def build_user_prompt(
    post: Post,
    project: ProjectConfig,
    base: Judgment,
    description: str = "",
) -> str:
    """Render the user turn. Deterministic: no timestamps, no random ordering."""
    evidence = "; ".join(base.why) or "(none)"
    return USER_TEMPLATE.format(
        project=project.name,
        description=description or project.site or "(not provided)",
        demand_domain=project.domain_or_keywords() or "(not provided)",
        keywords=", ".join(project.keywords) or "(none)",
        competitors=", ".join(project.competitors) or "(none)",
        sub=post.sub or "(unknown)",
        signals=", ".join(base.signals) or "(none)",
        evidence=evidence,
        score=base.score,
        title=post.title,
        body=(post.selftext or "(empty)")[:2000],
    )


@dataclass
class LLMJudge:
    """Rule-layer candidates, confirmed by an LLM.

    Subtractive by design: the rule layer proposes, the LLM disposes. The
    concrete layer/version identity is derived in ``__post_init__`` from the
    wrapped rule, so a v4 net publishes ``rule_v4+llm`` figures and never
    re-labels them as v3's.
    """

    client: Any = None
    rule: Judge | None = None
    candidate_min_score: int = CANDIDATE_MIN_SCORE
    min_confidence: float = 0.0
    description: str = ""
    llm_calls: int = 0
    llm_errors: int = 0
    cache_hits: int = 0
    _settings: Any = field(default=None, repr=False)

    layer: str = LAYER_RULE_V3_LLM
    judge_version: str = JUDGE_VERSION_LLM

    def __post_init__(self) -> None:
        """Default the rule layer, then take the composite identity from it."""
        if self.rule is None:
            self.rule = RuleV3Judge()
        rule_layer = str(getattr(self.rule, "layer", LAYER_RULE_V3) or LAYER_RULE_V3)
        composite, version = _COMPOSITE_BY_RULE_LAYER.get(
            rule_layer, (f"{rule_layer}+llm", JUDGE_VERSION_LLM)
        )
        self.layer = composite
        self.judge_version = version

    # ── collaborators ──────────────────────────────────────────────────────
    def _client(self) -> Any:
        """Return the LLM client, building one from settings on first use."""
        if self.client is None:
            from intentradar.config import Settings
            from intentradar.llm import build_client

            settings = self._settings or Settings.from_env()
            self.client = build_client(settings)
        return self.client

    @property
    def is_mock(self) -> bool:
        """Whether the LLM behind this judge is the deterministic offline mock."""
        if self.client is None:
            return True
        return bool(getattr(self.client, "is_mock", False))

    def describe_backend(self) -> str:
        """Protocol + endpoint + model, so a number can never be mislabelled.

        The W2 acceptance run used an Anthropic-shaped dev proxy serving
        ``deepseek-v4-flash``. The deliverable must quote Nebius + Nemotron
        figures — so every printed number names the model that actually
        produced it, and mock output says so in as many words.
        """
        if self.client is None:
            return "MOCK (no client configured; deterministic stand-in)"
        if self.is_mock:
            return "MOCK (deterministic stand-in; NOT a measurement)"
        config = getattr(self.client, "config", None)
        protocol = str(getattr(self.client, "backend_protocol", "") or "unknown")
        endpoint = str(getattr(config, "base_url", "") or "")
        model = str(getattr(config, "model", "") or "")
        return f"{protocol} @ {endpoint} · model={model or '(unset)'}"

    # ── judgment ──────────────────────────────────────────────────────────
    def judge(self, post: Post, project: ProjectConfig) -> Judgment:
        """Score with the wrapped rule layer, then confirm the candidate."""
        base = self.rule.judge(post, project)

        # Below the candidate bar, or an official/megathread post: no LLM spend.
        if base.score < self.candidate_min_score or self.rule.is_noise(post):
            return self._compose(base, None)

        verdict = self._ask(post, project, base)
        confirmed = verdict.is_actionable and not verdict.error
        if confirmed and verdict.confidence < self.min_confidence:
            confirmed = False
        return self._compose(base, verdict, keep_score=confirmed)

    def is_noise(self, post: Post) -> bool:
        """Same noise gate as the rule layer (official / megathread posts)."""
        return self.rule.is_noise(post)

    # ── internals ─────────────────────────────────────────────────────────
    def _ask(self, post: Post, project: ProjectConfig, base: Judgment) -> LLMVerdict:
        """One LLM call, with every failure mode turned into a recorded verdict."""
        from intentradar.errors import IntentRadarError

        client = self._client()
        try:
            response = client.complete(
                system=SYSTEM_PROMPT,
                user=build_user_prompt(post, project, base, self.description),
                tier="everyday",
            )
        except IntentRadarError as exc:
            self.llm_errors += 1
            log.warning("LLM judgment failed for post %s: %s", post.id, exc)
            return LLMVerdict(error=f"{type(exc).__name__}: {exc.message}")

        if getattr(response, "cached", False):
            self.cache_hits += 1
        else:
            self.llm_calls += 1

        verdict = parse_verdict(str(getattr(response, "content", "") or ""))
        verdict.model = str(getattr(response, "model", "") or "")
        verdict.cached = bool(getattr(response, "cached", False))
        verdict.mock = bool(getattr(response, "mock", False))
        if verdict.error:
            self.llm_errors += 1
            log.warning("LLM judgment unusable for post %s: %s", post.id, verdict.error)
        return verdict

    def _compose(
        self, base: Judgment, verdict: LLMVerdict | None, keep_score: bool = True
    ) -> Judgment:
        """Wrap the rule judgment with the LLM verdict attached.

        A rejected (or unanswerable) post keeps its evidence — so the audit trail
        survives — but scores 0 and therefore leaves the hit set.
        """
        score = base.score if keep_score else 0
        return Judgment(
            score=score,
            signals=list(base.signals),
            evidence=list(base.evidence),
            why=list(base.why),
            layer=self.layer,
            judge_version=self.judge_version,
            llm_verdict=None if verdict is None else verdict.is_actionable,
            llm_confidence=None if verdict is None else verdict.confidence,
            llm_reason="" if verdict is None else verdict.reason,
            llm_model="" if verdict is None else verdict.model,
            llm_error="" if verdict is None else verdict.error,
        )


def build_llm_judge(client: Any = None, **kwargs: Any) -> LLMJudge:
    """Convenience constructor used by :func:`intentradar.judge.get_judge`."""
    return LLMJudge(client=client, **kwargs)


def require_llm_available(judge: LLMJudge) -> None:
    """Raise a friendly :class:`ConfigError` when the semantic layer cannot run.

    Kept separate from the constructor so that building a judge offline (tests,
    ``config check``) never needs a key.
    """
    if judge.client is None and not judge.is_mock:
        raise ConfigError(
            "the rule_v3+llm layer needs an LLM endpoint — set "
            "INTENTRADAR_LLM_API_KEY (or NEBIUS_API_KEY) plus "
            "INTENTRADAR_LLM_MODEL (or NEBIUS_MODEL_EVERYDAY) in .env"
        )
