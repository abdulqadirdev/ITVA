"""Source tab: where this project's prompts come from."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from imgen.sources import SUPPORTED, build_source, sheet_tabs

SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


def extract_sheet_id(text: str) -> str:
    """Accept a full Google Sheets URL or a bare id."""
    text = text.strip()
    match = SHEET_ID.search(text)
    if match:
        return match.group(1)
    # A bare id has no slashes; anything else is probably a mistyped URL.
    return text if text and "/" not in text else ""


class SourcePanel(QWidget):
    changed = Signal()          # source settings edited
    loaded = Signal(int)        # prompts pulled into the queue

    def __init__(self, base_cfg) -> None:
        super().__init__()
        self.base_cfg = base_cfg
        self.project = None
        self._prompts: list[tuple[int, str]] = []

        self.radio_sheet = QRadioButton("Google Sheet (link)")
        self.radio_file = QRadioButton("Local file (CSV, Excel, TXT)")
        self.radio_sheet.setChecked(True)
        self.radio_sheet.toggled.connect(self._mode_changed)

        # --- Google Sheet ------------------------------------------------
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://docs.google.com/spreadsheets/d/…  or just the id")
        self.worksheet = QLineEdit()
        self.worksheet.setPlaceholderText("Sheet1")
        sheet_form = QFormLayout()
        sheet_form.addRow("Link or id", self.url)
        sheet_form.addRow("Tab name", self.worksheet)
        self.sheet_box = QGroupBox("Google Sheet")
        self.sheet_box.setLayout(sheet_form)
        hint = QLabel(
            "The sheet must be shared as <b>Anyone with the link → Viewer</b>. "
            "Read-only: results are written to the project's manifest.csv, not back "
            "into the sheet."
        )
        hint.setWordWrap(True)
        sheet_form.addRow(hint)

        # --- Local file --------------------------------------------------
        self.file_label = QLabel("no file chosen")
        self.file_label.setWordWrap(True)
        pick = QPushButton("Choose file…")
        pick.clicked.connect(self._pick_file)
        self.file_tab = QComboBox()
        self.file_tab.setToolTip("Which worksheet to read (Excel files only)")
        file_row = QHBoxLayout()
        file_row.addWidget(pick)
        file_row.addWidget(self.file_label, 1)
        file_form = QFormLayout()
        file_form.addRow(file_row)
        file_form.addRow("Worksheet", self.file_tab)
        self.file_box = QGroupBox("Local file")
        self.file_box.setLayout(file_form)

        # --- shared ------------------------------------------------------
        self.prompt_column = QComboBox()
        self.prompt_column.setEditable(True)
        self.prompt_column.setToolTip(
            "Header of the column holding the prompts (matched case-insensitively)"
        )
        self.status = QLabel("—")
        self.status.setWordWrap(True)

        btn_check = QPushButton("Check source")
        btn_check.clicked.connect(self.check)
        self.btn_load = QPushButton("Load prompts into queue")
        self.btn_load.setEnabled(False)
        self.btn_load.clicked.connect(self._load)

        actions = QHBoxLayout()
        actions.addWidget(btn_check)
        actions.addWidget(self.btn_load)
        actions.addStretch(1)

        bottom = QFormLayout()
        bottom.addRow("Prompt column", self.prompt_column)

        layout = QVBoxLayout(self)
        layout.addWidget(self.radio_sheet)
        layout.addWidget(self.sheet_box)
        layout.addWidget(self.radio_file)
        layout.addWidget(self.file_box)
        layout.addLayout(bottom)
        layout.addLayout(actions)
        layout.addWidget(self.status)
        layout.addStretch(1)

        self._mode_changed()

    # ---- project binding ------------------------------------------------

    def set_project(self, project) -> None:
        self.project = project
        source = project.source
        is_file = source.get("kind") == "file"
        (self.radio_file if is_file else self.radio_sheet).setChecked(True)
        self.url.setText(source.get("spreadsheet_id", ""))
        self.worksheet.setText(source.get("worksheet", "") or "Sheet1")
        path = source.get("file_path", "")
        self.file_label.setText(path or "no file chosen")
        self.prompt_column.setCurrentText(
            source.get("prompt_column", self.base_cfg.sheets.prompt_column)
        )
        self._refresh_tabs(path)
        self.status.setText("—")
        self.btn_load.setEnabled(False)
        self._mode_changed()

    def _mode_changed(self) -> None:
        sheet = self.radio_sheet.isChecked()
        self.sheet_box.setEnabled(sheet)
        self.file_box.setEnabled(not sheet)

    def _pick_file(self) -> None:
        patterns = " ".join(f"*{s}" for s in sorted(SUPPORTED))
        path, _ = QFileDialog.getOpenFileName(
            self, "Prompts file", str(Path.home()), f"Prompt files ({patterns})"
        )
        if path:
            self.file_label.setText(path)
            self._refresh_tabs(path)
            self.changed.emit()

    def _refresh_tabs(self, path: str) -> None:
        self.file_tab.clear()
        if not path:
            return
        try:
            tabs = sheet_tabs(replace(self.base_cfg.sheets, file_path=path))
        except Exception:  # noqa: BLE001 - a bad workbook is reported on Check
            tabs = []
        self.file_tab.addItems(tabs or ["(not an Excel file)"])
        self.file_tab.setEnabled(bool(tabs))

    # ---- source config --------------------------------------------------

    def current_source(self) -> dict:
        if self.radio_sheet.isChecked():
            return {
                "kind": "google_sheet",
                "mode": "csv",
                "spreadsheet_id": extract_sheet_id(self.url.text()),
                "worksheet": self.worksheet.text().strip() or "Sheet1",
                "prompt_column": self.prompt_column.currentText().strip(),
            }
        path = self.file_label.text()
        tab = self.file_tab.currentText() if self.file_tab.isEnabled() else ""
        return {
            "kind": "file",
            "mode": "file",
            "file_path": "" if path == "no file chosen" else path,
            "worksheet": tab,
            "prompt_column": self.prompt_column.currentText().strip(),
        }

    def apply_to_project(self) -> None:
        if self.project is None:
            return
        self.project.data["source"] = self.current_source()
        self.project.save()

    def check(self) -> bool:
        """Open the source and report what is in it, without touching the queue."""
        self.apply_to_project()
        try:
            source = build_source(self.project.config(self.base_cfg).sheets)
            self._prompts = source.read_prompts()
        except Exception as exc:  # noqa: BLE001 - every failure is user-facing
            self._prompts = []
            self.btn_load.setEnabled(False)
            self.status.setText(
                f"<span style='color:#e2666a'>{str(exc).splitlines()[0]}</span>"
            )
            return False

        # Offer the real headers now that we have them.
        headers = getattr(source, "headers", [])
        if headers:
            current = self.prompt_column.currentText()
            self.prompt_column.clear()
            self.prompt_column.addItems(headers)
            self.prompt_column.setCurrentText(current)

        self.status.setText(
            f"<span style='color:#2e9e52'>'{source.title}' — "
            f"{len(self._prompts)} prompt(s) found"
            f"{'' if source.writable else ', read-only'}</span>"
        )
        self.btn_load.setEnabled(bool(self._prompts))
        return True

    def _load(self) -> None:
        from imgen.db import JobStore

        if not self._prompts and not self.check():
            return
        cfg = self.project.config(self.base_cfg)
        store = JobStore(cfg.run.db_path)
        try:
            added, known = store.sync_prompts(self._prompts)
        finally:
            store.close()
        self.status.setText(
            f"<span style='color:#2e9e52'>loaded — {added} new, {known} already "
            "queued (their row data was refreshed, so a newly added column "
            "such as animation is now in the queue)</span>"
        )
        self.loaded.emit(added)
