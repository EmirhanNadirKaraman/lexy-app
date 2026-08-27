"""Repeated reviewer `stop` verdicts about ONE unresolved situation park the
loop instead of restarting it forever (stop-01).

The incident these pin, 2026-08-20: a lost postcommit binding left prof-01
holding an approved but unpublishable candidate, the reviewer refused three
times in fifteen minutes (each refusal worded differently, each describing the
same situation), and `health` reported `running` / `open_blockers: 0` /
`needs_attention: FALSE` the whole time — because a `stop` is a VERDICT and no
budget counts verdicts.

Every test here drives the REAL round: a fresh `LoopState` per session (what
`cli._start_new_session` writes), a registry re-read from disk (what
`cli._load_tasks` does per iteration), and a fresh Orchestrator over both (what
`cli._build_orchestrator` does). That matters more than usual: the mechanism
turns on a fingerprint holding still across consecutive rounds, and a test that
compared two hand-built fingerprints would be asserting on its own input rather
than on anything the loop produces.

Self-contained per this codebase's convention (see `test_blockers.py`'s
docstring) — the small `FakeGit`/`FakeClient` doubles are duplicated here rather
than imported from `test_orchestrator.py`.
"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from autoloop import cli, health
from autoloop.blockers import NO_TASK, BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, load_config
from autoloop.contract import Decision, Directive, parse_response
from autoloop.conversation import SubmitResult
from autoloop.errors import ContractError, StateCorruptError, StateError
from autoloop.lock import LoopLock
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import MAX_REPEATED_STOPS, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LoopState,
    Phase,
    StateStore,
    StopRepetition,
    StopRepetitionStore,
    stop_repetition_file,
)
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/stop-livelock-test"


# =============================================================================
# helpers
# =============================================================================


def block(obj) -> str:
    return f"Reasoning...\n```json\n{json.dumps(obj)}\n```"


def stop_block(reason: str = "all done") -> str:
    return block({"version": 3, "decision": "stop", "reason": reason})


#: The three refusals of the incident, verbatim in shape: one situation, three
#: different wordings. A counter keyed on the reason text would see three
#: unrelated stops here; the fingerprint sees one situation three times.
INCIDENT_REASONS = (
    "prof-01 still holds the already-approved unpublished candidate, but this "
    "packet is not a postcommit review packet, so I cannot authorize a push.",
    "This new-session packet cannot authorize its push without first "
    "resurfacing it as a supported postcommit review packet.",
    "The controller is repeatedly starting fresh sessions instead of "
    "presenting the required postcommit review packet needed to publish it.",
)


class FakeClient:
    """Conversation double: answers each `await_response` from a script."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.send_attempted = False
        self.closed = False

    def attach(self):
        pass

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        self.send_attempted = True
        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class FakeGit:
    """Only what a kickoff→stop round touches: `context.build_context` reads
    `head_sha` / `current_branch` / `dirty_files`, and nothing else in this
    path asks git anything."""

    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root)
        self.head = "a" * 40

    def current_branch(self):
        return "feature/x"

    def head_sha(self):
        return self.head

    def dirty_files(self):
        return []

    def dirty_entries(self):
        return []


def make_config(tmp_path: Path) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
    )


def write_config_toml(tmp_path: Path) -> Path:
    """A real `.toml` for the tests that go through CLI command functions
    (`_cmd_answer`), which call `load_config` themselves. Absolute `state_dir`
    and a `workers_root` OUTSIDE it, for the reasons `test_blockers.py`'s
    helper of the same name spells out."""
    path = tmp_path / "config.toml"
    path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    return path


