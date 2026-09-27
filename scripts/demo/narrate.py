#!/usr/bin/env python
"""Narration and subtitles for the demo video, generated from one source text.

Why this exists
---------------
A muted autoplay is how most people meet a submission video, so the video
needs a voiceover and needs captions. Both are generated here rather than
recorded and hand-timed, for the same reason the whole video is generated from
``docs/video.html``: hand-typed captions drift from the audio the moment
anything changes, and a project that sells "you can recompute our numbers"
cannot ship a caption track nobody can check.

Two things here are deliberately not guessed:

* **Subtitle timings come from the TTS engine's own sentence boundaries**, not
  from a human tapping a keyboard. Cue N starts exactly when the voice starts
  sentence N. (edge-tts 7.x emits ``SentenceBoundary``, not ``WordBoundary`` —
  feeding the latter yields an empty track.)
* **The voice is placed on the picture's timeline, not its own.** The per-scene
  durations are read out of ``docs/video.html`` and each narration clip is
  offset to its scene's start. The script then refuses to finish if a clip is
  longer than the scene it belongs to, because a voice that overruns into the
  next slide is worse than no voice at all.

Usage
-----
    python scripts/demo/narrate.py            # write audio + subs + timing.json
    python scripts/demo/narrate.py --dry-run  # print the timeline, write nothing
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import edge_tts

REPO = Path(__file__).resolve().parents[2]
VIDEO_HTML = REPO / "docs" / "video.html"
OUT_DIR = REPO / "build" / "narration"
TIMING = OUT_DIR / "timing.json"
SUBS = OUT_DIR / "demo.srt"

VOICE = "en-US-AndrewNeural"
RATE = "+0%"

# A caption line longer than this gets wrapped rather than shrunk to fit.
WRAP_AT = 44

# edge-tts reports offsets in 100-nanosecond ticks.
TICKS_PER_MS = 10_000

# One entry per <section class="scene"> in docs/video.html, in document order.
# The text is spoken AND captioned; numbers are digits because the engine reads
# "26.3 percent" correctly from digits, and a caption that spelled it out would
# read like a transcription error.
NARRATION: list[str] = [
    # 00 — title
    "IntentRadar watches Reddit for people asking for what you sell. "
    "That part is not new. What is new is the second half: it publishes how "
    "often it is wrong, and lets anyone recompute the number.",
    # 01 — two commands
    "Two commands. No API key, no network. The first replays a frozen "
    "recording of the model's judgements. The second scores them against two "
    "hundred and eleven hand-labelled posts.",
    # 02 — the funnel
    "Two stages. A cheap rule-based net pulls in 42 candidates at 26.3 percent "
    "precision. Then one careful model gate. With DeepSeek, precision is 100 "
    "percent on nine hits, recall 90 percent. With Nemotron, precision 62.5 "
    "percent, recall 100 percent.",
    # 03 — strict superset
    "Here is what we did not expect. Nemotron's hit set is a strict superset: "
    "every post DeepSeek accepted, Nemotron accepted too, plus eight more, and "
    "nothing was reversed. Of those eight, one is the buyer DeepSeek missed. "
    "Six are confirmed false positives. One is borderline, and we do not count "
    "it either way.",
    # 04 — fine print
    "Ten real buyers is a small number, so every figure carries a 95 percent "
    "interval. It did not run on Nebius: OpenRouter served it, and we fail "
    "that clause of the rules. Five of the 42 calls hit a 503 on the first "
    "attempt — and that count is the one figure here no test covers.",
    # 05 — check it yourself
    "MIT licensed. 348 labelled rows, 312 with a usable verdict. Clone it and "
    "run the suite yourself.",
]


def scene_durations() -> list[int]:
    """The ``data-ms`` of every scene, in document order."""
    html = VIDEO_HTML.read_text(encoding="utf-8")
    found = [int(m) for m in re.findall(r'class="scene"\s+data-ms="(\d+)"', html)]
    if not found:
        raise SystemExit(f"no scene timings found in {VIDEO_HTML}")
    return found


def _duration_ms(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return int(round(float(out.stdout.strip()) * 1000))


def _stamp(total_ms: int) -> str:
    ms = max(0, total_ms)
    return (f"{ms // 3600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},"
            f"{ms % 1000:03d}")


def _wrap(text: str) -> str:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > WRAP_AT:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return "\n".join(lines)


def _cues(sentences: list[dict], offset_ms: int) -> list[str]:
    out: list[str] = []
    for n, s in enumerate(sentences, start=1):
        start = offset_ms + s["start_ms"]
        end = offset_ms + s["start_ms"] + s["duration_ms"]
        out.append(f"{n}\n{_stamp(start)} --> {_stamp(end)}\n{_wrap(s['text'])}\n")
    return out


async def _synthesise(text: str, mp3: Path) -> list[dict]:
    """Write ``mp3`` and return its sentence boundaries in milliseconds."""
    communicate = edge_tts.Communicate(text, VOICE, rate=RATE)
    sentences: list[dict] = []
    with mp3.open("wb") as fh:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "SentenceBoundary":
                sentences.append({
                    "text": chunk["text"].strip(),
                    "start_ms": chunk["offset"] // TICKS_PER_MS,
                    "duration_ms": chunk["duration"] // TICKS_PER_MS,
                })
    return sentences


async def build(write: bool) -> tuple[list[dict], list[str]]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    durations = scene_durations()
    if len(durations) != len(NARRATION):
        raise SystemExit(
            f"{VIDEO_HTML.name} has {len(durations)} scenes but there are "
            f"{len(NARRATION)} narration blocks — they must match"
        )

    scenes: list[dict] = []
    cues: list[str] = []
    offset = 0
    next_cue = 1

    for index, (text, scene_ms) in enumerate(zip(NARRATION, durations, strict=True)):
        mp3 = OUT_DIR / f"scene{index:02d}.mp3"
        if write:
            sentences = await _synthesise(text, mp3)
            audio_ms = _duration_ms(mp3)
            for cue in _cues(sentences, offset):
                body = cue.split("\n", 1)[1]
                cues.append(f"{next_cue}\n{body}")
                next_cue += 1
        else:
            audio_ms = int(len(text.split()) / 155 * 60_000)

        scenes.append({
            "index": index,
            "start_ms": offset,
            "audio_ms": audio_ms,
            "scene_ms": scene_ms,
            "slack_ms": scene_ms - audio_ms,
            "words": len(text.split()),
        })
        offset += scene_ms

    return scenes, cues


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="estimate the timeline without calling the network")
    args = parser.parse_args()

    scenes, cues = asyncio.run(build(write=not args.dry_run))
    total = sum(s["scene_ms"] for s in scenes)

    print(f"{'#':>2}  {'start':>7}  {'audio':>7}  {'scene':>7}  {'slack':>7}  words")
    overrun: list[int] = []
    for s in scenes:
        flag = "" if s["slack_ms"] >= 0 else "  ← OVERRUN"
        if s["slack_ms"] < 0:
            overrun.append(s["index"])
        print(f"{s['index']:>2}  {s['start_ms']:>7}  {s['audio_ms']:>7}  "
              f"{s['scene_ms']:>7}  {s['slack_ms']:>7}  {s['words']}{flag}")
    print(f"total {total} ms ({total / 1000:.1f}s)")

    if overrun:
        print(f"\nnarration overruns scene(s) {overrun}: the voice would spill "
              "into the next slide. Lengthen those scenes in docs/video.html "
              "or shorten the script.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    SUBS.write_text("\n".join(cues).strip() + "\n", encoding="utf-8")
    TIMING.write_text(json.dumps(scenes, indent=2), encoding="utf-8")
    print(f"wrote {SUBS} ({len(cues)} cues)")
    print(f"wrote {TIMING}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
