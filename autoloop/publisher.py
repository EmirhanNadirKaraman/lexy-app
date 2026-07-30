"""The publisher side of Autoloop M2 — a controlled repository workers
cannot modify, and the only path through which a candidate commit is ever
made public.

THREAT MODEL — same boundary as `worker_env.py`: git and the OS are TRUSTED;
the process sandbox is the edge. `Publisher` does not defend against a fully
compromised parent process (if the orchestrator's own process is hostile,
nothing here helps) — it defends against a WORKER'S ordinary git access
reaching a real push destination. Concretely: `Publisher` is a SEPARATE
repository from any worker's, with its own controlled-empty hooks directory
and its own single configured remote, and the only two operations it exposes
(`import_candidate`, `publish`) each take an explicit, already-resolved
40-hex commit id — never a ref, branch name, or "current HEAD" of anything —
so nothing about which object gets published can be steered by a worker
maneuvering its own repository state, or by a later change to any repo's
HEAD. "Only the deterministic parent Autoloop process may invoke
publication" is enforced up to the process boundary: nothing plumbs a
`Publisher` instance (or the state needed to construct one — the publisher
repo's path and remote policy) down into worker-facing code or a
directive-authorization path in this codebase. It is NOT enforced against a
worker that can reach the publisher repo's filesystem path directly and run
git against it itself — that is the same out-of-scope "arbitrary shell code"
case documented in `worker_env.py`, and is exactly why `verify_worker_isolation`
(`worker_env.py`) checks that a worker's OWN git configuration carries no
reference to a publisher repo's location at all.

**Why a dedicated repo, not the main checkout.** The main checkout is a
working repository a human actively uses, with its own `.git/hooks` (which
on this platform ships EXECUTABLE `*.sample` files by default — verified,
see `git_gateway.py`'s hook-checking methods) and no reason to ever have its
`core.hooksPath` silently redirected by autoloop. `provision_publisher_repo`
instead creates (idempotently) a BARE, non-working repository under
`state_dir`, with `core.hooksPath` pointed at a directory this module
creates and keeps empty, and a single `remote.<name>.url` copied from the
main checkout's own — so the same real destination is published to, without
ever touching the main checkout's own git configuration.

**Provisioning uses direct `subprocess.run`, not `GitGateway`.** `git init
--bare` and `git config` (write forms) are not on the policy whitelist
(`policy.py`) and are not directive-driven — this is parent-process
repository SETUP, the same category of operation `WorkerRepoManager.create`
(`worker_env.py`) performs the same way, for the same reason: the whitelist
exists to gate what a model-authored directive can reach, not what the
orchestrator's own provisioning code does before any directive exists.
`Publisher` itself, once constructed, does all of its real work
(`import_candidate`, `publish`) through a policy-gated `GitGateway` exactly
like every other write path in this codebase.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import GitCommandError
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .state import utcnow_iso

#: Mirrors `git_gateway._SHA_RE` — a push/fetch source or want token must be
#: a literal, already-resolved 40-hex commit id, never a ref, tag or HEAD.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _run(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True)
    if proc.returncode != 0:
        raise GitCommandError(
            f"{' '.join(args)} (cwd={cwd}) failed (rc={proc.returncode}): "
            f"{(proc.stderr or proc.stdout).strip()}"
        )


# ---- publisher paths — single source of truth for `cli.py` / `doctor.py` --


def publisher_repo_path(state_dir: Path) -> Path:
    # Resolved for the same reason as WorkerRepoManager: these become
    # subprocess cwd / `git init` targets, and the shipped state_dir is relative.
    return Path(state_dir).resolve() / "publisher.git"


def publisher_hooks_path(state_dir: Path) -> Path:
    # Resolved for the same reason as WorkerRepoManager: these become
    # subprocess cwd / `git init` targets, and the shipped state_dir is relative.
    return Path(state_dir).resolve() / "publisher-hooks"


def publisher_url_snapshot_path(state_dir: Path) -> Path:
    # Resolved for the same reason as WorkerRepoManager: these become
    # subprocess cwd / `git init` targets, and the shipped state_dir is relative.
    return Path(state_dir).resolve() / "publisher_url.json"


# ---- publisher URL policy (v1): a provision-time snapshot, never -----------
# auto-updated from repository configuration -- see `provision_publisher_repo`
# and `reprovision_publisher` below for the two functions that touch it.


def read_publisher_url_snapshot(state_dir: Path) -> str | None:
    """The persisted remote url snapshot, or `None` if never provisioned.

    Raises `GitCommandError` if the file exists but cannot be parsed as the
    expected shape — corruption here is exactly the kind of thing that must
    surface loudly (a mismatch nobody can explain is worse than a missing
    snapshot, which just means "provision first")."""
    path = publisher_url_snapshot_path(state_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GitCommandError(f"publisher url snapshot {path} is corrupt: {exc}") from exc
    url = data.get("remote_url") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url:
        raise GitCommandError(f"publisher url snapshot {path} is malformed: {data!r}")
    return url


def _write_publisher_url_snapshot(state_dir: Path, remote: str, url: str) -> None:
    path = publisher_url_snapshot_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {"remote": remote, "remote_url": url, "recorded_at": utcnow_iso()},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def provision_publisher_repo(
    state_dir: Path, source_git: GitGateway, remote: str = "origin"
) -> Path:
    """Idempotently create/refresh the dedicated publisher repo under
    `state_dir/publisher.git`, with `core.hooksPath` pointed at
    `state_dir/publisher-hooks` (created empty, never wiped if it already
    has content — an existing file there is left for `Publisher`'s own
    construction-time check to refuse loudly, not silently cleaned up).

    **URL policy (v1): a provision-time snapshot, never auto-updated.** The
    FIRST call for a given `state_dir` reads `source_git`'s configured
    `remote.<remote>.url`, persists it to `publisher_url_snapshot_path`
    (`publisher_url.json`), and copies it into the publisher repo's config.
    EVERY call after that re-asserts the PERSISTED SNAPSHOT's value — never a
    fresh read of `source_git` — so a later change to the main checkout's own
    `origin` is NEVER picked up silently. `reprovision_publisher` (below) is
    the ONLY function that updates the snapshot after this first write;
    `doctor` is what surfaces a drift between the snapshot and the main
    checkout's current config, and `Orchestrator._dispatch_task_push` refuses
    to publish while one exists (see its own docstring).

    Returns the publisher repo's path. Raises `GitCommandError` if
    `source_git` has no `remote.<remote>.url` configured on first-ever
    provisioning, or if the bare repo already carries MULTIPLE values for
    that key (git refuses a plain `config <key> <value>` write in that case —
    surfaced here rather than silently picking one).
    """
    publisher_path = publisher_repo_path(state_dir)
    hooks_dir = publisher_hooks_path(state_dir)
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if not publisher_path.exists():
        _run(["git", "init", "-q", "--bare", str(publisher_path)], cwd=Path(state_dir).resolve())
    _run(["git", "config", "core.hooksPath", str(hooks_dir)], cwd=publisher_path)

    snapshot_url = read_publisher_url_snapshot(state_dir)
    if snapshot_url is None:
        url = source_git.config_get(f"remote.{remote}.url")
        if not url:
            raise GitCommandError(
                f"provision_publisher_repo refused: source repo has no "
                f"remote.{remote}.url configured — nothing to snapshot"
            )
        _write_publisher_url_snapshot(state_dir, remote, url)
        snapshot_url = url
    _run(["git", "config", f"remote.{remote}.url", snapshot_url], cwd=publisher_path)
    return publisher_path


def reprovision_publisher(
    state_dir: Path, source_git: GitGateway, remote: str = "origin", confirm: bool = False
) -> str:
    """The ONLY function that changes the publisher url snapshot after its
    first write. `confirm` has no default that makes this callable by
    accident: the sole caller anywhere in this codebase is `cli.py`'s
    `reprovision-publisher --confirm` command, an explicit operator action —
    nothing in `orchestrator.py`'s dispatch path, `authorize_directive`, or
    any other directive-reachable code calls this function AT ALL (not
    merely "with confirm=False"); see `test_v1_smoke.py` for the structural
    assertion that a ChatGPT directive can never reach it.

    Reads `source_git`'s CURRENT `remote.<remote>.url`, overwrites the
    snapshot with it, and re-provisions the publisher repo to match. Returns
    the newly-snapshotted url. Raises `GitCommandError` if `confirm` is not
    `True`, or if `source_git` has no configured url for `remote`.
    """
    if not confirm:
        raise GitCommandError(
            "reprovision_publisher refused: confirm=True was not passed. This "
            "is the ONLY way the publisher url snapshot changes after it is "
            "first written — call it again with confirm=True once the new "
            "destination has been verified by a human."
        )
    url = source_git.config_get(f"remote.{remote}.url")
    if not url:
        raise GitCommandError(
            f"reprovision_publisher refused: source repo has no "
            f"remote.{remote}.url configured — nothing to snapshot"
        )
    _write_publisher_url_snapshot(state_dir, remote, url)
    provision_publisher_repo(state_dir, source_git, remote)
    return url


class Publisher:
    """The only object that publishes a candidate commit.

    Construction runs the SAME structural checks `push_exact` runs at push
    time (single url, no pushurl, no mirror, no followTags, no insteadOf)
    plus a hooks-directory emptiness check — as an early, loud pre-flight,
    not as the thing that actually closes any of them. `GitGateway.
    push_exact` is what closes them, unconditionally, on every call,
    regardless of what this constructor already saw; these are
    belt-and-braces so a misconfigured publisher repo fails at construction
    time (or at the next `provision_publisher_repo` call, if construction
    happens on every run) rather than only failing deep inside a `publish()`
    call.

    **Known limitation: the remote url is a PROVISION-TIME snapshot.**
    `provision_publisher_repo` copies `remote.<remote>.url` from the main
    checkout ONCE, when it runs; a `Publisher` instance then holds that
    snapshot for as long as it is kept alive (in the current wiring, the
    orchestrator's whole run). Nothing here re-reads the main checkout's
    config at push time, and the current orchestrator wiring does not pass
    `publish`'s optional `expected_url` either. If an operator changes the
    main checkout's `origin` mid-session, this `Publisher` keeps publishing
    to the STALE destination — including its `remote_ref_sha` idempotency
    pre-check, which queries that same stale remote — unlike the legacy
    `worktree_git.push_exact` path, which reads live config on every call.
    Re-provisioning (calling `provision_publisher_repo` again and
    reconstructing `Publisher`) picks up a changed url; nothing does that
    automatically today.
    """

    def __init__(self, repo_root: Path, remote: str, policy: PolicyEngine, runner=None):
        self._git = GitGateway(Path(repo_root), policy, runner=runner)
        self.remote = remote
        self._check_hooks_or_raise()
        self._check_remote_or_raise()

    @property
    def repo_root(self) -> Path:
        return self._git.repo_root

    def _check_hooks_or_raise(self) -> None:
        hooks_dir = self._git.hooks_dir()
        if not hooks_dir.is_dir():
            return
        entries = sorted(p.name for p in hooks_dir.iterdir())
        if entries:
            raise GitCommandError(
                f"Publisher refuses: hooks directory {hooks_dir} is not "
                f"empty: {entries}. A publisher repo's hooks directory must "
                "be controlled and empty — nothing here was executed or "
                "bypassed; the offending file(s) were left untouched."
            )

    def _check_remote_or_raise(self) -> None:
        g = self._git
        instead_of = g.config_get_regexp(r"^url\..*\.insteadof$")
        if instead_of:
            raise GitCommandError(
                f"Publisher refuses: url.*.insteadOf rule(s) configured ({instead_of!r})"
            )
        pushurl = g.config_get(f"remote.{self.remote}.pushurl")
        if pushurl:
            raise GitCommandError(
                f"Publisher refuses: remote.{self.remote}.pushurl is configured ({pushurl!r})"
            )
        follow_tags = g.config_get("push.followTags").strip().lower()
        if follow_tags in ("true", "1", "yes", "on"):
            raise GitCommandError("Publisher refuses: push.followTags is enabled")
        if g.config_get(f"remote.{self.remote}.mirror").strip().lower() in (
            "true",
            "1",
            "yes",
            "on",
        ):
            raise GitCommandError(f"Publisher refuses: remote.{self.remote}.mirror is enabled")
        all_urls = g.config_get_all(f"remote.{self.remote}.url")
        if len(all_urls) > 1:
            raise GitCommandError(
                f"Publisher refuses: remote {self.remote!r} has {len(all_urls)} "
                f"configured urls ({all_urls}); exactly one is allowed"
            )
        if not all_urls:
            raise GitCommandError(f"Publisher refuses: remote {self.remote!r} has no configured url")

    def import_candidate(self, worker_repo_path: str | Path, candidate_sha: str) -> str:
        """Fetch `candidate_sha` from `worker_repo_path` — a LOCAL
        filesystem path, never a URL, never a configured remote — by its
        literal 40-hex id, then verify the object that landed is EXACTLY
        that id and IS a commit. Returns `candidate_sha` unchanged, for
        chaining into `publish`.

        No local ref is created for it (`GitGateway.fetch_object` fetches
        with no destination refspec) — the object is anchored only via
        `FETCH_HEAD`, which is enough: every later reference to it, here and
        in `publish`, is by its literal sha, never by a ref. Repeating the
        import is harmless — fetching an object already present in this
        repo's object database is a normal, side-effect-free no-op (verified
        against a real repo).

        Type verification is `GitGateway.read_commit`, which runs
        `cat-file commit <oid>` — that call itself fails (raising
        `GitCommandError`) if `candidate_sha` resolves to anything other
        than a commit object (a blob, a tree, a tag), which is exactly the
        "verify it is a commit" check; there is no separate `cat-file -t`
        step (`cat-file` is policy-whitelisted with an empty flag set, so
        `-t` would be denied at the policy layer before it ever ran).
        """
        if not _SHA_RE.match(candidate_sha or ""):
            raise GitCommandError(
                f"import_candidate refuses a non-40-hex candidate_sha: {candidate_sha!r}"
            )
        source = str(Path(worker_repo_path).resolve())
        self._git.fetch_object(source, candidate_sha)
        info = self._git.read_commit(candidate_sha)
        if not info.get("tree"):
            raise GitCommandError(
                f"import_candidate: {candidate_sha} did not parse as a commit object"
            )
        return candidate_sha

    def publish(
        self,
        candidate_sha: str,
        dest_ref: str,
        protected_refs: Sequence[str],
        expected_url: str | None = None,
    ) -> str:
        """Publish `candidate_sha` to `dest_ref` on `self.remote`, and
        nothing else. `candidate_sha` is treated as an OPAQUE,
        already-resolved 40-hex id supplied by the caller — this method
        never substitutes worker HEAD, this repo's own HEAD, a branch name,
        or any other resolved-at-call-time value for it. The caller (the
        orchestrator) is responsible for sourcing it from the reviewed
        request binding, never from a fresh lookup that could have moved on
        to a later round.

        A defense-in-depth re-check that the object actually exists here and
        is a commit (`read_commit`, same reasoning as `import_candidate`)
        runs before the push, in case `publish` is ever called in a process
        that did not just run `import_candidate` itself. The push itself is
        `GitGateway.push_exact` — reused, not reimplemented: it independently
        re-derives every check `Publisher.__init__` already ran (single url,
        no pushurl, no mirror, no followTags, no insteadOf), refuses a
        protected `dest_ref`, refuses anything but a fast-forward, refuses an
        active pre-push hook, and confirms via a FRESH `ls-remote` round-trip
        that the ref landed at exactly `candidate_sha` — never trusting its
        own push command's exit code alone. `push_exact` never runs
        repository code: it is a `git push` of one already-existing,
        already-verified commit object, nothing more.

        Never executes repository code as part of publication — no hook
        fires (the publisher's hooks directory is verified empty at
        construction and re-verified by `push_exact` immediately before the
        push), and nothing here evaluates or runs anything from the pushed
        commit's own tree.
        """
        self._git.read_commit(candidate_sha)
        return self._git.push_exact(
            self.remote, candidate_sha, dest_ref, protected_refs, expected_url=expected_url
        )

    def remote_ref_sha(self, dest_ref: str) -> str:
        """The sha `dest_ref` currently resolves to on `self.remote`, via a
        fresh network round-trip — used to reconcile after a crash (a push
        that landed but whose confirmation the caller never saw) without
        re-pushing, and to check idempotency before calling `publish` again."""
        return self._git.remote_ref_sha(self.remote, dest_ref)

    def describe(self) -> dict:
        """Diagnostics: the applied settings, with NO secret values — a
        remote url can legitimately be a bare local path in tests, but in
        production is redacted down to its host, never any embedded
        userinfo credential (`https://user:token@host/...`)."""
        url = self._git.config_get(f"remote.{self.remote}.url")
        return {
            "repo_root": str(self._git.repo_root),
            "remote": self.remote,
            "remote_url_redacted": redact_url(url),
            "hooks_dir": str(self._git.hooks_dir()),
        }


def redact_url(url: str) -> str:
    """Strip embedded userinfo (`user:token@`) from a url before it is ever
    logged, returned as diagnostics, or put into a parked question. A bare
    local filesystem path (the common case in tests, and possible in
    production too) has no userinfo to strip and passes through unchanged.
    Public (not `_redact_url`) because `orchestrator.py` and `doctor.py` both
    need it for the publisher-url-drift messages — one redaction routine,
    reused, rather than re-implemented per caller."""
    return re.sub(r"//[^/@]+@", "//", url)