class Loop:
    """The smallest thing that behaves like `cli._run_continuous` for these
    tests: `stop(reason)` runs ONE whole session — fresh `LoopState`, registry
    re-read from disk, fresh Orchestrator — that ends on the reviewer
    answering `stop`, and returns the resulting `LoopState`.

    Deliberately re-reading and re-constructing per round rather than reusing
    one Orchestrator: the claim is about a counter surviving the session
    replacement a stop causes, and reusing objects across rounds would test a
    loop that does not exist.
    """

    def __init__(self, tmp_path: Path, config: AutoloopConfig, tasks=("t1",)):
        self.tmp_path = Path(tmp_path)
        self.config = config
        repo_root = self.tmp_path / "repo"
        repo_root.mkdir(exist_ok=True)
        self.git = FakeGit(repo_root)
        self.store = StateStore(config.state_file)
        self.task_store = TaskStore(config.tasks_file)
        self.task_store.save(
            TaskRegistry(
                [Task(id=t, title=f"Title {t}", description="desc") for t in tasks]
            )
        )
        self.executions = TaskExecutionStore(self.tmp_path / "executions")
        self.blockers = BlockerStore(config.blockers_dir)
        self.ledger = StopRepetitionStore(stop_repetition_file(config.state_dir))
        self.rounds = 0

    def registry(self) -> TaskRegistry:
        return self.task_store.load()

    def _orchestrator(self, state: LoopState, registry: TaskRegistry, responses):
        return Orchestrator(
            config=self.config,
            store=self.store,
            state=state,
            policy=PolicyEngine(self.config.policy),
            git=self.git,
            executor=None,
            transcript=TranscriptLogger(self.config.transcript_file),
            client_factory=lambda: FakeClient(responses),
            registry=registry,
            task_store=self.task_store,
            manifest_store=ManifestStore(self.config.manifests_dir),
            execution_store=self.executions,
            blocker_store=self.blockers,
        )

    def stop(self, reason: str = "all done") -> LoopState:
        """One full session that ends with the reviewer answering `stop`."""
        self.rounds += 1
        state = LoopState.new(URL)          # what `_start_new_session` writes
        state.outbox = f"kickoff round {self.rounds}"
        self.store.save(state)
        orch = self._orchestrator(state, self.registry(), [stop_block(reason)])
        orch.run()
        return orch.state

    def dispatch_stop(self, reason: str) -> LoopState:
        """One session that ends on a `stop` handed straight to `_dispatch`,
        with no conversation turn in front of it.

        For the shapes the CONTRACT cannot deliver: `contract._require_str`
        refuses an empty OR whitespace-only `reason`, so `stop(...)` with one
        never produces a directive at all — it produces a parse error, and the
        round spends a corrective re-prompt asking for a reply no script has.
        Everything from `_dispatch` down is the real path, and it is the same
        entry `test_blockers.py` uses to drive its own stop.
        """
        self.rounds += 1
        state = LoopState.new(URL)
        # The phase `_dispatch` is always reached from, so a park recorded here
        # carries the same blocker `phase` as one reached through `stop`.
        state.phase = Phase.EXECUTING.value
        self.store.save(state)
        orch = self._orchestrator(state, self.registry(), [])
        orch._dispatch(Directive(decision=Decision.STOP, reason=reason))
        return orch.state


def parked_blocker(loop: Loop, state: LoopState):
    assert state.park_blocker_id is not None
    blocker = loop.blockers.load(state.park_blocker_id)
    assert blocker is not None
    return blocker


# =============================================================================
# 1. THE CLAIM: N consecutive stops about one unchanged situation park, with
#    the last reason verbatim
# =============================================================================


def test_three_matching_stops_park_with_the_last_reason_verbatim(tmp_path):
    """The incident, replayed: three refusals, three different wordings, one
    unchanged situation. The first two end the session exactly as a `stop`
    always has; the third parks."""
    loop = Loop(tmp_path, make_config(tmp_path))

    first = loop.stop(INCIDENT_REASONS[0])
    assert first.phase == Phase.STOPPED.value
    assert first.stop_kind == "contract"

    second = loop.stop(INCIDENT_REASONS[1])
    assert second.phase == Phase.STOPPED.value
    assert second.stop_kind == "contract"

    third = loop.stop(INCIDENT_REASONS[2])
    assert third.phase == Phase.NEEDS_USER.value
    assert third.park_kind == "loop_fatal"
    assert third.stop_kind == ""          # a park, not a stop of any kind

    blocker = parked_blocker(loop, third)
    assert blocker.code == "stop_livelock"
    assert blocker.kind == "loop_fatal"
    assert blocker.task_id == NO_TASK
    # VERBATIM: the reviewer's text was the diagnosis in this incident.
    assert INCIDENT_REASONS[2] in blocker.question
    assert blocker.question == third.question
    # And the earlier wordings are NOT what is quoted — the last one is.
    assert INCIDENT_REASONS[0] not in blocker.question


