"""Work that shipped under ANOTHER task's commits, recorded with evidence.

The claim under test: a task whose work is verifiably in the base under another
task's commits can be recorded as such — satisfying its dependents, skipped by
the scheduler, and NOT enumerated by the merge sweep as a task owing a branch —
and a task marked completed whose work is provably absent is visible as a
disagreement rather than reported as done.

MEASURED 2026-08-22, eight records disagreeing with the code in both directions:

  shipped but not recorded   auto-11, inbox-01, inbox-08, inbox-03, inbox-04
  recorded but not shipped   bind-01, split-01, dash-17

The two halves are expressed DIFFERENTLY on purpose, and
`test_all_eight_measured_records_can_be_expressed` is where that asymmetry is
pinned: the first five become evidence-backed `shipped_elsewhere` records, the
last three stay `completed` and surface as disagreements. Converting the second
group would launder a wrong record into a differently-wrong one instead of
showing it, which is the failure the whole task exists to end.

Real git wherever ancestry is the question — a mocked `merge-base` cannot show
what happens to a record when the base moves under it, which is the only reason
the evidence is re-checked rather than trusted once. The small `run_git`
helpers are duplicated rather than imported, matching this package's convention
(see `test_postcommit_primitives.py`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from autoloop import cli, dashboard, merge_sweep
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.errors import StateCorruptError, TaskGraphError
from autoloop.inbox import (
    KIND_SHIPPED_ELSEWHERE,
    InboxError,
    TaskInbox,
    apply_requests,
    check_request_shape,
    inbox_dir_for,
)
from autoloop.policy import PolicyConfig
from autoloop.tasks import (
    SATISFIES_DEPENDENCY,
    Task,
    TaskRegistry,
    TaskState,
    TaskStore,
)

URL = "https://chatgpt.com/c/ship-01"

#: Two full, well-formed shas that resolve to nothing. Used wherever the test
#: is about the RECORD rather than about git — the registry never asks a
#: repository anything, by design, so a fabricated sha exercises exactly the
#: same code a real one would.
SHA_A = "a" * 40
SHA_B = "b" * 40


# --- helpers ------------------------------------------------------------------


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path, name="repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    return repo


def commit(repo: Path, subject: str) -> str:
    (repo / "log.txt").write_text(subject + "\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", subject)
    return run_git(repo, "rev-parse", "HEAD").strip()


def head_of(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def task(tid, **kw):
    kw.setdefault("approved_paths", ("docs/A.md",))
    return Task(id=tid, title=f"Title {tid}", description="d", **kw)


def recorded_registry(*, commits=(SHA_A,), note="shipped under inbox-02's commits"):
    """A two-task graph where `dep` is recorded shipped-elsewhere and `later`
    depends on it — the inbox-02 / inbox-03 shape from the measurement."""
    registry = TaskRegistry([task("dep"), task("later", depends_on=("dep",))])
    registry.record_shipped_elsewhere("dep", list(commits), note)
    return registry


def roadmap_row(tid, status="pending", **kw):
    """A row shaped like `collect()`'s tolerant roadmap read — what the report
    functions take. Deliberately WITHOUT `description`: those functions never
    build a registry."""
    row = {"id": tid, "title": f"Title {tid}", "status": status,
           "shipped_commits": [], "shipped_note": "", "shipped_at": ""}
    row.update(kw)
    return row


def group_row(tid, **kw):
    """A `tasks.json` row carrying every field the REGISTRY needs, so
    `task_groups` builds one instead of degrading to `[]`."""
    row = {"id": tid, "title": tid.upper(), "description": "d", "status": "pending",
           "priority": 100, "depends_on": []}
    row.update(kw)
    return row


@pytest.fixture
def config(tmp_path):
    # `workers_root` OUTSIDE the state dir, as production requires: the inbox
    # derives from its parent, so a test that put it inside the checkout would
    # be testing a layout the loop refuses to run in.
    #
    # `auto_merge_enabled=True` because the flag check is the FIRST thing
    # `BacklogSweeper.sweep` does and it short-circuits to `DISABLED` — which
    # would make every sweep assertion below pass or fail for a reason that has
    # nothing to do with this task. `PolicyConfig()` defaults it off
    # (`test_auto_merge.py` pins that), so the sweep tests have to turn it on to
    # reach the enumeration they are actually about.
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "outside" / "workers",
    )


@pytest.fixture
def wired(config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """`is_ancestor` memoizes verdicts at module level and the commit-subject
    search is cached per repo. Two repositories built in the same wall-clock
    second with identical content share shas, so a verdict from an earlier test
    must not answer for a later one."""
    dashboard._ANCESTRY_CACHE.clear()
    dashboard._SHALLOW_CACHE.clear()
    dashboard._SUBJECT_CACHE.clear()
    yield
    dashboard._ANCESTRY_CACHE.clear()
    dashboard._SHALLOW_CACHE.clear()
    dashboard._SUBJECT_CACHE.clear()


# --- 1. dependents are satisfied ----------------------------------------------
#
# THE difference from `retire`, and the main reason the state was needed:
# inbox-03 and inbox-04 both depend on inbox-02, and a retired dependency is
# never satisfied by anything.


def test_a_shipped_elsewhere_record_satisfies_a_dependent_and_makes_it_ready():
    registry = recorded_registry()

    assert registry.state_of("dep") is TaskState.SHIPPED_ELSEWHERE
    assert registry.state_of("later") is TaskState.READY
    assert [t.id for t in registry.ready_tasks()] == ["later"]
    assert registry.next_ready().id == "later"


def test_a_retired_dependency_still_strands_which_is_why_this_state_exists():
    """The contrast that makes the change load-bearing rather than cosmetic.
    Retiring the dependency is refused precisely because it would strand the
    dependent — and if it were forced through, nothing would satisfy it."""
    registry = TaskRegistry([task("dep"), task("later", depends_on=("dep",))])

    with pytest.raises(TaskGraphError) as exc:
        registry.retire("dep")

    assert exc.value.code == "task_would_strand_dependents"
    assert "later" in str(exc.value)
    assert "retired" not in SATISFIES_DEPENDENCY


def test_the_scheduler_never_picks_a_shipped_elsewhere_task():
    """It is a record, not queue. `ready_tasks` is derived from `state_of`, so
    excluding it is structural rather than a filter that could be forgotten."""
    registry = recorded_registry()

    assert "dep" not in [t.id for t in registry.ready_tasks()]
    with pytest.raises(TaskGraphError) as exc:
        registry.mark_in_progress("dep")
    assert exc.value.code == "task_shipped_elsewhere"


def test_it_is_never_reported_as_a_stranded_dependent():
    """A record cannot be stranded: nothing is waiting to dispatch it. Leave it
    out of `_TERMINAL_STATUSES` and `retire` starts refusing valid retirements
    on behalf of a task that is already done."""
    registry = TaskRegistry([task("old"), task("dep", depends_on=("old",))])
    registry.record_shipped_elsewhere("dep", [SHA_A], "landed under x-01")

    report = registry.stranded_dependents("old")

    assert report.direct == () and report.transitive == ()
    assert registry.retire("old", reason="stale").status == "retired"


def test_the_summary_counts_it_separately_from_completed():
    """Folded into `completed` a reviewer would expect the merge sweep to have
    something to integrate for it. It has nothing — it never had a branch."""
    registry = recorded_registry()

    summary = registry.summary()

    assert "1 shipped elsewhere" in summary
    assert "0 completed" in summary


# --- 2. the record carries evidence, not an assertion -------------------------


def test_a_record_with_no_commits_is_refused():
    """"This shipped somewhere" naming nothing is exactly the operator
    assertion the state replaces. A claim that cannot be re-checked is not
    evidence, and refusing to write it is the only way to keep it from
    becoming one."""
    registry = TaskRegistry([task("t")])

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [], "trust me")

    assert exc.value.code == "bad_shipped_commits"
    assert registry.get("t").status == "pending"


@pytest.mark.parametrize(
    "bad",
    [
        ["a" * 7],                    # an abbreviation names a different object elsewhere
        ["A" * 40],                   # uppercase is not what git prints or what we parse
        ["z" * 40],                   # not hex
        ["a" * 39],                   # one short
        [None],
        [42],
    ],
)
def test_a_malformed_commit_sha_is_refused(bad):
    registry = TaskRegistry([task("t")])

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", bad, "note")

    assert exc.value.code == "bad_shipped_commits"


def test_a_bare_string_is_refused_rather_than_split_one_commit_per_character():
    """The `superseded_by` trap, on the field that unblocks this task's
    dependents. Iterating a string yields characters, so without the shape arm
    a single sha would be stored as forty "commits"."""
    registry = TaskRegistry([task("t")])

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", SHA_A, "note")

    assert exc.value.code == "bad_shipped_commits"
    assert registry.get("t").shipped_commits == ()


def test_the_same_commit_twice_is_refused():
    registry = TaskRegistry([task("t")])

    with pytest.raises(TaskGraphError):
        registry.record_shipped_elsewhere("t", [SHA_A, SHA_A], "note")


@pytest.mark.parametrize("note", ["", "   ", None, 7])
def test_a_blank_or_non_string_note_is_refused(note):
    """The commits say WHICH; the note says WHOSE. A record with shas and no
    account of them is a puzzle rather than a record."""
    registry = TaskRegistry([task("t")])

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [SHA_A], note)

    assert exc.value.code == "empty_task_field"
    assert registry.get("t").status == "pending"


def test_a_stored_bare_string_fails_closed_rather_than_loading_as_forty_commits():
    """`from_dict` bypasses `add_many`, so `_persisted_shipped_commits` is the
    ONLY gate a hand-edited row passes. Reading a malformed list as "no
    evidence" would delete the claim's support while leaving the claim."""
    stored = {
        "tasks": [{
            "id": "t", "title": "T", "description": "d",
            "status": "shipped_elsewhere", "shipped_commits": SHA_A,
        }]
    }

    with pytest.raises(StateCorruptError) as exc:
        TaskRegistry.from_dict(stored)

    assert "shipped_commits" in str(exc.value)


