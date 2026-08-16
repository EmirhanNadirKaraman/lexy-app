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

**A config still naming the script is refused at load** with the replacement
line in the message (`config._refuse_retired_restart_script`) — not left to
surface as bash's exit 127 in the middle of a browser fault. The `.autoloop/`
config is not in the repository, so this is a hand edit each deployment makes
once. The script itself is a failing tombstone: it restarts nothing, names the
replacement on stderr and exits 1. The loop can no longer reach it at all — that
refusal closes the route — so the tombstone is for the path still being typed by
hand, out of shell history, or by a wrapper of your own. Non-zero deliberately:
both callers surface `result.stderr` only on a non-zero exit, and this is the
file that taught us what a zero exit costs.

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
that_only_exists_in_the_quarantined_repo`.

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
