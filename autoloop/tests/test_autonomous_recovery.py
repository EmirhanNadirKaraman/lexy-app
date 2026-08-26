"""Autonomous recovery: a transport or environment fault exhausts its recovery
path and then steps aside, instead of stopping the loop (halt-02, 2026-08-25).

The one claim under test, stated as the loop has to satisfy it:

    With `autonomy.enabled` on, each code in `blockers.AUTONOMOUS_RECOVERIES`
    re-enters the recovery path that already exists — bounded and durably
    counted — and when that path is exhausted the ONE task in flight is set
    aside (`task_fatal`) so `run --continuous` keeps working. With the flag off,
    every one of them parks exactly as it did before. `submission_ambiguous`
    RE-ISSUES rather than parking. The five hard halts are unreachable from any
    of it.

Both halves are tested, and the OFF half is not an afterthought: a reversible
flag whose off position has drifted is not reversible.

"The ONE task in flight" is enforced since setaside-01 (2026-08-26) rather than
merely claimed: a park site may NAME a task, but only a task whose round is
actually running is ever quarantined, and a park naming any other keeps the
site's own loop-fatal terminal. Section 5b owns that claim.

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
    HARD_HALT_CODES,
    NO_TASK,
    RECOVER_BY_RESUBMITTING,
    RECOVER_BY_RESUMING,
    RECOVER_UNAVAILABLE,
    TRANSPORT_RECOVERIES,
    BlockerStore,
    autonomous_recovery,
)
from autoloop.config import AutoloopConfig, AutonomyConfig, BrowserConfig, load_config
from autoloop.errors import ConfigError, LoginExpiredError, StateCorruptError, StateError
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger

URL = "https://chatgpt.com/c/autonomy-test"

#: EVERY code halt-02 names, all seven: the six its claim says must "retry
#: through the recovery path that already exists", plus `submission_ambiguous`,
#: which the same claim says must RE-ISSUE rather than park. Listed whole,
#: including the retired one, so `RETIRED_ROTATION_CODE` below is subtracted
#: from something that actually contains it — a subtraction from a set that
#: never held it would assert nothing while reading as if it did.
SPECIFIED_CODES = (
    "login_expired",
    "rotation_failed",
    "git_unavailable_in_ready",
    "submission_ambiguous",
    "worker_environment_drift",
    "publisher_url_drift",
    "crash_reconciliation_ambiguous",
)

#: The one of those seven that no live provider can raise — brw-15 removed the
#: rotation machinery that was its only emitter. Named here once so the tests
#: below read the same value.
RETIRED_ROTATION_CODE = "rotation_failed"


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
    """A collaborator-free Orchestrator — `_to_needs_user` touches only
    `state`, `_log`, `_blocker_store` and `_store`, so `None` stand-ins are
    enough for every classification test here.

    `in_flight` seeds `state.task_execution` with a task id and `dispatched`
    seeds `state.current_task` with one. Together they are the round in flight,
    which is the ONLY thing `_autonomous_set_aside_task` will quarantine: it
    reads `task_execution` when the park site names no task, and it requires a
    site that DOES name one to name a task one of the two records already
    holds (setaside-01). Pass `in_flight=None` for the "a fault with no task
    behind it" cases.
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


def transcript_types(config) -> list[str]:
    if not config.transcript_file.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def transcript_entries(config, entry_type: str) -> list[dict]:
    """The `data` payload of every transcript entry of one type —
    `transcript_types` above answers "did it happen", this answers "with what".

    Returns the payload rather than the whole entry, matching
    `test_stale_record_rebuild.transcript_entries`: `TranscriptLogger.append`
    nests everything but `ts`/`type`/`iteration` under `data`, so a helper that
    returned the envelope would make every caller spell that out."""
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


