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
here creates an adopted manifest in production yet: `ChangeManifest.adopt(...)`
is the API, and the `precommit-review` workflow that calls it lands separately.

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
`run --retry`, `run --resubmit` (§5b), `reset --yes`, `unlock`.
`run --null-executor` dry-runs the loop without executing anything.

## 10. Recovery procedures

| Situation | Do |
|---|---|
| Crash / Ctrl+C anywhere | Just `run` again — every phase is persisted; requests are never double-submitted; executing re-verifies from saved state. |
| `stale lock` error | Inspect `python -m autoloop status`, then `python -m autoloop unlock` (refuses live locks). |
| Logged out mid-run (`needs_user`) | Log the profile back in, `run --retry`. |
| Browser dead / CDP unreachable | Relaunch the profile (§8), `run --retry` (or just `run` if not parked). |
| Repeated malformed replies / denials | Loop parks with the reason; talk to the conversation manually if needed, then `run --answer "..."`. |
| **Ambiguous submission** (`needs_user`, "submission … is AMBIGUOUS") | Open the conversation and look. If the request is there, `run --retry` (reconciles and continues). If it is genuinely absent, `run --resubmit` authorizes exactly one more send of the same id. Autoloop will not decide this for you — see §5b. |
| `send-not-ready` / `composer-not-synchronised` diagnostics | The editor never accepted the input, so **nothing was sent**: safe to `run --retry`. If it repeats, the composer selectors or the input method need attention (`browser/selectors.py`, `browser/chatgpt.py::_enter_prompt`). |
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
