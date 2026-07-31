"""Operator-changeset review (`changeset_review.py` +
`Orchestrator._dispatch_changeset_push`): publishing a hand-authored commit
through the same exact-SHA-bound review discipline as a produce-then-review
task candidate, without reopening the retired `_dispatch_git` path (S21).

Self-contained per this codebase's convention (see e.g.
`test_postcommit_review.py`'s docstring) — duplicates the small `run_git` /
`gateway` / `make_bare` helpers rather than importing them from another test
module. Real git throughout, throwaway repos with a bare remote, matching
`test_worker_publisher.py`'s style.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from autoloop.changeset_review import (
    ChangesetBinding,
    build_changeset_binding,
    build_changeset_packet,
)
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.prompts import TEMPLATES
from autoloop.publisher import Publisher, provision_publisher_repo, read_publisher_url_snapshot
from autoloop.state import LastResponse, LoopState, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

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


def commit_in(repo_path, message, filename="a.txt", content="two\n"):
    (Path(repo_path) / filename).write_text(content)
    run_git(repo_path, "commit", "-q", "-am", message)
    return run_git(repo_path, "rev-parse", "HEAD").strip()


def stamped_push_reply(req) -> str:
    """A minimal, valid `push` directive echoing exactly what was stamped
    into `req` — the literal text a well-behaved ChatGPT reply would send."""
    return json.dumps(
        {
            "version": 3,
            "decision": "push",
            "reason": "approved changeset",
            "reviewed": {
                "request_id": req.request_id,
                "head_sha": req.head_sha,
                "report_sha256": req.report_sha256,
            },
        }
    )


class _NoExecutor:
    """Never expected to run in any test here — a changeset review has no
    task and never reaches the executor dispatch path."""

    def execute(self, directive, task):
        raise AssertionError("no executor call expected in a changeset-review test")


def build_orchestrator(tmp_path, branch="feature/x", with_publisher=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", branch)
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "T")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("one\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    upstream = make_bare(tmp_path)
    run_git(repo, "remote", "add", "origin", str(upstream))

    policy_config = PolicyConfig()
    policy = PolicyEngine(policy_config)
    git = GitGateway(repo, policy)

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy_config,
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    registry = TaskRegistry([])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    manifest_store = ManifestStore(config.manifests_dir)

    publisher = None
    publisher_url_snapshot = None
    if with_publisher:
        publisher_state_dir = tmp_path / "publisher-state"
        publisher_repo_path = provision_publisher_repo(publisher_state_dir, git, "origin")
        publisher = Publisher(publisher_repo_path, "origin", PolicyEngine(policy_config))
        publisher_url_snapshot = read_publisher_url_snapshot(publisher_state_dir)

    def no_client():
        raise AssertionError("no browser client expected in a changeset-review test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=policy,
        git=git,
        executor=_NoExecutor(),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=manifest_store,
        publisher=publisher,
        publisher_url_snapshot=publisher_url_snapshot,
    )
    return orch, repo, upstream, config


def queue_changeset(orch, git, binding) -> None:
    """Mirrors `cli._cmd_review_changeset`'s session setup without going
    through argparse: record the binding and render the packet into
    `state.outbox`, ready for `_step_ready` to pick up."""
    packet_text = build_changeset_packet(git, binding)
    orch.state.changeset = dataclasses.asdict(binding)
    orch.state.outbox = TEMPLATES["changeset_review"].render(
        branch=binding.branch, dest_ref=binding.dest_ref, packet=packet_text
    )


# =============================================================================
# 1. a stamped approval for the bound candidate publishes exactly that sha
# =============================================================================


def test_stamped_approval_publishes_exactly_the_bound_candidate(tmp_path):
    orch, repo, upstream, _config = build_orchestrator(tmp_path)
    git = orch._git
    base_sha = git.head_sha()
    candidate_sha = commit_in(repo, "operator change")

    binding = build_changeset_binding(git, orch._policy, base_sha, candidate_sha)
    queue_changeset(orch, git, binding)
    orch._step_ready()

    req = orch.state.pending_request
    assert req is not None and req.changeset is not None
    assert req.changeset.candidate_sha == candidate_sha

    orch.state.last_response = LastResponse(
        request_id=req.request_id,
        raw=stamped_push_reply(req),
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        changeset=req.changeset,
    )
    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    remote_head = run_git(upstream, "rev-parse", binding.dest_ref).strip()
    assert remote_head == candidate_sha
    # The changeset binding is cleared once actually published.
    assert orch.state.changeset is None


# =============================================================================
# 2. a later commit on the branch is NOT published — the recorded candidate
#    is, proving the binding (not HEAD) decides
# =============================================================================


def test_later_commit_on_the_branch_is_not_published(tmp_path):
    orch, repo, upstream, _config = build_orchestrator(tmp_path)
    git = orch._git
    base_sha = git.head_sha()
    candidate_sha = commit_in(repo, "operator change", content="two\n")

    binding = build_changeset_binding(git, orch._policy, base_sha, candidate_sha)
    queue_changeset(orch, git, binding)
    orch._step_ready()
    req = orch.state.pending_request
    assert req.head_sha == candidate_sha  # coincides: branch is fast-forwarded to it here

    # The operator keeps committing AFTER the review packet was sent. The
    # running checkout's HEAD now disagrees with what was stamped.
    later_sha = commit_in(repo, "later, unreviewed change", content="three\n")
    assert later_sha != candidate_sha
    assert git.head_sha() == later_sha

    orch.state.last_response = LastResponse(
        request_id=req.request_id,
        raw=stamped_push_reply(req),
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        changeset=req.changeset,
    )
    orch._step_executing()

    # Dispatched successfully — the HEAD-moved staleness check does not
    # apply to a changeset-bound push.
    assert orch.state.phase == Phase.READY.value
    remote_head = run_git(upstream, "rev-parse", binding.dest_ref).strip()
    assert remote_head == candidate_sha
    assert remote_head != later_sha


# =============================================================================
# 3. a protected destination refuses, and nothing is pushed
# =============================================================================


def test_protected_destination_refuses_and_nothing_is_pushed(tmp_path):
    # Default PolicyConfig protects "main"/"master".
    orch, repo, upstream, _config = build_orchestrator(tmp_path, branch="main")
    git = orch._git
    base_sha = git.head_sha()
    candidate_sha = commit_in(repo, "operator change on main")

    # Layer 1 — CLI-time validation. `build_changeset_binding` refuses
    # outright on a protected branch, before any session is even created.
    with pytest.raises(GitCommandError, match="protected"):
        build_changeset_binding(git, orch._policy, base_sha, candidate_sha)

    # Layer 2 — dispatch-time defense-in-depth, independent of layer 1.
    # Construct the binding directly (as if it had been recorded before a
    # `protected_branches` config change, or by some other means layer 1's
    # guard never saw), queue it, THEN switch the checkout to an
    # UNPROTECTED branch before the approval is dispatched. That switch is
    # what makes this load-bearing: `_step_executing`'s `destination_branch`
    # must read the PINNED `resp.changeset.branch` ("main"), not a fresh
    # `self._git.current_branch()` ("side") — otherwise `authorize_directive`
    # would wrongly clear the destination as unprotected (verified: reverting
    # that ternary to `self._git.current_branch()` makes this test's
    # `phase == READY` assertion fail — it parks on `needs_user` instead,
    # from `push_exact`'s own belt-and-braces protected-ref refusal one
    # layer further down).
    binding = ChangesetBinding(
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        candidate_tree_sha=git.tree_of(candidate_sha),
        branch="main",
        dest_ref="refs/heads/main",
        packet_sha256="",
    )
    queue_changeset(orch, git, binding)
    orch._step_ready()
    req = orch.state.pending_request

    run_git(repo, "checkout", "-q", "-b", "side")
    assert git.current_branch() == "side"

    orch.state.last_response = LastResponse(
        request_id=req.request_id,
        raw=stamped_push_reply(req),
        received_at="now",
        head_sha=req.head_sha,
        base_sha=req.base_sha,
        report_sha256=req.report_sha256,
        changeset=req.changeset,
    )
    orch._step_executing()

    # Refused by `authorize_directive`'s protected-branch gate — a corrective
    # reprompt, not a crash, and never reaching `_dispatch_changeset_push`.
    assert orch.state.phase == Phase.READY.value
    assert "protected" in (orch.state.outbox or "").lower()
    assert run_git(upstream, "for-each-ref").strip() == ""


# =============================================================================
# 4. a push response with NO changeset binding still hits
#    legacy_git_path_retired — the old direct-push path is not reopened
# =============================================================================


def test_push_without_changeset_binding_still_hits_legacy_refusal(tmp_path):
    orch, _repo, upstream, config = build_orchestrator(tmp_path, with_publisher=False)
    orch.state.last_response = LastResponse(request_id="r1", raw="{}", received_at="now")

    orch._dispatch(Directive(decision=Decision.PUSH, reason="approved"))

    transcript_text = config.transcript_file.read_text(encoding="utf-8")
    assert "legacy_git_path_retired" in transcript_text
    assert run_git(upstream, "for-each-ref").strip() == ""