def test_the_park_fires_on_the_stop_that_reaches_the_ceiling(tmp_path):
    """The stated cost is `MAX_REPEATED_STOPS` reviewer turns and packet
    builds, not one more: the park happens ON the Nth stop, so exactly N
    sessions are spent. Pinned against the constant rather than the literal 3,
    so the cost claim in `docs/AUTOLOOP.md` and the constant cannot drift
    apart without this failing."""
    loop = Loop(tmp_path, make_config(tmp_path))

    for _ in range(MAX_REPEATED_STOPS - 1):
        assert loop.stop("same situation").phase == Phase.STOPPED.value

    assert loop.stop("same situation").phase == Phase.NEEDS_USER.value
    assert loop.rounds == MAX_REPEATED_STOPS


def test_needs_attention_is_true_after_the_park(tmp_path):
    """The outcome this task exists to produce. Every automated signal stayed
    GREEN through the incident; `health` must go red off the blocker alone."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    for reason in INCIDENT_REASONS:
        state = loop.stop(reason)

    assert state.phase == Phase.NEEDS_USER.value
    verdict = health.check(config, agent_probe=lambda: False)
    assert verdict.needs_attention is True
    assert verdict.code == health.STUCK_BLOCKED
    assert verdict.open_blockers == 1
    assert "stop_livelock" in verdict.detail


def test_the_park_ends_continuous_mode_instead_of_opening_another_session(
    tmp_path, capsys
):
    """The other half of the claim, and the half a blocker record alone does
    not prove: the loop must stop OPENING SESSIONS, not merely complain while
    it keeps going.

    Driven through the real `cli._run_continuous` over the state the park
    actually wrote — no hand-built park — because that is the function whose
    reaction to a `stop` was the bug. It exits 2 through the ordinary
    `loop_fatal` branch, `_select_and_kickoff` is never reached, and the parked
    session is still on disk afterwards rather than replaced by a fresh one.
    """
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    for reason in INCIDENT_REASONS:
        parked = loop.stop(reason)
    assert parked.phase == Phase.NEEDS_USER.value

    args = Namespace(
        config=tmp_path / "unused.toml",
        continuous=True,
        kickoff=None,
        kickoff_audit=False,
        answer=None,
        retry=False,
        resubmit=False,
        max_steps=None,
        null_executor=True,
    )
    assert cli._run_continuous(args, config) == 2

    after = StateStore(config.state_file).load()
    assert after.session_id == parked.session_id   # NOT a fourth session
    assert after.phase == Phase.NEEDS_USER.value
    assert "loop_fatal" in capsys.readouterr().out
    # t1 was never at fault and is not quarantined — this park is about the
    # controller, which is why it is loop_fatal and names no task.
    assert loop.registry().next_ready().id == "t1"


# =============================================================================
# 2. A SINGLE STOP IS UNCHANGED
# =============================================================================


def test_a_single_stop_behaves_exactly_as_today(tmp_path):
    """One `stop` still means "a human should decide" and still ends the
    session as a clean boundary. Nothing about this may change: bounding the
    repetition is not bounding stopping."""
    loop = Loop(tmp_path, make_config(tmp_path))

    state = loop.stop("the roadmap is finished for tonight")

    assert state.phase == Phase.STOPPED.value
    assert state.stop_kind == "contract"
    assert state.stop_reason == "the roadmap is finished for tonight"
    assert state.question is None
    assert state.park_kind is None
    assert state.park_blocker_id is None
    assert loop.blockers.open_blockers() == []
    # Under the lock, so `health` judges a RUNNING loop rather than reporting
    # "not running" about the test process — the claim here is that nothing
    # about a lone stop asks for attention.
    with LoopLock(loop.config.state_dir):
        verdict = health.check(loop.config, agent_probe=lambda: False)
    assert verdict.needs_attention is False


# =============================================================================
# 3. WHAT "THE SAME SITUATION" MEANS — the fingerprint holds still across real
#    rounds, and moves when the situation does
# =============================================================================


def test_the_fingerprint_is_identical_across_three_real_rounds(tmp_path):
    """The load-bearing fact underneath every test above, measured on what the
    ORCHESTRATOR computed rather than on a fingerprint built here.

    If any part of a round quietly rewrote the registry or an execution record
    — an adopted priority, a drained inbox entry, a normalised field, a
    timestamp — the digest would differ each time, the count would reset every
    round and the park would never fire, with nothing saying so. That is the
    fail-open this whole mechanism exists to avoid, so it is checked directly.

    Run WITH A LIVE EXECUTION RECORD, which is the incident's own shape and not
    a decoration: prof-01 held a committed, approved candidate throughout, that
    record is where the task identity actually lived, and its whole `asdict` is
    digested by raw bytes. An empty store would prove the digest is stable when
    there is nothing to churn — which is precisely the case this task is NOT
    about. `test_an_empty_execution_store_is_stable_too` keeps the other half.

    The third round's digest is read off the blocker's `detail`, since the park
    clears the ledger — which is also what makes that field worth carrying."""
    loop = Loop(tmp_path, make_config(tmp_path))
    loop.executions.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path=str(tmp_path / "worktrees" / "t1"),
            task_base_sha="b" * 40,
            candidate_sha="c" * 40,
            candidate_commit_count=1,
            review_round=1,
            intended_remote="origin",
            intended_remote_ref="refs/heads/autoloop/t1",
        )
    )

    seen = []
    for n, reason in enumerate(INCIDENT_REASONS[:2], start=1):
        loop.stop(reason)
        record = loop.ledger.load()
        assert record.count == n, "consecutive stops must accumulate"
        seen.append(record.fingerprint)

    parked = parked_blocker(loop, loop.stop(INCIDENT_REASONS[2]))
    assert f"situation fingerprint {seen[0]}" in parked.detail
    assert f"consecutive stops: {MAX_REPEATED_STOPS}" in parked.detail
    assert len(set(seen)) == 1, f"fingerprint churned across rounds: {seen}"


def test_an_empty_execution_store_is_stable_too(tmp_path):
    """The other half of the test above: with no execution record at all — a
    loop stopped between tasks — the digest must still hold still, so the
    executions term contributes a stable empty value rather than something that
    varies with an absent directory."""
    loop = Loop(tmp_path, make_config(tmp_path))

    loop.stop(INCIDENT_REASONS[0])
    first = loop.ledger.load().fingerprint
    loop.stop(INCIDENT_REASONS[1])
    second = loop.ledger.load()

    assert second.count == 2
    assert second.fingerprint == first


def test_the_registry_term_is_a_fixed_point_across_a_load_and_dump():
    """The registry term is `to_dict()` of a registry that came back through
    `from_dict`, and `from_dict` COERCES (`depends_on` to a tuple, `validation`
    to tuples of tuples, `hold_origin` through `str(... or "")`). If that round
    trip were not a fixed point, the digest would differ between the round that
    saved and every round that loaded — a churn no amount of "nothing wrote the
    file" reasoning would catch. Asserted with the coerced fields actually
    populated, since an empty tuple round-trips trivially."""
    registry = TaskRegistry(
        [
            Task(id="a", title="A", description="d", approved_paths=("docs/X.md",),
                 validation=(("ruff", "check", "."), ("pytest",))),
            Task(id="b", title="B", description="d", depends_on=("a",),
                 approved_paths=("autoloop/", "docs/Y.md")),
        ]
    )
    once = registry.to_dict()

    assert TaskRegistry.from_dict(once).to_dict() == once


def test_the_task_identity_term_actually_carries_the_current_task(tmp_path):
    """A term spelled wrong contributes nothing and looks exactly like a term
    that is working. `state.current_task` is keyed `task_id` — `id` is the
    `Task` dataclass's field name and never reaches this dict — so reading the
    wrong key would leave two stops about two DIFFERENT tasks with identical
    digests and nothing to show for it. Asserted directly rather than through a
    round, because the incident's own shape has no current task at all."""
    loop = Loop(tmp_path, make_config(tmp_path))
    state = LoopState.new(URL)
    orch = loop._orchestrator(state, loop.registry(), [])

    without = orch._stop_situation_fingerprint()
    state.current_task = {"task_id": "t1", "title": "Title t1", "decision": "implement"}
    with_t1 = orch._stop_situation_fingerprint()
    state.current_task = {"task_id": "t2", "title": "Title t2", "decision": "implement"}
    with_t2 = orch._stop_situation_fingerprint()

    assert len({without, with_t1, with_t2}) == 3
    # A shape this term cannot read must not raise — it reads as "no task",
    # which is the same value the incident's own kickoff stops carried.
    state.current_task = {"title": "no id here"}
    assert orch._stop_situation_fingerprint() == without


