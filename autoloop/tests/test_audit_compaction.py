"""Report compaction: structural bounds, reshape, merge-preserving dedup.

The whole point of these changes is to make the audit report smaller **without
losing anything**, so almost every test here is a preservation test rather than
a size test. Three losses are specifically guarded against:

* a finding silently **truncated** to fit,
* a finding silently **dropped** because it was too long or looked like a
  duplicate,
* a finding silently **accepted** oversized after a failed reshape, putting the
  outlier back in the report the bounds exist to keep out.

The bounds themselves came from measuring a real report: 28 blocks, median
1,464 chars, one at 21,022. They are set to catch that shape and nothing else.
"""

import json

import pytest

from autoloop.errors import AuditError
from test_audit_executor import AgentResult, audit_directive, build_executor
from test_audit_executor import repo as _repo  # pytest fixture, re-exported below

from autoloop.audit.findings import (
    MAX_EVIDENCE_CHARS,
    MAX_FINDING_CHARS,
    oversize_reasons,
    parse_findings,
)
from autoloop.audit.reconcile import reconcile
from autoloop.audit.report import render_report
from autoloop.audit.taskgen import generate_tasks
from autoloop.tasks import TaskRegistry

#: Re-exported so the reshape tests below can request the real-git fixture.
#: Aliased on import so the test parameters named `repo` do not read as a
#: redefinition of it.
repo = _repo


def item(**overrides):
    base = {
        "id": "sec-01",
        "category": "security",
        "severity": "high",
        "confidence": "confirmed",
        "affected_files": ["a.py"],
        "symbols": ["a.f"],
        "evidence": "a.py:10 does X",
        "impact": "users lose Y",
        "proposed_action": "do Z",
        "dependencies": [],
        "acceptance_criteria": ["Y no longer happens"],
        "validation_commands": ["ruff check ."],
        "safe_to_parallelize": True,
    }
    base.update(overrides)
    return base


def payload(*items):
    return json.dumps({"findings": list(items)})


def finding(**overrides):
    outcome = parse_findings(payload(item(**overrides)), "dom")
    assert outcome.findings, outcome.rejected or outcome.oversized
    return outcome.findings[0]


# ---- structural bounds hold findings, they never shorten them --------------


def test_an_ordinary_finding_is_not_oversized():
    assert oversize_reasons(finding()) == ()


def test_an_essay_in_evidence_is_held_not_truncated_and_not_rejected():
    """The 21k-char real-world outlier: prose written into a field specified as
    'cite locations'. It must survive intact, in neither bucket."""
    essay = "x" * (MAX_EVIDENCE_CHARS + 1)
    outcome = parse_findings(payload(item(evidence=essay)), "dom")

    assert outcome.findings == []
    assert outcome.rejected == []  # NOT a rejection — the finding is valid
    assert len(outcome.oversized) == 1
    held = outcome.oversized[0]
    assert held.finding_id == "sec-01"
    # Byte-for-byte intact, so a reshape compresses the agent's own words.
    assert held.item["evidence"] == essay
    assert any("evidence" in reason for reason in held.reasons)


def test_many_legal_fields_still_trip_the_whole_finding_budget():
    """Otherwise a report could be inflated by fields that are each fine."""
    chunk = "y" * 300
    held = parse_findings(
        payload(
            item(
                evidence="e" * 600,
                impact="i" * 390,
                proposed_action="p" * 290,
                acceptance_criteria=[chunk, chunk, chunk, chunk],
            )
        ),
        "dom",
    ).oversized
    assert held and any(str(MAX_FINDING_CHARS) in r for r in held[0].reasons)


def test_a_held_finding_is_still_counted_as_usable_output():
    """A domain whose only finding was oversized has NOT failed — reporting it
    as unusable would mark the domain uncovered and hide a real finding."""
    outcome = parse_findings(payload(item(evidence="x" * 5000)), "dom")
    assert outcome.usable is True


def test_oversize_check_runs_after_validation_not_instead_of_it():
    outcome = parse_findings(payload(item(severity="enormous", evidence="x" * 5000)), "dom")
    assert outcome.oversized == []
    assert outcome.rejected and "severity" in outcome.rejected[0].reason


# ---- merge-preserving deduplication ----------------------------------------


