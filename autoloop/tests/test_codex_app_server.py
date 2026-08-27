"""The Codex app-server reviewer: one thread, structured errors, honest claims.

No codex binary is involved anywhere in this file. `AppServerTransport` is a
protocol for exactly the reason `CodexRunner` is one — the suite must never
launch a real binary, and codex-cli is not installed everywhere it runs — so
every test drives a `FakeAppServer` that speaks the committed protocol over an
in-process pipe.

Four claims are under test, and they are worded as narrowly as the transport
states them:

1. **ONE THREAD SPANS A DELIVERY.** An oversized diff reaches the reviewer as
   numbered parts and the verdict question reaches it afterwards, all on one
   thread, within a single `submit`. Proved by reading the thread BACK off the
   server (`thread/read`) and by the reviewer's answer naming the part ids —
   which it could only know from earlier turns in the same context.
2. **STRUCTURED ERRORS.** Exhaustion is a named field on an error object, not a
   substring of a text blob. The counter-test matters more than the positive
   one: an error whose prose says "usage limit" but whose TYPE is unknown must
   NOT be classified as exhaustion.
3. **`idempotent_submit` IS FALSE, AND THAT IS THE HONEST ANSWER.**
   `turn/start` appends its input and then runs, so the failing-mid-append case
   leaves the prompt on the thread — which the tests demonstrate by reading it
   back — and a retry would double-post.
4. **NOTHING WAS TAKEN AWAY.** The subprocess provider is unchanged and still
   selectable, `fallback_provider` can still name either, and orchestrator,
   policy and state are untouched.

Two claims are deliberately ABSENT and must stay absent: no test asserts a
thread survives a process restart, and none asserts a sandbox preset is
enforced. Both belong to codex-03.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from autoloop.codex import wire
from autoloop.codex.app_server import (
    AppServerClient,
    SubprocessAppServer,
    ThreadEntry,
)
from autoloop.codex.app_server_conversation import (
    MAX_PARTS,
    CodexAppServerConversation,
)
from autoloop.codex.conversation import CodexConversation
from autoloop.codex.protocol_errors import (
    DEFAULT_QUOTA_ERROR_CODES,
    DEFAULT_RATE_LIMIT_ERROR_CODES,
    QUOTA_EXHAUSTED,
    RATE_LIMITED,
    UNCLASSIFIED,
    CodexProtocolError,
    classification_of,
    classify,
    failure_digest,
    is_quota_exhausted,
    is_rate_limited,
    usable_codes,
)
from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    ConversationConfig,
    load_config,
)
from autoloop.conversation import (
    SubmitResult,
    available_providers,
    create_conversation,
)
from autoloop.errors import BrowserError, QuotaExhaustedError, ResponseTimeoutError
from autoloop.packet import diff_part_id
from autoloop.policy import PolicyConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE = REPO_ROOT / "docs" / "codex-app-server-protocol.generated.ts"

RID = "alr-codex-0007"
PROMPT = f"[autoloop request {RID} | iteration 1]\n\nreview this"
PART_RE = re.compile(r"diffpart_[a-z0-9_]+")


# ---- a fake app-server -----------------------------------------------------


class FakeAppServer:
    """An in-process app-server that speaks the committed protocol.

    Deliberately strict where strictness is what makes a test discriminating:
    a `turn/start` naming a thread it never opened is an AssertionError, not a
    polite error response. A transport that opened a fresh thread per turn
    would otherwise pass the shared-context test quietly.
    """

    def __init__(self, *, mode: str = "delta", reply=None):
        self.mode = mode  # delta | item | synchronous
        self.reply = reply or default_reply
        self.threads: dict[str, list[dict]] = {}
        self.sent: list[dict] = []
        self.outbox: list[str] = []
        self.started = False
        self.stopped = False
        self.turn_threads: list[str] = []
        #: `{"error": {...}, "append": bool}` consumed by the next turn/start.
        self.fail_next_turn: dict | None = None
        self.thread_read_result: dict | None = None
        self._threads_made = 0

    # -- transport surface --

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def read_line(self, timeout: float) -> str | None:
        return self.outbox.pop(0) if self.outbox else None

    def send_line(self, line: str) -> None:
        message = json.loads(line)
        self.sent.append(message)
        method = message.get("method")
        if method is None:
            return  # the client answering one of OUR requests; recorded above
        if "id" not in message:
            return  # a client notification; nothing to answer
        handler = getattr(self, f"_on_{method.replace('/', '_')}", None)
        assert handler is not None, f"the fake server was sent an unknown method {method!r}"
        handler(message)

    # -- helpers --

    def _emit(self, payload: dict) -> None:
        self.outbox.append(json.dumps(payload))

    def _respond(self, message: dict, result: dict) -> None:
        self._emit({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _notify(self, method: str, params: dict) -> None:
        self._emit({"jsonrpc": "2.0", "method": method, "params": params})

    def texts(self, thread_id: str) -> list[str]:
        return [wire.item_text(item) for item in self.threads[thread_id]]

    # -- methods --

    def _on_initialize(self, message: dict) -> None:
        self._respond(
            message,
            {
                "userAgent": "fake-app-server",
                "codexHome": "/tmp/codex",
                "platformFamily": "unix",
                "platformOs": "macos",
            },
        )

    def _on_thread_start(self, message: dict) -> None:
        self._threads_made += 1
        thread_id = f"thread-{self._threads_made}"
        self.threads[thread_id] = []
        self._respond(message, {"threadId": thread_id})
        self._notify("thread/started", {"threadId": thread_id})

    def _on_thread_read(self, message: dict) -> None:
        if self.thread_read_result is not None:
            self._respond(message, self.thread_read_result)
            return
        thread_id = message["params"]["threadId"]
        self._respond(message, {"items": list(self.threads[thread_id])})

    def _on_turn_interrupt(self, message: dict) -> None:
        self._respond(message, {})

    def _on_turn_start(self, message: dict) -> None:
        params = message["params"]
        thread_id = params["threadId"]
        assert thread_id in self.threads, (
            f"turn/start named thread {thread_id!r}, which this server never "
            "opened — the transport is not holding one thread"
        )
        self.turn_threads.append(thread_id)
        text = "".join(entry["text"] for entry in params["input"])
        items = self.threads[thread_id]
        user_item = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }

        failure = self.fail_next_turn
        if failure is not None:
            self.fail_next_turn = None
            if failure.get("append", True):
                # The shape the honesty claim rests on: the input is on the
                # thread and the turn then fails.
                items.append(user_item)
            self._emit(
                {"jsonrpc": "2.0", "id": message["id"], "error": failure["error"]}
            )
            return

        items.append(user_item)
        answer = self.reply(items, text)
        agent_item = {
            "type": "agent_message",
            "role": "assistant",
            "content": [{"type": "input_text", "text": answer}],
        }

        if self.mode == "synchronous":
            # A server that answers on the response and emits nothing.
            items.append(agent_item)
            self._respond(message, {"item": agent_item})
            return

        self._respond(message, {})
        self._notify("turn/started", {"threadId": thread_id})
        # The input item completes too — a transport that echoes it back would
        # hand the contract parser its own prompt.
        self._notify("item/completed", {"threadId": thread_id, "item": user_item})
        if self.mode == "delta":
            for chunk in _chunks(answer, 7):
                self._notify(
                    "item/agentMessage/delta",
                    {"threadId": thread_id, "delta": chunk},
                )
        items.append(agent_item)
        self._notify("item/completed", {"threadId": thread_id, "item": agent_item})
        self._notify("turn/completed", {"threadId": thread_id})


def _chunks(text: str, size: int) -> list[str]:
    return [text[i: i + size] for i in range(0, len(text), size)] or [""]


def default_reply(items, text) -> str:
    """A reviewer that can only answer correctly if it has the whole thread.

    A part gets an acknowledgement. The verdict question gets an answer naming
    every part id the THREAD has seen — not the ones in the current message,
    which carries none.
    """
    if "needs no reply" in text:
        return "ack"
    seen: list[str] = []
    for item in items:
        for found in PART_RE.findall(wire.item_text(item)):
            if found not in seen:
                seen.append(found)
    request = re.search(r"alr-[a-z0-9-]+", text)
    return (
        f"reviewed {request.group(0) if request else 'unknown'} "
        f"against parts: {', '.join(seen) if seen else 'none'}"
    )


def build(fake: FakeAppServer, **kwargs) -> CodexAppServerConversation:
    client = AppServerClient(fake, timeout_seconds=30.0, clock=_fake_clock())
    return CodexAppServerConversation(client, **kwargs)


def _fake_clock():
    """Monotonic and free. Every call advances by a tenth of a second, so a
    test that waits for a message that never comes reaches its deadline in a
    few hundred iterations rather than in real minutes."""
    state = {"t": 0.0}

    def clock() -> float:
        state["t"] += 0.1
        return state["t"]

    return clock


def attachment(tmp_path: Path, chars: int) -> str:
    path = tmp_path / "diff.md"
    path.write_text("D" * chars, encoding="utf-8")
    return str(path)


# ---- 1. one thread spans the delivery --------------------------------------


def test_an_oversized_diff_is_delivered_as_parts_and_the_verdict_shares_the_thread(
    tmp_path,
):
    fake = FakeAppServer()
    codex = build(fake, part_chars=100)

    assert codex.submit(RID, PROMPT, attachment=attachment(tmp_path, 450)) is (
        SubmitResult.CONFIRMED
    )

    # Five parts plus the verdict question, and every one of them on the SAME
    # thread. The fake asserts the thread exists; this asserts there was one.
    assert len(fake.turn_threads) == 6
    assert set(fake.turn_threads) == {codex.thread_id}
    assert len(fake.threads) == 1

    # Read the thread BACK off the server: parts first, in order, then the
    # question. One context, not six.
    user_texts = [
        text for text in fake.texts(codex.thread_id) if "review diff part" in text
    ]
    assert [re.search(r"part (\d) of (\d)", t).groups() for t in user_texts] == [
        ("1", "5"), ("2", "5"), ("3", "5"), ("4", "5"), ("5", "5")
    ]

    # And the verdict names the parts, which it could only learn from the
    # thread — the question itself carries no part bodies.
    reply = codex.await_response(RID)
    assert reply.startswith(f"reviewed {RID} against parts:")
    for index in range(1, 6):
        assert diff_part_id(RID, index, 5) in reply


def test_the_verdict_prompt_says_where_the_attachment_went(tmp_path):
    """The packet says the diff is ATTACHED as a file. This transport has no
    upload, so without the note the reviewer would be told to read something
    that never arrived — and would correctly refuse to approve."""
    fake = FakeAppServer()
    codex = build(fake, part_chars=200)
    codex.submit(RID, PROMPT, attachment=attachment(tmp_path, 500))

    question = [t for t in fake.texts(codex.thread_id) if RID in t][0]
    assert "was not uploaded" in question
    assert "THIS SAME THREAD" in question
    assert diff_part_id(RID, 1, 3) in question and diff_part_id(RID, 3, 3) in question
    # The packet's own text is untouched — the note is appended, never woven in.
    assert question.startswith(PROMPT)


def test_the_orchestrator_chunking_path_lands_on_the_same_thread():
    """The other chunking route: `_step_delivering` submits each part under its
    own id, then `_step_submitting` asks the question. Same thread, and
    `reconcile` confirms each part against the SERVER."""
    fake = FakeAppServer()
    codex = build(fake)
    part_id = diff_part_id(RID, 1, 1)

    assert codex.submit(part_id, f"[part | {part_id}] deposit") is SubmitResult.CONFIRMED
    assert codex.reconcile(part_id) is True
    assert codex.has_request(part_id) is True
    assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED

    assert set(fake.turn_threads) == {codex.thread_id}
    assert part_id in codex.await_response(RID)


def test_chunked_delivery_is_declared_and_the_subprocess_adapter_still_refuses():
    """The declaration is a claim about shared history. One transport has it,
    the other does not, and both statements must stay true."""
    assert CodexAppServerConversation.supports_chunked_delivery is True
    assert getattr(CodexConversation, "supports_chunked_delivery", False) is False


def test_a_thread_is_not_opened_until_a_turn_needs_one():
    """`attach()` runs before every phase, including polling phases. Opening a
    thread there would spend account state on a loop that is only waiting."""
    fake = FakeAppServer()
    codex = build(fake)
    codex.attach()
    codex.attach()
    assert fake.threads == {}
    assert codex.thread_id == ""
    # Handshake happened exactly once despite two attaches.
    assert [m.get("method") for m in fake.sent] == ["initialize", "initialized"]


def test_no_rotation_surface_is_exposed():
    """Rotation is a browser concept. `_client_for_request` probes with
    `getattr`, so omitting these makes it unreachable rather than disabled."""
    codex = build(FakeAppServer())
    assert not hasattr(codex, "retarget")
    assert not hasattr(codex, "current_url")


# ---- 2. structured errors --------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        {"code": -32000, "message": "no", "data": {"type": "usage_limit_reached"}},
        {"code": -32000, "message": "no", "data": {"kind": "Quota-Exceeded"}},
        {"code": -32000, "message": "no", "data": {"code": "out_of_credits"}},
    ],
)
def test_exhaustion_is_read_off_a_named_field(error):
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": error}
    codex = build(fake)
    with pytest.raises(QuotaExhaustedError) as exc:
        codex.submit(RID, PROMPT)
    # The message explains why a fallback is worth trying at all.
    assert "separate quota" in str(exc.value)


# ---- 2a. transient throttle vs spent allowance ------------------------------
#
# quota-01, 2026-08-23. Three of the cases above USED to live in the list: a
# `rate_limit_exceeded` type, a 429 status, and a bare `code: 429`. Every one of
# them raised `QuotaExhaustedError`, which is loop_fatal with NO retry path — so
# a thirty-second throttle parked the loop exactly as the stderr matcher next
# door did before this task split its lists. They are the same defect one
# transport over, and these are the regressions that keep them apart.


#: Every shape a short-window limit wears on this wire.
TRANSIENT_ERRORS = [
    {"code": -32000, "message": "slow down", "data": {"code": "rate_limit_exceeded"}},
    {"code": -32000, "message": "slow down", "data": {"type": "Rate-Limited"}},
    {"code": -32000, "message": "slow down", "data": {"type": "too_many_requests"}},
    {"code": -32000, "message": "slow down", "data": {"status": 429}},
    {"code": -32000, "message": "slow down", "data": {"httpStatus": 429}},
    {"code": 429, "message": "slow down"},
]


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
def test_a_transient_throttle_is_not_a_spent_allowance(error):
    """Two different events with two different remedies. A short-window limit
    clears on a server-side timer; a weekly allowance does not. Only the second
    may reach `QuotaExhaustedError`."""
    assert is_quota_exhausted(error) is False
    assert is_rate_limited(error) is True
    assert classification_of(error) == RATE_LIMITED


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
def test_a_transient_throttle_takes_the_retryable_path_not_the_fatal_one(error):
    """At the transport seam, where the routing actually happens: the raised
    exception must be an ordinary `BrowserError`, which the orchestrator retries
    on its normal failure budget, and never the park."""
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": error}
    codex = build(fake)
    with pytest.raises(CodexProtocolError) as exc:
        codex.submit(RID, PROMPT)
    assert not isinstance(exc.value, QuotaExhaustedError)
    assert isinstance(exc.value, BrowserError)  # routed on the ordinary budget
    # Named as a throttle rather than left to be guessed from the message.
    assert exc.value.transient is True
    assert "not a spent allowance" in str(exc.value)


def test_a_numeric_429_alone_never_parks_the_loop():
    """The single case that caused this, isolated. `QUOTA_STATUS_CODES` used to
    hold 429 and `is_quota_exhausted` returned True on it before it looked at
    any type field at all."""
    for error in ({"code": 429}, {"code": "429"}, {"data": {"status": 429}}):
        assert is_quota_exhausted(error) is False
        assert isinstance(classify(error), CodexProtocolError)
        assert not isinstance(classify(error), QuotaExhaustedError)


def test_spent_wins_when_one_error_carries_both():
    """How a weekly allowance actually reports over HTTP: a 429 status with a
    type that names the allowance. Precedence lives in `classification_of`
    alone, so the digest and the raised exception cannot disagree."""
    error = {"code": 429, "data": {"type": "usage_limit_reached", "status": 429}}
    assert is_quota_exhausted(error) is True
    assert is_rate_limited(error) is False, "spent is tested first and wins"
    assert classification_of(error) == QUOTA_EXHAUSTED
    assert isinstance(classify(error), QuotaExhaustedError)
    assert failure_digest(error)["classification"] == QUOTA_EXHAUSTED


def test_an_unrecognised_failure_is_neither():
    """Degrades to an ordinary failure — noisy, never unsafe. It must not be
    swallowed either: it is still a raised `CodexProtocolError`."""
    error = {"code": -32603, "message": "segmentation fault", "data": {"type": "boom"}}
    assert classification_of(error) == UNCLASSIFIED
    assert isinstance(classify(error), CodexProtocolError)


def test_the_two_vocabularies_do_not_overlap():
    """A code in both lists would make the ORDER of the two checks the thing
    that decides whether the loop parks — which is a coin flip dressed as a
    rule. Same pin as `test_default_patterns_are_not_empty` next door."""
    assert DEFAULT_QUOTA_ERROR_CODES
    assert DEFAULT_RATE_LIMIT_ERROR_CODES
    assert not set(DEFAULT_QUOTA_ERROR_CODES) & set(DEFAULT_RATE_LIMIT_ERROR_CODES)
    # And nothing in the SPENT list describes a throttle, which is the property
    # the disjointness above only half covers.
    assert not [code for code in DEFAULT_QUOTA_ERROR_CODES if "rate" in code]


def test_the_transient_vocabulary_is_overridable_without_touching_code():
    error = {"code": -32000, "data": {"type": "seat_cooling_down"}}
    assert is_rate_limited(error) is False
    assert is_rate_limited(error, rate_limit_codes=("seat_cooling_down",)) is True


def test_an_operator_who_files_a_throttle_as_spent_is_obeyed_and_it_is_stated():
    """The honest limit of this side of the fix. There is no prompt guard here —
    the comparison is exact against a named field, and the list is the
    operator's explicit statement — so naming a throttle code in
    `quota_error_codes` parks the loop. Pinned so the behaviour is a documented
    consequence rather than a surprise."""
    error = {"code": -32000, "data": {"type": "rate_limited"}}
    assert is_quota_exhausted(error) is False, "not by default, which is the fix"
    assert is_quota_exhausted(error, quota_codes=("rate_limited",)) is True


def test_a_misfiled_transient_entry_cannot_suppress_a_real_park():
    """The other direction, which is the one that would be UNSAFE. Spent is
    tested first against `quota_codes`, so listing a spent marker in the
    transient vocabulary cannot downgrade a genuine exhaustion into a retry —
    a config mistake can cost a false park, never a missed one."""
    error = {"code": -32000, "data": {"type": "usage_limit_reached"}}
    assert is_rate_limited(error, rate_limit_codes=("usage_limit_reached",)) is False
    assert (
        classification_of(error, rate_limit_codes=("usage_limit_reached",))
        == QUOTA_EXHAUSTED
    )
    assert isinstance(
        classify(error, rate_limit_codes=("usage_limit_reached",)), QuotaExhaustedError
    )


def test_prose_that_mentions_a_usage_limit_is_not_exhaustion():
    """The counter-test, and the point of the whole module: `quota.py` has to
    scan free-form text and can only mitigate this by requiring a non-zero
    exit. Here the type field is what decides, so a message that merely
    DISCUSSES limits routes as an ordinary failure."""
    error = {
        "code": -32603,
        "message": "the model refused: it kept talking about your usage limit and 429s",
        "data": {"type": "model_refusal"},
    }
    assert is_quota_exhausted(error) is False
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": error}
    codex = build(fake)
    with pytest.raises(CodexProtocolError) as exc:
        codex.submit(RID, PROMPT)
    assert exc.value.error_type == "model_refusal"
    assert isinstance(exc.value, BrowserError)  # routed on the ordinary budget


def test_an_error_notification_is_classified_the_same_way():
    """Failures arrive two ways — as a JSON-RPC error and as an `error`
    notification — and a transport that only understood the first would hang
    through its whole timeout on the second."""
    fake = FakeAppServer()
    codex = build(fake)
    codex.attach()
    thread = codex._ensure_thread()  # reaching in: the fake needs a live thread

    original = fake._on_turn_start

    def erroring(message):
        fake._notify("error", {"code": -32000, "data": {"type": "usage_limit_reached"}})

    fake._on_turn_start = erroring
    with pytest.raises(QuotaExhaustedError):
        codex.submit(RID, PROMPT)
    fake._on_turn_start = original
    assert thread == codex.thread_id


def test_the_quota_vocabulary_is_overridable_without_touching_code():
    error = {"code": -32000, "data": {"type": "seat_allowance_spent"}}
    assert is_quota_exhausted(error) is False
    assert is_quota_exhausted(error, quota_codes=("seat_allowance_spent",)) is True


def test_a_failure_digest_is_bounded_and_carries_no_packet():
    digest = failure_digest(
        {"code": -32000, "message": "x" * 5000, "data": {"type": "boom"}}
    )
    assert digest["error_type"] == "boom"
    assert len(digest["message"]) <= 400
    assert set(digest) == {
        "code",
        "error_type",
        "status",
        "classification",
        "message",
    }
    # The record names the routing decision, so a reader does not have to infer
    # it from whichever exception happened to be raised afterwards.
    assert digest["classification"] == UNCLASSIFIED


@pytest.mark.parametrize(
    "error, expected",
    [
        ({"data": {"type": "usage_limit_reached"}}, QUOTA_EXHAUSTED),
        ({"data": {"status": 429}}, RATE_LIMITED),
        ({"code": -32601, "message": "method not found"}, UNCLASSIFIED),
    ],
)
def test_the_logged_digest_names_which_of_the_three_routes_was_taken(error, expected):
    """Asserted through the CLIENT, not the helper: what a reader will see is
    the digest built inside `_raise`, and it must name the same route the raised
    exception took. `test_a_configured_vocabulary_reaches_the_digest_and_the_routing`
    is the same claim with a non-default vocabulary."""
    logged = []
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": error}
    client = AppServerClient(
        fake,
        timeout_seconds=30.0,
        clock=_fake_clock(),
        log=lambda event, data: logged.append((event, data)),
    )
    codex = CodexAppServerConversation(client)
    with pytest.raises((CodexProtocolError, QuotaExhaustedError)):
        codex.submit(RID, PROMPT)
    failures = [row for row in logged if row[0] == "codex_app_server_failed"]
    assert failures, "every protocol failure leaves a record"
    assert failures[0][1]["classification"] == expected
    assert failures[0][1]["context"] == "turn/start"


def test_a_configured_vocabulary_reaches_the_digest_and_the_routing():
    """The two must move together. A client told that `seat_cooling_down` is a
    throttle has to both route it retryably AND say so in the record."""
    logged = []
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": {"data": {"type": "seat_cooling_down"}}}
    client = AppServerClient(
        fake,
        timeout_seconds=30.0,
        clock=_fake_clock(),
        rate_limit_codes=("seat_cooling_down",),
        log=lambda event, data: logged.append((event, data)),
    )
    codex = CodexAppServerConversation(client)
    with pytest.raises(CodexProtocolError) as exc:
        codex.submit(RID, PROMPT)
    assert exc.value.transient is True
    failures = [row for row in logged if row[0] == "codex_app_server_failed"]
    assert failures[0][1]["classification"] == RATE_LIMITED


def test_every_protocol_failure_is_logged_for_diagnosis():
    """What turns the first real exhaustion into a config edit instead of an
    investigation — so it must fire for UNRECOGNISED failures too."""
    logged = []
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": {"code": -1, "message": "something unfamiliar"}}
    client = AppServerClient(
        fake,
        timeout_seconds=30.0,
        clock=_fake_clock(),
        log=lambda event, data: logged.append((event, data)),
    )
    codex = CodexAppServerConversation(client)
    with pytest.raises(CodexProtocolError):
        codex.submit(RID, PROMPT)
    failures = [row for row in logged if row[0] == "codex_app_server_failed"]
    assert failures and "something unfamiliar" in failures[0][1]["message"]


def test_a_numeric_jsonrpc_code_is_not_a_vocabulary_word():
    """`-32601` is "method not found", not an error type. Treating a numeric
    code as a name would misfile transport faults as whatever the list holds."""
    assert classify({"code": -32601, "message": "method not found"}).__class__ is (
        CodexProtocolError
    )
    assert is_quota_exhausted({"code": -32601, "message": "usage limit"}) is False


# ---- 3. honest idempotency -------------------------------------------------


def test_idempotent_submit_is_explicitly_false():
    """`turn/start` appends its input and then runs. Declaring the subprocess
    adapter's property here would trade a park for silent duplicate turns."""
    assert CodexAppServerConversation.idempotent_submit is False
    assert CodexConversation.idempotent_submit is True  # unchanged next door


