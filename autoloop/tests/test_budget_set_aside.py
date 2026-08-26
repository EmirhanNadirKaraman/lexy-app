"""A budget or a ceiling sets its OWN task aside, instead of stopping the loop
(halt-01, 2026-08-26).

The one claim under test, stated as the loop has to satisfy it:

    With `autonomy.enabled` on, each of `attempt_count_ceiling`,
    `review_round_cap`, `parse_budget_exhausted`, `plan_denial_budget_
    exhausted`, `policy_denial_budget_exhausted`, `review_mismatch_budget_
    exhausted` and `git_failure_budget_exhausted` records its reason, sets the
    ONE task in flight aside, and lets `run --continuous` pick up the next ready
    task with no operator. With the flag off, every one of them ends exactly as
    it does today.

THE EIGHTH CODE IS THE POINT OF THE CORRECTION, not an omission.
`iteration_budget_exhausted` is a SESSION ceiling: it counts the run's
iterations, so no task is at fault, and setting one aside deletes the session
file — the next iteration would start at `iteration = 0` with a full budget,
making `policy.max_iterations` unenforceable while blocking the backlog one task
at a time. It stays a loop-level stop, and `blockers.SESSION_CEILING_CODES`
refuses it the way a hard halt is refused rather than merely leaving it out of a
list. Section 6 owns that claim.

TWO OF THE SEVEN CHANGE NOTHING, and this file says so rather than letting the
reader discover it. `attempt_count_ceiling` and `review_round_cap` already
classify `task_fatal` at their own park sites, so the claim already held for
them in both flag positions. Their table entries put the guarantee on record and
route it through the same active-task validation as the rest; section 4 asserts
the behaviour in BOTH positions so the no-op is visible rather than implied.

Self-contained per this codebase's convention (see `test_blockers.py`'s
docstring) — the small config/orchestrator helpers are duplicated here rather
than imported from another test module.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from autoloop import cli
from autoloop.blockers import (
    AUTONOMOUS_RECOVERIES,
    EXHAUSTED_BUDGET_RECOVERIES,
    HARD_HALT_CODES,
    NO_TASK,
    RECOVER_BY_REBUILDING_AT_HEAD,
    RECOVER_BY_RESUBMITTING,
    RECOVER_BY_RESUMING,
    RECOVER_UNAVAILABLE,
    SESSION_CEILING_CODES,
    STALE_EXECUTION_RECORD,
    AutonomousRecovery,
    BlockerStore,
    autonomous_recovery,
)
from autoloop.config import AutoloopConfig, AutonomyConfig, BrowserConfig
from autoloop.errors import StateCorruptError
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_CEILING_EXTENSIONS,
    MAX_SPLIT_DEPTH,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import TaskExecution

URL = "https://chatgpt.com/c/budget-set-aside"

#: The SESSION ceiling — named once so every test below reads the same value.
SESSION_CEILING_CODE = "iteration_budget_exhausted"

#: The seven codes halt-01's claim names that ARE task-scoped.
TASK_SCOPED_CODES = (
    "attempt_count_ceiling",
    "review_round_cap",
    "parse_budget_exhausted",
    "plan_denial_budget_exhausted",
    "policy_denial_budget_exhausted",
    "review_mismatch_budget_exhausted",
    "git_failure_budget_exhausted",
)

#: All EIGHT the claim names, listed whole so the subtraction below is a
#: subtraction from a set that actually contains the excluded member — a
#: subtraction from a set that never held it would assert nothing while reading
#: as if it did.
SPECIFIED_CODES = TASK_SCOPED_CODES + (SESSION_CEILING_CODE,)

#: The one member that ends the run through `_to_fault_stop` rather than
#: parking, and therefore the one that needed new code at all.
FAULT_STOP_CODE = "policy_denial_budget_exhausted"

#: The six that reach `_to_needs_user`.
PARKING_CODES = tuple(c for c in TASK_SCOPED_CODES if c != FAULT_STOP_CODE)

#: How each code is ACTUALLY raised in `orchestrator.py`, as
#: `(emitter, kind literal, does the site name a task)`. Section 2 reads the
#: real park sites and asserts this table matches, so the arguments the
#: behavioural tests below replay cannot drift away from the sites they stand
#: in for. `_to_fault_stop` takes no `kind`, hence the empty string.
PARK_SITES = {
    "attempt_count_ceiling": ("_to_needs_user", "task_fatal", True),
    "review_round_cap": ("_to_needs_user", "task_fatal", True),
    "parse_budget_exhausted": ("_to_needs_user", "loop_fatal", False),
    "plan_denial_budget_exhausted": ("_to_needs_user", "loop_fatal", False),
    "policy_denial_budget_exhausted": ("_to_fault_stop", "", True),
    "review_mismatch_budget_exhausted": ("_to_needs_user", "loop_fatal", False),
    "git_failure_budget_exhausted": ("_to_needs_user", "loop_fatal", False),
    SESSION_CEILING_CODE: ("_to_needs_user", "loop_fatal", False),
}


# =============================================================================
# helpers
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


def build(tmp_path, *, enabled=False, max_recovery_attempts=2, with_store=True,
          tasks=("t1",), in_flight="t1", dispatched=None):
    """A collaborator-free Orchestrator — `_to_needs_user` and `_to_fault_stop`
    touch only `state`, `_log`, `_blocker_store`, `_store`, `_registry` and
    `_task_store`, so `None` stand-ins are enough for every terminal test here.

    `in_flight` seeds `state.task_execution` and `dispatched` seeds
    `state.current_task`; between them they decide the loop's ONE active task,
    which is the only thing a set-aside will ever quarantine (setaside-01).
    """
    config = make_config(
        tmp_path, enabled=enabled, max_recovery_attempts=max_recovery_attempts
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    if in_flight is not None:
        state.task_execution = {"task_id": in_flight}
    if dispatched is not None:
        state.current_task = {"task_id": dispatched, "title": f"Title {dispatched}"}
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


def raise_at_site(orch, code: str) -> None:
    """Replay `code`'s REAL park site — the emitter it uses, the `kind` it
    passes and whether it names a task — as `PARK_SITES` records it and section
    2 verifies it against `orchestrator.py`.

    One helper rather than a literal per test, so a site that changes shape
    fails section 2 loudly instead of leaving these tests exercising arguments
    nothing produces. Deliberately takes NO overrides: a test that wants a
    different `task_id` than the site passes is not replaying that site, and
    the two bystander cases below call the emitters directly and say so.
    """
    emitter, kind, names_task = PARK_SITES[code]
    named = "t1" if names_task else None
    reason = f"{code}: the budget ran out"
    if emitter == "_to_fault_stop":
        orch._to_fault_stop(reason, code=code, task_id=named, detail="d")
        return
    orch._to_needs_user(reason, kind=kind, code=code, task_id=named, detail="d")


def transcript_types(config) -> list[str]:
    if not config.transcript_file.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def transcript_entries(config, entry_type: str) -> list[dict]:
    """The `data` payload of every transcript entry of one type."""
    if not config.transcript_file.exists():
        return []
    out = []
    for line in config.transcript_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == entry_type:
            out.append(entry.get("data") or {})
    return out


def _blocker_calls():
    """Every `_to_needs_user` / `_to_fault_stop` call in `orchestrator.py`
    whose `code=` is a plain string literal, as
    `(code, emitter, kind literal, names a task)`.

    AST-walked rather than grepped, for the same reason
    `test_m1_hardening._emitted_blocker_codes` is: a `code=` built from a
    conditional still yields its literals, and a docstring that happens to
    mention a code cannot be mistaken for a site that raises it. A call whose
    `code=` is a NAME (the delegation inside
    `_autonomous_fault_set_aside`) contributes nothing here, which is correct:
    it re-raises somebody else's code rather than owning one.
    """
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("_to_needs_user", "_to_fault_stop"):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        raw_code = keywords.get("code")
        if not isinstance(raw_code, ast.Constant) or not isinstance(raw_code.value, str):
            continue
        raw_kind = keywords.get("kind")
        kind = raw_kind.value if isinstance(raw_kind, ast.Constant) else ""
        names_task = "task_id" in keywords
        calls.append((raw_code.value, name, kind, names_task))
    return calls


def emitted_blocker_codes() -> set[str]:
    """Every code a blocker-emitting call in `orchestrator.py` can carry."""
    return {code for code, _, _, _ in _blocker_calls()}


# =============================================================================
# 1. the table — the seven that are automated, and the one that is refused
# =============================================================================


def test_the_table_holds_exactly_the_seven_task_scoped_budgets_and_ceilings():
    assert set(EXHAUSTED_BUDGET_RECOVERIES) == set(TASK_SCOPED_CODES)
    assert len(EXHAUSTED_BUDGET_RECOVERIES) == 7
    assert set(EXHAUSTED_BUDGET_RECOVERIES) <= set(AUTONOMOUS_RECOVERIES), (
        "halt-01's half must be reachable through the one lookup"
    )
    for code, entry in EXHAUSTED_BUDGET_RECOVERIES.items():
        assert entry.code == code, "the table is keyed by its own code"
        assert autonomous_recovery(code) is entry
        assert entry.why.strip(), f"{code} automates without saying why"


def test_none_of_them_retries_because_none_of_them_has_a_recovery_path():
    """A counter that reached its limit is not a transient fault: the only thing
    that could change the answer is an operator raising the limit or rewriting
    the task, and the loop must do neither for itself. So the recovery path is
    EMPTY — exhausted on the first occurrence — and the set-aside, the half that
    was missing, fires at once.

    `stale_record` must stay empty too: it is required only for a rebuild, and a
    value here would route one of these into `_autonomous_rebuild`."""
    for code, entry in EXHAUSTED_BUDGET_RECOVERIES.items():
        assert entry.action == RECOVER_UNAVAILABLE, code
        assert entry.max_attempts == 0, code
        assert entry.stale_record == "", code


def test_every_automated_budget_code_is_one_the_orchestrator_can_actually_raise():
    """The rule halt-02 stated and this half inherits: automating a code nothing
    emits reads as coverage while covering a path nothing reaches."""
    emitted = emitted_blocker_codes()
    unreachable = set(EXHAUSTED_BUDGET_RECOVERIES) - emitted
    assert not unreachable, f"automated codes nothing emits: {sorted(unreachable)}"


def test_the_session_ceiling_is_excluded_by_decision_and_refused_outright():
    """The correction auto-02 carried, pinned from both sides.

    It must be a REAL code — an exclusion that named nothing would be a
    guarantee about a park that cannot happen — it must be absent from the
    merged table, and `autonomous_recovery` must refuse it even if some later
    edit puts it there, which is what the disjointness assertion protects."""
    assert SESSION_CEILING_CODES == {SESSION_CEILING_CODE}
    assert SESSION_CEILING_CODE in SPECIFIED_CODES, "the subtraction above is vacuous"
    assert SESSION_CEILING_CODE in emitted_blocker_codes()
    assert SESSION_CEILING_CODES.isdisjoint(set(AUTONOMOUS_RECOVERIES))
    assert autonomous_recovery(SESSION_CEILING_CODE) is None


def test_the_five_hard_halts_are_still_disjoint_from_the_grown_table():
    """Growing the allowlist is exactly how a hard halt gets automated by
    accident, so the disjointness is re-asserted against the MERGED table here
    as well as in `test_autonomous_recovery.py`."""
    assert HARD_HALT_CODES.isdisjoint(set(AUTONOMOUS_RECOVERIES))
    assert HARD_HALT_CODES.isdisjoint(SESSION_CEILING_CODES)
    for code in HARD_HALT_CODES:
        assert autonomous_recovery(code) is None


# =============================================================================
# 2. the sites these tests replay are the sites the loop actually has
# =============================================================================


@pytest.mark.parametrize("code", sorted(PARK_SITES))
def test_each_code_is_raised_by_the_site_these_tests_replay(code):
    """`raise_at_site` stands in for eight real park sites. If one of them
    changes emitter, `kind`, or whether it names a task, every behavioural test
    below would keep passing against arguments nothing produces — the
    fail-silent shape this file exists to avoid. So the table is read off
    `orchestrator.py` instead of trusted."""
    expected = PARK_SITES[code]
    found = {(emitter, kind, names) for c, emitter, kind, names in _blocker_calls()
             if c == code}
    assert found == {expected}, f"{code} is raised as {sorted(found)}, not {expected}"


# =============================================================================
# 3. DEFAULT OFF — the reversibility half of the claim
# =============================================================================


@pytest.mark.parametrize("code", PARKING_CODES)
def test_with_the_flag_off_every_parking_code_parks_exactly_as_before(tmp_path, code):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=False)
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)

    _, site_kind, names_task = PARK_SITES[code]
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind
    assert orch.state.park_task_id == ("t1" if names_task else None)
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert blocker.kind == site_kind
    assert blocker.task_id == ("t1" if names_task else NO_TASK)
    assert "autonomous_recovery" not in transcript_types(config)


def test_with_the_flag_off_the_policy_denial_budget_still_ends_the_run(tmp_path):
    """The fault stop is the terminal most easily broken by this change, so its
    off position gets its own test rather than riding a parametrize."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=False)
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, FAULT_STOP_CODE)

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert orch.state.question is None and orch.state.park_kind is None
    blocker = blocker_store.load(orch.state.stop_blocker_id)
    assert (blocker.kind, blocker.code, blocker.task_id) == (
        "loop_fatal", FAULT_STOP_CODE, "t1"
    )
    assert "autonomous_fault_set_aside" not in transcript_types(config)


