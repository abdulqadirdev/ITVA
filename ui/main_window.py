"""Main window: project sidebar, tabs, menus and the live log."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import QSystemTrayIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPlainTextEdit as _QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from imgen import chrome, export, projects, usage
from imgen.platforms import open_path
from imgen.config import Config, load_config
from imgen.db import JobStore

from .gallery_panel import GalleryPanel
from .queue_panel import QueuePanel
from .settings_panel import SettingsPanel
from .source_panel import SourcePanel
from .setup_panel import SetupPanel
from .thumbnail_panel import ThumbnailPanel
from .update_banner import UpdateBanner
from .video_panel import VideoPanel
from .worker import (
    GenerationWorker,
    HealthWorker,
    LaunchWorker,
    NarrateWorker,
    ProbeWorker,
    FixWorker,
    ProduceWorker,
    ThumbnailWorker,
    UpdateCheckWorker,
    UpdateInstallWorker,
)

HEALTH_TEXT = {
    "ready": ("Flow: ready", "#2e9e52"),
    "signed-out": ("Flow: signed out", "#e2666a"),
    "no-chrome": ("Flow: browser not running", "#d79a3a"),
    "no-prompt-box": ("Flow: selectors stale", "#d79a3a"),
    "error": ("Flow: error", "#e2666a"),
}


class MainWindow(QMainWindow):
    def __init__(self, base_cfg: Config):
        super().__init__()
        self.base_cfg = base_cfg
        self.project = None
        self.gen_worker: GenerationWorker | None = None
        self.launch_worker: LaunchWorker | None = None
        self.health_worker: HealthWorker | None = None
        self.video_worker = None
        #: Every background thread started from here. A QThread destroyed
        #: while still running aborts the process, so none may outlive us.
        self._threads: list = []
        self.settings = QSettings("FlowBulkGenerator", "app")

        self.setWindowTitle("Flow Bulk Generator")
        self.resize(1320, 840)

        self.source_panel = SourcePanel(base_cfg)
        self.queue_panel = QueuePanel()
        self.gallery_panel = GalleryPanel()
        self.video_panel = VideoPanel(base_cfg)
        self.thumbnail_panel = ThumbnailPanel(base_cfg)
        self.settings_panel = SettingsPanel(base_cfg)

        self.tabs = QTabWidget()
        self.setup_panel = SetupPanel()
        self.setup_panel.fix_requested.connect(self.run_fix)
        self.setup_panel.recheck_requested.connect(self.refresh_setup)
        self.setup_panel.sign_in_requested.connect(self.launch_chrome)
        self.tabs.addTab(self.setup_panel, "Setup")
        self.tabs.addTab(self.source_panel, "Source")
        self.tabs.addTab(self.queue_panel, "Queue")
        self.tabs.addTab(self.gallery_panel, "Results")
        self.tabs.addTab(self.video_panel, "Video")
        self.tabs.addTab(self.thumbnail_panel, "Thumbnail")
        # The settings form is taller than the window on a laptop screen, and
        # the token reference at the bottom was being cut off.
        settings_scroll = QScrollArea()
        settings_scroll.setWidget(self.settings_panel)
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QScrollArea.NoFrame)
        self.tabs.addTab(settings_scroll, "Settings")

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([250, 1070])

        # A strip above everything, hidden until there is something to say.
        self.update_banner = UpdateBanner()
        self.update_banner.install_requested.connect(self.install_update)
        self.update_banner.restart_requested.connect(self.restart_app)

        central = QWidget()
        stack = QVBoxLayout(central)
        stack.setContentsMargins(0, 0, 0, 0)
        stack.setSpacing(0)
        stack.addWidget(self.update_banner)
        stack.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.log_view = QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_dock = QDockWidget("Log", self)
        # saveState/restoreState identify docks and toolbars by objectName;
        # without it the saved layout is dropped on the next start.
        self.log_dock.setObjectName("log_dock")
        self.log_dock.setWidget(self.log_view)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.log_dock)

        self._build_menus()
        self.lbl_chrome = QLabel()
        self.lbl_health = QLabel()
        self.lbl_usage = QLabel()
        self.lbl_project = QLabel()
        self.statusBar().addWidget(self.lbl_chrome)
        self.statusBar().addWidget(self.lbl_usage)
        self.statusBar().addWidget(self.lbl_health)
        self.statusBar().addPermanentWidget(self.lbl_project)

        self.source_panel.loaded.connect(lambda _n: self.refresh())
        self.settings_panel.saved.connect(lambda: self.append_log("settings saved"))
        self.queue_panel.start_requested.connect(self.start_run)
        self.queue_panel.stop_requested.connect(self.stop_run)
        self.queue_panel.retry_requested.connect(self.retry_failed)
        self.queue_panel.regenerate_requested.connect(self.regenerate)
        self.queue_panel.skip_requested.connect(self.skip)
        self.queue_panel.edit_requested.connect(self.edit_prompt)
        self.queue_panel.export_requested.connect(self.export_selected)
        self.video_panel.narrate_requested.connect(self.narrate)
        self.video_panel.build_requested.connect(self.build_video)
        self.video_panel.stop_requested.connect(self.stop_video)
        self.video_panel.setting_changed.connect(self.save_video_setting)
        self.video_panel.animation_changed.connect(self.save_row_animation)
        self.thumbnail_panel.blocks_changed.connect(self.save_thumbnail_blocks)
        self.thumbnail_panel.suggest_requested.connect(self.suggest_thumbnail)
        self.thumbnail_panel.queue_requested.connect(self.queue_thumbnail)
        self.thumbnail_panel.build_requested.connect(self.build_thumbnail)
        self.video_panel.register_worker = self.track

        self._restore_window()
        self.reload_projects(self.settings.value("last_project", type=str))
        self._health_timer = QTimer(self)
        self._health_timer.timeout.connect(self.update_chrome_state)
        self._health_timer.timeout.connect(self.update_usage)
        self._health_timer.start(5000)
        self.update_chrome_state()
        self.update_usage()

        # Land on Setup when something is actually missing. Someone who
        # has just installed the .deb and opened it should be told what
        # is left, not shown a Start button that fails.
        if self.refresh_setup():
            self.tabs.setCurrentWidget(self.setup_panel)

        # After the window is up, never before it: a slow or unreachable
        # GitHub must not delay anything the user can see. The check is
        # cached, so most launches do not reach the network at all.
        if self.base_cfg.update.check_on_launch:
            QTimer.singleShot(2500, lambda: self.check_updates(False))

    # ---- construction ---------------------------------------------------

    def _build_sidebar(self) -> QWidget:
        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(self._project_selected)

        buttons = QHBoxLayout()
        for label, slot in (
            ("New", self.new_project),
            ("Rename", self.rename_project),
            ("Delete", self.delete_project),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(6, 6, 0, 6)
        layout.addWidget(QLabel("<b>Projects</b>"))
        layout.addWidget(self.project_list, 1)
        layout.addLayout(buttons)
        return panel

    def _act(self, text, slot, shortcut=None, tip=None) -> QAction:
        action = QAction(text, self)
        action.triggered.connect(slot)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setStatusTip(tip)
        return action

    def _build_menus(self) -> None:
        bar = self.menuBar()

        self.act_launch = self._act(
            "Launch Chrome", self.launch_chrome, tip="Start the automation browser"
        )
        self.act_start = self._act(
            "Start run", lambda: self.start_run(self.queue_panel.limit.value()), "Ctrl+R"
        )
        self.act_stop = self._act("Stop", self.stop_run, "Ctrl+.")
        self.act_health = self._act("Check Flow", self.check_health, "Ctrl+H")

        run_menu = bar.addMenu("&Run")
        for action in (self.act_launch, self.act_health):
            run_menu.addAction(action)
        run_menu.addAction(self._act("Repair selectors…", self.repair_selectors))
        run_menu.addSeparator()
        for action in (self.act_start, self.act_stop):
            run_menu.addAction(action)
        run_menu.addAction(self._act("Retry failed", self.retry_failed))

        project_menu = bar.addMenu("&Project")
        project_menu.addAction(self._act("New…", self.new_project, "Ctrl+N"))
        project_menu.addAction(self._act("Rename…", self.rename_project))
        project_menu.addAction(self._act("Duplicate…", self.duplicate_project))
        project_menu.addAction(self._act("Delete…", self.delete_project))
        project_menu.addSeparator()
        project_menu.addAction(self._act("Open output folder", self.open_output))
        project_menu.addAction(self._act("Open project folder", self.open_project_folder))
        project_menu.addAction(self._act("Open run log", self.open_run_log))

        export_menu = bar.addMenu("&Export")
        export_menu.addAction(
            self._act("Images + index as .zip…", self.export_zip, "Ctrl+E")
        )
        export_menu.addAction(self._act("Index as .csv…", self.export_csv))

        view_menu = bar.addMenu("&View")
        view_menu.addAction(self.log_dock.toggleViewAction())
        view_menu.addAction(self._act("Refresh", self.refresh, "F5"))

        help_menu = bar.addMenu("&Help")
        help_menu.addAction(
            self._act("Check for updates", lambda: self.check_updates(True))
        )
        help_menu.addAction(self._act("About", self.about))

        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("main_toolbar")
        toolbar.setMovable(False)
        for action in (self.act_launch, self.act_health, self.act_start, self.act_stop):
            toolbar.addAction(action)
        self.act_stop.setEnabled(False)

    # ---- projects -------------------------------------------------------

    def reload_projects(self, select_slug: str | None = None) -> None:
        projects.ensure_default(self.base_cfg)
        self.project_list.blockSignals(True)
        self.project_list.clear()
        for project in projects.list_projects():
            item = QListWidgetItem(project.name)
            item.setData(Qt.UserRole, project.root)
            self.project_list.addItem(item)
            if project.slug == select_slug:
                self.project_list.setCurrentItem(item)
        self.project_list.blockSignals(False)
        if self.project_list.currentRow() < 0:
            self.project_list.setCurrentRow(0)
        else:
            self._project_selected(self.project_list.currentItem(), None)

    def _project_selected(self, current, _previous) -> None:
        if current is None:
            return
        self.project = projects.Project.load(current.data(Qt.UserRole))
        # A project carried from another machine records image paths that
        # do not exist here; repaired now rather than discovered later as
        # an empty gallery or a video that cannot find its pictures.
        repair = projects.prepare(self.project)
        if any(repair.values()):
            self.append_log(
                f"repaired {repair['images_repaired']} image path(s), "
                f"{repair['audio_moved']} narration path(s) "
                f"for '{self.project.name}'"
            )
        self.settings.setValue("last_project", self.project.slug)
        self.source_panel.set_project(self.project)
        self.settings_panel.set_project(self.project)
        self.gallery_panel.set_project(self.project, self.base_cfg)
        self.video_panel.set_project(self.project, self.project.config(self.base_cfg))
        self.thumbnail_panel.set_project(
            self.project, self.project.config(self.base_cfg)
        )
        self.lbl_project.setText(f"Project: {self.project.name}")
        self.refresh()

    def new_project(self) -> None:
        name, ok = QInputDialog.getText(self, "New project", "Name")
        if ok and name.strip():
            created = projects.Project.create(name.strip())
            self.append_log(f"created project '{created.name}' at {created.root}")
            self.reload_projects(created.slug)

    def rename_project(self) -> None:
        if self.project is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename project", "Name", text=self.project.name
        )
        if ok and name.strip():
            self.project.name = name.strip()
            self.project.save()
            self.reload_projects(self.project.slug)

    def delete_project(self) -> None:
        if self.project is None:
            return
        answer = QMessageBox.warning(
            self,
            "Delete project",
            f"Delete '{self.project.name}' and everything in it?\n\n{self.project.root}\n\n"
            "Generated images, the manifest and the queue are all removed. "
            "This cannot be undone.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            root = self.project.root
            self.project.delete()
            self.project = None
            self.append_log(f"deleted {root}")
            self.reload_projects()

    # ---- running --------------------------------------------------------

    def cfg(self) -> Config | None:
        return None if self.project is None else self.project.config(self.base_cfg)

    def start_run(self, limit: int) -> None:
        cfg = self.cfg()
        if cfg is None or (self.gen_worker and self.gen_worker.isRunning()):
            return
        if not chrome.is_up(cfg.chrome):
            self.append_log("Chrome is not running — use Run ▸ Launch Chrome first")
            return

        if limit > cfg.run.confirm_above:
            answer = QMessageBox.question(
                self,
                "Start a large run?",
                f"This will generate for <b>{limit}</b> prompts in "
                f"'{self.project.name}'.<br><br>"
                "Each one spends Flow credits, and at the current pace that is roughly "
                f"<b>{self._estimate(limit, cfg)}</b>.<br><br>Start?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return

        self._set_running(True, limit)
        self.append_log(f"--- run started: {limit} job(s) in '{self.project.name}' ---")
        self.gen_worker = GenerationWorker(cfg, limit=limit)
        self.track(self.gen_worker)
        self.gen_worker.log.connect(self.append_log)
        self.gen_worker.progress.connect(self.refresh)
        self.gen_worker.counts.connect(self.queue_panel.set_progress)
        self.gen_worker.finished_run.connect(self._run_done)
        self.gen_worker.start()

    @staticmethod
    def _estimate(limit: int, cfg: Config) -> str:
        per_job = 40 + cfg.flow.settle_seconds + (cfg.run.delay_min_s + cfg.run.delay_max_s) / 2
        minutes = round(limit * per_job / 60)
        return f"{minutes} minutes" if minutes < 60 else f"{minutes // 60}h {minutes % 60:02d}m"

    def _set_running(self, running: bool, total: int = 0) -> None:
        self.queue_panel.set_running(running, total)
        self.act_start.setEnabled(not running)
        self.act_stop.setEnabled(running)

    def stop_run(self) -> None:
        if self.gen_worker and self.gen_worker.isRunning():
            self.gen_worker.request_stop()
            self.append_log("stopping after the current job finishes …")
            self.act_stop.setEnabled(False)
            self.queue_panel.btn_stop.setEnabled(False)

    def _run_done(self, summary: str, reason: str) -> None:
        self._set_running(False)
        if summary:
            self.append_log(f"--- run finished: {summary} ---")
        if reason:
            self.append_log(f"stopped: {reason}")
        self.refresh()
        self.update_usage()
        self.gallery_panel.reload()
        self.notify(
            f"Run finished — {self.project.name}" if self.project else "Run finished",
            summary or reason or "no work done",
        )

    def retry_failed(self) -> None:
        self._with_store(lambda store: self.append_log(f"requeued {store.retry_failed()} job(s)"))

    def regenerate(self, job_ids: list) -> None:
        self._with_store(
            lambda store: self.append_log(
                f"queued {store.requeue_ids(job_ids)} job(s) for regeneration"
            )
        )

    def skip(self, job_ids: list) -> None:
        self._with_store(
            lambda store: self.append_log(f"skipped {store.skip_ids(job_ids)} job(s)")
        )

    def _with_store(self, action) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        store = JobStore(cfg.run.db_path)
        try:
            action(store)
        finally:
            store.close()
        self.refresh()

    def edit_prompt(self, job_id: int) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        store = JobStore(cfg.run.db_path)
        try:
            job = next((j for j in store.all_jobs() if j.id == job_id), None)
            if job is None:
                return
            text, ok = QInputDialog.getMultiLineText(
                self, "Edit prompt", f"Row {job.row_number}", job.prompt
            )
            if not ok or not text.strip() or text.strip() == job.prompt:
                return
            if store.update_prompt(job_id, text):
                self.append_log(f"row {job.row_number}: prompt edited and requeued")
            else:
                QMessageBox.information(
                    self,
                    "Not changed",
                    "Another row already has that exact prompt, so this edit "
                    "would merge the two. Change the wording slightly.",
                )
        finally:
            store.close()
        self.refresh()

    def export_selected(self, job_ids: list) -> None:
        jobs = [j for j in self._done_jobs() if j.id in set(job_ids)]
        self._export_jobs(jobs, suffix="-selection")

    def duplicate_project(self) -> None:
        if self.project is None:
            return
        name, ok = QInputDialog.getText(
            self, "Duplicate project", "Name", text=f"{self.project.name} copy"
        )
        if ok and name.strip():
            copy = self.project.duplicate(name.strip())
            self.append_log(
                f"duplicated settings and source into '{copy.name}' (results not copied)"
            )
            self.reload_projects(copy.slug)

    def repair_selectors(self) -> None:
        if getattr(self, "probe_worker", None) is not None and self.probe_worker.isRunning():
            return
        self.append_log("probing the live Flow page …")
        self.probe_worker = ProbeWorker(self.cfg() or self.base_cfg)
        self.track(self.probe_worker)
        self.probe_worker.finished_probe.connect(self._show_probe)
        self.probe_worker.start()

    def _show_probe(self, report: str, detected: dict) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Flow page — selector report")
        dialog.resize(820, 620)
        view = _QPlainTextEdit(report, readOnly=True)
        view.setLineWrapMode(_QPlainTextEdit.NoWrap)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        copy = buttons.addButton("Copy report", QDialogButtonBox.ActionRole)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(report))
        layout = QVBoxLayout(dialog)
        layout.addWidget(
            QLabel(
                "Selectors live in <code>config/selectors.toml</code>. Copy the "
                "working ones from this report into that file when Flow's UI changes."
            )
        )
        layout.addWidget(view)
        layout.addWidget(buttons)
        dialog.exec()

    def notify(self, title: str, message: str) -> None:
        """Desktop notification, when the platform offers one."""
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if getattr(self, "_tray", None) is None:
            from PySide6.QtWidgets import QStyle

            self._tray = QSystemTrayIcon(
                self.style().standardIcon(QStyle.SP_ComputerIcon), self
            )
            self._tray.show()
        self._tray.showMessage(title, message, QSystemTrayIcon.Information, 8000)

    def track(self, worker) -> None:
        """Remember a thread so closeEvent can wait for it."""
        self._threads = [t for t in self._threads if t.isRunning()]
        self._threads.append(worker)

    def _wait_for_threads(self, timeout_ms: int = 10000) -> None:
        for worker in self._threads:
            if worker.isRunning():
                if hasattr(worker, "request_stop"):
                    worker.request_stop()
                worker.wait(timeout_ms)
        self._threads.clear()

    # ---- video ----------------------------------------------------------

    def refresh_video(self) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        from imgen.produce import Producer

        store = JobStore(cfg.run.db_path)
        try:
            self.video_panel.set_rows(Producer(cfg).rows(store))
        finally:
            store.close()

    def save_row_animation(self, job_id: int, action: str) -> None:
        """Per-row animation lives in the queue, not in the source.

        The sheet may have no such column, and writing to it needs a
        service account the app may not have, so the choice is stored
        beside the row and survives a re-sync.
        """
        cfg = self.cfg()
        if cfg is None:
            return
        store = JobStore(cfg.run.db_path)
        try:
            store.set_field(job_id, cfg.sheets.animation_column, action)
        finally:
            store.close()
        self.refresh_video()

    # ---- thumbnail -------------------------------------------------------

    def thumbnail_picture(self, cfg, store):
        """The generated picture for the thumbnail, if there is one yet."""
        from imgen.thumbnail import is_thumbnail_job

        for job in store.done_with_images():
            if not is_thumbnail_job(job):
                continue
            for path in job.image_paths:
                if Path(path).exists():
                    return Path(path)
        return None

    def refresh_thumbnail(self) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        from imgen.thumbnail import description_in

        store = JobStore(cfg.run.db_path)
        try:
            rows = store.all_jobs()
            description = description_in(rows, cfg.sheets.thumbnail_column)
            picture = self.thumbnail_picture(cfg, store)
        finally:
            store.close()
        self.thumbnail_panel.set_description(description, picture)

    def save_thumbnail_blocks(self, blocks: list) -> None:
        if self.project is None:
            return
        self.project.overrides.setdefault("thumbnail", {})["blocks"] = blocks
        self.project.save()
        self.thumbnail_panel.cfg = self.project.config(self.base_cfg)

    def suggest_thumbnail(self) -> None:
        """Propose the words from the lines the script already has."""
        cfg = self.cfg()
        if cfg is None:
            return
        from imgen.thumbnail import suggest_blocks

        store = JobStore(cfg.run.db_path)
        try:
            blocks = suggest_blocks(
                store.all_jobs(), cfg.sheets.scene_column
            )
        finally:
            store.close()
        if not blocks:
            self.append_log("nothing in the sheet to suggest a thumbnail from")
            return
        self.thumbnail_panel.set_blocks(blocks)
        self.append_log(f"suggested {len(blocks)} block(s) from the script")

    def queue_thumbnail(self) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        from imgen.thumbnail import description_in, ensure_job

        store = JobStore(cfg.run.db_path)
        try:
            description = description_in(
                store.all_jobs(), cfg.sheets.thumbnail_column
            )
            queued = ensure_job(store, description)
        finally:
            store.close()
        self.append_log(
            "thumbnail picture queued - press Start run to generate it"
            if queued
            else "the sheet does not describe a thumbnail yet"
        )
        self.refresh()

    def build_thumbnail(self) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        store = JobStore(cfg.run.db_path)
        try:
            picture = self.thumbnail_picture(cfg, store)
        finally:
            store.close()
        if picture is None:
            self.append_log("no thumbnail picture yet - queue it and run first")
            return
        if getattr(self, "_thumb", None) is not None and self._thumb.isRunning():
            return

        self.thumbnail_panel.btn_build.setEnabled(False)
        self._thumb = ThumbnailWorker(
            cfg, picture, self.project.output_dir / "thumbnail.jpg"
        )
        self._thumb.note.connect(self.append_log)
        self._thumb.done.connect(self._thumbnail_done)
        self._thumb.failed.connect(self._thumbnail_failed)
        self.track(self._thumb)
        self._thumb.start()

    def _thumbnail_done(self, path: str) -> None:
        self.thumbnail_panel.btn_build.setEnabled(True)
        self.thumbnail_panel.show_result(Path(path))
        self.append_log(f"thumbnail written to {path}")

    def _thumbnail_failed(self, message: str) -> None:
        self.thumbnail_panel.btn_build.setEnabled(True)
        self.append_log(f"thumbnail: {message}")

    def save_video_setting(self, key: str, value) -> None:
        """Video toggles live in the tab itself; they are still per-project."""
        if self.project is None:
            return
        self.project.overrides.setdefault("video", {})[key] = value
        self.project.save()
        self.video_panel.cfg = self.project.config(self.base_cfg)
        self.settings_panel.set_project(self.project)

    def _start_video_worker(self, worker, total: int = 0) -> None:
        self.video_worker = worker
        self.track(worker)
        worker.log.connect(self.append_log)
        worker.counts.connect(self.video_panel.set_progress)
        worker.finished_run.connect(self._video_done)
        self.video_panel.set_busy(True, total)
        worker.start()

    def narrate(self, job_ids: list, force: bool) -> None:
        cfg = self.cfg()
        if cfg is None:
            return
        self.append_log("--- narration started ---")
        self._start_video_worker(NarrateWorker(cfg, job_ids or None, force))

    def build_video(self, limit: int) -> None:
        cfg = self.cfg()
        if cfg is None or (self.video_worker and self.video_worker.isRunning()):
            return
        name = "preview" if limit else "video"
        if not limit:
            from imgen.produce import Producer

            store = JobStore(cfg.run.db_path)
            try:
                rows = Producer(cfg).rows(store)
            finally:
                store.close()
            minutes = sum((r.duration_s or cfg.video.fallback_duration_s) for r in rows) / 60
            answer = QMessageBox.question(
                self,
                "Build the video?",
                f"This renders <b>{len(rows)}</b> clips into roughly "
                f"<b>{minutes:.1f} minutes</b> of video at "
                f"{cfg.video.width}x{cfg.video.height}.<br><br>"
                "Rows without narration are voiced first, which needs a connection."
                "<br><br>Continue?",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                return

        self.append_log(f"--- building {name} ---")
        self._start_video_worker(ProduceWorker(cfg, name=name, limit=limit or None))

    def stop_video(self) -> None:
        if self.video_worker and self.video_worker.isRunning():
            self.video_worker.request_stop()
            self.append_log("stopping after the current step ...")

    def _video_done(self, summary: str, error: str) -> None:
        self.video_panel.set_busy(False)
        if summary:
            self.append_log(f"--- done: {summary} ---")
            self.notify("Video ready", summary)
        if error:
            self.append_log(f"failed: {error}")
            QMessageBox.warning(self, "Video", error)
        self.refresh_video()

    # ---- health ---------------------------------------------------------

    def launch_chrome(self) -> None:
        self.act_launch.setEnabled(False)
        self.launch_worker = LaunchWorker(self.base_cfg)
        self.track(self.launch_worker)
        self.launch_worker.log.connect(self.append_log)
        self.launch_worker.done.connect(self._launch_done)
        self.launch_worker.start()

    def _launch_done(self, ok: bool, message: str) -> None:
        self.act_launch.setEnabled(True)
        self.append_log(("chrome: " if ok else "chrome failed: ") + message)
        if ok:
            self.append_log("first run? sign in to Google in that window")
        self.update_chrome_state()

    def check_health(self) -> None:
        if self.health_worker is not None and self.health_worker.isRunning():
            return
        cfg = self.cfg() or self.base_cfg
        self.lbl_health.setText("Flow: checking…")
        self.act_health.setEnabled(False)
        self.health_worker = HealthWorker(cfg)
        self.track(self.health_worker)
        self.health_worker.result.connect(self._health_done)
        self.health_worker.start()

    def _health_done(self, state: str, detail: str) -> None:
        self.act_health.setEnabled(True)
        text, colour = HEALTH_TEXT.get(state, ("Flow: unknown", "#8b8b8b"))
        self.lbl_health.setText(f"<span style='color:{colour}'>{text}</span>")
        self.append_log(f"flow health: {state} — {detail}")

    def update_usage(self) -> None:
        cfg = self.cfg() or self.base_cfg
        today = usage.today()
        left = usage.remaining(cfg.run.daily_image_cap)
        self.lbl_usage.setText(
            f"Today: {today} image(s)"
            + (f" · {left} left of {cfg.run.daily_image_cap}" if left is not None else "")
            + "   |"
        )

    def update_chrome_state(self) -> None:
        status = chrome.status(self.base_cfg.chrome)
        if status.running:
            self.lbl_chrome.setText(
                f"Chrome {status.version}  ·  CDP {self.base_cfg.chrome.debug_port}   |"
            )
            self.act_launch.setText("Chrome running")
            self.act_launch.setEnabled(False)
        else:
            self.lbl_chrome.setText("Chrome: not running   |")
            self.act_launch.setText("Launch Chrome")
            self.act_launch.setEnabled(True)

    # ---- export / folders -----------------------------------------------

    def _done_jobs(self) -> list:
        cfg = self.cfg()
        if cfg is None:
            return []
        store = JobStore(cfg.run.db_path)
        try:
            return store.done_with_images()
        finally:
            store.close()

    def export_zip(self) -> None:
        self._export_jobs(self._done_jobs())

    def _export_jobs(self, jobs: list, suffix: str = "-images") -> None:
        if not jobs:
            self.append_log("nothing to export — those rows have no images yet")
            return
        suggested = str(Path.home() / f"{self.project.slug}{suffix}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Export", suggested, "Zip archive (*.zip)")
        if not path:
            return
        images, count = export.export_zip(jobs, Path(path))
        self.append_log(f"exported {images} image(s) from {count} prompt(s) to {path}")

    def export_csv(self) -> None:
        jobs = self._done_jobs()
        if not jobs:
            self.append_log("nothing to export yet")
            return
        suggested = str(Path.home() / f"{self.project.slug}-index.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export", suggested, "CSV (*.csv)")
        if path:
            self.append_log(f"wrote {export.export_csv(jobs, Path(path))} row(s) to {path}")

    def open_output(self) -> None:
        if self.project:
            self.project.output_dir.mkdir(parents=True, exist_ok=True)
            open_path(self.project.output_dir)

    def open_project_folder(self) -> None:
        if self.project:
            open_path(self.project.root)

    def open_run_log(self) -> None:
        if self.project is None:
            return
        path = self.project.root / "run.log"
        if path.exists():
            open_path(path)
        else:
            self.append_log("no run log yet")

    # ---- setup -----------------------------------------------------------

    def refresh_setup(self) -> None:
        """Re-run the readiness checks and redraw the Setup tab."""
        from imgen.readiness import blocking, checks

        results = checks(self.base_cfg)
        self.setup_panel.show_checks(results)
        return blocking(results)

    def run_fix(self, check) -> None:
        if getattr(self, "_fix", None) is not None and self._fix.isRunning():
            return
        self.setup_panel.set_busy(check.key, "Working...")
        self.setup_panel.set_status(
            "A password may be asked for - that is the system's own prompt."
            if check.fix_command and check.fix_command[0] == "pkexec"
            else "Downloading. This can take a few minutes."
        )
        self.append_log(f"setup: {' '.join(check.fix_command)}")
        self._fix = FixWorker(check)
        self._fix.done.connect(lambda ok, msg: self._fix_finished(check, ok, msg))
        self.track(self._fix)
        self._fix.start()

    def _fix_finished(self, check, ok: bool, message: str) -> None:
        if ok:
            self.append_log(f"setup: {check.title} - done")
            self.setup_panel.set_status("")
        else:
            self.append_log(f"setup: {check.title} - {message}")
            self.setup_panel.set_status(f"{check.title}: {message}")
        # Adding the flathub remote is only half the job; the install
        # itself is the next press, and re-checking offers it.
        self.refresh_setup()
        self.update_chrome_state()

    # ---- updates ---------------------------------------------------------

    def check_updates(self, asked: bool = False) -> None:
        """Ask GitHub whether a newer release exists.

        `asked` is True when the user chose Help > Check for updates: then
        the answer is reported either way, and the cache is bypassed. The
        automatic check on launch stays silent unless there is something.
        """
        cfg = self.base_cfg
        if not cfg.update.repo:
            if asked:
                self.append_log(
                    "no update source is set - put the GitHub repository in "
                    "settings as update.repo = \"owner/name\""
                )
            return
        if getattr(self, "_update_check", None) is not None and (
            self._update_check.isRunning()
        ):
            return

        if not asked:
            from imgen.update import read_cache

            if read_cache(cfg.run.db_path.parent) is not None:
                return

        self._update_asked = asked
        self._update_check = UpdateCheckWorker(cfg)
        self._update_check.found.connect(self._update_found)
        self._update_check.failed.connect(self._update_check_failed)
        self.track(self._update_check)
        self._update_check.start()

    def _update_found(self, release) -> None:
        from imgen.update import write_cache

        write_cache(self.base_cfg.run.db_path.parent, release)
        if release is None:
            if getattr(self, "_update_asked", False):
                self.append_log("this is the newest version")
            return
        self.append_log(f"version {release.version} is available")
        self.update_banner.offer(release)

    def _update_check_failed(self, message: str) -> None:
        # Silent unless the user asked: a laptop that opens the app on a
        # train should not be told off about the network.
        if getattr(self, "_update_asked", False):
            self.append_log(f"update check failed: {message}")

    def install_update(self, release) -> None:
        busy = getattr(self, "gen_worker", None)
        if busy is not None and busy.isRunning():
            QMessageBox.information(
                self, "Busy",
                "A run is in progress. Stop it first, then update.",
            )
            return
        if getattr(self, "_update_install", None) is not None and (
            self._update_install.isRunning()
        ):
            return

        self.update_banner.set_busy("Downloading...")
        self._update_install = UpdateInstallWorker(self.base_cfg, release)
        self._update_install.progress.connect(self.update_banner.set_progress)
        self._update_install.note.connect(self.append_log)
        self._update_install.note.connect(self.update_banner.set_busy)
        self._update_install.done.connect(self.update_banner.set_installed)
        self._update_install.failed.connect(self._update_install_failed)
        self.track(self._update_install)
        self._update_install.start()

    def _update_install_failed(self, message: str) -> None:
        self.append_log(f"update failed: {message}")
        self.update_banner.set_failed(message)

    def restart_app(self) -> None:
        """Close this window and start the newly installed copy.

        The running process keeps the files it already opened, so the old
        build carries on working until it exits - which is why this is
        offered rather than forced.
        """
        import subprocess
        import sys

        from imgen.platforms import detach_kwargs

        command = (
            [sys.executable] + sys.argv
            if not getattr(sys, "frozen", False)
            else [sys.executable]
        )
        try:
            subprocess.Popen(command, **detach_kwargs())
        except OSError as exc:
            self.append_log(f"could not restart automatically: {exc}")
            return
        self.close()

    def about(self) -> None:
        QMessageBox.information(
            self,
            "Flow Bulk Generator",
            "<b>Flow Bulk Generator</b><br><br>"
            "Generates images with Google Flow from a sheet or file of prompts.<br><br>"
            "Flow has no API — it is driven inside a real Chrome profile you signed "
            "in to once, over the DevTools Protocol. No Flow credential is ever "
            "stored or handled.<br><br>"
            f"Automation profile:<br><code>{self.base_cfg.chrome.host_user_data_dir}</code>",
        )

    # ---- state ----------------------------------------------------------

    def append_log(self, line: str) -> None:
        self.log_view.appendPlainText(line)

    def refresh(self) -> None:
        cfg = self.cfg()
        if cfg is None:
            self.queue_panel.set_jobs([], {})
            return
        store = JobStore(cfg.run.db_path)
        try:
            self.queue_panel.set_jobs(store.all_jobs(), store.counts())
        finally:
            store.close()
        self.refresh_video()
        self.refresh_thumbnail()

    def _restore_window(self) -> None:
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        state = self.settings.value("windowState")
        if state:
            self.restoreState(state)

    def closeEvent(self, event) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState())
        if self.gen_worker and self.gen_worker.isRunning():
            answer = QMessageBox.question(
                self,
                "A run is in progress",
                "Stop it and close? The queue is saved, so the next start resumes "
                "from here.",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.gen_worker.request_stop()
            self.gen_worker.wait(20000)
        # Anything still running - a voice fetch, a health check, a render -
        # must be joined, not abandoned.
        self._wait_for_threads()
        self.gallery_panel._stop_loader()
        event.accept()


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    app.setApplicationName("Flow Bulk Generator")
    window = MainWindow(load_config())
    window.show()
    return app.exec()