def test_stops_about_genuinely_different_situations_never_park(tmp_path):
    """Two stops for two different reasons are not a livelock — and the thing
    that makes them different is the SITUATION, not the wording. Here the
    registry moves between every stop (a new task planned each round), which is
    real progress, so five stops in a row still never park."""
    loop = Loop(tmp_path, make_config(tmp_path))

    for n in range(5):
        registry = loop.registry()
        registry.add(Task(id=f"new-{n}", title="fresh work", description="desc"))
        loop.task_store.save(registry)

        state = loop.stop(f"stopping about something new: {n}")
        assert state.phase == Phase.STOPPED.value, f"parked on round {n}"
        assert loop.ledger.load().count == 1

    assert loop.blockers.open_blockers() == []


def test_the_same_reason_text_is_not_what_makes_a_situation_the_same(tmp_path):
    """The converse of the test above, and the reason the fingerprint ignores
    reason text: byte-identical wording across a CHANGING situation is not a
    livelock either."""
    loop = Loop(tmp_path, make_config(tmp_path))

    for n in range(4):
        registry = loop.registry()
        registry.add(Task(id=f"new-{n}", title="fresh work", description="desc"))
        loop.task_store.save(registry)
        assert loop.stop("word for word the same refusal").phase == Phase.STOPPED.value

    assert loop.blockers.open_blockers() == []


