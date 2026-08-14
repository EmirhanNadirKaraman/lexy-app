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

**The packet always carries the whole patch; DELIVERY is what is bounded.**
`build_review_packet` inlines the complete diff unconditionally, however
large it is, because that string is the *logical* packet — the thing
`report_sha256` must cover. What a single chat message can carry is a
separate question, answered by `plan_chunked_delivery` below: an oversized
patch is delivered as numbered parts, each under `PART_INCLUDE_MAX_CHARS` —
its own budget, sized against what the COMPOSER accepts and reads back rather
than against the generation failure that sizes `DIFF_INCLUDE_MAX_CHARS` — and
the verdict is asked for only once every part is confirmed present. The two
limits are separate constants on purpose; conflating them is what stalled
delivery on 2026-08-14 (see `PART_INCLUDE_MAX_CHARS`).

The hash still covers the complete packet
(diff included), so an approval echoing it cannot bind to a subset of the
review — see `orchestrator._step_delivering` for the all-or-nothing rule and
`_fall_back_to_omission` for what happens when a part does not land.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

from .git_gateway import GitGateway
from .tasks import Task, unauthorized_paths
from .worktask import TaskExecution

#: How much patch text may be INLINED in the one message that asks for a
#: verdict — i.e. the size at which a patch stops being inlined and starts
#: being chunked. Deliberately far below `GitGateway.RANGE_DIFF_MAX_BYTES`
#: (400 KB): that cap bounds what git is asked to RENDER, this one bounds what
#: a chat message can actually DELIVER. See `_format_diff_section` for the
#: failure that set it — a 38 KB diff was accepted by the composer, failed
#: generation server-side, and left no message in the conversation at all.
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
#:
#: This is the THRESHOLD, not the part size. A patch over it is chunked rather
#: than inlined; how much patch each of those parts carries is
#: `PART_INCLUDE_MAX_CHARS` below, which is sized from different evidence
#: entirely. Until 2026-08-14 one constant did both jobs, and the composer
#: bound — the lower of the two — went unrepresented. It is NOT raised by
#: chunking; chunking is what removes the pressure to raise it (see
#: `test_the_cap_is_sized_from_evidence_not_instinct`).
DIFF_INCLUDE_MAX_CHARS = 30_000

#: How much patch text ONE DEPOSITED PART may carry — the per-composer-message
#: budget, and deliberately NOT `DIFF_INCLUDE_MAX_CHARS`.
#:
#: The two numbers answer different questions and failed in different places:
#:
#:   * `DIFF_INCLUDE_MAX_CHARS` is sized against GENERATION. A 40,056-character
#:     message was accepted by the composer on 2026-08-04 and then failed
#:     server-side, leaving no message in the conversation at all.
#:   * this one is sized against the COMPOSER. `_enter_prompt`
#:     (`browser/chatgpt.py`) refuses to click Send until it can read the whole
#:     request back out of the contenteditable, and that read-back is bounded by
#:     `input_sync_timeout_seconds` (30.0s).
#:
#: Observed 2026-08-14 on brw-08 (request alr-7ad33735-0006): with parts at
#: 30,000 the read-back timed out repeatedly — `composer did not accept the
#: full request diffpart_..._02of02 within 30.0s (nothing was sent)` — while the
#: operator could SEE the text sitting in the composer. The refusal is correct
#: (a part sent unverified could be partial, and a partial part passed off as
#: whole is exactly what `report_sha256` exists to prevent), so the loop
#: correctly declined to send, exhausted its failure budget and parked.
#:
#: The bound is LATENCY-shaped, not capacity-shaped, so there is no clean
#: character cliff to put here: `docs/COMMON_ERRORS.md` §6 records 104k- and
#: 113k-character prompts that synced fine on 2026-07-31. 8,000 is the largest
#: per-message patch budget with an unbroken read-back record — it was the cap
#: in force until 2026-08-05 and never failed to sync; it was raised because it
#: was too CONSERVATIVE for the SINGLE-message case (it blocked rt-02 at 8,971
#: characters), never because it failed to send.
#:
#: It bounds the part's BODY, and `_render_part` adds ~450 characters of
#: identity block and instruction on top — which is still comfortably inside
#: that record rather than at its edge. Under the old cap the same 8,000
#: bounded the diff inside a WHOLE packet: commit list, changed paths, diff
#: stat, executor report, plus `build_prompt`'s context block and contract
#: instructions, for 12–15k-character messages that synced every time. A part
#: at ~8,450 is smaller than any message that ever synced under that cap.
#:
#: Raising it needs an instrumented live run that records read-back latency
#: against part size, not another instinct. Record what such a run measures in
#: the `docs/COMMON_ERRORS.md` §6 entry ("composer did not accept the full
#: request diffpart_…") alongside the two numbers already there, so the next
#: person can still tell the composer limit from the generation one.
PART_INCLUDE_MAX_CHARS = 8_000

