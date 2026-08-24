"""Autoloop M2: structural worker/publisher separation, real git throughout.

`worker_env.py` (the worker side — scrubbed environment, no-remote
`WorkerRepoManager`, `verify_worker_isolation`) and `publisher.py` (the
publisher side — a dedicated, hooks-controlled repository that is the only
path through which a candidate commit is ever made public) are each tested
standalone here, matching `test_postcommit_primitives.py`'s convention of
exercising primitives directly against real throwaway repos rather than only
through the orchestrator. A final section proves the orchestrator's
produce-then-review push (`Orchestrator._dispatch_task_push`) actually routes
through `Publisher` when one is supplied.

THREAT MODEL reminder (see `worker_env.py`/`publisher.py` module docstrings):
git and the OS are trusted; the process sandbox is the edge. What these tests
prove is that a worker cannot publish through ORDINARY (inherited/ambient)
git configuration, credentials, or hooks — not that a fully-cooperating
hostile subprocess with direct filesystem access to another repo's path is
somehow blocked (that is explicitly out of scope).
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.auto_merge import MergeDeferralStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.publisher import (
    Publisher,
    provision_publisher_repo,
    read_publisher_url_snapshot,
)
from autoloop.state import (
    TERMINAL_PHASES,
    LastResponse,
    LoopState,
    PendingRequest,
    Phase,
    StateStore,
)
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import (
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    preserve_execution,
)
from autoloop.worktree import WorktreeManager
from autoloop.worker_env import (
    WorkerRepoManager,
    describe_policy,
    verify_worker_isolation,
    worker_env,
    worker_repo_is_reusable,
)

URL = "https://chatgpt.com/c/test-conversation"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def gateway(root) -> GitGateway:
    return GitGateway(root, PolicyEngine(PolicyConfig()))


def make_bare(tmp_path, name="bare.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def install_hook(directory, name, body="#!/bin/sh\nexit 0\n", executable=True):
    hook = Path(directory) / name
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(body)
    if executable:
        hook.chmod(0o755)
    return hook


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


def make_worker(tmp_path, source_repo, base_sha, task_id="t1"):
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    return manager, manager.create(task_id, source_repo, base_sha)


def commit_in(worker_path, message, filename="a.txt", content="two\n"):
    (Path(worker_path) / filename).write_text(content)
    run_git(worker_path, "commit", "-q", "-am", message)
    return run_git(worker_path, "rev-parse", "HEAD").strip()


# =============================================================================
# 1. worker repo has NO remotes
# =============================================================================


def test_worker_repo_has_no_remotes(tmp_path, repo):
    policy = PolicyEngine(PolicyConfig())
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    wgit = worker.gateway(policy)
    assert wgit.config_get_regexp(r"^remote\..*\.url$") == ""
    assert verify_worker_isolation(wgit, worker.hooks_dir) == []
    # Belt and braces: read the raw config file too, not just through the
    # policy-gated gateway.
    raw = (Path(worker.path) / ".git" / "config").read_text()
    assert "[remote" not in raw


# =============================================================================
# 2. worker cannot discover a configured push url through INHERITED git
#    config (a global/system-ish url + insteadOf set OUTSIDE the repo)
# =============================================================================


def test_worker_env_ignores_inherited_global_insteadof_and_credential_helper(tmp_path, repo):
    """A poisoned `~/.gitconfig` (simulating an inherited/ambient config the
    calling process happens to have) sets both a credential helper and an
    `insteadOf` rewrite rule pointed at an attacker-controlled host. Without
    `worker_env()`, a `GitGateway` running under that `HOME` sees both.
    `worker_env()` must make them invisible."""
    policy = PolicyEngine(PolicyConfig())
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)

    poisoned_home = tmp_path / "poisoned_home"
    poisoned_home.mkdir()
    (poisoned_home / ".gitconfig").write_text(
        "[credential]\n\thelper = malicious-global-helper\n"
        '[url "https://evil.example/"]\n\tinsteadOf = https://github.com/\n'
    )
    leaky_env = dict(os.environ)
    leaky_env["HOME"] = str(poisoned_home)
    leaky_git = GitGateway(worker.path, policy, env=leaky_env)
    leaky_violations = verify_worker_isolation(leaky_git, worker.hooks_dir)
    assert any("credential" in v for v in leaky_violations)
    assert any("insteadOf" in v for v in leaky_violations)

    scrubbed_env = worker_env(base_env=leaky_env)  # same poisoned HOME, scrubbed on top
    protected_git = GitGateway(worker.path, policy, env=scrubbed_env)
    assert verify_worker_isolation(protected_git, worker.hooks_dir) == []


# =============================================================================
# 3. an active worker hook refuses the task
# =============================================================================


def test_active_worker_hook_is_reported_as_a_violation(tmp_path, repo):
    policy = PolicyEngine(PolicyConfig())
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    assert verify_worker_isolation(worker.gateway(policy), worker.hooks_dir) == []

    install_hook(worker.hooks_dir, "pre-commit")
    violations = verify_worker_isolation(worker.gateway(policy), worker.hooks_dir)
    assert any("active hook" in v and "pre-commit" in v for v in violations)


def test_worker_repo_create_refuses_a_pre_populated_hooks_dir(tmp_path, repo):
    base = gateway(repo).head_sha()
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    install_hook(tmp_path / "worker-hooks" / "t1", "pre-commit")
    with pytest.raises(GitCommandError, match="not empty"):
        manager.create("t1", repo, base)


def test_hooks_dir_redirection_is_what_makes_worker_hooks_inert(tmp_path, repo):
    """A hook sitting in the worker repo's DEFAULT `.git/hooks` (not the
    controlled, redirected dir) never runs, because `core.hooksPath` points
    elsewhere — same mechanism `Publisher` relies on (see test 9 below).
    Proven with a real commit, not just a config read."""
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    canary = worker.path / "canary-fired"
    install_hook(
        worker.path / ".git" / "hooks",
        "pre-commit",
        f"#!/bin/sh\ntouch '{canary}'\nexit 0\n",
    )
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    commit_in(worker.path, "real commit")
    assert not canary.exists()


# =============================================================================
# 4. worker process lacks inherited credential-helper config AND ssh-agent
#    variables
# =============================================================================


def test_worker_env_forces_git_config_nosystem_not_git_config_system():
    """Platform trap (verified empirically on Apple Git 2.39.5, macOS 26.2):
    `GIT_CONFIG_SYSTEM=/dev/null` does NOT suppress Apple's second,
    compiled-in system gitconfig
    (`/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig`,
    which sets `credential.helper=osxkeychain` on a stock toolchain install)
    — only `GIT_CONFIG_NOSYSTEM=1` does (confirmed via `git config --list
    --show-origin` under both). `worker_env()` must use the NOSYSTEM form,
    never `GIT_CONFIG_SYSTEM`."""
    env = worker_env(base_env={})
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_CONFIG_SYSTEM" not in env


def test_git_config_system_devnull_does_not_suppress_the_real_system_config(repo):
    """The live negative control for the platform trap above: a real
    subprocess, not just a dict comparison. `GIT_CONFIG_SYSTEM=/dev/null`
    (the WRONG fix) still leaks the ambient system credential helper;
    `worker_env()` (via `GIT_CONFIG_NOSYSTEM=1`) does not. Skips only if this
    machine has no ambient system/global credential.helper to demonstrate
    against — same precondition as the sibling test below."""
    wrong_fix_env = dict(os.environ)
    wrong_fix_env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    wrong_fix = subprocess.run(
        ["git", "config", "--get", "credential.helper"],
        cwd=str(repo), env=wrong_fix_env, capture_output=True, text=True,
    )
    if wrong_fix.returncode != 0 or not wrong_fix.stdout.strip():
        pytest.skip(
            "no ambient system/global credential.helper on this machine to "
            "demonstrate GIT_CONFIG_SYSTEM's failure to suppress it"
        )
    # The wrong fix really does leak it (this is the trap, reproduced live).
    assert wrong_fix.stdout.strip()

    correct_fix = subprocess.run(
        ["git", "config", "--get", "credential.helper"],
        cwd=str(repo), env=worker_env(), capture_output=True, text=True,
    )
    assert correct_fix.returncode != 0
    assert correct_fix.stdout.strip() == ""


def test_worker_env_removes_ssh_agent_and_git_ssh_vars():
    base = {
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "SSH_ASKPASS": "/usr/bin/ssh-askpass",
        "GIT_ASKPASS": "/usr/bin/git-askpass",
        "GIT_SSH": "/usr/bin/ssh",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
        "GIT_CONFIG_COUNT": "3",  # an unrelated parent-supplied GIT_CONFIG* var
        "PATH": "/usr/bin",
    }
    env = worker_env(base_env=base)
    for key in (
        "SSH_AUTH_SOCK",
        "SSH_ASKPASS",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_CONFIG_COUNT",
    ):
        assert key not in env
    assert env["PATH"] == "/usr/bin"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_credential_helper_resolves_to_nothing_under_worker_env_on_this_platform(repo):
    """Real subprocess, real ambient macOS system config — not a simulation.
    Positive control first (the ambient helper really is visible without
    scrubbing), then the actual claim (it is not, under `worker_env()`)."""
    ambient = subprocess.run(
        ["git", "config", "--get", "credential.helper"],
        cwd=str(repo), capture_output=True, text=True,
    )
    if ambient.returncode != 0 or not ambient.stdout.strip():
        pytest.skip(
            "no ambient system/global credential.helper on this machine to "
            "demonstrate the positive control against — see "
            "test_worker_env_ignores_inherited_global_insteadof_and_credential_helper "
            "for the portable (non-ambient) version of this proof"
        )
    scrubbed = subprocess.run(
        ["git", "config", "--get", "credential.helper"],
        cwd=str(repo), env=worker_env(), capture_output=True, text=True,
    )
    assert scrubbed.returncode != 0
    assert scrubbed.stdout.strip() == ""


def test_describe_policy_carries_no_secret_values(tmp_path, repo):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    info = describe_policy(worker)
    blob = repr(info)
    assert "osxkeychain" not in blob
    assert info["forced_env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert info["remotes_permitted_by_policy"] is False


# =============================================================================
# 5. worker cannot modify the publisher repository through its configured
#    paths — reframed as two separate, honest claims (a worker with direct
#    filesystem access to the publisher's path is out of scope; see the
#    module docstring)
# =============================================================================


def test_worker_planted_refs_and_objects_do_not_change_what_publish_sends(tmp_path, repo):
    policy = PolicyEngine(PolicyConfig())
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    publisher = Publisher(pub_path, "origin", policy)
    publisher.import_candidate(worker.path, candidate)

    # A worker-planted ref in the PUBLISHER pointing at something else. Under
    # the real threat model a worker should never reach this path at all —
    # this proves that EVEN IF it did, publish() still sends only the bound
    # candidate_sha, never something read off a ref.
    decoy_tree = run_git(worker.path, "rev-parse", "HEAD~0^{tree}").strip()
    decoy = run_git(
        pub_path, "commit-tree", decoy_tree, "-m", "planted by a worker"
    ).strip()
    run_git(pub_path, "update-ref", "refs/heads/evil-decoy", decoy)

    landed = publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    assert landed == candidate
    remote_head = run_git(upstream, "rev-parse", "refs/heads/autoloop/t1").strip()
    assert remote_head == candidate
    assert remote_head != decoy


def test_worker_config_carries_no_reference_to_the_publisher_path(tmp_path, repo):
    """NOT proven by "the worker's config is empty" alone — an empty string
    trivially contains no substring, which would make the path-absence
    assertion vacuous. A DECOY remote (pointing somewhere else entirely) is
    planted first, so the regex-reading mechanism genuinely has non-empty
    text to search; the real claim is that even with SOME config present,
    none of it names the publisher's path."""
    policy = PolicyEngine(PolicyConfig())
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")

    decoy = make_bare(tmp_path, "decoy.git")
    run_git(worker.path, "config", "remote.decoy.url", str(decoy))

    wgit = worker.gateway(policy)
    everything = "\n".join(
        [
            wgit.config_get_regexp(r"^remote\..*\.url$"),
            wgit.config_get_regexp(r"^remote\..*\.pushurl$"),
            wgit.config_get_regexp(r"^url\..*\.insteadof$"),
        ]
    )
    assert str(decoy) in everything  # the mechanism genuinely found something
    assert str(pub_path) not in everything  # but never the publisher's path


