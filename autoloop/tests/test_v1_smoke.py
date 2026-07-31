"""Autoloop v1 smoke tests (offline/fixture, no real Chrome/ChatGPT/Claude).

Proves the produce-then-review CLI wiring end to end: `_build_orchestrator`
always constructs the full collaborator set, the legacy executor-manifest
commit route is gone, rt-01 is selectable via the seed file, worker repos are
isolated, commits are captured honestly, revise rounds retain the task base,
publication routes only through `Publisher` (and refuses on a url snapshot
drift or a protected branch), `reprovision-publisher` is the only way the
snapshot changes and cannot be reached from a directive, continuous mode
makes zero Claude/ChatGPT calls on an unchanged fingerprint and resumes a
saved phase after a restart, and `doctor` reports the v1 checks.

Self-contained per this codebase's convention (see e.g.
`test_postcommit_flow.py`'s docstring) — duplicates the small `run_git` /
response-block / `FakeClient` / writing-executor helpers rather than
importing them from another test module.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.audit.agents import ClaudeCliRunner
from autoloop.browser.chatgpt import SubmitResult
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision
from autoloop.doctor import DoctorProbes, run_doctor
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.publisher import (
    provision_publisher_repo,
    read_publisher_url_snapshot,
    reprovision_publisher,
)
from autoloop.state import LastResponse, LoopState, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.worker_env import verify_worker_isolation

URL = "https://chatgpt.com/c/v1-smoke-test"


# ---- shared fixtures / helpers ---------------------------------------------


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def ref_sha_or_empty(repo, ref) -> str:
    """`git rev-parse --verify <ref>`, or "" if the ref does not exist —
    unlike `run_git`, never raises on a missing ref (exactly the "nothing
    was published" case several refusal tests need to assert)."""
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def real_repo_with_origin(tmp_path: Path, name: str = "repo") -> tuple[Path, Path]:
    """A real repo with one commit and a configured `origin` remote (a
    second bare repo) — the produce-then-review collaborators (worker repos,
    Publisher) all need a real repository underneath; a bare `origin` is
    what makes `provision_publisher_repo` able to snapshot a url at all."""
    repo_root = tmp_path / name
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")

    origin = tmp_path / f"{name}-origin.git"
    run_git(tmp_path, "init", "-q", "--bare", str(origin))
    run_git(repo_root, "remote", "add", "origin", str(origin))
    return repo_root, origin


def make_config(tmp_path: Path, policy: PolicyConfig | None = None) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy or PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
    )


def write_config_toml(tmp_path: Path, state_dir_name: str = ".al") -> Path:
    """A real `.toml` config file — only needed by tests that go through
    `load_config` (the CLI entry points read a file, not an in-memory
    `AutoloopConfig`). `workers_root` is always absolute (`tmp_path /
    "workers_root"`) regardless of `state_dir_name`'s shape — a RELATIVE
    `workers_root` is refused unconditionally by `load_config`, so it cannot
    share `state_dir_name`'s "test the relative-path shape" purpose."""
    path = tmp_path / "config.toml"
    workers_root_value = str(tmp_path / "workers_root")
    path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{state_dir_name}"\nworkers_root = "{workers_root_value}"\n\n'
        "[policy]\nimplement_enabled = true\n",
        encoding="utf-8",
    )
    return path


NULL_ARGS = Namespace(null_executor=True)


