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

import re
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import GitCommandError
from .git_gateway import GitGateway
from .policy import PolicyEngine

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


def provision_publisher_repo(
    state_dir: Path, source_git: GitGateway, remote: str = "origin"
) -> Path:
    """Idempotently create/refresh the dedicated publisher repo under
    `state_dir/publisher.git`, with `core.hooksPath` pointed at
    `state_dir/publisher-hooks` (created empty, never wiped if it already
    has content — an existing file there is left for `Publisher`'s own
    construction-time check to refuse loudly, not silently cleaned up), and
    `remote.<remote>.url` copied from `source_git`'s own configured url for
    that remote name. Safe to call every run: re-copies the url (so a
    changed real destination is picked up) and leaves an existing bare repo
    and its objects alone.

    Returns the publisher repo's path. Raises `GitCommandError` if
    `source_git` has no `remote.<remote>.url` configured, or if the bare
    repo already carries MULTIPLE values for that key (git refuses a plain
    `config <key> <value>` write in that case — surfaced here rather than
    silently picking one).
    """
    publisher_path = Path(state_dir) / "publisher.git"
    hooks_dir = Path(state_dir) / "publisher-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if not publisher_path.exists():
        _run(["git", "init", "-q", "--bare", str(publisher_path)], cwd=Path(state_dir))
    _run(["git", "config", "core.hooksPath", str(hooks_dir)], cwd=publisher_path)
    url = source_git.config_get(f"remote.{remote}.url")
    if not url:
        raise GitCommandError(
            f"provision_publisher_repo refused: source repo has no "
            f"remote.{remote}.url configured — nothing to copy"
        )
    _run(["git", "config", f"remote.{remote}.url", url], cwd=publisher_path)
    return publisher_path


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
            "remote_url_redacted": _redact_url(url),
            "hooks_dir": str(self._git.hooks_dir()),
        }


def _redact_url(url: str) -> str:
    """Strip embedded userinfo (`user:token@`) from a url before it is ever
    logged or returned as diagnostics. A bare local filesystem path (the
    common case in tests, and possible in production too) has no userinfo
    to strip and passes through unchanged."""
    return re.sub(r"//[^/@]+@", "//", url)
