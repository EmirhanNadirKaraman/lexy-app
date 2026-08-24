"""Accepting a split plan atomically across the registry, the execution record
and the worker repository (split-02, 2026-08-24).

THE CLAIM under test, and it is a crash-consistency claim rather than a happy
path one: a split is accepted across three stores that cannot be written
together, and no process death between any two of them can leave them
permanently disagreeing. The mechanism is a durable `worktask.SplitIntent`
written before the first store is touched and cleared only after all three have
been READ BACK and found to agree, plus an idempotent reconciler
(`orchestrator.apply_split_intent`) re-run at the top of every `_step_ready`.

The tests that carry the claim are the four under "crash at every write
boundary". Each one drives the REAL dispatch with one store subclassed to raise
a `_Crash` immediately AFTER its write really landed — so the intermediate state
on disk is the one production actually produces, not one the test hand-built —
then throws the whole process away (a fresh `LoopState`, a registry re-read from
`tasks.json`, plain stores) and requires the next start to reach ONE consistent
accepted-split state. `_Crash` derives from `BaseException` precisely so no
`except Exception` anywhere in the loop can quietly absorb it and turn a
simulated crash into a tested error path.

Real git, real worker repos and real on-disk stores throughout — the claim is
about what is on disk afterwards — with the small `run_git` / executor / build
helpers duplicated per this suite's self-contained convention.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import (
    CONTRACT_INSTRUCTIONS,
    Decision,
    Directive,
    TaskSpec,
    parse_response,
)
from autoloop.errors import ContractError, StateCorruptError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    SPLIT_RETIREMENT_REASON,
    MAX_SPLIT_REASON_CHARS,
    Orchestrator,
    apply_split_intent,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    IntentStore,
    SplitIntent,
    SplitIntentStore,
    SplitSuccessor,
    TaskExecutionStore,
    retire_execution,
    retirement_label,
    split_intents_dir,
)

URL = "https://chatgpt.com/c/test-conversation"

PATHS = ("docs/A.md", "docs/B.md")

#: The ceiling `test_contract.test_contract_stays_within_its_budget` pins, kept
#: here too because this suite is what MOVED it — see
#: `test_the_contract_instruction_ceiling_is_not_slack` for the accounting.
CONTRACT_CEILING = 4750


class _Crash(BaseException):
    """A simulated process death, deliberately NOT an `Exception`.

    Every failure branch in `apply_split_intent` catches a named exception type;
    a crash that any of them could catch would be testing the error path instead
    of the crash path, and the two have opposite requirements (an error path
    reports and keeps the intent; a crash reports nothing at all).
    """


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


def block(obj) -> str:
    return "Reasoning...\n```json\n" + json.dumps(obj) + "\n```"


def implement_block(task_id="t1", files=("docs/A.md",)):
    return block(
        {
            "version": 3,
            "decision": "implement",
            "reason": "next",
            "task_id": task_id,
            "decomposition": {
                "approach": "one commit",
                "files": list(files),
                "steps": ["write the file"],
            },
        }
    )


def stop_block(reason="all done"):
    return block({"version": 3, "decision": "stop", "reason": reason})


def spec(tid, title=None, description="desc", depends_on=(), approved_paths=PATHS):
    return TaskSpec(
        id=tid,
        title=title or f"Title {tid}",
        description=description,
        depends_on=tuple(depends_on),
        approved_paths=tuple(approved_paths),
    )


def split_directive(parent="t1", successors=("t1a", "t1b"), reason="too big"):
    return Directive(
        decision=Decision.PLAN,
        reason=reason,
        tasks=tuple(s if isinstance(s, TaskSpec) else spec(s) for s in successors),
        split_of=parent,
    )


def split_block(parent="t1", successors=("t1a", "t1b"), reason="too big"):
    return block(
        {
            "version": 3,
            "decision": "plan",
            "reason": reason,
            "split_of": parent,
            "tasks": [
                {
                    "id": tid,
                    "title": f"Title {tid}",
                    "description": "desc",
                    "approved_paths": list(PATHS),
                }
                for tid in successors
            ],
        }
    )


class FakeClient:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.closed = False

    def attach(self):
        pass

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        from autoloop.browser.chatgpt import SubmitResult

        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        self.closed = True


class WritingExecutor:
    def __init__(self, workers_root, files=None, status="ok"):
        self.workers_root = Path(workers_root)
        self.files = dict(files or {"docs/A.md": "# first"})
        self.status = status
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        self.calls.append((directive, task))
        worker = self.workers_root / task.id
        for rel, content in self.files.items():
            target = worker / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"{content}\n<!-- call {len(self.calls)} -->\n", encoding="utf-8"
            )
        return ExecutionOutcome(
            status=self.status,
            summary="did it",
            details="details",
            validation="ruff clean",
            changed_paths=tuple(self.files),
        )


# ---- crash injectors --------------------------------------------------------
#
# Each performs the REAL write and then dies. Subclasses rather than mocks so the
# state the next start finds is the state production leaves; a mock that skipped
# the write would be testing a state that never occurs.


class CrashAfterIntentSave(SplitIntentStore):
    def save(self, intent):
        super().save(intent)
        raise _Crash("died after writing the split intent")


class CrashAfterRegistrySave(TaskStore):
    def save(self, registry):
        super().save(registry)
        raise _Crash("died after saving the registry")


class CrashAfterArchive(TaskExecutionStore):
    def archive(self, task_id, label):
        super().archive(task_id, label)
        raise _Crash("died after archiving the execution record")


class CrashAfterQuarantine(WorkerRepoManager):
    def quarantine(self, task_id, label):
        super().quarantine(task_id, label)
        raise _Crash("died after quarantining the worker repo")


class NoopQuarantine(WorkerRepoManager):
    """Reports a quarantine that did not happen — the fail-open shape the
    read-back verification exists to refuse."""

    def quarantine(self, task_id, label):
        return self.root_dir.parent / "quarantine" / f"{task_id}-{label}"


class UnresolvableWorkerPath(WorkerRepoManager):
    """A manager that cannot say WHERE this task's worker repository would be.

    Not hypothetical: `WorkerRepoManager.path_for` calls `validate_task_id`
    first, so any id `tasks.json` holds that the validator refuses — an
    operator's hand-edit, a row from an older build — answers exactly like this.
    A reader that swallowed it would report "no worker repository" about a
    directory it never managed to name.
    """

    def path_for(self, task_id):
        raise ValueError(f"unsafe task id {task_id!r}")


class MutePathAfterQuarantine(WorkerRepoManager):
    """Answers `path_for` until the worker has been quarantined, and then stops.

    Contrived, and deliberately so: the real `quarantine` (and `retire_execution`
    before it) calls `path_for` itself, so a manager that never answers fails on
    a path `apply_split_intent` already covers by catching `ValueError` from the
    retirement. Going mute AFTERWARDS is the only way to reach the worker branch
    of the final read-back — which is the branch that decides whether the intent,
    the only record that anything was in flight, may be deleted.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._quarantined = False

    def quarantine(self, task_id, label):
        dest = super().quarantine(task_id, label)
        self._quarantined = True
        return dest

    def path_for(self, task_id):
        if self._quarantined:
            raise ValueError(f"unsafe task id {task_id!r}")
        return super().path_for(task_id)


class ScopeDriftingTaskStore(TaskStore):
    """Persists a WIDER scope for `t1a` than the registry it was handed carries.

    The registry counterpart of `NoopQuarantine`, and the purest form of the
    fail-open the final read-back exists to refuse: the in-memory mutation is
    exactly right, every write returns cleanly, and the file the NEXT process
    will read authorizes writes nobody approved. A verification that asked the
    just-mutated registry — or that asked the file only whether the ids were
    there — would answer yes and delete the intent.
    """

    def save(self, registry):
        super().save(registry)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for row in data["tasks"]:
            if row["id"] == "t1a":
                row["approved_paths"] = ["docs/EVERYTHING.md"]
        self.path.write_text(json.dumps(data), encoding="utf-8")


class StatusDriftingTaskStore(TaskStore):
    """Persists `t1a` as RETIRED however live the registry it was handed says it
    is — the same fail-open shape as `ScopeDriftingTaskStore`, one field over.

    The field the intent cannot record, so the mutation's trial (which runs on an
    in-memory copy that is exactly right) has nothing to catch. Only the file
    disagrees, and the file is what the next process reads and what the intent is
    about to be deleted on the strength of.
    """

    def save(self, registry):
        super().save(registry)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for row in data["tasks"]:
            if row["id"] == "t1a":
                row["status"] = "retired"
        self.path.write_text(json.dumps(data), encoding="utf-8")


