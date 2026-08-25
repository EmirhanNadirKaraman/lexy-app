"""Transport recovery: submission classification and the one same-chat resend.

The invariant every test here defends is the same one: **what the loop is
allowed to do automatically depends on what it can prove, not on what it is
guessing.** A send whose acceptance is merely unobserved licenses nothing (the
backend may have taken it, so resending risks a double post). A send the browser
itself reports as failed licenses exactly one resend, after reconciliation
confirms the request is absent. A second such failure licenses NOTHING: the loop
parks (`send_rejected_twice`).

brw-15 (2026-08-25) is why that last sentence is short. A second confirmed
rejection, a structurally unusable conversation and a three-strike silent
conversation each used to authorize one bounded conversation ROTATION — open a
fresh chat in `browser.project_url`, post the request into it, prove it landed,
rebind. That machinery is gone from `orchestrator.py`, so every test that
asserted a rotation happened went with it; what remains here is the
classification, the resend bound, the per-request conversation binding, and the
parks. The tests that pin "this fault must NOT rotate" are kept and still pass
for the stronger reason that nothing can.

`RotatingFakeClient` models the shape of the real transport rather than its
mechanics: a per-conversation server truth, a page URL that only becomes a real
`/c/<id>` after the first turn lands, and a `retarget` that decides which
conversation every subsequent call reads. It keeps its rotation-shaped
affordances (`retarget`, `current_url`, `placeholder_until`) because
`test_conversation_retirement.py` imports it and because they model a real
browser, not because anything under test still rotates.
"""

import json
import subprocess

import pytest

from autoloop.browser.chatgpt import SubmitResult
from autoloop.browser.observation import (
    SendObservation,
    SendOutcome,
    classify_submission,
    is_send_path,
    scrub_path,
)
from autoloop.cli import _authorize_resubmit
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.config_writer import (
    assert_untracked,
    replace_conversation_url,
    update_conversation_url,
)
from autoloop.errors import (
    BrowserError,
    ConfigError,
    ConversationSearchInconclusive,
    ConversationUnusableError,
    LoginExpiredError,
    ResponseTimeoutError,
    SessionLostError,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

from test_orchestrator import FakeExecutor, FakeGit, block  # noqa: E402 - see conftest sys.path

PROJECT_URL = "https://chatgpt.com/g/g-p-proj123-demo/project"
CONV_URL = "https://chatgpt.com/g/g-p-proj123-demo/c/original-chat"
NEW_CONV_URL = "https://chatgpt.com/g/g-p-proj123-demo/c/replacement-chat"
#: What the address bar ACTUALLY shows between "send clicked in the project" and
#: "the chat has a durable address": a placeholder under no project at all.
#: Observed 2026-08-16, where judging project membership against it parked the
#: loop LOOP-FATAL on a rotation that had worked.
PLACEHOLDER_URL = "https://chatgpt.com/c/WEB:0d1f4a6c-2b77-4d51-9f0e-6c2c9a7f1a33"
#: A chat that really is outside the project — a stray navigation, a redirect to
#: the plain composer. Refusing this is the rule the priming fix must not relax.
OUTSIDE_PROJECT_URL = "https://chatgpt.com/c/some-other-chat"


def stop_block(reason="all done"):
    return block({"version": 3, "decision": "stop", "reason": reason})


class RotatingFakeClient:
    """Conversation double with per-conversation server truth and rotation.

    `submit_results` is a queue consumed one entry per `submit()`; the tail
    value repeats once exhausted, so a test only scripts the interesting turns.
    A CONFIRMED submit persists the request id **in whichever conversation the
    client is currently pointed at** — which is what makes "reconcile the old
    chat for a request that landed in the new one" observable rather than
    theoretical.
    """

    def __init__(
        self,
        conversation_url=CONV_URL,
        responses=(),
        submit_results=(SubmitResult.CONFIRMED,),
        send_outcomes=None,
        attach_errors=(),
        new_url=NEW_CONV_URL,
        no_response_results=(True,),
    ):
        self.conversation_url = conversation_url
        self.responses = list(responses)
        self.submit_results = list(submit_results)
        self.send_outcomes = list(send_outcomes or [])
        self.attach_errors = list(attach_errors)
        self._new_url = new_url
        #: url -> set of request ids the server holds for that conversation.
        self.persisted: dict[str, set[str]] = {}
        self.page_url = conversation_url
        self.submitted: list[tuple[str, str, str]] = []  # (url, request_id, prompt)
        self.reconciled: list[tuple[str, str]] = []  # (url, request_id)
        self.awaited: list[tuple[str, str]] = []
        self.retargets: list[str] = []
        self.attach_calls = 0
        self.closed = False
        self.send_attempted = False
        self.send_outcome = SendOutcome.UNKNOWN
        self.send_observations: list[SendObservation] = []
        # Queue consumed one entry per `reconcile_no_response()` call (tail
        # repeats, same convention as `submit_results`). True = "still no
        # assistant turn for this request" (a rotation candidate); False =
        # "a reply appeared" (cancels it).
        self.no_response_results = list(no_response_results)
        self.no_response_calls: list[tuple[str, str]] = []  # (url, request_id)
        self.find_calls: list[tuple[str, str]] = []
        self._stranded_at = None
        #: The pre-persistence address the SPA shows before a brand-new chat has
        #: a durable one, and how many reads keep showing it (None = forever).
        #: See `placeholder_until`.
        self._placeholder = None
        self._placeholder_reads = 0
        self.find_finds_nothing = False
        #: (url, request_id) pairs the SERVER holds but a WINDOW READ misses —
        #: the 2026-08-05 bug in one attribute. ChatGPT mounts a window of a
        #: conversation, not its history, so `has_request`/`reconcile` (window
        #: reads) go blind to these while `find_conversation_with` (which
        #: mounts the tail) still sees them. Without this the fake has a single
        #: truth and every "the search rescued it" test passes on reconcile.
        self.unmounted: set[tuple[str, str]] = set()
        #: Raised by `find_conversation_with` — the search refusing to conclude.
        self.find_error = None

    # -- test helpers ------------------------------------------------------
    def seed(self, url, request_id):
        self.persisted.setdefault(url, set()).add(request_id)

    def hide_from_the_window(self, url, request_id):
        """Persist a request that a reload will NOT show: it is in the chat,
        below the mounted window, exactly like the turn a human found by
        pressing End and scrolling six times."""
        self.seed(url, request_id)
        self.unmounted.add((url, request_id))

    def strand_the_address_bar(self, project_url):
        """Model the real failure: the chat is created and holds the request,
        but the SPA leaves the URL on the project page for good."""
        self._stranded_at = project_url

    def placeholder_until(self, reads=None, placeholder=PLACEHOLDER_URL):
        """Show the pre-persistence placeholder for the first `reads` address
        reads (None = never resolves), then the real one.

        The ordering the 2026-08-16 park was blind to: a chat opened from a
        project page has no durable URL until its first message lands, and the
        address until then is `https://chatgpt.com/c/WEB:<uuid>` — NOT under the
        project prefix, so it fails the membership check exactly like a foreign
        chat. It is not the project page either, which is why "the address
        changed" was never evidence the chat existed."""
        self._placeholder = placeholder
        self._placeholder_reads = reads

    def find_conversation_with(self, request_id, project_url, limit=6):
        """Find the chat by CONTENT, as the real client does — including the
        turns a window read never mounted, which is the whole reason it is a
        better witness than `reconcile`."""
        self.find_calls.append((request_id, project_url))
        if self.find_error is not None:
            raise self.find_error
        if self.find_finds_nothing:
            return None
        for url, ids in self.persisted.items():
            if request_id in ids and url != project_url:
                return url
        return None

    # -- LLMConversation surface -------------------------------------------
    def attach(self):
        self.attach_calls += 1
        if self.attach_errors:
            raise self.attach_errors.pop(0)

    def retarget(self, url):
        self.retargets.append(url)
        self.conversation_url = url

    def current_url(self):
        # When stranded, the address bar stays on whatever it was before the
        # submit — the project page — no matter how long the loop polls.
        if self._stranded_at is not None:
            return self._stranded_at
        if self._placeholder is not None and self._placeholder_reads != 0:
            if self._placeholder_reads is not None:
                self._placeholder_reads -= 1
            return self._placeholder
        return self.page_url

    def has_request(self, request_id):
        if (self.conversation_url, request_id) in self.unmounted:
            return False
        return request_id in self.persisted.get(self.conversation_url, set())

    def reconcile(self, request_id):
        self.reconciled.append((self.conversation_url, request_id))
        return self.has_request(request_id)

    def reconcile_no_response(self, request_id):
        self.no_response_calls.append((self.conversation_url, request_id))
        result = (
            self.no_response_results.pop(0)
            if len(self.no_response_results) > 1
            else self.no_response_results[0]
        )
        return result

    def submit(self, request_id, prompt):
        result = (
            self.submit_results.pop(0) if len(self.submit_results) > 1 else self.submit_results[0]
        )
        self.send_outcome = (
            self.send_outcomes.pop(0)
            if len(self.send_outcomes) > 1
            else (self.send_outcomes[0] if self.send_outcomes else _outcome_for(result))
        )
        self.send_attempted = True
        self.submitted.append((self.conversation_url, request_id, prompt))
        if result is SubmitResult.CONFIRMED:
            # A chat inside a project has no /c/<id> until its first turn; the
            # server mints one on submit. That ordering is why rotation must
            # send before it can capture and verify a URL.
            if self.conversation_url == PROJECT_URL:
                self.page_url = self._new_url
                self.seed(self._new_url, request_id)
            else:
                self.page_url = self.conversation_url
                self.seed(self.conversation_url, request_id)
        return result

    def await_response(self, request_id):
        self.awaited.append((self.conversation_url, request_id))
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        self.closed = True


def _outcome_for(result):
    if result is SubmitResult.CONFIRMED:
        return SendOutcome.ACCEPTED
    if result is SubmitResult.REJECTED:
        return SendOutcome.REJECTED
    return SendOutcome.UNKNOWN


def build(
    tmp_path,
    client,
    *,
    state=None,
    policy=None,
    project_url=PROJECT_URL,
    config_path=None,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    git = FakeGit(repo_root)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL, project_url=project_url),
        policy=policy or PolicyConfig(),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    if state is None:
        state = LoopState.new(CONV_URL)
        state.outbox = "kickoff report"
    store.save(state)
    registry = TaskRegistry()
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
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
        client_factory=lambda: client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        config_path=config_path,
    )
    return orch, store, config


