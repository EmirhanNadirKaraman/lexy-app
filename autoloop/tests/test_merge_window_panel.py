"""WHY the merge window is shut, on the page (dash-19).

`/api/state` reported the outcome — 65 merged, 6 unmerged, 3 unpublished — and
nothing about the cause, so the only rendered form of "what is stopping the
merge" was a line the startup sweep prints into `.autoloop/logs/loop-*.log`. A
log line is a snapshot taken when a sweep last ran and it goes stale in silence:
on 2026-08-21 it produced a task reported as holding the window forty minutes
after it had published, two records nearly retired as holders that the live check
already exempted as notes, and a round nearly interrupted to clear a jam that did
not exist.

Two claims, and the file is built around the gap between them:

* the payload carries the window's reasons AND its notes, from
  `cli._merge_window_blockers` — CALLED, not reimplemented, so the page can
  never disagree with the loop about whether a merge is safe;
* the page renders reasons and notes as different things, says when it was
  computed, and degrades a failed check to a stated `unknown` that is never
  readable as "open".

The second half is where this class of change dies. A check that quietly reports
"nothing is holding the window" when it could not run is the fail-open answer
that a panel replacing a log line must never give.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from autoloop import cli, dashboard
from autoloop.dashboard import MERGE_WINDOW_STATES, PAGE, collect, merge_window
from autoloop.errors import GitCommandError
from autoloop.state import LoopState, Phase, StateStore, utcnow_iso
from autoloop.tasks import Task, TaskRegistry, TaskStore

URL = "https://chatgpt.com/c/dash-19"


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """Same reason as `test_dashboard.py`'s: the tracker memoizes remote refs,
    ancestry verdicts, shallowness and the commit-subject walk at module level,
    and `collect` is called here. Two repos built in the same wall-clock second
    have the same commit sha, so a verdict keyed on the sha alone leaks between
    them and fails only near a second boundary."""
    caches = (dashboard._REMOTE_CACHE, dashboard._ANCESTRY_CACHE,
              dashboard._SHALLOW_CACHE, dashboard._SUBJECT_CACHE)
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


def run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


def run_js(source: str) -> str:
    """Run `source` under node and return its stdout.

    A local copy of `test_dashboard.py`'s helper rather than an import: these
    test modules are not a package, so a cross-file import would depend on
    collection order putting the other file's directory on `sys.path` first.
    Skipped rather than faked when node is absent, for the reason that one is —
    a hand-rolled JS interpreter would be testing the interpreter.
    """
    import shutil
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


def make_repo(tmp_path, state_dir=".autoloop"):
    """An observed checkout with one commit and a config the loop would accept.

    `state_dir` is RELATIVE by default, which is what the shipped config ships
    and what makes this fixture worth having: `load_config` honours a relative
    value verbatim, so it resolves against the process's cwd rather than against
    the checkout. Every test here would pass against a cwd-resolved state dir if
    pytest happened to run from the right directory.
    """
    repo = tmp_path / "repo"
    (repo / ".autoloop").mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "t@e.com")
    run_git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("x\n")
    run_git(repo, "add", "f.txt")
    run_git(repo, "commit", "-q", "-m", "init")
    workers_root = tmp_path / "workers"
    (repo / ".autoloop" / "config.toml").write_text(
        "[browser]\n"
        f'conversation_url = "{URL}"\n'
        "\n"
        "[paths]\n"
        f'state_dir = "{state_dir}"\n'
        f'workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    return repo


def head_of(repo) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def write_execution(repo, task_id="t-1", candidate="", base="", remote="",
                    dest_ref="", worktree_path="", published=""):
    """One execution record, exactly the shape the loop writes."""
    d = repo / ".autoloop" / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "candidate_sha": candidate or "abc123def456abc123def456abc123def456abcd",
            "task_base_sha": base,
            "intended_remote": remote,
            "intended_remote_ref": dest_ref,
            "worktree_path": worktree_path,
            "published_sha": published,
        }),
        encoding="utf-8",
    )


def write_state(repo, phase=Phase.AWAITING.value):
    StateStore(repo / ".autoloop" / "state.json").save(
        LoopState(session_id="mw", conversation_url=URL, phase=phase)
    )


def write_registry(repo, *tasks):
    TaskStore(repo / ".autoloop" / "tasks.json").save(TaskRegistry(list(tasks)))


class _FakeCheckout:
    """The least a gateway has to answer, with the semantics real git has.

    Only used where a test needs a state a real repository cannot cheaply be put
    into (a published candidate, a remote that raises). Everything that CAN be
    driven against a real checkout is, below — a fake alone would prove the
    plumbing and nothing about the predicate.
    """

    def __init__(self, head="head1234", commits=(), refs=None, remote_error=None):
        self._head = head
        self.commits = set(commits)
        self.refs = dict(refs or {})
        self.remote_error = remote_error
        self.lookups = []

    def head_sha(self):
        return self._head

    def is_descendant(self, candidate, base):
        for oid in (candidate, base):
            if oid not in self.commits:
                raise GitCommandError("merge-base", f"{oid}: not a valid object name")
        return False

    def read_commit(self, oid):
        if oid not in self.commits:
            raise GitCommandError("cat-file", f"{oid}: bad file")
        return {"tree": "t", "parents": [], "message": ""}

    def object_exists(self, oid):
        return oid in self.commits

    def remote_ref_sha(self, remote, dest_ref):
        self.lookups.append((remote, dest_ref))
        if self.remote_error is not None:
            raise self.remote_error
        return self.refs.get((remote, dest_ref), "")


# ---- the payload: the loop's own predicate, called ---------------------------


def test_the_window_is_the_loops_own_predicate_called_not_a_second_copy(
    tmp_path, monkeypatch
):
    """THE structural claim, and the one a behavioural test cannot make. A
    dashboard that derived its own version of the window could disagree with the
    loop about whether a merge is safe — the worst outcome this panel could
    produce — and a reimplementation that happened to agree today would pass
    every other test in this file.

    Pinned at the SEAM, the same way `test_context.py` pins the context block's
    caller: a recorder over `cli._merge_window_blockers` asserts what it
    receives AND that the payload is its return, verbatim.
    """
    repo = make_repo(tmp_path)
    calls = []

    def recorder(config, seen, git):
        calls.append((config, seen, git))
        return ["reason one"], ["note one"]

    monkeypatch.setattr(cli, "_merge_window_blockers", recorder)

    window = merge_window(repo)

    assert len(calls) == 1, "one call per sweep, never two"
    config, seen, git = calls[0]
    assert seen == set(), "publications are memoized within a sweep and no longer"
    assert git is not None, "the gateway must be supplied, not left to Path.cwd()"
    assert Path(git.repo_root) == repo, "the gateway must be rooted at the checkout"
    assert window["reasons"] == ["reason one"]
    assert window["notes"] == ["note one"]
    assert window["state"] == "shut"


def test_the_state_directory_is_the_one_the_rest_of_the_page_reads(
    tmp_path, monkeypatch
):
    """The bug this is most likely to have, and the one that would be invisible.

    `_merge_window_blockers` globs `config.state_dir/executions/*.json`, and a
    relative `[paths].state_dir` resolves against the CALLER's cwd — which for
    this process is wherever the dashboard was launched. Left alone, a dashboard
    started from anywhere but the checkout reads an empty directory and reports
    OPEN, or reads ANOTHER checkout's records and is confidently wrong about the
    wrong repository. Its own docstring records that failure from 2026-08-04.
    """
    repo = make_repo(tmp_path, state_dir=".autoloop")   # relative, as shipped
    seen_configs = []
    monkeypatch.setattr(
        cli, "_merge_window_blockers",
        lambda config, seen, git: (seen_configs.append(config), ([], []))[1],
    )

    merge_window(repo)

    config = seen_configs[0]
    assert config.state_dir == dashboard._state_dir(repo) == repo / ".autoloop"
    assert config.state_dir.is_absolute(), "a relative dir would follow the cwd"
    # And every path derived from it, since those are what the predicate reads.
    assert config.tasks_file == repo / ".autoloop" / "tasks.json"
    assert config.state_file == repo / ".autoloop" / "state.json"


def test_an_open_window_is_reported_open_with_no_reasons(tmp_path):
    """Nothing in flight: no execution record, no executing phase. The real
    predicate against a real checkout, with no fake anywhere."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)

    window = merge_window(repo)

    assert window["state"] == "open"
    assert window["reasons"] == []
    assert window["detail"] == "", "an answered question reports no failure"


