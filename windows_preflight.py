"""Pre-flight check for Windows — run this BEFORE installing.

Plain standard library only, so it works on a bare Python install with
nothing else set up. It answers the questions that actually decide whether
the install will succeed.

    py windows_preflight.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

OK, BAD, WARN = "[ ok ]", "[FAIL]", "[warn]"
problems = 0


def report(mark: str, label: str, detail: str = "") -> None:
    global problems
    if mark == BAD:
        problems += 1
    print(f"  {mark} {label}" + (f"  {detail}" if detail else ""))


print("\nFlow Bulk Generator — Windows pre-flight\n")

# ---- Python -------------------------------------------------------------

version = ".".join(str(n) for n in sys.version_info[:3])
if sys.version_info >= (3, 10):
    report(OK, "Python", f"{version} at {sys.executable}")
else:
    report(BAD, "Python", f"{version} — 3.10 or newer is required")

try:
    import venv  # noqa: F401

    report(OK, "venv module", "available")
except ImportError:
    report(BAD, "venv module", "missing — reinstall Python with the standard library")

if sys.maxsize <= 2**32:
    report(WARN, "architecture", "32-bit Python — PySide6 needs 64-bit")
else:
    report(OK, "architecture", "64-bit")

# ---- platform -----------------------------------------------------------

ON_WINDOWS = sys.platform.startswith("win")
if not ON_WINDOWS:
    report(WARN, "platform", f"this is {sys.platform}, not Windows — run it on the target machine")

# ---- Chrome -------------------------------------------------------------

CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
)

found = None
for template in CANDIDATES:
    candidate = Path(os.path.expandvars(template))
    if candidate.exists():
        found = candidate
        break
if found is None:
    found = shutil.which("chrome") or shutil.which("msedge")
    found = Path(found) if found else None

if found:
    report(OK, "browser", str(found))
elif not ON_WINDOWS:
    report(WARN, "browser", "skipped — Windows install paths are not present here")
else:
    report(
        BAD,
        "browser",
        "no Chrome or Edge found — install Google Chrome, or set chrome.binary "
        "in settings.toml after installing",
    )

# ---- writable locations -------------------------------------------------

for label, env_name, fallback in (
    ("data dir", "LOCALAPPDATA", Path.home() / "AppData" / "Local"),
    ("config dir", "APPDATA", Path.home() / "AppData" / "Roaming"),
):
    if not ON_WINDOWS:
        report(WARN, label, "skipped — these paths only mean something on Windows")
        continue
    base = Path(os.environ.get(env_name) or fallback)
    target = base / "FlowBulkGenerator"
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("x")
        probe.unlink()
        report(OK, label, str(target))
    except OSError as exc:
        report(BAD, label, f"{target} is not writable: {exc}")

# ---- the DevTools port --------------------------------------------------

import socket  # noqa: E402

with socket.socket() as probe:
    probe.settimeout(1.0)
    if probe.connect_ex(("127.0.0.1", 9222)) == 0:
        report(
            WARN,
            "port 9222",
            "something is already listening — the app will attach to it. Change "
            "chrome.debug_port in settings.toml if that is not this app.",
        )
    else:
        report(OK, "port 9222", "free")

# ---- verdict ------------------------------------------------------------

print()
if problems:
    print(f"  {problems} problem(s) to fix before installing.\n")
    sys.exit(1)
# The installer runs this itself, where "now run the installer" is noise.
if "--from-installer" not in sys.argv:
    print("  Ready to install:  powershell -ExecutionPolicy Bypass -File install.ps1\n")
