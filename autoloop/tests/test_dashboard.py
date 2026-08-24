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
    DEP_DEFAULT_VIEW,
    DEP_FILTERS,
    DEP_NODE_STATES,
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
    dep_view_key,
    dependency_graph,
    is_ancestor,
    merge_groups,
    merge_states,
    naming_ancestor,
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
    fail only near a second boundary.

    `_SUBJECT_CACHE` (dash-18) joins them for the same reason and one of its
    own: it holds `git log --all` for 60s per repo, so without this a `collect`
    test would judge its own registry against the previous test's commits."""
    import autoloop.dashboard as dash

    caches = (dash._REMOTE_CACHE, dash._ANCESTRY_CACHE, dash._SHALLOW_CACHE,
              dash._SUBJECT_CACHE)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


@pytest.fixture(autouse=True)
def _clean_upgrade_state():
    """The self-replacement machinery (loop-03) keeps three pieces of module
    state: which shas this process has already failed to upgrade to, the
    preflight results memoised beside them, and the request counter plus the
    armed flag that decide when a replacement is safe.

    All three are process-wide by design — `os.execv` replaces the whole image,
    so a per-thread count would say nothing — and every one of them would make
    test order load-bearing if it leaked. `_UPGRADING` is the sharp one: left
    set, `Handler.handle` refuses every connection, so an unrelated test would
    fail with a page that never answers.
    """
    import autoloop.dashboard as dash

    def reset():
        dash._UPGRADE_ATTEMPTS.clear()
        dash._PREFLIGHTS.clear()
        dash._INFLIGHT = 0
        dash._UPGRADING = False

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def no_process_replacement(monkeypatch):
    """Every route to replacing this process, disabled for the whole file.

    Same fixture, same reason, as `test_self_upgrade.py`'s: a test that really
    called `os.execv` would replace the pytest process with a dashboard — no
    failure, no report, just a test session that turns into a web server. The
    two tests that care about the exec install their own recorder over this.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError("this file must never replace the pytest process")

    monkeypatch.setattr(os, "execv", refuse)


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


# ---- a commit naming the task: the third evidence source (dash-18, 2026-08-23) -
#
# Measured 2026-08-21: the panel reported `unpublished: 3` — dash-02, pkt-03 and
# audit-0001 — and all three were done and IN the branch. `git merge-base
# --is-ancestor 07b659b autoloop/mainline` succeeds for audit-0001, whose commit
# subject is "repository audit". None of the three had an execution record and
# origin carried no branch for any of them: they landed by routes that predate
# the publisher, so `merge_states` fell to its last branch and labelled
# integrated work as though it had never shipped.
#
# The evidence it was missing already exists one section down, shipped by
# roadmap-01 — `mentions_task_id` + `is_ancestor`, whole-token matching and
# git's own ancestry answer. This wires that in as a THIRD source rather than
# writing a second copy of it, and the wiring is bounded by four properties, all
# asserted below rather than promised:
#
# * POSITIVE ONLY. A matching ancestor proves integration; no match, a match
#   that is not an ancestor, a match git could not resolve, a failed search and
#   an empty search all leave the row byte-identical to what it is today.
# * ORDER UNCHANGED. It is consulted only on the final fall-through, so it can
#   neither overrule a branch git says is not in HEAD nor re-word a row the
#   execution record already decided.
# * `unknown` STAYS `unknown`. It lives INSIDE the readable-remote branch: an
#   ancestor commit does not make an unreachable remote readable.
# * THE ROW SAYS WHICH EVIDENCE DECIDED IT. A subject-line match is a heuristic
#   and a published ref is not, and an operator acting on the row has to be able
#   to tell them apart.


def one_completed(task_id="dash-02", title="T"):
    """The `completed` list `merge_states` takes — one task, nothing else."""
    return [{"id": task_id, "title": title}]


def test_a_commit_naming_a_completed_task_and_in_head_renders_merged():
    """THE measured case. No execution record, no branch on origin, and a commit
    whose subject names the task sitting in the branch — which is decidable, and
    which the panel was throwing away."""
    commits = [("d" * 40, "Merge task dash-02 (dd28dfa) into autoloop/mainline")]

    rows = merge_states(one_completed(), {}, True, {}, lambda sha: "yes", commits)

    assert rows[0]["state"] == "merged"
    # The sha that decided it reaches the column, not a blank cell.
    assert rows[0]["sha"] == "d" * 12
    assert "names dash-02 in its subject" in rows[0]["detail"]
    assert "ancestor of HEAD" in rows[0]["detail"]


def test_each_evidence_source_says_which_one_decided_the_row():
    """Three sources, three different confidences, three distinguishable
    sentences — `git ls-remote` reported the exact ref the publisher wrote, the
    execution record named a commit, and the third is a match on a subject line.
    A shared wording would hide the weakest of the three behind the strongest."""
    def verdict(_sha):
        return "yes"

    by_ref = merge_states(one_completed("rt-9"), {}, True, {"autoloop/rt-9": "a" * 40}, verdict)
    by_record = merge_states(
        one_completed("rt-9"), {"rt-9": {"candidate_sha": "b" * 40}}, True, {}, verdict)
    by_subject = merge_states(one_completed("rt-9"), {}, True, {}, verdict,
                              [("c" * 40, "rt-9: the work")])

    assert [r[0]["state"] for r in (by_ref, by_record, by_subject)] == ["merged"] * 3
    details = [r[0]["detail"] for r in (by_ref, by_record, by_subject)]
    assert len(set(details)) == 3, f"an operator cannot tell these apart: {details}"
    # The heuristic is the one that has to name itself, because it is the one
    # whose evidence is a string a human typed.
    assert "subject" in details[2]
    assert "subject" not in details[0] and "subject" not in details[1]


def test_no_commit_names_the_task_and_the_row_is_byte_identical_to_today():
    """Absence of a mention is not evidence. The ancestry stub says "yes" to
    everything here, so a row that flipped would be one matched by something
    other than the subject."""
    commits = [("a" * 40, "second"), ("b" * 40, "Merge task pkt-03 (0fcc1c6)")]

    before = merge_states(one_completed("t-ghost"), {}, True, {}, lambda sha: "yes")
    after = merge_states(one_completed("t-ghost"), {}, True, {}, lambda sha: "yes", commits)

    assert after == before
    assert before[0]["state"] == "unpublished"
    assert "never pushed" in before[0]["detail"] and before[0]["sha"] == ""


def test_a_failed_search_and_an_empty_one_can_never_move_a_row():
    """`None` (git would not answer) and `[]` (it answered, nothing is there)
    are different facts and neither is evidence of integration. This is the
    fail-open shape to watch: a guard that switches itself off when the material
    it needs is missing, and merges the row because nothing said not to."""
    baseline = merge_states(one_completed(), {}, True, {}, lambda sha: "yes")

    for commits in (None, [], (), [("", "")]):
        assert merge_states(one_completed(), {}, True, {}, lambda sha: "yes", commits) == baseline
    assert baseline[0]["state"] == "unpublished"


def test_the_source_is_optional_so_an_uninformed_caller_is_unchanged():
    """`commits` defaults to `None`, so every existing call site — and any
    caller with no search to offer — gets exactly the classification it got
    before this source existed."""
    assert (merge_states(one_completed(), {}, True, {}, lambda sha: "yes")
            == merge_states(one_completed(), {}, True, {}, lambda sha: "yes", None))


def test_a_matching_commit_that_is_not_an_ancestor_does_not_flip_the_row():
    """Naming the task is the thing to TEST, never the answer. A commit sitting
    on a branch nobody merged names it just as well as one that landed."""
    commits = [("e" * 40, "dash-02: on a branch nobody merged")]

    rows = merge_states(one_completed(), {}, True, {}, lambda sha: "no", commits)

    assert rows[0]["state"] == "unpublished"
    assert rows[0]["sha"] == "", "a non-ancestor must not reach the sha column"


def test_a_match_git_could_not_resolve_is_not_read_as_integration():
    """The fail-open case this source is easiest to get wrong: the test has to
    be `== "yes"` and never `!= "no"`. `is_ancestor` answers `"unknown"` for an
    unreadable repository and for a shallow clone, and reading an unanswered
    question as integration asserts a merge nobody observed."""
    commits = [("f" * 40, "dash-02: shipped")]

    rows = merge_states(one_completed(), {}, True, {}, lambda sha: "unknown", commits)

    assert rows[0]["state"] == "unpublished"


def test_the_match_is_whole_token_so_pkt_03_is_not_satisfied_by_pkt_030():
    """`mentions_task_id` owns this and is reused rather than reimplemented, so
    the boundary is the task-id alphabet on both sides. `pkt-030`, `x-pkt-03`
    and `pkt-03.5` are three other real, legal ids."""
    decoys = [("a" * 40, "pkt-030: a different task entirely"),
              ("b" * 40, "x-pkt-03 and pkt-03.5 are two more")]

    rows = merge_states(one_completed("pkt-03"), {}, True, {}, lambda sha: "yes", decoys)
    assert rows[0]["state"] == "unpublished", "a substring match is a wrong positive"

    # …and the same list plus a real whole-token mention DOES decide the row, so
    # this is matching rather than never matching.
    rows = merge_states(one_completed("pkt-03"), {}, True, {}, lambda sha: "yes",
                        decoys + [("c" * 40, "pkt-03, part 4")])
    assert rows[0]["state"] == "merged" and rows[0]["sha"] == "c" * 12


def test_a_row_with_no_id_matches_nothing_rather_than_every_commit():
    """An empty id must not become a wildcard — that is how a malformed registry
    row would read as merged against whatever commit happens to be first."""
    commits = [("a" * 40, "Merge task dash-02 into autoloop/mainline")]

    rows = merge_states([{"title": "no id at all"}], {}, True, {},
                        lambda sha: "yes", commits)

    assert rows[0]["id"] == "" and rows[0]["state"] == "unpublished"


def test_an_unreadable_remote_stays_unknown_even_with_a_naming_ancestor():
    """An ancestor commit does not make an unreachable remote readable. The
    third source sits INSIDE the readable-remote branch for exactly this: a row
    nobody could judge has to keep saying so."""
    commits = [("a" * 40, "dash-02: shipped")]

    rows = merge_states(one_completed(), {}, False, {}, lambda sha: "yes", commits)

    assert rows[0]["state"] == "unknown"
    assert "unverified" in rows[0]["detail"]


def test_the_first_two_evidence_sources_still_decide_their_own_rows():
    """Order of evidence is unchanged. The third source is reached only on the
    fall-through, so it can neither overrule a branch git says is NOT in HEAD
    nor re-word a row the execution record already decided — both of which it
    would do here if it were consulted first, since it matches in both."""
    def verdict(sha):
        return {"a" * 40: "no", "b" * 40: "yes"}.get(sha, "yes")

    record = {"rt-10": {"task_branch": "autoloop/rt-10", "candidate_sha": "b" * 40}}
    commits = [("c" * 40, "rt-10: also named here, and an ancestor")]

    on_origin = merge_states(one_completed("rt-10"), record, True,
                             {"autoloop/rt-10": "a" * 40}, verdict, commits)
    assert on_origin[0]["state"] == "unmerged"
    assert on_origin[0]["detail"] == f"on origin at {'a' * 12}, not in HEAD"
    assert on_origin[0]["sha"] == "a" * 12

    from_record = merge_states(one_completed("rt-10"), record, True, {}, verdict, commits)
    assert from_record[0]["state"] == "merged"
    assert from_record[0]["detail"] == f"no branch on origin; commit {'b' * 12} is in HEAD"
    assert from_record[0]["sha"] == "b" * 12


def test_ancestry_is_asked_only_about_commits_whose_subject_names_the_task():
    """Matching GATES the ancestry check rather than running beside it. The
    negative is the load-bearing half: an implementation that asked git about
    every commit and then matched the subject would pass every state assertion
    above and walk the whole log for each row."""
    asked = []

    def verdict(sha):
        asked.append(sha)
        return "yes"

    commits = [("a" * 40, "unrelated"), ("b" * 40, "dash-02: shipped"),
               ("c" * 40, "also unrelated")]

    assert naming_ancestor("dash-02", commits, verdict) == "b" * 40
    assert asked == ["b" * 40], "ancestry must be asked only about matches"


def test_the_named_sha_is_the_first_match_in_log_order():
    """`shipped_states`' own convention, so the merge panel and `shipped-report`
    cannot name two different commits for a task that shipped as four."""
    commits = [("a" * 40, "pkt-03, part 1"), ("b" * 40, "pkt-03, part 2"),
               ("c" * 40, "pkt-03, part 3")]

    assert naming_ancestor("pkt-03", commits, lambda sha: "yes") == "a" * 40
    # …and a first match git says is NOT an ancestor does not stop the search.
    assert naming_ancestor(
        "pkt-03", commits, lambda sha: "no" if sha == "a" * 40 else "yes") == "b" * 40


def naming_commit_fixture(tmp_path, subject="Merge task t-ghost (0fcc1c6) into work"):
    """`merge_fixture`'s repo plus one commit on the observed branch whose
    subject names `t-ghost` — a task with no execution record and no branch on
    origin, i.e. the production shape of all three measured rows."""
    repo, _, _ = merge_fixture(tmp_path)
    (repo / "shipped.txt").write_text("shipped\n")
    run_git(repo, "add", "shipped.txt")
    run_git(repo, "commit", "-q", "-m", subject)
    return repo, run_git(repo, "rev-parse", "HEAD").strip()


def test_a_task_named_by_an_ancestor_commit_renders_merged_end_to_end(tmp_path):
    """The claim, against a real repository and through the payload the page
    renders. A pure-function test cannot show this: the title says DISPLAY, and
    the wiring — `merge_report` fetching the search and handing it down — is
    where it would silently not happen."""
    repo, sha = naming_commit_fixture(tmp_path)
    write_registry(repo, [completed("t-ghost")])   # no record, no branch on origin

    payload = collect(repo)
    row = by_id(payload)["t-ghost"]

    assert row["state"] == "merged", "integrated work must not read as unpublished"
    assert row["sha"] == sha[:12]
    assert "names t-ghost in its subject" in row["detail"]
    assert payload["merge"]["counts"] == {"merged": 1, "unmerged": 0,
                                          "unpublished": 0, "unknown": 0}
    # …and it renders in the merged GROUP, which is what the panel actually
    # draws — a row classified merged and grouped elsewhere is still misfiled.
    groups = merge_groups_by_key(payload["merge"]["groups"])
    assert [r["id"] for r in groups["merged"]["rows"]] == ["t-ghost"]
    assert groups["unpublished"]["rows"] == []


def test_the_same_task_with_no_naming_commit_still_renders_unpublished(tmp_path):
    """The other half of the claim: this must not have turned `unpublished` into
    a state nothing reaches. Same repository, same registry, one word changed in
    the commit subject."""
    repo, _ = naming_commit_fixture(tmp_path, subject="an unrelated commit")
    write_registry(repo, [completed("t-ghost")])

    row = by_id(collect(repo))["t-ghost"]

    assert row["state"] == "unpublished"
    assert "never pushed" in row["detail"]


def test_an_unreachable_origin_stays_unknown_even_with_the_commit_in_the_branch(tmp_path):
    """End-to-end version of the `unknown` bound. The commit naming the task is
    right there in the branch and git will confirm it — and the row must still
    say the remote could not be read, because it could not."""
    import autoloop.dashboard as dash

    repo, _ = naming_commit_fixture(tmp_path)
    write_registry(repo, [completed("t-ghost")])
    # A path that is not a repository fails instantly; an unroutable URL would
    # sit on the ls-remote timeout and stall the suite instead.
    run_git(repo, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    calls = []
    original = dash.commit_subjects

    def counted(target):
        calls.append(target)
        return original(target)

    dash.commit_subjects = counted
    try:
        payload = collect(repo)
    finally:
        dash.commit_subjects = original

    assert payload["merge"]["remote_ok"] is False
    assert by_id(payload)["t-ghost"]["state"] == "unknown"
    # …and the walk is skipped outright, because no row can reach the third
    # source with the remote unreadable. The same bound, stated at the call site
    # in `merge_report` as well as inside `merge_states`.
    assert calls == [], "a search whose result cannot be used must not run"


def test_the_subject_search_writes_nothing_to_the_observed_checkout(tmp_path):
    """`git log --all` runs on the 2s poll now, so it joins everything else here
    under the read-only rule: the loop's escape detector refuses a write-capable
    task if the primary checkout is dirty. `--no-optional-locks` is injected at
    `_run_status`, the single subprocess entry point, so this inherits it — and
    this is the test that would notice if a call were ever added around it."""
    repo, _ = naming_commit_fixture(tmp_path)
    write_registry(repo, [completed("t-ghost")])
    # Order matters: `run_git` here does NOT pass --no-optional-locks, so it
    # rewrites .git/index. Snapshot AFTER it, or the test measures its own
    # side effect and blames the tracker.
    status_before = run_git(repo, "status", "--porcelain")
    before = snapshot(repo)

    row = by_id(collect(repo))["t-ghost"]

    assert row["state"] == "merged", "the search must actually have run"
    assert snapshot(repo) == before, "the tracker must not create, remove or touch any file"
    assert run_git(repo, "status", "--porcelain") == status_before


def test_the_subject_search_is_cached_so_the_2s_poll_does_not_walk_every_ref(tmp_path):
    """The cost bound. `shipped-report` asks this once; the panel asks it on
    every poll, and it is the only read there whose cost grows with history —
    so it is memoized per repo exactly like `ls-remote`, and `shipped_report`
    still calls `commit_subjects` directly so a one-shot report never reads a
    cached answer."""
    import autoloop.dashboard as dash

    repo, _ = naming_commit_fixture(tmp_path)
    write_registry(repo, [completed("t-ghost")])
    calls = []
    original = dash.commit_subjects

    def counted(target):
        calls.append(target)
        return original(target)

    dash.commit_subjects = counted
    try:
        assert by_id(collect(repo))["t-ghost"]["state"] == "merged"
        assert by_id(collect(repo))["t-ghost"]["state"] == "merged"
    finally:
        dash.commit_subjects = original

    assert len(calls) == 1, f"the log walk ran {len(calls)} times across two polls"


def test_a_stale_cached_search_can_still_only_be_positive_evidence(tmp_path):
    """Caching `None` is safe by construction rather than by luck: the evidence
    is positive-only, so the worst a stale "the search failed" can do is leave a
    row reading as it did before this source existed. It is never allowed to
    freeze a VERDICT — it holds no verdicts, only the material one is read
    from."""
    import autoloop.dashboard as dash

    repo, _ = naming_commit_fixture(tmp_path)
    write_registry(repo, [completed("t-ghost")])
    dash._SUBJECT_CACHE[str(repo)] = {"at": time.time(), "commits": None}

    row = by_id(collect(repo))["t-ghost"]

    assert row["state"] == "unpublished", "a failed search must never assert a merge"
    assert "never pushed" in row["detail"]


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
                               "blocked": 1, "blocked_by_operator": 1, "retired": 1,
                               # ship-01: DONE, under another task's commits. Its
                               # own count for the reason it is its own state —
                               # folded into `completed` a reader would expect the
                               # merge sweep to have a branch to integrate for it,
                               # and it never had one.
                               "shipped_elsewhere": 0}
    # And the one-line summary says the same thing in `TaskRegistry.summary()`'s
    # own words, so the page and the review packet cannot report two roadmaps.
    # Asserted as an EXACT string because the vocabulary is the point, not the
    # arithmetic: this sentence is copied from `TaskRegistry.summary()` (minus
    # its priority-1 breakdown and `next ready:` tail, which are dispatch advice
    # rather than counts). If this fails, the fix is to change both or neither —
    # `summary()` is the authority, this is the copy.
    assert stats["line"] == ("7 tasks: 2 completed, 1 in progress, 1 ready, "
                             "1 blocked, 1 quarantined, 1 retired, "
                             "0 shipped elsewhere")


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

    # The second argument is the PROCESS's own currency (loop-03), passed so the
    # "could not be read" branch can say which of the two failures the operator
    # has. It is never allowed to become a source of counts.
    assert "renderStats(d.stats, d.build && d.build.upgrade)" in script
    for field in ("s.line", "s.total", "s.open", "s.denominator", "s.percent_done",
                  "s.tiles", "t.label", "t.count", "s.open_by_priority",
                  "s.open_by_area", "c.completed", "c.retired",
                  "w.unpublished_candidate", "r.detail"):
        assert field in script, f"{field} never reaches the DOM"

    # The state tiles are rendered FROM the payload's labels, so the template
    # cannot spell a state itself and cannot put two states under one word. A
    # hard-coded tile list is the shape that let `blocked` mean the quarantine
    # up here while the Roadmap group below meant a dependency.
    block = script.split("function renderStats(s, up){", 1)[1].split("\nfunction ", 1)[0]
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
    assert body.index("sig === LASTJSON") < body.index("renderStats(d.stats,")


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


# ---- the dependency graph (2026-08-20) ---------------------------------------
#
# Measured 2026-08-19: 132 tasks, 150 `depends_on` edges, 41 tasks with at least
# one dependency. The roadmap is a flat list grouped by state, so "port-01 is
# blocked and six tasks are waiting behind it" was a fact an operator could only
# get by reading `tasks.json` by hand.
#
# The claim under test: every `depends_on` relation renders as one directed edge
# from dependency to dependent, laid out so a task appears after everything it
# depends on, with each node carrying its id and its current state.
#
# The traps these tests exist for, each of which a looser test would pass:
#   * A LAYOUT THAT IS REALLY AN ALPHABETICAL SORT. Every fixture below is built
#     so alphabetical order CONTRADICTS dependency order (`z-01` before `a-01`),
#     which is the only way `layer[dep] < layer[dependent]` says anything.
#   * A GRAPH BUILT FROM A REGISTRY. `TaskRegistry.from_dict` runs
#     `_check_acyclic`, so a cyclic file raises and `task_groups` returns `[]` —
#     a registry-derived graph would show nothing in exactly the case a cycle is
#     worth showing. The cyclic tests drive `collect()` end to end and assert
#     BOTH that the registry refused the file and that the panel still drew it.
#   * A CYCLE THAT HANGS. Nothing asserts termination directly; the test
#     completing is the assertion, which is why the layering runs over the
#     condensation rather than over a visited set.


def dep_graph(tasks: list[dict]) -> dict:
    """`dependency_graph` over raw rows, grouped the way `collect()` groups.

    `task_groups` is passed in exactly as the payload does it, so these tests
    exercise the same state source the page reads — including its `[]` answer
    for a file no registry will load.
    """
    data = {"tasks": tasks}
    return dependency_graph(data, task_groups(data, {}))


def dep_nodes(graph: dict) -> dict:
    return {node["id"]: node for node in graph["nodes"]}


def dep_pairs(graph: dict) -> list[tuple[str, str]]:
    return [(edge["from"], edge["to"]) for edge in graph["edges"]]


def test_every_depends_on_pair_becomes_exactly_one_edge():
    """One edge per relation, pointing from the dependency to the dependent —
    the direction work flows, so an arrow INTO a node is what it is waiting for.

    A dependency declared twice on the same task is one relation stated twice:
    the second edge would draw exactly on top of the first, so a duplicate could
    only ever inflate a count.
    """
    graph = dep_graph([
        roadmap_task("z-root"),
        roadmap_task("y-root"),
        roadmap_task("m-mid", depends_on=["z-root", "y-root"]),
        roadmap_task("a-leaf", depends_on=["m-mid", "m-mid"]),
    ])

    assert sorted(dep_pairs(graph)) == [
        ("m-mid", "a-leaf"), ("y-root", "m-mid"), ("z-root", "m-mid"),
    ]
    # Exactly one, not at least one: no duplicates and nothing invented.
    assert len(dep_pairs(graph)) == len(set(dep_pairs(graph))) == 3
    # Every declared pair is present — asserted from the fixture's own rows
    # rather than from the list above, so a dropped edge cannot pass by both
    # sides being wrong together.
    declared = {(dep, task["id"])
                for task in graph["nodes"] for dep in task["depends_on"]}
    assert declared == set(dep_pairs(graph))


def test_a_task_is_laid_out_after_everything_it_depends_on():
    """The layout claim. Alphabetical order contradicts dependency order here on
    purpose — `a-01` depends on `z-01` — so a layout that merely sorted by id
    fails, which is the whole reason the fixture reads backwards."""
    graph = dep_graph([
        roadmap_task("z-01"),
        roadmap_task("a-01", depends_on=["z-01"]),
        roadmap_task("m-01", depends_on=["a-01"]),
        roadmap_task("b-01", depends_on=["z-01", "m-01"]),
    ])

    layer = {node["id"]: node["layer"] for node in graph["nodes"]}
    order = [node["id"] for node in graph["nodes"]]
    for dep, dependent in dep_pairs(graph):
        assert layer[dep] < layer[dependent], f"{dependent} is drawn before {dep}"
        assert order.index(dep) < order.index(dependent)
    # A node's layer is one past the HIGHEST of its dependencies, so `b-01`
    # lands past `m-01` rather than beside `a-01` — the longest path, not the
    # first one found.
    assert layer == {"z-01": 0, "a-01": 1, "m-01": 2, "b-01": 3}
    assert order == ["z-01", "a-01", "m-01", "b-01"]
    assert graph["layers"] == 4
    # Rows are the position WITHIN a column, so one-per-column here.
    assert [node["row"] for node in graph["nodes"]] == [0, 0, 0, 0]


def test_isolated_tasks_are_omitted_and_the_count_is_reported():
    """91 of the 132 tasks are in no dependency relation at all; drawing them as
    isolated dots is what makes the 41 that matter unreadable. The omission is
    reported rather than silent, and the arithmetic is asserted so a task cannot
    be dropped from both sides at once."""
    graph = dep_graph([
        roadmap_task("alone-1"), roadmap_task("alone-2"), roadmap_task("alone-3"),
        roadmap_task("z-root"),
        roadmap_task("a-waits", depends_on=["z-root"]),
    ])

    assert [node["id"] for node in graph["nodes"]] == ["z-root", "a-waits"]
    assert graph["omitted"] == 3
    assert graph["shown"] == 2
    assert graph["total"] == 5
    assert graph["omitted"] + graph["shown"] == graph["total"]
    # ISOLATED means neither direction. A root declaring no dependency of its
    # own but carrying dependents is the most interesting node on the page —
    # it is what everything behind it is waiting for — and must never be
    # mistaken for one of the 91.
    assert dep_nodes(graph)["z-root"]["depends_on"] == []
    assert dep_nodes(graph)["z-root"]["dependents"] == 1


def test_a_cyclic_registry_renders_and_reports_the_cycle(tmp_path):
    """The bound the panel is easiest to get wrong. Nothing enforces acyclicity
    when a `tasks.json` is written by hand, and `TaskRegistry.from_dict` REFUSES
    such a file (`_check_acyclic`) — so every other panel degrades to "the task
    graph could not be read" and a registry-derived graph would show nothing at
    all in the one case a cycle is worth showing.

    Driven through `collect()` end to end, asserting both halves: the registry
    really did refuse the file, and the panel really did draw it. That the test
    RETURNS is the termination assertion — there is no timeout to hide behind.
    """
    repo = make_repo(tmp_path)
    write_registry(repo, [
        roadmap_task("cyc-a", depends_on=["cyc-b"]),
        roadmap_task("cyc-b", depends_on=["cyc-a"]),
        roadmap_task("alone-1"),
        roadmap_task("after-1", depends_on=["cyc-a"]),
    ])

    payload = collect(repo)
    graph = payload["depgraph"]

    # The registry refused the whole file — this is the state every other panel
    # renders as unreadable.
    assert payload["groups"] == []
    # NOT ONE EDGE IS DROPPED to make the picture acyclic, the cycle's own edge
    # included.
    assert sorted(dep_pairs(graph)) == [
        ("cyc-a", "after-1"), ("cyc-a", "cyc-b"), ("cyc-b", "cyc-a"),
    ]
    # The cycle is reported as a cycle, with a concrete path an operator can
    # take straight to the file.
    assert graph["cycles"] == [
        {"nodes": ["cyc-a", "cyc-b"], "path": ["cyc-a", "cyc-b", "cyc-a"]}
    ]
    nodes = dep_nodes(graph)
    assert nodes["cyc-a"]["cyclic"] is True and nodes["cyc-b"]["cyclic"] is True
    assert nodes["after-1"]["cyclic"] is False
    # Members of a cycle cannot be ordered against each other and are not
    # pretended to be — they share a column. Everything downstream still lands
    # after them, which is what the condensation buys.
    assert nodes["cyc-a"]["layer"] == nodes["cyc-b"]["layer"]
    assert nodes["after-1"]["layer"] > nodes["cyc-a"]["layer"]
    assert [edge["cycle"] for edge in graph["edges"] if edge["to"] == "after-1"] == [False]
    assert sorted(edge["cycle"] for edge in graph["edges"]) == [False, True, True]
    # The rest of the panel still answers: the isolated task is still counted
    # out, and the states say where they came from because state_of() could not
    # be asked at all.
    assert graph["omitted"] == 1 and graph["total"] == 4 and graph["shown"] == 3
    assert graph["states_from"] == "status"
    assert {node["id"]: node["state"] for node in graph["nodes"]} == {
        "cyc-a": "blocked", "cyc-b": "blocked", "after-1": "blocked",
    }


def test_a_self_dependency_is_reported_as_the_one_node_cycle_it_is():
    """`from_dict` bypasses `_validate_depends_on`, so a hand-edited self-edge
    reaches this page. It is a cycle of one, and it is the shape that vanishes
    most quietly: drawn naively it is a zero-length path, i.e. nothing."""
    graph = dep_graph([
        roadmap_task("solo-1", depends_on=["solo-1"]),
        roadmap_task("after-1", depends_on=["solo-1"]),
    ])

    assert graph["cycles"] == [{"nodes": ["solo-1"], "path": ["solo-1", "solo-1"]}]
    assert [(e["from"], e["to"], e["cycle"]) for e in graph["edges"]] == [
        ("solo-1", "solo-1", True), ("solo-1", "after-1", False),
    ]
    assert dep_nodes(graph)["solo-1"]["cyclic"] is True
    assert dep_nodes(graph)["after-1"]["layer"] > dep_nodes(graph)["solo-1"]["layer"]


def test_every_node_state_is_the_state_the_registry_reports(tmp_path):
    """Node state comes from `TaskRegistry.state_of()` — the same payload the
    Roadmap panel groups by, passed in rather than re-derived — so the graph and
    the list beside it cannot disagree about what a task is.

    An edge into a BLOCKED node is the interesting case and an edge into a
    COMPLETED one is satisfied history, so every state here is a distinct
    reading and each is asserted by name.
    """
    repo = make_repo(tmp_path)
    write_registry(repo, [
        roadmap_task("z-done", status="completed"),
        roadmap_task("a-wip", status="in_progress", depends_on=["z-done"]),
        roadmap_task("b-held", status="blocked", depends_on=["z-done"]),
        roadmap_task("c-gone", status="retired", depends_on=["z-done"]),
        roadmap_task("d-ready", depends_on=["z-done"]),
        roadmap_task("e-waits", depends_on=["d-ready"]),
    ])

    payload = collect(repo)
    graph = payload["depgraph"]
    from_groups = {task["id"]: group["state"]
                   for group in payload["groups"] for task in group["tasks"]}

    assert graph["states_from"] == "registry"
    assert {node["id"]: node["state"] for node in graph["nodes"]} == {
        "z-done": "completed", "a-wip": "in_progress",
        # `blocked` ON DISK means quarantined, and `blocked` in the graph means
        # waiting on an incomplete dependency — the two meanings this page must
        # never fold together.
        "b-held": "blocked_by_operator", "c-gone": "retired",
        "d-ready": "ready", "e-waits": "blocked",
    }
    for node in graph["nodes"]:
        assert node["state"] == from_groups[node["id"]], node["id"]
        assert node["state"] in DEP_NODE_STATES
    # Every drawn state is a real task's state, and every node carries the id
    # the registry knows it by.
    assert all(node["known"] for node in graph["nodes"])
    assert graph["unknown"] == []


def test_a_dependency_naming_no_task_is_drawn_rather_than_dropped(tmp_path):
    """What `from_dict` really tolerates and `state_of` then rejects: a
    dangling `depends_on`. `_check_acyclic` looks each dep up with `.get`, so an
    unknown id is not a cycle, and `state_of` later raises `KeyError` on it.

    The edge is the evidence, so it is kept and its far end is drawn in a state
    that says the registry cannot answer for it — never dropped to tidy the
    picture, and never given a state this page invented.
    """
    repo = make_repo(tmp_path)
    write_registry(repo, [roadmap_task("a-real", depends_on=["z-ghost"])])

    payload = collect(repo)
    graph = payload["depgraph"]

    assert payload["groups"] == [], "state_of raises on a dangling dependency"
    assert dep_pairs(graph) == [("z-ghost", "a-real")]
    assert graph["unknown"] == ["z-ghost"]
    nodes = dep_nodes(graph)
    assert nodes["z-ghost"]["state"] == "unknown" and nodes["z-ghost"]["known"] is False
    assert nodes["a-real"]["known"] is True
    assert nodes["a-real"]["layer"] > nodes["z-ghost"]["layer"]
    # The omitted/shown identity counts TASKS. A phantom is not one, so it may
    # not appear on either side of it.
    assert graph["total"] == 1 and graph["shown"] == 1 and graph["omitted"] == 0


def test_the_graph_is_display_only_and_changes_no_dependency(tmp_path):
    """Display only, said as a property. The rows the graph reads are the rows
    the roadmap reads, unaltered, and every relation on the page is one the
    registry already had — nothing here adds, drops or reorders a dependency."""
    repo = make_repo(tmp_path)
    rows = [
        roadmap_task("z-root"),
        roadmap_task("a-mid", depends_on=["z-root"]),
        roadmap_task("b-leaf", depends_on=["a-mid"]),
        roadmap_task("alone-1"),
    ]
    write_registry(repo, rows)

    payload = collect(repo)

    stored = {(dep, row["id"]) for row in rows for dep in row["depends_on"]}
    assert set(dep_pairs(payload["depgraph"])) == stored
    # The flat roadmap and the grouped read are what they were — this panel is
    # a third view of the same rows, not a fourth source of truth.
    assert {row["id"] for row in payload["roadmap"]} == {row["id"] for row in rows}
    assert sum(group["count"] for group in payload["groups"]) == len(rows)
    # And the registry the loop dispatches from still reads the file the same
    # way: the dependencies are exactly as declared.
    registry = TaskRegistry.from_dict({"tasks": rows})
    assert {(dep, task.id) for task in registry.all_tasks()
            for dep in task.depends_on} == stored


# ---- the two display filters --------------------------------------------------
#
# 39% of the drawing was settled history: measured 2026-08-20 over 141 tasks, 22
# of the 71 drawn nodes were completed and 6 retired. A node that can never hold
# anything up again is noise in a panel whose whole question is "what is held up,
# and by what", so each of those two states gets a control.
#
# The claims these tests hold down, and why each is the one that can rot:
#
#   * FILTER BEFORE LAYOUT. The filtered graph is laid out AGAIN — hiding a node
#     must not leave a hole in its layer or strand its dependents at a layer
#     number derived from something no longer on the page.
#   * HIDDEN IS NOT OMITTED. dash-14's `omitted` counts tasks in NO relation at
#     all and its identity is `omitted + shown == total`. Booking a hidden node
#     there would destroy that claim by making it true of a different set every
#     time a checkbox moved; `hidden` is its own count and
#     `shown + hidden + omitted == total` is the generalisation.
#   * NOTHING LIVE DISAPPEARS. A pending task whose only relations were to
#     hidden nodes is the one thing a filter can silently delete. It is kept.


def dep_view(graph: dict, *, completed: bool, retired: bool) -> dict:
    """The graph the page draws for one position of the two controls."""
    return graph["views"][dep_view_key(completed, retired)]


def filter_rows() -> list[dict]:
    """The fixture the filter tests read, built so nothing passes by accident.

    Each control has something ONLY it removes (`z-done`, `y-gone`); `b-live`
    waits on one of each, so it keeps an edge under either filter alone and is
    the node whose layer must move; `c-orphan`'s only relation is to a completed
    task, so it is the live task a filter could silently delete; and
    `d-done-alone` is completed AND in no relation, which is the row that tells
    `hidden` and `omitted` apart — it belongs to the second in every view.
    """
    return [
        roadmap_task("z-done", status="completed"),
        roadmap_task("y-gone", status="retired"),
        roadmap_task("a-live", depends_on=["z-done"]),
        roadmap_task("b-live", depends_on=["a-live", "y-gone"]),
        roadmap_task("c-orphan", depends_on=["z-done"]),
        roadmap_task("alone-1"),
        roadmap_task("d-done-alone", status="completed"),
    ]


def assert_layout_is_sound(view: dict, where: str = "") -> None:
    """dash-14's layout invariant, over whatever this view actually draws.

    Three claims, because the filtered case can break any of them on its own: a
    node is drawn after everything it depends on, the layers it is placed in are
    a contiguous run from 0 (a hidden node leaves no empty column), and the rows
    within each layer are a contiguous run from 0 (it leaves no gap in a column
    either).
    """
    layer = {node["id"]: node["layer"] for node in view["nodes"]}
    order = [node["id"] for node in view["nodes"]]
    for edge in view["edges"]:
        assert edge["from"] in layer and edge["to"] in layer, (where, edge)
        if edge["cycle"]:
            continue
        assert layer[edge["from"]] < layer[edge["to"]], (where, edge)
        assert order.index(edge["from"]) < order.index(edge["to"]), (where, edge)
    used = sorted({node["layer"] for node in view["nodes"]})
    assert used == list(range(view["layers"])), (where, "an empty column")
    for number in used:
        rows_here = sorted(node["row"] for node in view["nodes"]
                           if node["layer"] == number)
        assert rows_here == list(range(len(rows_here))), (where, "a gap in a column")


def test_each_filter_removes_exactly_its_own_state_and_every_edge_touching_it():
    """The provable claim, one control at a time and then both.

    An edge is a claim about two nodes and half of one is a line into nothing,
    so hiding a node takes every edge incident to it — in BOTH directions, which
    is why `y-gone` is a dependency of `b-live` while `z-done` is a dependency of
    two others. Each toggle is asserted to remove its own state and to leave the
    other's alone, because a filter that hid both from either checkbox would
    pass every "is it gone" assertion on its own.
    """
    graph = dep_graph(filter_rows())
    everything = dep_view(graph, completed=True, retired=True)
    no_done = dep_view(graph, completed=False, retired=True)
    no_gone = dep_view(graph, completed=True, retired=False)
    neither = dep_view(graph, completed=False, retired=False)

    assert [node["id"] for node in everything["nodes"]] == [
        "y-gone", "z-done", "a-live", "c-orphan", "b-live",
    ]
    assert sorted(dep_pairs(everything)) == [
        ("a-live", "b-live"), ("y-gone", "b-live"),
        ("z-done", "a-live"), ("z-done", "c-orphan"),
    ]

    # "show completed" off: the completed node and BOTH its edges, and nothing
    # else — the retired node is still drawn, still with its own edge.
    assert {node["id"] for node in no_done["nodes"]} == {
        "y-gone", "a-live", "b-live", "c-orphan"}
    assert sorted(dep_pairs(no_done)) == [("a-live", "b-live"), ("y-gone", "b-live")]
    # "show retired" off: the mirror image, asserted from the other side.
    assert {node["id"] for node in no_gone["nodes"]} == {
        "z-done", "a-live", "b-live", "c-orphan"}
    assert sorted(dep_pairs(no_gone)) == [
        ("a-live", "b-live"), ("z-done", "a-live"), ("z-done", "c-orphan")]
    # Both off: the 43-live-nodes case the whole feature exists for.
    assert {node["id"] for node in neither["nodes"]} == {"a-live", "b-live", "c-orphan"}
    assert sorted(dep_pairs(neither)) == [("a-live", "b-live")]

    # No edge anywhere names a node this view does not draw — the property, not
    # four hand-written lists of it.
    for view in graph["views"].values():
        drawn = {node["id"] for node in view["nodes"]}
        for edge in view["edges"]:
            assert edge["from"] in drawn and edge["to"] in drawn, edge
    # …and a node's own `depends_on` says what the picture says, so a tooltip
    # cannot name a dependency the drawing has no edge for.
    assert dep_nodes(no_done)["a-live"]["depends_on"] == []
    assert dep_nodes(everything)["a-live"]["depends_on"] == ["z-done"]


def test_the_filtered_graph_is_laid_out_again_rather_than_left_with_holes():
    """RE-LAYER, DO NOT PUNCH HOLES. `a-live` sits at layer 1 because it waits on
    a completed task; with completed hidden it is waiting on nothing DRAWN and
    belongs at the left edge, and `b-live` behind it must move with it.

    The generic invariant is asserted over all four views as well, since the
    interesting failure is not this chain moving but some other view keeping a
    layer number derived from a node no longer on the page.
    """
    graph = dep_graph(filter_rows())
    everything = dep_view(graph, completed=True, retired=True)
    neither = dep_view(graph, completed=False, retired=False)

    assert {n["id"]: n["layer"] for n in everything["nodes"]} == {
        "z-done": 0, "y-gone": 0, "a-live": 1, "c-orphan": 1, "b-live": 2}
    assert everything["layers"] == 3
    # Both filters on: the chain slides left by one and the column that held the
    # hidden nodes is gone rather than empty.
    assert {n["id"]: n["layer"] for n in neither["nodes"]} == {
        "a-live": 0, "c-orphan": 0, "b-live": 1}
    assert neither["layers"] == 2
    # Rows are the position WITHIN a column and are re-packed too: `c-orphan`
    # takes the row `z-done` used to leave for it.
    assert {n["id"]: n["row"] for n in neither["nodes"]} == {
        "a-live": 0, "c-orphan": 1, "b-live": 0}

    for key, view in graph["views"].items():
        assert_layout_is_sound(view, key)


def test_the_hidden_count_is_reported_and_the_accounting_holds_in_every_view():
    """SAY WHAT IS HIDDEN, and book it apart from what is omitted.

    dash-14's `omitted + shown == total` is a claim about tasks in NO relation at
    all; a hidden node counted there would silently redefine it. The identity
    here is the strict generalisation, and the default view is asserted to
    satisfy dash-14's original as well — that is the no-regression proof.

    `d-done-alone` is the row that makes the disjointness say something: it is
    completed AND isolated, so a filter that swept "every completed task" into
    `hidden` rather than only the drawn ones would count it twice — once as
    hidden and once as omitted — and break the sum in the two views that hide
    completed while passing the other two.
    """
    graph = dep_graph(filter_rows())

    for key, view in graph["views"].items():
        assert view["total"] == 7, key
        assert view["shown"] + view["hidden"] + view["omitted"] == view["total"], key
        # `omitted` is dash-14's number and cannot move: a control hides a node,
        # it cannot put a task into a relation or take one out of one.
        assert view["omitted"] == 2, key
        assert sum(view["hidden_by_state"].values()) == view["hidden"], key
        assert view["shown"] == len(view["nodes"]) - len(view["unknown"]), key

    everything = dep_view(graph, completed=True, retired=True)
    assert (everything["hidden"], everything["shown"]) == (0, 5)
    assert everything["hidden_by_state"] == {"completed": 0, "retired": 0}
    # dash-14's own identity, unchanged, in the view the page opens on.
    assert everything["omitted"] + everything["shown"] == everything["total"]
    assert dep_view(graph, completed=False, retired=True)["hidden_by_state"] == {
        "completed": 1, "retired": 0}
    assert dep_view(graph, completed=True, retired=False)["hidden_by_state"] == {
        "completed": 0, "retired": 1}
    assert dep_view(graph, completed=False, retired=False)["hidden_by_state"] == {
        "completed": 1, "retired": 1}
    assert dep_view(graph, completed=False, retired=False)["shown"] == 3

    # The figure each control's label carries: how many DRAWN nodes it governs,
    # the same in every view so the label does not move when it is clicked. The
    # second completed task is not counted — it is not drawn in any view, so no
    # checkbox can hide it, and claiming otherwise would advertise a change the
    # click cannot make.
    for view in graph["views"].values():
        assert view["filterable"] == {"completed": 1, "retired": 1}
    assert len([row for row in filter_rows() if row["status"] == "completed"]) == 2


def test_a_task_whose_only_relations_are_hidden_is_drawn_alone_not_dropped():
    """The one way a filter can silently delete live work.

    `c-orphan` is pending and its only relation is to a completed task. With
    completed hidden it has nothing left to connect to — and dropping it would
    take a live task off the page for the sole reason that what it was waiting
    for is finished, which is the opposite of what the panel is for. It is kept,
    it is listed, and it is counted as SHOWN rather than as either hidden or
    omitted.
    """
    graph = dep_graph(filter_rows())
    no_done = dep_view(graph, completed=False, retired=True)

    assert no_done["drawn_alone"] == ["c-orphan"]
    node = dep_nodes(no_done)["c-orphan"]
    assert node["depends_on"] == [] and node["dependents"] == 0
    assert node["layer"] == 0, "a node with nothing drawn upstream is at the left edge"
    assert "c-orphan" in {n["id"] for n in no_done["nodes"]}
    # Counted as drawn — not quietly moved into either of the two "not on the
    # page" buckets.
    assert no_done["shown"] == 4 and no_done["hidden"] == 1 and no_done["omitted"] == 2
    # In the unfiltered view NOTHING is drawn alone, and that is structural: a
    # node is in the connected subgraph precisely because an edge touches it.
    for key, view in graph["views"].items():
        for task_id in view["drawn_alone"]:
            assert not [e for e in view["edges"]
                        if task_id in (e["from"], e["to"])], (key, task_id)
    assert dep_view(graph, completed=True, retired=True)["drawn_alone"] == []
    # …and it is not an artefact of hiding completed: with retired hidden
    # instead, `c-orphan` keeps its edge and no one is left alone.
    assert dep_view(graph, completed=True, retired=False)["drawn_alone"] == []


def test_a_cycle_with_hidden_members_is_still_reported_over_what_remains():
    """CYCLES SURVIVE FILTERING. Two shapes in one hand-edited file, because
    they fail in opposite directions:

      * `cyc-a → z-done → cyc-b → cyc-a` is a three-node cycle whose middle
        member is completed. Hiding it must not hide the cycle — `cyc-a` and
        `cyc-b` still depend on each other, and a filtered graph that reported
        "no dependency cycle" would be telling an operator the file is fine.
      * `p-one ↔ q-done` exists ONLY through a completed node. Its edges are
        incident to a hidden node, so they go, and with them the cycle: there is
        no cycle left among the drawn nodes to report, and inventing one would
        be reporting a relation the drawing does not show.

    That the test RETURNS is the termination assertion — filtering must not hang
    the walk, and `_dep_cycle_path` walks the FILTERED `deps` map, so a path
    through a hidden node would be the other way this breaks.
    """
    graph = dep_graph([
        roadmap_task("cyc-a", depends_on=["cyc-b"]),
        roadmap_task("cyc-b", depends_on=["cyc-a", "z-done"]),
        roadmap_task("z-done", status="completed", depends_on=["cyc-a"]),
        roadmap_task("p-one", depends_on=["q-done"]),
        roadmap_task("q-done", status="completed", depends_on=["p-one"]),
    ])
    everything = dep_view(graph, completed=True, retired=True)
    no_done = dep_view(graph, completed=False, retired=True)

    # The registry refuses a file like this, so the states are the stored ones —
    # which is exactly the case this panel exists to draw.
    assert everything["states_from"] == "status"
    assert [cycle["nodes"] for cycle in everything["cycles"]] == [
        ["cyc-a", "cyc-b", "z-done"], ["p-one", "q-done"]]

    # The surviving cycle is still reported, with a path over what remains.
    assert no_done["cycles"] == [
        {"nodes": ["cyc-a", "cyc-b"], "path": ["cyc-a", "cyc-b", "cyc-a"]}]
    assert {n["id"]: n["cyclic"] for n in no_done["nodes"]} == {
        "cyc-a": True, "cyc-b": True, "p-one": False}
    # No path names a node that is not drawn, and no edge does either.
    drawn = {node["id"] for node in no_done["nodes"]}
    for cycle in no_done["cycles"]:
        assert set(cycle["path"]) <= drawn and set(cycle["nodes"]) <= drawn
    assert sorted(dep_pairs(no_done)) == [("cyc-a", "cyc-b"), ("cyc-b", "cyc-a")]
    assert [edge["cycle"] for edge in no_done["edges"]] == [True, True]
    # The cycle that existed only through the hidden node is gone with its edges,
    # and the live task it held is drawn alone rather than dropped.
    assert no_done["drawn_alone"] == ["p-one"]
    # Members of a cycle still share a column, and the layout still holds.
    assert len({n["layer"] for n in no_done["nodes"] if n["cyclic"]}) == 1
    assert_layout_is_sound(no_done)


def test_a_phantom_dependency_survives_the_filters_rather_than_vanishing():
    """The node class that cannot defend itself: an id something depends on that
    is not a task at all. Its state is `unknown`, so no control governs it — but
    its only edge here comes from a completed task, and a `unknown` list read off
    the surviving EDGES rather than off the drawn nodes would drop it the moment
    that task was hidden. Silently, since it is not a task and appears in none of
    the three counts.
    """
    graph = dep_graph([
        roadmap_task("z-done", status="completed", depends_on=["ghost-1"]),
        roadmap_task("a-live", depends_on=["z-done"]),
    ])

    for key, view in graph["views"].items():
        assert view["unknown"] == ["ghost-1"], key
        assert "ghost-1" in {node["id"] for node in view["nodes"]}, key
        # A phantom is not a task, so it is on neither side of the identity.
        assert view["total"] == 2, key
        assert view["shown"] + view["hidden"] + view["omitted"] == 2, key

    no_done = dep_view(graph, completed=False, retired=True)
    assert no_done["edges"] == [], "both edges were incident to the hidden node"
    assert no_done["drawn_alone"] == ["a-live", "ghost-1"]
    assert (no_done["shown"], no_done["hidden"], no_done["omitted"]) == (1, 1, 0)
    assert dep_nodes(no_done)["ghost-1"]["state"] == "unknown"


def test_the_payload_carries_four_laid_out_views_and_opens_on_the_whole_graph():
    """The shape the page reads, and the default it opens on.

    Both controls start ON: the panel opens on the whole connected subgraph, so
    a task is never missing for a reason nobody asked for. That the top level IS
    the default view is what keeps every reader that predates the controls —
    dash-14's tests among them — reading the same graph as the page.
    """
    graph = dep_graph(filter_rows())

    assert DEP_DEFAULT_VIEW == dep_view_key(True, True) == "c1r1"
    assert set(graph["views"]) == {"c1r1", "c1r0", "c0r1", "c0r0"}
    assert graph["view"] == DEP_DEFAULT_VIEW
    default = graph["views"][DEP_DEFAULT_VIEW]
    assert {key: graph[key] for key in default} == default
    assert default["hidden"] == 0, "the page opens on the whole graph"
    # The two states a control governs are `TaskState` values, not strings this
    # page invented — the same rule `DEP_NODE_STATES` follows.
    assert DEP_FILTERS == (("completed", TaskState.COMPLETED.value),
                           ("retired", TaskState.RETIRED.value))
    assert {value for _name, value in DEP_FILTERS} <= set(DEP_NODE_STATES)


def test_filtering_is_display_only_and_moves_no_task(tmp_path):
    """DISPLAY ONLY, said as a property rather than as a promise. Every view is
    a reading of the SAME rows: no view invents a relation, none drops one the
    registry has for a task it draws, and the registry the loop actually
    dispatches from answers exactly as it did — same states, same `next_ready()`
    order — no matter which view the page is showing.
    """
    repo = make_repo(tmp_path)
    rows = filter_rows()
    write_registry(repo, rows)
    # What the loop would dispatch BEFORE the page has ever drawn the file.
    before = TaskRegistry.from_dict({"tasks": [dict(row) for row in rows]})
    picked = before.next_ready().id

    payload = collect(repo)
    graph = payload["depgraph"]
    stored = {(dep, row["id"]) for row in rows for dep in row["depends_on"]}

    for key, view in graph["views"].items():
        drawn = {node["id"] for node in view["nodes"]}
        pairs = set(dep_pairs(view))
        assert pairs <= stored, key
        # Nothing is dropped BETWEEN two drawn nodes either: a filter removes
        # edges by removing their endpoints and never for any other reason.
        assert pairs == {(dep, dependent) for dep, dependent in stored
                         if dep in drawn and dependent in drawn}, key

    # The registry is untouched by having been drawn: same state per task, and
    # the same task dispatched next.
    registry = TaskRegistry.from_dict({"tasks": [dict(row) for row in rows]})
    assert {task.id: registry.state_of(task.id) for task in registry.all_tasks()} == {
        "z-done": TaskState.COMPLETED, "y-gone": TaskState.RETIRED,
        "a-live": TaskState.READY, "b-live": TaskState.BLOCKED,
        "c-orphan": TaskState.READY, "alone-1": TaskState.READY,
        "d-done-alone": TaskState.COMPLETED,
    }
    assert registry.next_ready().id == picked, "drawing the file changed dispatch"
    # …and the file on disk still says what it said.
    stored_now = json.loads(
        (repo / ".autoloop" / "tasks.json").read_text(encoding="utf-8"))
    assert stored_now["tasks"] == rows


def test_every_node_state_has_an_icon_and_a_word_on_the_page():
    """A state with no mark renders a blank node. `DEP_NODE_STATES` is derived
    from `GROUPS`, so a seventh `TaskState` reaches the page automatically — and
    this is what stops it reaching the page unlabelled."""
    assert DEP_NODE_STATES == tuple(state.value for _k, _l, state in GROUPS) + ("unknown",)
    script = PAGE.split("<script>", 1)[1]
    marks = script.split("const DMARK = {", 1)[1].split("};", 1)[0]
    fills = script.split("const DFILL = {", 1)[1].split("};", 1)[0]
    order = script.split("const DORDER = [", 1)[1].split("];", 1)[0]
    for state in DEP_NODE_STATES:
        assert f"{state}:[" in marks, f"{state} has no icon and word"
        assert f"{state}:" in fills, f"{state} has no fill"
        assert f'"{state}"' in order, f"{state} would never reach the legend"
    # `blocked` is the word this page may not leave short anywhere it has room:
    # on disk it means QUARANTINED, and here it means waiting on a dependency —
    # the two-states-one-word confusion `TaskState` was split up to end, and the
    # reason `STAT_BUCKETS` already ships "blocked on a dependency".
    assert 'blocked:"blocked on a dependency"' in script
    assert ("blocked", "blocked on a dependency", "blocked") in STAT_BUCKETS


def test_the_dependency_panel_is_static_markup_the_refresh_never_reopens():
    """dash-12's rule, as wiring: the elements an operator holds are STATIC
    markup no render rebuilds, and the redraw itself goes through ONE gate that
    carries this panel's own signature rather than the page-wide one.

    The negatives are the load-bearing half. The positives alone pass for a
    render that snaps the table shut every two seconds, for a second `renderDeps`
    call site that no gate covers, and for a gate that is never reached because
    the page-wide guard returned first. What the gate DOES — what it redraws and
    what it holds — is measured under node in the tests below; this pins the
    wiring those cannot reach.
    """
    static_markup, script = PAGE.split("<script>", 1)

    for marker in ('<section id="deppanel">', '<svg id="depsvg"', 'id="depedges"',
                   'id="depnodes"', '<details id="deptablebox">', 'id="deptablesum"',
                   'id="deptable"', 'id="depnote"', 'id="deplegend"',
                   'id="depdetail"'):
        assert marker in static_markup, f"{marker} is not on the page"
    # The count of what was left out renders ABOVE the drawing: an omission
    # nobody scrolls to is an omission nobody sees.
    assert static_markup.index('id="depnote"') < static_markup.index('id="depsvg"')

    body = script.split("function renderDeps(d){", 1)[1].split("\n}", 1)[0]
    assert ".open" not in body, "a render that writes .open snaps the table shut every 2s"
    assert "deptablebox" not in body, "the disclosure element itself is never rebuilt"
    # Hand-built SVG, like `drawFlow`: nothing fetched at runtime, no library.
    assert "fetch(" not in body and "<script" not in body
    for external in ("<link", "@import", 'src="http'):
        assert external not in PAGE

    # `renderDeps` has exactly ONE caller — the definition and the call inside
    # `updateDeps`. A second call site is a redraw nothing guards, which is the
    # whole failure this panel had.
    assert script.count("renderDeps(") == 2, "definition plus the one guarded call"
    assert "renderDeps(d);\n  DEPBIND(d);" in script
    # The hook defaults to a no-op so the region can be lifted on its own. An
    # unassigned hook binds no listener on the real page while every node test
    # below still passes, so the assignment is pinned here instead.
    assert "DEPBIND = bindDeps;" in script

    guarded = script.split("function render(d, force){", 1)[1]
    # The INVERSE of the old ordering, deliberately: the panel is updated BEFORE
    # `sig === LASTJSON` because it carries its own signature. Sitting behind the
    # page-wide guard meant any unrelated figure moving rebuilt this graph — and
    # meant a tick that changed nothing else could never deliver a payload held
    # during a gesture.
    assert guarded.index("updateDeps(d);") < guarded.index("sig === LASTJSON")
    assert "renderDeps(" not in guarded and "bindDeps(" not in guarded
    # …and with NO `force`. `render(LAST, true)` is also raised by a click on a
    # PIPELINE node, so forwarding it would let an unrelated click replace the
    # dependency node someone is hovering.
    assert "updateDeps(d, force)" not in guarded

    binder = script.split("function bindDeps(d){", 1)[1].split("\n}\n", 1)[0]
    assert "render(LAST, true)" not in binder, "selecting a node rebuilds the page"
    assert "updateDeps(DEPDRAWN || LAST, true)" in binder
    # Selection survives a rebuild the way the pipeline's does. The GATE is
    # lifted with the pure region (the harnesses below run it); the listeners are
    # not, because they reach `render`, `LAST` and `showTip` — none of which the
    # node harness has.
    region = deps_panel_js()
    assert "let DSEL = null;" in region, "the region is not self-contained"
    assert "function updateDeps(d, force){" in region
    assert "function depsBusy(){" in region
    assert "showTip" not in region and "function bindDeps" not in region
    assert "function bindDeps(d){" in script and "DSEL = DSEL === n.id" in script


def deps_panel_js() -> str:
    """The dependency panel's own code, lifted verbatim out of the served page.

    `esc` and `rows` come along because the region depends on them; it carries
    no other module state, which is what lets it run against a stub document
    instead of a browser.
    """
    script = PAGE.split("<script>", 1)[1]
    lines = script.splitlines()
    esc_line = next(line for line in lines if line.startswith("const esc ="))
    rows_line = next(line for line in lines if line.startswith("const rows ="))
    region = script.split("// DEPGRAPH_START", 1)[1].split("// DEPGRAPH_END", 1)[0]
    return "\n".join((esc_line, rows_line, region))


def test_the_page_draws_one_edge_per_pair_and_says_what_it_left_out():
    """A payload carrying the graph is not a page showing it, so this RUNS the
    panel's own render against a stub document — twice, with the edge table
    opened and a node selected in between.

    What it tests is `renderDeps` ITSELF: what one draw puts on the page, and
    that a draw destroys neither the operator's disclosure nor their selection.
    It is NOT a test of the refresh — two direct calls with the same data cannot
    show that a poll leaves a panel alone, because the render they call is the
    thing that would replace it. The gate that decides whether a draw happens at
    all, and the interaction rule, are measured under "the refresh gate" below.

    The fixture is the hard case on purpose: a cycle, a task behind it, a root
    that nothing waits on, and an isolated task that must not be drawn.
    """
    graph = dependency_graph({"tasks": [
        roadmap_task("z-root"),
        roadmap_task("cyc-a", depends_on=["cyc-b"]),
        roadmap_task("cyc-b", depends_on=["cyc-a"]),
        roadmap_task("a-leaf", depends_on=["z-root", "cyc-a"]),
        roadmap_task("alone-1"),
    ]})
    payload = json.dumps({"depgraph": graph})

    harness = deps_panel_js() + """
