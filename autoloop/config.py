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
from .stall import DEFAULT_CEILING_SECONDS, DEFAULT_STALL_SECONDS


@dataclass(frozen=True)
class BrowserConfig:
    conversation_url: str
    cdp_url: str = "http://127.0.0.1:9222"
    #: Deliver an oversized review diff as an UPLOADED FILE instead of chunking
    #: it into messages. OFF by default: it changes what the reviewer receives,
    #: and the chunking path it replaces is the one every existing test pins.
    #:
    #: Why it exists: the composer cannot be proven to hold a large patch —
    #: `_enter_prompt` reads the editor back and a 30,000-character part never
    #: returns its own tail — so chunking fails permanently on exactly the
    #: changes most worth reviewing. Measured 2026-08-15: a 336 KB .md attached
    #: to a chat was read in full, quoting canaries from its last line.
    attach_oversized_diff: bool = False
    #: The ChatGPT *project* the conversation belongs to, e.g.
    #: "https://chatgpt.com/g/g-p-<id>-<slug>/project". Required for the two
    #: things that read the project's chat list: conversation rotation (which
    #: opens a new chat here) and the by-content search that resolves a false
    #: `submission_ambiguous` park (`Orchestrator._resolve_or_park_ambiguous`).
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
    #: Command run to restart the browser after a session loss. Empty
    #: disables auto-restart, which is the pre-existing behaviour.
    #:
    #: Declared by the operator rather than inferred: the loop knows only
    #: a `cdp_url`, not which Chrome process owns it, and a loop that
    #: pattern-matched process lists could kill the wrong browser. An
    #: explicit command also makes the blast radius reviewable — see
    #: `autoloop/browser/chrome_restart.py` (the shipped implementation,
    #: `["python3", "-m", "autoloop.browser.chrome_restart"]`), which matches
    #: one profile by its --user-data-dir EXACTLY and nothing else.
    restart_command: tuple[str, ...] = ()
    #: Minimum seconds between restart attempts. Without it a genuinely
    #: dead transport becomes a restart loop.
    restart_cooldown_seconds: float = 120.0
    #: First wait after ChatGPT reports its account-level throttle
    #: (`errors.RateLimitedError`), doubling on each consecutive occurrence up
    #: to `rate_limit_backoff_max_seconds`. Waiting is the entire remedy: the
    #: limit is server-side, so the loop cannot recover from it, only outlast
    #: it — and every retry it makes meanwhile is another request into the
    #: window that produced it.
    rate_limit_backoff_seconds: float = 60.0
    #: Ceiling on one such wait. Kept under the heartbeat monitor's staleness
    #: threshold (45 minutes, `scripts/check_heartbeat.py`) on purpose: the
    #: loop publishes a heartbeat BETWEEN steps, so a single sleep inside one
    #: is a gap in the record. Ten minutes leaves that alarm meaning what it
    #: says while still being a real wait.
    rate_limit_backoff_max_seconds: float = 600.0


@dataclass(frozen=True)
class ConversationConfig:
    provider: str = "browser_chatgpt"
    #: Where to go when `provider` reports its allowance exhausted. Empty
    #: disables failover — the loop parks instead. The pairing that motivates
    #: this is codex_cli -> browser_chatgpt: Codex draws on the ChatGPT plan's
    #: AGENTIC allowance (shared with ChatGPT Work and ChatGPT for Excel),
    #: while ordinary ChatGPT conversations draw on a separate quota, so the
    #: browser really does still work once Codex is spent. Two transports AND
    #: two budgets — which is the only reason automatic failover buys anything.
    fallback_provider: str = ""


