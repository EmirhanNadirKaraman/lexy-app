"""Stage 1 fidelity — the information-preservation principle, as tests.

The architectural rule these encode: a deterministic pipeline must not be able
to improve its own score by making irreversible decisions. Deleting content is
irreversible (the reviewer never sees it); retaining an artifact is not.
"""
from evaluation.annotation import parse_gold_text
from evaluation.compare import (
    SEVERITY_CRITICAL,
    SEVERITY_RECOVERABLE,
    severity_of,
)
from evaluation.metrics import (
    FALSE_DELETION_WEIGHT,
    RETAINED_ARTIFACT_WEIGHT,
    fidelity_metrics,
)
from evaluation.model import DroppedElement, PredictedDocument, PredictedSentence

GOLD_TEXT = "==== PAGE 1 ====\nDer Hof war still.\nDie Katze schlief.\n"


def gold_with_removal():
    doc = parse_gold_text(GOLD_TEXT, "t")
    doc.meta = {"expected_removals": [{"text": "Kapitel 2", "kind": "heading"}]}
    return doc


def pred(*sentences, dropped=None, uncertain_texts=()):
    return PredictedDocument(
        document_id="t",
        sentences=[
            PredictedSentence(text=s, uncertain=(s in uncertain_texts))
            for s in sentences
        ],
        dropped=dropped,
    )


class TestPreservation:
    def test_perfect_preservation(self):
        m = fidelity_metrics(
            parse_gold_text(GOLD_TEXT, "t"), pred("Der Hof war still.", "Die Katze schlief.")
        )
        assert m.content_preservation_rate == 1.0
        assert m.false_deleted_chars == 0
        assert m.false_deleted_sentences == 0
        assert m.information_loss_score == 0.0

    def test_deleted_sentence_is_counted(self):
        m = fidelity_metrics(parse_gold_text(GOLD_TEXT, "t"), pred("Der Hof war still."))
        assert m.false_deleted_sentences == 1
        assert m.false_deleted_chars > 0
        assert m.content_preservation_rate < 1.0

    def test_retained_artifact_does_not_reduce_preservation(self):
        # All gold content is present; an extra artifact must not be scored as
        # information loss.
        m = fidelity_metrics(
            parse_gold_text(GOLD_TEXT, "t"),
            pred("Der Hof war still.", "Die Katze schlief.", "Kapitel 2"),
        )
        assert m.content_preservation_rate == 1.0
        assert m.false_deleted_chars == 0
        assert m.retained_artifact_chars > 0


class TestAsymmetry:
    """The core of the principle: deletion must cost far more than retention."""

    def test_weight_ratio_is_explicit(self):
        assert FALSE_DELETION_WEIGHT > RETAINED_ARTIFACT_WEIGHT
        assert FALSE_DELETION_WEIGHT / RETAINED_ARTIFACT_WEIGHT >= 10

    def test_deleting_scores_worse_than_retaining_the_same_text(self):
        g = parse_gold_text(GOLD_TEXT, "t")
        # Pipeline A drops a real sentence to look tidy.
        deleting = fidelity_metrics(g, pred("Der Hof war still."))
        # Pipeline B keeps everything and additionally retains an artifact of
        # comparable size.
        retaining = fidelity_metrics(
            g, pred("Der Hof war still.", "Die Katze schlief.", "Ein Artefakt hier.")
        )
        assert deleting.information_loss_score > retaining.information_loss_score

    def test_flagging_uncertain_is_cheaper_than_leaving_unflagged(self):
        g = parse_gold_text(GOLD_TEXT, "t")
        artifact = "Kapitel 2 steht hier."
        unflagged = fidelity_metrics(
            g, pred("Der Hof war still.", "Die Katze schlief.", artifact)
        )
        flagged = fidelity_metrics(
            g,
            pred(
                "Der Hof war still.",
                "Die Katze schlief.",
                artifact,
                uncertain_texts=(artifact,),
            ),
        )
        assert flagged.information_loss_score < unflagged.information_loss_score
        assert flagged.uncertain_units == 1

    def test_flagging_is_still_not_free(self):
        # A flagged artifact costs reviewer attention; discount, not exemption.
        g = parse_gold_text(GOLD_TEXT, "t")
        artifact = "Kapitel 2 steht hier."
        flagged = fidelity_metrics(
            g,
            pred(
                "Der Hof war still.",
                "Die Katze schlief.",
                artifact,
                uncertain_texts=(artifact,),
            ),
        )
        assert flagged.information_loss_score > 0


class TestDeletionAccounting:
    def test_unreported_deletions_are_unknown_not_zero(self):
        # A pipeline that cannot say what it deleted must not be scored as
        # having deleted nothing.
        m = fidelity_metrics(parse_gold_text(GOLD_TEXT, "t"), pred("Der Hof war still."))
        assert m.reported_deletions is None
        assert m.unjustified_deletions is None

    def test_reported_deletion_of_artifact_is_justified(self):
        m = fidelity_metrics(
            parse_gold_text(GOLD_TEXT, "t"),
            pred(
                "Der Hof war still.",
                "Die Katze schlief.",
                dropped=[DroppedElement(text="42", reason="page_number")],
            ),
        )
        assert m.reported_deletions == 1
        assert m.unjustified_deletions == 0

    def test_reported_deletion_of_real_content_is_unjustified(self):
        m = fidelity_metrics(
            parse_gold_text(GOLD_TEXT, "t"),
            pred(
                "Der Hof war still.",
                dropped=[DroppedElement(text="Die Katze schlief.", reason="short_block")],
            ),
        )
        assert m.unjustified_deletions == 1


class TestArtifactRemoval:
    def test_removed_artifact_scores_full_rate(self):
        m = fidelity_metrics(gold_with_removal(), pred("Der Hof war still.", "Die Katze schlief."))
        assert m.artifact_removal_rate == 1.0

    def test_leaked_artifact_scores_zero_rate(self):
        m = fidelity_metrics(
            gold_with_removal(), pred("Der Hof war still.", "Die Katze schlief.", "Kapitel 2")
        )
        assert m.artifact_removal_rate == 0.0

    def test_no_expected_removals_is_none_not_one(self):
        m = fidelity_metrics(parse_gold_text(GOLD_TEXT, "t"), pred("Der Hof war still."))
        assert m.artifact_removal_rate is None


class TestSeverityClassification:
    def test_information_loss_is_critical(self):
        for key in ("content_preservation_rate", "false_deleted_chars",
                    "false_deleted_sentences", "information_loss_score"):
            assert severity_of(key) == SEVERITY_CRITICAL

    def test_retained_artifacts_are_recoverable(self):
        for key in ("retained_artifact_chars", "removals_leaked", "extra_chars"):
            assert severity_of(key) == SEVERITY_RECOVERABLE

    def test_boundary_metrics_are_normal(self):
        assert severity_of("boundary_f1") == "normal"
