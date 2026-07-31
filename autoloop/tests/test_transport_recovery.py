"""Transport recovery: submission classification, the one same-chat resend, and
bounded conversation rotation.

The invariant every test here defends is the same one: **what the loop is
allowed to do automatically depends on what it can prove, not on what it is
guessing.** A send whose acceptance is merely unobserved licenses nothing (the
backend may have taken it, so resending risks a double post). A send the browser
itself reports as failed licenses exactly one resend, after reconciliation
confirms the request is absent. A second such failure licenses one rotation.
Nothing licenses two.

`RotatingFakeClient` models the shape of the real transport rather than its
mechanics: a per-conversation server truth, a page URL that only becomes a real
`/c/<id>` after the first turn lands (which is why rotation has to submit before
it can capture), and a `retarget` that decides which conversation every
subsequent call reads.
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
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.config_writer import (
    assert_untracked,
    replace_conversation_url,
    update_conversation_url,
)
from autoloop.errors import (
    ConfigError,
    ConversationUnusableError,
    LoginExpiredError,
    ResponseTimeoutError,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import CONTINUATION_NOTE, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

from test_orchestrator import FakeExecutor, FakeGit, block  # noqa: E402 - see conftest sys.path

PROJECT_URL = "https://chatgpt.com/g/g-p-proj123-demo/project"
CONV_URL = "https://chatgpt.com/g/g-p-proj123-demo/c/original-chat"
NEW_CONV_URL = "https://chatgpt.com/g/g-p-proj123-demo/c/replacement-chat"


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

    # -- test helpers ------------------------------------------------------
    def seed(self, url, request_id):
        self.persisted.setdefault(url, set()).add(request_id)

    # -- LLMConversation surface -------------------------------------------
    def attach(self):
        self.attach_calls += 1
        if self.attach_errors:
            raise self.attach_errors.pop(0)

    def retarget(self, url):
        self.retargets.append(url)
        self.conversation_url = url

    def current_url(self):
        return self.page_url

    def has_request(self, request_id):
        return request_id in self.persisted.get(self.conversation_url, set())

    def reconcile(self, request_id):
        self.reconciled.append((self.conversation_url, request_id))
        return self.has_request(request_id)

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


# ---- 1. a persisted send is never resent and never rotates ------------------


def test_persisted_send_never_resends_or_rotates(tmp_path):
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


# ---- 4. a second confirmed rejection rotates, exactly once ------------------


def test_second_confirmed_rejection_rotates_exactly_once(tmp_path):
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    # reject -> reconcile+resend -> reject -> reconcile+rotate. Deliberately
    # stopped at the rotation: a fifth step would consume the reply and clear
    # the pending request this test is asserting about.
    orch.run(max_steps=4)

    assert orch.state.rotations == 1
    assert orch.state.conversation_url == NEW_CONV_URL
    assert orch.state.conversation_epoch == 1
    assert orch.state.pending_request.conversation_url == NEW_CONV_URL
    assert orch.state.phase == Phase.AWAITING.value
    # Rotation opened the project page, sent there, then bound the minted URL.
    assert PROJECT_URL in client.retargets
    assert client.retargets[-1] == NEW_CONV_URL
    rotated = transcript_entries(config, "conversation_rotated")
    assert len(rotated) == 1
    assert rotated[0]["data"]["old_url"] == CONV_URL
    assert rotated[0]["data"]["new_url"] == NEW_CONV_URL
    assert rotated[0]["data"]["reason"] == "send_rejected_twice"


def test_rotation_binds_only_after_reconciling_against_the_new_url(tmp_path):
    """The address bar is not evidence: a new chat that does not hold the
    request after reconciliation is refused, and nothing is rebound."""

    class NoPersistOnRotate(RotatingFakeClient):
        def submit(self, request_id, prompt):
            result = super().submit(request_id, prompt)
            if self.conversation_url == PROJECT_URL:
                # URL minted, request NOT actually stored: the exact shape a
                # trusted-URL implementation would accept and this one must not.
                self.persisted.get(self._new_url, set()).discard(request_id)
            return result

    client = NoPersistOnRotate(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED, SubmitResult.CONFIRMED],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=5)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.conversation_url == CONV_URL
    declined = transcript_entries(config, "rotation_declined")
    assert declined and declined[-1]["data"]["reason_code"] == "rotation_failed"
    # The request is still the OLD conversation's, unchanged. In particular its
    # prompt must not have been rewritten: a `--resubmit` into the original chat
    # would otherwise send it a note saying that conversation is abandoned.
    req = orch.state.pending_request
    assert req.conversation_url == CONV_URL
    assert req.conversation_epoch == 0
    assert CONTINUATION_NOTE not in req.prompt
    import hashlib

    assert req.prompt_sha256 == hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()


def test_a_failed_rotation_still_spends_the_budget(tmp_path):
    """A rotation SENDS. If it fails after that send, autoloop must not be able
    to open a second chat and post again — so the attempt is charged even when
    it does not complete."""

    class RotateSendExplodes(RotatingFakeClient):
        def submit(self, request_id, prompt):
            if self.conversation_url == PROJECT_URL:
                from autoloop.errors import BrowserError

                raise BrowserError("died right after posting")
            return super().submit(request_id, prompt)

    client = RotateSendExplodes(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=5)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.rotations == 1  # spent, not refunded
    assert orch.state.conversation_url == CONV_URL  # nothing was bound
    assert CONTINUATION_NOTE not in orch.state.pending_request.prompt
    # Reloading and retrying cannot open a second chat: the budget is gone.
    reloaded = StateStore(config.state_file).load()
    assert reloaded.rotations == 1


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


# ---- 6/7. failures that must never rotate ----------------------------------


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
    """A logged-out profile is an account problem; a new chat cannot fix it,
    and rotating would spend the run's single rotation on it."""
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
    BrowserErrors; only ConversationUnusableError authorizes a rotation."""
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


# ---- 8. an unusable conversation rotates ------------------------------------


def test_unusable_conversation_rotates(tmp_path):
    client = RotatingFakeClient(
        attach_errors=[ConversationUnusableError("wedged")],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert orch.state.rotations == 1
    assert orch.state.conversation_url == NEW_CONV_URL
    rotated = transcript_entries(config, "conversation_rotated")
    assert rotated and rotated[0]["data"]["reason"] == "conversation_unusable"
    # The fault is charged to the rotation budget, not also to the failure
    # budget — one fault, one accounting.
    assert orch.state.consecutive_failures == 0


def test_rotation_without_project_url_parks(tmp_path):
    client = RotatingFakeClient(attach_errors=[ConversationUnusableError("wedged")])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state, project_url="")

    orch.run(max_steps=2)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.rotations == 0
    declined = transcript_entries(config, "rotation_declined")
    assert declined and declined[0]["data"]["reason_code"] == "rotation_unavailable"


# ---- 9/10. per-request conversation binding --------------------------------


def test_delayed_response_in_the_abandoned_chat_cannot_authorize(tmp_path):
    """After a rotation the request is bound to the new chat, so the reply that
    gets read is the new chat's — a late answer in the abandoned one is never
    even looked at."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block("from the replacement chat")],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=6)

    assert orch.state.rotations == 1
    assert client.awaited, "the loop never awaited a response"
    # Every await happened against the replacement conversation.
    assert {url for url, _ in client.awaited} == {NEW_CONV_URL}
    assert orch.state.last_response is None or (
        orch.state.last_response.conversation_url == NEW_CONV_URL
    )


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

    with pytest.raises(Exception) as exc:
        orch.run(max_steps=1)
    assert "cannot be attributed" in str(exc.value)


