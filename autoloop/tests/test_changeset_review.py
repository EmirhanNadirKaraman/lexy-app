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
# 4. a push response with NO changeset binding is still REFUSED — the old
#    direct-push path is not reopened
#
# 2026-08-21 (bind-01): the refusal's CODE changed, not its effect. An unbound
# `push` now lands on `push_missing_review_binding`, whose message names the
# request the stamp cited and the directive that gets the candidate presented
# again, instead of on `legacy_git_path_retired`, whose message describes the
# retirement of a route the reviewer did not take and leaves it with no next
# move. `commit` / `commit_and_push` — the actually retired decisions — still
# get `legacy_git_path_retired` (see `test_review_binding_carry.py`). Nothing
# is published either way, which is what this test is for.
# =============================================================================


def test_push_without_changeset_binding_is_still_refused(tmp_path):
    orch, _repo, upstream, config = build_orchestrator(tmp_path, with_publisher=False)
    orch.state.last_response = LastResponse(request_id="r1", raw="{}", received_at="now")

    orch._dispatch(Directive(decision=Decision.PUSH, reason="approved"))

    transcript_text = config.transcript_file.read_text(encoding="utf-8")
    assert "push_missing_review_binding" in transcript_text
    assert run_git(upstream, "for-each-ref").strip() == ""


# =============================================================================
# 5. THE BINDING SEAM — end to end from the CLI-shaped queue through dispatch
# =============================================================================
#
# Every test above hand-builds a `LastResponse` that already carries the
# binding, which is exactly why none of them caught A2: the binding was built
# correctly, submitted and awaited intact, and then DROPPED when `_step_awaiting`
# persisted the response (it copied `postcommit` and not `changeset`). These
# drive the real ready -> submitting -> awaiting -> executing path, so the
# construction that lost it is actually exercised.


class _ScriptedClient:
    """Minimal conversation double: confirms the send and replies with a
    stamped approval built from the prompt the loop actually submitted."""

    def __init__(self, responder):
        self._responder = responder
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()

    def attach(self):
        return None

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        from autoloop.browser.chatgpt import SubmitResult

        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        return self._responder(self.submitted[-1][1])

    def close(self):
        return None


def _stamped_push(prompt: str) -> str:
    """A `push` carrying the integrity stamp copied from CONTEXT, exactly as
    the contract requires a reviewer to do."""
    import re as _re

    stamp = {
        "request_id": _re.search(r"request_id: (\S+)", prompt).group(1),
        "head_sha": _re.search(r"head_sha: (\S+)", prompt).group(1),
        "report_sha256": _re.search(r"report_sha256: (\S+)", prompt).group(1),
    }
    return (
        "```json\n"
        + json.dumps(
            {"version": 3, "decision": "push", "reason": "approved", "reviewed": stamp}
        )
        + "\n```"
    )


def test_binding_survives_awaiting_and_reaches_dispatch_end_to_end(tmp_path):
    """A2 regression. From a CLI-shaped queue to a real published sha, with
    NO hand-built LastResponse anywhere: the binding must still carry the
    candidate sha, base sha, dest ref and packet digest when dispatch runs."""
    orch, repo, upstream, _config = build_orchestrator(tmp_path)
    git = orch._git
    base_sha = git.head_sha()
    (repo / "b.txt").write_text("two\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "the changeset")
    candidate_sha = git.head_sha()

    binding = build_changeset_binding(git, PolicyEngine(PolicyConfig()), base_sha, candidate_sha)
    queue_changeset(orch, git, binding)
    orch._client_factory = lambda: _ScriptedClient(_stamped_push)

    # Probe AT dispatch: `_step_executing` consumes `last_response`, so
    # inspecting it after the run would always read None regardless of the
    # bug. What the brief asks to prove is precisely what arrives here.
    seen = {}
    original = orch._dispatch_changeset_push

    def spy(directive, resp):
        seen["changeset"] = resp.changeset
        seen["report_sha256"] = resp.report_sha256
        return original(directive, resp)

    orch._dispatch_changeset_push = spy
    orch.run(max_steps=4)

    bound = seen.get("changeset")
    assert bound is not None, (
        "changeset dispatch was reached with NO binding — the approval could "
        "not be tied to the candidate"
    )
    assert bound.candidate_sha == candidate_sha
    assert bound.base_sha == base_sha
    assert bound.dest_ref == binding.dest_ref
    assert bound.packet_sha256 == seen["report_sha256"], "packet digest not carried"

    # ...and it really published exactly that object, through the publisher.
    published = run_git(upstream, "rev-parse", binding.dest_ref).strip()
    assert published == candidate_sha


def test_a_packet_missing_the_identifiers_refuses_before_sending(tmp_path):
    """The other half: an unbindable changeset must fail BEFORE the review
    packet goes out, not after a full round is spent discovering the approval
    cannot publish anything. No fallback to an unbound send."""
    orch, repo, _upstream, _config = build_orchestrator(tmp_path)
    git = orch._git
    base_sha = git.head_sha()
    (repo / "b.txt").write_text("two\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "the changeset")
    candidate_sha = git.head_sha()

    binding = build_changeset_binding(git, PolicyEngine(PolicyConfig()), base_sha, candidate_sha)
    orch.state.changeset = dataclasses.asdict(binding)
    # A --packet body that never mentions the identifiers.
    orch.state.outbox = "Please review my changes. They are good. Thanks."

    client = _ScriptedClient(_stamped_push)
    orch._client_factory = lambda: client

    orch.run(max_steps=4)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "changeset review is queued" in orch.state.question
    assert client.submitted == [], "an unbindable packet was sent to the reviewer"