# =============================================================================
# 6. publisher imports EXACTLY candidate_sha; refuses a mismatched/absent
#    object
# =============================================================================


def publisher_for(tmp_path, source_repo_git, name="state") -> Publisher:
    """Provision + construct a `Publisher` whose remote url is copied from
    whatever `source_repo_git`'s `remote.origin.url` is ALREADY configured
    to (the caller is responsible for having set that up, e.g. via
    `run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))`,
    and for keeping its own reference to that bare repo to assert against)."""
    pub_path = provision_publisher_repo(tmp_path / name, source_repo_git, "origin")
    return Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


def test_publisher_imports_exactly_the_candidate_object(tmp_path, repo):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    publisher = publisher_for(tmp_path, gateway(repo))
    got = publisher.import_candidate(worker.path, candidate)
    assert got == candidate
    info = publisher._git.read_commit(candidate)
    assert info.get("tree")

    # repeating the import is harmless
    got_again = publisher.import_candidate(worker.path, candidate)
    assert got_again == candidate


def test_publisher_refuses_an_absent_object(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    publisher = publisher_for(tmp_path, gateway(repo))
    fake_sha = "a" * 40
    with pytest.raises(GitCommandError):
        publisher.import_candidate(repo, fake_sha)


def test_publisher_refuses_a_non_commit_object(tmp_path, repo):
    """A blob's sha is a well-formed 40-hex id and genuinely exists in the
    source repo — `import_candidate` must still refuse it, via
    `read_commit`'s own `cat-file commit` failing on a non-commit object."""
    blob_sha = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo), input="just a blob\n", capture_output=True, text=True, check=True,
    ).stdout.strip()
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    publisher = publisher_for(tmp_path, gateway(repo))
    with pytest.raises(GitCommandError):
        publisher.import_candidate(repo, blob_sha)


