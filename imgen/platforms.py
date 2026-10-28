"""Platform differences, kept in one place.

The app runs on Linux (native Chrome or the flatpak), Windows and macOS. What
actually varies is small but spread out: where Chrome lives, where a private
profile may be written, and how to show a file to the user.

One rule holds everywhere: the automation profile is never Chrome's default
profile. Since Chrome 136, --remote-debugging-port is refused on the default
user-data-dir on every platform, specifically so that local processes cannot
siphon cookies out of the everyday browser.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "FlowBulkGenerator"

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MACOS


@dataclass(frozen=True)
class ChromeLauncher:
    """How to start Chrome on this machine."""

    kind: str                 # "flatpak" | "binary"
    command: list[str]        # argv prefix, before Chrome's own flags
    label: str                # human-readable, for diagnostics

    @property
    def is_flatpak(self) -> bool:
        return self.kind == "flatpak"


# ---- locating Chrome ----------------------------------------------------

WINDOWS_CANDIDATES = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
)

MACOS_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)

LINUX_BINARIES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)


_WIN_VAR = re.compile(r"%([^%]+)%")


def expand(template: str) -> str:
    """Expand %VAR% and $VAR regardless of the host platform.

    os.path.expandvars only understands %VAR% when actually running on
    Windows, which made the Windows search paths impossible to test — and
    silently un-expanded anywhere else.
    """
    expanded = _WIN_VAR.sub(
        lambda m: os.environ.get(m.group(1), os.environ.get(m.group(1).upper(), m.group(0))),
        template,
    )
    # Normalise separators: a backslash is an ordinary character on POSIX, so
    # a Windows-style path would otherwise collapse into one long filename.
    # Windows accepts forward slashes, so this is safe in both directions.
    return os.path.expandvars(expanded).replace("\\", "/")


def flatpak_has(app_id: str) -> bool:
    if not IS_LINUX or not shutil.which("flatpak"):
        return False
    try:
        result = subprocess.run(
            ["flatpak", "info", app_id], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def find_chrome(explicit: str = "", flatpak_app: str = "com.google.Chrome") -> ChromeLauncher | None:
    """Locate a Chromium-family browser, preferring an explicit setting."""
    if explicit:
        path = Path(expand(explicit)).expanduser()
        if path.exists():
            return ChromeLauncher("binary", [str(path)], str(path))
        found = shutil.which(explicit)
        if found:
            return ChromeLauncher("binary", [found], found)
        return None

    if IS_LINUX:
        # Prefer a native binary: it needs no sandbox reasoning at all. The
        # flatpak works too, and is what this machine actually has.
        for name in LINUX_BINARIES:
            found = shutil.which(name)
            if found:
                return ChromeLauncher("binary", [found], found)
        if flatpak_app and flatpak_has(flatpak_app):
            return ChromeLauncher(
                "flatpak", ["flatpak", "run", flatpak_app], f"flatpak {flatpak_app}"
            )
        return None

    candidates = WINDOWS_CANDIDATES if IS_WINDOWS else MACOS_CANDIDATES
    for template in candidates:
        path = Path(expand(template)).expanduser()
        if path.exists():
            return ChromeLauncher("binary", [str(path)], str(path))
    for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
        found = shutil.which(name)
        if found:
            return ChromeLauncher("binary", [found], found)
    return None


# ---- ffmpeg -------------------------------------------------------------

FFMPEG_NAMES = ("ffmpeg.exe", "ffmpeg") if IS_WINDOWS else ("ffmpeg",)
FFPROBE_NAMES = ("ffprobe.exe", "ffprobe") if IS_WINDOWS else ("ffprobe",)


def _bundled_dir() -> Path | None:
    """Where a frozen build keeps the binaries shipped alongside it."""
    if not getattr(sys, "frozen", False):
        return None
    # PyInstaller one-dir: the executable sits next to _internal/.
    return Path(sys.executable).parent


def find_tool(names: tuple[str, ...], explicit: str = "") -> Path | None:
    """Locate ffmpeg or ffprobe: an explicit setting, then the bundle, then PATH."""
    if explicit:
        candidate = Path(expand(explicit)).expanduser()
        if candidate.exists():
            return candidate
        found = shutil.which(explicit)
        return Path(found) if found else None

    bundle = _bundled_dir()
    if bundle:
        for name in names:
            for where in (bundle, bundle / "ffmpeg", bundle / "_internal"):
                candidate = where / name
                if candidate.exists():
                    return candidate

    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)

    if IS_WINDOWS:
        for template in (
            r"%ProgramFiles%\ffmpeg\bin",
            r"%LocalAppData%\Microsoft\WinGet\Links",
        ):
            for name in names:
                candidate = Path(expand(template)) / name
                if candidate.exists():
                    return candidate
    return None


def find_ffmpeg(explicit: str = "") -> Path | None:
    return find_tool(FFMPEG_NAMES, explicit)


def find_ffprobe(explicit: str = "") -> Path | None:
    probe = find_tool(FFPROBE_NAMES, explicit)
    if probe:
        return probe
    # Some builds ship them side by side; derive one from the other.
    ffmpeg = find_ffmpeg(explicit)
    if ffmpeg:
        for name in FFPROBE_NAMES:
            candidate = ffmpeg.with_name(name)
            if candidate.exists():
                return candidate
    return None


# ---- where things live --------------------------------------------------

def app_data_dir() -> Path:
    """Per-user storage for this app's own files."""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "flow-bulk-generator"


