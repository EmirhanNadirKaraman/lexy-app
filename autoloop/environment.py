"""Snapshot and re-verify the git execution environment that hooks and push
destinations depend on.

Hooks are ALLOWED to exist and to run — `commit_and_capture` uses a normal
`git commit`, not the hook-refusing immutable-tree path (`commit_adopted`).
What this module refuses is the hook SET, the hooks directory, or a push
destination CHANGING between the moment a task starts and the moment its
commit is reviewed or pushed. A hook installed mid-task, or `core.hooksPath`
repointed, or a `url.*.insteadOf` rule appearing, all change what a commit or
a push actually does without changing anything the loop itself asked for —
so a snapshot-then-verify pair around the risky window is the only way to
fail closed on them.

Remote identity is read the same way `push_exact` reads it: directly from
`git config`, never via `git remote -v` / `git remote get-url`. Both of those
report the REWRITTEN destination when a `url.*.insteadOf` rule applies (F5),
so trusting them to notice a rewrite is circular — the rewrite is exactly
what they would silently reflect as normal.
"""

from __future__ import annotations

from dataclasses import dataclass

_REMOTE_URL_KEY_RE_GROUP = r"^remote\..*\.url$"
_INSTEAD_OF_KEY_RE_GROUP = r"^url\..*\.insteadof$"


@dataclass(frozen=True)
class EnvSnapshot:
    hooks_dir: str
    active_hooks: tuple[str, ...]
    core_hooks_path: str  # raw config value; "" if unset
    #: Every `url.<base>.insteadOf` rule as (config key, value), sorted.
    instead_of_rules: tuple[tuple[str, str], ...]
    #: `remote.<name>.url` for every remote, as (name, raw url), sorted.
    remote_urls: tuple[tuple[str, str], ...]
    #: `remote.<name>.pushurl` for every remote, as (name, raw url), sorted.
    #: When set, git pushes THERE INSTEAD of `remote.<name>.url` — verified
    #: empirically (pointing `url` at one bare repo and `pushurl` at a second
    #: sends the push to the second while `url` stays unchanged) — so this is
    #: tracked as its own field, never folded into `remote_urls`. Empty
    #: string when unset, which is the common case, and that absence is
    #: itself part of what must not change.
    remote_pushurls: tuple[tuple[str, str], ...]
    #: `remote.<name>.push` for every remote, as (name, raw value), sorted.
    #: This is a REFSPEC (e.g. `refs/heads/*:refs/heads/*`), not a URL — it
    #: only affects a bare `git push <remote>` with no explicit refspec.
    #: Neither `push()` nor `push_exact` is affected by it (both always pass
    #: an explicit refspec), but it is still tracked: some other, non-gateway
    #: `git push` run during the task window would be.
    remote_push_refspecs: tuple[tuple[str, str], ...]


def _remote_names(git) -> tuple[str, ...]:
    """Remote names derived from `remote.<name>.url` config keys directly —
    never `git remote -v`, for the reason in the module docstring."""
    raw = git.config_get_regexp(_REMOTE_URL_KEY_RE_GROUP)
    names: set[str] = set()
    for line in raw.splitlines():
        key = line.split(" ", 1)[0] if line else ""
        if key.startswith("remote.") and key.endswith(".url"):
            names.add(key[len("remote."):-len(".url")])
    return tuple(sorted(names))


def _instead_of_rules(git) -> tuple[tuple[str, str], ...]:
    raw = git.config_get_regexp(_INSTEAD_OF_KEY_RE_GROUP)
    rules = []
    for line in raw.splitlines():
        if not line:
            continue
        key, _, value = line.partition(" ")
        rules.append((key, value))
    return tuple(sorted(rules))


def snapshot(git) -> EnvSnapshot:
    """Capture the current hook set, hooks directory, `core.hooksPath`,
    every `insteadOf` rule, and every remote's url / pushurl / push refspec."""
    hooks_dir, active = git.active_commit_hooks()
    names = _remote_names(git)
    return EnvSnapshot(
        hooks_dir=str(hooks_dir),
        active_hooks=tuple(active),
        core_hooks_path=git.config_get("core.hooksPath"),
        instead_of_rules=_instead_of_rules(git),
        remote_urls=tuple(sorted((name, git.config_get(f"remote.{name}.url")) for name in names)),
        remote_pushurls=tuple(
            sorted((name, git.config_get(f"remote.{name}.pushurl")) for name in names)
        ),
        remote_push_refspecs=tuple(
            sorted((name, git.config_get(f"remote.{name}.push")) for name in names)
        ),
    )


def verify_unchanged(before: EnvSnapshot, git) -> list[str]:
    """Human-readable violations: anything captured in `before` that has
    since changed. Empty means the environment is exactly as it was."""
    after = snapshot(git)
    violations: list[str] = []

    if after.hooks_dir != before.hooks_dir:
        violations.append(
            f"hooks directory changed: {before.hooks_dir!r} -> {after.hooks_dir!r}"
        )
    if after.core_hooks_path != before.core_hooks_path:
        violations.append(
            f"core.hooksPath changed: {before.core_hooks_path!r} -> {after.core_hooks_path!r}"
        )
    added = sorted(set(after.active_hooks) - set(before.active_hooks))
    removed = sorted(set(before.active_hooks) - set(after.active_hooks))
    if added:
        violations.append(f"new active commit hook(s) since task start: {added}")
    if removed:
        violations.append(f"commit hook(s) no longer active since task start: {removed}")
    if after.instead_of_rules != before.instead_of_rules:
        violations.append(
            "url.*.insteadOf rule(s) changed: "
            f"{list(before.instead_of_rules)} -> {list(after.instead_of_rules)}"
        )
    if after.remote_urls != before.remote_urls:
        violations.append(
            f"remote url(s) changed: {list(before.remote_urls)} -> {list(after.remote_urls)}"
        )
    if after.remote_pushurls != before.remote_pushurls:
        violations.append(
            "remote pushurl(s) changed: "
            f"{list(before.remote_pushurls)} -> {list(after.remote_pushurls)}"
        )
    if after.remote_push_refspecs != before.remote_push_refspecs:
        violations.append(
            "remote push refspec(s) changed: "
            f"{list(before.remote_push_refspecs)} -> {list(after.remote_push_refspecs)}"
        )
    return violations
