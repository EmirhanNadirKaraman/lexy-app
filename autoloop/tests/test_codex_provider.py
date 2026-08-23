"""The Codex CLI reviewer and the recorded quota failover.

Three things are being defended here.

**Nothing the loop SENT may classify what came back, and every failure must
leave a record** (quota-01, 2026-08-23). `codex exec` echoes the whole prompt
onto stderr, so a packet quoting a line number used to park the loop loop_fatal
on a spent allowance it did not have. The guard is asserted from both ends —
an echoed packet never classifies as exhaustion, a genuine exhaustion message
still does — and the record is asserted against the TRANSCRIPT FILE, because
`codex_invocation_failed` was written on every failure for 24 days and reached
that file zero times.

**The adapter must not inherit ambiguity it does not have.** The browser
transport's UNCONFIRMED / reconcile / rotate machinery exists because a DOM
cannot say whether a message was delivered. A subprocess can. So a failed
`codex exec` is REJECTED and retryable, never a park — and the orchestrator
learns that from the `idempotent_submit` capability rather than from the
adapter lying about `send_attempted`.

**A failover must stay attributable.** The reviewer grants authority: its
approval carries the stamp that authorizes a commit. Handing the role to a
different provider mid-run is safe only when nothing was authorized under the
old one, and acceptable only when the record says which reviewer produced which
answer.

No codex binary is involved anywhere in this file — `CodexRunner` is a protocol
and every test injects a fake, exactly as `audit/agents.py` does for `claude`.
"""

import inspect
import json

import pytest

from autoloop.browser.chatgpt import SubmitResult
from autoloop.codex.conversation import (
    CodexConversation,
    CodexResult,
    SubprocessCodexRunner,
)
from autoloop.codex.quota import (
    DEFAULT_QUOTA_PATTERNS,
    DEFAULT_RATE_LIMIT_PATTERNS,
    NOT_A_FAILURE,
    QUOTA_EXHAUSTED,
    RATE_LIMITED,
    UNCLASSIFIED,
    classify,
    failure_digest,
    is_quota_exhausted,
    strip_echoed_prompt,
)
from autoloop.config import AutoloopConfig, BrowserConfig, CodexConfig, ConversationConfig
from autoloop.conversation import available_providers, create_conversation
from autoloop.errors import QuotaExhaustedError, ResponseTimeoutError
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

from test_orchestrator import FakeExecutor, FakeGit, block  # noqa: E402 - see conftest sys.path

RID = "alr-codex-0001"
PROMPT = f"[autoloop request {RID} | iteration 1]\n\nbody"


def stop_block(reason="all done"):
    return block({"version": 3, "decision": "stop", "reason": reason})


class FakeRunner:
    """Scripted `codex exec`. `results` is consumed one per call; the tail
    repeats, so a test scripts only the interesting invocations."""

    def __init__(self, results):
        self.results = list(results)
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def result(stdout="", stderr="", returncode=0):
    return CodexResult(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        duration_seconds=0.1,
        command=("codex", "exec"),
    )


# ---- the quota classifier (pure, so it is testable without the binary) ------

#: A prompt that carries none of the markers, for the cases where the guard is
#: not what is under test.
CLEAN = "review this diff and answer with the contract"


def test_a_successful_run_is_never_exhaustion():
    """OpenAI's documented behaviour is a soft stop: a turn already in flight
    finishes. A zero exit that mentions limits produced a review — use it."""
    assert not is_quota_exhausted(0, "you have hit your usage limit", "", CLEAN)
    assert not is_quota_exhausted(0, "", "rate limit warning", CLEAN)


def test_returncode_zero_is_never_a_failure_whatever_the_output_says():
    """The zero check sits in front of BOTH classifiers, not only the
    exhaustion one — a transient marker on a successful run is a warning
    printed alongside a review that the loop should use."""
    for stream in ("you have hit your usage limit", "429 too many requests"):
        assert classify(0, stream, stream, CLEAN).kind == NOT_A_FAILURE
        assert classify(0, "", stream, CLEAN).matched == ""


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your usage limit for Codex.",
        "quota exceeded for this plan",
        "You are out of credits. Purchase additional credits to continue.",
        "Your weekly limit resets on Monday.",
        "Error: plan limit reached — upgrade your plan to continue.",
    ],
)
def test_recognised_exhaustion_wordings(text):
    """A GENUINE spent-allowance message still classifies. The guard removes
    the loop's own words from evidence; it does not remove codex's."""
    assert is_quota_exhausted(1, "", text, CLEAN)
    assert is_quota_exhausted(1, text, "", CLEAN)
    assert classify(1, "", text, CLEAN).kind == QUOTA_EXHAUSTED


@pytest.mark.parametrize(
    "text",
    [
        "Error: rate limit exceeded",
        "429 Too Many Requests",
        "stream error: 429; retry after 30s",
        "slow down — you are sending requests too quickly",
    ],
)
def test_a_transient_throttle_is_not_a_spent_allowance(text):
    """Two different events with two different remedies. A short-window limit
    clears on a server-side timer; a weekly allowance does not. Only the second
    may reach `QuotaExhaustedError`, which is loop_fatal with no retry path."""
    verdict = classify(1, "", text, CLEAN)
    assert verdict.kind == RATE_LIMITED
    assert verdict.is_transient and not verdict.is_exhaustion
    assert not is_quota_exhausted(1, "", text, CLEAN)