# =============================================================================
# 4. FLAG ON — each of the seven sets its task aside, with the reason recorded
# =============================================================================


@pytest.mark.parametrize("code", PARKING_CODES)
def test_with_the_flag_on_every_parking_code_sets_the_round_in_flight_aside(
    tmp_path, code
):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.code) == ("task_fatal", "t1", code)
    # THE REASON IS RECORDED, which is half of what the claim promises: the
    # question an operator would have been shown is on the durable record, not
    # traded away for the loop continuing.
    assert blocker.question == f"{code}: the budget ran out"
    assert blocker.detail == "d"
    assert blocker.phase == Phase.EXECUTING.value
    # And no retry was spent: these have no recovery path to re-enter.
    assert "autonomous_recovery" not in transcript_types(config)


def test_with_the_flag_on_the_policy_denial_budget_sets_its_task_aside(tmp_path):
    """THE new behaviour, and the only member that needed new code.

    `_to_fault_stop` ended the run for an exhausted budget that is task-shaped
    in practice — the denials are about one task's missing decomposition, its
    quarantine, its unanswered ceiling classification. Under autonomous mode it
    becomes the set-aside park instead."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, FAULT_STOP_CODE)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert orch.state.stop_kind == "", "a set-aside park is not a fault stop"
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.code) == (
        "task_fatal", "t1", FAULT_STOP_CODE
    )
    assert blocker.question == f"{FAULT_STOP_CODE}: the budget ran out"
    # Exactly ONE record: the delegation happens before anything is written, so
    # the loop_fatal record the fault stop would have made never exists.
    assert len(blocker_store.all_blockers()) == 1
    handovers = transcript_entries(config, "autonomous_fault_set_aside")
    assert handovers and handovers[0]["task_id"] == "t1"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("code", ["attempt_count_ceiling", "review_round_cap"])
def test_the_two_already_task_fatal_codes_quarantine_in_both_flag_positions(
    tmp_path, code, enabled
):
    """THE NO-OP, asserted rather than left to be discovered.

    Both sites already pass `kind="task_fatal"`, so halt-01's claim held for
    them before this change and holds after it, in either flag position. Their
    table entries are not behaviour: they are what holds the guarantee to
    `blockers.AUTONOMOUS_RECOVERIES` and to the active-task validation, instead
    of to two park sites that happen to agree with it today."""
    orch, _, blocker_store, _, _ = build(
        tmp_path, enabled=enabled, tasks=("t1", "t2")
    )
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)

    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert blocker_store.load(orch.state.park_blocker_id).kind == "task_fatal"


# =============================================================================
# 5. …and the loop picks up the NEXT ready task, with no operator
# =============================================================================


@pytest.mark.parametrize("code", [
    # The discriminating one: its site names NO task, so the victim can only
    # come from the in-flight fallback that this change relies on.
    "git_failure_budget_exhausted",
    # The other end: the fault stop, which did not park at all before.
    FAULT_STOP_CODE,
])
def test_the_set_aside_lets_continuous_mode_continue_with_the_next_task(
    tmp_path, capsys, code
):
    """The half that matters, driven through the unmodified
    `cli._handle_parked_task`: t1 is quarantined, t2 stays dispatchable, the
    session file is cleared so the next iteration starts fresh, and the caller
    is told to carry on."""
    orch, config, _, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2")
    )
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)
    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "task_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert reloaded.state_of("t2") is TaskState.READY, "the loop still has work"
    assert reloaded.next_ready().id == "t2", "the NEXT ready task is selectable"
    assert not config.state_file.exists(), "the parked session was not cleared"
    assert "continuous mode continues" in capsys.readouterr().out


@pytest.mark.parametrize("code", ["git_failure_budget_exhausted", FAULT_STOP_CODE])
def test_the_same_exhaustion_without_autonomy_stops_the_loop(tmp_path, capsys, code):
    """The contrast that makes the test above mean something. One task's spent
    budget takes the whole roadmap down: nothing is quarantined and nothing
    continues."""
    orch, config, _blockers, task_store, registry = build(
        tmp_path, enabled=False, tasks=("t1", "t2")
    )
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)
    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "loop_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY
    capsys.readouterr()


# =============================================================================
# 6. the SESSION ceiling stays a loop-level stop
# =============================================================================


def test_the_session_ceiling_still_stops_the_loop_with_the_flag_on(tmp_path):
    """auto-02's correction, as behaviour.

    A task IS in flight and the flag IS on, so every ingredient of a set-aside
    is present — and the loop still parks `loop_fatal` with no victim, because
    the code never reaches the table at all."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("iteration budget exhausted (20)",
                        resume_phase=Phase.READY.value, kind="loop_fatal",
                        code=SESSION_CEILING_CODE)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert orch.state.resume_phase == Phase.READY.value, (
        "the operator's route back — raise the limit, then `run --retry` — is "
        "unchanged"
    )
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    assert "autonomous_recovery" not in transcript_types(config)
    # The plan is `None`, so the set-aside helper is never entered and there is
    # no refusal to record — the code is refused BEFORE any of that.
    assert not transcript_entries(config, "autonomous_set_aside_refused")


