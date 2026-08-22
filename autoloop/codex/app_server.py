"""Driving the local Codex app-server over stdio JSON-RPC.

`codex app-server` ships with codex-cli 0.147.0 and speaks the protocol
committed at `docs/codex-app-server-protocol.generated.ts`. It is LOCAL and
needs no metered API key of its own, and no SDK package is involved: the
published `@openai/codex-sdk` is TypeScript, there is no `codex_sdk` on this
host, and adding a line to `autoloop/requirements.txt` would not make one
importable inside a worker. So this is a subprocess over stdio — the same shape
`SubprocessCodexRunner` already uses for `codex exec`, one level lower.

What that buys, and it is the only reason to do it: **a thread**. `codex exec`
is a fresh process per turn, so `codex/conversation.py` must refuse
`supports_chunked_delivery` — parts deposited into it would be reviewed as
separate fragments of nothing. Here the parts and the question that follows
them are turns on ONE thread, and `thread/read` reads them back as one context.

Three things this deliberately does NOT do:

* **It does not resume.** `thread/resume` is real and is right there in the
  protocol; this client never sends it and holds its thread only in memory, so
  a restarted loop starts a new thread. Durability across restarts is codex-03.
* **It does not claim a sandbox.** No preset is selected, named or enforced.
  Containment here is the same containment the subprocess adapter states and
  can prove: the server runs with `cwd` outside the checkout, and every
  approval the server asks for is answered `abort`, so a turn that tries to run
  a command or write a patch is refused rather than confined. That is a
  property of this client's replies, not of a flag whose name nobody verified.
* **It does not read stderr.** The child's stderr goes to `DEVNULL`. Partly
  because an undrained pipe deadlocks a chatty server, and partly because the
  point of the exercise is to classify failures from protocol fields
  (`protocol_errors.py`) rather than from whatever the process printed.

**Framing.** Messages are newline-delimited JSON, one object per line. The
committed reference is a set of TYPE declarations and settles the message
*shapes*, not how they are delimited on the pipe, so this is the transport's
one unverifiable structural choice; a server that answers with LSP-style
`Content-Length:` headers gets a named error saying exactly that rather than a
JSON decode failure fifty lines into a stack trace.
"""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ..errors import BrowserError, ResponseTimeoutError, SessionLostError
from . import wire
from .protocol_errors import (
    DEFAULT_QUOTA_ERROR_CODES,
    CodexProtocolError,
    classify,
    failure_digest,
)

#: How long one blocking read may sit before the client re-checks its own
#: deadline. Small enough that a timeout is reported promptly, large enough not
#: to spin.
_READ_SLICE_SECONDS = 0.5

#: Budget for the courtesy `turn/interrupt` sent when a turn times out. Short,
#: and every failure inside it is swallowed: this is tidying up after a
#: transport that has already been declared broken.
_INTERRUPT_BUDGET_SECONDS = 5.0

#: How much of a non-JSON line is worth logging. A server banner is useful
#: once; an unbounded dump into the transcript is not.
_JUNK_LINE_CHARS = 200

#: What this client calls itself in `initialize`. `ClientInfo` is in the
#: committed reference; the values are ours.
CLIENT_NAME = "autoloop"
CLIENT_VERSION = "1"


class AppServerTransport(Protocol):
    """A line-oriented pipe to an app-server.

    A protocol for the same reason `CodexRunner` is one: the test suite must
    never launch a real binary, and codex-cli is not installed everywhere the
    suite runs. `read_line` has three outcomes and they are all meaningful —
    a line, `None` for "nothing within the timeout", and `EOFError` for "the
    server is gone" — because collapsing the last two would turn a dead child
    into a slow one.
    """

    def start(self) -> None: ...

    def send_line(self, line: str) -> None: ...

    def read_line(self, timeout: float) -> str | None: ...

    def stop(self) -> None: ...


