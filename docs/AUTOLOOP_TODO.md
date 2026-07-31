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

### A2. Operator-authored changesets still cannot be published by the loop
`review-changeset` binds correctly and ChatGPT approves, but dispatch refuses
with `legacy_git_path_retired`. Root cause **unknown** — an early diagnosis
(binding dropped in `_step_awaiting`) was disproved by reverting the supposed
fix and observing the binding survive anyway. Do not re-apply that fix without
new evidence.

Next diagnostic: run with `--max-steps 3` so the loop stops after the response
and before dispatch, then inspect `last_response.changeset` at that exact point.
That separates "never bound" from "bound but not routed" in one shot.

Until then, publication of infrastructure changes goes through `Publisher`
directly, which is how `b743567`, `5303926` and `da0c41e` all shipped.

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

### B4b. Post-commit validation ignores the task's declared validation
`_run_post_commit_validation` re-runs `config.audit.validation_commands`, not
`task.validation`. So a task like `rt-01`, which declares the backend suite
precisely because the configured default does not cover what it changes, has
its *reviewed commit* checked by the default only — the declared validation
runs once, in `ImplementExecutor`, against the pre-commit tree. That is a
weaker guarantee than §4b claims for the produce-then-review path, whose whole
point is that a commit hook can change committed content in ways pre-commit
validation never saw.

Found while wiring the validation-environment boundary (§4g), deliberately not
fixed there — out of that brief's scope. The fix is small (thread the task
through `_verify_committed` and prefer `task.validation`) but it changes what
gets refused, so it wants its own changeset and its own test.

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