@dataclass
class Wiring:
    orch: Orchestrator
    git: GitGateway
    registry: TaskRegistry
    task_store: TaskStore
    execution_store: TaskExecutionStore
    worker_repos: WorkerRepoManager
    executor: WritingExecutor
    config: AutoloopConfig
    store: StateStore
    client: FakeClient
    tmp_path: Path


def make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def build(
    tmp_path,
    responses=(),
    tasks=(),
    reuse=None,
    no_artifact_stores=False,
    worker_repos_cls=None,
) -> Wiring:
    """A real-git Orchestrator with real worker repos and real stores.

    `reuse` rebuilds over an EXISTING wiring's paths with a fresh `LoopState`,
    plain (non-crashing) stores and a registry re-read from `tasks.json` — the
    shape of the next process after a crash, and the only construction in which
    "the split finished by itself" means anything.
    """
    if reuse is not None:
        repo_root = reuse.git.repo_root
        config = reuse.config
        worker_repos = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
        execution_store = TaskExecutionStore(tmp_path / "executions")
        task_store = TaskStore(config.tasks_file)
        registry = task_store.load()
        git = reuse.git
    else:
        repo_root = make_repo(tmp_path)
        policy_config = PolicyConfig(implement_enabled=True)
        git = GitGateway(repo_root, PolicyEngine(policy_config))
        worker_repos = (worker_repos_cls or WorkerRepoManager)(
            tmp_path / "workers", tmp_path / "worker-hooks"
        )
        execution_store = TaskExecutionStore(tmp_path / "executions")
        config = AutoloopConfig(
            browser=BrowserConfig(conversation_url=URL),
            policy=policy_config,
            state_dir=tmp_path / ".al",
        )
        task_store = TaskStore(config.tasks_file)
        registry = TaskRegistry(list(tasks))
        task_store.save(registry)

    state = LoopState.new(URL)
    state.outbox = "kickoff report"
    store = StateStore(config.state_file)
    store.save(state)

    executor = WritingExecutor(worker_repos.root_dir)
    client = FakeClient(responses)
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
        worker_repos=None if no_artifact_stores else worker_repos,
        execution_store=None if no_artifact_stores else execution_store,
        intent_store=IntentStore(tmp_path / "intents"),
        validation_runner=ok_validation,
    )
    return Wiring(
        orch=orch,
        git=git,
        registry=registry,
        task_store=task_store,
        execution_store=execution_store,
        worker_repos=worker_repos,
        executor=executor,
        config=config,
        store=store,
        client=client,
        tmp_path=tmp_path,
    )


def ready_task(tid="t1", **kwargs):
    return Task(
        id=tid, title=f"Title {tid}", description="desc", approved_paths=PATHS, **kwargs
    )


def records(wiring, kind):
    path = wiring.config.transcript_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == kind:
            out.append(entry.get("data") or {})
    return out


def denial_codes(wiring):
    return [record.get("code") for record in records(wiring, "policy_denied")]


def park_codes(wiring):
    """Every park this wiring recorded, by code — `_to_needs_user` puts it in the
    transcript and nowhere else, and it is the only place "I found residue" and
    "I could not look" are told apart without reading prose."""
    return [record.get("code") for record in records(wiring, "needs_user")]


def intent_files(wiring):
    directory = split_intents_dir(wiring.config.state_dir)
    return sorted(directory.glob("*.json")) if directory.exists() else []


def edit_task_row(wiring, task_id, **fields):
    """Hand-edit one row of `tasks.json`, the way an operator with the loop
    stopped can — and the way this suite's `docs/SECURITY.md` note says the
    successor fields can change between a crash and the restart.

    Through the raw file rather than through a `TaskRegistry`, deliberately: the
    registry's own mutators refuse most of what an operator can type into the
    file, so going through them would test a state the loop can already reach
    rather than the one it has to survive.
    """
    path = wiring.config.tasks_file
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["tasks"]:
        if row["id"] == task_id:
            row.update(fields)
            break
    else:  # pragma: no cover - a typo in a test
        raise AssertionError(f"no task {task_id!r} in {path}")
    path.write_text(json.dumps(data), encoding="utf-8")


def install_crash(wiring, boundary):
    """Arm the crash AFTER the parent's implement round has already run.

    Not at construction, deliberately: `_dispatch_task_postcommit` saves the
    registry to record `mark_in_progress`, so a task store armed from the start
    would die during the round that CREATES the execution record and worker repo
    this test needs — testing a crash in a place this task says nothing about.
    """
    tmp_path = wiring.tmp_path
    config = wiring.config
    if boundary == "intent":
        wiring.orch._split_intents = CrashAfterIntentSave(
            split_intents_dir(config.state_dir)
        )
    elif boundary == "registry":
        wiring.orch._task_store = CrashAfterRegistrySave(config.tasks_file)
    elif boundary == "archive":
        wiring.orch._execution_store = CrashAfterArchive(tmp_path / "executions")
    elif boundary == "quarantine":
        wiring.orch._worker_repos = CrashAfterQuarantine(
            tmp_path / "workers", tmp_path / "worker-hooks"
        )
    else:  # pragma: no cover - a typo in a test
        raise AssertionError(f"unknown crash boundary {boundary!r}")
    return wiring


