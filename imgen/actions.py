"""What the rig can be asked to do.

Each action is plain CSS keyframes against the rig's named parts. Written
once, they play identically in every row - which is the whole reason the
character is rigged rather than redrawn.

Actions are deliberately short and readable. A sheet says `action=slap` and
gets exactly this, every time, with no model in the loop and nothing to
review.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    name: str
    describes: str
    css: str
    #: Looping actions fill whatever time the narration needs; one-shot
    #: actions play once and hold their final pose.
    loops: bool = True


def _breathing(scale: str = "1.012") -> str:
    return f"""
@keyframes breathe {{
  0%,100% {{ transform: translateY(0) scaleY(1); }}
  50%     {{ transform: translateY(-6px) scaleY({scale}); }}
}}
#torso {{ animation: breathe 3.4s ease-in-out infinite; }}
"""


BLINK = """
@keyframes blink { 0%,96%,100% { transform: scaleY(1); } 98% { transform: scaleY(0.1); } }
#eye-l, #eye-r { animation: blink 4.2s ease-in-out infinite; }
"""


def ambient_for(action: Action) -> str:
    """Keep the figure alive in the parts this action leaves alone.

    A one-shot action holds its final pose, and a held pose with nothing
    moving reads as a frozen statue rather than a person: a 1.2s slap in a
    3.3s clip left two full seconds in which not one pixel changed. Adding
    the ambience only where the action is not already driving avoids two
    animations fighting over the same element - CSS would simply let the
    last declaration win.
    """
    extra = []
    if "#torso" not in action.css:
        extra.append(_breathing())
    if "#eye-l" not in action.css and "#eye-r" not in action.css:
        extra.append(BLINK)
    return "".join(extra)


ACTIONS: dict[str, Action] = {
    "idle": Action("idle", "standing, just breathing", _breathing() + BLINK),

    "sleep": Action("sleep", "asleep, head tilted, eyes shut", """
@keyframes doze {
  0%,100% { transform: rotate(-11deg) translateY(0); }
  50%     { transform: rotate(-11deg) translateY(-7px); }
}
@keyframes shut { 0%,100% { transform: scaleY(0.08); } }
@keyframes slow-breath {
  0%,100% { transform: translateY(0) scaleY(1); }
  50%     { transform: translateY(-9px) scaleY(1.02); }
}
#head  { animation: doze 4.6s ease-in-out infinite; }
#eye-l, #eye-r { animation: shut 1s linear infinite; }
#torso { animation: slow-breath 4.6s ease-in-out infinite; }
#mouth { transform: scaleY(0.6); }
"""),

    "wake": Action("wake", "eyes snap open, head jerks upright", """
@keyframes snap-open {
  0%,18%  { transform: scaleY(0.08); }
  26%     { transform: scaleY(1.35); }
  36%,100%{ transform: scaleY(1); }
}
@keyframes jerk-up {
  0%,15%  { transform: rotate(-11deg); }
  30%     { transform: rotate(4deg) translateY(-14px); }
  48%,100%{ transform: rotate(0deg) translateY(0); }
}
@keyframes brows-up {
  0%,20%  { transform: translateY(0); }
  32%,100%{ transform: translateY(-11px); }
}
#eye-l, #eye-r { animation: snap-open 1.1s ease-out both; }
#head  { animation: jerk-up 1.1s cubic-bezier(.2,1.4,.4,1) both; }
#brow-l, #brow-r { animation: brows-up 1.1s ease-out both; }
""", loops=False),

    "freeze": Action("freeze", "rigid, eyes wide, holding still", """
@keyframes stare { 0%,100% { transform: scaleY(1.3) scaleX(1.15); } }
@keyframes hold-breath {
  0%,100% { transform: translateY(-3px) scaleY(1.015); }
}
#eye-l, #eye-r { animation: stare 2s linear infinite; }
#torso { animation: hold-breath 2s linear infinite; }
#brow-l { transform: translateY(-8px) rotate(-6deg); }
#brow-r { transform: translateY(-8px) rotate(6deg); }
"""),

    "slap": Action("slap", "swings a hand into their own cheek", """
/* The arm hangs down-and-out from the shoulder, so reaching the cheek is
   most of a half turn - a smaller angle just waves at the ceiling. */
@keyframes swing {
  0%      { transform: rotate(0deg); }
  30%     { transform: rotate(-96deg); }
  48%     { transform: rotate(-168deg); }
  58%     { transform: rotate(-150deg); }
  78%,100%{ transform: rotate(-6deg); }
}
@keyframes recoil {
  0%,44%  { transform: rotate(0deg) translateX(0); }
  54%     { transform: rotate(-15deg) translateX(-26px); }
  70%     { transform: rotate(6deg) translateX(8px); }
  86%,100%{ transform: rotate(0deg) translateX(0); }
}
@keyframes squeeze {
  0%,44%  { transform: scaleY(1); }
  54%     { transform: scaleY(0.12); }
  74%,100%{ transform: scaleY(1); }
}
@keyframes brace {
  0%,44%  { transform: translateX(0) rotate(0deg); }
  56%     { transform: translateX(-10px) rotate(-3deg); }
  80%,100%{ transform: translateX(0) rotate(0deg); }
}
#arm-r { animation: swing 1.3s cubic-bezier(.3,.1,.2,1.4) both; }
#head  { animation: recoil 1.3s ease-out both; }
#body  { animation: brace 1.3s ease-out both; }
#eye-l, #eye-r { animation: squeeze 1.3s ease-out both; }
""", loops=False),

    "shiver": Action("shiver", "small fast tremble", """
@keyframes tremble {
  0%,100% { transform: translateX(0); }
  25%     { transform: translateX(-4px); }
  75%     { transform: translateX(4px); }
}
#body { animation: tremble 0.16s linear infinite; }
#brow-l { transform: translateY(-5px); }
#brow-r { transform: translateY(-5px); }
"""),

    "point": Action("point", "raises an arm to point", """
@keyframes raise {
  0%      { transform: rotate(0deg); }
  60%     { transform: rotate(-118deg); }
  75%,100%{ transform: rotate(-104deg); }
}
#arm-r { animation: raise 0.9s cubic-bezier(.2,1.2,.3,1) both; }
""" + _breathing(), loops=False),

    "look-around": Action("look-around", "scans left, then right", """
@keyframes scan {
  0%,100% { transform: rotate(0deg); }
  28%     { transform: rotate(-13deg); }
  64%     { transform: rotate(13deg); }
}
#head { animation: scan 4.2s ease-in-out infinite; }
""" + _breathing()),

    "slump": Action("slump", "sags, defeated", """
@keyframes sag {
  0%      { transform: translateY(0) rotate(0deg); }
  100%    { transform: translateY(22px) rotate(7deg); }
}
@keyframes sag-body { 0% { transform: translateY(0); } 100% { transform: translateY(12px); } }
@keyframes droop { 0% { transform: scaleY(1); } 100% { transform: scaleY(0.45); } }
#head  { animation: sag 1.4s ease-out both; }
#body  { animation: sag-body 1.4s ease-out both; }
#eye-l, #eye-r { animation: droop 1.4s ease-out both; }
""", loops=False),
}


def names() -> list[str]:
    return sorted(ACTIONS)


def get(name: str) -> Action | None:
    return ACTIONS.get((name or "").strip().casefold().replace(" ", "-"))
