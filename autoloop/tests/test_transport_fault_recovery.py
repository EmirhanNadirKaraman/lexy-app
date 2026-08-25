"""A codex failure is recovered as a codex failure, not as a browser one.

MEASURED 2026-08-22, `conversation.provider = "codex_cli"`, no browser anywhere
in that run's design: 34 browser events between 17:00 and 17:35, ending

    17:34:22  browser_restarted        "launching chrome on profile
                                        /Users/emir/.autoloop-chrome, debug port 9222"
    17:34:22  browser_error            "no codex reply was captured for
                                        alr-7765f8cc-0021; the invocation did not
                                        complete in this process"  kind=ResponseTimeoutError
    17:34:22  browser_restart_skipped  "within cooldown"

Chrome really started (pid 29055, `--user-data-dir=/Users/emir/.autoloop-chrome
--remote-debugging-port=9222`), the loop parked `loop_fatal` on
`browser_restart_cooldown_blocked`, and the advice it left was to restart the
browser by hand or lower `browser.restart_cooldown_seconds`. Neither can repair
a subprocess fault.

The claim these tests defend, in one sentence: **a fault raised by a non-browser
transport never increments the browser fault budget, never launches a browser
and never parks on browser-specific advice — and a transport that declares
`idempotent_submit` resumes by RE-RUNNING its invocation rather than waiting in
`awaiting` for a reply that cannot exist.**

Four properties, and the fourth is the one a rename would fail:

1. the browser is never launched, and no browser budget moves (§1);
2. a park names a remedy for the transport actually in use (§2);
3. an unrecoverable `awaiting` is replayed — but ONLY on `idempotent_submit`,
   only in `awaiting`, only after `reconcile` confirms absence, and only within
   a bound (§3);
4. the browser provider's restart, cooldown and fault-budget behaviour is
   unchanged, which the untouched `test_rounds_and_restart.py` and
   `test_transport_recovery.py` suites assert directly and §4 re-checks through
   the new dispatch site. (Conversation rotation was the fourth item on that
   list until brw-15 removed it; §4's browser/codex split is now about the
   REMEDY each park quotes, not about which recovery each transport gets.)

No codex binary is involved: `CodexRunner` is a protocol and every test injects
a fake, exactly as `test_codex_provider.py` does.
"""

import json
import subprocess

import pytest

from autoloop import orchestrator as orchestrator_module
from autoloop.browser.chatgpt import SubmitResult
from autoloop.codex.conversation import CodexConversation, CodexResult
from autoloop.config import AutoloopConfig, BrowserConfig, ConversationConfig
from autoloop.conversation import (
    _BROWSER_BACKED,
    browser_backed_providers,
    register_provider,
    transport_is_browser_backed,
)
from autoloop.errors import BrowserError, RateLimitedError, ResponseTimeoutError
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import MAX_AWAIT_REPLAYS, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger

from test_orchestrator import FakeExecutor, FakeGit, block  # noqa: E402 - see conftest sys.path

RID = "alr-codex-0001"
PROMPT = f"[autoloop request {RID} | iteration 1]\n\nbody"
CONV_URL = "https://chatgpt.com/c/x"
#: A restart command that IS configured. Without one `_browser_restart_outcome`
#: returns RESTART_DISABLED and never reaches `subprocess.run` at all — so a
#: "no browser was launched" assertion would pass for the wrong reason and prove
#: nothing about routing. Every test here that claims Chrome stayed shut
#: configures this.
RESTART_COMMAND = ("python3", "-m", "autoloop.browser.chrome_restart")
#: A browser-backed transport registered for this module only.
#:
#: Since brw-16 (2026-08-25) no SHIPPED provider is browser-backed, so §4's
#: "the browser provider is untouched" tests would otherwise be asserting about
#: a transport that does not exist. What they are really about — that a
#: browser-backed transport still gets the restart, the cooldown park and the
#: unattachable recovery, and that a NON-browser one gets none of it — is
#: unchanged, and this is the seam a browser adapter arrives through now:
#: `register_provider(..., browser_backed=True)`. Using the retired name here
#: would test nothing, since `transport_is_browser_backed` no longer knows it.
BROWSER_PROVIDER = "fake_browser_for_fault_tests"


def stop_block(reason="all done"):
    return block({"version": 3, "decision": "stop", "reason": reason})


def result(stdout="", stderr="", returncode=0):
    return CodexResult(
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
        duration_seconds=0.1,
        command=("codex", "exec"),
    )