def test_a_row_written_before_the_field_existed_still_loads():
    """Backward compatibility, the same pattern every other added field has: a
    MISSING key is not malformed, and a hand-edited `null` becomes `""` rather
    than `None` for the two text fields."""
    stored = {
        "tasks": [{
            "id": "t", "title": "T", "description": "d", "status": "pending",
            "shipped_note": None, "shipped_at": None,
        }]
    }

    loaded = TaskRegistry.from_dict(stored).get("t")

    assert loaded.shipped_commits == ()
    assert loaded.shipped_note == "" and loaded.shipped_at == ""


def test_the_record_round_trips_through_the_task_store(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    store.save(recorded_registry(commits=(SHA_A, SHA_B), note="under inbox-02"))

    reread = store.load().get("dep")

    assert reread.status == "shipped_elsewhere"
    assert reread.shipped_commits == (SHA_A, SHA_B)
    assert reread.shipped_note == "under inbox-02"
    assert reread.shipped_at


# --- 3. re-recording, and the states it refuses -------------------------------


def test_re_recording_identical_evidence_is_a_no_op_that_keeps_the_timestamp():
    """A resubmitted request must not make the record say it was written later
    than it was."""
    registry = recorded_registry()
    first = registry.get("dep").shipped_at

    registry.record_shipped_elsewhere("dep", [SHA_A], "shipped under inbox-02's commits")

    assert registry.get("dep").shipped_at == first


def test_new_evidence_replaces_the_old_because_commits_legitimately_move():
    """Deliberately unlike `retire`'s written-once rule: a retirement records a
    DECISION, this records an OBSERVATION about commits, and a rebase renames
    every sha. The re-check is what makes a wrong rewrite visible."""
    registry = recorded_registry()

    registry.record_shipped_elsewhere("dep", [SHA_B], "rebased; now under inbox-02b")

    assert registry.get("dep").shipped_commits == (SHA_B,)
    assert registry.get("dep").shipped_note == "rebased; now under inbox-02b"


def test_an_in_progress_task_is_refused_so_the_round_is_not_stranded():
    registry = TaskRegistry([task("t")])
    registry.mark_in_progress("t")

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [SHA_A], "note")

    assert exc.value.code == "task_in_progress"
    assert registry.get("t").status == "in_progress"


