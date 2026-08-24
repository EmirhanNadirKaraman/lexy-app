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
import json
import os
import subprocess
from pathlib import Path

import pytest

from autoloop import cli, worktask
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError, StateError, TaskGraphError
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
from autoloop.state import LastResponse, LoopState, Phase, StateStore
from autoloop.tasks import HOLD_ORIGIN_OPERATOR, Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecution, TaskExecutionStore
from autoloop.worktree import WorktreeManager
from autoloop.worker_env import (
    WorkerRepoManager,
    describe_policy,
    verify_worker_isolation,
    worker_env,
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
# 6. `release-blocked` — retiring a QUARANTINED task's execution (release-01)
# =============================================================================
#
# `release` is the only verb that retires an execution, and it refuses anything
# that is not in progress. A BLOCKED task holds exactly the same hazard: on
# 2026-08-20 dash-12 parked `task_fatal` on `attempt_count_ceiling` while its
# worker still held an unpublished candidate, and that ONE record was the whole
# reason `_merge_window_blockers` held the window shut on base-02, dash-14 and
# val-02. `release` refused it, the `unblock` its message named is not a CLI
# verb, and `answer` keeps the record by design — so the operator performed
# `worktask.retire_execution`'s two moves by hand with the loop stopped.
#
# These tests live beside the worker-repo primitives because the quarantine
# half of that retirement is `WorkerRepoManager.quarantine`, exercised here
# against real directories rather than a stub.


@pytest.fixture
def loop_config(tmp_path):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        # `quarantine/` is a SIBLING of `workers_root`, so keeping the root one
        # level down leaves both inside `tmp_path`.
        workers_root=tmp_path / "outside" / "workers",
    )


@pytest.fixture
def wired_cli(loop_config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda _p: loop_config)
    loop_config.state_dir.mkdir(parents=True, exist_ok=True)
    return loop_config


class _UnreadableCheckout:
    """A gateway that can answer nothing. `_merge_window_blockers` fails CLOSED
    on every unanswerable question, so this pins the counterfactual below to the
    records on disk rather than to whatever git repository the test runner
    happens to have as its working directory."""

    def head_sha(self):
        raise GitCommandError("rev-parse", "no checkout here")

    def remote_ref_sha(self, *_args):
        raise GitCommandError("ls-remote", "no checkout here")

    def read_commit(self, *_args):
        raise GitCommandError("cat-file", "no checkout here")

    def object_exists(self, *_args):
        raise GitCommandError("cat-file", "no checkout here")

    def is_descendant(self, *_args):
        raise GitCommandError("merge-base", "no checkout here")


def _quarantined_task(
    config,
    task_id="dash-12",
    *,
    candidate="c" * 40,
    worker=True,
    record=True,
    blocker=True,
    kind="task_fatal",
):
    """dash-12's shape: a task parked `task_fatal`, its worker repo still on
    disk with real work in it, and an execution record still claiming an
    unpublished candidate. Returns `(task_store, execution_store, blocker)`."""
    store = TaskStore(config.tasks_file)
    registry = TaskRegistry(
        [Task(id=task_id, title="t", description="d", approved_paths=["docs/A.md"])]
    )
    registry.mark_in_progress(task_id)
    registry.block(task_id, "attempt count ceiling")
    store.save(registry)

    workers = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    worker_path = workers.path_for(task_id)
    if worker:
        worker_path.mkdir(parents=True)
        (worker_path / "half-done.txt").write_text("work in progress", encoding="utf-8")

    executions = TaskExecutionStore(config.executions_dir)
    if record:
        executions.save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path=str(worker_path),
                task_base_sha="d2d4d6b8" + "0" * 32,
                candidate_sha=candidate,
                review_round=2,
                attempt_count=3,
            )
        )

    parked = None
    if blocker:
        parked = BlockerStore(config.blockers_dir).record(
            task_id=task_id,
            kind=kind,
            code="attempt_count_ceiling",
            question=f"{task_id} hit the attempt ceiling; what now?",
            detail="3 attempts",
            phase="executing",
            now="2026-08-20T00:00:00+00:00",
        )
    return store, executions, parked


