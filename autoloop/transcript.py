"""Append-only JSONL transcript of everything the loop does.

One entry per event: requests prepared/submitted, raw responses, parsed
directives, policy verdicts, git actions, executor outcomes, errors,
diagnostics pointers. The transcript is the audit log — it is never read back
by the loop itself (recovery uses `state.py`), so its format can grow without
migration concerns. Payload fields live under "data" to avoid key collisions.
"""

from __future__ import annotations

import json
from pathlib import Path

from .state import utcnow_iso


class TranscriptLogger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        entry_type: str,
        *,
        iteration: int | None = None,
        request_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        entry: dict = {"ts": utcnow_iso(), "type": entry_type}
        if iteration is not None:
            entry["iteration"] = iteration
        if request_id is not None:
            entry["request_id"] = request_id
        if data:
            entry["data"] = data
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
