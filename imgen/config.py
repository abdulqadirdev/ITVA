"""Configuration loading.

Settings come from ``config/settings.toml``, optionally overlaid by a
git-ignored ``config/settings.local.toml``. Selectors live in their own file so
they can be repaired without touching code — see ``config/selectors.toml``.

Python on this machine is 3.10, which has no stdlib ``tomllib``; we use ``tomli``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import tomli

PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_ROOT.parent

#: True inside a PyInstaller build, where the package lives in a temporary
#: extraction directory that is deleted when the process exits.
FROZEN = getattr(sys, "frozen", False)

# Running from a checkout keeps everything beside the code, which is what you
# want while developing. An installed copy must not write into site-packages,
# so it uses the per-user directories instead. A frozen build is never a
# checkout, however the extracted layout happens to look — without this it
# would write projects and images into a folder that vanishes on exit.
DEV_MODE = not FROZEN and (SOURCE_ROOT / "config" / "settings.toml").exists()


def config_dir() -> Path:
    from .platforms import user_config_dir

    return SOURCE_ROOT / "config" if DEV_MODE else user_config_dir()


def data_dir() -> Path:
    from .platforms import app_data_dir

    return SOURCE_ROOT if DEV_MODE else app_data_dir()


def ensure_config() -> Path:
    """Make sure an editable settings file exists, seeding it on first run."""
    target = config_dir()
    target.mkdir(parents=True, exist_ok=True)
    for name in ("settings.toml", "selectors.toml"):
        destination = target / name
        if not destination.exists():
            packaged = PACKAGE_ROOT / "defaults" / name
            if packaged.exists():
                destination.write_text(packaged.read_text(encoding="utf-8"), encoding="utf-8")
    return target


# Kept for callers that only need somewhere to resolve relative paths against.
PROJECT_ROOT = SOURCE_ROOT

@dataclass(frozen=True)
class ChromeConfig:
    flatpak_app: str
    debug_port: int
    launch_timeout_s: int
    disable_extensions: bool
    binary: str = ""
    user_data_dir: str = ""
    extra_args: tuple[str, ...] = ()

    @property
    def host_user_data_dir(self) -> Path:
        """Absolute path of the automation profile, never Chrome's default."""
        if self.user_data_dir:
            from .platforms import expand

            return Path(expand(self.user_data_dir)).expanduser()
        from .platforms import automation_profile_dir, find_chrome

        return automation_profile_dir(
            find_chrome(self.binary, self.flatpak_app), self.flatpak_app
        )

    @property
    def cdp_base(self) -> str:
        return f"http://127.0.0.1:{self.debug_port}"


@dataclass(frozen=True)
class FlowConfig:
    app_url: str
    project_url: str
    signed_out_markers: list[str]
    expected_images: int
    min_images: int
    settle_seconds: int
    min_result_dimension: int
    generate_timeout_s: int

    @staticmethod
    def is_project(url: str) -> bool:
        """A project page is the only surface with a prompt box."""
        return "/project/" in url

    def looks_signed_out(self, url: str) -> bool:
        """Flow bounces signed-out visitors to its marketing page.

        That redirect is a far more reliable sign-out signal than hunting for a
        "Sign in" button in a UI that changes without notice.
        """
        return any(marker in url for marker in self.signed_out_markers)


@dataclass(frozen=True)
class SheetsConfig:
    mode: str
    service_account_file: Path
    spreadsheet_id: str
    worksheet: str
    prompt_column: str
    status_column: str
    paths_column: str
    generated_at_column: str
    error_column: str
    write_batch_size: int
    sequence_column: str = "sequence"
    scene_column: str = "scene"
    emotion_column: str = "emotion"
    voice_column: str = "voice"
    #: Which vector action a row plays instead of a still picture.
    animation_column: str = "animation"
    #: Describes the thumbnail picture. A column, not a row, so the
    #: thumbnail never turns into a clip in the video.
    thumbnail_column: str = "thumbnail"
    # Only used when mode == "file".
    file_path: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.spreadsheet_id)

    @property
    def writes_back(self) -> bool:
        return self.mode == "service_account"

    @property
    def csv_url(self) -> str:
        """Google's CSV export endpoint for a named worksheet."""
        from urllib.parse import quote

        return (
            f"https://docs.google.com/spreadsheets/d/{self.spreadsheet_id}"
            f"/gviz/tq?tqx=out:csv&sheet={quote(self.worksheet)}"
        )


@dataclass(frozen=True)
class RunConfig:
    output_dir: Path
    db_path: Path
    filename_template: str
    max_per_run: int
    concurrency: int
    poll_interval_s: float
    claim_existing: bool
    sweep_after_s: float
    confirm_above: int
    daily_image_cap: int
    run_until_empty: bool
    log_max_kb: int
    delay_min_s: float
    delay_max_s: float
    max_attempts: int