def dispatched_round(tmp_path, tasks=None, **kwargs):
    """A wiring whose task `t1` has really been dispatched: an execution record
    with a candidate sha, and a worker repo on disk."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1")],
        tasks=tasks if tasks is not None else [ready_task("t1")],
        **kwargs,
    )
    wiring.orch.run(max_steps=4)
    execution = wiring.execution_store.load("t1")
    assert execution is not None and execution.candidate_sha
    assert (tmp_path / "workers" / "t1").is_dir()
    return wiring


def assert_split_is_consistent(wiring, parent="t1", successors=("t1a", "t1b")):
    """The ONE accepted-split state, read back from disk on all three stores.

    Read from `tasks.json` rather than from the in-memory registry, deliberately:
    after a simulated crash the interesting question is what the NEXT process
    finds, and an in-memory assertion would pass against a registry that was
    never persisted.
    """
    registry = wiring.task_store.load()
    parent_task = registry.get(parent)
    assert parent_task.status == "retired"
    assert tuple(parent_task.superseded_by) == tuple(successors)
    for tid in successors:
        assert registry.has(tid), f"successor {tid} is missing from the registry"
        # PRESENCE IS NOT AGREEMENT. Every helper in this suite plans its
        # successors with `PATHS`, so a successor that reads back with any other
        # scope is a successor the reconciler adopted rather than created — the
        # fail-open every crash boundary below now also carries.
        assert tuple(registry.get(tid).approved_paths) == PATHS, (
            f"successor {tid} does not carry the write scope the split recorded"
        )
    assert wiring.execution_store.load(parent) is None
    assert not (wiring.tmp_path / "workers" / parent).exists()
    assert intent_files(wiring) == []
    archived = sorted(
        (wiring.tmp_path / "executions" / "archive").glob(f"{parent}-*.json")
    )
    quarantined = sorted((wiring.tmp_path / "quarantine").glob(f"{parent}-*"))
    assert len(archived) == 1, f"expected one archived record, got {archived}"
    assert len(quarantined) == 1, f"expected one quarantined worker, got {quarantined}"
    # The two halves of ONE retirement name each other, which is exactly what a
    # replay under a re-derived label would break.
    assert archived[0].stem == quarantined[0].name
    assert SPLIT_RETIREMENT_REASON in archived[0].stem


# ---------------------------------------------------------------------------
# contract: `split_of` is additive and cannot reach any other decision
# ---------------------------------------------------------------------------


def test_split_of_parses_on_a_plan_and_carries_the_parent_id():
    directive = parse_response(split_block("exec-01", ("exec-02", "exec-03")))
    assert directive.decision is Decision.PLAN
    assert directive.split_of == "exec-01"
    assert tuple(t.id for t in directive.tasks) == ("exec-02", "exec-03")


def test_a_plan_without_split_of_is_byte_for_byte_the_old_directive():
    """The backward-compatibility claim, stated as an assertion rather than as a
    version bump: every `plan` written before this key existed parses to a
    directive whose `split_of` is None, which is an ordinary plan."""
    directive = parse_response(
        block(
            {
                "version": 3,
                "decision": "plan",
                "reason": "more work",
                "tasks": [{"id": "a", "title": "A", "description": "d"}],
            }
        )
    )
    assert directive.split_of is None
    assert directive.decision is Decision.PLAN


@pytest.mark.parametrize(
    "decision,extra",
    [
        ("implement", {"task_id": "t1"}),
        ("revise", {"task_id": "t1", "feedback": "no"}),
        ("recut", {"task_id": "t1"}),
        ("stop", {}),
        ("audit", {}),
    ],
)
def test_split_of_is_refused_on_every_decision_that_is_not_plan(decision, extra):
    """A `split_of` on any other decision names a parent that decision has no
    successors for. Refused rather than dropped: a field that parses and is then
    ignored reads as configured while behaving as if it were not."""
    with pytest.raises(ContractError) as exc:
        parse_response(
            block(
                {
                    "version": 3,
                    "decision": decision,
                    "reason": "r",
                    "split_of": "t1",
                    **extra,
                }
            )
        )
    assert exc.value.code == "unexpected_field"


def test_a_blank_split_of_is_refused_rather_than_read_as_absent():
    """`""` is not `None`. Reading a blank id as "an ordinary plan" would accept
    a directive that meant to split something and silently do half of it."""
    with pytest.raises(ContractError) as exc:
        parse_response(
            block(
                {
                    "version": 3,
                    "decision": "plan",
                    "reason": "r",
                    "split_of": "   ",
                    "tasks": [{"id": "a", "title": "A", "description": "d"}],
                }
            )
        )
    assert exc.value.code == "missing_field:split_of"


def test_the_instructions_advertise_the_key():
    assert "split_of" in CONTRACT_INSTRUCTIONS


def test_the_contract_instruction_ceiling_is_not_slack():
    """The ceiling `test_contract.test_contract_stays_within_its_budget` pins was
    MOVED by this task, from 4,550 to 4,750, and this is the accounting.

    A new top-level KEY is the "genuine new requirement" that file's rule allows
    a move for — the reviewer cannot use a mechanism it is never told about, so
    the three lines describing `split_of` are the cost, and they are the whole
    cost: the reasoning (why a key on `plan` rather than a tenth decision, what
    "atomically" means across three stores, which crash point produces which
    state) lives in the source comments and in `apply_split_intent`'s docstring,
    which cost nothing per turn.

    This test also bounds the move from the other side. A ceiling with room to
    spare is not a ceiling, so the headroom is asserted too — and because the
    assertion prints both numbers on failure, the next executor to change this
    text gets the measured length rather than having to hand-sum it.
    """
    measured = len(CONTRACT_INSTRUCTIONS)
    assert measured <= CONTRACT_CEILING, f"measured {measured}"
    assert CONTRACT_CEILING - measured <= 300, f"measured {measured}"


# ---------------------------------------------------------------------------
# the intent record: shape, round trip, and every malformed reading
# ---------------------------------------------------------------------------


def make_intent(parent="t1", successors=("t1a", "t1b"), reason="split into t1a, t1b"):
    return SplitIntent.create(
        parent,
        [
            SplitSuccessor(
                id=tid, title=f"Title {tid}", description="desc", approved_paths=PATHS
            )
            for tid in successors
        ],
        retirement_reason=SPLIT_RETIREMENT_REASON,
        reason=reason,
    )


def test_the_intent_round_trips_with_its_successors_rehydrated(tmp_path):
    """`SplitIntent(**data)` would store dicts here. Explicit rehydration is what
    keeps every reader seeing the type it declared."""
    store = SplitIntentStore(tmp_path / "split-intents")
    store.save(make_intent())
    loaded = store.load("t1")
    assert loaded is not None
    assert [type(s) for s in loaded.successors] == [SplitSuccessor, SplitSuccessor]
    assert loaded.successor_ids == ("t1a", "t1b")
    assert loaded.successors[0].approved_paths == PATHS
    assert isinstance(loaded.successors[0].approved_paths, tuple)
    assert loaded.label.startswith(SPLIT_RETIREMENT_REASON)


def test_an_absent_intent_is_none_and_an_absent_directory_lists_nothing(tmp_path):
    store = SplitIntentStore(tmp_path / "never-created")
    assert store.load("t1") is None
    assert store.parent_ids() == ()


def test_clearing_an_intent_that_is_not_there_is_not_an_error(tmp_path):
    SplitIntentStore(tmp_path / "split-intents").clear("t1")


@pytest.mark.parametrize(
    "mutate,fragment",
    [
        (lambda d: d.update(label="../../escape"), "unusable retirement label"),
        (lambda d: d.update(label=""), "unusable retirement label"),
        (lambda d: d.update(label="has/slash"), "unusable retirement label"),
        (lambda d: d.update(label=None), "unusable retirement label"),
        (lambda d: d.update(successors=[]), "names no successors"),
        (lambda d: d.update(successors="t1a"), "names no successors"),
        (lambda d: d.update(parent_id="somebody-else"), "names parent"),
        (lambda d: d.update(reason=[1]), "non-string reason"),
        (lambda d: d.update(created_at=7), "non-string created_at"),
    ],
)
def test_a_malformed_intent_raises_rather_than_reading_as_absent(
    tmp_path, mutate, fragment
):
    """The whole mechanism rests on "no file means no split was in flight", so a
    file that cannot be trusted must never be read as no file. Every shape here
    raises `StateCorruptError`, which the reconciler turns into a park."""
    store = SplitIntentStore(tmp_path / "split-intents")
    store.save(make_intent())
    path = store.path_for("t1")
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruptError) as exc:
        store.load("t1")
    assert fragment in str(exc.value)


@pytest.mark.parametrize(
    "successor,fragment",
    [
        ({"id": "", "title": "T", "description": "d"}, "'id' is not a non-empty"),
        ({"id": "x", "title": "  ", "description": "d"}, "'title' is not a non-empty"),
        ({"id": "x", "title": "T", "description": ""}, "'description' is not"),
        (
            {"id": "x", "title": "T", "description": "d", "approved_paths": "docs/A.md"},
            "'approved_paths' is not a list",
        ),
        (
            {"id": "x", "title": "T", "description": "d", "depends_on": [1]},
            "'depends_on' contains something",
        ),
        (
            {"id": "x", "title": "T", "description": "d", "surprise": 1},
            "unknown successor keys",
        ),
        ("not-an-object", "successor that is not an object"),
    ],
)
def test_a_malformed_successor_raises(tmp_path, successor, fragment):
    """A bare string for `approved_paths` is the per-character split
    `tasks._persisted_superseded_by` documents — on the field that would become a
    successor's write authorization. Refused by TYPE, not by truthiness."""
    store = SplitIntentStore(tmp_path / "split-intents")
    store.save(make_intent())
    path = store.path_for("t1")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["successors"] = [successor]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruptError) as exc:
        store.load("t1")
    assert fragment in str(exc.value)


