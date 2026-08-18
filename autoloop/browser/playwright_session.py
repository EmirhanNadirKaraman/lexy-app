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

import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import AutoloopError, BrowserError, SessionLostError
from . import chrome_restart
from .observation import SendObservation, is_send_path, scrub_path


#: The one Playwright driver for this process, started lazily and never
#: stopped while the process lives. See `_driver` for why it is a singleton.
_DRIVER = None

#: Hosts this machine is evidence about. `chrome_restart`'s probes dial
#: 127.0.0.1 and `ps` lists local processes, so for a CDP URL pointing anywhere
#: else NOTHING here — port, endpoint or browser process — describes it, and the
#: whole diagnosis degrades to "unknown" (see `_unmeasured`). The loop's own
#: default is `http://127.0.0.1:9222` (`config.py`), so this is the rare path.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", ""})


#: Message fragments `scroll_to_end` chooses to FORGIVE when bringing the last
#: mounted node into view fails. All describe the node, not the connection: a
#: virtualizer detaching what we just measured, or a node that cannot be made
#: visible, is a routine repaint, and restarting Chrome over one would be a
#: worse bug than the scroll that painted nothing.
#:
#: Matched by exception NAME and MESSAGE rather than by class on purpose.
#: Playwright is imported lazily (see the module docstring) so that nothing
#: else in autoloop — the whole test suite included — needs the package
#: installed; naming `playwright.sync_api.TimeoutError` here would both break
#: that and make this branch unexercisable. This is a list of what we forgive,
#: NOT a claim about which exception the library raises for a given fault, so
#: it deliberately covers both shapes the same fault can arrive in.
_BENIGN_SCROLL_FAULTS = (
    "not attached to the dom",
    "detached from document",
    "element is not visible",
    "element is outside of the viewport",
    "element is not stable",
)


#: How many pixels short of the bottom still counts as "at the end of the
#: list". Fractional device-pixel ratios and sub-pixel layout leave a scroll
#: container a hair off its own maximum even when the browser considers it
#: fully scrolled, so an exact comparison would report "more to paint" forever.
_SCROLL_END_SLACK_PX = 4

#: Distance in pixels from the END of the scroll container that holds the last
#: mounted node, or `null` when nothing about the position can be measured.
#:
#: Walks OUT from the node rather than guessing a container selector: ChatGPT's
#: scroller is an unnamed div whose classes change with every UI revision, and a
#: selector for it would rot silently into "no evidence" (which now costs an
#: answer, not just a scroll). A container that does not scroll at all — a short
#: conversation that fits — returns 0, i.e. AT the end: everything it holds is
#: already in view, and calling that "unknown" would refuse every chat short
#: enough to mount in one window.
_SCROLL_REMAINING_JS = """
node => {
  const remaining = el => el.scrollHeight - el.scrollTop - el.clientHeight;
  let el = node.parentElement;
  while (el) {
    const overflow = getComputedStyle(el).overflowY;
    if (/(auto|scroll|overlay)/.test(overflow) && el.scrollHeight > el.clientHeight + 1) {
      return remaining(el);
    }
    el = el.parentElement;
  }
  const doc = document.scrollingElement || document.documentElement;
  if (!doc) { return null; }
  return doc.scrollHeight > doc.clientHeight + 1 ? remaining(doc) : 0;
}
"""


def _is_benign_scroll_fault(exc: BaseException) -> bool:
    """May `scroll_to_end` swallow `exc` and carry on?

    Only for an element-local fault. A dead driver channel arrives as a plain
    `Exception` whose text names the connection ("Connection closed while
    reading from the driver"), matches nothing here, and is re-raised so
    `_call` can turn it into the `BrowserError` the loop routes.
    """
    if isinstance(exc, AutoloopError):
        return False  # a deliberate diagnosis keeps its own routing
    if type(exc).__name__ == "TimeoutError":
        # `scroll_into_view_if_needed` retries an unactionable element until
        # its own timeout, so this is the ordinary shape of "that node would
        # not scroll" — and it is bounded, unlike a lost connection.
        return True
    text = str(exc).lower()
    return any(fragment in text for fragment in _BENIGN_SCROLL_FAULTS)


