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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from intentradar.config import ProjectConfig, Watchlist
from intentradar.errors import ConfigError
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
    "llm_verdict",
    "llm_confidence",
    "llm_reason",
    "llm_model",
    "llm_error",
    "judged_at",
    "label",
]

LABEL_COLUMNS = ["id", "permalink", "title", "label", "reviewer", "notes"]

# Accepted spellings for one ground-truth cell. Anything else is a typo and is
# reported as such — an unreadable label must never become a silent 0.
LABEL_TRUE_TOKENS = {"1", "true", "yes", "y", "t", "actionable", "是"}
LABEL_FALSE_TOKENS = {"0", "false", "no", "n", "f", "not_actionable", "否"}
# "The reviewer genuinely could not decide." Real information, but not ground
# truth: these rows are excluded from every metric and counted, exactly like a
# blank cell. Never collapsed into 0 — that would manufacture false positives.
LABEL_BORDERLINE_TOKENS = {"borderline", "unsure", "maybe", "?", "2"}


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
    base_dir: Path = field(default_factory=Path)

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
        return cls(
            testset_id=testset_id, meta=meta, posts=posts, expected_hits=expected, base_dir=base
        )

    # ── labels ─────────────────────────────────────────────────────────────
    def load_labels(self) -> LabelSet:
        """Read ``labels.csv`` next to ``posts.jsonl``."""
        return LabelSet.load(self.base_dir / "labels.csv", self.testset_id)

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
                raise ConfigError(f"testset {dataset.testset_id}: meta.project is missing")
        if not self.watchlist_path:
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
            raise ConfigError(f"export format {fmt!r}: expected csv or jsonl")
        log.info("exported %d hits to %s", len(report.hits), out_path)
        return out_path


# ── ground truth + scoring (W2) ─────────────────────────────────────────────


@dataclass
class LabelSet:
    """The human ground truth for one dataset.

    A blank cell means "not reviewed yet", which is **not** the same as "not
    actionable": unreviewed rows are excluded from every metric *and counted*, so
    the reader can see how much of the number is actually backed by a human.
    """

    labels: dict[str, bool] = field(default_factory=dict)
    rows: int = 0
    blank_ids: list[str] = field(default_factory=list)
    borderline_ids: list[str] = field(default_factory=list)  # reviewer could not decide
    orphan_ids: list[str] = field(default_factory=list)  # labelled but not in the dataset
    testset_id: str = ""

    @property
    def labeled(self) -> int:
        """Rows with a usable 1/0."""
        return len(self.labels)

    @property
    def unlabeled(self) -> int:
        """Rows a reviewer has not filled in yet."""
        return len(self.blank_ids)

    @property
    def borderline(self) -> int:
        """Rows a reviewer marked as undecidable."""
        return len(self.borderline_ids)

    @property
    def undecided_ids(self) -> list[str]:
        """Every row that carries no usable verdict (blank or borderline)."""
        return self.blank_ids + self.borderline_ids

    @property
    def positives(self) -> int:
        """Rows a human marked actionable."""
        return sum(1 for v in self.labels.values() if v)

    @property
    def negatives(self) -> int:
        """Rows a human marked not actionable."""
        return sum(1 for v in self.labels.values() if not v)

    @property
    def complete(self) -> bool:
        """True when every reviewable row carries a usable verdict."""
        return self.rows > 0 and not self.undecided_ids

    @classmethod
    def load(cls, path: Path, testset_id: str = "") -> LabelSet:
        """Read ``labels.csv``. Missing file = an empty (not fabricated) label set."""
        path = Path(path)
        if not path.exists():
            return cls(testset_id=testset_id)
        labels: dict[str, bool] = {}
        blanks: list[str] = []
        borderline: list[str] = []
        unknown: list[str] = []
        rows = 0
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                post_id = str(row.get("id") or "").strip()
                if not post_id:
                    continue
                rows += 1
                raw = str(row.get("label") or "").strip()
                if not raw:
                    blanks.append(post_id)
                    continue
                token = raw.lower()
                if token in LABEL_TRUE_TOKENS:
                    labels[post_id] = True
                elif token in LABEL_FALSE_TOKENS:
                    labels[post_id] = False
                elif token in LABEL_BORDERLINE_TOKENS:
                    borderline.append(post_id)
                else:
                    # Collected, not raised on the first hit: a reviewer fixing a
                    # typo should see every bad cell in one pass.
                    unknown.append(f"{post_id}={raw!r}")

        if unknown:
            shown = ", ".join(unknown[:10])
            more = f" (+{len(unknown) - 10} more)" if len(unknown) > 10 else ""
            raise ConfigError(
                f"{path.name}: {len(unknown)} row(s) have a label that is not "
                f"actionable / not_actionable / borderline: {shown}{more}. "
                f"Fix the typo instead of letting it count as 0."
            )
        return cls(
            labels=labels,
            rows=rows,
            blank_ids=blanks,
            borderline_ids=borderline,
            testset_id=testset_id,
        )

    def against(self, post_ids: Iterable[str]) -> tuple[dict[str, bool], list[str]]:
        """Split labels into (usable, orphan) for one set of post ids."""
        known = set(post_ids)
        usable = {k: v for k, v in self.labels.items() if k in known}
        orphans = sorted(set(self.labels) - known)
        return usable, orphans


