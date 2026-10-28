"""Projects: self-contained workspaces.

Each project owns its prompt source, its queue, its output folder and its
overrides of the global defaults. Two projects never share state, so a sheet
of 150 storyboard frames and a handful of one-off experiments cannot
deduplicate against each other or write into the same folder.

On disk:

    projects/<slug>/
        project.json    source + settings overrides
        state.db        this project's queue
        output/         this project's images, sidecars and manifest.csv
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, data_dir

def default_projects_dir() -> Path:
    """Where projects live — beside the code in a checkout, else per-user."""
    return data_dir() / "projects"

_SLUG = re.compile(r"[^a-z0-9]+")

# Settings a project may override. Anything not listed stays global.
OVERRIDABLE = {
    "flow": (
        "project_url",
        "expected_images",
        "min_images",
        "settle_seconds",
        "min_result_dimension",
        "generate_timeout_s",
    ),
    "run": ("delay_min_s", "delay_max_s", "max_attempts", "filename_template", "daily_image_cap",
              "concurrency", "poll_interval_s", "claim_existing", "sweep_after_s"),
    "video": (
        "tts_engine", "voice", "rate", "piper_model", "padding_s",
        "fallback_duration_s", "width", "height", "fps", "burn_captions",
        "caption_font", "caption_size", "ken_burns", "zoom_strength",
        "per_scene", "crf", "preset",
        "caption_preset", "caption_colour", "caption_outline", "caption_highlight",
        "caption_box", "caption_outline_px", "caption_position", "caption_margin",
        "caption_animation", "transition", "transition_s",
        "music", "music_db", "ducking", "music_fade_s",
        "animate_rows",
        "animation_framing", "animation_camera", "animation_backdrop",
    ),
    "thumbnail": ("blocks", "width", "height"),
}


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")[:48] or "project"


class Project:
    def __init__(self, root: Path, data: dict):
        self.root = root
        self.data = data

    # ---- identity -------------------------------------------------------

    @property
    def slug(self) -> str:
        return self.root.name

    @property
    def name(self) -> str:
        return self.data.get("name", self.slug)

    @name.setter
    def name(self, value: str) -> None:
        self.data["name"] = value

    @property
    def db_path(self) -> Path:
        return self.root / "state.db"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def source(self) -> dict:
        return self.data.setdefault("source", {})

    @property
    def overrides(self) -> dict:
        return self.data.setdefault("settings", {})

    # ---- persistence ----------------------------------------------------

    @classmethod
    def create(cls, name: str, projects_dir: Path | None = None) -> "Project":
        projects_dir = projects_dir or default_projects_dir()
        projects_dir.mkdir(parents=True, exist_ok=True)
        slug = base = slugify(name)
        n = 2
        while (projects_dir / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        root = projects_dir / slug
        (root / "output").mkdir(parents=True)
        project = cls(
            root,
            {
                "name": name,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source": {"kind": "none"},
                "settings": {},
            },
        )
        project.save()
        return project

    @classmethod
    def load(cls, root: Path) -> "Project":
        data = json.loads((root / "project.json").read_text())
        return cls(root, data)

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "project.json").write_text(json.dumps(self.data, indent=2))

    def duplicate(self, name: str, projects_dir: Path = None) -> "Project":
        """Copy the configuration into a new project, without the results.

        Settings and source are what is worth reusing; the queue and the
        images belong to the run that produced them.
        """
        target = Project.create(name, projects_dir or self.root.parent)
        target.data["source"] = dict(self.source)
        target.data["settings"] = {k: dict(v) for k, v in self.overrides.items()}
        target.save()
        return target

    def delete(self) -> None:
        """Remove the project and everything it generated."""
        shutil.rmtree(self.root)

    # ---- config ---------------------------------------------------------

    def config(self, base: Config) -> Config:
        """Base settings with this project's source and overrides applied."""
        source = self.source
        sheets = replace(
            base.sheets,
            mode=source.get("mode", base.sheets.mode),
            spreadsheet_id=source.get("spreadsheet_id", ""),
            worksheet=source.get("worksheet", base.sheets.worksheet),
            prompt_column=source.get("prompt_column", base.sheets.prompt_column),
            file_path=source.get("file_path", ""),
        )
        flow = replace(base.flow, **self._picked("flow"))
        video = replace(base.video, **self._picked("video"))
        run = replace(
            base.run,
            output_dir=self.output_dir,
            db_path=self.db_path,
            **self._picked("run"),
        )
        picked = self._picked("thumbnail")
        if "blocks" in picked:
            # They are stored as plain dicts in project.json; _block_fields
            # drops anything TextBlock does not know, so a hand-edited file
            # cannot stop the app from starting.
            from .config import TextBlock, _block_fields

            picked["blocks"] = tuple(
                TextBlock(**_block_fields(b)) for b in (picked["blocks"] or [])
            )
        thumbnail = replace(base.thumbnail, **picked)
        return replace(
            base, sheets=sheets, flow=flow, run=run, video=video,
            thumbnail=thumbnail,
        )

    def _picked(self, section: str) -> dict:
        stored = self.overrides.get(section, {})
        return {k: v for k, v in stored.items() if k in OVERRIDABLE[section]}


