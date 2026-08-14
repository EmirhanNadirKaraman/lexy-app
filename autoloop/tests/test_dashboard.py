"""The live tracker. Read-only is the load-bearing property: the loop's escape
detector refuses a write-capable task if the primary checkout is dirty, so a
tracker that touched the working tree would stop the thing it observes."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autoloop.dashboard import (
    MARKS,
    PAGE,
    STATUS,
    app_tasks,
    collect,
    is_ancestor,
    merge_states,
    pipeline,
    worker_progress,
)
from autoloop.state import utcnow_iso


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


# ---- priority editing (the one write path) -----------------------------------


def test_the_post_endpoint_writes_only_to_the_inbox(tmp_path, monkeypatch):
    """The write path must not touch the repo or the state dir. A write into
    `.autoloop/` mid-run would trip the escape detector and park the loop
    loop-fatal, which is the whole reason the inbox lives outside the checkout."""
    import autoloop.dashboard as dash
    from autoloop.inbox import TaskInbox

    repo = make_repo(tmp_path)
    inbox_dir = tmp_path / "outside" / "inbox"
    monkeypatch.setattr(dash, "_inbox_dir", lambda _repo: inbox_dir)

    before = snapshot(repo)
    TaskInbox(dash._inbox_dir(repo)).submit_priority("rt-01", 3)
    assert snapshot(repo) == before, "the checkout must be untouched"

    specs, problems = TaskInbox(inbox_dir).drain()
    assert problems == []
    assert specs == [{"kind": "priority", "id": "rt-01", "priority": 3}]


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
