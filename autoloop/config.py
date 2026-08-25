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
from .validation import TEST_SELECTION_MODES, TEST_SELECTION_REACHABLE


@dataclass(frozen=True)
class BrowserConfig:
    """Settings for a browser-backed conversation transport.

    **Nothing shipped reads these to choose a transport any more** (brw-16,
    2026-08-25): no browser provider is registered, so `[browser]` is an
    OPTIONAL section that configures a capability the loop does not currently
    have. It is kept — accepted, defaulted, never required — for two reasons:

    * An operator upgrading mid-flight must not have to edit a config file
      before the loop will start. A `[browser]` section that is still populated
      loads exactly as it always did and is simply not consulted.
    * `register_provider(..., browser_backed=True)` still works, so an adapter
      that does drive a browser has somewhere to read its endpoint and its
      timeouts from without reintroducing a config section.

    `conversation_url` therefore DEFAULTS to `""` rather than being required.
    That is the whole of the configuration half of brw-16's claim: a config with
    no `[browser]` section at all constructs this dataclass and loads.
    """

    #: The one persistent chat a browser adapter would drive. Empty is normal
    #: now — `state.LoopState.new("")` is a valid session, and nothing on the
    #: codex transports reads it.
    conversation_url: str = ""
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
    #: Which registered adapter holds the reviewer role. The default is the
    #: transport the loop actually runs on, and since brw-16 (2026-08-25) it has
    #: to be: the browser provider it used to name is no longer registered, so a
    #: config with no `[conversation]` section would otherwise load cleanly and
    #: then fail at `create_conversation` on the first step.
    provider: str = "codex_cli"
    #: Where to go when `provider` reports its allowance exhausted. Empty
    #: disables failover — the loop parks instead, which is the DEFAULT and, as
    #: of brw-16, the only shipped pairing that is not two seats on one budget.
    #:
    #: Historical: the pairing this existed for was codex_cli -> browser_chatgpt.
    #: Codex draws on the ChatGPT plan's AGENTIC allowance (shared with ChatGPT
    #: Work and ChatGPT for Excel) while ordinary ChatGPT conversations draw on a
    #: separate quota, so the browser really did still work once Codex was spent
    #: — two transports AND two budgets. With no browser provider registered
    #: that pairing is gone; naming it here is handled explicitly rather than
    #: ignored (`_migrate_retired_browser_provider`), never left to fail at the
    #: moment of handover.
    fallback_provider: str = ""


