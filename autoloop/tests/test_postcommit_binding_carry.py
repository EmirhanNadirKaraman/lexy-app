"""bind-02: a corrective re-prompt keeps the postcommit binding it is correcting,
and a stamped approval may name the packet it reviewed.

THE INCIDENT (prof-01, 2026-08-20). A produce-then-review packet went out as
`alr-683fbfc7-0005`; the reply failed to parse (`unexpected_field`); the loop
sent a corrective re-prompt as `-0006`; the reviewer answered it with a fully
stamped `push`. The correction carried no `postcommit` binding — its payload
contains none of the candidate's identifiers, so `_current_pending_postcommit`
bound it to nothing — so `_dispatch` fell through to `legacy_git_path_retired`
and a candidate that had passed four review rounds and full validation became
APPROVED AND UNPUBLISHABLE. The reviewer then correctly refused every
subsequent packet, each refusal ended the session, each new session sent
another kickoff: three cycles in fifteen minutes with `needs_attention` FALSE
throughout, ended by a human discarding four rounds of approved work.

TWO MECHANISMS, TESTED SEPARATELY BECAUSE THEY FAIL SEPARATELY:

  * THE CARRY (`LoopState.carry_postcommit`). A corrective re-prompt is a
    formatting/decision correction about a packet that has not changed, so it
    inherits the binding of the request it corrects.
  * THE LEDGER (`LoopState.sent_postcommits`). A stamped approval names the
    request it reviewed; if that id is a postcommit packet this loop sent, that
    is better evidence of what was approved than `last_response`, which is only
    the most recent thing sent.

WHAT IS NOT WIDENED, and has its own tests below: an approval that resolves no
binding is still refused, the named packet's three stamps are still demanded
exactly, `commit`/`commit_and_push` are still retired, and every push-time
check in `_dispatch_task_push` still runs.

Real git throughout, with this file's own copies of the small helpers —
see `test_postcommit_primitives.py`'s docstring for why duplication rather
than a shared import is this codebase's convention.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive, ReviewRef
from autoloop.errors import GitError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import MAX_SENT_POSTCOMMIT_RECORDS, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LastResponse, LoopState, Phase, StateStore
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


def worktree_git_for(worktrees: WorktreeManager, task_id: str) -> GitGateway:
    return GitGateway(worktrees.path_for(task_id), PolicyEngine(PolicyConfig()))


def build(tmp_path, executor, task_id="t1", policy=None):
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
        browser=BrowserConfig(conversation_url=URL),
        policy=policy or PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    approved = tuple(sorted(set(getattr(executor, "files", {}) or {})
                            | {p for r in (getattr(executor, "per_round_files", None) or ())
                               for p in r}))
    task = Task(
        id=task_id,
        title=f"Title {task_id}",
        description="desc",
        approved_paths=approved,
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
    return orch, repo_root, worktrees, execution_store, task, config


def implement(task_id="t1"):
    return Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)


def block(obj) -> str:
    return f"Reasoning...\n```json\n{json.dumps(obj)}\n```"


def push_naming(req_or_stamp, **overrides) -> str:
    """A fully stamped `push`, citing the request/head/report of `req_or_stamp`
    (a `PendingRequest` or a dict of the three stamps)."""
    if isinstance(req_or_stamp, dict):
        stamp = dict(req_or_stamp)
    else:
        stamp = {
            "request_id": req_or_stamp.request_id,
            "head_sha": req_or_stamp.head_sha,
            "report_sha256": req_or_stamp.report_sha256,
        }
    stamp.update(overrides)
    return block(
        {"version": 3, "decision": "push", "reason": "approved", "reviewed": stamp}
    )


def deliver(orch, req, raw):
    """What `_step_awaiting` does to a reply, without a transport: persist it as
    the response to `req`, carrying every binding field across, and hand the
    loop to `executing`."""
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


def send_packet(orch):
    """Turn the queued review packet into a sent request."""
    orch._step_ready()
    req = orch.state.pending_request
    assert req is not None
    return req


def with_origin(tmp_path, repo_root):
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))
    return bare


def transcript_of(config) -> str:
    return config.transcript_file.read_text(encoding="utf-8")


# =============================================================================
# 1. THE CLAIM — a parse error on a postcommit packet produces a correction
#    that STILL carries the binding
# =============================================================================


def test_parse_error_correction_keeps_the_postcommit_binding(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    assert packet.postcommit is not None, "precondition: the packet IS bound"

    deliver(orch, packet, "not a json directive at all")
    orch._step_executing()

    # A corrective re-prompt was queued, and the binding was recorded for it.
    assert orch.state.phase == Phase.READY.value
    assert "contract_violation" in (orch.state.outbox or "")
    assert orch.state.carry_postcommit is not None

    correction = send_packet(orch)
    assert correction.request_id != packet.request_id
    # The payload itself carries none of the identifiers — the binding is
    # inherited, not re-derived, which is exactly the point.
    assert packet.postcommit.candidate_sha not in correction.payload
    assert correction.postcommit is not None
    assert correction.postcommit == packet.postcommit
    # ...including the digest of the packet it came from. The correction
    # presents no packet, so claiming its own digest would be a lie.
    assert correction.postcommit.packet_sha256 == packet.report_sha256
    assert correction.report_sha256 != packet.report_sha256
    # Consumed exactly once: nothing is left to attach to a later request.
    assert orch.state.carry_postcommit is None


def test_a_stamped_push_answering_the_correction_publishes(tmp_path):
    """The whole incident, end to end: malformed reply, correction, approval.
    The approval must reach the postcommit publish path and must NOT be refused
    as `legacy_git_path_retired`."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, task, config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    execution = execution_store.load(task.id)
    with_origin(tmp_path, repo_root)

    deliver(orch, packet, "not a json directive at all")
    orch._step_executing()
    correction = send_packet(orch)

    deliver(orch, correction, push_naming(correction))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha
    text = transcript_of(config)
    assert "legacy_git_path_retired" not in text
    assert "task_pushed" in text


