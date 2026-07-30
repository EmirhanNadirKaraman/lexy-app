"""`python -m autoloop doctor` — non-destructive preflight.

Checks configuration, state dir, lock, git identity, branch policy, worker
isolation, controlled hooks directories, publisher configuration, publisher
URL snapshot drift, CDP reachability, Playwright, provider registration,
conversation URL shape, and (when the browser stack is actually reachable)
that the conversation opens logged-in with resolvable composer/message
selectors. It NEVER submits a message.

"Non-destructive" means never irreversible and never touching the real
conversation or the target repo's own history — it does create/remove a
throwaway probe worker repo and idempotently provision the publisher repo,
both scoped entirely under `config.state_dir` (autoloop's own scratch space),
the same category of side effect as the existing state-dir-writable probe
file.

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
from .publisher import (
    Publisher,
    provision_publisher_repo,
    publisher_hooks_path,
    read_publisher_url_snapshot,
    redact_url,
)
from .worker_env import WorkerRepoManager, verify_worker_isolation

# A conversation URL is either a plain `/c/<id>` or a project- / GPT-scoped
# `/g/<slug>/c/<id>` (chatgpt.com Projects put the conversation under the
# project). Both are valid targets; only the `/c/<id>` part identifies the
# conversation, and the loop navigates to whatever full URL is configured.
_CHATGPT_URL = re.compile(
    r"^https://chatgpt\.com(?:/g/[A-Za-z0-9_-]+)?/c/[A-Za-z0-9_-]+/?(?:[?#].*)?$"
)


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

    # 6. worker isolation — create a throwaway probe worker repo, verify it
    # with the SAME `verify_worker_isolation` production code uses, remove
    # it. Proves the WorkerRepoManager pipeline actually isolates today, not
    # just that the code reads correctly.
    probe_id = "doctor-probe"
    worker_repos = WorkerRepoManager(config.workers_dir, config.worker_hooks_dir)
    worker_repos.remove(probe_id)  # clear any stale probe from a crashed prior run
    try:
        head = GitGateway(repo_root, PolicyEngine(config.policy)).head_sha()
        worker = worker_repos.create(probe_id, repo_root, head)
        try:
            probe_git = worker.gateway(PolicyEngine(config.policy))
            violations = verify_worker_isolation(
                probe_git, expected_hooks_dir=worker_repos.hooks_dir_for(probe_id)
            )
            if violations:
                add("worker_isolation", "fail", "; ".join(violations))
            else:
                add("worker_isolation", "ok", f"probe repo at {worker.path} is isolated")
        finally:
            worker_repos.remove(probe_id)
    except AutoloopError as exc:
        add("worker_isolation", "fail", str(exc))

    # 7. controlled hooks directories — every accumulated task's worker hooks
    # dir, and the publisher's own, must be empty. Broader than check 6: that
    # one probes a single fresh id; this sweeps every hooks dir that has ever
    # been created.
    try:
        stray = []
        if config.worker_hooks_dir.is_dir():
            for task_dir in sorted(config.worker_hooks_dir.iterdir()):
                if task_dir.is_dir() and any(task_dir.iterdir()):
                    stray.append(str(task_dir))
        pub_hooks = publisher_hooks_path(config.state_dir)
        if pub_hooks.is_dir() and any(pub_hooks.iterdir()):
            stray.append(str(pub_hooks))
        if stray:
            add("hooks_dirs", "fail", f"non-empty controlled hooks dir(s): {stray}")
        else:
            add("hooks_dirs", "ok", "all controlled hooks directories are empty")
    except OSError as exc:
        add("hooks_dirs", "fail", str(exc))

    # 8. publisher configuration — idempotently (re)provision (safe: scoped
    # entirely under state_dir, never touches the target repo), then
    # construct a real `Publisher` against it, which independently re-runs
    # the single-url / no-pushurl / no-mirror / no-followTags / no-insteadOf
    # / empty-hooks-dir checks `push_exact` itself relies on.
    try:
        git_for_publisher = GitGateway(repo_root, PolicyEngine(config.policy))
        publisher_path = provision_publisher_repo(config.state_dir, git_for_publisher)
        publisher = Publisher(publisher_path, "origin", PolicyEngine(config.policy))
        described = publisher.describe()
        add(
            "publisher",
            "ok",
            f"repo={described['repo_root']} url={described['remote_url_redacted']}",
        )
    except AutoloopError as exc:
        add("publisher", "fail", str(exc))

    # 9. publisher URL snapshot vs the CONFIGURED (main checkout's live)
    # remote.origin.url. A mismatch means the main checkout's origin changed
    # since the publisher was last (re)provisioned — `Orchestrator.
    # _dispatch_task_push` refuses to publish while this holds; the ONLY fix
    # is the explicit `reprovision-publisher --confirm` command named below.
    try:
        snapshot = read_publisher_url_snapshot(config.state_dir)
        live = GitGateway(repo_root, PolicyEngine(config.policy)).config_get(
            "remote.origin.url"
        )
        if snapshot is None:
            add(
                "publisher_url_drift",
                "warn",
                "no snapshot yet — provisioned by the `publisher` check above just now",
            )
        elif snapshot == live:
            add(
                "publisher_url_drift",
                "ok",
                f"snapshot matches configured remote.origin.url ({redact_url(live)})",
            )
        else:
            add(
                "publisher_url_drift",
                "fail",
                f"snapshot ({redact_url(snapshot)}) != configured remote.origin.url "
                f"({redact_url(live)}) — verify the new destination is correct, then "
                "run `python -m autoloop reprovision-publisher --confirm`",
            )
    except AutoloopError as exc:
        add("publisher_url_drift", "fail", str(exc))

    # 10. CDP endpoint
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

    # 11. playwright
    playwright_ok = bool(probes.playwright_present())
    add(
        "playwright",
        "ok" if playwright_ok else "fail",
        "importable" if playwright_ok else "not installed — pip install -r autoloop/requirements.txt",
    )

    # 12. provider registration
    provider = config.conversation.provider
    if provider in available_providers():
        add("provider", "ok", provider)
    else:
        add("provider", "fail", f"'{provider}' not registered ({available_providers()})")

    # 13. conversation URL shape (browser_chatgpt only)
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
                "https://chatgpt.com/c/<id> or "
                "https://chatgpt.com/g/<project>/c/<id>",
            )

    # 14. live browser: login + conversation + selectors. Never submits.
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
        # attach() navigates only if the page is elsewhere, then waits for the
        # composer and checks login. It never types and never submits.
        conversation.attach()
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
