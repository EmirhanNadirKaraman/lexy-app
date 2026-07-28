"""Per-worker `llm_cache` namespacing for parallel pytest-xdist runs.

Same shape as `_email_helper`: tests tag every row they cause to be written
with the current worker id, and the autouse cleanup fixture in conftest deletes
only rows carrying that tag. A worker can therefore never reap another worker's
in-flight rows, and no fixture can reach a real row.

Why this exists
---------------
`llm_cache` is a GLOBAL table with no user FK, so the autouse user cleanup
cannot reach it. Before this helper, tests wrote to it and never cleaned up:
a dev database accumulated **2,760 rows, of which 2,042 came from
`test_llm_cache.py`'s literal `test_prompt` / `test` / `p` keys and another
718 from provider-fakes writing real prompt keys** (`item_gloss`,
`reading_translate`, `reading_explain`, `book_ocr_repair`). Every full-suite
run added more.

Two problems, not one:

1. **Residue.** Rows accumulate forever.
2. **Collision.** The provider-fakes reported the *production* model id, so
   their rows landed on exactly the cache keys production uses. All 335
   `item_gloss` rows in the dev database were fakes' output like
   `{"gloss": "stub"}` — sitting on the keys real glosses would occupy, and on
   the keys a curated gloss seed would want (TODO #43 step 2). A test run could
   hand `"stub gloss"` to a real SRS card.

The tag fixes both: a test row's model never equals a production model, so it
cannot occupy a production key, and it is always reapable.

Choosing the tag
----------------
The tag goes in the **model**, not the prompt key, because the model is the one
field every write path controls — direct `set_cached` calls *and* the fake
providers, whose `model_id` feeds `make_cache_key`. Tagging prompt keys instead
would miss the fakes, which must keep using real prompt keys (`item_gloss`)
for the code under test to behave normally.

**Cleanup must never key on `prompt_key`.** `item_gloss` will hold real curated
rows once TODO #43 step 2 lands; deleting by that key would wipe them.
"""
import os

#: Prefix for every model string a test causes to be written. Chosen so it can
#: never collide with a real model id (`claude-*`, an OpenAI-compatible model
#: name) or with the `curated:*` sentinel a future gloss seed would use.
TEST_MODEL_PREFIX = "zztest-model"


def worker_id() -> str:
    """The pytest-xdist worker id, or 'main' when running serially."""
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def test_model(suffix: str = "") -> str:
    """A worker-tagged model string for cache rows a test creates.

    Format: `zztest-model-{worker_id}` plus an optional suffix when a test
    needs two distinct models (e.g. asserting the key varies by model).
    """
    return f"{TEST_MODEL_PREFIX}-{worker_id()}{suffix}"


def cleanup_pattern() -> str:
    """LIKE pattern matching only THIS worker's cache rows.

    Used by the autouse `cleanup` fixture in conftest. Scoped to the worker so
    a finishing worker cannot delete rows another worker is still using.
    """
    return f"{TEST_MODEL_PREFIX}-{worker_id()}%"