def test_the_correction_does_not_restamp_the_presented_report(tmp_path):
    """`presented_report_sha256` / `review_request_id` record which report
    PRESENTED the candidate. A correction presents nothing, so overwriting them
    would replace the record of the review with a record of the apology."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, execution_store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    assert execution_store.load(task.id).presented_report_sha256 == packet.report_sha256

    deliver(orch, packet, "not a json directive at all")
    orch._step_executing()
    correction = send_packet(orch)

    execution = execution_store.load(task.id)
    assert execution.presented_report_sha256 == packet.report_sha256
    assert execution.review_request_id == packet.request_id
    assert execution.review_request_id != correction.request_id


def test_a_second_parse_error_keeps_carrying_the_binding(tmp_path):
    """The chain, not just the first link: a malformed reply TO THE CORRECTION
    produces another correction, which must still be bound. Bounded by the
    parse budget like any other retry — this is not an unbounded loop."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(
        tmp_path, executor, policy=PolicyConfig(implement_enabled=True, max_parse_retries=5)
    )
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    current = packet
    for _ in range(3):
        deliver(orch, current, "still not json")
        orch._step_executing()
        assert orch.state.phase == Phase.READY.value
        current = send_packet(orch)
        assert current.postcommit == packet.postcommit


# =============================================================================
# 2. THE OTHER CORRECTIVE RE-PROMPTS — the same rule, not a special case —
#    and the boundary where the rule deliberately stops
# =============================================================================


def test_a_policy_denial_correction_keeps_the_binding(tmp_path):
    """A denial asks for a different decision about the SAME presented state.
    Driven through a real denial: `push` to a branch the policy protects."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(
        tmp_path,
        executor,
        policy=PolicyConfig(implement_enabled=True, protected_branches=("autoloop/t1",)),
    )
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    assert packet.postcommit.task_branch == "autoloop/t1"

    deliver(orch, packet, push_naming(packet))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "policy_denied" in (orch.state.outbox or "")
    correction = send_packet(orch)
    assert correction.postcommit == packet.postcommit


def test_a_review_mismatch_correction_keeps_the_binding(tmp_path):
    """This correction exists to get a correctly stamped approval back; losing
    the binding would make the loop refuse the very reply it asked for."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    deliver(orch, packet, push_naming(packet, report_sha256="0" * 64))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "review_integrity" in (orch.state.outbox or "")
    correction = send_packet(orch)
    assert correction.postcommit == packet.postcommit