@dataclass(frozen=True)
class CodexConfig:
    """The Codex CLI reviewer. Only consulted when a codex provider is used."""

    #: Base invocation. Split from `sandbox_args` so the two can be reasoned
    #: about separately — this is "how do I run it", that is "what may it do".
    command: tuple[str, ...] = ("codex", "exec")
    #: Flags that confine the reviewer. Deliberately NOT given a permissive
    #: default: the flag names cannot be verified from this repository, and
    #: guessing them would produce a setting that looks like a control and is
    #: not one. `doctor` warns while this is empty. The reviewer is confined
    #: regardless by running outside the checkout (see `working_dir`).
    sandbox_args: tuple[str, ...] = ()
    timeout_seconds: float = 900.0
    #: Where the CLI runs. Empty means the user's home directory — anywhere but
    #: the repository. The prompt is self-contained, so the reviewer needs no
    #: filesystem access, and this containment holds without depending on a
    #: sandbox flag's name.
    working_dir: str = ""
    #: Substrings that identify an exhausted allowance in a FAILED invocation.
    #: Empty uses `codex.quota.DEFAULT_QUOTA_PATTERNS`. Overridable because the
    #: real wording cannot be confirmed here and will change; every non-zero
    #: exit logs its stderr tail so the first real exhaustion shows what to add.
    quota_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutorConfig:
    kind: str = "audit"  # audit | null (null = record-only dry run)


#: This repository's own audit-report location, and the default for
#: `RepoConfig.audit_report_glob`. Relative to the repo root, newest-by-NAME
#: (the reports are date-stamped, `docs/AUDIT_<date>.md`), which is why the
#: dashboard sorts them as strings rather than by mtime.
DEFAULT_AUDIT_REPORT_GLOB = "docs/AUDIT_*.md"
#: This repository's own declaration of its application database: the
#: git-tracked example env file, and the key inside it. Defaults for
#: `RepoConfig.env_example_file` / `env_example_db_key`; see
#: `validation_env.repo_declared_db_name` for what that marker is and, more
#: importantly, what it is not.
DEFAULT_ENV_EXAMPLE_FILE = ".env.example"
DEFAULT_ENV_EXAMPLE_DB_KEY = "DB_NAME"


@dataclass(frozen=True)
class RepoConfig:
    """What the loop cannot infer about the TARGET REPOSITORY, minus anything
    that grants authority.

    Each setting here was a hardcoded constant naming this repository's own
    conventions, which is the reason the loop could not be pointed at anything
    else. Each default is the exact value that was hardcoded, so a config that
    omits this section behaves identically to the code before the section
    existed — that equivalence is what `test_config_repo_section.py` pins, and
    it is the property to preserve if these ever change.

    **Deliberately NOT a general escape hatch, and specifically not an
    authorization surface.** Every setting below says WHERE to read something
    the repository itself states; none of them decides what an agent may write.
    The always-approved tracker list does decide that, and it is therefore NOT
    here: it stays the fixed `tasks.TRACKER_PATHS` constant, because this file
    lives under the gitignored state directory and an edit to it is not a
    reviewed diff. That distinction is the whole shape of this section — see
    `tasks.TRACKER_PATHS` and `docs/SECURITY.md` S31 for the design that was
    tried and withdrawn, and `_migrate_retired_tracker_paths` below for what
    happens to a config that still names the withdrawn key.
    """

    #: The git-tracked example env file that declares the APPLICATION database
    #: name, and the key inside it. Used for exactly one refusal: a validation
    #: env file pointed at that same database (`validation_env.
    #: repo_declared_db_name`). Exactly `""` disables the refusal — honest for
    #: a repository that declares no such name anywhere, and no worse than the
    #: pre-existing behaviour when the file is simply absent. Only that exact
    #: value: a blank-looking `"   "` is refused at load, so the disable is
    #: always something the operator wrote on purpose.
    env_example_file: str = DEFAULT_ENV_EXAMPLE_FILE
    env_example_db_key: str = DEFAULT_ENV_EXAMPLE_DB_KEY
    #: Where the dashboard reads the application backlog from — the newest
    #: audit report, by name. A glob relative to the repo root; metacharacters
    #: are the point here, unlike in `env_example_file`, so it is checked only
    #: for being relative and traversal-free. Exactly `""` means the dashboard's
    #: "Language-app tasks" panel stays empty, which is the correct reading for
    #: a repository that files no audit reports — and, as above, only that exact
    #: value; padding is refused rather than read as the opt-out.
    audit_report_glob: str = DEFAULT_AUDIT_REPORT_GLOB


