# INGESTION_PIPELINE.md

Design for the document-ingestion boundary: an offline `nlp-histo` worker produces a versioned
**document package**; the language-app backend validates and imports it, reconstructs continuous
text, segments it into sentences, and serves a **sentence-by-sentence reader**.

Status: **design approved 2026-07-29. A1 and A2 shipped 2026-07-29; everything
else not started.** Tracked as ROADMAP **N7** / TODO **#44**. Task board: §13.

---

## 1. Why

The reader's unit today is a *block*. Sentences are split in the browser by
`/(?<=[.!?])\s+/u` (`BookReaderPage.tsx:24`), counted, and PATCHed back as a bare integer
(`book_pages.sentence_count`, migration 012). No sentence text, IDs, offsets, or provenance exist
server-side, and `book_blocks.block_type` is only ever `'text'` (`book_service.py:431`).

A sentence reader needs a real sentence entity with stable identity and provenance back to the
page. That is the goal; everything below serves it.

### What already exists, precisely

| Layer | Reality |
|---|---|
| Shipping `book_service` | PyMuPDF + Tesseract. Real bboxes, `ocr_confidence`, `is_header_footer`. No layout labels, no reading order, no offsets, no sentences. |
| `files/json/*_layout.json` | 27 real Docling layouts (`type`, `page`, `level`, `bbox`), produced by `masking/latest_ingest.py` — **now broken** (imports a `parsers` package absent from this repo). |
| `~/Documents/GitHub/nlp-histo/` | The real, installable extraction pipeline. Own `.venv` (2.2 G) with docling, torch, transformers. Working CLI, tests, and an eval harness. |
| `language-app/nlp_histo/` | An **untracked vendored copy** of the extraction stage. See §9. |

---

## 2. Evidence

Measured over the 27 German graded readers in `files/json/` (1699 pages, 27459 elements), using
aggregate statistics only. Runs with **no Docling installed** — this doubles as the regression
harness (§10).

**Extraction is not as clean as assumed:**

| Signal | Measured | Means |
|---|---|---|
| Prose blocks lacking terminal punctuation | **9440 / 20416 (43.5%)** | Paragraph reconstruction not happening |
| Blocks starting lowercase | 21.6% | Mid-sentence continuation |
| Unterminated block immediately before a page break | **259** | Cross-page joins, countable today |
| Bare-number `TEXT` blocks vs `PAGE_HEADER` labels | **1964 vs 5** | Page numbers/headers not removed |
| `CHECKBOX_UNSELECTED` blocks holding ordinary German words | **1307** | Misclassification |
| `LIST_ITEM` share | 5650 (25%) | Fiction dialogue misread as list items |

**`nlp-histo`'s linguistics are English-biomedical and do not transfer.** Measured against
`language-app/nlp_histo/parsers/`, verified **byte-identical** to `nlp-histo/src/nlp_histo/parsers/`
(`diff -rq`), so these characterise the worker repo:

| Function | On German fiction | Verdict |
|---|---|---|
| `is_relevant_para` (default `pre_filter=True`) | **drops 30.5%** of prose (6228 blocks, mostly short) | **must be off** |
| `remove_citations` | silently mutates **9.1%** of blocks | **must be off** for fiction |
| `ContextAwareStitcher._is_cut_off` | fires on **4.0%** of unterminated blocks | English connector list; near-useless |
| German-tuned rule (lowercase-next ∪ German connectors ∪ hyphen) | **41.1%** recall | 10× better; residue is the AI's job |
| `_classify_paragraph` | types 61 dash-initial blocks as `table` | in fiction that's dialogue → false merges |

The mechanism behind the 30.5%: `is_relevant_para` (`parsers/layout_utils.py:360-398`) auto-passes
≥20 words, requires a verb **or a biomedical entity** for 4–19 words, and rejects <4 words.
German dialogue turns are mostly short, so they fail. `has_bio_entity` is essentially always
False outside biomedicine, so the filter silently degrades to verb-or-length.

**Deterministic German segmentation is already strong.** spaCy `de_core_news_md` — already a
dependency, already loaded by `nlp_service` — gets **7/8** hard cases right: `Dr.`, `z. B.`,
`usw.`, ordinal `am 3. Oktober`, `Nr. 5`, `„…"`, ellipsis. The **only** failure is missing
terminal punctuation across a line break.

> **This is the deterministic/AI boundary, derived by measurement:** determinism handles
> abbreviations and quotes; the AI handles missing punctuation and cross-block joins.

**Coordinate systems differ, 100% consistently.** See §6.

---

## 2b. Governing principle — preserve information, don't maximise standalone F1

**Adopted 2026-07-29. This overrides local optimisation in every deterministic
stage.**

The deterministic pipeline exists to hand the multimodal reviewer as much
usable information as possible. The reviewer resolves ambiguity — it cannot
recover what was already discarded. So the two failure modes are asymmetric:

| Failure | Severity | Why |
|---|---|---|
| Deleting genuine content | **irreversible** | The reviewer never sees it. |
| Retaining an artifact | recoverable | The reviewer deletes it in one operation. |

Deterministic stages should, in priority order: (1) preserve all genuine
content; (2) preserve provenance and geometry; (3) preserve reading order;
(4) remove only artifacts identifiable with very high confidence; (5) **mark
uncertainty rather than guess**.