def test_publisher_import_candidate_refuses_a_malformed_sha(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    publisher = publisher_for(tmp_path, gateway(repo))
    with pytest.raises(GitCommandError):
        publisher.import_candidate(repo, "not-a-sha")


# =============================================================================
# 7. a later worker HEAD does not affect publication
# =============================================================================


def test_later_worker_head_does_not_affect_publication(tmp_path, repo):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")
    # Worker HEAD moves on AFTER the candidate was reviewed/bound.
    later = commit_in(worker.path, "later, unreviewed change", content="three\n")
    assert later != candidate
    assert run_git(worker.path, "rev-parse", "HEAD").strip() == later

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))
    publisher.import_candidate(worker.path, candidate)
    landed = publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    assert landed == candidate

    remote_head = run_git(upstream, "rev-parse", "refs/heads/autoloop/t1").strip()
    assert remote_head == candidate
    # The publisher never even fetched the later commit.
    got_later = subprocess.run(
        ["git", "cat-file", "-t", later], cwd=str(publisher.repo_root),
        capture_output=True, text=True,
    )
    assert got_later.returncode != 0


# =============================================================================
# 8. publisher refuses multiple urls, pushurl, mirror, followTags, url
#    rewrites
# =============================================================================


def test_publisher_refuses_multiple_configured_urls(tmp_path, repo):
    one, two = make_bare(tmp_path, "one.git"), make_bare(tmp_path, "two.git")
    run_git(repo, "remote", "add", "origin", str(one))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    run_git(pub_path, "config", "--add", "remote.origin.url", str(two))
    with pytest.raises(GitCommandError, match="configured urls"):
        Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


def test_publisher_refuses_pushurl(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    run_git(pub_path, "config", "remote.origin.pushurl", str(make_bare(tmp_path, "evil.git")))
    with pytest.raises(GitCommandError, match="pushurl"):
        Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


def test_publisher_refuses_mirror(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    run_git(pub_path, "config", "remote.origin.mirror", "true")
    with pytest.raises(GitCommandError, match="mirror"):
        Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


def test_publisher_refuses_follow_tags(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    run_git(pub_path, "config", "push.followTags", "true")
    with pytest.raises(GitCommandError, match="followTags"):
        Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


def test_publisher_refuses_insteadof_rewrite(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    run_git(pub_path, "config", "url.https://evil.example/.insteadOf", "https://github.com/")
    with pytest.raises(GitCommandError, match="insteadOf"):
        Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


# =============================================================================
# 9. publisher runs NO hooks
# =============================================================================


def test_publisher_ignores_a_hook_in_the_default_non_effective_dir(tmp_path, repo):
    """The canary sits in the publisher bare repo's DEFAULT `.git/hooks` (a
    bare repo's hooks dir is just `hooks/` at its root) — NOT the controlled
    dir `core.hooksPath` redirects to. `Publisher` must not refuse
    construction (the EFFECTIVE dir is still empty) and a real push must
    never fire it."""
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))

    canary = tmp_path / "canary-fired"
    install_hook(
        publisher.repo_root / "hooks",
        "pre-push",
        f"#!/bin/sh\ntouch '{canary}'\nexit 0\n",
    )
    publisher.import_candidate(worker.path, candidate)
    landed = publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    assert landed == candidate
    assert not canary.exists()
    assert run_git(upstream, "rev-parse", "refs/heads/autoloop/t1").strip() == candidate


def test_publisher_refuses_construction_with_a_hook_in_the_effective_dir(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", str(make_bare(tmp_path)))
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    install_hook(tmp_path / "state" / "publisher-hooks", "pre-push")
    with pytest.raises(GitCommandError, match="not empty"):
        Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))


# =============================================================================
# 10. protected branch and force refspec refuse
# =============================================================================


def test_publisher_refuses_a_protected_branch(tmp_path, repo):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))
    publisher.import_candidate(worker.path, candidate)
    with pytest.raises(GitCommandError):
        publisher.publish(candidate, "refs/heads/main", ("main", "master"))
    assert run_git(upstream, "for-each-ref").strip() == ""


def test_publisher_refuses_a_force_refspec_shape(tmp_path, repo):
    """`publish()` never lets a caller construct the pushed refspec itself —
    `dest_ref` must be a plain `refs/heads/<name>`. A `+`-prefixed or `..`
    dest_ref is refused the same way `push_exact`'s own F2 checks refuse it
    (reused, not reimplemented)."""
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))
    publisher.import_candidate(worker.path, candidate)
    with pytest.raises(GitCommandError):
        publisher.publish(candidate, "refs/heads/+evil", ())
    with pytest.raises(GitCommandError):
        publisher.publish(candidate, "refs/heads/a/../b", ())
    assert run_git(upstream, "for-each-ref").strip() == ""


# =============================================================================
# 11. repeated identical push is idempotent
# =============================================================================


def test_publisher_publish_is_idempotent(tmp_path, repo):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))
    publisher.import_candidate(worker.path, candidate)
    first = publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    second = publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    assert first == second == candidate
    assert run_git(upstream, "rev-parse", "refs/heads/autoloop/t1").strip() == candidate


# =============================================================================
# 12. remote divergence refuses (non-fast-forward), never force
# =============================================================================


def test_publisher_refuses_non_fast_forward(tmp_path, repo):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate1 = commit_in(worker.path, "round 1")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))
    publisher.import_candidate(worker.path, candidate1)
    publisher.publish(candidate1, "refs/heads/autoloop/t1", ())

    # A DIFFERENT commit, not a descendant of candidate1, claims the same
    # dest_ref first (simulating a sibling process racing this one).
    run_git(worker.path, "update-ref", "refs/heads/autoloop/t1", base)
    run_git(worker.path, "checkout", "-q", "autoloop/t1")
    divergent = commit_in(worker.path, "divergent round", content="divergent\n")
    publisher.import_candidate(worker.path, divergent)
    with pytest.raises(GitCommandError):
        publisher.publish(divergent, "refs/heads/autoloop/t1", ())
    remote_head = run_git(upstream, "rev-parse", "refs/heads/autoloop/t1").strip()
    assert remote_head == candidate1  # unchanged, never force-updated


# =============================================================================
# 13. crash after a successful push reconciles from the remote sha, without
#     re-pushing
# =============================================================================


def test_crash_after_successful_push_reconciles_without_repushing(tmp_path, repo, monkeypatch):
    base = gateway(repo).head_sha()
    _manager, worker = make_worker(tmp_path, repo, base)
    run_git(worker.path, "config", "user.email", "t@example.com")
    run_git(worker.path, "config", "user.name", "T")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    candidate = commit_in(worker.path, "reviewed change")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))
    publisher = publisher_for(tmp_path, gateway(repo))
    publisher.import_candidate(worker.path, candidate)
    landed = publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    assert landed == candidate

    # "Crash" recovery: read the remote ref directly (never re-push blindly).
    recovered = publisher.remote_ref_sha("refs/heads/autoloop/t1")
    assert recovered == candidate

    def fail_if_called(*a, **kw):
        raise AssertionError("push_exact must not be called on a same-sha retry")

    monkeypatch.setattr(GitGateway, "push_exact", fail_if_called)
    # The caller's own recommended pattern: only publish if the remote
    # doesn't already show the candidate.
    if publisher.remote_ref_sha("refs/heads/autoloop/t1") != candidate:  # pragma: no cover
        publisher.publish(candidate, "refs/heads/autoloop/t1", ())
    assert run_git(upstream, "rev-parse", "refs/heads/autoloop/t1").strip() == candidate