# =============================================================================
# 4. RESET ON PROGRESS
# =============================================================================


def test_progress_between_two_stops_resets_the_count(tmp_path):
    """Anything that changes the situation clears the counter. Here it is a new
    task-execution record — the shape of "the loop actually started working on
    something" — and after it the loop is back to needing a full
    `MAX_REPEATED_STOPS` matching stops."""
    loop = Loop(tmp_path, make_config(tmp_path))

    loop.stop("stuck")
    loop.stop("stuck")
    assert loop.ledger.load().count == 2

    loop.executions.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path=str(tmp_path / "worktrees" / "t1"),
            task_base_sha="b" * 40,
        )
    )

    state = loop.stop("stuck")
    assert state.phase == Phase.STOPPED.value, "progress must clear the count"
    assert loop.ledger.load().count == 1

    # And the mechanism still works afterwards — reset is not disablement.
    assert loop.stop("stuck").phase == Phase.STOPPED.value
    assert loop.stop("stuck").phase == Phase.NEEDS_USER.value


def test_publishing_a_candidate_resets_the_count(tmp_path):
    """`published_sha` lives inside the execution record, so "no candidate
    published" needs no separate term in the fingerprint — this is the test
    that says so rather than the docstring alone."""
    loop = Loop(tmp_path, make_config(tmp_path))
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path=str(tmp_path / "worktrees" / "t1"),
        task_base_sha="b" * 40,
        candidate_sha="c" * 40,
    )
    loop.executions.save(execution)

    loop.stop("cannot publish this")
    loop.stop("cannot publish this")
    assert loop.ledger.load().count == 2

    execution.published_sha = "c" * 40
    execution.published_at = "2026-08-23T12:00:00+00:00"
    loop.executions.save(execution)

    assert loop.stop("cannot publish this").phase == Phase.STOPPED.value
    assert loop.ledger.load().count == 1


def test_completing_a_task_resets_the_count(tmp_path):
    """The registry half of the same rule."""
    loop = Loop(tmp_path, make_config(tmp_path))

    loop.stop("stuck")
    loop.stop("stuck")

    registry = loop.registry()
    registry.mark_completed("t1")
    loop.task_store.save(registry)

    assert loop.stop("stuck").phase == Phase.STOPPED.value
    assert loop.ledger.load().count == 1


def test_the_park_clears_the_ledger_so_a_relapse_costs_the_same_bound(tmp_path):
    """Left at the ceiling, the very next stop after an operator answers would
    park again immediately — a new livelock wearing the old one's clothes. The
    park clears the counter, and the blocker's `recurrences` is what records
    that this has happened before."""
    loop = Loop(tmp_path, make_config(tmp_path))

    for _ in range(MAX_REPEATED_STOPS):
        state = loop.stop("stuck")
    assert state.phase == Phase.NEEDS_USER.value
    assert loop.ledger.load() is None
    first_blocker = parked_blocker(loop, state)
    assert first_blocker.recurrences == 1

    for _ in range(MAX_REPEATED_STOPS - 1):
        assert loop.stop("stuck").phase == Phase.STOPPED.value
    state = loop.stop("stuck")
    assert state.phase == Phase.NEEDS_USER.value

    # ONE record, seen twice — `BlockerStore.record` matches on (task, code,
    # phase). `task_id` and `code` are constants of this park, and `phase` is
    # `executing` BOTH times because `Loop.stop` starts each round from a fresh
    # `LoopState` exactly as `cli._start_new_session` does; a park reached from
    # some other phase would rightly be a second record.
    assert len(loop.blockers.open_blockers()) == 1
    assert parked_blocker(loop, state).recurrences == 2