def test_spent_wins_over_transient_when_a_message_carries_both():
    verdict = classify(1, "", "429: you have hit your usage limit, resets Monday", CLEAN)
    assert verdict.kind == QUOTA_EXHAUSTED


def test_an_unrecognised_failure_is_not_exhaustion():
    """Degrades to an ordinary failure — noisy, never unsafe. An unrecognised
    failure authorizes nothing, and re-running a stateless call cannot
    double-post."""
    assert not is_quota_exhausted(1, "", "segmentation fault", CLEAN)
    assert classify(1, "", "segmentation fault", CLEAN).kind == UNCLASSIFIED


def test_patterns_are_overridable_without_touching_code():
    assert not is_quota_exhausted(1, "", "kaputt: cool down", CLEAN)
    assert is_quota_exhausted(1, "", "kaputt: cool down", CLEAN, patterns=("kaputt",))


def test_an_empty_pattern_cannot_classify_everything_as_exhaustion():
    """The empty string is a substring of every stream, so one blank entry in a
    hand-edited `codex.quota_patterns` would park the loop on any failure at
    all — the original defect by a second door."""
    assert not is_quota_exhausted(1, "", "segmentation fault", CLEAN, patterns=("", "  "))
    assert (
        classify(
            1,
            "",
            "segmentation fault",
            CLEAN,
            quota_patterns=("",),
            rate_limit_patterns=("  ",),
        ).kind
        == UNCLASSIFIED
    )


# ---- the guard: what the loop SENT is never evidence -------------------------

#: The shape that parked the loop twice on 2026-08-22. A review packet that
#: quotes a line number, discusses rate limiting and names quotas — all three
#: bare markers the old list matched on.
LOUD_PROMPT = (
    "[autoloop request alr-codex-0009 | iteration 4]\n\n"
    "Task quota-01: the loop must not read its own prompt as an exhausted\n"
    "allowance. See docs/autoloop.md:4295. DEFAULT_QUOTA_PATTERNS contains\n"
    "the bare substrings 429, quota and rate limit, so any non-zero exit\n"
    "reads as a spent allowance. Too many requests is in there too.\n"
)


@pytest.mark.parametrize("returncode", [1, 2, 137])
def test_an_echoed_prompt_cannot_declare_the_allowance_spent(returncode):
    """`codex exec` echoes the WHOLE prompt back on stderr — measured, 180,024
    bytes. So every word of the review packet is inside the string being
    matched, and before the guard a packet quoting a line number turned any
    failure into a loop_fatal park on an account 4% through its window."""
    verdict = classify(returncode, "", LOUD_PROMPT, LOUD_PROMPT)
    assert verdict.kind == UNCLASSIFIED
    assert not verdict.is_exhaustion
    # And the guard is VISIBLE: the markers it refused to count are named.
    assert "429" in verdict.suppressed
    assert "rate limit" in verdict.suppressed


def test_the_guard_survives_framing_the_echo_is_wrapped_in():
    """Nothing here recognises the echo's SHAPE, which is why a codex build
    that reframes, indents or reflows its echo cannot reopen the hole."""
    stderr = (
        "codex 0.9.1\n----- prompt -----\n"
        + "\n".join("    " + line for line in LOUD_PROMPT.splitlines())
        + "\n----- end -----\nError: connection reset by peer\n"
    )
    verdict = classify(1, "", stderr, LOUD_PROMPT)
    assert verdict.kind == UNCLASSIFIED


def test_the_guard_removes_only_the_markers_the_prompt_actually_carries():
    """Not a blanket switch-off. A prompt that mentions rate limiting still
    lets an unrelated exhaustion wording through."""
    verdict = classify(1, "", LOUD_PROMPT + "\nError: quota exceeded\n", LOUD_PROMPT)
    assert verdict.kind == QUOTA_EXHAUSTED
    assert verdict.matched == "quota exceeded"


def test_a_prompt_that_quotes_the_exhaustion_wording_degrades_to_a_retry():
    """The stated price of the guard, pinned so nobody discovers it by
    surprise: when the packet itself contains the wording, a genuine exhaustion
    that round reads as an ordinary failure. Retryable and noisy, which is the
    direction this task asks for — fewer FALSE parks."""
    prompt = "the CLI prints 'quota exceeded' when the plan is spent"
    verdict = classify(1, "", "Error: quota exceeded", prompt)
    assert verdict.kind == UNCLASSIFIED
    assert verdict.suppressed == ("quota exceeded",)


def test_classification_cannot_be_called_without_the_prompt_it_sent():
    """A default on `prompt` would let a call site forget it and silently turn
    the guard off — an alarm that disables itself when its input is missing."""
    for func in (classify, is_quota_exhausted, failure_digest, strip_echoed_prompt):
        parameter = inspect.signature(func).parameters["prompt"]
        assert parameter.default is inspect.Parameter.empty, func.__name__


