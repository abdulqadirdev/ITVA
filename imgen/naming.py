"""Filename templates.

Lets the output be named for how it will be used. A storyboard assembled in a
video editor wants `001.jpg, 002.jpg` in sequence order; a review folder wants
the prompt in the name. Both come from the same template string.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass

# Characters no filesystem should be asked to carry. The Windows set is the
# strictest, so it is the one applied everywhere — a project generated on
# Linux should still open from a Windows share.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SLUG = re.compile(r"[^a-z0-9]+")

# Windows refuses these as filenames regardless of extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

DEFAULT_TEMPLATE = "{row:04}_{prompt}_{index}"

TOKENS: list[tuple[str, str]] = [
    ("{row}", "source row number — use {row:04} to zero-pad"),
    ("{seq}", "value of the sequence column, if the sheet has one"),
    ("{scene}", "value of the scene column, slugified"),
    ("{prompt}", "the prompt, slugified and shortened"),
    ("{index}", "image number within this prompt (1, 2, …)"),
    ("{n}", "running count across the whole project — use {n:04} to pad"),
    ("{date}", "YYYY-MM-DD"),
    ("{time}", "HHMMSS"),
    ("{project}", "project name, slugified"),
]


class TemplateError(ValueError):
    """The template refers to something that does not exist."""


@dataclass(frozen=True)
class NameContext:
    row: int = 1
    seq: str = ""
    scene: str = ""
    prompt: str = ""
    index: int = 1
    n: int = 1
    date: str = "2026-01-01"
    time: str = "120000"
    project: str = "project"

    def as_dict(self) -> dict:
        return {
            "row": self.row,
            # Numeric sequences must stay ints, or "{seq:04}" pads a string on
            # the RIGHT: "7" becomes "7000" instead of "0007".
            "seq": _as_number(self.seq) if self.seq else self.row,
            "scene": slugify(self.scene),
            "prompt": slugify(self.prompt, limit=48),
            "index": self.index,
            "n": self.n,
            "date": self.date,
            "time": self.time,
            "project": slugify(self.project),
        }


def _as_number(value: str):
    text = (value or "").strip()
    return int(text) if text.isdigit() else slugify(text)


def slugify(text: str, limit: int = 40) -> str:
    return _SLUG.sub("-", (text or "").lower()).strip("-")[:limit]


def sanitize(name: str) -> str:
    """Make a rendered name safe on every platform."""
    cleaned = _ILLEGAL.sub("-", name).strip(" .")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if cleaned.split(".")[0].lower() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:150] or "image"


class _Formatter(string.Formatter):
    """Reports an unknown token instead of raising KeyError at save time."""

    def get_value(self, key, args, kwargs):
        if isinstance(key, str) and key not in kwargs:
            known = ", ".join(token for token, _ in TOKENS)
            raise TemplateError(f"unknown token '{{{key}}}'. Available: {known}")
        return super().get_value(key, args, kwargs)


def render(template: str, context: NameContext) -> str:
    """Render one filename stem (no extension)."""
    if not template.strip():
        template = DEFAULT_TEMPLATE
    try:
        rendered = _Formatter().vformat(template, (), context.as_dict())
    except TemplateError:
        raise
    except (ValueError, IndexError, KeyError) as exc:
        raise TemplateError(f"could not apply the template: {exc}") from exc

    # A template may describe folders; sanitize each part, keep the structure.
    parts = [p for p in rendered.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        raise TemplateError("the template produced an empty name")
    return "/".join(sanitize(part) for part in parts)


def preview(template: str) -> str:
    """What the template produces for a representative row."""
    return (
        render(
            template,
            NameContext(
                row=7,
                seq="7",
                scene="2am hook",
                prompt="Modern person half-asleep in bed at night",
                index=1,
                n=7,
                project="Default",
            ),
        )
        + ".jpg"
    )
