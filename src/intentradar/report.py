"""Daily report writer (Markdown + JSON).

The Markdown layout is the one already validated in production
(``scripts/reports/2026-09-27.md``) with a single addition required by the PRD:
an **evidence line** per lead, so a reader can audit a judgment without leaving
the report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from intentradar.models import Evidence, Lead

log = logging.getLogger(__name__)

WHY_SEPARATOR = " | "  # PRD §3.3


def format_evidence(evidence: list[Evidence]) -> str:
    """Render evidence as one auditable line.

    Example::

        ask[which\\s+(?:app|tool|one)\\b] → "which app"; competitor=brainscape;
        keyword=flashcard; pain=cost(cooccur=flashcard)
    """
    parts: list[str] = []
    for e in evidence:
        if e.type in {"switch", "ask"}:
            parts.append(f"{e.type}[{e.pattern or ''}] → \"{e.matched or ''}\"")
        elif e.type == "pain":
            cooccur = f"(cooccur={e.cooccur})" if e.cooccur else ""
            parts.append(f"pain={e.value}{cooccur}")
        else:
            parts.append(f"{e.type}={e.value}")
    return "; ".join(parts)


@dataclass
class ProjectSection:
    """One project's worth of leads for the report."""

    name: str
    scanned: int
    leads: list[Lead] = field(default_factory=list)
    layer: str = ""
    judge_version: str = ""


@dataclass
class ReportPaths:
    """Paths of the files written by :meth:`ReportWriter.write`."""

    markdown: Path
    json: Path


class ReportWriter:
    """Writes ``reports/<date>.md`` and ``reports/<date>.json``."""

    def __init__(self, out_dir: Path) -> None:
        """Initialise with the output directory (created on demand)."""
        self.out_dir = Path(out_dir)

    def write(self, date: str, sections: list[ProjectSection]) -> ReportPaths:
        """Write both files and return their paths."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        md_path = self.out_dir / f"{date}.md"
        json_path = self.out_dir / f"{date}.json"

        md_path.write_text(self.render_markdown(date, sections), encoding="utf-8")
        payload = {
            "date": date,
            "projects": {
                s.name: {
                    "scanned": s.scanned,
                    "hits": len(s.leads),
                    "layer": s.layer,
                    "judge_version": s.judge_version,
                    "leads": [lead.to_dict() for lead in s.leads],
                }
                for s in sections
            },
        }
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("wrote report %s", md_path)
        return ReportPaths(markdown=md_path, json=json_path)

    @staticmethod
    def render_markdown(date: str, sections: list[ProjectSection]) -> str:
        """Build the Markdown body (pure function, easy to test)."""
        lines: list[str] = [f"# Reddit 监控日报 {date}", ""]
        for section in sections:
            hot = [
                lead
                for lead in section.leads
                if "切换意图" in lead.judgment.signals or "求助选型" in lead.judgment.signals
            ]
            lines.append(f"## {section.name} — {len(section.leads)} 条（强意图 {len(hot)} 条）")
            lines.append("")
            if not section.leads:
                lines.append("_本轮无新命中_")
                lines.append("")
                continue
            for lead in section.leads:
                post, judgment = lead.post, lead.judgment
                lines.append(f"### [{judgment.score}分] {post.title[:150]}")
                meta = (
                    f"- r/{post.sub} · {post.created} · ↑{post.upvotes} · 💬{post.num_comments}"
                )
                if judgment.signals:
                    meta += f" · 信号：{'/'.join(judgment.signals)}"
                lines.append(meta)
                lines.append(f"- 命中理由：{WHY_SEPARATOR.join(judgment.why)}")
                lines.append(f"- 证据：{format_evidence(judgment.evidence)}")
                lines.append(f"- {post.permalink}")
                text = post.text[:200]
                if text:
                    lines.append(f"- 摘要：{text}")
                lines.append("")
        return "\n".join(lines)
