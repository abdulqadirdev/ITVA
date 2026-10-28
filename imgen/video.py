"""Assembling generated images and narration into a video.

ffmpeg is driven directly rather than through a wrapper library. It is the
engine underneath every Python option anyway, and calling it ourselves keeps
the dependency list short and the bundle small — a 300 MB download is already
enough.

Each clip is rendered to its own file and the clips are then concatenated.
That costs a little disk but buys a lot: per-clip progress, a render that can
resume, per-scene exports for free, and a failure that names the clip that
caused it instead of collapsing one enormous filter graph.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

# Frame naming lives with the renderer that writes them.
from .animate import FRAME_GLOB, FRAME_PATTERN
from .captions import Caption, write_srt
from .subtitles import CaptionStyle, write_ass
from .platforms import find_ffmpeg, find_ffprobe


class VideoError(RuntimeError):
    """Rendering could not start or could not finish."""


@dataclass
class Clip:
    row: int
    image: Path
    text: str
    scene: str = ""
    audio: Path | None = None
    duration: float = 4.0
    #: (start, duration, word) from the speech engine, for exact karaoke.
    words: tuple = ()
    #: A folder of f00000.png frames to play instead of the still picture,
    #: for a row whose animation column names an action. Everything else -
    #: captions, narration, length, transitions - is unchanged, so an
    #: animated row and a still row concatenate without special cases.
    frames: Path | None = None


@dataclass
class RenderSettings:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    burn_captions: bool = True
    caption_style: CaptionStyle = field(default_factory=CaptionStyle)
    #: none | fade | crossfade
    transition: str = "fade"
    transition_s: float = 0.4
    ken_burns: bool = True
    zoom_strength: float = 0.12
    padding_s: float = 0.35
    crossfade_s: float = 0.0
    audio_bitrate: str = "192k"
    crf: int = 20
    preset: str = "medium"
    #: Background music mixed under the whole video, looped to fit.
    music: str = ""
    music_db: float = -20.0
    #: Dip the music while someone is speaking.
    ducking: bool = True
    music_fade_s: float = 2.0
    ffmpeg: str = ""
    ffprobe: str = ""


# ---- tool discovery ------------------------------------------------------

def require_tools(settings: RenderSettings) -> tuple[Path, Path]:
    ffmpeg = find_ffmpeg(settings.ffmpeg)
    ffprobe = find_ffprobe(settings.ffprobe)
    if not ffmpeg or not ffprobe:
        raise VideoError(
            "ffmpeg was not found. It is the engine that builds the video.\n"
            "  Linux:   sudo apt install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "Or set video.ffmpeg in settings to its full path."
        )
    return ffmpeg, ffprobe


def probe_duration(ffprobe: Path, media: Path) -> float:
    """Length of an audio or video file, in seconds."""
    result = subprocess.run(
        [
            str(ffprobe), "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(media),
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise VideoError(f"could not read the length of {media.name}: {result.stderr.strip()}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (ValueError, KeyError) as exc:
        raise VideoError(f"unexpected ffprobe output for {media.name}") from exc


# ---- filter construction -------------------------------------------------

def _escape_for_filter(path: Path) -> str:
    """ffmpeg filter arguments treat ':' and '\\' specially."""
    text = str(path)
    for char in ("\\", ":", "'", "[", "]", ","):
        text = text.replace(char, f"\\{char}")
    return text


def frame_align(duration: float, fps: int) -> float:
    """Snap a duration onto the frame grid.

    A clip's picture is a whole number of frames, so asking for 3.278s at
    30fps gives 3.267s of video. Leaving the audio, the caption and the
    sidecar .srt on the unrounded figure puts each of them a few
    milliseconds out of step with the picture, in every clip, in the same
    direction - which is what makes it visible by the end rather than at
    the start.
    """
    return max(round(duration * fps), 1) / fps


def build_filter(clip: Clip, settings: RenderSettings, index: int, srt: Path | None) -> str:
    """Scale, optionally Ken Burns, optionally burn captions."""
    w, h, fps = settings.width, settings.height, settings.fps
    parts = []

    if clip.frames is not None:
        # The frames already move; panning them as well would fight the
        # animation, and zoompan would resample every one of them.
        parts.append(
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps={fps}"
        )
    elif settings.ken_burns:
        # Upscale before zooming: zoompan steps in whole source pixels, so
        # zooming a 1080p still directly produces visible judder.
        parts.append(
            f"scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,"
            f"crop={w * 2}:{h * 2}"
        )
        frames = max(int(round(clip.duration * fps)), 1)
        # Alternate the direction so a long sequence does not feel mechanical.
        if index % 2 == 0:
            zoom = f"min(zoom+{settings.zoom_strength / frames:.8f},{1 + settings.zoom_strength})"
        else:
            zoom = f"max({1 + settings.zoom_strength}-on*{settings.zoom_strength / frames:.8f},1.0)"
        parts.append(
            f"zoompan=z='{zoom}':d={frames}"
            ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={w}x{h}:fps={fps}"
        )
    else:
        parts.append(
            f"scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},fps={fps}"
        )

    if srt is not None:
        # ASS rather than SRT: the styling and the animation travel with the
        # text, which is the only way to get colours, a box or a per-line
        # effect out of libass.
        parts.append(f"ass='{_escape_for_filter(srt)}'")

    # Not for "crossfade": the overlap does the work, and dipping to black
    # underneath it would show through the blend.
    if settings.transition == "fade" and settings.transition_s > 0:
        # A dip at each end reads as a transition while still letting the
        # clips be concatenated without re-encoding.
        fade = min(settings.transition_s, max(clip.duration / 3, 0.05))
        parts.append(f"fade=t=in:st=0:d={fade:.2f}")
        parts.append(f"fade=t=out:st={max(clip.duration - fade, 0):.2f}:d={fade:.2f}")

    parts.append("format=yuv420p")
    return ",".join(parts)


# ---- rendering -----------------------------------------------------------

def render_clip(
    clip: Clip,
    destination: Path,
    settings: RenderSettings,
    ffmpeg: Path,
    index: int = 0,
    work_dir: Path | None = None,
) -> Path:
    """One image plus its narration, as a self-contained mp4."""
    if clip.frames is None and not clip.image.exists():
        raise VideoError(f"row {clip.row}: image is missing ({clip.image})")
    if clip.frames is not None and not any(clip.frames.glob(FRAME_GLOB)):
        raise VideoError(f"row {clip.row}: no animation frames in {clip.frames}")

    # Everything below - the caption's end, the fade, the zoompan frame
    # count and -t - has to agree on one number, or they disagree by a
    # fraction of a frame each time.
    clip = replace(clip, duration=frame_align(clip.duration, settings.fps))

    work_dir = work_dir or destination.parent
    work_dir.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)

    srt = None
    if settings.burn_captions and clip.text.strip():
        # A per-clip subtitle file keeps timings local, so the same clip can
        # be reused in a scene export without its captions drifting.
        srt = work_dir / f"{destination.stem}.ass"
        style = replace(
            settings.caption_style, width=settings.width, height=settings.height
        )
        write_ass([Caption(0.0, clip.duration, clip.text, clip.words)], srt, style)

    # Every input must be declared before the filter and map options.
    # Interleaving them makes ffmpeg read "-f lavfi" as an *output* option,
    # which fails with nothing more helpful than "Invalid argument".
    has_audio = bool(clip.audio and clip.audio.exists())
    if clip.frames is not None:
        command = [
            str(ffmpeg), "-y",
            "-framerate", str(settings.fps),
            "-i", str(clip.frames / FRAME_PATTERN),
        ]
    else:
        command = [str(ffmpeg), "-y", "-loop", "1", "-i", str(clip.image)]
    if has_audio:
        command += ["-i", str(clip.audio)]
    else:
        # Silence rather than no track at all, so every clip has the same
        # stream layout and concat can copy instead of re-encoding.
        command += [
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        ]

    command += [
        # apad runs the narration out to the end of the picture. Without it
        # the audio stream is only as long as the speech, so on concat the
        # next clip's voice starts where the last voice ended rather than
        # where its own picture starts, and the whole soundtrack creeps
        # ahead by the padding - every clip, in the same direction.
        # The aformat is for the same reason: a 24kHz mono narration and a
        # 48kHz stereo silence cannot be concatenated without re-encoding.
        "-filter_complex",
        f"[0:v]{build_filter(clip, settings, index, srt)}[v];"
        f"[1:a]apad,aformat=sample_fmts=fltp:sample_rates=48000"
        f":channel_layouts=stereo[a]",
        "-map", "[v]",
        "-map", "[a]",
        "-c:a", "aac", "-b:a", settings.audio_bitrate, "-ar", "48000", "-ac", "2",
        "-t", f"{clip.duration:.3f}",
        "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
        "-pix_fmt", "yuv420p", "-r", str(settings.fps),
        str(destination),
    ]

    result = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if result.returncode != 0 or not destination.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise VideoError(f"row {clip.row}: ffmpeg failed\n{tail}")
    return destination


def crossfade(
    parts: list[Path],
    durations: list[float],
    destination: Path,
    settings: RenderSettings,
    ffmpeg: Path,
) -> Path:
    """Join clips with a true overlap.

    Unlike the plain join this re-encodes, because the frames at each seam
    are a blend of two clips and cannot simply be copied. The filter graph
    grows with the number of clips, so a long sequence is slow and memory
    hungry - the caller decides whether that is worth it.
    """
    if len(parts) < 2:
        raise VideoError("a crossfade needs at least two clips")

    overlap = crossfade_overlap(durations, settings)
    inputs: list[str] = []
    for part in parts:
        inputs += ["-i", str(part)]

    video_steps, audio_steps = [], []
    last_v, last_a, offset = "[0:v]", "[0:a]", 0.0
    for index in range(1, len(parts)):
        offset += durations[index - 1] - overlap
        out_v, out_a = f"[v{index}]", f"[a{index}]"
        video_steps.append(
            f"{last_v}[{index}:v]xfade=transition=fade:"
            f"duration={overlap:.3f}:offset={offset:.3f}{out_v}"
        )
        audio_steps.append(f"{last_a}[{index}:a]acrossfade=d={overlap:.3f}{out_a}")
        last_v, last_a = out_v, out_a

    graph = ";".join(video_steps + audio_steps)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(ffmpeg), "-y", *inputs,
            "-filter_complex", graph,
            "-map", last_v, "-map", last_a,
            "-c:v", "libx264", "-preset", settings.preset, "-crf", str(settings.crf),
            "-pix_fmt", "yuv420p", "-r", str(settings.fps),
            "-c:a", "aac", "-b:a", settings.audio_bitrate,
            str(destination),
        ],
        capture_output=True, text=True, timeout=3600,
    )
    if result.returncode != 0 or not destination.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise VideoError(f"crossfading failed\n{tail}")
    return destination


#: Beyond this many clips the crossfade filter graph becomes impractical.
CROSSFADE_LIMIT = 60


def crossfade_overlap(durations: list[float], settings: RenderSettings) -> float:
    """How much each transition eats, by the same rule crossfade() uses."""
    return max(min(settings.transition_s, min(durations) / 2), 0.05)


def will_crossfade(count: int, settings: RenderSettings) -> bool:
    """Whether join() is going to crossfade, decided in one place.

    The sidecar .srt has to be written against the video that is actually
    produced. Working it out separately put the subtitles 7.5s ahead of a
    one-minute video, because every transition overlaps two clips and the
    caption cursor knew nothing about it.
    """
    return settings.transition == "crossfade" and 2 <= count <= CROSSFADE_LIMIT


def join(
    parts: list[Path],
    durations: list[float],
    destination: Path,
    settings: RenderSettings,
    ffmpeg: Path,
    work_dir: Path,
    log=print,
) -> Path:
    """Join clips the way the chosen transition requires."""
    if settings.transition == "crossfade" and len(parts) >= 2:
        if len(parts) > CROSSFADE_LIMIT:
            log(
                f"  ! crossfade needs one filter step per clip and there are "
                f"{len(parts)}; falling back to the per-clip fade"
            )
        else:
            log(f"  crossfading {len(parts)} clips (re-encodes, slower)")
            return crossfade(parts, durations, destination, settings, ffmpeg)
    return concat(parts, destination, ffmpeg, work_dir)


def concat(parts: list[Path], destination: Path, ffmpeg: Path, work_dir: Path) -> Path:
    """Join rendered clips without re-encoding."""
    if not parts:
        raise VideoError("there is nothing to join")

    work_dir.mkdir(parents=True, exist_ok=True)
    listing = work_dir / f"{destination.stem}-parts.txt"
    listing.write_text(
        "\n".join(f"file '{p.resolve()}'" for p in parts) + "\n", encoding="utf-8"
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            str(ffmpeg), "-y", "-f", "concat", "-safe", "0",
            "-i", str(listing), "-c", "copy", str(destination),
        ],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0 or not destination.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise VideoError(f"joining the clips failed\n{tail}")
    return destination


def add_music(
    video: Path,
    music: Path,
    destination: Path,
    settings: RenderSettings,
    ffmpeg: Path,
) -> Path:
    """Lay a music bed under a finished video.

    With ducking the music is compressed against the narration itself, so it
    steps back whenever anyone speaks and returns in the gaps - the thing
    that separates a video with music from a video you cannot hear.
    """
    if not music.exists():
        raise VideoError(f"music file not found: {music}")

    loop_and_level = (
        f"[1:a]volume={settings.music_db}dB,"
        f"afade=t=in:st=0:d={settings.music_fade_s}[bed]"
    )
    if settings.ducking:
        chain = (
            f"{loop_and_level};"
            # One copy of the narration drives the compressor, the other is
            # heard. A sidechain cannot both be an input and an output.
            "[0:a]asplit=2[voice][key];"
            "[bed][key]sidechaincompress="
            "threshold=0.015:ratio=12:attack=15:release=450:makeup=1[ducked];"
            "[voice][ducked]amix=inputs=2:duration=first:normalize=0[out]"
        )
    else:
        chain = f"{loop_and_level};[0:a][bed]amix=inputs=2:duration=first:normalize=0[out]"

    result = subprocess.run(
        [
            str(ffmpeg), "-y",
            "-i", str(video),
            "-stream_loop", "-1", "-i", str(music),
            "-filter_complex", chain,
            "-map", "0:v", "-map", "[out]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", settings.audio_bitrate,
            "-shortest", str(destination),
        ],
        capture_output=True, text=True, timeout=1800,
    )
    if result.returncode != 0 or not destination.exists():
        tail = "\n".join(result.stderr.strip().splitlines()[-6:])
        raise VideoError(f"adding music failed\n{tail}")
    return destination


@dataclass
class RenderResult:
    video: Path
    srt: Path
    scenes: dict[str, Path] = field(default_factory=dict)
    clips: int = 0
    seconds: float = 0.0


def render(
    clips: list[Clip],
    output_dir: Path,
    name: str,
    settings: RenderSettings,
    per_scene: bool = True,
    log: Callable[[str], None] = print,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> RenderResult:
    """Render every clip, then the full video and optionally each scene."""
    if not clips:
        raise VideoError("no finished rows with images to build a video from")

    ffmpeg, _ = require_tools(settings)
    # One source of truth for every clip's length: the sidecar .srt, the
    # crossfade offsets and the rendered clips must all be told the same
    # thing, and that thing has to be what the picture will actually be.
    clips = [replace(c, duration=frame_align(c.duration, settings.fps)) for c in clips]
    work_dir = output_dir / "video-parts"
    work_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[Path] = []
    for index, clip in enumerate(clips):
        if should_stop():
            log("  stop requested")
            break
        target = work_dir / f"{index:05d}_row{clip.row}.mp4"
        log(f"  [{index + 1}/{len(clips)}] row {clip.row} ({clip.duration:.1f}s)")
        render_clip(clip, target, settings, ffmpeg, index=index, work_dir=work_dir)
        rendered.append(target)
        if on_progress:
            on_progress(index + 1, len(clips))

    if not rendered:
        raise VideoError("no clips were rendered")

    # The sidecar subtitle file always covers the whole video, with running
    # timings, whether or not captions were burned into the picture.
    shown = clips[: len(rendered)]
    lengths = [c.duration for c in shown]
    overlap = (
        crossfade_overlap(lengths, settings)
        if will_crossfade(len(rendered), settings)
        else 0.0
    )
    captions, cursor = [], 0.0
    for clip in shown:
        captions.append(
            Caption(
                cursor,
                cursor + clip.duration,
                clip.text,
                tuple((s + cursor, d, w) for s, d, w in clip.words),
            )
        )
        # A crossfade plays the next clip over the tail of this one, so the
        # timeline advances by less than the clip is long.
        cursor += clip.duration - overlap
    # Trim each cue where the next one starts: during an overlap both
    # pictures are on screen, but two subtitles at once are not wanted.
    captions = [
        replace(cue, end=min(cue.end, nxt.start)) if cue.end > nxt.start else cue
        for cue, nxt in zip(captions, captions[1:], strict=False)
    ] + captions[-1:]
    srt = write_srt(captions, output_dir / f"{name}.srt")

    log("  joining clips")
    video = join(
        rendered, lengths, output_dir / f"{name}.mp4", settings, ffmpeg, work_dir, log
    )

    if settings.music:
        music = Path(settings.music).expanduser()
        if music.exists():
            log(f"  adding music{' with ducking' if settings.ducking else ''}")
            with_music = work_dir / f"{name}-music.mp4"
            add_music(video, music, with_music, settings, ffmpeg)
            with_music.replace(video)
        else:
            log(f"  ! music file not found, skipped: {music}")

    scenes: dict[str, Path] = {}
    if per_scene:
        grouped: dict[str, list[Path]] = {}
        # Truncation is intended: a stopped run has fewer rendered parts
        # than clips, and the scenes should cover only what exists.
        for clip, part in zip(clips, rendered, strict=False):
            grouped.setdefault(clip.scene or "unsorted", []).append(part)
        if len(grouped) > 1:
            scene_dir = output_dir / "scenes"
            by_scene_len: dict[str, list[float]] = {}
            for clip, _part in zip(clips, rendered, strict=False):
                by_scene_len.setdefault(clip.scene or "unsorted", []).append(clip.duration)
            for scene, parts in grouped.items():
                safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in scene)[:48]
                log(f"  scene '{scene}' ({len(parts)} clips)")
                scenes[scene] = join(
                    parts, by_scene_len.get(scene, []), scene_dir / f"{safe}.mp4",
                    settings, ffmpeg, work_dir, log,
                )

    return RenderResult(
        video=video,
        srt=srt,
        scenes=scenes,
        clips=len(rendered),
        seconds=cursor,
    )