def test_a_turn_that_fails_after_appending_leaves_the_prompt_on_the_thread():
    """The failing-mid-append case, demonstrated rather than asserted: the
    server has the prompt, this process has no reply, and a retry would post
    the same prompt a second time. That is why the declaration above is False."""
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": {"code": -32603, "message": "died"}, "append": True}
    codex = build(fake)

    with pytest.raises(CodexProtocolError):
        codex.submit(RID, PROMPT)

    # Appended, and unanswered.
    thread_texts = fake.texts(codex.thread_id)
    assert any(RID in text for text in thread_texts)
    assert codex.has_request(RID) is False
    # `reconcile` sees the marker AND the absence of a reply, and refuses to
    # call it persisted — which routes the round to `submission_ambiguous`
    # instead of to a phantom `awaiting`.
    assert codex.reconcile(RID) is False


def test_reconcile_recovers_a_reply_the_server_kept():
    """The other half of being non-idempotent: because the thread is real,
    an ambiguous submission can be RESOLVED from the server rather than
    guessed at. The subprocess adapter has no equivalent."""
    fake = FakeAppServer()
    codex = build(fake)
    codex.submit(RID, PROMPT)

    # A fresh adapter over the same server, holding no memory of the turn, but
    # pointed at the same thread — the shape a crash between submit and await
    # leaves behind WITHIN one process.
    revived = build(fake)
    revived.attach()
    revived._thread_id = codex.thread_id  # pointed by hand: there is no resume (codex-03)
    assert revived.has_request(RID) is False
    assert revived.reconcile(RID) is True
    assert RID in revived.await_response(RID)


