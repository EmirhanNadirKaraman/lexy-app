"""Deterministic policy / safety layer.

Pure functions over configuration + state — no LLM involvement, no I/O. Every
check returns a Verdict; the orchestrator and git gateway act on verdicts, and
the git gateway re-validates every git invocation as defense in depth even
though decisions were already authorized upstream.

Hard rules with no configuration escape hatch:
  * force pushes (any form) — the `push` whitelist admits zero flags
  * history rewrites / destructive ops (reset, clean, rebase, checkout, ...)
    — their subcommands are simply not on the whitelist
Configurable rules:
  * protected branches, commit/push enablement, iteration + retry budgets
  * auto-merge (`auto_merge_enabled`, default off)

`merge` IS on the whitelist (auto_merge.py) and is the one subcommand there
that moves the checkout's own branch head. It is not a history rewrite — it
only ever adds a commit whose parents include the previous head — and it
carries its own shape check below: exactly two legal forms, `merge --abort`
with no other token, and a merge of a literal 40-hex commit id. There is
still no way to reach reset/clean/rebase/checkout from any setting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .contract import (
    AUDIT_TASK_ID,
    COMMIT_DECISIONS,
    PUSH_DECISIONS,
    TASK_DECISIONS,
    Decision,
    Directive,
)
from .tasks import TaskRegistry, TaskState


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    code: str
    reason: str

    @classmethod
    def ok(cls) -> "Verdict":
        return cls(True, "ok", "")

    @classmethod
    def deny(cls, code: str, reason: str) -> "Verdict":
        return cls(False, code, reason)


@dataclass(frozen=True)
class PolicyConfig:
    max_iterations: int = 20
    max_consecutive_failures: int = 3
    max_parse_retries: int = 2
    max_policy_denials: int = 3
    #: Review rounds allowed per task. 0 means UNLIMITED, which is the
    #: default: a hard cap of 2 abandoned work that was still converging,
    #: and the operator, not this file, should decide when to give up.
    #: Unlimited is only safe because a SEPARATE guard stops the real
    #: runaway case — identical revise feedback twice in a row means the
    #: round cannot change its own outcome (see
    #: `_revise_feedback_is_unchanged`). Bound this only if you want a
    #: hard ceiling on top of that.
    max_review_rounds: int = 0
    allow_commit: bool = True
    allow_push: bool = True
    protected_branches: tuple[str, ...] = ("main", "master")
    allow_protected_push: bool = False
    # Phase gate: while False (Phase 3 default), `implement` and `revise` of
    # repository tasks are denied deterministically — only the audit-review
    # cycle (audit / revise-of-audit / plan / stamped Markdown commits / push /
    # stop) is executable.
    implement_enabled: bool = False
    #: How many times one run may abandon a wedged conversation for a fresh one
    #: in the same project. One is the deliberate default: rotation exists to
    #: escape a broken chat, and a loop that keeps opening new ones on a
    #: systemic outage would fan out a chat per iteration while looking like it
    #: is making progress.
    max_conversation_rotations: int = 1
    #: Integrate a completed task's published side branch into the base the
    #: loop builds against, and push the base — `auto_merge.py`. OFF by
    #: default, and deliberately so: it is the only setting that moves the
    #: shared branch head without an operator in the loop, and every
    #: in-flight execution record pins a `task_base_sha` that a moved head
    #: can invalidate. The gate (`cli._merge_window_blockers`) makes that
    #: safe; the flag makes it opt-in.
    #:
    #: Note for a base branch listed in `protected_branches`: the merge still
    #: happens locally, but `push_exact` refuses to publish it (logged as
    #: `auto_merge_push_refused`) unless `allow_protected_push` is also set.
    #: That pairing is intentional — enabling auto-merge is not by itself
    #: permission to push `main`.
    auto_merge_enabled: bool = False
    #: How many times one run may hand the reviewer role to the configured
    #: fallback provider after an exhausted allowance. One is the deliberate
    #: default: the useful move is primary -> fallback, and switching back
    #: would require a quota window to have reset, which does not happen inside
    #: a single run.
    max_provider_switches: int = 1


# Whitelist, not blacklist: a git invocation is allowed only if its subcommand
# appears here AND every flag it passes appears in that subcommand's set.
# `push` admitting zero flags is what makes --force / --force-with-lease /
# --delete / --mirror structurally impossible. reset / clean / rebase /
# checkout / filter-branch are not listed, so they are denied regardless of
# flags. `add` admits only `--` (explicit paths) — `git add -A` was removed in
# Phase 3 and there is deliberately no flag or config that brings it back.
# `restore` is admitted ONLY with `--staged` (index-only unstaging; see
# _REQUIRED_GIT) — a worktree-touching `git restore <path>` stays impossible.
_ALLOWED_GIT: dict[str, frozenset[str]] = {
    # `-uall` (`--untracked-files=all`) is admitted alongside the plain form:
    # `GitGateway.dirty_entries_all` needs it so a caller that turns the
    # result into an exact path set (`ImplementExecutor.changed_paths`) sees
    # literal file paths, not a collapsed new-directory entry — see that
    # method's docstring. Read-only either way, so admitting it widens
    # nothing security-relevant.
    "status": frozenset({"--porcelain", "-z", "-uall"}),
    "rev-parse": frozenset(
        {"--abbrev-ref", "--verify", "--show-toplevel", "--short", "--git-path"}
    ),
    "log": frozenset({"-1", "--format=%H", "--format=%s", "--format=%B"}),
    "diff": frozenset({"--stat", "--name-only", "--cached", "-z"}),
    "show": frozenset({"--stat", "--format=%H"}),
    "branch": frozenset({"--show-current"}),
    "add": frozenset({"--"}),
    "commit": frozenset({"-m"}),
    "push": frozenset(),
    "remote": frozenset({"-v"}),
    "restore": frozenset({"--staged", "--"}),
    # Read-only index inspection, needed to verify what was actually STAGED
    # rather than what the working tree currently shows (adopted manifests).
    "cat-file": frozenset(),
    # `-s`/`--` are the pre-existing staged-index-inspection flags (`staged_mode`).
    # `--others`/`--ignored`/`--exclude-standard` are read-only enumeration
    # flags added for the M1 escape detector (`escape_detector.py`), which
    # needs the literal tracked/untracked/ignored path lists `git status`
    # cannot give it without collapsing directories or omitting unchanged
    # tracked files — see `GitGateway.list_tracked_paths` and friends.
    "ls-files": frozenset({"-s", "-z", "--", "--others", "--ignored", "--exclude-standard"}),
    # Immutable-tree commit path (adopted manifests). `git commit` is NOT used
    # there: it runs hooks that can rewrite the index between verification and
    # commit creation. These build and verify a tree, create a commit object
    # from it, and move the branch by compare-and-swap.
    "write-tree": frozenset(),
    "commit-tree": frozenset({"-p", "-m"}),
    "update-ref": frozenset(),
    "symbolic-ref": frozenset(),
    "ls-tree": frozenset({"-r", "-z"}),
    # `-p`/`--stat` + the four `--no-*` safety flags are for the post-commit
    # review path (`range_diff` / `range_diff_stat`): plumbing `diff-tree`
    # honours `diff.external` and textconv filters unless told not to, so
    # rendering a reviewed diff without `--no-ext-diff --no-textconv
    # --no-color --no-renames` could run attacker-configured code or hide a
    # rename behind a heuristic.
    "diff-tree": frozenset(
        {
            "-r",
            "--name-only",
            "-z",
            "-p",
            "--stat",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
        }
    ),
    # Post-commit primitives (produce-then-review): confirming ancestry,
    # listing commits in a range, reading config without trusting
    # `remote -v`/`get-url` (which can reflect an `insteadOf` rewrite rather
    # than the literal configured value — F5), and confirming what actually
    # landed on the remote.
    "merge-base": frozenset({"--is-ancestor"}),
    "rev-list": frozenset(),
    "ls-remote": frozenset({"--heads"}),
    "config": frozenset({"--get", "--get-regexp", "--get-all"}),
    # Autoloop M2 (`publisher.py`'s `import_candidate` / `worker_env.py`'s
    # `WorkerRepo`): fetching a SINGLE already-existing object by its literal
    # 40-hex id from a LOCAL filesystem path, with no destination ref and no
    # wildcard. Zero flags admitted, mirroring `push`'s own empty set — the
    # want-token and source-path shape checks below (F2-style) are what keep
    # this from becoming an arbitrary-refspec fetch.
    "fetch": frozenset(),
    # Auto-merge (`auto_merge.py`): integrating a completed task's reviewed
    # candidate into the base. THE FIRST MUTATING CHECKOUT SUBCOMMAND on this
    # whitelist, so it carries its own shape check below (`sub == "merge"`)
    # rather than relying on the flag loop alone: the thing being merged must
    # be a literal 40-hex commit id, exactly like `push`'s refspec source and
    # `fetch`'s want-token (F2). A branch name can move between the moment the
    # merge window is checked and the moment the merge runs.
    #
    # `--abort` is here so the conflict path can restore the checkout through
    # the same gateway. It is deliberately NOT in `_REQUIRED_GIT` alongside
    # `--no-ff`, because the two forms are mutually exclusive: requiring
    # either flag would deny the other invocation outright.
    "merge": frozenset({"--no-ff", "--no-edit", "--abort", "-m"}),
    # Per-task worktree lifecycle (`worktree.py`'s WorktreeManager). Only the
    # real FLAGS live here — the action verb (add/remove/list/prune) is
    # checked separately below via `_ALLOWED_GIT_VERBS`, because the flag loop
    # in `validate_git_command` only inspects tokens starting with "-" and
    # would otherwise let an unlisted verb like `git worktree lock` through
    # with zero flags to trip on (the same class of gap F2 closed for `push`'s
    # refspec source).
    "worktree": frozenset({"-b", "--force", "--porcelain"}),
}

# Subcommands whose first positional argument (`args[1]`) is an action VERB,
# not a free-form value (a path, a branch name, a sha, ...). The per-flag loop
# above only inspects tokens starting with "-", so without this a verb outside
# the set here would sail through as long as its own flags happened to be
# whitelisted — `git worktree lock <path>` takes no flags at all and would
# otherwise pass silently. `validate_git_command` denies both a missing verb
# (bare `git worktree`) and a verb not in this set.
_ALLOWED_GIT_VERBS: dict[str, frozenset[str]] = {
    "worktree": frozenset({"add", "remove", "list", "prune"}),
}

# Flags that MUST be present for the subcommand to be allowed at all.
_REQUIRED_GIT: dict[str, frozenset[str]] = {
    "restore": frozenset({"--staged"}),
    "add": frozenset({"--"}),
}

#: A `push` refspec's source (the part before ":") must be a literal,
#: already-resolved 40-hex commit id — never a branch name, tag, or `HEAD`,
#: all of which can move between review and push. Matched against the whole
#: token, so `+<40hex>:...` fails this too (belt-and-braces with the
#: `+`-prefix check below, which fires first).
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class PolicyEngine:
    def __init__(self, config: PolicyConfig):
        self.config = config

    # ---- directive authorization -------------------------------------------

    def authorize_directive(
        self, directive: Directive, current_branch: str, registry: TaskRegistry
    ) -> Verdict:
        """Gate commit/push decisions and task references.

        Commits and pushes happen ONLY via directives whose decision explicitly
        approves them (enforced structurally: the orchestrator calls the git
        gateway only from those branches); this check adds the configurable
        layer on top. Task references (implement/revise, and the optional
        completion id on commits) must point at a known task that is not
        blocked and not already completed — deterministic graph state, never
        ChatGPT's claim about it.

        `ask_user` is refused here unconditionally — see the check below.
        """
        decision = directive.decision
        # Retired at runtime, and deliberately ahead of every configurable
        # gate below so that "unconditional" reads that way: no policy flag,
        # branch, or task state can admit it. `ask_user` parked the loop on a
        # question addressed to a human who, in an autonomous run, is not
        # there to answer it — the loop stalled instead of either changing
        # course or ending. `Decision.ASK_USER` deliberately REMAINS in the
        # contract enum: the parser accepting it is what routes a stale
        # `ask_user` reply into the budget-capped corrective-reprompt path
        # below, rather than into a contract violation that says nothing
        # about why the decision is unavailable.
        if decision is Decision.ASK_USER:
            return Verdict.deny(
                "legacy_ask_user_retired",
                "`ask_user` is retired — this loop does not pause for a human "
                "operator. Choose a different course of action, or reply "
                "`stop` with the reason describing what a human needs to "
                "decide.",
            )
        if decision in TASK_DECISIONS:
            if directive.task_id == AUDIT_TASK_ID:
                # The audit pseudo-task: revise re-runs the audit with
                # feedback; "implement audit" is meaningless.
                if decision is Decision.IMPLEMENT:
                    return Verdict.deny(
                        "phase_gate", "'implement' cannot target the audit pseudo-task"
                    )
            else:
                if not self.config.implement_enabled:
                    return Verdict.deny(
                        "phase_gate",
                        "this phase supports audit review only — implement/revise "
                        "of repository tasks is disabled "
                        "(policy.implement_enabled=false). Available: revise the "
                        "audit (task_id='audit'), plan, commit/push approved "
                        "Markdown, stop.",
                    )
                verdict = self._check_task_reference(directive.task_id, registry)
                if not verdict.allowed:
                    return verdict
        elif decision in COMMIT_DECISIONS and directive.task_id is not None:
            # Completing an already-completed task is an idempotent no-op
            # (crash recovery re-dispatches the same approval), so it is
            # allowed here and skipped in the git dispatch.
            verdict = self._check_task_reference(
                directive.task_id, registry, allow_completed=True
            )
            if not verdict.allowed:
                return verdict
        if decision in COMMIT_DECISIONS and not self.config.allow_commit:
            return Verdict.deny(
                "commit_disabled", "commits are disabled by policy (policy.allow_commit=false)"
            )
        if decision in PUSH_DECISIONS:
            if not self.config.allow_push:
                return Verdict.deny(
                    "push_disabled", "pushes are disabled by policy (policy.allow_push=false)"
                )
            if (
                current_branch in self.config.protected_branches
                and not self.config.allow_protected_push
            ):
                return Verdict.deny(
                    "protected_branch",
                    f"direct push to protected branch '{current_branch}' is not allowed",
                )
        return Verdict.ok()

    def _check_task_reference(
        self, task_id: str | None, registry: TaskRegistry, allow_completed: bool = False
    ) -> Verdict:
        if not task_id or not registry.has(task_id):
            return Verdict.deny(
                "task_unknown",
                f"task '{task_id}' is not in the registry — plan it first",
            )
        state = registry.state_of(task_id)
        if state is TaskState.COMPLETED:
            if allow_completed:
                return Verdict.ok()
            return Verdict.deny("task_completed", f"task '{task_id}' is already completed")
        if state is TaskState.BLOCKED:
            return Verdict.deny(
                "task_blocked",
                f"task '{task_id}' is blocked by incomplete dependencies",
            )
        if state is TaskState.BLOCKED_BY_OPERATOR:
            # Quarantined after a `task_fatal` park (see orchestrator.py's
            # blocker classification, docs/AUTOLOOP.md §9c) — enforcement,
            # not just advisory: without this, `next_ready()` keeping the
            # task off the roadmap summary would be the ONLY thing stopping
            # a directive that names the task id directly, and this is the
            # gate every TASK_DECISIONS dispatch passes through before
            # `_dispatch_executor` ever calls `mark_in_progress`.
            return Verdict.deny(
                "task_blocked_by_operator",
                f"task '{task_id}' is quarantined pending an operator answer "
                "(see `python -m autoloop blockers`)",
            )
        return Verdict.ok()

    # ---- git command validation (defense in depth) --------------------------

    def validate_git_command(self, args: Sequence[str]) -> Verdict:
        if not args:
            return Verdict.deny("git_empty", "empty git command")
        sub = args[0]
        allowed_flags = _ALLOWED_GIT.get(sub)
        if allowed_flags is None:
            return Verdict.deny(
                "git_subcommand_forbidden",
                f"git subcommand '{sub}' is not on the whitelist",
            )
        verbs = _ALLOWED_GIT_VERBS.get(sub)
        if verbs is not None:
            if len(args) < 2:
                return Verdict.deny(
                    "git_verb_required",
                    f"'git {sub}' requires an action verb (one of {sorted(verbs)})",
                )
            verb = args[1]
            if verb not in verbs:
                return Verdict.deny(
                    "git_verb_forbidden",
                    f"'git {sub} {verb}' is not allowed — allowed actions for "
                    f"'git {sub}' are {sorted(verbs)}",
                )
        for token in args[1:]:
            if token.startswith("-") and token not in allowed_flags:
                return Verdict.deny(
                    "git_flag_forbidden", f"flag '{token}' is not allowed for 'git {sub}'"
                )
        # F2, closed here in addition to `push`'s empty flag set: the flag
        # loop above only inspects tokens starting with "-", so a refspec
        # like `+<sha>:refs/heads/x` (no leading "-") sailed through
        # untouched and force-pushed with zero flags — reproduced as a real
        # forced update. Both checks below apply to EVERY non-flag `push`
        # token, deliberately not just ones that look like refspecs.
        if sub == "push":
            for token in args[1:]:
                if token.startswith("+"):
                    return Verdict.deny(
                        "git_push_force_prefix",
                        f"push argv token '{token}' starts with '+' — a "
                        "force-update refspec prefix is refused unconditionally, "
                        "regardless of the flag whitelist (F2)",
                    )
                if ":" in token:
                    source = token.split(":", 1)[0]
                    if not _HEX40.match(source):
                        return Verdict.deny(
                            "git_push_refspec_source",
                            f"push refspec '{token}' has a source that is not a "
                            "literal 40-hex commit id — only an already-created, "
                            "already-resolved commit may be pushed by refspec "
                            "through this gateway (F2); a branch name, tag or "
                            "'HEAD' can move between review and push",
                        )
        if sub == "merge":
            # Two shapes and nothing else: `merge --abort` (restore the
            # checkout after a conflict) and `merge [--no-ff] [--no-edit]
            # [-m <msg>] <40-hex>` (integrate one already-resolved commit).
            tokens = list(args[1:])
            if "--abort" in tokens:
                if tokens != ["--abort"]:
                    return Verdict.deny(
                        "git_merge_abort_shape",
                        "'git merge --abort' takes no other arguments — mixing it "
                        f"with {sorted(t for t in tokens if t != '--abort')} is refused",
                    )
                return Verdict.ok()
            positionals = []
            expect_message = False
            for token in tokens:
                if expect_message:
                    expect_message = False
                    continue
                if token == "-m":
                    expect_message = True
                    continue
                if token.startswith("-"):
                    continue
                positionals.append(token)
            if len(positionals) != 1 or not _HEX40.match(positionals[0]):
                return Verdict.deny(
                    "git_merge_commit",
                    "'git merge' is only allowed as 'merge [flags] <40-hex commit "
                    f"id>' (got {positionals!r}) — a branch name, tag or 'HEAD' can "
                    "move between the merge-window check and the merge itself, so "
                    "only an already-resolved, already-reviewed commit id may be "
                    "merged through this gateway",
                )
        if sub == "fetch":
            # Autoloop M2: `git fetch <source> <want>` — exactly two
            # positional arguments, no refspec, no wildcard, no remote name.
            # `source` must be an absolute filesystem path (never a URL
            # scheme like `https://`/`ssh://`, never `user@host:path`
            # scp-syntax, never a relative path) — cheap insurance since the
            # caller (`GitGateway.fetch_object`) always constructs it itself
            # rather than taking it from a directive. `want` must be a
            # literal, already-resolved 40-hex object id, exactly like
            # `push`'s refspec source (F2): a branch name or tag could
            # pull in history nobody asked for.
            positionals = args[1:]
            if len(positionals) != 2:
                return Verdict.deny(
                    "git_fetch_shape",
                    "'git fetch' is only allowed as 'fetch <source> <want>' "
                    f"(got {len(positionals)} argument(s))",
                )
            source, want = positionals
            if "+" in want or ":" in want or not _HEX40.match(want):
                return Verdict.deny(
                    "git_fetch_want",
                    f"fetch want-token {want!r} must be a literal 40-hex object "
                    "id with no ':' (refspec) or '+' (force) — a branch name, "
                    "tag or wildcard is refused",
                )
            if "://" in source or not Path(source).is_absolute():
                # `Path(...).is_absolute()` alone already rejects a leading
                # '-' (flag injection) and scp-like 'user@host:path' syntax —
                # neither starts with '/' — so the '://' check is the only
                # thing doing independent work here; kept as an explicit,
                # named case rather than relying on that being obvious.
                return Verdict.deny(
                    "git_fetch_source",
                    f"fetch source {source!r} must be an absolute local "
                    "filesystem path — no URL scheme, no scp-like "
                    "'user@host:path' syntax, no relative path",
                )
        required = _REQUIRED_GIT.get(sub)
        if required and not required.issubset(args[1:]):
            missing = sorted(required - set(args[1:]))
            return Verdict.deny(
                "git_flag_required",
                f"'git {sub}' is only allowed with {missing} present",
            )
        return Verdict.ok()

    # ---- budgets ------------------------------------------------------------

    def check_iteration_budget(self, next_iteration: int) -> Verdict:
        if next_iteration > self.config.max_iterations:
            return Verdict.deny(
                "iteration_budget",
                f"iteration budget exhausted ({self.config.max_iterations}); "
                "raise policy.max_iterations in the config to continue",
            )
        return Verdict.ok()

    def check_failure_budget(self, consecutive_failures: int) -> Verdict:
        if consecutive_failures > self.config.max_consecutive_failures:
            return Verdict.deny(
                "failure_budget",
                f"more than {self.config.max_consecutive_failures} consecutive browser failures",
            )
        return Verdict.ok()

    def check_parse_budget(self, parse_retries: int) -> Verdict:
        if parse_retries > self.config.max_parse_retries:
            return Verdict.deny(
                "parse_budget",
                f"more than {self.config.max_parse_retries} malformed responses in a row",
            )
        return Verdict.ok()

    def check_rotation_budget(self, rotations_used: int) -> Verdict:
        """May this run abandon a wedged conversation for a fresh one?

        `rotations_used` is how many rotations this run has ALREADY performed
        — genuinely this run: `cli._reset_run_scoped_budgets` zeroes it once
        per process, so the cap bounds autonomous churn WITHIN a run and a
        deliberate operator restart begins with a fresh budget.
        so the check is `>=`, not `>` like the other budgets: those count
        attempts that have happened and ask whether to keep going, this one
        asks permission before the fact.
        """
        if rotations_used >= self.config.max_conversation_rotations:
            return Verdict.deny(
                "rotation_budget",
                f"conversation rotation budget exhausted "
                f"({self.config.max_conversation_rotations} per run)",
            )
        return Verdict.ok()

    def check_provider_switch_budget(self, switches_used: int) -> Verdict:
        """May this run hand the reviewer role to the fallback provider?

        `>=` like `check_rotation_budget` and for the same reason: it asks
        permission before the fact, rather than judging attempts already made.
        """
        if switches_used >= self.config.max_provider_switches:
            return Verdict.deny(
                "provider_switch_budget",
                f"conversation provider-switch budget exhausted "
                f"({self.config.max_provider_switches} per run)",
            )
        return Verdict.ok()

    def check_denial_budget(self, policy_denials: int) -> Verdict:
        if policy_denials > self.config.max_policy_denials:
            return Verdict.deny(
                "denial_budget",
                f"more than {self.config.max_policy_denials} policy-denied directives in a row",
            )
        return Verdict.ok()
