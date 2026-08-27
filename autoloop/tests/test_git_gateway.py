"""Git gateway against real throwaway repos: reads, EXACT-path staging (no
`git add -A` anywhere), the immutable-tree `commit_adopted` path (hook
refusal, tree verification, compare-and-swap), pushes to a bare remote, and
policy denial before subprocess.

The legacy `commit()` method (plain `git commit` gated only on
`ChangeManifest` provenance) was removed 2026-07-30 — see docs/SECURITY.md
S21 — along with its tests here; `commit_adopted` below closed the same hole
a different way and is what the hook-attack tests pin."""

import subprocess

import pytest

from autoloop.errors import DiffTooLargeError, GitCommandError, GitOperationDenied
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


def test_object_exists_answers_from_the_object_database(repo):
    """True/False are the only two answers, and they come from `cat-file -e`'s
    exit code — 0 present, 1 absent. `read_commit` cannot make this
    distinction: it dies with the same status whether the object is missing,
    corrupt or unreadable, which is why `cli._candidate_is_retired` needs a
    separate probe before writing a record off."""
    gw = gateway(repo)

    assert gw.object_exists(gw.head_sha()) is True
    # Deliberately not the all-zeros sha: that is git's null OID and some
    # versions special-case it, which would test the one value git treats
    # differently rather than an ordinary absent object.
    assert gw.object_exists("c" * 40) is False, "well-formed, simply not here"
    with pytest.raises(GitCommandError):
        gw.read_commit("c" * 40)        # the read alone cannot say which it was


def test_object_exists_raises_rather_than_guessing_at_a_name_git_refuses(repo):
    """A die (rc 128) is not an answer about existence. Reporting it as
    "absent" is exactly the fail-open the merge-window gate must not have."""
    with pytest.raises(GitCommandError) as excinfo:
        gateway(repo).object_exists("not a valid object name")
    assert "not an answer" in str(excinfo.value)


def test_object_exists_admits_only_the_existence_flag():
    """`-e` prints nothing and returns only a status. Every other `cat-file`
    flag stays off the whitelist."""
    engine = PolicyEngine(PolicyConfig())
    assert engine.validate_git_command(("cat-file", "-e", "a" * 40)).allowed
    assert engine.validate_git_command(("cat-file", "commit", "a" * 40)).allowed
    assert not engine.validate_git_command(("cat-file", "-p", "a" * 40)).allowed
    assert not engine.validate_git_command(("cat-file", "--batch")).allowed


def test_the_size_cap_refusal_is_its_own_exception_type(repo):
    """"Too big to render" is the ONE `GitCommandError` here that says nothing
    is wrong with the repository, and a caller has to be able to act on that
    without also catching a torn repo (split-05).

    Both halves are asserted because both matter: it IS a `GitCommandError`, so
    every existing broad handler keeps catching it and nothing widened; and it
    is a distinct type, so a narrow clause placed ABOVE the broad one can route
    the cap refusal — and only the cap refusal — somewhere else.
    """
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n" * 200)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "grow a")
    sha = gw.head_sha()

    gw.RANGE_DIFF_MAX_BYTES = 10  # instance override; the real diff dwarfs it
    with pytest.raises(DiffTooLargeError) as excinfo:
        gw.range_diff(base, sha)
    assert "byte cap" in str(excinfo.value)
    assert isinstance(excinfo.value, GitCommandError), "existing handlers must still catch it"
    # The stat carries the SAME cap and the SAME type: a stat is usually orders
    # of magnitude smaller, but that is a size relationship, not a guarantee,
    # and a caller falling back to it must be able to see it refuse the same way.
    with pytest.raises(DiffTooLargeError):
        gw.range_diff_stat(base, sha)


