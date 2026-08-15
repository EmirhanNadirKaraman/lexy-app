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
import shutil
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
from .state import StateStore
from .publisher import (
    Publisher,
    provision_publisher_repo,
    publisher_hooks_path,
    read_publisher_url_snapshot,
    redact_url,
)
from .validation_env import load_validation_env, validate_validation_env_path
from .worker_env import WorkerRepoManager, validate_workers_root, verify_worker_isolation

# A conversation URL is either a plain `/c/<id>` or a project- / GPT-scoped
# `/g/<slug>/c/<id>` (chatgpt.com Projects put the conversation under the
# project). Both are valid targets; only the `/c/<id>` part identifies the
# conversation, and the loop navigates to whatever full URL is configured.
_CHATGPT_URL = re.compile(
    r"^https://chatgpt\.com(?:/g/[A-Za-z0-9_-]+)?/c/[A-Za-z0-9_-]+/?(?:[?#].*)?$"
)

# A project URL, the rotation target: the `/g/<slug>` prefix on its own or with
# chatgpt.com's `/project` landing suffix. Deliberately NOT allowed to match a
# `/c/<id>` conversation — rotating "into" an existing chat is not a rotation.
_CHATGPT_PROJECT_URL = re.compile(
    r"^https://chatgpt\.com/g/[A-Za-z0-9_-]+(?:/project)?/?(?:[?#].*)?$"
)


def _is_within(candidate: Path, root: Path) -> bool:
    """True when `candidate` resolves inside `root`.

    Resolves both sides first, so a symlink pointing into the checkout is
    caught rather than passing a string comparison.
    """
    try:
        Path(candidate).expanduser().resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def _probe_live(add, name, provider_name, config, probes, cdp_ok, playwright_ok):
    """Open one provider's conversation read-only and report what resolved.

    Never submits. Skips rather than fails when a provider's prerequisites are
    unavailable, so a missing codex binary does not mask a healthy browser (or
    the reverse) — the point of probing both is to learn about each one
    independently.
    """
    factory = probes.conversation_factory
    if factory is None:
        if provider_name == "browser_chatgpt" and not (cdp_ok and playwright_ok):
            add(name, "skip", f"{provider_name}: skipped — CDP or playwright unavailable")
            return

        def factory():
            return create_conversation(provider_name, config)

    conversation = None
    try:
        conversation = factory()
        # attach() navigates only if the page is elsewhere, then waits for the
        # composer and checks login. It never types and never submits. For a
        # CLI provider it is an honest no-op, so this reports reachability of
        # the adapter, not of the binary — see the `codex_command` check.
        conversation.attach()
        messages = getattr(conversation, "messages", None)
        if callable(messages):
            count = len(messages())
            add(
                name,
                "ok",
                f"{provider_name}: logged in; conversation open; composer + "
                f"message selectors resolve ({count} messages visible)",
            )
        else:
            add(name, "ok", f"{provider_name}: constructed (no message probe exposed)")
    except LoginExpiredError as exc:
        add(name, "fail", f"{provider_name}: logged out: {exc}")
    except (BrowserError, AutoloopError) as exc:
        add(name, "fail", f"{provider_name}: {exc}")
    finally:
        if conversation is not None:
            try:
                conversation.close()
            except Exception:
                pass


