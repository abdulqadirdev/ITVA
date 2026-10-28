"""How many generations a run is allowed to spend, now that several run at once.

Every bug here costs real credits, so the accounting is pinned down with
fakes standing in for Flow rather than a browser.
"""

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen import runner as runner_mod  # noqa: E402
from imgen.config import load_config  # noqa: E402
from imgen.db import JobStore  # noqa: E402
from imgen.flow import Batch, BrowserGone  # noqa: E402


class FakeFlow:
    """Accepts prompts and reports them finished on the next poll."""

    def __init__(self, die_after: int | None = None):
        self.submitted: list[str] = []
        self.die_after = die_after
        self.max_in_flight = 0
        self._live: set[str] = set()

    # -- worker side --
    def open_app(self):
        return object()

    def submit(self, _page, prompt: str):
        if self.die_after is not None and len(self.submitted) >= self.die_after:
            raise BrowserGone("Target closed")
        self.submitted.append(prompt)
        self._live.add(prompt)
        self.max_in_flight = max(self.max_in_flight, len(self._live))
        return None

    def download_all(self, _page, sources):
        return [b"\xff\xd8fake" for _ in sources]

    # -- reader side --
    def refresh(self, _page, scrolls=1):
        return {}

    def sweep(self, _page, steps=24):
        return {}

    @property
    def seen(self):
        return {}

    def find(self, prompt: str):
        if prompt in self._live:
            self._live.discard(prompt)
            return Batch(prompt, 100, ["https://flow-content.google/image/x"])
        return None


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "BrowserSession", lambda _cfg: _NullContext())
    monkeypatch.setattr(runner_mod, "first_match", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod.usage, "record", lambda *a, **k: 0)
    monkeypatch.setattr(runner_mod.usage, "today", lambda *a, **k: 0)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)

    base = load_config()

    def build(die_after=None, concurrency=1, prompts=50):
        cfg = replace(
            base,
            run=replace(
                base.run,
                output_dir=tmp_path / "out",
                db_path=tmp_path / "state.db",
                max_per_run=25,
                concurrency=concurrency,
                poll_interval_s=0,
                delay_min_s=0,
                delay_max_s=0,
                run_until_empty=False,
            # The fake has no pre-existing batches to claim.
            claim_existing=False,
            ),
        )
        store = JobStore(cfg.run.db_path)
        store.sync_prompts([(i, f"prompt {i}") for i in range(2, 2 + prompts)])
        store.close()

        flow = FakeFlow(die_after)
        monkeypatch.setattr(runner_mod, "FlowWorker", lambda *a, **k: flow)
        monkeypatch.setattr(runner_mod, "BatchReader", lambda *a, **k: flow)
        return runner_mod.Runner(cfg, log=lambda _m: None), flow

    return build


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_an_explicit_limit_is_honoured_above_max_per_run(harness):
    """The GUI passes the whole backlog; clamping it to max_per_run made the
    app promise 147 jobs and quietly run 25."""
    run, flow = harness()
    stats = run.run(limit=40, prompts_file=None)
    assert len(flow.submitted) == 40
    assert stats.done == 40


def test_no_limit_falls_back_to_max_per_run(harness):
    run, flow = harness()
    run.run(prompts_file=None)
    assert len(flow.submitted) == 25


def test_a_zero_limit_falls_back_rather_than_doing_nothing(harness):
    run, flow = harness()
    run.run(limit=0, prompts_file=None)
    assert len(flow.submitted) == 25


def test_concurrency_is_respected_and_never_exceeded(harness):
    run, flow = harness(concurrency=3)
    run.run(limit=12, prompts_file=None)
    assert len(flow.submitted) == 12
    assert flow.max_in_flight <= 3, "more generations in flight than configured"


def test_concurrency_does_not_change_how_much_is_spent(harness):
    """Running three at a time must still cost exactly the budget."""
    run, flow = harness(concurrency=3)
    stats = run.run(limit=7, prompts_file=None)
    assert len(flow.submitted) == 7 and stats.done == 7


def test_recovery_does_not_restart_the_budget(harness):
    """A run of 20 that loses the browser at 8 must still stop at 20 in
    total, not run another full 20 after relaunching."""
    run, flow = harness(die_after=8)
    launched = []
    original = runner_mod.chrome_mod.launch
    runner_mod.chrome_mod.launch = lambda *a, **k: launched.append(1)
    try:
        run.run(limit=20, prompts_file=None)
    finally:
        runner_mod.chrome_mod.launch = original

    assert launched, "the browser should have been relaunched"
    assert run.stats.done <= 20