const NODES = {};
for (const id of ["depnote","depsvg","depedges","depnodes","deplegend",
                  "deptablesum","deptable","depdetail","deptablebox"])
  NODES[id] = {innerHTML:"", textContent:"", open:false, attrs:{},
               setAttribute(key, value){ this.attrs[key] = value; }};
const document = {getElementById: id => NODES[id]};
const PAYLOAD = __PAYLOAD__;
renderDeps(PAYLOAD);
// The operator opens the edge table and clicks a node, then a draw happens.
NODES.deptablebox.open = true;
DSEL = "cyc-a";
renderDeps(PAYLOAD);
console.log(JSON.stringify({
  open: NODES.deptablebox.open,
  note: NODES.depnote.innerHTML,
  edges: NODES.depedges.innerHTML,
  nodes: NODES.depnodes.innerHTML,
  table: NODES.deptable.innerHTML,
  summary: NODES.deptablesum.textContent,
  detail: NODES.depdetail.textContent,
  legend: NODES.deplegend.innerHTML,
  box: NODES.depsvg.attrs,
}));
""".replace("__PAYLOAD__", payload)
    out = json.loads(run_js(harness))

    # ONE path per edge, the cycle's own included — four edges, four paths.
    assert len(graph["edges"]) == 4
    assert out["edges"].count('<path class="dedge') == 4
    assert out["edges"].count('dedge cyc') == 2, "a cycle edge is marked, not dropped"
    # Every drawn node carries its id AND its state as text, never colour alone.
    for task_id, word in (("z-root", "○ ready"), ("cyc-a", "◍ blocked"),
                          ("cyc-b", "◍ blocked"), ("a-leaf", "◍ blocked")):
        assert f'data-id="{task_id}"' in out["nodes"]
        assert f">{task_id}</text>" in out["nodes"]
        assert word in out["nodes"]
    assert "alone-1" not in out["nodes"], "an isolated task is not drawn"
    # …and the page SAYS it left it out, with the total beside it.
    assert "1 of 5 task(s) are in no dependency relation at all" in out["note"]
    assert "4 dependency edge(s)" in out["note"] and "4 node(s)" in out["note"]
    # The cycle is named, with its path, rather than being a gap in the drawing.
    assert "1 dependency cycle(s)" in out["note"]
    assert "cyc-a → cyc-b → cyc-a" in out["note"]
    assert "in a cycle" in out["nodes"]
    # The viewBox is written from the payload's OWN layout. The static markup
    # ships a placeholder, and a graph left on it is clipped or scaled away —
    # so this asserts the property (it was recomputed, and it is positive on
    # both axes) rather than a pixel count that would fail on arithmetic the
    # moment anyone tunes a node's width.
    assert '<svg id="depsvg" viewBox="0 0 1140 90">' in PAGE
    assert out["box"]["viewBox"] != "0 0 1140 90"
    drawn_w, drawn_h = out["box"]["viewBox"].split()[2:]
    assert float(drawn_w) > 0 and float(drawn_h) > 0
    # …and the layout it was computed from is the one the fixture describes:
    # two columns, the taller of them three nodes deep.
    assert graph["layers"] == 2
    assert max(node["row"] for node in graph["nodes"]) + 1 == 3
    # The table view is the readable twin: one row per edge, marked the same
    # way, and each state column headed by WHOSE state it is.
    assert out["table"].count("<tr>") == 5, "a header row plus one row per edge"
    assert out["table"].count("⚠ in a dependency cycle") == 2
    assert "<th>dependency state</th>" in out["table"]
    assert "<th>waiting-task state</th>" in out["table"]
    assert out["summary"] == "Table view — all 4 edge(s) as text"
    # A draw writes neither the disclosure state nor the selection away: the
    # table is still open and the clicked node is still the selected one. That
    # a draw does not HAPPEN under an operator is the gate's job, below.
    assert out["open"] is True
    assert '<g class="dnode sel cyc"' in out["nodes"]
    assert out["detail"] == (
        "cyc-a — blocked on a dependency; depends on cyc-b; 2 task(s) depend on it"
    )
    # The legend names every state that is actually on screen, icon and word —
    # and it spells `blocked` OUT. `blocked` on disk means quarantined and
    # `blocked` here means waiting on a dependency; the 126px node box cannot
    # hold the wide spelling, so every context that can hold it carries it.
    assert "◍ blocked on a dependency" in out["legend"]
    assert "○ ready" in out["legend"] and "⚠ in a cycle" in out["legend"]
    assert "blocked on a dependency" in out["table"]
    # …while the box itself keeps the short word, because it has room for
    # nothing else.
    assert "◍ blocked</text>" in out["nodes"]


def test_the_page_says_so_when_no_task_declares_a_dependency():
    """An empty graph and a broken panel must not look the same — the one panel
    on this page that could render nothing legitimately."""
    graph = dependency_graph({"tasks": [roadmap_task("alone-1"),
                                        roadmap_task("alone-2")]})
    payload = json.dumps({"depgraph": graph})

    harness = deps_panel_js() + """
