"""smoke-browser command through the real CLI with a fake registered
conversation provider: full contract path (request id, stamp, parser,
transcript), clean terminal state, executor never invoked.

The NAME is historical. Since brw-16 (2026-08-25) no browser-backed provider is
registered, so this command no longer defaults to one: it smokes whatever
`conversation.provider` names, and refuses up front — before the loop lock —
when the provider it was asked for is not registered. A command that silently
could not work was the outcome that change had to avoid.
"""

import json
import re

from autoloop import cli
from autoloop.browser.chatgpt import SubmitResult
from autoloop import conversation as conversation_module
from autoloop.conversation import register_provider


class FakeSmokeConversation:
    def __init__(self, reply):
        self.reply = reply
        self.submitted = []
        self.already = set()
        self.reconciles = []

    def attach(self):
        pass

    def has_request(self, request_id):
        return request_id in self.already

    def reconcile(self, request_id):
        self.reconciles.append(request_id)
        return request_id in self.already

    def submit(self, request_id, prompt):
        self.submitted.append((request_id, prompt))
        self.already.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        return self.reply(self) if callable(self.reply) else self.reply

    def close(self):
        pass


STOP_REPLY = (
    '```json\n{"version": 3, "decision": "stop", "reason": "smoke test acknowledged"}\n```'
)


