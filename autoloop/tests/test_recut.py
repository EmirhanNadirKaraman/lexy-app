"""The reviewer's `recut` verdict, and the wanted-verb field beside it.

Two mechanisms, one task (recut-01, 2026-08-24), and they are tested together
because they are two answers to the same question — what happens when the
reviewer needs a verb the protocol does not have.

* **`recut`** is the verb it now HAS: discard an unsalvageable candidate and cut
  the task again from the current base, bounded so a destructive action the
  reviewer takes by itself cannot repeat forever, cannot touch published work,
  and cannot discard a candidate whose verdict is still outstanding.
* **`wanted_decision`** is how the NEXT missing verb gets found by counting
  rather than by a human noticing one directive's prose. It is parsed, recorded
  and tallied, and it is structurally unable to reach `_dispatch` — the tests
  under "the wanted verb can never be dispatched" are the ones that matter,
  because a field that is merely *unlikely* to be acted on is an unbounded
  reviewer vocabulary waiting for a refactor.

Real git and real worker repos throughout (the recut's whole claim is about what
is on disk afterwards), with the small `run_git` / executor / build helpers
duplicated per this suite's self-contained convention.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import (
    ACTIVE_DECISIONS,
    RETIRED_DECISIONS,
    CONTRACT_INSTRUCTIONS,
    Decision,
    Directive,
    parse_response,
)
from autoloop.errors import ContractError, StateCorruptError, TaskGraphError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_TASK_RECUTS,
    RECUT_RETIREMENT_REASON,
    MAX_WANTED_DECISION_CHARS,
    MAX_WANTED_DECISION_KINDS,
    WANTED_DECISION_OVERFLOW,
    Orchestrator,
    WantedDecisionTally,
    wanted_decisions_file,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import HOLD_ORIGIN_OPERATOR, Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import IntentStore, TaskExecutionStore

URL = "https://chatgpt.com/c/test-conversation"

#: Every task in these tests may write both files, so a round before a recut and
#: a round after it can touch DIFFERENT paths without a scope refusal — which is
#: what makes "the second cut carried nothing over" observable.
PATHS = ("docs/A.md", "docs/B.md")


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def block(obj) -> str:
    return "Reasoning...\n```json\n" + json.dumps(obj) + "\n```"


def decomp(files=("docs/A.md",)):
    return {
        "approach": "one commit",
        "files": list(files),
        "steps": ["write the file"],
    }


def implement_block(task_id="t1", files=("docs/A.md",)):
    return block(
        {
            "version": 3,
            "decision": "implement",
            "reason": "next",
            "task_id": task_id,
            "decomposition": decomp(files),
        }
    )


def recut_block(task_id="t1", reason="the branch is contaminated", wanted=None):
    data = {
        "version": 3,
        "decision": "recut",
        "reason": reason,
        "task_id": task_id,
    }
    if wanted is not None:
        data["wanted_decision"] = wanted
    return block(data)


def stop_block(reason="all done", wanted=None):
    data = {"version": 3, "decision": "stop", "reason": reason}
    if wanted is not None:
        data["wanted_decision"] = wanted
    return block(data)


class FakeClient:
    """Minimal conversation double: hands back scripted replies in order and
    remembers what was submitted."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.closed = False
        self.send_attempted = False

    def attach(self):
        pass

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        from autoloop.browser.chatgpt import SubmitResult

        self.send_attempted = True
        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        self.closed = True


class WritingExecutor:
    """Writes into the dispatched task's own worker repo (`workers_root/<id>`)
    and reports success.

    `per_call_files`, when given, is consumed one entry per call, so a round
    before a recut and the round after it can write different paths — the only
    way to SEE that the second cut inherited nothing from the first.
    """

    def __init__(self, workers_root, files=None, per_call_files=None, status="ok"):
        self.workers_root = Path(workers_root)
        self.files = dict(files or {"docs/A.md": "# first"})
        self.per_call_files = list(per_call_files) if per_call_files else None
        self.status = status
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        if self.per_call_files is not None:
            files = self.per_call_files[len(self.calls)]
        else:
            files = self.files
        self.calls.append((directive, task))
        worker = self.workers_root / task.id
        for rel, content in files.items():
            target = worker / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"{content}\n<!-- call {len(self.calls)} -->\n", encoding="utf-8"
            )
        return ExecutionOutcome(
            status=self.status,
            summary="did it",
            details="details",
            validation="ruff clean",
            changed_paths=tuple(files),
        )


@dataclass
class Wiring:
    orch: Orchestrator
    git: GitGateway
    registry: TaskRegistry
    task_store: TaskStore
    execution_store: TaskExecutionStore
    worker_repos: WorkerRepoManager
    executor: WritingExecutor
    config: AutoloopConfig
    store: StateStore
    client: FakeClient
    tmp_path: Path


