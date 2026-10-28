"""Styled, animated captions.

SRT can only carry text; everything visual has to be forced on from the
command line. ASS carries the styling with the text, which is what makes
colours, a background box, positioning and per-line animation possible at
all. The .srt is still written alongside for anyone who wants plain
subtitles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .captions import Caption, wrap

#: Caption animations. Each is an ASS override applied to the line.
ANIMATIONS = {
    "none": "",
    "fade": r"{\fad(220,220)}",
    "pop": r"{\fscx60\fscy60\t(0,160,\fscx100\fscy100)\fad(80,160)}",
    "rise": r"{\fad(180,180)\move($cx,$y1,$cx,$y2,0,260)}",
    "typewriter": "",   # handled per word, see build_line
    "highlight": "",    # karaoke, see build_line
}

#: Ready-made looks, so the common cases need no colour fiddling.
PRESETS = {
    "Clean white": dict(primary="#FFFFFF", outline="#000000", box=False, outline_px=3),
    "Yellow subtitle": dict(primary="#FFE14D", outline="#000000", box=False, outline_px=3),
    # With BorderStyle=3 the "outline" value is the box padding, so zero
    # means no box at all - which is what it rendered as.
    "Black box": dict(primary="#FFFFFF", outline="#000000", box=True, outline_px=6),
    "Bold shout": dict(primary="#FFFFFF", outline="#000000", box=False, outline_px=6),
    "Soft grey": dict(primary="#EDEDED", outline="#222222", box=False, outline_px=2),
}

POSITIONS = {"bottom": 2, "middle": 5, "top": 8}


@dataclass
class CaptionStyle:
    font: str = "DejaVu Sans"
    size: int = 48
    primary: str = "#FFFFFF"
    outline: str = "#000000"
    highlight: str = "#FFD400"
    box: bool = False
    outline_px: int = 3
    shadow_px: int = 0
    position: str = "bottom"
    margin_v: int = 70
    bold: bool = True
    animation: str = "fade"
    wrap_at: int = 42
    width: int = 1920
    height: int = 1080

    @classmethod
    def from_preset(cls, name: str, **over) -> "CaptionStyle":
        values = dict(PRESETS.get(name, PRESETS["Clean white"]))
        values.update(over)
        return cls(**values)


def ass_colour(value: str, alpha: int = 0) -> str:
    """#RRGGBB to ASS &HAABBGGRR - note the reversed byte order."""
    text = (value or "#FFFFFF").lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    try:
        r, g, b = (int(text[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        r, g, b = 255, 255, 255
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def ass_time(seconds: float) -> str:
    """ASS wants H:MM:SS.cc, with centiseconds."""
    seconds = max(seconds, 0.0)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours)}:{int(minutes):02d}:{secs:05.2f}"


def _split_words(text: str) -> list[str]:
    return [w for w in re.split(r"(\s+)", text) if w.strip()]


def build_line(caption: Caption, style: CaptionStyle) -> str:
    """The text of one Dialogue event, including its animation."""
    body = wrap(caption.text, style.wrap_at).replace("\n", r"\N")
    duration = max(caption.end - caption.start, 0.1)

    if style.animation == "highlight":
        # Exact timings when the engine reported them; otherwise each word's
        # share is estimated from its length, which follows the voice closely
        # but is not frame-accurate.
        if caption.words:
            parts, cursor = [], 0.0
            for start, word_duration, word in caption.words:
                gap = max(start - cursor, 0.0)
                if gap > 0.01:
                    parts.append(rf"{{\kf{max(int(round(gap * 100)), 1)}}}")
                parts.append(rf"{{\kf{max(int(round(word_duration * 100)), 1)}}}{word} ")
                cursor = start + word_duration
            return "".join(parts).strip()

        words = _split_words(caption.text)
        if words:
            total = sum(len(w) for w in words) or 1
            parts = []
            for word in words:
                centis = max(int(round(duration * 100 * len(word) / total)), 1)
                parts.append(rf"{{\kf{centis}}}{word} ")
            return "".join(parts).strip()

    if style.animation == "typewriter":
        words = _split_words(caption.text)
        if words:
            total = sum(len(w) for w in words) or 1
            elapsed, parts = 0.0, []
            for word in words:
                share = duration * len(word) / total
                parts.append(rf"{{\alpha&HFF&\t({int(elapsed * 1000)},"
                             rf"{int((elapsed + share * 0.4) * 1000)},\alpha&H00&)}}{word} ")
                elapsed += share
            return "".join(parts).strip()

    override = ANIMATIONS.get(style.animation, "")
    if "$cx" in override:
        centre = style.width // 2
        base = style.height - style.margin_v
        override = override.replace("$cx", str(centre))
        override = override.replace("$y1", str(base + 40)).replace("$y2", str(base))
    return f"{override}{body}"


@dataclass
class AssDocument:
    style: CaptionStyle
    captions: list[Caption] = field(default_factory=list)

    def render(self) -> str:
        s = self.style
        border_style = 3 if s.box else 1
        # A box with no padding is invisible; keep it meaningful.
        outline_px = max(s.outline_px, 4) if s.box else s.outline_px
        back = ass_colour("#000000", alpha=0x40 if s.box else 0x80)

        header = [
            "[Script Info]",
            "ScriptType: v4.00+",
            "WrapStyle: 0",
            "ScaledBorderAndShadow: yes",
            f"PlayResX: {s.width}",
            f"PlayResY: {s.height}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
            "Style: Default,{font},{size},{primary},{second},{outline},{back},"
            "{bold},0,0,0,100,100,0,0,{border},{out_px},{shadow},{align},60,60,{mv},1".format(
                font=s.font,
                size=s.size,
                primary=ass_colour(s.primary),
                # SecondaryColour is what karaoke reveals from.
                second=ass_colour(s.highlight),
                outline=ass_colour(s.outline),
                back=back,
                bold=-1 if s.bold else 0,
                border=border_style,
                out_px=outline_px,
                shadow=s.shadow_px,
                align=POSITIONS.get(s.position, 2),
                mv=s.margin_v,
            ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        ]

        events = [
            f"Dialogue: 0,{ass_time(c.start)},{ass_time(c.end)},Default,,0,0,0,,"
            f"{build_line(c, s)}"
            for c in self.captions
            if c.text.strip()
        ]
        return "\n".join(header + events) + "\n"


def write_ass(captions: list[Caption], destination, style: CaptionStyle):
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(AssDocument(style, captions).render(), encoding="utf-8")
    return destination
