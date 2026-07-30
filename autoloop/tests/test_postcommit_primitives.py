"""Produce-then-review primitives against real throwaway repos: honest sha
capture (never precomputed), plumbing-rendered range diffs that cannot run an
external diff driver or a textconv filter, NUL-delimited path handling,
`push_exact`'s pre-flight checks (F2/F5), the environment snapshot/verify
pair, and crash reconciliation (F8) via `reconcile_after_crash`.

Mirrors `test_git_gateway.py`'s repo fixture and `run_git`/`install_hook`
helpers rather than importing them, matching that file's own self-contained
style.
"""

import subprocess

import pytest

from autoloop import environment
from autoloop.errors import GitCommandError, StateCorruptError
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.worktask import (
    CommitIntent,
    IntentStore,
    Reconciliation,
    TaskExecution,
    TaskExecutionStore,
    reconcile_after_crash,
)


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


def install_hook(repo, name, body="#!/bin/sh\nexit 0\n", executable=True):
    hook = repo / ".git" / "hooks" / name
    hook.write_text(body)
    if executable:
        hook.chmod(0o755)
    return hook


def make_intent(gw, task_id, base_sha, paths, message):
    return CommitIntent.create(task_id, "autoloop/" + task_id, base_sha, paths, message)


# ---- 1. candidate sha is read from HEAD, never predicted -------------------


def test_candidate_sha_equals_rev_parse_head_after_commit(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    store = IntentStore(tmp_path / "intents")
    sha, summary = gw.commit_and_capture("update a", ("a.txt",), store, intent)
    assert sha == run_git(repo, "rev-parse", "HEAD").strip()
    assert sha != base
    assert "a.txt" in summary


# ---- 2/3. a pre-commit hook is not authorized by prior review -------------


def test_hook_modification_appears_in_range_diff(repo, tmp_path):
    """`commit_and_capture` runs a NORMAL commit — hooks execute. The review
    step is `range_diff` reading the resulting commit's actual tree, so
    whatever the hook really did is what a reviewer sees, not what was
    approved before the hook ran."""
    install_hook(
        repo,
        "pre-commit",
        "#!/bin/sh\nprintf 'HOOK PAYLOAD\\n' > a.txt\ngit add a.txt\n",
    )
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("APPROVED\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    store = IntentStore(tmp_path / "intents")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), store, intent)
    diff = gw.range_diff(base, sha)
    assert "HOOK PAYLOAD" in diff
    assert "APPROVED" not in diff  # the hook's bytes are what was committed


def test_hook_added_path_is_revealed_by_commit_range_paths(repo, tmp_path):
    """A hook sneaking in an extra path is not blocked here (this is the
    produce half) but IS visible to whatever reviews the range afterward."""
    install_hook(
        repo,
        "pre-commit",
        "#!/bin/sh\nprintf 'sneaked\\n' > extra.txt\ngit add extra.txt\n",
    )
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    store = IntentStore(tmp_path / "intents")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), store, intent)
    changed = gw.commit_range_paths(base, sha)
    assert changed == {"a.txt", "extra.txt"}


# ---- 4. range_diff is scoped to the recorded candidate, not the branch tip -


def test_later_local_commit_does_not_change_recorded_range_diff(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    store = IntentStore(tmp_path / "intents")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), store, intent)
    before = gw.range_diff(base, sha)

    (repo / "unrelated.txt").write_text("more human work\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "later commit")

    after = gw.range_diff(base, sha)
    assert after == before
    assert "unrelated.txt" not in after


# ---- commit_list -------------------------------------------------------


def test_commit_list_is_oldest_first_with_subject_and_parents(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "first change\n\nwith a body")
    sha1, _ = gw.commit_and_capture(
        "first change\n\nwith a body", ("a.txt",), IntentStore(tmp_path / "intents"), intent
    )
    (repo / "a.txt").write_text("three\n")
    run_git(repo, "commit", "-a", "-q", "-m", "second change")
    sha2 = gw.head_sha()

    commits = gw.commit_list(base, sha2)
    assert [c["sha"] for c in commits] == [sha1, sha2]
    assert commits[0]["subject"] == "first change"
    assert commits[1]["subject"] == "second change"
    assert commits[0]["parents"] == [base]
    assert commits[1]["parents"] == [sha1]


def test_commit_list_empty_range_is_empty(repo):
    gw = gateway(repo)
    base = gw.head_sha()
    assert gw.commit_list(base, base) == []


# ---- 5-9. push_exact ---------------------------------------------------


def make_bare(tmp_path, name="bare.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def test_push_exact_publishes_only_the_candidate(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)

    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))
    landed = gw.push_exact("origin", sha, "refs/heads/main", ())
    assert landed == sha

    # a later local commit must NOT reach the remote
    (repo / "a.txt").write_text("three\n")
    run_git(repo, "commit", "-a", "-q", "-m", "later, unpushed")
    remote_head = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert remote_head == sha