def prepare(project: "Project") -> dict:
    """Make a project usable before it is read.

    Recorded image and audio paths are absolute, so a project carried to
    another machine - or to another user account, or restored from a
    backup - points every record at a directory that does not exist
    there. Nothing errors: the gallery and the export skip missing files
    silently, and the first thing that cannot skip is the video builder,
    by which point it looks like the images were never generated.

    The check is cheap and the scan only runs when something is actually
    missing, so this is safe to call every time a project is opened.
    """
    from .db import JobStore

    store = JobStore(project.db_path)
    try:
        if not store.paths_are_broken():
            return {}
        repaired, lost = store.repair_image_paths(project.output_dir)
        moved, cleared = store.repair_audio_paths(project.output_dir)
    finally:
        store.close()

    report = {
        "images_repaired": repaired, "images_lost": lost,
        "audio_moved": moved, "audio_cleared": cleared,
    }
    if any(report.values()):
        project.data["last_repair"] = report
        project.save()
    return report


def list_projects(projects_dir: Path | None = None) -> list[Project]:
    projects_dir = projects_dir or default_projects_dir()
    if not projects_dir.exists():
        return []
    found = [
        Project.load(child)
        for child in sorted(projects_dir.iterdir())
        if (child / "project.json").exists()
    ]
    return sorted(found, key=lambda p: p.name.lower())


def ensure_default(base: Config, projects_dir: Path | None = None) -> Project:
    """Guarantee at least one project exists, adopting any pre-project state.

    The app previously kept a single state.db and output/ at the repo root.
    Those are moved into the first project rather than abandoned, so an
    existing queue keeps its record of what has already been generated — and
    does not pay to generate it again.
    """
    projects_dir = projects_dir or default_projects_dir()
    existing = list_projects(projects_dir)
    if existing:
        return existing[0]

    project = Project.create("Default", projects_dir)
    if base.sheets.spreadsheet_id:
        project.source.update(
            {
                "kind": "google_sheet",
                "mode": base.sheets.mode,
                "spreadsheet_id": base.sheets.spreadsheet_id,
                "worksheet": base.sheets.worksheet,
                "prompt_column": base.sheets.prompt_column,
            }
        )

    if base.run.db_path.exists() and not project.db_path.exists():
        shutil.move(str(base.run.db_path), str(project.db_path))
    legacy_output = base.run.output_dir
    if legacy_output.exists() and legacy_output.is_dir():
        for item in legacy_output.iterdir():
            target = project.output_dir / item.name
            if not target.exists():
                shutil.move(str(item), str(target))

    # Moving the output folder invalidates every absolute path recorded with
    # a job, so put them right immediately rather than leaving the next
    # feature to trip over it.
    from .db import JobStore

    store = JobStore(project.db_path)
    try:
        repaired, lost = store.repair_image_paths(project.output_dir)
        moved_audio, cleared_audio = store.repair_audio_paths(project.output_dir)
    finally:
        store.close()
    if repaired or lost or moved_audio or cleared_audio:
        project.data["last_repair"] = {
            "images_repaired": repaired, "images_lost": lost,
            "audio_moved": moved_audio, "audio_cleared": cleared_audio,
        }

    project.save()
    return project
