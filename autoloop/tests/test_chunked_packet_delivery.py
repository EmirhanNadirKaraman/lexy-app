"""Delivering an oversized review packet in numbered parts (pkt-01).

Before this, a diff over `packet.DIFF_INCLUDE_MAX_CHARS` was OMITTED with an
honest notice, and the reviewer correctly refused to approve what it could not
see. On 2026-08-05 sub-01 produced a 41 KB patch and the reviewer escalated to
the operator, asking either to raise the cap or to authorize approval unseen.
Neither is good: 41 KB is ABOVE the 40,056-character message that actually
failed to send on 2026-08-04, and approving unseen removes the review.

The measured failure was ONE MESSAGE being too big, not the total volume.
Several smaller messages are fine — so the patch is deposited as numbered
parts, each under the same per-message budget an inline diff has always
respected, and only then is a verdict requested.

The three rules under test here are what make that safe rather than clever:

  1. ALL-OR-NOTHING — no decision is requested until every part is confirmed.
  2. FALL BACK TO OMISSION — a part that does not land sends the omission
     notice, never a half-delivered patch.
  3. THE INTEGRITY BINDING SURVIVES — `report_sha256` covers the COMPLETE
     logical packet, so an approval cannot bind to a subset of the review.

Real git throughout, and the small `run_git`/`WritingExecutor`/`build_postcommit`
helpers are duplicated rather than imported, per this suite's convention (see
`test_postcommit_primitives.py`'s docstring).
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from autoloop import packet as packet_mod
from autoloop.errors import BrowserError, LoginExpiredError, StateError
from autoloop.browser.chatgpt import SubmitResult
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.packet import (
    DIFF_INCLUDE_MAX_CHARS,
    diff_part_id,
    plan_chunked_delivery,
    split_diff_into_parts,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def big_file_text(lines: int = 1200) -> str:
    """A file whose patch comfortably exceeds one message but stays inside
    `DIFF_MAX_PARTS`. ~42 KB — deliberately close to sub-01's real 41 KB
    candidate rather than an arbitrary giant."""
    return "".join(f"line {i:05d} of a large generated source file\n" for i in range(lines))


class WritingExecutor:
    def __init__(self, worktrees_root, files=None):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files or {})
        self.calls = 0

    def execute(self, directive, task):
        wt = self.worktrees_root / task.id
        self.calls += 1
        for rel, content in self.files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary=f"round {self.calls}",
            details="details",
            validation="ok",
            changed_paths=tuple(self.files),
        )


class ChunkingClient:
    """A persistent-conversation transport double.

    `persisted` is server truth and is the ONLY thing `reconcile`/`has_request`
    answer from — a send that is not recorded there is invisible, which is how
    the real client behaves after a generation failure.
    """

    supports_chunked_delivery = True

    def __init__(self, responses=(), refuse: set[str] | None = None):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        #: ids whose send is accepted by the composer but never persisted —
        #: the exact 2026-08-04 failure mode, reproduced.
        self.refuse = set(refuse or ())
        self.send_attempted = False
        self.attached = 0

    def attach(self):
        self.attached += 1

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        self.send_attempted = True
        self.submitted.append((request_id, prompt))
        if request_id in self.refuse:
            return SubmitResult.UNCONFIRMED
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        pass


class NoChunkingClient(ChunkingClient):
    """A transport with no shared history between turns — `codex_cli`'s shape.
    Declares nothing, so the orchestrator must not chunk for it."""

    supports_chunked_delivery = False


def build_postcommit(tmp_path, executor, task_id="t1", client=None, project_url=""):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")

    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL, project_url=project_url),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    task = Task(
        id=task_id,
        title=f"Title {task_id}",
        description="desc",
        approved_paths=tuple(sorted(getattr(executor, "files", {}) or {})),
    )
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=(lambda: client) if client is not None else (lambda: None),
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=ok_validation,
    )
    return orch, repo_root, worktrees, execution_store, task


def implement(task_id="t1"):
    return Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)


def oversized_round(tmp_path, client, files=None, project_url=""):
    """Drive one produce-then-review round whose diff needs chunking, up to the
    point where the parts are planned but not yet delivered."""
    executor = WritingExecutor(tmp_path / "worktrees", files or {"big.py": big_file_text()})
    orch, repo_root, worktrees, execution_store, task = build_postcommit(
        tmp_path, executor, client=client, project_url=project_url
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    return orch, execution_store, task


# =============================================================================
# 1. The common case does not change
# =============================================================================


def test_a_small_diff_is_still_sent_inline_as_one_message(tmp_path):
    """Chunking must cost nothing where fidelity was already cheap: no parts,
    no delivering phase, the patch inside the single message that asks for the
    verdict."""
    client = ChunkingClient()
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, _wt, _store, task = build_postcommit(tmp_path, executor, client=client)

    orch._dispatch_executor(implement(task.id))
    orch._step_ready()

    req = orch.state.pending_request
    assert orch.state.phase == Phase.SUBMITTING.value, "no delivery phase for a small diff"
    assert req.delivery is None
    assert "Full diff:" in req.payload and "OMITTED" not in req.payload
    assert "print('hi')" in req.payload
    assert "print('hi')" in req.prompt, "the patch is in the message that is sent"
    assert "DELIVERED ABOVE IN" not in req.prompt


def test_a_small_diff_leaves_no_delivery_state_behind(tmp_path):
    """`outbox_diff` is the hook the whole mechanism hangs off; it must stay
    `None` for a packet that fits, or every later step has to reason about a
    plan that should not exist."""
    client = ChunkingClient()
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, _wt, _store, task = build_postcommit(tmp_path, executor, client=client)

    orch._dispatch_executor(implement(task.id))

    assert orch.state.outbox_diff is None


# =============================================================================
# 2. An oversized diff arrives as ordered numbered parts
# =============================================================================


def test_an_oversized_diff_is_planned_as_numbered_parts(tmp_path):
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    req = orch.state.pending_request
    assert orch.state.phase == Phase.DELIVERING.value
    assert req.delivery is not None
    parts = req.delivery.parts
    assert len(parts) >= 2, "the fixture must actually need more than one message"
    assert [p["index"] for p in parts] == list(range(1, len(parts) + 1))
    assert all(p["total"] == len(parts) for p in parts)


def test_every_part_stays_under_the_single_message_limit(tmp_path):
    """The whole point. 40,056 characters is the only measured failure; each
    part carries at most `DIFF_INCLUDE_MAX_CHARS` of patch, which is exactly
    what an inline diff has always been allowed to carry."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    for part in orch.state.pending_request.delivery.parts:
        assert len(part["text"]) < 40_056, part["part_id"]
        assert len(_body_of(part["text"])) <= DIFF_INCLUDE_MAX_CHARS, part["part_id"]