Prefer keeping a suspicious short paragraph flagged `uncertain` over deleting it
because it resembles a running header. Prefer preserving an uncertain page
transition over joining paragraphs that may belong to different sections.
Prefer emitting confidence values over silently applying aggressive heuristics.

**Encoded in code, not just prose:** `FALSE_DELETION_WEIGHT` is 10×
`RETAINED_ARTIFACT_WEIGHT` in `evaluation/metrics.py`; a flagged-uncertain
artifact is discounted to 25% (cheap, not free — it still costs reviewer
attention); and `evaluation/compare.py` sorts a *critical* regression above any
numerically larger *normal* one. A 0.001 drop in `content_preservation_rate`
outranks a 0.20 drop in `boundary_f1`.

This is also why §2's measurements matter as a warning rather than a target:
`is_relevant_para` deletes 30.5% of German prose while looking locally
reasonable, and a stage tuned purely for its own segmentation score would do
the same thing.

### Two-stage evaluation

Measured **independently**, so a pipeline cannot buy Stage 2 with Stage 1 losses:

- **Stage 1 — deterministic fidelity:** content preservation, false deletions,
  provenance completeness, reading-order correctness, artifact removal.
- **Stage 2 — final reader quality** (after deterministic + AI review):
  reconstruction, sentence quality, boundaries, reader usability, provenance,
  residual artifacts.

### What the reviewer must receive

Rendered page image, extracted elements, hierarchy, reading order, bounding
boxes, provenance, confidence values, **uncertainty flags**, and neighbouring
elements — so it resolves ambiguity rather than re-deriving discarded facts.

---

## 3. Architecture and ownership

```
PDF
 └─ nlp-histo worker (own venv, GPU deps)   ── CLI now, queue worker later
 └─ DOCUMENT PACKAGE  (versioned, self-contained, on disk)
 └─ language-app import service              ── validate → stage → persist
 └─ [optional] multimodal review             ── gated by deterministic validators
 └─ reconstruction (German-tuned stitching, cross-page)
 └─ sentence segmentation (spaCy de + exceptions)
 └─ book_sentences → sentence-by-sentence reader
```

| `nlp-histo` owns | language-app owns |
|---|---|
| PDF parsing, Docling execution | Package validation + import |
| Page rendering, layout/geometry, masking | Optional multimodal review + op application |
| Block classification, initial reading order | Cross-page/cross-block reconstruction |
| Extraction-level cleanup, source provenance | Sentence segmentation + persistence |
| Producing the package | Translations, vocab, grammar, audio, notes, progress, navigation |

**The package is the only interface.** language-app must never import `nlp_histo`; the worker must
never touch the app database. This is what keeps GPU deps out of the API process, and it is
testable: an import test asserting `import nlp_histo` **fails** from the backend environment.

The contract is the *package on disk*, not the invocation — so a CLI today and a queue consumer
later require no schema change.

---

## 4. The document package

```text
document-package/<document_id>/
├── manifest.json          schema + extractor version, config digest, checksums, warnings
├── document.json          pages[] + elements[] + reading order + provenance
├── pages/0001.png …       rendered page images
├── assets/                cropped figures/tables (optional)
└── CHECKSUMS.txt          sha256 per file
```

### Why a new artifact rather than extending an existing one

`nlp-histo` writes outputs grouped **by type, not by document**: `out/text/{id}_text.txt`,
`out/json/{id}_media.json`, `out/run_metadata/{id}_stats.json`, `out/figures/*.png`, plus a
batch-level `run_{run_id}.json`. Nothing self-contained or portable exists.

Two of those writers carry explicit contracts forbidding this use — `manifest_writer.py` and
`stats_writer.py` are both documented as *observability-only, must never raise, must never
influence extraction*. A package manifest is load-bearing: a failed write **must** fail the run.
Different contract → **new writer** (`outputs/package_writer.py`), composing the existing ones:

- `manifest_writer` already produces run_id, git SHA + dirty flag, host, config snapshot and
  `config_digest` → reuse for `manifest.extractor`.
- `stats_writer` already tracks per-document counts, stable rejection codes (`R0_empty`,
  `R3_dense_text`, …) and `mark_skipped`/`mark_failed`/`mark_ok` → maps onto `manifest.counts`
  and `manifest.warnings`.
- `media_json_writer` already serialises cropped media → `assets/`.

> **Hook point:** the `OutputWriter.write(pmcid, rows, figures, tables, pdf_path)` protocol is
> **insufficient** — it never receives `LayoutResult`, so it cannot see `page_dims` or per-element
> bboxes for body text. The package writer must hook at `runner._process` instead.

### Three gaps in existing outputs the package must close

1. **No schema version reaches any file.** `pyproject` `0.1.0`, `schema_version="pdf_v1"` and
   `STAGE_CACHE_VERSION` all exist but none is stamped into a written payload. A consumer cannot
   tell which producer made a file. The package manifest fixes this first.
2. **Paths are CWD-relative.** `media_json_writer` writes `image_path` relative to the working
   directory, so the artifact breaks the moment it moves. Package paths must be package-relative.
3. **`page_dims` never reaches a writer.** It lives only on `LayoutResult`. A `BoundingBox` is
   meaningless without page height (§6), so the package must carry it.

