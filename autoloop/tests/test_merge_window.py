"""Two dead ends that came from the operator and the loop sharing one branch.

`merge-window`: every merge into the branch the loop builds against, while a
task holds a candidate, strands that task — the loop refuses to rebase
(correctly: a reviewer has already seen the candidate) and parks. It happened
four times on 2026-08-02, each time because "no agent is running right now"
was mistaken for "safe to merge".

Stale completed-task park: resolving a park BY COMPLETING its task then left a
session that could only be archived, because `block` refuses a completed task
and the fail-closed branch escalated that refusal to loop_fatal.
"""

import argparse
import json

import pytest

from autoloop import cli
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.errors import GitCommandError
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore

URL = "https://chatgpt.com/c/merge-window"


@pytest.fixture
def config(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=checkout / ".autoloop",
        workers_root=tmp_path / "workers",
    )


@pytest.fixture
def wired(config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def _state(config, phase=Phase.AWAITING.value, **kw):
    StateStore(config.state_file).save(
        LoopState(session_id="mw", conversation_url=URL, phase=phase, **kw)
    )


def _execution(
    config,
    task_id="t-1",
    candidate="abc123def456",
    base="000111222333",
    remote="",
    dest_ref="",
    worktree_path="",
):
    d = config.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.json").write_text(
        json.dumps({
            "task_id": task_id,
            "candidate_sha": candidate,
            "task_base_sha": base,
            "intended_remote": remote,
            "intended_remote_ref": dest_ref,
            "worktree_path": worktree_path,
        }),
        encoding="utf-8",
    )


class _FakeRemote:
    """Stands in for `GitGateway.remote_ref_sha`. Records every lookup, so a
    test can assert the remote was NOT consulted — half of what the
    fail-closed cases claim is that they never got as far as the network."""

    def __init__(self, refs=None, error=None):
        self.refs = refs or {}
        self.error = error
        self.lookups = []

    def remote_ref_sha(self, remote, dest_ref):
        self.lookups.append((remote, dest_ref))
        if self.error is not None:
            raise self.error
        return self.refs.get((remote, dest_ref), "")


@pytest.fixture
def remote(monkeypatch):
    """Default: a remote that knows nothing. Every existing test writes
    records with no push intent, so none of them reach it."""
    fake = _FakeRemote()
    monkeypatch.setattr(cli, "_window_git", lambda _config: fake)
    return fake


def _args(**kw):
    return argparse.Namespace(
        config=None, wait=kw.get("wait", False),
        timeout=kw.get("timeout", 0.1), poll=kw.get("poll", 0.01),
    )


# --- merge-window -------------------------------------------------------------


def test_an_in_flight_candidate_closes_the_window(wired, capsys):
    """THE case a phase check misses. No agent is running and the phase is
    quiet, but a candidate is bound to an older base — merging strands it."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired)

    assert cli._cmd_merge_window(_args()) == 1

    out = capsys.readouterr().out
    assert "CLOSED" in out
    assert "would strand it" in out
    assert "abc123def456"[:12] in out


def test_a_quiet_loop_with_no_candidate_is_safe(wired, capsys):
    _state(wired, phase=Phase.AWAITING.value)

    assert cli._cmd_merge_window(_args()) == 0
    assert "OPEN" in capsys.readouterr().out


def test_an_executing_phase_closes_the_window(wired, capsys):
    _state(wired, phase=Phase.EXECUTING.value)

    assert cli._cmd_merge_window(_args()) == 1
    assert "executing" in capsys.readouterr().out


def test_an_execution_record_without_a_candidate_does_not_close_it(wired):
    """A dispatched task that has not committed yet holds no reviewed work,
    so there is nothing a moved head could discard."""
    _state(wired, phase=Phase.AWAITING.value)
    d = wired.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t-1.json").write_text(json.dumps({"task_id": "t-1"}), encoding="utf-8")

    assert cli._cmd_merge_window(_args()) == 0


def test_a_record_for_finished_work_does_not_close_the_window(wired):
    """Records outlive the work they describe — nothing archives one when a
    candidate is published or its task is quarantined. Counting those would
    close the window permanently on work that can no longer be stranded.
    Found by running this command against the real repo the moment it was
    written: it reported a completed task and a quarantined one."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="done-1")
    _execution(wired, task_id="quarantined-1")

    store = TaskStore(wired.tasks_file)
    registry = TaskRegistry([
        Task(id="done-1", title="t", description="d"),
        Task(id="quarantined-1", title="t", description="d"),
    ])
    registry.mark_completed("done-1")
    registry.block("quarantined-1", "failed its own validation")
    store.save(registry)

    assert cli._cmd_merge_window(_args()) == 0