def emitted_blocker_codes() -> set[str]:
    """Every string literal reachable as the `code=` argument of a
    blocker-emitting call in `orchestrator.py`.

    A local copy of `test_m1_hardening._emitted_blocker_codes`, deliberately:
    this module asserts REACHABILITY (a code nothing can raise must not be
    automated), and a reachability check that depends on another test module
    staying importable is a check that can be switched off by an unrelated
    edit. AST-walked so a `code=` built from a conditional still yields every
    branch's literal.
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
# 1. the table itself — allowlist, hard halts, reachability
# =============================================================================


def test_the_table_covers_every_specified_code_that_a_provider_can_still_raise():
    """SIX of the seven codes halt-02 names are automated. The seventh is the
    rotation failure, which no live provider can raise — see the test below.

    Asserted over `TRANSPORT_RECOVERIES`, halt-02's own half of the table,
    rather than over the merged `AUTONOMOUS_RECOVERIES`: halt-03 added a second
    half for STALE RECORDS (`test_stale_record_rebuild.py` owns that claim), and
    a set-equality over the merge would fail on every later addition while
    saying nothing about the codes this test is actually about."""
    assert set(TRANSPORT_RECOVERIES) == set(SPECIFIED_CODES) - {RETIRED_ROTATION_CODE}
    assert len(TRANSPORT_RECOVERIES) == 6
    assert set(TRANSPORT_RECOVERIES) <= set(AUTONOMOUS_RECOVERIES), (
        "halt-02's half must still be reachable through the one lookup"
    )
    for code, entry in TRANSPORT_RECOVERIES.items():
        assert entry.code == code, "the table is keyed by its own code"
        assert entry.action in (
            RECOVER_BY_RESUMING, RECOVER_BY_RESUBMITTING, RECOVER_UNAVAILABLE
        )
        assert entry.max_attempts >= 0
        assert entry.why.strip(), f"{code} automates without saying why"


def test_every_automated_code_is_one_the_orchestrator_can_actually_raise():
    """The rule halt-02 states in one line: a code no live provider can raise
    should be REMOVED, not automated. Automating a dead code is worse than
    useless — it reads as coverage while covering a path nothing reaches."""
    unreachable = set(AUTONOMOUS_RECOVERIES) - emitted_blocker_codes()
    assert not unreachable, f"automated codes nothing emits: {sorted(unreachable)}"


def test_the_retired_rotation_code_is_removed_rather_than_automated():
    """brw-15 removed the rotation machinery and with it the only site that
    could raise this code. halt-02 named it among the six to automate; the
    answer is that there is nothing left to automate, and the table says so by
    omission rather than by carrying a dead entry."""
    assert RETIRED_ROTATION_CODE in SPECIFIED_CODES, "the subtraction above is vacuous"
    assert RETIRED_ROTATION_CODE not in AUTONOMOUS_RECOVERIES
    assert RETIRED_ROTATION_CODE not in emitted_blocker_codes()
    assert autonomous_recovery(RETIRED_ROTATION_CODE) is None


def test_the_five_hard_halts_are_disjoint_from_the_table_and_refused_outright():
    """Two locks, and the test that keeps them agreeing. The allowlist is what
    enforces the rule; `HARD_HALT_CODES` is the redundant refusal that turns
    "somebody added one to the table" into a failing test."""
    assert HARD_HALT_CODES.isdisjoint(set(AUTONOMOUS_RECOVERIES))
    assert HARD_HALT_CODES == {
        "checkout_escape_detected",
        "worker_isolation_violation",
        "primary_checkout_dirty",
        "approved_path_symlink_traversal",
        "prompt_integrity_mismatch",
    }
    # And every one of them is real: a hard halt nothing emits would be a
    # guarantee about a code that cannot occur.
    assert HARD_HALT_CODES <= emitted_blocker_codes()
    for code in HARD_HALT_CODES:
        assert autonomous_recovery(code) is None


def test_an_unknown_or_empty_code_is_not_automated():
    assert autonomous_recovery("unclassified") is None
    assert autonomous_recovery("") is None
    assert autonomous_recovery("some_future_park_nobody_reasoned_about") is None


def test_the_three_codes_with_no_recovery_path_carry_a_zero_budget():
    """Their remedies are operator actions the loop must not perform for
    itself — repairing the shared git environment, confirming a new push
    destination, clearing a commit intent by hand. An empty recovery path is
    exhausted immediately, so the set-aside is the whole of what they get."""
    for code in ("worker_environment_drift", "publisher_url_drift",
                 "crash_reconciliation_ambiguous"):
        entry = AUTONOMOUS_RECOVERIES[code]
        assert entry.action == RECOVER_UNAVAILABLE
        assert entry.max_attempts == 0


# =============================================================================
# 2. DEFAULT OFF — the reversibility half of the claim
# =============================================================================


def test_autonomy_is_off_by_default_in_the_dataclass_and_in_a_config_without_the_section(tmp_path):
    assert AutonomyConfig().enabled is False
    assert AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
    ).autonomy.enabled is False

    path = tmp_path / "config.toml"
    path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    assert load_config(path).autonomy.enabled is False


@pytest.mark.parametrize("code", SPECIFIED_CODES)
def test_with_the_flag_off_every_named_code_parks_exactly_as_before(tmp_path, code):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=False)

    orch._to_needs_user(
        "something environmental broke",
        resume_phase=Phase.AWAITING.value,
        kind="loop_fatal",
        code=code,
    )

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert orch.state.resume_phase == Phase.AWAITING.value
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.code) == ("loop_fatal", NO_TASK, code)
    assert "autonomous_recovery" not in transcript_types(config)


def test_with_the_flag_off_a_task_fatal_site_keeps_its_own_classification(tmp_path):
    """`crash_reconciliation_ambiguous` parks task_fatal at its own site today.
    The off position must not quietly re-classify it either."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=False)
    orch._to_needs_user(
        "ambiguous", kind="task_fatal", code="crash_reconciliation_ambiguous",
        task_id="t1",
    )
    assert orch.state.park_kind == "task_fatal"
    assert orch.state.park_task_id == "t1"
    assert blocker_store.load(orch.state.park_blocker_id).kind == "task_fatal"


