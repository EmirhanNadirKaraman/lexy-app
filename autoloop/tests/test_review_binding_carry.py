"""bind-01: a corrective re-prompt keeps the postcommit binding it is
correcting, and a stamped approval is reconciled against the request it NAMES.

The defect these pin was observed on prof-01, 2026-08-20. A reply to a
postcommit review packet failed to parse; the corrective re-prompt the loop
sent in its place was built with no binding, because a correction's TEXT
carries no candidate identifiers to bind from. The reviewer's fully stamped
`push` then found `last_response.postcommit is None`, fell through to the
retired-legacy-path denial, and a candidate that had passed four review rounds
became APPROVED AND UNPUBLISHABLE — with no supported route back. The reviewer
correctly refused every later packet, each refusal was a `stop`, each `stop`
ended the session, each new session sent a kickoff, and the loop burned a full
round every five minutes with `needs_attention` false throughout.

Two independent mechanisms close it, and both are exercised here:

  * the CARRY — a correction that is still answering an unchanged review packet
    inherits that packet's binding (`_carry_postcommit_binding`);
  * the LEDGER — every request sent carrying a binding is remembered with the
    stamp it published, so an approval naming an earlier request resolves
    against that request rather than against whatever is most recent
    (`_resolve_reviewed_packet`).

Neither widens what a `push` may publish. `test_postcommit_review.py` holds the
end-to-end run() version of the headline case; this file is the mechanism, the
precedence rule and the negatives.

Real git throughout, with the small `run_git`/`gateway`/`WritingExecutor`
helpers duplicated rather than imported — this codebase's convention for
self-contained test files (see `test_postcommit_primitives.py`).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive, ReviewRef
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    MAX_POSTCOMMIT_PACKETS,
    LastResponse,
    LoopState,
    Phase,
    StateStore,
)
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_bare(tmp_path, name="bare.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    """Writes `files` into the worktree for `task.id` and reports success."""

    def __init__(self, worktrees_root, files=None, per_round_files=None):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files or {})
        self.per_round_files = list(per_round_files) if per_round_files else None
        self.calls = 0

    def execute(self, directive, task):
        wt = self.worktrees_root / task.id
        files = (
            self.per_round_files[self.calls]
            if self.per_round_files is not None
            else self.files
        )
        self.calls += 1
        for rel, content in files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary=f"round {self.calls}",
            details="details",
            validation="ok",
            changed_paths=tuple(files.keys()),
        )


def build(tmp_path, executor, task_id="t1", policy=None):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
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
        browser=BrowserConfig(conversation_url=URL),
        policy=policy or PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    derived = set(getattr(executor, "files", {}) or {})
    for round_files in getattr(executor, "per_round_files", None) or ():
        derived |= set(round_files)
    task = Task(
        id=task_id,
        title=f"Title {task_id}",
        description="desc",
        approved_paths=tuple(sorted(derived)),
    )
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
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


def revise(task_id="t1"):
    return Directive(
        decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback="fix it"
    )


def send_review_packet(orch, task_id="t1"):
    """Run one produce-then-review round and put its packet on the wire.

    Returns the `PendingRequest` that carries it — the request a reviewer would
    be answering.
    """
    orch._dispatch_executor(implement(task_id))
    assert orch.state.phase == Phase.READY.value
    orch._step_ready()
    req = orch.state.pending_request
    assert req is not None and req.postcommit is not None
    return req


def answer(orch, req, raw):
    """Persist `raw` as the reply to `req`, exactly as `_step_awaiting` would."""
    orch.state.last_response = LastResponse(
        request_id=req.request_id,
        raw=raw,
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        postcommit=req.postcommit,
        changeset=req.changeset,
    )
    orch.state.pending_request = None
    orch.state.phase = Phase.EXECUTING.value


def push_reply(request_id, head_sha, report_sha256):
    return "Looks good.\n```json\n" + json.dumps(
        {
            "version": 3,
            "decision": "push",
            "reason": "approved",
            "reviewed": {
                "request_id": request_id,
                "head_sha": head_sha,
                "report_sha256": report_sha256,
            },
        }
    ) + "\n```"


def stamp_of(req):
    return ReviewRef(
        request_id=req.request_id,
        head_sha=req.head_sha,
        report_sha256=req.report_sha256,
    )


# =============================================================================
# 1. THE CARRY — a parse error on a postcommit packet keeps the binding
# =============================================================================


def test_a_parse_error_correction_still_carries_the_postcommit_binding(tmp_path):
    orch, _repo, _worktrees, execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)
    original = packet.postcommit

    answer(orch, packet, "unexpected_field: this is not a directive")
    orch._step_executing()

    # the correction is queued, and it is queued BOUND
    assert orch.state.phase == Phase.READY.value
    assert "contract_violation" in orch.state.outbox
    assert orch.state.outbox_postcommit is not None

    orch._step_ready()
    correction = orch.state.pending_request
    assert correction.request_id != packet.request_id
    assert correction.postcommit == original

    # `packet_sha256` is NOT re-stamped to the correction's own digest: it
    # names the packet this binding belongs to, and the correction presented
    # nothing.
    assert correction.postcommit.packet_sha256 == packet.report_sha256
    assert correction.report_sha256 != packet.report_sha256
    # and the carry does not outlive the one correction it was carried for
    assert orch.state.outbox_postcommit is None


def test_the_correction_tells_the_reviewer_which_review_it_is_still_part_of(tmp_path):
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)

    answer(orch, packet, "not a directive")
    orch._step_executing()

    payload = orch.state.outbox
    assert packet.postcommit.candidate_sha[:12] in payload
    assert task.id in payload
    # ...but not the four identifiers that would let the payload bind itself —
    # the binding here is inherited, not re-derived from prose.
    assert packet.postcommit.candidate_sha not in payload


def test_a_parse_error_on_a_non_postcommit_request_behaves_exactly_as_before(tmp_path):
    """The candidate is LIVE on record throughout, deliberately.

    With no execution record at all this passes for the wrong reason — it would
    prove "nothing to bind to" rather than "a payload that presented nothing
    binds nothing". Run the round first and the assertions discriminate: they
    fail if the four-identifier check is relaxed, or if the inherited path ever
    fires without a carry having happened.
    """
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    orch._dispatch_executor(implement(task.id))
    assert orch.state.task_execution is not None
    assert orch.state.task_execution["candidate_sha"]

    orch.state.outbox = "an ordinary audit report, no candidate in sight"
    orch._step_ready()
    req = orch.state.pending_request
    assert req.postcommit is None
    assert orch.state.postcommit_packets == []

    answer(orch, req, "still not a directive")
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert orch.state.outbox_postcommit is None
    assert "SAME review" not in orch.state.outbox
    assert orch.state.postcommit_packets == []

    orch._step_ready()
    assert orch.state.pending_request.postcommit is None


def test_a_correction_does_not_restamp_the_execution_record(tmp_path):
    """The record's `presented_report_sha256` / `review_request_id` point at the
    request that actually PRESENTED the candidate. A correction presented
    nothing, so stamping it there would destroy the binding it describes."""
    orch, _repo, _worktrees, execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)
    before = execution_store.load(task.id)
    assert before.review_request_id == packet.request_id
    assert before.presented_report_sha256 == packet.report_sha256

    answer(orch, packet, "not a directive")
    orch._step_executing()
    orch._step_ready()

    after = execution_store.load(task.id)
    assert after.review_request_id == packet.request_id
    assert after.presented_report_sha256 == packet.report_sha256


def test_a_review_mismatch_correction_carries_the_binding(tmp_path):
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)

    # a stamp that names the right request but the wrong report
    answer(orch, packet, push_reply(packet.request_id, packet.head_sha, "0" * 64))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "review_integrity" in orch.state.outbox
    orch._step_ready()
    assert orch.state.pending_request.postcommit == packet.postcommit


def test_a_policy_denial_correction_carries_the_binding(tmp_path):
    """A denial refuses the DIRECTIVE; it does not change the packet. The
    reviewer is being asked the same question again, so the correction is still
    that review."""
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path,
        WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"}),
        policy=PolicyConfig(implement_enabled=True, allow_push=False),
    )
    packet = send_review_packet(orch, task.id)

    answer(
        orch,
        packet,
        push_reply(packet.request_id, packet.head_sha, packet.report_sha256),
    )
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "push_disabled" in orch._config.transcript_file.read_text(encoding="utf-8")
    orch._step_ready()
    assert orch.state.pending_request.postcommit == packet.postcommit


def test_a_git_failure_correction_deliberately_drops_the_binding(tmp_path):
    """The one correction class that must NOT carry. A git failure means the
    action taken under an approval did not complete, so the state the packet
    described is no longer the repository's state — the next round presents
    what now exists rather than re-offering the old approval."""
    from autoloop.errors import GitCommandError

    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)
    answer(orch, packet, "irrelevant")
    # a stale carry left by an earlier correction must not be inherited here
    orch.state.outbox_postcommit = asdict(packet.postcommit)

    orch._handle_git_failure(Phase.EXECUTING, GitCommandError("push rejected"))

    assert orch.state.phase == Phase.READY.value
    assert "git_failure" in orch.state.outbox
    assert orch.state.outbox_postcommit is None
    orch._step_ready()
    assert orch.state.pending_request.postcommit is None


# =============================================================================
# 2. THE LEDGER — an approval is reconciled against the request it NAMES
# =============================================================================


def test_a_push_naming_an_earlier_packet_publishes_after_last_response_moved_on(tmp_path):
    orch, repo_root, worktrees, execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))
    packet = send_review_packet(orch, task.id)
    candidate = packet.postcommit.candidate_sha
    branch = packet.postcommit.task_branch

    # `last_response` has moved on: a LATER, entirely unbound request is what
    # the loop most recently sent, and its reply is what is in hand.
    orch.state.last_response = LastResponse(
        request_id="alr-later-0099",
        raw=push_reply(packet.request_id, packet.head_sha, packet.report_sha256),
        received_at="now",
        head_sha=orch._git.head_sha(),
        base_sha="",
        report_sha256="f" * 64,
        postcommit=None,
    )
    orch.state.pending_request = None
    orch.state.phase = Phase.EXECUTING.value

    orch._step_executing()

    worktree_git = GitGateway(worktrees.path_for(task.id), PolicyEngine(PolicyConfig()))
    assert worktree_git.remote_ref_sha("origin", f"refs/heads/{branch}") == candidate
    transcript = orch._config.transcript_file.read_text(encoding="utf-8")
    assert "legacy_git_path_retired" not in transcript
    assert "push_missing_review_binding" not in transcript


def test_a_push_naming_a_superseded_packet_is_refused_as_stale(tmp_path):
    """The obvious attack on the new resolution path: reach back past a round
    that has already advanced the candidate. `_dispatch_task_push` already
    contained this — the ledger must not route around it."""
    orch, repo_root, worktrees, execution_store, task = build(
        tmp_path,
        WritingExecutor(
            tmp_path / "worktrees", per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}]
        ),
    )
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))
    round_one = send_review_packet(orch, task.id)
    candidate_a = round_one.postcommit.candidate_sha

    # round 2 advances the candidate and sends its own packet
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id))
    orch._step_ready()
    round_two = orch.state.pending_request
    assert round_two.postcommit.candidate_sha != candidate_a

    # ...and the reviewer stamps ROUND ONE
    answer(
        orch,
        round_two,
        push_reply(round_one.request_id, round_one.head_sha, round_one.report_sha256),
    )
    orch._step_executing()

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "no longer this task's current candidate" in orch.state.question
    worktree_git = GitGateway(worktrees.path_for(task.id), PolicyEngine(PolicyConfig()))
    assert run_git(bare, "for-each-ref").strip() == ""
    assert worktree_git.remote_ref_sha(
        "origin", f"refs/heads/{round_one.postcommit.task_branch}"
    ) == ""


def test_a_stamp_naming_a_request_never_sent_as_a_packet_still_fails_closed(tmp_path):
    """No ledger entry means no expected values to verify against, so the
    ordinary mismatch refusal stands. Nothing is relaxed by the lookup."""
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)

    answer(
        orch,
        packet,
        push_reply("alr-never-0001", packet.head_sha, packet.report_sha256),
    )
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "review_integrity" in orch.state.outbox
    transcript = orch._config.transcript_file.read_text(encoding="utf-8")
    assert "review_mismatch:request_id" in transcript


def test_the_ledger_survives_the_state_file_and_stays_bounded(tmp_path):
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)

    entry = orch._sent_postcommit_packet(packet.request_id)
    assert entry is not None
    assert entry["head_sha"] == packet.head_sha
    assert entry["report_sha256"] == packet.report_sha256
    assert entry["binding"]["candidate_sha"] == packet.postcommit.candidate_sha

    reloaded = StateStore(orch._config.state_file).load()
    assert reloaded.postcommit_packets == orch.state.postcommit_packets
    # plain dicts on both sides of the round trip — never a dataclass that
    # would come back as something else
    assert all(isinstance(p, dict) for p in reloaded.postcommit_packets)

    binding = packet.postcommit
    for index in range(MAX_POSTCOMMIT_PACKETS + 5):
        orch._record_postcommit_packet(f"alr-fill-{index:04d}", "h", "r", binding)
    assert len(orch.state.postcommit_packets) == MAX_POSTCOMMIT_PACKETS
    assert orch._sent_postcommit_packet(packet.request_id) is None  # aged out


def test_re_recording_a_request_replaces_its_entry(tmp_path):
    """`_fall_back_to_omission` legitimately re-stamps a request that has not
    been sent yet; the ledger must agree with the request it describes."""
    orch, _repo, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    packet = send_review_packet(orch, task.id)

    orch._record_postcommit_packet(
        packet.request_id, "newhead", "newreport", packet.postcommit
    )

    matching = [
        p for p in orch.state.postcommit_packets if p["request_id"] == packet.request_id
    ]
    assert len(matching) == 1
    assert matching[0]["report_sha256"] == "newreport"


# =============================================================================
# 3. THE NEGATIVES — nothing here widens what a `push` may publish
# =============================================================================


def test_an_unbound_push_is_still_refused_and_says_what_to_do_next(tmp_path):
    orch, repo_root, _worktrees, _execution_store, task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))
    # a real, unpublished candidate exists — this is the situation in which a
    # dead-end refusal did the damage
    orch._dispatch_executor(implement(task.id))
    assert orch.state.task_execution is not None
    orch.state.last_response = LastResponse(
        request_id="alr-x-0002", raw="{}", received_at="now"
    )

    orch._dispatch(
        Directive(
            decision=Decision.PUSH,
            reason="approved",
            reviewed=ReviewRef("alr-never-0001", "head", "report"),
        )
    )

    assert orch.state.phase == Phase.READY.value  # a correction, not a park
    assert run_git(bare, "for-each-ref").strip() == ""
    transcript = orch._config.transcript_file.read_text(encoding="utf-8")
    assert "push_missing_review_binding" in transcript

    payload = orch.state.outbox
    assert "alr-never-0001" in payload          # why it was refused
    assert "Nothing was pushed" in payload
    assert task.id in payload and "`revise`" in payload   # what to do about it


def test_an_unbound_push_with_no_candidate_at_all_says_so_honestly(tmp_path):
    orch, _repo, _worktrees, _execution_store, _task = build(
        tmp_path, WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    )
    orch.state.last_response = LastResponse(
        request_id="alr-x-0002", raw="{}", received_at="now"
    )

    orch._dispatch(
        Directive(
            decision=Decision.PUSH,
            reason="approved",
            reviewed=ReviewRef("alr-never-0001", "head", "report"),
        )
    )

    assert "nothing this `push` could publish" in orch.state.outbox


def test_the_retired_legacy_decisions_are_still_refused_as_legacy(tmp_path):
    """`commit` and `commit_and_push` are retired (docs/SECURITY.md S21) and
    are caught before the unbound-push branch — they are legacy decisions
    however they are stamped, which is a different fault with a different
    remedy."""
    for decision in (Decision.COMMIT, Decision.COMMIT_AND_PUSH):
        orch, _repo, _worktrees, _execution_store, task = build(
            tmp_path / decision.value,
            WritingExecutor(tmp_path / decision.value / "worktrees", {"a.py": "one\n"}),
        )
        packet = send_review_packet(orch, task.id)
        answer(orch, packet, "irrelevant")

        orch._dispatch(
            Directive(
                decision=decision,
                reason="approved",
                commit_message="ship it",
                commit_paths=("a.py",),
                reviewed=stamp_of(packet),
            )
        )

        transcript = orch._config.transcript_file.read_text(encoding="utf-8")
        assert "legacy_git_path_retired" in transcript
        assert "push_missing_review_binding" not in transcript
        assert "no longer supported" in orch.state.outbox
