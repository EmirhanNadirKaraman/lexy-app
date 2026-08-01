"""The Codex CLI reviewer and the recorded quota failover.

Two things are being defended here.

**The adapter must not inherit ambiguity it does not have.** The browser
transport's UNCONFIRMED / reconcile / rotate machinery exists because a DOM
cannot say whether a message was delivered. A subprocess can. So a failed
`codex exec` is REJECTED and retryable, never a park — and the orchestrator
learns that from the `idempotent_submit` capability rather than from the
adapter lying about `send_attempted`.

**A failover must stay attributable.** The reviewer grants authority: its
approval carries the stamp that authorizes a commit. Handing the role to a
different provider mid-run is safe only when nothing was authorized under the
old one, and acceptable only when the record says which reviewer produced which
answer.

No codex binary is involved anywhere in this file — `CodexRunner` is a protocol
and every test injects a fake, exactly as `audit/agents.py` does for `claude`.
"""

import json

import pytest

from autoloop.browser.chatgpt import SubmitResult
from autoloop.codex.conversation import (
    CodexConversation,
    CodexResult,
    SubprocessCodexRunner,
)
from autoloop.codex.quota import (
    DEFAULT_QUOTA_PATTERNS,
    failure_digest,
    is_quota_exhausted,
)
from autoloop.config import AutoloopConfig, BrowserConfig, ConversationConfig
from autoloop.conversation import available_providers, create_conversation
from autoloop.errors import QuotaExhaustedError, ResponseTimeoutError
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

from test_orchestrator import FakeExecutor, FakeGit, block  # noqa: E402 - see conftest sys.path

RID = "alr-codex-0001"
PROMPT = f"[autoloop request {RID} | iteration 1]\n\nbody"


def stop_block(reason="all done"):
    return block({"version": 3, "decision": "stop", "reason": reason})


class FakeRunner:
    """Scripted `codex exec`. `results` is consumed one per call; the tail
    repeats, so a test scripts only the interesting invocations."""

    def __init__(self, results):
        self.results = list(results)
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def result(stdout="", stderr="", returncode=0):
    return CodexResult(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        duration_seconds=0.1,
        command=("codex", "exec"),
    )


# ---- the quota classifier (pure, so it is testable without the binary) ------


def test_a_successful_run_is_never_exhaustion():
    """OpenAI's documented behaviour is a soft stop: a turn already in flight
    finishes. A zero exit that mentions limits produced a review — use it."""
    assert not is_quota_exhausted(0, "you are near your usage limit", "")
    assert not is_quota_exhausted(0, "", "rate limit warning")


@pytest.mark.parametrize(
    "text",
    [
        "You've hit your usage limit for Codex.",
        "Error: rate limit exceeded",
        "quota exceeded for this plan",
        "429 Too Many Requests",
        "You are out of credits. Purchase additional credits to continue.",
    ],
)
def test_recognised_exhaustion_wordings(text):
    assert is_quota_exhausted(1, "", text)
    assert is_quota_exhausted(1, text, "")


def test_an_unrecognised_failure_is_not_exhaustion():
    """Degrades to an ordinary failure — noisy, never unsafe. An unrecognised
    failure authorizes nothing, and re-running a stateless call cannot
    double-post."""
    assert not is_quota_exhausted(1, "", "segmentation fault")


def test_patterns_are_overridable_without_touching_code():
    assert not is_quota_exhausted(1, "", "ratelimited: cool down")
    assert is_quota_exhausted(1, "", "ratelimited: cool down", patterns=("ratelimited",))


def test_failure_digest_is_bounded_and_carries_no_prompt():
    digest = failure_digest(7, "x" * 5000)
    assert digest["returncode"] == 7
    assert len(digest["stderr_tail"]) <= 400
    assert set(digest) == {"returncode", "stderr_tail"}


# ---- the adapter -----------------------------------------------------------


def test_a_successful_invocation_confirms_and_returns_the_reply():
    runner = FakeRunner([result(stdout=stop_block())])
    codex = CodexConversation(runner)
    assert codex.submit(RID, PROMPT) is SubmitResult.CONFIRMED
    assert codex.await_response(RID) == stop_block()
    assert runner.prompts == [PROMPT]


