"""Choosing a voice out of several hundred.

A flat list of 322 names is unusable, so they are filtered by language,
gender and personality, and can be auditioned before being accepted — the
only way to actually tell them apart.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from imgen.platforms import open_path
from imgen.tts import (
    VoiceOption,
    all_categories,
    all_personalities,
    group_by_language,
    list_edge_voices,
    preview_text,
    synthesise,
)

ANY = "Any"


class _LoadWorker(QThread):
    loaded = Signal(list)

    def run(self) -> None:
        try:
            self.loaded.emit(list_edge_voices())
        except Exception:  # noqa: BLE001 - offline is a normal state
            self.loaded.emit([])


class _PreviewWorker(QThread):
    done = Signal(str, str)   # path, error

    def __init__(self, option: VoiceOption, rate: int):
        super().__init__()
        self.option = option
        self.rate = rate

    def run(self) -> None:
        try:
            target = Path(tempfile.gettempdir()) / f"voice-{self.option.name}"
            path = synthesise(
                preview_text(self.option), target,
                engine="edge", voice=self.option.name, rate=self.rate,
            )
            self.done.emit(str(path), "")
        except Exception as exc:  # noqa: BLE001
            self.done.emit("", str(exc))


class VoiceDialog(QDialog):
    def __init__(self, current: str = "", rate: int = 0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose a voice")
        self.resize(760, 560)
        self.rate = rate
        self.all: list[VoiceOption] = []
        self.chosen = current
        self._preview: _PreviewWorker | None = None
        self._loader: _LoadWorker | None = None

        self.search = QLineEdit(placeholderText="Search name, language or personality...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filters)

        self.language = QComboBox()
        self.gender = QComboBox()
        self.trait = QComboBox()
        for box in (self.language, self.gender, self.trait):
            box.addItem(ANY)
            box.currentIndexChanged.connect(self.apply_filters)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("Language"))
        filters.addWidget(self.language, 2)
        filters.addWidget(QLabel("Gender"))
        filters.addWidget(self.gender)
        filters.addWidget(QLabel("Style"))
        filters.addWidget(self.trait)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self.accept())
        self.list.currentItemChanged.connect(self._selected)

        self.status = QLabel("Loading voices...")
        self.status.setWordWrap(True)

        self.btn_preview = QPushButton("Preview")
        self.btn_preview.setToolTip("Speak a sample line in this voice")
        self.btn_preview.clicked.connect(self.preview)
        self.btn_preview.setEnabled(False)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.addButton(self.btn_preview, QDialogButtonBox.ActionRole)

        layout = QVBoxLayout(self)
        layout.addWidget(self.search)
        layout.addLayout(filters)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)
        layout.addWidget(buttons)

        self._loader = _LoadWorker()
        self._loader.loaded.connect(self._loaded)
        self._loader.start()

    # ---- data ------------------------------------------------------------

    def _loaded(self, options: list) -> None:
        self.all = options
        if not options:
            self.status.setText(
                "The voice list could not be fetched - offline, or the service is "
                "unavailable. The voice already configured still works."
            )
            return

        for name in group_by_language(options):
            self.language.addItem(name)
        for name in sorted({o.gender for o in options if o.gender}):
            self.gender.addItem(name)
        for name in all_personalities(options) + all_categories(options):
            if self.trait.findText(name) < 0:
                self.trait.addItem(name)

        # Open on the current voice's language rather than the top of a list
        # of 142.
        current = next((o for o in options if o.name == self.chosen), None)
        if current:
            index = self.language.findText(current.language)
            if index >= 0:
                self.language.setCurrentIndex(index)
        self.apply_filters()

    def visible(self) -> list[VoiceOption]:
        text = self.search.text().strip()
        language = self.language.currentText()
        gender = self.gender.currentText()
        trait = self.trait.currentText()

        found = []
        for option in self.all:
            if language != ANY and option.language != language:
                continue
            if gender != ANY and option.gender != gender:
                continue
            if trait != ANY and trait not in option.personalities + option.categories:
                continue
            if text and not option.describes(text):
                continue
            found.append(option)
        return found

    def apply_filters(self) -> None:
        self.list.clear()
        options = self.visible()
        for option in options:
            traits = ", ".join(option.personalities[:3]) or ", ".join(option.categories[:2])
            item = QListWidgetItem(
                f"{option.short}  —  {option.language}  ·  {option.gender}"
                + (f"  ·  {traits}" if traits else "")
            )
            item.setData(Qt.UserRole, option)
            item.setToolTip(option.name)
            self.list.addItem(item)
            if option.name == self.chosen:
                self.list.setCurrentItem(item)
        self.status.setText(f"{len(options)} of {len(self.all)} voices")
        if self.list.currentItem() is None and options:
            self.list.setCurrentRow(0)

    def _selected(self, current, _previous) -> None:
        option = current.data(Qt.UserRole) if current else None
        self.btn_preview.setEnabled(option is not None)
        if option:
            self.chosen = option.name

    # ---- preview ----------------------------------------------------------

    def preview(self) -> None:
        item = self.list.currentItem()
        if item is None or (self._preview is not None and self._preview.isRunning()):
            return
        option = item.data(Qt.UserRole)
        self.btn_preview.setEnabled(False)
        self.btn_preview.setText("Speaking...")
        self._preview = _PreviewWorker(option, self.rate)
        self._preview.done.connect(self._previewed)
        self._preview.start()

    def _previewed(self, path: str, error: str) -> None:
        self.btn_preview.setEnabled(True)
        self.btn_preview.setText("Preview")
        if error:
            self.status.setText(f"Preview failed: {error}")
        elif path:
            open_path(path)

    def selected_voice(self) -> str:
        return self.chosen

    def closeEvent(self, event) -> None:
        for worker in (self._loader, self._preview):
            if worker is not None and worker.isRunning():
                worker.wait(8000)
        event.accept()

    def done(self, result: int) -> None:
        for worker in (self._loader, self._preview):
            if worker is not None and worker.isRunning():
                worker.wait(8000)
        super().done(result)
