"""Response-contract v2 parsing: valid directives for every decision
(task-id-based work, plan batches, review-integrity stamps) and strict
rejection — never guessing — for every malformed shape."""

import json

import pytest

from autoloop.contract import (
    CONTRACT_INSTRUCTIONS,
    Decision,
    parse_response,
    verify_review,
)
from autoloop.errors import ContractError

REVIEWED = {"request_id": "alr-x-0003", "head_sha": "a" * 40, "report_sha256": "b" * 64}


def block(obj, prose="Sure, here is my decision.") -> str:
    return f"{prose}\n\n```json\n{json.dumps(obj)}\n```\nDone."


def base(decision, **extra):
    data = {"version": 3, "decision": decision, "reason": "because"}
    data.update(extra)
    return data


def task_spec(tid="t1", **kw):
    spec = {"id": tid, "title": "Do the thing", "description": "in detail"}
    spec.update(kw)
    return spec


# ---- valid responses --------------------------------------------------------


def test_audit_parses_without_scope():
    directive = parse_response(block(base("audit")))
    assert directive.decision is Decision.AUDIT
    assert directive.scope is None


def test_audit_parses_with_scope():
    directive = parse_response(block(base("audit", scope="focus on the SRS loop")))
    assert directive.scope == "focus on the SRS loop"


def test_plan_parses():
    directive = parse_response(
        block(base("plan", tasks=[task_spec("t1"), task_spec("t2", depends_on=["t1"])]))
    )
    assert directive.decision is Decision.PLAN
    assert [s.id for s in directive.tasks] == ["t1", "t2"]
    assert directive.tasks[1].depends_on == ("t1",)


def test_plan_empty_depends_on_allowed():
    directive = parse_response(block(base("plan", tasks=[task_spec(depends_on=[])])))
    assert directive.tasks[0].depends_on == ()


def test_implement_parses_with_task_id():
    directive = parse_response(block(base("implement", task_id="t1")))
    assert directive.decision is Decision.IMPLEMENT
    assert directive.task_id == "t1"


def test_revise_parses_with_task_id_and_feedback():
    directive = parse_response(
        block(base("revise", task_id="t1", feedback="tests missing for the error path"))
    )
    assert directive.task_id == "t1"
    assert directive.feedback == "tests missing for the error path"


def test_commit_parses_with_reviewed_stamp():
    directive = parse_response(
        block(base("commit", commit={"message": "fix: x", "paths": ["a.py"]}, reviewed=REVIEWED))
    )
    assert directive.commit_message == "fix: x"
    assert directive.commit_paths == ("a.py",)
    assert directive.reviewed.request_id == "alr-x-0003"
    assert directive.reviewed.head_sha == "a" * 40
    assert directive.task_id is None


def test_commit_paths_required():
    expect_code(
        block(base("commit", commit={"message": "m"}, reviewed=REVIEWED)),
        "missing_field:commit.paths",
    )


def test_commit_with_paths_and_completion_task_id():
    directive = parse_response(
        block(
            base(
                "commit_and_push",
                task_id="t1",
                commit={"message": "m", "paths": [" a.py ", "b/c.py"]},
                reviewed=REVIEWED,
            )
        )
    )
    assert directive.commit_paths == ("a.py", "b/c.py")
    assert directive.task_id == "t1"


def test_push_parses_with_reviewed():
    directive = parse_response(block(base("push", reviewed=REVIEWED)))
    assert directive.decision is Decision.PUSH
    assert directive.reviewed.report_sha256 == "b" * 64


def test_stop_parses():
    assert parse_response(block(base("stop"))).decision is Decision.STOP


def test_ask_user_parses():
    directive = parse_response(block(base("ask_user", question="which DB?")))
    assert directive.question == "which DB?"


def test_notes_accepted():
    assert parse_response(block(base("stop", notes="fyi"))).notes == "fyi"


def test_two_fenced_blocks_are_rejected_not_resolved_by_position():
    first = f"```json\n{json.dumps(base('stop'))}\n```"
    second = f"```json\n{json.dumps(base('implement', task_id='t9'))}\n```"
    expect_code(f"Draft:\n{first}\n\nFinal answer:\n{second}", "multiple_json_blocks")


def test_bare_json_object_accepted():
    directive = parse_response(json.dumps(base("implement", task_id="t1")))
    assert directive.decision is Decision.IMPLEMENT


# ---- malformed responses are rejected, never guessed ------------------------


