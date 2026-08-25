"""Stale state is rebuilt at the current head, not handed to an operator
(halt-03, 2026-08-25).

The one claim under test, stated as the loop has to satisfy it:

    With `autonomy.enabled` on, each of `task_base_behind_head`,
    `push_candidate_stale`, `push_candidate_unresolvable`, `state_inconsistent`,
    `audit_revise_no_record` and `changeset_binding_missing` ARCHIVES the stale
    record it is holding, REBUILDS at the current head and RE-DISPATCHES, with
    no operator step. With the flag off every one of them parks exactly as it
    did before, and the five hard halts are unreachable from any of it.

"Archives the stale record" is exact for the codes whose record is an execution
record, and precise about the rest: what is stale can be a PACKET or an approval
POINTER, and there the rebuild keeps the durable record and rebuilds around it.

**The one mistake this suite is shaped to catch, because two cuts of the feature
made it.** A handler drops the stale record, queues a sentence explaining what
happened, and returns True. The loop moves, so every "did it park?" assertion
passes — but a sentence carries none of the identifiers an approval binds by, so
the NEXT request goes out unbound and the candidate underneath becomes
unpublishable for the rest of the session. That is the park performed rather
than avoided, and it is invisible to any test that stops at the rebuild. So the
two arms that rebuild a review are each tested END TO END —
`ChangesetWiring` and `PostcommitWiring` drive the real refusal site, then the
round the rebuild bought, then a stamped approval, and assert the candidate
landed on the remote. Where a rebuild is genuinely impossible (the record's own
candidate does not resolve) the assertion is the opposite one: archived through
recut-01's path, or parked, and never a request that could not bind.

Four things about the shape of this file, because they are the ways a test suite
for this could look convincing and prove nothing:

* **The task-free half is tested with no task in flight.** Three of the six
  codes carry no task and never could — `state_inconsistent` is the loop's own
  bookkeeping, and `changeset_binding_missing` plus the changeset arm of
  `push_candidate_unresolvable` belong to an operator's changeset, which has no
  roadmap task by construction. halt-02's gate refused to automate anything
  without a task to set aside, so a test that seeded `state.task_execution`
  (as every halt-02 test does) would pass against code that leaves exactly
  those three codes halting the loop. `build(..., in_flight=None)` is the
  fixture that makes that impossible to miss.
* **The archival half runs against real git and a real `WorkerRepoManager`,**
  because the claim is about what is on disk afterwards: an execution record in
  `executions/archive/`, a worker in `quarantine/`, and a task back in the
  queue. Duplicated per this suite's self-contained convention.
* **The refusals are tested as refusals**, one per recut-01 bound. A rebuild
  that archived a published candidate, or one whose verdict was still in
  flight, would satisfy "no operator step" while destroying approvable work —
  that is the failure this feature is one mistake away from.
* **The default-off tests discriminate on what the rebuild CHANGES.** Three of
  them originally keyed on `state.changeset is not None`, which the changeset
  rebuild now preserves on the success path too — so they would have gone
  vacuous and stayed green whatever the flag did. They key on the outbox.

**The second mistake, caught in review of the first fix and pinned here.** A
rebuild that DESTROYS a record must act on what was established, not on what was
merely attempted. The changeset arm asked "does this candidate resolve?" with a
two-value answer, so no gateway, an unreadable repository, an I/O error and a
name git will not parse all read as "gone" — and each of them deleted an
operator's queued review, the one record in this feature that exists nowhere
else afterwards. Seven consecutive tests below hold that line: exactly one of
them drops, and it is the one where `git cat-file -e` itself answered no
(`ABSENT_SHA`); the other six put the arm in a state where the question went
unanswered and assert the review is still there. "It parks" is the correct
outcome for every one of those — the park is the state the loop was already in.

**The third mistake: the SAME fail-open, on the other arm, surviving the fix to
the first.** The task arm asked its worker repository the same question with the
same two-value answer — `read_commit`/`tree_of` in one `try`, any failure read as
"unresolvable" — and its `False` is more consequential than the changeset arm's,
because it archives a live execution record, quarantines the worker AND is the
one thing licensed to bypass `_recut_outstanding_verdict`. So a transient git
failure, a policy refusal, a corrupt object, an I/O error or a worker directory
that had been removed would each have thrown away a candidate an approval still
in flight could publish. It now goes through the same `_commit_presence`, and the
four tests under "the worker-repository presence probe" below are the matrix:
explicit absence archives; exit 128 parks; an OSError parks; an object that IS
there but whose tree will not resolve parks. Each park is asserted on the four
things that must survive it — the record, the worker, the approval pointers and
the outbox — because "it parked" alone would also pass against code that parked
after archiving.

Self-contained per this codebase's convention (see `test_blockers.py`) — the
config/orchestrator helpers are duplicated here rather than imported from
`test_autonomous_recovery.py` or `test_recut.py`.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoloop import cli
from autoloop import orchestrator as orchestrator_module
from autoloop.changeset_review import build_changeset_binding, build_changeset_packet
from autoloop.blockers import (
    AUTONOMOUS_RECOVERIES,
    HARD_HALT_CODES,
    NO_TASK,
    RECOVER_BY_REBUILDING_AT_HEAD,
    STALE_AUDIT_POINTER,
    STALE_EXECUTION_RECORD,
    STALE_PUSH_BINDING,
    STALE_QUEUED_REVIEW,
    STALE_SESSION_ROUND,
    STALE_RECORD_RECOVERIES,
    TRANSPORT_RECOVERIES,
    AutonomousRecovery,
    BlockerStore,
    autonomous_recovery,
)
from autoloop.config import AutoloopConfig, AutonomyConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import StateCorruptError, TaskGraphError
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    AUTONOMOUS_REBUILD_RETIREMENT_REASON,
    CHANGESET_BINDING_FIELDS,
    DISPLACED_OUTBOX_LOG_CHARS,
    MAX_TASK_RECUTS,
    UNRESOLVABLE_CANDIDATE_REBUILD_CAUSE,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.prompts import TEMPLATES
from autoloop.publisher import (
    Publisher,
    provision_publisher_repo,
    read_publisher_url_snapshot,
)
from autoloop.state import (
    LastResponse,
    LoopState,
    PendingRequest,
    Phase,
    PostcommitBinding,
    StateStore,
)
from autoloop.tasks import (
    HOLD_ORIGIN_OPERATOR,
    Task,
    TaskRegistry,
    TaskState,
    TaskStore,
)
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/stale-record-test"

#: The six codes halt-03 names, verbatim from the task. Listed here rather than
#: read off the table so the assertions below compare the table against the
#: SPEC, not against itself.
SPECIFIED_CODES = (
    "task_base_behind_head",
    "push_candidate_stale",
    "push_candidate_unresolvable",
    "state_inconsistent",
    "audit_revise_no_record",
    "changeset_binding_missing",
)

#: The three that can be raised with no task behind them at all. Two of the park
#: sites pass no `task_id` and never could, and the third —
#: `push_candidate_unresolvable` — has two sites, one of each kind.
TASK_FREE_CODES = (
    "state_inconsistent",
    "audit_revise_no_record",
    "changeset_binding_missing",
    "push_candidate_unresolvable",
)


# =============================================================================
# helpers — collaborator-free
# =============================================================================


def make_config(tmp_path, *, enabled=False, max_recovery_attempts=2) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
        autonomy=AutonomyConfig(
            enabled=enabled, max_recovery_attempts=max_recovery_attempts
        ),
    )


def build(tmp_path, *, enabled=True, max_recovery_attempts=2, with_store=True,
          tasks=("t1",), in_flight="t1"):
    """A collaborator-free Orchestrator, enough for every rebuild whose stale
    record is a SESSION POINTER or an approval binding — none of those touch
    git, the execution store or the worker manager.

    `in_flight=None` is the fixture that matters: it is the state the three
    task-free codes are actually raised in, and the one halt-02's gate refused
    to act in.
    """
    config = make_config(
        tmp_path, enabled=enabled, max_recovery_attempts=max_recovery_attempts
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    if in_flight is not None:
        state.task_execution = {"task_id": in_flight}
    store.save(state)
    registry = TaskRegistry([
        Task(id=tid, title=f"Title {tid}", description="d", approved_paths=("a.py",))
        for tid in tasks
    ])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    blocker_store = BlockerStore(config.blockers_dir) if with_store else None
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        blocker_store=blocker_store,
    )
    return orch, config, blocker_store, task_store, registry


def transcript_entries(config, kind) -> list[dict]:
    if not config.transcript_file.exists():
        return []
    out = []
    for line in config.transcript_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == kind:
            out.append(entry.get("data") or {})
    return out


def transcript_types(config) -> list[str]:
    if not config.transcript_file.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def emitted_blocker_codes() -> set[str]:
    """Every string literal reachable as the `code=` argument of a
    blocker-emitting call in `orchestrator.py`.

    A local copy of the same walk `test_autonomous_recovery.py` keeps, and local
    for the same reason: this module asserts REACHABILITY, and a reachability
    check that depends on another test module staying importable is a check an
    unrelated edit can switch off.
    """
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("_to_needs_user", "_to_fault_stop"):
            continue
        for kw in node.keywords:
            if kw.arg != "code":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    codes.add(sub.value)
    return codes


# =============================================================================
# helpers — real git, for the one rebuild that archives a record on disk
# =============================================================================


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path: Path, *, branch="main", name="repo") -> Path:
    repo_root = tmp_path / name
    repo_root.mkdir(parents=True, exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", branch)
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


class RealWiring:
    """Everything `task_base_behind_head`'s rebuild touches, for real: a git
    repository whose head has moved past the recorded base, a live
    `TaskExecution` on disk, a `WorkerRepoManager` with a real quarantine
    directory, and a task the registry holds `in_progress`.

    Built as a class rather than a dataclass so the attribute list reads beside
    the construction that fills it.
    """

    def __init__(self, tmp_path, *, enabled=True, review_round=1, recut_count=0,
                 published_sha="", with_worker=False, status="in_progress"):
        self.repo_root = make_repo(tmp_path)
        self.config = make_config(tmp_path, enabled=enabled)
        self.git = GitGateway(self.repo_root, PolicyEngine(self.config.policy))
        self.base_sha = self.git.head_sha()
        # The head walks forward, which is the whole condition: every other
        # task's completion auto-merges while this one waits.
        (self.repo_root / "OTHER.md").write_text("moved on\n", encoding="utf-8")
        run_git(self.repo_root, "add", "-A")
        run_git(self.repo_root, "commit", "-q", "-m", "another task landed")
        self.head_sha = self.git.head_sha()

        self.worker_repos = WorkerRepoManager(
            tmp_path / "workers", tmp_path / "worker-hooks"
        )
        self.execution_store = TaskExecutionStore(tmp_path / "executions")
        self.task_store = TaskStore(self.config.tasks_file)
        self.registry = TaskRegistry([
            Task(id="t1", title="Title t1", description="d",
                 approved_paths=("a.py",), recut_count=recut_count),
            Task(id="t2", title="Title t2", description="d",
                 approved_paths=("b.py",)),
        ])
        if status == "in_progress":
            self.registry.mark_in_progress("t1")
        self.task_store.save(self.registry)

        worker_path = ""
        if with_worker:
            repo = self.worker_repos.create("t1", self.repo_root, self.base_sha)
            worker_path = str(repo.path)
        self.execution = TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            # Empty by default, which is one of the real refusals
            # `_carry_reviewed_candidate_past` returns ("the record names no
            # worker repository") and therefore a genuine way to reach this park.
            worktree_path=worker_path,
            task_base_sha=self.base_sha,
            candidate_sha=self.base_sha,
            review_round=review_round,
            recut_count=recut_count,
            published_sha=published_sha,
        )
        self.execution_store.save(self.execution)

        self.state = LoopState.new(URL)
        self.state.task_execution = {"task_id": "t1", "candidate_sha": self.base_sha}
        self.state.phase = Phase.EXECUTING.value
        self.store = StateStore(self.config.state_file)
        self.store.save(self.state)
        self.blocker_store = BlockerStore(self.config.blockers_dir)
        self.orch = Orchestrator(
            config=self.config,
            store=self.store,
            state=self.state,
            policy=PolicyEngine(self.config.policy),
            git=self.git,
            executor=None,
            transcript=TranscriptLogger(self.config.transcript_file),
            client_factory=None,
            registry=self.registry,
            task_store=self.task_store,
            manifest_store=ManifestStore(self.config.manifests_dir),
            worker_repos=self.worker_repos,
            execution_store=self.execution_store,
            blocker_store=self.blocker_store,
        )

    def park_as_the_site_does(self):
        """Call `_to_needs_user` with exactly the arguments the
        `task_base_behind_head` site passes — see `_rebase_execution_if_stale`.
        `test_the_park_site_still_passes_the_arguments_this_fixture_replays`
        below is what keeps this honest."""
        self.orch._to_needs_user(
            f"task t1: its recorded base {self.base_sha[:12]} is behind the "
            f"branch head {self.head_sha[:12]}...",
            kind="task_fatal",
            code="task_base_behind_head",
            task_id="t1",
            detail=f"base={self.base_sha} head={self.head_sha} review_round=1",
        )


# =============================================================================
# helpers — a real operator changeset, for the one rebuild that re-renders a
# packet from git objects
# =============================================================================


#: A well-formed 40-hex object name that no repository built here holds. It is
#: the ONE shape `git cat-file -e` answers "no" to (exit 1); a short or
#: malformed name dies with 128 instead, which is not an answer — see
#: `GitGateway.object_exists`. Every test that needs git to have ANSWERED gone
#: uses this, and the tests that need it not to have answered use the others.
ABSENT_SHA = "0" * 39 + "1"


def probing_runner(answers):
    """`subprocess.run` with some git subcommands SCRIPTED and everything else
    real, through `GitGateway`'s own `runner` seam.

    `answers` maps an argument prefix — `("cat-file", "-e")` — to the exit code
    git should report for it. That is the only thing the presence probe reads,
    and the only thing that distinguishes "git says the object is not here"
    (exit 1) from "git could not look" (128: a corrupt object, an I/O error, an
    unreadable repository). Scripted rather than produced by corrupting a real
    repository, because those failures cannot be produced portably and the code
    under test keys on the exit status either way.

    A value that is an EXCEPTION rather than an exit code is raised instead of
    returned, which is the one failure shape no exit code can express: the
    subprocess never ran at all (`OSError` — the working directory is gone, a
    file handle limit, git not on PATH). `GitGateway._git` does not catch it, so
    it surfaces from `object_exists` exactly as it would in production, and
    `_commit_presence` must read it as "unanswered" rather than as "absent".
    """

    def run(argv, **kwargs):
        for prefix, answer in answers.items():
            if tuple(argv[1:1 + len(prefix)]) == tuple(prefix):
                if isinstance(answer, BaseException) or (
                    isinstance(answer, type) and issubclass(answer, BaseException)
                ):
                    raise answer
                return subprocess.CompletedProcess(
                    argv, answer, stdout="", stderr="fatal: scripted git failure"
                )
        return subprocess.run(argv, **kwargs)

    return run


def scripted_worker_gateway(monkeypatch, worker_path, answers):
    """Script the gateway `_rebuild_task_review_at_head` builds for THIS worker
    repository, and leave every other gateway in the process real.

    That handler constructs its own `GitGateway(Path(execution.worktree_path),
    self._policy)` — there is no runner seam to inject through, deliberately, so
    the test reaches the module-level name instead of the class. Narrowed by
    repository root because the archive path that may run afterwards
    (`release_task_to_pending` → `WorkerRepoManager.retire`) touches git too, and
    a process-wide script would be answering for commands this test is not
    about.
    """
    real = orchestrator_module.GitGateway

    def factory(repo_root, policy, *args, **kwargs):
        if Path(repo_root) == Path(worker_path):
            kwargs.setdefault("runner", probing_runner(answers))
        return real(repo_root, policy, *args, **kwargs)

    monkeypatch.setattr(orchestrator_module, "GitGateway", factory)


class ChangesetWiring:
    """An operator changeset queued exactly as `cli._cmd_review_changeset`
    queues one, on a real repository, with a real Publisher behind it.

    Real git and a real publisher because the claim being tested is not "a dict
    survived": it is that the REDISPATCHED request still carries a changeset
    binding and that an approval to it publishes the same candidate with nobody
    intervening. Only the actual `_step_ready` → `_step_executing` path can show
    that, and both ends of it read the repository.

    `branch` is deliberately not `main`: `build_changeset_binding` refuses a
    protected branch, and `PolicyConfig.protected_branches` holds main/master by
    default.

    `with_publisher` DEFAULTS OFF and one test turns it on. Provisioning a
    publisher clones the repository, and only the end-to-end test publishes
    anything — every other test here stops at the rebuild. Paid where it buys
    something, per this suite's own "nothing here can publish" rule.
    """

    def __init__(self, tmp_path, *, enabled=True, with_publisher=False,
                 branch="feature/x", max_recovery_attempts=2,
                 with_blocker_store=True, tasks=()):
        self.repo_root = make_repo(tmp_path, branch=branch)
        self.config = make_config(
            tmp_path, enabled=enabled, max_recovery_attempts=max_recovery_attempts
        )
        self.policy = PolicyEngine(self.config.policy)
        self.git = GitGateway(self.repo_root, self.policy)
        self.base_sha = self.git.head_sha()
        (self.repo_root / "operator.md").write_text("hand-authored\n", encoding="utf-8")
        run_git(self.repo_root, "add", "-A")
        run_git(self.repo_root, "commit", "-q", "-m", "operator's own change")
        self.candidate_sha = self.git.head_sha()

        self.upstream = None
        publisher = None
        publisher_url_snapshot = None
        if with_publisher:
            self.upstream = tmp_path / "bare.git"
            subprocess.run(["git", "init", "-q", "--bare", str(self.upstream)], check=True)
            run_git(self.repo_root, "remote", "add", "origin", str(self.upstream))
            publisher_state_dir = tmp_path / "publisher-state"
            publisher_repo = provision_publisher_repo(
                publisher_state_dir, self.git, "origin"
            )
            publisher = Publisher(publisher_repo, "origin", PolicyEngine(self.config.policy))
            publisher_url_snapshot = read_publisher_url_snapshot(publisher_state_dir)

        self.binding = build_changeset_binding(
            self.git, self.policy, self.base_sha, self.candidate_sha
        )
        self.queued = dataclasses.asdict(self.binding)
        self.state = LoopState.new(URL)
        self.state.changeset = dict(self.queued)
        # THE state this fault is actually raised in: the queued packet always
        # binds (`build_changeset_packet` stamps all four identifiers and
        # `review-changeset` sets no `outbox_diff`), so `changeset_binding_
        # missing` can only be reached once something else has taken the outbox.
        # A corrective re-prompt is the commonest way.
        self.state.outbox = (
            "Your last reply could not be parsed: unexpected field 'notes'. "
            "Re-send the same verdict in the documented shape."
        )
        self.state.phase = Phase.READY.value
        self.store = StateStore(self.config.state_file)
        self.store.save(self.state)
        self.registry = TaskRegistry([
            Task(id=tid, title=f"Title {tid}", description="d",
                 approved_paths=("a.py",))
            for tid in tasks
        ])
        self.task_store = TaskStore(self.config.tasks_file)
        self.task_store.save(self.registry)
        self.blocker_store = (
            BlockerStore(self.config.blockers_dir) if with_blocker_store else None
        )
        self.orch = Orchestrator(
            config=self.config,
            store=self.store,
            state=self.state,
            policy=self.policy,
            git=self.git,
            executor=None,
            transcript=TranscriptLogger(self.config.transcript_file),
            client_factory=None,
            registry=self.registry,
            task_store=self.task_store,
            manifest_store=ManifestStore(self.config.manifests_dir),
            blocker_store=self.blocker_store,
            publisher=publisher,
            publisher_url_snapshot=publisher_url_snapshot,
        )

    def park_as_the_site_does(self, missing="base_sha"):
        """`_step_ready`'s own arguments for `changeset_binding_missing` — see
        that site. `test_the_changeset_park_site_still_passes_the_arguments_this
        _fixture_replays` keeps this honest."""
        self.orch._to_needs_user(
            "a changeset review is queued but its packet does not contain "
            f"{missing} as literal text, so the approval could never be bound to "
            "the candidate. Nothing was sent. Re-queue with `review-changeset` "
            "(its default rendering always includes the four identifiers) or add "
            "them to your --packet body.",
            kind="loop_fatal",
            code="changeset_binding_missing",
            detail=f"candidate={self.candidate_sha[:12]} missing={missing}",
        )

    def stamped_push_reply(self, req) -> str:
        """The literal text a well-behaved approval sends: a `push` echoing
        exactly the three stamps this request carried."""
        return json.dumps({
            "version": 3,
            "decision": "push",
            "reason": "approved the operator changeset",
            "reviewed": {
                "request_id": req.request_id,
                "head_sha": req.head_sha,
                "report_sha256": req.report_sha256,
            },
        })


# =============================================================================
# helpers — a real produce-then-review candidate, for the push arm
# =============================================================================


class PostcommitWiring:
    """A task holding a REAL committed candidate in a REAL worker repository,
    with the execution record, registry entry and (optionally) Publisher the
    push arm's rebuild and the round after it actually read.

    Why this fixture exists rather than the collaborator-free `build()` the push
    tests used to use: `build()` passes no execution store, so the handler could
    never learn which candidate the task holds — and a test written on it could
    only ever assert that some pointers were dropped, which is exactly the half
    remedy that shipped and was refused. Everything below is real because the
    claim is "the next round can be approved and published", and only the real
    `_step_ready` → `_step_executing` path can show that.

    `candidate` picks the shape the EXECUTION RECORD is in, which is what
    decides between the three outcomes:

    * `"commit"` — a real commit on `autoloop/t1`, descended from the recorded
      base. The rebuild re-presents it.
    * `"missing"` — a 40-hex sha nothing resolves. Nothing can be re-presented,
      so the rebuild archives and requeues.
    * `""` — no candidate was ever committed. Same archive path.
    """

    def __init__(self, tmp_path, *, enabled=True, with_publisher=False,
                 candidate="commit", published_sha="", in_registry=True,
                 with_record=True, recut_count=0, status="in_progress"):
        self.repo_root = make_repo(tmp_path)
        self.config = make_config(tmp_path, enabled=enabled)
        self.policy = PolicyEngine(self.config.policy)
        self.git = GitGateway(self.repo_root, self.policy)
        self.base_sha = self.git.head_sha()

        self.upstream = None
        publisher = None
        publisher_url_snapshot = None
        if with_publisher:
            self.upstream = tmp_path / "bare.git"
            subprocess.run(["git", "init", "-q", "--bare", str(self.upstream)], check=True)
            run_git(self.repo_root, "remote", "add", "origin", str(self.upstream))
            publisher_state_dir = tmp_path / "publisher-state"
            publisher_repo = provision_publisher_repo(
                publisher_state_dir, self.git, "origin"
            )
            publisher = Publisher(publisher_repo, "origin", PolicyEngine(self.config.policy))
            publisher_url_snapshot = read_publisher_url_snapshot(publisher_state_dir)

        self.worker_repos = WorkerRepoManager(
            tmp_path / "workers", tmp_path / "worker-hooks"
        )
        worker = self.worker_repos.create("t1", self.repo_root, self.base_sha)
        self.worker_path = Path(worker.path)
        run_git(self.worker_path, "config", "user.email", "test@example.com")
        run_git(self.worker_path, "config", "user.name", "Test")
        run_git(self.worker_path, "config", "commit.gpgsign", "false")
        (self.worker_path / "a.py").write_text("print('the work')\n", encoding="utf-8")
        run_git(self.worker_path, "add", "-A")
        run_git(self.worker_path, "commit", "-q", "-m", "task t1: the work")
        #: The candidate the task ACTUALLY holds.
        self.candidate_sha = run_git(self.worker_path, "rev-parse", "HEAD").strip()
        #: The candidate an APPROVAL names — the one a later round advanced past.
        #: A real commit, so nothing here passes for want of a resolvable object.
        self.approved_sha = self.base_sha
        # `ABSENT_SHA`, never the null oid: since the presence probe is
        # tri-state, "missing" has to mean the shape `git cat-file -e` answers
        # exit 1 to. The all-zeros name is git-special and can die 128 instead,
        # which is not an answer — that would turn this case into a park for a
        # reason having nothing to do with what it is testing.
        self.recorded_sha = {
            "commit": self.candidate_sha,
            "missing": ABSENT_SHA,
            "": "",
        }[candidate]

        self.execution_store = TaskExecutionStore(tmp_path / "executions")
        self.execution = TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path=str(self.worker_path),
            task_base_sha=self.base_sha,
            candidate_sha=self.recorded_sha,
            review_round=1,
            recut_count=recut_count,
            published_sha=published_sha,
        )
        if with_record:
            self.execution_store.save(self.execution)

        self.registry = TaskRegistry([
            Task(id=tid, title=f"Title {tid}", description="d",
                 approved_paths=("a.py",), recut_count=recut_count)
            for tid in (("t1", "t2") if in_registry else ("t2",))
        ])
        if in_registry and status == "in_progress":
            self.registry.mark_in_progress("t1")
        self.task_store = TaskStore(self.config.tasks_file)
        self.task_store.save(self.registry)

        self.state = LoopState.new(URL)
        # STALE on purpose, and it is the pointer `_current_pending_postcommit`
        # binds against — a rebuild that forgot to refresh it from the record
        # would render a correct packet that still binds to nothing.
        self.state.task_execution = {
            "task_id": "t1", "candidate_sha": self.approved_sha
        }
        self.state.sent_postcommits = [
            {"request_id": "alr-old", "head_sha": self.base_sha,
             "report_sha256": "digest",
             "postcommit": {"task_id": "t1", "candidate_sha": self.approved_sha}},
            {"request_id": "alr-other", "head_sha": self.base_sha,
             "report_sha256": "other",
             "postcommit": {"task_id": "t2", "candidate_sha": "keep"}},
        ]
        # PRODUCTION-SHAPED, and it is load-bearing rather than decoration: the
        # record's current candidate was PRESENTED in its own round, so the
        # ledger holds an entry naming it. Without this line the archive route's
        # `_recut_outstanding_verdict` refusal never fires in any test here, and
        # the route would have been unreachable in production for exactly the
        # shape it exists for while the suite stayed green.
        if self.recorded_sha:
            self.state.sent_postcommits.insert(1, {
                "request_id": "alr-presented", "head_sha": self.base_sha,
                "report_sha256": "presented",
                "postcommit": {"task_id": "t1", "candidate_sha": self.recorded_sha},
            })
        self.state.carry_postcommit = {
            "task_id": "t1", "candidate_sha": self.approved_sha
        }
        self.state.phase = Phase.EXECUTING.value
        self.store = StateStore(self.config.state_file)
        self.store.save(self.state)
        self.blocker_store = BlockerStore(self.config.blockers_dir)
        self.orch = Orchestrator(
            config=self.config,
            store=self.store,
            state=self.state,
            policy=self.policy,
            git=self.git,
            executor=None,
            transcript=TranscriptLogger(self.config.transcript_file),
            client_factory=None,
            registry=self.registry,
            task_store=self.task_store,
            manifest_store=ManifestStore(self.config.manifests_dir),
            worker_repos=self.worker_repos,
            execution_store=self.execution_store,
            blocker_store=self.blocker_store,
            publisher=publisher,
            publisher_url_snapshot=publisher_url_snapshot,
        )

    def binding(self, candidate_sha=None) -> PostcommitBinding:
        """The binding an approval carries into `_dispatch_task_push`."""
        return PostcommitBinding(
            task_id="t1",
            task_branch="autoloop/t1",
            base_sha=self.base_sha,
            candidate_sha=candidate_sha or self.approved_sha,
            candidate_tree_sha="tree-as-reviewed",
            packet_sha256="digest",
        )

    def refuse_at_the_real_site(self, candidate_sha=None) -> None:
        """Drive `_dispatch_task_push` ITSELF — no replayed `_to_needs_user`
        call — so the refusal, its code, its `task_id` and the recovery that
        answers it are the production ones. A fixture that replayed the park
        would still pass if the site stopped reaching it."""
        binding = self.binding(candidate_sha)
        resp = LastResponse(
            request_id="alr-old",
            raw=json.dumps({"version": 3, "decision": "push", "reason": "ok"}),
            received_at="now",
            head_sha=self.base_sha,
            base_sha=self.base_sha,
            report_sha256="digest",
            postcommit=binding,
        )
        # Set on the state too, exactly as `_await_response` leaves it before a
        # dispatch — `_recut_outstanding_verdict` reads it from there.
        self.orch.state.last_response = resp
        self.orch._dispatch_task_push(
            Directive(decision=Decision.PUSH, reason="approved"), resp
        )

    def stamped_push_reply(self, req) -> str:
        return json.dumps({
            "version": 3,
            "decision": "push",
            "reason": "approved the re-presented candidate",
            "reviewed": {
                "request_id": req.request_id,
                "head_sha": req.head_sha,
                "report_sha256": req.report_sha256,
            },
        })


# =============================================================================
# 1. the table
# =============================================================================


def test_the_table_names_all_six_codes_and_one_action():
    """The claim's list, checked against the spec rather than against itself.
    ONE action for all six — they differ only in WHICH record is stale, which is
    what `stale_record` names."""
    assert set(STALE_RECORD_RECOVERIES) == set(SPECIFIED_CODES)
    assert len(STALE_RECORD_RECOVERIES) == 6
    for code, entry in STALE_RECORD_RECOVERIES.items():
        assert entry.code == code, "the table is keyed by its own code"
        assert entry.action == RECOVER_BY_REBUILDING_AT_HEAD
        assert entry.stale_record, f"{code} rebuilds without naming a record"
        assert entry.max_attempts == 1
        assert entry.why.strip(), f"{code} automates without saying why"


def test_every_code_names_a_record_the_orchestrator_has_a_handler_for():
    """A record kind with no handler falls into `_autonomous_rebuild`'s
    fail-closed tail and rebuilds nothing — which would read as coverage while
    the loop still parked."""
    handled = {
        STALE_EXECUTION_RECORD,
        STALE_PUSH_BINDING,
        STALE_QUEUED_REVIEW,
        STALE_AUDIT_POINTER,
        STALE_SESSION_ROUND,
    }
    for code, entry in STALE_RECORD_RECOVERIES.items():
        assert entry.stale_record in handled, f"{code} names an unhandled record"


def test_the_two_halves_of_the_table_are_disjoint_and_merge_into_the_one_lookup():
    """`autonomous_recovery` is the ONE lookup, and it is where the hard-halt
    refusal lives. A caller that consulted either half directly would bypass
    it, so the halves must not be reachable as a substitute for the merge."""
    assert set(TRANSPORT_RECOVERIES).isdisjoint(set(STALE_RECORD_RECOVERIES))
    assert set(AUTONOMOUS_RECOVERIES) == (
        set(TRANSPORT_RECOVERIES) | set(STALE_RECORD_RECOVERIES)
    )
    for code, entry in STALE_RECORD_RECOVERIES.items():
        assert autonomous_recovery(code) is entry


def test_every_stale_record_code_is_one_the_orchestrator_can_actually_raise():
    """The rule halt-02 stated and this inherits: a code no live site can raise
    should be REMOVED, not automated. Automating a dead code reads as coverage
    while covering nothing."""
    unreachable = set(STALE_RECORD_RECOVERIES) - emitted_blocker_codes()
    assert not unreachable, f"automated codes nothing emits: {sorted(unreachable)}"


def test_the_five_hard_halts_stay_disjoint_and_refused_after_the_table_grew():
    """halt-02's guarantee, re-asserted against the MERGED table: adding six
    entries must not have brought a hard halt in with them."""
    assert HARD_HALT_CODES.isdisjoint(set(AUTONOMOUS_RECOVERIES))
    assert HARD_HALT_CODES == {
        "checkout_escape_detected",
        "worker_isolation_violation",
        "primary_checkout_dirty",
        "approved_path_symlink_traversal",
        "prompt_integrity_mismatch",
    }
    assert HARD_HALT_CODES <= emitted_blocker_codes()
    for code in HARD_HALT_CODES:
        assert autonomous_recovery(code) is None


@pytest.mark.parametrize("code", sorted(HARD_HALT_CODES))
@pytest.mark.parametrize("site_kind", ["loop_fatal", "task_fatal"])
def test_a_hard_halt_is_never_rebuilt_and_keeps_its_own_classification(
    tmp_path, code, site_kind
):
    """The property is "autonomous mode does not touch it", not "it stops the
    loop": the five do not share one classification today, so the park has to be
    the one the site asked for, whichever that is."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user(f"task t1: {code}", resume_phase=Phase.EXECUTING.value,
                        kind=site_kind, code=code, task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind
    assert blocker_store.load(orch.state.park_blocker_id).kind == site_kind
    assert "autonomous_rebuild" not in transcript_types(config)


