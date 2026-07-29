"""Run an evaluation: gold corpus × one document source → a comparable result."""
from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION, baselines
from .annotation import discover_document_ids, gold_path, load_gold
from .metrics import DEFAULT_TOLERANCE, DocumentResult, evaluate_document, summarize
from .model import DocumentSource
from .segmentation import DEFAULT_MODEL, segmenter_info


@dataclass
class RunResult:
    """Everything needed to render a report or diff against another run."""

    label: str
    source_name: str
    corpus_dir: str
    corpus_hash: str
    segmenter: str
    tolerance: int
    created_at: str
    results: list[DocumentResult] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    environment: dict = field(default_factory=dict)

    @property
    def summary(self) -> dict:
        return summarize(self.results)

    def as_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "label": self.label,
            "source": self.source_name,
            "corpus_dir": self.corpus_dir,
            "corpus_hash": self.corpus_hash,
            "segmenter": self.segmenter,
            "tolerance": self.tolerance,
            "created_at": self.created_at,
            "environment": self.environment,
            "frozen_baselines": baselines.as_dict(),
            "summary": self.summary,
            "documents": [r.as_dict() for r in self.results],
            "skipped": self.skipped,
        }

    def write_json(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path


def corpus_hash(corpus_dir: Path, document_ids: list[str]) -> str:
    """Stable hash of the gold files in play.

    :func:`evaluation.compare.compare_runs` refuses to diff runs with different
    corpus hashes. Without that guard a "regression" could just be someone
    having edited an annotation, which is the most confusing possible way for
    the harness to lie.
    """
    digest = hashlib.sha256()
    for doc_id in sorted(document_ids):
        path = gold_path(corpus_dir, doc_id)
        digest.update(doc_id.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def run_evaluation(
    corpus_dir: Path,
    source: DocumentSource,
    label: str,
    document_ids: list[str] | None = None,
    tolerance: int = DEFAULT_TOLERANCE,
    model: str = DEFAULT_MODEL,
) -> RunResult:
    """Evaluate every gold document the *source* can also produce.

    A document present in gold but absent from the source is **skipped and
    recorded**, never silently dropped — a shrinking corpus would otherwise
    look like an improving score.
    """
    corpus_dir = Path(corpus_dir)
    gold_ids = document_ids or discover_document_ids(corpus_dir)
    available = set(source.available_documents())

    results: list[DocumentResult] = []
    skipped: list[dict] = []

    for doc_id in gold_ids:
        if doc_id not in available:
            skipped.append({"document_id": doc_id, "reason": "not produced by source"})
            continue
        gold = load_gold(corpus_dir, doc_id)
        try:
            predicted = source.load(doc_id)
        except (OSError, ValueError) as exc:
            skipped.append({"document_id": doc_id, "reason": f"source error: {exc}"})
            continue
        results.append(evaluate_document(gold, predicted, tolerance))

    return RunResult(
        label=label,
        source_name=getattr(source, "name", type(source).__name__),
        corpus_dir=str(corpus_dir),
        corpus_hash=corpus_hash(corpus_dir, gold_ids),
        segmenter=segmenter_info(model),
        tolerance=tolerance,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        results=results,
        skipped=skipped,
        environment={
            "python": sys.version.split()[0],
            "platform": platform.platform(),
        },
    )