const NODES = {};
for (const id of ["depnote","depsvg","depedges","depnodes","deplegend",
                  "deptablesum","deptable","depdetail"])
  NODES[id] = {innerHTML:"", textContent:"", open:false, attrs:{},
               setAttribute(key, value){ this.attrs[key] = value; }};
const document = {getElementById: id => NODES[id]};
renderDeps(__PAYLOAD__);
console.log(JSON.stringify({note: NODES.depnote.innerHTML,
                            nodes: NODES.depnodes.innerHTML,
                            table: NODES.deptable.innerHTML,
                            detail: NODES.depdetail.textContent}));
""".replace("__PAYLOAD__", payload)
    out = json.loads(run_js(harness))

    assert graph["nodes"] == [] and graph["edges"] == [] and graph["cycles"] == []
    assert graph["omitted"] == 2 and graph["shown"] == 0
    assert "no task declares a dependency" in out["note"]
    assert "2 of 2 task(s) are in no dependency relation at all" in out["note"]
    assert "✓ no dependency cycle" in out["note"]
    assert out["nodes"] == ""
    assert '<p class="empty">none</p>' in out["table"]
    assert out["detail"] == "no task selected"


# ---- the refresh gate ---------------------------------------------------------
#
# dash-12's rule is that an element the operator is interacting with is not
# replaced by a tick, and the four tests below are what MEASURE it rather than
# asserting the shape of the code around it. Two things make them say anything:
#
#   * NON-REPLACEMENT IS PROVEN BY A SENTINEL, never by comparing one render's
#     output with another's. A render that rebuilt everything from an identical
#     payload produces an identical string — so "the HTML is the same after the
#     tick" is exactly what the bug looks like too. Here the panel's DOM is
#     overwritten with a sentinel AFTER a draw; if the tick redraws, the
#     sentinel is gone.
#   * EVERY NEGATIVE IS PAIRED WITH ITS POSITIVE. A `depsBusy` wired to return
#     true forever, or a signature that never changes, passes every "was not
#     replaced" assertion in this file. So each test also releases the gesture
#     (or changes the graph) and asserts the redraw DOES land.
DEPS_STUB_JS = """
const NODES = {};
for (const id of ["depnote","depsvg","depedges","depnodes","deplegend",
                  "deptablesum","deptable","depdetail","deptablebox"])
  NODES[id] = {innerHTML:"", textContent:"", open:false, attrs:{},
               setAttribute(key, value){ this.attrs[key] = value; }};
