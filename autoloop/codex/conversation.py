"""`LLMConversation` over the Codex CLI.

The browser provider is an elaborate machine for *not knowing whether a message
was delivered* — optimistic rendering, reconciliation, conversation epochs,
rotation. None of that is a property of the reviewer role; all of it is a
property of reading a DOM. A subprocess returns an exit code, so this adapter
knows.

That collapses the interface honestly rather than by pretending:

* `submit` runs the CLI to completion and stashes the reply. It returns
  CONFIRMED when a reply was captured and REJECTED when the process failed —
  never UNCONFIRMED, because there is no state in which "we might have sent
  something" is true.
* `await_response` returns the stash. The wait already happened.
* `has_request` / `reconcile` answer from the stash, which is authoritative
  precisely because the transport is synchronous: if this process did not
  capture a reply, no reply exists.

**`idempotent_submit` is the important declaration.** A `codex exec` that
launched and failed appended nothing to any durable conversation, so re-running
costs tokens and cannot double-post. The orchestrator probes this attribute and
skips the ambiguity park for providers that declare it. Without it, every failed
invocation would park a human on `submission_ambiguous` — a rule that exists for
a shared, persistent chat thread and means nothing here. The adapter declares
the property rather than lying about `send_attempted`, so the orchestrator's
reasoning stays visible at the seam instead of being smuggled through a flag.

**No rotation, by omission.** `retarget` / `current_url` are absent, so
`_client_for_request`'s `getattr` probe skips retargeting and rotation is
structurally unreachable. Every rotation trigger — a disproven send, a wedged
conversation, a chat that accepts a turn and never answers — describes a browser
conversation. There is nothing here to rotate away from.

**The reviewer gets no repository access.** It runs with `cwd` pointed outside
the checkout: the prompt is self-contained (every turn re-sends its CONTEXT
block and the full contract), so the reviewer needs no filesystem at all, and
containment that does not depend on knowing a sandbox flag's name is
containment that still holds when the flag is renamed. Configured sandbox
arguments are passed through on top of that, not instead of it.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..browser.chatgpt import SubmitResult
from ..errors import BrowserError, QuotaExhaustedError, ResponseTimeoutError
from .quota import DEFAULT_QUOTA_PATTERNS, failure_digest, is_quota_exhausted


@dataclass(frozen=True)
class CodexResult:
    stdout: str
    stderr: str
    returncode: int
    duration_seconds: float
    command: tuple[str, ...]


class CodexRunner(Protocol):
    """How a prompt reaches the CLI. A protocol for the same reason
    `audit.agents.AgentRunner` is one: tests must never launch a real binary,
    and the binary is not installed everywhere the suite runs."""

    def run(self, prompt: str) -> CodexResult: ...


class SubprocessCodexRunner:
    """The real runner: one `codex exec` invocation per prompt.

    The prompt goes through **argv**, never a shell — same rule as
    `git_gateway` and `audit.agents`. It is model-authored text that can
    contain anything, and `shell=True` anywhere near it would be a command
    injection with extra steps.
    """

    def __init__(
        self,
        command: tuple[str, ...] = ("codex", "exec"),
        *,
        sandbox_args: tuple[str, ...] = (),
        timeout_seconds: float = 900.0,
        cwd: Path | None = None,
        env: dict | None = None,
    ):
        self._command = tuple(command)
        self._sandbox_args = tuple(sandbox_args)
        self._timeout = timeout_seconds
        # Default to the user's home rather than the repository. See the module
        # docstring: the reviewer has no business reading this checkout, and a
        # cwd outside it is a containment we can state without knowing the
        # CLI's sandbox flag names.
        self._cwd = Path(cwd) if cwd else Path.home()
        self._env = env

    @property
    def argv_preview(self) -> tuple[str, ...]:
        """The invocation minus the prompt — for `doctor` and diagnostics."""
        return (*self._command, *self._sandbox_args)

    def run(self, prompt: str) -> CodexResult:
        argv = (*self._command, *self._sandbox_args, prompt)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                cwd=str(self._cwd),
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise BrowserError(
                f"the codex CLI was not found ({self._command[0]!r}). Install it "
                "and sign in with `codex login`, or set conversation.provider "
                "back to 'browser_chatgpt'."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ResponseTimeoutError(
                f"codex did not finish within {self._timeout}s"
            ) from exc
        return CodexResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
            duration_seconds=time.monotonic() - started,
            # The prompt is deliberately excluded: this is carried into
            # diagnostics, and the prompt is the whole review packet.
            command=self.argv_preview,
        )


class CodexConversation:
    """One reviewer turn per CLI invocation. Stateless between turns, which is
    safe because every prompt already carries its own CONTEXT block and the
    full response contract."""

    #: Declares that a failed `submit` left nothing behind on any durable
    #: conversation, so retrying the same request id cannot duplicate a turn.
    #: Read by the orchestrator via `getattr`; providers that do not set it are
    #: treated as they always were.
    idempotent_submit = True

    def __init__(
        self,
        runner: CodexRunner,
        *,
        quota_patterns: tuple[str, ...] = DEFAULT_QUOTA_PATTERNS,
        log=None,
    ):
        self._runner = runner
        self._quota_patterns = tuple(quota_patterns)
        #: request_id -> captured reply. In-memory on purpose: it is a cache of
        #: what THIS process observed, not a claim about the world. After a
        #: restart it is empty, `reconcile` truthfully says "no reply captured",
        #: and `idempotent_submit` makes re-running the correct response.
        self._responses: dict[str, str] = {}
        self._log = log or (lambda event, data: None)

    # ---- lifecycle ----------------------------------------------------------

    def attach(self) -> None:
        """Nothing to attach to. Honest no-op: there is no page to navigate, no
        login to verify, and inventing a probe here would be a fake check."""

    def close(self) -> None:
        """No connection is held. Each turn is its own process."""

    # ---- reads --------------------------------------------------------------

    def has_request(self, request_id: str) -> bool:
        return request_id in self._responses

    def reconcile(self, request_id: str) -> bool:
        """Authoritative, unlike its browser counterpart: a synchronous
        transport either captured a reply or none exists."""
        return request_id in self._responses

    # ---- actions ------------------------------------------------------------

    def submit(self, request_id: str, prompt: str) -> SubmitResult:
        if request_id in self._responses:
            return SubmitResult.ALREADY_PERSISTED

        result = self._runner.run(prompt)

        if result.returncode != 0:
            digest = failure_digest(result.returncode, result.stderr)
            # Logged on EVERY failure, not only unrecognised ones: this is what
            # turns the first real exhaustion into a one-line config fix
            # instead of an investigation. See `quota.py`.
            self._log("codex_invocation_failed", digest)
            if is_quota_exhausted(
                result.returncode, result.stdout, result.stderr, self._quota_patterns
            ):
                raise QuotaExhaustedError(
                    "the Codex allowance for this ChatGPT plan is exhausted "
                    f"(exit {result.returncode}). Codex shares an agentic pool "
                    "with ChatGPT Work and ChatGPT for Excel; ordinary ChatGPT "
                    "conversations draw on a separate quota, which is why the "
                    "browser fallback can still run."
                )
            return SubmitResult.REJECTED

        reply = result.stdout.strip()
        if not reply:
            # A clean exit with nothing on stdout is a broken invocation, not a
            # reply. Reporting REJECTED keeps it retryable; calling it CONFIRMED
            # would hand the contract parser an empty string and spend a parse
            # retry saying so.
            self._log(
                "codex_invocation_failed",
                failure_digest(result.returncode, "exited 0 with empty stdout"),
            )
            return SubmitResult.REJECTED

        self._responses[request_id] = reply
        return SubmitResult.CONFIRMED

    def await_response(self, request_id: str) -> str:
        """Return the reply captured by `submit`. The waiting already happened
        — a CLI turn is synchronous, so there is nothing to poll for."""
        reply = self._responses.get(request_id)
        if reply is None:
            raise ResponseTimeoutError(
                f"no codex reply was captured for {request_id}; the invocation "
                "did not complete in this process"
            )
        return reply
