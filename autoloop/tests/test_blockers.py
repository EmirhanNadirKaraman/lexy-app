"""Blockers: task_fatal vs loop_fatal classification, persisted `Blocker`
records, continuous mode's task quarantine, and the `blockers`/`answer` CLI
commands.

Self-contained per this codebase's convention (see e.g.
`test_postcommit_review.py`'s docstring) — duplicates the small
`run_git`/`WritingExecutor`/`build_postcommit`/`FakeClient`-shaped helpers
rather than importing them from another test module. `build_postcommit`
here is deliberately the LIGHTER `WorktreeManager`-based construction (like
`test_postcommit_review.py`), not the full worker-repo/publisher one
(`test_v1_smoke.py`) — worker isolation and publication are irrelevant to
classification/blocker behaviour, and the lighter setup makes the
task_fatal-triggering scenarios (round-cap, attempt-count) cheap to build.
"""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.audit.agents import ClaudeCliRunner
from autoloop.blockers import NO_TASK, Blocker, BlockerStore, by_severity
from autoloop.config import AutoloopConfig, BrowserConfig, load_config
from autoloop.contract import Decision, Directive
from autoloop.errors import StateCorruptError, StateError, TaskGraphError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.lock import LoopLock
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore, utcnow_iso
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecution, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/blockers-test"


# =============================================================================
# shared helpers
# =============================================================================


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def real_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo_root = tmp_path / name
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def make_config(tmp_path: Path, policy: PolicyConfig | None = None) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy or PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
    )


def write_config_toml(tmp_path: Path, state_dir_name: str = ".al") -> Path:
    """A real `.toml` config file for tests that go through CLI command
    functions (`_cmd_blockers`/`_cmd_answer`), which call `load_config`
    themselves rather than taking an in-memory `AutoloopConfig`. `state_dir`
    is written as an ABSOLUTE path (`tmp_path / state_dir_name`) — unlike
    `test_v1_smoke.py`'s helper of the same name, which deliberately uses a
    RELATIVE one to test that real-world shape. A relative path here would
    resolve against the pytest process's actual cwd (not `tmp_path`), which
    is not just wrong for the test but writes real, un-isolated files into
    whatever directory pytest happened to be invoked from — see
    `docs/COMMON_ERRORS.md`. `workers_root` is a SIBLING of `state_dir`
    (`tmp_path / "workers_root"`), i.e. absolute and outside whatever repo a
    given test later creates under `tmp_path` — satisfies
    `worker_env.validate_workers_root` without needing repo-root context
    here."""
    path = tmp_path / "config.toml"
    state_dir_value = str(tmp_path / state_dir_name)
    workers_root_value = str(tmp_path / "workers_root")
    path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{state_dir_value}"\nworkers_root = "{workers_root_value}"\n\n'
        "[policy]\nimplement_enabled = true\n",
        encoding="utf-8",
    )
    return path


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def ready_task(tid="t1", approved_paths=("a.py",)) -> Task:
    # Default matches `WritingExecutor`'s own default file — see below.
    return Task(id=tid, title=f"Title {tid}", description="desc", approved_paths=tuple(approved_paths))


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)


def revise(task_id="t1", feedback="please fix it") -> Directive:
    return Directive(
        decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback=feedback
    )


class WritingExecutor:
    """Writes into the dispatched task's own worktree and reports success.
    `per_round_files` (when given) is consumed one entry per call so
    successive `revise` rounds produce a real diff instead of an empty,
    refused commit — same shape as `test_postcommit_review.py`'s double."""

    def __init__(self, worktrees_root, files=None, per_round_files=None, status="ok"):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files or {"a.py": "one\n"})
        self.per_round_files = list(per_round_files) if per_round_files else None
        self.status = status
        self.calls = 0

    def execute(self, directive, task):
        wt = self.worktrees_root / task.id
        files = self.per_round_files[self.calls] if self.per_round_files is not None else self.files
        self.calls += 1
        for rel, content in files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status=self.status,
            summary=f"round {self.calls}",
            details="details",
            validation="ok",
            changed_paths=tuple(files.keys()),
        )


def build_postcommit(
    tmp_path,
    executor,
    task_ids=("t1",),
    policy=None,
    blocker_store: BlockerStore | None = None,
):
    """Real-git, `WorktreeManager`-backed Orchestrator (the lighter of the
    two produce-then-review constructions this codebase's tests use — see
    module docstring). Returns everything a classification/blocker test
    needs, including `config`/`store`/`task_store`/`registry` directly
    rather than forcing callers to reach through `orch._...` private
    attributes."""
    repo_root = real_repo(tmp_path)
    git = GitGateway(repo_root, PolicyEngine(policy or PolicyConfig()))
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = make_config(tmp_path, policy)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    registry = TaskRegistry([ready_task(tid) for tid in task_ids])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    manifest_store = ManifestStore(config.manifests_dir)

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
        manifest_store=manifest_store,
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=intent_store,
        blocker_store=blocker_store,
        validation_runner=ok_validation,
    )
    return orch, config, store, task_store, registry, execution_store


def minimal_orchestrator(tmp_path, blocker_store: BlockerStore | None = None, tasks=()):
    """An Orchestrator with no git/executor/browser collaborators at all —
    `_to_needs_user` never touches any of them (only `state`, `_log`,
    `_blocker_store`, `_store`), so `None` stand-ins are enough for the pure
    classification/persistence tests that never call `_dispatch_executor` or
    `run()`."""
    config = make_config(tmp_path)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    registry = TaskRegistry(list(tasks))
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
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
    return orch, config, store, task_store, registry


# =============================================================================
# 1. a task_fatal park blocks only that task; the loop continues onto the
#    NEXT ready task
# =============================================================================


def test_task_fatal_park_blocks_only_that_task_and_loop_selects_next_ready(tmp_path):
    executor = WritingExecutor(
        tmp_path / "worktrees",
        per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}, {"a.py": "three\n"}],
    )
    blocker_store = BlockerStore(tmp_path / ".al" / "blockers")
    orch, config, store, task_store, registry, execution_store = build_postcommit(
        # Pins the cap this test depends on: it reaches its task_fatal park
        # BY exhausting review rounds, which no longer happens by default.
        tmp_path, executor, task_ids=("t1", "t2"), blocker_store=blocker_store,
        policy=PolicyConfig(implement_enabled=True, max_review_rounds=2),
    )

    # Drive t1 through 3 rounds directly (bypassing the browser entirely —
    # `_dispatch_executor` is exactly what `_step_executing` would call after
    # parsing a directive) to hit the review-round cap: task_fatal.
    orch._dispatch_executor(implement("t1"))
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise("t1", "round 2 feedback"))
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise("t1", "round 3 feedback"))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert orch.state.park_task_id == "t1"
    assert orch.state.park_blocker_id is not None

    # The blocker record exists and carries the operator-facing question.
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert blocker is not None
    assert blocker.kind == "task_fatal"
    assert blocker.code == "review_round_cap"
    assert blocker.task_id == "t1"
    assert blocker.question == orch.state.question

    # cli's continuous-mode handler quarantines t1 and clears the session.
    outcome = cli._handle_parked_task(config, store, task_store, registry, orch.state)
    assert outcome == "task_fatal"
    assert registry.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert registry.get("t1").blocked_reason  # non-empty
    assert registry.state_of("t2") is TaskState.READY  # untouched
    assert not config.state_file.exists()  # session cleared, no litter left behind

    # Persistence across an outer iteration: a FRESH load (simulating the
    # next pass of `_run_continuous`'s while-loop re-reading tasks.json from
    # disk) still shows t1 blocked — `task_store.save` inside
    # `_handle_parked_task` is what makes this true.
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR

    # The loop continues: with t1 quarantined, t2 is the next ready task —
    # assert the SELECTION itself, not just that `_select_and_kickoff`
    # started something (it would return True for ANY ready task).
    assert reloaded.next_ready().id == "t2"
    started = cli._select_and_kickoff(config, store, reloaded)
    assert started is True
    fresh_state = store.load()
    assert fresh_state is not None
    assert fresh_state.session_id != orch.state.session_id  # a NEW session


# =============================================================================
# 2. a loop_fatal park stops the loop (exit 2) and never touches other
#    tasks' status
# =============================================================================


