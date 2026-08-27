"""Where the shared transport vocabulary LIVES, and what the move did not change.

`SubmitResult` and `SendOutcome` are the words every provider speaks: the
orchestrator branches on both on the codex path, which has no browser in it at
all. They were defined inside `autoloop/browser/` until brw-17 (2026-08-27) and
are defined in `autoloop/conversation.py` now, so retiring the browser transport
(brw-18) takes nothing live with it.

Three claims, and each is here because breaking it fails SILENTLY:

* **Identity, not equality.** Every caller compares with `is`
  (`orchestrator._submit_request`, `inbox.provider_asker`). Both enums subclass
  `str`, so a SECOND definition of `SubmitResult` would still satisfy
  `== "rejected"` — an equality-based test would stay green while every `is`
  check in the loop silently went False. The re-export must therefore be the
  same object, not a copy.
* **The values are a STORAGE FORMAT.** `Orchestrator._submit_request` writes
  `SendOutcome.REJECTED.value` to `PendingRequest.last_send_outcome`, which
  lands in `state.json` and is read back on the next start. A renamed value
  misreads a resumed run whose state predates the rename, and nothing raises.
* **Live modules no longer import a retired transport.** Pinned by reading the
  source of `autoloop/*.py`, because an import that creeps back is invisible
  until brw-18 deletes the package underneath it. `autoloop/codex/` is NOT
  scanned: both adapters there still reach `SubmitResult` through the re-export,
  that package was outside brw-17's approved scope, and repointing those two
  lines is brw-18's job. Scanning it would fail on a state this task was told to
  leave standing.
"""

import re
from pathlib import Path

import autoloop
from autoloop.browser import chatgpt as browser_chatgpt
from autoloop.browser import observation as browser_observation
from autoloop.codex import app_server_conversation as codex_app_server_conversation
from autoloop.codex import conversation as codex_conversation
from autoloop.conversation import SendOutcome, SubmitResult

PACKAGE_DIR = Path(autoloop.__file__).resolve().parent

#: Matches an import statement, indented or not, that reaches into the browser
#: package — by relative path (`from .browser…`, the form the live modules use)
#: OR by absolute one (`from autoloop.browser…`, `import autoloop.browser…`).
#: Both, deliberately: brw-17's own DONE MEANS greps for the relative form only,
#: and a guard that watches one spelling is a guard the other spelling walks
#: past. Deliberately NOT matched: prose and operator advice naming
#: `autoloop.browser.chrome_restart`, which is a command line rather than a
#: dependency — `config.RESTART_COMMAND_REPLACEMENT` is the shipped example.
_BROWSER_IMPORT = re.compile(
    r"^\s*(?:from \.{1,2}|from autoloop\.|import autoloop\.)browser(?:[.\s]|$)"
)

#: The ONE live import of the browser package that brw-17 deliberately leaves
#: standing: `attachable_page_targets` is genuinely browser-specific, and brw-18
#: removes it together with the code path that calls it.
_ALLOWED_BROWSER_IMPORT = "from .browser.playwright_session import attachable_page_targets"


# ---- the vocabulary is defined here, and reachable from where it used to be --


def test_the_vocabulary_is_defined_in_the_transport_neutral_module():
    assert SubmitResult.__module__ == "autoloop.conversation"
    assert SendOutcome.__module__ == "autoloop.conversation"


def test_every_import_path_yields_THE_SAME_enum_object():
    """`is` comparisons are what the orchestrator uses; a copy would pass `==`."""
    assert browser_chatgpt.SubmitResult is SubmitResult
    assert browser_observation.SendOutcome is SendOutcome
    assert codex_conversation.SubmitResult is SubmitResult
    assert codex_app_server_conversation.SubmitResult is SubmitResult
    assert browser_chatgpt.SubmitResult.REJECTED is SubmitResult.REJECTED
    assert browser_observation.SendOutcome.REJECTED is SendOutcome.REJECTED


def test_submit_result_members_and_values_are_unchanged():
    assert [member.name for member in SubmitResult] == [
        "ALREADY_PERSISTED",
        "CONFIRMED",
        "UNCONFIRMED",
        "REJECTED",
    ]
    assert SubmitResult.ALREADY_PERSISTED.value == "already_persisted"
    assert SubmitResult.CONFIRMED.value == "confirmed"
    assert SubmitResult.UNCONFIRMED.value == "unconfirmed"
    assert SubmitResult.REJECTED.value == "rejected"


def test_send_outcome_members_and_values_are_unchanged():
    """These three strings are what `state.json` holds — see the module
    docstring. A rename here breaks a resumed run, not this test's own file."""
    assert [member.name for member in SendOutcome] == [
        "ACCEPTED",
        "REJECTED",
        "UNKNOWN",
    ]
    assert SendOutcome.ACCEPTED.value == "accepted"
    assert SendOutcome.REJECTED.value == "rejected"
    assert SendOutcome.UNKNOWN.value == "unknown"


def test_both_are_still_str_enums_so_a_persisted_value_compares_equal():
    """The `str` mixin is why `req.last_send_outcome == SendOutcome.REJECTED.value`
    reads a plain string out of state.json and still matches."""
    assert issubclass(SubmitResult, str)
    assert issubclass(SendOutcome, str)
    assert SendOutcome("rejected") is SendOutcome.REJECTED
    assert SubmitResult("unconfirmed") is SubmitResult.UNCONFIRMED


# ---- no live module imports the retired transport ----------------------------


def test_no_top_level_module_imports_the_browser_package_except_the_one():
    """brw-17's DONE MEANS, asserted on the source rather than on a symptom.

    Reads every `autoloop/*.py` — the package's own live modules, not
    `autoloop/browser/` (which may import itself) and not `autoloop/tests/`.

    The population is asserted BEFORE the scan, in this same function: a glob
    that matched nothing (a moved package, a renamed directory) would report
    "clean" about a tree it never read, which is the fail-open shape this test
    exists to catch in other people's code."""
    scanned = sorted(PACKAGE_DIR.glob("*.py"))
    assert len(scanned) > 20, f"the scan read almost nothing — {PACKAGE_DIR} is wrong"
    offenders = []
    for module in scanned:
        for lineno, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not _BROWSER_IMPORT.match(line):
                continue
            if line.strip() == _ALLOWED_BROWSER_IMPORT:
                continue
            offenders.append(f"{module.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "live modules must not import the retired browser transport; move the "
        f"shared name into conversation.py instead: {offenders}"
    )


def test_the_one_allowed_browser_import_is_still_there():
    """The negative test above would also pass if `attachable_page_targets` had
    been removed — which is brw-18's job, not brw-17's. Pin that this round left
    it alone, so the test above cannot go green by deleting the wrong thing."""
    orchestrator_src = (PACKAGE_DIR / "orchestrator.py").read_text(encoding="utf-8")
    assert _ALLOWED_BROWSER_IMPORT in orchestrator_src


def test_the_browser_package_still_imports_and_still_speaks_the_vocabulary():
    """Constraint 3: brw-17 moves code, it does not retire the transport. The
    adapter must still be importable and still return these members."""
    assert browser_chatgpt.BrowserChatGPT.supports_chunked_delivery is True
    assert browser_observation.classify_submission([]) is SendOutcome.UNKNOWN
    observed = [browser_observation.SendObservation("/backend-api/conversation", 200)]
    assert browser_observation.classify_submission(observed) is SendOutcome.ACCEPTED
