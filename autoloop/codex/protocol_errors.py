"""Turning a JSON-RPC error object into one of autoloop's routed failures.

This is the half of the app-server transport that exists because of a measured
weakness, not because a protocol was available. `codex/quota.py` decides whether
an allowance is spent by substring-matching stderr, and says so in its own
docstring: "the exact wording OpenAI uses … cannot be verified from this
repository, and it will change." Every failure there is a text blob and every
classification is a guess about prose.

An app-server failure is an **object**: `{"code": …, "message": …, "data": {…}}`.
So this module reads NAMED FIELDS and compares them for equality —
`error.code`, and `data.type` / `data.code` / `data.kind` / `data.status` /
`data.httpStatus` — instead of scanning a free-form string. That is the
difference being claimed, and it is worth being precise about its limit:

* **What is structural, and therefore solid.** Which fields are read, that a
  numeric `429` in a status field is a THROTTLE whatever the prose says, and
  that a failure this module does not recognise becomes a plainly-named
  `CodexProtocolError` rather than being quietly folded into "quota" or
  "success".
* **What is still a list.** The two *vocabularies* — `usage_limit_reached` and
  friends for a spent allowance, `rate_limit_exceeded` and friends for a
  throttle. The committed protocol reference carries no error-code enumeration
  (`ErrorNotification`'s body is one of the `v2/` declarations that are
  referenced but not concatenated into it), so the values cannot be checked
  against ground truth here any more than the stderr wordings could. The
  difference is that a value is now matched against a field that means "the
  error's type", not against everything the process happened to print, and both
  lists are overridable from config exactly as `quota_patterns` is.

**Transient is not spent, here too** (quota-01, 2026-08-23). This module
originally routed `rate_limit_exceeded`, `rate_limited`, `too_many_requests` and
a numeric `429` to `QuotaExhaustedError`, which is loop_fatal with no retry
path at all. A short-window throttle clears on a server-side timer; a weekly
allowance does not, and answering the first with a park is most of the harm
misclassification can do here. They are two lists now
(`DEFAULT_QUOTA_ERROR_CODES` and `DEFAULT_RATE_LIMIT_ERROR_CODES`), 429 lives in
the second, spent is tested FIRST so an error carrying both reads as spent, and
only spent reaches `QuotaExhaustedError`. Everything else — transient and
unrecognised alike — becomes a `CodexProtocolError`, which is a `BrowserError`
and therefore already on the ordinary retryable failure budget.

`RateLimitedError` is deliberately NOT raised for the transient case, even
though it owns the back-off budget: its handler
(`orchestrator._classify_rate_limit_state`) probes a held browser page and then
counts CDP page targets, and a codex deployment whose Chrome window is closed
answers that probe with ZERO targets and parks `loop_fatal` on
`browser_unattachable`. That trades one false park for another. `codex_cli`
makes the same choice for the same reason (`docs/AUTOLOOP.md` §5d).

Every unrecognised failure is logged with a bounded, secret-free digest, for the
same reason `quota.py` logs its stderr tail: the first real exhaustion in
production should turn into a one-line config edit, not an investigation. The
digest carries the classification too, so the transcript says which of the three
answers the routing took rather than leaving it to be inferred from the
exception that followed.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import AutoloopError, BrowserError, QuotaExhaustedError

#: Values that mean "the plan's allowance is SPENT" when they appear in an
#: error's TYPE field — the window is used up and waiting changes nothing, so
#: the loop hands the reviewer role over or parks. Compared case-insensitively,
#: and after folding `-`/space to `_`, so `Usage-Limit-Reached` and
#: `usage_limit_reached` are one entry.
#:
#: Nothing here describes a short-window throttle. That is the whole point of
#: the split; see `DEFAULT_RATE_LIMIT_ERROR_CODES`.
DEFAULT_QUOTA_ERROR_CODES: tuple[str, ...] = (
    "usage_limit_reached",
    "usage_limit_exceeded",
    "quota_exceeded",
    "insufficient_quota",
    "credit_limit_reached",
    "out_of_credits",
    "plan_limit_reached",
)

#: Values that mean "slow down" — a TRANSIENT throttle whose remedy is time,
#: not a different provider. These route to `CodexProtocolError`, which is a
#: `BrowserError` and therefore retryable on the ordinary failure budget; they
#: never raise `QuotaExhaustedError`.
DEFAULT_RATE_LIMIT_ERROR_CODES: tuple[str, ...] = (
    "rate_limit_exceeded",
    "rate_limit_reached",
    "rate_limited",
    "rate_limit",
    "too_many_requests",
    "throttled",
    "slow_down",
)

#: HTTP statuses that mean a transient throttle. Numeric, so no wording is
#: involved. `429` is "too many requests" — a status a server sends while it is
#: still willing to serve you later — and it lives HERE rather than in the spent
#: list for exactly the reason `"429"` lives in `quota.DEFAULT_RATE_LIMIT_PATTERNS`.
RATE_LIMIT_STATUS_CODES: tuple[int, ...] = (429,)

#: The three answers `classification_of` can give. Strings rather than an enum
#: because they are written straight into a transcript record and read by eye,
#: and deliberately the same three words `codex/quota.py` uses for the other
#: transport, so one operator vocabulary covers both.
QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
UNCLASSIFIED = "unclassified"

#: Fields on `error.data` that carry the error's own name.
_TYPE_FIELDS = ("type", "code", "kind", "error_type", "errorType", "reason")
#: Fields that carry an HTTP status.
_STATUS_FIELDS = ("status", "statusCode", "status_code", "httpStatus", "http_status")

#: Bound on the error text kept for the transcript. Same reasoning as
#: `quota.STDERR_TAIL_CHARS`: this reaches an audit log, and an unbounded server
#: message there is both noise and a place for unaudited content to accumulate.
MESSAGE_CHARS = 400


class CodexProtocolError(BrowserError):
    """The app-server answered with a failure this client cannot act on.

    A `BrowserError` so the orchestrator's existing failure routing applies
    unchanged — the transport is broken or the server refused, which is the
    same class of event as a browser fault and belongs on the same budget.
    `code` and `error_type` are carried as fields rather than only inside the
    message so a caller can branch without parsing prose.

    `transient` marks the throttle case. It changes no routing — a throttle and
    an unrecognised protocol fault take the same retryable path, deliberately,
    because that path is the correct answer to both — and exists so a caller or
    a reader can tell "the server asked us to slow down" from "the server broke"
    without matching on the message.
    """

    def __init__(
        self,
        message: str,
        *,
        code: Any = None,
        error_type: str = "",
        transient: bool = False,
    ):
        self.code = code
        self.error_type = error_type
        self.transient = transient
        super().__init__(message)


def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def usable_codes(codes, fallback: tuple[str, ...]) -> tuple[str, ...]:
    """`codes` with blanks and non-strings dropped, or `fallback` if nothing is
    left.

    A configured `[""]` is a NON-EMPTY list of nothing: the plain
    `tuple(codes) or fallback` idiom passes it through, and the result then
    matches no error at all. Equality matching means that cannot over-classify
    — a blank entry matches nothing rather than everything, unlike the substring
    hole `quota._usable` exists to close — but a vocabulary that recognises
    nothing is still a check that has quietly switched itself off, which is the
    failure class this whole task is about. So the fallback tests the CONTENTS,
    not the list.
    """
    kept = tuple(
        code.strip() for code in (codes or ()) if isinstance(code, str) and code.strip()
    )
    return kept or fallback


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def error_type_of(error: Any) -> str:
    """The error's own name, normalized — `""` when it carries none.

    Reads `data.type`/`data.code`/… first and falls back to a STRING `code` on
    the error itself. A numeric `code` is deliberately not a type: JSON-RPC
    reserves those for transport-level faults (`-32601` and friends), and
    treating one as a vocabulary word would misfile "method not found" as
    whatever the list happens to contain.
    """
    if not isinstance(error, Mapping):
        return ""
    data = error.get("data")
    for field in _TYPE_FIELDS:
        if isinstance(data, Mapping):
            found = _normalize(data.get(field))
            if found:
                return found
    return _normalize(error.get("code"))


def status_of(error: Any) -> int | None:
    """An HTTP status carried by the error, or None.

    The top-level `code` is only read as a status when it is one of the statuses
    this module knows: JSON-RPC reserves that field for transport faults
    (`-32601` and friends), so a bare integer there is a status only when it
    plainly is one.
    """
    if not isinstance(error, Mapping):
        return None
    data = error.get("data")
    for field in _STATUS_FIELDS:
        if isinstance(data, Mapping):
            found = _as_int(data.get(field))
            if found is not None:
                return found
    code = _as_int(error.get("code"))
    return code if code in RATE_LIMIT_STATUS_CODES else None


def message_of(error: Any) -> str:
    if not isinstance(error, Mapping):
        return str(error)[:MESSAGE_CHARS]
    message = error.get("message")
    return (message if isinstance(message, str) else str(error))[:MESSAGE_CHARS]


def is_quota_exhausted(
    error: Any, *, quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES
) -> bool:
    """True when this error object names a SPENT allowance.

    One test, and it is an exact match on a named field: the error's own type,
    equal to a known marker. No substring of `message` is consulted,
    deliberately — a review that *discusses* rate limiting must never be able to
    look like one, which is the failure mode the stderr matcher in `quota.py`
    can only mitigate by requiring a non-zero exit first.

    A numeric 429 is deliberately NOT enough. It is the status of a short-window
    throttle, and this function's answer is what reaches `QuotaExhaustedError`,
    which is loop_fatal with no retry path; see `is_rate_limited`.

    The honest limit, stated because there is no prompt guard on this side to
    catch it: an operator who puts `rate_limited` into `codex.quota_error_codes`
    gets a loop_fatal park on a throttle. The comparison is exact and the list
    is their explicit statement, so nothing here can second-guess it.
    """
    found = error_type_of(error)
    return bool(found) and found in {_normalize(code) for code in quota_codes}


def is_rate_limited(
    error: Any,
    *,
    rate_limit_codes: tuple[str, ...] = DEFAULT_RATE_LIMIT_ERROR_CODES,
    quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES,
) -> bool:
    """True when this error object names a TRANSIENT throttle.

    Spent is tested first and wins, so an error carrying both — a
    `usage_limit_reached` type alongside a 429 status, which is exactly how a
    server would report a weekly allowance over HTTP — reads as spent rather
    than as something to retry.

    That check is repeated here rather than left to `classification_of` on
    purpose: this is a public predicate, and a caller asking it directly must
    get the same answer the routing gives. An empty `rate_limit_codes` means
    "recognise nothing as a throttle", which degrades to `UNCLASSIFIED` — still
    retryable, so the safe direction; the construction sites
    (`AppServerClient`, `conversation._codex_app_server_factory`) fall back to
    the built-in list rather than relying on that.
    """
    if is_quota_exhausted(error, quota_codes=quota_codes):
        return False
    if status_of(error) in RATE_LIMIT_STATUS_CODES:
        return True
    found = error_type_of(error)
    return bool(found) and found in {_normalize(code) for code in rate_limit_codes}


def classification_of(
    error: Any,
    *,
    quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES,
    rate_limit_codes: tuple[str, ...] = DEFAULT_RATE_LIMIT_ERROR_CODES,
) -> str:
    """`QUOTA_EXHAUSTED` / `RATE_LIMITED` / `UNCLASSIFIED` — the routing decision.

    The single source of the spent-beats-transient precedence, so the digest
    written to the transcript and the exception raised at the wire can never
    disagree about what a failure was.
    """
    if is_quota_exhausted(error, quota_codes=quota_codes):
        return QUOTA_EXHAUSTED
    if is_rate_limited(
        error, rate_limit_codes=rate_limit_codes, quota_codes=quota_codes
    ):
        return RATE_LIMITED
    return UNCLASSIFIED


def failure_digest(
    error: Any,
    *,
    quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES,
    rate_limit_codes: tuple[str, ...] = DEFAULT_RATE_LIMIT_ERROR_CODES,
) -> dict:
    """Secret-free, bounded summary of a protocol failure for the transcript.

    Carries the fields that determine routing, the decision they produced, and a
    truncated message. Never the params: those are the review packet.
    """
    return {
        "code": error.get("code") if isinstance(error, Mapping) else None,
        "error_type": error_type_of(error),
        "status": status_of(error),
        "classification": classification_of(
            error, quota_codes=quota_codes, rate_limit_codes=rate_limit_codes
        ),
        "message": message_of(error),
    }


def classify(
    error: Any,
    *,
    quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES,
    rate_limit_codes: tuple[str, ...] = DEFAULT_RATE_LIMIT_ERROR_CODES,
    context: str = "",
) -> AutoloopError:
    """The exception this error object should be raised as.

    Returns rather than raises so the caller's traceback starts at the wire,
    and so a caller can log the digest and the decision together.

    Only a SPENT allowance becomes `QuotaExhaustedError`. A throttle and an
    unrecognised fault both become `CodexProtocolError` — a `BrowserError`, so
    the orchestrator's ordinary retryable failure budget applies — and the
    throttle carries `transient=True` plus a message that says which it is.
    """
    where = f" during {context}" if context else ""
    verdict = classification_of(
        error, quota_codes=quota_codes, rate_limit_codes=rate_limit_codes
    )
    if verdict == QUOTA_EXHAUSTED:
        return QuotaExhaustedError(
            "the Codex allowance for this ChatGPT plan is exhausted — the "
            f"app-server refused with {error_type_of(error)}"
            f"{where}. Codex shares an agentic pool with ChatGPT Work and "
            "ChatGPT for Excel; ordinary ChatGPT conversations draw on a "
            "separate quota, which is why the browser fallback can still run."
        )
    code = error.get("code") if isinstance(error, Mapping) else None
    if verdict == RATE_LIMITED:
        return CodexProtocolError(
            "the codex app-server is throttling this account"
            f"{where}: {message_of(error)}. That is a short-window RATE LIMIT, "
            "not a spent allowance — it clears on a server-side timer, so this "
            "stays on the ordinary retryable budget instead of parking the loop.",
            code=code,
            error_type=error_type_of(error),
            transient=True,
        )
    return CodexProtocolError(
        f"the codex app-server failed{where}: {message_of(error)}",
        code=code,
        error_type=error_type_of(error),
    )
