"""Driving Google Flow through the DOM to turn one prompt into images."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from enum import Enum

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

from .browser import BrowserSession, SignInState, first_match, sign_in_state, wait_first
from .config import Config

# Pulls the bytes of an image the page has already loaded. Works for blob:,
# data: and https: sources alike, and inherits the page's own credentials, so
# there is no cookie-domain juggling on our side.
_FETCH_AS_DATA_URL = """
async (src) => {
  const response = await fetch(src);
  if (!response.ok) throw new Error('HTTP ' + response.status);
  const blob = await response.blob();
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}
"""


class Outcome(str, Enum):
    DONE = "done"
    BLOCKED = "blocked"
    QUOTA = "quota"
    FAILED = "failed"


class SignedOutError(RuntimeError):
    """The automation profile lost its Flow session mid-run."""


class BrowserGone(RuntimeError):
    """The browser or its tab disappeared — the run can recover by relaunching."""


_GONE = ("target closed", "browser has been closed", "connection closed",
         "target page, context or browser has been closed", "websocket")


def is_browser_gone(exc: BaseException) -> bool:
    text = str(exc).casefold()
    return any(marker in text for marker in _GONE)


@dataclass
class GenerationResult:
    outcome: Outcome
    images: list[bytes] = field(default_factory=list)
    error: str | None = None


def open_flow(session: BrowserSession, cfg: Config) -> Page:
    """Land on a Flow *project*, which is the only surface with a prompt box.

    flow.google.com/ is the project list; an earlier version stopped there and
    reported the prompt box missing. Order of preference: a tab already inside
    a project, then the pinned project from settings, then the most recent
    existing project, and only as a last resort a new one — so a bulk run does
    not litter the account with empty projects.
    """
    page = session.page(cfg.flow.app_url, usable=cfg.flow.is_project)
    page.wait_for_load_state("domcontentloaded")

    if cfg.flow.looks_signed_out(page.url):
        return page  # caller reports the sign-out

    if not cfg.flow.is_project(page.url):
        if cfg.flow.project_url:
            page.goto(cfg.flow.project_url, wait_until="domcontentloaded")
        else:
            link = wait_first(page, cfg.selectors.get("project_link"), timeout_s=15)
            if link is not None:
                link.locator.click()
                page.wait_for_url("**/project/**", timeout=30000)
            else:
                new_project = first_match(page, cfg.selectors.get("new_project_button"))
                if new_project is not None:
                    new_project.locator.click()
                    page.wait_for_url("**/project/**", timeout=60000)

    # Flow is a single-page app: the URL changes well before ProseMirror mounts,
    # so waiting on navigation alone reports a missing prompt box on a project
    # that is merely still rendering.
    if cfg.flow.is_project(page.url):
        wait_first(page, cfg.selectors.get("prompt_input"), timeout_s=30)
    return page


class FlowWorker:
    def __init__(self, cfg: Config, session: BrowserSession, log=print):
        self.cfg = cfg
        self.session = session
        self.log = log

    # ---- page state -----------------------------------------------------

    def open_app(self) -> Page:
        page = open_flow(self.session, self.cfg)
        if sign_in_state(page, self.cfg) is SignInState.SIGNED_OUT:
            raise SignedOutError(
                "Flow redirected to its signed-out page. Sign in by hand in the "
                "automation Chrome window, then start the run again."
            )
        return page

    def _image_sources(self, page: Page) -> list[str]:
        """Sources of images large enough to be generated results.

        The size floor is not a nicety: the signed-in account avatar is served
        from the same googleusercontent.com host as the results, so matching on
        the URL alone picks it up on every poll.
        """
        sources: list[str] = []
        for selector in self.cfg.selectors.get("result_image"):
            try:
                sources += page.eval_on_selector_all(
                    selector,
                    """(els, min) => els
                         .map(e => ({src: e.currentSrc || e.src,
                                     w: e.naturalWidth, h: e.naturalHeight}))
                         .filter(x => x.src && x.w >= min && x.h >= min)
                         .map(x => x.src)""",
                    self.cfg.flow.min_result_dimension,
                )
            except PWError:
                continue
        return list(dict.fromkeys(sources))

    def _enter_prompt(self, page: Page, box, prompt: str) -> str | None:
        """Type into Flow's ProseMirror editor; return an error string or None.

        ProseMirror keeps its own document model, so assigning a value is not
        enough — the text has to arrive as real keystrokes. We read it back
        afterwards rather than trust the write, because silently generating
        from a half-entered prompt would cost credits and look like a success.
        """
        try:
            box.locator.click()
            page.keyboard.press("ControlOrMeta+a")
            page.keyboard.press("Delete")
            page.keyboard.type(prompt, delay=5)
        except PWError as exc:
            return f"could not enter prompt: {exc}"

        try:
            entered = (box.locator.inner_text() or "").strip()
        except PWError:
            return None  # cannot verify; let the generation proceed
        if entered.replace("\n", " ") != prompt.strip().replace("\n", " "):
            return f"prompt did not land in the editor (editor holds {entered[:60]!r})"
        return None

    def _is_busy(self, page: Page) -> bool:
        return first_match(page, self.cfg.selectors.get("loading_indicator")) is not None

    # ---- generation -----------------------------------------------------

    def submit(self, page: Page, prompt: str, timeout_s: float = 25.0) -> str | None:
        """Send one prompt. Returns an error message, or None on success.

        The generate button is disabled whenever the editor is empty, and
        ProseMirror takes a moment to register typed text. Clicking in that
        window does nothing at all and the prompt is silently lost - which
        looked exactly like a generation that timed out five minutes later.
        """
        box = wait_first(page, self.cfg.selectors.get("prompt_input"), timeout_s=20)
        if box is None:
            return (
                "prompt input not found - config/selectors.toml is out of date. "
                "Run: imgen probe"
            )

        error = self._enter_prompt(page, box, prompt)
        if error:
            return error

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            button = first_match(page, self.cfg.selectors.get("generate_button"))
            if button is not None and button.locator.is_enabled():
                try:
                    button.locator.click()
                    return None
                except PWError as exc:
                    return f"could not start generation: {exc}"
            if first_match(page, self.cfg.selectors.get("blocked_message")):
                return "blocked: Flow refused this prompt"
            page.wait_for_timeout(250)
        return "the generate button never became ready"

    def generate(self, prompt: str) -> GenerationResult:
        page = self.open_app()

        box = wait_first(page, self.cfg.selectors.get("prompt_input"), timeout_s=20)
        if box is None:
            return GenerationResult(
                Outcome.FAILED,
                error=(
                    "prompt input not found — config/selectors.toml is out of date "
                    "for the current Flow UI. Run: python -m imgen.cli probe"
                ),
            )

        # Everything already on screen is history, not our result.
        baseline = set(self._image_sources(page))

        error = self._enter_prompt(page, box, prompt)
        if error:
            return GenerationResult(Outcome.FAILED, error=error)

        button = first_match(page, self.cfg.selectors.get("generate_button"))
        if button is None:
            # Some builds submit on Enter with no visible button.
            box.locator.press("Enter")
        else:
            button.locator.click()

        return self._await_result(page, baseline)

    def _await_result(self, page: Page, baseline: set[str]) -> GenerationResult:
        deadline = time.monotonic() + self.cfg.flow.generate_timeout_s
        stable_for = 0
        last_count = 0

        while time.monotonic() < deadline:
            if first_match(page, self.cfg.selectors.get("quota_message")):
                return GenerationResult(Outcome.QUOTA, error="Flow reports no remaining credits")
            if first_match(page, self.cfg.selectors.get("blocked_message")):
                return GenerationResult(Outcome.BLOCKED, error="Flow refused this prompt")

            fresh = [s for s in self._image_sources(page) if s not in baseline]

            if self.cfg.flow.expected_images and len(fresh) >= self.cfg.flow.expected_images:
                return self._collect(page, fresh)

            if len(fresh) >= self.cfg.flow.min_images and not self._is_busy(page):
                # Flow fills its grid one image at a time, so "an image exists"
                # is not "generation finished". Only a quiet stretch is.
                stable_for = stable_for + 1 if len(fresh) == last_count else 0
                last_count = len(fresh)
                if stable_for >= self.cfg.flow.settle_seconds:
                    return self._collect(page, fresh)
            else:
                stable_for = 0
                last_count = len(fresh)

            page.wait_for_timeout(1000)

        return GenerationResult(
            Outcome.FAILED,
            error=f"no result within {self.cfg.flow.generate_timeout_s}s",
        )

    def _download(self, page: Page, src: str) -> bytes | None:
        """Fetch one result image.

        Two routes, because neither covers both cases:

        * ``blob:`` and ``data:`` sources exist only inside the page, so they
          have to be read there.
        * ``https:`` results come from flow-content.google, a different origin
          from the page. An in-page fetch of those is subject to CORS and can
          fail even though the <img> rendered fine. Playwright's request API
          runs outside the page, carries the context's cookies, and is not
          bound by CORS — so that is the route for real URLs.
        """
        if src.startswith(("blob:", "data:")):
            try:
                data_url = page.evaluate(_FETCH_AS_DATA_URL, src)
            except PWError as exc:
                self.log(f"    in-page read failed: {exc}")
                return None
            if not isinstance(data_url, str) or "," not in data_url:
                return None
            return base64.b64decode(data_url.split(",", 1)[1])

        try:
            response = page.context.request.get(src, timeout=60000)
            if not response.ok:
                self.log(f"    download failed: HTTP {response.status}")
                return None
            return response.body()
        except PWError as exc:
            self.log(f"    download failed: {exc}")
            return None

    def download_all(self, page: Page, sources: list[str]) -> list[bytes]:
        """Fetch every image of a finished batch."""
        images = []
        for src in sources:
            blob = self._download(page, src)
            if blob:
                images.append(blob)
        return images

    def _collect(self, page: Page, sources: list[str]) -> GenerationResult:
        images: list[bytes] = []
        # Not capped at expected_images: if Flow is set to produce more, keep
        # all of them rather than silently discarding the extras.
        for src in sources:
            blob = self._download(page, src)
            if blob:
                images.append(blob)

        if not images:
            return GenerationResult(
                Outcome.FAILED, error="images appeared but none could be downloaded"
            )
        return GenerationResult(Outcome.DONE, images=images)


# ---- batches -------------------------------------------------------------

# Flow renders each generation as a "batch" carrying its own prompt and a
# live "image NN%" reading. Reading those is what allows several prompts to
# be in flight at once: each result is identified by its prompt rather than
# by when it appeared.
# Flow prefixes a batch's innerText with its icon-button labels, and which
# buttons exist changes as the batch finishes ("image 99%" while running,
# "download undo delete keyboard_return" once done). Anchoring with /g does
# NOT repeat - `^` only matches index 0 without /m - so a single alternation
# stripped just the first label and the same batch was then cached under two
# different keys, one of them permanently unfinished. Hence a repeating group.
_DECOR_JS = (
    r"/^((?:download|undo|delete|favorite|more_vert|more_horiz"
    r"|keyboard_return|keyboard_arrow_down|expand_more"
    r"|reuse prompt|expand prompt|trash batch|download batch"
    r"|image(?: \d+%)?)\s*)+/i"
)

_SCAN_BATCHES_TEMPLATE = """
(minDim) => [...document.querySelectorAll('.batch-container')].map(b => {
  // The percentage is only ever in the live text, so read it before
  // anything is removed.
  const raw = (b.innerText || '').replace(/\\s+/g, ' ').trim();
  const pct = raw.match(/image (\\d+)%/);

  // The prompt is read from a copy with the controls taken out, rather
  // than by stripping their labels off the front of the text. Flow's
  // buttons render Material Symbols ligatures, so their labels arrive as
  // ordinary words ("download undo delete") glued to the prompt, and the
  // set of them changes as a batch finishes. Enumerating them was wrong
  // twice over: a bare "image" label appeared that no list had, and the
  // same batch then cached under two keys - one stuck at 99% forever.
  const clone = b.cloneNode(true);
  clone.querySelectorAll(
    'button, [role=button], [aria-hidden=true], mat-icon,' +
    '[class*=material-symbols], [class*=material-icons], [class*=icon]'
  ).forEach(n => n.remove());
  const prompt = (clone.innerText || clone.textContent || '')
      .replace(/\\s+/g, ' ')
      .replace(_DECOR, '')
      .trim();

  const images = [...b.querySelectorAll('img')]
      .filter(i => i.naturalWidth >= minDim && i.naturalHeight >= minDim)
      .map(i => i.currentSrc || i.src)
      .filter(Boolean);
  return {prompt, percent: pct ? Number(pct[1]) : null, images};
}).filter(x => x.prompt)
"""

_SCAN_BATCHES = _SCAN_BATCHES_TEMPLATE.replace("_DECOR", _DECOR_JS)


@dataclass
class Batch:
    prompt: str
    percent: int | None
    images: list[str]

    @property
    def finished(self) -> bool:
        return bool(self.images)


def normalise(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def matches(batch_prompt: str, wanted: str, length: int = 50) -> bool:
    """Is this batch the one for `wanted`?

    The opening of the prompt is what distinguishes rows - storyboard
    prompts share a long common tail of style instructions - but the text
    Flow shows is wrapped in whatever buttons that batch happens to have.
    So the job's opening is looked for *anywhere* in the batch text rather
    than only at its start: a prefix comparison failed whenever the
    decoration changed, and the result was a finished image reported as
    stuck at 99%.
    """
    a, b = normalise(batch_prompt), normalise(wanted)
    if not a or not b:
        return False
    head = b[:length]
    return head in a or a[:length] in b


#: Flow renders the batch list with Angular's CDK virtual scroller: only a
#: handful of items exist in the DOM at once, and the scrollable element is
#: an inner container, so page-level wheel events move nothing at all.
_SCROLL = """
(top) => {
  const el = document.querySelector('.cdk-virtual-scrollable')
          || document.querySelector('[class*=page-container]');
  if (!el) return null;
  if (top !== null) el.scrollTop = top;
  return {top: el.scrollTop, max: Math.max(el.scrollHeight - el.clientHeight, 0),
          view: el.clientHeight};
}
"""


class BatchReader:
    """Reads the batch list, scrolling because it is virtualised.

    Only the batches near the viewport exist in the DOM, so a finished
    generation can disappear from it. Everything seen is remembered for the
    life of the reader.
    """

    #: Upper bound on a sweep's scroll stops, so a very long project cannot
    #: turn one sweep into a minute of scrolling.
    MAX_SWEEP_STEPS = 60
    SWEEP_DWELL_MS = 420

    def __init__(self, cfg: Config, min_dimension: int | None = None):
        self.cfg = cfg
        self.min_dimension = min_dimension or cfg.flow.min_result_dimension
        self.seen: dict[str, Batch] = {}

    def _collect(self, page: Page) -> None:
        try:
            found = page.evaluate(_SCAN_BATCHES, self.min_dimension)
        except PWError:
            return
        for item in found:
            batch = Batch(item["prompt"], item["percent"], item["images"])
            key = normalise(batch.prompt)[:80]
            previous = self.seen.get(key)
            # Never let a later, emptier reading overwrite a finished one.
            if previous is None or batch.finished or not previous.finished:
                self.seen[key] = batch

    def refresh(self, page: Page, scrolls: int = 1) -> dict[str, Batch]:
        """Read the batches near the top, where anything in flight will be.

        New generations are prepended, so a running job is always among the
        first few; a full sweep every couple of seconds would be wasted work
        and would yank the page about while the user is watching.
        """
        self._collect(page)
        if scrolls > 1:
            self.sweep(page, steps=scrolls)
        return self.seen

    def sweep(self, page: Page, steps: int = 24) -> dict[str, Batch]:
        """Walk the whole virtualised list, collecting everything.

        Used when looking for work Flow has already done, rather than when
        watching something run.
        """
        try:
            pos = page.evaluate(_SCROLL, None)
        except PWError:
            pos = None
        if not pos or not pos.get("max"):
            self._collect(page)
            return self.seen

        # Walk in overlapping viewport-sized steps rather than a fixed number
        # of samples. With a fixed count, anything between two sample points
        # is never rendered and so never seen: three sweeps of the same list
        # returned 124, 108 and 69 batches. Missing one is not an error, it
        # just means paying to generate a result Flow already has.
        height = pos["max"]
        view = max(int(pos.get("view") or 0), 1)
        stride = max(int(view * 0.8), 1)
        targets = list(range(0, int(height) + stride, stride))
        if len(targets) > self.MAX_SWEEP_STEPS:
            targets = [
                round(height * i / (self.MAX_SWEEP_STEPS - 1))
                for i in range(self.MAX_SWEEP_STEPS)
            ]
        for target in targets:
            try:
                page.evaluate(_SCROLL, min(target, int(height)))
            except PWError:
                break
            # Long enough for the scroller to render the new rows and for
            # their images to report a natural size; an image still loading
            # measures 0x0 and would be read as a batch with no result.
            page.wait_for_timeout(self.SWEEP_DWELL_MS)
            self._collect(page)
        try:
            page.evaluate(_SCROLL, 0)
        except PWError:
            pass
        return self.seen

    def find(self, prompt: str) -> Batch | None:
        """Find this prompt's batch, preferring one that has images.

        A batch's visible text changes as it finishes, so the same
        generation can end up cached under two keys - one captured while it
        was running and one once it was done. Returning whichever came
        first then reported a completed row as stuck at 99% forever. The
        decoration stripping is what keeps the keys equal; this is the
        guard for the day Flow adds a button nobody has seen yet.
        """
        fallback: Batch | None = None
        for batch in self.seen.values():
            if matches(batch.prompt, prompt):
                if batch.finished:
                    return batch
                if fallback is None:
                    fallback = batch
        return fallback
