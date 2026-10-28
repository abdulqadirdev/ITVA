"""Two small things that make narration sound deliberate.

A pronunciation list, because every script has a word the voice gets wrong
and no amount of re-rendering fixes it. And nonverbals, because a laugh or a
sigh cannot be synthesised by any engine that runs on this machine — but a
three-second recording can be spliced in, which gets the same effect.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: [laugh] or (sigh) anywhere in a script line.
TAG = re.compile(r"[\[(]\s*([a-zA-Z][a-zA-Z _-]{0,30})\s*[\])]")

AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".ogg", ".opus", ".flac")


# ---- pronunciation -------------------------------------------------------

def load_pronunciation(path: Path) -> dict[str, str]:
    """A written -> spoken mapping, one `written = spoken` per line.

    Deliberately spelling rather than phonemes: "GPT = gee pee tee" is
    something anyone can write and fix in seconds, and it works on every
    engine.
    """
    if not path.exists():
        return {}
    mapping: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        written, spoken = line.split("=", 1)
        written, spoken = written.strip(), spoken.strip()
        if written:
            mapping[written] = spoken
    return mapping


def apply_pronunciation(text: str, mapping: dict[str, str]) -> str:
    """Replace whole words only, longest first, ignoring case."""
    if not mapping or not text:
        return text
    for written in sorted(mapping, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(written)}(?!\w)", re.IGNORECASE)
        text = pattern.sub(mapping[written], text)
    return text


# ---- nonverbals ----------------------------------------------------------

@dataclass(frozen=True)
class Nonverbal:
    name: str
    path: Path


def available_nonverbals(folder: Path) -> dict[str, Path]:
    """Clips the project can splice in, keyed by their file name."""
    if not folder.exists():
        return {}
    found: dict[str, Path] = {}
    for item in sorted(folder.iterdir()):
        if item.is_file() and item.suffix.lower() in AUDIO_SUFFIXES:
            found.setdefault(item.stem.casefold(), item)
    return found


def split_tags(text: str, known: dict[str, Path]) -> tuple[str, list[Nonverbal]]:
    """Separate spoken words from the nonverbal tags in a line.

    An unknown tag is left in place rather than dropped: silently removing
    "[sigh]" when no clip exists would hide the fact that nothing happened,
    and leaving it means the voice says it, which is obvious.
    """
    found: list[Nonverbal] = []

    def replace(match: re.Match) -> str:
        key = match.group(1).strip().casefold().replace(" ", "-")
        clip = known.get(key) or known.get(key.replace("-", "_"))
        if clip is None:
            return match.group(0)
        found.append(Nonverbal(key, clip))
        return " "

    spoken = TAG.sub(replace, text)
    return " ".join(spoken.split()), found


def splice(
    speech: Path,
    clips: list[Nonverbal],
    destination: Path,
    ffmpeg: Path,
    before: bool = False,
) -> Path:
    """Join nonverbal clips onto the narration.

    Concatenated rather than mixed: a laugh over the top of a sentence
    sounds like two people, while a laugh after it sounds like one.
    """
    if not clips:
        return speech

    order = [c.path for c in clips] + [speech] if before else [speech] + [c.path for c in clips]
    inputs: list[str] = []
    for item in order:
        inputs += ["-i", str(item)]

    chain = "".join(f"[{i}:a]" for i in range(len(order)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(ffmpeg), "-y", *inputs,
            "-filter_complex",
            f"{chain}concat=n={len(order)}:v=0:a=1[out]",
            "-map", "[out]", str(destination),
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0 or not destination.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-4:])
        raise RuntimeError(f"could not add the nonverbal clip\n{tail}")
    return destination
