"""Policy enforcement: directive authorization (git gating + task-graph
references), the git command whitelist (force pushes / destructive ops
structurally impossible), and budgets."""

import pytest

from autoloop.contract import Decision, Directive
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task, TaskRegistry


def directive(decision: Decision, task_id=None) -> Directive:
    return Directive(
        decision=decision, reason="r", task_id=task_id, commit_message="m"
    )


def engine(**overrides) -> PolicyEngine:
    return PolicyEngine(PolicyConfig(**overrides))


def make_registry() -> TaskRegistry:
    reg = TaskRegistry(
        [
            Task(id="ready1", title="t", description="d"),
            Task(id="done1", title="t", description="d"),
            Task(id="blocked1", title="t", description="d", depends_on=("ready1",)),
            Task(id="wip1", title="t", description="d"),
        ]
    )
    reg.mark_completed("done1")
    reg.mark_in_progress("wip1")
    return reg


def auth(eng, d, branch="feature/x", registry=None):
    return eng.authorize_directive(d, branch, registry or make_registry())


# Task-reference tests run with the phase gate lifted; the gate itself is
# tested separately below.
def task_engine(**overrides):
    overrides.setdefault("implement_enabled", True)
    return engine(**overrides)


# ---- directive authorization -------------------------------------------------


@pytest.mark.parametrize("decision", [Decision.AUDIT, Decision.STOP, Decision.PLAN])
def test_non_git_non_task_decisions_always_allowed(decision):
    assert auth(engine(), directive(decision)).allowed


@pytest.mark.parametrize("decision", [Decision.IMPLEMENT, Decision.REVISE])
def test_task_decisions_allowed_on_ready_task(decision):
    assert auth(task_engine(), directive(decision, task_id="ready1")).allowed


def test_implement_allowed_on_in_progress_task():
    assert auth(task_engine(), directive(Decision.IMPLEMENT, task_id="wip1")).allowed


def test_implement_unknown_task_denied():
    verdict = auth(task_engine(), directive(Decision.IMPLEMENT, task_id="ghost"))
    assert not verdict.allowed and verdict.code == "task_unknown"


def test_implement_blocked_task_denied():
    verdict = auth(task_engine(), directive(Decision.IMPLEMENT, task_id="blocked1"))
    assert not verdict.allowed and verdict.code == "task_blocked"


def test_implement_completed_task_denied():
    verdict = auth(task_engine(), directive(Decision.IMPLEMENT, task_id="done1"))
    assert not verdict.allowed and verdict.code == "task_completed"


def test_commit_completion_of_completed_task_allowed():
    # Idempotent crash recovery: re-approving a commit that already completed
    # its task must not be denied.
    assert auth(engine(), directive(Decision.COMMIT, task_id="done1")).allowed


def test_commit_completion_of_unknown_task_denied():
    verdict = auth(engine(), directive(Decision.COMMIT, task_id="ghost"))
    assert not verdict.allowed and verdict.code == "task_unknown"


def test_commit_allowed_by_default():
    assert auth(engine(), directive(Decision.COMMIT), branch="main").allowed


def test_commit_denied_when_disabled():
    verdict = auth(engine(allow_commit=False), directive(Decision.COMMIT))
    assert not verdict.allowed and verdict.code == "commit_disabled"


def test_push_denied_on_protected_branch():
    verdict = auth(engine(), directive(Decision.PUSH), branch="main")
    assert not verdict.allowed and verdict.code == "protected_branch"


def test_commit_and_push_denied_on_protected_branch():
    verdict = auth(engine(), directive(Decision.COMMIT_AND_PUSH), branch="master")
    assert not verdict.allowed and verdict.code == "protected_branch"


def test_push_allowed_on_feature_branch():
    assert auth(engine(), directive(Decision.PUSH)).allowed


def test_push_allowed_on_protected_with_override():
    assert auth(engine(allow_protected_push=True), directive(Decision.PUSH), branch="main").allowed


def test_push_denied_when_disabled_even_with_override():
    verdict = auth(
        engine(allow_push=False, allow_protected_push=True), directive(Decision.PUSH)
    )
    assert not verdict.allowed and verdict.code == "push_disabled"


def test_custom_protected_branches():
    eng = engine(protected_branches=("release",))
    assert auth(eng, directive(Decision.PUSH), branch="main").allowed
    assert not auth(eng, directive(Decision.PUSH), branch="release").allowed


# ---- git command whitelist ---------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ("status", "--porcelain"),
        ("rev-parse", "HEAD"),
        ("branch", "--show-current"),
        ("log", "-1", "--format=%B"),
        ("restore", "--staged", "--", "a.py"),
        ("add", "--", "a.py", "b.py"),
        ("commit", "-m", "feat: something"),
        ("push", "origin", "feature/x"),
    ],
)
def test_whitelisted_commands_allowed(args):
    assert engine().validate_git_command(args).allowed


@pytest.mark.parametrize(
    "args,code",
    [
        ((), "git_empty"),
        (("push", "--force", "origin", "main"), "git_flag_forbidden"),
        (("push", "-f", "origin", "main"), "git_flag_forbidden"),
        (("push", "--force-with-lease"), "git_flag_forbidden"),
        (("push", "--delete", "origin", "x"), "git_flag_forbidden"),
        (("push", "--mirror"), "git_flag_forbidden"),
        (("reset", "--hard", "HEAD~1"), "git_subcommand_forbidden"),
        (("clean", "-fd"), "git_subcommand_forbidden"),
        (("rebase", "-i", "HEAD~3"), "git_subcommand_forbidden"),
        (("checkout", "--", "."), "git_subcommand_forbidden"),
        (("restore", "."), "git_flag_required"),
        (("add", "-A"), "git_flag_forbidden"),
        (("add", "a.py"), "git_flag_required"),
        (("filter-branch", "--all"), "git_subcommand_forbidden"),
        (("stash", "drop"), "git_subcommand_forbidden"),
        (("commit", "--amend"), "git_flag_forbidden"),
        (("add", "-f", "ignored.bin"), "git_flag_forbidden"),
    ],
)
def test_forbidden_commands_denied(args, code):
    verdict = engine().validate_git_command(args)
    assert not verdict.allowed
    assert verdict.code == code


