"""Command line entry point: doctor / launch / stop / probe / run."""

from __future__ import annotations

import argparse
import sys

from . import chrome, projects
from .config import Config, load_config

OK, BAD, WARN, INFO = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m", "·"


def _line(mark: str, label: str, detail: str = "") -> None:
    print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    failures = 0
    print("\nChrome")

    st = chrome.status(cfg.chrome)
    if st.browser:
        _line(OK, "browser", st.browser)
    else:
        _line(BAD, "browser", "no Chrome, Chromium or Edge found")
        _line(INFO, "", "set chrome.binary in config/settings.toml to its full path")
        failures += 1
    if st.profile_exists:
        _line(OK, "automation profile", str(st.profile_path))
    else:
        _line(WARN, "automation profile", f"not created yet — {st.profile_path}")

    if st.running:
        _line(OK, "devtools endpoint", f"{cfg.chrome.cdp_base}  ({st.version})")
    else:
        _line(BAD, "devtools endpoint", f"nothing listening on {cfg.chrome.cdp_base}")
        _line(INFO, "", "run:  python -m imgen.cli launch")
        failures += 1

    print("\nGoogle Sheets")
    _line(INFO, "mode", cfg.sheets.mode)

    if cfg.sheets.mode == "service_account":
        sa = cfg.sheets.service_account_file
        if not sa.exists():
            _line(WARN, "service account", f"missing — {sa}")
        else:
            try:
                import json

                email = json.loads(sa.read_text()).get("client_email", "?")
                _line(OK, "service account", email)
                _line(INFO, "", "share the sheet with this address (Editor)")
            except (ValueError, OSError) as exc:
                _line(BAD, "service account", f"unreadable: {exc}")
                failures += 1
    else:
        _line(INFO, "write-back", "off — results go to output/manifest.csv")

    if not cfg.sheets.spreadsheet_id:
        _line(WARN, "spreadsheet_id", "not set in config/settings.toml")
    else:
        try:
            from .sources import build_source

            source = build_source(cfg.sheets)
            prompts = source.read_prompts()
            _line(OK, "sheet", f"'{source.title}' — {len(prompts)} prompt(s)")
        except Exception as exc:  # noqa: BLE001 - surface any auth/API failure
            first = str(exc).splitlines()[0]
            _line(BAD, "sheet", f"{type(exc).__name__}: {first}")
            failures += 1

    print("\nNarration")
    from .tts import edge_available, piper_available

    if edge_available():
        _line(OK, "edge-tts", f"online voices, current: {cfg.video.voice}")
    else:
        _line(WARN, "edge-tts", "not installed")
    if piper_available(cfg.video.piper_model):
        _line(OK, "piper", f"offline fallback: {cfg.video.piper_model}")
    else:
        _line(WARN, "piper", "no offline voice set - narration needs a connection")
        _line(INFO, "", "models: huggingface.co/rhasspy/piper-voices")

    from .platforms import find_ffmpeg

    if find_ffmpeg(cfg.video.ffmpeg):
        _line(OK, "ffmpeg", str(find_ffmpeg(cfg.video.ffmpeg)))
    else:
        _line(BAD, "ffmpeg", "not found - video cannot be built")
        failures += 1

    print("\nGoogle Flow")
    if not st.running:
        _line(WARN, "sign-in", "skipped (browser not running)")
    else:
        try:
            from .browser import BrowserSession, SignInState, sign_in_state
            from .flow import open_flow

            with BrowserSession(cfg.chrome) as session:
                page = open_flow(session, cfg)
                state = sign_in_state(page, cfg)
                if state is SignInState.SIGNED_IN:
                    _line(OK, "session", f"signed in — {page.url[:60]}")
                    from .browser import first_match

                    if first_match(page, cfg.selectors.get("prompt_input")):
                        _line(OK, "prompt box", "found")
                    else:
                        _line(WARN, "prompt box", "not found with current selectors")
                        _line(INFO, "", "run:  python -m imgen.cli probe")
                elif state is SignInState.SIGNED_OUT:
                    _line(WARN, "sign-in", "NOT signed in")
                    _line(INFO, "", "sign in by hand in the Chrome window, then re-run doctor")
                else:
                    _line(WARN, "sign-in", f"undetermined (page: {page.url})")
                    _line(INFO, "", "run:  python -m imgen.cli probe")
        except Exception as exc:  # noqa: BLE001
            _line(BAD, "sign-in", f"{type(exc).__name__}: {exc}")
            failures += 1

    print()
    if failures:
        print(f"{BAD} {failures} check(s) failed\n")
    else:
        print(f"{OK} all required checks passed\n")
    return 1 if failures else 0