def test_the_session_ceiling_quarantines_nothing_and_keeps_the_session(
    tmp_path, capsys
):
    """Through `cli._handle_parked_task`, the same way section 5 drives the
    seven: the loop stops, no task is blocked, and the session file survives so
    a `run --retry` after the limit is raised re-enters `ready` with the outbox
    intact."""
    orch, config, _, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2")
    )
    orch.state.phase = Phase.READY.value
    orch._to_needs_user("iteration budget exhausted (20)",
                        resume_phase=Phase.READY.value, kind="loop_fatal",
                        code=SESSION_CEILING_CODE)

    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "loop_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY
    assert config.state_file.exists()
    capsys.readouterr()


def test_the_session_ceiling_is_not_automated_even_by_a_site_that_names_a_task(
    tmp_path
):
    """The forward-looking half. Today's site names no task; if one ever did,
    the refusal must still hold — it is on the CODE, not on whether a victim
    happens to be available."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("iteration budget exhausted (20)", kind="loop_fatal",
                        code=SESSION_CEILING_CODE, task_id="t1")

    assert orch.state.park_kind == "loop_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).kind == "loop_fatal"


def test_the_session_ceiling_is_not_reclassified_by_repetition(tmp_path):
    """No number of recurrences turns it into a set-aside — the refusal is on
    the code, not on a counter."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    for _ in range(4):
        orch.state.phase = Phase.READY.value
        orch._to_needs_user("iteration budget exhausted (20)",
                            resume_phase=Phase.READY.value, kind="loop_fatal",
                            code=SESSION_CEILING_CODE)
        assert orch.state.park_kind == "loop_fatal"