@dataclass(frozen=True)
class CodexConfig:
    """The Codex reviewer. Only consulted when a codex provider is used.

    TWO transports share this section, because they share an account, a working
    directory and a timeout:

    * `codex_cli` — one `codex exec` process per turn (`command`,
      `sandbox_args`, `quota_patterns`).
    * `codex_app_server` — one `codex app-server` process holding one thread
      (`app_server_*`, `quota_error_codes`, `rate_limit_error_codes`).

    Nothing is shared between the two sets, so switching
    `conversation.provider` changes which half is read and leaves the other
    half inert rather than half-applied.
    """

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
    #: Substrings that identify a SPENT ALLOWANCE in a FAILED invocation — the
    #: window is used up and waiting does not help, so the loop parks or hands
    #: over. Empty uses `codex.quota.DEFAULT_QUOTA_PATTERNS`. Overridable
    #: because the real wording cannot be confirmed here and will change; every
    #: non-zero exit now logs a real diagnostic (`codex_invocation_failed`), so
    #: the first real exhaustion shows exactly what to add.
    #:
    #: A marker here can no longer be triggered by the loop's OWN prompt: it is
    #: ignored when the prompt accounts for it — either because it occurs in the
    #: text that was sent or because it sits on an output line that does —
    #: because `codex exec` echoes the whole prompt onto stderr. Both
    #: comparisons ignore whitespace and punctuation, so an echo codex re-wraps
    #: is still ignored. Widening this list is therefore safe in a way it was
    #: not before — see `codex/quota.py`.
    quota_patterns: tuple[str, ...] = ()
    #: Substrings that identify a TRANSIENT throttle — the account is being
    #: asked to slow down and the remedy is time, not a different provider.
    #: Empty uses `codex.quota.DEFAULT_RATE_LIMIT_PATTERNS`. Separate from
    #: `quota_patterns` because routing a thirty-second 429 to the permanent,
    #: loop_fatal branch is most of the harm a misclassification here can do.
    rate_limit_patterns: tuple[str, ...] = ()

    # ---- codex_app_server only ------------------------------------------
    #: How to launch the local app-server. It ships with codex-cli and needs no
    #: metered API key of its own; no SDK package is involved, and none may be
    #: assumed (`@openai/codex-sdk` is TypeScript and there is no `codex_sdk`
    #: on this host).
    app_server_command: tuple[str, ...] = ("codex", "app-server")
    #: How much diff text one deposited part carries. Nothing here fights a
    #: composer — the text travels as JSON on a pipe — so this is sized for the
    #: reviewer rather than the transport.
    app_server_part_chars: int = 60_000
    #: Largest attachment this transport will deliver at all. Over it, `submit`
    #: raises with a named reason rather than truncating a patch.
    app_server_max_attachment_chars: int = 4_000_000
    #: Error TYPES that mean an exhausted allowance, matched exactly against a
    #: protocol error's own type field (never against free-form text). Empty
    #: uses `codex.protocol_errors.DEFAULT_QUOTA_ERROR_CODES`. Overridable for
    #: the same reason `quota_patterns` is — the committed protocol reference
    #: carries no error-code enumeration — but a value here is compared with a
    #: named field, not scanned for in everything the server printed.
    #:
    #: SPENT ONLY. A value here routes to the loop_fatal `QuotaExhaustedError`
    #: branch, which has no retry path, and unlike `quota_patterns` there is no
    #: prompt guard on this side to catch a mistake: naming a throttle code here
    #: parks the loop on a limit that clears in thirty seconds. Throttles go in
    #: `rate_limit_error_codes`.
    quota_error_codes: tuple[str, ...] = ()
    #: Error TYPES that mean a TRANSIENT throttle — the server is asking this
    #: account to slow down and the remedy is time. Empty uses
    #: `codex.protocol_errors.DEFAULT_RATE_LIMIT_ERROR_CODES`. These route to
    #: `CodexProtocolError`, which is retryable on the ordinary failure budget.
    #: The numeric HTTP status 429 is recognised here without being listed.
    #:
    #: "Empty" means EMPTY OF CONTENT, for this key and for `quota_error_codes`
    #: both: `protocol_errors.usable_codes` drops blanks first, so a configured
    #: `[""]` falls back to the built-in list rather than becoming a vocabulary
    #: that recognises nothing.
    rate_limit_error_codes: tuple[str, ...] = ()


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
#: Where the TARGET repository ships the per-domain charters its audit agents
#: are briefed with, and the default for `RepoConfig.audit_charters_file`.
#: Relative to the repo root. THIS repository ships that file, so the default is
#: live wiring rather than a placeholder. It stays OPTIONAL for a repository
#: that ships nothing: genuine absence means the built-in charters in
#: `audit/executor.py` (`DEFAULT_DOMAINS`), which is exactly the behaviour that
#: existed before the file could be read at all. Absence only — a directory or
#: an unreadable entry at the path is refused; see
#: `audit.executor.load_charter_domains`.
DEFAULT_AUDIT_CHARTERS_FILE = "docs/audit_charters.toml"


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
    #: Where the repository ships its own AUDIT CHARTERS — the per-domain
    #: briefs the read-only audit subagents are given (`audit/executor.py`).
    #: A repository-relative path to one file; absent on disk means the
    #: built-in charters, which is what makes this compatible rather than a
    #: new requirement. Exactly `""` means "never look", for an operator who
    #: wants the built-ins whatever the checkout happens to contain; padding
    #: is refused rather than read as that opt-out, exactly as above.
    #:
    #: Same rule as the two settings above — this says WHERE the repository
    #: states something about itself, and grants nothing. The charters are
    #: prose handed to agents that are confined at argv level
    #: (`agents.py`'s `--allowedTools`/`--disallowedTools`); a charter cannot
    #: widen what an agent may do any more than a reviewer's scope can. See
    #: `docs/SECURITY.md` S24.
    audit_charters_file: str = DEFAULT_AUDIT_CHARTERS_FILE


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
    #: How the POST-COMMIT validation re-run decides which tests to run.
    #:
    #: `"reachable"` (default) narrows each configured pytest command to the
    #: test files reachable from the commit's own changed paths through the
    #: repository's import graph — see `validation.select_validation_commands`
    #: for the model and for the widening rules that make a narrow run safe.
    #: `"full"` disables selection entirely and every configured command runs
    #: exactly as written.
    #:
    #: The DEFAULT here is what live deployments get, not the value in
    #: `config.example.toml`: that template is copied once and never re-read.
    #: It defaults to `"reachable"` because that is the behaviour that was
    #: asked for; the safety comes from the widening rules (anything the graph
    #: cannot resolve runs the full suite), not from leaving the flag off.
    test_selection: str = TEST_SELECTION_REACHABLE