@dataclass(frozen=True)
class VideoConfig:
    tts_engine: str
    voice: str
    rate: int
    piper_model: str
    padding_s: float
    fallback_duration_s: float
    width: int
    height: int
    fps: int
    burn_captions: bool
    caption_font: str
    caption_size: int
    caption_preset: str
    caption_colour: str
    caption_outline: str
    caption_highlight: str
    caption_box: bool
    caption_outline_px: int
    caption_position: str
    caption_margin: int
    caption_animation: str
    transition: str
    transition_s: float
    music: str
    music_db: float
    ducking: bool
    music_fade_s: float
    ken_burns: bool
    zoom_strength: float
    per_scene: bool
    crf: int
    preset: str
    #: Off ignores the animation column entirely, so a filled sheet can
    #: still be built as plain stills without emptying the column.
    animate_rows: bool = True
    #: Staging for rows that name an action in the animation column.
    animation_framing: str = "full"
    animation_camera: str = "none"
    #: "image" puts the character over the row's own picture;
    #: any backdrop name (plain, night, cave, ...) uses that instead.
    animation_backdrop: str = "image"
    ffmpeg: str = ""
    ffprobe: str = ""


@dataclass(frozen=True)
class Selectors:
    """Ordered candidate selector lists, keyed by logical element name."""

    groups: dict[str, list[str]] = field(default_factory=dict)

    def get(self, name: str) -> list[str]:
        try:
            return self.groups[name]
        except KeyError as exc:  # pragma: no cover - config error path
            raise KeyError(
                f"No selector group '{name}' in config/selectors.toml. "
                f"Known groups: {sorted(self.groups)}"
            ) from exc


@dataclass(frozen=True)
class TextBlock:
    """One run of words on the thumbnail, with its own look.

    A thumbnail reads as one shout plus a smaller remark, so the blocks
    are separate rather than one styled string: each carries its own font,
    size, colour and placement, and they may overlap on purpose.
    """

    text: str = ""
    font: str = "Arial Black"
    size: int = 110
    colour: str = "#FFFFFF"
    outline: str = "#000000"
    outline_px: int = 10
    shadow: bool = True
    #: left | centre | right
    align: str = "centre"
    #: top | middle | bottom
    position: str = "middle"
    #: Nudge from that anchor, in pixels, so blocks can sit off the grid.
    dx: int = 0
    dy: int = 0
    rotate: float = 0.0
    uppercase: bool = True
    italic: bool = False
    weight: int = 900
    #: Letter spacing in em. Tight is what display type wants.
    tracking: float = -0.01
    line_height: float = 1.04
    #: A solid slab behind the words. "" means none.
    box: str = ""
    box_pad: int = 16
    #: Fraction of the width this block may fill before it wraps.
    text_width: float = 0.86


@dataclass(frozen=True)
class ThumbnailConfig:
    width: int = 1280
    height: int = 720
    blocks: tuple[TextBlock, ...] = ()


@dataclass(frozen=True)
class UpdateConfig:
    """Where to look for a newer build."""

    #: "owner/name" on GitHub. Empty disables the whole feature.
    repo: str = ""
    check_on_launch: bool = True
    #: Only for a private repository. Set it on the machine, never in a
    #: build: a token inside the app belongs to whoever holds the app.
    token: str = ""


@dataclass(frozen=True)
class Config:
    chrome: ChromeConfig
    flow: FlowConfig
    sheets: SheetsConfig
    run: RunConfig
    video: VideoConfig
    thumbnail: ThumbnailConfig
    update: UpdateConfig
    selectors: Selectors