# =============================================================================
# 7. the five hard halts stay hard, with the table three families wide
# =============================================================================


@pytest.mark.parametrize("code", sorted(HARD_HALT_CODES))
@pytest.mark.parametrize("site_kind", ["loop_fatal", "task_fatal"])
def test_a_hard_halt_keeps_its_own_classification_with_the_flag_on(
    tmp_path, code, site_kind
):
    """BOTH kinds, because the five do not share one today — the property is
    "autonomous mode does not touch it", not "it halts the loop"."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user(f"task t1: {code}", resume_phase=Phase.EXECUTING.value,
                        kind=site_kind, code=code, task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind, f"{code} was re-classified by autonomy"
    assert blocker_store.load(orch.state.park_blocker_id).kind == site_kind
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_hard_halt_reached_through_the_fault_stop_gate_is_refused_too(tmp_path):
    """The gate `_to_fault_stop` gained is a second door into the same table, so
    it gets the same refusal asserted against it. No site raises a hard halt
    from a fault stop today; this is the guard on that staying true."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_fault_stop("an agent wrote outside its worker repository",
                        code="checkout_escape_detected", task_id="t1")

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert blocker_store.load(orch.state.stop_blocker_id).kind == "loop_fatal"
    assert "autonomous_fault_set_aside" not in transcript_types(config)


