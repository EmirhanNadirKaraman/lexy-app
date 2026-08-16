"""Operator task inbox + priority ordering.

The point of the inbox is that it is safe to write at ANY moment, including
while a write-capable agent is running. That safety rests on one property —
it lives outside the checkout, so the escape detector never sees it — and the
first test below pins exactly that.
"""

from __future__ import annotations

import json

import pytest

from autoloop.inbox import InboxError, TaskInbox, inbox_dir_for
from autoloop.tasks import Task, TaskRegistry


def test_the_inbox_lives_outside_the_checkout(tmp_path):
    """The property the whole design depends on. `escape_detector` snapshots
    tracked + untracked + IGNORED paths, so anything under the repo — including
    the gitignored `.autoloop/` — is inside the before/after comparison taken
    around every agent call. An operator write landing there mid-execute would
    park the loop LOOP-FATAL. Placing the inbox beside `workers_root`, which is
    already required to be external, is what makes mid-run submission safe."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    workers_root = tmp_path / "outside" / "workers"

    inbox = inbox_dir_for(workers_root, repo / ".autoloop")

    with pytest.raises(ValueError):
        inbox.resolve().relative_to(repo.resolve())


def test_submit_then_drain_round_trip(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit({"id": "a-1", "title": "T", "description": "D", "priority": 2})
    inbox.submit({"id": "a-2", "title": "T2", "description": "D2"})

    specs, problems = inbox.drain()
    assert problems == []
    assert [s["id"] for s in specs] == ["a-1", "a-2"], "submission order"
    assert specs[0]["priority"] == 2
    # Drained means gone — a second drain must not replay the same requests.
    assert inbox.drain() == ([], [])


def test_submit_refuses_a_malformed_request(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="missing required"):
        inbox.submit({"id": "a-1", "title": "T"})
    with pytest.raises(InboxError, match="unknown field"):
        inbox.submit({"id": "a", "title": "T", "description": "D", "urgency": "high"})
    with pytest.raises(InboxError, match="priority must be an integer"):
        inbox.submit({"id": "a", "title": "T", "description": "D", "priority": "high"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"


def test_an_unparseable_request_is_quarantined_not_replayed_forever(tmp_path):
    """One typo must never stop a running loop, and must not re-fail on every
    drain. Moved aside rather than deleted — it is what the operator wrote."""
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.directory.mkdir(parents=True)
    (inbox.directory / "20260801T000000Z-1-1.json").write_text("{not json", encoding="utf-8")

    specs, problems = inbox.drain()
    assert specs == []
    assert len(problems) == 1
    assert inbox.drain() == ([], []), "must not re-fail forever"
    assert list((inbox.directory / "rejected").glob("*.json")), "evidence preserved"


def test_submit_is_atomic(tmp_path):
    """A drain racing a submit must never see a half-written file."""
    inbox = TaskInbox(tmp_path / "inbox")
    path = inbox.submit({"id": "a-1", "title": "T", "description": "D"})
    assert json.loads(path.read_text())["id"] == "a-1"
    assert not list(inbox.directory.glob("*.tmp")), "no temp file left behind"


# ---- priority ordering -------------------------------------------------------


def test_next_ready_prefers_the_lower_priority_number():
    registry = TaskRegistry()
    registry.add_many([
        Task(id="later", title="L", description="d", priority=5),
        Task(id="urgent", title="U", description="d", priority=1),
        Task(id="default", title="D", description="d"),  # 100
    ])
    assert registry.next_ready().id == "urgent"


def test_a_task_added_later_can_overtake_one_already_queued():
    """The reason ordering changed from insertion order: otherwise an operator
    cannot steer a running loop — a task added mid-run could never be picked
    before the ones already queued, however urgent."""
    registry = TaskRegistry()
    registry.add_many([Task(id="first", title="F", description="d", priority=50)])
    assert registry.next_ready().id == "first"

    registry.add_many([Task(id="second", title="S", description="d", priority=1)])
    assert registry.next_ready().id == "second"


def test_equal_priorities_break_on_id_not_dict_order():
    registry = TaskRegistry()
    registry.add_many([
        Task(id="zzz", title="Z", description="d", priority=7),
        Task(id="aaa", title="A", description="d", priority=7),
    ])
    assert registry.next_ready().id == "aaa"


def test_priority_survives_a_persistence_round_trip(tmp_path):
    from autoloop.tasks import TaskStore

    store = TaskStore(tmp_path / "tasks.json")
    registry = TaskRegistry()
    registry.add_many([Task(id="p", title="P", description="d", priority=3)])
    store.save(registry)
    assert store.load().get("p").priority == 3


def test_an_old_tasks_file_without_priority_still_loads(tmp_path):
    """Backward compatibility: a roadmap written before the field existed must
    keep working, defaulting to last place rather than jumping the queue."""
    from autoloop.tasks import TaskStore

    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "tasks": [{"id": "old", "title": "O", "description": "d", "status": "pending"}],
    }), encoding="utf-8")
    assert TaskStore(path).load().get("old").priority == 100


# ---- the shared merge (one implementation, two callers) ----------------------


def test_apply_requests_is_the_single_merge_used_by_both_callers():
    """`Orchestrator._drain_task_inbox` and `python -m autoloop drain-inbox`
    must apply a request identically. Two copies would drift, and a drift means
    the same request behaves differently depending on who applied it."""
    import inspect

    from autoloop import cli, orchestrator
    from autoloop.inbox import apply_requests

    assert "apply_requests(" in inspect.getsource(orchestrator.Orchestrator._drain_task_inbox)
    assert "apply_requests(" in inspect.getsource(cli._cmd_drain_inbox)
    assert callable(apply_requests)


def test_apply_requests_adds_reprioritises_and_refuses_in_one_pass():
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="existing", title="E", description="d", priority=9)])

    added, reprioritised, refused = apply_requests(registry, [
        {"id": "brand-new", "title": "N", "description": "d", "priority": 2},
        {"kind": "priority", "id": "existing", "priority": 1},
        {"id": "existing", "title": "dupe", "description": "d"},      # duplicate id
        {"kind": "priority", "id": "ghost", "priority": 1},           # unknown task
    ])

    assert len(added) == 1 and "brand-new" in added[0]
    assert len(reprioritised) == 1 and "existing -> 1" in reprioritised[0]
    assert len(refused) == 2, refused
    # The good ones landed despite the bad ones queued alongside.
    assert registry.get("brand-new").priority == 2
    assert registry.get("existing").priority == 1
    assert registry.get("existing").title == "E", "the original is untouched"


def test_a_refused_batch_never_raises():
    """One typo must not discard the good requests queued behind it."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, reprioritised, refused = apply_requests(registry, [
        {"id": "", "title": "", "description": ""},
        {"id": "ok", "title": "T", "description": "d"},
    ])
    assert [a.split(" ")[0] for a in added] == ["ok"]
    assert len(refused) == 1


