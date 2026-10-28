"""Normalise the shipped Windows scripts.

Windows PowerShell 5.1 decodes a BOM-less .ps1 with the system ANSI codepage,
where a UTF-8 em dash turns into a typographic quote that PowerShell accepts
as a real string delimiter. One such character in a comment can break a whole
script. So: ASCII only, CRLF, and a UTF-8 BOM on .ps1 files.

    python tools/fix_script_encoding.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PS_SCRIPTS = ("install.ps1", "uninstall.ps1")
OTHER = ("Install.bat",)
BOM = b"\xef\xbb\xbf"

SWAPS = {
    "—": "-", "–": "-",
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "…": "...", " ": " ",
    "→": "->",
}


def normalise(path: Path, want_bom: bool, check: bool) -> bool:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    for bad, good in SWAPS.items():
        text = text.replace(bad, good)

    leftover = sorted({c for c in text if ord(c) > 127})
    if leftover:
        print(f"  {path.name}: non-ASCII with no replacement: {leftover}")
        return False

    body = text.replace("\r\n", "\n").replace("\n", "\r\n").encode("ascii")
    wanted = (BOM if want_bom else b"") + body

    if raw == wanted:
        print(f"  {path.name}: ok")
        return True
    if check:
        print(f"  {path.name}: NEEDS FIXING")
        return False
    path.write_bytes(wanted)
    print(f"  {path.name}: rewritten ({len(wanted)} bytes)")
    return True


def main() -> int:
    check = "--check" in sys.argv
    ok = True
    for name in PS_SCRIPTS:
        ok &= normalise(ROOT / name, want_bom=True, check=check)
    for name in OTHER:
        ok &= normalise(ROOT / name, want_bom=False, check=check)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