#: How many parts a chunked delivery may take before the loop gives up and
#: falls back to the omission notice. A judgement, and labelled as one: the
#: only real data point is sub-01's 41 KB patch, which is six parts at the
#: current part size. Six parts is therefore a ceiling of ~48 KB of patch —
#: down from the ~180 KB it meant while parts were 30,000 characters, because
#: the ceiling is `DIFF_MAX_PARTS * PART_INCLUDE_MAX_CHARS` and the part size
#: dropped to a deliverable one on 2026-08-14. That still covers the largest
#: candidate ever observed; past it, "reply `revise` asking for a smaller
#: commit" — which the omission notice already says — is a better answer than a
#: dozen chat messages nobody can hold in their head at once. The COUNT is the
#: lever if a real patch needs more: raise it only with that patch's size
#: recorded here, and never by re-raising the part size, which is the number
#: that has a measured failure behind it.
DIFF_MAX_PARTS = 6

#: The literal line the inline diff section starts with. `plan_chunked_delivery`
#: locates `_INLINE_DIFF_HEADER + diff` inside the rendered payload and swaps it
#: for a delivery notice, which is what lets the *sent* message stay small while
#: `state.outbox` — the thing `report_sha256` covers — keeps the whole patch.
_INLINE_DIFF_HEADER = "Full diff:\n"


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


def _format_out_of_scope(changed: set[str], allowed: tuple[str, ...]) -> str:
    """Which changed paths the task's declared scope did not authorize —
    computed HERE, from `changed` (git's own `commit_range_paths`) against
    `execution.allowed_paths`, exactly like every other read section.

    This is the control that replaces a refusal. Since 2026-08-05 both scope
    gates are ADVISORY (`docs/SECURITY.md` S25 amendment): an out-of-scope path
    no longer parks the task, it reaches a human. So a reviewer who never sees
    the list is a reviewer who cannot exercise the judgement the park used to
    make for them — which is why this renders UNCONDITIONALLY, with an explicit
    none-line. Absence of the section must never be the signal; "(none)" is.

    Deliberately NOT read from `TaskExecution.out_of_scope_paths`, even though
    that field holds the same answer by the time a packet is built. That record
    is the union of BOTH gates, and the pre-commit one compares
    `outcome.changed_paths` — the executor's own report. Sourcing the section
    from it would let a report naming a file it never touched put that file in
    front of the reviewer as a fact about the commit. Same rule as everywhere
    else in this module: the executor gets exactly one labelled section, and
    this is not it.

    `allowed` is `execution.allowed_paths`, the same input `_verify_committed`
    compares against, so the packet and the post-commit check cannot disagree
    about what counted as out of scope. Note an AUDIT execution unions its
    committed paths into `allowed_paths` by design (`orchestrator.py`'s
    `is_audit` branch), so an audit packet reports none — that is the audit
    path having no declared scope to exceed, not a missing check.
    """
    outside = unauthorized_paths(changed, allowed)
    if not outside:
        return (
            "Out-of-scope paths (0):\n"
            "  (none — every changed path is inside the task's declared scope)"
        )
    scope = ", ".join(sorted(allowed)) or "(nothing declared)"
    return (
        f"OUT-OF-SCOPE PATHS ({len(outside)}) — this commit touched "
        f"{len(outside)} path(s) the task's\n"
        "declared scope did not authorize. Read from git (the changed paths of\n"
        "the range above, checked against the task's approved paths), NOT from\n"
        "anything the executor reported:\n"
        + "\n".join(f"  ! {path}" for path in sorted(outside))
        + "\n"
        "  The scope check is ADVISORY — these paths did NOT stop the commit,\n"
        "  so this list is the only place the overrun surfaces. A declared\n"
        "  scope is a prediction made before the code was read, so a wrong one\n"
        "  is ordinary; judge whether these edits belong to THIS task, and\n"
        "  reply `revise` if they do not.\n"
        f"  Declared scope: {scope}"
    )


