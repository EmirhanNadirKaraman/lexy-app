"""Urgent preemption: an operator takes the loop NOW, without touching the gate.

The claim under test is narrow and whole: an operator marks a task urgent
through the inbox, and the loop dispatches THAT task next — pausing at the next
SAFE phase boundary, returning the displaced task cleanly to pending, and
recording what was displaced — with no change to how any candidate is reviewed,
approved or published.

Measured 2026-08-21/22, and each measurement has a test here:

* Priority alone cannot preempt. `next_ready` chooses among READY tasks, so a
  task already in flight is untouchable by it, and codex-01 raised to P0 only
  TIED with the P0 task already mid-round and lost the id tiebreak
  ("brw-13" < "codex-01"). Silently.
* The manual sequence is pause -> wait for the boundary -> release -> drain ->
  restart, and the wait alone cost 10 and 15 minutes on two occasions.
* `retire_execution` was used where `release` was needed: the execution record
  and the worker repo moved, the STATUS did not, and two tasks sat
  `in_progress` and invisible to `next_ready` for about an hour.

There is deliberately no pause flag anywhere in this path, which is why the
overlapping-request tests below assert the flag's ABSENCE: the documented
deadlock is one preemption calling `resume` on its way out and stranding
another that is waiting on it, and a lock that is never taken cannot strand
anyone.
"""

from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

import pytest

from autoloop import cli, execution_records, orchestrator as orchestrator_module
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.contract import RETIRED_DECISIONS, Decision, Directive
from autoloop.errors import GitCommandError, TaskGraphError, TemplateError
from autoloop.inbox import (
    KIND_URGENT,
    InboxError,
    TaskInbox,
    apply_requests,
    inbox_dir_for,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import PREEMPTION_STOP_KIND, Orchestrator
from autoloop.policy import PolicyEngine
from autoloop.prompts import TEMPLATES
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/urgent-preemption"


# --- fixtures / builders ------------------------------------------------------


def _task(tid, priority=100, **kw):
    kw.setdefault("approved_paths", ("docs/A.md",))
    return Task(id=tid, title=f"Title {tid}", description="d", priority=priority, **kw)


@pytest.fixture
def config(tmp_path):
    # `workers_root` OUTSIDE the state dir, as production requires: the inbox,
    # the PAUSE flag and the heartbeat all derive from its parent, and a test
    # that put them inside the checkout would be testing a layout the loop
    # refuses to run in.
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "outside" / "workers",
    )


class StubGit:
    """The orchestrator's git gateway is never reached by the preemption path.

    Preemption moves a task status, an execution record and a directory; it
    reads no sha, builds no packet and runs no command. A stub with nothing on
    it is therefore evidence in itself — if a future change starts asking git
    something here, these tests fail with an `AttributeError` naming the call.
    """


class StubExecutor:
    def execute(self, directive, task):  # pragma: no cover - never dispatched here
        raise AssertionError("no executor runs during a preemption")


def no_conversation():  # pragma: no cover - reached only by a regression
    raise AssertionError("no conversation is opened during a preemption")


