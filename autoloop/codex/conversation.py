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

**Every non-zero exit leaves a record, and the prompt cannot classify it.**
`submit` logs `codex_invocation_failed` on every failure — including the ones
it does not recognise — and it classifies with `quota.classify`, which is
handed the FINAL prompt so that a marker the loop itself sent can never be read
back as evidence. `codex exec` echoes the whole prompt onto stderr, so without
that argument the review packet is inside the haystack; see `quota.py` for the
two parks that fact caused, and for why the comparison ignores whitespace and
punctuation on both sides — a literal substring test let a REFLOWED echo
synthesise a marker the prompt never contained verbatim. Only a SPENT allowance
raises `QuotaExhaustedError`; a transient throttle and an unrecognised fault
both return REJECTED and stay retryable.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..browser.chatgpt import SubmitResult
from ..errors import BrowserError, QuotaExhaustedError, ResponseTimeoutError
from .quota import (
    DEFAULT_QUOTA_PATTERNS,
    DEFAULT_RATE_LIMIT_PATTERNS,
    classify,
    failure_digest,
)

#: Ceiling on the argv-borne prompt, well under this host's 1 MiB ARG_MAX so
#: the environment block and the rest of the command line still fit.
_ARGV_BUDGET_BYTES = 700_000


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
        rate_limit_patterns: tuple[str, ...] = DEFAULT_RATE_LIMIT_PATTERNS,
        log=None,
    ):
        self._runner = runner
        self._quota_patterns = tuple(quota_patterns)
        self._rate_limit_patterns = tuple(rate_limit_patterns)
        #: request_id -> captured reply. In-memory on purpose: it is a cache of
        #: what THIS process observed, not a claim about the world. After a
        #: restart it is empty, `reconcile` truthfully says "no reply captured",
        #: and `idempotent_submit` makes re-running the correct response.
        self._responses: dict[str, str] = {}
        #: A no-op default is right for a unit test that only wants the return
        #: value. It is NOT right for a production adapter, and the factory
        #: (`autoloop.conversation._codex_cli_factory`) passes a real transcript
        #: writer — see that function. Constructing this class in production
        #: without one is the defect that left ZERO `codex_invocation_failed`
        #: records across a 24-day transcript, so the wiring lives at the
        #: factory rather than being enforced by removing this default.
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

    def submit(
        self, request_id: str, prompt: str, attachment: str | None = None
    ) -> SubmitResult:
        """Run one `codex exec` turn.

        `attachment` is an absolute path to an oversized diff the orchestrator
        could not inline (`orchestrator._write_diff_attachment`). The browser
        adapter uploads it because its composer cannot be proven to hold a
        large patch; there is no composer here — the prompt reaches the CLI
        through argv — so the file is simply appended to the prompt and the
        reviewer sees the whole diff in one turn.

        Accepting the argument at all is what keeps the call from raising
        TypeError: `orchestrator._step_submitting` passes it by name precisely
        so a provider that cannot take it "fails loudly at the call rather
        than silently sending a review request whose diff never arrived", and
        this provider previously could not.

        OPERATOR PATCH, 2026-08-21, superseded by codex-01. codex-01's
        app-server transport carries an oversized diff as numbered parts over
        a resumable thread, which is strictly better than one enormous argv;
        this exists so that codex-01's OWN 228 KB candidate could reach a
        reviewer at all. Remove it once that transport is the configured
        provider.
        """
        if request_id in self._responses:
            return SubmitResult.ALREADY_PERSISTED

        if attachment:
            diff = Path(attachment).read_text(encoding="utf-8")
            prompt = f"{prompt}\n\n{diff}"
            # argv is bounded by ARG_MAX (1 MiB on this host). Refusing here
            # with a named reason beats letting subprocess raise a bare E2BIG
            # that reads like a crash rather than a size limit.
            if len(prompt.encode("utf-8")) > _ARGV_BUDGET_BYTES:
                raise BrowserError(
                    f"the review prompt for {request_id} is "
                    f"{len(prompt.encode('utf-8'))} bytes once the "
                    f"{len(diff)}-character diff is inlined, over the "
                    f"{_ARGV_BUDGET_BYTES}-byte argv budget. Use a transport "
                    "that chunks (codex-01's app-server) rather than raising "
                    "this ceiling."
                )

        try:
            result = self._runner.run(prompt)
        except Exception as exc:
            # An invocation that never produced an exit code at all — the binary
            # could not be launched, or the process was killed at the timeout.
            # Still a failed invocation and still owed a record: the routing is
            # unchanged (the error is re-raised untouched, so the orchestrator
            # sees exactly what it saw before), but "why did this fail" is now
            # answerable from the transcript rather than only from whatever the
            # handler upstream happened to print. `returncode` is None, not a
            # fabricated -1.
            self._log(
                "codex_invocation_failed",
                failure_digest(
                    None,
                    "",
                    "",
                    prompt,
                    request_id=request_id,
                    note=(
                        "the invocation never returned an exit code — "
                        f"{type(exc).__name__}: {exc}"
                    ),
                ),
            )
            raise

        if result.returncode != 0:
            # `prompt` — the FINAL one, after any attachment was inlined above,
            # which is the text that actually reached the process. This is the
            # guard: an output line the prompt accounts for is not part of the
            # haystack, and a marker whose letters occur in what we SENT cannot
            # classify what came back — because `codex exec` echoes the whole
            # prompt onto stderr and every word of the review packet is
            # therefore inside the string being matched. See `quota.py`.
            failure = classify(
                result.returncode,
                result.stdout,
                result.stderr,
                prompt,
                quota_patterns=self._quota_patterns,
                rate_limit_patterns=self._rate_limit_patterns,
            )
            # Logged on EVERY failure, not only unrecognised ones: this is what
            # turns the first real exhaustion into a one-line config fix
            # instead of an investigation. See `quota.py`.
            self._log(
                "codex_invocation_failed",
                failure_digest(
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    prompt,
                    request_id=request_id,
                    classification=failure,
                ),
            )
            if failure.is_exhaustion:
                raise QuotaExhaustedError(
                    "the Codex allowance for this ChatGPT plan is exhausted "
                    f"(exit {result.returncode}; codex's own error output says "
                    f"{failure.matched!r}, which is not text this prompt sent "
                    "it). Codex shares an agentic pool with ChatGPT Work and "
                    "ChatGPT for Excel; ordinary ChatGPT conversations draw on "
                    "a separate quota, which is why the browser fallback can "
                    "still run."
                )
            # Everything else — a transient throttle and an unrecognised fault
            # alike — is REJECTED, which is retryable and is not loop_fatal. A
            # short-window 429 is emphatically NOT a spent allowance and must
            # not reach the branch above, which parks with no retry path.
            return SubmitResult.REJECTED

        reply = result.stdout.strip()
        if not reply:
            # A clean exit with nothing on stdout is a broken invocation, not a
            # reply. Reporting REJECTED keeps it retryable; calling it CONFIRMED
            # would hand the contract parser an empty string and spend a parse
            # retry saying so.
            self._log(
                "codex_invocation_failed",
                failure_digest(
                    result.returncode,
                    result.stdout,
                    result.stderr,
                    prompt,
                    request_id=request_id,
                    note="exited 0 with no reply on stdout",
                ),
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
