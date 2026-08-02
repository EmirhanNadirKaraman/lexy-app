"""B9: a retry must not rebuild on a base the branch has moved past.

`TaskExecution.task_base_sha` is recorded once, at first dispatch. Observed
2026-08-02: audit-0001 was refused for two failing tests, those tests were
fixed one commit later, and the retry failed with the SAME two because its
base predated the fix — it would have repeated forever.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import Phase
from autoloop.tasks import Task
from autoloop.worktask import TaskExecution, TaskExecutionStore


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@e.com")
    git(root, "config", "user.name", "T")
    (root / "f.txt").write_text("one\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    return root


class _FakeWorkerRepos:
    """Records what the re-base asks of it. `create` returns an object with the
    two attributes the caller reads."""

    def __init__(self, root):
        self.root = Path(root)
        self.quarantined: list[str] = []
        self.created: list[str] = []

    def quarantine(self, task_id, label):
        self.quarantined.append(label)
        return self.root / f"quarantine/{task_id}-{label}"

    def create(self, task_id, source, base_sha):
        self.created.append(base_sha)

        class _Repo:
            branch = f"autoloop/{task_id}"
            path = self.root / task_id

        return _Repo()


def _orch(repo, tmp_path, execution, review_round=0):
    """An Orchestrator with only what `_rebase_execution_if_stale` touches."""
    from autoloop.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._git = GitGateway(repo, PolicyEngine(PolicyConfig()))
    orch._worker_repos = _FakeWorkerRepos(tmp_path / "workers")
    store = TaskExecutionStore(tmp_path / "executions")
    orch._execution_store = store
    orch._logged: list = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw))
    orch._parked: list = []
    orch._to_needs_user = lambda msg, **kw: orch._parked.append((msg, kw))
    execution.review_round = review_round
    store.save(execution)
    return orch


def _execution(repo, base, **kw):
    return TaskExecution(
        task_id="t1", task_branch="autoloop/t1",
        worktree_path=str(repo / "worker"), task_base_sha=base, **kw
    )


def test_a_base_left_behind_is_rebased_onto_the_current_head(repo, tmp_path):
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")          # the fix that lands later
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the fix")
    head = git(repo, "rev-parse", "HEAD")

    execution = _execution(repo, old_base, candidate_sha="c" * 40, attempt_count=2)
    orch = _orch(repo, tmp_path, execution)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is not None, "must not park when nothing has been reviewed"
    assert result.task_base_sha == head, "re-pointed at the branch head"
    assert orch._worker_repos.created == [head], "worker rebuilt at the new base"
    assert orch._worker_repos.quarantined, "old worker preserved as evidence, not deleted"
    assert result.candidate_sha == "", "the old candidate belonged to the old base"
    assert result.attempt_count == 2, (
        "attempt_count must survive: a moving base must not refill the retry "
        "budget, or a task could churn forever by re-basing"
    )
    # Persisted, not just mutated in memory — a crash must not lose the re-base.
    assert TaskExecutionStore(tmp_path / "executions").load("t1").task_base_sha == head


def test_an_already_reviewed_candidate_refuses_instead_of_discarding(repo, tmp_path):
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    execution = _execution(repo, old_base, candidate_sha="c" * 40)
    orch = _orch(repo, tmp_path, execution, review_round=1)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None, "must park rather than proceed"
    assert orch._worker_repos.created == [], "nothing rebuilt"
    assert orch._worker_repos.quarantined == [], "nothing moved"
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head"
    assert old_base[:12] in message and "review round has already run" in message


def test_an_up_to_date_base_is_left_alone(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    execution = _execution(repo, head)
    orch = _orch(repo, tmp_path, execution)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is execution
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []


def test_a_base_that_is_not_an_ancestor_is_left_alone(repo, tmp_path):
    """A base off the branch entirely (rewritten history) is unusual — stopping
    is safer than silently re-pointing it."""
    unrelated = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-m", "orphan")
    execution = _execution(repo, unrelated)
    orch = _orch(repo, tmp_path, execution)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))
    assert result is execution
    assert orch._worker_repos.created == []


def test_phase_enum_is_untouched_by_this_module():
    assert Phase.NEEDS_USER.value == "needs_user"