def test_an_intent_naming_the_parent_or_a_duplicate_as_a_successor_raises(tmp_path):
    store = SplitIntentStore(tmp_path / "split-intents")
    store.save(make_intent())
    path = store.path_for("t1")
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["successors"]
    data["successors"] = [rows[0], dict(rows[0])]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruptError) as exc:
        store.load("t1")
    assert "twice" in str(exc.value)

    data["successors"] = [{**rows[0], "id": "t1"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruptError) as exc:
        store.load("t1")
    assert "its own" in str(exc.value)


def test_a_file_that_is_not_json_raises(tmp_path):
    store = SplitIntentStore(tmp_path / "split-intents")
    store.directory.mkdir(parents=True)
    store.path_for("t1").write_text("{not json", encoding="utf-8")
    with pytest.raises(StateCorruptError):
        store.load("t1")
    # ...and it is still LISTED, so the reconciler meets it and parks rather than
    # the listing quietly dropping the file nobody can read.
    assert store.parent_ids() == ("t1",)


# ---------------------------------------------------------------------------
# the shared label: what makes a replayed retirement idempotent
# ---------------------------------------------------------------------------


def test_retire_execution_without_a_label_behaves_exactly_as_before(tmp_path):
    store = TaskExecutionStore(tmp_path / "executions")
    from autoloop.worktask import TaskExecution

    store.save(TaskExecution(task_id="t1", task_branch="b", worktree_path="w",
                             task_base_sha="a" * 40))
    retirement = retire_execution("t1", store, None, reason="released-by-operator")
    assert retirement.label.startswith("released-by-operator-")
    assert retirement.record_path is not None and retirement.record_path.exists()


def test_a_supplied_label_is_used_verbatim_and_replays_idempotently(tmp_path):
    """The property a re-derived label would break: run the retirement twice
    under the SAME label and there is still exactly one archived record, filed
    under that name — no collision, no second copy, no second label."""
    from autoloop.worktask import TaskExecution

    store = TaskExecutionStore(tmp_path / "executions")
    store.save(TaskExecution(task_id="t1", task_branch="b", worktree_path="w",
                             task_base_sha="a" * 40))
    label = retirement_label(SPLIT_RETIREMENT_REASON)
    first = retire_execution("t1", store, None, label=label)
    second = retire_execution("t1", store, None, label=label)
    assert first.label == second.label == label
    assert first.record_path is not None
    assert second.record_path is None  # nothing left to file
    archived = sorted((tmp_path / "executions" / "archive").glob("t1-*.json"))
    assert [p.name for p in archived] == [f"t1-{label}.json"]


# ---------------------------------------------------------------------------
# the happy path, end to end
# ---------------------------------------------------------------------------


def test_a_split_retires_the_parent_adds_the_successors_and_files_the_round_away(
    tmp_path,
):
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(split_directive())

    assert_split_is_consistent(wiring)
    assert wiring.orch.state.phase == Phase.READY.value
    assert "SPLIT APPLIED" in (wiring.orch.state.outbox or "")


def test_the_split_lands_through_a_real_parsed_directive(tmp_path):
    """The dispatch is reached from the wire, not only from a hand-built
    `Directive` — a key that parses but is never routed would pass every test
    above."""
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(parse_response(split_block()))
    assert_split_is_consistent(wiring)


def test_the_parents_retirement_reason_names_the_successors_and_the_reviewer(tmp_path):
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(split_directive(reason="this is three tasks in a coat"))
    parent = wiring.task_store.load().get("t1")
    assert parent.blocked_reason.startswith("split into t1a, t1b")
    assert "three tasks in a coat" in parent.blocked_reason


def test_a_reviewer_paragraph_cannot_grow_the_roadmap_without_bound(tmp_path):
    """The reviewer authors that sentence and nothing enforces the contract's
    "one short sentence", so the durable copy is bounded — visibly."""
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(split_directive(reason="x" * 5000))
    parent = wiring.task_store.load().get("t1")
    assert len(parent.blocked_reason) < MAX_SPLIT_REASON_CHARS + 60
    assert parent.blocked_reason.endswith("…")


def test_the_parents_dependents_are_re_pointed_at_the_successors(tmp_path):
    """A retirement satisfies no dependency, so a split that left a dependent
    naming the parent would strand it forever. The successors are added BEFORE
    the retirement precisely so `_retirement_rewrites` can lift that refusal."""
    wiring = dispatched_round(
        tmp_path,
        tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))],
    )
    wiring.orch._dispatch(split_directive())
    registry = wiring.task_store.load()
    assert tuple(registry.get("t2").depends_on) == ("t1a", "t1b")


def test_a_successor_that_declares_the_parent_as_its_dependency_is_not_stranded(
    tmp_path,
):
    """A plausible reviewer mistake: the first successor is written as depending
    on the task being split. Adding the successors first means it is a live
    dependent by the time the retirement runs, so `_substitute_dependency`
    rewrites the edge — dropping the self-reference rather than making `t1a`
    depend on itself — and nothing is left waiting on a retired id."""
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(
        split_directive(successors=(spec("t1a", depends_on=("t1",)), spec("t1b")))
    )
    assert_split_is_consistent(wiring)
    registry = wiring.task_store.load()
    assert "t1" not in registry.get("t1a").depends_on
    assert "t1a" not in registry.get("t1a").depends_on
    assert tuple(registry.get("t1a").depends_on) == ("t1b",)


def test_the_session_stops_pointing_at_the_round_it_just_filed_away(tmp_path):
    """Same cleanup `recut` does, for the same reason: the execution record is
    gone, so a later approval must not be able to resolve a binding to it."""
    wiring = dispatched_round(tmp_path)
    assert (wiring.orch.state.task_execution or {}).get("task_id") == "t1"
    wiring.orch._dispatch(split_directive())
    assert wiring.orch.state.task_execution is None
    assert wiring.orch.state.current_task is None
    assert wiring.orch.state.carry_postcommit is None


def test_a_split_is_recorded_in_the_transcript_with_the_reviewers_reason(tmp_path):
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(split_directive(reason="two features in one task"))
    entries = records(wiring, "task_split")
    assert len(entries) == 1
    assert entries[0]["task_id"] == "t1"
    assert entries[0]["reason"] == "two features in one task"
    assert entries[0]["successors"] == ["t1a", "t1b"]
    assert entries[0]["complete"] is True
    assert entries[0]["quarantined_worker"]


def test_an_ordinary_plan_is_completely_unaffected(tmp_path):
    """The other half of "additive": a plan with no `split_of` still adds tasks
    and retires nothing, writes no intent, and touches no execution record."""
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(
        Directive(decision=Decision.PLAN, reason="more work", tasks=(spec("t9"),))
    )
    registry = wiring.task_store.load()
    assert registry.has("t9")
    assert registry.get("t1").status == "in_progress"
    assert wiring.execution_store.load("t1") is not None
    assert intent_files(wiring) == []
    assert records(wiring, "task_split") == []


