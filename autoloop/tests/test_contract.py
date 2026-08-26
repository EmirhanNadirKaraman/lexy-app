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
    Decomposition,
    _DECOMPOSITION_KEYS,
    parse_response,
    verify_review,
)
from autoloop.errors import ContractError

REVIEWED = {"request_id": "alr-x-0003", "head_sha": "a" * 40, "report_sha256": "b" * 64}

#: A well-formed decomposition, as a reviewer sends it on `implement`.
DECOMP = {
    "approach": "add the field, gate it in policy, render it into the prompt",
    "files": ["autoloop/contract.py", "autoloop/policy.py"],
    "steps": ["parse and store the field", "refuse an implement without one"],
}


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


# ---- the decomposition that rides on the directive --------------------------
#
# Every task is decomposed and the decomposition approved before any code is
# written (operator decision, 2026-08-17). It rides on the `implement` directive
# the loop ALREADY exchanges, so it costs no extra round; the parser's job is
# only shape, and whether a directive needed one is `policy`'s call — pinned in
# `test_policy.py`, not here.


def test_implement_parses_with_a_decomposition():
    directive = parse_response(
        block(base("implement", task_id="t1", decomposition=DECOMP))
    )
    assert directive.decomposition.approach.startswith("add the field")
    assert directive.decomposition.files == (
        "autoloop/contract.py",
        "autoloop/policy.py",
    )
    assert len(directive.decomposition.steps) == 2


def test_a_one_step_decomposition_parses():
    """"This is one step" must be an acceptable answer at every layer. Most
    work that lands is a single reviewable commit, and a parser that demanded
    two steps would force a split the reviewer did not want."""
    one = {**DECOMP, "steps": ["one commit: the field, its gate and its tests"]}
    directive = parse_response(block(base("implement", task_id="t1", decomposition=one)))
    assert directive.decomposition.steps == (
        "one commit: the field, its gate and its tests",
    )


def test_implement_without_a_decomposition_still_parses():
    """Optional at THIS layer, on purpose (see `contract.Decomposition`):
    requiring it here would be a breaking wire change, and it would answer a
    missing plan with `missing_field:decomposition` out of the small
    parse-retry budget instead of the policy denial that states the rule."""
    directive = parse_response(block(base("implement", task_id="t1")))
    assert directive.decomposition is None


def test_revise_may_carry_a_reshaped_decomposition():
    directive = parse_response(
        block(base("revise", task_id="t1", feedback="split step 2", decomposition=DECOMP))
    )
    assert directive.decomposition is not None


def test_a_plan_for_the_audit_pseudo_task_is_refused_rather_than_dropped():
    """The audit is not a roadmap task — `_resolve_audit_task` mints a
    synthetic `Task` the registry never sees — so nothing would store or apply
    a decomposition sent with `revise task_id="audit"`. Accepting and dropping
    it would be a field that reads as configured while behaving as if it were
    not; `scope` on `audit` is the way to narrow that work."""
    expect_code(
        block(base("revise", task_id="audit", feedback="dig deeper", decomposition=DECOMP)),
        "unexpected_field",
    )
    # ...and the same directive without one is still perfectly valid.
    assert parse_response(
        block(base("revise", task_id="audit", feedback="dig deeper"))
    ).decomposition is None


@pytest.mark.parametrize(
    "bad,code",
    [
        ("not an object", "bad_type:decomposition"),
        ({**DECOMP, "steps": []}, "bad_type:decomposition.steps"),
        ({**DECOMP, "files": []}, "bad_type:decomposition.files"),
        ({**DECOMP, "files": [1]}, "bad_type:decomposition.files"),
        ({**DECOMP, "approach": "  "}, "missing_field:decomposition.approach"),
        ({"files": ["a.py"], "steps": ["s"]}, "missing_field:decomposition.approach"),
        ({**DECOMP, "owner": "me"}, "unknown_keys"),
    ],
)
def test_a_malformed_decomposition_is_rejected(bad, code):
    """Present but empty is not a smaller plan — it is a plan that answers none
    of the question. Each part is required once the key is sent at all."""
    expect_code(block(base("implement", task_id="t1", decomposition=bad)), code)