# =============================================================================
# 3. stage 1 — the retry
# =============================================================================


def test_login_expired_re_enters_the_phase_the_park_declared_resumable(tmp_path):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.AWAITING.value

    orch._to_needs_user(
        "session logged out",
        resume_phase=Phase.AWAITING.value,
        kind="loop_fatal",
        code="login_expired",
    )

    # It did NOT park: the loop is back in the phase it fell out of.
    assert orch.state.phase == Phase.AWAITING.value
    assert orch.state.question is None
    assert orch.state.park_kind is None
    assert orch.state.park_blocker_id is None
    assert "needs_user" not in transcript_types(config)
    assert "autonomous_recovery" in transcript_types(config)
    # And the fault is on the record, open, with the task it would be charged
    # to already resolved — nothing about it became invisible.
    open_records = blocker_store.open_blockers()
    assert [(b.code, b.task_id, b.kind, b.recurrences) for b in open_records] == [
        ("login_expired", "t1", "task_fatal", 1)
    ]
    # The state file agrees with the object: a crash here resumes the retry.
    assert StateStore(config.state_file).load().phase == Phase.AWAITING.value


def test_the_retry_budget_is_spent_and_then_the_task_is_set_aside(tmp_path):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True,
                                              max_recovery_attempts=2)
    for attempt in range(2):
        orch.state.phase = Phase.AWAITING.value
        orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                            kind="loop_fatal", code="login_expired")
        assert orch.state.phase == Phase.AWAITING.value, f"retry {attempt} did not fire"

    # Third occurrence: the recovery path is exhausted.
    orch.state.phase = Phase.AWAITING.value
    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert orch.state.park_task_id == "t1"
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.recurrences) == ("task_fatal", "t1", 3)
    assert transcript_types(config).count("autonomous_recovery") == 2


def test_git_unavailable_in_ready_retries_the_context_build_then_steps_aside(tmp_path):
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True, max_recovery_attempts=1)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("git unavailable", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="git_unavailable_in_ready")
    assert orch.state.phase == Phase.READY.value

    orch._to_needs_user("git unavailable", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="git_unavailable_in_ready")
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")


def test_a_lower_config_ceiling_restrains_the_table(tmp_path):
    """`max_recovery_attempts` is a ceiling, never a floor: at 0 the set-aside
    behaviour stays and no retry happens at all."""
    orch, _, _, _, _ = build(tmp_path, enabled=True, max_recovery_attempts=0)
    orch.state.phase = Phase.AWAITING.value
    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"


def test_a_higher_config_ceiling_cannot_widen_a_codes_own_allowance(tmp_path):
    """The other direction of the same rule. `submission_ambiguous` allows ONE
    re-issue; a config that asks for ten still gets one."""
    orch, _, _, _, _ = build(tmp_path, enabled=True, max_recovery_attempts=10)
    orch.state.pending_request = PendingRequest(request_id="r1", payload="p",
                                                send_attempted=True)
    orch.state.phase = Phase.SUBMISSION_UNCONFIRMED.value
    orch._to_needs_user("ambiguous", resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
                        kind="loop_fatal", code="submission_ambiguous")
    assert orch.state.phase == Phase.SUBMITTING.value

    orch.state.pending_request.send_attempted = True
    orch.state.phase = Phase.SUBMISSION_UNCONFIRMED.value
    orch._to_needs_user("ambiguous", resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
                        kind="loop_fatal", code="submission_ambiguous")
    assert orch.state.phase == Phase.NEEDS_USER.value


# =============================================================================
# 4. submission_ambiguous RE-ISSUES rather than parking
# =============================================================================


