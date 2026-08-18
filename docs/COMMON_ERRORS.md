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
