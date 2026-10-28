"""The vector-animation scene: rig, actions, framing."""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen.actions import get, names  # noqa: E402
from imgen.rig import PARTS, Rig  # noqa: E402
from imgen.stage import BACKDROPS, CAMERAS, FRAMINGS, Shot, StageError, build_html  # noqa: E402


# ---- the rig -------------------------------------------------------------

def test_every_declared_part_exists_in_the_drawing():
    svg = Rig().svg()
    for part in PARTS:
        assert f'id="{part}"' in svg, f"{part} is declared but never drawn"


def test_moving_parts_pivot_at_a_joint():
    """Without an explicit origin a limb rotates about the page corner."""
    svg = Rig().svg()
    for limb in ("arm-l", "arm-r", "leg-l", "leg-r", "head"):
        block = svg.split(f'id="{limb}"')[1][:80]
        assert "transform-origin" in block, f"{limb} has no pivot"


def test_the_rig_keeps_its_own_coordinate_space():
    """Its paths are written in 1920x1080; the output size must not change
    that, or the figure renders outside the frame."""
    import inspect

    from imgen import stage

    assert "Rig(look=shot.look)" in inspect.getsource(stage.build_html)


# ---- actions -------------------------------------------------------------

def test_actions_only_move_parts_that_exist():
    for name in names():
        for part in re.findall(r"#([a-z-]+)\s*[,{]", get(name).css):
            assert part in PARTS, f"{name} moves unknown part {part!r}"


def test_every_action_declares_keyframes_and_uses_them():
    for name in names():
        css = get(name).css
        declared = set(re.findall(r"@keyframes\s+([\w-]+)", css))
        used = set(re.findall(r"animation:\s*([\w-]+)", css))
        assert used <= declared, f"{name} uses undeclared keyframes {used - declared}"


def test_one_shot_actions_hold_their_final_pose():
    """Without `both` the character snaps back the instant it finishes."""
    for name in names():
        action = get(name)
        if not action.loops:
            assert "both" in action.css, f"{name} plays once but does not hold"


def test_an_unknown_action_is_named_rather_than_ignored():
    with pytest.raises(StageError, match="unknown action"):
        build_html(Shot(action="moonwalk"))


# ---- the scene -----------------------------------------------------------

def test_nothing_plays_by_itself():
    """Frames are produced by seeking the clock. A freely playing animation
    would give a different result on every run."""
    assert "animation-play-state: paused" in build_html(Shot())


def test_a_one_shot_action_is_told_how_long_the_shot_is():
    assert "--dur: 2.5s" in build_html(Shot(action="slap", duration=2.5))


@pytest.mark.parametrize("framing", list(FRAMINGS))
def test_every_framing_is_widescreen(framing):
    """A crop that is not 16:9 would letterbox the shot."""
    _x, _y, w, h = FRAMINGS[framing]
    assert abs(w / h - 16 / 9) < 0.02, f"{framing} is {w}x{h}"


@pytest.mark.parametrize("framing", list(FRAMINGS))
def test_framing_reaches_the_viewbox(framing):
    html = build_html(Shot(framing=framing))
    assert f'viewBox="{" ".join(str(n) for n in FRAMINGS[framing])}"' in html


def test_a_generated_image_can_stand_in_as_the_backdrop(tmp_path):
    """Served, not linked and not inlined.

    Two earlier shapes were both wrong and both looked right in a test:
    a file:// link is never loaded, because set_content leaves the document
    on about:blank; and a base64 data URI loads but costs five times as
    much per screenshot. The renderer answers BACKDROP_URL from disk.
    """
    from imgen.stage import BACKDROP_URL

    picture = tmp_path / "shot.jpg"
    picture.write_bytes(b"\xff\xd8\xff\xe0 pretend jpeg")
    html = build_html(Shot(image=picture))

    assert "file://" not in html, "a linked file:// backdrop never loads"
    assert "base64" not in html, "inlining it makes every frame 5x slower"
    assert BACKDROP_URL in html


@pytest.mark.parametrize("backdrop", list(BACKDROPS))
def test_every_backdrop_renders(backdrop):
    assert build_html(Shot(backdrop=backdrop))


@pytest.mark.parametrize("camera", list(CAMERAS))
def test_every_camera_move_renders(camera):
    assert build_html(Shot(camera=camera))