def test_loop_fatal_park_stops_continuous_mode_and_leaves_tasks_untouched(tmp_path, capsys):
    config = make_config(tmp_path)
    registry = TaskRegistry([ready_task("t1"), ready_task("t2")])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = Phase.NEEDS_USER.value
    state.park_kind = "loop_fatal"
    state.park_task_id = "t1"  # loop_fatal parks MAY still name a task descriptively
    state.question = "the environment is broken"
    store.save(state)

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    rc = cli._run_continuous(args, config)
    assert rc == 2

    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY  # NOT quarantined
    assert reloaded.state_of("t2") is TaskState.READY
    out = capsys.readouterr().out
    assert "loop_fatal" in out


# =============================================================================
# 3. an unclassified / new park site defaults to loop_fatal — fail closed
# =============================================================================


def test_to_needs_user_default_kind_is_loop_fatal_and_persists_as_such(tmp_path):
    blocker_store = BlockerStore(tmp_path / ".al" / "blockers")
    orch, config, store, task_store, registry = minimal_orchestrator(
        tmp_path, blocker_store=blocker_store
    )

    # A brand-new park site that forgot to pass `kind=` at all — exactly the
    # "nobody has reasoned about this one yet" scenario.
    orch._to_needs_user("some future failure nobody classified")

    assert orch.state.park_kind == "loop_fatal"
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert blocker.kind == "loop_fatal"
    assert blocker.code == "unclassified"
    assert blocker.task_id == NO_TASK  # no task_id was given either


def test_missing_or_unrecognised_park_kind_is_treated_as_loop_fatal_by_cli(tmp_path):
    """A state file written by an older build (before `park_kind` existed,
    so it defaults to `None` via `LoopState`'s dataclass default) must not
    be silently treated as task_fatal just because a `park_task_id` happens
    to be set from stale/unrelated data."""
    config = make_config(tmp_path)
    registry = TaskRegistry([ready_task("t1")])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = Phase.NEEDS_USER.value
    state.park_kind = None
    state.park_task_id = "t1"
    state.question = "old-format park"
    store.save(state)

    outcome = cli._handle_parked_task(config, store, task_store, registry, state)
    assert outcome == "loop_fatal"
    assert registry.state_of("t1") is TaskState.READY  # never quarantined
    assert config.state_file.exists()  # session NOT cleared for a loop_fatal park


# =============================================================================
# 3b. a FAULT STOP ends the run without parking, and still leaves a record
#
# The terminal the exhausted policy-denial budget reaches since `ask_user`'s
# retirement was completed. It is `stopped`, not `needs_user` — there is no
# question for an operator, because the only thing that could produce a
# different directive is the reviewer that just spent the budget — but it is
# NOT the reviewer's own `stop`, and everything downstream has to be able to
# tell the two apart.
# =============================================================================