# =============================================================================
# 2. DEFAULT OFF — the reversibility half
# =============================================================================


@pytest.mark.parametrize("code", SPECIFIED_CODES)
def test_with_the_flag_off_every_named_code_parks_exactly_as_before(tmp_path, code):
    """`task_base_behind_head` parks `task_fatal` at its own site and the other
    five park `loop_fatal`, so the off position is asserted against the SITE's
    classification rather than against one blanket value — a copy-pasted
    `loop_fatal` here would have been a claim about the sites, and a false one.
    """
    site_kind = "task_fatal" if code == "task_base_behind_head" else "loop_fatal"
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=False)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.changeset = {"candidate_sha": "abc", "base_sha": "def"}
    orch.state.outbox = "the operator's packet"

    orch._to_needs_user("something is stale", kind=site_kind, code=code)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind
    assert orch.state.park_task_id is None
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.code) == (site_kind, NO_TASK, code)
    # And nothing was rebuilt: the stale records are exactly where they were.
    assert orch.state.changeset == {"candidate_sha": "abc", "base_sha": "def"}
    assert orch.state.outbox == "the operator's packet"
    assert "autonomous_rebuild" not in transcript_types(config)


def test_with_the_flag_off_the_execution_record_is_left_on_disk(tmp_path):
    """The half of the off position that matters most: an archival that fired
    with the flag off would be irreversible in the one way a park is not."""
    wiring = RealWiring(tmp_path, enabled=False)

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    assert TaskStore(wiring.config.tasks_file).load().state_of("t1") is (
        TaskState.IN_PROGRESS
    )