def make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def build(
    tmp_path,
    responses=(),
    tasks=(),
    per_call_files=None,
    files=None,
    policy=None,
    reuse=None,
) -> Wiring:
    """A real-git Orchestrator with a real `WorkerRepoManager`, so a recut's
    quarantine and archive can be asserted on disk.

    `reuse` rebuilds over an EXISTING wiring's paths with a fresh `LoopState`
    and a registry re-read from `tasks.json` — the shape of a new session after
    a recut, and the only way to prove the recut count survived on disk rather
    than merely in memory.
    """
    if reuse is not None:
        repo_root = reuse.git.repo_root
        config = reuse.config
        worker_repos = reuse.worker_repos
        execution_store = reuse.execution_store
        task_store = reuse.task_store
        registry = task_store.load()
        git = reuse.git
    else:
        repo_root = make_repo(tmp_path)
        policy_config = policy or PolicyConfig(implement_enabled=True)
        git = GitGateway(repo_root, PolicyEngine(policy_config))
        worker_repos = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
        execution_store = TaskExecutionStore(tmp_path / "executions")
        config = AutoloopConfig(
            browser=BrowserConfig(conversation_url=URL),
            policy=policy_config,
            state_dir=tmp_path / ".al",
        )
        task_store = TaskStore(config.tasks_file)
        registry = TaskRegistry(list(tasks))
        task_store.save(registry)

    state = LoopState.new(URL)
    state.outbox = "kickoff report"
    store = StateStore(config.state_file)
    store.save(state)

    executor = WritingExecutor(
        worker_repos.root_dir, files=files, per_call_files=per_call_files
    )
    client = FakeClient(responses)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=IntentStore(tmp_path / "intents"),
        validation_runner=ok_validation,
    )
    return Wiring(
        orch=orch,
        git=git,
        registry=registry,
        task_store=task_store,
        execution_store=execution_store,
        worker_repos=worker_repos,
        executor=executor,
        config=config,
        store=store,
        client=client,
        tmp_path=tmp_path,
    )


def ready_task(tid="t1", **kwargs):
    return Task(
        id=tid,
        title=f"Title {tid}",
        description="desc",
        approved_paths=PATHS,
        **kwargs,
    )


def records(wiring, kind):
    path = wiring.config.transcript_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == kind:
            out.append(entry.get("data") or {})
    return out


def denial_codes(wiring):
    return [record.get("code") for record in records(wiring, "policy_denied")]


# ---------------------------------------------------------------------------
# contract: the new decision parses, and the old vocabulary still does
# ---------------------------------------------------------------------------


def test_recut_parses_and_carries_its_task_id_and_reason():
    directive = parse_response(recut_block("port-01", reason="contaminated branch"))
    assert directive.decision is Decision.RECUT
    assert directive.task_id == "port-01"
    assert directive.reason == "contaminated branch"


def test_recut_without_a_task_id_is_rejected():
    """A recut that names nothing names nothing to discard. Refused by the
    parser rather than defaulted to "the current task": a destructive verb must
    never guess which work it is about."""
    with pytest.raises(ContractError) as exc:
        parse_response(block({"version": 3, "decision": "recut", "reason": "r"}))
    assert exc.value.code == "missing_field:task_id"


def test_recut_is_an_active_decision_and_is_advertised():
    assert Decision.RECUT in ACTIVE_DECISIONS
    assert Decision.RECUT not in RETIRED_DECISIONS
    assert "recut" in CONTRACT_INSTRUCTIONS


def test_the_instructions_say_which_of_recut_and_stop_to_use():
    """The bound the task states in words: `stop` parks because a HUMAN must
    decide, `recut` is the reviewer deciding, and an unsure reviewer should
    still choose `stop`. A verb offered without that sentence is one the
    reviewer has to guess the boundary of."""
    text = CONTRACT_INSTRUCTIONS
    assert "`recut` vs `stop`" in text
    assert "unsure" in text.lower()
    # The two halves of the distinction, stated rather than implied.
    assert "YOU deciding" in text and "a HUMAN to decide" in text


#: The verbs that did NOT exist before recut-01 wrote this file: `recut`, whose
#: compatibility claim is the point of the test below, and `split` (split-03),
#: which arrived later still. Excluding both is what keeps the test about the
#: vocabulary that predates this task — including one of them would build a
#: payload for a verb that had not been invented and assert it parses as legacy.
_ADDED_SINCE_THE_PREVIOUS_VOCABULARY = {Decision.RECUT, Decision.SPLIT}


@pytest.mark.parametrize(
    "decision",
    sorted(
        d.value
        for d in ACTIVE_DECISIONS | RETIRED_DECISIONS
        if d not in _ADDED_SINCE_THE_PREVIOUS_VOCABULARY
    ),
)
def test_a_reply_written_against_the_previous_vocabulary_still_parses(decision):
    """The compatibility claim, checked against every decision that existed
    before this task — including the retired `ask_user`, whose whole retirement
    depends on still parsing. None of them carries `wanted_decision`, which is
    the other half: an omitted field must behave exactly as it did before it
    existed."""
    payload = {"version": 3, "decision": decision, "reason": "r"}
    if decision in ("implement", "revise"):
        payload["task_id"] = "t1"
    if decision == "revise":
        payload["feedback"] = "fix it"
    if decision == "plan":
        payload["tasks"] = [{"id": "t1", "title": "T", "description": "d"}]
    if decision in ("commit", "commit_and_push"):
        payload["commit"] = {"message": "m", "paths": ["a.py"]}
    if decision in ("commit", "push", "commit_and_push"):
        payload["reviewed"] = {
            "request_id": "alr-x-0001",
            "head_sha": "a" * 40,
            "report_sha256": "b" * 64,
        }
    directive = parse_response(block(payload))
    assert directive.decision.value == decision
    assert directive.wanted_decision is None


def test_the_unknown_decision_correction_now_offers_recut():
    """A reviewer sent an unusable verb is told what it may choose from. If
    `recut` were missing there, the verb would exist and be unreachable to the
    one reader that needs to hear about it."""
    with pytest.raises(ContractError) as exc:
        parse_response(block({"version": 3, "decision": "nope", "reason": "r"}))
    assert exc.value.code == "unknown_decision"
    assert "recut" in str(exc.value)
    # ...and still never advertises a retired decision.
    assert "ask_user" not in str(exc.value)


