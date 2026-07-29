"""`python -m autoloop doctor` — non-destructive preflight.

Checks configuration, state dir, lock, git identity, branch policy, CDP
reachability, Playwright, provider registration, conversation URL shape, and
(when the browser stack is actually reachable) that the conversation opens
logged-in with resolvable composer/message selectors. It NEVER submits a
message.

Every external boundary is injectable (DoctorProbes) so the whole command is
unit-testable without Chrome, playwright, or a network.
"""

from __future__ import annotations

import importlib.util
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import AutoloopConfig
from .conversation import available_providers, create_conversation
from .errors import AutoloopError, BrowserError, LoginExpiredError
from .git_gateway import GitGateway
from .lock import LoopLock
from .policy import PolicyEngine

_CHATGPT_URL = re.compile(r"^https://chatgpt\.com/c/[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str


def _default_probe_cdp(url: str, timeout: float = 3.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local CDP
        return response.read(200).decode("utf-8", "replace")


def _default_playwright_present() -> bool:
    return importlib.util.find_spec("playwright") is not None


@dataclass
class DoctorProbes:
    probe_cdp: Callable[[str], str] = _default_probe_cdp
    playwright_present: Callable[[], bool] = field(
        default_factory=lambda: _default_playwright_present
    )
    conversation_factory: Callable | None = None  # defaults to the real provider


def run_doctor(
    config: AutoloopConfig, repo_root: Path, probes: DoctorProbes | None = None
) -> list[CheckResult]:
    probes = probes or DoctorProbes()
    results: list[CheckResult] = []

    def add(name: str, status: str, detail: str) -> None:
        results.append(CheckResult(name=name, status=status, detail=detail))

    # 1. config already loaded by the caller — record it.
    add("config", "ok", "parsed; unknown keys would have been rejected at load")

    # 2. state dir writable
    try:
        config.state_dir.mkdir(parents=True, exist_ok=True)
        probe = config.state_dir / ".doctor-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add("state_dir", "ok", str(config.state_dir))
    except OSError as exc:
        add("state_dir", "fail", f"not writable: {exc}")

    # 3. lock availability
    lock = LoopLock(config.state_dir)
    info = lock.read()
    if info is None:
        add("lock", "ok", "free")
    elif LoopLock.is_live(info):
        add("lock", "warn", f"held by a live process ({info.describe()})")
    else:
        add(
            "lock",
            "fail",
            f"stale lock ({info.describe()}) — recover with `python -m autoloop unlock`",
        )

    # 4./5. git identity + branch policy
    git = GitGateway(repo_root, PolicyEngine(config.policy))
    try:
        branch = git.current_branch()
        head = git.head_sha()
        add("git", "ok", f"branch={branch} head={head[:12]} dirty={len(git.dirty_files())}")
        if branch in config.policy.protected_branches and not config.policy.allow_protected_push:
            add(
                "branch_policy",
                "warn",
                f"'{branch}' is protected — pushes will be DENIED "
                "(policy.protected_branches / allow_protected_push)",
            )
        else:
            add("branch_policy", "ok", f"pushes to '{branch}' are permitted by policy")
    except AutoloopError as exc:
        add("git", "fail", str(exc))

    # 6. CDP endpoint
    cdp_ok = False
    cdp_url = config.browser.cdp_url.rstrip("/") + "/json/version"
    try:
        payload = probes.probe_cdp(cdp_url)
        cdp_ok = True
        add("cdp", "ok", f"{cdp_url} reachable ({payload.strip()[:60]}...)")
    except Exception as exc:
        add(
            "cdp",
            "fail",
            f"{cdp_url} unreachable ({exc}) — launch the dedicated profile with "
            "--remote-debugging-port",
        )

    # 7. playwright
    playwright_ok = bool(probes.playwright_present())
    add(
        "playwright",
        "ok" if playwright_ok else "fail",
        "importable" if playwright_ok else "not installed — pip install -r autoloop/requirements.txt",
    )

    # 8. provider registration
    provider = config.conversation.provider
    if provider in available_providers():
        add("provider", "ok", provider)
    else:
        add("provider", "fail", f"'{provider}' not registered ({available_providers()})")

    # 9. conversation URL shape (browser_chatgpt only)
    if provider == "browser_chatgpt":
        if "REPLACE-ME" in config.browser.conversation_url:
            add(
                "conversation_url",
                "fail",
                "still the config.example.toml placeholder — set "
                "browser.conversation_url to your real conversation",
            )
        elif _CHATGPT_URL.match(config.browser.conversation_url):
            add("conversation_url", "ok", config.browser.conversation_url)
        else:
            add(
                "conversation_url",
                "fail",
                f"'{config.browser.conversation_url}' does not look like "
                "https://chatgpt.com/c/<id>",
            )

    # 10. live browser: login + conversation + selectors. Never submits.
    factory = probes.conversation_factory
    if factory is None:
        if not (cdp_ok and playwright_ok):
            add("browser_live", "skip", "skipped — CDP or playwright unavailable")
            return results

        def factory():
            return create_conversation(provider, config)

    conversation = None
    try:
        conversation = factory()
        conversation.open()  # navigates, checks login + composer selector
        messages = getattr(conversation, "messages", None)
        if callable(messages):
            count = len(messages())
            add(
                "browser_live",
                "ok",
                f"logged in; conversation open; composer + message selectors "
                f"resolve ({count} messages visible)",
            )
        else:
            add("browser_live", "ok", "conversation opened (provider exposes no message probe)")
    except LoginExpiredError as exc:
        add("browser_live", "fail", f"logged out: {exc}")
    except (BrowserError, AutoloopError) as exc:
        add("browser_live", "fail", str(exc))
    finally:
        if conversation is not None:
            try:
                conversation.close()
            except Exception:
                pass
    return results


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == "fail" for r in results) else 0
