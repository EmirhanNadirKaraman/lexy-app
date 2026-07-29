"""Document-ingestion evaluation harness (roadmap A1).

A reproducible way to measure any ingestion pipeline against hand-written gold
annotations. Deliberately dependency-light: no LLM, no ``nlp_histo`` import, no
Docling, no torch. The only non-stdlib import in the whole package is spaCy,
and only in :mod:`evaluation.segmentation`, which produces the *predicted*
segmentation for the deterministic baseline.

See ``docs/INGESTION_PIPELINE.md`` for the architecture this measures and
``benchmark/README.md`` for how to add and annotate documents.
"""

SCHEMA_VERSION = "1.0.0"
"""Version of the run-result JSON written by :mod:`evaluation.report`.

Bump on any change to the metric definitions or the result payload shape.
:func:`evaluation.compare.compare_runs` refuses to diff runs whose schema
versions differ, so a bump is what stops an apples-to-oranges comparison.
"""
