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
— no real code-editing executor is wired up yet, only the audit executor is
production-ready), but `audit`/`revise("audit")` are never gated and always
run.

`cli.py`'s `_build_orchestrator` — what the plain `run` command always calls —
unconditionally constructs the full produce-then-review collaborator set
(`WorkerRepoManager`, `TaskExecutionStore`, `IntentStore`, a provisioned
`Publisher`). There is no configuration that falls back to the retired path.

Code: `autoloop/`. Runtime state: `.autoloop/` (gitignored).

---

## 1. Architecture (Autoloop v1)

| Component | File(s) | Owns |
|---|---|---|
| Orchestrator | `orchestrator.py` | Persisted state machine (ready → submitting → awaiting → executing), failure routing, budgets, review-integrity gates, produce-then-review dispatch (`_dispatch_task_postcommit` — the only executor-dispatch path). |
| Lock | `lock.py` | Single-instance lock per state dir (see §3). |
| Change manifest (retired, kept for its own unit tests) | `manifest.py` | The old task-owned change-manifest commit gate (see §4) — **no production caller since 2026-07-30** (docs/SECURITY.md S21/S22). |
| Worktrees / task execution | `worktree.py` (`WorktreeManager`, unused in production — see §4c), `worktask.py` (`TaskExecution`, `CommitIntent`, `reconcile_after_crash`) | Per-task worktree/branch bookkeeping, and the crash-safe commit-intent/candidate-sha bookkeeping for produce-then-review (see §4b). |
| Review packet | `packet.py` | Renders the post-commit review packet from immutable git objects (see §4b). |
| Worker/publisher separation | `worker_env.py` (`worker_env`, `WorkerRepoManager`, `verify_worker_isolation`), `publisher.py` (`Publisher`, `provision_publisher_repo`, `reprovision_publisher`) | Autoloop M2 (see §4c): a scrubbed environment + no-remote repo for worker-side git access — this is what production `_build_orchestrator` actually uses, not `WorktreeManager` — and a dedicated, hooks-controlled repository that is the only path through which a candidate commit is published, with a provision-time URL snapshot (§4d). |
| Conversation | `conversation.py` (interface/registry), `browser/chatgpt.py` (`BrowserChatGPT`), `browser/playwright_session.py` (CDP, lazy), `browser/selectors.py` | One persistent reviewer conversation; duplicate/stale/streaming/login guards; provider-pluggable. |
| Contract | `contract.py` | Response contract **v3** + strict parser + `verify_review`. |
| Policy | `policy.py` | Deterministic gates: git whitelist (`add -A` and force pushes structurally impossible), task-reference checks, **phase gate**, budgets. |
| Tasks | `tasks.py` | Task registry/graph (derived ready/blocked, cycles rejected, atomic persistence). `seed_tasks.json` (git-tracked, alongside `tasks.py`) seeds a fresh registry with `rt-01` when `.autoloop/tasks.json` does not exist yet (§9b). `block`/`unblock` quarantine a task after a `task_fatal` park (§9c) via a dedicated `blocked` status/`TaskState.BLOCKED_BY_OPERATOR`, distinct from the dependency-derived `blocked` state. |
| Blockers | `blockers.py` | Persisted operator-facing `Blocker` records (one JSON file per blocker, `.autoloop/blockers/`) for every park, `task_fatal` or `loop_fatal` (§9c) — `python -m autoloop blockers`/`answer`. |
| Context | `context.py` | Per-request CONTEXT block: integrity stamp + previous decision/task, roadmap, git summary, changed files, validation summary. |
| Prompts | `prompts.py` | Strict template library (incl. `audit_kickoff`, `smoke_test`, `postcommit_review`). |
| Git | `git_gateway.py` | Only git runner; exact-path staging; policy-validated per call; `push_exact` is the only way to publish anything (no ambient `push()`); the legacy `commit()` method is **removed** (S21). |
| Doctor | `doctor.py` | Non-destructive preflight (§6), including worker isolation, controlled hooks directories, publisher configuration, and publisher URL drift. |
| Audit executor | `audit/` | The production executor (§7): `findings` (agent contract), `agents` (claude-CLI runner), `reconcile`, `taskgen`, `markdown` (MD-only gate), `report`, `executor`. Dispatched as a task-shaped unit of work (`Orchestrator._resolve_audit_task`), so it runs through §4b like any other task. |
| State / transcript | `state.py`, `transcript.py` | Atomic crash-safe state; append-only JSONL audit log. |
| CLI | `cli.py` | `run [--continuous] status tasks next-task blockers answer doctor smoke-browser pause resume unlock reset reprovision-publisher` (§8/§9). |

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
   `config.audit.validation_commands` re-run against the committed tree
   (pre-commit validation is not enough — a hook can change committed
   content after the executor last saw it).
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

**Wiring (2026-07-30 — production, unconditional).** `Orchestrator` takes
`publisher: Publisher | None = None` and `worker_repos` alongside
`worktrees`/`execution_store`/`intent_store`; `cli.py`'s `_build_orchestrator`
— what the plain `run` command always calls — constructs ALL of them: a
`WorkerRepoManager(config.workers_dir, config.worker_hooks_dir)`, a
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
only resolutions are: reconciliation finds it (→ awaiting), `run --retry`
(reconcile again), or `run --resubmit` — an explicit operator decision that
authorizes exactly one more send **of the same request id**, so a message that
did land is detected and not duplicated. A prior send attempt also blocks an
automatic resend if the machine re-enters `submitting`.

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

**Rotation is the last resort, not the reflex.** A second confirmed rejection, or
a `ConversationUnusableError`, may abandon the chat for a fresh one in the
configured project:

* `browser.project_url` must be set **explicitly**. It is never derived from
  `conversation_url` — a guessed project opens chats somewhere you did not
  choose. Unset means the loop parks instead of rotating.
* `policy.max_conversation_rotations` (default **1**) caps it per run. A second
  rotation in one run usually means the fault is not the chat.
* `ConversationUnusableError` is deliberately narrow: the page demonstrably
  reached the conversation URL, is not an auth page, and still has no composer
  (or shows an explicit unavailable marker). A page that never loaded, a dropped
  CDP connection and a logged-out profile stay ordinary failures on the normal
  budget — rotating for a network blip would spend the one rotation and leave
  none for the real thing. The two budgets are also kept separate: a rotation
  attempt does not also increment `consecutive_failures`.
* Never rotates for: generation already started, a slow answer, an ordinary
  response timeout, login expiry, rate limits, capacity, a malformed reply, or a
  policy denial.

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
`push`, `ask_user`, or `stop`. `implement` is rejected in this phase.

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

The test suite needs none of this. The audit executor additionally needs the
`claude` CLI on PATH (it is, in this environment).

## 9. First-run procedure

```bash
python -m autoloop doctor          # fix anything red
python -m autoloop smoke-browser   # optional but recommended: one live round-trip
python -m autoloop run --kickoff-audit
```

`--kickoff-audit` opens the session by offering ChatGPT the audit; on its
`audit` reply the executor runs (agents take minutes), the commit happens
automatically (§2/§4b), the review packet goes back for review, and the loop
continues until `stop`/`ask_user`/budget.

Ongoing control: `status`, `tasks`, `next-task`, `pause`/`resume`,
`run --answer "..."`, `run --retry`, `run --resubmit` (§5b), `reset --yes`,
`unlock`. `run --null-executor` dry-runs the loop without executing anything.

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
  distinct from the dependency-derived `blocked` `TaskState`, so it never
  auto-resolves) and clears the session, so the very next pass starts a
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
  a policy-denial or parse/iteration budget exhausted, ChatGPT's own
  `ask_user` (literally a question for a human), or anything not
  confidently classifiable. The whole loop stops, exactly as every park did
  before this split existed.
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

## 10. Recovery procedures

| Situation | Do |
|---|---|
| Crash / Ctrl+C anywhere | Just `run` (or `run --continuous`) again — every phase is persisted; requests are never double-submitted; executing re-verifies from saved state. |
| `stale lock` error | Inspect `python -m autoloop status`, then `python -m autoloop unlock` (refuses live locks). |
| Logged out mid-run (`needs_user`) | Log the profile back in, `run --retry`. |
| Browser dead / CDP unreachable | Relaunch the profile (§8), `run --retry` (or just `run` if not parked). |
| Repeated malformed replies / denials | Loop parks with the reason; talk to the conversation manually if needed, then `run --answer "..."`. |
| **Ambiguous submission** (`needs_user`, "submission … is AMBIGUOUS") | Open the conversation and look. If the request is there, `run --retry` (reconciles and continues). If it is genuinely absent, `run --resubmit` authorizes exactly one more send of the same id. Autoloop will not decide this for you — see §5b. |
| `send-not-ready` / `composer-not-synchronised` diagnostics | The editor never accepted the input, so **nothing was sent**: safe to `run --retry`. If it repeats, the composer selectors or the input method need attention (`browser/selectors.py`, `browser/chatgpt.py::_enter_prompt`). |
| Crash mid-audit | `run` — the audit directive re-dispatches (`_resolve_audit_task` resumes the SAME per-run worker repo/unit id when redispatching within the same iteration; prior run's raw reports remain under `.autoloop/audit/`). |
| Crash mid-commit | `run` — `commit_and_capture` is crash-recoverable via `CommitIntent`/`reconcile_after_crash` (§4b), never a bare "message matches HEAD" idempotency shortcut. |
| `doctor`'s `publisher_url_drift` check fails | The main checkout's `origin` changed since the publisher was last provisioned. Verify the NEW destination is actually correct, then `python -m autoloop reprovision-publisher --confirm` (§4d) — the only way the snapshot updates. Any push attempted before that is refused, not silently redirected. |
| `run --continuous` exited 0 with "continuous mode: exhausted" | Nothing autonomous is left to do — every ready task is done/blocked and the repository fingerprint hasn't changed. `python -m autoloop blockers` lists what's waiting; `answer <id> "..."` each one (unblocking any `task_fatal` task), then restart `run --continuous`. |
| `run --continuous` exited 2 | A `loop_fatal` park (§9c) — the environment or the operator is the problem, not one task. `python -m autoloop blockers` shows the question (also in `status`); resolve it (fix the environment, or `answer` it if that's enough), then `run --retry`/`--answer` (WITHOUT `--continuous`) before restarting `run --continuous`. |

---

## 11. Known limitations

* There is no repository-editing task executor yet — `implement`/`revise` of
  an ordinary registry task is policy-denied until `policy.implement_enabled
  = true`, and even then the only production `TaskExecutor`
  (`AuditExecutor`) reports an honest "unsupported decision" error rather
  than editing code. `audit`/`revise("audit")` are unaffected by this gate
  and always run.
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
  Everything the loop needs is in the newest turn, so this is currently
  harmless — `reconcile` checks the request that was just sent, and
  `await_response` needs the last message. But it means **a DOM read is not a
  full history read**: never infer "the conversation contains only X" from a
  message count, and if a future change needs older turns it must scroll them
  in rather than assume they are present.
* Selector defaults will drift with ChatGPT's UI eventually
  (`browser/selectors.py` is the fix point).