# ---- the digest: a failure must stay diagnosable -----------------------------


def test_failure_digest_is_bounded_and_carries_no_prompt_echo():
    digest = failure_digest(7, "", "x" * 5000, CLEAN)
    assert digest["returncode"] == 7
    assert len(digest["stderr_tail"]) <= 400
    assert digest["stderr_chars"] == 5000
    assert digest["classification"] == UNCLASSIFIED


def test_a_diagnostic_before_the_echo_survives_the_strip():
    """The finding that refused round 1 of this task. Real stderr is
    `Error: <the useful line>` followed by the echo; a strip that keeps only
    what FOLLOWS the echo deletes the diagnostic and can return empty."""
    stderr = f"Error: model 'gpt-5-codex' is not available\n{LOUD_PROMPT}"
    digest = failure_digest(1, "", stderr, LOUD_PROMPT)
    assert "is not available" in digest["stderr_tail"]
    assert digest["prompt_echo_chars"] >= len(LOUD_PROMPT.strip())


def test_text_on_both_sides_of_an_echo_survives():
    stderr = f"Error: before the echo\n{LOUD_PROMPT}\nError: after the echo\n"
    digest = failure_digest(1, "", stderr, LOUD_PROMPT)
    assert "before the echo" in digest["stderr_tail"]
    assert "after the echo" in digest["stderr_tail"]


def test_a_stdout_only_failure_still_records_its_diagnostic():
    """`codex exec` can die before it writes to stderr. Classifying such a
    failure while recording nothing about it is how an operator ends up
    starting from the account API."""
    digest = failure_digest(1, "fatal: could not read auth.json", "", CLEAN)
    assert "could not read auth.json" in digest["stdout_tail"]
    assert digest["stdout_chars"] == len("fatal: could not read auth.json")


def test_a_failure_that_printed_only_the_echo_still_says_so():
    """Never an empty record. When every byte codex printed was our own text
    coming back, the digest says that and the exit code carries the meaning."""
    digest = failure_digest(9, "", LOUD_PROMPT, LOUD_PROMPT)
    assert digest["stderr_tail"] == ""
    assert digest["stdout_tail"] == ""
    assert "echo of the prompt" in digest["note"]
    assert digest["returncode"] == 9


def test_a_failure_that_printed_nothing_at_all_still_says_so():
    digest = failure_digest(11, "", "", CLEAN)
    assert "printed nothing at all" in digest["note"]


def test_the_digest_names_the_marker_that_decided_the_routing():
    verdict = classify(1, "", "Error: quota exceeded", CLEAN)
    digest = failure_digest(1, "", "Error: quota exceeded", CLEAN, classification=verdict)
    assert digest["classification"] == QUOTA_EXHAUSTED
    assert digest["matched_pattern"] == "quota exceeded"


def test_the_digest_names_the_markers_the_guard_refused_to_count():
    """Not silently swallowed: an operator reading the transcript can see the
    guard fire rather than wonder why an exhaustion was never recognised."""
    verdict = classify(1, "", LOUD_PROMPT, LOUD_PROMPT)
    digest = failure_digest(1, "", LOUD_PROMPT, LOUD_PROMPT, classification=verdict)
    assert "429" in digest["suppressed_patterns"]


def test_stripping_an_echo_never_glues_two_diagnostics_together():
    text, removed = strip_echoed_prompt(f"Error: A{LOUD_PROMPT}Error: B", LOUD_PROMPT)
    assert "Error: AError: B" not in text
    assert "Error: A" in text and "Error: B" in text
    assert removed == len(LOUD_PROMPT)


def test_stripping_tolerates_an_absent_or_empty_stream():
    assert strip_echoed_prompt("", LOUD_PROMPT) == ("", 0)
    assert strip_echoed_prompt("Error: boom", "") == ("Error: boom", 0)


# ---- the adapter -----------------------------------------------------------


def test_a_successful_invocation_confirms_and_returns_the_reply():
    runner = FakeRunner([result(stdout=stop_block())])
    codex = CodexConversation(runner)
    assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED
    assert codex.await_response(RID) == stop_block()
    assert runner.prompts == [PROMPT]


def test_submit_never_returns_unconfirmed():
    """There is no state in which 'we might have sent something' is true, so
    the adapter must not manufacture one."""
    for res in (result(returncode=1, stderr="boom"), result(stdout="   ")):
        codex = CodexConversation(FakeRunner([res]))
        assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED


def test_a_clean_exit_with_empty_stdout_is_rejected_not_confirmed():
    """Calling it CONFIRMED would hand the contract parser an empty string and
    spend a parse retry saying so."""
    codex = CodexConversation(FakeRunner([result(stdout="", returncode=0)]))
    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED


def test_reconcile_is_authoritative_within_the_process():
    codex = CodexConversation(FakeRunner([result(stdout=stop_block())]))
    assert codex.reconcile(RID) is False
    codex.submit(RID, PROMPT)
    assert codex.reconcile(RID) is True
    assert codex.has_request(RID) is True


