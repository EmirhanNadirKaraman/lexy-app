"""Metric definitions.

**The alignment is the specification.** "Incorrect split" and "incorrect merge"
are only meaningful relative to an explicit correspondence between predicted
and gold sentences, so that correspondence is defined once here and everything
else is derived from it. Two independently-written counters would disagree and
there would be no way to tell which was right.

The alignment, precisely:

1. Both sides are reduced to a **normalized continuous string** and a set of
   **character offsets** where a sentence boundary falls. Offsets, not sentence
   indices — a single extra boundary shifts every later index and turns the
   remaining counts into noise.
2. The predicted string is aligned to the gold string with
   :class:`difflib.SequenceMatcher`, and predicted offsets are mapped into gold
   coordinates. This is what lets a pipeline that mangles a character still be
   scored fairly on where it put its boundaries.
3. Boundaries are matched within a **tolerance window** (default 1 character),
   so a boundary off by a space is one near-miss rather than a false positive
   *and* a false negative.
4. A gold boundary with no predicted match is a **missed split** (the pipeline
   merged two sentences). A predicted boundary with no gold match is a
   **spurious split**. Both fall out of the same matching; neither is counted
   separately.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field

from .annotation import GoldDocument
from .model import PredictedDocument
from .normalize import normalize_text

DEFAULT_TOLERANCE = 1
"""Characters of slack when matching a predicted boundary to a gold one."""

_TERMINAL_PUNCT = tuple(".!?…\"'»«”“)]")
_BARE_NUMBER = re.compile(r"^\d{1,4}$")
_PUNCT_ONLY = re.compile(r"^[^\w]+$")
MIN_READER_UNIT_CHARS = 3
"""Below this a unit cannot be a real sentence. Mirrors ``subtitle_segmenter``."""


# ---------------------------------------------------------------------------
# offsets and alignment
# ---------------------------------------------------------------------------


def boundary_offsets(sentences: list[str]) -> tuple[str, list[int]]:
    """Return the joined text and the offset after each sentence but the last.

    Sentences are joined with a single space, matching
    ``GoldDocument.continuous_text`` and ``PredictedDocument.continuous_text``.
    The final position is not a boundary — it is the end of the document, which
    both sides always agree on and which would inflate every score.
    """
    parts = [normalize_text(s) for s in sentences]
    parts = [p for p in parts if p]
    text_parts: list[str] = []
    offsets: list[int] = []
    cursor = 0
    for i, part in enumerate(parts):
        if i > 0:
            cursor += 1  # the joining space
        text_parts.append(part)
        cursor += len(part)
        if i < len(parts) - 1:
            offsets.append(cursor)
    return " ".join(text_parts), offsets


def build_offset_map(src: str, dst: str) -> dict[int, int]:
    """Map character offsets in *src* onto offsets in *dst*.

    Uses :class:`difflib.SequenceMatcher` matching blocks. Offsets inside an
    equal block map exactly; offsets in a replaced or deleted region map to the
    start of the next equal block, which is the closest defensible position.

    This is what makes the boundary metrics robust to a pipeline that drops a
    running header or mis-reads a character: the *text* difference is scored by
    :func:`text_fidelity`, and boundary placement is scored separately rather
    than being destroyed by an offset shift.
    """
    mapping: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(a=src, b=dst, autojunk=False)
    for a0, b0, size in matcher.get_matching_blocks():
        for k in range(size + 1):
            mapping.setdefault(a0 + k, b0 + k)
    mapping.setdefault(len(src), len(dst))
    return mapping


def _map_offset(mapping: dict[int, int], offset: int, fallback_len: int) -> int:
    if offset in mapping:
        return mapping[offset]
    known = [o for o in mapping if o <= offset]
    if not known:
        return 0
    nearest = max(known)
    return min(mapping[nearest] + (offset - nearest), fallback_len)


# ---------------------------------------------------------------------------
# boundary metrics
# ---------------------------------------------------------------------------


@dataclass
class BoundaryMetrics:
    """Sentence-boundary agreement between a pipeline and the gold annotation."""

    gold_boundaries: int
    predicted_boundaries: int
    matched: int
    spurious_splits: int
    """Predicted boundary with no gold counterpart — the pipeline split too eagerly."""
    missed_splits: int
    """Gold boundary the pipeline did not produce — it merged two sentences."""
    precision: float
    recall: float
    f1: float
    window_diff: float | None
    pk: float | None
    window_k: int | None

    def as_dict(self) -> dict:
        return asdict(self)


def _match_boundaries(
    gold: list[int], predicted: list[int], tolerance: int
) -> tuple[int, list[int], list[int]]:
    """Greedy nearest-first matching within *tolerance*.

    Greedy is correct here because boundaries are monotonically ordered and the
    tolerance is far smaller than any realistic sentence, so the nearest
    candidate is the only plausible partner.
    """
    remaining = sorted(predicted)
    used = [False] * len(remaining)
    matched = 0
    matched_gold: list[int] = []

    for g in sorted(gold):
        best_idx, best_dist = None, tolerance + 1
        for i, p in enumerate(remaining):
            if used[i]:
                continue
            dist = abs(p - g)
            if dist < best_dist:
                best_idx, best_dist = i, dist
        if best_idx is not None and best_dist <= tolerance:
            used[best_idx] = True
            matched += 1
            matched_gold.append(g)

    unmatched_pred = [p for i, p in enumerate(remaining) if not used[i]]
    unmatched_gold = [g for g in sorted(gold) if g not in set(matched_gold)]
    return matched, unmatched_pred, unmatched_gold


def _segment_id(boundaries: list[int], position: int) -> int:
    """Which segment *position* falls in, given sorted *boundaries*."""
    lo, hi = 0, len(boundaries)
    while lo < hi:
        mid = (lo + hi) // 2
        if boundaries[mid] <= position:
            lo = mid + 1
        else:
            hi = mid
    return lo


def window_diff_and_pk(
    gold: list[int], predicted: list[int], length: int, k: int | None = None
) -> tuple[float | None, float | None, int | None]:
    """WindowDiff and Pk over character positions.

    ``k`` defaults to half the average gold segment length, which is the
    convention in the segmentation literature. It is computed rather than
    hardcoded because segment length varies hugely between a dialogue-heavy
    page and a descriptive one.

    Returns ``(None, None, None)`` when the document is too short to slide a
    window — reporting 0.0 there would look like a perfect score.
    """
    if length <= 1:
        return None, None, None
    n_segments = len(gold) + 1
    if k is None:
        k = max(2, int(round((length / n_segments) / 2)))
    if k >= length:
        return None, None, None

    g_sorted, p_sorted = sorted(gold), sorted(predicted)
    windows = length - k
    if windows <= 0:
        return None, None, None

    wd_errors = 0
    pk_errors = 0
    for i in range(windows):
        j = i + k
        g_count = sum(1 for b in g_sorted if i < b <= j)
        p_count = sum(1 for b in p_sorted if i < b <= j)
        if g_count != p_count:
            wd_errors += 1
        g_same = _segment_id(g_sorted, i) == _segment_id(g_sorted, j)
        p_same = _segment_id(p_sorted, i) == _segment_id(p_sorted, j)
        if g_same != p_same:
            pk_errors += 1

    return wd_errors / windows, pk_errors / windows, k


def boundary_metrics(
    gold: GoldDocument,
    predicted: PredictedDocument,
    tolerance: int = DEFAULT_TOLERANCE,
) -> BoundaryMetrics:
    """Compare boundary placement, after mapping predicted offsets onto gold."""
    gold_text, gold_offsets = boundary_offsets([s.text for s in gold.sentences])
    pred_text, pred_offsets = boundary_offsets([s.text for s in predicted.sentences])

    mapping = build_offset_map(pred_text, gold_text)
    mapped = [_map_offset(mapping, o, len(gold_text)) for o in pred_offsets]

    matched, spurious, missed = _match_boundaries(gold_offsets, mapped, tolerance)

    precision = matched / len(mapped) if mapped else 0.0
    recall = matched / len(gold_offsets) if gold_offsets else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    wd, pk, k = window_diff_and_pk(gold_offsets, mapped, len(gold_text))

    return BoundaryMetrics(
        gold_boundaries=len(gold_offsets),
        predicted_boundaries=len(mapped),
        matched=matched,
        spurious_splits=len(spurious),
        missed_splits=len(missed),
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        window_diff=round(wd, 4) if wd is not None else None,
        pk=round(pk, 4) if pk is not None else None,
        window_k=k,
    )


# ---------------------------------------------------------------------------
# document-level metrics
# ---------------------------------------------------------------------------


@dataclass
class DocumentMetrics:
    gold_chars: int
    predicted_chars: int
    gold_sentences: int
    predicted_sentences: int
    gold_paragraphs: int
    predicted_paragraphs: int | None
    text_similarity: float
    """0–1 character-level agreement between the two continuous texts."""
    missing_chars: int
    """Gold characters absent from the prediction — text the pipeline lost."""
    extra_chars: int
    """Predicted characters absent from gold — artifacts it failed to remove."""
    duplicated_units: int
    """Predicted reader units appearing more than once (extraction duplication)."""
    expected_removals: int
    removals_leaked: int
    """Expected-removal strings still present anywhere in the predicted text.

    Substring, not whole-unit, and deliberately so. An artifact absorbed *into*
    a sentence ("…seit einer Stunde Kapitel 1 · Die Abfahrt 12 auf denselben
    Punkt…") is worse than one standing alone as its own unit, because it
    corrupts real reading material rather than merely adding a skippable line.
    A whole-unit test scores that case as clean, which is precisely backwards.
    """
    removals_leaked_as_units: int
    """The subset that leaked as standalone reader units — the milder failure."""

    def as_dict(self) -> dict:
        return asdict(self)


def text_fidelity(gold_text: str, pred_text: str) -> tuple[float, int, int]:
    """Return ``(similarity, missing_chars, extra_chars)``.

    Similarity is :meth:`difflib.SequenceMatcher.ratio`. The two character
    counts are the asymmetric halves that matter operationally: *missing* is
    text a learner will never see, *extra* is artifacts that leaked through.
    Reporting only a single similarity number would hide which of the two is
    happening, and they have opposite fixes.
    """
    matcher = difflib.SequenceMatcher(a=gold_text, b=pred_text, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return (
        round(matcher.ratio(), 4),
        len(gold_text) - matched,
        len(pred_text) - matched,
    )


def document_metrics(
    gold: GoldDocument, predicted: PredictedDocument
) -> DocumentMetrics:
    gold_text = gold.continuous_text
    pred_text = predicted.continuous_text
    similarity, missing, extra = text_fidelity(gold_text, pred_text)

    seen: dict[str, int] = {}
    for s in predicted.sentences:
        key = normalize_text(s.text)
        seen[key] = seen.get(key, 0) + 1
    duplicated = sum(count - 1 for count in seen.values() if count > 1)

    removals = [normalize_text(r.get("text", "")) for r in gold.expected_removals]
    removals = [r for r in removals if r]
    predicted_units = {normalize_text(s.text) for s in predicted.sentences}
    leaked = sum(1 for r in removals if r in pred_text)
    leaked_as_units = sum(1 for r in removals if r in predicted_units)

    return DocumentMetrics(
        gold_chars=len(gold_text),
        predicted_chars=len(pred_text),
        gold_sentences=len(gold.sentences),
        predicted_sentences=len(predicted.sentences),
        gold_paragraphs=gold.paragraph_count,
        predicted_paragraphs=None,  # needs A4 reconstruction; see reader_quality
        text_similarity=similarity,
        missing_chars=missing,
        extra_chars=extra,
        duplicated_units=duplicated,
        expected_removals=len(removals),
        removals_leaked=leaked,
        removals_leaked_as_units=leaked_as_units,
    )


# ---------------------------------------------------------------------------
# Stage 1: deterministic fidelity
# ---------------------------------------------------------------------------

FALSE_DELETION_WEIGHT = 10.0
"""Cost of losing one character of genuine content.

Ten times the cost of retaining an artifact character, and the ratio is the
whole point rather than a tuning knob. A deletion is **irreversible**: the
multimodal reviewer sees the pipeline's output, so content dropped before it
runs can never be recovered. A retained artifact is **recoverable**: the
reviewer can delete it with one operation.

Consequence: a deterministic stage that raises its own boundary F1 by
aggressively dropping suspicious blocks scores *worse* here, which is the
intent. See ``docs/INGESTION_PIPELINE.md`` §2 — ``is_relevant_para`` would
delete 30.5% of German prose while looking locally reasonable.
"""

RETAINED_ARTIFACT_WEIGHT = 1.0
UNCERTAIN_FLAG_DISCOUNT = 0.25
"""Multiplier for artifact text the pipeline kept *and flagged* as uncertain.

Not zero — a flagged artifact still costs the reviewer attention. But far below
an unflagged one, because "kept and marked uncertain" is precisely the
behaviour the architecture asks for.
"""


SENTENCE_RECOVERY_THRESHOLD = 0.5
"""Fraction of a gold sentence's characters that must survive somewhere in the
output for it to count as *preserved* rather than deleted."""


def count_lost_sentences(
    gold: GoldDocument,
    pred_text: str,
    threshold: float = SENTENCE_RECOVERY_THRESHOLD,
) -> int:
    """Gold sentences whose content is genuinely absent from the output.

    **Not** an exact-substring test, deliberately. A sentence interrupted by a
    retained artifact ("…seit einer Stunde `Kapitel 1 · Die Abfahrt 12` auf
    denselben Punkt…") has lost nothing — every character is still there. An
    exact test would report it as deleted while ``false_deleted_chars`` reports
    zero loss, and those two numbers contradicting each other would push a
    pipeline toward removing artifacts aggressively to "recover" sentences it
    never actually lost.

    So recovery is measured by character overlap: a sentence counts as lost
    only when most of its characters are missing from the output entirely.
    The exact-substring case is checked first purely as a fast path.
    """
    lost = 0
    for sentence in gold.sentences:
        text = normalize_text(sentence.text)
        if not text or text in pred_text:
            continue
        matcher = difflib.SequenceMatcher(a=text, b=pred_text, autojunk=False)
        recovered = sum(block.size for block in matcher.get_matching_blocks())
        if recovered / len(text) < threshold:
            lost += 1
    return lost


@dataclass
class FidelityMetrics:
    """Stage 1 — did the deterministic pipeline preserve the information?

    Scored independently of sentence quality on purpose. A pipeline must not be
    able to buy a better boundary F1 with irreversible deletions.
    """

    gold_chars: int
    content_preservation_rate: float
    """Share of gold characters still present. The headline Stage 1 number.

    **Order-sensitive caveat:** this is computed by linear alignment, so text
    that is *reordered* (a two-column page read right-then-left) registers as
    both missing and extra characters even though nothing was lost. Read it
    together with ``reading_order_correctness``: preservation low + order low
    means reordering; preservation low + order high means genuine deletion.
    Only the second is irreversible.
    """
    false_deleted_chars: int
    """Genuine content the pipeline lost. Irreversible; weighted heaviest.

    Subject to the same reordering caveat as ``content_preservation_rate``.
    ``false_deleted_sentences`` is order-insensitive and is the reliable
    signal when the two disagree.
    """
    false_deleted_sentences: int
    """Gold sentences with no recognisable counterpart in the output."""
    retained_artifact_chars: int
    """Text present that gold does not have. Recoverable; weighted lightly."""
    uncertain_units: int
    """Units kept but flagged — the preferred alternative to deleting."""
    expected_removals: int
    artifacts_removed: int
    artifact_removal_rate: float | None
    reported_deletions: int | None
    """``None`` when the pipeline does not report what it deleted."""
    unjustified_deletions: int | None
    """Reported deletions whose text appears in gold — deleting real content."""
    provenance_completeness: float | None
    reading_order_correctness: float | None
    information_loss_score: float
    """Weighted loss, lower is better. Dominated by false deletions by design."""

    def as_dict(self) -> dict:
        return asdict(self)


def fidelity_metrics(
    gold: GoldDocument, predicted: PredictedDocument
) -> FidelityMetrics:
    gold_text = gold.continuous_text
    pred_text = predicted.continuous_text
    _, missing, extra = text_fidelity(gold_text, pred_text)

    gold_len = len(gold_text) or 1
    preservation = max(0.0, 1.0 - missing / gold_len)

    lost_sentences = count_lost_sentences(gold, pred_text)

    removals = [normalize_text(r.get("text", "")) for r in gold.expected_removals]
    removals = [r for r in removals if r]
    removed = sum(1 for r in removals if r not in pred_text)
    removal_rate = round(removed / len(removals), 4) if removals else None

    if predicted.reports_deletions:
        reported = len(predicted.dropped or [])
        unjustified = sum(
            1
            for d in (predicted.dropped or [])
            if normalize_text(d.text) and normalize_text(d.text) in gold_text
        )
    else:
        reported = None
        unjustified = None

    uncertain_chars = sum(
        len(normalize_text(s.text)) for s in predicted.sentences if s.uncertain
    )
    unflagged_extra = max(0, extra - uncertain_chars)
    flagged_extra = min(extra, uncertain_chars)

    loss = (
        FALSE_DELETION_WEIGHT * missing
        + RETAINED_ARTIFACT_WEIGHT * unflagged_extra
        + RETAINED_ARTIFACT_WEIGHT * UNCERTAIN_FLAG_DISCOUNT * flagged_extra
    ) / gold_len

    reader = reader_quality(gold, predicted)

    return FidelityMetrics(
        gold_chars=len(gold_text),
        content_preservation_rate=round(preservation, 4),
        false_deleted_chars=missing,
        false_deleted_sentences=lost_sentences,
        retained_artifact_chars=extra,
        uncertain_units=len(predicted.uncertain_units),
        expected_removals=len(removals),
        artifacts_removed=removed,
        artifact_removal_rate=removal_rate,
        reported_deletions=reported,
        unjustified_deletions=unjustified,
        provenance_completeness=reader.page_provenance_coverage,
        reading_order_correctness=(
            round(1.0 - reader.ordering_errors / reader.total_units, 4)
            if reader.total_units
            else None
        ),
        information_loss_score=round(loss, 4),
    )


# ---------------------------------------------------------------------------
# Stage 2: reader-quality metrics
# ---------------------------------------------------------------------------


@dataclass
class ReaderMetrics:
    """Is each unit something a learner should actually be shown?"""

    total_units: int
    complete_units: int
    """Starts like a sentence and ends with terminal punctuation."""
    fragment_units: int
    bare_number_units: int
    punctuation_only_units: int
    too_short_units: int
    page_spanning_expected: int
    page_spanning_recovered: int
    """Gold page-spanning sentences the pipeline emitted as ONE unit."""
    ordering_errors: int
    page_provenance_coverage: float | None
    """``None`` when the source supplies no page provenance at all."""
    block_provenance_coverage: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def is_complete_unit(text: str) -> bool:
    """A unit that reads as a whole sentence.

    Heuristic and deliberately simple: it must be long enough, not be a bare
    number, and end in terminal punctuation. German capitalizes all nouns, so
    an initial-capital test says nothing about sentence-hood and is not used.
    """
    text = text.strip()
    if len(text) < MIN_READER_UNIT_CHARS:
        return False
    if _BARE_NUMBER.match(text) or _PUNCT_ONLY.match(text):
        return False
    return text.endswith(_TERMINAL_PUNCT)


def reader_quality(gold: GoldDocument, predicted: PredictedDocument) -> ReaderMetrics:
    units = [normalize_text(s.text) for s in predicted.sentences]

    complete = sum(1 for u in units if is_complete_unit(u))
    bare_numbers = sum(1 for u in units if _BARE_NUMBER.match(u))
    punct_only = sum(1 for u in units if _PUNCT_ONLY.match(u))
    too_short = sum(
        1
        for u in units
        if len(u) < MIN_READER_UNIT_CHARS and not _BARE_NUMBER.match(u)
    )
    fragments = len(units) - complete

    expected_spanning = gold.page_spanning_sentences
    predicted_set = set(units)
    recovered = sum(
        1 for s in expected_spanning if normalize_text(s.text) in predicted_set
    )

    ordering_errors = _count_ordering_errors(gold, units)

    if predicted.sentences and predicted.provides_page_provenance:
        page_cov = round(
            sum(1 for s in predicted.sentences if s.has_page_provenance)
            / len(predicted.sentences),
            4,
        )
    else:
        page_cov = None
    if predicted.sentences and predicted.provides_block_provenance:
        block_cov = round(
            sum(1 for s in predicted.sentences if s.has_block_provenance)
            / len(predicted.sentences),
            4,
        )
    else:
        block_cov = None

    return ReaderMetrics(
        total_units=len(units),
        complete_units=complete,
        fragment_units=fragments,
        bare_number_units=bare_numbers,
        punctuation_only_units=punct_only,
        too_short_units=too_short,
        page_spanning_expected=len(expected_spanning),
        page_spanning_recovered=recovered,
        ordering_errors=ordering_errors,
        page_provenance_coverage=page_cov,
        block_provenance_coverage=block_cov,
    )


def _count_ordering_errors(gold: GoldDocument, units: list[str]) -> int:
    """Predicted units that appear out of gold order.

    Counted as the number of units that must be removed to leave an increasing
    subsequence of gold indices — i.e. ``len(matched) - LIS(matched)``. Only
    units matching a gold sentence exactly are considered; unmatched units are
    a text problem, already counted elsewhere, not an ordering one.
    """
    index_of = {normalize_text(s.text): s.sentence_index for s in gold.sentences}
    seq = [index_of[u] for u in units if u in index_of]
    if len(seq) < 2:
        return 0

    # Longest strictly-increasing subsequence, O(n log n).
    import bisect

    tails: list[int] = []
    for value in seq:
        pos = bisect.bisect_left(tails, value)
        if pos == len(tails):
            tails.append(value)
        else:
            tails[pos] = value
    return len(seq) - len(tails)


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


@dataclass
class DocumentResult:
    document_id: str
    fidelity: FidelityMetrics
    boundary: BoundaryMetrics
    document: DocumentMetrics
    reader: ReaderMetrics
    phenomena: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "phenomena": self.phenomena,
            "fidelity": self.fidelity.as_dict(),
            "boundary": self.boundary.as_dict(),
            "document": self.document.as_dict(),
            "reader": self.reader.as_dict(),
        }


def evaluate_document(
    gold: GoldDocument,
    predicted: PredictedDocument,
    tolerance: int = DEFAULT_TOLERANCE,
) -> DocumentResult:
    return DocumentResult(
        document_id=gold.document_id,
        fidelity=fidelity_metrics(gold, predicted),
        boundary=boundary_metrics(gold, predicted, tolerance),
        document=document_metrics(gold, predicted),
        reader=reader_quality(gold, predicted),
        phenomena=gold.phenomena,
    )


def summarize(results: list[DocumentResult]) -> dict:
    """Corpus-level roll-up.

    Boundary precision/recall are recomputed from summed counts (micro-average)
    rather than averaged per document, so a 3-sentence fixture cannot outweigh
    a 900-sentence book. WindowDiff/Pk are macro-averaged over the documents
    that produced one, because they are already length-normalized.
    """
    if not results:
        return {"documents": 0}

    matched = sum(r.boundary.matched for r in results)
    pred_b = sum(r.boundary.predicted_boundaries for r in results)
    gold_b = sum(r.boundary.gold_boundaries for r in results)
    precision = matched / pred_b if pred_b else 0.0
    recall = matched / gold_b if gold_b else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    wds = [r.boundary.window_diff for r in results if r.boundary.window_diff is not None]
    pks = [r.boundary.pk for r in results if r.boundary.pk is not None]
    total_units = sum(r.reader.total_units for r in results)

    gold_chars = sum(r.fidelity.gold_chars for r in results) or 1
    false_deleted = sum(r.fidelity.false_deleted_chars for r in results)
    retained = sum(r.fidelity.retained_artifact_chars for r in results)
    removals_total = sum(r.fidelity.expected_removals for r in results)
    removed_total = sum(r.fidelity.artifacts_removed for r in results)
    reported = [
        r.fidelity.reported_deletions
        for r in results
        if r.fidelity.reported_deletions is not None
    ]
    unjustified = [
        r.fidelity.unjustified_deletions
        for r in results
        if r.fidelity.unjustified_deletions is not None
    ]
    order_scores = [
        r.fidelity.reading_order_correctness
        for r in results
        if r.fidelity.reading_order_correctness is not None
    ]

    return {
        "documents": len(results),
        # -- Stage 1: deterministic fidelity (information preservation) ------
        "content_preservation_rate": round(1.0 - false_deleted / gold_chars, 4),
        "false_deleted_chars": false_deleted,
        "false_deleted_sentences": sum(
            r.fidelity.false_deleted_sentences for r in results
        ),
        "retained_artifact_chars": retained,
        "uncertain_units": sum(r.fidelity.uncertain_units for r in results),
        "artifact_removal_rate": (
            round(removed_total / removals_total, 4) if removals_total else None
        ),
        "reported_deletions": sum(reported) if reported else None,
        "unjustified_deletions": sum(unjustified) if unjustified else None,
        "reading_order_correctness": (
            round(sum(order_scores) / len(order_scores), 4) if order_scores else None
        ),
        "information_loss_score": round(
            sum(r.fidelity.information_loss_score * r.fidelity.gold_chars for r in results)
            / gold_chars,
            4,
        ),
        # -- Stage 2: reader quality -----------------------------------------
        "boundary_precision": round(precision, 4),
        "boundary_recall": round(recall, 4),
        "boundary_f1": round(f1, 4),
        "gold_boundaries": gold_b,
        "predicted_boundaries": pred_b,
        "spurious_splits": sum(r.boundary.spurious_splits for r in results),
        "missed_splits": sum(r.boundary.missed_splits for r in results),
        "window_diff_mean": round(sum(wds) / len(wds), 4) if wds else None,
        "pk_mean": round(sum(pks) / len(pks), 4) if pks else None,
        "text_similarity_mean": round(
            sum(r.document.text_similarity for r in results) / len(results), 4
        ),
        "missing_chars": sum(r.document.missing_chars for r in results),
        "extra_chars": sum(r.document.extra_chars for r in results),
        "duplicated_units": sum(r.document.duplicated_units for r in results),
        "expected_removals": sum(r.document.expected_removals for r in results),
        "removals_leaked": sum(r.document.removals_leaked for r in results),
        "removals_leaked_as_units": sum(
            r.document.removals_leaked_as_units for r in results
        ),
        "total_units": total_units,
        "complete_units": sum(r.reader.complete_units for r in results),
        "fragment_units": sum(r.reader.fragment_units for r in results),
        "complete_unit_rate": (
            round(sum(r.reader.complete_units for r in results) / total_units, 4)
            if total_units
            else None
        ),
        "page_spanning_expected": sum(r.reader.page_spanning_expected for r in results),
        "page_spanning_recovered": sum(
            r.reader.page_spanning_recovered for r in results
        ),
        "ordering_errors": sum(r.reader.ordering_errors for r in results),
    }