def test_a_candidate_bound_to_the_head_is_reported_with_the_task_holding_it(
    tmp_path,
):
    """The case a phase check misses, end to end against real git: a record
    whose base IS the current head. The operator's question is WHICH task, so
    the id has to survive into the payload."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_execution(repo, task_id="dash-19", base=head_of(repo))

    window = merge_window(repo)

    assert window["state"] == "shut"
    assert len(window["reasons"]) == 1
    reason = window["reasons"][0]
    assert "dash-19" in reason, f"the holder must be named: {reason}"
    assert "strand" in reason
    assert window["notes"] == []


def test_a_record_that_should_have_been_retired_is_a_note_and_not_a_reason(
    tmp_path,
):
    """The valuable half. This record does NOT close the window — its worker
    repo is gone and the checkout cannot resolve the candidate — but it is a
    defect an operator should see, and it appears on no other surface today."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_registry(repo, Task(id="prov-01", title="t", description="d"))
    write_execution(
        repo, task_id="prov-01",
        candidate="0" * 40,                      # no such object in this repo
        worktree_path=str(tmp_path / "gone" / "prov-01"),
    )

    window = merge_window(repo)

    assert window["state"] == "open", "a note has never closed the window"
    assert window["reasons"] == []
    assert len(window["notes"]) == 1
    note = window["notes"][0]
    assert "prov-01" in note and "NOT in flight" in note
    assert "should have been retired" in note


