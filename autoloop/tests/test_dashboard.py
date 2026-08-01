"""The live tracker. Read-only is the load-bearing property: the loop's escape
detector refuses a write-capable task if the primary checkout is dirty, so a
tracker that touched the working tree would stop the thing it observes."""

from __future__ import annotations

import json
import subprocess
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
