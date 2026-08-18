"""One Playwright driver per process, whatever the teardown paths do.

`sync_playwright().start()` raises "Playwright Sync API inside the asyncio
loop" when another driver is ALREADY RUNNING in the thread — the sync API
drives its own event loop, so a second start lands inside the first. That made
a leaked driver fatal rather than wasteful, and leaking one was easy:
`close()` swallowed a failed `stop()`, and `Orchestrator._drop_client`
swallows a failed `close()` and drops the reference regardless.

Verified against a live Chrome before these were written: the old code crashes
on the SECOND connect with the production error once a teardown fails; the new
code survives repeated connect/teardown cycles and leaves the human's browser
running on the same pid.
"""

import json
import sys
import types

import pytest

from autoloop.browser import playwright_session as ps
from autoloop.errors import SessionLostError


class FakeError(Exception):
    pass


class FakeBrowser:
    def __init__(self, driver):
        self._driver = driver
        self.closed = False
        self.contexts = [FakeContext()]

    def close(self):
        self.closed = True
        self._driver.browsers_closed += 1


class FakePage:
    def __init__(self, url="https://chatgpt.com/c/abc"):
        self.url = url
        self.closed = False

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, urls=("https://chatgpt.com/c/abc",)):
        self.pages = [FakePage(u) for u in urls]

    def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class FakeChromium:
    def __init__(self, driver, fail=False):
        self._driver = driver
        self.fail = fail

    def connect_over_cdp(self, url):
        self._driver.connects += 1
        if self.fail:
            raise FakeError("connection refused")
        return FakeBrowser(self._driver)


class FakeDriver:
    def __init__(self):
        self.connects = 0
        self.stops = 0
        self.browsers_closed = 0
        self.chromium = FakeChromium(self)

    def stop(self):
        self.stops += 1


@pytest.fixture(autouse=True)
def no_machine_probes(monkeypatch):
    """Nothing in this module may touch the real machine.

    `describe_cdp_endpoint` runs a TCP probe, an HTTP probe and `ps` on every
    failed connect — pointed at the operator's OWN Chrome if a test let it run
    for real, which is neither hermetic nor fast. Every test starts from the
    "nothing is running" machine and says so itself when it wants another.
    """
    monkeypatch.setattr(ps, "_port_in_use", lambda port: False)
    monkeypatch.setattr(ps, "_endpoint_ready", lambda port: False)
    monkeypatch.setattr(ps, "_list_processes", lambda: [])
    monkeypatch.setenv("AUTOLOOP_CHROME_PROFILE", "/tmp/autoloop-chrome-test")


@pytest.fixture
def fake_playwright(monkeypatch):
    """Install a fake `playwright.sync_api` and reset the module singleton."""
    starts = {"count": 0, "drivers": []}

    def sync_playwright():
        class Starter:
            def start(self_inner):
                starts["count"] += 1
                driver = FakeDriver()
                starts["drivers"].append(driver)
                return driver

        return Starter()

    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = sync_playwright
    module.Error = FakeError
    package = types.ModuleType("playwright")
    package.sync_api = module
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    monkeypatch.setattr(ps, "_DRIVER", None)
    yield starts
    ps._DRIVER = None


def test_many_sessions_share_one_driver(fake_playwright):
    """The core guarantee. A second live driver is a hard error in real
    Playwright, so 'started once' is the property, not an optimisation."""
    sessions = [ps.PlaywrightSession.connect("http://localhost:9222") for _ in range(5)]

    assert fake_playwright["count"] == 1
    assert fake_playwright["drivers"][0].connects == 5
    assert len({id(s._playwright) for s in sessions}) == 1


def test_close_drops_the_connection_and_never_the_driver(fake_playwright):
    """Stopping the shared driver in `close()` is what made the next
    `connect()` start a second one."""
    session = ps.PlaywrightSession.connect("http://localhost:9222")
    driver = fake_playwright["drivers"][0]

    session.close()

    assert driver.browsers_closed == 1
    assert driver.stops == 0

    # And the next session still works off the same driver.
    ps.PlaywrightSession.connect("http://localhost:9222")
    assert fake_playwright["count"] == 1


def test_a_failed_teardown_cannot_poison_the_next_connect(fake_playwright):
    """The production path: `close()` hides its own exception and
    `_drop_client` hides close()'s, so a broken connection is torn down by
    dropping the reference. That must not leave a driver that the next
    connect trips over."""

    class Exploding:
        def close(self):
            raise RuntimeError("close failed on a broken connection")

    session = ps.PlaywrightSession.connect("http://localhost:9222")
    session._browser = Exploding()
    session.close()  # must not raise

    ps.PlaywrightSession.connect("http://localhost:9222")
    assert fake_playwright["count"] == 1


def test_close_is_idempotent(fake_playwright):
    session = ps.PlaywrightSession.connect("http://localhost:9222")
    driver = fake_playwright["drivers"][0]

    session.close()
    session.close()

    assert driver.browsers_closed == 1  # not twice
    assert driver.stops == 0