# ---------------------------------------------------------------------------
# contract: the wanted verb parses, and is never validated as a decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("decision", sorted(d.value for d in ACTIVE_DECISIONS))
def test_wanted_decision_is_accepted_on_every_active_decision(decision):
    """It says "none of these fitted", and the reviewer still had to send one of
    them — so forbidding it per-decision would forbid it exactly where it is
    used."""
    payload = {"version": 3, "decision": decision, "reason": "r", "wanted_decision": "split"}
    if decision in ("implement", "revise", "recut", "split"):
        payload["task_id"] = "t1"
    if decision == "revise":
        payload["feedback"] = "fix it"
    if decision == "plan":
        payload["tasks"] = [{"id": "t1", "title": "T", "description": "d"}]
    if decision == "split":
        # A successor id distinct from `task_id` above: a split naming its own
        # parent is refused at dispatch, and a fixture that quietly wrote one
        # would read as if the parser had blessed it.
        payload["tasks"] = [{"id": "t1-a", "title": "T", "description": "d"}]
    if decision in ("commit", "commit_and_push"):
        payload["commit"] = {"message": "m", "paths": ["a.py"]}
    if decision in ("commit", "push", "commit_and_push"):
        payload["reviewed"] = {
            "request_id": "alr-x-0001",
            "head_sha": "a" * 40,
            "report_sha256": "b" * 64,
        }
    assert parse_response(block(payload)).wanted_decision == "split"


@pytest.mark.parametrize("value", sorted(d.value for d in Decision))
def test_a_wanted_decision_naming_a_real_decision_is_kept_verbatim(value):
    """Deliberately NOT validated against `Decision`. A reviewer that names an
    existing verb is telling us the instructions are unclear — it believed the
    fitting verb was unavailable when it was not — and that is a signal to
    record, not an error to raise."""
    directive = parse_response(block(
        {"version": 3, "decision": "stop", "reason": "r", "wanted_decision": value}
    ))
    assert directive.wanted_decision == value
    # And it is still a plain string, never coerced into the enum.
    assert isinstance(directive.wanted_decision, str)
    assert not isinstance(directive.wanted_decision, Decision)


def test_a_blank_wanted_decision_is_rejected():
    """Optional, never blank: an empty answer to "what would you have used" is
    worse than no answer, because it counts as an occurrence of nothing."""
    with pytest.raises(ContractError) as exc:
        parse_response(block(
            {"version": 3, "decision": "stop", "reason": "r", "wanted_decision": "  "}
        ))
    assert exc.value.code == "missing_field:wanted_decision"


def test_a_non_string_wanted_decision_is_rejected():
    with pytest.raises(ContractError):
        parse_response(block(
            {"version": 3, "decision": "stop", "reason": "r", "wanted_decision": 7}
        ))


def test_the_instructions_state_the_cap_the_loop_actually_enforces():
    """`MAX_TASK_RECUTS` lives in `orchestrator.py`, and `contract.py` cannot
    import it (the dependency runs the other way), so the prompt carries the
    number as a literal. This is what stops the two agreeing today and silently
    disagreeing the first time the constant moves — a reviewer told it has three
    cuts when it has two spends its last one believing it has a spare."""
    assert f"after {MAX_TASK_RECUTS} recuts" in CONTRACT_INSTRUCTIONS


def test_the_wanted_decision_key_is_documented_in_the_instructions():
    assert "wanted_decision" in CONTRACT_INSTRUCTIONS
    assert "NEVER acted on" in CONTRACT_INSTRUCTIONS


# ---------------------------------------------------------------------------
# the wanted verb can never be dispatched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wanted",
    sorted(d.value for d in Decision) + ["split", "rebase", "", "  push  ", "PUSH"],
)
def test_a_wanted_verb_never_becomes_the_verb_that_is_acted_on(tmp_path, wanted):
    """THE hard bound. Every value a reviewer could write — including the name
    of a real, powerful decision — rides on a `stop`, and what happens is a
    stop: no executor round, no git push, no recut of the live task.

    A directive is built by hand rather than parsed, so even a value the parser
    would refuse (the empty string) is put through `_dispatch` — the claim is
    about what the dispatcher can do with the field, not about what reaches it.
    """
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    orch = wiring.orch
    orch.state.last_response = None
    directive = Directive(
        decision=Decision.STOP,
        reason="ending",
        wanted_decision=wanted,
    )
    orch._dispatch(directive)

    assert orch.state.phase == Phase.STOPPED.value
    assert wiring.executor.calls == []
    assert wiring.registry.get("t1").status == "pending"
    assert wiring.registry.get("t1").recut_count == 0
    assert not (wiring.tmp_path / "workers" / "t1").exists()
    assert records(wiring, "task_recut") == []


def test_a_wanted_recut_on_a_stop_discards_nothing(tmp_path):
    """The sharpest instance of the rule above: a live candidate exists, the
    reviewer writes `wanted_decision: "recut"`, and the candidate is still
    there afterwards. If the field could ever be acted on, this is where it
    would show."""
    wiring = build(tmp_path, responses=[implement_block("t1")], tasks=[ready_task("t1")])
    wiring.orch.run(max_steps=4)
    execution = wiring.execution_store.load("t1")
    assert execution is not None and execution.candidate_sha

    wiring.orch._dispatch(
        Directive(decision=Decision.STOP, reason="ending", wanted_decision="recut")
    )

    still_there = wiring.execution_store.load("t1")
    assert still_there is not None
    assert still_there.candidate_sha == execution.candidate_sha
    assert (wiring.tmp_path / "workers" / "t1").is_dir()
    assert not (wiring.tmp_path / "quarantine").exists()


def test_the_directive_dispatch_reads_only_the_decision_field():
    """A structural statement of the same thing, at the type level: the parsed
    field is a `str`, `Decision` is never constructed from it, and the two
    fields are independent on the `Directive`."""
    directive = parse_response(block(
        {"version": 3, "decision": "stop", "reason": "r", "wanted_decision": "push"}
    ))
    assert directive.decision is Decision.STOP
    assert directive.wanted_decision == "push"
    assert type(directive.wanted_decision) is str


