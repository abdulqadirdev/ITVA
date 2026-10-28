"""Settings tab: per-project overrides of the global defaults."""

from __future__ import annotations

from PySide6.QtCore import Signal

from imgen.naming import TOKENS, TemplateError, preview
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# (section, key, label, widget spec, tooltip)
FIELDS = [
    ("flow", "project_url", "Flow project URL", ("text",),
     "Generate into this specific Flow project. Empty = use the most recent one."),
    ("flow", "expected_images", "Early-exit at N images", ("int", 0, 16),
     "Stop waiting once this many images arrive. 0 = always wait for the quiet "
     "period instead, which is correct whatever Flow's x1/x2/x4 setting is."),
    ("flow", "min_images", "Minimum images", ("int", 1, 16),
     "Fewer than this and the job counts as a failure."),
    ("flow", "settle_seconds", "Settle seconds", ("int", 1, 120),
     "Quiet seconds with no new image before results are taken as complete. "
     "Flow fills its grid one image at a time."),
    ("flow", "min_result_dimension", "Min result size (px)", ("int", 32, 4096),
     "Ignore images smaller than this. Keeps the account avatar, served from a "
     "matching host, from being saved as a result."),
    ("flow", "generate_timeout_s", "Generation timeout (s)", ("int", 30, 1800),
     "Give up on one prompt after this long."),
    ("run", "delay_min_s", "Delay min (s)", ("float", 0.0, 600.0),
     "Lower bound of the jittered pause between jobs."),
    ("run", "delay_max_s", "Delay max (s)", ("float", 0.0, 600.0),
     "Upper bound of the jittered pause. Pacing keeps the account out of trouble."),
    ("run", "max_attempts", "Max attempts", ("int", 1, 10),
     "Retries for transient failures. Policy refusals are never retried."),
    ("run", "daily_image_cap", "Daily image cap", ("int", 0, 100000),
     "Stop once this many images have been generated today, across all "
     "projects — credits are billed to the Google account, not a project. "
     "0 disables it."),
    ("video", "width", "Video width", ("int", 320, 7680), "Output width in pixels."),
    ("video", "height", "Video height", ("int", 240, 4320), "Output height in pixels."),
    ("video", "fps", "Frames per second", ("int", 12, 60), "30 is standard for this kind of video."),
    ("video", "padding_s", "Pause after each line (s)", ("float", 0.0, 5.0),
     "Breathing room added to each clip beyond the length of its narration."),
    ("video", "fallback_duration_s", "Length without narration (s)", ("float", 0.5, 30.0),
     "Used only for rows that have no script line."),
    ("video", "caption_size", "Caption size", ("int", 10, 72), "Font size of burned-in captions."),
    ("video", "caption_font", "Caption font", ("text",),
     "Must be installed on this machine. DejaVu Sans is a safe default."),
    ("video", "zoom_strength", "Zoom amount", ("float", 0.0, 0.5),
     "How far the slow zoom travels. 0.12 is subtle; above 0.2 gets distracting."),
    ("video", "animation_framing", "Animation framing", ("text",),
     "How close the camera sits on an animated row: wide, full, medium or close."),
    ("video", "animation_camera", "Animation camera", ("text",),
     "Camera move on an animated row: none, push-in, pull-out or drift-left."),
    ("video", "animation_backdrop", "Animation backdrop", ("text",),
     "'image' puts the character over that row's own generated picture; or name "
     "a flat backdrop: plain, bedroom, night, cave, day, warm."),
    ("video", "crf", "Quality (CRF)", ("int", 14, 32),
     "Lower is better and bigger. 20 is a good balance; 23 is noticeably smaller."),
    ("video", "preset", "Encoder preset", ("text",),
     "x264 preset: veryfast renders quickly, slow compresses better."),
    ("video", "tts_engine", "Voice engine", ("text",),
     "'edge' (online, better) or 'piper' (offline). The other is tried if the first fails."),
    ("video", "piper_model", "Piper model file", ("text",),
     "Path to a piper .onnx voice, for narration with no internet at all. "
     "Download one (.onnx plus its .onnx.json, ~60MB) from "
     "huggingface.co/rhasspy/piper-voices. Without it, edge-tts is the only "
     "engine and a connection is required."),
    ("run", "filename_template", "Filename template", ("text",),
     "How generated files are named. Tokens are listed below the field."),
]