def test_a_completed_task_is_refused_so_a_disagreement_is_not_laundered():
    """bind-01 / split-01 / dash-17 are the case. Converting them would rewrite
    a wrong record into a differently-wrong one; they must stay visible."""
    registry = TaskRegistry([task("t")])
    registry.mark_completed("t")

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [SHA_A], "note")

    assert exc.value.code == "task_completed"
    assert "DISAGREEMENT" in str(exc.value)


def test_a_retired_task_is_refused_because_a_supersession_is_history():
    registry = TaskRegistry([task("t")])
    registry.retire("t", superseded_by=["u"], reason="superseded")

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [SHA_A], "note")

    assert exc.value.code == "task_retired"


def test_a_loop_quarantine_is_refused_so_its_blocker_is_not_orphaned():
    """A `task_fatal` park writes a `blockers.Blocker` record beside the
    registry row, and that record is read INDEPENDENTLY of the registry by
    `start`, `health.check` and the heartbeat. Moving the row terminal without
    closing it is the split brain `cli._reconcile_retired_blockers` exists for:
    the page would say "already done, elsewhere" while the loop stayed stopped
    waiting on exactly this task. Provenance is the stored field, never the
    reason text — the same gate `operator_unblock` reads."""
    registry = TaskRegistry([task("t")])
    registry.block("t", "task_fatal: validation failed")

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [SHA_A], "landed under auto-10")

    assert exc.value.code == "task_blocked_by_operator"
    assert "autoloop answer" in str(exc.value)
    assert registry.get("t").status == "blocked"


def test_a_crafted_hold_reason_cannot_launder_a_loop_quarantine():
    """`blocked_reason` is unconstrained free text the loop writes too, so a
    park detail that merely BEGINS with the operator prefix must not read as an
    operator hold. `block()` clears `hold_origin` unconditionally, and this
    reads only that field."""
    from autoloop.tasks import OPERATOR_HOLD_PREFIX

    registry = TaskRegistry([task("t")])
    registry.block("t", OPERATOR_HOLD_PREFIX + "looks like an operator wrote it")

    with pytest.raises(TaskGraphError) as exc:
        registry.record_shipped_elsewhere("t", [SHA_A], "landed under auto-10")

    assert exc.value.code == "task_blocked_by_operator"


def test_a_parked_task_converts_and_its_operator_hold_origin_is_cleared():
    """The five measured records were all parked `blocked_by_operator` as a
    stopgap. The park REASON is kept — it is the account of why — but the hold
    origin goes: there is no hold left to have one, and a marker left behind
    would still be deciding who may release a quarantine that no longer
    exists."""
    registry = TaskRegistry([task("t")])
    registry.operator_block("t", "parked pending ship-01")

    registry.record_shipped_elsewhere("t", [SHA_A], "landed under auto-10")

    stored = registry.get("t")
    assert stored.status == "shipped_elsewhere"
    assert stored.hold_origin == ""
    assert "parked pending ship-01" in stored.blocked_reason
    assert registry.unblock_obstacle("t").code == "task_shipped_elsewhere"


# --- 4. no other write path may overwrite the record --------------------------


def _recorded_one():
    registry = TaskRegistry([task("t")])
    registry.record_shipped_elsewhere("t", [SHA_A], "landed under x-01")
    return registry


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda r: r.mark_completed("t"), id="mark_completed"),
        pytest.param(lambda r: r.mark_in_progress("t"), id="mark_in_progress"),
        pytest.param(lambda r: r.block("t", "parked"), id="block"),
        pytest.param(lambda r: r.operator_block("t", "hold"), id="operator_block"),
        pytest.param(lambda r: r.retire("t"), id="retire"),
        pytest.param(lambda r: r.set_decomposition("t", "plan"), id="set_decomposition"),
        pytest.param(lambda r: r.set_description("t", "new"), id="set_description"),
        pytest.param(lambda r: r.set_approved_paths("t", ["a.py"]), id="set_approved_paths"),
        pytest.param(lambda r: r.set_depends_on("t", []), id="set_depends_on"),
        pytest.param(lambda r: r.request_urgent("t", "now"), id="request_urgent"),
        pytest.param(lambda r: r.release("t"), id="release"),
    ],
)
def test_no_mutator_can_silently_overwrite_the_record(call):
    """Every one of these ends in a bare status assignment or a field rewrite.
    Adding a status without an arm in each is how the record would be lost with
    its shas still sitting on the row, saying nothing.

    `mark_completed` is the sharpest: completing this task would put it into the
    merge sweep with no branch and no execution record — the `unresolved` →
    HELD shape the state exists to keep out of the sweep, arrived at by tidying.
    """
    registry = _recorded_one()

    with pytest.raises(TaskGraphError):
        call(registry)

    stored = registry.get("t")
    assert stored.status == "shipped_elsewhere"
    assert stored.shipped_commits == (SHA_A,)


def test_the_dispatch_refusal_names_the_commits_so_the_reader_can_check():
    registry = _recorded_one()

    with pytest.raises(TaskGraphError) as exc:
        registry.mark_in_progress("t")

    assert SHA_A[:12] in str(exc.value)