#: The key retired on 2026-08-14. An existing config may still name it, and
#: naming it still loads — but it is handled EXPLICITLY, never ignored: it is
#: migrated onto `audit_agent_timeout_seconds` (see `_migrate_retired_timeout`)
#: and the operator is told, once per process, what it now does and does not
#: govern. It is never stored under its old name.
RETIRED_AGENT_TIMEOUT_KEY = "agent_timeout_seconds"
#: The key it migrates onto — the ONE replacement that kept its exact meaning.
MIGRATED_TIMEOUT_KEY = "audit_agent_timeout_seconds"

#: The shell helper retired on 2026-08-16 (brw-08), kept here as ONE spelling
#: shared by the tombstone that replaced it and the tests that pin this path.
#:
#: `load_config` deliberately does NOT act on it. A `restart_command` still
#: naming the script keeps loading, exactly as written, for the length of the
#: transition — the live `.autoloop/config.toml` is not in this repository, so
#: refusing here would make every command (`status`, `doctor`, `run`, the
#: recovery commands) fail on an unmigrated deployment the moment this branch
#: merged, taking away the tooling the operator would use to recover. The
#: compatibility boundary is the tombstone at `scripts/restart_autoloop_chrome.sh`
#: instead: ordinary commands keep working, and only an actual browser restart
#: fails — non-zero, with the replacement line on stderr. Re-adding a refusal
#: here belongs to a later cleanup, once live configs have been migrated.
RETIRED_RESTART_SCRIPT = "restart_autoloop_chrome.sh"
#: The module that replaced it, spelled exactly as it goes in the config.
RESTART_COMMAND_REPLACEMENT = ("python3", "-m", "autoloop.browser.chrome_restart")


def _restart_command_toml(command: tuple[str, ...] = RESTART_COMMAND_REPLACEMENT) -> str:
    """`restart_command = [...]`, ready to paste. A config error is plausibly
    the only thing the operator sees — `cli.main` prints `error: <exc>` and
    nothing else — so it carries the literal line rather than a pointer to a
    file. Used by the shape check in `load_config`."""
    return "restart_command = [" + ", ".join(f'"{token}"' for token in command) + "]"