class StallingFlow(FakeFlow):
    """Accepts a prompt and then reports the same percentage forever."""

    def __init__(self, stuck_at: int = 99):
        super().__init__()
        self.stuck_at = stuck_at

    def find(self, prompt: str):
        if prompt in self._live:
            return Batch(prompt, self.stuck_at, [])
        return None


def test_a_row_stuck_at_the_same_percent_eventually_gives_up(tmp_path, monkeypatch):
    """The reader caches what it has seen, so a batch frozen at 99% kept
    being found. Resetting the clock on every poll meant the row held its
    slot forever and the run never ended."""
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(runner_mod, "BrowserSession", lambda _cfg: _NullContext())
    monkeypatch.setattr(runner_mod, "first_match", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod.usage, "record", lambda *a, **k: 0)
    monkeypatch.setattr(runner_mod.usage, "today", lambda *a, **k: 0)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)

    base = load_config()
    cfg = dc_replace(
        base,
        flow=dc_replace(base.flow, generate_timeout_s=1),
        run=dc_replace(
            base.run,
            output_dir=tmp_path / "out", db_path=tmp_path / "state.db",
            concurrency=1, poll_interval_s=0, delay_min_s=0, delay_max_s=0,
            claim_existing=False,
        ),
    )
    store = JobStore(cfg.run.db_path)
    store.sync_prompts([(2, "prompt 2")])
    store.close()

    flow = StallingFlow()
    monkeypatch.setattr(runner_mod, "FlowWorker", lambda *a, **k: flow)
    monkeypatch.setattr(runner_mod, "BatchReader", lambda *a, **k: flow)

    # Without the fix this never returns. A steadily advancing clock lets
    # the timeout be reached without waiting for it in real time.
    ticks = {"t": 0.0}

    def clock() -> float:
        ticks["t"] += 0.5
        return ticks["t"]

    monkeypatch.setattr(runner_mod.time, "monotonic", clock)

    runner = runner_mod.Runner(cfg, log=lambda _m: None)
    stats = runner.run(limit=1, prompts_file=None)
    assert stats.failed == 1, "the stalled row must be given up on, not held forever"


class SlidingFlow(FakeFlow):
    """Finishes a job, but only shows it to a full sweep.

    This is what Flow actually does: new batches are prepended constantly,
    so a slow job slides out of the small rendered window and the top-of-list
    read never sees that it finished.
    """

    def __init__(self):
        super().__init__()
        self._hidden: set[str] = set()

    def find(self, prompt: str):
        if prompt in self._hidden:
            return Batch(prompt, 100, ["https://flow-content.google/image/x"])
        if prompt in self._live:
            self._live.discard(prompt)
            self._hidden.add(prompt)
            return Batch(prompt, 99, [])     # looks stalled from the top
        return None

    def refresh(self, _page, scrolls=1):
        return {}

    def sweep(self, _page, steps=24):
        return {}


def test_a_job_that_slid_out_of_view_is_found_by_the_sweep(tmp_path, monkeypatch):
    """A finished image reported as "stuck at 99%" cost five minutes and
    then a second generation of something Flow had already made."""
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(runner_mod, "BrowserSession", lambda _cfg: _NullContext())
    monkeypatch.setattr(runner_mod, "first_match", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod.usage, "record", lambda *a, **k: 0)
    monkeypatch.setattr(runner_mod.usage, "today", lambda *a, **k: 0)
    monkeypatch.setattr(runner_mod.time, "sleep", lambda _s: None)

    ticks = {"t": 0.0}

    def clock() -> float:
        ticks["t"] += 5.0
        return ticks["t"]

    monkeypatch.setattr(runner_mod.time, "monotonic", clock)

    base = load_config()
    cfg = dc_replace(
        base,
        flow=dc_replace(base.flow, generate_timeout_s=600),
        run=dc_replace(
            base.run,
            output_dir=tmp_path / "out", db_path=tmp_path / "state.db",
            concurrency=1, poll_interval_s=0, delay_min_s=0, delay_max_s=0,
            claim_existing=False, sweep_after_s=10.0,
        ),
    )
    store = JobStore(cfg.run.db_path)
    store.sync_prompts([(2, "prompt 2")])
    store.close()

    flow = SlidingFlow()
    monkeypatch.setattr(runner_mod, "FlowWorker", lambda *a, **k: flow)
    monkeypatch.setattr(runner_mod, "BatchReader", lambda *a, **k: flow)

    stats = runner_mod.Runner(cfg, log=lambda _m: None).run(limit=1, prompts_file=None)
    assert stats.done == 1, "the sweep should have found the finished image"
    assert stats.failed == 0, "it must not be reported as stuck"
    assert len(flow.submitted) == 1, "and must not be generated a second time"