### `manifest.json`

```json
{
  "package_schema_version": "1.0.0",
  "producer": "nlp-histo@0.1.0",
  "document_id": "de-graded-01-das-herz-von-dresden",
  "source": {"filename": "…", "sha256": "…", "bytes": 1234567, "page_count": 25},
  "extractor": {
    "git_sha": "…", "git_dirty": false, "docling_version": "2.65.0",
    "config_digest": "…", "profile": "german_fiction"
  },
  "created_at": "2026-07-29T12:00:00Z",
  "language": "de",
  "coordinate_space": "pdf_points_bottom_left",
  "page_images": {"dpi": 200, "format": "png", "path_template": "pages/{page_index:04d}.png"},
  "page_dims": {"1": {"width": 383.04, "height": 581.40}},
  "counts": {"pages": 25, "elements": 645, "warnings": 3},
  "warnings": [{"code": "unresolved_reading_order", "page_index": 11, "detail": "…"}]
}
```

`extractor.profile` is load-bearing: it records that the **German-fiction profile** ran (deletion
paths off), so import can *refuse* a package produced with the biomedical profile.

> **Provenance trap:** `manifest_writer._git_info()` uses `Path.cwd()`, not `__file__` — a run
> launched from language-app would record **language-app's** SHA. The package writer must resolve
> the worker repo explicitly.

### `document.json` element

```json
{
  "id": "p0011-e0007",
  "page_index": 11,
  "type": "paragraph",
  "docling_label": "TEXT",
  "level": 2,
  "text": "…",
  "bbox": {"x1": 72.0, "y1": 186.0, "x2": 521.0, "y2": 141.0,
           "page": 12, "coordinate_space": "pdf_points_bottom_left"},
  "reading_order": 42,
  "parent_id": "p0011",
  "paragraph_id": "para-0031",
  "confidence": {"classification": 0.98, "text": null},
  "provenance": {"extractor_stage": "layout", "docling_index": 137},
  "warnings": []
}
```

- **`type` is our own stable enum**, not Docling's label. 13 distinct labels appear in the corpus
  (`TEXT, LIST_ITEM, SECTION_HEADER, CHECKBOX_UNSELECTED, PICTURE, FOOTNOTE, CAPTION, TABLE,
  DOCUMENT_INDEX, FORMULA, CHECKBOX_SELECTED, PAGE_HEADER, CODE`). The raw label is kept as
  `docling_label`. `nlp-histo` stringifies labels defensively in four places precisely because
  they are version-fragile; the contract must not inherit that fragility.
- **`bbox` carries `page` explicitly.** `BoundingBox.to_dict()` drops it today and callers pass it
  as a sibling key — an easy source of mismatch. Pages are **1-based**.
- **`confidence` is an object with nullable members.** Classification confidence is largely absent
  and text confidence does not exist; honest nulls beat fabricated `1.0`s.
- **No internal Python objects are serialized.** No pickles, no dataclass dumps.

### `import_result.json`

Written by the app after an import attempt, making a re-run diffable and promoting warnings into
review work.

```json
{
  "import_schema_version": "1.0.0",
  "document_id": "…", "source_sha256": "…",
  "status": "imported", "doc_id": "8f3c…", "imported_at": "…",
  "counts": {"pages": 25, "elements_in": 645, "blocks_written": 611,
             "elements_skipped": 34, "sentences": 834},
  "skipped": [{"element_id": "p0001-e0003", "reason": "page_number", "stage": "import_filter"}],
  "warnings_promoted": [{"code": "unresolved_reading_order", "page_index": 11,
                         "action": "queued_for_review"}],
  "validation": {"schema": "pass", "checksums": "pass", "containment": "pass",
                 "profile": "german_fiction"},
  "errors": []
}
```

`status` ∈ `imported | rejected | partial_rejected`. `skipped` is itemised, not counted — "34
elements vanished" is the failure mode hardest to notice later, and this is what makes false
deletions auditable.

### Stable identifiers

1. **Element IDs** — `p{page:04d}-e{seq:04d}`, stable within a package version. Positional, and
   expected to change when extraction changes. That is fine; they are the *package's* identity.
2. **Document ID** — derived from the source file **sha256** plus a slug. Not the filename
   (renames), not a DB serial (unknowable by the worker). `nlp-histo`'s `document_id.py` is the
   right *pattern* but PMC-specific (`canonical_document_id` strips NLM version suffixes) — write
   a German-book equivalent.
3. **Sentence IDs** — the durable, user-facing ones. See §7.

---

## 5. CLI, import workflow, and validation

### Operator walkthrough (phase 1, manual)

```bash
# 1. Worker env — the only place GPU deps live
~/Documents/GitHub/nlp-histo/.venv/bin/nlp-histo ingest -- \
    --pdf-dir files/book_pdfs --glob "01.*.pdf" \
    --out-root "$PACKAGE_ROOT" \
    --no-db --no-main-pdf-only --profile german_fiction

# 2. Package lands at $PACKAGE_ROOT/<document_id>/ with manifest.json + CHECKSUMS.txt

# 3. App imports it (validates before touching the DB)
curl -X POST /api/v1/books/import -d '{"package_path": "<document_id>"}'
```