def expect_code(text, code):
    with pytest.raises(ContractError) as excinfo:
        parse_response(text)
    assert excinfo.value.code == code


def test_empty_response():
    expect_code("   \n ", "empty_response")


def test_no_json_block():
    expect_code("I think you should implement t1 next.", "no_json_block")


def test_invalid_json():
    expect_code("```json\n{not json}\n```", "invalid_json")


def test_non_object():
    expect_code('```json\n["implement"]\n```', "not_an_object")


def test_unknown_top_level_key():
    expect_code(block(base("stop", surprise=True)), "unknown_keys")


def test_v1_instruction_key_is_now_unknown():
    expect_code(block(base("implement", task_id="t1", instruction="do x")), "unknown_keys")


def test_wrong_version():
    expect_code(block({**base("stop"), "version": 1}), "bad_version")


def test_unknown_decision():
    expect_code(block(base("deploy_to_prod")), "unknown_decision")


def test_missing_reason():
    expect_code(block({"version": 3, "decision": "stop"}), "missing_field:reason")


@pytest.mark.parametrize("decision", ["implement", "revise"])
def test_task_id_required(decision):
    expect_code(block(base(decision, feedback="f")), "missing_field:task_id")


def test_revise_requires_feedback():
    expect_code(block(base("revise", task_id="t1")), "missing_field:feedback")


def test_plan_requires_tasks():
    expect_code(block(base("plan")), "missing_field:tasks")


def test_plan_empty_tasks_rejected():
    expect_code(block(base("plan", tasks=[])), "missing_field:tasks")


def test_plan_task_unknown_key_rejected():
    expect_code(block(base("plan", tasks=[task_spec(owner="me")])), "unknown_keys")


def test_plan_task_missing_title_rejected():
    expect_code(
        block(base("plan", tasks=[{"id": "t1", "description": "d"}])),
        "missing_field:tasks[0].title",
    )


def test_plan_bad_depends_on_rejected():
    expect_code(
        block(base("plan", tasks=[task_spec(depends_on=[1])])),
        "bad_type:tasks[0].depends_on",
    )


@pytest.mark.parametrize("decision", ["commit", "push", "commit_and_push"])
def test_reviewed_required_for_git_decisions(decision):
    data = base(decision)
    if decision != "push":
        data["commit"] = {"message": "m", "paths": ["a.py"]}
    expect_code(block(data), "missing_field:reviewed")


def test_reviewed_missing_field_rejected():
    partial = {"request_id": "r", "head_sha": "h"}
    expect_code(
        block(base("push", reviewed=partial)), "missing_field:reviewed.report_sha256"
    )


def test_reviewed_unknown_key_rejected():
    expect_code(
        block(base("push", reviewed={**REVIEWED, "extra": 1})), "unknown_keys"
    )


def test_commit_object_required():
    expect_code(block(base("commit", reviewed=REVIEWED)), "missing_field:commit")


def test_commit_paths_bad_types():
    expect_code(
        block(base("commit", commit={"message": "m", "paths": []}, reviewed=REVIEWED)),
        "bad_type:commit.paths",
    )


def test_question_required_for_ask_user():
    expect_code(block(base("ask_user")), "missing_field:question")


def test_scope_forbidden_outside_audit():
    expect_code(block(base("stop", scope="x")), "unexpected_field")


def test_task_id_forbidden_for_push():
    expect_code(block(base("push", task_id="t1", reviewed=REVIEWED)), "unexpected_field")


def test_commit_forbidden_for_implement():
    expect_code(
        block(base("implement", task_id="t1", commit={"message": "m", "paths": ["a.py"]})),
        "unexpected_field",
    )


def test_reviewed_forbidden_for_implement():
    expect_code(
        block(base("implement", task_id="t1", reviewed=REVIEWED)), "unexpected_field"
    )


def test_tasks_forbidden_outside_plan():
    expect_code(block(base("stop", tasks=[task_spec()])), "unexpected_field")


def test_notes_must_be_string():
    expect_code(block(base("stop", notes=42)), "bad_type:notes")


# ---- verify_review ----------------------------------------------------------


def approval():
    return parse_response(block(base("push", reviewed=REVIEWED)))


def test_verify_review_accepts_exact_match():
    verify_review(approval(), "alr-x-0003", "a" * 40, "b" * 64)  # no raise