# =============================================================================
# describe() redaction
# =============================================================================


def test_publisher_describe_redacts_userinfo_from_url(tmp_path, repo):
    run_git(repo, "remote", "add", "origin", "https://ghost:s3cr3t-token@example.com/org/repo.git")
    pub_path = provision_publisher_repo(tmp_path / "state", gateway(repo), "origin")
    publisher = Publisher(pub_path, "origin", PolicyEngine(PolicyConfig()))
    info = publisher.describe()
    assert "s3cr3t-token" not in info["remote_url_redacted"]
    assert "ghost" not in info["remote_url_redacted"]
    assert "example.com" in info["remote_url_redacted"]


# =============================================================================
# Orchestrator wiring: `_dispatch_task_push` routes through Publisher
# =============================================================================


class WritingExecutor:
    def __init__(self, worktrees_root, files):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files)

    def execute(self, directive, task):
        wt = self.worktrees_root / task.id
        for rel, content in self.files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary="did the work",
            details="details",
            validation="ok",
            changed_paths=tuple(self.files.keys()),
        )


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def build_orchestrator_with_publisher(tmp_path, task_id="t1"):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")

    upstream = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(upstream))

    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    # Matches `WritingExecutor(tmp_path / "worktrees", {"a.py": ...})` below.
    task = Task(id=task_id, title=f"Title {task_id}", description="desc", approved_paths=("a.py",))
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    manifest_store = ManifestStore(config.manifests_dir)

    publisher_state_dir = tmp_path / "publisher-state"
    publisher_repo_path = provision_publisher_repo(publisher_state_dir, git, "origin")
    publisher = Publisher(publisher_repo_path, "origin", PolicyEngine(config.policy))
    publisher_url_snapshot = read_publisher_url_snapshot(publisher_state_dir)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "print('hi')\n"})

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=manifest_store,
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=ok_validation,
        publisher=publisher,
        publisher_url_snapshot=publisher_url_snapshot,
    )
    return orch, repo_root, upstream, execution_store, task, publisher


def _object_present(repo_root, sha) -> bool:
    return subprocess.run(
        ["git", "cat-file", "-t", sha], cwd=str(repo_root), capture_output=True, text=True
    ).returncode == 0


def test_orchestrator_push_routes_through_publisher(tmp_path):
    """Not just "the ref landed on the shared upstream" (both branches would
    show that, since they push to the same bare repo) — the DISCRIMINATING
    fact is that only the publisher-routed path ever imports the candidate
    object into the publisher's OWN, separate repository. Deleting the
    `if self._publisher is not None:` branch in `_dispatch_task_push` would
    still land the ref correctly but would fail this test's object-presence
    assertion (verified: forcing `orch._publisher = None` here makes this
    exact assertion fail)."""
    orch, _repo_root, upstream, _execution_store, task, publisher = build_orchestrator_with_publisher(
        tmp_path
    )
    orch._dispatch_executor(Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task.id))
    orch._step_ready()

    req = orch.state.pending_request
    assert req is not None and req.postcommit is not None
    resp = LastResponse(
        request_id=req.request_id,
        raw="{}",
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    candidate = req.postcommit.candidate_sha

    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)

    assert orch.state.phase == Phase.READY.value
    remote_head = run_git(
        upstream, "rev-parse", f"refs/heads/{req.postcommit.task_branch}"
    ).strip()
    assert remote_head == candidate
    # The discriminating assertion: the publisher repo actually received the
    # object via import_candidate, proving THIS path (not the legacy one)
    # ran.
    assert _object_present(publisher.repo_root, candidate)


def test_orchestrator_push_without_publisher_still_uses_legacy_path(tmp_path):
    """Regression guard: `publisher=None` (every pre-M2 caller) must still
    behave exactly as before — publishing straight from the worktree, never
    touching the (still-provisioned, still-present) publisher repo."""
    orch, _repo_root, upstream, _execution_store, task, publisher = build_orchestrator_with_publisher(
        tmp_path
    )
    orch._publisher = None  # simulate a pre-M2 construction
    orch._dispatch_executor(Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task.id))
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id,
        raw="{}",
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    candidate = req.postcommit.candidate_sha
    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)
    assert orch.state.phase == Phase.READY.value
    # The discriminating assertion, this time in the negative: the publisher
    # repo (provisioned and available, but never plumbed into this dispatch
    # since `orch._publisher` was forced to None) never received the object.
    assert not _object_present(publisher.repo_root, candidate)
    remote_head = run_git(
        upstream, "rev-parse", f"refs/heads/{req.postcommit.task_branch}"
    ).strip()
    assert remote_head == candidate


# ---- integration: the orchestrator actually USES the isolated worker repo ---