def test_submission_ambiguous_reissues_the_same_request_id(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.pending_request = PendingRequest(
        request_id="req-42", payload="the packet", send_attempted=True
    )
    orch.state.phase = Phase.SUBMISSION_UNCONFIRMED.value

    orch._to_needs_user("ambiguous", resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
                        kind="loop_fatal", code="submission_ambiguous")

    assert orch.state.phase == Phase.SUBMITTING.value
    assert orch.state.pending_request.send_attempted is False
    # The SAME id — if the earlier attempt did land, reconciliation detects it
    # and nothing is duplicated. A new id would guarantee the duplicate.
    assert orch.state.pending_request.request_id == "req-42"
    assert orch.state.pending_request.payload == "the packet"
    assert "autonomous_recovery" in transcript_types(config)


def test_a_reissue_with_no_request_in_flight_sets_aside_instead(tmp_path):
    """Fail-closed: nothing to re-issue is not a licence to invent one."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    orch.state.pending_request = None
    orch.state.phase = Phase.SUBMISSION_UNCONFIRMED.value

    orch._to_needs_user("ambiguous", resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
                        kind="loop_fatal", code="submission_ambiguous")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")


def test_the_budget_cannot_be_refreshed_by_the_fault_moving_one_phase_along(tmp_path):
    """`submission_ambiguous` is raised from `submitting` AND from
    `submission_unconfirmed`. `BlockerStore.record` keys on phase — correctly,
    for an operator question — so the budget is metered on `open_recurrences`,
    which does not, or the second phase would buy a second allowance."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.pending_request = PendingRequest(request_id="r1", payload="p",
                                                send_attempted=True)

    orch.state.phase = Phase.SUBMITTING.value
    orch._to_needs_user("ambiguous", resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
                        kind="loop_fatal", code="submission_ambiguous")
    assert orch.state.phase == Phase.SUBMITTING.value  # the one re-issue

    orch.state.pending_request.send_attempted = True
    orch.state.phase = Phase.SUBMISSION_UNCONFIRMED.value
    orch._to_needs_user("ambiguous", resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
                        kind="loop_fatal", code="submission_ambiguous")

    assert orch.state.phase == Phase.NEEDS_USER.value, "a phase change bought a second re-issue"
    assert blocker_store.open_recurrences("t1", "submission_ambiguous") == 2
    assert len(blocker_store.open_blockers()) == 2  # two records, one budget


# =============================================================================
# 5. stage 2 — set aside, and the loop continues
# =============================================================================


@pytest.mark.parametrize("code", ["worker_environment_drift", "publisher_url_drift",
                                  "crash_reconciliation_ambiguous"])
def test_a_code_with_no_recovery_path_is_set_aside_on_its_first_occurrence(tmp_path, code):
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user(f"task t1: {code}", kind="loop_fatal", code=code, task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert "autonomous_recovery" not in transcript_types(config)
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.recurrences) == ("task_fatal", "t1", 1)


def test_the_set_aside_park_makes_continuous_mode_continue(tmp_path, capsys):
    """The half that matters: `cli._handle_parked_task` — unmodified by
    halt-02 — quarantines the one task and tells the caller to carry on."""
    orch, config, _, task_store, registry = build(tmp_path, enabled=True,
                                                  tasks=("t1", "t2"))
    orch.state.phase = Phase.EXECUTING.value
    orch._to_needs_user("task t1: the git environment changed", kind="loop_fatal",
                        code="worker_environment_drift", task_id="t1")

    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "task_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert reloaded.state_of("t2") is TaskState.READY  # the loop still has work
    assert "continuous mode continues" in capsys.readouterr().out


def test_the_same_park_without_autonomy_stops_the_loop(tmp_path, capsys):
    """The contrast that makes the test above mean something."""
    orch, config, _, task_store, registry = build(tmp_path, enabled=False,
                                                  tasks=("t1", "t2"))
    orch.state.phase = Phase.EXECUTING.value
    orch._to_needs_user("task t1: the git environment changed", kind="loop_fatal",
                        code="worker_environment_drift", task_id="t1")

    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "loop_fatal"
    assert TaskStore(config.tasks_file).load().state_of("t1") is TaskState.READY
    capsys.readouterr()