# ---- the mutation vocabulary -------------------------------------------------
#
# `task` + `priority` became `task` + six mutations. What keeps that from being
# the "general edit-a-task request" the priority-only design refused: the two
# authorities are split by question and each has ONE implementation (shape is
# `check_request_shape`, run at both gates; content is the registry), nothing a
# dispatch is currently reading can be edited, and blocking has a reverse.
# These pin all three.


def test_every_mutation_kind_reaches_its_registry_mutator(tmp_path):
    """One round trip per kind, through the real inbox: submit, drain, apply.
    A kind that is in the vocabulary but wired to nothing would otherwise look
    fine at submit and silently do nothing on merge."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("priority", "t", 1)
    inbox.submit_mutation("description", "t", "rewritten instructions")
    inbox.submit_mutation("approved_paths", "t", ["autoloop/inbox.py"])
    inbox.submit_mutation("depends_on", "t", ["dep"])
    inbox.submit_mutation("block", "t", "waiting on the API key")
    inbox.submit_mutation("unblock", "t")

    registry = TaskRegistry()
    registry.add_many([Task(id="dep", title="D", description="d"),
                       Task(id="t", title="T", description="d")])
    specs, problems = inbox.drain()
    added, applied, refused = apply_requests(registry, specs)

    assert (problems, added, refused) == ([], [], [])
    assert len(applied) == 6, applied
    task = registry.get("t")
    assert task.priority == 1
    assert task.description == "rewritten instructions"
    assert task.approved_paths == ("autoloop/inbox.py",)
    assert task.depends_on == ("dep",)
    assert task.status == "pending", "blocked then released"
    assert task.blocked_reason == ""


def test_a_mutation_request_carries_only_its_own_field(tmp_path):
    """The rule the priority branch has always had, now driven off
    `MUTATION_PAYLOAD` so a new kind cannot forget it. A request naming a field
    its kind ignores has not done what its author intended, so it is refused
    rather than dropped. This is the submit gate; the merge gate runs the same
    check — see
    `test_a_hand_written_mutation_carrying_a_foreign_field_is_refused_atomically`."""
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="carries only"):
        inbox.submit({"kind": "description", "id": "t", "description": "d",
                      "approved_paths": ["a.py"]})
    with pytest.raises(InboxError, match="carries only"):
        inbox.submit({"kind": "unblock", "id": "t", "reason": "because"})
    with pytest.raises(InboxError, match="needs the task 'id'"):
        inbox.submit({"kind": "block", "id": "  ", "reason": "because"})
    with pytest.raises(InboxError, match="needs 'approved_paths' as a list"):
        inbox.submit({"kind": "approved_paths", "id": "t", "approved_paths": "a.py"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"


def test_submission_validates_shape_only_and_leaves_content_to_the_registry(tmp_path):
    """Registry-derived refusal reasons. A blank description and a path with a
    glob in it are both well-SHAPED, so they queue — and are then refused on
    merge in the registry's own words, by the same validators creation calls.
    A second rule set here would drift and start refusing what `add_many`
    accepts."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("description", "t", "   ")
    inbox.submit_mutation("approved_paths", "t", ["autoloop/*.py"])
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="original")])

    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert applied == []
    assert len(refused) == 2, refused
    assert "non-empty description" in refused[0]
    assert "no globs" in refused[1]
    assert registry.get("t").description == "original", "nothing half-applied"