def test_with_the_flag_off_the_unbindable_packet_is_left_for_the_operator(tmp_path):
    """The off position for `changeset_binding_missing`, against a REAL queued
    changeset. The parametrized test above covers all six codes with a
    collaborator-free orchestrator, where this one code would refuse for want of
    a git gateway whatever the flag said — so the off position for it is asserted
    here, where the rebuild would otherwise have succeeded."""
    wiring = ChangesetWiring(tmp_path, enabled=False)
    displaced = wiring.state.outbox

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.park_kind == "loop_fatal"
    assert wiring.orch.state.outbox == displaced
    assert wiring.orch.state.changeset == wiring.queued
    assert "autonomous_rebuild" not in transcript_types(wiring.config)


# =============================================================================
# 3. the six rebuilds
# =============================================================================


def test_task_base_behind_head_archives_the_record_and_requeues_at_head(tmp_path):
    """The measured case: 15 parks, 18.5h, median 0.54h. The park's own text
    names the remedy ("archive .autoloop/executions/<task>.json to start fresh
    at the current head"); this asserts the loop performs it."""
    wiring = RealWiring(tmp_path)

    wiring.park_as_the_site_does()

    # It did NOT park.
    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.question is None
    assert wiring.orch.state.park_blocker_id is None
    assert "needs_user" not in transcript_types(wiring.config)

    # ARCHIVED, not deleted: the live record is gone and an archived copy exists
    # under this feature's own label.
    assert wiring.execution_store.load("t1") is None
    archived = list((wiring.execution_store.directory / "archive").glob("t1-*.json"))
    assert len(archived) == 1
    assert AUTONOMOUS_REBUILD_RETIREMENT_REASON in archived[0].name

    # REBUILT AT HEAD: the task is back in the queue, so its next dispatch cuts
    # from the current head with an empty tree.
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.get("t1").recut_count == 1

    # RE-DISPATCHED: an outbox the next `ready` step can actually send.
    assert wiring.orch.state.outbox and "REBUILT AT HEAD" in wiring.orch.state.outbox
    assert wiring.orch.state.task_execution is None
    assert wiring.orch.state.last_response is None

    entries = transcript_entries(wiring.config, "autonomous_rebuild")
    assert len(entries) == 1
    assert entries[0]["stale_record"] == STALE_EXECUTION_RECORD
    assert entries[0]["discarded_base"] == wiring.base_sha
    assert entries[0]["archived_record"] == str(archived[0])

    # The state file agrees with the object: a crash here resumes the rebuild.
    assert StateStore(wiring.config.state_file).load().phase == Phase.READY.value


