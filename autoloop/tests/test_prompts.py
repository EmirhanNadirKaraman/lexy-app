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
    review_mismatch_payload,
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