def test_a_mutation_cannot_strand_a_task_the_loop_is_running(tmp_path):
    """The refusal that makes the whole vocabulary safe to expose. All three
    content fields are what an already-started dispatch is judged against; the
    dependency case is the one with no way out at all, since the round then
    fails BOTH `mark_completed` and `release`."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit_mutation("depends_on", "running", ["other"])
    inbox.submit_mutation("approved_paths", "running", ["autoloop/tasks.py"])
    inbox.submit_mutation("block", "running", "hold this")
    # ... while the one mutation that cannot strand anything still lands.
    inbox.submit_mutation("priority", "running", 1)

    registry = TaskRegistry()
    registry.add_many([Task(id="other", title="O", description="d"),
                       Task(id="running", title="R", description="d",
                            approved_paths=("autoloop/inbox.py",))])
    registry.mark_in_progress("running")

    specs, _ = inbox.drain()
    _, applied, refused = apply_requests(registry, specs)

    assert len(refused) == 3, refused
    assert all("in progress" in line for line in refused), refused
    assert applied == ["running -> 1"]
    task = registry.get("running")
    assert (task.depends_on, task.approved_paths, task.status) == (
        (), ("autoloop/inbox.py",), "in_progress",
    )


def test_blocking_through_the_inbox_has_a_reverse_through_the_inbox(tmp_path):
    """A hold placed here writes no `blockers.Blocker` record, and
    `python -m autoloop answer` — the only route out of `blocked` — takes a
    blocker id. So without an `unblock` kind this vocabulary would write a
    state with no way back out of it."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])

    inbox.submit_mutation("block", "t", "waiting on the operator")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])
    assert refused == [] and len(applied) == 1
    assert registry.state_of("t").value == "blocked_by_operator"

    inbox.submit_mutation("unblock", "t")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])
    assert refused == [] and len(applied) == 1
    assert registry.state_of("t").value == "ready"


