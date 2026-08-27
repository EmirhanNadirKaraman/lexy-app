"""Exception hierarchy for autoloop.

Grouped by subsystem so the orchestrator can route failures deterministically:
BrowserError subclasses retry/reconnect, GitError subclasses are reported back
to ChatGPT, ContractError triggers a corrective re-prompt, State/Config errors
stop the process (they mean the operator must intervene).
"""


class AutoloopError(Exception):
    """Base class for all autoloop errors."""


class ConfigError(AutoloopError):
    """Configuration file missing, malformed, or containing unknown keys."""


class StateError(AutoloopError):
    """Persistent state is inconsistent with the requested operation."""


class StateCorruptError(StateError):
    """State file exists but cannot be decoded. The file is left untouched."""


class ContractError(AutoloopError):
    """ChatGPT's response does not satisfy the response contract.

    Carries a stable ``code`` (e.g. ``no_json_block``, ``unknown_decision``)
    that is echoed back to ChatGPT in the corrective prompt.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class BrowserError(AutoloopError):
    """Base class for browser-automation failures."""


class LoginExpiredError(BrowserError):
    """The dedicated profile is no longer logged in to ChatGPT."""


class SessionLostError(BrowserError):
    """The CDP connection or page died (browser closed / restarted)."""


class SubmissionError(BrowserError):
    """A prompt could not be submitted or its submission was never confirmed."""


class ConversationUnusableError(BrowserError):
    """The configured conversation loaded but cannot be used at all.

    Narrow on purpose: this is the one browser failure that authorizes
    rotating to a fresh conversation, and a run gets a single rotation. Two
    shapes qualify, and `code` names which so the transcript and the
    rotation record can tell them apart:

    * ``"conversation_unusable"`` (the default) — the page demonstrably
      reached the conversation URL, is not an auth page, and still has no
      composer (or shows an explicit conversation-error marker).
    * ``"submission_never_appeared"`` — a submission THIS loop made is
      provably not in the conversation: the page is attachable and
      un-throttled, and the message list was mounted to its END without the
      message appearing (the same two proofs `find_conversation_with`
      requires before it may call anything absent). Raised from both
      surfaces that fault wears while awaiting — the response-start bound
      expiring cleanly, and the awaiting read itself dying with a
      lost-session label (`BrowserChatGPT._classify_awaiting_read_failure`),
      which is how the incident actually presented. The browser is
      demonstrably fine — every read that established this went through it —
      so the THREAD is wedged and restarting Chrome cannot help (2026-08-17:
      ten minutes of 45-second restarts against a chat that had silently
      stopped taking the loop's messages, while the same account posted by
      hand in a different conversation).

    Either way *this chat* is wedged. A page that never loaded, a dropped
    CDP connection, or a logged-out profile are ordinary `BrowserError` /
    `SessionLostError` / `LoginExpiredError` on the normal failure budget:
    rotating for those would spend the one rotation on a network blip and
    leave none for the real thing.
    """

    def __init__(self, message: str, *, code: str = "conversation_unusable"):
        self.code = code
        super().__init__(message)


class ConversationSearchInconclusive(BrowserError):
    """A by-content conversation search could not rule EITHER way.

    Raised by `BrowserChatGPT.find_conversation_with` for four faults, which
    all mean the same thing — what was read is not evidence about the
    conversation that was asked about, so "found here" and "not in this
    project" are equally unsupported:

    * the page it read is not the page it asked for (a rotation mid-flight
      moves the shared page);
    * the virtualized message list was still CHANGING at the end of the list
      when the mount bound ran out (a long or streaming conversation);
    * the mount never reached the END of that list (a gesture that is not
      driving the scroller);
    * the session cannot report a scroll position at all, so an unchanged
      window proves only that the gesture stopped mounting — the tail when the
      gesture works, the opening window when it silently missed.

    Only ABSENCE needs all of that. A SIGHTING is direct evidence and is
    returned by any session, position signal or not.

    Deliberately NOT a `ConversationUnusableError` — that one authorizes
    spending the run's single rotation, and an inconclusive read says nothing
    about whether any chat is wedged. An ordinary `BrowserError`, so callers
    that already catch that keep their current behaviour (the search degrades
    to "not found", exactly as before this existed) while a caller that wants
    to tell "no" apart from "cannot tell" can name this type.

    Since the missing-submission check reuses the same tail mount
    (`BrowserChatGPT._rule_out_missing_submission`), `await_response` can
    surface the page-identity fault too; it routes as an ordinary browser
    fault there, exactly as the page-drifted error always has. On the
    read-failure entry (`_classify_awaiting_read_failure`) it never escapes
    at all: an inconclusive probe re-raises the original lost-session fault,
    which carries the same routing.
    """


class ResponseTimeoutError(BrowserError):
    """No completed assistant response appeared within the timeout.

    `stage` distinguishes which of `BrowserChatGPT.await_response`'s two
    bounds fired: `"start"` means no assistant turn began generating at all
    within `response_start_timeout` — the one shape of this error that is a
    candidate for the "silent conversation" rotation entry condition (see
    `orchestrator._handle_response_start_timeout` and `docs/AUTOLOOP.md`
    §5c). `"complete"` (the default, for every other raise site) means a
    response visibly started but did not settle in time — the conversation
    is plainly alive, so this is never grounds to rotate.

    `elapsed` is the ACTUAL measured wait in monotonic seconds, not the
    configured timeout value — so a caller proving a minimum total wait was
    really observed (rather than assumed from config) has real evidence to
    sum, not just a repeated constant. `None` for an instance that never
    measured it (e.g. hand-constructed in a test).
    """

    def __init__(self, message: str, *, stage: str = "complete", elapsed: float | None = None):
        self.stage = stage
        self.elapsed = elapsed
        super().__init__(message)


class RateLimitedError(AutoloopError):
    """ChatGPT is throttling the ACCOUNT and has covered the page with its
    "Too many requests" modal.

    Deliberately NOT a `BrowserError`. Nothing about the browser is broken, and
    the ordinary browser recovery — drop the client, restart Chrome, retry —
    is the one response that provably makes this worse: the limit is
    account-level and server-side, so a fresh browser meets the same wall while
    adding another request to the window that produced it. Overnight on
    2026-08-14/15 the loop did exactly that, reported each round as
    `browser session lost: Locator.click: Timeout 30000ms exceeded. waiting for
    locator("#prompt-textarea")`, and burned pkt-03 through its attempt ceiling
    without ever reaching a review. Routing it here instead means it never
    reaches `_handle_browser_failure` at all.

    Not `QuotaExhaustedError` either, though that one's docstring notes the web
    UI rate-limits too. That error means the plan ALLOWANCE is spent, and its
    handler answers by switching to the fallback provider or parking — neither
    of which addresses a temporary throttle that clears on a server-side timer
    with no operator action at all. The remedy here is to WAIT, so it has its
    own handler (`orchestrator._handle_rate_limited`) and its own bounded
    budget (`policy.max_rate_limit_backoffs`), on the same principle as the
    cooldown-skipped restart exemption: a failure nobody could have recovered
    from must not be charged to the budget that decides recovery is hopeless.

    `stage` names where the throttle was seen (`"attach"`, `"submit-confirm"`,
    `"await"`, ...) purely for diagnostics.
    """

    def __init__(self, message: str, *, stage: str = ""):
        self.stage = stage
        super().__init__(message)


class QuotaExhaustedError(AutoloopError):
    """The reviewer's plan allowance is spent — an account condition, not a
    transport fault.

    Deliberately NOT a `BrowserError`: routing it through the ordinary failure
    budget would burn `max_consecutive_failures` in seconds and land the loop in
    `failed`, which describes neither the cause nor the remedy. It parks
    explicitly, or hands over to the configured fallback provider, and says
    which. Either provider may raise it — ChatGPT's web UI rate-limits too.
    """


class GitError(AutoloopError):
    """Base class for git-gateway failures."""


class GitCommandError(GitError):
    """A whitelisted git command ran but exited non-zero."""


class DiffTooLargeError(GitCommandError):
    """A diff render was REFUSED for exceeding `GitGateway.RANGE_DIFF_MAX_BYTES`
    — the one `GitCommandError` that says nothing is wrong with the repository.

    A subclass rather than a sibling, deliberately: every existing
    `except GitCommandError` (`changeset_review`, `_finish_postcommit`'s park,
    the CLI's rebuild paths) keeps catching it unchanged, so adding this widens
    nothing on its own. What it buys is the ability to catch the cap refusal
    SPECIFICALLY, ahead of the broad clause, and route it somewhere a torn repo
    must never go — see `orchestrator._finish_postcommit`, where a cap refusal
    asks the reviewer for a split plan while every other `GitCommandError` still
    parks. Clause ORDER is therefore load-bearing wherever both are caught: the
    narrow one first, or it is never reached.

    Raised by both `GitGateway.range_diff` and `GitGateway.range_diff_stat`, so
    "the stat busted the cap too" is the same type and is handled by the same
    reading: a stat that cannot be rendered is not a smaller artifact to fall
    back to, it is nothing to show.
    """


class GitOperationDenied(GitError):
    """The policy layer refused to run a git command (defense in depth)."""


class ExecutorError(AutoloopError):
    """The task executor failed in a way it could not report as an outcome."""


class TaskGraphError(AutoloopError):
    """A task-registry operation is invalid (duplicate/unknown id, cycle,
    illegal state transition). Carries a stable ``code`` echoed to ChatGPT."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class TemplateError(AutoloopError):
    """A prompt template was rendered with missing or unknown fields."""


class LockHeldError(AutoloopError):
    """Another live autoloop process owns the state directory's lock."""


class StaleLockError(AutoloopError):
    """A lock file exists but its owner is verifiably dead. Recover with
    `python -m autoloop unlock` — never removed silently."""


class ManifestViolation(GitError):
    """A commit was refused by the task-owned change manifest (approved paths
    not produced by the task, unrelated changes, missing manifest). Subclass
    of GitError so the orchestrator reports it back to ChatGPT."""


class AuditError(AutoloopError):
    """The audit executor failed in a way it could not report as an outcome."""


class EnvironmentDriftError(GitCommandError):
    """The git execution environment changed under a running task — a hook
    appeared, `core.hooksPath` moved, or an `insteadOf`/pushurl rule was
    added.

    Distinct from an ordinary `GitCommandError` because the cause is the
    SHARED environment, not this unit of work. Quarantining one task and
    moving to the next would leave the same condition in place for every
    task after it, so this is classified `loop_fatal` while an ordinary
    task-owned path/content refusal stays `task_fatal`. Subclasses
    GitCommandError so existing `except GitCommandError` handlers still
    catch it."""