def test_a_fault_with_no_task_behind_it_parks_exactly_as_today(tmp_path):
    """There is nothing to set aside, so the loop does not invent a victim —
    and because the second stage is unavailable, the first does not run either.
    Retrying toward a park that would still stop the loop only spends rounds."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.AWAITING.value

    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    assert "autonomous_recovery" not in transcript_types(config)


# =============================================================================
# 5b. the set-aside quarantines the ROUND IN FLIGHT, never a bystander
#     (setaside-01, 2026-08-26)
# =============================================================================
#
# The rationale that made an explicit `task_id` exist is REAL and is preserved:
# a push approval carries `binding.task_id`, the identity captured when the
# reviewed packet was sent, and an approval naming an older packet genuinely
# resolves a binding for a task that is not the one `state.task_execution`
# describes. What the site's id must not do is DECIDE the quarantine — it is
# validated against the round in flight, and a mismatch keeps the site's own
# terminal instead of setting a bystander aside.


def test_a_named_task_that_is_not_the_round_in_flight_is_not_quarantined(tmp_path):
    """THE claim of setaside-01. t1's round is in flight; the park names t2.

    t2 is an eligible registry task that has done nothing wrong, so it must not
    be set aside — and the ORIGINAL loop-fatal terminal the site chose must
    survive, rather than being converted into a `task_fatal` about someone
    else. `publisher_url_drift` is the live site with this shape: it is
    `RECOVER_UNAVAILABLE`, so it reaches the set-aside on its first occurrence,
    and it names `binding.task_id` rather than the executing task."""
    orch, config, blocker_store, _, _ = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1"
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("push refused", kind="loop_fatal",
                        code="publisher_url_drift", task_id="t2")

    # The bystander is not quarantined, and the terminal is the site's own.
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id == "t2", (
        "the park still REPORTS the task the site named — it simply does not "
        "set it aside"
    )
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id) == ("loop_fatal", "t2")
    # The refusal left evidence rather than firing invisibly.
    refusals = transcript_entries(config, "autonomous_set_aside_refused")
    assert refusals and refusals[0]["named_task_id"] == "t2"
    assert refusals[0]["round_task_ids"] == ["t1"]
    # And no retry was spent walking toward a park that still stops the loop.
    assert "autonomous_recovery" not in transcript_types(config)


def test_the_bystander_stays_dispatchable_and_the_loop_stops(tmp_path, capsys):
    """The half that matters, driven through the same `cli._handle_parked_task`
    the set-aside relies on: t2 keeps its registry state, and continuous mode
    reads `loop_fatal` and stops rather than churning past a fault it never
    quarantined anything for."""
    orch, config, _, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1"
    )
    orch.state.phase = Phase.EXECUTING.value
    orch._to_needs_user("push refused", kind="loop_fatal",
                        code="publisher_url_drift", task_id="t2")

    verdict = cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )

    assert verdict == "loop_fatal"
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t2") is TaskState.READY, "a bystander was blocked"
    assert reloaded.state_of("t1") is TaskState.READY
    capsys.readouterr()


def test_a_denied_directive_naming_another_task_during_a_round_quarantines_neither(
    tmp_path,
):
    """The brief's own scenario, written against a code that can actually reach
    the helper.

    `policy_denial_budget_exhausted` — the example the finding names — routes
    through `_to_fault_stop` today and is not in `AUTONOMOUS_RECOVERIES`, so it
    cannot reach the set-aside at all. The SHAPE it describes is what matters
    and is reproduced here: a directive about t2 is refused while t1's round is
    in flight, the refusal exhausts its recovery path, and the loop must not
    answer that by blocking t2. `worker_environment_drift` is used because it
    is the other zero-budget code whose site argues `loop_fatal`."""
    orch, config, blocker_store, task_store, registry = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1"
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("the git environment changed", kind="loop_fatal",
                        code="worker_environment_drift", task_id="t2")

    assert orch.state.park_kind == "loop_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).kind == "loop_fatal"
    # NEITHER: t2 because it is a bystander, t1 because the site did not name
    # it and nothing here invents a victim.
    cli._handle_parked_task(
        config, StateStore(config.state_file), task_store, registry, orch.state
    )
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t2") is TaskState.READY
    assert reloaded.state_of("t1") is TaskState.READY


def test_a_named_task_that_IS_the_round_in_flight_is_still_set_aside(tmp_path):
    """The other direction, and the reason this is a match rather than a ban on
    explicit ids: every ordinary site names the task it is executing, and those
    keep quarantining exactly as halt-02 shipped them."""
    orch, _, blocker_store, _, _ = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1"
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("push refused", kind="loop_fatal",
                        code="publisher_url_drift", task_id="t1")

    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert blocker_store.load(orch.state.park_blocker_id).kind == "task_fatal"


def test_the_dispatched_task_counts_as_the_round_even_before_its_record_exists(
    tmp_path,
):
    """The fail-silent case the match could have introduced, pinned.

    `_dispatch_task_postcommit` writes `state.task_execution` only AFTER
    `_rebase_execution_if_stale` has had its chance to park
    `task_base_behind_head` — halt-03's largest measured automation — so a
    first dispatch reaches that park with no execution record in state at all
    and only `state.current_task` naming the task being cut. Matching against
    the execution record alone would have refused there in production while
    every test in this file passed, because these tests seed `task_execution`
    and the real site does not.

    DISCRIMINATION, because the obvious assertions do not provide any. Parking
    `task_fatal` naming t1 is what the OLD unconditional `return task_id` did
    too, so asserting only that would echo the fixture. What is asserted
    instead is the pair that only the working match can produce: NO
    `autonomous_set_aside_refused` entry, and a rebuild that got far enough to
    refuse on the missing collaborators rather than on
    `_rebuild_execution_record_at_head`'s FIRST check, `"the park named no
    task"` — which is exactly where a set-aside that returned `None` would have
    left it. The negative twin below is the other half of the pair."""
    orch, config, blocker_store, _, _ = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight=None, dispatched="t1"
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("its recorded base is behind the branch head",
                        kind="task_fatal", code="task_base_behind_head",
                        task_id="t1")

    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert blocker_store.load(orch.state.park_blocker_id).task_id == "t1"
    assert not transcript_entries(config, "autonomous_set_aside_refused"), (
        "the match refused a task the dispatched round names"
    )
    refusals = transcript_entries(config, "autonomous_rebuild_refused")
    assert refusals, "the rebuild was never reached"
    assert "execution store" in refusals[0]["reason"], (
        f"the rebuild refused at the wrong check: {refusals[0]['reason']!r} — "
        "'the park named no task' means the set-aside resolved nothing"
    )


def test_with_no_round_at_all_the_same_park_refuses_instead(tmp_path):
    """The negative twin of the test above, and the two together are what fail
    if `state.current_task` is ever dropped from `_round_task_ids()`.

    Identical park, identical code, identical named task — the ONLY difference
    is that no record of any kind names a round. Here the set-aside must
    refuse, the refusal must be recorded, and the rebuild must never be
    reached."""
    orch, config, blocker_store, _, _ = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight=None, dispatched=None
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("its recorded base is behind the branch head",
                        kind="task_fatal", code="task_base_behind_head",
                        task_id="t1")

    refusals = transcript_entries(config, "autonomous_set_aside_refused")
    assert refusals and refusals[0]["round_task_ids"] == []
    assert not transcript_entries(config, "autonomous_rebuild_refused"), (
        "the rebuild ran for a park with no round behind it"
    )
    # The site called this one `task_fatal` itself, so that is what it keeps —
    # the refusal preserves the SITE's terminal, it does not impose one.
    assert (orch.state.park_kind, orch.state.park_task_id) == ("task_fatal", "t1")
    assert blocker_store.load(orch.state.park_blocker_id).kind == "task_fatal"


def test_a_task_named_by_neither_record_is_refused_even_with_both_seeded(tmp_path):
    """Both records populated and neither naming t3: still a bystander."""
    orch, _, _, _, _ = build(
        tmp_path, enabled=True, tasks=("t1", "t2"), in_flight="t1", dispatched="t2"
    )
    orch._to_needs_user("push refused", kind="loop_fatal",
                        code="publisher_url_drift", task_id="t3")
    assert orch.state.park_kind == "loop_fatal"


@pytest.mark.parametrize("execution", [
    None, {}, {"task_id": ""}, {"task_id": "   "}, {"task_id": 7}, "not-a-dict", [],
])
def test_an_unusable_execution_record_never_licenses_a_quarantine(tmp_path, execution):
    """A hand-edited or half-written state file must read as "no round in
    flight" — never as agreement with whatever the site named. Every one of
    these shapes leaves the site's own loop-fatal terminal in place."""
    orch, _, _, _, _ = build(tmp_path, enabled=True, tasks=("t1", "t2"), in_flight=None)
    orch.state.task_execution = execution

    orch._to_needs_user("push refused", kind="loop_fatal",
                        code="publisher_url_drift", task_id="t2")

    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id == "t2"


