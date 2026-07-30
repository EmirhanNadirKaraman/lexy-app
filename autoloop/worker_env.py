"""Worker-side environment and repository isolation (Autoloop M2).

THREAT MODEL — read this before trusting anything in this module. Git and the
OS are TRUSTED. The boundary this module defends is the process sandbox a
worker's subprocesses run inside — arbitrary sandbox escape, direct
credential theft (e.g. reading `~/.ssh/id_ed25519` off disk, which nothing
here prevents), or a worker simply running arbitrary shell code that does
whatever it likes are all OUT OF SCOPE and are NOT claimed to be defended.
What this module DOES guarantee: a worker subprocess that uses git NORMALLY —
inheriting whatever environment and git configuration a naive `subprocess.run`
would hand it — cannot discover or use a push destination, credential helper,
or hook through ORDINARY (inherited/ambient) git configuration, because
`worker_env()` strips every ambient credential/SSH channel and disables every
system/global git config layer, and `WorkerRepoManager` builds a repository
that never has a remote configured in the first place. A worker that
deliberately runs `git remote add … && git push …` with credentials it
obtained some other way (an unencrypted SSH key sitting on disk, a token
typed into a file) is exercising git NORMALLY with a self-supplied
destination — that is "arbitrary shell code doing whatever it likes" and is
explicitly out of scope; see `publisher.py` for the structural guarantee that
actually matters (publication happens only through the deterministic parent
process, over an object it re-verifies by exact id).

**Not wired into task execution in this pass.** There is no repository task
executor yet (`executor.NullExecutor` is a stub, `policy.implement_enabled`
defaults to `False`) — nothing in the current codebase spawns a subprocess to
do a worker's implementation work, so there is nothing to apply `worker_env()`
to yet. This module is built and tested standalone, exactly like
`worktree.py`'s `WorktreeManager` was ahead of its `cli.py` wiring: ready for
a future executor to use, not integrated into `orchestrator.py` in this pass.

**Platform trap (verified empirically on Apple Git 2.39.5, macOS 26.2).**
`GIT_CONFIG_SYSTEM=/dev/null` does NOT suppress the system gitconfig — Apple's
git ships a SECOND, compiled-in system config path
(`/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig`, which
sets `credential.helper=osxkeychain`) that `GIT_CONFIG_SYSTEM` does not
address at all. Only `GIT_CONFIG_NOSYSTEM=1` suppresses it — confirmed with
`git config --list --show-origin` under both env vars. `GIT_CONFIG_SYSTEM` is
therefore never used here; using it instead of `GIT_CONFIG_NOSYSTEM` would
pass on Linux and silently leave a live credential helper active on macOS.

**Why a fresh `WorkerRepoManager` repo, not a `WorktreeManager` linked
worktree (`worktree.py`).** `git worktree add` creates a linked working tree
that SHARES its `.git` directory — and therefore every remote, every hook,
every credential-relevant config key — with the checkout it was created from.
That sharing is exactly the gap Autoloop M2 exists to close: a subprocess
running inside such a worktree has the SAME ordinary git access to the real
origin as the checkout it was linked from, regardless of what environment
variables it is started with. `WorkerRepoManager` instead runs `git init` to
create a genuinely separate repository that has never had a remote
configured, and imports only the base commit's content into it via a
ONE-TIME local-path `git fetch <path> <sha>` — never a persistent configured
remote (verified: this brings in the object without creating
`remote.*.url`).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import GitCommandError
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .worktree import validate_task_id

#: Removed unconditionally: an ambient ssh-agent socket or askpass/ssh
#: wrapper would let a worker authenticate to a real remote even with every
#: git config layer scrubbed. `GIT_SSH`/`GIT_SSH_COMMAND` are the
#: git-specific ssh overrides; removing them reverts to plain `ssh`, which
#: still tries default identity files on disk — that residual channel is
#: direct-credential-access territory (see the module docstring) and is not
#: claimed to be closed here.
_SCRUB_VARS = (
    "SSH_AUTH_SOCK",
    "SSH_ASKPASS",
    "GIT_ASKPASS",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
)

#: Forced to exactly these values regardless of what the parent process had.
#: `GIT_CONFIG_NOSYSTEM` (not `GIT_CONFIG_SYSTEM=/dev/null` — see the module
#: docstring) suppresses every system-level config file; `GIT_CONFIG_GLOBAL`
#: pointed at `/dev/null` suppresses `~/.gitconfig` / `~/.config/git/config`
#: without touching `HOME` itself (the brief is explicit: do not repurpose
#: `HOME`); `GIT_TERMINAL_PROMPT=0` turns a would-be interactive credential
#: prompt into an immediate failure instead of a hang or a human typing a
#: real credential into a worker's prompt.
_FORCED_GIT_CONFIG_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}

def worker_env(base_env: dict | None = None) -> dict:
    """The environment mapping a worker's git subprocesses run under.

    Starts from `base_env` (a copy of `os.environ` if omitted), removes every
    var in `_SCRUB_VARS`, removes every OTHER `GIT_CONFIG*` var the parent
    process might have set (so a parent-supplied override cannot survive),
    then forces `_FORCED_GIT_CONFIG_ENV` on top. `HOME` is never touched —
    the brief calls that out explicitly as a scoped-controls-only boundary,
    and `GIT_CONFIG_GLOBAL=/dev/null` already suppresses the global config
    file that `HOME` would otherwise point at.
    """
    env = dict(base_env if base_env is not None else os.environ)
    for key in _SCRUB_VARS:
        env.pop(key, None)
    for key in list(env):
        if key.startswith("GIT_CONFIG") and key not in _FORCED_GIT_CONFIG_ENV:
            del env[key]
    env.update(_FORCED_GIT_CONFIG_ENV)
    return env


def describe_policy(worker: "WorkerRepo | None" = None) -> dict:
    """Diagnostics describing the isolation policy APPLIED, never a dump of
    an actual environment or config file — so there is nothing secret to
    redact. When `worker` is given, its (non-secret) paths are included.

    Every value here describes POLICY INTENT (what this module always does),
    never a measurement of any particular worker repo's actual state — that
    measurement is `verify_worker_isolation`'s job, not this function's. The
    `*_by_policy` key names are deliberate: a bare `"remote_configured":
    False` would read as an observed fact this function checked, when it is
    really just restating what `WorkerRepoManager.create` always does (never
    calls `git remote add`).
    """
    info: dict = {
        "forced_env": dict(_FORCED_GIT_CONFIG_ENV),
        "removed_env_vars": sorted(_SCRUB_VARS),
        "removed_env_var_pattern": "GIT_CONFIG* other than the forced two above",
        "home_repurposed_by_policy": False,
        "remotes_permitted_by_policy": False,
        "hooks_dir_policy": "controlled, empty, enumerated (not limited to named hooks)",
    }
    if worker is not None:
        info["worker_repo_path"] = str(worker.path)
        info["worker_hooks_dir"] = str(worker.hooks_dir)
        info["worker_branch"] = worker.branch
    return info


@dataclass(frozen=True)
class WorkerRepo:
    task_id: str
    path: Path
    hooks_dir: Path
    branch: str
    base_sha: str

    def gateway(self, policy: PolicyEngine, runner=None) -> GitGateway:
        """A `GitGateway` rooted at this repo, running its subprocesses
        under the SCRUBBED `worker_env()` mapping — NOT the calling
        process's own environment.

        This is not optional decoration: `GitGateway` with no explicit `env`
        inherits whatever ambient credential helper / system config the
        CALLING process happens to have, regardless of how isolated the
        worker repo's own on-disk git config is (verified — see
        `GitGateway.__init__`'s docstring). Any code that wants to observe
        or operate on this repo AS THE WORKER WOULD must go through this
        method, not a bare `GitGateway(self.path, policy)`.
        """
        return GitGateway(self.path, policy, runner=runner, env=worker_env())


def _run(args: list[str], cwd: Path, env: dict) -> None:
    proc = subprocess.run(args, cwd=str(cwd), env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitCommandError(
            f"{' '.join(args)} (cwd={cwd}) failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )


class WorkerRepoManager:
    """Creates one isolated, no-remote repository per task.

    `root_dir` holds one repo directory per task id (`root_dir/<task_id>`).
    `hooks_root_dir` holds one EMPTY controlled hooks directory per task id
    (`hooks_root_dir/<task_id>`), kept SEPARATE from the repo directory
    itself so it never shows up as an untracked path inside the worker's own
    `git status` — mirrors `WorktreeManager`'s reasoning (`worktree.py`) for
    why its `root_dir` must sit outside any checkout it did not create.

    Repository creation and configuration below (`git init`, `git config
    core.hooksPath`) run via direct `subprocess.run`, not `GitGateway`: these
    are PARENT-PROCESS provisioning steps, never directive-driven, so
    routing them through the policy whitelist would be pointless (that
    whitelist exists to gate what a MODEL-AUTHORED directive can reach) and
    would require widening it for `init`/config-write verbs this module is
    the only caller of. The one-time content import (`git fetch <path>
    <sha>`) and the branch checkout DO run under `worker_env()` — belt and
    braces, so the freshly created repo never even transiently sees an
    inherited credential helper or system/global config during setup.
    """

    def __init__(self, root_dir: Path, hooks_root_dir: Path):
        # RESOLVED, deliberately. These paths are used as subprocess `cwd`
        # values and as `git init` targets from a DIFFERENT working directory,
        # so a relative path (the shipped config uses `state_dir = ".autoloop"`)
        # would be re-interpreted against whatever cwd each call happens to use
        # — `git init -q <rel>` run with `cwd=<rel>` nests the repo one level
        # deeper and the next call's `cwd` then does not exist. Tests never saw
        # it because they pass absolute `tmp_path` values.
        self.root_dir = Path(root_dir).resolve()
        self.hooks_root_dir = Path(hooks_root_dir).resolve()

    def path_for(self, task_id: str) -> Path:
        validate_task_id(task_id)
        return self.root_dir / task_id

    def hooks_dir_for(self, task_id: str) -> Path:
        validate_task_id(task_id)
        return self.hooks_root_dir / task_id

    def create(
        self,
        task_id: str,
        source_repo_path: Path,
        base_sha: str,
        branch: str | None = None,
    ) -> WorkerRepo:
        """Create the worker repo for `task_id`, seeded with `base_sha`'s
        content imported (never remote-fetched on an ongoing basis) from
        `source_repo_path`, and check out `branch` (default
        `autoloop/<task_id>`) onto it. Refuses if a repo already exists at
        the target path for this task id.
        """
        validate_task_id(task_id)
        branch = branch or f"autoloop/{task_id}"
        path = self.path_for(task_id)
        hooks_dir = self.hooks_dir_for(task_id)
        if path.exists():
            raise GitCommandError(
                f"worker repo create refused: a repo already exists at {path} "
                f"for task {task_id!r} — remove it first, or this is a resumed "
                "task and the caller should reuse the existing WorkerRepo "
                "rather than calling create() again"
            )
        if hooks_dir.exists() and any(hooks_dir.iterdir()):
            raise GitCommandError(
                f"worker repo create refused: hooks directory {hooks_dir} for "
                f"task {task_id!r} already exists and is not empty"
            )
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.hooks_root_dir.mkdir(parents=True, exist_ok=True)
        hooks_dir.mkdir(parents=True, exist_ok=True)

        env = worker_env()
        _run(["git", "init", "-q", str(path)], cwd=self.root_dir, env=env)
        _run(
            ["git", "config", "core.hooksPath", str(hooks_dir)],
            cwd=path,
            env=env,
        )
        source = str(Path(source_repo_path).resolve())
        # Belt-and-braces (on top of `worker_env()`'s env-level scrubbing):
        # `-c credential.helper=` overrides any credential helper for THIS
        # invocation specifically, regardless of what config layer it came
        # from — meaningful here because `fetch` is the one operation in
        # this method that is network-shaped, even though `source` is
        # always a local filesystem path (never a URL) by construction.
        # `checkout` below never touches a remote, so it gets no such flag.
        _run(
            ["git", "-c", "credential.helper=", "fetch", "-q", source, base_sha],
            cwd=path,
            env=env,
        )
        _run(["git", "checkout", "-q", "-B", branch, "FETCH_HEAD"], cwd=path, env=env)
        return WorkerRepo(
            task_id=task_id, path=path, hooks_dir=hooks_dir, branch=branch, base_sha=base_sha
        )

    def remove(self, task_id: str) -> None:
        """Remove the worker repo directory and its hooks directory for
        `task_id`, if present. Plain filesystem removal — there is no shared
        object database or linked worktree to detach from (unlike
        `WorktreeManager.remove`, which must run `git worktree remove`)."""
        shutil.rmtree(self.path_for(task_id), ignore_errors=True)
        shutil.rmtree(self.hooks_dir_for(task_id), ignore_errors=True)


def verify_worker_isolation(git: GitGateway, expected_hooks_dir: Path | None = None) -> list[str]:
    """Human-readable violations in `git`'s (a worker repo's) configuration.
    Empty means clean.

    `git` MUST be constructed to run under the scrubbed worker environment
    (`WorkerRepo.gateway`, or `GitGateway(path, policy, env=worker_env())`)
    for this to mean what it claims — verified empirically: a `GitGateway`
    with no explicit `env` reports the CALLING process's ambient
    `credential.helper` (e.g. macOS's `osxkeychain`) as if the worker would
    see it, even against a freshly created, genuinely no-remote worker repo,
    because `git config --get-regexp` walks whatever system/global config
    layers the subprocess's OWN environment resolves, not the repo's local
    config in isolation. A `git` built without `worker_env()` applied will
    therefore over-report — which fails closed (a spurious violation blocks
    a task rather than missing a real one) but is not the same claim as
    "this repo, as configured, is isolated".

    Checked, each independently:

      * any remote configured at all (`remote.*.url`) — a worker repo must
        never have one, so presence alone is a violation, not just a
        mismatch;
      * any `remote.*.pushurl`;
      * any `url.*.insteadOf` rule;
      * `push.followTags` enabled;
      * any `remote.*.mirror` enabled;
      * any `credential.*helper` configured (covers both the global-shaped
        `credential.helper` and a URL-scoped `credential.<url>.helper`);
      * `core.hooksPath` not pointed at `expected_hooks_dir` (when given);
      * any ACTIVE hook anywhere in the EFFECTIVE hooks directory —
        enumerated directly (not limited to `GitGateway`'s named
        `COMMIT_HOOKS`/`PUSH_HOOKS` set), because a `WorkerRepoManager`
        repo's controlled hooks directory is created EMPTY: the correct
        state is zero files, named or not, so listing the directory and
        flagging anything executable catches a hook name this module did
        not think to enumerate. (`.git/hooks`'s stock `*.sample` files are
        never at risk of a false positive here precisely because a worker
        repo's `core.hooksPath` is redirected away from `.git/hooks` in the
        first place — see `WorkerRepoManager.create`.)

    An active hook REFUSES the task; this function only reports, the caller
    decides what "refuse" means for it.
    """
    violations: list[str] = []

    remotes = git.config_get_regexp(r"^remote\..*\.url$")
    if remotes:
        violations.append(f"worker repo has a configured remote: {remotes!r}")

    pushurls = git.config_get_regexp(r"^remote\..*\.pushurl$")
    if pushurls:
        violations.append(f"worker repo has a configured pushurl: {pushurls!r}")

    instead_of = git.config_get_regexp(r"^url\..*\.insteadof$")
    if instead_of:
        violations.append(f"worker repo has a url.*.insteadOf rule: {instead_of!r}")

    follow_tags = git.config_get("push.followTags").strip().lower()
    if follow_tags in ("true", "1", "yes", "on"):
        violations.append("worker repo has push.followTags enabled")

    mirrors = git.config_get_regexp(r"^remote\..*\.mirror$")
    for line in mirrors.splitlines():
        if not line:
            continue
        key, _, value = line.partition(" ")
        # A bare boolean key with no "=value" in the ini source (e.g. just
        # `mirror` on its own line) has no value at all here — git ini
        # semantics treat that as `true`, so an empty value counts as a hit.
        value = value.strip().lower() if value else "true"
        if value in ("true", "1", "yes", "on"):
            violations.append(f"worker repo has remote.*.mirror enabled: {key!r}")
            break

    credential_helpers = git.config_get_regexp(r"^credential\..*helper$")
    if credential_helpers:
        violations.append(f"worker repo has a credential helper configured: {credential_helpers!r}")

    effective_hooks_dir = git.hooks_dir()
    if expected_hooks_dir is not None:
        if effective_hooks_dir.resolve() != Path(expected_hooks_dir).resolve():
            violations.append(
                "worker repo's effective hooks directory is "
                f"{effective_hooks_dir}, expected the controlled "
                f"{Path(expected_hooks_dir).resolve()}"
            )
    if effective_hooks_dir.is_dir():
        active = sorted(
            entry.name
            for entry in effective_hooks_dir.iterdir()
            if entry.is_file() and os.access(entry, os.X_OK)
        )
        if active:
            violations.append(
                f"worker repo has active hook(s) in {effective_hooks_dir}: {active}"
            )

    return violations