def pending(request_id="alr-test-0001", url=CONV_URL, **overrides):
    fields = dict(
        request_id=request_id,
        payload="body",
        prompt=f"[autoloop request {request_id} | iteration 1]\n\nbody",
        conversation_url=url,
        conversation_epoch=0,
    )
    fields.update(overrides)
    req = PendingRequest(**fields)
    import hashlib

    req.prompt_sha256 = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()
    return req


def transcript_entries(config, entry_type=None):
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if entry_type is None or r.get("type") == entry_type]


# ---- 1. a persisted send is never resent ------------------------------------
#
# `Orchestrator._url_in_project` and the nine tests that pinned its
# segment-boundary rules were removed with the rotation machinery (brw-15): the
# question "is this replacement chat inside the configured project" only ever
# arose while binding a chat rotation had just created. The equivalent rule that
# is still live — "are these two URLs the same chat" — is `_same_conversation`,
# exercised by `test_the_resolution_survives_a_project_relative_search_result`
# below.


def test_persisted_send_never_resends(tmp_path):
    """Reconciliation finds the request already there: nothing may be sent."""
    client = RotatingFakeClient(responses=[stop_block()])
    client.seed(CONV_URL, "alr-test-0001")
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert client.submitted == []  # nothing left the browser
    assert orch.state.phase == Phase.AWAITING.value
    assert orch.state.rotations == 0
    assert transcript_entries(config, "conversation_rotated") == []


# ---- 2/3. a disproven send earns exactly one same-chat resend ---------------


def test_rejected_send_reconciles_absent_then_resends_in_the_same_chat(tmp_path):
    """REJECTED is not itself a licence: the resend follows the reconcile."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)  # submitting -> REJECTED
    assert orch.state.phase == Phase.SUBMISSION_REJECTED.value
    assert orch.state.pending_request.last_send_outcome == SendOutcome.REJECTED.value

    orch.run(max_steps=1)  # reconcile says absent -> authorize one resend
    assert (CONV_URL, "alr-test-0001") in client.reconciled
    assert orch.state.phase == Phase.SUBMITTING.value
    assert orch.state.pending_request.resends_used == 1
    assert orch.state.rotations == 0

    authorized = transcript_entries(config, "resend_authorized")
    assert authorized and authorized[0]["data"]["reason_code"] == "send_rejected_confirmed_absent"


def test_successful_resend_avoids_rotation(tmp_path):
    """The second send lands, so the chat was never the problem."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=3)  # reject -> reconcile -> resend

    assert orch.state.phase == Phase.AWAITING.value
    assert len(client.submitted) == 2
    assert all(url == CONV_URL for url, _, _ in client.submitted)  # same chat throughout
    assert orch.state.rotations == 0
    assert transcript_entries(config, "conversation_rotated") == []


# ---- 4. a second confirmed rejection parks ----------------------------------