class SettingsPanel(QWidget):
    saved = Signal()

    def __init__(self, base_cfg) -> None:
        super().__init__()
        self.base_cfg = base_cfg
        self.project = None
        self.widgets: dict[tuple[str, str], QWidget] = {}

        groups = {"flow": QFormLayout(), "run": QFormLayout(), "video": QFormLayout()}
        for section, key, label, spec, tip in FIELDS:
            widget = self._make(spec)
            widget.setToolTip(tip)
            self.widgets[(section, key)] = widget
            groups[section].addRow(label, widget)

        flow_box = QGroupBox("Generation")
        flow_box.setLayout(groups["flow"])
        run_box = QGroupBox("Pacing and retries")
        run_box.setLayout(groups["run"])
        video_box = QGroupBox("Video and narration")
        video_box.setLayout(groups["video"])

        # Live preview of the filename template — the one setting where the
        # effect is impossible to picture from the value alone.
        self.name_preview = QLabel()
        self.name_preview.setWordWrap(True)
        groups["run"].addRow("Example", self.name_preview)
        tokens = QLabel(
            "<small>"
            + "<br>".join(f"<code>{t}</code> — {d}" for t, d in TOKENS)
            + "<br><br>Numbers pad: <code>{row:04}</code> → 0007. "
            "A <code>/</code> makes a subfolder, so <code>{scene}/{seq:03}</code> "
            "groups a storyboard by scene.</small>"
        )
        tokens.setWordWrap(True)
        groups["run"].addRow("", tokens)

        self.widgets[("run", "filename_template")].textChanged.connect(self._update_preview)

        flow_note = QLabel(
            "<small>Model, aspect ratio and images-per-prompt (x1/x2/x4) are chosen "
            "in Flow's own picker, not here — Flow remembers them per project.</small>"
        )
        flow_note.setWordWrap(True)
        groups["flow"].addRow("", flow_note)

        self.paths = QLabel()
        self.paths.setWordWrap(True)

        save = QPushButton("Save settings")
        save.clicked.connect(self._save)
        reset = QPushButton("Reset to defaults")
        reset.clicked.connect(self._reset)
        buttons = QHBoxLayout()
        buttons.addWidget(save)
        buttons.addWidget(reset)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(flow_box)
        layout.addWidget(run_box)
        layout.addWidget(video_box)
        layout.addWidget(self.paths)
        layout.addLayout(buttons)
        layout.addStretch(1)

    @staticmethod
    def _make(spec) -> QWidget:
        kind = spec[0]
        if kind == "text":
            return QLineEdit()
        if kind == "int":
            box = QSpinBox()
            box.setRange(spec[1], spec[2])
            return box
        box = QDoubleSpinBox()
        box.setRange(spec[1], spec[2])
        box.setSingleStep(0.5)
        return box

    def set_project(self, project) -> None:
        self.project = project
        effective = project.config(self.base_cfg)
        for (section, key), widget in self.widgets.items():
            value = getattr(getattr(effective, section), key)
            if isinstance(widget, QLineEdit):
                widget.setText(str(value or ""))
            else:
                widget.setValue(value)
        self.paths.setText(
            f"<b>Output</b> {effective.run.output_dir}<br><b>Queue</b> {effective.run.db_path}"
        )
        self._update_preview()

    def _update_preview(self) -> None:
        template = self.widgets[("run", "filename_template")].text()
        try:
            self.name_preview.setText(f"<code>{preview(template)}</code>")
        except TemplateError as exc:
            self.name_preview.setText(f"<span style='color:#e2666a'>{exc}</span>")

    def _save(self) -> None:
        if self.project is None:
            return
        stored: dict[str, dict] = {"flow": {}, "run": {}, "video": {}}
        for (section, key), widget in self.widgets.items():
            stored[section][key] = (
                widget.text().strip() if isinstance(widget, QLineEdit) else widget.value()
            )
        self.project.data["settings"] = stored
        self.project.save()
        self.set_project(self.project)
        self.saved.emit()

    def _reset(self) -> None:
        if self.project is None:
            return
        self.project.data["settings"] = {}
        self.project.save()
        self.set_project(self.project)
        self.saved.emit()