def test_fault_stop_records_a_loop_fatal_blocker_without_parking(tmp_path):
    blocker_store = BlockerStore(tmp_path / ".al" / "blockers")
    orch, config, store, task_store, registry = minimal_orchestrator(
        tmp_path, blocker_store=blocker_store
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_fault_stop(
        "the reviewer kept proposing refused directives",
        code="policy_denial_budget_exhausted",
        task_id="t1",
        detail="decision=ask_user verdict_code=legacy_ask_user_retired",
    )

    # Terminal, and terminal as a STOP: nothing parked, nothing resumable.
    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert orch.state.stop_reason == "the reviewer kept proposing refused directives"
    assert orch.state.question is None
    assert orch.state.resume_phase is None
    assert orch.state.park_kind is None and orch.state.park_blocker_id is None

    # Ending is not forgetting: the operator-facing record is the same one a
    # park would have written, so `blockers` / `answer` behave unchanged.
    blocker = blocker_store.load(orch.state.stop_blocker_id)
    assert blocker is not None
    assert blocker.kind == "loop_fatal"
    assert blocker.code == "policy_denial_budget_exhausted"
    assert blocker.question == orch.state.stop_reason
    assert blocker.phase == Phase.EXECUTING.value  # where it happened, not "stopped"

    # And it survives the process: a reload sees the same classification.
    reloaded = store.load()
    assert reloaded.stop_kind == "fault"
    assert reloaded.stop_blocker_id == blocker.id


def test_contract_stop_is_classified_and_is_not_a_fault(tmp_path):
    """The other half of the discriminator. Without this, `stop_kind` could
    default its way to correctness in the fault test above while every real
    `stop` stayed unclassified — and an unclassified stop is the one that
    `cli._cmd_smoke_browser` must NOT report as PASS."""
    orch, config, store, task_store, registry = minimal_orchestrator(tmp_path)

    orch._dispatch(Directive(decision=Decision.STOP, reason="all done"))

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "contract"
    assert cli._is_fault_stop(orch.state) is False
    assert cli._is_fault_stop(store.load()) is False


def test_fault_stop_ends_continuous_mode_instead_of_starting_a_fresh_session(
    tmp_path, capsys
):
    """The reason a fault stop is distinguishable from a contract stop at all.

    Continuous mode treats `stopped` as a clean boundary and runs the
    selection policy — correct for a reviewer that decided the round was
    finished, and a churn machine for a run that died on the denial budget:
    the same READY task would be picked, a fresh session kicked off, the same
    reviewer would deny again, and the loop would burn a full round per
    iteration while looking like progress."""
    config = make_config(tmp_path)
    registry = TaskRegistry([ready_task("t1")])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = "fault"
    state.stop_reason = "more than 0 policy-denied directives in a row"
    store.save(state)

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    rc = cli._run_continuous(args, config)

    assert rc == 2
    out = capsys.readouterr().out
    assert "stop_kind=fault" in out
    # No fresh session was started over the top of the stopped one — the task
    # is still READY and waiting, not consumed by a round nobody asked for.
    assert store.load().session_id == state.session_id
    assert TaskStore(config.tasks_file).load().state_of("t1") is TaskState.READY


def test_plain_run_on_a_fault_stopped_session_names_a_recovery_that_works(
    tmp_path, capsys
):
    """The parked branch offers `--answer` / `--retry`, and BOTH raise for a
    fault stop: `--answer` requires `needs_user`, `--retry` requires the
    `resume_phase` a fault stop deliberately clears. Printing them would send
    the operator to two commands that error — a terminal telling someone to do
    something impossible is worse than one that says nothing."""
    config = make_config(tmp_path)
    TaskStore(config.tasks_file).save(TaskRegistry([ready_task("t1")]))

    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = "fault"
    state.stop_reason = "more than 0 policy-denied directives in a row"
    store.save(state)

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=False,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    # `_run_locked`, not `_cmd_run`: the branch under test is here, and going
    # through the outer command would take the lock, run the startup backlog
    # sweep and publish a heartbeat — none of which this is about.
    rc = cli._run_locked(args, config)

    assert rc == 2
    out = capsys.readouterr().out
    assert "stop_kind=fault" in out
    assert "blockers" in out  # the recovery that does work
    assert "Loop is parked. Use --answer / --retry" not in out
    # Nothing was run: the session is exactly as the fault stop left it.
    assert store.load().session_id == state.session_id


def test_a_contract_stop_is_still_a_clean_boundary_for_continuous_mode(tmp_path):
    """Guards the fix above against over-reach: the fault check must not turn
    every completed session into a halt. With a ready task waiting, a
    `contract` stop still starts the next round."""
    config = make_config(tmp_path)
    registry = TaskRegistry([ready_task("t1")])
    TaskStore(config.tasks_file).save(registry)

    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = "contract"
    state.stop_reason = "done"
    store.save(state)

    assert cli._is_fault_stop(state) is False
    started = cli._select_and_kickoff(config, store, registry)
    assert started is True
    assert store.load().session_id != state.session_id  # a NEW session


def test_a_legacy_unclassified_stop_is_treated_as_a_clean_boundary(tmp_path):
    """A state file written before `stop_kind` existed carries `""`. It is
    read as a clean boundary (the behaviour every `stopped` session had before
    fault stops existed), NOT as a fault: being wrong that way costs one extra
    round, while the other direction would halt a healthy loop on every
    session it ever completed."""
    config = make_config(tmp_path)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    store.save(state)

    assert state.stop_kind == ""
    assert cli._is_fault_stop(store.load()) is False


# =============================================================================
# 4. blocker records persist with the operator question text and survive
#    reload
# =============================================================================


def test_blocker_persists_and_reloads_with_the_operator_question_text(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    blocker = Blocker(
        id=store.next_id("t1"),
        task_id="t1",
        kind="task_fatal",
        code="attempt_count_ceiling",
        question="task t1: 5 commit attempts on autoloop/t1 without an approved review",
        detail="attempt_count=5 cap=5",
        phase="executing",
        created_at=utcnow_iso(),
    )
    store.save(blocker)

    reloaded = store.load(blocker.id)
    assert reloaded is not None
    assert reloaded == blocker  # full round-trip, not just the id
    assert reloaded.question == blocker.question
    assert reloaded.resolved_at is None
    assert reloaded.answer is None


# =============================================================================
# 5. a corrupt blocker record raises rather than reading as absent
# =============================================================================


def test_corrupt_blocker_record_raises_not_absent(tmp_path):
    directory = tmp_path / "blockers"
    directory.mkdir()
    (directory / "blk-t1-001.json").write_text("{not valid json", encoding="utf-8")
    store = BlockerStore(directory)

    with pytest.raises(StateCorruptError):
        store.load("blk-t1-001")
    # A corrupt file must not be silently skipped while listing either.
    with pytest.raises(StateCorruptError):
        store.open_blockers()
    with pytest.raises(StateCorruptError):
        store.all_blockers()


# =============================================================================
# 6. `blockers` lists open ones; `answer` resolves and unblocks; the task
#    becomes READY again
# =============================================================================


def test_cli_blockers_lists_and_answer_resolves_and_unblocks(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)

    registry = TaskRegistry([ready_task("t1")])
    registry.block("t1", "review round cap reached")
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    blocker_store = BlockerStore(config.blockers_dir)
    blocker = Blocker(
        id=blocker_store.next_id("t1"),
        task_id="t1",
        kind="task_fatal",
        code="review_round_cap",
        question="task t1: review round cap (2) reached — please advise",
        detail="",
        phase="executing",
        created_at=utcnow_iso(),
    )
    blocker_store.save(blocker)

    rc = cli._cmd_blockers(Namespace(config=config_path, all=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert blocker.id in out
    assert "task=t1" in out
    assert "please advise" in out

    rc = cli._cmd_answer(Namespace(config=config_path, blocker_id=blocker.id, text="retry it"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "resolved" in out
    assert "ready again" in out

    resolved = blocker_store.load(blocker.id)
    assert resolved.resolved_at is not None
    assert resolved.answer == "retry it"

    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.get("t1").blocked_reason == ""

    # now resolved, so it drops out of the default (open-only) listing
    capsys.readouterr()
    cli._cmd_blockers(Namespace(config=config_path, all=False))
    assert blocker.id not in capsys.readouterr().out
    cli._cmd_blockers(Namespace(config=config_path, all=True))
    assert blocker.id in capsys.readouterr().out


# =============================================================================
# 7. `answer` refuses an unknown id and an already-resolved id
# =============================================================================


def test_answer_refuses_unknown_and_already_resolved(tmp_path):
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    blocker_store = BlockerStore(config.blockers_dir)

    with pytest.raises(StateError):
        cli._cmd_answer(Namespace(config=config_path, blocker_id="blk-nope-001", text="x"))

    blocker = Blocker(
        id=blocker_store.next_id("t1"),
        task_id="t1",
        kind="loop_fatal",
        code="git_failure_budget_exhausted",
        question="repeated git failures",
        detail="",
        phase="executing",
        created_at=utcnow_iso(),
    )
    blocker_store.save(blocker)
    cli._cmd_answer(Namespace(config=config_path, blocker_id=blocker.id, text="first answer"))

    with pytest.raises(StateError):
        cli._cmd_answer(Namespace(config=config_path, blocker_id=blocker.id, text="second answer"))
    # the first answer is untouched by the refused second attempt
    assert blocker_store.load(blocker.id).answer == "first answer"


# =============================================================================
# 8. exhaustion: no ready task + unchanged fingerprint + open blocker(s) ->
#    prints all open blockers, exits 0
# =============================================================================


def test_exhaustion_prints_open_blockers_and_exits_0(tmp_path, monkeypatch, capsys):
    repo_root = real_repo(tmp_path)
    monkeypatch.chdir(repo_root)
    config = make_config(tmp_path)

    registry = TaskRegistry([ready_task("t1")])
    registry.block("t1", "stuck")
    TaskStore(config.tasks_file).save(registry)

    cli._save_fingerprint(config, cli.repo_fingerprint(repo_root))

    blocker_store = BlockerStore(config.blockers_dir)
    blocker = Blocker(
        id=blocker_store.next_id("t1"),
        task_id="t1",
        kind="task_fatal",
        code="review_round_cap",
        question="task t1 is stuck at the review round cap",
        detail="",
        phase="executing",
        created_at=utcnow_iso(),
    )
    blocker_store.save(blocker)

    def boom_conversation(*a, **kw):
        raise AssertionError("no ChatGPT call expected during exhaustion")

    def boom_claude(self, spec):
        raise AssertionError("no Claude call expected during exhaustion")

    monkeypatch.setattr(cli, "create_conversation", boom_conversation)
    monkeypatch.setattr(ClaudeCliRunner, "run", boom_claude)

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    rc = cli._run_continuous(args, config)
    assert rc == 0
    out = capsys.readouterr().out
    assert "exhausted" in out
    assert blocker.id in out
    assert "task t1 is stuck at the review round cap" in out


# =============================================================================
# 9. a blocked task is never returned by next_ready()
# =============================================================================


def test_blocked_task_never_returned_by_next_ready():
    registry = TaskRegistry([ready_task("t1"), ready_task("t2")])
    registry.block("t1", "review round cap")
    assert registry.next_ready().id == "t2"
    assert [t.id for t in registry.ready_tasks()] == ["t2"]

    registry.block("t2", "attempt ceiling")
    assert registry.next_ready() is None
    assert registry.ready_tasks() == []


# =============================================================================
# 10. tasks.json written before this change still loads
# =============================================================================


def test_pre_blockers_tasks_json_still_loads(tmp_path):
    path = tmp_path / "tasks.json"
    old_format = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "t1",
                "title": "Old task",
                "description": "written before blocked_reason existed",
                "depends_on": [],
                "status": "pending",
                "created_at": "2026-01-01T00:00:00+00:00",
                "completed_at": None,
                # NOTE: no "blocked_reason" key at all.
            }
        ],
    }
    path.write_text(json.dumps(old_format), encoding="utf-8")

    registry = TaskStore(path).load()
    assert registry is not None
    task = registry.get("t1")
    assert task.blocked_reason == ""
    assert registry.state_of("t1") is TaskState.READY

    # And the loaded registry supports the new operations right away.
    registry.block("t1", "new reason")
    assert registry.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR


# =============================================================================
# 11. zero Claude and zero ChatGPT calls while the fingerprint is unchanged
#     and everything is blocked (counting fakes, not just assert-on-call)
# =============================================================================


def test_zero_calls_while_fingerprint_unchanged_and_everything_blocked(tmp_path, monkeypatch):
    repo_root = real_repo(tmp_path)
    monkeypatch.chdir(repo_root)
    config = make_config(tmp_path)

    registry = TaskRegistry([ready_task("t1"), ready_task("t2")])
    registry.block("t1", "stuck A")
    registry.block("t2", "stuck B")
    TaskStore(config.tasks_file).save(registry)
    cli._save_fingerprint(config, cli.repo_fingerprint(repo_root))

    blocker_store = BlockerStore(config.blockers_dir)
    for tid in ("t1", "t2"):
        blocker_store.save(
            Blocker(
                id=blocker_store.next_id(tid),
                task_id=tid,
                kind="task_fatal",
                code="attempt_count_ceiling",
                question=f"{tid} is stuck",
                detail="",
                phase="executing",
                created_at=utcnow_iso(),
            )
        )

    chatgpt_calls = {"count": 0}
    claude_calls = {"count": 0}

    def counting_conversation(*a, **kw):
        chatgpt_calls["count"] += 1
        raise AssertionError("no ChatGPT call expected")

    def counting_claude(self, spec):
        claude_calls["count"] += 1
        raise AssertionError("no Claude call expected")

    monkeypatch.setattr(cli, "create_conversation", counting_conversation)
    monkeypatch.setattr(ClaudeCliRunner, "run", counting_claude)

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    rc = cli._run_continuous(args, config)
    assert rc == 0
    assert chatgpt_calls["count"] == 0
    assert claude_calls["count"] == 0


# =============================================================================
# 12. round-trip: block -> answer -> unblock -> selected on the next pass
# =============================================================================


def test_round_trip_block_answer_unblock_selected_next_pass(tmp_path):
    executor = WritingExecutor(
        tmp_path / "worktrees",
        per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}, {"a.py": "three\n"}],
    )
    blocker_store = BlockerStore(tmp_path / ".al" / "blockers")
    orch, config, store, task_store, registry, _execution_store = build_postcommit(
        # Pins the cap: this test reaches its task_fatal park BY exhausting
        # review rounds, and the cap now defaults to unlimited. Its feedback
        # differs per round, so the convergence guard correctly does not fire.
        tmp_path, executor, task_ids=("t1",), blocker_store=blocker_store,
        policy=PolicyConfig(implement_enabled=True, max_review_rounds=2)
    )
    config_path = write_config_toml(tmp_path)  # same state_dir (".al"), for the CLI commands

    orch._dispatch_executor(implement("t1"))
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise("t1", "round 2"))
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise("t1", "round 3"))
    assert orch.state.park_kind == "task_fatal"
    blocker_id = orch.state.park_blocker_id

    outcome = cli._handle_parked_task(config, store, task_store, registry, orch.state)
    assert outcome == "task_fatal"
    assert TaskStore(config.tasks_file).load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR

    rc = cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker_id, text="go ahead, try again")
    )
    assert rc == 0

    unblocked = TaskStore(config.tasks_file).load()
    assert unblocked.state_of("t1") is TaskState.READY

    started = cli._select_and_kickoff(config, store, unblocked)
    assert started is True
    fresh = store.load()
    assert fresh is not None and fresh.phase == Phase.READY.value


