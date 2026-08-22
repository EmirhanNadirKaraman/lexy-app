"""`LLMConversation` over the local Codex app-server.

The subprocess adapter next door (`codex/conversation.py`) is honest about what
it is: one process per turn, therefore no shared history, therefore
`supports_chunked_delivery` must stay unset — "parts sent to it would be
reviewed as separate fragments". That refusal cost real work. port-01 lost a
whole attempt when a 414,596-byte diff met the 400,000-byte packet cap
(`review_packet_build_failed`), and on 2026-08-21 this task's own 228 KB
candidate could not be submitted at all until an operator patch inlined it
through argv.

This adapter has a thread, so it does not have to refuse. What it declares, and
exactly what each declaration means:

**`supports_chunked_delivery = True`.** One thread is held for the life of this
object, and every turn — each numbered part, then the message that asks for a
verdict — goes onto it. That is a claim about SHARED HISTORY and nothing else:
the reviewer answering the verdict question has the parts in its context, and
`reconcile` proves it by asking the server (`thread/read`) rather than by
consulting this process's memory. It is not a claim about size limits, and it
is not a claim about surviving a restart.

**`idempotent_submit = False`, set explicitly rather than omitted.** The
subprocess adapter may declare it because a failed `codex exec` appended
nothing anywhere. `turn/start` is not like that: the input is appended to the
thread and THEN the turn runs, so a failure after the append would double-post
on a retry. Declaring the property here would trade a park for silent duplicate
turns in a reviewer's context. What replaces it is better than either: because
the thread is real, `reconcile` can ask the server what actually happened, and
answers True only when the request is on the thread AND has an answer to hand
back. A turn that was appended and never answered reconciles False and parks
`submission_ambiguous` — the correct outcome, arrived at from evidence rather
than from a declaration.

**No restart durability, and no sandbox preset.** `thread/resume` exists in the
protocol and this adapter never sends it; the thread lives in memory and a new
process starts a new thread. No sandbox preset is selected, named or enforced.
Both belong to codex-03. Containment here is what `app_server.py` can prove:
the server runs outside the checkout and every approval it requests is answered
`abort`.

**No rotation, by omission** — same as the subprocess adapter. `retarget` /
`current_url` are absent, so `orchestrator._client_for_request`'s `getattr`
probe skips retargeting and rotation is structurally unreachable. Every
rotation trigger describes a browser conversation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ..browser.chatgpt import SubmitResult
from ..errors import BrowserError, ResponseTimeoutError
from ..packet import diff_part_id
from .app_server import AppServerClient
from .protocol_errors import CodexProtocolError

#: How much attachment text one deposited part carries. Nothing here fights a
#: composer — the text travels as JSON on a pipe — so this is sized for the
#: reviewer's benefit rather than the transport's: parts small enough to be
#: quoted back, few enough that a patch does not become a hundred messages.
DEFAULT_PART_CHARS = 60_000

#: Ceiling on an attachment this adapter will deliver at all. A refusal with a
#: named reason beats spending an hour of turns on a patch nobody could review;
#: `submit` raises rather than silently truncating, because a diff delivered
#: minus its tail is the one failure mode chunking exists to prevent.
DEFAULT_MAX_ATTACHMENT_CHARS = 4_000_000

#: Independent of the character ceiling: a patch that is small enough but
#: pathologically split (a tiny `part_chars`) must not turn into hundreds of
#: turns either.
MAX_PARTS = 64


def _render_part(part_id: str, index: int, total: int, body: str) -> str:
    """One deposited part.

    Worded like `packet._render_part`, and for the same reason: a part is a
    deposit, not a question. Asking for a verdict here would invite approval of
    a partial patch, which is strictly worse than not sending it.
    """
    return "\n".join(
        [
            f"[autoloop review diff part {index} of {total} | {part_id}]",
            "",
            f"This is part {index} of {total} of the patch for the review "
            "request that follows on this same thread. It is NOT a question "
            "and needs no reply — do not review, approve or comment yet. The "
            "message asking for your verdict comes last and carries the "
            "response contract.",
            "",
            body,
        ]
    )


def _delivery_notice(first: str, last: str, total: int) -> str:
    """Appended to the verdict prompt when parts were deposited for it.

    Necessary, not decorative: the packet the orchestrator built says the diff
    is ATTACHED as a file, because that is what it asked the transport to do.
    This transport has no upload, so without this note the reviewer would be
    told to read an attachment that never arrived and would correctly refuse to
    approve. The note describes delivery only — the packet's own contents,
    and therefore its `report_sha256`, are untouched.
    """
    return (
        "\n\n[autoloop transport note — codex app-server]\n"
        "The file named as ATTACHED above was not uploaded: this transport has "
        "no upload. Its full contents reached you on THIS SAME THREAD, as the "
        f"{total} numbered parts immediately before this message ({first} … "
        f"{last}). Nothing was truncated and nothing was summarised. Treat "
        "those parts as the attachment: read them, then answer this message."
    )


class CodexAppServerConversation:
    """One thread, many turns. See the module docstring for what each declared
    capability does and does not claim."""

    #: See the module docstring. One thread spans the parts and the verdict.
    supports_chunked_delivery = True

    #: Explicitly False. `turn/start` appends its input before the turn runs,
    #: so a failure afterwards may leave the prompt on the thread and a retry
    #: would post it twice. `reconcile` resolves the ambiguity from the server
    #: instead of a declaration resolving it from optimism.
    idempotent_submit = False

    def __init__(
        self,
        client: AppServerClient,
        *,
        part_chars: int = DEFAULT_PART_CHARS,
        max_attachment_chars: int = DEFAULT_MAX_ATTACHMENT_CHARS,
        log: Callable[[str, dict], None] | None = None,
    ):
        self._client = client
        self._part_chars = max(1, int(part_chars))
        self._max_attachment_chars = int(max_attachment_chars)
        self._log = log or (lambda event, data: None)
        self._thread_id: str = ""
        #: request_id -> captured reply. A cache of what this process observed
        #: or recovered from the thread, never a claim about the world on its
        #: own; `reconcile` is what consults the server.
        self._responses: dict[str, str] = {}

    # ---- lifecycle ---------------------------------------------------------

    def attach(self) -> None:
        """Start the app-server and handshake. Cheap and idempotent, as the
        contract requires — it is called before every phase, including polling
        phases. Deliberately does NOT open a thread: a thread is opened by the
        first turn that needs one, so an `attach` during a polling phase costs
        nothing on the account."""
        self._client.start()

    def close(self) -> None:
        self._client.close()
        self._thread_id = ""

    @property
    def thread_id(self) -> str:
        """The live thread, or `""` before the first turn. Exposed for
        diagnostics and for the tests that prove one thread spans a delivery."""
        return self._thread_id

    # ---- reads -------------------------------------------------------------

    def has_request(self, request_id: str) -> bool:
        """Whether this process has a reply in hand for `request_id`.

        The cheap, local read — the counterpart of the browser adapter's "in
        the currently loaded history". `reconcile` is the one that asks the
        server.
        """
        return request_id in self._responses

    def reconcile(self, request_id: str) -> bool:
        """Ask the SERVER whether this request is on the thread and answered.

        Both halves matter. The marker alone would report a turn that was
        appended and then failed as persisted, and `_step_submitting` would
        move to `awaiting` for a reply that does not exist and never will. An
        answer alone cannot be attributed. Together they mean exactly what the
        orchestrator needs: this request was asked and there is a reply to hand
        back, so nothing needs re-sending.

        A reply found here is stashed, so a submission whose turn completed on
        the server while this client was dying is recovered rather than
        re-asked.
        """
        if request_id in self._responses:
            return True
        if not self._thread_id:
            return False
        entries = self._client.read_thread(self._thread_id)
        marker = next(
            (i for i, entry in enumerate(entries) if request_id in entry.text), None
        )
        if marker is None:
            return False
        # `is_agent`, not "the next non-empty item": a reasoning summary sits
        # between the request and the answer on a thinking model, and handing
        # one back as the verdict would put the contract parser to work on the
        # reviewer's private notes.
        reply = next(
            (
                entry.text.strip()
                for entry in entries[marker + 1:]
                if entry.is_agent and entry.text.strip()
            ),
            "",
        )
        if not reply:
            self._log(
                "codex_app_server_turn_unanswered",
                {"request_id": request_id, "items": len(entries)},
            )
            return False
        self._responses[request_id] = reply
        return True

    def await_response(self, request_id: str) -> str:
        """Return the reply captured by `submit` (or recovered by `reconcile`).
        The waiting already happened — a turn is driven to completion inside
        `submit`, so there is nothing to poll for."""
        reply = self._responses.get(request_id)
        if reply is None:
            raise ResponseTimeoutError(
                f"no codex app-server reply was captured for {request_id}; the "
                "turn did not complete in this process"
            )
        return reply

    # ---- actions -----------------------------------------------------------

    def submit(
        self, request_id: str, prompt: str, attachment: str | None = None
    ) -> SubmitResult:
        """Send one turn — or, with an attachment, its parts and then the turn.

        `attachment` is an absolute path to an oversized diff the orchestrator
        could not inline (`orchestrator._write_diff_attachment`). Accepting the
        parameter is not optional: `_step_submitting` passes it BY NAME, so a
        provider without it raises TypeError and kills the loop — measured
        twice on 2026-08-21.

        What this transport does with it is deposit it as numbered parts on the
        thread this request's verdict question will be asked on, and then ask
        the question. That is the whole point of the thread, and it is the
        `supports_chunked_delivery` claim exercised inside a single submit:
        the parts and the question share one context, and `thread/read` can
        show it.
        """
        if request_id in self._responses:
            return SubmitResult.ALREADY_PERSISTED

        self.attach()
        if attachment:
            prompt = self._deposit_attachment(request_id, prompt, attachment)
        reply = self._turn(prompt)
        self._responses[request_id] = reply
        return SubmitResult.CONFIRMED

    # ---- internals ---------------------------------------------------------

    def _deposit_attachment(self, request_id: str, prompt: str, attachment: str) -> str:
        """Send the attachment as numbered parts; return the annotated prompt.

        All-or-nothing by construction: any part that fails raises out of
        `submit`, so the verdict question is never asked on a partial patch.
        That is the same rule `orchestrator._step_delivering` enforces for the
        provider-level chunking path, held here by the fact that a failed turn
        raises rather than returning a value the caller might ignore.
        """
        diff = Path(attachment).read_text(encoding="utf-8")
        if len(diff) > self._max_attachment_chars:
            raise BrowserError(
                f"the diff attached to {request_id} is {len(diff)} characters, "
                f"over this transport's {self._max_attachment_chars}-character "
                "ceiling (codex.app_server_max_attachment_chars). Refusing to "
                "deliver a patch this large rather than truncating it."
            )
        bodies = [
            diff[start: start + self._part_chars]
            for start in range(0, len(diff), self._part_chars)
        ] or [""]
        if len(bodies) > MAX_PARTS:
            raise BrowserError(
                f"the diff attached to {request_id} would need {len(bodies)} "
                f"parts, over the {MAX_PARTS}-part ceiling. Raise "
                "codex.app_server_part_chars rather than sending a review as "
                "hundreds of turns."
            )
        total = len(bodies)
        part_ids = [diff_part_id(request_id, i, total) for i in range(1, total + 1)]
        for index, (part_id, body) in enumerate(zip(part_ids, bodies), start=1):
            self._turn(_render_part(part_id, index, total, body))
            self._log(
                "codex_app_server_part_delivered",
                {
                    "request_id": request_id,
                    "part_id": part_id,
                    "index": index,
                    "total": total,
                    "chars": len(body),
                    "thread_id": self._thread_id,
                },
            )
        return prompt + _delivery_notice(part_ids[0], part_ids[-1], total)

    def _turn(self, text: str) -> str:
        return self._client.run_turn(self._ensure_thread(), text)

    def _ensure_thread(self) -> str:
        """The one thread, opened on first use.

        Cached for the life of this object, which is what makes every turn of a
        single review request — parts, then verdict — share a context. It is
        never resumed: a new process opens a new thread, and this adapter
        promises nothing else (codex-03).
        """
        if not self._thread_id:
            self._thread_id = self._client.start_thread()
            self._log("codex_app_server_thread_started", {"thread_id": self._thread_id})
        return self._thread_id


__all__ = [
    "DEFAULT_MAX_ATTACHMENT_CHARS",
    "DEFAULT_PART_CHARS",
    "MAX_PARTS",
    "CodexAppServerConversation",
    "CodexProtocolError",
]
