"""The frozen-build recipe.

These are cheap assertions about things that fail silently: a bundle that
builds fine and then cannot start, or writes its projects into a temp
directory that is deleted on exit.
"""

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPEC = ROOT / "packaging" / "flow-bulk-generator.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-build.yml"


def spec_text() -> str:
    return SPEC.read_text()


def test_the_seed_configs_are_bundled():
    """ensure_config() copies these to a fresh machine on first run; without
    them an installed copy has no settings at all."""
    body = spec_text()
    assert "defaults" in body and "settings.toml" in body and "selectors.toml" in body


def test_the_playwright_driver_is_bundled():
    """Playwright talks to a bundled Node binary over a pipe. It is required
    even though we only attach to an existing Chrome."""
    assert 'collect_data_files("playwright")' in spec_text()


def test_hidden_imports_cover_what_pyinstaller_cannot_see():
    body = spec_text()
    for name in ("gspread", "openpyxl", "playwright.sync_api"):
        assert name in body, f"{name} is imported indirectly and must be declared"
    assert "google.auth" in body and "google.oauth2" in body


def test_the_unused_heavy_qt_modules_are_excluded():
    body = spec_text()
    for name in ("QtWebEngineCore", "QtQuick3D", "QtMultimedia", "Qt3DRender"):
        assert name in body, f"{name} ships in the wheel, is unused, and is large"


def test_the_build_is_one_dir_not_one_file():
    """A one-file build of this size unpacks ~300 MB to a temp directory on
    every launch."""
    body = spec_text()
    assert "COLLECT(" in body
    assert "exclude_binaries=True" in body


def test_the_gui_build_has_no_console_window():
    assert "console=False" in spec_text()


# ---- the workflow -------------------------------------------------------

def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def test_workflow_builds_on_windows():
    assert workflow()["jobs"]["build"]["runs-on"] == "windows-latest"


def test_workflow_runs_the_tests_before_building():
    steps = workflow()["jobs"]["build"]["steps"]
    names = [s.get("name", "") for s in steps]
    test_at = next(i for i, n in enumerate(names) if "test" in n.lower())
    build_at = next(i for i, n in enumerate(names) if n == "Build")
    assert test_at < build_at, "a broken build should fail before it is packaged"


def test_workflow_verifies_the_executable_exists():
    """PyInstaller exits 0 on plenty of builds that produce nothing usable."""
    body = WORKFLOW.read_text()
    assert "Test-Path $exe" in body and "throw" in body


@pytest.mark.parametrize("marker", ["upload-artifact", "action-gh-release"])
def test_workflow_publishes_the_result(marker):
    assert marker in WORKFLOW.read_text()