# =============================================================================
# 13. quarantine is ENFORCED, not just advisory — a directive that names a
#     BLOCKED_BY_OPERATOR task directly is denied by policy (and, defense in
#     depth, by TaskRegistry.mark_in_progress too)
# =============================================================================


def test_blocked_by_operator_task_cannot_be_dispatched_around():
    registry = TaskRegistry([ready_task("t1")])
    registry.block("t1", "review round cap reached")
    policy = PolicyEngine(PolicyConfig(implement_enabled=True))

    verdict = policy.authorize_directive(implement("t1"), "main", registry)
    assert not verdict.allowed
    assert verdict.code == "task_blocked_by_operator"

    verdict = policy.authorize_directive(revise("t1", "try again"), "main", registry)
    assert not verdict.allowed
    assert verdict.code == "task_blocked_by_operator"

    # Defense in depth: even a caller that bypasses policy entirely (a test,
    # a future dispatch path) cannot silently un-quarantine the task via
    # mark_in_progress.
    with pytest.raises(TaskGraphError) as excinfo:
        registry.mark_in_progress("t1")
    assert excinfo.value.code == "task_blocked_by_operator"
    assert registry.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR  # unchanged


# =============================================================================
# 14. `status`/`_summary` survives a corrupt blocker record (only `blockers`
#     itself is supposed to raise loudly)
# =============================================================================


def test_summary_survives_a_corrupt_blocker_record(tmp_path):
    config = make_config(tmp_path)
    registry = TaskRegistry([ready_task("t1")])
    TaskStore(config.tasks_file).save(registry)
    state = LoopState.new(URL)

    blockers_dir = config.blockers_dir
    blockers_dir.mkdir(parents=True)
    (blockers_dir / "blk-t1-001.json").write_text("{not valid json", encoding="utf-8")

    text = cli._summary(config, state, registry)
    assert "open blockers" in text
    assert "unreadable" in text

    # The dedicated `blockers` command, by contrast, still raises loudly —
    # that IS its job (test 5 pins the store-level behaviour this relies on).
    with pytest.raises(StateCorruptError):
        BlockerStore(config.blockers_dir).open_blockers()


# ---- acceptance-check regressions (Phase 1 gaps) ---------------------------


def test_reparking_the_same_condition_updates_one_blocker(tmp_path):
    """A restart/retry against the same wall must not fill the queue with
    duplicates of one problem — the operator has to be able to see how many
    DISTINCT things are wrong."""
    from autoloop.blockers import BlockerStore

    store = BlockerStore(tmp_path / "blockers")
    for i in range(4):
        store.record(
            task_id="t1", kind="task_fatal", code="post_commit_verification_failed",
            question="q", detail="d", phase="executing",
            now=f"2026-07-31T00:0{i}:00+00:00",
        )
    assert len(store.all_blockers()) == 1
    assert store.all_blockers()[0].recurrences == 4

    # A genuinely different condition is its own record.
    store.record(task_id="t1", kind="task_fatal", code="review_round_cap",
                 question="q2", detail="d", phase="executing",
                 now="2026-07-31T00:09:00+00:00")
    assert len(store.all_blockers()) == 2

    # A resolved blocker is not matched — the condition returning after an
    # answer is genuinely new.
    store.resolve("blk-t1-001", "handled")
    store.record(task_id="t1", kind="task_fatal", code="post_commit_verification_failed",
                 question="q", detail="d", phase="executing",
                 now="2026-07-31T00:10:00+00:00")
    assert len(store.all_blockers()) == 3


def test_environment_drift_is_loop_fatal_not_a_task_refusal():
    """A hook appearing mid-task affects EVERY task. If it were task_fatal the
    loop would march through the backlog blocking each task in turn while the
    hook stayed installed."""
    import inspect
    from autoloop import orchestrator
    from autoloop.errors import EnvironmentDriftError, GitCommandError

    assert issubclass(EnvironmentDriftError, GitCommandError)
    src = inspect.getsource(orchestrator.Orchestrator._dispatch_task_postcommit)
    drift = src.index("except EnvironmentDriftError")
    generic = src.index("except GitCommandError")
    assert drift < generic, "drift must be caught BEFORE the generic handler"
    assert 'code="worker_environment_drift"' in src
    assert 'kind="loop_fatal"' in src[drift:generic]
    # And the task-scoped handler must not claim to cover environment drift.
    tail = src[generic:generic + 1200]
    assert 'code="commit_refused"' in tail
    assert 'kind="task_fatal"' in tail


