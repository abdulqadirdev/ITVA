"""Subtitles.

An .srt is always written, whether or not captions are burned in. Burning is
destructive — the text is pixels afterwards — so keeping the sidecar means
the wording can still be corrected, translated, or uploaded separately.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str
    #: (start, duration, word) relative to this caption, when the speech
    #: engine reported them. Empty means timings must be estimated.
    words: tuple = ()


def timestamp(seconds: float) -> str:
    """SRT wants HH:MM:SS,mmm."""
    seconds = max(seconds, 0.0)
    whole = int(seconds)
    ms = int(round((seconds - whole) * 1000))
    if ms == 1000:  # rounding can tip a whole second
        whole, ms = whole + 1, 0
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d},{ms:03d}"


def wrap(text: str, width: int = 42) -> str:
    """Two short lines read far better on screen than one long one."""
    text = " ".join((text or "").split())
    if len(text) <= width:
        return text
    return "\n".join(textwrap.wrap(text, width=width, max_lines=3, placeholder="..."))


def build_srt(captions: list[Caption], width: int = 42) -> str:
    blocks = []
    for index, caption in enumerate(captions, start=1):
        body = wrap(caption.text, width)
        if not body:
            continue
        blocks.append(
            f"{index}\n{timestamp(caption.start)} --> {timestamp(caption.end)}\n{body}\n"
        )
    return "\n".join(blocks)


def write_srt(captions: list[Caption], destination: Path, width: int = 42) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(build_srt(captions, width), encoding="utf-8")
    return destination


def force_style(
    font: str = "DejaVu Sans",
    size: int = 22,
    outline: int = 2,
    margin_v: int = 40,
) -> str:
    """Style string for ffmpeg's subtitles filter.

    Colours are ASS BGR with an alpha byte: &HAABBGGRR. White text with a
    solid black outline is the combination that stays readable over any
    image, which matters when the background is whatever Flow produced.
    """
    return (
        f"FontName={font},FontSize={size},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline={outline},Shadow=0,"
        f"Alignment=2,MarginV={margin_v}"
    )