def test_an_urgent_pin_is_refused_rather_than_silently_accepted():
    """Without its own arm this state falls through `_refuse_unurgentable` to
    the `approved_paths` check and the pin is ACCEPTED — the silent no-op
    accept that method exists to prevent, on a task that can never dispatch."""
    registry = _recorded_one()

    with pytest.raises(TaskGraphError) as exc:
        registry.request_urgent("t", "now")

    assert exc.value.code == "task_shipped_elsewhere"
    assert registry.live_urgent_target() is None


# --- 5. the merge sweep never asks it for a branch ----------------------------


class _RefusingMerger:
    """Any call at all is the failure: a shipped-elsewhere task has no branch,
    so reaching the merger for one means the enumeration asked."""

    def attempt(self, task_id, seen=None):  # pragma: no cover - the assertion IS the test
        raise AssertionError(f"the sweep tried to merge {task_id!r}")


def _sweeper(config, registry, execution_store, git, merger=None):
    from autoloop.policy import PolicyEngine

    return merge_sweep.BacklogSweeper(
        config=config,
        git=git,
        policy=PolicyEngine(config.policy),
        execution_store=execution_store,
        registry=registry,
        log=lambda *a, **k: None,
        merger=merger or _RefusingMerger(),
    )


class _StubGit:
    """A git that answers HEAD, reports a clean tree, and says nothing is an
    ancestor — so every candidate the sweep DOES enumerate reads as unmerged.
    `read_commit` exists because a record with no `published_at` orders on the
    candidate's committer timestamp."""

    def __init__(self, head="d" * 40):
        self._head = head

    def head_sha(self):
        return self._head

    def dirty_files(self):
        return []

    def is_descendant(self, head, candidate):
        return False

    def read_commit(self, sha):
        return {"committer": "T <t@example.com> 1700000000 +0000"}


def test_the_sweep_does_not_enumerate_a_shipped_elsewhere_task(config):
    """It never had a branch, an execution record or a candidate. Enumerating
    it would mint an `unresolved` entry — a record naming no candidate — and
    HOLD every future invocation."""
    from autoloop.worktask import TaskExecutionStore

    config.state_dir.mkdir(parents=True, exist_ok=True)
    registry = recorded_registry()
    result = _sweeper(
        config, registry, TaskExecutionStore(config.executions_dir), _StubGit()
    ).sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.unresolved == []
    assert result.pending == []
    assert result.is_clear is True


def test_the_sweep_still_holds_for_a_genuinely_completed_unjudgeable_task(
    config, monkeypatch
):
    """The bound that must NOT be weakened. A completed task whose record names
    no candidate is still unresolved, and one unresolved task still makes the
    WHOLE invocation non-mutating — even though a shipped-elsewhere record sits
    beside it that the sweep correctly ignored. `_RefusingMerger` proves nothing
    was merged: the mergeable branch is left pending, untouched."""
    from autoloop.worktask import TaskExecution, TaskExecutionStore

    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = TaskExecutionStore(config.executions_dir)
    registry = recorded_registry()
    registry.add_many([task("unjudgeable"), task("mergeable")])
    registry.mark_completed("unjudgeable")
    registry.mark_completed("mergeable")
    store.save(TaskExecution(
        task_id="unjudgeable", task_branch="autoloop/unjudgeable", worktree_path="",
        task_base_sha="e" * 40, candidate_sha="", review_round=1,
    ))
    store.save(TaskExecution(
        task_id="mergeable", task_branch="autoloop/mergeable", worktree_path="",
        task_base_sha="e" * 40, candidate_sha="f" * 40, review_round=1,
        intended_remote="origin", intended_remote_ref="refs/heads/autoloop/mergeable",
    ))
    monkeypatch.setattr(cli, "_candidate_publication", lambda *a, **k: (True, ""))

    result = _sweeper(config, registry, store, _StubGit()).sweep()

    assert result.outcome == merge_sweep.HELD
    assert [tid for tid, _why in result.unresolved] == ["unjudgeable"]
    assert result.pending == ["mergeable"]
    assert result.is_clear is False
    assert "dep" not in [tid for tid, _why in result.unresolved]


def test_the_merge_window_exempts_a_shipped_elsewhere_task(wired):
    """A REGRESSION guard in the literal sense. The five records were parked
    `blocked`, i.e. exempt through BLOCKED_BY_OPERATOR; recording them MOVES
    them out of that arm, so without the new entry a leftover execution record
    would start holding the merge window shut on work already in the base."""
    from autoloop.worktask import TaskExecution, TaskExecutionStore

    registry = recorded_registry()
    TaskStore(wired.tasks_file).save(registry)
    TaskExecutionStore(wired.executions_dir).save(TaskExecution(
        task_id="dep", task_branch="autoloop/dep", worktree_path="",
        task_base_sha="f" * 40, candidate_sha="c" * 40, review_round=1,
    ))

    reasons, _notes = cli._merge_window_blockers(wired, set(), None)

    assert [r for r in reasons if "dep" in r] == []


# --- 6. the operator route is live-safe ---------------------------------------


def test_the_state_is_reachable_through_the_inbox_without_stopping_the_loop():
    """Writing under `.autoloop/` needs the loop stopped — the escape detector
    snapshots that directory — so the record goes through the inbox, which is
    the attested path for everything except `priority`."""
    registry = TaskRegistry([task("dep"), task("later", depends_on=("dep",))])

    added, applied, refused = apply_requests(registry, [{
        "kind": KIND_SHIPPED_ELSEWHERE,
        "id": "dep",
        KIND_SHIPPED_ELSEWHERE: {"commits": [SHA_A], "note": "under inbox-02"},
    }])

    assert (added, refused) == ([], [])
    assert applied and "shipped elsewhere" in applied[0]
    assert registry.state_of("dep") is TaskState.SHIPPED_ELSEWHERE
    assert registry.state_of("later") is TaskState.READY