# =============================================================================
# 8. reviewer-first composition: ask, THEN set aside (ceil-01 + halt-01)
# =============================================================================


def _ceiling_fixture(tmp_path, *, enabled, extensions=0, split_depth=0):
    orch, config, blocker_store, task_store, registry = build(
        tmp_path, enabled=enabled, tasks=("t1", "t2")
    )
    task = registry.get("t1")
    task.attempt_extensions = extensions
    task.split_depth = split_depth
    execution = TaskExecution(
        task_id="t1", task_branch="autoloop/t1",
        worktree_path=str(tmp_path / "wt"), task_base_sha="0" * 40,
        candidate_sha="a" * 40, attempt_count=5,
    )
    # Stubbed because the request's RENDERING is `_ceiling_plan_request`'s claim
    # and needs a git gateway; what is under test here is the ORDER — that the
    # ask happens and the park does not.
    orch._ceiling_plan_request = lambda *a, **k: "CEILING PLAN REQUEST"
    return orch, config, blocker_store, task_store, registry, task, execution


def test_a_task_with_a_remedy_left_still_asks_the_reviewer_first(tmp_path):
    """ceil-01 and halt-01 COMPOSE, and this is the half that proves autonomy
    does not short-circuit the ask.

    The reviewer holds the candidate and the verdict history, so it is the one
    that can tell "one named remaining fix" from "this spec is wrong". Nothing
    in halt-01 runs until that ask has been made and has failed to resolve
    anything: the interception is at `_to_needs_user`/`_to_fault_stop`, and
    `_handle_attempt_ceiling` reaches neither while a remedy is available."""
    orch, config, blocker_store, _, _, task, execution = _ceiling_fixture(
        tmp_path, enabled=True
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._handle_attempt_ceiling(
        directive=None, task=task, execution=execution, worktree_git=None,
        state=orch.state, cap=5, is_audit=False,
    )

    assert orch.state.phase == Phase.READY.value, "the ask was skipped"
    assert orch.state.outbox == "CEILING PLAN REQUEST"
    assert orch.state.park_kind is None
    assert blocker_store.open_blockers() == [], "autonomy parked before asking"
    assert orch._registry.get("t1").ceiling_plan_requested_at
    assert "attempt_ceiling_plan_requested" in transcript_types(config)


def test_a_task_whose_remedies_are_spent_is_then_set_aside(tmp_path, capsys):
    """The other half of the composition: the ask has already happened, the
    extensions and the split depth are both used up, so there is nothing left to
    classify. THAT is when the task is set aside and the loop moves on."""
    orch, config, blocker_store, task_store, registry, task, execution = (
        _ceiling_fixture(tmp_path, enabled=True,
                         extensions=MAX_CEILING_EXTENSIONS,
                         split_depth=MAX_SPLIT_DEPTH)
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._handle_attempt_ceiling(
        directive=None, task=task, execution=execution, worktree_git=None,
        state=orch.state, cap=5, is_audit=False,
    )

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert blocker.code == "attempt_count_ceiling"
    assert "neither a further extension nor a further decomposition" in blocker.question

    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )
    assert verdict == "task_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert reloaded.next_ready().id == "t2"
    capsys.readouterr()


# =============================================================================
# 9. fail-closed edges
# =============================================================================


@pytest.mark.parametrize("code", TASK_SCOPED_CODES)
def test_without_a_blocker_store_nothing_is_automated(tmp_path, code):
    """No durable record means no durable QUESTION, and the set-aside deletes
    the session file on the strength of that record existing. So every one of
    the seven ends exactly as it does with the flag off."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, with_store=False)
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)

    _, site_kind, _ = PARK_SITES[code]
    if code == FAULT_STOP_CODE:
        assert orch.state.phase == Phase.STOPPED.value
        assert orch.state.stop_kind == "fault"
    else:
        assert orch.state.phase == Phase.NEEDS_USER.value
        assert orch.state.park_kind == site_kind
    assert "autonomous_fault_set_aside" not in transcript_types(config)


def test_a_fault_stop_with_no_task_in_flight_keeps_its_fault_stop(tmp_path):
    """THE fail-open this change is written around.

    A refused set-aside must NOT become a `loop_fatal` park: that would hold the
    session open for an answer nobody can give, which is the exact stall
    `_to_fault_stop` exists to remove. So the terminal stays a fault stop, with
    the same `stop_kind`, the same `loop_fatal` record and the same
    `stop_blocker_id` it has today."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_fault_stop("the reviewer kept proposing refused directives",
                        code=FAULT_STOP_CODE, task_id=None, detail="d")

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert orch.state.park_kind is None and orch.state.park_blocker_id is None
    assert blocker_store.load(orch.state.stop_blocker_id).kind == "loop_fatal"
    assert "autonomous_fault_set_aside" not in transcript_types(config)


