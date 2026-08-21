"""Why the merge window is shut, on the page, from the LIVE check.

`GET /api/state` reported the outcome — `merge.counts = {'merged': 65,
'unmerged': 6, ...}` — and nothing about the cause. `cli._merge_window_blockers`
was referenced in `cli.py`, `auto_merge.py` and `merge_sweep.py` and NOWHERE in
`dashboard.py`, so the page could say six branches had not landed and could not
say what was stopping them.

The only rendered form of the answer was the startup sweep's line in
`.autoloop/logs/loop-*.log`, and a log line is a snapshot taken when a sweep last
ran. On 2026-08-21 that produced three wrong conclusions in one session: a task
reported as holding the window forty minutes after it had published; two records
nearly retired as "holders" that the live check already exempted as NOTES; and a
round nearly interrupted for a jam that did not exist.

What these tests hold to, in the order they would break something:

1. **Called, not copied.** A fourth caller of `cli._merge_window_blockers`. A
   second implementation on this page could disagree with the loop about whether
   a merge is safe, which is the worst outcome available here.
2. **Read from the CHECKOUT, never from the process's cwd.** `load_config` keeps
   `[paths].state_dir` relative, so a dashboard started anywhere else would glob
   an empty (or someone else's) executions directory, find no candidate, and
   render the window OPEN. That failure comes from a working directory rather
   than from anything git said, and it is the single worst output this panel can
   produce.
3. **Reasons and notes stay apart.** A reason shuts the window; a note says a
   RECORD is wrong and shuts nothing. Collapsed into one list, a latent fault
   either looks like a blocker or disappears.
4. **Never an empty list that reads as open.** A remote that cannot be answered
   is a REASON naming the failure (the CLI is fail-closed and this page inherits
   that); a check that could not run at all is `unknown` with the cause in words.
5. **Read-only and lock-free**, while a real other process holds `LoopLock`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from autoloop import cli, dashboard
from autoloop.dashboard import MERGE_WINDOW_STATUSES, PAGE, collect, merge_window
from autoloop.errors import GitCommandError
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore

URL = "https://chatgpt.com/c/dash-17"


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """The tracker memoizes remote refs, ancestry verdicts and shallowness at
    module level; `collect()` populates all three. Cleared around every test for
    the same reason `test_dashboard.py` does it — two repos built in the same
    wall-clock second get identical commit shas in different directories."""
    caches = (dashboard._REMOTE_CACHE, dashboard._ANCESTRY_CACHE, dashboard._SHALLOW_CACHE)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


def run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


def snapshot(root: Path) -> dict:
    return {p: p.stat().st_mtime_ns for p in Path(root).rglob("*") if p.is_file()}


def make_repo(tmp_path, phase: str = Phase.AWAITING.value) -> Path:
    """A checkout the merge-window check can actually RUN against.

    Deliberately richer than `test_dashboard.py`'s `make_repo`, which writes no
    `.autoloop/config.toml`: without one, `cli.load_config` raises and the whole
    window short-circuits to `unknown` before a single git call — so every test
    built on that fixture would pass without ever exercising this path.
    """
    repo = tmp_path / "repo"
    (repo / ".autoloop").mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "t@e.com")
    run_git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x\n")
    run_git(repo, "add", "f.txt")
    run_git(repo, "commit", "-q", "-m", "init")
    (repo / ".autoloop" / "config.toml").write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nworkers_root = "{tmp_path / "workers"}"\n',
        encoding="utf-8",
    )
    StateStore(repo / ".autoloop" / "state.json").save(
        LoopState(session_id="dash17", conversation_url=URL, phase=phase)
    )
    # An EMPTY registry on disk, rather than letting `cli._seed_registry` fall
    # back to the package's real `seed_tasks.json`. The check exempts a record
    # whose task reached a terminal registry state, so a test id that happened
    # to collide with a seeded one would change the answer for a reason no
    # reader of the test could see.
    TaskStore(repo / ".autoloop" / "tasks.json").save(TaskRegistry())
    return repo


def write_execution(repo: Path, task_id="t-1", **over) -> Path:
    """One execution record — the REAL hazard the window check exists for: a
    candidate pinned to a base, which a branch move would strand."""
    directory = repo / ".autoloop" / "executions"
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task_id,
        "candidate_sha": "abc123def456789",
        "task_base_sha": "000111222333444",
        "intended_remote": "",
        "intended_remote_ref": "",
        "worktree_path": "",
    }
    record.update(over)
    path = directory / f"{task_id}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


# ---- the payload: called, not copied ------------------------------------------


def test_the_payload_carries_the_windows_reasons_and_its_notes(tmp_path):
    """The provable claim. `/api/state` reported six unmerged branches and
    nothing about the cause; it now carries both halves of the live answer."""
    repo = make_repo(tmp_path)
    write_execution(repo, task_id="prov-01")

    window = collect(repo)["merge_window"]

    assert window["status"] == "closed"
    assert any("prov-01" in reason for reason in window["reasons"])
    # Both keys travel, always — a payload that omitted `notes` when there were
    # none would make "no records are wrong" and "this build has no notes"
    # indistinguishable to anything reading `/api/state`.
    assert window["notes"] == []


def test_the_reasons_and_notes_come_from_the_cli_check_not_a_second_implementation(
    tmp_path, monkeypatch
):
    """The bound that matters most. If this page computed its own window it
    could disagree with the loop about whether a merge is safe.

    Driven by patching `cli._merge_window_blockers` to return sentinels: a copy
    of the logic would still produce a plausible answer here (the record below
    is a real in-flight candidate) and would fail this test, which is the
    difference a structural grep cannot see.
    """
    repo = make_repo(tmp_path)
    write_execution(repo)
    calls = []

    def _sentinel(config, seen=None, git=None):
        calls.append((config, seen, git))
        return ["SENTINEL REASON"], ["SENTINEL NOTE"]

    monkeypatch.setattr(cli, "_merge_window_blockers", _sentinel)

    window = merge_window(repo)

    assert len(calls) == 1, "the check runs exactly once per request"
    assert window["reasons"] == ["SENTINEL REASON"]
    assert window["notes"] == ["SENTINEL NOTE"]
    config, seen, git = calls[0]
    # The loop's own config, with the state dir made absolute against the
    # OBSERVED checkout, and a gateway rooted there rather than at `Path.cwd()`
    # (which is what `cli._window_git` would have built).
    assert config.state_dir == repo / ".autoloop"
    assert git.repo_root == repo
    # A fresh memo per request. One shared across requests would freeze a
    # confirmed publication that a force-push had since invalidated.
    assert seen == set()


def test_the_window_carries_no_clock_of_its_own(tmp_path):
    """`served_at` is the computation time, and there is exactly one of them.

    `render()` builds its re-render signature from the payload MINUS `served_at`
    and `progress`. A timestamp nested inside `merge_window` would therefore
    differ on every 2s poll, rebuilding the whole page forever and snapping every
    disclosure an operator had opened shut.
    """
    repo = make_repo(tmp_path)

    payload = collect(repo)

    assert set(payload["merge_window"]) == {"status", "reasons", "notes", "detail"}
    assert payload["served_at"]
    script = PAGE.split("<script>", 1)[1]
    assert "const {served_at, progress, ...rest} = d;" in script
    assert "renderMergeWindowStamp(served_at);" in script


# ---- what the live check actually answers -------------------------------------


def test_a_quiet_loop_with_no_candidate_reports_an_open_window(tmp_path):
    """Open is `reasons == []` AND the word — never one without the other."""
    repo = make_repo(tmp_path)

    window = merge_window(repo)

    assert window["status"] == "open"
    assert window["reasons"] == []
    assert window["detail"] == ""


def test_every_in_flight_candidate_gets_its_own_reason(tmp_path):
    """Every reason, not the first one. An operator asking "why has nothing
    merged" needs all of the holders, not a sample of them."""
    repo = make_repo(tmp_path)
    write_execution(repo, task_id="dash-17")
    write_execution(repo, task_id="merge-04", candidate_sha="feedfacefeedface")

    window = merge_window(repo)

    assert window["status"] == "closed"
    assert len(window["reasons"]) == 2
    joined = " ".join(window["reasons"])
    assert "dash-17" in joined and "merge-04" in joined
    # The candidate sha is in the text, so the claim is inspectable rather than
    # asserted — the same shape the CLI prints.
    assert "abc123def456" in joined and "feedfacefeed" in joined


