"""Is this machine actually able to run the app, and can it be fixed here?

Written for the person who installed the .deb and opened it, and who is
not going to edit a TOML file. Every check says what is wrong in one
sentence and, where it can, carries the exact command that fixes it so
the app can offer a button instead of instructions.

Nothing here runs anything. `checks()` only looks; the caller runs
`Check.fix_command` on a thread when someone presses the button.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .platforms import IS_LINUX, display_font, find_chrome

FLATHUB_REPO = "https://flathub.org/repo/flathub.flatpakrepo"
CHROME_FLATPAK = "com.google.Chrome"


@dataclass
class Check:
    key: str
    title: str
    ok: bool
    detail: str
    #: Empty when nothing can be done automatically.
    fix_label: str = ""
    fix_command: list[str] = field(default_factory=list)
    #: True when the app is unusable until this is dealt with.
    required: bool = True
    #: Shown under the detail when there is no automatic fix.
    manual: str = ""


def _flatpak_ready() -> bool:
    if not shutil.which("flatpak"):
        return False
    try:
        out = subprocess.run(
            ["flatpak", "remotes"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "flathub" in out.stdout


def chrome_check(cfg: Config) -> Check:
    found = find_chrome(cfg.chrome.binary, cfg.chrome.flatpak_app)
    if found is not None:
        return Check(
            "chrome", "Google Chrome", True,
            f"found: {found.label}",
        )

    # Flathub needs no password and no repository to be added by hand,
    # which is the whole reason it is preferred over Google's own .deb
    # here: a user install asks for nothing.
    if IS_LINUX and shutil.which("flatpak"):
        command = (
            ["flatpak", "install", "--user", "-y", "flathub", CHROME_FLATPAK]
            if _flatpak_ready()
            else [
                "flatpak", "remote-add", "--if-not-exists", "--user",
                "flathub", FLATHUB_REPO,
            ]
        )
        return Check(
            "chrome", "Google Chrome", False,
            "not installed — the app generates images inside Chrome, so it "
            "cannot do anything without one",
            fix_label="Install Chrome", fix_command=command,
        )

    return Check(
        "chrome", "Google Chrome", False,
        "not installed — the app generates images inside Chrome",
        manual="Install it from the Pop!_Shop, or from google.com/chrome.",
    )


def signed_in_check(cfg: Config) -> Check:
    """Has anyone actually signed in on the automation profile?

    Read from disk rather than by opening a browser: this runs every time
    the panel is shown, and launching Chrome to find out would be a
    window appearing on its own.

    The signal is `account_info` in the profile's Preferences, which is
    empty until an account is attached. The obvious files are not
    signals at all - `Local State` and `Login Data` are written the first
    time Chrome runs, signed in or not, so either would have reported a
    brand-new profile as ready and sent the user to a Start button that
    lands on a Google sign-in page.
    """
    profile = Path(cfg.chrome.host_user_data_dir)
    signed = False
    preferences = profile / "Default" / "Preferences"
    try:
        data = json.loads(preferences.read_text(encoding="utf-8"))
        signed = bool(data.get("account_info"))
    except (OSError, ValueError):
        # Chrome may be mid-write, or the profile may predate this file.
        signed = (profile / "Default" / "Account Web Data").exists()

    if signed:
        return Check(
            "signin", "Signed in to Google", True,
            "the automation profile has an account",
        )
    return Check(
        "signin", "Signed in to Google", False,
        "nobody has signed in yet - the app works inside your own Google "
        "session, because Flow has no other way in",
        fix_label="Open Chrome to sign in",
        manual="Press the button, sign in to Google in the window that "
               "opens, then come back and press Check again. It is asked "
               "once, and this profile is separate from your everyday "
               "browsing.",
    )


def ffmpeg_check(cfg: Config) -> Check:
    from .platforms import find_ffmpeg

    found = find_ffmpeg(cfg.video.ffmpeg)
    if found:
        return Check("ffmpeg", "Video tools (ffmpeg)", True, f"found: {found}")
    return Check(
        "ffmpeg", "Video tools (ffmpeg)", False,
        "not installed — images will still generate, but narration, "
        "captions and the finished video cannot be built",
        fix_label="Install ffmpeg",
        fix_command=["pkexec", "apt-get", "install", "-y", "ffmpeg"],
    )


def font_check(cfg: Config) -> Check:
    """A heavy face for thumbnails. Never fatal, so never alarming."""
    face = display_font()
    if face != "sans-serif":
        return Check("font", "Thumbnail font", True, f"using {face}",
                     required=False)
    return Check(
        "font", "Thumbnail font", False,
        "no bold display font was found; thumbnails will still build, in a "
        "plainer face",
        fix_label="Install fonts",
        fix_command=["pkexec", "apt-get", "install", "-y", "fonts-dejavu-core"],
        required=False,
    )


def checks(cfg: Config) -> list[Check]:
    """Everything, in the order someone should deal with it."""
    return [
        chrome_check(cfg),
        signed_in_check(cfg),
        ffmpeg_check(cfg),
        font_check(cfg),
    ]


def blocking(results: list[Check]) -> list[Check]:
    return [c for c in results if c.required and not c.ok]


def all_ready(results: list[Check]) -> bool:
    return not blocking(results)