def test_a_fault_stop_naming_a_bystander_keeps_its_fault_stop(tmp_path, capsys):
    """setaside-01's rule, applied to the new door: t1's round is in flight and
    the denial names t2, so nothing is quarantined — not t2, which did nothing
    wrong, and not t1, which the site did not name. The refusal is recorded, and
    the terminal is unchanged."""
    orch, config, blocker_store, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1"
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_fault_stop("denials exhausted", code=FAULT_STOP_CODE, task_id="t2",
                        detail="d")

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert blocker_store.load(orch.state.stop_blocker_id).kind == "loop_fatal"
    refusals = transcript_entries(config, "autonomous_set_aside_refused")
    assert refusals and refusals[0]["reason"] == "named_task_is_not_the_active_task"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY
    capsys.readouterr()


def test_a_disagreement_between_the_round_records_keeps_the_fault_stop(tmp_path):
    """The other setaside-01 refusal, on the same door. With `task_execution`
    naming t1 and `current_task` naming t2 the loop has no single active task,
    so it quarantines nothing and does not convert the terminal either."""
    orch, config, blocker_store, _, _ = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1", dispatched="t2"
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_fault_stop("denials exhausted", code=FAULT_STOP_CODE, task_id="t1",
                        detail="d")

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    refusals = transcript_entries(config, "autonomous_set_aside_refused")
    assert refusals and refusals[0]["reason"] == "round_identity_records_disagree"


