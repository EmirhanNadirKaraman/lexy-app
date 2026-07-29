"""Generic browser-session protocol.

`ChatGPTClient` is written against this minimal surface so the real Playwright
implementation and the in-memory fake used in tests are interchangeable.
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
    def goto(self, url: str) -> None: ...

    def url(self) -> str: ...

    def exists(self, selector: str) -> bool: ...

    def click(self, selector: str) -> None: ...

    def fill(self, selector: str, text: str) -> None: ...

    def elements(self, selector: str, attr: str) -> list[tuple[str, str]]:
        """(attr value, inner text) for each match, in DOM order."""
        ...

    def screenshot(self, path: Path) -> None: ...

    def html(self) -> str: ...

    def close(self) -> None: ...