def test_second_confirmed_rejection_parks_and_sends_nothing_more(tmp_path):
    """The bound on the resend. Two disproven sends of one request, in one
    chat, is where the loop stops: it does not send a third time, and (since
    brw-15) it does not move the request to a fresh chat either."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    # reject -> reconcile+resend -> reject -> reconcile+park.
    orch.run(max_steps=5)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert park_code(config) == "send_rejected_twice"
    # Exactly the two sends the resend rule allows, both into the ORIGINAL chat.
    assert [url for url, _, _ in client.submitted] == [CONV_URL, CONV_URL]
    # Nothing was rebound and no budget was spent on a recovery that no longer
    # exists.
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.conversation_epoch == 0
    assert orch.state.pending_request.conversation_url == CONV_URL
    assert orch.state.rotations == 0
    assert client.retargets == [] or PROJECT_URL not in client.retargets
    assert transcript_entries(config, "conversation_rotated") == []


def test_the_park_after_two_rejections_leaves_the_request_byte_identical(tmp_path):
    """A `--resubmit` after this park must send the SAME bytes into the SAME
    chat. Rotation used to rewrite `prompt` (appending a note saying the
    conversation was abandoned) and re-stamp `prompt_sha256`; with it gone,
    nothing on the request may move on the way to the park."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED],
    )
    original = pending()
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=5)

    req = orch.state.pending_request
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert req.prompt == original.prompt
    assert req.prompt_sha256 == original.prompt_sha256
    # And the durable copy says the same thing, so a restart resumes on it.
    reloaded = StateStore(config.state_file).load()
    assert reloaded.pending_request.prompt == original.prompt
    assert reloaded.rotations == 0


# ---- 5. unknown acceptance parks and never resends --------------------------


def test_unknown_acceptance_parks_and_never_resends(tmp_path):
    client = RotatingFakeClient(
        submit_results=[SubmitResult.UNCONFIRMED],
        send_outcomes=[SendOutcome.UNKNOWN],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=4)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert len(client.submitted) == 1  # never a second send
    assert orch.state.rotations == 0
    assert orch.state.pending_request.resends_used == 0
    assert transcript_entries(config, "resend_authorized") == []
    assert transcript_entries(config, "conversation_rotated") == []


# ---- 6/7. failures that take the ordinary failure budget --------------------
#
# Each of these was written when a wedged conversation could rotate, to pin that
# THIS fault is not that one. They are kept: the assertions (the failure budget
# moves, the loop stays retryable, nothing is rebound) are the live behaviour,
# and `rotations == 0` is now a statement about the whole module rather than
# about the branch each test takes.


def test_response_timeout_after_generation_started_never_rotates(tmp_path):
    """A slow or stalled answer is not a broken conversation."""
    client = RotatingFakeClient(responses=[])
    client.seed(CONV_URL, "alr-test-0001")
    client.await_response = _raiser(ResponseTimeoutError("did not complete"))
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.rotations == 0
    assert orch.state.phase == Phase.AWAITING.value  # retried on the failure budget
    assert orch.state.consecutive_failures == 1


def test_login_expiry_never_rotates(tmp_path):
    """A logged-out profile is an account problem, and it must park as
    `login_expired` rather than as anything about the conversation."""
    client = RotatingFakeClient(attach_errors=[LoginExpiredError("logged out")])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.rotations == 0
    assert transcript_entries(config, "conversation_rotated") == []


def test_generic_browser_failure_never_rotates(tmp_path):
    """Rate limits, capacity refusals and transport faults surface as ordinary
    BrowserErrors, and take the ordinary failure budget; only
    ConversationUnusableError is routed away from it."""
    from autoloop.errors import BrowserError

    client = RotatingFakeClient(attach_errors=[BrowserError("too many requests")])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.rotations == 0
    assert orch.state.consecutive_failures == 1
    assert transcript_entries(config, "conversation_rotated") == []


# ---- 8. an unusable conversation parks --------------------------------------


