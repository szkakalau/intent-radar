"""rule_v4 — high-recall coarse filter: a net, not a verdict.

Why this exists
---------------
``rule_v3`` is a *verdict* layer: it scores high only when strong intent
phrases appear, and it is scored against a published number (211 → 6 → 2.8%)
that must stay frozen. Measured against the labelled ground truth it has
**0 true positives out of 6 hits** — and, more importantly, 6 of the 7
actionable posts score < 3, so they never even reach the W2 semantic layer.

v3's failure mode is specific and reproducible: **it scores pain, not intent.**
Four of its six hits ("taking up all my phone storage", "TTS on anki mobile?",
"GUYS I AM FORGETTING THE ANATOMY") reach the threshold on
*competitor + pain-word + category-word* alone, with no intent phrase at all.
Meanwhile real requests ("Best Italian Anki Card Deck", "microbio questions …
any workbooks I can use") score 0–1 because their phrasing is not in the
regex lists.

So v4 inverts the contract: **宽进严出 (wide in, strict out).** The rule layer
is no longer the decider — it is the net that feeds candidates to the LLM,
which does the precision work. v4 optimises for recall under a candidate
budget; precision is explicitly delegated downstream.

Three changes versus v3, each traceable to a measured miss:

1. **Sentiment demoted.** ``suck`` / ``hate`` / ``frustrated`` / ``tired of``
   were +4 SWITCH signals in v3. Every one of v3's false positives came in on
   them. They are now +1 weak signals; the real switching phrases
   (``alternative to``, ``switch from``, ``instead of``, ``replacement for``,
   ``worth it``, ``too expensive``, ``cancel``, ``looking for another``,
   ``thinking about subscribing``) keep +4.
2. **ASK widened, and one v3 bug fixed.** ``what\\s+(?:do|are)\\s+you\\s+use``
   cannot match "what **apps** do you use" — no noun slot. v4 allows nouns in
   between. Added: ``does anyone have/know/use/recommend``, ``cannot find``,
   ``any good deck/workbook/resource/course``, ``which … should I``,
   ``suggestions for``, ``do you use a/any``, ``ways to find/get/learn``.
3. **Support/bug posts excluded.** A post whose body reads like a bug report or
   a tech-support question is excluded even when it trips a switching phrase.
   v3's ``1wpik83`` (storage) and ``1wot6p9`` (TTS config) are exactly this.

The category gate is **kept but widened**. Kept, because v3 proved that intent
phrases with no category anchor pull in unrelated domains ("which language
teacher should I pick"). Widened, because a strict ``keyword or competitor``
gate rejects 4 of the 7 labelled actionable posts — they contain no configured
category word at all, and v4 would inherit v3's ceiling. v4 therefore also
accepts a **category hint**: the intent phrase may itself name a purchasable
thing ("any workbooks", "another app", "recommendation"). That is a candidate
decision, not a verdict — the LLM makes the final call.

Frozen v3 is untouched. v4 is a separate layer with its own
``judge_version``, selectable side by side.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from intentradar.config import ProjectConfig
from intentradar.models import (
    JUDGE_VERSION_V4,
    LAYER_RULE_V4,
    Evidence,
    Judgment,
    Post,
    Signal,
    derive_why,
)

log = logging.getLogger(__name__)

# ── 1) 真·切换意图（+4）───────────────────────────────────────────────────
# v3 的 suck / hate / frustrat(ed|ing) / tired of 已从这里移除 —— 实测抓来的
# 全是吐槽和技术支持。它们降级到 WEAK_SENTIMENT_RE 拿 +1。
SWITCH_RE = [
    r"alternative(?:s)?\s+to",
    r"switch(?:ing)?\s+(?:from|away|off)",
    r"instead\s+of",
    r"better\s+than",
    r"any\s+(?:good\s+|decent\s+)?alternative",
    r"worth\s+(?:it|paying|the\s+price|upgrading)",
    r"too\s+expensive",
    r"cancel(?:led|ling|ing)?\b",
    r"migrat(?:e|ing)\s+(?:from|off)",
    r"replacement\s+for",
    r"look(?:ing)?\s+for\s+(?:another|something\s+else|a\s+better|an\s+alternative)",
    r"thinking\s+about\s+(?:subscribing|signing\s+up|switching|cancelling|canceling)",
    r"mov(?:e|ed|ing)\s+(?:away\s+)?from",
]

# ── 2) 弱情绪信号（+1）────────────────────────────────────────────────────
WEAK_SENTIMENT_RE = [
    r"suck(?:s|ed)?\b",
    r"\bhate\b",
    r"frustrat(?:ed|ing)",
    r"tired\s+of",
]

# ── 3) 求助/选型意图（+3，v4 放宽）─────────────────────────────────────────
ASK_RE = [
    # ── v3 原有，保留 ──
    r"any\s+(?:tool|app|apps|software|website|site|program|extension)",
    r"recommend(?:ation)?s?\b",
    r"is\s+there\s+(?:a|an)\b",
    r"looking\s+for\b",
    r"suggestion(?:s)?\b",
    r"advice\s+on\b",
    r"help\s+(?:me\s+)?find",
    r"which\s+(?:app|tool|one)\b",
    # ── v4 新增 ──
    r"does\s+any(?:one|body|one\s+here)\s+(?:have|know|use|recommend|suggest)",
    r"can(?:not|'t)\s+find",
    # 修复 v3 bug: "what apps do you use" 中间可以插名词。
    # 名词槽用 [\w/]+ 而不是 \w+ —— 真实文本里 "apps/resources" 带斜杠；
    # 动词也要收 using/getting，Reddit 上 "what apps are you using?" 更常见。
    r"what\s+(?:[\w/]+\s+){0,3}(?:do|are|should|would|can)\s+(?:you|i|we)\s+"
    r"(?:us(?:e|ing)|recommend(?:ing)?|suggest(?:ing)?|get(?:ting)?|buys?|do)",
    # "any good deck" / "any workbooks" —— 这类措辞本身就是可购买物
    r"any\s+(?:good\s+|decent\s+)?(?:workbook|resource|deck|course|material|book|tool|app)s?\b",
    r"which\s+(?:\w+\s+){0,2}(?:should|do|would|can)\s+i\b",
    r"suggest(?:ion)?s?\s+for",
    r"do\s+you\s+use\s+(?:a|any|the)\b",
    r"ways\s+to\s+(?:be\s+)?(?:found|find|get|reach|learn)",
]

# ── 4) 品类提示：意图短语自带可购买物 ──────────────────────────────────────
# 品类门在 v3 实测有效，但严格版（keyword or competitor）会拒掉 7 条 actionable
# 里的 4 条 —— 它们一个配置品类词都没有。所以 v4 额外接受「意图短语本身点名了
# 一个可购买的东西」。这是候选判断，不是判定，最终由 LLM 决定。
CATEGORY_HINT_RE = [
    r"\b(?:app|apps|application|tool|tools|software|website|site|program|extension|platform)s?\b",
    r"\b(?:deck|decks|workbook|workbooks|resource|resources|course|courses|"
    r"material|materials|book|books|flashcard|flashcards)\b",
    r"\b(?:subscription|plan|tier|premium|paid|free\s+trial)\b",
    r"\b(?:recommendation|alternative|replacement)s?\b",
]

# ── 5) bug 报告 / 技术支持（排除）──────────────────────────────────────────
# v3 的 1wpik83（存储占满）和 1wot6p9（TTS 配置）就是这类：踩到了切换/情绪词，
# 但正文其实是在求技术支持。注意这里刻意不包含 "cannot find" —— 那是真实需求
# （"i cannot find a good deck"），不是故障。
SUPPORT_RE = [
    r"is\s+there\s+any\s+way\s+to",
    r"how\s+do\s+i\s+(?:fix|sync|restore|recover|import|export|reset|set\s+up|configure|install|update)",
    r"\berror\b",
    r"\bbug\b",
    r"not\s+working",
    r"doesn'?t\s+work",
    r"can'?t\s+(?:access|open|log\s*in|sync|download|install|import|export)",
    r"taking\s+up\s+(?:all\s+)?(?:my\s+)?(?:phone\s+)?storage",
    r"out\s+of\s+space",
    r"crash(?:es|ed|ing)?\b",
    r"won'?t\s+(?:open|load|sync)",
]

PAIN_WORDS = [
    "storage", "slow", "expensive", "hate", "sucks", "annoying",
    "broken", "bug", "crash", "laggy", "clunky", "bloated",
    "paywall", "subscription", "price", "cost",
]

NOISE_TITLE_RE = [
    r"mega\s*thread", r"daily\s+(?:thread|discussion)", r"weekly\s+(?:thread|discussion)",
    r"beta\s+feedback", r"release\s+notes", r"\.?\s*\d+\.\d+\s+(?:beta|release)",
    r"mod\s+post", r"rules?\s+(?:update|reminder)", r"\[meta\]",
]

# A v4 candidate is any post carrying a real intent phrase. Below this the post
# never reaches the LLM; at/above it the LLM does the precision work.
CANDIDATE_MIN_SCORE = 3


def score(text: str, proj: Mapping[str, Any]) -> tuple[int, list[str], list[str]]:
    """v4 coarse score. Returns ``(score, why, signals)``.

    Reference implementation; :meth:`RuleV4Judge.judge` is asserted to agree
    with it on every post of both frozen testsets.
    """
    tl = text.lower()
    total = 0
    why: list[str] = []
    sig: set[str] = set()

    # 1) 真·切换意图
    for pat in SWITCH_RE:
        m = re.search(pat, tl)
        if m:
            total += 4
            why.append(f"切换:{m.group(0)[:24]}")
            sig.add(Signal.SWITCH.value)
            break

    # 2) 求助/选型
    for pat in ASK_RE:
        m = re.search(pat, tl)
        if m:
            total += 3
            why.append(f"求助:{m.group(0)[:24]}")
            sig.add(Signal.ASK.value)
            break

    # 3) 弱情绪（v3 里这是 +4，实测全是噪声）
    for pat in WEAK_SENTIMENT_RE:
        m = re.search(pat, tl)
        if m:
            total += 1
            why.append(f"情绪:{m.group(0)[:24]}")
            break

    # 4) 竞品名
    comp_hit = [c for c in proj.get("competitors", []) if c.lower() in tl]
    if comp_hit:
        total += 3
        why.append(f"竞品:{comp_hit[0]}")
        sig.add(Signal.COMPETITOR.value)

    # 5) 品类关键词
    kw_hit = [k for k in proj.get("keywords", []) if k.lower() in tl]
    if kw_hit:
        total += 1
        why.append(f"品类:{kw_hit[0]}")

    # 6) 痛点词 —— 必须与品类词共现
    if kw_hit:
        pain = [w for w in PAIN_WORDS if w in tl]
        if pain:
            total += 2
            why.append(f"痛点:{pain[0]}")
            sig.add(Signal.PAIN.value)

    # 7) bug / 技术支持排除（有明确求助意图的除外）
    if not sig & {Signal.ASK.value}:
        for pat in SUPPORT_RE:
            if re.search(pat, tl):
                return 0, [f"技术支持({pat[:24]})"], sorted(sig)

    # 8) 品类门（放宽：意图短语自带可购买物也算）
    hint = next((p for p in CATEGORY_HINT_RE if re.search(p, tl)), "") if not (kw_hit or comp_hit) else ""
    if not kw_hit and not comp_hit and not hint:
        return 0, [f"无品类关联({'/'.join(sorted(sig)) or '弱信号'})"], sorted(sig)
    if hint:
        sig.add("品类提示")

    return total, why, sorted(sig)


def is_noise(title: str) -> bool:
    """Official / megathread posts are not actionable leads. Same gate as v3."""
    t = title.lower()
    return any(re.search(p, t) for p in NOISE_TITLE_RE)


def _analyze(text: str, proj: Mapping[str, Any]) -> tuple[int, list[str], list[Evidence]]:
    """Mirror of :func:`score` that also emits machine-readable evidence."""
    tl = text.lower()
    total = 0
    evidence: list[Evidence] = []
    sig: set[str] = set()

    for pat in SWITCH_RE:
        m = re.search(pat, tl)
        if m:
            total += 4
            evidence.append(Evidence(type="switch", pattern=pat, matched=m.group(0)))
            sig.add(Signal.SWITCH.value)
            break

    for pat in ASK_RE:
        m = re.search(pat, tl)
        if m:
            total += 3
            evidence.append(Evidence(type="ask", pattern=pat, matched=m.group(0)))
            sig.add(Signal.ASK.value)
            break

    for pat in WEAK_SENTIMENT_RE:
        m = re.search(pat, tl)
        if m:
            total += 1
            evidence.append(Evidence(type="weak_sentiment", pattern=pat, matched=m.group(0)))
            break

    comp_hit = [c for c in proj.get("competitors", []) if c.lower() in tl]
    if comp_hit:
        total += 3
        evidence.append(Evidence(type="competitor", value=comp_hit[0]))
        sig.add(Signal.COMPETITOR.value)

    kw_hit = [k for k in proj.get("keywords", []) if k.lower() in tl]
    if kw_hit:
        total += 1
        evidence.append(Evidence(type="keyword", value=kw_hit[0]))

    if kw_hit:
        pain = [w for w in PAIN_WORDS if w in tl]
        if pain:
            total += 2
            evidence.append(Evidence(type="pain", value=pain[0], cooccur=kw_hit[0]))
            sig.add(Signal.PAIN.value)

    if not sig & {Signal.ASK.value}:
        for pat in SUPPORT_RE:
            m = re.search(pat, tl)
            if m:
                evidence.append(Evidence(type="support", pattern=pat, matched=m.group(0)))
                return 0, sorted(sig), evidence

    hint = ""
    if not (kw_hit or comp_hit):
        for pat in CATEGORY_HINT_RE:
            m = re.search(pat, tl)
            if m:
                hint = m.group(0)
                evidence.append(Evidence(type="category_hint", pattern=pat, matched=hint))
                break

    if not kw_hit and not comp_hit and not hint:
        evidence.append(Evidence(type="gate", value="no_category_or_competitor"))
        return 0, sorted(sig), evidence
    if hint:
        sig.add("品类提示")

    return total, sorted(sig), evidence


class RuleV4Judge:
    """v4: a high-recall candidate net. Feeds the LLM; does not decide alone."""

    layer: str = LAYER_RULE_V4
    judge_version: str = JUDGE_VERSION_V4

    def judge(self, post: Post, project: ProjectConfig) -> Judgment:
        """Score one post and return a fully auditable judgment."""
        total, signals, evidence = _analyze(post.text, project.to_mapping())
        why = derive_why(evidence, signals)
        return Judgment(
            score=total,
            signals=signals,
            evidence=evidence,
            why=why,
            layer=self.layer,
            judge_version=self.judge_version,
        )

    def is_noise(self, post: Post) -> bool:
        """Whether the post is an official / megathread post."""
        return is_noise(post.title)