class FakeRunner:
    """Scripted `codex exec`. `results` is consumed one per call; the tail
    repeats. An entry that is an Exception is RAISED instead of returned, which
    is how the CLI's own timeout and its missing-binary fault arrive."""

    def __init__(self, results):
        self.results = list(results)
        self.prompts: list[str] = []

    def run(self, prompt):
        self.prompts.append(prompt)
        entry = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(entry, Exception):
            raise entry
        return entry


class SilentIdempotentClient:
    """A transport that declares `idempotent_submit`, CONFIRMS every send, and
    never has a reply for anyone.

    Deliberately self-contradictory — a real adapter that confirms a submit can
    answer the matching `await_response` — because that contradiction is exactly
    the fail-open the replay bound exists for: without `MAX_AWAIT_REPLAYS` this
    shape re-invokes a reviewer forever, on no budget and with no park.
    """

    idempotent_submit = True

    def __init__(self):
        self.submitted: list[str] = []
        self.closed = False

    def attach(self):
        pass

    def has_request(self, rid):
        return False

    def reconcile(self, rid):
        return False

    def submit(self, rid, prompt):
        self.submitted.append(rid)
        return SubmitResult.CONFIRMED

    def await_response(self, rid):
        raise ResponseTimeoutError(f"no reply was captured for {rid}")

    def close(self):
        self.closed = True


class PlainClient(SilentIdempotentClient):
    """The same, minus the declaration. Stands for every transport that has not
    said re-running is safe — which must never be re-run automatically."""

    idempotent_submit = False


class ExplodingReconcileClient(SilentIdempotentClient):
    """Declares the capability but cannot answer whether the reply is there.
    "Could not ask" is not "absent"."""

    def reconcile(self, rid):
        raise BrowserError("the transport could not be asked")


def build(
    tmp_path,
    client,
    *,
    provider="codex_cli",
    state=None,
    policy=None,
    fallback="",
    **browser_kw,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    git = FakeGit(repo_root)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL, **browser_kw),
        policy=policy or PolicyConfig(),
        state_dir=tmp_path / ".al",
        conversation=ConversationConfig(provider=provider, fallback_provider=fallback),
    )
    store = StateStore(config.state_file)
    if state is None:
        state = LoopState.new(CONV_URL)
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
        client_factory=lambda: client,
        registry=TaskRegistry(),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
    )
    # The one probe in this file's reach that dials a real socket. Stubbed on
    # the instance to UNMEASURABLE — the job the autouse `_no_live_cdp_probe`
    # conftest fixture did for the whole suite until brw-16 removed it (see
    # `conftest.py` for why it could not stay there). Tests that describe a
    # particular browser assign their own afterwards.
    orch._attachable_page_targets = lambda: None
    return orch, store, config


@pytest.fixture(autouse=True)
def _browser_backed_provider():
    """Register `BROWSER_PROVIDER` for one test, then leave the registry as
    found — including for the tests below that assert on its exact contents."""
    from autoloop import conversation as conversation_module

    register_provider(BROWSER_PROVIDER, lambda config: None, browser_backed=True)
    try:
        yield
    finally:
        conversation_module._PROVIDERS.pop(BROWSER_PROVIDER, None)
        _BROWSER_BACKED.discard(BROWSER_PROVIDER)


def pending(rid=RID, prompt=PROMPT, **overrides):
    import hashlib

    fields = dict(request_id=rid, payload="body", prompt=prompt, conversation_url=CONV_URL)
    fields.update(overrides)
    req = PendingRequest(**fields)
    req.prompt_sha256 = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()
    return req


def state_in(phase, **overrides):
    state = LoopState.new(CONV_URL)
    state.phase = phase.value
    state.pending_request = pending()
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


def rows(config, kind=None):
    path = config.transcript_file
    if not path.exists():
        return []
    entries = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [r for r in entries if kind is None or r.get("type") == kind]


def park_code(config):
    parked = rows(config, "needs_user")
    return parked[-1]["data"]["code"] if parked else None


@pytest.fixture
def restarts(monkeypatch):
    """Every `subprocess.run` the orchestrator performs, recorded rather than
    executed — the same instrument `test_rounds_and_restart._fake_restart` uses.

    A recorded call in a test here means the browser restart command ran. No
    codex invocation can be mistaken for one: `CodexRunner` is a protocol and
    every test injects `FakeRunner`, which never reaches `subprocess` at all.
    """
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="up", stderr="")

    monkeypatch.setattr(orchestrator_module.subprocess, "run", run)
    return calls


