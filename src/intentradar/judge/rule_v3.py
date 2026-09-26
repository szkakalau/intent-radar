"""rule_v3 — the regex intent judge, migrated verbatim from ``scripts/monitor.py``.

RED LINE MODULE
---------------
The scoring constants and the ``score()`` body below are a **byte-for-byte
port** of ``scripts/monitor.py`` lines 110–190, including list order, the
``break`` statements and the ``[:24]`` truncation. The published baseline
(211 posts in window → 6 hits → 2.8%) is only reproducible if this behaviour is
preserved exactly. Do not "clean up" these regexes without bumping
``models.JUDGE_VERSION`` and re-publishing the number.

Six behaviours the regression test pins down:

1. ``SWITCH_RE`` / ``ASK_RE`` are phrase-level regexes, not bag-of-words; only
   the **first** match counts (``break``), +4 / +3.
2. Competitor names are scanned in **config order**, +3.
3. Category keywords are scanned in **config order**, +1.
4. Pain words only count when a category keyword is present (co-occurrence), +2.
5. Category gate: no keyword *and* no competitor → score 0 and the reason list
   collapses to ``无品类关联(...)``.
6. A question mark never adds score, and official posts are filtered by
   ``NOISE_TITLE_RE``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from intentradar.config import ProjectConfig
from intentradar.models import (
    JUDGE_VERSION,
    LAYER_RULE_V3,
    Evidence,
    Judgment,
    Post,
    Signal,
    derive_why,
)

log = logging.getLogger(__name__)

# ── 意图信号词典（短语级，不用单词）─────────────────────────────────────
# v1 用「词袋 OR + 问号加分」被实测打脸：169 命中里 87 条只因为带个问号。
# v2 改成：切换意图 / 求助意图 用正则短语，痛点词必须和品类词共现才算数。
# 下面四个常量与 monitor.py 完全一致，禁止修改。
SWITCH_RE = [
    r"alternative(?:s)?\s+to", r"switch(?:ing)?\s+(?:from|away|off)",
    r"instead\s+of", r"better\s+than", r"any\s+(?:good\s+|decent\s+)?alternative",
    r"worth\s+(?:it|paying|the\s+price|upgrading)", r"too\s+expensive",
    r"suck(?:s|ed)?\b", r"\bhate\b", r"frustrat(?:ed|ing)", r"cancel(?:led|ling|ling)",
    r"migrat(?:e|ing)\s+(?:from|off)", r"replacement\s+for", r"tired\s+of",
]
ASK_RE = [
    r"any\s+(?:tool|app|apps|software|website|site|program|extension)",
    r"recommend(?:ation)?s?\b", r"is\s+there\s+(?:a|an)\b", r"looking\s+for\b",
    r"what\s+(?:do|are)\s+you\s+use", r"which\s+(?:app|tool|one)\b",
    r"suggestion(?:s)?\b", r"advice\s+on\b", r"help\s+(?:me\s+)?find",
]
PAIN_WORDS = ["storage", "slow", "expensive", "hate", "sucks", "annoying",
              "broken", "bug", "crash", "laggy", "clunky", "bloated",
              "paywall", "subscription", "price", "cost"]

NOISE_TITLE_RE = [
    r"mega\s*thread", r"daily\s+(?:thread|discussion)", r"weekly\s+(?:thread|discussion)",
    r"beta\s+feedback", r"release\s+notes", r"\.?\s*\d+\.\d+\s+(?:beta|release)",
    r"mod\s+post", r"rules?\s+(?:update|reminder)", r"\[meta\]",
]


def score(text: str, proj: Mapping[str, Any]) -> tuple[int, list[str], list[str]]:
    """短语级意图打分。返回 (分数, 命中理由, 信号类型)

    Verbatim port of ``scripts/monitor.py::score``. Kept as the canonical
    reference implementation; :meth:`RuleV3Judge.judge` is asserted to agree
    with it on every post of the frozen testset (see
    ``tests/test_rule_v3_regression.py``).
    """
    tl = text.lower()
    s, why, sig = 0, [], set()

    # 1) 切换/流失意图 —— 最强信号
    for pat in SWITCH_RE:
        m = re.search(pat, tl)
        if m:
            s += 4
            why.append(f"切换:{m.group(0)[:24]}")
            sig.add("切换意图")
            break          # 只算一次，避免叠加灌水

    # 2) 求助/选型意图
    for pat in ASK_RE:
        m = re.search(pat, tl)
        if m:
            s += 3
            why.append(f"求助:{m.group(0)[:24]}")
            sig.add("求助选型")
            break

    # 3) 竞品名
    comp_hit = [c for c in proj.get("competitors", []) if c.lower() in tl]
    if comp_hit:
        s += 3
        why.append(f"竞品:{comp_hit[0]}")
        sig.add("竞品提及")

    # 4) 品类关键词
    kw_hit = [k for k in proj.get("keywords", []) if k.lower() in tl]
    if kw_hit:
        s += 1
        why.append(f"品类:{kw_hit[0]}")

    # 5) 痛点词 —— 必须与品类词共现，否则是通用吐槽（"this problem"）
    if kw_hit:
        pain = [w for w in PAIN_WORDS if w in tl]
        if pain:
            s += 2
            why.append(f"痛点:{pain[0]}")
            sig.add("痛点吐槽")

    # 6) 品类门：没有品类词/竞品名的「切换意图」多半是别的领域的吐槽
    #    实测砍掉了「选语言老师」「医学生 outsider」这类与业务无关的强意图帖
    if not kw_hit and not comp_hit:
        # NOTE: the original joined the raw *set* (`'/'.join(sig)`), whose iteration
        # order depends on PYTHONHASHSEED and therefore varies between processes.
        # The data contract (§3.2) specifies `'/'.join(signals)` — the sorted list —
        # and §"确定性" of the architecture requires signals to be sorted(set).
        # Gate-rejected posts score 0 and never reach a report, so this makes the
        # output deterministic without touching any published number.
        return 0, [f"无品类关联({'/'.join(sorted(sig)) or '弱信号'})"], sorted(sig)

    # 注意：不再给「问号」单独加分 —— Reddit 上一半帖子都带问号
    return s, why, sorted(sig)


def is_noise(title: str) -> bool:
    """Official / megathread posts are not actionable leads. Verbatim port."""
    t = title.lower()
    return any(re.search(p, t) for p in NOISE_TITLE_RE)


def _analyze(text: str, proj: Mapping[str, Any]) -> tuple[int, list[str], list[Evidence]]:
    """Mirror of :func:`score` that also emits machine-readable evidence.

    The numeric behaviour is identical (same constants, same order, same
    ``break``s); only the return type differs. ``tests/test_rule_v3_regression.py``
    asserts the two agree on every post of the frozen dataset.
    """
    tl = text.lower()
    total = 0
    evidence: list[Evidence] = []
    sig: set[str] = set()

    # 1) 切换/流失意图
    for pat in SWITCH_RE:
        m = re.search(pat, tl)
        if m:
            total += 4
            evidence.append(Evidence(type="switch", pattern=pat, matched=m.group(0)))
            sig.add(Signal.SWITCH.value)
            break

    # 2) 求助/选型意图
    for pat in ASK_RE:
        m = re.search(pat, tl)
        if m:
            total += 3
            evidence.append(Evidence(type="ask", pattern=pat, matched=m.group(0)))
            sig.add(Signal.ASK.value)
            break

    # 3) 竞品名
    comp_hit = [c for c in proj.get("competitors", []) if c.lower() in tl]
    if comp_hit:
        total += 3
        evidence.append(Evidence(type="competitor", value=comp_hit[0]))
        sig.add(Signal.COMPETITOR.value)

    # 4) 品类关键词
    kw_hit = [k for k in proj.get("keywords", []) if k.lower() in tl]
    if kw_hit:
        total += 1
        evidence.append(Evidence(type="keyword", value=kw_hit[0]))

    # 5) 痛点词 —— 必须与品类词共现
    if kw_hit:
        pain = [w for w in PAIN_WORDS if w in tl]
        if pain:
            total += 2
            evidence.append(Evidence(type="pain", value=pain[0], cooccur=kw_hit[0]))
            sig.add(Signal.PAIN.value)

    signals = sorted(sig)

    # 6) 品类门
    if not kw_hit and not comp_hit:
        evidence.append(Evidence(type="gate", value="no_category_or_competitor"))
        return 0, signals, evidence

    return total, signals, evidence


class RuleV3Judge:
    """The rule-based judge. Pure: no network, no file I/O."""

    layer: str = LAYER_RULE_V3
    judge_version: str = JUDGE_VERSION

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
