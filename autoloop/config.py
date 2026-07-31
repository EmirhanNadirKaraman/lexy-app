"""TOML configuration loading for autoloop.

Strict by design: unknown sections or keys raise ConfigError instead of being
ignored, so a typo'd safety limit can never silently fall back to a default.
See `config.example.toml` for the annotated template.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import tomllib

from .errors import ConfigError
from .policy import PolicyConfig


@dataclass(frozen=True)
class BrowserConfig:
    conversation_url: str
    cdp_url: str = "http://127.0.0.1:9222"
    #: The ChatGPT *project* the conversation belongs to, e.g.
    #: "https://chatgpt.com/g/g-p-<id>-<slug>/project". Required for conversation
    #: rotation and for nothing else — a rotation opens a new chat here.
    #: Configured EXPLICITLY rather than sliced out of `conversation_url`:
    #: deriving it would mean guessing which URL shapes are project-scoped, and
    #: a wrong guess opens a chat somewhere the operator never chose. Unset
    #: means rotation is unavailable and the loop parks instead.
    project_url: str = ""
    #: Every wait below is bounded separately, so a stuck channel fails fast
    #: with a specific diagnosis instead of hanging on one giant timeout.
    composer_timeout_seconds: float = 30.0
    input_sync_timeout_seconds: float = 30.0
    send_ready_timeout_seconds: float = 30.0
    #: How long to wait for evidence the server accepted a submitted turn.
    submit_timeout_seconds: float = 60.0
    #: How long to wait for the assistant to START answering.
    response_start_timeout_seconds: float = 120.0
    #: How long to wait for a started answer to settle.
    response_timeout_seconds: float = 900.0
    reconcile_timeout_seconds: float = 30.0
    poll_interval_seconds: float = 2.0
    stability_seconds: float = 3.0


@dataclass(frozen=True)
class ConversationConfig:
    provider: str = "browser_chatgpt"


@dataclass(frozen=True)
class ExecutorConfig:
    kind: str = "audit"  # audit | null (null = record-only dry run)


@dataclass(frozen=True)
class AuditConfig:
    agent_command: tuple[str, ...] = ("claude",)
    agent_timeout_seconds: float = 900.0
    max_parallel_agents: int = 3
    validation_commands: tuple[tuple[str, ...], ...] = (("ruff", "check", "."),)


@dataclass(frozen=True)
class AutoloopConfig:
    browser: BrowserConfig
    policy: PolicyConfig
    state_dir: Path
    #: EXTERNAL worker-repo location (Autoloop M1 finding #1) — `None` is a
    #: valid dataclass value (every direct `AutoloopConfig(...)` construction
    #: across the test suite predates this field and does not set it) but is
    #: NOT a usable one: `load_config` below requires an absolute value in
    #: `[paths].workers_root` and raises `ConfigError` otherwise, and the two
    #: places that actually construct a `WorkerRepoManager` for real dispatch
    #: (`cli._build_orchestrator`, `doctor.run_doctor`) both call
    #: `worker_env.validate_workers_root` and refuse before doing so — see
    #: those call sites. There is deliberately NO fallback to the old
    #: `state_dir / "workers"` default (`workers_dir` below, kept only for
    #: locating pre-existing/legacy worker repos to report on, never as a
    #: place new ones are created).
    workers_root: Path | None = None
    conversation: ConversationConfig = ConversationConfig()
    executor: ExecutorConfig = ExecutorConfig()
    audit: AuditConfig = AuditConfig()

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def tasks_file(self) -> Path:
        return self.state_dir / "tasks.json"

    @property
    def manifests_dir(self) -> Path:
        return self.state_dir / "manifests"

    @property
    def audit_dir(self) -> Path:
        return self.state_dir / "audit"

    @property
    def smoke_dir(self) -> Path:
        return self.state_dir / "smoke"

    @property
    def transcript_file(self) -> Path:
        return self.state_dir / "transcript.jsonl"

    @property
    def diagnostics_dir(self) -> Path:
        return self.state_dir / "diagnostics"

    @property
    def pause_file(self) -> Path:
        return self.state_dir / "PAUSE"

    # ---- produce-then-review collaborators (Autoloop v1 CLI wiring) --------
    #
    # All paths below live under `state_dir` (already gitignored). Publisher
    # paths (bare repo, hooks dir, url snapshot) are NOT duplicated here —
    # `publisher.py` is their single source of truth (`publisher_repo_path` /
    # `publisher_hooks_path` / `publisher_url_snapshot_path`); import from
    # there rather than re-deriving the same relative names in two places.

    @property
    def workers_dir(self) -> Path:
        """The OLD, pre-M1-fix worker location (`state_dir / "workers"`,
        nested inside the checkout by construction). No production code
        creates NEW worker repos here anymore — real dispatch uses
        `workers_root` instead (validated externally; see that field's
        docstring). Kept only so `doctor.py` / the CLI can find and report on
        worker repos a pre-fix deployment left behind here (finding #1's
        "migrate or safely abandon existing disposable workers" — reported,
        never moved)."""
        return self.state_dir / "workers"

    @property
    def worker_hooks_dir(self) -> Path:
        return self.state_dir / "worker-hooks"

    @property
    def executions_dir(self) -> Path:
        return self.state_dir / "executions"

    @property
    def intents_dir(self) -> Path:
        return self.state_dir / "intents"

    @property
    def blockers_dir(self) -> Path:
        """Durable operator-facing blocker records (`blockers.BlockerStore`)
        — survives a `task_fatal` park clearing `state.json`, and survives
        `reset` too (like `continuous_fingerprint_file`, `reset` only
        archives `state.json`/`tasks.json`)."""
        return self.state_dir / "blockers"

    @property
    def seed_tasks_file(self) -> Path:
        """Git-tracked seed file (`autoloop/seed_tasks.json`, alongside this
        module) — NOT under `state_dir`. `cli.py`'s `next-task` command loads
        it directly into a fresh `TaskRegistry` when `.autoloop/tasks.json`
        does not exist yet; it is never written to."""
        return Path(__file__).resolve().parent / "seed_tasks.json"

    @property
    def continuous_fingerprint_file(self) -> Path:
        """`run --continuous`'s "have I already looked at this exact
        repository state" marker (HEAD sha + a content digest of the dirty
        tree — see `cli.repo_fingerprint`). Deliberately OUTSIDE the
        session/task lifecycle `reset` touches: it survives a `reset` on
        purpose, so a reset does not force a redundant audit of an otherwise
        unchanged repository."""
        return self.state_dir / "continuous_fingerprint.json"


_SECTIONS = {"browser", "policy", "paths", "conversation", "executor", "audit"}


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in [{section}]: {sorted(unknown)}")


def load_config(path: Path) -> AutoloopConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}. Copy autoloop/config.example.toml "
            "there and fill in browser.conversation_url."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc

    unknown_sections = set(data) - _SECTIONS
    if unknown_sections:
        raise ConfigError(f"unknown config sections: {sorted(unknown_sections)}")

    browser_data = data.get("browser", {})
    browser_fields = {f.name for f in dataclasses.fields(BrowserConfig)}
    _check_keys("browser", browser_data, browser_fields)
    if not browser_data.get("conversation_url"):
        raise ConfigError(
            "browser.conversation_url is required — the URL of the one persistent "
            "ChatGPT conversation the loop uses (https://chatgpt.com/c/...)"
        )
    browser = BrowserConfig(**browser_data)

    policy_data = dict(data.get("policy", {}))
    policy_fields = {f.name for f in dataclasses.fields(PolicyConfig)}
    _check_keys("policy", policy_data, policy_fields)
    if "protected_branches" in policy_data:
        branches = policy_data["protected_branches"]
        if not isinstance(branches, list) or not all(isinstance(b, str) for b in branches):
            raise ConfigError("policy.protected_branches must be a list of strings")
        policy_data["protected_branches"] = tuple(branches)
    policy = PolicyConfig(**policy_data)

    paths_data = data.get("paths", {})
    _check_keys("paths", paths_data, {"state_dir", "workers_root"})
    state_dir = Path(paths_data.get("state_dir", ".autoloop"))

    # `workers_root` (Autoloop M1 finding #1): required, absolute, no
    # default — NEVER silently falls back to `state_dir / "workers"`. This
    # catches "missing" and "relative" at load time, cheaply, without needing
    # a repo root; "nested beneath the checkout / its .git / the state dir /
    # the publisher dir" needs that context and is checked separately by
    # `worker_env.validate_workers_root` at the two places a `WorkerRepoManager`
    # actually gets constructed for real dispatch (`cli.py`, `doctor.py`).
    workers_root_raw = paths_data.get("workers_root")
    if not workers_root_raw or not str(workers_root_raw).strip():
        raise ConfigError(
            "paths.workers_root is required — an ABSOLUTE path outside this "
            "checkout where task worker repositories live (e.g. "
            "\"~/.autoloop/workers\"). There is no default; see "
            "config.example.toml. (Autoloop M1: a worker repo nested inside "
            "the checkout is invisible to every git-based verification "
            "primitive scoped to the checkout.)"
        )
    workers_root = Path(str(workers_root_raw)).expanduser()
    if not workers_root.is_absolute():
        raise ConfigError(
            f"paths.workers_root must be an absolute path, got {workers_root_raw!r} "
            "(after expanding '~') — a relative path is ambiguous across the "
            "different working directories worker-repo subprocesses run from"
        )

    conversation_data = data.get("conversation", {})
    conversation_fields = {f.name for f in dataclasses.fields(ConversationConfig)}
    _check_keys("conversation", conversation_data, conversation_fields)
    conversation = ConversationConfig(**conversation_data)

    executor_data = data.get("executor", {})
    _check_keys("executor", executor_data, {f.name for f in dataclasses.fields(ExecutorConfig)})
    executor = ExecutorConfig(**executor_data)
    if executor.kind not in ("audit", "null"):
        raise ConfigError(f"executor.kind must be 'audit' or 'null', got '{executor.kind}'")

    audit_data = dict(data.get("audit", {}))
    _check_keys("audit", audit_data, {f.name for f in dataclasses.fields(AuditConfig)})
    if "agent_command" in audit_data:
        cmd = audit_data["agent_command"]
        if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
            raise ConfigError("audit.agent_command must be a non-empty list of strings")
        audit_data["agent_command"] = tuple(cmd)
    if "validation_commands" in audit_data:
        commands = audit_data["validation_commands"]
        if not isinstance(commands, list) or not all(
            isinstance(c, list) and c and all(isinstance(t, str) for t in c) for c in commands
        ):
            raise ConfigError(
                "audit.validation_commands must be a list of non-empty string lists, "
                'e.g. [["ruff", "check", "."]]'
            )
        audit_data["validation_commands"] = tuple(tuple(c) for c in commands)
    audit = AuditConfig(**audit_data)

    return AutoloopConfig(
        browser=browser,
        policy=policy,
        state_dir=state_dir,
        workers_root=workers_root,
        conversation=conversation,
        executor=executor,
        audit=audit,
    )