def test_the_quarantined_worker_and_the_archived_record_name_each_other(tmp_path):
    """`retire_execution`'s one-label rule, inherited rather than re-implemented:
    a human reading either half can find the other."""
    wiring = RealWiring(tmp_path, with_worker=True)

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value
    entries = transcript_entries(wiring.config, "autonomous_rebuild")
    label = entries[0]["label"]
    assert label.startswith(AUTONOMOUS_REBUILD_RETIREMENT_REASON)
    assert label in entries[0]["archived_record"]
    assert label in entries[0]["quarantined_worker"]
    assert Path(entries[0]["quarantined_worker"]).exists()
    assert not wiring.worker_repos.path_for("t1").exists()


def test_push_candidate_stale_rebuilds_a_bindable_review_of_the_current_candidate(
    tmp_path
):
    """The stale thing here is the APPROVAL BINDING — the park says so in its
    own words ("a later round advanced it") — so the execution record beneath it
    is NOT archived: that would discard a live candidate over a stale pointer.

    **But dropping the binding is only half a remedy, and the half that shipped
    first was refused.** A payload that merely explains what happened carries
    none of the four identifiers `_current_pending_postcommit` binds on, so the
    next request goes out unbound and the candidate the task actually holds
    becomes unpublishable for the rest of the session — the park performed
    rather than avoided. This asserts the other half: the record's CURRENT
    candidate is re-presented as a real review packet carrying all four.

    Driven through `_dispatch_task_push` itself, so the refusal, its code and
    its `task_id` are the production ones rather than a replay."""
    wiring = PostcommitWiring(tmp_path)

    wiring.refuse_at_the_real_site()

    orch = wiring.orch
    assert orch.state.phase == Phase.READY.value
    assert orch.state.question is None
    assert orch.state.park_blocker_id is None
    # The record is untouched — this arm archives nothing.
    assert wiring.execution_store.load("t1") is not None
    assert TaskStore(wiring.config.tasks_file).load().state_of("t1") is (
        TaskState.IN_PROGRESS
    )
    # The stale pointers are gone; another task's approval still binds.
    assert [r["request_id"] for r in orch.state.sent_postcommits] == ["alr-other"]
    assert orch.state.carry_postcommit is None
    assert orch.state.last_response is None
    # REBUILT: a real review packet for the candidate the record names, carrying
    # every identifier the next `_step_ready` will bind on.
    assert "STALE APPROVAL BINDING REBUILT" in orch.state.outbox
    for value in ("t1", "autoloop/t1", wiring.base_sha, wiring.candidate_sha):
        assert value in orch.state.outbox
    # And the pointer that packet is bound against was refreshed from the record.
    assert orch.state.task_execution["candidate_sha"] == wiring.candidate_sha
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_PUSH_BINDING
    # BOTH of this task's entries — the one the approval named and the one that
    # presented the current candidate. `_step_ready` records a fresh entry for
    # that candidate on the very next step, so it is unbound for no round at all.
    assert entry["forgotten_packets"] == ["alr-old", "alr-presented"]
    assert entry["rebuilt_candidate"] == wiring.candidate_sha


