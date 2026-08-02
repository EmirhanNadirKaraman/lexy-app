"""Unlimited review rounds (guarded by convergence) and browser auto-restart.

Both exist because of the same session: three browser stalls each cleared by
restarting one Chrome profile by hand, and an audit abandoned at a hard cap of
2 while it was arguably still converging.
"""

from __future__ import annotations

import subprocess

import pytest

from autoloop.config import BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig
from autoloop.worktask import TaskExecution


def _orch():
    orch = Orchestrator.__new__(Orchestrator)
    orch._logged = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw.get("data") or {}))
    orch._last_browser_restart = None
    return orch


def _execution(**kw):
    return TaskExecution(task_id="t1", task_branch="autoloop/t1",
                         worktree_path="/tmp/w", task_base_sha="a" * 40, **kw)


def revise(text):
    return Directive(decision=Decision.REVISE, reason="r", task_id="t1", feedback=text)


# ---- review rounds -----------------------------------------------------------


def test_the_round_cap_defaults_to_unlimited():
    """A hard 2 abandoned work that was still converging. The operator decides
    when to give up, not this constant."""
    assert PolicyConfig().max_review_rounds == 0


def test_identical_feedback_twice_is_what_stops_an_unlimited_loop():
    orch = _orch()
    execution = _execution(review_round=7)
    execution.last_revise_feedback = orch._normalise_feedback("Fix the duplicate findings.")

    assert orch._revise_feedback_is_unchanged(execution, revise("Fix the duplicate findings."))
    # Whitespace and case are noise, not a new request.
    assert orch._revise_feedback_is_unchanged(execution, revise("  fix   THE Duplicate\nfindings.  "))


def test_genuinely_different_feedback_always_gets_another_round():
    """The guard must not be fuzzy: two different complaints deserve a round
    each, however many have already run."""
    orch = _orch()
    execution = _execution(review_round=99)
    execution.last_revise_feedback = orch._normalise_feedback("Fix the duplicate findings.")

    assert not orch._revise_feedback_is_unchanged(execution, revise("Now fix the file scopes."))


def test_a_non_revise_directive_is_never_treated_as_repeated():
    orch = _orch()
    execution = _execution(review_round=3)
    execution.last_revise_feedback = "anything"
    push = Directive(decision=Decision.PUSH, reason="ok", task_id="t1")
    assert not orch._revise_feedback_is_unchanged(execution, push)


def test_empty_feedback_never_trips_the_guard():
    """Absent feedback is not evidence of repetition — stopping on it would
    abandon a task for saying nothing."""
    orch = _orch()
    execution = _execution(review_round=3)
    execution.last_revise_feedback = ""
    assert not orch._revise_feedback_is_unchanged(execution, revise(""))


# ---- browser auto-restart ----------------------------------------------------


def _with_browser(orch, **kw):
    class _Cfg:
        browser = BrowserConfig(conversation_url="https://chatgpt.com/c/x", **kw)
    orch._config = _Cfg()
    return orch


def test_no_restart_command_means_the_old_behaviour(monkeypatch):
    orch = _with_browser(_orch())
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    assert orch._attempt_browser_restart() is False
    assert called == [], "nothing may be executed when none is configured"


def test_a_configured_command_runs_and_reports_success(monkeypatch):
    orch = _with_browser(_orch(), restart_command=("true",))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="up", stderr=""),
    )
    assert orch._attempt_browser_restart() is True
    assert any(e == "browser_restarted" for e, _ in orch._logged)


def test_the_cooldown_stops_a_restart_loop(monkeypatch):
    """Without it a genuinely dead transport thrashes the browser instead of
    surfacing the fault."""
    orch = _with_browser(_orch(), restart_command=("true",), restart_cooldown_seconds=600.0)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )
    assert orch._attempt_browser_restart() is True
    assert orch._attempt_browser_restart() is False, "second attempt must be refused"
    assert any(e == "browser_restart_skipped" for e, _ in orch._logged)


def test_a_failing_restart_is_reported_not_raised(monkeypatch):
    orch = _with_browser(_orch(), restart_command=("false",))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="boom"),
    )
    assert orch._attempt_browser_restart() is False
    assert any(e == "browser_restart_failed" for e, _ in orch._logged)


def test_a_restart_command_that_cannot_run_never_escapes(monkeypatch):
    """A broken restart command must not become a new crash on top of the
    browser fault it was meant to fix."""
    orch = _with_browser(_orch(), restart_command=("nope",))

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", boom)
    assert orch._attempt_browser_restart() is False
    assert any(e == "browser_restart_failed" for e, _ in orch._logged)


def test_restart_command_must_be_a_list_of_strings(tmp_path):
    from autoloop.config import load_config
    from autoloop.errors import ConfigError

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/x"\n'
        'restart_command = "restart.sh"\n\n'
        f'[paths]\nworkers_root = "{tmp_path / "w"}"\n', encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(cfg)
