"""A strip across the top of the window when a newer release exists.

A bar rather than a dialog: an update is worth noticing and not worth
interrupting anyone for, and a dialog that appears over a running job is
the kind of thing people learn to dismiss without reading.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QWidget,
)


class UpdateBanner(QWidget):
    install_requested = Signal(object)   # the Release to install
    restart_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.release = None
        self.setVisible(False)
        self.setObjectName("updateBanner")
        # A plain QWidget ignores a stylesheet background unless this is
        # set, and the bar then inherits whatever the theme paints. With
        # near-white text on it that meant the message was invisible -
        # the buttons showed, the words did not.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#updateBanner {"
            "  background: #26405e; border-bottom: 1px solid #3c5a7e;"
            "}"
            "QWidget#updateBanner QLabel { color: #eef3f9; }"
            # The flat ones sit on the bar itself and take its colour.
            "QWidget#updateBanner QPushButton[flat=\"true\"] {"
            "  color: #cfe0f2; border: none; padding: 4px 10px;"
            "}"
            "QWidget#updateBanner QPushButton[flat=\"true\"]:hover {"
            "  color: #ffffff; text-decoration: underline;"
            "}"
        )

        self.message = QLabel("")
        self.message.setWordWrap(False)

        self.btn_notes = QPushButton("What's new")
        self.btn_notes.setFlat(True)
        self.btn_notes.clicked.connect(self._show_notes)

        self.btn_install = QPushButton("Update")
        self.btn_install.setDefault(True)
        # Its own fill, not just a text colour. As the default button the
        # theme paints it almost white, and the near-white label on the
        # rest of the bar then vanished into it.
        self.btn_install.setStyleSheet(
            "QPushButton {"
            "  background: #2f7bd6; color: #ffffff; border: none;"
            "  padding: 5px 18px; border-radius: 3px; font-weight: 600;"
            "}"
            "QPushButton:hover { background: #3d8ae8; }"
            "QPushButton:disabled { background: #44586e; color: #9fb0c2; }"
        )
        self.btn_install.clicked.connect(self._install)

        self.btn_later = QPushButton("Later")
        self.btn_later.setFlat(True)
        self.btn_later.clicked.connect(lambda: self.setVisible(False))

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(190)
        self.progress.setVisible(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 7, 12, 7)
        row.addWidget(self.message)
        row.addSpacing(14)
        row.addWidget(self.progress)
        row.addStretch(1)
        for widget in (self.btn_notes, self.btn_install, self.btn_later):
            row.addWidget(widget)

    # ---- states ---------------------------------------------------------

    def offer(self, release) -> None:
        self.release = release
        self.message.setText(
            f"<b>Version {release.version} is available</b> "
            f"&nbsp;&nbsp;{release.megabytes:.0f} MB"
        )
        self.btn_notes.setVisible(bool(release.notes))
        self.btn_install.setVisible(True)
        self.btn_install.setEnabled(True)
        self.btn_later.setVisible(True)
        self.progress.setVisible(False)
        self.setVisible(True)

    def set_busy(self, text: str) -> None:
        self.message.setText(text)
        self.btn_install.setEnabled(False)
        self.btn_notes.setVisible(False)
        self.btn_later.setVisible(False)
        self.progress.setVisible(True)
        self.setVisible(True)

    def set_progress(self, fraction: float) -> None:
        self.progress.setValue(int(fraction * 100))

    def set_installed(self) -> None:
        version = self.release.version if self.release else ""
        self.message.setText(
            f"<b>Version {version} installed.</b> Restart to use it."
        )
        self.progress.setVisible(False)
        self.btn_install.setText("Restart now")
        self.btn_install.setEnabled(True)
        self.btn_install.setVisible(True)
        try:
            self.btn_install.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self.btn_install.clicked.connect(self.restart_requested)
        self.btn_later.setText("Later")
        self.btn_later.setVisible(True)
        self.setVisible(True)

    def set_failed(self, message: str) -> None:
        self.message.setText(f"Update failed: {message.splitlines()[0][:120]}")
        self.btn_install.setEnabled(True)
        self.btn_later.setVisible(True)
        self.progress.setVisible(False)
        self.setVisible(True)

    # ---- actions --------------------------------------------------------

    def _install(self) -> None:
        if self.release is not None:
            self.install_requested.emit(self.release)

    def _show_notes(self) -> None:
        if self.release is None:
            return
        box = QMessageBox(self)
        box.setWindowTitle(f"What's new in {self.release.version}")
        box.setTextFormat(Qt.PlainText)
        box.setText(self.release.notes or "No notes were published.")
        box.exec()
