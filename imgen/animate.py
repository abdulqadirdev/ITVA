"""Turning an animated SVG scene into video frames.

Chrome is already running and already driven over CDP, so it is the
renderer: it is the only thing on this machine that understands CSS
animation on SVG properly.

Frames are produced by setting the animation clock and screenshotting, not
by letting the animation play and grabbing whatever is on screen. Playing
would make every render slightly different and tie frame accuracy to how
busy the machine is; seeking makes the output identical every time.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from playwright.sync_api import TimeoutError as PWTimeout

from .browser import BrowserSession
from .stage import BACKDROP_URL, Shot, build_html

#: Pause every animation and park the clock at an exact millisecond.
SEEK = """
(ms) => {
  const all = document.getAnimations();
  all.forEach(a => { a.pause(); a.currentTime = ms; });
  return all.length;
}
"""


class AnimateError(RuntimeError):
    pass


#: Frames are JPEG, not PNG. The capture is the whole cost of a render,
#: and PNG-compressing a photographic backdrop 1920x1080 at a time was
#: most of it: 371ms a frame against 151ms, and 682 KB against 148 KB.
#: They are handed straight to x264, so losslessness buys nothing.
FRAME_GLOB = "f*.jpg"
FRAME_PATTERN = "f%05d.jpg"
FRAME_QUALITY = 92


@dataclass
class FrameSet:
    folder: Path
    count: int
    fps: int


def render_frames(
    session: BrowserSession,
    shot: Shot,
    folder: Path,
    fps: int = 24,
    shot_timeout_ms: int = 60_000,
    log: Callable[[str], None] = print,
    should_stop: Callable[[], bool] = lambda: False,
) -> FrameSet:
    """Screenshot the scene once per frame, with the clock set by hand."""
    folder.mkdir(parents=True, exist_ok=True)
    # Both extensions: a folder may hold PNG frames from before the
    # capture format changed, and leaving them wastes several hundred MB
    # a row for files nothing will read.
    for pattern in (FRAME_GLOB, "f*.png"):
        for stale in folder.glob(pattern):
            stale.unlink()

    page = session.context.new_page()
    try:
        page.set_viewport_size({"width": shot.width, "height": shot.height})
        if shot.image is not None:
            # Answer the backdrop URL from disk. Serving it as a normal
            # resource lets Chrome decode it once; inlining it as a data
            # URI made every screenshot five times slower.
            import mimetypes

            kind = mimetypes.guess_type(shot.image.name)[0] or "image/jpeg"
            body = shot.image.read_bytes()
            page.route(
                BACKDROP_URL,
                lambda route: route.fulfill(status=200, content_type=kind, body=body),
            )
        page.set_content(build_html(shot), wait_until="load")

        found = page.evaluate(SEEK, 0)
        if not found:
            raise AnimateError(
                f"the scene declares no animation - is '{shot.action}' empty?"
            )

        total = max(int(round(shot.duration * fps)), 1)
        for index in range(total):
            if should_stop():
                log("  stop requested")
                break
            page.evaluate(SEEK, index * 1000.0 / fps)
            try:
                page.screenshot(
                    path=str(folder / (FRAME_PATTERN % index)),
                    type="jpeg",
                    quality=FRAME_QUALITY,
                    timeout=shot_timeout_ms,
                )
            except PWTimeout as exc:
                raise AnimateError(
                    f"frame {index} of '{shot.action}' took longer than "
                    f"{shot_timeout_ms / 1000:.0f}s to capture. The browser is "
                    "probably busy - a generation run in the same Chrome will "
                    "do it. Try again when it is idle."
                ) from exc
        return FrameSet(folder, total, fps)
    finally:
        page.close()


def frames_to_clip(
    frames: FrameSet,
    destination: Path,
    ffmpeg: Path,
    audio: Path | None = None,
    crf: int = 20,
    preset: str = "medium",
    audio_bitrate: str = "192k",
) -> Path:
    """Assemble the frames, with the narration if there is any."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg), "-y",
        "-framerate", str(frames.fps),
        "-i", str(frames.folder / FRAME_PATTERN),
    ]
    if audio and audio.exists():
        command += ["-i", str(audio)]
    else:
        command += [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]
    command += [
        "-map", "0:v", "-map", "1:a",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-shortest", str(destination),
    ]

    result = subprocess.run(command, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0 or not destination.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise AnimateError(f"assembling the frames failed\n{tail}")
    return destination