def build_review_packet(execution: TaskExecution, worktree_git: GitGateway, task: Task) -> str:
    """The rendered review packet — see `build_review_packet_with_diff`, of
    which this is the text half. Kept as the plain-string entry point because
    most callers (and every test that only inspects the rendered packet) have
    no use for the diff separately."""
    return build_review_packet_with_diff(execution, worktree_git, task)[0]


def build_review_packet_with_diff(
    execution: TaskExecution, worktree_git: GitGateway, task: Task
) -> tuple[str, str]:
    """Render the full review packet for `execution.candidate_sha` against
    `execution.task_base_sha`, reading only from the worktree's own
    `GitGateway` (never the main checkout's), and return it together with the
    raw patch text it embeds.

    The patch is returned separately, rather than recovered later by slicing
    the rendered string, because the caller needs the exact bytes twice: once
    inside the hashed packet and once as the source of the delivery parts. Two
    derivations of "the diff" that could ever disagree is precisely the drift
    that would let an approval bind to something other than what was shown.

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
            # Directly under the path list it is about, and ABOVE both the
            # executor's report and the diff — the diff is what gets OMITTED on
            # a large commit, and this must survive that (see
            # `_format_diff_section`).
            _format_out_of_scope(changed, execution.allowed_paths),
            "",
            "Diff stat:",
            stat.strip() or "  (empty)",
            "",
            _format_executor_report(execution),
            "",
            _format_diff_section(diff),
        ]
    ), diff


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
    """The full patch, inline, whatever its size.

    This is the LOGICAL packet: `context.report_sha256` hashes it, so it has
    to contain everything the review is about. Bounding it here would mean the
    hash covered less than the review did.

    What a single chat message can carry is a different question, and it has a
    measured answer. `GitGateway.RANGE_DIFF_MAX_BYTES` (400 KB) is a safety cap
    on what git is asked to render; it is not a limit on what the REVIEWER can
    receive. On 2026-08-04 rt-09 produced a 38 KB diff — legal by that cap —
    and ChatGPT could not process the message at all: the composer accepted it
    and rendered optimistically (so the loop logged `request_submitted:
    confirmed`), generation then failed server-side, and the whole turn was
    never persisted. Reloading the conversation showed no user message. The
    loop waited 486s for a reply to a message that did not exist, restarted
    Chrome between attempts, and finally tried to rotate to a fresh chat —
    which failed identically, because the same oversized payload could not
    land there either. Three separate blockers, one cause.

    That failure was ONE MESSAGE being too big, not the total volume — several
    smaller messages land fine. So the size question is answered at DELIVERY
    (`plan_chunked_delivery`), not by amputating the packet here.
    """
    diff = diff.strip()
    if not diff:
        return _INLINE_DIFF_HEADER + "  (empty)"
    return _INLINE_DIFF_HEADER + diff


# ---- chunked delivery -------------------------------------------------------


@dataclass(frozen=True)
class DiffPart:
    """One numbered message of an oversized patch.

    `part_id` is what the loop reads back from persisted history to confirm the
    part landed — the same standard a submission is held to, never the send's
    own say-so.
    """

    part_id: str
    index: int
    total: int
    body: str
    text: str


@dataclass(frozen=True)
class DeliveryPlan:
    """How to deliver one oversized packet: the parts to send first, the
    abridged payload to ask the verdict with, and the payload to fall back to
    if any part does not land.

    All three are derived from the SAME rendered payload the caller passes in,
    so nothing here can present a patch that differs from the hashed one:
    `final_payload` and `fallback_payload` are that payload with the inline
    diff section textually replaced, and every part body is a slice of the
    exact diff string it contained.
    """

    parts: tuple[DiffPart, ...]
    final_payload: str
    fallback_payload: str


def payload_carries_diff(payload: str, diff: str) -> bool:
    """Is `diff` inlined in `payload` exactly once, as the diff section?

    The precondition for every rewrite below. It is what lets a patch be
    carried alongside the payload it is already inside without the two being
    able to drift: a stored diff that does not match its payload is not
    "close enough to use", it is a different packet, and the caller must
    ignore it rather than deliver parts of something nobody hashed.
    """
    return payload.count(_INLINE_DIFF_HEADER + diff.strip()) == 1


def diff_part_id(request_id: str, index: int, total: int) -> str:
    """The confirmation token for part `index` of `total`.

    Deliberately NOT `request_id` with a suffix. Every provider answers
    "did this land?" with a substring search over user messages
    (`BrowserChatGPT.has_request`), so a part carrying the request id verbatim
    would make `_step_submitting`'s pre-send reconciliation match part 1 and
    conclude the verdict request had already been sent — the verdict message
    would never go out, and the loop would sit waiting for a reply to a
    question nobody was asked. The id is therefore transformed (hyphens to
    underscores) so it is unmistakably the same request to a human reader and
    structurally not the same token to a substring search.

    `plan_chunked_delivery` re-checks that property on the values it actually
    built rather than trusting this construction; see it for what happens when
    the check fails.
    """
    return f"diffpart_{request_id.replace('-', '_')}_{index:02d}of{total:02d}"


def split_diff_into_parts(diff: str, max_chars: int = PART_INCLUDE_MAX_CHARS) -> list[str]:
    """Split `diff` into ordered slices of at most `max_chars`, preferring line
    boundaries. Concatenating the result reproduces `diff` byte for byte —
    that is the property the reviewer's "is this the whole patch?" rests on, so
    nothing is trimmed, re-joined or normalised along the way.

    A single line longer than `max_chars` (a minified bundle, a generated blob)
    is cut mid-line rather than allowed to overflow the message budget. Losing
    the line boundary is cosmetic; overflowing is the failure this whole
    mechanism exists to avoid.
    """
    if max_chars <= 0:  # pragma: no cover - defensive; callers pass the constant
        raise ValueError("max_chars must be positive")
    slices: list[str] = []
    rest = diff
    while rest:
        if len(rest) <= max_chars:
            slices.append(rest)
            break
        window = rest[:max_chars]
        cut = window.rfind("\n")
        # `cut + 1` keeps the newline with the part that precedes it. A cut at
        # 0 would produce an empty slice and never terminate, so fall through
        # to the hard cut in that case too.
        if cut <= 0:
            cut = max_chars - 1
        slices.append(rest[: cut + 1])
        rest = rest[cut + 1:]
    return slices


def _render_part(
    part_id: str, index: int, total: int, body: str, task_id: str, candidate_sha: str
) -> str:
    """One part message.

    Deliberately NOT built through `prompts.build_prompt`: that appends
    `CONTRACT_INSTRUCTIONS`, which tells the reviewer to answer with a
    directive. A part is a deposit, not a request — asking for a verdict here
    would invite exactly the partial-diff approval the all-or-nothing rule
    exists to prevent.
    """
    return "\n".join(
        [
            f"[autoloop review diff part {index} of {total} | {part_id}]",
            f"task_id: {task_id}",
            f"candidate_sha: {candidate_sha}",
            "",
            f"This is part {index} of {total} of the patch for the review request "
            "that follows. It is NOT a question and needs no reply — do not "
            "review, approve or comment yet. The packet asking for your verdict "
            f"arrives after part {total}, and names all {total} part ids so you "
            "can check none is missing before deciding.",
            "",
            body,
        ]
    )


def _format_diff_delivered_in_parts(part_ids: tuple[str, ...], diff_chars: int) -> str:
    listed = "\n".join(f"    {i}. {pid}" for i, pid in enumerate(part_ids, start=1))
    return (
        f"Full diff: DELIVERED ABOVE IN {len(part_ids)} PARTS — {diff_chars} "
        f"characters, over the {DIFF_INCLUDE_MAX_CHARS}-character single-message\n"
        "  limit, so it was sent as numbered messages rather than omitted.\n"
        "  Nothing was truncated and nothing was summarised: concatenating the\n"
        "  parts in order reproduces the patch exactly. The parts, in order:\n"
        f"{listed}\n"
        "  Every part was confirmed present in this conversation's persisted\n"
        "  history before this message was sent — you are not being asked to\n"
        "  decide on a partial delivery. If any part is nevertheless missing\n"
        "  from what you can read, say so and reply `revise` rather than\n"
        "  approving on the parts you do have."
    )


def _format_diff_omitted(diff_chars: int, parts_may_have_landed: bool) -> str:
    # Worded "if any appear" rather than "the parts above", because the caller
    # knows a chunked delivery was ATTEMPTED but not how many messages actually
    # reached the conversation — that is the very thing that failed. Naming a
    # count we cannot verify would be the same class of claim this packet
    # exists to avoid.
    disown = (
        "\n"
        "  If any `autoloop review diff part` messages appear above, IGNORE\n"
        "  them: a chunked delivery of this patch was started and did not\n"
        "  complete, so whatever landed is a FRAGMENT, not the change. Do not\n"
        "  review it as if it were the patch.\n"
        if parts_may_have_landed
        else ""
    )
    return (
        f"Full diff: OMITTED — {diff_chars} characters, over the "
        f"{DIFF_INCLUDE_MAX_CHARS}-character send limit.\n"
        "  Nothing was truncated: the patch is absent, not shortened, so no\n"
        "  section above is a partial view of it. The changed-path list and\n"
        "  diff stat ARE complete and are read from git.\n"
        f"{disown}"
        "  To read the patch itself:\n"
        f"    git diff-tree -r -p <base_sha> <candidate_sha>\n"
        "  If you cannot review the change without it, reply `revise` asking\n"
        "  for a smaller commit rather than approving unseen."
    )


def plan_chunked_delivery(
    payload: str,
    diff: str,
    request_id: str,
    task_id: str,
    candidate_sha: str,
    max_chars: int = DIFF_INCLUDE_MAX_CHARS,
    max_parts: int = DIFF_MAX_PARTS,
    part_max_chars: int = PART_INCLUDE_MAX_CHARS,
) -> DeliveryPlan | None:
    """Plan the delivery of `payload` — the complete logical packet, diff
    inline — as parts plus a verdict request. `None` means "do not chunk":
    either the patch fits in one message (the common case, unchanged
    behaviour) or chunking cannot be done safely.

    `max_chars` and `part_max_chars` are two different limits and are kept as
    two parameters on purpose. `max_chars` decides WHETHER to chunk — it is the
    single-message threshold, sized against the generation failure. Once the
    answer is yes, `part_max_chars` decides how big each part is, and it is
    sized against what the composer accepts and reads back. They were one
    parameter until 2026-08-14, which sized every part to a number no part had
    ever been measured against; see `PART_INCLUDE_MAX_CHARS`.

    A returned plan always carries its own `fallback_payload`, and the caller
    must keep it: a part that does not land is answered by sending THAT
    instead, never by proceeding with a half-delivered patch.

    Returns `None` when a precondition cannot be guaranteed — the caller then
    renders `omission_payload` and behaves exactly as it did before chunking
    existed:

    * the inline diff section does not appear exactly once in `payload` — the
      payload is not the one this diff was rendered into, so the abridged
      message could not be derived from it honestly;
    * a part id would contain the request id as a substring, which would break
      every provider's "has this request landed?" check (see `diff_part_id`);
    * the patch needs more than `max_parts` parts of `part_max_chars` each.
    """
    diff = diff.strip()
    if not diff or len(diff) <= max_chars:
        return None

    section = _INLINE_DIFF_HEADER + diff
    if payload.count(section) != 1:
        return None

    slices = split_diff_into_parts(diff, part_max_chars)
    if len(slices) > max_parts:
        return None

    total = len(slices)
    part_ids = tuple(diff_part_id(request_id, i, total) for i in range(1, total + 1))
    if any(request_id in pid for pid in part_ids):
        # Checked on the built values, not argued from the format string: if
        # the request-id shape ever changes so the transform stops separating
        # them, this must fail closed to omission rather than silently make
        # the verdict message unsendable.
        return None

    parts = tuple(
        DiffPart(
            part_id=pid,
            index=i,
            total=total,
            body=body,
            text=_render_part(pid, i, total, body, task_id, candidate_sha),
        )
        for i, (pid, body) in enumerate(zip(part_ids, slices), start=1)
    )
    return DeliveryPlan(
        parts=parts,
        final_payload=payload.replace(
            section, _format_diff_delivered_in_parts(part_ids, len(diff))
        ),
        fallback_payload=payload.replace(section, _format_diff_omitted(len(diff), True)),
    )


def omission_payload(payload: str, diff: str, parts_may_have_landed: bool = False) -> str:
    """`payload` with its inline diff replaced by the omission notice — the
    pre-chunking rendering, used when chunking is unavailable or has failed.

    Returns `payload` unchanged when the section is not found exactly once;
    there is nothing honest to substitute in that case, and a payload that
    already lacks an inline diff is already in this form.
    """
    section = _INLINE_DIFF_HEADER + diff.strip()
    if payload.count(section) != 1:
        return payload
    return payload.replace(
        section, _format_diff_omitted(len(diff.strip()), parts_may_have_landed)
    )
