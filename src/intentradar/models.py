"""Data contract (PRD §3.2).

This module is the single source of truth for what a "lead" looks like. Every
number we publish is explained by a pair of `(judge_version, testset_id)` — so
those two fields travel with every judgment.

Design principle: **anyone looking at one lead can decide whether the judgment
is right without reading the code.**
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# Bump JUDGE_VERSION whenever scoring behaviour changes; otherwise the published
# accuracy number cannot be interpreted.
JUDGE_VERSION = "v3.0.0"
LAYER_RULE_V3 = "rule_v3"  # regex only — the frozen baselines
# W2: rule-layer candidates confirmed by an LLM. Subtractive by design — see
# intentradar/judge/llm.py — so its recall can never exceed the rule layer's.
LAYER_RULE_V3_LLM = "rule_v3+llm"
JUDGE_VERSION_LLM = "v3.1.0+llm"

# W2b: v3 stays frozen at its published number; v4 is a separate, selectable
# layer with its own version — "wide in, strict out" (see judge/rule_v4.py).
LAYER_RULE_V4 = "rule_v4"
JUDGE_VERSION_V4 = "v4.0.0"
LAYER_RULE_V4_LLM = "rule_v4+llm"
# v4.2.0+llm: re-cut criteria. BUG — added a "must name a specific product"
# constraint that meta.json's `label_judgement_standard` explicitly forbids
# ("naming the CATEGORY is enough"), and rejected "ways/methods" outright.
# Recalled; do not quote its numbers as anything but the bug's cost.
# v4.3.0+llm: implements the authoritative standard — three conditions
# (category explicit / unmet and actively seeking / author's own decision) with
# CATEGORY enumerated as product, service, way-or-solution, or capability.
# v4.4.0+llm: syncs the rest of the standard that the labeler has since written
# into meta.json — criterion (1) judged by DEMAND DOMAIN rather than SKU (with
# per-project domain text), the two ways (1) can fail, and rules (a) free-only
# -> false, (b) research stage -> false, (c) first-person build-vs-buy
# SATISFIES criterion (2). Also states that every rejection reason must map to
# one of the three criteria, so no private standard can creep back in.
# v4.5.0+llm: rule (b) REWRITTEN — its test sentence is now "is the author
# making an adoption / purchase decision FOR THEMSELVES", not "is the author
# talking to other people". v4.4.0's wording made the model reject 1wpsql7
# (someone weighing a paid subscription, asking existing users "is it worth
# it?") as "research stage", which contradicted both the label and the
# standard's own precedent list. Self-check line added, and the "asking a
# community for opinions" bullet brought in line with it.
# The prompt IS the rule, so the version moves with it: the same version string
# must never mean two different criteria.
JUDGE_VERSION_V4_LLM = "v4.5.0+llm"


class Signal(StrEnum):
    """Enumeration of intent signals (PRD §3.2)."""

    SWITCH = "切换意图"
    ASK = "求助选型"
    COMPETITOR = "竞品提及"
    PAIN = "痛点吐槽"


_HTML_RE = re.compile(r"<[^>]+>")
_HTML_ENTITIES = (
    ("&amp;", "&"),
    ("&quot;", '"'),
    ("&#39;", "'"),
    ("&#x27;", "'"),
    ("&gt;", ">"),
    ("&lt;", "<"),
)


def clean(s: str | None) -> str:
    """Strip HTML and collapse whitespace.

    Verbatim port from ``scripts/monitor.py::clean`` — changing it would change
    the text the regexes see, and therefore change the hit set.
    """
    if not s:
        return ""
    s = _HTML_RE.sub(" ", s)
    for a, b in _HTML_ENTITIES:
        s = s.replace(a, b)
    return " ".join(s.split())


def now_iso_local() -> str:
    """Current local time as ISO-8601 with offset, e.g. ``2026-09-27T01:40:00+08:00``."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Evidence:
    """Machine-readable proof for one scoring rule.

    Only non-``None`` fields are serialised, keeping the contract compact.
    """

    type: str  # "switch" | "ask" | "competitor" | "keyword" | "pain" | "gate"
    pattern: str | None = None  # the regex that matched (switch / ask)
    matched: str | None = None  # the actual phrase in the post (switch / ask)
    value: str | None = None  # the matched term (competitor / keyword / pain / gate)
    cooccur: str | None = None  # the category word it co-occurred with (pain only)

    def to_dict(self) -> dict[str, Any]:
        """Serialise, dropping unset fields."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Evidence:
        """Deserialise from a dict produced by :meth:`to_dict`."""
        return cls(
            type=str(raw.get("type", "")),
            pattern=raw.get("pattern"),
            matched=raw.get("matched"),
            value=raw.get("value"),
            cooccur=raw.get("cooccur"),
        )


# Mapping from evidence type -> why-string template. Keeps `why` byte-identical
# to the pre-migration v3 output while `evidence` stays the authoritative record.
_WHY_TEMPLATES: dict[str, str] = {
    "switch": "切换:{matched}",
    "ask": "求助:{matched}",
    "competitor": "竞品:{value}",
    "keyword": "品类:{value}",
    "pain": "痛点:{value}",
    # v4 additions. Absent from v3 evidence, so v3 output is unaffected.
    "weak_sentiment": "情绪:{matched}",
    "category_hint": "品类提示:{matched}",
    "support": "技术支持:{matched}",
}


def derive_why(evidence: list[Evidence], signals: list[str]) -> list[str]:
    """Derive the human-readable ``why`` list from evidence (PRD §3.2 table).

    Rules:
      * ``switch`` / ``ask`` -> ``f"{label}:{matched[:24]}"``
      * ``competitor`` / ``keyword`` / ``pain`` -> ``f"{label}:{value}"``
      * ``gate`` -> the whole list collapses to ``无品类关联(<signals>)``
      * the result is truncated to 4 entries (v3 behaviour: ``why[:4]``)
    """
    for e in evidence:
        if e.type == "gate":
            return [f"无品类关联({'/'.join(signals) or '弱信号'})"]

    out: list[str] = []
    for e in evidence:
        template = _WHY_TEMPLATES.get(e.type)
        if template is None:
            continue
        matched = e.matched or ""
        out.append(template.format(matched=matched[:24], value=e.value))
    return out[:4]


@dataclass
class Post:
    """A single Reddit post, normalised."""

    id: str
    sub: str
    title: str
    selftext: str = ""
    created_utc: int = 0
    upvotes: int | None = None
    num_comments: int | None = None

    @property
    def permalink(self) -> str:
        """Reddit permalink. The provider does not return it, so we build it."""
        return f"https://reddit.com/r/{self.sub}/comments/{self.id}/"

    @property
    def text(self) -> str:
        """Title + body, cleaned — exactly what the scorer sees."""
        return clean(f"{self.title} {self.selftext}")

    @property
    def created(self) -> str:
        """Local-time display string, e.g. ``2026-09-25 00:36``. Not UTC (v3 parity)."""
        if not self.created_utc:
            return "?"
        return datetime.fromtimestamp(self.created_utc).strftime("%Y-%m-%d %H:%M")

    @classmethod
    def from_raw(cls, raw: dict[str, Any], sub: str = "") -> Post:
        """Build a Post from a raw provider payload.

        Accepts both provider field names and the normalised ones used in
        ``data/testset/*/posts.jsonl``.
        """
        subreddit = str(raw.get("subreddit") or raw.get("sub") or sub or "")
        upvotes = raw.get("score")
        if upvotes is None:
            upvotes = raw.get("upvotes")
        comments = raw.get("num_comments")
        if comments is None:
            comments = raw.get("comments")
        return cls(
            id=str(raw.get("id") or ""),
            sub=subreddit,
            title=str(raw.get("title") or ""),
            selftext=str(raw.get("selftext") or ""),
            created_utc=int(raw.get("created_utc") or 0),
            upvotes=None if upvotes is None else int(upvotes),
            num_comments=None if comments is None else int(comments),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the flat contract (no author field — privacy)."""
        return {
            "id": self.id,
            "sub": self.sub,
            "title": self.title,
            "selftext": self.selftext,
            "created_utc": self.created_utc,
            "upvotes": self.upvotes,
            "num_comments": self.num_comments,
            "permalink": self.permalink,
            "created": self.created,
        }


@dataclass
class Judgment:
    """The verdict for one post, with everything needed to audit it."""

    score: int
    signals: list[str]  # sorted(set) — Unicode code-point order
    evidence: list[Evidence]  # authoritative, up to 5 entries
    why: list[str]  # derived from evidence, truncated to 4 (v3 compatibility)
    layer: str = LAYER_RULE_V3
    judge_version: str = JUDGE_VERSION

    # ── W2 semantic layer ──────────────────────────────────────────────────
    # All default to "the LLM was not asked", so every record the rule layer
    # produces stays byte-identical to the frozen baselines.
    llm_verdict: bool | None = None  # True = actionable, None = not asked
    llm_confidence: float | None = None
    llm_reason: str = ""
    llm_model: str = ""
    llm_error: str = ""  # non-empty => the verdict is a fallback, not a judgment

    def to_dict(self) -> dict[str, Any]:
        """Serialise the judgment part of the contract."""
        return {
            "score": self.score,
            "signals": self.signals,
            "evidence": [e.to_dict() for e in self.evidence],
            "why": self.why,
            "layer": self.layer,
            "judge_version": self.judge_version,
            "llm_verdict": self.llm_verdict,
            "llm_confidence": self.llm_confidence,
            "llm_reason": self.llm_reason,
            "llm_model": self.llm_model,
            "llm_error": self.llm_error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Judgment:
        """Deserialise a judgment previously written by :meth:`to_dict`."""
        evidence = [Evidence.from_dict(e) for e in raw.get("evidence", []) or []]
        signals = list(raw.get("signals", []) or [])
        confidence = raw.get("llm_confidence")
        return cls(
            score=int(raw.get("score", 0)),
            signals=signals,
            evidence=evidence,
            why=list(raw.get("why", []) or []),
            layer=str(raw.get("layer", LAYER_RULE_V3)),
            judge_version=str(raw.get("judge_version", JUDGE_VERSION)),
            llm_verdict=raw.get("llm_verdict"),
            llm_confidence=None if confidence is None else float(confidence),
            llm_reason=str(raw.get("llm_reason") or ""),
            llm_model=str(raw.get("llm_model") or ""),
            llm_error=str(raw.get("llm_error") or ""),
        )


@dataclass
class Lead:
    """A post plus its judgment. The unit that flows through the pipeline."""

    post: Post
    judgment: Judgment
    judged_at: str = field(default_factory=now_iso_local)

    def to_dict(self) -> dict[str, Any]:
        """Flat PRD §3.2 contract — the only format written to reports and exports."""
        j = self.judgment
        return {
            "id": self.post.id,
            "sub": self.post.sub,
            "permalink": self.post.permalink,
            "title": self.post.title[:150],
            "created": self.post.created,
            "upvotes": self.post.upvotes,
            "comments": self.post.num_comments,
            "score": j.score,
            "signals": j.signals,
            "evidence": [e.to_dict() for e in j.evidence],
            "why": j.why,
            "layer": j.layer,
            "judge_version": j.judge_version,
            "llm_verdict": j.llm_verdict,
            "llm_confidence": j.llm_confidence,
            "llm_reason": j.llm_reason,
            "llm_model": j.llm_model,
            "llm_error": j.llm_error,
            "judged_at": self.judged_at,
            "text": self.post.text[:400],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Lead:
        """Rebuild a Lead from a flat contract dict (used by eval / export)."""
        post = Post(
            id=str(raw.get("id") or ""),
            sub=str(raw.get("sub") or ""),
            title=str(raw.get("title") or ""),
            selftext=str(raw.get("selftext") or ""),
            created_utc=int(raw.get("created_utc") or 0),
            upvotes=raw.get("upvotes"),
            num_comments=raw.get("comments"),
        )
        judgment = Judgment(
            score=int(raw.get("score", 0)),
            signals=list(raw.get("signals", []) or []),
            evidence=[Evidence.from_dict(e) for e in raw.get("evidence", []) or []],
            why=list(raw.get("why", []) or []),
            layer=str(raw.get("layer", LAYER_RULE_V3)),
            judge_version=str(raw.get("judge_version", JUDGE_VERSION)),
            llm_verdict=raw.get("llm_verdict"),
            llm_confidence=(
                None if raw.get("llm_confidence") is None else float(raw["llm_confidence"])
            ),
            llm_reason=str(raw.get("llm_reason") or ""),
            llm_model=str(raw.get("llm_model") or ""),
            llm_error=str(raw.get("llm_error") or ""),
        )
        return cls(post=post, judgment=judgment, judged_at=str(raw.get("judged_at") or ""))
