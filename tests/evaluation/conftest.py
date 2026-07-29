from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def benchmark_dir() -> Path:
    """The committed benchmark corpus."""
    return REPO_ROOT / "benchmark" / "documents"


@pytest.fixture
def tiny_corpus(tmp_path) -> Path:
    """A two-sentence gold document plus a matching damaged block input.

    Deliberately minimal so integration tests fail with an obvious cause. The
    block input splits the first sentence in two and adds a page number, which
    is enough to exercise joining, segmentation and artifact leakage.
    """
    import json

    (tmp_path / "mini.gold.txt").write_text(
        "==== PAGE 1 ====\nDer Hund schlief.\nDie Katze wachte.\n",
        encoding="utf-8",
    )
    (tmp_path / "mini.gold.json").write_text(
        json.dumps(
            {
                "phenomena": ["split_across_blocks", "page_number"],
                "expected_removals": [
                    {"text": "42", "kind": "page_number", "pages": [1]}
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "mini.blocks.json").write_text(
        json.dumps(
            {
                "document_id": "mini",
                "blocks": [
                    {"block_id": "b1", "page": 1, "type": "TEXT", "text": "Der Hund"},
                    {
                        "block_id": "b2",
                        "page": 1,
                        "type": "TEXT",
                        "text": "schlief. Die Katze wachte.",
                    },
                    {"block_id": "b3", "page": 1, "type": "TEXT", "text": "42"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path