def test_a_git_failure_that_is_not_about_size_is_not_the_size_exception(repo):
    """The distinction is only worth anything if it excludes something. An
    unresolvable sha is a genuine git failure: it raises `GitCommandError` and
    must NOT satisfy `except DiffTooLargeError`, or "this repository is damaged"
    would be routed as "this change is too big"."""
    gw = gateway(repo)
    with pytest.raises(GitCommandError) as excinfo:
        gw.range_diff("c" * 40, gw.head_sha())
    assert not isinstance(excinfo.value, DiffTooLargeError)


def test_gateway_has_no_ambient_push_method(repo):
    """`push()` was removed 2026-07-30 (pass 2b) — it pushed whatever the
    current branch tip happened to be, exactly the wrong-destination race M1
    exists to close. Publishing goes ONLY through `push_exact`'s explicit
    `<sha>:<dest_ref>` refspec now; see `test_postcommit_primitives.py` for
    its dedicated coverage (bare-remote publish, idempotency, protected-ref
    and non-fast-forward refusals, and more)."""
    assert not hasattr(GitGateway, "push")


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


# ---- immutable-tree commit path (adopted manifests) ------------------------
#
# `git commit` is not used here because its hooks can rewrite the index after
# any verification. These tests pin the attacks that proved it.

import hashlib  # noqa: E402 - grouped with the tree-path tests it serves


def install_hook(repo, name, body="#!/bin/sh\nexit 0\n", executable=True):
    hook = repo / ".git" / "hooks" / name
    hook.write_text(body)
    if executable:
        hook.chmod(0o755)
    return hook


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def real_verifier(gw, expected: dict[str, str], approved: set[str]):
    """A faithful stand-in for manifest.verify_tree_content."""

    def verify(tree, parent_tree):
        violations = []
        changed = gw.changed_paths(parent_tree, tree)
        if changed != approved:
            violations.append(f"changed set {sorted(changed)} != approved {sorted(approved)}")
        entries = gw.tree_entries(tree)
        for path, want in expected.items():
            mode, kind, oid = entries.get(path, ("", "", ""))
            if kind != "blob" or mode == "120000":
                violations.append(f"'{path}' is a symlink in the tree")
                continue
            if not oid or sha256_of(gw.blob_bytes(oid)) != want:
                violations.append(f"'{path}' tree content does not match approved bytes")
        return violations

    return verify


def test_hook_that_rewrites_an_approved_file_cannot_alter_committed_bytes(repo):
    """The reproduced attack: a pre-commit hook rewrote the approved file and
    the old `git commit` path committed the hook's bytes."""
    install_hook(
        repo,
        "pre-commit",
        "#!/bin/sh\nprintf 'HOOK PAYLOAD\\n' > a.txt\ngit add a.txt\n",
    )
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    verify = real_verifier(gw, {"a.txt": sha256_of(b"APPROVED\n")}, {"a.txt"})
    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted("adopted", ("a.txt",), verify)
    assert "active commit hook" in str(excinfo.value)
    assert "pre-commit" in str(excinfo.value)
    assert gw.head_message() == "init"                  # no commit created
    assert (repo / "a.txt").read_text() == "APPROVED\n"  # hook never ran


def test_hook_that_stages_an_extra_file_cannot_add_it(repo):
    install_hook(
        repo,
        "pre-commit",
        "#!/bin/sh\nprintf 'sneaked\\n' > extra.txt\ngit add extra.txt\n",
    )
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    with pytest.raises(GitCommandError):
        gw.commit_adopted(
            "adopted", ("a.txt",),
            real_verifier(gw, {"a.txt": sha256_of(b"APPROVED\n")}, {"a.txt"}),
        )
    assert not (repo / "extra.txt").exists()
    assert gw.head_message() == "init"


@pytest.mark.parametrize(
    "hook", ["pre-commit", "prepare-commit-msg", "commit-msg", "post-commit"]
)
def test_each_relevant_hook_fails_closed(repo, hook):
    install_hook(repo, hook)
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted("adopted", ("a.txt",), lambda t, p: [])
    assert hook in str(excinfo.value)
    assert "NOT executed and NOT bypassed" in str(excinfo.value)