def test_the_parts_reproduce_the_patch_byte_for_byte(tmp_path):
    """Nothing is truncated, summarised or re-wrapped: 'is this the whole
    patch?' has to be answerable by concatenation."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    for part in req.delivery.parts:
        assert part["text"].startswith("[autoloop review diff part")
    # The bodies are sliced from the diff inside the hashed payload, so the
    # concatenation has to appear there verbatim.
    diff = "".join(_body_of(part["text"]) for part in req.delivery.parts)
    assert diff in req.payload
    assert len(diff) > DIFF_INCLUDE_MAX_CHARS


def _body_of(part_text: str) -> str:
    """The patch slice of a rendered part.

    `maxsplit=2` on purpose: a part carries a three-line identity block, one
    instruction paragraph, then the patch — and the patch itself contains blank
    lines, so splitting on every blank line would cut it up.
    """
    return part_text.split("\n\n", 2)[2]


def test_the_parts_are_delivered_in_order_before_the_verdict(tmp_path):
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    expected = [p["part_id"] for p in req.delivery.parts]

    orch._step_delivering()

    assert [rid for rid, _ in client.submitted] == expected
    assert orch.state.phase == Phase.SUBMITTING.value
    assert req.delivery.complete


def test_a_part_says_it_is_not_the_question(tmp_path):
    """A part is a deposit. Routed through `build_prompt` it would carry the
    response contract and invite a verdict on a fragment — the exact approval
    the all-or-nothing rule exists to prevent."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    first = orch.state.pending_request.delivery.parts[0]["text"]
    assert "needs no reply" in first
    assert "RESPONSE FORMAT" not in first, (
        "the response contract must not be attached to a part — it is what "
        "tells the reviewer to answer with a directive"
    )


