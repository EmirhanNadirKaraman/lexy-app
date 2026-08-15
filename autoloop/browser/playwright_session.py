"""Playwright-backed BrowserSession over CDP.

Connects to an ALREADY-RUNNING Chrome/Chromium that was started by the human
with a dedicated, already-logged-in profile:

    chrome --user-data-dir=$HOME/.autoloop-chrome --remote-debugging-port=9222

It never launches a browser, never touches the login flow, and never sees
credentials. `close()` only disconnects — the user's browser stays open.

The one tab it will close on someone else's behalf is a DUPLICATE of the
conversation it just bound to (`_reap_duplicates`, called from `connect`),
because `close()` cannot run on an abrupt exit and those pile up. Every other
tab in the profile is left alone.

The Playwright driver itself is a per-process singleton (`_driver`): starting
a second one while the first is alive is a hard error, so sessions share one
and `close()` drops only the CDP connection.

It also deliberately exposes no cookie/storage accessor, so no code path
(diagnostics included) can capture authentication material.

It additionally implements the optional send-observation capability
(`start_send_observation` / `take_send_observations`): a passive
`page.on("response")` / `page.on("requestfailed")` listener that records an HTTP
status and a URL path for the conversation-send endpoint and nothing else. It
never issues a request of its own — see `observation.py` for why the vocabulary
is that narrow.

Playwright is imported lazily inside `connect`, so nothing else in autoloop
(including the whole test suite) needs the package installed.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import BrowserError, SessionLostError
from .observation import SendObservation, is_send_path, scrub_path


#: The one Playwright driver for this process, started lazily and never
#: stopped while the process lives. See `_driver` for why it is a singleton.
_DRIVER = None


def _driver():
    """The process's single Playwright driver.

    `sync_playwright().start()` raises "Playwright Sync API inside the asyncio
    loop" if another driver is ALREADY RUNNING in this thread — the sync API
    drives an event loop of its own, so a second start lands inside the first
    one. Stop-then-start is fine; alive-then-start is not. (Verified both ways
    against a live Chrome before this was written.)

    That made a leaked driver fatal rather than merely wasteful, and leaking
    one was easy: `close()` swallowed a failed `stop()`, and
    `Orchestrator._drop_client` swallows a failed `close()` and drops the
    reference regardless. So tearing down a session whose connection had
    already broken — exactly what happens after a browser error — could leave
    a live driver with nothing pointing at it, and the next `connect()` would
    take the whole loop down with it. Reusing one driver makes that
    unreachable no matter who forgets to close what, instead of relying on
    every teardown path being perfect.

    Never stopped: a `stop()` here would strand every session still holding a
    browser off this driver, and the process is about to exit anyway.
    """
    global _DRIVER
    if _DRIVER is None:
        from playwright.sync_api import sync_playwright

        _DRIVER = sync_playwright().start()
    return _DRIVER


class PlaywrightSession:
    def __init__(self, page, playwright, error_cls, browser=None, opened_page=False):
        self._page = page
        self._playwright = playwright
        self._error_cls = error_cls
        self._browser = browser
        #: True when `connect` opened this tab rather than adopting one. Only
        #: then may `close` close it — closing a tab the operator opened would
        #: be the mirror of the bug this fixes.
        self._opened_page = opened_page
        self._observations: list[SendObservation] = []
        self._listening = False

    @staticmethod
    def _same_conversation(a: str, b: str) -> bool:
        """Same conversation, ignoring query, fragment and a trailing slash."""
        from urllib.parse import urlsplit

        pa, pb = urlsplit(a or ""), urlsplit(b or "")
        return (
            bool(pa.netloc)
            and pa.netloc == pb.netloc
            and pa.path.rstrip("/") == pb.path.rstrip("/")
        )

    @classmethod
    def connect(
        cls,
        cdp_url: str,
        match_url_substring: str = "chatgpt.com",
        conversation_url: str | None = None,
    ) -> "PlaywrightSession":
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError as exc:
            raise BrowserError(
                "playwright is not installed — run: pip install -r autoloop/requirements.txt "
                "&& playwright install chromium"
            ) from exc
        pw = _driver()
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as exc:
            # Deliberately NOT stopping the driver: it is shared, and a failed
            # connect is the case most likely to be retried.
            raise SessionLostError(
                f"cannot connect to Chrome DevTools at {cdp_url} — is the dedicated "
                f"profile running with --remote-debugging-port? ({exc})"
            ) from exc
        context = browser.contexts[0] if browser.contexts else browser.new_context()

        # Bind to the CONFIGURED conversation, never to whatever ChatGPT tab
        # happens to be open first. The profile accumulates strays — a chat a
        # failed rotation created, something left open by hand — and adopting
        # one silently attaches the loop to the wrong conversation. It then
        # reports "page left the configured conversation while awaiting <id>",
        # which reads as the page navigating away when really it was never on
        # the right page (observed 2026-08-03; the loop's Chrome was sitting on
        # a third chat while the config named another).
        #
        # With no conversation given, the old substring match still applies —
        # `doctor` and the smoke test connect without one.
        page = None
        if conversation_url:
            for candidate in context.pages:
                if cls._same_conversation(conversation_url, candidate.url):
                    page = candidate
                    break
        else:
            for candidate in context.pages:
                if match_url_substring in candidate.url:
                    page = candidate
                    break

        # No match means open our own rather than adopt a stranger's: a new tab
        # in this profile is logged in exactly the same, and the caller
        # navigates to the conversation anyway.
        opened_here = page is None
        if opened_here:
            page = context.new_page()
        elif conversation_url:
            cls._reap_duplicates(context, page, conversation_url)
        return cls(page, pw, PlaywrightError, browser, opened_page=opened_here)

    @classmethod
    def _reap_duplicates(cls, context, page, conversation_url: str) -> None:
        """Close OTHER tabs sitting on the conversation we just bound to.

        `close()` already closes the tab it opened, but it has to RUN, and it
        does not when the process ends abruptly — every pause-and-exit, kill or
        crash, plus `doctor` and any ad-hoc probe. `_drop_client` swallows a
        failed `close()` by design, so nothing notices. Tabs therefore pile up
        in the profile until Chrome is restarted (observed 2026-08-04: two tabs
        on the SAME conversation, one of them orphaned).

        Reaping here is the same shape as `autoloop start` repairing a provably
        stale lock: clean up what is demonstrably orphaned, leave anything
        ambiguous alone. Hence three bounds, all load-bearing:

        * ONLY duplicates of the conversation this session bound to. A tab on a
          DIFFERENT chat may be deliberate — an operator reading a past
          conversation — and must survive.
        * NEVER the bound page, compared by IDENTITY. A duplicate has the same
          URL by definition, so a URL comparison would skip exactly the tab
          this exists to close.
        * Only for a CONFIGURED conversation, and only in the context we bound
          in. The caller with no conversation in hand (`doctor`, the smoke
          test) picked its page by bare substring, so "duplicates of whatever
          we landed on" is precisely the ambiguous case. That costs no
          coverage: a tab `doctor` orphans on the configured conversation is
          reaped by the next loop `connect`, which does pass one.

        Safe only because `~/.autoloop-chrome` is a dedicated profile. Never
        generalise this to an arbitrary CDP endpoint — that could be a human's
        main browser, full of their own tabs.
        """
        # list(): closing a page mutates `context.pages` in real Playwright.
        for candidate in list(context.pages):
            if candidate is page:
                continue
            try:
                # The URL read is inside the try too — a dying page raises on
                # that, not only on close(). Best-effort per candidate, like
                # the existing teardown: one stray that refuses to close must
                # not abort the connect, nor stop the remaining strays being
                # reaped.
                if cls._same_conversation(conversation_url, candidate.url):
                    candidate.close()
            except Exception:
                pass

    def _call(self, fn):
        try:
            return fn()
        except self._error_cls as exc:
            raise SessionLostError(f"browser session lost: {exc}") from exc

    # ---- BrowserSession protocol --------------------------------------------

    def goto(self, url: str) -> None:
        self._call(lambda: self._page.goto(url, wait_until="domcontentloaded"))

    def reload(self) -> None:
        self._call(lambda: self._page.reload(wait_until="domcontentloaded"))

    def url(self) -> str:
        return self._call(lambda: self._page.url)

    def exists(self, selector: str) -> bool:
        return self._call(lambda: self._page.locator(selector).count() > 0)

    def is_enabled(self, selector: str) -> bool:
        def _probe():
            loc = self._page.locator(selector)
            return loc.count() > 0 and loc.first.is_enabled()

        return self._call(_probe)

    def click(self, selector: str) -> None:
        self._call(lambda: self._page.locator(selector).first.click())

    def focus(self, selector: str) -> None:
        # A real click, not .focus(): ProseMirror sets up its selection state
        # from the pointer interaction the way it does for a person.
        self._call(lambda: self._page.locator(selector).first.click())

    def press(self, keys: str) -> None:
        self._call(lambda: self._page.keyboard.press(keys))

    def insert_text(self, text: str) -> None:
        # keyboard.insert_text emits beforeinput/input (CDP Input.insertText),
        # which is what the editor listens for — and unlike type() it does not
        # cost one round-trip per character on a multi-thousand-char prompt.
        # It emits no key events, so it can never trigger an accidental send.
        self._call(lambda: self._page.keyboard.insert_text(text))

    def set_input_files(self, selector: str, path: str) -> None:
        """Attach a file to a (usually hidden) file input.

        Playwright sets files directly on the element, so the input does not
        need to be visible — which matters because ChatGPT keeps it behind the
        attach button. This uploads; it does not send.
        """
        self._call(lambda: self._page.locator(selector).first.set_input_files(path, timeout=60000))

    def inner_text(self, selector: str) -> str:
        def _read():
            loc = self._page.locator(selector)
            return loc.first.inner_text() if loc.count() > 0 else ""

        return self._call(_read)

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

    # ---- optional send-observation capability -------------------------------

    def start_send_observation(self) -> None:
        """Attach the passive listener once and clear any prior window.

        Attaching is idempotent (the handlers stay for the life of the page);
        each call opens a fresh window by dropping what the previous one saw,
        so an observation can never be attributed to a later send.
        """
        self._observations = []
        if self._listening:
            return

        def _on_response(response):
            try:
                if is_send_path(response.url):
                    # Status and path ONLY. Reading `response.body()` or any
                    # header here would put credentials and message content
                    # into a diagnostics dump — see observation.py.
                    self._observations.append(
                        SendObservation(path=scrub_path(response.url), status=response.status)
                    )
            except Exception:
                pass

        def _on_request_failed(request):
            try:
                if is_send_path(request.url):
                    self._observations.append(
                        SendObservation(
                            path=scrub_path(request.url),
                            status=None,
                            failure=str(request.failure or "request failed")[:120],
                        )
                    )
            except Exception:
                pass

        try:
            self._page.on("response", _on_response)
            self._page.on("requestfailed", _on_request_failed)
        except self._error_cls:
            # A page that cannot take listeners is a dead page; the caller
            # discovers that on its next real operation. Observation is an
            # optimisation, never a precondition.
            return
        self._listening = True

    def take_send_observations(self) -> list[SendObservation]:
        """Drain the current window. Draining leaves the listener attached."""
        observations, self._observations = self._observations, []
        return observations

    def close(self) -> None:
        """Drop THIS session's CDP connection. The driver stays up.

        Stopping the shared driver here would break every other session and
        make the next `connect()` start a second one — the failure this class
        exists to avoid. Closing a CDP-connected browser only ends the
        connection: the human's Chrome keeps running, verified against a live
        profile (same pid, CDP still answering, afterwards).
        """
        if self._browser is None:
            return
        if self._opened_page and self._page is not None:
            # Ours to clean up. Without this a long run leaks one tab per
            # client rebuild, and a profile full of ChatGPT tabs is exactly
            # what made tab selection ambiguous in the first place.
            try:
                self._page.close()
            except Exception:
                pass
        try:
            self._browser.close()
        except Exception:
            pass
        finally:
            self._browser = None
