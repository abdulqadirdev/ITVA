"""Delivery shaped by mood, and narration that survives a moved folder."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen import emotion  # noqa: E402
from imgen.db import JobStore, Status  # noqa: E402


# ---- emotion -------------------------------------------------------------

def test_every_named_emotion_resolves():
    for name in emotion.names():
        assert emotion.resolve(name) is not None


def test_sad_is_slower_and_lower_than_angry():
    """The whole approach rests on pace and pitch carrying the mood, so the
    directions have to be right."""
    sad_rate, sad_pitch, _ = emotion.apply("sad")
    angry_rate, angry_pitch, _ = emotion.apply("angry")
    assert sad_rate < 0 < angry_rate
    assert sad_pitch < angry_pitch


def test_whisper_is_much_quieter_and_shout_much_louder():
    assert emotion.apply("whisper")[2] < -30
    assert emotion.apply("shout")[2] > 20


def test_neutral_changes_nothing():
    assert emotion.apply("neutral") == (0, 0, 0)


@pytest.mark.parametrize(
    "written, same_as",
    [("furious", "angry"), ("sleepy", "tired"), ("crying", "sad"),
     ("whispering", "whisper"), ("JOYFUL", "happy"), ("  mad  ", "angry")],
)
def test_the_words_people_actually_type_are_understood(written, same_as):
    assert emotion.apply(written) == emotion.apply(same_as)


def test_an_unknown_mood_is_ignored_rather_than_breaking_the_row():
    assert emotion.apply("interpretive dance") == (0, 0, 0)


def test_offsets_stack_on_the_project_settings_but_stay_in_range():
    assert emotion.apply("happy", rate=10)[0] == 18
    # edge-tts rejects extremes, and they sound wrong well before that.
    assert emotion.apply("shout", rate=95)[0] <= 100
    assert emotion.apply("whisper", volume=-80)[2] >= -90


# ---- narration survives --------------------------------------------------

def _finished_row(tmp_path, audio: Path):
    store = JobStore(tmp_path / "t.db")
    store.sync_prompts([(2, "a cat")])
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["a.jpg"])
    store.set_audio(job.id, str(audio), 3.0, [[0.0, 0.5, "a"]])
    return store, job


def test_narration_is_relocated_when_the_folder_moves(tmp_path):
    old = tmp_path / "old" / "line.mp3"
    old.parent.mkdir()
    old.write_bytes(b"x")
    store, _ = _finished_row(tmp_path, old)

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    old.rename(new_dir / "line.mp3")

    assert store.repair_audio_paths(new_dir) == (1, 0)
    assert Path(store.all_jobs()[0].audio_path).exists()


def test_narration_that_cannot_be_found_is_cleared_not_left_dangling(tmp_path):
    """A dangling path made the row look narrated, so it was skipped and the
    clip came out silent. Clearing it means it is simply spoken again."""
    store, _ = _finished_row(tmp_path, tmp_path / "gone" / "line.mp3")
    assert store.repair_audio_paths(tmp_path / "empty") == (0, 1)

    job = store.all_jobs()[0]
    assert job.audio_path is None and job.duration_s is None
    assert job.word_times == []


def test_word_timings_round_trip(tmp_path):
    audio = tmp_path / "line.mp3"
    audio.write_bytes(b"x")
    store, _ = _finished_row(tmp_path, audio)
    assert store.all_jobs()[0].word_times == [[0.0, 0.5, "a"]]


# ---- music ----------------------------------------------------------------

def test_ducking_splits_the_narration_before_using_it_as_a_key():
    """A sidechain input cannot also be the output, so the voice has to be
    duplicated or ffmpeg fails with a filter graph error."""
    import inspect

    from imgen import video

    body = inspect.getsource(video.add_music)
    assert "asplit=2" in body
    assert "sidechaincompress" in body


def test_music_is_looped_so_a_short_track_still_covers_the_video():
    import inspect

    from imgen import video

    assert "-stream_loop" in inspect.getsource(video.add_music)