def test_the_verdict_message_names_every_part_id(tmp_path):
    """So the reviewer can check for itself that nothing is missing, rather
    than taking the loop's word for it."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    orch._step_delivering()

    for part in req.delivery.parts:
        assert part["part_id"] in req.prompt
    assert "DELIVERED ABOVE IN" in req.prompt
    assert "OMITTED" not in req.prompt


def test_the_verdict_message_is_small_enough_to_send(tmp_path):
    """The failure being fixed was one oversized message. Splitting the patch
    off would be pointless if the remaining message were still too big."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    assert len(orch.state.pending_request.prompt) < 40_056


# =============================================================================
# 3. All-or-nothing: no verdict until every part is confirmed
# =============================================================================


def test_no_verdict_is_requested_until_every_part_is_confirmed(tmp_path):
    """The transition out of `delivering` happens only after the loop, and the
    verdict message is not among what was sent while parts were outstanding."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    orch._step_delivering()

    part_ids = {p["part_id"] for p in req.delivery.parts}
    assert set(rid for rid, _ in client.submitted) == part_ids
    assert req.request_id not in {rid for rid, _ in client.submitted}


def test_asking_for_a_verdict_after_a_partial_delivery_is_refused(tmp_path):
    """THE mutation test. Rule 1 must not rest on two lines being in the right
    order: `submitting` re-checks the condition itself, so a future edit that
    reorders the transition fails here instead of quietly approving on half a
    patch."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    # One part landed, the rest did not — then something jumps straight to the
    # verdict, which is exactly the bug this refuses.
    req.delivery.delivered = 1
    assert not req.delivery.complete
    orch.state.phase = Phase.SUBMITTING.value

    with pytest.raises(StateError) as exc:
        orch._step_submitting()

    assert "partial patch" in str(exc.value)
    assert client.submitted == [], "nothing may be sent while parts are outstanding"


def test_delivering_without_a_plan_refuses_rather_than_guesses(tmp_path):
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    orch.state.pending_request.delivery = None

    with pytest.raises(StateError):
        orch._step_delivering()


def test_a_confirmed_part_is_not_resent_after_a_crash(tmp_path):
    """The cursor is persisted per part, and a resumed delivery reads back
    rather than re-posting — the same reason `submitting` reconciles before it
    sends."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    first_id = req.delivery.parts[0]["part_id"]

    orch._step_delivering()
    assert req.delivery.complete
    resent_before = len(client.submitted)

    # Re-enter the phase as a crashed process would: the cursor is intact and
    # the parts are in persisted history.
    req.delivery.delivered = 0
    orch.state.phase = Phase.DELIVERING.value
    orch._step_delivering()

    assert len(client.submitted) == resent_before, "a present part must not be re-posted"
    assert first_id in client.persisted
    assert orch.state.phase == Phase.SUBMITTING.value


def test_the_delivery_cursor_is_persisted_not_only_in_memory(tmp_path):
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    orch._step_delivering()

    reloaded = StateStore(orch._store.path).load()
    assert reloaded.phase == Phase.SUBMITTING.value
    assert reloaded.pending_request.delivery.complete
    assert reloaded.pending_request.delivery.part_ids() == [
        p["part_id"] for p in orch.state.pending_request.delivery.parts
    ]


# =============================================================================
# 4. A failed part falls back to the omission notice
# =============================================================================


def test_a_part_that_does_not_land_falls_back_to_omission(tmp_path):
    """Reproduces 2026-08-04 exactly: the composer accepts the message, the
    turn is never persisted. Proceeding on what DID land would leave the
    reviewer holding a fragment it believes is the change."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    client.refuse = {req.delivery.parts[-1]["part_id"]}

    orch._step_delivering()

    assert orch.state.phase == Phase.SUBMITTING.value
    assert req.delivery is None, "no half-plan may survive the fallback"
    assert "Full diff: OMITTED" in req.payload
    assert "Nothing was truncated" in req.payload
    assert "DELIVERED ABOVE IN" not in req.payload