@dataclass(frozen=True)
class AuditConfig:
    agent_command: tuple[str, ...] = ("claude",)
    #: **Read-only AUDIT subagents only** — an elapsed-time bound, exactly
    #: what the retired `agent_timeout_seconds` meant, at its old value. Kept
    #: as a timeout for this path on purpose: a read-only agent produces no
    #: filesystem change, so there is no progress signal to watch, and a
    #: timeout there costs a re-run rather than destroying work.
    audit_agent_timeout_seconds: float = 900.0
    #: **Write-capable IMPLEMENT subagents** — how long the worker repository
    #: may show NO change at all before the agent is treated as hung and
    #: killed. Not a task budget: while files keep changing, the agent keeps
    #: running however long the work takes. Defaulted generously (30 min) —
    #: a long compile or test run is not a stall, and killing a healthy agent
    #: is the failure this setting exists to correct. See `stall.py`.
    agent_stall_seconds: float = DEFAULT_STALL_SECONDS
    #: The absolute backstop for BOTH paths, set far above any real task
    #: (4 hours). It should effectively never fire; when it does, that is a
    #: finding, and `StallReport.describe()` says so loudly.
    agent_ceiling_seconds: float = DEFAULT_CEILING_SECONDS
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
    #: Absolute path to the operator-authored file holding the DEDICATED TEST
    #: database credentials the post-writer validation subprocess runs under
    #: (`validation_env.py`). Optional: `None` means validation runs with no
    #: database credentials at all, which is correct for tasks whose declared
    #: validation does not need one. Like `workers_root`, only the cheap
    #: checks (absolute, expandable) happen at load time — location,
    #: permissions, ownership and content are checked by
    #: `validation_env.validate_validation_env_path` / `load_validation_env`
    #: at the two places that need a repo root (`cli.py`, `doctor.py`).
    validation_env_file: Path | None = None
    conversation: ConversationConfig = ConversationConfig()
    codex: CodexConfig = CodexConfig()
    executor: ExecutorConfig = ExecutorConfig()
    audit: AuditConfig = AuditConfig()
    #: What the TARGET repository declares about itself. Defaulted, and last
    #: but one for the same reason `migration_notices` is last: every direct
    #: `AutoloopConfig(...)` construction across the test suite predates it,
    #: and each default is the constant that used to be hardcoded — so an
    #: unaware caller gets exactly the previous behaviour.
    repo: RepoConfig = RepoConfig()
    #: Operator-facing messages about retired keys this config still names —
    #: DATA, not a side effect. `load_config` stays pure: it never writes to a
    #: stream and holds no "already warned" global, so the notice text can be
    #: asserted by any test in any order without contaminating another. The
    #: once-per-process suppression lives in exactly one place, `cli.py`'s
    #: `emit_migration_notices`. Last field and defaulted, because the direct
    #: `AutoloopConfig(...)` constructions across the test suite predate it.
    migration_notices: tuple[str, ...] = ()

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
        """The pause flag, deliberately OUTSIDE the checkout.

        It used to live at `state_dir / "PAUSE"`, and `state_dir` is inside
        the tree `escape_detector` snapshots around every write-capable agent
        call — it enumerates ignored paths on purpose, since `.autoloop/` is
        gitignored in production and an agent forging state there is exactly
        what the detector exists to catch. So running `pause` while a task
        was dispatched created a file the detector then reported as an escape,
        and parked the loop `loop_fatal`. The documented way to stop the loop
        broke it.

        Placed beside `workers_root` for the same reason `inbox.inbox_dir_for`
        is: that path is already required to be absolute and outside the
        checkout, its `.git`, the state dir and the publisher paths
        (`worker_env.validate_workers_root`). Operator-writable things belong
        outside the snapshotted tree — the inbox learned this first.

        Exempting the path inside the detector was the alternative and is
        worse: it would carve a permanent hole in a security-shaped check so
        that one operator convenience can write into the watched tree.
        """
        if self.workers_root is not None:
            return Path(self.workers_root).expanduser().parent / "PAUSE"
        return self.state_dir / "PAUSE"

    @property
    def legacy_pause_file(self) -> Path:
        """The pre-2026-08-02 location, still honoured on read.

        A flag left here by an older build must not be silently ignored:
        the operator asked for a pause and would otherwise get a loop that
        keeps running. Read from both, write only to `pause_file`, clear both.
        """
        return self.state_dir / "PAUSE"

    @property
    def heartbeat_file(self) -> Path:
        """Where the loop publishes its liveness, OUTSIDE the checkout.

        Two independent reasons, either sufficient:

        * `escape_detector` snapshots the checkout around every write-capable
          agent call, ignored paths included. A heartbeat written inside it
          mid-round would be reported as an escape — the bug the pause flag
          had.
        * macOS TCC blocks a launchd agent from reading `~/Documents` at all
          (`getcwd: Operation not permitted`, exit 126 — hit on 2026-08-02
          scheduling the health check). A monitor that never touches a
          protected path needs no Full Disk Access grant, so the durable job
          works with no security change.

        Beside `workers_root`, like the inbox and the pause flag.
        """
        if self.workers_root is not None:
            return Path(self.workers_root).expanduser().parent / "heartbeat.json"
        return self.state_dir / "heartbeat.json"

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
    def merge_deferrals_dir(self) -> Path:
        """Auto-merge's own retry queue (`auto_merge.MergeDeferralStore`) —
        one record per task whose completed, published candidate could not be
        integrated yet because the merge window was shut, the remote base had
        moved, or the checkout was dirty.

        Durable for the same reason `blockers_dir` is: a deferral that lived
        only in `state.json` would be discarded by the next `task_fatal` park
        or `reset`, and the work would go back to being invisible — which is
        the whole failure auto-merge exists to close."""
        return self.state_dir / "merge-deferrals"

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


