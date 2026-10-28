"""Composing a shot: background, character, action.

The whole scene is one HTML page. Animations are declared paused, so the
renderer can set the clock to an exact time and screenshot - the same frame
comes out every run, which a live-playing animation could never guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .actions import Action, ambient_for, get as get_action
from .rig import Look, Rig, base_css

#: Flat backdrops, so a shot needs no artwork to exist.
BACKDROPS: dict[str, str] = {
    "plain": "#E9E6EF",
    "bedroom": "linear-gradient(#2B2A3D 0%, #3C3A52 62%, #4A4660 100%)",
    "night": "linear-gradient(#171A2B 0%, #232845 100%)",
    "cave": "linear-gradient(#211C1A 0%, #3A2E27 70%, #4A3A30 100%)",
    "day": "linear-gradient(#BFD9EE 0%, #E6EFF6 100%)",
    "warm": "linear-gradient(#F3DFC8 0%, #E8C9A6 100%)",
}

#: How much of the figure is in frame. Scaling the whole stage pushed the
#: feet out of shot; cropping the viewBox is what a camera actually does,
#: and every crop keeps 16:9 so nothing is letterboxed.
FRAMINGS: dict[str, tuple[int, int, int, int]] = {
    #        x     y     w     h
    "wide":  (0,    0, 1920, 1080),   # whole stage, figure small
    # The figure spans y 236..960, so head-to-feet needs ~820 of height;
    # at 16:9 that forces 1458 of width, most of it empty. Unavoidable for
    # a standing figure in a widescreen frame.
    "full":  (231, 176, 1458,  820),  # head to feet
    "medium": (584, 196,  752,  423),  # head and torso - the explainer shot
    "close": (740, 250,  440,  248),  # face
}


#: Slow camera moves, applied to the whole stage.
CAMERAS: dict[str, str] = {
    "none": "",
    "push-in": """
@keyframes cam { 0% { transform: scale(1); } 100% { transform: scale(1.09); } }
#stage { animation: cam var(--dur) linear both; transform-origin: 50% 46%; }
""",
    "pull-out": """
@keyframes cam { 0% { transform: scale(1.09); } 100% { transform: scale(1); } }
#stage { animation: cam var(--dur) linear both; transform-origin: 50% 46%; }
""",
    "drift-left": """
@keyframes cam { 0% { transform: scale(1.06) translateX(1.4%); }
                 100% { transform: scale(1.06) translateX(-1.4%); } }
#stage { animation: cam var(--dur) linear both; }
""",
}


@dataclass
class Shot:
    action: str = "idle"
    backdrop: str = "plain"
    camera: str = "none"
    duration: float = 3.0
    #: An image used as the backdrop instead of a flat colour - a generated
    #: picture behind a vector character.
    image: Path | None = None
    look: Look = field(default_factory=Look)
    width: int = 1920
    height: int = 1080
    #: Vertical placement, so the figure can stand lower in a wide shot.
    offset_y: int = 0
    #: wide | full | medium | close
    framing: str = "full"


#: Where a Shot's image is served from. render_frames routes it.
BACKDROP_URL = "https://backdrop.invalid/picture"


class StageError(ValueError):
    pass


def resolve_action(name: str) -> Action:
    found = get_action(name)
    if found is None:
        from .actions import names

        raise StageError(
            f"unknown action {name!r}. Known: {', '.join(names())}"
        )
    return found


def build_html(shot: Shot) -> str:
    action = resolve_action(shot.action)
    # Ambient life in whatever the action does not drive itself.
    action_css = action.css + ambient_for(action)
    # The rig is drawn in its own fixed 1920x1080 space; only the <svg>
    # element's size changes. Passing the output size here made the viewBox
    # match the output instead, so the figure rendered at raw user units and
    # sat entirely outside the frame.
    rig = Rig(look=shot.look)

    if shot.image is not None:
        # A fixed URL that the renderer intercepts and answers with the
        # file's bytes. Two earlier attempts were wrong: a file:// link
        # never loads, because set_content leaves the document on
        # about:blank; and inlining the picture as a base64 data URI loads
        # but costs five times as much per screenshot - 1077ms a frame
        # against 207ms - because the whole JPEG is re-decoded on every
        # capture. Served as an ordinary resource it is decoded once.
        backdrop = (
            f"background-image:url('{BACKDROP_URL}')"
            ";background-size:cover;background-position:center"
        )
    else:
        value = BACKDROPS.get(shot.backdrop, BACKDROPS["plain"])
        key = "background-image" if value.startswith("linear-gradient") else "background-color"
        backdrop = f"{key}:{value}"

    camera = CAMERAS.get(shot.camera, "")
    box = FRAMINGS.get(shot.framing, FRAMINGS["full"])
    view = " ".join(str(n) for n in box)
    # A looping action fills whatever time the line needs; a one-shot plays
    # once and holds, so it must be told how long the shot is.
    timing = "" if action.loops else f"* {{ --dur: {shot.duration}s; }}"

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
  html,body {{ margin:0; padding:0; width:{shot.width}px; height:{shot.height}px;
               overflow:hidden; }}
  body {{ {backdrop}; }}
  #stage {{ width:{shot.width}px; height:{shot.height}px; }}
  #figure {{ transform: translateY({shot.offset_y}px); }}
  /* Nothing may play on its own: the renderer sets the clock per frame. */
  * {{ animation-play-state: paused !important; }}
  {timing}
  {base_css()}
  {camera}
  {action_css}
</style></head>
<body>
  <div id="stage">
    <svg id="figure" xmlns="http://www.w3.org/2000/svg"
         viewBox="{view}"
         width="{shot.width}" height="{shot.height}">
      {rig.svg()}
    </svg>
  </div>
</body></html>"""
