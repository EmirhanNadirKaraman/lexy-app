"""Response-contract v2 parsing: valid directives for every decision
(task-id-based work, plan batches, review-integrity stamps) and strict
rejection — never guessing — for every malformed shape."""

import json

import pytest

from autoloop.contract import Decision, parse_response, verify_review
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


def test_last_json_block_wins():
    first = f"```json\n{json.dumps(base('stop'))}\n```"
    second = f"```json\n{json.dumps(base('implement', task_id='t9'))}\n```"
    directive = parse_response(f"Draft:\n{first}\n\nFinal answer:\n{second}")
    assert directive.task_id == "t9"


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
