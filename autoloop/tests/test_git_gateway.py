"""Git gateway against real throwaway repos: reads, EXACT-path staging (no
`git add -A` anywhere), idempotent commits, pushes to a bare remote, and
policy denial before subprocess."""

import subprocess

import pytest

from autoloop.errors import GitCommandError, GitOperationDenied
from autoloop.git_gateway import GitGateway
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
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "a.txt").write_text("one\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


def gateway(root) -> GitGateway:
    return GitGateway(root, PolicyEngine(PolicyConfig()))


def test_reads(repo):
    gw = gateway(repo)
    assert gw.current_branch() == "main"
    assert len(gw.head_sha()) == 40
    assert gw.head_message() == "init"
    assert not gw.is_dirty()


def test_commit_stages_exact_paths(repo):
    gw = gateway(repo)
    first_sha = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    sha, already, summary = gw.commit("update a", ("a.txt",))
    assert not already
    assert sha != first_sha
    assert gw.head_message() == "update a"
    assert "a.txt" in summary  # staged diff summary captured pre-commit
    assert not gw.is_dirty()


def test_commit_requires_paths(repo):
    (repo / "a.txt").write_text("two\n")
    with pytest.raises(GitCommandError):
        gateway(repo).commit("update a", ())


def test_commit_leaves_unapproved_dirty_files_alone(repo):
    gw = gateway(repo)
    (repo / "a.txt").write_text("two\n")
    (repo / "unrelated.txt").write_text("human work in progress\n")
    gw.commit("update a", ("a.txt",))
    dirty = "".join(gw.dirty_files())
    assert "unrelated.txt" in dirty
    assert "a.txt" not in dirty


def test_commit_refuses_preexisting_index_entries(repo):
    gw = gateway(repo)
    (repo / "a.txt").write_text("two\n")
    (repo / "sneaky.txt").write_text("already staged by someone\n")
    run_git(repo, "add", "sneaky.txt")  # index dirtied outside the loop
    with pytest.raises(GitCommandError) as excinfo:
        gw.commit("update a", ("a.txt",))
    assert "sneaky.txt" in str(excinfo.value)
    # the unapproved entry was unstaged again, nothing was committed
    assert gw.head_message() == "init"
    assert "sneaky.txt" not in gw.staged_paths()


def test_commit_is_idempotent_after_crash(repo):
    gw = gateway(repo)
    (repo / "a.txt").write_text("two\n")
    sha, _, _ = gw.commit("update a", ("a.txt",))
    # Re-dispatch of the same directive after a crash — even with OTHER files
    # still dirty in the tree.
    (repo / "unrelated.txt").write_text("other work\n")
    sha2, already, _ = gw.commit("update a", ("a.txt",))
    assert already
    assert sha2 == sha


def test_commit_clean_tree_with_other_message_fails(repo):
    with pytest.raises(GitCommandError):
        gateway(repo).commit("something else entirely", ("a.txt",))


def test_commit_handles_deletions_and_untracked(repo):
    gw = gateway(repo)
    (repo / "new.txt").write_text("brand new\n")
    (repo / "a.txt").unlink()
    sha, already, _ = gw.commit("replace a with new", ("a.txt", "new.txt"))
    assert not already
    assert not gw.is_dirty()
    assert "new.txt" in run_git(repo, "show", "--stat", "--format=%H")


def test_commit_handles_worktree_rename(repo):
    gw = gateway(repo)
    (repo / "a.txt").rename(repo / "b.txt")
    sha, already, _ = gw.commit("rename a to b", ("a.txt", "b.txt"))
    assert not already
    assert not gw.is_dirty()


def test_push_to_bare_remote(repo, tmp_path):
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    run_git(repo, "remote", "add", "origin", str(bare))
    gw = gateway(repo)
    gw.push()
    remote_head = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_head == gw.head_sha()


def test_push_from_detached_head_fails(repo):
    run_git(repo, "checkout", "-q", "--detach")
    with pytest.raises(GitCommandError):
        gateway(repo).push()


def test_force_push_denied(repo):
    with pytest.raises(GitOperationDenied):
        gateway(repo)._git("push", "--force", "origin", "main")


def test_add_all_denied_at_the_gateway(repo):
    with pytest.raises(GitOperationDenied):
        gateway(repo)._git("add", "-A")


def test_denied_command_never_reaches_subprocess(tmp_path):
    calls = []

    def spy(cmd, **kwargs):
        calls.append(cmd)
        raise AssertionError("subprocess must not run for denied commands")

    gw = GitGateway(tmp_path, PolicyEngine(PolicyConfig()), runner=spy)
    with pytest.raises(GitOperationDenied):
        gw._git("reset", "--hard", "HEAD~1")
    with pytest.raises(GitOperationDenied):
        gw._git("clean", "-fd")
    with pytest.raises(GitOperationDenied):
        gw._git("add", "-A")
    assert calls == []