def test_a_split_can_replace_a_task_that_was_never_dispatched(tmp_path):
    """A pending parent has no execution record and no worker repo. The
    retirement is absence-tolerant, so the split lands with the artefact halves
    simply having nothing to move — reported as such rather than failing."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._dispatch(split_directive())
    registry = wiring.task_store.load()
    assert registry.get("t1").status == "retired"
    assert registry.has("t1a") and registry.has("t1b")
    assert intent_files(wiring) == []
    assert "(no record on disk)" in (wiring.orch.state.outbox or "")


# ---------------------------------------------------------------------------
# CRASH AT EVERY WRITE BOUNDARY — the evidence this task exists to produce
# ---------------------------------------------------------------------------


def crash_a_split(tmp_path, boundary, tasks=None, directive=None):
    """Drive the real split dispatch to `boundary` and die there. Returns the
    crashed wiring, whose on-disk state is what the next process will find."""
    wiring = install_crash(dispatched_round(tmp_path, tasks=tasks), boundary)
    with pytest.raises(_Crash):
        wiring.orch._dispatch(directive or split_directive())
    return wiring


def restart(wiring, tmp_path, responses=()):
    """The next process: fresh `LoopState`, plain stores, registry re-read from
    `tasks.json`. Nothing of the crashed process survives except the disk."""
    return build(tmp_path, reuse=wiring, responses=responses)


def test_crash_after_the_intent_and_before_the_registry_save_reconciles(tmp_path):
    """Boundary 1. The intent is the ONLY thing on disk; the registry still
    describes a live parent and no successors. The next start applies the whole
    split from the record alone — which is why the record carries the full
    successor definitions and not merely their ids."""
    crashed = crash_a_split(tmp_path, "intent")
    assert [p.name for p in intent_files(crashed)] == ["t1.json"]
    assert crashed.task_store.load().get("t1").status == "in_progress"
    assert not crashed.task_store.load().has("t1a")

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)


def test_crash_after_the_registry_save_and_before_the_archive_reconciles(tmp_path):
    """Boundary 2 — the reviewer's own words: `tasks.json` says the parent is
    retired while its execution record and worker repo still exist. That record
    holds the repository-wide merge window shut, so this is the window the whole
    mechanism is named for."""
    crashed = crash_a_split(tmp_path, "registry")
    assert crashed.task_store.load().get("t1").status == "retired"
    assert crashed.execution_store.load("t1") is not None
    assert (tmp_path / "workers" / "t1").is_dir()
    assert [p.name for p in intent_files(crashed)] == ["t1.json"]

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)


def test_crash_after_the_archive_and_before_the_quarantine_reconciles(tmp_path):
    """Boundary 3. The record has moved and the worker has not — the split half
    of a retirement. The replay must quarantine under the SAME label the archive
    already used, or the two halves stop naming each other on disk."""
    crashed = crash_a_split(tmp_path, "archive")
    assert crashed.execution_store.load("t1") is None
    assert (tmp_path / "workers" / "t1").is_dir()
    assert [p.name for p in intent_files(crashed)] == ["t1.json"]
    recorded_label = SplitIntentStore(
        split_intents_dir(crashed.config.state_dir)
    ).load("t1").label

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)
    assert (tmp_path / "quarantine" / f"t1-{recorded_label}").is_dir()


def test_crash_after_both_artifacts_and_before_the_intent_is_cleared_reconciles(
    tmp_path,
):
    """Boundary 4 — the only crash point that leaves no visible residue, and the
    reason the intent is cleared LAST. Every store is already at its target; the
    replay must move nothing and must clear the record."""
    crashed = crash_a_split(tmp_path, "quarantine")
    assert crashed.task_store.load().get("t1").status == "retired"
    assert crashed.execution_store.load("t1") is None
    assert not (tmp_path / "workers" / "t1").exists()
    assert [p.name for p in intent_files(crashed)] == ["t1.json"]
    before = sorted(p.name for p in (tmp_path / "quarantine").iterdir())

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)
    assert sorted(p.name for p in (tmp_path / "quarantine").iterdir()) == before


@pytest.mark.parametrize("boundary", ["intent", "registry", "archive", "quarantine"])
def test_every_boundary_reconciles_through_the_ordinary_loop_not_only_when_asked(
    tmp_path, boundary
):
    """The same four crashes, finished by `run()` rather than by a direct call:
    `_step_ready` reconciles before it builds the packet that asks what to do
    next, so a restarted loop repairs itself without anybody knowing to ask."""
    crashed = crash_a_split(tmp_path, boundary)
    fresh = restart(crashed, tmp_path, responses=[stop_block()])
    assert fresh.orch.run() == Phase.STOPPED.value
    assert_split_is_consistent(fresh)


@pytest.mark.parametrize("boundary", ["intent", "registry", "archive", "quarantine"])
def test_a_dependent_is_re_pointed_however_late_the_crash_landed(tmp_path, boundary):
    """The dependency rewrite is part of the same registry write, so it has to
    survive every boundary too — otherwise a crash could leave `t2` waiting
    forever on a task the reconciler then retired."""
    crashed = crash_a_split(
        tmp_path, boundary, tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))]
    )
    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)
    assert tuple(fresh.task_store.load().get("t2").depends_on) == ("t1a", "t1b")


def test_reconciling_twice_changes_nothing_the_second_time(tmp_path):
    """Idempotence to a FIXED POINT, not merely once: the reconciler runs on
    every `_step_ready`, so a second pass over a completed split must be a
    no-op rather than a second retirement."""
    crashed = crash_a_split(tmp_path, "registry")
    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    after_first = sorted(p.name for p in (tmp_path / "quarantine").iterdir())
    assert fresh.orch._reconcile_split_intents() is True
    assert fresh.orch._reconcile_split_intents() is True
    assert sorted(p.name for p in (tmp_path / "quarantine").iterdir()) == after_first
    assert_split_is_consistent(fresh)


def test_a_reviewer_that_resends_the_same_split_is_told_it_already_landed(tmp_path):
    """The replay a restart actually performs: `_step_executing` re-dispatches
    the same directive. It must complete the interrupted split and then REPORT
    it, not deny it — a denial spends the denial budget telling the reviewer its
    directive failed when it succeeded."""
    crashed = crash_a_split(tmp_path, "registry")
    fresh = restart(crashed, tmp_path)
    fresh.orch._dispatch(split_directive())
    assert_split_is_consistent(fresh)
    assert "SPLIT ALREADY APPLIED" in (fresh.orch.state.outbox or "")
    assert denial_codes(fresh) == []
    assert fresh.orch.state.phase == Phase.READY.value


def test_a_registry_that_records_a_split_whose_artifacts_never_moved_is_not_reported_done(
    tmp_path,
):
    """The park message's SECOND option, taken — and the fail-open it opens.

    `_park_split_intent` tells an operator they may DELETE the intent if the
    split should not happen. After a boundary-2 crash that leaves `tasks.json`
    already saying "retired, superseded by t1a, t1b" while the execution record
    and the worker repo are still on disk, and the deleted record is the only
    thing that would have made the loop finish them. A registry-only
    `_split_already_applied` then answers yes and the loop reports a finished
    split to the reviewer — over a live record that holds the merge window shut
    and that nothing will ever revisit, since a retired task is neither
    dispatched nor swept. It must park on all three stores instead of reporting
    on one.
    """
    crashed = crash_a_split(tmp_path, "registry")
    for path in intent_files(crashed):
        path.unlink()
    assert intent_files(crashed) == []

    fresh = restart(crashed, tmp_path)
    fresh.orch._dispatch(split_directive())

    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    assert fresh.orch.state.park_kind == "loop_fatal"
    assert "SPLIT ALREADY APPLIED" not in (fresh.orch.state.outbox or "")
    question = fresh.orch.state.question or ""
    # The residue was SEEN, so it is the actionable park rather than the "could
    # not look" one — the transcript is where those two stay told apart.
    assert park_codes(fresh) == ["split_residue_unreconciled"]
    assert "artefacts never moved" in question
    # BOTH residues named: the silent one (a record nothing announces) and the
    # loud one (a directory the next dispatch trips over).
    assert "merge window" in question
    assert str(tmp_path / "workers" / "t1") in question
    # A park, not a denial — the reviewer cannot fix this by sending anything.
    assert denial_codes(fresh) == []
    # And nothing was moved behind the operator: refusing is this loop's job.
    assert fresh.execution_store.load("t1") is not None
    assert (tmp_path / "workers" / "t1").is_dir()


def test_an_unreadable_record_is_residue_rather_than_a_finished_split(tmp_path):
    """The fail-closed direction of the same check. A record this loop cannot
    parse is exactly the one nobody may declare gone, so it counts as unknown
    residue — a reader that swallowed the error into "no record" would report the
    split as applied on the strength of a file it could not read.

    The worker half is moved out of the way first, so the ONLY thing that can
    park this dispatch is the unreadable record.
    """
    crashed = crash_a_split(tmp_path, "registry")
    for path in intent_files(crashed):
        path.unlink()
    (tmp_path / "workers" / "t1").rename(tmp_path / "workers" / "t1-moved-by-hand")
    (tmp_path / "executions" / "t1.json").write_text("{ truncated", encoding="utf-8")

    fresh = restart(crashed, tmp_path)
    fresh.orch._dispatch(split_directive())

    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    question = fresh.orch.state.question or ""
    assert "execution record cannot be read" in question
    assert "cannot see whether the split ever finished" in question
    assert park_codes(fresh) == ["split_residue_uninspectable"]
    assert "SPLIT ALREADY APPLIED" not in (fresh.orch.state.outbox or "")


@pytest.mark.parametrize(
    "missing, fragment",
    [
        ("execution", "no execution store configured"),
        ("worker", "no worker-repository manager configured"),
        ("both", "no execution store configured"),
    ],
)
def test_a_store_this_loop_cannot_inspect_is_never_read_as_a_finished_split(
    tmp_path, missing, fragment
):
    """The blind spot an earlier round documented and left open, closed.

    `SPLIT ALREADY APPLIED` is a claim about THREE stores. A loop whose execution
    store or worker-repository manager is `None` has read one of them — the
    registry — and cannot read the other, so answering "no residue" would be the
    check going silent exactly where it cannot look, and the reviewer would be
    told the artefacts are gone by a process that never asked. It parks instead,
    under its own code, and moves nothing.

    Crashed at boundary 4, deliberately: every artefact really HAS moved, so the
    only thing this dispatch could park on is the store it cannot inspect. That
    is the exact shape of the collapse being refused — with the missing store
    read as "nothing there", all three cases would report the split as verified
    across three stores while one of them was never opened.

    The old defence — "`split_unavailable` refuses to START a split without both
    stores, so this registry must have been written by a process that had them" —
    is a claim about how the registry got there, not about what is on disk: both
    stores are constructor arguments, so the loop that wrote the retirement and
    the loop that reads it back need not be configured alike. This test IS that
    mismatch.
    """
    crashed = crash_a_split(tmp_path, "quarantine")
    for path in intent_files(crashed):
        path.unlink()

    fresh = restart(crashed, tmp_path)
    # Both artefacts are genuinely gone — verified through the stores this
    # wiring still has, before either is taken away from the orchestrator.
    assert fresh.execution_store.load("t1") is None
    assert not (tmp_path / "workers" / "t1").exists()
    if missing in ("execution", "both"):
        fresh.orch._execution_store = None
    if missing in ("worker", "both"):
        fresh.orch._worker_repos = None

    fresh.orch._dispatch(split_directive())

    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    assert fresh.orch.state.park_kind == "loop_fatal"
    assert park_codes(fresh) == ["split_residue_uninspectable"]
    assert fragment in (fresh.orch.state.question or "")
    # No success report, and no denial either — the reviewer cannot fix a loop's
    # own wiring by sending a different directive.
    assert "SPLIT ALREADY APPLIED" not in (fresh.orch.state.outbox or "")
    assert denial_codes(fresh) == []
    # Nothing was moved to make the park true: the quarantined evidence is still
    # exactly where the crashed process left it.
    assert len(list((tmp_path / "quarantine").glob("t1-*"))) == 1


def test_residue_that_was_seen_outranks_residue_that_could_not_be_looked_at(tmp_path):
    """The mixed answer, and the rule it follows: something WAS found, so the
    park is the actionable one and both halves are named.

    Boundary 2 with no execution store — the worker repo is really there
    (`found`) while the record cannot be inspected at all (`unknown`). Reporting
    only the half this loop could see would send an operator to move one
    directory and call it done.
    """
    crashed = crash_a_split(tmp_path, "registry")
    for path in intent_files(crashed):
        path.unlink()

    fresh = restart(crashed, tmp_path)
    fresh.orch._execution_store = None
    fresh.orch._dispatch(split_directive())

    question = fresh.orch.state.question or ""
    assert park_codes(fresh) == ["split_residue_unreconciled"]
    assert str(tmp_path / "workers" / "t1") in question
    assert "no execution store configured" in question
    assert "SPLIT ALREADY APPLIED" not in (fresh.orch.state.outbox or "")
    assert denial_codes(fresh) == []


def test_a_worker_path_that_cannot_be_resolved_is_residue_rather_than_silence(
    tmp_path,
):
    """A manager that raises instead of answering where the worker repo would be.

    Crashed at boundary 3, so the execution-record half is genuinely CLEAN (the
    record is already archived) — which is what makes this test sharp: the only
    thing standing between this dispatch and a `SPLIT ALREADY APPLIED` is a
    `path_for` that will not answer. `_surviving_worker_path` swallows exactly
    this into `""`, which is right where it is used (decorating a residue already
    known) and would be a fail-open here.
    """
    crashed = crash_a_split(tmp_path, "archive")
    for path in intent_files(crashed):
        path.unlink()

    fresh = restart(crashed, tmp_path)
    assert fresh.execution_store.load("t1") is None
    fresh.orch._worker_repos = UnresolvableWorkerPath(
        tmp_path / "workers", tmp_path / "worker-hooks"
    )

    fresh.orch._dispatch(split_directive())

    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    question = fresh.orch.state.question or ""
    assert "could not be resolved" in question
    assert "unsafe task id" in question
    assert park_codes(fresh) == ["split_residue_uninspectable"]
    assert "SPLIT ALREADY APPLIED" not in (fresh.orch.state.outbox or "")
    assert denial_codes(fresh) == []
    assert (tmp_path / "workers" / "t1").is_dir()


def test_a_worker_path_that_cannot_be_resolved_also_blocks_the_intent_from_clearing(
    tmp_path,
):
    """The OTHER emitter of a completion claim, on the same fault.

    `_split_disagreement` is what lets `apply_split_intent` throw the intent away,
    so it has to fail closed on an unanswerable `path_for` for the same reason
    `_split_residue` does — the intent is the only record that anything was ever
    in flight.
    """
    wiring = dispatched_round(tmp_path, worker_repos_cls=MutePathAfterQuarantine)
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    intent = make_intent()
    store.save(intent)

    result = apply_split_intent(
        intent,
        registry=wiring.registry,
        task_store=wiring.task_store,
        execution_store=wiring.execution_store,
        worker_repos=wiring.orch._worker_repos,
        intent_store=store,
    )
    assert result.complete is False
    assert "could not be resolved" in result.obstacle
    assert store.load("t1") is not None


def test_a_replay_over_three_agreeing_stores_still_reports_the_split(tmp_path):
    """The other side of the same coin, pinned so the fail-closed reading above
    cannot quietly become "park on everything": when all three stores WERE read
    and all three agree, the reviewer is told the split landed."""
    crashed = crash_a_split(tmp_path, "registry")
    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert fresh.orch._split_residue("t1").blocking is False

    fresh.orch._dispatch(split_directive())
    assert "SPLIT ALREADY APPLIED" in (fresh.orch.state.outbox or "")
    assert park_codes(fresh) == []


# ---------------------------------------------------------------------------
# the reconciler fails CLOSED
# ---------------------------------------------------------------------------


def test_an_unreadable_intent_parks_rather_than_being_skipped_or_cleared(tmp_path):
    """The fail-open this refuses: reading a corrupt record as "no split was in
    flight" would let the loop carry on with three stores that may already
    disagree, and nothing would say so."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    directory = split_intents_dir(wiring.config.state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "t1.json").write_text("{ truncated", encoding="utf-8")

    assert wiring.orch._reconcile_split_intents() is False
    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.park_kind == "loop_fatal"
    assert "t1.json" in (wiring.orch.state.question or "")
    # NOT cleared: the file is the only evidence a split was in flight.
    assert (directory / "t1.json").exists()


def test_a_park_stops_the_step_before_a_packet_is_built(tmp_path):
    """`_step_ready` returns on a park instead of asking the reviewer what to do
    next about a roadmap the loop has just said it cannot trust."""
    wiring = build(tmp_path, tasks=[ready_task("t1")], responses=[stop_block()])
    directory = split_intents_dir(wiring.config.state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "t1.json").write_text("{ truncated", encoding="utf-8")

    assert wiring.orch.run() == Phase.NEEDS_USER.value
    assert wiring.client.submitted == []


def test_an_unlistable_intent_directory_is_not_read_as_an_empty_one(
    tmp_path, monkeypatch
):
    """`()` from this listing means "no split was in flight", which is the fact
    the whole mechanism turns on. An `OSError` swallowed into `()` would be the
    check going quiet exactly where it cannot see."""
    store = SplitIntentStore(tmp_path / "split-intents")
    store.directory.mkdir(parents=True)

    def boom(self, pattern):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "glob", boom)
    with pytest.raises(StateCorruptError) as exc:
        store.parent_ids()
    assert "not an empty one" in str(exc.value)


