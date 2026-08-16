"""Response-contract v2 parsing: valid directives for every decision
(task-id-based work, plan batches, review-integrity stamps) and strict
rejection — never guessing — for every malformed shape."""

import json

import pytest

from autoloop.contract import (
    ACTIVE_DECISIONS,
    AUDIT_VS_READY_PREFERENCE,
    CONTRACT_INSTRUCTIONS,
    RETIRED_DECISIONS,
    NEXT_WORK_PREFERENCE,
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


def test_legacy_ask_user_with_a_question_still_parses():
    """`ask_user` is retired from the instructions, not from the parser: a
    conversation that saw the old contract can still answer it, and that reply
    must park the loop rather than burn the parse-retry budget."""
    directive = parse_response(block(base("ask_user", question="which DB?")))
    assert directive.decision is Decision.ASK_USER
    assert directive.question == "which DB?"


def test_legacy_ask_user_without_a_question_parses():
    """The contract no longer asks for `question`, so a legacy reply may omit
    it — the orchestrator parks with a placeholder instead."""
    directive = parse_response(block(base("ask_user")))
    assert directive.decision is Decision.ASK_USER
    assert directive.question is None


@pytest.mark.parametrize("decision", sorted(d.value for d in RETIRED_DECISIONS))
def test_every_retired_decision_still_parses(decision):
    """Backward parsing is what retirement means here, and it is load-bearing
    rather than courtesy: a live conversation that already read the old
    instructions can answer a retired decision at any time, and the parser
    accepting it is what routes that reply to the policy layer's denial —
    which says WHY the decision is gone — instead of a `unknown_decision`
    contract violation that spends the parse-retry budget saying nothing.

    Parametrized over the set so a later retirement cannot be implemented as
    "delete the enum member", which would break exactly those in-flight
    conversations the retirement is supposed to absorb."""
    assert parse_response(block(base(decision))).decision.value == decision


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


def test_blank_question_on_a_legacy_ask_user_rejected():
    """Optional, but never blank: a park whose question is an empty string is
    worse than one that says it has none."""
    expect_code(block(base("ask_user", question="  ")), "missing_field:question")


def test_question_forbidden_outside_ask_user():
    expect_code(block(base("stop", question="which DB?")), "unexpected_field")


#: One COMPLETE, otherwise-valid payload per ACTIVE decision: every field that
#: decision requires and nothing else, so the only thing wrong with it below is
#: the added `question`.
#:
#: Completeness is the whole point rather than tidiness. `question` is the LAST
#: field `parse_response` checks — after scope, tasks, task_id, feedback,
#: commit and reviewed — so a payload missing a required field fails earlier
#: with that field's own code, and a test written against it would be pinning
#: `missing_field:task_id` while claiming to pin the question rule.
_COMPLETE_PAYLOADS = {
    "audit": {},
    "plan": {"tasks": [task_spec()]},
    "implement": {"task_id": "t1"},
    "revise": {"task_id": "t1", "feedback": "tests missing"},
    "commit": {"commit": {"message": "m", "paths": ["a.py"]}, "reviewed": REVIEWED},
    "push": {"reviewed": REVIEWED},
    "commit_and_push": {
        "commit": {"message": "m", "paths": ["a.py"]},
        "reviewed": REVIEWED,
    },
    "stop": {},
}


def test_complete_payloads_cover_every_active_decision():
    """Keeps the two tests below honest as the enum grows: a new active
    decision with no payload here would otherwise be silently unexercised by
    both."""
    assert set(_COMPLETE_PAYLOADS) == {d.value for d in ACTIVE_DECISIONS}


@pytest.mark.parametrize("decision", sorted(_COMPLETE_PAYLOADS))
def test_question_forbidden_for_every_active_decision(decision):
    """`question` survives in `_TOP_LEVEL_KEYS` only so a legacy `ask_user`
    reaches its retirement denial instead of dying at `unknown_keys` — see
    `test_legacy_ask_user_with_a_question_still_parses`. That tolerance must
    not leak into the decisions the contract still offers: a key kept for one
    retired shape, accepted on an active one, is a second undocumented way to
    address a human that policy would never see.

    Generalizes the `stop`-only case above to the whole active set, so the
    property is pinned for each decision rather than for the one that happens
    to need no other fields."""
    payload = base(decision, **_COMPLETE_PAYLOADS[decision], question="which DB?")
    expect_code(block(payload), "unexpected_field")


@pytest.mark.parametrize("decision", sorted(_COMPLETE_PAYLOADS))
def test_every_complete_payload_parses_without_question(decision):
    """The positive control for the test above: each payload is valid on its
    own, so `unexpected_field` there is attributable to the added `question`
    and to nothing else in the envelope."""
    directive = parse_response(block(base(decision, **_COMPLETE_PAYLOADS[decision])))
    assert directive.decision.value == decision
    assert directive.question is None


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


@pytest.mark.parametrize("decision", sorted(d.value for d in ACTIVE_DECISIONS))
def test_contract_names_every_active_decision(decision):
    """An active decision the instructions never mention is unreachable in
    practice."""
    assert decision in CONTRACT_INSTRUCTIONS


@pytest.mark.parametrize("decision", sorted(d.value for d in RETIRED_DECISIONS))
def test_contract_never_offers_a_retired_decision(decision):
    """Retirement is exactly this: still parsed, never advertised. A retired
    value left in the guidance keeps being chosen."""
    assert decision not in CONTRACT_INSTRUCTIONS


def test_active_and_retired_partition_the_enum():
    """Derived by subtraction, so a decision can be in neither set only if the
    derivation is broken."""
    assert ACTIVE_DECISIONS | RETIRED_DECISIONS == set(Decision)
    assert not ACTIVE_DECISIONS & RETIRED_DECISIONS
    assert Decision.ASK_USER in RETIRED_DECISIONS


def test_unknown_decision_correction_lists_active_decisions_only():
    """The correction is what the model picks from next — naming a retired
    decision there invites it back into use."""
    with pytest.raises(ContractError) as excinfo:
        parse_response(block(base("deploy_to_prod")))
    message = str(excinfo.value)
    for decision in ACTIVE_DECISIONS:
        assert decision.value in message
    assert "ask_user" not in message


def test_contract_does_not_document_the_legacy_question_field():
    """The field-level half of the retirement, which the decision-level check
    above does not cover: a re-added `question (optional) ...` line names no
    retired DECISION, so `test_contract_never_offers_a_retired_decision`
    passes right through it.

    `question` is accepted by the parser for exactly one shape — the retired
    `ask_user` — and rejected as `unexpected_field` for every decision the
    instructions still offer (pinned by
    `test_question_forbidden_for_every_active_decision`). Documenting it would
    therefore advertise a field that cannot be used with any advertised
    decision: an invitation to a guaranteed contract violation, and a
    back-door restatement of the retired shape.

    Paired with a positive anchor, so it cannot pass vacuously against an
    empty or truncated text — `notes` is the optional top-level field that IS
    documented, and the one a trim would plausibly take `question` down with.
    """
    assert "question" not in CONTRACT_INSTRUCTIONS.lower()
    assert "notes" in CONTRACT_INSTRUCTIONS


@pytest.mark.parametrize(
    "field",
    [
        "version", "decision", "reason", "scope", "tasks", "task_id",
        "feedback", "commit", "reviewed", "notes",
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


# ---- the finish-before-start scheduling preference --------------------------
#
# The first of the two advisory paragraphs in the instructions (the second,
# `AUDIT_VS_READY_PREFERENCE`, is pinned further down). It is prose the model
# reads, not something `parse_response` can enforce, so the tests below pin its
# CONTENT the same way the format tests above do — and pin it to the CONTEXT
# numbers it depends on, because a preference the reviewer cannot evaluate is
# not a preference, it is decoration.


def test_preference_states_finish_before_start():
    assert "finish before you start" in NEXT_WORK_PREFERENCE
    # ...and names which decisions that ranks against which.
    assert "prefer `revise` or an approval on" in NEXT_WORK_PREFERENCE
    assert "`implement` on a fresh task" in NEXT_WORK_PREFERENCE


@pytest.mark.parametrize(
    "condition",
    [
        "when nothing is in flight",
        "blocked on something external",
        "when the operator asks",
    ],
)
def test_preference_keeps_all_three_start_anyway_conditions(condition):
    """Prefer, never forbid. Each of the three is a real case where starting
    fresh work is right; the external-blocker one especially — a task stuck on
    an outside condition must not stall everything queued behind it."""
    assert condition in NEXT_WORK_PREFERENCE


def test_preference_is_advisory_not_a_refusal():
    """It must read as a ranking the reviewer applies, not as a rule something
    enforces — a refusal would park the loop rather than redirect it."""
    assert "a preference, not a parser rule" in NEXT_WORK_PREFERENCE


def test_preference_cites_the_context_counts_that_make_it_checkable():
    """The mutation this guards: drop the counts from the CONTEXT block (or
    rename the label) and the rule points at numbers that are not there.

    Written as a literal on purpose. The other half of the pin — that this is
    the same label `render_context` actually emits — lives in `test_context.py`
    (`test_the_label_the_contract_points_at_is_the_label_that_is_rendered`),
    which owns `IN_FLIGHT_LABEL`. Importing it here would point a test-only
    dependency back down the import chain (`context` already reaches `contract`
    via `git_gateway` → `policy`) to prove something once more."""
    assert "`in_flight`" in NEXT_WORK_PREFERENCE
    assert "in progress" in NEXT_WORK_PREFERENCE
    assert "unpublished candidate" in NEXT_WORK_PREFERENCE


def test_preference_does_not_restate_the_audit_rule():
    """Whether a fresh audit or ready roadmap work comes first is a separate
    rule with its own home — `AUDIT_VS_READY_PREFERENCE`, pinned below. This
    clause orders implement/revise/approve among themselves; duplicating the
    audit rule here would give one preference two texts to drift between.
    (`audit` itself stays documented above — see
    test_contract_names_every_decision.)"""
    assert "audit" not in NEXT_WORK_PREFERENCE.lower()


def test_preference_is_actually_shipped_in_the_instructions():
    assert NEXT_WORK_PREFERENCE in CONTRACT_INSTRUCTIONS


# ---- the ready-work-before-audit scheduling preference ----------------------
#
# The second advisory clause, and the reason it exists: on 2026-08-05 the loop
# was running a synthetic audit unit while 15 tasks sat READY, six of them
# priority 1. An audit ADDS findings, so choosing one over ready work drains
# nothing and grows the backlog. Same test shape as the clause above — the
# instructions are prose a model reads, so what a trim can silently delete is a
# rule, and only a content test notices.


def test_audit_preference_states_ready_work_comes_first():
    """The rule itself, stated as a rule: while the roadmap has ready tasks,
    work one of them instead of ordering a fresh audit."""
    assert "While any task is ready" in AUDIT_VS_READY_PREFERENCE
    # ...and names which decisions that ranks against which.
    assert "prefer `implement` on one" in AUDIT_VS_READY_PREFERENCE
    assert "over `audit`" in AUDIT_VS_READY_PREFERENCE
    # ...with the reason, so it reads as a rule rather than an arbitrary order.
    assert "an audit adds findings" in AUDIT_VS_READY_PREFERENCE


@pytest.mark.parametrize(
    "condition",
    [
        "when no task is ready",
        "blocked on something outside the roadmap",
        "when the operator asks",
    ],
)
def test_audit_preference_keeps_all_three_audit_anyway_conditions(condition):
    """Prefer, never forbid. An empty or fully blocked roadmap is exactly when
    an audit is the right move, and continuous mode depends on that to find new
    work at all — a rule that made `audit` unreachable would stall the loop the
    moment the queue drains.

    The middle condition is about a blocker the task graph does not model — an
    upstream release, an operator decision, a service that is down. It has to
    be phrased that way round: `TaskRegistry.state_of` only calls a task READY
    once its declared `depends_on` are complete, so "ready but waiting on an
    unmet dependency" describes no task that can exist. Each fragment is pinned
    inside a single line of the shipped text rather than across its wrap, so
    re-flowing the clause cannot break these assertions for a reason that has
    nothing to do with the rule."""
    assert condition in AUDIT_VS_READY_PREFERENCE


def test_audit_preference_recommends_implement_and_never_revise_for_ready_work():
    """The regression this pins. `implement` is the directive the protocol
    defines for a READY task; `revise` sends an already-started task back to
    its executor and is phase-gated besides. Recommending `revise` for the
    tasks the READY count describes would point the reviewer at a directive
    that is invalid for exactly those tasks. Ranking `revise` and the approvals
    against fresh work belongs to `NEXT_WORK_PREFERENCE`, not here.

    The positive assertion comes first on purpose: an absence check alone would
    also pass against an empty clause."""
    assert "prefer `implement` on one" in AUDIT_VS_READY_PREFERENCE
    assert "revise" not in AUDIT_VS_READY_PREFERENCE.lower()


def test_audit_preference_never_calls_a_ready_task_dependency_blocked():
    """The other half of the same regression: a READY task cannot have an unmet
    declared dependency — `TaskRegistry.state_of` returns BLOCKED until every
    `depends_on` is completed — so the audit-anyway condition must name the
    unmodelled, external kind of blocker instead. Paired with a positive anchor
    for the same reason as above."""
    assert "blocked on something outside the roadmap" in AUDIT_VS_READY_PREFERENCE
    assert "dependency" not in AUDIT_VS_READY_PREFERENCE.lower()
    assert "depends_on" not in AUDIT_VS_READY_PREFERENCE.lower()


def test_audit_preference_is_advisory_not_a_refusal():
    """It must read as a ranking the reviewer applies. Encoding it as a policy
    denial would be the wrong layer twice over: policy authorizes actions, and
    a refused audit directive would park the loop rather than redirect it."""
    assert "a preference, not a parser rule" in AUDIT_VS_READY_PREFERENCE


def test_audit_preference_cites_the_context_counts_that_make_it_checkable():
    """The mutation this guards: drop the ready/priority-1 counts from the
    roadmap summary (or rename the label) and the rule points at numbers that
    are not there. The other half of the pin — that `render_context` actually
    emits this label and those counts — lives in `test_context.py`, which owns
    `ROADMAP_LABEL`."""
    assert "`roadmap`" in AUDIT_VS_READY_PREFERENCE
    assert "how many tasks are ready" in AUDIT_VS_READY_PREFERENCE
    assert "how many of those\nare priority 1" in AUDIT_VS_READY_PREFERENCE


def test_audit_preference_does_not_restate_the_in_flight_rule():
    """The mirror of `test_preference_does_not_restate_the_audit_rule`: one
    preference, one text. This clause ranks ready work against `audit` and says
    nothing about what is already in flight."""
    assert "in_flight" not in AUDIT_VS_READY_PREFERENCE
    assert "finish before you start" not in AUDIT_VS_READY_PREFERENCE


def test_audit_preference_is_actually_shipped_in_the_instructions():
    assert AUDIT_VS_READY_PREFERENCE in CONTRACT_INSTRUCTIONS


def test_contract_stays_within_its_budget():
    """A ceiling, not a target. The instructions are re-sent on EVERY turn, so
    they are a per-turn tax on a metered allowance.

    2,850 was set just above the 2,812 the 2026-08-01 trim achieved. Adding
    the finish-before-start preference (2026-08-14) grew the text by a
    measured 402 characters — the 400-character clause plus the two-character
    blank-line join — for a total of 3,214.

    Adding the ready-work-before-audit preference (2026-08-15) grew it by a
    further measured 440 — the 438-character clause plus its own two-character
    join. Both measurements were made by hand, summing line lengths, because
    the executors for these changes had no shell. (The clause was 449 when
    first written; the same-day revision that dropped `revise` from it and
    renamed its second escape hatch took 11 characters back out.)

    3,700 is derived from the previous assertion's GUARANTEED bound (3,240 —
    the number the suite actually enforced) plus 451, the growth measured when
    this clause was first written and 11 more than it now costs, not from the
    recorded 3,214, which is a hand count nothing re-verified. Trusting the
    record would put the ceiling at 3,690 and leave the suite one character
    from failing if the old count was itself off by its own margin, with no
    shell available to diagnose it. The extra ~9 buys the whole risk out; it is
    not room to write in. Raising this ceiling is fine when a genuine new
    requirement lands; raising it to make room for explanation is not."""
    assert len(CONTRACT_INSTRUCTIONS) <= 3700


def test_preference_clause_has_its_own_tighter_budget():
    """The clause is the part likely to attract elaboration, so it carries a
    separate ceiling: measured 400, capped at 420. The whole rule fits in six
    lines — anything materially longer is explanation, and explanation belongs
    in the source comment next to it, which costs nothing per turn."""
    assert len(NEXT_WORK_PREFERENCE) <= 420


def test_audit_preference_clause_has_its_own_tighter_budget():
    """Same ceiling-per-clause treatment as the one above, and for the same
    reason: measured 438, capped at 470, six lines. The rationale for this rule
    is longer than the rule — it lives in the source comment beside the
    constant, which costs nothing per turn, not in the prompt, which is re-sent
    on every one."""
    assert len(AUDIT_VS_READY_PREFERENCE) <= 470