def test_the_id_that_is_quarantined_is_read_off_the_round_not_off_the_caller(tmp_path):
    """One source of truth for the returned value. A site whose id differs only
    by surrounding whitespace still matches, and what reaches `park_task_id`
    and the blocker record is the round's own, normalised id — a
    caller-supplied string never passes through."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight="t1")

    orch._to_needs_user("push refused", kind="loop_fatal",
                        code="publisher_url_drift", task_id="  t1  ")

    assert orch.state.park_kind == "task_fatal"
    assert orch.state.park_task_id == "t1"
    assert blocker_store.load(orch.state.park_blocker_id).task_id == "t1"


# =============================================================================
# 6. the hard halts stay hard
# =============================================================================


@pytest.mark.parametrize("code", sorted(HARD_HALT_CODES))
@pytest.mark.parametrize("site_kind", ["loop_fatal", "task_fatal"])
def test_a_hard_halt_is_never_automated_and_keeps_its_own_classification(
    tmp_path, code, site_kind
):
    """BOTH kinds, because the five hard halts do not share one today —
    `approved_path_symlink_traversal` parks `task_fatal` at its own site while
    `checkout_escape_detected` parks `loop_fatal` — so the property under test
    is NOT "it halts the loop". It is "autonomous mode does not touch it": the
    park is the one the site asked for, whichever that is, and no retry fires.
    Asserting `loop_fatal` for all five would have been a claim about the sites
    rather than about this change, and a false one."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user(f"task t1: {code}", resume_phase=Phase.EXECUTING.value,
                        kind=site_kind, code=code, task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind, f"{code} was re-classified by autonomy"
    assert blocker_store.load(orch.state.park_blocker_id).kind == site_kind
    assert orch.state.resume_phase == Phase.EXECUTING.value
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_hard_halt_is_not_reclassified_by_repetition(tmp_path):
    """No number of recurrences turns a hard halt into a set-aside — the
    refusal is on the code, not on a counter."""
    orch, _, _, _, _ = build(tmp_path, enabled=True)
    for _ in range(5):
        orch.state.phase = Phase.EXECUTING.value
        orch._to_needs_user("escape", resume_phase=Phase.EXECUTING.value,
                            kind="loop_fatal", code="checkout_escape_detected",
                            task_id="t1")
        assert orch.state.park_kind == "loop_fatal"