def test_the_inbox_reverse_will_not_release_a_loop_raised_quarantine(tmp_path):
    """The narrowing that keeps the reverse from being a bypass. A `task_fatal`
    quarantine is resolved by `answer`, which resolves the blocker record and
    unblocks the task together; releasing it from here would put the task back
    in the ready queue with its blocker still open."""
    from autoloop.inbox import apply_requests

    inbox = TaskInbox(tmp_path / "inbox")
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    registry.block("t", "validation failed three times")

    inbox.submit_mutation("unblock", "t")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])

    assert applied == []
    assert len(refused) == 1 and "autoloop answer" in refused[0]
    assert registry.get("t").blocked_reason == "validation failed three times"


def test_the_inbox_reverse_refuses_a_quarantine_that_merely_reads_like_a_hold(tmp_path):
    """The end-to-end twin of `test_tasks.py`'s registry regression, and the
    reason provenance is a stored field rather than the reason text.
    `blocked_reason` is free text the LOOP writes as well, so a park detail
    beginning with `OPERATOR_HOLD_PREFIX` used to make a real quarantine
    releasable from here — the blocker record left open and unanswered while
    the task went straight back into `ready_tasks()`."""
    from autoloop.inbox import apply_requests
    from autoloop.tasks import OPERATOR_HOLD_PREFIX

    inbox = TaskInbox(tmp_path / "inbox")
    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    reason = OPERATOR_HOLD_PREFIX + "quoted from the agent's own report"
    registry.block("t", reason)

    inbox.submit_mutation("unblock", "t")
    _, applied, refused = apply_requests(registry, inbox.drain()[0])

    assert applied == []
    assert len(refused) == 1 and "autoloop answer" in refused[0]
    assert registry.get("t").blocked_reason == reason
    assert registry.state_of("t").value == "blocked_by_operator"


def test_a_creation_request_cannot_carry_a_mutation_field(tmp_path):
    """The other half of "a request carries only its own kind's fields".
    `reason` belongs to `block`, and a `task` request naming it meant a hold —
    checked against one GLOBAL field set it submitted cleanly and was then
    silently ignored on merge, which is precisely the outcome the per-kind rule
    exists to prevent. This is the submit gate; the merge gate runs the same
    check — see
    `test_a_hand_written_creation_carrying_a_mutation_field_is_refused_on_merge`."""
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="mutation-only"):
        inbox.submit({"kind": "task", "id": "t", "title": "T", "description": "D",
                      "reason": "hold this instead"})
    # Same for the no-kind legacy form, which is a creation request too.
    with pytest.raises(InboxError, match="unknown field"):
        inbox.submit({"id": "t", "title": "T", "description": "D", "reason": "x"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"
    # The control: the same field on the kind that owns it is accepted.
    inbox.submit({"kind": "block", "id": "t", "reason": "hold this"})
    assert len(inbox.pending()) == 1


def test_one_shape_implementation_serves_both_gates():
    """The drift guard on the split, in the same style as the shared-merge and
    three-bucket guards above. `submit` is not the only gate: hand-writing the
    JSON file is the documented — and today the ONLY — operator route to five of
    the six mutation kinds, and such a file reaches `apply_requests` without
    ever passing through `submit`. Two shape implementations would mean the
    field an operator typed is refused by one route and silently ignored by the
    other, which is the whole defect the per-kind rule exists to prevent."""
    import ast
    import inspect
    import textwrap

    from autoloop import inbox

    for gate in (inbox.TaskInbox.submit, inbox.apply_requests):
        # An actual CALL node, not a substring of the source. Both of these
        # discuss the shared check at length in their docstrings, and a guard a
        # comment can satisfy guards nothing.
        tree = ast.parse(textwrap.dedent(inspect.getsource(gate)))
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "check_request_shape" in called, (
            f"{gate.__qualname__} does not go through the shared shape check"
        )


def test_a_hand_written_creation_carrying_a_mutation_field_is_refused_on_merge():
    """`apply_requests` called DIRECTLY, which is what a hand-written file gets:
    `drain` hands it the parsed object and nothing else ran `submit`'s checks.
    The per-kind contract has to hold here too, or the route the vocabulary
    documents as the only one for the new kinds is the one route with no gate —
    this request used to be applied as a plain creation with the `reason` the
    author meant as a hold silently dropped."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, applied, refused = apply_requests(registry, [
        {"kind": "task", "id": "held", "title": "T", "description": "D",
         "reason": "hold this instead"},
        {"id": "queued-behind", "title": "Q", "description": "d"},
    ])

    assert len(refused) == 1, refused
    assert refused[0].startswith("held: "), refused[0]
    assert "mutation-only" in refused[0], refused[0]
    assert not registry.has("held"), "atomic: the task must not be half-created"
    assert applied == []
    assert [a.split(" ")[0] for a in added] == ["queued-behind"], (
        "the valid request queued behind it still lands"
    )


def test_a_hand_written_mutation_carrying_a_foreign_field_is_refused_atomically():
    """The mutation half of the same hole, and the one that shows why "atomic"
    needs asserting on BOTH fields: `apply_requests` used to read the key its
    kind names and ignore the rest, so this landed the hold and dropped the
    scope rewrite — a request that half-did what it said."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d",
                            approved_paths=("autoloop/inbox.py",))])

    added, applied, refused = apply_requests(registry, [
        {"kind": "block", "id": "t", "reason": "hold this",
         "approved_paths": ["autoloop/tasks.py"]},
        {"kind": "priority", "id": "t", "priority": 3},
    ])

    assert len(refused) == 1, refused
    assert "carries only" in refused[0], refused[0]
    task = registry.get("t")
    assert (task.status, task.blocked_reason, task.hold_origin) == ("pending", "", "")
    assert task.approved_paths == ("autoloop/inbox.py",), "neither field landed"
    assert added == []
    assert applied == ["t -> 3"], "the valid request queued behind it still lands"