def test_the_redispatched_task_request_binds_and_its_approval_publishes_that_candidate(
    tmp_path
):
    """THE claim for the push arm, end to end and with nobody intervening: after
    the rebuild the next request carries a POSTCOMMIT BINDING again, and a
    stamped approval to it publishes exactly the candidate the task holds.

    This is the test the previous round did not have, and the one that
    distinguishes a rebuild from a drop. Every assertion below was false against
    the unbound explanatory payload: `req.postcommit` was `None`, so the approval
    resolved no binding and `_dispatch_task_push` refused it.

    `_step_ready` is called by hand because this suite has no transport."""
    wiring = PostcommitWiring(tmp_path, with_publisher=True)

    wiring.refuse_at_the_real_site()  # the real refusal -> the rebuild

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.pending_request is None  # nothing was sent unbound

    wiring.orch._step_ready()  # the round the rebuild bought

    req = wiring.orch.state.pending_request
    assert req is not None and req.postcommit is not None
    assert req.postcommit.candidate_sha == wiring.candidate_sha
    assert req.postcommit.task_branch == "autoloop/t1"

    wiring.orch.state.last_response = LastResponse(
        request_id=req.request_id,
        raw=wiring.stamped_push_reply(req),
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    wiring.orch._step_executing()

    landed = run_git(wiring.upstream, "rev-parse", "refs/heads/autoloop/t1").strip()
    assert landed == wiring.candidate_sha
    assert wiring.execution_store.load("t1").published_sha == wiring.candidate_sha
    # NO OPERATOR STEP: the record was written and never answered.
    blockers = wiring.blocker_store.all_blockers()
    assert [b.code for b in blockers] == ["push_candidate_stale"]
    assert blockers[0].answer is None


def test_push_candidate_unresolvable_on_the_task_arm_archives_and_requeues(
    tmp_path, monkeypatch
):
    """The task arm of the OTHER code, at its own site. Here the approval names
    the candidate the record itself names, and that commit no longer reads — so
    there is nothing to re-present, and dropping the binding would change none of
    the causal stale state. The safe archive/recut path is taken instead.

    The object is made to vanish between the descendant check and the read,
    because that seam is how this refusal is reachable at all (a prune, a
    corrupt object): `is_descendant` RAISES rather than returning False for an
    object git cannot resolve, so a sha that was never there would never get
    this far.

    **Scripted at BOTH probes, and that is the whole point of the rewrite.** The
    earlier version made `read_commit` raise and left the object in place, so it
    proved only "a read failed" — under a tri-state probe `cat-file -e` then
    answers 0 and the correct outcome is a rebuild, not an archive. Archiving is
    authorized here because git ANSWERS exit 1: the object database does not hold
    it. `("cat-file", "commit"): 128` is the prune's own shape (`cat-file commit`
    dies identically for a missing object and a corrupt one), and
    `("cat-file", "-e"): 1` is git's answer to the question that follows."""
    wiring = PostcommitWiring(tmp_path)
    scripted_worker_gateway(
        monkeypatch,
        wiring.worker_path,
        {("cat-file", "commit"): 128, ("cat-file", "-e"): 1},
    )

    wiring.refuse_at_the_real_site(candidate_sha=wiring.candidate_sha)

    orch = wiring.orch
    assert orch.state.phase == Phase.READY.value
    assert orch.state.pending_request is None
    # ARCHIVED through recut-01's path, not dropped and not re-presented.
    assert wiring.execution_store.load("t1") is None
    archived = list((wiring.execution_store.directory / "archive").glob("t1-*.json"))
    assert len(archived) == 1
    assert AUTONOMOUS_REBUILD_RETIREMENT_REASON in archived[0].name
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.get("t1").recut_count == 1
    assert "REBUILT AT HEAD" in orch.state.outbox
    assert UNRESOLVABLE_CANDIDATE_REBUILD_CAUSE in orch.state.outbox
    assert orch.state.task_execution is None
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_EXECUTION_RECORD


# =============================================================================
# 5b. the worker-repository presence probe — what may archive a live record
#
# The task arm's counterpart to the changeset arm's seven tests below, and the
# same fail-open one arm later. Its `False` is the more consequential of the two:
# it archives a live execution record, quarantines the worker AND is the one
# thing licensed to bypass `_recut_outstanding_verdict`, so a candidate an
# approval still in flight could publish is destroyed by it. Exactly one answer
# authorizes that — `git cat-file -e` returning 1 — and the three tests after it
# put the probe in states where the question went UNANSWERED and assert that
# everything the archive would have consumed is still there.
# =============================================================================


def assert_parked_with_everything_intact(wiring, tmp_path, *, reason_fragment):
    """The four things a park must leave standing, asserted together so no test
    here can prove "it parked" while the record was already gone.

    "It parked" alone would also pass against code that archived the record,
    quarantined the worker and THEN parked — which is the failure mode being
    excluded, not a hypothetical: the archival is durable before the payload is
    built, so a park after it is a park with the work destroyed.
    """
    orch = wiring.orch
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.pending_request is None
    # 1. the execution record, unarchived and naming the same candidate.
    record = wiring.execution_store.load("t1")
    assert record is not None
    assert record.candidate_sha == wiring.recorded_sha
    assert not (wiring.execution_store.directory / "archive").exists()
    # 2. the worker repository, where it was and not in quarantine.
    assert wiring.worker_path.exists()
    assert not (tmp_path / "quarantine").exists()
    # 3. the approval bindings — including `alr-presented`, which is what the
    #    outstanding-verdict refusal reads. Its survival IS the protection.
    assert [r["request_id"] for r in orch.state.sent_postcommits] == [
        "alr-old", "alr-presented", "alr-other"
    ]
    assert orch.state.carry_postcommit == {
        "task_id": "t1", "candidate_sha": wiring.approved_sha
    }
    # 4. the task, still in flight and never charged a recut.
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.IN_PROGRESS
    assert reloaded.get("t1").recut_count == 0
    # …and nothing claimed a rebuild happened.
    assert "autonomous_rebuild" not in transcript_types(wiring.config)
    reasons = [
        e["reason"] for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert any(reason_fragment in reason for reason in reasons)


def test_a_probe_that_dies_never_archives_the_tasks_execution_record(
    tmp_path, monkeypatch
):
    """Exit 128 is not an answer. A corrupt object, an unreadable repository, an
    I/O error inside git and a policy refusal all die with it, and every one of
    them used to read as "the candidate is unresolvable" — archiving a live
    record, quarantining its worker and bypassing the outstanding-verdict
    refusal on evidence that was never gathered.

    The ledger entry `alr-presented` is what makes this more than tidiness: the
    record's candidate WAS presented in its own round, so a verdict on it is
    outstanding, and the bypass this park prevents is the bypass of that."""
    wiring = PostcommitWiring(tmp_path)
    wiring.orch.state.outbox = "the packet that was already queued"
    scripted_worker_gateway(
        monkeypatch,
        wiring.worker_path,
        {("cat-file", "commit"): 128, ("cat-file", "-e"): 128},
    )

    wiring.refuse_at_the_real_site(candidate_sha=wiring.candidate_sha)

    assert wiring.orch.state.outbox == "the packet that was already queued"
    assert_parked_with_everything_intact(
        wiring, tmp_path, reason_fragment="did not answer whether candidate"
    )


def test_a_probe_that_cannot_run_at_all_never_archives_the_record(
    tmp_path, monkeypatch
):
    """The failure no exit code can express: the subprocess never ran. A worker
    directory that has been removed under the loop, a file-handle limit, git
    missing from PATH — `subprocess.run` raises `OSError` and `GitGateway` does
    not catch it.

    Its own arm, because `_commit_presence` catches `(GitError, OSError)` in two
    separate places and a fix that only handled `GitError` would leave this one
    escaping out of a park handler — replacing a recoverable park with a crashed
    process, in the one place a second failure has nowhere to go.

    Reached by the `push_candidate_stale` road (the approval names a different
    sha), and that is FORCED rather than chosen. On the unresolvable road
    `_dispatch_task_push` reaches git twice before it can classify anything, and
    neither reach survives a repository that is not there: `_candidate_is_on_
    task_line` calls `is_descendant` unguarded (`orchestrator.py:9277`), which is
    the EARLIEST escape, and one line later `read_commit` is wrapped in `except
    GitCommandError` only, which an `OSError` also passes through. So that road
    cannot be driven with a dead repository at all — the exception leaves the
    site before any recovery is consulted.

    Named here rather than fixed, deliberately, and the distinction matters to
    what this file claims. Both escapes are FAIL-LOUD and PRE-DATE this change:
    the loop dies instead of raising `push_candidate_unresolvable`, so no rebuild
    runs, nothing is archived and no record is destroyed on evidence nobody
    gathered. They therefore weaken the six-code claim in NO direction — a code
    that is never raised triggers no rebuild — which is why closing them is a
    change to the publish-authorization path rather than to this handler, and is
    reported instead of taken. The recovery under test reads the same record
    through the same probe whichever road reaches it, which is what lets this
    test exercise the `OSError` arm of `_commit_presence` honestly."""
    wiring = PostcommitWiring(tmp_path)
    wiring.orch.state.outbox = "the packet that was already queued"
    scripted_worker_gateway(
        monkeypatch,
        wiring.worker_path,
        {
            ("cat-file", "commit"): OSError("worker repository is gone"),
            ("cat-file", "-e"): OSError("worker repository is gone"),
        },
    )

    wiring.refuse_at_the_real_site()

    assert wiring.orch.state.outbox == "the packet that was already queued"
    assert_parked_with_everything_intact(
        wiring, tmp_path, reason_fragment="did not answer whether candidate"
    )


def test_a_candidate_whose_tree_will_not_resolve_parks_rather_than_archiving(
    tmp_path, monkeypatch
):
    """`_commit_presence` answers only about the COMMIT, and deliberately says
    `True` for an object that is there but does not read as one. The binder reads
    the tree as well, so it is probed separately — and its failure REFUSES.

    A tree that will not resolve is an undiagnosed shape, not git reporting the
    candidate absent, and archiving a record over it would be the same fail-open
    wearing different clothes. It parks instead, which is also what keeps the
    unresolvable tree from being discovered later as a raise inside
    `_step_ready`."""
    wiring = PostcommitWiring(tmp_path)
    scripted_worker_gateway(
        monkeypatch, wiring.worker_path, {("rev-parse",): 128}
    )

    wiring.refuse_at_the_real_site(candidate_sha=wiring.approved_sha)

    assert_parked_with_everything_intact(
        wiring, tmp_path, reason_fragment="tree could not be resolved"
    )


def test_only_gits_own_absent_answer_archives_the_record(tmp_path, monkeypatch):
    """The positive half of the same line, stated as the discrimination rather
    than as an outcome: the four probe states above differ ONLY in what git said,
    and exactly one of them destroys anything.

    Written as one test over both answers so the pair cannot drift apart — a
    later edit that made 128 archive would have to delete an assertion here, not
    merely fail to add one."""
    outcomes = {}
    for label, rc in (("absent", 1), ("unanswered", 128)):
        with monkeypatch.context() as patch:
            wiring = PostcommitWiring(tmp_path / label)
            scripted_worker_gateway(
                patch,
                wiring.worker_path,
                {("cat-file", "commit"): 128, ("cat-file", "-e"): rc},
            )
            wiring.refuse_at_the_real_site(candidate_sha=wiring.candidate_sha)
            outcomes[label] = (
                wiring.orch.state.phase,
                wiring.execution_store.load("t1") is None,
            )

    assert outcomes["absent"] == (Phase.READY.value, True)
    assert outcomes["unanswered"] == (Phase.NEEDS_USER.value, False)


@pytest.mark.parametrize("candidate", ["missing", ""])
def test_a_record_that_names_no_resolvable_candidate_is_archived_not_re_presented(
    tmp_path, candidate
):
    """Reached by the OTHER road: the approval is refused as
    `push_candidate_stale` because the record disagrees with it, and the record's
    own candidate then turns out to be unresolvable (`missing`) or absent (`""`).
    Re-presenting is impossible, so the rebuild archives and requeues rather than
    emitting a payload nothing can bind.

    The `missing` case is also what pins the ONE refusal the archive route lets a
    caller switch off. The fixture's ledger holds an entry that PRESENTED that
    candidate — as production always does — so `_recut_outstanding_verdict`
    reports a verdict still in flight and the route would park. It is bypassed
    only on proven non-resolution, because `_dispatch_task_push` would refuse
    that very approval as `push_candidate_unresolvable`: there is no work left
    for the refusal to protect, and keeping it would trade a park for a park."""
    wiring = PostcommitWiring(tmp_path, candidate=candidate)

    wiring.refuse_at_the_real_site()

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.execution_store.load("t1") is None
    assert TaskStore(wiring.config.tasks_file).load().state_of("t1") is TaskState.READY
    assert "REBUILT AT HEAD" in wiring.orch.state.outbox
    assert transcript_entries(wiring.config, "autonomous_rebuild")[0][
        "stale_record"
    ] == STALE_EXECUTION_RECORD


def test_an_already_published_candidate_only_loses_its_binding(tmp_path):
    """One of the two task-arm shapes with nothing to rebuild AND nothing to
    archive (the other is the record-is-gone pair below).
    Re-presenting a published candidate would invite a second push of work that
    already shipped — the double-publish `_forget_sent_postcommits_for_task`
    exists to prevent — and archiving it is what recut-01 refuses (budget-01).
    So the pointer goes, the record stays, and the payload says so."""
    wiring = PostcommitWiring(tmp_path)
    wiring.execution.published_sha = wiring.candidate_sha
    wiring.execution_store.save(wiring.execution)

    wiring.refuse_at_the_real_site()

    orch = wiring.orch
    assert orch.state.phase == Phase.READY.value
    assert wiring.execution_store.load("t1") is not None  # NOT archived
    assert [r["request_id"] for r in orch.state.sent_postcommits] == ["alr-other"]
    assert "STALE APPROVAL BINDING DISCARDED" in orch.state.outbox
    assert wiring.candidate_sha[:12] in orch.state.outbox
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_PUSH_BINDING
    assert entry["published_sha"] == wiring.candidate_sha


def test_a_recordless_binding_for_a_requeued_task_is_dropped_and_the_loop_goes_on(
    tmp_path
):
    """The park's SECOND stated cause — "the execution record is gone" — in the
    shape that actually produces it: a `recut`, a `release` or an earlier rebuild
    archived the record and returned the task to the queue. There is then no
    candidate to publish, nothing to archive, and the stale approval pointer is
    the whole of the stale state, so dropping it IS the complete remedy.

    Parking here (what the first cut of this revision did, reading the record's
    absence alone) would halt the loop over a fault whose cause had already been
    cleared — the opposite of what this feature is for."""
    wiring = PostcommitWiring(tmp_path, with_record=False, status="ready")

    wiring.refuse_at_the_real_site()

    orch = wiring.orch
    assert orch.state.phase == Phase.READY.value
    assert orch.state.park_blocker_id is None
    assert [r["request_id"] for r in orch.state.sent_postcommits] == ["alr-other"]
    assert "STALE APPROVAL BINDING DISCARDED" in orch.state.outbox
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["record_absent"] is True
    assert entry["task_state"] == TaskState.READY.value


def test_a_recordless_binding_for_a_task_still_in_flight_parks(tmp_path):
    """The other half of the same split, and the reason it is a split. A task
    still `in_progress` with no execution record is genuinely unfinishable:
    `health.stranded_fault_rounds` skips an ABSENT record on purpose, so
    `_reconcile_stranded_tasks` will not requeue it either. Nothing the loop can
    do makes that publishable, so it parks with the question it already had."""
    wiring = PostcommitWiring(tmp_path, with_record=False, status="in_progress")

    wiring.refuse_at_the_real_site()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.pending_request is None
    assert wiring.orch.state.outbox is None
    assert "autonomous_rebuild" not in transcript_types(wiring.config)
    reasons = [
        e["reason"]
        for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert any("still in progress with no execution record" in r for r in reasons)


def test_without_an_execution_store_the_push_arm_refuses_rather_than_dispatching(
    tmp_path
):
    """The fail-open this revision closes, in the exact configuration the
    previous round's tests ran in. With no execution store there is no way to
    learn which candidate the task holds, so a "rebuild" could only ever be the
    unbound explanatory payload — and the loop parks instead."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.outbox = "the packet that was already queued"

    orch._to_needs_user("push refused — the reviewed candidate is stale",
                        kind="loop_fatal", code="push_candidate_stale", task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.pending_request is None
    assert orch.state.outbox == "the packet that was already queued"
    assert "autonomous_rebuild" not in transcript_types(config)
    reasons = [e["reason"] for e in transcript_entries(config, "autonomous_rebuild_refused")]
    assert any("execution store" in reason for reason in reasons)


def test_a_task_arm_rebuild_refused_for_want_of_its_task_parks_unbound_free(tmp_path):
    """The other task-arm refusal: the packet renders the task's id and title, so
    a task the registry does not hold cannot be re-presented without inventing
    one. It parks — and, the property that matters, it parks having dispatched
    nothing."""
    wiring = PostcommitWiring(tmp_path, in_registry=False)

    wiring.refuse_at_the_real_site()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.pending_request is None
    assert wiring.orch.state.outbox is None
    assert wiring.execution_store.load("t1") is not None
    reasons = [
        e["reason"]
        for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert any("not in the registry" in reason for reason in reasons)


def test_the_changeset_arm_drops_the_queued_review_when_git_says_the_commit_is_gone(
    tmp_path
):
    """The second producer, which names NO task — `_dispatch_changeset_push`.
    A test that only covered the task arm would pass while the changeset arm
    still halted the loop.

    Dropping is the arm taken when GIT ITSELF reports that its object database
    does not hold the QUEUE ENTRY's own candidate: then no packet could be
    rendered from it and no approval could ever publish it. `ABSENT_SHA` is
    what makes that an answer rather than a guess — a well-formed name `cat-file
    -e` returns 1 for, on a repository that is right there and readable. The six
    tests that follow it put the same arm in a state where that question went
    UNANSWERED, and each asserts the review is still where it was."""
    wiring = ChangesetWiring(tmp_path)
    wiring.state.changeset = {**wiring.queued, "candidate_sha": ABSENT_SHA}

    wiring.orch._to_needs_user(
        "changeset push refused — the candidate no longer resolves",
        kind="loop_fatal", code="push_candidate_unresolvable",
    )

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.changeset is None
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    # The whole queued record survives in the transcript — an operator's
    # changeset must never evaporate with nothing saying so.
    assert entry["discarded_changeset"]["candidate_sha"] == ABSENT_SHA
    assert entry["discarded_changeset"]["dest_ref"] == wiring.queued["dest_ref"]
    # And the line says which question was answered to authorize the deletion.
    assert entry["candidate_absent_per"] == "git cat-file -e"


def test_no_git_gateway_is_not_evidence_that_the_queued_candidate_is_gone(tmp_path):
    """THE fail-open this arm shipped with, in the destructive direction: with
    no gateway to ask, the first cut read "cannot resolve" off its own missing
    collaborator and DELETED an operator's queued review — the one record here
    that exists nowhere else once the state file is rewritten.

    Nobody to ask is the shape of unverifiability, not of absence. The loop
    parks with the review exactly where it was, which is the state it was
    already in."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)  # git=None
    orch.state.phase = Phase.EXECUTING.value
    queued = {"candidate_sha": "c" * 40, "base_sha": "b" * 40,
              "branch": "feature/x", "dest_ref": "refs/heads/feature/x"}
    orch.state.changeset = dict(queued)

    orch._to_needs_user("changeset push refused — the candidate no longer resolves",
                        kind="loop_fatal", code="push_candidate_unresolvable")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.changeset == queued          # UNTOUCHED
    reasons = [
        e["reason"] for e in transcript_entries(config, "autonomous_rebuild_refused")
    ]
    assert reasons and "no git gateway" in reasons[0]
    # Nothing was rebuilt, so nothing may claim to have been.
    assert "autonomous_rebuild" not in transcript_types(config)


def test_a_probe_that_dies_leaves_the_queued_review_where_it_was(tmp_path):
    """A die is not an answer. `cat-file` exits 128 for a corrupt object, an
    I/O error, a policy refusal and a repository it cannot open — the same
    status for four causes, none of which is "this commit was never here". A
    repository having a bad day must cost the loop a park, never an operator's
    queued review."""
    wiring = ChangesetWiring(tmp_path)
    wiring.state.changeset = {**wiring.queued, "candidate_sha": ABSENT_SHA}
    wiring.orch._git = GitGateway(
        wiring.repo_root,
        wiring.policy,
        runner=probing_runner({("cat-file", "commit"): 128, ("cat-file", "-e"): 128}),
    )

    wiring.orch._to_needs_user(
        "changeset push refused — the candidate no longer resolves",
        kind="loop_fatal", code="push_candidate_unresolvable",
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.changeset == {
        **wiring.queued, "candidate_sha": ABSENT_SHA
    }
    reasons = [
        e["reason"]
        for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert reasons and "did not answer" in reasons[0]
    assert "autonomous_rebuild" not in transcript_types(wiring.config)


def test_a_repository_that_is_no_longer_there_parks_rather_than_dropping(tmp_path):
    """The same rule against a REAL environment fault rather than a scripted
    one: the checkout is gone, so every probe raises `OSError` before git is
    even reached. The queued review survives that, because "we could not look"
    is not "it is not there" — and a park handler is the one place a second
    failure has nowhere to go, so it must come back as a refusal rather than as
    an exception out of `_to_needs_user`."""
    wiring = ChangesetWiring(tmp_path)
    queued = {**wiring.queued, "candidate_sha": ABSENT_SHA}
    wiring.state.changeset = dict(queued)
    shutil.rmtree(wiring.repo_root)

    wiring.orch._to_needs_user(
        "changeset push refused — the candidate no longer resolves",
        kind="loop_fatal", code="push_candidate_unresolvable",
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.changeset == queued
    reasons = [
        e["reason"]
        for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert reasons and "did not answer" in reasons[0]


def test_an_object_git_holds_but_cannot_read_as_a_commit_is_kept_not_dropped(tmp_path):
    """PRESENT is the fail-closed reading of an object git holds but will not
    hand back as a commit — a tree named by mistake, a corrupt object, an
    unreadable one. `_commit_presence` answers True on `cat-file -e`, the
    rebuild is attempted, and the render refuses. The record stays for a human
    either way; what must not happen is destroying it over a shape nobody has
    diagnosed."""
    wiring = ChangesetWiring(tmp_path)
    queued = {**wiring.queued, "candidate_sha": ABSENT_SHA}
    wiring.state.changeset = dict(queued)
    wiring.orch._git = GitGateway(
        wiring.repo_root,
        wiring.policy,
        # git says it HOLDS the object and refuses to read it as a commit.
        runner=probing_runner({("cat-file", "commit"): 128, ("cat-file", "-e"): 0}),
    )

    wiring.orch._to_needs_user(
        "changeset push refused — the candidate no longer resolves",
        kind="loop_fatal", code="push_candidate_unresolvable",
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.changeset == queued
    reasons = [
        e["reason"]
        for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert reasons and "could not be re-rendered" in reasons[0]


def test_a_queued_changeset_naming_no_candidate_is_kept_not_dropped(tmp_path):
    """An entry that names no commit has no commit whose absence could be
    established — there is nothing to prove and so nothing may be destroyed on
    it. Same refusal `_rebuild_changeset_packet_at_head` makes for any missing
    identifier, for the same reason: the four come off the stored entry or the
    rebuild declines."""
    wiring = ChangesetWiring(tmp_path)
    queued = {k: v for k, v in wiring.queued.items() if k != "candidate_sha"}
    wiring.state.changeset = dict(queued)

    wiring.orch._to_needs_user(
        "changeset push refused — the candidate no longer resolves",
        kind="loop_fatal", code="push_candidate_unresolvable",
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.changeset == queued
    reasons = [
        e["reason"]
        for e in transcript_entries(wiring.config, "autonomous_rebuild_refused")
    ]
    assert reasons and reasons[0] == "the queued changeset carries no candidate_sha"


def test_a_changeset_entry_that_is_not_a_record_parks_instead_of_raising(tmp_path):
    """A hand-edited state file is the one caller that can put something other
    than a dict here, and `queued.get(...)` on it is an `AttributeError` inside
    a park handler — a crashed process instead of a park. It is also not a thing
    to delete: nothing here can read it well enough to say what it was."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.changeset = "queued by hand, badly"

    orch._to_needs_user("changeset push refused — the candidate no longer resolves",
                        kind="loop_fatal", code="push_candidate_unresolvable")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.changeset == "queued by hand, badly"
    reasons = [
        e["reason"] for e in transcript_entries(config, "autonomous_rebuild_refused")
    ]
    assert reasons and "not a record this can read" in reasons[0]


def test_the_changeset_arm_rebuilds_a_queued_review_whose_candidate_still_resolves(
    tmp_path
):
    """The approval's binding and the operator's queue entry can name different
    commits — the binding is whatever was bound when the packet went out, and
    `review-changeset` may have been run again since. So the ENTRY decides: one
    whose candidate still resolves is a review that is fine standing behind a
    packet that is stale, which is `_rebuild_changeset_packet_at_head`'s case
    exactly. Dropping it on the binding's evidence would destroy a publishable
    operator review."""
    wiring = ChangesetWiring(tmp_path)
    displaced = wiring.state.outbox

    wiring.orch._to_needs_user(
        "changeset push refused — the reviewed candidate no longer resolves",
        kind="loop_fatal", code="push_candidate_unresolvable",
    )

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.changeset == wiring.queued  # PRESERVED
    assert wiring.orch.state.outbox != displaced
    for name in CHANGESET_BINDING_FIELDS:
        assert wiring.queued[name] in wiring.orch.state.outbox
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_QUEUED_REVIEW


def test_changeset_binding_missing_rebuilds_the_packet_and_keeps_the_queue_entry(
    tmp_path
):
    """The one code that halts the loop INDEFINITELY: it is raised inside
    `_step_ready` before anything is sent, so every future round refuses at the
    same line for as long as the payload stands.

    **The stale record is the PACKET, not the queue entry**, and the first cut of
    this change had it the other way round. The queued packet always binds —
    `build_changeset_packet` stamps all four identifiers whatever body it is
    given, and `review-changeset` sets no `outbox_diff`, so `_plan_delivery`
    returns at its first line and cannot rewrite it — so this fault can only be
    raised once something ELSE holds the outbox. Dropping the entry and keeping
    that payload sent it unbound and left the operator's candidate unpublishable
    for the rest of the session: the review discarded, not rebuilt."""
    wiring = ChangesetWiring(tmp_path)
    displaced = wiring.state.outbox

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.question is None
    assert wiring.orch.state.park_blocker_id is None
    # PRESERVED, byte for byte: the operator's review is still queued.
    assert wiring.orch.state.changeset == wiring.queued
    # REBUILT: the payload is now exactly what `review-changeset` would have
    # rendered for this binding at this head, so all four identifiers are back.
    expected = TEMPLATES["changeset_review"].render(
        branch=wiring.binding.branch,
        dest_ref=wiring.binding.dest_ref,
        packet=build_changeset_packet(wiring.git, wiring.binding),
    )
    assert wiring.orch.state.outbox == expected
    assert wiring.orch.state.outbox != displaced
    for name in CHANGESET_BINDING_FIELDS:
        assert wiring.queued[name] in wiring.orch.state.outbox
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_QUEUED_REVIEW
    assert entry["rebound_changeset"]["candidate_sha"] == wiring.candidate_sha
    # The displaced payload is identified rather than copied.
    assert entry["displaced_outbox_chars"] == len(displaced)
    assert entry["displaced_outbox_head"] == displaced[:DISPLACED_OUTBOX_LOG_CHARS]


def test_the_redispatched_request_binds_and_its_approval_publishes_that_candidate(
    tmp_path
):
    """THE claim, end to end and with nobody intervening: after the rebuild the
    next request carries a changeset binding again, and a stamped approval to it
    publishes exactly the candidate the operator queued.

    A rebuild that merely left a dict in `state.changeset` would satisfy every
    assertion in the test above and still be useless — the binding is only real
    if `_current_pending_changeset` can take it off the payload and
    `_dispatch_changeset_push` can act on it.

    `_step_ready` is called by hand twice, rather than driving `run()`, because
    this suite has no transport: the first call raises the fault from its own
    site and the rebuild answers it, the second is the round that rebuild made
    possible. `run()` would step the same two phases and then try to submit.

    One of the two tests in this file that provision a Publisher, because they
    are the two that publish — this one for the changeset arm, and
    `test_the_redispatched_task_request_binds_and_its_approval_publishes_that_
    candidate` for the push arm."""
    wiring = ChangesetWiring(tmp_path, with_publisher=True)

    wiring.orch._step_ready()  # raises `changeset_binding_missing` -> rebuild

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.pending_request is None  # nothing was sent unbound
    assert wiring.orch.state.changeset == wiring.queued

    wiring.orch._step_ready()  # the round the rebuild bought

    req = wiring.orch.state.pending_request
    assert req is not None and req.changeset is not None
    assert req.changeset.candidate_sha == wiring.candidate_sha
    assert req.changeset.dest_ref == wiring.binding.dest_ref

    wiring.orch.state.last_response = LastResponse(
        request_id=req.request_id,
        raw=wiring.stamped_push_reply(req),
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        changeset=req.changeset,
    )
    wiring.orch._step_executing()

    assert wiring.orch.state.phase == Phase.READY.value
    landed = run_git(wiring.upstream, "rev-parse", wiring.binding.dest_ref).strip()
    assert landed == wiring.candidate_sha
    assert wiring.orch.state.changeset is None  # cleared by the publish itself
    # NO OPERATOR STEP: the record was written and never answered.
    blockers = wiring.blocker_store.all_blockers()
    assert [b.code for b in blockers] == ["changeset_binding_missing"]
    assert blockers[0].answer is None and blockers[0].resolved_at is None


def test_a_queued_changeset_missing_an_identifier_is_refused_not_guessed(tmp_path):
    """The fail-open this must never take: `build_changeset_binding` would happily
    supply a `branch`/`dest_ref` by reading `git.current_branch()`, so a checkout
    that has since moved would rebind the operator's candidate to a destination
    they never named. The four identifiers come off the stored entry or the
    rebuild refuses."""
    wiring = ChangesetWiring(tmp_path)
    wiring.state.changeset = {
        key: value for key, value in wiring.queued.items() if key != "dest_ref"
    }

    wiring.park_as_the_site_does(missing="dest_ref")

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.park_kind == "loop_fatal"
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and reasons[0]["reason"] == "the queued changeset carries no dest_ref"
    assert "autonomous_rebuild" not in transcript_types(wiring.config)


def test_a_candidate_that_no_longer_renders_parks_instead_of_raising(tmp_path):
    """A park handler is the one place a second failure has nowhere to go. A
    candidate sha that resolves to nothing in this repository must come back as a
    refusal, not as an exception out of `_to_needs_user`."""
    wiring = ChangesetWiring(tmp_path)
    gone = "0" * 39 + "1"
    wiring.state.changeset = {**wiring.queued, "candidate_sha": gone}

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "could not be re-rendered" in reasons[0]["reason"]
    # And the operator's queued record is still there to be repaired by hand.
    assert wiring.orch.state.changeset["candidate_sha"] == gone


def test_without_a_git_gateway_the_packet_is_not_invented(tmp_path):
    """A packet rendered from anything but the repository would be a review
    request the reviewer cannot see the change in — and an approval to it would
    publish a candidate nobody was shown."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)  # git=None
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "a corrective re-prompt"
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "feature/x", "dest_ref": "refs/heads/feature/x"}

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.outbox == "a corrective re-prompt"
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "no git gateway" in reasons[0]["reason"]


def test_the_rebuilt_packet_does_not_inherit_the_displaced_packets_attachment(
    tmp_path
):
    """`_step_ready` writes `outbox_attachment` near the top of the step and moves
    it onto the request at the bottom; every rebuild here parks in between. A path
    left in state would be attached to the NEXT request — one change's diff
    presented as another's, under a `report_sha256` that does not cover it."""
    wiring = ChangesetWiring(tmp_path)
    wiring.state.outbox_diff = "diff --git a/x b/x\n"
    wiring.state.outbox_attachment = str(tmp_path / "review-diff.patch")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.outbox_attachment is None
    assert wiring.orch.state.outbox_diff is None


def test_the_changeset_park_site_still_passes_the_arguments_this_fixture_replays(
    tmp_path
):
    """`ChangesetWiring.park_as_the_site_does` replays `_step_ready`'s call. If
    that site is ever re-classified, given a `task_id`, or made resumable, these
    tests would go on passing against arguments nothing raises — so the site is
    read here rather than trusted."""
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "attr", None) == "_to_needs_user"
        and any(
            kw.arg == "code"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value == "changeset_binding_missing"
            for kw in node.keywords
        )
    ]
    assert len(calls) == 1, "changeset_binding_missing is raised in exactly one place"
    passed = {kw.arg for kw in calls[0].keywords}
    assert "task_id" not in passed, "the site names no task — the fixture assumes so"
    assert "resume_phase" not in passed, "not resumable — the rebuild sets the phase"
    kind = next(kw.value.value for kw in calls[0].keywords if kw.arg == "kind")
    assert kind == "loop_fatal"


def test_audit_revise_no_record_drops_the_pointer_and_asks_for_a_fresh_audit(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.current_task = {"task_id": "audit", "title": "repository audit"}

    orch._to_needs_user("revise of the audit pseudo-task has no audit currently "
                        "on record for this session",
                        kind="loop_fatal", code="audit_revise_no_record")

    assert orch.state.phase == Phase.READY.value
    assert orch.state.current_task is None
    assert orch.state.last_response is None
    assert "AUDIT POINTER DISCARDED" in orch.state.outbox
    assert "audit" in orch.state.outbox
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_AUDIT_POINTER
    assert entry["discarded_pointer"]["task_id"] == "audit"


def test_state_inconsistent_discards_the_half_finished_round(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.SUBMITTING.value
    orch.state.pending_request = PendingRequest(request_id="alr-9", payload="p")
    orch.state.outbox = "a packet from the inconsistent round"

    orch._to_needs_user("phase=submitting but no pending request",
                        resume_phase=Phase.SUBMITTING.value,
                        kind="loop_fatal", code="state_inconsistent")

    assert orch.state.phase == Phase.READY.value
    assert orch.state.pending_request is None
    assert orch.state.last_response is None
    assert "ROUND REBUILT" in orch.state.outbox
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_SESSION_ROUND
    assert entry["discarded_request"] == "alr-9"


# =============================================================================
# 4. the task-free half — the gate halt-02 left closed
# =============================================================================


@pytest.mark.parametrize("code", TASK_FREE_CODES)
def test_a_task_free_code_is_rebuilt_with_no_task_in_flight(tmp_path, code):
    """THE load-bearing test of this change. halt-02's gate turned every plan
    off when `_autonomous_set_aside_task` found nothing to quarantine, and three
    of the six codes here carry no task and never could — so a suite that seeded
    `state.task_execution` (as every halt-02 test does) would pass against code
    that leaves exactly the loop-halting codes halting the loop.

    Driven off `ChangesetWiring` — a REAL queued changeset on a real repository —
    rather than a hand-written dict, because `changeset_binding_missing` now
    re-renders the packet from git objects and a fabricated sha would refuse for
    a reason that has nothing to do with the gate under test."""
    wiring = ChangesetWiring(tmp_path)
    wiring.state.phase = Phase.EXECUTING.value
    wiring.state.current_task = {"task_id": "audit"}

    wiring.orch._to_needs_user(f"stale: {code}", kind="loop_fatal", code=code)

    assert wiring.orch.state.phase == Phase.READY.value, f"{code} was not rebuilt"
    assert wiring.orch.state.park_kind is None
    # The fault is still ON THE RECORD — nothing became invisible.
    assert [b.code for b in wiring.blocker_store.all_blockers()] == [code]
    assert transcript_entries(wiring.config, "autonomous_rebuild")[0]["code"] == code


def test_the_changeset_arm_is_not_captured_by_an_unrelated_task_in_flight(tmp_path):
    """THE bug an earlier cut of this change shipped, and the reason the gate
    moved rather than being widened.

    `_dispatch_changeset_push` raises `push_candidate_unresolvable` naming NO
    task, while `state.task_execution` routinely names an unrelated task whose
    candidate is still unpublished — an operator can queue a `review-changeset`
    at any moment. With the in-flight fallback consulted, the rebuild took the
    TASK branch: it forgot an innocent task's approval binding and left the
    queued changeset, the record that had actually gone stale, exactly where it
    was. Wrong twice — it damaged approvable work AND did not fix the fault.

    Driven off `ChangesetWiring` since the halt-03 revision, because the drop
    now requires git to have ANSWERED that the object is gone: the hand-built
    dict this used to carry named a short sha on a loop with no gateway, which
    is the unverifiable case and now parks. The routing claim is unchanged and
    is what is asserted."""
    wiring = ChangesetWiring(tmp_path, tasks=("t1",))
    wiring.state.task_execution = {"task_id": "t1"}
    wiring.state.phase = Phase.EXECUTING.value
    wiring.state.sent_postcommits = [
        {"request_id": "alr-live",
         "postcommit": {"task_id": "t1", "candidate_sha": "live"}},
    ]
    wiring.state.carry_postcommit = {"task_id": "t1", "candidate_sha": "live"}
    wiring.state.changeset = {**wiring.queued, "candidate_sha": ABSENT_SHA}

    wiring.orch._to_needs_user("changeset push refused — the reviewed candidate "
                               "no longer resolves", kind="loop_fatal",
                               code="push_candidate_unresolvable")

    assert wiring.orch.state.phase == Phase.READY.value
    # The record that was stale is gone...
    assert wiring.orch.state.changeset is None
    # ...and the innocent task's approvable binding is untouched.
    assert [r["request_id"] for r in wiring.orch.state.sent_postcommits] == ["alr-live"]
    assert wiring.orch.state.carry_postcommit == {
        "task_id": "t1", "candidate_sha": "live"
    }
    entry = transcript_entries(wiring.config, "autonomous_rebuild")[0]
    assert entry["task_id"] == NO_TASK
    assert entry["discarded_changeset"]["candidate_sha"] == ABSENT_SHA


def test_a_refused_task_free_rebuild_quarantines_nobody(tmp_path):
    """The other half of the same fix. A refused rebuild of a session-scoped
    record must park exactly as the loop parks today — `loop_fatal`, naming no
    task — rather than setting aside whichever task happened to be in flight for
    a fault belonging to an operator's changeset."""
    orch, config, blocker_store, task_store, _ = build(
        tmp_path, enabled=True, in_flight="t1", tasks=("t1", "t2")
    )
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = None          # nothing queued -> the rebuild refuses

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY


def test_a_task_free_rebuild_never_invents_a_task_to_quarantine(tmp_path):
    """The other half of the same rule: acting without a task must not mean
    acting ON a task. Nothing is set aside, so `run --continuous` keeps every
    task it had."""
    wiring = ChangesetWiring(tmp_path, tasks=("t1", "t2"))

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value  # it really did rebuild
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY
    assert wiring.orch.state.park_task_id is None


def test_a_transport_code_still_needs_a_task_with_nothing_in_flight(tmp_path):
    """halt-02's gate is NARROWED, not removed. A retry-then-set-aside plan with
    no task to set aside still falls through to the ordinary park: the second
    stage cannot happen, so the first must not either."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.AWAITING.value

    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    assert "autonomous_recovery" not in transcript_types(config)


def test_the_execution_record_rebuild_still_requires_its_task(tmp_path):
    """The one stale record that is genuinely task-scoped. With no task named
    and none in flight there is no record to archive, so it parks."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("base behind head", kind="task_fatal",
                        code="task_base_behind_head")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "autonomous_rebuild" not in transcript_types(config)


# =============================================================================
# 5. the refusals — recut-01's bounds, inherited rather than re-earned
# =============================================================================


def test_a_published_candidate_is_never_archived(tmp_path):
    """budget-01's shape: a record was archived by hand 54 seconds before the
    reviewer returned PUSH for that exact candidate. A loop that archives on its
    own must refuse this, or it rebuilds that incident on a timer."""
    wiring = RealWiring(tmp_path, published_sha="deadbeefcafe")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "already published" in reasons[0]["reason"]


def test_a_candidate_with_a_verdict_still_in_flight_is_never_archived(tmp_path):
    """The same shape with the packet still outstanding: an approval for it can
    still arrive, so the work may already be approved."""
    wiring = RealWiring(tmp_path)
    wiring.orch.state.sent_postcommits = [
        {"request_id": "alr-live",
         "postcommit": {"task_id": "t1", "candidate_sha": wiring.base_sha}}
    ]

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "outstanding" in reasons[0]["reason"]


def test_an_unreadable_execution_record_is_never_archived(tmp_path):
    """UNREADABLE, not absent. A record this cannot parse may name a published
    candidate or one under review, and archiving it unread destroys the only
    evidence of which — the fail-open this whole design refuses."""
    wiring = RealWiring(tmp_path)
    (wiring.execution_store.directory / "t1.json").write_text("{ not json",
                                                              encoding="utf-8")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert (wiring.execution_store.directory / "t1.json").exists()
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "cannot be read" in reasons[0]["reason"]


def test_the_recut_cap_bounds_the_rebuild_across_episodes(tmp_path):
    """The bound that makes an automatic archival acceptable. `registry.recut`
    charges `recut_count`, which SURVIVES the archival a count on the execution
    record would not — so a task whose base keeps landing behind the head is
    rebuilt at most `MAX_TASK_RECUTS` times and then parks for a human."""
    wiring = RealWiring(tmp_path, recut_count=MAX_TASK_RECUTS)

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.park_kind == "task_fatal"
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and f"cap {MAX_TASK_RECUTS}" in reasons[0]["reason"]


def test_an_operator_hold_is_never_laundered_into_a_rebuild(tmp_path):
    """`recut_obstacle`'s own rule, reached through the same call: a task a
    human said "not now" about is not one the loop may cut again by itself."""
    wiring = RealWiring(tmp_path, status="pending")
    # The documented operator sequence, not a hand-poked field: `operator_block`
    # is what the inbox calls, and it is the only thing that sets
    # `HOLD_ORIGIN_OPERATOR`.
    wiring.registry.operator_block("t1", "parked by hand")
    wiring.task_store.save(wiring.registry)
    assert wiring.registry.get("t1").hold_origin == HOLD_ORIGIN_OPERATOR

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "task_operator_hold" in reasons[0]["reason"]


def test_a_task_the_registry_will_not_move_is_never_archived(tmp_path):
    """A pending task has no round in flight to discard. `recut_obstacle`
    refuses it and the rebuild parks rather than archiving anyway."""
    wiring = RealWiring(tmp_path, status="pending")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "task_not_in_progress" in reasons[0]["reason"]


def test_without_an_execution_store_and_worker_manager_nothing_is_archived(tmp_path):
    """BOTH halves, exactly as `_dispatch_recut` demands them: retiring the
    record without a worker manager leaves the contaminated worktree where the
    next dispatch looks for it — a rebuild that says it archived and did not."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("base behind head", kind="task_fatal",
                        code="task_base_behind_head", task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "execution store" in reasons[0]["reason"]


def test_a_task_missing_from_the_registry_is_never_archived(tmp_path):
    """Driven through the REAL wiring, not the collaborator-free build: without
    an execution store the earlier refusal fires first and the assertion below
    would be checking a different branch than it names."""
    wiring = RealWiring(tmp_path)

    wiring.orch._to_needs_user("base behind head", kind="task_fatal",
                               code="task_base_behind_head", task_id="ghost-99")

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "not in the registry" in reasons[0]["reason"]


def test_a_binding_rebuild_with_nothing_to_drop_parks_instead(tmp_path):
    """Re-dispatching without having changed anything is theatre that costs a
    round and arrives at the same park."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.changeset = None

    orch._to_needs_user("changeset push refused", kind="loop_fatal",
                        code="push_candidate_unresolvable")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "no queued changeset" in reasons[0]["reason"]


def test_an_unbindable_changeset_rebuild_with_no_queue_entry_parks_instead(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = None

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "no changeset is queued" in reasons[0]["reason"]


def test_an_unknown_record_kind_rebuilds_nothing(tmp_path):
    """`_autonomous_rebuild` has no fallback branch: an entry that names a
    record nobody wrote a handler for parks, rather than falling into the
    nearest existing handler. This is the guard that keeps the table safe to
    grow."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value
    plan = AutonomousRecovery(
        code="login_expired", action=RECOVER_BY_REBUILDING_AT_HEAD,
        max_attempts=1, why="test", stale_record="a_kind_nobody_handles",
    )
    orch._autonomy_plan = lambda code: plan

    orch._to_needs_user("whatever", kind="loop_fatal", code="login_expired",
                        task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and reasons[0]["reason"] == "unknown_record_kind"


def test_without_a_blocker_store_nothing_is_rebuilt(tmp_path):
    """No durable record means no budget to count and no durable question. The
    loop parks exactly as it always did.

    Discriminated on the OUTBOX, not on `state.changeset`: since the rebuild
    preserves the queue entry on the success path too, "the changeset is still
    there" no longer tells the two apart and would have made this test — and the
    two below it — pass whatever the gate did."""
    wiring = ChangesetWiring(tmp_path, with_blocker_store=False)
    displaced = wiring.state.outbox

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.outbox == displaced  # the packet was NOT rebuilt
    assert "autonomous_rebuild" not in transcript_types(wiring.config)


def test_a_corrupt_blocker_record_raises_rather_than_licensing_a_rebuild(tmp_path):
    """The fail-open this must not have: a store that cannot be read must never
    answer "nothing open", which would read as a full, unspent budget."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    blocker_store.directory.mkdir(parents=True, exist_ok=True)
    (blocker_store.directory / "blk-t1-001.json").write_text("{ nope",
                                                             encoding="utf-8")
    orch.state.phase = Phase.READY.value
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    with pytest.raises(StateCorruptError):
        orch._to_needs_user("unbindable", kind="loop_fatal",
                            code="changeset_binding_missing")
    assert "autonomous_rebuild" not in transcript_types(config)
    assert orch.state.changeset is not None


def test_a_non_boolean_enabled_is_not_read_as_consent_to_rebuild(tmp_path):
    """A hand-built config is validated by nothing, so the gate is `is not True`
    rather than a truthiness test — `enabled = "no"` is a truthy string."""
    wiring = ChangesetWiring(tmp_path)
    object.__setattr__(wiring.config, "autonomy", AutonomyConfig(enabled="yes"))  # type: ignore[arg-type]
    displaced = wiring.state.outbox

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.outbox == displaced
    assert "autonomous_rebuild" not in transcript_types(wiring.config)


def test_a_config_ceiling_of_zero_performs_no_rebuild_at_all(tmp_path):
    """`max_recovery_attempts` is a CEILING on the table, never a floor: at 0 no
    rebuild happens and the loop parks, which is how an operator switches the
    archival off without switching autonomy off."""
    wiring = ChangesetWiring(tmp_path, max_recovery_attempts=0)
    displaced = wiring.state.outbox

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.outbox == displaced
    assert "autonomous_rebuild" not in transcript_types(wiring.config)


# =============================================================================
# 6. `state_inconsistent` — the one that has to be bounded by a budget
# =============================================================================


def test_a_corrupt_state_error_is_not_rebuilt_at_all(tmp_path):
    """`StateCorruptError` subclasses `StateError`, so it reaches the SAME
    handler in `run` and would be automated as `state_inconsistent`. Rebuilding
    a round on top of a store that cannot be READ is precisely the fail-open
    this design exists to refuse, so the park site vetoes that occurrence.

    Driven through `run()`, so the veto is tested where it is wired rather than
    as a parameter nobody passes."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value

    def fake_step(phase):
        raise StateCorruptError("tasks.json is unreadable")

    orch._step = fake_step
    assert orch.run(max_steps=3) == Phase.NEEDS_USER.value

    assert orch.state.park_kind == "loop_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).code == "state_inconsistent"
    assert "autonomous_rebuild" not in transcript_types(config)
    assert transcript_entries(config, "state_error")[0]["corrupt"] is True


def test_an_ordinary_state_error_is_rebuilt_through_run(tmp_path):
    """The contrast that makes the test above mean something, and the wiring
    check: the same handler, the same code, `recoverable=True`."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value
    calls = []

    def fake_step(phase):
        from autoloop.errors import StateError

        calls.append(phase)
        if len(calls) == 1:
            raise StateError("phase=ready but outbox is empty")
        orch.state.phase = Phase.STOPPED.value

    orch._step = fake_step
    assert orch.run(max_steps=5) == Phase.STOPPED.value

    assert [p.value for p in calls] == [Phase.READY.value, Phase.READY.value]
    assert transcript_entries(config, "state_error")[0]["corrupt"] is False
    assert transcript_entries(config, "autonomous_rebuild")[0]["stale_record"] == (
        STALE_SESSION_ROUND
    )


def test_a_second_inconsistency_with_no_completed_step_between_parks(tmp_path):
    """THE bound for the one rebuild whose stale record is not durable. Every
    other handler removes a record, so the identical fault cannot recur off it;
    this one removes the loop's own round, and the inconsistency underneath can
    outlive it. Its blocker is therefore left OPEN, and the second occurrence
    finds the allowance spent."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("inconsistent", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="state_inconsistent")
    assert orch.state.phase == Phase.READY.value  # the one rebuild

    orch.state.phase = Phase.READY.value
    orch._to_needs_user("inconsistent", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="state_inconsistent")

    assert orch.state.phase == Phase.NEEDS_USER.value, "a second rebuild was granted"
    assert transcript_types(config).count("autonomous_rebuild") == 1
    assert blocker_store.open_recurrences(NO_TASK, "state_inconsistent") == 2


def test_the_state_inconsistent_record_stays_open_until_a_step_completes(tmp_path):
    """The other half of that bound: the record is closed by
    `_close_recovered_blocker` on a step that afterwards COMPLETES, which is the
    only free, honest evidence the inconsistency is behind the loop."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("inconsistent", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="state_inconsistent")

    assert len(blocker_store.open_blockers()) == 1
    orch._close_recovered_blocker()
    closed = blocker_store.all_blockers()
    assert closed[0].resolved_at is not None
    assert closed[0].answer is None, "a machine close must never forge an answer"


# =============================================================================
# 7. the loop actually keeps going
# =============================================================================


def test_the_rebuilt_task_is_offered_to_the_next_round(tmp_path):
    """"Re-dispatch" means the queue can hand it out again, not merely that the
    status string changed. `next_ready` is the function the packet builder
    consults, so this is the observable form of the claim."""
    wiring = RealWiring(tmp_path)

    wiring.park_as_the_site_does()

    reloaded = TaskStore(wiring.config.tasks_file).load()
    offered = reloaded.next_ready()
    assert offered is not None and offered.id == "t1"


def test_a_park_that_still_happens_is_still_a_park_continuous_mode_works_past(
    tmp_path, capsys
):
    """When a rebuild refuses, the loop must land exactly where it landed
    before — including the existing quarantine that lets `run --continuous`
    keep working on the rest of the roadmap."""
    wiring = RealWiring(tmp_path, recut_count=MAX_TASK_RECUTS)

    wiring.park_as_the_site_does()

    verdict = cli._handle_parked_task(
        wiring.config, StateStore(wiring.config.state_file), wiring.task_store,
        wiring.registry, wiring.orch.state,
    )
    assert verdict == "task_fatal"
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert reloaded.state_of("t2") is TaskState.READY
    capsys.readouterr()


def test_the_park_site_still_passes_the_arguments_this_fixture_replays(tmp_path):
    """`RealWiring.park_as_the_site_does` replays the `task_base_behind_head`
    call rather than driving a failed carry-forward, which is faster and far
    more legible — but a replay that drifts from the site is a test of nothing.

    So the site itself is read: it must still pass this code with a `task_id`
    and `kind="task_fatal"`. Without the `task_id`, the rebuild would have no
    record to archive and every test above would be exercising a call the loop
    never makes."""
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_to_needs_user":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        code = kwargs.get("code")
        if not (isinstance(code, ast.Constant) and code.value == "task_base_behind_head"):
            continue
        found.append(kwargs)
    assert len(found) == 1, "the park site moved or was duplicated"
    site = found[0]
    assert isinstance(site.get("kind"), ast.Constant)
    assert site["kind"].value == "task_fatal"
    assert "task_id" in site, "the site no longer names a task to archive"


def test_the_state_inconsistent_site_still_passes_the_recoverable_veto(tmp_path):
    """The veto is the whole reason a corrupt store is not rebuilt on. A site
    that stopped passing it would switch the guard off silently, which is the
    exact failure shape this feature is built to refuse."""
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_to_needs_user":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        code = kwargs.get("code")
        if isinstance(code, ast.Constant) and code.value == "state_inconsistent":
            found.append(kwargs)
    assert len(found) == 1, "the park site moved or was duplicated"
    assert "recoverable" in found[0], "the corrupt-state veto is no longer passed"


def test_the_push_sites_still_split_into_a_task_arm_and_a_task_free_arm():
    """The split `_rebuild_stale_push_binding` dispatches on is `task_id`, and it
    is only correct because the park sites really are shaped that way: the task
    push passes `binding.task_id` and the changeset push passes none. A site that
    started passing a task_id for the changeset arm would silently route an
    operator's changeset into the task rebuild — the capture bug an earlier cut
    shipped, reached from the other end."""
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    arms: dict[str, list[bool]] = {
        "push_candidate_stale": [], "push_candidate_unresolvable": []
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_to_needs_user":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        code = kwargs.get("code")
        if isinstance(code, ast.Constant) and code.value in arms:
            arms[code.value].append("task_id" in kwargs)
    # One task-scoped site for the stale code; two sites for the unresolvable
    # one, exactly one of which names a task.
    assert arms["push_candidate_stale"] == [True]
    assert sorted(arms["push_candidate_unresolvable"]) == [False, True]


def test_nothing_here_can_publish(tmp_path):
    """A rebuild archives, re-renders and re-dispatches, and does nothing else:
    no rebuild imports, pushes or re-provisions anything, and the one that
    archives on disk asserts the record moved rather than a ref.

    The two publishes in this file are deliberately NOT a rebuild's doing. Each
    happens only after a stamped reviewer approval reaches a dispatch
    (`_dispatch_changeset_push` / `_dispatch_task_push`) in the round the rebuild
    made possible — which is the whole point of restoring the binding, and the
    property the previous round's drop-only handler could not satisfy. That is
    why `ChangesetWiring` and `PostcommitWiring` can carry a Publisher and this
    orchestrator has none."""
    wiring = RealWiring(tmp_path)
    assert wiring.orch._publisher is None

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value
    assert "push" not in transcript_types(wiring.config)
    assert wiring.git.head_sha() == wiring.head_sha


def test_a_rebuild_never_touches_another_tasks_record(tmp_path):
    """Scope: the archival names one task, and `t2`'s row must be exactly where
    it was afterwards."""
    wiring = RealWiring(tmp_path)

    wiring.park_as_the_site_does()

    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.get("t2").recut_count == 0
    assert reloaded.state_of("t2") is TaskState.READY


def test_a_registry_refusal_is_reported_rather_than_skipped(tmp_path):
    """strand-01's rule, inherited: a silent `continue` is the fail-open. Every
    refusal here writes `autonomous_rebuild_refused`, because the park an
    operator then sees carries the ORIGINAL question and cannot say the loop
    tried."""
    wiring = RealWiring(tmp_path, status="pending")

    wiring.park_as_the_site_does()

    refused = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert len(refused) == 1
    assert refused[0]["code"] == "task_base_behind_head"
    assert refused[0]["reason"]


def test_recut_obstacle_raising_is_caught_rather_than_escaping_the_park(tmp_path):
    """A park handler is the one place a second failure has nowhere to go: an
    exception out of here replaces a recoverable park with a crashed process."""
    wiring = RealWiring(tmp_path)

    def boom(task_id):
        raise TaskGraphError("task_unknown", "gone")

    wiring.registry.recut_obstacle = boom

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
