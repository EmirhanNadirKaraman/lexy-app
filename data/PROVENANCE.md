# data/ — provenance, licence basis, and product use

Why this file exists: `data/` held vocabulary files with **no README, no LICENSE,
no attribution, and generic commit messages** (`Add b1 word list and support`).
A 2026-07-27 audit had to infer each file's nature from its byte structure, and
wrongly concluded the files were blocked pending a licensing review. They are
not. This file records what each file actually is, so that conclusion is not
re-derived.

---

## Licence basis

**The project owner holds distribution licences for the vocabulary files listed
below** (stated 2026-07-27). Product use, including distribution, is cleared on
that basis.

**Evidence status — what is recorded here vs. what is not.** Everything in the
*content* sections below is measured directly from the files and is
reproducible. The licence basis is the owner's assertion; **no licence artifact
is stored in this repo**. To make it self-supporting, someone with the paperwork
should fill in, per file:

- [ ] the upstream source (publisher / platform / dataset name)
- [ ] the licence or permission reference (agreement, receipt, terms URL, date)
- [ ] any attribution string the licence requires

Until those are filled in, treat the clearance above as "cleared by the owner",
not as "documented in the repo". Do not restate an upstream source name anywhere in the
codebase that is not written down here — the audit's structural guesses about
possible upstreams were **not** confirmed and are deliberately not recorded.

---

## Files

### `words_4000_old.txt` — **preferred source. Do not be misled by the name.**

**This is not an old version of `words_4000.txt`. It is the full-fidelity
source**, and the single most valuable vocabulary asset in the repo (453 KB vs
44 KB).

11-column TSV, 4,095 entries:

| Col | Content | Coverage |
|---|---|---|
| 0 | headword, nouns carry their article (`die Kasse`) | 100% |
| 2 | English translation | **100%** — all ≤40 chars, i.e. gloss-shaped |
| 4 | example sentence | **100%** |
| 5 | plural / full conjugation (`trägt vor; trug vor; hat vorgetragen`) | 71% |
| 8 | POS — 1,851 noun, 1,061 verb, 681 adj, 249 adv, 76 pron, 53 prep, 31 num, 25 conj, 12 part | 99% |
| 9 | group 1–169 | 100% |
| 10 | per-item numeric id | 100% |

- **Ordering is frequency-descending** (group 1 = `ein, zu, im, auf, werden,
  ich`; group 169 = `das Weib, das Volumen, das Vitamin`). Use *this ordering*
  as the frequency rank.
- **Field 9 is a grouping, not a rank** — 169 groups, 116 of them exactly 25
  items. If chunking is wanted, these are the source's own chunks and are more
  defensible than inventing 1–500 bands.
- 0 duplicates, clean UTF-8 umlauts, no punctuation-only lines.

**Use as:** the source of truth for the German 4000 list, for seeding
`word_table`, and for pre-seeding the gloss cache.

### `words_4000.txt` — redundant

4,096 lines; **strictly column 0 of `words_4000_old.txt`** (same set; one extra
line). Carries no translation, example, POS or conjugation. 14 duplicates.

**Use as:** nothing, while `words_4000_old.txt` is present. Prefer `_old`.

### `b1_unparsed.txt` — B1 entry set

2,840 bare headwords, fully alphabetical (sortedness 1.00), no articles, 29
duplicates. Contains **815 entries that `b1_parsed.txt` does not** (`Abfahrt`,
`Abflug`, `Abgas`, `Ampel`, `Ankunft`, …).

**Use as:** the B1 **entry set** — it has the better coverage.

### `b1_parsed.txt` — B1 enrichment

2,032 entries. A **strict subset** of `b1_unparsed.txt`: it adds **zero**
headwords and is missing 815 of them. What it adds is the information a learner
actually needs — **gender** (`die Abbildung`) and **verb valency**
(`etw. (Akk) bei jdm. abgeben`).

The subsetting is mechanical, not editorial: `scripts/b1_word_finder.py` joins
`b1_unparsed.txt` against `final_result.txt`, so the 815 missing entries are
simply those absent from that dictionary.

**Use as:** enrichment over `b1_unparsed.txt`. Keep both — neither dominates.

---

## Other files in this directory

| File | What it is | Product use |
|---|---|---|
| `final_result.txt` | 5,156-row TSV German verb + valency dictionary. **Read at import by `subtitle-scraper/phrase_finder.py` and by `lexy-app/backend/main.py`.** | Infrastructure — not a word list. Do not repurpose casually. |
| `real_final_result.txt` | 4,089 lines; column 1 of `final_result.txt` | Derived; no independent value |
| `known_words.txt`, `known_words_old.txt` | 771 / 1,008 entries — one person's personal study history | **Not** product list material. `known_words.txt` is also read by `lexy-app/backend/tests/test_free_chat_progression.py`. |
| `verbs.txt` | 1,061 bare infinitives | Narrow; superseded by `words_4000_old.txt`'s 1,061 verbs, which carry conjugations |
| `gemini_answer.txt` | 1,062-row CSV `verb,usage` — LLM output | Derived |

---

## Implementation notes (measured 2026-07-27)

**Seed `word_table` before building any system list.** Only ~40% of these
headwords currently resolve against `word_table`, because the German catalog
(6,962 rows) is built from scraped YouTube subtitles and lacks ordinary
vocabulary — `Abbildung`, `Abenteuer`, `Abfall`, `Adresse` are all missing. A
list built today would read ~60% "unresolved". Importing the 2,432 absent
entries is a ~44% catalog expansion.

**Import with headword, POS and lemma** from `words_4000_old.txt`. This gives
real POS rather than the sparse `pos='X'` rows that
`word_service.learn_word_anyway` creates.

**Pre-seed the permanent gloss cache** from column 2.
`llm_service.translate_item_gloss` is one permanently-cached LLM call per item
on the SRS due-card path; 4,095 human-quality glosses already exist here.

**Example sentences (column 4) can later preload example generation**, if the
cache path supports it. Lower priority than the gloss seeding.

**System lists need a schema change first.** `word_lists.user_id` is `NOT NULL`
with `ON DELETE CASCADE`, so every list is user-owned; there is no
system/default/shared list concept yet.

**Do not use `word_table.frequency` as a general-German frequency rank.**
Migration 032 defines it as "number of sentences the word appears in" — i.e.
app-corpus frequency over scraped subtitles. Measured: German has 5,558
alphabetic words with 63% appearing in ≤1 sentence; by that ranking rank 500 is
`Hongkong`, rank 1000 `Olafs`, rank 2000 the English word `trust`, and
punctuation ranks top-2. Use `words_4000_old.txt`'s own ordering instead.

**Labelling.** Name lists after what the data supports. `onboarding.py`'s
`LevelTier.A1/A2/B1` are self-described as labelling CEFR *informally* with
approximate boundaries, so those tiers are **not** a CEFR source — that is an
accuracy point, independent of licensing.