# ---------------------------------------------------------------------------
# the tally
# ---------------------------------------------------------------------------


def test_the_tally_counts_occurrences_across_directives(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    orch = wiring.orch
    for wanted in ("recut", "split", "recut"):
        orch._record_wanted_decision(
            Directive(decision=Decision.STOP, reason="r", wanted_decision=wanted), None
        )
    counts = json.loads(
        wanted_decisions_file(wiring.config.state_dir).read_text(encoding="utf-8")
    )
    assert counts == {"recut": 2, "split": 1}
    events = records(wiring, "wanted_decision")
    assert len(events) == 3
    assert events[-1]["tally"] == {"recut": 2, "split": 1}


def test_the_rendered_tally_is_what_an_operator_reads(tmp_path):
    """`wanted: recut x7, split x3` — ordered by count so the roadmap reads off
    the top, and carried on the event's `result` key, which is what
    `dashboard.collect`'s recent-events feed renders when no `decision`, `code`
    or `error` is present."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    for _ in range(7):
        wiring.orch._record_wanted_decision(
            Directive(decision=Decision.STOP, reason="r", wanted_decision="recut"), None
        )
    for _ in range(3):
        wiring.orch._record_wanted_decision(
            Directive(decision=Decision.STOP, reason="r", wanted_decision="split"), None
        )
    event = records(wiring, "wanted_decision")[-1]
    assert event["result"] == "wanted: recut x7, split x3"
    # The keys the dashboard would prefer over `result` are deliberately absent,
    # so the operator sees the tally rather than a decision name.
    assert "decision" not in event and "code" not in event and "error" not in event


def test_the_tally_outlives_the_loop_state_it_was_counted_in(tmp_path):
    """`cli._select_and_kickoff` replaces the whole `LoopState` at every session
    boundary, so a counter living there would be reset by the very transition it
    exists to count. `build(reuse=...)` is that boundary."""
    first = build(tmp_path, tasks=[ready_task("t1")])
    first.orch._record_wanted_decision(
        Directive(decision=Decision.STOP, reason="r", wanted_decision="recut"), None
    )
    second = build(tmp_path, reuse=first)
    second.orch._record_wanted_decision(
        Directive(decision=Decision.STOP, reason="r", wanted_decision="recut"), None
    )
    counts = json.loads(
        wanted_decisions_file(second.config.state_dir).read_text(encoding="utf-8")
    )
    assert counts == {"recut": 2}


def test_a_directive_without_the_field_writes_nothing_at_all(tmp_path):
    """The compatibility claim on the loop side: today's directives carry no
    wanted verb, and they must leave no event and no file behind."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._record_wanted_decision(
        Directive(decision=Decision.STOP, reason="r"), None
    )
    assert records(wiring, "wanted_decision") == []
    assert not wanted_decisions_file(wiring.config.state_dir).exists()


def test_an_unreadable_tally_is_rebuilt_and_the_transcript_says_so(tmp_path):
    """It enforces nothing, so an unreadable file must not park a round — but a
    rebuilt count must never look like a first sighting, or a lost history reads
    as "nobody has ever asked for this"."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    path = wanted_decisions_file(wiring.config.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    wiring.orch._record_wanted_decision(
        Directive(decision=Decision.STOP, reason="r", wanted_decision="recut"), None
    )
    event = records(wiring, "wanted_decision")[-1]
    assert event["tally"] == {"recut": 1}
    assert event["tally_reset"] is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"recut": 1}


def test_a_first_count_is_not_reported_as_a_reset(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._record_wanted_decision(
        Directive(decision=Decision.STOP, reason="r", wanted_decision="recut"), None
    )
    assert records(wiring, "wanted_decision")[-1]["tally_reset"] is False


def test_one_hand_edited_row_costs_only_its_own_count(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    path = wanted_decisions_file(wiring.config.state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"recut": 4, "split": "many", "rebase": -1, "": 3}), encoding="utf-8"
    )
    counts, reset = WantedDecisionTally(path).record("recut")
    assert counts == {"recut": 5}
    assert reset is False  # the file was readable; three rows were not


def test_a_long_wanted_verb_is_truncated_and_the_raw_value_is_kept(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    long = "x" * (MAX_WANTED_DECISION_CHARS + 20)
    wiring.orch._record_wanted_decision(
        Directive(decision=Decision.STOP, reason="r", wanted_decision=long), None
    )
    event = records(wiring, "wanted_decision")[-1]
    assert event["wanted"] == "x" * MAX_WANTED_DECISION_CHARS
    assert event["wanted_raw"] == long


def test_the_distinct_verb_count_is_bounded_without_losing_a_count(tmp_path):
    """The value is reviewer-authored free text, so without a bound it is a way
    to grow a file in the loop's state directory one key per round. The fold is
    visible: the totals still add up."""
    path = wanted_decisions_file(tmp_path / "st")
    tally = WantedDecisionTally(path)
    for i in range(MAX_WANTED_DECISION_KINDS):
        tally.record(f"verb{i}")
    counts, _ = tally.record("one-verb-too-many")
    assert "one-verb-too-many" not in counts
    assert counts[WANTED_DECISION_OVERFLOW] == 1
    assert sum(counts.values()) == MAX_WANTED_DECISION_KINDS + 1


def test_verbs_are_folded_by_case_and_whitespace():
    assert WantedDecisionTally.normalise("  ReCut  ") == "recut"
    assert WantedDecisionTally.normalise("split\n  the task") == "split the task"
    assert WantedDecisionTally.normalise("   ") == ""


def test_an_empty_tally_renders_as_nothing():
    assert WantedDecisionTally.render({}) == ""


def test_an_unwritable_state_dir_does_not_take_the_round_down(tmp_path):
    """A counter must never be the thing that kills a round. The occurrence
    still reaches the transcript through the event; only the cumulative file
    misses it."""
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("i am a file\n", encoding="utf-8")
    counts, _ = WantedDecisionTally(blocked / "wanted.json").record("recut")
    assert counts == {"recut": 1}


# ---------------------------------------------------------------------------
# recut: the happy path, end to end
# ---------------------------------------------------------------------------


def test_recut_retires_the_execution_and_returns_the_task_to_the_queue(tmp_path):
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    assert wiring.orch.run() == Phase.STOPPED.value

    # The live record is gone from where the merge window and the next dispatch
    # look for it...
    assert wiring.execution_store.load("t1") is None
    assert not (wiring.tmp_path / "workers" / "t1").exists()
    # ...and the task is selectable again.
    assert wiring.registry.get("t1").status == "pending"
    assert wiring.registry.get("t1").recut_count == 1


def test_the_archived_record_and_quarantined_worker_are_both_there_and_cross_named(
    tmp_path,
):
    """NOTHING IS DELETED, and the two halves of one attempt name each other on
    disk — the property `worktask.retire_execution` exists to guarantee, checked
    here through the reviewer's own verb rather than an operator command."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()

    archived = sorted((wiring.tmp_path / "executions" / "archive").glob("t1-*.json"))
    quarantined = sorted((wiring.tmp_path / "quarantine").glob("t1-*"))
    assert len(archived) == 1 and len(quarantined) == 1
    # One label, both halves, and the label says WHO discarded the round.
    label = archived[0].name[len("t1-"): -len(".json")]
    assert quarantined[0].name == f"t1-{label}"
    assert label.startswith(RECUT_RETIREMENT_REASON)
    # The discarded commit is still reachable inside the quarantined worker.
    record = json.loads(archived[0].read_text(encoding="utf-8"))
    assert record["candidate_sha"]
    assert (quarantined[0] / ".git").exists()