Note `nlp-histo ingest` takes **no flags of its own** — argv after the subcommand is forwarded to
the runner, and `nlp-histo ingest -- --help` prints the real option list.

`PACKAGE_ROOT` is an env var on the app side and paths resolve relative to it. The API never
accepts an absolute path from a client — that is what makes the containment check below
meaningful rather than advisory.

### Worker defaults that must change for German fiction

These are real traps, verified in `runner.py`:

| Setting | Default | Why it must change |
|---|---|---|
| `--main-pdf-only` | **ON** (`runner.py:1177`) | Groups by stem-before-first-underscore and **drops entire documents** that share a prefix. Built for multi-PDF PMC packages. |
| `filtering.apply_paragraph_relevance_filtering` | True | Routes through `is_relevant_para` → **30.5% deletion** (§2). |
| `filtering.apply_ner_filtering` | True | scispaCy biomedical NER; meaningless for fiction. |
| `skip_references_section` | **hardcoded `True`** (`runner.py:441`) | Not a config field. `_REF_HEADERS` is English-only exact-match. Must become configurable. |
| `masking.drop_tables_in_top_pts` | 50.0 | Assumes scientific tables never start near the page top. |
| `database.enabled` | `False` in dataclass, **`True` from the CLI** (`runner.py:1193`) | CLI and Python-API defaults diverge. Always pass `--no-db` explicitly. |
| `text.write_raw_text` | `False` in dataclass, **`True` from the CLI** | Same divergence. |

### Import validation gates, in order

1. `package_schema_version` compatible (semver major must match).
2. `CHECKSUMS.txt` verified; `source.sha256` matches the stored PDF if we have it.
3. **Path containment** — every image/asset path must resolve inside the package root. This is a
   zip-slip/traversal sink and the package may arrive from another machine. Precedent: the S7
   note in `routers/content_requests.py` (validate at the boundary before anything reaches the
   filesystem or a subprocess). **`docs/SECURITY.md` must be updated in the same PR.**
4. Structural: unique element IDs, `reading_order` a permutation without gaps or duplicates, every
   `parent_id` resolvable, bboxes within page dimensions, `page_index` contiguous.
5. Profile check: refuse a package whose `extractor.profile` is not a German-fiction profile.

**Checksums are mandatory (tightened 2026-07-29).** `CHECKSUMS.txt` is a required
member of the package layout, so **every** checksum failure is fatal and none of
them reach planning or persistence:

| Condition | Code |
|---|---|
| no `CHECKSUMS.txt` | `missing_checksums` |
| present but no usable entries | `empty_checksums` |
| line is not `<sha256>  <path>` | `malformed_checksum_line` |
| digest is not 64 hex chars (sha256 only) | `invalid_checksum_digest` |
| same path listed twice | `duplicate_checksum_entry` |
| `manifest.json` / `document.json` not covered | `unchecksummed_required_file` |
| entry names a path outside the package | `checksum_path_escape` |
| entry names an absent file | `checksum_missing_file` |
| digest does not match | `checksum_mismatch` |

The earlier behaviour warned-and-continued on a missing file, reasoning that an
early worker version should not be blocked for no safety gain. That was the
wrong trade: it made the one gate establishing *the bytes are what the worker
wrote* optional in practice, and "unverified" quietly became the common case.
Producer compatibility belongs in `package_schema_version`, where it is explicit
and negotiable — not in a validator that silently accepts less.

Only sha256 is supported, because it is the only algorithm the contract
documents. Adding another is a contract change, not a validator tweak.

**As implemented (A2).** Gates do **not** short-circuit on the first fatal —
independent defects are reported together, because someone fixing a worker bug
needs the whole list. The one exception is an incompatible schema version:
continuing past it produces a wall of misleading structural errors about fields
that legitimately changed shape.

Structural validation is additionally skipped when checksum verification proves
the *contents* untrustworthy (a mismatch, a bad entry) — its findings would
describe corrupted bytes and send someone chasing a bug that does not exist.
A merely **absent** inventory does not suppress it: the content still parsed and
is internally consistent, so its structural errors are real and are reported in
the same pass. Containment always runs; it is a security gate about declared
paths, and untrusted bytes are exactly when it matters.

Findings carry three severities. `fatal` blocks the import and no database
mutation is attempted; `warning` imports but is **promoted into
`import_result.json`** so it becomes review work rather than log noise; `info`
is recorded only. Severity follows §2b — anything that would make the app
*misplace or lose* content is fatal (duplicate ids, coordinate-space mismatch,
bbox off the page), while anything merely *unproven* is a warning (unknown
element type, missing confidence), because discarding the element would be the
irreversible choice.

**Dry-run** (`dry_run=True`, the default) runs every gate *and* builds the full
persistence plan, then writes nothing. It is predictive rather than a partial
rehearsal: `DryRunPersistence` reports the same page and block counts a real
import would. `POST /api/v1/books/import` returns **200 with a rejected result**
on validation failure — the body is the diagnostic; only an unusable request is
a 4xx.

**Persistence is a seam, not an implementation.** `PersistenceBackend` is the
Protocol A3 implements; `build_plan()` (shared by every backend) converts bboxes
to fitz space, assigns **dense** `block_index` per page, and itemises skipped
elements. Until A3 lands, a real import raises `PersistenceNotAvailable` naming
the blocking task rather than writing rows into a schema that has no
`reading_order` column and whose `block_type` is only ever `'text'` — writing
them would silently discard the two things the package exists to carry.

