import sys
from pathlib import Path

import pytest

# Make `import autoloop` work regardless of where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def _no_live_cdp_probe(monkeypatch):
    """Keep the one probe that dials a socket out of a hermetic suite.

    `Orchestrator._attachable_page_targets` asks the CONFIGURED CDP endpoint
    how many pages it has, and the configured endpoint on a developer machine
    is a real Chrome on 127.0.0.1:9222. Left live, every rate-limit test would
    classify against whatever browser happens to be running on the machine —
    and on one with Chrome up and no tabs open, the tests that pin "a throttle
    never restarts the browser" would restart it.

    Stubbed to None, which means UNMEASURABLE, because that is the value that
    leaves the classifier behaving exactly as it did before the probe existed.
    A test about the classification sets its own (`orch._attachable_page_targets
    = lambda: 0`); an instance attribute shadows this.
    """
    from autoloop.orchestrator import Orchestrator

    monkeypatch.setattr(Orchestrator, "_attachable_page_targets", lambda self: None)