def test_submit_never_returns_unconfirmed():
    """There is no state in which 'we might have sent something' is true, so
    the adapter must not manufacture one."""
    for res in (result(returncode=1, stderr="boom"), result(stdout="   ")):
        codex = CodexConversation(FakeRunner([res]))
        assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED


def test_a_clean_exit_with_empty_stdout_is_rejected_not_confirmed():
    """Calling it CONFIRMED would hand the contract parser an empty string and
    spend a parse retry saying so."""
    codex = CodexConversation(FakeRunner([result(stdout="", returncode=0)]))
    assert codex.submit(RID, PROMPT) is SubmitResult.REJECTED


def test_reconcile_is_authoritative_within_the_process():
    codex = CodexConversation(FakeRunner([result(stdout=stop_block())]))
    assert codex.reconcile(RID) is False
    codex.submit(RID, PROMPT)
    assert codex.reconcile(RID) is True
    assert codex.has_request(RID) is True


def test_resubmitting_a_captured_request_sends_nothing():
    runner = FakeRunner([result(stdout=stop_block())])
    codex = CodexConversation(runner)
    codex.submit(RID, PROMPT)
    assert codex.submit(RID, PROMPT) is SubmitResult.ALREADY_PERSISTED
    assert len(runner.prompts) == 1


def test_awaiting_an_uncaptured_request_raises_rather_than_hanging():
    codex = CodexConversation(FakeRunner([result(stdout="x")]))
    with pytest.raises(ResponseTimeoutError):
        codex.await_response(RID)


def test_exhausted_allowance_raises_quota_exhausted():
    codex = CodexConversation(
        FakeRunner([result(returncode=1, stderr="You've hit your usage limit")])
    )
    with pytest.raises(QuotaExhaustedError) as exc:
        codex.submit(RID, PROMPT)
    # The message explains why a fallback is worth trying at all.
    assert "separate quota" in str(exc.value)


def test_every_failed_invocation_is_logged_for_diagnosis():
    """This is what turns the first real exhaustion into a config edit instead
    of an investigation, so it must fire for UNRECOGNISED failures too."""
    logged = []
    codex = CodexConversation(
        FakeRunner([result(returncode=3, stderr="something unfamiliar")]),
        log=lambda event, data: logged.append((event, data)),
    )
    codex.submit(RID, PROMPT)
    assert logged and logged[0][0] == "codex_invocation_failed"
    assert logged[0][1]["returncode"] == 3
    assert "something unfamiliar" in logged[0][1]["stderr_tail"]


def test_the_adapter_declares_idempotent_submit_and_no_rotation_surface():
    codex = CodexConversation(FakeRunner([result(stdout="x")]))
    assert codex.idempotent_submit is True
    # Absent by design: rotation is a browser concept, and `_client_for_request`
    # probes for these, so omitting them makes rotation unreachable rather than
    # disabled.
    assert not hasattr(codex, "retarget")
    assert not hasattr(codex, "current_url")


def test_the_runner_never_uses_a_shell_and_confines_the_working_dir(tmp_path):
    runner = SubprocessCodexRunner(
        command=("codex", "exec"), sandbox_args=("--read-only",), cwd=tmp_path
    )
    assert runner.argv_preview == ("codex", "exec", "--read-only")
    # The preview is what reaches diagnostics — it must not carry the prompt,
    # which is the entire review packet.
    assert all("request" not in part for part in runner.argv_preview)


def test_a_missing_binary_is_a_clear_actionable_error(tmp_path):
    from autoloop.errors import BrowserError

    runner = SubprocessCodexRunner(command=("definitely-not-a-real-binary-xyz",), cwd=tmp_path)
    with pytest.raises(BrowserError) as exc:
        runner.run("hello")
    assert "codex login" in str(exc.value)


def test_the_provider_is_registered_and_constructible(tmp_path):
    assert "codex_cli" in available_providers()
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path,
        conversation=ConversationConfig(provider="codex_cli"),
    )
    conversation = create_conversation("codex_cli", config)
    assert isinstance(conversation, CodexConversation)
    # Constructing must not require the binary — only running does.
    conversation.attach()
    conversation.close()


def test_default_patterns_are_not_empty():
    assert DEFAULT_QUOTA_PATTERNS


# ---- failover --------------------------------------------------------------


