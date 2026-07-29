"""Task-graph proposal generation: priority ordering, id assignment without
collisions, dependency mapping, human decisions excluded."""

from test_audit_reconcile import finding

from autoloop.audit.reconcile import reconcile
from autoloop.audit.taskgen import generate_tasks
from autoloop.tasks import Task, TaskRegistry


def registry(*ids):
    return TaskRegistry([Task(id=i, title="t", description="d") for i in ids])


def test_priority_ordering():
    result = reconcile(
        [
            finding("doc", category="doc_drift", files=("d.md",)),
            finding("loss", category="data_loss", files=("a.py",)),
            finding("sec", category="security", files=("b.py",)),
            finding("test", category="missing_test", files=("c.py",)),
        ]
    )
    proposal = generate_tasks(result, registry())
    priorities = [(t.finding_ids[0].split(":")[1], t.priority) for t in proposal.tasks]
    assert priorities == [("loss", 1), ("sec", 2), ("test", 5), ("doc", 7)]
    # ids assigned in priority order
    assert [t.id for t in proposal.tasks] == ["au-001", "au-002", "au-003", "au-004"]


def test_ids_skip_registry_collisions():
    result = reconcile([finding("f1")])
    proposal = generate_tasks(result, registry("au-001", "au-002"))
    assert proposal.tasks[0].id == "au-003"


def test_never_generates_reserved_roadmap_ids():
    result = reconcile([finding(f"f{i}", files=(f"x{i}.py",)) for i in range(5)])
    proposal = generate_tasks(result, registry())
    assert all(t.id.startswith("au-") for t in proposal.tasks)


def test_dependency_mapping_between_findings():
    result = reconcile(
        [
            finding("base", category="defect", files=("a.py",)),
            finding("follow", category="missing_test", files=("b.py",), deps=("base",)),
        ]
    )
    proposal = generate_tasks(result, registry())
    by_finding = {t.finding_ids[0].split(":")[1]: t for t in proposal.tasks}
    assert by_finding["follow"].depends_on == (by_finding["base"].id,)


def test_dependency_on_existing_roadmap_task_preserved():
    result = reconcile([finding("f1", deps=("A2",))])
    proposal = generate_tasks(result, registry("A2"))
    assert proposal.tasks[0].depends_on == ("A2",)


def test_unresolved_dependency_noted_not_invented():
    result = reconcile([finding("f1", deps=("ghost-finding",))])
    proposal = generate_tasks(result, registry())
    task = proposal.tasks[0]
    assert task.depends_on == ()
    assert "unresolved dependency" in task.description


def test_human_decisions_are_skipped_with_reason():
    result = reconcile([finding("hd", category="human_decision")])
    proposal = generate_tasks(result, registry())
    assert proposal.tasks == []
    assert proposal.skipped and "human decision" in proposal.skipped[0][1]


def test_task_carries_full_structure():
    result = reconcile([finding("f1")])
    [task] = generate_tasks(result, registry()).tasks
    assert task.scope == "fix it"
    assert "no drive-by refactors" in task.description
    assert task.acceptance_criteria == ("fixed",)
    assert task.validation_commands == ("ruff check .",)
    assert task.expected_files == ("a.py",)
    assert task.parallelizable is True
    assert task.to_plan_dict()["id"] == task.id
    assert "Scope:" in task.to_plan_dict()["description"]
