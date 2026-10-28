"""The thumbnail: a picture from Flow, the words from the tool."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen.config import TextBlock, ThumbnailConfig, load_config  # noqa: E402
from imgen.db import Job, JobStore, Status  # noqa: E402
from imgen.sheets import PromptRow  # noqa: E402
from imgen.thumbnail import (  # noqa: E402
    JOB_MARKER,
    PICTURE_URL,
    build_html,
    description_in,
    ensure_job,
    is_thumbnail_job,
)


def job(row_number: int, fields: dict) -> Job:
    return Job(id=row_number, row_number=row_number, prompt="p",
               status=Status.DONE, attempts=1, error=None,
               image_paths=[], fields=fields)


def test_the_first_filled_cell_wins():
    """One thumbnail per project, so a second description is ignored
    rather than quietly producing two."""
    rows = [
        job(2, {"scene": "hook"}),
        job(3, {"thumbnail": "   "}),
        job(4, {"Thumbnail": "a huge mosquito over a sleeping figure"}),
        job(5, {"thumbnail": "something else entirely"}),
    ]
    assert description_in(rows, "thumbnail") == "a huge mosquito over a sleeping figure"
    assert description_in([job(2, {})], "thumbnail") == ""


def test_the_picture_is_a_queue_job_but_never_a_clip(tmp_path):
    """It goes through the queue so it gets Flow submission, retries and
    claim-before-generate for free - but a thumbnail is not a scene, and
    a stray clip of it at the front of the video would be obvious."""
    from imgen.produce import Producer

    store = JobStore(tmp_path / "s.db")
    try:
        store.sync_prompts([PromptRow(2, "a real scene", {})])
        assert ensure_job(store, "the thumbnail picture") is True
        assert ensure_job(store, "   ") is False, "nothing to queue"

        jobs = store.all_jobs()
        assert len(jobs) == 2
        tagged = [j for j in jobs if is_thumbnail_job(j)]
        assert len(tagged) == 1
        assert tagged[0].prompt == "the thumbnail picture"

        # Mark both finished with an image, then ask for the storyboard.
        picture = tmp_path / "x.jpg"
        picture.write_bytes(b"x")
        for j in jobs:
            store.set_images(j.id, [str(picture)])
        rows = Producer(load_config(), log=lambda m: None).rows(store)
        assert [r.prompt for r in rows] == ["a real scene"]
    finally:
        store.close()


def test_queueing_the_same_description_twice_does_not_pile_up(tmp_path):
    store = JobStore(tmp_path / "s.db")
    try:
        ensure_job(store, "one picture")
        ensure_job(store, "one picture")
        assert len(store.all_jobs()) == 1
    finally:
        store.close()


def test_the_words_are_drawn_over_a_served_picture(tmp_path):
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"\xff\xd8\xff\xe0 jpeg")
    spec = ThumbnailConfig(blocks=(TextBlock(text="two\nlines", outline_px=9),))
    html = build_html(picture, spec)

    assert "TWO<br>LINES" in html, "uppercase on, one sheet line per line"
    assert PICTURE_URL in html
    assert "file://" not in html, "set_content leaves the page on about:blank"
    assert "paint-order: stroke fill" in html, "outline must not eat the letters"
    assert "-webkit-text-stroke:9px" in html


def test_text_is_escaped_not_injected(tmp_path):
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"x")
    spec = ThumbnailConfig(
        blocks=(TextBlock(text="<script>alert(1)</script>", uppercase=False),)
    )
    html = build_html(picture, spec)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("align", ["left", "centre", "right"])
@pytest.mark.parametrize("position", ["top", "middle", "bottom"])
def test_every_placement_renders(align, position, tmp_path):
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"x")
    spec = ThumbnailConfig(
        blocks=(TextBlock(text="hi", align=align, position=position),)
    )
    assert build_html(picture, spec)


def test_the_marker_is_not_a_column_anyone_would_type():
    """It shares the fields dict with the sheet's own columns."""
    assert JOB_MARKER.startswith("__") and JOB_MARKER.endswith("__")


def test_several_blocks_each_keep_their_own_look(tmp_path):
    """A thumbnail is a shout plus a remark, and they want different
    fonts and places - so they are separate blocks, not one string."""
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"x")
    spec = ThumbnailConfig(blocks=(
        TextBlock(text="badge", font="Impact", size=40, box="#FFD400", rotate=-3),
        TextBlock(text="shout", font="Arial Black", size=120),
    ))
    html = build_html(picture, spec)

    assert "'Impact'" in html and "'Arial Black'" in html
    assert "40px" in html and "120px" in html
    assert "background:#FFD400" in html, "the slab is part of the block"
    assert "rotate(-3" in html
    assert html.count("id=\"block") == 2


def test_an_empty_block_draws_nothing(tmp_path):
    picture = tmp_path / "p.jpg"
    picture.write_bytes(b"x")
    html = build_html(picture, ThumbnailConfig(blocks=(
        TextBlock(text="   "), TextBlock(text="real"),
    )))
    assert html.count("id=\"block") == 1


def test_a_suggestion_only_ever_picks_lines_the_script_contains():
    """There is no model here. Inventing a claim would mean inventing it
    wrongly, so the suggestion chooses wording rather than writing it."""
    from imgen.thumbnail import suggest_blocks

    lines = [
        "and you miss.",
        "It was the single most dangerous noise",
        "Congratulations. You just lost a fight",
        "to an animal that weighs less than a raindrop.",
    ]
    rows = [
        Job(id=i, row_number=i, prompt="p", status=Status.DONE, attempts=1,
            error=None, image_paths=[],
            fields={"script line": text, "scene": "2am hook"})
        for i, text in enumerate(lines, start=2)
    ]
    blocks = suggest_blocks(rows, "scene")
    assert blocks, "something should have scored"
    spoken = " ".join(b.text.replace("\n", " ") for b in blocks)
    for block in blocks:
        flat = block.text.replace("\n", " ")
        assert any(flat in line for line in lines), f"invented: {flat!r}"
    # A continuation line is never the headline: it reads as broken alone.
    assert "and you miss" not in spoken.casefold()


def test_the_scene_name_is_not_put_on_the_thumbnail():
    """Scene names here are production labels - "Time jump", "Blood
    rewritten" - and one on a thumbnail reads as a mistake."""
    from imgen.thumbnail import suggest_blocks

    rows = [
        Job(id=2, row_number=2, prompt="p", status=Status.DONE, attempts=1,
            error=None, image_paths=[],
            fields={"script line": "The deadliest animal alive", "scene": "Time jump"})
    ]
    for block in suggest_blocks(rows, "scene"):
        assert "time jump" not in block.text.casefold()


def test_a_size_too_big_for_the_text_is_cut_down():
    """A guessed size is only a guess until the text is in the real font.
    A 37-character line at 108px wrapped to four lines and covered the
    whole picture, so the page measures and shrinks before the capture."""
    from imgen.thumbnail import FIT, MAX_BLOCK_HEIGHT, fit_size

    # The estimate alone must already scale with the length.
    assert fit_size("SHORT", 1280, 0.86) > fit_size("A MUCH LONGER HEADLINE", 1280, 0.86)
    assert fit_size("", 1280, 0.86) == 112, "nothing to measure, keep the cap"
    assert fit_size("x" * 200, 1280, 0.86) >= 28, "never shrink to nothing"

    # And the browser has the last word, using the real font metrics.
    assert "getBoundingClientRect" in FIT
    assert 0 < MAX_BLOCK_HEIGHT < 1