def user_config_dir() -> Path:
    """Where this app's editable configuration lives once installed."""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "flow-bulk-generator"


def automation_profile_dir(launcher: ChromeLauncher | None, flatpak_app: str) -> Path:
    """Where the dedicated automation profile is kept.

    For the flatpak this must sit inside the app's own ~/.var/app tree, which
    it can always write to — anywhere else would need a `flatpak override
    --filesystem` grant. Verified on Linux: the sandbox does not remap $HOME,
    so this one absolute path is valid inside and outside the sandbox alike.
    """
    if launcher is not None and launcher.is_flatpak:
        return Path.home() / ".var" / "app" / flatpak_app / "config" / "google-chrome-automation"
    return app_data_dir() / "chrome-automation"


# ---- showing things to the user ----------------------------------------

def open_path(path: str | Path) -> None:
    """Open a file or folder in the desktop's default handler."""
    target = str(path)
    if IS_WINDOWS:
        os.startfile(target)  # noqa: S606 - the platform's own opener
        return
    opener = "open" if IS_MACOS else "xdg-open"
    try:
        subprocess.Popen([opener, target])
    except OSError:
        pass


def detach_kwargs() -> dict:
    """Popen flags that let a launched browser outlive this process."""
    if IS_WINDOWS:
        # CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS
        return {"creationflags": 0x00000200 | 0x00000008}
    return {"start_new_session": True}


def terminate(pid: int) -> bool:
    """Stop a process we started, without pattern-matching on names.

    Deliberately pid-based: a `pkill -f chrome` would be able to match and
    kill unrelated processes, including the shell running it.
    """
    import signal

    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
            )
            return True
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
        return False


#: Faces worth offering first for a thumbnail: heavy, condensed, legible
#: small. Only those actually installed are shown.
DISPLAY_FIRST = (
    "Arial Black", "Impact", "Anton", "Bebas Neue", "Montserrat ExtraBold",
    "Fira Sans Compressed Heavy", "Fira Sans Compressed ExtraBold",
    "Fira Sans Condensed Heavy", "Fira Sans Heavy", "Oswald",
    "DejaVu Sans Condensed", "DejaVu Sans", "Liberation Sans Narrow",
    "Arial", "Verdana", "Tahoma", "Georgia", "Impact",
)


#: Preference order for a thumbnail's heavy display face. Arial Black is
#: first because it is what most thumbnails use, but it ships with
#: Microsoft's fonts and is absent from a stock Pop!_OS or Ubuntu - which
#: is exactly the machine an installer lands on. Fira Sans is Pop's own UI
#: family, so its heavy cuts are usually there.
DISPLAY_PREFERENCE = (
    "Arial Black", "Impact", "Anton",
    "Fira Sans Compressed Heavy", "Fira Sans Condensed Heavy",
    "Fira Sans Extra Bold", "Liberation Sans Narrow",
    "DejaVu Sans Condensed", "DejaVu Sans",
)


def display_font(preference=DISPLAY_PREFERENCE) -> str:
    """The best heavy face this machine actually has.

    Naming one that is not installed is the quiet failure: the page falls
    back to whatever it likes and the thumbnail comes out in the wrong
    face with nothing said anywhere.
    """
    available = set(installed_fonts())
    for name in preference:
        if name in available:
            return name
    return "sans-serif"


def installed_fonts() -> list[str]:
    """Font families this machine can actually render.

    Offering a name the machine does not have is worse than offering
    fewer: the page silently falls back and the thumbnail comes out in
    the wrong face with no warning anywhere.
    """
    found: list[str] = []
    if not IS_WINDOWS and not IS_MACOS:
        try:
            out = subprocess.run(
                ["fc-list", ":", "family"],
                capture_output=True, text=True, timeout=10,
            )
            seen = set()
            for line in out.stdout.splitlines():
                for name in line.split(","):
                    name = name.strip()
                    if name and name not in seen:
                        seen.add(name)
                        found.append(name)
        except (OSError, subprocess.SubprocessError):
            found = []
    if not found:
        # Windows and macOS, or a Linux box with no fontconfig tools:
        # fall back to faces those systems ship.
        found = [
            "Arial", "Arial Black", "Impact", "Verdana", "Tahoma",
            "Trebuchet MS", "Georgia", "Times New Roman", "Courier New",
            "Helvetica", "Helvetica Neue", "Futura", "Gill Sans",
            "DejaVu Sans",
        ]
    preferred = [f for f in DISPLAY_FIRST if f in found]
    rest = sorted(f for f in found if f not in preferred)
    # Dedupe while keeping the preferred ones on top.
    ordered, seen = [], set()
    for name in preferred + rest:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered
