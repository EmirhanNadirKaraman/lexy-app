"""`start`: repair what is provably safe, report what is not.

The split is the whole design. A lock whose owner is provably dead and a CDP
port that does not answer are decidable from evidence. Archiving an execution
record, quarantining a worker repo, or "resolving" a blocker are judgements
that destroy work when guessed wrong — a repair command that guesses at those
is worse than none, because it looks like it worked.
"""

import argparse

import pytest

from autoloop import cli
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.lock import LoopLock
from autoloop.state import LoopState, Phase, StateStore

URL = "https://chatgpt.com/c/start-command"


@pytest.fixture
def config(tmp_path):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path.parent / f"{tmp_path.name}-workers",
    )


@pytest.fixture
def wired(config, monkeypatch):
    """`start` with a healthy browser and no loop actually launched."""
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    monkeypatch.setattr(cli, "_default_probe_cdp", lambda url: '{"Browser":"Chrome"}')
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def _args(**kw):
    return argparse.Namespace(config=None, check_only=kw.get("check_only", True))


def _a_state(**kw):
    return LoopState(session_id="start-cmd", conversation_url=URL, **kw)


def _write_dead_lock(config):
    """A lock file whose owner cannot possibly be alive."""
    lock = LoopLock(config.state_dir)
    lock.acquire()
    lock._owned = False  # keep the file: simulate a process that died holding it
    import json

    data = json.loads(lock.path.read_text(encoding="utf-8"))
    data["pid"] = 999_999_999  # never a live pid
    lock.path.write_text(json.dumps(data), encoding="utf-8")
    return lock


# --- the safe repairs ---------------------------------------------------------


def test_a_dead_lock_is_cleared(wired, capsys):
    _write_dead_lock(wired)
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 0

    assert not (wired.state_dir / "LOCK").exists()
    assert "stale lock removed" in capsys.readouterr().out


def test_a_live_lock_means_already_running_not_a_fault(wired, capsys):
    """The case that shipped wrong in the first draft: a held lock is the
    healthy already-running state, and pressing start twice must not read as
    'something needs a decision'. It also must not run the repairs — restarting
    a working run's browser mid-request breaks a run that was fine."""
    def must_not_probe(url):
        raise AssertionError(
            "start touched the browser while a live run held the lock"
        )

    monkeypatch_target = cli._default_probe_cdp
    cli._default_probe_cdp = must_not_probe
    try:
        with LoopLock(wired.state_dir):
            code = cli._cmd_start(_args())
    finally:
        cli._default_probe_cdp = monkeypatch_target

    out = capsys.readouterr().out
    assert code == 0
    assert "already running" in out
    assert "need a decision" not in out


def test_the_pause_flag_is_cleared_because_start_means_run(wired, capsys):
    StateStore(wired.state_file).save(_a_state())
    wired.pause_file.parent.mkdir(parents=True, exist_ok=True)
    wired.pause_file.touch()

    assert cli._cmd_start(_args()) == 0

    assert not wired.pause_file.exists()
    assert "flag cleared" in capsys.readouterr().out


def test_a_silent_browser_is_restarted_via_the_declared_command(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(config.state_file).save(_a_state())
    object.__setattr__(config.browser, "restart_command", ("true",))

    probes = {"n": 0}

    def flaky(url):
        probes["n"] += 1
        if probes["n"] == 1:
            raise OSError("connection refused")
        return '{"Browser":"Chrome"}'

    monkeypatch.setattr(cli, "_default_probe_cdp", flaky)

    assert cli._cmd_start(_args()) == 0
    assert "restarted, CDP answering" in capsys.readouterr().out


def test_a_silent_browser_with_no_restart_command_refuses_to_start(config, monkeypatch, capsys):
    """Never infer which Chrome to kill. Without a declared command this is a
    human's job, and saying so beats pattern-matching process lists."""
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(config.state_file).save(_a_state())

    def dead(url):
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "_default_probe_cdp", dead)

    assert cli._cmd_start(_args()) == 2
    assert "no browser.restart_command" in capsys.readouterr().out


# --- what it must NOT do ------------------------------------------------------


def test_an_open_blocker_is_reported_never_resolved(wired, capsys):
    """Resolving a blocker means answering a question nobody has read. The
    command prints the exact `answer` invocation and stops."""
    store = BlockerStore(wired.blockers_dir)
    blocker = store.record(
        task_id="t-1",
        kind="task_fatal",
        code="approved_paths_missing",
        question="task t-1 has no approved_paths; add them and retry",
        detail="",
        phase="executing",
        now="2026-08-02T00:00:00+00:00",
    )
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args()) == 2

    out = capsys.readouterr().out
    assert "1 OPEN" in out
    assert f"python -m autoloop answer {blocker.id}" in out
    assert store.open_blockers(), "the blocker must still be open"


def test_a_parked_session_is_reported_never_auto_answered(wired, capsys):
    """`needs_user` would make continuous mode stop on the first pass, so
    starting into it is pointless — but the answer is the operator's."""
    StateStore(wired.state_file).save(
        _a_state(phase=Phase.NEEDS_USER.value, question="which action should be taken?")
    )

    assert cli._cmd_start(_args()) == 2

    out = capsys.readouterr().out
    assert "PARKED at needs_user" in out
    assert "which action should be taken?" in out
    assert "run --answer" in out
    # untouched: the phase is still parked for the operator to resolve
    assert StateStore(wired.state_file).load().phase == Phase.NEEDS_USER.value


def test_a_task_fatal_park_with_no_blocker_does_not_stop_start(wired, capsys):
    """Not every park needs a human. `_handle_parked_task` quarantines a
    task_fatal park's task and keeps going on the rest of the roadmap — that
    is the task_fatal/loop_fatal split. Refusing on every `needs_user` would
    send the operator off to resolve something the loop handles itself."""
    StateStore(wired.state_file).save(
        _a_state(
            phase=Phase.NEEDS_USER.value,
            park_kind="task_fatal",
            question="task t-9 failed its own validation",
        )
    )

    assert cli._cmd_start(_args()) == 0

    out = capsys.readouterr().out
    assert "quarantines that task and continues" in out
    assert "all clear" in out


def test_a_loop_fatal_park_still_stops_start(wired, capsys):
    StateStore(wired.state_file).save(
        _a_state(
            phase=Phase.NEEDS_USER.value,
            park_kind="loop_fatal",
            question="the conversation is unusable",
        )
    )

    assert cli._cmd_start(_args()) == 2
    assert "PARKED at needs_user" in capsys.readouterr().out


def test_a_failed_session_is_reported_with_its_recovery(wired, capsys):
    StateStore(wired.state_file).save(_a_state(phase=Phase.FAILED.value))

    assert cli._cmd_start(_args()) == 2
    assert "run --retry" in capsys.readouterr().out


def test_a_clean_slate_starts(wired, capsys):
    """No session yet is the fresh-checkout case, not a fault."""
    assert cli._cmd_start(_args()) == 0
    assert "all clear" in capsys.readouterr().out


def test_check_only_never_launches_the_loop(wired, monkeypatch, capsys):
    launched = []
    monkeypatch.setattr(cli, "_cmd_run", lambda a: launched.append(a) or 0)
    StateStore(wired.state_file).save(_a_state())

    assert cli._cmd_start(_args(check_only=True)) == 0
    assert launched == []

    assert cli._cmd_start(_args(check_only=False)) == 0
    assert len(launched) == 1
    assert launched[0].continuous is True
