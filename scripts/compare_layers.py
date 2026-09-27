"""Compare rule layers v3 / v4 against the labelled ground truth.

Run from the repo root::

    uv run python scripts/compare_layers.py

This is the acceptance harness for the v4 "wide in, strict out" redesign. It
answers three questions with numbers, not opinions:

* **recall** — of the posts a human marked ``actionable``, how many does each
  rule layer even offer as a candidate? (v3's answer was 0 of 7, which is why
  the W2 semantic layer had nothing to work on: it can only subtract.)
* **candidate volume** — how many posts each layer sends onward. This is the
  cost side of the trade: every candidate is an LLM call.
* **which ones are missed** — the id, score and title of every actionable post
  the layer fails to surface, so a miss can be diagnosed instead of averaged.

Nothing here is committed as a published number; it is the measurement that
decides whether a number is worth publishing.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from intentradar.config import Watchlist  # noqa: E402
from intentradar.eval import EvalDataset  # noqa: E402
from intentradar.judge import get_judge  # noqa: E402
from intentradar.judge.rule_v4 import CANDIDATE_MIN_SCORE  # noqa: E402
from intentradar.models import Post  # noqa: E402

TESTSET_DIR = REPO_ROOT / "data" / "testset"
WATCHLIST_PATH = REPO_ROOT / "config" / "watchlist.json"

TESTSETS = [
    ("einprag-2026-09-27", "Einprag"),
    ("bootstrap-2026-09-27", "IntentRadar_自举"),
]
LAYERS = ["rule_v3", "rule_v4"]

# Acceptance targets set by team-lead for P0-A.
TARGETS = {
    "einprag-2026-09-27": {"recall": 6, "max_candidates": 60},
    "bootstrap-2026-09-27": {"recall": 2, "max_candidates": 60},
}


def load_labels(base: Path) -> dict[str, str]:
    """Read ``labels.csv`` into ``{post_id: raw_label}``."""
    path = base / "labels.csv"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {
            str(row.get("id") or "").strip(): str(row.get("label") or "").strip()
            for row in csv.DictReader(fh)
            if str(row.get("id") or "").strip()
        }


def candidates(posts: list[Post], judge: object, project: object, threshold: int) -> dict[str, int]:
    """Return ``{post_id: score}`` for every post at/above ``threshold``."""
    out: dict[str, int] = {}
    for post in posts:
        if not post.id:
            continue
        judgment = judge.judge(post, project)  # type: ignore[attr-defined]
        if judgment.score < threshold:
            continue
        if judge.is_noise(post):  # type: ignore[attr-defined]
            continue
        out[post.id] = judgment.score
    return out


def main() -> int:
    """Print the v3 / v4 comparison and return 0 (targets are advisory)."""
    watchlist = Watchlist.load(WATCHLIST_PATH)
    overall_ok = True

    print(f"candidate threshold for v4: score >= {CANDIDATE_MIN_SCORE}")
    print("v3 threshold: score >= 5 (its published min_score)\n")

    for ts_id, project_name in TESTSETS:
        dataset = EvalDataset.load(ts_id, TESTSET_DIR)
        project = watchlist.get(project_name)
        labels = load_labels(dataset.base_dir)
        posts = dataset.in_window()

        actionable = {pid for pid, lab in labels.items() if lab == "actionable"}
        target = TARGETS.get(ts_id, {})

        print("=" * 78)
        print(f"{ts_id}   posts in window: {len(posts)}   actionable: {len(actionable)}")
        print("=" * 78)

        for layer in LAYERS:
            judge = get_judge(layer)
            threshold = CANDIDATE_MIN_SCORE if layer == "rule_v4" else 5
            hits = candidates(posts, judge, project, threshold)
            caught = sorted(set(hits) & actionable)
            missed = sorted(actionable - set(hits))

            print(f"\n[{layer}]  score >= {threshold}")
            print(f"  candidates      {len(hits)}")
            print(f"  actionable hit  {len(caught)} / {len(actionable)}")
            print(f"  recall          {len(caught) / len(actionable) * 100:.1f}%"
                  if actionable else "  recall          n/a")
            if missed:
                print("  MISSED actionable posts (score = what the layer actually gave):")
                for pid in missed:
                    post = next((p for p in posts if p.id == pid), None)
                    title = post.title if post else "?"
                    # Re-judge rather than reusing `hits`: a miss means the post
                    # scored *below* the threshold, so the score is not in `hits`
                    # at all. Printing it is the whole point of the diagnosis.
                    score = judge.judge(post, project).score if post else "?"
                    print(f"    - {pid}  score={score}  {title[:66]}")
            else:
                print("  MISSED actionable posts: none")

            # Targets were set for v4 (the layer being designed). v3 is the
            # frozen baseline under comparison — it is expected to fail them,
            # and reporting that as a "miss" would bury the actual signal.
            if layer != "rule_v4":
                continue
            t_recall = target.get("recall")
            t_vol = target.get("max_candidates")
            notes = []
            if t_recall is not None and len(caught) < t_recall:
                notes.append(f"recall {len(caught)} < target {t_recall}")
                overall_ok = False
            if t_vol is not None and len(hits) > t_vol:
                notes.append(f"candidates {len(hits)} > budget {t_vol}")
                overall_ok = False
            if notes:
                print(f"  !! BELOW TARGET: {'; '.join(notes)}")
            else:
                print(f"  targets: recall >= {t_recall} OK · candidates <= {t_vol} OK")
        print()

    print("=" * 78)
    print("ALL TARGETS MET" if overall_ok else "TARGETS NOT MET — see the !! lines above")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