def test_an_executing_phase_is_a_reason_like_any_other(tmp_path):
    """The transient case. It closes the window and it clears by itself, so it
    is reported as a reason and NOT classified apart — what closes the window is
    the CLI's question (merge-04's to change), never this page's."""
    repo = make_repo(tmp_path, phase=Phase.EXECUTING.value)

    window = merge_window(repo)

    assert window["status"] == "closed"
    assert any("executing" in reason for reason in window["reasons"])


def test_the_window_is_read_from_the_checkout_not_from_the_process_cwd(tmp_path, monkeypatch):
    """`load_config` keeps `[paths].state_dir` exactly as written, and the
    shipped value is the relative `.autoloop`.

    So a dashboard serving `--repo X` from any other working directory would
    glob a DIFFERENT executions directory. The `elsewhere/.autoloop` below is
    what makes that fatal rather than merely wrong: with it present the check
    finds an empty directory, reports no reasons, and the page says the window is
    OPEN while a real candidate is in flight. Asserting only `closed` would pass
    on the broken version too (an ABSENT directory is its own fail-closed
    reason), which is why the reason text is asserted as well.
    """
    repo = make_repo(tmp_path)
    write_execution(repo)
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / ".autoloop").mkdir(parents=True)
    monkeypatch.chdir(elsewhere)

    window = merge_window(repo)

    assert window["status"] == "closed"
    assert any("t-1" in reason for reason in window["reasons"])
    assert "does not exist" not in " ".join(window["reasons"])


