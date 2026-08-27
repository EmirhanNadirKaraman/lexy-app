"""Prompt templates and message construction.

Every outgoing message = header (request-id + iteration) + CONTEXT block
(built by `context.py` — includes the review-integrity stamp) + a rendered
payload template + CONTRACT_INSTRUCTIONS.

Templates are strict: rendering with a missing or unknown field raises
TemplateError, so a template/orchestrator drift fails a test instead of
producing a silently mangled prompt. Payload templates carry no context of
their own — the CONTEXT block is injected once by `build_prompt`.
"""

from __future__ import annotations

import string
from dataclasses import dataclass, field

from .contract import CONTRACT_INSTRUCTIONS
from .errors import TemplateError


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    body: str
    fields: frozenset[str] = field(init=False)

    def __post_init__(self):
        names = {
            fname
            for _, fname, _, _ in string.Formatter().parse(self.body)
            if fname is not None
        }
        if any(not n for n in names):
            raise TemplateError(f"template '{self.name}' uses positional {{}} fields")
        object.__setattr__(self, "fields", frozenset(names))

    def render(self, **values: str) -> str:
        unknown = set(values) - self.fields
        if unknown:
            raise TemplateError(
                f"template '{self.name}' got unknown fields: {sorted(unknown)}"
            )
        missing = self.fields - set(values)
        if missing:
            raise TemplateError(
                f"template '{self.name}' is missing fields: {sorted(missing)}"
            )
        return self.body.format(**values)


TEMPLATES: dict[str, PromptTemplate] = {
    t.name: t
    for t in [
        PromptTemplate(
            name="audit",
            body=(
                "You are the engineering reviewer in an autonomous loop. Your "
                "counterpart (an AI software engineer with full access to the "
                "repository) produced the repository report below and will carry "
                "out whatever your directive says. Work is authorized only by "
                "task id — if the roadmap lacks the right tasks, `plan` them "
                "first.\n\n{report}\n\nReview and answer with your next directive."
            ),
        ),
        PromptTemplate(
            name="plan_ack",
            body=(
                "Your plan was accepted: {added_count} task(s) added to the "
                "roadmap.\n\nRoadmap: {roadmap}\n\nAnswer with your next "
                "directive (typically `implement` the next ready task)."
            ),
        ),
        PromptTemplate(
            name="implementation_review",
            body=(
                "The executor finished working on task {task_id} ({task_title}) "
                "under your '{decision}' directive.\n\nStatus: {status}\n\n"
                "{summary}\n\n{details}\n\nValidation: {validation}\n\nReview "
                "the outcome and answer with your next directive (`revise` with "
                "feedback, another task, or a git decision if appropriate)."
            ),
        ),
        PromptTemplate(
            name="commit_approval",
            body=(
                "The executor finished task {task_id} ({task_title}) and "
                "validation passed.\n\n{summary}\n\n{details}\n\nValidation: "
                "{validation}\n\nIf you approve this work, reply `commit` or "
                "`commit_and_push` with commit.message, optionally task_id to "
                "mark the task completed, and the `reviewed` stamp copied from "
                "CONTEXT. If not, reply `revise` with feedback."
            ),
        ),
        PromptTemplate(
            name="push_approval",
            body=(
                "Commit {commit_sha} was created ({commit_message}) and is NOT "
                "yet pushed.\n\nIf you approve publishing it, reply `push` with "
                "the `reviewed` stamp copied from CONTEXT. Otherwise continue "
                "with another directive."
            ),
        ),
        PromptTemplate(
            name="git_report",
            body=(
                "Git action completed: {summary_line}\n\nAnswer with your next "
                "directive."
            ),
        ),
        PromptTemplate(
            name="failure_recovery",
            body=(
                "The previous step failed and nothing further was executed.\n\n"
                "Kind: {failure_kind}\nDetail: {detail}\n\n{guidance}"
            ),
        ),
        # The question here is the loop's own — `state.question`, written by
        # `orchestrator._to_needs_user` when a park needs an operator — never
        # one the reviewer raised: the decision that let it raise one is
        # retired, so "your question" would misattribute every reachable case.
        # The retired decision is deliberately NOT named (a template body that
        # names it offers it; see `test_no_template_offers_a_retired_decision`).
        PromptTemplate(
            name="clarification",
            body=(
                "The run parked on a question for the human operator, who "
                "answered it. The question is the loop's own, not one you "
                "raised.\n\nQuestion: {question}\nAnswer: {answer}\n\nContinue "
                "with your next directive."
            ),
        ),
        PromptTemplate(
            name="audit_kickoff",
            body=(
                "New session. The executor is ready to run the first autonomous "
                "repository audit: read-only subagents per domain (architecture, "
                "tests/CI, security, DB/migrations, documentation drift, "
                "ingestion pipeline), reconciled findings, one dated Markdown "
                "report, and a proposed task graph for your review. Nothing is "
                "committed without your stamped approval.\n\nReply `audit` "
                "(optionally with a scope) to start, or another directive."
            ),
        ),
        PromptTemplate(
            name="postcommit_review",
            body=(
                "The executor committed task {task_id} ({task_title}) directly to "
                "its own branch, and the commit passed structural post-commit "
                "review (ancestry from the task base, path ownership, a clean "
                "worktree, and a re-run of validation against the committed tree). "
                "Nothing has been pushed. The packet below is rendered ONLY from "
                "the immutable committed git objects — not from what the executor "
                "claimed to have done.\n\n{packet}\n\nIf you approve, reply `push` "
                "with the `reviewed` stamp copied from CONTEXT — this publishes "
                "exactly the candidate commit above to its own task branch, "
                "nothing else. If not, reply `revise` with this task_id and "
                "feedback describing what must change (at most one further "
                "revision round is allowed for this task)."
            ),
        ),
        # The ask a candidate too large to RENDER gets instead of a park
        # (split-05). Deliberately a separate template from `postcommit_review`
        # above rather than a variant of it: that one ends by offering `push`,
        # and the whole point here is that there is nothing to approve. The two
        # bodies must never converge.
        PromptTemplate(
            name="postcommit_split_review",
            body=(
                "The executor committed task {task_id} ({task_title}) to its own "
                "branch and the commit PASSED structural post-commit review "
                "(ancestry from the task base, path ownership, a clean worktree, "
                "and a re-run of validation against the committed tree). Nothing "
                "has been pushed — and nothing can be, because its patch is too "
                "large to render, so you are NOT being shown it.\n\n{packet}\n\n"
                "ONE QUESTION, and the file list and per-file line counts above "
                "are what answers it: is this ONE claim, or several?\n\n"
                "  A. SEVERAL — reply `split` with this task_id and at least "
                "{min_successors} successors in `tasks`, each with its own "
                "`approved_paths` and each independently reviewable. The "
                "successor list is FLAT: a successor cannot itself carry a "
                "split, and none of them may be split again afterwards. Each "
                "inherits the {inherited_attempts} attempt(s) already spent "
                "here, so this refunds no budget.\n\n"
                "  B. ONE CLAIM — this work genuinely cannot be cut smaller. "
                "Reply anything else and the task parks for a human operator, "
                "with your reason recorded. That park is the correct answer "
                "sometimes and it is always available.\n\n"
                "THE COMMITTED WORK IS DISCARDED EITHER WAY BY A SPLIT. A split "
                "retires this task into its successors: the commit above is "
                "never published, its execution record is archived and its "
                "worker repository quarantined (nothing is deleted), and the "
                "successors redo the work in pieces from the current base. It "
                "passed validation and post-commit review; it is thrown away "
                "because it cannot be reviewed, not because it is wrong.\n\n"
                "Every other answer parks — including `revise` (it orders the "
                "same size again), `recut` (the same task from a clean base is "
                "the same size), and an approval (you have not read this "
                "change). Answer A, or accept the park."
            ),
        ),
        PromptTemplate(
            name="changeset_review",
            body=(
                "An operator authored a changeset directly on branch {branch} — "
                "not produced by this loop's own executor. The packet below is "
                "rendered ONLY from immutable git objects between base_sha and "
                "candidate_sha; nothing here is claimed, everything is "
                "read.\n\n{packet}\n\nIf you approve, reply `push` with the "
                "`reviewed` stamp copied from CONTEXT — this publishes exactly "
                "the candidate commit above to {dest_ref}, nothing else. This "
                "review path has no revise loop; if you do not approve, reply "
                "`stop` with the reason describing what must change."
            ),
        ),
        PromptTemplate(
            name="smoke_test",
            body=(
                "AUTOLOOP SMOKE TEST — this verifies the browser automation "
                "channel only. It is NOT an engineering request. Do NOT audit, "
                "plan, implement, revise, commit, or push; no code or repository "
                "work of any kind will be accepted.\n\nReply with exactly one "
                "fenced json block:\n"
                '{{"version": 3, "decision": "stop", "reason": "smoke test acknowledged"}}'
            ),
        ),
    ]
}


