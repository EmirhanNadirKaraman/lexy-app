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

### `final_result.txt` — **the central source, and already load-bearing**

**This file is production infrastructure, not a candidate list.** It is read at
backend startup: `main.py` → `matcher_service.get_blueprint_map()` →
`phrase_service.seed_from_blueprint_map()`, which seeds `phrase_table`. It is
also read at import by `subtitle-scraper/phrase_finder.py` for German phrase
extraction. **Changing it changes the live phrase catalog** — treat edits as a
production change, not a data tweak.

2-column TSV, 5,156 rows (every row has exactly 2 columns):

| Col | Content | Notes |
|---|---|---|
| 0 | headword / lemma, nouns carry their article | **4,075 unique** after article-stripping; 1,850 are nouns |
| 1 | phrase blueprint, or the headword repeated | **5,037 unique**; 950 differ from col 0 (the real phrase patterns); 994 carry valency markers (`etw.` / `jdn.` / `jdm.` / `sich`) |

Rows where col 0 == col 1 (4,206) are plain words. Rows where they differ (950)
are verb-valency blueprints — `haben` → `etw./jdn. (Akk) haben`, `geben` →
`jdm. (Dat) etw. (Akk) geben`. A headword may carry several patterns, which is
why col 0 has fewer unique values than there are rows.

**Coverage, measured 2026-07-27:**
- **100% of its 950 patterns are live in `phrase_table`** (2,829 German rows
  there in total).
- 51% of its 4,075 headwords currently resolve in `word_table` — the catalog is
  scraped-subtitle vocabulary and lacks ordinary words.
- **Its headwords are a superset of `words_4000_old.txt`'s**: 4,069 shared,
  **0 unique to `words_4000_old.txt`**, 6 unique to `final_result.txt`.

**Use as:** the source of truth for *which* German words and phrases exist —
the entry set for built-in lists, and the phrase half of them.

### `words_4000_old.txt` — **metadata enrichment for the same headwords**

**This is not an old version of `words_4000.txt`. It is the full-fidelity
metadata file** (453 KB vs 44 KB).

It and `final_result.txt` describe **the same headword set from two angles** and
join 1:1 on the headword — `final_result.txt` supplies the word/phrase
inventory, this file supplies translations, examples, POS and conjugations for
those same words. Neither replaces the other.

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

**Use as:** the enrichment layer — seeding `word_table` with real POS and
lemma, and pre-seeding the gloss cache from the translation column. Take the
*entry set* from `final_result.txt`, the *metadata* from here.

### `words_4000.txt` — redundant

4,096 lines; **strictly column 0 of `words_4000_old.txt`** (same set; one extra
line). Carries no translation, example, POS or conjugation. 14 duplicates.

**Use as:** nothing, while `words_4000_old.txt` is present. Prefer `_old`.

### `b1_unparsed.txt` — B1 entry set

2,840 bare headwords, fully alphabetical (sortedness 1.00), no articles, 29
duplicates. Contains **815 entries that `b1_parsed.txt` does not** (`Abfahrt`,
`Abflug`, `Abgas`, `Ampel`, `Ankunft`, …).

**Use as:** the B1 **entry set** — it has the better coverage. Supplemental to `final_result.txt`: it contributes **815 headwords that `final_result.txt` does not have**, while `final_result.txt` carries 2,050 that it lacks.

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
| `real_final_result.txt` | 4,089 lines; column 1 of `final_result.txt` | Derived; no independent value |
| `known_words.txt`, `known_words_old.txt` | 771 / 1,008 entries — one person's personal study history | **Not** product list material. `known_words.txt` is also read by `lexy-app/backend/tests/test_free_chat_progression.py`. |
| `verbs.txt` | 1,061 bare infinitives | Narrow; superseded by `words_4000_old.txt`'s 1,061 verbs, which carry conjugations |
| `gemini_answer.txt` | 1,062-row CSV `verb,usage` — LLM output | Derived |

---

## Implementation notes (measured 2026-07-27)

**The built-in lists should be word *and phrase* lists, not word lists.**
`final_result.txt` carries 950 phrase blueprints alongside its 4,075 headwords,
and all 950 are already live in `phrase_table`. A built-in list drawn from this
file that silently dropped the phrases would discard the half of the data that
is already wired.

**Phrase support in the list feature is NOT complete today.** The schema is
ready and the progression path is ready; the service is not:

| Layer | State |
|---|---|
| `word_list_items.item_type` | ✅ column exists, already polymorphic |
| `progression_service.apply_progression` | ✅ already called with `entry["item_type"]` — words and phrases share the `free_chat_*` / `status_marked_*` events |
| SRS / review | ✅ `review_service.get_due_cards` joins the per-type display table |
| `word_list_service._resolve_surfaces` | ❌ queries `word_table` only |
| `word_list_service.create_list` | ❌ hardcodes `"word"` as the inserted `item_type` |
| `word_list_service._load_entries` | ❌ late-status lookup pins `item_type = 'word'` |
| Frontend | ❌ no per-type display; a phrase would render as a plain surface |

**No migration is needed** — this is service + frontend work. Export needs no
change (it emits stored surfaces). `mark-unknown-learning` needs the resolved
`item_type` threaded through, which `apply_progression` already accepts.

**Seed `word_table` before building any list.** Only ~51% of
`final_result.txt`'s headwords resolve against `word_table` today, because the
German catalog (6,962 rows) is built from scraped YouTube subtitles and lacks
ordinary vocabulary — `Abbildung`, `Abenteuer`, `Abfall`, `Adresse` are all
missing. A list built first would read roughly half "unresolved".

**Import with headword, POS and lemma** — entry set from `final_result.txt`,
POS and lemma joined from `words_4000_old.txt` on the headword. This gives real
POS rather than the sparse `pos='X'` rows that
`word_service.learn_word_anyway` creates.

**Pre-seed the permanent gloss cache** from column 2.
`llm_service.translate_item_gloss` is one permanently-cached LLM call per item
on the SRS due-card path; 4,095 human-quality glosses already exist here.

**Example sentences (column 4) can later preload example generation**, if the
cache path supports it. Lower priority than the gloss seeding.

**System lists needed a schema change first — it shipped.** That change was
migration **037** (2026-07-28): `word_lists.user_id` is now **nullable**, paired
with `is_system BOOLEAN NOT NULL DEFAULT false` and a CHECK constraint
(`word_lists_owner_ck`) that makes an owned "system" list and an ownerless
private list unrepresentable. `system_list_seed_service` seeds the built-ins.
See `docs/SCHEMA.md` §"Word lists: ownership". *(Corrected 2026-08-25 — the
paragraph below this line described the pre-037 shape as current.)*

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
