"""Autoloop — orchestration infrastructure for an autonomous Fable <-> ChatGPT
engineering loop.

The executor (an AI engineer with repo access) produces reports; ChatGPT —
driven through a real browser session, never the OpenAI API — reviews them and
answers with a machine-readable directive (see `contract.py`). The orchestrator
executes directives under a deterministic policy layer (`policy.py`) and
persists every step so the loop survives crashes (`state.py`).

Entry point: ``python -m autoloop run`` (see `cli.py` / docs/AUTOLOOP.md).
"""

__version__ = "0.1.0"
