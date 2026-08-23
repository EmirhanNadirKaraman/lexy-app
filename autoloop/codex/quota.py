"""What a failed `codex` invocation actually was — decided from codex's own
error output, never from an echo of what the loop sent it.

**The measured defect this module exists to close (2026-08-22).** `codex exec`
ECHOES THE WHOLE PROMPT BACK ON STDERR: a 180,024-byte review packet came back
verbatim. The classifier searched `f"{stdout}\\n{stderr}".lower()` for the bare
substrings `"429"`, `"quota"` and `"rate limit"`, so every word of the packet
was inside the haystack. A packet that quoted `docs/autoloop.md:4295` — a LINE
NUMBER — made a completely unrelated non-zero exit read as a spent allowance,
and `QuotaExhaustedError` is loop_fatal with no retry path. The loop parked
twice on an account that had used 4% of its weekly window. That is a
self-inflicted denial of service: the reviewer's own INPUT declared the account
exhausted.

**Two bounds, for two different jobs. Do not merge them.**

1. *Classification* is bounded by a GUARD, not by trying to carve the echo out
   of the stream. A marker classifies only when it does **not** occur in the
   prompt (`classify`). If the loop sent the characters `429`, an echoed `429`
   cannot fire — whatever framing codex wraps the echo in, whether it reflows
   it, indents it, or invents a new output format next release. Nothing here
   depends on recognising the echo's shape, which is why the property survives
   a codex upgrade.
2. *Diagnostics* are bounded by conservative echo REMOVAL (`strip_echoed_prompt`
   + `failure_digest`). Exact occurrences of the prompt are deleted and the
   text on BOTH sides is kept, because stderr in the wild looks like
   `Error: <the useful diagnostic>\\n<the prompt echo>` and a strip that keeps
   only what follows the echo throws the diagnostic away. Over-stripping here
   is the round-1 bug of this very task; under-stripping costs at most a
   bounded excerpt of text the loop already wrote itself.

**The price of the guard, stated plainly.** When a review packet genuinely
discusses usage limits, a genuine exhaustion during that round degrades to an
ordinary failure. That is the trade `quota.py` has always documented in the
other direction ("a missed pattern degrades to an ordinary failure — noisy, but
never unsafe: an unrecognised failure cannot authorize anything, and re-running
a stateless CLI call cannot double-post"), and it is the direction this task
asks for: fewer FALSE parks. It is never silent — every marker the guard
suppressed is named in the transcript record under `suppressed_patterns`.

**Transient is not spent.** A short-window 429 clears on a server-side timer; a
weekly allowance does not. They are two separate lists (`DEFAULT_QUOTA_PATTERNS`
and `DEFAULT_RATE_LIMIT_PATTERNS`), spent is tested FIRST so a message carrying
both reads as spent, and only spent reaches `QuotaExhaustedError`. Everything
else — transient and unrecognised alike — stays on the retryable path. Before
this split, `"429"`, `"too many requests"` and `"rate limit"` all lived in the
exhaustion list, so a thirty-second throttle was routed to the permanent branch.

**The wordings are still a list, and always will be.** The exact prose OpenAI
uses cannot be verified from this repository and will change. What changed is
that a wrong list can no longer be triggered by our own input, and that every
non-zero exit now records a real diagnostic (`failure_digest`, logged as
`codex_invocation_failed` by `codex/conversation.py`), so the first real
exhaustion is a one-line config edit rather than an investigation.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Substrings that mark a SPENT ALLOWANCE — the plan's window is used up and
#: waiting a minute changes nothing. Matched case-insensitively against
#: codex's output, ONLY when the process already failed, and ONLY when the
#: marker does not also occur in the prompt that was sent.
#:
#: Deliberately specific. `"quota"` on its own is a word that appears in
#: ordinary prose (this repository's own task descriptions discuss quotas by
#: name); `"quota exceeded"` is a sentence a CLI prints.
DEFAULT_QUOTA_PATTERNS: tuple[str, ...] = (
    "usage limit reached",
    "usage limit exceeded",
    "hit your usage limit",
    "reached your usage limit",
    "quota exceeded",
    "insufficient quota",
    "out of credits",
    "insufficient credits",
    "credit balance",
    "upgrade your plan",
    "purchase additional credits",
    "plan limit reached",
    "weekly limit",
    "monthly limit",
)

#: Substrings that mark a TRANSIENT throttle — the account is being asked to
#: slow down, and the remedy is time, not a different provider. These route to
#: the retryable path; they never raise `QuotaExhaustedError`.
#:
#: `"429"` lives HERE, not above. It is the status a short-window limit uses,
#: it is three characters that occur in line numbers, byte counts and diffs,
#: and routing it to a loop_fatal park is most of the harm this module exists
#: to undo.
DEFAULT_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "429",
    "retry after",
    "try again later",
    "slow down",
)

#: How much of each stream is worth keeping for a diagnostic. Bounded because
#: this text reaches the transcript, and an unbounded CLI dump there is both
#: noise and a place for content nobody audited to accumulate.
STDERR_TAIL_CHARS = 400

#: Ceiling on the suppressed-marker list carried in a record. The list exists so
#: the guard is visible, not so a pathological prompt can pad the audit log.
MAX_SUPPRESSED = 10

#: What an excised prompt echo leaves behind. A newline rather than a marker,
#: so `Error: A\n<echo>\nError: B` does not glue into `Error: AError: B` and so
#: a stream that was NOTHING but echo strips to empty — which is what makes
#: `failure_digest` able to say "the exit code is the whole diagnostic" instead
#: of printing a placeholder and calling it a record.
_ECHO_REPLACEMENT = "\n"

#: Inserted when a bounded excerpt had to drop its middle. Both ends are kept
#: because the useful line can be at either: a CLI that prints its error first
#: and its echo second, or the reverse.
_ELIDED = " […] "

#: The four answers `classify` can give. Strings rather than an enum because
#: they are written straight into a transcript record and read back by eye.
NOT_A_FAILURE = "not_a_failure"
QUOTA_EXHAUSTED = "quota_exhausted"
RATE_LIMITED = "rate_limited"
UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class CodexFailure:
    """What one failed invocation was, and what the guard refused to count.

    `suppressed` is not decoration. It is the difference between a guard and a
    silence: it names every marker that WAS present in codex's output and was
    ignored because the loop had sent those same characters, so an operator
    reading the transcript can see the guard fire rather than wonder why an
    exhaustion was never recognised.
    """

    kind: str
    matched: str = ""
    suppressed: tuple[str, ...] = ()

    @property
    def is_exhaustion(self) -> bool:
        return self.kind == QUOTA_EXHAUSTED

    @property
    def is_transient(self) -> bool:
        return self.kind == RATE_LIMITED


def _usable(patterns) -> tuple[str, ...]:
    """Lower-cased, de-duplicated, EMPTIES DROPPED.

    The empty string is a substring of everything, so one blank entry in a
    hand-edited `codex.quota_patterns` would classify every failure as an
    exhausted allowance — the original defect by a second door. Dropped here
    rather than validated at config load, so a value arriving from anywhere
    (config, a caller, a future provider) gets the same treatment.
    """
    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns or ():
        if not isinstance(pattern, str):
            continue
        needle = pattern.strip().lower()
        if not needle or needle in seen:
            continue
        seen.add(needle)
        out.append(needle)
    return tuple(out)


def classify(
    returncode: int,
    stdout: str,
    stderr: str,
    prompt: str,
    *,
    quota_patterns: tuple[str, ...] = DEFAULT_QUOTA_PATTERNS,
    rate_limit_patterns: tuple[str, ...] = DEFAULT_RATE_LIMIT_PATTERNS,
) -> CodexFailure:
    """What this invocation was. `prompt` is REQUIRED and is the guard.

    `prompt` has no default on purpose. A default would let a call site forget
    it and silently turn the guard off — an alarm that disables itself when its
    input is missing is exactly the failure this module exists to remove, and a
    signature that cannot be called without the evidence is a stronger
    statement than a comment asking callers to remember. Pass the FINAL prompt,
    the one actually handed to the process, not the template it was built from.

    `returncode == 0` is never a failure of any kind, whatever the output says:
    the run produced a review and the loop should use it. OpenAI's documented
    behaviour is a soft stop — a turn already in flight is allowed to finish —
    so a successful run at the boundary is the expected shape, not an edge case
    to defend against. The check is in front of BOTH classifiers, not only the
    exhaustion one.

    Spent is tested before transient so a message carrying both ("429 — you
    have hit your usage limit, resets Monday") reads as spent.
    """
    if returncode == 0:
        return CodexFailure(NOT_A_FAILURE)
    haystack = f"{stdout or ''}\n{stderr or ''}".lower()
    # A non-str prompt is a programming error the annotation already forbids;
    # treating it as "nothing was sent" keeps this path from raising while a
    # failure is being handled, and the suppressed list will show an empty
    # guard if it ever happens.
    sent = prompt.lower() if isinstance(prompt, str) else ""

    suppressed: list[str] = []

    def first_own_match(patterns) -> str:
        """The first marker present in the output that the loop did NOT send."""
        found = ""
        for needle in _usable(patterns):
            if needle not in haystack:
                continue
            if needle in sent:
                # Present, but it is our own text coming back. Recorded, never
                # counted. This is the whole fix.
                suppressed.append(needle)
                continue
            if not found:
                found = needle
        return found

    spent = first_own_match(quota_patterns)
    transient = first_own_match(rate_limit_patterns)
    ignored = tuple(suppressed[:MAX_SUPPRESSED])
    if spent:
        return CodexFailure(QUOTA_EXHAUSTED, spent, ignored)
    if transient:
        return CodexFailure(RATE_LIMITED, transient, ignored)
    return CodexFailure(UNCLASSIFIED, "", ignored)


def is_quota_exhausted(
    returncode: int,
    stdout: str,
    stderr: str,
    prompt: str,
    patterns: tuple[str, ...] = DEFAULT_QUOTA_PATTERNS,
) -> bool:
    """True when a FAILED invocation failed because the allowance is SPENT.

    A thin reading of `classify` with the transient list emptied, kept because
    "is this exhaustion?" is a question worth asking as a boolean. `prompt` is
    required here for the same reason it is required there.
    """
    verdict = classify(
        returncode,
        stdout,
        stderr,
        prompt,
        quota_patterns=patterns,
        rate_limit_patterns=(),
    )
    return verdict.is_exhaustion


def _echo_candidates(prompt: str) -> tuple[str, ...]:
    """The exact strings worth searching for as an echo, longest first."""
    if not isinstance(prompt, str):
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for candidate in (prompt, prompt.strip()):
        if candidate and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return tuple(sorted(out, key=len, reverse=True))


def strip_echoed_prompt(text: str, prompt: str) -> tuple[str, int]:
    """`(text with exact prompt echoes excised, characters excised)`.

    Conservative on purpose, and asymmetric with `classify` on purpose. Only
    EXACT occurrences of the prompt go; everything else stays, on both sides of
    the excision. `codex exec` prints `Error: <diagnostic>` and then the echo,
    so a strip that returns only the text FOLLOWING the echo deletes the one
    line worth having and can return the empty string — measured, and the
    finding that refused round 1 of this task.

    Not a security boundary and not load-bearing for classification: a codex
    build that reflows the echo defeats this, and nothing depends on it not
    doing so. The excerpt it feeds is bounded either way.
    """
    if not isinstance(text, str) or not text:
        return ("", 0)
    removed = 0
    for candidate in _echo_candidates(prompt):
        hits = text.count(candidate)
        if not hits:
            continue
        removed += hits * len(candidate)
        text = text.replace(candidate, _ECHO_REPLACEMENT)
    return (text, removed)


def _bounded(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    """At most `limit` characters, keeping BOTH ends when the middle must go."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_ELIDED))
    head = keep // 2
    return text[:head] + _ELIDED + text[len(text) - (keep - head):]


def failure_digest(
    returncode: int | None,
    stdout: str,
    stderr: str,
    prompt: str,
    *,
    request_id: str = "",
    classification: CodexFailure | None = None,
    note: str = "",
) -> dict:
    """Bounded, secret-free summary of a failed invocation, for the transcript.

    This is the record the whole task turns on. Across the entire 24-day
    transcript there were ZERO `codex_invocation_failed` entries, because the
    adapter was constructed without a logger — so when the loop parked on a
    false exhaustion, the exit code and the stderr that would have named the
    real fault had been discarded and the investigation had to start from the
    account API instead.

    What it carries and why:

    * `returncode` and `classification` — the routing decision and its input.
      Always present, so even a process that printed nothing at all leaves a
      record naming what happened to it. `returncode` is `None` for an
      invocation that never produced one — a binary that could not be launched,
      or a process killed at the timeout — rather than a fabricated `-1` that
      would read as a real exit status; the `note` names the fault instead.
    * `stderr_tail` / `stdout_tail` — bounded excerpts of what CODEX printed,
      with exact prompt echoes excised. Both, because a `codex exec` that fails
      before it writes to stderr puts its complaint on stdout, and round 1 of
      this task recorded nothing at all for that shape.
    * `stderr_chars` / `stdout_chars` / `prompt_echo_chars` — raw sizes, so an
      empty excerpt is readable as "all echo" or "printed nothing" rather than
      as "the record failed".
    * `suppressed_patterns` — the markers the guard refused to count because the
      prompt contained them. The guard's visibility.

    Never the prompt, the argv or the environment: the prompt is the review
    packet and the environment carries auth. `stdout_tail` is new as of this
    task and is a deliberate, narrow amendment to that rule — the reviewer's
    reply already reaches the transcript in full under `response_received.raw`,
    and the excerpt here is echo-stripped and bounded to
    `STDERR_TAIL_CHARS`. See docs/SECURITY.md.
    """
    own_err, echo_err = strip_echoed_prompt(stderr or "", prompt)
    own_out, echo_out = strip_echoed_prompt(stdout or "", prompt)
    err_tail = _bounded(own_err)
    out_tail = _bounded(own_out)
    verdict = classification or CodexFailure(
        NOT_A_FAILURE if returncode == 0 else UNCLASSIFIED
    )
    echoed = echo_err + echo_out
    digest = {
        "request_id": request_id,
        "returncode": returncode,
        "classification": verdict.kind,
        "matched_pattern": verdict.matched,
        "stderr_tail": err_tail,
        "stdout_tail": out_tail,
        "stderr_chars": len(stderr or ""),
        "stdout_chars": len(stdout or ""),
        "prompt_echo_chars": echoed,
    }
    if verdict.suppressed:
        digest["suppressed_patterns"] = list(verdict.suppressed)
    # Echo-stripped and bounded like the excerpts: a caller-supplied note can
    # carry an exception's message, and an exception raised by a runner this
    # module does not own could carry anything, the prompt included.
    reason = _bounded(strip_echoed_prompt(note, prompt)[0]) if note else ""
    if not reason and not err_tail and not out_tail:
        reason = (
            "codex printed nothing but an echo of the prompt"
            if echoed
            else "codex printed nothing at all"
        ) + "; the exit code is the whole diagnostic"
    if reason:
        digest["note"] = reason
    return digest


__all__ = [
    "DEFAULT_QUOTA_PATTERNS",
    "DEFAULT_RATE_LIMIT_PATTERNS",
    "MAX_SUPPRESSED",
    "NOT_A_FAILURE",
    "QUOTA_EXHAUSTED",
    "RATE_LIMITED",
    "STDERR_TAIL_CHARS",
    "UNCLASSIFIED",
    "CodexFailure",
    "classify",
    "failure_digest",
    "is_quota_exhausted",
    "strip_echoed_prompt",
]