def cmd_launch(cfg: Config, args: argparse.Namespace) -> int:
    if chrome.is_up(cfg.chrome):
        st = chrome.status(cfg.chrome)
        print(f"{OK} already running — {st.version} on {cfg.chrome.cdp_base}")
        return 0
    print(f"{INFO} launching {cfg.chrome.flatpak_app}")
    print(f"{INFO} profile: {cfg.chrome.host_user_data_dir}")
    try:
        st = chrome.launch(cfg.chrome, url=cfg.flow.app_url)
    except chrome.ChromeError as exc:
        print(f"\n{BAD} {exc}\n", file=sys.stderr)
        return 1
    print(f"{OK} devtools on {cfg.chrome.cdp_base} — {st.version}")
    print(f"{INFO} first run? sign in to Google in that window, then: python -m imgen.cli doctor")
    return 0


def cmd_stop(cfg: Config, args: argparse.Namespace) -> int:
    if chrome.stop(cfg.chrome):
        print(f"{OK} stopped")
        return 0
    print(f"{WARN} no recorded pid; close the window by hand if it is still open")
    return 0


def cmd_probe(cfg: Config, args: argparse.Namespace) -> int:
    """Report what is actually on the live Flow page.

    This is the repair tool for config/selectors.toml: Google ships UI changes
    without notice, and guessing from the outside is hopeless. Sign in first.
    """
    from .browser import BrowserSession, first_match, sign_in_state
    from .flow import open_flow

    if not chrome.is_up(cfg.chrome):
        print(f"{BAD} browser not running — python -m imgen.cli launch", file=sys.stderr)
        return 1

    with BrowserSession(cfg.chrome) as session:
        page = open_flow(session, cfg)
        page.wait_for_timeout(args.settle * 1000)

        print(f"\n  url   : {page.url}")
        print(f"  title : {page.title()}")
        print(f"  state : {sign_in_state(page, cfg).value}")

        if cfg.flow.looks_signed_out(page.url):
            print(
                f"\n{WARN} Signed out — Flow only shows its marketing page.\n"
                "   Sign in by hand in the automation Chrome window, then re-run probe.\n"
            )
            return 1

        print("\nConfigured selectors")
        for group in sorted(cfg.selectors.groups):
            candidates = cfg.selectors.get(group)
            hit = first_match(page, candidates)
            if hit:
                count = page.locator(hit.selector).count()
                _line(OK, f"{group:<18}", f"{hit.selector}  ({count} match)")
            else:
                _line(BAD, f"{group:<18}", f"no match among {len(candidates)} candidates")

        print("\nCandidate text inputs on the page")
        for item in page.eval_on_selector_all(
            "textarea, input[type=text], [contenteditable=true]",
            """els => els.slice(0, 15).map(e => ({
                 tag: e.tagName.toLowerCase(),
                 placeholder: e.getAttribute('placeholder') || '',
                 aria: e.getAttribute('aria-label') || '',
                 id: e.id || '', name: e.getAttribute('name') || '',
                 cls: (e.className || '').toString().slice(0, 60),
                 visible: !!(e.offsetWidth || e.offsetHeight),
               }))""",
        ):
            print(f"   {item}")

        print("\nButtons")
        for item in page.eval_on_selector_all(
            "button, [role=button]",
            """els => els.slice(0, 40).map(e => ({
                 text: (e.innerText || '').trim().slice(0, 32),
                 aria: (e.getAttribute('aria-label') || '').slice(0, 32),
                 visible: !!(e.offsetWidth || e.offsetHeight),
               })).filter(x => x.text || x.aria)""",
        ):
            print(f"   {item}")

        print("\nImages currently on the page")
        for item in page.eval_on_selector_all(
            "img",
            """els => els.slice(0, 25).map(e => ({
                 src: (e.currentSrc || e.src || '').slice(0, 72),
                 alt: (e.alt || '').slice(0, 30),
                 w: e.naturalWidth, h: e.naturalHeight,
               }))""",
        ):
            print(f"   {item}")

        print("\n  Copy the working selectors into config/selectors.toml\n")
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    from pathlib import Path

    from .db import JobStore
    from .runner import Runner

    if args.retry_failed:
        store = JobStore(cfg.run.db_path)
        print(f"{INFO} requeued {store.retry_failed()} job(s)")
        store.close()

    if not args.dry_run and not chrome.is_up(cfg.chrome):
        print(f"{BAD} browser not running — python -m imgen.cli launch", file=sys.stderr)
        return 1

    runner = Runner(cfg, log=print)
    try:
        stats = runner.run(
            limit=args.limit,
            dry_run=args.dry_run,
            prompts_file=Path(args.prompts_file) if args.prompts_file else None,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n{BAD} {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1

    print(f"\n{OK} {stats.as_line()}")
    if stats.stopped_reason:
        print(f"{WARN} stopped: {stats.stopped_reason}")
    print()
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    from .db import JobStore

    store = JobStore(cfg.run.db_path)
    counts = store.counts()
    store.close()
    if not counts:
        print(f"\n{INFO} queue is empty ({cfg.run.db_path})\n")
        return 0
    print()
    for status, n in sorted(counts.items()):
        _line(INFO, f"{status:<10}", str(n))
    print()
    return 0


def cmd_video(cfg: Config, args: argparse.Namespace) -> int:
    from .db import JobStore
    from .produce import Producer

    store = JobStore(cfg.run.db_path)
    try:
        producer = Producer(cfg, log=print)
        rows = producer.rows(store)
        if not rows:
            print(f"\n{WARN} no finished rows with images yet\n")
            return 1

        if args.narrate_only:
            made = producer.narrate(store, rows[: args.limit] if args.limit else rows,
                                    force=args.force)
            print(f"\n{OK} narrated {made} row(s)\n")
            return 0

        result = producer.build(
            store,
            name=args.name,
            narrate_first=not args.no_narrate,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"\n{BAD} {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1
    finally:
        store.close()

    print(f"\n{OK} {result.clips} clip(s), {result.seconds:.0f}s")
    _line(INFO, "video", str(result.video))
    _line(INFO, "subtitles", str(result.srt))
    for scene, path in result.scenes.items():
        _line(INFO, f"scene '{scene}'", str(path))
    print()
    return 0


def cmd_projects(cfg: Config, args: argparse.Namespace) -> int:
    from .db import JobStore

    found = projects.list_projects()
    if not found:
        print(f"\n{INFO} no projects yet — the GUI creates one on first run\n")
        return 0
    print()
    for project in found:
        store = JobStore(project.db_path)
        counts = store.counts()
        store.close()
        summary = "  ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "empty"
        source = project.source
        where = source.get("spreadsheet_id") or source.get("file_path") or "no source"
        _line(INFO, f"{project.slug:<22}", f"{project.name}  —  {summary}")
        _line(" ", f"{'':<22}", f"  {where}")
    print()
    return 0


def resolve_project(base: Config, wanted: str | None):
    """Pick the project a command should operate on."""
    found = projects.list_projects()
    if wanted:
        for project in found:
            if wanted.lower() in (project.slug.lower(), project.name.lower()):
                projects.prepare(project)
                return project
        names = ", ".join(p.slug for p in found) or "none"
        raise SystemExit(f"{BAD} no project {wanted!r}. Available: {names}")
    project = projects.ensure_default(base)
    # Cheap unless something is actually missing: a project moved between
    # machines records paths that do not exist on this one.
    projects.prepare(project)
    return project


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="imgen", description=__doc__)
    parser.add_argument(
        "--project",
        help="project slug or name to act on (default: the first one). "
        "Projects are created in the GUI; see `imgen projects`.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="check every prerequisite").set_defaults(func=cmd_doctor)
    sub.add_parser("launch", help="start the automation Chrome").set_defaults(func=cmd_launch)
    sub.add_parser("stop", help="stop the automation Chrome").set_defaults(func=cmd_stop)

    probe = sub.add_parser("probe", help="dump the live Flow DOM to repair selectors")
    probe.add_argument("--settle", type=float, default=3.0, help="seconds to let the app render")
    probe.set_defaults(func=cmd_probe)

    run = sub.add_parser("run", help="generate images for queued prompts")
    run.add_argument("--limit", type=int, help="cap jobs this run (still bounded by max_per_run)")
    run.add_argument("--dry-run", action="store_true", help="show what would be submitted")
    run.add_argument("--prompts-file", help="read prompts from a local file instead of the sheet")
    run.add_argument("--retry-failed", action="store_true", help="requeue failed/quota jobs first")
    run.set_defaults(func=cmd_run)

    video = sub.add_parser("video", help="narrate the rows and build the video")
    video.add_argument("--name", default="video", help="output file name (no extension)")
    video.add_argument("--limit", type=int, help="only the first N rows, for a quick look")
    video.add_argument("--no-narrate", action="store_true",
                       help="use existing narration only; do not synthesise")
    video.add_argument("--narrate-only", action="store_true",
                       help="synthesise speech and stop, without rendering")
    video.add_argument("--force", action="store_true",
                       help="with --narrate-only, redo rows that already have audio")
    video.set_defaults(func=cmd_video)

    sub.add_parser("status", help="show queue counts").set_defaults(func=cmd_status)
    sub.add_parser("projects", help="list projects and their queues").set_defaults(
        func=cmd_projects
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base = load_config()
    if args.func is cmd_projects:
        return args.func(base, args)
    # Everything else runs inside a project, so the CLI and the GUI share one
    # queue and one output folder rather than quietly keeping two.
    project = resolve_project(base, args.project)
    cfg = project.config(base)
    print(f"{INFO} project: {project.name}")
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