# ---- 0. the discriminator is a property of the PROVIDER, not of an object ----


def test_only_a_provider_that_declared_itself_is_browser_backed():
    """No SHIPPED provider is (brw-16). The discriminator still works, and the
    only member is the one this module registers — `test_conversation.py` pins
    the empty registry with nothing registered at all."""
    assert transport_is_browser_backed(BROWSER_PROVIDER)
    assert not transport_is_browser_backed("codex_cli")
    assert not transport_is_browser_backed("codex_app_server")
    assert not transport_is_browser_backed("browser_chatgpt"), "retired, not special"
    assert browser_backed_providers() == [BROWSER_PROVIDER]


def test_an_unknown_or_empty_provider_is_not_browser_backed():
    """The fail-CLOSED direction, and the reason this is keyed on a name rather
    than on `getattr(client, ...)`: the failure handlers run with the client
    already dropped, and a transport whose FACTORY raised never produced an
    object to ask. Both would answer "no client", and defaulting that to "yes,
    browser" is the 2026-08-22 incident."""
    assert not transport_is_browser_backed("")
    assert not transport_is_browser_backed("some_future_adapter")
    assert not transport_is_browser_backed("BROWSER_CHATGPT")  # exact names only


def test_an_adapter_can_declare_itself_browser_backed():
    from autoloop import conversation as conversation_module

    name = "fake_playwright_for_test"
    try:
        register_provider(name, lambda config: None)
        assert not transport_is_browser_backed(name), "silence means not a browser"
        register_provider(name, lambda config: None, browser_backed=True)
        assert transport_is_browser_backed(name)
    finally:
        conversation_module._PROVIDERS.pop(name, None)
        _BROWSER_BACKED.discard(name)
    assert browser_backed_providers() == [BROWSER_PROVIDER]


