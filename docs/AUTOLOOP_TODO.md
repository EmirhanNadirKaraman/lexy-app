# Autoloop — TODO

Open work on the loop itself. App-level tasks live in `docs/TODO.md`; security
findings live in `docs/SECURITY.md` and are referenced by id here rather than
duplicated. Everything below was found by running the thing, not by planning.

---

## P0 — blocks trusting an unattended run

### A1. No live progress while agents work
The audit fans out six headless `claude -p` subagents whose output is captured
only **when each finishes**. Nothing streams. For a fan-out that takes tens of
minutes the operator sees an empty `raw/` directory and a silent
`.autoloop/loop.out`, then a burst. There is no way to tell "working" from
"wedged" without inspecting process state by hand.

Fix: emit a transcript event when each agent **starts**, not only on completion,
and surface per-domain state in `status`. Cheap, and it removes the single worst
feedback gap in normal operation.

### ~~A2. Operator-authored changesets still cannot be published by the loop~~ — RESOLVED 2026-08-01
`review-changeset` bound correctly and ChatGPT approved, but dispatch refused
with `legacy_git_path_retired`.

**Root cause: `orchestrator.py`'s `_step_awaiting` built `LastResponse` with
`postcommit=req.postcommit` and simply never copied `changeset=req.changeset`.**
It is the only production construction of `LastResponse`, so a binding that was
built, submitted and awaited perfectly intact was dropped at the moment the
response was persisted — reaching dispatch as `None`, where the changeset
branch is skipped and the retired legacy path is all that is left to refuse.

The earlier note here said this diagnosis was "disproved". It was not: the
original check inspected `last_response` AFTER `orch.run()` returned, and
`_step_executing` consumes `last_response` — so that probe reads `None` whether
the bug is present or not. The measurement was wrong, not the hypothesis. The
regression now probes AT `_dispatch_changeset_push`, and is confirmed to fail
when the one-line copy is removed.

Every existing changeset test hand-built a `LastResponse` that already carried
the binding, so none of them ever exercised the construction that lost it —
that is why 6 passing tests coexisted with a completely broken feature.

Also closed alongside it: a queued changeset whose packet lacks the four
identifiers now parks `changeset_binding_missing` BEFORE the packet is sent,
instead of spending a full review round to discover the approval cannot
publish anything. No fallback to an unbound send.

### A3. Stale `TaskExecution` records strand when `workers_root` changes
The orchestrator recomputes a worker path from the current `workers_root`
instead of using the record's stored `worktree_path`, so moving the root leaves
existing records pointing nowhere: `FileNotFoundError` on
`~/.autoloop/workers/<task>` while the real repo sits at the old location.
Worked around by archiving the record to `.autoloop/executions-archive/`.

Fix: reconcile on load — if the stored path exists and the recomputed one does
not, either adopt the stored path or fail with a message naming both. Never
silently recompute.

---

## P1 — correctness and usability

### B1. `review-changeset` has no `--dest-ref` override
It derives the destination from whatever branch is checked out. Run from a
worktree on a different branch, it silently pins that branch — a binding that
would have published to a ref nobody asked for. Add an explicit override and
refuse when the candidate is not on the resolved branch.

### B2. `review-changeset` does not validate identifiers at queue time
The binding gate requires `base_sha`, `candidate_sha`, `branch` and `dest_ref`
to appear as literal text in the payload. A `--packet` body missing any of them
binds nothing — discovered only *after* a full review round was spent. Check at
queue time and refuse immediately.

### B3. `state_dir` is relative and resolves against cwd
Running a command from the worktree writes session state into the worktree's
`.autoloop/`, and `doctor` then reports on a different session than the one
running. Same class as the bug fixed for `workers_root` and the publisher paths
— two of three were resolved, this one was missed.

### B4. Default packet rendering is the full patch
`MAX_REVIEW_DIFF_BYTES` (400 kB) correctly refuses rather than truncating, but
because the default rendering is the complete patch, any real range hits the
wall and forces `--packet`. Make the default a summary — commit list, path set,
diffstat — and include the full patch only below a smaller threshold. The four
identifiers must stay in the body at any size, since `report_sha256` covers
exactly those bytes.

