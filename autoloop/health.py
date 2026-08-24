"""Is the loop working, or does it need a human?

A monitor is only as good as its false-alarm rate. An alert that fires while
the loop is happily running teaches you to ignore alerts, and then the one
that matters is ignored too. So every signal here is chosen to distinguish
"quiet because it is working" from "quiet because it is stuck", and the
default thresholds are deliberately generous.

Signals, and why these rather than the obvious ones:

* **The lock, not a process name.** `LoopLock.is_live` is boot-aware and
  authoritative. Matching process names is what it looks like you should do
  and it is wrong twice over: the loop runs as `autoloop start` OR
  `autoloop run` depending on how it was launched, and `pgrep -fc` counts
  PATTERNS, not processes. Both mistakes were made against this very loop on
  2026-08-02 and both produced a confident wrong answer.

* **Transcript age, not `state.json` mtime.** State is written at phase
  TRANSITIONS, so a healthy loop mid-`executing` can leave it untouched for
  twenty minutes. Reading its mtime as liveness reports a working loop as
  dead — also made, also confidently wrong.

* **A live agent suppresses the silence alarm.** An audit fan-out runs six
  subagents for fifteen-plus minutes and writes nothing to the transcript
  while it does. That is the single most likely false alarm, so an agent
  process being alive is treated as proof of work even when everything else
  is quiet.

* **A task stranded by an environment fault is a fault the transcript is
  silent about.** Every other signal here asks about the LOOP; this one asks
  about the queue behind it. A round destroyed by the environment leaves its
  task `in_progress`, which `next_ready()` never returns, so the task stops
  being scheduled with no symptom other than its own absence — three sat that
  way for twenty-one hours on 2026-08-22 while the loop worked perfectly on
  other tasks and reported `running` throughout. `stranded_fault_rounds` is
  the predicate; it is carried on EVERY verdict (`Health.stranded_tasks`)
  rather than being a code of its own, because the states it co-occurs with
  — not running, blocked, a stale lock — all return before any late check
  could fire, which would make it decoration.

* **Silence is awake time, not wall-clock.** A laptop that sleeps for hours
  is indistinguishable from a hung loop if silence is measured on the wall
  clock — observed 2026-08-05: "no activity for 224 minutes" over a machine
  that had been asleep, with the loop writing again 69 seconds after wake.
  This is the SAME wrong assumption `lock.boot_time_epoch` already corrects
  for locks (kern.boottime / /proc/stat btime, deliberately never a
  monotonic clock — macOS stops the monotonic clock during sleep, the exact
  case in question), so the same evidence family is used here — and
  `boot_time_epoch` itself is the shared boundary: wake history read from a
  running kernel describes THIS boot only, so any part of the window before
  boot (an off or rebooting machine) can no more accuse the loop than sleep
  can. Within the boot, kern.sleeptime/kern.waketime on darwin and
  CLOCK_BOOTTIME−CLOCK_MONOTONIC on linux fill in the sleep. Only time the
  machine PROVABLY spent awake counts against the loop: proven sleep and
  any stretch the history cannot vouch for (before boot, or before darwin's
  last recorded sleep — those sysctls carry exactly one pair) are
  discounted alike, and when wake history cannot be established at all the
  check FAILS TOWARD QUIET on this one axis — a false "stuck" trains a
  human to ignore the monitor, while a missed detection is retried by the
  next check minutes later.

Verdicts are advisory. Nothing here writes, locks, or touches the loop's
state — it is safe to run on any schedule, including while the loop is
mid-round.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .blockers import BlockerStore
from .errors import StateError, TaskGraphError
from .lock import LoopLock, boot_time_epoch
from .state import Phase, StateStore
from .tasks import TaskStore
from .worktask import (
    ATTEMPT_FAULT,
    ATTEMPT_OPEN,
    REASON_SENT_FOR_REVIEW,
    TaskExecutionStore,
    attempt_outcome,
    split_attempt,
)

#: How long a live loop may write nothing before it is called stuck. Generous
#: on purpose: an audit fan-out is quiet for fifteen-plus minutes, and a
#: review round can wait on a human-speed reviewer. Tightening this trades a
#: faster alert for false alarms, which is the wrong trade for a monitor.
#: Do NOT raise it to accommodate laptop sleep either — sleep is discounted
#: from the measured silence instead (`machine_sleep_in_window`); a bigger
#: threshold would only make a genuinely hung loop slower to surface.
DEFAULT_SILENCE_MINUTES = 45.0

#: Verdict codes. `needs_attention` is what a scheduler acts on.
OK_RUNNING = "running"
OK_PAUSED = "paused"
OK_IDLE = "idle"
STUCK_BLOCKED = "blocked"
STUCK_PARKED = "parked"
STUCK_FAILED = "failed"
STUCK_STALE_LOCK = "stale_lock"
STUCK_SILENT = "silent"
STUCK_NOT_RUNNING = "not_running"
#: A task is `in_progress` with nothing scheduling it and no open blocker
#: saying why — see `stranded_fault_rounds`. Returned only when NOTHING ELSE
#: needs attention; when something does, that verdict keeps its own code and
#: carries the strand in `Health.stranded_tasks` and its detail instead.
STUCK_STRANDED = "stranded"


@dataclass(frozen=True)
class Health:
    code: str
    needs_attention: bool
    summary: str
    detail: str = ""
    phase: str = ""
    open_blockers: int = 0
    silent_minutes: float | None = None
    #: Ids of tasks left `in_progress` by a round the environment destroyed
    #: (`stranded_fault_rounds`). Carried on EVERY verdict, including the ones
    #: that need attention for another reason: the 2026-08-22 incident's most
    #: likely health states were `not_running` and `blocked`, both of which
    #: return before any late check could run, so a code of its own would never
    #: have fired when it mattered. Empty is the ordinary case and the field
    #: has a default, so a caller reading `asdict` gains a key and loses
    #: nothing.
    stranded_tasks: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass(frozen=True)
class StrandedRound:
    """One task left `in_progress` by a round the environment destroyed.

    The unit `stranded_fault_rounds` returns, and the whole of what the two
    readers need: `health.check` NAMES these tasks, and
    `orchestrator.Orchestrator._reconcile_stranded_tasks` either returns one to
    the queue or files a blocker saying why it did not.

    `obstacle` is the split between those two answers, and it is a STRING
    rather than a boolean so the blocker can quote it. Empty means the record
    is in the narrow shape that is provably safe to hand back to scheduling —
    no candidate, no review round — and anything else names what stopped that.
    """

    task_id: str
    fault_code: str
    candidate_sha: str = ""
    review_round: int = 0
    attempt_count: int = 0
    fault_attempt_count: int = 0
    obstacle: str = ""

    @property
    def safe_to_requeue(self) -> bool:
        """`candidate_sha` empty AND `review_round == 0`, and nothing else.

        Deliberately derived from `obstacle` rather than re-testing the fields:
        one place decides, so a caller cannot get "safe" and "no obstacle to
        report" from two different rules. The BUDGET is not part of it — a
        record whose fault allowance is spent is safe in this sense and still
        must not be requeued, which is the caller's own gate (it owns the
        ceilings).
        """
        return not self.obstacle


#: The fault code reported for a round that never stamped its own exit — the
#: same slug `orchestrator._reconcile_unfinished_attempts` will settle it under
#: when the task is dispatched again, so the transcript reads consistently
#: whichever half of the loop names it first.
FAULT_INTERRUPTED = "interrupted_mid_round"

#: Reported instead of a fault code when the execution record itself cannot be
#: read. NOT treated as "no fault round": a record that fails to decode is the
#: one state in which we cannot tell, and reading "cannot tell" as "nothing
#: happened" is exactly how a task goes missing quietly.
FAULT_UNREADABLE_RECORD = "execution_record_unreadable"


def stranded_fault_rounds(
    registry, execution_store, current_task_id: str = ""
) -> tuple[StrandedRound, ...]:
    """Every task the loop has left `in_progress` after a round the ENVIRONMENT
    destroyed, in registry order.

    THE predicate, and there is deliberately one of it for two very different
    users: `check` below only reports these, while `orchestrator._reconcile_
    stranded_tasks` acts on them. A second implementation would eventually
    disagree about which tasks are stranded, and then the monitor would name a
    task the loop had already returned to the queue (or, worse, stay quiet
    about one it had not).

    It lives HERE, in the detection module, rather than in `worktask` beside the
    record it reads: this is a judgement about the loop's health made from
    several stores at once, and `worktask` is deliberately a persistence module
    with no opinions. Nothing in this file writes, so the orchestrator importing
    it cannot import a mutation by accident.

    **What counts as stranded — four conditions, all required.**

    1. The registry's STORED status is `in_progress` (`TaskRegistry.
       in_progress_tasks`). That is the state `next_ready()` never returns.
    2. It is NOT `current_task_id` — the task the loop's own session says it is
       working (`LoopState.task_execution`). This is the whole of the "the loop
       moved on" evidence, and it is what makes this safe to run every round: a
       round that has just faulted is still the current task while its report
       goes to the reviewer, so the reviewer keeps its redo (that is how
       quota-01 and dash-18 recovered on their own in the incident). Only once
       the loop has dispatched something ELSE is the task demonstrably
       abandoned.
    3. Its execution record's LAST attempt reads as a round the environment
       took: settled on the fault budget with an outcome that is not
       `sent_for_review`, or still OPEN (nothing ever stamped it, which
       `_reconcile_unfinished_attempts` defines as environmental). A round that
       reached a reviewer is not stranded — its next move belongs to the
       reviewer — and a round that failed on the TASK's own merits is charged to
       the task budget and reads `task`, so it is not swept either.
    4. The record does not carry a `published_sha`. A published candidate is
       durable on its own branch and its task is finishing, not stranded;
       drawing a blocker for one would be a loud false alarm.

    **Fail-closed on unreadable input, never quiet.** A corrupt or unreadable
    execution record for an in-progress task is returned as a strand carrying
    `FAULT_UNREADABLE_RECORD` and an obstacle, so it is reported and never
    auto-requeued. A record that is simply ABSENT is a different thing and is
    skipped: there is no evidence of a fault round, so this predicate has
    nothing to say about it (an in-progress task with no record at all is an
    adjacent strand class that neither reader acts on — it is visible in the
    dashboard's own in-progress list).

    **A ledger entry whose budget label parses to neither OPEN nor settled is
    left alone** (`worktask.split_attempt` is deliberately tolerant of a
    hand-edited record). It is not evidence of a fault, and inventing one from
    an unparseable field would be the same guess the attempt ledger exists to
    replace.
    """
    stranded: list[StrandedRound] = []
    for task in registry.in_progress_tasks():
        if task.id == current_task_id:
            continue
        try:
            execution = execution_store.load(task.id)
        except (StateError, OSError, ValueError) as exc:
            stranded.append(
                StrandedRound(
                    task_id=task.id,
                    fault_code=FAULT_UNREADABLE_RECORD,
                    obstacle=f"its execution record could not be read ({exc})",
                )
            )
            continue
        if execution is None or not execution.attempt_ledger:
            continue
        if execution.published_sha:
            continue
        _, budget, reason = split_attempt(execution.attempt_ledger[-1])
        outcome = attempt_outcome(reason)
        if budget in ATTEMPT_OPEN:
            fault_code = FAULT_INTERRUPTED
        elif budget == ATTEMPT_FAULT and outcome != REASON_SENT_FOR_REVIEW:
            fault_code = outcome or "unclassified_fault"
        else:
            continue
        obstacles = []
        if execution.candidate_sha:
            obstacles.append(
                f"it holds candidate {execution.candidate_sha[:12]}, and archiving "
                "that would destroy an incoming verdict"
            )
        if execution.review_round:
            obstacles.append(
                f"a reviewer has already seen review round {execution.review_round}"
            )
        stranded.append(
            StrandedRound(
                task_id=task.id,
                fault_code=fault_code,
                candidate_sha=execution.candidate_sha,
                review_round=execution.review_round,
                attempt_count=execution.attempt_count,
                fault_attempt_count=execution.fault_attempt_count,
                obstacle="; ".join(obstacles),
            )
        )
    return tuple(stranded)


def _strand_survey(config) -> tuple[tuple[StrandedRound, ...], str]:
    """`(strands, note)` for the loop `config` describes, read from disk.

    `note` non-empty means the survey COULD NOT RUN — an unreadable task file,
    an unreadable state file — and the caller escalates on it rather than
    reading a failed check as a clean one. A file that is simply ABSENT is not
    a failure: no task file means no tasks, which is the honest answer for a
    state directory the loop has never written to (and is what keeps this
    silent for every caller that has no roadmap at all).

    Never raises. It is called from `check`, which is advisory, read-only and
    routinely run against a half-initialised state directory.
    """
    try:
        registry = TaskStore(config.tasks_file).load()
    except (StateError, OSError, ValueError, TaskGraphError, KeyError) as exc:
        # The same net `cli._cmd_start`'s preflight casts over this exact call,
        # and for the same reason: `from_dict` deliberately tolerates shapes
        # that later raise `TaskGraphError`/`KeyError` rather than `StateError`,
        # and a monitor must report a task file it cannot read instead of dying
        # on it.
        return (), f"the task registry could not be read ({exc})"
    if registry is None:
        return (), ""
    try:
        state = (
            StateStore(config.state_file).load() if config.state_file.exists() else None
        )
    except (StateError, OSError, ValueError) as exc:
        # The state file names the task the loop is CURRENTLY working, and
        # without it every in-progress task looks abandoned. Refuse to guess:
        # reporting a healthy round as a strand is the false alarm this module
        # is written against, and reporting nothing would be the fail-open.
        return (), f"the loop state could not be read ({exc})"
    current = ((state.task_execution if state is not None else None) or {}).get(
        "task_id"
    ) or ""
    try:
        strands = stranded_fault_rounds(
            registry, TaskExecutionStore(config.executions_dir), current
        )
    except (  # pragma: no cover - defensive
        StateError,
        OSError,
        ValueError,
        TaskGraphError,
        KeyError,
    ) as exc:
        return (), f"the strand check could not run ({exc})"
    return strands, ""


def _describe_strands(strands: tuple[StrandedRound, ...]) -> str:
    """One line naming every stranded task and its fault code.

    Every id, not a sample: the whole failure being reported is a task nobody
    can see, and a truncated list would recreate it one row down.
    """
    return "stranded in_progress after an environment fault: " + ", ".join(
        f"{s.task_id} ({s.fault_code})" for s in strands
    )


def _with_strands(config, verdict: Health) -> Health:
    """`verdict`, plus whatever the strand survey found.

    Applied to EVERY verdict rather than being a check of its own, because the
    verdicts that return early are exactly the ones a stranded task co-occurs
    with — a stale lock, an open blocker, a loop that is not running. A late
    check would have stayed silent through the whole 2026-08-22 incident.

    A verdict that already needs attention keeps its own code (the operator is
    being sent somewhere for a reason) and gains the strand in its detail and in
    `stranded_tasks`. One that does not becomes `STUCK_STRANDED`: a task off the
    board with nothing scheduling it is precisely what this monitor exists to
    say out loud.
    """
    strands, note = _strand_survey(config)
    if not strands and not note:
        return verdict
    ids = tuple(s.task_id for s in strands)
    described = _describe_strands(strands) if strands else note
    if verdict.needs_attention:
        return replace(
            verdict,
            stranded_tasks=ids,
            detail=f"{verdict.detail}; {described}" if verdict.detail else described,
        )
    if strands:
        summary = (
            f"autoloop has {len(strands)} task(s) stranded in_progress — nothing "
            "will schedule them"
        )
    else:
        summary = "autoloop cannot verify whether any task is stranded"
    return Health(
        code=STUCK_STRANDED,
        needs_attention=True,
        summary=summary,
        detail=described,
        phase=verdict.phase,
        open_blockers=verdict.open_blockers,
        silent_minutes=verdict.silent_minutes,
        stranded_tasks=ids,
    )


def _agent_running(pattern: str = "claude -p") -> bool:
    """Is a subagent alive? Suppresses the silence alarm.

    `pgrep -f` LISTS matches; `pgrep -fc` counts patterns and will happily
    report 0 while a match is running. Use the listing and count lines.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


def _work_running(loop_pid: int | None) -> bool:
    """Is the loop's own process doing something other than waiting?

    A SECOND proof-of-work signal, weaker than `_agent_running` and checked
    after it. Validation is not an agent: while `pytest -n auto` runs the
    writer has already exited, nothing reaches the transcript until the round
    ends, and a healthy round was reported STUCK_SILENT — measured 2026-08-23,
    stop-01 at 48 minutes with eight pytest workers live.

    CAFFEINATE IS EXCLUDED, and that is the whole difficulty. The loop starts
    as `caffeinate -is python3 -m autoloop run`, so a bare has-children test is
    ALWAYS true and would retire this alarm entirely — trading a false positive
    for a false negative, which is worse: a monitor that never fires cannot be
    noticed failing.

    Anything else under the loop is work — pytest, ruff, npm, a git subprocess.
    Enumerating them would go stale the first time `validation_commands`
    changes, so the rule is "a child that is not the caffeinate wrapper".
    """
    if not loop_pid:
        return False
    try:
        children = subprocess.run(
            ["pgrep", "-P", str(loop_pid)], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    for pid in children.stdout.split():
        try:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if cmd.strip() and not cmd.lstrip().startswith("caffeinate"):
            return True
    return False


@dataclass(frozen=True)
class SleepEvidence:
    """What the platform can prove about machine sleep inside a time window.

    `asleep_seconds is None` means wake history could not be established —
    the caller must then fail toward quiet, and `note` says why so the
    verdict's detail can explain itself. When it is a number, it is the
    seconds of the window that CANNOT be credited as awake: proven sleep,
    plus any stretch the available history does not vouch for (time before
    the current boot, or before darwin's last recorded sleep). Subtracting
    it therefore leaves a proven LOWER BOUND on awake silence — a stuck
    verdict is only ever built on time the machine is known to have been
    awake, while unprovable time quietly counts as sleep. 0.0 stays a real
    answer: provably awake throughout.
    """

    asleep_seconds: float | None
    note: str


def _parse_timeval_sec(out: str) -> float:
    """`{ sec = 1754126400, usec = 837291 } Sat Aug  2 ...` → 1754126400.0.

    The same output shape — and the same parse — as `lock.boot_time_epoch`
    uses for kern.boottime, so the two modules read one family of evidence.
    """
    marker = "sec = "
    start = out.index(marker) + len(marker)
    end = start
    while end < len(out) and out[end].isdigit():
        end += 1
    return float(out[start:end])


def _sysctl_timeval_epoch(name: str) -> float | None:
    try:
        out = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return _parse_timeval_sec(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _darwin_sleep_evidence(window_start: float, window_end: float) -> SleepEvidence:
    """Not-awake seconds from kern.sleeptime/kern.waketime — the LAST
    sleep→wake pair, which bounds what these sysctls can prove. While we are
    running no sleep postdates `waketime`, so the pair characterises
    [sleeptime, now] COMPLETELY: a window starting at or after `sleeptime`
    gets its exact sleep overlap, awake-throughout included. A window
    starting earlier reaches time the pair cannot see — any number of
    finished sleeps may hide before the last one — so there only the tail
    since `waketime` is credited as awake and everything before it joins the
    discount. That under-counts awake silence (the quiet direction, per the
    SleepEvidence contract) without going blind: a hung loop keeps growing
    that proven-awake tail and still crosses the threshold within one
    silence window of the last wake.
    """
    slept = _sysctl_timeval_epoch("kern.sleeptime")
    woke = _sysctl_timeval_epoch("kern.waketime")
    if slept is None or woke is None:
        return SleepEvidence(None, "kern.sleeptime/kern.waketime unreadable")
    if slept <= 0 and woke <= 0:
        return SleepEvidence(0.0, "no sleep recorded this boot")
    if slept <= 0 or woke < slept:
        # A wake without its sleep (or the reverse) is not a window we can
        # subtract; guessing here is how a hung loop gets excused.
        return SleepEvidence(None, "kern.sleeptime/kern.waketime inconsistent")
    if window_start >= slept:
        overlap = min(woke, window_end) - max(slept, window_start)
        return SleepEvidence(max(0.0, overlap), "kern.sleeptime/kern.waketime")
    proven_awake = max(0.0, window_end - woke)
    not_awake = max(0.0, (window_end - window_start) - proven_awake)
    return SleepEvidence(
        not_awake,
        "kern.sleeptime/kern.waketime; window predates the last sleep, so only "
        "the tail since the last wake counts as awake",
    )


def _linux_sleep_evidence(window_start: float, window_end: float) -> SleepEvidence:
    """CLOCK_BOOTTIME advances during suspend, CLOCK_MONOTONIC does not (on
    linux), so their difference is total suspend since boot. It carries no
    placement, so the whole amount is credited to the window (clamped):
    over-crediting can only miss a detection, never fabricate one — the
    direction this monitor is built to fail toward.
    """
    try:
        total = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):
        return SleepEvidence(None, "CLOCK_BOOTTIME unavailable")
    if total <= 0:
        return SleepEvidence(0.0, "no suspend recorded this boot")
    window = max(0.0, window_end - window_start)
    return SleepEvidence(min(total, window), "CLOCK_BOOTTIME-CLOCK_MONOTONIC")


def machine_sleep_in_window(window_start: float, window_end: float) -> SleepEvidence:
    """Not-awake seconds inside [window_start, window_end] epoch seconds, or
    an unavailable verdict the caller must treat as quiet.

    `lock.boot_time_epoch` is the shared boot boundary: sleep evidence read
    from a running kernel describes THIS boot only, so any part of the
    window before boot — an off or rebooting machine — joins the discount
    rather than being mistaken for awake silence. Without a readable boot
    time the window cannot be tied to the boot the evidence describes, and
    the honest answer is unavailable, not a guess.
    """
    boot = boot_time_epoch()
    if boot is None:
        return SleepEvidence(
            None, "boot time unreadable — wake history cannot be bounded to this boot"
        )
    pre_boot = max(0.0, min(boot, window_end) - window_start)
    start = max(window_start, boot)
    if sys.platform == "darwin":
        evidence = _darwin_sleep_evidence(start, window_end)
    elif sys.platform.startswith("linux"):
        evidence = _linux_sleep_evidence(start, window_end)
    else:
        return SleepEvidence(None, f"no wake-history source on {sys.platform}")
    if evidence.asleep_seconds is None or pre_boot <= 0.0:
        return evidence
    return SleepEvidence(
        evidence.asleep_seconds + pre_boot,
        f"{evidence.note}; pre-boot time discounted (window predates this boot)",
    )


def last_transcript_event(path: Path) -> datetime | None:
    """Timestamp of the newest transcript entry, or None.

    Reads only the tail: a long run's transcript grows without bound and a
    health check must stay cheap enough to run every few minutes.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            stamp = json.loads(line).get("ts")
        except json.JSONDecodeError:
            continue  # a torn final line during a concurrent append
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def check(
    config,
    now: datetime | None = None,
    silence_minutes: float = DEFAULT_SILENCE_MINUTES,
    agent_probe=_agent_running,
    work_probe=_work_running,
    sleep_probe=machine_sleep_in_window,
) -> Health:
    """Judge the loop, then the queue behind it. Read-only, and safe to run
    mid-round.

    Two steps rather than one, and the split is the point. `_judge` answers "is
    this loop working", which is what every signal in this module's docstring
    is about, and it returns EARLY from a dozen places. `_with_strands` then
    asks the question that is true independently of all of them — "is a task
    off the board with nothing scheduling it" — and applies the answer to
    whatever verdict came back, so it cannot be shadowed by one.
    """
    return _with_strands(
        config, _judge(config, now, silence_minutes, agent_probe, work_probe, sleep_probe)
    )


def _judge(
    config,
    now: datetime | None = None,
    silence_minutes: float = DEFAULT_SILENCE_MINUTES,
    agent_probe=_agent_running,
    work_probe=_work_running,
    sleep_probe=machine_sleep_in_window,
) -> Health:
    """Is the LOOP working? The verdict `check` starts from — every signal in
    the module docstring except the strand survey, unchanged."""
    now = now or datetime.now(timezone.utc)

    lock = LoopLock(config.state_dir)
    info = lock.read()
    live = info is not None and LoopLock.is_live(info)

    if info is not None and not live:
        return Health(
            code=STUCK_STALE_LOCK,
            needs_attention=True,
            summary="autoloop crashed — a stale lock is left behind",
            detail=f"{info.describe()}; recover with `python -m autoloop start`",
        )

    blockers = BlockerStore(config.blockers_dir).open_blockers_by_severity()
    state = StateStore(config.state_file).load() if config.state_file.exists() else None
    phase = state.phase if state is not None else ""

    # Blockers first: they are the reason a human is needed, and they outlive
    # the session that raised them.
    if blockers:
        # `blockers[0]` is now the PRIMARY one — `blockers.primary_sort_key`,
        # severity before recency — not whatever the blocker directory happened
        # to list first. The count is unchanged and the rest are still open, so
        # the detail says how many else there are rather than implying this is
        # the only one. With exactly one open (the common case) that suffix is
        # absent and the line is byte-identical to what it always was.
        primary, others = blockers[0], len(blockers) - 1
        detail = f"{primary.id} ({primary.code}): {primary.question[:200]}"
        if others:
            detail += f" (+{others} more open)"
        return Health(
            code=STUCK_BLOCKED,
            needs_attention=True,
            summary=f"autoloop needs a decision — {len(blockers)} open blocker(s)",
            detail=detail,
            phase=phase,
            open_blockers=len(blockers),
        )

    if state is not None and Phase(phase) is Phase.FAILED:
        return Health(
            code=STUCK_FAILED,
            needs_attention=True,
            summary="autoloop session FAILED",
            detail="resolve with `python -m autoloop run --retry`",
            phase=phase,
        )

    if state is not None and Phase(phase) is Phase.NEEDS_USER:
        # A task_fatal park is one continuous mode handles by quarantining
        # that task and carrying on, so it is only worth waking someone for
        # when the loop is not running to handle it.
        handled = getattr(state, "park_kind", None) == "task_fatal" and live
        if not handled:
            return Health(
                code=STUCK_PARKED,
                needs_attention=True,
                summary="autoloop is parked and waiting for you",
                detail=(state.question or "(no question recorded)")[:200],
                phase=phase,
            )

    # A pause is a decision, not a fault.
    if config.pause_file.exists() or config.legacy_pause_file.exists():
        return Health(
            code=OK_PAUSED,
            needs_attention=False,
            summary="autoloop is paused (`resume` to continue)",
            phase=phase,
        )

    if not live:
        return Health(
            code=STUCK_NOT_RUNNING,
            needs_attention=True,
            summary="autoloop is not running",
            detail="start it with `python -m autoloop start`",
            phase=phase,
        )

    last = last_transcript_event(config.transcript_file)
    silent = None if last is None else (now - last).total_seconds() / 60.0
    if silent is not None and silent > silence_minutes:
        if agent_probe():
            # The commonest false alarm: an audit fan-out is quiet for
            # fifteen-plus minutes while six subagents work. Checked before
            # sleep evidence on purpose — a live agent is proof of work
            # whatever the wall clock or the wake history says.
            return Health(
                code=OK_RUNNING,
                needs_attention=False,
                summary=f"autoloop is working (agent running, quiet {silent:.0f}m)",
                phase=phase,
                silent_minutes=silent,
            )
        if work_probe(info.pid if info else None):
            # Weaker than a live agent and checked after it: this only says the
            # loop's process is busy, which during validation is the truth the
            # transcript cannot tell. Still ahead of sleep evidence — a running
            # subprocess settles the question without it.
            return Health(
                code=OK_RUNNING,
                needs_attention=False,
                summary=f"autoloop is working (round in progress, quiet {silent:.0f}m)",
                detail="validation or another subprocess is running under the loop",
                phase=phase,
                silent_minutes=silent,
            )
        evidence = sleep_probe(last.timestamp(), now.timestamp())
        if evidence.asleep_seconds is None:
            # Fail toward quiet: without wake history a slept laptop and a
            # hung loop are indistinguishable, and a false "stuck" is the
            # alarm that teaches a human to ignore this monitor.
            return Health(
                code=OK_RUNNING,
                needs_attention=False,
                summary=(
                    f"autoloop is quiet ({silent:.0f}m) but wake history is "
                    "unavailable — not calling it stuck"
                ),
                detail=(
                    f"{evidence.note}; the machine may have been asleep, "
                    "and the next check re-judges in minutes"
                ),
                phase=phase,
                silent_minutes=silent,
            )
        asleep = evidence.asleep_seconds / 60.0
        awake_silent = max(0.0, silent - asleep)
        if awake_silent > silence_minutes:
            discount = f", {asleep:.0f}m not provably awake discounted" if asleep > 0 else ""
            return Health(
                code=STUCK_SILENT,
                needs_attention=True,
                summary=f"autoloop looks stuck — no activity for {awake_silent:.0f} minutes",
                detail=f"phase={phase}, no subagent running{discount}",
                phase=phase,
                silent_minutes=awake_silent,
            )
        return Health(
            code=OK_RUNNING,
            needs_attention=False,
            summary=(
                f"autoloop is running (quiet {silent:.0f}m, "
                f"{asleep:.0f}m of it not provably awake)"
            ),
            phase=phase,
            silent_minutes=awake_silent,
        )

    return Health(
        code=OK_RUNNING,
        needs_attention=False,
        summary=f"autoloop is running (phase={phase or 'starting'})",
        phase=phase,
        silent_minutes=silent,
    )