def test_the_shipped_defaults_match_the_live_config():
    """A fresh machine is seeded from imgen/defaults. Kept in step by
    hand, it drifted until the whole [video] section and four sheet
    columns were missing - and nothing broke, because every reader uses
    .get() with a default, so it went unnoticed for several releases.
    An installed copy simply had settings it could not show anyone."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "sync_defaults.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_shipped_defaults_name_nobody():
    """The zip and the .deb go to other people."""
    import tomli

    data = tomli.loads((ROOT / "imgen" / "defaults" / "settings.toml").read_text())
    assert data["sheets"]["spreadsheet_id"] == ""
    assert data["video"]["music"] == ""
    assert data["video"]["piper_model"] == ""
    text = (ROOT / "imgen" / "defaults" / "settings.toml").read_text()
    for leak in ("abdulqadir", "/home/", "1lRUq"):
        assert leak not in text, f"{leak!r} must not ship"


# ---- the Debian package -------------------------------------------------

DEB_SCRIPT = ROOT / "packaging" / "build_deb.sh"


def test_the_deb_declares_what_the_bundle_cannot_carry():
    """Python, Qt and the browser driver are inside the bundle, so the
    package must not pretend to need them - but ffmpeg is shelled out to
    and fontconfig is read for the font list, and neither is bundled."""
    text = DEB_SCRIPT.read_text()
    depends = next(
        line for line in text.splitlines() if line.startswith("Depends:")
    )
    assert "ffmpeg" in depends
    assert "fontconfig" in depends
    # Qt's X libraries ship inside the bundle; depending on them would
    # make the package fail to install for no reason.
    for bundled in ("libxcb-cursor0", "python3", "libqt", "pyside"):
        assert bundled not in depends.lower()


def test_the_menu_entry_has_one_main_category():
    """Two main categories and the app can appear twice in the menu;
    desktop-file-validate warns about exactly this."""
    text = DEB_SCRIPT.read_text()
    line = next(line for line in text.splitlines() if line.startswith("Categories="))
    mains = {"AudioVideo", "Development", "Education", "Game", "Graphics",
             "Network", "Office", "Science", "Settings", "System", "Utility"}
    listed = [c for c in line.split("=", 1)[1].split(";") if c]
    assert len(set(listed) & mains) == 1, listed


def test_the_launcher_is_a_wrapper_not_a_symlink():
    """A PyInstaller bundle finds its own directory from argv[0]; a
    symlink from /usr/bin points it at the wrong place."""
    text = DEB_SCRIPT.read_text()
    assert "exec /opt/flow-bulk-generator/FlowBulkGenerator" in text
    assert "ln -s" not in text


def test_the_deb_build_refuses_stale_defaults():
    """The package seeds every fresh machine from imgen/defaults, which
    is the one place the drift actually reached a user."""
    assert "sync_defaults.py --check" in DEB_SCRIPT.read_text()


def test_there_is_exactly_one_copy_of_the_version():
    """The updater compares this number against the newest GitHub tag.
    A second copy in pyproject would drift, and the drift would show up
    as 'no updates available' forever on every other machine."""
    import tomli

    import imgen

    data = tomli.loads((ROOT / "pyproject.toml").read_text())
    assert "version" in data["project"].get("dynamic", []), (
        "pyproject must read the version from imgen, not carry its own"
    )
    assert "version" not in data["project"]
    assert data["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "imgen.__version__"
    }
    # And the packaging script must read the same place.
    script = (ROOT / "packaging" / "build_deb.sh").read_text()
    assert "imgen/__init__.py" in script
    assert "pyproject.toml | head" not in script

    assert imgen.__version__.count(".") == 2, imgen.__version__


def test_the_release_script_refuses_to_go_backwards():
    """An older tag becomes 'latest' on GitHub, and every machine is then
    told it is up to date forever."""
    script = (ROOT / "tools" / "release.sh").read_text()
    assert "sort -V" in script, "versions must compare numerically"
    assert "is older than the current" in script
    # A release whose .deb is missing is offered and cannot be installed.
    assert "gh release create" in script
    assert 'dist/flow-bulk-generator_${VERSION}' in script


def test_the_build_output_is_never_committable():
    """`git add -A` on this project is a normal first step, and dist/
    holds a 332 MB bundle and an 86 MB .deb. Committed once, they are in
    the history forever."""
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    rules = {line.strip() for line in ignored if line.strip()
             and not line.startswith("#")}
    for must in ("dist/", "build/", "projects/", "output/", "secrets/",
                 ".venv/", "*.db"):
        assert must in rules, f"{must} must be ignored"


def test_the_readme_links_point_somewhere():
    """A README is the first thing anyone cloning this reads, and a dead
    link in it is the first thing they find."""
    import re

    text = (ROOT / "README.md").read_text()
    targets = re.findall(r"\]\(([^)]+)\)", text)
    for target in targets:
        if target.startswith(("http", "#", "../")):
            continue  # external, an anchor, or GitHub-relative
        assert (ROOT / target).exists(), f"README links to missing {target}"


def test_the_readme_documents_every_sheet_column_the_app_reads():
    """Someone setting up a sheet works from this table. A column the
    app reads and the README does not mention is one nobody fills in."""
    from imgen.config import load_config

    text = (ROOT / "README.md").read_text()
    sheets = load_config().sheets
    for attribute in (
        "prompt_column", "sequence_column", "scene_column",
        "emotion_column", "voice_column", "animation_column",
        "thumbnail_column",
    ):
        name = getattr(sheets, attribute)
        assert f"`{name}`" in text, f"README never mentions the {name!r} column"
