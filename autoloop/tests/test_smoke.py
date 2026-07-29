"""smoke-browser command through the real CLI with a fake registered
conversation provider: full contract path (request id, stamp, parser,
transcript), clean terminal state, executor never invoked."""

import json

from autoloop import cli
from autoloop import conversation as conversation_module
from autoloop.conversation import register_provider


class FakeSmokeConversation:
    def __init__(self, reply):
        self.reply = reply
        self.submitted = []
        self.already = set()

    def open(self):
        pass

    def already_submitted(self, request_id):
        return request_id in self.already

    def submit(self, request_id, prompt):
        self.submitted.append((request_id, prompt))
        self.already.add(request_id)

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
        f'state_dir = "{tmp_path / ".al"}"\n',
        encoding="utf-8",
    )
    return config


def run_smoke(tmp_path, conversation):
    provider = "fake_smoke_provider"
    register_provider(provider, lambda config: conversation)
    try:
        config = write_config(tmp_path, provider)
        return cli.main(["smoke-browser", "--config", str(config)])
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
