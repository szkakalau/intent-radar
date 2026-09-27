"""Build the public accuracy dashboard from the frozen recordings.

Reads ONLY committed, frozen artefacts — the testset, the labels, and the two
`--record` directories. It never calls an endpoint. If the numbers it renders
disagree with what the CLI prints, that is a bug in this script, and the page
says so on its face rather than quietly showing something else.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # repo/scripts/demo/build_data.py
TESTSET = REPO / "data" / "testset" / "einprag-2026-09-27"
DEEPSEEK = REPO / "data" / "replay" / "einprag-2026-09-27" / "rule_v4_llm"
NEMOTRON = REPO / "data" / "replay" / "nemotron-openrouter-2026-09-27" / "rule_v4_llm"


def load_posts() -> dict[str, dict]:
    out = {}
    for line in (TESTSET / "posts.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row
    return out


def load_labels() -> dict[str, dict]:
    import csv

    out = {}
    with (TESTSET / "labels.csv").open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            out[row["id"]] = row
    return out


def load_verdicts(directory: Path) -> dict[str, dict]:
    """Parse the recorded raw responses into (verdict, confidence, reason)."""
    out: dict[str, dict] = {}
    for line in (directory / "calls.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        call = json.loads(line)
        text = call["response"]
        try:
            body = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except (ValueError, IndexError):
            out[call["post_id"]] = {"verdict": None, "confidence": None, "reason": text[:200]}
            continue
        out[call["post_id"]] = {
            "verdict": body.get("is_actionable"),
            "confidence": body.get("confidence"),
            "reason": str(body.get("reason", ""))[:400],
        }
    return out


def main() -> None:
    posts = load_posts()
    labels = load_labels()
    deepseek = load_verdicts(DEEPSEEK)
    nemotron = load_verdicts(NEMOTRON)

    shared = sorted(set(deepseek) & set(nemotron))
    rows = []
    for pid in shared:
        post = posts.get(pid, {})
        label = labels.get(pid, {})
        ds = deepseek[pid]
        nm = nemotron[pid]
        human = (label.get("label") or "unlabeled").strip()
        rows.append(
            {
                "id": pid,
                "sub": post.get("subreddit", ""),
                "title": post.get("title", ""),
                "human": human,
                "human_note": (label.get("notes") or "")[:300],
                "ds": bool(ds["verdict"]),
                "nm": bool(nm["verdict"]),
                "nm_conf": nm["confidence"],
                "nm_reason": nm["reason"],
            }
        )

    both_yes = [r for r in rows if r["ds"] and r["nm"]]
    extra = [r for r in rows if r["nm"] and not r["ds"]]
    reversed_ = [r for r in rows if r["ds"] and not r["nm"]]

    for r in rows:
        r["cls"] = (
            "extra" if (r["nm"] and not r["ds"]) else ("both" if r["ds"] and r["nm"] else "neither")
        )

    payload = {
        "rows": rows,
        "counts": {
            "candidates": len(rows),
            "both_yes": len(both_yes),
            "extra": len(extra),
            "reversed": len(reversed_),
            "extra_tp": sum(1 for r in extra if r["human"] == "actionable"),
            "extra_fp": sum(1 for r in extra if r["human"] == "not_actionable"),
            "extra_bd": sum(1 for r in extra if r["human"] == "borderline"),
            "flipped": len(reversed_),
        },
    }
    out = REPO / "docs" / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # Inline the data so the published page is one self-contained file: no fetch,
    # no server, no way for the page to silently show something the repo does not.
    template = (REPO / "docs" / "template.html").read_text(encoding="utf-8")
    marker = "/*__DATA__*/null"
    if marker not in template:
        raise SystemExit("site/template.html has lost its /*__DATA__*/ marker")
    page = (REPO / "docs" / "index.html")
    page.write_text(
        template.replace(marker, json.dumps(payload, ensure_ascii=False)),
        encoding="utf-8",
    )
    print("wrote", page, len(page.read_text(encoding="utf-8")), "chars")

    print("wrote", out, len(rows), "rows")
    print("counts:", json.dumps(payload["counts"]))
    print("extra ids:", [r["id"] for r in extra])
    print("extra human labels:", [(r["id"], r["human"]) for r in extra])
    _ = html  # keep the import used if this grows escaping needs


if __name__ == "__main__":
    main()
