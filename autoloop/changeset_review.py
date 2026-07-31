"""Operator-changeset review: bind ChatGPT's stamped approval to a specific,
already-existing commit an operator authored directly on the branch — never
produced by this loop's own executor (contrast `packet.py` / `worktask.py`'s
produce-then-review path, which commits ON BEHALF of a task in its own
worktree/worker repo).

Before this module, a `push` directive answering anything other than a
produce-then-review candidate hit `_dispatch`'s `legacy_git_path_retired`
refusal (`orchestrator.py`, docs/SECURITY.md S21) — so an operator's own
infrastructure commits could only ever be published by hand. This module
gives that flow an equally exact-SHA-bound path, WITHOUT reopening the
retired one: `Orchestrator._dispatch_changeset_push` (see that method) is a
new dispatch branch, gated on a `ChangesetBinding` recorded by
`python -m autoloop review-changeset`, never on a bare `push` directive.

`ChangesetBinding` mirrors `state.PostcommitBinding`'s shape and field
naming deliberately (see that class's docstring) rather than inventing a
parallel vocabulary, but the two are kept as separate types: a
`PostcommitBinding` is anchored to a task and its own worktree
(`task_id`/`task_branch`, cross-checked against `TaskExecutionStore` at
push time); a `ChangesetBinding` has neither — the reviewed candidate lives
directly in the RUNNING checkout that built the packet, and `dest_ref` is
recorded explicitly (rather than derived at push time) since nothing here
assumes the destination ref shares the source branch's name.

**Why `head_sha`-based staleness detection does not apply here.** The
generic review-integrity stamp (`context.build_context`'s `head_sha`) is the
RUNNING checkout's `git rev-parse HEAD` at the moment a request is sent —
for the produce-then-review task path that checkout is the ORCHESTRATOR's
own repo, which legitimately never moves while a task works in its own
separate worktree, so "HEAD is unchanged" is a meaningful freshness check
there. For an operator changeset, the running checkout IS the branch being
reviewed, and the whole point of this feature is that the operator may keep
committing to it after a review packet is sent — the recorded
`candidate_sha`, not whatever HEAD has since become, is what must publish.
`Orchestrator._step_executing` skips the generic HEAD-moved check for a
changeset-bound response accordingly; identity is carried entirely by
`ChangesetBinding.candidate_sha` plus the `report_sha256` digest, exactly
as it is for a postcommit push.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import GitCommandError
from .git_gateway import GitGateway
from .packet import _format_changed_paths, _format_commit_list
from .policy import PolicyEngine

#: Mirrors `git_gateway._SHA_RE` / `publisher._SHA_RE` — a bound sha must be
#: a literal, already-resolved 40-hex commit id, never a ref, tag, or HEAD.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass
class ChangesetBinding:
    """Which operator-authored changeset a single request/response pair
    concerns — captured once, when the review packet is queued, and
    (`candidate_tree_sha` aside) never recomputed from a later "latest
    state" lookup. See the module docstring for how this relates to
    `state.PostcommitBinding`.

    `candidate_tree_sha` is the candidate commit's tree object id. It is
    captured at binding time and re-derived from the immutable commit
    object at push time (`Orchestrator._dispatch_changeset_push`); the two
    disagreeing only if something replaced the object (`git replace`) or
    the object database itself is inconsistent — exactly the class of
    tamper `packet_sha256` alone cannot see, since that hash covers the
    rendered packet TEXT, not a fresh read of the object right before
    publishing it.

    `packet_sha256` is the digest of the FULL rendered request payload
    (`context.report_sha256`), which embeds this packet verbatim — so it
    covers the literal `base_sha`/`candidate_sha` text stamped into it. An
    approval that echoes THIS digest could only have reviewed THIS
    candidate; a review of a different commit hashes to a different digest.
    Left empty by `build_changeset_binding` and filled in later, at
    `Orchestrator._current_pending_changeset` time, once the actual
    payload text exists to hash — mirrors `PostcommitBinding`'s own late
    binding via `_current_pending_postcommit`.
    """

    base_sha: str
    candidate_sha: str
    candidate_tree_sha: str
    branch: str
    dest_ref: str
    packet_sha256: str


def _require_sha(label: str, value: object) -> str:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        raise GitCommandError(
            f"review-changeset refuses {label}={value!r}: must be a literal, "
            "already-resolved 40-hex commit id"
        )
    return value


def build_changeset_binding(
    git: GitGateway, policy: PolicyEngine, base_sha: str, candidate_sha: str
) -> ChangesetBinding:
    """Validate `base_sha`/`candidate_sha` and bind them to the branch this
    checkout is currently on. Raises `GitCommandError` — never guesses,
    never defaults — if either sha is not literal 40-hex, does not resolve
    to a commit object, `candidate_sha` is not a descendant of `base_sha`,
    or the current branch is protected.

    `packet_sha256` is left empty; see `ChangesetBinding`'s docstring for
    why it can only be filled in later.
    """
    base_sha = _require_sha("base_sha", base_sha)
    candidate_sha = _require_sha("candidate_sha", candidate_sha)
    for label, sha in (("base_sha", base_sha), ("candidate_sha", candidate_sha)):
        info = git.read_commit(sha)  # raises GitCommandError if unresolvable
        if not info.get("tree"):
            raise GitCommandError(
                f"review-changeset refuses {label}={sha!r}: does not resolve to a "
                "commit object"
            )
    if not git.is_descendant(candidate_sha, base_sha):
        raise GitCommandError(
            f"review-changeset refuses: candidate {candidate_sha[:12]} is not a "
            f"descendant of base {base_sha[:12]}"
        )
    branch = git.current_branch()
    if not branch:
        raise GitCommandError(
            "review-changeset refuses: the checkout is not on a named branch "
            "(detached HEAD?) — a changeset review needs a branch to publish to"
        )
    dest_ref = f"refs/heads/{branch}"
    protected = () if policy.config.allow_protected_push else policy.config.protected_branches
    if branch in protected or dest_ref in protected:
        raise GitCommandError(
            f"review-changeset refuses: branch {branch!r} is protected "
            f"({sorted(protected)}) — check out a non-protected branch before "
            "queuing a changeset review"
        )
    candidate_tree_sha = git.tree_of(candidate_sha)
    return ChangesetBinding(
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        candidate_tree_sha=candidate_tree_sha,
        branch=branch,
        dest_ref=dest_ref,
        packet_sha256="",
    )


def build_changeset_packet(
    git: GitGateway, binding: ChangesetBinding, body: str | None = None
) -> str:
    """Render the review packet for `binding`, from immutable git objects
    only — mirroring `packet.build_review_packet`'s approach (commit list,
    changed paths, diffstat, and `range_diff` with its existing safe flags
    and byte cap, reused here rather than reimplemented).

    `branch`/`dest_ref`/`base_sha`/`candidate_sha` are ALWAYS stamped as
    literal labeled lines, sourced from `binding` — never from `body`. This
    is what lets `report_sha256` (hashed over the whole rendered request,
    which embeds this packet verbatim) bind an approval to exactly this
    candidate even when `body` is supplied: an operator-provided packet
    body replaces the git-rendered diff CONTENT, never the header the
    binding is checked against.
    """
    header = [
        "OPERATOR CHANGESET REVIEW PACKET — an operator-authored changeset "
        "reviewed directly from immutable git objects; not produced by this "
        "loop's own executor.",
        f"branch: {binding.branch}",
        f"dest_ref: {binding.dest_ref}",
        f"base_sha: {binding.base_sha}",
        f"candidate_sha: {binding.candidate_sha}",
        "",
    ]
    if body is not None:
        return "\n".join(header + [body.strip()])
    base_sha, candidate_sha = binding.base_sha, binding.candidate_sha
    commits = git.commit_list(base_sha, candidate_sha)
    changed = git.commit_range_paths(base_sha, candidate_sha)
    base_entries = git.tree_entries(git.tree_of(base_sha))
    candidate_entries = git.tree_entries(git.tree_of(candidate_sha))
    stat = git.range_diff_stat(base_sha, candidate_sha)
    diff = git.range_diff(base_sha, candidate_sha)
    return "\n".join(
        header
        + [
            f"Commits ({base_sha[:12]}..{candidate_sha[:12]}, oldest first):",
            _format_commit_list(commits),
            "",
            f"Changed paths ({len(changed)}):",
            _format_changed_paths(changed, base_entries, candidate_entries),
            "",
            "Diff stat:",
            stat.strip() or "  (empty)",
            "",
            "Full diff:",
            diff,
        ]
    )
