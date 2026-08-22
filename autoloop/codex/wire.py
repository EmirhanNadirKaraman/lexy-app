"""Method names and field spellings for the local Codex app-server protocol.

The module is split into two halves and **the split is the whole point**. Round
2 of this task guessed four method names that appear NOWHERE in the committed
reference (they are named in `test_codex_app_server.py`, which asserts the
reference declares no such methods — this module deliberately does not spell
them, so that a grep of `autoloop/codex/` for an invented name comes back
empty). So every string this transport puts on the wire lives here, next to a
statement of whether it can be checked against ground truth or not.

**PINNED.** Every constant in the first half appears verbatim in
`docs/codex-app-server-protocol.generated.ts` — the authoritative binding set
emitted by `codex app-server generate-ts --experimental` against codex-cli
0.147.0 and committed as a read-only reference. `test_codex_app_server.py`
parses the `ClientRequest` / `ServerNotification` / `ServerRequest` unions out
of that file and fails if one of these strings is not in them. Regenerating the
reference against a newer codex-cli therefore breaks a TEST rather than a live
review.

**UNPINNED.** The second half is the part the reference cannot settle. The
generated file concatenates the 97 *top-level* declaration files; the `v2/`
declarations — `ThreadStartParams`, `TurnStartParams`, `ThreadReadParams`,
`ItemCompletedNotification`, `TurnCompletedNotification`, `ErrorNotification`
— are referenced by import path (362 `from "./v2/…"` lines) but their bodies
are NOT in it, so `grep 'export type ThreadStartParams'` comes back empty.
Which means the spelling of the key that carries a thread id into `thread/start`
is not knowable from this repository. Rather than hard-code one guess and call
it protocol, every such spelling is listed below as a TUPLE of candidates and
read tolerantly, and every reader returns `None` — never a plausible wrong
answer — when it recognises nothing. The caller turns that into a named error
that says which shape it could not read.

Two spellings ARE pinned even though they describe params, because their types
are top-level and therefore present in the reference:

* the input content item `{"type": "input_text", "text": …}` — `ContentItem`
  and `AgentMessageInputContent` both carry that variant;
* the approval answer `{"decision": "abort"}` — `ApplyPatchApprovalResponse` /
  `ExecCommandApprovalResponse` are `{decision: ReviewDecision}`, and
  `ReviewDecision` includes `"abort"`.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# ---------------------------------------------------------------------------
# PINNED — checked against docs/codex-app-server-protocol.generated.ts
# ---------------------------------------------------------------------------

#: Handshake. `initialize` is a request; `initialized` is the one member of
#: `ClientNotification`.
METHOD_INITIALIZE = "initialize"
NOTIFICATION_INITIALIZED = "initialized"

METHOD_THREAD_START = "thread/start"
METHOD_THREAD_READ = "thread/read"
METHOD_TURN_START = "turn/start"
METHOD_TURN_INTERRUPT = "turn/interrupt"

#: Everything this transport may send as a JSON-RPC *request*. Deliberately
#: short: `thread/resume` (restart durability) and `thread/fork` /
#: `thread/rollback` are real methods this transport does NOT call — see
#: `app_server.py`'s module docstring and codex-03.
CLIENT_REQUEST_METHODS: tuple[str, ...] = (
    METHOD_INITIALIZE,
    METHOD_THREAD_START,
    METHOD_THREAD_READ,
    METHOD_TURN_START,
    METHOD_TURN_INTERRUPT,
)

NOTIFICATION_ERROR = "error"
NOTIFICATION_THREAD_STARTED = "thread/started"
NOTIFICATION_TURN_STARTED = "turn/started"
NOTIFICATION_TURN_COMPLETED = "turn/completed"
NOTIFICATION_ITEM_COMPLETED = "item/completed"
NOTIFICATION_ITEM_AGENT_MESSAGE_DELTA = "item/agentMessage/delta"

#: Server notifications this transport acts on. Anything else is ignored by
#: design — a transport that must understand every notification the server can
#: emit breaks on the next codex-cli release.
SERVER_NOTIFICATION_METHODS: tuple[str, ...] = (
    NOTIFICATION_ERROR,
    NOTIFICATION_THREAD_STARTED,
    NOTIFICATION_TURN_STARTED,
    NOTIFICATION_TURN_COMPLETED,
    NOTIFICATION_ITEM_COMPLETED,
    NOTIFICATION_ITEM_AGENT_MESSAGE_DELTA,
)

#: Server->client requests whose RESPONSE shape is in the committed reference,
#: so they can be answered rather than merely refused. Both are approval asks,
#: and this client answers both `abort`: the reviewer is handed a
#: self-contained prompt and has no business running a command or writing a
#: patch. Everything else with a `method` AND an `id` gets a JSON-RPC error —
#: never silence, which would leave the server waiting and wedge the turn.
SERVER_REQUEST_APPLY_PATCH_APPROVAL = "applyPatchApproval"
SERVER_REQUEST_EXEC_COMMAND_APPROVAL = "execCommandApproval"
DENIABLE_SERVER_REQUEST_METHODS: tuple[str, ...] = (
    SERVER_REQUEST_APPLY_PATCH_APPROVAL,
    SERVER_REQUEST_EXEC_COMMAND_APPROVAL,
)

#: `ReviewDecision` — the refusal this client answers every approval with.
REVIEW_DECISION_ABORT = "abort"
#: `ContentItem` / `AgentMessageInputContent` — how user text is carried.
INPUT_TEXT_TYPE = "input_text"

# ---------------------------------------------------------------------------
# UNPINNED — the v2 param/notification bodies are absent from the reference
# ---------------------------------------------------------------------------

#: Where a thread id may be spelled, on the way out and on the way back.
#: `conversationId` is not a guess: the reference's own top-level params
#: (`ApplyPatchApprovalParams`, `ExecCommandApprovalParams`) still carry a
#: `ThreadId` under that name, so both spellings are live in 0.147.0.
THREAD_ID_KEYS: tuple[str, ...] = (
    "threadId",
    "thread_id",
    "conversationId",
    "conversation_id",
)
#: The key `thread/start`'s result may nest its thread under.
THREAD_CONTAINER_KEYS: tuple[str, ...] = ("thread", "conversation")
#: The key carrying a turn's user input on `turn/start`.
TURN_INPUT_KEY = "input"
#: Where `thread/start` is told which directory the thread runs in.
THREAD_CWD_KEY = "cwd"
#: Where `thread/read` puts the thread's contents.
THREAD_ITEMS_KEYS: tuple[str, ...] = ("items", "turns", "messages", "events")
#: Where a notification wraps the single item it is about.
ITEM_CONTAINER_KEYS: tuple[str, ...] = ("item", "turn", "message")
#: Where text lives on an item, a delta or a content entry.
TEXT_KEYS: tuple[str, ...] = ("text", "delta", "content", "value")
#: Where an item says who produced it.
ROLE_KEYS: tuple[str, ...] = ("role", "author", "sender", "type", "itemType")

#: The single enumeration of everything above, so a reader can see the size of
#: the unverified surface without grepping. Asserted non-empty by the test
#: suite, alongside the assertion that the v2 bodies really are absent from the
#: committed reference — if a future regeneration includes them, that test
#: fails and these become pinnable.
UNPINNED_FIELD_SPELLINGS: dict[str, tuple[str, ...]] = {
    "thread_id": THREAD_ID_KEYS,
    "thread_container": THREAD_CONTAINER_KEYS,
    "turn_input": (TURN_INPUT_KEY,),
    "thread_cwd": (THREAD_CWD_KEY,),
    "thread_items": THREAD_ITEMS_KEYS,
    "item_container": ITEM_CONTAINER_KEYS,
    "text": TEXT_KEYS,
    "role": ROLE_KEYS,
}

#: The v2 declarations whose absence from the committed reference is the reason
#: the spellings above are tolerant rather than pinned.
ABSENT_V2_DECLARATIONS: tuple[str, ...] = (
    "ThreadStartParams",
    "TurnStartParams",
    "ThreadReadParams",
    "ItemCompletedNotification",
    "TurnCompletedNotification",
    "ErrorNotification",
)

#: Role/type values that mean "this item is the prompt we just sent", so its
#: text must never be mistaken for the reviewer's answer.
_USER_MARKERS = ("user", "input", "prompt")
#: Role/type values that mean "this is the reviewer talking".
_AGENT_MARKERS = ("assistant", "agent")


def first_present(payload: Any, keys: Sequence[str]) -> Any:
    """The first of `keys` present on `payload`, or None. Mapping-only: a
    non-mapping is not an error here, it is simply an absence."""
    if not isinstance(payload, Mapping):
        return None
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def find_thread_id(payload: Any) -> str | None:
    """A thread id out of a result or a notification, or None.

    Looks at the top level first, then inside one level of `thread` /
    `conversation` wrapper. Deliberately not a recursive search: a deep walk
    would happily return the *parent* thread id out of a nested
    `thread_spawn` record and call it ours.
    """
    direct = first_present(payload, THREAD_ID_KEYS)
    if isinstance(direct, str) and direct:
        return direct
    nested = first_present(payload, THREAD_CONTAINER_KEYS)
    inner = first_present(nested, THREAD_ID_KEYS)
    if isinstance(inner, str) and inner:
        return inner
    if isinstance(nested, Mapping) and isinstance(nested.get("id"), str) and nested["id"]:
        return str(nested["id"])
    return None


def item_text(value: Any) -> str:
    """Every string this item carries under a recognised text key, joined.

    One level of structure only — a string, a `{text: …}`, or a list of
    those — because the shapes that matter (`ContentItem`,
    `AgentMessageInputContent`) are exactly that shallow, and an unbounded
    recursive scrape would pull reasoning summaries and tool arguments into a
    reviewer's verdict.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "".join(item_text(entry) for entry in value)
    if isinstance(value, Mapping):
        found = first_present(value, TEXT_KEYS)
        if found is not None and found is not value:
            return item_text(found)
    return ""