def test_duplicates_are_merged_not_discarded():
    """The pre-2026-08-01 behaviour kept the better instance and threw the other
    away, losing evidence and acceptance criteria nothing else carried. These
    two describe the SAME defect — that is what licenses the fold at all."""
    a = finding(id="a", evidence="a.py:10 logs the header",
                acceptance_criteria=["no token in logs"], confidence="confirmed",
                impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    b = finding(id="b", evidence="a.py:44 logs it again",
                acceptance_criteria=["no header in logs"], confidence="probable",
                impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")

    result = reconcile([a, b])
    kept = result.accepted()

    assert len(kept) == 1
    merged = kept[0]
    # Every unique statement from BOTH survives.
    assert "a.py:10 logs the header" in merged.evidence
    assert "a.py:44 logs it again" in merged.evidence
    assert set(merged.acceptance_criteria) == {"no token in logs", "no header in logs"}
    assert result.duplicates == [("dom:b", "dom:a")]


def test_a_merge_attributes_the_folded_finding():
    a = finding(id="a", impact="tokens leak into logs", proposed_action="redact tokens")
    b = finding(id="b", impact="tokens leak into logs badly", proposed_action="redact tokens")
    merged = reconcile([a, b]).accepted()[0]
    assert "dom:b" in merged.impact  # attribution, not anonymous concatenation


def test_identical_text_is_not_duplicated_into_the_merged_finding():
    a = finding(id="a", impact="same impact")
    b = finding(id="b", impact="same impact")
    merged = reconcile([a, b]).accepted()[0]
    assert merged.impact.count("same impact") == 1


def test_distinct_defects_in_the_same_symbol_stay_separate():
    """The guarantee this whole predicate exists for. An off-by-one and an
    unchecked return in `mod.f` are two problems — two severities, two
    remediations, two tasks. Folding them yields one task whose identity
    belongs to neither, and preserving both texts inside it does NOT repair
    that, because one finding becomes one task.

    An earlier version of this file asserted the opposite and pinned the bug.
    """
    a = finding(id="a", affected_files=["a.py"], symbols=["mod.f"],
                impact="loop reads one element past the end of the buffer",
                proposed_action="clamp the loop bound to len(buffer)")
    b = finding(id="b", affected_files=["a.py"], symbols=["mod.f"],
                impact="the return value of write() is ignored so partial writes vanish",
                proposed_action="raise when write() reports fewer bytes than requested")

    kept = reconcile([a, b]).accepted()

    assert len(kept) == 2, "two distinct defects must not become one task"
    assert {f.id for f in kept} == {"a", "b"}


def test_location_overlap_alone_never_folds():
    """Same file, same symbol, same category — and nothing else in common.
    Location is a prefilter, never a verdict."""
    a = finding(id="a", affected_files=["a.py"], symbols=["mod.f"],
                impact="timestamps are stored without a timezone",
                proposed_action="store aware datetimes in UTC")
    b = finding(id="b", affected_files=["a.py"], symbols=["mod.f"],
                impact="the retry budget is never reset between sessions",
                proposed_action="zero the counter when a session starts")

    assert len(reconcile([a, b]).accepted()) == 2


def test_a_fold_requires_the_substance_signal_not_just_the_location():
    """Two agents reporting the SAME defect in near-identical terms do fold —
    otherwise deduplication would never fire at all."""
    shared = dict(
        affected_files=["a.py"],
        symbols=["mod.f"],
        impact="authorization tokens leak into the request log",
        proposed_action="redact authorization tokens from the request log",
    )
    a = finding(id="a", confidence="confirmed", **shared)
    b = finding(id="b", confidence="probable", **shared)

    result = reconcile([a, b])

    assert len(result.accepted()) == 1
    assert result.duplicates == [("dom:b", "dom:a")]


def test_evidence_alone_cannot_make_two_findings_look_the_same():
    """Two agents citing the same file:line is location agreement wearing a
    different hat — it must not push distinct defects over the threshold."""
    cited = "a.py:10-20 — see the block quoted here verbatim in both reports"
    a = finding(id="a", evidence=cited, impact="unbounded memory growth",
                proposed_action="cap the cache size")
    b = finding(id="b", evidence=cited, impact="password printed on stderr",
                proposed_action="remove the debug print")

    assert len(reconcile([a, b]).accepted()) == 2


def test_findings_in_different_files_never_fold():
    """Location overlap is the prefilter; without it nothing is even
    considered, however similar the wording."""
    a = finding(id="a", affected_files=["a.py"], impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    b = finding(id="b", affected_files=["b.py"], impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    assert len(reconcile([a, b]).accepted()) == 2


def test_different_categories_never_merge():
    a = finding(id="a", category="security")
    b = finding(id="b", category="doc_drift")
    assert len(reconcile([a, b]).accepted()) == 2


def test_a_merge_keeps_the_stronger_severity_and_confidence():
    weak = finding(id="w", severity="low", confidence="probable")
    strong = finding(id="s", severity="critical", confidence="confirmed")
    merged = reconcile([weak, strong]).accepted()[0]
    assert (merged.severity, merged.confidence) == ("critical", "confirmed")


def test_a_merge_takes_the_cautious_parallelism_answer():
    """One agent's optimism must not override another's caution."""
    safe = finding(id="a", safe_to_parallelize=True)
    unsafe = finding(id="b", safe_to_parallelize=False)
    assert reconcile([safe, unsafe]).accepted()[0].safe_to_parallelize is False


def test_merging_is_order_independent_in_what_it_preserves():
    a = finding(id="a", acceptance_criteria=["ac-a"], evidence="a.py:1 alpha",
                impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    b = finding(id="b", acceptance_criteria=["ac-b"], evidence="a.py:2 beta",
                impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    forward = reconcile([a, b]).accepted()[0]
    backward = reconcile([b, a]).accepted()[0]
    for merged in (forward, backward):
        assert set(merged.acceptance_criteria) == {"ac-a", "ac-b"}
        assert "alpha" in merged.evidence and "beta" in merged.evidence


def test_three_reports_of_one_defect_collapse_without_losing_any_of_them():
    a = finding(id="a", acceptance_criteria=["one"], impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    b = finding(id="b", acceptance_criteria=["two"], impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    c = finding(id="c", acceptance_criteria=["three"], impact="authorization tokens leak into the request log",
                proposed_action="redact authorization tokens from the request log")
    merged = reconcile([a, b, c]).accepted()
    assert len(merged) == 1
    assert set(merged[0].acceptance_criteria) == {"one", "two", "three"}


# ---- the report renders the task graph once --------------------------------


def render(findings):
    reconciled = reconcile(findings)
    proposal = generate_tasks(reconciled, TaskRegistry())
    return render_report(
        date="2026-08-01",
        branch="main",
        head_sha="a" * 40,
        dirty_count=0,
        scope=None,
        feedback=None,
        validation_runs=[],
        reconciled=reconciled,
        proposal=proposal,
        agent_failures=[],
        raw_reports_dir="/tmp/x",
    ), proposal


def test_the_task_graph_is_rendered_once_as_json():
    text, proposal = render([finding(id="a", severity="critical")])
    assert proposal.tasks, "test needs at least one proposed task"
    assert "```json" in text
    # The Markdown table carried the same fields as the JSON; it is gone.
    assert "| id | P | title | depends on | parallel |" not in text


def test_every_proposed_task_is_still_present_in_the_json():
    text, proposal = render(
        [finding(id="a", severity="critical"), finding(id="b", category="defect")]
    )
    block = text.split("```json")[1].split("```")[0]
    rendered = json.loads(block)
    assert {t["id"] for t in rendered} == {t.id for t in proposal.tasks}


def test_a_merged_findings_content_reaches_the_report():
    """End to end: nothing lost between two agents reporting and the bytes the
    reviewer actually reads."""
    a = finding(id="a", impact="alpha impact", acceptance_criteria=["ac-alpha"])
    b = finding(id="b", impact="beta impact", acceptance_criteria=["ac-beta"])
    text, _ = render([a, b])
    for fragment in ("alpha impact", "beta impact", "ac-alpha", "ac-beta"):
        assert fragment in text


@pytest.mark.parametrize("field_name", ["evidence", "impact", "proposed_action"])
def test_no_bound_is_enforced_by_shortening_anything(field_name):
    """Guards the property that matters most: the code has no truncation path.
    If someone 'fixes' an oversized finding by slicing it, this fails."""
    long_value = "z" * 5000
    outcome = parse_findings(payload(item(**{field_name: long_value})), "dom")
    assert outcome.oversized
    assert outcome.oversized[0].item[field_name] == long_value


# ---- the reshape round is bounded, and its failure parks -------------------
#
# These exercise the executor end to end with a fake agent runner, because the
# guarantee under test is about control flow across the whole run, not about a
# single function: exactly one reshape attempt, then a park with the finding
# preserved on disk — never a loop, never a silent accept, drop or truncate.

class ReshapeRunner:
    """Emits an oversized finding on the first pass, then whatever `reshaped`
    says on the reshape pass. Records every spec so the number of rounds is
    observable."""

    def __init__(self, reshaped=None, reshape_fails=False):
        self.reshaped = reshaped
        self.reshape_fails = reshape_fails
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        is_reshape = spec.domain.endswith("-reshape")
        if is_reshape:
            if self.reshape_fails:
                return AgentResult(
                    domain=spec.domain, raw_text="", returncode=1,
                    duration_seconds=0.1, command=("claude",), error="boom",
                )
            text = self.reshaped if self.reshaped is not None else json.dumps({"findings": []})
        elif spec.domain == "docs_drift":
            text = payload(item(id="big", category="doc_drift", evidence="x" * 5000))
        else:
            text = json.dumps({"findings": []})
        return AgentResult(
            domain=spec.domain, raw_text=text, returncode=0,
            duration_seconds=0.1, command=("claude",),
        )

    @property
    def reshape_rounds(self):
        return sum(1 for s in self.specs if s.domain.endswith("-reshape"))


def test_a_successful_reshape_keeps_the_finding_and_runs_exactly_one_round(repo, tmp_path):
    compact = payload(item(id="big", category="doc_drift", evidence="a.py:10 is stale",
                           impact="docs mislead readers"))
    runner = ReshapeRunner(reshaped=compact)
    executor = build_executor(repo, tmp_path, runner=runner)

    outcome = executor.execute(audit_directive(), task=None)

    assert runner.reshape_rounds == 1  # bounded: one attempt, not a loop
    assert outcome.status == "ok"
    report = (repo / "docs").glob("AUDIT_*.md")
    text = next(report).read_text(encoding="utf-8")
    assert "a.py:10 is stale" in text  # the reshaped finding is IN the report


def test_a_still_oversized_reshape_parks_and_preserves_the_finding(repo, tmp_path):
    runner = ReshapeRunner(
        reshaped=payload(item(id="big", category="doc_drift", evidence="y" * 5000))
    )
    executor = build_executor(repo, tmp_path, runner=runner)

    with pytest.raises(AuditError) as exc:
        executor.execute(audit_directive(), task=None)

    assert runner.reshape_rounds == 1  # never a second attempt
    assert "still oversized" in str(exc.value)
    preserved = list((tmp_path / "runs").rglob("oversized_findings.json"))
    assert preserved, "the finding must be preserved verbatim on disk"
    saved = json.loads(preserved[0].read_text(encoding="utf-8"))
    assert saved[0]["item"]["evidence"] == "x" * 5000  # the ORIGINAL, untouched


def test_a_failed_reshape_agent_parks_rather_than_dropping_the_finding(repo, tmp_path):
    runner = ReshapeRunner(reshape_fails=True)
    executor = build_executor(repo, tmp_path, runner=runner)

    with pytest.raises(AuditError):
        executor.execute(audit_directive(), task=None)
    assert runner.reshape_rounds == 1


def test_a_reshape_returning_nothing_parks_instead_of_silently_losing_it(repo, tmp_path):
    """An empty reshape is the most dangerous shape: it looks like success
    while the finding has vanished."""
    runner = ReshapeRunner(reshaped=json.dumps({"findings": []}))
    executor = build_executor(repo, tmp_path, runner=runner)

    with pytest.raises(AuditError):
        executor.execute(audit_directive(), task=None)


def test_a_held_finding_blocks_completion_even_though_its_domain_is_covered(repo, tmp_path):
    """The two halves must hold together, which is why they are asserted as a
    pair rather than in two separate tests.

    "Covered" and "complete" answer different questions. A domain whose only
    finding was held DID produce usable output — calling it uncovered would
    hide a real finding behind a coverage warning. But the run must NOT finish
    while that finding is unresolved, or the report ships silently missing it.
    Getting either half wrong loses the finding; getting them backwards loses
    it twice over.
    """
    runner = ReshapeRunner(reshaped=json.dumps({"findings": []}))
    executor = build_executor(repo, tmp_path, runner=runner)

    with pytest.raises(AuditError) as exc:
        executor.execute(audit_directive(), task=None)

    # No report was written: completion is genuinely blocked, not merely warned.
    assert not list((repo / "docs").glob("AUDIT_*.md"))
    assert "lost, not shortened" in str(exc.value)
    # And the domain that produced it was never marked unusable.
    first_pass = parse_findings(
        payload(item(id="big", category="doc_drift", evidence="x" * 5000)), "docs_drift"
    )
    assert first_pass.usable is True and first_pass.oversized