def test_a_failed_connect_leaves_the_driver_usable(fake_playwright):
    """The old code stopped the driver here. Shared now — and a refused
    connect is the case most likely to be retried."""
    ps._driver()
    driver = fake_playwright["drivers"][0]
    driver.chromium.fail = True

    with pytest.raises(SessionLostError):
        ps.PlaywrightSession.connect("http://localhost:9222")
    assert driver.stops == 0

    driver.chromium.fail = False
    ps.PlaywrightSession.connect("http://localhost:9222")
    assert fake_playwright["count"] == 1


def test_a_missing_playwright_still_says_how_to_install_it(monkeypatch):
    monkeypatch.setattr(ps, "_DRIVER", None)
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    from autoloop.errors import BrowserError

    with pytest.raises(BrowserError, match="playwright is not installed"):
        ps.PlaywrightSession.connect("http://localhost:9222")
    ps._DRIVER = None


# ---- binding to the CONFIGURED conversation ---------------------------------
#
# `connect` used to take the first tab whose URL merely contained
# "chatgpt.com". The dedicated profile accumulates strays — a chat a failed
# rotation created, something left open by hand — so the loop could attach to
# the wrong conversation and then report "page left the configured
# conversation while awaiting <id>", which reads as the page navigating away
# when it was never on the right page. Observed 2026-08-03: the loop's Chrome
# sat on a third chat while the config named another.

WANTED = "https://chatgpt.com/g/g-p-abc-demo/c/wanted-chat"
STRAY = "https://chatgpt.com/g/g-p-abc-demo/c/some-other-chat"


def test_it_binds_to_the_configured_conversation_not_the_first_chatgpt_tab(fake_playwright):
    """The bug. A stray ChatGPT tab must not be adopted when the conversation
    this session is for is open in another one."""
    import autoloop.browser.playwright_session as ps

    original = FakeChromium.connect_over_cdp

    def with_pages(self, url):
        self._driver.connects += 1
        browser = FakeBrowser(self._driver)
        browser.contexts = [FakeContext((STRAY, WANTED))]
        return browser

    FakeChromium.connect_over_cdp = with_pages
    try:
        session = ps.PlaywrightSession.connect("http://cdp", conversation_url=WANTED)
        assert session._page.url == WANTED
    finally:
        FakeChromium.connect_over_cdp = original


def test_no_matching_tab_opens_its_own_rather_than_adopting_a_stranger(fake_playwright):
    """A new tab in this profile is logged in identically, and the caller
    navigates to the conversation anyway — so adopting an unrelated chat buys
    nothing and costs correctness."""
    import autoloop.browser.playwright_session as ps

    original = FakeChromium.connect_over_cdp

    def only_stray(self, url):
        self._driver.connects += 1
        browser = FakeBrowser(self._driver)
        browser.contexts = [FakeContext((STRAY,))]
        return browser

    FakeChromium.connect_over_cdp = only_stray
    try:
        session = ps.PlaywrightSession.connect("http://cdp", conversation_url=WANTED)
        assert session._page.url == "about:blank", "must not adopt the stray"
        assert session._opened_page is True
    finally:
        FakeChromium.connect_over_cdp = original


def test_a_tab_we_opened_is_closed_again_but_a_borrowed_one_is_not(fake_playwright):
    """Closing a tab the operator opened would be the mirror of this bug; not
    closing ours leaks one per client rebuild, and a profile full of ChatGPT
    tabs is what made selection ambiguous to begin with."""
    import autoloop.browser.playwright_session as ps

    original = FakeChromium.connect_over_cdp

    def only_stray(self, url):
        self._driver.connects += 1
        browser = FakeBrowser(self._driver)
        browser.contexts = [FakeContext((STRAY,))]
        return browser

    FakeChromium.connect_over_cdp = only_stray
    try:
        ours = ps.PlaywrightSession.connect("http://cdp", conversation_url=WANTED)
        page = ours._page
        ours.close()
        assert page.closed is True
    finally:
        FakeChromium.connect_over_cdp = original

    def has_wanted(self, url):
        self._driver.connects += 1
        browser = FakeBrowser(self._driver)
        browser.contexts = [FakeContext((WANTED,))]
        return browser

    FakeChromium.connect_over_cdp = has_wanted
    try:
        borrowed = ps.PlaywrightSession.connect("http://cdp", conversation_url=WANTED)
        page = borrowed._page
        borrowed.close()
        assert page.closed is False, "a tab we did not open is not ours to close"
    finally:
        FakeChromium.connect_over_cdp = original


def test_query_and_trailing_slash_do_not_defeat_the_match(fake_playwright):
    import autoloop.browser.playwright_session as ps

    same = ps.PlaywrightSession._same_conversation
    assert same(WANTED, WANTED + "/")
    assert same(WANTED, WANTED + "?model=gpt-5")
    assert not same(WANTED, STRAY)
    assert not same(WANTED, "https://evil.example/g/g-p-abc-demo/c/wanted-chat")
    assert not same(WANTED, "")


