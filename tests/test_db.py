"""Job-queue behaviour: dedupe, claiming, resume, retry policy."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen.db import JobStore, Status  # noqa: E402


def store(tmp_path) -> JobStore:
    return JobStore(tmp_path / "t.db")


def test_sync_is_idempotent_on_prompt_text(tmp_path):
    s = store(tmp_path)
    assert s.sync_prompts([(2, "a cat"), (3, "a dog")]) == (2, 0)
    # Same prompts, different rows (sheet reordered) -> nothing new.
    assert s.sync_prompts([(9, "a dog"), (10, "a cat")]) == (0, 2)
    assert len(s.all_jobs()) == 2


def test_blank_prompts_are_ignored(tmp_path):
    s = store(tmp_path)
    assert s.sync_prompts([(2, "   "), (3, ""), (4, "real")]) == (1, 0)


def test_completed_work_is_not_regenerated(tmp_path):
    s = store(tmp_path)
    s.sync_prompts([(2, "a cat")])
    job = s.claim_next(max_attempts=3)
    s.finish(job.id, Status.DONE, image_paths=["out/1.png"])

    s.sync_prompts([(2, "a cat")])          # sheet re-read on next run
    assert s.claim_next(max_attempts=3) is None
    assert s.all_jobs()[0].status is Status.DONE


def test_crash_mid_job_is_resumable(tmp_path):
    s = store(tmp_path)
    s.sync_prompts([(2, "a cat")])
    s.claim_next(max_attempts=3)            # now 'running', then we "crash"
    assert s.reset_stale_running() == 1
    assert s.claim_next(max_attempts=3) is not None


def test_failed_jobs_retry_until_the_cap(tmp_path):
    s = store(tmp_path)
    s.sync_prompts([(2, "a cat")])
    for expected_attempt in (1, 2, 3):
        job = s.claim_next(max_attempts=3)
        assert job is not None and job.attempts == expected_attempt
        s.requeue(job.id, "timeout")
    assert s.claim_next(max_attempts=3) is None, "should stop after max_attempts"


def test_blocked_prompts_are_never_retried(tmp_path):
    s = store(tmp_path)
    s.sync_prompts([(2, "nope")])
    job = s.claim_next(max_attempts=3)
    s.finish(job.id, Status.BLOCKED, error="policy")
    assert s.claim_next(max_attempts=3) is None


def test_retry_failed_clears_attempts(tmp_path):
    s = store(tmp_path)
    s.sync_prompts([(2, "a cat")])
    job = s.claim_next(max_attempts=1)
    s.requeue(job.id, "boom")
    assert s.claim_next(max_attempts=1) is None
    assert s.retry_failed() == 1
    job = s.claim_next(max_attempts=1)
    assert job is not None and job.attempts == 1


def test_a_locally_set_field_survives_a_resync(tmp_path):
    """The animation is chosen in the app, and the sheet has no such
    column. A re-sync used to replace the whole fields blob, so pressing
    Load wiped every choice."""
    import json

    from imgen.db import JobStore
    from imgen.sheets import PromptRow

    store = JobStore(tmp_path / "s.db")
    try:
        store.sync_prompts([PromptRow(2, "a prompt", {"scene": "hook"})])
        job = next(iter(store.conn.execute("SELECT * FROM jobs")))
        store.set_field(job["id"], "animation", "slap")

        # The source is read again and still has no animation column.
        store.sync_prompts([PromptRow(2, "a prompt", {"scene": "hook v2"})])

        fields = json.loads(
            next(iter(store.conn.execute("SELECT fields FROM jobs")))["fields"]
        )
        assert fields["animation"] == "slap", "the local choice must survive"
        assert fields["scene"] == "hook v2", "the source still wins for its own"

        # Clearing it in the app removes the key rather than storing "".
        store.set_field(job["id"], "animation", "")
        fields = json.loads(
            next(iter(store.conn.execute("SELECT fields FROM jobs")))["fields"]
        )
        assert "animation" not in fields
    finally:
        store.close()
