#!/usr/bin/env python
"""Mute picture + narration + captions -> the shipped demo video.

Why this exists
---------------
The video is generated in three steps, and this is the third:

    python scripts/demo/narrate.py       # voice clips + caption timings
    python scripts/demo/record_video.py  # silent picture into build/silent.mp4
    python scripts/demo/assemble.py      # -> docs/intentradar-demo.mp4

Assembling by hand in a shell is fine once and wrong forever: the exact
``adelay`` offsets, the subtitle style and the encode flags end up living in
somebody's scrollback, which means the shipped video can no longer be
reproduced from the repository. This script is where that recipe lives.

The offsets come from ``timing.json``, which ``narrate.py`` derives from the
``data-ms`` of each scene in ``docs/video.html``. So the voice is placed on the
picture's timeline rather than on one of its own — and if a clip were longer
than its scene, ``narrate.py`` would have refused to write it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PICTURE = REPO / "build" / "silent.mp4"
NARRATION_DIR = REPO / "build" / "narration"
DEFAULT_OUT = REPO / "docs" / "intentradar-demo.mp4"

# Captions are burned in rather than offered as a soft track: a soft track is
# one click that most viewers will not make, and a muted autoplay with no
# captions is a video with no content.
SUBTITLE_STYLE = (
    "FontName=Segoe UI,FontSize=26,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00101010,"
    "Outline=2,Shadow=1,Alignment=2,MarginV=44"
)


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _filter_path(path: Path) -> str:
    """A path libass will accept inside a filter graph.

    A Windows drive letter is filter-graph syntax — "C:/..." is read as a
    filter named C — so the graph is built from paths relative to the repo and
    ffmpeg is run with the repo as its working directory. Escaping the colon
    also fails: libass then sees a literal backslash in the filename.
    """
    return path.resolve().relative_to(REPO).as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--picture", type=Path, default=DEFAULT_PICTURE,
                        help="silent recording from record_video.py")
    parser.add_argument("--narration-dir", type=Path, default=NARRATION_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    timing = args.narration_dir / "timing.json"
    subs = args.narration_dir / "demo.srt"
    for path in (args.picture, timing, subs):
        if not path.exists():
            print(f"missing {path} — run narrate.py and record_video.py first",
                  file=sys.stderr)
            return 1

    scenes = json.loads(timing.read_text(encoding="utf-8"))
    # Relative paths throughout, because a drive letter cannot survive the
    # filter graph (see _filter_path).
    rel = lambda p: p.resolve().relative_to(REPO).as_posix()  # noqa: E731
    inputs: list[str] = ["-i", rel(args.picture)]
    labels: list[str] = []
    for scene in scenes:
        clip = args.narration_dir / f"scene{scene['index']:02d}.mp3"
        if not clip.exists():
            print(f"missing {clip}", file=sys.stderr)
            return 1
        inputs += ["-i", rel(clip)]
        # +1 because input 0 is the picture.
        n = scene["index"] + 1
        labels.append(
            f"[{n}:a]adelay={scene['start_ms']}|{scene['start_ms']}[a{scene['index']}]"
        )

    chain = ";".join(labels)
    mixed = "".join(f"[a{s['index']}]" for s in scenes)
    # No apad: the narration is shorter than the picture and padding it to the
    # full length with silence is what `-t` already does at the output stage.
    filt = (
        f"{chain};"
        f"{mixed}amix=inputs={len(scenes)}:normalize=0:dropout_transition=0[voice];"
        f"[0:v]subtitles={_filter_path(subs)}:force_style='{SUBTITLE_STYLE}'[v]"
    )

    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", filt,
           "-map", "[v]", "-map", "[voice]",
           "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k",
           "-t", f"{_duration(args.picture):.2f}", rel(args.out)]
    done = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stderr.strip() or "(ffmpeg produced no message)", file=sys.stderr)
        return done.returncode

    print(f"wrote {args.out} ({args.out.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