def test_without_a_conversation_the_old_substring_match_still_applies(fake_playwright):
    """`doctor` and the smoke test connect with no conversation in hand."""
    import autoloop.browser.playwright_session as ps

    session = ps.PlaywrightSession.connect("http://cdp")
    assert "chatgpt.com" in session._page.url


# ---- reaping orphaned duplicates at connect ---------------------------------
#
# `close()` closes the tab it opened and never one it merely borrowed — that
# part was never broken. The leak is that `close()` has to RUN, and it does not
# when the process ends abruptly: every pause-and-exit, kill or crash, plus
# `doctor` and any ad-hoc probe. `Orchestrator._drop_client` swallows a failed
# `close()` by design, so nothing notices, and tabs accumulate in the dedicated
# profile until Chrome is restarted. Observed 2026-08-04: two tabs on the SAME
# conversation, one of them orphaned.
#
# So `connect` reaps what is DEMONSTRABLY orphaned — another tab on the very
# conversation it just bound to — and nothing else. A tab on a different chat
# may be an operator reading a past conversation.


def _connect_with_pages(context):
    """Run a connect whose single context is `context`, restoring the double."""
    original = FakeChromium.connect_over_cdp

    def with_pages(self, url):
        self._driver.connects += 1
        browser = FakeBrowser(self._driver)
        browser.contexts = [context]
        return browser

    FakeChromium.connect_over_cdp = with_pages
    try:
        return ps.PlaywrightSession.connect("http://cdp", conversation_url=WANTED)
    finally:
        FakeChromium.connect_over_cdp = original


def test_a_duplicate_of_the_bound_conversation_is_closed(fake_playwright):
    """The orphan. Two tabs on the same conversation, one of them left by a
    session that never got to run `close()`."""
    context = FakeContext((WANTED, WANTED))
    bound, duplicate = context.pages

    session = _connect_with_pages(context)

    assert session._page is bound
    assert duplicate.closed is True
    assert bound.closed is False, "never the page this session bound to"


def test_a_tab_on_a_different_chat_survives_the_reaper(fake_playwright):
    """The bound that keeps this safe: a tab on ANOTHER conversation may be
    deliberate — an operator reading a past chat — so only provable duplicates
    of the bound conversation may be closed."""
    context = FakeContext((WANTED, WANTED, STRAY))
    bound, duplicate, stray = context.pages

    session = _connect_with_pages(context)

    assert session._page is bound
    assert bound.closed is False
    assert duplicate.closed is True
    assert stray.closed is False, "a different conversation is not ours to close"


def test_the_bound_page_is_never_closed_even_when_it_is_the_only_tab(fake_playwright):
    """Identity, not URL: a duplicate has the SAME url by definition, so a
    url-based skip would close exactly the tab we are working in."""
    context = FakeContext((WANTED,))
    (bound,) = context.pages

    session = _connect_with_pages(context)

    assert session._page is bound
    assert bound.closed is False


def test_a_stray_that_refuses_to_close_does_not_abort_the_connect(fake_playwright):
    """Best-effort per candidate, like the existing teardown. A single `try`
    around the whole loop would satisfy 'does not raise' while silently
    abandoning every stray after the first failure."""
    context = FakeContext((WANTED, WANTED, WANTED))
    bound, exploding, second = context.pages

    def boom():
        raise RuntimeError("page is already gone")

    exploding.close = boom

    session = _connect_with_pages(context)  # must not raise

    assert session._page is bound
    assert bound.closed is False
    assert exploding.closed is False  # it refused; nothing we can do
    assert second.closed is True, "one bad stray must not stop the reaping"


def test_no_conversation_means_no_reaping(fake_playwright):
    """`doctor` and the smoke test pick their page by bare substring, so
    'duplicates of whatever we landed on' is the ambiguous case, not a provable
    orphan. Costs no coverage: the next loop `connect` passes a conversation
    and reaps anything doctor orphaned on it."""
    context = FakeContext((WANTED, WANTED))
    bound, other = context.pages

    original = FakeChromium.connect_over_cdp

    def with_pages(self, url):
        self._driver.connects += 1
        browser = FakeBrowser(self._driver)
        browser.contexts = [context]
        return browser

    FakeChromium.connect_over_cdp = with_pages
    try:
        session = ps.PlaywrightSession.connect("http://cdp")
    finally:
        FakeChromium.connect_over_cdp = original

    assert session._page is bound
    assert other.closed is False


def test_opening_our_own_tab_reaps_nothing(fake_playwright):
    """Nothing matched the conversation — by construction there is no
    duplicate to reap, and the stray we declined to adopt stays open."""
    context = FakeContext((STRAY,))
    (stray,) = context.pages

    session = _connect_with_pages(context)

    assert session._opened_page is True
    assert session._page.url == "about:blank"
    assert stray.closed is False