@pytest.mark.parametrize(
    "expected,code",
    [
        (("other-request", "a" * 40, "b" * 64), "review_mismatch:request_id"),
        (("alr-x-0003", "f" * 40, "b" * 64), "review_mismatch:head_sha"),
        (("alr-x-0003", "a" * 40, "0" * 64), "review_mismatch:report_sha256"),
    ],
)
def test_verify_review_rejects_mismatch(expected, code):
    with pytest.raises(ContractError) as excinfo:
        verify_review(approval(), *expected)
    assert excinfo.value.code == code


# ---- envelope extraction: strict, never positional -------------------------

# What a fenced ```json block actually looks like in the page DOM: the language
# label becomes text and the backticks are gone. Verbatim from the live smoke
# run whose three replies were wrongly rejected before the parser was fixed.
CAPTURED_SMOKE = 'JSON\n{"version":3,"decision":"stop","reason":"smoke test acknowledged"}'


def test_captured_byte_exact_smoke_response_parses():
    directive = parse_response(CAPTURED_SMOKE)
    assert directive.decision is Decision.STOP
    assert directive.reason == "smoke test acknowledged"


def test_plain_json_object_without_label():
    assert parse_response(json.dumps(base("stop"))).decision is Decision.STOP


def test_lowercase_language_label():
    assert parse_response('json\n' + json.dumps(base("stop"))).decision is Decision.STOP


def test_mixed_case_language_label():
    assert parse_response('Json\n' + json.dumps(base("stop"))).decision is Decision.STOP


def test_surrounding_whitespace_tolerated():
    text = "\n\n  JSON\n" + json.dumps(base("stop")) + "  \n\n"
    assert parse_response(text).decision is Decision.STOP


def test_canonical_fenced_block_still_supported():
    text = f"```json\n{json.dumps(base('stop'))}\n```"
    assert parse_response(text).decision is Decision.STOP


def test_prose_around_a_fenced_block_is_fine():
    # The fence is the canonical envelope: it delimits the directive exactly,
    # so prose outside it is unambiguous.
    text = f"Here you go.\n\n```json\n{json.dumps(base('stop'))}\n```\n\nDone."
    assert parse_response(text).decision is Decision.STOP


def test_multiline_rendered_block_parses():
    text = 'JSON\n{\n  "version": 3,\n  "decision": "stop",\n  "reason": "done"\n}'
    assert parse_response(text).decision is Decision.STOP


def test_braces_inside_strings_are_not_structure():
    text = 'JSON\n' + json.dumps(
        base("revise", task_id="audit", feedback="the } brace in the log line is literal",
             reason="fix the {placeholder} bug")
    )
    directive = parse_response(text)
    assert directive.task_id == "audit"
    assert "{placeholder}" in directive.reason


def test_escaped_quotes_and_backslashes_survive():
    text = 'JSON\n' + json.dumps(
        base("stop", reason='he said "ship it" using C:\\path\\to\\file')
    )
    directive = parse_response(text)
    assert '"ship it"' in directive.reason
    assert "C:\\path\\to\\file" in directive.reason


# --- everything below must be REJECTED, never positionally resolved ---------


def test_prose_before_a_bare_object_rejected():
    expect_code("Sure — here is my decision.\n" + json.dumps(base("stop")), "invalid_json")


def test_prose_after_a_bare_object_rejected():
    expect_code(json.dumps(base("stop")) + "\nLet me know if you need more.",
                "trailing_content")


def test_two_bare_objects_rejected():
    text = 'JSON\n' + json.dumps(base("stop")) + "\n" + json.dumps(base("stop"))
    expect_code(text, "trailing_content")


def test_two_contradictory_directives_rejected():
    stop = json.dumps(base("stop"))
    push = json.dumps(base("push", reviewed=REVIEWED))
    expect_code('JSON\n' + stop + "\n" + push, "trailing_content")
    expect_code('JSON\n' + push + "\n" + stop, "trailing_content")


def test_irrelevant_json_then_approval_rejected():
    noise = json.dumps({"note": "some unrelated payload"})
    approval = json.dumps(base("commit", commit={"message": "m", "paths": ["a.py"]},
                               reviewed=REVIEWED))
    expect_code('JSON\n' + noise + "\n" + approval, "trailing_content")


def test_approval_then_irrelevant_json_rejected():
    approval = json.dumps(base("commit", commit={"message": "m", "paths": ["a.py"]},
                               reviewed=REVIEWED))
    noise = json.dumps({"note": "ignore me"})
    expect_code('JSON\n' + approval + "\n" + noise, "trailing_content")