def test_a_git_failure_reprompt_keeps_the_binding(tmp_path):
    """The FIFTH corrective site, found by walking every place that replaces
    `last_response` with a payload asking for another decision rather than by
    the list in the brief. `_handle_git_failure`'s message — "nothing further
    was done, decide how to proceed" — is a policy denial's shape, about a state
    that has not changed."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    deliver(orch, packet, push_naming(packet))
    orch._handle_git_failure(Phase.EXECUTING, GitError("remote exploded"))

    assert orch.state.phase == Phase.READY.value
    assert "git_failure" in (orch.state.outbox or "")
    correction = send_packet(orch)
    assert correction.postcommit == packet.postcommit


def test_an_executor_round_that_reported_back_does_NOT_carry(tmp_path):
    """The boundary of the mechanism, stated as a test. A failed executor round
    sends an `implementation_review` report — a round that RAN, not a re-prompt
    about an unchanged packet — so an approval of that report must not publish
    the candidate from before the revision the reviewer asked for."""

    class FailingExecutor(WritingExecutor):
        def execute(self, directive, task):
            outcome = super().execute(directive, task)
            return ExecutionOutcome(
                status="error",
                summary="round failed",
                details="details",
                validation="no",
                changed_paths=outcome.changed_paths,
            )

    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    assert packet.postcommit is not None

    orch._executor = FailingExecutor(tmp_path / "worktrees", {"a.py": "two\n"})
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(
        Directive(decision=Decision.REVISE, reason="fix", task_id=task.id, feedback="fix it")
    )

    assert orch.state.phase == Phase.READY.value
    assert orch.state.carry_postcommit is None
    report = send_packet(orch)
    assert report.postcommit is None


def test_a_plan_rejection_carries_nothing_because_there_is_nothing_to_carry(tmp_path):
    """Routed through the same helper as the other four rather than exempted.
    It is a no-op by fact, not by exemption: a `plan` reply answers a request
    that never carried a binding, which the helper decides by looking."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, _task, _config = build(tmp_path, executor)
    orch.state.outbox = "kickoff"
    plan_req = send_packet(orch)
    assert plan_req.postcommit is None

    deliver(
        orch,
        plan_req,
        block(
            {
                "version": 3,
                "decision": "plan",
                "reason": "roadmap",
                # `depends_on` names an id that does not exist -> TaskGraphError.
                "tasks": [
                    {
                        "id": "t9",
                        "title": "t9",
                        "description": "d",
                        "depends_on": ["nope"],
                    }
                ],
            }
        ),
    )
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "plan_rejected" in (orch.state.outbox or "")
    assert orch.state.carry_postcommit is None


# =============================================================================
# 3. THE LEDGER — an approval may name the packet it reviewed, even after
#    `last_response` has moved on
# =============================================================================


def _packet_then_unrelated_round(tmp_path, executor, policy=None):
    """Send a review packet, then answer it with something the loop ACTS on
    (an accepted `plan`), so the next request carries no binding and
    `last_response` has genuinely moved past the packet."""
    orch, repo_root, worktrees, execution_store, task, config = build(
        tmp_path, executor, policy=policy
    )
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    deliver(
        orch,
        packet,
        block(
            {
                "version": 3,
                "decision": "plan",
                "reason": "roadmap",
                "tasks": [
                    {
                        "id": "t2",
                        "title": "second",
                        "description": "d",
                        "approved_paths": ["b.py"],
                    }
                ],
            }
        ),
    )
    orch._step_executing()
    assert orch.state.phase == Phase.READY.value
    later = send_packet(orch)
    assert later.postcommit is None, "the follow-up request is genuinely unbound"
    return orch, repo_root, worktrees, execution_store, task, config, packet, later


