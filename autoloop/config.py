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
    response_timeout_seconds: float = 900.0
    submit_timeout_seconds: float = 60.0
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
    _check_keys("paths", paths_data, {"state_dir"})
    state_dir = Path(paths_data.get("state_dir", ".autoloop"))

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
        conversation=conversation,
        executor=executor,
        audit=audit,
    )
