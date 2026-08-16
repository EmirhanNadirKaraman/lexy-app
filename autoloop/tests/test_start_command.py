"""`start`: repair what is provably safe, report what is not.

The split is the whole design. A lock whose owner is provably dead and a CDP
port that does not answer are decidable from evidence. Archiving an execution
record, quarantining a worker repo, or "resolving" a blocker are judgements
that destroy work when guessed wrong — a repair command that guesses at those
is worse than none, because it looks like it worked.
"""

import argparse

import pytest

from autoloop import cli
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.lock import LoopLock
from autoloop.state import LoopState, Phase, StateStore

URL = "https://chatgpt.com/c/start-command"


@pytest.fixture
def config(tmp_path):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path.parent / f"{tmp_path.name}-workers",
    )


@pytest.fixture
def wired(config, monkeypatch):
    """`start` with a healthy browser and no loop actually launched."""
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    monkeypatch.setattr(cli, "_default_probe_cdp", lambda url: '{"Browser":"Chrome"}')
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def _args(**kw):
    return argparse.Namespace(config=None, check_only=kw.get("check_only", True))


def _a_state(**kw):
    return LoopState(session_id="start-cmd", conversation_url=URL, **kw)


def _write_dead_lock(config):
    """A lock file whose owner cannot possibly be alive."""
    lock = LoopLock(config.state_dir)
    lock.acquire()
    lock._owned = False  # keep the file: simulate a process that died holding it
    import json

    data = json.loads(lock.path.read_text(encoding="utf-8"))
    data["pid"] = 999_999_999  # never a live pid
    lock.path.write_text(json.dumps(data), encoding="utf-8")
    return lock


# --- the safe repairs ---------------------------------------------------------


def test_a_dead_lock_is_cleared(wired, capsys):
    _write_dead_lock(wired)
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 0

    assert not (wired.state_dir / "LOCK").exists()
    assert "stale lock removed" in capsys.readouterr().out


def test_a_live_lock_means_already_running_not_a_fault(wired, capsys):
    """The case that shipped wrong in the first draft: a held lock is the
    healthy already-running state, and pressing start twice must not read as
    'something needs a decision'. It also must not run the repairs — restarting
    a working run's browser mid-request breaks a run that was fine."""
    def must_not_probe(url):
        raise AssertionError(
            "start touched the browser while a live run held the lock"
        )

    monkeypatch_target = cli._default_probe_cdp
    cli._default_probe_cdp = must_not_probe
    try:
        with LoopLock(wired.state_dir):
            code = cli._cmd_start(_args())
    finally:
        cli._default_probe_cdp = monkeypatch_target

    out = capsys.readouterr().out
    assert code == 0
    assert "already running" in out
    assert "need a decision" not in out


def test_the_pause_flag_is_cleared_because_start_means_run(wired, capsys):
    StateStore(wired.state_file).save(_a_state())
    wired.pause_file.parent.mkdir(parents=True, exist_ok=True)
    wired.pause_file.touch()

    assert cli._cmd_start(_args()) == 0

    assert not wired.pause_file.exists()
    assert "flag cleared" in capsys.readouterr().out


def test_a_silent_browser_is_restarted_via_the_declared_command(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(config.state_file).save(_a_state())
    object.__setattr__(config.browser, "restart_command", ("true",))

    probes = {"n": 0}

    def flaky(url):
        probes["n"] += 1
        if probes["n"] == 1:
            raise OSError("connection refused")
        return '{"Browser":"Chrome"}'

    monkeypatch.setattr(cli, "_default_probe_cdp", flaky)

    assert cli._cmd_start(_args()) == 0
    assert "restarted, CDP answering" in capsys.readouterr().out


def test_a_silent_browser_with_no_restart_command_refuses_to_start(config, monkeypatch, capsys):
    """Never infer which Chrome to kill. Without a declared command this is a
    human's job, and saying so beats pattern-matching process lists."""
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(config.state_file).save(_a_state())

    def dead(url):
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "_default_probe_cdp", dead)

    assert cli._cmd_start(_args()) == 2
    assert "no browser.restart_command" in capsys.readouterr().out


# --- what it must NOT do ------------------------------------------------------