# ---- no browser fault may leave this module as a plain Exception -------------
#
# Observed 2026-08-15: the loop did not park, it DIED. `connect_over_cdp` raised
#
#     Exception: BrowserType.connect_over_cdp: Connection closed while reading
#     from the driver
#
# — a PLAIN `Exception`, because Playwright's `rewrite_error` gives driver-channel
# failures no type of their own. `except playwright.sync_api.Error` therefore did
# not match, the exception reached the top level, and the process ended with
# phase=submitting, stop_reason=None, consecutive_failures=0 and no blocker: from
# the outside, indistinguishable from a clean exit. So the guards here are
# POSITIONAL, and these tests raise things that are NOT `module.Error` —
# `FakeError` is, so a test built on it proves nothing about this bug.

DRIVER_DEAD = "BrowserType.connect_over_cdp: Connection closed while reading from the driver"


def _connect_over_cdp_raises(monkeypatch, exc):
    def boom(self, url):
        self._driver.connects += 1
        raise exc

    monkeypatch.setattr(FakeChromium, "connect_over_cdp", boom)


def test_a_plain_exception_from_the_driver_becomes_a_browser_error(
    fake_playwright, monkeypatch
):
    """The 2026-08-15 crash. A fault with no catchable type must still be a
    `BrowserError`, because that is the only thing `Orchestrator.run` routes to
    a restart, the failure budget and a park."""
    from autoloop.errors import BrowserError

    _connect_over_cdp_raises(monkeypatch, Exception(DRIVER_DEAD))

    with pytest.raises(SessionLostError) as caught:
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    assert isinstance(caught.value, BrowserError)
    assert "Connection closed while reading from the driver" in str(caught.value)
    # The shared driver survives a failed connect, exactly as before.
    assert fake_playwright["drivers"][0].stops == 0


def test_the_converted_error_still_names_the_original_exception_type(
    fake_playwright, monkeypatch
):
    """`_handle_browser_failure` logs `kind=type(exc).__name__`, which from now
    on always reads `SessionLostError`. Without the original type in the text,
    a driver crash and a refused socket look identical in the transcript."""
    _connect_over_cdp_raises(monkeypatch, RuntimeError("driver went away"))

    with pytest.raises(SessionLostError, match="RuntimeError: driver went away"):
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")


def test_a_failure_starting_the_driver_is_routed_too(fake_playwright, monkeypatch):
    """`_driver()` talks to the same subprocess channel, so it dies the same
    way — and it runs inside the same guard."""

    def no_driver():
        raise Exception("Playwright Sync API inside the asyncio loop")

    monkeypatch.setattr(ps, "_driver", no_driver)

    with pytest.raises(SessionLostError, match="asyncio loop"):
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")


def test_an_autoloop_error_is_not_flattened_into_a_transport_fault(
    fake_playwright, monkeypatch
):
    """The guard is wide, not indiscriminate: a deliberate diagnosis raised
    inside the connect keeps its own type and its own routing."""
    from autoloop.errors import LoginExpiredError

    _connect_over_cdp_raises(monkeypatch, LoginExpiredError("profile is logged out"))

    with pytest.raises(LoginExpiredError):
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")


def test_a_dead_driver_channel_mid_operation_is_a_browser_error(fake_playwright):
    """Guarding only `connect` would leave the identical crash reachable from
    every submit and every poll: once the channel is dead, the next `goto`
    raises the same untyped exception."""
    session = ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    def boom(url, wait_until=None):
        raise Exception(DRIVER_DEAD)

    session._page.goto = boom

    with pytest.raises(SessionLostError, match="Connection closed"):
        session.goto("https://chatgpt.com/c/abc")


def test_an_operation_raising_an_autoloop_error_passes_through(fake_playwright):
    from autoloop.errors import LoginExpiredError

    session = ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    def boom(url, wait_until=None):
        raise LoginExpiredError("logged out mid-run")

    session._page.goto = boom

    with pytest.raises(LoginExpiredError):
        session.goto("https://chatgpt.com/c/abc")


# ---- the tail-mount gesture keeps the driver-failure contract ----------------
#
# `scroll_to_end` is the one call here that swallows an exception on purpose: a
# virtualizer detaches the node it was asked to scroll to, and restarting Chrome
# over a repaint would be a worse bug than a gesture that painted nothing. The
# swallow has to stay ELEMENT-LOCAL, because the caller
# (`BrowserChatGPT._mount_message_tail`) reads ABSENCE out of a list that stops
# changing — so a dead driver channel arriving as "the gesture did nothing"
# turns a lost browser into "the request is not in this conversation", which is
# the exact class of confident-wrong answer the search exists to avoid.


class _FakeLocator:
    """Enough of a Playwright locator for the tail-mount gesture: a node count,
    a `scroll_into_view_if_needed` that fails the way a test asks it to, and an
    `evaluate` standing in for the scroll container's own arithmetic —
    `remaining` is how many pixels lie below the viewport, which is what
    decides whether the gesture may claim the list's end."""

    def __init__(self, count, fault=None, remaining=0, evaluate_fault=None):
        self._count = count
        self._fault = fault
        self._remaining = remaining
        self._evaluate_fault = evaluate_fault
        self.scrolls = 0
        self.evaluations = 0

    def count(self):
        return self._count

    def nth(self, index):
        return self

    def scroll_into_view_if_needed(self, timeout=None):
        self.scrolls += 1
        if self._fault is not None:
            raise self._fault

    def evaluate(self, expression):
        self.evaluations += 1
        if self._evaluate_fault is not None:
            raise self._evaluate_fault
        return self._remaining