def test_reconcile_answers_false_before_any_thread_exists():
    codex = build(FakeAppServer())
    assert codex.reconcile(RID) is False


def test_reconcile_refuses_to_guess_when_the_readback_is_unreadable():
    """"Not there" and "cannot read the answer" are different, and only one of
    them may authorize a resend."""
    fake = FakeAppServer()
    codex = build(fake)
    codex.submit(RID, PROMPT)
    fake.thread_read_result = {"totally": "unexpected"}
    other = build(fake)
    other.attach()
    other._thread_id = codex.thread_id  # reaching in, deliberately
    with pytest.raises(CodexProtocolError) as exc:
        other.reconcile(RID)
    assert "thread/read" in str(exc.value)


def test_a_captured_request_is_never_re_sent():
    fake = FakeAppServer()
    codex = build(fake)
    codex.submit(RID, PROMPT)
    assert codex.submit(RID, PROMPT) is SubmitResult.ALREADY_PERSISTED
    assert len(fake.turn_threads) == 1


def test_awaiting_an_uncaptured_request_raises_rather_than_hanging():
    codex = build(FakeAppServer())
    with pytest.raises(ResponseTimeoutError):
        codex.await_response(RID)


# ---- 4. the attachment argument --------------------------------------------


def test_submit_accepts_an_attachment_by_name():
    """`orchestrator._step_submitting` passes it BY NAME. A provider without
    the parameter raises TypeError and kills the loop — measured twice on
    2026-08-21."""
    signature = inspect.signature(CodexAppServerConversation.submit)
    assert "attachment" in signature.parameters
    assert signature.parameters["attachment"].default is None