Import is **all-or-nothing per document**, in one transaction. A failure leaves
`book_documents.status='error'` with `error_message` (already truncated to 2000 chars at
`book_service.py:450`) and never partially-populated pages. Warnings do not block; they persist
and become review triggers (§8).

**Mapping into existing tables — no parallel models.** Package elements → `book_blocks`, with
`block_type` finally carrying real values, `bbox_*` converted to fitz coords at the boundary
(§6), and `reading_order` stored.

### Re-import semantics

Re-import of the same `source.sha256` is **versioned** — not rejected, not silently overwriting.
Extraction will improve, and surviving re-extraction is the whole point of `sentence_uid` (§7).

- Byte-identical package (same checksum) → no-op, returns the existing `doc_id`.
- Changed package → new version, runs the sentence remap, preserves `reading_selections` and
  progress by uid.
- A remap that would orphan more than a configured share of saved sentences → **fails the gate
  and reports**, rather than quietly discarding progress.

---

## 6. Coordinate conventions

Two systems, and the boundary must convert exactly once.

| System | Origin | y direction | Used by |
|---|---|---|---|
| **Docling PDF space** | bottom-left | up | package, `BoundingBox`, all worker output |
| **fitz screen space** | top-left | down | `book_blocks.bbox_*`, PyMuPDF, page images |

In Docling space the invariant is **`y1 > y2`** (y1 = top edge = larger value). Verified: 100% of
645 sampled elements. Units are PDF points throughout — never pixels, never normalised. Pages are
1-based.

The worker already ships the conversion pair (`models/dto.py`):

```python
to_fitz_rect(page_height)   → top = page_height - max(y1, y2);  bottom = page_height - min(y1, y2)
from_fitz_rect(rect, page_height, page)
```

`to_fitz_rect` is defensive (`max`/`min`); `from_fitz_rect` assumes `rect.y0 < rect.y1`. The pair
is exactly inverse only when that holds — the round-trip test must assert it.

**Both require an externally supplied `page_height`.** A `BoundingBox` is not self-describing,
which is why `page_dims` is mandatory in the manifest.

---

## 7. Sentences (migration 038)

The load-bearing decision:

> **Anchor to `(start_token_id, end_token_id)`, not character offsets into `clean_text`.**
> `book_service.retokenize_with_preservation` (`:327`) is an LCS that already keeps `token_id`s
> stable across a block edit — exactly the "don't break saved reading progress" requirement. Char
> offsets break on every repair edit. `reading_selections.anchors` already uses this shape, so
> stability is inherited rather than invented.

`book_sentences` columns: `sentence_id` SERIAL PK, `sentence_uid` (content-derived, for
cross-re-extraction remap), `doc_id`, `chapter_id`, `paragraph_id`, `doc_sentence_index`,
`para_sentence_index`, `text_verbatim`, `text_normalized`, `text_corrected`, `source_pages[]`,
`source_element_ids[]`, `source_block_ids[]`, `start_token_id`, `end_token_id`, `char_start`,
`char_end`, `boundary_confidence`, `repair_metadata` JSONB.

**The global index is not the persistent identifier.** `doc_sentence_index` drives prev/next
navigation; `sentence_uid` survives re-extraction via an explicit remap (match on uid, then fuzzy
text), and resume positions store the uid.

**Reader-unit suppression:** never emit a sentence whose source elements are all
header/footer/page-number, that is punctuation-only, or below `min_chars` (`subtitle_segmenter`
already uses 3).

Segmentation mirrors `subtitle_segmenter.py`, which already emits `char_start`/`char_end` per
sentence (`CandidateUtterance`) — a validated pattern rather than a new invention.

**Learner correction layer** (optional, separate): structured edits
`[{type, span, before, after}]` stored beside the verbatim sentence, never over it.

---

## 8. Reconstruction, AI review, and gating

### Reconstruction

German-tuned stitching replaces `_is_cut_off`'s English logic: next-element-starts-lowercase
(strongest signal — German capitalises all nouns), German connector/article tail, trailing hyphen,
soft-hyphen dehyphenation. Cross-page joins treat the page boundary as evidence, not a blocker.

Three carry-over fixes:
- `reconstruct_with_sources` accepts dicts but `_build_elements` keeps only the string
  (`'sources': [text]`), so "sources" are text values. We need **element IDs** — carry the dict
  through rather than re-matching by text.
- `_is_cut_off` treats `»` as sentence-final; German uses `»` as an *opening* quote, and U+201C
  (the closing half of `„…"`) is missing entirely.
- `extract_text` (`layout_utils.py:437`) uses `page`/`bbox` for filtering then drops them from its
  output rows (`:515`). `HierarchicalRow` has no bbox, page, or element id at all — so a sentence
  cannot currently be mapped back to a rectangle. Provenance must be carried through.

### Deterministic validators gate the LLM

They never call it, they are the cheapest item, and the harness already exists — the §2 queries
are the prototype. Signals: unterminated-block rate, lowercase-start rate, bare-number blocks,
overlapping bboxes, non-monotonic reading order, `CHECKBOX_*`/`LIST_ITEM` anomaly rate, package
warnings, per-page outliers versus the book's own distribution.

