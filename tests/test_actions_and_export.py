"""Per-row actions and exporting."""

import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen import export  # noqa: E402
from imgen.db import JobStore, Status  # noqa: E402


def seeded(tmp_path, prompts=("a cat", "a dog")) -> JobStore:
    store = JobStore(tmp_path / "t.db")
    store.sync_prompts(list(enumerate(prompts, start=2)))
    return store


def test_regenerate_revives_a_completed_job(tmp_path):
    store = seeded(tmp_path, ("a cat",))
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["old.jpg"])
    assert store.claim_next(3) is None, "a done job must not be picked up again"

    assert store.requeue_ids([job.id]) == 1
    revived = store.claim_next(3)
    assert revived is not None
    assert revived.attempts == 1, "attempts reset before the new try"
    assert revived.image_paths == [], "the stale result is cleared"


def test_regenerate_ignores_an_empty_selection(tmp_path):
    assert seeded(tmp_path).requeue_ids([]) == 0


def test_skip_takes_jobs_out_of_the_queue(tmp_path):
    store = seeded(tmp_path)
    jobs = store.all_jobs()
    assert store.skip_ids([j.id for j in jobs]) == 2
    assert store.claim_next(3) is None
    assert store.counts() == {"skipped": 2}


def test_pending_count_matches_what_a_run_would_claim(tmp_path):
    store = seeded(tmp_path, ("a", "b", "c"))
    assert store.pending_count(3) == 3
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["x.jpg"])
    assert store.pending_count(3) == 2

    failing = store.claim_next(3)
    store.requeue(failing.id, "boom")
    assert store.pending_count(3) == 2, "a retryable failure still counts"
    assert store.pending_count(1) == 1, "but not once it is out of attempts"


def test_done_with_images_only_returns_jobs_that_produced_something(tmp_path):
    store = seeded(tmp_path)
    first, second = store.claim_next(3), store.claim_next(3)
    store.finish(first.id, Status.DONE, image_paths=["a.jpg"])
    store.finish(second.id, Status.BLOCKED, error="policy")
    assert [j.prompt for j in store.done_with_images()] == ["a cat"]


def test_zip_export_contains_images_and_an_index(tmp_path):
    store = seeded(tmp_path, ("a cat",))
    image = tmp_path / "cat.jpg"
    image.write_bytes(b"\xff\xd8fake")
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=[str(image)])

    target = tmp_path / "out" / "export.zip"
    images, count = export.export_zip(store.done_with_images(), target)
    assert (images, count) == (1, 1)

    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        assert "images/cat.jpg" in names and "index.csv" in names
        index = archive.read("index.csv").decode()
    assert "a cat" in index and "cat.jpg" in index


def test_export_skips_files_that_no_longer_exist(tmp_path):
    store = seeded(tmp_path, ("a cat",))
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=[str(tmp_path / "deleted.jpg")])
    images, count = export.export_zip(store.done_with_images(), tmp_path / "e.zip")
    assert (images, count) == (0, 1), "a missing file must not abort the export"


def test_csv_export_quotes_prompts_containing_commas(tmp_path):
    store = seeded(tmp_path, ("a cat, sitting",))
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["x.jpg"])
    target = tmp_path / "index.csv"
    export.export_csv(store.done_with_images(), target)

    import csv as csvmod

    rows = list(csvmod.reader(target.open()))
    assert rows[1][1] == "a cat, sitting"


def test_editing_a_prompt_requeues_it_under_the_new_text(tmp_path):
    store = seeded(tmp_path, ("a cat",))
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["old.jpg"])

    assert store.update_prompt(job.id, "a cat wearing a hat") is True
    revived = store.claim_next(3)
    assert revived.prompt == "a cat wearing a hat"
    assert revived.image_paths == []

    # Dedupe now follows the new text, not the old one.
    store.sync_prompts([(2, "a cat")])
    assert len(store.all_jobs()) == 2


def test_editing_into_an_existing_prompt_is_refused(tmp_path):
    store = seeded(tmp_path, ("a cat", "a dog"))
    cat = next(j for j in store.all_jobs() if j.prompt == "a cat")
    assert store.update_prompt(cat.id, "a dog") is False, "would merge two rows"
    assert store.update_prompt(cat.id, "   ") is False


def test_set_images_replaces_the_recorded_variants(tmp_path):
    store = seeded(tmp_path, ("a cat",))
    job = store.claim_next(3)
    store.finish(job.id, Status.DONE, image_paths=["a.jpg", "b.jpg", "c.jpg"])
    store.set_images(job.id, ["b.jpg"])
    assert store.all_jobs()[0].image_paths == ["b.jpg"]
    store.set_images(job.id, [])
    assert store.all_jobs()[0].image_paths == []
