"""Packaging a project's results into something shareable."""

from __future__ import annotations

import csv
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .db import Job


def manifest_rows(jobs: list[Job]) -> list[list[str]]:
    rows = [["row", "prompt", "images"]]
    for job in jobs:
        rows.append(
            [str(job.row_number), job.prompt, " ".join(Path(p).name for p in job.image_paths)]
        )
    return rows


def export_zip(jobs: list[Job], destination: Path) -> tuple[int, int]:
    """Write a zip of every generated image plus an index CSV.

    Returns (images, jobs). Missing files are skipped rather than aborting the
    export — a half-deleted output folder should still yield what survives.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    images = 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for job in jobs:
            for path in job.image_paths:
                source = Path(path)
                if source.exists():
                    archive.write(source, f"images/{source.name}")
                    images += 1

        index = "\n".join(
            ",".join(_quote(cell) for cell in row) for row in manifest_rows(jobs)
        )
        archive.writestr("index.csv", index + "\n")
        archive.writestr(
            "README.txt",
            "Generated with Flow Bulk Generator\n"
            f"Exported {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n"
            f"{images} image(s) from {len(jobs)} prompt(s)\n"
            "index.csv maps each source row and prompt to its image files.\n",
        )
    return images, len(jobs)


def export_csv(jobs: list[Job], destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows(manifest_rows(jobs))
    return len(jobs)


def _quote(value: str) -> str:
    if any(c in value for c in ',"\n'):
        return '"' + value.replace('"', '""') + '"'
    return value