### ~~B4b. Post-commit validation ignores the task's declared validation~~ — RESOLVED 2026-08-01
`_run_post_commit_validation` re-ran `config.audit.validation_commands`, not
`task.validation`. So a task like `rt-01`, which declares the backend suite
precisely because the configured default does not cover what it changes, had
its *reviewed commit* checked by the default only — the declared validation ran
once, in `ImplementExecutor`, against the pre-commit tree. That was a weaker
guarantee than §4b claims for the produce-then-review path, whose whole point
is that a commit hook can change committed content in ways pre-commit
validation never saw.

**Fixed:** `TaskExecution` now persists `validation_commands` +
`validation_cwd`, captured from the `Task` at dispatch (and re-synced onto a
loaded record, so a pre-existing execution file does not silently fall back).
`_run_post_commit_validation` takes the execution rather than a bare path and
runs those commands from that cwd, falling back to the configured default only
when the task declared none — the same `tuple(task.validation) or default`
rule `ImplementExecutor` already used, so the two ends now agree by
construction. A declared `validation_cwd` missing from the committed tree is a
refusal, not a silent run from the repo root.

Regression: `test_post_commit_reruns_the_tasks_own_validation_not_the_audit_set`
drives a REAL `ImplementExecutor` and the orchestrator through ONE recording
runner, so "the same commands before and after" is observed rather than
asserted twice against separate doubles. Verified to fail when the fix is
reverted — it then records `[declared, ruff-check]`, which is the bug exactly.

### B7. The protected-branch check is vacuous on a detached HEAD
Running `doctor` from a detached-HEAD worktree reports:

    [ok] branch_policy   pushes to '' are permitted by policy

The current-branch lookup returns an empty string, and `''` is not in
`protected_branches`, so the check passes by default. Nothing was at risk when
this was found (publication was disabled, and `push_exact` re-checks the
protected set independently at push time), but a gate whose answer is
"permitted" for a branch name that does not exist is the wrong shape: it
should refuse an empty/undeterminable branch rather than treat it as unlisted.

Found while standing up a clean runner worktree so the loop could dispatch
write-capable work while the primary checkout held unrelated uncommitted
changes (see B8).

### B8. `state_dir` is relative, so a second checkout cannot share a session
A dirty primary checkout parks `primary_checkout_dirty` (loop-fatal) before any
write-capable agent starts — correct, since a dirty checkout cannot be a
trustworthy baseline for escape detection. But when the dirt is unrelated work
in progress, the loop is stuck until someone commits or stashes it.

The workaround is a second, clean worktree — which only works because
`state_dir` can be given an ABSOLUTE path, so the runner shares the original
session, blockers, tasks, publisher and transcript. That is B3 in reverse: the
relative default resolves against cwd and would have silently created a second,
empty session instead. Worth making first-class: a documented "runner checkout"
mode, rather than something each operator rediscovers under pressure.

### ~~B10. Nothing at runtime ever marks a task completed~~ — RESOLVED 2026-08-04
`TaskRegistry.mark_completed` (`autoloop/tasks.py:333`) is the only line in the
codebase that writes `status = "completed"`, and it has **no runtime caller** —
verified 2026-08-04:

```
rg -n "mark_completed" autoloop/ --glob '!*/tests/*'
→ autoloop/tasks.py:323:    def mark_completed(...)      # the definition, nothing else
```

The `Decision` enum has no terminal member either (audit / plan / implement /
revise / commit / push / commit_and_push / stop / ask_user), so a reviewer has
no vocabulary to say "this task is done". The publish path
(`orchestrator._dispatch_task_push`) clears `state.task_execution`, logs
`task_pushed` and returns to READY — the registry is never touched. A task that
publishes its candidate therefore stays `in_progress` forever.

`rt-01`, `dash-02` and `audit-0001` read `completed` because they were marked by
hand, out of band. Their provenance is inconsistent, which is why the pattern
looked like a mechanism: `rt-01`'s candidate is not even an ancestor of the
branch head, so completion never tracked merging either.

Consequence, before the 2026-08-04 fix: `_merge_window_blockers` exempted only
`COMPLETED` / `BLOCKED_BY_OPERATOR`, so **every task the loop published closed
the merge window permanently**. Four tasks were holding it shut, three of them
already pushed to their own side branches, and no amount of waiting could open
it. That command now exempts a candidate whose publication it can confirm
against the remote, which makes it usable, but the underlying gap remains:
nothing retires an execution record, so a published task is still
re-dispatchable and would park on `task_base_behind_head` (B9) after a merge.