def test_the_fallback_disowns_the_parts_that_already_landed(tmp_path):
    """Rule 2's sharp edge. Parts 1..k are still sitting in the conversation;
    a notice that does not name them leaves the reviewer holding part of a
    patch it believes is whole."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    client.refuse = {req.delivery.parts[-1]["part_id"]}

    orch._step_delivering()

    assert "`autoloop review diff part` messages appear above, IGNORE" in req.payload
    assert "FRAGMENT" in req.payload
    assert req.payload in req.prompt, "the notice must be in the message actually sent"


def test_the_fallback_never_sends_a_partial_patch_as_the_verdict(tmp_path):
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    client.refuse = {req.delivery.parts[-1]["part_id"]}

    orch._step_delivering()

    assert "line 01199 of a large generated source file" not in req.prompt


def test_a_provider_without_shared_history_never_chunks(tmp_path):
    """`codex_cli` runs a fresh process per turn, so parts sent to it would be
    separate reviews of fragments. Falling back is the pre-chunking behaviour
    for that provider, not a regression."""
    client = NoChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    orch._step_delivering()

    assert client.submitted == [], "not one part may be sent to it"
    assert orch.state.phase == Phase.SUBMITTING.value
    assert "Full diff: OMITTED" in orch.state.pending_request.payload


def test_a_patch_needing_too_many_parts_is_omitted_not_chunked():
    """A bound on the mechanism itself: past `DIFF_MAX_PARTS`, 'reply `revise`
    asking for a smaller commit' beats a dozen chat messages."""
    diff = "x" * 200 + "\n"
    payload = "header\n\nFull diff:\n" + diff

    plan = plan_chunked_delivery(
        payload, diff, "alr-abc12345-0007", task_id="t1", candidate_sha="a" * 40,
        max_chars=20, max_parts=3,
    )

    assert plan is None


def test_a_patch_that_does_not_match_its_payload_is_not_chunked():
    """The drift guard. A stored patch that is not inside the payload is not
    'close enough' — it is a different packet, and delivering slices of it
    would show the reviewer something nobody hashed."""
    plan = plan_chunked_delivery(
        "header\n\nFull diff:\n" + "a" * 40_000,
        "b" * 40_000,
        "alr-abc12345-0007",
        task_id="t1",
        candidate_sha="a" * 40,
    )

    assert plan is None


def test_a_rotation_gives_up_the_parts_rather_than_pointing_at_a_chat_without_them(tmp_path):
    """The parts live in the conversation being abandoned. A rotation carries
    only the verdict message, which would name part ids the replacement chat
    does not contain — a reviewer asked to decide on a patch that is not
    there."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(
        tmp_path, client, project_url="https://chatgpt.com/g/g-p-abc/project"
    )
    req = orch.state.pending_request
    orch._step_delivering()
    assert req.delivery.complete

    def rotation_fails(_req, _project_url):
        raise BrowserError("the replacement chat never loaded")

    orch._rotate_conversation = rotation_fails
    orch._attempt_rotation(req, reason="conversation_unusable")

    assert req.delivery is None
    assert "Full diff: OMITTED" in req.payload
    assert "`autoloop review diff part` messages appear above, IGNORE" in req.payload
    assert hashlib.sha256(req.payload.encode("utf-8")).hexdigest() == req.report_sha256


def test_a_rotation_refused_before_it_sends_anything_keeps_the_delivery(tmp_path):
    """The other side of the same rule: a rotation the preconditions refuse
    posts nothing, so the old conversation still holds the whole patch and
    there is nothing to give up."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)   # project_url unset
    req = orch.state.pending_request
    orch._step_delivering()

    orch._attempt_rotation(req, reason="conversation_unusable")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert req.delivery is not None and req.delivery.complete
    assert "DELIVERED ABOVE IN" in req.prompt


# =============================================================================
# 5. The integrity binding still covers the WHOLE logical packet
# =============================================================================


def test_the_hash_covers_the_complete_packet_not_the_sent_message(tmp_path):
    """Rule 3. `report_sha256` is what an approval must echo
    (`contract.verify_review`); if it covered only the abridged message, an
    approval could bind to a review of less than the whole change."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    assert hashlib.sha256(req.payload.encode("utf-8")).hexdigest() == req.report_sha256
    # The discriminating pair: the last line of the patch is inside the hashed
    # payload and NOT inside the message that asks for the verdict.
    last_line = "line 01199 of a large generated source file"
    assert last_line in req.payload
    assert last_line not in req.prompt


def test_every_part_and_the_verdict_belong_to_the_same_request(tmp_path):
    """Rule 3's other half: a part that could not be tied to this request
    would let an approval bind to a review it was not part of."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    stem = req.request_id.replace("-", "_")

    for part in req.delivery.parts:
        assert stem in part["part_id"], "a part must be traceable to its request"
    assert req.request_id in req.prompt, "the verdict message carries the request id"