# =============================================================================
# 7. fail-closed edges
# =============================================================================


def test_without_a_blocker_store_nothing_is_automated(tmp_path):
    """No durable record means no budget to count and — worse — no durable
    question: the set-aside deletes the session file on the strength of that
    record existing. So the loop parks exactly as it always did."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, with_store=False)
    orch.state.phase = Phase.AWAITING.value

    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_blocker_id is None
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_missing_or_terminal_resume_phase_falls_through_to_the_set_aside(tmp_path):
    """A park with no resumable phase has said there is no path to re-enter;
    a terminal one would re-enter the park it just left. Neither is a retry."""
    # A fresh state directory per case (indexed, never named from the value —
    # a `repr` in a path is how a test starts depending on the filesystem's
    # opinion of quotes and braces), so each starts with an empty blocker
    # store and therefore a full, unspent budget.
    for index, resume in enumerate((None, Phase.NEEDS_USER.value, "not-a-phase", "")):
        orch, _, _, _, _ = build(tmp_path / f"case-{index}", enabled=True)
        orch.state.phase = Phase.AWAITING.value
        orch._to_needs_user("logged out", resume_phase=resume,
                            kind="loop_fatal", code="login_expired")
        assert orch.state.phase == Phase.NEEDS_USER.value, f"resume_phase={resume!r} retried"
        assert orch.state.park_kind == "task_fatal"


def test_a_corrupt_blocker_record_raises_rather_than_licensing_a_retry(tmp_path):
    """The fail-open this must not have: a store that cannot be read must never
    answer "nothing open", which would read as a full, unspent budget."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    blocker_store.directory.mkdir(parents=True, exist_ok=True)
    (blocker_store.directory / "blk-t1-001.json").write_text("{ not json", encoding="utf-8")
    orch.state.phase = Phase.AWAITING.value

    with pytest.raises(StateCorruptError):
        orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                            kind="loop_fatal", code="login_expired")
    assert "autonomous_recovery" not in transcript_types(config)


def test_a_non_boolean_enabled_is_not_read_as_consent(tmp_path):
    """A hand-built config is validated by nothing, so the orchestrator's own
    gate is `is not True` rather than a truthiness test."""
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
    orch.state.phase = Phase.AWAITING.value
    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"


def test_a_task_execution_without_a_usable_task_id_is_no_task_at_all(tmp_path):
    shapes = ({}, {"task_id": ""}, {"task_id": "   "}, {"task_id": 7}, "not-a-dict")
    for index, execution in enumerate(shapes):
        orch, _, _, _, _ = build(tmp_path / f"shape-{index}", enabled=True,
                                 in_flight=None)
        orch.state.task_execution = execution
        orch.state.phase = Phase.AWAITING.value
        orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                            kind="loop_fatal", code="login_expired")
        assert orch.state.park_kind == "loop_fatal", f"{execution!r} named a victim"


# =============================================================================
# 8. the record is closed only by evidence
# =============================================================================


