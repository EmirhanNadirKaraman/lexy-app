"""Text normalization — the single most load-bearing function in the harness.

Every metric in :mod:`evaluation.metrics` compares *character offsets* into a
normalized concatenation of the document text. If the gold side and the
predicted side normalize differently by even one space, every offset shifts and
every number the harness reports is silently wrong.

So there is exactly one normalizer, both sides call it, and it is tested
directly rather than only through the metrics that depend on it.

What it deliberately does NOT do: no case folding, no punctuation stripping, no
Unicode-quote rewriting. Those would erase the very phenomena we measure
(German ``„…"`` / ``»…«`` quoting, ellipses, em dashes). Normalization here
means whitespace and invisible characters only.
"""
from __future__ import annotations

import re
import unicodedata

# Characters that carry no visual width but break offset alignment when one
# side of a comparison has them and the other does not. Soft hyphen (­) is
# the important one for PDF text: it marks a line-break hyphenation point and
# routinely survives extraction.
_INVISIBLE = dict.fromkeys(
    map(ord, "­​‌‍⁠﻿"),
    None,
)

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Return *text* with Unicode, invisible characters and whitespace settled.

    Steps, in order:

    1. NFC composition — so ``ü`` written as ``u`` + combining diaeresis
       compares equal to the precomposed form. Without this, offsets diverge
       between an OCR path and an embedded-text path on the same page.
    2. Drop zero-width and soft-hyphen characters.
    3. Collapse every run of whitespace (including newlines) to a single space.
    4. Strip leading/trailing whitespace.

    The result is a single line. That is intentional: paragraph and page
    structure is carried by the annotation model (:mod:`evaluation.annotation`),
    not by whitespace in the compared string.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_INVISIBLE)
    text = _WHITESPACE_RUN.sub(" ", text)
    return text.strip()


def join_normalized(parts: list[str]) -> str:
    """Normalize each part and join with a single space, dropping empties.

    Used to build the continuous-document string from a sequence of sentences,
    paragraphs or blocks. Empty parts are dropped *before* joining so a blank
    block cannot introduce a double space and shift every later offset.
    """
    cleaned = [normalize_text(p) for p in parts]
    return " ".join(p for p in cleaned if p)


def dehyphenate(text: str) -> str:
    """Repair a word split across a line break by a hyphen.

    ``"Fens-\\nter"`` → ``"Fenster"``. Only fires when a hyphen is followed by
    whitespace and then a lowercase letter, which is the shape a typesetter's
    line-break hyphen takes in German prose. A hyphen followed by an uppercase
    letter is left alone: in German that is far more likely a real compound
    (``"Nord-Süd"``) than a break artifact.

    Not part of :func:`normalize_text` because it *changes* the text rather
    than settling its encoding, and the harness must be able to measure a
    pipeline that does not dehyphenate.
    """
    return re.sub(r"(\w)-\s+([a-zäöüß])", r"\1\2", text)