def build_loop(config, tasks=(), state=None, executions=None, worker_repos=None):
    """A minimal Orchestrator wired to real task/execution/worker stores.

    Deliberately NOT the full postcommit rig: nothing here dispatches, submits
    or reviews, and a real git repo would only make the tests slower without
    exercising a line the preemption path touches.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(config.state_file)
    if state is None:
        state = LoopState.new(URL)
        state.outbox = "a packet about the task in flight"
    store.save(state)
    registry = TaskRegistry(list(tasks))
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=StubGit(),
        executor=StubExecutor(),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_conversation,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        execution_store=executions
        or TaskExecutionStore(config.executions_dir),
        worker_repos=worker_repos
        or WorkerRepoManager(config.workers_root, config.worker_hooks_dir),
    )
    return orch, store, task_store


def in_flight(orch, task_id, candidate="c" * 40, review_round=1):
    """Put `task_id` mid-round: in-progress, a worker repo with real work in
    it, an execution record holding a candidate, and the session pointing at
    it. The exact shape a preemption has to be able to displace."""
    orch._registry.mark_in_progress(task_id)
    orch._task_store.save(orch._registry)
    worker_path = orch._worker_repos.path_for(task_id)
    worker_path.mkdir(parents=True, exist_ok=True)
    (worker_path / "half-done.txt").write_text("twenty minutes of work", encoding="utf-8")
    orch._execution_store.save(
        TaskExecution(
            task_id=task_id,
            task_branch=f"autoloop/{task_id}",
            worktree_path=str(worker_path),
            task_base_sha="b" * 40,
            candidate_sha=candidate,
            review_round=review_round,
        )
    )
    orch.state.current_task = {
        "task_id": task_id,
        "title": f"Title {task_id}",
        "decision": "implement",
        "started_at": "2026-08-22T00:00:00+00:00",
    }
    orch._store.save(orch.state)
    return worker_path


# --- 1. the urgent task is dispatched next, id tiebreak and all ---------------


def test_priority_alone_cannot_preempt_but_the_pin_can():
    """The 2026-08-21 reproduction, exactly. codex-01 was raised to P0 while
    brw-13 held the loop; brw-13 was ALREADY P0, so P0 only tied and the id
    tiebreak decided it. Nothing said so."""
    registry = TaskRegistry([_task("brw-13", priority=0), _task("codex-01", priority=0)])

    assert registry.next_ready().id == "brw-13", "the id tiebreak, as measured"
    registry.set_priority("codex-01", 0)
    assert registry.next_ready().id == "brw-13", "a P0 request only TIES with a P0"

    registry.request_urgent("codex-01", "the codex transport is down")

    assert registry.next_ready().id == "codex-01"


def test_the_pin_outranks_priority_in_both_directions():
    """It is not "priority zero by another name": a pinned task at the DEFAULT
    priority still outranks a P1, so an operator does not have to reason about
    numbers to preempt, and cannot be beaten by a number they did not know
    about."""
    registry = TaskRegistry([_task("aaa", priority=1), _task("zzz", priority=100)])
    assert registry.next_ready().id == "aaa"

    registry.request_urgent("zzz", "production is down")

    assert registry.next_ready().id == "zzz"


def test_the_urgent_task_is_selected_next_even_though_the_running_task_wins_by_id(config):
    """End to end at the loop's own boundary: brw-13 mid-round, codex-01
    requested urgent through the INBOX, and the very next selection is
    codex-01 — not because it was added later or scored better, but because the
    round in flight was displaced."""
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13", priority=0), _task("codex-01", priority=0)]
    )
    in_flight(orch, "brw-13")
    inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
    orch._task_inbox = inbox
    inbox.submit({"kind": KIND_URGENT, "id": "codex-01", "reason": "transport down"})

    outcome = orch.run()

    assert outcome == Phase.STOPPED.value
    assert orch.state.stop_kind == PREEMPTION_STOP_KIND
    # Reloaded from disk, because continuous mode reloads `tasks.json` at the
    # top of its next iteration: an unsaved release or pin simply is not there.
    reloaded = task_store.load()
    assert reloaded.next_ready().id == "codex-01"
    assert reloaded.state_of("brw-13") is TaskState.READY


def test_the_dispatch_gate_refuses_another_task_while_the_pin_is_live(config):
    """"Offered next" is not "dispatched next". `next_ready()` puts the pinned
    task at the head of the queue and the CONTEXT block briefs only that task,
    but policy authorizes any READY task by id — so a reviewer naming a
    different one is refused through the ordinary budget-capped re-prompt
    rather than quietly working around the operator."""
    orch, _, _ = build_loop(config, tasks=[_task("brw-13"), _task("codex-01")])
    orch._registry.request_urgent("codex-01", "transport down")
    orch.state.phase = Phase.EXECUTING.value

    orch._dispatch_executor(
        Directive(decision=Decision.IMPLEMENT, reason="next", task_id="brw-13")
    )

    assert orch._registry.state_of("brw-13") is TaskState.READY, "never started"
    assert orch.state.policy_denials == 1
    assert "urgent" in (orch.state.outbox or "").lower()
    assert "codex-01" in (orch.state.outbox or "")


def test_the_pin_is_consumed_by_the_dispatch_it_asked_for():
    """One dispatch, not permanent precedence — and the single slot has to
    reopen, or the next operator can never preempt again."""
    registry = TaskRegistry([_task("codex-01"), _task("other")])
    registry.request_urgent("codex-01", "transport down")

    registry.mark_in_progress("codex-01")

    assert registry.get("codex-01").urgent_at == ""
    assert registry.live_urgent_target() is None
    registry.request_urgent("other", "the next emergency")
    assert registry.live_urgent_target().id == "other"


# --- 2. the displaced task returns to pending, all three halves moved ---------


def test_the_displaced_task_returns_to_pending_with_all_three_halves_moved(config):
    """THREE things have to move, not one — the failure of 2026-08-21 was
    `retire_execution` where `release` was needed: the record and the worker
    moved, the STATUS did not, and the task sat `in_progress` and invisible to
    `next_ready` for about an hour."""
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")]
    )
    worker_path = in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch._task_store.save(orch._registry)

    assert orch._preempt_for_urgent(Phase.READY) is True

    # 1. the STATUS
    assert task_store.load().state_of("brw-13") is TaskState.READY
    # 2. the WORKER REPO — moved, never deleted
    assert not worker_path.exists(), "a stale worker makes the next dispatch refuse"
    quarantined = list(config.workers_root.parent.glob("quarantine/brw-13-*"))
    assert quarantined, "the displaced round's work must survive somewhere"
    assert (quarantined[0] / "half-done.txt").read_text(encoding="utf-8") == (
        "twenty minutes of work"
    )
    # 3. the EXECUTION RECORD — or the merge window stays shut on a candidate
    #    that no longer exists as in-flight.
    assert orch._execution_store.load("brw-13") is None
    assert list(config.executions_dir.glob("archive/brw-13-*.json"))


def test_the_displaced_task_is_selectable_again_afterwards(config):
    """A preemption is a reordering, not a retirement. Once the urgent task has
    been dispatched — which clears the pin — the displaced task is simply the
    next ready task again."""
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")]
    )
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch._preempt_for_urgent(Phase.READY)

    registry = task_store.load()
    assert registry.next_ready().id == "codex-01"
    registry.mark_in_progress("codex-01")

    assert registry.next_ready().id == "brw-13"


def test_the_displaced_round_is_recorded_not_silently_discarded(config):
    """A preemption that silently discarded twenty minutes of work would be
    worse than a slow queue. The record names the task, both phases, the
    urgent target and where the work went."""
    orch, _, _ = build_loop(config, tasks=[_task("brw-13"), _task("codex-01")])
    in_flight(orch, "brw-13", candidate="a" * 40, review_round=2)
    orch._registry.request_urgent("codex-01", "transport down")

    orch._preempt_for_urgent(Phase.READY)

    record = orch.state.preemption
    assert record["displaced_task_id"] == "brw-13"
    assert record["displaced_returned_to_pending"] is True
    assert record["urgent_task_id"] == "codex-01"
    assert record["urgent_reason"] == "transport down"
    assert record["preempted_at_phase"] == Phase.READY.value
    assert record["displaced_candidate_sha"] == "a" * 40
    assert record["displaced_review_round"] == 2
    assert "quarantine/brw-13-" in record["quarantined_worker_path"]
    assert record["archived_execution_record"].endswith(".json")

    entries = [
        json.loads(line)
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    preempted = [e for e in entries if e.get("type") == "task_preempted"]
    assert len(preempted) == 1, "the durable record outlives the session"
    assert preempted[0]["data"]["displaced_task_id"] == "brw-13"


def test_the_preemption_reuses_the_release_path_rather_than_repeating_it():
    """Structural, and it is the point of the whole `release_task_to_pending`
    extraction: the way releasing a task goes wrong is doing one third of it,
    so a second implementation is the defect, not a style question."""
    source = inspect.getsource(Orchestrator._preempt_for_urgent)
    assert "release_task_to_pending(" in source
    assert "registry.release(" not in source, "the three-part move is not repeated here"
    assert "release_task_to_pending(" in inspect.getsource(cli._cmd_release)


class FlakyWorkerRepos:
    """A `WorkerRepoManager` whose `quarantine` fails its first `failures`
    calls with the error a COLLIDING LABEL actually raises.

    That collision is not hypothetical: `retire_execution` derives its label
    from the reason plus a whole-second timestamp, so two retirements of one
    task inside the same second collide by construction. It also raises
    `GitCommandError`, which is not an `OSError` — the shape that used to
    escape the preemption's own `except` clause and reach the loop's git
    handler instead of being recorded.
    """

    def __init__(self, inner, failures):
        self._inner = inner
        self._failures_left = failures
        self.labels = []

    def path_for(self, task_id):
        return self._inner.path_for(task_id)

    def hooks_dir_for(self, task_id):  # pragma: no cover - not reached here
        return self._inner.hooks_dir_for(task_id)

    def quarantine(self, task_id, label):
        self.labels.append(label)
        if self._failures_left > 0:
            self._failures_left -= 1
            raise GitCommandError(
                f"quarantine destination {task_id}-{label} already exists — "
                "'label' must be unique per call"
            )
        return self._inner.quarantine(task_id, label)


def test_a_colliding_label_is_retried_under_a_distinct_one(config):
    """The likelier of the two named retirement failures, and it is a label
    problem rather than a filesystem one — so retrying under `<reason>-retry`
    is what turns it back into a clean retirement, where retrying under the
    same label would collide identically forever."""
    workers = FlakyWorkerRepos(
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir), failures=1
    )
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")], worker_repos=workers
    )
    worker_path = in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")

    assert orch._preempt_for_urgent(Phase.READY) is True

    assert [label.startswith("displaced-by-urgent-retry") for label in workers.labels] == [
        False,
        True,
    ]
    assert task_store.load().state_of("brw-13") is TaskState.READY
    assert not worker_path.exists()
    assert list(config.workers_root.parent.glob("quarantine/brw-13-*"))
    record = orch.state.preemption
    assert record["displaced_returned_to_pending"] is True
    assert record["displaced_artifacts_retired"] is True
    assert record["stale_worker_path"] == ""
    # The record was filed by the attempt that then failed on the worker, so
    # the retry had nothing left to file and reported `None`. Reporting that
    # as "there was no record" would lose the half a human has to find.
    assert record["archived_execution_record"].endswith(".json")
    assert Path(record["archived_execution_record"]).exists()


def test_a_retirement_that_cannot_succeed_is_recorded_as_the_partial_it_is(config):
    """The failure-consistency rule. The status move is durable BEFORE the
    artefacts move, so a retirement that fails leaves a task that IS pending —
    reporting that as "NOT returned to pending" sent an operator to fix a
    status that was already correct while the residue that actually blocks the
    next dispatch went unnamed. Two facts, recorded separately."""
    workers = FlakyWorkerRepos(
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir), failures=2
    )
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")], worker_repos=workers
    )
    worker_path = in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")

    assert orch._preempt_for_urgent(Phase.READY) is True

    # The STATUS moved, durably, and the task is selectable again.
    assert task_store.load().state_of("brw-13") is TaskState.READY
    record = orch.state.preemption
    assert record["displaced_returned_to_pending"] is True
    # ...and the ARTEFACTS did not, said as its own fact, naming what survived.
    assert record["displaced_artifacts_retired"] is False
    assert record["stale_worker_path"] == str(worker_path)
    assert worker_path.exists(), "nothing was deleted on the way past"
    assert "already exists" in record["obstacle"]
    assert "returned to pending" in orch.state.stop_reason
    assert str(worker_path) in orch.state.stop_reason

    entries = [
        json.loads(line)
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failed = [e for e in entries if e.get("type") == "preemption_retirement_failed"]
    assert len(failed) == 1
    assert failed[0]["data"]["stale_worker_path"] == str(worker_path)


def test_a_resumable_residue_is_left_paired_so_the_next_dispatch_continues_it(
    config, monkeypatch, capsys
):
    """`retire_execution` files the RECORD first and the worker second, so a
    worker half that fails splits the pair. When the worker is one the dispatch
    would RESUME, that split is pure loss — with no record the dispatch takes
    the first-dispatch branch and `WorkerRepoManager.create` refuses to write
    into the directory that is still there, discarding a round that could have
    been continued. The record goes back beside it instead, which is exactly
    what a killed round leaves."""
    # Patched on `execution_records`, which is where `_worker_can_resume`
    # resolves the probe since shrink-01 (2026-08-26) lifted the release path
    # out of `orchestrator`. Patching `orchestrator` instead reaches only the
    # dispatch-side calls, which this path never runs — the assertions below
    # would then fail against the real probe, which refuses this worker.
    monkeypatch.setattr(execution_records, "worker_repo_is_reusable", lambda p, b: True)
    workers = FlakyWorkerRepos(
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir), failures=2
    )
    orch, _, _ = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")], worker_repos=workers
    )
    worker_path = in_flight(orch, "brw-13", candidate="a" * 40)
    orch._registry.request_urgent("codex-01", "transport down")

    orch._preempt_for_urgent(Phase.READY)

    survivor = orch._execution_store.load("brw-13")
    assert survivor is not None, "the record was put back beside its worker"
    assert survivor.candidate_sha == "a" * 40, "and it is the ORIGINAL record"
    assert survivor.worktree_path == str(worker_path)
    record = orch.state.preemption
    assert (record["stale_execution_record"], record["residue_resumable"]) == (True, True)
    # Said out loud, because the pairing has a real cost the operator is the
    # one who has to weigh: a live record shuts the repository-wide merge
    # window, and `_merge_window_blockers` exempts it only once that
    # candidate is PUBLISHED — a re-`release` is not available, the task is no
    # longer in progress.
    assert "merge window stays shut until that round publishes" in orch.state.stop_reason
    capsys.readouterr()
    cli._report_preemption(orch.state)
    assert "ACTION       none required" in capsys.readouterr().out


def test_a_worker_the_next_dispatch_cannot_resume_is_left_split_and_named(config):
    """The gate on that repair, and the reason it is not optional. This
    worker is a plain directory — `worker_repo_is_reusable` (the real one, run
    here) refuses it, so the next dispatch would refuse to create over it
    whether or not a record sits beside it. Restoring the record anyway would
    shut the merge window for the whole repository and buy nothing, so the
    residue is left split and the one `mv` is named instead."""
    workers = FlakyWorkerRepos(
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir), failures=2
    )
    orch, _, _ = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")], worker_repos=workers
    )
    worker_path = in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")

    orch._preempt_for_urgent(Phase.READY)

    record = orch.state.preemption
    assert record["residue_resumable"] is False
    assert record["stale_execution_record"] is False, "the merge window is not shut"
    assert orch._execution_store.load("brw-13") is None
    assert record["stale_worker_path"] == str(worker_path)
    assert f"Move {worker_path} aside" in orch.state.stop_reason


def test_the_urgent_task_still_takes_the_loop_after_a_partial_release(config):
    """The preemption's own claim must survive the residue: the operator asked
    for their task, and a worker repo that could not be moved is the DISPLACED
    task's problem, not theirs."""
    workers = FlakyWorkerRepos(
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir), failures=2
    )
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")], worker_repos=workers
    )
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")

    orch._preempt_for_urgent(Phase.READY)

    assert orch.state.stop_kind == PREEMPTION_STOP_KIND
    assert task_store.load().next_ready().id == "codex-01"


