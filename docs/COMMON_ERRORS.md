# COMMON_ERRORS.md

Running log of errors actually hit while writing or running code in this repo —
with the symptom first, so this file is greppable by the error text you're
staring at.

**This is not a style guide.** Every entry below is something that really
happened and really cost time. If an entry stops being true (tool upgraded,
config changed), mark it resolved with a date rather than deleting it — the
history is what stops the same trap being re-entered.

**Add to this file whenever you hit an error**, in the same change that fixes
it. Entry template at the bottom.

---

## 0. Writing a validation command (read before quoting any result)

Two rules, both learned the same way: a validation command that is *shaped*
wrong doesn't fail — it succeeds and reports something false. Everything in §1
below is an instance of one of these.

**Start from an explicit repo path. Never inherit the shell's cwd.**
The working directory carries across tool calls, including parallel ones in the
same message. `lexy-app/backend/` and the repo root *both* contain a `tests/`
directory, so `pytest tests/` from the wrong place runs the wrong suite and
prints a completely plausible number.

```bash
# Good — location is stated, and pwd is echoed so the transcript proves it
cd /Users/emir/Documents/GitHub/language-app && pwd && pytest tests/

# Bad — depends on wherever the last command left the shell
pytest tests/
```

**Avoid pipes in validation commands, or set `pipefail`.**
`cmd | tail` reports `tail`'s exit status, so a failing build reads as a pass.

```bash
# Best — no pipe; status is the command's own
npm run build > /tmp/build.log 2>&1; echo "EXIT: $?"; tail -12 /tmp/build.log

# Acceptable — pipefail makes the pipeline adopt the first failure
set -o pipefail; npm run build 2>&1 | tail -12; echo "EXIT: $?"

# Also fine — read the producer's status explicitly
npm run build 2>&1 | tail -12; echo "EXIT: ${PIPESTATUS[0]}"

# Bad — $? is tail's, and tail almost always succeeds
npm run build 2>&1 | tail -12; echo "EXIT: $?"
```

`set -o pipefail` is not the default in the shells used here, and it does not
persist between tool calls — put it in the same command line as the pipeline it
guards.

**Corollary:** before quoting a number in a report, check it came from a run whose
location and exit status you can both see. A pass you cannot locate is not a pass.

---

## 1. Environment / tooling

### `command not found: timeout`
**Symptom:** `(eval):1: command not found: timeout`
**Cause:** macOS ships no GNU `timeout`. (`gtimeout` exists only with coreutils
installed.)
**Fix:** Use the Bash tool's own `timeout` parameter (milliseconds) instead of
wrapping the command.

### Working directory persists between Bash calls — silently wrong results
**Symptom:** A command run "from the repo root" reports the wrong thing. Seen
twice in one session:
- `python -m pytest tests/` reported **745 passed** (the backend suite) instead
  of the root pipeline suite's **671** — because a previous call had `cd`'d into
  `lexy-app/backend`, where a `tests/` directory also exists. (Counts as-measured
  that day; both suites have since moved — 847 and 221 as of 2026-07-27. The trap
  is unchanged, and it recurred three times during the LLM-provider work.)
- `python -m compileall subtitle-scraper scripts …` printed
  `Can't list 'subtitle-scraper'` for every path.

**Why it's dangerous:** the first case *succeeded* and produced a plausible
number. Nothing failed; the report was just wrong.

**Cause:** the working directory carries across Bash tool calls, including into
calls issued in parallel in the same message.

**Fix:** put an explicit `cd /absolute/path &&` at the front of any command whose
result depends on location — especially when several suites share a directory
name. Add `pwd` to the command when the output will be quoted in a report.

### An autoloop CLI run from a sibling worktree reports on an EMPTY state dir
**Symptom:** `python -m autoloop merge-window`, run from a second worktree
against the live config, printed `merge window OPEN` while the same command in
the primary checkout listed four blocking candidates. Nothing had changed
between the two runs.
**Cause:** `state_dir` is **relative** in the shipped config (`.autoloop`), so it
resolves against the caller's cwd — not against the config file's location and
not against the repo. From the worktree it pointed at a directory that does not
exist, every `glob("executions/*.json")` came back empty, and "found no records"
was indistinguishable from "there are no records". Same family as the cwd trap
above, and as AUTOLOOP_TODO B8.
**Fix:** pass an absolute `state_dir`, or run the CLI with the checkout as cwd.
To exercise a *worktree's* code against the *live* state, set cwd to the live
checkout and import the worktree explicitly — cwd would otherwise win on
`sys.path`:

```bash
cd /path/to/live-checkout
PYTHONSAFEPATH=1 PYTHONPATH=/path/to/worktree python3 -m autoloop.cli merge-window
# confirm which copy actually loaded:
PYTHONSAFEPATH=1 PYTHONPATH=/path/to/worktree python3 -c "import autoloop; print(autoloop.__file__)"
```

`merge-window` itself now refuses to answer when its `state_dir` is missing
(2026-08-04) — an unreadable directory is not evidence of safety. Any other
command reading `state_dir` still has this shape.

### Autoloop's Chrome restart reports success while restarting nothing
**Symptom:** the loop cycles forever on a ~3-minute period. Every cycle logs
`browser_restarted` with `returncode 0` and `"autoloop chrome up on 9222"`,
immediately followed by `browser_error: cannot connect to Chrome DevTools at
http://127.0.0.1:9222`. A hand-run `curl http://127.0.0.1:9222/json/version`
returns 200 the whole time, so the browser looks healthy. The pid the script
claims to have stopped is different on every cycle.
**Cause:** two faults stacked, and the second hid the first.

The underlying fault was a wedged browser: `BrowserType.connect_over_cdp`
reached `<ws connected>` and then hung until its 180000ms timeout — which is
exactly the 3-minute cycle. HTTP `/json/version` still answered, so no probe
built on it could see the problem. It ignored `SIGTERM`.

The fault that made it unrecoverable was `restart_autoloop_chrome.sh` selecting
its target with `ps ... | head -1`. `ps` lists ascending by pid, every Chrome
helper inherits `--user-data-dir`, so `head -1` reliably picked a **renderer**,
not the browser. Chrome respawns a killed renderer instantly; the readiness
`curl` then hit the untouched main process and returned 200. Result:
`returncode 0`, a plausible "stopping autoloop chrome (pid NNNN)" line, and a
browser that was never restarted. **A recovery command that lies is worse than
one that fails** — the loop retried against the same wedged browser for hours
and reported healthy recovery each time.
**Fix (2026-08-14, in the shell script):** `main_pids()` required all three of:
the command *is* the Chrome browser binary, no `--type=` (that excludes
helpers), and an exact `--user-data-dir` match (so `.autoloop-chrome` does not
select `.autoloop-chrome-backup`). It also proved port 9222 was **free** after
killing and before launching — otherwise a stale holder makes the readiness
probe pass against the impostor — escalated to `SIGKILL` when `SIGTERM` was
ignored, and required `webSocketDebuggerUrl` in the probe rather than any 200.

One trap while writing that filter, and it is the same bug in a new costume:
`awk -v prof="--user-data-dir=$PROFILE"` carries the profile path in its **own**
argv, so a plain content match selects the matcher process itself. The old
`grep -v grep` was covering this. Requiring the command to start with the Chrome
binary path is what actually closed it.

**Fix (2026-08-16, brw-06/07/08): the shell script is RETIRED.** The restart
path is now `python3 -m autoloop.browser.chrome_restart`
(`autoloop/browser/chrome_restart.py`), and `config.example.toml` ships
`restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]`. Two
reasons, both from this entry:

* **It was untestable.** The post-commit validation runner allows only
  ruff/pytest/python/npm/npx/tsc, so no test in this repo could ever have
  exercised a `.sh` — which is how a recovery command that lies shipped at all.
  The module is pinned by `autoloop/tests/test_chrome_restart.py` against a
  fake machine (nothing there lists, signals or launches a real process).
* **One pid is not enough.** The shell version stopped a single main pid; when
  two instances were running on the profile, the survivor kept the debug port
  and the "restart" landed the loop back on the wedged browser. The module
  stops **every** process carrying that `--user-data-dir`, then polls until
  nothing holds the port (a kill is not proof it was freed), then launches, then
  requires `webSocketDebuggerUrl` from `/json/version` before reporting success.
  The safety bound is unchanged and load-bearing: match the profile path
  exactly, never the binary name — the operator's everyday Chrome runs from the
  same binary under a different `--user-data-dir`.

**The `.autoloop/config.toml` that decides this is NOT in the repository**, so
changing `config.example.toml` changed nothing for a running deployment: until
the operator hand-edits `[browser].restart_command`, the loop still launches the
script. That is why the script is still on disk, as a **failing tombstone** — it
restarts nothing, prints the replacement line on stderr and exits 1, which both
callers surface as `restart FAILED: …`. Non-zero deliberately: they surface
`result.stderr` only on a non-zero exit, and this is the file that taught us
what a zero exit costs.

`load_config` deliberately does **not** refuse a config that still names it.
Refusing would fail `status`, `doctor`, `run` and the recovery commands the
moment the change merged, over a setting only a restart reads — taking away the
tooling the operator would use to recover. So an unmigrated deployment keeps
working everywhere except the restart, and the restart fails with the fix
attached rather than with bash's exit 127 (`No such file or directory`), which
is what deleting the file outright would have produced. `git rm` and any
load-time refusal both belong to a later cleanup, once live configs have been
migrated.

```bash
# Verify a real restart: the main pid must CHANGE and helpers must be ignored.
BEFORE=$(ps -eo pid,command | grep -- "--user-data-dir=$HOME/.autoloop-chrome" \
    | grep -v grep | grep -v -- '--type=' | awk '{print $1}')
python3 -m autoloop.browser.chrome_restart      # run from the checkout
AFTER=$(ps -eo pid,command | grep -- "--user-data-dir=$HOME/.autoloop-chrome" \
    | grep -v grep | grep -v -- '--type=' | awk '{print $1}')
[ "$BEFORE" != "$AFTER" ] && echo "PASS: browser actually restarted"
```

### Autoloop session ends `failed` with zero blockers, transcript full of `browser_restart_skipped`
**Symptom:** the loop stops with `phase: failed`, `python -m autoloop blockers`
lists nothing, and the only clue is in the transcript: a run of `browser_error`
entries, each immediately followed by
`browser_restart_skipped {"reason": "within cooldown"}`. Chrome was never
restarted, yet the run died of "more than 3 consecutive browser failures".
**Cause (observed 2026-08-04, fixed 2026-08-14):** the two browser guards
cancelled each other. `browser.restart_cooldown_seconds` (120s) refused each
restart, while every refused failure still spent
`policy.max_consecutive_failures` (3). The budget ran out before the cooldown
did, so the one action that would have fixed the hang was never attempted and
the terminal state recorded no reason for it — `_handle_browser_failure` writes
`stop_reason` and `phase=failed` directly, and only a `needs_user` park creates
a `Blocker`.
**Fix (already in the code — this entry is for reading an OLD transcript):** a
failure whose restart was skipped for the cooldown no longer touches
`consecutive_failures`; it is counted against `policy.max_browser_restart_skips`
instead, and exhausting THAT parks `needs_user` with
`code="browser_restart_cooldown_blocked"`, naming the cooldown. If you see the
old shape, the session predates the fix; if you see the new park, restart Chrome
by hand and `run --retry` (see `docs/AUTOLOOP.md` §5c, §10).
**Related trap:** a restart that reports success while restarting nothing looks
almost identical in the transcript — `browser_restarted returncode 0` followed
immediately by the same `browser_error`. That is the entry above, and it is a
different bug: there the restart ran and lied, here it never ran at all. Read
which of the two events sits between the failures.

### The autoloop process is simply GONE — `phase: submitting`, `stop_reason: null`, no blocker
**Symptom:** nothing in the loop's own records says anything happened. The state
file shows a mid-flight phase (`submitting`), `stop_reason: null`,
`consecutive_failures: 0`, `python -m autoloop blockers` lists nothing, and the
notify watcher reports a stop with no reason to report. The process is not
running. From the outside this is indistinguishable from a clean exit or an
operator `kill`; the only evidence is on the terminal the loop was started
from — an unhandled traceback ending in:

```
Exception: BrowserType.connect_over_cdp: Connection closed while reading from the driver
```

**Cause (observed 2026-08-15, fixed the same day):** the exception type. Chrome
was RUNNING but no longer serving CDP on 9222 — the wedged shape of the entry
two above — and Playwright's `rewrite_error` turns a driver-channel failure into
a **plain `Exception`**, not a `playwright.sync_api.Error` and not a subclass of
anything nameable. `PlaywrightSession.connect` caught only the narrow type, so
the fault sailed past `Orchestrator.run`'s `except BrowserError` routing —
restart, failure budget, park — and out of the process. Every mechanism the loop
has for failing safely was bypassed by a type mismatch.
**Fix (already in the code):** every call into Playwright is guarded
POSITIONALLY — one `except Exception` around `connect`/`_call`, re-raising
`AutoloopError` untouched and everything else as `SessionLostError`. Catching by
type cannot work here, because the fault has no type to catch. The message keeps
the original exception's type name (`kind=` in the transcript now always reads
`SessionLostError`) and appends what the machine looked like at fault time:

```
cannot connect to Chrome DevTools at http://127.0.0.1:9222 — Chrome IS running
but this endpoint is unusable — restart the dedicated profile (python3 -m
autoloop.browser.chrome_restart); a wedged browser can keep answering HTTP and
can ignore SIGTERM [endpoint=http://127.0.0.1:9222 port_open=yes
cdp_answering=no chrome_on_profile=1 (pid 4711) chrome_on_port=1
profile=~/.autoloop-chrome] (original fault: Exception: BrowserType.connect_over_cdp: ...)
```

The action leads and the evidence follows on purpose: `autoloop start` prints
`blocker.question[:160]`, so a message ordered evidence-first would show four
key=value pairs in the compact view and cut off the sentence saying what to do.

`chrome_on_*` counts BROWSER processes only (`--type=` helpers excluded), and it
is measured inside the failing connect because `_handle_browser_failure` drops
the client and restarts Chrome before anything is written — a later probe would
describe the repair, not the fault.
**Reading it:** `chrome_on_profile>0` with `cdp_answering=no` is a wedged
browser (it may ignore SIGTERM — `chrome_restart` escalates).
A zero `chrome_on_profile` means nothing is running: start the dedicated profile.
`cdp_answering=yes` alongside a failed connect is the 2026-08-14 shape, where
HTTP answers and CDP itself is wedged.
**All four fields `unknown`** is the fifth reading, and it is not a broken probe:
every measurement above is LOCAL (the probes dial 127.0.0.1, `ps` lists this
machine), so an endpoint this machine cannot speak for gets none of them rather
than a confident diagnosis of the wrong host. Two shapes reach it — a
non-loopback `cdp_url` (`http://gpu-box:9222`: check Chrome on *that* host and
the route to it; a local Chrome on the dedicated profile is not evidence about
it, and restarting it would fix nothing) and a `cdp_url` with no usable port
(fix the url — the default is `http://127.0.0.1:9222`).
**If you are reading an OLD transcript:** a crash predating this fix leaves NO
`browser_error` entry at all — that is how to tell it from every other browser
failure, all of which log one.

### `ImportError: cannot import name 'markcoroutinefunction'` from a partially initialized `inspect`
**Symptom:** A throwaway script that only does `import asyncio, asyncpg` dies with
a traceback that ends inside `asyncpg/compat.py`:
```
ImportError: cannot import name 'markcoroutinefunction' from partially
initialized module 'inspect' (most likely due to a circular import)
```
and the traceback names *your own script* as the `inspect` module.

**Cause:** the script was named `inspect.py`. Its directory is first on
`sys.path`, so it shadows the stdlib `inspect`; `asyncpg` imports `inspect`, gets
the script back, and the "circular import" is really a name collision.

**Why it's confusing:** the error blames `asyncpg`, which is innocent, and the
code in the file is irrelevant — an empty `inspect.py` breaks the same way.

**Fix:** never name a scratch file after a stdlib module. `inspect.py`,
`types.py`, `token.py`, `select.py`, `copy.py` and `logging.py` are the usual
offenders. Rename the file (deleting the stale `__pycache__` too if one exists).

### Connecting to the DB from a scratch script: `psql` prompts for a password
**Symptom:** `psql -d german_vocabulary -c '\d word_lists'` fails with
`fe_sendauth: no password supplied`, so the live schema looks unreachable —
even though `pytest` talks to the same database happily.
**Cause:** the credentials live in the repo-root `.env`, which the backend loads
explicitly (`database.py:6`); `psql` and a bare `load_dotenv()` from a scratchpad
cwd both miss it.
**Fix:** load that exact path from the script — `load_dotenv("<repo root>/.env")`
— then read `DB_*` via `os.getenv`. This keeps the "never read `.env`" rule
(§12): the process loads it, you never open or print it.

### Piping to `tail` hides the real exit code
**Symptom:** `npm run build 2>&1 | tail -12; echo "EXIT: $?"` printed `EXIT: 0`
while the build had actually failed with two TypeScript errors.
**Cause:** `$?` is the exit status of `tail`, not of the piped command.
**Fix:** `${PIPESTATUS[0]}`, or redirect to a file and check the status directly:
```bash
npm run build > /tmp/build.log 2>&1; echo "EXIT: $?"; tail -12 /tmp/build.log
```

### `${PIPESTATUS[0]}` prints empty — the shell here is zsh, not bash
**Symptom:** the documented fix for the piped-exit-code trap above,
`cmd | tail -3; echo "EXIT: ${PIPESTATUS[0]}"`, prints `EXIT:` with no number.
**Cause:** zsh's array is lowercase **and 1-indexed** — `${pipestatus[1]}` is
the first command. `PIPESTATUS[0]` is a bash-ism and expands to nothing.
**Fix:** don't pipe when you need the status. Redirect and check directly —
this works in both shells and is what the entry above already recommends:
```bash
ruff check . > /tmp/r.log 2>&1; echo "EXIT: $?"; cat /tmp/r.log
```

### Scripted multi-step refactor: later regex runs on text an earlier one rewrote
**Symptom:** a Python script doing a sequence of `re.sub` / `str.replace` over a
source file produces code that looks right in the diff but is subtly broken.
Two real instances in one session, both in the same script:
- `.replace("tool_block.input", "response")` ran *before*
  `re.sub(r'result = tool_block\.input\n', "")`, so the second pattern never
  matched and a dangling `result = response` survived (caught later by ruff
  F841 — but only because the variable happened to go unused).
- A `.get(` rewrite dropped a closing quote, turning
  `response.get("corrections", [])` into `response.get("corrections, [])` —
  a syntax error 500 lines from anything that looked related.

**Why it's dangerous:** each step is individually plausible, the file still
*looks* like valid Python in a diff, and the failure surfaces far from the edit.
Recovering incrementally is worse than starting over — you end up patching
damage rather than doing the conversion.

**Fix:** `git checkout --` the file and redo the whole conversion in **one**
script that (a) uses brace/paren matching instead of regex for nested
structures, and (b) **asserts its own postconditions before writing**:
```python
assert n_converted == n_expected, f"converted {n_converted}/{n_expected}"
assert "_client" not in s, "old symbol survived"
for block in each_converted_call(s):
    for req in ("system=", "messages=", "schema=", "max_tokens="):
        assert req in block
compile(s, path, "exec")      # syntax gate
p.write_text(s)               # write LAST
```
A verifier that runs before the write turns a silent corruption into a loud
failure. Note the verifier itself needs the same care: a non-greedy
`re.finditer(r'call\((.*?)\n\s*\)')` stopped inside a multi-line argument and
reported a false failure on correctly-converted code.

### Edit tool: "Found N matches of the string to replace"
**Symptom:** An edit fails because the target line appears more than once (e.g.
`result = onboarding.seed_from_level("u1", LevelTier.A1, store)` appears 4× in
one test file).
**Fix:** include a neighbouring unique line (the function signature above it, the
line after it) in `old_string`. Don't reach for `replace_all` unless every
occurrence genuinely should change — the linter flagged only one of them.

---

### A word exists in `word_table` but a vocabulary list reports it `unresolved`
**Symptom:** `Öl`, `Übung` and `Änderung` are all present in `word_table`
(`SELECT * FROM word_table WHERE word = 'Öl'` returns a row), yet
`POST /api/v1/word-lists` reports them `unresolved` — even when the pasted
spelling matches the stored row **byte for byte**. Plain ASCII words in the
same list resolve fine. 50 German `word_table` rows and 18 `phrase_table`
canonicals were affected — note it is *any* uppercase non-ASCII letter, not
just a leading one, so multi-token `die Änderung` broke too.

**Cause:** this database is `datcollate=C datctype=C`, so Postgres folds
**ASCII only**:

```sql
SELECT lower('Öl');            -- 'Öl'   (unchanged!)
SELECT 'Öl' ILIKE 'öl';        -- false
SELECT lower('Ab');            -- 'ab'   (ASCII works, which is why this hides)
```

Python's `str.lower()` folds the whole Unicode range. The resolver sent
Python-lowered keys (`'öl'`) into `WHERE lower(w.word) = ANY($1)`, where the
SQL side produced `'Öl'`. The two keys never met.

**Why it's confusing:** the query looks obviously correct, the row is visibly
there, and every ASCII test passes. A fixture word without an umlaut cannot
detect it.

**Fix that does NOT work:** making both sides SQL — `lower(w.word) =
lower($1)`. Under C locale *neither* side folds, so `'Öl'` vs `'öl'` stay
unequal. Verify before believing any fix: `SELECT lower('Öl') = lower('öl');`
should return `true`, and here it returns `false`.

**Fix:** fold in Python and let SQL only fetch rows —
`services/text_norm.normalize_key` (NFC + strip + `lower()`), applied to both
the input and the fetched surface. See `word_list_service._resolve_surfaces`.

**Two traps inside the fix:**
- Use `lower()`, **not** `casefold()`. Casefold maps `ß` → `ss`, which merges
  `schließen`/`schliessen`, `heißt`/`heisst`, `Großteil`/`Grossteil` — 7 real
  pairs of *distinct* German rows — into one key, turning words that resolve
  cleanly today into `ambiguous`.
- A bounded "send a few case variants as exact matches" query is **not**
  sufficient. No whole-string case transform turns a typed `Die Änderung` into
  a stored `die Änderung`, so multi-token phrases stay broken. `lower(col
  COLLATE "und-x-icu")` is correct and bounded if you ever need it (it agreed
  with Python `lower()` on all 12,138 German word+phrase rows and leaves `ß` alone), at the
  cost of requiring an ICU-enabled Postgres.

**Same root cause, still open (audited 2026-07-27, not fixed):**
- `reading_service.find_catalog_item:316` — `WHERE LOWER(word) = $1` fed
  `canonical.lower()`. A reading selection of an umlaut word never binds to the
  catalog, so it never propagates to the main SRS (CLAUDE.md §8b).
- `reading_service.get_page_word_statuses:73` — `WHERE LOWER(w.word) = ANY($2)`
  fed Python-lowered tokens. Umlaut words render without their status colour.
- `word_service.lookup_word_by_text:63` and `learn_word_anyway:175` — `ILIKE`.
- `search_service` — `ILIKE` (search) and `lower(word) LIKE lower($2) || '%'`
  (suggest/autocomplete).

**Cleared by the same audit:** `subtitle-scraper/pipeline.py` never asks
Postgres to fold — it matches `(word, pos)` exactly and inserts with
`ON CONFLICT DO NOTHING`, so it cannot hit this bug. It *can* create case
variants as separate rows, which is the (pre-existing, unrelated) reason 417
German and 1,944 Spanish surfaces report `ambiguous` in vocabulary lists.

### Counting rows after `INSERT ... ON CONFLICT DO NOTHING` over-reports
**Symptom:** `scripts/backfill_word_catalog.py --apply` logged
`APPLY: inserted 17 word_table row(s)` on the second *and* third run, while
`SELECT count(*) FROM word_table` did not move at all.
**Cause:** the count was a separate `SELECT count(*) ... WHERE word =
ANY($1::text[])` run after the insert. That counts rows matching the candidate
list — including every row that was **already there**.
**Fix:** get the number from the statement itself, with `RETURNING`, and take
`len(rows)`. `ON CONFLICT DO NOTHING` suppresses the `RETURNING` row for a
conflict, so the count is exactly what this call wrote.
**Related:** here the *proximate* cause of the wrong number was the collation
bug above — those 17 surfaces were umlaut-initial and looked missing every
time. Fixing only the count would have left the pointless re-inserts.

