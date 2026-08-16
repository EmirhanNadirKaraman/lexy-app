# AUTOLOOP — autonomous Fable ↔ ChatGPT engineering loop

Orchestration for a loop where an AI engineer (Fable) does the repository work
and ChatGPT — used **through a real browser session, never the OpenAI API** —
reviews each report and answers with the next machine-readable directive.

**State of the system: Autoloop v1 (2026-07-30).** Every directive that does
repository work — `audit`, `revise` (of the audit or of a registry task), and
`implement` — runs through the **produce-then-review** commit path (§4b): its
own isolated worker repository, an automatic commit once validation passes,
and review from the immutable committed objects. This is now the **only**
dispatch path; the older authorize-then-produce/change-manifest commit gate
described in §4 was **retired**, not merely superseded — see
`docs/SECURITY.md` S21 ("closed by retirement"). `policy.implement_enabled`
still gates `implement`/`revise` of ordinary registry tasks (default `false`
as of this writing — the phase gate is an operator decision, independent of
whether an executor exists behind it), but `audit`/`revise("audit")` are
never gated and always run.

**Two production executors as of the implement executor (below).**
`cli._build_executor` now constructs both `AuditExecutor` (read-only
subagents; §7) and `ImplementExecutor` (write-capable subagent; §7b) and
wraps them in a small `_DispatchingExecutor` that routes each directive by
the same `is_audit` test `orchestrator._dispatch_executor` itself uses
(`decision is AUDIT or task_id == "audit"` → audit executor, everything else
→ implement executor). The orchestrator's own `self._executor` field and the
`TaskExecutor` protocol are untouched — the dispatcher lives entirely in
`cli.py`'s wiring layer. `implement`/`revise` of a real registry task still
reaches `ImplementExecutor` only once `policy.implement_enabled = true`; the
phase gate is enforced by `policy.py` before dispatch, and `ImplementExecutor`
itself refuses (defense in depth) anything that is not an `implement`/
`revise` of a real task.

`cli.py`'s `_build_orchestrator` — what the plain `run` command always calls —
unconditionally constructs the full produce-then-review collaborator set
(`WorkerRepoManager`, `TaskExecutionStore`, `IntentStore`, a provisioned
`Publisher`). There is no configuration that falls back to the retired path.

Code: `autoloop/`. Runtime state: `.autoloop/` (gitignored).

---

## 1. Architecture (Autoloop v1)

| Component | File(s) | Owns |
|---|---|---|
| Orchestrator | `orchestrator.py` | Persisted state machine (ready → [delivering] → submitting → awaiting → executing), failure routing, budgets, review-integrity gates, produce-then-review dispatch (`_dispatch_task_postcommit` — the only executor-dispatch path). `delivering` is entered only for a review packet whose patch is too large for one chat message (§5d-bis). |
| Lock | `lock.py` | Single-instance lock per state dir (see §3). |
| Change manifest (retired, kept for its own unit tests) | `manifest.py` | The old task-owned change-manifest commit gate (see §4) — **no production caller since 2026-07-30** (docs/SECURITY.md S21/S22). |
| Worktrees / task execution | `worktree.py` (`WorktreeManager`, unused in production — see §4c), `worktask.py` (`TaskExecution`, `CommitIntent`, `reconcile_after_crash`) | Per-task worktree/branch bookkeeping, and the crash-safe commit-intent/candidate-sha bookkeeping for produce-then-review (see §4b). |
| Review packet | `packet.py` | Renders the post-commit review packet from immutable git objects (see §4b). |
| Worker/publisher separation | `worker_env.py` (`worker_env`, `WorkerRepoManager`, `verify_worker_isolation`), `publisher.py` (`Publisher`, `provision_publisher_repo`, `reprovision_publisher`) | Autoloop M2 (see §4c): a scrubbed environment + no-remote repo for worker-side git access — this is what production `_build_orchestrator` actually uses, not `WorktreeManager` — and a dedicated, hooks-controlled repository that is the only path through which a candidate commit is published, with a provision-time URL snapshot (§4d). |
| Conversation | `conversation.py` (interface/registry), `browser/chatgpt.py` (`BrowserChatGPT`), `browser/playwright_session.py` (CDP, lazy, **one driver per process** — see below), `browser/selectors.py` | One persistent reviewer conversation; duplicate/stale/streaming/login guards; provider-pluggable. |
| Contract | `contract.py` | Response contract **v3** + strict parser + `verify_review`. `CONTRACT_INSTRUCTIONS` = the response format plus **two** advisory paragraphs, each prose the reviewer weighs and **not** a `policy.py` refusal (which would park the loop instead of redirecting it). `NEXT_WORK_PREFERENCE`: prefer finishing work already in flight (revise/approve a task holding an unpublished candidate) over dispatching a fresh one, unless nothing is in flight, everything in flight is blocked on something external, or the operator asks; it reads the `in_flight` counts from the CONTEXT block. `AUDIT_VS_READY_PREFERENCE` (added 2026-08-15): while the roadmap has READY tasks, `implement` one of them rather than ordering a fresh `audit`, since an audit ADDS findings — `audit` stays right when no task is ready, when every ready task is blocked on something outside the roadmap (an unmodelled blocker; a task with an unmet *declared* dependency is BLOCKED, never READY), or when the operator asks; it reads the ready and priority-1 counts from the CONTEXT `roadmap` line. One rule, one text: neither clause restates the other. |
| Policy | `policy.py` | Deterministic gates: git whitelist (`add -A` and force pushes structurally impossible), task-reference checks, **phase gate**, budgets. |
| Tasks | `tasks.py` | Task registry/graph (derived ready/blocked, cycles rejected, atomic persistence). `seed_tasks.json` (git-tracked, alongside `tasks.py`) seeds a fresh registry with `rt-01` when `.autoloop/tasks.json` does not exist yet (§9b). **Three states mean "not running", and they are not interchangeable:** the dependency-derived `BLOCKED` resolves itself; `block`/`unblock` quarantine a task after a `task_fatal` park (§9c) via a dedicated `blocked` status/`TaskState.BLOCKED_BY_OPERATOR`, which resolves when an operator answers; and `retire` (§9d) marks work SUPERSEDED via `status="retired"`/`TaskState.RETIRED` + `Task.superseded_by`, which resolves for nobody. |
| Blockers | `blockers.py` | Persisted operator-facing `Blocker` records (one JSON file per blocker, `.autoloop/blockers/`) for every park, `task_fatal` or `loop_fatal` (§9c) — `python -m autoloop blockers`/`answer`. |
| Context | `context.py` | Per-request CONTEXT block: integrity stamp + previous decision/task, roadmap, `in_flight` counts (in progress / holding an unpublished candidate), git summary, changed files, validation summary. |
| Prompts | `prompts.py` | Strict template library (incl. `audit_kickoff`, `smoke_test`, `postcommit_review`). |
| Git | `git_gateway.py` | Only git runner; exact-path staging; policy-validated per call; `push_exact` is the only way to publish anything (no ambient `push()`); the legacy `commit()` method is **removed** (S21). |
| Doctor | `doctor.py` | Non-destructive preflight (§6), including worker isolation, controlled hooks directories, publisher configuration, and publisher URL drift. |
| Audit executor | `audit/` | The read-only production executor (§7): `findings` (agent contract), `agents` (claude-CLI runner, tool set now a constructor param — see §7b), `reconcile`, `taskgen`, `markdown` (MD-only gate), `report`, `executor`. Dispatched as a task-shaped unit of work (`Orchestrator._resolve_audit_task`), so it runs through §4b like any other task. |
| Implement executor | `implement_executor.py` | The write-capable production executor (§7b): `ImplementExecutor` runs ONE `Edit`/`Write`-capable subagent (`implement_agent_runner`, built on the SAME `audit.agents.ClaudeCliRunner` the audit uses, configured with a different tool set) against a task's own worker repo, derives `changed_paths` from the worker repo's real `git status`, then re-runs validation. `cli._build_executor`'s `_DispatchingExecutor` routes `implement`/`revise`-of-a-real-task here; `audit`/`revise("audit")` still go to `AuditExecutor`. |
| State / transcript | `state.py`, `transcript.py` | Atomic crash-safe state; append-only JSONL audit log. |
| CLI | `cli.py` | `run [--continuous] status tasks next-task blockers answer retire release doctor smoke-browser pause resume unlock reset reprovision-publisher` (§8/§9). |

---

## 2. Executor lifecycle (produce-then-review, unconditional)

For **every** `audit` / `revise` (of the audit, or of a registry task) /
`implement` directive, `_dispatch_executor` routes to
`_dispatch_task_postcommit` — see §4b for the full sequence (worker repo →
executor → automatic commit → structural + re-run validation → review
packet). There is no other branch: the old manifest-begin/execute/
manifest-finish/report cycle this section used to describe was the
authorize-then-produce path, retired 2026-07-30 (docs/SECURITY.md S21).

The one thing specific to *which* directive is running: `_resolve_audit_task`
gives the audit its own synthetic, per-run `Task` (`audit-<iteration>`, e.g.
`audit-0007`) — distinct from the protocol-level pseudo-id `"audit"` ChatGPT
uses in a `revise` directive — so it gets the same isolated-worker-repo
treatment as a real task, and a `revise` of the audit resumes the SAME unit
id rather than forking a new worker repo per round. `implement`/`revise` of
an ordinary registry task use the task's own id directly.

`NullExecutor` (via `run --null-executor` or `executor.kind = "null"`) still
works: the worker repo is still created (there is nothing to route around),
but the executor itself reports `status="not_implemented"` with no
`changed_paths`, and `_dispatch_task_postcommit` checks `outcome.status`
BEFORE attempting any commit — an `implementation_review` reporting the
honest "not implemented" outcome is sent, and no `git commit` is ever
called, rather than a fabricated success.

When a real executor is in play (i.e. `executor.kind != "null"`), `_executor`
is `cli._build_executor`'s `_DispatchingExecutor`, which holds both
`AuditExecutor` and `ImplementExecutor` and picks per call — see the
"Two production executors" note above (state-of-the-system intro) and §7b.

---

## 3. Single-instance locking

`run`, `resume`, `reset` and `smoke-browser` hold `.autoloop/LOCK` (atomic
`O_CREAT|O_EXCL`; records pid, hostname, start time, run id, state dir).
`status` / `tasks` / `doctor` / `pause` stay available while locked.

* Live owner (same host, pid alive — or any foreign host, which can't be
  verified) → **fail closed**; wait or stop that process.
* Verifiably dead owner → error naming the recovery command. Locks are never
  stolen silently: `python -m autoloop unlock` is the only recovery, and it
  refuses live locks. A corrupt lock file counts as stale (nothing live could
  have written it) and stays on disk for diagnosis until `unlock`.
* Clean exit releases; release is run-id-guarded, so a zombie can never
  remove a successor's lock.
* **A lock written before the machine's current boot is stale whatever its
  pid says**, and the boot check runs BEFORE the pid probe. Pids are
  reassigned across a reboot, so a lock left by a power-off can name a pid
  that some unrelated process now holds; `os.kill(pid, 0)` then reports it
  live and `unlock` refuses with "stop that process instead" — pointing the
  operator at an innocent process with no way forward. Boot time comes from
  `kern.boottime` (darwin) or `/proc/stat`'s `btime` (linux), never from a
  monotonic clock: macOS's stops during sleep, so uptime-minus-now on a
  laptop that has slept would date boot too recently and could declare a
  LIVE lock stale. When boot time is unreadable, or the stamp is
  unparseable or timezone-naive, the pid probe decides exactly as before —
  the check can only ever make MORE locks recoverable, never fewer.

### 3g. Detecting a task's scope (propose, never authorize)

The dashboard's new-task form has a **Detect paths** button. It reads the
title and description and fills `approved_paths` with what the task probably
touches, each line carrying the reason it was proposed:

```
autoloop/validation.py    # defines run_validation_commands
autoloop/tests/           # directory named in the description
```

The operator reads the list, deletes what does not belong, and submits. The
comments are stripped on submit — a path is what the registry validates.

**Why it stops there.** `approved_paths` is what a write-capable agent may
write, and `docs/SECURITY.md` finding #2 exists because the executor's own
report must never define its own scope. Deriving the scope automatically at
merge or dispatch time would rebuild that circularity with extra steps: the
task would arrive carrying its own permission slip. So detection runs at
AUTHORING time, into a field a human reads, and `/api/suggest-paths` queues
nothing at all.

**Deliberately not an LLM.** Every suggestion is a mechanical consequence of
text the operator wrote plus files that exist, so each one is explainable in a
phrase — and a suggestion you cannot explain is one you cannot check. Three
sources, most trustworthy first: a path the text names; a bare filename that
resolves to exactly ONE tracked file; an identifier DEFINED in exactly one
file. Ambiguity resolves to nothing rather than a guess, because offering the
wrong `models.py` is worse than offering none — it looks considered.

Identifiers are matched by SHAPE (snake_case or CamelCase), not by a
blocklist. A bare lowercase word like `report` is prose that happens to
collide with a function name; the first version matched it and produced one
confident, wrong suggestion.

---

### 3f. `merge-window` — do not strand the loop's own work

```bash
python -m autoloop merge-window            # exit 0 = safe to merge
python -m autoloop merge-window --wait     # block until it opens
```

The operator and the loop share one branch, so every merge into it while a
task holds a candidate invalidates that task: `task_base_sha` is pinned, the
loop refuses to rebase (a reviewer has already seen the candidate, and
re-basing would discard reviewed work), and it parks. This happened **four
times on 2026-08-02**, every time because "no agent is running right now"
looked like "safe to merge".

The real condition is not the phase — it is whether any
`.autoloop/executions/*.json` carries a `candidate_sha` **for a task that could
still be dispatched or reviewed**. Records outlive the work they describe, so a
completed or quarantined task's record is skipped; an unknown id is not. A
dispatched task that has not committed yet holds nothing reviewed, so it does
not close the window; an executing phase does. A candidate already PUBLISHED on
its own side branch — confirmed against the remote, never inferred from the
record's own `intended_remote_ref`, which is written *before* the push — is
durable and does not close it either.