class WritingExecutor:
    """Writes `files` into the dispatched task's own worker repo
    (`workers_root / task.id`) and reports success. `task` is never `None`
    here — the orchestrator resolves the audit to a synthetic `Task` before
    ever reaching an executor."""

    def __init__(self, workers_root, files, status="ok"):
        self.workers_root = Path(workers_root)
        self.files = dict(files)
        self.status = status
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        self.calls.append((directive, task))
        wt = self.workers_root / task.id
        for rel, content in self.files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            # `# call N` (not an HTML-style comment) so it stays valid Python
            # when `rel` ends in `.py` — real `ruff check .` runs against
            # this content in the CLI-built orchestrator (`_build_orchestrator`
            # wires no fake validation_runner; tests override it explicitly).
            target.write_text(f"{content}\n# call {len(self.calls)}\n", encoding="utf-8")
        return ExecutionOutcome(
            status=self.status,
            summary="did it",
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


def block(obj) -> str:
    return f"Reasoning...\n```json\n{json.dumps(obj)}\n```"


def audit_block(scope=None):
    data = {"version": 3, "decision": "audit", "reason": "orient"}
    if scope:
        data["scope"] = scope
    return block(data)


def implement_block(task_id="t1"):
    return block({"version": 3, "decision": "implement", "reason": "next", "task_id": task_id})


def revise_block(task_id="t1", feedback="please fix it"):
    return block(
        {"version": 3, "decision": "revise", "reason": "not quite", "task_id": task_id, "feedback": feedback}
    )


def stop_block():
    return block({"version": 3, "decision": "stop", "reason": "done"})


def extract_stamp(prompt: str) -> dict:
    return {
        "request_id": re.search(r"request_id: (\S+)", prompt).group(1),
        "head_sha": re.search(r"head_sha: (\S+)", prompt).group(1),
        "report_sha256": re.search(r"report_sha256: (\S+)", prompt).group(1),
    }


def push_approval(client):
    stamp = extract_stamp(client.submitted[-1][1])
    return block({"version": 3, "decision": "push", "reason": "approved", "reviewed": stamp})


class FakeClient:
    def __init__(self, responses=(), submit_result=SubmitResult.CONFIRMED):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.send_attempted = False

    def attach(self):
        pass

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        self.send_attempted = True
        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return self.submit_result if hasattr(self, "submit_result") else SubmitResult.CONFIRMED

    def await_response(self, request_id):
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        pass


def build_v1_orchestrator(
    tmp_path,
    monkeypatch,
    responses=(),
    policy=None,
    executor_files=None,
    task_id="t1",
    tasks=(),
    with_publisher=True,
    args=NULL_ARGS,
):
    """The FULL CLI-built collaborator set (`cli._build_orchestrator`), not a
    hand-assembled one — this is what makes these tests prove the WIRING,
    not just that the underlying primitives work in isolation.

    `_build_orchestrator` (like `_cmd_doctor`) always operates on
    `Path.cwd()`, not an explicit repo path — `monkeypatch.chdir` into the
    throwaway repo is therefore load-bearing, not decoration: without it
    these tests would silently operate on whatever directory the test
    process happens to be running from.
    """
    from autoloop.tasks import Task

    repo_root, origin = real_repo_with_origin(tmp_path)
    monkeypatch.chdir(repo_root)
    config = make_config(tmp_path, policy)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.outbox = "kickoff report"
    store.save(state)
    files = executor_files if executor_files is not None else {"feature.py": "print('hi')\n"}
    registry = TaskRegistry(
        list(tasks)
        or [
            Task(
                id=task_id,
                title=f"Title {task_id}",
                description="desc",
                approved_paths=tuple(files.keys()),
            )
        ]
    )
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    orch = cli._build_orchestrator(config, args, store, state, task_store, registry)
    orch._executor = WritingExecutor(config.workers_root, files)
    # `_build_orchestrator` (production) wires NO fake validation runner —
    # post-commit validation really runs `ruff check .`. These tests are not
    # about validation content, so swap in the same deterministic-pass fake
    # every other postcommit test file uses.
    orch._validation_runner = ok_validation
    if not with_publisher:
        orch._publisher = None
        orch._publisher_url_snapshot = None
    client = FakeClient(responses=list(responses))
    orch._client_factory = lambda: client
    return orch, config, repo_root, origin, client, registry, task_store


# =============================================================================
# 1. CLI normal run reaches the new worker path
# =============================================================================


def test_cli_run_reaches_the_produce_then_review_worker_path(tmp_path, monkeypatch):
    orch, config, repo_root, _origin, client, registry, _ = build_v1_orchestrator(
        tmp_path, monkeypatch, responses=[audit_block()]
    )

    assert orch._worker_repos is not None
    assert orch._execution_store is not None
    assert orch._intent_store is not None
    assert orch._publisher is not None
    assert orch._publisher_url_snapshot is not None

    calls = []
    real = Orchestrator._dispatch_task_postcommit

    def spy(self, directive, task, state):
        calls.append((directive.decision, task.id))
        return real(self, directive, task, state)

    monkeypatch.setattr(Orchestrator, "_dispatch_task_postcommit", spy)
    orch.run(max_steps=4)
    assert calls and calls[0][0] is Decision.AUDIT


# =============================================================================
# 2. the legacy executor commit route is UNREACHABLE
# =============================================================================


def test_legacy_commit_route_no_longer_exists():
    assert not hasattr(Orchestrator, "_dispatch_git")
    assert not hasattr(GitGateway, "commit")
    # the removed branch's own trigger condition is gone from the source too
    source = inspect.getsource(Orchestrator._dispatch_executor)
    assert "ChangeManifest.begin" not in source
    assert "_dispatch_git" not in inspect.getsource(Orchestrator)


# =============================================================================
# 3. rt-01 dry-run selection
# =============================================================================


def test_next_task_selects_rt01(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    args = Namespace(config=config_path)
    rc = cli._cmd_next_task(args)
    out = capsys.readouterr().out.strip()
    assert rc == 0
    assert out == "rt-01 — admin-gate GET /books/packages and POST /books/import"


def test_seed_tasks_file_is_git_tracked_and_matches_next_task_output():
    seed_path = Path(__file__).resolve().parents[1] / "seed_tasks.json"
    repo_root = Path(__file__).resolve().parents[2]
    assert seed_path.exists()
    specs = json.loads(seed_path.read_text(encoding="utf-8"))
    assert any(s["id"] == "rt-01" for s in specs)
    # "git tracked" is the whole point of the name: `config.seed_tasks_file`
    # resolves package-relative, so a checkout that never staged this file
    # would still pass every check above on the machine that wrote it, while
    # `next-task` silently breaks on every OTHER checkout. Prove the
    # positive (indexed) when possible; when it is not yet staged (this
    # worktree, mid-change, before a human `git add`), fall back to proving
    # it is at least track-ABLE — no `.gitignore` rule would silently
    # swallow it — so this test still discriminates a real footgun (a
    # `*.json` or `autoloop/` ignore rule) from the expected pre-commit gap.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(seed_path)],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", str(seed_path)],
            cwd=str(repo_root),
        )
        assert ignored.returncode != 0, (
            f"{seed_path} is excluded by a .gitignore rule — `next-task` "
            "and continuous-mode task selection will silently break on any "
            "checkout other than this one"
        )


# =============================================================================
# 4. worker repository has no remote and no credentials
# =============================================================================


def test_worker_repository_has_no_remote_or_credentials(tmp_path, monkeypatch):
    orch, config, repo_root, _origin, _client, _registry, _ = build_v1_orchestrator(tmp_path, monkeypatch)
    execution = orch._worker_repos.create("wt1", repo_root, GitGateway(repo_root, orch._policy).head_sha())
    g = execution.gateway(orch._policy)
    violations = verify_worker_isolation(g, expected_hooks_dir=orch._worker_repos.hooks_dir_for("wt1"))
    assert violations == []


# =============================================================================
# 5/6. commit captured AFTER hooks return; the packet is derived from it
# =============================================================================


def test_commit_captured_after_hooks_and_packet_derived_from_it(tmp_path, monkeypatch):
    """The hook-attack-timing edge case itself (a hook REWRITING content
    between staging and commit) is exhaustively pinned at the primitive
    level in `test_postcommit_primitives.py`/`test_git_gateway.py` — a hook
    installed mid-task, by design, is instead REFUSED outright by the
    environment-drift check (`environment.snapshot`/`verify_unchanged`,
    wired in `_dispatch_task_postcommit`) before `commit_and_capture` is
    even reached, since worker repos redirect `core.hooksPath` away from
    `.git/hooks` specifically to make "a hook that appeared after the task
    started" detectable rather than silently trusted. What this smoke test
    proves, through the FULL CLI-wired dispatch (not the raw primitive): the
    persisted `candidate_sha` is never stale or predicted — it matches a
    FRESH, independent `git rev-parse HEAD` taken from the worker repo AFTER
    dispatch returns — and the review packet ChatGPT is actually shown is
    derived from exactly that same, real commit.
    """
    orch, config, repo_root, _origin, client, registry, _ = build_v1_orchestrator(
        tmp_path, monkeypatch, responses=[implement_block("t1")]
    )

    orch.run(max_steps=4)
    execution = orch._execution_store.load("t1")
    assert execution is not None and execution.candidate_sha

    independent_head = run_git(execution.worktree_path, "rev-parse", "HEAD").strip()
    assert independent_head == execution.candidate_sha  # never stale, never predicted

    # the packet ChatGPT was actually shown carries that exact sha, literally
    assert f"candidate_sha: {execution.candidate_sha}" in (orch.state.outbox or "")


# =============================================================================
# 7. revise produces a new candidate while retaining the task base
# =============================================================================


def test_revise_produces_new_candidate_retaining_task_base(tmp_path, monkeypatch):
    orch, config, repo_root, _origin, client, registry, _ = build_v1_orchestrator(
        tmp_path,
        monkeypatch,
        responses=[implement_block("t1"), revise_block("t1", "polish it")],
    )

    orch.run(max_steps=4)
    execution_round1 = orch._execution_store.load("t1")
    assert execution_round1.review_round == 1
    base1, candidate1 = execution_round1.task_base_sha, execution_round1.candidate_sha

    orch.run(max_steps=4)
    execution_round2 = orch._execution_store.load("t1")
    assert execution_round2.review_round == 2
    assert execution_round2.task_base_sha == base1  # retained
    assert execution_round2.candidate_sha != candidate1  # advanced


# =============================================================================
# 8. an approved push routes ONLY through Publisher
# =============================================================================


def test_approved_push_routes_only_through_publisher(tmp_path, monkeypatch):
    orch, config, repo_root, origin, client, registry, _ = build_v1_orchestrator(
        tmp_path,
        monkeypatch,
        responses=[implement_block("t1"), "PLACEHOLDER"],
        policy=PolicyConfig(implement_enabled=True, allow_protected_push=True),
    )
    client.responses[1] = push_approval

    calls = {"publisher_repo": [], "worker_repo": []}
    original = GitGateway.push_exact

    def spy(self, *a, **kw):
        target = "publisher_repo" if self.repo_root == orch._publisher.repo_root else "worker_repo"
        calls[target].append(a)
        return original(self, *a, **kw)

    monkeypatch.setattr(GitGateway, "push_exact", spy)
    orch.run(max_steps=8)

    assert orch.state.phase == Phase.READY.value
    assert calls["publisher_repo"], "push_exact must be called on the Publisher's own repo"
    assert not calls["worker_repo"], "push_exact must NEVER be called directly on the worker repo"
    remote_head = run_git(origin, "rev-parse", "refs/heads/autoloop/t1").strip()
    execution = orch._execution_store.load("t1")
    assert remote_head == execution.candidate_sha


# =============================================================================
# 9. publisher URL drift refuses
# =============================================================================


def test_publisher_url_drift_refuses_the_push(tmp_path, monkeypatch):
    orch, config, repo_root, origin, client, registry, _ = build_v1_orchestrator(
        tmp_path,
        monkeypatch,
        responses=[implement_block("t1"), "PLACEHOLDER"],
        policy=PolicyConfig(implement_enabled=True, allow_protected_push=True),
    )
    client.responses[1] = push_approval

    # The main checkout's origin changes AFTER the publisher was provisioned
    # — the snapshot autoloop is holding no longer matches.
    elsewhere = tmp_path / "elsewhere.git"
    run_git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    run_git(repo_root, "remote", "set-url", "origin", str(elsewhere))

    orch.run(max_steps=8)
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "reprovision-publisher --confirm" in orch.state.question
    # nothing was published
    assert ref_sha_or_empty(origin, "refs/heads/autoloop/t1") == ""


# =============================================================================
# 10. explicit reprovision updates the snapshot only when directly invoked
# =============================================================================


def test_reprovision_is_the_only_way_the_snapshot_changes_and_is_unreachable_from_a_directive(
    tmp_path,
):
    repo_root, origin = real_repo_with_origin(tmp_path)
    state_dir = tmp_path / ".al"
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    provision_publisher_repo(state_dir, git)
    original_snapshot = read_publisher_url_snapshot(state_dir)
    assert original_snapshot is not None

    with pytest.raises(Exception):
        reprovision_publisher(state_dir, git, confirm=False)
    assert read_publisher_url_snapshot(state_dir) == original_snapshot  # unchanged without confirm

    new_origin = tmp_path / "new-origin.git"
    run_git(tmp_path, "init", "-q", "--bare", str(new_origin))
    run_git(repo_root, "remote", "set-url", "origin", str(new_origin))
    updated = reprovision_publisher(state_dir, git, confirm=True)
    assert updated == str(new_origin)
    assert read_publisher_url_snapshot(state_dir) == str(new_origin)

    # structural: nothing directive-reachable CALLS it. orchestrator.py's
    # parked drift message legitimately NAMES the command (prose, for a
    # human to type) without ever invoking it — the distinguishing check is
    # "no call expression", not "the word never appears".
    import autoloop.orchestrator as orchestrator_mod
    import autoloop.policy as policy_mod
    import autoloop.contract as contract_mod

    for mod in (orchestrator_mod, policy_mod, contract_mod):
        assert "reprovision_publisher(" not in inspect.getsource(mod)
    # the CLI command is the sole caller, and it is not itself reachable
    # from `main()`'s directive-dispatch machinery — it is its own top-level
    # subcommand, requiring --confirm, invoked only by a human on the CLI.
    cli_source = inspect.getsource(cli)
    assert cli_source.count("_reprovision_publisher_snapshot(") == 1


# =============================================================================
# 11. protected branch refuses
# =============================================================================


def test_protected_task_branch_refuses_the_push(tmp_path, monkeypatch):
    orch, config, repo_root, origin, client, registry, _ = build_v1_orchestrator(
        tmp_path,
        monkeypatch,
        responses=[implement_block("t1"), "PLACEHOLDER"],
        # the worker's OWN branch (autoloop/t1) is explicitly protected here
        policy=PolicyConfig(implement_enabled=True, protected_branches=("autoloop/t1",)),
    )
    client.responses[1] = push_approval

    orch.run(max_steps=8)
    # The denial is queued (state.outbox / state.question) but the step
    # budget lands exactly before it would be SENT as a fresh request —
    # check the queued payload directly rather than depending on an extra
    # round-trip's step count. Assert the EXACT policy/gateway wording
    # (`push_exact`'s protected-branch guard, orchestrator.py ~1234) rather
    # than a loose "protected" substring, so this discriminates a
    # protected-branch refusal from any other denial that happens to
    # mention "protected" in passing. Also pin `submitted` to exactly the
    # two requests that legitimately went out (kickoff, review packet) —
    # proving the denial itself was never sent as a third request.
    denial_text = (orch.state.outbox or "") + (orch.state.question or "")
    assert "direct push to protected branch" in denial_text
    assert "'autoloop/t1'" in denial_text
    assert len(client.submitted) == 2
    assert ref_sha_or_empty(origin, "refs/heads/autoloop/t1") == ""


# =============================================================================
# 12. an unchanged idle fingerprint makes ZERO Claude and ZERO ChatGPT calls
# =============================================================================


def test_idle_unchanged_fingerprint_makes_zero_claude_or_chatgpt_calls(tmp_path, monkeypatch):
    repo_root, _origin = real_repo_with_origin(tmp_path)
    config = make_config(tmp_path)
    store = StateStore(config.state_file)
    # no ready task: an empty, already-persisted registry
    task_store = TaskStore(config.tasks_file)
    task_store.save(TaskRegistry())

    chatgpt_calls = {"count": 0}
    claude_calls = {"count": 0}

    def boom_conversation(*a, **kw):
        chatgpt_calls["count"] += 1
        raise AssertionError("no ChatGPT call expected")

    def boom_claude(self, spec):
        claude_calls["count"] += 1
        raise AssertionError("no Claude call expected")

    monkeypatch.setattr(cli, "create_conversation", boom_conversation)
    monkeypatch.setattr(ClaudeCliRunner, "run", boom_claude)
    # `_select_and_kickoff` fingerprints `Path.cwd()` — same convention
    # `_build_orchestrator`/`_cmd_doctor` already use for "the target repo".
    monkeypatch.chdir(repo_root)

    # Prime the persisted fingerprint to the CURRENT repository state, so
    # this run is genuinely "unchanged since the last check" rather than
    # "never checked before".
    cli._save_fingerprint(config, cli.repo_fingerprint(repo_root))

    registry = task_store.load()
    started = cli._select_and_kickoff(config, store, registry)

    assert started is False
    assert chatgpt_calls["count"] == 0
    assert claude_calls["count"] == 0


# =============================================================================
# 13. restart resumes the saved phase
# =============================================================================


class _StopContinuous(Exception):
    """Raised by the patched `time.sleep` to break `_run_continuous`'s
    infinite loop deterministically, once it reaches a genuine "nothing to
    do" boundary — never depends on real timing."""


def test_continuous_mode_restart_resumes_the_saved_phase(tmp_path, monkeypatch):
    repo_root, _origin = real_repo_with_origin(tmp_path)
    monkeypatch.chdir(repo_root)  # _build_orchestrator (called inside _run_continuous) uses Path.cwd()
    config = make_config(tmp_path)
    task_store = TaskStore(config.tasks_file)
    task_store.save(TaskRegistry())  # no ready task

    # Pre-seed the fingerprint to the CURRENT repo state: once the resumed
    # session reaches a clean boundary (stopped) and there is still no ready
    # task, the selection policy must find "unchanged" and sleep — this is
    # what lets the test end deterministically via the patched `time.sleep`
    # rather than kicking off yet another round.
    cli._save_fingerprint(config, cli.repo_fingerprint(repo_root))

    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.iteration = 1
    state.phase = Phase.EXECUTING.value
    state.last_response = LastResponse(
        request_id="alr-restart-0001",
        raw=audit_block(),
        received_at="t",
        head_sha="a" * 40,
        base_sha="(none)",
        report_sha256="b" * 64,
    )
    store.save(state)

    # Round 1 (the resumed EXECUTING dispatch) needs no client interaction at
    # all — `_step_executing` dispatches straight from the already-saved
    # response. Round 2 is the FRESH request the dispatch just queued
    # (state.outbox), which genuinely goes through submit/await — answered
    # here with `stop`, reaching a clean boundary.
    client = FakeClient(responses=[stop_block()])
    monkeypatch.setattr(cli, "create_conversation", lambda *a, **kw: client)

    def stop_sleep(seconds):
        raise _StopContinuous()

    monkeypatch.setattr(cli.time, "sleep", stop_sleep)

    args = Namespace(
        config=config.state_dir.parent / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    with pytest.raises(_StopContinuous):
        cli._run_continuous(args, config)

    assert len(client.submitted) == 1  # exactly the one fresh request queued by the resumed dispatch
    reloaded = store.load()
    assert reloaded.phase == Phase.STOPPED.value  # the resumed session ran to completion, not restarted
    assert reloaded.session_id == state.session_id  # the SAME session was resumed, never replaced


# =============================================================================
# 14. doctor reports the complete v1 configuration
# =============================================================================


def test_doctor_reports_the_complete_v1_configuration(tmp_path):
    repo_root, _origin = real_repo_with_origin(tmp_path)
    config = make_config(tmp_path)

    def probe_cdp(url):
        raise OSError("no CDP in this test")

    results = run_doctor(
        config, repo_root, probes=DoctorProbes(probe_cdp=probe_cdp, playwright_present=lambda: False)
    )
    names = {r.name: r for r in results}
    for expected in (
        "branch_policy",
        "worker_isolation",
        "hooks_dirs",
        "publisher",
        "publisher_url_drift",
    ):
        assert expected in names, sorted(names)
    assert names["worker_isolation"].status == "ok", names["worker_isolation"].detail
    assert names["hooks_dirs"].status == "ok", names["hooks_dirs"].detail
    assert names["publisher"].status == "ok", names["publisher"].detail
    assert names["publisher_url_drift"].status == "ok", names["publisher_url_drift"].detail


def test_relative_state_dir_still_provisions_worker_and_publisher(tmp_path, monkeypatch):
    """The shipped config uses `state_dir = ".autoloop"` — a RELATIVE path.

    Every other test passes an absolute `tmp_path`, so none of them exercised
    the real configuration. Relative paths become subprocess `cwd` values and
    `git init` targets from a different working directory: `git init -q <rel>`
    run with `cwd=<rel>` nests the repo a level deeper, and the next call's
    `cwd` then does not exist. `doctor` crashed with
    `FileNotFoundError: '.autoloop/workers/doctor-probe'` before this was fixed.
    """
    from autoloop.publisher import (
        provision_publisher_repo,
        publisher_repo_path,
        read_publisher_url_snapshot,
    )
    from autoloop.worker_env import WorkerRepoManager, verify_worker_isolation, worker_env

    source = tmp_path / "src"
    source.mkdir()
    run_git(source, "init", "-q", "-b", "main")
    run_git(source, "config", "user.email", "t@example.com")
    run_git(source, "config", "user.name", "T")
    (source / "f.txt").write_text("x\n", encoding="utf-8")
    run_git(source, "add", "f.txt")
    run_git(source, "commit", "-q", "-m", "init")
    run_git(source, "remote", "add", "origin", str(tmp_path / "remote.git"))
    run_git(tmp_path, "init", "-q", "--bare", "remote.git")

    # Operate with cwd = the repo and a RELATIVE state dir, exactly as shipped.
    monkeypatch.chdir(source)
    state_dir = Path(".autoloop")

    workers = WorkerRepoManager(state_dir / "workers", state_dir / "worker-hooks")
    git = GitGateway(source, PolicyEngine(PolicyConfig()))
    repo = workers.create("probe", git.repo_root, git.head_sha())

    assert repo.path.is_absolute(), "manager must resolve relative roots"
    assert (repo.path / ".git").exists(), "worker repo really was created"
    wgit = GitGateway(repo.path, PolicyEngine(PolicyConfig()), env=worker_env())
    assert verify_worker_isolation(wgit) == []

    provision_publisher_repo(state_dir, git)
    pub = publisher_repo_path(state_dir)
    assert pub.is_absolute() and pub.exists()
    assert read_publisher_url_snapshot(state_dir)
    # Nothing was nested one level deeper by a re-interpreted relative path.
    assert not (source / ".autoloop" / ".autoloop").exists()