class ScriptedClient:
    """Minimal conversation double. `quota_on_submit` raises the exhaustion the
    orchestrator is supposed to route, rather than returning a result."""

    def __init__(self, name, *, quota_on_submit=False, responses=(), idempotent=False):
        self.name = name
        self.quota_on_submit = quota_on_submit
        self.responses = list(responses)
        self.persisted: set[str] = set()
        self.submitted: list[str] = []
        self.closed = False
        self.send_attempted = False
        if idempotent:
            self.idempotent_submit = True

    def attach(self):
        pass

    def has_request(self, rid):
        return rid in self.persisted

    def reconcile(self, rid):
        return rid in self.persisted

    def submit(self, rid, prompt):
        if self.quota_on_submit:
            raise QuotaExhaustedError("allowance spent; separate quota note")
        self.send_attempted = True
        self.submitted.append(rid)
        self.persisted.add(rid)
        return SubmitResult.CONFIRMED

    def await_response(self, rid):
        return self.responses.pop(0) if self.responses else stop_block()

    def close(self):
        self.closed = True


def build(tmp_path, clients, *, fallback="browser_chatgpt", state=None, policy=None):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    git = FakeGit(repo_root)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=policy or PolicyConfig(),
        state_dir=tmp_path / ".al",
        conversation=ConversationConfig(provider="codex_cli", fallback_provider=fallback),
    )
    store = StateStore(config.state_file)
    if state is None:
        state = LoopState.new("https://chatgpt.com/c/x")
        state.outbox = "kickoff"
    store.save(state)
    executor = FakeExecutor()
    executor.git = git
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: clients[config.conversation.provider],
        provider_factory=lambda provider: clients[provider],
        registry=TaskRegistry(),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
    )
    return orch, store, config


def pending(rid=RID):
    import hashlib

    req = PendingRequest(
        request_id=rid,
        payload="body",
        prompt=PROMPT,
        conversation_url="https://chatgpt.com/c/x",
    )
    req.prompt_sha256 = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()
    return req


def entries(config, kind):
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [r for r in rows if r.get("type") == kind]


def park_code(config):
    """The code a park recorded. It lives on the transcript's `needs_user`
    entry (and on a `Blocker` when a store is configured) — never on
    `LoopState`, which carries only the kind."""
    rows = entries(config, "needs_user")
    return rows[-1]["data"]["code"] if rows else None


def submitting_state(**overrides):
    state = LoopState.new("https://chatgpt.com/c/x")
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def test_exhausted_primary_hands_over_to_the_fallback(tmp_path):
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True, idempotent=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=submitting_state())

    orch.run(max_steps=1)  # codex raises; the role moves

    assert orch.state.active_provider == "browser_chatgpt"
    assert orch.state.provider_switches == 1
    assert orch.state.phase == Phase.SUBMITTING.value
    switched = entries(config, "provider_switched")
    assert switched and switched[0]["data"]["from_provider"] == "codex_cli"
    assert switched[0]["data"]["to_provider"] == "browser_chatgpt"
    assert switched[0]["data"]["reason"] == "quota_exhausted"

    orch.run(max_steps=1)  # re-issued on the browser
    assert clients["browser_chatgpt"].submitted == [RID]
    assert orch.state.pending_request.provider == "browser_chatgpt"


def test_the_handover_clears_only_per_transport_marks(tmp_path):
    """The fallback has never seen this request, so the exhausted transport's
    marks describe nothing here — without clearing them `submitting` would park
    on `submission_ambiguous` and the failover would be dead on arrival."""
    # The realistic route to a request carrying transport marks: a send was
    # disproven on codex, reconciliation confirmed absence and authorized the
    # one resend, and THAT resend is the invocation that reports exhaustion.
    state = submitting_state()
    state.phase = Phase.SUBMISSION_REJECTED.value
    state.pending_request.send_attempted = True
    state.pending_request.last_send_outcome = "rejected"
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=state)

    orch.run(max_steps=2)  # reconcile+authorize resend, then quota on the resend

    assert orch.state.provider_switches == 1

    req = orch.state.pending_request
    assert req.send_attempted is False
    assert req.last_send_outcome == ""
    assert req.resends_used == 0
    # The request itself is untouched — same id, same bytes.
    assert req.request_id == RID
    assert req.prompt == PROMPT


