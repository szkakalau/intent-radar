"""Record docs/video.html into an MP4 with a headless browser.

Why this exists
---------------
The demo video is a required submission artefact and it is *also* a claim:
every figure in it has to match the repository. Filming a screen by hand makes
the picture and the repo two separate things that can drift apart. So the video
is a committed HTML file whose numbers are the published ones, and this script
records it — the same way the dashboard is generated from the frozen recordings.

Playwright writes VP8/WebM; ffmpeg transcodes to H.264 MP4 because that is what
Devpost and YouTube accept without an argument.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "docs" / "video.html"
OUT_MP4 = REPO / "docs" / "intentradar-demo.mp4"
WIDTH, HEIGHT = 1280, 720

# Sum of the per-scene durations in docs/video.html, plus a tail so the last
# scene does not get cut mid-sentence.
TOTAL_MS = 177_000 + 3_000


def main() -> int:
    if not SOURCE.exists():
        print(f"missing {SOURCE}", file=sys.stderr)
        return 1

    from playwright.sync_api import sync_playwright

    tmp_webm = OUT_MP4.with_suffix(".webm")
    tmp_webm.unlink(missing_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            record_video_dir=str(tmp_webm.parent),
            record_video_size={"width": WIDTH, "height": HEIGHT},
        )
        page = context.new_page()
        page.goto(SOURCE.as_uri())
        page.wait_for_timeout(TOTAL_MS)
        video = page.video
        context.close()  # flushes the video to disk
        browser.close()
        if video is not None:
            raw = Path(video.path())
            if raw.exists():
                raw.replace(tmp_webm)

    if not tmp_webm.exists():
        print("no video was produced", file=sys.stderr)
        return 1

    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(tmp_webm),
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-an", str(OUT_MP4),
        ],
        check=True,
    )
    tmp_webm.unlink(missing_ok=True)
    print(f"wrote {OUT_MP4} ({OUT_MP4.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