def test_a_published_candidate_is_a_note_naming_the_park_it_will_cause(tmp_path):
    """The second note shape, measured live on 2026-08-21: safe to merge past,
    but the record does not record its own publication, so a later revise would
    park it. It names a future failure in advance and nothing else reports it."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    landed = "8d96c52aeca4" + "0" * 28
    write_execution(repo, task_id="audit-0002", candidate=landed,
                    remote="origin", dest_ref="autoloop/task/audit-0002")
    git = _FakeCheckout(refs={("origin", "autoloop/task/audit-0002"): landed})

    window = merge_window(repo, git=git)

    assert window["state"] == "open"
    assert window["reasons"] == []
    assert any("audit-0002" in n and "published at origin/" in n
               for n in window["notes"]), window["notes"]
    assert any("would park it" in n for n in window["notes"])


def test_a_transient_reason_is_still_a_reason(tmp_path):
    """"A phase is executing" clears by itself and asks nothing of an operator,
    and it still closes the window. It is reported as a reason like any other —
    the page frames it, the payload does not filter it."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.EXECUTING.value)

    window = merge_window(repo)

    assert window["state"] == "shut"
    assert window["reasons"] == ["a phase is executing — an agent may be mid-write"]


def test_reasons_and_notes_stay_separate_all_the_way_to_the_payload(tmp_path):
    """One record holding the window and one written off, together. Collapsing
    the two lists would make a latent fault look like a blocker, or hide it."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_registry(repo, Task(id="prov-01", title="t", description="d"))
    write_execution(repo, task_id="prov-01", candidate="0" * 40,
                    worktree_path=str(tmp_path / "gone" / "prov-01"))
    write_execution(repo, task_id="dash-19", base=head_of(repo))

    window = merge_window(repo)

    assert window["state"] == "shut"
    assert [r for r in window["reasons"] if "dash-19" in r]
    assert [n for n in window["notes"] if "prov-01" in n]
    assert not [r for r in window["reasons"] if "prov-01" in r], (
        "a note must never be counted as a blocker"
    )
    assert not [n for n in window["notes"] if "dash-19" in n]


def test_every_reason_and_note_is_exactly_one_line(tmp_path, monkeypatch):
    """The reasons interpolate `GitError` messages, which carry git's stderr and
    can be multi-line. A list item whose text is three lines is a list that has
    stopped being a list."""
    repo = make_repo(tmp_path)
    monkeypatch.setattr(
        cli, "_merge_window_blockers",
        lambda *_a, **_k: (["holding\nthe\nwindow"], ["a\tnote\n  split over lines"]),
    )

    window = merge_window(repo)

    assert window["reasons"] == ["holding the window"]
    assert window["notes"] == ["a note split over lines"]


def test_the_payload_state_is_one_of_the_three_the_page_can_draw(tmp_path):
    """`MERGE_WINDOW_STATES` and what the backend emits are pinned equal: a
    fourth state would render as an empty panel."""
    repo = make_repo(tmp_path)
    assert merge_window(repo)["state"] in MERGE_WINDOW_STATES
    write_execution(repo, task_id="dash-19", base=head_of(repo))
    assert merge_window(repo)["state"] in MERGE_WINDOW_STATES
    assert merge_window(tmp_path / "no-such-repo")["state"] in MERGE_WINDOW_STATES


# ---- the failure paths: unknown, never a silent "open" -----------------------


def test_a_predicate_that_raises_degrades_to_unknown_and_never_to_open(
    tmp_path, monkeypatch
):
    """THE fail-open case. An empty reason list means "nothing is holding the
    window"; a check that could not run has no reason list at all, and the two
    must never render alike."""
    repo = make_repo(tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("git had a bad day")

    monkeypatch.setattr(cli, "_merge_window_blockers", boom)

    window = merge_window(repo)

    assert window["state"] == "unknown"
    assert window["state"] != "open"
    assert window["reasons"] == [] and window["notes"] == []
    assert "RuntimeError" in window["detail"] and "bad day" in window["detail"]


def test_a_remote_that_will_not_answer_keeps_the_window_shut(tmp_path):
    """Publication is an EXEMPTION, so a remote failure must not be allowed to
    write a record off. The predicate keeps it shut by its own fail-closed rule
    and reports WHY; this pins that the panel passes that through rather than
    converting an unanswerable remote into "open"."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_execution(repo, task_id="dash-19", base=head_of(repo),
                    remote="origin", dest_ref="autoloop/task/dash-19")
    git = _FakeCheckout(remote_error=GitCommandError("ls-remote", "host is down"))

    window = merge_window(repo, git=git)

    assert window["state"] == "shut"
    assert "could not verify origin/" in window["reasons"][0]


