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
