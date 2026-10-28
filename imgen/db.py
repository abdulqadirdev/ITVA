"""SQLite job queue.

Keeping state on disk rather than in memory is what makes a run resumable: the
browser can crash, Flow can log us out, the machine can sleep, and the next
start picks up exactly where it stopped without re-generating (and re-paying
for) work that already succeeded.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"   # policy refusal — never retried
    QUOTA = "quota"       # out of credits — stops the run
    FAILED = "failed"     # transient — retried with backoff
    SKIPPED = "skipped"


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".opus"}

# Statuses that a fresh run should leave alone.
TERMINAL = {Status.DONE, Status.BLOCKED, Status.SKIPPED}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY,
    row_number   INTEGER NOT NULL,
    prompt_hash  TEXT    NOT NULL UNIQUE,
    prompt       TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    image_paths  TEXT,
    fields       TEXT,
    audio_path   TEXT,
    duration_s   REAL,
    word_times   TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: int
    row_number: int
    prompt: str
    status: Status
    attempts: int
    error: str | None
    image_paths: list[str]
    fields: dict = field(default_factory=dict)
    audio_path: str | None = None
    duration_s: float | None = None
    #: [[start, duration, word], ...] - exact, from the speech engine.
    word_times: list = field(default_factory=list)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        keys = row.keys()
        return cls(
            id=row["id"],
            row_number=row["row_number"],
            prompt=row["prompt"],
            status=Status(row["status"]),
            attempts=row["attempts"],
            error=row["error"],
            image_paths=json.loads(row["image_paths"]) if row["image_paths"] else [],
            # `fields` was added after the first databases were written.
            fields=json.loads(row["fields"]) if "fields" in keys and row["fields"] else {},
            audio_path=row["audio_path"] if "audio_path" in keys else None,
            duration_s=row["duration_s"] if "duration_s" in keys else None,
            word_times=(
                json.loads(row["word_times"])
                if "word_times" in keys and row["word_times"]
                else []
            ),
        )


class JobStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        Existing queues carry real work — what has already been generated and
        paid for — so they are upgraded in place rather than recreated.
        """
        present = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        for column, ddl in (("fields", "TEXT"), ("audio_path", "TEXT"), ("duration_s", "REAL"),
            ("word_times", "TEXT")):
            if column not in present:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    def sync_prompts(self, rows: list) -> tuple[int, int]:
        """Insert prompts from the sheet; return (added, already_known).

        Deduplicated on prompt text, so re-running after the sheet is reordered
        or appended to never regenerates an image that already exists. An
        existing job keeps its status; only its row number is refreshed.
        """
        added = known = 0
        now = _now()
        for entry in rows:
            # Accepts (row_number, prompt) or a PromptRow carrying its columns.
            if isinstance(entry, tuple):
                row_number, prompt, fields = entry[0], entry[1], {}
            else:
                row_number, prompt, fields = entry.row_number, entry.prompt, entry.fields
            text = prompt.strip()
            if not text:
                continue
            digest = prompt_hash(text)
            # rowcount/lastrowid cannot distinguish the INSERT path from the
            # DO UPDATE path, so ask outright.
            exists = self.conn.execute(
                "SELECT 1 FROM jobs WHERE prompt_hash = ?", (digest,)
            ).fetchone()
            # Merge rather than replace. The source wins for every column it
            # carries, but a key held only locally survives - otherwise a
            # routine "Load" would wipe values set in the app, such as an
            # animation chosen per row when the sheet has no such column.
            merged = fields
            if exists:
                previous = self.conn.execute(
                    "SELECT fields FROM jobs WHERE prompt_hash = ?", (digest,)
                ).fetchone()
                held = json.loads(previous["fields"]) if previous["fields"] else {}
                merged = {**held, **fields}
            encoded = json.dumps(merged) if merged else None
            self.conn.execute(
                """
                INSERT INTO jobs (row_number, prompt_hash, prompt, status, fields,
                                  created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?)
                ON CONFLICT(prompt_hash) DO UPDATE SET
                    row_number = excluded.row_number,
                    fields = COALESCE(excluded.fields, jobs.fields)
                """,
                (row_number, digest, text, encoded, now, now),
            )
            if exists:
                known += 1
            else:
                added += 1
        self.conn.commit()
        return added, known

    def set_field(self, job_id: int, key: str, value: str) -> None:
        """Set one column on a row, in the app rather than in the source.

        Used for values the sheet does not carry - an animation chosen per
        row, say. An empty value removes the key, so clearing it in the app
        is the same as never having set it.
        """
        row = self.conn.execute(
            "SELECT fields FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return
        fields = json.loads(row["fields"]) if row["fields"] else {}
        folded = key.casefold()
        for existing in [k for k in fields if k.casefold() == folded]:
            del fields[existing]
        if value:
            fields[key] = value
        self.conn.execute(
            "UPDATE jobs SET fields = ?, updated_at = ? WHERE id = ?",
            (json.dumps(fields) if fields else None, _now(), job_id),
        )
        self.conn.commit()

    def reset_stale_running(self) -> int:
        """Return jobs abandoned mid-flight by a crash to the queue."""
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'pending', updated_at = ? WHERE status = 'running'",
            (_now(),),
        )
        self.conn.commit()
        return cur.rowcount

    def claim_next(self, max_attempts: int) -> Job | None:
        row = self.conn.execute(
            """
            SELECT * FROM jobs
            WHERE (status = 'pending' OR (status = 'failed' AND attempts < ?))
            ORDER BY row_number LIMIT 1
            """,
            (max_attempts,),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        self.conn.commit()
        job = Job.from_row(row)
        job.attempts += 1
        job.status = Status.RUNNING
        return job

    def finish(
        self,
        job_id: int,
        status: Status,
        image_paths: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, image_paths = ?, error = ?, updated_at = ? WHERE id = ?",
            (
                status.value,
                json.dumps(image_paths) if image_paths else None,
                error,
                _now(),
                job_id,
            ),
        )
        self.conn.commit()

    def requeue(self, job_id: int, error: str) -> None:
        """A transient failure: keep the attempt count, allow another try."""
        self.conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
            (error, _now(), job_id),
        )
        self.conn.commit()

    def retry_failed(self) -> int:
        cur = self.conn.execute(
            "UPDATE jobs SET status = 'pending', attempts = 0, error = NULL, updated_at = ? "
            "WHERE status IN ('failed', 'quota')",
            (_now(),),
        )
        self.conn.commit()
        return cur.rowcount

    def set_status(self, job_id: int, status: Status, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status.value, error, _now(), job_id),
        )
        self.conn.commit()

    def requeue_ids(self, job_ids: list[int]) -> int:
        """Force specific jobs back into the queue, attempts reset.

        Used by "Regenerate": a job that already succeeded is terminal, so it
        needs an explicit reset rather than the retry path, which only picks
        up failures.
        """
        if not job_ids:
            return 0
        marks = ",".join("?" * len(job_ids))
        cur = self.conn.execute(
            f"UPDATE jobs SET status = 'pending', attempts = 0, error = NULL, "
            f"image_paths = NULL, updated_at = ? WHERE id IN ({marks})",
            [_now(), *job_ids],
        )
        self.conn.commit()
        return cur.rowcount

    def skip_ids(self, job_ids: list[int]) -> int:
        if not job_ids:
            return 0
        marks = ",".join("?" * len(job_ids))
        cur = self.conn.execute(
            f"UPDATE jobs SET status = 'skipped', updated_at = ? WHERE id IN ({marks})",
            [_now(), *job_ids],
        )
        self.conn.commit()
        return cur.rowcount

    def pending_count(self, max_attempts: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) n FROM jobs WHERE status = 'pending' "
            "OR (status = 'failed' AND attempts < ?)",
            (max_attempts,),
        ).fetchone()
        return row["n"]

    def done_with_images(self) -> list[Job]:
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE image_paths IS NOT NULL ORDER BY row_number"
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def update_prompt(self, job_id: int, text: str) -> bool:
        """Change a job's prompt and requeue it.

        The prompt is the dedupe key, so its hash moves with it. If the new
        text already exists as another job, the edit is refused rather than
        silently merging two rows.
        """
        text = text.strip()
        if not text:
            return False
        digest = prompt_hash(text)
        clash = self.conn.execute(
            "SELECT id FROM jobs WHERE prompt_hash = ? AND id != ?", (digest, job_id)
        ).fetchone()
        if clash:
            return False
        self.conn.execute(
            "UPDATE jobs SET prompt = ?, prompt_hash = ?, status = 'pending', "
            "attempts = 0, error = NULL, image_paths = NULL, updated_at = ? WHERE id = ?",
            (text, digest, _now(), job_id),
        )
        self.conn.commit()
        return True

    def set_images(self, job_id: int, paths: list[str]) -> None:
        """Replace a job's recorded images, e.g. after discarding variants."""
        self.conn.execute(
            "UPDATE jobs SET image_paths = ?, updated_at = ? WHERE id = ?",
            (json.dumps(paths) if paths else None, _now(), job_id),
        )
        self.conn.commit()

    def set_audio(
        self,
        job_id: int,
        path: str | None,
        duration: float | None,
        words: list | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE jobs SET audio_path = ?, duration_s = ?, word_times = ?, "
            "updated_at = ? WHERE id = ?",
            (path, duration, json.dumps(words) if words else None, _now(), job_id),
        )
        self.conn.commit()

    def clear_audio(self, job_ids: list[int]) -> int:
        if not job_ids:
            return 0
        marks = ",".join("?" * len(job_ids))
        cur = self.conn.execute(
            f"UPDATE jobs SET audio_path = NULL, duration_s = NULL, "
            f"word_times = NULL, updated_at = ? "
            f"WHERE id IN ({marks})",
            [_now(), *job_ids],
        )
        self.conn.commit()
        return cur.rowcount

    def repair_audio_paths(self, output_dir: Path) -> tuple[int, int]:
        """Re-point narration at where the files actually are.

        Same failure as the images: paths are stored absolute, so moving the
        output folder - or a run that wrote somewhere else - leaves rows
        pointing at nothing. Audio that cannot be found is cleared rather
        than left dangling, so the row is simply narrated again instead of
        being silently treated as done.

        Returns (relocated, cleared).
        """
        by_name: dict[str, Path] = {}
        if output_dir.exists():
            for candidate in output_dir.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() in AUDIO_SUFFIXES:
                    by_name.setdefault(candidate.name, candidate)

        relocated = cleared = 0
        rows = self.conn.execute(
            "SELECT id, audio_path FROM jobs WHERE audio_path IS NOT NULL"
        ).fetchall()
        for row in rows:
            path = Path(row["audio_path"])
            if path.exists():
                continue
            replacement = by_name.get(path.name)
            if replacement:
                self.conn.execute(
                    "UPDATE jobs SET audio_path = ?, updated_at = ? WHERE id = ?",
                    (str(replacement), _now(), row["id"]),
                )
                relocated += 1
            else:
                self.conn.execute(
                    "UPDATE jobs SET audio_path = NULL, duration_s = NULL, "
                    "word_times = NULL, updated_at = ? WHERE id = ?",
                    (_now(), row["id"]),
                )
                cleared += 1
        self.conn.commit()
        return (relocated, cleared)

    def paths_are_broken(self) -> bool:
        """Is any recorded image or audio file missing from disk?

        A cheap check so the repair can run whenever a project is opened
        without scanning the output folder every time.
        """
        for job in self.done_with_images():
            if any(not Path(p).exists() for p in job.image_paths):
                return True
            if job.audio_path and not Path(job.audio_path).exists():
                return True
        return False

    def repair_image_paths(self, output_dir: Path) -> tuple[int, int]:
        """Re-point recorded images at where the files actually are.

        Paths are stored absolute, so moving a project's output folder — which
        the first-run migration does — leaves every record pointing at the old
        location. The gallery and the export both skip missing files silently,
        so this stayed invisible until the video builder needed them.

        Returns (jobs repaired, paths that could not be found).
        """
        if not output_dir.exists():
            return (0, 0)

        by_name: dict[str, Path] = {}
        for candidate in output_dir.rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in IMAGE_SUFFIXES:
                by_name.setdefault(candidate.name, candidate)

        repaired = lost = 0
        for job in self.done_with_images():
            updated, changed = [], False
            for recorded in job.image_paths:
                path = Path(recorded)
                if path.exists():
                    updated.append(recorded)
                    continue
                replacement = by_name.get(path.name)
                if replacement:
                    updated.append(str(replacement))
                    changed = True
                else:
                    lost += 1
            if changed:
                self.set_images(job.id, updated)
                repaired += 1
        return (repaired, lost)

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def all_jobs(self) -> list[Job]:
        rows = self.conn.execute("SELECT * FROM jobs ORDER BY row_number").fetchall()
        return [Job.from_row(r) for r in rows]