def _ok_proc(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def test_orchestrator_task_repo_is_an_isolated_worker_repo(tmp_path):
    """Proves the worker side is WIRED, not merely available. The task's
    working repository must be a separate repo with no remote — not a linked
    worktree sharing `.git` (and therefore `origin`) with the main checkout."""
    from autoloop.config import AutoloopConfig, BrowserConfig
    from autoloop.contract import Decision, Directive
    from autoloop.executor import ExecutionOutcome
    from autoloop.manifest import ManifestStore
    from autoloop.orchestrator import Orchestrator
    from autoloop.state import LoopState, StateStore
    from autoloop.tasks import Task, TaskRegistry, TaskStore
    from autoloop.transcript import TranscriptLogger
    from autoloop.worker_env import WorkerRepoManager
    from autoloop.worktask import IntentStore, TaskExecutionStore

    main = tmp_path / "main"
    main.mkdir()
    run_git(main, "init", "-q", "-b", "main")
    run_git(main, "config", "user.email", "t@example.com")
    run_git(main, "config", "user.name", "T")
    (main / "README.md").write_text("hi\n")
    run_git(main, "add", "README.md")
    run_git(main, "commit", "-q", "-m", "init")
    # The MAIN checkout has a network remote. The worker must not inherit it.
    run_git(main, "remote", "add", "origin", "https://example.invalid/repo.git")

    git = GitGateway(main, PolicyEngine(PolicyConfig()))
    workers = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")

    class Writer:
        def execute(self, directive, task):
            repo = workers.path_for(task.id)
            (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
            return ExecutionOutcome(
                status="ok", summary="wrote feature.py",
                changed_paths=("feature.py",), validation="ok",
            )

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(config.browser.conversation_url)
    store.save(state)
    # Matches `Writer.execute`'s own `feature.py` write below.
    task = Task(id="t1", title="T", description="d", approved_paths=("feature.py",))
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    orch = Orchestrator(
        config=config, store=store, state=state,
        policy=PolicyEngine(config.policy), git=git, executor=Writer(),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: (_ for _ in ()).throw(AssertionError("no browser")),
        registry=registry, task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        execution_store=TaskExecutionStore(tmp_path / "ex"),
        intent_store=IntentStore(tmp_path / "in"),
        validation_runner=_ok_proc,
        worker_repos=workers,
    )

    orch._dispatch_executor(Directive(decision=Decision.IMPLEMENT, reason="go", task_id="t1"))

    execution = TaskExecutionStore(tmp_path / "ex").load("t1")
    assert execution is not None and execution.candidate_sha, "the task committed"
    worker_path = Path(execution.worktree_path)
    assert worker_path == workers.path_for("t1"), "task ran in the WORKER repo"

    # The isolation properties that matter, asserted on the real repo.
    wgit = GitGateway(worker_path, PolicyEngine(PolicyConfig()), env=worker_env())
    assert wgit.config_get_all("remote.origin.url") == [], "worker inherited no remote"
    assert verify_worker_isolation(wgit) == []
    assert not (worker_path / ".git").is_file(), "a real repo, not a linked worktree"


def test_orchestrator_refuses_a_worker_repo_with_an_active_hook(tmp_path):
    """An active hook anywhere in the worker's effective hooks path refuses the
    task outright — hooks are never treated as validation."""
    from autoloop.worker_env import WorkerRepoManager

    main = tmp_path / "main"
    main.mkdir()
    run_git(main, "init", "-q", "-b", "main")
    run_git(main, "config", "user.email", "t@example.com")
    run_git(main, "config", "user.name", "T")
    (main / "README.md").write_text("hi\n")
    run_git(main, "add", "README.md")
    run_git(main, "commit", "-q", "-m", "init")

    git = GitGateway(main, PolicyEngine(PolicyConfig()))
    workers = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    repo = workers.create("t1", git.repo_root, git.head_sha())

    hook = repo.hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    wgit = GitGateway(repo.path, PolicyEngine(PolicyConfig()), env=worker_env())
    violations = verify_worker_isolation(wgit)
    assert any("hook" in v.lower() for v in violations), violations


# =============================================================================
# 14. `shelve`: set a task aside WITHOUT discarding the round it holds
# =============================================================================
#
# `release` has exactly one meaning — "throw it back and REDO it" — and it
# enforces that meaning: `worktask.retire_execution` archives the execution
# record and quarantines the worker repo together, so the next dispatch starts
# from scratch. `shelve` is the sibling for the other legitimate case, and the
# ONE claim it makes is this: a single command returns an `in_progress` task to
# `pending` while leaving its execution record AND its worker repository
# exactly where they are, so the next dispatch RESUMES the recorded round —
# same candidate_sha, same review_round, same attempt_count.
#
# Observed 2026-08-20. dash-12 held a candidate of 5 files / 1160 insertions at
# review round 1 and its reviewer asked, in as many words, to "resume the
# existing dash-12 worker repository and preserve its partial implementation;
# do not restart the task". It was ALSO stranded `in_progress`, which
# `next_ready()` skips — and while stranded its unpublished candidate held the
# merge window shut on base-02, dash-14 and val-02 for six hours. `release` was
# the wrong tool and no right one existed, so `tasks.json` was edited by hand,
# with the loop stopped, three times in one night.
#
# These tests live beside the worker-repo primitives on purpose: the claim is
# only checkable against a REAL worker repository, because the resume decision
# is `worker_env.worker_repo_is_reusable`, which shells out to
# `git rev-parse --show-toplevel` and `git branch --show-current`. A stubbed
# worker would let a shelve that quietly broke the resume path pass.


class _ShelveCheckout:
    """The least a checkout has to answer for `cli._merge_window_blockers`.

    Injected through `cli._window_git` — the seam that function documents — so
    these tests never touch a network and never depend on the cwd being a git
    repository. It knows no commits by default, so `is_descendant` refuses
    exactly as real git refuses about a sha it does not hold, which is what
    keeps a record bound to an unresolvable base blocking the window by the
    fail-closed route rather than by accident.
    """

    def __init__(self, head="head1234", commits=()):
        self._head = head
        self.commits = set(commits)

    def head_sha(self):
        return self._head

    def is_descendant(self, candidate, base):
        for oid in (candidate, base):
            if oid not in self.commits:
                raise GitCommandError("merge-base", f"{oid}: not a valid object name")
        return False

    def read_commit(self, oid):
        if oid not in self.commits:
            raise GitCommandError("cat-file", f"{oid}: bad file")
        return {"tree": "t", "parents": [], "message": ""}

    def object_exists(self, oid):
        return oid in self.commits

    def remote_ref_sha(self, remote, dest_ref):
        return ""


def _shelve_config(tmp_path):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        # Outside `state_dir`, like production: `quarantine/` is a SIBLING of
        # `workers_root`, and a release has to be able to find it there.
        workers_root=tmp_path / "outside" / "workers",
    )


def _stranded_round(
    tmp_path,
    source_repo,
    *,
    task_id="dash-12",
    candidate="c" * 40,
    review_round=1,
    attempt_count=2,
    extra_tasks=(),
):
    """A task interrupted mid-round, in the shape `release` and `shelve` both
    find it: `in_progress` in the registry, a REAL worker repo built by the same
    `WorkerRepoManager.create` a dispatch uses, and an execution record naming
    both plus the candidate a reviewer has already seen."""
    config = _shelve_config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)

    manager = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    base = gateway(source_repo).head_sha()
    worker = manager.create(task_id, source_repo, base)
    (worker.path / "half-done.txt").write_text("work in progress", encoding="utf-8")

    registry = TaskRegistry(
        [Task(id=task_id, title="t", description="d", approved_paths=["docs/A.md"]),
         *extra_tasks]
    )
    registry.mark_in_progress(task_id)
    store = TaskStore(config.tasks_file)
    store.save(registry)

    executions = TaskExecutionStore(config.executions_dir)
    executions.save(
        TaskExecution(
            task_id=task_id,
            task_branch=worker.branch,
            worktree_path=str(worker.path),
            task_base_sha=base,
            candidate_sha=candidate,
            review_round=review_round,
            attempt_count=attempt_count,
        )
    )
    return config, store, executions, worker


def _wire(monkeypatch, config, checkout=None):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    fake = checkout if checkout is not None else _ShelveCheckout()
    monkeypatch.setattr(cli, "_window_git", lambda _c: fake)
    return fake


def _save_session(config, **fields):
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    for key, value in fields.items():
        setattr(state, key, value)
    store.save(state)
    return store


def _shelve(task_id="dash-12"):
    return cli._cmd_shelve(argparse.Namespace(config=None, task_id=task_id))


# ---- the claim: pending again, and NEITHER half moved -----------------------