def _discard(task_id="dash-12", reason="superseded by a hand-written fix"):
    return argparse.Namespace(config=None, task_id=task_id, reason=reason)


def test_release_blocked_retires_the_execution_and_returns_the_task(wired_cli, capsys):
    """THE claim: one supported command moves the worker to quarantine, the
    record to executions/archive, resolves the blocker with the recorded
    reason, and returns the task to the queue — no hand-editing."""
    store, executions, parked = _quarantined_task(wired_cli)
    workers = WorkerRepoManager(wired_cli.workers_root, wired_cli.worker_hooks_dir)

    assert cli._cmd_release_blocked(_discard()) == 0

    # 1. the task is back in the queue
    assert store.load().state_of("dash-12") is TaskState.READY
    assert store.load().next_ready().id == "dash-12"
    # 2. the worker moved, and its work went with it
    assert not workers.path_for("dash-12").exists()
    quarantined = sorted((wired_cli.workers_root.parent / "quarantine").glob("dash-12-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "half-done.txt").read_text(encoding="utf-8") == "work in progress"
    # 3. the record moved — out of the live directory, into the archive, kept
    assert executions.load("dash-12") is None
    archived = sorted((wired_cli.executions_dir / "archive").glob("dash-12-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8"))["candidate_sha"] == "c" * 40
    # 4. the blocker is closed WITH the reason, and never as an operator answer
    closed = BlockerStore(wired_cli.blockers_dir).load(parked.id)
    assert closed.resolved_at is not None
    assert closed.answer is None, "nobody answered this question — the work was discarded"
    assert "superseded by a hand-written fix" in closed.archived_reason
    assert closed.question == "dash-12 hit the attempt ceiling; what now?"
    out = capsys.readouterr().out
    assert "blocked -> pending" in out and "kept, not deleted" in out


def test_release_blocked_files_both_halves_under_one_label(wired_cli):
    """One operation, not two that happen to run next to each other: the
    quarantined worker and the archived record name the same attempt, so a
    human reading either finds the other half."""
    _quarantined_task(wired_cli, task_id="dash-14")

    assert cli._cmd_release_blocked(_discard("dash-14")) == 0

    quarantined = sorted((wired_cli.workers_root.parent / "quarantine").glob("dash-14-*"))
    archived = sorted((wired_cli.executions_dir / "archive").glob("dash-14-*.json"))
    assert len(quarantined) == 1 and len(archived) == 1
    worker_label = quarantined[0].name[len("dash-14-"):]
    record_label = archived[0].stem[len("dash-14-"):]
    assert worker_label == record_label, (
        f"the halves drifted apart: {worker_label!r} vs {record_label!r}"
    )
    assert worker_label.startswith("discarded-by-operator-")
    assert worker_label[len("discarded-by-operator-"):], "the label must be unique per call"


def test_release_blocked_never_uses_the_operator_reason_as_a_path_label(wired_cli):
    """`retire_execution` interpolates its `reason` into a directory name and a
    filename. The operator's `--reason` is free text, so it goes on the blocker
    record — where it is data — and never into either path."""
    _quarantined_task(wired_cli, task_id="val-02")

    assert cli._cmd_release_blocked(_discard("val-02", reason="../../etc/passwd  oops")) == 0

    quarantined = sorted((wired_cli.workers_root.parent / "quarantine").glob("val-02-*"))
    archived = sorted((wired_cli.executions_dir / "archive").glob("val-02-*.json"))
    assert len(quarantined) == 1 and len(archived) == 1
    assert quarantined[0].parent.name == "quarantine", "the move stayed inside quarantine/"
    assert archived[0].parent.name == "archive", "the record stayed inside archive/"
    for name in (quarantined[0].name, archived[0].name):
        assert "etc" not in name and ".." not in name and " " not in name
    # ...and the words themselves are not lost: they are on the record.
    closed = BlockerStore(wired_cli.blockers_dir).open_blockers()
    assert closed == []
    archived_blocker = BlockerStore(wired_cli.blockers_dir).all_blockers()[0]
    assert "../../etc/passwd" in archived_blocker.archived_reason


def test_release_blocked_requires_a_reason(wired_cli, capsys):
    """A blocker cleared with no recorded reason is the silent delete this
    command exists to avoid — and nothing may move before that is known."""
    store, executions, parked = _quarantined_task(wired_cli)

    assert cli._cmd_release_blocked(_discard(reason="   ")) == 1

    assert "must not be empty" in capsys.readouterr().out
    assert store.load().state_of("dash-12") is TaskState.BLOCKED_BY_OPERATOR
    assert executions.load("dash-12") is not None
    assert BlockerStore(wired_cli.blockers_dir).load(parked.id).resolved_at is None


def test_release_blocked_refuses_completed_work_and_an_operator_hold(wired_cli, capsys):
    """The two states a broad relaxation of `release` would have swallowed.
    Completed work cannot be un-completed, and an operator's own quarantine is
    not the loop's to discard."""
    store = TaskStore(wired_cli.tasks_file)
    registry = TaskRegistry(
        [
            Task(id="done-01", title="t", description="d", approved_paths=["docs/A.md"]),
            Task(id="held-01", title="t", description="d", approved_paths=["docs/A.md"]),
        ]
    )
    registry.mark_completed("done-01")
    registry.operator_block("held-01", "paused while I think")
    store.save(registry)
    executions = TaskExecutionStore(wired_cli.executions_dir)
    for task_id in ("done-01", "held-01"):
        executions.save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path="",
                task_base_sha="a" * 40,
                candidate_sha="b" * 40,
            )
        )

    assert cli._cmd_release_blocked(_discard("done-01")) == 1
    assert "cannot un-complete" in capsys.readouterr().out
    assert cli._cmd_release_blocked(_discard("held-01")) == 1
    assert "OPERATOR hold" in capsys.readouterr().out

    reloaded = store.load()
    assert reloaded.state_of("done-01") is TaskState.COMPLETED
    assert reloaded.get("held-01").hold_origin == HOLD_ORIGIN_OPERATOR
    assert reloaded.get("held-01").status == "blocked"
    # A refused command moves NOTHING: both records are still live.
    assert executions.load("done-01") is not None
    assert executions.load("held-01") is not None
    assert cli._cmd_release_blocked(_discard("nope-99")) == 1
    assert "no task with id" in capsys.readouterr().out