# =============================================================================
# 5. THE PARK IS REACHABLE THROUGH THE NORMAL BLOCKER MACHINERY
# =============================================================================


def test_answer_clears_the_park_through_the_ordinary_command(tmp_path, capsys):
    """`python -m autoloop answer <id> "..."`, unmodified. `stop_livelock`
    carries no `_RESOLUTION_PRECONDITIONS` entry on purpose: the condition is
    the reviewer and the controller disagreeing, and there is no local recheck
    that could establish it has been fixed — the operator's answer IS the
    resolution."""
    config_path = write_config_toml(tmp_path)
    config = load_config(config_path)
    loop = Loop(tmp_path, config)

    for reason in INCIDENT_REASONS:
        state = loop.stop(reason)
    blocker = parked_blocker(loop, state)
    assert loop.blockers.open_blockers()

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="rebound prof-01")
    ) == 0

    assert loop.blockers.open_blockers() == []
    closed = loop.blockers.load(blocker.id)
    assert closed.resolved_at is not None
    assert closed.answer == "rebound prof-01"
    # The BLOCKER no longer drives the verdict. The session itself is still
    # parked until the operator resumes it (`run --answer`), which is exactly
    # how every other loop_fatal park recovers — answering a record is not
    # secretly a restart.
    verdict = health.check(config, agent_probe=lambda: False)
    assert verdict.code == health.STUCK_PARKED
    assert verdict.open_blockers == 0
    out = capsys.readouterr().out
    assert f"blocker {blocker.id} resolved." in out