Policy: review flagged pages only, plus a ~2% whole-book sample floor so systematic failures are
not missed on locally-plausible pages.

### AI review

Page-level: it matches one image, caches cleanly, and fails in isolation. Input is the rendered
page image plus a projection of that page's elements (id, type, level, bbox, reading_order, text,
confidence, warnings).

Closed operation vocabulary: `delete_element`, `reclassify_element`, `merge_elements`,
`split_element`, `reorder_elements`, `replace_text`. Only `replace_text` alters characters, and it
must carry `expected_text` — if that does not match current text exactly the op is rejected, which
also makes application idempotent, order-independent, and conflict-detecting.

Guards making "must not rewrite the story" mechanical rather than a prompt request:
- edit-distance bound per `replace_text` (≤15% of element length, absolute cap ~20 chars);
- token-multiset check, catching summarising/modernising that passes a length check;
- per-page deletion budget (≤30% of elements) — exceeding it fails the whole page's op set;
- `reason` is an **enum**, making failure modes countable and usable for future fine-tuning.

Rejected ops are recorded, not discarded — they are the false-correction dataset.

**Audit and rollback:** ops live in their own table; derived text = verbatim + applied ops.
Rollback is re-derivation with ops disabled, not an inverse-op engine. Verbatim is never
overwritten, mirroring the existing `corrected_text` / `correction_status` channel.

**Caching:** `llm_cache_service.make_cache_key("book_page_review", model, {page_image_sha256,
elements_payload_sha256, profile})`. Folding both hashes means an extraction change invalidates
the review. Note `llm_cache` is global with no user FK and tests tag rows `zztest-model-{worker}`
— never clean it by `prompt_key`.

**Multimodal enablement:** `llm_provider.structured()` already passes `messages` through verbatim
to both backends, so an image can reach the wire today; the problem is the wire formats diverge
(Anthropic `source.base64` vs OpenAI `image_url` data-URI). The change is a **neutral image
content-block normalizer inside `llm_provider`**, keeping the seam's promise (CLAUDE.md §12).

**Rate limiting:** 1699 pages far exceeds the per-user limiter (30/min, 400/hr, in-process).
Ingestion review needs its own budget path.

---

## 9. Migration

1. Keep the PyMuPDF path as-is and default.
2. Add package schema + validator + `book_import_service` (no AI).
3. Import worker output for one book behind a flag; compare against the existing path.
4. Benchmark both paths on the stratified sample (§10).
5. Add reconstruction + server-side segmentation → `book_sentences`; reader reads sentences.
6. Add optional AI review, gated by §8.
7. Decide whether PyMuPDF stays as fallback — likely **yes**, for user uploads with no worker run.

### The vendored copy

`language-app/nlp_histo/` (46 files, 8587 LOC) is untracked (`?? nlp_histo/`) and **not**
gitignored, and `ruff.toml` has no `exclude` — committing it would make `ruff check .` lint
another project's source, which CLAUDE.md requires to pass cleanly.

It is **not** a stale copy: it differs from `nlp-histo` by exactly one added file
(`config_hash.py`) and one changed import (`runner.py:98`), which severs the extraction stage's
dependency on `knowledge_extraction`. Both hash implementations are logically identical
(sha256[:16] over the same canonical JSON), so **the same config yields the same hash in both
repos** and the stage cache survives. That decoupling should be **upstreamed to `nlp-histo`**,
then the copy deleted.

> **Defect to fix before sharing any stage cache:** `runner._get_nlp()` swallows `ImportError` and
> returns `None`, so filtering silently runs without scispaCy — while `_compute_config_hash()`
> still stamps `"nlp_model": "en_core_sci_sm"` into the cache key. The hash claims NER ran when it
> did not, poisoning any cache shared between the two repos.

### Migration traps

**Do not add a unique constraint on `(page_id, block_index)` in the same step as the backfill.**
`block_index` on the native path is PyMuPDF's `block_no` with image and artifact blocks skipped
and *not* renumbered (`book_service.py:142`), and no such constraint exists today. Whether live
rows contain duplicates is **unverified**. Migrations are append-only, so a constraint that fails
on real data is expensive to unwind. Sequence it: 038 adds columns, backfills dense indices, and
*reports* duplicates; a later migration adds the constraint once verified clean.

`book_pages.image_path` is currently non-null **only for scanned pages**, and the frontend keys
`has_image` off it. Populating it for all pages flips that behaviour everywhere — gate on a new
column or update the frontend deliberately, not incidentally.

---

## 10. Evaluation

Ground truth: **~200 manually reviewed pages**, stratified across the 27 books (clean / flagged /
scanned), not whole books.

Metrics: segmentation **boundary F1 + WindowDiff/Pk** (not bare "accuracy"); separately counted
**false merges, false splits, false corrections, false deletions** — false deletions weighted
hardest, since §2 shows that is where the damage is; cross-page join accuracy; throughput,
latency, peak VRAM, wall-time per page.

**Reuse `nlp-histo/eval/` — a real, human-labelled harness.** It has `ground_truth.py` +
`ground_truth.csv`, `annotate.py` / `auto_annotate.py`, `label_rubric.yaml`,
`precision_recall.py`, an `llm_judge/`, 28 sample PDFs, and ~28 per-variant annotation
directories (`00_docling_offtheshelf`, `01_docling`, `02_tatr_090`, …) forming a full ablation
grid. That **per-configuration layout is exactly the structure** the three-way comparison needs.

