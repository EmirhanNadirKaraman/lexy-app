"""Prompt templates: the required template set exists, rendering is strict
(missing/unknown fields fail loudly), payload helpers produce the expected
sections."""

import pytest

from autoloop.contract import CONTRACT_INSTRUCTIONS, RETIRED_DECISIONS
from autoloop.errors import TemplateError
from autoloop.prompts import (
    TEMPLATES,
    PromptTemplate,
    build_prompt,
    git_error_payload,
    kickoff_payload,
    parse_error_payload,
    plan_rejected_payload,
    policy_denied_payload,
    represented_candidate_note,
    review_mismatch_payload,
    same_review_note,
    user_answer_payload,
)

REQUIRED_TEMPLATES = {
    "audit",
    "implementation_review",
    "commit_approval",
    "push_approval",
    "failure_recovery",
    "clarification",
}


def test_required_templates_exist():
    assert REQUIRED_TEMPLATES <= set(TEMPLATES)


@pytest.mark.parametrize("name", sorted(TEMPLATES))
def test_every_template_renders_with_its_fields(name):
    template = TEMPLATES[name]
    rendered = template.render(**{f: f.upper() for f in template.fields})
    for f in template.fields:
        assert f.upper() in rendered


def test_render_missing_field_raises():
    with pytest.raises(TemplateError) as excinfo:
        TEMPLATES["failure_recovery"].render(failure_kind="x", detail="y")
    assert "guidance" in str(excinfo.value)


def test_render_unknown_field_raises():
    with pytest.raises(TemplateError) as excinfo:
        TEMPLATES["clarification"].render(question="q", answer="a", extra="nope")
    assert "extra" in str(excinfo.value)


def test_positional_fields_rejected_at_definition():
    with pytest.raises(TemplateError):
        PromptTemplate(name="bad", body="hello {}")


def test_build_prompt_composition():
    prompt = build_prompt("alr-x-0001", 3, "CONTEXT\nrequest_id: alr-x-0001", "PAYLOAD")
    assert "[autoloop request alr-x-0001 | iteration 3]" in prompt
    assert "CONTEXT" in prompt
    assert "PAYLOAD" in prompt
    assert CONTRACT_INSTRUCTIONS in prompt


def test_kickoff_payload_wraps_report():
    payload = kickoff_payload("THE REPORT")
    assert "THE REPORT" in payload
    assert "task id" in payload


def test_parse_error_payload_carries_code():
    payload = parse_error_payload("no_json_block", "nothing found")
    assert "no_json_block" in payload
    assert "nothing found" in payload
    # no note asked for, nothing added — the shape every non-bound correction
    # still has
    assert "SAME review" not in payload


@pytest.mark.parametrize(
    "build",
    [
        lambda note: parse_error_payload("no_json_block", "nothing found", note),
        lambda note: policy_denied_payload("push", "protected branch", note),
        lambda note: review_mismatch_payload("review_mismatch:head_sha", "drifted", note),
    ],
    ids=["parse_error", "policy_denied", "review_mismatch"],
)
def test_the_three_carrying_corrections_can_name_the_review_they_continue(build):
    """The three corrective re-prompts that inherit a postcommit binding
    (bind-01) each render the note that tells the reviewer the packet has not
    changed and where to copy the stamp from."""
    payload = build(same_review_note("t1", "a" * 40))
    assert "SAME review" in payload
    assert "t1" in payload
    assert "a" * 12 in payload
    # the FULL candidate sha is deliberately absent: a correction must not
    # carry the identifiers that would let it bind itself as if it were a
    # review packet (orchestrator._current_pending_postcommit)
    assert "a" * 40 not in payload
    assert "THIS request" in payload


def test_the_re_presentation_note_leads_the_packet_and_denies_new_work():
    """bind-01. `_handle_unbound_push` re-presents an already-reviewed
    candidate, and the packet body opens with "The executor committed task ..."
    — which reads as a fresh round unless the note contradicts it first."""
    payload = (
        TEMPLATES["postcommit_review"]
        .render(
            task_id="t1",
            task_title="Title",
            packet="THE PACKET",
            note=represented_candidate_note("push refused — bound to no candidate"),
        )
        .strip()
    )
    assert payload.startswith("YOUR PREVIOUS `push` WAS REFUSED")
    assert "push refused — bound to no candidate" in payload   # the verdict verbatim
    assert "No new work was produced" in payload
    assert "THE PACKET" in payload


def test_an_ordinary_postcommit_packet_is_unaffected_by_the_note_field():
    """The negative control for the field above: empty note, stripped render,
    and the payload is exactly what it was before the field existed."""
    payload = (
        TEMPLATES["postcommit_review"]
        .render(task_id="t1", task_title="Title", packet="THE PACKET", note="")
        .strip()
    )
    assert payload.startswith("The executor committed task t1")
    assert "REFUSED" not in payload


def test_policy_denied_payload():
    payload = policy_denied_payload("push", "protected branch")
    assert "policy_denied" in payload
    assert "protected branch" in payload


def test_review_mismatch_payload_instructs_restamp():
    payload = review_mismatch_payload("review_mismatch:head_sha", "head drifted")
    assert "review_integrity" in payload
    assert "head drifted" in payload
    assert "report_sha256" in payload


def test_git_error_payload():
    payload = git_error_payload("commit", "remote rejected")
    assert "git_failure" in payload
    assert "remote rejected" in payload


def test_plan_rejected_payload():
    payload = plan_rejected_payload("dependency_cycle", "a -> b -> a")
    assert "plan_rejected" in payload
    assert "a -> b -> a" in payload


def test_user_answer_payload():
    payload = user_answer_payload("Which DB?", "Postgres")
    assert "Which DB?" in payload
    assert "Postgres" in payload


@pytest.mark.parametrize("decision", sorted(d.value for d in RETIRED_DECISIONS))
def test_no_template_offers_a_retired_decision(decision):
    """`CONTRACT_INSTRUCTIONS` is not the only reviewer-visible text: every
    payload template ships in the same prompt, and several name decisions
    outright ("reply `commit`...", "reply `revise` with feedback"). A retired
    decision surviving in one of those is the same failure as leaving it in
    the instructions — the reviewer is invited to choose the one directive
    policy refuses unconditionally.

    Template BODIES only, deliberately: `policy_denied_payload` renders a
    verdict reason, and the retirement's own denial names the retired
    decision on purpose."""
    offenders = [name for name, t in TEMPLATES.items() if decision in t.body]
    assert not offenders, f"'{decision}' is retired but still offered by: {sorted(offenders)}"
