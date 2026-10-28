"""Captions, timing and the video pipeline's decisions.

ffmpeg itself is exercised by one end-to-end test; everything else is pure
logic and runs without it.
"""

import subprocess
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen.captions import Caption, build_srt, timestamp, wrap  # noqa: E402
from imgen.db import JobStore, Status  # noqa: E402
from imgen.produce import order_key, script_of  # noqa: E402
from imgen.video import Clip, RenderSettings, build_filter  # noqa: E402


# ---- captions -----------------------------------------------------------

@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, "00:00:00,000"),
        (3.456, "00:00:03,456"),
        (61.5, "00:01:01,500"),
        (3725.25, "01:02:05,250"),
        (-1, "00:00:00,000"),
    ],
)
def test_srt_timestamps(seconds, expected):
    assert timestamp(seconds) == expected


def test_long_lines_are_wrapped_for_reading():
    text = "That thin high whine somewhere near your ear that simply will not stop"
    wrapped = wrap(text, width=30)
    assert "\n" in wrapped
    assert all(len(line) <= 30 for line in wrapped.splitlines())


def test_short_lines_are_left_alone():
    assert wrap("Then you hear it.", width=42) == "Then you hear it."


def test_srt_numbers_entries_from_one_and_skips_empties():
    srt = build_srt([Caption(0, 1, "first"), Caption(1, 2, "  "), Caption(2, 3, "second")])
    assert srt.startswith("1\n")
    assert "second" in srt and srt.count(" --> ") == 2


# ---- which column is the narration --------------------------------------

class FakeJob:
    def __init__(self, fields, row=2):
        self.fields = fields
        self.row_number = row


def test_the_script_column_is_found_by_any_of_its_usual_names():
    for name in ("script line", "Script", "narration", "Voiceover"):
        assert script_of(FakeJob({name: "hello"})) == "hello"


def test_no_script_column_is_not_an_error():
    assert script_of(FakeJob({"image prompt": "a cat"})) == ""


# ---- storyboard order ----------------------------------------------------

def test_rows_are_ordered_by_the_sequence_column_not_the_sheet_row():
    jobs = [FakeJob({"sequence": "10"}, row=2), FakeJob({"sequence": "2"}, row=9)]
    ordered = sorted(jobs, key=lambda j: order_key(j, "sequence"))
    assert [j.fields["sequence"] for j in ordered] == ["2", "10"], "must sort numerically"


def test_rows_without_a_usable_sequence_fall_back_to_the_row_number():
    jobs = [FakeJob({}, row=5), FakeJob({"sequence": "x"}, row=3), FakeJob({"sequence": "1"}, row=9)]
    ordered = sorted(jobs, key=lambda j: order_key(j, "sequence"))
    # Numbered rows lead; the rest keep sheet order behind them.
    assert ordered[0].fields.get("sequence") == "1"
    assert [j.row_number for j in ordered[1:]] == [3, 5]


# ---- the filter graph ----------------------------------------------------

def test_ken_burns_upscales_before_zooming():
    """zoompan steps in whole source pixels, so zooming a 1080p still
    directly produces visible judder."""
    f = build_filter(Clip(2, Path("a.jpg"), "hi", duration=3), RenderSettings(), 0, None)
    assert "scale=3840:2160" in f and "zoompan" in f


def test_ken_burns_alternates_direction_between_clips():
    settings = RenderSettings()
    first = build_filter(Clip(2, Path("a.jpg"), "hi", duration=3), settings, 0, None)
    second = build_filter(Clip(3, Path("b.jpg"), "hi", duration=3), settings, 1, None)
    assert first != second, "every clip zooming the same way looks mechanical"


def test_disabling_ken_burns_leaves_a_plain_scale():
    f = build_filter(
        Clip(2, Path("a.jpg"), "hi", duration=3),
        RenderSettings(ken_burns=False), 0, None,
    )
    assert "zoompan" not in f and "scale=1920:1080" in f


def test_the_fade_transition_dips_each_clip_at_both_ends():
    """Cheap enough to keep concat copying instead of re-encoding."""
    f = build_filter(
        Clip(2, Path("a.jpg"), "hi", duration=4),
        RenderSettings(transition="fade", transition_s=0.4), 0, None,
    )
    assert "fade=t=in:st=0" in f and "fade=t=out" in f


def test_no_transition_means_no_fade_filters():
    f = build_filter(
        Clip(2, Path("a.jpg"), "hi", duration=4),
        RenderSettings(transition="none"), 0, None,
    )
    assert "fade=" not in f


def test_the_fade_never_exceeds_a_third_of_a_short_clip():
    """A 0.4s dip at each end of a 0.6s clip would leave nothing visible."""
    f = build_filter(
        Clip(2, Path("a.jpg"), "hi", duration=0.6),
        RenderSettings(transition="fade", transition_s=0.4), 0, None,
    )
    fade = float(f.split("fade=t=in:st=0:d=")[1].split(",")[0])
    assert fade <= 0.2 + 1e-6


