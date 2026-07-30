"""Task-owned change manifests against real git repos: snapshot diffing
(created/modified/deleted, renames, pre-existing dirt), commit verification
rules, persistence."""

import hashlib
import subprocess

import pytest

from autoloop.git_gateway import GitGateway
from autoloop.errors import ManifestViolation
from autoloop.manifest import (
    ADOPTED_SCHEMA,
    KIND_ADOPTED,
    KIND_EXECUTOR,
    ChangeManifest,
    ManifestStore,
    render_adoption_block,
    snapshot,
    verify_commit,
    verify_tree_content,
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


# ---- adopted manifests: (path, mode, type, content) binding -----------------


def adopt(root, paths, manifest_id="adopt-1", mode="100644"):
    """`paths` may be a list (all approved as `mode`) or an explicit mapping."""
    entries = paths if isinstance(paths, dict) else {p: mode for p in paths}
    return ChangeManifest.adopt(manifest_id, entries, gw(root))


def present(manifest, report="report-sha-abc"):
    """Simulate the orchestrator's ready-phase binding."""
    manifest.presented_report_sha256 = report
    return manifest


def make_exec(path):
    path.chmod(0o755)
    return path


def staged_tree(repo, paths):
    """Stage `paths` and write the tree, as commit_adopted does."""
    run_git(repo, "add", "--", *paths)
    g = gw(repo)
    return g.write_tree(), g.tree_of("HEAD")


def test_adopted_manifest_records_paths_hashes_and_modes(repo):
    (repo / "a.txt").write_text("changed a\n")
    (repo / "run.sh").write_text("#!/bin/sh\n")
    make_exec(repo / "run.sh")
    m = adopt(repo, {"a.txt": "100644", "run.sh": "100755"})
    assert m.kind == KIND_ADOPTED and m.adopted_schema == ADOPTED_SCHEMA
    assert m.adopted["a.txt"] == {
        "sha256": hashlib.sha256(b"changed a\n").hexdigest(),
        "mode": "100644",
    }
    assert m.adopted["run.sh"]["mode"] == "100755"


def test_mode_is_never_inferred_from_the_working_tree(repo):
    """An executable file cannot be adopted as 100644, and vice versa."""
    (repo / "run.sh").write_text("#!/bin/sh\n")
    make_exec(repo / "run.sh")
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, {"run.sh": "100644"})
    assert "git would stage 100755" in str(excinfo.value)

    (repo / "plain.txt").write_text("x\n")
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, {"plain.txt": "100755"})
    assert "git would stage 100644" in str(excinfo.value)


@pytest.mark.parametrize("mode", ["120000", "160000", "040000", "100600", "", "100644 "])
def test_only_supported_blob_modes_may_be_adopted(repo, mode):
    (repo / "a.txt").write_text("x\n")
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, {"a.txt": mode})
    assert "may be adopted" in str(excinfo.value)


def test_adoption_requires_a_mapping_so_duplicates_are_unrepresentable(repo):
    (repo / "a.txt").write_text("x\n")
    with pytest.raises(ManifestViolation) as excinfo:
        ChangeManifest.adopt("m", ["a.txt", "a.txt"], gw(repo))
    assert "mapping of path -> approved git mode" in str(excinfo.value)


# --- tree verification: the authoritative gate ------------------------------