def write_config(tmp_path, provider):
    config = tmp_path / "config.toml"
    config.write_text(
        "[browser]\n"
        'conversation_url = "https://chatgpt.com/c/smoke"\n'
        "[conversation]\n"
        f'provider = "{provider}"\n'
        "[paths]\n"
        f'state_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    return config


def run_smoke(tmp_path, conversation, *, explicit_provider=True):
    provider = "fake_smoke_provider"
    register_provider(provider, lambda config: conversation)
    try:
        config = write_config(tmp_path, provider)
        # `--provider` is still passed explicitly by most tests here, because
        # naming the seat under test is what this command is for. Since brw-16
        # it is no longer REQUIRED: omitting it reads `conversation.provider`,
        # which is the same fake in this config — see the pair of tests at the
        # end of this file.
        argv = ["smoke-browser", "--config", str(config)]
        if explicit_provider:
            argv += ["--provider", provider]
        return cli.main(argv)
    finally:
        conversation_module._PROVIDERS.pop(provider, None)


def test_smoke_pass(tmp_path, capsys):
    conversation = FakeSmokeConversation(STOP_REPLY)
    assert run_smoke(tmp_path, conversation) == 0
    out = capsys.readouterr().out
    assert "PASS" in out
    # the one submitted prompt went through the normal machinery
    [(request_id, prompt)] = conversation.submitted
    assert request_id.startswith("alr-")
    assert "AUTOLOOP SMOKE TEST" in prompt
    assert "request_id:" in prompt and "report_sha256:" in prompt  # stamped context
    assert "RESPONSE FORMAT" in prompt  # contract attached
    # transcript recorded the exchange
    transcript = (tmp_path / ".al" / "transcript.jsonl").read_text()
    assert "request_submitted" in transcript
    assert "smoke test acknowledged" in transcript
    # terminal state is clean and isolated from the main session
    state = json.loads((tmp_path / ".al" / "smoke" / "state.json").read_text())
    assert state["phase"] == "stopped"
    assert not (tmp_path / ".al" / "state.json").exists()  # main session untouched


def test_smoke_fails_cleanly_on_wrong_decision(tmp_path, capsys):
    # ChatGPT misbehaves and answers ask_user -> smoke reports FAIL, exit 2.
    reply = '```json\n{"version": 3, "decision": "ask_user", "reason": "r", "question": "why?"}\n```'
    assert run_smoke(tmp_path, FakeSmokeConversation(reply)) == 2
    assert "FAIL" in capsys.readouterr().out


def test_smoke_never_invokes_executor(tmp_path):
    # An implement reply would need the executor — policy denies it, the
    # corrective loop runs out of its tiny iteration budget, and the
    # _SmokeNeverExecutor guarantees execution could never have happened.
    reply = '```json\n{"version": 3, "decision": "implement", "reason": "r", "task_id": "t1"}\n```'
    conversation = FakeSmokeConversation(reply)
    code = run_smoke(tmp_path, conversation)
    assert code == 2  # parked, not crashed — executor was never reached


def test_a_refused_smoke_reply_fault_stops_and_is_never_reported_as_a_pass(tmp_path):
    """WHY the two tests above still exit 2, now that a denied directive ends
    the loop in `stopped` rather than parking it.

    The smoke policy sets `max_policy_denials=0`, so the very first refused
    reply exhausts the denial budget, and an exhausted denial budget stops the
    loop (`orchestrator._to_fault_stop`) instead of parking it. That puts a
    MISBEHAVING reply in the same phase as a well-behaved one — `stopped` —
    and phase alone would have `smoke-browser` announce PASS for a reviewer
    that answered `ask_user`. `stop_kind` is what keeps them apart, and this
    test pins the distinction at the state level rather than only through the
    exit code, so a future change that reintroduced the collapse would fail
    here with a readable reason."""
    reply = '```json\n{"version": 3, "decision": "ask_user", "reason": "r", "question": "why?"}\n```'
    assert run_smoke(tmp_path, FakeSmokeConversation(reply)) == 2

    state = json.loads((tmp_path / ".al" / "smoke" / "state.json").read_text())
    assert state["phase"] == "stopped"  # the loop ended itself...
    assert state["stop_kind"] == "fault"  # ...but NOT the way a PASS ends
    assert state["question"] is None  # never parked: no human was asked anything
    assert state["resume_phase"] is None  # and nothing is resumable
    assert "policy-denied" in state["stop_reason"]


def test_a_passing_smoke_reply_is_classified_as_a_contract_stop(tmp_path):
    """The positive half: `smoke-browser` gates PASS on `stop_kind ==
    "contract"`, so a reviewer's real `stop` has to actually carry that value
    — otherwise the gate would fail closed on every healthy run."""
    assert run_smoke(tmp_path, FakeSmokeConversation(STOP_REPLY)) == 0
    state = json.loads((tmp_path / ".al" / "smoke" / "state.json").read_text())
    assert state["stop_kind"] == "contract"


def test_parked_smoke_state_is_archived(tmp_path):
    """A previous smoke session left parked in `awaiting` must be archived, not
    resumed — every smoke run starts clean."""
    smoke_dir = tmp_path / ".al" / "smoke"
    smoke_dir.mkdir(parents=True)
    stale = {
        "session_id": "old",
        "conversation_url": "https://chatgpt.com/c/smoke",
        "phase": "awaiting",
        "iteration": 7,
        "schema_version": 2,
        "created_at": "t",
        "updated_at": "t",
    }
    (smoke_dir / "state.json").write_text(json.dumps(stale), encoding="utf-8")

    assert run_smoke(tmp_path, FakeSmokeConversation(STOP_REPLY)) == 0

    backups = list(smoke_dir.glob("state.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text())["session_id"] == "old"
    fresh = json.loads((smoke_dir / "state.json").read_text())
    assert fresh["phase"] == "stopped"
    assert fresh["session_id"] != "old"
    assert fresh["iteration"] == 1


def test_no_git_write_is_reachable_from_a_smoke_request(tmp_path, monkeypatch):
    """Even if ChatGPT answers a smoke test with a commit approval, the smoke
    path must never reach a git write."""

    class GuardGit:
        def __init__(self, *args, **kwargs):
            pass

        # reads the context block needs
        def current_branch(self):
            return "feature/smoke"

        def head_sha(self):
            return "a" * 40

        def dirty_files(self):
            return []

        def commit(self, *args, **kwargs):
            raise AssertionError("smoke test must never commit")

        def push(self, *args, **kwargs):
            raise AssertionError("smoke test must never push")

    monkeypatch.setattr(cli, "GitGateway", GuardGit)

    def commit_reply(conversation):
        prompt = conversation.submitted[-1][1]
        stamp = {
            "request_id": re.search(r"request_id: (\S+)", prompt).group(1),
            "head_sha": re.search(r"head_sha: (\S+)", prompt).group(1),
            "report_sha256": re.search(r"report_sha256: (\S+)", prompt).group(1),
        }
        return (
            "```json\n"
            + json.dumps(
                {
                    "version": 3,
                    "decision": "commit",
                    "reason": "trying to sneak a commit through the smoke test",
                    "commit": {"message": "nope", "paths": ["README.md"]},
                    "reviewed": stamp,
                }
            )
            + "\n```"
        )

    # Not a pass: the run ends non-zero. What matters is that no git write ran
    # (GuardGit would have raised) and the loop refused it deterministically.
    assert run_smoke(tmp_path, FakeSmokeConversation(commit_reply)) == 2


# ---- single-round-trip guarantees ------------------------------------------


def test_smoke_policy_allows_exactly_one_of_everything(tmp_path, monkeypatch):
    """The smoke harness must not negotiate over several messages: one
    iteration, zero parse retries, zero denial retries, one failure ends it."""
    captured = {}
    real_engine = cli.PolicyEngine

    def spy(config):
        captured["config"] = config
        return real_engine(config)

    monkeypatch.setattr(cli, "PolicyEngine", spy)
    assert run_smoke(tmp_path, FakeSmokeConversation(STOP_REPLY)) == 0
    policy = captured["config"]
    assert policy.max_iterations == 1
    assert policy.max_parse_retries == 0
    assert policy.max_policy_denials == 0
    assert policy.max_consecutive_failures == 1


def test_smoke_sends_exactly_one_request_then_fails_on_malformed_reply(tmp_path):
    """A malformed reply is a smoke FAILURE — never a corrective re-prompt."""
    conversation = FakeSmokeConversation("Sure, everything looks fine to me!")
    assert run_smoke(tmp_path, conversation) == 2
    assert len(conversation.submitted) == 1  # exactly one request, no retry
    state = json.loads((tmp_path / ".al" / "smoke" / "state.json").read_text())
    assert state["phase"] == "needs_user"
    assert state["iteration"] == 1
    assert state["parse_retries"] == 1  # counted once, budget 0 → immediate stop


def test_smoke_receives_exactly_one_response(tmp_path):
    conversation = FakeSmokeConversation(STOP_REPLY)
    assert run_smoke(tmp_path, conversation) == 0
    assert len(conversation.submitted) == 1
    transcript = (tmp_path / ".al" / "transcript.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["type"] for line in transcript]
    assert kinds.count("request_submitted") == 1
    assert kinds.count("response_received") == 1


def test_smoke_cannot_construct_an_audit_executor_or_agent_runner(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("smoke test must never build an audit executor/agent runner")

    monkeypatch.setattr(cli, "AuditExecutor", boom)
    monkeypatch.setattr(cli, "ClaudeCliRunner", boom)
    assert run_smoke(tmp_path, FakeSmokeConversation(STOP_REPLY)) == 0


# ---- no browser default (brw-16) --------------------------------------------


def test_omitting_the_provider_smokes_the_configured_transport(tmp_path):
    """The replacement default. It used to be `browser_chatgpt` REGARDLESS of
    `conversation.provider`, on the reasoning that the browser was the fallback
    and so the seat most worth proving before it was needed. That provider is
    not registered any more, so the old default would make every bare
    `smoke-browser` fail on a name nothing can build."""
    conversation = FakeSmokeConversation(STOP_REPLY)
    assert run_smoke(tmp_path, conversation, explicit_provider=False) == 0
    assert len(conversation.submitted) == 1


def test_an_unregistered_provider_is_refused_before_anything_is_touched(tmp_path, capsys):
    """"Make it say so", checked in the way that matters: the refusal names the
    provider AND what is registered, and it happens before the loop lock is
    taken and before the previous smoke state is archived — a command that
    cannot build a client has nothing to smoke, and locking to discover that
    would block a real run for no reason."""
    config = write_config(tmp_path, "browser_chatgpt")
    smoke_state = tmp_path / ".al" / "smoke" / "state.json"
    smoke_state.parent.mkdir(parents=True)
    smoke_state.write_text('{"session_id": "previous"}', encoding="utf-8")

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "browser_chatgpt" in out, "the provider it could not build"
    assert "codex_cli" in out, "and what IS registered"
    assert json.loads(smoke_state.read_text())["session_id"] == "previous", (
        "nothing was archived, so no lock was taken and no state was disturbed"
    )
    assert not list(smoke_state.parent.glob("state.json.bak-*"))


def test_the_refusal_happens_before_the_loop_lock_is_taken(tmp_path, capsys):
    """"Before the lock" asserted directly rather than inferred.

    A lock owned by another HOST is live by definition (`LoopLock.is_live` fails
    closed for a pid it cannot verify), so `LoopLock(...)` would raise
    `LockHeldError` here and `cli.main` would print `error: another autoloop
    process holds …`. Seeing the refusal instead is what proves the check runs
    first — and it matters: an unbuildable provider has nothing to smoke, so
    blocking a real run to discover that would be a cost for no answer.
    """
    config = write_config(tmp_path, "browser_chatgpt")
    lock = tmp_path / ".al" / "LOCK"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "pid": 4242,
                "hostname": "some-other-machine",
                "started_at": "2026-08-25T00:00:00+00:00",
                "run_id": "r",
                "state_dir": str(tmp_path / ".al"),
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2

    out = capsys.readouterr().out
    assert "smoke-browser: FAIL" in out
    assert "not registered" in out
    assert "another autoloop process holds" not in out


def test_an_explicit_unregistered_provider_is_refused_too(tmp_path, capsys):
    """The other door into the same check: `--provider` names it directly."""
    config = write_config(tmp_path, "fake_smoke_provider")  # not registered here
    code = cli.main(
        ["smoke-browser", "--config", str(config), "--provider", "no_such_provider"]
    )
    assert code == 2
    assert "no_such_provider" in capsys.readouterr().out


def test_each_smoke_run_uses_a_fresh_request_id(tmp_path):
    first = FakeSmokeConversation(STOP_REPLY)
    assert run_smoke(tmp_path, first) == 0
    second = FakeSmokeConversation(STOP_REPLY)
    assert run_smoke(tmp_path, second) == 0
    first_id = first.submitted[0][0]
    second_id = second.submitted[0][0]
    assert first_id != second_id  # a new session id per run, so no id is reused
    assert first_id.endswith("-0001") and second_id.endswith("-0001")
