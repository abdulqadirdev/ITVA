"""Turning a script line into spoken audio.

Two engines on purpose. edge-tts sounds better and needs no key, but it is an
online service we do not control — the same kind of dependency as Flow
itself. piper runs entirely offline from a local model, so a run can finish
when that service is unavailable or the machine is offline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EDGE_VOICE = "en-US-AndrewNeural"


class TtsError(RuntimeError):
    """Speech could not be synthesised."""


@dataclass(frozen=True)
class WordTime:
    """When a single word is spoken, in seconds from the start."""

    start: float
    duration: float
    text: str

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Speech:
    path: Path
    words: list[WordTime]


@dataclass(frozen=True)
class VoiceOption:
    engine: str
    name: str            # e.g. en-US-AndrewNeural
    label: str
    locale: str = ""     # en-US
    language: str = ""   # English (United States)
    gender: str = ""
    categories: tuple[str, ...] = ()      # General, News, Cartoon, ...
    personalities: tuple[str, ...] = ()   # Friendly, Cheerful, ...

    @property
    def short(self) -> str:
        """The part of the name that is not the locale, e.g. "Andrew"."""
        tail = self.name.split("-")[-1]
        return tail[:-6] if tail.endswith("Neural") else tail

    def describes(self, text: str) -> bool:
        haystack = " ".join(
            (self.name, self.language, self.gender, *self.categories, *self.personalities)
        ).casefold()
        return all(word in haystack for word in text.casefold().split())


# ---- engine availability -------------------------------------------------

def edge_available() -> bool:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        return False
    return True


def piper_available(model_path: str) -> bool:
    if not model_path:
        return False
    try:
        import piper  # noqa: F401
    except ImportError:
        return False
    return Path(model_path).expanduser().exists()


# ---- synthesis -----------------------------------------------------------

def _signed(value: int, unit: str = "%") -> str:
    """edge-tts wants a signed offset, e.g. '+10%' or '-5Hz'."""
    return f"{value:+d}{unit}"


def _synthesise_edge(
    text: str, destination: Path, voice: str, rate: int, pitch: int, volume: int
) -> list[WordTime]:
    """Speak the text and report when each word lands.

    Streaming rather than .save() so the word boundaries come back with the
    audio. They are what lets a caption highlight follow the voice exactly
    instead of being estimated from word length.
    """
    import edge_tts

    words: list[WordTime] = []

    async def run() -> None:
        communicate = edge_tts.Communicate(
            text,
            voice or DEFAULT_EDGE_VOICE,
            rate=_signed(rate),
            pitch=_signed(pitch, "Hz"),
            volume=_signed(volume),
            boundary="WordBoundary",
        )
        with destination.open("wb") as handle:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    # Reported in 100-nanosecond ticks.
                    words.append(
                        WordTime(
                            start=chunk["offset"] / 1e7,
                            duration=chunk["duration"] / 1e7,
                            text=chunk["text"],
                        )
                    )

    try:
        # A fresh loop each time: this runs on a worker thread, which has
        # none of its own, and asyncio.run tears it down cleanly afterwards.
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - network, service, or audio error
        raise TtsError(f"edge-tts failed: {exc}") from exc

    if not destination.exists() or destination.stat().st_size == 0:
        raise TtsError("edge-tts produced an empty file")
    return words


def _synthesise_piper(text: str, destination: Path, model_path: str) -> None:
    """Speak offline from a local piper model.

    piper yields audio in chunks rather than writing a file, and only the
    first chunk reports the format - so the wave header is set from it and
    the rest are appended.
    """
    import wave

    from piper import PiperVoice

    try:
        voice = PiperVoice.load(str(Path(model_path).expanduser()))
        with wave.open(str(destination), "wb") as output:
            configured = False
            for chunk in voice.synthesize(text):
                if not configured:
                    output.setnchannels(getattr(chunk, "sample_channels", 1))
                    output.setsampwidth(getattr(chunk, "sample_width", 2))
                    output.setframerate(
                        getattr(chunk, "sample_rate", voice.config.sample_rate)
                    )
                    configured = True
                output.writeframes(chunk.audio_int16_bytes)
            if not configured:
                raise TtsError("piper produced no audio")
    except TtsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"piper failed: {exc}") from exc

    if not destination.exists() or destination.stat().st_size == 0:
        raise TtsError("piper produced an empty file")


def synthesise(
    text: str,
    destination: Path,
    engine: str = "edge",
    voice: str = DEFAULT_EDGE_VOICE,
    rate: int = 0,
    pitch: int = 0,
    volume: int = 0,
    piper_model: str = "",
    log=print,
) -> Speech:
    """Speak `text` into `destination`, falling back when the first choice fails.

    Returns the file actually written and, where the engine reports them, the
    time of every word. The extension changes with the engine (mp3 from edge,
    wav from piper), so the caller must use what comes back.
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("nothing to say: the script line is empty")

    destination.parent.mkdir(parents=True, exist_ok=True)
    order = ["edge", "piper"] if engine != "piper" else ["piper", "edge"]

    problems = []
    for choice in order:
        if choice == "edge" and edge_available():
            target = destination.with_suffix(".mp3")
            try:
                words = _synthesise_edge(text, target, voice, rate, pitch, volume)
                return Speech(target, words)
            except TtsError as exc:
                problems.append(str(exc))
                if engine == "edge":
                    log(f"      {exc}; trying the offline voice")
        elif choice == "piper" and piper_available(piper_model):
            target = destination.with_suffix(".wav")
            try:
                _synthesise_piper(text, target, piper_model)
                # piper reports no boundaries; captions fall back to estimates.
                return Speech(target, [])
            except TtsError as exc:
                problems.append(str(exc))

    detail = "; ".join(problems) if problems else "no speech engine is available"
    raise TtsError(
        f"{detail}. edge-tts needs an internet connection; piper needs a model "
        "file set in Settings."
    )