def test_unusable_conversation_parks_without_sending_anything(tmp_path):
    """The fault that used to rotate. The chat loaded and is broken; the loop
    stops, names it, and sends nothing — a replacement chat is an operator
    action now."""
    client = RotatingFakeClient(
        attach_errors=[ConversationUnusableError("wedged")],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert park_code(config) == "conversation_unusable"
    assert client.submitted == [], "a wedged chat must not be answered with a send"
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.rotations == 0
    # The fault is NOT also charged to the browser failure budget — one fault,
    # one accounting. That rule outlives the rotation it was written for.
    assert orch.state.consecutive_failures == 0
    unusable = transcript_entries(config, "conversation_unusable")
    assert unusable and unusable[0]["data"]["reason_code"] == "conversation_unusable"


def test_the_wedged_chat_park_carries_the_errors_own_code(tmp_path):
    """`ConversationUnusableError.code` distinguishes a chat that would not
    load from a submission that provably never appeared. The blocker CODE stays
    the fixed `conversation_unusable` (a varying code set is one nothing can
    map preconditions onto); the error's own code rides in the transcript and
    the question."""
    client = RotatingFakeClient(
        attach_errors=[
            ConversationUnusableError("never appeared", code="submission_never_appeared")
        ],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert park_code(config) == "conversation_unusable"
    unusable = transcript_entries(config, "conversation_unusable")
    assert unusable and unusable[0]["data"]["reason_code"] == "submission_never_appeared"
    assert "submission_never_appeared" in (orch.state.question or "")
    assert CONV_URL in (orch.state.question or ""), "the park must name the wedged chat"


def test_a_wedged_conversation_parks_the_same_way_with_no_project_url(tmp_path):
    """`browser.project_url` used to decide whether this fault could rotate or
    had to park, and the park it produced advised setting that key. It is not
    consulted any more: the same fault, the same park, either way."""
    client = RotatingFakeClient(attach_errors=[ConversationUnusableError("wedged")])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state, project_url="")

    orch.run(max_steps=2)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "conversation_unusable"
    assert orch.state.rotations == 0
    assert "browser.project_url" not in (orch.state.question or "")


# ---- 9/10. per-request conversation binding --------------------------------


def test_reconciliation_uses_the_requests_own_url_not_the_loops(tmp_path):
    """A request bound to the old chat is reconciled against the OLD chat, even
    though the loop has since moved on."""
    client = RotatingFakeClient(conversation_url=NEW_CONV_URL)
    client.seed(CONV_URL, "alr-old-0001")
    state = LoopState.new(NEW_CONV_URL)  # the loop has rotated
    state.rotations = 1
    state.conversation_epoch = 1
    state.phase = Phase.SUBMISSION_UNCONFIRMED.value
    state.pending_request = pending("alr-old-0001", url=CONV_URL, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert client.reconciled == [(CONV_URL, "alr-old-0001")]
    assert orch.state.phase == Phase.AWAITING.value  # found where it was sent


def test_unbound_request_after_a_rotation_refuses_to_guess(tmp_path):
    """A pre-existing request with no binding is adopted only while no rotation
    has happened; afterwards it cannot be attributed and must not be guessed."""
    client = RotatingFakeClient()
    state = LoopState.new(NEW_CONV_URL)
    state.rotations = 1
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending("alr-legacy-0001", url="")
    orch, store, config = build(tmp_path, client, state=state)

    # The refusal is unchanged — what changed (2026-08-03) is that it PARKS
    # with a durable blocker instead of propagating out of the process. The
    # property this test exists for is that nothing is guessed, so it is
    # asserted on the recorded reason rather than on an exception.
    outcome = orch.run(max_steps=1)

    assert outcome == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert "cannot be attributed" in (orch.state.question or "")
    assert orch.state.pending_request.conversation_url == "", "must not be guessed"


def test_legacy_request_binds_to_the_loop_url_before_any_rotation(tmp_path):
    client = RotatingFakeClient(responses=[stop_block()])
    client.seed(CONV_URL, "alr-legacy-0001")
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending("alr-legacy-0001", url="")
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.pending_request.conversation_url == CONV_URL


# ---- 11. the rotation budget is inert --------------------------------------


def test_a_spent_rotation_budget_no_longer_changes_anything(tmp_path):
    """A state file left by an older process can carry `rotations > 0`. That
    used to be the difference between rotating and parking `rotation_cap_
    reached`; with no rotation to cap, the same fault takes the same park and
    the counter is neither read nor written."""
    client = RotatingFakeClient(attach_errors=[ConversationUnusableError("wedged again")])
    state = LoopState.new(CONV_URL)
    state.rotations = 1  # an older run's spent budget
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "conversation_unusable"
    assert orch.state.rotations == 1, "left exactly as found"
    assert "max_conversation_rotations" not in (orch.state.question or "")


def test_the_policy_budget_survives_for_the_state_readers_that_still_use_it():
    """`policy.max_conversation_rotations` and `PolicyEngine.check_rotation_
    budget` are deliberately NOT removed: `cli._reset_run_scoped_budgets` and
    `doctor`'s conversation checks still read the counter they bound, and
    `policy.py` is outside brw-15's scope. Nothing in `orchestrator.py` calls
    this any more — `test_no_rotation_machinery_is_reachable_from_the_
    orchestrator` is what pins that half."""
    engine = PolicyEngine(PolicyConfig(max_conversation_rotations=1))
    assert engine.check_rotation_budget(0).allowed
    assert not engine.check_rotation_budget(1).allowed
    zero = PolicyEngine(PolicyConfig(max_conversation_rotations=0))
    assert zero.check_rotation_budget(0).allowed is False


def test_no_rotation_machinery_is_reachable_from_the_orchestrator():
    """The claim brw-15 makes, asserted rather than described.

    Named methods first, because those are what the removal was about; then the
    module source, because a re-added helper under a new name would still have
    to build a URL, spend the budget or emit one of the retired codes to do
    anything, and every one of those is a string this catches."""
    import inspect

    from autoloop import orchestrator as orchestrator_module

    for gone in (
        "_attempt_rotation",
        "_attempt_silence_rotation",
        "_rotate_conversation",
        "_park_rotation",
        "_url_in_project",
        "_continuation_prompt",
        "_heal_config_url",
        "_reconcile_no_response",
    ):
        assert not hasattr(Orchestrator, gone), f"{gone} is back"
    for gone in ("CONTINUATION_NOTE", "ROTATION_URL_TIMEOUT_SECONDS",
                 "ROTATION_URL_POLL_SECONDS", "PLACEHOLDER_CONVERSATION_PREFIX"):
        assert not hasattr(orchestrator_module, gone), f"{gone} is back"

    source = inspect.getsource(orchestrator_module)
    # The retired blocker codes. Quoted with their `code=`/`"..."` punctuation
    # so the module docstring, which names them as history, does not match.
    for retired in ("rotation_unavailable", "rotation_cap_reached",
                    "rotation_failed", "rotation_unsupported_by_transport"):
        assert f'"{retired}"' not in source, f"{retired} is emitted again"
    # And the two calls any replacement would have to make.
    assert "check_rotation_budget" not in source
    assert "rotations +=" not in source


# ---- 12. the config heal is atomic and refuses tracked files ----------------


CONFIG_TEXT = """# autoloop config
[browser]
# the one persistent conversation
conversation_url = "https://chatgpt.com/c/old"   # keep this in sync
project_url = "https://chatgpt.com/g/g-p-x/project"
cdp_url = "http://127.0.0.1:9222"

[policy]
max_iterations = 20
conversation_url = "not-this-one"
"""


def test_config_rewrite_touches_only_the_browser_url():
    patched = replace_conversation_url(CONFIG_TEXT, "https://chatgpt.com/c/new")
    assert 'conversation_url = "https://chatgpt.com/c/new"   # keep this in sync' in patched
    # Comments, ordering and every other key survive byte for byte.
    assert "# the one persistent conversation" in patched
    assert 'cdp_url = "http://127.0.0.1:9222"' in patched
    # A same-named key in another section is out of scope and untouched.
    assert 'conversation_url = "not-this-one"' in patched


def test_config_rewrite_refuses_a_url_that_would_break_the_toml():
    with pytest.raises(ConfigError):
        replace_conversation_url(CONFIG_TEXT, 'https://chatgpt.com/c/x"evil')


def test_config_rewrite_refuses_to_invent_a_missing_key():
    with pytest.raises(ConfigError):
        replace_conversation_url("[browser]\ncdp_url = \"x\"\n", "https://chatgpt.com/c/new")


def test_config_update_is_atomic_and_leaves_no_temp_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")

    update_conversation_url(path, "https://chatgpt.com/c/new", repo)

    assert 'conversation_url = "https://chatgpt.com/c/new"' in path.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_config_update_refuses_a_git_tracked_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    tracked = repo / "config.toml"
    tracked.write_text(CONFIG_TEXT, encoding="utf-8")
    subprocess.run(["git", "add", "config.toml"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "add"], cwd=repo, check=True
    )

    with pytest.raises(ConfigError) as exc:
        assert_untracked(tracked, repo)
    assert "tracked by git" in str(exc.value)

    with pytest.raises(ConfigError):
        update_conversation_url(tracked, "https://chatgpt.com/c/new", repo)
    # Refusal means refusal: the file is byte-identical.
    assert tracked.read_text(encoding="utf-8") == CONFIG_TEXT


def test_a_wedged_conversation_never_rewrites_the_config(tmp_path):
    """`config_writer` is still exercised above, but nothing in the loop calls
    it any more: the rotation's config heal was its one caller. A fault that
    used to end with the config pointing at a new chat must now leave the file
    byte-identical — the operator owns that edit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    config_path = tmp_path / "al-config.toml"
    before = CONFIG_TEXT.replace("https://chatgpt.com/c/old", CONV_URL)
    config_path.write_text(before, encoding="utf-8")
    client = RotatingFakeClient(
        attach_errors=[ConversationUnusableError("wedged")], responses=[stop_block()]
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state, config_path=config_path)

    orch.run(max_steps=2)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert config_path.read_text(encoding="utf-8") == before
    assert transcript_entries(config, "config_heal_failed") == []


def test_drift_guard_accepts_a_recorded_rotation_and_nothing_else(tmp_path):
    """`cli._drift_is_recorded_rotation` is deliberately left alone by brw-15.
    Nothing writes `last_rotation` any more, but a state file written before
    the removal still carries one, and refusing to start on it would strand a
    session on a recovery a previous process completed."""
    from autoloop.cli import _drift_is_recorded_rotation

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL),
        policy=PolicyConfig(),
        state_dir=tmp_path,
    )
    state = LoopState.new(NEW_CONV_URL)
    state.rotations = 1
    state.last_rotation = {"old_url": CONV_URL, "new_url": NEW_CONV_URL}
    assert _drift_is_recorded_rotation(state, config)

    # An operator editing the config to a third URL is still refused.
    state.last_rotation = {"old_url": "https://chatgpt.com/c/somewhere", "new_url": NEW_CONV_URL}
    assert not _drift_is_recorded_rotation(state, config)

    # A state that never rotated is still refused.
    state.rotations = 0
    state.last_rotation = {"old_url": CONV_URL, "new_url": NEW_CONV_URL}
    assert not _drift_is_recorded_rotation(state, config)


# ---- 13. observations carry no sensitive metadata ---------------------------


def test_observation_vocabulary_cannot_express_a_secret():
    fields = set(SendObservation.__dataclass_fields__)
    assert fields == {"path", "status", "failure"}
    for forbidden in ("headers", "cookie", "cookies", "authorization", "body", "token"):
        assert forbidden not in fields


def test_observed_paths_drop_query_strings():
    assert scrub_path("https://chatgpt.com/backend-api/conversation?token=SECRET") == (
        "/backend-api/conversation"
    )
    assert "SECRET" not in scrub_path("https://chatgpt.com/backend-api/conversation?token=SECRET")


def test_rejected_submission_logs_only_path_status_and_failure(tmp_path):
    client = RotatingFakeClient(submit_results=[SubmitResult.REJECTED])
    client.send_observations = [
        SendObservation(path="/backend-api/conversation", status=429, failure="")
    ]
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    rows = transcript_entries(config, "submission_rejected")
    assert rows
    observations = rows[0]["data"]["observations"]
    assert observations == [{"path": "/backend-api/conversation", "status": 429, "failure": ""}]
    raw = config.transcript_file.read_text(encoding="utf-8").lower()
    for forbidden in ("cookie", "authorization", "bearer", "session-token"):
        assert forbidden not in raw


# ---- 14. a restart preserves the binding ------------------------------------


def test_restart_preserves_the_requests_own_binding(tmp_path):
    """The binding is durable, and a park does not disturb it: the request the
    next process reads is still the ORIGINAL chat's, which is where a
    `--resubmit` has to go."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)
    orch.run(max_steps=5)

    reloaded = StateStore(config.state_file).load()

    assert reloaded.phase == Phase.NEEDS_USER.value
    assert reloaded.rotations == 0
    assert reloaded.conversation_url == CONV_URL
    assert reloaded.conversation_epoch == 0
    assert reloaded.pending_request.conversation_url == CONV_URL
    assert reloaded.pending_request.conversation_epoch == 0
    assert reloaded.last_rotation in (None, {})


def test_rejected_outcome_survives_a_restart(tmp_path):
    """Recovery resumes from the same evidence the live run had, instead of
    downgrading a disproven send to 'ambiguous' and parking a human."""
    client = RotatingFakeClient(submit_results=[SubmitResult.REJECTED])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)
    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.pending_request.last_send_outcome == SendOutcome.REJECTED.value

    # A fresh orchestrator over that state routes back to the rejected phase
    # rather than parking as ambiguous.
    resumed_client = RotatingFakeClient(submit_results=[SubmitResult.CONFIRMED])
    reloaded.phase = Phase.SUBMITTING.value
    second = tmp_path / "second"
    second.mkdir()
    orch2, _, config2 = build(second, resumed_client, state=reloaded)
    orch2.run(max_steps=1)
    assert orch2.state.phase == Phase.SUBMISSION_REJECTED.value


# ---- 15. every send this loop makes goes to the request's own chat ----------


def test_the_only_sends_are_the_two_the_resend_rule_allows(tmp_path):
    """The replacement for `test_new_chat_prompt_is_the_original_plus_one_
    continuation_line`. There is no replacement chat and no continuation note:
    the prompt that goes out the second time is the SAME prompt, into the SAME
    conversation, and there is never a third."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    original_prompt = pending().prompt
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=5)

    assert len(client.submitted) == 2
    for url, rid, prompt in client.submitted:
        assert url == CONV_URL
        assert rid == "alr-test-0001"
        assert prompt == original_prompt
    assert PROJECT_URL not in [url for url, _, _ in client.submitted]


# ---- 16. the silent conversation ------------------------------------------
#
# A CONFIRMED, persisted send whose assistant turn never starts: the send is
# known good, the model simply never begins. It used to be a third rotation
# trigger, earned by three `ResponseTimeoutError(stage="start")`s, an
# accumulated wait past a configured floor and one final reconciliation. Since
# brw-15 it is an ordinary transport fault that also advances two per-request
# counters, and these tests pin exactly that: the counters move, they survive a
# restart, and nothing else happens.


def _start_timeout(elapsed=125.0):
    """A `responses` queue entry that raises the response-START timeout (never
    the response-COMPLETE one) `_handle_response_start_timeout` watches for.
    `elapsed` is the ACTUAL measured wait the exception reports, kept above
    the 120s configured start timeout `build()` uses — what a real timeout
    would measure.
    """

    def _raise(client):
        raise ResponseTimeoutError(
            "no assistant response began within 120.0s", stage="start", elapsed=elapsed
        )

    return _raise


def test_two_start_timeouts_are_an_ordinary_retry(tmp_path):
    """Two response-START timeouts advance the counters and leave the loop
    retryable on the ordinary failure budget."""
    client = RotatingFakeClient(responses=[_start_timeout(), _start_timeout()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert orch.state.pending_request.start_timeouts == 2
    assert orch.state.phase == Phase.AWAITING.value  # retried, not parked
    assert orch.state.rotations == 0
    assert client.no_response_calls == []
    assert transcript_entries(config, "conversation_rotated") == []


def test_a_third_start_timeout_is_still_only_the_failure_budget(tmp_path):
    """THE removed trigger. Three consecutive response-START timeouts, an
    accumulated wait far past the old floor, and a transport that would answer
    the final silence check with "still silent" — the exact state that used to
    rotate. Now: no silence check is even asked for, nothing is sent, nothing
    is rebound, and the loop is still on the ordinary failure budget."""
    client = RotatingFakeClient(
        responses=[_start_timeout(), _start_timeout(), _start_timeout()],
        no_response_results=[True],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=3)

    assert orch.state.rotations == 0
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.conversation_epoch == 0
    assert orch.state.pending_request.conversation_url == CONV_URL
    assert orch.state.pending_request.start_timeouts == 3, "counted, not acted on"
    assert client.no_response_calls == [], "the silence check is gone, not merely unused"
    assert client.submitted == [], "the trigger's whole point was a send; there is none"
    assert transcript_entries(config, "conversation_rotated") == []
    assert transcript_entries(config, "response_silence_confirmed") == []
    # The failure budget is what governs it, and three faults is what it saw.
    assert orch.state.consecutive_failures == 3


def test_a_reply_that_arrives_after_the_timeouts_is_still_read(tmp_path):
    """The counters must not become a trap of their own: a conversation that
    was merely slow answers on the next step, from its OWN chat, and the loop
    proceeds."""
    client = RotatingFakeClient(
        responses=[
            _start_timeout(),
            _start_timeout(),
            _start_timeout(),
            stop_block("late, but here"),
        ],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=4)

    assert client.awaited == [(CONV_URL, "alr-test-0001")] * 4
    assert orch.state.last_response is not None
    assert orch.state.last_response.conversation_url == CONV_URL
    assert client.submitted == []


def test_no_send_at_all_during_timeout_retries(tmp_path):
    """`awaiting` never resends on its own account. The one send this sequence
    used to make — the rotation's, into a replacement chat — is gone, so the
    answer is now simply zero."""
    client = RotatingFakeClient(
        responses=[_start_timeout(), _start_timeout(), _start_timeout()],
        no_response_results=[True],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=3)

    assert client.submitted == []
    assert client.retargets == [] or PROJECT_URL not in client.retargets


def test_restart_preserves_the_timeout_count(tmp_path):
    """A crash after two response-start timeouts loses neither counter: a
    fresh process resumes both from disk, and the third timeout — now against
    a fresh client — keeps counting rather than starting over."""
    client = RotatingFakeClient(responses=[_start_timeout(), _start_timeout()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True)
    orch, store, config = build(tmp_path, client, state=state)
    orch.run(max_steps=2)
    assert orch.state.pending_request.start_timeouts == 2

    reloaded = StateStore(config.state_file).load()
    assert reloaded.pending_request.start_timeouts == 2
    assert reloaded.pending_request.start_timeout_wait_seconds == pytest.approx(250.0)
    assert reloaded.phase == Phase.AWAITING.value
    assert reloaded.rotations == 0
    assert reloaded.conversation_epoch == 0
    assert reloaded.pending_request.conversation_url == CONV_URL

    resumed_client = RotatingFakeClient(responses=[_start_timeout()])
    second = tmp_path / "second"
    second.mkdir()
    orch2, _, config2 = build(second, resumed_client, state=reloaded)
    orch2.run(max_steps=1)

    assert orch2.state.pending_request.start_timeouts == 3
    assert orch2.state.rotations == 0
    assert orch2.state.conversation_url == CONV_URL
    assert orch2.state.conversation_epoch == 0

    reloaded2 = StateStore(config2.state_file).load()
    assert reloaded2.pending_request.start_timeouts == 3
    assert reloaded2.pending_request.conversation_url == CONV_URL
    assert reloaded2.last_rotation in (None, {})


# ---- classification unit tests ---------------------------------------------


def test_classification_is_conservative():
    assert classify_submission([]) is SendOutcome.UNKNOWN
    ok = SendObservation(path="/backend-api/conversation", status=200)
    bad = SendObservation(path="/backend-api/conversation", status=500)
    dead = SendObservation(path="/backend-api/conversation", status=None, failure="ERR_FAILED")
    assert classify_submission([ok]) is SendOutcome.ACCEPTED
    assert classify_submission([bad]) is SendOutcome.REJECTED
    assert classify_submission([dead]) is SendOutcome.REJECTED
    # A window holding both a failure and a success is exactly where a resend
    # could double-post, so it resolves to UNKNOWN, not to either verdict.
    assert classify_submission([bad, ok]) is SendOutcome.UNKNOWN
    assert classify_submission([ok, bad]) is SendOutcome.UNKNOWN


def test_send_path_allowlist_is_narrow():
    assert is_send_path("https://chatgpt.com/backend-api/conversation")
    assert is_send_path("https://chatgpt.com/backend-api/f/conversation")
    assert is_send_path("https://chatgpt.com/backend-api/conversation/abc123")
    # Neighbouring endpoints that are NOT a turn submission.
    assert not is_send_path("https://chatgpt.com/backend-api/conversation/init")
    assert not is_send_path("https://chatgpt.com/backend-api/conversation/gen_title")
    assert not is_send_path("https://chatgpt.com/backend-api/models")
    assert not is_send_path("https://chatgpt.com/backend-api/accounts/check")
    assert not is_send_path("https://chatgpt.com/")


def _raiser(exc):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


# ---- three incidents that no longer have code to happen in ------------------
#
# Removed with the rotation machinery (brw-15), and listed rather than silently
# dropped, because each was a real production failure and the next person to
# consider re-adding a "just open a fresh chat" recovery should know what it
# costs to get right:
#
#   * 2026-08-03, twice — a replacement chat that existed and held the request
#     was refused because its address had not been minted yet, or because the
#     project-membership compare mishandled ChatGPT's slug suffix. Three
#     rotations failed leaving orphaned chats nobody read
#     (`test_rotation_waits_for_the_chat_id_instead_of_reading_the_project_page`,
#     `test_a_rotation_is_found_by_request_id_when_the_address_bar_lags`,
#     `test_a_rotation_still_refuses_when_no_chat_carries_the_request`).
#   * 2026-08-16 — a chat opened from a project page carries a placeholder
#     `/c/WEB:<uuid>` address, under no project, until its first message lands.
#     Judging membership on it parked the loop LOOP-FATAL on a rotation that had
#     worked (`test_a_placeholder_address_is_primed_and_then_accepted` and the
#     three tests beside it).
#
# `RotatingFakeClient.placeholder_until` / `strand_the_address_bar` /
# `find_finds_nothing` are what those tests drove; they are left on the fake
# because it models a browser, not because anything still uses them.


def test_the_loop_never_navigates_away_from_the_requests_own_chat(tmp_path):
    """The property all three 2026-08-03 incidents were downstream of: the loop
    used to drive the browser to a PROJECT PAGE and reason about where it ended
    up. Nothing does that now — every `retarget` this loop issues aims at the
    conversation the request is already bound to, so an address bar that lags,
    strands or shows a placeholder cannot mislead anything."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED],
    )
    client.strand_the_address_bar(PROJECT_URL)
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending("alr-x-0001")
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=5)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "send_rejected_twice"
    assert all(url == CONV_URL for url in client.retargets)
    assert orch.state.conversation_url == CONV_URL