_SECTIONS = {"browser", "policy", "paths", "conversation", "codex", "executor", "audit", "repo"}


def _check_keys(section: str, data: dict, allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"unknown keys in [{section}]: {sorted(unknown)}")


def _migrate_retired_timeout(audit_data: dict) -> tuple[str, ...]:
    """Handle a config that still names `audit.agent_timeout_seconds`.

    Explicitly, which is the whole requirement — an existing config must not
    quietly acquire different behaviour under a name it already uses. Three
    things make that true, and all three are load-bearing:

    * **The key is consumed here**, so it never reaches `AuditConfig` and
      `config.audit.agent_timeout_seconds` does not exist. Nothing can read
      the old name and get a value.
    * **Its MEANING is preserved, not its scope.** The old key meant "elapsed
      bound for a subagent". That meaning survives intact on exactly one path
      — read-only audit agents, which change no files and so offer no progress
      to watch — so the value migrates onto `audit_agent_timeout_seconds` and
      governs there exactly as before. It no longer bounds the write-capable
      implement agent, because bounding THAT by elapsed time is the measured
      failure this whole change exists to correct (`stall.py`).
    * **The operator is told**, via a notice the CLI prints. Migrating in
      silence would be the "silently acquires different behaviour" failure
      wearing a different hat.

    An explicit `audit_agent_timeout_seconds` WINS: it is the current name for
    the setting, so someone who wrote both has already chosen, and the notice
    says the legacy value was dropped rather than letting the retired key
    quietly override the key that replaced it.

    Refusing the config outright was the alternative. It is rejected because a
    config naming this key is a config written when the loop worked, and the
    only action it enables — delete one line — is one the loader can take
    itself without making a running deployment unstartable.
    """
    if RETIRED_AGENT_TIMEOUT_KEY not in audit_data:
        return ()
    legacy = audit_data.pop(RETIRED_AGENT_TIMEOUT_KEY)
    # Validated with the same rule the key it migrates onto gets, and BEFORE
    # anything is said about it: a migration notice quoting a value that is
    # about to be refused would be noise in front of the real error.
    if not isinstance(legacy, (int, float)) or isinstance(legacy, bool) or legacy <= 0:
        raise ConfigError(
            f"audit.{RETIRED_AGENT_TIMEOUT_KEY} must be a positive number, got {legacy!r}"
        )
    head = (
        f"autoloop: NOTICE — audit.{RETIRED_AGENT_TIMEOUT_KEY} was RETIRED on "
        "2026-08-14. It bounded every subagent by ELAPSED TIME, which cannot "
        "tell a large task from a wedged one: measured over 2026-08-05/06 it "
        "never once caught a hung agent and killed six agents mid-write, "
        "discarding up to 631 insertions at a time. Your config still loads; "
        "the key has been handled as follows."
    )
    if MIGRATED_TIMEOUT_KEY in audit_data:
        outcome = (
            f"  IGNORED: audit.{MIGRATED_TIMEOUT_KEY} = "
            f"{audit_data[MIGRATED_TIMEOUT_KEY]} is also set and WINS — it is "
            "the current name for this setting. The retired key's value "
            f"({legacy}) was dropped."
        )
    else:
        audit_data[MIGRATED_TIMEOUT_KEY] = legacy
        outcome = (
            f"  MIGRATED: its value ({legacy}) now sets "
            f"audit.{MIGRATED_TIMEOUT_KEY}, the elapsed bound for READ-ONLY "
            "audit subagents. That is the one replacement which keeps the old "
            "meaning exactly — those agents change no files, so there is no "
            "progress to observe, and a timeout there costs a re-run rather "
            "than destroying work."
        )
    return (
        "\n".join(
            [
                head,
                outcome,
                "  NOT carried over: write-capable implement subagents are no "
                "longer bounded by elapsed time at all. They are bounded by "
                "SILENCE — audit.agent_stall_seconds (default "
                f"{DEFAULT_STALL_SECONDS:.0f}) is how long the worker "
                "repository may show NO change before the agent counts as "
                "hung. While files keep changing, the agent keeps running, "
                "however long the task takes.",
                "  Backstop: audit.agent_ceiling_seconds (default "
                f"{DEFAULT_CEILING_SECONDS:.0f}) still terminates a "
                "pathological run. It is set far above any real task and "
                "should effectively never fire.",
                f"  Silence this by deleting audit.{RETIRED_AGENT_TIMEOUT_KEY} "
                "from your config and setting the keys above deliberately.",
            ]
        ),
    )


