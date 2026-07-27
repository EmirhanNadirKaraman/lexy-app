"""
Runtime-aligned port of the segmentation-stage tests (#17 salvage batch 2).

Targets the RUNTIME module `subtitle_segmenter.py`, not the orphan refactor at
`src/app/subtitles/segmentation.py`.

Why this file exists: before this batch, `subtitle_segmenter.py` (382 lines)
had **zero** runtime test coverage — the only tests for the segmentation stage
lived in `tests/pipeline/test_pipeline.py` against `app.subtitles.segmentation`,
which nothing calls at runtime. Segmentation is the stage that splits a merged
subtitle window into individual utterances, so a regression here silently
changes what every downstream stage (extraction, eligibility, i+1 matching)
receives.

API parity was verified before porting: `SubtitleSegmenter.segment_window`,
`SegmentationConfig(component=…, min_chars=…)` (same defaults: "senter" / 3),
and the `SubtitleFragment` / `MergedSubtitleWindow` / `CandidateUtterance`
dataclass fields are identical between runtime and the refactor. No production
code was changed to make these pass.

Ported nodeids (src/app suite):
  tests/pipeline/test_pipeline.py::TestSegmentation::test_single_sentence_window_produces_one_candidate
  tests/pipeline/test_pipeline.py::TestSegmentation::test_two_sentence_window_produces_two_candidates
  tests/pipeline/test_pipeline.py::TestSegmentation::test_split_window_timing_is_interpolated
  tests/pipeline/test_pipeline.py::TestSegmentation::test_segment_below_min_chars_is_dropped
"""
import pytest
import spacy

from subtitle_merger import MergedSubtitleWindow, SubtitleFragment
from subtitle_segmenter import SegmentationConfig, SubtitleSegmenter


@pytest.fixture(scope="session")
def nlp():
    """Load de_core_news_md once per session — model load is expensive."""
    try:
        return spacy.load("de_core_news_md")
    except OSError:
        pytest.skip(
            "de_core_news_md not installed. "
            "Run: python -m spacy download de_core_news_md"
        )


def make_window(text: str, start: float = 0.0, end: float = 3.0) -> MergedSubtitleWindow:
    frag = SubtitleFragment(text=text, start_time=start, end_time=end, index=0)
    return MergedSubtitleWindow(
        fragments=[frag],
        text=text,
        start_time=start,
        end_time=end,
    )


class TestSegmentation:
    """Merged windows → candidate utterances."""

    def test_single_sentence_window_produces_one_candidate(self, nlp):
        window = make_window("Das Kino ist sehr schön.", 0.0, 3.0)
        candidates = SubtitleSegmenter(nlp).segment_window(window)

        assert len(candidates) == 1
        assert "schön" in candidates[0].text

    def test_two_sentence_window_produces_two_candidates(self, nlp):
        """The key regression guard for multi-utterance windows: the merger
        joins fragments across sentence boundaries, and segmentation is what
        splits them apart again. If this collapses to one candidate, every
        downstream i+1 match is computed over the wrong unit."""
        window = make_window(
            "Ich bin sehr müde. Ich muss morgen früh arbeiten.", start=0.0, end=6.0
        )
        candidates = SubtitleSegmenter(nlp).segment_window(window)

        assert len(candidates) == 2
        assert "müde" in candidates[0].text
        assert "arbeiten" in candidates[1].text

    def test_split_window_timing_is_interpolated(self, nlp):
        """Per-sentence times are interpolated by character position: the first
        candidate keeps the window's start, the last keeps its end, and they do
        not overlap. Overlapping times would let one utterance's subtitle
        highlight bleed into the next."""
        window = make_window(
            "Ich bin sehr müde. Ich muss morgen früh arbeiten.", start=0.0, end=6.0
        )
        first, second = SubtitleSegmenter(nlp).segment_window(window)

        assert first.start_time == pytest.approx(0.0)
        assert second.end_time == pytest.approx(6.0)
        assert first.end_time <= second.start_time

    def test_segment_below_min_chars_is_dropped(self, nlp):
        """min_chars raised to 5 so 'Ok.' (3 chars) is discarded."""
        segmenter = SubtitleSegmenter(nlp, SegmentationConfig(min_chars=5))
        candidates = segmenter.segment_window(
            make_window("Ok. Das Kino ist sehr schön.", 0.0, 4.0)
        )

        assert not any(c.text.strip() == "Ok." for c in candidates)