def test_a_part_id_is_not_mistaken_for_the_request_itself(tmp_path):
    """The defect that would silently kill the feature. Every provider answers
    'did this land?' with a substring search over user messages, so a part
    carrying the request id verbatim makes `submitting`'s pre-send
    reconciliation match part 1, conclude the verdict was already sent, and
    wait forever for a reply to a question nobody asked."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    orch._step_delivering()

    assert req.delivery.complete
    # Behaviour, not string format: with every part landed and the verdict
    # message not yet sent, the transport must still say the REQUEST is absent.
    assert client.reconcile(req.request_id) is False
    assert not any(req.request_id in pid for pid in req.delivery.part_ids())


def test_the_postcommit_binding_carries_the_whole_packet_digest(tmp_path):
    client = ChunkingClient()
    orch, execution_store, task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    assert req.postcommit is not None
    assert req.postcommit.packet_sha256 == req.report_sha256
    assert execution_store.load(task.id).presented_report_sha256 == req.report_sha256


def test_the_fallback_restamps_every_holder_of_the_digest(tmp_path):
    """Swapping the payload changes `report_sha256`, and that digest is held in
    three places that must agree — miss one and a legitimate approval is
    refused at push time, long after the mistake and with nothing on screen
    explaining it."""
    client = ChunkingClient()
    orch, execution_store, task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    before = req.report_sha256
    client.refuse = {req.delivery.parts[-1]["part_id"]}

    orch._step_delivering()

    assert req.report_sha256 != before, "the packet changed; so must its digest"
    assert hashlib.sha256(req.payload.encode("utf-8")).hexdigest() == req.report_sha256
    assert req.postcommit.packet_sha256 == req.report_sha256
    assert execution_store.load(task.id).presented_report_sha256 == req.report_sha256
    assert req.report_sha256 in req.prompt, "CONTEXT must show the digest to echo"


def test_the_fallback_keeps_the_prompt_integrity_stamp_truthful(tmp_path):
    """`submitting` refuses to send a prompt whose sha256 does not match its
    recorded one. A fallback that rewrote the prompt without re-stamping would
    park the loop on 'state file may be corrupted'."""
    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    client.refuse = {req.delivery.parts[-1]["part_id"]}

    orch._step_delivering()

    assert hashlib.sha256(req.prompt.encode("utf-8")).hexdigest() == req.prompt_sha256


def test_the_fallback_does_not_spend_another_review_round(tmp_path):
    """The round was spent in `_finish_postcommit`. This is the same round,
    delivered differently — charging it twice would exhaust a two-round budget
    on one review."""
    client = ChunkingClient()
    orch, execution_store, task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    before = execution_store.load(task.id).review_round
    client.refuse = {req.delivery.parts[-1]["part_id"]}

    orch._step_delivering()

    assert execution_store.load(task.id).review_round == before


# =============================================================================
# 6. Readback, and the virtualized message tail
# =============================================================================


def test_the_message_tail_is_mounted_before_a_part_is_called_absent(tmp_path):
    """ChatGPT renders only the newest few turns (docs/AUTOLOOP.md §11), so a
    part several messages back can be persisted and still missing from
    `innerText`. Concluding 'absent' from an unmounted message would throw away
    a complete delivery and replace it with an omission notice."""
    class TailClient(ChunkingClient):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.mounts = 0
            self.reveal_on_mount: set[str] = set()

        def mount_message_tail(self):
            self.mounts += 1
            # Only a message that was actually SENT can be scrolled into view.
            # Revealing one that was never posted would test nothing.
            sent = {rid for rid, _ in self.submitted}
            self.persisted |= self.reveal_on_mount & sent

    client = TailClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request
    hidden = req.delivery.parts[-1]["part_id"]
    client.refuse = {hidden}          # the send never persists, as far as the DOM shows
    client.reveal_on_mount = {hidden}  # ... but scrolling it in finds it

    orch._step_delivering()

    assert client.mounts > 0, "the tail must be mounted before ruling a part absent"
    assert req.delivery is not None and req.delivery.complete
    assert "OMITTED" not in req.payload, "a mounted part must not trigger the fallback"


def test_a_login_expiry_while_mounting_is_routed_not_swallowed(tmp_path):
    """`BrowserError` subclasses are the two faults the loop ROUTES rather than
    retries — a logged-out profile and a wedged chat. Eating one here would
    demote it to 'the part is absent', and the loop would answer a login prompt
    by omitting a diff."""
    class ExpiredTailClient(ChunkingClient):
        def mount_message_tail(self):
            raise LoginExpiredError("the browser profile is logged out")

    client = ExpiredTailClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    with pytest.raises(LoginExpiredError):
        orch._step_delivering()

    assert "Full diff: OMITTED" not in orch.state.pending_request.payload


def test_git_failing_mid_delivery_never_discards_the_request(tmp_path):
    """The generic git-failure route writes a git-error payload and returns to
    `ready`, which OVERWRITES `pending_request` — abandoning a part-delivered
    patch in the conversation with nothing left to disown it. `delivering` is
    treated like `ready` instead: park retryably, keep the cursor."""
    from autoloop.errors import GitError

    client = ChunkingClient()
    orch, _store, _task = oversized_round(tmp_path, client)
    req = orch.state.pending_request

    orch._handle_git_failure(Phase.DELIVERING, GitError("git is unavailable"))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.resume_phase == Phase.DELIVERING.value
    assert orch.state.pending_request is req, "the in-flight request must survive"
    assert orch.state.pending_request.delivery is not None
    assert orch.state.outbox is None, "no payload may be queued over the request"


def test_a_provider_without_the_tail_capability_still_delivers(tmp_path):
    """The probe is optional, like every other transport capability: an adapter
    that cannot scroll reads what is rendered, exactly as before."""
    client = ChunkingClient()
    assert not hasattr(client, "mount_message_tail")
    orch, _store, _task = oversized_round(tmp_path, client)

    orch._step_delivering()

    assert orch.state.pending_request.delivery.complete


def test_a_failing_tail_mount_cannot_fail_a_confirmation(tmp_path):
    """Mounting more history can only ever ADD evidence, so it must never turn
    a present part into an absent one by raising."""
    class ExplodingTailClient(ChunkingClient):
        def mount_message_tail(self):
            raise RuntimeError("scroll container not found")

    client = ExplodingTailClient()
    orch, _store, _task = oversized_round(tmp_path, client)

    orch._step_delivering()

    assert orch.state.phase == Phase.SUBMITTING.value
    assert orch.state.pending_request.delivery.complete


# =============================================================================
# 7. The splitting primitives
# =============================================================================


def test_splitting_is_lossless():
    diff = "".join(f"line {i}\n" for i in range(500))

    parts = split_diff_into_parts(diff, max_chars=300)

    assert "".join(parts) == diff
    assert all(len(p) <= 300 for p in parts)
    assert len(parts) > 1


def test_splitting_prefers_line_boundaries():
    diff = "".join(f"line {i}\n" for i in range(200))

    parts = split_diff_into_parts(diff, max_chars=100)

    assert all(p.endswith("\n") for p in parts)


def test_a_single_overlong_line_is_cut_rather_than_allowed_to_overflow():
    """Losing a line boundary is cosmetic; overflowing the message budget is
    the failure this mechanism exists to avoid."""
    diff = "x" * 500 + "\n"

    parts = split_diff_into_parts(diff, max_chars=100)

    assert "".join(parts) == diff
    assert all(len(p) <= 100 for p in parts)


def test_a_part_id_never_contains_its_request_id():
    for total in (2, 3, 6):
        for index in range(1, total + 1):
            pid = diff_part_id("alr-abc12345-0007", index, total)
            assert "alr-abc12345-0007" not in pid
            assert "0007" in pid and f"{index:02d}of{total:02d}" in pid


def test_part_ids_are_unique_within_a_delivery():
    ids = [diff_part_id("alr-abc12345-0007", i, 6) for i in range(1, 7)]

    assert len(set(ids)) == 6


def test_the_cap_is_not_raised_by_chunking():
    """Chunking is what removes the need to raise `DIFF_INCLUDE_MAX_CHARS`;
    pinned here as well as in `test_the_cap_is_sized_from_evidence_not_instinct`
    because raising it is the tempting shortcut this task exists to avoid."""
    assert packet_mod.DIFF_INCLUDE_MAX_CHARS == 30_000
    assert packet_mod.DIFF_INCLUDE_MAX_CHARS <= 40_056 * 0.8