# ---- what the removed priming block was about (2026-08-16) ------------------
#
# A chat opened from a project page has no durable URL until its first message
# lands; the address until then is `https://chatgpt.com/c/WEB:<uuid>`, under no
# project at all. The wait that used to stop at the first change handed that
# placeholder to the project-membership check, which refused it every time and
# parked the loop LOOP-FATAL on a rotation that had actually worked. Four tests
# pinned the repair (prime, then judge; still refuse a chat genuinely outside
# the project; bound the wait; make the new URL durable before anything else).
# All four went with the machinery. `PLACEHOLDER_URL` and `OUTSIDE_PROJECT_URL`
# are kept above as the documented shapes, since nothing else records them.


# ---- a FALSE ambiguity is resolved by PROVING the request is there ----------
#
# The asymmetry these tests pin down, and the reason it is not symmetric:
#
#   * proved PRESENT -> resolve automatically. Nothing is ambiguous, resuming
#     sends nothing, so the worst a wrong "present" can do is wait in a chat
#     that holds the request.
#   * absent, or unproven either way -> park, exactly as before. Acting on
#     absence means resending, which can duplicate a request the backend
#     accepted — unrecoverable — and absence is precisely the conclusion a
#     flaky read gets wrong.
#
# So: prove presence and proceed; never infer absence and act.

