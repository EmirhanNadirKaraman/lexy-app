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
    url = "https://chatgpt.com/c/abc"


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]

    def new_page(self):
        page = FakePage()
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
