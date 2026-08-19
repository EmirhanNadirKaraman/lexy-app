"""The live tracker. Read-only is the load-bearing property: the loop's escape
detector refuses a write-capable task if the primary checkout is dirty, so a
tracker that touched the working tree would stop the thing it observes."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autoloop.dashboard import (
    GROUPS,
    IN_PROGRESS_KINDS,
    MARKS,
    MERGE_GROUPS,
    MERGE_STATES,
    PAGE,
    STAT_BUCKETS,
    STATUS,
    app_tasks,
    collect,
    is_ancestor,
    merge_groups,
    merge_states,
    pipeline,
    roadmap_stats,
    task_groups,
    worker_progress,
)
from autoloop.state import utcnow_iso
from autoloop.tasks import TaskRegistry, TaskState


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """The tracker memoizes remote refs (60s), ancestry verdicts and
    shallowness at module level, so a test would otherwise read the previous
    test's repository. Ancestry is the sharp one: `make_repo` commits fixed
    content with a fixed author and no `GIT_AUTHOR_DATE`, so two repos built in
    the same wall-clock second have the SAME commit sha in different
    directories — a verdict keyed on the sha alone would leak across them and
    fail only near a second boundary."""
    import autoloop.dashboard as dash

    for cache in (dash._REMOTE_CACHE, dash._ANCESTRY_CACHE, dash._SHALLOW_CACHE):
        cache.clear()
    yield
    for cache in (dash._REMOTE_CACHE, dash._ANCESTRY_CACHE, dash._SHALLOW_CACHE):
        cache.clear()


def snapshot(root: Path) -> dict:
    """Every file under `root` with its mtime — the same shape the read-only
    test uses, so "did anything change" is one comparison."""
    return {p: p.stat().st_mtime_ns for p in Path(root).rglob("*") if p.is_file()}


def run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".autoloop").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / "autoloop").mkdir()
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "t@e.com")
    run_git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x\n")
    run_git(repo, "add", "f.txt")
    run_git(repo, "commit", "-q", "-m", "init")
    return repo


def test_collect_writes_nothing_to_the_observed_checkout(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".autoloop" / "state.json").write_text(json.dumps({"phase": "executing"}))
    # Order matters: `run_git` here does NOT pass --no-optional-locks, so it
    # rewrites .git/index. Snapshot AFTER it, or the test measures its own
    # side effect and blames the tracker.
    status_before = run_git(repo, "status", "--porcelain")
    before = {p: p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}

    collect(repo)

    after = {p: p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}
    # Every file, including .git internals — `git status` refreshes and rewrites
    # `.git/index` unless told not to, which a 2s poll would do forever.
    assert after == before, "the tracker must not create, remove or touch any file"
    assert run_git(repo, "status", "--porcelain") == status_before


def test_roadmap_falls_back_to_the_seed_when_no_registry_exists(tmp_path):
    """`tasks.json` only appears once a registry is saved. Reading only that
    showed an empty roadmap while `next-task` correctly reported rt-01."""
    repo = make_repo(tmp_path)
    (repo / "autoloop" / "seed_tasks.json").write_text(
        json.dumps([{"id": "rt-01", "title": "admin-gate", "description": "d"}])
    )
    d = collect(repo)
    assert [r["id"] for r in d["roadmap"]] == ["rt-01"]
    assert d["roadmap"][0]["status"] == "pending"


def test_app_tasks_come_from_the_audit_report_not_the_registry(tmp_path):
    """The registry holds only what was seeded; the proposed backlog lives in
    the audit report. A tracker reading the registry alone would report one
    task when there are many."""
    repo = make_repo(tmp_path)
    (repo / "docs" / "AUDIT_2026-07-30.md").write_text(
        "| **rt-01** | **P1** | **Admin-gate the endpoints.** more text |\n"
        "| rt-02 | P2 | Something else entirely |\n"
    )
    tasks = app_tasks(repo)
    assert [t["id"] for t in tasks] == ["rt-01", "rt-02"]
    assert tasks[0]["priority"] == "P1"
    assert "*" not in tasks[0]["title"], "markdown emphasis must be stripped"


def test_pipeline_marks_blocked_when_the_loop_is_parked():
    stages = pipeline({"phase": "needs_user"}, [], [{"id": "blk-1"}])
    assert stages[0]["state"] == "blocked"
    idle_or_blocked = {s["state"] for s in stages}
    assert "active" not in idle_or_blocked, "a parked loop has nothing running"


# ---- visual encoding (dataviz method) ---------------------------------------
#
# The page's colours are a correctness property, not taste: an earlier version
# painted the "running" stage with the reserved status-good green, which spends
# the good/bad channel on progress and leaves nothing to say with when something
# is actually wrong. These pin the rules that fix cost real debugging to find.


def test_status_colours_are_never_used_for_a_non_status_state():
    """`blocked` is the only pipeline state that is a health verdict, so it is
    the only one allowed a status colour. `active`/`done`/`idle` describe
    progress and must draw from the mark roles instead."""
    fill = PAGE.split("const FILL = {", 1)[1].split("};", 1)[0]
    compact = "".join(fill.split())
    assert 'active:"var(--mark-active)"' in compact
    assert 'done:"var(--mark-done)"' in compact
    assert 'idle:"var(--mark-idle)"' in compact
    for role in ("--good", "--warning", "--serious"):
        assert role not in fill, f"{role} is a reserved status colour, not a progress mark"
    assert "--critical" in fill, "blocked must still use the status critical role"


def test_every_pipeline_state_ships_an_icon_and_a_word():
    """Colour never carries meaning alone — required because two marks sit
    below 3:1 on their surface, and because CVD readers get no hue at all."""
    states = {"active", "done", "blocked", "idle"}
    mark = PAGE.split("const MARK = {", 1)[1].split("};", 1)[0]
    for s in states:
        assert f"{s}:[" in mark.replace(" ", ""), f"state {s} has no icon+word pair"


def test_pipeline_only_emits_states_the_page_can_draw():
    """A state the page has no mark for would render as an undefined fill —
    invisible, and silently so."""
    drawable = {"active", "done", "blocked", "idle"}
    scenarios = [
        ({"phase": "executing", "task_execution": {"task_id": "t"}}, [], []),
        ({"phase": "needs_user"}, [], [{"id": "b"}]),
        ({"phase": "awaiting", "task_execution": {"task_id": "t", "candidate_sha": "a" * 40},
          "last_decision": "push"}, [{"domain": "d"}], []),
        ({}, [], []),
    ]
    for state, agents, blockers in scenarios:
        stages = pipeline(state, agents, blockers)
        emitted = {s["state"] for s in stages}
        assert emitted <= drawable, f"undrawable state(s) {emitted - drawable}"


def test_validated_mark_hexes_are_the_ones_the_page_ships():
    """The hexes in MARKS are the ones `validate_palette.js` passed against the
    node surface (see dashboard.py's MARKS comment). If someone edits the CSS
    without re-running the validator, this catches the drift."""
    for mode in ("light", "dark"):
        for role, hexv in MARKS[mode].items():
            assert f"--mark-{role}:{hexv}" in PAGE, f"{mode}/{role} drifted from {hexv}"
    for role, hexv in STATUS.items():
        assert f"--{role}:{hexv}" in PAGE
    # Status colours are fixed, never themed: exactly one declaration each.
    assert PAGE.count("--critical:#d03b3b") == 1


def test_stat_tile_values_use_proportional_figures():
    """`tabular-nums` on a large standalone number makes it look loose; it
    belongs on columns that align vertically, which here is `code`."""
    tile_rule = PAGE.split(".v{", 1)[1].split("}", 1)[0]
    assert "tabular-nums" not in tile_rule
    code_rule = PAGE.split("code{", 1)[1].split("}", 1)[0]
    assert "tabular-nums" in code_rule


# ---- priority editing (applied immediately, never queued) --------------------
#
# Setting a priority used to write a request into the inbox, which the loop
# applied whenever it next drained between steps. The page kept re-rendering
# from `tasks.json` in the meantime, so a save that worked looked exactly like
# one that did not and the operator resubmitted (two such requests sat in the
# inbox on 2026-08-05). It is now written straight to `tasks.json` and read
# back. Creation is still queued — that one carries authorization.


def test_a_queued_priority_request_is_still_drained(tmp_path, monkeypatch):
    """The inbox `priority` kind did not go away: a request written into that
    directory by hand is still the loop's to apply. Only the dashboard stopped
    queueing them."""
    import autoloop.dashboard as dash
    from autoloop.inbox import TaskInbox

    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    monkeypatch.setattr(dash, "_inbox_dir", lambda _repo: inbox_dir)

    before = snapshot(repo)
    TaskInbox(dash._inbox_dir(repo)).submit_priority("rt-01", 3)
    assert snapshot(repo) == before, "queueing must still touch nothing observed"

    specs, problems = TaskInbox(inbox_dir).drain()
    assert problems == []
    assert specs == [{"kind": "priority", "id": "rt-01", "priority": 3}]


def write_tasks(repo, tasks):
    """A registry file where `collect` and `/api/priority` both look for it."""
    import autoloop.dashboard as dash

    path = dash._tasks_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "tasks": tasks}), encoding="utf-8")
    return path


def a_task(task_id, **over):
    row = {"id": task_id, "title": task_id.upper(), "description": "d",
           "status": "pending", "priority": 100, "depends_on": [],
           "approved_paths": ["autoloop/dashboard.py"]}
    row.update(over)
    return row


@contextlib.contextmanager
def serving_with_store(tmp_path, repo, monkeypatch):
    """The dashboard served for real, with BOTH operator paths pointed outside
    the checkout: the inbox directory and the mutation ledger."""
    import autoloop.dashboard as dash
    from autoloop.tasks import TaskStore

    outside = tmp_path / "outside"
    monkeypatch.setattr(
        dash, "_task_store",
        lambda r: TaskStore(dash._tasks_file(r), ledger=outside / "task-mutations.jsonl"),
    )
    with serving(repo, outside / "inbox", monkeypatch) as base:
        yield base


def test_a_priority_edit_is_applied_and_read_back_immediately(tmp_path, monkeypatch):
    """Not queued: the file has the new value the moment the POST returns, and
    the number in the response came from re-reading that file rather than from
    the request body."""
    repo = make_repo(tmp_path)
    tasks_file = write_tasks(repo, [a_task("dash-03", priority=3)])

    with serving_with_store(tmp_path, repo, monkeypatch) as base:
        status, body = post(base, "/api/priority", {"id": "dash-03", "priority": 2})

    assert status == 200, body
    assert body["priority"] == 2 and body["applied"] is True
    stored = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert stored["tasks"][0]["priority"] == 2
    # And a fresh read of the page shows it, which is what the operator was
    # watching snap back before.
    assert collect(repo)["roadmap"][0]["priority"] == 2


def test_a_priority_edit_changes_nothing_but_the_priority(tmp_path, monkeypatch):
    """The read-only posture is intact except for this ONE field. `status` is
    what the loop dispatches on, `approved_paths` is authorization, `depends_on`
    reorders the graph — and none of `state.json`, the execution records or the
    blockers may be touched at all."""
    repo = make_repo(tmp_path)
    state = repo / ".autoloop"
    (state / "state.json").write_text(json.dumps({"phase": "executing"}), encoding="utf-8")
    (state / "blockers").mkdir()
    (state / "blockers" / "blk-1.json").write_text(json.dumps({"id": "blk-1"}), encoding="utf-8")
    (state / "executions").mkdir()
    (state / "executions" / "dash-03.json").write_text(
        json.dumps({"task_id": "dash-03"}), encoding="utf-8")
    tasks_file = write_tasks(repo, [
        a_task("dash-03", priority=3, depends_on=[], status="pending"),
    ])
    before_rows = json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"]
    untouched = {p: p.stat().st_mtime_ns for p in repo.rglob("*")
                 if p.is_file() and p != tasks_file}

    with serving_with_store(tmp_path, repo, monkeypatch) as base:
        status, body = post(base, "/api/priority", {"id": "dash-03", "priority": 1})

    assert status == 200, body
    after_rows = json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"]
    assert after_rows[0]["priority"] == 1
    assert after_rows[0]["status"] == "pending"
    assert after_rows[0]["approved_paths"] == ["autoloop/dashboard.py"]
    assert after_rows[0]["depends_on"] == []
    for before_row, after_row in zip(before_rows, after_rows):
        assert {k: v for k, v in before_row.items() if k != "priority"}.items() <= \
               {k: v for k, v in after_row.items() if k != "priority"}.items()
    # The only NEW file inside the checkout is the (always empty) mutex file.
    now = {p: p.stat().st_mtime_ns for p in repo.rglob("*")
           if p.is_file() and p != tasks_file}
    appeared = set(now) - set(untouched)
    assert {p.name for p in appeared} <= {"tasks.json.lock"}
    assert all(p.read_bytes() == b"" for p in appeared)
    assert {p: v for p, v in now.items() if p not in appeared} == untouched


def test_a_priority_edit_refuses_a_field_it_must_never_apply(tmp_path, monkeypatch):
    """Refused, not silently dropped. The queued route was gated by
    `inbox.check_request_shape`; applying directly must not become the lax door
    into the same field set."""
    repo = make_repo(tmp_path)
    tasks_file = write_tasks(repo, [a_task("dash-03", priority=3)])

    with serving_with_store(tmp_path, repo, monkeypatch) as base:
        status, body = post(base, "/api/priority", {
            "id": "dash-03", "priority": 1,
            "approved_paths": ["lexy-app/backend/routers/books.py"],
        })

    assert status == 400
    assert "approved_paths" in body["error"]
    stored = json.loads(tasks_file.read_text(encoding="utf-8"))
    assert stored["tasks"][0]["priority"] == 3, "a refused edit changes nothing"


def test_a_failed_priority_edit_reports_the_reason(tmp_path, monkeypatch):
    """A row that snaps back with nothing said is the bug this endpoint exists
    to fix, so a refusal has to arrive as a refusal — in the registry's own
    words."""
    repo = make_repo(tmp_path)
    write_tasks(repo, [a_task("dash-03", priority=3)])

    with serving_with_store(tmp_path, repo, monkeypatch) as base:
        unknown_status, unknown_body = post(base, "/api/priority",
                                            {"id": "nope", "priority": 1})
        bad_status, bad_body = post(base, "/api/priority",
                                    {"id": "dash-03", "priority": "high"})

    assert unknown_status == 400 and "nope" in unknown_body["error"]
    assert bad_status == 400 and bad_body["error"]


def test_a_priority_edit_will_not_create_the_registry(tmp_path, monkeypatch):
    """`collect` falls back to the tracked seed for DISPLAY when there is no
    `tasks.json`. Writing one from a priority form would be a brand-new write
    path — the loop creates the registry, on its first task-graph change."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    tasks_file = dash._tasks_file(repo)
    assert not tasks_file.exists()

    with serving_with_store(tmp_path, repo, monkeypatch) as base:
        status, body = post(base, "/api/priority", {"id": "rt-01", "priority": 1})

    assert status == 500, body
    assert not tasks_file.exists(), "a refused edit must not create the registry"


#: A real process holding the RUN-level lock the way a live `run --continuous`
#: does: for its whole run, letting go only when told. Spelled out here rather
#: than imported from `test_tasks.py` — `autoloop/tests/` is not a package, so
#: one test module cannot import another by path in every runner.
_LOOP_LOCK_HOLDER = """
import sys, time
from pathlib import Path
from autoloop.lock import LoopLock

state_dir, held_flag, release_flag = sys.argv[1:4]
lock = LoopLock(Path(state_dir)).acquire()
Path(held_flag).write_text("held")
while not Path(release_flag).exists():
    time.sleep(0.02)
lock.release()
"""


def test_a_priority_edit_lands_while_the_loop_lock_is_held(tmp_path, monkeypatch):
    """The case the whole design is for. `LoopLock` is held for the ENTIRE run
    — that is why `answer` and `release` refuse while the loop is up — so an
    edit that waited for it would be waiting for the loop to stop. Driven over
    the real socket, against a lock a REAL other process owns, with the wall
    clock asserted: "does not block" has to be measured, not implied."""
    import autoloop
    from autoloop.errors import LockHeldError
    from autoloop.lock import LoopLock

    repo = make_repo(tmp_path)
    tasks_file = write_tasks(repo, [a_task("dash-03", priority=3)])
    state_dir = repo / ".autoloop"
    held, release = tmp_path / "held", tmp_path / "release"
    package_root = Path(autoloop.__file__).resolve().parent.parent

    child = subprocess.Popen(
        [sys.executable, "-c", _LOOP_LOCK_HOLDER, str(state_dir), str(held), str(release)],
        env={**os.environ, "PYTHONPATH": str(package_root)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while not held.exists() and time.monotonic() < deadline:
            assert child.poll() is None, child.communicate()
            time.sleep(0.02)
        assert held.exists(), "the child never took the lock"
        with pytest.raises(LockHeldError):
            LoopLock(state_dir).acquire()  # genuinely held, not merely a file
        with serving_with_store(tmp_path, repo, monkeypatch) as base:
            started = time.monotonic()
            status, body = post(base, "/api/priority", {"id": "dash-03", "priority": 1})
            elapsed = time.monotonic() - started
    finally:
        # Reaped, never ASSERTED here: an assertion inside `finally` would
        # replace a real failure from the body with a subprocess one.
        release.write_text("go")
        try:
            code = child.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
            child.kill()
            code = child.wait(timeout=30)

    assert code == 0, "the lock holder failed"
    assert status == 200, body
    assert elapsed < 5.0, f"the edit waited {elapsed:.1f}s on a lock it must not take"
    assert json.loads(tasks_file.read_text(encoding="utf-8"))["tasks"][0]["priority"] == 1


def test_a_priority_request_cannot_change_authorization():
    """Priority is the only thing this endpoint can express. It must not be a
    general task editor — `approved_paths` is authorization surface and belongs
    in a reviewable `plan`, not a form on a localhost page."""
    from autoloop.inbox import InboxError, TaskInbox

    inbox = TaskInbox(Path("/tmp/never-written"))
    with pytest.raises(InboxError, match="only id \\+ priority"):
        inbox.submit({
            "kind": "priority", "id": "rt-01", "priority": 1,
            "approved_paths": ["lexy-app/backend/routers/books.py"],
        })


def test_the_roadmap_is_sorted_by_priority_for_display(tmp_path):
    """What the operator sees must match what `next_ready()` would pick."""
    repo = make_repo(tmp_path)
    (repo / ".autoloop" / "tasks.json").write_text(json.dumps({
        "schema_version": 1,
        "tasks": [
            {"id": "b", "title": "B", "description": "d", "status": "pending", "priority": 9},
            {"id": "a", "title": "A", "description": "d", "status": "pending", "priority": 1},
        ],
    }), encoding="utf-8")
    roadmap = collect(repo)["roadmap"]
    assert [t["id"] for t in roadmap] == ["a", "b"]
    assert roadmap[0]["priority"] == 1


# ---- task creation (the second write path) -----------------------------------
#
# These drive the REAL handler over a real socket, not `TaskInbox` directly: the
# thing worth pinning is routing + field handling + what reaches the inbox file,
# and a test that calls `TaskInbox.submit` itself would pass with the endpoint
# deleted.


@contextlib.contextmanager
def serving(repo, inbox_dir, monkeypatch):
    """The dashboard on an ephemeral port, with its inbox redirected outside the
    checkout — the placement the whole design rests on."""
    import autoloop.dashboard as dash

    monkeypatch.setattr(dash, "_inbox_dir", lambda _repo: inbox_dir)
    monkeypatch.setattr(dash.Handler, "repo", repo)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def post(base, path, payload, headers=None):
    """POST JSON, returning `(status, body)` for refusals as well as successes.
    Every call carries a timeout: a hung request here would stall the suite
    rather than fail it."""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"X-Autoloop": "1"} if headers is None else headers)},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_the_task_endpoint_queues_exactly_what_was_typed(tmp_path, monkeypatch):
    """`approved_paths` is authorization surface, so the request that lands in
    the inbox must carry the operator's paths verbatim and in order — nothing
    inferred from the title, nothing defaulted, no wildcard. And the checkout
    must be byte-identical across the request: a write into `.autoloop/` while
    an agent is running trips the escape detector and parks the loop loop-fatal,
    which is why the inbox lives outside the checkout in the first place."""
    from autoloop.inbox import TaskInbox

    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    paths = ["autoloop/dashboard.py", "autoloop/tests/test_dashboard.py", "docs/"]

    with serving(repo, inbox_dir, monkeypatch) as base:
        before = snapshot(repo)
        status, body = post(base, "/api/task", {
            "id": "dash-03", "title": "Author tasks", "description": "form on the page",
            "priority": 4, "approved_paths": paths,
        })
        assert snapshot(repo) == before, "the observed checkout must be untouched"

    assert status == 200, body
    assert body["queued"] == "dash-03"
    specs, problems = TaskInbox(inbox_dir).drain()
    assert problems == []
    assert specs == [{
        "kind": "task", "id": "dash-03", "title": "Author tasks",
        "description": "form on the page", "priority": 4, "approved_paths": paths,
    }]