def test_answer_cannot_clear_an_environmental_invariant_by_text(tmp_path, monkeypatch, capsys):
    """A protected-branch push refusal can NEVER be cleared by answer text.

    Previously this asserted on `push_refused`, whose precondition only rechecked
    `allow_push` — a flag necessarily already true for the push to have reached
    the gateway at all, so the check was a no-op for every real failure. The
    orchestrator now emits `push_refused_protected`, which maps to an
    unconditional refusal: a protected destination is not something an operator
    can assert their way past."""
    from autoloop.blockers import Blocker, BlockerStore
    from autoloop.cli import main

    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@e.com")
    run_git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x\n")
    run_git(repo, "add", "f.txt")
    run_git(repo, "commit", "-q", "-m", "init")
    cfg = repo / "config.toml"
    workers_root_value = str(tmp_path / "workers_root")
    cfg.write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/abc"\n\n'
        '[policy]\nallow_push = false\n\n'
        f'[paths]\nstate_dir = ".al"\nworkers_root = "{workers_root_value}"\n',
        encoding="utf-8")
    monkeypatch.chdir(repo)

    store = BlockerStore(repo / ".al" / "blockers")
    store.save(Blocker(id="blk-(loop)-001", task_id="(loop)", kind="loop_fatal",
                       code="push_refused_protected", question="push to main?", detail="",
                       phase="executing", created_at="2026-07-31T00:00:00+00:00"))

    code = main(["answer", "blk-(loop)-001", "I enabled it, honest", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 1
    assert "NOT resolved" in out
    assert "cannot be cleared by an answer" in out
    assert store.load("blk-(loop)-001").resolved_at is None, "must stay open"


# =============================================================================
# 15. a task is `blocked` only while it has at least one OPEN blocker (blk-01)
#
# The mirror image of the retirement sweep in test_start_command.py: that one
# closes a retired task's question, this one requeues a task whose questions are
# all closed. port-01 (2026-08-19) sat `blocked` for hours with every blocker
# resolved — out of `next_ready()` with nothing left to justify it, and no
# supported command able to return it (`answer` needs an open blocker,
# `release` wants `in_progress`, `retire` means "never again", there is no
# `unblock`). The only route out was editing tasks.json by hand.
# =============================================================================


def _open_blocker(store, task_id, code, kind="task_fatal", when="2026-08-19T00:00:00+00:00"):
    return store.record(
        task_id=task_id,
        kind=kind,
        code=code,
        question=f"task {task_id} parked with {code}; what now?",
        detail="",
        phase="executing",
        now=when,
    )


def _transcript_entries(config, entry_type):
    path = config.transcript_file
    if not path.exists():
        return []
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [e for e in entries if e.get("type") == entry_type]


def _split_brain(config, task_ids=("t1",), extra_tasks=()):
    """A registry in the exact state port-01 was found in: `blocked` rows with
    no open blocker record anywhere. Returns the store and the registry."""
    registry = TaskRegistry([ready_task(tid) for tid in (*task_ids, *extra_tasks)])
    for tid in task_ids:
        registry.block(tid, f"parked: {tid} needs a human")
    store = TaskStore(config.tasks_file)
    store.save(registry)
    return store, registry


def test_answering_the_last_open_blocker_returns_the_task_to_the_queue(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",), extra_tasks=("t2",))
    blocker = _open_blocker(BlockerStore(config.blockers_dir), "t1", "review_round_cap")

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="try it again")
    ) == 0

    assert "ready again" in capsys.readouterr().out
    reloaded = store.load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.get("t1").blocked_reason == ""
    assert {t.id for t in reloaded.ready_tasks()} == {"t1", "t2"}


def test_answering_one_of_two_blockers_leaves_the_task_blocked(tmp_path, capsys):
    """`record` mints a separate blocker per (task, code, phase), so one task
    can carry two distinct questions. The first answer used to requeue it with
    the second still unanswered — the invariant runs both ways round."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    first = _open_blocker(blockers, "t1", "review_round_cap")
    second = _open_blocker(blockers, "t1", "attempt_count_ceiling")

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=first.id, text="that one is fine")
    ) == 0

    out = capsys.readouterr().out
    assert "stays blocked" in out
    assert "ready again" not in out
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert [b.id for b in blockers.open_blockers()] == [second.id]

    # ...and the second answer, which IS the last one, releases it.
    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=second.id, text="and so is that")
    ) == 0
    assert "ready again" in capsys.readouterr().out
    assert store.load().state_of("t1") is TaskState.READY


def test_an_open_loop_fatal_blocker_still_keeps_its_task_blocked(tmp_path):
    """Counting is by TASK, not by kind — deliberately wider than
    `_reconcile_retired_blockers`' `task_fatal` allowlist. That sweep CLOSES
    records so it must prove one is closeable; this one only decides whether to
    keep a task out of the queue, where the conservative direction is the
    opposite."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, registry = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    escape = _open_blocker(blockers, "t1", "checkout_escape_detected", kind="loop_fatal")

    assert cli._reconcile_unblocked_tasks(config, store, registry) == []
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR

    # Archived (never answered — an escape refuses every answer by design) and
    # the same sweep now releases it.
    blockers.archive_stale(escape.id, "session retired; every path inspected")
    released = cli._reconcile_unblocked_tasks(config, store, registry)

    assert [task_id for task_id, _ in released] == ["t1"]
    assert store.load().state_of("t1") is TaskState.READY


def test_the_sweep_reports_the_transition_and_is_idempotent(tmp_path):
    """A task returning to the queue on nobody's authority must be visible.
    `unblock()` clears `blocked_reason`, so the transcript is the only place the
    account of WHY it was quarantined survives."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, registry = _split_brain(config, task_ids=("t1",))

    released = cli._reconcile_unblocked_tasks(config, store, registry)
    assert released == [("t1", "parked: t1 needs a human")]

    entries = _transcript_entries(config, "task_auto_unblocked")
    assert len(entries) == 1
    assert entries[0]["data"]["task_id"] == "t1"
    assert entries[0]["data"]["prior_blocked_reason"] == "parked: t1 needs a human"

    # Idempotent: the task is `pending` now, so a second pass has nothing to do
    # and writes no second entry. This runs at the top of every continuous
    # iteration, so a sweep that re-fired would fill the transcript.
    assert cli._reconcile_unblocked_tasks(config, store, registry) == []
    assert len(_transcript_entries(config, "task_auto_unblocked")) == 1


def test_the_sweep_never_writes_to_a_blocker_record(tmp_path):
    """The bound `do not resolve blockers as a side effect of anything` made
    checkable — byte-for-byte, open and closed records alike."""
    from dataclasses import asdict

    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, registry = _split_brain(config, task_ids=("t1", "t2"))
    blockers = BlockerStore(config.blockers_dir)
    answered = _open_blocker(blockers, "t1", "review_round_cap")
    blockers.resolve(answered.id, "go again")          # t1: split brain
    still_open = _open_blocker(blockers, "t2", "attempt_count_ceiling")  # t2: genuine
    before = {b.id: asdict(b) for b in blockers.all_blockers()}

    released = cli._reconcile_unblocked_tasks(config, store, registry)

    assert [task_id for task_id, _ in released] == ["t1"]
    assert {b.id: asdict(b) for b in blockers.all_blockers()} == before
    assert [b.id for b in blockers.open_blockers()] == [still_open.id]
    reloaded = store.load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.BLOCKED_BY_OPERATOR


def test_an_operator_hold_is_never_swept_back_into_the_queue(tmp_path, capsys):
    """The one exclusion in the whole mechanism. An inbox hold creates NO
    blocker record by design, so "nothing open names it" is true of one from
    the instant it is placed — provenance (`hold_origin`), not the absence of a
    record, is what decides."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR

    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    registry = TaskRegistry([ready_task("held"), ready_task("t1")])
    registry.operator_block("held", "waiting on a product decision")
    registry.block("t1", "parked: t1 needs a human")
    store = TaskStore(config.tasks_file)
    store.save(registry)
    blocker = _open_blocker(BlockerStore(config.blockers_dir), "t1", "review_round_cap")

    # Directly...
    assert [t.id for t in registry.blocker_derived_blocked()] == ["t1"]

    # ...and through every command that runs the sweep.
    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="carry on")
    ) == 0
    capsys.readouterr()

    reloaded = store.load()
    assert reloaded.state_of("t1") is TaskState.READY
    held = reloaded.get("held")
    assert held.status == "blocked"
    assert held.hold_origin == HOLD_ORIGIN_OPERATOR
    assert held.blocked_reason.endswith("waiting on a product decision")
    # Scoped to `held`, not "no entries at all": since review round 3 `answer`
    # releases through the SAME reconciliation as every other caller, so `t1`
    # does get an entry. The claim here is only that `held` never does.
    assert [e["data"]["task_id"] for e in _transcript_entries(config, "task_auto_unblocked")] == [
        "t1"
    ]


def test_start_reconciles_a_registry_that_is_already_in_the_split_state(
    tmp_path, monkeypatch, capsys
):
    """`start`'s preflight, before anything selects a task — the same place the
    retirement sweep lives, and for the same reason: a registry that arrived in
    this state has no command that would otherwise notice.

    The operator hold rides along deliberately: the exclusion has to hold on the
    AUTOMATIC paths, not only on the one where a human typed `answer`."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR

    config = make_config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    monkeypatch.setattr(cli, "_default_probe_cdp", lambda url: '{"Browser":"Chrome"}')
    store, registry = _split_brain(config, task_ids=("t1",), extra_tasks=("held",))
    registry.operator_block("held", "waiting on a product decision")
    store.save(registry)

    assert cli._cmd_start(Namespace(config=None, check_only=True)) == 0

    out = capsys.readouterr().out
    assert "t1 returned to the queue" in out
    assert "no open blocker" in out
    assert "held returned to the queue" not in out
    reloaded = store.load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.get("held").status == "blocked"
    assert reloaded.get("held").hold_origin == HOLD_ORIGIN_OPERATOR


def test_continuous_mode_reconciles_at_the_top_of_an_iteration(tmp_path, monkeypatch, capsys):
    """The loop's own startup sweep, wired for real — and on the registry the
    iteration hands the orchestrator, not a copy: a task released into a copy
    would still read `blocked` where `next_ready()` looks, and the round's first
    ordinary save would write the stale status straight back.

    `t1` depends on an in-progress `t0`, so releasing it produces no READY task
    and the iteration reaches the exhaustion check instead of kicking off a
    session; the `(loop)` blocker is what makes that check exit 0. The operator
    hold rides along because this is the path that runs unattended for hours —
    the one where an accidental release would go unnoticed longest."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR

    repo_root = real_repo(tmp_path)
    monkeypatch.chdir(repo_root)
    config = make_config(tmp_path)

    registry = TaskRegistry([ready_task("t0"), ready_task("t1"), ready_task("held")])
    registry.set_depends_on("t1", ("t0",))
    registry.mark_in_progress("t0")
    registry.block("t1", "parked: post-commit validation failed")
    registry.operator_block("held", "waiting on a product decision")
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    _open_blocker(BlockerStore(config.blockers_dir), NO_TASK, "login_expired",
                  kind="loop_fatal")
    cli._save_fingerprint(config, cli.repo_fingerprint(repo_root))

    def boom_conversation(*a, **kw):
        raise AssertionError("no ChatGPT call expected")

    def boom_claude(self, spec):
        raise AssertionError("no Claude call expected")

    monkeypatch.setattr(cli, "create_conversation", boom_conversation)
    monkeypatch.setattr(ClaudeCliRunner, "run", boom_claude)

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    assert cli._run_continuous(args, config) == 0

    out = capsys.readouterr().out
    assert "t1 returned to the queue" in out
    assert "held returned to the queue" not in out
    reloaded = task_store.load()
    assert reloaded.get("t1").status == "pending"
    # Still not dispatchable — the dependency is real and the sweep does not
    # touch it. `blocked` here is the derived kind, which resolves itself.
    assert reloaded.state_of("t1") is TaskState.BLOCKED
    assert reloaded.get("held").status == "blocked"
    assert reloaded.get("held").hold_origin == HOLD_ORIGIN_OPERATOR
    assert [e["data"]["task_id"] for e in _transcript_entries(config, "task_auto_unblocked")] == ["t1"]


