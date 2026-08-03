"""The live tracker. Read-only is the load-bearing property: the loop's escape
detector refuses a write-capable task if the primary checkout is dirty, so a
tracker that touched the working tree would stop the thing it observes."""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from autoloop.dashboard import MARKS, PAGE, STATUS, app_tasks, collect, pipeline


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