**RESOLVED 2026-08-04.** `Orchestrator._mark_task_completed`, called from exactly one place: immediately after `_dispatch_task_push` has reconciled `landed == candidate_sha` against a fresh `ls-remote`. "Completed" therefore means *the reviewed object is durable on its own side branch* — deliberately NOT "merged into the base", because nothing here observes merges and a completion the loop cannot verify is worse than none. Every failure is swallowed to a log: the push already succeeded, so turning bookkeeping into a park would strand work that is safely published. Writing it surfaced a second gap — `mark_completed` guarded COMPLETED and BLOCKED but not BLOCKED_BY_OPERATOR (unlike `mark_in_progress`), so the first cut silently completed a quarantined task and deleted the operator decision it recorded; guarded now. Five tests; the mutations (never mark, mark without persisting, drop the quarantine guard) fail 3, 1 and 1.

Superseded original plan: give it exactly one producer —
either post-push (published to its side branch) or post-merge (the side branch
landed in the base, which nothing currently observes). Whichever is chosen, the
merge-window predicate should keep gating on publication rather than on task
state, because publication is the property that actually makes the work durable.

### B9. A task's base sha is pinned at first dispatch, so an upstream fix never reaches a retry
`TaskExecution.task_base_sha` is recorded once, when the record is created, and
never revisited. Every later attempt recreates the worker at that same base. So
when a task fails for a reason that is fixed ON THE BRANCH afterwards, the
retry rebuilds the worker at the OLD base and fails identically — forever.

Observed 2026-08-02: `audit-0001` was refused because post-commit validation
found 2 failing autoloop tests. Those were pre-existing defects on the branch,
fixed in `7a207f2`. The retry still failed with the same two, because the
record's base was `39cedcf` — one commit earlier. The worker suite reported
929 passed against the checkout's 931, which is the tell: a two-test gap
between worker and checkout means the worker is not at the base you fixed.

This compounds B6. There, a repeated environmental failure burns the attempt
budget; here, the retry cannot possibly succeed, so every one of those attempts
is spent on a base known to be broken.

**Fix:** when a task is retried after a `task_fatal` park and its base is an
ancestor of the current branch head, re-base the record — record the new base
and recreate the worker there — or refuse with a message naming both shas
instead of silently retrying at the stale one. Never silently reuse a base the
branch has moved past. Workaround until then: archive
`.autoloop/executions/<task>.json` so a fresh record is created at current
HEAD (the same manual step A3 already needs).

### B6. A repeating ENVIRONMENTAL validation failure burns the attempt budget
rt-01 consumed five attempts on 2026-07-31. Rounds 1–3 (20:34, 20:46, 20:59)
failed **identically**: `validation failed after implementation — ruff check .:
PASS; python3 -m pytest -n auto -q: FAIL`. The agent implemented the task
correctly every time (the quarantined worker for attempt 5 still holds a
modified `routers/books.py`, a new `test_books_import_admin.py` and the doc
updates); validation failed because the worker had no database credentials,
which is environmental and could not be fixed by retrying.

Each failure left the worker dirty, so the next round quarantined it as
"residual uncommitted state from a prior attempt" and started over — correct
per M1 finding #3, but it means the loop redid the same work three times and
spent three of its attempts on a condition no amount of retrying could clear.

The blocker taxonomy already distinguishes `task_fatal` from `loop_fatal`, but
a validation failure is always charged to the task. **Fix:** when N
consecutive rounds fail with a byte-identical validation summary, stop
retrying and park as environmental — a retry that cannot change its own
outcome is not a retry. The credential cause is fixed (§4g), so this is about
the next environmental failure, not this one.

### B5. Fail-closed check for unmapped blocker codes
Three times now a new `loop_fatal` code shipped without a
`_RESOLUTION_PRECONDITIONS` entry, meaning an environmental blocker could be
cleared by arbitrary answer text. The current test is a curated reverse-list,
which catches stale keys but not missing ones. Invert it: walk the AST for every
emitted `code=`, and require each security/environment code to be either mapped
or explicitly listed as text-resolvable.