def test_force_push_has_no_config_escape_hatch():
    permissive = engine(
        allow_commit=True, allow_push=True, allow_protected_push=True, protected_branches=()
    )
    assert not permissive.validate_git_command(("push", "--force", "origin", "main")).allowed


# ---- budgets -----------------------------------------------------------------


def test_iteration_budget():
    eng = engine(max_iterations=3)
    assert eng.check_iteration_budget(3).allowed
    verdict = eng.check_iteration_budget(4)
    assert not verdict.allowed and verdict.code == "iteration_budget"


def test_failure_budget():
    eng = engine(max_consecutive_failures=2)
    assert eng.check_failure_budget(2).allowed
    assert not eng.check_failure_budget(3).allowed


def test_browser_restart_skip_budget_is_separate_from_the_failure_budget():
    """Failures the restart cooldown refused are counted here instead of on
    `check_failure_budget` — one is "recovery does not work", the other is
    "recovery was never tried"."""
    eng = engine(max_consecutive_failures=3, max_browser_restart_skips=2)
    assert eng.check_browser_restart_skip_budget(2).allowed
    verdict = eng.check_browser_restart_skip_budget(3)
    assert not verdict.allowed and verdict.code == "browser_restart_skip_budget"
    assert "cooldown" in verdict.reason
    # Spending one never spends the other.
    assert eng.check_failure_budget(3).allowed


def test_parse_budget():
    eng = engine(max_parse_retries=1)
    assert eng.check_parse_budget(1).allowed
    assert not eng.check_parse_budget(2).allowed


def test_denial_budget():
    eng = engine(max_policy_denials=1)
    assert eng.check_denial_budget(1).allowed
    assert not eng.check_denial_budget(2).allowed


# ---- phase gate (Phase 3: audit review only) --------------------------------


def test_implement_denied_by_default_phase_gate():
    verdict = auth(engine(), directive(Decision.IMPLEMENT, task_id="ready1"))
    assert not verdict.allowed and verdict.code == "phase_gate"
    assert "audit review only" in verdict.reason


def test_revise_of_registry_task_denied_by_default_phase_gate():
    verdict = auth(engine(), directive(Decision.REVISE, task_id="ready1"))
    assert not verdict.allowed and verdict.code == "phase_gate"


def test_revise_of_audit_allowed_under_phase_gate():
    assert auth(engine(), directive(Decision.REVISE, task_id="audit")).allowed


def test_implement_of_audit_pseudo_task_denied():
    verdict = auth(task_engine(), directive(Decision.IMPLEMENT, task_id="audit"))
    assert not verdict.allowed and verdict.code == "phase_gate"


def test_commit_allowed_under_phase_gate():
    # The audit-review cycle needs stamped Markdown commits even while
    # implement is disabled.
    assert auth(engine(), directive(Decision.COMMIT, task_id="done1")).allowed


def test_phase_gate_reason_does_not_offer_ask_user():
    """The phase-gate denial lists what IS still available. `ask_user` was on
    that list and is now retired, so leaving it there would have the policy
    layer recommending the one decision it refuses unconditionally."""
    verdict = auth(engine(), directive(Decision.IMPLEMENT, task_id="ready1"))
    assert not verdict.allowed
    assert "ask_user" not in verdict.reason
    assert "stop" in verdict.reason  # still names the real alternatives


# ---- ask_user retirement -----------------------------------------------------


def test_ask_user_denied_under_fully_permissive_config():
    """Unconditional means no configuration admits it. Everything that could
    plausibly gate a decision is switched to its most permissive setting
    here, on a non-protected branch — if the denial were incidental to any
    of these flags rather than categorical, this is where it would show."""
    eng = engine(
        implement_enabled=True,
        allow_commit=True,
        allow_push=True,
        allow_protected_push=True,
        protected_branches=(),
    )
    verdict = auth(eng, directive(Decision.ASK_USER))
    assert not verdict.allowed
    assert verdict.code == "legacy_ask_user_retired"


@pytest.mark.parametrize("task_id", [None, "ready1", "done1", "blocked1", "ghost"])
def test_ask_user_denied_regardless_of_task_reference(task_id):
    """The denial precedes every task-reference check, so the verdict must be
    the retirement one for a known-ready task and an unknown one alike —
    never `task_unknown`/`task_blocked`, which would imply that naming a
    valid task could make `ask_user` executable."""
    verdict = auth(task_engine(), directive(Decision.ASK_USER, task_id=task_id))
    assert not verdict.allowed
    assert verdict.code == "legacy_ask_user_retired"


@pytest.mark.parametrize("branch", ["main", "master", "feature/x"])
def test_ask_user_denied_on_every_branch(branch):
    verdict = auth(engine(), directive(Decision.ASK_USER), branch=branch)
    assert not verdict.allowed and verdict.code == "legacy_ask_user_retired"


def test_ask_user_denial_reason_does_not_recommend_ask_user():
    """Guidance that pointed back at the refused decision would loop the
    reviewer straight into the same denial until the budget ran out."""
    verdict = auth(engine(), directive(Decision.ASK_USER))
    assert verdict.reason.count("ask_user") == 1  # names itself, once, as retired
    assert "retired" in verdict.reason
    assert "stop" in verdict.reason
