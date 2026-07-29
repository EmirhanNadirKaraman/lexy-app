# Benchmark corpus

Gold annotations for the document-ingestion evaluation harness
(`evaluation/`, roadmap **A1**). Design: `docs/INGESTION_PIPELINE.md`.

---

## The guiding principle these fixtures encode

The deterministic pipeline should **maximize information preservation, not its
own standalone F1.** A multimodal reviewer runs downstream specifically to
resolve ambiguity — but it can only see what the deterministic stage emitted.

So the two failure modes are not symmetric:

| Failure | Severity | Why |
|---|---|---|
| Deleting genuine content | **irreversible** | The reviewer never sees it. Nothing downstream can recover it. |
| Retaining an artifact | recoverable | The reviewer can delete it with one operation. |

`FALSE_DELETION_WEIGHT` is **10×** `RETAINED_ARTIFACT_WEIGHT` in
`evaluation/metrics.py` for exactly this reason, and
`evaluation/compare.py` sorts a *critical* regression above any *normal* or
*recoverable* one regardless of numeric size. A 0.001 drop in
`content_preservation_rate` outranks a 0.20 drop in `boundary_f1`.

**Preferred pipeline behaviour:** keep a suspicious block and set
`uncertain=True` on it. A flagged artifact is discounted to 25% of the cost of
an unflagged one — cheap, but not free, because it still costs the reviewer
attention.

---

## Corpus licensing — read before adding a document

The 27 German graded readers in `files/book_pdfs/` are **commercial material**
and `files/` is gitignored. Nothing derived from them may be committed.

Everything in `benchmark/documents/` is **original German text written for this
benchmark**. Keep it that way. If you need a longer or more natural document,
use public-domain sources (Gutenberg-DE, Wikisource) and record the source in
the sidecar's `note` field.

To evaluate against the real local corpus instead, point the harness at it —
nothing is copied into the repo:

```bash
python -m evaluation run --label real_corpus --source docling
BENCHMARK_CORPUS_ROOT=/path/to/corpus python -m evaluation run --source docling ...
```

---

## Files per document

Each document `<id>` has three files:

| File | Role |
|---|---|
| `<id>.blocks.json` | **Input.** A simulated extractor output, deliberately damaged. |
| `<id>.gold.txt` | **Expected output.** The reader units a perfect pipeline produces. |
| `<id>.gold.json` | **Sidecar.** Expected removals, difficult regions, phenomena. |

### `<id>.gold.txt` format

```text
# Lines starting with '#' are comments.
==== PAGE 1 ====
Dr. Müller kam um 17 Uhr an.
Er setzte sich.

Ein neuer Absatz beginnt hier.

==== PAGE 2 ====
Ein Satz, der über den <PB/> Seitenumbruch hinweg weitergeht.
```

- `==== PAGE n ====` starts a page (1-based, must increase).
- **One sentence per line** — this is the unit the reader will show.
- A **blank line** separates paragraphs.
- `<PB/>` marks a page break *inside* a sentence. The marker is stripped and
  the sentence gains a second entry in `source_pages`.

No character offsets appear in the file. They are derived at load time, so you
can fix a typo without running an update script.

### `<id>.gold.json` sidecar

```json
{
  "title": "Sprechstunde",
  "note": "Original text written for this benchmark.",
  "phenomena": ["dialogue", "abbreviation", "page_number"],
  "expected_removals": [
    { "text": "Praxis am Markt", "kind": "running_header", "pages": [1] }
  ],
  "difficult_regions": [
    { "sentence_index": 0, "phenomenon": "abbreviation", "note": "'Dr.' must not end the sentence." }
  ]
}
```

`expected_removals` are strings that must **not** survive into the output.
A string may never be both an expected removal and a gold sentence — a test
enforces this.

---

## Current documents

| Document | Phenomena |
|---|---|
| `01_dialog_abkuerzungen` | dialogue, `Dr.`/`usw.`, `„…"` quotes, sentence split across blocks, running header, page number |
| `02_seitenumbruch` | page-spanning sentence, footer and page number *between* its halves, hyphen-split word |
| `03_zeichensetzung` | ellipsis, em dash, `»…«` quotes, **OCR-missing period**, isolated punctuation |
| `04_kapitel_layout` | chapter heading, image interrupting a paragraph, caption, two-column reading-order fault |

`03` is the deliberate pin for the one case deterministic segmentation cannot
solve: a terminal period the extractor lost. Fixing it needs evidence from the
page, which is the narrow job reserved for AI review (**A8**).

---

## Adding a document

1. Write `<id>.gold.txt` — the reader units you *want*, in reading order.
2. Write `<id>.blocks.json` — the damaged input. Mirror `book_blocks`:
   `{"block_id", "page", "type", "text"}`. Use realistic `type` values
   (`TEXT`, `LIST_ITEM`, `SECTION_HEADER`, `PICTURE`, `CAPTION`,
   `CHECKBOX_UNSELECTED`).
3. Write `<id>.gold.json` — at minimum a non-empty `phenomena` list.
4. Run `python -m evaluation run --label check` and confirm the numbers move
   in the direction you expect.
5. Run `pytest tests/evaluation/` — corpus-integrity tests will catch a
   missing input file, an invalid sidecar, or a removal that is also a gold
   sentence.

Isolate one phenomenon per document where you can. A fixture that mixes five
failure modes tells you a score dropped but not why.

---

## Running

```bash
python -m evaluation list                                  # gold documents
python -m evaluation run --label baseline                  # joined baseline
python -m evaluation run --label per_block --no-join       # block-per-unit
python -m evaluation compare out/eval/per_block.json out/eval/baseline.json
```

`run` writes `<label>.json` (machine-readable, consumed by `compare`) and
`<label>.md` (human report) under `out/eval/`.

`compare` **refuses** to diff runs whose schema version, corpus hash or
segmenter differ — an edited annotation must never read as a code regression.
Use `--allow-mismatch` to override, `--fail-on-regression` for CI.

---

## How later stages plug in

Adding a pipeline is a new adapter class in `evaluation/adapters.py`, never a
change to `evaluation/metrics.py`:

| Roadmap task | Adapter |
|---|---|
| **A2** package import | reads a document package → `PredictedDocument` |
| **A4** reconstruction | emits stitched sentences with `source_pages` per unit |
| **A5** segmentation | replaces `evaluation/segmentation.py` as the predicted side |
| **A8** AI review | wraps another source, applies validated operations |

An adapter must **not** clean anything up — that would measure the adapter
instead of the pipeline and flatter it. Report deletions via
`PredictedDocument.dropped`; a pipeline that does not is scored `None` (unknown)
for deletion accounting, never zero.

---

## Metrics that are not yet computable

Reported honestly as `n/a` rather than faked:

- **Paragraph reconstruction** (`predicted_paragraphs`) — needs **A4**.
- **Provenance completeness** — needs element IDs; today `HierarchicalRow`
  carries no page, bbox or element id.

Both metric functions accept the input already, so A4/A5 populate them without
touching metric code.
