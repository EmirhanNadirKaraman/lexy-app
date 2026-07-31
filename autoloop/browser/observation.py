"""Passive observation of the ChatGPT page's own send traffic.

The transport reads a *rendered page*. That page is an optimistic, lossy
projection of what the server did — the lesson of both entries in
`docs/COMMON_ERRORS.md` §6. When a send silently fails to persist, the DOM
alone produces the same evidence as a send that succeeded but was never
observed, so the loop cannot tell "definitely not sent" from "unknown" and has
to park for a human every time.

This module adds the one signal the DOM cannot carry: whether the browser's own
request to the conversation endpoint *succeeded*. It is strictly an observer —
it issues nothing, it is not a second transport, and it cannot become one
(`SendObservation` has nowhere to put a response body, and the session protocol
exposes no request method).

**What is deliberately not captured.** No cookies, no `Authorization` header, no
headers at all, no request body, no response body, no query string. A
`SendObservation` carries an HTTP status, a path, and a coarse failure kind —
that is the whole vocabulary, so a diagnostic dump cannot leak credentials even
by accident.

**Correlation is temporal, not by id.** With bodies off-limits there is no
request id to match on, so an observation counts as ours when it lands on an
allowlisted send path inside the window bounded by our own Send click.
`BrowserChatGPT._wait_not_generating` guarantees no generation is in flight when
that window opens, and the loop is the only actor in its dedicated conversation.
That is sound for a single-actor conversation and nothing else, which is why
anything ambiguous inside the window degrades to UNKNOWN rather than resolving
to a verdict — see `classify_submission`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

#: Paths whose responses report whether a *new turn* was accepted. Deliberately
#: an allowlist and deliberately narrow: the web client fires a crowd of other
#: `/backend-api/*` calls around a send (telemetry, model lists, conversation
#: metadata), and a 200 from any of those says nothing about our turn. Matching
#: is on the path prefix only — never the query string, which can carry ids.
SEND_PATH_PREFIXES: tuple[str, ...] = (
    "/backend-api/conversation",
    "/backend-api/f/conversation",
    "/backend-alt/conversation",
)

#: Paths that look like a send but are a *different* operation on the same
#: prefix (renaming a chat, fetching history, generating a title). Checked
#: before `SEND_PATH_PREFIXES` so they are excluded rather than mistaken for a
#: turn submission.
NOT_A_SEND_SUFFIXES: tuple[str, ...] = (
    "/init",
    "/gen_title",
    "/textdocs",
)


class SendOutcome(str, Enum):
    #: The backend demonstrably accepted the turn.
    ACCEPTED = "accepted"
    #: The backend demonstrably refused it, or the request never completed.
    REJECTED = "rejected"
    #: Evidence is missing, partial or self-contradictory. Never actionable.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SendObservation:
    """One observed send attempt. Secret-free by construction.

    `path` is a URL path with no query and no fragment. `status` is `None` when
    the request never produced a response (`failure` then says why, in
    Playwright's coarse wording — e.g. `net::ERR_INTERNET_DISCONNECTED`).
    """

    path: str
    status: int | None = None
    failure: str = ""

    @property
    def outcome(self) -> SendOutcome:
        if self.status is None:
            # No response at all: the turn cannot have been accepted.
            return SendOutcome.REJECTED
        if 200 <= self.status < 300:
            return SendOutcome.ACCEPTED
        if self.status >= 400:
            return SendOutcome.REJECTED
        # 1xx/3xx on a send endpoint is not a thing we have a reading for.
        return SendOutcome.UNKNOWN


def is_send_path(url: str) -> bool:
    """True when `url`'s path is a turn-submission endpoint.

    Query strings are dropped before matching, so a path never carries an id
    into a log line.
    """
    path = urlsplit(url).path.rstrip("/")
    if not path:
        return False
    if any(path.endswith(suffix) for suffix in NOT_A_SEND_SUFFIXES):
        return False
    return any(
        path == prefix or path.startswith(prefix + "/") for prefix in SEND_PATH_PREFIXES
    )


def scrub_path(url: str) -> str:
    """The path component alone — no scheme, host, query or fragment."""
    return urlsplit(url).path


def classify_submission(observations: list[SendObservation]) -> SendOutcome:
    """Fold the observations made during one Send click into one verdict.

    Conservative on purpose, because the two verdicts unlock different
    automatic behaviour and the wrong one is expensive in opposite directions:

    * **No observation at all** → UNKNOWN. The capability may be absent, the
      listener may have attached late, or the browser may have issued the
      request without us seeing the response. "We saw nothing" is not
      "nothing happened".
    * **Exactly one observation** → its own outcome.
    * **Several that agree** → that shared outcome.
    * **Several that disagree** (a retry behind the scenes, a failed request
      followed by a successful one) → UNKNOWN. A mixed window is precisely the
      case where a resend could double-post.

    ACCEPTED is safe to act on even though a 2xx is not by itself proof of
    persistence: it only routes to `awaiting`, and `_response_started` still
    requires our request id to be present in the conversation before anything
    is read. A 200 on a stream that then dies therefore falls through to the
    ordinary response-start timeout instead of being mistaken for a reply. Do
    not "simplify" that history check away on the strength of the status code.
    """
    if not observations:
        return SendOutcome.UNKNOWN
    outcomes = {obs.outcome for obs in observations}
    if len(outcomes) == 1:
        return outcomes.pop()
    return SendOutcome.UNKNOWN
