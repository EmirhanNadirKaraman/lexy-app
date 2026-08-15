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

EVERY call into Playwright is guarded POSITIONALLY (`connect`, `_call`), not by
exception type. Playwright's `rewrite_error` turns a driver-channel failure into
a plain `Exception` — "Connection closed while reading from the driver" is not a
`playwright.sync_api.Error` and not a subclass of anything we can name — so a
narrow `except` clause misses exactly the fault that kills the transport. On
2026-08-15 one such connect took the whole loop down: the exception reached the
top level, the process ended with `phase=submitting`, `stop_reason=None` and no
blocker, and from outside it was indistinguishable from a clean exit. Everything
raised out of this module is a `BrowserError`, so the orchestrator's existing
recovery (restart → failure budget → a park that names the cause) sees it.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import AutoloopError, BrowserError, SessionLostError
from . import chrome_restart
from .observation import SendObservation, is_send_path, scrub_path


#: The one Playwright driver for this process, started lazily and never
#: stopped while the process lives. See `_driver` for why it is a singleton.
_DRIVER = None

#: Hosts whose port/endpoint we may probe from here. A CDP URL pointing
#: anywhere else is reported as "unknown" rather than measured against
#: 127.0.0.1, which is what `chrome_restart`'s probes actually talk to.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})


def _port_in_use(port: int) -> bool:
    """Thin seam over `chrome_restart` so a test can fake the machine."""
    return chrome_restart._default_port_in_use(port)


def _endpoint_ready(port: int) -> bool:
    """True when `/json/version` answers with a browser websocket URL."""
    return chrome_restart._default_endpoint_ready(port)


def _list_processes() -> list[tuple[int, str]]:
    return chrome_restart._default_list_processes()


def _endpoint_host_port(cdp_url: str) -> tuple[str, int | None]:
    parts = urlsplit(cdp_url if "//" in cdp_url else f"//{cdp_url}")
    try:
        port = parts.port
    except ValueError:  # a malformed authority, e.g. ":not-a-port"
        port = None
    return (parts.hostname or "", port)


def _chrome_pids(profile: str, port: int | None) -> tuple[list[int], list[int]]:
    """Browser processes on `profile`, and browser processes claiming `port`.

    Helpers are excluded by `--type=`: every Chrome renderer inherits the
    parent's `--user-data-dir` and its `--remote-debugging-port`, so counting
    them would report "Chrome is running" from a pile of children whose browser
    process has gone (the same trap that made the old restart script kill a
    renderer and declare success — see docs/COMMON_ERRORS.md).
    """
    on_profile: list[int] = []
    on_port: list[int] = []
    port_flag = f"--remote-debugging-port={port}" if port is not None else None
    for pid, command in _list_processes():
        if "--type=" in command:
            continue
        if chrome_restart.matches_profile(command, profile):
            on_profile.append(pid)
        if port_flag is not None and port_flag in command:
            on_port.append(pid)
    return (on_profile, on_port)


def _format_pids(pids: list[int]) -> str:
    """A count plus the first few pids — the operator needs something to signal,
    not the whole list a busy profile produces."""
    if not pids:
        return "0"
    shown = ", ".join(str(pid) for pid in pids[:3])
    return f"{len(pids)} (pid {shown}{', ...' if len(pids) > 3 else ''})"


def describe_cdp_endpoint(cdp_url: str) -> str:
    """What the machine looked like at the moment a connect failed.

    'Chrome is running but the port is dead' and 'there is no browser at all'
    need opposite operator actions — restart the wedged one (it may ignore
    SIGTERM) versus start one — and a bare "cannot connect" describes both. The
    third case is real too and neither of the first two: a Chrome whose
    `/json/version` still returns 200 while CDP itself is wedged
    (docs/COMMON_ERRORS.md, 2026-08-14), so the endpoint is probed separately
    from the port rather than inferred from it.

    Measured HERE, in the failing connect, because it cannot be measured later:
    `_handle_browser_failure` drops the client and restarts the browser before
    anything is written, so an orchestrator-side probe would describe the world
    after the repair, not the fault.

    Never raises — see `_diagnose`. A diagnosis that throws would replace the
    recorded park with the crash it exists to prevent.

    The ACTION leads and the evidence follows, because not every surface shows
    the whole string: `autoloop start` prints `blocker.question[:160]`. Ordered
    the other way, the compact view would show four key=value pairs and cut off
    the one sentence saying what to do.

    Bounded: a 0.5s port probe, a 2s HTTP probe and one `ps` (30s timeout),
    against a restart command the callers already allow 180s.
    """
    try:
        return _diagnose(cdp_url)
    except Exception as exc:
        return (
            f"diagnosis=unavailable ({type(exc).__name__}: {exc}) "
            f"[endpoint={cdp_url}]"
        )


