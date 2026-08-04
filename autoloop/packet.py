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

import textwrap

from .git_gateway import GitGateway
from .tasks import Task
from .worktask import TaskExecution

#: How much patch text a packet may carry. Deliberately far below
#: `GitGateway.RANGE_DIFF_MAX_BYTES` (400 KB): that cap bounds what git is
#: asked to RENDER, this one bounds what a chat message can actually DELIVER.
#: See `_format_diff_section` for the failure that set it — a 38 KB diff was
#: accepted by the composer, failed generation server-side, and left no
#: message in the conversation at all.
#:
#: Raised 8_000 → 30_000 on 2026-08-05, because 8_000 was a guess and the
#: evidence says otherwise. The only measured failure was at 40,056 characters;
#: 8_000 was picked as a "safe margin" the same day, with nothing between the
#: two numbers actually tested. It then blocked rt-02 at 8,971 characters — 971
#: over — and the reviewer escalated to the operator rather than approve a diff
#: it could not see, which is the notice below working as intended against a
#: threshold that was not.
#:
#: The thing 8_000 got wrong is worth stating, because the next person tuning
#: this will hit it too: the cap counts PATCH BYTES, not reviewability. rt-02's
#: candidate was 12 insertions and 87 deletions — trivial to review, large to
#: print. Sizing the limit to "how big is the text" while reasoning about it as
#: "how hard is this to review" is what made 8_000 feel defensible.
#:
#: 30_000 keeps a ~25% margin below the one real data point while covering
#: ordinary work. It is still a judgement, not a measurement: if a packet ever
#: fails to land again, record the size HERE rather than halving the number on
#: instinct — that is how the first guess got made.
DIFF_INCLUDE_MAX_CHARS = 30_000


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
            "POST-COMMIT REVIEW PACKET — every section below is READ from",
            "immutable git objects in the range shown, except the one section",
            "explicitly labelled as the executor's own claims.",
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
            _format_executor_report(execution),
            "",
            _format_diff_section(diff),
        ]
    )


def _format_executor_report(execution: TaskExecution) -> str:
    """The executor's own account of the round — the ONLY unread section.

    Labelled loudly on purpose. The packet's value has always been that a
    reviewer reads git rather than a self-description; folding the executor's
    words back in reintroduces exactly the voice that `docs/SECURITY.md`
    finding #2 removed from AUTHORIZATION. It is safe here, and only here,
    because nothing downstream consumes it: path ownership is checked against
    `commit_range_paths` vs `allowed_paths`, ancestry against the real graph,
    and validation by re-running it on the committed tree. A false report can
    mislead the reviewer's JUDGEMENT — it cannot widen scope, fake a passing
    suite, or authorize a push.

    Empty for any record written before these fields existed (a candidate
    committed by an older build, or crash-recovery adoption of one). Saying so
    is better than an unexplained blank: an absent report is not a silent one.
    """
    summary = (execution.report_summary or "").strip()
    details = (execution.report_details or "").strip()
    if not summary and not details:
        return (
            "Executor report (CLAIMED by the executor, not read from git):\n"
            "  (none recorded — this candidate predates report capture, or was\n"
            "   adopted after a crash. Judge from the git-read sections above.)"
        )
    body = "\n".join(part for part in (summary, details) if part)
    return (
        "Executor report (CLAIMED by the executor, not read from git — every\n"
        "other section is read. Treat this as intent, and check it against the\n"
        "changed paths and diff stat above rather than trusting it):\n"
        + textwrap.indent(body, "  ")
    )


def _format_diff_section(diff: str) -> str:
    """The full patch when it is small enough to send, an honest omission
    notice when it is not.

    `GitGateway.RANGE_DIFF_MAX_BYTES` (400 KB) is a safety cap on what git is
    asked to render; it is not a limit on what the REVIEWER can receive. On
    2026-08-04 rt-09 produced a 38 KB diff — legal by that cap — and ChatGPT
    could not process the message at all: the composer accepted it and
    rendered optimistically (so the loop logged `request_submitted:
    confirmed`), generation then failed server-side, and the whole turn was
    never persisted. Reloading the conversation showed no user message. The
    loop waited 486s for a reply to a message that did not exist, restarted
    Chrome between attempts, and finally tried to rotate to a fresh chat —
    which failed identically, because the same oversized payload could not
    land there either. Three separate blockers, one cause.

    Truncating silently would be worse than omitting loudly: a reviewer who
    cannot see that the patch was cut will read a partial diff as the whole
    change. So the notice states the real size, names what IS still authorative
    (paths + stat, both read from git), and says how to read the rest.
    """
    diff = diff.strip()
    if not diff:
        return "Full diff:\n  (empty)"
    if len(diff) <= DIFF_INCLUDE_MAX_CHARS:
        return "Full diff:\n" + diff
    return (
        f"Full diff: OMITTED — {len(diff)} characters, over the "
        f"{DIFF_INCLUDE_MAX_CHARS}-character send limit.\n"
        "  Nothing was truncated: the patch is absent, not shortened, so no\n"
        "  section above is a partial view of it. The changed-path list and\n"
        "  diff stat ARE complete and are read from git.\n"
        "  To read the patch itself:\n"
        f"    git diff-tree -r -p <base_sha> <candidate_sha>\n"
        "  If you cannot review the change without it, reply `revise` asking\n"
        "  for a smaller commit rather than approving unseen."
    )
