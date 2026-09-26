"""Offline evaluation: frozen datasets, recomputation, exports (P0-6).

The promise "anyone can recompute our number" is only real if the dataset is
frozen on disk and the recomputation needs no network and no credentials.
:meth:`EvalDataset.snapshot` freezes a dataset; :meth:`EvalRunner.run` replays
it.
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from intentradar.config import ProjectConfig, Watchlist
from intentradar.judge import Judge, get_judge
from intentradar.models import LAYER_RULE_V3, Lead, Post
from intentradar.pipeline import dedupe_leads

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "id",
    "sub",
    "title",
    "permalink",
    "created",
    "upvotes",
    "comments",
    "score",
    "signals",
    "evidence_json",
    "why",
    "layer",
    "judge_version",
    "judged_at",
    "label",
]

LABEL_COLUMNS = ["id", "permalink", "title", "label", "reviewer", "notes"]


@dataclass
class EvalReport:
    """The four numbers plus the audit trail that explains them."""

    testset_id: str
    project: str
    total: int
    hits: list[Lead]
    layer: str
    judge_version: str
    expected: list[dict[str, Any]] = field(default_factory=list)
    expected_match: bool = True
    snapshot_date: str = ""

    @property
    def hit_count(self) -> int:
        """Number of posts over threshold."""
        return len(self.hits)

    @property
    def hit_rate_pct(self) -> float:
        """hit_count / total, as a percentage rounded to one decimal."""
        if not self.total:
            return 0.0
        return round(self.hit_count / self.total * 100, 1)

    def render(self) -> str:
        """Terminal block (PRD §3.3 layout)."""
        sep = "─" * 45
        lines = [
            f"testset : {self.testset_id}"
            + (f"   (frozen {self.snapshot_date})" if self.snapshot_date else ""),
            f"judge   : {self.layer} ({self.judge_version})",
            sep,
            f"{'posts in window':<24}{self.total}",
            f"{'hits over threshold':<24}{self.hit_count}",
            f"{'hit rate':<24}{self.hit_rate_pct}%",
            "─",
            "noise rate = (# hits a human marks 'not actionable') / (# hits)",
            "no manual review included in this run "
            f"(see data/testset/{self.testset_id}/labels.csv)",
            "→ full accuracy incl. human review is delivered in W2 (`eval score`)",
        ]
        if self.expected:
            status = "MATCH" if self.expected_match else "MISMATCH"
            lines.append(f"expected {len(self.expected)} hits: {status}")
        return "\n".join(lines)


@dataclass
class EvalDataset:
    """A frozen snapshot: posts + metadata + the hit set recorded at freeze time."""

    testset_id: str
    meta: dict[str, Any]
    posts: list[Post]
    expected_hits: list[dict[str, Any]] = field(default_factory=list)

    # ── loading ────────────────────────────────────────────────────────────
    @staticmethod
    def list_available(root: Path) -> list[str]:
        """Ids of every frozen dataset under ``root``."""
        root = Path(root)
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if (p / "posts.jsonl").exists())

    @classmethod
    def load(cls, testset_id: str, root: Path) -> EvalDataset:
        """Load a frozen dataset from ``<root>/<testset_id>/``."""
        base = Path(root) / testset_id
        posts_path = base / "posts.jsonl"
        if not posts_path.exists():
            from intentradar.errors import ConfigError

            available = cls.list_available(root)
            raise ConfigError(
                f"testset {testset_id!r} not found under {root}"
                + (f" (available: {', '.join(available)})" if available else "")
            )
        posts: list[Post] = []
        with posts_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    posts.append(Post.from_raw(json.loads(line)))
        meta = {}
        meta_path = base / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        expected: list[dict[str, Any]] = []
        expected_path = base / "expected_hits.json"
        if expected_path.exists():
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
        return cls(testset_id=testset_id, meta=meta, posts=posts, expected_hits=expected)

    # ── derived helpers ────────────────────────────────────────────────────
    @property
    def cutoff_utc(self) -> int:
        """Epoch cutoff of the frozen rolling window (0 = no filtering)."""
        if self.meta.get("cutoff_utc"):
            return int(self.meta["cutoff_utc"])
        collected = self.meta.get("collected_at")
        if collected:
            ts = datetime.fromisoformat(collected).timestamp()
            return int(ts - int(self.meta.get("window_days", 3)) * 86400)
        return 0

    def in_window(self) -> list[Post]:
        """Posts inside the frozen window."""
        cutoff = self.cutoff_utc
        if not cutoff:
            return list(self.posts)
        return [p for p in self.posts if p.created_utc >= cutoff]

    # ── freezing ───────────────────────────────────────────────────────────
    @classmethod
    def snapshot(
        cls,
        project: ProjectConfig,
        provider: Any,
        judge: Judge,
        root: Path,
        window_days: int,
        min_score: int,
        pages_per_sub: int = 2,
        testset_id: str | None = None,
        cache: str | None = "1d",
    ) -> EvalDataset:
        """Collect live, judge everything, and freeze the result on disk.

        Only non-personal fields are stored — **author names are dropped**.
        """
        from intentradar.collect import SourceResult

        now = int(datetime.now().timestamp())
        cutoff = now - window_days * 86400
        testset_id = testset_id or f"{project.name.lower().replace(' ', '-')}-{datetime.now().strftime('%Y-%m-%d')}"

        raw_posts: list[Post] = []
        per_sub: dict[str, int] = {}
        for sub in project.subreddits:
            result: SourceResult = provider.fetch_subreddit(sub, pages=pages_per_sub, cache=cache)
            per_sub[sub] = len(result.posts)
            raw_posts.extend(result.posts)

        # De-duplicate raw ids, keep window filter, judge every post.
        unique: dict[str, Post] = {}
        for post in raw_posts:
            if post.id and post.id not in unique:
                unique[post.id] = post
        in_window = [p for p in unique.values() if p.created_utc >= cutoff]

        hits: list[Lead] = []
        for post in in_window:
            judgment = judge.judge(post, project)
            if judgment.score < min_score:
                continue
            if judge.is_noise(post):
                continue
            hits.append(Lead(post=post, judgment=judgment))
        hits = dedupe_leads(hits)
        hits.sort(key=lambda lead: -lead.judgment.score)

        base = Path(root) / testset_id
        base.mkdir(parents=True, exist_ok=True)

        with (base / "posts.jsonl").open("w", encoding="utf-8") as fh:
            for post in in_window:
                payload = post.to_dict()
                payload.pop("author", None)
                fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

        expected = [{"id": lead.post.id, "score": lead.judgment.score} for lead in hits]
        (base / "expected_hits.json").write_text(
            json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        meta = {
            "testset_id": testset_id,
            "project": project.name,
            "snapshot_date": datetime.now().strftime("%Y-%m-%d"),
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "window_days": window_days,
            "pages_per_sub": pages_per_sub,
            "subreddits": list(project.subreddits),
            "posts_per_sub": per_sub,
            "total_posts": len(in_window),
            "total_in_window": len(in_window),
            "cutoff_utc": cutoff,
            "min_score": min_score,
            "judge_version": judge.judge_version,
            "layer": judge.layer,
            "hits": len(hits),
            "hit_rate_pct": round(len(hits) / len(in_window) * 100, 1) if in_window else 0.0,
            "privacy": "author / user names are never stored",
        }
        (base / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        cls._write_labels(base, in_window)

        log.info("froze %d posts / %d hits into %s", len(in_window), len(hits), base)
        return cls(testset_id=testset_id, meta=meta, posts=in_window, expected_hits=expected)

    @staticmethod
    def _write_labels(base: Path, posts: Iterable[Post]) -> None:
        """Write ``labels.csv`` with empty label columns, ready for a reviewer."""
        with (base / "labels.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
            writer.writeheader()
            for post in posts:
                writer.writerow(
                    {
                        "id": post.id,
                        "permalink": post.permalink,
                        "title": post.title[:150],
                        "label": "",
                        "reviewer": "",
                        "notes": "",
                    }
                )


class EvalRunner:
    """Replays a frozen dataset through a judge, fully offline."""

    def __init__(self, judge: Judge | None = None, watchlist_path: Path | None = None) -> None:
        """Initialise with a judge (default ``rule_v3``) and a watchlist path."""
        self.judge = judge or get_judge(LAYER_RULE_V3)
        self.watchlist_path = watchlist_path

    def _resolve_project(self, dataset: EvalDataset) -> ProjectConfig:
        """Find the project config referenced by the dataset metadata."""
        name = dataset.meta.get("project")
        if not name:
            from intentradar.errors import ConfigError

            raise ConfigError(f"testset {dataset.testset_id}: meta.project is missing")
        if not self.watchlist_path:
            from intentradar.errors import ConfigError

            raise ConfigError("watchlist path is required to resolve the project config")
        return Watchlist.load(Path(self.watchlist_path)).get(str(name))

    def run(self, dataset: EvalDataset, project: ProjectConfig | None = None) -> EvalReport:
        """Recompute the hit set for a frozen dataset."""
        project = project or self._resolve_project(dataset)
        min_score = int(dataset.meta.get("min_score") or 5)

        posts = dataset.in_window()
        hits: list[Lead] = []
        for post in posts:
            judgment = self.judge.judge(post, project)
            if judgment.score < min_score:
                continue
            if self.judge.is_noise(post):
                continue
            hits.append(Lead(post=post, judgment=judgment))
        hits = dedupe_leads(hits)
        hits.sort(key=lambda lead: -lead.judgment.score)

        expected = dataset.expected_hits
        actual_ids = {lead.post.id: lead.judgment.score for lead in hits}
        match = True
        if expected:
            expected_ids = {str(e["id"]): int(e["score"]) for e in expected}
            match = expected_ids == actual_ids

        return EvalReport(
            testset_id=dataset.testset_id,
            project=project.name,
            total=len(posts),
            hits=hits,
            layer=self.judge.layer,
            judge_version=self.judge.judge_version,
            expected=expected,
            expected_match=match,
            snapshot_date=str(dataset.meta.get("snapshot_date", "")),
        )

    # ── exports ────────────────────────────────────────────────────────────
    def export(self, report: EvalReport, fmt: str, out_path: Path) -> Path:
        """Write the hits to CSV or JSONL so a third party can review them."""
        fmt = fmt.lower()
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "csv":
            with out_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
                writer.writeheader()
                for lead in report.hits:
                    payload = lead.to_dict()
                    writer.writerow(
                        {
                            "id": payload["id"],
                            "sub": payload["sub"],
                            "title": payload["title"],
                            "permalink": payload["permalink"],
                            "created": payload["created"],
                            "upvotes": payload["upvotes"],
                            "comments": payload["comments"],
                            "score": payload["score"],
                            "signals": "|".join(payload["signals"]),
                            "evidence_json": json.dumps(payload["evidence"], ensure_ascii=False),
                            "why": "|".join(payload["why"]),
                            "layer": payload["layer"],
                            "judge_version": payload["judge_version"],
                            "judged_at": payload["judged_at"],
                            "label": "",
                        }
                    )
        elif fmt in {"jsonl", "json"}:
            with out_path.open("w", encoding="utf-8") as fh:
                for lead in report.hits:
                    fh.write(json.dumps(lead.to_dict(), ensure_ascii=False) + "\n")
        else:
            from intentradar.errors import ConfigError

            raise ConfigError(f"export format {fmt!r}: expected csv or jsonl")
        log.info("exported %d hits to %s", len(report.hits), out_path)
        return out_path