def test_the_operator_is_told_what_the_partial_release_left_behind(config, capsys):
    """A residue nobody can find is a residue nobody reasons about.
    `_report_preemption` prints the partial release as the two facts it is —
    the status moved, the artefacts did not — and names what survived."""
    workers = FlakyWorkerRepos(
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir), failures=2
    )
    orch, _, _ = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")], worker_repos=workers
    )
    worker_path = in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch._preempt_for_urgent(Phase.READY)
    capsys.readouterr()

    cli._report_preemption(orch.state)

    out = capsys.readouterr().out
    assert "in_progress -> pending, selectable again" in out
    assert "could NOT be retired" in out
    assert f"worker repo still at {worker_path}" in out
    assert f"ACTION       move {worker_path} aside" in out


def _audit_round(orch, unit_id="audit-0007"):
    """A dispatched AUDIT unit: minted per run, absent from the registry, with
    a worker repo and an execution record — the shape `_resolve_audit_task`
    produces."""
    worker_path = orch._worker_repos.path_for(unit_id)
    worker_path.mkdir(parents=True, exist_ok=True)
    (worker_path / "report.md").write_text("# findings", encoding="utf-8")
    orch._execution_store.save(
        TaskExecution(
            task_id=unit_id,
            task_branch=f"autoloop/{unit_id}",
            worktree_path=str(worker_path),
            task_base_sha="b" * 40,
        )
    )
    orch.state.current_task = {"task_id": unit_id, "title": "repository audit"}
    return worker_path