def test_a_missing_config_is_unknown_and_says_so(tmp_path):
    """The dashboard renders against any checkout, including one that has never
    been configured. That is a reason to say the window is unknown and not a
    reason to stop serving the page."""
    repo = make_repo(tmp_path)
    (repo / ".autoloop" / "config.toml").unlink()

    window = merge_window(repo)

    assert window["state"] == "unknown"
    assert window["detail"], "an unknown with no detail is an unexplained blank"
    assert "config" in window["detail"]


def test_an_unparseable_task_graph_is_unknown_rather_than_open(tmp_path):
    """`_merge_window_blockers` loads the registry to decide which records are
    exempt. A graph it cannot read is a question it cannot answer."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    (repo / ".autoloop" / "tasks.json").write_text("{not json", encoding="utf-8")

    window = merge_window(repo)

    assert window["state"] == "unknown"
    assert window["detail"]


def test_a_state_dir_that_does_not_exist_is_reported_and_never_read_as_open(
    tmp_path,
):
    """A directory that cannot be read is not evidence that nothing is in
    flight. The predicate's own first branch says so; this pins that the panel
    surfaces it rather than showing an empty, reassuring list."""
    repo = make_repo(tmp_path, state_dir="does-not-exist")

    window = merge_window(repo)

    assert window["state"] == "shut"
    assert "does not exist" in window["reasons"][0]
    assert "nothing can be called safe" in window["reasons"][0]


def test_the_git_subprocesses_this_panel_makes_are_bounded(monkeypatch):
    """`GitGateway` passes no timeout to `subprocess.run`, which is right for a
    loop running one command at a time and wrong for a page that sweeps every
    2s and funnels every tab through one sweep (`collect_shared`). An
    unreachable origin with no bound stops the whole page answering, for every
    viewer at once."""
    recorded = {}

    def fake_run(args, **kwargs):
        recorded["args"] = args
        recorded["kwargs"] = kwargs
        raise AssertionError("not reached — the bound is what is under test")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(AssertionError):
        dashboard._bounded_git(["git", "rev-parse", "HEAD"], cwd=".")

    assert recorded["kwargs"]["timeout"] == dashboard.MERGE_WINDOW_GIT_TIMEOUT
    assert dashboard.MERGE_WINDOW_GIT_TIMEOUT == 15, (
        "the same bound the page's own ls-remote carries"
    )


def test_the_gateway_the_panel_builds_carries_that_bound(tmp_path, monkeypatch):
    """The constant is worth nothing if the gateway is built without it. Reaches
    for the private `_runner` deliberately: it is the only observable that
    distinguishes a bounded gateway from an unbounded one without running git."""
    repo = make_repo(tmp_path)
    seen = []
    monkeypatch.setattr(
        cli, "_merge_window_blockers",
        lambda config, s, git: (seen.append(git), ([], []))[1],
    )

    merge_window(repo)

    assert seen[0]._runner is dashboard._bounded_git


def test_a_timeout_is_reported_as_unknown_rather_than_as_an_open_window(
    tmp_path, monkeypatch
):
    """`subprocess.TimeoutExpired` is neither `GitError` nor `OSError`, so it
    passes THROUGH the predicate's fail-closed handlers to this panel's guard.
    That is the intended route, and it must land on unknown."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_execution(repo, task_id="dash-19", base=head_of(repo))

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["git"], dashboard.MERGE_WINDOW_GIT_TIMEOUT)

    monkeypatch.setattr(dashboard, "_bounded_git", timeout)

    window = merge_window(repo)

    assert window["state"] == "unknown"
    assert "TimeoutExpired" in window["detail"]


