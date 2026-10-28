"""Daily usage tracking.

Credits are billed against the Google account, not a project, so the counter
lives at app level — otherwise a cap could be spent several times over by
running two projects in the same day.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .platforms import app_data_dir

KEEP_DAYS = 120


def _path() -> Path:
    directory = app_data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "usage.json"


def _load() -> dict[str, int]:
    path = _path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return {k: int(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (ValueError, OSError):
        # A corrupt counter must never block generating; start it over.
        return {}


def _save(data: dict[str, int]) -> None:
    trimmed = dict(sorted(data.items())[-KEEP_DAYS:])
    try:
        _path().write_text(json.dumps(trimmed, indent=2))
    except OSError:
        pass


def record(images: int, when: date | None = None) -> int:
    """Add to today's count and return the new total."""
    if images <= 0:
        return today()
    key = (when or date.today()).isoformat()
    data = _load()
    data[key] = data.get(key, 0) + images
    _save(data)
    return data[key]


def today(when: date | None = None) -> int:
    return _load().get((when or date.today()).isoformat(), 0)


def history(days: int = 14) -> list[tuple[str, int]]:
    return sorted(_load().items())[-days:]


def remaining(cap: int) -> int | None:
    """Images left under a daily cap, or None when no cap is set."""
    return None if cap <= 0 else max(cap - today(), 0)