What does **not** transfer: the metrics. `ground_truth.csv` is 29 rows of
`pmcid, missed_figures, missed_tables, total_tables` — crop detection, not text quality. The
harness has **no ground truth for text, reading order, or hierarchy**, so German-sentence metrics
are genuinely new work. The `llm_judge/` pattern is reusable for adjudicating ambiguous
boundaries, but must be scored against human annotation, never treated as ground truth.

**Do not promise byte-reproducibility.** `runner._seed_pipeline()` seeds `random`/`numpy`/`torch`
but explicitly disclaims determinism from Docling, TATR, OCR, and spaCy.

**Licensing constraint:** `files/` is gitignored and holds commercial graded readers. Annotations
stay local; anything committed or shared uses public-domain German text (Gutenberg-DE,
Wikisource). All analysis here used aggregate statistics only — keep that discipline.

---

## 11. Disk and dependencies

Measured, and it inverts the usual framing — **the offline-worker split costs zero additional
disk, because the environment already exists**:

| | Size | Contents |
|---|---|---|
| `nlp-histo/.venv` | **2.2 G** | docling 2.65.0 pinned (2.66.0 installed), torch 2.9.1, transformers 4.57.3 |
| `language-app/.venv` | **135 M** | stays this size |
| `~/.cache/huggingface` | 1.1 G | single shared location |

Disk is **94% used, 29 G free** on the data volume. Adopting Docling into the backend would *add*
~2.2 G by duplicating Torch. Both interpreters are Python 3.12.0, so a subprocess handoff is clean.
Note `nlp-histo`'s `pyproject.toml` declares **no** dependencies by design — `requirements.txt` is
the tested source of truth (`pip install -r requirements.txt && pip install -e . --no-deps`), and
`requires-python` is capped at `<3.13` by scispaCy.

Rules:
- **One model-cache location** (`HF_HOME`, `TORCH_HOME`, docling artifacts) shared across projects.
  Today **nothing in `nlp-histo` sets any of these** — caches land in process defaults. Making this
  explicit is net-new work, not a config tweak.
- Set `output_root` / `files_root` explicitly; every `PathConfig` default is CWD-relative, so
  output lands wherever the process was launched.
- One document at a time initially — the module-level singleton guard in `content_requests.py` is
  the precedent.
- Page images and crops are **regenerable**: delete after successful import, regenerate on demand
  for review. At 200 dpi the page PNGs dominate; measure on book #1 before scaling to 1699 pages.

---

## 12. Risks and open questions

**Risks.** (1) False deletions — the worst outcome; mitigated by profile gating, deletion budgets,
and weighting them hardest in eval. (2) Coordinate-flip errors — one conversion point, tested both
directions. (3) Reading order is inherited from Docling unvalidated; element order is
`iterate_items()` sequence, not geometric. (4) Package/DB drift as extraction improves — mitigated
by `sentence_uid` remap. (5) Vendored-copy drift — resolved by upstreaming then deleting.

**Open, to decide during implementation.** Whether the worker also emits per-word geometry
(`_ocr_page_png` collects per-word bbox and confidence then discards it — recapturing enables
word-level overlays but enlarges the package). Whether chapter detection is worker-side (it has
`SECTION_HEADER` + `level`) or app-side. Package retention period.

**Alternatives rejected.** In-backend Docling — costs 2.2 G duplicate Torch and contradicts the
deployment boundary. Flat `layout.json` only — no checksums, versioning, images, or warnings.
Direct DB writes from the worker — couples the repos and breaks the "package is the only
interface" test.

---

## 13. Task breakdown

### `nlp-histo` tasks
| # | Task | Files |
|---|---|---|
| H0 | Upstream the `config_hash` decoupling already present in the vendored copy; fix the `nlp_model` hash lie in `_get_nlp()` | `runner.py:98,492-501`, `config_hash.py` |
| H1 | German-fiction profile: relevance/NER filtering off, `remove_citations` off, `skip_references_section` made configurable | `config.py`, `runner.py:441`, `parsers/layout_utils.py` |
| H2 | German stitching rules in `_is_cut_off` (lowercase-next, German connectors, `»`/U+201C) | `parsers/text_processing.py:127`; extend `tests/parsers/test_text_processing_cutoff.py` |
| H3 | Carry `page`/`bbox`/element-id through assembly (dropped at `:515`; `HierarchicalRow` has none) | `layout_utils.py:437-540`, `models/dto.py:124`; extend `tests/pdf_text_extraction/test_extract_text_ordering.py` |
| H4 | `outputs/package_writer.py` — manifest + document + CHECKSUMS, hooked at `runner._process` (not the `OutputWriter` protocol, which lacks `page_dims`) | new; composes `manifest_writer`, `stats_writer`, `media_json_writer` |
| H5 | **Full-page image rendering — net-new.** No page PNG is written today; only crops. Needs a `page_dpi` field and a page-image dir in `PathConfig` | new component; `config.py:81-119` |
| H6 | Profile flag + generic-document defaults (`--no-main-pdf-only`, `--no-db`) on the existing `nlp-histo ingest` CLI | `runner.py:1079-1289`; pattern from `test_cli_cropping_flags.py` |
| H7 | Stable document-ID derivation for non-PMC documents | new, patterned on `document_id.py` |

