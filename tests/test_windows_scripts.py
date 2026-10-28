"""The shipped Windows scripts must survive Windows PowerShell 5.1.

Windows runs `powershell.exe` (5.1) by default, not `pwsh` (7.x). 5.1 decodes
a BOM-less .ps1 using the system ANSI codepage, so a single UTF-8 em dash
became `a EUR "` under CP1252 — and PowerShell accepts that trailing smart
quote as a real string delimiter. One em dash in a comment opened a string
that never closed, and the whole script failed to parse.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PS_SCRIPTS = ["install.ps1", "uninstall.ps1"]
ALL_SCRIPTS = PS_SCRIPTS + ["Install.bat"]

# What PowerShell treats as a string delimiter beyond the ASCII ones.
SMART_QUOTES = "‘’‚‛“”„‟"


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_scripts_contain_no_characters_outside_ascii(name):
    raw = (ROOT / name).read_bytes().lstrip(b"\xef\xbb\xbf")
    try:
        raw.decode("ascii")
    except UnicodeDecodeError as exc:
        line = raw[: exc.start].count(b"\n") + 1
        pytest.fail(f"{name} line {line}: non-ASCII byte {raw[exc.start]:#x}")


@pytest.mark.parametrize("name", PS_SCRIPTS)
def test_powershell_scripts_carry_a_utf8_bom(name):
    """Without it, 5.1 guesses the encoding from the system codepage."""
    assert (ROOT / name).read_bytes().startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_scripts_use_crlf_line_endings(name):
    body = (ROOT / name).read_bytes()
    assert b"\r\n" in body
    assert b"\n" not in body.replace(b"\r\n", b""), "a bare LF is left somewhere"


@pytest.mark.parametrize("name", PS_SCRIPTS)
def test_misreading_as_cp1252_cannot_introduce_a_quote(name):
    """The actual failure, reproduced: decode the way 5.1 would without a BOM
    and check no stray quote character appears."""
    raw = (ROOT / name).read_bytes().lstrip(b"\xef\xbb\xbf")
    as_ansi = raw.decode("cp1252", errors="replace")
    assert not any(ch in as_ansi for ch in SMART_QUOTES), (
        "a byte decodes to a typographic quote under CP1252; PowerShell treats "
        "those as real string delimiters"
    )
    assert as_ansi == raw.decode("ascii"), "ANSI and ASCII readings must agree"


@pytest.mark.parametrize("name", PS_SCRIPTS)
def test_quotes_are_balanced_line_by_line(name):
    """A cheap guard against the symptom even if the cause changes."""
    text = (ROOT / name).read_text(encoding="utf-8-sig")
    for number, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0] if line.strip().startswith("#") else line
        if code.count('"') % 2 or code.count("'") % 2:
            # Apostrophes inside comments are fine; flag only real code.
            if not line.strip().startswith("#"):
                pytest.fail(f"{name} line {number}: unbalanced quote -> {line.strip()[:70]}")


def test_no_powershell_7_only_syntax():
    """Windows runs 5.1; PS7 features would parse there but fail at runtime."""
    for name in PS_SCRIPTS:
        body = (ROOT / name).read_text(encoding="utf-8-sig")
        assert "??" not in body, f"{name}: null-coalescing is PS7+"
        assert "?." not in body, f"{name}: null-conditional is PS7+"
        if "$IsWindows" in body:
            assert "$null -eq $IsWindows" in body, (
                f"{name}: $IsWindows does not exist in 5.1 and must be guarded"
            )


# ---- Python detection on a real Windows machine -------------------------

def test_python_probe_survives_a_command_that_writes_to_stderr():
    """Under $ErrorActionPreference='Stop', a native command's stderr becomes
    a terminating error, and `2>$null` does not prevent it in 5.1. The
    Microsoft Store stub writes to stderr, which killed the installer before
    it could fall back to anything."""
    body = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "function Invoke-Probe" in body
    assert "$ErrorActionPreference = 'Continue'" in body, "the probe must relax it"
    assert "2>&1" in body, "stderr must be captured, not left to raise"


def test_store_stub_is_rejected():
    body = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "WindowsApps" in body, "the Store alias directory must be excluded"
    assert "'^\\d+\\.\\d+$'" in body or '^\\d+\\.\\d+$' in body, (
        "a real interpreter prints a version; the stub prints a sentence"
    )


def test_every_path_match_is_tried_not_just_the_first():
    """Get-Command returns only the first match. The Store stub routinely
    shadows a real interpreter installed later, so without -All a machine
    with working Python reports having none."""
    body = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "-All" in body, "Get-Command must enumerate every match on PATH"


def test_known_install_directories_are_searched_as_a_fallback():
    body = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "Programs\\Python" in body
    assert "Python3*" in body


def test_installer_continues_after_installing_python():
    """Telling someone to close the window and start over is where they drop
    out. winget puts Python where the directory scan already looks, so the
    run can simply carry on."""
    body = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "function Find-Python" in body, "detection must be reusable"
    assert "function Update-PathFromRegistry" in body, (
        "a process inherits PATH at launch; a just-installed interpreter is "
        "invisible until PATH is re-read"
    )

    after_winget = body[body.index("winget install"):]
    assert "$python = Find-Python" in after_winget, "must look again, not give up"
    assert "Continuing with the install" in after_winget

    # The old behaviour was an unconditional exit straight after installing.
    assert "exit 0" not in after_winget[: after_winget.index("Continuing")], (
        "the installer must not stop once winget has finished"
    )


def test_winget_step_stays_behind_a_confirmation():
    """Installing software on someone else's machine is a visible yes/no."""
    body = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    assert "Confirm 'Install Python now using winget?'" in body