def test_legacy_request_binds_to_the_loop_url_before_any_rotation(tmp_path):
    client = RotatingFakeClient(responses=[stop_block()])
    client.seed(CONV_URL, "alr-legacy-0001")
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending("alr-legacy-0001", url="")
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.pending_request.conversation_url == CONV_URL


# ---- 11. the rotation cap parks --------------------------------------------


def test_rotation_cap_parks(tmp_path):
    client = RotatingFakeClient(attach_errors=[ConversationUnusableError("wedged again")])
    state = LoopState.new(CONV_URL)
    state.rotations = 1  # this run has already used its rotation
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.rotations == 1  # not incremented
    declined = transcript_entries(config, "rotation_declined")
    assert declined and declined[-1]["data"]["reason_code"] == "rotation_cap_reached"
    assert "max_conversation_rotations" in orch.state.question


def test_rotation_budget_is_checkable_without_an_orchestrator():
    engine = PolicyEngine(PolicyConfig(max_conversation_rotations=1))
    assert engine.check_rotation_budget(0).allowed
    assert not engine.check_rotation_budget(1).allowed
    # A cap of 0 disables rotation entirely — the second way to turn it off,
    # equivalent in effect to leaving browser.project_url unset. The check is
    # `>=` rather than `>` precisely so 0 means "never", not "once".
    zero = PolicyEngine(PolicyConfig(max_conversation_rotations=0))
    assert zero.check_rotation_budget(0).allowed is False


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


