"""Deciding whether a failed `codex` invocation ran out of quota.

This is a pure function over `(returncode, stdout, stderr)` for one reason: the
exact wording OpenAI uses when a ChatGPT plan's agentic allowance is exhausted
cannot be verified from this repository, and it will change. Keeping the
decision pure and pattern-driven means confirming it against the real CLI is a
config edit and a test case, never a debugging session inside a subprocess
wrapper.

**Why this matters more than it looks.** Misclassifying quota exhaustion as a
generic failure is the same mistake `ConversationUnusableError` exists to avoid
on the browser side: a generic error burns `policy.max_consecutive_failures`
(three, by default) within seconds and lands the loop in `failed`, which tells
the operator nothing about what actually happened. Classified, it parks
immediately — or hands over to the fallback provider — and says so.

**The patterns are a starting point, not a verified list.** Every non-zero exit
is logged to the transcript with its return code and a truncated stderr tail
(`autoloop.codex.conversation`), so the first real exhaustion shows exactly what
to add to `codex.quota_patterns`. Until then a missed pattern degrades to an
ordinary failure — noisy, but never unsafe: an unrecognised failure cannot
authorize anything, and re-running a stateless CLI call cannot double-post.
"""

from __future__ import annotations

#: Substrings that mark an exhausted allowance rather than a broken invocation.
#: Matched case-insensitively against stdout and stderr together, and ONLY when
#: the process already failed — a successful run that happens to mention "rate
#: limit" in a review is not an exhaustion event.
DEFAULT_QUOTA_PATTERNS: tuple[str, ...] = (
    "usage limit",
    "usage limits",
    "rate limit",
    "rate_limit",
    "quota",
    "out of credits",
    "insufficient credits",
    "credit balance",
    "too many requests",
    "429",
    "upgrade your plan",
    "purchase additional credits",
)

#: How much of stderr is worth keeping for a diagnostic. Bounded because this
#: text reaches the transcript, and an unbounded CLI dump there is both noise
#: and a place for content nobody audited to accumulate.
STDERR_TAIL_CHARS = 400


def is_quota_exhausted(
    returncode: int,
    stdout: str,
    stderr: str,
    patterns: tuple[str, ...] = DEFAULT_QUOTA_PATTERNS,
) -> bool:
    """True when a FAILED invocation failed because the allowance is spent.

    `returncode == 0` is never exhaustion, whatever the output says: the run
    produced a review, and the loop should use it. OpenAI's documented
    behaviour is a soft stop — a turn already in flight is allowed to finish —
    so a successful run at the boundary is the expected shape, not an edge
    case to defend against.
    """
    if returncode == 0:
        return False
    haystack = f"{stdout}\n{stderr}".lower()
    return any(pattern.lower() in haystack for pattern in patterns)


def failure_digest(returncode: int, stderr: str) -> dict:
    """Secret-free summary of a failed invocation, for the transcript.

    Deliberately the return code plus a bounded stderr TAIL: the tail is where
    a CLI puts its error, and bounding it keeps an unexpected dump out of the
    audit log. Never includes the prompt, stdout, argv or environment — the
    prompt carries repository content and the environment carries auth.
    """
    tail = stderr.strip()[-STDERR_TAIL_CHARS:]
    return {"returncode": returncode, "stderr_tail": tail}
