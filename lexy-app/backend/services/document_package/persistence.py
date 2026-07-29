"""The persistence seam.

Parsing and validation must not know how rows get written, and the writer must
not know how a package is parsed. That separation is the point: **A3** adds
``book_sentences``, ``book_blocks.reading_order`` and the ``block_index``
backfill, and it should be able to plug in here without touching a single
validator.

Three implementations, none of them a temporary hack:

* :class:`PersistenceBackend` — the Protocol A3 implements.
* :class:`DryRunPersistence` — a *complete* implementation that writes nothing
  and reports exactly what a real import would do. This is what makes dry-run
  meaningful rather than a stubbed-out branch.
* :class:`PendingSchemaPersistence` — the honest default for real imports until
  A3 lands. It refuses loudly and names the blocking task, rather than writing
  partial rows into a schema that cannot hold them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from . import contract
from .coordinates import CoordinateError, page_height_for, to_fitz
from .loader import LoadedPackage


class PersistenceNotAvailable(RuntimeError):
    """Raised when real persistence is requested but the schema cannot hold it."""


@dataclass
class PlannedBlock:
    """One ``book_blocks`` row an import would write.

    Built during dry-run *and* real import, so the two paths compute the same
    thing and a dry-run cannot quietly disagree with what follows it.
    """

    element_id: str
    page_index: int
    block_index: int
    block_type: str
    text: str
    reading_order: int | None = None
    bbox: dict[str, float] | None = None
    """Already converted to fitz space (§6). ``None`` when the element had no
    bbox or the conversion failed — recorded, never silently zeroed."""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class PersistOutcome:
    """What a persistence backend did, or would have done."""

    doc_id: str | None = None
    pages_written: int = 0
    blocks_written: int = 0
    dry_run: bool = True
    skipped: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class PersistenceBackend(Protocol):
    """What A3 must implement.

    Receives an already-validated package plus the plan built from it. It is
    explicitly **not** responsible for validation — by the time it is called,
    every gate has passed and no fatal issue exists.
    """

    name: str

    async def persist(
        self,
        pkg: LoadedPackage,
        plan: list[PlannedBlock],
        *,
        user_id: str,
    ) -> PersistOutcome:
        ...


# ---------------------------------------------------------------------------
# planning — shared by every backend
# ---------------------------------------------------------------------------


def build_plan(pkg: LoadedPackage) -> tuple[list[PlannedBlock], list[dict]]:
    """Turn validated elements into the rows an import would write.

    Returns ``(plan, skipped)``. ``skipped`` is **itemised, not counted** —
    "34 elements vanished" is the failure mode hardest to notice later, and an
    itemised list is what makes a false deletion auditable (§4).

    Ordering: by ``reading_order`` when every element has one, otherwise by
    document order. ``block_index`` is written **dense and contiguous** per
    page, which is what the existing native path fails to do
    (``book_service.py:142`` leaves PyMuPDF's ``block_no`` with gaps).
    """
    elements = [e for e in pkg.elements if isinstance(e, dict)]
    orders = [e.get("reading_order") for e in elements]
    if elements and all(isinstance(o, int) and not isinstance(o, bool) for o in orders):
        elements = sorted(elements, key=lambda e: e["reading_order"])

    plan: list[PlannedBlock] = []
    skipped: list[dict] = []
    per_page_counter: dict[int, int] = {}

    for element in elements:
        element_id = element.get("id")
        if not isinstance(element_id, str):
            skipped.append(
                {"element_id": None, "reason": "missing_id", "stage": "plan"}
            )
            continue

        text = element.get("text")
        text = text if isinstance(text, str) else ""
        page = element.get("page_index")
        if not isinstance(page, int) or isinstance(page, bool):
            skipped.append(
                {"element_id": element_id, "reason": "invalid_page_index", "stage": "plan"}
            )
            continue

        element_type = element.get("type")
        if not isinstance(element_type, str) or element_type not in contract.ELEMENT_TYPES:
            element_type = "unknown"

        if not text.strip():
            # Kept out of the block plan because an empty block is not reader
            # material, but recorded so the omission is visible rather than
            # inferred from a count mismatch.
            skipped.append(
                {"element_id": element_id, "reason": "empty_text", "stage": "plan"}
            )
            continue

        bbox_payload = element.get("bbox")
        converted: dict[str, float] | None = None
        if isinstance(bbox_payload, dict):
            try:
                height = page_height_for(pkg.page_dims, page)
                converted = to_fitz(bbox_payload, height).as_columns()
            except CoordinateError:
                # Import the text without geometry rather than dropping the
                # element: losing content is irreversible, losing a bbox is not.
                converted = None

        index = per_page_counter.get(page, 0)
        per_page_counter[page] = index + 1
        order = element.get("reading_order")
        plan.append(
            PlannedBlock(
                element_id=element_id,
                page_index=page,
                block_index=index,
                block_type=element_type,
                text=text,
                reading_order=order if isinstance(order, int) and not isinstance(order, bool) else None,
                bbox=converted,
            )
        )

    return plan, skipped


# ---------------------------------------------------------------------------
# backends
# ---------------------------------------------------------------------------


class DryRunPersistence:
    """Computes the full plan, writes nothing.

    A complete implementation rather than a no-op: it reports the same page and
    block counts a real import would, so a dry-run is genuinely predictive.
    """

    name = "dry_run"

    async def persist(
        self,
        pkg: LoadedPackage,
        plan: list[PlannedBlock],
        *,
        user_id: str,
    ) -> PersistOutcome:
        pages = {block.page_index for block in plan}
        return PersistOutcome(
            doc_id=None,
            pages_written=len(pages),
            blocks_written=len(plan),
            dry_run=True,
            notes=[
                "dry run — no database mutation was attempted",
                f"would write {len(plan)} block(s) across {len(pages)} page(s)",
            ],
        )


class PendingSchemaPersistence:
    """The honest default for real imports until A3 lands.

    Refuses rather than writing partial rows. ``book_blocks`` today has no
    ``reading_order`` column and its ``block_type`` is only ever ``'text'``, so
    a "real" import right now would silently discard the two things the package
    exists to deliver — which is exactly the irreversible information loss the
    architecture forbids.
    """

    name = "pending_a3"

    async def persist(
        self,
        pkg: LoadedPackage,
        plan: list[PlannedBlock],
        *,
        user_id: str,
    ) -> PersistOutcome:
        raise PersistenceNotAvailable(
            "real persistence requires roadmap task A3 (migration 038: "
            "book_sentences, book_blocks.reading_order, dense block_index). "
            "Until then use dry_run=True — the plan is computed identically, "
            "so nothing is lost by waiting."
        )
