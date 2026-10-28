"""Emotion without a model that can act.

The expressive engines that take tags like [laughs] are billion-parameter
models; on a laptop with no GPU they take minutes per line. What does work,
instantly and offline, is shaping delivery: pace, pitch and loudness carry
most of what a listener reads as mood, and picking a voice whose own
personality already leans that way does the rest.

It is not acting. It is enough that a sad line sounds slower and lower than
an angry one, which is most of the effect in a narrated explainer.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery:
    """Offsets applied on top of the project's own rate, pitch and volume."""

    rate: int = 0        # percent
    pitch: int = 0       # Hz
    volume: int = 0      # percent
    #: Voice personalities that suit this mood, used when auto-picking.
    traits: tuple[str, ...] = ()
    hint: str = ""


EMOTIONS: dict[str, Delivery] = {
    "neutral": Delivery(hint="as written"),
    "happy": Delivery(8, 6, 4, ("Cheerful", "Friendly", "Positive"), "lighter and a little quicker"),
    "excited": Delivery(18, 12, 10, ("Cheerful", "Bright", "Lively"), "fast, high, loud"),
    "sad": Delivery(-14, -8, -8, ("Sad", "Gentle", "Warm"), "slow, low, quiet"),
    "angry": Delivery(10, -5, 14, ("Authority", "Confident", "Serious"), "hard and loud, pitched down"),
    "calm": Delivery(-8, -3, -4, ("Calm", "Gentle", "Soothing"), "unhurried"),
    "serious": Delivery(-5, -6, 2, ("Serious", "Authority", "Confident"), "measured and low"),
    "curious": Delivery(3, 7, 0, ("Friendly", "Casual"), "lifted, questioning"),
    "tired": Delivery(-18, -9, -10, ("Gentle", "Warm"), "dragging and flat"),
    "scared": Delivery(14, 9, -6, ("Gentle",), "quick, high, held back"),
    "whisper": Delivery(-10, -2, -45, ("Gentle", "Soothing"), "very quiet"),
    "shout": Delivery(12, 6, 25, ("Authority", "Confident"), "loud and forward"),
}

#: Words people actually type in a sheet, mapped to the real name.
ALIASES = {
    "joy": "happy", "joyful": "happy", "cheerful": "happy", "glad": "happy",
    "sorrow": "sad", "crying": "sad", "upset": "sad", "down": "sad",
    "mad": "angry", "furious": "angry", "annoyed": "angry", "irritated": "angry",
    "laugh": "happy", "laughing": "happy",
    "fear": "scared", "afraid": "scared", "nervous": "scared", "anxious": "scared",
    "quiet": "whisper", "soft": "whisper", "whispering": "whisper",
    "loud": "shout", "shouting": "shout", "yelling": "shout",
    "relaxed": "calm", "gentle": "calm", "soothing": "calm",
    "formal": "serious", "stern": "serious",
    "sleepy": "tired", "exhausted": "tired", "bored": "tired",
    "wondering": "curious", "questioning": "curious",
    "normal": "neutral", "": "neutral", "none": "neutral",
}


def names() -> list[str]:
    return list(EMOTIONS)


def resolve(name: str) -> Delivery:
    """Look up a mood, tolerating whatever the sheet happens to say."""
    key = (name or "").strip().casefold()
    key = ALIASES.get(key, key)
    return EMOTIONS.get(key, EMOTIONS["neutral"])


def apply(
    name: str, rate: int = 0, pitch: int = 0, volume: int = 0
) -> tuple[int, int, int]:
    """Combine a mood with the project's own settings, kept within range.

    edge-tts rejects extremes, and they sound wrong long before they are
    rejected, so the result is clamped rather than passed through.
    """
    delivery = resolve(name)
    return (
        max(-50, min(100, rate + delivery.rate)),
        max(-50, min(50, pitch + delivery.pitch)),
        max(-90, min(100, volume + delivery.volume)),
    )