RESCUED = "alr-af11e1b3-0006"


def park_code(config):
    rows = transcript_entries(config, "needs_user")
    return rows[-1]["data"]["code"] if rows else None


#: Sentences that assert the request IS NOT THERE. Exactly one park may use
#: them: the one where the by-content search walked the chats to their end and
#: came back empty. Everywhere else — no project configured, a search that
#: refused to conclude, a wedged page, a provider that cannot search — nothing
#: read the history, so claiming absence would be manufacturing the evidence
#: that points an operator at `--resubmit`.
#:
#: Keyed on phrases rather than on the word "absent": the genuine-miss note
#: legitimately says "evidence of absence", so a bare-token check would flag the
#: one park that is entitled to the claim and miss "not in persisted history".
ABSENCE_CLAIMS = (
    "not in persisted history",
    "read its recent chats to the end",
    "did not find the request",
)


def assert_claims_no_absence(question):
    for claim in ABSENCE_CLAIMS:
        assert claim not in (question or ""), (
            f"this park established nothing about presence, so it must not say {claim!r}"
        )


def ambiguous_state(request_id=RESCUED):
    """A send was attempted, acceptance was never observed, and the loop is
    about to decide whether a human has to look at it."""
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMISSION_UNCONFIRMED.value
    state.pending_request = pending(request_id, send_attempted=True)
    return state