def test_a_loop_that_cannot_list_its_intents_parks_rather_than_carrying_on(tmp_path):
    class Unlistable(SplitIntentStore):
        def parent_ids(self):
            raise StateCorruptError("the directory cannot be listed")

    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._split_intents = Unlistable(split_intents_dir(wiring.config.state_dir))
    assert wiring.orch._reconcile_split_intents() is False
    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "cannot be listed" in (wiring.orch.state.question or "")


def test_an_intent_the_registry_will_not_accept_parks_and_keeps_the_record(tmp_path):
    """An operator who edits `tasks.json` between the crash and the restart can
    make a recorded intent inapplicable. That is a park naming the file, never a
    silent clear — "the split cannot happen" and "the split happened" must not
    produce the same on-disk state."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    store.save(make_intent(parent="ghost"))

    assert wiring.orch._reconcile_split_intents() is False
    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert store.load("ghost") is not None


def test_a_successor_id_taken_by_a_different_task_is_refused_not_adopted(tmp_path):
    """Skipping an already-present successor is what makes the replay possible;
    skipping it WITHOUT checking is how somebody else's task gets silently
    adopted as this split's successor."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.registry.add(
        Task(id="t1a", title="A DIFFERENT TASK", description="mine", approved_paths=PATHS)
    )
    wiring.task_store.save(wiring.registry)
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    store.save(make_intent())

    assert wiring.orch._reconcile_split_intents() is False
    assert "split_successor_occupied" in (wiring.orch.state.question or "")
    assert wiring.task_store.load().get("t1a").title == "A DIFFERENT TASK"
    assert wiring.task_store.load().get("t1").status != "retired"


def test_a_successor_whose_write_scope_was_widened_after_the_crash_is_not_adopted(
    tmp_path,
):
    """The hole this round closes, on the field that makes it a security one.
    Boundary 2 leaves the successors on disk and the artefacts not yet moved; an
    operator who edits `tasks.json` before the restart can hand `t1a` a scope the
    reviewer never approved. Checking title and description only, the replay
    adopts it and the read-back certifies it on PRESENCE — and the intent, the
    only record that anything was ever in flight, is deleted over it."""
    crashed = crash_a_split(tmp_path, "registry")
    edit_task_row(crashed, "t1a", approved_paths=["docs/A.md", "docs/EVERYTHING.md"])

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is False
    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    assert "approved_paths" in (fresh.orch.state.question or "")

    # PRESERVED, not cleared — and it still carries the scope that was approved,
    # which is what lets an operator see what the edit changed.
    store = SplitIntentStore(split_intents_dir(fresh.config.state_dir))
    intent = store.load("t1")
    assert intent is not None
    assert tuple(intent.successors[0].approved_paths) == PATHS
    # Nothing was repaired behind the operator either: refusing is this
    # reconciler's job, rewriting somebody else's task is not.
    assert tuple(fresh.task_store.load().get("t1a").approved_paths) == (
        "docs/A.md",
        "docs/EVERYTHING.md",
    )
    # The trial refuses before the live registry or either artefact is touched,
    # so the parent's round is still there to be looked at.
    assert fresh.execution_store.load("t1") is not None
    assert (tmp_path / "workers" / "t1").is_dir()


