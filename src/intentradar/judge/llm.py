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
SYSTEM_PROMPT = """\
You are a strict classifier for a Reddit lead-generation tool.

You are given ONE post. Decide whether it expresses buying intent worth sending
to a sales team.

Answer is_actionable = true ONLY IF BOTH of these hold:
  (1) INTENT — the author expresses a recognisable need to acquire, replace, or
      be recommended a product or service: they are shopping, asking for
      recommendations, alternatives, or "what should I use", announcing they are
      switching away from something, or asking whether a paid option is worth it.
  (2) CATEGORY — that need maps to a real, purchasable product or service
      category: software, app, tool, device, subscription, or paid service.

Answer is_actionable = false for:
  * Pure complaints or venting with no replacement need ("this is so slow, ugh").
  * Bug reports, error reports, crash logs, and support questions about a
    product the author already owns and wants to keep using.
  * Academic, theoretical, conceptual, or study-method discussion where there is
    nothing to buy ("how does spaced repetition work?").
  * Career, job, internship, residency, med-school, or admissions consulting
    questions. These are extremely common in r/medicalschool: the person wants
    advice, not a product.
  * Meta discussion about Reddit itself, self-promotion of the author's own
    product, surveys, and research requests.
  * Posts where the author is offering, selling, or hiring rather than looking.

Additional rules:
  * A question mark, urgency, or exclamation marks are NOT evidence of intent.
  * Asking a community for opinions is only intent if the opinion being asked
    for is "which product should I buy/use".
  * If you are unsure, answer false. Precision matters more than recall here.

Reply with ONE JSON object and nothing else — no markdown, no prose:
{"is_actionable": <true|false>, "confidence": <0.0-1.0>, "reason": "<one short sentence; quote the decisive phrase from the post>"}
"""

USER_TEMPLATE = """\
Project being monitored: {project}
Project description: {description}
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