def test_an_audit_round_is_waited_out_rather_than_displaced(config):
    """An audit holds no task in the queue and its product is a report, so
    quarantining it mid-write spends the round twice — once to produce the
    report and once to redo it — to save the tail of a round the loop has
    already paid for. `next_ready()` still returns the urgent task for the
    round after it, and the round AFTER that is bounded by the fresh-audit
    refusal, not by another lap."""
    orch, _, _ = build_loop(config, tasks=[_task("codex-01")])
    worker_path = _audit_round(orch)
    orch._registry.request_urgent("codex-01", "transport down")

    assert orch._preempt_for_urgent(Phase.READY) is False

    assert orch.state.phase == Phase.READY.value
    assert worker_path.exists(), "the audit's own work is untouched"
    assert orch._execution_store.load("audit-0007") is not None
    assert not list(config.workers_root.parent.glob("quarantine/*"))
    assert orch._registry.next_ready().id == "codex-01", "still selected next"


def test_the_session_a_preemption_starts_does_not_preempt_itself_in_a_lap(config):
    """The churn this guard exists to stop, driven one lap further than the
    test above. An audit round that reached the executor is waited out rather
    than quarantined mid-write — displacing it would spend the round twice to
    save the tail of one the loop has already paid for.

    What bounds the wait at ONE round rather than one per lap is the pair of
    changes below it: `_start_new_session` opens a pinned session on the urgent
    task instead of on the audit kickoff, and `_refused_ahead_of_urgent`
    refuses a FRESH audit while the pin is live. Before those, the fresh
    session invited an `audit` and the gate let it through."""
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")]
    )
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    assert orch._preempt_for_urgent(Phase.READY) is True

    # The next session dispatches an audit while the pin is still live.
    orch.state.phase = Phase.READY.value
    orch.state.preemption = None
    _audit_round(orch, "audit-0008")

    assert orch._preempt_for_urgent(Phase.READY) is False
    assert orch.state.preemption is None, "no second preemption was recorded"
    assert not list(config.workers_root.parent.glob("quarantine/audit-0008-*"))
    assert task_store.load().next_ready().id == "codex-01"


def test_an_idle_loop_is_not_preempted_in_a_circle(config):
    """With nothing in flight there is nothing to displace, and ending the
    round would achieve nothing: the next session would reach the same
    boundary with the same pin and the loop would spend every iteration ending
    sessions that had done nothing. The pin alone steers the next selection."""
    orch, _, _ = build_loop(config, tasks=[_task("codex-01"), _task("other")])
    orch._registry.request_urgent("codex-01", "transport down")

    assert orch._preempt_for_urgent(Phase.READY) is False
    assert orch.state.phase == Phase.READY.value
    assert orch._registry.next_ready().id == "codex-01", "still selected next"


# --- 2b. the urgent task is the next DISPATCH, not merely the next offer ------
#
# "Selected next" was not the claim. A preemption ends the round and continuous
# mode opens a NEW session, and every new session used to open on the AUDIT
# KICKOFF — so the reviewer was invited to answer `audit`, `_dispatch_executor`
# let audits through, and the urgent task waited out a full executor round
# (measured 1282s) that the whole mechanism exists to skip. Two changes close
# that, and both are tested here: the pinned session opens on the urgent task,
# and a fresh audit is refused while the pin is live.


def _next_session(config, orch, store, task_store):
    """Continuous mode's next iteration, in the two lines it actually is:
    select and kick off from the reloaded registry, then build the round on the
    freshly saved session. `_cmd_run` does this through `_build_orchestrator`;
    rebinding the two loaded objects on the existing orchestrator exercises the
    same inputs without a browser, a git repo or a lock."""
    registry = task_store.load()
    started = cli._select_and_kickoff(config, store, registry)
    orch.state = store.load()
    orch._registry = registry
    return started


def test_a_preempted_session_opens_on_the_urgent_task_not_on_the_audit(config):
    """The kickoff is what the reviewer answers, so a kickoff that offers the
    audit is a request for one. The session a preemption starts names the task
    the operator paid a displaced round for."""
    orch, store, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01")]
    )
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    assert orch._preempt_for_urgent(Phase.READY) is True

    assert _next_session(config, orch, store, task_store) is True

    outbox = store.load().outbox or ""
    assert "codex-01" in outbox
    assert "transport down" in outbox
    assert outbox != TEMPLATES["audit_kickoff"].render()
    assert "first autonomous repository audit" not in outbox


def test_an_unpinned_session_still_opens_on_the_audit_kickoff(config):
    """The ordinary path is untouched: with no pin live, a new session offers
    the audit exactly as it always has."""
    # Nothing in flight; only the selection matters here.
    _, store, task_store = build_loop(config, tasks=[_task("brw-13")])

    assert cli._select_and_kickoff(config, store, task_store.load()) is True

    assert store.load().outbox == TEMPLATES["audit_kickoff"].render()


def test_the_urgent_kickoff_asks_for_a_directive_policy_will_authorize(config):
    """A kickoff that solicits a directive policy then denies would trade audit
    churn for DENIAL churn, on exactly the tasks that have never been planned —
    and the denial budget ends a run. So the ask is driven by whether the task
    already holds an approved decomposition."""
    registry = TaskRegistry([_task("codex-01")])
    registry.request_urgent("codex-01", "transport down")
    # `implement_enabled=True` because the DEFAULT is the Phase-3 gate, which
    # denies every implement of a repository task before the decomposition
    # check is reached — under it this test would pass on the wrong verdict.
    policy = PolicyEngine(PolicyConfig(implement_enabled=True))
    bare = Directive(decision=Decision.IMPLEMENT, reason="urgent", task_id="codex-01")

    # Unplanned: policy refuses a bare `implement`, so the kickoff must ask for
    # the plan rather than for the directive alone.
    verdict = policy.authorize_directive(bare, "autoloop/x", registry)
    assert (verdict.allowed, verdict.code) == (False, "decomposition_missing")
    unplanned = cli.urgent_kickoff_payload("codex-01", "T", "transport down", False)
    assert "NO approved decomposition" in unplanned
    assert "must" in unplanned and "decomposition" in unplanned

    # Planned: the same bare directive is authorized, and asking for the plan
    # again would tax a round that is already going well.
    registry.set_decomposition("codex-01", "Step 1: fix the transport")
    assert policy.authorize_directive(bare, "autoloop/x", registry).allowed is True
    planned = cli.urgent_kickoff_payload("codex-01", "T", "transport down", True)
    assert "already has an approved decomposition" in planned