def _port_in_use(port: int) -> bool:
    """Thin seam over `chrome_restart` so a test can fake the machine."""
    return chrome_restart._default_port_in_use(port)


def _endpoint_ready(port: int) -> bool:
    """True when `/json/version` answers with a browser websocket URL."""
    return chrome_restart._default_endpoint_ready(port)


def _list_processes() -> list[tuple[int, str]]:
    return chrome_restart._default_list_processes()


def _urlopen(url: str, timeout: float):
    """Thin seam over the one HTTP call this module makes, so a test can
    describe a CDP endpoint without a socket."""
    return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310 - CDP endpoint


def _endpoint_host_port(cdp_url: str) -> tuple[str, int | None]:
    """Host and port, or `("", None)` for anything this cannot parse.

    The whole `urlsplit` is inside the guard, not just the `.port` read: a
    malformed authority raises `ValueError` from `.port` on today's CPython but
    can raise from the parse itself, and which one it is must not decide whether
    the operator gets a diagnosis. An unparseable endpoint IS an unprobeable
    one, so it takes the `port is None` branch and says to fix the url.
    """
    try:
        parts = urlsplit(cdp_url if "//" in cdp_url else f"//{cdp_url}")
        return (parts.hostname or "", parts.port)
    except ValueError:  # a malformed authority, e.g. ":not-a-port"
        return ("", None)


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

    Only for an endpoint this machine can actually measure — see `_unmeasured`
    for the rest, which get no evidence rather than local evidence.

    Bounded: a 0.5s port probe, a 2s HTTP probe and one `ps` (30s timeout),
    against a restart command the callers already allow 180s. The unmeasurable
    path runs none of the three.
    """
    try:
        return _diagnose(cdp_url)
    except Exception as exc:
        return (
            f"diagnosis=unavailable ({type(exc).__name__}: {exc}) "
            f"[endpoint={cdp_url}]"
        )


def _unmeasured(cdp_url: str, hint: str) -> str:
    """The diagnosis for an endpoint this machine cannot probe.

    EVERY field is unknown, and none of them is quietly filled in from local
    state. That is the whole point: a Chrome on this machine says nothing about
    a CDP endpoint on another one, so reporting its pids here — or reading them
    to choose between "Chrome IS running, restart it" and "NO Chrome is
    running, start it" — produces a confident diagnosis of the wrong machine,
    which is worse than none. The `ps` is skipped too, rather than run and
    discarded: it costs up to 30s and could not inform the answer either way.
    """
    return (
        f"{hint} [endpoint={cdp_url} port_open=unknown cdp_answering=unknown "
        "chrome_on_profile=unknown chrome_on_port=unknown]"
    )


def _diagnose(cdp_url: str) -> str:
    host, port = _endpoint_host_port(cdp_url)
    if port is None:
        # A malformed authority (":not-a-port", a bare hostname). Nothing to
        # probe and nothing to restart — the endpoint itself is the fault.
        return _unmeasured(
            cdp_url,
            "fix the CDP url — it names no usable port (expected something like "
            "http://127.0.0.1:9222), so nothing here can reach it",
        )
    if host not in _LOOPBACK_HOSTS:
        return _unmeasured(
            cdp_url,
            f"check Chrome on {host} (and the route to it) — this endpoint is "
            "not on this machine, so nothing measurable from here describes it",
        )
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


def json_list_url(cdp_url: str) -> str:
    """`<cdp endpoint>/json/list`, or "" for an endpoint with no authority at
    all. A bare `host:port` is assumed to be http, the same assumption
    `doctor` makes when it builds `/json/version`."""
    base = (cdp_url or "").strip().rstrip("/")
    if not base:
        return ""
    if "://" not in base:
        base = f"http://{base}"
    return f"{base}/json/list"


def count_page_targets(body: str) -> int | None:
    """Page targets in a `/json/list` payload, or None when the payload is not
    one. Only `type == "page"` counts: a service worker or a background page is
    not something Playwright can drive a conversation on."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    return sum(
        1 for target in payload if isinstance(target, dict) and target.get("type") == "page"
    )