class _FakeKeyboard:
    def __init__(self):
        self.pressed = []

    def press(self, keys):
        self.pressed.append(keys)


#: Named exactly as Playwright names its own, which is all the predicate can
#: match on: `playwright.sync_api` is imported lazily so that autoloop (this
#: suite included) runs without the package installed, and importing the real
#: class here to name it would defeat that.
_FakeTimeoutError = type("TimeoutError", (Exception,), {})


def _scrolling_session(fault=None, **locator_kwargs):
    session = ps.PlaywrightSession.connect("http://127.0.0.1:9222")
    locator = _FakeLocator(3, fault, **locator_kwargs)
    keyboard = _FakeKeyboard()
    session._page.locator = lambda selector: locator
    session._page.keyboard = keyboard
    return session, locator, keyboard


def test_a_dead_driver_channel_during_a_tail_scroll_is_not_swallowed(fake_playwright):
    """The contract the swallow must not break. Reported as a benign
    no-op, this would leave the mount reading an unpainted list and calling a
    present request absent — with no `BrowserError` for the orchestrator to
    restart or park on."""
    session, _locator, keyboard = _scrolling_session(Exception(DRIVER_DEAD))

    with pytest.raises(SessionLostError, match="Connection closed"):
        session.scroll_to_end("main div")
    # And it aborted rather than half-running the gesture on a dead channel.
    assert keyboard.pressed == []


def test_an_autoloop_error_during_a_tail_scroll_keeps_its_own_type(fake_playwright):
    """A deliberate diagnosis raised under the gesture is routed as itself, not
    demoted to "that node would not scroll"."""
    from autoloop.errors import LoginExpiredError

    session, _locator, keyboard = _scrolling_session(LoginExpiredError("logged out"))

    with pytest.raises(LoginExpiredError):
        session.scroll_to_end("main div")
    assert keyboard.pressed == []


def test_a_node_that_times_out_scrolling_still_presses_end(fake_playwright):
    """The other half: `scroll_into_view_if_needed` retries an unactionable
    element until its own bounded timeout, which is the ordinary shape of "the
    virtualizer moved that node". The End press is the rest of the gesture and
    still runs."""
    session, locator, keyboard = _scrolling_session(_FakeTimeoutError("Timeout 5000ms exceeded."))

    session.scroll_to_end("main div")  # must not raise

    assert locator.scrolls == 1
    assert keyboard.pressed == ["End"]


def test_a_detached_node_message_is_forgiven_too(fake_playwright):
    """The same fault can arrive as a plain error with a message instead of a
    timeout, so the predicate matches both shapes — it is a list of what we
    choose to forgive, not a claim about which one the library raises."""
    session, _locator, keyboard = _scrolling_session(
        FakeError("Element is not attached to the DOM")
    )

    session.scroll_to_end("main div")  # must not raise

    assert keyboard.pressed == ["End"]


def test_an_empty_list_still_presses_end(fake_playwright):
    """Nothing mounted yet is the normal state of a freshly loaded chat, not a
    failure — there is no last node to scroll to, and End is what paints one.
    There is also nothing to measure, so it claims no position."""
    session = ps.PlaywrightSession.connect("http://127.0.0.1:9222")
    keyboard = _FakeKeyboard()
    session._page.locator = lambda selector: _FakeLocator(0)
    session._page.keyboard = keyboard

    assert session.scroll_to_end("main div") is None

    assert keyboard.pressed == ["End"]


# ---- the gesture reports WHERE it got to ------------------------------------
#
# The caller cannot tell "the list is fully mounted" from "the gesture went to
# the wrong element" by watching the window it paints: both leave it unchanged.
# The scroll container can tell, and this is the only place that reads it. A
# `None` here costs the caller the ability to conclude ABSENCE, so the one
# thing this must never do is report a position it did not measure.


def test_a_container_scrolled_to_its_bottom_reports_the_end_of_the_list(fake_playwright):
    """Nothing below the viewport — the gesture arrived, and an unchanged window
    after this is evidence about the conversation."""
    session, locator, _keyboard = _scrolling_session(remaining=0)

    assert session.scroll_to_end("main div") is True
    assert locator.evaluations == 1


def test_a_container_with_more_below_reports_it_is_not_at_the_end(fake_playwright):
    """The gesture ran and the list did not get there. Reported honestly, this
    is what stops a stuck gesture from settling into a false absence."""
    session, _locator, _keyboard = _scrolling_session(remaining=1800)

    assert session.scroll_to_end("main div") is False