## 2. Test suite

### An `inspect.getsource` test fails in a file you never touched
**Symptom:** an advisory validation run reports failures in an unrelated test
file, and the assertion shows the WRONG function's body — e.g.
`test_task_inbox.py::test_apply_requests_is_the_single_merge_used_by_both_callers`
failing with `assert 'apply_requests(' in '    def _rebase_execution_if_stale(…'`,
plus a `StopIteration` from its sibling. The same tests passed on the previous
run and pass again afterwards. Measured 2026-08-24 (recov-01).
**Cause:** not a regression, and not flakiness in the usual sense. Those tests
read `inspect.getsource(orchestrator.Orchestrator._drain_task_inbox)`, which
seeks by the code object's recorded `co_firstlineno` and then reads the file
**as it is on disk right now**. Editing `orchestrator.py` while the run is in
flight shifts every line below the edit, so the seek lands in whichever method
now occupies those lines. The edit does not even have to be near the test's
subject — one added block near the top of a 8,500-line module is enough.
**Fix:** do not edit a file while its validation run is executing. The advisory
channel says the result describes the tree AS IT STOOD WHEN YOU ASKED; that is
true of what it *reports on*, not of what a long-running `inspect.getsource`
re-reads mid-run. Wait for `RESULT #n`, then edit. To tell this artifact from a
real break, check the named subject directly: if `_drain_task_inbox` still
contains `apply_requests(` with a three-name unpack, the assertion holds and the
run's view of the file was stale.

### Backend suite: hundreds of `asyncpg` errors, or one unreproducible failure
**Symptom:** `python3 -m pytest -n auto` in `lexy-app/backend` reports something
like `1260 errors` with tracebacks bottoming out in
`pool = await asyncpg.create_pool(...)`, or a single odd failure such as
`test_repeated_calls_drain_to_zero`. Re-running alone passes clean at the
expected `1258 passed, 2 skipped`.

**Cause:** two backend suites running against the SAME Postgres database at the
same time. Easy to do by accident — start one in the background to save time,
then start another in the foreground while it is still going. The per-worker
`PYTEST_XDIST_WORKER` tagging (CLAUDE.md §11) isolates workers *within* one run;
it does nothing between two independent runs, which trample each other's rows
and exhaust the pool.

**Why it's dangerous:** the output looks like a catastrophic regression in code
that was never touched, which invites debugging the wrong thing entirely. The
single-failure variant is worse — it reads as a real flake worth chasing.

**Fix:** run the backend suite once at a time. Before believing any backend
failure, confirm nothing else is running against the DB and re-run it alone:
```bash
cd lexy-app/backend && python3 -m pytest -n auto -q   # expect 1258 passed, 2 skipped
```
If a background run is already in flight, wait for its notification rather than
starting a second one.

### `ModuleNotFoundError: No module named 'services'` — only `test_document_package.py` fails collection
**Symptom:** Backend suite reports `1151 passed, 2 skipped, 1 error` — the
error is an ImportError collecting `tests/test_document_package.py`. The A2
session had verified `1258 passed, 2 skipped` on the same tree.
**Cause (verified 2026-07-29):** invocation form, not branch state or missing
files. All A2 files exist (untracked on `main`). `test_document_package.py` is
the only backend test importing bare `from services import …`; every other
test uses `from backend.services …`, resolvable because the package chain
(`backend/__init__.py` + `backend/tests/__init__.py`) puts `lexy-app/` on
`sys.path`. Bare `services` additionally needs `lexy-app/backend` on
`sys.path` — which only happens when **cwd is on sys.path**, i.e. with
`python -m pytest` (Python inserts cwd) but NOT with the `pytest` console
script. 1151 + 107 (that file) = 1258 exactly.
**Fix:** run the backend suite as
`cd lexy-app/backend && python3 -m pytest -n auto` (the reconciled canonical
command; CLAUDE.md §11 and docs/TESTS.md updated). The A2 test file was left
untouched — aligning its imports with the `backend.services` convention is a
proposed follow-up task, not something to do as a drive-by.

### `ValueError: Seed must be between 0 and 2**32 - 1` — every test errors
**Symptom:** Backend suite reports `211 passed, 1036 errors`; root suite
`1 passed, 1339 errors`. Every error is at test **setup and teardown**, so no
assertion ever runs.
**Cause:** `pytest-randomly` (arrived transitively, not a declared dependency)
reseeds per test with `session_seed + crc32(nodeid)`. It masks that value for its
own numpy call but passes the **unmasked** sum to `pytest_randomly.random_seeder`
entry points. spaCy's thinc registers `thinc.api:fix_random_seed` there, which
calls `numpy.random.seed()` with no mask — and numpy rejects seeds ≥ 2**32.
**Fix:** already applied — `addopts = -p no:randomly` in both `pytest.ini` and
`lexy-app/pytest.ini`. **Do not remove those lines.** Uninstalling the package
locally is not a fix; it just moves the failure to the next machine.

### Full-suite flakes under `pytest -n auto` from shared-catalog picks
**Symptom:** Intermittent failures in `test_srs_review` / `test_suggest` /
`test_srs_backfill` / `test_account_deletion` / `test_grammar_rules_srs`, green
serially and in isolation.
**Cause:** a test grabs a shared row with `SELECT … FROM <catalog> LIMIT 1` and no
`ORDER BY`; a parallel worker's teardown deletes that exact row mid-test.
**Fix:** own the row (`make_word` / `insert_owned_word` fixtures) or pick
deterministically (`ORDER BY <pk> LIMIT 1`). Full detail and the audit hint for
the next round are in `docs/TESTS.md`.

### A "happens once per process" test fails with an EMPTY stream, not a doubled one
**Symptom:** `test_the_cli_prints_the_migration_notice_on_stderr_once` fails on
`assert captured.err.count(...) == 1` with an actual count of **0**. The stream
is empty, not duplicated. Passes when run alone (`-k once`), fails in the full
suite. Nothing about the assertion looks order-dependent.
**Cause:** the behaviour under test is suppression by **process-global** state —
`cli._EMITTED_MIGRATION_NOTICES`, which makes a retired-key notice print at most
once per process (`run --continuous` reloads its config every round, and a
notice repeated each round is one an operator scrolls past). Any earlier test in
the same process that loaded a legacy config through the CLI already consumed
the one emission. The "once" test then correctly observes nothing. Read
literally the failure says "it printed zero times", which points at emission
being broken; the actual fault is that it printed already, somewhere else.
**Fix:** test a per-process contract in a **process of its own** — run the
scenario via `subprocess.run([sys.executable, "-c", program, …])` with
`PYTHONPATH` at the repo root, and assert on `proc.stderr`. That makes "this
process has not printed yet" true by construction rather than by test ordering.
See the test named above.
**Do NOT** fix it by weakening production semantics (dropping the ledger, or
re-emitting per call) so the in-process assertion passes — that discards the
behaviour the test exists to pin. If you need an in-process test for the
*routing* or the *content*, reset only the ledger and only for that test
(`monkeypatch.setattr(cli, "_EMITTED_MIGRATION_NOTICES", set())`), which
restores itself. Better still, keep the producer pure: `config.load_config`
returns notices as data (`AutoloopConfig.migration_notices`) and writes to no
stream, so content assertions cannot be contaminated by ordering at all — only
the genuinely global "once" contract needs the subprocess.

### A `BacklogSweeper` test asserts `nothing_to_do` / `held` and gets `disabled`
**Symptom:** a test builds an `AutoloopConfig` by hand, calls
`merge_sweep.BacklogSweeper(...).sweep()` and asserts on `result.outcome`. It
gets `disabled`, and nothing in the message points at the fixture.
**Cause:** `PolicyConfig()` defaults `auto_merge_enabled` to **False** —
`test_auto_merge.py` pins that default deliberately — and the flag check is the
FIRST thing `sweep()` does, ahead of the checkout probe and the backlog walk. A
hand-built config that does not set it short-circuits before the code under test
runs at all.
**Fix:** `PolicyConfig(auto_merge_enabled=True)` in the fixture. See
`test_shipped_elsewhere.py`'s `config` fixture and `test_merge_sweep.py`'s
`build`, which passes it explicitly for the same reason.
**Watch for the quiet version.** An outcome assertion fails loudly; an assertion
like `result.unresolved == []` or `result.pending == []` PASSES on a `disabled`
sweep, vacuously, because enumeration never ran. A sweep test that only checks
what the sweep did *not* find proves nothing unless it also pins the outcome.
**Do NOT** change the default to make a test pass — it is off deliberately (it
gates a loop that would otherwise move the base branch head unasked), and the
short-circuit is correct behaviour, not the bug.

