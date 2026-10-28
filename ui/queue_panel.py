"""Queue tab: the job table, filtering, per-row actions and run controls."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from imgen.platforms import open_path
from imgen.db import Status

STATUS_COLOURS = {
    Status.DONE: QColor("#2e9e52"),
    Status.RUNNING: QColor("#4a9ff5"),
    Status.BLOCKED: QColor("#d79a3a"),
    Status.QUOTA: QColor("#d79a3a"),
    Status.FAILED: QColor("#e2666a"),
    Status.SKIPPED: QColor("#8b8b8b"),
}

JOB_ROLE = Qt.UserRole + 1


class JobTableModel(QAbstractTableModel):
    COLUMNS = ("Row", "Prompt", "Status", "Tries", "Images", "Error")

    def __init__(self) -> None:
        super().__init__()
        self._jobs: list = []

    def set_jobs(self, jobs: list) -> None:
        self.beginResetModel()
        self._jobs = jobs
        self.endResetModel()

    def job_at(self, row: int):
        return self._jobs[row]

    def rowCount(self, parent=None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self._jobs)

    def columnCount(self, parent=None) -> int:
        return 0 if parent is not None and parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        job = self._jobs[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            return (
                job.row_number,
                job.prompt,
                job.status.value,
                job.attempts,
                len(job.image_paths),
                job.error or "",
            )[column]
        if role == Qt.ForegroundRole and column == 2:
            return STATUS_COLOURS.get(job.status)
        if role == Qt.ToolTipRole:
            return job.error or job.prompt
        if role == JOB_ROLE:
            return job
        return None


class JobFilter(QSortFilterProxyModel):
    """Filters by status and by text in the prompt or error."""

    def __init__(self) -> None:
        super().__init__()
        self.status = ""
        self.text = ""

    def set_status(self, status: str) -> None:
        self.status = status
        self.invalidate()

    def set_text(self, text: str) -> None:
        self.text = text.strip().casefold()
        self.invalidate()

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        job = self.sourceModel().job_at(row)
        if self.status and job.status.value != self.status:
            return False
        if self.text:
            haystack = f"{job.prompt} {job.error or ''}".casefold()
            if self.text not in haystack:
                return False
        return True


class QueuePanel(QWidget):
    start_requested = Signal(int)
    stop_requested = Signal()
    retry_requested = Signal()
    regenerate_requested = Signal(list)
    skip_requested = Signal(list)
    edit_requested = Signal(int)      # one job id
    export_requested = Signal(list)   # job ids

    def __init__(self) -> None:
        super().__init__()
        self.model = JobTableModel()
        self.proxy = JobFilter()
        self.proxy.setSourceModel(self.model)
        self._run_started = 0.0
        self._run_total = 0

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSortingEnabled(True)
        # setSortingEnabled alone leaves the indicator and the actual order
        # disagreeing — it showed ascending while listing row 155 first.
        self.table.sortByColumn(0, Qt.AscendingOrder)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 520)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_filters())
        layout.addLayout(self._build_controls())
        layout.addWidget(self.table)
        layout.addLayout(self._build_progress())

    # ---- construction ---------------------------------------------------

    def _build_filters(self):
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search prompts and errors…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.proxy.set_text)

        self.status_box = QComboBox()
        self.status_box.addItem("All statuses", "")
        for status in Status:
            self.status_box.addItem(status.value, status.value)
        self.status_box.currentIndexChanged.connect(
            lambda: self.proxy.set_status(self.status_box.currentData())
        )

        row = QHBoxLayout()
        row.addWidget(self.search, 1)
        row.addWidget(self.status_box)
        return row

    def _build_controls(self):
        self.limit = QSpinBox()
        self.limit.setRange(1, 100000)
        self.limit.setToolTip("How many jobs to run now. Defaults to everything pending.")

        self.btn_start = QPushButton("Start")
        self.btn_stop = QPushButton("Stop", enabled=False)
        self.btn_retry = QPushButton("Retry failed")
        self.summary = QLabel("—")

        self.btn_start.clicked.connect(lambda: self.start_requested.emit(self.limit.value()))
        self.btn_stop.clicked.connect(self.stop_requested.emit)
        self.btn_retry.clicked.connect(self.retry_requested.emit)

        row = QHBoxLayout()
        row.addWidget(QLabel("Run"))
        row.addWidget(self.limit)
        row.addWidget(QLabel("of"))
        row.addWidget(self.summary)
        row.addStretch(1)
        row.addWidget(self.btn_retry)
        row.addWidget(self.btn_start)
        row.addWidget(self.btn_stop)
        return row

    def _build_progress(self):
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress.setTextVisible(True)
        self.eta = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.progress, 1)
        row.addWidget(self.eta)
        return row

    # ---- per-row actions ------------------------------------------------

    def _selected_jobs(self) -> list:
        rows = {i.row() for i in self.table.selectionModel().selectedIndexes()}
        return [
            self.proxy.index(row, 0).data(JOB_ROLE)
            for row in sorted(rows)
            if self.proxy.index(row, 0).data(JOB_ROLE) is not None
        ]

    def _context_menu(self, point) -> None:
        jobs = self._selected_jobs()
        if not jobs:
            return
        menu = QMenu(self)
        ids = [j.id for j in jobs]

        regen = QAction(f"Regenerate ({len(jobs)})", self)
        regen.setToolTip("Clear the result and queue these prompts again")
        regen.triggered.connect(lambda: self.regenerate_requested.emit(ids))
        menu.addAction(regen)

        skip = QAction(f"Skip ({len(jobs)})", self)
        skip.setToolTip("Mark as skipped so runs pass over them")
        skip.triggered.connect(lambda: self.skip_requested.emit(ids))
        menu.addAction(skip)

        if len(jobs) == 1:
            edit = QAction("Edit prompt…", self)
            edit.setToolTip("Change the text and queue it again")
            edit.triggered.connect(lambda: self.edit_requested.emit(jobs[0].id))
            menu.addAction(edit)

        menu.addSeparator()
        images = [p for job in jobs for p in job.image_paths if Path(p).exists()]
        open_image = QAction("Open image", self)
        open_image.setEnabled(bool(images))
        open_image.triggered.connect(lambda: open_path(images[0]))
        menu.addAction(open_image)

        export_sel = QAction(f"Export these ({len(jobs)})…", self)
        export_sel.setEnabled(bool(images))
        export_sel.triggered.connect(lambda: self.export_requested.emit(ids))
        menu.addAction(export_sel)

        copy = QAction("Copy prompt", self)
        copy.triggered.connect(
            lambda: QGuiApplication.clipboard().setText("\n".join(j.prompt for j in jobs))
        )
        menu.addAction(copy)

        menu.exec(self.table.viewport().mapToGlobal(point))

    # ---- state ----------------------------------------------------------

    def set_jobs(self, jobs: list, counts: dict) -> None:
        self.model.set_jobs(jobs)
        pending = counts.get("pending", 0) + counts.get("failed", 0)
        total = sum(counts.values())
        shown = self.proxy.rowCount()
        extra = f"  ({shown} shown)" if shown != total else ""
        self.summary.setText(f"{pending} pending  ·  {total} total{extra}")
        if pending and not self.limit.hasFocus():
            self.limit.setMaximum(max(pending, 1))
            self.limit.setValue(pending)
        elif not pending:
            self.limit.setMaximum(1)

    def set_running(self, running: bool, total: int = 0) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_retry.setEnabled(not running)
        self.btn_stop.setEnabled(running)
        self.limit.setEnabled(not running)
        self.progress.setVisible(running)
        if running:
            self._run_started = time.monotonic()
            # Set the real range up front; otherwise the bar shows 0/100 until
            # the first job reports back.
            self.progress.setMaximum(max(total, 1))
            self.progress.setValue(0)
        self.eta.setText("")

    def set_progress(self, done: int, total: int) -> None:
        self._run_total = total
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat("%v / %m")
        if done >= total:
            self.eta.setText("")
            return
        if done and self._run_started:
            elapsed = time.monotonic() - self._run_started
            remaining = (elapsed / done) * (total - done)
            self.eta.setText(self._human(remaining))

    @staticmethod
    def _human(seconds: float) -> str:
        minutes = int(seconds // 60)
        if minutes < 1:
            return "less than a minute left"
        if minutes < 60:
            return f"about {minutes} min left"
        return f"about {minutes // 60}h {minutes % 60:02d}m left"