def test_the_submit_path_never_touches_the_checkout_or_the_state_dir(tmp_path):
    inbox = TaskInbox(inbox_dir_for(None, tmp_path / "state"))

    path = inbox.submit({
        "kind": KIND_SHIPPED_ELSEWHERE, "id": "dep",
        KIND_SHIPPED_ELSEWHERE: {"commits": [SHA_A], "note": "under inbox-02"},
    })

    assert json.loads(path.read_text(encoding="utf-8"))["id"] == "dep"


@pytest.mark.parametrize(
    "payload, expected",
    [
        pytest.param("under inbox-02", "as an object", id="not-an-object"),
        pytest.param({"commits": [SHA_A]}, "['note']", id="missing-note"),
        pytest.param({"note": "n"}, "['commits']", id="missing-commits"),
        pytest.param(
            {"commits": [SHA_A], "note": "n", "why": "x"}, "carries only", id="extra-key"
        ),
        pytest.param({"commits": SHA_A, "note": "n"}, "as a list", id="commits-not-list"),
        pytest.param({"commits": [SHA_A], "note": 7}, "as a string", id="note-not-string"),
    ],
)
def test_a_malformed_payload_is_refused_at_both_gates(payload, expected):
    """Shape is checked on the way IN (`submit`) and on the way OUT
    (`apply_requests`) off the one function, because hand-writing the JSON file
    is a documented operator route and never passes through `submit`. A key the
    receiver ignores must be reported, not dropped — and here the dropped key
    would be half the evidence."""
    spec = {"kind": KIND_SHIPPED_ELSEWHERE, "id": "dep", KIND_SHIPPED_ELSEWHERE: payload}

    with pytest.raises(InboxError) as exc:
        check_request_shape(spec)
    assert expected in str(exc.value)

    registry = TaskRegistry([task("dep")])
    _added, applied, refused = apply_requests(registry, [spec])
    assert applied == [] and refused
    assert registry.get("dep").status == "pending"


def test_a_stray_field_beside_the_payload_is_refused():
    with pytest.raises(InboxError) as exc:
        check_request_shape({
            "kind": KIND_SHIPPED_ELSEWHERE, "id": "dep", "reason": "x",
            KIND_SHIPPED_ELSEWHERE: {"commits": [SHA_A], "note": "n"},
        })

    assert "carries only" in str(exc.value)


def test_a_registry_refusal_becomes_one_refused_line_and_never_stops_the_batch():
    """The inbox owns SHAPE, the registry owns CONTENT, and one operator typo
    must not discard the good requests queued behind it."""
    registry = TaskRegistry([task("a"), task("b")])

    added, applied, refused = apply_requests(registry, [
        {"kind": KIND_SHIPPED_ELSEWHERE, "id": "a",
         KIND_SHIPPED_ELSEWHERE: {"commits": ["nope"], "note": "n"}},
        {"kind": KIND_SHIPPED_ELSEWHERE, "id": "b",
         KIND_SHIPPED_ELSEWHERE: {"commits": [SHA_A], "note": "n"}},
    ])

    assert added == []
    assert len(refused) == 1 and refused[0].startswith("a: ")
    assert len(applied) == 1
    assert registry.get("a").status == "pending"
    assert registry.get("b").status == "shipped_elsewhere"


# --- 7. the evidence is re-checked, never trusted once ------------------------


def elsewhere_row(tid, commits, note="under x-01"):
    return {"id": tid, "title": f"Title {tid}", "shipped_commits": list(commits),
            "shipped_note": note, "shipped_at": "2026-08-23T00:00:00+00:00"}


def test_a_record_whose_commit_stops_being_an_ancestor_reads_as_a_disagreement(tmp_path):
    """The whole reason the shas are stored rather than a flag. The record was
    true when it was written; the base moved; it must stop reading as done."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, "work that never landed")
    run_git(repo, "checkout", "-q", "work")

    report = dashboard.shipped_report(
        repo, [roadmap_row("t", "shipped_elsewhere", shipped_commits=[side],
                           shipped_note="under x-01")], base,
    )

    row = report["elsewhere"][0]
    assert row["state"] == "invalidated"
    assert row["commits"][0]["ancestry"] == "not-in-base"
    kinds = [r["kind"] for r in report["disagreements"]["rows"]]
    assert kinds == ["shipped_evidence_absent"]
    assert report["disagreements"]["proven"] == 1


def test_a_verified_record_reads_as_verified_and_is_not_a_disagreement(tmp_path):
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "inbox-02: the mutation vocabulary")

    report = dashboard.shipped_report(
        repo, [roadmap_row("inbox-01", "shipped_elsewhere", shipped_commits=[carrier],
                           shipped_note="under inbox-02")], head_of(repo),
    )

    row = report["elsewhere"][0]
    assert row["state"] == "verified"
    assert row["commits"][0]["ancestry"] == "in-base"
    assert report["disagreements"]["rows"] == []
    assert report["disagreements"]["unverified"] == []


def test_one_stale_commit_among_several_invalidates_the_whole_record(tmp_path):
    """ALL, not ANY — the opposite of `shipped_states`, deliberately. There the
    commits are search results and one in the base proves the work landed; here
    they are the record's own claim, so a record must not survive by its
    stalest half."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    landed = commit(repo, "one that landed")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    stale = commit(repo, "one that did not")
    run_git(repo, "checkout", "-q", "work")

    rows = dashboard.shipped_elsewhere_states(
        [elsewhere_row("t", [landed, stale])],
        lambda sha: dashboard.is_ancestor(repo, sha, base),
    )

    assert rows[0]["state"] == "invalidated"
    assert [c["ancestry"] for c in rows[0]["commits"]] == ["in-base", "not-in-base"]