def test_the_documented_key_set_is_the_accepted_key_set():
    """The schema line must name the keys the parser actually takes.

    It documented `{approach, files, ordered steps}` while the accepted key was
    literally `steps`, which is the worst shape a contract error can have: a
    reviewer that copies the documentation sends `ordered steps`, draws
    `unknown_keys`, and spends the small parse-retry budget on a correction the
    instructions caused — on a field that is now MANDATORY, so the cost lands on
    every task. Asserted against `_DECOMPOSITION_KEYS` rather than as a literal
    substring, so a future reword cannot pass this vacuously."""
    documented = "{" + ", ".join(sorted(_DECOMPOSITION_KEYS)) + "}"
    assert documented == "{approach, files, steps}"
    # Whitespace-normalised: the schema line wraps, and where it wraps is
    # formatting rather than contract.
    flat = " ".join(CONTRACT_INSTRUCTIONS.split())
    assert documented in flat
    # ...and the ordering rule survives as prose ABOUT the steps, which is what
    # it always was — the requirement is that they are worked in order, not that
    # the key has a longer name.
    assert f"{documented}; steps are worked in order" in flat


def test_the_documented_key_is_the_key_that_parses():
    """The other half of the pin: the documented spelling parses, and the
    spelling the old text implied does not."""
    assert parse_response(
        block(base("implement", task_id="t1", decomposition=DECOMP))
    ).decomposition.steps == tuple(DECOMP["steps"])
    expect_code(
        block(
            base(
                "implement",
                task_id="t1",
                decomposition={
                    "approach": DECOMP["approach"],
                    "files": DECOMP["files"],
                    "ordered steps": DECOMP["steps"],
                },
            )
        ),
        "unknown_keys",
    )


def test_render_is_the_one_text_the_task_and_the_agent_both_read():
    """`Decomposition.render` is what `Task.decomposition` stores and what the
    implementing agent is shown, so the reviewer's plan and the agent's
    instructions cannot drift. A one-step plan reads back as one step rather
    than as a list of one."""
    rendered = Decomposition(
        approach="do it in one commit", files=("a.py",), steps=("the whole thing",)
    ).render()
    assert "do it in one commit" in rendered
    assert "a.py" in rendered
    assert "This is one step:" in rendered

    many = Decomposition(
        approach="two commits", files=("a.py",), steps=("first", "second")
    ).render()
    assert "Steps, in order:" in many
    assert "1. first" in many and "2. second" in many


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
    "recut": {"task_id": "t1"},
    # ONE successor parses on purpose. `split` requires at least
    # `orchestrator.MIN_CEILING_SPLIT_TASKS`, and that count is authorization
    # rather than shape — the same layering `approved_paths` and
    # `decomposition` already use, so a one-successor split draws a bounded
    # denial that explains itself instead of spending the parse-retry budget.
    "split": {"task_id": "t1", "tasks": [task_spec()]},
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


@pytest.mark.parametrize(
    "decision", sorted(set(_COMPLETE_PAYLOADS) - {"implement", "revise"})
)
def test_decomposition_forbidden_outside_the_task_decisions(decision):
    """A plan attached to `stop`, `audit`, `plan` or a git approval is a plan
    nothing could ever apply, and a second undocumented place to put one. Same
    complete-payload treatment as the `question` test above, and for the same
    reason: `decomposition` is checked after task_id/feedback, so an incomplete
    payload would fail earlier under another field's code."""
    payload = base(decision, **_COMPLETE_PAYLOADS[decision], decomposition=DECOMP)
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


@pytest.mark.parametrize(
    "decision", sorted(set(_COMPLETE_PAYLOADS) - {"plan", "split"})
)
def test_tasks_forbidden_outside_the_decisions_that_carry_them(decision):
    """`tasks` is accepted by exactly two decisions (`contract.
    CARRIES_TASK_SPECS`): `plan` ADDS them to the roadmap, `split` proposes them
    as the successors a named task is retired into. Anywhere else it is a batch
    of task definitions nothing would ever apply — and, since split-03, a
    plausible-looking way to attach successors to a decision that cannot retire
    anything. Generalizes the `stop`-only case this replaces."""
    payload = base(decision, **_COMPLETE_PAYLOADS[decision], tasks=[task_spec()])
    expect_code(block(payload), "unexpected_field")


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
        "feedback", "decomposition", "commit", "reviewed", "notes",
    ],
)
def test_contract_documents_every_top_level_field(field):
    assert field in CONTRACT_INSTRUCTIONS