def test_a_task_with_no_approved_paths_is_refused_rather_than_queued(tmp_path, monkeypatch):
    """The registry accepts an empty scope and `effective_approved_paths` keeps
    returning (), so the orchestrator then refuses to dispatch that task for
    ever. Queuing one is a trap, not a task — say so at submit."""
    from autoloop.inbox import TaskInbox

    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    with serving(repo, inbox_dir, monkeypatch) as base:
        status, body = post(base, "/api/task", {
            "id": "dash-03", "title": "T", "description": "d", "approved_paths": ["  ", ""],
        })
    assert status == 400
    assert "approved_paths" in body["error"]
    assert TaskInbox(inbox_dir).pending() == [], "a refused request must queue nothing"


def test_the_task_endpoint_refuses_a_field_the_form_cannot_produce(tmp_path, monkeypatch):
    """Narrower than `inbox.ALLOWED_FIELDS` on purpose: `validation` decides
    which commands grade the task and `depends_on` reorders the graph, and
    neither is on this form — so a request carrying one did not come from this
    page. Refused, not silently dropped."""
    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    with serving(repo, inbox_dir, monkeypatch) as base:
        status, body = post(base, "/api/task", {
            "id": "dash-03", "title": "T", "description": "d",
            "approved_paths": ["autoloop/dashboard.py"],
            "validation": [["echo", "ok"]],
        })
    assert status == 400
    assert "validation" in body["error"]


def test_the_write_path_keeps_its_routing_guards(tmp_path, monkeypatch):
    """The page has no authentication. The custom header is what a cross-origin
    form post cannot set without a preflight this server never approves — it
    must gate the new endpoint too, not just `/api/priority`."""
    from autoloop.inbox import TaskInbox

    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    good = {"id": "dash-03", "title": "T", "description": "d",
            "approved_paths": ["autoloop/dashboard.py"]}
    with serving(repo, inbox_dir, monkeypatch) as base:
        no_header, _ = post(base, "/api/task", good, headers={})
        cross, _ = post(base, "/api/task", good,
                        headers={"X-Autoloop": "1", "Origin": "http://evil.example"})
        unknown, _ = post(base, "/api/anything", good)
    assert (no_header, cross, unknown) == (403, 403, 404)
    assert TaskInbox(inbox_dir).pending() == [], "no refusal may reach the inbox"


def test_pending_authorization_is_shown_as_text_before_the_loop_merges_it(tmp_path, monkeypatch):
    """The loop applies a queued creation request without asking again, so the
    window between submit and merge is the only chance to notice a scope nobody
    meant to grant. A count would not do — the paths themselves must be on the
    page."""
    import autoloop.dashboard as dash
    from autoloop.inbox import TaskInbox

    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    monkeypatch.setattr(dash, "_inbox_dir", lambda _repo: inbox_dir)
    TaskInbox(inbox_dir).submit({
        "kind": "task", "id": "dash-03", "title": "T", "description": "d",
        "approved_paths": ["autoloop/dashboard.py"],
    })
    queued = collect(repo)["inbox"]
    assert queued == [{"kind": "task", "id": "dash-03", "priority": None, "title": "T",
                       "approved_paths": ["autoloop/dashboard.py"]}]
    # …and the page renders that field rather than counting the requests.
    assert "r.approved_paths" in PAGE


def test_the_creation_form_is_static_markup_with_one_listener():
    """`render()` rebuilds a section's innerHTML on every 2s poll. A form built
    by JS would lose half-typed text on the next tick and rebind its submit
    listener each time, so a later submit would fire twice — one queued task per
    accumulated listener."""
    static_markup, script = PAGE.split("<script>", 1)
    assert '<form class="newtask" id="newtask"' in static_markup
    assert "<form" not in script, "no form markup may be generated by a re-render"
    assert script.count("ntform.addEventListener") == 1, "exactly one listener, bound at load"
    # The form's button shares the `save` class for styling, so the roadmap's
    # per-render binding must be scoped — unscoped, it hands the STATIC button
    # one extra click listener per poll, each of which looks up an
    # `input.pri[data-id="undefined"]` that does not exist and throws.
    assert 'querySelectorAll("#roadmap button.save")' in script, \
        "the per-render binding must not reach the static form's submit button"