// The three gestures `depsBusy` asks about, each switchable from a test: a node
// with keyboard focus, a node under the pointer, and the node a text selection
// is anchored in. `document.hasFocus` is deliberately absent — the stub is the
// foreground window.
const HOLD = {focus:null, hover:null, anchor:null};
const INSIDE = {inPanel:true};
NODES.deppanel = {
  contains: el => !!el && el.inPanel === true,
  querySelector: sel => (sel.indexOf(":hover") >= 0 ? HOLD.hover : null),
};
// Where `document.activeElement` sits when nobody has focused anything. It is
// not an interaction with anything, and a guard that read it as one would freeze
// this panel permanently on any page nobody has clicked.
const BODY = {};
const document = {getElementById: id => NODES[id] || null, body: BODY,
                  get activeElement(){ return HOLD.focus; }};
const window = {getSelection: () => HOLD.anchor
  ? {rangeCount: 1, isCollapsed: false, anchorNode: HOLD.anchor} : null};
// Every element `renderDeps` writes. `intact` is ALL of them, because a redraw
// that touched one of them touched the operator's DOM.
const OWNED = ["depnote","depedges","depnodes","deplegend","deptablesum",
               "deptable","depdetail"];
const stamp = mark => OWNED.forEach(id => {
  NODES[id].innerHTML = mark; NODES[id].textContent = mark; });
const intact = mark => OWNED.every(id =>
  NODES[id].innerHTML === mark && NODES[id].textContent === mark);
const shows = id => NODES.depnodes.innerHTML.includes('data-id="' + id + '"');
"""


def deps_gate_js(**payloads: str) -> str:
    """The panel's own code plus the stub, with each payload bound to a const.

    The gate (`updateDeps`/`depsBusy`) lives INSIDE `DEPGRAPH_START`/`END` for
    this reason: the interaction rule is behaviour, and behaviour that only the
    browser can run is behaviour nothing in CI checks.
    """
    consts = "".join(f"const {name} = {body};\n" for name, body in payloads.items())
    return deps_panel_js() + DEPS_STUB_JS + consts


def dep_payload(graph: dict, iteration: int, agents: list) -> str:
    """A whole-dashboard payload, not a bare graph: the failure being fixed is a
    tick in which something ELSE moved, so the fixture has to carry something
    else that can move."""
    return json.dumps({"depgraph": graph, "session": {"iteration": iteration},
                       "agents": agents, "served_at": "12:00:00"})


def test_an_unrelated_payload_change_does_not_redraw_the_dependency_panel():
    """Panel-local change detection. The dependency graph moves when a task is
    added, merged or retired — minutes or hours apart — while the payload around
    it moves every two seconds (an agent starts, the iteration counter ticks).
    Redrawing this panel for those was throwing away a hovered node, a focus ring
    and any selected text in the edge table, dozens of times an hour, for a graph
    that had not changed by one edge.

    The opened edge table is here too: a disclosure is NOT an interaction that
    blocks a redraw — nothing rebuilds it — so a real dependency change must
    still land while it is open.
    """
    same = dependency_graph({"tasks": [
        roadmap_task("z-root"),
        roadmap_task("a-leaf", depends_on=["z-root"]),
    ]})
    grown = dependency_graph({"tasks": [
        roadmap_task("z-root"),
        roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("b-new", depends_on=["a-leaf"]),
    ]})

    harness = deps_gate_js(
        FIRST=dep_payload(same, 4, []),
        POLL=dep_payload(same, 5, [{"id": "agent-1"}]),
        LATER=dep_payload(grown, 6, [{"id": "agent-1"}]),
    ) + """
updateDeps(FIRST);
const drew = shows("a-leaf");
// The operator's own DOM, and the edge table opened under it.
stamp("HELD");
NODES.deptablebox.open = true;
// A 2s tick in which an agent appeared and the iteration moved. Not one
// dependency changed.
updateDeps(POLL);
const afterUnrelated = intact("HELD");
// …and a tick in which the graph itself grew, with focus where it sits when
// nobody has focused anything.
HOLD.focus = BODY;
updateDeps(LATER);
console.log(JSON.stringify({drew, afterUnrelated,
  redrew: !intact("HELD"), grew: shows("b-new"),
  open: NODES.deptablebox.open}));
"""
    out = json.loads(run_js(harness))

    assert out["drew"] is True, "the first payload must draw"
    assert out["afterUnrelated"] is True, "an unrelated change rebuilt the panel"
    assert out["redrew"] is True, "a real dependency change must land"
    assert out["grew"] is True
    assert out["open"] is True, "the redraw closed the operator's edge table"


def test_a_focused_dependency_node_is_not_replaced_while_it_is_focused():
    """The gesture the graph's own keyboard support creates: a node is focused,
    it carries the focus ring and its tooltip, and `bindDeps` has hung four
    listeners on that exact element. Replacing it mid-tick drops the operator
    back to the top of the document.

    Two ticks are held, not one — a guard that only survived the first would
    still fail anyone reading for longer than two seconds — and the release is
    asserted, because a guard stuck at "busy" passes the first half alone.
    """
    before = dependency_graph({"tasks": [
        roadmap_task("z-root"),
        roadmap_task("a-leaf", depends_on=["z-root"]),
    ]})
    after = dependency_graph({"tasks": [
        roadmap_task("z-root"),
        roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("b-new", depends_on=["a-leaf"]),
    ]})

    harness = deps_gate_js(
        FIRST=dep_payload(before, 4, []),
        LATER=dep_payload(after, 5, []),
    ) + """
updateDeps(FIRST);
stamp("HELD");
// The operator tabs onto a node, and the graph changes underneath them.
HOLD.focus = INSIDE;
updateDeps(LATER);
const tick1 = intact("HELD");
updateDeps(LATER);
const tick2 = intact("HELD");
// They tab away. The next poll is what delivers what was held — no listener
// needed for it to land.
HOLD.focus = null;
updateDeps(LATER);
console.log(JSON.stringify({tick1, tick2,
  delivered: shows("b-new"), redrew: !intact("HELD")}));
"""
    out = json.loads(run_js(harness))

    assert out["tick1"] is True, "the focused node was replaced by a tick"
    assert out["tick2"] is True, "held for one tick only"
    assert out["redrew"] is True and out["delivered"] is True, (
        "the held payload never landed after the interaction ended"
    )


def test_a_hovered_node_and_a_selection_in_the_table_hold_the_redraw_too():
    """The other two gestures, in the order an operator makes them. Hover is the
    one the tooltip follows, and a selection in the edge table is someone
    copying a task id out of it — the reason the table exists.

    Chained rather than run as two fixtures on purpose: the second gesture holds
    a graph that changed AGAIN, so this also shows the gate has no memory of
    having been released once.
    """
    one = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"])]})
    two = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("b-new", depends_on=["a-leaf"])]})
    three = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("b-new", depends_on=["a-leaf"]),
        roadmap_task("c-new", depends_on=["b-new"])]})

    harness = deps_gate_js(
        ONE=dep_payload(one, 4, []),
        TWO=dep_payload(two, 5, []),
        THREE=dep_payload(three, 6, []),
    ) + """
updateDeps(ONE);
stamp("HOVER");
HOLD.hover = INSIDE;              // the pointer is over a node
updateDeps(TWO);
const hovering = intact("HOVER");
HOLD.hover = null;                // the pointer leaves
updateDeps(TWO);
const afterHover = shows("b-new");

stamp("SELECT");
// A text selection anchored in a text node inside the panel — the edge table.
HOLD.anchor = {nodeType: 3, parentNode: INSIDE};
updateDeps(THREE);
const selecting = intact("SELECT");
HOLD.anchor = null;
updateDeps(THREE);
console.log(JSON.stringify({hovering, afterHover, selecting,
                            afterSelect: shows("c-new")}));
"""
    out = json.loads(run_js(harness))

    assert out["hovering"] is True, "a hovered node was replaced by a tick"
    assert out["afterHover"] is True, "the held payload never landed after hover"
    assert out["selecting"] is True, "selected table text was replaced by a tick"
    assert out["afterSelect"] is True, "the held payload never landed after selecting"


def test_selecting_a_node_redraws_the_graph_that_is_on_screen_not_the_held_one():
    """The one caller that forces a redraw: the node picker, which changed `DSEL`
    and needs the highlight to move under the operator's cursor. It is a gesture,
    so it must not become the way a held graph slips in underneath — it redraws
    `DEPDRAWN`, the payload already on screen.

    Then the held one still lands on the following tick, which is the half that
    would rot silently: swallowing it here would leave the panel permanently one
    dependency change behind, and nothing would say so.
    """
    before = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"])]})
    after = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("b-new", depends_on=["a-leaf"])]})

    harness = deps_gate_js(
        FIRST=dep_payload(before, 4, []),
        LATER=dep_payload(after, 5, []),
    ) + """
updateDeps(FIRST);
HOLD.hover = INSIDE;
updateDeps(LATER);            // held: the pointer is on a node
stamp("HELD");
// What `bindDeps`'s picker does, spelled out — the listeners themselves live
// outside the region this harness runs.
DSEL = "a-leaf";
updateDeps(DEPDRAWN, true);
const picked = {redrew: !intact("HELD"),
                selected: NODES.depnodes.innerHTML.includes('class="dnode sel"'),
                slipped: shows("b-new")};
