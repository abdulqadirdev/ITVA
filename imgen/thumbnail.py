"""Laying the words over a generated thumbnail picture.

Composited in the browser rather than with ffmpeg's drawtext. drawtext
cannot wrap a line, centres badly once it does, and its outline is a crude
border rather than a stroke around the glyphs - which is most of what makes
a thumbnail readable at the size YouTube actually shows it. A page gives
real wrapping, `paint-order: stroke` so the outline sits behind the letters
instead of eating into them, and a drop shadow, for a few lines of CSS.

The picture is served from a routed URL, not inlined and not linked: a
scene handed to the page with set_content sits on about:blank, so a file://
reference never loads at all.
"""

from __future__ import annotations

import html
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from .browser import BrowserSession
from .config import ThumbnailConfig

#: Marks the one queue job that produces the thumbnail picture. It is a
#: real job so it gets Flow submission, retries, claim-before-generate and
#: resume for free - but it must never become a clip, so the storyboard
#: filters it out by this key.
JOB_MARKER = "__thumbnail__"

#: Where the picture is served from while the page is being captured.
PICTURE_URL = "https://thumbnail.invalid/picture"

#: JPEG, like the animation frames: a thumbnail is a photograph, and PNG
#: costs several times as much to encode for no visible gain.
QUALITY = 92

ALIGN = {"left": "flex-start", "centre": "center", "right": "flex-end"}
POSITION = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}
TEXT_ALIGN = {"left": "left", "centre": "center", "right": "right"}


#: No block may grow past this share of the frame height. A size is only
#: ever a guess until the text is in the real font, and the failure mode
#: is spectacular - a 37-character line at 108px wrapped to four lines and
#: covered the picture completely.
MAX_BLOCK_HEIGHT = 0.62

#: Shrinks any block taller than its share, using the browser's own
#: metrics rather than an estimated character width.
FIT = """
(limit) => {
  const shrunk = [];
  document.querySelectorAll('[id^=block]').forEach(el => {
    let size = parseFloat(getComputedStyle(el).fontSize);
    const started = size;
    let guard = 60;
    while (el.getBoundingClientRect().height > limit && size > 16 && guard-- > 0) {
      size = Math.floor(size * 0.94);
      el.style.fontSize = size + 'px';
    }
    if (size !== started) shrunk.push([el.id, started, size]);
  });
  return shrunk;
}
"""


class ThumbnailError(RuntimeError):
    """The thumbnail could not be built."""


@dataclass
class Thumbnail:
    path: Path
    width: int
    height: int


def _block_css(index: int, b) -> str:
    """One absolutely-placed layer per block, so blocks may overlap."""
    shadow = "text-shadow: 0 6px 18px rgba(0,0,0,.55);" if b.shadow else ""
    stroke = (
        f"-webkit-text-stroke:{b.outline_px}px {b.outline}; paint-order: stroke fill;"
        if b.outline_px > 0
        else ""
    )
    slab = (
        f"background:{b.box}; padding:{b.box_pad}px {round(b.box_pad * 1.6)}px;"
        if b.box
        else ""
    )
    transform = f"translate({b.dx}px,{b.dy}px) rotate({b.rotate}deg)"
    return f"""
  #layer{index} {{
    position:absolute; inset:0; display:flex; box-sizing:border-box;
    padding:{{pad}}px;
    align-items:{POSITION.get(b.position, "center")};
    justify-content:{ALIGN.get(b.align, "center")};
    pointer-events:none;
  }}
  #block{index} {{
    max-width:{round(b.text_width * 100)}%;
    font-family:'{b.font}', 'Arial Black', 'Fira Sans Compressed Heavy',
                'DejaVu Sans', sans-serif;
    font-size:{b.size}px;
    font-weight:{b.weight};
    font-style:{"italic" if b.italic else "normal"};
    line-height:{b.line_height};
    letter-spacing:{b.tracking}em;
    color:{b.colour};
    text-align:{TEXT_ALIGN.get(b.align, "center")};
    transform:{transform};
    {slab}
    {stroke}
    {shadow}
  }}
"""