@dataclass(frozen=True)
class AutonomyConfig:
    """`[autonomy]` — how the loop behaves when a transport or environment
    fault would otherwise park it for a human (halt-02, 2026-08-25).

    OFF BY DEFAULT, and the default is the compatibility contract: with this
    section absent — which is every existing config file, since the template is
    copied once and never re-read — every park behaves exactly as it did
    before, including the classification each park site chose for itself. The
    flag is a single boolean and turning it back off restores that, so nothing
    here forecloses the current behaviour.

    What `enabled` switches on is described by `blockers.AUTONOMOUS_RECOVERIES`,
    which is an ALLOWLIST of six codes and cannot reach
    `blockers.HARD_HALT_CODES` at all.
    """

    #: The flag. False means "park exactly as today" at every site.
    enabled: bool = False
    #: A CEILING on the per-code retry budgets in `blockers.AUTONOMOUS_
    #: RECOVERIES`, never a floor: the effective budget is `min(the code's
    #: own max_attempts, this)`. So lowering it to 0 keeps the set-aside
    #: behaviour while performing no retries at all, and raising it above a
    #: code's own number changes nothing — a config value can restrain the
    #: table, never widen it. Two by default, matching the largest number the
    #: table actually asks for.
    max_recovery_attempts: int = 2


#: What the unconfigured `[paths].state_dir` is called, as a SIBLING of
#: `workers_root` (port-01, 2026-08-23). Everything writable the loop keeps
#: between steps — `state.json`, `tasks.json`, the lock, the transcript, the
#: publisher repo, executions, blockers — lives under it, so this is the last
#: of the loop's own writable paths to leave the checkout. The inbox, the
#: PAUSE flag, the heartbeat and the mutation ledger went first, for the one
#: reason that applies here too: `escape_detector` snapshots the checkout
#: around every write-capable agent call INCLUDING ignored paths, so loop
#: state written inside it mid-round is indistinguishable from an agent
#: writing where it may not.
DEFAULT_STATE_DIR_NAME = "state"

#: The pre-port-01 default, kept as a name rather than a path because that is
#: all it ever was: `Path(".autoloop")`, resolved against the PROCESS CWD at
#: every use — i.e. inside the checkout for any run started from it, and
#: pointing at a directory that does not exist for anything else (see
#: `docs/COMMON_ERRORS.md`, "An autoloop CLI run from a sibling worktree
#: reports on an EMPTY state dir"). Also the directory the config file itself
#: lives in (`cli.DEFAULT_CONFIG` is `.autoloop/config.toml`), which is what
#: `legacy_state_dir_for` uses to find it.
LEGACY_STATE_DIR_NAME = ".autoloop"


def default_state_dir(workers_root: Path) -> Path:
    """Where writable loop state goes when `[paths].state_dir` is unset.

    Beside `workers_root`, exactly like `inbox.inbox_dir_for`,
    `AutoloopConfig.pause_file`, `AutoloopConfig.heartbeat_file` and
    `tasks.mutation_ledger_for` — that path is already required to be absolute
    and outside the checkout, its `.git`, the state dir and the publisher paths
    (`worker_env.validate_workers_root`), so a sibling of it inherits the same
    guarantee without a second rule about where it may point.

    Absolute by construction, because `workers_root` is (`load_config` refuses
    a relative one). That is the second thing this fixes: the old default was
    relative, so every command resolved the state dir against its own cwd and a
    run from the wrong directory reported confidently on an empty one.
    """
    return Path(workers_root).expanduser().parent / DEFAULT_STATE_DIR_NAME


