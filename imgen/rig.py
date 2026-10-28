"""A posable stick figure, drawn as SVG.

The point of a rig is consistency. Generating a fresh drawing for every row
would give 152 slightly different characters; defining the character once
and only changing its pose gives the same person throughout.

This style was chosen because it is describable rather than drawn - "plain
round white head, black dot eyes, thin stick limbs" is a handful of shapes.
A realistic style could not be rigged this way.

Every moving part carries an id and an explicit transform-box/origin, so an
animation can rotate an arm about its shoulder instead of the page corner.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Parts an action is allowed to move. Anything else is a typo.
PARTS = (
    "root", "body", "head", "hair", "eye-l", "eye-r", "brow-l", "brow-r",
    "mouth", "torso", "arm-l", "arm-r", "leg-l", "leg-r",
)


@dataclass(frozen=True)
class Look:
    """How the character is coloured. Muted pastels, per the house style."""

    skin: str = "#F7F3EE"
    line: str = "#2B2B33"
    hair: str = "#33303A"
    shirt: str = "#A9B5D1"
    trousers: str = "#6E7490"
    line_width: float = 5.0


@dataclass(frozen=True)
class Rig:
    look: Look = field(default_factory=Look)
    width: int = 1920
    height: int = 1080

    def svg(self) -> str:
        """The character, centred, in a 1920x1080 stage."""
        c = self.look
        w = c.line_width
        stroke = f'stroke="{c.line}" stroke-width="{w}" stroke-linecap="round"'

        # Joints are placed so the limb pivots where a shoulder or hip is,
        # not at the shape's own corner.
        return f'''
<g id="root" class="rig">
  <g id="body">
    <g id="leg-l" style="transform-origin:960px 700px">
      <path d="M960 700 L905 865" {stroke} fill="none"/>
      <path d="M905 865 L880 960" {stroke} fill="none"/>
    </g>
    <g id="leg-r" style="transform-origin:960px 700px">
      <path d="M960 700 L1015 865" {stroke} fill="none"/>
      <path d="M1015 865 L1040 960" {stroke} fill="none"/>
    </g>

    <g id="torso">
      <path d="M900 470 Q960 448 1020 470 L1036 700 Q960 718 884 700 Z"
            fill="{c.shirt}" stroke="{c.line}" stroke-width="{w}"
            stroke-linejoin="round"/>
    </g>

    <g id="arm-l" style="transform-origin:905px 492px">
      <path d="M905 492 L852 626" {stroke} fill="none"/>
      <path d="M852 626 L838 722" {stroke} fill="none"/>
    </g>
    <g id="arm-r" style="transform-origin:1015px 492px">
      <path d="M1015 492 L1068 626" {stroke} fill="none"/>
      <path d="M1068 626 L1082 722" {stroke} fill="none"/>
    </g>

    <g id="head" style="transform-origin:960px 356px">
      <circle cx="960" cy="356" r="104"
              fill="{c.skin}" stroke="{c.line}" stroke-width="{w}"/>
      <g id="hair">
        <path d="M858 330 Q884 232 960 236 Q1040 232 1062 330
                 Q1020 286 960 290 Q900 286 858 330 Z"
              fill="{c.hair}" stroke="{c.line}" stroke-width="{w}"
              stroke-linejoin="round"/>
      </g>
      <g id="brow-l" style="transform-origin:920px 318px">
        <path d="M900 320 L942 314" {stroke} stroke-width="{w - 1}" fill="none"/>
      </g>
      <g id="brow-r" style="transform-origin:1000px 318px">
        <path d="M978 314 L1020 320" {stroke} stroke-width="{w - 1}" fill="none"/>
      </g>
      <g id="eye-l" style="transform-origin:921px 358px">
        <circle cx="921" cy="358" r="11" fill="{c.line}"/>
      </g>
      <g id="eye-r" style="transform-origin:999px 358px">
        <circle cx="999" cy="358" r="11" fill="{c.line}"/>
      </g>
      <g id="mouth" style="transform-origin:960px 408px">
        <path d="M938 406 Q960 420 982 406" {stroke} stroke-width="{w - 1}"
              fill="none"/>
      </g>
    </g>
  </g>
</g>'''.strip()


def base_css(look: Look | None = None) -> str:
    """Rules every rig needs, whatever it is doing."""
    return """
.rig g { transform-box: fill-box; }
#root, #body, #head, #torso { transform-box: view-box; }
#arm-l, #arm-r, #leg-l, #leg-r { transform-box: view-box; }
#eye-l, #eye-r, #brow-l, #brow-r, #mouth, #hair { transform-box: view-box; }
"""