There is a third exemption, added 2026-08-15 after fourteen records held the
window shut at once: a record whose task is back in the queue **and** whose
recorded worker repo is gone **and** whose candidate the checkout cannot
resolve is a defect in the record, not work in flight — there is no reachable
commit for a moved base to strand. All three conditions are required, and an
empty `worktree_path` does not satisfy the second ("we never recorded where it
was" is not "we know it is gone"). It is reported as a `note:`, never hidden:
`release` retires its record now, so seeing one means something should have
been retired and was not.

The third condition is answered by git and by nothing else, and only when git
actually answers. `read_commit` failing is not that answer — `cat-file commit`
dies the same way for a missing object, a corrupt one, an I/O error and a
policy refusal — so a failed read leads to one further question,
`GitGateway.object_exists`, which reports True/False from `cat-file -e`'s exit
code (0 present, 1 absent) and raises on anything else. Only an explicit
"the object database does not hold this" writes a record off; every other
outcome keeps the window shut, the same fail-closed rule publication checking
follows.

The intended workflow:

```bash
git switch -c fix/whatever && ...work...
python -m autoloop merge-window --wait \
  && git switch audit/initial-autoloop-audit \
  && git merge --ff-only fix/whatever
```

Working in a `git worktree` is the companion habit: `git ls-files` never lists
`.git/` internals, so a worktree outside the checkout is invisible to the
escape detector, and editing the primary checkout while a write-capable task
is dispatched is what trips it (§3e).

---

### 3f-bis. `policy.auto_merge_enabled` — publication is not integration

```toml
[policy]
auto_merge_enabled = false     # the default
```

B10 retires a task the moment its candidate is confirmed on its own side
branch. That is publication, and until this flag existed it was also the end
of the road: the operator merged every branch by hand. On **2026-08-06 seven
completed tasks were unmerged at once**, among them the tab reaper and the
Python restart module — fixes for failures the loop was still hitting while
their code sat on branches nobody had pulled.

With the flag on, `auto_merge.py` runs immediately after a completion
(`orchestrator._auto_merge_after_completion`, the last statement of
`_dispatch_task_push`): it merges the candidate into the branch the loop
builds against and **pushes that branch**. An unpushed merge is the same
invisibility one level down.

It is gated on the predicate above — `cli._merge_window_blockers`, *called*,
not reimplemented, because a second copy that drifted by one case is how
thirteen tasks get stranded at once (which is how many held unpublished
candidates on 2026-08-06). What happens at each outcome:

| Situation | What happens | Transcript entry |
|---|---|---|
| Window open, clean merge | merged + base pushed | `auto_merge_pushed` |
| Window shut / base moved / dirty checkout | **deferred**, retried after the next completion | `auto_merge_deferred` |
| Conflict | `merge --abort`, base byte-identical, conflicting files named | `auto_merge_conflict` |
| Merged but the base push was refused | deferral kept; a retry re-pushes | `auto_merge_push_refused` |
| HEAD did not move / lost the old base / dirty after merge | nothing pushed | `auto_merge_failed` |

Nothing here ever parks. By the time it runs the push has already landed and
the task is already completed, so an integration problem is logged and left
for the next pass — turning it into a park would stop a working loop over a
step that can simply happen later. **A deferred merge is a normal state.**
Deferrals live in `<state_dir>/merge-deferrals/`, one file per task, and
survive a `reset` for the same reason blockers do.

**A deferral pins its execution record.** The retry is drained from that
record — `AutoMerger.attempt` reads `candidate_sha`, `worktree_path` and
`intended_remote_ref` back off the live file — so anything that retires the
record while a deferral is outstanding silently ends the retry: the next drain
finds no record, *skips* the task, and clearing the deferral is part of
skipping. `_dispatch_task_push` states this where it advances the record rather
than retiring it, and `orchestrator._reconcile_published_execution` honours the
same rule: a record it would otherwise retire as "already shipped" is kept
(logged `execution_retire_pinned_by_deferral`), the task is completed so the
merger will still touch it, and the dispatch stops without parking — the park
it would otherwise take asks the operator to archive that very record.
Retirement happens on a later dispatch, once the merge has been pushed and
confirmed and the deferral is gone. An unreadable deferral store counts as
"one is outstanding": a dropped retry is indistinguishable from work that was
never merged.

A merge command returning 0 is not evidence: after the merge the head must
have moved, must contain the candidate, must still contain the previous base,
and the tree must be clean, before anything is pushed. The remote base is
checked for divergence *before* the merge rather than left to the push failing
afterwards — a local merge onto a base the remote is already ahead of has to
be unwound by hand, and the git whitelist has no `reset`.

Two things it deliberately does not do:

* **Protected bases.** If the base is in `protected_branches`, the merge
  happens locally and the push is refused until `allow_protected_push = true`.
  Enabling auto-merge is not by itself permission to push `main`.
* **The pre-existing backlog.** This module only reacts to completions it
  sees. Sweeping the branches nobody is going to report is §3f-ter.

---

### 3f-ter. `merge-backlog` — the branches nobody will report

```bash
python -m autoloop merge-backlog     # exit 0 = the backlog is clear
```

`auto_merge.py` (§3f-bis) reacts to ONE completion. A branch published before
that existed — or by a process that died before integrating anything — has no
event left to react to, and until `merge_sweep.py` nothing ever looked for it.
That is how **2026-08-06** happened: seven completed tasks published and
unmerged at the same moment (auto-08, auto-12, brw-01, brw-07, inbox-09,
rt-10, rt-11), the base still at d2d4d6b, noticed only by a hand-written
`git ls-remote` loop. Two of the seven were fixes for failures the loop was
still hitting.

The sweep runs **at startup** (once per process, inside the loop lock, so
`run`, `start` and `resume` all get it) and **on demand** from the command
above. Same flag as auto-merge — `policy.auto_merge_enabled`, default off —
because it moves the same branch head, and the command takes the loop lock for
the same reason (`merge-window` does not: it only reports).

What it adds to auto-merge, which it CALLS rather than reimplements:

* **Enumeration.** Every COMPLETED task whose execution record carries a
  candidate, whose candidate is not already in the base, and whose branch the
  remote confirms is carrying exactly that candidate. A completed, unmerged
  task whose branch is gone from origin is NAMED (`merge_sweep_unresolved`),
  never merged from the record's own claim.
* **"Could not look" is not "nothing to merge".** A completed task has three
  possible answers, not two — in the base, outstanding, or *unjudgeable* — and
  the third has to survive into the exit code or it collapses into the first.
  Four states are unresolved: the remote does not confirm the branch (a deleted
  ref and an unreachable remote are indistinguishable, and
  `_candidate_publication` is not asked to distinguish them); the execution
  record cannot be READ; there is no live record and no archived one shows the
  work already landed; the record names no candidate. None is attempted, and
  every one of them makes the run not-clear: `merge-backlog` exits **1** and the
  startup hook prints instead of staying silent. Exit 0 means the backlog is
  provably clear and nothing weaker.
* **One unjudgeable task holds the WHOLE invocation** (`merge_sweep_held`,
  since 2026-08-15). Naming it and sweeping on is not safe, because this module
  deliberately supports a later branch being cut from an earlier one: publish A,
  cut B from A, lose A's ref, and merging B makes A an ancestor of HEAD anyway —
  the refusal to merge A undone transitively by the next branch in the list.
  Excluding only the candidates that DESCEND from an unresolved one needs the
  ancestry of a commit the sweep may be unable to name or resolve at all, so the
  invariant is the coarse one: any enumeration-time unresolved ⇒ nothing is
  merged this invocation. **The cost is real and accepted** — one stale
  unjudgeable task blocks every sweep until an operator deals with it — but
  nothing has been mutated at that point, so it costs a delay, never a bad base.
  Startup still only REPORTS and the loop starts normally. A publication that
  stops being confirmed *mid-sweep* is a different answer and keeps its own
  (`merge_sweep_publication_changed`, below): branches have already landed by
  then, so the honest report is where the sweep got to.
* **Retired records are still read, for one question only.** `retire_execution`
  archives a record once publication is CONFIRMED, which is not the same as
  merged — with the flag off, nothing integrates it. The sweep therefore checks
  `executions/archive/` for a sha that is already an ancestor of HEAD (silent
  if so, unresolved if not) — ancestry alone, exactly as on the live path,
  since requiring the archived record's own `published_sha` would leave every
  archive predating that field permanently unresolved with its candidate
  demonstrably merged. It never merges FROM an archived record; the merge
  machinery reads the live one. This is the one place that
  differs from `merge-window`, which ignores the archive deliberately — the
  gate asks "could moving the base strand this?" (a retired record cannot),
  this asks "is this branch in the base?" (retirement says nothing either way).
* **Only the NEWEST retirement answers** (since 2026-08-15). `archive` keeps
  every generation — a release, a retry, the `published-<sha>` retirement that
  completed the task — and they describe different commits, so "any archived
  copy names an ancestor" clears a task on the strength of a superseded attempt
  while its completing publication sits unmerged. Generation is read off the
  archive FILENAME (`retire_execution` appends a fixed-width `YYYYMMDDTHHMMSSZ`
  instant to every label; whole labels do not order across differing reasons,
  that trailing component does). A single archived copy needs no ordering and is
  judged directly, which keeps pre-stamp records answerable; from two upwards an
  unstamped label is unresolved rather than guessed at, and a same-second tie
  requires all of the tied copies. Superseded generations are *not* required to
  have landed — a released attempt's candidate is usually abandoned — or every
  retried task would be unresolved forever.
* **Publication is re-confirmed per branch, at the moment it is merged.** The
  `seen` cache of confirmed publications is shared with the merge-window gate,
  but a candidate's own key is evicted immediately before its merge, so every
  branch is integrated on an `ls-remote` taken after the previous one landed —
  never on evidence gathered before the sweep started. A ref deleted or
  force-moved mid-sweep stops it (`merge_sweep_publication_changed`) rather
  than being merged from a stale positive.
* **Merged-ness by ancestry, and by nothing else.** `merge-base --is-ancestor`.
  Not the task status, not a branch-name match — those are what made the
  backlog invisible in the first place, since both are equally true of a branch
  that landed in the base an hour ago. A candidate the checkout cannot resolve
  reads as not-integrated, which is not a guess: every ancestor of HEAD is in
  the local object database by definition.
* **Order: oldest publication first.** A branch cut from another branch only
  applies cleanly after the one it builds on; arbitrary order manufactures
  conflicts that do not really exist. `published_at` when the record has one,
  and a record with none sorts ahead of every record that has one — the field
  only exists from 2026-08-15, so its absence dates the record — with the
  candidate's committer date ordering that older group among itself.
* **Stop at the first branch that does not land.** Conflict, a merge that
  failed verification, or a deferral: all of them halt the sweep, leave every
  later branch untouched, and name them. A half-swept backlog with one branch
  aborted mid-way is harder to reason about than a clean stop, and the operator
  has to resolve that conflict before the rest mean anything. Order is a
  heuristic and is allowed to be, because stopping is what makes a wrong order
  safe: it costs a stalled sweep, never a corrupted base.
* **A stop is not automatically a restoration** (since 2026-08-15). Two
  outcomes leave the base MOVED: a merge that ran and then failed verification
  (deliberately not undone — `reset` is off the git whitelist by design), and
  one that verified and whose PUSH was then refused, which comes back as
  `deferred` — the same slug a shut gate and a dirty checkout produce, both of
  which touch nothing. So the answer is never read off the outcome: HEAD and
  `status --porcelain` are observed immediately before each attempt and again
  the moment one does not land, and "the base is exactly as it was" is printed
  only when those two match. Anything else — including a probe that could not
  read the checkout — prints `UNRECONCILED`, names both ends of the move, and
  says that nothing here will undo it. This is also the ONE sweep outcome that
  stops `run` from starting the loop: dispatching roadmap work onto a head
  nobody verified, or pushing work stacked on a merge the remote has never
  seen, is exactly what stopping the sweep exists to prevent. Every other way a
  sweep merges nothing — held, deferred, refused over a dirty checkout, stopped
  on a conflict that aborted cleanly — still reports and lets the loop start.
  The refusal publishes a `parked` heartbeat, not `stopped`: nobody chose it and
  it needs a decision, and staying silent would leave a monitor reading the
  previous run's `running` beat forever.

The gate is checked ONCE, before the first merge, so a shut merge window defers
the **whole** sweep rather than merging part of it and writing one deferral per
branch. And the sweep keeps no queue of its own: the work-list is re-derived
from git ancestry every run, so a sweep that stopped halfway simply
re-enumerates what is left next time.

| Situation | What happens | Transcript entry |
|---|---|---|
| Outstanding branches found | listed before anything is merged | `merge_sweep_backlog` |
| Window shut | nothing attempted at all | `merge_sweep_deferred` |
| Any task the enumeration could not judge | nothing attempted at all; the withheld branches are named | `merge_sweep_held` |
| Each branch that lands | merged + base pushed by `AutoMerger` | `auto_merge_pushed` |
| First branch that does not | sweep halts, remainder named | `merge_sweep_stopped` |
| A stop that left HEAD moved or the tree changed (failed verification, refused push) | reported `UNRECONCILED` with both shas; `run` refuses to start the loop | `merge_sweep_stopped` (`unreconciled`, `base_sha_before_attempt`, `base_sha_after_attempt`) |
| A completed task it could not judge (ref gone, remote unreachable, record unreadable/absent/candidate-less, archive unorderable or superseded) | named, not merged, run does NOT count as clear (exit 1), and the whole sweep is held | `merge_sweep_unresolved` |
| A ref that changed DURING the sweep | that branch and the rest are left alone; the sweep stops | `merge_sweep_publication_changed` |
| Backlog cleared | — | `merge_sweep_completed` |

---

### 3e. Heartbeat + the durable monitor

```bash
bash scripts/install_health_monitor.sh     # one-time; launchd, every 10 min
```

`health` (§3d) is the better check, but it reads the state dir, blockers and
transcript — all inside `~/Documents` here, which **macOS TCC puts out of
reach of a launchd agent** (`getcwd: Operation not permitted`, exit 126, hit
on 2026-08-02). Granting Full Disk Access to `/bin/bash` would fix it and is a
bad trade.

So the loop PUBLISHES and the monitor JUDGES. `heartbeat.json` is written
beside `workers_root` — outside the checkout, like the inbox and the pause
flag — once per phase step, plus `stopped` on a clean exit. The installer
copies a **stdlib-only** checker to `~/.autoloop/` and points launchd at that,
so the agent never touches a protected path and no permission grant is needed.

The split is deliberate:

* **Staleness is the monitor's call.** A loop that hung, crashed or was killed
  cannot report "I am stuck" — it stops writing. Threshold defaults to 45 min,
  because a single-threaded loop is blocked inside an audit fan-out for
  fifteen-plus and cannot beat during one.
* **Everything the loop knows goes in the file.** Blockers, a park, a pause —
  it is alive and aware in each case, and inferring them from silence would be
  slower and wrong (a pause is not a fault).
* **`stopped` is why a clean stop does not page you.** It is judged before
  staleness; otherwise every deliberate stop would look identical to a crash.

Uninstall: `launchctl bootout gui/$(id -u)/com.autoloop.health && rm ~/Library/LaunchAgents/com.autoloop.health.plist`

---

### 3d. `health` — is it working, or stuck?

```bash
python -m autoloop health              # exit 0 = fine, 1 = needs you
python -m autoloop health --json       # machine-readable verdict
```

Read-only and lock-free, so a scheduler may run it at any moment including
mid-round. The exit code is the contract.

Three signals, each chosen against a mistake that was actually made here:

* **The lock, not a process name.** `LoopLock.is_live` is boot-aware and
  authoritative. The loop runs as `autoloop start` OR `autoloop run`, and
  `pgrep -fc` counts PATTERNS rather than processes — both produced confident
  wrong answers on 2026-08-02.
* **Transcript age, not `state.json` mtime.** State is written at phase
  TRANSITIONS, so a healthy loop mid-`executing` leaves it untouched for
  twenty minutes; its mtime reports a working loop as dead.
* **A live agent suppresses the silence alarm.** An audit fan-out runs six
  subagents for fifteen-plus minutes writing nothing. That is the likeliest
  false alarm, so a live agent counts as proof of work.

`scripts/autoloop_health_notify.sh` wraps it for launchd/cron. It `cd`s to the
repo first — `state_dir` is relative, and launchd inherits `/` — and notifies
only on a CHANGE of verdict, so a loop blocked since breakfast does not
produce forty identical alerts. A check that itself fails still notifies:
a monitor that goes quiet when it breaks is the worst kind.

---

### 3c. Three recovery commands for interrupted work

```bash
python -m autoloop release <task-id>                     # in-progress -> pending
python -m autoloop archive-blocker <id> --reason "..."   # close a dead blocker
python -m autoloop retire <task-id> --superseded-by <id> # superseded, for good (§9d)
```

The first two put work BACK; the third takes it out. `release` says "this round
was interrupted, run it again"; `retire` says "this will never run again,
because it already happened under another id". Reaching for the wrong one is
recoverable in only one direction, which is why `release` refuses anything that
is not in-progress and `retire` refuses only completed work.

**`release`** returns a task stranded IN-PROGRESS to pending. A task is marked
in-progress at dispatch and cleared when the round finishes; a `loop_fatal`
park in between finishes nothing, so `state_of` reports IN_PROGRESS,
`next_ready` skips it forever, and no command could move it — `unblock`
correctly refuses anything that is not `blocked`.

It clears **three** things, not one: the STATUS; the stale WORKER REPO, which
would otherwise make the next dispatch refuse (`create()` will not write into
an existing directory); and the EXECUTION RECORD, which would otherwise keep
claiming a live unpublished `candidate_sha` for a task that is back in the
queue and will be redone from scratch. That third one was silently left behind
until 2026-08-15: releasing 25 stranded tasks the day before left 14 records
pinned to the pre-merge HEAD, `merge-window` held the window shut on every one
of them, and it could not reopen by itself — each of those tasks would have had
to be re-dispatched *and* re-published first. With `auto_merge_enabled` on, the
next task to complete published and then logged `auto_merge_deferred "merge
window closed"`, and the published-but-unmerged backlog began rebuilding
silently. An operator archived the 14 records by hand.

Nothing is deleted. The worker moves to `quarantine/<task-id>-<label>` (an
interrupted round usually holds real work) and the record to
`.autoloop/executions/archive/<task-id>-<label>.json`, **under the same label**,
so the two halves name each other and the candidate stays recoverable.
`worktask.retire_execution` does both in one call precisely so they cannot
drift apart.

**`archive-blocker`** closes a blocker whose session has been retired. Some
blockers cannot be answered at all — `checkout_escape_detected` refuses every
answer by design, since a text reply would fabricate exactly the human
confirmation it exists to demand, and its message says to archive the session
instead. But that left the blocker RECORD open, `start` refuses to run with an
open blocker, and nothing on the CLI could close it. It writes
`archived_reason`, never `answer`, and it REFUSES a blocker belonging to the
session that is still live — otherwise it would become the "clear the escape
detection" button the precondition table deliberately withholds.

**`retire`** is documented in full in §9d, with the six tasks it was written
for. In short: it is the only way to say that work is superseded rather than
stuck, it records the successor id(s) in `Task.superseded_by` so the chain is
machine-readable, and it deletes nothing — including the original
`blocked_reason`.

---

### 3b. `start` — the one command to come back to

```bash
python -m autoloop start              # repair, report, then run continuously
python -m autoloop start --check-only # repair and report, do not run
```

**A start-time command, deliberately not a pre-stop one.** Stopping is already
clean (§3a): SIGTERM and SIGHUP release the lock, `pause` finishes the current
phase, every state write is atomic and fsynced. And a pre-stop repair would be
unreliable by construction — the cases it exists for (a power cut, a crash, a
panic) are exactly the ones that never give you the chance to run it. Recovery
has to work from evidence left behind, not from cooperation before the fact.

It repairs only what is decidable from evidence, and reports the rest:

| Condition | What `start` does |
|---|---|
| Lock held by a LIVE process | says "already running" and exits 0 — not a fault, and it touches nothing else |
| Lock whose owner is provably dead | removes it (boot-aware, §3) |
| CDP not answering | runs `browser.restart_command`, then re-probes |
| CDP silent, no restart command configured | refuses — never infers which Chrome to kill |
| Pause flag set | clears it: `start` is an explicit request to run |
| Open blockers | prints each with its exact `answer` command, and stops |
| Session parked `task_fatal`, no open blocker | starts — continuous mode quarantines that task and carries on by itself |
| Session parked `loop_fatal`, or `failed` | prints the question and its recovery command, and stops |

**What it will never do**, and the reason the list is short: archiving an
execution record discards the link to a reviewed candidate, quarantining a
worker repo moves the only copy of a branch, and "resolving" a blocker means
answering a question nobody has read. Those need a judgement, and a repair
command that guesses at them is worse than none — because it looks like it
worked. Everything above the line is decidable; everything below it is yours.

---

### 3a. Stopping the loop, and losing the machine

`SIGTERM` and `SIGHUP` release the lock **inside the signal handler**,
before unwinding. Python's default action for both is to die without
running `finally`, so before this the tidy-looking way to stop a run (a
shutdown, a logout, plain `kill`) was the one that left a lock behind,
while Ctrl-C — which raises `KeyboardInterrupt` and unwinds — was clean.
The release must happen in the handler rather than by unwinding: a SIGTERM
arriving mid-fan-out unwinds into `ThreadPoolExecutor.shutdown(wait=True)`,
which waits on agents that run for minutes, and a shutdown's grace period
is seconds. Child agents are deliberately left to exit on their own.

This lives on `LoopLock` (installed by `acquire`, restored by `release`),
not at one call site, so it covers every holder listed above rather than
whichever ones a wrapper was remembered on — `smoke-browser` drives a real
browser and `review-changeset` waits on a reviewer, both long enough to be
running when a machine goes down.

What each way of stopping costs:

| How it stops | Lock | State | In-flight work |
|---|---|---|---|
| `pause` | released | consistent | none — finishes the current phase first |
| Ctrl-C / SIGTERM / SIGHUP | released | consistent | the current step |
| SIGKILL / power cut | left behind, `unlock` clears it | consistent | the current step |

"State consistent" is not a hope: `StateStore.save` writes a temp file,
**fsyncs it**, renames, then fsyncs the directory. Temp-file + rename alone
is atomic against a killed process but not against a killed machine — the
rename can reach disk while the data blocks it points at have not, leaving
a truncated state file after a power cut. Directory fsync is best-effort
(some network mounts refuse it) and never fails a save that otherwise
succeeded.

"In-flight work" is a whole step, not a partial one. The audit fan-out
writes each domain's raw output only after **all** agents return
(`executor.py:_run_agents`), so losing a run mid-fan-out loses all six
domains, not the one in progress. Nothing is corrupted; the step is
re-dispatched on resume.

---

## 4. Task-owned change manifests (RETIRED 2026-07-30 — kept for reference)

> **This whole section describes the authorize-then-produce commit path,
> which was RETIRED, not fixed — docs/SECURITY.md S21 ("closed by
> retirement"). `orchestrator.py`'s `_dispatch_git` (the only caller of
> `verify_commit`/`GitGateway.commit()`) is gone; produce-then-review (§4b)
> is the only commit path left, for audit AND implement/revise alike.
> `manifest.py` and `commit_adopted` (below) are kept, unmodified, because
> `test_manifest.py`/`test_git_gateway.py` still exercise them directly as
> standalone primitives — see docs/SECURITY.md S22. Do not read the
> present-tense wording below as describing current dispatch behaviour; it
> is preserved as documentation of code that still exists but has no
> production caller.**

A commit approval must name **exact paths**, and every approved path must be a
file the last-executed task actually created, modified or deleted:

* `commit.paths` is required and non-empty at the **contract** level (v3).
* `manifest.verify_commit` refuses paths that were dirty before the task
  started (pre-existing human work) or that the task never touched.
* `GitGateway.commit` stages exactly the approved paths (`git add -- ...`),
  verifies the index path-for-path (unstaging any surprise via
  `git restore --staged`), records the staged diff summary in the transcript,
  then commits. Idempotent after a crash.
* The policy whitelist no longer contains `-A` and requires `--` for `add` —
  there is **no configuration escape hatch** that restores stage-everything.

### Adopted manifests — committing work the loop did not create

Work already in the tree (made by a human, or by the lead outside the loop) has
no executor provenance, so the rule above can never pass for it. An **adopted**
manifest binds such work by **content** instead:

* the caller names **every** path explicitly — the list is never inferred from
  the dirty tree, so an unrelated edit cannot be swept in;
* each path is recorded with a SHA-256 of its approved content **and an
  explicitly stated git mode** (`100644` or `100755`) — never inferred from the
  working tree, because identical bytes can be committed either way and the
  executable bit is a privilege change;
* a commit succeeds only if every requested path is adopted **and** still
  hashes to the approved value;
* the commit is built from an **immutable verified tree**, never by
  `git commit` (see "Why not `git commit`" below);
* **symlinks are refused at adoption**, including paths under a symlinked
  directory: reading a file follows the link while git stages the link itself
  (mode `120000`, blob = the target path string), so the reviewed bytes would
  not be the committed bytes. A symlink that somehow reached a manifest is
  refused again by the staged-mode check;
* clean paths, deletions, directories, duplicates, absolute paths and `..`
  escapes are all refused at adoption time.

This is stronger than the executor rule, not weaker. Provenance ("did the task
create this?") is a proxy for what matters — reviewedness ("was *this content*
approved?"). Adoption checks the real property: an executor-owned path is
committable today even if its content changed after review; an adopted path is
not.


#### Why not `git commit`

Three rounds of review each found the verification reading something mutable.
The progression is worth keeping, because each fix looked sufficient:

| verified | defeated by |
|---|---|
| working tree, before staging | swap after the check: `git add` stages other bytes |
| working tree, after staging | *swap-and-restore*: stage altered bytes, put the approved bytes back on disk — the file looks untouched while the index holds the attacker's content |
| the index, after staging | a **pre-commit hook**: `git commit` runs hooks *after* any check, and a hook can rewrite the index. Reproduced: the index check passed, the hook replaced the approved file's bytes **and** staged an extra file, and both landed in the commit |

Only an object id names fixed bytes, so the adopted path commits a **tree**:

1. refuse if any commit hook is active (never bypassed, never emulated);
2. require a symbolic branch HEAD; record the branch ref and original HEAD;
3. stage exactly the approved paths — the index must equal that set;
4. `write-tree` → candidate tree;
5. verify **that tree** entry by entry as a complete tuple — exact path,
   expected mode, object type `blob`, and content hashing to the adopted
   SHA-256 — plus a changed-path set versus the parent tree that is exactly the
   approved set. Symlinks, submodules, trees and unknown modes are refused;
6. `commit-tree` with the original HEAD as the single parent;
7. read the commit object back: tree id, single parent, exact message bytes,
   and a non-split author/committer identity;
8. re-check the hook state (a hook installed, or `core.hooksPath` repointed,
   after step 1 must not apply to this commit), then `update-ref`
   **compare-and-swap** against the original HEAD — a branch that moved
   meanwhile fails without overwriting it;
9. confirm HEAD resolves to that commit and carries that exact tree.

**No ref moves and no history is published before step 8.** Step 6 does write a
commit object, so a failure after it — a lost CAS — leaves an *unreachable*
commit in the object database, which `git gc` prunes. That is reported
explicitly rather than described as "nothing irreversible", which would be
inaccurate. Residual staged or working-tree changes are **reported, never
reset** — discarding a human's work to tidy up would be worse than leaving it.

**Path handling is NUL-delimited** (`-z` on `status`, `ls-tree`, `diff-tree`,
`ls-files`, `diff --cached`). Without it git quotes and escapes paths containing
spaces, tabs or non-ASCII, and a pathname-keyed security check would compare the
wrong string.

**Identity rule:** the created commit's author and committer must both be present
and agree on their `Name <email>` part; timestamps may differ. `commit-tree`
derives both from the same configuration, so a divergence means the environment
overrode one. No signing is required or checked.

**Guarantee boundary:** these checks are process-local. Arbitrary concurrent
mutation of git configuration, hooks or the object database by an external
hostile process is outside what they can promise.

**Hook policy — fail closed, never bypass.** The effective hooks directory is
resolved by git itself (`rev-parse --git-path hooks`, so `core.hooksPath` is
honoured), and the four hooks an ordinary non-amend commit can invoke are
checked: `pre-commit`, `prepare-commit-msg`, `commit-msg`, `post-commit`. A hook
counts as active when git would treat that exact path as executable (executable
symlinks included). If any is active the adopted commit is **refused** with the
hook names and directory — the hooks are neither run nor skipped. `*.sample`
files never block, and a configured `core.hooksPath` with no active hooks is
fine. The consequence is honest: in a repo with commit hooks, content-bound
authorization is not achievable through this path, because a hook can rewrite
the tree after it is approved.

Amend, merge commits and detached HEAD are unsupported on the adopted path.

**The integrity chain.** Hashes are deliberately **not** carried in the
directive — a directive is model-authored text, and integrity values inside it
could diverge from the reviewed report. Instead:

```
content → manifest.adopted[path] = sha256          (recorded at adopt time)
        → render_adoption_block() inside the review payload
        → report_sha256 = sha256(payload)          (covers the hash table)
        → manifest.presented_report_sha256          (stamped only when the
                                                     payload really carried it)
        → the `reviewed` stamp must echo that report_sha256 (verify_review)
        → commit: presented report == answered report, every path adopted,
          every hash still matching — then re-verified on the immutable TREE
```

A stale approval cannot be replayed: it answers a different `report_sha256`, so
the presented-report comparison fails. A payload that omits the block binds
nothing, and any approval answering it is refused as "never presented".

**Trust boundary.** The orchestrator does not refuse to *send* a payload lacking
the block — doing so would deadlock error re-prompts, which legitimately carry
no table. It simply never binds such a report, and the commit gate refuses. So
the guarantee is: *no commit without an approval that answered a report which
provably contained the exact hashes of the exact files being committed.*

**Residual limitation.** Adoption authorizes content, never publication —
`allow_push`, protected branches and remote behaviour are untouched. And nothing
creates an adopted manifest in production yet: `ChangeManifest.adopt(...)` is the
API, but no caller in this repo invokes it — it stays available for whatever
adopts pre-existing human/lead work later. This is a *different* mechanism from
produce-then-review (§4b below): adoption binds work the loop did not create by
content hash; produce-then-review is for work a task's own executor produced,
verified from the immutable commit it made. Do not confuse the two, and do not
resurrect an archived "precommit-review" design under either name — that design
predates produce-then-review and was superseded by it, not merged into it.

Files changed *during* the task window by someone else are indistinguishable
from task work and therefore count as task-changed — they are still only
committable if ChatGPT explicitly approves those paths. Don't edit the tree
while an executor task is running.

---

## 4b. Produce-then-review: per-task worktrees and the post-commit review packet

A second, structurally different commit path for real (non-audit) tasks —
implemented across three passes (2026-07-30): pass 1 (`git_gateway.py`,
`worktask.py`) the honest-sha commit primitive and `push_exact`; pass 2a
(`worktree.py`, `orchestrator.py`) per-task worktrees and the structural
post-commit verification gate; pass 2b (`packet.py`, this section) the review
packet, the request/response binding that pins a push to the candidate it
reviewed, and push routing. Gated behind an optional
`worktrees`/`execution_store`/`intent_store` constructor triple on
`Orchestrator` — every existing caller (including `cli.py`, unchanged) takes
the §4 manifest path unaffected. Not wired into `cli.py` yet.

**Why a second path at all.** §4's manifest gate authorizes a commit BEFORE it
exists, from a snapshot diff ChatGPT approved sight-unseen of the real diff.
Produce-then-review inverts that: the executor commits immediately to its own
branch in its own linked worktree (`WorktreeManager`, one branch
`autoloop/<task_id>` and one directory per task — git itself refuses to check
the same branch out twice, so two attempts at the same task can't race), a
structural gate re-verifies the resulting IMMUTABLE commit, and only then is
it shown to ChatGPT as a real diff for a real, already-existing commit.
Nothing here can roll a bad commit back — `reset`/`checkout`/`clean` are not
on the git whitelist — so every refusal in this path means "park and report",
never "undo".

**Sequence per task (`Orchestrator._dispatch_task_postcommit`).**

1. First dispatch: `task_base_sha` = the MAIN checkout's HEAD, worktree +
   branch created off it (`TaskExecution`, `worktask.py`).
2. A pending `CommitIntent` from a previous crash is reconciled FIRST
   (`reconcile_after_crash`, F8 — see `worktask.py`'s module docstring) —
   `RECOVERABLE` adopts the branch tip without re-committing, `AMBIGUOUS`
   parks for a human, `NO_COMMIT` clears the stale intent and proceeds fresh.
3. Round cap: `execution.review_round >= 2` refuses a third round outright —
   see below.
4. Environment snapshot (`environment.snapshot`) BEFORE the executor runs, so
   a hook installed mid-task (e.g. a dependency postinstall script) is
   caught, not silently trusted.
5. The executor runs; a non-`ok` outcome never reaches the commit step.
6. `commit_and_capture` writes the `CommitIntent` durably, stages exactly the
   reported `changed_paths`, runs a NORMAL `git commit` (hooks enabled — the
   diff a hook produces is what gets reviewed, not what was staged before it
   ran), and reads the candidate sha from `rev-parse HEAD` — never predicted.
7. `_verify_committed` (the structural gate): candidate is a descendant of
   `task_base_sha`; the commit range is non-empty; every touched path is in
   `execution.allowed_paths` (the UNION of `changed_paths` across every round
   so far — comparing against only the LATEST round's paths would wrongly
   flag an earlier round's legitimate paths as "outside" once
   `review_round > 0`, since `commit_range_paths(task_base_sha,
   candidate_sha)` spans every round); the worktree is clean after commit;
   the task's own `execution.validation_commands` (falling back to
   `config.audit.validation_commands` when it declared none) re-run against
   the committed tree — pre-commit validation is not enough, a hook can
   change committed content after the executor last saw it. **Every pytest
   command in that list runs with `-n auto -p no:cacheprovider`** (val-01,
   2026-08-06): `validation.effective_validation_commands` adds them on the
   way to the subprocess, so a config copied before the flags existed, a
   task's declared `validation`, and a command list persisted by an earlier
   session all get them. A command selecting the `isolated` marker is left
   serial — that marker means "its own process" — and one that already states
   a worker count is left exactly as written. The summary is still one
   `PASS`/`FAIL` line per command, naming the command that really ran.
8. On success: `packet.build_review_packet` renders the packet and it is sent
   for review (state re-enters `ready`, it does not park). On failure: parks
   in `needs_user` with every reason; the commit is not rolled back.

**The review packet (`packet.py`).** Rendered ONLY from immutable git objects
in `task_base_sha..candidate_sha`: the commit list (sha/subject/parents),
every changed path with its mode and object type on each side of the range
(read from the two trees directly, not inferred from the diff), the diff
stat, and the full diff (`range_diff` — plumbing-rendered, no external diff
driver or textconv filter, refuses above a byte cap rather than truncating).
`task_id`, `branch`, `base_sha` and `candidate_sha` are stamped into the
returned text as literal lines — load-bearing, not decoration: that string
becomes `state.outbox`, and `context.report_sha256` hashes exactly those
bytes. Two different commits can produce byte-identical diff TEXT, so a
digest over the diff alone would not pin which commit was reviewed; the four
identifiers inside the hashed body are what make an approval of candidate A
structurally unable to authorize publishing a swapped-in candidate B.

The packet inlines the WHOLE patch, however large — that string is the logical
packet, and bounding it here would mean the hash covered less than the review
did. What a single chat message can carry is a separate question, answered at
DELIVERY: a patch too large for one message is sent as numbered parts before
the message asking for a verdict, and omitted (loudly, never truncated) only
when that cannot be done. See §5d-bis.

**The one unread section, and the assumptions inside it.** `packet.
_format_executor_report` renders what the executor SAID about the round
(`TaskExecution.report_summary` / `report_details`), labelled a CLAIM in the
heading because every other section is read from git. Since `ask_user` was
retired it also renders `TaskExecution.assumptions` — the readings the executor
CHOSE where the task did not say. An ambiguous task can no longer stop the run
to ask, so `implement_executor` instructs the agent to take the **smallest
reversible reading** and to write one `ASSUMPTION:` line per choice; those
lines are collected from the agent's own output — the declaration form only,
first on its line, so neither prose about the convention nor a quoted or
bulleted echo of the instruction itself becomes a disclosure nobody made —
accumulated across rounds
(union, first-seen order — a round-2 executor assuming nothing must not erase
what round 1 assumed and shipped) and shown here. **This is not first-time
visibility** — `report_details` is the whole agent transcript, so the lines
were already in the packet somewhere. What the section adds is the LABEL (a
reviewer skimming a transcript cannot miss the choices that most need judging)
and the ACCUMULATION (`report_details` is replaced every round; these are
unioned, so round 1's assumption survives into the review of a range that still
contains its code). **Bounded only when RENDERED**, on two axes and both in the
PACKET: `packet.ASSUMPTIONS_MAX_CHARS` (4,000) caps the section, dropping the
OLDEST entries — each was shown in the packet for the round that made it — and
`packet.ASSUMPTION_MAX_CHARS_EACH` (500) shortens a single over-long line,
without which one pasted-reasoning entry would consume the section budget and
hide every real disclosure behind a withheld count. Both say what they did,
never silently. The RECORD is never truncated: that would delete evidence out
of the file crash-recovery reads, to solve a problem that only exists at render
time — and because `report_details` is REPLACED every round, a line dropped on
the way to the record has no other copy left once the next round commits. (This
is the shape after review on 2026-08-16; the first cut also capped what one
round could contribute, 20 lines of 500 characters, which bounded a chat
message by editing the durable record.) They inform the
reviewer's judgement and nothing else: scope is still checked with
`commit_range_paths` against `allowed_paths`, and validation by re-running it —
a sentence here cannot widen either. A record written before this field existed loads as "none
recorded", and an EMPTY list renders no section at all (unlike an empty
report, which says so): the absence of assumption lines is not a claim that
the task was unambiguous, and printing "none recorded" would invite reading it
as one.

**Request/response binding (`state.PostcommitBinding`).** A dedicated field
on `PendingRequest`/`LastResponse` — never `last_manifest_id`, which belongs
to the §4 manifest path and means something different — captured once, when
the packet is actually sent, and carried through response handling
unmodified: `task_id`, `task_branch`, `base_sha`, `candidate_sha`,
`candidate_tree_sha` (the candidate's tree object id, for a push-time
tamper check) and `packet_sha256`. `Orchestrator._current_pending_postcommit`
binds a request ONLY when its payload actually carries all four identifiers
as literal substrings (mirroring the §4 adoption block's own carries-check) —
a corrective re-prompt or any other payload legitimately carries none of
this and must bind nothing. Everything downstream (push routing) reads the
candidate sha from this binding alone — never from a fresh
`TaskExecutionStore` lookup ("latest" state can have moved on to a new round
by the time an approval arrives) and never from the directive (a `push`
directive cannot even carry a task_id — see `contract._forbid`).

**Push routing (`Orchestrator._dispatch_task_push`, `GitGateway.push_exact`).**
There is no ambient `push()` anymore (removed 2026-07-30 — it pushed
whatever the current branch tip happened to be, exactly the
wrong-destination race this path exists to close). A `push` decision bound to
a postcommit review routes to `_dispatch_task_push`, which: refuses unless
the FRESH `TaskExecutionStore` record still shows the SAME candidate as the
binding (a later round having advanced it is a refusal, not "push the old
one anyway"); refuses unless the candidate is still a descendant of
`task_base_sha`, still resolves, and its tree still matches
`candidate_tree_sha`; then publishes via `push_exact` to
`refs/heads/<task_branch>` on `execution.intended_remote` (default
`"origin"`) — an explicit, already-resolved `<sha>:<dest_ref>` refspec, never
a bare branch push, rejected outright if `dest_ref` is protected. Before
pushing, `remote_ref_sha` is checked first: if the remote already has this
exact candidate (a push that landed before a crash, or an ordinary retry),
nothing is re-pushed. `authorize_directive`'s protected-branch check is
evaluated against `resp.postcommit.task_branch` for a postcommit-bound push —
NOT the main checkout's current branch, which would otherwise make every
produce-then-review push evaluate against the wrong name (denying it
whenever the main checkout happens to sit on `main`/`master`, the opposite
of what `protected_branches` is meant to gate). `push_exact`'s OWN
protected-ref check has no `allow_protected_push` escape hatch by design
(see its docstring) — `_dispatch_task_push` therefore passes an EMPTY
protected-refs tuple to it when `allow_protected_push` is true, so that
policy knob stays meaningful instead of `authorize_directive` approving a
push that `push_exact` then silently refuses anyway.

**`commit` and `commit_and_push` (and any `push` not bound to a
produce-then-review candidate) are refused outright (2026-07-30).** There is
nothing new to commit in this path — the commit already exists, made
automatically once validation passed — so ChatGPT is never expected to send
these decisions anymore, and if one arrives anyway it is denied through the
same budget-capped `policy_denied` corrective-reprompt machinery as any
other policy violation (`_dispatch`'s `legacy_git_path_retired` denial),
never routed to an executor and never able to publish "whatever the current
branch is". This supersedes what used to be a narrower, conditional guard
inside the now-removed `_dispatch_git` (which only refused a *stale or
unbound* response while a live candidate was on record) — the replacement is
unconditional, so there is no longer a scenario where a legacy commit/push
decision succeeds at all, regardless of what `state.task_execution` shows.

**Revision rounds.** `revise` re-enters the same worktree, keeps the ORIGINAL
`task_base_sha`, and produces a NEW commit on top of the current candidate —
`review_round` increments each time `_finish_postcommit` runs, success or
failure. Maximum two rounds: round 1 is the initial `implement`, round 2 is
one `revise`. A third round (`review_round >= 2` at the top of
`_dispatch_task_postcommit`) never reaches the executor or ChatGPT —
`_park_round_cap` parks immediately in `needs_user` with BOTH the full
accumulated diff (`task_base_sha..candidate_sha`) and the latest round's own
diff (the previous round's tip, derived from `commit_list` since each round
is exactly one commit — not a separately persisted field), plus the feedback
that triggered the third attempt.

---

## 4c. Worker/publisher separation (Autoloop M2)

`worker_env.py` + `publisher.py`, 2026-07-30. M1's produce-then-review path
(§4b) closed the "review before publish" race but left a structural gap a
review flagged: a task's own linked worktree (`WorktreeManager`, §4b) SHARES
its `.git` directory — every remote, every hook, every credential-relevant
config key — with the main checkout it was created from. Code running inside
that worktree therefore has the SAME ordinary git access to the real origin
as the checkout it was linked from, regardless of what the produce-then-review
gate does afterward. M2 closes this STRUCTURALLY by separating two concerns
that M1 left conflated: the environment a worker's own git commands run
under, and the repository that actually publishes.

**THREAT MODEL — read this before trusting any claim below.** Git and the OS
are TRUSTED; the process sandbox is the boundary these modules defend.
Arbitrary sandbox escape, direct credential theft (reading an unencrypted SSH
key off disk), or a worker simply running arbitrary shell code that does
whatever it likes are OUT OF SCOPE and are not claimed to be closed. What IS
guaranteed: a worker using git NORMALLY — inheriting whatever a naive
`subprocess.run` would hand it — cannot discover or use a push destination,
credential helper, or hook through ORDINARY (inherited/ambient) git
configuration. See the module docstrings in `worker_env.py` and
`publisher.py` for the full statement; both restate it, deliberately, so
neither can be read in isolation and overclaim.

**The worker side (`worker_env.py`).**

- `worker_env(base_env=None) -> dict` — the environment mapping a worker's
  git subprocesses should run under. Removes `SSH_AUTH_SOCK`, `SSH_ASKPASS`,
  `GIT_ASKPASS`, `GIT_SSH`, `GIT_SSH_COMMAND`, and every OTHER `GIT_CONFIG*`
  var the parent had, then forces `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_TERMINAL_PROMPT=0`. **Platform trap,
  verified empirically (Apple Git 2.39.5, macOS 26.2):**
  `GIT_CONFIG_SYSTEM=/dev/null` does NOT suppress Apple's second,
  compiled-in system gitconfig
  (`/Library/Developer/CommandLineTools/usr/share/git-core/gitconfig`, which
  sets `credential.helper=osxkeychain`) — only `GIT_CONFIG_NOSYSTEM=1` does.
  `HOME` is never repurposed (the brief this shipped against called that out
  explicitly as a scoped-controls-only boundary).
- `WorkerRepoManager` — creates one isolated, no-remote repository PER TASK
  (`git init`, never `git worktree add`), seeded with the task's base commit
  via a ONE-TIME local-path `git fetch <source-path> <base-sha>` — never a
  persistent configured remote — with `core.hooksPath` pointed at a
  dedicated, empty, controlled directory it creates. This is why it is a
  genuinely separate repository rather than a `WorktreeManager` linked
  worktree: a linked worktree cannot be given its own remotes or hooks
  independent of the checkout it was linked from; a fresh `git init` can.
- `verify_worker_isolation(git, expected_hooks_dir=None) -> list[str]` —
  violations (empty = clean) for: any configured remote/pushurl,
  `url.*.insteadOf`, `push.followTags`, `remote.*.mirror`, any credential
  helper, `core.hooksPath` not pointed at the expected controlled directory,
  or any ACTIVE (executable) file anywhere in the EFFECTIVE hooks directory
  — enumerated directly, not limited to git's named hook set, because a
  `WorkerRepoManager` repo's controlled hooks directory is created EMPTY, so
  the correct state is zero files, named or not. **`git` MUST be
  constructed with `env=worker_env()` (or via `WorkerRepo.gateway(policy)`)
  for this to mean what it claims** — verified: a `GitGateway` with no
  explicit `env` reports the CALLING process's ambient config, not the
  worker repo's isolation, because `git config --get-regexp` resolves
  whatever environment the subprocess itself runs under.
- `describe_policy(worker=None) -> dict` — diagnostics of the APPLIED policy
  (forced env vars, removed patterns), never a dump of an actual environment
  or config file, so there is nothing secret to redact.

**The publisher side (`publisher.py`).**

- `provision_publisher_repo(state_dir, source_git, remote="origin") -> Path`
  — idempotently creates a BARE repo at `state_dir/publisher.git` with
  `core.hooksPath` pointed at `state_dir/publisher-hooks` (created empty,
  never wiped if it already has content — left for `Publisher`'s own
  construction check to refuse loudly) and `remote.<remote>.url` copied from
  `source_git`'s own configured url. Uses direct `subprocess.run` for
  `init --bare` / config writes — these are parent-process provisioning
  steps, never directive-driven, so routing them through the policy
  whitelist (which exists to gate what a MODEL-AUTHORED directive can reach)
  would be pointless.
- `Publisher(repo_root, remote, policy, runner=None)` — construction runs
  the same structural pre-flight `push_exact` runs at push time (single url,
  no pushurl, no mirror, no followTags, no insteadOf) plus a
  hooks-directory-emptiness check, and refuses to construct if any fail.
  These are belt-and-braces, not what actually closes them —
  `GitGateway.push_exact` (§4b) is, unconditionally, on every call.
  - `import_candidate(worker_repo_path, candidate_sha) -> str` — fetches
    `candidate_sha` (a literal 40-hex id, never a ref) from a LOCAL
    filesystem path via the new `GitGateway.fetch_object`, then verifies via
    `read_commit` that the imported object is exactly that id and IS a
    commit (`read_commit` runs `cat-file commit <oid>`, which itself fails
    on anything else — no separate `cat-file -t` step, since `cat-file` is
    policy-whitelisted with an empty flag set). No local ref is created;
    the object is anchored only via `FETCH_HEAD`. Repeating the import is
    harmless (verified against a real repo). A later worker HEAD moving on
    does not affect a candidate already imported by exact sha.
  - `publish(candidate_sha, dest_ref, protected_refs, expected_url=None) ->
    str` — never substitutes worker HEAD, publisher HEAD, a branch name, or
    a fresh lookup for `candidate_sha`; the caller (the orchestrator) is
    responsible for sourcing it from the reviewed request binding. Calls
    `GitGateway.push_exact` — reused, not reimplemented.
  - `remote_ref_sha(dest_ref) -> str` — a fresh `ls-remote` round-trip, used
    to reconcile after a crash (a push that landed but whose confirmation
    was never seen) without re-pushing, and to check idempotency before
    calling `publish` again.
  - `describe() -> dict` — diagnostics with userinfo (`user:token@host`)
    stripped from any url before it is ever returned.

**New `GitGateway` surface this required.** `fetch_object(source_path,
want_sha)` — `git fetch <source_path> <want_sha>`, policy-whitelisted
(`policy.py`'s new `"fetch"` entry) with F2-style checks: `want_sha` must be
a literal 40-hex id with no `':'`/`'+'`, `source_path` must be an absolute
local filesystem path with no URL scheme or scp-like `user@host:path`
syntax. `GitGateway.__init__` also gained an optional `env: dict | None`
parameter (default `None` = inherit the current process environment,
identical to every construction site that predates this) — without it, a
`GitGateway` rooted at a worker repo would run its subprocesses under the
CALLING process's own environment, defeating `verify_worker_isolation`
entirely; `WorkerRepo.gateway(policy)` is the convenience that applies
`worker_env()` correctly.

**Wiring (2026-07-30 — production, unconditional; `config.workers_dir` below
was replaced by the externally-validated `config.workers_root` in the
hardening pass documented in §4e).** `Orchestrator` takes
`publisher: Publisher | None = None` and `worker_repos` alongside
`worktrees`/`execution_store`/`intent_store`; `cli.py`'s `_build_orchestrator`
— what the plain `run` command always calls — constructs ALL of them: a
`WorkerRepoManager(config.workers_root, config.worker_hooks_dir)`, a
`TaskExecutionStore`/`IntentStore` under `config.executions_dir`/
`config.intents_dir`, and a `Publisher` from `provision_publisher_repo
(config.state_dir, git)`. `worker_repos is not None` takes precedence over
`worktrees` in `_dispatch_task_postcommit` (§4b), and production never sets
`worktrees` at all — every task, audit included, runs in a genuinely
isolated `WorkerRepoManager` repo, and `_dispatch_task_push` always imports
from it into the `Publisher` repo before publishing. There is no
configuration under which the CLI falls back to a shared-`.git` linked
worktree or a direct worktree-to-remote push.

**What this closes end to end, now that it is wired:** a task's
implementation work — real `implement`/`revise` once a code-editing executor
exists, and the audit unconditionally today — runs in a repository with no
configured remote and no ambient credential access (`worker_env`), and
publication happens only through the separate, hooks-controlled `Publisher`
repository, over an object it re-verifies by exact id, never a fresh lookup.
`WorktreeManager` (the M1 linked-worktree lifecycle) remains in the codebase
and is still directly tested, but is not what production `_build_orchestrator`
constructs.

---

## 4d. Publisher URL policy (v1: a provision-time snapshot)

`provision_publisher_repo(state_dir, source_git, remote="origin")` records
`source_git`'s configured `remote.<remote>.url` into a persisted snapshot
(`state_dir/publisher_url.json`, via `publisher.read_publisher_url_snapshot`
/ the internal `_write_publisher_url_snapshot`) **only on the first-ever
call** for a given `state_dir`. Every call after that re-asserts the
SNAPSHOT's value into the publisher repo's own git config — never a fresh
read of `source_git` — so a later change to the main checkout's own `origin`
is **never picked up silently**.

* **`Orchestrator._dispatch_task_push`** compares the main checkout's LIVE
  `remote.<remote>.url` against the snapshot immediately, before doing
  anything else. A mismatch parks (`needs_user`) naming the exact reprovision
  command, rather than publishing to a destination nobody re-confirmed.
  `Publisher.publish` is also given `expected_url=<snapshot>` as
  belt-and-braces — `push_exact` re-checks it against the PUBLISHER repo's
  own configured url immediately before pushing (catches the publisher
  repo's config being tampered independently of the main checkout).
* **`doctor`** (§6) reports the same comparison as the `publisher_url_drift`
  check — `ok` when the snapshot matches the main checkout's current config,
  `fail` (naming the reprovision command) on a mismatch, `warn` if no
  snapshot exists yet (the preceding `publisher` check provisions one in the
  same run).
* **`python -m autoloop reprovision-publisher --confirm`** is the ONLY way
  the snapshot changes after its first write (`publisher.
  reprovision_publisher(state_dir, source_git, remote, confirm=True)` —
  `confirm` has no default that makes it callable by accident). It re-reads
  `source_git`'s CURRENT url, overwrites the snapshot, and re-provisions the
  publisher repo to match. It takes the single-instance lock (§3), like
  `run`/`reset`. Nothing in `orchestrator.py`'s dispatch path, or anywhere
  reachable from a ChatGPT directive, calls this function — see
  `autoloop/tests/test_v1_smoke.py`'s structural assertion.
* Credentials are never exposed: any url containing embedded userinfo
  (`https://user:token@host/...`) is redacted (`publisher.redact_url`) before
  it reaches `status`, `doctor`, a parked question, or `Publisher.describe()`.

---

## 4e. External worker location, escape detection, non-circular task
scope, and bounded attempts (post-M2 hardening pass)

An adversarial review of §4c's M2 isolation found four gaps that survived
it — all four closed here. **Naming note:** this pass is tracked externally
as "M1" of a separate postcommit-review effort (worktree/branch name); that
label is UNRELATED to this document's own "M1" (§4b, produce-then-review)
and "M2" (§4c, worker/publisher separation) milestones, which predate it.
Code comments in this codebase refer to the four findings below by number
(#1/#2/#3/#7, matching the review that reported them), not by milestone
letter, specifically to avoid that collision.

**Finding #1 — the worker repo lived inside the tree it was supposed to be
isolated from.** `WorkerRepoManager`'s isolation (§4c) is entirely about
GIT CONFIGURATION (no remote, no hooks, scrubbed credential env) — it says
nothing about the worker repo's FILESYSTEM LOCATION. Before this pass,
`cli._build_orchestrator` constructed it at `config.workers_dir`
(`state_dir / "workers"`), which defaults to `.autoloop/workers` — INSIDE
the checkout. A write there is invisible to `git status`-based verification
scoped to the checkout, and a write-capable agent's argv carries no
`--add-dir`/path-jail flag at all — confinement was a sentence in the
prompt, nothing structural.

Fix: `config.workers_root`, a REQUIRED, absolute `[paths]` key with no
default (`config.example.toml` ships `~/.autoloop/workers`).
`load_config` refuses a missing, blank, or relative value; `worker_env.
validate_workers_root(workers_root, repo_root, state_dir) -> list[str]`
(called from `cli._build_orchestrator` — refuses execution — and from
`doctor.run_doctor` — a `fail` check named `workers_root`) refuses one
nested beneath the checkout, its `.git` (including, if `.git` is a linked
worktree's gitdir POINTER FILE — verified against this very worktree during
this pass — the REAL git-common-dir it resolves to, not just the literal
`.git` path), the state dir, or either publisher path
(`publisher_repo_path`/`publisher_hooks_path`). `config.workers_dir` is kept
as a property, but nothing creates NEW worker repos there anymore — it is
only used to locate and REPORT (never move) pre-existing worker repos a
pre-fix deployment left behind (`doctor`'s `legacy_workers` check, `warn`
severity, lists each stray task directory's dirty/clean state and HEAD).

**Finding #2 — primary-checkout escape detection.** `escape_detector.py`, a
deterministic filesystem snapshot of the PRIMARY checkout (repo-relative
path -> content sha256 / symlink target / executable bit — via
`enumerate_checkout_paths` + `snapshot_checkout`), taken immediately before
and immediately after a write-capable executor call
(`Orchestrator._execute_with_escape_detection`, wired into
`_dispatch_task_postcommit` for the non-audit `worker_repos` path only —
the audit is exempt: it legitimately writes under `.autoloop/audit/` by
design, so wrapping it would produce a false LOOP-FATAL on the first real
audit). `diff_snapshots` reports any creation, deletion, content change,
symlink-target change, or executable-bit change as a `checkout_escape_
detected` LOOP-FATAL park — the isolation mechanism itself may be
compromised, so this stops the whole loop, not just the one task.

**Read this as detection, not prevention.** Nothing in this module stops a
write; the agent runs as an ordinary subprocess with the orchestrator's own
filesystem permissions. A write that happens and is reverted before the
"after" snapshot runs is not caught, by this or by anything else here —
closing that needs a real OS-level sandbox (container/chroot/jail), which
is out of scope. The detector also does not inspect the primary checkout's
`.git/` internals (hooks, config, refs) — "tracked, untracked, ignored" is
exactly the three `git status` categories, deliberately scoped to the
working tree; a planted hook or rewritten remote in the PRIMARY checkout's
own `.git/` is a real, more severe, still-open gap, recorded honestly in
`docs/SECURITY.md` rather than silently left unmentioned.

*Why the exclusion list for Autoloop's own volatile files
(`state.json`/transcript/blocker+execution+intent stores/publisher) is
EMPTY.* An earlier design excluded them on the theory that the orchestrator
writes them across a task's lifecycle — but that directly conflicts with
catching an AGENT tampering with them, which is exactly what is required.
The fix is a narrow WINDOW instead of a broad one: the snapshot brackets
only the `TaskExecutor.execute()` call itself, and nothing the orchestrator
does writes any of those files between the "before" and "after" snapshots
(`ImplementExecutor` — the only write-capable executor — never touches
`.autoloop/` at all, per its own module docstring). So excluding them would
remove real detection capability for no gain; the enumerated exclusion set
the brief for this pass asked for is empty BY MEASUREMENT.

*The one exemption, added 2026-08-16 (esc-01), and why it does not reopen
the paragraph above.* `escape_detector.is_derived_bytecode` reports nothing
for a `.pyc` directly inside a `__pycache__/` directory, carrying a tag some
interpreter really emits, whose sibling `.py` source is present AS A REGULAR
FILE on every snapshot side the cache entry exists on. The line being drawn is AUTHORED vs DERIVED, not noisy vs quiet:
`state.json` and a blocker record hold the only copy of a claim, whereas a
cache entry is written by the interpreter from a source this same snapshot
still tracks byte for byte — a claim the predicate checks rather than assumes
(a `.py` that is a SYMLINK is watched as a target string, never hashed, so it
qualifies nothing). It was needed because
any out-of-band `import autoloop.<x>` against the primary checkout — a
dashboard restart, a `health --json` poll — recompiles a stale cache entry
mid-round, and `__pycache__/` is gitignored, so the detector saw it. That
produced three loop-fatal `checkout_escape_detected` parks on 2026-08-15/16,
none caused by an agent, each costing a `reset --yes` and the in-flight round
(one stranded five tasks `in_progress`). Note the timing before proposing
anything narrower: the recompile fires because the source changed BEFORE the
window (a merge the loop itself made), so a rule of "flag a `.pyc` only when
its source did not change in the window" would have flagged all three.
Deliberately still reported: a `.pyc` OUTSIDE `__pycache__` (the legacy
layout — it imports with no source beside it); a `.pyo` ANYWHERE, including
inside `__pycache__`, since no supported CPython emits that name (PEP 488
replaced it with a `.pyc`'s `.opt-N` infix) and so one is authored, not
compiled; a `.pyc` whose TAG is not one an interpreter emits
(`mod.attacker.pyc`) — accepted tags are `cpython-<digits>[t]` plus this
runtime's own `sys.implementation.cache_tag`, and the family shape is the
load-bearing half because the incidents' writer was a different process than
the loop, so keying on the runtime tag alone would recreate the parks; an
orphan cache entry with no sibling `.py`; a cache entry whose
sibling `.py` is a symlink, or is missing on one side of the window (the
per-side check, rather than a union of both key sets, is what refuses a cache
entry planted in the same window that deletes its source); and any symlink or
directory appearing at a cache path.
Deliberately NOT covered: `.so`/`.pyd`, which are authored build outputs.
The same rule reaches the validation mutation guard below, since that
delegates to `diff_snapshots`. Residual, recorded in `docs/SECURITY.md` S24
rather than argued away: a forged cache entry whose `(mtime, size)` or
PEP 552 header matches its source would be loaded by the interpreter and is
not reported — header verification costs an attacker one `os.stat` to
defeat, while failing closed on an unreadable header would manufacture the
very parks this removes. `PYTHONDONTWRITEBYTECODE`/`-B` was reported (esc-01
brief) as already applied to the loop, supervisor, deadman and dashboard
wrappers, which live outside this checkout — nothing here sets it, so it is
not a control this repository can claim. It would not be the fix anyway: it
depends on every future caller remembering.

Also new: `Orchestrator._prepare_write_capable_worker` requires the PRIMARY
checkout's index and working tree to be clean (`primary_checkout_dirty`,
loop_fatal) before every write-capable dispatch — a dirty baseline cannot be
trusted for the before/after diff above.

**Finding #3 — circular task ownership, failed-round residue, and
unbounded pre-commit retries.** Three related gaps in the produce-then-
review path (§4b):

* *Ownership was circular.* `execution.allowed_paths` — what the post-commit
  review gate (`_verify_committed`) checks a commit's changed paths
  against — used to be the UNION of every round's `outcome.changed_paths`,
  i.e. the AGENT'S OWN report of what it touched. "Did it stay in scope?"
  was checked against "whatever it did." Fix: `Task.approved_paths`, a new
  field — EXACT repository-relative paths, validated on the way into
  `TaskRegistry.add_many` (`tasks._validate_approved_path`: no globs, no
  `..`, not absolute, not home-relative — refuses with `TaskGraphError`
  code `bad_approved_path`) so both a ChatGPT `plan` (`TaskSpec.
  approved_paths`, accepted-but-not-required at the protocol level — making
  it required would be a breaking wire change to every existing `plan`;
  instead a task with none can simply never be dispatched, same safety
  property, zero blast radius) and `seed_tasks.json` go through the same
  gate. `_dispatch_task_postcommit` refuses to dispatch a non-audit
  implement/revise for a task whose `approved_paths` is empty (task_fatal,
  code `approved_paths_missing`) and, once dispatched, checks
  `outcome.changed_paths` against it BEFORE `commit_and_capture` ever runs.
  **That second check is ADVISORY since 2026-08-05 (operator decision), as
  is the post-commit one** — see the amendment below; the park code
  `changed_paths_outside_approved` is retired and survives only as a
  transcript entry. The EMPTY-scope refusal above is a different rule and is
  NOT relaxed: "no scope declared" still means "not dispatchable", so a task
  cannot become write-capable by omitting the field.
  `execution.allowed_paths` is now seeded ONCE from `task.approved_paths` at
  creation and never unioned with anything self-reported again — the
  post-commit check remains as defense in depth for what the pre-commit
  gate cannot see (a commit hook adding a path strictly after it runs; see
  `test_hook_adding_unexpected_path_is_recorded_not_refused`). A LOADED
  (not freshly created) `TaskExecution` whose `allowed_paths` disagrees with
  the task's CURRENT `approved_paths` is re-synced to match on every
  dispatch — covers a resumed task whose on-disk `TaskExecution` predates
  this field. Accepted, not hidden: this means an operator who hand-edits
  `tasks.json` mid-task to widen `approved_paths` re-authorizes the whole
  accumulated range on the task's very next dispatch, not just what a fresh
  task would see — only reachable by direct edit of the on-disk task graph
  (an operator with that level of access, not an agent), and `plan` refuses
  a duplicate task id, so this is a deliberate operator action, not
  something a directive or an agent's own report can trigger. The AUDIT is
  exempt from all of this — its write surface is already bounded by
  `MarkdownPolicy` (at most one new `docs/AUDIT_<date>.md`), a completely
  different, non-agent-controlled mechanism, so it keeps the pre-existing
  accumulate-from-`changed_paths` behaviour.
  A NEW filesystem-aware check, `escape_detector.find_symlink_traversal`,
  runs at dispatch time (string validation in `tasks.py` has no repo-root
  awareness by design): an approved path whose ancestor component — or the
  leaf itself — already exists on disk as a symlink is refused
  (`approved_path_symlink_traversal`, task_fatal), because the string alone
  cannot show that following it writes through to somewhere outside the
  repository.
* *Failed-round residue.* A round that returns `status="error"` (a
  validation failure, before any commit) used to leave its files sitting in
  the worker repo; the NEXT round reused the same worktree, so
  `ImplementExecutor`'s `git.dirty_paths_all()` read picked up the old
  failed round's files too, and a round that later passed committed them
  alongside its own legitimate change. Fix:
  `Orchestrator._prepare_write_capable_worker` requires the worker repo
  clean (`dirty_entries_all()` empty) before every write-capable dispatch;
  if not, `WorkerRepoManager.quarantine(task_id, label)` MOVES (never
  deletes) the dirty repo to a sibling `quarantine/<task_id>-<label>`
  directory — preserved for diagnosis, but no longer reachable by a later
  `create()` for the same task id, which `create()` already refuses if
  anything exists at its target path — and a fresh repo is created from
  `execution.candidate_sha` (a round already committed — a later `revise`
  still builds on that) or `execution.task_base_sha` (nothing has committed
  yet). No git reset/clean/checkout is used anywhere in this — the policy
  whitelist admits none of them, on purpose (§ the git command whitelist
  note in `config.example.toml`); quarantine is plain filesystem `shutil.
  move`, mirroring how `WorkerRepoManager.remove` was already a plain
  `shutil.rmtree`. **The fetch SOURCE for that recreation differs by which
  sha is resumed** (fixed same-day, after the pass's own tests initially
  missed it — see `docs/COMMON_ERRORS.md` §8 and `docs/SECURITY.md` S25's
  addendum): `task_base_sha` always exists in the primary checkout, so that
  recreation fetches from there; `candidate_sha` was committed INSIDE the
  worker repo now being quarantined — its own separate git object database,
  never pushed anywhere — so that recreation fetches from the quarantined
  directory itself (still a valid, fetchable git repo on disk; `quarantine`
  moves, never deletes).
* *Unbounded pre-commit retries.* `TaskExecution.attempt_count` used to
  increment only in `_finish_postcommit`, reached only AFTER a commit — so
  a validation failure, or a crash before any commit, never consumed an
  attempt, making `MAX_TASK_ATTEMPTS` (5) toothless against exactly the
  failure mode it exists for. Fix: the increment (and an immediate
  `TaskExecutionStore.save`) now happens in `_dispatch_task_postcommit`
  BEFORE the executor is ever called, right after the attempt-count-ceiling
  and review-round-cap checks pass — so it is persisted to disk before any
  crash, restart, or validation failure could occur, and the ceiling holds
  across all of them. `_finish_postcommit` no longer increments it (it
  would double-count a fresh commit's already-bumped value, and a
  crash-recovered adoption's value was already bumped in the earlier,
  crashed process before ITS executor call).

**Amendment, 2026-08-05 — the two path-scope CHECKS are ADVISORY; the review
packet is the control.** Operator decision, after six parks in three days that
were all legitimate work and at least three of which were caused by a task
scope guessed wrong when the task was written. `approved_paths` is set before
an agent runs, i.e. at the moment of least information about what the fix will
touch, so a declared scope is a PREDICTION — and no amount of pre-authorization
repairs a prediction made before anyone read the code.

Unchanged: both comparisons still run, same `tasks.unauthorized_paths` matcher,
same inputs, at both ends — pre-commit (`outcome.changed_paths` vs
`effective_approved_paths(task.approved_paths)`) and post-commit
(`commit_range_paths` vs `execution.allowed_paths`). Changed: the CONSEQUENCE
only. They record onto `TaskExecution.out_of_scope_paths` (accumulated across
rounds, written solely from what the comparisons produced) and the round
proceeds to commit and review. Both had to move together — relaxing only the
pre-commit one would have moved the park downstream to
`post_commit_verification_failed` for the very same paths.

The information must survive, and that is the whole point rather than a
caveat: `packet._format_out_of_scope` renders an `OUT-OF-SCOPE PATHS (n)`
section into every review packet, computed FROM GIT at render time
(`commit_range_paths` vs `execution.allowed_paths`) and never from
`out_of_scope_paths` — that record is the union of both gates, and the
pre-commit gate's input is the executor's own report, so reading it would let a
false report state a fact about the commit. The section renders ONLY when a
path is actually out of scope (changed 2026-08-15 from an unconditional
`(none)` line: a section that is empty in nearly every packet trains a reviewer
to skim past the one packet where it matters — what guards against it being
dropped in a refactor is now `test_the_packet_names_every_out_of_scope_path`
plus S25's literal-string grep, not a line a human might notice missing). It
says in its own text that the check is advisory and did not stop the commit, so
it reads as a judgement call rather than a handled failure, and it sits ABOVE
the diff, which is omitted over `packet.DIFF_INCLUDE_MAX_CHARS`. Residual risk,
accepted and named: an agent
touching unexpected files now produces a reviewable candidate instead of a
refusal, so detection depends on the reviewer reading that list — and on a
large commit they see the paths but not the content.

NOT relaxed, all different mechanisms: an empty `approved_paths` still refuses
dispatch (`approved_paths_missing`); escape detection (a write outside the
worker repo into the primary checkout) is still loop_fatal; every other
post-commit refusal — ancestry, empty range, dirty worktree, failing
validation, validation mutating the tree — still parks; and candidates still go
to their own side branch for an operator to merge. See `docs/SECURITY.md` S25's
amendment for the security accounting.

**Finding #7 — blocker preconditions that could not do what their comment
claimed.** `cli._RESOLUTION_PRECONDITIONS` maps a blocker `code` to a
function RE-CHECKED at `answer` time, specifically so environmental
conditions cannot be cleared by an operator's promise alone. Two keys were
dead and one was mismapped:

* `git_failure_budget` never matched anything — the code `orchestrator.py`
  actually emits is `git_failure_budget_exhausted`. Renamed; the precondition
  (a `doctor` browser/login re-check) is otherwise unchanged.
* `push_refused_protected` was never emitted at all — EVERY push refusal,
  including a protected-branch one, surfaced as the generic `push_refused`.
  `Orchestrator._dispatch_task_push` now distinguishes them by RE-COMPUTING
  the same `gateway_protected` membership check `push_exact` itself uses
  (never by sniffing the exception string) and emits
  `push_refused_protected` specifically when the refusal was a protected-ref
  one — making `_precondition_protected` (already correctly implemented,
  previously unreachable) a real, reachable precondition: "the operator
  cannot text their way past a protected destination; the target or the
  policy must actually change."
* `worker_environment_drift` was mapped to `_precondition_browser`, whose
  checks (cdp/playwright/provider/conversation_url/browser_live) never
  inspect git hooks or worker isolation at all — ANY answer text cleared
  this blocker regardless of whether the worker environment it describes
  was still broken. Replaced with `_precondition_worker_environment_drift`,
  a dedicated recheck reusing the SAME primitives `doctor`'s `worker_
  isolation` check is built on (`validate_workers_root` +
  `WorkerRepoManager` + `verify_worker_isolation` against a real throwaway
  probe repo).

Verified exhaustively, not by a hand-maintained list (the SAME failure mode
that let the two dead/mismapped keys above go unnoticed for as long as they
did): `autoloop/tests/test_m1_hardening.py`'s
`test_every_precondition_key_matches_a_real_emitted_code` AST-walks
`orchestrator.py` for every string literal that can appear as the `code=`
argument of a `_to_needs_user(...)` call (including from inside a
conditional expression, like the `push_refused`/`push_refused_protected`
choice above) and asserts every `_RESOLUTION_PRECONDITIONS` key is one of
them. The reverse (a real, security-shaped code that has NO
`_RESOLUTION_PRECONDITIONS` entry at all) is intentionally NOT asserted by
that same AST walk — most emitted codes correctly resolve by answer text
alone (every ordinary `task_fatal`), so a fully exhaustive
reverse mapping would be wrong, not just redundant, and the walk has no way
to tell "should have a precondition" from "correctly doesn't."

**That gap is not hypothetical — it reopened within the same pass, twice.**
The three new `loop_fatal` codes finding #2/#3 above added
(`primary_checkout_dirty`, `checkout_escape_detected`,
`worker_isolation_violation`) initially shipped with no
`_RESOLUTION_PRECONDITIONS` entry at all, meaning any operator answer text
would have cleared a still-dirty primary checkout, a live escape-detector
finding, or a still-broken worker isolation — found in a first round of
review, fixed same-day (`docs/SECURITY.md` S26's addendum).
`worker_isolation_violation` reuses the existing
`_precondition_worker_environment_drift`; `primary_checkout_dirty` got a
new `_precondition_checkout_clean` (re-runs `GitGateway.is_dirty()` — text
alone cannot make a dirty tree clean).

That first fix's `checkout_escape_detected` precondition also reused
`_precondition_checkout_clean`, sharing the SAME `is_dirty()` recheck as
`primary_checkout_dirty` — which is wrong, and a SECOND round of review
caught it: `escape_detector`'s snapshot deliberately covers a superset of
what `is_dirty()` can ever see — tracked, untracked, AND ignored paths (see
finding #2 above and `escape_detector.enumerate_checkout_paths`). Autoloop's
own state directory is gitignored in production, so the canonical scenario
this detector exists to catch — an agent tampering with `.autoloop/
state.json` or a blocker record to cover its tracks — is exactly a case
where the working tree stays clean throughout, and `is_dirty()` would have
reported nothing to refuse on. Fixed: `checkout_escape_detected` now has
its OWN precondition, `_precondition_checkout_escape_detected`, which
refuses UNCONDITIONALLY (mirroring `_precondition_protected`'s shape) —
this detection needs a human to read the reported paths, not an automated
recheck to decide it was handled.

A deliberately CURATED (not AST-derived) test,
`test_security_and_environment_codes_all_have_a_precondition`, pins the
full set of codes that must never resolve on text alone — added because the
forward-only exhaustiveness test structurally cannot express "this key is
missing," and it would not have caught either round of this gap on its own.
Any future `loop_fatal` / security-shaped `code=` needs its precondition
DESIGNED (not just present) in the SAME change that adds it, not as a
follow-up — "does the recheck actually re-verify the condition that fired
the park" is a design question the exhaustiveness test cannot answer, as
this section's own history shows.

---

## 4f. Operator-changeset review (publishing a hand-authored commit)

`changeset_review.py`, 2026-07-31. Everything above this section reviews and
publishes work THIS LOOP produced in its own task worker repo
(`PostcommitBinding`, §4b/§4c). It has no path for a changeset an operator
authored directly on the branch autoloop itself runs from — before this,
`push`'s only two outcomes were "answers a live `PostcommitBinding`" or
`legacy_git_path_retired` (§4c's `_dispatch` — the retired direct-push route
stays retired), so every infrastructure commit had to be pushed by hand.

**`ChangesetBinding`** (`changeset_review.py`) mirrors `PostcommitBinding`'s
shape and field naming (`base_sha`, `candidate_sha`, `candidate_tree_sha`,
`packet_sha256`) but is a distinct type: there is no task, no
`TaskExecutionStore`, and no separate worktree behind it — the candidate
lives directly in the checkout the orchestrator itself runs from. It carries
`branch`/`dest_ref` explicitly rather than deriving them at push time.

**CLI: `python -m autoloop review-changeset --base <sha> --candidate <sha>
[--packet FILE]`** (`cli._cmd_review_changeset`). Refuses (before touching
any session) unless both shas are literal 40-hex and resolve to commit
objects, `--candidate` is a descendant of `--base`, and the checked-out
branch is not protected — `changeset_review.build_changeset_binding`.
Requires no existing session (same rule as `run --kickoff`). On success it
renders the review packet — `changeset_review.build_changeset_packet`,
reusing `packet.py`'s commit-list/changed-paths/diffstat/`range_diff`
machinery — records the binding on `state.changeset`, and queues it as
`state.outbox`; a later `run` sends it. `--packet FILE` replaces only the
diff BODY with the file's text; the `branch`/`dest_ref`/`base_sha`/
`candidate_sha` header is always stamped from git, never from the file —
`report_sha256` (hashed over the whole rendered request, which embeds this
packet verbatim) is what stops an approval of one commit validating against
another, so those four identifiers must always be literal text in the
hashed body regardless of where the rest of the packet text came from.

**Dispatch.** `Orchestrator._dispatch` routes a `push` directive whose
response carries `resp.changeset` to `_dispatch_changeset_push` — checked
before the `resp.postcommit` branch (the two are mutually exclusive in
practice; the order carries no meaning) and before the
`legacy_git_path_retired` fallback, so a `push` with no changeset binding
still refuses exactly as before (§4c). `_dispatch_changeset_push` mirrors
`_dispatch_task_push`: `resp.changeset.candidate_sha` is the ONLY source of
what publishes (never `directive`, never a fresh lookup); it re-verifies
descendant-of-base, that the candidate still resolves, and that its tree
still matches `candidate_tree_sha`, then imports the object by literal id
from `self._git.repo_root` into the Publisher and calls `publish` — the
same `push_exact` underneath, unconditionally. **A Publisher is required**
for this path — there is no direct-push fallback (unlike
`_dispatch_task_push`'s no-publisher branch): pushing straight from the
orchestrator's own checkout would be the retired legacy direct-push shape
this feature exists to replace, not reintroduce. Its absence parks with the
new `changeset_publisher_required` — mapped in `cli._RESOLUTION_PRECONDITIONS`
to the existing `_precondition_publisher_url` recheck (and added to
`test_m1_hardening.py`'s curated `security_and_environment_codes` set) in
the SAME change that introduced it, per §4e's rule: an environmental
`loop_fatal` code with no precondition at all is exactly the gap that bit
twice before (S25/S26) — the forward-only exhaustiveness test cannot catch
a missing key, only the curated reverse test can.

**Why the generic HEAD-moved staleness check is skipped for this path.**
Every other reviewed decision stamps `head_sha` from `context.build_context`
— the RUNNING checkout's `git rev-parse HEAD` at request-send time — and
`_step_executing` refuses if that checkout's HEAD has since moved
(`review_mismatch:head_moved`). That check is sound where the checkout
legitimately should not move while a request is outstanding (the
orchestrator's own repo during a produce-then-review task round, which
happens in a separate worktree). For a changeset review the checkout IS the
branch under review, and the whole feature exists to let the operator keep
committing to it after a packet is sent — the reviewed candidate, not
whatever HEAD has since become, is what must publish. `_step_executing`
skips the HEAD-moved check specifically when the decision is `push` AND
`resp.changeset is not None` — narrower than "any changeset-bound
response", since `resp.changeset` is only ever meant to answer a `push`
(a stray `commit`/`commit_and_push` reply somehow carrying one still gets
the ordinary check, and separately lands in `legacy_git_path_retired`
either way);
identity is instead carried entirely by `resp.changeset.candidate_sha` plus
the `report_sha256` digest `verify_review` already checked. Proved directly:
a later, unreviewed commit lands on the branch after the packet is queued,
and the stamped approval still publishes the earlier, recorded candidate —
never the later one (`test_changeset_review.py`).

**Protected-branch destination.** `_step_executing`'s `destination_branch`
computation (used by `authorize_directive`'s protected-branch gate) reads
`resp.changeset.branch` — the value pinned at binding time — rather than a
fresh `self._git.current_branch()`, for the same reason `_dispatch_task_push`
reads `resp.postcommit.task_branch`: judging the CURRENT checkout branch
would be wrong (and, for a changeset review specifically, would let a
`protected_branches` config change or a branch switch between review and
dispatch retroactively make a still-protected destination look clear).
`_dispatch_changeset_push`'s own `push_exact` call re-checks the same
protected set independently, belt-and-braces, exactly like the
produce-then-review path.

---

## 4f-bis. Always-approved repository trackers

`Task.approved_paths` is the machine-checkable authorization scope, and it is
deliberately not self-widening (§4e). But this repository's own rules make
updating four documents a **condition of doing the work**: `CLAUDE.md` §12
requires `docs/SUMMARY.md` whenever a file is added, removed or changes
responsibility, and `docs/TESTS.md` whenever a test is added, removed or
renamed; §14 requires `docs/SECURITY.md` in the same change as a
security-relevant edit; §12 requires `docs/COMMON_ERRORS.md` when something new
breaks.

So a task that adds a file and does not list `SUMMARY.md` **cannot be completed
while obeying the repo's rules** — the agent must violate `CLAUDE.md` or violate
its path scope. rt-01 hit exactly that twice: after the first refusal the scope
was widened by four paths, and the next attempt was refused again for
`docs/SUMMARY.md`. Enumerating obligations by hand per task does not converge.

`tasks.TRACKER_PATHS` is therefore implicitly approved for every scoped task,
combined in exactly one place (`effective_approved_paths`) so the dispatch-time
seed, the every-dispatch re-sync, the PRE-commit gate and the POST-commit
path-ownership check cannot disagree. That last one is not hypothetical: the
first version of this change patched three of the four sites, and the
pre-commit gate then refused a tracker edit the post-commit check would have
allowed.

Bounded on purpose:

* **Fixed constant, not configurable.** Widening every task's scope must be a
  reviewed diff, never a TOML edit.
* **Markdown trackers only** — no code, no config, no test file, nothing
  executable.
* **An unscoped task stays unscoped.** Empty `approved_paths` still means "no
  scope authorized yet" and still refuses dispatch (`docs/SECURITY.md` finding
  #2); it does not become a task that may write documentation.
* **Visibility is unchanged.** Tracker edits still appear in
  `commit_range_paths` and in the review packet — this removes a refusal, not
  the reviewer's sight of it.

Residual risk, stated rather than hidden: `docs/SECURITY.md` is the security
tracker, so an agent can now edit the record of a finding without that being
named in its task. It is documentation, not a control — but a weakened finding
misleads a later reader, and it is why this list stays four entries long.

## 4f-ter. Operator task inbox and priorities

Adding a task while the loop runs used to be impossible without breaking
something. Two independent reasons, both real:

1. **The escape detector snapshots the state dir.**
   `enumerate_checkout_paths` covers tracked, untracked AND ignored paths, so
   `.autoloop/tasks.json` sits inside the before/after snapshot taken around
   every write-capable agent call. An operator edit landing mid-execute is
   indistinguishable from an agent writing outside its worker repo and parks
   the loop LOOP-FATAL. **That coverage is not the bug and must not be
   "fixed":** `tasks.json` holds `approved_paths`, so an agent able to edit it
   undetected could widen its own authorization — the circular ownership
   finding #2 closes.
2. **Lost updates.** The running orchestrator holds the registry in memory and
   saves it on task-graph changes, so an external edit can be overwritten by
   the next save. The single-instance lock exists to prevent exactly this.

`inbox.py` resolves both without weakening either. Requests go to a directory
BESIDE `workers_root` — already required to be absolute and outside the
checkout, its `.git`, the state dir and the publisher paths — so submission
touches nothing the detector watches and needs no lock. The loop drains it
between steps (`run`, never inside one), validates through
`TaskRegistry.add_many` — the same gate a ChatGPT `plan` goes through, so
there is no second implementation to drift — and writes `tasks.json` itself.
**The loop remains the only writer of the registry.**

    python -m autoloop add-task --id dash-02 --priority 1 \
        --title "..." --description "..." \
        --approved-path autoloop/dashboard.py \
        --validation "ruff check ."

Safe at any instant, including mid-execute. A malformed request is refused at
submit; an unparseable file is moved to `inbox/rejected/` rather than deleted
or replayed forever; a request the registry refuses (duplicate id, unknown
dependency, bad approved path) is reported and dropped. One typo never stops a
running loop.

**Applying queued requests on demand.** `run` drains between steps, but that
also dispatches whatever the current phase is — so with a review packet waiting
in the outbox there was no way to apply queued requests without also sending
it. `python -m autoloop drain-inbox` merges and exits without stepping the
phase machine. It takes the single-instance lock, because it writes
`tasks.json` and the loop must stay the only writer — "the loop" meaning
"whoever holds the lock" — so it refuses rather than racing a live run.

Both callers share ONE merge (`inbox.apply_requests`). Two copies would drift,
and a drift means the same request behaves differently depending on who applied
it — the same reasoning as `tasks.effective_approved_paths`, and a test pins
that both call sites use it.

**Editing priorities from the tracker.** The dashboard's roadmap section shows
each task's priority in a number input with a Save button. Saving POSTs to
`/api/priority`, which writes a `kind: "priority"` request to the SAME inbox —
the page's only write path, and it touches neither the repository nor the state
dir, so the read-only property everything else in that file depends on is
unchanged and a save is safe while an agent is running. The change is queued,
not applied: the loop applies it on its next run, and the page says so rather
than showing a value that is not yet true.

A priority request carries an id and a number, and nothing else — submitting
one with `approved_paths` is refused. That is deliberate: priority decides what
runs next, `approved_paths` decides what an agent may touch, and only the first
belongs on a form. The endpoint has no authentication (the server binds
127.0.0.1), so it requires an `X-Autoloop` header a cross-origin form post
cannot set without a preflight this server never approves, and refuses a
non-local `Origin`. Both are cheap mitigations against a local page in the same
browser, not claimed to be more; the blast radius is bounded by what the
endpoint can express.

**Priorities.** `Task.priority` is an ascending integer — 1 outranks 2, the
default 100 sorts last, ties break on id so selection stays deterministic.
`next_ready()` orders by it instead of insertion order, which is the point: an
operator has to be able to steer a running loop, and under insertion order a
task added later could never overtake one already queued however urgent.

## 4f-quater. Directory prefixes in `approved_paths`

Scope was exact paths only, and authoring it became the loop's main source of
friction: rt-01 was refused twice for files it genuinely had to touch, and every
imported audit task arrived unscoped. The answer is to relax how scope is
EXPRESSED, not what is enforced.

An entry is now either an exact repository-relative file, or a **directory
prefix ending in `/`**:

    approved_paths = [
      "lexy-app/backend/routers/",          # this directory and everything under it
      "docs/SECURITY.md",                   # this file, and only this file
    ]

Matching is on segment boundaries, which the trailing slash gives for free:
`lexy-app/backend/routers/` authorizes `.../routers/books.py` but never
`.../routers_backup/secret.py`. An exact entry never matches by prefix, so
naming a file authorizes that file alone.

`tasks.unauthorized_paths` is the single matcher, used by BOTH the pre-commit
gate and the post-commit ownership check. Two implementations would drift, and
a drift means a path refused before the commit but accepted after — the same
reasoning as `effective_approved_paths`, and the same bug that was caught
there when only three of four call sites were updated.

**What did not change.** Everything a prefix must survive: no `..`, no glob
metacharacters, no whitespace, no leading `-`, nothing absolute or
home-relative. The executor's own report still never defines its own
authorization (`docs/SECURITY.md` finding #2). A prefix is broader than a file,
so it is worth choosing the narrowest one that covers the work — but it is
still a scope the OPERATOR declared up front and the reviewer sees in the
packet.

Also fixed here: the segment pattern required an alphanumeric first character,
which made ordinary files unrepresentable — `tests/_auth_helper.py` and
`.gitignore` were both refused while the error text claimed `_` was legal.
A leading `.` or `_` is now accepted; `.` and `..` segments are refused
separately, which is the check that actually matters.

## 4g. The validation-environment boundary (test DB credentials)

**The problem.** A task may declare validation that needs a database — `rt-01`
declares `ruff check .` plus `python3 -m pytest -n auto -q` under
`lexy-app/backend`, and that suite reads `DB_HOST`/`DB_PORT`/`DB_NAME`/
`DB_USER`/`DB_PASSWORD` plus `SECRET_KEY` from the environment
(`database.py:70-73`, `core/security.py:11`, `tests/conftest.py:37-42`). A
worker repo is a fresh clone and `.env` is gitignored, so in a worker those
variables are absent and every DB-backed test fails authentication. Observed
directly: rt-01 burned four attempts, each reporting a `pytest` failure that
was really `asyncpg.exceptions.InvalidPasswordError` ×1197.

The three obvious fixes are all wrong. Copying `.env` into worker repos puts
the production DB password, the JWT signing key and the Anthropic API key on
disk in every worker, undoing M2's containment. Sourcing `.env` into the
loop's shell hands all of it to the write-capable agent through ordinary
inheritance. Narrowing the declared validation makes the check vacuous — the
exact weakness `7616b18` fixed.

**The boundary.** `autoloop/validation_env.py`. One operator-authored file,
`[paths].validation_env_file` (absolute, optional), holding ONLY six names:

    DB_HOST  DB_PORT  DB_NAME  DB_USER  DB_PASSWORD  SECRET_KEY

Delivery is one-directional and explicit at both ends:

* **Validator gets them.** `run_validation_commands` never lets a subprocess
  inherit `os.environ`. It always passes `strip_validation_vars(os.environ)`
  — the parent environment MINUS those six names — and overlays the file's
  values when a `ValidationEnv` is configured. The file is therefore the ONLY
  channel; an operator who sources `.env` into the loop's shell does not
  silently change what validation connects to, and an unconfigured loop runs
  validation with no credentials rather than ambient ones.
* **Writer explicitly loses them.** `ClaudeCliRunner.run` passes
  `env=strip_validation_vars()` for BOTH tool sets (write-capable implement
  and read-only audit), and `worker_env()` strips them from every worker git
  subprocess. Removal, not omission — the agent inherits the loop's
  environment by construction, so they have to be taken back out.
* The file's **path** never enters any environment, and
  `strip_validation_vars` additionally drops any `*VALIDATION_ENV_FILE*`
  variable, so the property holds even if a future caller passes one through.

Only the two POST-WRITER validation sites receive the credentials: the
`ImplementExecutor`'s own validation run and the orchestrator's post-commit
re-run. The audit executor deliberately gets none — read-only agents, no
writer, no database.

**Refusals** (all fail closed, and no message ever contains a value):
relative path, missing file, non-regular file, symlink (checked BEFORE
`resolve()`, so a link into the real `.env` is caught as a link rather than
followed), a file not owned by the running user, any group/world permission
bit, a path inside the checkout / state dir / `workers_root` / either
publisher path, an unknown key, a duplicate key, a malformed line (including
an `export ` prefix), an empty value, a missing required key, a `DB_PASSWORD`
or `SECRET_KEY` under 8 characters, and a `DB_NAME` equal to the application
database name this repository declares in `.env.example`.

That last one is the only production marker the repo actually defines. It is
one exact string, **not** a test-vs-production discriminator and **not** a
name heuristic — supplying a dedicated throwaway database stays the
operator's responsibility. There is deliberately no host refusal:
`.env.example`'s `DB_HOST` is `localhost`, which is exactly where a
legitimate test database lives.

**Redaction.** Every value is replaced with `[redacted <NAME>]` in the
validation summary, longest value first. That matters beyond logging: the
summary becomes `state.last_validation`, which reaches `state.json`, the
transcript, blocker records **and the review packet sent to ChatGPT**.
Short secrets are refused at load time rather than handled by a length
threshold at redaction time, so redaction can never be defeated by a value
too short to match safely.

**What this is NOT — read this before describing the boundary to anyone.**

**Candidate validation code can observe the test credentials.** The validation
subprocess is handed them on purpose so the backend suite can run; any test,
conftest or imported module in that process can read `os.environ` and do what
it likes with the values. Redaction scrubs the loop's own summaries — it
cannot stop candidate code from exfiltrating what it was deliberately given.
The protection is **least privilege** (a dedicated role on a throwaway
database), **local-only scope** (a separate local cluster, reachable from
nowhere else), **separation from production** (production credentials are
forbidden and the declared application `DB_NAME` is refused), and
**publication gating** (a candidate that mutates the tree during validation is
refused — see the mutation guard below). It is NOT secrecy from candidate
code.

**S24 remains OPEN** — the write-capable agent still has no path jail; escape
is detected after the fact, not prevented. Per-run ephemeral databases and a
real sandbox are the next steps, deliberately not attempted here.

**Validation mutation guard (2026-08-01).** Validation reads; it must never
write. `_verify_committed` brackets the post-commit validation run with
`escape_detector.snapshot_worker_tree` and refuses the candidate on any
difference: content, creation, deletion, symlink target, executable bit —
over tracked, untracked AND ignored paths — plus the `.git/index` bytes, since
staging a change mutates the index while leaving every file identical. The
pre-existing residual-dirty check cannot cover this: it runs BEFORE validation
and is a `git status` check, so an ignored path (exactly where a test dumping
its config would land) is invisible to it. The refusal holds even when the
mutated file is one the task was approved to touch — approval authorises the
AGENT to edit a path, never validation to mutate one. Messages name PATHS
ONLY, so a park message is safe even when what was written was a credential;
the mutated files stay on disk uncommitted, as evidence. **One exemption,
inherited (2026-08-16, esc-01):** this delegates to `diff_snapshots`, so a
validation `pytest` run's own `__pycache__` entries — derived from sources
already in the tree — are silent, and the refusal message says "MUTATED the
worker tree beyond its own bytecode cache" rather than claiming validation
wrote nothing at all. See §4e for what stays in scope. **Precisely how far
that reaches:** the exemption only recognises a cache name whose tag is one an
interpreter emits, so it covers the ordinary
`<mod>.cpython-3XX[.opt-N].pyc` a run writes for every non-test module it
imports, but NOT `pytest`'s assertion-rewriter name
(`<mod>.<cache_tag>-pytest-<version>.pyc`), which interposes its own version
into the tag position — a rewritten TEST-module cache is still reported.
Erring toward reporting is the right direction for a guard; a tag rule loose
enough to accept `cpython-312-pytest-8.3.4` is loose enough to accept
`attacker`. Run validation with `-B`/`PYTHONDONTWRITEBYTECODE` and the
question does not arise.

**What the mutation guard is NOT** (reviewer's wording, kept because it is
exactly right): it detects **persistent net changes** to the snapshotted worker
tree and index. It is defence in depth, not an exfiltration or
write-prevention boundary. Three things it cannot see, by construction:

* validation that **transmits** a credential (a POST leaves no trace on disk);
* validation that writes **outside** the snapshotted scope — the snapshot is
  the worker repo, so anything elsewhere on the filesystem is invisible to it;
* validation that writes and then **restores** the content before the second
  snapshot — only the net difference is compared, so a transient write is
  indistinguishable from no write at all.

It raises the cost of an accidental leak landing in a reviewed commit. It does
not stop deliberate exfiltration, and must not be described as if it did.

**Building the validation database (done 2026-08-01, reproducible).** The repo
cannot rebuild its own schema (see `docs/COMMON_ERRORS.md`), so the database is
built from one that already works. What actually worked, in order:

1. A dedicated server, so the credentials in the env file reach nothing else.
   The unused Homebrew `postgresql@16` cluster started on port **5433** with
   `pg_ctl -D /opt/homebrew/var/postgresql@16 -o "-p 5433" start`; its
   `pg_hba.conf` is `trust`, so no superuser password is needed. (The
   `postgresql@14` cluster, which would have matched production's major
   version, is a broken install — missing timezone data AND `libpq`.)
2. `CREATE ROLE lexy_validation LOGIN PASSWORD …` + `CREATE DATABASE
   lexy_validation_test OWNER lexy_validation TEMPLATE template0 ENCODING
   'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'`, then `CREATE EXTENSION pgcrypto` as
   superuser — migration 001 needs pgcrypto and the plain role cannot install
   it. Do NOT make the role a superuser: it would then reach the real database
   too, which is the whole thing this boundary exists to prevent.
3. Schema from the working database, read-only:
   `pg_dump --schema-only --no-owner --no-privileges | psql -p 5433 …`, then
   `alembic stamp head` — NOT `upgrade head`. `--schema-only` copies the
   `alembic_version` TABLE but not its ROW, so the chain would otherwise
   replay against a schema that already has everything. Stamping is only
   correct because the source is genuinely at head, which was verified
   structurally first (037's `word_lists.is_system` and nullable `user_id`,
   036's `word_norm`, 034's table all present).
4. Reference DATA, which stamping necessarily skips: `pg_dump --data-only -t
   language_table -t lemma_override` from the working database (the German row
   exists in no migration — it predates Alembic), plus `video_category` seeded
   from migration 013's own `CATEGORIES` list.

5. Corpus, via `python3 scripts/seed_validation_db.py` — deterministic,
   idempotent, and entirely INVENTED (no row is copied from any real
   database). Without it ~55 tests skip themselves rather than fail
   ("run the subtitle pipeline first"), and a skipping test grades nothing.
   Two videos, five sentences, five words and their `word_to_sentence` links,
   chosen to satisfy specific guards: a lemma with two surface forms in one
   video, a word whose lemma differs from its surface, a non-ASCII surface,
   and a `word_id` that collides with a real `phrase_id`. It refuses to run
   against the database name `.env.example` declares, reusing
   `validation_env.repo_declared_db_name`. `--verify` re-checks every guard
   without writing.

Result: **1259 passed, 1 skipped, 0 failed** — against a documented baseline of
1258 passed / 2 skipped, so one MORE test runs than on the dev database.
Getting there surfaced two pre-existing latent test bugs a non-dev database
exposes rather than causes — unconstrained catalog picks in
`test_free_chat_progression.py`, now fixed (`docs/COMMON_ERRORS.md`).

The one remaining skip is `test_match_inflected_phrase_matches_canonical`, and
it is **inert by construction, not by configuration**: it skips unless
`sich freuen auf` is in `phrase_table`, but the phrase extractor never
produces that canonical — measured, `match_sentence("ich freue mich auf die
Reise", "de")` returns `['ich', 'jdn. (Akk) freuen']`. Seeding the phrase makes
the test RUN and FAIL, so the seeder deliberately does not (see its `PHRASE`
comment). Whether the extractor or the test is wrong is an app-level question.

Divergences worth knowing: exactly the 18 categories migration 013 defines,
the synthetic corpus above, and PG16 against production's PG14.8.

**Allowlist deviation, stated plainly.** The brief that specified this
boundary named `JWT_SECRET_KEY`. This repository reads `SECRET_KEY`
(`core/security.py:11`, which raises at import when it is unset). Verified in
a fresh clone with no `.env`: six test modules fail to import until
`SECRET_KEY` is set, after which all 1260 tests collect with no other
variable supplied — so the six above are exactly sufficient and exactly
necessary. `JWT_SECRET_KEY` is not accepted as an alias; implementing the
brief literally would have rejected the real name as unknown.

---

## 5. Response contract (v3)

As v2 (task-id-based work authorization, `plan`, `reviewed` integrity stamps —
see the table in the contract itself, embedded in every prompt), plus:

* `commit.paths` required, non-empty.
* `revise` with `task_id: "audit"` re-runs the audit with feedback.
* Phase gate: `implement` (and `revise` of repository tasks) is denied with
  `policy_denied (phase_gate)` explaining that only the audit-review cycle is
  available. Flip `policy.implement_enabled = true` in a later phase.

Git approvals still require: stamp echo (`request_id`, `head_sha`,
`report_sha256` from the reviewed request), unchanged git HEAD at execution
time, policy authorization, and now the manifest gate + exact staging.

---

## 5b. Browser transport: submission confirmation, reconciliation, input

This section exists because of a concrete production failure (2026-07-29): a
prompt was typed, Send was clicked, ChatGPT drew the user bubble
**optimistically**, the client accepted that bubble as proof of submission, and
the message was never persisted. A reload then erased the evidence and the loop
waited 15 minutes for an answer that could not exist.

**Optimistic rendering is not submission.** A user bubble in the current,
unreloaded DOM proves only that the browser drew something. Submission counts as
`CONFIRMED` only on evidence that the *server* accepted the turn:

* an assistant response for **our** turn has begun (a node after our request, or
  generation is running), or
* an explicit `reconcile()` — a controlled reload — finds our request id in
  persisted conversation history.

Composer clearing and Send-button state are explicitly **not** evidence.

**The "a send may have happened" fact is durable, and pessimistic.** The
orchestrator persists `pending_request.send_attempted = True` *before* handing
the prompt to the transport, and clears it only when the transport can prove the
click never happened (`BrowserChatGPT.send_attempted` is False — composer or Send
never accepted the input). Recording it after the fact would lose it whenever
`submit()` raised *after* clicking Send — login expiry during confirmation, a
dying page, or SIGKILL — and the next run would post a duplicate. With the
marker durable, recovery always reconciles first and parks rather than reposting.

**Ambiguity is never a retry.** Anything weaker yields `UNCONFIRMED`, and the
loop parks in `submission_unconfirmed`. The backend may have accepted a message
the browser failed to observe, so an automatic resend could double-post. The
only resolutions are: a READ finds it (→ awaiting — see the next paragraph for
the two reads), `run --retry` (reconcile again), or `run --resubmit` — an
explicit operator decision that authorizes exactly one more send **of the same
request id**, so a message that did land is detected and not duplicated. A prior
send attempt also blocks an automatic resend if the machine re-enters
`submitting`.

**A false ambiguity is resolved by PROVING the request is there** (added
2026-08-16, `_resolve_or_park_ambiguous`). `reconcile()` reads the conversation's
mounted window, and ChatGPT mounts a window of a chat rather than its history: on
2026-08-05 `alr-af11e1b3-0006` parked as `submission_ambiguous` while the chat
held the request *and* its answer (`decision push`) — reading it by hand took
pressing End and scrolling six times. So when the reload comes back empty, the
by-content search (`find_conversation_with`, which mounts the tail and refuses to
answer unless it demonstrably reached the end — §5c) gets the last word:

* **found in this request's own conversation** → the park is cancelled, the loop
  resumes into `awaiting` and reads the answer. Nothing is ambiguous, and
  resuming **sends nothing**, so the risk is zero.
* **found in a different chat** → still parks, but the operator is told which
  chat. Rotation reuses the request id in the replacement chat, so a hit
  elsewhere can be a retired copy, and rebinding to it would be a rotation
  performed on a duplicate id.
* **not found, the search refused to conclude, a page it opened was wedged, or
  no project is configured** → parks exactly as before, and the park says which
  of those happened.

**The park question states the evidence obtained, and nothing more.** It used to
open "the request is not in persisted history after reconciliation" — which
`reconcile()` cannot establish, because it reads the mounted window. In every
park where the search did not run or did not conclude, that sentence asserted
absence and the note under it then said absence was never established: a
contradiction that reads as licence for `--resubmit`. The base sentence now says
only that reconciliation did not SEE the request in the window it read back;
wording that means "it is not there" belongs to the one branch that earned it —
the search that read the chats to their end and came back empty — and appears
only in that note.

The asymmetry is the design: **prove presence and proceed; never infer absence
and act.** Presence is safe to act on because acting means waiting; absence is
not, because acting means resending — and absence is precisely the conclusion a
flaky read gets wrong. The park itself is untouched, and `run --resubmit` remains
the only thing that repeats a send.

**A broken browser is not an ambiguous submission.** Only the search's own
refusal (`ConversationSearchInconclusive`) is collapsed into that park.
`SessionLostError`, `LoginExpiredError` and ordinary `BrowserError` propagate to
`run()` and keep the routes they already have — browser restart and the failure
budget, or a `login_expired` park with `submission_unconfirmed` as the resume
point. Every one of those SENDS NOTHING, so nothing is risked by letting them
through, while catching them would swap a recoverable transport fault for a park
naming the wrong cause: a dropped CDP connection is not evidence about what is
in the conversation. The single exception is `ConversationUnusableError`, caught
here because its route ACTS — it authorizes the rotation that reposts the request
id, and the search reads the project page and other chats, so a chat that is not
even this request's must never license a repost of it.

**Navigation is explicit.** `attach()` navigates only when there is no page on
the conversation (URL compared without query/fragment/trailing slash);
`reconcile()` is the only reload; `awaiting` never navigates, so a streaming
answer is never interrupted. If the page drifts off the conversation mid-await,
that is an error the orchestrator recovers from by re-attaching — not a silent
renavigation.

**Composer input is browser-realistic.** `fill()` is gone: setting a
contenteditable's text does not drive the events ChatGPT's ProseMirror editor
listens for, which is how a full-looking DOM sent nothing. The client now
focuses the composer with a real click, clears it with `ControlOrMeta+A` +
`Delete`, inserts the prompt with `keyboard.insert_text` (emits
beforeinput/input via CDP, one round-trip for a multi-thousand-character prompt,
and no key events so it cannot trigger an accidental send), **verifies the
editor holds the whole request** (request id + prompt tail), and waits for the
Send control to be genuinely enabled before clicking. If the editor never
enables Send, the run fails *before* sending — an unambiguous outcome that is
safe to retry.

**Every wait is bounded separately** (`[browser]` in the config): composer
readiness, input synchronisation, Send readiness, submission confirmation,
response start, response completion, reconciliation. On timeout a structured
`meta.json` lands in `.autoloop/diagnostics/<stamp>-<tag>/` with the request id,
stage, configured vs actual URL, composer state, matching user-message count,
whether an assistant turn started, whether a send was attempted, whether
reconciliation ran, and whether retry is prohibited. Diagnostics carry **no
cookies, tokens or storage** — the session protocol cannot read them.

**Replies are read from a rendered page, and the envelope is strict.** ChatGPT
renders a fenced code block as a widget whose text is the language label
followed by the code — the backticks never appear in `innerText`. Exactly two
representations are accepted, and both must contain exactly one directive:

* **canonical fenced** — one ```json block (raw markdown / other providers);
  prose outside it is fine because the fence delimits the directive. Two blocks
  are rejected (`multiple_json_blocks`).
* **rendered / plain** — the whole reply is the JSON value, optionally preceded
  by one language-label line. Nothing else may surround it.

**Position is never used to disambiguate.** A second object, another decision,
or trailing text is rejected (`trailing_content`), not silently resolved by
first- or last-wins. A directive can authorize a commit or a push, so "guess
which one they meant" is not an acceptable rule; the loop re-prompts instead.
Parsing failure is always a safe stop — it can never authorize execution.

**Response matching is scoped to the turn**: the reply must be the last message,
authored by the assistant, positioned *after* the user message carrying the
current request id. An earlier assistant message (e.g. the conversation's
opening `Understood.`) can never satisfy a later request.

---

## 5c. Transport recovery: disproving a send, resending once, rotating once

Added 2026-07-31. §5b made ambiguity safe; this section makes it **rarer**,
without weakening the rule that ambiguity never retries.

**The problem.** In one real run, two of sixteen sends simply did not persist
(see `docs/COMMON_ERRORS.md` §6). Every field the loop could see was a DOM
reading, and "the server refused it" and "the browser never issued it" render
identically. Unable to distinguish them, the loop parked a human on each — the
correct call on that evidence, and an expensive one.

**The missing fact is one layer down.** `browser/observation.py` attaches a
passive listener to the page's own traffic on the conversation-send endpoint. It
records a **status and a path, nothing else** — there is no field on
`SendObservation` for a header, a cookie or a body, and query strings are
stripped, so this cannot leak credentials into a diagnostic. It issues no
requests: it is an observer, never a second transport.

Verdicts, in the order they outrank each other:

| Evidence | Result | What it licenses |
|---|---|---|
| Our request id is in persisted history | `CONFIRMED` / accepted | proceed to `awaiting` |
| Send request returned 4xx/5xx, or never completed | `REJECTED` | reconcile, then **one** same-chat resend |
| No observation, or a mixed window (a failure *and* a success) | `UNCONFIRMED` | nothing — park, exactly as before |

**History outranks the network in both directions.** A rejecting status on a
request that turns out to be in the conversation resolves to *accepted* — the
status code is a proxy, history is direct evidence. And a 2xx **never** confirms
on its own: `_response_started` still requires the request id to be present, so
a 200 on a stream that then dies falls through to the ordinary response-start
timeout instead of masquerading as a reply. Do not remove that check on the
strength of a status code.

**One resend, after confirmation, never on the verdict alone.** `REJECTED`
routes to phase `submission_rejected`, which reconciles first. Only *confirmed
absence* — nothing in the conversation — licenses a resend, and only one, reusing
the same request id so a message that did land is still detected. Sessions
without the observation capability (every non-Playwright adapter) never produce
`REJECTED` and behave exactly as they did before.

**Rotation is the last resort, not the reflex.** A second confirmed rejection, a
`ConversationUnusableError`, or a confirmed **silent conversation** (below) may
abandon the chat for a fresh one in the configured project:

* `browser.project_url` must be set **explicitly**. It is never derived from
  `conversation_url` — a guessed project opens chats somewhere you did not
  choose. Unset means the loop parks instead of rotating.
* `policy.max_conversation_rotations` (default **1**) caps it per run. A second
  rotation in one run usually means the fault is not the chat.
* **"Per run" means per process**, and it took a real incident to make that
  true. `state.rotations` lives in the state file, which outlives the process,
  so the budget was really per *session*: a dropped network on 2026-08-02 spent
  the one rotation, and every later `run --retry` re-read the same count and
  parked with the same reason. Neither escape the park message offered was
  right — raise a policy cap for a rotation that was never needed, or `reset`,
  which back then also took the task registry. `cli._reset_run_scoped_budgets`
  now zeroes it once per process, logging `rotation_budget_reset` with the
  forgiven count so genuine churn stays visible.
  The reset lives in `_cmd_run`, **not** `_build_orchestrator`: `_run_continuous`
  rebuilds the orchestrator every iteration, so resetting there would refill the
  budget between rotations and delete the cap. Within one run the cap is exactly
  as strict as before — a rotation still costs its budget the moment it is
  attempted, and a failed attempt is still not refunded.
* `ConversationUnusableError` is deliberately narrow: the page demonstrably
  reached the conversation URL, is not an auth page, and still has no composer
  (or shows an explicit unavailable marker). A page that never loaded, a dropped
  CDP connection and a logged-out profile stay ordinary failures on the normal
  budget — rotating for a network blip would spend the one rotation and leave
  none for the real thing. The two budgets are also kept separate: a rotation
  attempt does not also increment `consecutive_failures`.
* Never rotates for: generation already started, a slow answer, a single or
  merely occasional response-start timeout, login expiry, rate limits,
  capacity, a malformed reply, or a policy denial.

**One Playwright driver per process.** `sync_playwright().start()` raises
"Playwright Sync API inside the asyncio loop" when another driver is already
RUNNING in the thread — the sync API drives an event loop of its own, so a
second start lands inside the first. Stop-then-start is fine; alive-then-start
is not. That made a *leaked* driver fatal rather than merely wasteful, and
leaking one was easy: `PlaywrightSession.close()` swallowed a failed `stop()`,
and `Orchestrator._drop_client` swallows a failed `close()` and drops the
reference regardless — so tearing down a session whose connection had already
broken (what happens after any browser error) left a live driver with nothing
pointing at it, and the next `connect()` killed the process. It presented as a
browser fault, but restarting Chrome never helped.

`playwright_session._driver()` now holds one driver for the process, started
lazily and never stopped; `close()` drops only the CDP connection. Closing a
CDP-connected browser leaves the human's Chrome running (same pid, CDP still
answering — verified). Sharing the driver makes the failure unreachable
regardless of which teardown path forgets what, rather than depending on all
of them being correct.

**A browser fault may not end the process — and the guard is positional, not by
type (2026-08-15).** The loop did not park, it DIED: `connect_over_cdp` raised
a plain `Exception` ("Connection closed while reading from the driver"), because
Playwright's `rewrite_error` gives driver-channel failures no type of their own.
It matched no `except` clause, reached the top level, and left `phase=submitting`,
`stop_reason=None`, `consecutive_failures=0` and no blocker — indistinguishable
from a clean exit, and the notify watcher had a stop with no reason to report.
The trigger was the familiar one (a Chrome running but no longer serving CDP);
what failed was the handling.

So `PlaywrightSession.connect` and `PlaywrightSession._call` each wrap the call
POSITIONALLY: `AutoloopError` re-raises untouched, everything else becomes
`SessionLostError`, and `KeyboardInterrupt`/`SystemExit` pass through because
they are not `Exception`. From there the routing above applies unchanged —
restart, failure budget, then a park or `failed` naming the cause. Two details
are load-bearing:

* The converted message keeps the ORIGINAL exception's type name. The
  transcript's `kind=` field now always reads `SessionLostError`, so without it
  a driver crash and a refused socket are indistinguishable after the fact.
* It carries `describe_cdp_endpoint(cdp_url)`: the endpoint, whether the port is
  open, whether `/json/version` answers, and how many BROWSER processes
  (`--type=` helpers excluded) run on the dedicated profile and on the debug
  port. "Chrome alive, port dead" and "no Chrome at all" need opposite operator
  actions and read identically as "cannot connect"; a third shape is real too —
  HTTP answering while CDP is wedged (2026-08-14). Measured inside the failing
  connect, because `_handle_browser_failure` drops the client and restarts the
  browser before anything is written, so a later probe would describe the repair
  rather than the fault. Every one of those measurements is LOCAL — the probes
  dial 127.0.0.1 and `ps` lists this machine — so an endpoint this machine
  cannot speak for (a non-loopback host, or no usable port) is reported with
  ALL four fields `unknown` and is pointed at that host or at the url, never at
  the local dedicated profile: a Chrome here, even one on that profile holding
  9222, is not evidence about a browser somewhere else, and "restart the
  profile" would be a confident diagnosis of the wrong machine. The loop's own
  default `cdp_url` is `http://127.0.0.1:9222`, so this is the rare path, not
  the usual one. The diagnosis can never raise: it degrades to
  `diagnosis=unavailable` instead of becoming the crash it exists to prevent.
  The ACTION leads and the key=value evidence follows, because `autoloop start`
  prints `blocker.question[:160]` — ordered the other way, the compact view
  shows the fields and cuts off the sentence saying what to do.

Same principle as the throttling back-off and the "a failure nobody could
recover from must not spend the budget" rule: a transport fault degrades into a
recorded decision, never into silence.

**A silent conversation — send confirmed, model never starts — is the third
trigger.** Added 2026-07-31, after this exact shape recurred three times: the
send is CONFIRMED and persisted (§5b/§5c above already rule out ambiguity and a
disproven send), and the model simply never begins generating, producing
repeated `ResponseTimeoutError`s that used to just spend the ordinary failure
budget down to `failed`. `BrowserChatGPT.await_response` now tags each timeout
with a `stage` — `"start"` (nothing began within `response_start_timeout_seconds`)
or `"complete"` (a response began and merely did not settle) — and rotation may
consider only `"start"`. All three of the following must hold, checked in this
order, before autoloop will call the chat unusable:

1. **Three consecutive `stage="start"` timeouts for the SAME request**, with no
   resubmission in between. This holds by construction, not by an extra guard:
   `awaiting` has no transition back to `submitting` except through a completed
   rotation, which resets the count — so three in a row can only mean three
   timeouts in one conversation, for one submission, nothing resent between
   them.
2. **A total measured wait of at least 3× `response_start_timeout_seconds`**
   (the default 120s → a 360s floor), computed from the configured value —
   never hardcoded — and checked against the ACTUAL elapsed time each timeout
   measured (`ResponseTimeoutError.elapsed`), not merely assumed from config.
   This is a soft gate, not an invariant to crash on: the floor is computed
   from the CURRENT config, while each `elapsed` was measured against
   whatever was configured at the time, so raising
   `response_start_timeout_seconds` between processes (the third trigger's
   own restart path can land exactly here) can leave a true, honestly
   measured wait below a floor computed from the new value. That is
   insufficient evidence, not corruption — the loop logs
   `response_silence_wait_below_floor` and keeps retrying ordinarily rather
   than raising.
3. **One FINAL reconciliation of the (still-current) conversation confirms no
   assistant turn has started** — `BrowserChatGPT.reconcile_no_response`, an
   explicit reload followed by the same "has the assistant begun answering our
   turn" check `await_response` itself uses, so a reply that landed between the
   third timeout and this check is not missed. A reply appearing here
   **cancels** the rotation attempt entirely: the streak resets to 0, exactly
   as if the timeouts had never happened, because the conversation was never
   actually silent — it was just slow.

Only once all three hold does the loop retire the old conversation, open
exactly one replacement in the configured project, resend the same request id
(the ordinary rotation continuation prompt — see below), and bind to it. It is
bounded by the SAME `policy.max_conversation_rotations` budget as the other two
triggers, not a separate allowance — a replacement chat that is also silent
parks `loop_fatal` on the second attempt, same as any other rotation-cap
refusal. Every occurrence, first through third, still goes through
`_handle_browser_failure` first, every time, and is charged to the ordinary
failure budget by it — with the one exemption that handler makes for a restart
the cooldown refused (see "A restart that was never attempted is not a failed
restart" below); the silent-conversation check is layered on top of it, never a
bypass. A
completed rotation resets `consecutive_failures` to 0 alongside the silence
count, exactly like an ordinary successful `awaiting` step already does — the
replacement conversation does not inherit the retired one's fault count. This
is what makes the "replacement chat is also silent" case above reachable at
all: without the reset, a single timeout in the replacement chat would push
`consecutive_failures` straight past `max_consecutive_failures` (already at
its ceiling from the three timeouts that earned the first rotation) and fail
the loop before a second rotation attempt — and its cap refusal — is ever
reached.

**Rotation proves before it binds.** ChatGPT does not mint a `/c/<id>` until a
chat has its first turn, so the order is forced: retarget to the project page,
submit there, read the URL the server assigned, check it is inside the configured
project — and then **reconcile against it**. Until the new conversation itself
confirms it holds the request, nothing is bound and the rotation has not
happened. Trusting the address bar would be the same class of mistake as
trusting an optimistic bubble.

**A failed rotation still costs its budget, and changes nothing else.** The
budget is consumed *before* the send, durably — a rotation posts a message, and
if the process dies between that send and the binding, recovery must not be able
to open a second chat and post again. Same pessimism as `send_attempted`, for
the same reason. Everything else is left exactly as it was: the request keeps
its old conversation binding and, crucially, its **original prompt**. Rewriting
the prompt before the send succeeded would leave a failed rotation holding text
that announces the conversation is abandoned — sitting in the request that
`--resubmit` would send into the conversation that was never abandoned.

**Every request owns its conversation.** `PendingRequest.conversation_url` and
`conversation_epoch` are the authority for submitting, awaiting and reconciling
that request — never `LoopState.conversation_url`, which moves. That is what makes
a late reply in an abandoned chat structurally unable to authorize anything: it is
not in the conversation the request is bound to, so it is never read. A request
written before this field existed is adopted onto the loop's URL on first touch,
which is only correct while `rotations == 0`; afterwards an unbound request raises
rather than being guessed at.

**The config follows the state.** A completed rotation rewrites
`[browser].conversation_url` so the next session does not walk back into the chat
the loop just escaped. The write is line-surgical (comments and every other key
survive), atomic, and **refuses a git-tracked path** — the config lives under the
gitignored state dir, and a loop that can quietly edit tracked files while
recovering from a browser fault is a worse problem than a failed heal. If the
heal fails, the CLI's drift guard recognises the recorded rotation (state moved,
config did not) and continues; any *other* disagreement still refuses to start.

`doctor` reports which conversation is actually live, how much rotation budget is
left, and whether `project_url` looks like a project.

**A restart that was never attempted is not a failed restart.** Two guards sit
on the browser path and, until 2026-08-04, cancelled each other out.
`browser.restart_cooldown_seconds` (default 120s) stops a genuinely broken
browser being restarted in a tight loop; `policy.max_consecutive_failures`
(default 3) stops a permanently broken transport churning forever. Both are
wanted. What was wrong was the interaction: every one of four consecutive
browser failures logged `browser_restart_skipped {reason: within cooldown}` —
so the loop could not restart Chrome, the one action that would have fixed the
hang — while those same failures spent the failure budget. The budget ran out
before the cooldown did, and the session ended `failed` with no blocker record,
which is the state that hides its own cause.

`_handle_browser_failure` now distinguishes WHY no restart happened:

| Restart outcome | Charged to |
|---|---|
| ran, succeeded | nothing — the attempt is free, as before |
| ran, failed | `max_consecutive_failures` (real evidence recovery does not work) |
| no `restart_command` configured | `max_consecutive_failures` (nothing to try later either — the default deployment's budget stays reachable) |
| skipped, still within the cooldown | `policy.max_browser_restart_skips` (default 5) — **never** the failure budget |

The fix is the exemption, not a re-tuned pair of numbers: it holds however
either constant is later changed, whereas "make the cooldown shorter than the
budget takes to exhaust" would be a coupling that works for exactly one pair
and traps whoever edits them next. `state.browser_restart_skips` counts only
within one cooldown window — any restart that actually RUNS, success or
failure, resets it — so it cannot accumulate across healthy periods, and a
fresh process (whose cooldown stamp is empty) always gets a real attempt.

Exhausting the skip budget **parks** `loop_fatal` with
`code="browser_restart_cooldown_blocked"`, naming
`browser.restart_cooldown_seconds` and its value in the question, so
`python -m autoloop blockers` shows the operator what stopped the session and
`run --retry` resumes it (that path clears every fault count). It deliberately
does not enter `failed`: a terminal state whose cause is recoverable only by
reading the transcript is the defect underneath this one.

**A throttled account is not a broken browser — and the browser recovery makes
it worse.** ChatGPT rate-limits the ACCOUNT when it sees too many requests, and
says so with a full-screen modal ("Too many requests… please wait a few minutes
before trying again", `[Got it]`). The modal is an `absolute inset-0` overlay
that intercepts pointer events; it removes nothing, so every wait in the
transport fails on it as a plain click timeout. Overnight on 2026-08-14/15 the
loop reported that, from 07:56 onward, as

```
browser session lost: Locator.click: Timeout 30000ms exceeded.
  waiting for locator("#prompt-textarea")
```

and answered each one by restarting Chrome and retrying. **Restarting and
retrying is what generates requests too quickly**, so the loop deepened the
exact condition it was failing on and reported the deepening as further browser
failures. pkt-03 burned through its five-attempt ceiling
(`blk-pkt-03-001`, `attempt_count_ceiling`) without ever reaching an approved
review, and the words *rate*, *limit* and *throttle* appear nowhere in the
transcript for that period. An operator found it by opening the browser and
reading the screen.

*Detection is by `data-testid`, never by the prose* — `[data-testid=
"modal-conversation-history-rate-limit"]`, captured live by attempting a click
while throttled and reading which element Playwright said intercepted it. The
wording and the locale both move; the testid does not. And the visible text is
not even a fallback: a search for "Too many requests" in the page's
`inner_text` reported healthy against a firmly limited account, alongside three
other passive checks. **The composer is present and enabled the whole time** —
any readiness probe written against composer presence alone reports a false
all-clear.

`errors.RateLimitedError` is deliberately NOT a `BrowserError`, so it cannot
reach that recovery at all. `orchestrator._handle_rate_limited` waits instead:

| | |
|---|---|
| restart the browser | **never** — a fresh browser meets the same server-side wall and adds a request |
| drop the client | **never** — re-attaching navigates, and a navigation is another request |
| `max_consecutive_failures` | **never charged** — same principle as the row above about skipped restarts |
| what it does | wait `browser.rate_limit_backoff_seconds` (doubling per consecutive occurrence to `rate_limit_backoff_max_seconds`), dismiss the modal in place, leave the phase untouched so the loop re-enters the step |
| bounded by | `policy.max_rate_limit_backoffs` (default 6 = 60+120+240+480+600+600 = 2100s, 35 minutes of measured wait) |
| streak reset by | a step that COMPLETES — nothing else |
| survives a crash | the wait is a persisted DEADLINE, served by whichever process is running when it expires |

**The wait is durable, not just its counter.** `rate_limit_backoffs` records that
a back-off was *entered*; `rate_limit_retry_not_before` records the instant it
runs *to*. Both are saved before the sleep. Without the second, a process killed
just after that save would resume treating the whole delay as already waited and
walk straight back into the browser step — so a supervisor restarting the loop
would skip every back-off in turn, rebuilding the restart storm this path exists
to stop out of process restarts instead of browser ones. `run()` therefore serves
whatever remains of the deadline before EVERY step (logged as
`rate_limit_wait_resumed`, so a resumed process sleeping ten minutes before its
first step says so), and the remainder is clamped to the delay the schedule
prescribes — a backward clock jump or a hand-edited stamp must not become a sleep
long enough to break the heartbeat monitor's staleness alarm. An unreadable stamp
fails open and is discarded: the counter still bounds the episode, and a limit
still in force raises again on the next step.

`rate_limit_wait_seconds` is credited when a wait FINISHES, never when it starts.
A crash mid-wait therefore credits nothing, and the process that finally meets
the deadline credits that wait once — including the part of it spent with the
loop dead, which for a server-side limit is real waiting: the remedy is calendar
time in which the account makes no requests, and a process that is not running
makes none.

**The re-probe is the next step, and only a step that completes ends the
streak.** Dismissal is still required — the modal hides the composer even
after the server-side limit expires, so a stale one left standing would read
as a throttle that never lifts — but it is *not evidence the limit lifted*.
The overlay is gone because the loop closed it; the limit is server-side and
answers to a timer, not to a click. Resetting the count on a successful
dismissal would reset it on every single occurrence: the delay would never
double, `max_rate_limit_backoffs` would never accumulate, and the park would
be unreachable — a fixed-interval retry loop wearing the shape of a back-off,
which is a slower version of the incident this replaces. So the reset lives on
`run()`'s success path, where it costs no request and cannot be faked.

Exhausting the back-off budget parks `loop_fatal` with `code="rate_limited"`,
naming the throttle and the total wait actually measured (not the configured
schedule), so `python -m autoloop blockers` says **rate limited** where it used
to say **browser session lost**. Leave the account idle, then `run --retry`.

It has no `cli._RESOLUTION_PRECONDITIONS` entry, deliberately and for the same
reason `browser_restart_cooldown_blocked` has none: the only recheck that could
establish whether the limit is still in force is *another request against the
limit*, which is the behaviour this whole path exists to stop. The condition
also clears on a server-side timer with no operator action, so refusing the
answer would gate a blocker on something the operator cannot demonstrate.

---

## 5d. Two reviewers: Codex primary, browser fallback

Added 2026-08-01. The reviewer seat now has two occupants, and the default is
the CLI.

**Why Codex is primary.** Everything §5b and §5c describe is machinery for not
knowing whether a message was delivered — a property of reading a DOM, not of
the reviewer role. A subprocess returns an exit code. So `codex_cli` collapses
the interface honestly: `submit` runs the CLI and stashes the reply, returning
CONFIRMED or REJECTED and **never** UNCONFIRMED; `await_response` returns the
stash; `reconcile` is authoritative because the transport is synchronous. It
declares `idempotent_submit`, which is what tells the orchestrator that a failed
send appended nothing to any durable conversation and can simply be re-issued —
without it, every failed invocation would park a human on `submission_ambiguous`,
a rule written for a shared chat thread that means nothing here.

Rotation is unreachable rather than disabled: the adapter omits
`retarget`/`current_url`, and every rotation trigger describes a browser
conversation.

**Why the browser stays.** Codex draws on your ChatGPT plan's **agentic**
allowance (shared with ChatGPT Work and ChatGPT for Excel). Ordinary ChatGPT
conversations draw on a **separate** quota. So the browser is not a second door
onto the same budget — it genuinely still works once Codex is spent. That, and
only that, is what makes automatic failover worth its complexity.

**Failover is bounded, gated and recorded.** On `QuotaExhaustedError` the loop
hands the reviewer role to `conversation.fallback_provider`:

* Bounded by `policy.max_provider_switches` (default 1). The useful move is
  primary → fallback; switching back would need a quota window to have reset,
  which does not happen inside one run.
* Gated on `state.last_response is None`. Quota can only bite while a request is
  unanswered, but the guard is asserted rather than inferred from the phase
  machine: a handover straddling an answered turn is the one shape that could
  put two reviewers inside a single review round.
* Recorded on the request and the response (`provider`), plus a
  `ProviderSwitch` record and a `provider_switched` transcript entry. The
  reviewer grants authority — an approval carrying a `reviewed` stamp must stay
  attributable to the transport that produced it. A silent swap would leave the
  audit trail saying a directive was reviewed with no answer to *by whom*.
* State beats config afterwards (`active_provider`), so a resumed run does not
  quietly return to the exhausted provider and spend the same allowance again.

When the handover happens the request keeps its id and its bytes, but the
exhausted transport's per-transport marks (`send_attempted`, `last_send_outcome`,
`resends_used`) are cleared — the fallback has never seen this request, so those
marks describe nothing here. Without that, `submitting` would park on
`submission_ambiguous` and the failover would be dead on arrival.

With no fallback configured, a fallback equal to the primary, no request in
flight, or the budget spent, the loop parks `loop_fatal` and says which.

**Quota detection is honest about what it cannot verify.** `codex/quota.py` is a
pure function over `(returncode, stdout, stderr)` with an overridable pattern
list, because the exact exhaustion wording could not be confirmed when this was
written and will change. A `returncode == 0` is never exhaustion whatever the
text says — OpenAI's documented behaviour is a soft stop, so a turn in flight
finishes and that reply should be used. Every non-zero exit logs its return code
and a bounded stderr tail, so the first real exhaustion shows exactly what to add
to `codex.quota_patterns`. A missed pattern degrades to an ordinary failure:
noisy, never unsafe — an unrecognised failure authorizes nothing, and re-running
a stateless call cannot double-post.

**The reviewer gets no repository access.** It runs with `cwd` outside the
checkout. The prompt is self-contained (every turn carries its own CONTEXT block
and the full contract), so the reviewer needs no filesystem at all, and
containment that does not depend on a sandbox flag's name still holds when the
flag is renamed. `codex.sandbox_args` is deliberately **empty** by default rather
than carrying a guessed flag that would look like a control without being one;
`doctor` warns while it is unset and fails if `working_dir` is inside the repo.

**`doctor` probes both seats.** `primary_live` and `fallback_live` are separate
checks. An unverified fallback is not a fallback: checking only the configured
primary means the browser profile's login is first tested at the moment the
allowance runs out. `smoke-browser` is pinned to `browser_chatgpt` for the same
reason — exercising that transport is its whole purpose.

---

## 5d-bis. Chunked packet delivery: a big diff arrives in parts, not omitted

Added 2026-08-14 (pkt-01).

**The problem.** A diff over `packet.DIFF_INCLUDE_MAX_CHARS` (30,000) was
OMITTED with an honest notice, and the reviewer correctly refused to approve
what it could not see. On 2026-08-05 sub-01 produced a 41 KB patch and the
reviewer escalated to the operator, asking either to raise the cap or to
authorize approval unseen. Neither is acceptable: 41 KB is *above* the 40,056
character message that actually failed to send on 2026-08-04 (the composer
accepted it, generation failed server-side, and the turn was never persisted),
and approving unseen removes the review.

**The insight.** The measured failure was ONE MESSAGE being too big, not the
total volume. Several smaller messages are fine.

**The shape.** The packet's git-read facts and the abridged verdict request go
in one message; the patch goes ahead of it as numbered parts, each carrying at
most `DIFF_INCLUDE_MAX_CHARS` of diff — the same budget an inline diff has
always respected. `DIFF_INCLUDE_MAX_CHARS` is **not raised**; chunking is what
removes the pressure to raise it, and
`test_the_cap_is_sized_from_evidence_not_instinct` still pins the number to its
evidence.

Three rules make this safe rather than merely clever:

1. **All or nothing.** No decision is requested until every part is confirmed
   present. A half-delivered patch plus a verdict is approval on a partial
   diff — strictly worse than today's omission, because the notice at least
   tells the reviewer exactly what it cannot see. The transition out of
   `delivering` happens after the delivery loop, and `_step_submitting` raises
   a `StateError` rather than asking when parts are outstanding: the rule is
   not left resting on two lines being in the right order.
2. **Fall back to omission on any failure.** A part that does not land sends
   the pre-chunking notice instead — and that notice now also **disowns** the
   parts that already landed ("If any `autoloop review diff part` messages
   appear above, IGNORE them … whatever landed is a FRAGMENT"), so the reviewer
   is never left holding part of a patch it believes is whole. It says "if any"
   rather than naming a count, because the thing that just failed is precisely
   our knowledge of how many messages reached the conversation.
3. **The integrity binding survives.** `context.report_sha256` hashes the
   COMPLETE logical packet — `state.outbox`, patch inline — not the abridged
   message that is sent. Every part carries a part id derived from the request
   id, and the verdict message lists all of them. A fallback re-renders the
   packet and re-stamps all three holders of the digest (the request,
   `postcommit.packet_sha256`, and `TaskExecution.presented_report_sha256`);
   missing the third would refuse a legitimate approval at push time, long
   after the mistake.

**Confirmation is by readback, never by the send.** Each part is confirmed the
same way a submission is: reload, then look for the id in *persisted* history.
Before concluding a part is absent, the loop mounts the virtualized message tail
if the provider offers `mount_message_tail()` — ChatGPT renders only the newest
few turns (§11), so a rendered-but-unmounted message reading as missing would
throw away a complete delivery. The mount is best-effort and its failures are
swallowed: mounting more history can only ADD evidence.

**Part ids deliberately do not contain the request id.** Every provider answers
"did this land?" with a substring search over user messages
(`BrowserChatGPT.has_request`). A part carrying `alr-…-0007` verbatim would make
`submitting`'s pre-send reconciliation match part 1, conclude the verdict
request had already been sent, and leave the loop waiting for a reply to a
question nobody was asked. `packet.diff_part_id` transforms the id
(`alr-x-0007` → `diffpart_alr_x_0007_01of02`) and `plan_chunked_delivery`
re-checks the built values rather than trusting the format string.

**A rotation gives up the parts.** The parts live in the conversation being
abandoned, and a rotation (§5c) posts only the verdict message — which would
name part ids the replacement chat does not contain. Re-sending them is not an
option either, since the rotation posts the question itself and they would
arrive after it. So `_attempt_rotation` falls back to the omission notice before
rotating a chunked request. That happens AFTER both rotation preconditions: a
rotation refused for a missing `project_url` or a spent budget posts nothing, so
the old conversation still holds the whole delivery and there is nothing to give
up.

**Chunking is opt-in per provider.** `supports_chunked_delivery` is probed with
`getattr`, like every other optional transport capability. `browser_chatgpt`
declares it (one persistent conversation, so earlier messages are context for
later ones); `codex_cli` does not and must not — every turn there is a separate
process with no shared history, so parts sent to it would be reviewed as
separate fragments. A provider without the declaration gets the omission
notice, exactly as before.

**What it costs.** One reload per part (`_deliver_part` reconciles rather than
trusting the send — the same price `submitting` already pays). And ChatGPT
answers each part despite being told not to, so the next part's `submit` waits
out that generation; a reply longer than `browser.send_ready_timeout_seconds`
raises, restarts the browser, and re-enters `delivering`, which resumes from the
persisted cursor without re-posting anything. Recoverable, but it is why the
part count is bounded rather than open-ended.

**Failures in `delivering` never discard the request.** `_handle_git_failure`
treats `delivering` like `ready`: it parks retryably instead of writing a
git-error payload and returning to `ready`, which would overwrite
`pending_request` and abandon a part-delivered patch in the conversation with
nothing left to disown it. Reachable in practice, since the fallback itself
builds a context and `build_context` reads git.

**Bounds.** `packet.DIFF_MAX_PARTS` (6, ≈180 KB of patch) caps the mechanism;
past that, "reply `revise` asking for a smaller commit" — which the omission
notice already says — beats a dozen messages nobody can hold in their head.
That number is a judgement and is labelled as one: the only real data point is
sub-01's 41 KB, which is two parts.

**Not in scope, stated so it is not mistaken for covered.**
`changeset_review.build_changeset_packet` embeds its diff with no per-message
cap at all — a different path, a different bug, untouched here.

---

## 5e. Report compaction: smaller reports, nothing lost

Added 2026-08-01, to cut what the reviewer must read per turn — the packet is
the loop's dominant token cost, and a smaller allowance goes further.

**Measured first.** In a real run, 7 review packets were 71% of all prompt
bytes, and 96% of the largest was the full diff of a generated audit report. In
that report, 28 finding blocks had a median of 1,464 characters and **one had
21,022** — 32% of all finding bytes in a single item. That outlier was not a
verbose finding; it was prose written into `evidence`, a field specified as
"cite code/doc lines".

**Truncating the reviewer's input is not on the table.** The reviewer returns a
`reviewed{report_sha256}` stamp that authorizes a commit. Show it a shortened
artifact and it is stamping bytes it never read, which voids the review
integrity §5b and §5c exist to establish. So the report is made smaller at the
source, and the reviewer always sees 100% of what exists.

**Structural bounds hold findings; they never shorten them.** `findings.py`
bounds the free-text fields (evidence 700, impact 400, action 300, per-criterion
200, whole-finding 2,200 — set well above the median so they catch that one
shape and nothing else). A finding over a bound is neither accepted nor
rejected: it is held as `OversizedFinding` with its item dict byte-for-byte
intact. The domain still counts as covered — a held finding is a real finding,
and marking its domain unusable would hide it.

**One reshape round, then park.** The executor asks that agent to re-express its
own findings within the limits, carrying the originals verbatim so it compresses
its own words rather than re-deriving a possibly weaker finding. Splitting one
finding into several is the intended fix, so ids may gain suffixes. Exactly one
attempt: a finding that is still oversized after being told precisely what was
wrong will not converge by being asked again, and every alternative is worse —
looping burns the agent budget on one item, accepting puts the outlier back, and
dropping or truncating destroys a finding nobody has read.

The park fires on any of: still oversized, reshape agent failed, or **fewer
findings returned than were sent**. That last check matters most — an agent
replying `{"findings": []}` would otherwise look like a clean success while the
finding vanished, the one loss a report's reader could never detect. Originals
are written to `oversized_findings.json` before the park.

**Deduplication merges; it does not discard.** The old rule kept the
higher-quality instance and dropped the other, recording only a `(dropped, kept)`
id pair — throwing away impacts, acceptance criteria and evidence that nothing
else carried. Now a fold unions every unique evidence reference, impact,
acceptance criterion, symbol, dependency and validation command, attributes the
folded text to its original id, takes the stronger severity/confidence, and
takes the **cautious** answer on `safe_to_parallelize` so one agent's optimism
cannot override another's caution.

**Location is a prefilter, never a verdict.** A first version of this widened
the candidate test to "file and symbol overlap" on the reasoning that merging
preserves content so over-merging is harmless. That was wrong, and worth
recording: `generate_tasks` promotes one finding to one task, so folding two
distinct defects yields a single task whose identity, severity, dependencies and
remediation belong to neither — and keeping both texts inside it does not repair
that. An off-by-one and an unchecked return in the same function are two
problems.

So a fold requires location overlap **and** a substance signal (a high Jaccard
overlap of the impact and proposed-action wording, deliberately excluding
evidence — two agents citing the same `file:line` is location agreement wearing
a different hat). The threshold is set high because the failure directions are
not symmetric: failing to merge two reports of one defect costs a duplicated
block, visible to any reader; merging two defects costs a defect, silently.
Deduplication is expected to fire rarely, and that is correct — compaction comes
from the structural bounds, which is where the measured win actually was.

**The task graph renders once.** It was emitted as a Markdown table *and* a JSON
block carrying identical fields. The JSON is the representation that does work
(a `plan` decision is adopted from it), so the table went.

---

## 6. Preflight: `doctor` and the live smoke test

```bash
python -m autoloop doctor         # never submits anything
python -m autoloop smoke-browser  # submits exactly ONE harmless request
```

`doctor` checks: config validity, state-dir writability, lock state, git
identity, branch policy (warns when pushes would be denied), **worker
isolation** (creates a throwaway probe worker repo via the real
`WorkerRepoManager`, runs `verify_worker_isolation` against it, removes it),
**controlled hooks directories** (every accumulated task's worker hooks dir,
and the publisher's own, must be empty), **publisher configuration**
(idempotently (re)provisions, then constructs a real `Publisher` — single
url, no pushurl/mirror/followTags/insteadOf, empty hooks dir), **publisher
URL drift** (§4d — the persisted snapshot vs. the main checkout's current
`remote.origin.url`), CDP endpoint reachability, Playwright presence,
provider registration, conversation-URL shape, and — only when CDP+Playwright
are actually available — that the conversation opens logged-in with the
composer and message selectors resolving. Exit 0/1. "Non-destructive" here
means never irreversible and never touching the real conversation or the
target repo's own history — the probe worker repo and publisher provisioning
are both scoped entirely under `config.state_dir`, the same category of side
effect as the pre-existing state-dir-writable probe file.

`smoke-browser` runs the full normal machinery (request id, CONTEXT stamp,
parser, transcript, diagnostics-on-failure) against an **isolated** smoke
state (`.autoloop/smoke/`), sending one prompt that identifies itself as a
smoke test and demands a contract-v3 `stop`. PASS = the loop terminal state is
`stopped`. It is **exactly one round-trip**: `max_iterations=1`,
`max_parse_retries=0`, `max_policy_denials=0`, `max_consecutive_failures=1`, so a
malformed reply is a FAILURE rather than a corrective re-prompt in a reserved
channel. It can never invoke an executor (a guard executor raises if
dispatch were ever reached) and never touches the main session state. Its waits
are tightened (reply bounds in minutes, one browser failure ends it) so a broken
channel fails fast instead of grinding through retries, and any previously
parked smoke session is archived rather than resumed.

Manual prerequisites for both live commands: the dedicated Chrome profile
running with `--remote-debugging-port=9222`, logged into chatgpt.com, and
`browser.conversation_url` pointing at your persistent conversation.

---

## 7. The audit executor

`audit`/`revise-of-audit` only — anything else returns an error outcome (and
is policy-denied before that). **Runs inside the audit's own isolated worker
repo since 2026-07-30** (§2/§4b) — `AuditExecutor`'s constructor takes the
STANDALONE `git`/`markdown`/`agent_runner` used directly whenever `task` is
`None` (every direct-call test in `test_audit_executor.py`), plus optional
`worker_repo_root_for`/`policy`/`agent_runner_factory`; `cli.py`'s
`_build_executor` always supplies all three in production, so `execute()`
re-roots onto `worker_repo_root_for(task.id)` — meaning steps 1–2 below read
the WORKER repo's state (a frozen clone at `task_base_sha`; uncommitted work
in the MAIN checkout is invisible to the audit), and subagents' `cwd` is that
same worker repo, not the main checkout. Pipeline:

1. Record git state (branch, HEAD, dirty count) — of the worker repo.
2. Run configured **validation commands** (argv lists; binaries restricted to
   `ruff/pytest/python/python3/npm/npx/tsc` — refusal, not trust).
3. Fan out **read-only subagents**, one per domain: architecture/structure,
   tests+CI, security/paths/data-integrity, DB+migrations, documentation
   drift, ingestion pipeline (A1/A2). Invocation is the real Claude Code CLI
   in headless mode: `claude -p <prompt> --output-format json
   --permission-mode dontAsk --allowedTools Read Grep Glob --disallowedTools
   Edit Write NotebookEdit Bash Task Agent WebFetch WebSearch` — subagents
   cannot edit files, run commands, or delegate further. No model API.
4. Persist every raw agent report (`.autoloop/audit/<run>/raw/`) separately
   from anything derived.
5. Parse against the **strict findings contract** (id, category, severity,
   confidence, affected files, symbols, evidence, impact, proposed action,
   dependencies, acceptance criteria, validation commands,
   safe-to-parallelize). Malformed items are rejected with reasons, never
   guessed.
6. **Reconcile**: dedupe across agents (same category + file set, keep the
   higher-confidence instance), classify into confirmed defects / security
   or data-loss risks / architectural risks / documentation drift / missing
   tests / optional improvements / human decisions. Speculation and style
   opinions are recorded but **never promoted to tasks**.
7. **Propose a task graph** (`au-NNN` ids — cannot collide with roadmap
   A1–A11; registry collisions checked; dependencies mapped; priorities:
   data-loss/correctness → security → (dependency edges) → architecture →
   missing tests → maintainability → docs → optional). The graph is a
   PROPOSAL: ChatGPT adopts it — edited as it sees fit — via a normal `plan`
   decision. The registry is never mutated by the audit itself.
8. Write **one** dated Markdown report `docs/AUDIT_YYYY-MM-DD.md` — the only
   file an audit changes. Failed agents are reported as explicit coverage
   gaps (`status = error`, "COVERAGE INCOMPLETE"), never papered over.

**Markdown edit policy** (`audit/markdown.py`): the executor may write only
canonical docs (`CLAUDE.md`, `docs/ROADMAP.md`, `docs/SUMMARY.md`,
`docs/TESTS.md`, `docs/SECURITY.md`, `docs/INGESTION_PIPELINE.md`,
`docs/AUTOLOOP.md`, `docs/TODO.md`) plus at most one new dated report. In
Phase 3 it only writes the report; canonical-doc corrections surface as
proposed tasks instead of silent rewrites, so history is preserved. All
writes land in the change manifest.

**Review cycle**: ChatGPT may `revise` (task_id "audit"), `plan`, `commit` /
`commit_and_push` the approved Markdown paths (all integrity checks apply),
`push`, or `stop`. `implement` is rejected in this phase, and so is
`ask_user` — retired, see §9c.

---

## 7b. The implement executor

`implement`/`revise` of a REAL registry task (never the audit pseudo-task,
never `audit` itself — those stay `AuditExecutor`'s, routed by
`cli._build_executor`'s `_DispatchingExecutor`, §2). Gated upstream by
`policy.implement_enabled` (default `false`); `ImplementExecutor.execute`
refuses anything else as defense in depth, mirroring `AuditExecutor`'s own
refusal branch. Runs inside the task's own isolated worker repo, exactly
like the audit (`worker_repo_root_for`/`policy`/`agent_runner_factory` —
same constructor shape as `AuditExecutor`, same re-rooting behaviour).
Pipeline (`implement_executor.py`):

1. Build a prompt carrying the task's id, title and description (plus
   revision feedback, if this is a `revise` round), telling the agent it may
   only touch files inside its own working directory and must not run `git`
   or any other command.
2. Run exactly ONE subagent via `implement_agent_runner` — the same
   `audit.agents.ClaudeCliRunner` the audit uses, headless (`claude -p ...
   --output-format json --permission-mode dontAsk`), but configured with a
   WRITE-capable tool set: `--allowedTools Read Grep Glob Edit Write
   --disallowedTools NotebookEdit Bash Task Agent WebFetch WebSearch`. `Bash`
   and `Task`/`Agent` stay disallowed on both executors — the executor (not
   the agent) runs validation and owns the commit, and nested delegation is
   out of scope. No `--model` flag is ever passed (`AgentSpec.model` stays
   `""`), so model selection is whatever the `claude` CLI defaults to — there
   is no per-task model table. **Bounded by SILENCE, not elapsed time
   (2026-08-14, `stall-01`).** The runner carries a `stall.WorkerTreeProbe`
   over this task's worker repo, so it SPAWNS and supervises instead of
   running under a wall-clock timeout: while the repo keeps changing the agent
   runs, however long the task takes, and it is killed only after
   `audit.agent_stall_seconds` (default 1800) with no filesystem change at
   all, or at `audit.agent_ceiling_seconds` (default 14400), the absolute
   backstop that should effectively never fire. The retired
   `audit.agent_timeout_seconds` killed six agents mid-write over
   2026-08-05/06 and never once caught a hang. A config still naming it loads
   and is handled explicitly: the value migrates onto
   `audit.audit_agent_timeout_seconds` — the one replacement that keeps its
   meaning — and `cli.emit_migration_notices` prints a notice on stderr, once
   per process, saying what it now does and does not govern. An explicit
   `audit_agent_timeout_seconds` wins over it. The retired name never survives
   into `AuditConfig`, so nothing can read it back. Read-only audit subagents (§7a) keep an
   elapsed bound under `audit.audit_agent_timeout_seconds`: they change no
   files, so there is nothing to observe, and a timeout there costs a re-run
   rather than destroying work. A killed run comes back as an ordinary
   `status="error"` outcome whose summary names the silence, the elapsed time,
   the files and lines it had produced, and that validation never started —
   see `autoloop/stall.py`.
3. **Never trust the agent's own account of what it changed.** After the
   agent returns, `changed_paths` is read from the worker repo's OWN `git
   status --porcelain -z -uall` (`GitGateway.dirty_paths_all`) — `-uall`
   (`--untracked-files=all`) specifically, because the plain form collapses a
   new file inside a brand-new directory to just the directory entry (`??
   d/`), which would go on to fail the post-commit structural check (it
   compares literal file paths, and `d/` does not match `d/f.py`). An empty
   result is `status="error"` ("changed no files") rather than an empty
   success.
4. Re-run the configured **validation commands** (same allowlisted binaries
   and `validation.py` helper as the audit) in the worker repo. Failure is
   `status="error"` carrying the validation summary; the changed files are
   still reported (nothing is rolled back — produce-then-review never rolls
   anything back, §4b).
5. `status="ok"` only when the agent succeeded AND at least one path changed
   AND validation passed. Every other outcome is `status="error"` with an
   honest summary — this executor never raises for an ordinary failure; the
   orchestrator's park/quarantine machinery (§9c) handles it exactly like an
   `AuditExecutor` failure.

**Side effects are confined to the worker repo.** Unlike the audit, this
executor writes no Markdown report, keeps no `run_dir_base`, and never
touches `.autoloop/` — the only durable artifact of a run is whatever the
agent wrote inside its own worker repo, surfaced back as
`ExecutionOutcome.changed_paths`. `cli.py`'s `_DispatchingExecutor` is the
only new wiring in the orchestrator's executor slot; `orchestrator.py` and
the `TaskExecutor` protocol (`executor.py`) are unchanged.

---

## 8. Setup

```bash
pip install -r autoloop/requirements.txt   # playwright (runtime only)
# one-time dedicated profile:
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    --user-data-dir="$HOME/.autoloop-chrome" --remote-debugging-port=9222
# log in to chatgpt.com in that window, create a conversation, copy its URL
mkdir -p .autoloop && cp autoloop/config.example.toml .autoloop/config.toml
# edit .autoloop/config.toml: browser.conversation_url, review [policy]/[audit]
```

The test suite needs none of this. The audit and implement executors
additionally need the `claude` CLI on PATH (it is, in this environment).

### 8a. Restarting Chrome — `browser.restart_command`

```toml
[browser]
restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]
```

That is what the template ships since **2026-08-16**, and it is the only
restart path: `scripts/restart_autoloop_chrome.sh` is retired. The module
(`autoloop/browser/chrome_restart.py`) stops **every** Chrome carrying
`--user-data-dir=<profile>`, polls until nothing holds the debug port, launches,
and reports success only once `/json/version` answers with a browser websocket
URL. Its safety bound: the profile path is matched EXACTLY and the binary name
never is — the operator's everyday browser runs from the same binary under a
different profile. Defaults are `~/.autoloop-chrome` / `9222`, overridable with
`--profile` / `--port` / `--chrome` or `AUTOLOOP_CHROME_PROFILE` /
`AUTOLOOP_CHROME_PORT`. It replaced a shell helper that stopped one pid and
relaunched into a survivor still owning the port, reporting success every time
(`docs/COMMON_ERRORS.md`); a `.sh` also could not be validated at all, since the
validation runner allows only ruff/pytest/python/npm/npx/tsc.

**⚠ Your `.autoloop/config.toml` is not in this repository — change it by
hand.** Copying the template only fixes a *fresh* deployment. A running one is
still holding `["bash", "scripts/restart_autoloop_chrome.sh"]` until you make
the one-line edit above, and nothing in this repo could have made it for you.

**What an unmigrated config gets in the meantime.** It still **loads** —
`load_config` deliberately does not refuse the old value. Everything except a
browser restart keeps working: `status`, `doctor`, `run`, the recovery commands.
Refusing at load would have failed all of them the moment this change merged,
over a setting only a restart reads, taking away the tooling you would use to
fix it. The one thing that does fail is the restart itself, and it fails
**loudly**: `scripts/restart_autoloop_chrome.sh` is now a **failing tombstone**
— it restarts nothing, prints the replacement line on stderr and exits 1, which
both callers surface as `restart FAILED: …`. That is deliberately not bash's
exit 127 (`No such file or directory`), which is what deleting the file would
have produced: same failure, no instruction, in the middle of the browser fault
the restart exists to clear. The tombstone also covers the path still typed by
hand, out of shell history, or by a wrapper of your own outside
`restart_command`.

The file is removed (`git rm`) in a later cleanup, once the live configs have
been switched over and the path has stopped being typed. Re-adding a load-time
refusal belongs to that same cleanup — after migration, not before it.

Run the loop **from the checkout**: `-m` resolves `autoloop` from the working
directory, exactly as the old relative script path did — neither caller
(`cli._repair_browser`, `orchestrator._attempt_browser_restart`) passes a `cwd`.
Those two allow the command 180s; the module's own bounds total ≈123s, sized to
sit under that, so the two move together or not at all.

## 9. First-run procedure

```bash
python -m autoloop doctor          # fix anything red
python -m autoloop smoke-browser   # optional but recommended: one live round-trip
python -m autoloop run --kickoff-audit
```

`--kickoff-audit` opens the session by offering ChatGPT the audit; on its
`audit` reply the executor runs (agents take minutes), the commit happens
automatically (§2/§4b), the review packet goes back for review, and the loop
continues until `stop`/budget/a park.

Ongoing control: `status`, `tasks`, `next-task`, `pause`/`resume`,
`run --answer "..."`, `run --retry`, `run --resubmit` (§5b), `reset --yes`,
`unlock`. `run --null-executor` dry-runs the loop without executing anything.

**`reset --yes` archives the SESSION only; the task registry survives.** It
used to archive `tasks.json` too, unprompted. The two are unrelated — a session
is one conversation plus its in-flight request, while the registry is the
accumulated roadmap (imported audit findings, operator-set priorities,
quarantine decisions) — so reaching for `reset` to clear a wedged conversation
discarded work that had no bearing on the wedge, announced by one line of
output after the fact. `--tasks` is the opt-in for the rare case that means it.
Both are moves to a printed `.bak-<stamp>` path, never deletions.

### 9a. Continuous mode

```bash
python -m autoloop run --continuous
```

Loops the existing phase machine indefinitely instead of running one session
to a terminal phase and stopping: a saved session in a non-terminal phase is
resumed via the ordinary `Orchestrator.run()` (this is what makes a
killed-and-restarted `run --continuous` pick up the saved phase rather than
starting over); at a clean boundary (no session yet, or the last one ended
`stopped`), the selection policy decides what's next — a unique ready task
(`TaskRegistry.next_ready()`, unmodified) starts a new round; otherwise a
changed repository fingerprint (HEAD sha + a content digest of the dirty
tree, `cli.repo_fingerprint`, persisted to
`config.continuous_fingerprint_file`) permits exactly one audit round; an
**unchanged** fingerprint with no ready task sleeps locally and makes **zero
Claude and zero ChatGPT calls** — the fingerprint check runs before anything
that would construct an Orchestrator, executor, or browser client.

A session parked on `failed` (a budget-exhausted browser/git failure — never
routed through the classification below) **always stops** the continuous
loop outright — resolve it with a plain `run --retry` (without
`--continuous`), then restart. A session parked on `needs_user` is **split
by classification** — see §9c: a `task_fatal` park quarantines just the one
task at fault and the loop keeps going; a `loop_fatal` park stops the loop
exactly like `failed` does.
`--kickoff`/`--kickoff-audit`/`--answer`/`--retry`/`--resubmit`/`--max-steps`
are refused alongside `--continuous` — it manages session kickoff, resume
and stepping itself.

### 9b. Task selection: `rt-01` and `next-task`

`autoloop/seed_tasks.json` (git-tracked, alongside `tasks.py`) seeds a fresh
`TaskRegistry` with one entry — `rt-01`, "admin-gate GET /books/packages and
POST /books/import" (`docs/AUDIT_2026-07-30.md` line ~378) — whenever
`.autoloop/tasks.json` does not exist yet. `TaskStore.save` (called from the
normal dispatch path the first time anything touches the task graph) is what
actually creates `tasks.json` on disk; the seed is read-only and never
written to.

```bash
python -m autoloop next-task   # read-only, no lock, never implements or commits
# -> "rt-01 — admin-gate GET /books/packages and POST /books/import"
```

`next-task` prints exactly what continuous mode's selection policy
(`next_ready()`) would pick right now, or "no ready task".

### 9c. Blockers: `task_fatal` vs `loop_fatal`, and the fail-closed default

Before this, `run --continuous` was single-track: ANY park (any of
`orchestrator.py`'s ~25 `_to_needs_user` call sites) stopped the whole loop,
even when the failure concerned exactly one task and every other ready task
could have kept going. Every park is now classified with a `kind`:

* **`task_fatal`** — the problem is about ONE unit of work: post-commit
  verification failures (unexpected path from a hook, path outside task
  ownership, empty commit range, residual dirty worktree, failed re-run
  validation), the review-round cap, the attempt-count ceiling, an
  AMBIGUOUS crash reconciliation for that task, a review packet that could
  not be built (e.g. an oversized diff), or a commit refused before it
  happened (environment/HEAD drift for that task). Continuous mode
  quarantines the task (`TaskRegistry.block` — a NEW `blocked` status,
  distinct from the dependency-derived `blocked` `TaskState`, and distinct
  again from `retired` (§9d), so it never auto-resolves) and clears the
  session, so the very next pass starts a
  clean round on whatever else is READY. **Enforced, not advisory:**
  `policy._check_task_reference` denies any `implement`/`revise` directive
  that names a quarantined task id directly (`task_blocked_by_operator`) —
  `next_ready()` skipping it is not the only thing standing between
  ChatGPT and re-triggering the same failure; `TaskRegistry.mark_in_progress`
  refuses it too, defense in depth for any dispatch path that bypasses
  policy.
* **`loop_fatal`** — the problem is about the ENVIRONMENT or the operator:
  browser/login failures, response timeouts, submission ambiguity,
  publisher URL drift, a protected-branch refusal / `allow_push` disabled,
  a parse/iteration budget exhausted, a plan rejected or a review stamp
  mismatched until the shared denial budget ran out, or anything not
  confidently classifiable. The whole loop stops, exactly as every park did
  before this split existed. **`ask_user` cannot reach a park at all:** it is
  retired, a legacy one is denied (`legacy_ask_user_retired`) and corrected
  like any other policy denial, and the one path that used to remain — a
  reviewer answering it until the denial budget ran out — now ends the run
  instead of parking it (see the fault stop below).

**Fault stop: the terminal that is not a park and not a `stop`.** An
exhausted POLICY-DENIAL budget (`orchestrator._handle_policy_denial` →
`_to_fault_stop`) ends the run in `stopped` with `stop_kind="fault"` rather
than parking on `needs_user`. A park asks a human a question; there is no
question here, because the only thing that could produce a directive policy
accepts is the reviewer that just spent the budget. Three consequences worth
knowing:

* it still records a `loop_fatal` `blockers.Blocker` under the same code
  (`policy_denial_budget_exhausted`), so `blockers`/`answer` are unchanged;
* `run --continuous` STOPS on it (exit 2) instead of treating `stopped` as a
  clean boundary — otherwise the selection policy would start a fresh session
  into the identical wall on the next pass;
* `smoke-browser` and plain `run` both read `stop_kind` rather than the phase,
  so a run that died this way is never reported as a completed one. A
  reviewer's own `stop` carries `stop_kind="contract"`; a state file written
  before this existed carries `""` and is read as an ordinary clean boundary.

  The sibling budgets deliberately still PARK, though all three spend the same
  `state.policy_denials` counter: a rejected plan is about an operator-owned
  roadmap they can repair, and a repeated review mismatch can mean the
  repository moved under the loop. Both have a human-side explanation; a policy
  denial does not.
* **The default is `loop_fatal`.** `orchestrator._to_needs_user(question,
  ..., kind="loop_fatal", code="unclassified")` — an unclassified or newly
  added park site fails closed: it stops the loop rather than silently
  being treated as safe to quarantine and churn past. Widening a park to
  `task_fatal` is a deliberate per-site decision, never an accident of
  omission.

**Every park, of either kind, is persisted** as a `blockers.Blocker`
(`autoloop/blockers.py`) — one JSON file per blocker under
`.autoloop/blockers/`, carrying the exact operator-facing question text,
extra `detail` (paths/shas/reasons), the loop phase it happened in, and a
stable id (`blk-<task_id>-<NNN>`, or `blk-(loop)-<NNN>` for a park not tied
to any task). This is independent of continuous mode — a plain `run` also
writes one on every park, so `blockers`/`answer` work either way. A corrupt
blocker record raises rather than being read as absent, same rule as every
other store in this package.

```bash
python -m autoloop blockers            # open blockers: id, task, code, question, age
python -m autoloop blockers --all      # + resolved ones
python -m autoloop answer <id> "<text>"  # resolve + (if task_fatal) unblock the task
```

**Exhaustion.** At a clean boundary, "no ready task and the fingerprint is
unchanged" used to always mean "sleep and poll again" — and still does,
UNLESS there is at least one OPEN blocker at that point. With one, "nothing
ready, nothing new to audit, and something is still waiting on a human" is
genuinely "nothing can proceed autonomously": every open blocker (id, task,
question) is printed and the process exits `0` — a clean end, not an error.
Zero open blockers is still the ordinary idle steady state, unchanged.

`reset` archives `state.json`/`tasks.json` but never touches
`.autoloop/blockers/` — a blocker recorded against a task that a later
`reset` + fresh plan no longer has will fail to `unblock` (there is nothing
to unblock), but `answer` still resolves the blocker record itself; the CLI
reports this rather than raising.

### 9d. Retired: superseded work is not blocked work

`blocked` used to carry a THIRD meaning, and it was the one that made the
dashboard's blocked count useless. As of 2026-08-14 seven tasks were stored
`blocked`; six of them were retirements — work superseded by a successor task
and never coming back — saying so only in free-text `blocked_reason`:

| task | why it stopped |
|---|---|
| `brw-02`, `brw-04` | superseded by `brw-06` |
| `brw-05` | retired alongside `brw-02` / `brw-04` |
| `brw-06` | split at the reviewer's request (`blk-(loop)-018`) into `brw-07` + `brw-08` |
| `sub-01` | superseded by `sub-02` and `sub-03` |
| `dash-01` | stale since 2026-08-03 — in_progress at dispatch with no candidate and no execution record, so nothing will ever finish it |

Only `audit-0003` was a genuine failure. The operator read the dashboard, saw
seven blocked rows, and reasonably asked when they would be fixed; for six the
answer was "never — they already were, under a different id". **A status that
means both "needs you" and "needs nobody" is not a call to action.**

So there are three not-running states, and they differ by WHO resolves them:

| state | stored `status` | resolved by |
|---|---|---|
| `BLOCKED` | `pending` (derived) | the dependency completing — nobody has to do anything |
| `BLOCKED_BY_OPERATOR` | `blocked` | an operator answering the blocker (`autoloop answer`) |
| `RETIRED` | `retired` | nobody. It is already over. |

**`Task.superseded_by`** names the successor id(s), so the chain is
machine-readable instead of prose: `brw-05 → brw-02 → brw-06 → brw-07/brw-08`
is followable one hop at a time, each hop recording what that task's own reason
said. It is validated for SHAPE only — a successor need not exist (brw-06 was
split before brw-07/brw-08 were planned), it is not a dependency, and nothing
schedules off it. Empty is legal and means "stale, not replaced" (`dash-01`).

**Nothing is deleted, ever.** `retire` keeps the task, its description, its
scope and its `blocked_reason`; there is no un-retire and no remove. The
supersession chain is the only record that brw-07/brw-08 continue brw-02/brw-04
— regression history, the same rule `docs/SECURITY.md` findings live under.

```bash
python -m autoloop retire <task-id> --superseded-by <id> [--superseded-by <id>] \
                                    [--reason "..."]
```

Takes the loop lock (it writes `tasks.json`). Accepts a pending, in-progress or
quarantined task — `dash-01` was in-progress, which is exactly the shape that
needs retiring — and refuses only a completed one.

**A retirement is written ONCE.** A second `retire` on the same task cannot
add, replace, reorder or reword anything: an exact repeat is reported as the
no-op it is, and anything else is refused with `task_already_retired`. This is
not tidiness — omitting `--superseded-by` on a repeat used to assign `()` over
the recorded chain, so `autoloop retire brw-02` was the command that deleted
the record this section says is never deleted. `block` stays idempotent in the
other direction (it refreshes the reason) because a quarantine is a live
question that can genuinely re-fire; a supersession cannot. Argue with a
recorded retirement by planning a task, not by overwriting the last decision.

**Retiring a quarantined task closes its blocker too.** The two halves live in
different files — the row in `tasks.json`, the question in `blockers/` — and
the blocker list is read independently of the registry by `start`,
`health.check` and the heartbeat. Moving only the registry produced a row that
said "waits on nobody" beside a loop that was still stopped waiting on exactly
that task. `cli._reconcile_retired_blockers` closes them via
`BlockerStore.archive_stale`, so the record keeps its question, detail,
recurrence count and session id and gains a machine reason naming the
retirement — never an `answer`, which would forge the operator confirmation
`_RESOLUTION_PRECONDITIONS` exists to demand.

**Only the QUARANTINE is closed — `kind="task_fatal"`, as an allowlist.** A
quarantine asks about the one task at fault, so retiring that task genuinely
makes it unanswerable. A `loop_fatal` record is the opposite: a loop-wide
safety condition that merely names whichever task was in flight when it fired
(`checkout_escape_detected`, `primary_checkout_dirty`, a worker or publisher
environment failure). Closing one of those on a retirement would manufacture
resolution of the condition itself — `start` proceeds, `health` goes quiet, and
the escaped write or the dirty checkout is still there. So every `loop_fatal`
blocker is preserved regardless of its task id, until its own precondition
recheck clears it or the operator archives it explicitly; a task holding both
kinds has exactly one of them closed. An unrecognised or empty `kind` counts as
loop_fatal and is left alone, the same fail-closed reading
`orchestrator._to_needs_user` and `_handle_parked_task` already use. `(loop)`
blockers are never swept either: a login expiry is a loop-level condition no
task retirement answers.

The sweep runs from `retire` itself, from `start`'s preflight, and at the top of every
`run --continuous` iteration — the last two because the six migrated
retirements below change status on LOAD, with no command run to notice their
records were left open. The continuous sweep is at the top of the iteration
rather than at the exhaustion check because the readers that misjudge an
orphaned blocker are out of process: a loop with plenty of ready work would
otherwise leave `health` reporting `stuck_blocked` for hours while working
perfectly.

**The six above are migrated in code, not by hand.** `tasks._RETIREMENTS` maps
each id to the successors read from its existing reason, and
`_migrate_retirements` applies it inside `TaskRegistry.from_dict`; the live
`tasks.json` is loop state outside the repository, so a load-time migration is
the only route to it. Two guards: the stored status must still be `blocked`
(making it idempotent — after the first save nothing matches) AND a marker must
still appear in the reason (making it self-limiting — a revived task
quarantined later for a real reason is left alone). A reason that was reworded
simply does not migrate and stays quarantined, and `autoloop retire` is the
manual route. `audit-0003` is deliberately absent from the table. The
successors it writes go through the same `_validate_superseded_by` every other
writer uses, and so does the field as READ BACK off `tasks.json`
(`_persisted_superseded_by`): `from_dict` bypasses `add_many` by design, so it
is the only gate a stored or hand-edited row passes, and a malformed chain is
`StateCorruptError` rather than a bare string silently loading as one successor
per character.

Enforcement mirrors the quarantine's: `policy._check_task_reference` denies an
`implement`/`revise` naming a retired id (`task_retired`, naming the
successor), and `TaskRegistry.mark_in_progress`/`mark_completed`/`block` refuse
it too as defense in depth — `block` because it ends in a bare
`status = "blocked"` that would silently un-retire the row, which should be
unreachable (a task that cannot be dispatched cannot park) and so is treated as
a fail-closed loop_fatal rather than smoothed over. `cli._merge_window_blockers` treats RETIRED as terminal
alongside completed and quarantined, so a superseded task's leftover execution
record cannot hold the merge window shut on work nobody will finish.

The dashboard groups by it (§ the roadmap panel in `dashboard.py`): **Ready /
In progress / Blocked / Needs a human / Retired / Done**, with the Retired
group collapsed like Done and its rows naming the successor.

One consequence, stated rather than hidden: a task that DEPENDS on a retired
one stays BLOCKED forever, since only `completed` satisfies a dependency. That
is correct — the prerequisite genuinely never happened under that id — and it
is not new (a retirement stored as `blocked` did the same). Re-plan the
dependent against the successor `superseded_by` names.

## 10. Recovery procedures

| Situation | Do |
|---|---|
| Crash / Ctrl+C anywhere | Just `run` (or `run --continuous`) again — every phase is persisted; requests are never double-submitted; executing re-verifies from saved state. |
| `stale lock` error | Inspect `python -m autoloop status`, then `python -m autoloop unlock` (refuses live locks). |
| Logged out mid-run (`needs_user`) | Log the profile back in, `run --retry`. |
| Browser dead / CDP unreachable | `python3 -m autoloop.browser.chrome_restart` from the checkout (§8a) — or relaunch the profile by hand (§8) — then `run --retry` (or just `run` if not parked). |
| `restart FAILED: … restart_autoloop_chrome.sh was RETIRED` | Your `.autoloop/config.toml` still names the shell helper retired 2026-08-16. Nothing else is broken — the config loads and every other command works — but no browser restart will succeed until you set `restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]` (§8a). The loop's live config is not in this repo, so nothing could have done it for you; the failing tombstone carries that exact line on stderr. |
| Parked `browser_restart_cooldown_blocked` | Repeated browser failures whose restart `browser.restart_cooldown_seconds` refused, so none was ever attempted (§5c). Restart the browser by hand (`python3 -m autoloop.browser.chrome_restart`, §8a) — or lower that cooldown if it is set too high for this machine. Then close the blocker it recorded (`python -m autoloop blockers`, then `answer <id> "..."`) and `run --retry`: an open blocker stops `start` and ends a `--continuous` pass, so retrying without it just parks again. Those failures never spent the failure budget, so nothing else needs resetting. |
| Parked `rate_limited` | ChatGPT is rate-limiting the ACCOUNT ("Too many requests…"), and it did not lift across the loop's whole back-off budget (§5c). **Do not restart the browser** — the limit is server-side and a restart adds another request; that reflex is what caused the incident this park exists to replace. Leave the account idle for a while (an hour is usually plenty), close the blocker (`python -m autoloop blockers`, then `answer <id> "..."`), and `run --retry`. If it recurs, raise `browser.rate_limit_backoff_seconds` so the loop waits longer before re-probing. The back-offs never spent the failure budget, so nothing else needs resetting. |
| Repeated malformed replies / denials | Loop parks with the reason; talk to the conversation manually if needed, then `run --answer "..."`. |
| **Ambiguous submission** (`needs_user`, "submission … is AMBIGUOUS") | The by-content search already ran and did not prove the request present — the park text says what it found (nothing, a different chat, or that it refused to conclude), so read that line first. Open the conversation and look. If the request is there, `run --retry` (reconciles and continues). If it is genuinely absent, `run --resubmit` authorizes exactly one more send of the same id. Autoloop resolves this by itself only when it can PROVE the request is in this request's own conversation; it never decides the absent direction for you — see §5b. |
| `send-not-ready` / `composer-not-synchronised` diagnostics | The editor never accepted the input, so **nothing was sent**: safe to `run --retry`. If it repeats, the composer selectors or the input method need attention (`browser/selectors.py`, `browser/chatgpt.py::_enter_prompt`). |
| Crash mid-audit | `run` — the audit directive re-dispatches (`_resolve_audit_task` resumes the SAME per-run worker repo/unit id when redispatching within the same iteration; prior run's raw reports remain under `.autoloop/audit/`). |
| Crash mid-commit | `run` — `commit_and_capture` is crash-recoverable via `CommitIntent`/`reconcile_after_crash` (§4b), never a bare "message matches HEAD" idempotency shortcut. |
| `doctor`'s `publisher_url_drift` check fails | The main checkout's `origin` changed since the publisher was last provisioned. Verify the NEW destination is actually correct, then `python -m autoloop reprovision-publisher --confirm` (§4d) — the only way the snapshot updates. Any push attempted before that is refused, not silently redirected. |
| `run --continuous` exited 0 with "continuous mode: exhausted" | Nothing autonomous is left to do — every ready task is done/blocked and the repository fingerprint hasn't changed. `python -m autoloop blockers` lists what's waiting; `answer <id> "..."` each one (unblocking any `task_fatal` task), then restart `run --continuous`. |
| `run --continuous` exited 2 | A `loop_fatal` park (§9c) — the environment or the operator is the problem, not one task. `python -m autoloop blockers` shows the question (also in `status`); resolve it (fix the environment, or `answer` it if that's enough), then `run --retry`/`--answer` (WITHOUT `--continuous`) before restarting `run --continuous`. |

---

## 11. Known limitations

* `implement`/`revise` of an ordinary registry task is still policy-denied
  until `policy.implement_enabled = true` (default `false` as of this
  writing — an operator decision, made separately from this doc). Once
  flipped, `ImplementExecutor` (§7b) is wired up and write-capable — this is
  no longer "no executor exists". `audit`/`revise("audit")` are unaffected by
  this gate and always run.
* `ImplementExecutor` runs exactly ONE subagent per `implement`/`revise`
  call — no fan-out, no self-review, no second opinion. Quality depends
  entirely on that one pass plus the configured validation commands and the
  two-round revise cap (§4b); there is no retry-with-different-prompt on a
  weak first attempt.
* The audit re-runs agents from scratch on `revise` (no incremental caching).
* The retired change-manifest attribution (§4) was time-based: edits made by
  a human *during* an executor run were indistinguishable from task work.
  Produce-then-review (§4b/§2) does not have this problem — a task's worker
  repo starts from a clean checkout of `task_base_sha`, so path ownership is
  `outcome.changed_paths`, not inferred from a before/after diff of a shared
  tree.
* Produce-then-review's two-round cap (§4b) is per-task and does not reset —
  a task that hits it stays parked; there is no `--revise-again` override,
  only the general `run --answer` / manual state edit escape hatches §10
  already documents for any park.
* Worker/audit unit ids (`t1`, `audit-0007`, ...) are not scoped per session:
  `.autoloop/workers/<id>` and the matching `TaskExecutionStore` record
  persist across a `reset` (which only archives `state.json`/`tasks.json`).
  A fresh session reusing the same id (rare — `reset` restarts iteration
  counting, and audit ids are minted from `state.iteration`) would collide
  with `WorkerRepoManager.create`'s "already exists" refusal rather than
  silently reusing stale state. No production code path currently cleans up
  a completed task's worker repo/hooks dir/execution record.
* Subagent quality/latency depends on the local `claude` CLI; a failed agent
  is reported as a coverage gap, not retried automatically.
* The publisher URL policy (§4d) is a provision-time snapshot by design — an
  operator-changed `origin` is DETECTED (`doctor`, `_dispatch_task_push`) but
  never auto-healed; `reprovision-publisher --confirm` is a manual step.
* Every approved audit publishes its own `refs/heads/autoloop/audit-NNNN`
  branch to the real remote (same as any task) — nothing prunes these.
  Continuous mode (§9a) runs at most one audit per repository-fingerprint
  change, but a long-lived deployment will still accumulate one remote
  branch per approved audit round over time; branch cleanup on the remote is
  an operator task today, not something autoloop does for you.
* `doctor`'s live check and `smoke-browser` require the real dedicated
  browser; hermetic tests mock them, so "implemented" ≠ "live verified" until
  smoke-browser has actually passed on your machine.
* One loop per state dir (enforced); multiple state dirs are possible but
  share nothing.
* An ambiguous submission needs a human to look at the conversation. That is
  deliberate — the alternative is a possible duplicate post — but it does mean
  the loop is not fully unattended in that one case.
* `submitting` costs one reload per request (the pre-send reconciliation). That
  is the price of never trusting an optimistic bubble.
* **ChatGPT virtualizes the message DOM.** Measured live 2026-07-30: a
  10-message conversation mounted only the 6 most recent nodes (3 turns);
  older turns exist server-side but are not in `innerText` until you scroll.
  Everything the loop needs is *usually* in the newest turn — `reconcile`
  checks the request that was just sent, and `await_response` needs the last
  message. But it means **a DOM read is not a full history read**: never infer
  "the conversation contains only X" from a message count. Chunked packet
  delivery (§5d-bis) is the one path that can need an older turn, when a
  resumed delivery re-confirms parts sent before a crash; it calls the optional
  `mount_message_tail()` before ruling a part absent. That capability is not
  implemented on `BrowserChatGPT` yet, so today a resumed delivery reads only
  what is mounted and falls back to the omission notice if an earlier part is
  no longer rendered — safe, but a re-send of work that did land.
  The one readback that DOES mount its own tail is the by-content conversation
  search (`find_conversation_with`, 2026-08-16): it repeats a "go to the end"
  gesture and refuses to answer at all rather than call something absent while
  the list may still be unpainted. It got that because it is the read a
  decision is made from — on 2026-08-05 `alr-af11e1b3-0006` parked as ambiguous
  while the chat held the request and its answer, six scrolls below the fold.
  The mount is deliberately private (`_mount_message_tail`), so the delivery
  probe above still finds no public capability and keeps its historical
  behaviour.
  **Absence needs two independent proofs** (corrected 2026-08-16, before the
  search was wired to any decision): the session must report the list is AT ITS
  END, *and* the mounted window must then stay byte-identical across
  consecutive reads. Each alone has a false positive that looks exactly like
  success. The node count is not even a candidate — a virtualizer may slide a
  constant-size window, so a count reading 6 before and after a gesture is
  consistent with six different messages having gone past; that count-based
  version stopped after two gestures and reproduced the very park it was
  written for. Content-only is ambiguous in a subtler way: an unchanged window
  says the GESTURE stopped mounting, which is the tail when the gesture works
  and the opening window when it silently missed (End goes to whatever holds
  focus). And end-of-list alone is ambiguous because ChatGPT follows a
  streaming answer down — the view is at the bottom while the content is still
  arriving.
  The position signal is a return value on the optional `scroll_to_end`
  capability: True (container at its end, a chat too short to scroll included),
  False (more below), None (cannot measure). `PlaywrightSession` computes it
  from the real container by walking out from the last mounted node; an adapter
  that answers None — the End-key fallback included — keeps its SIGHTINGS and
  loses only the ability to rule things out. The verdict comes from what the
  mount SAW rather than from a readback after it, since a slide can carry the
  request into the window and out again. The deliberate cost is that a
  still-streaming chat, a stuck gesture and a session without the signal all
  answer `ConversationSearchInconclusive` instead of `None` — refusing to rule
  beats ruling wrong, which is the whole reason this readback exists.
* Selector defaults will drift with ChatGPT's UI eventually
  (`browser/selectors.py` is the fix point).