#: The `[repo]` key that existed for one unshipped round on 2026-08-16 and was
#: withdrawn in review (port-02, `docs/SECURITY.md` S31). A config may still
#: name it — `config.example.toml` advertised it — and naming it still loads,
#: but it is handled EXPLICITLY and never applied. Same treatment, and the same
#: reasoning, as `RETIRED_AGENT_TIMEOUT_KEY`.
RETIRED_TRACKER_PATHS_KEY = "tracker_paths"


def _migrate_retired_tracker_paths(repo_data: dict) -> tuple[str, ...]:
    """Handle a config that still names `repo.tracker_paths`.

    The key is CONSUMED (so it cannot reach `RepoConfig`) and its value is
    DISCARDED (so it cannot authorize anything), and the operator is told both
    facts. Three properties, all deliberate:

    * **Never applied.** The always-approved tracker list is
      `tasks.TRACKER_PATHS`, a constant in reviewed source, because this file
      is gitignored and an edit to it is not a diff anyone reads. Honouring the
      key is exactly the control that was withdrawn.
    * **Not silently ignored.** A setting that loads and does nothing is worse
      than a constant — it reads as configured while behaving otherwise — so
      the notice says the value was dropped and names where the real list
      lives. An operator who wanted those paths granted learns it here rather
      than from a task parking on an unauthorized-path refusal.
    * **Not a hard refusal.** Same call as `RETIRED_RESTART_SCRIPT`: the live
      `.autoloop/config.toml` is not in this repository, so refusing at load
      would make every command — `status`, `doctor`, the recovery commands —
      fail on an unmigrated deployment the moment this landed, taking away the
      tooling needed to fix it. The direction is safe either way: discarding
      grants FEWER paths than the operator may believe, so the failure mode is
      a refused task, never an over-authorized one.
    """
    if RETIRED_TRACKER_PATHS_KEY not in repo_data:
        return ()
    dropped = repo_data.pop(RETIRED_TRACKER_PATHS_KEY)
    return (
        "\n".join(
            [
                f"autoloop: NOTICE — repo.{RETIRED_TRACKER_PATHS_KEY} is NOT a "
                "setting and its value was DROPPED, not applied. The "
                "always-approved documentation trackers are the fixed "
                "`TRACKER_PATHS` constant in autoloop/tasks.py.",
                f"  Dropped: {dropped!r}.",
                "  Why: a tracker is granted to EVERY scoped task without being "
                "named in it, and this config file lives under the gitignored "
                "state directory — so an edit here would widen every task's "
                "write authorization without a diff anyone reviews. It was "
                "briefly configurable on 2026-08-16 and withdrawn in review; "
                "see docs/SECURITY.md S31.",
                "  To change the list for this repository, edit TRACKER_PATHS in "
                "autoloop/tasks.py — the package is vendored into the repository "
                "it operates on, so that edit is a reviewed commit in this "
                "repository's own history.",
                f"  Silence this by deleting repo.{RETIRED_TRACKER_PATHS_KEY} "
                "from your config.",
            ]
        ),
    )