---

## P2 — deferred, with reasons

### C1. Escape detection is not prevention — `docs/SECURITY.md` S24, OPEN
The write-capable agent has no path jail; confinement is a prompt instruction
plus an after-the-fact snapshot diff. A write outside the worker repo is caught
and parks loop-fatal, but is not blocked. Acceptable under a trusted-agent
model, which is what this deployment assumes. A real jail (wrapper rejecting
tool-call paths outside `repo_root`, or an OS sandbox) is the actual fix.
`.git/` internals remain uncovered.

### C2. `commit_adopted` is superseded and unreachable — S22
No production caller. Retained only until its tests are ported to
`commit_verified_tree`. Delete both together.

### C3. `RotationRecord` has no `old_epoch` field
The retired epoch is recoverable only as `epoch - 1`. Fine today; add the field
if rotation ever allows more than one per run.

### C4. Audit-branch sprawl
Every approved audit publishes its own `autoloop/audit-NNNN` branch and nothing
prunes them. A long-running deployment accumulates one remote branch per audit.

---

## C5. Self-hosting with rollback — the interesting one

The loop can already modify itself: workers edit a *copy*, exact-SHA publication
makes "which version am I running" precisely answerable, and `doctor` +
`smoke-browser` exercise the real transport. What is missing is the compiler
bootstrap discipline — **keep the previous stage**.

Two pieces:

1. **Run from a pinned copy.** The loop executes stage *N* from a location it
   never writes to. Tasks build stage *N+1* in a worker repo, as they already do.
2. **Self-upgrade canary.** After publishing a change under `autoloop/`, restart
   on the new version and run `doctor` + `smoke-browser`. Both green → the new
   stage becomes pinned. Either red → fall back to stage *N* automatically and
   file a blocker.

The invariant worth stating: *never destroy the last version of yourself that
passed a live smoke test.* Without it, publishing a change that breaks the
transport removes the tool you would use to fix the transport.

Do this **after** the loop has completed real application work. Full circularity
before that buys mainly a sophisticated way to break your own tooling.

---

## D1. Operator removal manifest — the `git rm` list (port-05, 2026-08-26)

port-05 disconnected this harness from language-app's **tooling**: `pytest.ini`
no longer collects `autoloop/tests`, `ruff.toml` excludes `autoloop`, and
`.github/workflows/tests.yml` has no harness job. The files are still on disk,
untouched and merely unreferenced. Deleting them is one operator action and
cannot be done by a loop round — the executor has no delete tool for
pre-existing paths, and a 76,225-line deletion would blow past the review
packet's 400,000-byte cap by two orders of magnitude.

**Precondition, non-negotiable:** do not run any of this until the extracted
repository has run `python -m autoloop doctor` **and a full round** against
language-app as an EXTERNAL target. Until then the harness lives here.

### Remove — autoloop-owned, unreferenced by this repository

| Path | Note |
|---|---|
| `autoloop/` | the package and its tests, the whole tree |
| `docs/AUTOLOOP.md` | the harness manual (8,101 lines) |
| `docs/AUTOLOOP_TODO.md` | this file — the harness's own TODO, including this manifest |
| `scripts/check_heartbeat.py` | judges the loop from `~/.autoloop/heartbeat.json`; **move first**, it is the harness's monitor |
| `scripts/install_health_monitor.sh` | installs the launchd agent that runs the file above; copies it out of `$REPO_DIR/scripts/`, so it moves with it |
| `scripts/autoloop_health_notify.sh` | runs `python3 -m autoloop health`; **move and fix its `AUTOLOOP_REPO` default**, which is `$HOME/Documents/GitHub/language-app` |
| `scripts/restart_autoloop_chrome.sh` | the retired-restart tombstone; see the ordering note below |

Three of those four `scripts/` files are a **move**, not a plain delete: the
health monitor is real operational tooling for the loop and should land in the
new repository before it is removed here. `check_heartbeat.py` deliberately
imports nothing from `autoloop` (macOS TCC blocks a launchd agent from reading
`~/Documents`, so it is copied to `~/.autoloop` and run from there) — that
independence is what makes the move trivial, and an already-installed copy at
`~/.autoloop/check_heartbeat.py` keeps running either way. Only re-installation
needs the source.