def test_the_remedies_live_with_the_registry_not_in_the_orchestrator():
    """The seam this change had to respect rather than breach. Per-transport
    ADVICE is by definition provider-specific, so the obvious place to put it —
    beside the handler that quotes it — is the one place it may not go.
    `test_codex_app_server.py` already forbids the app-server names in
    `orchestrator.py`; this widens the same check to every registered provider,
    so the next remedy added cannot leak a name back into that module."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "orchestrator.py"
    ).read_text(encoding="utf-8")
    for name in ("codex_cli", "codex_app_server", "browser_chatgpt", BROWSER_PROVIDER):
        assert name not in source
    # And the advice really is reachable for each of them.
    from autoloop.conversation import transport_remedy

    for name in ("codex_cli", "codex_app_server"):
        assert "codex" in transport_remedy(name)


def test_the_orchestrator_asks_the_ACTIVE_provider(tmp_path):
    """A run that failed over to the browser answers for the browser, and a run
    configured for the browser that failed over to codex answers for codex —
    `state.active_provider` wins over the config, exactly as `active_provider()`
    already decides everywhere else."""
    orch, _, _ = build(tmp_path / "a", SilentIdempotentClient(), provider="codex_cli")
    assert orch._transport_is_browser_backed() is False
    orch.state.active_provider = BROWSER_PROVIDER
    assert orch._transport_is_browser_backed() is True

    other, _, _ = build(
        tmp_path / "b", SilentIdempotentClient(), provider=BROWSER_PROVIDER
    )
    assert other._transport_is_browser_backed() is True
    other.state.active_provider = "codex_cli"
    assert other._transport_is_browser_backed() is False


# ---- 1. a codex fault launches no browser and spends no browser budget -------


def test_a_codex_await_failure_launches_no_browser(tmp_path, restarts):
    """THE incident, reproduced through `run()` with a real `CodexConversation`.

    `await_response` raises `ResponseTimeoutError` because this process holds no
    reply for the request — which is what a restarted loop always finds. Before
    this change that reached `_handle_response_start_timeout` ->
    `_handle_browser_failure` and started Chrome.
    """
    client = CodexConversation(FakeRunner([result(stdout=stop_block())]))
    # The request was submitted by a PREVIOUS process, so this client's stash is
    # empty — and `reconcile` truthfully says the reply is absent.
    state = state_in(Phase.AWAITING, pending_request=pending(submitted=True, send_attempted=True))
    orch, store, config = build(
        tmp_path,
        client,
        state=state,
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=0.0,
    )

    orch.run(max_steps=1)

    assert restarts == [], "no browser may be launched for a subprocess fault"
    assert orch.state.browser_restart_skips == 0
    assert rows(config, "browser_error") == []
    assert rows(config, "browser_restarted") == []
    assert rows(config, "browser_restart_skipped") == []


def test_a_codex_submit_failure_launches_no_browser(tmp_path, restarts):
    """The other entry point, and a different exception type: the CLI could not
    be launched at all, so `submit` raises a plain `BrowserError` from
    `submitting`. Same routing question, same answer."""
    client = CodexConversation(FakeRunner([BrowserError("the codex CLI was not found")]))
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.SUBMITTING),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=0.0,
    )

    orch.run(max_steps=1)

    assert restarts == []
    assert orch.state.browser_restart_skips == 0
    assert rows(config, "browser_error") == []
    transport = rows(config, "transport_error")
    assert transport and transport[0]["data"]["provider"] == "codex_cli"
    assert transport[0]["data"]["kind"] == "BrowserError"


def test_a_codex_fault_never_spends_the_browser_restart_skip_budget(tmp_path, restarts):
    """The counter that ended the measured run. A live cooldown turns every
    browser failure into a `browser_restart_skips` charge and ends in the
    `browser_restart_cooldown_blocked` park; a codex fault must not be able to
    reach either, however many times it happens."""
    client = PlainClient()
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=99, max_browser_restart_skips=1),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=600.0,
    )

    orch.run(max_steps=5)

    assert restarts == []
    assert orch.state.browser_restart_skips == 0
    assert park_code(config) != "browser_restart_cooldown_blocked"
    assert orch.state.phase == Phase.AWAITING.value, "retried on the ordinary budget"
    assert orch.state.consecutive_failures == 5


def test_a_codex_fault_still_spends_the_ORDINARY_failure_budget(tmp_path, restarts):
    """The exemption is for the BROWSER budget alone. A transport that keeps
    failing must still end somewhere — un-charging `consecutive_failures` would
    let a permanently broken codex retry forever, which is a worse version of
    the fault this replaces."""
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=2),
        restart_command=RESTART_COMMAND,
    )

    orch.run(max_steps=4)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "transport_failure_budget_exhausted"
    assert restarts == []


# ---- 2. the park names a fix the operator can actually perform ---------------


def test_the_codex_park_names_the_transport_and_not_the_browser(tmp_path, restarts):
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=1),
        restart_command=RESTART_COMMAND,
    )

    orch.run(max_steps=3)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert park_code(config) == "transport_failure_budget_exhausted"
    question = orch.state.question or ""
    # It names the transport in use...
    assert "codex_cli" in question
    # ...and a remedy that acts on THAT transport.
    assert "codex exec" in question
    assert "codex_invocation_failed" in question
    assert "conversation.fallback_provider" in question
    # ...and never sends the investigation to a subsystem this run does not use.
    assert "chrome_restart" not in question
    assert "restart_cooldown_seconds" not in question
    assert "browser.restart" not in question
    # The park is resumable from where it happened, like every other park here.
    assert orch.state.resume_phase == Phase.AWAITING.value


def test_the_app_server_park_names_its_own_records(tmp_path, restarts):
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        provider="codex_app_server",
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=1),
        restart_command=RESTART_COMMAND,
    )

    orch.run(max_steps=3)

    question = orch.state.question or ""
    assert "codex_app_server" in question
    assert "codex_app_server_failed" in question
    assert "chrome_restart" not in question


def test_an_unregistered_transport_still_gets_actionable_advice(tmp_path, restarts):
    """The generic branch. A provider with no entry in `_TRANSPORT_REMEDIES`
    must still be told what to look at — silence here would recreate the
    original fault in a new place."""
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        provider="some_future_adapter",
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=1),
        restart_command=RESTART_COMMAND,
    )

    orch.run(max_steps=3)

    question = orch.state.question or ""
    assert "some_future_adapter" in question
    assert "conversation.provider" in question
    assert "conversation.fallback_provider" in question
    assert "chrome_restart" not in question


def test_the_park_records_a_blocker_an_operator_can_read(tmp_path, restarts):
    from autoloop.blockers import BlockerStore

    orch, store, config = build(
        tmp_path,
        PlainClient(),
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=1),
        restart_command=RESTART_COMMAND,
    )
    orch._blocker_store = BlockerStore(tmp_path / "blockers")

    orch.run(max_steps=3)

    open_blockers = orch._blocker_store.open_blockers()
    assert len(open_blockers) == 1
    parked = open_blockers[0]
    assert parked.code == "transport_failure_budget_exhausted"
    assert parked.question == orch.state.question
    assert "codex_cli" in parked.detail


# ---- 3. an unrecoverable `awaiting` is REPLAYED, on the declaration alone ----


def test_a_loop_restarted_in_awaiting_re_runs_and_completes_the_round(tmp_path):
    """TWO orchestrators over one state directory — the honest form of "the loop
    was restarted". The first submits and leaves the phase at `awaiting`; the
    second inherits that phase with a FRESH `CodexConversation` whose in-memory
    stash is empty, exactly as a new process does."""
    first_client = CodexConversation(FakeRunner([result(stdout=stop_block())]))
    orch, store, config = build(
        tmp_path, first_client, state=state_in(Phase.SUBMITTING)
    )
    orch.run(max_steps=1)
    assert orch.state.phase == Phase.AWAITING.value
    assert len(first_client._runner.prompts) == 1

    reloaded = StateStore(config.state_file).load()
    assert reloaded.phase == Phase.AWAITING.value
    assert reloaded.pending_request.submitted is True

    second_client = CodexConversation(FakeRunner([result(stdout=stop_block("second run"))]))
    resumed = Orchestrator(
        config=config,
        store=store,
        state=reloaded,
        policy=PolicyEngine(config.policy),
        git=orch._git,
        executor=orch._executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: second_client,
        registry=TaskRegistry(),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
    )

    # Step 1: the await fails, the reply is confirmed absent, the request is
    # re-issued rather than left waiting for something that cannot arrive.
    resumed.run(max_steps=1)
    assert resumed.state.phase == Phase.SUBMITTING.value
    assert resumed.state.pending_request.replays_used == 1
    assert resumed.state.pending_request.send_attempted is False
    authorized = rows(config, "transport_replay_authorized")
    assert authorized and authorized[-1]["data"]["provider"] == "codex_cli"
    assert authorized[-1]["data"]["reason_code"] == (
        "idempotent_submit_unrecoverable_reply"
    )

    # Steps 2-3: the re-run lands and the round finishes on the reviewer's
    # verdict, which is the whole point — not "it did not crash".
    resumed.run(max_steps=3)
    assert resumed.state.phase == Phase.STOPPED.value
    assert len(second_client._runner.prompts) == 1, "exactly one re-invocation"
    # The SAME request, byte for byte — a replay re-issues what was reviewed,
    # it does not rebuild a fresh packet.
    assert second_client._runner.prompts[0] == PROMPT


def test_the_replay_re_sends_the_same_request_id_and_bytes(tmp_path):
    client = CodexConversation(FakeRunner([result(stdout=stop_block())]))
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
    )

    orch.run(max_steps=2)  # replay, then the re-issued submit

    assert client._runner.prompts == [PROMPT]
    assert orch.state.pending_request is None or (
        orch.state.pending_request.request_id == RID
    )


def test_a_transport_without_the_declaration_is_never_re_run(tmp_path, restarts):
    """`idempotent_submit` IS the licence, and only it. A transport that has not
    said a failed send appended nothing must not be re-invoked — that is exactly
    how a duplicate turn gets posted into a shared thread."""
    client = PlainClient()
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=99),
    )

    orch.run(max_steps=2)

    assert client.submitted == [], "nothing may be re-sent"
    # What it does INSTEAD: stays in `awaiting`, retrying on the ordinary
    # failure budget, exactly as it did before replay existed.
    assert orch.state.phase == Phase.AWAITING.value
    assert orch.state.pending_request.replays_used == 0
    assert orch.state.consecutive_failures == 2
    assert rows(config, "transport_replay_authorized") == []
    declined = rows(config, "transport_replay_declined")
    assert declined and declined[0]["data"]["reason_code"] == "not_idempotent"


def test_the_app_server_transport_is_not_re_run(tmp_path):
    """The shipped non-idempotent transport, asserted as the class attribute the
    orchestrator actually probes rather than as a second fake."""
    from autoloop.codex.app_server_conversation import CodexAppServerConversation

    assert CodexAppServerConversation.idempotent_submit is False
    assert CodexConversation.idempotent_submit is True


def test_a_submit_side_timeout_is_never_read_as_a_replay_signal(tmp_path, restarts):
    """`SubprocessCodexRunner.run` raises the SAME `ResponseTimeoutError` type
    when the CLI outruns `codex.timeout_seconds` — but from `submit`, where a
    process may still be alive and the existing send machinery already owns the
    decision. Only `awaiting` is a replay signal."""
    client = CodexConversation(
        FakeRunner([ResponseTimeoutError("codex did not finish within 900.0s")])
    )
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.SUBMITTING),
        policy=PolicyConfig(max_consecutive_failures=99),
        restart_command=RESTART_COMMAND,
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.SUBMITTING.value
    assert orch.state.pending_request.replays_used == 0
    assert rows(config, "transport_replay_authorized") == []
    assert restarts == []
    assert rows(config, "transport_error")


def test_a_reply_that_is_actually_there_is_never_replayed(tmp_path):
    """Presence outranks everything. A transport whose `reconcile` says the
    reply exists has an ordinary fault, not an unsatisfiable phase."""

    class HoldsTheReply(SilentIdempotentClient):
        def reconcile(self, rid):
            return True

    client = HoldsTheReply()
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=99),
    )

    orch.run(max_steps=1)

    assert client.submitted == []
    assert orch.state.phase == Phase.AWAITING.value
    assert orch.state.pending_request.replays_used == 0
    assert rows(config, "transport_replay_authorized") == []


def test_a_reconcile_that_cannot_answer_does_not_authorize_a_replay(tmp_path):
    """FAIL CLOSED. "Could not ask" is not "absent", and a check that quietly
    passes when what it needs is unavailable is the shape this whole task is
    about."""
    client = ExplodingReconcileClient()
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=99),
    )

    orch.run(max_steps=1)

    assert client.submitted == []
    assert orch.state.pending_request.replays_used == 0
    declined = rows(config, "transport_replay_declined")
    assert declined and declined[0]["data"]["reason_code"] == "reconcile_failed"


def test_the_replay_budget_is_bounded_and_ends_in_a_park(tmp_path, restarts):
    """A transport that confirms every send and answers none would re-invoke a
    reviewer forever. The bound turns that into a park."""
    client = SilentIdempotentClient()
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=1),
        restart_command=RESTART_COMMAND,
    )

    orch.run(max_steps=40)

    assert len(client.submitted) == MAX_AWAIT_REPLAYS
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "transport_failure_budget_exhausted"
    declined = rows(config, "transport_replay_declined")
    assert any(r["data"]["reason_code"] == "replay_budget" for r in declined)
    assert restarts == []


def test_the_replay_count_survives_a_restart(tmp_path):
    """The bound is on the REQUEST, not on the process — otherwise every restart
    would refill it and the bound would not exist."""
    client = SilentIdempotentClient()
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_consecutive_failures=99),
    )

    orch.run(max_steps=2)  # replay -> submit -> awaiting again

    reloaded = StateStore(config.state_file).load()
    assert reloaded.pending_request.replays_used == 1


def test_the_replay_does_not_charge_the_failure_budget(tmp_path):
    """A recovery that was PERFORMED is not evidence recovery fails — the same
    rule `_handle_browser_failure` already applies to a restart that ran."""
    client = CodexConversation(FakeRunner([result(stdout=stop_block())]))
    orch, store, config = build(
        tmp_path,
        client,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
    )

    orch.run(max_steps=1)

    assert orch.state.pending_request.replays_used == 1
    assert orch.state.consecutive_failures == 0
    assert rows(config, "transport_error") == []


def test_a_provider_handover_gives_the_new_transport_a_fresh_replay_budget(tmp_path):
    """`replays_used` counts re-invocations of the transport that had them. The
    fallback has never seen this request."""
    req = pending(submitted=True)
    req.replays_used = MAX_AWAIT_REPLAYS
    state = state_in(Phase.SUBMITTING, pending_request=req)

    class QuotaClient(PlainClient):
        def submit(self, rid, prompt):
            from autoloop.errors import QuotaExhaustedError

            raise QuotaExhaustedError("allowance spent")

    clients = {"codex_cli": QuotaClient(), BROWSER_PROVIDER: PlainClient()}
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    git = FakeGit(repo_root)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        conversation=ConversationConfig(
            provider="codex_cli", fallback_provider=BROWSER_PROVIDER
        ),
    )
    store = StateStore(config.state_file)
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
        client_factory=lambda: clients["codex_cli"],
        provider_factory=lambda provider: clients[provider],
        registry=TaskRegistry(),
        task_store=TaskStore(config.tasks_file),
        manifest_store=ManifestStore(config.manifests_dir),
    )

    orch.run(max_steps=1)

    assert orch.state.active_provider == BROWSER_PROVIDER
    assert orch.state.pending_request.replays_used == 0


# ---- 4. the browser provider is untouched -----------------------------------


def test_the_browser_provider_still_restarts_through_the_new_dispatch(tmp_path, restarts):
    """The bound this change must not break, checked at the NEW seam: a browser
    fault routed through `_route_transport_fault` still reaches
    `_handle_browser_failure`, still runs the restart command, and still leaves
    the fault free on the failure budget when the restart succeeded.

    (`test_rounds_and_restart.py` and `test_transport_recovery.py` assert the
    rest of the browser behaviour and are deliberately untouched by this task.)
    """

    class DeadBrowser(PlainClient):
        def attach(self):
            from autoloop.errors import SessionLostError

            raise SessionLostError("the CDP connection died")

    orch, store, config = build(
        tmp_path,
        DeadBrowser(),
        provider=BROWSER_PROVIDER,
        state=state_in(Phase.SUBMITTING),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=0.0,
    )

    orch.run(max_steps=1)

    assert restarts == [list(RESTART_COMMAND)]
    browser_errors = rows(config, "browser_error")
    assert browser_errors and browser_errors[0]["data"]["recovered"] == "restarted"
    assert orch.state.consecutive_failures == 0
    assert rows(config, "transport_error") == []


def test_the_browser_provider_still_parks_on_the_cooldown(tmp_path, restarts):
    """The park this task must NOT delete: for a browser run,
    `browser_restart_cooldown_blocked` and its advice are correct."""

    class DeadBrowser(PlainClient):
        def attach(self):
            from autoloop.errors import SessionLostError

            raise SessionLostError("the CDP connection died")

    orch, store, config = build(
        tmp_path,
        DeadBrowser(),
        provider=BROWSER_PROVIDER,
        state=state_in(Phase.SUBMITTING),
        policy=PolicyConfig(max_browser_restart_skips=1),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=600.0,
    )

    orch.run(max_steps=4)

    assert park_code(config) == "browser_restart_cooldown_blocked"
    assert "restart_cooldown_seconds" in (orch.state.question or "")
    assert "chrome_restart" in (orch.state.question or "")


def test_a_browser_rate_limit_still_reaches_the_unattachable_recovery(tmp_path, restarts):
    """The rate-limit guard added for non-browser transports must not disable
    the browser's own zero-target recovery."""
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        provider=BROWSER_PROVIDER,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=0.0,
    )
    orch._attachable_page_targets = lambda: 0
    orch._sleep = lambda seconds: None

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert restarts == [list(RESTART_COMMAND)]