def test_release_blocked_refuses_a_loop_fatal_blocker(wired_cli, capsys):
    """A `loop_fatal` record is a LOOP-WIDE condition — a dirty checkout, an
    escaped write — that merely happened to be recorded while this task was in
    flight. Discarding one task's work is no evidence about it, so the command
    refuses rather than closing it and letting `start` proceed."""
    store, executions, parked = _quarantined_task(wired_cli, kind="loop_fatal")

    assert cli._cmd_release_blocked(_discard()) == 1

    out = capsys.readouterr().out
    assert "loop_fatal" in out and "Nothing changed." in out
    assert BlockerStore(wired_cli.blockers_dir).load(parked.id).resolved_at is None
    assert store.load().state_of("dash-12") is TaskState.BLOCKED_BY_OPERATOR
    assert executions.load("dash-12") is not None, "the retirement never started"


def test_release_blocked_leaves_the_blocker_open_when_the_worker_cannot_move(
    wired_cli, monkeypatch, capsys
):
    """Both halves move or neither does. A retirement that failed while the
    blocker was resolved would return the task to the queue with a stale record
    still holding the merge window shut — the exact defect `retire_execution`
    was written to prevent."""
    store, _executions, parked = _quarantined_task(wired_cli)

    def refuse(self, task_id, label):
        raise GitCommandError("mv", "quarantine destination is not writable")

    monkeypatch.setattr(WorkerRepoManager, "quarantine", refuse)

    assert cli._cmd_release_blocked(_discard()) == 1

    assert "still open" in capsys.readouterr().out
    assert BlockerStore(wired_cli.blockers_dir).load(parked.id).resolved_at is None
    assert store.load().state_of("dash-12") is TaskState.BLOCKED_BY_OPERATOR
    # The record went first and stays archived — the safe residue, since a
    # surviving RECORD is the silent failure and a surviving WORKER is the loud
    # one (the next dispatch refuses to create over it, naming the path).
    assert sorted((wired_cli.executions_dir / "archive").glob("dash-12-*.json"))
    assert WorkerRepoManager(
        wired_cli.workers_root, wired_cli.worker_hooks_dir
    ).path_for("dash-12").exists()