def test_an_attachment_over_the_ceiling_is_refused_by_name(tmp_path):
    """Refusing beats truncating: a diff delivered minus its tail is the
    failure chunking exists to prevent."""
    fake = FakeAppServer()
    codex = build(fake, max_attachment_chars=100)
    with pytest.raises(BrowserError) as exc:
        codex.submit(RID, PROMPT, attachment=attachment(tmp_path, 500))
    assert "app_server_max_attachment_chars" in str(exc.value)
    assert fake.turn_threads == []  # nothing was asked


def test_an_attachment_that_would_need_too_many_parts_is_refused(tmp_path):
    fake = FakeAppServer()
    codex = build(fake, part_chars=1)
    with pytest.raises(BrowserError) as exc:
        codex.submit(RID, PROMPT, attachment=attachment(tmp_path, MAX_PARTS + 1))
    assert f"{MAX_PARTS}-part ceiling" in str(exc.value)


def test_a_failed_part_never_reaches_the_verdict_question(tmp_path):
    """All-or-nothing, held by construction: a failed turn raises out of
    `submit`, so the question is never asked on a partial patch."""
    fake = FakeAppServer()
    fake.fail_next_turn = {"error": {"code": -1, "message": "part died"}}
    codex = build(fake, part_chars=100)
    with pytest.raises(CodexProtocolError):
        codex.submit(RID, PROMPT, attachment=attachment(tmp_path, 450))
    assert not any(RID in text for text in fake.texts(codex.thread_id))