def test_shelve_returns_the_task_to_pending_and_moves_neither_half(
    tmp_path, repo, monkeypatch, capsys
):
    """The whole difference from `release`, in one test. The status moves so
    `next_ready` can see the task again; the execution record and the worker
    repository do not move at all, and the counters they carry are untouched."""
    config, store, executions, worker = _stranded_round(tmp_path, repo)
    _wire(monkeypatch, config)

    assert _shelve() == 0

    assert store.load().state_of("dash-12") is TaskState.READY

    kept = executions.load("dash-12")
    assert kept is not None, "the LIVE record must still be where a dispatch looks"
    assert kept.candidate_sha == "c" * 40
    assert kept.review_round == 1
    # NOT refunded. Preserving a round preserves its cost — only `release`
    # resets the budget, and it does so by archiving the record rather than by
    # editing a counter.
    assert kept.attempt_count == 2
    assert not (config.executions_dir / "archive").exists(), "nothing was archived"

    assert worker.path.is_dir(), "the worker repo must still be at its recorded path"
    assert (worker.path / "half-done.txt").read_text(encoding="utf-8") == "work in progress"
    assert not (config.workers_root.parent / "quarantine").exists(), "nothing was quarantined"

    out = capsys.readouterr().out
    assert "in_progress -> pending" in out
    assert "execution record KEPT" in out
    assert "worker repo KEPT" in out
    assert "not refunded" in out


def test_the_next_dispatch_resumes_the_shelved_round_rather_than_starting_over(
    tmp_path, repo, monkeypatch
):
    """The two facts `Orchestrator._dispatch_task_postcommit` actually branches
    on, asserted after a shelve — and asserted against the REAL probe, not a
    restatement of it.

    `execution = self._execution_store.load(task.id)` decides `resumed`, and
    `worker_repo_is_reusable(worktree_path, task_branch)` decides whether that
    worker is reused as it stands rather than recreated over. Both must hold,
    and the record must still carry the counters the displaced round spent."""
    config, _store, executions, worker = _stranded_round(tmp_path, repo)
    _wire(monkeypatch, config)

    assert _shelve() == 0

    resumed = executions.load("dash-12")
    assert resumed is not None, "no record means the dispatch takes the FIRST-dispatch branch"
    assert worker_repo_is_reusable(Path(resumed.worktree_path), resumed.task_branch), (
        "the three-fact reuse probe must pass, or the dispatch calls create() "
        "over a directory that is still there and refuses"
    )
    assert Path(resumed.worktree_path) == worker.path
    assert (resumed.candidate_sha, resumed.review_round, resumed.attempt_count) == (
        "c" * 40,
        1,
        2,
    )


def test_release_still_archives_all_three_and_leaves_nothing_to_resume(
    tmp_path, repo, monkeypatch, capsys
):
    """The contrast, and the regression guard: `release` is unchanged. Run
    against the IDENTICAL fixture, it fails every assertion the shelve test
    above makes — which is what makes those assertions discriminating rather
    than merely true."""
    config, store, executions, worker = _stranded_round(tmp_path, repo)
    _wire(monkeypatch, config)

    assert cli._cmd_release(argparse.Namespace(config=None, task_id="dash-12")) == 0

    assert store.load().state_of("dash-12") is TaskState.READY
    assert executions.load("dash-12") is None, "release archives the record"
    assert sorted((config.executions_dir / "archive").glob("dash-12-*.json"))
    assert not worker.path.exists(), "release quarantines the worker"
    quarantined = sorted((config.workers_root.parent / "quarantine").glob("dash-12-*"))
    assert quarantined and (quarantined[0] / "half-done.txt").exists()
    assert "kept, not deleted" in capsys.readouterr().out


def test_shelve_refuses_a_task_that_is_not_in_progress(tmp_path, repo, monkeypatch, capsys):
    """Narrow on exactly `release`'s terms: it can neither un-complete finished
    work nor launder a quarantine."""
    config, store, executions, _worker = _stranded_round(tmp_path, repo)
    registry = store.load()
    registry.release("dash-12")            # already back in the queue
    store.save(registry)
    _wire(monkeypatch, config)

    assert _shelve() == 1
    out = capsys.readouterr().out
    assert "not in progress" in out and "shelve" in out
    # and nothing was touched on the way to refusing
    assert executions.load("dash-12") is not None


def test_shelve_reaches_an_in_progress_task_whose_dependency_is_incomplete(
    tmp_path, repo, monkeypatch
):
    """`state_of` reports BLOCKED — not IN_PROGRESS — for an in-progress task
    with an unmet dependency, because the dependency test runs first. `release`
    asks `state_of` and so refuses exactly the task that is hardest to get
    back (`_displaced_work_exists` records the same reading for the same
    reason); `shelve` asks the STORED status, which `mark_in_progress` alone
    writes.

    It grants nothing: the row lands `pending` and `next_ready` still skips it
    until the dependency completes — honest, and selectable the moment it is."""
    config, store, _executions, _worker = _stranded_round(
        tmp_path,
        repo,
        extra_tasks=[Task(id="dep-01", title="d", description="d", approved_paths=["docs/C.md"])],
    )
    registry = store.load()
    # `set_depends_on` refuses an in-progress task (`_refuse_immutable`), so
    # build the on-disk shape the way it really arises: the edge existed while
    # the task was pending, the task was dispatched, and the dependency has not
    # completed. Assigning the status back directly is what `mark_in_progress`
    # wrote at dispatch.
    registry.release("dash-12")
    registry.set_depends_on("dash-12", ["dep-01"])
    registry.get("dash-12").status = "in_progress"
    store.save(registry)
    assert store.load().state_of("dash-12") is TaskState.BLOCKED
    _wire(monkeypatch, config)

    # `release` refuses it — unchanged, and the reason this widening exists.
    assert cli._cmd_release(argparse.Namespace(config=None, task_id="dash-12")) == 1
    assert store.load().get("dash-12").status == "in_progress"

    assert _shelve() == 0
    after = store.load()
    assert after.get("dash-12").status == "pending"
    assert after.state_of("dash-12") is TaskState.BLOCKED, (
        "still not selectable — the dependency has not landed, and shelving "
        "must not pretend otherwise"
    )
    assert after.next_ready() is None or after.next_ready().id != "dash-12"


# ---- failure mode 2: never shelve mid-submission -----------------------------


@pytest.mark.parametrize(
    "phase",
    [
        Phase.DELIVERING,
        Phase.SUBMITTING,
        Phase.SUBMISSION_UNCONFIRMED,
        Phase.SUBMISSION_REJECTED,
        Phase.AWAITING,
    ],
)
def test_shelving_is_refused_while_a_review_packet_is_outstanding(
    tmp_path, repo, monkeypatch, capsys, phase
):
    """Exiting at any of these strands a packet whose acceptance nobody can
    establish afterwards. Refused OUTRIGHT — and refused before anything is
    written, so the task is exactly as it was found."""
    config, store, executions, worker = _stranded_round(tmp_path, repo)
    _save_session(config, phase=phase.value, current_task={"task_id": "dash-12"})
    _wire(monkeypatch, config)

    assert _shelve() == 1

    assert store.load().state_of("dash-12") is TaskState.IN_PROGRESS, (
        "a refusal must leave the status alone"
    )
    assert executions.load("dash-12") is not None
    assert worker.path.is_dir()
    out = capsys.readouterr().out
    assert "was NOT shelved" in out and phase.value in out
    # The session is untouched too: still that phase, still naming the task.
    saved = StateStore(config.state_file).load()
    assert saved.phase == phase.value
    assert saved.current_task == {"task_id": "dash-12"}