def test_a_push_naming_a_sent_packet_is_honoured_after_last_response_moved_on(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    (orch, repo_root, worktrees, execution_store, task, config, packet, later) = (
        _packet_then_unrelated_round(tmp_path, executor)
    )
    execution = execution_store.load(task.id)
    with_origin(tmp_path, repo_root)

    # The approval answers `later` but cites the PACKET it reviewed.
    deliver(orch, later, push_naming(packet))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha
    text = transcript_of(config)
    assert "approval_bound_to_named_packet" in text
    assert "legacy_git_path_retired" not in text


def test_naming_a_sent_packet_still_demands_that_packet_s_exact_stamps(tmp_path):
    """NOT a widening. Resolving by the named id changes WHICH request the
    stamps are checked against, never WHETHER they are checked."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    (orch, repo_root, worktrees, execution_store, task, config, packet, later) = (
        _packet_then_unrelated_round(tmp_path, executor)
    )
    with_origin(tmp_path, repo_root)

    deliver(orch, later, push_naming(packet, report_sha256="0" * 64))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "review_integrity" in (orch.state.outbox or "")
    wt_git = worktree_git_for(worktrees, task.id)
    execution = execution_store.load(task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""


def test_naming_a_request_id_the_loop_never_bound_is_refused(tmp_path):
    """The ledger answers "did THIS loop send a postcommit packet under that
    id". An id it never sent resolves nothing, and the approval falls back to
    the response's own (absent) binding — a refusal, never a guess.

    `allow_protected_push` is set so the refusal under test is the one this
    test is about. Without it an unbound `push` is denied EARLIER, by
    `authorize_directive`'s protected-branch gate: `destination_branch` falls
    back to the main checkout's current branch when nothing is bound, and that
    branch is `main`. Still a refusal, but a different one, and asserting on it
    would pass while proving nothing about the ledger.
    """
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    (orch, repo_root, worktrees, execution_store, task, config, packet, later) = (
        _packet_then_unrelated_round(
            tmp_path,
            executor,
            policy=PolicyConfig(implement_enabled=True, allow_protected_push=True),
        )
    )
    with_origin(tmp_path, repo_root)

    deliver(orch, later, push_naming(packet, request_id="alr-nobody-9999"))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "review_integrity" in (orch.state.outbox or "")
    execution = execution_store.load(task.id)
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""


def test_an_unbound_push_is_refused_under_the_default_policy_too(tmp_path):
    """The companion to the test above, without `allow_protected_push`. It is
    refused EARLIER and by a different gate — `destination_branch` falls back to
    the main checkout's branch when nothing is bound, and that is `main` — so
    the property that holds in both configurations is the one asserted here:
    the loop refuses and nothing reaches the remote. Worth pinning because the
    two gates produce different text, and a reader debugging one refusal should
    know the other exists."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    (orch, repo_root, worktrees, execution_store, task, config, packet, later) = (
        _packet_then_unrelated_round(tmp_path, executor)
    )
    with_origin(tmp_path, repo_root)

    deliver(orch, later, push_naming(packet, request_id="alr-nobody-9999"))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    execution = execution_store.load(task.id)
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""
    assert "task_pushed" not in transcript_of(config)


def test_the_ledger_is_bounded_and_evicts_the_oldest(tmp_path):
    """It lives in the state file, which is rewritten every step. Eviction is
    fail-closed: an approval naming an evicted packet resolves nothing."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    first = send_packet(orch)

    binding = first.postcommit
    for index in range(MAX_SENT_POSTCOMMIT_RECORDS + 3):
        orch._record_sent_postcommit(
            orch.state, f"alr-fake-{index:04d}", "head", f"report{index}", binding
        )

    assert len(orch.state.sent_postcommits) == MAX_SENT_POSTCOMMIT_RECORDS
    ids = [record["request_id"] for record in orch.state.sent_postcommits]
    assert first.request_id not in ids, "the oldest entry was evicted"
    assert ids[-1] == f"alr-fake-{MAX_SENT_POSTCOMMIT_RECORDS + 2:04d}"


def test_recording_the_same_request_twice_replaces_rather_than_duplicates(tmp_path):
    """`_fall_back_to_omission` re-stamps an unsent request; the ledger has to
    follow it, not accumulate two entries claiming different digests for one
    id."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    orch._record_sent_postcommit(
        orch.state, packet.request_id, packet.head_sha, "restamped", packet.postcommit
    )
    matching = [
        record
        for record in orch.state.sent_postcommits
        if record["request_id"] == packet.request_id
    ]
    assert len(matching) == 1
    assert matching[0]["report_sha256"] == "restamped"


# =============================================================================
# 4. WHAT IS STILL REFUSED
# =============================================================================