# ---- the `notes` length / line-break rule ----------------------------------
#
# Measured 2026-08-20/21: `parse_error` fired 25 times in three weeks and EIGHT
# of them landed in one thirty-hour window. The recent ones were all the same
# defect — `invalid_json: Invalid control character at: line 6 column ~2073` —
# a literal newline inside the long `notes` value. The reviewer's CONTENT was
# right every time; only its encoding was not. Two parked the loop
# `parse_budget_exhausted` (loop_fatal), one of them for six unattended hours.
#
# `notes`' entire specification was "anything else worth recording": no length,
# no format, no escaping. These tests pin the rule that replaced it. It is
# GUIDANCE, in the same category as the two preference clauses below —
# `parse_response` is untouched and enforces neither half, which
# `test_the_notes_rules_are_guidance_the_parser_does_not_enforce` is what
# proves rather than asserts.


def _flat(text):
    """Whitespace-normalised, exactly like
    `test_the_documented_key_set_is_the_accepted_key_set` above: the rule spans
    two lines and where it wraps is formatting rather than contract."""
    return " ".join(text.split())


def test_contract_bounds_the_length_of_notes():
    """The field that broke the loop now carries a bound the reviewer can apply
    without reference to anything else.

    Pinned as the whole clause rather than as the digits. `"200" in text` would
    pass against any unrelated number that ever appeared in the schema — and
    `notes` is the field a trim would plausibly shorten back to a bare
    `(optional)`, which is the state this rule exists to leave behind."""
    assert "notes (optional) at most 200 characters, on ONE line." in _flat(
        CONTRACT_INSTRUCTIONS
    )


def test_contract_forbids_a_literal_line_break_inside_a_string_value():
    r"""The defect itself, stated as an instruction rather than as a lesson on
    JSON escaping, and scoped to every string value rather than to `notes`.

    The escape MUST be asserted as a raw backslash-n. `"\n" in
    CONTRACT_INSTRUCTIONS` passes vacuously — the instructions are full of real
    newlines — so that assertion would certify the exact bug it looks like it
    is catching: `_RESPONSE_FORMAT` is a NON-raw triple-quoted string, so a
    source that wrote `\n` there would ship a real line break inside the very
    sentence forbidding them, and the model would read a wrapped line."""
    flat = _flat(CONTRACT_INSTRUCTIONS)
    assert "NEVER put a literal line break inside a JSON string value" in flat
    assert r"write \n." in flat
    # ...and what happens otherwise, so it reads as a rule and not a taste.
    assert "invalid JSON, and the reply is REJECTED" in flat


def test_the_rule_names_the_escape_so_multiline_values_stay_expressible():
    r"""Why it is `write \n` and NOT "keep every string on one line".

    `commit.message` is documented two lines above as the FULL commit message,
    and this repository's commit messages have bodies. A one-line-only rule
    would have fixed the parse errors by quietly costing every commit body —
    a worse trade, and an invisible one. The escape is legal JSON, so the
    permissive reading survives; this test fails if either half is dropped."""
    assert r"\n" in CONTRACT_INSTRUCTIONS
    assert "the full commit message" in CONTRACT_INSTRUCTIONS


def test_the_notes_rule_advertises_no_retired_decision():
    """The rule is new text in a prompt that must offer ACTIVE decisions only.
    Covered for the whole document by
    `test_contract_never_offers_a_retired_decision`; asserted here too because
    this clause sits in the same paragraph as the key list and is the text most
    likely to be reworded next."""
    for retired in RETIRED_DECISIONS:
        assert retired.value not in _flat(CONTRACT_INSTRUCTIONS)


#: The production failure's SHAPE, hand-built. `json.dumps` escapes a newline
#: into a valid `\n`, so the malformed reply cannot be produced through it.
def _raw_newline_in_notes(envelope):
    body = (
        '{\n'
        '  "version": 3,\n'
        '  "decision": "stop",\n'
        '  "reason": "done",\n'
        '  "notes": "first line\n'
        'second line"\n'
        '}'
    )
    return f"```json\n{body}\n```" if envelope == "fenced" else f"JSON\n{body}"


@pytest.mark.parametrize("envelope", ["rendered", "fenced"])
def test_a_raw_newline_inside_a_string_value_is_still_rejected(envelope):
    """The parser is deliberately UNCHANGED: the rule above makes this reply
    rarer, never acceptable. A malformed reply is still refused with a code and
    re-prompted inside the same `policy.max_parse_retries` budget.

    Both envelopes, because the reported failures were `line 6 column 2073` —
    a pretty-printed object, i.e. the rendered/bare path — and a test that only
    covered the fenced form would miss the shape that actually took the loop
    down twice."""
    with pytest.raises(ContractError) as excinfo:
        parse_response(_raw_newline_in_notes(envelope))
    assert excinfo.value.code == "invalid_json"
    # The refusal names the real reason, so the corrective re-prompt does too.
    assert "control character" in str(excinfo.value).lower()


