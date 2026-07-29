"""Review-context builder: integrity stamp values, git summary, changed-file
parsing, previous decision/task, roadmap and validation summaries."""

import hashlib

from autoloop.context import build_context, render_context
from autoloop.state import LoopState
from autoloop.tasks import Task, TaskRegistry

URL = "https://chatgpt.com/c/test"


class FakeGit:
    def __init__(self):
        self.head = "a" * 40
        self.branch = "feature/x"
        self.porcelain = [" M lexy-app/a.py", "?? new_file.py", "R  old.py -> new.py"]

    def head_sha(self):
        return self.head

    def current_branch(self):
        return self.branch

    def dirty_files(self):
        return list(self.porcelain)


def make_state(**kw):
    state = LoopState.new(URL)
    for key, value in kw.items():
        setattr(state, key, value)
    return state


def make_registry():
    return TaskRegistry([Task(id="t1", title="First task", description="d")])


def test_integrity_stamp_values():
    payload = "the report body"
    ctx = build_context(make_state(), FakeGit(), make_registry(), "alr-x-0001", payload)
    assert ctx.request_id == "alr-x-0001"
    assert ctx.head_sha == "a" * 40
    assert ctx.base_sha == "(none)"  # nothing reviewed yet
    assert ctx.report_sha256 == hashlib.sha256(payload.encode()).hexdigest()
    assert ctx.timestamp  # stamped


def test_base_sha_uses_reviewed_commit():
    state = make_state(reviewed_commit="b" * 40)
    ctx = build_context(state, FakeGit(), make_registry(), "r", "p")
    assert ctx.base_sha == "b" * 40


def test_changed_files_parsed_from_porcelain():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "r", "p")
    assert ctx.changed_files == ("lexy-app/a.py", "new_file.py", "new.py")
    assert ctx.dirty_count == 3


def test_previous_decision_and_task():
    state = make_state(
        last_decision="implement",
        current_task={"task_id": "t1", "title": "First task", "decision": "implement"},
        last_validation="ruff clean; 12 tests passed",
    )
    ctx = build_context(state, FakeGit(), make_registry(), "r", "p")
    assert ctx.previous_decision == "implement"
    assert "t1" in ctx.previous_task
    assert "First task" in ctx.previous_task
    assert ctx.validation_summary == "ruff clean; 12 tests passed"


def test_defaults_when_nothing_happened_yet():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "r", "p")
    assert ctx.previous_decision == "(none)"
    assert ctx.previous_task == "(none)"
    assert ctx.validation_summary == "(none)"


def test_roadmap_status_from_registry():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "r", "p")
    assert "next ready: t1" in ctx.roadmap_status


def test_render_contains_all_labels():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "alr-x-0001", "p")
    block = render_context(ctx)
    for label in (
        "request_id: alr-x-0001",
        "timestamp:",
        f"head_sha: {'a' * 40}",
        "base_sha:",
        "report_sha256:",
        "branch: feature/x",
        "changed_files:",
        "previous_decision:",
        "previous_task:",
        "validation:",
        "roadmap:",
    ):
        assert label in block


def test_render_truncates_long_file_lists():
    git = FakeGit()
    git.porcelain = [f" M f{i}.py" for i in range(50)]
    ctx = build_context(make_state(), git, make_registry(), "r", "p")
    block = render_context(ctx, max_files=40)
    assert "10 more" in block