# ---- reply shapes ----------------------------------------------------------


def test_a_server_that_streams_and_a_server_that_answers_synchronously():
    for mode in ("delta", "item", "synchronous"):
        fake = FakeAppServer(mode=mode)
        codex = build(fake)
        assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED
        assert codex.await_response(RID).startswith(f"reviewed {RID}")


def test_the_prompt_is_never_echoed_back_as_the_reply():
    """`item/completed` fires for the input item too. A transport that took the
    first completed item would hand the contract parser its own packet."""
    fake = FakeAppServer(mode="item")
    codex = build(fake)
    codex.submit(RID, PROMPT)
    assert PROMPT not in codex.await_response(RID)


def test_a_turn_with_no_readable_assistant_message_is_a_named_error():
    fake = FakeAppServer(mode="item", reply=lambda items, text: "")
    codex = build(fake)
    with pytest.raises(CodexProtocolError) as exc:
        codex.submit(RID, PROMPT)
    assert "without any readable assistant message" in str(exc.value)


def test_reasoning_text_is_not_mistaken_for_a_verdict():
    """A thinking model puts a reasoning item between the request and the
    answer. `reconcile` requires an AGENT item, not the next text it finds."""
    entries = [
        ThreadEntry(text=f"ask {RID}", is_agent=False, is_user=True),
        ThreadEntry(text="thinking out loud", is_agent=False, is_user=False),
        ThreadEntry(text="the verdict", is_agent=True, is_user=False),
    ]
    fake = FakeAppServer()
    codex = build(fake)
    codex._thread_id = "thread-x"  # reaching in, deliberately
    codex._client.read_thread = lambda thread_id: entries  # reaching in, deliberately
    assert codex.reconcile(RID) is True
    assert codex.await_response(RID) == "the verdict"


