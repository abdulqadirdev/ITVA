"""Background threads for work that must never block the Qt event loop."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from imgen import chrome
from imgen.config import Config
from imgen.runner import Runner


class LaunchWorker(QThread):
    """Starts the automation Chrome. Separate from generation because the
    first launch waits on a human to sign in."""

    log = Signal(str)
    done = Signal(bool, str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        try:
            if chrome.is_up(self.cfg.chrome):
                self.done.emit(True, "already running")
                return
            self.log.emit(f"launching {self.cfg.chrome.flatpak_app} …")
            status = chrome.launch(self.cfg.chrome, url=self.cfg.flow.app_url)
            self.done.emit(True, status.version or "running")
        except Exception as exc:  # noqa: BLE001
            self.done.emit(False, f"{type(exc).__name__}: {exc}")


class HealthWorker(QThread):
    """Checks that Flow is actually usable before a run is started.

    Worth its own thread: it attaches over CDP and loads a page, which takes
    seconds, and the answer decides whether a long run is worth beginning.
    """

    result = Signal(str, str)   # state, detail

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        if not chrome.is_up(self.cfg.chrome):
            self.result.emit("no-chrome", "automation browser is not running")
            return
        try:
            from imgen.browser import BrowserSession, SignInState, first_match, sign_in_state
            from imgen.flow import open_flow

            with BrowserSession(self.cfg.chrome) as session:
                page = open_flow(session, self.cfg)
                state = sign_in_state(page, self.cfg)
                if state is SignInState.SIGNED_OUT:
                    self.result.emit("signed-out", "sign in to Google in the Chrome window")
                elif first_match(page, self.cfg.selectors.get("prompt_input")):
                    self.result.emit("ready", page.url)
                else:
                    self.result.emit(
                        "no-prompt-box",
                        "signed in, but the prompt box was not found — "
                        "run `imgen probe` and update config/selectors.toml",
                    )
        except Exception as exc:  # noqa: BLE001
            self.result.emit("error", f"{type(exc).__name__}: {exc}")


class GenerationWorker(QThread):
    """Runs the generation loop.

    Playwright's sync API is bound to the thread that starts it, so the whole
    Runner — browser session included — is constructed inside run(), never on
    the Qt main thread.
    """

    log = Signal(str)
    progress = Signal()
    counts = Signal(int, int)             # jobs finished, jobs planned
    job_state = Signal(int, str, str)     # job id, state, live detail e.g. "62%"
    finished_run = Signal(str, str)       # summary, stopped reason

    def __init__(self, cfg: Config, limit: int | None, prompts_file: Path | None = None):
        super().__init__()
        self.cfg = cfg
        self.limit = limit
        self.prompts_file = prompts_file
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        runner = Runner(
            self.cfg,
            log=lambda line: self.log.emit(line.rstrip()),
            on_job=lambda job, state, detail='': self.job_state.emit(job.id, state.value, detail),
            on_progress=lambda done, total: self.counts.emit(done, total),
            should_stop=self._stop.is_set,
        )
        try:
            stats = runner.run(limit=self.limit, prompts_file=self.prompts_file)
            self.finished_run.emit(stats.as_line(), stats.stopped_reason or "")
        except Exception as exc:  # noqa: BLE001
            self.finished_run.emit("", f"{type(exc).__name__}: {exc}")
        finally:
            self.progress.emit()


class ProbeWorker(QThread):
    """Reads the live Flow page so stale selectors can be repaired.

    Google changes Flow's UI without notice, and when it does the app's only
    symptom is "prompt box not found". This is the repair path, in the GUI
    rather than only in a terminal.
    """

    finished_probe = Signal(str, dict)   # report, detected selectors

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        if not chrome.is_up(self.cfg.chrome):
            self.finished_probe.emit("The automation browser is not running.", {})
            return
        try:
            from imgen.browser import BrowserSession, first_match
            from imgen.flow import open_flow

            lines: list[str] = []
            detected: dict[str, str] = {}
            with BrowserSession(self.cfg.chrome) as session:
                page = open_flow(session, self.cfg)
                page.wait_for_timeout(2500)
                lines.append(f"URL   : {page.url}")
                lines.append(f"Title : {page.title()}\n")

                if self.cfg.flow.looks_signed_out(page.url):
                    lines.append("Signed out — sign in first, then probe again.")
                    self.finished_probe.emit("\n".join(lines), {})
                    return

                lines.append("Configured selectors")
                for group in sorted(self.cfg.selectors.groups):
                    hit = first_match(page, self.cfg.selectors.get(group))
                    if hit:
                        lines.append(f"  OK    {group:<20} {hit.selector}")
                        detected[group] = hit.selector
                    else:
                        lines.append(f"  MISS  {group:<20} no candidate matched")

                lines.append("\nText inputs on the page")
                for item in page.eval_on_selector_all(
                    "textarea, input[type=text], [contenteditable=true]",
                    """els => els.slice(0, 12).map(e => ({
                         tag: e.tagName.toLowerCase(),
                         cls: (e.className || '').toString().slice(0, 50),
                         placeholder: e.getAttribute('placeholder') || '',
                         aria: e.getAttribute('aria-label') || '',
                         visible: !!(e.offsetWidth || e.offsetHeight)}))""",
                ):
                    lines.append(f"  {item}")

                lines.append("\nButtons")
                for item in page.eval_on_selector_all(
                    "button, [role=button]",
                    """els => els.slice(0, 30).map(e => ({
                         text: (e.innerText || '').trim().slice(0, 28),
                         aria: (e.getAttribute('aria-label') || '').slice(0, 34)}))
                         .filter(x => x.text || x.aria)""",
                ):
                    lines.append(f"  {item}")

                lines.append("\nImages")
                for item in page.eval_on_selector_all(
                    "img",
                    """els => els.slice(0, 20).map(e => ({
                         src: (e.currentSrc || e.src || '').slice(0, 64),
                         w: e.naturalWidth, h: e.naturalHeight}))""",
                ):
                    lines.append(f"  {item}")

            self.finished_probe.emit("\n".join(lines), detected)
        except Exception as exc:  # noqa: BLE001
            self.finished_probe.emit(f"{type(exc).__name__}: {exc}", {})


class VoiceListWorker(QThread):
    """Fetches the voice list without freezing the window."""

    loaded = Signal(list)

    def run(self) -> None:
        from imgen.tts import list_edge_voices

        try:
            self.loaded.emit(list_edge_voices())
        except Exception:  # noqa: BLE001 - offline is not an error
            self.loaded.emit([])


class ProduceWorker(QThread):
    """Narrates the rows and renders the video.

    Separate from GenerationWorker because it touches no browser at all: it
    is ffmpeg and a speech service, and should keep working while Flow is
    signed out or unavailable.
    """

    log = Signal(str)
    counts = Signal(int, int)
    finished_run = Signal(str, str)   # summary, error

    def __init__(self, cfg: Config, name: str, limit: int | None = None,
                 narrate: bool = True):
        super().__init__()
        self.cfg = cfg
        self.name = name
        self.limit = limit
        self.narrate = narrate
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from imgen.db import JobStore
        from imgen.produce import Producer

        store = JobStore(self.cfg.run.db_path)
        try:
            producer = Producer(
                self.cfg,
                log=lambda line: self.log.emit(line.rstrip()),
                on_progress=lambda done, total: self.counts.emit(done, total),
                should_stop=self._stop.is_set,
            )
            result = producer.build(
                store, name=self.name, narrate_first=self.narrate, limit=self.limit
            )
            extra = f", {len(result.scenes)} scene file(s)" if result.scenes else ""
            self.finished_run.emit(
                f"{result.clips} clips, {result.seconds:.0f}s{extra} -> {result.video.name}",
                "",
            )
        except Exception as exc:  # noqa: BLE001
            self.finished_run.emit("", f"{type(exc).__name__}: {exc}")
        finally:
            store.close()


class NarrateWorker(QThread):
    """Synthesis only, so a voice can be reworked without a re-render."""

    log = Signal(str)
    counts = Signal(int, int)
    finished_run = Signal(str, str)

    def __init__(self, cfg: Config, job_ids: list | None = None, force: bool = False):
        super().__init__()
        self.cfg = cfg
        self.job_ids = job_ids
        self.force = force
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from imgen.db import JobStore
        from imgen.produce import Producer

        store = JobStore(self.cfg.run.db_path)
        try:
            producer = Producer(
                self.cfg,
                log=lambda line: self.log.emit(line.rstrip()),
                on_progress=lambda done, total: self.counts.emit(done, total),
                should_stop=self._stop.is_set,
            )
            rows = producer.rows(store)
            if self.job_ids:
                wanted = set(self.job_ids)
                rows = [r for r in rows if r.id in wanted]
            made = producer.narrate(store, rows, force=self.force)
            self.finished_run.emit(f"{made} clip(s) narrated", "")
        except Exception as exc:  # noqa: BLE001
            self.finished_run.emit("", f"{type(exc).__name__}: {exc}")
        finally:
            store.close()


class ThumbnailWorker(QThread):
    """Composes the thumbnail in the browser, off the GUI thread.

    It attaches over CDP and screenshots a page, which is seconds of work;
    doing it inline would freeze the window, and a QThread that outlives
    the window aborts the process, so the window tracks this like every
    other worker.
    """

    done = Signal(str)      # path to the finished file
    failed = Signal(str)
    note = Signal(str)      # e.g. a block that had to be shrunk to fit

    def __init__(self, cfg: Config, picture, destination):
        super().__init__()
        self.cfg = cfg
        self.picture = picture
        self.destination = destination

    def run(self) -> None:
        try:
            from imgen.browser import BrowserSession
            from imgen.thumbnail import render

            with BrowserSession(self.cfg.chrome) as session:
                made = render(
                    session, self.picture, self.cfg.thumbnail, self.destination,
                    log=self.note.emit,
                )
            self.done.emit(str(made.path))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class UpdateCheckWorker(QThread):
    """Asks GitHub whether a newer release exists.

    On a thread because it is a network call on the path that opens the
    window: done inline, a slow or unreachable GitHub would freeze the
    app before it had drawn anything.
    """

    found = Signal(object)    # a Release, or None when already current
    failed = Signal(str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def run(self) -> None:
        try:
            from imgen.update import check

            self.found.emit(check(self.cfg.update.repo, self.cfg.update.token))
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class UpdateInstallWorker(QThread):
    """Downloads the release and hands it to pkexec apt-get."""

    progress = Signal(float)   # 0..1 while downloading
    note = Signal(str)
    done = Signal()
    failed = Signal(str)

    def __init__(self, cfg: Config, release):
        super().__init__()
        self.cfg = cfg
        self.release = release

    def run(self) -> None:
        try:
            from imgen.update import download, install

            self.note.emit(f"downloading {self.release.name} ...")
            package = download(
                self.release,
                token=self.cfg.update.token,
                on_progress=self.progress.emit,
            )
            install(package, log=self.note.emit)
            self.done.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))


class FixWorker(QThread):
    """Runs one readiness fix - a flatpak or apt command - off the GUI.

    pkexec shows the desktop's own password dialog and can sit there for
    a minute; a flatpak install of Chrome is a few hundred megabytes.
    Neither belongs on the thread that draws the window.
    """

    done = Signal(bool, str)   # succeeded, message

    def __init__(self, check):
        super().__init__()
        self.check = check

    def run(self) -> None:
        import subprocess

        try:
            result = subprocess.run(
                self.check.fix_command,
                capture_output=True, text=True, timeout=1800,
            )
        except FileNotFoundError:
            self.done.emit(False, f"{self.check.fix_command[0]} is not installed")
            return
        except subprocess.TimeoutExpired:
            self.done.emit(False, "it took too long and was stopped")
            return
        except OSError as exc:
            self.done.emit(False, str(exc))
            return

        if result.returncode == 126:
            self.done.emit(False, "the password prompt was dismissed")
            return
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()
            self.done.emit(False, tail[-1] if tail else "it did not work")
            return
        self.done.emit(True, "done")
