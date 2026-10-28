"""Matching a Flow result back to the row that asked for it.

Everything about running several generations at once rests on this. When it
fails the symptom is not an error: the image is sitting in Flow, finished,
and the app reports the row as stuck at 99% until it times out - then pays
to generate it again.
"""

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen.flow import _DECOR_JS, _SCAN_BATCHES, matches, normalise  # noqa: E402

PROMPT = (
    "One large cartoon mosquito centered under a spotlight on a plain pale "
    "background. stick figure modern person: plain round white head, simple "
    "black dot eyes."
)

#: The strip expression the scanner actually ships, translated rather than
#: retyped. A hand-written copy here passed for a whole release while the
#: real regex was still the broken single-alternation one.
def _python_equivalent(js: str) -> re.Pattern:
    body, _, flags = js.rpartition("/")
    assert body.startswith("/"), js
    return re.compile(body[1:], re.IGNORECASE if "i" in flags else 0)


STRIP = _python_equivalent(_DECOR_JS)


def test_the_shipped_regex_is_the_one_under_test():
    """Guard against this file drifting away from the code again."""
    assert _DECOR_JS in _SCAN_BATCHES
    assert ")+/" in _DECOR_JS, "the button group must repeat"


def test_a_batch_keeps_one_identity_as_it_finishes():
    """The bug that cost 300s a row, twice.

    A running batch reads "image 99% <prompt>"; the same batch once done
    reads "download undo delete keyboard_return <prompt>". Stripping only
    the first label gave the two states different text, so the reader
    cached them as two batches and kept answering with the unfinished one
    while the finished image sat in the page.
    """
    prompt = "A smoky campfire burning at a cave entrance, night."
    running = STRIP.sub("", f"image 99% {prompt}")
    done = STRIP.sub("", f"download undo delete keyboard_return {prompt}")
    assert running == done == prompt
    assert normalise(running)[:80] == normalise(done)[:80], "cache keys must agree"


def test_the_scanner_strips_every_leading_button_not_just_the_first():
    """A batch leads with several button labels. Stripping one left "undo
    delete " glued to the prompt, and no result ever matched its row."""
    assert "+" in _SCAN_BATCHES.split("replace(")[1][:200], "the group must repeat"
    raw = "download undo delete One large cartoon mosquito"
    assert STRIP.sub("", raw) == "One large cartoon mosquito"


@pytest.mark.parametrize(
    "decoration",
    [
        "",
        "download undo delete ",
        "undo delete ",
        "image 99% ",
        "download batch undo delete keyboard_return ",
        "favorite more_vert download ",
    ],
)
def test_a_result_is_matched_whatever_buttons_surround_it(decoration):
    assert matches(STRIP.sub("", decoration + PROMPT), PROMPT)


def test_a_result_is_matched_even_if_the_decoration_survives():
    """Belt and braces: matching looks for the prompt anywhere in the batch
    text, so a button label we have not seen before cannot break it."""
    assert matches("some new button " + PROMPT, PROMPT)


def test_different_rows_are_not_confused():
    """Storyboard prompts share a long tail of style instructions, so only
    the opening distinguishes them."""
    other = (
        "A single large cartoon mosquito centered on a plain pale background. "
        "stick figure modern person: plain round white head, simple black dot eyes."
    )
    assert not matches(other, PROMPT)
    assert not matches(PROMPT, other)


def test_a_truncated_batch_still_matches():
    assert matches(PROMPT[:70], PROMPT)


def test_blank_text_matches_nothing():
    assert not matches("", PROMPT)
    assert not matches(PROMPT, "")


def test_normalise_collapses_whitespace_and_case():
    assert normalise("  Two   WORDS\n") == "two words"


def test_the_scanner_scrolls_the_virtual_list_not_the_page():
    """Flow renders the batches with Angular's CDK virtual scroller: only a
    few exist at a time and the scrollable element is an inner container, so
    page-level wheel events move nothing."""
    from imgen.flow import _SCROLL

    assert "cdk-virtual-scrollable" in _SCROLL
    assert "scrollTop" in _SCROLL


def test_find_prefers_a_finished_batch_over_a_stale_running_one():
    """Defence in depth for the same failure.

    If Flow ever adds a button the stripper does not know, the two states
    of one batch land under different keys again. The reader must then
    still answer with the one that has an image, not with whichever was
    seen first.
    """
    from imgen.flow import Batch, BatchReader, normalise

    reader = BatchReader.__new__(BatchReader)
    reader.seen = {}
    prompt = "A caveman sleeping calmly within the smoke, mosquitoes kept away."
    stale = Batch(prompt, 99, [])
    fresh = Batch(f"sparkle {prompt}", None, ["https://flow.google.com/asb/x"])
    reader.seen[normalise(stale.prompt)[:80]] = stale
    reader.seen[normalise(fresh.prompt)[:80]] = fresh

    found = reader.find(prompt)
    assert found is fresh, "the finished batch must win"
    assert found.finished