@pytest.mark.parametrize("code", [
    c for c in PARKING_CODES if not PARK_SITES[c][2]
])
def test_a_code_whose_site_names_no_task_still_stops_a_loop_with_none_in_flight(
    tmp_path, code
):
    """Honest scoping of the claim: "sets ITS task aside" presumes there is one.

    `parse_budget_exhausted` and `plan_denial_budget_exhausted` are routinely
    raised during a plan or audit round that has no task in flight at all, and
    there `_autonomous_set_aside_task` answers `None` — a real answer, not a
    failure — so the loop parks `loop_fatal` exactly as it does today rather
    than inventing a victim. setaside-01's gate, unchanged and not widened."""
    orch, _, blocker_store, _, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight=None
    )
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, code)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    # And nothing is quarantined on the way out.
    assert registry.state_of("t1") is TaskState.READY
    assert registry.state_of("t2") is TaskState.READY


def test_a_corrupt_blocker_record_raises_rather_than_licensing_a_set_aside(tmp_path):
    """A store that cannot be READ must never answer "nothing open" — that is a
    full, unspent budget derived from evidence nobody could read. Asserted on
    the fault-stop door as well as the park one, because the new gate calls into
    the same store."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    blocker_store.directory.mkdir(parents=True, exist_ok=True)
    (blocker_store.directory / "blk-t1-001.json").write_text("{ not json",
                                                            encoding="utf-8")
    orch.state.phase = Phase.EXECUTING.value

    with pytest.raises(StateCorruptError):
        raise_at_site(orch, FAULT_STOP_CODE)


@pytest.mark.parametrize("action", [
    RECOVER_BY_RESUMING, RECOVER_BY_RESUBMITTING, RECOVER_BY_REBUILDING_AT_HEAD,
])
def test_the_fault_stop_door_only_ever_performs_a_set_aside(tmp_path, action):
    """A FAULT STOP may become a set-aside and nothing else.

    Every code that reaches the new gate today is `RECOVER_UNAVAILABLE`, so
    "step the task aside" is the whole content of its plan. A future entry with
    a real recovery path would otherwise re-issue a request, or rebuild a round,
    from a terminal that has declared itself unrecoverable — a decision nobody
    has made. The gate therefore refuses any other action outright, and the
    plans below are injected precisely because no table entry can produce them
    yet: a guard that only fires for inputs that cannot occur is untested, and
    this is what will fail when one can."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.pending_request = None
    orch._autonomy_plan = lambda code: AutonomousRecovery(
        code=code, action=action, max_attempts=2, why="injected",
        stale_record=STALE_EXECUTION_RECORD if action == RECOVER_BY_REBUILDING_AT_HEAD else "",
    )
    orch.state.phase = Phase.EXECUTING.value

    raise_at_site(orch, FAULT_STOP_CODE)

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
    assert blocker_store.load(orch.state.stop_blocker_id).kind == "loop_fatal"
    assert "autonomous_fault_set_aside" not in transcript_types(config)