def _repo_relative(section_key: str, value: str, *, allow_globs: bool) -> str:
    """Raise `ConfigError` unless `value` is a plain repository-relative path.

    Both `[repo]` path settings are joined onto a repo root the loop is handed,
    so an absolute path or a `..` would read a file the operator did not point
    the loop at — and the audit glob is passed to `Path.glob`, which raises
    outright on an absolute pattern. `allow_globs` is the one difference:
    `audit_report_glob` is a pattern by definition, while `env_example_file`
    names one file and must not quietly match several.
    """
    if value.strip() != value or "\\" in value:
        raise ConfigError(
            f"repo.{section_key} must be a repository-relative path with no padding "
            f"and '/' separators, got {value!r}"
        )
    if value.startswith(("/", "~")):
        raise ConfigError(
            f"repo.{section_key} must be relative to the repository root, got {value!r} "
            "— it is resolved against a checkout the loop is pointed at, so an "
            "absolute path would read something else entirely"
        )
    segments = value.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise ConfigError(
            f"repo.{section_key} must not contain '..', '.', or an empty segment, "
            f"got {value!r}"
        )
    if not allow_globs and any(ch in value for ch in "*?[]"):
        raise ConfigError(
            f"repo.{section_key} names one exact file and may not contain glob "
            f"metacharacters, got {value!r}"
        )
    return value


