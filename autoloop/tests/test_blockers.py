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
from autoloop.blockers import NO_TASK, Blocker, BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, load_config
from autoloop.contract import Decision, Directive
from autoloop.errors import StateCorruptError, StateError, TaskGraphError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore, utcnow_iso
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
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
        tmp_path, executor, task_ids=("t1", "t2"), blocker_store=blocker_store
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
        tmp_path, executor, task_ids=("t1",), blocker_store=blocker_store
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
