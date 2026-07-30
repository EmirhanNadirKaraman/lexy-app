"""The post-commit review packet: everything ChatGPT needs to review a
produce-then-review commit, rendered ONLY from immutable git objects in the
range `task_base_sha..candidate_sha` — never from what the executor claimed
to have done, never from a working-tree read.

`build_review_packet` stamps `task_id`, `branch`, `base_sha` and
`candidate_sha` into the returned text as literal, labeled lines. This is not
decoration: the returned string becomes `state.outbox`, and
`context.report_sha256` hashes exactly those bytes — that hash is the value a
later approval must echo (`contract.verify_review`). Two different commits
(different shas, different parents, possibly different trees entirely) can
still produce byte-identical `git diff` TEXT — the unified diff alone does
not pin which commit was reviewed. Concatenating the four identifiers into
the hashed body is what closes that gap: an approval computed over candidate
A's packet cannot be replayed to authorize publishing candidate B, because
B's packet — differing in at least its `candidate_sha:` line — hashes to a
different `report_sha256`.
"""

from __future__ import annotations

from .git_gateway import GitGateway
from .tasks import Task
from .worktask import TaskExecution


def _format_commit_list(commits: list[dict]) -> str:
    if not commits:
        return "  (no commits)"
    lines = []
    for commit in commits:
        parents = ", ".join(p[:12] for p in commit["parents"]) or "(root)"
        lines.append(f"  {commit['sha'][:12]}  parents=[{parents}]  {commit['subject']}")
    return "\n".join(lines)


def _format_changed_paths(
    changed: set[str],
    base_entries: dict[str, tuple[str, str, str]],
    candidate_entries: dict[str, tuple[str, str, str]],
) -> str:
    """One line per changed path with its mode and object TYPE on each side
    of the range — not inferred from the diff text, read directly from the
    two trees via `GitGateway.tree_entries`, so a reviewer can see e.g. a
    plain-file-to-symlink swap or an executable-bit flip even if the diff
    renderer would show neither distinctly."""
    if not changed:
        return "  (no changed paths)"
    lines = []
    for path in sorted(changed):
        before = base_entries.get(path)
        after = candidate_entries.get(path)
        if before is None and after is not None:
            mode, kind, oid = after
            lines.append(f"  A  {path}  mode={mode} type={kind} oid={oid[:12]}")
        elif before is not None and after is None:
            mode, kind, oid = before
            lines.append(f"  D  {path}  mode={mode} type={kind} oid={oid[:12]} (deleted)")
        elif before is not None and after is not None:
            bmode, _bkind, boid = before
            amode, akind, aoid = after
            tag = "M" if bmode == amode else "M(mode changed)"
            lines.append(
                f"  {tag}  {path}  mode={bmode}->{amode} type={akind} "
                f"oid={boid[:12]}->{aoid[:12]}"
            )
        else:  # pragma: no cover - commit_range_paths only reports real diffs
            lines.append(f"  ?  {path}  (absent from both trees)")
    return "\n".join(lines)


def build_review_packet(execution: TaskExecution, worktree_git: GitGateway, task: Task) -> str:
    """Render the full review packet for `execution.candidate_sha` against
    `execution.task_base_sha`, reading only from the worktree's own
    `GitGateway` (never the main checkout's).

    Raises `GitCommandError` (via `range_diff`/`range_diff_stat`) if the diff
    exceeds the byte cap — the caller decides how to handle that; this
    function does not truncate or hide anything to avoid it.
    """
    base_sha = execution.task_base_sha
    candidate_sha = execution.candidate_sha

    commits = worktree_git.commit_list(base_sha, candidate_sha)
    changed = worktree_git.commit_range_paths(base_sha, candidate_sha)
    base_entries = worktree_git.tree_entries(worktree_git.tree_of(base_sha))
    candidate_entries = worktree_git.tree_entries(worktree_git.tree_of(candidate_sha))
    stat = worktree_git.range_diff_stat(base_sha, candidate_sha)
    diff = worktree_git.range_diff(base_sha, candidate_sha)

    return "\n".join(
        [
            "POST-COMMIT REVIEW PACKET — rendered only from immutable git",
            "objects in the range below; nothing here is claimed, everything is read.",
            f"task_id: {task.id}",
            f"task_title: {task.title}",
            f"branch: {execution.task_branch}",
            f"base_sha: {base_sha}",
            f"candidate_sha: {candidate_sha}",
            f"review_round: {execution.review_round}",
            "",
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