def test_an_unbound_push_with_no_matching_packet_is_still_refused(tmp_path):
    """The retired direct-push route is not reopened. A live, unpublished
    candidate on record does not change that — the binding is what authorizes,
    and there is none."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, task, config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    send_packet(orch)
    with_origin(tmp_path, repo_root)
    assert orch.state.task_execution is not None

    orch.state.last_response = LastResponse(
        request_id="r-unbound", raw="{}", received_at="now"
    )
    orch._dispatch(Directive(decision=Decision.PUSH, reason="approved"))

    assert "legacy_git_path_retired" in transcript_of(config)
    execution = execution_store.load(task.id)
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""


def test_the_unbound_push_refusal_names_the_candidate_and_the_way_forward(tmp_path):
    """The refusal must not dead-end. In the incident the reviewer was told its
    approval was invalid but not what would make one valid, and answered by
    refusing every packet until a human intervened."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, execution_store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    send_packet(orch)
    execution = execution_store.load(task.id)

    orch.state.last_response = LastResponse(
        request_id="r-unbound", raw="{}", received_at="now"
    )
    orch._dispatch(Directive(decision=Decision.PUSH, reason="approved"))

    outbox = orch.state.outbox or ""
    assert task.id in outbox
    assert execution.candidate_sha[:12] in outbox
    assert "revise" in outbox
    assert "postcommit review packet" in outbox


def test_commit_and_push_stays_retired_even_with_a_bound_response(tmp_path):
    """The legacy authorize-then-produce decisions are refused by decision, not
    by whether a binding happens to be available — and the carry does not open a
    route back to them."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, task, config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    with_origin(tmp_path, repo_root)

    stamp = {
        "request_id": packet.request_id,
        "head_sha": packet.head_sha,
        "report_sha256": packet.report_sha256,
    }
    deliver(
        orch,
        packet,
        block(
            {
                "version": 3,
                "decision": "commit_and_push",
                "reason": "approved",
                "commit": {"message": "do it", "paths": ["a.py"]},
                "reviewed": stamp,
            }
        ),
    )
    orch._step_executing()

    assert "legacy_git_path_retired" in transcript_of(config)
    execution = execution_store.load(task.id)
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""


def test_a_second_push_naming_an_already_published_packet_is_refused(tmp_path):
    """Publication does not advance `execution.candidate_sha`, so without
    forgetting the packet a repeat approval would resolve the same binding and
    re-run the completion path for work that already shipped.

    `allow_protected_push` for the same reason as the test two above: once the
    packet is forgotten the second `push` is unbound, and an unbound push is
    otherwise refused earlier on the main checkout's branch rather than on the
    binding this test is about."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    (orch, repo_root, worktrees, execution_store, task, config, packet, later) = (
        _packet_then_unrelated_round(
            tmp_path,
            executor,
            policy=PolicyConfig(implement_enabled=True, allow_protected_push=True),
        )
    )
    with_origin(tmp_path, repo_root)

    deliver(orch, later, push_naming(packet))
    orch._step_executing()
    assert orch.state.phase == Phase.READY.value
    assert orch.state.sent_postcommits == []

    follow_up = send_packet(orch)
    deliver(orch, follow_up, push_naming(packet))
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "review_integrity" in (orch.state.outbox or "")


# =============================================================================
# 5. FAIL-CLOSED EDGES
# =============================================================================


def test_an_unreadable_carry_reads_as_absent_and_says_so(tmp_path):
    """A hand-edited or half-written record must not raise deep inside
    `_step_ready`, where nothing catches it — and must not be honoured
    half-filled either. Absent means the correction binds nothing, which is the
    behaviour that predates the carry."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    deliver(orch, packet, "not a json directive at all")
    orch._step_executing()
    # Corrupt the record between the correction being queued and being built.
    orch.state.carry_postcommit = {"task_id": task.id, "candidate_sha": ""}

    correction = send_packet(orch)

    assert correction.postcommit is None
    assert "postcommit_carry_unusable" in transcript_of(config)


def test_a_carry_whose_candidate_has_moved_on_is_not_applied(tmp_path):
    """A carry is only meaningful while the candidate it names is still this
    session's live, unpublished candidate."""
    executor = WritingExecutor(
        tmp_path / "worktrees",
        per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}],
    )
    orch, _repo, _wt, execution_store, task, config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)
    candidate_a = packet.postcommit.candidate_sha

    deliver(orch, packet, "not a json directive at all")
    orch._step_executing()
    assert orch.state.carry_postcommit is not None

    # A revise round lands while the correction is still queued: the task's
    # current candidate is now B, and the carried binding names A.
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(
        Directive(decision=Decision.REVISE, reason="fix", task_id=task.id, feedback="fix it")
    )
    assert execution_store.load(task.id).candidate_sha != candidate_a
    orch.state.carry_postcommit = {
        "task_id": task.id,
        "task_branch": packet.postcommit.task_branch,
        "base_sha": packet.postcommit.base_sha,
        "candidate_sha": candidate_a,
        "candidate_tree_sha": packet.postcommit.candidate_tree_sha,
        "packet_sha256": packet.report_sha256,
    }

    request = send_packet(orch)

    assert "postcommit_carry_stale" in transcript_of(config)
    # The round-2 packet binds itself in the ordinary way; what must NOT happen
    # is the stale carry re-presenting candidate A as bound.
    assert request.postcommit is None or request.postcommit.candidate_sha != candidate_a


