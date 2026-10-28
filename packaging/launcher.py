"""Entry point for the frozen Windows build.

A bundled app has no console, so anything that escapes here would vanish
silently. The last-resort handler puts it somewhere the person can read.
"""

from __future__ import annotations

import multiprocessing
import sys
import traceback


def main() -> int:
    # Must come first in a frozen app: without it, any library that starts a
    # process would relaunch the whole executable instead.
    multiprocessing.freeze_support()

    try:
        from ui.main_window import main as run_app

        return run_app()
    except Exception:  # noqa: BLE001 - nothing above this to catch it
        detail = traceback.format_exc()
        try:
            from imgen.platforms import app_data_dir

            target = app_data_dir()
            target.mkdir(parents=True, exist_ok=True)
            crash = target / "startup-error.txt"
            crash.write_text(detail, encoding="utf-8")
            location = str(crash)
        except Exception:  # noqa: BLE001
            location = "(could not be written)"

        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            # A QApplication must exist before any widget, even a message box.
            QApplication.instance() or QApplication([])
            QMessageBox.critical(
                None,
                "Flow Bulk Generator could not start",
                f"{detail.strip().splitlines()[-1]}\n\nFull details saved to:\n{location}",
            )
        except Exception:  # noqa: BLE001 - no GUI available either
            sys.stderr.write(detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
