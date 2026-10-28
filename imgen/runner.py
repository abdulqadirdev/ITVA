"""Run orchestration: queue, pacing, retry policy, persistence, write-back."""

from __future__ import annotations

import csv
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .browser import BrowserSession, first_match
from . import chrome as chrome_mod
from . import usage
from .config import Config
from .db import Job, JobStore, Status
from .flow import (
    BatchReader,
    GenerationResult,
    BrowserGone,
    FlowWorker,
    Outcome,
    SignedOutError,
    is_browser_gone,
)
from .naming import NameContext, TemplateError, render
from .sources import PromptSource, build_source

Logger = Callable[[str], None]
#: (job, state, detail) - detail carries live progress like "62%".
JobCallback = Callable[[Job, Status, str], None]
ProgressCallback = Callable[[int, int], None]

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str, limit: int = 48) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:limit] or "prompt"


def _unique(path: Path) -> Path:
    """Never overwrite: a template can legitimately collide (e.g. {scene})."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def image_extension(blob: bytes) -> str:
    """Pick the extension from the bytes, not from a guess.

    Flow returns JPEG, so naming everything .png produced files that no viewer
    would open by double-click.
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if blob[:2] == b"\xff\xd8":
        return "jpg"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "bin"


@dataclass
class RunStats:
    done: int = 0
    blocked: int = 0
    failed: int = 0
    images: int = 0
    stopped_reason: str | None = None

    def as_line(self) -> str:
        return (
            f"{self.done} done · {self.images} images · "
            f"{self.blocked} blocked · {self.failed} failed"
        )