def test_archiving_the_last_blocker_returns_its_task_to_the_queue(tmp_path, capsys):
    """The `answer` invariant on the other closing path. `checkout_escape_
    detected` refuses every answer by design, so archival is the ONLY way its
    record ever closes — and a task whose last record closes that way is as
    unjustifiably quarantined as one whose last record was answered."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    stale = _open_blocker(blockers, "t1", "checkout_escape_detected", kind="loop_fatal")

    assert cli._cmd_archive_blocker(
        Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
    ) == 0

    assert "t1 returned to the queue" in capsys.readouterr().out
    closed = blockers.load(stale.id)
    assert closed.archived_reason and closed.answer is None, "machine reason, not an answer"
    reloaded = store.load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert [t.id for t in reloaded.ready_tasks()] == ["t1"]
    assert [e["data"]["task_id"] for e in _transcript_entries(config, "task_auto_unblocked")] == [
        "t1"
    ]


def test_a_live_lock_refuses_the_whole_archival_rather_than_half_of_it(tmp_path, capsys):
    """The command writes `tasks.json` now, so it takes the loop lock — and
    when it cannot, NOTHING moves.

    The assertion that matters is on the BLOCKER, not the task: an archival
    that lands while the requeue is skipped leaves exactly the state this whole
    mechanism exists to make impossible (`blocked` with no open record), and
    "the task is still blocked" is equally true of that broken outcome. Holding
    the lock in-process is a real live lock — same pid, no `exec_handoff`
    marker, so `acquire` refuses it like any other."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    stale = _open_blocker(blockers, "t1", "checkout_escape_detected", kind="loop_fatal")

    with LoopLock(config.state_dir):
        assert cli._cmd_archive_blocker(
            Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
        ) == 1
        out = capsys.readouterr().out

    assert "NOT archived" in out and "nothing changed" in out
    assert blockers.load(stale.id).resolved_at is None, "the record must still be open"
    assert [b.id for b in blockers.open_blockers()] == [stale.id]
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert not _transcript_entries(config, "task_auto_unblocked")

    # Released: the same command now moves both halves together.
    assert cli._cmd_archive_blocker(
        Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
    ) == 0
    assert "t1 returned to the queue" in capsys.readouterr().out
    assert blockers.load(stale.id).resolved_at is not None
    assert store.load().state_of("t1") is TaskState.READY


def test_a_stale_lock_refuses_it_too_and_names_the_recovery(tmp_path, capsys):
    """A dead session is exactly what leaves a lock behind, and it is exactly
    when this command gets used — so the refusal has to name `unlock` rather
    than read as a bug. Never `break_stale`: locks are not stolen here."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    stale = _open_blocker(blockers, "t1", "checkout_escape_detected", kind="loop_fatal")
    lock = LoopLock(config.state_dir)
    lock.state_dir.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("{ not json", encoding="utf-8")  # unreadable → provably not live

    assert cli._cmd_archive_blocker(
        Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
    ) == 1

    out = capsys.readouterr().out
    assert "unlock" in out and "NOT archived" in out
    assert lock.path.exists(), "refused, not recovered — `unlock` is the operator's call"
    assert blockers.load(stale.id).resolved_at is None
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR


# =============================================================================
# blk-01, review round 3 — the two CLOSING commands fail closed.
#
# Taking the lock removed the RACE, but both commands still closed the record
# first and requeued best-effort afterwards: a `tasks.json` that would not
# parse, a reconciliation that raised, or a save that failed left the blocker
# durably CLOSED with its task still `blocked` — the exact split state the whole
# mechanism exists to make impossible, with a warning printed over it and a
# promise that some later startup would notice.
#
# Scope is synchronous command failure, not crash consistency: a process killed
# between the two writes still leaves the split state, and the startup sweeps
# (`_cmd_start`, `_run_locked`, `_run_continuous`) stay deliberately TOLERANT
# because they have no blocker of their own to put back.
# =============================================================================


def _boom_reconcile(monkeypatch, exc):
    """Fail the reconciliation itself, AFTER the lock is taken and after the
    record has been closed — the window the old shape left open."""

    def boom(*_args, **_kwargs):
        raise exc

    monkeypatch.setattr(cli, "_reconcile_unblocked_tasks", boom)


def _boom_task_save(monkeypatch):
    """Fail the real `tasks.json` write, so the reconciliation runs for real and
    only the durable half is refused."""

    def boom(self, registry):
        raise StateError("tasks.json could not be written")

    monkeypatch.setattr(TaskStore, "save", boom)


def test_answering_fails_closed_when_the_task_graph_cannot_be_reconciled(
    tmp_path, monkeypatch, capsys
):
    """A resolution that cannot requeue what it was holding is not a resolution.

    `KeyError` is the shape `_cmd_start` names — a `depends_on` naming a task
    that no longer exists survives `from_dict` and fails on the later lookup —
    and it used to be swallowed with "the loop's next start will fix it", which
    is the invariant restated as a promise about another process."""
    from dataclasses import asdict

    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    blocker = _open_blocker(blockers, "t1", "review_round_cap")
    before = asdict(blockers.load(blocker.id))
    _boom_reconcile(monkeypatch, KeyError("t0"))

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="try it again")
    ) == 1

    out = capsys.readouterr().out
    assert f"blocker {blocker.id} resolved." not in out, "not reported as a close"
    assert "NOT resolved" in out and "was reopened" in out
    assert asdict(blockers.load(blocker.id)) == before, "restored byte-for-byte"
    assert blockers.load(blocker.id).answer is None
    assert [b.id for b in blockers.open_blockers()] == [blocker.id]
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert not _transcript_entries(config, "task_auto_unblocked")

    # And the reopened record is genuinely answerable again — a restore that
    # left it unusable would just be the split brain one file over.
    monkeypatch.undo()
    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="try it again")
    ) == 0
    assert "ready again" in capsys.readouterr().out
    assert store.load().state_of("t1") is TaskState.READY


def test_answering_fails_closed_when_tasks_json_cannot_be_saved(
    tmp_path, monkeypatch, capsys
):
    """The same rule with the reconciliation running for real and only the
    durable write refused — and the fault budget, which used to be refilled
    before the task graph was even read, stays spent."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    blocker = _open_blocker(blockers, "t1", "fault_attempt_ceiling")
    executions = TaskExecutionStore(config.executions_dir)
    executions.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path="/tmp/wt",
            task_base_sha="a" * 40,
            attempt_count=1,
            fault_attempt_count=3,
        )
    )
    _boom_task_save(monkeypatch)

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="network is fine")
    ) == 1

    out = capsys.readouterr().out
    assert f"blocker {blocker.id} resolved." not in out
    assert "NOT resolved" in out
    assert blockers.load(blocker.id).resolved_at is None
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert not _transcript_entries(config, "task_auto_unblocked")
    assert executions.load("t1").fault_attempt_count == 3, "budget not refilled"