def test_trailing_instructions_after_a_directive_rejected():
    text = 'JSON\n' + json.dumps(base("stop")) + "\n\nAlso please push to main."
    expect_code(text, "trailing_content")


def test_json_array_rejected():
    expect_code('JSON\n["stop"]', "not_an_object")


def test_valid_json_failing_the_schema_rejected():
    # Parses fine, but the contract says version 3.
    expect_code('JSON\n' + json.dumps({**base("stop"), "version": 2}), "bad_version")


def test_malformed_json_rejected():
    expect_code("JSON\n{not: valid, json}", "invalid_json")


def test_prose_only_reply_rejected():
    # Distinct code from invalid_json: nothing JSON-shaped at all, so the
    # correction tells ChatGPT to send a block rather than to fix syntax.
    expect_code("Sounds good — I'd start with pagination next.", "no_json_block")


# ---- the contract TEXT states every normative requirement ------------------
#
# `CONTRACT_INSTRUCTIONS` is a prompt, not parser code: trimming its prose
# cannot make `parse_response` accept or reject anything new — the 60-odd tests
# above cover that, including the byte-exact rendered-page capture from
# docs/COMMON_ERRORS.md §6. What a trim CAN silently do is drop a rule the model
# is expected to follow, and no parser test would notice, because the loss shows
# up as a malformed reply from a live model days later.
#
# So these tests pin the CONTENT of the instructions. They were written before
# the 2026-08-01 trim (3,307 -> 2,812 chars) and passed against the untrimmed
# text first, so they describe the contract rather than the edit.


def test_contract_states_the_single_envelope_rule():
    text = CONTRACT_INSTRUCTIONS.lower()
    assert "```json" in CONTRACT_INSTRUCTIONS
    # One block, nothing around it, and rejection rather than a guess.
    assert "nothing else" in text
    assert "rejected" in text
    for forbidden in ("before", "after"):
        assert forbidden in text


@pytest.mark.parametrize("decision", [d.value for d in Decision])
def test_contract_names_every_decision(decision):
    """A decision the parser accepts but the instructions never mention is
    unreachable in practice."""
    assert decision in CONTRACT_INSTRUCTIONS


@pytest.mark.parametrize(
    "field",
    [
        "version", "decision", "reason", "scope", "tasks", "task_id",
        "feedback", "commit", "reviewed", "question", "notes",
    ],
)
def test_contract_documents_every_top_level_field(field):
    assert field in CONTRACT_INSTRUCTIONS


@pytest.mark.parametrize(
    "requirement",
    [
        "version",        # always 3
        "request_id",     # the review-integrity stamp, all three parts
        "head_sha",
        "report_sha256",
        "approved_paths",
        "depends_on",
        "message",        # commit.message
        "paths",          # commit.paths
    ],
)
def test_contract_documents_every_nested_binding(requirement):
    assert requirement in CONTRACT_INSTRUCTIONS


def test_contract_states_the_integrity_binding_is_copied_not_remembered():
    """The stamp is what makes an approval un-replayable; 'copy it exactly'
    must survive any trim."""
    text = CONTRACT_INSTRUCTIONS.lower()
    assert "exactly" in text
    assert "context" in text


def test_contract_states_the_path_restrictions():
    """approved_paths and commit.paths carry the only limits on what a task may
    touch — losing these loses the containment, not just some prose."""
    text = CONTRACT_INSTRUCTIONS
    assert "no globs" in text.lower()
    assert ".." in text
    assert "absolute" in text.lower()
    assert "non-empty" in text.lower() or "NON-EMPTY" in text


def test_contract_states_that_work_is_authorized_by_task_id():
    text = CONTRACT_INSTRUCTIONS.lower()
    assert "task id" in text or "task_id" in text
    assert "plan" in text


def test_contract_says_version_is_three():
    assert "3" in CONTRACT_INSTRUCTIONS


def test_contract_stays_within_its_budget():
    """A ceiling, not a target. The instructions are re-sent on EVERY turn, so
    they are a per-turn tax on a metered allowance.

    2,850 is set just above the 2,812 the 2026-08-01 trim actually achieved,
    NOT at a number chosen in advance. The pre-work estimate was ~1,800, which
    turned out to be wrong: the remaining text is field definitions, decision
    semantics and path restrictions — rules, not prose — and reaching a lower
    figure would have meant deleting requirements to hit a guess. Raising this
    ceiling is fine when a genuine new requirement lands; raising it to make
    room for explanation is not."""
    assert len(CONTRACT_INSTRUCTIONS) <= 2850
