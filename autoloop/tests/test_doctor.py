"""Doctor preflight with every boundary mocked: no Chrome, no network, no
playwright — and proof that it never submits a message."""

import json
import socket

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.doctor import DoctorProbes, exit_code, run_doctor
from autoloop.errors import LoginExpiredError
from autoloop.lock import LoopLock
from autoloop.policy import PolicyConfig

URL = "https://chatgpt.com/c/valid-id-123"


class FakeConversation:
    def __init__(self, login_expired=False):
        self.login_expired = login_expired
        self.opened = 0
        self.submits = []
        self.closed = False

    def open(self):
        self.opened += 1
        if self.login_expired:
            raise LoginExpiredError("logged out")

    def messages(self):
        return [("user", "hi")]

    def already_submitted(self, request_id):
        return False

    def submit(self, request_id, prompt):
        self.submits.append((request_id, prompt))

    def await_response(self, request_id):
        raise AssertionError("doctor must never await a response")

    def close(self):
        self.closed = True


def make_config(tmp_path, **policy) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(**policy),
        state_dir=tmp_path / ".al",
    )


def probes(conversation=None, cdp_ok=True, playwright=True) -> DoctorProbes:
    def probe_cdp(url):
        if not cdp_ok:
            raise OSError("connection refused")
        return json.dumps({"Browser": "Chrome/126"})

    return DoctorProbes(
        probe_cdp=probe_cdp,
        playwright_present=lambda: playwright,
        conversation_factory=(lambda: conversation) if conversation else None,
    )


def by_name(results):
    return {r.name: r for r in results}


def init_repo(path):
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "feature/x", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.c"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "commit.gpgsign=false", "commit", "-qm", "i"],
        check=True,
    )


def test_all_green(tmp_path):
    init_repo(tmp_path)
    conversation = FakeConversation()
    results = run_doctor(make_config(tmp_path), tmp_path, probes(conversation))
    named = by_name(results)
    for check in ("config", "state_dir", "lock", "cdp", "playwright", "provider",
                  "conversation_url", "browser_live"):
        assert named[check].status == "ok", (check, named[check].detail)
    assert exit_code(results) == 0
    # non-destructive: opened but NEVER submitted
    assert conversation.opened == 1
    assert conversation.submits == []
    assert conversation.closed


def test_git_identity_and_protected_branch_warning(tmp_path):
    import subprocess

    init_repo(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "branch", "-m", "main"], check=True)
    results = run_doctor(make_config(tmp_path), tmp_path, probes(FakeConversation()))
    named = by_name(results)
    assert named["git"].status == "ok"
    assert "branch=main" in named["git"].detail
    assert named["branch_policy"].status == "warn"  # main is protected by default
    assert "DENIED" in named["branch_policy"].detail


def test_cdp_unreachable_fails_and_skips_live(tmp_path):
    results = run_doctor(make_config(tmp_path), tmp_path, probes(cdp_ok=False))
    named = by_name(results)
    assert named["cdp"].status == "fail"
    assert named["browser_live"].status == "skip"
    assert exit_code(results) == 1


def test_playwright_missing_fails(tmp_path):
    results = run_doctor(make_config(tmp_path), tmp_path, probes(playwright=False))
    assert by_name(results)["playwright"].status == "fail"


def test_logged_out_browser_fails(tmp_path):
    conversation = FakeConversation(login_expired=True)
    results = run_doctor(make_config(tmp_path), tmp_path, probes(conversation))
    named = by_name(results)
    assert named["browser_live"].status == "fail"
    assert "logged out" in named["browser_live"].detail
    assert conversation.closed


def test_stale_lock_reported_as_failure(tmp_path):
    config = make_config(tmp_path)
    lock = LoopLock(config.state_dir)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": 99999999,
                "hostname": socket.gethostname(),
                "started_at": "t",
                "run_id": "r",
                "state_dir": str(config.state_dir),
            }
        ),
        encoding="utf-8",
    )
    results = run_doctor(config, tmp_path, probes(FakeConversation()))
    named = by_name(results)
    assert named["lock"].status == "fail"
    assert "unlock" in named["lock"].detail


def test_bad_conversation_url_fails(tmp_path):
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://example.com/not-chatgpt"),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
    )
    results = run_doctor(config, tmp_path, probes(FakeConversation()))
    assert by_name(results)["conversation_url"].status == "fail"