def test_archiving_fails_closed_when_the_task_graph_cannot_be_reconciled(
    tmp_path, monkeypatch, capsys
):
    """`checkout_escape_detected` refuses every answer by design, so archival is
    the only way its record ever closes — and it is bound by the same rule."""
    from dataclasses import asdict

    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    stale = _open_blocker(blockers, "t1", "checkout_escape_detected", kind="loop_fatal")
    before = asdict(blockers.load(stale.id))
    _boom_reconcile(monkeypatch, TaskGraphError("unknown_task", "t0 is not in the registry"))

    assert cli._cmd_archive_blocker(
        Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
    ) == 1

    out = capsys.readouterr().out
    assert "archived at" not in out, "not reported as a close"
    assert "NOT archived" in out and "was reopened" in out
    assert asdict(blockers.load(stale.id)) == before, "restored byte-for-byte"
    assert blockers.load(stale.id).archived_reason == ""
    assert [b.id for b in blockers.open_blockers()] == [stale.id]
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert not _transcript_entries(config, "task_auto_unblocked")

    monkeypatch.undo()
    assert cli._cmd_archive_blocker(
        Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
    ) == 0
    assert "t1 returned to the queue" in capsys.readouterr().out
    assert store.load().state_of("t1") is TaskState.READY


def test_archiving_fails_closed_when_tasks_json_cannot_be_saved(
    tmp_path, monkeypatch, capsys
):
    """The durable-write half for `archive-blocker`, matching `answer`'s."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    stale = _open_blocker(blockers, "t1", "checkout_escape_detected", kind="loop_fatal")
    _boom_task_save(monkeypatch)

    assert cli._cmd_archive_blocker(
        Namespace(config=config_path, blocker_id=stale.id, reason="session retired")
    ) == 1

    out = capsys.readouterr().out
    assert "archived at" not in out
    assert "NOT archived" in out
    assert blockers.load(stale.id).resolved_at is None
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert not _transcript_entries(config, "task_auto_unblocked")


def test_a_reopen_that_itself_fails_says_the_record_is_still_closed(
    tmp_path, monkeypatch, capsys
):
    """The one state nothing here can repair by itself. It must be reported in
    the terms that matter — the record is CLOSED and the task was not requeued —
    rather than raising a traceback out of a command that already wrote.

    And specifically NOT as "was NOT resolved": the answer really is on disk in
    this branch, so that line would be the false one `_cmd_answer` is written to
    avoid, and an operator who believed it would answer the blocker twice."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    store, _ = _split_brain(config, task_ids=("t1",))
    blockers = BlockerStore(config.blockers_dir)
    blocker = _open_blocker(blockers, "t1", "review_round_cap")
    _boom_reconcile(monkeypatch, KeyError("t0"))

    # Only the RESTORE write fails: `resolve` writes a record with `resolved_at`
    # set, `_reopen_blocker` writes one without. Failing both would break the
    # close itself and never reach the branch under test.
    real_save = BlockerStore.save

    def selective_save(self, record):
        if record.resolved_at is None:
            raise OSError("blockers/ is read-only")
        real_save(self, record)

    monkeypatch.setattr(BlockerStore, "save", selective_save)

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="try it again")
    ) == 1

    out = capsys.readouterr().out
    assert "could NOT be reopened" in out
    assert "remains CLOSED (resolved) on disk" in out
    assert "NOT resolved" not in out, "the answer IS on disk — saying otherwise is false"
    # Both halves of the state it reports, read back from disk.
    reopened = blockers.load(blocker.id)
    assert reopened.resolved_at is not None and reopened.answer == "try it again"
    assert store.load().state_of("t1") is TaskState.BLOCKED_BY_OPERATOR


def test_the_startup_sweeps_stay_tolerant_of_an_unreadable_task_graph(
    tmp_path, monkeypatch, capsys
):
    """The narrowing that keeps this change to the two CLOSING commands. A
    startup sweep has no blocker of its own to put back, so refusing to start
    would trade a repairable state for an unstartable loop — `start` reports the
    unreadable graph and carries on, exactly as before."""
    config = make_config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    monkeypatch.setattr(cli, "_default_probe_cdp", lambda url: '{"Browser":"Chrome"}')
    _split_brain(config, task_ids=("t1",))
    _boom_reconcile(monkeypatch, KeyError("t0"))

    # Exit 2 is `start`'s "the items above need a decision" — a REPORT, not a
    # traceback out of the command whose whole job is reporting findings.
    assert cli._cmd_start(Namespace(config=None, check_only=True)) == 2
    out = capsys.readouterr().out
    assert "UNREADABLE" in out
    assert "NOT started" in out


# =============================================================================
# blk-02 — with several blockers open, WHICH one is "the" blocker is decided
# by severity, never by directory order.
#
# Observed 2026-08-21: `blk-(loop)-038` (loop_fatal, parse_budget_exhausted,
# 09:25:18) and `blk-blk-01-003` (task_fatal, task_base_behind_head, 09:19:26)
# were open together, and an operator recovery script iterating
# `blockers/*.json` acted on the SECOND because that is what the glob returned
# first. It failed safe that time; with the codes swapped it would have
# "recovered" the loop-fatal one.
#
# The fixtures below deliberately make filename order AND recency point at the
# wrong record — `blk-t-a-001.json` sorts before `blk-t-b-001.json` (and is the
# newer of the two), while the loop_fatal one is `t-b`. Only severity can pick
# the loop_fatal record, so reverting the ordering fails these rather than
# passing by coincidence.
# =============================================================================


def _severity_pair(store):
    """The incident, arranged so every OTHER available order is wrong:
    task_fatal first by filename and newer by timestamp, loop_fatal second and
    older. Returns (loop_fatal, task_fatal)."""
    task_fatal = _open_blocker(
        store, "t-a", "task_base_behind_head",
        kind="task_fatal", when="2026-08-21T09:25:18+00:00",
    )
    loop_fatal = _open_blocker(
        store, "t-b", "parse_budget_exhausted",
        kind="loop_fatal", when="2026-08-21T09:19:26+00:00",
    )
    assert [p.stem for p in sorted(store.directory.glob("blk-*.json"))] == [
        task_fatal.id, loop_fatal.id
    ], "the fixture is only meaningful while the glob lists the WRONG one first"
    return loop_fatal, task_fatal