def test_shelving_is_refused_while_a_request_is_still_pending_in_ready(
    tmp_path, repo, monkeypatch, capsys
):
    """A request OUTLIVES its own phase, which is why
    `orchestrator._at_round_boundary` checks `pending_request` separately from
    the phase — a packet answered and not yet consumed is still one this loop
    owes something to. The phase here is the otherwise-safe `ready`."""
    config, store, _executions, _worker = _stranded_round(tmp_path, repo)
    _save_session(
        config,
        phase=Phase.READY.value,
        current_task={"task_id": "dash-12"},
        pending_request=PendingRequest(request_id="alr-abcdef12-0007", payload="p"),
    )
    _wire(monkeypatch, config)

    assert _shelve() == 1
    assert store.load().state_of("dash-12") is TaskState.IN_PROGRESS
    assert "alr-abcdef12-0007" in capsys.readouterr().out


def test_shelving_is_refused_when_the_session_state_cannot_be_read(
    tmp_path, repo, monkeypatch, capsys
):
    """FAIL CLOSED, and this is the one that matters most. An unreadable
    session is exactly the one in which "is a review packet outstanding?"
    cannot be answered — and answering it "no" by default is the check
    silently passing when what it needs is absent."""
    config, store, executions, _worker = _stranded_round(tmp_path, repo)
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.state_file.write_text("{not json at all", encoding="utf-8")
    _wire(monkeypatch, config)

    assert _shelve() == 1
    assert store.load().state_of("dash-12") is TaskState.IN_PROGRESS
    assert executions.load("dash-12") is not None
    out = capsys.readouterr().out
    assert "could not be read" in out and "refusing" in out


def test_shelving_is_refused_when_the_session_is_parked_on_the_task(
    tmp_path, repo, monkeypatch, capsys
):
    """A `needs_user` park OWNS its task: the next continuous iteration runs
    `_handle_parked_task`, which quarantines `park_task_id`. Shelving on top of
    one would be silently undone an iteration later — the task would come back
    `blocked`, not `pending`."""
    config, store, _executions, _worker = _stranded_round(tmp_path, repo)
    _save_session(
        config,
        phase=Phase.NEEDS_USER.value,
        park_kind="task_fatal",
        park_task_id="dash-12",
    )
    _wire(monkeypatch, config)

    assert _shelve() == 1
    assert store.load().state_of("dash-12") is TaskState.IN_PROGRESS
    assert "needs_user" in capsys.readouterr().out


def test_shelving_is_refused_when_the_phase_is_unrecognised(
    tmp_path, repo, monkeypatch, capsys
):
    """The other fail-open shape: a phase this build does not know is precisely
    the one whose packet status cannot be decided, so it refuses rather than
    falling through the `in PACKET_OUTSTANDING_PHASES` test as safe."""
    config, store, _executions, _worker = _stranded_round(tmp_path, repo)
    store_state = _save_session(config, phase=Phase.READY.value)
    raw = config.state_file.read_text(encoding="utf-8")
    config.state_file.write_text(
        raw.replace('"phase": "ready"', '"phase": "teleporting"'), encoding="utf-8"
    )
    assert store_state.load().phase == "teleporting"
    _wire(monkeypatch, config)

    assert _shelve() == 1
    assert store.load().state_of("dash-12") is TaskState.IN_PROGRESS
    assert "unrecognised phase" in capsys.readouterr().out


# ---- failure mode 1: the session must stop dragging the loop back ------------


def test_shelving_detaches_the_session_so_the_loop_can_select_something_else(
    tmp_path, repo, monkeypatch, capsys
):
    """Observed on dash-09: flipping a status to pending redirects nothing on
    its own, because `_run_continuous` resumes a saved session in a NON-terminal
    phase BEFORE it ever consults `next_ready()`. The loop restarted straight
    back onto the task and only `reset --yes` broke the pull.

    Asserted against the LITERAL condition that branch uses
    (`Phase(state.phase) not in TERMINAL_PHASES`), not a paraphrase of it."""
    config, _store, _executions, _worker = _stranded_round(
        tmp_path,
        repo,
        extra_tasks=[Task(id="other-01", title="o", description="d", approved_paths=["docs/B.md"])],
    )
    state_store = _save_session(
        config,
        phase=Phase.READY.value,
        current_task={"task_id": "dash-12"},
        task_execution={"task_id": "dash-12"},
        outbox="a packet about dash-12",
        outbox_diff="a diff",
    )
    _wire(monkeypatch, config)

    assert _shelve() == 0

    after = state_store.load()
    assert Phase(after.phase) in TERMINAL_PHASES, (
        "a non-terminal phase is exactly what pulls the loop straight back"
    )
    assert after.phase == Phase.STOPPED.value
    assert after.stop_kind == cli.SHELVE_STOP_KIND
    assert "dash-12" in after.stop_reason
    assert after.current_task is None and after.task_execution is None
    # The queued packet was about the round that just went back to the queue.
    assert after.outbox is None and after.outbox_diff is None
    assert "detached from dash-12" in capsys.readouterr().out


def test_the_shelve_stop_kind_is_not_mistaken_for_a_fault_or_a_preemption(
    tmp_path, repo, monkeypatch
):
    """Every reader of `stop_kind` gates on the POSITIVE value it wants, so a
    new value has to be checked against those readers rather than assumed
    inert. `_is_fault_stop` True would STOP the continuous loop — the opposite
    of what shelving is for — and `_is_preemption_stop` True would print a
    displacement that never happened."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    state_store = _save_session(
        config, phase=Phase.READY.value, current_task={"task_id": "dash-12"}
    )
    _wire(monkeypatch, config)

    assert _shelve() == 0

    after = state_store.load()
    assert not cli._is_fault_stop(after)
    assert not cli._is_preemption_stop(after)


def test_a_session_about_another_task_is_left_exactly_as_it_was(
    tmp_path, repo, monkeypatch, capsys
):
    """The detach is task-scoped. A session running something else needs no
    detaching, and rewriting it would end a round that had nothing to do with
    the shelve."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    state_store = _save_session(
        config, phase=Phase.READY.value, current_task={"task_id": "other-99"},
        outbox="a packet about other-99",
    )
    _wire(monkeypatch, config)

    assert _shelve() == 0

    after = state_store.load()
    assert after.phase == Phase.READY.value
    assert after.current_task == {"task_id": "other-99"}
    assert after.outbox == "a packet about other-99"
    assert "does not name dash-12" in capsys.readouterr().out


# ---- what it must TELL the operator: the merge window it holds shut ----------


