"""Per-OS behaviour, exercised without being on that OS.

The Windows and macOS paths cannot be run here, so the logic that decides
them is tested directly. Getting these wrong means an installed copy writes
into the wrong place, or no browser is found at all.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen import platforms  # noqa: E402


@pytest.fixture
def as_os(monkeypatch):
    def _set(name: str, env: dict | None = None):
        monkeypatch.setattr(platforms, "IS_WINDOWS", name == "windows")
        monkeypatch.setattr(platforms, "IS_MACOS", name == "macos")
        monkeypatch.setattr(platforms, "IS_LINUX", name == "linux")
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
    return _set


# ---- where user data goes ----------------------------------------------

def test_windows_uses_localappdata(as_os, tmp_path):
    as_os("windows", {"LOCALAPPDATA": str(tmp_path / "Local")})
    assert platforms.app_data_dir() == tmp_path / "Local" / "FlowBulkGenerator"


def test_windows_config_uses_roaming_appdata(as_os, tmp_path):
    as_os("windows", {"APPDATA": str(tmp_path / "Roaming")})
    assert platforms.user_config_dir() == tmp_path / "Roaming" / "FlowBulkGenerator"


def test_macos_uses_application_support(as_os):
    as_os("macos")
    expected = Path.home() / "Library" / "Application Support" / "FlowBulkGenerator"
    assert platforms.app_data_dir() == expected
    assert platforms.user_config_dir() == expected


def test_linux_follows_the_xdg_variables(as_os, tmp_path):
    as_os("linux", {"XDG_DATA_HOME": str(tmp_path / "data"),
                    "XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    assert platforms.app_data_dir() == tmp_path / "data" / "flow-bulk-generator"
    assert platforms.user_config_dir() == tmp_path / "cfg" / "flow-bulk-generator"


def test_linux_falls_back_when_xdg_is_unset(as_os, monkeypatch):
    as_os("linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert platforms.app_data_dir() == Path.home() / ".local" / "share" / "flow-bulk-generator"


# ---- the automation profile --------------------------------------------

def test_flatpak_profile_lives_inside_the_sandbox_own_tree(as_os):
    as_os("linux")
    launcher = platforms.ChromeLauncher("flatpak", ["flatpak", "run", "x"], "x")
    # Anywhere else would need a `flatpak override --filesystem` grant.
    assert platforms.automation_profile_dir(launcher, "com.google.Chrome") == (
        Path.home() / ".var" / "app" / "com.google.Chrome"
        / "config" / "google-chrome-automation"
    )


def test_a_plain_binary_keeps_its_profile_in_the_app_data_dir(as_os, tmp_path):
    as_os("windows", {"LOCALAPPDATA": str(tmp_path)})
    launcher = platforms.ChromeLauncher("binary", ["chrome.exe"], "chrome.exe")
    assert platforms.automation_profile_dir(launcher, "ignored") == (
        tmp_path / "FlowBulkGenerator" / "chrome-automation"
    )


def test_the_profile_is_never_chromes_default(as_os, tmp_path):
    """Chrome 136+ refuses --remote-debugging-port on the default profile."""
    as_os("linux", {"XDG_DATA_HOME": str(tmp_path)})
    resolved = platforms.automation_profile_dir(None, "com.google.Chrome")
    assert "chrome-automation" in str(resolved)
    assert resolved != Path.home() / ".config" / "google-chrome"


# ---- finding a browser --------------------------------------------------

def test_an_explicit_binary_path_wins(as_os, tmp_path):
    as_os("windows")
    exe = tmp_path / "chrome.exe"
    exe.write_text("")
    found = platforms.find_chrome(explicit=str(exe))
    assert found.kind == "binary" and found.command == [str(exe)]


def test_an_explicit_path_that_does_not_exist_is_reported_as_not_found(as_os):
    as_os("windows")
    assert platforms.find_chrome(explicit="C:/nope/chrome.exe") is None


def test_windows_searches_the_usual_install_locations(as_os, tmp_path, monkeypatch):
    as_os("windows", {"ProgramFiles": str(tmp_path)})
    target = tmp_path / "Google" / "Chrome" / "Application" / "chrome.exe"
    target.parent.mkdir(parents=True)
    target.write_text("")
    monkeypatch.setattr(platforms.shutil, "which", lambda _n: None)
    found = platforms.find_chrome()
    assert found is not None and found.command == [str(target)]


def test_linux_prefers_a_native_binary_over_the_flatpak(as_os, monkeypatch):
    as_os("linux")
    monkeypatch.setattr(platforms.shutil, "which",
                        lambda n: "/usr/bin/google-chrome" if n == "google-chrome" else None)
    monkeypatch.setattr(platforms, "flatpak_has", lambda _a: True)
    found = platforms.find_chrome()
    assert found.kind == "binary", "a native binary needs no sandbox reasoning"


def test_linux_falls_back_to_the_flatpak(as_os, monkeypatch):
    as_os("linux")
    monkeypatch.setattr(platforms.shutil, "which", lambda _n: None)
    monkeypatch.setattr(platforms, "flatpak_has", lambda _a: True)
    found = platforms.find_chrome()
    assert found.kind == "flatpak" and found.is_flatpak


# ---- process handling ---------------------------------------------------

def test_detach_flags_differ_per_platform(as_os):
    as_os("windows")
    assert "creationflags" in platforms.detach_kwargs()
    as_os("linux")
    assert platforms.detach_kwargs() == {"start_new_session": True}


# ---- variable expansion -------------------------------------------------

def test_windows_style_variables_expand_on_any_host(monkeypatch):
    """os.path.expandvars only handles %VAR% when run on Windows, which left
    the Windows search paths unexpanded (and untestable) everywhere else."""
    monkeypatch.setenv("ProgramFiles", "C:/PF")
    assert platforms.expand("%ProgramFiles%/Google") == "C:/PF/Google"


def test_unknown_variables_are_left_alone_rather_than_blanked(monkeypatch):
    monkeypatch.delenv("NOPE_NOT_SET", raising=False)
    assert platforms.expand("%NOPE_NOT_SET%/x") == "%NOPE_NOT_SET%/x"


def test_unix_style_variables_still_expand(monkeypatch):
    monkeypatch.setenv("HOME_TEST", "/home/x")
    assert platforms.expand("$HOME_TEST/y") == "/home/x/y"


def test_backslash_paths_are_usable_on_any_host(monkeypatch):
    """A backslash is an ordinary character on POSIX, so an un-normalised
    Windows path collapses into a single long filename."""
    monkeypatch.setenv("ProgramFiles", "C:/PF")
    assert platforms.expand(r"%ProgramFiles%\Google\Chrome") == "C:/PF/Google/Chrome"


# ---- the installer and the app must agree on where things live ----------

def _ps1() -> str:
    return (Path(__file__).resolve().parent.parent / "install.ps1").read_text()


def test_installer_prefix_matches_where_the_app_looks(as_os, tmp_path):
    """install.ps1 puts the venv and projects under %LOCALAPPDATA%; the app
    must read them from the same place, or an install silently produces an
    empty-looking app."""
    assert "Join-Path $env:LOCALAPPDATA 'FlowBulkGenerator'" in _ps1()

    as_os("windows", {"LOCALAPPDATA": str(tmp_path)})
    assert platforms.app_data_dir() == tmp_path / "FlowBulkGenerator"


def test_installer_config_dir_matches_where_the_app_looks(as_os, tmp_path):
    assert "Join-Path $env:APPDATA   'FlowBulkGenerator'" in _ps1()

    as_os("windows", {"APPDATA": str(tmp_path)})
    assert platforms.user_config_dir() == tmp_path / "FlowBulkGenerator"


def test_installer_does_not_abort_when_shortcuts_cannot_be_made():
    """A machine with Windows Script Host locked down should still end up
    with a working install."""
    body = _ps1()
    assert "try { $shell = New-Object -ComObject WScript.Shell } catch { }" in body
    assert "$shortcutMade" in body, "the summary must not claim a shortcut that failed"


def test_installer_resolves_the_venv_layout_rather_than_assuming():
    body = _ps1()
    assert "Join-Path $Venv 'Scripts'" in body
    assert "Join-Path $Venv 'bin'" in body


# ---- frozen builds ------------------------------------------------------

def test_a_frozen_build_never_counts_as_a_checkout(monkeypatch):
    """Inside a PyInstaller bundle the package sits in a temp extraction
    directory that is deleted on exit. Treating that as a checkout would put
    projects and generated images there."""
    import importlib
    import sys as _sys

    from imgen import config as config_module

    monkeypatch.setattr(_sys, "frozen", True, raising=False)
    reloaded = importlib.reload(config_module)
    try:
        assert reloaded.FROZEN is True
        assert reloaded.DEV_MODE is False
        assert reloaded.data_dir() == reloaded.app_data_dir() if hasattr(
            reloaded, "app_data_dir"
        ) else True
    finally:
        monkeypatch.undo()
        importlib.reload(config_module)