# ---- read-only, lock-free, and on the payload --------------------------------


def test_the_window_reaches_the_api_payload_beside_the_timestamp(tmp_path):
    """`collect` is what `/api/state` serves. The window has to be understood as
    of `served_at`, so both have to be in the same payload."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_execution(repo, task_id="dash-19", base=head_of(repo))

    payload = collect(repo)

    assert payload["merge_window"]["state"] == "shut"
    assert any("dash-19" in r for r in payload["merge_window"]["reasons"])
    assert payload["served_at"], "the window is only readable as of a time"


def test_reading_the_window_writes_nothing_to_the_observed_checkout(tmp_path):
    """The load-bearing property of this whole page: the loop's escape detector
    refuses a write-capable task if the primary checkout is dirty, so a tracker
    that touched the working tree would park the thing it observes."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_registry(repo, Task(id="prov-01", title="t", description="d"))
    write_execution(repo, task_id="prov-01", candidate="0" * 40,
                    worktree_path=str(tmp_path / "gone" / "prov-01"))
    write_execution(repo, task_id="dash-19", base=head_of(repo))
    # AFTER the git calls above, which rewrite .git/index: snapshot any earlier
    # and this measures its own side effect and blames the panel.
    status_before = run_git(repo, "status", "--porcelain")
    before = {p: p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}

    window = merge_window(repo)

    assert window["reasons"] and window["notes"], "it really did do the work"
    after = {p: p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}
    assert after == before, "the window check must not create, remove or touch a file"
    assert run_git(repo, "status", "--porcelain") == status_before


def test_the_window_answers_while_the_loop_holds_its_run_lock(tmp_path):
    """Lock-free, and stated as the thing that would break if it were not: the
    `LoopLock` is held for a whole run, so a panel that took it would answer
    only while the loop was stopped — which is when nobody needs it. A panel
    that took it and swallowed the refusal would render `unknown` forever, which
    is why the assertion is on a real answer rather than on "did not raise"."""
    repo = make_repo(tmp_path)
    write_state(repo, phase=Phase.AWAITING.value)
    write_execution(repo, task_id="dash-19", base=head_of(repo))
    lock_path = repo / ".autoloop" / "LOCK"
    lock_path.write_text(json.dumps({
        "pid": os.getpid(), "hostname": socket.gethostname(),
        "started_at": utcnow_iso(), "run_id": "held-by-the-loop",
        "state_dir": str(repo / ".autoloop"),
    }), encoding="utf-8")
    held = lock_path.read_bytes()

    window = merge_window(repo)

    assert window["state"] == "shut", "a lock-free read answers while the loop runs"
    assert any("dash-19" in r for r in window["reasons"])
    assert lock_path.read_bytes() == held, "the lock was neither taken nor rewritten"


# ---- the page ----------------------------------------------------------------