def _read_state_quietly(config: AutoloopConfig):
    """The session state, or None if there is none or it cannot be read.

    `doctor` is the command you run when things are already broken, so a
    corrupt or unreadable state file must not stop it from reporting everything
    else — the other checks are exactly what diagnoses that corruption.
    """
    try:
        return StateStore(config.state_file).load()
    except (AutoloopError, OSError):
        return None


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

    # 3b. workers_root — Autoloop M1 finding #1. MUST be absolute and
    # entirely outside the checkout/.git/state dir/publisher dirs; refuses
    # (never falls back to the old `config.workers_dir`) if not. This gates
    # check 6 below: creating even a THROWAWAY probe repo at an unsafe
    # location would be doing exactly the unsafe thing being diagnosed.
    workers_root_violations = validate_workers_root(config.workers_root, repo_root, config.state_dir)
    if workers_root_violations:
        add("workers_root", "fail", "; ".join(workers_root_violations))
    else:
        add("workers_root", "ok", str(Path(config.workers_root).resolve()))

    # 3b-bis. validation environment (the credential boundary). Reports the
    # SAME checks `cli._load_validation_env` enforces, so doctor can never
    # come back clean on a configuration a real run would refuse. Names and
    # paths only — `ValidationEnv` has no accessor that yields a value, and
    # its `describe()` says so explicitly.
    if config.validation_env_file is None:
        add(
            "validation_env",
            "skip",
            "no paths.validation_env_file configured — post-writer validation "
            "runs with no database credentials (correct unless a task's declared "
            "validation needs one)",
        )
    else:
        location_violations = validate_validation_env_path(
            config.validation_env_file, repo_root, config.state_dir, config.workers_root
        )
        if location_violations:
            add("validation_env", "fail", "; ".join(location_violations))
        else:
            try:
                loaded = load_validation_env(
                    config.validation_env_file,
                    repo_root=repo_root,
                    state_dir=config.state_dir,
                    workers_root=config.workers_root,
                )
            except AutoloopError as exc:
                add("validation_env", "fail", str(exc))
            else:
                add(
                    "validation_env",
                    "ok",
                    f"{loaded.path} defines {', '.join(loaded.keys())} "
                    "(values redacted; delivered only to the post-writer "
                    "validation subprocess, stripped from the agent subprocess)",
                )

    # 3c. legacy workers — pre-M1-fix deployments created worker repos under
    # `config.workers_dir` (`state_dir / "workers"`, nested inside the
    # checkout). This never MOVES anything (moving repository changes
    # implicitly is explicitly refused by the brief this closes) — it only
    # reports what is there so an operator can migrate or discard it by
    # hand. `warn`, never `fail`: mere presence is not itself unsafe once
    # `workers_root` (above) is the location new work actually uses.
    legacy_root = config.workers_dir
    if legacy_root.is_dir():
        stray = []
        for task_dir in sorted(p for p in legacy_root.iterdir() if p.is_dir()):
            try:
                legacy_git = GitGateway(task_dir, PolicyEngine(config.policy))
                state_desc = "dirty working tree" if legacy_git.dirty_files() else "clean working tree"
                stray.append(f"{task_dir.name} ({state_desc}, head={legacy_git.head_sha()[:12]})")
            except AutoloopError:
                stray.append(f"{task_dir.name} (not a readable git repo)")
        if stray:
            add(
                "legacy_workers",
                "warn",
                f"{len(stray)} pre-M1 worker repo(s) still at {legacy_root} — "
                "never moved automatically, inspect and migrate/discard by hand: "
                + "; ".join(stray),
            )
        else:
            add("legacy_workers", "ok", f"{legacy_root} exists but holds no task directories")
    else:
        add("legacy_workers", "ok", f"{legacy_root} does not exist — nothing to migrate")

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

    # 6. worker isolation — create a throwaway probe worker repo at the
    # (validated, external) `workers_root`, verify it with the SAME
    # `verify_worker_isolation` production code uses, remove it. Proves the
    # WorkerRepoManager pipeline actually isolates today, not just that the
    # code reads correctly. Skipped (not "fail" — already reported by check
    # 3b) if `workers_root` itself is not safe to use.
    if workers_root_violations:
        add("worker_isolation", "skip", "workers_root is not valid — see the 'workers_root' check above")
    else:
        probe_id = "doctor-probe"
        worker_repos = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
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

        # 13b. which conversation is ACTUALLY live, and how much rotation
        # budget is left. The config is only the starting point: after a
        # rotation the state is authoritative, and an operator debugging a run
        # needs to know which chat to open.
        state = _read_state_quietly(config)
        if state is None:
            add("conversation_active", "ok", "no session state yet — the config URL will be used")
        else:
            drifted = state.conversation_url != config.browser.conversation_url
            add(
                "conversation_active",
                "warn" if drifted else "ok",
                f"{state.conversation_url}"
                + (" (state has moved past the config — a rotation was recorded)" if drifted else ""),
            )
            cap = config.policy.max_conversation_rotations
            add(
                "conversation_rotations",
                "warn" if state.rotations >= cap else "ok",
                f"{state.rotations}/{cap} used this run"
                + (" — the next unusable conversation will park, not rotate"
                   if state.rotations >= cap else ""),
            )
            # 13b-2. how large the live conversation has grown, and whether the
            # loop can still retire it for size. Reported separately from the
            # rotation budget because it answers a different question: not "can
            # I escape a broken chat" but "is the chat I am in about to become
            # slow" — see docs/AUTOLOOP.md §5c.
            packet_cap = config.policy.max_conversation_packets
            retire_cap = config.policy.max_conversation_retirements
            if packet_cap <= 0:
                add(
                    "conversation_size",
                    "ok",
                    f"{state.conversation_packets} autoloop messages — retirement "
                    "for size is disabled (policy.max_conversation_packets = 0)",
                )
            else:
                over = state.conversation_packets >= packet_cap
                spent = state.retirements >= retire_cap
                add(
                    "conversation_size",
                    "warn" if over and spent else "ok",
                    f"{state.conversation_packets}/{packet_cap} autoloop messages, "
                    f"{state.retirements}/{retire_cap} retirements used this run"
                    + (
                        " — over the size threshold with no retirement budget "
                        "left, so the loop will keep working in this thread"
                        if over and spent
                        else " — the next round will retire this conversation"
                        if over
                        else ""
                    ),
                )

        # 13c. rotation target
        project_url = config.browser.project_url
        if not project_url:
            add(
                "project_url",
                "warn",
                "unset — conversation rotation is disabled; a wedged chat will "
                "park for you instead of moving to a fresh one",
            )
        elif _CHATGPT_PROJECT_URL.match(project_url):
            add("project_url", "ok", project_url)
        else:
            add(
                "project_url",
                "fail",
                f"'{project_url}' does not look like https://chatgpt.com/g/<project>[/project]",
            )

    # 13d. Codex reviewer, when either seat uses it. The live probe below
    # constructs the adapter but cannot prove the binary works without spending
    # quota, so what is checkable here is checked here: is it on PATH, and is
    # it confined.
    codex_seats = {provider, config.conversation.fallback_provider} & {"codex_cli"}
    if codex_seats:
        binary = config.codex.command[0] if config.codex.command else ""
        found = shutil.which(binary) if binary else None
        if found:
            add("codex_command", "ok", f"{binary} -> {found}")
        else:
            add(
                "codex_command",
                "fail",
                f"'{binary}' is not on PATH — install the Codex CLI and sign in "
                "with `codex login`, or change codex.command",
            )
        workdir = config.codex.working_dir or str(Path.home())
        inside_repo = _is_within(Path(workdir), repo_root)
        add(
            "codex_workdir",
            "fail" if inside_repo else "ok",
            f"{workdir}"
            + (
                " — INSIDE the repository. The reviewer's prompt is "
                "self-contained and it must not be able to read the checkout; "
                "set codex.working_dir outside it."
                if inside_repo
                else " (outside the repository)"
            ),
        )
        if not config.codex.sandbox_args:
            add(
                "codex_sandbox",
                "warn",
                "codex.sandbox_args is empty — the reviewer is confined only by "
                "running outside the repository. Add the CLI's read-only "
                "sandbox flags once you have confirmed their names.",
            )
        else:
            add("codex_sandbox", "ok", " ".join(config.codex.sandbox_args))

    # 14. live conversation checks: login + conversation + selectors. Never
    # submits.
    #
    # BOTH providers are probed, not just the configured primary. An
    # unverified fallback is not a fallback: with Codex primary, checking only
    # `conversation.provider` means the browser profile's login is first tested
    # at the moment the allowance runs out — the worst possible time to learn
    # it expired three days ago.
    fallback = config.conversation.fallback_provider
    _probe_live(add, "primary_live", provider, config, probes, cdp_ok, playwright_ok)
    if fallback and fallback != provider:
        _probe_live(add, "fallback_live", fallback, config, probes, cdp_ok, playwright_ok)
    elif fallback == provider and fallback:
        add(
            "fallback_live",
            "warn",
            f"conversation.fallback_provider is also '{provider}' — a quota "
            "failover would have nowhere to go",
        )
    else:
        add(
            "fallback_live",
            "warn",
            "no conversation.fallback_provider configured — an exhausted "
            "allowance parks the loop instead of handing over",
        )
    return results


def exit_code(results: list[CheckResult]) -> int:
    return 1 if any(r.status == "fail" for r in results) else 0
