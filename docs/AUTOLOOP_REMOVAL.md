# Removing `autoloop/` from this checkout — the operator manifest

**This is language-app's document, not the harness's, and it survives the
removal it prescribes.** It lists nothing about itself on the `git rm` list
below. After the removal it stays as the record of what left and why — the same
disposition as the dated `docs/AUDIT_*.md` reports. Delete it only as a
deliberate, separate decision.

It lives here rather than in `docs/AUTOLOOP_TODO.md` because that file is the
harness's own TODO and is itself on the list; a manifest that goes away with the
thing it describes cannot be the handoff. Two files that survive the removal
point at this one: `docs/TODO.md` **#46** and the measurement note in
`docs/ROADMAP.md`.

---

## What port-05 did, and what it deliberately did not do

port-05 (2026-08-26) disconnected the harness from this repository's **tooling**:
`pytest.ini` no longer collects `autoloop/tests`, `ruff.toml` excludes
`autoloop`, and `.github/workflows/tests.yml` has no harness job. The files are
still on disk, untouched and merely unreferenced by any command this repository
runs.

Deleting them is one operator action and cannot be done by a loop round. The
executor has no delete tool for pre-existing paths outside a task's own scope,
and a 76,225-line deletion would blow past the review packet's 400,000-byte cap
by two orders of magnitude — brw-14 was refused at 416,193 bytes on 2026-08-24
*after passing review*, for a far smaller change.

**Precondition, non-negotiable:** do not run any of this until the extracted
repository has run `python -m autoloop doctor` **and a full round** against
language-app as an EXTERNAL target. Until then the harness lives here.

---

## Step 0 — the one path that BREAKS on removal. Fix it first.

`scripts/seed_validation_db.py:48` does
`from autoloop.validation_env import repo_declared_db_name`. It is the **only**
file outside `autoloop/` in this repository that imports the package, and it is
**language-app's** script — it seeds this repository's validation database with
the synthetic corpus ~55 backend tests need in order to assert rather than skip.
`git rm -r autoloop/` gives it an `ImportError` at line 48 before it reads a
single argument.

**Neither validation command catches this.** `ruff check .` does not resolve
imports, and nothing under `tests/` imports the script, so both stay green while
the script is dead. The failure surfaces the next time someone rebuilds a
validation database — exactly when they are least able to debug it.

The fix is small and self-contained: inline `repo_declared_db_name`, which reads
`.env.example`'s `DB_NAME` and returns it, and drop the `sys.path.insert` and the
`# noqa: E402` with it. Tracked as **#46** in `docs/TODO.md`, with the full
argument.

---

## Step 1 — remove. Autoloop-owned, unreferenced by this repository.

| Path | Note |
|---|---|
| `autoloop/` | the package and its tests, the whole tree |
| `docs/AUTOLOOP.md` | the harness manual (8,101 lines) |
| `docs/AUTOLOOP_TODO.md` | the harness's own TODO — open work on the loop, not on this app |
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

---

## Step 2 — keep. Referenced here, or about this repository.

