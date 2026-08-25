"""Nothing here may import `autoloop`.

That is a REQUIREMENT of this file, not an accident of it being short. pytest
applies a conftest to its whole directory tree, so every edge this module has
is an edge every test under `autoloop/tests/` has — and
`validation.select_validation_commands` (`[audit] test_selection = "reachable"`,
on by default since 2026-08-20) reads exactly that graph to decide which test
files a commit's changed paths can reach. While this file imported
`autoloop.orchestrator`, a change to almost any module in the package selected
the ENTIRE tree: measured 2026-08-25, a change to `autoloop/dashboard.py`
selected 92 test files. The selector was not wrong — every test really did
execute that import — which is why the fix had to be here rather than there.

What it imported for, and why that is gone (brw-16, 2026-08-25): a single
autouse fixture, `_no_live_cdp_probe`, stubbed
`Orchestrator._attachable_page_targets` so a hermetic suite would not dial a
real Chrome on 127.0.0.1:9222. That probe is only ever reached for a provider
`conversation.transport_is_browser_backed` says drives a browser, and no
registered provider does any more. Every module that still exercises the
browser-backed recovery machinery — `test_orchestrator.py`,
`test_rounds_and_restart.py`, `test_transport_recovery.py` (and
`test_conversation_retirement.py` through it) and
`test_transport_fault_recovery.py` — registers a browser-backed adapter of its
own through `register_provider(..., browser_backed=True)`, and the two whose
tests can reach the probe stub it on the orchestrator instance they built —
locally, visibly, in the file that needs it.

Do NOT re-add a fixture here that reaches `autoloop` through a STRING
monkeypatch target to dodge the import. That hides the edge from static
analysis while the runtime dependency remains, which makes the graph lie about
a real dependency — and the soundness of every narrowed validation run rests on
that graph telling the truth.
"""

import sys
from pathlib import Path

# Make `import autoloop` work regardless of where pytest is invoked from.
# A path manipulation, not an import: it adds no edge to the graph above.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
