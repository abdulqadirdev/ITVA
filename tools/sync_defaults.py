"""Regenerate imgen/defaults/settings.toml from the working config.

The shipped defaults are what a fresh machine is seeded with. Keeping
them in step by hand did not work: by the time anyone looked, the whole
[video] section and four sheet columns were missing, so an installed copy
started with settings it could not show the user. Nothing was broken -
every reader uses .get() with a default - which is exactly why it went
unnoticed for several releases.

So the defaults are generated from the live config, with every personal
value blanked by name. Run after adding a setting:

    python tools/sync_defaults.py          # write
    python tools/sync_defaults.py --check  # fail if out of date

`tests/test_packaging.py` runs the check, so the two cannot drift again.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "config" / "settings.toml"
SHIPPED = ROOT / "imgen" / "defaults" / "settings.toml"

#: Anything naming this machine, this account or this spreadsheet. A
#: value here is emptied; a key absent here ships as written.
PERSONAL = {
    ("sheets", "spreadsheet_id"),
    ("sheets", "file_path"),
    ("sheets", "service_account_file"),
    ("flow", "project_url"),
    ("video", "music"),
    ("video", "piper_model"),
    ("video", "ffmpeg"),
    ("video", "ffprobe"),
    ("chrome", "binary"),
    ("chrome", "user_data_dir"),
    # A token in a shipped build belongs to whoever holds the build.
    ("update", "token"),
}

HEADER = """# Defaults for a fresh machine. GENERATED - do not edit by hand.
#
# Run tools/sync_defaults.py after changing config/settings.toml; the
# packaging test fails if these two drift apart. Personal values (the
# spreadsheet, local file paths) are blanked on the way through.

"""


def neutralise(line: str, section: str) -> str:
    """Blank a personal value, keeping the key and its comment."""
    if "=" not in line or line.lstrip().startswith("#"):
        return line
    key = line.split("=", 1)[0].strip()
    if (section, key) not in PERSONAL:
        return line
    return f'{key} = ""\n'


def generate() -> str:
    out, section = [HEADER], ""
    for line in LIVE.read_text(encoding="utf-8").splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]").split(".")[0]
        out.append(neutralise(line, section))
    return "".join(out)


def main() -> int:
    wanted = generate()
    if "--check" in sys.argv:
        current = SHIPPED.read_text(encoding="utf-8") if SHIPPED.exists() else ""
        if current != wanted:
            print(
                "imgen/defaults/settings.toml is out of date.\n"
                "Run: python tools/sync_defaults.py",
                file=sys.stderr,
            )
            return 1
        print("defaults are in step with config/settings.toml")
        return 0
    SHIPPED.write_text(wanted, encoding="utf-8")
    print(f"wrote {SHIPPED.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