def attachable_page_targets(cdp_url: str, timeout: float = 2.0) -> int | None:
    """How many pages the CDP endpoint says it has, or None when that could not
    be measured.

    The state `/json/version` cannot see. On 2026-08-17 the operator closed the
    browser window: Chrome stayed alive, `/json/version` kept answering with a
    valid `webSocketDebuggerUrl` — so every probe built on it, `doctor`
    included, reported a healthy browser — and `/json/list` returned ZERO
    targets. Playwright could not attach at all, so there was no page to read a
    modal on and nothing to click. Restarting the profile restored 11 targets.

    **Zero and unmeasurable are deliberately different answers.** A 0 means the
    endpoint answered and named no page; anything else that goes wrong (no
    answer, a non-200, a body that is not a JSON array) is None, because the
    caller acts on 0 by restarting the browser and an unreachable endpoint is
    already the ordinary `BrowserError` path — one that diagnoses itself
    through `describe_cdp_endpoint` and restarts on its own budget.

    Never raises, for the same reason `describe_cdp_endpoint` does not: it is
    called while a fault is already being handled.
    """
    url = json_list_url(cdp_url)
    if not url:
        return None
    try:
        with _urlopen(url, timeout) as response:
            if response.status != 200:
                return None
            # Bounded: a profile with many tabs still lists far less than this,
            # and an endpoint answering with something else must not be read
            # into memory unbounded.
            body = response.read(2_000_000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return None
    return count_page_targets(body)


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

    # ---- optional tail-mounting capability ----------------------------------

    def scroll_to_end(self, selector: str) -> bool | None:
        """Move the view to the END of a virtualized list, and report whether it
        got there.

        ChatGPT keeps only a window of a conversation in the DOM and mounts
        more as the viewport moves, so recent turns can be absent from
        `innerText` purely because nothing scrolled to them: on 2026-08-05 the
        chat holding `alr-af11e1b3-0006` needed End plus six scrolls before its
        tail rendered. This is that gesture, programmatically — bring the last
        mounted node into view AND press End, because either alone can leave
        the container where it was.

        Returns True when the list's own scroll container is demonstrably at
        its end afterwards, False when there is more below, and None when the
        position could not be measured. **The return value is the point**, not a
        convenience: the caller has to tell "the list is fully mounted" from
        "the gesture did nothing", and those two produce the same unchanged
        window. End goes to whatever holds focus, so a gesture that missed the
        scroller is silent — this measurement is the only thing that is not.
        A conversation short enough that nothing scrolls reads as True (see
        `_SCROLL_REMAINING_JS`); a chat with no mounted node yet reads as None,
        because there is nothing whose container we could measure.

        Best-effort per call, but only for ELEMENT-local faults: the caller
        repeats the gesture and watches what the mounted window contains, so a
        node that refuses to come into view is not a dead session — it costs
        the measurement (None) and nothing else. Anything else — the driver
        channel above all — is re-raised into `_call` and surfaces as a
        `BrowserError`, the same as every other call here. That distinction is
        the whole contract: the caller rules on ABSENCE from what this reports,
        so "the gesture painted nothing" and "the browser is gone" must not
        become the same observation.
        """

        def _scroll():
            loc = self._page.locator(selector)
            count = loc.count()
            if not count:
                # A freshly loaded chat mounts nothing for a moment. End is what
                # paints the first window; there is no node to measure yet.
                self._page.keyboard.press("End")
                return None
            last = loc.nth(count - 1)
            try:
                last.scroll_into_view_if_needed(timeout=5000)
            except Exception as exc:
                # A node that will not scroll into view (detached by the
                # virtualizer mid-gesture, covered, zero-sized) says nothing
                # about the session's health — the End press below is the
                # other half of the gesture and still runs. Every OTHER
                # fault leaves through `_call`.
                if not _is_benign_scroll_fault(exc):
                    raise
            self._page.keyboard.press("End")
            try:
                remaining = last.evaluate(_SCROLL_REMAINING_JS)
            except Exception as exc:
                # Measuring is the same element-local risk as scrolling to it,
                # and it fails the same ways. Unmeasured is reported as
                # unmeasured — never as "at the end".
                if not _is_benign_scroll_fault(exc):
                    raise
                return None
            if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
                return None
            return remaining <= _SCROLL_END_SLACK_PX

        return self._call(_scroll)

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