def test_a_hair_short_of_the_bottom_still_counts_as_the_end(fake_playwright):
    """Fractional device-pixel ratios and sub-pixel layout leave a container a
    little short of its own maximum even when the browser considers it fully
    scrolled. An exact comparison would report "more to paint" forever and turn
    every absence into a refusal."""
    session, _locator, _keyboard = _scrolling_session(remaining=0.5)

    assert session.scroll_to_end("main div") is True


def test_a_measurement_that_fails_locally_reports_no_position(fake_playwright):
    """Measuring is the same element-local risk as scrolling to the node, and
    fails the same ways. Unmeasured must arrive as unmeasured: reported as
    `True` it would license exactly the confident absence this signal exists to
    prevent."""
    session, _locator, keyboard = _scrolling_session(
        evaluate_fault=FakeError("Element is not attached to the DOM")
    )

    assert session.scroll_to_end("main div") is None
    assert keyboard.pressed == ["End"]  # the gesture itself still ran


def test_a_dead_driver_channel_during_the_measurement_is_not_swallowed(fake_playwright):
    """Same contract as the scroll half: a lost browser is a lost browser, not
    "the position is unknown". Demoted to None it would look like an adapter
    without the capability, and the search would park instead of restarting."""
    session, _locator, _keyboard = _scrolling_session(
        evaluate_fault=Exception(DRIVER_DEAD)
    )

    with pytest.raises(SessionLostError, match="Connection closed"):
        session.scroll_to_end("main div")


def test_a_measurement_that_is_not_a_number_reports_no_position(fake_playwright):
    """The JS returns `null` when it cannot find anything to measure. Truthiness
    is not the test — anything that is not a distance is no evidence."""
    session, _locator, _keyboard = _scrolling_session(remaining=None)

    assert session.scroll_to_end("main div") is None


def test_a_listener_that_cannot_attach_never_ends_the_process(fake_playwright):
    """The observation capability is an optimisation. A driver-channel failure
    while attaching it used to escape as a plain Exception, from a call site
    that exists only to make diagnostics nicer."""
    session = ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    def boom(event, handler):
        raise Exception(DRIVER_DEAD)

    session._page.on = boom

    session.start_send_observation()  # must not raise

    assert session._listening is False


# ---- the park has to say WHICH browser fault this was ------------------------
#
# "browser process alive, port dead" and "no browser at all" need opposite
# operator actions — restart the wedged one (it can ignore SIGTERM) versus start
# one — and the old message described both as "cannot connect". The endpoint is
# probed separately from the port because a third case is real: a Chrome whose
# `/json/version` answers 200 while CDP itself is wedged (2026-08-14).
#
# Every one of those measurements is LOCAL — the probes dial 127.0.0.1 and `ps`
# lists this machine — so an endpoint that cannot be probed from here (another
# host, or no usable port) gets NONE of it: every field unknown, and no advice
# about the local dedicated profile. A local Chrome is not evidence about a
# browser somewhere else.


def _machine(monkeypatch, *, processes=(), port_open=False, answering=False):
    monkeypatch.setattr(ps, "_list_processes", lambda: list(processes))
    monkeypatch.setattr(ps, "_port_in_use", lambda port: port_open)
    monkeypatch.setattr(ps, "_endpoint_ready", lambda port: answering)


def test_the_diagnosis_reports_a_running_browser_that_stopped_serving_cdp(monkeypatch):
    _machine(
        monkeypatch,
        processes=[(4711, "/Applications/Chrome --user-data-dir=/tmp/autoloop-chrome-test")],
        port_open=True,
        answering=False,
    )

    described = ps.describe_cdp_endpoint("http://127.0.0.1:9222")

    assert "endpoint=http://127.0.0.1:9222" in described
    assert "port_open=yes" in described
    assert "cdp_answering=no" in described
    assert "chrome_on_profile=1 (pid 4711)" in described
    assert "Chrome IS running" in described


def test_the_diagnosis_reports_when_there_is_no_browser_at_all(monkeypatch):
    _machine(monkeypatch, processes=[], port_open=False, answering=False)

    described = ps.describe_cdp_endpoint("http://127.0.0.1:9222")

    assert "chrome_on_profile=0" in described
    assert "chrome_on_port=0" in described
    assert "NO Chrome is running" in described


def test_helper_processes_are_not_evidence_of_a_running_browser(monkeypatch):
    """Every renderer inherits `--user-data-dir` and `--remote-debugging-port`,
    so counting them reports a live browser from the children of a dead one —
    the same mistake that made the old restart script kill a renderer and
    report success."""
    _machine(
        monkeypatch,
        processes=[
            (5001, "/Applications/Chrome --type=renderer --user-data-dir=/tmp/autoloop-chrome-test"),
            (5002, "/Applications/Chrome --type=gpu-process --remote-debugging-port=9222"),
        ],
    )

    described = ps.describe_cdp_endpoint("http://127.0.0.1:9222")

    assert "chrome_on_profile=0" in described
    assert "chrome_on_port=0" in described
    assert "NO Chrome is running" in described