def test_a_successor_whose_dependencies_were_edited_after_the_crash_is_not_adopted(
    tmp_path,
):
    """The same hole on the other durable field. `t1b` is planned with no
    dependencies; an edit that makes it wait on `t1a` changes the order the
    roadmap will schedule this split's work in, and a reconciler that reads
    "both ids exist" calls that the split it recorded."""
    crashed = crash_a_split(tmp_path, "registry")
    edit_task_row(crashed, "t1b", depends_on=["t1a"])

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is False
    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    assert "depends_on" in (fresh.orch.state.question or "")
    assert SplitIntentStore(split_intents_dir(fresh.config.state_dir)).load("t1")
    assert tuple(fresh.task_store.load().get("t1b").depends_on) == ("t1a",)
    assert fresh.execution_store.load("t1") is not None


@pytest.mark.parametrize(
    "status", ["retired", "completed", "shipped_elsewhere", "Retired", "retiredx"]
)
def test_a_successor_that_stopped_being_live_after_the_crash_is_not_adopted(
    tmp_path, status
):
    """The hole the other four field comparisons cannot see, on the DEFAULT
    successor — `make_intent`/`split_directive` plan `t1a` and `t1b` with no
    dependencies at all, which is the ordinary shape.

    Empty `depends_on` never reaches the substitution in
    `_expected_successor_depends_on`, and a parent with no dependents makes
    `_retirement_rewrites` return before `retire` consults its `live` filter — so
    with `t1a` hand-edited to a terminal status the replay matched on title,
    description, `approved_paths` and `depends_on`, adopted it, certified it on
    the read-back, and deleted the intent over a parent superseded by work that
    will never happen.

    The last two statuses are why this is an ALLOW-LIST. Neither is in
    `tasks._TERMINAL_STATUSES`, `TaskRegistry.from_dict` validates `status`
    not at all, and a deny-list would read both as live and adopt the row —
    the guard going quiet on exactly the hand-edited input it exists for.
    """
    crashed = crash_a_split(tmp_path, "registry")
    edit_task_row(crashed, "t1a", status=status)

    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is False
    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    # NAMED, not merely refused: an operator who cannot see which status stopped
    # the split has to go and diff `tasks.json` against the intent by hand.
    assert repr(status) in (fresh.orch.state.question or "")

    # PRESERVED — the intent is the only record that a split was ever in flight.
    assert SplitIntentStore(split_intents_dir(fresh.config.state_dir)).load("t1")
    # Refused by the trial, so neither the live registry nor either artefact
    # moved: the parent's round is still there for the operator to look at.
    assert fresh.task_store.load().get("t1a").status == status
    assert fresh.execution_store.load("t1") is not None
    assert (tmp_path / "workers" / "t1").is_dir()


def test_a_registry_that_persists_a_status_it_was_not_given_is_never_certified(
    tmp_path,
):
    """The same liveness check on the OTHER side of the acceptance — the final
    read-back, immediately before the intent is cleared.

    Every write here succeeds and the mutated registry in memory is exactly
    right, so the trial has nothing to refuse; only the file the next process
    will read says `t1a` is retired. A read-back that asked the just-mutated
    registry, or that asked the file for presence alone, would clear the intent
    and leave the parent superseded by a terminal row with nothing recording it.
    """
    wiring = dispatched_round(tmp_path)
    lying = StatusDriftingTaskStore(wiring.config.tasks_file)
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    intent = make_intent()
    store.save(intent)

    result = apply_split_intent(
        intent,
        registry=wiring.registry,
        task_store=lying,
        execution_store=wiring.execution_store,
        worker_repos=wiring.worker_repos,
        intent_store=store,
    )
    assert result.complete is False
    assert "'retired'" in result.obstacle
    assert "t1a" in result.obstacle
    assert store.load("t1") is not None


def test_a_registry_that_persists_a_scope_it_was_not_given_is_never_certified(tmp_path):
    """The read-back proved to be a READ-BACK. Every write here succeeds and the
    mutated registry in memory is exactly right — only the file disagrees, which
    is precisely the thing the next process reads and the in-memory object can
    never reveal.

    It fires AFTER the artefacts have moved, and that is the honest reading of
    the obstacle: this is not "nothing happened", it is "the parent's round is
    filed away and the registry does not describe the split that filed it", so
    the intent stays and a human is sent to the registry."""
    wiring = dispatched_round(tmp_path)
    lying = ScopeDriftingTaskStore(wiring.config.tasks_file)
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    intent = make_intent()
    store.save(intent)

    result = apply_split_intent(
        intent,
        registry=wiring.registry,
        task_store=lying,
        execution_store=wiring.execution_store,
        worker_repos=wiring.worker_repos,
        intent_store=store,
    )
    assert result.complete is False
    assert "approved_paths" in result.obstacle
    assert "t1a" in result.obstacle
    assert store.load("t1") is not None
    assert result.record_path is not None
    assert wiring.execution_store.load("t1") is None


@pytest.mark.parametrize("boundary", ["intent", "registry", "archive", "quarantine"])
def test_a_successor_that_names_the_parent_reconciles_rather_than_parking(
    tmp_path, boundary
):
    """The other half of verifying `depends_on`, and the half a naive check gets
    wrong. The registry legitimately does NOT store what the intent recorded when
    a successor declares the parent as its own dependency: retiring the parent
    re-points every direct dependent, including that successor, so
    `('t1',)` on the record is `('t1b',)` on disk. Comparing against the recorded
    tuple would park a CORRECT split at every boundary from the registry write
    on — a fail-closed check turned into a machine that refuses valid work."""
    crashed = crash_a_split(
        tmp_path,
        boundary,
        directive=split_directive(
            successors=(spec("t1a", depends_on=("t1",)), spec("t1b"))
        ),
    )
    fresh = restart(crashed, tmp_path)
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)
    assert tuple(fresh.task_store.load().get("t1a").depends_on) == ("t1b",)
    assert intent_files(fresh) == []


def test_a_successor_deleted_from_a_half_applied_registry_is_recreated_not_parked_on(
    tmp_path,
):
    """The sharp edge of accounting for the rewrite: the prediction has to be
    made against the registry AS IT WILL BE when the retirement runs, not as it
    is on the way in.

    `t1a` declares the parent, so the retirement pointed it at `t1b`. Delete
    `t1b`'s row and `t1a` still reads `('t1b',)` — but a prediction computed from
    who is present RIGHT NOW would compute the live set as `t1a` alone, drop the
    edge, and park a replay on a mismatch it invented, while the honest repair
    (re-create the missing successor) was available all along."""
    crashed = crash_a_split(
        tmp_path,
        "registry",
        directive=split_directive(
            successors=(spec("t1a", depends_on=("t1",)), spec("t1b"))
        ),
    )
    path = crashed.config.tasks_file
    data = json.loads(path.read_text(encoding="utf-8"))
    data["tasks"] = [row for row in data["tasks"] if row["id"] != "t1b"]
    path.write_text(json.dumps(data), encoding="utf-8")

    fresh = restart(crashed, tmp_path)
    assert fresh.task_store.load().has("t1b") is False
    assert fresh.orch._reconcile_split_intents() is True
    assert_split_is_consistent(fresh)
    # Re-created from the intent's own definition, scope included — which is why
    # the record carries the full successor and not merely its id.
    registry = fresh.task_store.load()
    assert tuple(registry.get("t1b").approved_paths) == PATHS
    assert tuple(registry.get("t1a").depends_on) == ("t1b",)


def test_a_quarantine_that_did_not_happen_is_not_reported_as_a_finished_split(tmp_path):
    """The read-back verification, exercised on the one thing that could make it
    pointless: a worker manager that RETURNS a destination without moving
    anything. "No exception escaped" is not the property being claimed."""
    wiring = dispatched_round(tmp_path, worker_repos_cls=NoopQuarantine)
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    intent = make_intent()
    store.save(intent)

    result = apply_split_intent(
        intent,
        registry=wiring.registry,
        task_store=wiring.task_store,
        execution_store=wiring.execution_store,
        worker_repos=wiring.orch._worker_repos,
        intent_store=store,
    )
    assert result.complete is False
    assert "worker repository is still at" in result.obstacle
    assert store.load("t1") is not None


def test_a_loop_with_no_artifact_stores_cannot_finish_an_intent_it_finds(tmp_path):
    """Missing collaborators are an OBSTACLE, never a skip: this process cannot
    finish an operation an earlier one started, and returning quietly would leave
    the intent to be reported as reconciled by nobody."""
    wiring = build(tmp_path, tasks=[ready_task("t1")], no_artifact_stores=True)
    store = SplitIntentStore(split_intents_dir(wiring.config.state_dir))
    store.save(make_intent())

    assert wiring.orch._reconcile_split_intents() is False
    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert store.load("t1") is not None