def build_html(picture: Path, spec: ThumbnailConfig) -> str:
    """The page that is screenshotted: one picture, any number of blocks."""
    pad = round(spec.height * 0.05)
    styles, bodies = [], []
    for index, block in enumerate(spec.blocks):
        words = block.text.upper() if block.uppercase else block.text
        lines = "<br>".join(
            html.escape(part) for part in words.splitlines() if part.strip()
        )
        if not lines:
            continue
        styles.append(_block_css(index, block).replace("{pad}", str(pad)))
        bodies.append(
            f'<div id="layer{index}"><div id="block{index}">{lines}</div></div>'
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; width:{spec.width}px; height:{spec.height}px;
               overflow:hidden; position:relative; }}
  body {{ background-image:url('{PICTURE_URL}');
          background-size:cover; background-position:center; }}
{"".join(styles)}
</style></head>
<body>{"".join(bodies)}</body></html>
"""


def render(
    session: BrowserSession,
    picture: Path,
    spec: ThumbnailConfig,
    destination: Path,
    timeout_ms: int = 60_000,
    log=lambda message: None,
) -> Thumbnail:
    """Screenshot the composed page to `destination`."""
    if not picture.exists():
        raise ThumbnailError(f"the thumbnail picture is missing ({picture})")
    if not any(b.text.strip() for b in spec.blocks):
        raise ThumbnailError("there is no thumbnail text to lay over the picture")

    destination.parent.mkdir(parents=True, exist_ok=True)
    kind = mimetypes.guess_type(picture.name)[0] or "image/jpeg"
    body = picture.read_bytes()

    page = session.context.new_page()
    try:
        page.set_viewport_size({"width": spec.width, "height": spec.height})
        page.route(
            PICTURE_URL,
            lambda route: route.fulfill(status=200, content_type=kind, body=body),
        )
        page.set_content(build_html(picture, spec), wait_until="load")
        for block_id, was, now in page.evaluate(
            FIT, spec.height * MAX_BLOCK_HEIGHT
        ):
            log(f"  {block_id}: {was:.0f}px would not fit, used {now}px")
        page.screenshot(
            path=str(destination), type="jpeg", quality=QUALITY, timeout=timeout_ms
        )
    finally:
        page.close()
    return Thumbnail(destination, spec.width, spec.height)


def description_in(rows, column: str) -> str:
    """The thumbnail's description, from the first row that carries one.

    One thumbnail per project, so the first filled cell wins and the rest
    of the column is ignored rather than quietly producing several.
    """
    folded = column.casefold()
    for row in rows:
        fields = {k.casefold(): v for k, v in (getattr(row, "fields", None) or {}).items()}
        value = str(fields.get(folded, "") or "").strip()
        if value:
            return value
    return ""


def is_thumbnail_job(job) -> bool:
    return JOB_MARKER in (getattr(job, "fields", None) or {})


def ensure_job(store, description: str) -> bool:
    """Queue the thumbnail's picture, if the sheet describes one.

    Deduplicated like any other prompt, so calling this on every refresh
    does not pile up jobs; changing the description queues a new one and
    leaves the old picture in place rather than deleting work.
    """
    from .sheets import PromptRow

    text = (description or "").strip()
    if not text:
        return False
    store.sync_prompts([PromptRow(0, text, {JOB_MARKER: "1"})])
    return True


# ---- suggesting the words from the sheet --------------------------------

#: Words that make a thumbnail line worth shouting. Not an attempt at
#: copywriting - the suggestion only ever picks lines the script already
#: contains, so it cannot invent a claim the video does not make.
PUNCHY = (
    "deadliest", "most", "never", "every", "nobody", "worst", "first",
    "only", "single", "half", "million", "billion", "thousand", "years",
    "killed", "kills", "danger", "dangerous", "truth", "real", "secret",
    "actually", "still", "why", "how",
)

#: Scenes whose name says the line is doing the selling.
LOUD_SCENES = ("hook", "claim", "kicker", "twist")


def _headline_score(line: str, scene: str) -> float:
    words = line.split()
    if not words or len(words) < 3:
        return -1.0
    score = 0.0
    length = len(line)
    # 18-44 characters is roughly one readable line at thumbnail size.
    score += 3.0 if 18 <= length <= 44 else (1.0 if length <= 60 else -2.0)
    # A line starting lowercase is the tail of the one before it, and a
    # trailing comma says the thought is unfinished - both read as broken
    # on their own.
    score += 1.5 if line[:1].isupper() else -3.0
    score -= 3.0 if line.rstrip().endswith(",") else 0.0
    lowered = line.casefold()
    score += 1.2 * sum(1 for w in PUNCHY if w in lowered)
    score += 1.0 if any(c.isdigit() for c in line) else 0.0
    score += 2.0 if any(s in (scene or "").casefold() for s in LOUD_SCENES) else 0.0
    return score


def _two_lines(text: str) -> str:
    """Break a headline near the middle, on a word boundary."""
    words = text.split()
    if len(words) < 4:
        return text
    target = len(text) / 2
    best, run = 1, 0
    for index, word in enumerate(words[:-1], start=1):
        run += len(word) + 1
        if abs(run - target) < abs(
            sum(len(w) + 1 for w in words[:best]) - target
        ):
            best = index
    return " ".join(words[:best]) + "\n" + " ".join(words[best:])


#: Roughly how wide one character of a heavy display face is, as a
#: fraction of the font size, with headroom. Measured against Arial Black:
#: 0.62 is the true average and left the longest line landing exactly on
#: the wrap boundary, so it wrapped anyway.
CHAR_WIDTH = 0.68


def fit_size(text: str, width: int, text_width: float, largest: int = 112) -> int:
    """The biggest size at which the longest line still fits on one line.

    Picking a fixed size and hoping is what turned a 37-character
    suggestion into four stacked lines covering the whole picture. The
    browser wraps at max-width, so the size has to come from the text.
    """
    longest = max((len(line) for line in text.splitlines() if line.strip()), default=0)
    if longest == 0:
        return largest
    room = width * text_width
    return max(28, min(largest, int(room / (longest * CHAR_WIDTH))))


def suggest_blocks(rows, scene_column: str, width: int = 1280):
    """Propose thumbnail words from what the sheet already says.

    It chooses lines, it does not write them: there is no model here, so
    inventing a claim would mean inventing it wrongly. The best-scoring
    script line becomes the shout, the next best short one the remark, and
    the scene it came from the little badge.
    """
    from .config import TextBlock
    from .platforms import display_font
    from .produce import script_of

    folded_scene = scene_column.casefold()

    scored = []
    for row in rows:
        fields = {k.casefold(): v for k, v in (getattr(row, "fields", None) or {}).items()}
        # Reuse the storyboard's own idea of where the words live: it
        # already tries "script line", "script", "narration" and the rest.
        line = (script_of(row) or "").strip()
        scene = str(fields.get(folded_scene, "") or "").strip()
        if not line:
            continue
        scored.append((_headline_score(line, scene), line, scene))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return ()

    _, headline, _scene = scored[0]
    remark = next(
        (line for score, line, _ in scored[1:] if score > 0 and len(line) <= 52),
        "",
    )

    # Deliberately no badge from the scene name. Scene names here are
    # production labels - "Time jump", "Blood rewritten" - and putting one
    # on a thumbnail reads as a mistake. A badge is one click to add, with
    # wording the viewer is meant to see.
    shout = _two_lines(headline.rstrip("."))
    size = fit_size(shout, width, 0.86)
    blocks = [
        TextBlock(text=shout, font=display_font(), size=size, colour="#FFFFFF",
                  outline="#101010", outline_px=max(6, round(size * 0.12)),
                  align="centre", position="middle", dy=-10),
    ]
    if remark:
        blocks.append(
            TextBlock(text=remark.rstrip("."), font=display_font((
                          "Fira Sans Condensed", "Liberation Sans Narrow",
                          "DejaVu Sans Condensed", "DejaVu Sans")),
                      size=fit_size(remark, width, 0.86, largest=44),
                      weight=700, colour="#FFD400", outline="#101010",
                      outline_px=6, uppercase=False, align="centre",
                      position="bottom", dy=-8)
        )
    return tuple(blocks)