# ---- failure never reads as "open" --------------------------------------------


def test_a_remote_that_cannot_be_read_is_a_reason_never_an_empty_open_window(tmp_path):
    """A record naming a push intent whose remote cannot be answered.

    `origin` is pointed at a path that does not exist, rather than left
    unconfigured: an absent remote name could in principle be rescued by an
    ambient `url.<x>.insteadOf` on the machine running the suite, and this test
    is about what happens when the answer cannot be had — so the failure is made
    deterministic instead of assumed.

    `cli._candidate_publication` is fail-closed — only an `ls-remote` equal to
    the candidate counts as published — so the failure becomes a REASON naming
    it, and the page reads CLOSED with the cause in words. That is deliberately
    not `unknown`: the verdict has to stay identical to the one the loop acts on.
    """
    repo = make_repo(tmp_path)
    run_git(repo, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))
    write_execution(repo, intended_remote="origin",
                    intended_remote_ref="refs/heads/autoloop/t-1")

    window = merge_window(repo)

    assert window["status"] == "closed"
    assert window["reasons"], "a failed remote read must never leave an empty list"
    assert any("could not verify" in reason for reason in window["reasons"])
    assert any("origin/refs/heads/autoloop/t-1" in reason
               for reason in window["reasons"])


def test_a_remote_that_answers_and_has_no_such_branch_is_a_reason_too(tmp_path):
    """The other half, and the one that proves the reason above is not just
    "any git error": a REACHABLE remote that simply does not carry the branch is
    still not-published, so the candidate is still in flight and the window is
    still shut — with a different sentence."""
    repo = make_repo(tmp_path)
    empty_remote = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", str(empty_remote))
    run_git(repo, "remote", "add", "origin", str(empty_remote))
    write_execution(repo, intended_remote="origin",
                    intended_remote_ref="refs/heads/autoloop/t-1")

    window = merge_window(repo)

    assert window["status"] == "closed"
    assert any("does not exist" in reason for reason in window["reasons"])


def test_a_checkout_with_no_loop_config_reads_unknown_and_says_why(tmp_path):
    """`unknown` is "the check could not be RUN", and it is never open."""
    repo = make_repo(tmp_path)
    (repo / ".autoloop" / "config.toml").unlink()

    window = merge_window(repo)

    assert window["status"] == "unknown"
    assert window["reasons"] == []
    assert "config file not found" in window["detail"]


