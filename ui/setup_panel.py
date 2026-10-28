"""The Setup tab: everything this machine still needs, with the buttons.

Written for whoever installed the .deb and opened it. They are not going
to read a README or edit a TOML file, so each row says what is wrong in
one sentence and carries the button that fixes it.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

GREEN = "#2e9e52"
AMBER = "#d79a3a"
GREY = "#9aa0a6"


class CheckRow(QFrame):
    fix_requested = Signal(object)   # the Check

    def __init__(self, check) -> None:
        super().__init__()
        self.check = check
        self.setFrameShape(QFrame.StyledPanel)

        mark = "✓" if check.ok else ("!" if check.required else "–")
        colour = GREEN if check.ok else (AMBER if check.required else GREY)

        badge = QLabel(mark)
        badge.setFixedWidth(22)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"color: {colour}; font-size: 17px; font-weight: bold;")

        title = QLabel(f"<b>{check.title}</b>")
        detail = QLabel(check.detail)
        detail.setWordWrap(True)
        detail.setStyleSheet(f"color: {GREY};")

        words = QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(title)
        words.addWidget(detail)
        if check.manual and not check.ok:
            note = QLabel(check.manual)
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {GREY}; font-style: italic;")
            words.addWidget(note)

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.addWidget(badge)
        row.addLayout(words, 1)

        self.button = None
        if not check.ok and check.fix_label:
            self.button = QPushButton(check.fix_label)
            self.button.clicked.connect(lambda: self.fix_requested.emit(check))
            row.addWidget(self.button, 0, Qt.AlignTop)

    def set_busy(self, text: str) -> None:
        if self.button is not None:
            self.button.setEnabled(False)
            self.button.setText(text)


class SetupPanel(QWidget):
    fix_requested = Signal(object)
    recheck_requested = Signal()
    sign_in_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, CheckRow] = {}

        self.headline = QLabel("")
        self.headline.setWordWrap(True)

        self.btn_recheck = QPushButton("Check again")
        self.btn_recheck.clicked.connect(self.recheck_requested)

        top = QHBoxLayout()
        top.addWidget(self.headline, 1)
        top.addWidget(self.btn_recheck, 0, Qt.AlignTop)

        self.list = QVBoxLayout()
        self.list.setSpacing(6)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {GREY};")

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(self.list)
        root.addWidget(self.status)
        root.addStretch(1)

    def show_checks(self, results: list) -> None:
        while self.list.count():
            item = self.list.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.rows.clear()

        missing = [c for c in results if c.required and not c.ok]
        if not missing:
            self.headline.setText(
                "<b>Everything is ready.</b> Open the Source tab, point it at "
                "your sheet, and press Start run."
            )
        else:
            self.headline.setText(
                f"<b>{len(missing)} thing(s) to set up before this can run.</b> "
                "Each one has a button."
            )

        for check in results:
            row = CheckRow(check)
            # The sign-in step opens a browser rather than installing
            # anything, so it goes to its own handler.
            if check.key == "signin" and row.button is not None:
                row.button.clicked.disconnect()
                row.button.clicked.connect(self.sign_in_requested)
            else:
                row.fix_requested.connect(self.fix_requested)
            self.rows[check.key] = row
            self.list.addWidget(row)

    def set_busy(self, key: str, text: str) -> None:
        row = self.rows.get(key)
        if row is not None:
            row.set_busy(text)

    def set_status(self, text: str) -> None:
        self.status.setText(text)