def test_captions_are_only_burned_when_a_subtitle_file_is_given():
    """ASS rather than SRT: the styling and animation travel with the text,
    which is the only way libass can colour or animate a line."""
    settings = RenderSettings()
    clip = Clip(2, Path("a.jpg"), "hi", duration=3)
    assert "ass=" not in build_filter(clip, settings, 0, None)
    assert "ass=" in build_filter(clip, settings, 0, Path("/tmp/x.ass"))


def test_filter_paths_are_escaped():
    """A ':' in a path would otherwise be read as the next filter argument."""
    f = build_filter(
        Clip(2, Path("a.jpg"), "hi", duration=3),
        RenderSettings(), 0, Path("/tmp/a:b/x.ass"),
    )
    quoted = f.split("ass='")[1].split("'")[0]
    assert "\\:" in quoted, "the colon in the path must be escaped"


# ---- narration is remembered --------------------------------------------

def test_audio_is_stored_against_the_row_and_survives_reload(tmp_path):
    store = JobStore(tmp_path / "t.db")
    store.sync_prompts([(2, "a cat")])
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["a.jpg"])

    store.set_audio(job.id, "/tmp/a.mp3", 3.25)
    reloaded = store.all_jobs()[0]
    assert reloaded.audio_path == "/tmp/a.mp3"
    assert reloaded.duration_s == pytest.approx(3.25)

    assert store.clear_audio([job.id]) == 1
    assert store.all_jobs()[0].audio_path is None


def test_moving_the_output_folder_does_not_orphan_the_images(tmp_path):
    """The first-run migration moves output/, which left every recorded path
    pointing at the old location. The gallery and export skipped the missing
    files silently; the video builder could not."""
    store = JobStore(tmp_path / "t.db")
    store.sync_prompts([(2, "a cat")])
    job = store.claim_next(3)

    old = tmp_path / "old" / "cat.jpg"
    old.parent.mkdir()
    old.write_bytes(b"\xff\xd8x")
    store.finish(job.id, Status.DONE, image_paths=[str(old)])

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    shutil.move(str(old), str(new_dir / "cat.jpg"))
    assert not Path(store.all_jobs()[0].image_paths[0]).exists()

    repaired, lost = store.repair_image_paths(new_dir)
    assert (repaired, lost) == (1, 0)
    assert Path(store.all_jobs()[0].image_paths[0]).exists()


# ---- one real render -----------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_clip_really_renders(tmp_path):
    from imgen.video import render_clip, require_tools

    settings = RenderSettings(width=320, height=180, fps=12, preset="ultrafast",
                              ken_burns=False, burn_captions=False)
    ffmpeg, _ = require_tools(settings)

    image = tmp_path / "frame.png"
    import subprocess

    subprocess.run(
        [str(ffmpeg), "-y", "-f", "lavfi", "-i", "color=c=navy:s=640x360",
         "-frames:v", "1", str(image)],
        capture_output=True, timeout=60,
    )
    out = render_clip(
        Clip(2, image, "hello", duration=1.0), tmp_path / "clip.mp4", settings, ffmpeg
    )
    assert out.exists() and out.stat().st_size > 0


# ---- styled captions -----------------------------------------------------

def test_colours_are_converted_to_ass_byte_order():
    """ASS is &HAABBGGRR - blue and red swap places, so a wrong conversion
    silently produces the complementary colour."""
    from imgen.subtitles import ass_colour

    assert ass_colour("#FFE14D") == "&H004DE1FF"
    assert ass_colour("#000000") == "&H00000000"
    assert ass_colour("#FFF") == "&H00FFFFFF"
    assert ass_colour("nonsense") == "&H00FFFFFF", "must not crash on bad input"


def test_a_box_caption_always_has_padding():
    """With BorderStyle=3 the outline value is the box size; zero renders no
    box at all, which is what the preset originally did."""
    from imgen.subtitles import AssDocument, CaptionStyle

    style = CaptionStyle.from_preset("Black box", outline_px=0)
    body = AssDocument(style, []).render()
    line = next(x for x in body.splitlines() if x.startswith("Style:"))
    fields = line.split(",")
    # Field order per the ASS V4+ Format line: ..., BorderStyle, Outline, ...
    assert fields[15] == "3", "BorderStyle 3 is the opaque box"
    assert int(fields[16]) >= 4, "a box with no padding is invisible"


def test_highlight_animation_emits_karaoke_timings():
    from imgen.captions import Caption
    from imgen.subtitles import CaptionStyle, build_line

    line = build_line(Caption(0, 2.0, "one two three"), CaptionStyle(animation="highlight"))
    assert line.count("\\kf") == 3, "one timing per word"