# ---------------------------------------------------------------------------
# refusals: every one of them happens before the intent exists
# ---------------------------------------------------------------------------


def assert_nothing_moved(wiring, code):
    assert denial_codes(wiring)[-1] == code
    assert intent_files(wiring) == []
    assert records(wiring, "task_split") == []


def test_a_split_that_names_no_successors_is_denied_rather_than_deleting_the_task(
    tmp_path,
):
    """The fail-open the empty case would be: retiring the parent naming nothing
    and creating nothing is a deletion, not a split. `_parse_task_specs` refuses
    an empty `tasks` list and `SplitIntentStore.load` refuses such a record on
    the replay path, but neither covers the FIRST application from a directive
    built in code — this refusal is what does."""
    wiring = dispatched_round(tmp_path)
    wiring.orch._dispatch(
        Directive(decision=Decision.PLAN, reason="split it", tasks=(), split_of="t1")
    )
    assert_nothing_moved(wiring, "split_no_successors")
    assert wiring.task_store.load().get("t1").status == "in_progress"
    assert wiring.execution_store.load("t1") is not None


def test_a_split_of_an_unknown_task_is_denied(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._dispatch(split_directive(parent="nope"))
    assert_nothing_moved(wiring, "task_unknown")
    assert not wiring.task_store.load().has("t1a")


@pytest.mark.parametrize("parent", ["audit", "audit-0007"])
def test_a_split_of_an_audit_unit_is_denied_by_name(tmp_path, parent):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._dispatch(split_directive(parent=parent))
    assert_nothing_moved(wiring, "split_audit_unit")


def test_a_successor_that_is_the_parent_is_denied(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._dispatch(split_directive(successors=("t1", "t1b")))
    assert_nothing_moved(wiring, "split_successor_is_parent")
    assert wiring.task_store.load().get("t1").status == "pending"


def test_a_successor_id_that_already_exists_is_denied(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1"), ready_task("t1a")])
    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "duplicate_task")
    assert wiring.task_store.load().get("t1a").title == "Title t1a"


@pytest.mark.parametrize("status", ["completed", "retired", "blocked"])
def test_only_queued_or_in_flight_work_can_be_split(tmp_path, status):
    """`retire` itself would accept a quarantined task; a split does not. A
    `blocked` row is an open question with a blocker record behind it, and
    retiring it without answering is the split brain
    `cli._reconcile_retired_blockers` exists for."""
    wiring = build(tmp_path, tasks=[ready_task("t1", status=status)])
    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "split_parent_not_splittable")


def test_a_loop_with_no_worker_manager_refuses_the_split_outright(tmp_path):
    """Retiring the roadmap row alone would leave the contaminated worker exactly
    where the next dispatch looks — an accepted split that is inconsistent from
    the first instant."""
    wiring = build(tmp_path, tasks=[ready_task("t1")], no_artifact_stores=True)
    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "split_unavailable")
    assert wiring.task_store.load().get("t1").status == "pending"


def test_a_published_candidate_is_never_split_away(tmp_path):
    wiring = dispatched_round(tmp_path)
    execution = wiring.execution_store.load("t1")
    execution.published_sha = "b" * 40
    execution.intended_remote = "origin"
    execution.intended_remote_ref = "refs/heads/autoloop/t1"
    wiring.execution_store.save(execution)

    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "split_candidate_published")
    assert wiring.execution_store.load("t1") is not None
    assert (tmp_path / "workers" / "t1").is_dir()


def test_an_unreadable_execution_record_is_refused_rather_than_filed_away(tmp_path):
    """Unreadable is not absent. A record this loop cannot parse may name a
    published candidate, and archiving it unread destroys the only evidence."""
    wiring = dispatched_round(tmp_path)
    wiring.execution_store.path_for("t1").write_text("{ nope", encoding="utf-8")
    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "split_record_unreadable")


def test_a_split_while_a_reply_is_in_flight_is_refused(tmp_path):
    wiring = dispatched_round(tmp_path)
    from autoloop.state import PendingRequest

    wiring.orch.state.pending_request = PendingRequest(
        request_id="alr-x-0001", payload="a packet awaiting a verdict", submitted=True
    )
    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "split_verdict_outstanding")


def test_a_split_that_would_build_a_dependency_cycle_is_denied_before_anything_moves(
    tmp_path,
):
    """The trial mutation, and why it is the LAST refusal: a graph refusal
    reaches the reviewer as something it can answer, instead of becoming a
    durable intent that can never apply and parks the loop every round."""
    wiring = build(
        tmp_path, tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))]
    )
    wiring.orch._dispatch(
        split_directive(successors=(spec("t1a", depends_on=("t2",)), spec("t1b")))
    )
    assert_nothing_moved(wiring, "dependency_cycle")
    registry = wiring.task_store.load()
    assert registry.get("t1").status == "pending"
    assert not registry.has("t1a")
    assert tuple(registry.get("t2").depends_on) == ("t1",)


def test_a_split_whose_dependent_is_mid_round_is_denied(tmp_path):
    """Rewriting a running dispatch's dependencies is what strands it, so
    `_retirement_rewrites` refuses — and the trial turns that into a denial with
    nothing written."""
    wiring = build(
        tmp_path,
        tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",), status="in_progress")],
    )
    wiring.orch._dispatch(split_directive())
    assert_nothing_moved(wiring, "task_in_progress")
    assert wiring.task_store.load().get("t1").status == "pending"


def test_a_denied_split_leaves_the_execution_record_and_worker_untouched(tmp_path):
    """One assertion over the whole refusal set: a denial is a denial across all
    three stores, not only in the registry."""
    wiring = dispatched_round(tmp_path)
    before = wiring.execution_store.load("t1")
    wiring.orch._dispatch(split_directive(successors=("t1", "t1b")))
    after = wiring.execution_store.load("t1")
    assert after is not None and after.candidate_sha == before.candidate_sha
    assert (tmp_path / "workers" / "t1").is_dir()
    assert not (tmp_path / "quarantine").exists()
    assert intent_files(wiring) == []


# ---------------------------------------------------------------------------
# the parent id is a FILENAME: an intent nobody can list is worse than none
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parent_id", ["../escapee", "sub/dir", "", "/absolute", "a/../../b"]
)
def test_an_intent_that_would_land_outside_its_directory_is_refused(tmp_path, parent_id):
    """`parent_ids` finds an interrupted split by listing `*.json` in ONE
    directory, so an intent written anywhere else is one the reconciler can never
    look at — and it is the only record that anything was in flight.

    Refused rather than written, and BEFORE the caller is told it was saved: the
    caller's next act is to mutate the registry, so a store that accepted this
    would hand back a split whose durable half is unreachable."""
    store = SplitIntentStore(tmp_path / "split-intents")
    with pytest.raises(StateCorruptError) as exc:
        store.save(make_intent(parent=parent_id))
    assert "never be finished" in str(exc.value)
    assert store.parent_ids() == ()
    assert not (tmp_path / "escapee.json").exists()
    assert not (tmp_path / "absolute.json").exists()


def test_a_parent_id_the_registry_holds_but_a_filename_cannot_parks_before_anything_moves(
    tmp_path,
):
    """The reachable version of the case above, with no crash in it at all.

    `TaskRegistry.add_many` validates every id it accepts, but `from_dict`
    deliberately does not re-validate a stored row — so a hand-edited
    `tasks.json` can hold `../escapee`, every refusal in `_dispatch_split` passes
    (the row is really there and really splittable), and the intent write is the
    first thing that can object. It must object BEFORE the registry moves: this
    is the one path to a retired parent whose intent no reconciliation would
    ever find."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    edit_task_row(wiring, "t1", id="../escapee")
    fresh = restart(wiring, tmp_path)
    assert fresh.registry.has("../escapee")

    fresh.orch._dispatch(split_directive(parent="../escapee"))

    assert park_codes(fresh)[-1] == "split_intent_unwritable"
    assert fresh.orch.state.phase == Phase.NEEDS_USER.value
    assert records(fresh, "task_split") == []
    # Nothing written, here or anywhere the listing cannot reach.
    assert intent_files(fresh) == []
    assert not (fresh.config.state_dir / "escapee.json").exists()
    # And nothing moved: the registry write comes after the intent, so the
    # parent is still live work and its successors do not exist.
    registry = fresh.task_store.load()
    assert registry.get("../escapee").status == "pending"
    assert not registry.has("t1a")