def test_a_rate_limit_on_a_non_browser_transport_never_restarts(tmp_path, restarts):
    """A codex transport does not raise `RateLimitedError` today
    (`codex/protocol_errors.py` says so explicitly). If one ever does, the
    classification's third world — reached by dialling `browser.cdp_url`, which
    answers about whatever Chrome happens to be running on this HOST — must not
    turn it into a browser restart."""
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        provider="codex_cli",
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=0.0,
    )
    orch._attachable_page_targets = lambda: 0  # a Chrome with no tabs, on this host
    orch._sleep = lambda seconds: None

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert restarts == []
    limited = rows(config, "rate_limited")
    assert limited and limited[-1]["data"]["classification"] == "throttled"
    assert "drives no browser" in limited[-1]["data"]["evidence"]


def test_the_rate_limit_park_does_not_send_a_codex_run_to_the_browser(tmp_path, restarts):
    """The second half of the same handler, and the one an adversarial pass
    found after the restart was already guarded. Exhausting
    `max_rate_limit_backoffs` writes a park, and its unsighted-modal branch
    tells the operator to `curl .../json/list` and says "a restart IS the
    remedy" — advice about someone else's program on a run that drives no
    browser. Half a door shut is not shut."""
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        provider="codex_cli",
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_rate_limit_backoffs=1),
        restart_command=RESTART_COMMAND,
        restart_cooldown_seconds=0.0,
    )
    orch._attachable_page_targets = lambda: 0
    orch._sleep = lambda seconds: None

    for _ in range(2):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "rate_limited"
    question = orch.state.question or ""
    assert "codex_cli" in question
    assert "json/list" not in question
    assert "restart IS the remedy" not in question
    assert "What the browser looked like" not in question
    assert restarts == []