def test_release_blocked_frees_a_merge_window_a_bare_unblock_would_hold_shut(
    wired_cli, monkeypatch
):
    """Why the two halves cannot be split. Requeueing the task ALONE removes
    the terminal-state exemption its `blocked` status was getting, so the live
    record starts holding the window shut on everything else. Retiring the
    record is what makes the requeue safe."""
    store, _executions, _parked = _quarantined_task(wired_cli)
    monkeypatch.setattr(cli, "_window_git", lambda _config: _UnreadableCheckout())

    # While quarantined, the record is exempt (terminal registry state).
    assert cli._merge_window_blockers(wired_cli)[0] == []

    # THE COUNTERFACTUAL: unblock without retiring, exactly what the only
    # available commands could do, and the window shuts on dash-12.
    registry = store.load()
    registry.unblock("dash-12")
    store.save(registry)
    reasons, _notes = cli._merge_window_blockers(wired_cli)
    assert any("dash-12" in r for r in reasons), reasons

    # Put it back the way dash-12 actually was, and run the real command.
    registry = store.load()
    registry.block("dash-12", "attempt count ceiling")
    store.save(registry)
    assert cli._cmd_release_blocked(_discard()) == 0

    assert store.load().state_of("dash-12") is TaskState.READY
    reasons, _notes = cli._merge_window_blockers(wired_cli)
    assert reasons == [], f"the retired record must not hold the window shut: {reasons}"


def test_release_blocked_tolerates_a_quarantine_with_nothing_left_to_retire(
    wired_cli, capsys
):
    """Absence is a no-op, not an error, and never a silent one: a task parked
    before it ever committed has no worker and no record, and a `blocked` row
    whose records were already closed has no blocker either. The task still has
    to reach the queue, and the operator still has to be told what was there."""
    store, _executions, _parked = _quarantined_task(
        wired_cli, task_id="bare-01", worker=False, record=False, blocker=False
    )

    assert cli._cmd_release_blocked(_discard("bare-01")) == 0

    assert store.load().state_of("bare-01") is TaskState.READY
    out = capsys.readouterr().out
    assert "no execution record to retire" in out
    assert "no worker repo to clear" in out
    assert "no open blocker named this task" in out
    # ...and the fresh-budgets line is NOT printed, because there were no
    # budgets to archive. A summary that says what did not happen is the false
    # sentence every message here is written to avoid.
    assert "went into the archive" not in out


def test_a_blocker_record_that_cannot_be_read_refuses_rather_than_requeueing(
    wired_cli, capsys
):
    """The fail-open shape this whole area loses to: a store that RAISES must
    not be read as a store with nothing open in it. Requeueing on that reading
    would return the task to the queue with its question still live, which is
    the one state the command exists to make impossible."""
    store, executions, _parked = _quarantined_task(wired_cli, blocker=False)
    wired_cli.blockers_dir.mkdir(parents=True, exist_ok=True)
    (wired_cli.blockers_dir / "blk-dash-12-001.json").write_text(
        "{not json", encoding="utf-8"
    )

    assert cli._cmd_release_blocked(_discard()) == 1

    assert "blocker store could not be read" in capsys.readouterr().out
    assert store.load().state_of("dash-12") is TaskState.BLOCKED_BY_OPERATOR
    assert executions.load("dash-12") is not None, "the retirement never started"


