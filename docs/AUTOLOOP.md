# AUTOLOOP — autonomous Fable ↔ ChatGPT engineering loop

Orchestration for a loop where an AI engineer (Fable) does the repository work
and ChatGPT — used **through a real browser session, never the OpenAI API** —
reviews each report and answers with the next machine-readable directive.

State of the system after Phase 3: the loop can run its **first autonomous
repository audit** — read-only Claude Code subagents per domain, reconciled
findings, one dated Markdown report, a proposed task graph — and have browser
ChatGPT review it. Implementation of repository tasks is **deliberately gated
off** (`policy.implement_enabled = false`); an `implement` reply is denied with
an explanation.

Code: `autoloop/`. Runtime state: `.autoloop/` (gitignored).

---

## 1. Architecture (after Phase 3)

| Component | File(s) | Owns |
|---|---|---|
| Orchestrator | `orchestrator.py` | Persisted state machine (ready → submitting → awaiting → executing), failure routing, budgets, review-integrity + manifest gates. |
| Lock | `lock.py` | Single-instance lock per state dir (see §3). |
| Change manifest | `manifest.py` | Task-owned change tracking + the commit gate (see §4). |
| Conversation | `conversation.py` (interface/registry), `browser/chatgpt.py` (`BrowserChatGPT`), `browser/playwright_session.py` (CDP, lazy), `browser/selectors.py` | One persistent reviewer conversation; duplicate/stale/streaming/login guards; provider-pluggable. |
| Contract | `contract.py` | Response contract **v3** + strict parser + `verify_review`. |
| Policy | `policy.py` | Deterministic gates: git whitelist (`add -A` and force pushes structurally impossible), task-reference checks, **phase gate**, budgets. |
| Tasks | `tasks.py` | Task registry/graph (derived ready/blocked, cycles rejected, atomic persistence). |
| Context | `context.py` | Per-request CONTEXT block: integrity stamp + previous decision/task, roadmap, git summary, changed files, validation summary. |
| Prompts | `prompts.py` | Strict template library (incl. `audit_kickoff`, `smoke_test`). |
| Git | `git_gateway.py` | Only git runner; exact-path staging; policy-validated per call. |
| Doctor | `doctor.py` | Non-destructive preflight (§6). |
| Audit executor | `audit/` | The production executor (§7): `findings` (agent contract), `agents` (claude-CLI runner), `reconcile`, `taskgen`, `markdown` (MD-only gate), `report`, `executor`. |
| State / transcript | `state.py`, `transcript.py` | Atomic crash-safe state; append-only JSONL audit log. |
| CLI | `cli.py` | `run status tasks doctor smoke-browser pause resume unlock reset`. |

---

## 2. Executor lifecycle

For every `audit` / `revise`(audit) / (future) `implement` directive:

1. **Manifest begin** — content snapshot of the dirty tree, persisted to
   `.autoloop/manifests/<task>-i<iteration>.json`, state saved (crash-safe:
   redispatch reuses the same manifest id).
2. **Execute** — the `TaskExecutor` runs (`AuditExecutor` in Phase 3;
   `NullExecutor` via `run --null-executor` or `executor.kind = "null"` for
   dry runs). Executors must tolerate redispatch after a crash.
3. **Manifest finish** — second snapshot; the diff (created/modified/deleted,
   by content hash, untracked-vs-tracked aware) is the task's owned change
   set.
4. **Report** — outcome (+ its `validation` summary) becomes the next review
   request; the manifest id is recorded in state for the commit gate.

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

## 4. Task-owned change manifests (no `git add -A`, ever)

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

Files changed *during* the task window by someone else are indistinguishable
from task work and therefore count as task-changed — they are still only
committable if ChatGPT explicitly approves those paths. Don't edit the tree
while an executor task is running.

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

## 6. Preflight: `doctor` and the live smoke test

```bash
python -m autoloop doctor         # never submits anything
python -m autoloop smoke-browser  # submits exactly ONE harmless request
```

`doctor` checks: config validity, state-dir writability, lock state, git
identity, branch policy (warns when pushes would be denied), CDP endpoint
reachability, Playwright presence, provider registration, conversation-URL
shape, and — only when CDP+Playwright are actually available — that the
conversation opens logged-in with the composer and message selectors
resolving. Exit 0/1.

`smoke-browser` runs the full normal machinery (request id, CONTEXT stamp,
parser, transcript, diagnostics-on-failure) against an **isolated** smoke
state (`.autoloop/smoke/`), sending one prompt that identifies itself as a
smoke test and demands a contract-v3 `stop`. PASS = the loop terminal state is
`stopped`. It can never invoke an executor (a guard executor raises if
dispatch were ever reached) and never touches the main session state.

Manual prerequisites for both live commands: the dedicated Chrome profile
running with `--remote-debugging-port=9222`, logged into chatgpt.com, and
`browser.conversation_url` pointing at your persistent conversation.

---

## 7. The audit executor

`audit`/`revise-of-audit` only — anything else returns an error outcome (and
is policy-denied before that). Pipeline:

1. Record git state (branch, HEAD, dirty count).
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
`audit` reply the executor runs (agents take minutes), the report goes back
for review, and the loop continues until `stop`/`ask_user`/budget.

Ongoing control: `status`, `tasks`, `pause`/`resume`, `run --answer "..."`,
`run --retry`, `reset --yes`, `unlock`. `run --null-executor` dry-runs the
loop without executing anything.

## 10. Recovery procedures

| Situation | Do |
|---|---|
| Crash / Ctrl+C anywhere | Just `run` again — every phase is persisted; requests are never double-submitted; executing re-verifies from saved state. |
| `stale lock` error | Inspect `python -m autoloop status`, then `python -m autoloop unlock` (refuses live locks). |
| Logged out mid-run (`needs_user`) | Log the profile back in, `run --retry`. |
| Browser dead / CDP unreachable | Relaunch the profile (§8), `run --retry` (or just `run` if not parked). |
| Repeated malformed replies / denials | Loop parks with the reason; talk to the conversation manually if needed, then `run --answer "..."`. |
| Crash mid-audit | `run` — the audit directive re-dispatches (a fresh agent fan-out; prior run's raw reports remain under `.autoloop/audit/`). |
| Crash mid-commit | `run` — commit is idempotent (clean approved paths + matching HEAD message → recognized as done). |

---

## 11. Known limitations

* Implementation tasks are gated off — Phase 3 ships audit-review only.
* The audit re-runs agents from scratch on `revise` (no incremental caching).
* Manifest attribution is time-based: edits made by a human *during* an
  executor run are indistinguishable from task work (§4).
* Subagent quality/latency depends on the local `claude` CLI; a failed agent
  is reported as a coverage gap, not retried automatically.
* `doctor`'s live check and `smoke-browser` require the real dedicated
  browser; hermetic tests mock them, so "implemented" ≠ "live verified" until
  smoke-browser has actually passed on your machine.
* One loop per state dir (enforced); multiple state dirs are possible but
  share nothing.
* Selector defaults will drift with ChatGPT's UI eventually
  (`browser/selectors.py` is the fix point).
