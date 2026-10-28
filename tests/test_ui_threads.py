"""Background threads must never outlive the window.

Qt aborts the whole process with "QThread: Destroyed while thread is still
running" if a QThread object is collected or the app exits while it runs.
That is a core dump, not an exception, so these run the real window in a
subprocess and check that it came back cleanly.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PREAMBLE = textwrap.dedent(
    f"""
    import os, sys
    sys.path.insert(0, {str(ROOT)!r})
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from imgen.config import load_config
    from ui.main_window import MainWindow
    app = QApplication([])
    window = MainWindow(load_config())
    window.show()
    """
)


def run_scenario(body: str, timeout: int = 90) -> subprocess.CompletedProcess:
    script = PREAMBLE + textwrap.dedent(body) + "\napp.exec()\nprint('CLEAN')\n"
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=timeout, cwd=ROOT,
    )


def assert_clean(result: subprocess.CompletedProcess) -> None:
    combined = result.stdout + result.stderr
    assert "Destroyed while thread" not in combined, combined[-500:]
    assert result.returncode == 0, f"exit {result.returncode}\n{combined[-500:]}"
    assert "CLEAN" in result.stdout


@pytest.mark.slow
def test_starting_the_same_check_twice_does_not_abort():
    """A second start used to replace the first worker, collecting a running
    QThread - which aborts the process."""
    assert_clean(run_scenario("""
        def go():
            window.check_health()
            window.check_health()
        QTimer.singleShot(400, go)
        QTimer.singleShot(14000, app.quit)
    """))


@pytest.mark.slow
def test_closing_while_a_worker_runs_does_not_abort():
    assert_clean(run_scenario("""
        def go():
            window.check_health()
            QTimer.singleShot(200, window.close)
            QTimer.singleShot(600, app.quit)
        QTimer.singleShot(400, go)
    """))


@pytest.mark.slow
def test_spamming_every_button_then_closing_does_not_abort():
    assert_clean(run_scenario("""
        def go():
            for _ in range(4):
                window.check_health()
            for _ in range(3):
                window.repair_selectors()
            QTimer.singleShot(300, window.close)
            QTimer.singleShot(800, app.quit)
        QTimer.singleShot(500, go)
    """))


@pytest.mark.slow
def test_the_voice_dialog_joins_its_own_threads():
    """It loads 322 voices and can speak a preview, both off the UI thread."""
    assert_clean(run_scenario("""
        from ui.voice_dialog import VoiceDialog
        dialog = VoiceDialog("en-US-AndrewNeural", 0, window)
        dialog.show()
        QTimer.singleShot(600, dialog.close)
        QTimer.singleShot(1200, app.quit)
    """))


def test_the_window_tracks_its_threads():
    """A cheap check that the mechanism exists at all, so the slow tests
    above are not the only thing standing between us and a core dump."""
    source = (ROOT / "ui" / "main_window.py").read_text()
    assert "def track(" in source and "_wait_for_threads" in source
    assert "self.track(" in source
    # Every worker the window starts must be registered.
    for worker in ("LaunchWorker(", "HealthWorker(", "ProbeWorker(", "GenerationWorker("):
        start = source.index(worker)
        assert "self.track(" in source[start : start + 200], f"{worker} is not tracked"