@dataclass
class LayerScore:
    """Confusion matrix and derived metrics for one judge layer."""

    layer: str
    judge_version: str
    predicted: int = 0  # posts the layer put above threshold
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    unverified: int = 0  # predicted but the human never labelled it
    skipped_unlabeled: int = 0  # ground-truth rows excluded for having no label
    llm_calls: int = 0
    llm_errors: int = 0
    mock: bool = False
    backend: str = ""  # protocol + endpoint + model, verbatim from the client

    @property
    def precision(self) -> float | None:
        """TP / (TP + FP). ``None`` when the layer predicted nothing."""
        denom = self.tp + self.fp
        if denom == 0:
            return None
        return self.tp / denom

    @property
    def recall(self) -> float | None:
        """TP / (TP + FN). ``None`` when there is no positive ground truth."""
        denom = self.tp + self.fn
        if denom == 0:
            return None
        return self.tp / denom

    @property
    def f1(self) -> float | None:
        """Harmonic mean of precision and recall; ``None`` when either is."""
        p = self.precision
        r = self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    @property
    def noise_rate(self) -> float | None:
        """Share of the layer's hits a human marked not actionable (1 - precision)."""
        p = self.precision
        return None if p is None else 1.0 - p

    @property
    def verified_predictions(self) -> int:
        """Predictions that a human actually passed judgement on."""
        return self.tp + self.fp

    @property
    def title(self) -> str:
        """``rule_v3 (v3.0.0)`` — the header used in the comparison table."""
        return f"{self.layer} ({self.judge_version})"


@dataclass
class ScoreReport:
    """Rule layer vs rule+LLM, computed against the same ground truth."""

    testset_id: str
    project: str
    min_score: int
    labels: LabelSet
    scores: list[LayerScore]
    snapshot_date: str = ""

    def render(self) -> str:
        """Terminal block: both layers side by side, nothing hidden."""
        sep = "─" * 62
        lines = [
            f"testset : {self.testset_id}"
            + (f"   (frozen {self.snapshot_date})" if self.snapshot_date else ""),
            f"project : {self.project}   min_score={self.min_score}",
            f"labels  : {self.labels.rows} rows · {self.labels.labeled} labeled "
            f"({self.labels.positives} actionable / {self.labels.negatives} not) · "
            f"{self.labels.unlabeled} unlabeled · {self.labels.borderline} borderline",
        ]
        if self.labels.orphan_ids:
            lines.append(
                f"          {len(self.labels.orphan_ids)} label(s) reference posts that are "
                f"not in this testset and were ignored"
            )
        lines.append(sep)

        if not self.labels.labeled:
            lines.append("NO GROUND TRUTH — every label cell is empty.")
            lines.append(f"  fill in data/testset/{self.testset_id}/labels.csv (label = 1 or 0)")
            lines.append("  precision/recall cannot be computed from an empty file,")
            lines.append("  and nothing here is guessed: the counts below are 0 because")
            lines.append("  there is nothing to compare against, not because the tool failed.")
            lines.append(sep)
            for score in self.scores:
                lines.append(f"{score.title:<34}predicted {score.predicted}")
            return "\n".join(lines)

        headers = ["metric"] + [s.title for s in self.scores]
        width = max(22, *(len(h) + 2 for h in headers[1:]))
        lines.append(f"{'metric':<18}" + "".join(h.rjust(width) for h in headers[1:]))

        def _pct(value: float | None) -> str:
            return "-" if value is None else f"{value * 100:.1f}%"

        rows: list[tuple[str, list[str]]] = [
            ("predicted", [str(s.predicted) for s in self.scores]),
            ("true positive", [str(s.tp) for s in self.scores]),
            ("false positive", [str(s.fp) for s in self.scores]),
            ("false negative", [str(s.fn) for s in self.scores]),
            ("precision", [_pct(s.precision) for s in self.scores]),
            ("recall", [_pct(s.recall) for s in self.scores]),
            ("F1", [_pct(s.f1) for s in self.scores]),
            ("noise rate", [_pct(s.noise_rate) for s in self.scores]),
            ("unverified hits", [str(s.unverified) for s in self.scores]),
        ]
        for name, values in rows:
            lines.append(f"{name:<18}" + "".join(v.rjust(width) for v in values))

        lines.append(sep)
        for score in self.scores:
            if score.layer == LAYER_RULE_V3:
                continue
            lines.append(
                f"llm: {score.llm_calls} calls · {score.llm_errors} errors "
                f"· backend={score.backend or 'n/a'}"
            )
            if score.mock:
                lines.append("  !! the semantic layer ran against the deterministic mock —")
                lines.append("     its numbers are NOT a measurement. Set an API key to")
                lines.append("     get real rule_v3+llm figures.")
            if score.llm_errors:
                lines.append(
                    f"  !! {score.llm_errors} post(s) could not be judged by the LLM and were"
                )
                lines.append("     scored 0 — recall above is depressed by failures, not by")
                lines.append("     the model's judgement.")

        if not self.labels.complete:
            excluded = len(self.labels.undecided_ids)
            lines.append(
                f"note: {excluded} of {self.labels.rows} rows carry no usable verdict "
                f"({self.labels.unlabeled} unlabeled + {self.labels.borderline} borderline); "
                f"they are excluded from every ratio above, not counted as 0."
            )
        return "\n".join(lines)