def test_resubmitting_a_captured_request_sends_nothing():
    runner = FakeRunner([result(stdout=stop_block())])
    codex = CodexConversation(runner)
    codex.submit(RID, PROMPT)
    assert codex.submit(RID, PROMPT) is SubmitResult.ALREADY_PERSISTED
    assert len(runner.prompts) == 1


def test_awaiting_an_uncaptured_request_raises_rather_than_hanging():
    codex = CodexConversation(FakeRunner([result(stdout="x")]))
    with pytest.raises(ResponseTimeoutError):
        codex.await_response(RID)


def test_exhausted_allowance_raises_quota_exhausted():
    codex = CodexConversation(
        FakeRunner([result(returncode=1, stderr="You've hit your usage limit")])
    )
    with pytest.raises(QuotaExhaustedError) as exc:
        codex.submit(RID, PROMPT)
    # The message explains why a fallback is worth trying at all.
    assert "separate quota" in str(exc.value)


def test_every_failed_invocation_is_logged_for_diagnosis():
    """This is what turns the first real exhaustion into a config edit instead
    of an investigation, so it must fire for UNRECOGNISED failures too."""
    logged = []
    codex = CodexConversation(
        FakeRunner([result(returncode=3, stderr="something unfamiliar")]),
        log=lambda event, data: logged.append((event, data)),
    )
    codex.submit(RID, PROMPT)
    assert logged and logged[0][0] == "codex_invocation_failed"
    assert logged[0][1]["returncode"] == 3
    assert "something unfamiliar" in logged[0][1]["stderr_tail"]
    # Which invocation this was. A record that cannot be tied to a request is a
    # weaker answer to "why did this fail" than it looks.
    assert logged[0][1]["request_id"] == RID


def test_the_prompt_the_adapter_guards_with_is_the_one_it_sent(tmp_path):
    """After an attachment is inlined, the text that reached the process is
    longer than the `prompt` argument. Guarding with the argument would leave
    every marker in the ATTACHED diff able to declare the account spent."""
    attachment = tmp_path / "diff.patch"
    attachment.write_text("--- a\n+++ b\n+# quota exceeded (a diff hunk)\n", encoding="utf-8")
    runner = FakeRunner([result(returncode=1, stderr="")])
    logged = []
    codex = CodexConversation(runner, log=lambda event, data: logged.append((event, data)))
    # stderr echoes what was actually sent, which now includes the diff.
    runner.results = [result(returncode=1, stderr=PROMPT + "\n\n" + attachment.read_text())]
    assert codex.submit(RID, PROMPT, attachment=str(attachment)) is SubmitResult.REJECTED
    assert logged[0][1]["classification"] == UNCLASSIFIED
    assert "quota exceeded" in logged[0][1]["suppressed_patterns"]


def test_an_echoed_review_packet_never_parks_the_loop_as_exhausted():
    """The whole task, at the adapter seam. The runner returns the prompt on
    stderr, exactly as `codex exec` does, and the prompt discusses 429s, quotas
    and rate limits by name — which several task descriptions in this
    repository do."""
    logged = []
    codex = CodexConversation(
        FakeRunner([result(returncode=1, stderr=LOUD_PROMPT)]),
        log=lambda event, data: logged.append((event, data)),
    )
    assert codex.submit(RID, LOUD_PROMPT) is SubmitResult.REJECTED
    assert logged[0][1]["classification"] == UNCLASSIFIED
    assert logged[0][1]["suppressed_patterns"]


def test_a_transient_throttle_stays_on_the_retryable_path():
    """REJECTED is the ordinary retry path this transport already has.
    `QuotaExhaustedError` is loop_fatal with no retry, so a thirty-second limit
    reaching it is a false outage — the harm this task names."""
    logged = []
    codex = CodexConversation(
        FakeRunner([result(returncode=1, stderr="429 Too Many Requests; retry after 30s")]),
        log=lambda event, data: logged.append((event, data)),
    )
    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED
    assert logged[0][1]["classification"] == RATE_LIMITED


def test_a_clean_exit_with_no_reply_is_recorded_too():
    """Exit 0 with nothing on stdout is a broken invocation. It stays a
    non-exhaustion by construction, and it still leaves a record naming it."""
    logged = []
    codex = CodexConversation(
        FakeRunner([result(stdout="", returncode=0, stderr=LOUD_PROMPT)]),
        log=lambda event, data: logged.append((event, data)),
    )
    assert codex.submit(RID, LOUD_PROMPT) is SubmitResult.REJECTED
    assert logged[0][0] == "codex_invocation_failed"
    assert logged[0][1]["classification"] == NOT_A_FAILURE
    assert "no reply on stdout" in logged[0][1]["note"]


def test_a_stdout_only_failure_reaches_the_record():
    logged = []
    codex = CodexConversation(
        FakeRunner([result(returncode=2, stdout="fatal: not logged in", stderr="")]),
        log=lambda event, data: logged.append((event, data)),
    )
    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED
    assert "not logged in" in logged[0][1]["stdout_tail"]


class RaisingRunner:
    """A runner whose process never returns an exit code — the shape a missing
    binary and a killed-at-the-timeout invocation both wear."""

    def __init__(self, exc):
        self.exc = exc

    def run(self, prompt):
        raise self.exc