def test_a_check_that_raises_reads_unknown_rather_than_open(tmp_path, monkeypatch):
    """Every failure of the check itself lands here — a corrupt state file, a
    conversation url the loop would refuse, a gateway that will not build. None
    of them may render as an open window, and the page must not 500 either."""
    repo = make_repo(tmp_path)

    def _explode(config, seen=None, git=None):
        raise GitCommandError("git rev-parse HEAD failed (rc=128): not a repository")

    monkeypatch.setattr(cli, "_merge_window_blockers", _explode)

    window = merge_window(repo)

    assert window["status"] == "unknown"
    assert window["reasons"] == [] and window["notes"] == []
    assert "not a repository" in window["detail"]


def test_a_hanging_git_call_is_reported_as_a_failure_rather_than_wedging_the_page():
    """`GitGateway` passes no `timeout=` — every other caller is a loop or a CLI
    command that may block. This one sits on a 2s poll, so an `ls-remote` against
    an unreachable host would hang the request thread forever.

    Reported as a FAILED call rather than raised, on purpose: raising abandons
    the whole check on one slow ref and renders `unknown`, while a non-zero exit
    becomes a per-record reason through the path that already handles a remote
    that would not answer.
    """
    out = dashboard._window_runner(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        capture_output=True, text=True, timeout=0.5,
    )

    assert out.returncode == 1
    assert "timed out" in out.stderr


# ---- read-only and lock-free --------------------------------------------------


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


def test_reading_the_window_writes_nothing_and_never_waits_for_the_loop_lock(tmp_path):
    """Both halves of the posture, against a lock a REAL other process owns.

    Read-only is load-bearing here in a way it is not for the panels above: the
    window's git calls go through `GitGateway`, which does NOT get this module's
    `--no-optional-locks` injection (that lives in `_run_status`, and nothing
    routes through it) — so a call added to the check that refreshed the index
    would have a 2s poll rewriting `.git/index` in the checkout the loop's escape
    detector snapshots.

    Lock-free is measured rather than implied: `LoopLock` is held for an ENTIRE
    run, so a read that waited for it would be waiting for the loop to stop.
    """
    import autoloop
    from autoloop.errors import LockHeldError
    from autoloop.lock import LoopLock

    repo = make_repo(tmp_path)
    # A remote that is configured and unreachable, so the check really does
    # reach the gateway (the half with no `--no-optional-locks`) and does so
    # deterministically, without a network round-trip.
    run_git(repo, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))
    write_execution(repo, intended_remote="origin",
                    intended_remote_ref="refs/heads/autoloop/t-1")
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
        # AFTER the lock exists on disk, so the lock file is part of `before`.
        status_before = run_git(repo, "status", "--porcelain")
        before = snapshot(repo)
        started = time.monotonic()
        # Through `collect`, not `merge_window` — the claim is about the
        # ENDPOINT. `test_dashboard.py`'s own writes-nothing test is vacuous for
        # this path (its `make_repo` writes no `config.toml`, so the window
        # short-circuits to `unknown` before a single git call), so if this one
        # narrowed to the function, nothing would cover `/api/state` here.
        window = collect(repo)["merge_window"]
        elapsed = time.monotonic() - started
        after = snapshot(repo)
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
    assert after == before, "the window check must not create, remove or touch any file"
    assert run_git(repo, "status", "--porcelain") == status_before
    assert elapsed < 5.0, f"the read waited {elapsed:.1f}s on a lock it must not take"
    # And it still answered, rather than degrading to `unknown` under the lock.
    assert window["status"] == "closed"


# ---- the page -----------------------------------------------------------------