def test_a_record_for_a_LIVE_task_still_closes_it(wired, capsys):
    """The guard must not swallow the case the command exists for."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="live-1")
    TaskStore(wired.tasks_file).save(
        TaskRegistry([Task(id="live-1", title="t", description="d")])
    )

    assert cli._cmd_merge_window(_args()) == 1
    assert "would strand it" in capsys.readouterr().out


def test_a_record_whose_task_is_unknown_still_closes_it(wired):
    """An id the registry has never heard of is not evidence of safety."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="ghost-1")

    assert cli._cmd_merge_window(_args()) == 1


def test_no_session_at_all_is_safe(wired):
    assert cli._cmd_merge_window(_args()) == 0


def test_a_state_dir_that_is_not_THERE_is_not_evidence_of_safety(config, monkeypatch, capsys):
    """`state_dir` is relative in the shipped config (`.autoloop`), so it
    resolves against the caller's cwd. Run from a sibling worktree or a cron
    wrapper with its own working directory and every glob comes back empty —
    which used to print OPEN. Reading the wrong directory is not the same as
    finding nothing there. Hit for real while dry-running this change."""
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    assert not config.state_dir.exists()

    assert cli._cmd_merge_window(_args()) == 1
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "nothing can be called safe" in out


def test_an_unreadable_execution_record_is_skipped_not_fatal(wired):
    """A torn write must not make the tool unusable — it reports on the
    records it can read."""
    _state(wired, phase=Phase.AWAITING.value)
    d = wired.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t-1.json").write_text("{not json", encoding="utf-8")

    assert cli._cmd_merge_window(_args()) == 0


def test_wait_gives_up_and_reports_rather_than_hanging(wired, capsys):
    _state(wired, phase=Phase.EXECUTING.value)

    assert cli._cmd_merge_window(_args(wait=True, timeout=0.05)) == 1
    assert "gave up" in capsys.readouterr().out


# --- a PUBLISHED candidate cannot be stranded ---------------------------------
#
# The exemption that makes this command usable at all. Nothing in the loop ever
# calls `TaskRegistry.mark_completed` (verified 2026-08-04: only tests call it,
# and `Decision` has no terminal member a reviewer could use to say "done"), so
# a task that publishes its candidate stays `in_progress` forever. Gating only
# on the registry's terminal states therefore closed the window PERMANENTLY:
# on 2026-08-04 four tasks held candidates, three of them already pushed to
# their own side branches on origin, and no amount of waiting could open it.

PUSHED = "refs/heads/autoloop/rt-9"


def test_a_published_candidate_does_not_close_the_window(wired, remote, capsys):
    """Its reviewed object is durable on the remote and the operator merges
    that side branch as an ordinary branch, so moving the base cannot discard
    it."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    TaskStore(wired.tasks_file).save(
        TaskRegistry([Task(id="rt-9", title="t", description="d")])
    )
    remote.refs[("origin", PUSHED)] = "abc123def456"

    assert cli._cmd_merge_window(_args()) == 0
    out = capsys.readouterr().out
    assert "OPEN" in out
    assert remote.lookups == [("origin", PUSHED)], "must confirm against the remote"


def test_the_exemption_reports_the_residual_rather_than_hiding_it(wired, remote, capsys):
    """A published record is still re-dispatchable, and a `revise` naming it
    after the base moves parks on `task_base_behind_head`. Recoverable, but a
    real consequence of merging — so the operator is told."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    remote.refs[("origin", PUSHED)] = "abc123def456"

    assert cli._cmd_merge_window(_args()) == 0
    out = capsys.readouterr().out
    assert "note:" in out
    assert "rt-9" in out and "would park it" in out


def test_a_record_that_KNOWS_it_published_reports_no_park(wired, remote, capsys):
    """The residual is only a park while the RECORD does not know what the
    remote just said. Since 2026-08-15 publication writes a confirmed
    `published_sha`, and a later revise reconciles from the remote instead of
    refusing to re-base — so reporting a park there would send the operator
    after a problem that no longer exists."""
    _state(wired, phase=Phase.AWAITING.value)
    d = wired.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rt-9.json").write_text(
        json.dumps({
            "task_id": "rt-9",
            "candidate_sha": "abc123def456",
            "task_base_sha": "000111222333",
            "intended_remote": "origin",
            "intended_remote_ref": PUSHED,
            "published_sha": "abc123def456",
        }),
        encoding="utf-8",
    )
    remote.refs[("origin", PUSHED)] = "abc123def456"

    assert cli._cmd_merge_window(_args()) == 0
    out = capsys.readouterr().out
    assert "note:" in out and "rt-9" in out
    assert "reconciles it against the remote" in out
    assert "would park it" not in out