`scripts/restart_autoloop_chrome.sh` is the one with an ordering constraint. It
is kept on purpose as a **failing tombstone** for a live `.autoloop/config.toml`
whose `browser.restart_command` still names it: it restarts nothing, prints the
`restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]` line to
paste, and exits non-zero. Its replacement leaves in the same `git rm`, so
before removing it, check every live config has been migrated. After that its
advice is stale anyway and it should go with the rest.

### Keep — referenced here, or about this repository

| Path | Disposition |
|---|---|
| `.gitignore`'s `.autoloop/` entry | **keep.** That is the loop's STATE AND CONFIG directory, not the package. The loop still runs against this repository as an external target and still reads `.autoloop/config.toml` here. Dropping it would untrack a private conversation URL and make the checkout read as dirty to the loop. |
| `docs/audit_charters.toml` | **keep.** It describes THIS repository and is read by the harness from the root of whatever checkout it audits. Its absence is a *supported* state that silently falls back to built-in domains, so losing it is invisible — which is why the `docs` CI job now checks it. |
| `.gitattributes` | **keep.** Deliberately rule-free; its prose is the record of why `merge=union` was tried and removed. It names `autoloop/note_merge.py` as a cross-repository pointer, which stays accurate. |
| `ruff.toml`'s `extend-exclude = ["autoloop"]` | **keep now, delete in the removal commit.** A pattern matching nothing is not an error for ruff, so it stays correct through the removal and is simply dead afterwards. |
| `pytest.ini`, `requirements.txt`, `AGENTS.md`, `docs/SCHEMA.md`, `.github/workflows/dependency-audit.yml` | **keep unchanged.** None of them references `autoloop` at all. `requirements.txt` never declared the harness's dependencies (it is one `-r` line into the backend set); declaring them properly is the new repository's job. |
| `CLAUDE.md` §12's loop rules | **keep.** Change-note merge rules, the out-of-scope cleanup/revert authorities, `DELETE-FILE`, intake — these govern how a loop round must behave *in this repository*. They describe an external tool acting here, not a package shipped here. |
| `docs/SUMMARY.md`, `docs/TESTS.md`, `docs/COMMON_ERRORS.md`, `docs/SECURITY.md` autoloop sections | **keep until the removal commit, then excise the banner-marked blocks.** Each already carries a banner saying it describes a package this repository no longer owns. They were not deleted here because doing so alongside the tooling change would exceed the review packet cap. `docs/SECURITY.md` findings are never deleted (§14, regression history) — they travel with the code. |
| `docs/AUDIT_2026-07-30.md`, `-08-02`, `-08-03`, `-08-05`, `-08-22` | **keep.** Dated audit reports. Historical records of what was true on a date; not descriptions of current structure. |
| `docs/ROADMAP.md`'s 2026-08-25 measurement | **keep as a dated measurement.** It quotes `autoloop/tests` 3,672 as of that date; annotated in place rather than rewritten. |
| `.autoloop/config.toml` | **not in this repository** — gitignored, operator-owned, and therefore not on any `git rm` list. It still needs a human pass at removal time: `browser.restart_command` (see the tombstone above), `state_dir`, and the `[repo]` paths all point at a layout that is about to change. Nothing in a checkout can make that edit for you. |
| `.github/workflows/tests.yml`'s `docs`-job comment | **keep the job, touch up the comment in the removal commit.** The header names `autoloop/tests/test_docs_merge.py` and `autoloop/tests/test_audit_charters.py` to explain where those checks came from. Those paths stop existing at the `git rm`, so the comment should be reworded to past tense then — it is the one in-scope file that would otherwise describe a package this repository no longer contains. The checks themselves are about `docs/` and stay. |

### One path that BREAKS on removal — fix before, not after

`scripts/seed_validation_db.py:48` does
`from autoloop.validation_env import repo_declared_db_name`. It is the only
non-`autoloop/` file in this repository that imports the package, and it is
**language-app's** script (it seeds this repository's validation database for
the backend suite). `git rm -r autoloop/` gives it an `ImportError` at line 48
before it does anything. Neither validation command catches this: `ruff` does
not resolve imports, and nothing under `tests/` imports the script. Tracked as
**#46** in `docs/TODO.md`.