def test_a_completed_step_closes_the_record_the_retry_was_riding_on(tmp_path):
    """A retry followed by a completed step is the only free, honest evidence
    the fault is behind the loop — the same evidence the rate-limit reset uses,
    read a second time. Driven through `run()`, so the wiring is tested and not
    just the helper."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.AWAITING.value
    calls = []

    def fake_step(phase):
        calls.append(phase)
        if len(calls) == 1:
            raise LoginExpiredError("session expired")
        orch.state.phase = Phase.STOPPED.value

    orch._step = fake_step
    # `max_steps` is a harness guard, not part of the property: the sequence
    # above needs two steps, so a run that reaches five has looped and this
    # test must FAIL rather than hang the suite it is in.
    assert orch.run(max_steps=5) == Phase.STOPPED.value

    assert [p.value for p in calls] == [Phase.AWAITING.value, Phase.AWAITING.value]
    closed = blocker_store.all_blockers()
    assert len(closed) == 1
    assert closed[0].resolved_at is not None
    assert closed[0].answer is None, "a machine close must never forge an operator answer"
    assert "autonomous recovery" in closed[0].archived_reason
    assert blocker_store.open_blockers() == []


def test_a_retry_that_parks_leaves_its_record_open(tmp_path):
    """The other direction: the marker is dropped by a park, never acted on.
    A record closed here would say a fault recovered that did not."""
    orch, _, blocker_store, _, _ = build(tmp_path, enabled=True, max_recovery_attempts=1)
    orch.state.phase = Phase.AWAITING.value
    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")
    assert orch._autonomous_recovered_blocker

    orch.state.phase = Phase.AWAITING.value
    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch._autonomous_recovered_blocker == ""
    assert len(blocker_store.open_blockers()) == 1
    # And a later completed step must not close it retroactively.
    orch._close_recovered_blocker()
    assert len(blocker_store.open_blockers()) == 1


def test_closing_a_record_never_raises_out_of_the_step_loop(tmp_path):
    """A store that cannot be written leaves the record open — fewer retries,
    never more — and must not take the run down with it."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch._autonomous_recovered_blocker = "blk-t1-999"  # no such record

    orch._close_recovered_blocker()  # must not raise

    assert orch._autonomous_recovered_blocker == ""
    assert "autonomous_recovery_close_failed" in transcript_types(config)


def test_close_recovered_refuses_an_empty_reason_and_a_closed_record(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    blocker = store.record(task_id="t1", kind="task_fatal", code="login_expired",
                           question="q", detail="", phase="awaiting",
                           now="2026-08-25T00:00:00+00:00")
    with pytest.raises(StateError):
        store.close_recovered(blocker.id, "   ")
    store.close_recovered(blocker.id, "recovered")
    with pytest.raises(StateError):
        store.close_recovered(blocker.id, "recovered again")
    reloaded = store.load(blocker.id)
    assert reloaded.answer is None and reloaded.archived_reason == "recovered"


def test_open_recurrences_counts_only_open_records_for_that_task_and_code(tmp_path):
    store = BlockerStore(tmp_path / "blockers")
    assert store.open_recurrences("t1", "login_expired") == 0
    for phase in ("awaiting", "submitting"):
        store.record(task_id="t1", kind="task_fatal", code="login_expired", question="q",
                     detail="", phase=phase, now="2026-08-25T00:00:00+00:00")
    store.record(task_id="t2", kind="task_fatal", code="login_expired", question="q",
                 detail="", phase="awaiting", now="2026-08-25T00:00:00+00:00")
    store.record(task_id="t1", kind="task_fatal", code="git_unavailable_in_ready",
                 question="q", detail="", phase="ready", now="2026-08-25T00:00:00+00:00")
    assert store.open_recurrences("t1", "login_expired") == 2

    resolved = store.find_open("t1", "login_expired", "awaiting")
    store.close_recovered(resolved.id, "recovered")
    assert store.open_recurrences("t1", "login_expired") == 1


# =============================================================================
# 9. the config section
# =============================================================================


def _config_text(tmp_path, autonomy_body: str) -> str:
    return (
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n\n'
        f"[autonomy]\n{autonomy_body}"
    )


def test_the_section_loads_and_is_honoured(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        _config_text(tmp_path, "enabled = true\nmax_recovery_attempts = 1\n"),
        encoding="utf-8",
    )
    autonomy = load_config(path).autonomy
    assert autonomy.enabled is True and autonomy.max_recovery_attempts == 1


@pytest.mark.parametrize("body,fragment", [
    ('enabled = "true"\n', "must be a boolean"),
    ("enabled = 1\n", "must be a boolean"),
    ("max_recovery_attempts = -1\n", "non-negative integer"),
    ('max_recovery_attempts = "2"\n', "non-negative integer"),
    ("max_recovery_attempts = true\n", "non-negative integer"),
    ("enabld = true\n", "unknown keys in [autonomy]"),
])
def test_a_malformed_section_is_refused_rather_than_coerced(tmp_path, body, fragment):
    """The direction that matters: a typo must never read as consent to run
    without an operator. Refusing at load time is how `enabled = "no"` — a
    truthy string — fails loudly instead of switching autonomy on."""
    path = tmp_path / "config.toml"
    path.write_text(_config_text(tmp_path, body), encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert fragment in str(exc.value)


def test_the_example_config_ships_the_section_switched_off(tmp_path):
    """The template is copied once and never re-read, so a template that ships
    it on would enable autonomous recovery for every new deployment."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    text = example.read_text(encoding="utf-8")
    assert "[autonomy]" in text
    assert "enabled = false" in text
    for code in sorted(HARD_HALT_CODES):
        assert code in text, f"the template does not name the hard halt {code}"
