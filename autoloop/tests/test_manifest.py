"""Task-owned change manifests against real git repos: snapshot diffing
(created/modified/deleted, renames, pre-existing dirt), commit verification
rules, persistence."""

import subprocess

import pytest

from autoloop.git_gateway import GitGateway
from autoloop.manifest import (
    ChangeManifest,
    ManifestStore,
    snapshot,
    verify_commit,
)
from autoloop.policy import PolicyConfig, PolicyEngine


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "tracked.txt").write_text("v1\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


def gw(root) -> GitGateway:
    return GitGateway(root, PolicyEngine(PolicyConfig()))


def begin(root) -> ChangeManifest:
    return ChangeManifest.begin("m-1", "t1", gw(root))


def finish(manifest, root) -> ChangeManifest:
    manifest.finish(snapshot(gw(root)))
    return manifest


# ---- snapshot diffing -------------------------------------------------------


def test_task_created_file(repo):
    manifest = begin(repo)
    (repo / "new.txt").write_text("made by task\n")
    finish(manifest, repo)
    assert manifest.created == ["new.txt"]
    assert manifest.modified == [] and manifest.deleted == []


def test_task_modified_tracked_file(repo):
    manifest = begin(repo)
    (repo / "tracked.txt").write_text("v2 by task\n")
    finish(manifest, repo)
    assert manifest.modified == ["tracked.txt"]


def test_task_deleted_tracked_file(repo):
    manifest = begin(repo)
    (repo / "tracked.txt").unlink()
    finish(manifest, repo)
    assert manifest.deleted == ["tracked.txt"]


def test_preexisting_dirty_file_untouched_is_not_a_task_change(repo):
    (repo / "human.txt").write_text("human work before the task\n")
    manifest = begin(repo)
    (repo / "new.txt").write_text("task output\n")
    finish(manifest, repo)
    assert manifest.created == ["new.txt"]
    assert "human.txt" not in manifest.task_changed_paths()


def test_unrelated_file_changed_during_task_is_recorded_as_task_change(repo):
    # The manifest cannot attribute WHO edited during the window — anything
    # that changed after baseline counts as task-changed and is therefore
    # committable only with explicit approval. What matters for safety is the
    # pre-existing-dirt exclusion, covered above.
    (repo / "human.txt").write_text("human v1\n")
    manifest = begin(repo)
    (repo / "human.txt").write_text("edited during the task window\n")
    finish(manifest, repo)
    assert manifest.modified == ["human.txt"]


def test_worktree_rename_is_delete_plus_create(repo):
    manifest = begin(repo)
    (repo / "tracked.txt").rename(repo / "renamed.txt")
    finish(manifest, repo)
    assert manifest.deleted == ["tracked.txt"]
    assert manifest.created == ["renamed.txt"]


def test_reverted_baseline_dirt_is_not_a_task_change(repo):
    (repo / "tracked.txt").write_text("dirty before task\n")
    manifest = begin(repo)
    (repo / "tracked.txt").write_text("v1\n")  # back to HEAD content
    finish(manifest, repo)
    assert manifest.task_changed_paths() == set()


# ---- verify_commit ----------------------------------------------------------


def make_finished(repo, task_files=("new.txt",)) -> ChangeManifest:
    manifest = begin(repo)
    for name in task_files:
        (repo / name).write_text(f"task output {name}\n")
    return finish(manifest, repo)


def test_verify_allows_exact_task_paths(repo):
    manifest = make_finished(repo)
    assert verify_commit(manifest, ("new.txt",)) == []


def test_verify_refuses_empty_paths(repo):
    manifest = make_finished(repo)
    violations = verify_commit(manifest, ())
    assert violations and "explicit path list" in violations[0]


def test_verify_refuses_unfinished_manifest(repo):
    manifest = begin(repo)
    violations = verify_commit(manifest, ("new.txt",))
    assert violations and "unfinished" in violations[0]


def test_verify_refuses_preexisting_dirty_path(repo):
    (repo / "human.txt").write_text("human work\n")
    manifest = make_finished(repo)
    violations = verify_commit(manifest, ("new.txt", "human.txt"))
    assert any("already modified before the task" in v for v in violations)


def test_verify_refuses_path_not_changed_by_task(repo):
    manifest = make_finished(repo)
    violations = verify_commit(manifest, ("new.txt", "ghost.txt"))
    assert any("was not changed by task" in v for v in violations)


def test_verify_allows_task_deletions(repo):
    manifest = begin(repo)
    (repo / "tracked.txt").unlink()
    finish(manifest, repo)
    assert verify_commit(manifest, ("tracked.txt",)) == []


# ---- persistence ------------------------------------------------------------


def test_store_roundtrip(tmp_path, repo):
    manifest = make_finished(repo)
    store = ManifestStore(tmp_path / "manifests")
    store.save(manifest)
    loaded = store.load("m-1")
    assert loaded.task_id == "t1"
    assert loaded.created == ["new.txt"]
    assert loaded.finished_at == manifest.finished_at


def test_store_missing_returns_none(tmp_path):
    assert ManifestStore(tmp_path).load("nope") is None
