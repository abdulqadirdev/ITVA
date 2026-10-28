"""Launch the real window, render it, screenshot it, exit.

Proves the GUI constructs and paints — a syntax check cannot. The capture is
wrapped so that any failure still quits: an exception inside a Qt slot is
printed and swallowed, which would otherwise leave the app running forever.

Usage:  python tests/smoke_ui.py [out.png] [tab-index]
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from imgen.config import load_config  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui-smoke.png")
tab = int(sys.argv[2]) if len(sys.argv) > 2 else 0

app = QApplication([])
window = MainWindow(load_config())
window.tabs.setCurrentIndex(tab)
window.append_log("smoke test: window constructed")
window.show()

status = {"ok": False}


def capture() -> None:
    try:
        window.grab().save(str(out))
        print(
            f"captured {out}  ({window.width()}x{window.height()}, "
            f"tab '{window.tabs.tabText(tab)}', "
            f"{window.queue_panel.model.rowCount()} rows, "
            f"{window.project_list.count()} project(s))"
        )
        status["ok"] = True
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        app.quit()


QTimer.singleShot(2500, capture)
app.exec()
sys.exit(0 if status["ok"] else 1)