def _markers(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    parts = []
    for key in ROLE_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value.lower())
    return " ".join(parts)


def is_user_item(item: Any) -> bool:
    """True when this item is (or contains) the text this client sent."""
    return any(marker in _markers(item) for marker in _USER_MARKERS)


def is_agent_item(item: Any) -> bool:
    """True when this item is the reviewer's own message.

    Conservative on purpose: an item this function cannot classify contributes
    nothing, and a turn that produced no classifiable agent text raises rather
    than returning whatever text happened to be lying around.
    """
    markers = _markers(item)
    if any(marker in markers for marker in _USER_MARKERS):
        return False
    return any(marker in markers for marker in _AGENT_MARKERS)


def unwrap_item(payload: Any) -> Any:
    """The item a notification is about — unwrapped if it is nested, the
    payload itself if the notification carries its fields inline."""
    inner = first_present(payload, ITEM_CONTAINER_KEYS)
    return inner if inner is not None else payload


def thread_items(result: Any) -> list[Any] | None:
    """`thread/read`'s items, or None when the shape is unrecognised.

    None and `[]` are different answers and both matter: an empty list is a
    thread with nothing on it, while None means this client cannot read the
    server's reply at all — which callers must surface, never treat as "not
    there".
    """
    found = first_present(result, THREAD_ITEMS_KEYS)
    if isinstance(found, list):
        return found
    nested = first_present(result, THREAD_CONTAINER_KEYS)
    found = first_present(nested, THREAD_ITEMS_KEYS)
    if isinstance(found, list):
        return found
    return None