def merge_window_js() -> str:
    """The panel's own code, lifted verbatim out of the served page.

    `esc` comes along because every helper in the region depends on it; the
    region reaches nothing else on the page — `MW` and `MWSIG` are its own
    declarations — which is what lets it run against a stub document.
    """
    script = PAGE.split("<script>", 1)[1]
    esc_line = next(line for line in script.splitlines()
                    if line.startswith("const esc ="))
    region = script.split("// MERGE_WINDOW_START", 1)[1]
    region = region.split("// MERGE_WINDOW_END", 1)[0]
    return "\n".join((esc_line, region))


def render_window(payloads) -> list[dict]:
    """Run `renderMergeWindow` once per payload against a stub document and
    return what each render left in the DOM."""
    harness = merge_window_js() + """
const NODES = {};
for (const id of ["mwstate", "mwreasons", "mwnotes"])
  NODES[id] = {innerHTML: ""};
const document = {getElementById: id => NODES[id]};
const OUT = [];
for (const payload of __PAYLOADS__) {
  renderMergeWindow(payload);
  OUT.push({state: NODES.mwstate.innerHTML, reasons: NODES.mwreasons.innerHTML,
            notes: NODES.mwnotes.innerHTML});
}
console.log(JSON.stringify(OUT));
""".replace("__PAYLOADS__", json.dumps(payloads))
    return json.loads(run_js(harness))


def one(payload) -> dict:
    return render_window([payload])[0]


def test_an_open_window_renders_as_open_with_no_reasons():
    out = one({"served_at": "11:02:41",
               "merge_window": {"state": "open", "reasons": [], "notes": [],
                                "detail": ""}})

    assert "OPEN" in out["state"] and "checked 11:02:41" in out["state"]
    assert "nothing — the loop may merge" in out["reasons"]
    assert "<li>" not in out["reasons"], "an open window lists no reasons"
    assert "none" in out["notes"]


def test_a_closed_window_renders_every_reason():
    reasons = [
        "task dash-19 has a candidate (4468003bc00f) bound to base b28dc15 — "
        "never pushed; that base IS the current head b28dc15abcde, so merging "
        "would strand it",
        "a phase is executing — an agent may be mid-write",
    ]
    out = one({"served_at": "11:02:41",
               "merge_window": {"state": "shut", "reasons": reasons, "notes": [],
                                "detail": ""}})

    assert "SHUT" in out["state"]
    for reason in reasons:
        assert f"<li>{reason}</li>" in out["reasons"], f"{reason[:40]}… was dropped"
    assert '<span class="gc">2</span>' in out["reasons"], "the count is in the heading"
    # The transient one is rendered like any other, with no imperative attached
    # to it — the framing lives in the panel's static prose, and the payload is
    # never filtered.
    assert "a phase is executing" in out["reasons"]


def test_notes_render_distinctly_from_reasons_in_both_directions():
    """The distinction is the point: one closes the window, the other says a
    record is wrong. Asserted in BOTH directions, because a one-way check passes
    for a page that renders the notes twice."""
    out = one({"served_at": "11:02:41", "merge_window": {
        "state": "shut",
        "reasons": ["task dash-19 has a candidate bound to base — would strand it"],
        "notes": ["task prov-01: candidate 6ab4a529a4e2 is NOT in flight"],
        "detail": "",
    }})

    assert "dash-19" in out["reasons"] and "dash-19" not in out["notes"]
    assert "prov-01" in out["notes"] and "prov-01" not in out["reasons"]
    # Different containers AND different words, so the two are still distinct in
    # a screenshot, to a screen reader, and to a reader who does not see colour.
    assert "Holding the window shut" in out["reasons"]
    assert "Notes — wrong, but holding nothing" in out["notes"]
    assert "▲" in out["reasons"] and "ℹ" in out["notes"]


def test_notes_are_rendered_even_when_the_window_is_open():
    """They are the half nothing else surfaces, and the window being open is
    exactly when an operator has no other reason to look."""
    out = one({"served_at": "11:02:41", "merge_window": {
        "state": "open", "reasons": [],
        "notes": ["task audit-0002: … its record does not record that publication"],
        "detail": "",
    }})

    assert "OPEN" in out["state"]
    assert "audit-0002" in out["notes"]