def _block_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Only the keys TextBlock knows, coerced to its types.

    The blocks are hand-editable in settings.toml and in project.json, so
    an unknown or mistyped key must be ignored rather than stop the app
    from starting.
    """
    types = {f.name: f.type for f in fields(TextBlock)}
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if key not in types:
            continue
        want = types[key]
        try:
            if want == "int":
                out[key] = int(value)
            elif want == "float":
                out[key] = float(value)
            elif want == "bool":
                out[key] = bool(value)
            else:
                out[key] = str(value)
        except (TypeError, ValueError):
            continue
    return out


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as fh:
        return tomli.load(fh)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve(path_value: str) -> Path:
    from .platforms import expand

    path = Path(expand(path_value)).expanduser()
    return path if path.is_absolute() else data_dir() / path


def load_config(settings_path: Path | None = None) -> Config:
    settings_path = settings_path or (ensure_config() / "settings.toml")
    data = _read_toml(settings_path)

    local = settings_path.with_name("settings.local.toml")
    if local.exists():
        data = _deep_merge(data, _read_toml(local))

    selectors_raw = _read_toml(settings_path.with_name("selectors.toml"))
    selectors = Selectors(
        groups={name: list(body.get("candidates", [])) for name, body in selectors_raw.items()}
    )

    c, i, s, r = data["chrome"], data["flow"], data["sheets"], data["run"]
    v = data.get("video", {})
    t = data.get("thumbnail", {})
    u = data.get("update", {})

    return Config(
        chrome=ChromeConfig(
            flatpak_app=c.get("flatpak_app", "com.google.Chrome"),
            debug_port=int(c["debug_port"]),
            launch_timeout_s=int(c["launch_timeout_s"]),
            disable_extensions=bool(c.get("disable_extensions", True)),
            binary=c.get("binary", ""),
            user_data_dir=c.get("user_data_dir", ""),
            extra_args=tuple(c.get("extra_args", [])),
        ),
        flow=FlowConfig(
            app_url=i["app_url"],
            project_url=i.get("project_url", ""),
            signed_out_markers=list(i["signed_out_markers"]),
            expected_images=int(i["expected_images"]),
            min_images=int(i["min_images"]),
            settle_seconds=int(i["settle_seconds"]),
            min_result_dimension=int(i["min_result_dimension"]),
            generate_timeout_s=int(i["generate_timeout_s"]),
        ),
        sheets=SheetsConfig(
            mode=s.get("mode", "csv"),
            service_account_file=_resolve(s["service_account_file"]),
            spreadsheet_id=s.get("spreadsheet_id", ""),
            worksheet=s.get("worksheet", "Sheet1"),
            prompt_column=s["prompt_column"],
            status_column=s["status_column"],
            paths_column=s["paths_column"],
            generated_at_column=s["generated_at_column"],
            error_column=s["error_column"],
            write_batch_size=int(s["write_batch_size"]),
            file_path=s.get("file_path", ""),
            sequence_column=s.get("sequence_column", "sequence"),
            scene_column=s.get("scene_column", "scene"),
            emotion_column=s.get("emotion_column", "emotion"),
            voice_column=s.get("voice_column", "voice"),
            animation_column=s.get("animation_column", "animation"),
            thumbnail_column=s.get("thumbnail_column", "thumbnail"),
        ),
        run=RunConfig(
            output_dir=_resolve(r["output_dir"]),
            filename_template=r.get("filename_template", "{row:04}_{prompt}_{index}"),
            db_path=_resolve(r["db_path"]),
            max_per_run=int(r["max_per_run"]),
            concurrency=max(1, int(r.get("concurrency", 2))),
            poll_interval_s=float(r.get("poll_interval_s", 2.0)),
            claim_existing=bool(r.get("claim_existing", True)),
            sweep_after_s=float(r.get("sweep_after_s", 25.0)),
            confirm_above=int(r.get("confirm_above", 50)),
            daily_image_cap=int(r.get("daily_image_cap", 0)),
            run_until_empty=bool(r.get("run_until_empty", False)),
            log_max_kb=int(r.get("log_max_kb", 2048)),
            delay_min_s=float(r["delay_min_s"]),
            delay_max_s=float(r["delay_max_s"]),
            max_attempts=int(r["max_attempts"]),
        ),
        video=VideoConfig(
            tts_engine=v.get("tts_engine", "edge"),
            voice=v.get("voice", "en-US-AndrewNeural"),
            rate=int(v.get("rate", 0)),
            piper_model=v.get("piper_model", ""),
            padding_s=float(v.get("padding_s", 0.35)),
            fallback_duration_s=float(v.get("fallback_duration_s", 4.0)),
            width=int(v.get("width", 1920)),
            height=int(v.get("height", 1080)),
            fps=int(v.get("fps", 30)),
            burn_captions=bool(v.get("burn_captions", True)),
            caption_font=v.get("caption_font", "DejaVu Sans"),
            caption_size=int(v.get("caption_size", 48)),
            caption_preset=v.get("caption_preset", "Clean white"),
            caption_colour=v.get("caption_colour", "#FFFFFF"),
            caption_outline=v.get("caption_outline", "#000000"),
            caption_highlight=v.get("caption_highlight", "#FFD400"),
            caption_box=bool(v.get("caption_box", False)),
            caption_outline_px=int(v.get("caption_outline_px", 3)),
            caption_position=v.get("caption_position", "bottom"),
            caption_margin=int(v.get("caption_margin", 70)),
            caption_animation=v.get("caption_animation", "fade"),
            transition=v.get("transition", "fade"),
            transition_s=float(v.get("transition_s", 0.4)),
            music=v.get("music", ""),
            music_db=float(v.get("music_db", -20.0)),
            ducking=bool(v.get("ducking", True)),
            music_fade_s=float(v.get("music_fade_s", 2.0)),
            ken_burns=bool(v.get("ken_burns", True)),
            zoom_strength=float(v.get("zoom_strength", 0.12)),
            per_scene=bool(v.get("per_scene", True)),
            crf=int(v.get("crf", 20)),
            preset=v.get("preset", "medium"),
            animate_rows=bool(v.get("animate_rows", True)),
            animation_framing=v.get("animation_framing", "full"),
            animation_camera=v.get("animation_camera", "none"),
            animation_backdrop=v.get("animation_backdrop", "image"),
            ffmpeg=v.get("ffmpeg", ""),
            ffprobe=v.get("ffprobe", ""),
        ),
        thumbnail=ThumbnailConfig(
            width=int(t.get("width", 1280)),
            height=int(t.get("height", 720)),
            blocks=tuple(TextBlock(**_block_fields(b)) for b in t.get("blocks", [])),
        ),
        update=UpdateConfig(
            repo=str(u.get("repo", "")).strip(),
            check_on_launch=bool(u.get("check_on_launch", True)),
            token=str(u.get("token", "")).strip(),
        ),
        selectors=selectors,
    )