def test_an_open_blocker_is_reported_never_resolved(wired, capsys):
    """Resolving a blocker means answering a question nobody has read. The
    command prints the exact `answer` invocation and stops."""
    store = BlockerStore(wired.blockers_dir)
    blocker = store.record(
        task_id="t-1",
        kind="task_fatal",
        code="approved_paths_missing",
        question="task t-1 has no approved_paths; add them and retry",
        detail="",
        phase="executing",
        now="2026-08-02T00:00:00+00:00",
    )
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 2

    out = capsys.readouterr().out
    assert "1 OPEN" in out
    assert f"python -m autoloop answer {blocker.id}" in out
    assert store.open_blockers(), "the blocker must still be open"


def test_a_parked_session_is_reported_never_auto_answered(wired, capsys):
    """`needs_user` would make continuous mode stop on the first pass, so
    starting into it is pointless — but the answer is the operator's."""
    StateStore(wired.state_file).save(
        _a_state(phase=Phase.NEEDS_USER.value, question="which action should be taken?")
    )

    assert cli._cmd_start(_args()) == 2

    out = capsys.readouterr().out
    assert "PARKED at needs_user" in out
    assert "which action should be taken?" in out
    assert "run --answer" in out
    # untouched: the phase is still parked for the operator to resolve
    assert StateStore(wired.state_file).load().phase == Phase.NEEDS_USER.value


def test_a_task_fatal_park_with_no_blocker_does_not_stop_start(wired, capsys):
    """Not every park needs a human. `_handle_parked_task` quarantines a
    task_fatal park's task and keeps going on the rest of the roadmap — that
    is the task_fatal/loop_fatal split. Refusing on every `needs_user` would
    send the operator off to resolve something the loop handles itself."""
    StateStore(wired.state_file).save(
        _a_state(
            phase=Phase.NEEDS_USER.value,
            park_kind="task_fatal",
            question="task t-9 failed its own validation",
        )
    )

    assert cli._cmd_start(_args()) == 0

    out = capsys.readouterr().out
    assert "quarantines that task and continues" in out
    assert "all clear" in out


def test_a_loop_fatal_park_still_stops_start(wired, capsys):
    StateStore(wired.state_file).save(
        _a_state(
            phase=Phase.NEEDS_USER.value,
            park_kind="loop_fatal",
            question="the conversation is unusable",
        )
    )

    assert cli._cmd_start(_args()) == 2
    assert "PARKED at needs_user" in capsys.readouterr().out


def test_a_failed_session_is_reported_with_its_recovery(wired, capsys):
    StateStore(wired.state_file).save(_a_state(phase=Phase.FAILED.value))

    assert cli._cmd_start(_args()) == 2
    assert "run --retry" in capsys.readouterr().out


def test_a_clean_slate_starts(wired, capsys):
    """No session yet is the fresh-checkout case, not a fault."""
    assert cli._cmd_start(_args()) == 0
    assert "all clear" in capsys.readouterr().out


def test_check_only_never_launches_the_loop(wired, monkeypatch, capsys):
    launched = []
    monkeypatch.setattr(cli, "_cmd_run", lambda a: launched.append(a) or 0)
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args(check_only=True)) == 0
    assert launched == []

    assert cli._cmd_start(_args(check_only=False)) == 0
    assert len(launched) == 1
    assert launched[0].continuous is True


# --- a retired task cannot keep the loop stopped ------------------------------
#
# `start` refuses while any blocker is open, and a blocker is read from its own
# file rather than from the registry. So retiring a quarantined task used to fix
# only half of it: the dashboard row said RETIRED / "waits on nobody" while
# `start` still stopped on the question that task had parked with — the same
# one-status-two-meanings failure `TaskState.RETIRED` exists to end, rebuilt one
# file over. These run the whole path, `retire` command included.


def _quarantine(config, task_id, code, kind="task_fatal"):
    """A task blocked in the registry AND holding an open blocker record —
    what a `task_fatal` park actually leaves behind.

    `kind="loop_fatal"` writes the other shape: a LOOP-WIDE condition that
    merely names the task that happened to be in flight when it fired. The
    registry side is the same either way (a `loop_fatal` park leaves the task
    in_progress rather than blocked, but what is under test here is the blocker
    record, and `block` is the shorter route to a task the sweep will consider)."""
    from autoloop.tasks import Task, TaskRegistry, TaskStore

    store = TaskStore(config.tasks_file)
    registry = store.load() or TaskRegistry([])
    if not registry.has(task_id):
        registry.add(Task(id=task_id, title="t", description="d"))
    registry.block(task_id, f"parked: {code}")
    store.save(registry)
    return BlockerStore(config.blockers_dir).record(
        task_id=task_id, kind=kind, code=code,
        question=f"task {task_id} parked with {code}; what now?", detail="",
        phase="executing", now="2026-08-14T00:00:00+00:00",
    )


