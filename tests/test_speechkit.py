"""Pronunciation fixes and nonverbal clips."""

import shutil
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen.speechkit import (  # noqa: E402
    apply_pronunciation,
    available_nonverbals,
    load_pronunciation,
    splice,
    split_tags,
)


# ---- pronunciation -------------------------------------------------------

def test_rules_are_read_and_comments_ignored(tmp_path):
    path = tmp_path / "p.txt"
    path.write_text("# how to say things\nGPT = gee pee tee\n\nAI=ay eye\nbroken line\n")
    assert load_pronunciation(path) == {"GPT": "gee pee tee", "AI": "ay eye"}


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_pronunciation(tmp_path / "nope.txt") == {}


def test_only_whole_words_are_replaced():
    """Replacing inside words would turn "Flowing" into "flohing"."""
    out = apply_pronunciation("Flow and Flowing and inflow", {"Flow": "floh"})
    assert out == "floh and Flowing and inflow"


def test_replacement_ignores_case_but_longest_rule_wins():
    rules = {"AI": "ay eye", "AI model": "ay eye model"}
    assert apply_pronunciation("an ai model here", rules) == "an ay eye model here"


def test_no_rules_leaves_the_text_alone():
    assert apply_pronunciation("unchanged", {}) == "unchanged"


# ---- nonverbal tags ------------------------------------------------------

@pytest.fixture
def clips(tmp_path):
    folder = tmp_path / "nonverbals"
    folder.mkdir()
    for name in ("laugh.mp3", "sigh.wav", "Deep-Breath.mp3"):
        (folder / name).write_bytes(b"x")
    (folder / "notes.txt").write_text("ignored")
    return available_nonverbals(folder)


def test_only_audio_files_count(clips):
    assert set(clips) == {"laugh", "sigh", "deep-breath"}


@pytest.mark.parametrize("written", ["[laugh]", "(laugh)", "[ laugh ]", "[LAUGH]"])
def test_a_tag_is_recognised_however_it_is_written(written, clips):
    spoken, found = split_tags(f"Before {written} after.", clips)
    assert spoken == "Before after."
    assert [c.name for c in found] == ["laugh"]


def test_a_tag_with_no_clip_is_left_in_place(clips):
    """Dropping it silently would hide the fact that nothing happened."""
    spoken, found = split_tags("He [cough] stopped.", clips)
    assert "[cough]" in spoken and found == []


def test_several_tags_in_one_line(clips):
    spoken, found = split_tags("[laugh] Well. (sigh) Fine.", clips)
    assert spoken == "Well. Fine."
    assert [c.name for c in found] == ["laugh", "sigh"]


def test_a_line_that_is_only_a_tag_leaves_nothing_to_speak(clips):
    spoken, found = split_tags("[laugh]", clips)
    assert spoken == "" and len(found) == 1


# ---- splicing ------------------------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_a_clip_is_joined_onto_the_end_of_the_speech(tmp_path):
    import subprocess

    from imgen.speechkit import Nonverbal

    ffmpeg = Path(shutil.which("ffmpeg"))

    def tone(name: str, seconds: float) -> Path:
        path = tmp_path / name
        subprocess.run(
            [str(ffmpeg), "-y", "-f", "lavfi",
             "-i", f"sine=frequency=440:duration={seconds}", str(path)],
            capture_output=True, timeout=60,
        )
        return path

    speech, extra = tone("speech.wav", 1.0), tone("laugh.wav", 0.5)
    out = splice(speech, [Nonverbal("laugh", extra)], tmp_path / "joined.wav", ffmpeg)

    with wave.open(str(out)) as handle:
        joined = handle.getnframes() / handle.getframerate()
    assert joined == pytest.approx(1.5, abs=0.1), "both parts should be present"


def test_splicing_nothing_returns_the_speech_untouched(tmp_path):
    original = tmp_path / "a.wav"
    original.write_bytes(b"x")
    assert splice(original, [], tmp_path / "b.wav", Path("/bin/true")) == original