def list_edge_voices(locale_prefix: str = "") -> list[VoiceOption]:
    """Every voice the service offers, with the metadata needed to group them.

    Returns an empty list when offline, which is a normal state rather than
    an error: the voice already configured still works.
    """
    if not edge_available():
        return []
    import edge_tts

    try:
        voices = asyncio.run(edge_tts.list_voices())
    except Exception:  # noqa: BLE001 - offline is not an error here
        return []

    options = []
    for v in voices:
        name = v.get("ShortName", "")
        if locale_prefix and not name.startswith(locale_prefix):
            continue
        tag = v.get("VoiceTag") or {}
        options.append(
            VoiceOption(
                engine="edge",
                name=name,
                label=f"{name}  ({v.get('Gender', '')})",
                locale=v.get("Locale", ""),
                language=v.get("LocaleName", v.get("Locale", "")),
                gender=v.get("Gender", ""),
                # The service returns some of these with stray whitespace,
                # which otherwise shows up as a duplicate category.
                categories=tuple(sorted({c.strip() for c in (tag.get("ContentCategories") or ()) if c.strip()})),
                personalities=tuple(sorted({p.strip() for p in (tag.get("VoicePersonalities") or ()) if p.strip()})),
            )
        )
    return sorted(options, key=lambda o: (o.language, o.gender, o.name))


def group_by_language(options: list[VoiceOption]) -> dict[str, list[VoiceOption]]:
    grouped: dict[str, list[VoiceOption]] = {}
    for option in options:
        grouped.setdefault(option.language or option.locale, []).append(option)
    return dict(sorted(grouped.items()))


def all_categories(options: list[VoiceOption]) -> list[str]:
    found = {c for o in options for c in o.categories}
    return sorted(found)


def all_personalities(options: list[VoiceOption]) -> list[str]:
    found = {p for o in options for p in o.personalities}
    return sorted(found)


def preview_text(option: VoiceOption) -> str:
    """A line to audition a voice with, in a language it can actually read."""
    samples = {
        "ur": "\u06cc\u06c1 \u0622\u0648\u0627\u0632 \u06a9\u06cc\u0633\u06cc \u0644\u06af \u0631\u06c1\u06cc \u06c1\u06d2\u061f",
        "hi": "\u092f\u0939 \u0906\u0935\u093e\u091c\u093c \u0915\u0948\u0938\u0940 \u0932\u0917 \u0930\u0939\u0940 \u0939\u0948?",
        "ar": "\u0643\u064a\u0641 \u064a\u0628\u062f\u0648 \u0647\u0630\u0627 \u0627\u0644\u0635\u0648\u062a\u061f",
        "es": "Asi es como suena esta voz.",
        "fr": "Voici a quoi ressemble cette voix.",
    }
    return samples.get(
        (option.locale or "en").split("-")[0],
        "It's 2am. You're finally drifting off.",
    )