def test_the_kickoff_reads_the_plan_state_from_the_registry(config):
    """The half above is only true if `_start_new_session` passes the real
    answer: a hardcoded one would be wrong for every task on one side of it."""
    _, store, task_store = build_loop(config, tasks=[_task("codex-01")])
    registry = task_store.load()
    registry.request_urgent("codex-01", "transport down")
    task_store.save(registry)

    cli._select_and_kickoff(config, store, task_store.load())
    assert "NO approved decomposition" in (store.load().outbox or "")

    registry = task_store.load()
    registry.set_decomposition("codex-01", "Step 1: fix the transport")
    task_store.save(registry)

    cli._select_and_kickoff(config, store, task_store.load())
    assert "already has an approved decomposition" in (store.load().outbox or "")


def test_the_urgent_kickoff_is_as_strict_as_every_other_template():
    """The kickoff template lives in `cli.py`, not in `prompts.TEMPLATES`, so
    the two guarantees that library provides have to be asserted here rather
    than inherited. First: STRICT rendering. `test_prompts.py` renders every
    template in the dict with its own fields and refuses missing/unknown ones,
    which is what makes a template/caller drift fail a test instead of shipping
    a mangled prompt to the reviewer."""
    template = cli.URGENT_KICKOFF
    fields = {f: f.upper() for f in template.fields}

    rendered = template.render(**fields)

    for field in template.fields:
        assert field.upper() in rendered
    with pytest.raises(TemplateError, match="missing"):
        template.render(task_id="codex-01")
    with pytest.raises(TemplateError, match="unknown"):
        template.render(**fields, extra="nope")


@pytest.mark.parametrize("decision", sorted(d.value for d in RETIRED_DECISIONS))
def test_the_urgent_kickoff_offers_no_retired_decision(decision):
    """Second: `test_prompts.py::test_no_template_offers_a_retired_decision`
    enumerates `TEMPLATES`, and this body is not in that dict. It is exactly the
    shape that check exists for — it names decisions outright ("Reply
    `implement`...", "no fresh `audit`"), and a retired one surviving in it
    would invite the reviewer to send the directive policy refuses
    unconditionally."""
    assert decision not in cli.URGENT_KICKOFF.body


def test_a_fresh_audit_is_refused_while_the_pin_is_live(config):
    """An audit takes no task out of the queue, but it takes the LOOP for a
    full executor round — which is the only thing an urgent request is asking
    for. Refused through the ordinary budget-capped re-prompt, naming the task
    to send instead."""
    dispatched = []
    orch, _, _ = build_loop(config, tasks=[_task("codex-01")])
    orch._dispatch_task_postcommit = lambda d, t, s: dispatched.append(t.id)
    orch._registry.request_urgent("codex-01", "transport down")

    orch._dispatch_executor(Directive(decision=Decision.AUDIT, reason="routine audit"))

    assert dispatched == [], "no audit round was started"
    assert orch.state.policy_denials == 1
    outbox = orch.state.outbox or ""
    assert "codex-01" in outbox
    assert "not cancelled" in outbox, "the audit is deferred, not refused forever"


def test_a_revise_of_an_audit_arc_already_in_flight_is_not_refused(config):
    """The one exemption, and it is not cosmetic: round 1's commit lives in a
    worker repo only round 2 can reach, so refusing this abandons real work to
    save a round the loop has already paid for. It cannot smuggle a round in
    after a preemption either — a preemption clears `current_task`, and a
    revise-of-audit with no audit on record parks instead of minting a unit."""
    dispatched = []
    orch, _, _ = build_loop(config, tasks=[_task("codex-01")])
    orch._dispatch_task_postcommit = lambda d, t, s: dispatched.append(t.id)
    orch._registry.request_urgent("codex-01", "transport down")
    orch.state.current_task = {"task_id": "audit-0007", "title": "repository audit"}

    orch._dispatch_executor(
        Directive(
            decision=Decision.REVISE,
            reason="tighten the findings",
            task_id="audit",
            feedback="two findings are unsupported",
        )
    )

    assert dispatched == ["audit-0007"], "the arc in flight continues"
    assert orch.state.policy_denials == 0


def test_nothing_is_dispatched_between_the_preemption_and_the_urgent_task(config):
    """THE end-to-end claim, in the order it actually happens: a round in
    flight is displaced, continuous mode opens the next session, and the very
    next thing that reaches the executor is the urgent task — with an `audit`
    and an unrelated `implement` both attempted in between and both refused."""
    dispatched = []
    orch, store, task_store = build_loop(
        config, tasks=[_task("brw-13", priority=0), _task("codex-01", priority=0)]
    )
    orch._dispatch_task_postcommit = lambda d, t, s: dispatched.append(t.id)
    in_flight(orch, "brw-13")
    inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
    orch._task_inbox = inbox
    inbox.submit({"kind": KIND_URGENT, "id": "codex-01", "reason": "transport down"})

    assert orch.run() == Phase.STOPPED.value
    assert orch.state.stop_kind == PREEMPTION_STOP_KIND
    assert dispatched == [], "the preemption itself dispatches nothing"

    assert _next_session(config, orch, store, task_store) is True
    assert "codex-01" in (orch.state.outbox or "")

    # Everything the reviewer could plausibly send FIRST, refused in turn.
    orch._dispatch_executor(Directive(decision=Decision.AUDIT, reason="audit first"))
    orch._dispatch_executor(
        Directive(decision=Decision.IMPLEMENT, reason="brw-13 was nearly done", task_id="brw-13")
    )
    assert dispatched == [], "no audit and no other task ran ahead of the pin"
    assert orch.state.policy_denials == 2

    orch._dispatch_executor(
        Directive(decision=Decision.IMPLEMENT, reason="the urgent one", task_id="codex-01")
    )

    assert dispatched == ["codex-01"], "the urgent task is the FIRST dispatch after the preemption"
    assert orch._registry.state_of("codex-01") is TaskState.IN_PROGRESS
    assert orch._registry.live_urgent_target() is None, "the pin is spent by its dispatch"


# --- 3. a request that arrives mid-review waits for a safe boundary -----------


UNSAFE_PHASES = [
    Phase.DELIVERING,
    Phase.SUBMITTING,
    Phase.SUBMISSION_UNCONFIRMED,
    Phase.SUBMISSION_REJECTED,
    Phase.AWAITING,
    Phase.EXECUTING,
]