def test_no_payload_field_falls_outside_both_per_kind_sets():
    """A drift guard on the split, not a behaviour test. Every mutation payload
    has to be either a creation field or declared mutation-only, or
    `ALLOWED_FIELDS` — the union `dashboard.TASK_REQUEST_FIELDS` documents
    itself against — quietly stops naming the whole vocabulary. Nothing
    validates against the union, which is exactly why nothing else would fail
    when a new kind forgets it."""
    from autoloop.inbox import (
        ALLOWED_FIELDS,
        CREATION_FIELDS,
        MUTATION_ONLY_FIELDS,
        MUTATION_PAYLOAD,
    )

    payloads = {p for p in MUTATION_PAYLOAD.values() if p is not None}
    assert payloads <= ALLOWED_FIELDS
    assert ALLOWED_FIELDS == CREATION_FIELDS | MUTATION_ONLY_FIELDS
    assert "reason" not in CREATION_FIELDS, "the leak this split closed"


def test_retire_is_not_in_the_vocabulary(tmp_path):
    """Deliberate and load-bearing. `retire` is written-once with no reverse by
    design, so an inbox request that reached it would be exactly the
    unblockable one-way state `block`/`unblock` are shaped to avoid."""
    from autoloop.inbox import KINDS

    assert "retire" not in KINDS
    with pytest.raises(InboxError, match="unknown kind"):
        TaskInbox(tmp_path / "inbox").submit({"kind": "retire", "id": "t"})


