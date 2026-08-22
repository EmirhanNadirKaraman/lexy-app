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
  numeric `429` in a status field is an exhaustion whatever the prose says, and
  that a failure this module does not recognise becomes a plainly-named
  `CodexProtocolError` rather than being quietly folded into "quota" or
  "success".
* **What is still a list.** The exhaustion *vocabulary* — `usage_limit_reached`
  and friends. The committed protocol reference carries no error-code
  enumeration (`ErrorNotification`'s body is one of the `v2/` declarations that
  are referenced but not concatenated into it), so the values cannot be checked
  against ground truth here any more than the stderr wordings could. The
  difference is that a value is now matched against a field that means "the
  error's type", not against everything the process happened to print, and the
  list is overridable from config exactly as `quota_patterns` is.

Every unrecognised failure is logged with a bounded, secret-free digest, for the
same reason `quota.py` logs its stderr tail: the first real exhaustion in
production should turn into a one-line config edit, not an investigation.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..errors import AutoloopError, BrowserError, QuotaExhaustedError

#: Values that mean "the plan's allowance is spent" when they appear in an
#: error's TYPE field. Compared case-insensitively, and after folding `-`/space
#: to `_`, so `Usage-Limit-Reached` and `usage_limit_reached` are one entry.
DEFAULT_QUOTA_ERROR_CODES: tuple[str, ...] = (
    "usage_limit_reached",
    "usage_limit_exceeded",
    "rate_limit_exceeded",
    "rate_limited",
    "quota_exceeded",
    "insufficient_quota",
    "credit_limit_reached",
    "out_of_credits",
    "plan_limit_reached",
    "too_many_requests",
)

#: HTTP statuses that mean the same thing. Numeric, so no wording is involved.
QUOTA_STATUS_CODES: tuple[int, ...] = (429,)

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
    """

    def __init__(self, message: str, *, code: Any = None, error_type: str = ""):
        self.code = code
        self.error_type = error_type
        super().__init__(message)


def _normalize(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().replace("-", "_").replace(" ", "_")


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
    """An HTTP status carried by the error, or None."""
    if not isinstance(error, Mapping):
        return None
    data = error.get("data")
    for field in _STATUS_FIELDS:
        if isinstance(data, Mapping):
            found = _as_int(data.get(field))
            if found is not None:
                return found
    return _as_int(error.get("code")) if _as_int(error.get("code")) in QUOTA_STATUS_CODES else None


def message_of(error: Any) -> str:
    if not isinstance(error, Mapping):
        return str(error)[:MESSAGE_CHARS]
    message = error.get("message")
    return (message if isinstance(message, str) else str(error))[:MESSAGE_CHARS]


def is_quota_exhausted(
    error: Any, *, quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES
) -> bool:
    """True when this error object names an exhausted allowance.

    Both tests are exact matches on a named field — an error type equal to a
    known marker, or a status equal to 429. No substring of `message` is
    consulted, deliberately: a review that *discusses* rate limiting must never
    be able to look like one, which is the failure mode the stderr matcher in
    `quota.py` can only mitigate by requiring a non-zero exit first.
    """
    if status_of(error) in QUOTA_STATUS_CODES:
        return True
    found = error_type_of(error)
    return bool(found) and found in {_normalize(code) for code in quota_codes}


def failure_digest(error: Any) -> dict:
    """Secret-free, bounded summary of a protocol failure for the transcript.

    Carries the two fields that determine routing plus a truncated message.
    Never the params: those are the review packet.
    """
    return {
        "code": error.get("code") if isinstance(error, Mapping) else None,
        "error_type": error_type_of(error),
        "status": status_of(error),
        "message": message_of(error),
    }


def classify(
    error: Any,
    *,
    quota_codes: tuple[str, ...] = DEFAULT_QUOTA_ERROR_CODES,
    context: str = "",
) -> AutoloopError:
    """The exception this error object should be raised as.

    Returns rather than raises so the caller's traceback starts at the wire,
    and so a caller can log the digest and the decision together.
    """
    where = f" during {context}" if context else ""
    if is_quota_exhausted(error, quota_codes=quota_codes):
        return QuotaExhaustedError(
            "the Codex allowance for this ChatGPT plan is exhausted — the "
            f"app-server refused with {error_type_of(error) or 'a 429'}"
            f"{where}. Codex shares an agentic pool with ChatGPT Work and "
            "ChatGPT for Excel; ordinary ChatGPT conversations draw on a "
            "separate quota, which is why the browser fallback can still run."
        )
    return CodexProtocolError(
        f"the codex app-server failed{where}: {message_of(error)}",
        code=error.get("code") if isinstance(error, Mapping) else None,
        error_type=error_type_of(error),
    )