@pytest.mark.parametrize("phase", UNSAFE_PHASES, ids=lambda p: p.value)
def test_a_request_arriving_mid_round_waits_rather_than_interrupting(config, phase):
    """Never interrupt while a review packet is outstanding: pausing in
    `submitting` or `awaiting` strands an approved push, and `executing` has a
    write-capable agent inside a worker repo."""
    orch, _, task_store = build_loop(config, tasks=[_task("brw-13"), _task("codex-01")])
    worker_path = in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch.state.pending_request = PendingRequest(request_id="alr-1", payload="packet")

    assert orch._preempt_for_urgent(phase) is False

    assert orch._registry.state_of("brw-13") is TaskState.IN_PROGRESS
    assert worker_path.exists(), "nothing was quarantined mid-review"
    assert orch._execution_store.load("brw-13") is not None
    assert orch.state.phase != Phase.STOPPED.value
    assert task_store.load().state_of("brw-13") is TaskState.IN_PROGRESS


def test_ready_with_a_packet_still_outstanding_is_not_a_boundary(config):
    """`pending_request` outlives its own phase: a request answered and not yet
    consumed is still a packet the loop owes something to, so `ready` alone is
    not the boundary."""
    orch, _, _ = build_loop(config, tasks=[_task("brw-13"), _task("codex-01")])
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch.state.pending_request = PendingRequest(request_id="alr-1", payload="packet")

    assert orch._at_round_boundary(Phase.READY) is False
    assert orch._preempt_for_urgent(Phase.READY) is False


def test_the_wait_ends_at_the_boundary_rather_than_being_forgotten(config):
    """"Waits" has to mean waits. The same pin that was declined mid-`awaiting`
    is acted on the moment the round comes back to `ready`."""
    orch, _, _ = build_loop(config, tasks=[_task("brw-13"), _task("codex-01")])
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch.state.pending_request = PendingRequest(request_id="alr-1", payload="packet")
    assert orch._preempt_for_urgent(Phase.AWAITING) is False

    orch.state.pending_request = None

    assert orch._preempt_for_urgent(Phase.READY) is True
    # And it reports what it waited through, not only where it acted.
    assert orch.state.preemption["first_observed_phase"] == Phase.AWAITING.value
    assert orch.state.preemption["preempted_at_phase"] == Phase.READY.value


def test_the_safe_boundary_is_the_one_the_loop_already_treats_as_safe():
    """One predicate, two users. A second copy would be a second answer to
    "may this session be interrupted", and the phases where the answer matters
    are the ones where being wrong strands a packet."""
    source = inspect.getsource(Orchestrator._self_upgrade_due)
    assert "_at_round_boundary(" in source
    assert "_at_round_boundary(" in inspect.getsource(Orchestrator._preempt_for_urgent)


# --- 4. a target that cannot be dispatched is refused, loudly -----------------


def test_a_dependency_unmet_target_is_refused_with_a_reason():
    registry = TaskRegistry([_task("dep"), _task("blocked-by-dep", depends_on=("dep",))])

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("blocked-by-dep", "cannot wait")

    assert excinfo.value.code == "task_blocked"
    assert "dep" in str(excinfo.value)
    assert registry.live_urgent_target() is None


def test_a_quarantined_target_is_refused_with_a_reason():
    registry = TaskRegistry([_task("held")])
    registry.block("held", "validation failed twice")

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("held", "cannot wait")

    assert excinfo.value.code == "task_blocked_by_operator"
    assert "validation failed twice" in str(excinfo.value)


def test_a_retired_target_is_refused_with_its_successor():
    registry = TaskRegistry([_task("brw-06")])
    registry.retire("brw-06", superseded_by=("brw-07",))

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("brw-06", "cannot wait")

    assert excinfo.value.code == "task_retired"
    assert "brw-07" in str(excinfo.value)


def test_a_completed_target_is_refused():
    registry = TaskRegistry([_task("done")])
    registry.mark_completed("done")

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("done", "cannot wait")

    assert excinfo.value.code == "task_completed"


def test_a_target_already_in_flight_is_refused_by_that_name():
    """Not a generic "not ready": the request has already been granted in the
    only sense that matters, and an operator told "not ready" about the task
    the loop is running would resubmit."""
    registry = TaskRegistry([_task("running")])
    registry.mark_in_progress("running")

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("running", "cannot wait")

    assert excinfo.value.code == "task_in_progress"
    assert "already the round in flight" in str(excinfo.value)


def test_an_unscoped_target_is_refused_before_a_round_is_displaced():
    """`_dispatch_task_postcommit` refuses an unscoped implement/revise, so
    accepting this would displace a round in order to dispatch a task that
    parks on arrival."""
    registry = TaskRegistry([Task(id="unscoped", title="U", description="d")])

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("unscoped", "cannot wait")

    assert excinfo.value.code == "no_approved_paths"


def test_an_unknown_target_is_refused():
    with pytest.raises(TaskGraphError) as excinfo:
        TaskRegistry().request_urgent("nope", "cannot wait")

    assert excinfo.value.code == "task_unknown"


def test_a_reasonless_request_is_refused():
    registry = TaskRegistry([_task("codex-01")])

    for reason in ("", "   ", None):
        with pytest.raises(TaskGraphError) as excinfo:
            registry.request_urgent("codex-01", reason)
        assert excinfo.value.code == "empty_task_field"
    assert registry.live_urgent_target() is None


def test_a_refused_request_leaves_the_incumbent_pin_untouched():
    """Atomicity, and the ORDER of the checks. The incumbent is reported before
    the target's own state, so an operator whose second request lost is told
    which preemption is already landing rather than being sent to look at their
    own task — and the pin they lost to is not cleared on the way past."""
    registry = TaskRegistry(
        [_task("codex-01"), _task("dep"), _task("late", depends_on=("dep",))]
    )
    registry.request_urgent("codex-01", "transport down")

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("late", "also urgent")

    assert excinfo.value.code == "urgent_already_pending"
    assert registry.live_urgent_target().id == "codex-01"
    assert registry.next_ready().id == "codex-01"


def test_the_drain_reports_a_refused_request_rather_than_ignoring_it():
    """A silent no-op is not a valid answer. The refusal reaches the operator
    as the drain's own `refused` line, in the registry's words."""
    registry = TaskRegistry([_task("dep"), _task("late", depends_on=("dep",))])

    added, applied, refused = apply_requests(
        registry, [{"kind": KIND_URGENT, "id": "late", "reason": "cannot wait"}]
    )

    assert (added, applied) == ([], [])
    assert len(refused) == 1 and "late" in refused[0] and "dep" in refused[0]


# --- 5. one at a time; overlapping requests do not deadlock -------------------