def test_push_exact_is_idempotent(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))

    first = gw.push_exact("origin", sha, "refs/heads/main", ())
    second = gw.push_exact("origin", sha, "refs/heads/main", ())
    assert first == second == sha


def test_push_exact_rejects_non_fast_forward(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent1 = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha1, _ = gw.commit_and_capture(
        "update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent1
    )
    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))
    gw.push_exact("origin", sha1, "refs/heads/main", ())

    # Rewind local main back to base and create a DIVERGENT commit — remote
    # main is at sha1, this new commit is not a descendant of it.
    run_git(repo, "update-ref", "refs/heads/main", base)
    (repo / "a.txt").write_text("divergent\n")
    run_git(repo, "commit", "-a", "-q", "-m", "divergent")
    sha2 = gw.head_sha()

    with pytest.raises(GitCommandError):
        gw.push_exact("origin", sha2, "refs/heads/main", ())
    remote_head = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "refs/heads/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert remote_head == sha1  # unchanged


@pytest.mark.parametrize("protected_name", ["main", "master"])
def test_push_exact_refuses_protected_refs(repo, tmp_path, protected_name):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))
    with pytest.raises(GitCommandError):
        gw.push_exact("origin", sha, f"refs/heads/{protected_name}", ("main", "master"))


def test_push_exact_refuses_invalid_inputs(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))

    with pytest.raises(GitCommandError):
        gw.push_exact("origin", "not-a-sha", "refs/heads/feature", ())
    with pytest.raises(GitCommandError):
        gw.push_exact("origin", sha, "refs/heads/a/../b", ())
    with pytest.raises(GitCommandError):
        gw.push_exact("origin", sha, "refs/heads/+evil", ())

    # nothing landed anywhere on the remote
    empty = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref"], capture_output=True, text=True
    ).stdout
    assert empty.strip() == ""


def test_policy_rejects_plus_prefixed_push_refspec(repo):
    """Regression for F2: a `+`-prefixed refspec must be denied at the
    policy layer regardless of push's empty flag set."""
    verdict = PolicyEngine(PolicyConfig()).validate_git_command(
        ("push", "origin", "+" + "a" * 40 + ":refs/heads/main")
    )
    assert not verdict.allowed
    assert verdict.code == "git_push_force_prefix"


def test_policy_rejects_non_sha_push_refspec_source():
    verdict = PolicyEngine(PolicyConfig()).validate_git_command(
        ("push", "origin", "main:refs/heads/main")
    )
    assert not verdict.allowed
    assert verdict.code == "git_push_refspec_source"


def test_push_exact_refuses_when_pushurl_configured(repo, tmp_path):
    """`remote.<name>.pushurl` overrides the actual push destination while
    `remote.<name>.url` stays unchanged — verified empirically (pointing
    `url` at one bare repo and `pushurl` at a second sends the push to the
    second). `push_exact` must refuse on its mere presence."""
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    bare_url = make_bare(tmp_path, "bare-url.git")
    bare_pushurl = make_bare(tmp_path, "bare-pushurl.git")
    run_git(repo, "remote", "add", "origin", str(bare_url))
    run_git(repo, "config", "remote.origin.pushurl", str(bare_pushurl))

    with pytest.raises(GitCommandError) as excinfo:
        gw.push_exact("origin", sha, "refs/heads/main", ())
    assert "pushurl" in str(excinfo.value)
    for bare in (bare_url, bare_pushurl):
        empty = subprocess.run(
            ["git", "--git-dir", str(bare), "for-each-ref"], capture_output=True, text=True
        ).stdout
        assert empty.strip() == ""  # neither destination received anything