def _retire_args(task_id, superseded_by=None):
    return argparse.Namespace(
        config=None, task_id=task_id, superseded_by=superseded_by, reason=""
    )


def test_start_and_health_stop_caring_about_a_retired_task_and_nothing_else(wired, capsys):
    """End to end, and deliberately with a SECOND quarantined task: the claim
    is that a retirement stops blocking startup *solely* on its own account,
    not that retiring one task clears the queue."""
    from autoloop import health

    superseded = _quarantine(wired, "old-01", "attempt_count_ceiling")
    genuine = _quarantine(wired, "audit-0003", "validation_failed")
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 2, "two open blockers, both unanswered"
    assert health.check(wired).code == health.STUCK_BLOCKED
    capsys.readouterr()

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0
    capsys.readouterr()

    # Still refused — but only on the genuine failure's account now.
    assert cli._cmd_start(_args()) == 2
    out = capsys.readouterr().out
    assert "1 OPEN" in out
    assert genuine.id in out
    assert superseded.id not in out
    assert health.check(wired).open_blockers == 1

    # Answer the one that really is a question, and the loop starts.
    cli._cmd_answer(argparse.Namespace(config=None, blocker_id=genuine.id, text="rerun it"))
    capsys.readouterr()

    assert cli._cmd_start(_args()) == 0
    assert "all clear" in capsys.readouterr().out
    assert health.check(wired).code != health.STUCK_BLOCKED

    # Nothing was deleted to get here: the superseded task's blocker is closed
    # with a machine reason, never an operator answer.
    closed = BlockerStore(wired.blockers_dir).load(superseded.id)
    assert closed.resolved_at is not None
    assert closed.answer is None
    assert "retired" in closed.archived_reason


def test_start_reconciles_a_retirement_that_predates_the_state(wired, capsys):
    """The six historical rows are re-filed by `tasks._migrate_retirements` on
    LOAD — no command is run, so nothing else would ever notice their blocker
    records were left open. `start` is where that lands, so `start` is where it
    is reconciled."""
    from autoloop.tasks import TaskStore

    _quarantine(wired, "brw-02", "attempt_count_ceiling")
    store = TaskStore(wired.tasks_file)
    registry = store.load()
    registry.block("brw-02", "superseded by brw-06")  # the reason the table matches on
    store.save(registry)
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 0

    out = capsys.readouterr().out
    assert "task brw-02 is retired" in out
    assert not BlockerStore(wired.blockers_dir).open_blockers()


def test_retiring_a_task_never_closes_a_loop_fatal_blocker_naming_it(wired, capsys):
    """The sweep is scoped by KIND, not just by task id.

    A `loop_fatal` park is a loop-wide safety condition, and several of them
    name whatever task was in flight when they fired — `checkout_escape_detected`
    here, but `primary_checkout_dirty` and the worker/publisher environment
    failures are the same shape. Retiring that task answers the quarantine and
    NOTHING about the escaped write: closing it would manufacture resolution of
    the safety condition and let `start` proceed into a checkout that was
    written outside the worker."""
    from dataclasses import asdict

    from autoloop import health

    quarantine = _quarantine(wired, "old-01", "attempt_count_ceiling")
    escape = _quarantine(wired, "old-01", "checkout_escape_detected", kind="loop_fatal")
    StateStore(wired.state_file).save(_a_state())
    assert len(BlockerStore(wired.blockers_dir).open_blockers()) == 2, (
        "two distinct codes, so two records — `record` upserts on (task, code, phase)"
    )
    before = asdict(BlockerStore(wired.blockers_dir).load(escape.id))

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0
    capsys.readouterr()

    blockers = BlockerStore(wired.blockers_dir)
    # The quarantine goes, archived rather than answered — nobody responded.
    closed = blockers.load(quarantine.id)
    assert closed.resolved_at is not None
    assert closed.answer is None
    assert "retired" in closed.archived_reason

    # The loop-fatal record survives untouched — not resolved, not archived,
    # not re-worded, not even bumped.
    assert [b.id for b in blockers.open_blockers()] == [escape.id]
    assert asdict(blockers.load(escape.id)) == before

    # And it still stops the loop and still reads as needing attention.
    assert cli._cmd_start(_args()) == 2
    out = capsys.readouterr().out
    assert "1 OPEN" in out and escape.id in out
    assert quarantine.id not in out
    assert health.check(wired).code == health.STUCK_BLOCKED