@pytest.mark.parametrize("window", [
    {"state": "unknown", "reasons": [], "notes": [], "detail": "GitError: host is down"},
    {"state": "sideways", "reasons": [], "notes": [], "detail": ""},
    {},
])
def test_an_unanswered_check_never_renders_as_an_open_window(window):
    """The fail-open shape, in the three ways it can arrive: a stated unknown,
    a state this page cannot interpret, and no window in the payload at all.
    Each has an empty reason list, and an empty reason list must never be drawn
    as "nothing is holding it"."""
    out = one({"served_at": "11:02:41", "merge_window": window})

    assert "UNKNOWN" in out["state"]
    assert "OPEN" not in out["state"]
    assert "NOT open" in out["state"]
    assert "nothing — the loop may merge" not in out["reasons"]
    assert "not computed" in out["reasons"]
    # And the counts say unknown rather than 0: "holding the window shut 0" is
    # the same false claim written in another place.
    assert '<span class="gc">0</span>' not in out["reasons"]
    assert '<span class="gc">unknown</span>' in out["reasons"]
    assert '<span class="gc">unknown</span>' in out["notes"]


def test_a_shut_window_with_no_reasons_is_never_drawn_as_an_open_one():
    """The one fail-open route the state check does not cover. `merge_window`
    cannot produce this pairing — there the state IS the reasons — so if it
    arrives, the check is faulty, and printing "nothing — the loop may merge"
    under a SHUT headline would be the false sentence this panel exists to
    stop an operator reading."""
    out = one({"served_at": "11:02:41", "merge_window": {
        "state": "shut", "reasons": [], "notes": [], "detail": ""}})

    assert "SHUT" in out["state"]
    assert "nothing — the loop may merge" not in out["reasons"]
    assert "a fault in the check, not an open window" in out["reasons"]


def test_a_field_that_is_not_a_list_cannot_take_the_whole_render_down():
    """`reasons` arriving as a string has a `length` and no `map`, so a truthy
    test passes it through and the render throws — one panel's malformed payload
    taking every other panel on the page with it. `run_js` fails the test if the
    harness throws at all, so reaching the assertions IS the claim."""
    out = one({"served_at": "11:02:41", "merge_window": {
        "state": "shut", "reasons": "task dash-19 holds it", "notes": None,
        "detail": ""}})

    assert "SHUT" in out["state"]
    assert "<li>" not in out["reasons"], "a string is not a one-item list"
    assert "none" in out["notes"]


def test_the_unknown_detail_reaches_the_page():
    out = one({"served_at": "11:02:41", "merge_window": {
        "state": "unknown", "reasons": [], "notes": [],
        "detail": "ConfigError: config file not found",
    }})

    assert "ConfigError: config file not found" in out["state"]


def test_the_stamp_moves_on_every_poll_even_when_the_window_does_not_change():
    """The whole difference between this panel and the log line it replaces. A
    window whose reasons have not changed in an hour must still say it was
    checked two seconds ago — a stamp that only moved when the answer did would
    BE the stale snapshot."""
    window = {"state": "shut", "reasons": ["a phase is executing"], "notes": [],
              "detail": ""}
    first, second = render_window([
        {"served_at": "11:02:41", "merge_window": window},
        {"served_at": "11:02:43", "merge_window": window},
    ])

    assert "checked 11:02:41" in first["state"]
    assert "checked 11:02:43" in second["state"], "the stamp froze"
    # …while the lists themselves are not rebuilt under an operator's selection.
    assert first["reasons"] == second["reasons"]


def test_a_changed_window_still_redraws_the_lists():
    """The other half of that guard: a signature that never invalidated would
    freeze the reasons on screen, which is the same stale-snapshot failure with
    a moving clock on top."""
    first, second = render_window([
        {"served_at": "11:02:41", "merge_window": {
            "state": "shut", "reasons": ["task a holds it"], "notes": [],
            "detail": ""}},
        {"served_at": "11:02:43", "merge_window": {
            "state": "shut", "reasons": ["task b holds it"], "notes": [],
            "detail": ""}},
    ])

    assert "task a holds it" in first["reasons"]
    assert "task b holds it" in second["reasons"]
    assert "task a holds it" not in second["reasons"]