def test_the_transition_is_in_the_transcript_with_the_reviewers_reason(tmp_path):
    wiring = build(
        tmp_path,
        responses=[
            implement_block("t1"),
            recut_block("t1", reason="port-01 has reached a structural dead end"),
            stop_block(),
        ],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()

    events = records(wiring, "task_recut")
    assert len(events) == 1
    event = events[0]
    assert event["task_id"] == "t1"
    assert event["reason"] == "port-01 has reached a structural dead end"
    assert event["discarded_candidate"]
    assert event["recut_count"] == 1 and event["cap"] == MAX_TASK_RECUTS
    assert event["archived_record"].endswith(".json")
    assert event["quarantined_worker"]
    assert event["artifacts_retired"] is True


def test_the_next_dispatch_is_cut_from_the_current_base_and_inherits_nothing(tmp_path):
    """The claim, in the only form that can be checked: the fresh cut's base is
    the CURRENT head (which has moved since the discarded round started), and
    the new candidate's whole range holds only the new round's file. The
    discarded round's path is absent, which is what "an empty diff to start
    from" means once a commit has been made on top of it."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
        per_call_files=[{"docs/A.md": "# first cut"}, {"docs/B.md": "# second cut"}],
    )
    wiring.orch.run()
    first_base = wiring.git.head_sha()

    # The base moves while the task sits in the queue — an ordinary merge.
    (wiring.git.repo_root / "MOVED.md").write_text("moved\n", encoding="utf-8")
    run_git(wiring.git.repo_root, "add", "-A")
    run_git(wiring.git.repo_root, "commit", "-q", "-m", "base moves on")
    moved_base = wiring.git.head_sha()
    assert moved_base != first_base

    second = build(
        tmp_path,
        responses=[implement_block("t1", files=("docs/B.md",)), stop_block()],
        reuse=wiring,
    )
    second.executor.per_call_files = [{"docs/B.md": "# second cut"}]
    second.orch.run()

    execution = second.execution_store.load("t1")
    assert execution is not None
    assert execution.task_base_sha == moved_base
    assert execution.recut_count == 1        # mirrored onto the fresh record
    assert execution.review_round == 1       # a fresh arc, not a resumed one
    assert execution.attempt_count == 1      # and a fresh attempt budget
    # EXACTLY one commit on top of that base. Together with the base assertion
    # above this is the "empty diff to start from" claim in its checkable form:
    # the branch cannot have inherited the discarded round's commit and still
    # hold one commit over the CURRENT head.
    assert execution.candidate_commit_count == 1

    worker_git = GitGateway(
        Path(execution.worktree_path), PolicyEngine(second.config.policy)
    )
    touched = worker_git.commit_range_paths(
        execution.task_base_sha, execution.candidate_sha
    )
    assert touched == {"docs/B.md"}, touched


def test_a_blocked_task_is_exactly_the_one_a_recut_can_reach(tmp_path):
    """The dependency this task was written on top of. A contaminated candidate
    is normally already parked `task_fatal` by the time a recut is warranted —
    port-01 was `blocked` on `attempt_count_ceiling` when its reviewer reached
    that conclusion — so a verb that only accepted `in_progress` would refuse
    precisely when it is needed."""
    wiring = build(
        tmp_path, responses=[implement_block("t1")], tasks=[ready_task("t1")]
    )
    wiring.orch.run(max_steps=4)
    wiring.registry.block("t1", "attempt_count_ceiling")
    wiring.task_store.save(wiring.registry)
    assert wiring.registry.get("t1").status == "blocked"

    wiring.orch.state.last_response = None
    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert wiring.registry.get("t1").status == "pending"
    assert wiring.registry.get("t1").blocked_reason == ""
    assert wiring.execution_store.load("t1") is None


def test_the_recut_count_is_durable_across_the_retirement_it_charges(tmp_path):
    """The count must survive the operation that increments it, and it must
    survive it ON DISK: the execution record it is nominally "enforced on" is
    archived by that very operation."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()

    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.get("t1").recut_count == 1
    raw = json.loads(wiring.config.tasks_file.read_text(encoding="utf-8"))
    row = next(t for t in raw["tasks"] if t["id"] == "t1")
    assert row["recut_count"] == 1


def test_the_report_tells_the_reviewer_what_was_discarded_and_what_is_left(tmp_path):
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()
    # Asserted on what was actually SENT, not on the outbox: the report's job is
    # to reach the reviewer, and a message assembled but never delivered would
    # pass an outbox assertion.
    sent = [prompt for _, prompt in wiring.client.submitted if "RECUT APPLIED" in prompt]
    assert len(sent) == 1
    report = sent[0]
    assert "t1" in report
    assert f"recut 1 of {MAX_TASK_RECUTS}" in report
    assert "quarantine" in report
    assert "CURRENT base" in report


# ---------------------------------------------------------------------------
# recut: the bounds
# ---------------------------------------------------------------------------


def make_candidate(tmp_path, task_id="t1", **task_kwargs):
    """One completed implement round, so there is a real candidate to refuse to
    discard."""
    wiring = build(
        tmp_path,
        responses=[implement_block(task_id)],
        tasks=[ready_task(task_id, **task_kwargs)],
    )
    wiring.orch.run(max_steps=4)
    execution = wiring.execution_store.load(task_id)
    assert execution is not None and execution.candidate_sha
    return wiring, execution


def assert_nothing_discarded(wiring, task_id="t1"):
    assert wiring.execution_store.load(task_id) is not None
    assert (wiring.tmp_path / "workers" / task_id).is_dir()
    assert not (wiring.tmp_path / "quarantine").exists()
    assert records(wiring, "task_recut") == []


def test_a_published_candidate_is_refused_outright(tmp_path):
    """The hardest bound: published work is never discarded by this loop. If it
    is wrong, that is a new task."""
    wiring, execution = make_candidate(tmp_path)
    execution.published_sha = execution.candidate_sha
    execution.published_at = "2026-08-24T10:00:00+00:00"
    execution.intended_remote = "origin"
    execution.intended_remote_ref = "refs/heads/autoloop/t1"
    wiring.execution_store.save(execution)

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert denial_codes(wiring)[-1] == "recut_candidate_published"
    assert_nothing_discarded(wiring)
    assert wiring.registry.get("t1").recut_count == 0


def test_a_candidate_whose_verdict_is_outstanding_is_refused(tmp_path):
    """budget-01's shape, with the reviewer in the operator's seat: t1's
    candidate was presented under one request, the reviewer is answering a
    DIFFERENT one about t2, and an approval naming t1's packet can still arrive
    (`_approval_packet` resolves a packet by the id an approval names). So t1's
    work may already be approved and must not be thrown away."""
    wiring = build(
        tmp_path,
        responses=[
            implement_block("t1"),
            implement_block("t2"),
            recut_block("t1"),
            stop_block(),
        ],
        tasks=[ready_task("t1"), ready_task("t2")],
    )
    wiring.orch.run()

    assert "recut_verdict_outstanding" in denial_codes(wiring)
    assert wiring.execution_store.load("t1") is not None
    assert (wiring.tmp_path / "workers" / "t1").is_dir()
    assert records(wiring, "task_recut") == []


def test_recutting_the_candidate_this_reply_is_about_is_allowed(tmp_path):
    """The positive control for the test above, and the recut's primary use: the
    reviewer answering a packet IS the verdict on the candidate that packet
    presented, so that case is never "outstanding". Without this the outstanding
    check would refuse every recut that matters."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()
    assert "recut_verdict_outstanding" not in denial_codes(wiring)
    assert len(records(wiring, "task_recut")) == 1


def test_a_request_still_in_flight_refuses_the_recut(tmp_path):
    """Defence in depth against a state the ordinary single-request flow does
    not reach: never discard a candidate while this loop is waiting to hear
    about one."""
    from autoloop.state import PendingRequest

    wiring, _ = make_candidate(tmp_path)
    wiring.orch.state.pending_request = PendingRequest(
        request_id="alr-inflight-0001", payload="a review packet"
    )

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert denial_codes(wiring)[-1] == "recut_verdict_outstanding"
    assert_nothing_discarded(wiring)


def test_the_cap_parks_for_a_human_instead_of_cutting_again(tmp_path):
    wiring, execution = make_candidate(tmp_path)
    task = wiring.registry.get("t1")
    task.recut_count = MAX_TASK_RECUTS
    wiring.task_store.save(wiring.registry)

    wiring.orch._dispatch(parse_response(recut_block("t1", reason="one more")))

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.park_kind == "task_fatal"
    parks = records(wiring, "needs_user")
    assert parks[-1]["code"] == "recut_cap"
    assert parks[-1]["task_id"] == "t1"
    # A PARK, not a denial: there is no answer the reviewer could give.
    assert "recut_cap" not in denial_codes(wiring)
    # And nothing was discarded on the way to the park.
    assert_nothing_discarded(wiring)
    assert wiring.registry.get("t1").recut_count == MAX_TASK_RECUTS


def test_the_cap_park_names_the_reviewers_reason_and_the_count(tmp_path):
    wiring, _ = make_candidate(tmp_path)
    wiring.registry.get("t1").recut_count = MAX_TASK_RECUTS
    wiring.orch._dispatch(
        parse_response(recut_block("t1", reason="still contaminated"))
    )
    question = wiring.orch.state.question
    assert "still contaminated" in question
    assert f"cap {MAX_TASK_RECUTS}" in question
    assert "specification problem" in question


def test_a_record_that_lost_its_count_cannot_lower_the_cap(tmp_path):
    """Two copies and `max`, because each survives a failure the other does not.
    Here the REGISTRY row is the one that predates the field (loads as 0) while
    the live record still carries what the last cut seeded."""
    wiring, execution = make_candidate(tmp_path)
    execution.recut_count = MAX_TASK_RECUTS
    wiring.execution_store.save(execution)
    assert wiring.registry.get("t1").recut_count == 0

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert records(wiring, "needs_user")[-1]["code"] == "recut_cap"
    assert_nothing_discarded(wiring)


def test_a_registry_count_alone_still_reaches_the_cap(tmp_path):
    """The other direction: the record is fresh (the ordinary case after a cut,
    since the cut archives the old one) and the registry carries the history."""
    wiring, execution = make_candidate(tmp_path)
    assert execution.recut_count == 0
    wiring.registry.get("t1").recut_count = MAX_TASK_RECUTS

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert records(wiring, "needs_user")[-1]["code"] == "recut_cap"


def test_a_garbled_count_on_the_record_cannot_crash_a_dispatch(tmp_path):
    """`TaskExecution` is rehydrated by `TaskExecution(**data)` with no type
    gate, so a hand-edited record can hold a string. It reads as 0 and the
    registry's own count still stands — never as an exception inside a
    dispatch."""
    wiring, execution = make_candidate(tmp_path)
    path = wiring.execution_store.path_for("t1")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["recut_count"] = "two"
    path.write_text(json.dumps(raw), encoding="utf-8")

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert records(wiring, "task_recut")[-1]["recut_count"] == 1


def test_two_cuts_are_allowed_and_the_third_parks(tmp_path):
    """The cap end to end: N cuts land, N+1 parks. Driven through the real
    dispatch each time, so the count really is charged by the operation.

    Worth knowing before this ever looks flaky: the two cuts may land inside the
    SAME wall-clock second, and `retire_execution` derives its label from the
    reason plus a whole-second stamp — so the second cut's `archive` destination
    collides and it is `release_task_to_pending`'s `<reason>-retry` attempt that
    carries it. Both endings are correct and this test passes under either; which
    one it exercises is a property of the clock, not of the code.
    """
    wiring, _ = make_candidate(tmp_path)
    for expected in range(1, MAX_TASK_RECUTS + 1):
        wiring.orch._dispatch(parse_response(recut_block("t1")))
        assert wiring.registry.get("t1").recut_count == expected
        assert wiring.orch.state.phase == Phase.READY.value
        # Cut the task again so there is something to discard next round.
        wiring.orch.state.last_response = None
        wiring.orch._dispatch(parse_response(implement_block("t1")))

    wiring.orch._dispatch(parse_response(recut_block("t1")))
    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert records(wiring, "needs_user")[-1]["code"] == "recut_cap"
    assert wiring.registry.get("t1").recut_count == MAX_TASK_RECUTS


def test_an_operator_hold_is_never_released_by_a_recut(tmp_path):
    """`status == "blocked"` carries two meanings and they must not be
    reversible by the same route. A hold placed through the inbox is a human
    saying "not this one"; the reviewer may not undo it by naming the task in a
    recut."""
    wiring, _ = make_candidate(tmp_path)
    # The documented operator sequence: `operator_block` refuses an in-progress
    # task and says to release it first. The execution record and worker repo
    # stay exactly where they are, which is what this test then asserts.
    wiring.registry.release("t1")
    wiring.registry.operator_block("t1", "parked by hand")
    wiring.task_store.save(wiring.registry)
    assert wiring.registry.get("t1").hold_origin == HOLD_ORIGIN_OPERATOR

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert denial_codes(wiring)[-1] == "task_operator_hold"
    assert wiring.registry.get("t1").status == "blocked"
    assert wiring.registry.get("t1").hold_origin == HOLD_ORIGIN_OPERATOR
    assert_nothing_discarded(wiring)


def test_a_completed_task_cannot_be_un_finished_by_a_recut(tmp_path):
    wiring, _ = make_candidate(tmp_path)
    wiring.registry.mark_completed("t1")

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert denial_codes(wiring)[-1] == "task_not_in_progress"
    assert wiring.registry.get("t1").status == "completed"
    assert_nothing_discarded(wiring)


def test_a_loop_that_cannot_retire_both_halves_refuses_the_recut(tmp_path):
    """`retire_execution` quarantines only `if worker_repos is not None`, so a
    loop without one would archive the record, report success, and leave the
    contaminated worktree exactly where the next dispatch looks for it — a recut
    that says it discarded the branch and did not."""
    wiring, _ = make_candidate(tmp_path)
    wiring.orch._worker_repos = None

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert denial_codes(wiring)[-1] == "recut_unavailable"
    assert wiring.execution_store.load("t1") is not None
    assert (wiring.tmp_path / "workers" / "t1").is_dir()


def test_an_unknown_task_is_refused(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.orch._dispatch(parse_response(recut_block("nope")))
    assert denial_codes(wiring)[-1] == "task_unknown"


def test_the_audit_pseudo_task_is_refused_by_name(tmp_path):
    """An audit unit is synthetic and is not in the registry, so a lookup would
    answer `task_unknown` and send the reviewer looking for a planning mistake
    that does not exist."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    for task_id in ("audit", "audit-0003"):
        wiring.orch.state.policy_denials = 0
        wiring.orch._dispatch(parse_response(recut_block(task_id)))
        assert denial_codes(wiring)[-1] == "recut_audit_unit"


def test_a_task_with_no_execution_record_is_refused(tmp_path):
    """Its next dispatch is already a fresh cut from the current base, so there
    is nothing for a recut to do — and saying so is better than a no-op that
    charges a cut."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.registry.mark_in_progress("t1")
    wiring.orch._dispatch(parse_response(recut_block("t1")))
    assert denial_codes(wiring)[-1] == "recut_no_execution"
    assert wiring.registry.get("t1").recut_count == 0


def test_an_unreadable_execution_record_is_refused_not_discarded(tmp_path):
    """Fail-closed: a record this cannot parse may name a published candidate or
    one under review, and archiving it unread would destroy the only evidence of
    which."""
    wiring, _ = make_candidate(tmp_path)
    wiring.execution_store.path_for("t1").write_text("{ broken", encoding="utf-8")

    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert denial_codes(wiring)[-1] == "recut_record_unreadable"
    assert (wiring.tmp_path / "workers" / "t1").is_dir()
    assert not (wiring.tmp_path / "quarantine").exists()


def test_a_failed_retirement_parks_instead_of_reporting_success(tmp_path):
    """The status move is durable before the artefacts move, so a retirement
    that cannot finish leaves a pending task whose worker the next dispatch
    would refuse to create over. That is not something a further message to the
    reviewer can fix."""
    wiring, _ = make_candidate(tmp_path)

    def refuse(task_id, label):
        raise OSError("quarantine destination is read-only")

    wiring.worker_repos.quarantine = refuse
    wiring.orch._dispatch(parse_response(recut_block("t1")))

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert records(wiring, "needs_user")[-1]["code"] == "recut_retirement_failed"
    # The task did move — reporting otherwise would be the lie this park exists
    # to avoid.
    assert wiring.registry.get("t1").status == "pending"


def test_the_recut_forgets_the_packets_that_presented_the_discarded_candidate(
    tmp_path,
):
    """A later approval naming the discarded packet must not resolve a binding
    to work that no longer has a record."""
    wiring = build(
        tmp_path,
        responses=[implement_block("t1"), recut_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    wiring.orch.run()
    assert wiring.orch.state.sent_postcommits == []
    assert wiring.orch.state.task_execution is None
    assert wiring.orch.state.current_task is None


# ---------------------------------------------------------------------------
# the registry verb underneath it
# ---------------------------------------------------------------------------


def test_recut_obstacle_is_the_askable_form_of_the_refusal():
    """`orchestrator._dispatch_recut` must be able to say WHY before it performs
    anything destructive — a caller that learned the answer by attempting the
    move would already have charged a cut."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    assert registry.recut_obstacle("t1").code == "task_not_in_progress"
    registry.mark_in_progress("t1")
    assert registry.recut_obstacle("t1") is None


def test_recut_obstacle_raises_for_an_unknown_id():
    """Never answers `None` for a typo — silence would read as "eligible"."""
    registry = TaskRegistry([])
    with pytest.raises(TaskGraphError) as exc:
        registry.recut_obstacle("ghost")
    assert exc.value.code == "task_unknown"


def test_release_is_left_exactly_as_narrow_as_it_was():
    """`TaskRegistry.release` says "Narrow on purpose" and has two callers this
    task has no business changing. The blocked-task admission is `recut`'s
    alone."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.block("t1", "parked")
    with pytest.raises(TaskGraphError) as exc:
        registry.release("t1")
    assert exc.value.code == "task_not_in_progress"
    # ...while recut accepts exactly that task.
    assert registry.recut("t1").status == "pending"


def test_recut_charges_the_cut_in_the_same_object_it_moves():
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.mark_in_progress("t1")
    moved = registry.recut("t1")
    assert moved.status == "pending"
    assert moved.recut_count == 1
    assert registry.get("t1").recut_count == 1


def test_recut_clears_a_quarantines_reason_like_unblock_does():
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.block("t1", "attempt_count_ceiling")
    registry.recut("t1")
    assert registry.get("t1").blocked_reason == ""
    assert registry.get("t1").hold_origin == ""


@pytest.mark.parametrize("status", ["pending", "completed"])
def test_recut_refuses_a_task_with_nothing_in_flight(status):
    registry = TaskRegistry([Task(id="t1", title="T", description="d", status=status)])
    with pytest.raises(TaskGraphError) as exc:
        registry.recut("t1")
    assert exc.value.code == "task_not_in_progress"
    assert registry.get("t1").recut_count == 0


def test_a_stored_recut_count_survives_a_round_trip(tmp_path):
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.mark_in_progress("t1")
    registry.recut("t1")
    store = TaskStore(tmp_path / "tasks.json")
    store.save(registry)
    assert store.load().get("t1").recut_count == 1


def test_a_task_file_written_before_the_field_existed_loads_as_zero(tmp_path):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(TaskRegistry([Task(id="t1", title="T", description="d")]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    for row in raw["tasks"]:
        row.pop("recut_count", None)
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load().get("t1").recut_count == 0


@pytest.mark.parametrize("value", ["two", True, -1, 1.5])
def test_an_unreadable_stored_count_refuses_rather_than_defaulting_to_zero(
    tmp_path, value
):
    """The only bound on a destructive action the reviewer takes by itself.
    Reading a value it cannot trust as 0 would not merely lose information — it
    would hand the whole allowance back."""
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(TaskRegistry([Task(id="t1", title="T", description="d")]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tasks"][0]["recut_count"] = value
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(StateCorruptError):
        store.load()


def test_an_explicit_null_count_loads_as_zero(tmp_path):
    """A hand-edited `null` is the one non-int that is not corruption: it is how
    a row with nothing recorded is spelled, and it must not become `None` on the
    dataclass (every reader compares it as a number)."""
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.save(TaskRegistry([Task(id="t1", title="T", description="d")]))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["tasks"][0]["recut_count"] = None
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert store.load().get("t1").recut_count == 0
