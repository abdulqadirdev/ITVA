"""Finding, fetching and installing a newer release from GitHub.

The app is installed to /opt by a .deb, which is root-owned, so it cannot
replace itself: the install step hands the package to `pkexec apt-get`,
and the desktop's own polkit dialog asks for the password. The alternative
- a helper running as root in the background - would be a far larger thing
to trust for a far smaller convenience.

Nothing here knows about Qt. The GUI runs `check()` on a worker thread and
`install()` behind a button; the CLI calls the same two functions.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

API = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = f"flow-bulk-generator/{__version__}"

#: How long a check is believed before asking GitHub again. Unauthenticated
#: callers get 60 requests an hour from one address, and several laptops in
#: one office share that address, so a check on every launch is both rude
#: and pointless - releases do not appear minute by minute.
CACHE_SECONDS = 6 * 3600


class UpdateError(RuntimeError):
    """The check or the install could not be completed."""


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    url: str
    name: str
    size: int

    @property
    def megabytes(self) -> float:
        return self.size / 1_000_000


def parse_version(text: str) -> tuple[int, ...]:
    """Turn "v1.10.2" into (1, 10, 2) for comparison.

    Comparing the strings instead would make 1.10.0 *older* than 1.9.0,
    which is the classic way an updater silently stops offering updates
    at exactly the point the version numbers get interesting.
    """
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers[:4]) or (0,)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def _request(url: str, token: str = "", accept: str = "application/vnd.github+json"):
    request = urllib.request.Request(url)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Accept", accept)
    # Only needed for a private repository. A token shipped inside the app
    # would be readable by anyone holding the app, so this is meant to be
    # set per machine, not baked into the build.
    token = token or os.environ.get("FLOW_UPDATE_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    return request


def check(repo: str, token: str = "", timeout: float = 15.0) -> Release | None:
    """The newest release, or None when this copy is already current."""
    if not repo or "/" not in repo:
        raise UpdateError(
            "no GitHub repository is configured - set update.repo to "
            "'owner/name' in settings"
        )
    try:
        with urllib.request.urlopen(
            _request(API.format(repo=repo), token), timeout=timeout
        ) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError(
                f"no releases found for {repo}. A private repository also "
                "answers 404 without a token."
            ) from exc
        if exc.code in (401, 403):
            raise UpdateError(
                "GitHub refused the request - either the hourly limit was "
                "reached or the repository is private and needs a token."
            ) from exc
        raise UpdateError(f"GitHub returned {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"could not reach GitHub: {exc}") from exc

    tag = str(payload.get("tag_name") or "")
    if not is_newer(tag):
        return None

    asset = next(
        (
            a
            for a in payload.get("assets", [])
            if str(a.get("name", "")).endswith(".deb")
        ),
        None,
    )
    if asset is None:
        raise UpdateError(f"release {tag} has no .deb attached to it")

    return Release(
        version=".".join(str(n) for n in parse_version(tag)),
        tag=tag,
        notes=str(payload.get("body") or "").strip(),
        url=str(asset["url"]),
        name=str(asset["name"]),
        size=int(asset.get("size") or 0),
    )


def download(
    release: Release,
    destination: Path | None = None,
    token: str = "",
    on_progress=None,
    timeout: float = 60.0,
) -> Path:
    """Fetch the .deb, reporting progress as a fraction from 0 to 1."""
    destination = destination or Path(tempfile.mkdtemp(prefix="flow-update-"))
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / release.name

    # The asset API URL serves JSON metadata unless octet-stream is asked
    # for; without this header the download is a few hundred bytes of JSON
    # that dpkg then rejects as a corrupt archive.
    request = _request(release.url, token, accept="application/octet-stream")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = int(response.headers.get("Content-Length") or release.size or 0)
            done = 0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if on_progress and total:
                        on_progress(min(done / total, 1.0))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError(f"download failed: {exc}") from exc

    if release.size and target.stat().st_size != release.size:
        raise UpdateError(
            f"downloaded {target.stat().st_size} bytes, expected {release.size}"
        )
    return target


def install(package: Path, log=lambda message: None) -> None:
    """Install the .deb, asking for a password through the desktop.

    apt rather than dpkg so a new dependency is pulled in rather than
    leaving the package half-configured.
    """
    if not package.exists():
        raise UpdateError(f"the downloaded package is missing ({package})")
    if shutil.which("pkexec") is None:
        raise UpdateError(
            "pkexec is not installed, so the update cannot ask for a "
            f"password. Install it by hand:\n    sudo apt install {package}"
        )

    command = [
        "pkexec", "apt-get", "install", "-y",
        "--allow-downgrades", str(package),
    ]
    log("asking for permission to install...")
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=900,
        # apt refuses to run without this when it has no terminal.
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
    if result.returncode == 126:
        raise UpdateError("the password prompt was dismissed - nothing was changed")
    if result.returncode != 0:
        tail = "\n".join(
            (result.stderr or result.stdout).strip().splitlines()[-6:]
        )
        raise UpdateError(f"apt failed:\n{tail}")
    log("installed - restart the app to use the new version")


# ---- remembering the last answer ----------------------------------------


def cache_path(base: Path) -> Path:
    return base / "update-check.json"


def read_cache(base: Path, max_age: float = CACHE_SECONDS) -> dict | None:
    """The last check, if it is recent enough to believe."""
    path = cache_path(base)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(data.get("checked_at", 0)) > max_age:
        return None
    return data


def write_cache(base: Path, release: Release | None) -> None:
    payload = {
        "checked_at": time.time(),
        "current": __version__,
        "latest": release.version if release else __version__,
    }
    try:
        cache_path(base).parent.mkdir(parents=True, exist_ok=True)
        cache_path(base).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        # A cache that cannot be written is not worth failing a check over.
        pass
