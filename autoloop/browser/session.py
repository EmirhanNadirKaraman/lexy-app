"""Generic browser-session protocol.

`BrowserChatGPT` is written against this minimal surface so the real Playwright
implementation and the in-memory fake used in tests are interchangeable.

Two deliberate omissions:

* **No `fill()`.** Setting a contenteditable's text directly does not drive the
  events ChatGPT's ProseMirror editor listens for — the DOM looked full while
  the editor's own state stayed empty, which is how the 2026-07-29 smoke test
  sent nothing while appearing to succeed. Input goes through
  `focus` + `press` + `insert_text` instead.
* **No cookie / storage accessors.** Diagnostics must never be able to capture
  authentication material, so the protocol simply cannot reach it.

Two **optional** capabilities are deliberately NOT part of this Protocol.
`BrowserChatGPT` probes each with `getattr` and behaves exactly as it did
before when it is absent, so the in-memory fakes and any future provider
adapter stay valid without implementing either:

* `start_send_observation` / `take_send_observations` — a passive network
  listener over the send request. See `observation.py`.
* `scroll_to_end(selector) -> bool | None` — scroll the LAST match of
  `selector` into view, to paint more of a virtualized list, and report the
  position it reached. ChatGPT renders a window of a conversation rather than
  its history, so a readback that concludes "absent" from what is currently
  painted is reporting the scroll position (see
  `BrowserChatGPT._mount_message_tail`).

  Return **True** when the list's own scroll container is demonstrably at its
  end after the gesture (a list short enough that nothing scrolls counts: all
  of it is in view), **False** when there is more below, and **None** when the
  position cannot be measured. An adapter that returns None — including one
  written before this capability reported anything, and the End-key fallback
  the client uses when the capability is absent entirely — is not penalised for
  a SIGHTING, but it can never establish ABSENCE: an unchanged window means
  "the gesture stopped mounting", which is the tail when the gesture works and
  the opening window when it silently missed (End goes to whatever holds
  focus). Without a position signal those two are the same observation, and the
  client answers `ConversationSearchInconclusive` rather than picking one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Message:
    role: str  # "user" | "assistant" (value of the role attribute)
    text: str


class BrowserSession(Protocol):
    def goto(self, url: str) -> None:
        """Navigate to `url`. An explicit act — never a polling side effect."""
        ...

    def reload(self) -> None:
        """Reload the current page. Used only by explicit reconciliation."""
        ...

    def url(self) -> str: ...

    def exists(self, selector: str) -> bool: ...

    def is_enabled(self, selector: str) -> bool:
        """True when the first match exists AND is not disabled."""
        ...

    def click(self, selector: str) -> None: ...

    def focus(self, selector: str) -> None:
        """Put the keyboard caret in the element (a real click on it)."""
        ...

    def press(self, keys: str) -> None:
        """Press a key or chord, e.g. "ControlOrMeta+a" or "Delete"."""
        ...

    def insert_text(self, text: str) -> None:
        """Insert text at the caret as an input event the editor can observe."""
        ...

    def inner_text(self, selector: str) -> str:
        """Rendered text of the first match ("" when absent)."""
        ...

    def elements(self, selector: str, attr: str) -> list[tuple[str, str]]:
        """(attr value, inner text) for each match, in DOM order."""
        ...

    def screenshot(self, path: Path) -> None: ...

    def html(self) -> str: ...

    def close(self) -> None: ...
