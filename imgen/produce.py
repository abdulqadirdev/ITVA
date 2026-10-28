"""From a finished project to a finished video.

Three steps, kept separate so each can be re-run on its own: narrate every
row, measure the narration to get the timing, then render. Narration is the
slow, network-bound part and is cached on the row, so changing a caption
style or the resolution does not re-synthesise 152 voice clips.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .config import Config
from .db import Job, JobStore
from . import emotion as mood
from .naming import slugify
from .platforms import find_ffmpeg
from .speechkit import (
    apply_pronunciation,
    available_nonverbals,
    load_pronunciation,
    splice,
    split_tags,
)
from .tts import synthesise
from .subtitles import CaptionStyle
from .video import (
    Clip,
    RenderResult,
    RenderSettings,
    frame_align,
    probe_duration,
    render,
    require_tools,
)

Logger = Callable[[str], None]
Progress = Callable[[int, int], None]

SCRIPT_KEYS = ("script line", "script", "narration", "voiceover", "caption")


def script_of(job: Job, explicit: str = "") -> str:
    """The line to speak and caption.

    Falls back through the names people actually use for that column before
    giving up, because the sheet is the user's, not ours.
    """
    fields = {k.casefold(): v for k, v in (job.fields or {}).items()}
    if explicit and fields.get(explicit.casefold()):
        return fields[explicit.casefold()].strip()
    for key in SCRIPT_KEYS:
        if fields.get(key):
            return fields[key].strip()
    return ""


def field_of(job: Job, column: str) -> str:
    fields = {k.casefold(): v for k, v in (job.fields or {}).items()}
    return (fields.get(column.casefold()) or "").strip()


def order_key(job: Job, sequence_column: str) -> tuple:
    """Storyboard order: the sequence column when it is numeric, else the row."""
    raw = (job.fields or {}).get(sequence_column.casefold()) or (job.fields or {}).get(
        sequence_column
    )
    if raw and str(raw).strip().isdigit():
        return (0, int(str(raw).strip()))
    return (1, job.row_number)


def settings_from(cfg: Config) -> RenderSettings:
    v = cfg.video
    style = CaptionStyle.from_preset(
        v.caption_preset,
        font=v.caption_font, size=v.caption_size,
        primary=v.caption_colour, outline=v.caption_outline,
        highlight=v.caption_highlight, box=v.caption_box,
        outline_px=v.caption_outline_px, position=v.caption_position,
        margin_v=v.caption_margin, animation=v.caption_animation,
        width=v.width, height=v.height,
    )
    return RenderSettings(
        width=v.width, height=v.height, fps=v.fps,
        burn_captions=v.burn_captions, caption_style=style,
        transition=v.transition, transition_s=v.transition_s,
        music=v.music, music_db=v.music_db, ducking=v.ducking,
        music_fade_s=v.music_fade_s,
        ken_burns=v.ken_burns, zoom_strength=v.zoom_strength,
        padding_s=v.padding_s, crf=v.crf, preset=v.preset,
        ffmpeg=v.ffmpeg, ffprobe=v.ffprobe,
    )


@dataclass
class Producer:
    cfg: Config
    log: Logger = print
    on_progress: Progress | None = None
    should_stop: Callable[[], bool] = lambda: False

    # ---- step 1: narration ----------------------------------------------

    def rows(self, store: JobStore) -> list[Job]:
        """Finished rows that still have their image, in storyboard order."""
        from .thumbnail import is_thumbnail_job

        usable = [
            job
            for job in store.done_with_images()
            # The thumbnail's picture is a queue job so it gets the same
            # Flow handling as everything else, but it is not a scene and
            # must never turn up as a clip.
            if any(Path(p).exists() for p in job.image_paths)
            and not is_thumbnail_job(job)
        ]
        return sorted(usable, key=lambda j: order_key(j, self.cfg.sheets.sequence_column))

    def narrate(self, store: JobStore, jobs: list[Job], force: bool = False) -> int:
        """Give every row narration. Returns how many were synthesised."""
        v = self.cfg.video
        audio_dir = self.cfg.run.output_dir / "audio"
        project_dir = self.cfg.run.output_dir.parent
        # Both live beside the project so they travel with it.
        say_as = load_pronunciation(project_dir / "pronunciation.txt")
        clips = available_nonverbals(project_dir / "nonverbals")
        if say_as:
            self.log(f"  {len(say_as)} pronunciation rule(s)")
        if clips:
            self.log(f"  nonverbal clips: {', '.join(sorted(clips))}")
        made = 0
        pending = [
            j for j in jobs
            if script_of(j) and (force or not (j.audio_path and Path(j.audio_path).exists()))
        ]
        if not pending:
            self.log("  narration is already up to date")
            return 0

        _, ffprobe = require_tools(settings_from(self.cfg))
        self.log(f"  narrating {len(pending)} row(s)")
        for index, job in enumerate(pending, start=1):
            if self.should_stop():
                self.log("  stop requested")
                break
            raw = script_of(job)
            # Tags come out before synthesis - no engine here can laugh -
            # and the clips are joined on afterwards.
            text, nonverbals = split_tags(raw, clips)
            text = apply_pronunciation(text, say_as)
            if not text:
                self.log(f"  ! row {job.row_number}: nothing left to speak")
                continue
            # A mood in the sheet reshapes pace, pitch and loudness; a voice
            # in the sheet overrides the project's, which is how one video
            # ends up with more than one speaker.
            feeling = field_of(job, self.cfg.sheets.emotion_column)
            rate, pitch, volume = mood.apply(feeling, v.rate, 0, 0)
            voice = field_of(job, self.cfg.sheets.voice_column) or v.voice

            try:
                speech = synthesise(
                    text,
                    audio_dir / f"{job.row_number:04d}_{slugify(text, 32)}",
                    engine=v.tts_engine, voice=voice,
                    rate=rate, pitch=pitch, volume=volume,
                    piper_model=v.piper_model, log=self.log,
                )
                audio_path = speech.path
                if nonverbals:
                    ffmpeg = find_ffmpeg(v.ffmpeg)
                    if ffmpeg:
                        audio_path = splice(
                            speech.path, nonverbals,
                            speech.path.with_name(speech.path.stem + "-full.mp3"),
                            ffmpeg,
                        )
                seconds = probe_duration(ffprobe, audio_path)
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
                self.log(f"  ! row {job.row_number}: {exc}")
                continue

            store.set_audio(
                job.id,
                str(audio_path),
                seconds,
                # Word timings describe the speech only; a spliced clip
                # shifts nothing before it, so they stay valid.
                [[w.start, w.duration, w.text] for w in speech.words],
            )
            made += 1
            suffix = f"  [{feeling}]" if feeling else ""
            self.log(f"  [{index}/{len(pending)}] row {job.row_number}: {seconds:.1f}s{suffix}")
            if self.on_progress:
                self.on_progress(index, len(pending))
        return made

    def nonverbal_clips(self) -> dict:
        return available_nonverbals(self.cfg.run.output_dir.parent / "nonverbals")

    def animation_of(self, job: Job) -> str:
        """The vector action this row plays, or "" for a still picture."""
        # Both sides folded: headers are stored as the sheet spells them,
        # so "Animation" in the sheet must still match the configured
        # "animation" - the same rule every other column here follows.
        fields = {k.casefold(): v for k, v in (job.fields or {}).items()}
        column = self.cfg.sheets.animation_column.casefold()
        return str(fields.get(column, "") or "").strip()

    def caption_text(self, job: Job) -> str:
        """What the caption shows.

        The original wording, minus any nonverbal tag that became a clip.
        Pronunciation fixes are deliberately NOT applied: "gee pee tee"
        belongs in the speech, not on the screen.
        """
        spoken, _ = split_tags(script_of(job), self.nonverbal_clips())
        return spoken

    # ---- step 2: timeline ------------------------------------------------

    def clips(self, jobs: list[Job]) -> list[Clip]:
        v = self.cfg.video
        built = []
        for job in jobs:
            image = next((Path(p) for p in job.image_paths if Path(p).exists()), None)
            if image is None:
                continue
            audio = Path(job.audio_path) if job.audio_path and Path(job.audio_path).exists() else None
            # The narration decides how long its picture stays up; that is
            # what keeps voice, caption and image in step with no manual timing.
            duration = (job.duration_s + v.padding_s) if (audio and job.duration_s) else v.fallback_duration_s
            built.append(
                Clip(
                    row=job.row_number,
                    image=image,
                    text=self.caption_text(job),
                    words=tuple(tuple(w) for w in (job.word_times or [])),
                    scene=(job.fields or {}).get(self.cfg.sheets.scene_column.casefold(), ""),
                    audio=audio,
                    duration=round(duration, 3),
                )
            )
        return built

    # ---- step 2b: vector animation ---------------------------------------

    def animate(self, jobs: list[Job], clips: list[Clip], work_dir: Path) -> int:
        """Render frames for every row whose animation column names an action.

        The frames become the clip's picture; captions, narration, length
        and transitions are untouched, so an animated row and a still row
        concatenate with no special case downstream.
        """
        if not self.cfg.video.animate_rows:
            return 0
        wanted = {j.row_number: self.animation_of(j) for j in jobs}
        todo = [c for c in clips if wanted.get(c.row)]
        if not todo:
            return 0

        from .animate import render_frames
        from .browser import BrowserSession
        from .stage import Shot, StageError

        v = self.cfg.video
        settings = settings_from(self.cfg)
        use_image = v.animation_backdrop.casefold() == "image"
        done = 0
        self.log(f"  animating {len(todo)} row(s) - this is the slow part")
        with BrowserSession(self.cfg.chrome) as session:
            for number, clip in enumerate(todo, 1):
                if self.should_stop():
                    self.log("  stop requested")
                    break
                action = wanted[clip.row]
                folder = work_dir / f"frames-row{clip.row:05d}"
                shot = Shot(
                    action=action,
                    duration=clip.duration,
                    width=settings.width,
                    height=settings.height,
                    framing=v.animation_framing,
                    camera=v.animation_camera,
                    backdrop=v.animation_backdrop,
                    image=clip.image if use_image else None,
                )
                try:
                    render_frames(
                        session, shot, folder,
                        fps=settings.fps,
                        log=lambda m: None,
                        should_stop=self.should_stop,
                    )
                except StageError as exc:
                    # One mistyped action must not cost a 150-row render.
                    self.log(f"  ! row {clip.row}: {exc} - keeping the still image")
                    continue
                clip.frames = folder
                done += 1
                self.log(f"  [{number}/{len(todo)}] row {clip.row}: {action}")
        return done

    # ---- step 3: render --------------------------------------------------

    def build(
        self,
        store: JobStore,
        name: str = "video",
        narrate_first: bool = True,
        limit: int | None = None,
    ) -> RenderResult:
        jobs = self.rows(store)
        if not jobs:
            raise RuntimeError(
                "no finished rows with images yet — generate some images first"
            )
        if limit:
            jobs = jobs[:limit]

        if narrate_first:
            self.narrate(store, jobs)
            jobs = [j for j in self.rows(store) if j.id in {x.id for x in jobs}]

        clips = self.clips(jobs)
        # Align before animating: the frame count and the clip's -t have to
        # come from the same number or the last frames are cut off.
        settings = settings_from(self.cfg)
        clips = [
            replace(c, duration=frame_align(c.duration, settings.fps)) for c in clips
        ]
        self.animate(jobs, clips, self.cfg.run.output_dir / "video-parts")
        silent = sum(1 for c in clips if c.audio is None)
        if silent:
            self.log(f"  {silent} clip(s) have no narration and use the fallback length")

        return render(
            clips,
            self.cfg.run.output_dir,
            name,
            settings_from(self.cfg),
            per_scene=self.cfg.video.per_scene,
            log=self.log,
            on_progress=self.on_progress,
            should_stop=self.should_stop,
        )
