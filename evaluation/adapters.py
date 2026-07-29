"""Adapters: reshape a pipeline's output into :class:`PredictedDocument`.

Two ship today, both deliberately naive — an adapter must not clean anything
up, or the harness would measure the adapter instead of the pipeline and
flatter it.

* :class:`BlockJsonSource` reads the committed benchmark inputs
  (``<id>.blocks.json``), whose shape mirrors ``book_blocks`` so the adapter
  exercises a realistic structure rather than a toy one.
* :class:`DoclingLayoutJsonSource` reads the real, gitignored corpus at
  ``files/json/*_layout.json``.

Future adapters (A2 package import, A4 reconstruction, A6 AI review) are each a
new class here. None of them requires a change to :mod:`evaluation.metrics`.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .model import DroppedElement, PredictedDocument, PredictedSentence
from .normalize import dehyphenate, normalize_text
from .segmentation import DEFAULT_MODEL, segment

#: Element/block types that carry prose. Everything else (PICTURE, TABLE …) is
#: not reader material. ``CHECKBOX_UNSELECTED`` is included deliberately: on the
#: German corpus 1307 such blocks hold ordinary words, so excluding them would
#: hide a real misclassification behind a filter.
PROSE_TYPES = frozenset(
    {"TEXT", "PARAGRAPH", "LIST_ITEM", "CHECKBOX_UNSELECTED", "CHECKBOX_SELECTED"}
)

_BARE_NUMBER = re.compile(r"^\d{1,4}$")


@dataclass
class _Block:
    block_id: str
    page: int
    type: str
    text: str


def _blocks_to_document(
    document_id: str,
    blocks: list[_Block],
    model: str,
    drop_bare_numbers: bool = False,
    join_across_blocks: bool = True,
) -> PredictedDocument:
    """Shared baseline: concatenate prose blocks in order, then segment.

    ``join_across_blocks`` is what distinguishes the two baselines the harness
    is meant to contrast. With it on, blocks are joined into one string before
    segmentation, so a sentence split across two blocks can be recovered. With
    it off, each block is segmented independently — which is what a
    block-per-unit reader does today, and why it produces fragments.
    """
    dropped: list[DroppedElement] = []
    prose: list[_Block] = []
    for block in blocks:
        if block.type.upper() not in PROSE_TYPES:
            if block.text.strip():
                dropped.append(
                    DroppedElement(
                        text=block.text,
                        reason=f"non_prose_type:{block.type.upper()}",
                        source_pages=(block.page,),
                        source_block_ids=(block.block_id,),
                    )
                )
            continue
        if drop_bare_numbers and _BARE_NUMBER.match(block.text.strip()):
            dropped.append(
                DroppedElement(
                    text=block.text,
                    reason="bare_number",
                    source_pages=(block.page,),
                    source_block_ids=(block.block_id,),
                )
            )
            continue
        prose.append(block)

    if not prose:
        return PredictedDocument(document_id=document_id, dropped=dropped)

    sentences: list[PredictedSentence] = []
    if join_across_blocks:
        joined = dehyphenate(" ".join(normalize_text(b.text) for b in prose))
        pages = tuple(sorted({b.page for b in prose}))
        block_ids = tuple(b.block_id for b in prose)
        for text in segment(joined, model):
            sentences.append(
                PredictedSentence(
                    text=text, source_pages=pages, source_block_ids=block_ids
                )
            )
    else:
        for block in prose:
            for text in segment(normalize_text(block.text), model):
                sentences.append(
                    PredictedSentence(
                        text=text,
                        source_pages=(block.page,),
                        source_block_ids=(block.block_id,),
                    )
                )

    return PredictedDocument(
        document_id=document_id,
        sentences=sentences,
        dropped=dropped,
        diagnostics={
            "blocks_total": len(blocks),
            "blocks_prose": len(prose),
            "blocks_dropped": len(dropped),
            "join_across_blocks": join_across_blocks,
            "drop_bare_numbers": drop_bare_numbers,
        },
    )


class BlockJsonSource:
    """Committed benchmark inputs — ``<document_id>.blocks.json``.

    Payload shape (mirrors ``book_blocks``)::

        {"document_id": "...",
         "blocks": [{"block_id": "b1", "page": 1, "type": "TEXT", "text": "…"}]}
    """

    def __init__(
        self,
        directory: Path,
        name: str = "blocks_baseline",
        model: str = DEFAULT_MODEL,
        drop_bare_numbers: bool = False,
        join_across_blocks: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.name = name
        self.model = model
        self.drop_bare_numbers = drop_bare_numbers
        self.join_across_blocks = join_across_blocks

    def available_documents(self) -> Iterable[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            p.name[: -len(".blocks.json")] for p in self.directory.glob("*.blocks.json")
        )

    def load(self, document_id: str) -> PredictedDocument:
        path = self.directory / f"{document_id}.blocks.json"
        if not path.is_file():
            raise FileNotFoundError(f"no benchmark input: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        blocks = [
            _Block(
                block_id=str(b.get("block_id", f"b{i}")),
                page=int(b.get("page", 1)),
                type=str(b.get("type", "TEXT")),
                text=str(b.get("text", "")),
            )
            for i, b in enumerate(payload.get("blocks", []))
        ]
        return _blocks_to_document(
            document_id,
            blocks,
            self.model,
            self.drop_bare_numbers,
            self.join_across_blocks,
        )


class DoclingLayoutJsonSource:
    """The real local corpus — ``files/json/<name>_layout.json``.

    Gitignored commercial material, so nothing here is committed and the
    committed test suite must pass without it. Element order is Docling's
    ``iterate_items()`` sequence, which is inherited unvalidated — that is
    itself one of the things worth measuring.
    """

    SUFFIX = "_layout.json"

    def __init__(
        self,
        directory: Path,
        name: str = "docling_json",
        model: str = DEFAULT_MODEL,
        drop_bare_numbers: bool = False,
        join_across_blocks: bool = True,
    ) -> None:
        self.directory = Path(directory)
        self.name = name
        self.model = model
        self.drop_bare_numbers = drop_bare_numbers
        self.join_across_blocks = join_across_blocks

    def available_documents(self) -> Iterable[str]:
        if not self.directory.is_dir():
            return []
        return sorted(
            p.name[: -len(self.SUFFIX)] for p in self.directory.glob(f"*{self.SUFFIX}")
        )

    def load(self, document_id: str) -> PredictedDocument:
        path = self.directory / f"{document_id}{self.SUFFIX}"
        if not path.is_file():
            raise FileNotFoundError(f"no layout json: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        blocks = [
            _Block(
                block_id=f"e{i}",
                page=int(el.get("page", 1)),
                type=str(el.get("type", "TEXT")),
                text=str(el.get("text", "")),
            )
            for i, el in enumerate(payload.get("elements", []))
        ]
        return _blocks_to_document(
            document_id,
            blocks,
            self.model,
            self.drop_bare_numbers,
            self.join_across_blocks,
        )