def test_an_archival_that_fails_leaves_the_task_quarantined(wired_cli, monkeypatch, capsys):
    """The other half of "both halves move or neither does". The retirement is
    already durable at this point and is NOT rolled back — it is reported — but
    the task must not reach the queue with its blocker still open."""
    store, _executions, parked = _quarantined_task(wired_cli)

    def refuse(self, blocker_id, reason):
        raise StateError("the blockers directory is not writable")

    monkeypatch.setattr(BlockerStore, "archive_stale", refuse)

    assert cli._cmd_release_blocked(_discard()) == 1

    assert "could not be archived" in capsys.readouterr().out
    assert BlockerStore(wired_cli.blockers_dir).load(parked.id).resolved_at is None
    assert store.load().state_of("dash-12") is TaskState.BLOCKED_BY_OPERATOR


def test_a_requeue_that_cannot_be_completed_reopens_the_blocker(
    wired_cli, monkeypatch, capsys
):
    """`answer`'s rule, applied to a multi-record close: closing the last open
    blocker of a quarantined task returns that task to the queue in the SAME
    operation, so a close that cannot requeue is not a close."""
    store, _executions, parked = _quarantined_task(wired_cli)

    def refuse(config, task_store, registry):
        raise StateError("tasks.json could not be rewritten")

    monkeypatch.setattr(cli, "_reconcile_unblocked_tasks", refuse)

    assert cli._cmd_release_blocked(_discard()) == 1

    out = capsys.readouterr().out
    assert "could not be reconciled" in out and "NOT returned to the queue" in out
    reopened = BlockerStore(wired_cli.blockers_dir).load(parked.id)
    assert reopened.resolved_at is None and reopened.archived_reason == ""
    assert store.load().state_of("dash-12") is TaskState.BLOCKED_BY_OPERATOR
    # The retirement stands — it is never rolled back — and the message says so.
    assert sorted((wired_cli.executions_dir / "archive").glob("dash-12-*.json"))


def test_a_colliding_label_is_retried_under_a_second_one(wired_cli, monkeypatch):
    """Both halves refuse a colliding destination rather than clobbering an
    earlier attempt's evidence, and the label carries only a WHOLE-SECOND stamp
    — so two retirements of one task inside the same second collide by
    construction. The retry changes the label instead of waiting out the
    second. Time is frozen here because that is the only way to make the
    collision deterministic."""
    monkeypatch.setattr(worktask, "utcnow_iso", lambda: "2026-08-20T09:00:00+00:00")
    _quarantined_task(wired_cli)
    assert cli._cmd_release_blocked(_discard()) == 0

    # The same task parks again, in the same frozen second: the first attempt's
    # archive destination is already taken.
    store, executions, _parked = _quarantined_task(wired_cli)
    assert cli._cmd_release_blocked(_discard(reason="and again")) == 0

    assert store.load().state_of("dash-12") is TaskState.READY
    assert executions.load("dash-12") is None
    archived = sorted((wired_cli.executions_dir / "archive").glob("dash-12-*.json"))
    assert len(archived) == 2, archived
    assert any("discarded-by-operator-retry-" in p.name for p in archived)
    quarantined = sorted((wired_cli.workers_root.parent / "quarantine").glob("dash-12-*"))
    assert len(quarantined) == 2, "neither attempt's evidence was clobbered"