def test_a_hostile_reason_renders_as_text_rather_than_as_markup():
    """Reasons interpolate task ids read off `tasks.json` and git's stderr.
    Anyone who can write that file can write `<script>`, and this is the one
    field long enough that nobody would notice it had been read as markup."""
    out = one({"served_at": "11:02:41", "merge_window": {
        "state": "shut",
        "reasons": ["task <script>alert(1)</script> holds it"],
        "notes": ["note <img src=x onerror=alert(2)>"],
        "detail": "<b>boom</b>",
    }})

    for rendered in (out["state"], out["reasons"], out["notes"]):
        assert "<script>" not in rendered
        assert "<img" not in rendered
    assert "&lt;script&gt;" in out["reasons"]
    assert "&lt;img" in out["notes"]

    # The `detail` too, which is the ONLY untrusted text the headline itself
    # interpolates and is reachable only through the unknown branch — an
    # exception's own message, carrying a config path or git's stderr.
    unknown = one({"served_at": "11:02:41", "merge_window": {
        "state": "unknown", "reasons": [], "notes": [],
        "detail": "GitError: <script>alert(3)</script>",
    }})
    assert "<script>" not in unknown["state"]
    assert "&lt;script&gt;" in unknown["state"]


# ---- the wiring: a panel nothing calls renders nothing ------------------------


def test_the_panel_is_wired_into_the_page_above_the_change_guard():
    """A render function nothing calls is a feature that ships green. And it has
    to be called ABOVE the page-wide guard, or the stamp only moves when the
    reasons do — which is the stale log line, re-implemented in the browser."""
    script = PAGE.split("<script>", 1)[1]
    body = script.split("function render(d, force){", 1)[1]
    call = body.index("renderMergeWindow(d)")
    guard = body.index("if (!force && sig === LASTJSON) return;")
    assert call < guard, "below the guard the stamp freezes with the payload"
    # And nothing else may return before it. The node harness calls
    # `renderMergeWindow` directly, so it can only ever see the function's own
    # behaviour — an early exit ABOVE this call would freeze the stamp on the
    # real page with every assertion in this file still green. The one return
    # that is allowed is the null-payload guard, which renders nothing at all
    # rather than rendering something stale.
    # Comment lines dropped first: this panel's own block above the call
    # explains what a return there would cost, and counting the word in prose
    # would make the assertion depend on how that comment is phrased.
    before = "\n".join(line for line in body[:call].splitlines()
                       if not line.strip().startswith("//"))
    assert before.count("return") == 1, (
        "a new early return above renderMergeWindow(d) would freeze the stamp "
        f"on the real page with every test in this file still green: {before}"
    )
    assert "if (!d) return;" in before, "the one allowed return is the null guard"


def test_the_page_ships_the_three_containers_the_panel_writes():
    static_markup = PAGE.split("<script>", 1)[0]
    for element in ('id="mwstate"', 'id="mwreasons"', 'id="mwnotes"'):
        assert element in static_markup, f"{element} is not on the page"
    # Reasons above notes: what holds the window is the question, and a note is
    # context for it.
    assert static_markup.index('id="mwreasons"') < static_markup.index('id="mwnotes"')
    # The panel sits under the one that reports the outcome it explains.
    assert static_markup.index('id="mergehead"') < static_markup.index('id="mwstate"')


def test_the_page_says_what_a_reason_and_a_note_each_mean():
    """The two words carry the whole distinction, so the page defines them
    rather than leaving an operator to infer it from two lists.

    Whitespace is collapsed first. The prose is hard-wrapped in the source, so a
    phrase that happens to straddle a line break is absent as a literal while
    being present on the page — which would make this test a check on where the
    editor wrapped rather than on what the panel says."""
    static_markup = PAGE.split("<script>", 1)[0]
    panel = " ".join(static_markup.split('id="mwstate"', 1)[1].split())
    assert "A <b>reason</b> holds the window shut" in panel
    assert "A <b>note</b> holds nothing" in panel
    # A transient reason must not be rendered as an accusation.
    assert "clears by itself" in panel
    # And unknown must be defined on the page, not only in the render.
    assert "never the same as open" in panel


def test_every_window_state_has_an_icon_and_a_word_on_the_page():
    """Colour is never the only channel, and a state missing from `MW` renders
    an empty panel — so the backend's list and the page's are pinned equal."""
    region = merge_window_js()
    for state in MERGE_WINDOW_STATES:
        assert f"{state}:[" in region.replace(" ", ""), f"{state} cannot be drawn"
    for word in ("OPEN", "SHUT", "UNKNOWN"):
        assert word in region
