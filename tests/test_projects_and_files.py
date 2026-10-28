"""Project isolation and local-file prompt sources."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen import projects, sources  # noqa: E402
from imgen.config import load_config  # noqa: E402
from imgen.sheets import SheetError  # noqa: E402
from tests.test_csv_source import make_cfg  # noqa: E402


# ---- projects -----------------------------------------------------------


def test_each_project_gets_its_own_queue_and_output(tmp_path):
    base = load_config()
    a = projects.Project.create("Storyboard", tmp_path)
    b = projects.Project.create("Experiments", tmp_path)

    ca, cb = a.config(base), b.config(base)
    assert ca.run.db_path != cb.run.db_path
    assert ca.run.output_dir != cb.run.output_dir
    assert ca.run.db_path.parent == a.root


def test_same_name_twice_does_not_collide(tmp_path):
    first = projects.Project.create("Ads", tmp_path)
    second = projects.Project.create("Ads", tmp_path)
    assert first.root != second.root
    assert len(projects.list_projects(tmp_path)) == 2


def test_overrides_apply_and_unknown_keys_are_ignored(tmp_path):
    base = load_config()
    project = projects.Project.create("Tuned", tmp_path)
    project.overrides["run"] = {"delay_min_s": 1.0, "nonsense": 99}
    project.overrides["flow"] = {"settle_seconds": 20}

    cfg = project.config(base)
    assert cfg.run.delay_min_s == 1.0
    assert cfg.flow.settle_seconds == 20
    assert cfg.run.delay_max_s == base.run.delay_max_s  # untouched


def test_project_survives_a_save_and_reload(tmp_path):
    project = projects.Project.create("Round trip", tmp_path)
    project.source.update({"kind": "google_sheet", "mode": "csv", "spreadsheet_id": "X1"})
    project.save()

    reloaded = projects.Project.load(project.root)
    assert reloaded.name == "Round trip"
    assert reloaded.source["spreadsheet_id"] == "X1"


def test_delete_removes_everything_it_generated(tmp_path):
    project = projects.Project.create("Temp", tmp_path)
    (project.output_dir / "a.jpg").write_bytes(b"x")
    project.delete()
    assert not project.root.exists()
    assert projects.list_projects(tmp_path) == []


# ---- file sources -------------------------------------------------------


def test_csv_file_rows_keep_their_line_numbers(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text("Prompt,Note\na cat,x\n\na dog,y\n")
    source = sources.FileSource.connect(make_cfg(mode="file", file_path=str(path)))
    assert source.read_prompts() == [(2, "a cat"), (4, "a dog")]
    assert source.writable is False


def test_txt_file_is_one_prompt_per_line_ignoring_comments(tmp_path):
    path = tmp_path / "p.txt"
    path.write_text("# header\na cat\n\na dog\n")
    source = sources.FileSource.connect(make_cfg(mode="file", file_path=str(path)))
    assert source.read_prompts() == [(2, "a cat"), (4, "a dog")]


def test_xlsx_file_is_read_by_header_name(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "p.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Prompts"
    sheet.append(["scene", "Image Prompt"])   # different case on purpose
    sheet.append(["one", "a cat"])
    sheet.append(["two", "a dog"])
    book.save(path)

    cfg = make_cfg(
        mode="file", file_path=str(path), prompt_column="image prompt", worksheet="Prompts"
    )
    assert sources.FileSource.connect(cfg).read_prompts() == [(2, "a cat"), (3, "a dog")]
    assert sources.sheet_tabs(cfg) == ["Prompts"]


def test_missing_file_and_bad_type_are_reported_clearly(tmp_path):
    with pytest.raises(SheetError, match="file not found"):
        sources.FileSource.connect(make_cfg(mode="file", file_path=str(tmp_path / "nope.csv")))

    bad = tmp_path / "p.pdf"
    bad.write_bytes(b"%PDF")
    with pytest.raises(SheetError, match="unsupported file type"):
        sources.FileSource.connect(make_cfg(mode="file", file_path=str(bad)))


def test_missing_prompt_column_names_what_was_found(tmp_path):
    path = tmp_path / "p.csv"
    path.write_text("Idea,Note\na cat,x\n")
    with pytest.raises(SheetError, match="Idea"):
        sources.FileSource.connect(make_cfg(mode="file", file_path=str(path)))


def test_a_project_carried_from_another_machine_finds_its_images(tmp_path):
    """Recorded paths are absolute. Copied to another machine - or another
    user account, or restored from a backup - every one of them points at
    a directory that is not there. Nothing errors: the gallery and the
    export skip missing files quietly, and the first thing that cannot
    skip is the video builder, by which point it looks as though the
    images were never generated.
    """
    from imgen.db import JobStore
    from imgen.projects import Project, prepare
    from imgen.sheets import PromptRow

    projects_dir = tmp_path / "projects"
    project = Project.create("Carried", projects_dir)

    picture = project.output_dir / "0002_a-scene_1.jpg"
    picture.write_bytes(b"\xff\xd8\xff\xe0 jpeg")
    narration = project.output_dir / "narration" / "0002.mp3"
    narration.parent.mkdir(parents=True, exist_ok=True)
    narration.write_bytes(b"id3")

    store = JobStore(project.db_path)
    try:
        store.sync_prompts([PromptRow(2, "a scene", {})])
        job = store.all_jobs()[0]
        # ...as written on the machine it came from.
        store.set_images(job.id, ["/home/someone-else/projects/default/output/"
                                  "0002_a-scene_1.jpg"])
        store.set_audio(
            job.id, "/home/someone-else/projects/default/output/narration/0002.mp3", 2.5
        )
        assert store.paths_are_broken()
    finally:
        store.close()

    report = prepare(project)
    assert report["images_repaired"] == 1
    assert report["audio_moved"] == 1

    store = JobStore(project.db_path)
    try:
        assert not store.paths_are_broken()
        job = store.all_jobs()[0]
        assert Path(job.image_paths[0]) == picture
        assert Path(job.audio_path) == narration
    finally:
        store.close()


def test_preparing_a_healthy_project_does_nothing(tmp_path):
    """It runs on every open, so it must cost nothing when all is well."""
    from imgen.db import JobStore
    from imgen.projects import Project, prepare
    from imgen.sheets import PromptRow

    project = Project.create("Fine", tmp_path / "projects")
    picture = project.output_dir / "a.jpg"
    picture.write_bytes(b"x")
    store = JobStore(project.db_path)
    try:
        store.sync_prompts([PromptRow(2, "a scene", {})])
        store.set_images(store.all_jobs()[0].id, [str(picture)])
    finally:
        store.close()

    assert prepare(project) == {}
    assert "last_repair" not in project.data