class EvalScorer:
    """Replays a frozen dataset through several judges and scores them."""

    def __init__(self, judges: Sequence[Judge] | None = None) -> None:
        """Initialise. Defaults to ``[rule_v3, rule_v3+llm]`` — the comparison."""
        if judges:
            self.judges = list(judges)
        else:
            self.judges = [get_judge(LAYER_RULE_V3), get_judge("rule_v3+llm")]

    def score(
        self,
        dataset: EvalDataset,
        project: ProjectConfig | None = None,
        min_score: int | None = None,
        labels: LabelSet | None = None,
    ) -> ScoreReport:
        """Compute precision / recall / F1 per layer against ``labels.csv``."""
        from intentradar.judge.llm import LLMJudge

        threshold = int(min_score if min_score else dataset.meta.get("min_score") or 5)
        posts = dataset.in_window()
        post_ids = [p.id for p in posts if p.id]
        ground_truth = labels if labels is not None else dataset.load_labels()
        usable, orphans = ground_truth.against(post_ids)
        ground_truth.orphan_ids = orphans

        scores: list[LayerScore] = []
        for judge in self.judges:
            hits: list[Lead] = []
            for post in posts:
                if not post.id:
                    continue
                judgment = judge.judge(post, project) if project else None
                if judgment is None:  # pragma: no cover - project is always resolved
                    continue
                if judgment.score < threshold:
                    continue
                if judge.is_noise(post):
                    continue
                hits.append(Lead(post=post, judgment=judgment))
            hits = dedupe_leads(hits)
            predicted = {lead.post.id for lead in hits}

            tp = fp = fn = tn = 0
            for post_id, is_actionable in usable.items():
                in_hits = post_id in predicted
                if in_hits and is_actionable:
                    tp += 1
                elif in_hits and not is_actionable:
                    fp += 1
                elif not in_hits and is_actionable:
                    fn += 1
                else:
                    tn += 1

            scores.append(
                LayerScore(
                    layer=judge.layer,
                    judge_version=judge.judge_version,
                    predicted=len(predicted),
                    tp=tp,
                    fp=fp,
                    fn=fn,
                    tn=tn,
                    unverified=sum(1 for pid in predicted if pid in ground_truth.undecided_ids),
                    skipped_unlabeled=len(ground_truth.undecided_ids),
                    llm_calls=int(getattr(judge, "llm_calls", 0) or 0) if isinstance(judge, LLMJudge) else 0,
                    llm_errors=int(getattr(judge, "llm_errors", 0) or 0) if isinstance(judge, LLMJudge) else 0,
                    mock=bool(getattr(judge, "is_mock", False)) if isinstance(judge, LLMJudge) else False,
                    backend=(
                        str(judge.describe_backend()) if isinstance(judge, LLMJudge) else ""
                    ),
                )
            )

        project_name = project.name if project else str(dataset.meta.get("project", ""))
        return ScoreReport(
            testset_id=dataset.testset_id,
            project=project_name,
            min_score=threshold,
            labels=ground_truth,
            scores=scores,
            snapshot_date=str(dataset.meta.get("snapshot_date", "")),
        )