def test_a_stale_process_reports_itself(tmp_path, monkeypatch):
    """`PAGE` is baked in at import, so a process started before an edit serves
    the old HTML for as long as it lives — indistinguishable from a feature that
    never shipped, which is exactly how it was hit. The payload compares the
    import-time source hash against the file's current one."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    assert collect(repo)["build"]["stale"] is False, "unchanged source is not stale"

    monkeypatch.setattr(dash, "_IMPORT_STAMP", "0000deadbeef")
    build = collect(repo)["build"]
    assert build["stale"] is True
    assert build["running"] == "0000deadbeef"
    assert build["on_disk"] != build["running"]


# ---- liveness and the audit-report parser (2026-08-03) -----------------------
#
# The header said "stopped" while the loop was executing, and the app-task
# panel was empty. Three separate bugs, all of which reported an absence rather
# than an error — the worst shape, because nothing looks broken.


def test_liveness_reads_the_LOCK_not_a_process_name(tmp_path, monkeypatch):
    """The file is named LOCK; the old code read "lock.json" and so always saw
    no lock. And it matched `pgrep -f "autoloop run --continuous"`, which never
    matches a loop started via `autoloop start` — that command calls the run
    path IN-PROCESS, so argv still says start."""
    import os

    from autoloop.dashboard import collect
    from autoloop.lock import LoopLock

    repo = make_repo(tmp_path)
    state = repo / ".autoloop"
    state.mkdir(exist_ok=True)

    # No lock at all -> stopped.
    assert collect(repo)["health"]["label"] == "stopped"

    # A lock held by THIS process is live, whatever any process name says.
    with LoopLock(state):
        health = collect(repo)["health"]
    assert health["label"] == "running"
    assert health["lock_alive"] is True
    assert health["lock_pid"] == str(os.getpid())


def test_a_lock_whose_owner_is_gone_reads_as_stale_not_running(tmp_path):
    """Distinct from both other states: the loop died holding the lock, and
    saying "running" there would hide a crash."""
    import json as _json

    from autoloop.dashboard import collect
    from autoloop.lock import LoopLock

    repo = make_repo(tmp_path)
    state = repo / ".autoloop"
    state.mkdir(exist_ok=True)
    lock = LoopLock(state)
    lock.acquire()
    lock._owned = False  # leave the file behind
    data = _json.loads(lock.path.read_text(encoding="utf-8"))
    data["pid"] = 999_999_999
    lock.path.write_text(_json.dumps(data), encoding="utf-8")

    health = collect(repo)["health"]
    assert health["label"] == "stopped — stale lock"
    assert health["role"] == "critical"


def test_app_tasks_parses_the_format_the_auditor_actually_emits(tmp_path):
    """The parser expected a markdown TABLE row. The auditor emits
    `#### <domain>:<id> — <title>` with severity on the following line, so the
    panel had been silently empty for every report on disk — not just the
    newest one."""
    from autoloop.dashboard import app_tasks

    repo = make_repo(tmp_path)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "AUDIT_2026-08-03.md").write_text(
        "## Confirmed defects (2)\n"
        "#### security_paths:ing-01 — Add a structural validator for bbox pages\n"
        "- severity **medium**, confidence **confirmed**\n"
        "#### db_migrations:db-02 — Comment the downgrade reconstruction\n"
        "- severity **low**, confidence **confirmed**\n",
        encoding="utf-8",
    )

    found = app_tasks(repo)
    assert [t["id"] for t in found] == ["security_paths:ing-01", "db_migrations:db-02"]
    assert found[0]["priority"] == "medium"
    assert "structural validator" in found[0]["title"]


def test_the_retired_table_format_still_renders(tmp_path):
    """An older report on disk must not go blank because the format moved on."""
    from autoloop.dashboard import app_tasks

    repo = make_repo(tmp_path)
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "AUDIT_2026-01-01.md").write_text(
        "| rt-01 | P1 | Guard the destructive downgrade |\n", encoding="utf-8"
    )

    found = app_tasks(repo)
    assert found[0]["id"] == "rt-01"
    assert found[0]["priority"] == "P1"


# ---- live progress for the running task (2026-08-14) -------------------------
#
# "An agent is running" and "an agent is running and has written 400 lines in 12
# minutes" are different facts, and only the second one tells a productive round
# from a wedged one. merge-01 ran twice for 1800s on 2026-08-06 and was killed by
# the timeout both times, having written 591 insertions across 16 files and then
# 532 across 15 — that it was working at all was discoverable only afterwards, by
# hand, in the quarantined worker repo.
#
# Every number must come from GIT IN THE WORKER. The executor's own
# `report_summary`/`report_details` are claims, and these fixtures carry
# deliberately absurd ones so a test fails the moment anything reads them.

_CLAIMED_INSERTIONS = 424242  # numbers no path or sha will coincidentally contain
_CLAIMED_FILES = 31337


def make_worker(tmp_path, name="worker"):
    """A worker repo with one base commit — `(path, base_sha)`."""
    worker = tmp_path / name
    worker.mkdir(parents=True)
    run_git(worker, "init", "-q", "-b", "work")
    run_git(worker, "config", "user.email", "t@e.com")
    run_git(worker, "config", "user.name", "T")
    (worker / "kept.py").write_text("one\ntwo\nthree\n")
    run_git(worker, "add", "kept.py")
    run_git(worker, "commit", "-q", "-m", "base")
    return worker, run_git(worker, "rev-parse", "HEAD").strip()


def executing_state(worker, base_sha, *, started_at, task_id="merge-01"):
    """`state.json` shaped like a loop mid-round, agent claims included."""
    return {
        "phase": "executing",
        "current_task": {"task_id": task_id, "title": "t", "decision": "implement",
                         "started_at": started_at},
        "task_execution": {
            "task_id": task_id,
            "task_branch": f"autoloop/{task_id}",
            "worktree_path": str(worker),
            "task_base_sha": base_sha,
            # What the executor SAID it did. Present on every real record, and
            # never a source for anything below.
            "report_summary": f"wrote {_CLAIMED_INSERTIONS} insertions "
                              f"across {_CLAIMED_FILES} files",
            "report_details": f"{_CLAIMED_INSERTIONS} insertions, {_CLAIMED_FILES} files",
        },
    }


def test_progress_counts_lines_from_git_in_the_worker(tmp_path):
    """Tracked edits AND untracked files, because a brand-new file is where most
    of an agent's output lands — counting only tracked changes would show 0
    lines for a round that wrote hundreds."""
    worker, base = make_worker(tmp_path)
    (worker / "kept.py").write_text("one\nTWO\nthree\nfour\n")   # +2 −1, 1 file
    (worker / "added.py").write_text("a\nb\nc\nd\ne\n")          # +5, 1 file
    dispatched = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)

    progress = worker_progress(
        executing_state(worker, base, started_at=dispatched.isoformat()),
        # Injected so `elapsed_seconds` is an exact number: a live clock puts a
        # 17-digit float in the payload, and the claim-absence assertions below
        # search that payload as text.
        now=dispatched.timestamp() + 60.0,
    )

    assert (progress["insertions"], progress["deletions"], progress["files"]) == (7, 1, 2)
    assert progress["base"] == "task_base_sha", "the figure must span the whole task"
    assert progress["partial"] is False
    # The mutation this guards: sourcing any of it from the agent's own report.
    assert str(_CLAIMED_INSERTIONS) not in json.dumps(progress)
    assert str(_CLAIMED_FILES) not in json.dumps(progress)


def test_an_unreadable_worker_reads_unknown_never_zero_and_never_the_agents_claim(tmp_path):
    """Zero lines means "nothing written yet", which is exactly the alarming
    state — inventing it from a directory we cannot open would be a lie in the
    dangerous direction. And the agent's own figures must not fill the gap: a
    fallback to them is the one change that makes this test fail."""
    worker, base = make_worker(tmp_path)
    state = executing_state(worker, base, started_at=utcnow_iso())
    shutil.rmtree(worker)  # quarantined, moved, or never created

    progress = worker_progress(state)

    assert progress["insertions"] is None
    assert progress["deletions"] is None
    assert progress["files"] is None
    dumped = json.dumps(progress)
    assert str(_CLAIMED_INSERTIONS) not in dumped and str(_CLAIMED_FILES) not in dumped
    assert progress["note"], "unknown must say why it is unknown"
    # Elapsed is independent of the worker: the clock still runs.
    assert progress["elapsed_seconds"] is not None


def test_a_half_read_worker_is_unknown_rather_than_an_understatement(tmp_path, monkeypatch):
    """Both git reads or neither. Reporting the tracked diff alone when the
    untracked listing failed understates the work in exactly the direction that
    matters: an agent whose output is new files would read as idle."""
    import autoloop.dashboard as dash

    worker, base = make_worker(tmp_path)
    (worker / "kept.py").write_text("one\nTWO\nthree\n")
    real = dash._run_checked
    monkeypatch.setattr(
        dash, "_run_checked",
        lambda args, **kw: None if "ls-files" in args else real(args, **kw),
    )

    progress = dash.worker_progress(executing_state(worker, base, started_at=utcnow_iso()))

    assert (progress["insertions"], progress["deletions"], progress["files"]) == (None, None, None)
    assert progress["note"]


def test_an_idle_loop_shows_no_progress_figures_at_all(tmp_path):
    """`state.task_execution` is cleared the moment a candidate is published, so
    its absence is the loop's own statement that nothing is in flight. Figures
    left beside an idle loop read as live activity — the exact confusion this
    panel exists to remove."""
    repo = make_repo(tmp_path)
    worker, _base = make_worker(tmp_path)
    (worker / "added.py").write_text("a\nb\nc\n")  # last round's work, still on disk
    (repo / ".autoloop" / "state.json").write_text(json.dumps({
        "phase": "ready",
        # The finished round is still named here; it must not resurrect figures.
        "current_task": {"task_id": "merge-01", "started_at": utcnow_iso()},
        "task_execution": None,
    }), encoding="utf-8")

    assert collect(repo)["progress"] is None

    # …and the page renders nothing for it: the section ships hidden and the
    # renderer hides it again on a falsy payload.
    assert '<section id="progressbox" style="display:none">' in PAGE
    script = PAGE.split("<script>", 1)[1]
    assert 'if (!p) { box.style.display = "none"' in script


def test_elapsed_is_computed_from_the_dispatch_timestamp(tmp_path):
    """Dispatch, not file mtimes and not the page's own start: the stamp below is
    hours away from anything this test writes, so only the timestamp can produce
    the expected number."""
    worker, base = make_worker(tmp_path)
    dispatched = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    now = dispatched.timestamp() + 1500.0  # the 25-minute round that got killed

    progress = worker_progress(
        executing_state(worker, base, started_at=dispatched.isoformat()), now=now
    )

    assert progress["elapsed_seconds"] == 1500.0
    assert progress["dispatched_at"] == dispatched.isoformat()

    # A `current_task` naming a DIFFERENT task cannot date this round, so elapsed
    # reads unknown — while the lines, which come from git, still render. The 0
    # here is measured (a worker with no changes yet), not invented.
    state = executing_state(worker, base, started_at=dispatched.isoformat())
    state["current_task"]["task_id"] = "someone-else"
    mismatched = worker_progress(state, now=now)
    assert mismatched["elapsed_seconds"] is None
    assert mismatched["insertions"] == 0


def test_reading_progress_writes_nothing_to_the_worker_repo(tmp_path):
    """The dashboard takes no lock and a scheduler may hit it mid-round, so the
    worker repo must be byte-identical across a poll — same property the
    observed checkout already has, now extended to the directory the loop's
    agent is actively writing."""
    repo = make_repo(tmp_path)
    worker, base = make_worker(tmp_path)
    (worker / "added.py").write_text("a\nb\nc\n")
    (repo / ".autoloop" / "state.json").write_text(
        json.dumps(executing_state(worker, base, started_at=utcnow_iso())), encoding="utf-8"
    )
    # Snapshot AFTER the last `run_git` above: it does NOT pass
    # --no-optional-locks and rewrites .git/index, so snapshotting earlier would
    # measure the test's own side effect and blame the tracker.
    before = snapshot(worker)

    payload = collect(repo)

    assert snapshot(worker) == before, "the worker repo must not be written to"
    assert payload["progress"]["files"] == 1


def test_the_page_renders_the_counts_and_the_clock():
    """A payload carrying the numbers is not a page showing them. Every test
    above drives `worker_progress`; `renderProgress` is the display path, and
    deleting an interpolation there leaves all of them green while the operator
    sees nothing."""
    block = PAGE.split("function renderProgress(p){", 1)[1].split("\nfunction ", 1)[0]
    for field in ("p.insertions", "p.deletions", "p.files", "p.elapsed_seconds"):
        assert field in block, f"{field} never reaches the DOM"


def test_the_progress_figures_stay_out_of_the_re_render_signature():
    """Their clock ticks every poll. In the signature, the unchanged-payload
    guard would never fire and the whole DOM would be rebuilt every 2s, losing
    hover state and any text selection — so they are excluded and rendered
    before the guard, exactly as `served_at` is."""
    script = PAGE.split("<script>", 1)[1]
    assert "const {served_at, progress, ...rest} = d;" in script
    body = script.split("function render(d, force){", 1)[1]
    assert body.index("renderProgress(progress);") < body.index("sig === LASTJSON")


def test_the_served_javascript_actually_parses():
    """One syntax error kills the WHOLE script.

    `PAGE` is a plain Python string, so a single `\\n` written inside a JS
    literal is decoded here and splits that literal across two physical lines.
    The browser then throws `SyntaxError: Invalid or unexpected token`, no
    dynamic section renders, and the page shows only its static markup — which
    reads as a dead dashboard rather than a typo. That shipped on 2026-08-04
    and survived every check I ran, because serving valid HTML and a correct
    JSON payload says nothing about whether the script parses.

    So this asks a JS engine instead of inspecting strings.
    """
    import re
    import shutil
    import subprocess
    import tempfile

    from autoloop.dashboard import PAGE

    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment without node
        pytest.skip("node is required to syntax-check the served script")

    scripts = re.findall(r"<script>(.*?)</script>", PAGE, re.S)
    assert scripts, "the page must carry a script block"

    for index, body in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(body)
            path = handle.name
        result = subprocess.run(
            [node, "--check", path], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, (
            f"script block {index} does not parse:\n{result.stderr[:600]}"
        )


# ---- merged vs unmerged (2026-08-06) -----------------------------------------
#
# Seven tasks read `completed` while none of their code was in HEAD — published
# to their side branches and never merged, two of them fixing failures that were
# still happening while their fix sat unused. Nothing on the page said so; it
# took a `git ls-remote` loop to find out. `completed` means PUBLISHED, which is
# not the same as integrated, so the page must show three states and must never
# turn a network hiccup into an alarming fourth.


def merge_fixture(tmp_path):
    """The production shape: an observed checkout, an `origin`, one branch that
    IS an ancestor of HEAD, and one pushed from a SEPARATE clone.

    The separate clone is the point rather than tidiness. Workers commit in
    isolated repos and the publisher pushes from its own, so the observed
    checkout has never seen an unmerged side branch's objects — `merge-base
    --is-ancestor` exits 128 there, not 1, and that is the path all seven real
    tasks took. A fixture that committed the branch locally would exercise the
    easy exit code and prove nothing about the case that shipped.
    """
    repo = make_repo(tmp_path)
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "--bare", "-q", str(origin))
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "origin", "work")

    merged_sha = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "push", "-q", "origin", f"{merged_sha}:refs/heads/autoloop/t-merged")
    (repo / "f.txt").write_text("second\n")
    run_git(repo, "add", "f.txt")
    run_git(repo, "commit", "-q", "-m", "second")

    # Built with init + fetch rather than `git clone`: the bare origin's HEAD is
    # dangling (it was created empty and only ever had `work` pushed into it),
    # and clone's handling of that is a needless dependency for a fixture whose
    # only requirement is that these objects never enter the observed checkout.
    side = tmp_path / "side"
    side.mkdir()
    run_git(side, "init", "-q", "-b", "work")
    run_git(side, "config", "user.email", "t@e.com")
    run_git(side, "config", "user.name", "T")
    run_git(side, "fetch", "-q", str(origin), "work")
    run_git(side, "checkout", "-q", "-B", "work", "FETCH_HEAD")
    (side / "g.txt").write_text("side\n")
    run_git(side, "add", "g.txt")
    run_git(side, "commit", "-q", "-m", "side work")
    unmerged_sha = run_git(side, "rev-parse", "HEAD").strip()
    run_git(side, "push", "-q", str(origin), "HEAD:refs/heads/autoloop/t-unmerged")
    return repo, merged_sha, unmerged_sha


def write_registry(repo, tasks, executions=()):
    (repo / ".autoloop" / "tasks.json").write_text(
        json.dumps({"schema_version": 1, "tasks": tasks}), encoding="utf-8"
    )
    directory = repo / ".autoloop" / "executions"
    directory.mkdir(parents=True, exist_ok=True)
    for record in executions:
        (directory / f"{record['task_id']}.json").write_text(
            json.dumps(record), encoding="utf-8"
        )


def completed(task_id):
    return {"id": task_id, "title": task_id.upper(), "description": "d",
            "status": "completed", "priority": 100}


def by_id(payload):
    return {row["id"]: row for row in payload["merge"]["rows"]}


def test_a_branch_that_is_an_ancestor_of_head_renders_merged(tmp_path):
    repo, merged_sha, _ = merge_fixture(tmp_path)
    write_registry(repo, [completed("t-merged")])

    row = by_id(collect(repo))["t-merged"]
    assert row["state"] == "merged"
    assert row["branch"] == "autoloop/t-merged"
    assert row["sha"] == merged_sha[:12]


def test_a_branch_on_origin_but_not_in_head_renders_unmerged(tmp_path):
    """THE case that hides finished work. Its objects were never fetched into
    this checkout, so git answers with an error rather than a 1 — and an object
    absent from the local database cannot be reachable from local HEAD, which
    is what makes that a definite "no" instead of an unknown."""
    repo, _, unmerged_sha = merge_fixture(tmp_path)
    write_registry(repo, [completed("t-unmerged")])

    row = by_id(collect(repo))["t-unmerged"]
    assert row["state"] == "unmerged"
    assert row["sha"] == unmerged_sha[:12]
    assert "not in HEAD" in row["detail"]


def test_a_completed_task_with_no_branch_renders_not_published(tmp_path):
    """Should not happen — completed in the registry with nothing pushed. Say
    so rather than hiding it in one of the other two states."""
    repo, _, _ = merge_fixture(tmp_path)
    write_registry(repo, [completed("t-ghost")])

    row = by_id(collect(repo))["t-ghost"]
    assert row["state"] == "unpublished"
    assert "never pushed" in row["detail"]


def test_an_unreachable_remote_renders_unknown_never_not_merged(tmp_path):
    """A network hiccup must not invent "seven tasks are stranded". The row
    that IS unmerged when origin answers has to read `unknown` when it does
    not — this fails if the unreachable branch is rendered not-merged."""
    repo, merged_sha, _ = merge_fixture(tmp_path)
    write_registry(
        repo,
        [completed("t-unmerged"), completed("t-merged")],
        [{"task_id": "t-merged", "task_branch": "autoloop/t-merged",
          "candidate_sha": merged_sha}],
    )
    # A path that is not a repository fails instantly; an unroutable URL would
    # sit on the ls-remote timeout and stall the suite instead.
    run_git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    payload = collect(repo)
    rows = by_id(payload)
    assert rows["t-unmerged"]["state"] != "unmerged", "a failed lookup is not evidence"
    assert rows["t-unmerged"]["state"] == "unknown"
    assert payload["merge"]["counts"]["unmerged"] == 0
    assert payload["merge"]["remote_ok"] is False
    # …and a task whose own commit is provably IN HEAD still reads merged: the
    # execution record supplies a commit id, git decides ancestry, and that
    # answer does not need the network.
    assert rows["t-merged"]["state"] == "merged"


def test_the_unmerged_count_matches_the_rows(tmp_path):
    """The count is the sentence this feature exists to say, so it must be
    derived from the rows and must not tally anything git could not answer."""
    repo, _, _ = merge_fixture(tmp_path)
    write_registry(
        repo,
        [completed("t-merged"), completed("t-unmerged"), completed("t-ghost"),
         {"id": "t-open", "title": "OPEN", "description": "d",
          "status": "pending", "priority": 100}],
    )

    payload = collect(repo)
    counts = payload["merge"]["counts"]
    rows = payload["merge"]["rows"]
    assert counts["unmerged"] == sum(1 for r in rows if r["state"] == "unmerged") == 1
    assert counts["merged"] == 1 and counts["unpublished"] == 1
    # Roadmap order (priority, then id), completed rows only: the pending task
    # has no merged-ness to report and must not appear.
    assert [r["id"] for r in rows] == ["t-ghost", "t-merged", "t-unmerged"]


def test_is_ancestor_maps_gits_three_answers(tmp_path):
    """The exit code IS the answer: 0 yes, 1 no, anything else needs triage.
    Driven against a real repository, because the triage is where the risk is
    and an injected stub would never reach it."""
    repo, merged_sha, unmerged_sha = merge_fixture(tmp_path)
    head = run_git(repo, "rev-parse", "HEAD").strip()

    assert is_ancestor(repo, merged_sha, head) == "yes"          # exit 0
    # A commit that IS in this object database and is not an ancestor of HEAD —
    # the plain exit-1 answer.
    run_git(repo, "checkout", "-q", "-b", "detour")
    (repo / "h.txt").write_text("detour\n")
    run_git(repo, "add", "h.txt")
    run_git(repo, "commit", "-q", "-m", "detour")
    local_other = run_git(repo, "rev-parse", "HEAD").strip()
    run_git(repo, "checkout", "-q", "work")
    # Without this the three verdicts below could be measured against a HEAD
    # the checkouts moved, and every one of them would pass for the wrong pair.
    assert run_git(repo, "rev-parse", "HEAD").strip() == head
    assert is_ancestor(repo, local_other, head) == "no"

    # Absent from this object database entirely — git errors out, and the
    # answer is still a definite no.
    assert is_ancestor(repo, unmerged_sha, head) == "no"
    # Nothing to compare against is never a verdict.
    assert is_ancestor(repo, unmerged_sha, "") == "unknown"


def test_the_execution_record_is_positive_evidence_only():
    """The record may supply a commit id; it may never assert a state. Every
    negative has to come from git, because "the record says it was pushed" is
    exactly what read as done for seven unmerged tasks."""
    task = [{"id": "rt-10", "title": "T"}]
    record = {"rt-10": {"task_branch": "autoloop/rt-10", "candidate_sha": "c" * 40,
                        "intended_remote": "origin",
                        "intended_remote_ref": "refs/heads/autoloop/rt-10"}}

    # A pushed-looking record whose commit is not in HEAD, remote readable.
    rows = merge_states(task, record, True, {}, lambda sha: "no")
    assert rows[0]["state"] == "unpublished"
    # Same record, remote unreadable -> unknown, never a negative.
    rows = merge_states(task, record, False, {}, lambda sha: "unknown")
    assert rows[0]["state"] == "unknown"
    # Same record, but git says the commit IS in HEAD -> merged even though
    # origin no longer carries the branch (merged then deleted is ordinary).
    rows = merge_states(task, record, True, {}, lambda sha: "yes")
    assert rows[0]["state"] == "merged"
    # The branch name comes from the record's own ref, not a guess.
    assert rows[0]["branch"] == "autoloop/rt-10"


def test_a_task_with_no_execution_record_still_gets_a_branch_looked_up():
    """The naming convention is allowed to FIND a branch. It is never allowed
    to decide anything about it — the state below comes from ancestry."""
    rows = merge_states([{"id": "rt-11", "title": "T"}], {}, True,
                        {"autoloop/rt-11": "a" * 40}, lambda sha: "no")
    assert rows[0]["branch"] == "autoloop/rt-11"
    assert rows[0]["state"] == "unmerged"


def test_the_unmerged_count_is_a_stat_tile_not_only_a_table_row():
    """The sentence this feature exists to say is `seven finished tasks are not
    in your branch`, and a number nobody scrolls to does not say it. The tile
    grid renders above the pipeline, before every other section."""
    tiles = PAGE.split('getElementById("tiles").innerHTML = [', 1)[1].split("].map", 1)[0]
    assert "not merged" in tiles and "nUnmerged" in tiles
    assert PAGE.index('id="tiles"') < PAGE.index('id="merged"')
    # Icon + word beside the number, like every other state on the page: the
    # warn border must not be the only thing carrying the meaning.
    merge_marks = PAGE.split("const MS = {", 1)[1].split("};", 1)[0]
    for state in ("merged", "unmerged", "unpublished", "unknown"):
        assert f"{state}:[" in merge_marks.replace(" ", "")


# ---- the merge panel, grouped by state (2026-08-19) ---------------------------
#
# Measured 2026-08-18: `merge.rows` carried 61 rows and `merge.counts` already
# said {merged: 57, unpublished: 3, unmerged: 1, unknown: 0}. The panel rendered
# all 61 flat, so the FOUR rows that needed a human sat under 57 that did not,
# and the counts were computed and then thrown away as a display axis.
#
# The fix is the roadmap panel's own mechanism one section down — an ordered
# group list carrying the count and an explicit `collapsed` flag, with the
# hide/show policy in the PAYLOAD rather than the template — not a second one.
# Two properties carry it, and both are asserted rather than described:
#
# * COLLAPSED IS NOT HIDDEN. All four groups render with their counts, including
#   `Unknown (0)`: an unknown row means the sweep could not judge a branch,
#   which is the state most worth seeing and the easiest to hide by accident.
# * An opened disclosure SURVIVES the 2s poll. The <details> is static markup
#   the render never rebuilds and never writes `.open` to — and that negative is
#   the load-bearing half, because a `box.open = !g.collapsed` on every tick
#   would pass every structural check in this file and snap the panel shut every
#   two seconds.


def four_state_rows():
    """One completed task in each of three states, built by `merge_states`
    itself so these tests exercise the rows the page really renders.

    `unknown` is deliberately left at zero: that is the ordinary reading (a
    reachable origin answers every branch) and it is exactly the empty-but-
    actionable group the panel must still render with its count.
    """
    refs = {"autoloop/t-merged": "a" * 40, "autoloop/t-unmerged": "b" * 40}
    verdict = {"a" * 40: "yes", "b" * 40: "no"}
    return merge_states(
        [{"id": "t-merged", "title": "M"}, {"id": "t-unmerged", "title": "U"},
         {"id": "t-ghost", "title": "G"}],
        {}, True, refs, lambda sha: verdict.get(sha, "unknown"),
    )


def merge_groups_by_key(groups):
    return {group["key"]: group for group in groups}


def test_the_merge_panel_groups_every_state_exactly_once():
    """`MERGE_STATES` is the vocabulary and `MERGE_GROUPS` is the display order,
    so a fifth state added to one and not the other would render nowhere — the
    same guarantee `test_the_groups_come_from_state_of_and_never_from_the_status_string`
    makes for the roadmap. The order is triage order: what needs a human first,
    what nobody has to act on last."""
    keys = [key for key, _label, _collapsed in MERGE_GROUPS]

    assert keys == ["unmerged", "unpublished", "unknown", "merged"]
    assert set(keys) == set(MERGE_STATES)
    assert len(keys) == len(set(keys)) == len(MERGE_STATES)
    # Exactly one collapsed group, and it is the one nobody has to act on.
    assert [key for key, _l, collapsed in MERGE_GROUPS if collapsed] == ["merged"]
    # One group per state in the built payload too, in the same order.
    assert [g["key"] for g in merge_groups(four_state_rows())] == keys


def test_only_the_merged_group_is_collapsed_and_it_still_carries_its_count():
    """The claim the panel exists to make. `merged` is the group that only ever
    grows and that nobody has to act on, so it is a counted disclosure; the
    other three are the ones a human acts on and render open."""
    groups = merge_groups_by_key(merge_groups(four_state_rows()))

    assert groups["merged"]["collapsed"] is True
    assert groups["merged"]["count"] == 1
    for key in ("unmerged", "unpublished", "unknown"):
        assert groups[key]["collapsed"] is False, f"{key} needs a human — never collapsed"
    # No `hidden` field anywhere: the roadmap has one because it has a hide
    # policy, and this panel's rule is that it has none. An always-false flag is
    # an invitation to flip it.
    for group in groups.values():
        assert "hidden" not in group


def test_a_merge_group_with_no_rows_still_renders_with_its_count():
    """`unknown (0)` is the group most worth seeing and the easiest to hide by
    accident: a row lands there when the sweep could not judge a branch at all.
    An empty group that vanished would make "every branch was judged" and "that
    section failed to render" look identical."""
    groups = merge_groups_by_key(merge_groups(four_state_rows()))

    assert groups["unknown"]["count"] == 0 and groups["unknown"]["rows"] == []
    assert groups["unknown"]["collapsed"] is False
    # …and an empty panel is still four groups, not zero.
    assert [g["count"] for g in merge_groups([])] == [0, 0, 0, 0]


def test_grouping_is_display_only_and_changes_no_row_and_no_count(tmp_path):
    """Display only, said as a property rather than as a promise: the flat
    `rows` list and the `counts` dict are what they were, every row object
    appears in exactly one group unaltered, and no row is dropped by grouping."""
    repo, _, _ = merge_fixture(tmp_path)
    write_registry(repo, [completed("t-merged"), completed("t-unmerged"),
                          completed("t-ghost")])

    merge = collect(repo)["merge"]
    rows = merge["rows"]

    # The flat list is untouched — same roadmap order, same six fields.
    assert [r["id"] for r in rows] == ["t-ghost", "t-merged", "t-unmerged"]
    for row in rows:
        assert set(row) == {"id", "title", "branch", "sha", "state", "detail"}
    # Every row in exactly one group, unaltered, and nothing invented.
    grouped = [row for group in merge["groups"] for row in group["rows"]]
    assert sorted(grouped, key=lambda r: r["id"]) == sorted(rows, key=lambda r: r["id"])
    assert sum(g["count"] for g in merge["groups"]) == len(rows)
    # …and each group's count agrees with the per-state count already published.
    for group in merge["groups"]:
        assert group["count"] == merge["counts"][group["key"]]
        assert all(r["state"] == group["key"] for r in group["rows"])
    # The base branch and base head — what "merged" is relative to — are still
    # in the payload, outside every group.
    assert merge["base_branch"] and merge["base_head"]


def test_the_merged_disclosure_is_static_markup_the_refresh_never_reopens():
    """The bound this change is easiest to break silently. The <details> lives
    in static markup, so a poll cannot rebuild it — and `renderMerge` writes the
    summary text and the rows and NEVER `.open`, because writing the payload's
    default back each tick would snap an opened panel shut every two seconds.

    The negative is the load-bearing half: the positive alone passes for a
    render that reopens the box on every poll."""
    static_markup, script = PAGE.split("<script>", 1)

    assert '<details id="mgmergedbox">' in static_markup
    assert 'id="mgmergedsum"' in static_markup and 'id="mgmergedrows"' in static_markup
    body = script.split("function renderMerge(d){", 1)[1].split("\n}", 1)[0]
    assert ".open" not in body, "a render that writes .open snaps the panel shut every 2s"
    assert "mgmergedbox" not in body, "the disclosure element itself is never rebuilt"
    # By KEY, never "the group carrying the collapsed flag" — that predicate is
    # wrong the moment a second collapsed group appears, and silently so.
    assert 'groups.find(g => g.key === "merged")' in body
    # The base-branch line renders ABOVE the groups and outside all of them.
    assert static_markup.index('id="mergehead"') < static_markup.index('id="merged"')
    assert 'getElementById("mergehead")' in body


def merge_panel_js() -> str:
    """The merge panel's own code, lifted verbatim out of the served page.

    `esc` and `rows` come along because every helper in the region depends on
    them; the region carries no other module state, which is what lets it run
    against a stub document instead of a browser.
    """
    script = PAGE.split("<script>", 1)[1]
    lines = script.splitlines()
    esc_line = next(line for line in lines if line.startswith("const esc ="))
    rows_line = next(line for line in lines if line.startswith("const rows ="))
    region = script.split("// MERGE_PANEL_START", 1)[1].split("// MERGE_PANEL_END", 1)[0]
    return "\n".join((esc_line, rows_line, region))


def test_the_page_renders_every_merge_group_and_keeps_an_opened_one_open():
    """A payload carrying the groups is not a page showing them, and a page
    showing them is not one that survives the poll — so this RUNS the panel's
    own render twice against a stub document, with the disclosure opened in
    between, exactly as an operator would.

    A structural check cannot see the second failure: `renderMerge` is called on
    every payload change, so a line that reset the box would leave every string
    assertion above green and the panel unusable.
    """
    groups = merge_groups(four_state_rows())
    payload = json.dumps({"merge": {
        "base_branch": "autoloop/mainline", "base_head": "abc123def456",
        "counts": {group["key"]: group["count"] for group in groups},
        "rows": [row for group in groups for row in group["rows"]],
        "groups": groups,
    }})

    harness = merge_panel_js() + """
const NODES = {};
for (const id of ["mergehead", "merged", "mgmergedsum", "mgmergedrows", "mgmergedbox"])
  NODES[id] = {innerHTML: "", textContent: "", open: false};
const document = {getElementById: id => NODES[id]};
const PAYLOAD = __PAYLOAD__;
renderMerge(PAYLOAD);
const firstSummary = NODES.mgmergedsum.textContent;
// The operator opens the merged group, then the 2s poll fires again.
NODES.mgmergedbox.open = true;
renderMerge(PAYLOAD);
console.log(JSON.stringify({
  open: NODES.mgmergedbox.open,
  summary: NODES.mgmergedsum.textContent,
  firstSummary: firstSummary,
  inline: NODES.merged.innerHTML,
  collapsedRows: NODES.mgmergedrows.innerHTML,
  head: NODES.mergehead.innerHTML,
}));
""".replace("__PAYLOAD__", payload)
    out = json.loads(run_js(harness))

    # The disclosure the operator opened is still open after the refresh.
    assert out["open"] is True
    assert out["summary"] == out["firstSummary"] == "✓ Merged (1)"
    # Every actionable group renders, LABEL AND COUNT TOGETHER — the empty one
    # included, because `Unknown (0)` is the answer rather than the absence of
    # one. The count is asserted inside the heading, so a group that rendered
    # its word and dropped its number fails here.
    for heading in ('▲ NOT merged <span class="gc">1</span>',
                    '○ Not published <span class="gc">1</span>',
                    '? Unknown <span class="gc">0</span>'):
        assert heading in out["inline"], f"{heading} never reaches the DOM"
    assert '<p class="empty">none</p>' in out["inline"], "an empty group says so"
    # The collapsed group's rows are behind the disclosure, not in the inline
    # list: 57 merged rows above the four that matter is the fault being fixed.
    assert "<code>t-merged</code>" in out["collapsedRows"]
    assert "<code>t-merged</code>" not in out["inline"]
    assert "<code>t-unmerged</code>" in out["inline"]
    assert "<code>t-ghost</code>" in out["inline"]
    assert "✓ Merged" not in out["inline"], "the collapsed group has no inline heading"
    # Row content is what it was before the grouping: the same five columns,
    # icon + word, and `row-active` on the row that needs a human.
    assert "<th>task</th>" in out["inline"] and "<th>why</th>" in out["inline"]
    assert '<tr class="row-active">' in out["inline"]
    assert "▲ NOT merged" in out["inline"] and "✓ merged" in out["collapsedRows"]
    # The base branch and base head stay visible above the groups.
    assert "autoloop/mainline" in out["head"] and "abc123def456" in out["head"]


# ---- the roadmap, grouped by state (2026-08-15) -------------------------------
#
# One flat list of 20+ tasks answers none of the three questions an operator has
# — what is moving, what needs me, what is next. The grouping is only as good as
# its source, so the load-bearing property is that every group comes from
# `TaskRegistry.state_of()`: `pending` on disk covers BOTH Ready and Blocked (the
# difference is derived from dependencies and never stored), and a stored
# `blocked` means QUARANTINED, which is the opposite kind of problem from waiting
# on a dependency. A page reading the status string would be wrong in both
# directions at once.
#
# These drive `task_groups` directly where a registry is all that is needed (no
# git, no repo) and `collect()` where the wiring itself is the point. The new
# rendering lives in the one <script> block
# `test_the_served_javascript_actually_parses` extracts and runs `node --check`
# over, so a broken escape in it fails there rather than needing a second parse
# test.


def roadmap_task(task_id, **over):
    """A `tasks.json` row carrying every field the registry needs, so each test
    below states only what it is actually about."""
    row = {"id": task_id, "title": task_id.upper(), "description": "d",
           "status": "pending", "priority": 100, "depends_on": []}
    row.update(over)
    return row


def groups_by_key(groups):
    return {g["key"]: g for g in groups}


def test_the_in_progress_group_carries_the_candidate_and_the_review_round(tmp_path):
    """The group an operator acts on, so it must say WHICH commit is under
    review and which round it is on. Both come from the execution record — the
    loop's own bookkeeping — and this drives `collect()` end to end because
    reading those records is half the feature."""
    repo = make_repo(tmp_path)
    write_registry(
        repo,
        [roadmap_task("wip-1", status="in_progress"),
         roadmap_task("wip-2", status="in_progress", priority=200)],
        [{"task_id": "wip-1", "candidate_sha": "a" * 40, "review_round": 2}],
    )

    group = groups_by_key(collect(repo)["groups"])["in_progress"]

    assert group["count"] == 2
    assert [t["id"] for t in group["tasks"]] == ["wip-1", "wip-2"]
    assert group["tasks"][0]["detail"] == f"candidate {'a' * 12} · review round 2"
    # A task dispatched moments ago has genuinely committed nothing yet. Saying
    # so beats a blank cell, which reads as a missing feature.
    assert group["tasks"][1]["detail"] == "no candidate committed yet"


def test_the_needs_a_human_group_shows_the_blocker_reason():
    """`blocked` on disk is a quarantine: it never resolves itself, so the
    reason is the whole content of the row — it is what the operator has to
    answer. And it must not land in the Blocked group, which means something
    that WILL resolve itself."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("q-1", status="blocked",
                     blocked_reason="approved_paths refused: docs/"),
    ]}, {}))

    assert groups["needs_human"]["count"] == 1
    assert groups["needs_human"]["tasks"][0]["detail"] == "approved_paths refused: docs/"
    assert groups["blocked"]["count"] == 0


def test_the_ready_group_holds_pending_tasks_whose_dependencies_are_satisfied():
    """Ready is what `next_ready()` would actually pick, which is not a stored
    field: `r-1` and `b-1` carry the same `pending` status and only one of them
    can run."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("done-1", status="completed"),
        roadmap_task("r-1", depends_on=["done-1"]),
        roadmap_task("b-1", depends_on=["r-1"]),
    ]}, {}))

    assert [t["id"] for t in groups["ready"]["tasks"]] == ["r-1"]
    assert groups["ready"]["tasks"][0]["detail"] == "next to be dispatched"
    assert [t["id"] for t in groups["blocked"]["tasks"]] == ["b-1"]


def test_the_blocked_group_names_the_dependency_it_is_waiting_on():
    """Saying blocked without saying by what is not actionable. Only the
    INCOMPLETE dependencies are named — a completed one is not why anything is
    still waiting."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("done-1", status="completed"),
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("b-1", depends_on=["done-1", "wip-1"]),
    ]}, {}))

    group = groups["blocked"]
    assert [t["id"] for t in group["tasks"]] == ["b-1"]
    assert group["tasks"][0]["detail"] == "waiting on wip-1"


def test_the_done_group_is_counted_and_collapsed_by_default():
    """Done only ever grows, so it is a counted disclosure rather than 40 rows
    between the operator and the four that matter. It is never `hidden`: the
    count is the point, and `Done (0)` costs one line."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task(f"d-{i}", status="completed") for i in range(3)
    ]}, {}))

    group = groups["done"]
    assert group["count"] == 3
    assert group["collapsed"] is True and group["hidden"] is False
    # The page ships it closed (a <details> with no `open` attribute) and puts
    # the count in the summary, which is the only part of it always on screen.
    assert '<details id="donebox">' in PAGE
    assert "Done (${gDone.count})" in PAGE
    # Completed is PUBLISHED, not merged — said on the page, since the two are
    # exactly what looked identical when seven finished tasks sat unmerged.
    assert "published, not merged into this branch" in PAGE


def test_needs_a_human_renders_explicitly_when_empty_and_other_groups_do_not():
    """Silence and "nothing needs you" must not look identical: a hidden group
    is indistinguishable from a section that failed to render, and this is the
    one group whose emptiness IS the answer."""
    groups = groups_by_key(task_groups({"tasks": [roadmap_task("r-1")]}, {}))

    assert groups["needs_human"]["count"] == 0
    assert groups["needs_human"]["hidden"] is False
    for key in ("in_progress", "blocked"):
        assert groups[key]["count"] == 0
        assert groups[key]["hidden"] is True, f"an empty {key} may be dropped"

    # …and the page prints the word: it drops only what the backend marked
    # hidden, and `rows()` renders "none" for an empty body.
    script = PAGE.split("<script>", 1)[1]
    assert "groups.filter(g => !g.collapsed && !g.hidden)" in script
    assert '<p class="empty">none</p>' in script


def test_the_ready_group_is_in_next_ready_order():
    """The order shown must be the order the loop picks, or the panel is
    telling the operator something the code will not do.

    `_ready_order` cannot simulate `next_ready()` — replaying picks means
    COMPLETING tasks, and this page may not call a mutating registry method —
    so it repeats that method's `(priority, id)` key. This is what stops the
    repetition drifting: the real `next_ready()` / `mark_completed()` loop runs
    here, on a registry of its own, and the sequences must be equal.

    The fixture is built so the comparison can be full equality: insertion
    order differs from priority order, `r-a`/`r-b` tie so the id tiebreak is
    exercised, and the blocked task waits on the IN-PROGRESS one — completing
    the ready set can therefore never add a new pick mid-loop.
    """
    rows = [
        roadmap_task("r-c", priority=5),
        roadmap_task("r-b", priority=1),
        roadmap_task("r-a", priority=1),
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("b-1", depends_on=["wip-1"]),
    ]
    shown = [t["id"] for t in groups_by_key(task_groups({"tasks": rows}, {}))["ready"]["tasks"]]

    registry = TaskRegistry.from_dict({"tasks": [dict(row) for row in rows]})
    picks = []
    while (nxt := registry.next_ready()) is not None:
        picks.append(nxt.id)
        registry.mark_completed(nxt.id)

    assert picks == ["r-a", "r-b", "r-c"], "the fixture must exercise both keys"
    assert shown == picks


def test_the_groups_come_from_state_of_and_never_from_the_status_string():
    """The invariant the whole panel rests on. `status` cannot answer this:
    `pending` covers Ready and Blocked alike, and `blocked` means quarantined —
    so a page reading it would be wrong in both directions at once, sending an
    operator to answer a blocker that does not exist while a task that really
    is waiting for them reads as merely queued."""
    payload = task_groups({"tasks": [
        roadmap_task("open-1"),
        roadmap_task("waiting-1", depends_on=["open-1"]),
        roadmap_task("quarantined-1", status="blocked", blocked_reason="answer me"),
    ]}, {})
    groups = groups_by_key(payload)

    # Two tasks, the same stored status, two different groups.
    assert [t["id"] for t in groups["ready"]["tasks"]] == ["open-1"]
    assert [t["id"] for t in groups["blocked"]["tasks"]] == ["waiting-1"]
    # And the one whose stored status IS "blocked" is in neither.
    assert [t["id"] for t in groups["needs_human"]["tasks"]] == ["quarantined-1"]

    # Every TaskState is claimed by exactly one group, so a state added to the
    # registry cannot silently render nowhere.
    assert [g["state"] for g in payload] == [state.value for _key, _label, state in GROUPS]
    assert {g["state"] for g in payload} == {state.value for state in TaskState}


def test_a_retired_task_is_grouped_apart_from_both_kinds_of_blocked():
    """The whole point. Three tasks, three different "not running" states, and
    the operator's question differs for each: `waiting-1` needs nothing,
    `quarantined-1` needs them, `brw-02` needs nobody — it already shipped
    under brw-06. Folding the third into either of the first two is what made
    the blocked count worthless as a call to action."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("open-1"),
        roadmap_task("waiting-1", depends_on=["open-1"]),
        roadmap_task("quarantined-1", status="blocked", blocked_reason="answer me"),
        roadmap_task("brw-02", status="retired", superseded_by=["brw-06"],
                     blocked_reason="superseded by brw-06"),
    ]}, {}))

    assert [t["id"] for t in groups["retired"]["tasks"]] == ["brw-02"]
    assert [t["id"] for t in groups["blocked"]["tasks"]] == ["waiting-1"]
    assert [t["id"] for t in groups["needs_human"]["tasks"]] == ["quarantined-1"]


def test_the_retired_group_names_the_successor_rather_than_the_prose():
    """`superseded_by` is why this state exists: the chain has to be readable
    without parsing a sentence. The successor list also reaches the payload, so
    a consumer of /data.json can follow it."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("brw-06", status="retired", superseded_by=["brw-07", "brw-08"],
                     blocked_reason="split at the reviewer's request"),
    ]}, {}))

    row = groups["retired"]["tasks"][0]
    assert row["detail"] == "superseded by brw-07, brw-08"
    assert row["superseded_by"] == ["brw-07", "brw-08"]


def test_a_retirement_with_no_successor_falls_back_to_its_reason():
    """`dash-01` went stale rather than being replaced, so the reason is the
    only account there is — and a blank cell reads as a broken panel."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("dash-01", status="retired",
                     blocked_reason="stale since 2026-08-03: no candidate, no record"),
        roadmap_task("bare-1", status="retired"),
    ]}, {}))

    details = {t["id"]: t["detail"] for t in groups["retired"]["tasks"]}
    assert details["dash-01"] == "stale since 2026-08-03: no candidate, no record"
    assert details["bare-1"] == "retired; no successor recorded"


def test_the_retired_group_is_counted_and_collapsed_in_its_own_disclosure():
    """Two collapsed groups now, so each must be looked up by KEY.
    `groups.find(g => g.collapsed)` would hand both disclosures the same group
    and quietly fill the Done box with retirements."""
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("r-1", status="retired", superseded_by=["r-2"]),
        roadmap_task("d-1", status="completed"),
    ]}, {}))

    assert groups["retired"]["collapsed"] is True and groups["retired"]["hidden"] is False
    assert groups["retired"]["count"] == 1
    static_markup, script = PAGE.split("<script>", 1)
    assert '<details id="retiredbox">' in static_markup and 'id="retiredsum"' in static_markup
    assert "groups.find(g => g.collapsed)" not in script, \
        "with two collapsed groups this predicate returns the wrong one"
    assert 'byKey("retired")' in script and 'byKey("done")' in script
    assert "Retired (${gRetired.count})" in script
    assert "t.superseded_by" in script, "the successor column never reaches the DOM"


def test_the_migrated_retirements_reach_the_page_from_a_pre_state_task_file(tmp_path):
    """End to end on the shape actually on disk: `tasks.json` still says
    `blocked` with the successor in free text. The migration runs when the
    registry LOADS, so the page shows the retirement before the loop has
    written anything — and the flat roadmap row agrees with the group, or the
    app-task panel would mark brw-02 "blocked" two panels below."""
    repo = make_repo(tmp_path)
    write_registry(repo, [
        roadmap_task("brw-02", status="blocked", blocked_reason="superseded by brw-06"),
        roadmap_task("audit-0003", status="blocked", blocked_reason="failed its validation"),
    ])

    payload = collect(repo)
    groups = groups_by_key(payload["groups"])

    assert [t["id"] for t in groups["retired"]["tasks"]] == ["brw-02"]
    assert groups["retired"]["tasks"][0]["detail"] == "superseded by brw-06"
    # The genuine failure still asks for a human.
    assert [t["id"] for t in groups["needs_human"]["tasks"]] == ["audit-0003"]
    statuses = {r["id"]: r["status"] for r in payload["roadmap"]}
    assert statuses == {"brw-02": "retired", "audit-0003": "blocked"}


def test_an_unreadable_task_graph_says_so_rather_than_rendering_empty_groups():
    """A roadmap with no tasks renders every group zeroed, so an empty list can
    only mean the graph itself did not load — and "no tasks" and "unreadable"
    call for opposite reactions. A dependency naming a task that does not exist
    is the real shape of this: `from_dict` accepts it and `state_of` then
    raises, which must not take the page down."""
    assert task_groups({"tasks": []}, {}) != []
    assert task_groups({"tasks": [roadmap_task("a", depends_on=["ghost"])]}, {}) == []
    assert task_groups({"tasks": [{"id": "a", "surprise": 1}]}, {}) == []

    script = PAGE.split("<script>", 1)[1]
    assert "the task graph could not be read" in script


def test_the_page_renders_every_group_with_its_heading_and_count():
    """A payload carrying the groups is not a page showing them — the lesson
    `renderProgress` already recorded. Deleting an interpolation here leaves
    every test above green while the operator sees nothing."""
    static_markup, script = PAGE.split("<script>", 1)
    assert '<div id="roadmap" class="scroll"></div>' in static_markup
    assert '<details id="donebox">' in static_markup and 'id="donesum"' in static_markup
    for field in ("g.label", "g.count", "t.detail", "t.priority"):
        assert field in script, f"{field} never reaches the DOM"

    # The per-render Save binding is scoped to #roadmap, so a priority input
    # under #done would render with no listener — and re-prioritising a
    # finished task means nothing anyway.
    assert 'querySelectorAll("#roadmap button.save")' in script
    assert "priCell" not in script.split('getElementById("done").innerHTML', 1)[1]


# ---- the summary at the top (2026-08-16) --------------------------------------
#
# The page listed tasks and answered none of the three questions an operator
# arrives with: how much is done, how much is moving, is the queue converging.
# Counting it by hand on 2026-08-06 took a script — 66 tasks, 17 completed, 23
# in progress, 18 pending, 8 blocked — and the flat list is what shipped.
#
# Two properties carry the feature, and both are asserted below rather than
# described:
#
# * every TASK-STATE count is derived from `TaskRegistry.state_of()` (through
#   the same `groups` payload the Roadmap panel renders), one count per state
#   under the state's own name, so the summary cannot disagree with what
#   actually dispatches — and no word up there can mean a different state down
#   here; and
# * an unreachable remote reads UNKNOWN, never "not published". That is the
#   mutation test: twelve unpublished candidates is a real alarm, and inventing
#   it out of a network failure would make the number worthless.
#
# The in-progress publication breakdown is deliberately NOT claimed to come from
# `state_of()`: a registry knows a task is IN_PROGRESS and cannot know where its
# commit went. It reads the execution record's `candidate_sha` against the
# cached `ls-remote`, which is exactly why `unknown` is one of its states.


def stats_for(rows, executions=None, remote_ok=True, refs=None, remote_name="origin"):
    """`roadmap_stats` wired exactly as `collect()` wires it: the groups come
    from `task_groups`, so these tests exercise the derivation the page renders
    rather than a parallel one built for the test."""
    executions = executions or {}
    groups = task_groups({"tasks": rows}, executions)
    return roadmap_stats(groups, executions, remote_ok, refs or {}, remote_name)


def test_the_counts_are_what_state_of_reports_and_nothing_else():
    """The load-bearing property. `status` cannot answer this: stored `pending`
    covers Ready and Blocked alike and stored `blocked` means quarantined, so
    the counts are compared against `state_of()` run directly on the same rows —
    one count per state, keyed by `TaskState.value`."""
    rows = [
        roadmap_task("done-1", status="completed"),
        roadmap_task("done-2", status="completed"),
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("ready-1"),
        roadmap_task("dep-1", depends_on=["wip-1"]),
        roadmap_task("q-1", status="blocked", blocked_reason="answer me"),
        roadmap_task("rt-1", status="retired", superseded_by=["done-1"]),
    ]
    registry = TaskRegistry.from_dict({"tasks": [dict(row) for row in rows]})
    seen = {state: 0 for state in TaskState}
    for task in registry.all_tasks():
        seen[registry.state_of(task.id)] += 1

    stats = stats_for(rows)

    assert stats["total"] == len(rows) == 7
    assert stats["counts"] == {state.value: seen[state] for state in TaskState}
    # Spelled out too, so a bug that moved a task between states consistently in
    # both halves of the comparison above still fails here. Note what the two
    # `blocked*` counts are: dep-1 waits on wip-1 (resolves itself), q-1 waits
    # on an operator. Folding them — or spending the name `blocked` on the
    # quarantine, as the first version of this summary did — is the regression.
    assert stats["counts"] == {"completed": 2, "in_progress": 1, "ready": 1,
                               "blocked": 1, "blocked_by_operator": 1, "retired": 1}
    # And the one-line summary says the same thing in `TaskRegistry.summary()`'s
    # own words, so the page and the review packet cannot report two roadmaps.
    # Asserted as an EXACT string because the vocabulary is the point, not the
    # arithmetic: this sentence is copied from `TaskRegistry.summary()` (minus
    # its priority-1 breakdown and `next ready:` tail, which are dispatch advice
    # rather than counts). If this fails, the fix is to change both or neither —
    # `summary()` is the authority, this is the copy.
    assert stats["line"] == ("7 tasks: 2 completed, 1 in progress, 1 ready, "
                             "1 blocked, 1 quarantined, 1 retired")


def test_every_task_state_is_claimed_by_exactly_one_bucket():
    """A state added to the registry must not vanish from the summary, and no
    state may be counted twice. The buckets name a GROUP KEY, and `GROUPS` pins
    each key to its state, so this asks whether the six buckets are a bijection
    onto the six `TaskState`s — and whether each bucket's own count key is that
    state's value, which is what stops a name drifting off its state."""
    key_to_state = {key: state for key, _label, state in GROUPS}
    claimed = [key_to_state[group_key] for _name, _label, group_key in STAT_BUCKETS]

    assert len(claimed) == len(set(claimed)), "a state is counted in two buckets"
    assert {state.value for state in claimed} == {state.value for state in TaskState}
    for (name, _label, group_key), state in zip(STAT_BUCKETS, claimed):
        assert name == state.value, f"count key {name!r} is not {state.value!r}"
        assert key_to_state[group_key] is state


def test_no_word_in_the_summary_names_two_different_states():
    """The revision that produced this test. The first version rolled READY ∪
    BLOCKED into `pending` and spent the freed name on BLOCKED_BY_OPERATOR, so
    `blocked` meant the quarantine in the top summary and "waiting on a
    dependency" in the Roadmap panel eight inches below — on the same page, in
    the same operator's eye, with opposite calls to action (answer a blocker vs
    wait for a dependency).

    So: every tile carries the label the page renders, and the mapping from
    label to state is asserted here rather than trusted to the template."""
    rows = [
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("ready-1"),
        roadmap_task("dep-1", depends_on=["wip-1"]),
        roadmap_task("q-1", status="blocked", blocked_reason="answer me"),
    ]
    groups = task_groups({"tasks": rows}, {})
    stats = roadmap_stats(groups, {}, True, {})
    group_labels = {g["key"]: g["label"] for g in groups}
    group_counts = {g["state"]: g["count"] for g in groups}

    tiles = {tile["state"]: tile for tile in stats["tiles"]}
    assert [tile["state"] for tile in stats["tiles"]] == [
        name for name, _label, _key in STAT_BUCKETS
    ]
    # Each tile counts exactly the group it names, so the number beside a label
    # is the number of rows listed under that label below.
    for tile in stats["tiles"]:
        assert tile["count"] == stats["counts"][tile["state"]]
        assert tile["count"] == group_counts[tile["state"]]

    # The quarantine borrows the Roadmap group's own words and never the word
    # `blocked`; the dependency wait keeps `blocked` and says what it waits on.
    quarantine = tiles[TaskState.BLOCKED_BY_OPERATOR.value]
    assert quarantine["label"] == group_labels["needs_human"].lower() == "needs a human"
    assert "blocked" not in quarantine["label"]
    assert quarantine["count"] == 1

    dependency = tiles[TaskState.BLOCKED.value]
    assert dependency["label"].startswith("blocked") and "dependenc" in dependency["label"]
    assert dependency["count"] == 1

    # Ready is its own count rather than half of a `pending` bucket: it is the
    # one state that is dispatchable right now, which is the difference the
    # roll-up erased.
    assert tiles[TaskState.READY.value]["count"] == 1
    # And the open figure is exactly the four non-terminal states, so the two
    # halves of the summary cannot describe different sets of tasks.
    assert stats["open"] == sum(
        stats["counts"][state] for state in
        ("in_progress", "ready", "blocked", "blocked_by_operator")
    ) == 4


def test_retired_tasks_leave_the_percentage_denominator_on_both_sides():
    """A retirement was deliberately dropped. Counting it as outstanding
    understates progress; counting it as done overstates it. Neither reading is
    allowed, so it comes out of the fraction entirely — and the denominator
    ships with the percentage so the exclusion is visible rather than assumed."""
    rows = [
        roadmap_task("d-1", status="completed"),
        roadmap_task("o-1"),
        roadmap_task("rt-1", status="retired", superseded_by=["d-1"]),
    ]

    stats = stats_for(rows)

    assert stats["total"] == 3 and stats["counts"]["retired"] == 1
    assert stats["open"] == 1 and stats["denominator"] == 2
    assert stats["denominator"] == stats["counts"]["completed"] + stats["open"]
    assert stats["denominator"] == stats["total"] - stats["counts"]["retired"]
    assert stats["percent_done"] == 50.0
    # Retired-as-outstanding would read 33.3; retired-as-done would read 66.7.
    assert stats["percent_done"] not in (round(100 / 3, 1), round(200 / 3, 1))


def test_a_roadmap_with_nothing_to_divide_by_reports_no_percentage():
    """0% is a verdict. With every task retired there is nothing to be a
    fraction of, so the figure is absent rather than an alarming zero."""
    stats = stats_for([roadmap_task("rt-1", status="retired")])

    assert stats["denominator"] == 0
    assert stats["percent_done"] is None


def test_each_in_progress_sub_category_is_classified_including_a_published_one():
    """`in progress: 23` hides three quite different situations. A published
    candidate is retireable (B10 should have completed it); an unpublished one
    pins a task_base_sha and is a task_base_behind_head park waiting to happen;
    no candidate at all is a timeout, a refusal or an abandoned round."""
    rows = [roadmap_task(f"wip-{n}", status="in_progress") for n in (1, 2, 3)]
    executions = {
        "wip-1": {"task_id": "wip-1", "task_branch": "autoloop/wip-1",
                  "candidate_sha": "a" * 40},
        "wip-2": {"task_id": "wip-2", "task_branch": "autoloop/wip-2",
                  "candidate_sha": "b" * 40},
        "wip-3": {"task_id": "wip-3", "task_branch": "autoloop/wip-3"},
    }
    # wip-1's branch is AT its candidate; wip-2's branch exists at some other
    # commit, so the branch head does not carry this candidate.
    refs = {"autoloop/wip-1": "a" * 40, "autoloop/wip-2": "c" * 40}

    stats = stats_for(rows, executions, True, refs)

    by_id = {row["id"]: row for row in stats["in_progress"]["rows"]}
    assert by_id["wip-1"]["kind"] == "published"
    assert by_id["wip-2"]["kind"] == "unpublished_candidate"
    assert by_id["wip-3"]["kind"] == "no_candidate"
    assert stats["in_progress"]["counts"] == {
        "published": 1, "unpublished_candidate": 1, "no_candidate": 1, "unknown": 0,
    }
    # The breakdown must account for every in-progress task, or the flat count
    # and the sub-counts tell the operator two different things.
    assert sum(stats["in_progress"]["counts"].values()) == stats["counts"]["in_progress"] == 3
    # A mismatch names BOTH shas, so the verdict can be checked rather than
    # merely believed.
    assert "b" * 12 in by_id["wip-2"]["detail"] and "c" * 12 in by_id["wip-2"]["detail"]


def test_an_unreachable_remote_is_unknown_never_not_published():
    """THE mutation. A failed `ls-remote` and a remote with no such branch both
    produce an empty ref map; reading them the same way would manufacture the
    alarming state out of a network hiccup. Treating unreachable as
    not-published fails here."""
    rows = [roadmap_task("wip-1", status="in_progress"),
            roadmap_task("wip-2", status="in_progress")]
    executions = {
        "wip-1": {"task_id": "wip-1", "candidate_sha": "a" * 40},
        "wip-2": {"task_id": "wip-2"},
    }

    stats = stats_for(rows, executions, remote_ok=False, refs={})

    kinds = {row["id"]: row["kind"] for row in stats["in_progress"]["rows"]}
    assert kinds["wip-1"] == "unknown"
    assert stats["in_progress"]["counts"]["unpublished_candidate"] == 0
    assert stats["in_progress"]["counts"]["unknown"] == 1
    # …and the answer that never needed the network is still given: nothing was
    # committed, and no remote could change that.
    assert kinds["wip-2"] == "no_candidate"
    assert stats["in_progress"]["counts"]["no_candidate"] == 1


def test_a_record_naming_no_remote_is_read_against_the_one_that_was_polled():
    """Most records carry no `intended_remote` at all. Reading that absence as
    "some other remote" would make the whole breakdown read `unknown` against
    live data while every test above still passed — so absence means the remote
    that was actually polled, and only a DIFFERENT named remote is unknown."""
    rows = [roadmap_task("wip-1", status="in_progress"),
            roadmap_task("wip-2", status="in_progress")]
    executions = {
        "wip-1": {"task_id": "wip-1", "task_branch": "autoloop/wip-1",
                  "candidate_sha": "a" * 40},
        "wip-2": {"task_id": "wip-2", "task_branch": "autoloop/wip-2",
                  "candidate_sha": "a" * 40, "intended_remote": "elsewhere"},
    }
    refs = {"autoloop/wip-1": "a" * 40, "autoloop/wip-2": "a" * 40}

    kinds = {row["id"]: row["kind"]
             for row in stats_for(rows, executions, True, refs)["in_progress"]["rows"]}

    assert kinds["wip-1"] == "published"
    assert kinds["wip-2"] == "unknown", "a remote nobody read cannot be evidence"


def test_the_branch_comes_from_the_record_before_the_naming_convention():
    """`intended_remote_ref` is what the publisher meant to write, so it beats
    the checked-out branch, which beats `autoloop/<id>` — a lookup key, never
    evidence. The merge panel resolves it the same way, through the same
    helper, so the two panels cannot name different branches for one task."""
    rows = [roadmap_task("wip-1", status="in_progress"),
            roadmap_task("wip-2", status="in_progress"),
            roadmap_task("wip-3", status="in_progress")]
    executions = {
        "wip-1": {"task_id": "wip-1", "candidate_sha": "a" * 40,
                  "task_branch": "ignored/wip-1",
                  "intended_remote_ref": "refs/heads/published/wip-1"},
        "wip-2": {"task_id": "wip-2", "candidate_sha": "a" * 40,
                  "task_branch": "worker/wip-2"},
        "wip-3": {"task_id": "wip-3", "candidate_sha": "a" * 40},
    }

    branches = {row["id"]: row["branch"]
                for row in stats_for(rows, executions)["in_progress"]["rows"]}

    assert branches == {"wip-1": "published/wip-1", "wip-2": "worker/wip-2",
                        "wip-3": "autoloop/wip-3"}


def test_the_open_work_is_broken_down_by_priority_and_by_area():
    """The counts say how much is left; these say whether what is left is worth
    doing next. 31 open reads differently once eleven of them are p2, and once
    eight turn out to be one over-split family."""
    rows = [
        roadmap_task("inbox-01", priority=2),
        roadmap_task("inbox-02", priority=2),
        roadmap_task("dash-05", priority=1, status="in_progress"),
        roadmap_task("rt-09"),
        # Neither of these is open work, so neither may appear in either split.
        roadmap_task("port-02", priority=2, status="completed"),
        roadmap_task("auto-01", priority=1, status="retired", superseded_by=["rt-09"]),
    ]

    stats = stats_for(rows)

    assert stats["open"] == 4
    # Priority keeps its own order — 1 outranks 2 outranks the default 100 — so
    # sorting these by size would scramble the answer.
    assert stats["open_by_priority"] == [
        {"key": 1, "count": 1}, {"key": 2, "count": 2}, {"key": 100, "count": 1},
    ]
    # Areas have no order of their own, so the biggest family leads.
    assert stats["open_by_area"] == [
        {"key": "inbox", "count": 2}, {"key": "dash", "count": 1}, {"key": "rt", "count": 1},
    ]
    assert sum(row["count"] for row in stats["open_by_priority"]) == stats["open"]
    assert sum(row["count"] for row in stats["open_by_area"]) == stats["open"]


def test_an_unreadable_graph_reports_unknown_rather_than_a_row_of_zeros():
    """A summary is the one panel where a fabricated 0 would be believed. An
    empty `groups` means the graph did not load as a registry (see
    `task_groups`), which is not the same as a roadmap with no tasks in it."""
    unreadable = roadmap_stats([], {}, True, {})
    empty = stats_for([])

    assert unreadable["readable"] is False
    assert unreadable["percent_done"] is None
    assert "could not be read" in unreadable["line"]
    # A genuinely empty roadmap IS readable, and reports zeros honestly.
    assert empty["readable"] is True and empty["total"] == 0
    assert empty["percent_done"] is None

    script = PAGE.split("<script>", 1)[1]
    assert "!s.readable" in script, "the page must distinguish unreadable from empty"


def test_the_summary_is_wired_from_the_same_groups_the_roadmap_renders(tmp_path):
    """End to end, against a real checkout with a real origin: the payload's
    counts must equal the group counts rendered below them, or the page shows
    two answers to one question. A published candidate is exercised against an
    actual `git ls-remote`, not a hand-written ref map."""
    repo, merged_sha, _ = merge_fixture(tmp_path)
    write_registry(
        repo,
        [roadmap_task("t-merged", status="in_progress"),
         roadmap_task("t-ghost", status="in_progress"),
         roadmap_task("d-1", status="completed"),
         roadmap_task("r-1")],
        [{"task_id": "t-merged", "task_branch": "autoloop/t-merged",
          "candidate_sha": merged_sha},
         {"task_id": "t-ghost", "task_branch": "autoloop/t-ghost",
          "candidate_sha": "f" * 40}],
    )

    payload = collect(repo)
    stats, groups = payload["stats"], groups_by_key(payload["groups"])

    assert stats["readable"] is True
    assert stats["counts"]["in_progress"] == groups["in_progress"]["count"] == 2
    assert stats["counts"]["completed"] == groups["done"]["count"] == 1
    # Every top-level count against the group it renders above, by GROUPS key —
    # not a spot check, because a summary that agrees with the list on five
    # states out of six is exactly as misleading as one that agrees on none.
    for name, _label, group_key in STAT_BUCKETS:
        assert stats["counts"][name] == groups[group_key]["count"], name
    assert stats["counts"]["ready"] == groups["ready"]["count"] == 1
    assert stats["total"] == len(payload["roadmap"]) == 4
    kinds = {row["id"]: row["kind"] for row in stats["in_progress"]["rows"]}
    assert kinds == {"t-merged": "published", "t-ghost": "unpublished_candidate"}


def test_an_unreachable_origin_leaves_the_breakdown_unknown_end_to_end(tmp_path):
    """The same wiring with the network gone. A path that is not a repository
    fails instantly; an unroutable URL would sit on the `ls-remote` timeout and
    stall the suite instead."""
    repo, merged_sha, _ = merge_fixture(tmp_path)
    write_registry(
        repo,
        [roadmap_task("t-merged", status="in_progress")],
        [{"task_id": "t-merged", "task_branch": "autoloop/t-merged",
          "candidate_sha": merged_sha}],
    )
    run_git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    stats = collect(repo)["stats"]

    assert stats["in_progress"]["rows"][0]["kind"] == "unknown"
    assert stats["in_progress"]["counts"]["unpublished_candidate"] == 0
    assert stats["in_progress"]["counts"]["unknown"] == 1


def test_the_summary_renders_at_the_top_of_the_page():
    """A payload carrying the counts is not a page showing them, and the
    operator's request was specifically about placement: above the task list,
    because it is what gets read first."""
    static_markup, script = PAGE.split("<script>", 1)

    assert '<section id="summary">' in static_markup
    for later in ('id="tiles"', 'id="progressbox"', 'id="merged"', 'id="roadmap"'):
        assert static_markup.index('id="summary"') < static_markup.index(later), \
            f"the counts must render above {later}"

    assert "renderStats(d.stats)" in script
    for field in ("s.line", "s.total", "s.open", "s.denominator", "s.percent_done",
                  "s.tiles", "t.label", "t.count", "s.open_by_priority",
                  "s.open_by_area", "c.completed", "c.retired",
                  "w.unpublished_candidate", "r.detail"):
        assert field in script, f"{field} never reaches the DOM"

    # The state tiles are rendered FROM the payload's labels, so the template
    # cannot spell a state itself and cannot put two states under one word. A
    # hard-coded tile list is the shape that let `blocked` mean the quarantine
    # up here while the Roadmap group below meant a dependency.
    block = script.split("function renderStats(s){", 1)[1].split("\nfunction ", 1)[0]
    assert "(s.tiles || []).map(t => [t.label, t.count])" in block
    for state in TaskState:
        assert f'"{state.value}"' not in block, f"{state.value} is spelled in the template"

    # Identity is never colour-alone on this page: every in-progress state ships
    # an icon and a word, and the table below repeats all of it as text.
    marks = PAGE.split("const WIP = {", 1)[1].split("};", 1)[0].replace(" ", "")
    for kind in IN_PROGRESS_KINDS:
        assert f"{kind}:[" in marks

    # Inside the change guard, unlike `renderProgress`: nothing in the summary
    # ticks on a clock, so rebuilding it on every 2s poll would discard text
    # selection for nothing.
    body = script.split("function render(d, force){", 1)[1]
    assert body.index("sig === LASTJSON") < body.index("renderStats(d.stats)")


# ---- the roadmap docket: full descriptions, expandable (2026-08-18) -----------
#
# The panel sent `id`, `title` and `priority` and nothing else, so the one
# question an operator actually has about a queued task — what does it say —
# could only be answered by opening `.autoloop/tasks.json` by hand, and that is
# the file the whole roadmap is steered from.
#
# Four properties carry the feature, and each is asserted rather than described:
#
# * the WHOLE description reaches the page (a truncation is invisible on the
#   page — it reads exactly like a task that really is that short);
# * it is ESCAPED, because it is untrusted text from a file going into HTML;
# * the ORDINAL is the position `next_ready()` selects from, pinned against the
#   real method rather than against a repeat of its sort key; and
# * a task that cannot be picked has NO ordinal and names what it waits on.
#
# The escaping and the filter are asserted by RUNNING the page's own helpers
# under node rather than by grepping for `esc(`. A template that interpolates
# `esc(t.description)` into the wrong place, or a filter that reads `t.title`
# twice, passes every string check and fails these.


def pure_roadmap_js() -> str:
    """The roadmap panel's pure helpers, lifted verbatim out of the served page.

    Everything between the markers is payload-in / string-out, with no DOM and
    no module state, so it can be executed directly. `esc` comes along because
    every helper in there depends on it — and it is the function under test in
    the escaping case.
    """
    script = PAGE.split("<script>", 1)[1]
    esc_line = next(
        line for line in script.splitlines() if line.startswith("const esc =")
    )
    region = script.split("// PURE_ROADMAP_START", 1)[1].split("// PURE_ROADMAP_END", 1)[0]
    return esc_line + "\n" + region


def run_js(source: str) -> str:
    """Run `source` under node and return its stdout.

    Skipped rather than faked when node is absent, exactly as the syntax check
    above is: a hand-rolled JS interpreter would be testing the interpreter.
    """
    import shutil
    import subprocess
    import tempfile

    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment without node
        pytest.skip("node is required to run the page's own helpers")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(source)
        path = handle.name
    result = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"the page's helpers threw:\n{result.stderr[:800]}"
    return result.stdout


def test_a_tasks_full_description_reaches_the_page_untruncated(tmp_path):
    """The whole point of the change. A slice would be indistinguishable on the
    page from a task that really is that short — which is the failure being
    fixed, not a smaller version of it — so this asserts EQUALITY against a
    description far longer than any cap the file has ever applied (`title` is
    still cut to 180 in the audit-report panel next door).

    Driven through `collect()` because the panel renders `groups`, so a field
    added to `_grouped` and dropped on the way out would still fail here.
    """
    repo = make_repo(tmp_path)
    long_desc = "WHAT IS MISSING.\n\n" + ("the roadmap is steered from this file. " * 130)
    assert len(long_desc) > 5000
    write_registry(repo, [roadmap_task("dash-10", description=long_desc)])

    row = groups_by_key(collect(repo)["groups"])["ready"]["tasks"][0]

    assert row["description"] == long_desc
    assert row["chars"] == len(long_desc)
    # …and the page puts it in the DOM, in a pre that keeps its shape: these
    # are hard-wrapped plain text with ALL-CAPS heads, so a reflow would
    # destroy information the description carries in its layout.
    static_markup, script = PAGE.split("<script>", 1)
    assert '<pre class="desc">' in script
    # pre-wrap, not a clamp and not a truncation: the descriptions are
    # hard-wrapped plain text with ALL-CAPS heads and indented lists, so their
    # shape carries meaning a reflow would destroy.
    assert "white-space:pre-wrap" in static_markup


def test_a_description_full_of_html_is_shown_as_text_not_run_as_markup():
    """`tasks.json` is untrusted input to this page: anyone who can write that
    file can write `<script>`, and the description is the one field long enough
    that nobody would notice it had been parsed rather than displayed.

    This RUNS the page's own row template rather than grepping it for `esc(`.
    A template that escapes the title and forgets the description passes every
    string check and fails here.
    """
    hostile = '<script>alert("x")</script> & <img src=x onerror=1> a < b'
    out = run_js(pure_roadmap_js() + """
const row = rmRow({id: "x-1", title: "T & <b>", description: HOSTILE,
                   priority: 1, ordinal: 1, detail: "d", waits_on: [],
                   superseded_by: [], chars: HOSTILE.length});
process.stdout.write(row);
""".replace("HOSTILE", json.dumps(hostile)))

    assert "<script>" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&lt;img src=x onerror=1&gt;" in out
    # `&` too, not only the angle brackets: `a &amp; b` must render as the
    # ampersand the task wrote, never as the head of an entity.
    assert "&amp;" in out
    # The title is escaped by the same pass, and nothing has been dropped: the
    # visible text is all still there, just as text.
    assert "a &lt; b" in out


def test_the_ordinal_is_the_position_next_ready_would_pick():
    """The number in the left rail is information, not decoration: it is the
    position `next_ready()` selects from, so it must survive the same pin the
    Ready group's ORDER already has — the real `next_ready()`/`mark_completed()`
    loop, run here on a registry of its own.

    The fixture makes insertion order differ from priority order and ties
    `r-a`/`r-b` so the id tiebreak is exercised.
    """
    rows = [
        roadmap_task("r-c", priority=5),
        roadmap_task("r-b", priority=1),
        roadmap_task("r-a", priority=1),
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("b-1", depends_on=["wip-1"]),
    ]
    groups = groups_by_key(task_groups({"tasks": rows}, {}))

    ordered = [(t["ordinal"], t["id"]) for t in groups["ready"]["tasks"]]

    registry = TaskRegistry.from_dict({"tasks": [dict(row) for row in rows]})
    picks = []
    while (nxt := registry.next_ready()) is not None:
        picks.append(nxt.id)
        registry.mark_completed(nxt.id)

    assert picks == ["r-a", "r-b", "r-c"], "the fixture must exercise both keys"
    assert ordered == [(1, "r-a"), (2, "r-b"), (3, "r-c")]
    # Nothing outside the ready set is numbered — none of it can be picked,
    # whatever its priority, and an ordinal there would be a claim about
    # dispatch order that the loop does not honour.
    assert groups["in_progress"]["tasks"][0]["ordinal"] is None
    assert groups["blocked"]["tasks"][0]["ordinal"] is None


def test_a_dependency_blocked_task_has_no_ordinal_and_names_what_it_waits_on():
    """Blocked is the case the ordinal exists to exclude: `b-1` here outranks
    every ready task on priority and still cannot run, so a number beside it
    would say the loop will pick it first. It carries the dependency instead —
    and only the INCOMPLETE ones, since a finished dependency is not why
    anything is waiting.
    """
    groups = groups_by_key(task_groups({"tasks": [
        roadmap_task("done-1", status="completed"),
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("r-1", priority=9),
        roadmap_task("b-1", priority=1, depends_on=["done-1", "wip-1"]),
    ]}, {}))

    blocked = groups["blocked"]["tasks"][0]
    assert blocked["id"] == "b-1"
    assert blocked["ordinal"] is None
    assert blocked["waits_on"] == ["wip-1"]
    # The chip and the prose sentence come from one function, so they can never
    # name different dependencies.
    assert blocked["detail"] == "waiting on wip-1"
    # A ready task waits on nothing and says so with an empty list rather than
    # with a chip reading "waits on".
    assert groups["ready"]["tasks"][0]["waits_on"] == []

    # A retirement usually still declares the dependencies it was planned with
    # (`state_of` says so in as many words), so filling this in for every state
    # would hang a "waits on" chip on a task that waits on nobody — the exact
    # misread `TaskState.RETIRED` was added to end.
    retired = groups_by_key(task_groups({"tasks": [
        roadmap_task("wip-1", status="in_progress"),
        roadmap_task("rt-1", status="retired", depends_on=["wip-1"],
                     superseded_by=["wip-1"]),
    ]}, {}))["retired"]["tasks"][0]
    assert retired["waits_on"] == [] and retired["ordinal"] is None


def test_the_search_matches_description_text_not_only_titles():
    """Half of what an operator searches for is only in the description — the
    file a task touches, the constraint it was given — so a filter reading ids
    and titles alone would answer "nothing matches" for a task that is on the
    page. Run, not grepped, for the same reason the escaping test is."""
    out = run_js(pure_roadmap_js() + """
const t = {id: "dash-10", title: "Show a task's full description",
           description: "the roadmap is steered from .autoloop/tasks.json"};
process.stdout.write(JSON.stringify({
  byDescription: rmMatch(t, "steered"),
  byPathInDescription: rmMatch(t, "tasks.json"),
  byId: rmMatch(t, "dash-1"),
  byTitle: rmMatch(t, "full description"),
  noMatch: rmMatch(t, "postgres"),
  emptyFilterKeepsEverything: rmMatch(t, ""),
  missingDescriptionIsNotAThrow: rmMatch({id: "x", title: "y"}, "y"),
}));
""")

    assert json.loads(out) == {
        "byDescription": True,
        "byPathInDescription": True,
        "byId": True,
        "byTitle": True,
        "noMatch": False,
        "emptyFilterKeepsEverything": True,
        "missingDescriptionIsNotAThrow": True,
    }


def test_the_priority_band_is_its_own_ramp_and_never_a_status_colour():
    """Priority decides execution order, which is a different axis from health:
    a p0 is urgent, not broken. So the bands are their own `--rm-band-*` tokens
    and none of them is `--good`/`--warning`/`--critical`, the roles reserved
    for verdicts everywhere else on this page."""
    out = json.loads(run_js(pure_roadmap_js() + """
process.stdout.write(JSON.stringify([0, 2, 3, 5, 6, 8, 9, 100].map(rmBand)
  .concat([rmBand(null), rmBand(undefined)])));
"""))
    assert out == ["urgent", "urgent", "active", "active", "queued", "queued",
                   "parked", "parked", "parked", "parked"]

    for band, light, dark in (("urgent", "#A83C2A", "#E0866F"),
                              ("active", "#966C15", "#D3A44E"),
                              ("queued", "#3B6096", "#84A9DB"),
                              ("parked", "#6C7A77", "#8B9895")):
        assert f"--rm-band-{band}:{light}" in PAGE
        # Declared in BOTH dark scopes: the media query (guarded so an explicit
        # light stamp still wins) and the explicit `data-theme="dark"`.
        assert PAGE.count(f"--rm-band-{band}:{dark}") == 2
    band_rules = "".join(
        line for line in PAGE.splitlines() if "--rm-band-" in line
    )
    for reserved in ("--good", "--warning", "--serious", "--critical"):
        assert reserved not in band_rules


def test_every_docket_colour_is_a_token_declared_in_all_three_theme_scopes():
    """So the panel survives with no reference file to copy from. Each colour is
    a token on bare `:root`, redefined under the dark media query — guarded with
    `:root:not([data-theme="light"])` so an explicit light choice still wins —
    and again under `:root[data-theme="dark"]` so the toggle beats an OS
    setting in both directions."""
    light, rest = PAGE.split('@media (prefers-color-scheme:dark)', 1)
    media, explicit = rest.split(':root[data-theme="dark"]', 1)
    for token, value in (("ground", "#F2F6F5"), ("surface", "#FFFFFF"),
                         ("ink", "#141D1B"), ("muted", "#5B6B68"),
                         ("line", "#DBE5E2"), ("accent", "#2F7268")):
        assert f"--rm-{token}:{value}" in light, f"--rm-{token} is not a light token"
    for token, value in (("ground", "#0E1413"), ("surface", "#151E1C"),
                         ("ink", "#E2EBE8"), ("muted", "#8FA09C"),
                         ("line", "#243130"), ("accent", "#63B9A8")):
        assert f"--rm-{token}:{value}" in media, f"--rm-{token} has no dark value"
        assert f"--rm-{token}:{value}" in explicit, f"--rm-{token} ignores the toggle"
    assert ':root:not([data-theme="light"])' in media

    # Type roles: serif for the page name and the task titles, monospace for
    # ids, ordinals, counts and the descriptions, system sans (from `body`) for
    # everything else.
    for rule in ("#roadmap .ttl{font-family:ui-serif", "#roadmap .ord{font-family:ui-monospace",
                 "#roadmap .tid{font-family:ui-monospace", "#roadmap .size{font-family:ui-monospace",
                 "#roadmap pre.desc{"):
        assert rule in PAGE, f"{rule} is missing"
    assert "font-family:ui-serif" in PAGE.split("h1{", 1)[1].split("}", 1)[0]

    # Self-contained: no stylesheet, font or script fetched from anywhere.
    for external in ("<link", "@import", "src=\"http", "fonts.googleapis"):
        assert external not in PAGE, f"{external} makes the page non-self-contained"


def test_the_docket_is_rendered_by_one_function_the_search_box_also_calls():
    """A payload carrying the descriptions is not a page showing them.

    The shape matters as much as the strings: the search box is STATIC markup
    (inside `#roadmap` its half-typed text would be erased by the 2s poll) and
    it re-renders through the SAME function a poll uses, so a filtered panel and
    a polled one cannot disagree. Open rows are restored from `RMOPEN` because a
    successful priority save clears `LASTJSON` to force a rebuild — without it,
    editing a priority would snap shut the row being read.
    """
    static_markup, script = PAGE.split("<script>", 1)

    assert '<div id="roadmap" class="scroll"></div>' in static_markup
    assert 'id="rmq"' in static_markup and 'id="rmexpand"' in static_markup
    assert static_markup.index('id="rmq"') < static_markup.index('id="roadmap"')

    for field in ("t.description", "t.ordinal", "t.waits_on", "t.chars", "t.detail",
                  "t.priority", "g.label", "g.count"):
        assert field in script, f"{field} never reaches the DOM"
    assert "function renderRoadmap(d){" in script
    assert "renderRoadmap(d);" in script and "renderRoadmap(LAST)" in script
    # Open state survives the rebuild, and `toggle` is bound per row because it
    # does not bubble — a delegated listener on #roadmap would never fire.
    assert 'addEventListener("toggle"' in script and "RMOPEN" in script
    # The heading count stays the STATE count under an active filter: the
    # summary tiles above are derived from this same payload, and a heading
    # that shrank as you typed would disagree with them.
    body = script.split("function renderRoadmap(d){", 1)[1].split("\nfunction ", 1)[0]
    assert "esc(g.count)" in body and "matched" in body
    # Priority editing is untouched, still applied immediately and still read
    # back from tasks.json — this change is display only.
    assert 'querySelectorAll("#roadmap button.save")' in script
    assert '"/api/priority"' in script