def test_an_invocation_that_never_returns_an_exit_code_is_recorded_and_re_raised():
    """Diagnosable is the claim, not just classifiable. The routing is
    unchanged — the error is re-raised untouched — but the transcript now
    answers "why did this fail" instead of only whatever printed upstream."""
    logged = []
    codex = CodexConversation(
        RaisingRunner(ResponseTimeoutError("codex did not finish within 900.0s")),
        log=lambda event, data: logged.append((event, data)),
    )
    with pytest.raises(ResponseTimeoutError):
        codex.submit(RID, PROMPT)
    assert logged[0][0] == "codex_invocation_failed"
    # None, not a fabricated -1 that would read as a real exit status.
    assert logged[0][1]["returncode"] is None
    assert "did not finish within" in logged[0][1]["note"]


def test_a_raising_runner_cannot_echo_the_prompt_back_through_its_message():
    """A runner this module does not own can raise anything, the prompt
    included, so the note is echo-STRIPPED like the stream excerpts."""
    logged = []
    codex = CodexConversation(
        RaisingRunner(RuntimeError(LOUD_PROMPT)),
        log=lambda event, data: logged.append((event, data)),
    )
    with pytest.raises(RuntimeError):
        codex.submit(RID, LOUD_PROMPT)
    note = logged[0][1]["note"]
    assert "docs/autoloop.md:4295" not in note
    assert "RuntimeError" in note, "the fault is still named"


def test_a_raising_runner_cannot_flood_the_transcript():
    """And BOUNDED, which is a separate property from stripping — a message
    that is not an echo is not shortened by the strip at all. 6,000 characters
    of non-echo text must still land as at most one excerpt's worth."""
    logged = []
    codex = CodexConversation(
        RaisingRunner(RuntimeError("z" * 6000)),
        log=lambda event, data: logged.append((event, data)),
    )
    with pytest.raises(RuntimeError):
        codex.submit(RID, PROMPT)
    note = logged[0][1]["note"]
    assert len(note) == 400
    # Both ends kept, so the fault's NAME survives a long message.
    assert note.startswith("the invocation never returned an exit code")
    assert note.endswith("z")


def test_config_may_widen_the_spent_list_without_reopening_the_hole():
    """The narrowed `quota_patterns` in `.autoloop/config.toml` was a stopgap,
    not the fix. An operator who puts the bare words back gets the guard, not
    the outage."""
    codex = CodexConversation(
        FakeRunner([result(returncode=1, stderr=LOUD_PROMPT)]),
        quota_patterns=("429", "quota", "rate limit"),
    )
    assert codex.submit(RID, LOUD_PROMPT) is SubmitResult.REJECTED


def test_the_adapter_declares_idempotent_submit_and_no_rotation_surface():
    codex = CodexConversation(FakeRunner([result(stdout="x")]))
    assert codex.idempotent_submit is True
    # Absent by design: rotation is a browser concept, and `_client_for_request`
    # probes for these, so omitting them makes rotation unreachable rather than
    # disabled.
    assert not hasattr(codex, "retarget")
    assert not hasattr(codex, "current_url")


def test_the_runner_never_uses_a_shell_and_confines_the_working_dir(tmp_path):
    runner = SubprocessCodexRunner(
        command=("codex", "exec"), sandbox_args=("--read-only",), cwd=tmp_path
    )
    assert runner.argv_preview == ("codex", "exec", "--read-only")
    # The preview is what reaches diagnostics — it must not carry the prompt,
    # which is the entire review packet.
    assert all("request" not in part for part in runner.argv_preview)


def test_a_missing_binary_is_a_clear_actionable_error(tmp_path):
    from autoloop.errors import BrowserError

    runner = SubprocessCodexRunner(command=("definitely-not-a-real-binary-xyz",), cwd=tmp_path)
    with pytest.raises(BrowserError) as exc:
        runner.run("hello")
    assert "codex login" in str(exc.value)


def test_the_provider_is_registered_and_constructible(tmp_path):
    assert "codex_cli" in available_providers()
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path,
        conversation=ConversationConfig(provider="codex_cli"),
    )
    conversation = create_conversation("codex_cli", config)
    assert isinstance(conversation, CodexConversation)
    # Constructing must not require the binary — only running does.
    conversation.attach()
    conversation.close()


def test_default_patterns_are_not_empty():
    assert DEFAULT_QUOTA_PATTERNS
    assert DEFAULT_RATE_LIMIT_PATTERNS
    # The two lists must not overlap: a marker in both would make the order of
    # the checks the thing that decides whether the loop parks.
    assert not set(DEFAULT_QUOTA_PATTERNS) & set(DEFAULT_RATE_LIMIT_PATTERNS)


# ---- the record actually reaches the transcript ------------------------------
#
# The defect was never that `submit` forgot to log. It logged on every failure,
# with a comment explaining why. The FACTORY built the adapter without a
# logger, so the no-op default stood and the count of `codex_invocation_failed`
# records across a 24-day transcript was ZERO. These tests therefore read the
# FILE — asserting that a method was called would have passed the whole time.


def codex_config(tmp_path, **codex_overrides):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        conversation=ConversationConfig(provider="codex_cli"),
        codex=CodexConfig(**codex_overrides),
    )