def test_the_blocker_lists_and_survives_the_session(tmp_path):
    """A park clears nothing durable, and the record outlives the state file —
    the property `blockers.py` exists for, checked on this code."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    for reason in INCIDENT_REASONS:
        state = loop.stop(reason)

    StateStore(config.state_file).path.unlink()
    open_now = BlockerStore(config.blockers_dir).open_blockers()

    assert [b.code for b in open_now] == ["stop_livelock"]
    assert INCIDENT_REASONS[2] in open_now[0].question
    assert open_now[0].session_id == state.session_id


# =============================================================================
# 6. ADVERSARIAL: the ways this could quietly fail to fire
# =============================================================================


def test_an_unusable_ledger_parks_rather_than_disabling_the_check(tmp_path):
    """The fail-open that would matter most: a ledger that cannot be read
    counts nothing, so every stop would look like the first one, the park would
    never fire and no signal would say the detector had stopped working. It
    parks instead, naming the file — the counter is disposable, and saying so
    is what keeps the operator from answering a blocker they cannot clear."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    ledger_path = stop_repetition_file(config.state_dir)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text("{ not json at all", encoding="utf-8")

    state = loop.stop("a perfectly ordinary first stop")

    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "loop_fatal"
    blocker = parked_blocker(loop, state)
    assert blocker.code == "stop_repetition_ledger_unusable"
    assert str(ledger_path) in blocker.question
    assert health.check(config, agent_probe=lambda: False).needs_attention is True


def test_a_ledger_that_is_a_directory_parks_too(tmp_path):
    """The OSError half of the same guard. A directory sitting where the file
    belongs is neither readable nor writable as a record, and it raises
    `OSError` rather than `StateError` — a catch that named only the latter
    would let this one through as "counted"."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    stop_repetition_file(config.state_dir).mkdir(parents=True)

    state = loop.stop("first stop")

    assert state.phase == Phase.NEEDS_USER.value
    assert parked_blocker(loop, state).code == "stop_repetition_ledger_unusable"


@pytest.mark.parametrize("reason", ["", "   \n  "], ids=["empty", "blank"])
def test_a_stop_with_an_empty_reason_still_parks_and_says_so(tmp_path, reason):
    """A reason with nothing readable in it must not make the park's question
    unreadable, and must certainly not skip the park: three unchanged
    empty-reason stops park on the third exactly like three worded ones.

    Both shapes, because they fail differently. `""` would leave the question
    ending on a colon; `"   "` is TRUTHY, so a bare `or` fallback would print
    the whitespace and produce the same unreadable line while looking handled —
    that is why `_park_stop_livelock` strips before falling back.

    Dispatched directly because the contract cannot produce either shape —
    the test below pins that — so a scripted `stop_block("")` would spend the
    round on a parse retry rather than on a stop. That makes
    `"(no reason recorded)"` a guard on the NON-contract construction sites
    (a directive handed to `_dispatch`, as here and in `test_blockers.py`), and
    those are the only ones that can reach it: `_park_stop_livelock` reads
    `record.last_reason`, and `StopRepetitionStore.observe` overwrites that with
    the incoming reason on BOTH of its branches, so a stale empty value already
    on disk cannot survive into the park either.
    """
    loop = Loop(tmp_path, make_config(tmp_path))

    for _ in range(MAX_REPEATED_STOPS - 1):
        earlier = loop.dispatch_stop(reason)
        assert earlier.phase == Phase.STOPPED.value
        assert earlier.stop_kind == "contract"
    state = loop.dispatch_stop(reason)

    assert state.phase == Phase.NEEDS_USER.value
    assert loop.rounds == MAX_REPEATED_STOPS   # no extra round was spent
    blocker = parked_blocker(loop, state)
    assert blocker.code == "stop_livelock"
    assert "(no reason recorded)" in blocker.question
    assert blocker.question == state.question


def test_the_contract_never_delivers_an_empty_stop_reason():
    """Why the test above dispatches directly instead of scripting a reply.

    `contract._require_str` refuses an empty `reason` for every decision, so a
    `stop` carrying one never becomes a `Directive`: it is a parse error, the
    round answers with a corrective re-prompt, and no stop is dispatched or
    counted. Pinned rather than assumed, because it is the whole justification
    for the shape of the test above — if the contract ever starts accepting a
    terse `stop`, this fails, and that one should be rewritten as a full round.
    """
    with pytest.raises(ContractError) as caught:
        parse_response(block({"version": 3, "decision": "stop", "reason": ""}))

    assert caught.value.code == "missing_field:reason"


def test_every_orchestrator_wires_the_ledger_without_being_asked(tmp_path):
    """The guard is derived in `__init__` from `config.state_dir` rather than
    passed in, so no construction site can leave it out. If this ever becomes a
    constructor parameter again, this is the test that notices."""
    loop = Loop(tmp_path, make_config(tmp_path))
    state = LoopState.new(URL)
    orch = loop._orchestrator(state, loop.registry(), [])

    assert orch._stop_repetitions is not None
    assert orch._stop_repetitions.path == stop_repetition_file(loop.config.state_dir)


def test_a_park_without_a_blocker_store_still_parks(tmp_path):
    """`_blocker_store` is `None` in hand-built Orchestrators. That must cost
    the RECORD, never the park: a loop that kept restarting because nobody
    wired a store would be the original bug with an excuse."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    loop.blockers = None

    for _ in range(MAX_REPEATED_STOPS):
        state = loop.stop("stuck")

    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "loop_fatal"
    assert state.park_blocker_id is None        # no store, so no record
    assert "stuck" in (state.question or "")    # the park still says everything


# =============================================================================
# 7. The ledger itself
# =============================================================================


def test_the_ledger_counts_by_fingerprint_not_by_reason(tmp_path):
    """`StopRepetitionStore.observe` in isolation: same digest accumulates and
    keeps `first_seen_at`; a different digest starts over."""
    ledger = StopRepetitionStore(tmp_path / "ledger.json")

    one = ledger.observe(fingerprint="abc", reason="first", session_id="s1", now="t1")
    two = ledger.observe(fingerprint="abc", reason="second", session_id="s2", now="t2")
    other = ledger.observe(fingerprint="xyz", reason="third", session_id="s3", now="t3")

    assert (one.count, two.count, other.count) == (1, 2, 1)
    assert two.first_seen_at == "t1" and two.last_seen_at == "t2"
    assert two.last_reason == "second"
    assert other.first_seen_at == "t3"


def test_the_ledger_raises_on_a_corrupt_record_rather_than_reading_as_absent(tmp_path):
    """Same rule as every other store in this package. Reading corruption as
    "nothing recorded" is the one answer that cannot be walked back here: it
    silently restarts the count forever.

    `StateCorruptError` subclasses `StateError`, which is why
    `_observe_contract_stop`'s `except (StateError, OSError)` catches this
    without naming it."""
    path = tmp_path / "ledger.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(StateError):
        StopRepetitionStore(path).load()
    with pytest.raises(StateCorruptError):
        StopRepetitionStore(path).load()


def test_an_absent_ledger_reads_as_absent(tmp_path):
    """The ordinary first-ever stop: no file, no record, no error."""
    assert StopRepetitionStore(tmp_path / "nothing.json").load() is None


@pytest.mark.parametrize(
    "payload",
    [
        {"fingerprint": "f", "count": "3", "last_reason": "r",
         "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"},
        {"fingerprint": "f", "count": True, "last_reason": "r",
         "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"},
        {"fingerprint": "f", "count": 0, "last_reason": "r",
         "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"},
        {"fingerprint": "f", "count": -1000, "last_reason": "r",
         "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"},
        {"fingerprint": 7, "count": 1, "last_reason": "r",
         "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"},
        {"fingerprint": "f", "count": 1, "last_reason": None,
         "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"},
    ],
    ids=["str-count", "bool-count", "zero", "negative", "int-fingerprint",
         "null-reason"],
)
def test_a_wrongly_typed_ledger_is_refused_rather_than_used(tmp_path, payload):
    """Shapes no writer produces, each with a distinct way of going wrong if
    it were merely unpacked. `"3"` and `True` both reach a `>= 3` comparison —
    the first raises `TypeError` from inside `_dispatch`, where NOTHING in
    `Orchestrator.run`'s type-keyed handler chain catches it, and the second
    answers False forever. `0` and `-1000` are plausible-looking numbers that
    would postpone the park silently. All are refused at the boundary."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StateCorruptError):
        StopRepetitionStore(path).load()


def test_a_wrongly_typed_ledger_parks_the_loop_instead_of_killing_it(tmp_path):
    """The reason the check above lives in `load()`. Through the real round, a
    ledger whose `count` is a string must produce a PARK with a blocker — not a
    traceback out of `run()`, which is the "vanished with no record" shape
    `Orchestrator.run`'s `StateError` clause was added to end."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    ledger_path = stop_repetition_file(config.state_dir)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {"fingerprint": "f", "count": "3", "last_reason": "r",
             "first_seen_at": "t", "last_seen_at": "t", "last_session_id": "s"}
        ),
        encoding="utf-8",
    )

    state = loop.stop("an ordinary stop against a hand-edited counter")

    assert state.phase == Phase.NEEDS_USER.value
    assert parked_blocker(loop, state).code == "stop_repetition_ledger_unusable"