HOLD.hover = null;
updateDeps(LATER);
console.log(JSON.stringify({picked, delivered: shows("b-new")}));
"""
    out = json.loads(run_js(harness))

    assert out["picked"]["redrew"] is True, "the click did not move the selection"
    assert out["picked"]["selected"] is True
    assert out["picked"]["slipped"] is False, (
        "a forced redraw let the held graph in under the operator's cursor"
    )
    assert out["delivered"] is True, "the forced redraw swallowed the held payload"


def test_a_held_graph_the_server_stopped_reporting_is_never_delivered():
    """A held payload has to be dropped when it goes OBSOLETE, not only when the
    draw that lands is it. A graph changes mid-gesture (b-new appears) and is
    held; before the operator lets go, b-new is retired and the next poll carries
    the graph ALREADY ON SCREEN. Keeping the superseded one meant `settle`
    rendered it the moment the gesture ended — the panel showing a task the
    registry no longer has, until the following tick took it away again.

    The rule that fixes it is one rule, and the two halves are chained here
    because a narrower version passes either half alone: a payload arriving
    without `force` is the newest the page has, so whatever differs from it is
    history — whether the screen already shows that payload (first half) or it is
    about to be drawn (second half, reached by a selection collapsed OUTSIDE the
    panel, which no listener sees). The paired positive is in both: a gate that
    simply discarded every held payload would pass every negative on its own.
    """
    first = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"])]})
    grown = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("b-new", depends_on=["a-leaf"])]})
    newer = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("c-new", depends_on=["a-leaf"])]})
    latest = dependency_graph({"tasks": [
        roadmap_task("z-root"), roadmap_task("a-leaf", depends_on=["z-root"]),
        roadmap_task("c-new", depends_on=["a-leaf"]),
        roadmap_task("d-new", depends_on=["c-new"])]})

    harness = deps_gate_js(
        FIRST=dep_payload(first, 4, []),
        GROWN=dep_payload(grown, 5, []),
        NEWER=dep_payload(newer, 6, []),
        LATEST=dep_payload(latest, 7, []),
    ) + """
updateDeps(FIRST);
HOLD.hover = INSIDE;          // the pointer goes onto a node
updateDeps(GROWN);            // held: the graph grew mid-gesture
stamp("HELD");
// b-new is retired while the operator is still holding the panel, so this poll
// carries the graph that is already drawn. Nothing to redraw — and nothing left
// worth delivering either.
updateDeps(FIRST);
const backToFirst = {intact: intact("HELD"), cleared: DEPHELD === null};
// The pointer leaves. What the page's `settle` listener does, spelled out: it is
// bound outside the region this harness lifts, so nothing here would run it.
HOLD.hover = null;
if (DEPHELD) updateDeps(DEPHELD);
const settled = {intact: intact("HELD"), stale: shows("b-new")};
// …and a real change still lands on the next tick. `staleAfter` reads a DOM
// that was actually rendered, unlike the sentinel-stamped one above.
updateDeps(NEWER);
const landed = {redrew: !intact("HELD"), delivered: shows("c-new"),
                staleAfter: shows("b-new")};

// The other half of the same rule: the superseding payload need not be the one
// already on screen. A selection collapsed OUTSIDE the panel ends the gesture
// with no listener firing, so the next tick simply draws a further change — and
// the graph held behind it is just as obsolete.
stamp("BASE");
HOLD.anchor = {nodeType: 3, parentNode: INSIDE};
updateDeps(GROWN);            // held again
const heldAgain = intact("BASE");
HOLD.anchor = null;           // collapsed outside the panel; no settle fires
updateDeps(LATEST);           // draws, and must drop what was held
const drawPath = {cleared: DEPHELD === null, drew: shows("d-new"),
                  stale: shows("b-new")};
stamp("CURRENT");
if (DEPHELD) updateDeps(DEPHELD);
console.log(JSON.stringify({backToFirst, settled, landed, drawPath, heldAgain,
                            afterSettle: intact("CURRENT")}));
"""
    out = json.loads(run_js(harness))

    assert out["backToFirst"]["intact"] is True, (
        "the graph already on screen was redrawn anyway"
    )
    assert out["backToFirst"]["cleared"] is True, (
        "a payload the server no longer reports stayed held"
    )
    assert out["settled"]["intact"] is True, (
        "the end of the gesture rendered a superseded graph"
    )
    assert out["settled"]["stale"] is False, "b-new is gone and must not be drawn"
    assert out["landed"]["redrew"] is True and out["landed"]["delivered"] is True, (
        "a real dependency change no longer lands"
    )
    assert out["landed"]["staleAfter"] is False, "the retired task was drawn anyway"

    assert out["heldAgain"] is True, (
        "the gate has memory of having been released once — the selection did "
        "not hold the redraw"
    )
    assert out["drawPath"]["drew"] is True, "the newest graph did not land"
    assert out["drawPath"]["stale"] is False, (
        "a graph held during the gesture was drawn instead of the newest one"
    )
    assert out["drawPath"]["cleared"] is True, (
        "a payload superseded by the draw itself stayed held"
    )
    assert out["afterSettle"] is True, (
        "settling after the draw rendered the superseded graph"
    )


# ---- the two filters, on the page ---------------------------------------------
#
# The backend lays out all four views; what these measure is the half only the
# browser runs — that the checkboxes SELECT one, that the page says which nodes
# they took out, and that a poll can neither reset a filter nor be frozen by one.


def test_unchecking_a_box_hides_that_state_on_the_page_and_says_what_it_hid():
    """`renderDeps` itself, run against a stub document with the two controls in
    it, one position at a time — including each box alone, because a render that
    read either checkbox for both filters would hide the right nodes whenever
    both are off and pass a test that only tried that.

    The counts are asserted as the page's own sentence rather than as payload
    fields: hiding has to be VISIBLE, and the arithmetic is written out because
    `hidden` and `omitted` are two different reasons a task is not on screen.
    """
    graph = dep_graph(filter_rows())
    payload = json.dumps({"depgraph": graph})

    harness = deps_panel_js() + """
const NODES = {};
for (const id of ["depnote","depsvg","depedges","depnodes","deplegend",
                  "deptablesum","deptable","depdetail","deptablebox",
                  "depshowdonelabel","depshowretiredlabel"])
  NODES[id] = {innerHTML:"", textContent:"", open:false, attrs:{},
               setAttribute(key, value){ this.attrs[key] = value; }};
// The two controls exactly as the static markup ships them: both CHECKED.
NODES.depshowdone = {id:"depshowdone", checked:true};
NODES.depshowretired = {id:"depshowretired", checked:true};
const document = {getElementById: id => NODES[id]};
const PAYLOAD = __PAYLOAD__;
const read = () => ({nodes: NODES.depnodes.innerHTML, note: NODES.depnote.innerHTML,
                     edges: NODES.depedges.innerHTML, table: NODES.deptable.innerHTML,
                     legend: NODES.deplegend.innerHTML,
                     label: NODES.depshowdonelabel.textContent});
renderDeps(PAYLOAD);
const all = read();
NODES.depshowdone.checked = false;          // "show completed" off, alone
renderDeps(PAYLOAD);
const noDone = read();
NODES.depshowdone.checked = true;
NODES.depshowretired.checked = false;       // "show retired" off, alone
renderDeps(PAYLOAD);
const noGone = read();
NODES.depshowdone.checked = false;          // both off
renderDeps(PAYLOAD);
const neither = read();
console.log(JSON.stringify({all, noDone, noGone, neither,
  boxes: [NODES.depshowdone.checked, NODES.depshowretired.checked]}));
""".replace("__PAYLOAD__", payload)
    out = json.loads(run_js(harness))

    # Both on: the whole connected subgraph, and the page says nothing is hidden
    # rather than staying quiet about it.
    for task_id in ("z-done", "y-gone", "a-live", "b-live", "c-orphan"):
        assert f'data-id="{task_id}"' in out["all"]["nodes"]
    assert out["all"]["edges"].count('<path class="dedge') == 4
    assert "0 more are hidden by the controls above (0 completed, 0 retired)" in out["all"]["note"]
    assert "5 drawn + 0 hidden + 2 in no relation = 7." in out["all"]["note"]
    # The label carries the figure the box governs before anyone clicks it.
    assert out["all"]["label"] == " show completed (1)"

    # Completed off: that node and both its edges are gone, the retired one is
    # untouched, and the live task left with no drawn relation is still there.
    assert 'data-id="z-done"' not in out["noDone"]["nodes"]
    assert 'data-id="y-gone"' in out["noDone"]["nodes"]
    assert 'data-id="c-orphan"' in out["noDone"]["nodes"]
    assert out["noDone"]["edges"].count('<path class="dedge') == 2
    assert "z-done" not in out["noDone"]["table"], "the readable twin still lists it"
    assert "1 more are hidden by the controls above (1 completed, 0 retired)" in out["noDone"]["note"]
    assert "4 drawn + 1 hidden + 2 in no relation = 7." in out["noDone"]["note"]
    # …and it SAYS the live task is drawn alone, and why it was kept.
    assert "1 task(s) are drawn alone" in out["noDone"]["note"]
    assert "c-orphan" in out["noDone"]["note"]
    assert "must not take a live task off the page" in out["noDone"]["note"]
    assert "✓ completed" not in out["noDone"]["legend"]

    # Retired off, alone: the mirror image. Each box governs its own state only.
    assert 'data-id="y-gone"' not in out["noGone"]["nodes"]
    assert 'data-id="z-done"' in out["noGone"]["nodes"]
    assert "0 more are hidden" not in out["noGone"]["note"]
    assert "(0 completed, 1 retired)" in out["noGone"]["note"]

    assert 'data-id="z-done"' not in out["neither"]["nodes"]
    assert 'data-id="y-gone"' not in out["neither"]["nodes"]
    assert 'data-id="b-live"' in out["neither"]["nodes"]
    assert "3 drawn + 2 hidden + 2 in no relation = 7." in out["neither"]["note"]
    assert "⊘ retired" not in out["neither"]["legend"]
    # Four renders and the boxes are where the operator left them: `renderDeps`
    # reads `.checked` and never writes it, which is the whole persistence
    # mechanism — the element is static markup nothing rebuilds.
    assert out["boxes"] == [False, False]


def test_a_filter_survives_the_poll_and_a_focused_box_does_not_freeze_the_graph():
    """dash-12's rule, applied to a control rather than to a node.

    A filter that silently reset every two seconds would be worse than no
    filter, so the tick must not touch the boxes — and it must not be BLOCKED by
    them either. `depsBusy` counts keyboard focus inside the panel as an
    interaction, and a clicked checkbox keeps focus until the operator clicks
    something else: counting it would freeze the graph from the first toggle
    until they thought to click the background. It is excluded for the same
    reason an open `<details>` is — nothing rebuilds it.

    The paired positive is the load-bearing half: a focused NODE still holds the
    redraw, so this cannot pass by `depsBusy` having been broken outright.
    """
    base = [roadmap_task("z-done", status="completed"),
            roadmap_task("a-live", depends_on=["z-done"])]
    grown = base + [roadmap_task("b-new", depends_on=["a-live"])]
    first = dep_graph(base)
    later = dep_graph(grown)
    newest = dep_graph(grown + [roadmap_task("c-new", depends_on=["b-new"])])

    harness = deps_gate_js(
        FIRST=dep_payload(first, 4, []),
        LATER=dep_payload(later, 5, []),
        NEWEST=dep_payload(newest, 6, []),
    ) + """
NODES.depshowdone = {id:"depshowdone", checked:true, inPanel:true};
NODES.depshowretired = {id:"depshowretired", checked:true, inPanel:true};
updateDeps(FIRST);
const opened = shows("z-done");
// The operator unchecks "show completed". What the change listener does, spelled
// out — it is bound outside the region this harness lifts — and the box keeps
// keyboard focus afterwards, exactly as a real click leaves it.
NODES.depshowdone.checked = false;
HOLD.focus = NODES.depshowdone;
updateDeps(DEPDRAWN, true);
const filtered = {done: shows("z-done"), live: shows("a-live")};
// A 2s tick carrying a genuinely changed graph, with that box still focused.
updateDeps(LATER);
const ticked = {grew: shows("b-new"), done: shows("z-done"),
                checked: NODES.depshowdone.checked};
// The paired positive: focus on a NODE is an interaction, and still holds.
stamp("HELD");
HOLD.focus = INSIDE;
updateDeps(NEWEST);
const held = intact("HELD");
HOLD.focus = null;
updateDeps(NEWEST);
const released = {redrew: !intact("HELD"), delivered: shows("c-new"),
                  stillFiltered: !shows("z-done")};
console.log(JSON.stringify({opened, filtered, ticked, held, released}));
"""
    out = json.loads(run_js(harness))

    assert out["opened"] is True, "the panel must open on the whole graph"
    assert out["filtered"]["done"] is False, "the toggle did not redraw the graph"
    assert out["filtered"]["live"] is True, "the toggle took a live task with it"
    assert out["ticked"]["grew"] is True, (
        "a focused filter checkbox froze the panel — a real graph change never landed"
    )
    assert out["ticked"]["done"] is False, "the poll put the hidden nodes back"
    assert out["ticked"]["checked"] is False, "the poll reset the operator's filter"
    assert out["held"] is True, "a focused NODE no longer holds the redraw"
    assert out["released"]["redrew"] is True and out["released"]["delivered"] is True
    assert out["released"]["stillFiltered"] is True, (
        "the filter was lost when the held payload landed"
    )


def test_the_filter_controls_are_static_markup_the_refresh_never_rewrites():
    """Where the operator's choice LIVES, as wiring. The two boxes are static
    markup, `renderDeps` reads `.checked` and never writes it, and the redraw a
    toggle asks for goes through the panel's own gate rather than rebuilding the
    page. The negatives are the load-bearing half — a render that wrote
    `.checked` back would reset the filter every two seconds, and a second
    `renderDeps` call site would be a redraw nothing guards.
    """
    static_markup, script = PAGE.split("<script>", 1)

    for marker in ('<input type="checkbox" id="depshowdone" checked>',
                   '<input type="checkbox" id="depshowretired" checked>',
                   'id="depshowdonelabel"', 'id="depshowretiredlabel"'):
        assert marker in static_markup, f"{marker} is not on the page"
    # Above the note, whose counts describe what these two did.
    assert static_markup.index('id="depshowdone"') < static_markup.index('id="depnote"')

    region = deps_panel_js()
    assert ".checked =" not in region, (
        "a render that writes .checked resets the operator's filter every 2s"
    )
    body = script.split("function renderDeps(d){", 1)[1].split("\n}", 1)[0]
    assert ".checked" not in body, "the draw reads the boxes through depShown, not by hand"
    assert "depView(d)" in body, "the draw must render the view the boxes select"
    # ONE redraw path, and it is the panel's own: `DEPDRAWN` is the payload
    # already on screen, so a toggle cannot let a held graph in behind it.
    assert 'box.addEventListener("change", () => updateDeps(DEPDRAWN || LAST, true));' in script
    assert script.count("renderDeps(") == 2, "definition plus the one guarded call"
    # The gate stops counting the boxes as an interaction — and says so where
    # the rule is, not somewhere else.
    busy = script.split("function depsBusy(){", 1)[1].split("\n}", 1)[0]
    assert "depIsFilter(active)" in busy

    # ONE spelling of the view key, on both sides. A page that built `c1r0` while
    # the backend built something else would silently fall back to the unfiltered
    # graph for every position but the default.
    assert 'const DFILTERS = [["depshowdone", "c"], ["depshowretired", "r"]];' in script
    assert [dep_view_key(*pair) for pair in
            ((True, True), (True, False), (False, True), (False, False))] == [
        "c1r1", "c1r0", "c0r1", "c0r0"]
    # The page states the three things a filter has to say for itself: what the
    # default is, how the counts add up, and what happens to a task whose last
    # relation was hidden.
    assert "Both start ON" in static_markup
    assert "drawn + hidden + in no relation = every task" in static_markup
    assert "KEPT and drawn alone" in static_markup


# ---- the dashboard runs the code the checkout holds (loop-03, 2026-08-23) -----
#
# MEASURED 2026-08-22: the page had been up 19.5 hours across four merges;
# `autoloop/tasks.py` was rewritten three hours earlier and
# `.autoloop/pending_upgrade.json` named it at 13:37. The signal was armed and
# nothing in this module read it.
#
# The symptom is what these tests are really about. The page said "the task
# graph could not be read — tasks.json did not load as a registry" about a file
# holding 171 valid tasks that `TaskRegistry.from_dict` parses and `state_of`
# raises for none of. A stale reader reporting a data error sends every
# investigation in the wrong direction, so the claim has two halves and both are
# pinned below: the process replaces itself, AND a genuinely unreadable graph
# still says so, distinguishably.
#
# **No test here replaces the pytest process** — `no_process_replacement` is
# autouse for the whole file and makes `os.execv` raise, so a test that reaches
# a real exec fails loudly instead of turning the session into a web server. The
# tests that care about the exec install their own recorder over it.

REPO_ROOT = Path(__file__).resolve().parents[2]


class Execed(Exception):
    """Raised by the recorder standing in for `os.execv`. Deliberately not an
    `OSError`: the production code catches `OSError` as "the exec was refused"
    and would swallow it, so a test wanting to observe a REAL exec has to raise
    something that propagates."""


def recording_execv(monkeypatch) -> list:
    """Install a recorder over `os.execv` and return the list it writes to.

    It records `_INFLIGHT` as well as the argv, because "no request was being
    served at the instant the image was replaced" is the safety property — not
    "the handler that decided had finished", which is a weaker claim on a
    threading server.
    """
    import autoloop.dashboard as dash

    calls: list = []

    def record(path, argv):
        calls.append({"path": path, "argv": list(argv), "inflight": dash._INFLIGHT})
        raise Execed(argv)

    monkeypatch.setattr(os, "execv", record)
    return calls


def upgrade_record(repo_root, **over):
    """One `PendingUpgrade` of the shape `AutoMerger._note_loop_code_merge`
    writes: loop code changed, not acted on yet, recorded just now."""
    from autoloop.auto_merge import UPGRADE_PENDING, PendingUpgrade

    data = {
        "base_sha": "b" * 40,
        "previous_base_sha": "a" * 40,
        "candidate_sha": "c" * 40,
        "task_id": "preempt-01",
        "repo_root": str(repo_root),
        "paths": ["autoloop/tasks.py"],
        "status": UPGRADE_PENDING,
        # AFTER this pytest process imported `dashboard`, which is what makes
        # the loaded code older than the checkout.
        "recorded_at": utcnow_iso(),
    }
    data.update(over)
    return PendingUpgrade(**data)


def arm_upgrade(repo, record):
    """Write the record where the dashboard reads it — through the loop's own
    store, so this is the signal the loop writes and not a lookalike."""
    from autoloop.auto_merge import UpgradeStore
    import autoloop.dashboard as dash

    UpgradeStore(dash._pending_upgrade_file(repo)).save(record)
    return dash._pending_upgrade_file(repo)


def running_tree():
    """The package root this pytest process imported `autoloop` from — the tree
    a replacement would load, and therefore the only `repo_root` that can make a
    record applicable to it."""
    import autoloop.dashboard as dash

    return dash._package_root()


class FakeSpec:
    def __init__(self, name):
        self.name = name


class FakeMain:
    """A stand-in for `sys.modules["__main__"]`. Without a `spec` it is the
    process started by PATH rather than by `python -m` — the shape
    `relaunch_argv` refuses."""

    def __init__(self, spec=None):
        if spec is not None:
            self.__spec__ = spec


def launchable(monkeypatch, argv=("/x/autoloop/dashboard.py",)):
    """Give this process a derivable launch shape.

    Needed by every test that drives an ATTEMPT, because pytest is usually
    started as a console script and so has no `__main__.__spec__` at all — which
    is precisely the case `relaunch_argv` refuses. Without this, those tests
    would stop at that refusal and never reach the bound they are about, while
    still going green on a weaker assertion.
    """
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.setitem(
        sys.modules, "__main__", FakeMain(FakeSpec("autoloop.dashboard"))
    )


def test_the_dashboard_reads_the_signal_file_the_loop_writes(tmp_path):
    """Two spellings of one path. The whole claim is that this reads the marker
    the loop already writes; a dashboard resolving a different path would read
    nothing, forever, and nothing would say so."""
    import autoloop.dashboard as dash
    from autoloop.config import AutoloopConfig, BrowserConfig

    from autoloop.policy import PolicyConfig

    repo = make_repo(tmp_path)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=repo / ".autoloop",
    )
    assert dash._pending_upgrade_file(repo) == config.pending_upgrade_file


def test_the_dashboards_preflight_timeout_matches_the_loops():
    """Spelled in this module rather than imported (importing `cli` would drag
    the browser and the codex client into a read-only tracker), so the two are
    pinned equal here instead of being allowed to drift apart silently."""
    import autoloop.dashboard as dash
    from autoloop import cli

    assert dash.PREFLIGHT_TIMEOUT_SECONDS == cli.PREFLIGHT_TIMEOUT_SECONDS


def test_the_preflight_covers_what_a_fresh_dashboard_loads():
    """`cli.PREFLIGHT_MODULES` names what a LOOP loads and does not contain
    `dashboard`, so preflighting with it would pass for a tree whose dashboard
    does not import — the only tree that matters here. The lazily imported ones
    are in the list because this process reaches them mid-request, long after
    any preflight has run."""
    import autoloop.dashboard as dash

    for name in ("autoloop", "autoloop.dashboard", "autoloop.tasks",
                 "autoloop.lock", "autoloop.inbox", "autoloop.path_suggest"):
        assert name in dash.DASHBOARD_PREFLIGHT_MODULES
    assert not [m for m in dash.DASHBOARD_PREFLIGHT_MODULES
                if "playwright" in m or "codex" in m or "browser" in m], (
        "optional third-party deps would fail every preflight on a machine "
        "without them and disable this feature for good"
    )


def test_the_preflight_imports_the_tree_it_is_pointed_at(tmp_path):
    """Both directions, with a real subprocess. A preflight that never launched
    an interpreter would pass for a tree with a syntax error in it."""
    import autoloop.dashboard as dash

    ok, detail = dash._preflight_import(REPO_ROOT)
    assert ok, detail

    broken = tmp_path / "checkout"
    (broken / "autoloop").mkdir(parents=True)
    (broken / "autoloop" / "__init__.py").write_text(
        "raise RuntimeError('this tree does not import')\n", encoding="utf-8"
    )
    ok, detail = dash._preflight_import(broken)
    assert not ok
    assert "this tree does not import" in detail


# --- the decision: five gates, and what the page says at each one -------------


def test_a_merge_after_this_process_started_makes_it_stale(tmp_path):
    """The whole feature, at the decision layer: the checkout moved to a sha
    this process did not load, and the record says so."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))

    view = dash.upgrade_decision(repo)
    assert view["state"] == "stale"
    assert view["base_sha"] == "b" * 40, "the FULL sha — it keys the one-shot"
    assert view["task_id"] == "preempt-01"
    assert "autoloop/tasks.py" in view["paths"]
    # And the page carries it, beside — never inside — the task-graph payload.
    assert collect(repo)["build"]["upgrade"]["state"] == "stale"