def test_the_historical_sweep_cannot_clear_a_loop_fatal_record_either(wired, capsys):
    """Same rule on the load-time path. `brw-02` is one of the six migrated by
    `tasks._RETIREMENTS` — its status moves with no command run, so `start`'s
    preflight is the sweep that sees it, and that sweep must be no broader than
    the one `retire` runs. A migration silently clearing a dirty-checkout record
    would be the worst version of this: nobody typed anything."""
    from dataclasses import asdict

    from autoloop.tasks import TaskStore

    quarantine = _quarantine(wired, "brw-02", "attempt_count_ceiling")
    dirty = _quarantine(wired, "brw-02", "primary_checkout_dirty", kind="loop_fatal")
    store = TaskStore(wired.tasks_file)
    registry = store.load()
    registry.block("brw-02", "superseded by brw-06")  # the reason the table matches on
    store.save(registry)
    StateStore(wired.state_file).save(_a_state())
    before = asdict(BlockerStore(wired.blockers_dir).load(dirty.id))

    assert cli._cmd_start(_args()) == 2, "the loop-fatal record still needs a human"

    out = capsys.readouterr().out
    assert f"{quarantine.id} closed" in out and "task brw-02 is retired" in out
    blockers = BlockerStore(wired.blockers_dir)
    assert [b.id for b in blockers.open_blockers()] == [dirty.id]
    assert asdict(blockers.load(dirty.id)) == before


def test_start_leaves_a_genuinely_quarantined_task_alone(wired, capsys):
    """audit-0003 is the one real failure among the seven blocked rows, and the
    sweep must never reach it — `state_of` decides membership, so only an
    actual retirement counts."""
    blocker = _quarantine(wired, "audit-0003", "validation_failed")
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 2

    assert [b.id for b in BlockerStore(wired.blockers_dir).open_blockers()] == [blocker.id]
    assert "1 OPEN" in capsys.readouterr().out


def test_start_reports_an_unreadable_task_file_instead_of_crashing(wired, capsys):
    """`start` exists to say what is wrong rather than die of it — the loading
    it now does for the retirement sweep must not change that."""
    wired.tasks_file.write_text("{not json", encoding="utf-8")
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 2
    assert "UNREADABLE" in capsys.readouterr().out


# --- a failed audit must not take the loop down ------------------------------


def test_a_synthetic_audit_unit_is_registered_so_it_can_be_quarantined(config):
    """Audit units are minted per run and never planned, so `block` refused
    them as task_unknown and `_handle_parked_task` escalated the park to
    loop_fatal — every audit that failed its own post-commit validation
    stopped the whole loop. Observed 2026-08-02 with `audit-0003`, refused on
    a flaky test."""
    from autoloop.tasks import TaskRegistry

    registry = TaskRegistry([])
    cli._register_synthetic_audit_unit(registry, "audit-0003")

    assert registry.has("audit-0003")
    registry.block("audit-0003", "failed its own validation")  # must not raise
    assert registry.get("audit-0003").status == "blocked"


def test_registering_is_narrow_and_idempotent(config):
    """Only minted audit ids, only when unknown. A real planned task that
    `block` refuses must still escalate exactly as before."""
    from autoloop.tasks import Task, TaskRegistry

    registry = TaskRegistry([Task(id="real-1", title="t", description="d")])

    for not_an_audit in ("real-1", "audit", "audit-x", "auditing-0001", "t-0001"):
        cli._register_synthetic_audit_unit(registry, not_an_audit)
    assert {t.id for t in registry.all_tasks()} == {"real-1"}

    cli._register_synthetic_audit_unit(registry, "audit-0007")
    registry.block("audit-0007", "first")
    cli._register_synthetic_audit_unit(registry, "audit-0007")  # already known
    assert registry.get("audit-0007").status == "blocked"