def test_rotation_heals_the_config(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    config_path = tmp_path / "al-config.toml"
    config_path.write_text(
        CONFIG_TEXT.replace("https://chatgpt.com/c/old", CONV_URL), encoding="utf-8"
    )
    client = RotatingFakeClient(
        attach_errors=[ConversationUnusableError("wedged")], responses=[stop_block()]
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state, config_path=config_path)

    import os

    cwd = os.getcwd()
    os.chdir(repo)
    try:
        orch.run(max_steps=2)
    finally:
        os.chdir(cwd)

    assert orch.state.rotations == 1
    assert f'conversation_url = "{NEW_CONV_URL}"' in config_path.read_text(encoding="utf-8")


def test_drift_guard_accepts_a_recorded_rotation_and_nothing_else(tmp_path):
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


# ---- 14. a restart preserves the binding and the rotation count -------------


def test_restart_preserves_request_url_and_rotation_count(tmp_path):
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)
    orch.run(max_steps=4)  # stops at the rotation, before the reply is consumed
    assert orch.state.rotations == 1

    reloaded = StateStore(config.state_file).load()

    assert reloaded.rotations == 1
    assert reloaded.conversation_url == NEW_CONV_URL
    assert reloaded.conversation_epoch == 1
    assert reloaded.pending_request.conversation_url == NEW_CONV_URL
    assert reloaded.pending_request.conversation_epoch == 1
    assert reloaded.last_rotation["old_url"] == CONV_URL


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


# ---- 15. the replacement chat gets a contract-complete prompt ---------------


def test_new_chat_prompt_is_the_original_plus_one_continuation_line(tmp_path):
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    original_prompt = pending().prompt
    state.pending_request = pending()
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=4)  # stops at the rotation, before the reply is consumed

    rotated_url, rotated_rid, rotated_prompt = client.submitted[-1]
    assert rotated_url == PROJECT_URL  # sent into the project's new chat
    assert rotated_rid == "alr-test-0001"  # same request id, deliberately
    assert original_prompt in rotated_prompt  # contract + CONTEXT block intact
    assert rotated_prompt.endswith(CONTINUATION_NOTE)
    assert rotated_prompt.count(CONTINUATION_NOTE) == 1
    # The re-stamp keeps the integrity check honest for the bytes actually sent.
    import hashlib

    assert orch.state.pending_request.prompt_sha256 == (
        hashlib.sha256(rotated_prompt.encode("utf-8")).hexdigest()
    )


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