def test_an_indeterminate_ancestry_check_is_unverified_never_verified():
    """THE fail-open this section exists to close. A shallow clone, an object
    nobody fetched and an unreadable repository all answer `unknown`, and
    rounding that up to verified switches the alarm off exactly when it cannot
    see. Rounding it DOWN to a disagreement is the other failure: every shallow
    clone would report the roadmap as broken."""
    rows = dashboard.shipped_elsewhere_states(
        [elsewhere_row("t", [SHA_A])], lambda sha: "unknown"
    )

    assert rows[0]["state"] == "unverified"
    assert rows[0]["state"] != "verified"
    report = dashboard.registry_disagreements([], rows)
    assert report["rows"] == []
    assert [r["id"] for r in report["unverified"]] == ["t"]


def test_a_record_with_no_commits_at_all_is_an_unsupported_claim():
    """Only reachable by hand-editing `tasks.json` — the registry refuses an
    empty list — and it must not render as an ordinary record."""
    rows = dashboard.shipped_elsewhere_states(
        [elsewhere_row("t", [])], lambda sha: "yes"
    )

    assert rows[0]["state"] == "unsupported"
    kinds = [r["kind"] for r in dashboard.registry_disagreements([], rows)["rows"]]
    assert kinds == ["shipped_evidence_missing"]


def test_a_completed_task_whose_commits_are_not_ancestors_is_a_proven_disagreement(
    tmp_path,
):
    """The other direction: the registry says done and git says the naming
    commits are outside the base."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "bind-01: the work nobody merged")
    run_git(repo, "checkout", "-q", "work")

    report = dashboard.shipped_report(repo, [roadmap_row("bind-01", "completed")], base)

    finding = report["disagreements"]["rows"][0]
    assert finding["kind"] == "completed_not_in_base"
    assert finding["proven"] is True


def test_a_completed_task_no_commit_names_is_reported_but_never_as_proof(tmp_path):
    """`shipped-report` returned NO MENTION for bind-01, split-01 and dash-17.
    That is a disagreement worth a human's eye AND it is not proof: the work
    may have shipped under a subject that never named the id, and presenting it
    as proof would be a licence to undo work that landed."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "something else entirely")

    report = dashboard.shipped_report(
        repo, [roadmap_row("split-01", "completed")], head_of(repo)
    )

    finding = report["disagreements"]["rows"][0]
    assert finding["kind"] == "completed_unwitnessed"
    assert finding["proven"] is False
    assert report["disagreements"]["proven"] == 0


def test_proven_findings_sort_ahead_of_unproven_ones():
    shipped = [
        {"id": "u-1", "title": "", "state": "unknown", "detail": "no mention"},
        {"id": "p-1", "title": "", "state": "not-in-base", "detail": "not an ancestor"},
    ]

    rows = dashboard.registry_disagreements(shipped, [])["rows"]

    assert [r["id"] for r in rows] == ["p-1", "u-1"]