# ---- containment and framing -----------------------------------------------


def test_every_approval_the_server_asks_for_is_answered_abort():
    """The app-server containment story, and it is a property of this client's
    replies rather than of a flag whose name nobody verified."""
    fake = FakeAppServer()
    codex = build(fake)
    codex.attach()
    thread = codex._ensure_thread()  # reaching in, deliberately

    def asking(message):
        fake._emit(
            {
                "jsonrpc": "2.0",
                "id": 900,
                "method": "execCommandApproval",
                "params": {"conversationId": thread, "command": ["rm", "-rf", "/"]},
            }
        )
        FakeAppServer._on_turn_start(fake, message)

    fake._on_turn_start = asking
    codex.submit(RID, PROMPT)

    answers = [m for m in fake.sent if m.get("id") == 900]
    assert answers and answers[0]["result"] == {"decision": "abort"}


def test_an_unserviceable_server_request_is_refused_never_ignored():
    """Silence wedges the turn: the server blocks on its own request and the
    round dies at the timeout with nothing saying why."""
    fake = FakeAppServer()
    codex = build(fake)
    codex.attach()
    codex._ensure_thread()  # reaching in, deliberately

    def asking(message):
        fake._emit(
            {"jsonrpc": "2.0", "id": 901, "method": "item/tool/call", "params": {}}
        )
        FakeAppServer._on_turn_start(fake, message)

    fake._on_turn_start = asking
    codex.submit(RID, PROMPT)

    answers = [m for m in fake.sent if m.get("id") == 901]
    assert answers and answers[0]["error"]["code"] == -32000
    assert "grants no approvals" in answers[0]["error"]["message"]


def test_lsp_style_framing_is_a_named_error_not_a_json_crash():
    """The one structural choice the committed reference cannot settle: it
    declares message TYPES, not delimiters."""
    fake = FakeAppServer()
    fake.outbox.append("Content-Length: 42")
    client = AppServerClient(fake, timeout_seconds=5.0, clock=_fake_clock())
    with pytest.raises(CodexProtocolError) as exc:
        client.start()
    assert "newline-delimited JSON" in str(exc.value)


def test_a_banner_line_on_stdout_does_not_fail_a_review():
    fake = FakeAppServer()
    fake.outbox.append("codex app-server 0.147.0 listening")
    codex = build(fake)
    assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED


def test_a_server_that_dies_mid_turn_is_a_session_loss():
    from autoloop.errors import SessionLostError

    class Dying(FakeAppServer):
        def read_line(self, timeout):
            if not self.outbox:
                raise EOFError("gone")
            return self.outbox.pop(0)

    fake = Dying()
    fake._on_turn_start = lambda message: None
    codex = build(fake)
    with pytest.raises(SessionLostError):
        codex.submit(RID, PROMPT)


def test_a_silent_server_times_out_and_says_so():
    fake = FakeAppServer()
    fake._on_turn_start = lambda message: None
    client = AppServerClient(fake, timeout_seconds=2.0, clock=_fake_clock())
    codex = CodexAppServerConversation(client)
    with pytest.raises(ResponseTimeoutError):
        codex.submit(RID, PROMPT)
    # And the abandoned turn was interrupted rather than left generating.
    assert any(m.get("method") == "turn/interrupt" for m in fake.sent)