def test_no_marker_at_all_is_silence(tmp_path):
    """The ordinary case, and it must stay silent: a banner on every page for a
    loop that has merged nothing is a banner nobody reads."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    assert not dash._pending_upgrade_file(repo).exists()
    view = dash.upgrade_decision(repo)
    assert view["state"] == "current" and view["note"] == ""


def test_a_docs_only_merge_does_not_trigger_a_restart(tmp_path):
    """Same rule the loop already follows. This is a page somebody is watching:
    re-execing it for a change it does not load is a visible flicker for
    nothing.

    Asserted against a record the dashboard filters ITSELF, through
    `loop_code_paths`, rather than against the writer's promise not to have
    written one — a test that only checked "no record" would pass with this
    whole gate deleted."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(
        running_tree(), paths=["docs/AUTOLOOP.md", "docs/TESTS.md"]))

    view = dash.upgrade_decision(repo)
    assert view["state"] == "current"
    assert "no file under autoloop/" in view["note"]
    # The gate is this module's own restatement of the rule, not a re-read of
    # the writer's: the same filter, on the same input, agrees with the loop's.
    from autoloop.auto_merge import loop_code_paths

    assert loop_code_paths(["docs/AUTOLOOP.md", "docs/TESTS.md"]) == []


def test_a_process_that_already_loaded_the_merged_code_is_current(tmp_path):
    """The gate that makes this self-limiting, and it is doing the work of a
    one-shot: a dashboard started AFTER the merge is already running the merged
    tree, so acting on a still-pending record would be a restart loop wearing an
    upgrade's clothes. It is also what the successor of a real re-exec sees."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(
        running_tree(), recorded_at="2020-01-01T00:00:00+00:00"))

    view = dash.upgrade_decision(repo)
    assert view["state"] == "current"
    assert "loaded its code after that merge" in view["note"]


def test_a_settled_record_is_not_acted_on_but_is_not_silent_either(tmp_path, monkeypatch):
    """`pending` gates the ACTION — the same gate the loop's own boundary
    applies, so this process can never act on a sha the loop has taken off the
    table.

    It does NOT gate the report, and that is the hole this closes rather than
    inherits. Nobody has the page open when the merge lands; the loop settles
    the record at its next round boundary; the first person to look then gets a
    stale page reporting nothing wrong with itself — the exact silence of
    2026-08-22, arrived at by a different route."""
    import autoloop.dashboard as dash
    from autoloop.auto_merge import (
        UPGRADE_EXEC_FAILED,
        UPGRADE_EXECED,
        UPGRADE_PREFLIGHT_FAILED,
        UPGRADE_UNAPPLICABLE,
    )

    repo = make_repo(tmp_path)
    launchable(monkeypatch)
    monkeypatch.setattr(
        dash, "_preflight_import", lambda root: pytest.fail("must not preflight")
    )
    for status in (UPGRADE_EXECED, UPGRADE_PREFLIGHT_FAILED,
                   UPGRADE_UNAPPLICABLE, UPGRADE_EXEC_FAILED):
        arm_upgrade(repo, upgrade_record(running_tree(), status=status))
        view = dash.upgrade_decision(repo)
        assert view["state"] == "stale_settled", status
        assert status in view["note"] and "by hand" in view["note"]
        # And nothing is attempted — `no_process_replacement` plus the refusing
        # preflight above make that a real assertion rather than an absence.
        assert dash._upgrade_at_boundary(repo) == "stale_settled"


def test_a_settled_record_older_than_this_process_is_simply_current(tmp_path):
    """The complement, and the one that keeps `stale_settled` from becoming a
    banner on every page forever: a dashboard started AFTER the merge is running
    the merged code, whatever became of the record."""
    import autoloop.dashboard as dash
    from autoloop.auto_merge import UPGRADE_EXECED

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(
        running_tree(), status=UPGRADE_EXECED,
        recorded_at="2020-01-01T00:00:00+00:00"))

    assert dash.upgrade_decision(repo)["state"] == "current"


def test_a_merge_in_another_checkout_is_not_a_reason_to_restart(tmp_path):
    """Replacing this process would load the same code again."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(tmp_path / "somewhere-else"))

    view = dash.upgrade_decision(repo)
    assert view["state"] == "not_this_tree"
    assert "would load the same code again" in view["note"]


@pytest.mark.parametrize(
    "content",
    ["{not json", "", "[]", '{"base_sha": "b"}', '{"nope": 1}'],
)
def test_an_unreadable_marker_says_MARKER_not_task_graph(tmp_path, content):
    """The failure this whole task exists to stop: a process problem reported
    as a data problem. `UpgradeStore.load` collapses absent and corrupt into
    `None`, so the file's existence is checked separately — otherwise a
    half-written marker would render as silence, and the page would go on
    blaming `tasks.json`."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    path = dash._pending_upgrade_file(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    view = dash.upgrade_decision(repo)
    assert view["state"] == "unreadable_marker"
    assert "pending_upgrade.json" in view["note"]
    assert "not the task graph" in view["note"]


def test_an_unreadable_recorded_at_refuses_rather_than_guessing(tmp_path):
    """A record that parses but cannot say WHEN it was written cannot establish
    that this process predates it. Refusing costs a delayed restart; guessing
    would either replace a current process or bless a stale one."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree(), recorded_at="not a timestamp"))

    view = dash.upgrade_decision(repo)
    assert view["state"] == "unreadable_marker"
    assert "recorded_at" in view["note"]


def test_a_naive_recorded_at_is_read_as_utc(tmp_path):
    """`utcnow_iso()` is tz-aware and older records may not be. Guessing local
    time for a naive stamp would shift it by hours in whichever direction the
    machine happens to sit — and the comparison it feeds decides whether a
    process replaces itself."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    arm_upgrade(repo, upgrade_record(running_tree(), recorded_at=naive))

    assert dash.upgrade_decision(repo)["state"] == "stale"


def test_the_dashboard_never_writes_the_signal_it_reads(tmp_path, monkeypatch):
    """Two independent reasons, both severe. The record is the LOOP's one-shot,
    so consuming it would stop the LOOP re-execing — and loop-02's tests never
    run a dashboard, so nothing there would catch it. And it lives inside the
    observed checkout, where any write trips the escape detector and parks the
    loop this page exists to watch.

    Driven through a real failed attempt, not just a read: settling is exactly
    what the loop does at this point, so the tempting write is on this path."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    path = arm_upgrade(repo, upgrade_record(running_tree()))
    before_bytes = path.read_bytes()
    before_tree = snapshot(repo)

    launchable(monkeypatch)
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (False, "boom"))
    assert dash._upgrade_at_boundary(repo) == "preflight_failed"
    collect(repo)

    assert path.read_bytes() == before_bytes, "the loop's one-shot is untouched"
    assert snapshot(repo) == before_tree, "and nothing else in the checkout moved"


# --- the attempt: preflight first, and one shot per sha ----------------------


def test_a_tree_that_does_not_import_leaves_the_process_serving(tmp_path, monkeypatch):
    """A dashboard that exec'd into a broken tree would be GONE — no process, no
    message, an operator staring at a dead port. So the old image stays and the
    page says why. `no_process_replacement` is what makes "not exec'ed" a real
    assertion here: an exec would raise."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    monkeypatch.setattr(
        dash, "_preflight_import", lambda root: (False, "SyntaxError: invalid syntax")
    )

    assert dash._upgrade_at_boundary(repo) == "preflight_failed"
    view = dash.upgrade_decision(repo)
    assert view["state"] == "preflight_failed"
    assert "SyntaxError" in view["note"]
    assert "still serving the code it started with" in view["note"]
    assert collect(repo)["build"]["upgrade"]["state"] == "preflight_failed"


def test_a_failed_preflight_is_not_retried_on_every_poll(tmp_path, monkeypatch):
    """The page polls every two seconds and the preflight launches an
    interpreter. Retrying it per poll would spawn a process every two seconds
    forever for an upgrade that has already been ruled out."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    attempts: list = []

    def preflight(root):
        attempts.append(root)
        return False, "boom"

    monkeypatch.setattr(dash, "_preflight_import", preflight)

    assert dash._upgrade_at_boundary(repo) == "preflight_failed"
    assert dash._upgrade_at_boundary(repo) == "preflight_failed"
    assert dash._upgrade_at_boundary(repo) == "preflight_failed"
    assert len(attempts) == 1
    assert attempts[0] == dash._package_root(), "the tree the replacement loads"


def test_the_preflight_runs_before_anything_is_replaced(tmp_path, monkeypatch):
    """Order matters: a tree that does not import must be found out while this
    process is still the one serving."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    seen: list = []

    monkeypatch.setattr(dash, "_preflight_import",
                        lambda root: (seen.append("preflight"), (True, ""))[1])

    def record(path, argv):
        seen.append("exec")
        raise Execed(argv)

    monkeypatch.setattr(os, "execv", record)

    with pytest.raises(Execed):
        dash._upgrade_at_boundary(repo)
    assert seen == ["preflight", "exec"]


def test_an_exec_that_is_refused_leaves_the_process_serving(tmp_path, monkeypatch):
    """`execv` raising means this process is still here and still holding the
    port. The gate that stopped it answering has to come back off, or refusing
    one upgrade costs the operator the whole page."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (True, ""))

    def refuse(path, argv):
        raise OSError("Exec format error")

    monkeypatch.setattr(os, "execv", refuse)

    assert dash._upgrade_at_boundary(repo) == "exec_failed"
    assert dash._UPGRADING is False, (
        "a process that could not be replaced must go on answering requests"
    )
    view = dash.upgrade_decision(repo)
    assert view["state"] == "exec_failed" and "Exec format error" in view["note"]
    # One shot: a refused exec is not a licence to try the same sha again.
    assert dash._upgrade_at_boundary(repo) == "exec_failed"


def test_an_exec_that_raises_something_else_still_unlocks_the_port(
    tmp_path, monkeypatch
):
    """`os.execv` is documented to raise `OSError`, and the mainline catches
    exactly that. Anything else on its way up would leave `_UPGRADING` set and
    the port silent for good — a worse outcome than never upgrading — so the
    flag is cleared in a `finally`, not in the `except`."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (True, ""))
    recording_execv(monkeypatch)

    with pytest.raises(Execed):
        dash._upgrade_at_boundary(repo)
    assert dash._UPGRADING is False


# --- the relaunch command: derived, never hard-coded --------------------------


@pytest.mark.parametrize(
    "spec_name, argv, expected",
    [
        # `python -m autoloop.dashboard --repo X --port 8787`
        ("autoloop.dashboard",
         ["/x/autoloop/dashboard.py", "--repo", "/checkout", "--port", "8787"],
         ["-m", "autoloop.dashboard", "--repo", "/checkout", "--port", "8787"]),
        # `python -m autoloop dashboard --port 8787` — runpy names the package's
        # `__main__`, which is not what `-m` takes.
        ("autoloop.__main__",
         ["/x/autoloop/__main__.py", "dashboard", "--port", "8787"],
         ["-m", "autoloop", "dashboard", "--port", "8787"]),
    ],
)
def test_the_relaunch_command_is_derived_from_how_this_process_started(
    monkeypatch, spec_name, argv, expected
):
    """Both documented launch shapes. The loop's own rebuild is hard-coded
    `[-m, autoloop, *argv[1:]]` because the loop has one shape; copying it here
    would, under `python -m autoloop.dashboard --repo X`, launch a LOOP RUN with
    `--repo X` — a write-capable, git-pushing process started by a read-only
    tracker."""
    import autoloop.dashboard as dash

    monkeypatch.setitem(sys.modules, "__main__", FakeMain(FakeSpec(spec_name)))
    monkeypatch.setattr(sys, "argv", argv)

    built, why = dash.relaunch_argv()
    assert why == ""
    assert built == [sys.executable, *expected]


def test_a_launch_shape_that_cannot_be_derived_refuses_rather_than_guesses(
    monkeypatch,
):
    """A script run by path has no `__main__.__spec__`. A wrong relaunch is
    unrecoverable; a refused one is a sentence on the page."""
    import autoloop.dashboard as dash

    monkeypatch.setitem(sys.modules, "__main__", FakeMain())
    built, why = dash.relaunch_argv()
    assert built == []
    assert "python -m" in why


def test_an_undeterminable_launch_shape_is_never_exec_ed(tmp_path, monkeypatch):
    """The refusal, driven through the boundary: nothing is replaced, the page
    says so, and `no_process_replacement` proves no exec was attempted."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (True, ""))
    monkeypatch.setitem(sys.modules, "__main__", FakeMain())

    assert dash._upgrade_at_boundary(repo) == "exec_failed"
    assert dash.upgrade_decision(repo)["state"] == "exec_failed"


def test_nothing_from_the_record_reaches_the_relaunch_command(tmp_path, monkeypatch):
    """`pending_upgrade.json` is a file INSIDE the observed checkout. It decides
    only WHETHER this process is replaced, never WHAT is run — otherwise an
    agent that can write the state dir could choose a command line for a
    process that then execs it."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(
        running_tree(),
        task_id="--evil-task",
        paths=["autoloop/tasks.py", "--evil-path"],
        candidate_sha="; rm -rf /",
    ))
    launchable(monkeypatch, ["/x/autoloop/dashboard.py", "--port", "8787"])
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (True, ""))
    calls = recording_execv(monkeypatch)

    with pytest.raises(Execed):
        dash._upgrade_at_boundary(repo)

    assert calls[0]["argv"] == [
        sys.executable, "-m", "autoloop.dashboard", "--port", "8787"
    ]
    joined = " ".join(calls[0]["argv"])
    for smuggled in ("--evil-task", "--evil-path", "rm -rf", str(repo)):
        assert smuggled not in joined


# --- the safe point: between requests, never mid-response --------------------


@contextlib.contextmanager
def serving_dashboard(repo, monkeypatch):
    """The real `Handler` on a real threading server — the handler whose
    `handle()` carries the boundary."""
    import autoloop.dashboard as dash

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


def get(base, path="/"):
    with urllib.request.urlopen(base + path, timeout=10) as resp:
        return resp.status, resp.read().decode()


def test_a_stale_dashboard_replaces_itself_at_a_request_boundary(
    tmp_path, monkeypatch
):
    """The claim, through the served path. The request that triggers it is
    answered IN FULL first — `inflight` is recorded at the instant of the exec
    and must be zero, which is the property a threading server needs (my handler
    having finished says nothing about the tab polling beside it)."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch, ["/x/autoloop/dashboard.py", "--port", "8787"])
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (True, ""))
    calls = recording_execv(monkeypatch)

    with serving_dashboard(repo, monkeypatch) as base:
        status, body = get(base, "/api/state")
        # The response arrived whole — the exec is not allowed to cost the
        # operator the answer they asked for.
        assert status == 200
        payload = json.loads(body)
        assert payload["build"]["upgrade"]["state"] == "stale"
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.02)

    assert len(calls) == 1, "one replacement, after the response"
    assert calls[0]["inflight"] == 0, "nothing was being served when the image went"
    assert calls[0]["argv"][:3] == [sys.executable, "-m", "autoloop.dashboard"]