def test_nothing_is_auto_converted_by_looking(tmp_path):
    """Detecting a disagreement and reporting it is in scope; silently
    rewriting a task's status because a heuristic matched is not. The report is
    a pure read — it holds no registry and cannot write one."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, "not merged")
    run_git(repo, "checkout", "-q", "work")
    rows = [roadmap_row("t", "shipped_elsewhere", shipped_commits=[side])]

    dashboard.shipped_report(repo, rows, base)
    dashboard.disagreement_report(repo, rows, base)

    assert rows[0]["status"] == "shipped_elsewhere"
    assert rows[0]["shipped_commits"] == [side]


def test_the_report_says_when_the_search_did_not_run(tmp_path):
    """With `git log --all` unanswerable no COMPLETED task can be judged — but
    a shipped-elsewhere record names its own commits, so it is judged anyway.
    A report that went silent about those rows would hide the one kind that is
    still perfectly answerable."""
    empty = tmp_path / "not-a-repo"
    empty.mkdir()

    report = dashboard.disagreement_report(
        empty, [roadmap_row("t", "shipped_elsewhere", shipped_commits=[SHA_A])], SHA_B
    )

    assert report["searched"] is False
    assert report["elsewhere"][0]["state"] == "unverified"


# --- 8. the dashboard shows it, and names where the work landed ---------------


def test_the_dashboard_groups_it_separately_and_names_the_commits():
    """Its own group, naming where the work landed — the same way the Retired
    group names successors. Folding it into Retired would put "already done"
    under a heading that means "never will be"."""
    rows = [group_row(
        "inbox-01", status="shipped_elsewhere", shipped_commits=[SHA_A],
        shipped_note="shipped under inbox-02's commits",
    )]

    groups = dashboard.task_groups({"tasks": rows}, {})

    group = next(g for g in groups if g["key"] == "shipped_elsewhere")
    assert group["label"] == "Shipped elsewhere"
    assert group["count"] == 1
    assert group["collapsed"] is True
    row = group["tasks"][0]
    assert row["shipped_commits"] == [SHA_A]
    assert SHA_A[:12] in row["detail"]
    assert "inbox-02" in row["detail"]
    assert row["ordinal"] is None, "a record cannot be next to be dispatched"


def test_a_hand_edited_row_with_no_commits_says_so_in_the_group():
    """It must not render as an ordinary record just because it has a note."""
    rows = [group_row("t", status="shipped_elsewhere", shipped_commits=[],
                      shipped_note="trust me")]

    groups = dashboard.task_groups({"tasks": rows}, {})

    detail = next(g for g in groups if g["key"] == "shipped_elsewhere")["tasks"][0]["detail"]
    assert "NO commits" in detail


def test_a_dependent_is_not_shown_waiting_on_a_shipped_elsewhere_dependency():
    """`_waiting_on` used to re-derive the dependency rule as
    `is not COMPLETED`. With the copy left in place a task `state_of` calls
    READY would carry a "waits on dep" chip — one panel disagreeing with the
    next about the same row."""
    rows = [
        group_row("dep", status="shipped_elsewhere", shipped_commits=[SHA_A],
                  shipped_note="under x-01"),
        group_row("later", depends_on=["dep"]),
    ]

    groups = dashboard.task_groups({"tasks": rows}, {})

    ready = next(g for g in groups if g["key"] == "ready")
    blocked = next(g for g in groups if g["key"] == "blocked")
    assert [t["id"] for t in ready["tasks"]] == ["later"]
    assert blocked["tasks"] == []
    assert ready["tasks"][0]["waits_on"] == []


def test_the_dependency_graph_agrees_with_the_roadmap_panel():
    """`_dep_states` has its own raw-row derivation for the case the registry
    will not load. A third hand-written `!= "completed"` there would draw the
    dependent BLOCKED while the Roadmap panel shows it Ready."""
    rows = [
        group_row("dep", status="shipped_elsewhere", shipped_commits=[SHA_A]),
        group_row("later", depends_on=["dep"]),
    ]

    states, source = dashboard._dep_states(dashboard._dep_rows({"tasks": rows}), None)

    assert source == "status"
    assert states["dep"] == TaskState.SHIPPED_ELSEWHERE.value
    assert states["later"] == TaskState.READY.value


def test_the_stats_count_it_and_the_line_matches_the_registry_summary():
    """`roadmap_stats["line"]` is `TaskRegistry.summary()`'s sentence, state for
    state and word for word — the page must not make a second authority out of
    one fact."""
    rows = [
        group_row("a", status="shipped_elsewhere", shipped_commits=[SHA_A]),
        group_row("b", status="completed"),
    ]
    groups = dashboard.task_groups({"tasks": rows}, {})

    stats = dashboard.roadmap_stats(groups, {}, True, {})

    assert stats["counts"]["shipped_elsewhere"] == 1
    assert "1 shipped elsewhere" in stats["line"]
    # Both DONE states are in the numerator: counted in the denominator as
    # outstanding and never in the numerator as finished would push the
    # percentage down for every record of this kind.
    assert stats["percent_done"] == 100.0


def test_every_node_state_the_page_can_draw_still_has_an_icon():
    """`DEP_NODE_STATES` is derived from `GROUPS`, so the new state reaches the
    dependency panel automatically — this is what stops it reaching the page
    unlabelled."""
    script = dashboard.PAGE.split("<script>", 1)[1]
    marks = script.split("const DMARK = {", 1)[1].split("};", 1)[0]
    fills = script.split("const DFILL = {", 1)[1].split("};", 1)[0]
    order = script.split("const DORDER = [", 1)[1].split("];", 1)[0]

    assert "shipped_elsewhere" in dashboard.DEP_NODE_STATES
    assert "shipped_elsewhere:[" in marks
    assert "shipped_elsewhere:" in fills
    assert '"shipped_elsewhere"' in order


def test_the_page_renders_the_group_and_the_disagreements_box():
    assert 'id="shippedbox"' in dashboard.PAGE
    assert 'id="disagreebox"' in dashboard.PAGE
    script = dashboard.PAGE.split("<script>", 1)[1]
    assert 'byKey("shipped_elsewhere")' in script
    assert "d.disagreements" in script


# --- 9. the CLI verifies before it queues -------------------------------------


def _record_shipped(task_id, repo, commits, note="under x-01", base="HEAD"):
    return cli._cmd_record_shipped(argparse.Namespace(
        config=None, task_id=task_id, commit=list(commits), note=note,
        repo=repo, base=base,
    ))


def test_record_shipped_queues_only_evidence_it_verified(wired, tmp_path, capsys):
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    carrier = commit(repo, "inbox-02: the mutation vocabulary")
    TaskStore(wired.tasks_file).save(
        TaskRegistry([task("inbox-01"), task("inbox-03", depends_on=("inbox-01",))])
    )

    code = _record_shipped("inbox-01", repo, [carrier[:8]], "under inbox-02")

    assert code == 0
    queued = list((wired.workers_root.parent / "inbox").glob("*.json"))
    spec = json.loads(queued[0].read_text(encoding="utf-8"))
    # Stored as the FULL sha even though an abbreviation was typed: an
    # abbreviation names a different object in a different checkout, so the
    # record would not mean the same thing to the next reader.
    assert spec[KIND_SHIPPED_ELSEWHERE]["commits"] == [carrier]
    assert spec["kind"] == KIND_SHIPPED_ELSEWHERE and spec["id"] == "inbox-01"
    assert "re-checked on every read" in capsys.readouterr().out
    # The registry is NOT written here — the loop stays its only writer.
    assert TaskStore(wired.tasks_file).load().get("inbox-01").status == "pending"


def test_record_shipped_refuses_a_commit_that_is_not_an_ancestor(wired, tmp_path, capsys):
    """An operator asserting that work shipped under a commit the base does not
    contain is exactly the unsupported assertion this record replaces."""
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, "never landed")
    run_git(repo, "checkout", "-q", "work")
    TaskStore(wired.tasks_file).save(TaskRegistry([task("t")]))

    code = _record_shipped("t", repo, [side], base=base)

    assert code == 1
    assert "NOT an ancestor" in capsys.readouterr().out
    assert not list((wired.workers_root.parent / "inbox").glob("*.json"))


def test_record_shipped_refuses_when_git_cannot_answer(wired, tmp_path, capsys,
                                                       monkeypatch):
    """"Could not look" is not "verified". A command that queued on an
    indeterminate check would be a verification step that switches itself off
    precisely when it cannot see."""
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    carrier = commit(repo, "a real commit")
    TaskStore(wired.tasks_file).save(TaskRegistry([task("t")]))
    monkeypatch.setattr(dashboard, "is_ancestor", lambda *a, **k: "unknown")

    code = _record_shipped("t", repo, [carrier])

    assert code == 1
    assert "could not decide" in capsys.readouterr().out
    assert not list((wired.workers_root.parent / "inbox").glob("*.json"))


def test_record_shipped_refuses_an_unresolvable_revision(wired, tmp_path, capsys):
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    TaskStore(wired.tasks_file).save(TaskRegistry([task("t")]))

    code = _record_shipped("t", repo, ["no-such-rev"])

    assert code == 1
    assert "names no commit" in capsys.readouterr().out
    assert not list((wired.workers_root.parent / "inbox").glob("*.json"))


def test_record_shipped_dry_runs_the_registry_before_queueing(wired, tmp_path, capsys):
    """The refusal an operator reads is the registry's own wording, from the
    same method the drain calls — so they are never told two different things
    about the same request."""
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    carrier = commit(repo, "a real commit")
    registry = TaskRegistry([task("t")])
    registry.mark_completed("t")
    TaskStore(wired.tasks_file).save(registry)

    code = _record_shipped("t", repo, [carrier])

    assert code == 1
    assert "already completed" in capsys.readouterr().out
    assert not list((wired.workers_root.parent / "inbox").glob("*.json"))


def test_the_command_is_registered_and_needs_its_evidence():
    parser = cli.build_parser()

    args = parser.parse_args(
        ["record-shipped", "t", "--commit", SHA_A, "--note", "under x-01"]
    )
    assert args.func is cli._cmd_record_shipped
    assert args.commit == [SHA_A] and args.note == "under x-01"

    with pytest.raises(SystemExit):
        parser.parse_args(["record-shipped", "t", "--note", "n"])
    with pytest.raises(SystemExit):
        parser.parse_args(["record-shipped", "t", "--commit", SHA_A])


def test_shipped_report_exits_non_zero_on_a_record_that_stopped_holding(
    wired, tmp_path, capsys
):
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    side = commit(repo, "never landed")
    run_git(repo, "checkout", "-q", "work")
    registry = TaskRegistry([task("t")])
    registry.record_shipped_elsewhere("t", [side], "under x-01")
    TaskStore(wired.tasks_file).save(registry)

    code = cli._cmd_shipped_report(
        argparse.Namespace(config=None, repo=repo, base=base)
    )

    out = capsys.readouterr().out
    assert code == 1
    assert "INVALIDATED" in out
    assert "shipped_evidence_absent" in out
    assert "never resolved by changing the record" in out


# --- 10. the eight measured records -------------------------------------------


def test_all_eight_measured_records_can_be_expressed(tmp_path):
    """The measurement, end to end, and the asymmetry is the point.

    The five whose work IS present become evidence-backed records that satisfy
    their dependents and stay out of the sweep. The three that are completed
    with their work absent stay completed and surface as disagreements —
    converting them would rewrite a wrong record rather than show it, which is
    the "do not auto-convert" bound.
    """
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "inbox-02: the mutation vocabulary and its guards")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "bind-01: cut from 23f6829, never merged")
    run_git(repo, "checkout", "-q", "work")

    registry = TaskRegistry([
        task("inbox-02"),
        task("auto-11"), task("inbox-01"), task("inbox-08"),
        task("inbox-03", depends_on=("inbox-02",)),
        task("inbox-04", depends_on=("inbox-02",)),
        task("bind-01"), task("split-01"), task("dash-17"),
    ])
    # Group 1: present in the base under another task's commits.
    for tid, note in (
        ("auto-11", "policy.legacy_ask_user_retired shipped under auto-10"),
        ("inbox-01", "inbox.KINDS carries six mutation kinds already"),
        ("inbox-08", "MUTATION_PAYLOAD is the per-kind request protocol"),
        ("inbox-03", "orchestrator calls _drain_task_inbox every round"),
        ("inbox-04", "docs/SECURITY.md S30 documents the vocabulary"),
    ):
        registry.record_shipped_elsewhere(tid, [carrier], note)
    # Group 2: recorded completed, work absent. NOT converted.
    for tid in ("bind-01", "split-01", "dash-17"):
        registry.mark_completed(tid)

    # Group 1 is out of the queue, and inbox-03 / inbox-04 are no longer stuck
    # behind inbox-02 — the thing `retire` could never have done for them.
    assert [t.id for t in registry.ready_tasks()] == ["inbox-02"]
    for tid in ("auto-11", "inbox-01", "inbox-08", "inbox-03", "inbox-04"):
        assert registry.state_of(tid) is TaskState.SHIPPED_ELSEWHERE
    registry.mark_completed("inbox-02")
    assert {t.id for t in registry.ready_tasks()} == set()

    roadmap = [
        {"id": t.id, "title": t.title, "status": t.status,
         "shipped_commits": list(t.shipped_commits), "shipped_note": t.shipped_note,
         "shipped_at": t.shipped_at}
        for t in registry.all_tasks()
    ]
    report = dashboard.shipped_report(repo, roadmap, base)

    assert {r["id"] for r in report["elsewhere"]} == {
        "auto-11", "inbox-01", "inbox-08", "inbox-03", "inbox-04"
    }
    assert {r["state"] for r in report["elsewhere"]} == {"verified"}
    findings = {r["id"]: r for r in report["disagreements"]["rows"]}
    assert set(findings) == {"bind-01", "split-01", "dash-17"}
    assert findings["bind-01"]["kind"] == "completed_not_in_base"
    assert findings["bind-01"]["proven"] is True
    # split-01 and dash-17 are NO MENTION — reported, never claimed as proof.
    for tid in ("split-01", "dash-17"):
        assert findings[tid]["kind"] == "completed_unwitnessed"
        assert findings[tid]["proven"] is False