def test_a_request_the_search_proves_present_resolves_without_parking(tmp_path):
    """The park that should never have happened (2026-08-05, `alr-af11e1b3-0006`).

    The request WAS in the conversation and had already been answered with
    `decision push`; reading it by hand took pressing End and scrolling six
    times, because ChatGPT mounts a window of a chat rather than its history.
    `reconcile`'s window read missed it, so the loop parked a human on an
    ambiguity that did not exist — and a resend would have double-posted a
    request that had already been reviewed.

    The by-content search mounts the tail, so it sees what the reload could
    not. Proof of presence resolves the park by itself: the request is there,
    so the loop resumes into `awaiting` and reads the answer.
    """
    client = RotatingFakeClient(responses=[stop_block()])
    client.hide_from_the_window(CONV_URL, RESCUED)
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=4)

    # The one assertion that makes this safe rather than merely convenient.
    assert client.submitted == [], "resolving an ambiguity must SEND NOTHING"
    assert transcript_entries(config, "submission_ambiguous") == []
    assert park_code(config) != "submission_ambiguous"
    confirmed = transcript_entries(config, "submission_confirmed_by_search")
    assert confirmed and confirmed[-1]["data"]["url"] == CONV_URL
    assert orch.state.phase != Phase.NEEDS_USER.value
    assert client.awaited, "it must go on to read the answer that was already there"


def test_the_resolution_survives_a_project_relative_search_result(tmp_path):
    """The trap that would make this fix silently never fire in production
    while every hand-typed-URL test stayed green. `find_conversation_with`
    builds candidates with `urljoin(project_url, href)`, and ChatGPT's list
    hrefs are `/c/<id>` — so the same chat comes back WITHOUT the project
    prefix the request is bound to. A string compare calls those two different
    conversations and parks."""
    stripped = "https://chatgpt.com/c/original-chat"
    assert stripped != CONV_URL and Orchestrator._same_conversation(stripped, CONV_URL)

    client = RotatingFakeClient(responses=[stop_block()])
    client.hide_from_the_window(CONV_URL, RESCUED)
    client.find_conversation_with = lambda rid, project, limit=6: stripped
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=4)

    assert orch.state.phase != Phase.NEEDS_USER.value
    assert client.submitted == []


def test_a_genuinely_absent_request_still_parks(tmp_path):
    """The direction that is NOT automated. The search read the project to the
    end and the request is in none of it — which is exactly the reading that
    would authorize a resend, and exactly the reading a flaky check gets
    wrong. Absence stays a human's call."""
    client = RotatingFakeClient(responses=[])
    orch, store, config = build(tmp_path, client, state=ambiguous_state("alr-absent-0001"))

    orch.run(max_steps=3)

    assert client.find_calls, "it must have looked before parking"
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
    assert client.submitted == [], "a park never resends"
    assert transcript_entries(config, "submission_confirmed_by_search") == []
    # The park must say the project WAS read — the fact that separates this
    # from a park where no search ran, and the one that tells the operator a
    # `--resubmit` is the plausible next move. This is the ONLY park entitled
    # to language that means "it is not there", so it must keep it: weakening
    # every park uniformly would leave the operator unable to tell a real miss
    # from a read that never happened.
    assert "read its recent chats to the end" in (orch.state.question or "")
    assert "did not find the request in any of them" in (orch.state.question or "")


def test_an_inconclusive_search_parks_rather_than_guessing(tmp_path):
    """A search that read a page it could not vouch for — a rotation moved the
    client mid-flight, or a virtualized list never proved it reached its end —
    says NOTHING about presence. "Said nothing" must not read as "absent": it
    parks, with the refusal recorded so the operator knows the search was not
    a clean miss."""
    client = RotatingFakeClient(responses=[])
    client.find_error = ConversationSearchInconclusive(
        "the search asked for one chat and is on another"
    )
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=3)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
    assert client.submitted == []
    refused = transcript_entries(config, "presence_search_inconclusive")
    assert refused and refused[-1]["data"]["reason_code"] == "search_refused_to_conclude"
    # And the question must READ like a refusal. A park whose opening sentence
    # says the request is not in persisted history, followed by a note saying
    # the search settled nothing, contradicts itself and steers the operator
    # toward `--resubmit` on evidence nobody gathered.
    assert "could not settle it" in (orch.state.question or "")
    assert_claims_no_absence(orch.state.question)


def test_login_expiry_during_the_search_is_not_demoted_to_ambiguity(tmp_path):
    """`LoginExpiredError` is a `BrowserError`, so a search-site clause catching
    that base would swallow it — and every logged-out profile would be reported
    as an ambiguous submission, which is precisely the misclassification this
    whole change exists to remove. It propagates instead, and `run()` parks it
    as `login_expired` with THIS phase as the resume point, so logging back in
    and retrying comes straight back to the search with nothing sent in between.
    Widen `_search_for_request`'s `except` back to `BrowserError` and this test
    is what fails."""
    client = RotatingFakeClient(responses=[])
    client.find_error = LoginExpiredError("session expired")
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=3)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "login_expired"
    assert orch.state.resume_phase == Phase.SUBMISSION_UNCONFIRMED.value
    assert client.submitted == []
    assert transcript_entries(config, "submission_ambiguous") == []