def test_a_short_single_line_notes_parses():
    """The shape the rule asks for: within the bound, no line break."""
    directive = parse_response(
        block(base("stop", notes="roadmap drained; nothing else in flight"))
    )
    assert directive.notes == "roadmap drained; nothing else in flight"


@pytest.mark.parametrize("decision", sorted(_COMPLETE_PAYLOADS))
def test_a_directive_omitting_notes_parses_exactly_as_it_did_before(decision):
    """`notes` stays OPTIONAL on every active decision. The task that added the
    rule was about making the field safe, not about deciding its fate — it is
    read by nothing in `orchestrator.py`, `dashboard.py`, `transcript.py` or
    `worktask.py`, and removing it is a separate question. Parametrized so the
    optionality is pinned per decision rather than for `stop` alone."""
    directive = parse_response(block(base(decision, **_COMPLETE_PAYLOADS[decision])))
    assert directive.notes is None


def test_the_notes_rules_are_guidance_the_parser_does_not_enforce():
    r"""The other half of "do not change the parser", and the half an
    enforcement-shaped fix would fail.

    Neither the 200-character bound nor the line-break rule is a parser rule:
    `parse_response` still accepts any string. Enforcing the bound here would
    convert a formatting slip into a `ContractError` charged to the small
    parse-retry budget — the same budget the overlong notes were exhausting —
    and a reply the reviewer meant would be thrown away for being verbose.

    The second case is the rule's own remedy: a newline written the documented
    way (`\n`, which `json.dumps` produces) is valid JSON and parses, carrying
    the real newline through to `Directive.notes`. So the rule asks for
    something that works, not merely for something shorter."""
    long_notes = "x" * 5_000
    assert parse_response(block(base("stop", notes=long_notes))).notes == long_notes

    escaped = "first line\nsecond line"
    assert parse_response(block(base("stop", notes=escaped))).notes == escaped