def transcript_rows(config, kind="codex_invocation_failed"):
    return entries(config, kind)


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (dict(returncode=1, stderr="Error: connection reset by peer"), UNCLASSIFIED),
        (dict(returncode=1, stderr="429 Too Many Requests"), RATE_LIMITED),
        (dict(returncode=2, stdout="fatal: not logged in"), UNCLASSIFIED),
        (dict(returncode=0, stdout=""), NOT_A_FAILURE),
    ],
)
def test_a_failed_invocation_from_the_factory_is_written_to_the_transcript(
    tmp_path, outcome, expected
):
    """Every shape of failure, read back off disk. Includes the two round 1 got
    wrong: a diagnostic that arrives before the echo, and a stdout-only
    failure."""
    config = codex_config(tmp_path)
    conversation = create_conversation("codex_cli", config)
    conversation._runner = FakeRunner([result(**outcome)])

    assert conversation.submit(RID, PROMPT) is SubmitResult.REJECTED

    rows = transcript_rows(config)
    assert len(rows) == 1, "the production adapter must not be built with a no-op logger"
    data = rows[0]["data"]
    assert data["returncode"] == outcome.get("returncode", 0)
    assert data["classification"] == expected
    assert data["request_id"] == RID
    # Something useful, always — never a record whose only content is that
    # something went wrong.
    assert data["stderr_tail"] or data["stdout_tail"] or data["note"]


def test_an_error_before_the_echo_reaches_the_transcript_intact(tmp_path):
    """The round 1 finding, pinned at the level it has to hold: the transcript,
    not a helper's return value."""
    config = codex_config(tmp_path)
    conversation = create_conversation("codex_cli", config)
    conversation._runner = FakeRunner(
        [result(returncode=1, stderr=f"Error: model 'x' is not available\n{LOUD_PROMPT}")]
    )

    assert conversation.submit(RID, LOUD_PROMPT) is SubmitResult.REJECTED

    data = transcript_rows(config)[0]["data"]
    assert "is not available" in data["stderr_tail"]
    assert data["classification"] == UNCLASSIFIED
    assert data["prompt_echo_chars"] > 0


def test_a_stdout_only_failure_reaches_the_transcript(tmp_path):
    config = codex_config(tmp_path)
    conversation = create_conversation("codex_cli", config)
    conversation._runner = FakeRunner(
        [result(returncode=3, stdout="fatal: auth.json is unreadable", stderr="")]
    )

    conversation.submit(RID, PROMPT)

    data = transcript_rows(config)[0]["data"]
    assert "auth.json is unreadable" in data["stdout_tail"]
    assert data["stderr_tail"] == ""


def test_the_factory_carries_the_configured_pattern_lists(tmp_path):
    config = codex_config(
        tmp_path, quota_patterns=("kaputt",), rate_limit_patterns=("langsam",)
    )
    conversation = create_conversation("codex_cli", config)
    assert conversation._quota_patterns == ("kaputt",)
    assert conversation._rate_limit_patterns == ("langsam",)
    # And empty config still means the built-in lists, not empty ones — an
    # empty list would classify nothing at all.
    default = create_conversation("codex_cli", codex_config(tmp_path / "b"))
    assert default._quota_patterns == DEFAULT_QUOTA_PATTERNS
    assert default._rate_limit_patterns == DEFAULT_RATE_LIMIT_PATTERNS


def test_the_factory_built_app_server_provider_also_carries_a_real_logger(tmp_path):
    """The identical never-reaches-the-transcript bug lived next door:
    `CodexAppServerConversation` and `AppServerClient` both take `log=` and the
    factory passed neither, so `codex_app_server_failed` was written to a
    no-op as well."""
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        conversation=ConversationConfig(provider="codex_app_server"),
    )
    conversation = create_conversation("codex_app_server", config)

    conversation._log("codex_app_server_thread_started", {"thread_id": "t-1"})
    conversation._client._log("codex_app_server_failed", {"context": "turn/start"})

    assert transcript_rows(config, "codex_app_server_thread_started")
    assert transcript_rows(config, "codex_app_server_failed")


# ---- failover --------------------------------------------------------------


class ScriptedClient:
    """Minimal conversation double. `quota_on_submit` raises the exhaustion the
    orchestrator is supposed to route, rather than returning a result."""

    def __init__(self, name, *, quota_on_submit=False, responses=(), idempotent=False):
        self.name = name
        self.quota_on_submit = quota_on_submit
        self.responses = list(responses)
        self.persisted: set[str] = set()
        self.submitted: list[str] = []
        self.closed = False
        self.send_attempted = False
        if idempotent:
            self.idempotent_submit = True

    def attach(self):
        pass

    def has_request(self, rid):
        return rid in self.persisted

    def reconcile(self, rid):
        return rid in self.persisted

    def submit(self, rid, prompt):
        if self.quota_on_submit:
            raise QuotaExhaustedError("allowance spent; separate quota note")
        self.send_attempted = True
        self.submitted.append(rid)
        self.persisted.add(rid)
        return SubmitResult.CONFIRMED

    def await_response(self, rid):
        return self.responses.pop(0) if self.responses else stop_block()

    def close(self):
        self.closed = True


