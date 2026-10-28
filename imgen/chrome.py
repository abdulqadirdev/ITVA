"""Launching and locating the automation Chrome instance.

The design rests on one constraint that holds on every platform: since Chrome
136, ``--remote-debugging-port`` is **refused** when the browser is using its
default user-data-dir. That restriction exists to stop other local processes
from siphoning cookies out of the everyday profile. So the app never touches
the normal profile; it runs a dedicated "automation" profile that the user
signs into once.

Nothing here sets an automation flag, so ``navigator.webdriver`` stays false
and Google's sign-in flow behaves exactly as it does for a hand-driven browser.

Where Chrome lives and where a profile may be written differ per OS; that is
all in :mod:`imgen.platforms`.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import ChromeConfig
from .platforms import ChromeLauncher, detach_kwargs, find_chrome, terminate

PIDFILE_NAME = ".imgen-chrome.pid"


class ChromeError(RuntimeError):
    """Chrome could not be found, launched or reached."""


@dataclass
class ChromeStatus:
    running: bool
    profile_exists: bool
    profile_path: Path
    browser: str | None = None
    version: str | None = None
    websocket_url: str | None = None


def launcher_for(cfg: ChromeConfig) -> ChromeLauncher | None:
    return find_chrome(explicit=cfg.binary, flatpak_app=cfg.flatpak_app)


def _cdp_get(cfg: ChromeConfig, path: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(f"{cfg.cdp_base}{path}", timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def is_up(cfg: ChromeConfig) -> bool:
    return _cdp_get(cfg, "/json/version") is not None


def status(cfg: ChromeConfig) -> ChromeStatus:
    profile = cfg.host_user_data_dir
    found = launcher_for(cfg)
    info = _cdp_get(cfg, "/json/version")
    if info is None:
        return ChromeStatus(
            running=False,
            profile_exists=profile.exists(),
            profile_path=profile,
            browser=found.label if found else None,
        )
    return ChromeStatus(
        running=True,
        profile_exists=profile.exists(),
        profile_path=profile,
        browser=found.label if found else None,
        version=info.get("Browser"),
        websocket_url=info.get("webSocketDebuggerUrl"),
    )


def build_command(cfg: ChromeConfig, url: str | None) -> list[str]:
    found = launcher_for(cfg)
    if found is None:
        raise ChromeError(
            "No Chrome could be found on this machine.\n"
            "Open the Setup tab - it has a button that installs it."
        )
    args = [
        *found.command,
        # Must not be the default profile — see the module docstring.
        f"--user-data-dir={cfg.host_user_data_dir}",
        f"--remote-debugging-port={cfg.debug_port}",
        # Chrome 111+ checks the Origin header on the DevTools websocket.
        f"--remote-allow-origins={cfg.cdp_base}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if cfg.disable_extensions:
        # Signing in makes Chrome Sync pull the everyday profile's extensions
        # into this one, and some inject into flow.google.com.
        args.append("--disable-extensions")
    args.extend(cfg.extra_args)
    if url:
        args.append(url)
    return args


def launch(cfg: ChromeConfig, url: str | None = None, wait: bool = True) -> ChromeStatus:
    """Start the automation browser, or reuse one that is already listening."""
    if is_up(cfg):
        return status(cfg)

    cfg.host_user_data_dir.mkdir(parents=True, exist_ok=True)
    command = build_command(cfg, url)

    log_path = cfg.host_user_data_dir / "launch.log"
    log = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, **detach_kwargs()
        )
    except OSError as exc:
        raise ChromeError(f"could not start Chrome: {exc}\n  {' '.join(command)}") from exc
    (cfg.host_user_data_dir / PIDFILE_NAME).write_text(str(proc.pid))

    if not wait:
        return status(cfg)

    deadline = time.monotonic() + cfg.launch_timeout_s
    while time.monotonic() < deadline:
        if is_up(cfg):
            return status(cfg)
        if proc.poll() is not None:
            # A launcher that hands off to an existing instance exits at once;
            # give the port a moment before calling it a failure.
            time.sleep(2.0)
            if is_up(cfg):
                return status(cfg)
            break
        time.sleep(0.5)

    tail = ""
    if log_path.exists():
        tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-15:])
    raise ChromeError(
        f"Chrome did not expose a DevTools endpoint on {cfg.cdp_base} within "
        f"{cfg.launch_timeout_s}s.\n\nCommand was:\n  {' '.join(command)}\n\n"
        f"Launch log tail ({log_path}):\n{tail or '  (empty)'}\n\n"
        "Most likely causes:\n"
        "  - another Chrome is already using this profile directory\n"
        "  - the profile path is not writable\n"
        "Try running the command above by hand to see Chrome's own error."
    )


def stop(cfg: ChromeConfig) -> bool:
    """Terminate the browser we started, via its recorded PID."""
    pidfile = cfg.host_user_data_dir / PIDFILE_NAME
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
    except ValueError:
        pidfile.unlink(missing_ok=True)
        return False
    ok = terminate(pid)
    pidfile.unlink(missing_ok=True)
    return ok