class SubprocessAppServer:
    """The real transport: `codex app-server` on stdin/stdout.

    Argv only, never a shell — same rule as `git_gateway` and `audit.agents`.
    Nothing model-authored reaches the command line at all here (prompts travel
    as JSON on stdin), which is a second reason this shape is better than
    `codex exec`: the 700 KB argv ceiling that stopped this task's own
    candidate from reaching a reviewer does not exist on a pipe.
    """

    def __init__(
        self,
        command: tuple[str, ...] = ("codex", "app-server"),
        *,
        cwd: Path | None = None,
        env: dict | None = None,
    ):
        self._command = tuple(command)
        # Default to the user's home, never the repository: the reviewer's
        # prompt is self-contained, so it needs no filesystem, and a cwd
        # outside the checkout is a containment that can be stated without
        # knowing any sandbox flag's name.
        self._cwd = Path(cwd) if cwd else Path.home()
        self._env = env
        self._proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._reader: threading.Thread | None = None

    @property
    def argv_preview(self) -> tuple[str, ...]:
        """The invocation, for `doctor` and diagnostics. Carries no prompt —
        there is nowhere in this transport for a prompt to reach argv."""
        return self._command

    def start(self) -> None:
        if self._proc is not None:
            return
        try:
            # An argv list, never a shell.
            self._proc = subprocess.Popen(
                list(self._command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                # DEVNULL, not PIPE: an undrained stderr pipe deadlocks the
                # child the moment it fills, and this transport classifies
                # failures from protocol fields rather than printed text.
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(self._cwd),
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise BrowserError(
                f"the codex app-server was not found ({self._command[0]!r}). "
                "Install codex-cli and sign in with `codex login`, or set "
                "conversation.provider back to 'codex_cli' / 'browser_chatgpt'."
            ) from exc
        self._reader = threading.Thread(
            target=self._drain_stdout, name="codex-app-server-stdout", daemon=True
        )
        self._reader.start()

    def _drain_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:  # pragma: no cover - defensive
            return
        try:
            for line in proc.stdout:
                self._lines.put(line)
        except (ValueError, OSError):  # pragma: no cover - closed under us
            pass
        finally:
            self._lines.put(None)  # EOF sentinel

    def send_line(self, line: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise SessionLostError("the codex app-server is not running")
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            raise SessionLostError(
                f"the codex app-server closed its stdin ({type(exc).__name__})"
            ) from exc

    def read_line(self, timeout: float) -> str | None:
        try:
            line = self._lines.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            return None
        if line is None:
            raise EOFError("the codex app-server closed its stdout")
        return line

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (ValueError, OSError):  # pragma: no cover - already closed
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
            proc.kill()
        except (ValueError, OSError):  # pragma: no cover
            pass


@dataclass(frozen=True)
class ThreadEntry:
    """One item read back off a thread.

    `is_agent` and `is_user` are both false for an item whose shape this client
    cannot classify — the two flags are evidence, not a partition, and a caller
    that needs "the reviewer answered" must require `is_agent` rather than
    infer it from `not is_user`.
    """

    text: str
    is_agent: bool
    is_user: bool


class _Turn:
    """What one `turn/start` has produced so far.

    Deltas are preferred over completed items when both arrive: a delta stream
    is unambiguously the assistant speaking, whereas `item/completed` fires for
    the input item too and telling them apart depends on field spellings the
    committed reference cannot confirm (`wire.is_agent_item`).
    """

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.deltas: list[str] = []
        self.items: list[str] = []
        self.started = False
        self.completed = False
        self.responded = False
        self.seen_methods: list[str] = []

    @property
    def finished(self) -> bool:
        """When this turn may be read out.

        `turn/completed` is the signal, and the fallback is deliberately narrow:
        a response plus some text, but ONLY if no `turn/started` was ever seen.
        The tempting wider rule — "stop as soon as there is text" — is wrong on
        exactly the turns that matter. A reviewer that says "let me look at part
        3" and then keeps working would have its aside taken as the verdict,
        which is a silently wrong review; waiting for a completion that never
        comes is a loud timeout. Prefer the loud one.
        """
        if self.completed:
            return True
        return self.responded and not self.started and bool(self.text())

    def text(self) -> str:
        joined = "".join(self.deltas).strip()
        if joined:
            return joined
        return "\n\n".join(part for part in self.items if part.strip()).strip()


class AppServerClient:
    """JSON-RPC over one app-server process. Synchronous by construction:
    exactly one request is ever in flight, so there is no correlation table to
    get wrong and no background dispatch to reason about."""

    def __init__(
        self,
        transport: AppServerTransport,
        *,
        timeout_seconds: float = 900.0,
        quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES,
        working_dir: str = "",
        log: Callable[[str, dict], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport
        self._timeout = timeout_seconds
        self._quota_codes = tuple(quota_codes) or DEFAULT_QUOTA_ERROR_CODES
        self._working_dir = working_dir
        self._log = log or (lambda event, data: None)
        self._clock = clock
        self._next = 0
        self._started = False

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Launch and handshake. Idempotent — `attach()` is called before every
        phase, including polling phases."""
        if self._started:
            return
        self._transport.start()
        self._request(
            wire.METHOD_INITIALIZE,
            {
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                "capabilities": {
                    # The methods this transport uses are behind the
                    # experimental flag the reference was generated with.
                    "experimentalApi": True,
                    "requestAttestation": False,
                },
            },
        )
        self._notify(wire.NOTIFICATION_INITIALIZED, None)
        self._started = True

    def close(self) -> None:
        self._started = False
        self._transport.stop()

    # ---- protocol calls ----------------------------------------------------

    def start_thread(self) -> str:
        """Open a thread and return its id.

        The id may arrive on the result or on the `thread/started`
        notification; both are consulted, and neither being readable is a named
        error rather than a `KeyError` — see `wire.py` on why the params
        spellings cannot be pinned from the committed reference.
        """
        params: dict[str, Any] = {}
        if self._working_dir:
            params[wire.THREAD_CWD_KEY] = self._working_dir
        notifications: list[dict] = []
        result = self._request(
            wire.METHOD_THREAD_START, params, sink=notifications.append
        )
        thread_id = wire.find_thread_id(result)
        for note in notifications:
            if thread_id:
                break
            if note.get("method") == wire.NOTIFICATION_THREAD_STARTED:
                thread_id = wire.find_thread_id(note.get("params"))
        if not thread_id:
            raise CodexProtocolError(
                "thread/start returned no readable thread id — looked for "
                f"{list(wire.THREAD_ID_KEYS)} on the result and on any "
                "thread/started notification. The app-server protocol may have "
                "moved; regenerate docs/codex-app-server-protocol.generated.ts."
            )
        return thread_id

    def read_thread(self, thread_id: str) -> list[ThreadEntry]:
        """Every item on the thread, oldest first, each labelled with who said
        it.

        This is what makes `reconcile` an authority rather than a memory: it
        asks the SERVER what is on the thread instead of reporting what this
        process believes it sent. The labels matter as much as the text — "is
        there a reply after this request" is a different question from "is
        there more text after this request", and a reasoning summary would
        answer the second one wrongly.
        """
        result = self._request(
            wire.METHOD_THREAD_READ, {wire.THREAD_ID_KEYS[0]: thread_id}
        )
        items = wire.thread_items(result)
        if items is None:
            raise CodexProtocolError(
                "thread/read returned a shape this client cannot read — looked "
                f"for {list(wire.THREAD_ITEMS_KEYS)}. Refusing to report a "
                "thread as empty on the strength of an unreadable answer."
            )
        entries = []
        for raw in items:
            item = wire.unwrap_item(raw)
            entries.append(
                ThreadEntry(
                    text=wire.item_text(item),
                    is_agent=wire.is_agent_item(item),
                    is_user=wire.is_user_item(item),
                )
            )
        return entries

    def run_turn(self, thread_id: str, text: str, *, timeout: float | None = None) -> str:
        """Send one turn and return the reviewer's reply.

        Completion is `turn/completed`, or — only for a server that never
        announced a turn at all — the `turn/start` response arriving with text
        on it. See `_Turn.finished` for why the second rule is that narrow.
        """
        request_id = self._send_request(
            wire.METHOD_TURN_START,
            {
                wire.THREAD_ID_KEYS[0]: thread_id,
                wire.TURN_INPUT_KEY: [
                    {"type": wire.INPUT_TEXT_TYPE, "text": text}
                ],
            },
        )
        deadline = self._clock() + (timeout or self._timeout)
        turn = _Turn(thread_id)
        try:
            while not turn.finished:
                message = self._read_message(deadline, context="turn/start")
                self._handle(message, request_id, turn)
        except ResponseTimeoutError:
            self._interrupt(thread_id)
            raise
        reply = turn.text()
        if not reply:
            raise CodexProtocolError(
                f"the turn on thread {thread_id} completed without any readable "
                f"assistant message (saw {sorted(set(turn.seen_methods))}). "
                "The notification shapes may have moved; regenerate "
                "docs/codex-app-server-protocol.generated.ts."
            )
        return reply

    # ---- message plumbing --------------------------------------------------

    def _handle(self, message: dict, request_id: int, turn: _Turn) -> None:
        method = message.get("method")
        if method is not None and "id" in message:
            self._answer_server_request(message)
            return
        if method is not None:
            turn.seen_methods.append(str(method))
            self._absorb_notification(method, message.get("params"), turn)
            return
        if message.get("id") != request_id:
            # A response to a request nobody is waiting for. Only reachable if
            # the server answers out of order, which a one-in-flight client
            # cannot produce; dropping it is safe and staying silent about it
            # is not.
            self._log("codex_app_server_stray_response", {"id": message.get("id")})
            return
        turn.responded = True
        error = message.get("error")
        if error:
            self._raise(error, context="turn/start")
        result = message.get("result")
        if isinstance(result, dict):
            self._absorb_item(result, turn)

    def _absorb_notification(self, method: str, params: Any, turn: _Turn) -> None:
        if method == wire.NOTIFICATION_ERROR:
            self._raise(params, context="a turn")
        elif method == wire.NOTIFICATION_TURN_STARTED:
            turn.started = True
        elif method == wire.NOTIFICATION_ITEM_AGENT_MESSAGE_DELTA:
            turn.deltas.append(wire.item_text(wire.unwrap_item(params)))
        elif method == wire.NOTIFICATION_ITEM_COMPLETED:
            self._absorb_item(params, turn)
        elif method == wire.NOTIFICATION_TURN_COMPLETED:
            if not turn.text():
                # Last chance: a server that carries the final message on the
                # completion notification rather than as its own item.
                self._absorb_item(params, turn)
            turn.completed = True

    @staticmethod
    def _absorb_item(payload: Any, turn: _Turn) -> None:
        item = wire.unwrap_item(payload)
        if not wire.is_agent_item(item):
            return
        text = wire.item_text(item)
        if text:
            turn.items.append(text)

    def _answer_server_request(self, message: dict) -> None:
        """Answer every server->client request — approvals with `abort`,
        everything else with an error.

        Silence is the one forbidden option: the server blocks on its own
        request, and the turn would sit there until this client's timeout with
        nothing in the transcript to say why.
        """
        method = str(message.get("method", ""))
        request_id = message.get("id")
        if method in wire.DENIABLE_SERVER_REQUEST_METHODS:
            self._log("codex_app_server_approval_denied", {"method": method})
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"decision": wire.REVIEW_DECISION_ABORT},
                }
            )
            return
        self._log("codex_app_server_request_refused", {"method": method})
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": (
                        f"autoloop's reviewer transport does not service {method!r}: "
                        "it grants no approvals and provides no tools."
                    ),
                },
            }
        )

    def _request(
        self,
        method: str,
        params: Any,
        *,
        sink: Callable[[dict], None] | None = None,
        timeout: float | None = None,
    ) -> dict:
        request_id = self._send_request(method, params)
        deadline = self._clock() + (timeout or self._timeout)
        while True:
            message = self._read_message(deadline, context=method)
            if message.get("method") is not None:
                if "id" in message:
                    self._answer_server_request(message)
                    continue
                # Raised BEFORE the sink sees it, and regardless of whether
                # there is a sink: a caller collecting notifications for their
                # content must not be able to turn a reported failure into a
                # collected one.
                if message.get("method") == wire.NOTIFICATION_ERROR:
                    self._raise(message.get("params"), context=method)
                if sink is not None:
                    sink(message)
                continue
            if message.get("id") != request_id:
                self._log("codex_app_server_stray_response", {"id": message.get("id")})
                continue
            error = message.get("error")
            if error:
                self._raise(error, context=method)
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def _interrupt(self, thread_id: str) -> None:
        """Best-effort `turn/interrupt` after a timeout, so a thread is not
        left generating into a client that has stopped listening. Every failure
        is swallowed: the caller is already raising."""
        try:
            self._request(
                wire.METHOD_TURN_INTERRUPT,
                {wire.THREAD_ID_KEYS[0]: thread_id},
                timeout=_INTERRUPT_BUDGET_SECONDS,
            )
        except Exception:  # tidying up after a failure that is already raising
            self._log("codex_app_server_interrupt_failed", {"thread_id": thread_id})

    def _raise(self, error: Any, *, context: str) -> None:
        digest = failure_digest(error if isinstance(error, dict) else {"message": str(error)})
        # Logged on EVERY failure, not only unrecognised ones — the same rule
        # `codex/quota.py` follows, and what turns the first real exhaustion
        # into a config edit instead of an investigation.
        self._log("codex_app_server_failed", {**digest, "context": context})
        raise classify(error, quota_codes=self._quota_codes, context=context)

    def _read_message(self, deadline: float, *, context: str) -> dict:
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ResponseTimeoutError(
                    f"the codex app-server did not answer {context} within "
                    f"{self._timeout}s"
                )
            try:
                line = self._transport.read_line(min(remaining, _READ_SLICE_SECONDS))
            except EOFError as exc:
                raise SessionLostError(
                    f"the codex app-server exited while answering {context}"
                ) from exc
            if line is None:
                continue
            line = line.strip()
            if not line:
                continue
            if line.lower().startswith("content-length:"):
                raise CodexProtocolError(
                    "the codex app-server is framing messages with LSP-style "
                    "Content-Length headers; this transport speaks "
                    "newline-delimited JSON. The framing is not settled by "
                    "docs/codex-app-server-protocol.generated.ts (it declares "
                    "message types, not delimiters) — see app_server.py."
                )
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                # A banner or a log line on stdout. Worth one bounded record;
                # not worth failing a review over.
                self._log(
                    "codex_app_server_junk_line", {"line": line[:_JUNK_LINE_CHARS]}
                )
                continue
            if isinstance(message, dict):
                return message

    def _send_request(self, method: str, params: Any) -> int:
        self._next += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next, "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)
        return self._next

    def _notify(self, method: str, params: Any) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._send(payload)

    def _send(self, payload: dict) -> None:
        self._transport.send_line(json.dumps(payload, ensure_ascii=False))
