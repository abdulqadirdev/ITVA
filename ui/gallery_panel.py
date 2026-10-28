"""Results tab: thumbnails of everything this project has generated."""

from __future__ import annotations

from pathlib import Path

from imgen.platforms import open_path
from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

THUMB = QSize(240, 135)


class ThumbnailLoader(QThread):
    """Decodes thumbnails off the UI thread.

    QImageReader.setScaledSize lets libjpeg downscale while decoding, so a
    1376x768 result costs a fraction of loading it full size — which matters
    when a project holds a few hundred of them.
    """

    ready = Signal(int, QPixmap)

    def __init__(self, paths: list[str]):
        super().__init__()
        self.paths = paths
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        for index, path in enumerate(self.paths):
            if self._stop:
                return
            reader = QImageReader(path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid():
                scaled = size.scaled(THUMB, Qt.KeepAspectRatio)
                reader.setScaledSize(scaled)
            image = reader.read()
            if not image.isNull():
                self.ready.emit(index, QPixmap.fromImage(image))


class GalleryPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.project = None
        self.base_cfg = None
        self.loader: ThumbnailLoader | None = None
        self.paths: list[str] = []

        self.view = QListWidget()
        self.view.setViewMode(QListWidget.IconMode)
        self.view.setIconSize(THUMB)
        self.view.setGridSize(QSize(THUMB.width() + 24, THUMB.height() + 48))
        self.view.setResizeMode(QListWidget.Adjust)
        self.view.setMovement(QListWidget.Static)
        self.view.setSpacing(6)
        self.view.setWordWrap(True)
        self.view.setSelectionMode(QListWidget.ExtendedSelection)
        self.view.itemDoubleClicked.connect(self._open_item)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)

        self.summary = QLabel("—")
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.reload)
        open_folder = QPushButton("Open folder")
        open_folder.clicked.connect(self._open_folder)

        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(refresh)
        top.addWidget(open_folder)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.view)

    def set_project(self, project, base_cfg) -> None:
        self.project = project
        self.base_cfg = base_cfg
        self.reload()

    def reload(self) -> None:
        self._stop_loader()
        self.view.clear()
        self.paths = []
        if self.project is None:
            self.summary.setText("—")
            return

        from imgen.db import JobStore

        store = JobStore(self.project.config(self.base_cfg).run.db_path)
        try:
            jobs = store.done_with_images()
        finally:
            store.close()

        for job in jobs:
            for path in job.image_paths:
                if not Path(path).exists():
                    continue
                item = QListWidgetItem(f"row {job.row_number}")
                item.setToolTip(f"row {job.row_number}\n\n{job.prompt}")
                item.setData(Qt.UserRole, path)
                item.setData(Qt.UserRole + 1, job.id)
                item.setTextAlignment(Qt.AlignHCenter)
                self.view.addItem(item)
                self.paths.append(path)

        self.summary.setText(
            f"{len(self.paths)} image(s) from {len(jobs)} prompt(s)"
            if self.paths
            else "nothing generated yet"
        )
        if self.paths:
            self.loader = ThumbnailLoader(self.paths)
            self.loader.ready.connect(self._set_thumb)
            self.loader.start()

    def _set_thumb(self, index: int, pixmap: QPixmap) -> None:
        item = self.view.item(index)
        if item is not None:
            item.setIcon(pixmap)

    def _menu(self, point) -> None:
        chosen = self.view.selectedItems()
        if not chosen:
            return
        menu = QMenu(self)
        open_action = QAction("Open", self)
        open_action.triggered.connect(lambda: self._open_item(chosen[0]))
        menu.addAction(open_action)
        menu.addSeparator()

        keep = QAction("Keep only this, discard the other variants", self)
        keep.setEnabled(len(chosen) == 1)
        keep.triggered.connect(lambda: self._keep_only(chosen[0]))
        menu.addAction(keep)

        drop = QAction(f"Delete selected ({len(chosen)})", self)
        drop.triggered.connect(lambda: self._delete([i for i in chosen]))
        menu.addAction(drop)
        menu.exec(self.view.viewport().mapToGlobal(point))

    def _keep_only(self, item: QListWidgetItem) -> None:
        """Discard a prompt's other variants, keeping the chosen one."""
        job_id = item.data(Qt.UserRole + 1)
        others = [
            self.view.item(i)
            for i in range(self.view.count())
            if self.view.item(i).data(Qt.UserRole + 1) == job_id
            and self.view.item(i) is not item
        ]
        if not others:
            return
        self._delete(others, question=f"Discard {len(others)} other variant(s)?")

    def _delete(self, items: list, question: str | None = None) -> None:
        from imgen.db import JobStore

        paths = [i.data(Qt.UserRole) for i in items]
        answer = QMessageBox.warning(
            self,
            "Delete images",
            (question or f"Delete {len(paths)} image file(s)?")
            + "\n\nThe files are removed from disk. The prompts stay in the "
            "queue, so they can be regenerated.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return

        removed = set()
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
                removed.add(path)
            except OSError:
                pass

        store = JobStore(self.project.config(self.base_cfg).run.db_path)
        try:
            for job in store.done_with_images():
                kept = [p for p in job.image_paths if p not in removed]
                if len(kept) != len(job.image_paths):
                    store.set_images(job.id, kept)
        finally:
            store.close()
        self.reload()

    def _open_item(self, item: QListWidgetItem) -> None:
        open_path(item.data(Qt.UserRole))

    def _open_folder(self) -> None:
        if self.project is None:
            return
        self.project.output_dir.mkdir(parents=True, exist_ok=True)
        open_path(self.project.output_dir)

    def _stop_loader(self) -> None:
        if self.loader is not None:
            self.loader.stop()
            self.loader.wait(3000)
            self.loader = None

    def closeEvent(self, event) -> None:
        self._stop_loader()
        event.accept()
