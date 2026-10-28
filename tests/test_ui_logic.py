"""UI logic that is worth testing without clicking anything."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen.config import load_config  # noqa: E402
from imgen.db import JobStore, Status  # noqa: E402


# ---- sheet links --------------------------------------------------------

@pytest.mark.parametrize(
    "given, expected",
    [
        ("https://docs.google.com/spreadsheets/d/ABC123/edit?usp=sharing", "ABC123"),
        ("https://docs.google.com/spreadsheets/d/ABC123/edit#gid=0", "ABC123"),
        ("https://docs.google.com/spreadsheets/d/ABC123", "ABC123"),
        ("  ABC123  ", "ABC123"),
        ("", ""),
        ("https://example.com/not-a-sheet", ""),
    ],
)
def test_sheet_id_is_pulled_out_of_whatever_the_user_pastes(qapp, given, expected):
    from ui.source_panel import extract_sheet_id

    assert extract_sheet_id(given) == expected


# ---- queue filtering ----------------------------------------------------

@pytest.fixture
def panel(qapp, keep_alive, tmp_path):
    from ui.queue_panel import QueuePanel

    store = JobStore(tmp_path / "t.db")
    store.sync_prompts([(2, "a sleepy cat"), (3, "a running dog"), (4, "a cat on a mat")])
    jobs = store.all_jobs()
    store.finish(jobs[0].id, Status.DONE, image_paths=["a.jpg"])
    store.finish(jobs[1].id, Status.BLOCKED, error="policy refusal")
    jobs = store.all_jobs()
    counts = store.counts()
    store.close()

    widget = keep_alive(QueuePanel())
    widget.set_jobs(jobs, counts)
    return widget


def test_search_matches_prompts(panel):
    panel.search.setText("cat")
    assert panel.proxy.rowCount() == 2


def test_search_also_matches_error_text(panel):
    panel.search.setText("policy")
    assert panel.proxy.rowCount() == 1


def test_search_is_case_insensitive(panel):
    panel.search.setText("SLEEPY")
    assert panel.proxy.rowCount() == 1


def test_status_filter_narrows_to_one_status(panel):
    panel.status_box.setCurrentText("done")
    assert panel.proxy.rowCount() == 1
    panel.status_box.setCurrentIndex(0)
    assert panel.proxy.rowCount() == 3


def test_limit_defaults_to_the_pending_backlog(panel):
    # One pending of three; done and blocked are terminal.
    assert panel.limit.value() == 1


def test_progress_reports_an_estimate_then_clears_at_the_end(panel):
    import time

    panel.set_running(True, total=10)
    assert panel.progress.maximum() == 10
    panel._run_started = time.monotonic() - 60
    panel.set_progress(2, 10)
    assert "left" in panel.eta.text()
    panel.set_progress(10, 10)
    assert panel.eta.text() == "", "no estimate once everything is done"


# ---- settings preview ---------------------------------------------------

def test_filename_preview_updates_and_reports_bad_tokens(qapp, keep_alive, tmp_path):
    from imgen import projects
    from ui.settings_panel import SettingsPanel

    base = load_config()
    project = projects.Project.create("Preview", tmp_path)
    widget = keep_alive(SettingsPanel(base))
    widget.set_project(project)

    field = widget.widgets[("run", "filename_template")]
    field.setText("{scene}/{seq:03}_{prompt}")
    assert "2am-hook/007" in widget.name_preview.text()

    field.setText("{nonsense}")
    assert "unknown token" in widget.name_preview.text()


def test_thumbnail_blocks_survive_the_round_trip(qapp, tmp_path):
    """Edited in the panel, stored as dicts in project.json, read back as
    TextBlock. Each hop is a place the list could quietly become empty."""
    from dataclasses import asdict

    from imgen.config import TextBlock
    from ui.thumbnail_panel import ThumbnailPanel

    seen = []
    panel = ThumbnailPanel(load_config())
    panel.blocks_changed.connect(seen.append)

    panel.set_blocks([TextBlock(text="SHOUT", size=100)])
    assert panel.list.count() == 1
    assert seen[-1][0]["text"] == "SHOUT"

    panel.list.setCurrentRow(0)
    panel._set(size=133, colour="#FF0000")
    assert seen[-1][0]["size"] == 133
    assert seen[-1][0]["colour"] == "#FF0000"

    # Adding uses the chosen preset rather than a bare default.
    panel.preset.setCurrentText("Badge")
    panel._add()
    assert panel.list.count() == 2
    assert seen[-1][1]["box"], "a badge has a slab behind it"

    # Order matters: later blocks paint over earlier ones.
    panel.list.setCurrentRow(1)
    panel._move(-1)
    assert seen[-1][0]["box"]

    panel._remove()
    assert panel.list.count() == 1
    assert len(seen[-1]) == 1

    # And the dicts are exactly what TextBlock accepts back.
    assert TextBlock(**seen[-1][0]) == TextBlock(**asdict(TextBlock(**seen[-1][0])))


def test_removing_the_last_block_disables_the_editor(qapp):
    from imgen.config import TextBlock
    from ui.thumbnail_panel import ThumbnailPanel

    panel = ThumbnailPanel(load_config())
    panel.set_blocks([TextBlock(text="only one")])
    panel.list.setCurrentRow(0)
    assert panel.text.isEnabled()
    panel._remove()
    assert not panel.text.isEnabled(), "nothing selected, nothing to edit"
    assert panel.list.count() == 0


def test_the_update_banner_only_appears_when_there_is_something(qapp):
    """Hidden by default, and after "Later" it stays hidden. A strip that
    is always there is one people stop reading."""
    from imgen.update import Release
    from ui.update_banner import UpdateBanner

    banner = UpdateBanner()
    assert not banner.isVisibleTo(banner)

    release = Release("1.2.0", "v1.2.0", "notes here", "https://x/a.deb",
                      "a.deb", 90_000_000)
    banner.offer(release)
    assert banner.isVisibleTo(banner)
    assert "1.2.0" in banner.message.text()
    assert "90 MB" in banner.message.text()
    assert banner.btn_notes.isVisibleTo(banner)

    banner.btn_later.click()
    assert not banner.isVisibleTo(banner)


def test_a_release_without_notes_offers_no_empty_dialog(qapp):
    from imgen.update import Release
    from ui.update_banner import UpdateBanner

    banner = UpdateBanner()
    banner.offer(Release("1.2.0", "v1.2.0", "", "u", "a.deb", 1))
    assert not banner.btn_notes.isVisibleTo(banner)


def test_the_update_button_is_dead_while_installing(qapp):
    """Clicking twice would start a second download and a second
    password prompt on top of the first."""
    from imgen.update import Release
    from ui.update_banner import UpdateBanner

    banner = UpdateBanner()
    banner.offer(Release("1.2.0", "v1.2.0", "", "u", "a.deb", 1))
    banner.set_busy("Downloading...")
    assert not banner.btn_install.isEnabled()
    assert banner.progress.isVisibleTo(banner)

    banner.set_progress(0.5)
    assert banner.progress.value() == 50

    banner.set_installed()
    assert banner.btn_install.isEnabled()
    assert "Restart" in banner.btn_install.text()


def test_a_failed_update_says_so_and_lets_you_retry(qapp):
    from imgen.update import Release
    from ui.update_banner import UpdateBanner

    banner = UpdateBanner()
    banner.offer(Release("1.2.0", "v1.2.0", "", "u", "a.deb", 1))
    banner.set_busy("Downloading...")
    banner.set_failed("apt failed:\nE: dpkg was interrupted")
    assert "Update failed" in banner.message.text()
    assert banner.btn_install.isEnabled(), "a failure must be retryable"
    assert not banner.progress.isVisibleTo(banner)