def workers_root_from(raw) -> Path:
    """`[paths].workers_root` as a validated absolute `Path`.

    Extracted from `load_config` (port-06, 2026-08-24) so that ANY reader can
    ask the loop's own question — "where does this deployment keep its worker
    repositories" — and get the loop's own answer or the loop's own refusal.
    The default state directory is derived from this value, so a second reader
    validating it a second way would derive a different default while looking
    like it agreed.

    Raises `ConfigError` for missing, blank, non-string and relative values.
    Every message is the one `load_config` already raised, verbatim: this is a
    move, not a new rule.
    """
    # `not raw` first, exactly as `load_config` tested it before this moved:
    # every falsy TOML value (absent, "", 0, false) is "not configured", and the
    # `.strip()` catches the blank-looking string that is not empty.
    if not raw or not str(raw).strip():
        raise ConfigError(
            "paths.workers_root is required — an ABSOLUTE path outside this "
            "checkout where task worker repositories live (e.g. "
            "\"~/.autoloop/workers\"). There is no default; see "
            "config.example.toml. (Autoloop M1: a worker repo nested inside "
            "the checkout is invisible to every git-based verification "
            "primitive scoped to the checkout.)"
        )
    workers_root = Path(str(raw)).expanduser()
    if not workers_root.is_absolute():
        raise ConfigError(
            f"paths.workers_root must be an absolute path, got {raw!r} "
            "(after expanding '~') — a relative path is ambiguous across the "
            "different working directories worker-repo subprocesses run from"
        )
    return workers_root


def resolve_state_dir(configured, workers_root, *, base: Path | None = None) -> Path:
    """THE rule that turns `[paths]` into a state directory. One function, two
    callers: `load_config` (the loop) and `dashboard._state_dir` (the page).

    Written for port-06 (2026-08-24), which is the bug it exists to make
    unrepresentable: the dashboard used to end `return repo / ".autoloop"` when
    the key was absent, i.e. the PRE-port-01 location, while the loop resolved
    `default_state_dir(workers_root)` — outside the checkout. Both halves of the
    page agreed with each other and disagreed with the loop, so a priority set
    from the page would have been written to an abandoned registry, read back
    correctly, and never reached the running loop. Two implementations drift;
    this one cannot, for the same reason `tasks.unauthorized_paths` is a single
    matcher.

    * **Absent** → `default_state_dir(workers_root)`, which needs a usable
      `workers_root` and REFUSES when there is none. That refusal is the point:
      a reader that cannot resolve the directory must say so rather than guess,
      because a silent guess is what produced the divergence.
    * **Explicit** → honoured **verbatim** — port-01's compatibility contract,
      unchanged in every respect. `~` is deliberately NOT expanded here (unlike
      `workers_root`): the loop honours the literal value, and a reader that
      expanded it would point somewhere the writer never writes. Blank and
      non-string values are refused instead of being read as `Path("")`, which
      is `.` — a guess wearing a value's clothes.
    * **Relative** → resolved against `base` when one is given, and returned as
      written when it is not. That is one rule, not two: `load_config` passes
      no base because the loop honours the operator's value against its OWN
      cwd, and the loop's cwd IS the checkout (`cli` builds every gateway on
      `Path.cwd()` and `DEFAULT_CONFIG` is the relative `.autoloop/config.toml`);
      the dashboard passes the checkout because ITS cwd is wherever the operator
      launched it. Both therefore name the same directory, which is what
      `test_state_dir_location.py` executes with `monkeypatch.chdir`.
    """
    if configured is None:
        root = workers_root_from(workers_root)
        state_dir = default_state_dir(root)
        # The one way the derivation can collide with the path it derives from.
        # Refused HERE, naming the remedy, rather than left to surface later as
        # `validate_workers_root`'s "workers_root is nested beneath the state
        # directory" — true, but baffling to an operator who never configured a
        # state directory at all.
        if state_dir == root:
            raise ConfigError(
                f"paths.workers_root ends in {DEFAULT_STATE_DIR_NAME!r} "
                f"({root}), which collides with the default state "
                "directory derived beside it. Set [paths].state_dir "
                "explicitly, or point workers_root at a differently named "
                "directory."
            )
        return state_dir
    if not isinstance(configured, str) or not configured.strip():
        raise ConfigError(
            f"paths.state_dir must be a non-empty string path, got {configured!r} "
            "— delete the key entirely to use the default beside "
            "paths.workers_root, which is what an absent value means"
        )
    path = Path(configured)
    if base is None or path.is_absolute():
        return path
    return Path(base) / path