# ---- the sheet column ----------------------------------------------------

def test_a_one_shot_action_keeps_the_figure_alive():
    """A 1.2s slap in a 3.3s clip left two seconds in which not one pixel
    changed - a frozen statue, not a held pose. Ambience fills the parts
    the action is not already driving."""
    from imgen.actions import ACTIONS, ambient_for

    slap = ACTIONS["slap"]
    assert not slap.loops
    assert "#torso" not in slap.css, "the premise: slap leaves the torso free"
    assert "breathe" in ambient_for(slap)

    # ...and never twice over the same part, which CSS would resolve by
    # simply dropping one of them.
    idle = ACTIONS["idle"]
    assert ambient_for(idle) == "", "idle already breathes and blinks"
    point = ACTIONS["point"]
    assert "breathe" not in ambient_for(point), "point drives the torso itself"


def test_the_animation_column_is_read_from_the_row(tmp_path):
    from imgen.config import load_config
    from imgen.db import Job, Status
    from imgen.produce import Producer

    cfg = load_config()
    producer = Producer(cfg, log=lambda m: None)
    column = cfg.sheets.animation_column

    def job(fields):
        return Job(id=1, row_number=2, prompt="x", status=Status.DONE,
                   attempts=1, error=None, image_paths=[], fields=fields)

    assert producer.animation_of(job({})) == "", "a blank row keeps its still"
    assert producer.animation_of(job({column: "  slap "})) == "slap"
    # The header is stored as the sheet spells it, so "Animation" with a
    # capital must still match the configured column name.
    assert producer.animation_of(job({column.capitalize(): "wake"})) == "wake"


def test_animated_frames_replace_the_still_without_ken_burns():
    """The frames already move; panning them too would fight the animation."""
    from imgen.video import Clip, RenderSettings, build_filter

    settings = RenderSettings(ken_burns=True)
    still = Clip(row=1, image=Path("a.jpg"), text="hi", duration=3.0)
    animated = Clip(row=1, image=Path("a.jpg"), text="hi", duration=3.0,
                    frames=Path("frames"))
    assert "zoompan" in build_filter(still, settings, 0, None)
    assert "zoompan" not in build_filter(animated, settings, 0, None)


def test_animation_can_be_switched_off_without_emptying_the_column(tmp_path):
    """The column is per-row optional; this is the master switch, so a
    filled sheet can still be built as plain stills."""
    from dataclasses import replace

    from imgen.config import load_config
    from imgen.db import Job, Status
    from imgen.produce import Producer
    from imgen.video import Clip

    cfg = load_config()
    column = cfg.sheets.animation_column
    job = Job(id=1, row_number=2, prompt="x", status=Status.DONE, attempts=1,
              error=None, image_paths=[], fields={column: "slap"})
    clip = Clip(row=2, image=tmp_path / "a.jpg", text="hi", duration=3.0)

    off = replace(cfg, video=replace(cfg.video, animate_rows=False))
    # Returns before it would open a browser, so no Chrome is needed here.
    assert Producer(off, log=lambda m: None).animate([job], [clip], tmp_path) == 0
    assert clip.frames is None, "the row keeps its still picture"


def test_frames_are_jpeg_and_every_consumer_agrees(tmp_path):
    """The capture is the whole cost of a render, and PNG-compressing a
    photographic backdrop was most of it - 1112ms a frame against 186.
    The frames go straight to x264, so lossless buys nothing.

    Three places name these files; a mismatch means ffmpeg finds nothing
    and the row renders black.
    """
    import inspect

    from imgen import animate, video

    assert animate.FRAME_PATTERN.endswith(".jpg")
    assert animate.FRAME_GLOB.endswith(".jpg")
    assert 'type="jpeg"' in inspect.getsource(animate.render_frames)

    assert video.FRAME_PATTERN is animate.FRAME_PATTERN
    assert video.FRAME_GLOB is animate.FRAME_GLOB
    assert animate.FRAME_PATTERN % 7 == "f00007.jpg"


def test_a_slow_capture_says_what_to_do_about_it():
    """A bare 'Page.screenshot: Timeout 30000ms exceeded' tells the user
    nothing. The browser being busy with a generation run is the cause."""
    import inspect

    from imgen.animate import render_frames

    source = inspect.getsource(render_frames)
    assert "shot_timeout_ms" in source
    assert "busy" in source