def _diagnose(cdp_url: str) -> str:
    host, port = _endpoint_host_port(cdp_url)
    if port is None or host not in _LOOPBACK_HOSTS:
        port_open = answering = "unknown"
    else:
        port_open = "yes" if _port_in_use(port) else "no"
        answering = "yes" if _endpoint_ready(port) else "no"

    profile = os.environ.get("AUTOLOOP_CHROME_PROFILE", chrome_restart.DEFAULT_PROFILE)
    on_profile, on_port = _chrome_pids(profile, port)
    running = bool(on_profile or on_port)
    if running:
        hint = (
            "Chrome IS running but this endpoint is unusable — restart the "
            "dedicated profile (python3 -m autoloop.browser.chrome_restart); a "
            "wedged browser can keep answering HTTP and can ignore SIGTERM"
        )
    else:
        hint = (
            "NO Chrome is running on that profile — start the dedicated profile "
            "(python3 -m autoloop.browser.chrome_restart) and check nothing else "
            "holds the port"
        )
    return (
        f"{hint} [endpoint={cdp_url} port_open={port_open} "
        f"cdp_answering={answering} chrome_on_profile={_format_pids(on_profile)} "
        f"chrome_on_port={_format_pids(on_port)} profile={profile}]"
    )


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
        #: Kept, and deliberately no longer branched on. Every guard in this
        #: class is positional now (see the module docstring): Playwright's
        #: driver-channel failures are plain `Exception`s, so this type
        #: identifies the library, not the set of faults it can raise.
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
        """Bind to the already-running browser, or raise a `BrowserError`.

        The guard is POSITIONAL — one `except Exception` around every call that
        reaches the driver — because the fault that matters here has no type to
        catch. Playwright's `rewrite_error` produces a plain `Exception` for a
        driver-channel failure, so the old `except PlaywrightError` let
        "Connection closed while reading from the driver" straight past the
        browser-failure routing and out of the process (2026-08-15). Nothing
        about that fault is exotic; only its type was.

        `AutoloopError` is re-raised untouched so a deliberate diagnosis (a
        missing playwright, a future `LoginExpiredError`) is not flattened into
        a transport fault, and `KeyboardInterrupt`/`SystemExit` pass through
        because they are not `Exception`.
        """
        try:
            from playwright.sync_api import Error as PlaywrightError
        except ImportError as exc:
            raise BrowserError(
                "playwright is not installed — run: pip install -r autoloop/requirements.txt "
                "&& playwright install chromium"
            ) from exc
        try:
            return cls._connect(cdp_url, PlaywrightError, match_url_substring, conversation_url)
        except AutoloopError:
            raise
        except Exception as exc:
            # Deliberately NOT stopping the driver: it is shared, and a failed
            # connect is the case most likely to be retried.
            #
            # The original type is named in the text because
            # `_handle_browser_failure` logs `kind=type(exc).__name__`, which
            # from here on always reads `SessionLostError` — without this the
            # transcript could not tell a driver crash from a refused socket.
            #
            # Order: what to DO, then the evidence, then the raw fault. A park
            # is read in a 160-character summary before it is read in full.
            raise SessionLostError(
                f"cannot connect to Chrome DevTools at {cdp_url} — "
                f"{describe_cdp_endpoint(cdp_url)} "
                f"(original fault: {type(exc).__name__}: {exc})"
            ) from exc

    @classmethod
    def _connect(
        cls,
        cdp_url: str,
        error_cls,
        match_url_substring: str,
        conversation_url: str | None,
    ) -> "PlaywrightSession":
        """The connect itself. Every line of it runs inside `connect`'s guard,
        including `_driver()` — starting the driver talks to the same
        subprocess channel that fails here, so it fails the same way."""
        pw = _driver()
        browser = pw.chromium.connect_over_cdp(cdp_url)
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
        return cls(page, pw, error_cls, browser, opened_page=opened_here)

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
        """Run one Playwright operation; anything it raises becomes a
        `BrowserError`.

        Positional for the same reason `connect` is: once the driver channel
        dies, the NEXT `goto` / `locator` / `inner_text` raises the same plain
        `Exception` that killed the loop from `connect_over_cdp`, and
        `except self._error_cls` misses it identically. Guarding only the
        connect would leave the same crash reachable from every submit and
        every poll.

        The wider net can relabel a non-transport bug (a bad path handed to
        `set_input_files`, say) as a lost session. That trade is deliberate:
        the cost is one restart and a park that still quotes the original
        exception type and message, against a process that dies with no reason
        recorded. `AutoloopError` still passes through unconverted.
        """
        try:
            return fn()
        except AutoloopError:
            raise
        except Exception as exc:
            raise SessionLostError(
                f"browser session lost ({type(exc).__name__}: {exc})"
            ) from exc

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
        except Exception:
            # A page that cannot take listeners is a dead page; the caller
            # discovers that on its next real operation. Observation is an
            # optimisation, never a precondition — so this swallows by
            # POSITION, not by `self._error_cls`: a driver-channel failure here
            # is a plain `Exception`, and letting it out would turn an optional
            # capability into the thing that ends the process.
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
