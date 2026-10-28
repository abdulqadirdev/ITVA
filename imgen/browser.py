"""Attaching Playwright to the already-running automation Chrome over CDP."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from playwright.sync_api import Error as PWError
from playwright.sync_api import Locator, Page, sync_playwright

from .config import ChromeConfig, Config


class SignInState(str, Enum):
    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    UNKNOWN = "unknown"


@dataclass
class Match:
    locator: Locator
    selector: str


class BrowserSession:
    """A live CDP attachment to the automation browser.

    Playwright's sync API is bound to the thread that created it, so a session
    must be created and used on one thread (in the GUI that is the worker
    QThread, never the Qt main thread).
    """

    def __init__(self, chrome: ChromeConfig):
        self._chrome = chrome
        self._pw = None
        self._browser = None
        self.context = None

    def __enter__(self) -> "BrowserSession":
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self._chrome.cdp_base)
        if not self._browser.contexts:
            raise RuntimeError(
                "Connected over CDP but the browser reported no contexts. "
                "Is this really the automation Chrome?"
            )
        # The signed-in cookies live in the browser's existing default context.
        # Calling browser.new_context() here would hand back a fresh, empty
        # context and every page would land on the Google sign-in screen — the
        # single most common way this setup is gotten wrong.
        self.context = self._browser.contexts[0]
        return self

    def __exit__(self, *exc) -> None:
        # Only tear down the Playwright driver. We deliberately do not call
        # browser.close(): this Chrome belongs to the user and holds their
        # session; we are a guest in it.
        if self._pw is not None:
            self._pw.stop()
        self._pw = self._browser = self.context = None

    def page(self, url: str | None = None, usable: Callable[[str], bool] | None = None) -> Page:
        """Get a page for `url`, reusing a suitable tab when there is one.

        Two things this must not do, both learned the hard way against a real
        browser with eighteen tabs open:

        * Reuse a tab merely because its URL shares the prefix. A stale
          ``flow.google.com/about`` tab matches ``flow.google.com/`` and then
          looks exactly like being signed out. `usable` filters those out.
        * Navigate one of the user's existing tabs away. This is their browser;
          we take a blank tab or open our own.
        """
        pages = [p for p in self.context.pages if not p.is_closed()]
        if url:
            for p in pages:
                if p.url.startswith(url) and (usable is None or usable(p.url)):
                    return p

        page = next((p for p in pages if p.url in ("about:blank", "chrome://newtab/")), None)
        if page is None:
            page = self.context.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return page


def first_match(page: Page, candidates: Iterable[str]) -> Match | None:
    """First candidate selector that is present and visible, in order."""
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                return Match(locator=locator, selector=selector)
        except PWError:
            # A malformed or unsupported selector should not abort the sweep.
            continue
    return None


def wait_first(page: Page, candidates: Iterable[str], timeout_s: float) -> Match | None:
    """Poll the candidate list until one matches or the budget runs out.

    Candidates cannot simply be comma-joined into one selector because the list
    mixes CSS with Playwright's own `text=` and `:has-text()` engines.
    """
    candidates = list(candidates)
    deadline = time.monotonic() + timeout_s
    while True:
        match = first_match(page, candidates)
        if match is not None:
            return match
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def sign_in_state(page: Page, cfg: Config) -> SignInState:
    """Decide whether the automation profile has a Flow session.

    Decided from the URL alone, on purpose. Flow bounces anonymous visitors to
    its marketing page, so any other flow.google.com URL already proves a
    session exists. Looking for a "Sign in" control instead is actively
    misleading: a signed-in Google app still links to accounts.google.com for
    the account switcher, which made an earlier version of this report every
    signed-in session as signed out.

    Whether the prompt box can be found is a separate question — a selector
    problem, not an auth problem — and callers check it separately.
    """
    if cfg.flow.looks_signed_out(page.url):
        return SignInState.SIGNED_OUT
    if page.url.startswith("https://flow.google.com/"):
        return SignInState.SIGNED_IN
    return SignInState.UNKNOWN