def test_the_real_transport_never_uses_a_shell_and_confines_the_working_dir(tmp_path):
    server = SubprocessAppServer(command=("codex", "app-server"), cwd=tmp_path)
    assert server.argv_preview == ("codex", "app-server")
    # Nothing model-authored can reach argv here at all: prompts travel as JSON
    # on stdin, which is why the 700 KB argv ceiling does not exist for this
    # transport.
    assert all("request" not in part for part in server.argv_preview)


def test_a_missing_binary_is_a_clear_actionable_error(tmp_path):
    server = SubprocessAppServer(
        command=("definitely-not-a-real-binary-xyz",), cwd=tmp_path
    )
    with pytest.raises(BrowserError) as exc:
        server.start()
    assert "codex login" in str(exc.value)


# ---- the protocol pin ------------------------------------------------------


def _union_methods(union: str) -> set[str]:
    text = REFERENCE.read_text(encoding="utf-8")
    marker = f"export type {union} = "
    start = text.index(marker)
    end = text.index(";", start)
    return set(re.findall(r'"method"\s*:\s*"([^"]+)"', text[start:end]))


def _declared_methods() -> set[str]:
    """Every string the committed reference declares as a JSON-RPC `method`,
    in any union. The question "is this a real method" asked of the DECLARATIONS
    rather than of the file's raw text — the reference opens with a header
    comment naming the four invented methods while explaining that they do not
    exist, and a substring search cannot tell that apart from a declaration."""
    text = REFERENCE.read_text(encoding="utf-8")
    return set(re.findall(r'"method"\s*:\s*"([^"]+)"', text))


def test_every_method_this_transport_sends_is_in_the_committed_protocol():
    """The instrument that makes an experimental protocol safe to depend on: a
    codex-cli upgrade that renames a method breaks THIS, not a live review.
    Round 2 shipped four invented names; this is what would have caught it."""
    declared = _union_methods("ClientRequest")
    assert set(wire.CLIENT_REQUEST_METHODS) <= declared
    # And the names round 2 invented are declared by nothing in the reference,
    # so the check has teeth. A superset of `declared` — the file spells a
    # `"method"` literal on five lines, the three unions among them — so this
    # asks the widest "is it a real method anywhere" the reference can answer.
    real = _declared_methods()
    for invented in (
        "newConversation",
        "addConversationListener",
        "sendUserMessage",
        "interruptConversation",
    ):
        assert invented not in real


def test_every_notification_this_transport_handles_is_in_the_committed_protocol():
    declared = _union_methods("ServerNotification")
    assert set(wire.SERVER_NOTIFICATION_METHODS) <= declared


def test_the_approvals_answered_abort_are_real_server_requests():
    declared = _union_methods("ServerRequest")
    assert set(wire.DENIABLE_SERVER_REQUEST_METHODS) <= declared
    text = REFERENCE.read_text(encoding="utf-8")
    # `{decision: ReviewDecision}` with `"abort"` among its variants — which is
    # what makes `abort` an answer rather than a guess.
    assert "export type ApplyPatchApprovalResponse = { decision: ReviewDecision, }" in text
    assert '"abort"' in text
    assert f'"type": "{wire.INPUT_TEXT_TYPE}"' in text


def test_the_unpinned_spellings_are_declared_unpinned_and_still_are():
    """The honesty half of `wire.py`. The v2 declaration bodies are referenced
    by the committed reference but not concatenated into it, so these spellings
    cannot be checked against ground truth. If a future regeneration DOES
    include them, this fails — and they become pinnable."""
    text = REFERENCE.read_text(encoding="utf-8")
    assert wire.UNPINNED_FIELD_SPELLINGS
    for name in wire.ABSENT_V2_DECLARATIONS:
        assert f"export type {name} =" not in text
        assert f'from "./v2/{name}"' in text


def test_thread_ids_are_read_tolerantly_but_never_invented():
    for payload in (
        {"threadId": "t1"},
        {"thread_id": "t1"},
        {"conversationId": "t1"},
        {"thread": {"threadId": "t1"}},
        {"thread": {"id": "t1"}},
    ):
        assert wire.find_thread_id(payload) == "t1"
    assert wire.find_thread_id({"nothing": "useful"}) is None
    assert wire.find_thread_id("not a mapping") is None


def test_a_thread_start_with_no_readable_id_is_a_named_error():
    fake = FakeAppServer()
    fake._on_thread_start = lambda message: fake._respond(message, {"huh": "?"})
    codex = build(fake)
    with pytest.raises(CodexProtocolError) as exc:
        codex.submit(RID, PROMPT)
    assert "no readable thread id" in str(exc.value)


# ---- 5. nothing was taken away ---------------------------------------------


def test_both_codex_providers_are_selectable_and_the_old_one_is_unchanged(tmp_path):
    # `browser_chatgpt` was in this set until brw-16 (2026-08-25) unregistered
    # it. Nothing about either codex seat changed with it.
    assert {"codex_cli", "codex_app_server"} <= set(available_providers())
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path,
        conversation=ConversationConfig(provider="codex_cli"),
    )
    subprocess_provider = create_conversation("codex_cli", config)
    assert isinstance(subprocess_provider, CodexConversation)
    subprocess_provider.attach()  # still an honest no-op
    subprocess_provider.close()

    app_server = create_conversation("codex_app_server", config)
    assert isinstance(app_server, CodexAppServerConversation)
    # Constructing must not launch anything — only `attach()` does.
    assert app_server.thread_id == ""


def test_either_codex_transport_can_be_named_as_the_fallback(tmp_path):
    """`fallback_provider` must still be able to name any registered seat.

    Both remaining seats are CONSTRUCTED here, which is stronger than the
    registration-only check the browser seat used to get (building it imported
    playwright, which this hermetic suite does not have). Since brw-16 there is
    no third seat to check either way — and note that both of these draw on ONE
    allowance, so this pairing proves the mechanism, not that it buys anything.
    """
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path,
        conversation=ConversationConfig(
            provider="codex_app_server", fallback_provider="codex_cli"
        ),
    )
    for pairing in (
        (config.conversation.provider, config.conversation.fallback_provider),
        ("codex_cli", "codex_app_server"),
    ):
        for name in pairing:
            assert create_conversation(name, config) is not None