def test_the_merge_window_predicate_still_counts_a_preserved_candidate(
    tmp_path, repo, monkeypatch
):
    """The kept candidate is a REAL hazard — moving the head under it is what
    parks a task on `task_base_behind_head` — so shelving must NOT exempt it to
    make the window reopen. Asserted by calling the predicate itself, after the
    shelve, exactly as `auto_merge` and `merge_sweep` call it."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    checkout = _wire(monkeypatch, config)

    assert _shelve() == 0

    reasons, _notes = cli._merge_window_blockers(config, set(), checkout)
    assert any(r.startswith("task dash-12 has a candidate") for r in reasons), reasons


def test_a_shelved_task_with_an_unpublished_candidate_names_the_branches_waiting(
    tmp_path, repo, monkeypatch, capsys
):
    """The sentence nobody had on 2026-08-20. dash-12's preserved candidate held
    the window shut on four branches for six hours and nothing named the
    connection until the deferral records were opened by hand."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    deferrals = MergeDeferralStore(config.merge_deferrals_dir)
    for tid, ref in (("base-02", "refs/heads/autoloop/base-02"),
                     ("dash-14", "refs/heads/autoloop/dash-14")):
        deferrals.record(
            task_id=tid,
            candidate_sha="a" * 40,
            dest_ref=ref,
            base_sha="b" * 40,
            reason="merge window closed",
            now="2026-08-20T00:00:00+00:00",
        )
    _wire(monkeypatch, config)

    assert _shelve() == 0

    out = capsys.readouterr().out
    assert "merge window SHUT" in out
    assert "the candidate you just preserved is what holds it" in out
    assert "base-02: refs/heads/autoloop/base-02" in out
    assert "dash-14: refs/heads/autoloop/dash-14" in out
    assert "2 published branch(es) are deferred and unmerged" in out


def test_the_window_report_is_unknown_rather_than_open_when_it_cannot_be_evaluated(
    tmp_path, repo, monkeypatch, capsys
):
    """The fail-open shape for the REPORT half: a predicate that raises must not
    print anything an operator could read as "merging is fine now"."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    _wire(monkeypatch, config)

    def boom(*_a, **_kw):
        raise GitCommandError("rev-parse", "the checkout is unreadable")

    monkeypatch.setattr(cli, "_merge_window_blockers", boom)

    assert _shelve() == 0

    out = capsys.readouterr().out
    assert "merge window: UNKNOWN" in out and "Treat it as SHUT" in out
    assert "merge window OPEN" not in out


def test_unreadable_merge_deferrals_report_unknown_rather_than_nothing_waiting(
    tmp_path, repo, monkeypatch, capsys
):
    """Two opposite facts an operator deciding whether to leave a task shelved
    must never see reported the same way: no deferral records exist, versus the
    deferral records could not be read."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    config.merge_deferrals_dir.mkdir(parents=True, exist_ok=True)
    (config.merge_deferrals_dir / "base-02.json").write_text("{oh dear", encoding="utf-8")
    _wire(monkeypatch, config)

    assert _shelve() == 0

    out = capsys.readouterr().out
    assert "waiting behind it: UNKNOWN" in out
    assert "nothing is known to be waiting" not in out


# ---- the attestation is CHECKED, not assumed ---------------------------------


def test_shelve_warns_when_the_preserved_round_would_not_actually_resume(
    tmp_path, repo, monkeypatch, capsys
):
    """A promise nothing checked is the fail-open version of this command. When
    the recorded worker does not pass the same three-fact probe the dispatch
    runs, saying "your round is kept" would be true about the files and false
    about what happens next — the next dispatch calls `create()` over a
    directory that is still there, and refuses."""
    config, store, executions, worker = _stranded_round(tmp_path, repo)
    # Point the record at a path that exists but is not a git repository — the
    # common shape of a half-written worker, and the one a mere `.exists()`
    # check would wave through.
    plain = tmp_path / "outside" / "not-a-repo"
    plain.mkdir(parents=True)
    record = executions.load("dash-12")
    record.worktree_path = str(plain)
    executions.save(record)
    _wire(monkeypatch, config)

    assert _shelve() == 0, "still shelved — nothing was moved, so nothing was lost"

    assert store.load().state_of("dash-12") is TaskState.READY
    assert executions.load("dash-12") is not None
    assert worker.path.is_dir()
    out = capsys.readouterr().out
    assert "will NOT resume this round" in out
    assert "three-fact reuse probe" in out


def test_preserve_execution_moves_nothing_and_reports_both_halves(tmp_path, repo):
    """The primitive on its own, against a real worker repo: it is a PURE READ.
    `retire_execution`'s sibling has to be provably incapable of the thing
    `retire_execution` exists to do."""
    config, _store, executions, worker = _stranded_round(tmp_path, repo)
    manager = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)

    before = sorted(p.name for p in config.executions_dir.iterdir())
    preserved = preserve_execution("dash-12", executions, manager)

    assert preserved.record_path == executions.path_for("dash-12")
    assert preserved.worker_path == worker.path
    assert preserved.resumable and preserved.obstacle == ""
    assert preserved.holds_a_candidate
    assert (preserved.candidate_sha, preserved.review_round, preserved.attempt_count) == (
        "c" * 40,
        1,
        2,
    )
    assert sorted(p.name for p in config.executions_dir.iterdir()) == before
    assert worker.path.is_dir()
    assert not (config.workers_root.parent / "quarantine").exists()


def test_preserve_execution_reports_an_unreadable_record_rather_than_raising(
    tmp_path, repo
):
    """Preserved AND unreadable are both true, and the caller has to say both:
    nothing moved the file, and the next dispatch will raise on it rather than
    resume. Collapsing that into a bare `resumable=False` would read exactly
    like a worker on the wrong branch, which has a different remedy."""
    config, _store, executions, _worker = _stranded_round(tmp_path, repo)
    executions.path_for("dash-12").write_text("{torn", encoding="utf-8")

    preserved = preserve_execution(
        "dash-12", executions, WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    )

    assert preserved.record_path == executions.path_for("dash-12")
    assert not preserved.resumable
    assert "unreadable" in preserved.obstacle
    assert not preserved.holds_a_candidate
    assert executions.path_for("dash-12").exists(), "a pure read moves nothing"


def test_preserve_execution_says_so_when_there_is_no_round_to_keep(tmp_path):
    """A task parked before it ever committed has neither half. Absence is not
    an error, and it must not be reported as a preserved round either."""
    config = _shelve_config(tmp_path)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    preserved = preserve_execution(
        "never-ran",
        TaskExecutionStore(config.executions_dir),
        WorkerRepoManager(config.workers_root, config.worker_hooks_dir),
    )
    assert preserved.record_path is None and preserved.worker_path is None
    assert not preserved.resumable and not preserved.holds_a_candidate
    assert "no execution record" in preserved.obstacle


def test_shelve_says_when_the_loop_will_pick_the_same_task_straight_back_up(
    tmp_path, repo, monkeypatch, capsys
):
    """Shelving returns a task to the QUEUE; it does not lower its priority. An
    operator who typed "shelve" and gets the same task back on the next
    iteration needs to have been told that would happen — the resume is correct
    and is the whole claim, but it is not what the word implies."""
    config, _store, _executions, _worker = _stranded_round(tmp_path, repo)
    _wire(monkeypatch, config)

    assert _shelve() == 0

    assert "is still what `next_ready()` returns" in capsys.readouterr().out
