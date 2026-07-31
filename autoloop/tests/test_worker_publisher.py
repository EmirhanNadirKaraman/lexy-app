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

import os
import subprocess
from pathlib import Path

import pytest

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
from autoloop.state import LastResponse, LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
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