def test_a_process_on_the_debug_port_counts_even_under_another_profile(monkeypatch):
    """The profile is a guess (an env default); the port is what we actually
    failed to reach, so a browser holding it is reported either way."""
    _machine(
        monkeypatch,
        processes=[(6001, "/Applications/Chrome --user-data-dir=/tmp/other --remote-debugging-port=9222")],
        port_open=True,
    )

    described = ps.describe_cdp_endpoint("http://127.0.0.1:9222")

    assert "chrome_on_profile=0" in described
    assert "chrome_on_port=1 (pid 6001)" in described
    assert "Chrome IS running" in described


def test_a_non_local_endpoint_is_reported_unknown_rather_than_measured(monkeypatch):
    """The probes talk to 127.0.0.1. Reporting their answer for another host
    would be a confident wrong diagnosis, which is worse than none."""
    _machine(monkeypatch, port_open=True, answering=True)

    described = ps.describe_cdp_endpoint("http://gpu-box:9222")

    assert "port_open=unknown" in described
    assert "cdp_answering=unknown" in described


def test_a_non_local_endpoint_is_not_diagnosed_from_local_chrome_processes(monkeypatch):
    """The sharp version of the test above, and the one that fails against
    unknown port fields bolted onto a local verdict.

    `ps` lists THIS machine, so a Chrome here is not evidence about a browser on
    another host — not even one matching the dedicated profile AND holding 9222,
    which is exactly the machine an operator running a local loop also has. If
    the pid counts or the ACTION come from that scan, `http://gpu-box:9222` gets
    diagnosed from an unrelated local Chrome and the operator is sent to restart
    the wrong browser.
    """
    scans = {"count": 0}

    def counted():
        scans["count"] += 1
        return [
            (
                4711,
                "/Applications/Chrome --user-data-dir=/tmp/autoloop-chrome-test "
                "--remote-debugging-port=9222",
            )
        ]

    monkeypatch.setattr(ps, "_list_processes", counted)
    monkeypatch.setattr(ps, "_port_in_use", lambda port: True)
    monkeypatch.setattr(ps, "_endpoint_ready", lambda port: True)

    described = ps.describe_cdp_endpoint("http://gpu-box:9222")

    assert "chrome_on_profile=unknown" in described
    assert "chrome_on_port=unknown" in described
    assert "4711" not in described, "a local pid says nothing about another host"
    assert "Chrome IS running" not in described
    assert "NO Chrome is running" not in described
    assert "chrome_restart" not in described, "never the local profile for a remote endpoint"
    assert "gpu-box" in described, "and it still has to say where to look instead"
    assert scans["count"] == 0, "nor spend a 30s `ps` on an answer it cannot use"


def test_an_endpoint_with_no_usable_port_is_not_diagnosed_from_local_chrome(monkeypatch):
    """The other unprobeable shape, held to the same rule. With no port there is
    nothing to probe and nothing a local browser can settle — the url itself is
    the fault — so a running local Chrome must not turn that into 'restart the
    dedicated profile', which would leave the broken url in place."""
    _machine(
        monkeypatch,
        processes=[(4711, "/Applications/Chrome --user-data-dir=/tmp/autoloop-chrome-test")],
        port_open=True,
    )

    described = ps.describe_cdp_endpoint("http://127.0.0.1")

    assert "port_open=unknown" in described
    assert "chrome_on_profile=unknown" in described
    assert "chrome_on_port=unknown" in described
    assert "Chrome IS running" not in described
    assert "fix the CDP url" in described
    assert "endpoint=http://127.0.0.1" in described


def test_an_unparseable_endpoint_is_diagnosed_rather_than_crashing(monkeypatch):
    """Same branch, reached the other way: `:not-a-port` raises out of the url
    parse instead of merely being absent. Which of the two it is must not decide
    whether the operator gets a diagnosis at all — hence `_endpoint_host_port`
    guards the whole `urlsplit`, not just the `.port` read, and the operator is
    told to fix the url rather than handed `diagnosis=unavailable`."""
    _machine(
        monkeypatch,
        processes=[(4711, "/Applications/Chrome --user-data-dir=/tmp/autoloop-chrome-test")],
        port_open=True,
    )

    described = ps.describe_cdp_endpoint("http://127.0.0.1:not-a-port")

    assert "fix the CDP url" in described
    assert "diagnosis=unavailable" not in described, "a bad url is diagnosable, not a crash"
    assert "chrome_on_profile=unknown" in described
    assert "Chrome IS running" not in described


def test_the_diagnosis_reaches_the_raised_error(fake_playwright, monkeypatch):
    """It is only worth measuring if it survives into the park: the message is
    what `stop_reason` and the blocker question carry."""
    _connect_over_cdp_raises(monkeypatch, Exception(DRIVER_DEAD))
    _machine(
        monkeypatch,
        processes=[(4711, "/Applications/Chrome --user-data-dir=/tmp/autoloop-chrome-test")],
        port_open=True,
    )

    with pytest.raises(SessionLostError) as caught:
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    message = str(caught.value)
    assert "http://127.0.0.1:9222" in message
    assert "chrome_on_profile=1 (pid 4711)" in message
    assert "cdp_answering=no" in message