def test_a_replacement_never_interrupts_a_response_in_flight(tmp_path, monkeypatch):
    """`os.execv` kills every thread at once, so "the handler that decided has
    finished" is not the safety property — "nothing else is being served" is.

    One request is held inside `collect` while a second runs to completion. The
    second one's boundary must NOT replace the process, because the first is
    still writing. Only when the first finishes may the exec happen.

    The second request is `/` and NOT `/api/state`, since dash-21: a second
    `/api/state` would now join the held sweep by design (`collect_shared`) and
    could not complete while the first is parked, which is the single-flight
    property and not a fault. `handle()` is the boundary under test and it is
    the same code for both routes — `_INFLIGHT` counts connections, not paths —
    so the claim is unchanged. Do not "fix" this back to `/api/state`; the held
    response's own body carries the upgrade state and is checked below."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    monkeypatch.setattr(dash, "_preflight_import", lambda root: (True, ""))
    calls = recording_execv(monkeypatch)

    real_collect = dash.collect
    entered = threading.Event()
    release = threading.Event()
    slow_once = {"used": False}

    def collect_holding_the_first_caller(target):
        if not slow_once["used"]:
            slow_once["used"] = True
            entered.set()
            release.wait(timeout=10)
        return real_collect(target)

    monkeypatch.setattr(dash, "collect", collect_holding_the_first_caller)
    held: list = []

    with serving_dashboard(repo, monkeypatch) as base:
        first = threading.Thread(
            target=lambda: held.append(get(base, "/api/state")), daemon=True
        )
        first.start()
        assert entered.wait(timeout=10), "the held request never reached collect"

        # A whole second request, start to finish, while the first is mid-response.
        status, body = get(base, "/")
        assert status == 200 and "<title>Autoloop" in body
        time.sleep(0.2)
        assert calls == [], (
            "the second request's boundary replaced the process while the first "
            "was still being written — that is a truncated page"
        )

        release.set()
        first.join(timeout=10)
        deadline = time.time() + 5
        while not calls and time.time() < deadline:
            time.sleep(0.02)

    assert held and held[0][0] == 200, "the held response still arrived, whole"
    # The held request's OWN body, read after it was released: the upgrade state
    # the second request used to assert, from the response that was in flight.
    assert json.loads(held[0][1])["build"]["upgrade"]["state"] == "stale"
    assert len(calls) == 1 and calls[0]["inflight"] == 0


def test_no_new_request_is_served_once_a_replacement_is_armed(tmp_path, monkeypatch):
    """The window between arming and `execv`. A connection accepted there would
    be answered by an image that is about to vanish; refusing it gives the
    browser a failed poll and the successor answers the retry, which is a page
    that blinks rather than a page cut in half."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    dash._UPGRADING = True
    with serving_dashboard(repo, monkeypatch) as base:
        # `RemoteDisconnected` — the connection is closed with nothing written.
        # An `OSError` at the client is a failed poll it will retry; a partial
        # body would be a page cut in half, which is the outcome being denied.
        with pytest.raises(OSError):
            get(base, "/api/state")
    assert dash._INFLIGHT == 0, "a refused connection is not a leaked count"


def test_a_request_arriving_at_the_boundary_defers_rather_than_truncates(
    tmp_path, monkeypatch
):
    """The recheck under the lock, after the preflight ran without it. A request
    that arrived in that gap defers the replacement — nothing is spent, nothing
    is said, and the next boundary asks again with the preflight already
    paid for."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    arm_upgrade(repo, upgrade_record(running_tree()))
    launchable(monkeypatch)
    calls: list = []

    def preflight(root):
        calls.append(root)
        dash._INFLIGHT += 1          # a connection arrives while we are asking
        return True, ""

    monkeypatch.setattr(dash, "_preflight_import", preflight)

    assert dash._upgrade_at_boundary(repo) == "deferred"
    dash._INFLIGHT = 0
    recording_execv(monkeypatch)
    with pytest.raises(Execed):
        dash._upgrade_at_boundary(repo)
    assert len(calls) == 1, "the deferred attempt's preflight was not paid for twice"
    assert dash.upgrade_decision(repo)["state"] == "stale", (
        "a deferral spends nothing — the sha is still available at the next boundary"
    )


# --- the loop is not supervised, and does not supervise ----------------------


def test_neither_process_finds_or_signals_the_other():
    """loop-02's bound, still standing: each process restarts ITSELF. There is
    no pid file that would make anything else safe, and a loop that signals
    other processes is a different and much larger claim.

    Asserted as an absence in the source, because that is what the bound is: no
    signal, no kill, no process-table lookup for the other side."""
    loop_side = (REPO_ROOT / "autoloop" / "cli.py").read_text(encoding="utf-8")
    merge_side = (REPO_ROOT / "autoloop" / "auto_merge.py").read_text(encoding="utf-8")
    orch = (REPO_ROOT / "autoloop" / "orchestrator.py").read_text(encoding="utf-8")
    page = (REPO_ROOT / "autoloop" / "dashboard.py").read_text(encoding="utf-8")

    for name, source in (("cli", loop_side), ("auto_merge", merge_side),
                         ("orchestrator", orch)):
        for sink in ("os.kill", "killpg", "send_signal", ".terminate()", "pkill"):
            assert sink not in source, f"{name} signals another process ({sink})"
    for sink in ("os.kill", "killpg", "send_signal", ".terminate()", "pkill",
                 "SIGTERM", "SIGKILL"):
        assert sink not in page, f"the dashboard signals another process ({sink})"
    # The dashboard replaces ITSELF, in its own pid, and nothing else.
    assert "os.execv(sys.executable, argv)" in page
    assert "pgrep" in page, (
        "reading the process table for DISPLAY is unchanged — the bound is about "
        "signalling, not about looking"
    )


# --- dash-21: one sweep at a time, one outstanding poll per tab --------------
#
# The claim, in two halves that are tested separately because they are two
# independent guards:
#
#   * the PAGE never has more than one `/api/state` request outstanding; and
#   * the SERVER never has more than one `collect()` sweep running.
#
# Measured 2026-08-24 06:20 against the live dashboard, before either guard
# existed: three consecutive `/api/state` answers took 34.6s, 35.6s and 36.6s,
# one second of progress per poll, because a 2s `setInterval` had issued about
# seventeen requests before the first one answered and `ThreadingHTTPServer`
# ran every one of them in its own thread. `test_the_pile_up_before_and_after_
# measured_in_one_repository` at the end of this section is the reproducible
# half of that: both arms in one process against ONE checkout.
#
# NOTHING IS CACHED, and these tests are also what say so. A caller arriving
# while a sweep runs joins THAT sweep; a caller arriving after it finished
# starts a new one. If a future change adds a TTL or a per-branch verdict store
# here, `test_a_caller_arriving_after_the_sweep_ended_runs_a_fresh_one` and
# `test_the_sweep_registry_is_not_a_cache_and_must_never_become_one` fail.


def wait_until(predicate, what, timeout=20):
    """Block until `predicate()` is true, or fail saying what never happened.

    Concurrency tests here must not `sleep(0.2)` and hope: "one sweep ran" is
    also true when the later callers arrived after it finished, which is the
    opposite of the property under test.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


class _JoinRecordingEvent(threading.Event):
    """A `_Sweep.done` that records every caller which WAITS on it.

    Waiting on that event is exactly what "joined the sweep already running"
    means — the caller has been past `_SWEEP_LOCK`, found a live sweep and
    decided not to lead one. Recording it is what lets a test hold the leader
    until every other caller has provably joined, instead of sleeping and
    hoping the scheduler cooperated.
    """

    def __init__(self, joined: list):
        super().__init__()
        self._joined = joined

    def wait(self, timeout=None):
        self._joined.append(threading.current_thread().name)
        return super().wait(timeout)


@contextlib.contextmanager
def joins_recorded(monkeypatch):
    """Yield the list of callers that joined a running sweep, as they join."""
    import autoloop.dashboard as dash

    joined: list = []

    class RecordingSweep(dash._Sweep):
        __slots__ = ()

        def __init__(self):
            super().__init__()
            self.done = _JoinRecordingEvent(joined)

    monkeypatch.setattr(dash, "_Sweep", RecordingSweep)
    yield joined


def test_concurrent_callers_share_the_one_sweep_already_running(tmp_path, monkeypatch):
    """Eight callers, one `collect()`. The leader is held inside the sweep until
    all seven others have provably joined it, so this cannot pass by the other
    seven arriving after it finished."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    sweeps: list = []
    entered = threading.Event()
    release = threading.Event()

    def one_held_sweep(target):
        sweeps.append(target)
        entered.set()
        assert release.wait(timeout=20), "the held sweep was never released"
        return {"served_at": "12:00:00", "sweep": len(sweeps)}

    monkeypatch.setattr(dash, "collect", one_held_sweep)
    answers: dict = {}

    def caller(index):
        answers[index] = dash.collect_shared(repo)

    with joins_recorded(monkeypatch) as joined:
        threads = [
            threading.Thread(target=caller, args=(i,), daemon=True) for i in range(8)
        ]
        threads[0].start()
        assert entered.wait(timeout=20), "the leader never reached collect()"
        for thread in threads[1:]:
            thread.start()
        wait_until(lambda: len(joined) >= 7, "seven callers to join the running sweep")
        release.set()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "a caller was never answered"

    assert len(sweeps) == 1, f"eight concurrent callers ran {len(sweeps)} sweeps"
    assert len(answers) == 8
    assert all(answer is answers[0] for answer in answers.values()), (
        "every joiner is answered from the leader's own result"
    )
    assert dash._SWEEPS_IN_FLIGHT == {}, "nothing outlives the sweep that made it"


def test_concurrent_api_state_requests_run_exactly_one_sweep(tmp_path, monkeypatch):
    """The same property through the real server and the real route, because
    `do_GET` calling `collect` instead of `collect_shared` would leave every
    test above passing while the page pile-up was untouched."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    sweeps: list = []
    entered = threading.Event()
    release = threading.Event()

    def one_held_sweep(target):
        sweeps.append(target)
        entered.set()
        assert release.wait(timeout=20), "the held sweep was never released"
        return {"served_at": "12:00:00", "sweep": len(sweeps)}

    monkeypatch.setattr(dash, "collect", one_held_sweep)
    answers: dict = {}

    with serving_dashboard(repo, monkeypatch) as base, joins_recorded(monkeypatch) as joined:
        def poll(index):
            answers[index] = get(base, "/api/state")

        threads = [
            threading.Thread(target=poll, args=(i,), daemon=True) for i in range(5)
        ]
        threads[0].start()
        assert entered.wait(timeout=20), "the first request never reached collect()"
        for thread in threads[1:]:
            thread.start()
        wait_until(lambda: len(joined) >= 4, "four tabs to join the running sweep")
        release.set()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "a request was never answered"

    assert len(sweeps) == 1, f"five concurrent tabs ran {len(sweeps)} sweeps"
    assert len(answers) == 5
    assert {status for status, _ in answers.values()} == {200}
    bodies = {body for _, body in answers.values()}
    assert len(bodies) == 1, "every caller was answered from the same sweep"


def test_a_caller_arriving_after_the_sweep_ended_runs_a_fresh_one(tmp_path, monkeypatch):
    """The no-cache half of the claim. A sweep's result is reachable only while
    that sweep is running; the next caller pays for a new one and gets the new
    one's answer, so there is no invalidation question to get wrong."""
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    sweeps: list = []

    def counting(target):
        sweeps.append(target)
        return {"sweep": len(sweeps)}

    monkeypatch.setattr(dash, "collect", counting)

    first = dash.collect_shared(repo)
    assert dash._SWEEPS_IN_FLIGHT == {}, "a finished sweep leaves nothing behind"
    second = dash.collect_shared(repo)
    third = dash.collect_shared(repo)

    assert [first["sweep"], second["sweep"], third["sweep"]] == [1, 2, 3]
    assert first is not second and second is not third
    assert len(sweeps) == 3


def test_the_sweep_registry_is_not_a_cache_and_must_never_become_one():
    """A drift guard on the constraint the task was rewritten around.

    Five earlier attempts died on cache invalidation — a head-keyed store whose
    old in-flight sweep evicted the new head's entry, and a 2.45s git timeout
    that must not be remembered. `collect_shared` therefore reads no clock and
    keys nothing on a head: an entry exists between the leader starting and the
    leader returning, and that is the entire lifetime.
    """
    import inspect

    import autoloop.dashboard as dash

    source = inspect.getsource(dash.collect_shared)
    for smell in ("time.time", "monotonic", "ttl", "TTL", "expire", "expiry",
                  "datetime", "_CACHE"):
        assert smell not in source, (
            f"`collect_shared` mentions {smell!r} — the cache is out of scope for "
            "dash-21 and re-adding one reopens the invalidation question that "
            "killed five rounds"
        )
    assert dash._SWEEPS_IN_FLIGHT == {}, (
        "with no sweep running the registry is empty; anything left in it is a cache"
    )


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("git went away mid-sweep"),
        subprocess.TimeoutExpired(cmd=["git", "log", "--all"], timeout=2.45),
        KeyboardInterrupt(),
    ],
    ids=["error", "git-timeout", "interrupt"],
)
def test_a_sweep_that_fails_releases_every_waiter(boom, tmp_path, monkeypatch):
    """The failure path, which is where a single-flight usually parks a page.

    `KeyboardInterrupt` is in the table on purpose: an `except Exception` in the
    leader would not catch it, `done` would never be set, and every joined tab
    would wait forever on a sweep whose thread had already gone. The git timeout
    is the real 2.45s one measured on 2026-08-24.
    """
    import autoloop.dashboard as dash

    repo = make_repo(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    attempts: list = []

    def failing(target):
        attempts.append(target)
        entered.set()
        assert release.wait(timeout=20), "the held sweep was never released"
        raise boom

    monkeypatch.setattr(dash, "collect", failing)
    raised: dict = {}

    def caller(index):
        try:
            dash.collect_shared(repo)
        except BaseException as exc:  # the point of the test
            raised[index] = exc

    with joins_recorded(monkeypatch) as joined:
        threads = [
            threading.Thread(target=caller, args=(i,), daemon=True) for i in range(5)
        ]
        threads[0].start()
        assert entered.wait(timeout=20), "the leader never reached collect()"
        for thread in threads[1:]:
            thread.start()
        wait_until(lambda: len(joined) >= 4, "four callers to join the doomed sweep")
        release.set()
        for thread in threads:
            thread.join(timeout=20)
            assert not thread.is_alive(), "a waiter was never released"

    assert len(attempts) == 1
    assert len(raised) == 5, "every caller was told, rather than left waiting"
    assert all(exc is boom for exc in raised.values())
    assert dash._SWEEPS_IN_FLIGHT == {}, "a failed sweep leaves no seat behind"

    # …and the failure is not remembered either: the next caller runs a fresh
    # sweep and is answered normally.
    def working(target):
        attempts.append(target)
        return {"sweep": len(attempts)}

    monkeypatch.setattr(dash, "collect", working)
    assert dash.collect_shared(repo) == {"sweep": 2}


def test_two_checkouts_never_share_a_sweep(tmp_path, monkeypatch):
    """Keyed by repo, for the reason `_REMOTE_CACHE` is: a single global slot
    would answer a question about checkout B with checkout A's payload, and
    would also make B wait on a sweep it has no interest in."""
    import autoloop.dashboard as dash

    repo_a = make_repo(tmp_path / "a")
    repo_b = make_repo(tmp_path / "b")
    held = threading.Event()
    release = threading.Event()
    sweeps: list = []

    def collect_holding_a(target):
        sweeps.append(target)
        if target == repo_a:
            held.set()
            assert release.wait(timeout=20), "A's sweep was never released"
        return {"repo": str(target)}

    monkeypatch.setattr(dash, "collect", collect_holding_a)
    answers: dict = {}

    def caller(name, repo):
        answers[name] = dash.collect_shared(repo)

    slow = threading.Thread(target=caller, args=("a", repo_a), daemon=True)
    slow.start()
    assert held.wait(timeout=20), "A's sweep never started"

    fast = threading.Thread(target=caller, args=("b", repo_b), daemon=True)
    fast.start()
    fast.join(timeout=20)
    assert not fast.is_alive(), "B waited on A's sweep — the registry is not keyed"
    assert answers["b"] == {"repo": str(repo_b)}

    release.set()
    slow.join(timeout=20)
    assert answers["a"] == {"repo": str(repo_a)}
    assert sorted(str(s) for s in sweeps) == sorted([str(repo_a), str(repo_b)])
    assert dash._SWEEPS_IN_FLIGHT == {}


# --- the page half: one outstanding /api/state per tab -----------------------


def pure_poll_js() -> str:
    """The tab's whole polling loop, lifted verbatim out of the served page.

    Everything between the markers depends on nothing but `fetch`, `render` and
    the timer functions, so a harness can supply those and drive real ticks
    against the real code. The `setInterval` that fires it is deliberately
    OUTSIDE the region — a test that started a real 2s timer would be testing
    node's clock rather than the guard.
    """
    script = PAGE.split("<script>", 1)[1]
    return script.split("// PURE_POLL_START", 1)[1].split("// PURE_POLL_END", 1)[0]


def test_a_tick_that_finds_one_in_flight_issues_nothing_and_draws_nothing():
    """Seventeen ticks against one unanswered request — the measured shape.

    One request is issued, sixteen ticks do nothing at all, and `render` is not
    called until the one request answers. Both halves matter: the second is
    what keeps the figures on screen the ones the last COMPLETED poll carried.
    """
    out = json.loads(run_js("""
const OUT = {issued: 0, live: 0, peak: 0, rendered: []};
const waiting = [];
const fetch = (url, opts) => {
  OUT.issued++; OUT.live++; OUT.peak = Math.max(OUT.peak, OUT.live);
  return new Promise(resolve => waiting.push(payload => {
    OUT.live--; resolve({json: async () => payload});
  }));
};
const render = d => OUT.rendered.push(d);
""" + pure_poll_js() + """
const settle = () => new Promise(r => setImmediate(r));
(async () => {
  const ticks = [];
  for (let i = 0; i < 17; i++) ticks.push(tick());
  await settle();
  OUT.duringTheBurst = {issued: OUT.issued, peak: OUT.peak, drawn: OUT.rendered.length};
  waiting.shift()({served_at: "12:00:00", n: 1});
  OUT.outcomes = await Promise.all(ticks);
  await settle();
  OUT.afterTheAnswer = {issued: OUT.issued, drawn: OUT.rendered.length};
  const later = tick();
  await settle();
  waiting.shift()({served_at: "12:00:35", n: 2});
  OUT.later = await later;
  OUT.final = {issued: OUT.issued, peak: OUT.peak, rendered: OUT.rendered};
  console.log(JSON.stringify(OUT));
})();
"""))

    assert out["duringTheBurst"] == {"issued": 1, "peak": 1, "drawn": 0}
    assert out["outcomes"] == ["rendered"] + ["skipped"] * 16
    assert out["afterTheAnswer"] == {"issued": 1, "drawn": 1}, (
        "a skipped tick must not draw — the display stays on the last completed poll"
    )
    assert out["later"] == "rendered"
    assert out["final"]["peak"] == 1, "never more than one request outstanding"
    assert out["final"]["issued"] == 2, "the guard releases once the answer lands"
    assert [row["n"] for row in out["final"]["rendered"]] == [1, 2]
    assert [row["served_at"] for row in out["final"]["rendered"]] == [
        "12:00:00", "12:00:35"
    ], "every figure drawn came from a poll that returned"


def test_a_request_that_never_settles_cannot_latch_the_guard_off():
    """The fail-open shape: a guard that latches ON is the same dead page by
    another route. A suspended laptop or a dropped socket leaves a request that
    never settles, and without the deadline this tab would stop polling for as
    long as it stayed open.

    The timers are faked so the 120s deadline can be fired directly; the real
    `AbortController` does the aborting.
    """
    out = json.loads(run_js("""
const timers = [];
const setTimeout = (fn, ms) => { timers.push({fn, ms, cleared: false}); return timers.length - 1; };
const clearTimeout = id => { if (timers[id]) timers[id].cleared = true; };
let issued = 0;
const drawn = [];
const fetch = (url, opts) => {
  issued++;
  return new Promise((resolve, reject) => {
    opts.signal.addEventListener("abort", () => reject(new Error("aborted")));
  });
};
const render = d => drawn.push(d);
""" + pure_poll_js() + """
const settle = () => new Promise(r => setImmediate(r));
(async () => {
  const stuck = tick();
  await settle();
  const skipped = await tick();
  const armed = timers[0];
  armed.fn();
  const stuckOut = await stuck;
  await settle();
  const next = tick();
  await settle();
  const issuedAfter = issued;
  timers[timers.length - 1].fn();
  const nextOut = await next;
  console.log(JSON.stringify({deadlineMs: armed.ms, cleared: armed.cleared,
    skipped, stuckOut, nextOut, issuedAfter, drawn: drawn.length}));
})();
"""))

    assert out["deadlineMs"] == 120000, (
        "far longer than any sweep observed (35s), far shorter than forever"
    )
    assert out["skipped"] == "skipped", "the tick during the stuck request did nothing"
    assert out["stuckOut"] == "failed"
    assert out["cleared"] is True, "the deadline is cancelled once the tick settles"
    assert out["issuedAfter"] == 2, "the next tick polls again — the guard did not latch"
    assert out["nextOut"] == "failed"
    assert out["drawn"] == 0, "a poll that never answered draws nothing"


def test_no_error_path_leaves_the_page_stuck_on_a_poll_that_failed():
    """Every way a poll can go wrong, in one tab, in order: a refused socket, a
    body that is not JSON, a body that cannot be read, and a `render` that
    throws. Each must clear the guard, and the last tick must still draw."""
    out = json.loads(run_js("""
let mode = "network";
let issued = 0;
const drawn = [];
const fetch = () => {
  issued++;
  if (mode === "network") return Promise.reject(new Error("connection refused"));
  if (mode === "badbody") return Promise.resolve({json: () => Promise.reject(new SyntaxError("not json"))});
  if (mode === "unreadable") return Promise.resolve({json: async () => { throw new Error("no body"); }});
  return Promise.resolve({json: async () => ({served_at: "12:01:10"})});
};
const render = d => { if (mode === "drawthrows") throw new Error("boom"); drawn.push(d); };
""" + pure_poll_js() + """
(async () => {
  const seen = {};
  for (const m of ["network", "badbody", "unreadable", "drawthrows", "ok"]) {
    mode = m;
    seen[m] = await tick();
  }
  console.log(JSON.stringify({seen, issued, drawn: drawn.length}));
})();
"""))

    assert out["seen"] == {
        "network": "failed", "badbody": "failed", "unreadable": "failed",
        "drawthrows": "failed", "ok": "rendered",
    }
    assert out["issued"] == 5, "each tick after a failure still issued its own request"
    assert out["drawn"] == 1


def test_a_throw_before_the_request_is_even_made_does_not_latch_the_guard():
    """The narrowest fail-open the guard can have: a throw AFTER `POLLING` is
    raised and BEFORE the `try` is entered would latch the flag on and the tab
    would never poll again — the dead page this change exists to fix, arriving
    through the fix. Everything after the flag therefore sits inside the block.

    Modelled with an `AbortController` that cannot be constructed, which is the
    only thing on that line that could throw.
    """
    out = json.loads(run_js("""
let AbortController = function(){ throw new ReferenceError("AbortController is not defined"); };
let issued = 0;
const drawn = [];
const fetch = () => { issued++; return Promise.resolve({json: async () => ({served_at: "12:02:00"})}); };
const render = d => drawn.push(d);
const RealAbortController = globalThis.AbortController;
""" + pure_poll_js() + """
(async () => {
  const broken = await tick();
  AbortController = RealAbortController;
  const repaired = await tick();
  console.log(JSON.stringify({broken, repaired, issued, drawn: drawn.length}));
})();
"""))

    assert out["broken"] == "failed", "the tick reported a failed poll"
    assert out["issued"] == 1, "the broken tick never reached the network"
    assert out["repaired"] == "rendered", (
        "the very next tick polled — a latched guard would have said 'skipped'"
    )
    assert out["drawn"] == 1


def test_the_page_polls_once_and_invents_no_clock_of_its_own():
    """The second constraint, asserted structurally because it is an absence.

    A skipped tick may leave stale figures on screen; it may never age them
    locally into a figure no poll ever returned. `elapsed_seconds` and
    `served_at` are computed server-side and rendered straight from the
    payload, `renderProgress` is reached only from `render`, and `render` is
    reached only from a completed poll or a forced redraw of `LAST`.
    """
    script = PAGE.split("<script>", 1)[1]

    assert script.count("setInterval(") == 1, "one timer on the page, and it is the poll"
    assert "setInterval(tick, 2000)" in script
    assert script.count('fetch("/api/state"') == 1, "one place asks the server for state"
    assert "Date.now(" not in script and "new Date(" not in script, (
        "nothing on this page runs a clock — every elapsed figure is the server's"
    )
    assert script.count("renderProgress(") == 2, (
        "defined once, called once, from `render` — a second caller could advance "
        "the live figures without a poll behind them"
    )
    assert "let POLLING = false;" in script


# --- the measurement ---------------------------------------------------------

MEASURED_CALLERS = 8
MEASURED_TASKS = 12


def measurement_repo(tmp_path):
    """A checkout whose sweep costs real git work: an origin, twelve completed
    tasks and a published branch for each, so `merge_report` and
    `disagreement_report` ask git about every one of them.

    BOTH arms of the measurement run against THIS repository, in this process,
    which is the objection two earlier rounds died on — a before taken against
    one tree and an after against another is not a measurement.
    """
    repo = make_repo(tmp_path)
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "--bare", "-q", str(origin))
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "origin", "work")

    tasks = []
    for index in range(MEASURED_TASKS):
        task_id = f"m-{index:02d}"
        head = run_git(repo, "rev-parse", "HEAD").strip()
        run_git(repo, "push", "-q", "origin", f"{head}:refs/heads/autoloop/{task_id}")
        (repo / f"f{index}.txt").write_text(f"{index}\n")
        run_git(repo, "add", f"f{index}.txt")
        run_git(repo, "commit", "-q", "-m", f"Merge task {task_id} into autoloop/mainline")
        tasks.append(completed(task_id))
    write_registry(repo, tasks)
    return repo