def test_non_executable_and_sample_hooks_do_not_block(repo):
    install_hook(repo, "pre-commit", executable=False)      # present, not executable
    (repo / ".git" / "hooks" / "commit-msg.sample").write_text("#!/bin/sh\n")
    (repo / ".git" / "hooks" / "commit-msg.sample").chmod(0o755)
    directory, active = gateway(repo).active_commit_hooks()
    assert active == []
    assert directory.name == "hooks"


def test_executable_symlink_hook_counts_as_active(repo):
    real = repo / "hook-impl.sh"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    (repo / ".git" / "hooks" / "pre-commit").symlink_to(real)
    _, active = gateway(repo).active_commit_hooks()
    assert active == ["pre-commit"]


def test_core_hookspath_is_resolved(repo):
    custom = repo / "myhooks"
    custom.mkdir()
    run_git(repo, "config", "core.hooksPath", "myhooks")
    gw = gateway(repo)
    directory, active = gw.active_commit_hooks()
    assert directory.resolve() == custom.resolve()
    assert active == []                                    # configured but empty: allowed
    (custom / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (custom / "pre-commit").chmod(0o755)
    _, active = gw.active_commit_hooks()
    assert active == ["pre-commit"]                         # and detected there


def test_changed_branch_ref_makes_cas_fail(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    original_head = gw.head_sha()

    def verify_then_move_branch(tree, parent_tree):
        # Someone else advances the branch after the tree is verified.
        (repo / "other.txt").write_text("concurrent\n")
        run_git(repo, "add", "other.txt")
        run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "concurrent")
        return []

    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted("adopted", ("a.txt",), verify_then_move_branch)
    assert "update-ref" in str(excinfo.value)
    assert gw.head_message() == "concurrent"        # the other commit is intact
    assert gw.head_sha() != original_head


def test_index_mutation_after_write_tree_cannot_alter_the_commit(repo):
    """The tree is immutable: mutating the index after it is written changes
    nothing about what gets committed."""
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    approved_digest = sha256_of(b"APPROVED\n")

    def mutate_index_then_pass(tree, parent_tree):
        (repo / "a.txt").write_text("LATE PAYLOAD\n")
        run_git(repo, "add", "a.txt")               # index now holds the payload
        return []                                   # but the tree does not

    sha, summary, residual = gw.commit_adopted("adopted", ("a.txt",), mutate_index_then_pass)
    committed = subprocess.run(
        ["git", "show", "HEAD:a.txt"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert committed == "APPROVED\n"                # the verified bytes
    assert sha256_of(committed.encode()) == approved_digest
    assert "a.txt" in residual                      # the late edit is reported, not reset


def test_tree_with_an_unapproved_path_is_rejected(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    (repo / "extra.txt").write_text("not reviewed\n")
    run_git(repo, "add", "extra.txt")               # pre-staged, unapproved
    gw = gateway(repo)
    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted(
            "adopted", ("a.txt",),
            real_verifier(gw, {"a.txt": sha256_of(b"APPROVED\n")}, {"a.txt"}),
        )
    assert "extra.txt" in str(excinfo.value)
    assert gw.head_message() == "init"


def test_tree_with_changed_approved_content_is_rejected(repo):
    (repo / "a.txt").write_text("NOT WHAT WAS APPROVED\n")
    gw = gateway(repo)
    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted(
            "adopted", ("a.txt",),
            real_verifier(gw, {"a.txt": sha256_of(b"APPROVED\n")}, {"a.txt"}),
        )
    assert "does not match approved bytes" in str(excinfo.value)
    assert gw.head_message() == "init"


def test_final_commit_has_the_verified_tree_parent_and_message(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    parent = gw.head_sha()
    message = "adopted: exact message\n\nwith a body line"
    sha, _, _ = gw.commit_adopted(
        "adopted: exact message\n\nwith a body line", ("a.txt",),
        real_verifier(gw, {"a.txt": sha256_of(b"APPROVED\n")}, {"a.txt"}),
    )
    info = gw.read_commit(sha)
    assert info["parents"] == [parent]
    assert info["message"] == message + "\n"
    assert info["tree"] == gw.tree_of("HEAD")
    assert gw.head_sha() == sha
    mode, kind, oid = gw.tree_entries(info["tree"])["a.txt"]
    assert (mode, kind) == ("100644", "blob")
    assert sha256_of(gw.blob_bytes(oid)) == sha256_of(b"APPROVED\n")


def test_detached_head_is_refused(repo):
    run_git(repo, "checkout", "-q", "--detach")
    (repo / "a.txt").write_text("APPROVED\n")
    with pytest.raises(GitCommandError) as excinfo:
        gateway(repo).commit_adopted("adopted", ("a.txt",), lambda t, p: [])
    assert "symbolic branch HEAD" in str(excinfo.value)


def test_hook_installed_during_verification_is_refused(repo):
    """Double check: a hook appearing after the initial check must not apply to
    a commit this call is about to create."""
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)

    def install_hook_then_pass(tree, parent_tree):
        install_hook(repo, "pre-commit")
        return []

    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted("adopted", ("a.txt",), install_hook_then_pass)
    assert "hook state changed during verification" in str(excinfo.value)
    assert gw.head_message() == "init"


def test_hookspath_repointed_during_verification_is_refused(repo):
    (repo / "a.txt").write_text("APPROVED\n")
    (repo / "elsewhere").mkdir()
    gw = gateway(repo)

    def repoint(tree, parent_tree):
        run_git(repo, "config", "core.hooksPath", "elsewhere")
        return []

    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted("adopted", ("a.txt",), repoint)
    assert "hook state changed during verification" in str(excinfo.value)


def test_cas_failure_leaves_branch_intact_and_commit_unreachable(repo):
    """A lost CAS must not overwrite the competing update, and the candidate
    commit must not be reachable from the branch (it becomes a dangling object)."""
    (repo / "a.txt").write_text("APPROVED\n")
    gw = gateway(repo)
    original_head = gw.head_sha()
    created: list[str] = []

    def advance_branch(tree, parent_tree):
        (repo / "other.txt").write_text("concurrent\n")
        run_git(repo, "add", "other.txt")
        run_git(repo, "-c", "commit.gpgsign=false", "commit", "-qm", "concurrent")
        created.append(gw.head_sha())
        return []

    with pytest.raises(GitCommandError) as excinfo:
        gw.commit_adopted("adopted", ("a.txt",), advance_branch)
    message = str(excinfo.value)
    assert "compare-and-swap" in message and "NOT overwritten" in message
    assert "unreachable" in message                       # reported, not hidden

    # the competing update survives untouched
    assert gw.head_sha() == created[0] != original_head
    assert gw.head_message() == "concurrent"
    # and nothing on the branch carries the adopted message
    log = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(repo), capture_output=True, text=True
    ).stdout
    assert "adopted" not in log


def test_author_and_committer_identity_rule(repo):
    """`present and not split` = same Name <email>; timestamps may differ."""
    gw = gateway(repo)
    assert gw.ident_identity("A U Thor <a@b.c> 1700000000 +0000") == "A U Thor <a@b.c>"
    assert gw.ident_identity("A U Thor <a@b.c> 1700000000 +0000") == gw.ident_identity(
        "A U Thor <a@b.c> 1799999999 +0200"
    )
    assert gw.ident_identity("A <a@b.c> 1 +0000") != gw.ident_identity("B <b@b.c> 1 +0000")

    (repo / "a.txt").write_text("APPROVED\n")
    sha, _, _ = gw.commit_adopted("adopted", ("a.txt",), lambda t, p: [])
    info = gw.read_commit(sha)
    assert gw.ident_identity(info["author"]) == gw.ident_identity(info["committer"])
