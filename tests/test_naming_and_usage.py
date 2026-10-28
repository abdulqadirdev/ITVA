"""Filename templates, daily usage accounting and browser-loss detection."""

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen import usage  # noqa: E402
from imgen.flow import is_browser_gone  # noqa: E402
from imgen.naming import NameContext, TemplateError, render, sanitize  # noqa: E402


# ---- templates ----------------------------------------------------------

def ctx(**over) -> NameContext:
    base = dict(row=7, seq="5", scene="2am hook", prompt="A sleepy person", index=2, n=19)
    base.update(over)
    return NameContext(**base)


def test_default_template_pads_and_slugifies():
    assert render("{row:04}_{prompt}_{index}", ctx()) == "0007_a-sleepy-person_2"


def test_numeric_sequence_pads_left_not_right():
    # "{:04}" on the string "5" would produce "5000"; it must produce "0005".
    assert render("{seq:04}", ctx()) == "0005"


def test_non_numeric_sequence_is_slugified_instead_of_padded():
    assert render("{seq}", ctx(seq="Scene 2A")) == "scene-2a"


def test_missing_sequence_falls_back_to_the_row_number():
    assert render("{seq:03}", ctx(seq="")) == "007"


def test_slash_in_a_template_creates_a_subfolder():
    assert render("{scene}/{seq:03}", ctx()) == "2am-hook/005"


def test_unknown_token_is_named_rather_than_crashing():
    with pytest.raises(TemplateError, match="unknown token '{nope}'"):
        render("{nope}", ctx())


def test_empty_template_falls_back_to_the_default():
    assert render("   ", ctx()) == "0007_a-sleepy-person_2"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('a:b*c?d"e|f', "a-b-c-d-e-f"),   # characters Windows refuses
        ("  spaced  ", "spaced"),
        ("trailing.", "trailing"),         # Windows strips trailing dots
        ("a---b", "a-b"),
    ],
)
def test_sanitize_makes_names_safe_on_every_platform(raw, expected):
    assert sanitize(raw) == expected


def test_windows_reserved_device_names_are_escaped():
    # "con.jpg" is unopenable on Windows however it is written.
    assert sanitize("con.jpg").startswith("_")
    assert sanitize("ok.jpg") == "ok.jpg"


def test_a_template_that_renders_to_nothing_is_rejected():
    with pytest.raises(TemplateError):
        render("///", ctx())


# ---- usage --------------------------------------------------------------

@pytest.fixture
def counter(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "app_data_dir", lambda: tmp_path)
    return tmp_path


def test_usage_accumulates_for_the_day(counter):
    assert usage.today() == 0
    usage.record(3)
    usage.record(2)
    assert usage.today() == 5


def test_usage_is_per_day(counter):
    usage.record(4, when=date(2026, 1, 1))
    assert usage.today(when=date(2026, 1, 2)) == 0
    assert usage.today(when=date(2026, 1, 1)) == 4


def test_remaining_respects_a_cap_and_ignores_zero(counter):
    usage.record(8)
    assert usage.remaining(10) == 2
    assert usage.remaining(5) == 0, "never negative"
    assert usage.remaining(0) is None, "0 means no cap"


def test_a_corrupt_counter_does_not_block_generating(counter):
    (counter / "usage.json").write_text("{ not json")
    assert usage.today() == 0
    usage.record(1)
    assert usage.today() == 1


# ---- browser loss -------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "Target closed",
        "Target page, context or browser has been closed",
        "Connection closed while reading from the driver",
        "websocket.send: connection is already closed",
    ],
)
def test_browser_loss_is_recognised(message):
    assert is_browser_gone(Exception(message))


def test_ordinary_failures_are_not_mistaken_for_browser_loss():
    assert not is_browser_gone(Exception("Timeout 30000ms exceeded"))
    assert not is_browser_gone(Exception("prompt input not found"))