class RejectingClient(PlainClient):
    """A transport whose every send is DISPROVEN — what `codex_cli` returns for
    every non-zero exit and for a clean exit with empty stdout."""

    def submit(self, rid, prompt):
        self.submitted.append(rid)
        return SubmitResult.REJECTED


def test_two_disproven_codex_sends_never_park_on_browser_advice(tmp_path, restarts):
    """THE reachable door, and the one an adversarial pass found last: two
    consecutive REJECTED sends leave `_step_submission_rejected` WITHOUT passing
    any fault handler, so none of the routing guards above see them. They used
    to reach `_attempt_rotation`, and with `browser.project_url` unset — the
    normal codex deployment — the park told the operator to set it "to the
    ChatGPT project this conversation belongs to". Since brw-15 the rotation is
    gone entirely and the park is `send_rejected_twice`; the property this test
    was written for is unchanged and is the reason it survives — the remedy
    names the transport actually in use."""
    client = RejectingClient()
    orch, store, config = build(
        tmp_path,
        client,
        provider="codex_cli",
        state=state_in(Phase.SUBMITTING),
        restart_command=RESTART_COMMAND,
    )

    # submit -> rejected -> reconcile+resend -> rejected -> reconcile -> park
    orch.run(max_steps=5)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert park_code(config) == "send_rejected_twice"
    question = orch.state.question or ""
    assert "codex_cli" in question
    assert "codex exec" in question, "the remedy acts on the transport in use"
    assert "browser.project_url" not in question
    assert "chrome_restart" not in question
    assert "conversation_url" not in question, "no browser advice for a codex run"
    # Nothing was rotated and no rotation budget was spent.
    assert orch.state.rotations == 0
    assert rows(config, "conversation_rotated") == []
    assert restarts == []