def test_push_INTENT_alone_is_not_publication(wired, remote, capsys):
    """The whole reason this check goes to the network. The orchestrator writes
    `intended_remote_ref` BEFORE the push so a crash is recoverable, so a
    REFUSED push leaves a record indistinguishable from a landed one on disk."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    # remote.refs deliberately empty: the intent was recorded, the push failed.

    assert cli._cmd_merge_window(_args()) == 1
    out = capsys.readouterr().out
    assert "does not exist" in out
    assert "would strand it" in out


def test_a_remote_ref_at_a_DIFFERENT_sha_is_not_publication(wired, remote, capsys):
    """The branch exists but carries someone else's commit — an earlier round's
    candidate, or a force-push. Not this candidate, so not safe."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    remote.refs[("origin", PUSHED)] = "999888777666"

    assert cli._cmd_merge_window(_args()) == 1
    assert "not the candidate" in capsys.readouterr().out


def test_an_unverifiable_remote_keeps_the_window_shut(wired, monkeypatch, capsys):
    """Offline, or a remote that refuses. Fail-closed: an unanswerable question
    is never answered 'safe'."""
    fake = _FakeRemote(error=GitCommandError("ls-remote", "network is unreachable"))
    monkeypatch.setattr(cli, "_window_git", lambda _config: fake)
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)

    assert cli._cmd_merge_window(_args()) == 1
    assert "could not verify" in capsys.readouterr().out


def test_a_record_with_no_push_intent_never_touches_the_network(wired, remote, capsys):
    """rt-02 on 2026-08-04: a candidate that was never pushed at all. It closes
    the window, and it must do so without an ls-remote there is no ref for."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-2")

    assert cli._cmd_merge_window(_args()) == 1
    out = capsys.readouterr().out
    assert "never pushed" in out
    assert remote.lookups == []


def test_wait_asks_the_remote_about_a_published_candidate_ONCE(wired, remote, capsys):
    """`--poll` defaults to 15s, so a wait held open by something else would
    otherwise re-ask the remote about every published candidate forever —
    hundreds of round-trips an hour for an answer that cannot change. Worse
    than wasteful: throttle the remote and the fail-closed branch turns every
    lookup into 'could not verify', so the wait talks itself into never
    opening."""
    _state(wired, phase=Phase.EXECUTING.value)      # keeps the loop polling
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    remote.refs[("origin", PUSHED)] = "abc123def456"

    assert cli._cmd_merge_window(_args(wait=True, timeout=0.05, poll=0.005)) == 1

    assert len(remote.lookups) == 1, (
        f"one confirmation should serve the whole invocation, got {remote.lookups}"
    )


def test_an_UNPUBLISHED_candidate_is_re_checked_on_every_poll(wired, remote):
    """The other half: becoming published is precisely the event `--wait`
    exists to notice, so a negative must never be cached."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    # The ref does not exist yet — every poll must go and look again.

    assert cli._cmd_merge_window(_args(wait=True, timeout=0.05, poll=0.005)) == 1
    assert len(remote.lookups) > 1, "a negative must be re-checked, not memoized"