def build(tmp_path, clients, *, fallback="browser_chatgpt", state=None, policy=None):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    git = FakeGit(repo_root)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=policy or PolicyConfig(),
        state_dir=tmp_path / ".al",
        conversation=ConversationConfig(provider="codex_cli", fallback_provider=fallback),
    )
    store = StateStore(config.state_file)
    if state is None:
        state = LoopState.new("https://chatgpt.com/c/x")
        state.outbox = "kickoff"
    store.save(state)
    executor = FakeExecutor()
    executor.git = git
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: clients[config.conversation.provider],
        provider_factory=lambda provider: clients[provider],
        registry=TaskRegistry(),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
    )
    return orch, store, config


def pending(rid=RID, prompt=PROMPT):
    import hashlib

    req = PendingRequest(
        request_id=rid,
        payload="body",
        prompt=prompt,
        conversation_url="https://chatgpt.com/c/x",
    )
    req.prompt_sha256 = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()
    return req


def entries(config, kind):
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("type") == kind]


def park_code(config):
    """The code a park recorded. It lives on the transcript's `needs_user`
    entry (and on a `Blocker` when a store is configured) — never on
    `LoopState`, which carries only the kind."""
    rows = entries(config, "needs_user")
    return rows[-1]["data"]["code"] if rows else None


def submitting_state(prompt=PROMPT, **overrides):
    state = LoopState.new("https://chatgpt.com/c/x")
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending(prompt=prompt)
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_exhausted_primary_hands_over_to_the_fallback(tmp_path):
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True, idempotent=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=submitting_state())

    orch.run(max_steps=1)  # codex raises; the role moves

    assert orch.state.active_provider == "browser_chatgpt"
    assert orch.state.provider_switches == 1
    assert orch.state.phase == Phase.SUBMITTING.value
    switched = entries(config, "provider_switched")
    assert switched and switched[0]["data"]["from_provider"] == "codex_cli"
    assert switched[0]["data"]["to_provider"] == "browser_chatgpt"
    assert switched[0]["data"]["reason"] == "quota_exhausted"

    orch.run(max_steps=1)  # re-issued on the browser
    assert clients["browser_chatgpt"].submitted == [RID]
    assert orch.state.pending_request.provider == "browser_chatgpt"


def test_the_handover_clears_only_per_transport_marks(tmp_path):
    """The fallback has never seen this request, so the exhausted transport's
    marks describe nothing here — without clearing them `submitting` would park
    on `submission_ambiguous` and the failover would be dead on arrival."""
    # The realistic route to a request carrying transport marks: a send was
    # disproven on codex, reconciliation confirmed absence and authorized the
    # one resend, and THAT resend is the invocation that reports exhaustion.
    state = submitting_state()
    state.phase = Phase.SUBMISSION_REJECTED.value
    state.pending_request.send_attempted = True
    state.pending_request.last_send_outcome = "rejected"
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=state)

    orch.run(max_steps=2)  # reconcile+authorize resend, then quota on the resend

    assert orch.state.provider_switches == 1

    req = orch.state.pending_request
    assert req.send_attempted is False
    assert req.last_send_outcome == ""
    assert req.resends_used == 0
    # The request itself is untouched — same id, same bytes.
    assert req.request_id == RID
    assert req.prompt == PROMPT


def test_the_response_records_which_reviewer_produced_it(tmp_path):
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser", responses=[stop_block("browser answered")]),
    }
    orch, store, config = build(tmp_path, clients, state=submitting_state())

    orch.run(max_steps=3)  # switch -> submit -> await

    assert orch.state.last_response is not None
    assert orch.state.last_response.provider == "browser_chatgpt"


def test_without_a_fallback_the_loop_parks(tmp_path):
    clients = {"codex_cli": ScriptedClient("codex", quota_on_submit=True)}
    orch, store, config = build(tmp_path, clients, fallback="", state=submitting_state())

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert park_code(config) == "quota_exhausted"
    assert orch.state.provider_switches == 0
    assert "separate quota" in orch.state.question


# ---- the two parks this task exists to prevent, at the loop level ------------


def run_real_codex(tmp_path, prompt=PROMPT, **outcome):
    """Drive one loop step with a REAL `CodexConversation` in the client seat,
    logging to the loop's own transcript.

    A double that raises `QuotaExhaustedError` on demand can show what the
    orchestrator does with one; it cannot show that the wrong input never
    produces one. These three tests need the classifier, the routing and the
    park exercised together.
    """
    clients: dict = {}
    orch, store, config = build(
        tmp_path, clients, fallback="", state=submitting_state(prompt=prompt)
    )
    transcript = TranscriptLogger(config.transcript_file)
    clients["codex_cli"] = CodexConversation(
        FakeRunner([result(**outcome)]),
        log=lambda event, data: transcript.append(event, data=data),
    )
    orch.run(max_steps=1)
    return orch, config


