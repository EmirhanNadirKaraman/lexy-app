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
    rotating to a fresh conversation, and a run gets a single rotation. It
    means the page demonstrably reached the conversation URL, is not an auth
    page, and still has no composer (or shows an explicit conversation-error
    marker) — i.e. *this chat* is wedged. A page that never loaded, a dropped
    CDP connection, or a logged-out profile are ordinary `BrowserError` /
    `SessionLostError` / `LoginExpiredError` on the normal failure budget:
    rotating for those would spend the one rotation on a network blip and
    leave none for the real thing.
    """


class ConversationSearchInconclusive(BrowserError):
    """A by-content conversation search could not rule EITHER way.

    Raised by `BrowserChatGPT.find_conversation_with` when the page it read is
    not the page it asked for, or when the virtualized message list was still
    CHANGING under the mount gesture when its bound ran out. Both mean the same
    thing: what was read is not evidence about the conversation that was asked
    about, so "found here" and "not in this project" are equally unsupported.

    Deliberately NOT a `ConversationUnusableError` — that one authorizes
    spending the run's single rotation, and an inconclusive read says nothing
    about whether any chat is wedged. An ordinary `BrowserError`, so callers
    that already catch that keep their current behaviour (the search degrades
    to "not found", exactly as before this existed) while a caller that wants
    to tell "no" apart from "cannot tell" can name this type.
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