def test_the_ledger_round_trips(tmp_path):
    path = tmp_path / "ledger.json"
    store = StopRepetitionStore(path)
    store.save(
        StopRepetition(
            fingerprint="f",
            count=2,
            last_reason="because",
            first_seen_at="t1",
            last_seen_at="t2",
            last_session_id="s",
        )
    )

    assert store.load().count == 2
    store.clear()
    assert store.load() is None
    store.clear()  # idempotent — a park may clear an already-absent ledger


def test_the_status_summary_shows_a_climbing_count(tmp_path):
    """Visibility before the park is paid for: the incident was caught by a
    person noticing the phase had not changed, and this is the line that would
    have said so."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    state = loop.stop("stuck")

    summary = cli._summary(config, state, loop.registry())

    assert "repeat stops 1 consecutive stop(s)" in summary
    assert f"parks at {MAX_REPEATED_STOPS}" in summary


def test_the_status_summary_is_silent_with_nothing_counted(tmp_path):
    """No stop yet, and an unreadable counter, both print nothing: a status
    LINE must not fail the whole summary, unlike the enforcement point."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)

    assert cli._repeated_stop_display(config) == ""
    stop_repetition_file(config.state_dir).parent.mkdir(parents=True, exist_ok=True)
    stop_repetition_file(config.state_dir).write_text("nonsense", encoding="utf-8")
    assert cli._repeated_stop_display(config) == ""
    assert "repeat stops" not in cli._summary(
        config, LoopState.new(URL), loop.registry()
    )


def test_the_lock_is_not_needed_to_read_the_ledger(tmp_path):
    """`_summary` is called from `status`, which runs against a LIVE loop.
    Reading the ledger must not take the loop lock or this becomes a hang."""
    config = make_config(tmp_path)
    loop = Loop(tmp_path, config)
    loop.stop("stuck")

    with LoopLock(config.state_dir):
        assert "repeat stops" in cli._repeated_stop_display(config)