| Path | Disposition |
|---|---|
| `docs/AUTOLOOP_REMOVAL.md` | **keep — this file.** It is the manifest, so it cannot be on its own list; it survives as the record of the removal. `docs/TODO.md` #46 and `docs/ROADMAP.md` both point at it and both survive. |
| `.gitignore`'s `.autoloop/` entry | **keep.** That is the loop's STATE AND CONFIG directory, not the package. The loop still runs against this repository as an external target and still reads `.autoloop/config.toml` here. Dropping it would untrack a private conversation URL and make the checkout read as dirty to the loop. |
| `docs/audit_charters.toml` | **keep.** It describes THIS repository and is read by the harness from the root of whatever checkout it audits. Its absence is a *supported* state that silently falls back to built-in domains, so losing it is invisible — which is why the `docs` CI job now checks it. |
| `.gitattributes` | **keep.** Deliberately rule-free; its prose is the record of why `merge=union` was tried and removed. It names `autoloop/note_merge.py` as a cross-repository pointer, which stays accurate. |
| `ruff.toml`'s `extend-exclude = ["autoloop"]` | **keep now, delete in the removal commit.** A pattern matching nothing is not an error for ruff, so it stays correct through the removal and is simply dead afterwards. Its comment names the `seed_validation_db.py` import — reword or drop that with Step 0. |
| `pytest.ini` | **keep, reword in the removal commit.** `testpaths` is already `tests` alone, but its comment names `autoloop/tests` and the `seed_validation_db.py` import as a pre-removal chore. Once Step 0 lands and the tree is gone, that paragraph is history and should say so — or go. |
| `requirements.txt`, `AGENTS.md`, `docs/SCHEMA.md`, `.github/workflows/dependency-audit.yml` | **keep unchanged.** None of them references `autoloop` at all. `requirements.txt` never declared the harness's dependencies (it is one `-r` line into the backend set); declaring them properly is the new repository's job. |
| `CLAUDE.md` §12's loop rules | **keep.** Change-note merge rules, the out-of-scope cleanup/revert authorities, `DELETE-FILE`, intake — these govern how a loop round must behave *in this repository*. They describe an external tool acting here, not a package shipped here. §11's ruff bullet names the `seed_validation_db.py` import; that sentence goes with Step 0. |
| `docs/SUMMARY.md`, `docs/TESTS.md`, `docs/COMMON_ERRORS.md`, `docs/SECURITY.md` autoloop sections | **keep until the removal commit, then excise the banner-marked blocks.** Each already carries a banner saying it describes a package this repository no longer owns. They were not deleted in port-05 because doing so alongside the tooling change would exceed the review packet cap. `docs/SECURITY.md` findings are never deleted (CLAUDE.md §14, regression history) — they travel with the code. |
| `docs/TODO.md` #45, #46 | **keep.** #46 is Step 0 above and closes with it. #45 is about guards this repository lost and has not fully restored — it outlives the removal. |
| `docs/AUDIT_2026-07-30.md`, `-08-02`, `-08-03`, `-08-05`, `-08-22` | **keep.** Dated audit reports. Historical records of what was true on a date; not descriptions of current structure. |
| `docs/ROADMAP.md`'s 2026-08-25 measurement | **keep as a dated measurement.** It quotes `autoloop/tests` 3,672 as of that date; annotated in place rather than rewritten, and its note points here. |
| `.autoloop/config.toml` | **not in this repository** — gitignored, operator-owned, and therefore not on any `git rm` list. It still needs a human pass at removal time: `browser.restart_command` (see the tombstone above), `state_dir`, and the `[repo]` paths all point at a layout that is about to change. Nothing in a checkout can make that edit for you. |
| `.github/workflows/tests.yml`'s `docs`-job comment | **keep the job, touch up the comment in the removal commit.** The header names `autoloop/tests/test_docs_merge.py` and `autoloop/tests/test_audit_charters.py` to explain where those checks came from. Those paths stop existing at the `git rm`, so the comment should be reworded to past tense then. The checks themselves are about `docs/` and stay. |

---

## Step 3 — after the `git rm`

Five files keep a comment that names a path which stops existing. None of them
breaks — they are all comments — but each should be reworded in the same
commit. Measured 2026-08-26 by a case-insensitive sweep for `autoloop` over the
tooling files:

| File | What goes stale |
|---|---|
| `pytest.ini` | the paragraph about `autoloop/tests` and the `seed_validation_db.py` import (dead once Step 0 lands) |
| `ruff.toml` | the same import note, plus `extend-exclude = ["autoloop"]` itself, which becomes a pattern matching nothing |
| `.gitignore` | the comment on the `.autoloop/` entry says the package is "on its way out of this checkout" — the ENTRY stays, the tense changes |
| `.github/workflows/tests.yml` | the `docs`-job header names `autoloop/tests/test_docs_merge.py` and `autoloop/tests/test_audit_charters.py` as where those checks came from |
| `CLAUDE.md` | §11's ruff bullet names the `seed_validation_db.py` import; §12's change-note rules cite `autoloop/note_merge.MAX_NOTE_LINE_CHARS` as the authority and `autoloop/tests/test_docs_merge.py` as the pin. The rules stay — the pin does not run here any more, and docs/TODO.md **#45** is the open item about that |

Then re-run the two validation commands (`ruff check .`,
`python3 -m pytest tests/ -q`) and sweep the tree for `autoloop` once more, case
insensitively. What should legitimately remain: this file; the dated
`docs/AUDIT_*.md` reports; `.autoloop/`-the-state-directory in `.gitignore`;
`docs/SECURITY.md`'s findings and their verification commands (never deleted —
CLAUDE.md §14); and CLAUDE.md's rules about how a loop round behaves here.
