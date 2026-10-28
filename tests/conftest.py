"""Shared test setup.

Qt tests run headless: the GUI must be testable on a machine with no display,
including CI.
"""

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Held at module scope so Python's garbage collector cannot destroy the
# QApplication, or widgets still referencing it, in an order Qt dislikes —
# that produces "shared QObject was deleted directly" at interpreter exit.
_APP = None
_WIDGETS: list = []


@pytest.fixture(scope="session")
def qapp():
    global _APP
    from PySide6.QtWidgets import QApplication

    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


@pytest.fixture
def keep_alive():
    """Register a widget so it outlives the test that made it."""

    def _keep(widget):
        _WIDGETS.append(widget)
        return widget

    return _keep