def test_a_repaired_record_path_names_this_retirement_not_an_older_retry(
    wired_cli, monkeypatch, capsys
):
    """When the first attempt archives the record and then fails on the worker,
    the retry has nothing left to file and the reported `record_path` is
    REPAIRED by reading the archive. It has to name the file THIS call wrote:
    the path is interpolated into the blocker's machine reason, so naming an
    older attempt's archive is a false statement in the very record that exists
    to account for the discard.

    The trap is that a task retired more than once has TWO namespaces in that
    archive — `<reason>-<stamp>` and `<reason>-retry-<stamp>` — and whole-name
    ordering puts every `-retry-` file after every dated one, whatever the
    stamps say. So one stale retry file outranks the fresh record."""
    clock = {"now": "2026-08-20T09:00:00+00:00"}
    monkeypatch.setattr(worktask, "utcnow_iso", lambda: clock["now"])

    # Two earlier retirements inside one frozen second, which is what puts a
    # `-retry-` RECORD in the archive at all: the second one's plain label
    # collides, so its retry label is what files the record.
    _quarantined_task(wired_cli)
    assert cli._cmd_release_blocked(_discard()) == 0
    _quarantined_task(wired_cli)
    assert cli._cmd_release_blocked(_discard(reason="and again")) == 0
    stale_retry = sorted((wired_cli.executions_dir / "archive").glob("dash-12-*-retry-*.json"))
    assert len(stale_retry) == 1, stale_retry

    # A third retirement, LATER, whose worker half fails once — the shape that
    # reaches the repair at all.
    clock["now"] = "2026-08-21T09:00:00+00:00"
    _store, _executions, parked = _quarantined_task(wired_cli)
    real_quarantine = WorkerRepoManager.quarantine
    calls = {"n": 0}

    def fail_once(self, task_id, label):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GitCommandError("mv", "quarantine destination is busy")
        return real_quarantine(self, task_id, label)

    monkeypatch.setattr(WorkerRepoManager, "quarantine", fail_once)
    capsys.readouterr()

    assert cli._cmd_release_blocked(_discard(reason="third time")) == 0

    filed = (
        wired_cli.executions_dir
        / "archive"
        / "dash-12-discarded-by-operator-20260821T090000Z.json"
    )
    assert filed.exists(), sorted((wired_cli.executions_dir / "archive").iterdir())
    out = capsys.readouterr().out
    assert f"execution record moved to {filed}" in out
    assert str(stale_retry[0]) not in out
    archived_reason = BlockerStore(wired_cli.blockers_dir).load(parked.id).archived_reason
    assert str(filed) in archived_reason
    assert str(stale_retry[0]) not in archived_reason, (
        "the blocker names an older retirement's record"
    )


def test_release_and_answer_behave_exactly_as_they_did(wired_cli, capsys):
    """The two verbs the new one must not disturb. `release` still refuses a
    quarantined task, and `answer` still leaves the execution record alone —
    most answers mean "carry on with the work you have", and discarding the
    candidate on every ordinary unblock is precisely what must not happen."""
    store, executions, parked = _quarantined_task(wired_cli)

    # `release` on a BLOCKED task: refused, exactly as before.
    assert cli._cmd_release(argparse.Namespace(config=None, task_id="dash-12")) == 1
    assert "not in progress" in capsys.readouterr().out
    assert executions.load("dash-12") is not None
    with pytest.raises(TaskGraphError) as excinfo:
        store.load().release("dash-12")
    assert excinfo.value.code == "task_not_in_progress"

    # `answer` on that same blocked task: requeues it and KEEPS the record.
    assert cli._cmd_answer(
        argparse.Namespace(config=None, blocker_id=parked.id, text="try again")
    ) == 0
    assert store.load().state_of("dash-12") is TaskState.READY
    kept = executions.load("dash-12")
    assert kept is not None and kept.candidate_sha == "c" * 40
    assert kept.attempt_count == 3, "an answer refills no budget on this code"
    assert BlockerStore(wired_cli.blockers_dir).load(parked.id).answer == "try again"
    assert WorkerRepoManager(
        wired_cli.workers_root, wired_cli.worker_hooks_dir
    ).path_for("dash-12").exists(), "answer keeps the worker too"

    # `release` on an IN-PROGRESS task: unchanged, record and worker retired
    # under its own label.
    registry = store.load()
    registry.mark_in_progress("dash-12")
    store.save(registry)
    # The two verbs stay on their own sides of the matrix: `release-blocked`
    # refuses an in-progress task and says which verb covers it.
    assert cli._cmd_release_blocked(_discard()) == 1
    refusal = capsys.readouterr().out
    assert "in progress, not quarantined" in refusal and "`release`" in refusal
    assert executions.load("dash-12") is not None, "a refusal moves nothing"

    assert cli._cmd_release(argparse.Namespace(config=None, task_id="dash-12")) == 0
    assert store.load().state_of("dash-12") is TaskState.READY
    assert executions.load("dash-12") is None
    assert sorted(
        (wired_cli.executions_dir / "archive").glob("dash-12-released-by-operator-*.json")
    )