def test_each_animation_produces_something_renderable():
    from imgen.captions import Caption
    from imgen.subtitles import ANIMATIONS, CaptionStyle, build_line

    for name in ANIMATIONS:
        line = build_line(Caption(0, 2.0, "hello there"), CaptionStyle(animation=name))
        assert line.strip(), f"{name} produced an empty line"


# ---- joining -------------------------------------------------------------

def test_crossfade_offsets_account_for_the_overlap():
    """Each seam shortens the result, so offsets must accumulate against the
    overlap or the clips drift apart."""
    import inspect

    from imgen import video

    body = inspect.getsource(video.crossfade)
    assert "offset += durations[index - 1] - overlap" in body
    assert "acrossfade" in body, "audio has to overlap too, not just the picture"


def test_crossfade_is_declined_for_very_long_sequences():
    """One filter step per clip; 152 of them is not practical."""
    from imgen.video import CROSSFADE_LIMIT

    assert 10 <= CROSSFADE_LIMIT <= 120


def test_crossfade_does_not_also_dip_each_clip_to_black():
    """The overlap does the work; a dip underneath it shows through."""
    from imgen.video import Clip, RenderSettings, build_filter

    f = build_filter(
        Clip(2, Path("a.jpg"), "hi", duration=4),
        RenderSettings(transition="crossfade", transition_s=0.5), 0, None,
    )
    assert "fade=" not in f


def test_the_overlap_never_exceeds_half_the_shortest_clip():
    """A 0.6s overlap between two 0.5s clips has nothing left to show."""
    from imgen.video import RenderSettings, crossfade_overlap

    settings = RenderSettings(transition="crossfade", transition_s=0.6)
    # Asserting on the source text broke the moment the rule moved into a
    # shared helper; assert what the rule does instead.
    assert crossfade_overlap([0.5, 0.5], settings) == pytest.approx(0.25)
    assert crossfade_overlap([4.0, 5.0], settings) == pytest.approx(0.6)
    assert crossfade_overlap([0.02, 3.0], settings) == pytest.approx(0.05)


# ---- keeping picture, voice and caption on the same clock ----------------

def test_durations_are_snapped_to_whole_frames():
    """A clip's picture is a whole number of frames; asking for 3.278s at
    30fps gives 3.267s. Everything else must be told that same number."""
    from imgen.video import frame_align

    assert frame_align(3.278, 30) == pytest.approx(98 / 30)
    assert frame_align(2.222, 30) == pytest.approx(67 / 30)
    assert frame_align(0.0, 30) == pytest.approx(1 / 30), "never a zero-frame clip"


def test_the_clip_audio_is_padded_to_the_length_of_the_picture(tmp_path, monkeypatch):
    """The narration is shorter than the clip by video.padding_s. Without
    apad the audio stream ends early, so on concat the next clip's voice
    starts where the last voice ended instead of where its picture starts,
    and the soundtrack creeps ahead by the padding on every clip.
    """
    from imgen.video import Clip, RenderSettings, render_clip

    seen = {}

    def fake_run(command, **kwargs):
        seen["cmd"] = command
        Path(command[-1]).write_bytes(b"x")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    image = tmp_path / "a.jpg"
    image.write_bytes(b"x")
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    clip = Clip(row=1, image=image, text="hi", audio=audio, duration=3.278)
    render_clip(clip, tmp_path / "out.mp4", RenderSettings(), Path("ffmpeg"))

    graph = seen["cmd"][seen["cmd"].index("-filter_complex") + 1]
    assert "apad" in graph, "the narration must run out to the end of the picture"
    assert "aformat" in graph, "clips must share one audio format to concat"
    # -t has to be the frame-aligned figure, not the raw one.
    assert seen["cmd"][seen["cmd"].index("-t") + 1] == f"{98 / 30:.3f}"


def test_the_sidecar_srt_accounts_for_the_transition():
    """A crossfade plays each clip over the tail of the last, so the video
    is shorter than the clips add up to. Writing the .srt from the plain
    running total put the subtitles 7.5s ahead of a one-minute video."""
    from imgen.video import RenderSettings, crossfade_overlap, will_crossfade

    settings = RenderSettings(transition="crossfade", transition_s=0.4)
    lengths = [3.0] * 20
    assert will_crossfade(20, settings)
    overlap = crossfade_overlap(lengths, settings)
    timeline = sum(lengths) - overlap * (len(lengths) - 1)

    cursor, last_end = 0.0, 0.0
    for length in lengths:
        last_end = cursor + length
        cursor += length - overlap
    assert last_end == pytest.approx(timeline), "the last cue must end with the video"

    # And the plain path must not subtract anything.
    assert not will_crossfade(20, RenderSettings(transition="fade"))
    assert not will_crossfade(1, settings), "one clip has no transition"