def test_an_echoed_review_packet_never_parks_the_loop(tmp_path):
    """2026-08-22, reproduced end to end. The loop parked twice, loop_fatal, on
    'the Codex allowance for this ChatGPT plan is exhausted (exit 1)' against an
    account 4% through its weekly window — roughly 25 minutes of downtime and an
    hour of investigation — because the packet quoted a line number."""
    orch, config = run_real_codex(
        tmp_path, prompt=LOUD_PROMPT, returncode=1, stderr=LOUD_PROMPT
    )

    assert orch.state.phase == Phase.SUBMISSION_REJECTED.value
    assert park_code(config) is None
    assert orch.state.provider_switches == 0
    # Not swallowed either: the failure is retryable AND it left a record
    # naming why, which is the half of this task the transcript proves.
    rows = entries(config, "codex_invocation_failed")
    assert len(rows) == 1
    assert rows[0]["data"]["classification"] == UNCLASSIFIED
    assert rows[0]["data"]["suppressed_patterns"]


def test_a_transient_throttle_takes_the_retryable_path_not_the_fatal_one(tmp_path):
    """`QuotaExhaustedError` is loop_fatal with no retry; a 429 clears on a
    server-side timer. Routing the second to the first is a false outage."""
    orch, config = run_real_codex(
        tmp_path, returncode=1, stderr="429 Too Many Requests; retry after 30s"
    )

    assert orch.state.phase == Phase.SUBMISSION_REJECTED.value
    assert park_code(config) is None
    assert entries(config, "codex_invocation_failed")[0]["data"]["classification"] == (
        RATE_LIMITED
    )


def test_a_genuine_exhaustion_still_parks_the_loop(tmp_path):
    """The other half of the claim. Narrowing what counts as evidence must not
    stop codex's own exhaustion message from being acted on."""
    orch, config = run_real_codex(
        tmp_path, returncode=1, stderr="Error: you have hit your usage limit"
    )

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "quota_exhausted"
    assert entries(config, "codex_invocation_failed")[0]["data"]["classification"] == (
        QUOTA_EXHAUSTED
    )


def test_a_fallback_equal_to_the_primary_parks(tmp_path):
    clients = {"codex_cli": ScriptedClient("codex", quota_on_submit=True)}
    orch, store, config = build(
        tmp_path, clients, fallback="codex_cli", state=submitting_state()
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.provider_switches == 0


def test_the_switch_budget_is_enforced(tmp_path):
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser", quota_on_submit=True),
    }
    orch, store, config = build(
        tmp_path, clients, state=submitting_state(provider_switches=1)
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "provider_switch_budget"
    assert orch.state.provider_switches == 1  # not incremented


def test_a_captured_reply_blocks_the_handover(tmp_path):
    """Quota cannot normally bite with a reply in hand; this asserts the guard
    rather than trusting the phase machine to keep that shape. A handover here
    would straddle an answered turn — two reviewers inside one round."""
    from autoloop.state import LastResponse

    state = submitting_state()
    state.last_response = LastResponse(
        request_id="alr-earlier-0001", raw=stop_block(), received_at="2026-08-01T00:00:00+00:00"
    )
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=state)

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.provider_switches == 0
    assert orch.state.active_provider == ""


def test_state_beats_config_after_a_switch(tmp_path):
    """A resumed run must not quietly return to the exhausted provider and
    spend the same allowance again."""
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=submitting_state())
    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.active_provider == "browser_chatgpt"

    second = tmp_path / "second"
    second.mkdir()
    orch2, _, _ = build(second, clients, state=reloaded)
    assert orch2.active_provider() == "browser_chatgpt"


def test_switch_budget_is_checkable_without_an_orchestrator():
    engine = PolicyEngine(PolicyConfig(max_provider_switches=1))
    assert engine.check_provider_switch_budget(0).allowed
    assert not engine.check_provider_switch_budget(1).allowed
    # 0 disables failover entirely — the other way to turn it off, alongside
    # leaving conversation.fallback_provider empty.
    assert PolicyEngine(PolicyConfig(max_provider_switches=0)).check_provider_switch_budget(
        0
    ).allowed is False


def test_an_idempotent_provider_never_parks_on_ambiguity(tmp_path):
    """The ambiguity park exists for a shared, persistent chat thread. A failed
    stateless invocation appended nothing, so retrying cannot double-post."""
    state = submitting_state()
    state.pending_request.send_attempted = True
    clients = {"codex_cli": ScriptedClient("codex", idempotent=True)}
    orch, store, config = build(tmp_path, clients, fallback="", state=state)

    orch.run(max_steps=1)

    assert orch.state.phase != Phase.NEEDS_USER.value
    assert clients["codex_cli"].submitted == [RID]
    authorized = entries(config, "resend_authorized")
    assert authorized and authorized[0]["data"]["reason_code"] == "idempotent_transport"


def test_a_non_idempotent_provider_still_parks_on_ambiguity(tmp_path):
    """The browser's rule is unchanged — this is the regression guard on the
    capability probe not leaking to providers that never declared it."""
    state = submitting_state()
    state.pending_request.send_attempted = True
    clients = {"codex_cli": ScriptedClient("codex")}  # no idempotent_submit
    orch, store, config = build(tmp_path, clients, fallback="", state=state)

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
