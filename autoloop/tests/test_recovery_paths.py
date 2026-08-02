"""The two escape hatches an operator reaches for when a run is wedged.

Both misbehaved in the same way: they looked like they addressed the problem
and quietly did something else. The rotation budget was documented per-run but
persisted per-session, so one dropped network spent it forever; `reset`
archived the task registry alongside the session, so clearing a wedged
conversation discarded the roadmap.
"""

import argparse

import pytest

from autoloop.cli import _cmd_reset, _reset_run_scoped_budgets
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.state import LoopState, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore

URL = "https://chatgpt.com/c/recovery-paths"


@pytest.fixture
def config(tmp_path):
    """Direct construction, like `test_doctor.make_config` — building one from
    the example TOML trips the conversation-url drift guard."""
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path.parent / f"{tmp_path.name}-workers",
    )


def _a_state(**kw) -> LoopState:
    return LoopState(
        session_id="recovery-paths",
        conversation_url=URL,
        **kw,
    )


# --- the rotation budget is per RUN -------------------------------------------


def test_a_new_run_starts_with_a_fresh_rotation_budget(config):
    """The incident: a dropped network spent the one rotation, and every later
    `run --retry` re-read the same count and parked with the same reason."""
    store = StateStore(config.state_file)
    store.save(_a_state(rotations=1))

    _reset_run_scoped_budgets(config)

    assert store.load().rotations == 0


def test_the_reset_is_recorded_with_what_it_forgave(config):
    """Zeroing a budget silently would hide genuine churn — the transcript
    event is what keeps 'every run rotates' visible."""
    StateStore(config.state_file).save(_a_state(rotations=2))

    _reset_run_scoped_budgets(config)

    entries = [
        line
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if "rotation_budget_reset" in line
    ]
    assert len(entries) == 1
    assert "2" in entries[0]


def test_an_unspent_budget_writes_nothing_at_all(config):
    """The common case is rotations == 0. It must not rewrite state or append
    a transcript line on every single run."""
    store = StateStore(config.state_file)
    store.save(_a_state(rotations=0))
    before = config.state_file.read_text(encoding="utf-8")

    _reset_run_scoped_budgets(config)

    assert config.state_file.read_text(encoding="utf-8") == before
    assert not config.transcript_file.exists() or "rotation_budget_reset" not in (
        config.transcript_file.read_text(encoding="utf-8")
    )


def test_no_session_yet_is_not_an_error(config):
    """`run` on a fresh checkout has no state file. Resetting a budget that
    does not exist must not crash the command before it starts."""
    assert not config.state_file.exists()
    _reset_run_scoped_budgets(config)  # must not raise


def test_the_cap_still_binds_within_one_run(config):
    """The guarantee the cap exists for. Resetting per-process must not turn
    into resetting per-iteration — `_run_continuous` rebuilds the orchestrator
    every pass, so a reset in the wrong place would refill the budget between
    rotations and remove the cap entirely."""
    from autoloop.policy import PolicyEngine

    policy = PolicyEngine(config.policy)
    assert policy.check_rotation_budget(0).allowed
    assert not policy.check_rotation_budget(1).allowed

    # A reset mid-run would be visible here: the state the orchestrator reads
    # keeps its spent count for the whole process.
    store = StateStore(config.state_file)
    store.save(_a_state(rotations=1))
    assert store.load().rotations == 1
    assert not policy.check_rotation_budget(store.load().rotations).allowed


# --- reset keeps the roadmap --------------------------------------------------


def _seed_tasks(config) -> TaskStore:
    store = TaskStore(config.tasks_file)
    store.save(
        TaskRegistry(
            [
                Task(id="t-1", title="keep me", description="d",
                     approved_paths=["docs/A.md"]),
                Task(id="t-2", title="me too", description="d",
                     approved_paths=["docs/B.md"]),
            ]
        )
    )
    return store


def _args(**kw):
    return argparse.Namespace(
        config=None, yes=kw.get("yes", True), tasks=kw.get("tasks", False)
    )


def test_reset_archives_the_session_and_keeps_the_registry(config, monkeypatch, capsys):
    """The damage this prevents: reaching for `reset` to clear a wedged
    conversation used to discard imported findings, priorities and quarantine
    decisions that had nothing to do with the wedge."""
    monkeypatch.setattr("autoloop.cli.load_config", lambda _p: config)
    StateStore(config.state_file).save(_a_state())
    tasks = _seed_tasks(config)

    assert _cmd_reset(_args()) == 0

    assert not config.state_file.exists()
    assert config.tasks_file.exists()
    assert {t.id for t in tasks.load().all_tasks()} == {"t-1", "t-2"}
    out = capsys.readouterr().out
    assert "task registry kept" in out
    # It must not claim to have touched something it did not.
    assert "tasks archived" not in out


def test_reset_tasks_opt_in_archives_both(config, monkeypatch):
    monkeypatch.setattr("autoloop.cli.load_config", lambda _p: config)
    StateStore(config.state_file).save(_a_state())
    _seed_tasks(config)

    assert _cmd_reset(_args(tasks=True)) == 0

    assert not config.state_file.exists()
    assert not config.tasks_file.exists()
    # Archived, never deleted — recoverable from the printed path.
    assert list(config.state_dir.glob("tasks.json.bak-*"))


def test_reset_without_yes_says_which_of_the_two_it_would_take(config, monkeypatch, capsys):
    """The old prompt said 'archives the current session state' while also
    archiving the registry. The confirmation has to name what it will do."""
    monkeypatch.setattr("autoloop.cli.load_config", lambda _p: config)
    StateStore(config.state_file).save(_a_state())
    _seed_tasks(config)

    assert _cmd_reset(_args(yes=False)) == 1
    plain = capsys.readouterr().out
    assert "task registry is kept" in plain

    assert _cmd_reset(_args(yes=False, tasks=True)) == 1
    assert "TASK REGISTRY" in capsys.readouterr().out

    # Refused, so nothing moved either time.
    assert config.state_file.exists()
    assert config.tasks_file.exists()
