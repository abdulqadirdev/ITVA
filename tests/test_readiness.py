"""Can this machine run the app, and can the app fix it here?

Written for whoever installs the .deb and opens it. They will not read a
README, so every failure has to be legible and, where possible, a button.
"""

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen import readiness  # noqa: E402
from imgen.config import load_config  # noqa: E402


def profile_cfg(tmp_path):
    cfg = load_config()
    return replace(cfg, chrome=replace(cfg.chrome, user_data_dir=str(tmp_path)))


def test_a_fresh_chrome_profile_does_not_count_as_signed_in(tmp_path):
    """The trap this check was rewritten for. `Local State` and
    `Login Data` appear the first time Chrome runs, signed in or not, so
    either would report a brand-new profile as ready - and the user would
    be sent to Start, which lands on a Google sign-in page."""
    (tmp_path / "Local State").write_text("{}")
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Login Data").write_bytes(b"sqlite")
    (default / "Preferences").write_text(json.dumps({"signin": {"allowed": True}}))

    check = readiness.signed_in_check(profile_cfg(tmp_path))
    assert not check.ok, "an empty profile must not read as signed in"
    assert check.fix_label, "and it must offer the way to fix it"


def test_an_attached_account_counts(tmp_path):
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Preferences").write_text(
        json.dumps({"account_info": [{"email": "someone@example.com"}]})
    )
    assert readiness.signed_in_check(profile_cfg(tmp_path)).ok


def test_unreadable_preferences_fall_back_rather_than_crash(tmp_path):
    """Chrome rewrites this file constantly; a half-written read must not
    take the whole Setup tab down."""
    default = tmp_path / "Default"
    default.mkdir()
    (default / "Preferences").write_text("{ this is not json")
    assert not readiness.signed_in_check(profile_cfg(tmp_path)).ok

    (default / "Account Web Data").write_bytes(b"sqlite")
    assert readiness.signed_in_check(profile_cfg(tmp_path)).ok


def test_a_missing_chrome_offers_an_install_not_a_config_file(monkeypatch):
    """The old message told people to edit chrome.binary in a TOML file,
    which is no use to the person this is written for."""
    monkeypatch.setattr(readiness, "find_chrome", lambda *a, **k: None)
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "/usr/bin/flatpak")
    monkeypatch.setattr(readiness, "_flatpak_ready", lambda: True)

    check = readiness.chrome_check(load_config())
    assert not check.ok
    assert check.fix_command[:2] == ["flatpak", "install"]
    assert "--user" in check.fix_command, "a user install asks for no password"
    assert "settings.toml" not in check.detail


def test_without_flathub_the_first_press_adds_it(monkeypatch):
    """Two presses, each of which does something, beats one that fails."""
    monkeypatch.setattr(readiness, "find_chrome", lambda *a, **k: None)
    monkeypatch.setattr(readiness.shutil, "which", lambda name: "/usr/bin/flatpak")
    monkeypatch.setattr(readiness, "_flatpak_ready", lambda: False)

    check = readiness.chrome_check(load_config())
    assert check.fix_command[:2] == ["flatpak", "remote-add"]


def test_without_flatpak_at_all_it_says_where_to_go(monkeypatch):
    monkeypatch.setattr(readiness, "find_chrome", lambda *a, **k: None)
    monkeypatch.setattr(readiness.shutil, "which", lambda name: None)

    check = readiness.chrome_check(load_config())
    assert not check.fix_command
    assert "google.com/chrome" in check.manual


def test_missing_ffmpeg_is_blocking_but_a_missing_font_is_not(monkeypatch):
    """Video is a headline feature; the thumbnail font is a preference.
    Marking both as errors teaches people to ignore the panel."""
    import imgen.platforms as platforms

    monkeypatch.setattr(platforms, "find_ffmpeg", lambda *a, **k: "")
    monkeypatch.setattr(readiness, "display_font", lambda *a, **k: "sans-serif")
    cfg = load_config()

    ffmpeg = readiness.ffmpeg_check(cfg)
    assert not ffmpeg.ok and ffmpeg.required
    assert ffmpeg.fix_command[0] == "pkexec"

    font = readiness.font_check(cfg)
    assert not font.ok and not font.required
    assert font.key not in [c.key for c in readiness.blocking([ffmpeg, font])]


def test_every_check_either_fixes_itself_or_says_what_to_do(monkeypatch):
    """A row with neither a button nor an instruction is a dead end."""
    import imgen.platforms as platforms

    monkeypatch.setattr(readiness, "find_chrome", lambda *a, **k: None)
    monkeypatch.setattr(readiness, "display_font", lambda *a, **k: "sans-serif")
    monkeypatch.setattr(platforms, "find_ffmpeg", lambda *a, **k: "")
    cfg = load_config()
    cfg = replace(cfg, chrome=replace(cfg.chrome, user_data_dir="/nonexistent"))

    for check in readiness.checks(cfg):
        if check.ok:
            continue
        assert check.fix_command or check.manual, f"{check.key} is a dead end"
        assert check.detail, f"{check.key} says nothing"