def legacy_state_dir_for(config_path: Path) -> Path | None:
    """The pre-port-01 in-checkout state directory for THIS deployment, or
    `None` when there is nothing to point at.

    Derived from the config file's own location rather than from a literal
    `Path(LEGACY_STATE_DIR_NAME)`, because the config has always lived INSIDE
    the state dir — `cli.DEFAULT_CONFIG` is `.autoloop/config.toml` and
    `config_writer` refuses to rewrite the file unless git is verifiably not
    tracking it, i.e. unless it sits in the gitignored state dir. So the
    directory holding the config IS the old state dir, whatever cwd the
    command ran from and wherever `--config` pointed.

    Gated on the directory NAME so it fires only for that shape. A config kept
    anywhere else belongs to an operator who has already said where their state
    lives, and guessing a state dir from an arbitrary parent directory would
    hand `workers_dir` a fallback pointing at something that is not a state dir
    at all.

    Returned UNRESOLVED, so it inherits exactly the resolution the old default
    had: `.autoloop/config.toml` yields `.autoloop`, relative, read against the
    caller's cwd — byte-identical to what `state_dir` used to be. Read-only by
    contract: nothing derives a write target from this (see
    `AutoloopConfig.workers_dir`, its one consumer).
    """
    parent = Path(config_path).parent
    return parent if parent.name == LEGACY_STATE_DIR_NAME else None


