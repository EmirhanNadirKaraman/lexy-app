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


class ResponseTimeoutError(BrowserError):
    """No completed assistant response appeared within the timeout."""


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
