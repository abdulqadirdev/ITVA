"""Video tab: narration, captions and rendering."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from imgen.actions import names as action_names
from imgen.platforms import find_ffmpeg, open_path
from imgen.subtitles import ANIMATIONS, POSITIONS, PRESETS

JOB_ROLE = Qt.UserRole + 1


class ActionDelegate(QStyledItemDelegate):
    """A dropdown of the actions the rig can play, plus blank for none.

    Typed free text would mean discovering a typo only when the render
    reported an unknown action, so the cell offers the real list.
    """

    def createEditor(self, parent, option, index):
        box = QComboBox(parent)
        box.addItem("")
        box.addItems(action_names())
        return box

    def setEditorData(self, editor, index):
        editor.setCurrentText(index.data() or "")

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentText())


class StoryboardModel(QAbstractTableModel):
    #: job id, action name ("" clears it)
    animation_edited = Signal(int, str)

    COLUMNS = ("Row", "Seq", "Scene", "Script line", "Mood", "Animation", "Length", "Voice")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list = []
        self._seq_col = "sequence"
        self._scene_col = "scene"
        self._mood_col = "emotion"
        self._anim_col = "animation"

    def set_rows(
        self, rows: list, seq_col: str, scene_col: str, mood_col: str,
        anim_col: str = "animation",
    ) -> None:
        self.beginResetModel()
        self._rows = rows
        self._seq_col, self._scene_col = seq_col.casefold(), scene_col.casefold()
        self._mood_col = mood_col.casefold()
        self._anim_col = anim_col.casefold()
        self.endResetModel()

    def rowCount(self, parent=None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._rows)

    def columnCount(self, parent=None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.COLUMNS)

    #: Index of the Animation column, which the user can edit in place.
    ANIM_COLUMN = 5

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if index.isValid() and index.column() == self.ANIM_COLUMN:
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or index.column() != self.ANIM_COLUMN:
            return False
        if role != Qt.EditRole:
            return False
        self.animation_edited.emit(self._rows[index.row()].id, str(value or "").strip())
        return True

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        from imgen.produce import script_of

        job = self._rows[index.row()]
        fields = {k.casefold(): v for k, v in (job.fields or {}).items()}
        has_audio = bool(job.audio_path and Path(job.audio_path).exists())

        if role == Qt.DisplayRole:
            return (
                job.row_number,
                fields.get(self._seq_col, ""),
                fields.get(self._scene_col, ""),
                script_of(job),
                fields.get(self._mood_col, ""),
                fields.get(self._anim_col, ""),
                f"{job.duration_s:.1f}s" if job.duration_s else "",
                "ready" if has_audio else ("—" if not script_of(job) else "missing"),
            )[index.column()]
        if role == Qt.ForegroundRole and index.column() == 7:
            return QColor("#2e9e52") if has_audio else QColor("#d79a3a")
        if role == Qt.ToolTipRole:
            return script_of(job) or "this row has no script line"
        if role == JOB_ROLE:
            return job
        return None


class VideoPanel(QWidget):
    narrate_requested = Signal(list, bool)   # job ids, force
    build_requested = Signal(int)            # limit, 0 = everything
    stop_requested = Signal()
    setting_changed = Signal(str, object)    # key, value
    animation_changed = Signal(int, str)     # job id, action name

    def __init__(self, base_cfg) -> None:
        super().__init__()
        self.base_cfg = base_cfg
        self.project = None
        self.model = StoryboardModel()
        #: Set by the window so background threads are waited for on exit.
        self.register_worker = None
        self._voice_name = ""

        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for column, width in enumerate((60, 55, 130, 440, 80, 110, 70, 70)):
            self.table.setColumnWidth(column, width)
        self.table.setItemDelegateForColumn(
            StoryboardModel.ANIM_COLUMN, ActionDelegate(self.table)
        )
        self.model.animation_edited.connect(self.animation_changed)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_warning())
        layout.addWidget(self._build_options())
        layout.addLayout(self._build_actions())
        layout.addWidget(self.table)
        layout.addLayout(self._build_progress())

    # ---- construction ---------------------------------------------------

    def _build_warning(self) -> QWidget:
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setVisible(False)
        return self.warning

    def _build_options(self) -> QWidget:
        # A plain combo of 322 names is unusable, so the picker is a dialog
        # that filters by language, gender and personality, and can speak a
        # sample before you commit.
        self.voice_label = QLabel("-")
        self.voice_label.setMinimumWidth(280)
        self.btn_voices = QPushButton("Choose voice...")
        self.btn_voices.setToolTip("Browse all voices by language and style, with previews")
        self.btn_voices.clicked.connect(self._choose_voice)

        self.rate = QSpinBox()
        self.rate.setRange(-50, 50)
        self.rate.setSuffix(" %")
        self.rate.setToolTip("Speaking rate, as an offset from normal")
        self.rate.valueChanged.connect(lambda v: self.setting_changed.emit("rate", v))

        self.captions = QCheckBox("Burn captions into the picture")
        self.captions.setToolTip(
            "A .srt is written either way. Burning paints the text into the "
            "frames, which cannot be undone."
        )
        self.captions.toggled.connect(
            lambda v: self.setting_changed.emit("burn_captions", v)
        )

        self.ken_burns = QCheckBox("Slow zoom (Ken Burns)")
        self.ken_burns.setToolTip("Stops a long sequence of stills looking like a slideshow")
        self.ken_burns.toggled.connect(lambda v: self.setting_changed.emit("ken_burns", v))

        self.animate_rows = QCheckBox("Animate rows that name an action")
        self.animate_rows.setToolTip(
            "Rows with a value in the animation column play a vector character "
            "instead of their still picture. Off ignores that column, so the "
            "sheet can stay filled in."
        )
        self.animate_rows.toggled.connect(
            lambda v: self.setting_changed.emit("animate_rows", v)
        )

        self.per_scene = QCheckBox("Also export each scene separately")
        self.per_scene.toggled.connect(lambda v: self.setting_changed.emit("per_scene", v))

        self.preset = self._combo(list(PRESETS), "caption_preset",
                                  "A ready-made look, so the colours need no fiddling")
        self.animation = self._combo(list(ANIMATIONS), "caption_animation",
                                     "How each line appears. 'highlight' colours each word "
                                     "as it is spoken, timed by word length rather than "
                                     "measured, so it follows the voice without being exact")
        self.position = self._combo(list(POSITIONS), "caption_position",
                                    "Where the caption sits in the frame")
        self.transition = self._combo(["fade", "none", "crossfade"], "transition",
                                      "fade dips each clip and still joins without "
                                      "re-encoding; crossfade truly overlaps but is slower")
        self.btn_colour = QPushButton("Colour...")
        self.btn_colour.clicked.connect(self._pick_colour)

        self.music_label = QLabel("no music")
        self.music_label.setMinimumWidth(190)
        btn_music = QPushButton("Music...")
        btn_music.setToolTip("An audio file laid under the whole video, looped to fit")
        btn_music.clicked.connect(self._pick_music)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(lambda: self._set_music(""))
        self.btn_music, self.btn_music_clear = btn_music, btn_clear

        self.ducking = QCheckBox("Dip music under speech")
        self.ducking.setToolTip(
            "Compresses the music against the narration, so it steps back "
            "whenever anyone speaks and returns in the gaps"
        )
        self.ducking.toggled.connect(lambda v: self.setting_changed.emit("ducking", v))

        self.music_db = QSpinBox()
        self.music_db.setRange(-40, 0)
        self.music_db.setSuffix(" dB")
        self.music_db.setToolTip("How loud the music sits under the voice")
        self.music_db.valueChanged.connect(
            lambda v: self.setting_changed.emit("music_db", float(v))
        )

        top = QHBoxLayout()
        top.addWidget(QLabel("Voice"))
        top.addWidget(self.voice_label)
        top.addWidget(self.btn_voices)
        top.addWidget(QLabel("  Rate"))
        top.addWidget(self.rate)
        top.addStretch(1)

        bottom = QHBoxLayout()
        for widget in (self.captions, self.ken_burns, self.animate_rows,
                       self.per_scene):
            bottom.addWidget(widget)
        bottom.addStretch(1)

        styling = QHBoxLayout()
        for label, widget in (
            ("Caption style", self.preset),
            ("Animation", self.animation),
            ("Position", self.position),
            ("Transition", self.transition),
        ):
            styling.addWidget(QLabel(label))
            styling.addWidget(widget)
        styling.addWidget(self.btn_colour)
        styling.addStretch(1)

        audio = QHBoxLayout()
        audio.addWidget(QLabel("Music"))
        audio.addWidget(self.music_label)
        audio.addWidget(self.btn_music)
        audio.addWidget(self.btn_music_clear)
        audio.addWidget(self.music_db)
        audio.addWidget(self.ducking)
        audio.addStretch(1)

        box = QGroupBox("Narration and look")
        inner = QVBoxLayout(box)
        inner.addLayout(top)
        inner.addLayout(bottom)
        inner.addLayout(styling)
        inner.addLayout(audio)
        return box

    def _pick_music(self) -> None:
        from pathlib import Path as _Path

        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Background music", str(_Path.home()),
            "Audio (*.mp3 *.wav *.m4a *.ogg *.opus *.flac);;All files (*)",
        )
        if path:
            self._set_music(path)

    def _set_music(self, path: str) -> None:
        self.setting_changed.emit("music", path)
        self._paint_music(path)

    def _paint_music(self, path: str) -> None:
        from pathlib import Path as _Path

        self.music_label.setText(_Path(path).name if path else "no music")
        self.music_label.setToolTip(path or "")
        for widget in (self.music_db, self.ducking, self.btn_music_clear):
            widget.setEnabled(bool(path))

    def _combo(self, items: list, key: str, tip: str) -> QComboBox:
        box = QComboBox()
        box.addItems(items)
        box.setToolTip(tip)
        box.currentTextChanged.connect(
            lambda text, k=key: self.setting_changed.emit(k, text)
        )
        return box

    def _pick_colour(self) -> None:
        from PySide6.QtGui import QColor

        current = QColor(getattr(self.cfg.video, "caption_colour", "#FFFFFF"))
        chosen = QColorDialog.getColor(current, self, "Caption colour")
        if chosen.isValid():
            self.setting_changed.emit("caption_colour", chosen.name().upper())
            self._paint_colour(chosen.name().upper())

    def _paint_colour(self, value: str) -> None:
        self.btn_colour.setText(f"Colour  {value}")
        self.btn_colour.setStyleSheet(
            f"QPushButton {{ border-left: 14px solid {value}; padding-left: 6px; }}"
        )

    def _build_actions(self):
        self.btn_narrate = QPushButton("Narrate missing")
        self.btn_narrate.setToolTip("Synthesise speech for rows that have none yet")
        self.btn_narrate.clicked.connect(lambda: self.narrate_requested.emit([], False))

        self.btn_preview = QPushButton("Preview first 3")
        self.btn_preview.setToolTip("Render a short sample before committing to the whole thing")
        self.btn_preview.clicked.connect(lambda: self.build_requested.emit(3))

        self.btn_build = QPushButton("Build video")
        self.btn_build.clicked.connect(lambda: self.build_requested.emit(0))

        self.btn_stop = QPushButton("Stop", enabled=False)
        self.btn_stop.clicked.connect(self.stop_requested.emit)

        self.btn_open = QPushButton("Open output")
        self.btn_open.clicked.connect(self._open_output)

        self.summary = QLabel("—")

        row = QHBoxLayout()
        row.addWidget(self.summary, 1)
        for widget in (
            self.btn_open, self.btn_narrate, self.btn_preview,
            self.btn_build, self.btn_stop,
        ):
            row.addWidget(widget)
        return row

    def _build_progress(self):
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setFormat("%v / %m")
        row = QHBoxLayout()
        row.addWidget(self.progress)
        return row

    # ---- per-row actions -------------------------------------------------

    def _selected(self) -> list:
        rows = {i.row() for i in self.table.selectionModel().selectedIndexes()}
        return [self.model.index(r, 0).data(JOB_ROLE) for r in sorted(rows)]

    def _menu(self, point) -> None:
        jobs = self._selected()
        if not jobs:
            return
        menu = QMenu(self)
        ids = [j.id for j in jobs]

        again = QAction(f"Re-narrate ({len(jobs)})", self)
        again.triggered.connect(lambda: self.narrate_requested.emit(ids, True))
        menu.addAction(again)

        audio = [j.audio_path for j in jobs if j.audio_path and Path(j.audio_path).exists()]
        play = QAction("Play narration", self)
        play.setEnabled(bool(audio))
        play.triggered.connect(lambda: open_path(audio[0]))
        menu.addAction(play)

        image = [p for j in jobs for p in j.image_paths if Path(p).exists()]
        show = QAction("Open image", self)
        show.setEnabled(bool(image))
        show.triggered.connect(lambda: open_path(image[0]))
        menu.addAction(show)
        menu.exec(self.table.viewport().mapToGlobal(point))

    # ---- state -----------------------------------------------------------

    def set_project(self, project, cfg) -> None:
        self.project = project
        self.cfg = cfg
        v = cfg.video
        for widget, value in (
            (self.captions, v.burn_captions),
            (self.ken_burns, v.ken_burns),
            (self.animate_rows, v.animate_rows),
            (self.per_scene, v.per_scene),
        ):
            widget.blockSignals(True)
            widget.setChecked(value)
            widget.blockSignals(False)
        self.rate.blockSignals(True)
        self.rate.setValue(v.rate)
        self.rate.blockSignals(False)
        for widget, value in (
            (self.preset, v.caption_preset),
            (self.animation, v.caption_animation),
            (self.position, v.caption_position),
            (self.transition, v.transition),
        ):
            widget.blockSignals(True)
            if widget.findText(value) >= 0:
                widget.setCurrentText(value)
            widget.blockSignals(False)
        self._paint_colour(v.caption_colour)
        self.music_db.blockSignals(True)
        self.music_db.setValue(int(v.music_db))
        self.music_db.blockSignals(False)
        self.ducking.blockSignals(True)
        self.ducking.setChecked(v.ducking)
        self.ducking.blockSignals(False)
        self._paint_music(v.music)
        self._set_voice(v.voice)
        self._check_ffmpeg()

    def _check_ffmpeg(self) -> None:
        if find_ffmpeg(getattr(self, "cfg", self.base_cfg).video.ffmpeg):
            self.warning.setVisible(False)
            return
        self.warning.setText(
            "<b style='color:#e2666a'>ffmpeg was not found.</b> It is the engine that "
            "builds the video.<br>Linux: <code>sudo apt install ffmpeg</code> &nbsp;·&nbsp; "
            "Windows: <code>winget install Gyan.FFmpeg</code> &nbsp;·&nbsp; "
            "or set its path in Settings."
        )
        self.warning.setVisible(True)
        for widget in (self.btn_build, self.btn_preview, self.btn_narrate):
            widget.setEnabled(False)

    def set_rows(self, rows: list) -> None:
        self.model.set_rows(
            rows,
            self.cfg.sheets.sequence_column,
            self.cfg.sheets.scene_column,
            self.cfg.sheets.emotion_column,
            self.cfg.sheets.animation_column,
        )
        narrated = sum(1 for r in rows if r.audio_path and Path(r.audio_path).exists())
        total_s = sum((r.duration_s or 0) + self.cfg.video.padding_s for r in rows if r.duration_s)
        self.summary.setText(
            f"{len(rows)} row(s) ready  ·  {narrated} narrated"
            + (f"  ·  about {total_s / 60:.1f} min of video" if total_s else "")
        )

    def set_busy(self, busy: bool, total: int = 0) -> None:
        for widget in (self.btn_narrate, self.btn_preview, self.btn_build):
            widget.setEnabled(not busy and find_ffmpeg(self.cfg.video.ffmpeg) is not None)
        self.btn_stop.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setMaximum(max(total, 1))
            self.progress.setValue(0)

    def set_progress(self, done: int, total: int) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    def _choose_voice(self) -> None:
        from .voice_dialog import VoiceDialog

        dialog = VoiceDialog(self._voice_name, self.rate.value(), self)
        if dialog.exec() == dialog.Accepted and dialog.selected_voice():
            self._set_voice(dialog.selected_voice())
            self.setting_changed.emit("voice", self._voice_name)

    def _set_voice(self, name: str) -> None:
        self._voice_name = name
        tail = name.split("-")[-1]
        pretty = tail[:-6] if tail.endswith("Neural") else tail
        locale = "-".join(name.split("-")[:2])
        self.voice_label.setText(f"<b>{pretty}</b>  <span style='color:#8b8b8b'>{locale}</span>")
        self.voice_label.setToolTip(name)

    def _open_output(self) -> None:
        if self.project:
            self.project.output_dir.mkdir(parents=True, exist_ok=True)
            open_path(self.project.output_dir)