def test_a_second_request_is_refused_while_the_first_is_still_landing():
    """ONE AT A TIME, and refused rather than queued: two preemptions in flight
    with one round to displace between them is exactly the shape the manual
    pause/resume sequence failed at."""
    registry = TaskRegistry([_task("codex-01"), _task("auto-02")])
    registry.request_urgent("codex-01", "transport down")

    with pytest.raises(TaskGraphError) as excinfo:
        registry.request_urgent("auto-02", "also down")

    assert excinfo.value.code == "urgent_already_pending"
    assert "codex-01" in str(excinfo.value)
    assert registry.live_urgent_target().id == "codex-01"
    assert registry.get("auto-02").urgent_at == ""


def test_the_second_request_is_accepted_once_the_first_has_been_dispatched():
    """The slot reopens rather than latching — the refusal above has to be a
    "not yet", or one urgent task would be the last one ever."""
    registry = TaskRegistry([_task("codex-01"), _task("auto-02")])
    registry.request_urgent("codex-01", "transport down")
    registry.mark_in_progress("codex-01")

    registry.request_urgent("auto-02", "also down")

    assert registry.live_urgent_target().id == "auto-02"


def test_a_repeat_of_the_same_request_is_idempotent():
    """An operator who is not sure the first one landed must not rewrite the
    record of when the preemption was asked for."""
    registry = TaskRegistry([_task("codex-01")])
    first = registry.request_urgent("codex-01", "transport down")
    stamp, reason = first.urgent_at, first.urgent_reason

    again = registry.request_urgent("codex-01", "a differently worded plea")

    assert (again.urgent_at, again.urgent_reason) == (stamp, reason)


def test_a_pin_whose_task_left_the_queue_does_not_wedge_the_slot():
    """`pending` is mutable, so a pinned task can be pushed out of READY after
    the fact. If that held the slot shut, every later urgent request would be
    refused forever on behalf of a preemption that can never happen."""
    registry = TaskRegistry([_task("dep"), _task("codex-01"), _task("auto-02")])
    registry.request_urgent("codex-01", "transport down")
    registry.set_depends_on("codex-01", ["dep"])  # now BLOCKED, never dispatchable

    assert registry.live_urgent_target() is None
    registry.request_urgent("auto-02", "the next emergency")

    assert registry.live_urgent_target().id == "auto-02"
    assert registry.get("codex-01").urgent_at == "", "the stale marker is cleared"


def test_two_overlapping_requests_do_not_deadlock_and_take_no_pause(config):
    """The documented failure of the manual sequence is that the first
    preemption to time out calls `resume` on its way out and strands the second
    on a lock that never clears. This path takes NO pause: the loop observes
    the request at its own boundary, so there is no flag to leave behind and
    nothing for a second request to wait on."""
    orch, _, task_store = build_loop(
        config, tasks=[_task("brw-13"), _task("codex-01"), _task("auto-02")]
    )
    in_flight(orch, "brw-13")
    inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
    orch._task_inbox = inbox
    inbox.submit({"kind": KIND_URGENT, "id": "codex-01", "reason": "transport down"})
    inbox.submit({"kind": KIND_URGENT, "id": "auto-02", "reason": "also down"})

    assert orch.run() == Phase.STOPPED.value

    # The first request won, the second was refused, and the loop RETURNED.
    reloaded = task_store.load()
    assert reloaded.next_ready().id == "codex-01"
    assert reloaded.get("auto-02").urgent_at == ""
    assert not config.pause_file.exists(), "no pause is ever taken"
    assert not config.legacy_pause_file.exists()
    assert not cli.pause_requested(config)


def test_no_part_of_the_preemption_path_touches_the_pause_flag():
    """Structural companion to the test above: the reason two preemptions
    cannot deadlock on a pause is that no code here writes, reads or clears
    one."""
    for func in (
        Orchestrator._preempt_for_urgent,
        Orchestrator._displaced_work_exists,
        Orchestrator._at_round_boundary,
        orchestrator_module.release_task_to_pending,
        cli._cmd_urgent,
    ):
        source = inspect.getsource(func)
        for forbidden in ("pause_file", "pause_requested", "clear_pause"):
            assert forbidden not in source, f"{func.__name__} touches {forbidden}"


# --- 6. review, approval and publication are untouched -----------------------


def test_the_preemption_neither_reviews_nor_approves_nor_publishes():
    """The entire win is preemption. Skipping review would save about 2% of a
    round (packet build 0.98s, submit 12-15s, verdict ~0s, executor round
    1282s) and would cost the binding that makes the system safe:
    `report_sha256` covers the packet bytes INCLUDING the candidate sha, which
    is what stops an approval computed over candidate A from publishing
    candidate B."""
    source = inspect.getsource(Orchestrator._preempt_for_urgent)
    for forbidden in (
        "verify_review",
        "report_sha256",
        "push_exact",
        "_dispatch_task_push",
        "mark_completed",
        "reviewed_commit",
    ):
        assert forbidden not in source, f"preemption must not reach {forbidden}"


def test_a_preempted_stop_is_a_clean_boundary_not_a_fault():
    """Continuous mode must carry on: the round ended deliberately, the
    displaced task is back in the queue, and the next selection is the point.
    `_is_fault_stop` reads `"fault"` positively for exactly this reason."""
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = PREEMPTION_STOP_KIND

    assert cli._is_fault_stop(state) is False
    assert cli._is_preemption_stop(state) is True


def test_the_reviewer_is_told_about_the_pin():
    """A rule the reviewer cannot see is not a rule: the dispatch gate refuses
    every other task, so the roadmap line has to name the one that is
    expected. Same coupling as the priority-1 breakdown beside it."""
    registry = TaskRegistry([_task("brw-13"), _task("codex-01")])
    assert "URGENT" not in registry.summary()

    registry.request_urgent("codex-01", "transport down")

    summary = registry.summary()
    assert "URGENT: codex-01" in summary
    assert "transport down" in summary
    assert "next ready: codex-01" in summary


# --- persistence + the inbox's shape gate ------------------------------------