def test_contract_states_that_one_step_is_a_valid_decomposition():
    """The constraint most easily lost to a trim, and the one whose loss is
    silent: a reviewer who is told to decompose but not that one step counts
    over-splits, which is how one capability became ten tasks with four of them
    already implemented. The rule has to be in the text the model reads."""
    text = CONTRACT_INSTRUCTIONS.lower()
    assert "one step" in text
    assert "decomposition" in text


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
    requirement lands; raising it to make room for explanation is not.

    The decomposition requirement (2026-08-18) IS a genuine new requirement and
    the ceiling still did not move, because the clause was paid for rather than
    added: two lines documenting the key cost a measured 137 characters and the
    reworded `implement` decision 3, against 150 freed by compressing prose
    that states the same rules in fewer words — the envelope paragraph (-49),
    the `reviewed` and `commit.paths` sentences (-28, -8), `approved_paths`
    (-11), the `plan` and `revise` decision lines (-13, -27) and two key
    headers (-7, -7). Net -10. Every content test above still passes, which is
    what "compressed, not deleted" has to mean: no rule was dropped, and the
    one edit that would have CHANGED a rule rather than shortening it — the
    `push` line, whose "the current branch" is what a reader checks a refspec
    against — was reverted for its 18 characters rather than kept for the
    headroom. Hand-summed line by line, like the counts above, because this
    executor had no shell either.

    The same day's follow-up spent a further measured +3: `ordered steps` in the
    schema line became the key that is actually parsed (`steps`, -8) and the
    ordering rule moved into prose about them (+11). The ceiling did NOT move
    for it, and the pointer that would have named the CONTEXT sections the plan
    is authored from — worth ~45 characters — was deliberately not written here:
    those sections label themselves in every request, so paying a per-turn tax
    to restate them is the "room for explanation" this ceiling refuses. Hand
    count, no shell.

    The `notes` rule (2026-08-24) is the first genuine new requirement to be
    paid for out of the headroom this ceiling already had, rather than by
    moving it. It cost +147 — +7 on the `notes` line itself ("anything else
    worth recording" -> "at most 200 characters, on ONE line.") plus two new
    lines of 69 and 68 characters and their joins — and buys the field that
    caused eight `invalid_json` parse errors in thirty hours and two loop_fatal
    `parse_budget_exhausted` parks. The ceiling did NOT move, which is the rule
    this docstring has stated since 2026-08-15: raising it for a real
    requirement is fine, raising it to make room for explanation is not — and
    the explanation for this one lives in the source comment above
    `_RESPONSE_FORMAT`, which costs nothing per turn.

    This is the FIRST entry here whose number was measured rather than
    hand-summed. Every count above says "hand count, no shell"; this executor
    had impl-02's advisory validation channel, so the length was read off a
    real run — **3,636**, i.e. 64 characters of headroom left, and the hand sum
    (3,489 + 147) agreed with it exactly. Treat the earlier figures as the
    estimates they say they are; treat this one as observed.

    **The ceiling MOVED for the first time since 2026-08-15 (recut-01,
    2026-08-24), to 4,550, and this is the accounting.** A ninth DECISION plus a
    new top-level KEY is the "genuine new requirement" the rule above allows —
    the reviewer could not previously say "this branch is contaminated, cut it
    again", so it issued `revise` while arguing against one, and an operator
    performed that recovery by hand twice in one day. A decision the reviewer
    cannot be told about is a decision it will not use, so this text is where
    the cost has to land.

    It was paid down first, not simply added. The first cut measured **4,988**
    on a real advisory run; compressing the three additions — rationale moved
    into the source comments beside `_RESPONSE_FORMAT` and `Decision.RECUT`,
    which cost nothing per turn — took a hand-summed 510 back out (the
    `wanted_decision` key 450 -> 201, the `recut` decision entry 733 -> 504, the
    `recut` vs `stop` trailer 153 -> 121), for roughly **4,478** and ~70
    characters of headroom. The two numbers that are OBSERVED are 3,636 and
    4,988; 4,478 is the difference of one hand sum from the other and should be
    replaced with a real reading by the next executor that measures it.

    What was NOT paid for out of this budget: why two recuts and not three, what
    "unsalvageable" means, how the cap survives the retirement that charges it,
    and the whole argument for the wanted-verb field. Those are in
    `docs/AUTOLOOP.md` §9g and in the source comments — the reviewer needs the
    RULE every turn and the reasoning never.

    **The ceiling MOVED a second time (split-03, 2026-08-26), to 5,300, and this
    is that accounting.** A TENTH decision is the same "genuine new requirement"
    recut-01's move was: the reviewer could not previously say "this task cannot
    be delivered as one reviewable candidate", so brw-14 PASSED review and was
    refused anyway for a 416,193-byte range diff, and an operator hand-wrote the
    same workaround into five task descriptions in one day. A decision the
    reviewer is never told about is a decision it will not use.

    Paid down first, again. The `split` entry was drafted at eleven lines and
    compressed to six, and its `vs revise vs recut` trailer from three lines to
    two, by moving every WHY into the source comment on `Decision.SPLIT` and
    into `docs/AUTOLOOP.md` §9h — both of which cost nothing per turn. What
    survives in the prompt is only what the reviewer must apply while choosing:
    what the verb does, that `tasks` are the successors, at least 2, ONE LEVEL,
    no successor may depend on task_id, when it is refused, and that nothing is
    deleted. What did NOT survive: why one level and not two, why a
    one-successor split is a rename, what "stranded" means, and the whole
    brw-14 account.

    The remaining cost is a hand-summed +634 against the 4,478 hand sum above —
    +457 for the six-line entry, +153 for the trailer, and +24 across the three
    key lines (`decision`, `tasks`, `task_id`) that had to name the new verb.
    That predicted roughly 5,112.

    And this time the prediction was CHECKED. A temporary parametrized probe
    (`len(...) > floor` for six floors) was run through the advisory validation
    channel, which reports failing test ids but no numbers: 4,900 / 5,000 /
    5,100 passed and 5,200 / 5,300 / 5,400 failed, so the real length is in
    (5,100, 5,200] — the hand sum lands inside its own bracket. 5,300 is that
    observed upper bracket plus the same order of headroom every earlier move
    kept, and it is NOT room to write in. The probe was deleted; the reading it
    left behind is `test_split_decision.test_the_measured_length_brackets_the_
    ceiling`, which asserts the tighter 5,200 and so fails BEFORE this ceiling
    does. Tighten this number against that one rather than against the hand sum.

    The second copy of this number is `test_context.py::test_no_scheduling_
    advice_moved_into_the_context_block`, which asserts the same ceiling for its
    own reason. Move both or neither."""
    assert len(CONTRACT_INSTRUCTIONS) <= 5300


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