def test_the_pile_up_before_and_after_measured_in_one_repository(tmp_path, monkeypatch):
    """The before/after, taken in one process against one checkout.

    Each arm starts `MEASURED_CALLERS` callers and reports three numbers: how
    many `collect()` sweeps ran, how many subprocesses those sweeps launched
    (through `_run_status`, the single subprocess entry point — git, plus the
    one `ps` that reads the agent table), and the wall clock.

    The AFTER arm runs FIRST on purpose. It is the arm this change is meant to
    favour, so giving the BEFORE arm the warm filesystem is the conservative
    ordering rather than the flattering one.

    Both arms rendezvous before doing their work, so neither is measured against
    a straggler: the before arm's callers meet at a barrier inside `collect`,
    and the after arm's leader waits until every other caller has provably
    joined its sweep. That rendezvous is the only thing either arm is made to
    wait for, and both pay it.
    """
    import autoloop.dashboard as dash

    repo = measurement_repo(tmp_path)
    real_collect = dash.collect
    real_status = dash._run_status
    caches = (dash._REMOTE_CACHE, dash._ANCESTRY_CACHE, dash._SHALLOW_CACHE,
              dash._SUBJECT_CACHE)

    def arm(entry, gate):
        for cache in caches:
            cache.clear()
        sweeps: list = []
        git_calls: list = []
        tally = threading.Lock()

        def counted_status(*args, **kwargs):
            with tally:
                git_calls.append(1)
            return real_status(*args, **kwargs)

        def counted_collect(target):
            with tally:
                sweeps.append(target)
            gate()
            return real_collect(target)

        monkeypatch.setattr(dash, "collect", counted_collect)
        monkeypatch.setattr(dash, "_run_status", counted_status)
        errors: list = []

        def caller():
            try:
                entry(repo)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=caller, daemon=True)
            for _ in range(MEASURED_CALLERS)
        ]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)
        wall = time.perf_counter() - started
        assert all(not t.is_alive() for t in threads), "a measured caller never returned"
        assert not errors, f"a measured caller failed: {errors[0]!r}"
        return {"sweeps": len(sweeps), "git": len(git_calls), "wall": wall}

    with joins_recorded(monkeypatch) as joined:
        after = arm(
            lambda target: dash.collect_shared(target),
            lambda: wait_until(
                lambda: len(joined) >= MEASURED_CALLERS - 1,
                "every other caller to join the one sweep",
                timeout=60,
            ),
        )

    barrier = threading.Barrier(MEASURED_CALLERS, timeout=60)
    before = arm(lambda target: dash.collect(target), barrier.wait)

    measurement = (
        f"dash-21 measurement — one repository, {MEASURED_TASKS} completed tasks, "
        f"{MEASURED_CALLERS} concurrent callers. "
        f"BEFORE (a sweep per caller, as ThreadingHTTPServer did): "
        f"sweeps={before['sweeps']} subprocesses={before['git']} "
        f"wall={before['wall']:.2f}s | "
        f"AFTER (collect_shared): sweeps={after['sweeps']} "
        f"subprocesses={after['git']} wall={after['wall']:.2f}s"
    )
    print(measurement)

    assert before["sweeps"] == MEASURED_CALLERS, (
        "the before arm is the unguarded path: one sweep per caller"
    )
    assert after["sweeps"] == 1, "the after arm runs one sweep for all eight callers"
    assert after["git"] < before["git"], measurement
    # The wall clock is REPORTED, not asserted, and the docstring above says why
    # the arms are ordered the way they are. Eight sweeps on eight threads spend
    # most of their time inside git subprocesses with the GIL released, so on a
    # machine with cores to spare the before arm can finish in not much more
    # than one sweep's wall clock while doing eight sweeps' work — an assertion
    # on it would be measuring the host's core count. The count of git
    # subprocesses above is the same cost without that confound, and the failure
    # the operator actually saw was requests stacking, which `sweeps` states
    # exactly.
    #
    # Observed 2026-08-24 on this repository, recorded in `docs/AUTOLOOP.md` §4i:
    # BEFORE 8 sweeps / 141 subprocess launches / 3.06s, AFTER 1 sweep / 20
    # launches / 1.96s. The wall clock moved least, for the reason above, and it
    # is quoted with the other two rather than on its own.


# --- stale process vs unreadable data: two faults, two renderings ------------


def test_a_genuinely_unreadable_graph_still_reports_as_unreadable(tmp_path):
    """The branch that must survive. `task_groups` returning `[]` still means
    "could not be read", because real corruption still needs saying — what
    changed is that STALENESS stopped being reported through it."""
    repo = make_repo(tmp_path)
    write_tasks(repo, [a_task("dash-03", depends_on=["nope-nothing-by-that-name"])])

    payload = collect(repo)
    assert payload["groups"] == [], "a dangling dependency is a real unreadable graph"
    assert payload["stats"]["readable"] is False
    # …and it is NOT reported as a process problem.
    assert payload["build"]["upgrade"]["state"] == "current"


def test_the_two_faults_are_distinguishable_on_the_page(tmp_path):
    """Both true at once — the 2026-08-22 shape, except that there the data was
    fine and only the reader was old. The payload has to carry them on separate
    axes so the page can say which one the operator has."""
    repo = make_repo(tmp_path)
    write_tasks(repo, [a_task("dash-03", depends_on=["nope-nothing-by-that-name"])])
    arm_upgrade(repo, upgrade_record(running_tree()))

    payload = collect(repo)
    assert payload["groups"] == []
    assert payload["build"]["upgrade"]["state"] == "stale"
    # The process verdict never speaks the data's vocabulary, in either
    # direction — that conflation is the whole defect.
    assert "task graph" not in payload["build"]["upgrade"]["note"]
    assert "tasks.json" not in payload["build"]["upgrade"]["note"]


def pure_upgrade_js() -> str:
    """The banner helpers, lifted verbatim out of the served page. Payload-in /
    string-out, no DOM and no module state, so they run directly."""
    script = PAGE.split("<script>", 1)[1]
    esc_line = next(
        line for line in script.splitlines() if line.startswith("const esc =")
    )
    region = script.split("// PURE_UPGRADE_START", 1)[1].split("// PURE_UPGRADE_END", 1)[0]
    return esc_line + "\n" + region


def test_the_page_says_which_fault_the_operator_has():
    """Executed, not grepped. Every non-`current` state produces a banner that
    names the PROCESS, and the "could not be read" branches gain a caveat
    pointing at it — because on 2026-08-22 an operator read "tasks.json did not
    load as a registry" and went and audited a perfectly healthy file."""
    states = ["stale", "stale_settled", "not_this_tree", "unreadable_marker",
              "preflight_failed", "exec_failed"]
    harness = pure_upgrade_js() + """
const out = {};
for (const state of __STATES__) {
  out[state] = {
    banner: upgradeBanner({state, base_sha: "b".repeat(40), task_id: "preempt-01",
                           note: "the note"}),
    caveat: graphCaveat({state}),
  };
}
out.current = {banner: "", caveat: graphCaveat({state: "current"})};
out.none = {banner: "", caveat: graphCaveat(null)};
console.log(JSON.stringify(out));
""".replace("__STATES__", json.dumps(states))
    rendered = json.loads(run_js(harness))

    # Five states, five different sentences. A shared one would put two faults
    # that call for opposite actions behind the same words, which is the defect
    # this whole task is about, one level up.
    assert len({rendered[s]["banner"] for s in states}) == len(states)
    for state in states:
        banner = rendered[state]["banner"]
        assert "bbbbbbbbbbbb" in banner and "bbbbbbbbbbbbb" not in banner, (
            "the sha is shown, truncated to twelve"
        )
        assert "preempt-01" in banner and "the note" in banner
        assert "did not load as a registry" not in banner, (
            f"{state} is a PROCESS fault and must never be phrased as a data one"
        )
        assert rendered[state]["caveat"], f"{state} must caveat the unreadable branch"
        assert "stale reader" in rendered[state]["caveat"]
    # The marker case names itself, because it is the one whose file lives in
    # the same directory as `tasks.json` and is the easiest to confuse with it.
    assert "NOT the task graph" in rendered["unreadable_marker"]["banner"]
    # Silence when there is nothing to say: a caveat on every page is a caveat
    # nobody reads, and it would libel a genuinely corrupt registry.
    assert rendered["current"]["caveat"] == ""
    assert rendered["none"]["caveat"] == ""


def test_the_unreadable_branches_carry_the_caveat_and_the_banner_carries_both():
    """Placement, in the served template: the two panels that have historically
    been blamed for a stale process, and the one box that says what it is."""
    script = PAGE.split("<script>", 1)[1]

    stats = script.split("function renderStats(s, up){", 1)[1].split("\nfunction ", 1)[0]
    assert "graphCaveat(up)" in stats
    board = script.split("function renderRoadmap(d){", 1)[1].split("\nfunction ", 1)[0]
    assert "graphCaveat(d.build && d.build.upgrade)" in board
    # The banner speaks for BOTH staleness questions — this file's own hash, and
    # the checkout moving under a process that loaded its code before it. On
    # 2026-08-22 the second was true and the first was not.
    assert "d.build.stale" in script and "d.build.upgrade" in script
    assert "upgradeBanner(up)" in script
    for state in ("stale", "stale_settled", "not_this_tree", "unreadable_marker",
                  "preflight_failed", "exec_failed"):
        assert f"{state}:" in script.split("const UPGRADE_SAY = {", 1)[1], \
            f"{state} would render as a bare state name"


def test_every_backend_upgrade_state_has_a_sentence_on_the_page():
    """The `MERGE_GROUPS` rule, applied one panel up: a state the backend can
    emit and the template cannot spell would reach an operator as a bare word."""
    import autoloop.dashboard as dash

    say = PAGE.split("const UPGRADE_SAY = {", 1)[1].split("\n};", 1)[0]
    for state in dash.UPGRADE_VIEW_STATES:
        if state == "current":
            assert f"{state}:" not in say, "current has nothing to say"
            continue
        assert f"{state}:" in say, f"{state} has no sentence on the page"


# --- end to end: a stale dashboard really does serve the new code ------------


def free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_a_stale_dashboard_ends_up_serving_the_new_code(tmp_path):
    """The claim end to end, in a real process: a dashboard started before the
    checkout moved serves the code that was on disk when it started, and after
    the signal is armed it serves the code that is on disk now.

    A COPY of this package is what the child runs, so the marker below is an
    edit to that copy and this repository is never written to. The marker is
    inserted ABOVE the `__main__` guard on purpose — under `python -m`, the
    guard's body runs as part of the module and anything after it never
    executes.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".autoloop").mkdir()
    shutil.copytree(
        REPO_ROOT / "autoloop",
        checkout / "autoloop",
        ignore=shutil.ignore_patterns("tests", "__pycache__"),
    )
    module = checkout / "autoloop" / "dashboard.py"
    port = free_port()
    log = tmp_path / "child.log"
    child = None
    try:
        with log.open("wb") as sink:
            child = subprocess.Popen(
                [sys.executable, "-m", "autoloop.dashboard",
                 "--repo", str(checkout), "--port", str(port)],
                cwd=str(checkout), stdout=sink, stderr=subprocess.STDOUT,
            )
        base = f"http://127.0.0.1:{port}"

        def page(deadline=30.0):
            end = time.time() + deadline
            last = None
            while time.time() < end:
                if child.poll() is not None:  # pragma: no cover - a dead child
                    raise AssertionError(
                        f"the dashboard exited {child.returncode}: "
                        f"{log.read_text(errors='replace')[-2000:]}"
                    )
                try:
                    return get(base)[1]
                except (urllib.error.URLError, OSError) as exc:
                    last = exc
                    time.sleep(0.1)
            raise AssertionError(f"the dashboard never answered: {last}")

        before = page()
        assert "RELOADED-BY-LOOP-03" not in before
        assert "<!doctype html>" in before

        # `utcnow_iso()` truncates to SECONDS, and the child's start time does
        # not. A record written in the same wall-clock second as the child's
        # import would floor to at or before it and read as already current —
        # so wait out the second rather than racing it. (In production that
        # truncation only ever errs toward "current", i.e. toward not
        # restarting, which is the safe direction; here it would just hang.)
        time.sleep(1.2)

        # The checkout moves: new code on disk, and the loop's own signal armed.
        module.write_text(
            module.read_text(encoding="utf-8").replace(
                'if __name__ == "__main__":',
                'PAGE = PAGE + "<!-- RELOADED-BY-LOOP-03 -->"\n\n\n'
                'if __name__ == "__main__":',
                1,
            ),
            encoding="utf-8",
        )
        from autoloop.auto_merge import UpgradeStore

        UpgradeStore(checkout / ".autoloop" / "pending_upgrade.json").save(
            upgrade_record(checkout, paths=["autoloop/dashboard.py"])
        )

        end = time.time() + 120
        served = ""
        while time.time() < end:
            served = page()
            if "RELOADED-BY-LOOP-03" in served:
                break
            time.sleep(0.25)
        assert "RELOADED-BY-LOOP-03" in served, (
            "the dashboard went on serving the code it started with"
        )
        assert "<!doctype html>" in served, "and the new page is whole"
        # Same pid: it replaced its own image, nothing restarted it.
        assert child.poll() is None
        assert "restarting into" in log.read_text(errors="replace")
    finally:
        if child is not None:
            child.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=10)