@pytest.mark.parametrize("module", ["orchestrator.py", "policy.py", "state.py"])
def test_the_orchestrator_policy_and_state_modules_stay_provider_agnostic(module):
    """The task's own bound: this ADDS a provider through the registry and the
    capability probes that already exist. A name from this change appearing in
    one of these three files means the seam was breached."""
    source = (REPO_ROOT / "autoloop" / module).read_text(encoding="utf-8")
    for name in (
        "codex_app_server",
        "CodexAppServerConversation",
        "app_server",
        "thread/start",
        "turn/start",
    ):
        assert name not in source


def test_the_new_config_keys_load_and_are_validated(tmp_path):
    config_file = tmp_path / "autoloop.toml"
    config_file.write_text(
        "\n".join(
            [
                "[browser]",
                'conversation_url = "https://chatgpt.com/c/x"',
                "[conversation]",
                'provider = "codex_app_server"',
                "[codex]",
                'app_server_command = ["codex", "app-server", "--experimental"]',
                'quota_error_codes = ["seat_allowance_spent"]',
                "app_server_part_chars = 1234",
                "[paths]",
                f'state_dir = "{tmp_path / ".al"}"',
                f'workers_root = "{tmp_path / "workers"}"',
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.codex.app_server_command == ("codex", "app-server", "--experimental")
    assert config.codex.quota_error_codes == ("seat_allowance_spent",)
    assert config.codex.app_server_part_chars == 1234
    # And the untouched half of the section still has its defaults.
    assert config.codex.command == ("codex", "exec")


@pytest.mark.parametrize(
    "line",
    [
        'app_server_command = "codex app-server"',
        "app_server_command = []",
        "quota_error_codes = [1, 2]",
        "rate_limit_error_codes = [1, 2]",
        'rate_limit_error_codes = "throttled"',
    ],
)
def test_a_malformed_new_config_key_is_refused_at_load(tmp_path, line):
    from autoloop.errors import ConfigError

    config_file = tmp_path / "autoloop.toml"
    config_file.write_text(
        "\n".join(
            [
                "[browser]",
                'conversation_url = "https://chatgpt.com/c/x"',
                "[codex]",
                line,
                "[paths]",
                f'state_dir = "{tmp_path / ".al"}"',
                f'workers_root = "{tmp_path / "workers"}"',
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_config(config_file)


def test_the_default_quota_vocabulary_is_not_empty():
    assert DEFAULT_QUOTA_ERROR_CODES


def test_the_transient_vocabulary_loads_from_config_and_reaches_the_client(tmp_path):
    """Config → factory → client, in one line each, because a setting that
    loads and is never passed on is indistinguishable from a setting that does
    not exist — which is the shape of the logger bug this task is about."""
    config_file = tmp_path / "autoloop.toml"
    config_file.write_text(
        "\n".join(
            [
                "[browser]",
                'conversation_url = "https://chatgpt.com/c/x"',
                "[conversation]",
                'provider = "codex_app_server"',
                "[codex]",
                'quota_error_codes = ["seat_allowance_spent"]',
                'rate_limit_error_codes = ["seat_cooling_down"]',
                "[paths]",
                f'state_dir = "{tmp_path / ".al"}"',
                f'workers_root = "{tmp_path / "workers"}"',
            ]
        ),
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert config.codex.rate_limit_error_codes == ("seat_cooling_down",)

    conversation = create_conversation("codex_app_server", config)
    assert conversation._client._quota_codes == ("seat_allowance_spent",)
    assert conversation._client._rate_limit_codes == ("seat_cooling_down",)

    # An unset section still means the built-in lists, not empty ones: empty
    # would classify every failure as unrecognised, silently.
    default = create_conversation(
        "codex_app_server",
        AutoloopConfig(
            browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
            policy=PolicyConfig(),
            state_dir=tmp_path / ".al2",
            conversation=ConversationConfig(provider="codex_app_server"),
        ),
    )
    assert default._client._quota_codes == DEFAULT_QUOTA_ERROR_CODES
    assert default._client._rate_limit_codes == DEFAULT_RATE_LIMIT_ERROR_CODES


def test_a_vocabulary_of_blanks_falls_back_instead_of_recognising_nothing():
    """`[""]` is a NON-EMPTY list of nothing. `tuple(codes) or DEFAULT` passes it
    through, and the client then recognises no error at all — a check switched
    off by config, which is the failure class this task exists to remove. The
    fallback tests the CONTENTS. (Equality matching means a blank could never
    over-classify, unlike the substring hole `quota._usable` closes; the harm
    here is the opposite one, a real exhaustion going unrecognised.)"""
    assert usable_codes([""], DEFAULT_QUOTA_ERROR_CODES) == DEFAULT_QUOTA_ERROR_CODES
    assert usable_codes(["  ", None, 7], DEFAULT_QUOTA_ERROR_CODES) == (
        DEFAULT_QUOTA_ERROR_CODES
    )
    assert usable_codes(None, DEFAULT_QUOTA_ERROR_CODES) == DEFAULT_QUOTA_ERROR_CODES
    # A real entry beside the junk is kept, and only it.
    assert usable_codes(["", " seat_spent "], DEFAULT_QUOTA_ERROR_CODES) == (
        "seat_spent",
    )

    client = AppServerClient(FakeAppServer(), quota_codes=("",), rate_limit_codes=("",))
    assert client._quota_codes == DEFAULT_QUOTA_ERROR_CODES
    assert client._rate_limit_codes == DEFAULT_RATE_LIMIT_ERROR_CODES