def test_the_pin_survives_a_persistence_round_trip(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    registry = TaskRegistry([_task("codex-01"), _task("brw-13")])
    registry.request_urgent("codex-01", "transport down")
    store.save(registry)

    reloaded = store.load()

    assert reloaded.live_urgent_target().id == "codex-01"
    assert reloaded.get("codex-01").urgent_reason == "transport down"
    assert reloaded.next_ready().id == "codex-01"


def test_a_tasks_file_written_before_the_pin_existed_still_loads(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {"id": "old", "title": "O", "description": "d", "status": "pending"},
                    {
                        "id": "hand-edited",
                        "title": "H",
                        "description": "d",
                        "status": "pending",
                        "urgent_at": None,
                        "urgent_reason": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = TaskStore(path).load()

    assert registry.get("old").urgent_at == ""
    # A hand-edited `null` must become `""` rather than `None`: every reader
    # tests this field for truthiness and then prints it.
    assert registry.get("hand-edited").urgent_at == ""
    assert registry.get("hand-edited").urgent_reason == ""
    assert registry.live_urgent_target() is None


def test_an_urgent_request_carries_only_kind_id_and_reason(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")

    with pytest.raises(InboxError, match="needs 'reason'"):
        inbox.submit({"kind": KIND_URGENT, "id": "codex-01"})
    with pytest.raises(InboxError, match="carries only"):
        inbox.submit(
            {"kind": KIND_URGENT, "id": "codex-01", "reason": "x", "priority": 1}
        )
    with pytest.raises(InboxError, match="as a string"):
        inbox.submit({"kind": KIND_URGENT, "id": "codex-01", "reason": 1})
    with pytest.raises(InboxError, match="needs the task 'id'"):
        inbox.submit({"kind": KIND_URGENT, "reason": "x"})
    assert inbox.pending() == [], "nothing malformed reaches the queue"


def test_a_hand_written_urgent_request_gets_the_same_shape_gate():
    """Hand-writing the JSON file is a documented operator route and never
    passes through `submit`, so `apply_requests` re-checks off the same
    function."""
    registry = TaskRegistry([_task("codex-01")])

    added, applied, refused = apply_requests(
        registry,
        [{"kind": KIND_URGENT, "id": "codex-01", "reason": "x", "approved_paths": []}],
    )

    assert (added, applied) == ([], [])
    assert "carries only" in refused[0]
    assert registry.live_urgent_target() is None


# --- the CLI ------------------------------------------------------------------


@pytest.fixture
def wired(config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def test_urgent_command_queues_a_request_without_taking_a_lock(wired, capsys):
    TaskStore(wired.tasks_file).save(TaskRegistry([_task("codex-01")]))

    code = cli._cmd_urgent(
        argparse.Namespace(config=None, task_id="codex-01", reason="transport down")
    )

    assert code == 0
    queued = list((wired.workers_root.parent / "inbox").glob("*.json"))
    assert len(queued) == 1
    spec = json.loads(queued[0].read_text(encoding="utf-8"))
    assert spec == {"kind": KIND_URGENT, "id": "codex-01", "reason": "transport down"}
    out = capsys.readouterr().out
    assert "SAFE boundary" in out
    # The registry itself is NOT written here — the loop stays its only writer.
    assert TaskStore(wired.tasks_file).load().live_urgent_target() is None


def test_release_command_still_raises_but_the_status_move_is_already_durable(
    wired, monkeypatch
):
    """The CLI half of the shared release path, and what sharing it did NOT
    change: a retirement that fails still propagates out of `_cmd_release`, of
    its original type, for `main` to report and exit 1 on. That contract has its
    own pin (`test_recovery_commands.py::
    test_the_record_is_retired_before_the_worker`) and an operator who ran the
    command by hand is reading its output, so the raise IS the loud ending — the
    opposite trade from `_preempt_for_urgent`, which nobody is watching and which
    therefore takes the failure back inside the `Release`.

    What is pinned HERE is the half that only became visible once one function
    served both callers: the STATUS move is made durable BEFORE the artefacts
    move, so on the way out of that raise the task really is pending again — and
    the retry under a distinct label runs on this path too, since a colliding
    label is as likely for an operator as for a preemption. No execution record
    is put back beside the survivor: `_repair_orphaned_record` is deliberately
    not reached on the raising path, because a live record shuts the
    repository-wide merge window and paying that silently behind a traceback is
    a cost nobody chose."""
    registry = TaskRegistry([_task("brw-13")])
    registry.mark_in_progress("brw-13")
    TaskStore(wired.tasks_file).save(registry)
    real = WorkerRepoManager(wired.workers_root, wired.worker_hooks_dir)
    worker_path = real.path_for("brw-13")
    worker_path.mkdir(parents=True)
    (worker_path / "half-done.txt").write_text("twenty minutes", encoding="utf-8")
    executions = TaskExecutionStore(wired.executions_dir)
    executions.save(
        TaskExecution(
            task_id="brw-13",
            task_branch="autoloop/brw-13",
            worktree_path=str(worker_path),
            task_base_sha="b" * 40,
            candidate_sha="c" * 40,
        )
    )
    workers = FlakyWorkerRepos(real, failures=2)
    monkeypatch.setattr(cli, "WorkerRepoManager", lambda *a, **k: workers)

    with pytest.raises(GitCommandError, match="already exists"):
        cli._cmd_release(argparse.Namespace(config=None, task_id="brw-13"))

    # The STATUS moved, durably, before the artefacts were touched at all.
    assert TaskStore(wired.tasks_file).load().state_of("brw-13") is TaskState.READY
    # The retry ran here too, under a label that could not collide identically.
    assert [label.startswith("released-by-operator-retry") for label in workers.labels] == [
        False,
        True,
    ]
    # The record went first and stayed retired; the worker is the loud residue
    # the next dispatch refuses to write over, and nothing was deleted.
    assert executions.load("brw-13") is None
    assert list((wired.executions_dir / "archive").glob("brw-13-*.json"))
    assert worker_path.exists()
    assert (worker_path / "half-done.txt").read_text(encoding="utf-8") == "twenty minutes"


def test_urgent_command_refuses_before_queueing_anything(wired, capsys):
    """The dry run is the registry's own `request_urgent` against a registry
    nothing saves, so the operator reads one authority's words — and a request
    that cannot be honoured never reaches the queue at all."""
    registry = TaskRegistry([_task("dep"), _task("late", depends_on=("dep",))])
    TaskStore(wired.tasks_file).save(registry)

    code = cli._cmd_urgent(
        argparse.Namespace(config=None, task_id="late", reason="cannot wait")
    )

    assert code == 1
    assert "dep" in capsys.readouterr().out
    assert not list((wired.workers_root.parent / "inbox").glob("*.json"))
    assert TaskStore(wired.tasks_file).load().get("late").urgent_at == ""


def test_the_operator_is_told_what_the_preemption_cost(config, capsys):
    """`Loop ended: stopped` on its own reads as the reviewer's own `stop`,
    which is the one thing a preemption is not."""
    orch, _, _ = build_loop(config, tasks=[_task("brw-13"), _task("codex-01")])
    in_flight(orch, "brw-13")
    orch._registry.request_urgent("codex-01", "transport down")
    orch._preempt_for_urgent(Phase.READY)
    capsys.readouterr()

    cli._report_preemption(orch.state)

    out = capsys.readouterr().out
    assert "URGENT task codex-01" in out
    assert "brw-13: in_progress -> pending" in out
    assert "no review packet was interrupted" in out
    assert "kept, not deleted" in out


def test_report_preemption_is_silent_for_any_other_stop(capsys):
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = "contract"

    cli._report_preemption(state)

    assert capsys.readouterr().out == ""