def test_the_action_survives_a_160_character_summary(fake_playwright, monkeypatch):
    """`autoloop start` prints `blocker.question[:160]`. Ordered evidence-first
    — which is how this was first written — that view shows four key=value
    pairs and cuts off the one sentence saying what to do."""
    _connect_over_cdp_raises(monkeypatch, Exception(DRIVER_DEAD))
    _machine(
        monkeypatch,
        processes=[(4711, "/Applications/Chrome --user-data-dir=/tmp/autoloop-chrome-test")],
        port_open=True,
    )

    with pytest.raises(SessionLostError) as caught:
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    summary = str(caught.value)[:160]
    assert "restart the dedicated profile" in summary
    assert "9222" in summary, "and which endpoint it is about"


def test_a_diagnosis_that_throws_never_becomes_the_failure(fake_playwright, monkeypatch):
    """`ps` can time out, a socket probe can raise. The thing added to prevent a
    crash must not be the crash — on the one path where a second exception is
    fatal."""
    _connect_over_cdp_raises(monkeypatch, Exception(DRIVER_DEAD))

    def no_ps():
        raise OSError("ps: command not found")

    monkeypatch.setattr(ps, "_list_processes", no_ps)

    with pytest.raises(SessionLostError) as caught:
        ps.PlaywrightSession.connect("http://127.0.0.1:9222")

    message = str(caught.value)
    assert "diagnosis=unavailable" in message
    assert "Connection closed while reading from the driver" in message


# ---- the state /json/version cannot see --------------------------------------
#
# 2026-08-17: the operator closed the browser WINDOW. Chrome stayed alive,
# `/json/version` kept answering with a valid `webSocketDebuggerUrl` — so every
# check built on it called the browser healthy — and `/json/list` returned ZERO
# targets. Playwright could not attach at all. Restarting restored 11 targets.


def _json_list(monkeypatch, body, status=200):
    """Answer `/json/list` with `body`, and record which URL was asked."""
    asked = []

    class _Response:
        def __init__(self):
            self.status = status

        def read(self, _limit=None):
            return body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def urlopen(url, timeout):
        asked.append(url)
        return _Response()

    monkeypatch.setattr(ps, "_urlopen", urlopen)
    return asked


def test_a_browser_with_no_page_targets_reports_zero(monkeypatch):
    """The window-closed state, and the whole reason this probe exists: nothing
    about `/json/version` distinguishes it from a working browser."""
    asked = _json_list(monkeypatch, "[]")

    assert ps.attachable_page_targets("http://127.0.0.1:9222") == 0
    assert asked == ["http://127.0.0.1:9222/json/list"]


def test_only_page_targets_count(monkeypatch):
    """A service worker is not somewhere a conversation can be driven, so a
    browser holding nothing else is still unattachable for this purpose."""
    body = (
        '[{"type": "page", "id": "1"}, {"type": "service_worker", "id": "2"}, '
        '{"type": "iframe", "id": "3"}]'
    )
    _json_list(monkeypatch, body)

    assert ps.attachable_page_targets("http://127.0.0.1:9222") == 1


def test_a_healthy_browser_reports_its_pages(monkeypatch):
    _json_list(monkeypatch, json.dumps([{"type": "page"} for _ in range(11)]))

    assert ps.attachable_page_targets("http://127.0.0.1:9222") == 11


def test_an_unreachable_endpoint_is_unmeasurable_not_zero(monkeypatch):
    """The distinction the caller acts on. Zero means "the browser answered and
    named no page", which authorises a restart; an endpoint that answers
    nothing is the ORDINARY browser fault, which already diagnoses and
    restarts itself on its own budget — reporting it as zero here would move
    that decision into the rate-limit path."""

    def refused(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(ps, "_urlopen", refused)

    assert ps.attachable_page_targets("http://127.0.0.1:9222") is None


def test_a_non_200_or_unparseable_answer_is_unmeasurable(monkeypatch):
    _json_list(monkeypatch, "[]", status=500)
    assert ps.attachable_page_targets("http://127.0.0.1:9222") is None

    _json_list(monkeypatch, "<html>not the CDP endpoint</html>")
    assert ps.attachable_page_targets("http://127.0.0.1:9222") is None

    # A dict is what /json/version answers with — right endpoint, wrong path.
    assert ps.count_page_targets('{"webSocketDebuggerUrl": "ws://x"}') is None


def test_the_probe_is_built_for_the_endpoint_it_is_given(monkeypatch):
    """A remote CDP endpoint is asked about ITSELF — unlike the local port/`ps`
    probes, an HTTP GET to `/json/list` describes the browser at the other end
    rather than this machine."""
    asked = _json_list(monkeypatch, "[]")

    ps.attachable_page_targets("gpu-box:9222")

    assert asked == ["http://gpu-box:9222/json/list"]
    assert ps.attachable_page_targets("") is None, "and an empty endpoint asks nothing"