def test_an_executing_phase_still_closes_it_even_with_everything_published(
    wired, remote, capsys
):
    """The two blockers are independent — publication says nothing about
    whether an agent is mid-write in the checkout right now."""
    _state(wired, phase=Phase.EXECUTING.value)
    _execution(wired, task_id="rt-9", remote="origin", dest_ref=PUSHED)
    remote.refs[("origin", PUSHED)] = "abc123def456"

    assert cli._cmd_merge_window(_args()) == 1
    assert "executing" in capsys.readouterr().out


# --- a record that is a DEFECT, not a hazard ----------------------------------
#
# What `release` used to leave behind. It returned the task to pending and
# quarantined the worker repo, and left the execution record in place with
# `candidate_sha` still set — a record claiming live unpublished work for a task
# that would be redone from scratch. The commit it names exists only inside the
# quarantined worker, unreachable from the checkout.
#
# On 2026-08-15, 14 such records (auto-01, auto-03, inbox-05, loop-01, …), all
# bound to the pre-merge HEAD, held the window shut. It could not reopen by
# itself: every one of those tasks would have had to be re-dispatched AND
# re-published first. `release` retires the record now; this is the belt and
# braces for records that predate that fix or drifted some other way — reported
# as a NOTE, because a record that should have been retired is worth seeing.


class _FakeCheckout:
    """A checkout that can answer questions, and knows a fixed set of commits."""

    def __init__(self, head="head1234", commits=()):
        self._head = head
        self.commits = set(commits)
        self.lookups = []

    def head_sha(self):
        if not self._head:
            raise GitCommandError("rev-parse", "not a git repository")
        return self._head

    def read_commit(self, oid):
        if oid not in self.commits:
            raise GitCommandError("cat-file", f"{oid}: bad file")
        return {"tree": "t", "parents": [], "message": ""}

    def remote_ref_sha(self, remote, dest_ref):
        self.lookups.append((remote, dest_ref))
        return ""


def _released(config, task_id="auto-01", worker=None):
    """A record `release` left behind: the task is back in the queue and the
    worker repo it names is gone."""
    _execution(config, task_id=task_id, worktree_path=str(worker or "/gone/workers/" + task_id))
    TaskStore(config.tasks_file).save(
        TaskRegistry([Task(id=task_id, title="t", description="d")])
    )


def test_a_record_left_behind_by_release_does_not_close_the_window(wired, capsys):
    """The 2026-08-15 case. Pending task + vanished worker + a candidate the
    checkout cannot resolve is provably not in-flight work."""
    _state(wired, phase=Phase.AWAITING.value)
    _released(wired)
    git = _FakeCheckout()

    reasons, notes = cli._merge_window_blockers(wired, set(), git)

    assert reasons == [], f"nothing is in flight to strand: {reasons}"
    assert any("auto-01" in n and "NOT in flight" in n for n in notes), notes
    assert any("should have been retired" in n for n in notes), (
        "a defect must be visible, not silently ignored"
    )


def test_a_vanished_worker_is_not_enough_while_the_commit_is_REACHABLE(wired):
    """Two of the three conditions is not the answer. If the checkout can
    resolve the candidate, a moved base can still strand it — the worker repo
    being gone says nothing about that."""
    _state(wired, phase=Phase.AWAITING.value)
    _released(wired)
    git = _FakeCheckout(commits={"abc123def456"})

    reasons, _notes = cli._merge_window_blockers(wired, set(), git)

    assert reasons and "would strand it" in reasons[0]