### Shared contract tasks
| # | Task |
|---|---|
| C1 | Versioned JSON Schemas (manifest, document, import result), committed in both repos |
| C2 | Stable `type` enum + Docling-label mapping (13 labels) |
| C3 | Coordinate-space spec + conversion helper with round-trip tests |
| C4 | Golden fixture package (small, public-domain) in both test suites |

### language-app tasks
| # | Task | Files |
|---|---|---|
| ~~A1~~ | ✅ **SHIPPED 2026-07-29** — evaluation harness + benchmark corpus. Two-stage metrics (§2b), severity-ordered comparison, 147 tests. No LLM, no `nlp_histo` import. | `evaluation/` (11 modules), `benchmark/documents/` (4 docs), `tests/evaluation/` |
| ~~A2~~ | ✅ **SHIPPED 2026-07-29** — `verify → validate → dry-run → persist`. Five gates in order, all findings collected (fatal/warning/info), `import_result.json`, containment chokepoint, persistence seam for A3. 107 tests. | `services/book_import_service.py`, `services/document_package/` (7 modules), `routers/books.py`, `tests/test_document_package.py` |
| A3 | Migration 038: `book_sentences`, `book_blocks.reading_order`, `block_index` backfill (**not** the constraint — see §9) | `migrations/versions/038_*.py` |
| A4 | German-tuned reconstruction (stitching, cross-page, dehyphenation) | new `reconstruction_service` |
| A5 | Server-side segmentation (spaCy `de` + exceptions), mirroring `subtitle_segmenter` | new `sentence_service`; `nlp_service.py` |
| A6 | Reader reads sentences; retire client `splitSentences()` | `BookReaderPage.tsx:24` |
| A7 | `llm_provider` image content-block normalizer | `services/llm_provider.py` |
| A8 | `page_review_service` + op validation/application/rollback + audit table | new service + migration |
| A9 | Ingestion LLM budget path separate from the per-user limiter | `core/deps.py`, `services/rate_limiter.py` |
| A10 | Worker invocation via `INGEST_WORKER_PYTHON` + SSE notify | patterned on `routers/content_requests.py:23-30` |
| A11 | Delete vendored `nlp_histo/` (after H0 upstream); delete `pdf_text_extraction/` + `masking/`, salvaging `pdf_text_extraction/resources.py` (`ModelRegistry`) | repo root |

### Evaluation tasks
| # | Task |
|---|---|
| E1 | Stratified 200-page ground truth (local only) — extend `eval/ground_truth.py` + `label_rubric.yaml` with text-quality labels |
| E2 | Metrics: boundary F1, WindowDiff/Pk, false merge/split/correction/deletion (**new** — existing metrics are crop-only) |
| E3 | Three-way comparison as `eval/annotations/{NN_pymupdf, NN_package_german, NN_package_ai}/`, reusing the existing per-variant layout |
| E4 | Throughput / latency / VRAM / disk-per-book measurement |
| E5 | Adapt `eval/llm_judge/` for ambiguous-boundary adjudication, scored against human annotation |

**A1 first** — no dependencies, no LLM, and it produces the baseline every other item is judged
against. **A1–A6 deliver a working sentence reader with the LLM entirely switched off**; that is
the shippable core.

**Ordering constraint between A1 and A11.** Three baseline figures (30.5%, 9.1%, 4.0%) are
computed by *importing* `is_relevant_para`, `remove_citations` and `_is_cut_off` from the vendored
copy that A11 deletes. A1 therefore records those three as **frozen baseline constants** with
provenance (source path, verified-identical commit, date); the live re-runnable checks are the
ones needing no `nlp_histo` import — unterminated rate, lowercase-start rate, bare-number blocks,
label distribution. Re-measuring the profile-dependent numbers happens in the worker env.

Also: `files/json/*.json` — A1's input — came from `masking/latest_ingest.py`. The JSONs are
gitignored but present on disk, so deleting `masking/` loses nothing; it does mean regeneration
goes through the new worker. Keep the existing JSONs until the worker reproduces them.

---

## 14. Verification

**Zero-dependency, works today:**
```bash
python3 scripts/eval/extraction_stats.py files/json/*.json
```
Must reproduce 43.5% / 21.6% / 1964 on the current corpus before any change lands.

**Contract:** JSON-Schema validation of the golden fixture in *both* repos' suites; coordinate
round-trip test (docling → fitz → docling is identity); an import test asserting `import
nlp_histo` **fails** from the backend environment — the boundary is the deliverable.

**Per stage:** rejection tests for every op guard in §8 (including a paraphrase attempt and an
over-budget deletion set); golden-file tests for the 8 German segmentation cases; a rollback test
asserting derived text returns to verbatim; a traversal test with `../` paths in the package.

**End-to-end:** worker CLI on one book → package validates → imports → `book_sentences` populated
→ reader renders sentences → `reading_selections` still resolve after a block PATCH.

**Every backend change:** `ruff check .` from repo root (must print `All checks passed!`) and
`cd lexy-app/backend && pytest -n auto`.