def test_the_response_records_which_reviewer_produced_it(tmp_path):
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser", responses=[stop_block("browser answered")]),
    }
    orch, store, config = build(tmp_path, clients, state=submitting_state())

    orch.run(max_steps=3)  # switch -> submit -> await

    assert orch.state.last_response is not None
    assert orch.state.last_response.provider == "browser_chatgpt"


def test_without_a_fallback_the_loop_parks(tmp_path):
    clients = {"codex_cli": ScriptedClient("codex", quota_on_submit=True)}
    orch, store, config = build(tmp_path, clients, fallback="", state=submitting_state())

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert park_code(config) == "quota_exhausted"
    assert orch.state.provider_switches == 0
    assert "separate quota" in orch.state.question


def test_a_fallback_equal_to_the_primary_parks(tmp_path):
    clients = {"codex_cli": ScriptedClient("codex", quota_on_submit=True)}
    orch, store, config = build(
        tmp_path, clients, fallback="codex_cli", state=submitting_state()
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.provider_switches == 0


def test_the_switch_budget_is_enforced(tmp_path):
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser", quota_on_submit=True),
    }
    orch, store, config = build(
        tmp_path, clients, state=submitting_state(provider_switches=1)
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "provider_switch_budget"
    assert orch.state.provider_switches == 1  # not incremented


def test_a_captured_reply_blocks_the_handover(tmp_path):
    """Quota cannot normally bite with a reply in hand; this asserts the guard
    rather than trusting the phase machine to keep that shape. A handover here
    would straddle an answered turn — two reviewers inside one round."""
    from autoloop.state import LastResponse

    state = submitting_state()
    state.last_response = LastResponse(
        request_id="alr-earlier-0001", raw=stop_block(), received_at="2026-08-01T00:00:00+00:00"
    )
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=state)

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.provider_switches == 0
    assert orch.state.active_provider == ""


def test_state_beats_config_after_a_switch(tmp_path):
    """A resumed run must not quietly return to the exhausted provider and
    spend the same allowance again."""
    clients = {
        "codex_cli": ScriptedClient("codex", quota_on_submit=True),
        "browser_chatgpt": ScriptedClient("browser"),
    }
    orch, store, config = build(tmp_path, clients, state=submitting_state())
    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.active_provider == "browser_chatgpt"

    second = tmp_path / "second"
    second.mkdir()
    orch2, _, _ = build(second, clients, state=reloaded)
    assert orch2.active_provider() == "browser_chatgpt"


def test_switch_budget_is_checkable_without_an_orchestrator():
    engine = PolicyEngine(PolicyConfig(max_provider_switches=1))
    assert engine.check_provider_switch_budget(0).allowed
    assert not engine.check_provider_switch_budget(1).allowed
    # 0 disables failover entirely — the other way to turn it off, alongside
    # leaving conversation.fallback_provider empty.
    assert PolicyEngine(PolicyConfig(max_provider_switches=0)).check_provider_switch_budget(
        0
    ).allowed is False


def test_an_idempotent_provider_never_parks_on_ambiguity(tmp_path):
    """The ambiguity park exists for a shared, persistent chat thread. A failed
    stateless invocation appended nothing, so retrying cannot double-post."""
    state = submitting_state()
    state.pending_request.send_attempted = True
    clients = {"codex_cli": ScriptedClient("codex", idempotent=True)}
    orch, store, config = build(tmp_path, clients, fallback="", state=state)

    orch.run(max_steps=1)

    assert orch.state.phase != Phase.NEEDS_USER.value
    assert clients["codex_cli"].submitted == [RID]
    authorized = entries(config, "resend_authorized")
    assert authorized and authorized[0]["data"]["reason_code"] == "idempotent_transport"


def test_a_non_idempotent_provider_still_parks_on_ambiguity(tmp_path):
    """The browser's rule is unchanged — this is the regression guard on the
    capability probe not leaking to providers that never declared it."""
    state = submitting_state()
    state.pending_request.send_attempted = True
    clients = {"codex_cli": ScriptedClient("codex")}  # no idempotent_submit
    orch, store, config = build(tmp_path, clients, fallback="", state=state)

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "submission_ambiguous"