def test_a_record_with_NO_recorded_worker_path_still_closes_the_window(wired):
    """'We never recorded where it was' is not 'we know it is gone'. An empty
    `worktree_path` is absence of evidence, and the fail-closed reading of it
    keeps the window shut."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="auto-01", worktree_path="")
    TaskStore(wired.tasks_file).save(
        TaskRegistry([Task(id="auto-01", title="t", description="d")])
    )
    git = _FakeCheckout()

    reasons, _notes = cli._merge_window_blockers(wired, set(), git)

    assert reasons and "would strand it" in reasons[0]


def test_an_IN_PROGRESS_task_is_never_written_off(wired):
    """A dispatched round is exactly the work this command protects. Its worker
    repo can be missing for reasons that are not 'it was retired' — a crash
    mid-`create`, a half-finished quarantine — and writing it off would strand
    the thing the gate exists for."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired, task_id="auto-01", worktree_path="/gone/workers/auto-01")
    store = TaskStore(wired.tasks_file)
    registry = TaskRegistry([Task(id="auto-01", title="t", description="d")])
    registry.mark_in_progress("auto-01")
    store.save(registry)

    reasons, _notes = cli._merge_window_blockers(wired, set(), _FakeCheckout())

    assert reasons and "would strand it" in reasons[0]


def test_a_checkout_that_cannot_answer_keeps_the_window_shut(wired):
    """Fail-closed, exactly like `_candidate_publication`: git being unable to
    answer is not git answering 'no such object'."""
    _state(wired, phase=Phase.AWAITING.value)
    _released(wired)

    reasons, _notes = cli._merge_window_blockers(wired, set(), _FakeCheckout(head=""))

    assert reasons and "would strand it" in reasons[0]


def test_writing_a_record_off_never_touches_the_network(wired):
    """It is decided entirely from the registry, the filesystem and local git
    — the record has no push intent to ask the remote about anyway."""
    _state(wired, phase=Phase.AWAITING.value)
    _released(wired)
    git = _FakeCheckout()

    cli._merge_window_blockers(wired, set(), git)

    assert git.lookups == []


# --- a stale park whose task has since completed ------------------------------


def _parked(config, task_id="t-1"):
    store = StateStore(config.state_file)
    store.save(
        LoopState(
            session_id="mw", conversation_url=URL, phase=Phase.NEEDS_USER.value,
            park_kind="task_fatal", park_task_id=task_id,
            question=f"task {task_id}: its recorded base is behind the branch head",
        )
    )
    return store


def test_a_park_whose_task_is_now_completed_does_not_stop_the_loop(config, capsys):
    """Resolving a park by publishing its candidate and completing the task
    used to produce a session that could only be archived: `block` refuses a
    completed task, and the fail-closed branch escalated that to loop_fatal."""
    store = _parked(config)
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry([Task(id="t-1", title="t", description="d")])
    registry.mark_completed("t-1")
    task_store.save(registry)

    outcome = cli._handle_parked_task(
        config, store, task_store, registry, store.load()
    )

    assert outcome == "task_fatal", "continuous mode must carry on"
    out = capsys.readouterr().out
    assert "already completed" in out
    assert "stale" in out
    # The session is cleared, so the next pass starts fresh rather than
    # re-reading the same park.
    assert not config.state_file.exists()
    # And completion is untouched — nothing tried to un-complete it.
    assert task_store.load().state_of("t-1") is TaskState.COMPLETED


def test_a_park_whose_task_is_unknown_still_escalates(config, capsys):
    """The fail-closed branch must survive: only the completed case is
    reinterpreted, because only that one is provably not a fault."""
    store = _parked(config, task_id="ghost-1")
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry([Task(id="t-1", title="t", description="d")])
    task_store.save(registry)

    outcome = cli._handle_parked_task(
        config, store, task_store, registry, store.load()
    )

    assert outcome != "task_fatal"
    assert "loop_fatal" in capsys.readouterr().out


def test_an_ordinary_task_fatal_park_still_quarantines(config, capsys):
    store = _parked(config)
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry([Task(id="t-1", title="t", description="d")])
    task_store.save(registry)

    outcome = cli._handle_parked_task(
        config, store, task_store, registry, store.load()
    )

    assert outcome == "task_fatal"
    assert task_store.load().state_of("t-1") is TaskState.BLOCKED_BY_OPERATOR