def test_a_ledger_of_the_wrong_shape_reads_as_empty_and_never_raises(tmp_path):
    """`state.sent_postcommits` is read from three call sites, all of them deep
    inside `_step_ready` / `_dispatch` where a `TypeError` ends the process with
    no park and no blocker. `reversed(5)` raises; so does iterating it. Every
    non-list reads as empty and every non-dict entry is dropped, which refuses
    the approval — the fail-closed direction."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(tmp_path, executor)
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    # A `reviewed` stamp naming a DIFFERENT request is what sends `_push_binding`
    # into the ledger at all; without one it returns before ever iterating, and
    # the shape guard would go unexercised while the test still passed.
    naming_the_packet = Directive(
        decision=Decision.PUSH,
        reason="ok",
        reviewed=ReviewRef(
            request_id=packet.request_id,
            head_sha=packet.head_sha,
            report_sha256=packet.report_sha256,
        ),
    )
    for junk in (5, "not a list", {"request_id": "x"}, None):
        orch.state.sent_postcommits = junk
        assert orch._sent_postcommit_records(orch.state) == []
        orch.state.last_response = LastResponse(
            request_id="somewhere-else", raw="{}", received_at="now"
        )
        assert orch._push_binding(naming_the_packet, orch.state.last_response) is None

    orch.state.sent_postcommits = [None, 7, {"request_id": packet.request_id}]
    assert orch._sent_postcommit_records(orch.state) == [
        {"request_id": packet.request_id}
    ]
    # ...and a record with no readable binding resolves nothing rather than
    # resolving something partial.
    orch.state.last_response = LastResponse(
        request_id="somewhere-else", raw="{}", received_at="now"
    )
    assert orch._push_binding(
        Directive(
            decision=Decision.PUSH,
            reason="ok",
            reviewed=ReviewRef(
                request_id=packet.request_id, head_sha="", report_sha256=""
            ),
        ),
        orch.state.last_response,
    ) is None


def test_a_parse_error_on_a_non_postcommit_request_behaves_exactly_as_before(tmp_path):
    """The mechanism must be invisible to every round that has no binding."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, _task, _config = build(tmp_path, executor)
    orch.state.outbox = "kickoff payload"
    first = send_packet(orch)
    assert first.postcommit is None

    deliver(orch, first, "not a json directive at all")
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert orch.state.parse_retries == 1
    assert "contract_violation" in (orch.state.outbox or "")
    assert orch.state.carry_postcommit is None
    assert orch.state.sent_postcommits == []

    correction = send_packet(orch)
    assert correction.postcommit is None


def test_an_exhausted_parse_budget_leaves_no_carry_behind(tmp_path):
    """The budget branch parks instead of sending a correction; a carry left
    for a request that will never be built is how a binding attaches to
    something unrelated."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, _repo, _wt, _store, task, _config = build(
        tmp_path, executor, policy=PolicyConfig(implement_enabled=True, max_parse_retries=1)
    )
    orch._dispatch_executor(implement(task.id))
    packet = send_packet(orch)

    deliver(orch, packet, "not json")
    orch._step_executing()
    assert orch.state.phase == Phase.READY.value
    current = send_packet(orch)

    deliver(orch, current, "still not json")
    orch._step_executing()

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.carry_postcommit is None