def test_a_raised_config_ceiling_cannot_buy_these_codes_a_retry(tmp_path):
    """`max_recovery_attempts` restrains the table and never widens it, so a
    config asking for ten retries still gets none: the entry's own allowance is
    zero and `min(0, 10)` is zero. A retry here would re-enter the very
    condition the budget just refused."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, max_recovery_attempts=10)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("repeated git failures", resume_phase=Phase.EXECUTING.value,
                        kind="loop_fatal", code="git_failure_budget_exhausted")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_non_boolean_enabled_is_not_read_as_consent_on_the_fault_stop_door(tmp_path):
    """A hand-built config is validated by nothing, so the orchestrator's gate
    is `is not True`. Asserted on the new door too: a truthy string must not
    convert a fault stop into a park."""
    config = make_config(tmp_path)
    object.__setattr__(config, "autonomy", AutonomyConfig(enabled="yes"))  # type: ignore[arg-type]
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.task_execution = {"task_id": "t1"}
    store.save(state)
    registry = TaskRegistry([Task(id="t1", title="T", description="d",
                                  approved_paths=("a.py",))])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    orch = Orchestrator(
        config=config, store=store, state=state, policy=PolicyEngine(config.policy),
        git=None, executor=None, transcript=TranscriptLogger(config.transcript_file),
        client_factory=None, registry=registry, task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        blocker_store=BlockerStore(config.blockers_dir),
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_fault_stop("denials exhausted", code=FAULT_STOP_CODE, task_id="t1")

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == "fault"