def test_push_exact_expected_url_mismatch_is_refused(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))

    with pytest.raises(GitCommandError) as excinfo:
        gw.push_exact(
            "origin", sha, "refs/heads/main", (), expected_url="https://not-the-real-remote/"
        )
    assert "expected" in str(excinfo.value)
    empty = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref"], capture_output=True, text=True
    ).stdout
    assert empty.strip() == ""  # refused before any network access

    # a matching expected_url is accepted
    landed = gw.push_exact("origin", sha, "refs/heads/main", (), expected_url=str(bare))
    assert landed == sha


def test_push_exact_refuses_when_insteadof_rule_present(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    bare = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(bare))
    run_git(repo, "config", "url.https://evil.example/.insteadOf", "https://github.com/")

    with pytest.raises(GitCommandError) as excinfo:
        gw.push_exact("origin", sha, "refs/heads/main", ())
    assert "insteadOf" in str(excinfo.value)
    empty = subprocess.run(
        ["git", "--git-dir", str(bare), "for-each-ref"], capture_output=True, text=True
    ).stdout
    assert empty.strip() == ""  # refused before any network access


# ---- 12. range_diff refuses rather than truncates --------------------------


def test_range_diff_refuses_above_byte_cap(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    gw.RANGE_DIFF_MAX_BYTES = 10  # instance override; the real diff exceeds this trivially
    with pytest.raises(GitCommandError) as excinfo:
        gw.range_diff(base, sha)
    assert "byte cap" in str(excinfo.value)
    with pytest.raises(GitCommandError):
        gw.range_diff_stat(base, sha)


# ---- 13. no external diff driver or textconv filter ever runs --------------


def test_range_diff_never_runs_external_diff_or_textconv(repo, tmp_path):
    """Two independent mechanisms, two independent controls.

    `diff.external`: a POSITIVE control using `git diff` (porcelain, which
    genuinely honours `diff.external` in this git version) proves the canary
    script actually works, then `range_diff` (built on `diff-tree`) is shown
    to never fire it. Verified separately: plain `git diff-tree -p`, even
    WITHOUT `--no-ext-diff`, never invokes `diff.external` either — `diff-tree`
    is plumbing that does not honour it regardless of the flag on this git
    version (2.39.5). So `--no-ext-diff` on `diff-tree` is belt-and-braces
    for this mechanism, not the thing actively closing a gap; the assertion
    below still guards against a future git version changing that.

    `textconv`: here the flag IS load-bearing for `diff-tree` specifically —
    `git diff-tree -p --textconv` (no `--no-textconv`) DOES invoke the
    configured textconv filter (verified empirically), so the positive
    control uses that exact command form, and `range_diff`'s `--no-textconv`
    is what keeps it from firing.
    """
    ext_canary = repo / "ext-diff-fired"
    ext_script = repo / "ext-diff.sh"
    ext_script.write_text(f"#!/bin/sh\ntouch '{ext_canary}'\nexit 0\n")
    ext_script.chmod(0o755)
    run_git(repo, "config", "diff.external", str(ext_script))

    textconv_canary = repo / "textconv-fired"
    textconv_script = repo / "textconv.sh"
    textconv_script.write_text(f"#!/bin/sh\ntouch '{textconv_canary}'\ncat \"$1\"\n")
    textconv_script.chmod(0o755)
    run_git(repo, "config", "diff.canary.textconv", str(textconv_script))
    (repo / ".gitattributes").write_text("*.bin diff=canary\n")
    (repo / "a.bin").write_bytes(b"one\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "add binary")

    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.bin").write_bytes(b"two\n")
    intent = make_intent(gw, "t1", base, ("a.bin",), "update a.bin")
    sha, _ = gw.commit_and_capture(
        "update a.bin", ("a.bin",), IntentStore(tmp_path / "intents"), intent
    )

    # Positive control 1: `git diff` (porcelain) really does invoke diff.external.
    subprocess.run(["git", "diff", base, sha], cwd=str(repo), capture_output=True, text=True)
    assert ext_canary.exists(), "positive control failed: diff.external never fired via `git diff`"
    ext_canary.unlink()

    # Positive control 2: `git diff-tree -p --textconv` really does invoke textconv.
    subprocess.run(
        ["git", "diff-tree", "-r", "-p", "--textconv", base, sha],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert textconv_canary.exists(), "positive control failed: textconv never fired via diff-tree --textconv"
    textconv_canary.unlink()

    # The actual assertion: our safety-flagged range_diff fires neither.
    gw.range_diff(base, sha)
    assert not ext_canary.exists()
    assert not textconv_canary.exists()


# ---- 14. spaces AND tabs round-trip through -z ------------------------------


def test_filenames_with_spaces_and_tabs_round_trip(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    weird = "weird name\twith a tab.txt"
    (repo / weird).write_text("content\n")
    intent = make_intent(gw, "t1", base, (weird,), "add a weird filename")
    sha, _ = gw.commit_and_capture(
        "add a weird filename", (weird,), IntentStore(tmp_path / "intents"), intent
    )
    changed = gw.commit_range_paths(base, sha)
    assert changed == {weird}


# ---- 15. reconcile_after_crash ----------------------------------------------


def test_reconcile_no_commit(repo):
    gw = gateway(repo)
    base = gw.head_sha()
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    assert reconcile_after_crash(intent, base, base, gw) == Reconciliation.NO_COMMIT


def test_reconcile_recoverable(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    (repo / "a.txt").write_text("two\n")
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    sha, _ = gw.commit_and_capture("update a", ("a.txt",), IntentStore(tmp_path / "intents"), intent)
    assert reconcile_after_crash(intent, sha, base, gw) == Reconciliation.RECOVERABLE


def test_reconcile_ambiguous_human_commit_same_parent_and_message_wrong_paths(repo):
    """The pinned scenario: identity, parent AND message all match what the
    loop would have produced, but the commit touches a path outside the plan
    — this must be AMBIGUOUS, not RECOVERABLE. Identity/message are never
    even inspected; only parent linkage + path-subset are."""
    gw = gateway(repo)
    base = gw.head_sha()
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    (repo / "a.txt").write_text("two\n")
    (repo / "other.txt").write_text("not part of the plan\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "update a")  # same message, same parent
    human_head = gw.head_sha()
    assert reconcile_after_crash(intent, human_head, base, gw) == Reconciliation.AMBIGUOUS


def test_reconcile_ambiguous_merge_commit(repo):
    gw = gateway(repo)
    base = gw.head_sha()
    run_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "b.txt").write_text("feature work\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "feature work")
    run_git(repo, "checkout", "-q", "main")
    run_git(repo, "-c", "commit.gpgsign=false", "merge", "--no-ff", "-q", "-m", "merge", "feature")
    merge_sha = gw.head_sha()
    intent = make_intent(gw, "t1", base, ("a.txt",), "update a")
    assert reconcile_after_crash(intent, merge_sha, base, gw) == Reconciliation.AMBIGUOUS


def test_reconcile_ambiguous_when_expected_parent_is_unrelated(repo):
    """`expected_parent_sha` in the intent does not even descend from
    `task_base_sha` — the intent cannot belong to this task's lineage, so
    nothing below is trusted."""
    gw = gateway(repo)
    base = gw.head_sha()
    run_git(repo, "checkout", "-q", "--orphan", "unrelated")
    run_git(repo, "rm", "-rf", "--cached", ".")
    (repo / "u.txt").write_text("unrelated history\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "unrelated root")
    unrelated_sha = gw.head_sha()
    run_git(repo, "checkout", "-q", "main")

    intent = make_intent(gw, "t1", unrelated_sha, ("a.txt",), "update a")
    assert reconcile_after_crash(intent, base, base, gw) == Reconciliation.AMBIGUOUS


# ---- 16. environment snapshot / verify --------------------------------------


def test_environment_flags_new_hook(repo):
    gw = gateway(repo)
    before = environment.snapshot(gw)
    install_hook(repo, "pre-commit")
    violations = environment.verify_unchanged(before, gw)
    assert any("hook" in v for v in violations)


def test_environment_flags_hookspath_change(repo):
    gw = gateway(repo)
    before = environment.snapshot(gw)
    (repo / "elsewhere").mkdir()
    run_git(repo, "config", "core.hooksPath", "elsewhere")
    violations = environment.verify_unchanged(before, gw)
    assert any("hooksPath" in v for v in violations)


def test_environment_flags_new_insteadof_rule(repo):
    gw = gateway(repo)
    before = environment.snapshot(gw)
    run_git(repo, "config", "url.https://evil.example/.insteadOf", "https://github.com/")
    violations = environment.verify_unchanged(before, gw)
    assert any("insteadOf" in v for v in violations)


def test_environment_unchanged_reports_nothing(repo):
    gw = gateway(repo)
    before = environment.snapshot(gw)
    assert environment.verify_unchanged(before, gw) == []


# ---- 17. corrupt records raise, never read as absent ------------------------


def test_corrupt_intent_record_raises(tmp_path):
    store = IntentStore(tmp_path / "intents")
    path = store._path("t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    with pytest.raises(StateCorruptError):
        store.load("t1")


def test_corrupt_task_execution_record_raises(tmp_path):
    store = TaskExecutionStore(tmp_path / "tasks")
    path = store._path("t1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json")
    with pytest.raises(StateCorruptError):
        store.load("t1")


def test_missing_record_is_none_not_an_error(tmp_path):
    assert IntentStore(tmp_path / "intents").load("nope") is None
    assert TaskExecutionStore(tmp_path / "tasks").load("nope") is None


# ---- store round-trips (sanity for the dataclasses themselves) -------------


def test_intent_store_round_trip_preserves_planned_paths_as_tuple(tmp_path):
    store = IntentStore(tmp_path / "intents")
    intent = CommitIntent.create("t1", "autoloop/t1", "a" * 40, ["b.txt", "a.txt"], "msg")
    store.save(intent)
    loaded = store.load("t1")
    assert loaded == intent
    assert isinstance(loaded.planned_paths, tuple)
    store.clear("t1")
    assert store.load("t1") is None


def test_task_execution_store_round_trip(tmp_path):
    store = TaskExecutionStore(tmp_path / "tasks")
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path="/tmp/wt",
        task_base_sha="a" * 40,
    )
    store.save(execution)
    loaded = store.load("t1")
    assert loaded == execution
    execution.candidate_sha = "b" * 40
    store.save(execution)
    assert store.load("t1").candidate_sha == "b" * 40


def test_commit_and_capture_refuses_when_head_moved_after_the_intent(tmp_path, repo):
    """A stale intent must fail closed BEFORE staging, not become an AMBIGUOUS
    reconciliation only discoverable after a crash."""
    gw_ = gateway(repo)
    intents = IntentStore(tmp_path / "intents")
    (repo / "a.txt").write_text("work\n", encoding="utf-8")
    intent = make_intent(gw_, "t1", gw_.head_sha(), ("a.txt",), "feat: a")
    # Someone else commits on this branch after the intent was recorded.
    (repo / "other.txt").write_text("theirs\n", encoding="utf-8")
    run_git(repo, "add", "other.txt")
    run_git(repo, "commit", "-q", "-m", "unrelated")

    with pytest.raises(GitCommandError, match="branch moved after the intent"):
        gw_.commit_and_capture("feat: a", ("a.txt",), intents, intent)

    # Nothing was staged and no commit was created.
    assert "a.txt" not in gw_.staged_paths()
    assert gw_.head_message().strip() == "unrelated"