@pytest.mark.parametrize(
    "error",
    [
        SessionLostError("cdp connection closed"),
        BrowserError("navigation failed"),
    ],
    ids=["session_lost", "generic_browser_error"],
)
def test_a_dead_browser_during_the_search_is_not_demoted_to_ambiguity(tmp_path, error):
    """A search that DIED is not a search that concluded anything.

    Only `ConversationSearchInconclusive` — the search's own verdict on its own
    evidence — may be collapsed into the ambiguity park. A dropped CDP
    connection or a page that never loaded is a transport fault the orchestrator
    already knows how to recover from: `run()` routes it to
    `_handle_browser_failure`, which drops the client, tries a browser restart
    and otherwise charges the ordinary failure budget, leaving the phase intact
    so the next step re-enters the search with a live browser.

    Catching it here would throw that away twice over: the restart never
    happens, and a dead browser — which says NOTHING about whether the request
    is in the conversation — gets reported to a human as evidence uncertainty.
    That is the same misclassification the whole change exists to remove, so
    both the named subclass and the bare base are pinned here.
    """
    client = RotatingFakeClient(responses=[])
    client.find_error = error
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    # Exactly one step: `run()` checks the budget BEFORE stepping, so this is a
    # single `_step`. A sticky error retried past `max_consecutive_failures`
    # would land in `failed` and hide the phase this asserts on.
    orch.run(max_steps=1)

    assert client.find_calls, "the search must be what failed, not something before it"
    assert client.submitted == [], "no route out of this phase sends anything"
    # Routed as the browser failure it is...
    errors = transcript_entries(config, "browser_error")
    assert errors and errors[-1]["data"]["kind"] == type(error).__name__
    assert errors[-1]["data"]["phase"] == Phase.SUBMISSION_UNCONFIRMED.value
    assert orch.state.consecutive_failures == 1, "the ordinary failure budget governs it"
    # ...and NOT as anything about the evidence.
    assert transcript_entries(config, "submission_ambiguous") == []
    assert transcript_entries(config, "presence_search_inconclusive") == []
    assert park_code(config) != "submission_ambiguous"
    # The phase survives, so the retry comes straight back to the search.
    assert orch.state.phase == Phase.SUBMISSION_UNCONFIRMED.value


def test_a_wedged_page_during_the_search_never_condemns_this_conversation(tmp_path):
    """The one browser fault that is caught here, and why it is the exception
    that proves the rule.

    `ConversationUnusableError` is not collapsed because its route says too
    little — it is collapsed because its route CONDEMNS THE WRONG CHAT. The
    search walks the project page and up to `limit` OTHER chats, so the page
    that raised it is usually not this request's conversation at all; letting it
    through would park `conversation_unusable` naming the chat this request IS
    bound to, on evidence gathered from a different one. (Until brw-15 it was
    worse: that route rotated, which POSTS the request id into a replacement
    chat — a send, from the one phase whose entire contract is that only
    `--resubmit` repeats one. That is why this test is older than the park it
    now asserts.)
    """
    client = RotatingFakeClient(responses=[])
    client.find_error = ConversationUnusableError("this chat appears wedged")
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=3)

    assert client.submitted == [], "a wedged page elsewhere must not cause a send"
    assert client.retargets == [], "and must not rebind the loop to another chat"
    assert orch.state.rotations == 0
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
    assert park_code(config) != "conversation_unusable"
    refused = transcript_entries(config, "presence_search_inconclusive")
    assert refused and refused[-1]["data"]["kind"] == "ConversationUnusableError"
    # A wedged page says nothing about presence, so neither may the park.
    assert "could not settle it" in (orch.state.question or "")
    assert_claims_no_absence(orch.state.question)


def test_a_hit_in_a_different_chat_parks_and_names_it(tmp_path):
    """Presence somewhere is not presence HERE. A loop moved to another chat —
    by an operator, or by a rotation an older process performed — keeps the
    request id, so a hit outside this request's own conversation can be a
    retired copy, and rebinding to it on that evidence would be a rebinding
    performed on a duplicate id. It parks, and the operator is told which chat
    to read instead of being sent to look for a message that may not exist."""
    client = RotatingFakeClient(responses=[])
    client.seed(NEW_CONV_URL, RESCUED)  # some OTHER chat carries the id
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=3)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
    assert client.submitted == []
    assert NEW_CONV_URL in (orch.state.question or "")
    ambiguous = transcript_entries(config, "submission_ambiguous")
    assert ambiguous[-1]["data"]["found_elsewhere"] == NEW_CONV_URL
    # The search FOUND the id — a park that also opened by declaring it not in
    # persisted history would contradict its own next sentence.
    assert_claims_no_absence(orch.state.question)


def test_a_provider_without_the_search_parks_exactly_as_before(tmp_path):
    """The capability is PROBED, like `retarget`/`current_url`. A transport
    that cannot search the way ChatGPT's chat list can must lose nothing and
    gain nothing — it parks on the same evidence it always did."""
    client = RotatingFakeClient(responses=[])
    # Assigned None rather than deleted: the method lives on the CLASS, so a
    # `del` on the instance raises. The orchestrator's `getattr` probe reads
    # both the same way — no capability.
    client.find_conversation_with = None
    orch, store, config = build(tmp_path, client, state=ambiguous_state())

    orch.run(max_steps=3)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
    skipped = transcript_entries(config, "presence_search_skipped")
    assert skipped and skipped[-1]["data"]["reason_code"] == "provider_cannot_search"
    # No search ran, so no sentence in this park may say the request is missing.
    assert "cannot search a project" in (orch.state.question or "")
    assert_claims_no_absence(orch.state.question)


def test_without_a_project_url_there_is_nothing_to_search(tmp_path):
    """No project configured is no chat list to read, so the search cannot run
    at all. It must fall through to the park rather than treat "could not
    look" as "not there"."""
    client = RotatingFakeClient(responses=[])
    orch, store, config = build(
        tmp_path, client, state=ambiguous_state(), project_url=""
    )

    orch.run(max_steps=3)

    assert client.find_calls == []
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
    skipped = transcript_entries(config, "presence_search_skipped")
    assert skipped and skipped[-1]["data"]["reason_code"] == "no_project_url"
    # And it must not claim a search it never ran. The first draft of the park
    # message said "a by-content search did not find it either" in this exact
    # case — manufacturing evidence, and pointing the operator at `--resubmit`
    # on the strength of a read nobody performed.
    assert "project_url is not configured" in (orch.state.question or "")
    # The same objection applies to the sentence ABOVE the note, which is where
    # it survived longest: nothing here read the history, so the park may not
    # open by asserting the request is not in it.
    assert_claims_no_absence(orch.state.question)


def test_resubmit_is_still_the_only_thing_that_repeats_a_send(tmp_path):
    """The park is not weakened. A request the search cannot find is parked and
    stays parked: the loop resends nothing on its own, no matter how many times
    it is run. Only the operator's explicit `--resubmit` — one more send of the
    SAME request id, so a message that did land is detected rather than
    duplicated — repeats it."""
    client = RotatingFakeClient(responses=[stop_block()])
    orch, store, config = build(tmp_path, client, state=ambiguous_state("alr-absent-0002"))

    orch.run(max_steps=3)
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert client.submitted == []

    # Re-running while parked changes nothing — `needs_user` is terminal for
    # the run and no automatic path back into `submitting` exists.
    orch.run(max_steps=3)
    assert client.submitted == []

    _authorize_resubmit(orch.state)
    store.save(orch.state)
    orch.run(max_steps=4)

    assert len(client.submitted) == 1, "the operator's one authorized send, and only it"
    assert client.submitted[0][:2] == (CONV_URL, "alr-absent-0002")