def request_header(request_id: str, iteration: int) -> str:
    return f"[autoloop request {request_id} | iteration {iteration}]"


def build_prompt(request_id: str, iteration: int, context_block: str, payload: str) -> str:
    return "\n\n".join(
        [
            request_header(request_id, iteration),
            context_block.strip(),
            payload.strip(),
            CONTRACT_INSTRUCTIONS,
        ]
    )


# ---- payload helpers (all rendered through TEMPLATES) -----------------------


def kickoff_payload(report: str) -> str:
    return TEMPLATES["audit"].render(report=report.strip())


def parse_error_payload(code: str, message: str) -> str:
    return TEMPLATES["failure_recovery"].render(
        failure_kind=f"contract_violation ({code})",
        detail=message,
        guidance=(
            "Resend the SAME decision as one valid ```json block matching the "
            "response contract."
        ),
    )


def policy_denied_payload(decision: str, reason: str) -> str:
    return TEMPLATES["failure_recovery"].render(
        failure_kind=f"policy_denied (decision={decision})",
        detail=reason,
        guidance=(
            "Choose a different course of action. If the policy itself has to "
            "change before this can proceed, reply `stop` with that in the "
            "reason — this loop does not pause for a human operator mid-run."
        ),
    )


def review_mismatch_payload(code: str, message: str) -> str:
    return TEMPLATES["failure_recovery"].render(
        failure_kind=f"review_integrity ({code})",
        detail=message,
        guidance=(
            "Approvals must reference the exact reviewed state. Re-issue the "
            "decision copying request_id, head_sha and report_sha256 from the "
            "CONTEXT block of the request that presented the work you approve "
            "— which is THIS request if you are approving the state described "
            "above."
        ),
    )


def git_error_payload(decision: str, detail: str) -> str:
    return TEMPLATES["failure_recovery"].render(
        failure_kind=f"git_failure (decision={decision})",
        detail=detail,
        guidance="Nothing further was done. Decide how to proceed.",
    )


def plan_rejected_payload(code: str, message: str) -> str:
    return TEMPLATES["failure_recovery"].render(
        failure_kind=f"plan_rejected ({code})",
        detail=message,
        guidance=(
            "No task was added. Fix the task list (stable slug ids, known "
            "dependencies, no cycles) and resend the plan."
        ),
    )


def user_answer_payload(question: str, answer: str) -> str:
    return TEMPLATES["clarification"].render(question=question, answer=answer.strip())