def test_exact_approved_content_and_mode_is_accepted(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    tree, parent = staged_tree(repo, ["a.txt"])
    assert verify_tree_content(m, ("a.txt",), gw(repo), tree, parent) == []


def test_approved_100644_committed_as_100755_is_rejected(repo):
    """Identical bytes, unapproved executable bit — the hole this round closed."""
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    m = present(adopt(repo, {"script.sh": "100644"}))
    make_exec(script)                                  # same bytes, +x
    tree, parent = staged_tree(repo, ["script.sh"])
    violations = verify_tree_content(m, ("script.sh",), gw(repo), tree, parent)
    assert violations and "unapproved file-mode change" in violations[0]
    assert "100755" in violations[0] and "100644" in violations[0]


def test_approved_100755_committed_as_100644_is_rejected(repo):
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\n")
    make_exec(script)
    m = present(adopt(repo, {"script.sh": "100755"}))
    script.chmod(0o644)                                # drops the bit
    tree, parent = staged_tree(repo, ["script.sh"])
    violations = verify_tree_content(m, ("script.sh",), gw(repo), tree, parent)
    assert violations and "unapproved file-mode change" in violations[0]


def test_new_file_with_an_unapproved_mode_is_rejected(repo):
    new = repo / "new.sh"
    new.write_text("#!/bin/sh\n")
    m = present(adopt(repo, {"new.sh": "100644"}))
    make_exec(new)
    tree, parent = staged_tree(repo, ["new.sh"])
    assert verify_tree_content(m, ("new.sh",), gw(repo), tree, parent)


def test_tree_content_change_is_rejected(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    (repo / "a.txt").write_text("TAMPERED\n")
    tree, parent = staged_tree(repo, ["a.txt"])
    violations = verify_tree_content(m, ("a.txt",), gw(repo), tree, parent)
    assert violations and "does not match the approved bytes" in violations[0]
    assert "TAMPERED" not in violations[0]


def test_tree_with_an_unapproved_path_is_rejected(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    (repo / "extra.txt").write_text("unreviewed\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    tree, parent = staged_tree(repo, ["a.txt", "extra.txt"])
    violations = verify_tree_content(m, ("a.txt",), gw(repo), tree, parent)
    assert any("unapproved paths" in v and "extra.txt" in v for v in violations)


def test_tree_missing_an_approved_path_is_rejected(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    (repo / "b.txt").write_text("also approved\n")
    m = present(adopt(repo, {"a.txt": "100644", "b.txt": "100644"}))
    tree, parent = staged_tree(repo, ["a.txt"])          # b.txt not staged
    violations = verify_tree_content(m, ("a.txt", "b.txt"), gw(repo), tree, parent)
    assert any("does not change approved paths" in v for v in violations)


def test_symlink_and_gitlink_entries_remain_rejected(repo):
    """A tree entry that is a link can never satisfy an adopted path."""
    (repo / "target.txt").write_text("target\n")
    (repo / "a.txt").write_text("APPROVED\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    (repo / "a.txt").unlink()
    (repo / "a.txt").symlink_to("target.txt")
    tree, parent = staged_tree(repo, ["a.txt"])
    violations = verify_tree_content(m, ("a.txt",), gw(repo), tree, parent)
    assert violations and ("mode 120000" in violations[0] or "symlink" in violations[0])


# --- awkward but valid filenames --------------------------------------------


@pytest.mark.parametrize("name", ["has space.txt", "has\ttab.txt", "quote\"d.txt", "ümlaut.txt"])
def test_awkward_filenames_do_not_confuse_verification(repo, name):
    """`status`/`ls-tree` without -z quote and escape these, which would make a
    pathname-keyed check compare the wrong string."""
    (repo / name).write_text("APPROVED\n")
    g = gw(repo)
    assert name in g.dirty_paths()                       # NUL-parsed status
    m = present(adopt(repo, {name: "100644"}))
    assert name in m.adopted
    tree, parent = staged_tree(repo, [name])
    assert name in g.tree_entries(tree)                  # NUL-parsed ls-tree
    assert g.changed_paths(parent, tree) == {name}       # NUL-parsed diff-tree
    assert verify_tree_content(m, (name,), g, tree, parent) == []


def test_awkward_filename_tampering_is_still_caught(repo):
    name = "has\ttab.txt"
    (repo / name).write_text("APPROVED\n")
    m = present(adopt(repo, {name: "100644"}))
    (repo / name).write_text("TAMPERED\n")
    tree, parent = staged_tree(repo, [name])
    assert verify_tree_content(m, (name,), gw(repo), tree, parent)


# --- schema: pre-mode manifests are refused, never reinterpreted ------------


def test_legacy_hash_only_manifest_is_refused(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    # Simulate a v1 manifest: hash only, no mode, no schema marker.
    m.adopted = {"a.txt": m.adopted["a.txt"]["sha256"]}
    m.adopted_schema = 0
    tree, parent = staged_tree(repo, ["a.txt"])
    violations = verify_tree_content(m, ("a.txt",), gw(repo), tree, parent)
    assert violations and "entry schema v0" in violations[0]
    assert "cannot be reinterpreted" in violations[0]
    assert verify_commit(m, ("a.txt",), gw(repo))        # the pre-check refuses too


def test_malformed_adopted_entry_is_refused(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    del m.adopted["a.txt"]["mode"]
    tree, parent = staged_tree(repo, ["a.txt"])
    violations = verify_tree_content(m, ("a.txt",), gw(repo), tree, parent)
    assert violations and "malformed" in violations[0]


# --- pre-stage working-tree check + provenance path -------------------------


def test_worktree_precheck_still_catches_a_changed_file(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    (repo / "a.txt").write_text("APPROVED \n")
    violations = verify_commit(m, ("a.txt",), gw(repo))
    assert violations and "changed after approval" in violations[0]


def test_unpresented_adopted_manifest_is_refused(repo):
    (repo / "a.txt").write_text("changed\n")
    m = adopt(repo, {"a.txt": "100644"})                 # never presented
    violations = verify_commit(m, ("a.txt",), gw(repo))
    assert violations and "never presented for review" in violations[0]


def test_unapproved_path_cannot_be_added_to_an_adopted_commit(repo):
    (repo / "a.txt").write_text("changed\n")
    (repo / "sneaky.txt").write_text("never reviewed\n")
    m = present(adopt(repo, {"a.txt": "100644"}))
    violations = verify_commit(m, ("a.txt", "sneaky.txt"), gw(repo))
    assert any("not in adopted manifest" in v for v in violations)


def test_multiple_dirty_files_stay_separable_into_explicit_groups(repo):
    (repo / "a.txt").write_text("group one\n")
    (repo / "g2.txt").write_text("group two\n")
    (repo / "untouched.txt").write_text("neither group\n")
    g1 = present(adopt(repo, {"a.txt": "100644"}, manifest_id="g1"))
    g2 = present(adopt(repo, {"g2.txt": "100644"}, manifest_id="g2"))
    assert verify_commit(g1, ("a.txt",), gw(repo)) == []
    assert verify_commit(g2, ("g2.txt",), gw(repo)) == []
    assert verify_commit(g1, ("g2.txt",), gw(repo))
    assert verify_commit(g1, ("untouched.txt",), gw(repo))


@pytest.mark.parametrize(
    "paths,expected",
    [
        ({}, "non-empty path list"),
        ({"../outside.txt": "100644"}, "without '..'"),
        ({"/etc/passwd": "100644"}, "repository-relative"),
        ({"clean.txt": "100644"}, "not a pending change"),
    ],
)
def test_adoption_refuses_invalid_path_sets(repo, paths, expected):
    (repo / "a.txt").write_text("dirty\n")
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, paths)
    assert expected in str(excinfo.value)


def test_adoption_refuses_a_deleted_path(repo):
    (repo / "tracked.txt").unlink()
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, {"tracked.txt": "100644"})
    assert "deleted" in str(excinfo.value)


def test_adoption_refuses_a_symlink(repo):
    (repo / "secret.txt").write_text("SECRET TARGET\n")
    (repo / "link.py").symlink_to("secret.txt")
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, {"link.py": "100644"})
    assert "symlink" in str(excinfo.value)


def test_adoption_refuses_a_path_under_a_symlinked_directory(repo):
    (repo / "real").mkdir()
    (repo / "real" / "f.txt").write_text("content\n")
    (repo / "linkdir").symlink_to("real")
    with pytest.raises(ManifestViolation) as excinfo:
        adopt(repo, {"linkdir/f.txt": "100644"})
    assert "symlink" in str(excinfo.value)


def test_adoption_never_infers_paths_from_the_dirty_tree(repo):
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    m = adopt(repo, {"a.txt": "100644"})
    assert sorted(m.adopted) == ["a.txt"]


def test_executor_manifest_behaviour_is_unchanged(repo):
    (repo / "human.txt").write_text("pre-existing\n")
    manifest = begin(repo)
    (repo / "new.txt").write_text("task output\n")
    finish(manifest, repo)
    assert manifest.kind == KIND_EXECUTOR and not manifest.is_adopted()
    assert manifest.adopted_schema == 0                   # irrelevant to this kind
    assert verify_commit(manifest, ("new.txt",)) == []
    assert any(
        "already modified before the task" in v
        for v in verify_commit(manifest, ("human.txt",))
    )


def test_adoption_block_lists_mode_and_hash_and_is_deterministic(repo):
    (repo / "a.txt").write_text("one\n")
    (repo / "b.txt").write_text("two\n")
    m = adopt(repo, {"b.txt": "100644", "a.txt": "100644"})
    block = render_adoption_block(m)
    assert block.splitlines()[0].startswith(f"ADOPTED-MANIFEST v{ADOPTED_SCHEMA} adopt-1 base=")
    assert block.splitlines()[1].endswith("  a.txt")      # sorted
    for path, entry in m.adopted.items():
        assert entry["sha256"] in block and entry["mode"] in block and path in block
    assert render_adoption_block(m) == block


def test_adoption_block_refused_for_executor_manifests(repo):
    manifest = finish(begin(repo), repo)
    with pytest.raises(ManifestViolation):
        render_adoption_block(manifest)


def test_round_trip_preserves_kind_schema_modes_and_binding(tmp_path, repo):
    (repo / "a.txt").write_text("changed\n")
    m = present(adopt(repo, {"a.txt": "100644"}), report="bound-report-sha")
    store = ManifestStore(tmp_path / "manifests")
    store.save(m)
    loaded = store.load("adopt-1")
    assert loaded.kind == KIND_ADOPTED and loaded.adopted_schema == ADOPTED_SCHEMA
    assert loaded.adopted == m.adopted                    # dict entries survive JSON
    assert loaded.presented_report_sha256 == "bound-report-sha"
    assert verify_commit(loaded, ("a.txt",), gw(repo)) == []


def test_manifests_written_before_adoption_existed_still_load(tmp_path):
    import json

    d = tmp_path / "manifests"
    d.mkdir()
    (d / "legacy.json").write_text(
        json.dumps(
            {
                "manifest_id": "legacy", "task_id": "t1", "base_head": "a" * 40,
                "baseline": {}, "started_at": "t", "created": ["x.py"],
                "modified": [], "deleted": [], "finished_at": "t",
            }
        )
    )
    loaded = ManifestStore(d).load("legacy")
    assert loaded.kind == KIND_EXECUTOR and not loaded.is_adopted()
    assert verify_commit(loaded, ("x.py",)) == []