@dataclass
class Runner:
    cfg: Config
    log: Logger = print
    on_job: JobCallback | None = None
    on_progress: ProgressCallback | None = None
    should_stop: Callable[[], bool] = lambda: False
    stats: RunStats = field(default_factory=RunStats)
    _counter: int = 0
    _completed: int = 0
    _log_file = None

    # ---- logging --------------------------------------------------------

    def _open_log(self) -> None:
        """Tee the run's log into the project folder.

        A long unattended run is exactly when the terminal or window that
        showed the log is gone by the time anyone looks at the result."""
        path = self.cfg.run.output_dir.parent / "run.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate rather than grow without bound; one previous log is kept.
        limit = self.cfg.run.log_max_kb * 1024
        if limit and path.exists() and path.stat().st_size > limit:
            backup = path.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            path.rename(backup)
        self._log_file = path.open("a", encoding="utf-8")
        inner = self.log

        def tee(line: str) -> None:
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            try:
                self._log_file.write(f"{stamp} {line.strip()}\n")
                self._log_file.flush()
            except (OSError, ValueError):
                pass
            inner(line)

        self.log = tee
        tee(f"=== run started {datetime.now(timezone.utc).isoformat(timespec='seconds')} ===")

    def _close_log(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    # ---- inputs ---------------------------------------------------------

    def load_prompts(
        self, store: JobStore, prompts_file: Path | None
    ) -> PromptSource | None:
        """Fill the queue from a sheet, or from a local file when testing.

        For a file the "row number" is the line number, which keeps log
        output traceable back to the source either way.
        """
        if prompts_file:
            lines = [
                (n, line.strip())
                for n, line in enumerate(prompts_file.read_text().splitlines(), start=1)
                if line.strip() and not line.startswith("#")
            ]
            added, known = store.sync_prompts(lines)
            self.log(f"  {len(lines)} prompts from {prompts_file.name} ({added} new, {known} known)")
            return None

        sync = build_source(self.cfg.sheets)
        rows = sync.read_prompts()
        added, known = store.sync_prompts(rows)
        self.log(f"  {len(rows)} prompts from '{sync.title}' ({added} new, {known} known)")
        if not sync.writable:
            self.log(
                "  read-only source — results go to output/manifest.csv, not the sheet"
            )
        return sync

    # ---- output ---------------------------------------------------------

    def save_images(self, job: Job, images: Iterable[bytes]) -> list[str]:
        out_dir = self.cfg.run.output_dir
        now = datetime.now(timezone.utc)
        paths: list[str] = []

        for index, blob in enumerate(images, start=1):
            self._counter += 1
            stem = self._render_name(job, index, now)
            path = out_dir / f"{stem}.{image_extension(blob)}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path = _unique(path)
            path.write_bytes(blob)
            paths.append(str(path))

        if paths:
            # Sidecar keeps provenance with the file, so an output folder stays
            # meaningful after it is moved away from this project.
            sidecar = Path(paths[0]).with_suffix(".json")
            sidecar.write_text(
                json.dumps(
                    {
                        "prompt": job.prompt,
                        "sheet_row": job.row_number,
                        "fields": job.fields,
                        "generated_at": now.isoformat(timespec="seconds"),
                        "images": [Path(p).name for p in paths],
                    },
                    indent=2,
                )
            )
        return paths

    def _render_name(self, job: Job, index: int, now) -> str:
        fields = {k.casefold(): v for k, v in (job.fields or {}).items()}
        context = NameContext(
            row=job.row_number,
            seq=fields.get(self.cfg.sheets.sequence_column.casefold(), ""),
            scene=fields.get(self.cfg.sheets.scene_column.casefold(), ""),
            prompt=job.prompt,
            index=index,
            n=self._counter,
            date=now.strftime("%Y-%m-%d"),
            time=now.strftime("%H%M%S"),
            project=self.cfg.run.output_dir.parent.name,
        )
        try:
            return render(self.cfg.run.filename_template, context)
        except TemplateError as exc:
            # A broken template must not cost a generation that already
            # succeeded, so fall back and say so once.
            self.log(f"      naming template ignored: {exc}")
            return render("{row:04}_{prompt}_{index}", context)

    MANIFEST_COLUMNS = ("finished_at", "row", "status", "prompt", "images", "error")

    def append_manifest(
        self, job: Job, status: str, paths: list[str] | None, error: str | None
    ) -> None:
        """Append one line to output/manifest.csv.

        Written for every run, not only read-only ones: it is the record that
        survives the sheet being edited and the SQLite file being deleted.
        """
        path = self.cfg.run.output_dir / "manifest.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if new:
                writer.writerow(self.MANIFEST_COLUMNS)
            writer.writerow(
                [
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    job.row_number,
                    status,
                    job.prompt,
                    " ".join(Path(p).name for p in (paths or [])),
                    error or "",
                ]
            )

    # ---- main loop ------------------------------------------------------

    def run(
        self,
        limit: int | None = None,
        dry_run: bool = False,
        prompts_file: Path | None = None,
    ) -> RunStats:
        store = JobStore(self.cfg.run.db_path)
        self._open_log()
        try:
            recovered = store.reset_stale_running()
            if recovered:
                self.log(f"  recovered {recovered} job(s) interrupted by a previous run")

            # {n} continues across runs rather than restarting each time.
            self._counter = sum(len(j.image_paths) for j in store.done_with_images())
            sync = self.load_prompts(store, prompts_file)
            # max_per_run is the default for a bare `run` with no --limit, not
            # a ceiling on one. Clamping here made the GUI promise 147 jobs,
            # confirm 147, estimate two hours — and then quietly run 25.
            budget = limit if limit and limit > 0 else self.cfg.run.max_per_run
            self.log(f"  budget this run: {budget}")

            if dry_run:
                return self._dry_run(store, budget)

            self._generate(store, sync, budget)

            if sync and sync.writable:
                written = sync.flush(force=True)
                if written:
                    self.log(f"  wrote {written} cells back to the sheet")
            return self.stats
        finally:
            self._close_log()
            store.close()

    def _generate(self, store: JobStore, sync: PromptSource | None, budget: int) -> None:
        """Run the loop, relaunching the browser if it disappears.

        A browser that dies 80 jobs into a 150-job run should not end the run:
        the queue is intact, so the cheapest correct response is to start it
        again and carry on.
        """
        recoveries = 0
        self._completed = 0
        while True:
            remaining = budget - self._completed
            if remaining <= 0:
                return
            try:
                with BrowserSession(self.cfg.chrome) as session:
                    worker = FlowWorker(self.cfg, session, log=self.log)
                    worker.open_app()  # fail fast if signed out
                    # Only what is LEFT of the budget: restarting it here would
                    # let a run that died at 80 of 100 spend 180 generations.
                    self._loop(store, sync, worker, remaining, budget)
                return
            except (BrowserGone, SignedOutError) as exc:
                if isinstance(exc, SignedOutError) or self.should_stop():
                    self.stats.stopped_reason = str(exc)
                    self.log(f"  ! {exc}")
                    return
                recoveries += 1
                if recoveries > 2:
                    self.stats.stopped_reason = "browser kept disappearing"
                    self.log("  ! browser lost again — giving up; the queue is intact")
                    return
                self.log(f"  ! browser lost ({exc}); relaunching and continuing")
                try:
                    chrome_mod.launch(self.cfg.chrome, url=self.cfg.flow.app_url)
                except chrome_mod.ChromeError as relaunch_error:
                    self.stats.stopped_reason = str(relaunch_error)
                    self.log(f"  ! could not relaunch: {relaunch_error}")
                    return

    def _dry_run(self, store: JobStore, budget: int) -> RunStats:
        self.log("  dry run — nothing will be submitted")
        # Read-only on purpose. Claiming jobs and putting them back would churn
        # attempt counts and statuses, which is exactly what a dry run must not do.
        queued = [
            job
            for job in store.all_jobs()
            if job.status in (Status.PENDING, Status.FAILED)
            and job.attempts < self.cfg.run.max_attempts
        ][:budget]
        for n, job in enumerate(queued, start=1):
            self.log(f"  [{n}] row {job.row_number}: {job.prompt[:70]}")
        if not queued:
            self.log("  queue is empty — nothing to do")
        return self.stats

    def _loop(
        self,
        store: JobStore,
        sync: PromptSource | None,
        worker: FlowWorker,
        remaining: int,
        budget: int,
    ) -> None:
        """Keep several generations in flight at once.

        Flow accepts more than one prompt per project and shows each as its
        own batch, carrying the prompt it came from and a live percentage.
        Results are therefore matched by prompt rather than by timing, which
        is what makes running them in parallel safe: nothing depends on
        which finished first.
        """
        page = worker.open_app()
        reader = BatchReader(self.cfg)
        slots = max(1, self.cfg.run.concurrency)

        # Flow keeps every batch it has ever made. Reading them first means a
        # re-run claims work that is already there instead of paying for it
        # again - which also recovers anything a previous run generated but
        # failed to recognise.
        if self.cfg.run.claim_existing:
            self.log("  checking what Flow has already generated")
            reader.sweep(page)
            ready = sum(1 for b in reader.seen.values() if b.finished)
            self.log(f"  {len(reader.seen)} batch(es) found, {ready} with images")
        in_flight: dict[int, dict] = {}
        started = 0
        last_sweep = 0.0

        def status(job: Job, state: Status, detail: str = "") -> None:
            if self.on_job:
                self.on_job(job, state, detail)

        while True:
            # --- fill the free slots ---------------------------------------
            while (
                not self.should_stop()
                and started < remaining
                and len(in_flight) < slots
            ):
                cap = self.cfg.run.daily_image_cap
                if cap and usage.today() >= cap:
                    self.stats.stopped_reason = f"daily cap of {cap} images reached"
                    self.log(f"  ! {self.stats.stopped_reason}")
                    started = remaining
                    break

                job = store.claim_next(self.cfg.run.max_attempts)
                if job is None:
                    started = remaining  # queue is empty; drain what is running
                    break

                existing = reader.find(job.prompt) if self.cfg.run.claim_existing else None
                if existing is not None and existing.finished:
                    images = worker.download_all(page, existing.images)
                    if images:
                        self.log(f"  = row {job.row_number}: already in Flow, not regenerated")
                        final = self._record(
                            store, sync, job, GenerationResult(Outcome.DONE, images=images)
                        )
                        status(job, final, "claimed")
                        started += 1
                        if self.on_progress:
                            self._completed += 1
                            self.on_progress(self._completed, max(budget, self._completed))
                        continue

                self.log(f"  -> row {job.row_number}: {job.prompt[:62]}")
                try:
                    error = worker.submit(page, job.prompt)
                except SignedOutError:
                    store.requeue(job.id, "signed out of Flow")
                    raise
                except Exception as exc:  # noqa: BLE001
                    if is_browser_gone(exc):
                        store.requeue(job.id, "browser disappeared")
                        raise BrowserGone(str(exc)) from exc
                    store.requeue(job.id, f"{type(exc).__name__}: {exc}")
                    self.stats.failed += 1
                    continue

                if error:
                    if error.startswith("blocked"):
                        self._record(store, sync, job, GenerationResult(Outcome.BLOCKED, error=error))
                        status(job, Status.BLOCKED, error)
                    else:
                        store.requeue(job.id, error)
                        self.stats.failed += 1
                        self.log(f"      {error}")
                        status(job, Status.FAILED, error)
                    continue

                in_flight[job.id] = {"job": job, "since": time.monotonic(), "percent": -1}
                started += 1
                status(job, Status.RUNNING, "submitted")

            if not in_flight:
                if self.should_stop():
                    self.stats.stopped_reason = "stopped by user"
                    self.log("  stop requested")
                return
                # starting nothing and running nothing means the queue is done

            # --- watch what is running -------------------------------------
            time.sleep(self.cfg.run.poll_interval_s)

            # Reading only the top of the list is enough while a job is
            # moving, but new batches are prepended constantly, so a slow one
            # slides out of the rendered window and its finished image is
            # never seen - which showed up as "stuck at 99%" on rows that had
            # in fact completed. If nothing has moved for a while, look
            # through the whole list before believing it.
            stale = [
                item for item in in_flight.values()
                if time.monotonic() - item["since"] > self.cfg.run.sweep_after_s
            ]
            deep = bool(stale) and (time.monotonic() - last_sweep) > self.cfg.run.sweep_after_s
            try:
                if deep:
                    reader.sweep(page)
                    # Timed from the end: a sweep of a long project takes
                    # ~20s, so stamping it at the start left almost no gap
                    # and the loop swept back to back while a job stalled.
                    last_sweep = time.monotonic()
                else:
                    reader.refresh(page)
            except Exception as exc:  # noqa: BLE001
                if is_browser_gone(exc):
                    for item in in_flight.values():
                        store.requeue(item["job"].id, "browser disappeared")
                    raise BrowserGone(str(exc)) from exc
                raise

            if first_match(page, self.cfg.selectors.get("quota_message")):
                for item in in_flight.values():
                    store.requeue(item["job"].id, "out of credits")
                self.stats.stopped_reason = "out of credits"
                self.log("  ! out of credits - stopping; the queue is preserved")
                return

            for job_id, item in list(in_flight.items()):
                job = item["job"]
                batch = reader.find(job.prompt)

                if batch is not None and batch.finished:
                    del in_flight[job_id]
                    images = worker.download_all(page, batch.images)
                    result = (
                        GenerationResult(Outcome.DONE, images=images)
                        if images
                        else GenerationResult(
                            Outcome.FAILED, error="images appeared but none could be downloaded"
                        )
                    )
                    final = self._record(store, sync, job, result)
                    status(job, final, "")
                    if self.on_progress:
                        self._completed += 1
                        self.on_progress(self._completed, max(budget, self._completed))
                    if sync and sync.writable:
                        sync.flush()
                    continue

                if batch is not None and batch.percent is not None:
                    if batch.percent != item["percent"]:
                        # Only real movement counts as being alive. The reader
                        # caches what it has seen, so resetting the clock on
                        # every poll let a stalled row hold a slot forever.
                        item["percent"] = batch.percent
                        item["since"] = time.monotonic()
                        status(job, Status.RUNNING, f"{batch.percent}%")
                    elif time.monotonic() - item["since"] > self.cfg.flow.generate_timeout_s:
                        del in_flight[job_id]
                        message = f"stuck at {batch.percent}% for {self.cfg.flow.generate_timeout_s}s"
                        store.requeue(job.id, message)
                        self.stats.failed += 1
                        self.log(f"      row {job.row_number}: {message}")
                        status(job, Status.FAILED, message)
                    continue

                if time.monotonic() - item["since"] > self.cfg.flow.generate_timeout_s:
                    del in_flight[job_id]
                    message = f"no result within {self.cfg.flow.generate_timeout_s}s"
                    store.requeue(job.id, message)
                    self.stats.failed += 1
                    self.log(f"      row {job.row_number}: {message}")
                    status(job, Status.FAILED, message)

    def _record(self, store: JobStore, sync: PromptSource | None, job: Job, result) -> Status:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if result.outcome is Outcome.DONE:
            paths = self.save_images(job, result.images)
            store.finish(job.id, Status.DONE, image_paths=paths)
            self.stats.done += 1
            self.stats.images += len(paths)
            usage.record(len(paths))
            self.log(f"      {len(paths)} image(s) saved")
            self.append_manifest(job, "done", paths, None)
            if sync and sync.writable:
                sync.queue_result(job.row_number, "done", paths=paths, generated_at=now)
            return Status.DONE

        if result.outcome is Outcome.BLOCKED:
            # Terminal on purpose: retrying a policy refusal only burns credits.
            store.finish(job.id, Status.BLOCKED, error=result.error)
            self.stats.blocked += 1
            self.log(f"      blocked: {result.error}")
            self.append_manifest(job, "blocked", None, result.error)
            if sync and sync.writable:
                sync.queue_result(job.row_number, "blocked", error=result.error, generated_at=now)
            return Status.BLOCKED

        if result.outcome is Outcome.QUOTA:
            # Requeued, not failed: this job never got a fair attempt.
            store.requeue(job.id, result.error or "quota")
            if sync and sync.writable:
                sync.queue_result(job.row_number, "quota", error=result.error, generated_at=now)
            return Status.QUOTA

        store.requeue(job.id, result.error or "unknown failure")
        self.stats.failed += 1
        self.log(f"      failed: {result.error}")
        self.append_manifest(job, "failed", None, result.error)
        if sync and sync.writable:
            sync.queue_result(job.row_number, "failed", error=result.error, generated_at=now)
        return Status.FAILED

    def _pause(self) -> None:
        delay = random.uniform(self.cfg.run.delay_min_s, self.cfg.run.delay_max_s)
        end = time.monotonic() + delay
        # Sleep in slices so a Stop click is not swallowed by a long pause.
        while time.monotonic() < end:
            if self.should_stop():
                return
            time.sleep(0.2)