def test_loop_fatal_outranks_task_fatal_whatever_the_directory_says(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    loop_fatal, task_fatal = _severity_pair(store)

    assert store.primary_blocker().id == loop_fatal.id
    assert [b.id for b in store.open_blockers_by_severity()] == [
        loop_fatal.id, task_fatal.id
    ]


def test_the_real_incident_pair_also_selects_the_loop_fatal_one(tmp_path):
    """The ids as actually observed. `(` sorts before `b`, so the glob happened
    to list this loop_fatal record first — the ordering must agree for the
    RIGHT reason, not stop being exercised because the accident was benign."""
    store = BlockerStore(tmp_path / "blockers")
    loop_fatal = _open_blocker(
        store, NO_TASK, "parse_budget_exhausted",
        kind="loop_fatal", when="2026-08-21T09:25:18+00:00",
    )
    _open_blocker(
        store, "blk-01", "task_base_behind_head",
        kind="task_fatal", when="2026-08-21T09:19:26+00:00",
    )
    assert store.primary_blocker().id == loop_fatal.id == f"blk-{NO_TASK}-001"


def test_no_number_of_task_fatal_blockers_outranks_one_loop_fatal(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    for n, tid in enumerate(("t-a", "t-b", "t-c", "t-d")):
        _open_blocker(store, tid, "review_round_cap", when=f"2026-08-21T10:0{n}:00+00:00")
    loop_fatal = _open_blocker(
        store, "t-z", "login_expired", kind="loop_fatal",
        when="2026-08-20T00:00:00+00:00",  # older than every task_fatal above
    )

    assert store.primary_blocker().id == loop_fatal.id
    assert len(store.open_blockers_by_severity()) == 5


def test_within_one_kind_the_most_recent_wins(tmp_path):
    """Recency is the honest tiebreak inside a severity: the newest record
    describes the state the loop actually reached."""
    store = BlockerStore(tmp_path / "blockers")
    older = _open_blocker(store, "t-a", "review_round_cap", when="2026-08-21T09:00:00+00:00")
    newer = _open_blocker(store, "t-b", "attempt_count_ceiling", when="2026-08-21T11:00:00+00:00")

    assert store.primary_blocker().id == newer.id
    assert [b.id for b in store.open_blockers_by_severity()] == [newer.id, older.id]


def test_identical_timestamps_order_stably_by_blocker_id(tmp_path):
    """Two blockers written in the same second. The id is monotonic per task
    and ascending, so the order is defined rather than glob-dependent — and
    repeat calls agree."""
    store = BlockerStore(tmp_path / "blockers")
    same_second = "2026-08-21T09:25:18+00:00"
    second = _open_blocker(store, "t-b", "review_round_cap", when=same_second)
    first = _open_blocker(store, "t-a", "review_round_cap", when=same_second)

    ranked = [b.id for b in store.open_blockers_by_severity()]
    assert ranked == sorted([first.id, second.id])
    assert ranked == [b.id for b in store.open_blockers_by_severity()], "stable"
    assert store.primary_blocker().id == first.id


def test_an_unparseable_created_at_never_wins_the_recency_tiebreak(tmp_path):
    """A record we cannot date must not be promoted for being unreadable — it
    sorts as the OLDEST of its kind, and is still listed."""
    store = BlockerStore(tmp_path / "blockers")
    dated = _open_blocker(store, "t-a", "review_round_cap", when="2026-01-01T00:00:00+00:00")
    undated = _open_blocker(store, "t-b", "review_round_cap", when="not a timestamp")

    assert store.primary_blocker().id == dated.id
    assert {b.id for b in store.open_blockers_by_severity()} == {dated.id, undated.id}


def test_an_unrecognised_kind_ranks_with_loop_fatal(tmp_path):
    """Fail-closed, matching `_to_needs_user`'s `kind="loop_fatal"` default
    (AUTOLOOP.md §9c): a severity we cannot read is treated as the
    loop-stopping one rather than sorted below every classified record, where
    an operator reading the primary would never see it."""
    store = BlockerStore(tmp_path / "blockers")
    _open_blocker(store, "t-a", "review_round_cap", when="2026-08-21T12:00:00+00:00")
    strange = _open_blocker(
        store, "t-b", "written_by_a_future_version",
        kind="catastrophic", when="2026-08-21T09:00:00+00:00",
    )

    assert store.primary_blocker().id == strange.id


def test_a_resolved_or_archived_blocker_is_never_primary(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    answered = _open_blocker(
        store, "t-a", "login_expired", kind="loop_fatal", when="2026-08-21T12:00:00+00:00"
    )
    archived = _open_blocker(
        store, "t-b", "checkout_escape_detected", kind="loop_fatal",
        when="2026-08-21T11:00:00+00:00",
    )
    still_open = _open_blocker(store, "t-c", "review_round_cap", when="2026-08-21T09:00:00+00:00")
    store.resolve(answered.id, "dealt with")
    store.archive_stale(archived.id, "session retired")

    # Both closed records outrank `still_open` on severity AND recency — only
    # the open filter keeps them out.
    assert store.primary_blocker().id == still_open.id
    assert [b.id for b in store.open_blockers_by_severity()] == [still_open.id]


def test_nothing_open_has_no_primary(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    assert store.primary_blocker() is None
    assert store.open_blockers_by_severity() == []
    # And a never-created directory answers the same way rather than raising.
    assert BlockerStore(tmp_path / "never").primary_blocker() is None


def test_exactly_one_open_blocker_is_that_blocker(tmp_path):
    """The overwhelmingly common case: ranking must be a no-op on it."""
    store = BlockerStore(tmp_path / "blockers")
    only = _open_blocker(store, "t-a", "review_round_cap")

    assert store.primary_blocker().id == only.id
    assert store.open_blockers_by_severity() == store.open_blockers() == [only]


def test_ranking_hides_nothing_and_changes_no_count(tmp_path):
    """`open_blockers` itself is left in its documented id order — the ranking
    is a second view of the SAME set, not a filter and not a replacement."""
    store = BlockerStore(tmp_path / "blockers")
    loop_fatal, task_fatal = _severity_pair(store)

    assert [b.id for b in store.open_blockers()] == [task_fatal.id, loop_fatal.id]
    assert len(store.open_blockers_by_severity()) == len(store.open_blockers()) == 2
    assert {b.id for b in store.open_blockers_by_severity()} == {
        b.id for b in store.open_blockers()
    }
    assert store.open_task_ids() == {"t-a", "t-b"}, "the whole-set readers are untouched"


def test_by_severity_is_the_same_order_the_store_uses(tmp_path):
    """The list-in-hand helper and the store method are one implementation —
    the property `cli._print_blocker_summary` relies on."""
    store = BlockerStore(tmp_path / "blockers")
    _severity_pair(store)

    assert [b.id for b in by_severity(store.open_blockers())] == [
        b.id for b in store.open_blockers_by_severity()
    ]


def test_a_corrupt_record_still_raises_rather_than_ranking_around_it(tmp_path):
    """Reads through `open_blockers`, so the crash-safety rule is unchanged: a
    record that will not decode must not read as 'nothing more urgent'."""
    directory = tmp_path / "blockers"
    directory.mkdir(parents=True)
    (directory / "blk-t1-001.json").write_text("{not json", encoding="utf-8")
    store = BlockerStore(directory)

    with pytest.raises(StateCorruptError):
        store.primary_blocker()
    with pytest.raises(StateCorruptError):
        store.open_blockers_by_severity()


# --- the operator-facing surfaces ---------------------------------------------


def test_status_names_the_primary_and_says_how_many_else_are_open(tmp_path):
    config = make_config(tmp_path)
    loop_fatal, task_fatal = _severity_pair(BlockerStore(config.blockers_dir))

    lines, ok = cli._report_blockers_and_phase(config)
    text = "\n".join(lines)

    assert ok is False
    assert "2 OPEN" in text, "the count is the count"
    assert f"{loop_fatal.id} is primary" in text
    assert "1 other(s) also open" in text
    # Nothing is hidden: both records still get their own block.
    assert f"{loop_fatal.id} ({loop_fatal.kind}/{loop_fatal.code})" in text
    assert f"{task_fatal.id} ({task_fatal.kind}/{task_fatal.code})" in text
    # And the primary is listed before the one it outranks.
    assert text.index(loop_fatal.id) < text.index(task_fatal.id)


def test_status_output_for_a_single_blocker_is_unchanged(tmp_path):
    """Byte-exact, not a substring check: a 'primary' line leaking into the
    common case would trade one wrong impression for another."""
    config = make_config(tmp_path)
    only = _open_blocker(BlockerStore(config.blockers_dir), "t-a", "review_round_cap")

    lines, ok = cli._report_blockers_and_phase(config)

    assert ok is False
    assert lines[:4] == [
        "blockers     1 OPEN — each needs a decision:",
        f"               {only.id} ({only.kind}/{only.code})",
        f"               {only.question[:160]}",
        f'               resolve: python -m autoloop answer {only.id} "..."',
    ]


def test_the_blockers_command_lists_open_ones_most_severe_first(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    loop_fatal, task_fatal = _severity_pair(BlockerStore(config.blockers_dir))

    assert cli._cmd_blockers(Namespace(config=config_path, all=False)) == 0
    out = capsys.readouterr().out

    assert out.index(loop_fatal.id) < out.index(task_fatal.id)
    assert task_fatal.id in out, "ranking is a reading order, never a shortlist"


def test_the_exhaustion_summary_ranks_and_still_prints_everything(capsys):
    """`_print_blocker_summary` takes a list, so it is the place a second
    caller-local `sorted()` would creep back in."""
    loop_fatal = Blocker(
        id="blk-t-b-001", task_id="t-b", kind="loop_fatal",
        code="parse_budget_exhausted", question="the reply could not be parsed",
        detail="", phase="reviewing", created_at="2026-08-21T09:19:26+00:00",
    )
    task_fatal = Blocker(
        id="blk-t-a-001", task_id="t-a", kind="task_fatal",
        code="task_base_behind_head", question="t-a is behind HEAD",
        detail="", phase="executing", created_at="2026-08-21T09:25:18+00:00",
    )

    cli._print_blocker_summary([task_fatal, loop_fatal])
    out = capsys.readouterr().out

    assert out.index(loop_fatal.id) < out.index(task_fatal.id)
    assert "2 blocker(s) are still open" in out
    assert f"{loop_fatal.id} is the primary one" in out
    assert "other 1 above are open too" in out
    assert task_fatal.question in out


def test_the_exhaustion_summary_for_a_single_blocker_is_unchanged(capsys):
    only = Blocker(
        id="blk-t-a-001", task_id="t-a", kind="task_fatal", code="review_round_cap",
        question="t-a hit the review round cap", detail="", phase="executing",
        created_at="2026-08-21T09:25:18+00:00",
    )

    cli._print_blocker_summary([only])
    out = capsys.readouterr().out

    assert "1 blocker(s) are still open" in out
    assert f"  {only.id}  task=t-a  code=review_round_cap  {only.question}" in out
    assert "primary" not in out