@dataclass(frozen=True)
class AutoloopConfig:
    browser: BrowserConfig
    policy: PolicyConfig
    #: THE write target for every path below. Since port-01 (2026-08-23) an
    #: unconfigured one resolves OUTSIDE the checkout (`default_state_dir`);
    #: an explicit `[paths].state_dir` is still honoured verbatim, unchanged,
    #: which is how an existing deployment keeps its state exactly where it is.
    state_dir: Path
    #: The pre-port-01 in-checkout state dir (`legacy_state_dir_for`), or
    #: `None`. READ-ONLY, and set ONLY when the default moved out from under a
    #: deployment — an explicitly configured `state_dir` did not move, so it
    #: gets `None` and nothing here changes for it.
    #:
    #: Deliberately NOT a general read-through fallback: exactly one property
    #: consults it (`workers_dir`, whose entire job is finding what a previous
    #: layout left behind, and which nothing writes to). Every other path below
    #: resolves under `state_dir` and only `state_dir`, because a read that can
    #: silently become a write is how loop state would land back inside the
    #: snapshotted tree. `state.json`, `tasks.json` and the lock are therefore
    #: NOT read from here — see `docs/AUTOLOOP.md` §3h for the one-line remedy.
    legacy_state_dir: Path | None = None
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
    #: `[autonomy]` — default OFF, so every `AutoloopConfig(...)` built
    #: directly (the whole test suite, and `doctor`) gets exactly the
    #: pre-halt-02 parking behaviour without naming the field.
    autonomy: AutonomyConfig = AutonomyConfig()
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

    #: CALLER-LESS since brw-16 (2026-08-25). `cli._cmd_smoke_browser` was its
    #: only reader, and that command is retired — it reads no config at all now.
    #: Kept rather than deleted for two reasons: an operator's `.autoloop/smoke/`
    #: still exists on disk and this is what names it, and if the loop ever wants
    #: a one-round-trip smoke of `conversation.provider` (a new command, with its
    #: own review — deliberately NOT this one renamed) this is where it lives.
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
        never moved).

        **The one place `legacy_state_dir` is read** (port-01, 2026-08-23), and
        the reason that field exists. Moving `state_dir` out of the checkout
        moved this property with it, onto a directory no pre-fix deployment
        ever wrote to — so `doctor`'s `legacy_workers` check would have gone
        permanently silent while still reading as a check that ran. That is the
        fail-open this branch closes: an empty report and "there is nothing
        there" would have become indistinguishable.

        The rule in full: **the first of the two that is a real DIRECTORY, new
        first; the new one when neither is.** So the new location wins whenever
        it holds anything, an absent/unreadable/file-shaped legacy entry falls
        through instead of raising, and — the case `.exists()` would get wrong
        — a stray FILE at the new path does not silence the legacy report,
        because a file is not worker repos and reading it as "the new location
        is populated" would be a check going quiet on garbage.

        Resolvable this way ONLY because every consumer is read-only —
        `doctor.run_doctor` calls `.is_dir()` and `.iterdir()` and reports. A
        property anything WROTE to must never do this, which is why no other
        path here has a fallback: a read that can silently become a write is
        how loop state would land back inside the snapshotted tree."""
        current = self.state_dir / "workers"
        if current.is_dir() or self.legacy_state_dir is None:
            return current
        legacy = Path(self.legacy_state_dir) / "workers"
        return legacy if legacy.is_dir() else current

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
    def pending_upgrade_file(self) -> Path:
        """The one `auto_merge.PendingUpgrade` record: a merge that changed the
        loop's own code, and what became of it (`auto_merge.UpgradeStore`).

        Durable, and outside the session lifecycle for the same reason
        `blockers_dir` is: it is the one-shot marker that keeps a merge which
        imports but fails at runtime from producing a restart loop, so a
        `task_fatal` park or a `reset` clearing it would take exactly the
        guard off. Absent means "nothing to upgrade to", which is also what an
        unreadable one means — see `UpgradeStore`."""
        return self.state_dir / "pending_upgrade.json"

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


_SECTIONS = {
    "browser", "policy", "paths", "conversation", "codex", "executor", "audit",
    "repo", "autonomy",
}


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


#: The conversation provider retired on 2026-08-25 (brw-16). No factory is
#: registered under this name any more; see `conversation._PROVIDERS`.
RETIRED_BROWSER_PROVIDER = "browser_chatgpt"


def _migrate_retired_browser_provider(conversation_data: dict) -> tuple[str, ...]:
    """Handle a `[conversation]` section that still names the browser provider.

    Same treatment, and the same reasoning, as `RETIRED_AGENT_TIMEOUT_KEY` and
    `repo.tracker_paths`: handled EXPLICITLY, never silently, and never by
    refusing a config that used to work. The live `.autoloop/config.toml` is not
    in this repository, so a hard refusal here would make `status`, `doctor`,
    `blockers` and the recovery commands fail on an unmigrated deployment the
    moment this landed — taking away the tooling an operator would use to fix
    it. It shipped naming `fallback_provider = "browser_chatgpt"`, so this is
    the expected state of every deployment on the day brw-16 merges, not an
    edge case.

    The two keys are treated DIFFERENTLY, because only one of them has a
    meaningful neutral value:

    * **`fallback_provider` is NEUTRALISED to `""`** — the documented value for
      "no failover; park instead", a path the orchestrator already implements
      and parks cleanly on (`quota_exhausted`). Left as written it would name an
      unregistered transport, and the loop would discover that only at the
      moment of handover: `_handle_quota_exhausted` does not consult the
      registry, so it would switch, record the switch, and then die in
      `create_conversation`. Failing over to nothing beats failing over to a
      `ConfigError`.
    * **`provider` is NOT rewritten.** Every value of it selects a transport, so
      there is no neutral one, and guessing would run reviews on a transport the
      operator did not choose. The notice says what to set; the config still
      loads, and `doctor`'s provider-registration check (#12) and
      `create_conversation` both name it precisely.
    """
    notices: list[str] = []
    if conversation_data.get("provider") == RETIRED_BROWSER_PROVIDER:
        notices.append(
            "\n".join(
                [
                    f"autoloop: NOTICE — conversation.provider = "
                    f"'{RETIRED_BROWSER_PROVIDER}' was RETIRED on 2026-08-25. No "
                    "browser-backed provider is registered any more: 21 of this "
                    "loop's first 103 blocker records were artifacts of driving a "
                    "browser rather than anything about review quality.",
                    "  NOT rewritten: every provider value selects a transport, so "
                    "there is no neutral one to migrate to and guessing would run "
                    "your reviews somewhere you did not choose. Your config still "
                    "LOADS; the run will refuse at transport construction, naming "
                    "the registered providers.",
                    '  Set conversation.provider = "codex_cli" (or '
                    '"codex_app_server") to start the loop.',
                ]
            )
        )
    if conversation_data.get("fallback_provider") == RETIRED_BROWSER_PROVIDER:
        conversation_data["fallback_provider"] = ""
        notices.append(
            "\n".join(
                [
                    f"autoloop: NOTICE — conversation.fallback_provider = "
                    f"'{RETIRED_BROWSER_PROVIDER}' names a provider RETIRED on "
                    "2026-08-25 and has been read as \"\" (failover disabled) for "
                    "this run.",
                    "  Why not left as written: nothing validates a fallback name "
                    "until the handover happens, so an exhausted allowance would "
                    "switch the reviewer role to an unregistered transport and "
                    "then fail constructing it. Disabled, the same exhaustion "
                    "parks on quota_exhausted with a readable question.",
                    "  Silence this by deleting the key, or point it at the other "
                    "codex seat — but note that both codex transports draw on ONE "
                    "allowance, so that pairing is two seats on one budget and "
                    "buys nothing when the budget is what ran out.",
                ]
            )
        )
    return tuple(notices)


def _repo_relative(section_key: str, value: str, *, allow_globs: bool) -> str:
    """Raise `ConfigError` unless `value` is a plain repository-relative path.

    Every `[repo]` path setting is joined onto a repo root the loop is handed,
    so an absolute path or a `..` would read a file the operator did not point
    the loop at — and the audit glob is passed to `Path.glob`, which raises
    outright on an absolute pattern. `allow_globs` is the one difference:
    `audit_report_glob` is a pattern by definition, while `env_example_file`
    and `audit_charters_file` each name one file and must not quietly match
    several.
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

    for key in ("env_example_file", "audit_report_glob", "audit_charters_file"):
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
            "there and fill in paths.workers_root."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc

    unknown_sections = set(data) - _SECTIONS
    if unknown_sections:
        raise ConfigError(f"unknown config sections: {sorted(unknown_sections)}")

    # `[browser]` is OPTIONAL and unused since brw-16 (2026-08-25) — see
    # `BrowserConfig`. Absent means every default; present means every key is
    # still validated exactly as before and simply not consulted, because an
    # unused section must be ignored rather than rejected.
    browser_data = data.get("browser", {})
    browser_fields = {f.name for f in dataclasses.fields(BrowserConfig)}
    _check_keys("browser", browser_data, browser_fields)
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
    # Read now, RESOLVED below `workers_root` — the default is derived from it
    # (port-01). Kept as the raw value so "absent" stays distinguishable from
    # every value an operator could actually have written.
    state_dir_raw = paths_data.get("state_dir")

    # `workers_root` (Autoloop M1 finding #1): required, absolute, no
    # default — NEVER silently falls back to `state_dir / "workers"`. This
    # catches "missing" and "relative" at load time, cheaply, without needing
    # a repo root; "nested beneath the checkout / its .git / the state dir /
    # the publisher dir" needs that context and is checked separately by
    # `worker_env.validate_workers_root` at the two places a `WorkerRepoManager`
    # actually gets constructed for real dispatch (`cli.py`, `doctor.py`).
    #
    # Through `workers_root_from` since port-06, so the dashboard asks this
    # question of the same code rather than of a second copy of the rule.
    workers_root = workers_root_from(paths_data.get("workers_root"))

    # `state_dir` (port-01, 2026-08-23) — resolved HERE, after `workers_root`,
    # because the default is derived from it, and through the SHARED resolver
    # since port-06 (2026-08-24), because the dashboard resolves the same key
    # and the two must be one rule rather than two agreeing copies.
    #
    # An EXPLICIT value is honoured verbatim, exactly as before: same `Path()`,
    # same relative-resolves-against-cwd behaviour (no `base` is passed here —
    # the loop's cwd is the checkout), no expansion. That is the whole
    # compatibility contract — a deployment that wants its state to stay where
    # it is says so in one line, and `config.example.toml` still ships that
    # line. Such a config also gets `legacy_state_dir=None`: nothing moved out
    # from under it, so there is no older location to look in.
    #
    # UNSET means "beside workers_root", outside the checkout, absolute.
    state_dir = resolve_state_dir(state_dir_raw, workers_root)
    legacy_state_dir = legacy_state_dir_for(path) if state_dir_raw is None else None

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
    # Key check FIRST, on the raw value, exactly as before: a malformed section
    # (`conversation = "x"`) still gets this loader's own "unknown keys in
    # [conversation]" rather than whatever `dict()` would raise about it.
    _check_keys("conversation", conversation_data, conversation_fields)
    # Copied only now, because the migration below REWRITES a key and must not
    # mutate the parsed document. These are real keys carrying a retired VALUE,
    # not unknown keys, so this belongs after the check and before the dataclass
    # — a neutralised fallback can never reach `ConversationConfig`.
    conversation_data = dict(conversation_data)
    conversation_notices = _migrate_retired_browser_provider(conversation_data)
    conversation = ConversationConfig(**conversation_data)

    codex_data = dict(data.get("codex", {}))
    _check_keys("codex", codex_data, {f.name for f in dataclasses.fields(CodexConfig)})
    # Every tuple-typed CodexConfig field must be listed here. `_check_keys`
    # reflects over the dataclass, so a new field is ACCEPTED automatically —
    # which means one omitted from this list lands in a frozen dataclass as a
    # mutable list, skipping the string-element check, and fails somewhere far
    # away instead of here.
    for key in (
        "command",
        "sandbox_args",
        "quota_patterns",
        "rate_limit_patterns",
        "app_server_command",
        "quota_error_codes",
        "rate_limit_error_codes",
    ):
        if key not in codex_data:
            continue
        value = codex_data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"codex.{key} must be a list of strings")
        if key in ("command", "app_server_command") and not value:
            raise ConfigError(f"codex.{key} must not be empty")
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
    if "test_selection" in audit_data:
        selection = audit_data["test_selection"]
        if selection not in TEST_SELECTION_MODES:
            raise ConfigError(
                "audit.test_selection must be one of "
                + ", ".join(f'"{mode}"' for mode in TEST_SELECTION_MODES)
                + f", got {selection!r}"
            )
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

    autonomy_data = dict(data.get("autonomy", {}))
    _check_keys("autonomy", autonomy_data, {f.name for f in dataclasses.fields(AutonomyConfig)})
    # Types are checked HERE rather than left to fail at the first fault. A
    # `enabled = "true"` (a string, which TOML happily carries) is truthy, so an
    # unchecked value would switch autonomous mode ON for an operator who typed
    # it wrong — the one direction this flag must never fail in. `bool` is
    # checked before `int` for `max_recovery_attempts` because `True` IS an int
    # in Python and would otherwise read as a budget of 1.
    if "enabled" in autonomy_data and not isinstance(autonomy_data["enabled"], bool):
        raise ConfigError(
            "autonomy.enabled must be a boolean (true/false), got "
            f"{autonomy_data['enabled']!r} — a non-boolean is refused rather "
            "than coerced, because the truthy reading would turn autonomous "
            "recovery on by accident"
        )
    if "max_recovery_attempts" in autonomy_data:
        attempts = autonomy_data["max_recovery_attempts"]
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
            raise ConfigError(
                "autonomy.max_recovery_attempts must be a non-negative integer, "
                f"got {attempts!r}"
            )
    autonomy = AutonomyConfig(**autonomy_data)

    repo, repo_notices = _load_repo_section(data)

    return AutoloopConfig(
        browser=browser,
        policy=policy,
        state_dir=state_dir,
        legacy_state_dir=legacy_state_dir,
        workers_root=workers_root,
        validation_env_file=validation_env_file,
        conversation=conversation,
        codex=codex,
        executor=executor,
        audit=audit,
        autonomy=autonomy,
        repo=repo,
        migration_notices=migration_notices + conversation_notices + repo_notices,
    )