def _load_repo_section(data: dict) -> tuple[RepoConfig, tuple[str, ...]]:
    """`[repo]`, validated, plus any operator notices it produced. Absent means
    `RepoConfig()` — i.e. this repository's own constants, which is the
    pre-configuration behaviour."""
    raw = data.get("repo", {})
    # Shape first, so `repo = "docs"` gets this loader's own error rather than
    # `dict()`'s raw conversion complaint. Strict config means a malformed
    # section is REPORTED as one; a `ValueError` about dictionary update
    # sequences names neither the section nor what it should have been.
    if not isinstance(raw, dict):
        raise ConfigError(
            f"[repo] must be a table, got {raw!r} — write it as a section header "
            '(`[repo]` followed by `key = "value"` lines), not as a bare key'
        )
    repo_data = dict(raw)
    # BEFORE `_check_keys`, exactly like `_migrate_retired_timeout`: the key is
    # consumed here, so it can never reach `RepoConfig`, and the operator gets
    # the reason instead of a generic "unknown key".
    notices = _migrate_retired_tracker_paths(repo_data)
    _check_keys("repo", repo_data, {f.name for f in dataclasses.fields(RepoConfig)})

    for key in ("env_example_file", "audit_report_glob"):
        if key not in repo_data:
            continue
        value = repo_data[key]
        if not isinstance(value, str):
            raise ConfigError(f"repo.{key} must be a string, got {value!r}")
        # EXACTLY empty is the documented opt-out; everything else is validated,
        # including `"   "`. The test is `!= ""` rather than `.strip()` because
        # the two are not the same refusal: blank-but-not-empty reaches
        # `repo_declared_db_name`, which reads any non-declaring value as "this
        # repository declares no application database" and returns `""` — so a
        # stray space in `env_example_file` would turn the validation-database
        # guard OFF while reading as configured. `_repo_relative` refuses
        # padding, so routing every non-empty value through it is the whole fix;
        # `""` is skipped rather than passed in because it would fail that
        # function's empty-segment check.
        if value != "":
            _repo_relative(key, value, allow_globs=(key == "audit_report_glob"))

    if "env_example_db_key" in repo_data:
        key_name = repo_data["env_example_db_key"]
        # `_KEY_RE`'s shape, spelled here rather than imported: this is the key
        # LOOKED UP in the repository's example env file, and a value with
        # spaces or an '=' in it could never match a parsed line anyway — so it
        # is a silent no-match rather than a refusal, which is the failure mode
        # this whole section exists to avoid.
        if not isinstance(key_name, str) or not key_name.strip():
            raise ConfigError(
                f"repo.env_example_db_key must be a non-empty string, got {key_name!r} "
                '— to turn the refusal off entirely, set env_example_file = "" instead, '
                "so the intent is stated in one place rather than inferred from a "
                "blank key"
            )
        if key_name.strip() != key_name or any(ch in key_name for ch in "= \t"):
            raise ConfigError(
                "repo.env_example_db_key must be a plain environment-variable name "
                f"(no padding, no '=', no whitespace), got {key_name!r}"
            )

    return RepoConfig(**repo_data), notices


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
    if "restart_command" in browser_data:
        cmd = browser_data["restart_command"]
        if not isinstance(cmd, list) or not all(isinstance(c, str) for c in cmd):
            raise ConfigError(
                "browser.restart_command must be a list of strings, e.g. "
                f"{_restart_command_toml()}"
            )
        # Stored EXACTLY as written, including a command still naming the
        # retired shell helper (see `RETIRED_RESTART_SCRIPT` above): the loader
        # neither refuses nor rewrites it, so an unmigrated deployment keeps
        # every non-browser command working while the operator migrates.
        browser_data["restart_command"] = tuple(cmd)
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
    _check_keys("paths", paths_data, {"state_dir", "workers_root", "validation_env_file"})
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

    # `validation_env_file` (the validation-environment boundary): OPTIONAL,
    # but absolute when present. Same split as `workers_root` above — "unset"
    # and "relative" are cheap and checked here; "outside the checkout /
    # workers root / publisher / state dir", "not group-readable", "owned by
    # me", "parses under the allowlist" all need a repo root and live in
    # `validation_env.py`, called from `cli.py` AND `doctor.py` (both, so
    # doctor cannot silently skip a check a real run enforces).
    validation_env_raw = paths_data.get("validation_env_file")
    validation_env_file: Path | None = None
    if validation_env_raw is not None and str(validation_env_raw).strip():
        validation_env_file = Path(str(validation_env_raw)).expanduser()
        if not validation_env_file.is_absolute():
            raise ConfigError(
                "paths.validation_env_file must be an absolute path, got "
                f"{validation_env_raw!r} (after expanding '~') — it is read from "
                "several different working directories"
            )

    conversation_data = data.get("conversation", {})
    conversation_fields = {f.name for f in dataclasses.fields(ConversationConfig)}
    _check_keys("conversation", conversation_data, conversation_fields)
    conversation = ConversationConfig(**conversation_data)

    codex_data = dict(data.get("codex", {}))
    _check_keys("codex", codex_data, {f.name for f in dataclasses.fields(CodexConfig)})
    for key in ("command", "sandbox_args", "quota_patterns"):
        if key not in codex_data:
            continue
        value = codex_data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"codex.{key} must be a list of strings")
        if key == "command" and not value:
            raise ConfigError("codex.command must not be empty")
        codex_data[key] = tuple(value)
    codex = CodexConfig(**codex_data)

    executor_data = data.get("executor", {})
    _check_keys("executor", executor_data, {f.name for f in dataclasses.fields(ExecutorConfig)})
    executor = ExecutorConfig(**executor_data)
    if executor.kind not in ("audit", "null"):
        raise ConfigError(f"executor.kind must be 'audit' or 'null', got '{executor.kind}'")

    audit_data = dict(data.get("audit", {}))
    # BEFORE `_check_keys`, which would otherwise report the retired key as a
    # generic "unknown key" — true, but useless to someone holding a config
    # that used to work.
    migration_notices = _migrate_retired_timeout(audit_data)
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
    # Checked here rather than left to fail at kill time. A stall window at or
    # above the ceiling reads as configured while being unreachable — the
    # backstop would always fire first and the progress detector would never
    # run, which is precisely the elapsed-time bound this replaced.
    for name in ("audit_agent_timeout_seconds", "agent_stall_seconds", "agent_ceiling_seconds"):
        value = getattr(audit, name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"audit.{name} must be a positive number, got {value!r}")
    if audit.agent_stall_seconds >= audit.agent_ceiling_seconds:
        raise ConfigError(
            f"audit.agent_stall_seconds ({audit.agent_stall_seconds}) must be "
            f"strictly below audit.agent_ceiling_seconds ({audit.agent_ceiling_seconds}) "
            "— otherwise the absolute backstop always fires first and the stall "
            "detector never runs, while still reading as configured"
        )

    repo, repo_notices = _load_repo_section(data)

    return AutoloopConfig(
        browser=browser,
        policy=policy,
        state_dir=state_dir,
        workers_root=workers_root,
        validation_env_file=validation_env_file,
        conversation=conversation,
        codex=codex,
        executor=executor,
        audit=audit,
        repo=repo,
        migration_notices=migration_notices + repo_notices,
    )