def test_a_stale_project_url_changes_nothing_on_a_codex_run(tmp_path, restarts):
    """The other half, and the worse one: an operator who moved from the browser
    to codex and left `[browser]` populated used to PASS both rotation
    preconditions, spend `state.rotations`, and only then fail inside
    `_rotate_conversation` because the transport has no `retarget`. Nothing
    consults `browser.project_url` on this path any more, so a leftover config
    value cannot cost a budget or change a single word of the park."""
    client = RejectingClient()
    orch, store, config = build(
        tmp_path,
        client,
        provider="codex_cli",
        state=state_in(Phase.SUBMITTING),
        project_url="https://chatgpt.com/g/g-p-leftover/project",
    )

    orch.run(max_steps=5)

    assert orch.state.rotations == 0, "the budget must not be spent"
    assert park_code(config) == "send_rejected_twice"
    question = orch.state.question or ""
    assert "browser.project_url" not in question
    assert "g-p-leftover" not in question
    assert "codex exec" in question


def test_the_browser_parks_on_the_same_code_with_its_own_remedy(tmp_path, restarts):
    """The bound on the two tests above: the split they assert is about the
    REMEDY, not about the outcome. A browser run reaching the same two disproven
    sends parks on the same `send_rejected_twice` code — it is the same fault —
    and gets browser-shaped advice instead of codex-shaped advice.

    Before brw-15 the two transports diverged at the code as well (the browser's
    missing `browser.project_url` parked `rotation_unavailable` naming that
    key), which is the divergence the removal collapses."""
    client = RejectingClient()
    orch, store, config = build(
        tmp_path,
        client,
        provider=BROWSER_PROVIDER,
        state=state_in(Phase.SUBMITTING),
    )

    orch.run(max_steps=5)

    assert park_code(config) == "send_rejected_twice"
    question = orch.state.question or ""
    assert "browser.conversation_url" in question, "the browser's own remedy"
    assert "codex exec" not in question
    assert "browser.project_url" not in question, "nothing consults it any more"
    assert orch.state.rotations == 0


def test_the_browser_rate_limit_park_keeps_its_exact_wording(tmp_path, restarts):
    """The bound on the test above: the browser's two branches are byte-identical
    to what `test_rounds_and_restart.py` already pins, including the evidence
    label the third branch made a variable."""
    orch, store, config = build(
        tmp_path,
        PlainClient(),
        provider=BROWSER_PROVIDER,
        state=state_in(Phase.AWAITING, pending_request=pending(submitted=True)),
        policy=PolicyConfig(max_rate_limit_backoffs=1),
    )
    orch._sleep = lambda seconds: None  # `_attachable_page_targets` stays None

    for _ in range(2):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    question = orch.state.question or ""
    assert park_code(config) == "rate_limited"
    assert "CHECK THE BROWSER BEFORE WAITING" in question
    assert "/json/list" in question
    assert "What the browser looked like when this was classified:" in question