### A test asserts `health.check(...).needs_attention is False` and gets `not_running`
**Symptom:** a hermetic test drives the loop to a healthy terminal (a clean
`stopped`, say), asserts nothing needs attention, and fails with
`code="not_running"` / `needs_attention=True` — no blocker, no park, nothing
wrong with the state it just built.
**Cause:** `health.check` asks `LoopLock(config.state_dir).read()` whether a loop
is running, and in a test there is none, so it reports the loop as down before
ever reaching the healthy path. Correct behaviour: from outside the process, a
state dir with no live lock IS a loop that is not running.
**Fix:** take the lock around the assertion — `with LoopLock(config.state_dir):`
— which makes `is_live` true for the test's own pid. See
`test_stop_livelock.py::test_a_single_stop_behaves_exactly_as_today`.
**The order matters more than the lock.** Open blockers are judged BEFORE the
lock's liveness and before any phase, so a test asserting `needs_attention is
True` off a blocker needs no lock at all, while one asserting `False` does. And
after a `loop_fatal` park, answering the blocker leaves the SESSION parked, so
`health` moves from `stuck_blocked` to `stuck_parked` rather than to `running` —
assert the code, not the boolean, or the test will read as a regression.

### A `health.check` test about a CORRUPT state file raises instead of returning a verdict

**Symptom:** a test writes `{ not json` into `config.state_file` to exercise the
strand survey's "cannot tell which task is current" arm, calls `health.check`,
and gets `StateCorruptError` out of the call instead of a `Health` back.
**Cause:** `check` is two steps — `_judge` (is the LOOP working) then
`_with_strands` (is a task off the board) — and `_judge` loads the same state
file first, where a corrupt one raises. That is pre-existing behaviour and is not
the strand survey's; the survey's own state-read guard is only reachable if the
file rots between the two reads.
**Fix:** assert against `health._strand_survey(config)` directly and say in the
docstring why. The guard is defensive, still correct, and testing it where it
lives is honest; routing `_judge`'s own raise through the survey would be a
different change (it would make every corrupt-state verdict a strand verdict).
Do **not** "fix" it by making `_strand_survey` swallow the error silently — a
survey that answers "nothing is stranded" when it could not look is exactly the
fail-open the survey exists to close. See
`test_strand_recovery.py::test_an_unreadable_state_file_refuses_to_guess_which_task_is_current`.

### A scripted `stop` with an empty `reason` never stops the loop

**Symptom:** a test scripts `{"version": 3, "decision": "stop", "reason": ""}` as
the round's only reply, expects `stopped`, and instead the fake conversation
raises something like `test script exhausted: no response left` — or the round
ends holding a corrective prompt nobody asked for.
**Cause:** `contract._require_str` refuses an empty *or whitespace-only* string
for every field it guards, and `reason` is one of them. The reply never becomes a
`Directive` at all: it is a `missing_field:reason` parse error, so the round
spends a parse retry and asks the conversation again — which a one-element
script cannot answer. Nothing was dispatched, and nothing a stop would have done
happened.
**Fix:** decide which half you are testing. For the stop DISPATCH, hand the
directive straight in — `orch._dispatch(Directive(decision=Decision.STOP,
reason=""))` — as `test_blockers.py` and
`test_stop_livelock.py::test_a_stop_with_an_empty_reason_still_parks_and_says_so`
do. For the PARSE, assert the `ContractError` code, as
`test_the_contract_never_delivers_an_empty_stop_reason` does beside it.
**Do not loosen `_require_str` to make the round work.** The reason is the only
record of why a session ended, and requiring one is the behaviour rather than the
obstacle. It also means production code reading a stop reason is guarded against
a shape only a hand-built directive can produce — worth keeping fallbacks for,
not worth widening the contract for.

---

## 3. Frontend build

### `tsc --noEmit` passes but `npm run build` fails
**Symptom:** `npx tsc --noEmit` exits 0; `npm run build` then fails with e.g.
`error TS2304: Cannot find name 'global'` in a test file.
**Cause:** `npm run build` runs `tsc -b` (project build), a different config
scope from a bare `tsc --noEmit`. Test files and their libs are resolved
differently, so browser-lib-absent globals like `global` only surface in the
build.
**Fix:** **`tsc --noEmit` alone does not prove the build is clean — always run
`npm run build` too.** For the specific case: capture the mock stub returned by
your fetch helper and assert on it, instead of reaching for `global.fetch`.

---

## 4. Lint (ruff)

Config is `/ruff.toml`; run `ruff check .` from the **repo root**. See CLAUDE.md
§11.

### Turning on all of `E` explodes with E501
**Symptom:** A config of `select = ["E", "F"], ignore = []` reports **405**
`E501 line-too-long` findings.
**Cause:** ruff's *default* is only E4/E7/E9 + F. Selecting all of `E` adds E5,
i.e. line length.
**Fix:** `ignore = ["E501"]` (what `/ruff.toml` ships). Enabling it would be a
repo-wide reflow, not a lint fix, and would bury real findings.

### `--fix` on F401 can delete a load-bearing import
Three traps, all checked before the 2026-07-27 sweep:
- **Re-exports.** A module may import a name purely so others can import it from
  there. Check for `__init__.py` files and `__all__` first, then grep for
  `from <module> import <name>` across the repo.
- **Availability probes.** In `masking/latest_ingest.py` the import *is* the
  test — it sets `VISUALIZE_AVAILABLE` by whether it raises. Ruff suggests
  `importlib.util.find_spec`, which is **not** equivalent: it proves the module
  resolves, not that the symbol exists. Keep the `# noqa: F401`.
- **`compileall` does not validate an F401 sweep.** It checks syntax, not import
  resolution. Scraper modules imported by no test were smoke-tested with
  `python -c "import <module>"`.

### E402 that must NOT be "fixed"
**Symptom:** ruff reports import-not-at-top in `subtitle-scraper/` modules and in
`matcher_service.py`.
**Cause and fix:** two different things wear the same rule number.
- **Real bug (27 of 41 found this way):** a stray
  `logger = logging.getLogger(__name__)` sitting *above* an import block —
  `main.py` had 21 findings from one such line. Move the logger below the
  imports.
- **Deliberate:** sibling modules imported after a `sys.path.insert` (the
  replacement for the old `os.chdir` hack, TODO #3). **Hoisting these breaks the
  scraper.** They carry `# noqa: E402` and a "do not hoist" comment. Leave them.

---

## 5. Research / documentation errors

These cost more than the code errors, because they produce confident wrong
statements rather than a stack trace.

### CLAUDE.md §8b described a deleted function as the live writer of `status`
**What happened:** §8b's `status_marked_known` rule said the status field was
"forced to `known` by `word_service.upsert_word_status` (called from the router
before `apply_progression`)". That function was deleted 2026-05-19 (#2) and the
router has made a single `apply_progression(..., status_override=body.status)`
call ever since — which §8b's *own* atomicity rule and §10 both state correctly,
two paragraphs apart. Anyone reading §8b top-down met the stale sentence first
and came away believing the two-call, non-atomic flow was still live. Three
consecutive audits (2026-07-30, 08-02, 08-03) re-flagged the same sentence
before it was fixed 2026-08-04.
**Rule:** treat an internal contradiction inside one doc as a code question, not
an editorial one — grep `def <symbol>` in `lexy-app/backend/services/` and read
the caller before deciding which half is stale. And note the inverse trap:
§8b/§10 name deleted symbols **on purpose** in their `Historical:` /
`~~struck-through~~` clauses (`word_service.upsert_word_status`,
`srs_service.py`, `src/app/`), so a grep hit in CLAUDE.md is not
evidence the symbol exists — and scrubbing those historical mentions destroys
the trail that `docs/SUMMARY.md`, `docs/TESTS.md` and `WORKFLOW_AUDIT.md` keep
in sync. Fix the sentence that describes it as *live*; leave the history.

### Trusting a grouped reference instead of the numbered source
**What happened:** Reported "Hole 19 (hardcoded `'de'`) is CLOSED, Hole 20
(per-message detection) still open". The mapping is the **reverse**:
`WORKFLOW_AUDIT.md` numbers Hole 19 as per-message detection and Hole 20 as the
hardcode. The error came from reading ROADMAP/TODO, which refer to them jointly
as "Hole 19/20" without ever stating which is which, and inferring the order.
The wrong mapping reached a commit message before being caught.
**Rule:** when an item has an ID, read the file that *defines* the ID —
`WORKFLOW_AUDIT.md` for holes, `TODO.md` for numbered items,
`SECURITY.md` for S-numbers. A joint reference elsewhere is not a definition.

### Reporting a doc's claim as a verified fact
**What happened:** Nearly reported "745 passed" from a `docs/TESTS.md` line
rather than from a run.
**Rule:** either run it and quote the run, or say explicitly that it's a
doc-claimed figure and give the command. Docs drift; several claims in this repo
were stale by weeks (see the 2026-07-27 sweep).

### A doc can be stale in the *other* direction
**What happened:** ROADMAP and WORKFLOW_AUDIT both listed W9 / the free-chat
language hardcode as open. It had shipped 2026-05-21 (migration 031). The
remaining `or "de"` in `routers/chat.py` is a legacy-row fallback, not a
hardcode — easy to misread as the bug still being present.
**Rule:** before reporting an item as open, grep the code. "The roadmap says
it's open" is not evidence.

---

## 6. Autoloop browser transport

Both entries below are from the same day of live bring-up (2026-07-29/30) and
share one lesson: **a browser is not an API. What the DOM shows is not what the
server did, and what the model wrote is not what `innerText` returns.**

### Loop hangs in `awaiting` forever; the conversation shows no new message
**Symptom:** `python -m autoloop smoke-browser` recorded `request_submitted`,
then sat in phase `awaiting` for 15 minutes. The conversation contained only its
baseline messages and **no user message carried the request id**.
**Cause:** ChatGPT renders your message bubble *optimistically*, before the
server accepts it. `submit()` polled the current, unreloaded DOM, saw that
bubble, and reported success. The send had failed (`fill()` on the ProseMirror
contenteditable never drove the editor's own state), and the next phase's
unconditional `goto()` reloaded the page and erased the evidence.
**Fix:** applied in `autoloop/browser/chatgpt.py`. Submission is CONFIRMED only
when an assistant turn for that request begins, or a controlled `reconcile()`
reload finds the request id in persisted history. Ambiguity → `UNCONFIRMED` →
phase `submission_unconfirmed`, which never auto-resends (the backend may have
accepted a message the browser missed). Input is now focus + `ControlOrMeta+A` +
`Delete` + `keyboard.insert_text` + content verification + wait-for-Send-enabled.
`fill()` was removed from the session protocol so it cannot come back.
**Do not** treat an optimistic bubble, a cleared composer, or Send-button state
as proof a message was sent.

### `no_json_block: no fenced ```json block found in response` — but the reply IS a fenced block
**Symptom:** three consecutive smoke replies rejected as malformed. The
transcript's captured text was
`'JSON\n{"version":3,"decision":"stop","reason":"smoke test acknowledged"}'`.
**Cause:** the loop reads replies from a **rendered** page. ChatGPT renders a
fenced code block as a widget whose `innerText` is the language label on its own
line followed by the code — **the backticks do not exist in the DOM**. The
fence regex could never match, and the bare-object fallback failed because the
text starts with `JSON\n`. ChatGPT had complied perfectly every time.
**Fix:** `autoloop/contract.py` accepts exactly two envelopes — one fenced
```json block, or the whole reply as one JSON value preceded by an optional
language-label line. A second object, another decision, or trailing text is
rejected (`trailing_content`); **position is never used to pick between
candidates**, because a directive can authorize a commit or push. (The first fix
attempt did use a last-object-wins brace scanner; that was replaced before any
live use — a positional rule is a silent-mis-selection risk, not a convenience.)
Regression tests use the byte-exact captured text.
**Lesson:** when a parser consumes rendered HTML rather than raw model output,
test it against bytes captured from the real page, not against what you believe
the model sends.

### `invalid_json: Invalid control character at: line 6 column 2073 (char 2324)`
**Symptom:** `parse_error` fired 25 times in three weeks and EIGHT of those in
one thirty-hour window (measured 2026-08-20/21), every recent one reporting the
same offset shape — line 6, column ~2000. Twice the third malformed reply in a
row parked the loop `parse_budget_exhausted`, which is loop_fatal
(`policy.max_parse_retries` is 2): 2026-08-20 23:23 on bind-01's postcommit
review, unattended for six hours, and 2026-08-21 07:02 on auto-02's.
**Cause:** a literal newline inside a JSON string value. The deep column offset
is the tell — it lands inside the long `notes` value, whose entire
specification in `CONTRACT_INSTRUCTIONS` was "anything else worth recording":
no length, no format, no escaping. The reviewer's CONTENT was correct every
time; only its encoding was not. Note the KIND changed. Historically the common
parse failure was `no_json_block` (13 of 25) — a conversational reply with no
JSON at all — so a `parse_error` count alone will not show you this.
**Fix:** `autoloop/contract.py` bounds `notes` (at most 200 characters, on one
line) and states the general rule — never a literal line break inside a JSON
string value, write `\n` — in the text that is re-sent EVERY round. The parser
is deliberately unchanged and still refuses the malformed reply. Both incidents
were first recovered with `run --answer` saying the same thing by hand, which
works and then decays: a conversational instruction lives in the thread and
does not survive a rotation or a fresh session.
**Lesson:** an optional free-text field with no stated bound is a latent parse
failure. `notes` had been used in 0 of 578 directives and is read by nothing in
`orchestrator.py`, `dashboard.py`, `transcript.py` or `worktask.py` — and it
was still the single largest source of loop-fatal parks.

### `submission of alr-… is AMBIGUOUS` on a send that simply never landed
**Symptom:** roughly one turn in eight parked for a human. `.autoloop/state.json`
sat in `awaiting`; the diagnostics snapshot said `send_attempted: true`,
`reconciled: true`, `matching_user_messages: 0`, `composer_chars: 0`,
`send_button_enabled: false` — and the conversation contained no such message.
Two of sixteen requests in one run (`alr-b58c9a33-0001`, `alr-fe650dbd-0001`).
**Not** length-related: 104k- and 113k-character prompts landed, and the
byte-identical resend of the 13,265-character failure landed on the next try.
**Cause (partly diagnosed, and that is the point):** the send genuinely did not
persist. *Why* it did not persist was unknowable, because every field in the
snapshot is a DOM reading, and "the server refused it" and "the browser never
issued it" produce identical DOM. Unable to tell those apart, the loop correctly
refused to resend — resending an accepted-but-unobserved message double-posts —
and parked. The cost was a human per dropped send.
**Fix:** `autoloop/browser/observation.py` (2026-07-31). A passive
`page.on("response")` listener on the conversation-send endpoint supplies the one
fact the DOM cannot: whether the browser's own request succeeded. A 4xx/5xx or a
request that never completed **disproves** acceptance → `SubmitResult.REJECTED` →
reconcile to confirm absence → exactly one same-chat resend. Missing or mixed
evidence still classifies as UNKNOWN and still parks.
**Do not** use a 2xx as proof of persistence, and do not remove the "is our
request id in the conversation" check on the strength of one: a 200 on a stream
that then dies must fall through to the response-start timeout, not be read as a
reply. Persisted history outranks the status code in both directions — a
rejecting status on a request that IS in history resolves to accepted.
**Related trap while diagnosing this:** a long user turn can render as a
`Pasted markdown(N).md` attachment chip rather than inline text (offset 227680 of
the captured `page.html`). `innerText` then contains none of it, so
`has_request()` cannot see a request id that is genuinely there. Loop-sent
prompts do not hit this — `keyboard.insert_text` fires no paste event — but a
human paste into the same conversation does.

### `browser session lost: Locator.click: Timeout 30000ms exceeded. waiting for locator("#prompt-textarea")` — repeating for hours
**Symptom:** every turn fails the same way, from one moment onward (07:56, on
2026-08-14/15), with nothing else in the transcript. The words **rate**,
**limit** and **throttle** appear nowhere in it. The loop restarts Chrome,
retries, fails identically, and keeps going until a task burns its attempt
ceiling (`blk-pkt-03-001`, `attempt_count_ceiling`) without ever reaching a
review. The state file and the diagnostics dumps say `composer_present: true`.

**Cause:** ChatGPT is **rate limiting the account** and has put up a modal:

> **Too many requests**
> You are making requests too quickly. We have temporarily limited access to
> your conversations to protect your data.
> Please wait a few minutes before trying again.  `[ Got it ]`

It is a full-screen overlay (`class="absolute inset-0"`, z-50) that **intercepts
pointer events**. Nothing is removed and nothing is disabled, so every click
into the composer times out while the page keeps reporting itself healthy.

**The restart is not the recovery — it is the mechanism.** Restarting and
retrying is what generates requests too quickly, so the loop deepened the exact
condition it was failing on and reported the deepening as further browser
failures. The limit is account-level and server-side; a fresh browser meets the
same wall and adds one more request.

**THE TRAP THAT COST AN HOUR:** the composer still EXISTS and reports
visible/enabled throughout. Three separate passive checks — page loads, message
count, composer presence — and a search for `Too many requests` in the page's
`inner_text` **all reported healthy** against a firmly limited account. Presence
of the composer is not evidence the page is usable. Only attempting an
interaction, or testing for the modal directly, tells them apart. Any readiness
probe written against composer presence alone reports a false all-clear.

**Detector — use the stable hook, not the prose.** Captured live 2026-08-15 by
attempting a click while throttled; Playwright named the element that
intercepted it:

```
id="modal-conversation-history-rate-limit"
data-testid="modal-conversation-history-rate-limit"
class="absolute inset-0"
```

Match on `data-testid`: it is stable across wording and locale, and the visible
text is neither (nor even findable — see the trap above).

**Fix (in the code since 2026-08-16, brw-09):**
`browser/selectors.py::rate_limit_modal` + `rate_limit_dismiss`;
`browser/chatgpt.py::_check_throttled` runs at every polling site (and re-reads
after any composer failure in `submit`), raising `errors.RateLimitedError` —
deliberately **not** a `BrowserError`, so it can never reach the restart-and-
retry recovery. `orchestrator._handle_rate_limited` waits instead: escalating
back-off (`browser.rate_limit_backoff_seconds`, doubling to
`rate_limit_backoff_max_seconds`), no restart, no client drop, and **not**
charged to `max_consecutive_failures` — it has its own
`policy.max_rate_limit_backoffs`, which ends in a `needs_user` park with
`code="rate_limited"` naming the throttle and the measured total wait. The modal
is dismissed after each wait, because it hides the composer even after the
server-side limit expires and a stale one would read as a continuing throttle —
but the dismissal is **not** read as the limit lifting. The re-probe is the next
step, and only a step that COMPLETES clears the streak: the overlay is gone
because the loop closed it, so resetting on a dismissal would reset on every
occurrence and turn the back-off into a fixed-interval retry that never
escalates and never parks.

**The wait survives the process.** `state.rate_limit_retry_not_before` is a
persisted deadline written *before* the sleep, and `run()` serves whatever
remains of it before every step (transcript: `rate_limit_wait_resumed`). Kill the
loop mid-wait and restart it and it finishes the wait; without that, a supervisor
restart would resume with a counter it cannot tell apart from a wait already
served, skip the back-off entirely, and rebuild the same storm out of process
restarts. `rate_limit_wait_seconds` is credited when a wait finishes, not when it
starts — so if the park message says 30s, 30s of waiting really happened.

**If you are reading an OLD transcript:** a run of identical
`Locator.click: Timeout … #prompt-textarea` errors with `browser_restarted`
between them is this, before the fix. A run that says `rate_limited` is this,
after it — leave the account idle and `run --retry`.

**But confirm it IS a throttle before you wait it out** — a browser with no
attachable page produces the same park. See the entry below.

### The loop reports `rate_limited` for hours and the account is NOT limited — `/json/list` returns `[]`
**Symptom (2026-08-17):** the loop backs off, escalates, spends its whole
`max_rate_limit_backoffs` budget and parks saying `rate_limited`. A probe run by
hand agrees: "still rate limited", repeatedly, for **four hours**. Meanwhile
ChatGPT in a normal browser is perfectly responsive, and nothing on the account
is throttled. Every health check says the browser is fine:

```bash
curl -s http://127.0.0.1:9222/json/version   # 200, with a valid webSocketDebuggerUrl
pgrep -f -- '--user-data-dir=.*autoloop-chrome'   # Chrome is running
```

**Cause:** the operator had closed the browser **window**. On macOS that leaves
Chrome running: the process survives, it keeps the debug port, and
`/json/version` keeps answering with a valid `webSocketDebuggerUrl` — so every
check built on that endpoint reports a healthy browser. What it does not have is
any target to attach to:

```bash
curl -s http://127.0.0.1:9222/json/list      # []  ← the whole fault, in one line
```

Playwright could not attach at all. With no page there was no modal to read, no
composer to click and nothing to re-probe — so the loop fell back to its
default, which is "assume the limit still holds", and waited out a limit that
never existed. **A live CDP endpoint with no attachable target looks like a
healthy browser to every check that only curls `/json/version`.** Restarting the
profile restored 11 targets immediately.

**Fix (2026-08-17, brw-11):** `_handle_rate_limited` now classifies before it
waits, and before it concludes the limit still holds
(`orchestrator._classify_rate_limit_state`):

1. **throttle modal present on an attachable page** → genuinely rate limited.
   Waits exactly as before: no restart, no client drop, no
   `consecutive_failures`. Asked FIRST, so a page that can show the modal is
   never restarted on the strength of an odd target count.
2. **no modal and a real click on the composer LANDS** → the limit cleared;
   resume without waiting. Presence proves nothing here (see the entry above);
   only the click does.
3. **nothing to attach to** (`/json/list` reports zero `type: "page"` targets)
   → **not a rate limit.** Drops the client, restarts the profile via
   `browser.restart_command` ONCE, re-probes once. Recovered → carry on;
   still nothing → park `code="browser_unattachable"` with a question that
   names the BROWSER. That restart does **not** increment
   `rate_limit_backoffs`: that budget bounds waiting on the *server*, and this
   is a local recovery that makes no request at all.

**What bounds that restart, and what lifts the bound:** ONE per *episode*, and
an episode ends when a step COMPLETES — not when the re-probe finds pages. So
the ordinary recovery (zero targets → restart → the next step runs normally)
leaves the next unattachable browser, hours later, free to restart again; two
zero-target faults with no completed step between them get one restart and then
a park. Deliberately not reset on a successful re-probe: targets can exist at
probe time and be gone at attach time, and clearing it there would give
restart → probe OK → clear → restart with nothing stopping it. If you see
`browser_unattachable` with `restart_already_spent: true`, that is the second
fault of one episode, and the browser really did come back with nothing to
attach to.

`attachable_page_targets` (`browser/playwright_session.py`) distinguishes **zero
from unmeasurable** on purpose: 0 means the endpoint answered and named no page,
which authorises the restart; an endpoint that answers nothing is the ordinary
`BrowserError` path, which already diagnoses itself (`describe_cdp_endpoint`)
and restarts on its own budget.

**Reading a transcript:** `browser_unattachable` → the browser, not the account.
`browser_reattached` after it → one restart fixed it. A `rate_limited` entry now
carries `classification` and `evidence`, so you can tell a modal that was really
observed from a default reached because nothing could be asked.

**Verify by hand, in this order** — `/json/list` is the question, `/json/version`
is not:

```bash
curl -s http://127.0.0.1:9222/json/list | python3 -c \
  'import json,sys; t=json.load(sys.stdin); print(sum(1 for x in t if x.get("type")=="page"), "page target(s)")'
# 0 → open the profile window, or: python3 -m autoloop.browser.chrome_restart
```

### `browser session lost (TimeoutError: Locator.get_attribute … waiting for locator '[data-message-author-role]')` — while the loop waits for its OWN sent message

**Symptom (2026-08-17, 09:05–09:15):** the loop is in `awaiting` with
`submitted: true` and `send_attempted: true`, and every round dies with a
locator timeout on the message list — it is waiting for the message it
believes it sent. Every check that exists passes: the composer is clickable,
no throttle modal is present, Chrome is healthy with 12 CDP targets, and the
ACCOUNT is demonstrably writing — an operator posted by hand in a DIFFERENT
conversation and it persisted. Yet this conversation stays pinned at the same
message count (33, for ten minutes) while the loop reads each timeout as a
lost session, restarts Chrome every 45 seconds, and gets nowhere.

**Cause: the CONVERSATION is wedged, not the browser.** The chat has silently
stopped taking this loop's messages, so the submission the loop is waiting
for never appeared and never will. **A locator timeout on one's own sent
message READS as a browser fault and is not one** — the browser is fine, so
restarting Chrome cannot possibly help. The recovery that fixes it is
rotation (`ConversationUnusableError` → `_attempt_rotation`), which fixed
this same conversation in seconds, twice, by hand: c/6a8038a8 had already
degraded once before, reaching 90+ packets and eventually refusing to load at
all.

**Tell it apart from the two faults it mimics before acting:**

* **A dead browser (brw-11's state 3) DOES want a restart.** That one has no
  attachable target at all — `curl -s http://127.0.0.1:9222/json/list`
  reports zero `"type": "page"` targets and every DOM read fails. Here the
  page answers every read; the reads are themselves the attachability proof.
* **An unmounted tail is not absence.** ChatGPT mounts a WINDOW of a
  conversation, so "my message is not in the DOM" can mean only "nothing
  scrolled to it" (the 2026-08-05 false park). Real absence needs the two
  mounted-tail proofs `find_conversation_with` already requires: the list
  demonstrably reached its END, and the mounted window then stopped changing.
* And a throttle is still a throttle: if the rate-limit overlay is up, that
  routing (`RateLimitedError`, wait — never restart, never rotate) wins.

**Fix (2026-08-18, brw-12):** the classification fires from BOTH surfaces the
fault wears, and the second is the one the incident actually wore.
`BrowserChatGPT._rule_out_missing_submission` (`browser/chatgpt.py`) runs when
the response-START bound expires cleanly with the request absent from the
mounted window — and it is ALSO reached when a mid-await DOM read dies with
the lost-session label itself (`_classify_awaiting_read_failure`): the exact
`Locator.get_attribute` timeout above is caught inside `await_response`,
re-probed through the same session (a fresh read succeeding IS the
attachability proof), and only then classified. Either way the tail is
mounted with the same two proofs the by-content search uses; only settled
absence on an attachable, un-throttled, logged-in page raises
`ConversationUnusableError(code="submission_never_appeared")`, which the
orchestrator answers with rotation — no Chrome restart, and **no charge to
`consecutive_failures`** (the brw-03 rule: the budget that decides recovery
is hopeless must not be spent on a fault no restart could fix). Everything
short of that proof keeps its old route: a probe that cannot read the page
(brw-11's dead browser), a request the probe SIGHTS, and an absence that
cannot settle all re-raise the original `SessionLostError` onto the ordinary
restart-and-budget path, and a clean timeout with unproven absence falls back
to the `stage="start"` timeout and its existing three-strike silence
rotation. A throttle or auth redirect discovered by the probe still routes as
itself.

**Reading an old transcript:** repeated `browser_restarted` entries while one
conversation's message count never moves, with `submitted: true`, is this,
before the fix. After it, look for `conversation_unusable` with
`reason_code: submission_never_appeared` followed by `conversation_rotated`.

---

## 7. Autoloop worker/publisher separation (M2)

### zsh silently eats a character out of `"$VAR:literal"` — a refspec with the wrong ref name, no error
**Symptom:** `git push origin "${CANDIDATE}:refs/heads/task/t1"` worked from a
shell one-liner, but an EARLIER attempt written as `"$CANDIDATE:refs/heads/task/t1"`
(no braces) failed with `error: src refspec fdfc398...efs/heads/task/t1 does not
match any` — note `efs` where `refs` should be. This happened identically on two
separate ad hoc verification commands before the pattern was recognized.
**Cause:** zsh applies history-style modifiers (`:t`, `:h`, `:r`, `:e`, ...) to a
**bare** `$name` expansion — including inside double quotes — when a `:`
immediately follows it. `$CANDIDATE:refs/...` parses as `$CANDIDATE` with
modifier `r` (bash-style "remove one word" semantics don't apply; zsh's own `:r`
strips a trailing suffix) applied, silently eating the leading `r` of the literal
text that followed. `${CANDIDATE}:refs/...` (braced) is immune — the modifier
syntax only fires on the unbraced form.
**Fix:** nothing repo-side (this only affects ad hoc shell commands, never the
Python code, which builds refspecs via plain f-string concatenation with no
shell involved). **Always brace a variable that is followed by a literal `:`
in a zsh command** — `"${VAR}:literal"`, never `"$VAR:literal"` — when
constructing a git refspec (or anything else `name:value`-shaped) by hand at a
shell prompt.

### `verify_worker_isolation` reports an ambient system credential helper even against a freshly created, genuinely no-remote worker repo
**Symptom:** `GitGateway(worker.path, policy)` (no `env=`) run against a brand
new `WorkerRepoManager`-created repo (verified to have zero remotes, zero
hooks) still reported `credential.helper=osxkeychain` as a violation.
**Cause:** `GitGateway` had no way to control the environment its subprocess
calls ran under — every `git` invocation inherited the CALLING process's own
environment, ambient system/global config included. `git config --get-regexp`
resolves whatever environment the subprocess itself runs under, not "the
repo's local config in isolation" — so a check built purely on `GitGateway`
reflects the CALLER's isolation, not the repo's, unless the caller explicitly
scrubs the subprocess environment too.
**Fix:** `GitGateway.__init__` gained an optional `env: dict | None = None`
parameter (default `None` = prior behavior, inherit the current process
environment — zero change for any of the 579 pre-M2 construction sites),
threaded into every `subprocess.run(..., env=self._env)` call. `worker_env.
WorkerRepo.gateway(policy)` is the convenience that constructs a `GitGateway`
with `env=worker_env()` correctly; anything checking worker isolation MUST use
it (or the equivalent explicit `env=`) rather than a bare `GitGateway(path,
policy)` — see `verify_worker_isolation`'s own docstring, which states this
requirement explicitly so it cannot be missed a second time.

### A test writes a REAL `.al`/`.autoloop` directory into the repo root instead of `tmp_path`
**Symptom:** after running `autoloop/tests/test_blockers.py`, `git status`
showed an untracked `.al/` at the repo root (with `blockers/` and
`tasks.json` inside) — and a later test in the same file failed with
`blocker 'blk-t1-001' is already resolved`, even though that test's own
`tmp_path` had never seen that id before.
**Cause:** a test wrote a `config.toml` with `[paths] state_dir = ".al"` (a
RELATIVE path, copied from `test_v1_smoke.py`'s `write_config_toml`, which
deliberately uses a relative path to test that real-world shape) and then
called a CLI command function (`_cmd_blockers`/`_cmd_answer`) that reads it
via `load_config` — without ever `monkeypatch.chdir`-ing into `tmp_path`
first. `config.state_dir` stayed the literal relative `Path(".al")`, so
every `config.blockers_dir`/`config.tasks_file` access resolved against
whatever directory `pytest` was actually invoked from (the repo root), not
`tmp_path`. Every test in the file that used the same pattern wrote into
that SAME real, never-cleaned directory, so state leaked from one test into
the next entirely outside of `tmp_path`'s isolation.
**Fix:** if a test needs `load_config`'s file-reading path (rather than
building an in-memory `AutoloopConfig` directly) AND does not also need a
real relative-cwd scenario, write the config with an ABSOLUTE `state_dir`
(`str(tmp_path / ".al")`) instead of a bare name — see `test_blockers.py`'s
`write_config_toml`. If the test genuinely needs to exercise a *relative*
`state_dir` (as `test_v1_smoke.py`'s does, on purpose, for
`test_relative_state_dir_still_provisions_worker_and_publisher`), keep it
relative but `monkeypatch.chdir(repo_root)` into a `tmp_path`-rooted
directory BEFORE anything reads the config, exactly as that test already
does. Either way: `rm -rf .al` at the repo root if a test run leaves stray
state behind — `.al` (the state-dir name these tests use) is NOT
gitignored (only the real `.autoloop/` is), so `git status` is the tell,
not a build failure.

---

## 8. Autoloop M1 hardening (external workers, escape detection, non-circular task scope)

### A task parks on `attempt_count_ceiling` with a `review_round` far below it — and an operator has to reset the counter by hand
**Symptom:** `python -m autoloop blockers` shows `attempt_count_ceiling` for a
task that was never failing review. Measured 2026-08-15..17 and reported at the
time as **five** hand repairs; the table below enumerates six.

| task | attempts | review_round | what actually happened |
|---|---|---|---|
| brw-09  | 5 | 1 | four STRUCTURAL refusals (paths outside `approved_paths`) — the reviewer saw none of them |
| exec-01 | 5 | 1 | two rounds died to the agent provider's session-limit 429; the reviewer's own words: "produced no work" |
| port-01 | 5 | 3 | one rate-limit round plus browser churn |
| brw-11  | 4 | 0–1 | three rounds lost to faults (incl. an agent-level API error at 368s) while its lint fix was already committed and passing |
| dash-04 | — | — | same shape |
| hlth-01 | — | — | same shape |

Each was repaired the same way: edit `.autoloop/executions/<task>.json`, set
`attempt_count` back to 0, leave `candidate_sha` / `review_round` /
`last_revise_feedback` alone. `~/.autoloop/afk-worker.sh` was written to
automate it and had to GUESS with a heuristic (`attempt_count - review_round
>= 2`), because the record did not say why any attempt had been spent.
**Cause:** ONE counter was paying for two unrelated things. `attempt_count` is
incremented in `orchestrator._dispatch_task_postcommit` before the executor
runs — deliberately, so a crash or a validation failure that never reaches a
commit still consumes an attempt (M1 finding #3), which is the only bound on a
task that dies every round without ever reaching a reviewer. But that same
increment also charged rounds destroyed by a provider throttle, a killed agent
or a process that did not survive, so a task converging through real review
rounds could be killed by rounds it did not cause.
**Fix:** applied repo-side 2026-08-17 (task budget-01) — two budgets, not one.
`attempt_count` keeps bounding the task's own work (validation failures,
structural refusals, rounds that reached the reviewer); `fault_attempt_count`
bounds rounds lost to faults, with its own ceiling and its own park code
`fault_attempt_ceiling`. `TaskExecution.attempt_ledger` records, per attempt,
`"<ordinal>|<budget>|<reason>"` — so the answer is read, never inferred. **Both
budgets still terminate**, so nothing here removes a bound: a task that faults
every round parks on `fault_attempt_ceiling` instead of churning. If you are
looking at this because a task parked anyway, read its `attempt_ledger` first —
it names each round's cause. The watcher script is now redundant and should be
deleted; it can only ever re-derive, badly, what the ledger states.

**Reading a ledger entry.** `budget` is `pending` / `pending_fault` while the
round is OPEN and `task` / `fault` once it has settled, so an entry still
reading either open label means one thing only: the round never reached one of
its own exits. Two ways that happens, both environmental — the process died
mid-round, or a `GitError` escaped the dispatch to `_handle_git_failure` (which
charges `consecutive_failures`, not the task). Either way the next dispatch
settles it onto the fault budget. `reason` is a bare outcome slug
(`sent_for_review`, `post_commit_verification_failed`,
`executor_reported_failure`, `interrupted_mid_round`, an
`audit.agents.AGENT_FAULT_*` code) — or `"<origin>><outcome>"` for a round a
fault forced the loop to redo, where the origin is the fault code that destroyed
the earlier review. `3|fault|browser_session_lost>sent_for_review` reads: the
third dispatch happened because a browser session loss killed a review, it was
charged to the fault budget, and it reached the reviewer.

### A task's `attempt_count` grows anyway, every SECOND fault, with a ledger full of `fault` entries
**Symptom:** the split above is in place, but a task alternating
review → fault → redo → review → fault still creeps up `attempt_count`. One
fault is absorbed, the next is not. Found in review of `budget-01` itself
before it landed.
**Cause:** a redo was written into the ledger as an already-SETTLED
`fault|<code>` entry at dispatch, which conflated "which counter is this
charged to" with "has this round finished". When the redo reached the reviewer,
`_finalise_attempt` correctly refused to re-stamp a settled entry — so the
ledger never recorded that a review was in flight — and `_note_round_fault`,
which matched the literal pair `(task, "sent_for_review")`, saw
`(fault, "<code>")` and declined to mark the second lost review. The next
dispatch found no `pending_fault_code` and billed the task.
**Fix:** a redo is opened OPEN like every other round (`pending_fault`), so its
own exit still stamps it; `_settle_attempt` is the single rule both
`_finalise_attempt` and `_reconcile_unfinished_attempts` go through; and
`_note_round_fault` keys on the OUTCOME segment (`worktask.attempt_outcome`) on
either budget. **The generalisable trap:** if one field answers both "what state
is this in" and "what did it cost", the transition that writes the cost early
erases the state, and every check downstream that keyed on the state goes
quietly false. Pinned by
`test_consecutive_session_ending_faults_never_fall_back_onto_the_task_budget`.

### A task's `attempt_count` grows on the round AFTER a redo the environment interrupted
**Symptom:** the two fixes above are in place and a review really was lost to a
fault, but the recovery is interrupted a second time — the loop is restarted
mid-redo, or the redo's agent hits the provider — and the dispatch that follows
is billed to `attempt_count`. The ledger shows a `fault` entry whose outcome is
NOT `sent_for_review`, immediately followed by a `task` entry. Found in review
of `budget-01` before it landed, and initially documented rather than fixed.
**Cause:** `pending_fault_code` is consumed by `_open_attempt` and was only ever
re-armed by `_note_round_fault`, which requires the last ledger entry to be a
SETTLED round that reached the reviewer. A redo taken by the environment
satisfies neither: while it is open the entry reads `pending_fault`, and once
settled its outcome is `interrupted_mid_round` (or an `AGENT_FAULT_*` code), not
`sent_for_review`. So the marker was gone while the lost review was still lost,
and the loop treated the next recovery dispatch as the task's own next try.
**Fix:** `_settle_attempt` rule 4 — a round OPENED on the fault budget that
STAYS on it without reaching a review re-arms `pending_fault_code` from its own
origin. It lives in `_settle_attempt` because all three ways this happens pass
through that one method: `_reconcile_unfinished_attempts` (the process died
mid-redo), and `_finalise_attempt` with either an `ExecutionOutcome.fault_kind`
or `worker_environment_drift`. **Still bounded** — each carried-forward dispatch
pays a `fault_attempt_count` charge, so a chain interrupted every time parks on
`fault_attempt_ceiling` like any other run of faults, and the marker clears for
good as soon as a round reaches a reviewer or fails on the task's own merits (a
structural refusal, a failed validation, an escape — those move the charge back
to `attempt_count` and end the chain). **The generalisable trap:** a
consume-once marker describes an EVENT, but what this needed to describe was a
STATE that outlives the event — "this task is still recovering a review it
earned". A single fault re-armed it; the second one had no event left to fire
on. Pinned by
`test_a_recovery_chain_interrupted_twice_never_reaches_the_task_budget` and
`test_a_recovery_chain_interrupted_forever_still_hits_the_fault_ceiling`.

### A merged fix has no effect on the running loop, and re-reading the code shows it IS merged
**Symptom:** a change lands on the base branch and the loop keeps behaving as
it did before. `git log` shows the merge, the file on disk holds the new code,
and the loop is not doing what the new code says. Measured 2026-08-18: the loop
process started 04:07:03; plan-01 merged a hard gate at 06:23:59 ("no task
starts without an approved decomposition"); by 09:00 the registry held 0
decompositions across 102 tasks — including dash-10, a task that STARTED after
the merge. brw-11's browser fix, merged 00:58, was inert the same way for the
whole night.
**Cause:** merging into a checkout does not reload a live Python process.
`policy.py` was imported at 04:07 and stayed imported; every module the loop
uses is the version that existed when the process started. Nothing about this
is visible in the repository — the diff is right, the tests pass, and the file
on disk agrees with both.
**Fix:** applied 2026-08-18 (loop-02). The loop replaces its own interpreter
(`os.execv`, same pid, same lock) at the next round boundary after a merge that
touched `autoloop/`, having first proved the merged tree imports — see
`docs/AUTOLOOP.md` §3f-quater. **If you hit this shape again, check in this
order:** `.autoloop/pending_upgrade.json` (absent = nothing was offered;
`status` says what became of one that was), then the transcript for
`self_upgrade_pending` / `self_upgrade_boundary` / `self_upgrade_exec` /
`self_upgrade_preflight_failed`. Three states are working-as-intended rather
than bugs — a docs-only merge offers nothing, a merged tree that does not
import is reported and NOT run, and a sha already exec'd for is never retried
(that one-shot is what stops a merge which imports and then dies at runtime
from becoming a restart loop). Plain `run` never replaces itself at all: its
argv carries flags that are not safe to re-run, so it reports and exits 0.
**The generalisable trap:** "the code says X" and "the process is running X"
are different claims, and every long-lived process that can modify its own
source can hold them apart indefinitely.

### The loop dies right after a self-upgrade, refusing a lock held by its own pid
**Symptom:** the transcript shows `self_upgrade_exec`, the console shows the
"restarting into …" line, and the very next thing is
`another autoloop process holds …/LOCK (pid=<this pid> host=<this host> …)`.
The pid it names is the pid that just printed the message, and `ps` confirms
nothing else is running.
**Cause:** the successor could not ADOPT the lock, so it fell through to the
ordinary live-lock refusal — which is correct behaviour, not a bug in the lock.
Adoption needs five things (`autoloop/lock.py`, handoff section): the
`exec_handoff` marker, this hostname, this pid named twice, the lock's own run
id, **and a token matching `AUTOLOOP_EXEC_HANDOFF_TOKEN` in the environment**.
The last one is the one that goes missing in practice, because it is the only
one that has to survive the `os.execv` rather than being read off disk.
**Check, in this order:** `cat .autoloop/LOCK` — no `exec_handoff` key means the
marker was already spent or cleared (a second start of the same image, or an
`execv` that was refused and disarmed); a marker present means the token side
failed. Then check whether the process was launched through anything that
sanitises the environment (a wrapper, a supervisor with a fixed env, a `sudo`
without `-E`) — `os.execv` inherits this process's environment and nothing
else carries the token.
**Recovery is the documented one and nothing special:** the loop is not
running, so the lock is stale in the ordinary sense once the pid is gone —
`python -m autoloop unlock`, then start again. Never hand-edit a token into the
lock file; it would authorize whatever reads it next, which is the property the
token exists to deny.
**What is NOT this:** a refusal *before* any `self_upgrade_exec` entry is a
plain second instance, and the recovery is the same but the diagnosis is not.

### The transcript says `self_upgrade_confirmed`, but the loop is still running the old code
**Symptom:** `self_upgrade_exec` is followed a little later by
`self_upgrade_confirmed` ("one full iteration completed under the merged
code"), `.autoloop/pending_upgrade.json` is gone, and the loop is still
behaving exactly as it did before the merge, and nothing a fresh process prints
on startup appears after the "restarting into …" line.
**Cause:** `os.execv` **can return** — by raising `OSError` (`Exec format
error`, `ENOMEM`, a `sys.executable` that has been replaced mid-run). The
record is flipped to `execed` *before* the call, because it has to be durable
across a replacement that never returns; when the call raises instead, that
status is a lie in the one place that reads it. `_run_continuous` carries on
with the old image, and the top of its next iteration is where
`_confirm_self_upgrade` retires an `execed` record and logs the confirmation —
crediting the old process's own iteration to a replacement that did not happen.
**Fix:** applied 2026-08-18 (loop-02, review round 2). An `execv` that raises
now settles the record to `exec_failed` after disarming the lock handoff, so
the confirmation finds nothing to retire. The one shot is unchanged: the record
has left `pending`, and only `pending` is ever acted on, so the sha is still
spent. Pinned by
`test_a_refused_exec_is_never_confirmed_as_a_replacement_that_happened`.
**How to tell the two apart if you see this shape again:** a genuine upgrade
changes what `python -m autoloop` is running but NOT the pid (`execv` preserves
it), so the pid is no evidence either way — read `self_upgrade_exec_failed` in
the transcript instead, and `status` in `pending_upgrade.json` before it is
cleared.
**The generalisable trap:** a status written before an operation as "this is
about to happen" is read afterwards as "this happened". Any call that can both
not-return and fail needs its optimistic marker settled on the failure path.

### The pytest session vanishes with no report, no failure and no summary line
**Symptom:** a test run ends mid-file. No traceback, no `FAILED`, no counts —
sometimes the output of something else entirely.
**Cause:** a test reached `os.execv`. It does not raise and it does not return:
it replaces the pytest process image, so the session is simply gone. Anything
exercising `cli._self_upgrade_at_boundary` without stubbing the exec does this.
**Fix:** `autoloop/tests/test_self_upgrade.py` has an autouse fixture making
`os.execv` raise `AssertionError`, so a test that reaches a real exec FAILS
instead of ending the run. A test that needs to observe the call installs its
own recorder over it — raising something that is **not** an `OSError`, because
the production code catches `OSError` as "the exec was refused" and would
swallow a sentinel that inherits from it. Same pattern, same reason, as
`test_restart_wiring.py`'s `no_machine_access`.

### An autoloop commit is refused by a test that passes when you re-run it
**Symptom:** post-commit validation refuses a commit with exactly one failing
test out of ~1000. Re-running the identical worker tree passes. Happened three
times before it was tracked down.
**Cause:** `test_crash_safety.py::test_sigint_release_is_unchanged` asserted
`proc.wait(timeout=30)` — that a signalled child had fully exited within a
fixed deadline. On a loaded machine (validation runs while six agents work) it
does not. The lock code was never at fault.
**Why it resisted diagnosis:** it passed 25/25 in isolation and 12/12 as a
whole file. It only failed inside a full-suite run, so any hunt that narrowed
to the file first found nothing.
**Fix:** applied repo-side — the signal tests poll for the LOCK FILE to
disappear (`_await_lock_release`) instead of timing interpreter teardown. The
release happens before the process exits, so polling the artefact tests the
real property with no timing assumption. Both mutations (handler that does not
release; no handler installed) still fail, so the test kept its teeth.
Subprocess holders now also go through a `holder()` context manager that always
reaps the child: **a test that leaks a sleeping process makes its NEIGHBOURS
flaky**, which is the harder failure to trace.
**Hunting a flake like this:** run the full suite in a loop under CPU load and
capture with `--color=no`. pytest's `FAILED` lines begin with an ANSI escape,
so `grep '^FAILED'` silently matches nothing — the same trap that made the
validation gate report a count with no name.
**Recurrence 2026-08-04:** a review round on task rt-06 reported
`test_sigint_release_is_unchanged` failing during that unit's validation. rt-06
changed only `lexy-app/backend/services/document_package/loader.py` and its
test file; the signal tests drive `autoloop/lock.py` through a subprocess whose
script imports `autoloop.lock` and nothing else, so there is no path between
them. If you see this again, re-run it alone (`python3 -m pytest
autoloop/tests/test_crash_safety.py -k sigint`) before suspecting the diff —
and note the mitigation above only removed the *interpreter-teardown* timing
assumption. `_await_lock_release` still has a 60s deadline, so a child that
does not get scheduled to unwind within a minute under load still fails.

### A commit was REFUSED at post-commit review and the blocker does not say which test failed
**Symptom:** a blocker reads `post-commit validation failed: ... pytest ...:
FAIL (1 failed, 992 passed, 1 skipped)` — a count and nothing else, often
wrapped in literal `\x1b[31m` escape codes. There is no way to tell which test
failed, so there is no way to tell a real regression from a flake.
**Cause:** the validation summary kept `output.splitlines()[-1]` — the LAST
line. For pytest that is the count line, and the `FAILED <file>::<test>` lines
sit immediately ABOVE it in the short summary.
**Fix:** applied repo-side — `validation.failure_digest` keeps the naming lines
AND the count, strips ANSI, and bounds the result (visibly, so truncation never
reads as "only 12 failed"). It is redacted through `ValidationEnv` exactly as
before; the digest is wider than the old one-line tail, so that redaction
matters more, not less — there is a test pinning that a password in a failure
message does not reach the summary.
**Diagnosing one on an older build:** re-run the exact tree by hand —
`cd ~/.autoloop/workers/<unit-id> && python3 -m pytest autoloop/tests -q`. If it
passes, the refusal was a flake; the commit is untouched on its branch, since
this gateway cannot reset or roll back.

### `the replacement chat is not inside the configured project: it is still the project page`
**Symptom:** a rotation parks `loop_fatal` claiming the chat id was never
assigned. Open the project afterwards and the chat is right there, holding the
request — one orphan per failure, each with a live request nobody read.
**Cause:** ChatGPT mints `/c/<id>` some time AFTER accepting the first message.
The rotation polled the address bar for `ROTATION_URL_TIMEOUT_SECONDS` (20s)
and treated expiry as proof the chat did not exist. On an account whose
composer needs 180s and whose replies routinely miss a 120s start timeout, 20s
is not a wait — it is a coin toss. Hit three times on 2026-08-03.
**Fix:** applied repo-side — the address bar is now only the FAST path. When
it lags, `BrowserChatGPT.find_conversation_with` reads the project's chat list
newest-first and returns the conversation whose persisted history carries the
request id. The id is in the message, so it identifies the chat without the
URL. The window was also raised to 30s, but that is incidental: what matters
is that expiry is no longer a verdict.
**The guard still holds:** if no chat carries the request, the rotation still
refuses rather than adopting an unrelated one — the same reasoning that makes
an unbound request refuse to guess.
**Diagnosing an older build:** open the project and look for a chat containing
the request id from the park message. If it exists, the rotation worked and
only the detection failed; point `browser.conversation_url` at that chat.

### `the replacement chat is not inside the configured project: 'https://chatgpt.com/c/WEB:<uuid>' is not under ...`
**Symptom:** a rotation parks `loop_fatal` with `rotation_failed`, naming an
address of the form `https://chatgpt.com/c/WEB:<uuid>` — no project prefix on
it at all. The loop stays down until an operator intervenes. Nothing else is
wrong: the project page loads, the composer is clickable, there is no
rate-limit modal, and the replacement chat is sitting in the project holding
the request. Hit 2026-08-16, on the first rotation the new
slow-conversation trigger fired (the retired thread had 90+ packets and a
direct navigation to it timed out, so retiring it was correct).
**Cause:** an ORDERING bug, not selector drift and not an outage. A chat
started from a project page has no durable URL until its first message lands;
until then the address is that `WEB:` placeholder, which is under no project.
The rotation's wait stopped as soon as the address DIFFERED from the project
page, so it handed the placeholder to the membership check — which refused it,
correctly, given what it was shown. The check is right; the moment it was
applied was wrong, and it would have failed on every rotation.
**Confirmed by hand:** open the project page, send one short message, and the
address immediately becomes `https://chatgpt.com/g/g-p-<project>-<slug>/c/<uuid>`,
which passes the SAME check unchanged.
**Fix:** applied repo-side — `_rotate_conversation` now polls for *an address
inside the project* rather than for *any address other than the project page*,
so the placeholder is a state to wait through instead of a verdict. The submit
already in that method is the priming message: one send, never retried, because
a second send from the project page opens a SECOND chat and orphans the first
with the same request live in both.
**What did NOT change:** the membership rule. A replacement genuinely outside
the project is still refused. It is still applied to the address bar only — not
to what `find_conversation_with` returns, whose candidates come back prefix-less
from `urljoin` and would then ALL be refused, undoing the 2026-08-03 rescue (see
`_same_conversation`). The wait is still bounded, and
on expiry the park quotes the address actually observed (and says "placeholder"
when it is one), so the next operator sees the shape rather than a generic
timeout.
**If you are looking at a park like this on an older build:** state still points
at the RETIRED conversation with the request marked submitted against it, so a
plain restart resumes on the dead thread. Open the project, find the chat
carrying the request id from the park message, point `browser.conversation_url`
at it and `reset` (the drift guard requires state and config to agree). Do not
`--resubmit` into a chat that already holds the request — that posts it twice.

### A readback says a message is not in the conversation, and it plainly is
**Symptom:** the loop parks `submission_ambiguous` (or drops a chunked part and
re-sends it) reporting that a readback did not see the request — on builds before
2026-08-16 the park said outright that it "is not in persisted history". Open the
chat and the request is there — on 2026-08-05 `alr-af11e1b3-0006` was there
*and already answered with a decision*. Seeing it took pressing End and
scrolling six times before the tail rendered.
**Cause:** ChatGPT's message list is VIRTUALIZED. Only a window of the
conversation is in the DOM, so `[data-message-author-role]` — the selector
behind `messages()` / `has_request()` — enumerates what is painted, not what
the conversation contains. A DOM read is therefore never a full history read
(`docs/AUTOLOOP.md` §11), and "absent" from an unmounted window is a statement
about the scroll position.
**Fix:** applied repo-side for the by-content search only.
`BrowserChatGPT.find_conversation_with` now mounts the tail before concluding:
it repeats a "go to the end" gesture (`scroll_to_end` when the session offers
it, otherwise the End key) and treats absence as established only when **two
independent things hold** — the session reports the list is AT ITS END, and the
mounted window then stays byte-identical across consecutive reads. Either alone
is a false-absence generator:

* *Count stopped growing* proves nothing at all. A virtualizer may slide a
  constant-size window, mounting newer nodes as it drops older ones, so the
  count reads 6 before and after while six different messages go past. (This
  was the first version of this fix, and it reproduced the very park it was
  written for.)
* *Window stopped changing* is ambiguous. It says the GESTURE stopped mounting
  — which is the tail when the gesture works, and the OPENING window when it
  silently missed. End goes to whatever holds focus, so a misfocused gesture on
  a short or initially stable list gives two identical reads and a confident
  wrong "absent".
* *At the end of the list* is ambiguous too: ChatGPT follows a streaming answer
  down, so the view sits at the bottom while the content underneath it is still
  arriving.

`scroll_to_end` therefore RETURNS a position — True (the scroll container is at
its end; a chat too short to scroll counts), False (more below), None (cannot
measure). `PlaywrightSession` computes it from the actual container by walking
out from the last mounted node. An adapter that answers None — the End-key
fallback included — keeps every SIGHTING it makes and simply cannot establish
absence; it raises `ConversationSearchInconclusive` instead. So does running out
of scrolls. The same read also confirms the page is the conversation it asked
for — a rotation mid-flight moves the shared page, and a confident answer about
a different chat is wrong in both directions.
**Reading the refusal:** the note names each chat with the gestures spent and
why it was not concluded — `still changing at the end of the list` is a long or
streaming conversation (retry), `never reached its end` is a gesture that is not
driving the scroller (a selector or focus problem), and `cannot report a scroll
position` is a session without the signal at all (expected for the End-key
fallback; on a real `PlaywrightSession` it means the measurement kept failing).
**The park it caused now resolves itself (2026-08-16).** `reconcile` still reads
a mounted window, so it can still miss a turn — but a miss no longer ends the
run. Before parking `submission_ambiguous`, the orchestrator runs the search
above (`_resolve_or_park_ambiguous`); if it PROVES the request is in this
request's own conversation, the park is cancelled and the loop resumes into
`awaiting`, sending nothing. Only that direction is automatic. Absence, a hit in a different
chat, a search that refused to conclude, and no `browser.project_url` all park
exactly as before, and the park text says which of them happened — so if you are
reading a `submission_ambiguous` question, start there rather than opening the
chat blind. **Read the note, not just the first sentence.** That sentence now
claims only that reconciliation did not SEE the request in the window it read
back, because a window read cannot establish more; only the note about a search
that read the chats to their end and came back empty is evidence of absence, and
only that one makes `--resubmit` the plausible next move. `run --resubmit` is still the only thing that repeats a send. A
wedged page during the search (`ConversationUnusableError`) parks the same way,
and for its own reason: that error's normal route is a rotation, which POSTS the
request id — and the search reads other chats, so a page that is not even this
request's conversation must never license a repost of it.
**A dead browser during that search is NOT a `submission_ambiguous` park.** A
`SessionLostError` or an ordinary `BrowserError` propagates to `run()` and takes
the normal browser-restart/failure-budget route, so the phase is retried with a
fresh client instead of being reported as evidence uncertainty (a dropped CDP
connection says nothing about what is in the conversation). If you see
`browser_error` with `"phase": "submission_unconfirmed"` and no
`presence_search_inconclusive` beside it, that is this path working — restart the
browser (§ "Browser dead / CDP unreachable") rather than hunting for a lost
message.
**Still open elsewhere:** every OTHER readback (`reconcile`, `has_request`,
`Orchestrator._part_present`) reads what is mounted. That is usually fine —
they check the newest turn — but do not infer "the conversation contains only
X" from a message count anywhere.
**Diagnosing one by hand:** open the chat, press End, and scroll to the bottom
several times before deciding the message is missing. If it appears, the send
landed and only the detection failed.

### The loop vanishes mid-run leaving NO blocker, no park and no heartbeat
**Symptom:** `autoloop health` reports `not_running` with `open_blockers: 0`
and a phase that was healthy moments earlier. Unlike every other stop, nothing
explains itself — no park, no blocker record, and the heartbeat's last status
is whatever the loop was doing rather than `stopped`. Happened twice on
2026-08-03.
**Cause:** a `StateError` propagating out of `Orchestrator.run()`. The one
seen was `request <id> has no conversation binding but this run has already
rotated N time(s)` — raised by `_bind_request_conversation`, which refuses to
attribute an unbound request after a rotation (correctly: pointing it at the
NEW chat would be the wrong repair). But its premise was false. The
`PendingRequest(...)` constructor call omitted `conversation_url`, so every
request was born UNBOUND and only became attributable when something touched
it; a rotation inside that window made the guard fire on a minutes-old
request.
**Where to look:** the run log — `tail ~/.autoloop/start-*.log`. A hard error
prints there and nowhere else, which is exactly why the loop appeared to
vanish. `.autoloop/state.json` then shows `rotations >= 1` alongside a
`pending_request` whose `conversation_url` is empty.
**Fix:** applied repo-side, in two parts. Requests are bound at creation, so
the invariant holds by construction rather than by timing. And a `StateError`
escaping a step now PARKS `loop_fatal` with a durable blocker (`code=
state_inconsistent`) instead of killing the process — a system whose design is
"park with a record so a human can see it" had one path that died without a
trace, and it was the one that bit.

### The dashboard renders only its static markup — pipeline and roadmap are blank
**Symptom:** the page loads, headings and the new-task form show, but every
section built by JavaScript is empty. The API returns correct data, so the
server looks fine and the page looks dead.
**Cause:** a JS syntax error. `PAGE` is a plain (non-raw) Python string, so a
single `\n` written inside a JS string literal is decoded by PYTHON and splits
that literal across two physical lines. The browser reports
`SyntaxError: Invalid or unexpected token`, and one syntax error kills the
WHOLE script — so nothing dynamic renders while static markup still does.
**Diagnosing it:** read the browser console, or
`curl -s localhost:8787/ > /tmp/p.html` and look at the reported line. Checking
the API payload proves nothing: it is served by a different code path and was
correct throughout.
**Fix:** applied repo-side — escapes inside the PAGE script must be DOUBLED
(`split("\\n")`), which the file already documented next to a correct example.
`test_the_served_javascript_actually_parses` now extracts every `<script>`
block and runs `node --check` on it, so this class of bug fails a test instead
of shipping. It found a second broken escape the moment it was written.

### The dashboard says "stopped" while the loop is running, and its task panel is empty
**Symptom:** the header reads `stopped`, the agents list is empty, and the
"Language-app tasks" section shows nothing — all while `autoloop health`
reports EXIT 0 and the loop is demonstrably executing.
**Cause:** three independent bugs in `dashboard.py`, every one of which
reported an ABSENCE rather than an error, which is the worst shape because
nothing looks broken.
1. Liveness matched `pgrep -f "autoloop run --continuous"`. A loop started
   with `autoloop start` never matches: that command calls the run path
   IN-PROCESS, so argv still says `start`.
2. The lock was read as `lock.json`. The file is named `LOCK`, so `lock_pid`
   was always empty and `lock_alive` always false.
3. `app_tasks` parsed a markdown TABLE row (`| rt-01 | P1 | … |`). The auditor
   emits `#### <domain>:<id> — <title>` with severity beneath, so the panel had
   been empty for EVERY report on disk, not merely the newest.
**Fix:** applied repo-side — liveness now reads the LOCK through `LoopLock`,
the same authority `autoloop health` uses, which is also boot-aware (a lock
left by a power cut whose pid has been reused reads stale, not live). `pgrep`
is kept for display only and matches both spellings. The parser handles the
current heading format and still honours the retired table form so an older
report keeps rendering.
**The general lesson:** there were two implementations of "is the loop
running" and the older one was wrong. When a check exists in a command, the
UI should call it rather than keep a private copy.

### `page left the configured conversation while awaiting <request-id>`
**Symptom:** mid-await the loop reports that its page navigated away, often
followed by `Execution context was destroyed` and then repeated
`no assistant response ... within 120.0s`. Nothing visibly navigated anything.
**Cause:** `PlaywrightSession.connect` bound to the FIRST tab whose URL merely
contained `chatgpt.com`. The dedicated profile accumulates strays — a chat a
failed rotation created, something left open by hand — so the loop could
attach to the wrong conversation from the start. The message then describes a
page "leaving" a conversation it was never on. Seen 2026-08-03 with the loop's
Chrome sitting on a third chat while the config named another.
**Fix:** applied repo-side — `connect` takes the configured `conversation_url`
and binds to THAT tab, comparing host+path so a query string or trailing slash
does not defeat the match. With no match it opens its own tab rather than
adopting a stranger's (a new tab in that profile is logged in identically, and
the caller navigates to the conversation anyway). It closes two things and no
others: a tab it opened itself (in `close()`), and — since 2026-08-06 — any
OTHER tab already sitting on the conversation it just bound to, reaped at
connect time. That second case exists because `close()` has to RUN and does not
on an abrupt exit (pause-and-exit, kill, crash, `doctor`, any ad-hoc probe), so
duplicates accumulated in the profile until Chrome was restarted (observed
2026-08-04). A tab on a DIFFERENT chat is still never touched — it may be the
operator reading a past conversation, and closing that would be the mirror of
this bug. Safe only because `~/.autoloop-chrome` is dedicated; never point the
reaper at a human's main browser.
**Diagnosing it:** list the profile's pages and compare against the config —
`curl -s localhost:9222/json | python3 -c "…"` versus
`grep ^conversation_url .autoloop/config.toml`. If they differ, this is it.

### `the replacement chat is not inside the configured project` — but it plainly is
**Symptom:** a rotation parks `loop_fatal` reporting that the chat it just
opened is outside the project, quoting a URL that visibly contains the project
id. Recovering by hand then fails the same way, because the loop is refusing a
chat that is genuinely in the project.
**Cause:** ChatGPT writes the project LANDING page as `/g/g-p-<id>/project`
but its conversations as `/g/g-p-<id>-<slugified-project-name>/c/<chat-id>`.
`Orchestrator._url_in_project` compared `startswith(base + "/c/")`, and the
slug suffix breaks that prefix — so a good chat reads as foreign. The tell
that it was never discriminating good from bad: the SAME check rejected the
conversation the loop had been working in successfully all day.
**What it costs:** the rotation has already created the chat AND posted the
request into it before the check runs, so each failure leaves an orphan chat
holding a live request. Look for those before creating another.
**Fix:** applied repo-side — the check compares path SEGMENTS and allows the
final one to carry a `-<slug>` suffix. The suffix must begin with `-`:
`g-p-abc` may match `g-p-abc-x` but never `g-p-abcdef`, the same
segment-boundary trap bare string prefixes hit in `approved_paths`.
**If you hit it on an older build:** the orphan chat is usable. Point
`browser.conversation_url` at it — it is in the project, it already carries
the request, and being new it also renders far faster than the thread that
triggered the rotation.

### `autoloop pause` parks the loop with `checkout_escape_detected`
**Symptom:** you run `python -m autoloop pause` while a task is running, and
instead of stopping cleanly the loop parks `loop_fatal` with
`created outside the worker repo: .autoloop/PAUSE (file)`. The supported way to
stop the loop breaks it.
**Cause:** the pause flag lived at `state_dir/PAUSE`, i.e. INSIDE the tree
`escape_detector` snapshots around every write-capable agent call. That
enumeration deliberately covers ignored paths — `.autoloop/` is gitignored in
production, and an agent forging `state.json` or a blocker record there is
precisely what the detector exists to catch — so it cannot distinguish the
operator's pause file from an agent's write.
**Fix:** applied repo-side — the flag moved beside `workers_root`, outside the
checkout, the same placement and the same reasoning as `inbox.inbox_dir_for`.
Operator-writable things belong outside the snapshotted tree. **Not** fixed by
exempting the path inside the detector: that would carve a permanent hole in a
security-shaped check for one convenience. A flag left at the old location by
an older build is still honoured on read and cleared by `resume`.
**If you hit it on an older build:** the detection is real but harmless here —
inspect the reported paths, confirm `.autoloop/PAUSE` is the only one, then
archive the session (`reset --yes` keeps the task registry) and close the
record with `archive-blocker <id> --reason "..."`.

### `checkout_escape_detected` naming a `__pycache__/*.pyc` nobody wrote
**Symptom:** the loop parks `loop_fatal` mid-round with
`content changed outside the worker repo: autoloop/__pycache__/orchestrator.cpython-312.pyc`
(or `dashboard.cpython-312.pyc`, or a `created …` line for one). No agent
touched it, no `.py` changed in that window, and `git status` is clean.
Because `checkout_escape_detected` refuses every `answer` by design, and
`archive-blocker` refuses while the session is live, the only way out is
`reset --yes` — which discards the in-flight round. **Three of these on
2026-08-15/16**: one from restarting the read-only dashboard while a round
held the lock (importing `autoloop.dashboard` rewrote its cache entry), two
from a supervisor polling `python3 -m autoloop health --json` every two
minutes (the first poll after the loop merged a source change recompiled
`orchestrator.cpython-312.pyc`). One of those resets left five tasks stranded
`in_progress`.
**Cause:** `__pycache__/` is gitignored, and `escape_detector.
enumerate_checkout_paths` covers ignored paths *on purpose* (the canonical
escape it exists to catch — an agent forging `.autoloop/state.json` — happens
with a clean working tree). So any out-of-band `import autoloop.<x>` against
the primary checkout writes into the snapshotted tree. Note the timing, which
rules out the obvious narrower fix: the recompile fires because the source
changed BEFORE the window (the merge), so "flag a `.pyc` only when its source
did not change in the window" would have flagged all three.
**Fix:** applied repo-side 2026-08-16 (esc-01) — `escape_detector.
is_derived_bytecode` exempts a `.pyc` sitting directly inside a
`__pycache__/` directory, **whose tag is one an interpreter actually emits**,
whose sibling `.py` is in the snapshot **as a regular file**, on every side the
cache entry itself exists. Derived, not
authored: the interpreter writes it from a source that stays fully in scope,
hashed by the same snapshot. Still reported, deliberately: a `.pyc` OUTSIDE
`__pycache__` (the legacy layout, importable with no source); a **`.pyo`
anywhere, `__pycache__` included** — no supported CPython emits that name
(PEP 488 replaced it with the `.opt-N` infix of a `.pyc`), so one is an
authored file borrowing a derived-looking extension; **a `.pyc` whose TAG is
not a real cache tag** (`mod.attacker.pyc`, `mod.cpython312.pyc`) — the tag is
the one part of the name a writer does not choose freely, and accepting any
dot-free tag would have granted a silent write beside every sourced module in
the tree, the same hole as the `.pyo` case one level down the name; an orphan
cache entry
with no sibling `.py`; a cache entry whose sibling `.py` is a SYMLINK (the
snapshot watches a symlink as a target string, never its bytes, so it vouches
for nothing) or whose source is missing on one side of the window; and any
symlink/directory appearing at a cache path. Contrast the `autoloop pause`
entry above, where exempting the path was the WRONG fix — `PAUSE` is
authored, its bytes are the only copy of the claim, and it moved outside the
tree instead.
**Which tags count:** `cpython-<digits>[t]` (the CPython family shape, `t`
being the free-threaded ABI flag) plus this runtime's own
`sys.implementation.cache_tag` as a literal. The family shape is the
load-bearing half, not the runtime tag: all three incidents were written by a
*different* process than the loop (a dashboard restart, `health --json`
polls), which need not run the loop's interpreter or even its version — pinning
to `sys.implementation.cache_tag` alone would reproduce the parks the first
time someone polls with another `python3`. A cache file from a genuine but
foreign interpreter (a `pypy39` tag, say) is still reported; that is a
readable spurious park rather than a silent write, and nothing imports this
checkout under one today.
**Also fixed by the same rule:** the validation mutation guard
(`diff_worker_tree`), which brackets the post-commit validation run and would
otherwise read every `.pyc` a `pytest` run compiles as "validation MUTATED the
worker tree". **Not all the way, though** — if you see that refusal naming
something like `tests/__pycache__/test_x.cpython-312-pytest-8.3.4.pyc`, that
is this same class hitting a stated limit, not a new bug: pytest's assertion
rewriter interposes its own version into the TAG position, so the name does
not carry a tag any interpreter emits and is reported. Ordinary
`<mod>.cpython-3XX[.opt-N].pyc` names are covered. Run validation with `-B` /
`PYTHONDONTWRITEBYTECODE=1` to avoid it; loosening the tag rule to cover it
was deliberately not done — a tag pattern wide enough for
`cpython-312-pytest-8.3.4` is wide enough for `attacker`.
**Still worth doing on the operator side:** run out-of-band autoloop commands
with `python3 -B` / `PYTHONDONTWRITEBYTECODE=1`. Per the esc-01 brief this was
applied as a stopgap to the loop, supervisor, deadman and dashboard
*wrappers*, which live outside this checkout — `rg -n
'PYTHONDONTWRITEBYTECODE|python3? -B' autoloop -g '*.py'` comes back empty
(only docs prose, including this entry, mentions the flag), so treat it as
reported, not as verified from this repository. Either way it is a stopgap and
not the fix: it depends on every future caller remembering, which is exactly
what failed three times here.
**If you hit it on an older build:** inspect the reported paths, confirm they
are all bytecode, then `reset --yes` (it keeps the task registry) and re-open
any task the reset left `in_progress`.

### The loop runs forever without progressing — same `audit` decision, same park, every cycle
**Symptom:** `run --continuous` is alive and healthy (no crash, no blocker you
can act on), but the transcript repeats one cycle: `directive {"decision":
"audit"}` → `needs_user {"task_id": "audit-00NN"}` → `request_prepared` →
`request_submitted`. Every pass costs a full ChatGPT round trip. `iteration`
never advances.
**Cause:** two things compounding.
1. The audit unit id is `audit-<iteration>`, **not unique per attempt** — and
   an audit that parks never advances the iteration, so each retry re-mints
   the *same* id and finds the same stale `.autoloop/executions/<id>.json`.
2. `decision=audit` skips `authorize_directive`'s `_check_task_reference` (it
   is a pseudo-task, not a registry entry), so nothing asked whether that id
   was already quarantined. The park re-quarantined an already-quarantined
   unit and told ChatGPT nothing, so it chose `audit` again.
**Fix:** applied repo-side — `_resolve_audit_task` refuses a quarantined unit
via `_handle_policy_denial` rather than parking. A denial re-prompts with the
reason (so the next directive can name a real task) and is bounded by
`check_denial_budget`, so a genuinely stuck loop stops instead of spinning.
**Clearing one that is already stuck:** stop the loop, archive
`.autoloop/executions/<unit-id>.json` (a move, keep the `.bak-`), then
`autoloop answer <blocker-id> "..."` to release the quarantine. Check first
that the reviewed candidate survives — `git -C ~/.autoloop/workers/<unit-id>
branch --contains <candidate-sha>` — since archiving the record does not touch
the branch, and that branch is the only copy.

### The loop collects the same reviewer `stop` every few minutes and every automated signal stays green
**Symptom:** `run --continuous` is alive, `health` reports `code: running`,
`open_blockers: 0`, `needs_attention: FALSE`, and the heartbeat keeps arriving —
but the transcript repeats one cycle: new session → kickoff → `stopped` →
new session. The reviewer's reasons are worded differently each time while
describing one situation. `phase` never moves off that cycle. Measured
2026-08-20: three full rounds in fifteen minutes, one ChatGPT turn each, and it
would have run for as long as the process did.
**Cause:** a `stop` is a VERDICT, not a failure, so nothing counted it.
`policy.max_consecutive_failures` never sees one; `stop` ends the session,
`--continuous` correctly treats a contract stop as a clean boundary and opens
the next one, and the new session's kickoff draws the same refusal. The
reviewer is right every time — the fault was on the controller side (in that
incident, a lost postcommit binding left a task holding an approved,
unpublishable candidate), which is exactly what no counter was watching.
**Fix:** applied repo-side (stop-01) — `orchestrator._handle_contract_stop`
charges every stop to `.autoloop/stop_repetition.json`, keyed by a fingerprint
of the SITUATION (task id, execution records, registry) rather than the reason
text, and the third consecutive stop about an unchanged situation parks
`loop_fatal` with `code="stop_livelock"`, quoting the reviewer's last reason
verbatim. `docs/AUTOLOOP.md` §9f. One stop, and stops about different
situations, behave exactly as before.
**Clearing one that is already parked:** read the blocker's question first — it
carries the reviewer's own words, and in this incident that text named the
controller's fault precisely. Fix what it names, `autoloop answer <id> "..."`,
then `run --answer "..."` (WITHOUT `--continuous`) to resume the parked session.
**If it parked `stop_repetition_ledger_unusable` instead:** the counter file
could not be read or written, so the check could not run and the loop refused to
carry on with it silently off. Delete `.autoloop/stop_repetition.json` — it is a
counter, nothing else reads it — then answer and resume.

### `Error: It looks like you are using Playwright Sync API inside the asyncio loop.` — `run --continuous` dies after a park
**Symptom:** the loop runs fine, parks or hits a browser error, prepares the
next request, and the whole process dies with this traceback ending at
`playwright_session.py` → `sync_playwright().start()`. Deterministic: it
happens on the SECOND browser client the process builds.
**Cause:** not asyncio in autoloop — there is none. Playwright's sync API
drives an event loop of its own, and `sync_playwright().start()` refuses when
another driver is **already running in the thread**. Stop-then-start is fine;
alive-then-start is not (both verified against a live Chrome). So a *leaked*
driver was fatal, and leaking one was easy: `PlaywrightSession.close()`
swallowed a failed `stop()`, and `Orchestrator._drop_client` swallows a failed
`close()` and drops the reference anyway. Tearing down a session whose
connection had already broken — precisely what happens after a browser error —
left a live driver with nothing pointing at it, and the next `connect()` took
the loop down.
**Why it reads as a browser problem:** it is not. Restarting Chrome does not
help, the CDP endpoint is healthy, and the conversation loads by hand.
**Fix:** applied repo-side — `playwright_session._driver()` holds ONE driver
per process, started lazily and never stopped, and `close()` drops only the
CDP connection (`browser.close()`, which leaves the human's Chrome running:
same pid, CDP still answering). **Do not "tidy" `close()` back into stopping
the driver** — that recreates the bug for every session that follows.
**Reproducing it, if you need to:** start a driver, connect, then call
`sync_playwright().start()` again WITHOUT stopping the first. A probe that
stops before starting will not reproduce it and will mislead you.

### `the conversation is unusable and conversation rotation budget exhausted (1 per run)` — and `run --retry` never clears it
**Symptom:** the loop parks `loop_fatal` with `rotation_cap_reached`. Every
`run --retry` parks again with the identical message, quoting the same stale
error. Opening the conversation URL by hand works fine.
**Cause:** two separate things, and the message named neither.
1. `state.rotations` is checked against a cap described everywhere as "per
   run", but it lives in the state file, which outlives the process — so it
   was really per *session*. One transport failure (a dropped network, a
   browser that died mid-navigation) spent it permanently.
2. The park text blamed the chat and suggested raising
   `policy.max_conversation_rotations`. Both wrong: the chat was fine, and no
   rotation was needed at all.
**How to tell what actually happened:** read the transcript, not the park
message — `python3 -c "…"` over `.autoloop/transcript.jsonl`, last ~10 rows.
In the 2026-08-02 incident the fresh error was `no assistant response to
alr-…-0004 began within 120.0s`, i.e. the loop was **waiting for a reply to a
request that was never posted** (confirmed by reading the conversation: it
contained only `-0002`). `--retry` resumes into `awaiting`, so it waits another
120 s and parks forever.
**Fix:** applied repo-side — `cli._reset_run_scoped_budgets` zeroes the budget
once per process, and the park message now says so and warns about the
never-posted-request case. **Do not move that reset into `_build_orchestrator`**:
`_run_continuous` rebuilds the orchestrator every iteration, so it would refill
the budget between rotations and remove the cap entirely.
**If you are stuck on a build that predates the fix:** archive the session
state ONLY — `StateStore(config.state_file).archive()` — and start a new run.
Do **not** use `reset --yes` on such a build; see the next entry.

### `reset --yes` silently archived the whole task registry
**Symptom:** you ran `reset --yes` to clear a wedged session and the roadmap
came back with only the seed task. Imported audit findings, operator-set
priorities and quarantine decisions all gone.
**Cause:** `_cmd_reset` archived `tasks.json` alongside `state.json`,
unprompted, announced by one line of output after it had already happened. The
confirmation prompt said "archives the current session state" and did not
mention the registry at all.
**Fix:** applied repo-side — the default now archives the session only and
prints `task registry kept`; `--tasks` is the opt-in. **Recovery either way:**
both are moves, never deletions. `ls .autoloop/tasks.json.bak-*` and rename the
newest back into place.

### `refusing to remove a LIVE lock (pid=… )` from `unlock`, after a reboot
**Symptom:** the machine was restarted (or power-cut) while `run --continuous`
was going. `python -m autoloop run` reports the state dir is locked; the
documented recovery, `python -m autoloop unlock`, then refuses — "refusing to
remove a LIVE lock. Stop that process instead." There is no process to stop:
the pid belongs to something unrelated that booted afterwards.
**Cause:** `LoopLock.is_live` decided staleness purely by `os.kill(pid, 0)`.
**Pids are reassigned across a reboot**, so the recorded pid can be handed to
any other process, and the probe then says "alive". Both commands were behaving
correctly on false evidence, which is why it reads as a dead end.
**Fix:** applied repo-side — `lock.py` compares the lock's `started_at` against
the machine's boot time (`kern.boottime` / `/proc/stat` `btime`) BEFORE probing
the pid; anything written before this boot is stale regardless. **Do not
"simplify" that to a monotonic clock**: macOS's `CLOCK_MONOTONIC` stops during
sleep, so a slept laptop would compute a boot time far too recent and could
declare a LIVE lock stale — the one direction this must never fail in. If you
hit this on a build that predates the fix, delete `.autoloop/LOCK` by hand
after confirming with `pgrep -f 'autoloop run'` that nothing is running.

### The loop leaves a stale lock on shutdown but NOT on Ctrl-C
**Symptom:** stopping with Ctrl-C is clean; a reboot, logout, or plain `kill`
leaves `.autoloop/LOCK` behind and the next `run` refuses to start.
**Cause:** it is backwards from how it looks. Ctrl-C raises
`KeyboardInterrupt`, which unwinds and runs `LoopLock`'s context-manager exit.
Python's default action for **SIGTERM and SIGHUP is to die without running
`finally`**, so the orderly-looking ways to stop skipped the release entirely.
**Fix:** applied repo-side — `LoopLock.acquire` installs handlers for both and
`release` restores them, so **every** lock holder gets it (`run` is the long
one, but `smoke-browser` drives a browser and `review-changeset` waits on a
reviewer; a per-command wrapper would only cover whichever ones somebody
remembered). The release happens **inside the handler**, before unwinding, and
that placement is load-bearing: a SIGTERM mid-fan-out unwinds
into `ThreadPoolExecutor.shutdown(wait=True)`, which waits on agents that run
for minutes, while a shutdown's grace period is seconds. A version that only
raised `SystemExit` and let the `with` block release looks identical in a quick
test and fails exactly when it matters — see
`test_lock_is_released_before_unwinding_not_by_it`.

### `TypeError: ImplementExecutor.__init__() got an unexpected keyword argument 'task_inbox'` at `run` startup
**Symptom:** every unit test passes, `ruff` is clean, and `python -m autoloop
run` dies immediately in `cli._build_executor`.
**Cause:** a new keyword was inserted by matching an anchor line that is NOT
unique. `validation_env=validation_env,` appears in BOTH the `ImplementExecutor`
and the `Orchestrator` construction in `cli.py`, so a "insert after the first
occurrence" edit landed the argument on the wrong constructor.
**Why nothing caught it:** every orchestrator test builds `Orchestrator(...)`
directly with its own collaborators. Nothing exercised `_build_orchestrator` /
`_build_executor`, so the real startup wiring had no coverage at all — a whole
seam only production ran.
**Fix:** `test_task_inbox.py::test_the_cli_actually_builds_an_orchestrator`
builds the real collaborator set against a throwaway repo (with an `origin`,
which the publisher provisioning needs). No browser, no agent, no network —
construction is the whole assertion, and that is enough to catch a misplaced
keyword. **When patching by anchor, assert the anchor is unique** (`s.count(a)
== 1`) before replacing; a non-unique anchor silently edits the wrong place.

### The loop asks you to unset `ANTHROPIC_API_KEY` — a variable that is not set anywhere
**Symptom:** a task parks with `ask_user`: *"Please unset ANTHROPIC_API_KEY and
any other external Claude authentication variables in the executor
environment."* Checking finds it unset in the environment, absent from every
shell profile, and absent from `~/.claude/settings.json`.
**Cause:** the loop's subagents run NESTED inside a Claude Code session, so
they inherit its auth context (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, …). The
CLI decides "another auth source" is present, disables claude.ai **connectors**
(an advisory — authentication itself is fine), and prints that banner to
stderr *first*. `ClaudeCliRunner.run` then captured `stderr[:2000]` — the
HEAD — so on any non-zero exit the banner became the entire reported cause. It
propagated into the executor summary, the review packet, and back out as a
directive to fix a variable that was never set, while the real failure was
never shown at all.
**Fix:** `agents.summarize_failure` (2026-08-01) drops advisory lines
(`BENIGN_STDERR_MARKERS`) from the reported cause, keeps BOTH head and tail of
long output (a traceback puts its cause LAST; a banner puts itself first), and
when the output is *only* advisory says `non-zero exit (N) with NO diagnostic
output` rather than presenting a warning as the cause.
**The general trap:** this is the second time a warning at the edge of captured
output masqueraded as a failure — once as a TAIL (the validation summary
below), once as a HEAD. When output is truncated for a summary, the truncation
itself decides what looks like the cause. Keep both ends, and never let a
warning be the whole answer.

### Validation reports a `pytest` failure whose tail is a warning, and the real error is `InvalidPasswordError`
**Symptom:** a task parked four times with
`ruff check .: PASS; python3 -m pytest -n auto -q: FAIL (warner(PytestBenchmarkWarning(text)))`.
The same command passes in the primary checkout (`1258 passed, 2 skipped`).
Inside the worker repo it produced **1197 errors**, all
`asyncpg.exceptions.InvalidPasswordError: password authentication failed`.
**Cause:** two separate things. (1) A worker repo is a fresh clone and `.env`
is gitignored, so `DB_*`/`SECRET_KEY` are absent and every DB-backed test
fails to authenticate — the boundary in §4g of `docs/AUTOLOOP.md` exists for
exactly this. (2) `run_validation_commands` summarises a failure with the
**last line** of combined output, which for pytest is often a warnings-summary
line rather than the error. The tail is a pointer, never the diagnosis.
**Fix:** configure `[paths].validation_env_file` (see
`autoloop/config.example.toml`). When triaging any validation FAIL, re-run the
command by hand in the worker repo rather than reading the tail — the tail
told us "benchmark warning" for a credentials problem.

### Backend tests that pass on the dev database fail on a fresh one: `assert 'word' in {'phrase'}`, `assert False` in the match tests
**Symptom:** on a newly built database the suite is green except a handful of
`match_learning_words` tests. Seen: `test_match_word_and_phrase_returned_together`
(`assert 'word' in {'phrase'}`), `test_match_returns_learning_phrase_by_surface`
and `test_match_learning_words_matches_phrases` (`assert False`).
**Cause:** the helpers pick an arbitrary row out of a SHARED catalog table and
the pick is only accidentally correct on the dev database.
  * `_get_phrase` does `SELECT … FROM phrase_table WHERE language=$1 LIMIT 1`
    with **no ORDER BY**, so it takes whatever is physically first. On a fresh
    database that was a leaked `_testphrase_<hex>` row from an earlier run —
    a synthetic surface the spaCy matcher can never match.
  * `_get_word` does `ORDER BY word_id LIMIT 1` filtered only by
    `word !~ '[0-9_]'` — **no language filter** — while the test then matches
    against the PHRASE's language (`de`). On a fresh database the only rows
    were Spanish (`hola`, `gato`), so the word could never match.
On the dev database row 1 happens to be a real German word/phrase, so both pass
and the dependency stays invisible. `_get_word`'s own comment shows this was
already patched once (for digit/underscore fixture pollution); the language
dimension was missed.
**Fix (landed 2026-08-01, `test_free_chat_progression.py`):** `_get_word` takes
an optional `language` and pins the pick to it — the failing call site passes
the phrase's language, which is what the match actually runs in; the default
`None` leaves the other three call sites byte-identical. `_get_phrase` gained
`ORDER BY phrase_id` and `canonical !~ '^_testphrase'`. Verified it FIXES
rather than mutes: with no German word the test now skips with a clear reason,
and with one German word present it PASSES — a skip alone would have proven
nothing. The equivalent helpers in the other ~13 test files were left alone;
they carry the same latent risk and are worth the same treatment. This is the
same failure class as the xdist
isolation cluster in `docs/TESTS.md`: **any `… FROM <shared catalog> LIMIT 1`
without an ORDER BY and without a filter that pins what you actually need is a
latent bug**, whether the disturbance is a parallel worker or a different
database.
**Note:** leaked fixture rows are the other half of this. `phrase_table` /
`word_table` are not user-scoped, so the autouse cleanup (which reaps by test
user) cannot reap them, and they accumulate. On a fresh database they are the
FIRST rows, which is why they dominate an unordered pick.

### `relation "video" does not exist` on a fresh `alembic upgrade head` — and the schema cannot be rebuilt any other way either
**Symptom:** building a clean database and running `alembic upgrade head` dies
at migration 006 (`ALTER TABLE video ADD COLUMN … channel_id`). The chain
creates 23 tables and never creates the content core — `video`, `word_table`,
`sentence`, `language_table`, `word_to_sentence`, `video_category`,
`phrase_blueprint`.
**Cause:** there are TWO schema sources and neither is complete.
  1. **Migrations** build the app half (users, srs_cards, llm_cache, book_*,
     chat_*, notification, …) and *alter* content tables they never create.
  2. A **`table.sql`** at the repo root built the content half. It was deleted
     in `25a194d`; recover it with `git show <sha-before>:table.sql`.
They cannot simply be composed. `table.sql` is a LATE snapshot — its `video`
already has `channel_id` and `fk_video_channel → channel(id)`, i.e. the state
*after* the 013–022 channel surrogate-key refactor — so replaying the chain
over it fails with `constraint "fk_video_category" for relation "video"
already exists`. And there is no single revision to `alembic stamp` past,
because the chain interleaves content-table alterations with app-table
creation, so any stamp that skips the conflicts also skips tables you need.
**Fix:** for a throwaway/CI/test database, copy the schema from a database
that already works — it is the only source that is provably current:
```bash
pg_dump -h <host> -p <port> -U <user> -d <live-db> \
  --schema-only --no-owner --no-privileges | psql -d <new-db>
```
**Worth fixing properly:** a baseline migration that creates the content core,
so the chain is self-sufficient. Until then every fresh deploy, CI database and
new-contributor setup hits this wall. `docs/AUDIT_2026-07-30.md` proposed
exactly this experiment as a validation step; running it confirmed the finding
and showed it is worse than "migrations are incomplete" — the two halves have
drifted into each other.

### `assert b'validation_user' in b'\x00\x00\x00\x08\x04\xd2\x16/'` — a test listener sees 8 bytes instead of a Postgres startup packet
**Symptom:** a test that opens a loopback listener and asserts the Postgres
startup packet carries the expected user/database receives only
`b"\x00\x00\x00\x08\x04\xd2\x16\x2f"`.
**Cause:** that IS the whole first message — asyncpg sends an **SSLRequest**
(length 8, body 80877103 = `0x04d2162f`) and waits for a single-byte reply
before sending the startup packet that actually contains `user`/`database`. A
listener that reads once and closes never sees the credentials.
**Fix:** answer the SSLRequest with `b"N"` (TLS declined), then `recv` again.
Applied in `autoloop/tests/test_validation_env.py`
(`test_validation_subprocess_delivers_credentials_to_a_real_db_client`), with
the byte constant named and commented so it is not mistaken for junk.

### `RuntimeError: SECRET_KEY is not set in .env` during `pytest --collect-only` in a clean clone
**Symptom:** six backend test modules ERROR at collection in a fresh clone;
`1141 tests collected, 6 errors`. Adding a dummy `SECRET_KEY` alone collects
all 1260.
**Cause:** `core/security.py:11` reads `SECRET_KEY` at **import** and raises
when unset. Useful as a tool, not just a trap: it is the cheapest way to
enumerate exactly which environment variables the suite needs, without a
database and without reading `.env`.
**Fix / note:** the variable is `SECRET_KEY`, **not** `JWT_SECRET_KEY`. A
brief, a doc, or a memory that says `JWT_SECRET_KEY` is wrong — check
`core/security.py` before propagating either name. The autoloop validation
allowlist deliberately does not accept an alias.

### A blocker's "recheck the condition" precondition is narrower than what actually fired the park, so it clears on nothing
**Symptom:** `cli._RESOLUTION_PRECONDITIONS["checkout_escape_detected"]` was
first fixed by pointing it at the SAME function already used for
`primary_checkout_dirty` (`_precondition_checkout_clean`, which re-runs
`GitGateway.is_dirty()`) — plausible, since both parks are about the
primary checkout's state, and the fix passed every test written for it at
the time. A second review caught that this clears the blocker for the
exact scenario the detector exists to catch: an agent tampering with
`.autoloop/state.json` (gitignored in production) leaves `is_dirty()`
reporting a clean tree throughout.
**Cause:** the park (`escape_detector`'s before/after snapshot) and the
recheck (`is_dirty()`) were built on DIFFERENT path enumerations —
`enumerate_checkout_paths` deliberately covers tracked + untracked +
IGNORED paths (see finding #2's own design and
`test_escape_detector_detects_ignored_content_change`, which asserts
`git.dirty_files() == []` even while the detector correctly flags ignored-
path tampering — that assertion was sitting in the same test file the
whole time and named the exact mismatch). A precondition that reuses an
existing recheck function without checking whether that function's
COVERAGE matches the condition that fired the specific park will pass
trivially whenever the two diverge, and "the checkout looks clean" is a
condition an agent tampering with a gitignored path can satisfy by doing
nothing at all.
**Fix:** `checkout_escape_detected` now has its own
`_precondition_checkout_escape_detected`, which refuses UNCONDITIONALLY —
mirroring `_precondition_protected`'s shape — rather than attempting any
automated recheck. Lesson for the next one of these: before pointing a new
precondition at an EXISTING recheck function, verify the function's
coverage actually matches what can trigger the park, not just that both
happen to mention "the checkout." When a detector's whole design point is
seeing MORE than an ordinary git status/diff would (as `escape_detector`'s
is), no git-status-shaped recheck can be trusted to re-verify it — refusing
unconditionally is the honest fallback, not a lesser fix.

### End-to-end orchestrator test fails with `primary_checkout_dirty` for a reason that has nothing to do with what the test checks
**Symptom:** every end-to-end `test_m1_hardening.py` test built around a real
git repo parked `loop_fatal` / `primary_checkout_dirty` (or, in one case,
`park_kind == "loop_fatal"` where the test expected `"task_fatal"`) the moment
`orch._dispatch_executor(...)` ran — before the scenario under test (escape
detection, path-ownership, quarantine, ...) ever got a chance to fire.
**Cause:** this pass added a precondition that the PRIMARY checkout must be
clean before any write-capable agent invocation (`_prepare_write_capable_
worker`, M1 finding #2). `Orchestrator.__init__`/`StateStore.save(state)`
writes `.al/state.json` into the repo root as its very first act; the real
production repo's `.gitignore` excludes `.autoloop/` (and this test suite's
`state_dir` is set to `.al`, not `.autoloop`), but the test helper's
throwaway `real_repo()` fixture never replicated any gitignore rule at all —
so that write showed up as an untracked, dirty path and tripped the new
precondition for every single test, regardless of what it was trying to
exercise.
**Fix:** `_build_worker_repos_orchestrator` (the shared fixture for this
test file) commits a `.gitignore` containing `.al/\n` as its first step,
before constructing anything. Any new end-to-end orchestrator test built on
a real git repo needs the same line — grep `_build_worker_repos_orchestrator`
for the exact pattern rather than re-deriving it.

### A test tries to prove path-ownership enforcement via a commit `pre-commit` hook and gets refused before the scenario even starts
**Symptom:** installing a `pre-commit` hook into a `WorkerRepoManager`
worker's controlled hooks directory, then dispatching, refused immediately
with `worker_isolation_violation` — the hook-based scenario the test was
trying to model (an unexpected path landing in a commit via a hook) never
ran at all.
**Cause:** `verify_worker_isolation` refuses ANY active hook found in a
worker's hooks directory, unconditionally, as a blanket isolation
guarantee — it does not distinguish "a malicious hook" from "a test fixture
installing one on purpose." `WorkerRepoManager.create` separately refuses
outright if the hooks directory is non-empty at creation time, so a hook
cannot even be pre-staged before the worker repo exists.
**Fix:** hooks are categorically impossible to exercise on the
`worker_repos` path — this is intentional, not a gap (see
`test_worker_isolation_refuses_any_active_hook_before_anything_else_runs`,
which asserts exactly this as a positive control). To model "an unexpected
path lands in a real commit some OTHER way", use the crash-recovery path
instead: commit both an approved and unapproved path directly via
`worker_git.commit_and_capture(...)`, leave `candidate_sha` unpersisted
(simulating a crash), and let the orchestrator's crash-recovery
(`reconcile_after_crash` / `CommitIntent`) adopt the commit as `RECOVERABLE`
— the post-commit path-ownership comparison still SEES it from there. See
`test_unexpected_commit_path_from_a_prior_process_is_recorded_on_adoption`.
(Since 2026-08-05 that comparison is ADVISORY: it records the path on
`TaskExecution.out_of_scope_paths` and the round proceeds to review instead
of parking, so assert on the record, not on a park.)

### `WorkerRepoManager.create()` raises `GitCommandError` fetching a `candidate_sha` after quarantine-and-recreate, even though the sha is real and was committed moments earlier
**Symptom:** a round that already committed successfully (`candidate_sha`
set), followed by a LATER round that fails validation and leaves residue in
the same worker repo, then a THIRD round whose dispatch quarantines that
dirty repo and tries to recreate — the recreate's `git fetch -q <source>
<candidate_sha>` fails with `src refspec <sha> does not match any`, even
though `candidate_sha` is a real commit that genuinely exists on disk.
**Cause:** `_prepare_write_capable_worker`'s quarantine-and-recreate branch
always passed `self._git.repo_root` (the PRIMARY checkout) as the fetch
source, regardless of whether it was resuming from `execution.task_base_sha`
(which the primary checkout always has) or `execution.candidate_sha` (a
commit made INSIDE the worker repo's own, separate git object database —
`WorkerRepoManager` creates a real, standalone `git init` repo per task,
never a linked worktree, so nothing about that commit is ever pushed or
otherwise made visible to the primary checkout). Fetching an object the
source repo never had was always going to fail; `test_failed_attempt_
residue_absent_from_the_next_candidate` didn't catch this because its
quarantine happens BEFORE any commit, so it only ever exercised the
`task_base_sha` branch.
**Fix:** the fetch source now depends on which sha is being resumed —
`execution.candidate_sha` (when set) fetches from the just-quarantined
directory (`WorkerRepoManager.quarantine` MOVES the repo, never deletes it,
so the object is still there and reachable via a local-path fetch);
`execution.task_base_sha` still fetches from `self._git.repo_root` as
before. See `orchestrator.py`'s `_prepare_write_capable_worker` and the
regression test `test_quarantine_recreate_resumes_from_a_candidate_sha_
that_only_exists_in_the_quarantined_repo`. (Since wrk-01, 2026-08-18, a
dispatch whose recorded worker passes the resumed-worker reuse gate no
longer quarantines it at all — the worker is resumed as it stands — so the
regression test now exercises this branch by calling
`_prepare_write_capable_worker` directly with the reuse flag left False;
the branch and its fetch-source fix are unchanged.)

### A test for a "generic exception" handler passes against code that has no generic handler
**Symptom:** a test written to prove `except Exception` was added — a stub
raising `OSError(errno.ENOENT, "no such file")` — asserts the new generic
error wording and fails, reporting `agent command not found: [Errno 2] …`
instead. Deleting the new handler does not change the result: the case never
reached it. (Hit on `ClaudeCliRunner.run`, `autoloop/audit/agents.py`.)
**Cause:** `OSError.__new__` dispatches on errno at CONSTRUCTION time. The
two-or-more-argument form returns an errno-specific subclass, not an
`OSError` — so `OSError(errno.ENOENT, …)` *is* a `FileNotFoundError` and is
caught by the pre-existing dedicated branch above the broad one. The
single-argument form (`OSError("boom")`) is never specialized, and other
errnos map elsewhere (`EACCES` → `PermissionError`).
**Fix:** build generic cases as single-argument `OSError`, or with an errno
that maps to something other than the type already handled. Then pin it: add
a control assertion that the *dedicated* message is absent from the result
(`assert "command not found" not in result.error`) so a case that silently
specializes fails loudly instead of passing for the wrong reason, plus one
test asserting the specialization itself — see
`test_every_generic_case_really_misses_the_dedicated_branch` in
`autoloop/tests/test_audit_agents.py`. The same trap applies to any
`except`-ordering test: `FileNotFoundError`, `PermissionError`,
`IsADirectoryError` and friends are all `OSError` subclasses, so a broad
clause placed above them swallows the specific one.

### A task parks on `review_feedback_unchanged` after the reviewer asked twice for a file to be REMOVED
**Symptom:** the reviewer's feedback is a removal ("`autoloop/obsolete.py` must
be absent from the candidate, not committed as a zero-byte addition"), the
executor runs a full round, changes everything else the review asked for, and
leaves the file. The next review repeats the same sentence verbatim, the
convergence guard fires (`task_fatal`, code `review_feedback_unchanged`), and
the task parks — usually with its actual implementation already accepted, which
is what makes this read as a mysterious park rather than a failure. Observed on
roadmap-01, 2026-08-18, after 8 rounds.
**Cause:** two separate things, and only one of them is scope. First, the
write-capable agent has NO way to delete a file: `WRITE_ALLOWED_TOOLS` is
Read/Grep/Glob/Edit/Write and `Bash` is disallowed, so the closest it can get to
"remove this" is `Write`-ing it empty — which is exactly the zero-byte addition
the reviewer objected to. Second, if the file is out of scope, `approved_paths`
does not name it either, so nothing in the round is working towards its removal.
**Fix (since 2026-08-19, task `scope-04`):** for a path the LOOP recorded in
`TaskExecution.out_of_scope_paths`, the agent asks and the executor unlinks —
write `REMOVE-OUT-OF-SCOPE: <path>` at the start of a line, copying the path
exactly as the prompt lists it. Nothing else is needed and nothing else works: a
path that is not in that record is ignored (and the round summary says so), the
grant is DELETION only, and no scope is widened. See `docs/AUTOLOOP.md` §4e's
2026-08-19 amendment.
**Still unfixed — check this before assuming the above applies:** a file INSIDE
`approved_paths` still cannot be deleted, because the missing piece there is the
agent's tool set rather than its authorization, and `REMOVE-OUT-OF-SCOPE:` will
refuse it as never recorded. roadmap-01's second file,
`autoloop/tests/test_obsolete.py`, was in scope and is exactly this case. Today
that removal needs an operator.

### The same park, but the reviewer asked for an EDIT to be UNDONE
**Symptom:** identical to the entry above — repeated feedback, the convergence
guard fires, the task parks — except the residue is a file that ALREADY EXISTED
and was merely modified out of scope, so there is nothing to delete. Observed on
port-01, 2026-08-20: ten edited files, zero creations, and because a revise
builds on the same branch the contaminated set was handed to every following
round unchanged. 8 commits, 11 attempts, 6 review rounds, branch discarded by
hand.
**Cause:** `REMOVE-OUT-OF-SCOPE:` deletes; it has no "put it back" form, and
deleting a file the base commit contains would be a second and worse overrun.
The agent cannot restore it either — it has no `Bash`, and reconstructing the
original from memory is a new edit, not a revert.
**Fix (since 2026-08-24, task `scope-05`):** write
`REVERT-OUT-OF-SCOPE: <path>`, same anchoring rules and same recorded-paths-only
authority, and the executor restores the file from `TaskExecution.task_base_sha`
— git's copy, not yours, so do not try to reconstruct the content. A path that
did not exist at the base has nothing to restore and is made absent instead;
naming one path under BOTH forms removes it and reports the revert superseded.
**Check the prompt before you rely on this.** Production wires it
(`cli._build_orchestrator` passes
`revert_authority=RecordedRevertAuthority(execution_store)`), but the form is
still rendered only when this round has a recorded path AND a usable base sha —
so if the prompt you were given does NOT list `REVERT-OUT-OF-SCOPE:` among the
request forms, the line does nothing for you: no execution record, no base sha
on it, or an embedder that wired no authority. Say so in the report rather than
retrying the line. `docs/AUTOLOOP.md` §4e's 2026-08-24 amendment has the rest.

### `archive-blocker` refuses with a lock error and archives nothing
**Symptom:** `python -m autoloop archive-blocker blk-xxx-001 --reason "..."`
prints `error: another autoloop process holds …/LOCK` or `error: stale lock at
…/LOCK … recover with: python -m autoloop unlock`, followed by
`blocker blk-xxx-001 was NOT archived — nothing changed`, and exits 1. The
command used to work whatever the lock said.
**Cause:** not a bug — the refusal is the fix for one (blk-01, 2026-08-21).
Archiving a blocker can close the LAST open record naming a quarantined task,
and that task then has to return to the queue in the same operation
(`docs/AUTOLOOP.md` §9c), which writes `.autoloop/tasks.json`. So the command
now takes the loop lock like `answer` and `retire` do, and when it cannot take
it, nothing happens at all — because the alternative, archiving anyway and
leaving the requeue to whoever runs next, produces exactly the `blocked`-with-no-
open-blocker state the sweep exists to end.
**Fix:** depends which lock it is.
- **Live** (`another autoloop process holds …`): a loop really is running. Wait
  for it, or `python -m autoloop pause` and let the round finish. The blocker is
  still open, so nothing was lost.
- **Stale** (`stale lock at … recover with`): the owner is verifiably dead — run
  `python -m autoloop unlock`, then the same `archive-blocker` command again.
  This is the common one here, because the dead session that left the lock is
  usually the same session whose blocker you are archiving.
Do NOT hand-edit the blocker record or call `BlockerStore.archive_stale` from a
one-liner to get past this. That is the route the command was written to
replace, and it skips the requeue the refusal is protecting.

### `answer` / `archive-blocker` exits 1 saying the blocker "was reopened"
**Symptom:** the command prints `error: the task graph could not be reconciled
(...)`, then `blocker blk-xxx-001 was reopened — nothing changed`, then
`blocker blk-xxx-001 was NOT resolved` (or `was NOT archived`), and exits 1. The
blocker is still listed as open by `python -m autoloop blockers`.
**Cause:** not a bug in the close — the close worked and was then UNDONE, on
purpose (blk-01, 2026-08-21, review round 3). Closing the last open record
naming a quarantined task has to requeue that task in the same operation
(`docs/AUTOLOOP.md` §9c). The parenthesised error is why the task half could not
be done, and it is a fault in `.autoloop/tasks.json`, not in the blocker:
usually a `depends_on` naming a task that no longer exists (`KeyError`, which
survives `from_dict` and fails on the later lookup), a graph that will not parse,
or a `tasks.json` that cannot be written.
**Fix:** repair the task graph, then run the same command again — the record was
restored byte-for-byte, so it is still answerable/archivable.
1. `python -m autoloop tasks` or `python -m autoloop start --check-only` to see
   the same fault reported (`tasks        UNREADABLE (...)`); `start` reports it
   and carries on rather than dying, which is the intended asymmetry.
2. Fix what it names — most often a dangling `depends_on`, or file permissions on
   `.autoloop/tasks.json`.
3. Re-run the original `answer` / `archive-blocker`.
If instead you see `blocker blk-xxx-001 could NOT be reopened (...)`, the restore
write itself failed: the record IS closed on disk and its task was NOT requeued.
Fix the filesystem problem, then either reopen the record by hand or leave it —
the next `start` / `run` sweep requeues the task, since those paths stay
deliberately tolerant.

### `retire` exits 1: `retiring task '…' would strand N dependents (…)`
**Symptom:** `python -m autoloop retire roadmap-01` prints
`error: retiring task 'roadmap-01' would strand 4 dependents (ingest-01,
ingest-02, ingest-03, ingest-08); 21 tasks blocked in total, counting those
behind them — …` and exits 1. Nothing was written: the task is still pending
and every dependent still names it.
**Cause:** working as intended (retire-01, 2026-08-23). `state_of` counts a
dependency satisfied only when it is `completed`, and a retirement is written
once with no reverse — so those 4 tasks, and the 17 behind them, would be
BLOCKED forever with no command able to release them (`answer` needs an open
blocker, `release` needs an in-progress task, and there is no `unblock`). The
refusal is the point; the old behaviour was to do it silently.
**Fix:** decide what the dependents should wait for, then say so in the same
command. Read BOTH numbers first — 4 direct and 21 total are different
decisions.
1. If a successor continues the work and is already a task in the graph:
   `retire roadmap-01 --superseded-by roadmap-02`. That id replaces
   `roadmap-01` in each dependent's `depends_on`, in the same operation.
2. If the task went stale, or the successor is not planned yet:
   `retire roadmap-01 --rewrite-dependents`, which drops the dependency
   instead. Add `--superseded-by` alongside it to keep the record even when the
   successor cannot be waited on.
3. If you would rather re-plan by hand, leave the retirement and re-point the
   dependents through the inbox's `depends_on` mutation first.

Two neighbouring messages, so you do not reach for the wrong flag:
* `--superseded-by names brw-08, which nothing can wait on (brw-08 is not a
  task in this graph)` — a successor need not exist for the RECORD, but nothing
  can wait on an id the graph does not have. Plan it, or use
  `--rewrite-dependents`.
* `ingest-01 depends on 'roadmap-01' and is in progress` — its dependencies are
  what the running dispatch is being judged against. Wait for the round, or
  `python -m autoloop release ingest-01` first. No flag forces this one.

If the task is ALREADY retired and its dependents are stranded, `retire` cannot
fix it — a repeat rewrites nothing, and `--rewrite-dependents` on one is refused
rather than silently ignored. Re-point them with a `depends_on` mutation.

### `retire --superseded-by` exits 1: `dependency_cycle: dependency cycle: …`
**Symptom:** `python -m autoloop retire old-01 --superseded-by new-01` prints
`error: dependency_cycle: dependency cycle: new-01 -> dep-01 -> new-01` (the
code and the message, as every `TaskGraphError` prints) and exits 1. Nothing
was written — not the retirement, not one dependent. The same command without
`--superseded-by` refuses for stranding instead, and every id in the chain
looked fine on its own.
**Cause:** working as intended (retire-01, 2026-08-23). The successor you named
is what closes the loop: `--superseded-by new-01` replaces `old-01` with
`new-01` in every dependent, so if `new-01` already waits (directly or through
a chain) on one of those dependents, the rewrite makes it wait on itself.
`tasks.TaskRegistry._retirement_rewrites` plans every dependent, then
cycle-checks the WHOLE resulting graph once before writing any of it — a
per-task check would pass each new edge on its own and still build the cycle.
Fails CLOSED: nothing is applied, so there is no half-repaired graph to unwind.
**Fix:** the successor is wrong for these dependents, so choose again — the
error names the loop, and the ids in it are the ones to look at.
1. `python -m autoloop tasks` and read `new-01`'s own `depends_on`. If it waits
   on work that waits on `old-01`, it cannot also replace `old-01` for them.
2. Name a different successor — one that does not wait on any of the listed
   dependents.
3. Or drop the dependency instead: `retire old-01 --rewrite-dependents` with no
   `--superseded-by`. Note that adding `--rewrite-dependents` *alongside*
   `--superseded-by new-01` does NOT get past this — it applies the same
   substitution and builds the same loop. The flag lifts the strand refusal,
   never the cycle check.
4. Or re-point the dependents by hand first (`depends_on` mutation), then
   retire.
**Do NOT** relax the cycle check to get past it: it is the only thing standing
between this command and a graph that can never be scheduled. Exactly ONE edge
is exempt — a self-edge on the task BEING retired, which nothing re-points
(that task is going terminal, so `state_of` never reads its dependencies again)
and which therefore cannot strand anybody.

A *stored* cycle cannot produce this message: `TaskRegistry.from_dict` runs the
same check over the whole graph on load, so a hand-edited `tasks.json` naming a
task after itself (or any longer loop) fails to LOAD. That surfaces as a load
error wherever the graph is read — `python -m autoloop start --check-only`
prints `tasks        UNREADABLE (dependency cycle: …)` (`cli.py:2800`) — and it
is the entry above, not this one.

---

## 9. Autoloop monitoring (health)

### health says stuck for N minutes but the loop is fine — the laptop was asleep
**Symptom:** `autoloop health` reports `autoloop looks stuck — no activity for
224 minutes, phase=awaiting, no subagent running` while the loop is perfectly
healthy: the transcript and state file were both written 69 seconds after the
alert, and the loop went on to restart its browser, rotate the conversation
and continue. (Observed 2026-08-05.)
**Cause:** `health.check` measured silence as wall-clock time since the last
transcript write, so hours of machine sleep read exactly like a hung loop —
nothing could possibly have run during them. This is the same wrong
assumption `lock.boot_time_epoch` already corrects for locks: wall-clock
arithmetic (and equally a monotonic clock, which macOS STOPS during sleep)
cannot tell "quiet" from "off".
**Fix:** applied repo-side in `autoloop/health.py` (2026-08-18, hlth-01).
Silence is now judged in awake minutes, and only time the machine PROVABLY
spent awake can accuse the loop. `machine_sleep_in_window` uses
`lock.boot_time_epoch` as the shared boot boundary — wake history read from
a running kernel describes THIS boot only, so any part of the window before
boot (an off or rebooting machine) joins the discount rather than reading
as awake silence — then fills in sleep within the boot from
`sysctl kern.sleeptime`/`kern.waketime` on darwin (the same sysctl-timeval
evidence family `lock.boot_time_epoch` uses for kern.boottime) and
CLOCK_BOOTTIME−CLOCK_MONOTONIC on linux. The darwin sysctls carry only the
LAST sleep→wake pair, so a window starting before that sleep reaches time
they cannot see — earlier finished sleeps hide there — and only the tail
since the last wake is credited as awake; proven sleep and unprovable time
are discounted alike before the threshold is applied. When wake history
cannot be read at all (boot time included) the check reports not-stuck and
says why in the detail — fail toward quiet, because a false "stuck" trains
a human to ignore the monitor, while a missed detection is retried by the
next scheduled check; a genuinely hung loop keeps growing the proven-awake
tail, so even partial history delays its alarm by at most one silence
window after the last wake. Do NOT "fix" this by raising
`DEFAULT_SILENCE_MINUTES`: that trades a wrong answer for a slower wrong
answer and delays the alert for a genuinely hung loop. A live subagent still
suppresses the alarm regardless of transcript age or wake history — that
rule is load-bearing and evaluated first.

### the dashboard says "the task graph could not be read" and tasks.json is fine
**Symptom:** the roadmap and summary panels both read *"the task graph could not
be read — tasks.json did not load as a registry"*, and the file is demonstrably
healthy: valid JSON, `TaskRegistry.from_dict` parses it, `state_of` raises for
none of its tasks, no dangling `depends_on`, and `task_groups` returns all six
groups when you run it by hand. (Observed 2026-08-22: 171 tasks, all six groups,
nothing wrong with the data at all.)
**Cause:** **the reader, not the file.** The dashboard is a long-lived Python
process and merging into the checkout does not reload it, so a page served by a
process older than the current `autoloop/tasks.py` is running code that cannot
parse what the current code writes. The page had been up 19.5 hours across four
merges. This is the most expensive shape of failure in the repo — a PROCESS
fault reported through a DATA branch — because every obvious next step (check
the JSON, check the dependencies, check whether the writer is atomic) is
looking in the wrong place, and all of them come back clean.
**Fix:** shipped 2026-08-23 (loop-03). The dashboard now reads the same
`.autoloop/pending_upgrade.json` the loop already consumes and re-execs itself
between connections; a stale process, an unreadable marker, a failed preflight
and a refused exec each render their own banner, and the two "could not be read"
panels carry a caveat pointing at it. **Read the banner before the file.** If
you are on an older build, or the banner says `exec_failed`, restart the
dashboard by hand and re-read the page before investigating the data — and note
that `build.stale` alone is not enough to tell you this: it hashes
`dashboard.py`, which on 2026-08-22 had not changed. See `docs/AUTOLOOP.md`
§3f-quater.

---

## 10. Autoloop merge sweep (documentation trackers)

### AssertionError: SUMMARY.md carries 2 notes markers, not 1
**Symptom:** `autoloop/tests/test_docs_merge.py::test_every_shipped_tracker_ends_with_an_append_only_section`
and `::test_every_change_note_line_is_short_enough_to_merge_by_line` both fail on
one tracker while the file looks correct — its change-note section is last in the
file, nothing follows it, and every note row is short. Before 2026-08-23 the
message read `docs/SUMMARY.md must carry exactly one notes marker` and the first
test was named `test_both_trackers_end_with_an_append_only_change_note_section`;
both changed when the tracker set grew from two files to four, and the failing
line now comes back inside a list of shape problems from `section_problems`.
**Cause:** a table row ABOVE the section quoted the marker comment in FULL while
documenting how the resolver works, so the file carried the marker twice. This is
not a formatting nit: `note_merge.resolve_note_append` begins with
`count(NOTES_MARKER) != 1 → None` (with two copies it cannot tell which one opens
the append-only section), so a duplicate switches auto-resolution off for that
tracker and every parallel note merge halts again — silently, with no symptom
except the 2026-08-18 failure returning. Hit 2026-08-19 in docs-01's own first
round, in `SUMMARY.md`'s `note_merge.py` row.
**Fix:** in every file in `note_merge.NOTE_TRACKERS` — `docs/SUMMARY.md`,
`docs/TESTS.md`, and since 2026-08-23 `docs/SECURITY.md` and this one — refer to
the marker as `CHANGE-NOTES` and never write the comment out a second time;
quoting it in `CLAUDE.md` is fine, since that file is not one of them. Rule 5 of
each tracker's change-note section and `CLAUDE.md` §12 now say so, and the
assertion message above names the consequence.

### AssertionError: SUMMARY.md: a change note grew to 976 chars — split it into a second line instead
**Symptom:** `autoloop/tests/test_docs_merge.py::test_every_change_note_line_is_short_enough_to_merge_by_line`
fails, validation fails with it, and an executor round that implemented its task
correctly is discarded. Hit twice on 2026-08-21 — merge-04 at 16:39:57 (976
chars, `TESTS.md`) and blk-02 at 17:43:57 (773 chars, `SUMMARY.md`) — costing
about 20 minutes each.
**Cause:** a change-note line ran past `note_merge.MAX_NOTE_LINE_CHARS`. The
limit is measured over the WHOLE line, so the `| date | task-id |` cells count
towards it; a note that reads as a reasonable sentence can still fail on the
assembled row. It is not a style rule — a row that keeps growing is the
2026-08-18 merge-sweep failure returning (see the next entry), which is why it
is enforced rather than suggested.
**Fix:** split the note into a SECOND appended line — same first cell, one
dated note per line, exactly as the four `state.py`, `transcript.py` rows in
`SUMMARY.md` do. Never shorten it by editing a line someone else wrote, and
never merge two notes into one row to save a line.
**Do not** hard-code the number into a doc or a prompt while fixing this.
`autoloop/note_merge.MAX_NOTE_LINE_CHARS` is the single source: the test reads
it, and `implement_executor._authoring_rules` renders it into every
implementing agent's brief so the rule arrives as input rather than as this
rejection (brief-01, 2026-08-22). A copy that silently disagrees with the test
is worse than no copy at all.

### CONFLICT (content): Merge conflict in docs/SUMMARY.md — two tasks recorded a change note
**Symptom:** the merge sweep aborts on `docs/SUMMARY.md` (or `docs/TESTS.md`)
for two branches that changed nothing in common. It halted three times in one
evening on 2026-08-18 and left five reviewed, published tasks unmerged for a
full day (dash-10, loop-02, brw-12, hlth-01, wrk-01), each resolved by hand.
14 merge commits already touched `SUMMARY.md`, so it had been recurring quietly.
**Cause:** every task records a change note in those two files, and the note
used to be appended INSIDE an existing table row — one row per module, the
longest 19,410 characters in `SUMMARY.md` and 15,729 in `TESTS.md`. Two
branches touching the same module therefore edit the same LINE, which is the
granularity git merges at, so nothing can reconcile them automatically.
**Fix:** applied repo-side (2026-08-19, docs-01) in two halves, both required.
Each tracker now ends with an append-only **"Change notes"** section opened by
a CHANGE-NOTES comment, and `CLAUDE.md` §12 makes a change note ONE NEW LINE —
a new table row, or a line appended below that comment. The
other half is `autoloop/note_merge.py`, called from
`auto_merge.AutoMerger._merge`: when a merge conflicts ONLY in that section and
both sides left every pre-existing line byte-identical, the two branches'
appended lines are combined and the merge is committed. Anything else — a
conflict in the prose above the section, an edited/deleted/reordered existing
note line, a conflicted path outside the trackers — is refused and the
merge aborts exactly as before, with `auto_merge_notes_refused` in the
transcript saying which and why.

**Since 2026-08-23 (notes-03) this covers four trackers, not two.**
`docs/SECURITY.md` and this file were added to `note_merge.NOTE_TRACKERS` after
each was given its own marker-delimited append-only section — the section
first, the list second, because a path granted to the resolver without one is a
file whose ordinary prose it would start combining. It mattered because ONE
uncovered path refuses the WHOLE merge: on 2026-08-22 bind-01, split-01 and
dash-17 were each refused with `conflicted path(s) outside the change-note
trackers`, over six documentation conflicts that were all this same
append-at-the-end shape. `CLAUDE.md` and `docs/SCHEMA.md` have no such section
and still conflict normally.

**Do not "simplify" this to `merge=union`.** It was shipped that way for a few
hours on 2026-08-19 and removed after review (`docs/SECURITY.md` S34). Git
cannot scope a merge attribute to a REGION, and union never reports a conflict
at all, so the attribute silently concatenated genuine prose conflicts in those
files instead of stopping the sweep; it also resolves per LINE, so two branches
that grew the SAME row duplicated the whole row rather than merging the two
additions. Both failures are demonstrated against real git in
`autoloop/tests/test_docs_merge.py` (`test_union_would_have_swallowed_a_genuine_prose_conflict`,
`test_union_duplicates_a_grown_row_instead_of_merging_the_two_additions`), and
`.gitattributes` is kept rule-free with the argument in it. If you
hand-resolve one of these anyway, re-read the result — the 2026-08-18 splice
into `SUMMARY.md`'s `orchestrator.py` row duplicated ~4,500 characters and cut
a sentence in half, and it is still there.

---

## 11. Autoloop audit charters (target-repository portability)

### `audit charter file … exists but is not a regular file (mode drwxr-xr-x)`
**Symptom:** an `audit` round comes back `status: error`, summary `audit not run
— the repository's audit charters could not be loaded: …`, with that phrase
instead of a parse complaint. Also seen as `cannot examine audit charter file …:
[Errno 20] Not a directory` or `[Errno 13] Permission denied`.
**Cause:** something occupies `docs/audit_charters.toml` (or whatever
`[repo].audit_charters_file` names) that is not a readable regular file — most
often a directory created by a botched checkout or an editor, or a parent path
component that is itself a file. This is a REFUSAL, deliberately: absence means
`FileNotFoundError` and nothing else, because `is_file()` answers False for a
directory exactly as it does for nothing at all, and treating those alike would
hand the run the built-in `DEFAULT_DOMAINS` inside a checkout that plainly meant
to ship its own charters — the same silent degradation the parser refuses.
**Fix:** look at what is actually there (`ls -ld docs/audit_charters.toml`) and
either put the real file back or remove the wrong-type entry, at which point the
built-in fallback applies again. `audit_charters_file = ""` in
`.autoloop/config.toml` is the deliberate opt-out if you want the built-ins
regardless. Do NOT relax this to `is_file()` — pinned by
`test_audit_charters.py::test_a_directory_at_the_charter_path_is_refused_rather_than_read_as_absent`.

### `audit not run — the repository's audit charters could not be loaded: …`
**Symptom:** an `audit` round comes back `status: error` with that summary and
nothing else — no `docs/AUDIT_<date>.md`, no `.autoloop/audit/<run-id>/`
directory, no agent output, `validation: not run`. The rest of the message
names a file and a fault, e.g. `… docs/audit_charters.toml [[domain]] #2:
duplicate slug 'security_paths'`.
**Cause:** the repository being audited ships an audit-charter file
(`[repo].audit_charters_file`, default `docs/audit_charters.toml`) that exists
but does not parse. This is a deliberate refusal, not a crash: the alternative
— falling back to the built-in `DEFAULT_DOMAINS` — would brief the agents on
THIS repository's architecture inside whatever checkout is under audit and file
a report that reads as complete while describing the wrong codebase. It is
checked before the run directory is created, so a failed round costs nothing.
Note the file is read from the root of the checkout the call is rooted at — in
production the audit's own worker repo, not the main checkout — so a fix in the
main checkout only takes effect for a round dispatched after it is committed.
**Fix:** repair the file the message names (format and rules in
`docs/AUTOLOOP.md` §7, "Domain charters": one `[[domain]]` table per domain in
wave order, exactly `slug`/`title`/`charter`/`model`, `model` limited to
`haiku`/`sonnet`/`""`, charters in `'''` literal strings because `"""`
processes escapes). Two escape hatches, both deliberate: delete the file and
the built-in charters are used again, or set `audit_charters_file = ""` in
`.autoloop/config.toml` to stop looking for one at all. Do NOT "fix" this by
making the loader fall back on a parse error — the refusal is the feature, and
`autoloop/tests/test_audit_charters.py::test_a_malformed_file_never_degrades_to_the_built_in_charters`
is what stops it coming back. If instead the audit RAN but reported domains you
did not expect (check the summary's "Domain charters came from …" clause and
the coverage table), that is the same setting working: some checkout in the
chain ships a charter file.

### A test asserting a phrase in CLI output fails, and the phrase is visibly there when you print it
**Symptom:** `assert "every record here predates measured durations" in out`
fails against `autoloop profile`'s output, but the sentence reads correctly on
screen. Grepping the source finds the string, spelled exactly.
**Cause:** the renderer word-wraps its explanatory notes
(`transcript._wrapped_note`, 74 columns), and the wrap fell inside the phrase:

```
  measured     n=0
               no record of type 'request_prepared' carries a duration_seconds; every
               record here predates measured durations
```

The rendered TEXT contains `"...; every\n               record here..."`, so the
substring the test looks for does not exist in it. Nothing is wrong with either
the assertion or the sentence — they simply cannot both be about a string a
formatter is free to break.
**Fix:** applied repo-side — the note is emitted as TWO `_wrapped_note` calls,
so the load-bearing sentence is short enough (45 chars) that no wrap can split
it. Do NOT fix it by weakening the assertion to a fragment, normalising
whitespace in the test, or widening the wrap column: each of those leaves the
next reformat free to break the same test again, and the first also stops the
test checking the thing it was written for.
**The general lesson:** if a test asserts on wrapped or formatted output, the
asserted phrase must be short enough to be unbreakable by the formatter, or the
formatter must be handed the phrase as its own unit.

### `autoloop profile` reports `execute  measured  n=0` on a live loop while the orchestrator tests all pass
**Symptom:** the timing instrumentation is committed and green, but a real
`.autoloop/transcript.jsonl` shows `duration_seconds` on `request_prepared` and
`request_submitted` and never on `executed`. The `execute` stage reports `n=0`
measured with a gap-derived row beside it — the exact hole the measurement was
added to close.
**Cause:** the executor is dispatched from a BRANCH, and production and half
the test suite take different arms of it
(`orchestrator._dispatch_task_postcommit`):

```python
if self._worker_repos is not None and not is_audit:
    outcome = self._execute_with_escape_detection(directive, task)   # production
else:
    outcome = self._executor.execute(directive, task)                # some tests
```

A stopwatch started inside the `else` measures only the second arm. Every
`build()`-based test in `test_orchestrator.py` takes it (no `worker_repos`), so
they pass — while every real round, and every `build_postcommit()` test, goes
through the escape-detection arm and records nothing. The failure is silent in
exactly the place a passing suite is most persuasive.
**Fix:** applied repo-side — the stopwatch is started BEFORE the `if`, so both
arms are inside the window, and `test_orchestrator.py::test_executed_carries_
its_request_id_and_a_measured_duration` uses `build_postcommit` (worker repos
present) rather than `build`. The escape-detector snapshots are inside the
measured window deliberately: they are part of what the round spends.
**The general lesson:** when adding instrumentation around a call that appears
more than once, check which call site production actually reaches before
choosing a test harness — and pick the harness that exercises that one.

### A measured duration tracks the gap it was supposed to replace
**Symptom:** `autoloop profile` shows `measured` and `gap-derived` moving
together on `submit` or `execute` — the measured number is a little under the
gap on every round, instead of being a small fraction of it on the bad ones.
The instrumentation is at the right call site and the tests pass.
**Cause:** `Stopwatch.stamp()` STOPS a watch that is still running, so the
boundary of the measurement is wherever `stamp` is called, not where the
operation ended. Starting a watch at the operation and stamping it at the
transcript record therefore measures the operation *plus* everything between:

```python
submit_watch = self._stopwatch()
result = client.submit(req.request_id, req.prompt)   # the operation
req.last_send_outcome = self._client_send_outcome(client)
...                                                  # reconciliation branches
self._log("request_submitted", data=submit_watch.stamp({...}))  # <- stops HERE
```

The result is a number labelled `measured` that is really a small gap. It is
the exact error the measured column exists to remove, and it is invisible in
the output — a gap wearing a measured label looks like a measurement.
**Fix:** applied repo-side — every site calls `watch.stop()` on the operation's
own last line and lets the latched value be stamped later (first stop wins, so
the later `stamp` writes the frozen reading). Do NOT "fix" a suspicious number
by subtracting an estimate of the bookkeeping, and do not move the `stamp` call
closer to the operation instead: the record cannot be written until the payload
is built, which is why the two are separated in the first place.
**The general lesson:** when a timing API can stop implicitly, the call that
stops it IS the boundary. Write the stop where the operation ends, and test it
by making the code between the boundary and the emit consume clock readings —
`test_work_after_the_boundary_cannot_inflate_any_measured_duration` does
exactly that, and it is the only kind of test that can tell the two apart.

---

## 12. Autoloop Codex app-server transport (`codex_app_server`)

### `the codex app-server is framing messages with LSP-style Content-Length headers`
**Symptom:** every round with `conversation.provider = "codex_app_server"` fails
immediately, at `attach()`, before any thread is opened.
**Cause:** this transport speaks newline-delimited JSON, one object per line, and
the server answered with `Content-Length:` headers instead. That choice is the
transport's one unverifiable structural decision:
`docs/codex-app-server-protocol.generated.ts` declares message TYPES, not
delimiters, so the framing cannot be checked against ground truth the way the
method names can. The error is raised deliberately rather than letting
`json.loads` fail on a header line and report a decode error fifty lines into a
stack trace.
**Fix:** the codex-cli version in use has changed its framing. Regenerate the
reference (`codex app-server generate-ts --out <dir> --experimental`), then teach
`AppServerClient._read_message` / `_send` the header framing — both directions,
in the same change. Do NOT "fix" it by stripping the header and parsing the rest:
the body length is what the header is for, and a partial read would silently
truncate a review packet. Falling back to `codex_cli` is the immediate
workaround; it is unaffected.

### `thread/start returned no readable thread id` / `thread/read returned a shape this client cannot read`
**Symptom:** a `codex_app_server` round fails with one of those two sentences,
each naming the key list it looked for.
**Cause:** protocol drift in the `v2/` param shapes. The committed reference
concatenates the 97 top-level declaration files and REFERENCES the `v2/` ones by
import path without including their bodies — `grep 'export type ThreadStartParams'`
comes back empty — so `codex/wire.py` reads those fields through tolerant
candidate lists (`THREAD_ID_KEYS`, `THREAD_ITEMS_KEYS`). Neither error means the
call failed; it means the ANSWER could not be read. `thread/read`'s refusal is
deliberately not a False: "not on the thread" and "cannot read the reply" are
different, and only the first may authorize a resend.
**Fix:** add the new spelling to the candidate tuple in `codex/wire.py` and
regenerate the reference in the same change. If the regenerated file now
CONTAINS the `v2/` bodies,
`test_the_unpinned_spellings_are_declared_unpinned_and_still_are` fails — that is
the signal to pin those fields properly instead of widening a guess list.

### A `codex_app_server` turn hangs until the timeout with the reviewer visibly idle
**Symptom:** `ResponseTimeoutError: the codex app-server did not answer
turn/start within 900.0s`, and a `turn/interrupt` in the transcript right after
it.
**Cause:** most likely a server→client request nobody answered. `ServerRequest`
includes approval asks (`applyPatchApproval`, `execCommandApproval`,
`item/commandExecution/requestApproval`, …) and the server BLOCKS on its own
request; a client that dispatches only responses-by-id and notifications drops
them silently. `_answer_server_request` exists for exactly this and must answer
everything — `{"decision": "abort"}` for the two approvals whose response type
the reference settles, a JSON-RPC error for the rest.
**Fix:** if a new server request method is wedging turns, it still gets an
error response, not silence. Do NOT answer a new approval kind `approved` to
clear the hang: that grants the reviewer command execution, which is `S37`'s
whole bound (`docs/SECURITY.md`).
**The other cause, and do not fix it the tempting way:** `turn/completed` never
arriving. The transport waits for it rather than stopping at the first assistant
text, because a reviewer that says "let me look at part 3" and keeps working
would otherwise have its aside taken as the verdict — a silently wrong review,
where waiting for a completion that never comes is a loud timeout. Fix the
completion signal, not the wait.

### `codex_app_server` is configured and `doctor` says nothing about codex at all
**Symptom:** `doctor` reports no `codex_command` / `codex_workdir` /
`codex_sandbox` rows, and a missing binary first surfaces as a failed review
round.
**Cause:** `doctor.py`'s check is gated on `{provider, fallback_provider} &
{"codex_cli"}`, so it does not recognise the app-server provider. That file was
outside codex-01's approved paths and is a known gap, recorded in
`docs/AUTOLOOP.md` §5d-ter and in `docs/SUMMARY.md`'s change notes.
**Fix:** until it is widened, check by hand: `which codex`, and confirm
`codex.working_dir` (empty means `$HOME`) is outside the repository. When
widening it, add `"codex_app_server"` to that set and read
`codex.app_server_command[0]` rather than `codex.command[0]` for the PATH check —
they are different settings and either may be overridden alone.

---

## 13. Autoloop urgent preemption (`python -m autoloop urgent`)

### `policy_denied … a fresh 'audit' is a full executor round and cannot start ahead of it`
**Symptom:** the reviewer answered `audit` and the loop refused it, re-prompting
with the urgent task's id. `state.policy_denials` went up by one.
**Cause:** intended, since 2026-08-22. An audit takes no task out of the queue
but it takes the LOOP, for a measured 1282-second executor round, which is the
only thing an urgent request is asking for. Before that date audits were exempt
and every new session opened on the audit kickoff, so the session a preemption
had just started invited exactly the round the operator had paid to skip.
**Fix:** none — send `implement` for the task the denial names. The audit is
deferred, not cancelled: request it again once the urgent task has been
dispatched, which consumes the pin. A `revise` continuing an audit arc already
in flight is NOT refused, so a half-finished audit can still be completed.
**If it is wedging the loop:** the denial budget is real, and a reviewer that
keeps answering `audit` will exhaust it and fault-stop. Check that the session
actually opened on the urgent kickoff (`rg -n 'URGENT operator request'` against
the outbox in `.autoloop/state.json`); a session opened by
`run --kickoff-audit` while a pin was live will not have, because that flag
builds the audit payload directly. Clear the pin or drop the flag.

### A release or a preemption could not retire the execution it left behind
**Symptom:** `release` exits 1 with the raw failure on stderr (`error:
quarantine destination <task>-<label> already exists …`, or whatever the
filesystem said), or `status` shows a preemption whose report says
`could NOT be retired`, naming a worker repo that is still in `workers/`.
**Cause:** the status half of a release is made durable BEFORE the artefacts
move, so a retirement that fails leaves a task that genuinely IS pending with
its worker repo and execution record still on disk. It is not a failed release
and the task is selectable again — check with `tasks` before treating the error
as "nothing happened".
**Fix, in both cases:** do not re-run `release` — it refuses a task that is no
longer `in_progress`. Move the worker repo aside, or the next dispatch of that
task will refuse to create one over it.

The two paths differ in how much they tell you, deliberately. `release` RAISES
(a human is reading that command's output, and the record has already been
archived, so the worker repo is the only residue). A preemption reports instead
— nobody is watching one, and taking the process down while an operator waits
for their urgent task is the worse ending — **so after a preemption read the
last line of the report, which is one of exactly two:**
* *"none required … the pair is resumable"* — the worker repo and its execution
  record were left PAIRED (`orchestrator._repair_orphaned_record`), which is the
  same shape a killed round leaves, so the next dispatch of that task reuses the
  worker and continues the round. The cost: a live execution record holds the
  merge window shut (`python -m autoloop merge-window`) until that candidate is
  PUBLISHED — dispatch alone does not reopen it. If the window has to reopen
  sooner, move the worker to `<workers_root>/../quarantine/<task>-<label>` and
  the record to `.autoloop/executions/archive/<task>-<label>.json` by hand,
  under one label.
* *"move &lt;path&gt; aside …"* — the worker is NOT resumable (not a git
  repository at that path, or not on the recorded branch), so the record was
  deliberately not restored and the merge window is not shut. Do the `mv`: the
  next dispatch of that task refuses to create a worker over that directory.

### An urgent request was queued and the loop is still working on the old task
**Symptom:** `urgent` printed `queued URGENT request for '<id>'`, minutes pass,
and the round in flight keeps going. `tasks` shows the target still `pending`.
**Cause:** almost always the intended behaviour, not a lost request. The
preemption acts ONLY at a safe boundary — `phase == ready` with no pending
request — so a request that lands while the loop is `submitting`, `awaiting` or
`executing` waits for the round to come back to `ready` rather than stranding a
review packet or an approved push. An `executing` phase is a write-capable
agent inside a worker repo and can legitimately run for twenty minutes.
**The other ordinary cause:** the round in flight is an AUDIT. Audit rounds are
waited out rather than displaced — an audit holds no task in the queue and its
product is a report, so quarantining it mid-write spends the round twice to save
the tail of one already paid for. The wait is bounded at that ONE round: since
2026-08-22 a fresh `audit` is refused while a pin is live, and the session a
preemption starts opens on the urgent task rather than on the audit kickoff, so
the loop cannot come back with another audit lap after lap.
**Check, in this order:** `status` for the phase; then
`rg -n 'urgent_awaiting_boundary|task_preempted' .autoloop/transcript.jsonl` —
the first entry means it is waiting and names the phase, the second means it has
already happened and names what was displaced. If NEITHER appears and the
request is gone from the inbox, look for `task_inbox_drained` with a `refused`
line: the registry refuses a target that is blocked, quarantined, retired,
completed, already in flight or unscoped, and that refusal is the answer.
**Fix:** none — wait for the boundary. Do NOT `pause` to hurry it along: pausing
is what this replaces, and a pause plus a hand `release` is the sequence that
cost 10 and 15 minutes on 2026-08-21 and left two tasks stranded `in_progress`.

### `task '<other>' is already the urgent target … and has not been dispatched yet`
**Symptom:** `urgent` exits 1 with `urgent_already_pending`, naming a different
task, and nothing is queued.
**Cause:** ONE preemption at a time, by design. A pin is live from the moment
it is granted until the dispatch it asked for starts, and a second request is
refused rather than queued behind or applied over the first — two preemptions in
flight with one round to displace between them is the shape the manual sequence
failed at, and overwriting would discard the round the first operator already
paid to displace.
**Fix:** wait for the named task to be dispatched (`mark_in_progress` consumes
the pin, so the slot reopens on the very next round) and resubmit. If the
incumbent can never be dispatched — it was blocked, quarantined or retired after
being pinned — its marker is already STALE, `live_urgent_target()` reports
nothing, and the next request is accepted without any manual clearing.

### The loop ended with `Loop ended: stopped` and no reviewer said `stop`
**Symptom:** a session ends `stopped` after an urgent request, and `status`
shows `stop reason  preempted for urgent task …`.
**Cause:** a preemption ends the round as a `stopped` session with
`stop_kind = "preempted"`, deliberately, so every caller that already treats a
non-fault `stopped` as a clean round boundary does the right thing without a new
branch. `run --continuous` reassesses and starts the next session immediately.
**Fix:** none — this is the success path. The displaced round is not lost: its
worker repo is at `<workers_root>/../quarantine/<task>-displaced-by-urgent-<stamp>`
with its execution record archived under the same label in
`.autoloop/executions/archive/`, and the task itself is back in the queue. Read
the `task_preempted` transcript entry for the candidate sha and review round it
was holding.

---

## 14. Autoloop validation reporting (fail-fast)

### The validation summary says `NOT RUN` — did the suite get skipped?
**Symptom:** a refused round's `validation:` line reads e.g.
`ruff check .: FAIL (...); python3 -m pytest -n auto autoloop/tests …: NOT RUN;
python3 -m pytest -n auto tests/ …: NOT RUN; STOPPED at the first failing
command: …`. It looks like validation lost two commands.
**Cause:** it is deliberate (val-03, 2026-08-22). A validation run stops at the
first command that FAILS and names every command after it `NOT RUN` — the
verdict was already decided. Nothing was skipped silently and nothing was
weakened: `all_passed` is False exactly as before, and every configured command
is still named in the summary.
**Fix:** none needed — fix the failing command and the next round runs further.
Read `NOT RUN` as "no evidence either way", never as "passed". It is also NOT
the `SKIPPED` that per-commit test selection reports (that one means "no
reachable test lives under this command's paths"). To run everything anyway,
call `validation.run_validation_commands(..., fail_fast=False)`; there is no
config key, and `docs/AUTOLOOP.md` §4h says why.

### A test asserts a validation summary and now fails on a later command
**Symptom:** a test feeding a MULTI-command list to a runner that returns a
nonzero code asserts `<later command>: PASS` and gets `NOT RUN`.
**Cause:** same change. Under the default only the commands up to and including
the first failure execute.
**Fix:** decide which property the test is really about. If it is about the
later command, pass `fail_fast=False` (that is what
`test_validation_parallelism.py::test_a_parallel_run_still_reports_pass_fail_per_command`
does, and it doubles as the full-run mode's exercise). If it is about the
report, assert `NOT RUN` — see `autoloop/tests/test_validation_failfast.py`.

---

## 15. Autoloop Codex CLI transport (`codex_cli`)

### `browser_restarted` / `browser_restart_cooldown_blocked` on a run that has no browser
**Symptom:** `conversation.provider = "codex_cli"`, no browser anywhere in the
run's design, and the transcript fills with browser events — 34 of them in 35
minutes on 2026-08-22. Chrome is genuinely launched (pid 29055,
`--user-data-dir=/Users/emir/.autoloop-chrome --remote-debugging-port=9222`), and
the loop finally parks `loop_fatal` on `browser_restart_cooldown_blocked`,
advising a manual `python3 -m autoloop.browser.chrome_restart` or a lower
`browser.restart_cooldown_seconds`. Neither can repair a subprocess fault. Often
the `browser_error` beside it reads `no codex reply was captured for alr-…; the
invocation did not complete in this process`, `kind=ResponseTimeoutError`.
**Cause:** two things, and only the second one is obvious. (1) Every transport
fault is a `BrowserError` subclass — `errors.py` names the hierarchy after the
first implementation — so the exception type could not say which subsystem
failed, and `run` sent a fault raised by `CodexConversation` to
`_handle_browser_failure`, which drops the client, runs
`browser.restart_command` and charges the browser budgets. (2) The underlying
fault is a PHASE THAT CANNOT BE SATISFIED: `CodexConversation` keeps its reply in
an in-memory dict (a CLI turn is synchronous, so there is nothing to poll for),
the persisted phase says `awaiting`, and after a restart that reply is gone for
good — so the loop waited, failed and retried over something that could never
appear.
**Fix:** applied repo-side 2026-08-24 (recov-01).
`conversation.transport_is_browser_backed(provider)` is asked first, keyed on the
provider NAME (a `getattr(client, …)` probe fails open — the handlers run with
the client already dropped). A non-browser fault goes to
`_handle_transport_failure`: no restart, no `browser_restart_skips`, a
`transport_error` record naming the provider, and — on the ordinary failure
budget running out — a `transport_failure_budget_exhausted` park whose advice
names `codex exec`, `codex_invocation_failed` and
`conversation.fallback_provider`. Separately, a transport that declares
`idempotent_submit` now re-enters `submitting` and RE-RUNS the invocation
(`_replay_unrecoverable_await`, bounded by `MAX_AWAIT_REPLAYS`), so a restart
mid-`awaiting` finishes the round instead of parking. If you see this symptom
again, do NOT lower the cooldown or restart Chrome: check
`conversation.provider`, and read the `transport_error` /
`codex_invocation_failed` records. `docs/AUTOLOOP.md` §5d-quater has the full
account.

### `rotation_unavailable` / "set browser.project_url" on a run that has no browser
**Symptom:** a `codex_cli` run parks `loop_fatal` after two failures on one
request, saying the conversation cannot be used and that no
`browser.project_url` is configured — a key that belongs to the browser
transport and cannot help. Or, if that key IS still set from an earlier browser
deployment: a park about a rotation that failed, plus a spent `state.rotations`.
**Cause:** the sibling of the entry above, reached by a different road.
`codex_cli` returns `SubmitResult.REJECTED` on every non-zero exit and on a clean
exit with empty stdout, so two ordinary failures walk `submitting →
submission_rejected → one resend → submission_rejected → _attempt_rotation`.
That path reaches rotation WITHOUT passing any fault handler, so the transport
guard on the fault routing never sees it. Rotation is a browser concept end to
end — it opens a chat in a ChatGPT project — and this transport has no
`retarget`/`current_url` at all.
**Fix:** applied repo-side 2026-08-24 (recov-01). `_attempt_rotation` refuses
first for a non-browser transport (`rotation_unsupported_by_transport`), names
that transport's own remedy, and spends no rotation budget. If you see the old
wording, do NOT set `browser.project_url` — read the `codex_invocation_failed`
records for the two failures instead; the sends were disproven for a reason the
transport already logged.

### `the Codex allowance for this ChatGPT plan is exhausted (exit 1)` — and it is not
**Symptom:** the loop parks `loop_fatal`, code `quota_exhausted`, on an account
that is nowhere near its limit. Checked two ways on 2026-08-22: `codex app-server`
`account/rateLimits/read` reported 4% of the weekly window used with
`rateLimitReachedType=None` and `spendControlReached=false`, and a live
`codex exec --skip-git-repo-check` request completed with exit 0. Roughly 25
minutes of downtime across two parks and an hour of investigation.
**Cause:** two defects at once. (1) `codex exec` ECHOES THE WHOLE PROMPT BACK ON
STDERR — measured, a 180,024-byte packet came back verbatim — and
`is_quota_exhausted` searched `f"{stdout}\n{stderr}"`, so every word of the
review packet was inside the haystack. With `"429"`, `"quota"` and `"rate limit"`
in the pattern list as bare substrings, a packet quoting `docs/autoloop.md:4295`
— a LINE NUMBER — made ANY non-zero exit read as a spent allowance. The real
failure could have been anything; `QuotaExhaustedError` is loop_fatal with no
retry path. (2) The diagnostic that would have named the real fault was written
and thrown away: `CodexConversation.submit` logs `codex_invocation_failed` on
every non-zero exit, but the factory constructed the adapter without a `log=`, so
the no-op default stood and that record appeared ZERO times in 24 days.
**Fix:** applied repo-side 2026-08-23 (quota-01) and load-bearing. `quota.classify`
takes the FINAL prompt as a REQUIRED argument, drops every output LINE the prompt
accounts for (`codex_owned_text`), matches only WITHIN a line so a wording cannot
be assembled across a join that was never printed, and counts a surviving marker
only when the prompt does not account for it either — so nothing the loop sent
can classify what came back. The comparisons ignore whitespace and punctuation, and that
detail is the whole of it: the first cut compared literal substrings, which a
REFLOWED echo defeats, because prompt text `quota` + newline + `exceeded` printed
back as `quota exceeded` is a marker the prompt does not contain verbatim. If you
are tempted to "simplify" either comparison back to a plain `in`, that is the
regression. `"429"`, `"too many requests"` and
`"rate limit"` moved to `codex.rate_limit_patterns`, which returns REJECTED and
stays retryable. `conversation._transcript_log` wires the real logger at all three
codex factories. **Do not** answer a recurrence by narrowing
`codex.quota_patterns` by hand — that was the 2026-08-22 stopgap and it only
lowered the odds; read the `codex_invocation_failed` record instead, which now
names the classification, the deciding marker and the markers the guard
suppressed.

### `codex_invocation_failed` records a `note` and both tails are empty
**Symptom:** the record says `codex printed nothing but an echo of the prompt` (or
`codex printed nothing at all`) and carries no excerpt of either stream.
**Cause:** not a broken record. `failure_digest` excises exact occurrences of the
prompt from both streams before bounding them, and `codex exec` echoes the prompt
onto stderr — so a process that failed without printing anything of its own leaves
nothing behind but its exit code. `prompt_echo_chars`, `stderr_chars` and
`stdout_chars` tell the two cases apart.
**Fix:** read `returncode`. If the exit code alone is not enough, reproduce the
invocation by hand with the same `codex.command` and `codex.sandbox_args`
(`autoloop doctor` reports both, as `codex_command` and `codex_sandbox`) — the
digest deliberately carries no argv and no environment.

---

## Adding an entry

Newest-first within a section. Keep the symptom line verbatim so it can be found
by pasting the error text.

```markdown
### <exact error text, or a one-line symptom>
**Symptom:** what you saw, including the numbers if they matter.
**Cause:** the actual mechanism, not a guess.
**Fix:** the command or code change. If it is already applied repo-side, say
where, and say plainly if it must not be removed.
```

An entry goes in the numbered section it belongs to, newest-first, exactly as
before — **not** at the end of the file. The end of the file is now the
append-only change-note section below.

---

## Change notes — append ONE new line at the END of this file

Where a task records what it changed in THIS tracker when there is no natural
home for it above. **This section stays last in the file, and a note is
appended at the very end of it.**

**It is not where error entries go.** A new error keeps its `###` entry in the
numbered section it belongs to, in the template shape above — ordinary prose
that two branches conflict on and a human resolves, unchanged. This section is
only for the one-line "what my task did here" note, and it exists because that
note is what every parallel branch writes and therefore what every parallel
branch collided on (measured 2026-08-22: bind-01 was refused a merge over
exactly this file).

Five rules. They are not style — they are the precondition the loop's own merge
path checks before it will combine two branches' notes
(`auto_merge.AutoMerger._merge` → `autoloop/note_merge.py`; the shape is pinned
by `autoloop/tests/test_docs_merge.py`, the reasoning is in `CLAUDE.md` §12):

1. **Add a line. Never grow a line.** Do not append your note into an existing
   entry or paragraph. The resolver only combines whole lines added AFTER
   everything that was already here; a grown line is an edit, and the merge
   stops.
2. **One note, one line.** Keep it to roughly a sentence. If it needs more, add
   a second line, or put the detail in the entry above that owns it and leave a
   pointer here.
3. **Never edit, delete or reorder a line someone else wrote.** The resolver
   refuses the whole merge if you do, and that refusal is the point: a
   rewritten claim needs a human to look at it.
4. **Order is arrival order, not chronology.** A merge concatenates one
   branch's appended lines then the other's. Read the `Date` and `Task`
   columns, never the position.
5. **Call the marker `CHANGE-NOTES`; never write the comment out again.** The
   section below opens with an HTML comment the resolver finds by that text,
   and it requires the marker to appear EXACTLY ONCE in this file. A second
   copy — the easy way to quote it while documenting how any of this works —
   makes the resolver decline every parallel merge, silently. That is the
   trap the first entry of §10 is about, and this file used to carry the
   comment in full in §10's own text.

Nothing may follow the last note line: this section is the end of the file, so
that the next task's append lands at the end of the ledger rather than inside
whatever came after it. Something appended below it in the older shape — a new
`###` entry, a new `##` section — fails
`test_docs_merge.py::test_every_shipped_tracker_ends_with_an_append_only_section`
rather than landing outside the ledger unnoticed.

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. Never edit a line above. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-23 | notes-03 | This file joined `note_merge.NOTE_TRACKERS`: a merge conflicting only in the section below is now combined by the loop, while a conflict in any error entry above the marker still refuses the whole merge. §10's fix text had to stop quoting the marker comment in full first — with two copies in the file, the resolver refuses every merge of it. |
| 2026-08-23 | port-01 | Read the two entries above about a RELATIVE `state_dir` — the sibling-worktree one in §2 and the stray `.al`/`.autoloop` one in §7 — as describing an EXPLICITLY configured value from now on. The unconfigured default is no longer `.autoloop`; it is `<workers_root>/../state`, absolute (`config.default_state_dir`, `docs/AUTOLOOP.md` §3h). Both entries stay accurate as written, because `config.example.toml` and both test helpers still set the key explicitly. |
| 2026-08-23 | quota-01 | New §15 for the `codex_cli` transport, with the two entries a false exhaustion park actually presents as. The first is the 2026-08-22 incident and says plainly that narrowing `codex.quota_patterns` by hand was the stopgap, not the fix — do not answer a recurrence that way. |
| 2026-08-23 | notes-04 | A `task_base_behind_head` park caused purely by a change-note collision should no longer happen: refreshing a task's base now consults the same resolver the merge sweep already used, in the other direction. A collision in a tracker's PROSE, or in any path outside `note_merge.NOTE_TRACKERS`, still parks with exactly the message and the three operator choices it always had — so every other entry's advice about that code is unchanged. |
| 2026-08-23 | loop-03 | New §9 entry for the most expensive failure shape in this repo: a PROCESS fault reported through a DATA branch. "the task graph could not be read" on a healthy `tasks.json` means the dashboard is older than the checkout, not that the file is corrupt — read the banner before the file. `build.stale` alone will not tell you: it hashes `dashboard.py`, which on 2026-08-22 had not changed while `tasks.py` had. |
| 2026-08-23 | quota-01 | §6's `rate_limited`-for-hours entry is about the BROWSER transport and is unaffected: it stays the right entry for a `RateLimitedError` with no attachable page behind it. The codex adapter deliberately never raises that error, which is why its transient limits get an entry of their own rather than a clause in that one. |
| 2026-08-23 | quota-01 | §15's first entry had its Fix paragraph corrected in place: it claimed a reflowed echo could not reopen the hole, and a literal substring test against the prompt does not hold that. The corrected text names the worked case (`quota` + newline + `exceeded` printed back as `quota exceeded`) and says outright that simplifying either comparison back to a plain `in` IS the regression — the next reader's most likely wrong move. |
| 2026-08-23 | quota-01 | §15 is titled for the `codex_cli` transport and stays that way, but read its first entry's SECOND half — a throttle routed to the loop_fatal branch — as having applied to `codex_app_server` too until this date: `rate_limit_exceeded`, `rate_limited`, `too_many_requests` and a numeric 429 were all in that transport's exhaustion vocabulary. No production symptom was ever recorded for it, which is why this is a note and not a new entry. `docs/AUTOLOOP.md` §5d-ter has the routing table. |
| 2026-08-23 | quota-01 | If that one ever DOES present: the symptom is a `loop_fatal` park, code `quota_exhausted`, whose `codex_app_server_failed` record shows `classification: quota_exhausted` next to an `error_type` or `status` that describes a throttle. The remedy is a config edit — move the code out of `codex.quota_error_codes` into `codex.rate_limit_error_codes` — not a code change, and not emptying either list. |
| 2026-08-23 | retire-01 | New §8 entry for `retire`'s strand refusal, filed there with the other recovery-command exits rather than as a §9/§10 entry, because the symptom is a CLI exit 1 on `python -m autoloop retire`. It is a working-as-intended entry: the refusal is the fix landing, not a fault, and the three neighbouring messages (`--superseded-by names … which nothing can wait on`, `… is in progress`, and the already-retired refusal) are listed so the reader does not reach for the wrong flag. |
| 2026-08-23 | retire-01 | Second §8 entry beside it, for the symptom the strand rewrite makes reachable: `retire --superseded-by` exiting 1 with `dependency cycle:`. The successor is what closes the loop, so the fix is choosing a different one (or dropping the edge) — never relaxing the check, and `--rewrite-dependents` does not get past it either, which the entry says outright because it is the obvious wrong move. It also says a STORED cycle cannot produce this: `from_dict` cycle-checks on load, so such a file fails to load instead. |
| 2026-08-23 | ship-01 | `python -m autoloop record-shipped` exiting 1 with "git could not decide whether … is an ancestor" is WORKING AS INTENDED, not a fault: a shallow clone, an object this checkout has never fetched, or an unreadable repository all answer `unknown`, and queueing on that would make the verification step pass precisely when it cannot see. The fix is `git fetch` the carrying commit (or unshallow) and re-run — never widening the check to accept `unknown`, which is the obvious wrong move. |
| 2026-08-23 | ship-01 | A task showing under *Registry / code disagreements* as `completed_unwitnessed` does NOT mean the work is missing. It means no commit subject names the id, which is absence of evidence — the work may have shipped under a subject that never named it. That is why the row is marked UNPROVEN and why `shipped-report` still exits 0 for it alone. Retiring or re-running the task on the strength of that row is the licence-to-redo-landed-work failure the report is shaped to refuse. |
| 2026-08-23 | ship-01 | `shipped-report` printing INVALIDATED for a record that was fine yesterday usually means the base moved, not that the record was wrong: a rebase or force-move renames the carrying commits. Re-run `record-shipped` with the new shas — re-recording is allowed on purpose, unlike a retirement. There is deliberately NO route back to `pending`, so do not look for one; a claim that the evidence was wrong is a task to plan, not a status to flip. |
| 2026-08-23 | ship-01 | New §2 entry for a fixture trap that cost a round here: a `BacklogSweeper` test whose hand-built `PolicyConfig()` leaves `auto_merge_enabled` at its default False gets `disabled` from `sweep()` before enumeration runs. The loud version fails on the outcome; the quiet version (`unresolved == []`, `pending == []`) passes vacuously, which is why the entry says a sweep test must pin the outcome as well as what the sweep did not find. |
| 2026-08-23 | stop-01 | New §8 entry for the 2026-08-20 livelock: the loop collecting the same reviewer `stop` every few minutes while `health` reports `running` / `open_blockers: 0` / `needs_attention: FALSE`. Filed as fixed rather than as a live trap — the third matching stop now parks `stop_livelock` — but the recovery is what the entry is for, including the `stop_repetition_ledger_unusable` variant, whose remedy is deleting a counter file rather than answering alone. |
| 2026-08-23 | stop-01 | New §2 entry for the test trap beside it: `health.check` asks the LOCK whether a loop is running, so a hermetic test asserting `needs_attention is False` fails with `not_running` unless it holds `LoopLock`. The half worth knowing is the ordering — blockers are judged before the lock and before any phase, so the True direction needs no lock, and after answering a loop_fatal blocker the verdict moves to `stuck_parked`, not to running. |
| 2026-08-23 | stop-01 | Revision round. Third §2 entry, for the trap that cost this round: a scripted `stop` whose `reason` is `""` never stops anything. `contract._require_str` refuses empty AND whitespace-only strings, so the reply is a `missing_field:reason` parse error, the round spends a corrective re-prompt, and a one-element fake client dies with "test script exhausted". Test the dispatch through `_dispatch` and the parse through the `ContractError` code — never by loosening the contract. |
| 2026-08-24 | scope-05 | New §9 entry beside the scope-04 one, for the same park with a different residue: the reviewer asks for an out-of-scope EDIT to be undone, and `REMOVE-OUT-OF-SCOPE:` cannot express it — deleting a file the base commit contains would be a worse overrun. `REVERT-OUT-OF-SCOPE: <path>` restores it from `task_base_sha`. The entry says outright not to reconstruct the content by hand: git holds it, and an agent-authored "put it back" is a new edit, not a revert. |
| 2026-08-24 | scope-05 | That entry ends with the check to run FIRST: if the prompt does not list `REVERT-OUT-OF-SCOPE:` among the request forms, no revert authority is wired for that run and the line does nothing — `cli._build_executor` has to pass `revert_authority=`. Report it rather than retrying the line. The scope-04 entry above is unchanged and still the right one for a file an earlier round CREATED out of scope. |
| 2026-08-24 | scope-05 | Revision round: that §9 entry's wiring check is rewritten, because production now passes `revert_authority=` from `cli._build_orchestrator`. The form is still rendered only when the round has a recorded out-of-scope path AND a usable base sha, so an absent `REVERT-OUT-OF-SCOPE:` in your prompt now means no execution record, no base sha on it, or an embedder that wired no authority — report it rather than retrying the line. |
| 2026-08-24 | contract-01 | New §6 entry for `invalid_json: Invalid control character at:` with a deep column offset — a literal newline inside the long `notes` value, which twice parked the loop `parse_budget_exhausted`. Filed beside the `no_json_block` entry because that is where contract-parse symptoms already live, though the cause is the model's encoding rather than the browser. The half worth knowing is that the KIND changed: 13 of the 25 historical parse errors were `no_json_block`, so a `parse_error` count alone will not show you this one. |
| 2026-08-24 | contract-01 | That entry's Fix says the recovery used twice — `run --answer` telling the reviewer to escape newlines and keep notes short — WORKS and then decays, because a conversational instruction lives in the thread and does not survive a rotation or a fresh session. `CONTRACT_INSTRUCTIONS` is re-sent every round, which is why the rule was put there instead. Do not answer a recurrence with another `--answer` alone. |
| 2026-08-24 | recov-01 | New §15 entry, filed FIRST in that section (newest-first): browser events, a genuinely launched Chrome and a `browser_restart_cooldown_blocked` park on a `codex_cli` run. The trap worth knowing is the remedy: the park's own advice — restart the browser, lower `browser.restart_cooldown_seconds` — cannot repair a subprocess fault, so following it wastes the investigation. Check `conversation.provider` and read `transport_error` / `codex_invocation_failed` instead. |
| 2026-08-24 | recov-01 | New §2 entry: an `inspect.getsource` test failing in a file you never touched, showing the WRONG function's body. Editing a module while its validation run is in flight shifts the lines under a seek that reads the file live, so the failure names an unrelated neighbour. Cost this round its last advisory run — two `test_task_inbox.py` tests "failed" against code that was correct. Check the named subject directly before debugging it. |
| 2026-08-24 | recov-01 | Same §15 entry gained the reachable variant: two consecutive codex failures on ONE request used to park `rotation_unavailable` telling the operator to set `browser.project_url`. Same trap, same remedy — that key is a browser setting and cannot help. `codex_cli` returns REJECTED on every non-zero exit, so this needs no exotic condition; the park is now `rotation_unsupported_by_transport` and names the transport. |
| 2026-08-24 | recov-01 | Same entry names the second, less obvious half: the `awaiting` phase is UNSATISFIABLE after a restart on `codex_cli`, because the reply lives in an in-memory dict. Persisting it is the wrong fix and was rejected; the transport already declares `idempotent_submit`, so the loop now re-runs the invocation. A recurrence on a transport WITHOUT that declaration is expected to keep waiting — that is not this bug. |
| 2026-08-24 | strand-01 | New §2 entry for the test trap this round hit: a `health.check` test that corrupts `config.state_file` to exercise the strand survey's "cannot tell which task is current" arm gets an exception instead of a verdict, because `check` is `_judge` then `_with_strands` and `_judge` reads the same file first. Assert against `health._strand_survey` and say why. The entry names the wrong fix outright — making the survey swallow the error is the fail-open it exists to close. |