def merge_window_js() -> str:
    """The window panel's own code, lifted verbatim out of the served page.

    `esc` comes along because the region depends on it; the region carries no
    other module state, which is what lets it run against a stub document
    instead of a browser. The same shape as `test_dashboard.py`'s
    `merge_panel_js`, deliberately — a second mechanism for the same job would
    drift.
    """
    script = PAGE.split("<script>", 1)[1]
    esc_line = next(line for line in script.splitlines() if line.startswith("const esc ="))
    region = script.split("// MERGE_WINDOW_START", 1)[1].split("// MERGE_WINDOW_END", 1)[0]
    return "\n".join((esc_line, region))


def run_js(source: str) -> str:
    """Run `source` under node and return its stdout. Skipped rather than faked
    when node is absent — a hand-rolled JS interpreter would test itself."""
    node = shutil.which("node")
    if node is None:  # pragma: no cover - environment without node
        pytest.skip("node is required to run the page's own helpers")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(source)
        path = handle.name
    result = subprocess.run([node, path], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, f"the page's helpers threw:\n{result.stderr[:800]}"
    return result.stdout


def render_window(window: dict, served_at: str = "12:34:56") -> dict:
    harness = merge_window_js() + """
const NODES = {};
for (const id of ["mwstamp", "mwstatus", "mwreasons", "mwnotes"])
  NODES[id] = {innerHTML: "", textContent: ""};
const document = {getElementById: id => NODES[id]};
renderMergeWindowStamp(__STAMP__);
renderMergeWindow(__PAYLOAD__);
console.log(JSON.stringify({
  stamp: NODES.mwstamp.textContent,
  status: NODES.mwstatus.innerHTML,
  reasons: NODES.mwreasons.innerHTML,
  notes: NODES.mwnotes.innerHTML,
}));
""".replace("__PAYLOAD__", json.dumps({"merge_window": window})) \
   .replace("__STAMP__", json.dumps(served_at))
    return json.loads(run_js(harness))


def test_the_page_renders_an_open_window_as_open_with_no_reasons():
    """And says "none" rather than rendering nothing: an open window and a panel
    that failed to render must not look alike."""
    out = render_window({"status": "open", "reasons": [], "notes": [], "detail": ""})

    assert "OPEN" in out["status"] and "CLOSED" not in out["status"]
    assert "<li>" not in out["reasons"]
    assert "none" in out["reasons"]
    assert out["notes"] == "", "there are no notes, and no heading claiming any"


def test_the_page_renders_every_reason_and_keeps_the_notes_visibly_apart():
    """The two lists mean different things, so they render as two lists.

    A note describes a RECORD that is wrong and does not shut anything — the
    measured examples are a candidate whose worker repo is gone, and a published
    candidate whose record does not say so (a future park, named in advance).
    Folded in with the reasons, the first would look like a blocker and the
    second would be read as one more thing to wait for.
    """
    out = render_window({
        "status": "closed",
        "reasons": ["task dash-17 has a candidate (abc123def456) — never pushed",
                    "a phase is executing — an agent may be mid-write"],
        "notes": ["task prov-01: candidate 6ab4a529a4e2 is NOT in flight"],
        "detail": "",
    })

    assert "CLOSED" in out["status"]
    # EVERY reason, and the count in the heading — a panel that rendered the
    # first and dropped the rest is the log line's failure repeated.
    assert "dash-17" in out["reasons"] and "a phase is executing" in out["reasons"]
    assert '<span class="gc">2</span>' in out["reasons"]
    # Notes render, and they render SOMEWHERE ELSE. Both directions asserted:
    # one alone passes for a panel that prints everything twice.
    assert "prov-01" in out["notes"]
    assert "prov-01" not in out["reasons"], "a note must never render as a blocker"
    assert "dash-17" not in out["notes"]
    # The distinction is carried by a heading and an icon as well as by the
    # class, so it is never colour-alone.
    assert 'class="mwlist reason"' in out["reasons"]
    assert 'class="mwlist note"' in out["notes"]
    assert "Reasons the window is shut" in out["reasons"]
    assert "do NOT shut the window" in out["notes"]


def test_the_page_renders_an_unknown_check_as_not_open_and_says_why():
    out = render_window({"status": "unknown", "reasons": [], "notes": [],
                         "detail": "the merge window could not be computed: "
                                   "config file not found"})

    assert "UNKNOWN" in out["status"]
    assert "OPEN" not in out["status"]
    assert "config file not found" in out["status"]
    # An empty reason list under `unknown` is a THIRD sentence, not the open
    # window's: nothing was assessed, which is not the same claim as nothing
    # holding it. Getting that wrong would put the open window's reassurance
    # under a verdict that made no assessment at all.
    assert "nothing was assessed" in out["reasons"]
    assert "no reason is holding" not in out["reasons"]


def test_the_page_says_when_the_window_was_read():
    """The whole point of replacing a log line: the answer is only as good as
    the instant it was taken at, so the instant is on the page."""
    out = render_window({"status": "open", "reasons": [], "notes": [], "detail": ""})

    assert "12:34:56" in out["stamp"]
    # The stamp is written OUTSIDE the page-wide change guard, next to
    # `#served`, so it moves every poll instead of freezing whenever the payload
    # happens not to change.
    script = PAGE.split("<script>", 1)[1]
    before_guard = script.split("if (!force && sig === LASTJSON) return;", 1)[0]
    assert "renderMergeWindowStamp(served_at);" in before_guard
    assert "renderMergeWindow(d);" not in before_guard, \
        "the panel body belongs inside the guard — a rebuild every 2s erases a selection"


def test_a_reason_full_of_html_is_shown_as_text_not_run_as_markup():
    """Reason and note text carries worktree paths and raw git error strings —
    untrusted by construction, and this page has no framework escaping it."""
    out = render_window({
        "status": "closed",
        "reasons": ["<img src=x onerror=alert(1)>"],
        "notes": ["<script>alert(2)</script>"],
        "detail": "",
    })

    assert "<img" not in out["reasons"] and "&lt;img" in out["reasons"]
    assert "<script>" not in out["notes"] and "&lt;script&gt;" in out["notes"]


def test_the_page_never_classifies_a_reason_by_reading_its_text():
    """Reasons arrive as flat strings. Pattern-matching one to decide it is the
    transient case would be the page inventing semantics the CLI does not
    express, and it would break the moment the wording changed — what a reason
    MEANS is merge-04's question, not this panel's. The "clears by itself"
    relief is static prose instead."""
    script = PAGE.split("<script>", 1)[1]
    region = script.split("// MERGE_WINDOW_START", 1)[1].split("// MERGE_WINDOW_END", 1)[0]
    body = region.split("function renderMergeWindow(d){", 1)[1]

    for sniff in ("executing", "indexOf", ".includes(", "startsWith", ".match("):
        assert sniff not in body, f"a reason is classified by {sniff} instead of rendered"

    static_markup = PAGE.split("<script>", 1)[0]
    section = static_markup.split('id="mwstamp"', 1)[1].split("</section>", 1)[0]
    assert "a phase is executing" in section
    assert "clears by itself" in section


def test_every_window_status_has_an_icon_and_a_word_on_the_page():
    """The backend pins the vocabulary; the page may not invent a fourth answer
    or drop one. Same shape as `MERGE_STATES` / `DEP_NODE_STATES`."""
    script = PAGE.split("<script>", 1)[1]
    table = script.split("const MW = {", 1)[1].split("};", 1)[0]

    assert sorted(re.findall(r"(\w+):\[", table)) == sorted(MERGE_WINDOW_STATUSES)
    for status in MERGE_WINDOW_STATUSES:
        icon, word = re.search(rf'{status}:\["(.+?)","(.+?)"\]', table).groups()
        assert icon and word, f"{status} renders without an icon or without a word"


def test_the_window_panel_sits_above_the_panel_whose_question_it_answers():
    """Six branches not having landed is the OUTCOME; this panel is the cause.
    An operator who has to scroll past the outcome to reach the cause is back to
    grepping the log."""
    static_markup = PAGE.split("<script>", 1)[0]

    assert static_markup.index('id="mwstatus"') < static_markup.index('id="mergehead"')
    for element in ('id="mwstamp"', 'id="mwreasons"', 'id="mwnotes"'):
        assert element in static_markup, f"{element} is not in the static markup"