def test_requests_apply_in_submission_order_in_one_pass():
    """`drain` returns oldest-first and `apply_requests` makes a SINGLE pass in
    that order. Two consequences, both asserted here: the last write to a field
    wins, and a mutation queued before its target exists is REFUSED rather than
    held back — deferring would make the outcome depend on what else happened
    to be in the batch."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    added, applied, refused = apply_requests(registry, [
        {"kind": "priority", "id": "late", "priority": 1},          # before it exists
        {"id": "late", "title": "L", "description": "d"},
        {"kind": "priority", "id": "late", "priority": 5},
        {"kind": "priority", "id": "late", "priority": 2},          # last one wins
    ])

    assert len(added) == 1
    assert len(refused) == 1 and "no task with id 'late'" in refused[0]
    assert applied == ["late -> 5", "late -> 2"]
    assert registry.get("late").priority == 2


def test_a_hand_written_request_with_an_unknown_kind_is_named_not_guessed_at():
    """`submit` refuses an unknown kind, so this is only reachable from a file
    an operator wrote by hand. Falling through to the creation branch would
    refuse it for whichever unrelated field it happens to lack, sending the
    reader after the wrong problem."""
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    _, applied, refused = apply_requests(registry, [
        {"kind": "retire", "id": "t"},
        # An UNHASHABLE kind. `kind in MUTATION_PAYLOAD` is a dict lookup, so
        # this raises `TypeError: unhashable type` unless the string check runs
        # first — and it would raise from outside the per-request try, taking
        # the whole drain (and the running loop's step) down with one file.
        {"kind": [], "id": "t"},
        {"id": "later", "title": "L", "description": "d"},
    ])

    assert applied == []
    assert len(refused) == 2, refused
    assert "unknown kind 'retire'" in refused[0]
    assert "unknown kind []" in refused[1]
    assert registry.has("later"), "the request queued behind them still landed"


def test_the_middle_bucket_stays_one_bucket_both_callers_already_save_on():
    """Not cosmetic. Both drain call sites unpack three values positionally and
    gate `task_store.save()` on `if added or <middle>`; a fourth bucket either
    of them forgot to add to that condition would apply a mutation in memory
    and never persist it, and the next in-memory save would overwrite it
    silently. One bucket cannot be half-wired."""
    import inspect

    from autoloop import cli, orchestrator
    from autoloop.inbox import apply_requests

    registry = TaskRegistry()
    registry.add_many([Task(id="t", title="T", description="d")])
    assert len(apply_requests(registry, [])) == 3, "three buckets, not four"

    for source in (inspect.getsource(orchestrator.Orchestrator._drain_task_inbox),
                   inspect.getsource(cli._cmd_drain_inbox)):
        call = next(ln for ln in source.splitlines() if "apply_requests(" in ln)
        names = [n.strip() for n in call.split("=")[0].split(",")]
        assert len(names) == 3, f"caller unpacks {names}, not three buckets"
        assert f"if {names[0]} or {names[1]}:" in source, (
            f"this caller does not persist its {names[1]!r} bucket"
        )


def test_the_cli_actually_builds_an_orchestrator(tmp_path, monkeypatch):
    """The gap that let a broken `run` ship: every other test constructs the
    Orchestrator directly, so nothing exercised `_build_orchestrator` /
    `_build_executor`. A keyword landing on the wrong constructor (task_inbox
    was passed to ImplementExecutor) type-errors only at real startup.

    Builds the real collaborator set against a throwaway repo — no browser, no
    agent, no network: construction is the whole assertion.
    """
    import subprocess

    from autoloop import cli
    from autoloop.config import load_config

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "work"), ("config", "user.email", "t@e.com"),
                 ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True)

    # `_build_orchestrator` provisions the publisher repo, which needs a real
    # remote to snapshot a url from.
    upstream = tmp_path / "upstream.git"
    subprocess.run(["git", "init", "-q", "--bare", str(upstream)], check=True,
                   capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(upstream)], cwd=repo,
                   check=True, capture_output=True)

    (repo / ".autoloop").mkdir()
    (repo / ".autoloop" / "config.toml").write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/abc"\n\n'
        f'[paths]\nworkers_root = "{tmp_path / "outside" / "workers"}"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)
    config = load_config(repo / ".autoloop" / "config.toml")
    store, state = cli._load_state(config)
    if state is None:
        from autoloop.state import LoopState, StateStore

        state = LoopState.new(config.browser.conversation_url)
        store = StateStore(config.state_file)
        store.save(state)
    task_store, registry = cli._load_tasks(config)

    orch = cli._build_orchestrator(
        config, argparse_ns(config), store, state, task_store, registry
    )
    assert orch._task_inbox is not None, "the inbox must reach the Orchestrator"
    assert orch._task_inbox.directory.is_absolute()


def argparse_ns(config):
    import argparse

    return argparse.Namespace(config=config.state_dir / "config.toml", null_executor=True)
