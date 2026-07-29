"""Playwright-backed BrowserSession over CDP.

Connects to an ALREADY-RUNNING Chrome/Chromium that was started by the human
with a dedicated, already-logged-in profile:

    chrome --user-data-dir=$HOME/.autoloop-chrome --remote-debugging-port=9222

It never launches a browser, never touches the login flow, and never sees
credentials. `close()` only disconnects — the user's browser stays open.

Playwright is imported lazily inside `connect`, so nothing else in autoloop
(including the whole test suite) needs the package installed.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import BrowserError, SessionLostError


class PlaywrightSession:
    def __init__(self, page, playwright, error_cls):
        self._page = page
        self._playwright = playwright
        self._error_cls = error_cls

    @classmethod
    def connect(cls, cdp_url: str, match_url_substring: str = "chatgpt.com") -> "PlaywrightSession":
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(
                "playwright is not installed — run: pip install -r autoloop/requirements.txt "
                "&& playwright install chromium"
            ) from exc
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as exc:
            pw.stop()
            raise SessionLostError(
                f"cannot connect to Chrome DevTools at {cdp_url} — is the dedicated "
                f"profile running with --remote-debugging-port? ({exc})"
            ) from exc
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for p in context.pages:
            if match_url_substring in p.url:
                page = p
                break
        if page is None:
            page = context.new_page()
        return cls(page, pw, PlaywrightError)

    def _call(self, fn):
        try:
            return fn()
        except self._error_cls as exc:
            raise SessionLostError(f"browser session lost: {exc}") from exc

    # ---- BrowserSession protocol --------------------------------------------

    def goto(self, url: str) -> None:
        self._call(lambda: self._page.goto(url, wait_until="domcontentloaded"))

    def url(self) -> str:
        return self._call(lambda: self._page.url)

    def exists(self, selector: str) -> bool:
        return self._call(lambda: self._page.locator(selector).count() > 0)

    def click(self, selector: str) -> None:
        self._call(lambda: self._page.locator(selector).first.click())

    def fill(self, selector: str, text: str) -> None:
        self._call(lambda: self._page.locator(selector).first.fill(text))

    def elements(self, selector: str, attr: str) -> list[tuple[str, str]]:
        def _read():
            out = []
            loc = self._page.locator(selector)
            for i in range(loc.count()):
                el = loc.nth(i)
                out.append((el.get_attribute(attr) or "", el.inner_text()))
            return out

        return self._call(_read)

    def screenshot(self, path: Path) -> None:
        self._call(lambda: self._page.screenshot(path=str(path), full_page=True))

    def html(self) -> str:
        return self._call(lambda: self._page.content())

    def close(self) -> None:
        try:
            self._playwright.stop()
        except Exception:
            pass
