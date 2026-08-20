"""Read-only live tracker for a running Autoloop, served on localhost.

    python -m autoloop.dashboard --repo /path/to/checkout --port 8787

Why this exists: the audit fans out six headless `claude -p` subagents whose
output is captured only when each one FINISHES. From outside, a working loop and
a wedged loop look identical — an empty `raw/` directory and a silent stdout.
This reads the state the loop already writes, plus the process table, and shows
what is happening now.

**Reading is `read_text`, `glob`, and `ps`, and it writes nothing while doing
it.** Every observation path here is read-only, and that is not politeness: the
loop's escape detector snapshots the primary checkout around every
write-capable agent call, so a tracker that touched the working tree while
observing would park the loop.

There are exactly TWO write paths, both in `Handler.do_POST`, and they are
different in kind:

* **A new task is QUEUED** into the `TaskInbox` directory OUTSIDE the checkout,
  which the loop drains between steps. A creation carries `approved_paths` —
  authorization — so the loop merging it is the review step. Read `do_POST`'s
  docstring and `docs/SECURITY.md` S28 before widening what that accepts.
* **A priority is APPLIED IMMEDIATELY** (2026-08-16), straight into
  `.autoloop/tasks.json` through `TaskStore.apply_priority`, and the row then
  shows the value read BACK from that file. Queued was the previous design and
  it failed the operator: the change only became true when the loop next
  drained, the page kept re-rendering the old number from `tasks.json`
  meanwhile, and a save that worked was indistinguishable from one that did
  not. Priority is the one field safe to change mid-flight — nothing already
  dispatched depends on it — and it is the field an operator uses to steer what
  runs next, which is worthless if it lands minutes late.

  That write takes NO `LoopLock` (held for the whole run) and still writes
  nothing else: not `state.json`, not an execution record, not a blocker, and
  not `status` / `approved_paths` / `depends_on`. It is serialised against the
  loop's own saves by the fine-grained mutex in `tasks.py`, and attested in a
  ledger outside the checkout so the escape detector can tell it from an agent
  writing into the state dir.

The one thing it learns that is not already on disk: which domain each in-flight
agent is on, parsed from the process table. Agent prompts carry
`Your domain: <title>.`, so a live fan-out is legible without changing the loop.

The second thing it learns that no file records: whether a COMPLETED task's work
is actually in the branch. `completed` means published, not integrated, and the
difference is where finished work goes missing — see `merge_report` /
`is_ancestor` below, which ask git rather than the registry, and which read
nothing the rest of this module does not already read.

The roadmap is grouped by `TaskRegistry.state_of()` and by nothing else (see
`GROUPS` / `task_groups`). That is a correctness rule rather than a style
choice: `ready` vs `blocked` is DERIVED from dependencies and never stored, and
a stored `status="blocked"` means QUARANTINED — so a page that read the status
string would disagree with the code that dispatches, in both directions at
once. Constructing a registry is a pure read; no mutating registry method is
called from here, and no lock is taken.

Each live row carries its WHOLE description and opens to show it (2026-08-18).
The panel used to send `id`, `title` and `priority` and nothing else, so the one
question an operator has about a queued task — what does it actually say — could
only be answered by opening `.autoloop/tasks.json` by hand, and that file is
what the entire roadmap is steered from. The row's left rail carries the
EXECUTION ORDINAL from `_grouped`'s `ordinals` map, which is the ready set in
`next_ready()`'s own order; a task that cannot be picked has no number and says
what it waits on instead. Descriptions are untrusted text going into HTML and
are escaped by the page's `esc()` at every interpolation.

Three of those groups are the three ways a task can be not-running, and they
are separated because the operator's question differs: **Blocked** waits on a
dependency (nothing to do), **Needs a human** waits on you, **Retired** waits on
nobody — it was superseded, and its row names the successor rather than asking
for anything. Six of the seven `blocked` rows on 2026-08-14 were retirements,
which is what made the blocked count useless as a call to action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .errors import StateError, TaskGraphError
from .tasks import TaskRegistry, TaskState

#: Reserved status roles (dataviz palette). Never reused for anything else, and
#: every use in the page ships an icon + label so state is never colour-alone.
#: Fixed, never themed — the same four steps clear 3:1 on the dark surface.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

#: The pipeline's mark colours, and WHY they are these.
#:
#: A stage is not a category — the six stages are an ordered progression, and
#: what varies is how far along each one is. So exactly one stage carries a hue
#: (the one running now, categorical slot 1); finished stages recede to
#: secondary ink because history should not compete with the present; idle
#: stages sit on the axis neutral. `blocked` is the only state that is genuinely
#: a health verdict, so it is the only one allowed a status colour.
#:
#: The previous version painted `active` with status-good green, which is the
#: "status colour used for a non-status series" anti-pattern: "running" is not a
#: verdict about health, and reusing the good/bad channel for progress leaves
#: nothing to say with when something IS wrong.
#:
#: VALIDATED, not eyeballed — `scripts/validate_palette.js` from the dataviz
#: skill, against the surface the marks actually sit on (the node fill, not the
#: page):
#:   light  "#2a78d6,#d03b3b" --surface #f4f3f0  → ALL CHECKS PASS
#:          (CVD ΔE 23.8 protan / 33.5 tritan, normal 31.6, both >= 3:1)
#:   dark   "#3987e5,#d03b3b" --surface #2a2a27  → ALL CHECKS PASS, one WARN:
#:          critical sits at exactly 3.0:1, which obligates relief. Both reliefs
#:          are shipped — every mark carries an icon + word, and the stage table
#:          below the diagram repeats all of it as text.
#: A first attempt used sequential ramp steps for `done` and FAILED both modes
#: (light `#86b6ef` below the chroma floor and 1.9:1; dark `#184f95` outside the
#: lightness band) — recorded so nobody re-derives it.
MARKS = {
    "light": {"active": "#2a78d6", "done": "#52514e", "idle": "#c3c2b7"},
    "dark": {"active": "#3987e5", "done": "#c3c2b7", "idle": "#383835"},
}

_DOMAIN = re.compile(r"Your domain:\s*(.+?)\.")

#: This repository's own audit-report location, and the fallback whenever
#: `[repo].audit_report_glob` cannot be read (no config yet, unparseable TOML,
#: a value this module refuses). Spelled here rather than imported from
#: `config`: `load_config` is strict and would raise on a checkout with no
#: `browser.conversation_url`, and this page must render against any checkout —
#: the same reason `_inbox_dir` below reads the TOML directly. The two
#: spellings are pinned equal by `test_config_repo_section.py`.
DEFAULT_AUDIT_REPORT_GLOB = "docs/AUDIT_*.md"


def _is_safe_glob(pattern: str) -> bool:
    """Is `pattern` a plain repository-relative glob?

    `load_config` refuses an unsafe one at startup, but this module reads the
    raw TOML (see `DEFAULT_AUDIT_REPORT_GLOB`), so it never gets that check —
    and `Path.glob` raises outright on an absolute pattern, which would 500 the
    page rather than degrade it. Traversal is refused for the obvious reason: a
    read-only tracker has no business rendering files from outside the checkout
    it was pointed at.
    """
    if pattern.startswith(("/", "~")) or "\\" in pattern:
        return False
    return not any(seg in ("", ".", "..") for seg in pattern.split("/"))

#: This module's content hash, frozen at IMPORT. `PAGE` is a module-level
#: string, so a process started before an edit serves the old HTML for as long
#: as it lives — a stale tracker is indistinguishable from a missing feature,
#: which cost a real round-trip ("why can't I see it?"). Comparing this against
#: the file's CURRENT hash at request time is the only way the page can know it
#: is out of date, because everything else it reads comes from the same stale
#: process.
def _source_stamp() -> str:
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    except OSError:
        return "unknown"


_IMPORT_STAMP = _source_stamp()

#: `git ls-remote` per observed checkout: `{str(repo): {"at", "ok", "refs"}}`.
#: Keyed by repo rather than global because a single flat dict silently served
#: one checkout's refs for another — harmless for a dashboard that watches one
#: repo, wrong for anything (tests included) that reads two inside the TTL.
_REMOTE_CACHE: dict = {}
#: `(repo, sha, head) -> "yes" | "no"`. Ancestry of a fixed commit pair can
#: never change, so a hit is permanently valid and the 2s poll costs nothing
#: after the first answer. "unknown" is deliberately NEVER cached: it means git
#: could not answer, and a transient failure must not freeze into a verdict.
#: The repo is part of the key because `make_repo`-style fixtures produce
#: identical shas across different directories.
_ANCESTRY_CACHE: dict = {}
_SHALLOW_CACHE: dict = {}


def _run_status(args, cwd=None, timeout=8):
    """`(returncode, stdout)`. -1 on a launch failure or timeout.

    The exit code is the payload for `git merge-base --is-ancestor`, whose whole
    answer IS its status: 0 yes, 1 no, anything else "git could not tell you".
    """
    # `git status` refreshes the index and rewrites `.git/index` as a side
    # effect, so a "read-only" tracker polling every 2s was in fact writing to
    # the repository it observes. `--no-optional-locks` exists for exactly this
    # case: it tells git not to take the index lock or write refreshed state.
    # It is injected HERE, the single subprocess entry point, so a call added
    # later cannot quietly skip it.
    #
    # It costs nothing in accuracy: git still compares CONTENT for entries whose
    # stat data looks dirty; what it skips is writing the refreshed index back.
    # Never drop it to "speed up" a call — every git read here runs against a
    # repo the loop is actively writing.
    if args and args[0] == "git":
        args = ["git", "--no-optional-locks", *args[1:]]
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (subprocess.SubprocessError, OSError):
        return -1, ""


def _run_checked(args, cwd=None, timeout=8) -> str | None:
    """Run a command, returning `None` when it FAILED and its stdout when it did
    not — so an empty success and a failure are two different answers.

    `_run` below collapses both to `""`, which is fine wherever empty means
    "nothing to show". It is a lie for the progress figures: an unreadable
    worker repo would render as zero lines changed, and zero is precisely the
    alarming state ("the agent has written nothing"), invented out of a
    directory we could not read.
    """
    code, out = _run_status(args, cwd=cwd, timeout=timeout)
    return out if code == 0 else None


def _run(args, cwd=None, timeout=8):
    return _run_checked(args, cwd=cwd, timeout=timeout) or ""


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def live_agents() -> list[dict]:
    """In-flight `claude -p` subagents, with the domain each is auditing.

    `ps` output is the only source that knows this while an agent is still
    running — the run directory stays empty until one completes.
    """
    out = _run(["ps", "-eo", "pid,etime,command"])
    agents = []
    for line in out.splitlines():
        if "claude -p" not in line or "grep" in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, command = parts
        m = _DOMAIN.search(command)
        agents.append(
            {"pid": pid, "elapsed": etime, "domain": m.group(1).strip() if m else "(unidentified)"}
        )
    return agents


#: A finding heading in the audit report: `#### <domain>:<id> — <title>`.
#: The previous pattern expected a markdown TABLE row (`| rt-01 | P1 | … |`),
#: a format the auditor stopped emitting — so this panel had been silently
#: empty for both of the reports on disk, not merely the newest one. An
#: em dash or a plain hyphen may separate id from title.
_FINDING = re.compile(
    r"^#{2,4}\s+([a-z_]+):([a-z]+-\d+)\s*[—-]\s*(.+?)\s*$", re.M
)
#: Severity line beneath a finding, used as the priority stand-in.
_SEVERITY = re.compile(r"severity\s+\*{0,2}(critical|high|medium|low)\*{0,2}", re.I)
#: The retired table form, still honoured so an older report keeps rendering.
_RT_ROW = re.compile(r"^\|\s*\*{0,2}(rt-\d+|hb-\d+)\*{0,2}\s*\|\s*\*{0,2}(P\d)\*{0,2}\s*\|\s*(.+?)\s*\|", re.M)


def app_tasks(repo: Path, limit: int = 40,
              report_glob: str = DEFAULT_AUDIT_REPORT_GLOB) -> list[dict]:
    """The application backlog, read from the newest committed audit report.

    The loop's own registry holds only what has been seeded; the audit report is
    where the proposed work actually lives, so a tracker that showed the
    registry alone would claim there is one task when there are fifteen.

    `report_glob` is `[repo].audit_report_glob` — where THIS target repository
    files those reports — defaulted to this one's `docs/AUDIT_*.md` so a caller
    that does not pass it behaves exactly as before the setting existed. Newest
    is newest BY NAME (`reverse=True` on the sorted paths), which works because
    the reports are date-stamped; that is a property of the naming convention,
    not of the glob, so a repository whose reports are not date-sortable gets
    the last one alphabetically rather than a wrong-but-confident "newest".
    """
    report_glob = (report_glob or "").strip()
    if not report_glob or not _is_safe_glob(report_glob):
        return []
    reports = sorted(repo.glob(report_glob), reverse=True)
    if not reports:
        return []
    try:
        text = reports[0].read_text()
    except OSError:
        return []
    def _clean(value: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"[*`]", "", value)).strip()

    out, seen = [], set()

    # Current format: one heading per finding, severity on the line below.
    for match in _FINDING.finditer(text):
        domain, short, title = match.groups()
        tid = f"{domain}:{short}"
        if tid in seen:
            continue
        seen.add(tid)
        tail = text[match.end(): match.end() + 400]
        sev = _SEVERITY.search(tail)
        out.append({
            "id": tid,
            "priority": (sev.group(1).lower() if sev else ""),
            "title": _clean(title)[:180],
            "source": reports[0].name,
        })
        if len(out) >= limit:
            return out

    # Retired table form, so an older report still renders.
    for tid, prio, title in _RT_ROW.findall(text):
        if tid in seen:
            continue
        seen.add(tid)
        out.append({"id": tid, "priority": prio, "title": _clean(title)[:180],
                    "source": reports[0].name})
        if len(out) >= limit:
            break
    return out



# ---- live progress for the task executing right now --------------------------
#
# An operator watching a 25-minute round can see THAT an agent is running and
# nothing about whether it is getting anywhere. On 2026-08-06 merge-01 ran twice
# for 1800s and was killed by the timeout both times, having written 591
# insertions across 16 files and then 532 across 15 — that it was working rather
# than wedged was only discoverable afterwards, by hand, in the quarantined
# worker repo. These figures make that visible while it happens.
#
# Read from GIT IN THE WORKER, never from anything the agent reports about
# itself. `TaskExecution.report_summary`/`report_details` are the executor's own
# claims (that file says so in as many words); they are not consulted here and
# must never become a fallback — a fallback would turn "we could not read the
# worker" into a confident number authored by the process being observed.

#: Caps on the untracked-file scan. It runs on every 2s poll, so this is a
#: page-responsiveness property, not a nicety. Tripping either cap sets
#: `partial`, which means the LINE counts are a lower bound; the file count
#: stays exact either way, because `git ls-files` reports it without reading a
#: byte of content.
_UNTRACKED_FILE_CAP = 400
_UNTRACKED_BYTE_BUDGET = 8_000_000
#: Shorter than `_run`'s 8s: these calls sit on the 2s poll, and a page that
#: blocks on git is worse than a page that says "unknown" for one tick.
_PROGRESS_TIMEOUT = 2.0
#: Wall clocks get adjusted. A dispatch stamp a couple of minutes in the future
#: is skew, and 0 is the honest reading of it; anything further ahead is not a
#: measurable elapsed time at all, so it reads unknown rather than a made-up 0.
_CLOCK_SKEW_GRACE = 120.0


def _elapsed_seconds(started_at: str, now: float) -> float | None:
    """Seconds since `started_at`, or `None` when that cannot be established."""
    try:
        stamp = datetime.fromisoformat(started_at)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:  # `utcnow_iso()` is tz-aware; older stamps may not be
        stamp = stamp.replace(tzinfo=timezone.utc)
    delta = now - stamp.timestamp()
    if delta < 0:
        return 0.0 if delta > -_CLOCK_SKEW_GRACE else None
    return delta


def _numstat(out: str) -> tuple[int, int, int]:
    """`(insertions, deletions, files)` from `git diff --numstat` output.

    A binary file's row is `-`, `-`, path: it counts as a touched FILE with no
    line counts, which is what git itself reports rather than a zero we chose.
    """
    insertions = deletions = files = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed = parts[0], parts[1]
        files += 1
        if added.isdigit():
            insertions += int(added)
        if removed.isdigit():
            deletions += int(removed)
    return insertions, deletions, files


def _untracked_lines(worker: Path, paths: list[str]) -> tuple[int, int, bool]:
    """`(insertions, files, partial)` for untracked files.

    A brand-new file is where most of an agent's output lands, and git's own
    diff says nothing about it until something adds it — so counting it is the
    difference between "0 lines written" and the truth. Every line of a new file
    is an insertion, exactly as `git diff` would report it once added.
    """
    insertions = 0
    partial = len(paths) > _UNTRACKED_FILE_CAP
    budget = _UNTRACKED_BYTE_BUDGET
    for rel in paths[:_UNTRACKED_FILE_CAP]:
        if budget <= 0:
            partial = True
            break
        path = worker / rel
        if path.is_symlink():
            # git records a symlink's TARGET as its whole content — one line —
            # and following it could read something enormous or a device. Never
            # open it; take git's own count.
            insertions += 1
            continue
        try:
            if not path.is_file():
                continue  # a fifo/socket would block on open; count it, read none
            with path.open("rb") as handle:
                data = handle.read(budget + 1)
        except OSError:
            # One unreadable file is not an unreadable worker: it is one file we
            # could not count, so the total becomes a lower bound rather than
            # discarding everything else that was read successfully.
            partial = True
            continue
        if len(data) > budget:
            data, partial = data[:budget], True
        budget -= len(data)
        if b"\x00" in data[:8000]:
            continue  # binary: a touched file, no line count — git says the same
        insertions += data.count(b"\n") + (0 if (not data or data.endswith(b"\n")) else 1)
    return insertions, len(paths), partial


def worker_progress(state: dict, now: float | None = None) -> dict | None:
    """What the task executing RIGHT NOW has written, and for how long.

    `None` means nothing is executing, and the page then shows nothing at all.
    That is deliberate: `state.task_execution` is cleared the moment a candidate
    is published (`orchestrator._dispatch_task_push`), so its absence is the
    loop's own statement that no unit of work is in flight — and leftover
    figures beside an idle loop read as live activity, which is the one thing
    this panel exists to make legible.

    A dict with `insertions`/`deletions`/`files` set to `None` means UNKNOWN —
    the worker repo could not be read. Never zero: zero means "nothing written
    yet", which is exactly the alarming state, and inventing it from a directory
    we could not open would be a lie in the dangerous direction.

    Read-only and lock-free by construction: two `git` reads through
    `_run_checked` (which injects `--no-optional-locks`) plus plain file reads.
    No lock is taken, so a scheduler hitting this mid-round is safe.
    """
    execution = state.get("task_execution") or {}
    task_id = execution.get("task_id")
    if not task_id:
        return None

    now = time.time() if now is None else now
    current = state.get("current_task") or {}
    # Only THIS task's dispatch stamp will do. `current_task` outlives its round
    # and can name a different task than the one in flight; borrowing its
    # timestamp then would date the round from someone else's dispatch.
    started = str(current.get("started_at") or "") if current.get("task_id") == task_id else ""
    worker_path = str(execution.get("worktree_path") or "")
    progress = {
        "task_id": task_id,
        "worker": worker_path,
        "dispatched_at": started,
        "elapsed_seconds": _elapsed_seconds(started, now) if started else None,
        "insertions": None,
        "deletions": None,
        "files": None,
        "partial": False,
        "base": "",
        "note": "",
    }

    if not worker_path or not Path(worker_path).is_dir():
        progress["note"] = (
            f"no readable worker repo at {worker_path or '(none recorded)'}"
        )
        return progress

    worker = Path(worker_path)
    # Against the task's base sha, so the figure spans every round committed so
    # far plus the uncommitted work in the tree — the whole of what this task has
    # written. A bare `git diff` would be worktree-vs-index and would miss
    # anything already staged. `HEAD` is the fallback when the base sha does not
    # resolve here, and the payload says which one was used so the number is
    # self-describing.
    base = str(execution.get("task_base_sha") or "").strip()
    label = "task_base_sha"
    numstat = (
        _run_checked(["git", "diff", "--numstat", base, "--"],
                     cwd=worker, timeout=_PROGRESS_TIMEOUT)
        if base else None
    )
    if numstat is None:
        label = "HEAD"
        numstat = _run_checked(["git", "diff", "--numstat", "HEAD", "--"],
                               cwd=worker, timeout=_PROGRESS_TIMEOUT)
    # `-z`: NUL-delimited, which also turns OFF git's path quoting. Without it a
    # name with an umlaut or a space comes back as `"caf\303\251.py"` and every
    # such file fails to open — an undercount, silently, on a German-language
    # repo where those names are ordinary.
    others = _run_checked(["git", "ls-files", "-z", "--others", "--exclude-standard"],
                          cwd=worker, timeout=_PROGRESS_TIMEOUT)
    # EITHER read failing makes the whole figure unknown. Reporting the tracked
    # diff alone when the untracked listing failed would understate the work in
    # exactly the direction that matters — a productive agent writing new files
    # would look idle.
    if numstat is None or others is None:
        progress["note"] = f"git could not be read in {worker_path}"
        return progress

    insertions, deletions, files = _numstat(numstat)
    new_lines, new_files, partial = _untracked_lines(
        worker, [name for name in others.split("\0") if name]
    )
    progress.update(
        insertions=insertions + new_lines,
        deletions=deletions,
        files=files + new_files,
        partial=partial,
        base=label,
    )
    return progress


def _config_toml(repo: Path) -> dict:
    """The loop's own `config.toml` as a plain dict, `{}` when it cannot be
    read.

    Read RAW rather than through `config.load_config`, which is strict and
    raises on a checkout with no `browser.conversation_url` — this page must
    render against any checkout, including one that has never been configured.
    That is the pre-existing bargain `_inbox_dir` struck; it is factored out
    here so the settings this module reads all fail the same way (silently,
    onto their documented default) instead of each inventing its own.
    """
    try:
        import tomllib

        data = tomllib.loads((repo / ".autoloop" / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, KeyError):
        return {}
    return data if isinstance(data, dict) else {}


def _config_section(repo: Path, name: str) -> dict:
    """One `[section]` of the config as a dict, `{}` when it is missing or is
    not a table.

    The `isinstance` is not decoration. `data.get(name) or {}` raises
    `AttributeError` on the next `.get` if the key holds a scalar
    (`repo = "docs"`), which would 500 the page — contradicting the whole
    reason `_config_toml` swallows its errors. Reached by both settings this
    module reads, so neither can regrow its own version of that bug.
    """
    section = _config_toml(repo).get(name)
    return section if isinstance(section, dict) else {}


def _audit_report_glob(repo: Path) -> str:
    """`[repo].audit_report_glob`, or this repository's own default.

    A non-string value falls back rather than raising: the whole point of
    reading the TOML directly is that a malformed config must not take the
    tracker down with it.
    """
    value = _config_section(repo, "repo").get("audit_report_glob")
    return value if isinstance(value, str) else DEFAULT_AUDIT_REPORT_GLOB


def _inbox_dir(repo: Path) -> Path:
    """Where task requests go. Resolved through the loop's own config so the
    dashboard and `add-task` always agree, with the documented default as the
    fallback for a dashboard pointed at a checkout that has no config yet."""
    workers_root = _config_section(repo, "paths").get("workers_root")
    if workers_root and isinstance(workers_root, str):
        return Path(workers_root).expanduser().parent / "inbox"
    return Path.home() / ".autoloop" / "inbox"


def _state_dir(repo: Path) -> Path:
    """`[paths].state_dir` for this checkout, defaulted to `.autoloop`.

    A relative value is resolved against the checkout, which is where the loop
    itself resolves it from (`config.load_config` reads it as a plain `Path` and
    every real run has the repo root as its cwd).
    """
    configured = _config_section(repo, "paths").get("state_dir")
    if configured and isinstance(configured, str):
        path = Path(configured).expanduser()
        return path if path.is_absolute() else repo / path
    return repo / ".autoloop"


def _tasks_file(repo: Path) -> Path:
    """The registry file this page reads AND, for a priority edit, writes.

    Resolved through the config rather than hard-coded, because the read and
    the write must be the same file: the row shows a value read back from disk,
    so a page reading one path and writing another would report a save that did
    not happen where it was looking.
    """
    return _state_dir(repo) / "tasks.json"


def _task_store(repo: Path):
    """A `TaskStore` for this checkout, wired to the mutation ledger.

    Built per request rather than held: the store is stateless (a path, a
    ledger path, and a mutex resolved from the path), and a long-lived one
    would keep a stale handle if the operator moved the state dir under it.
    """
    from .tasks import TaskStore, mutation_ledger_for

    workers_root = _config_section(repo, "paths").get("workers_root")
    root = (
        Path(workers_root).expanduser()
        if workers_root and isinstance(workers_root, str)
        else Path.home() / ".autoloop" / "workers"
    )
    return TaskStore(
        _tasks_file(repo), ledger=mutation_ledger_for(root, _state_dir(repo))
    )


def _pending_inbox(repo: Path) -> list[dict]:
    """Queued requests, so a submit is visibly recorded rather than vanishing
    until the loop next runs.

    `approved_paths` is carried through deliberately. A creation request is the
    only thing this page can queue that touches authorization, and the loop
    applies it without a second operator confirmation — so the paths must be
    readable as text between the submit and the merge, or the only record of
    what scope is pending is a JSON file nobody is looking at.
    """
    out = []
    for path in sorted(_inbox_dir(repo).glob("*.json")):
        spec = _json(path)
        if not isinstance(spec, dict):
            continue
        out.append({
            "kind": spec.get("kind", "task"),
            "id": spec.get("id"),
            "priority": spec.get("priority"),
            "title": spec.get("title") or "",
            "approved_paths": list(spec.get("approved_paths") or ()),
        })
    return out


#: How a task's side branch is named when nothing on disk says otherwise
#: (`worker_env.py:330`, `worktree.branch_for`). Used ONLY to look a branch up;
#: it never decides whether that branch is merged.
BRANCH_PREFIX = "autoloop/"

#: The four states a completed task can be in. `unpublished` is not a normal
#: state — it means the registry says done and no side branch exists anywhere —
#: so it is shown rather than folded into one of the others.
#:
#: This is the VOCABULARY, not a display order: the page lists these in
#: `MERGE_GROUPS`'s triage order, which puts the one nobody has to act on last.
#: `test_the_merge_panel_groups_every_state_exactly_once` pins the two against
#: each other, so a fifth state cannot render nowhere.
MERGE_STATES = ("merged", "unmerged", "unpublished", "unknown")


def _is_shallow(repo: Path) -> bool:
    key = str(repo)
    if key not in _SHALLOW_CACHE:
        out = _run(["git", "rev-parse", "--is-shallow-repository"], cwd=repo).strip()
        _SHALLOW_CACHE[key] = out == "true"
    return _SHALLOW_CACHE[key]


def is_ancestor(repo: Path, sha: str, head: str) -> str:
    """Is `sha` an ancestor of `head`? `"yes"` / `"no"` / `"unknown"`.

    Git's own answer, not a guess: `merge-base --is-ancestor` exits 0 for yes
    and 1 for no. That is the ONLY thing allowed to decide merged-ness here —
    not the task status, not the execution record, not a branch-name match,
    which are exactly what let seven finished-but-unmerged tasks read as done
    on 2026-08-06.

    Any other exit is triaged rather than guessed. The common one is 128, "not
    a valid object name", and it is the PRODUCTION case rather than an edge:
    workers commit in isolated repos and the publisher pushes from its own, so
    the observed checkout usually has never seen a side branch's objects. The
    inference that makes this feature work at all: **every ancestor of local
    HEAD is in the local object database, so an object that is absent cannot be
    one** — that is a definite "no", not an unknown. It is only unsound in a
    shallow clone, where history is deliberately truncated, so that case is
    probed and reported unknown instead. A repository git cannot read at all
    stays unknown, because inventing "not merged" from a broken read would put
    an alarming state on the page that nothing observed.
    """
    if not sha or not head:
        return "unknown"
    key = (str(repo), sha, head)
    cached = _ANCESTRY_CACHE.get(key)
    if cached:
        return cached
    code, _ = _run_status(["git", "merge-base", "--is-ancestor", sha, head], cwd=repo)
    if code in (0, 1):
        verdict = "yes" if code == 0 else "no"
    elif _run_status(["git", "cat-file", "-e", sha + "^{commit}"], cwd=repo)[0] == 0:
        # The object IS here and git still would not answer — something is
        # wrong with the repository, so say so.
        verdict = "unknown"
    elif _run(["git", "rev-parse", "--verify", "--quiet", head + "^{commit}"], cwd=repo).strip():
        # The object is absent and HEAD reads fine: absent cannot be reachable.
        verdict = "unknown" if _is_shallow(repo) else "no"
    else:
        verdict = "unknown"
    if verdict != "unknown":
        _ANCESTRY_CACHE[key] = verdict
        if len(_ANCESTRY_CACHE) > 4096:  # a long-lived process, not a leak
            _ANCESTRY_CACHE.clear()
    return verdict


def branch_for(task_id: str, record: dict) -> str:
    """The side branch a task's work belongs on, from the loop's own record.

    Evidence order, best first: the ref the publisher INTENDED to write, then
    the branch the worker checked out, and only then `BRANCH_PREFIX + id` —
    which is a lookup key, never evidence. Shared by the merge panel and the
    in-progress breakdown so the two cannot name different branches for the
    same task and then disagree about whether it was published.
    """
    ref = str(record.get("intended_remote_ref") or "")
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/"):]
    return str(record.get("task_branch") or "") or f"{BRANCH_PREFIX}{task_id}"


def merge_states(completed: list[dict], executions: dict, remote_ok: bool,
                 refs: dict, ancestor) -> list[dict]:
    """One row per completed task: which of `MERGE_STATES` it is in, and why.

    Pure resolution logic — `ancestor(sha) -> "yes"/"no"/"unknown"` is injected,
    so the four states are decidable without a repository, while the git-facing
    half stays in `is_ancestor` above.

    Order of evidence, and why:

    1. `refs` (what `git ls-remote` reported) gives the branch's published sha.
       Ancestry against HEAD then answers merged vs not.
    2. With no ref for the branch, the execution record's `candidate_sha` is
       used as POSITIVE evidence only: if that commit is in HEAD, the work is
       integrated whatever became of the branch (merged then deleted on origin
       is ordinary). It can never produce "not merged" — the record is not
       allowed to make a negative claim, which is the failure this replaces.
    3. Otherwise: `unpublished` when the remote was readable and simply had no
       such branch, `unknown` when it was not readable, because an unreachable
       remote cannot distinguish "no branch" from "seven branches".
    """
    out = []
    for task in completed:
        task_id = task.get("id") or ""
        record = executions.get(task_id) or {}
        branch = branch_for(task_id, record)
        published = refs.get(branch) or ""
        candidate = str(record.get("candidate_sha") or "")
        state, detail = "unknown", "origin unreachable — merged-ness unverified"
        if published:
            verdict = ancestor(published)
            if verdict == "yes":
                state, detail = "merged", f"{published[:12]} is an ancestor of HEAD"
            elif verdict == "no":
                state, detail = "unmerged", f"on origin at {published[:12]}, not in HEAD"
            else:
                state, detail = "unknown", "git could not resolve the branch against HEAD"
        elif candidate and ancestor(candidate) == "yes":
            state = "merged"
            detail = f"no branch on origin; commit {candidate[:12]} is in HEAD"
        elif remote_ok:
            state = "unpublished"
            detail = f"completed, but origin has no {branch} — the work was never pushed"
        out.append({
            "id": task_id, "title": task.get("title") or "", "branch": branch,
            "sha": (published or candidate)[:12], "state": state, "detail": detail,
        })
    return out


def _executions(repo: Path) -> dict:
    """`{task_id: record}` for every LIVE execution record.

    `glob("*.json")`, deliberately NOT `rglob`: `worktask.retire_execution`
    files a retired record one level down in `executions/archive/`, and that
    non-recursion is what actually takes it out of everything reading this
    directory (`worktask.py:357` says so in as many words). Recursing here
    would resurrect retired candidates in both callers at once.

    A corrupt record is skipped, never raised — a tracker that 500s because one
    record is half-written tells the operator nothing.
    """
    out: dict = {}
    directory = repo / ".autoloop" / "executions"
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        record = _json(path)
        if isinstance(record, dict):
            out[str(record.get("task_id") or path.stem)] = record
    return out


#: The merge panel's groups, in the order the page lists them: `(state, label,
#: collapsed)`. Triage order — the three states that need a human first, and
#: the one nobody has to act on last.
#:
#: This is the roadmap panel's `GROUPS` / `COLLAPSED` pattern applied to the
#: same problem one panel up, deliberately rather than a second mechanism: on
#: 2026-08-18 this panel rendered 61 rows flat, of which FOUR needed a human and
#: 57 did not, so the four that mattered were buried under the ones that did
#: not. The counts were already computed and then thrown away as a display axis.
#:
#: COLLAPSED IS NOT HIDDEN, and there is deliberately no `hidden` flag here.
#: The roadmap has one because it has a hide policy; this panel's rule is that
#: it has none — every group renders with its count, `unknown (0)` included. An
#: `unknown` row means the sweep could not judge a branch, which is the state
#: most worth seeing and the easiest to hide by accident, and an always-false
#: field would be an invitation to flip it.
#:
#: The LABEL travels in the payload for the same reason the roadmap's does: the
#: backend pins the word for a state, so the template cannot invent one. The
#: ICON stays in the template's `MS` map, which already carries icon + word for
#: every state and is what the row cells render.
MERGE_GROUPS: tuple[tuple[str, str, bool], ...] = (
    ("unmerged", "NOT merged", False),
    ("unpublished", "Not published", False),
    ("unknown", "Unknown", False),
    # The only collapsed group, and the reason this panel is grouped at all:
    # merged only ever grows and nobody has to act on it. Collapsed is not
    # hidden — its count is in the summary, and a merged row's whole value is
    # that it stays findable when someone asks where a task's work went.
    ("merged", "Merged", True),
)


def merge_groups(rows: list[dict]) -> list[dict]:
    """`rows` split by state into `MERGE_GROUPS`, in order, each with its count.

    Pure, display-only, and additive: the flat `rows` list and the `counts`
    dict are unchanged and still travel beside this, so nothing that reads them
    has to learn about groups. A row object here IS the row object there — the
    same fields, never a copy with something dropped.

    EVERY group is emitted, including one with no rows. A group that vanished
    when it emptied would make "the sweep judged every branch" and "the panel
    failed to render that section" look identical, which is the failure the
    roadmap's `ALWAYS_SHOWN` already records one panel down.

    A state no group claims lands in `unknown` rather than disappearing —
    grouping is a second axis on the same rows, so a completed task that no
    group claimed would be silently absent from the panel. `merge_states` only
    ever produces the four, and the page's own `MS[r.state] || MS.unknown`
    falls back the same way.
    """
    grouped: dict[str, list[dict]] = {state: [] for state, _label, _collapsed in MERGE_GROUPS}
    for row in rows:
        state = str(row.get("state") or "")
        grouped[state if state in grouped else "unknown"].append(row)
    return [
        {"key": state, "label": label, "collapsed": collapsed,
         "count": len(grouped[state]), "rows": grouped[state]}
        for state, label, collapsed in MERGE_GROUPS
    ]


def merge_report(repo: Path, roadmap: list[dict], head: str,
                 remote_ok: bool, refs: list[dict], branch: str,
                 executions: dict) -> dict:
    """The payload the page renders: rows, a count per state, and the same rows
    grouped by state for display.

    Read-only and lock-free, like everything else here: `executions` is read by
    `_executions` as plain JSON and every git call is a local read.
    """
    completed = [t for t in roadmap if (t.get("status") or "") == "completed"]
    rows = merge_states(
        completed, executions, remote_ok,
        {r["ref"]: r.get("full") or r.get("sha") or "" for r in refs},
        lambda sha: is_ancestor(repo, sha, head),
    )
    return {
        "base_branch": branch or "HEAD",
        "base_head": head[:12],
        "remote_ok": remote_ok,
        "counts": {s: sum(1 for r in rows if r["state"] == s) for s in MERGE_STATES},
        "rows": rows,
        # The SAME rows on a second axis, so the panel can put the four that
        # need a human above the fold and the 57 that do not behind a counted
        # disclosure. The hide/show policy lives here rather than in the
        # template — exactly as the roadmap's does — so "merged is collapsed and
        # the other three are not" is a fact a test can assert about the payload
        # instead of a substring of a script.
        "groups": merge_groups(rows),
    }


# ---- the roadmap, grouped by state -------------------------------------------
#
# One flat list of 20+ tasks answers none of the three questions an operator
# actually has — what is moving, what needs me, what is next — so the list is
# split by state, each group counted under its own heading.
#
# `TaskRegistry.state_of()` is the ONLY source of that state. Re-deriving it
# from the stored `status` string would disagree with the code that dispatches,
# in both directions at once: READY vs BLOCKED is derived from dependencies and
# is never stored at all, and a stored `status="blocked"` means QUARANTINED
# (`TaskState.BLOCKED_BY_OPERATOR`), which is the opposite kind of problem from
# "waiting on a dependency" — one needs an operator answer, the other resolves
# itself. Sending someone to `autoloop answer` for a task that is merely queued
# behind another is exactly the confusion this panel exists to remove.
#
# RETIRED is the third of those and the reason this panel was still misread
# after the split: it is not a problem at all. The work shipped (or stopped)
# under another id, so the row exists to be findable, not to be fixed. Grouping
# it with either kind of blocked puts a "when will this be done?" next to an
# answer of "it already was, elsewhere".

#: The groups, in the order the page lists them, each pinned to the `TaskState`
#: it renders. The order is the order an operator triages in: act on it, decide
#: it, expect it, understand why it is stuck, and only then history.
GROUPS: tuple[tuple[str, str, TaskState], ...] = (
    ("in_progress", "In progress", TaskState.IN_PROGRESS),
    ("needs_human", "Needs a human", TaskState.BLOCKED_BY_OPERATOR),
    ("ready", "Ready", TaskState.READY),
    ("blocked", "Blocked", TaskState.BLOCKED),
    # Retired sits between "why is this stuck" and history, because that is
    # where the operator's question actually lands: they read seven blocked
    # rows, asked when they would be fixed, and for six of them the answer was
    # "never — they already were, under a different id". A row here is not a
    # call to action, which is exactly why it may not sit in either Blocked
    # group.
    ("retired", "Retired", TaskState.RETIRED),
    ("done", "Done", TaskState.COMPLETED),
)

#: The group that renders even when it is empty, and says `none` in as many
#: words. Silence and "nothing needs you" must not look identical: a hidden
#: group is indistinguishable from a section that failed to render, and this is
#: the one group whose emptiness IS the answer the operator came for.
ALWAYS_SHOWN = frozenset({"needs_human"})

#: Collapsed behind a `<details>`, with its count in the summary. `done` and
#: `retired`, the two groups that only ever grow and that nobody has to act on
#: — an operator scrolling past 40 finished tasks to reach the four that matter
#: is the flat list's failure repeated inside the fix. Collapsed is not hidden:
#: each renders its count, and a retirement's whole value is that it stays
#: findable, so the chain that says brw-07/brw-08 continue brw-02/brw-04
#: survives.
#:
#: Each collapsed group gets its OWN disclosure in the static markup, looked up
#: by key. `groups.find(g => g.collapsed)` would silently hand the first one to
#: whichever `<details>` asked.
COLLAPSED = frozenset({"done", "retired"})


def _ready_order(registry: TaskRegistry) -> list:
    """The READY tasks in the order `next_ready()` would pick them.

    The same key that method uses — `(priority, id)`, ascending — and
    deliberately NOT a simulation of it: replaying successive picks means
    COMPLETING tasks, and this page may not call a mutating registry method (it
    holds no lock and must never write). The duplicated key is pinned instead
    by `test_the_ready_group_is_in_next_ready_order`, which runs the real
    `next_ready()` / `mark_completed()` loop on a registry of its own and
    demands the same sequence — so changing that key fails there rather than
    quietly showing an order the loop does not follow.
    """
    return sorted(registry.ready_tasks(), key=lambda t: (t.priority, t.id))


def _in_progress_detail(record: dict) -> str:
    """What an operator acts on: the commit under review, and which round.

    Both come from the loop's own bookkeeping (`worktask.TaskExecution`), never
    from anything an agent reported about itself. A record carrying neither
    says so rather than rendering blank — a task dispatched two minutes ago has
    genuinely not committed anything yet, and empty space reads as a missing
    feature.
    """
    sha = str(record.get("candidate_sha") or "")[:12]
    review_round = record.get("review_round")
    bits = []
    if sha:
        bits.append(f"candidate {sha}")
    if review_round is not None and str(review_round).strip():
        bits.append(f"review round {review_round}")
    return " · ".join(bits) or "no candidate committed yet"


def _waiting_on(registry: TaskRegistry, task) -> list[str]:
    """The dependencies that are NOT yet completed, in declared order.

    Incompleteness is asked of `state_of` rather than of the dependency's
    status string, for the same reason the groups are: a dependency that is
    itself quarantined is not completed either, and only one of those two
    readings survives a task being blocked behind a blocked task.

    One function, two consumers — the prose `detail` below and the row's
    "waits on" chip — so the sentence an operator reads and the chip beside it
    can never name different dependencies.
    """
    return [
        dep for dep in task.depends_on
        if registry.state_of(dep) is not TaskState.COMPLETED
    ]


def _blocked_detail(registry: TaskRegistry, task) -> str:
    """Which dependency is holding this task up. "Blocked" without saying by
    what is not actionable — it is the difference between a fact and a chore.
    """
    waiting = _waiting_on(registry, task)
    return ("waiting on " + ", ".join(waiting)) if waiting else "waiting on a dependency"


def _retired_detail(task) -> str:
    """What replaced this task — the one thing an operator needs from a row
    they cannot act on.

    `superseded_by` first, because it is the machine-readable record and it
    answers "where did this work go" in four characters. `blocked_reason` is
    the fallback rather than the answer: it is free text, it is why the six
    retirements were unreadable in the first place, and for `dash-01` — stale
    at dispatch, superseded by nothing — it is the only account there is. A
    retirement with neither says so instead of rendering blank, which reads as
    a broken cell.
    """
    if task.superseded_by:
        return "superseded by " + ", ".join(task.superseded_by)
    return task.blocked_reason or "retired; no successor recorded"


def _group_detail(key: str, task, index: int, registry: TaskRegistry,
                  executions: dict) -> str:
    if key == "in_progress":
        return _in_progress_detail(executions.get(task.id) or {})
    if key == "needs_human":
        # `block()` always records a reason, so an empty one means a
        # hand-edited or pre-`blocked_reason` file — say which rather than
        # rendering a quarantined task with nothing beside it.
        return task.blocked_reason or "no reason recorded"
    if key == "blocked":
        return _blocked_detail(registry, task)
    if key == "retired":
        return _retired_detail(task)
    if key == "ready" and index == 0:
        return "next to be dispatched"
    return ""


def _grouped(registry: TaskRegistry, executions: dict) -> list[dict]:
    states = {task.id: registry.state_of(task.id) for task in registry.all_tasks()}
    ready = _ready_order(registry)
    # THE EXECUTION ORDINAL, built once from the ready set and looked up per row
    # rather than recomputed per group. That is what makes "the position
    # `next_ready()` will select from" structural instead of a coincidence: it
    # is the same list, in the same `(priority, id)` order, and every task that
    # is not in it gets `None` — in progress, quarantined, dependency-blocked,
    # retired and done alike, none of which `next_ready()` can pick whatever
    # their priority. A number beside a row that cannot run would be a claim
    # about dispatch order that the loop does not honour.
    ordinals = {task.id: index for index, task in enumerate(ready, 1)}
    groups = []
    for key, label, state in GROUPS:
        members = ready if key == "ready" else sorted(
            (t for t in registry.all_tasks() if states[t.id] is state),
            key=lambda t: (t.priority, t.id),
        )
        tasks = [
            {"id": task.id, "title": task.title, "priority": task.priority,
             # Carried on every row, not just a retired one: the supersession
             # chain is the reason this field exists, and a consumer of
             # `/data.json` reading it should not have to know which group a
             # task landed in first. Empty for everything else.
             "superseded_by": list(task.superseded_by),
             # THE WHOLE DESCRIPTION, never a slice. This is the field the
             # roadmap is steered from and the page sent none of it, so an
             # operator asking what a queued task actually says had to open
             # `.autoloop/tasks.json` by hand. A truncation here would be
             # indistinguishable on the page from a task that really is that
             # short, which is the failure being fixed rather than a smaller
             # version of it. `chars` travels with it so the row can show the
             # size without the page measuring a string the backend already
             # has.
             #
             # It does widen `/api/state`: descriptions are the largest thing
             # in `tasks.json` and this payload is polled every 2s, so both the
             # response and the page's `JSON.stringify(rest)` re-render
             # signature grow with the roadmap. Bounded by the roadmap's own
             # size, on a localhost socket, and the signature comparison is a
             # string compare — but it is a real cost, recorded here rather
             # than left for a reader to discover.
             "description": str(task.description or ""),
             "chars": len(str(task.description or "")),
             "ordinal": ordinals.get(task.id),
             # ONLY for the group that is actually waiting on a dependency.
             # `state_of` says in as many words that a retired task usually
             # still declares the dependencies it was planned with, so filling
             # this in unconditionally would hang a "waits on brw-01" chip on a
             # retirement — the exact misread `TaskState.RETIRED` was added to
             # end. Empty everywhere else, and the page renders no chip for it.
             "waits_on": _waiting_on(registry, task) if key == "blocked" else [],
             "detail": _group_detail(key, task, index, registry, executions)}
            for index, task in enumerate(members)
        ]
        groups.append({
            "key": key, "label": label, "state": state.value, "count": len(tasks),
            # The hide/show policy lives HERE rather than in the template, so
            # "an empty `Needs a human` is still rendered" is a fact a test can
            # assert about the payload instead of a substring of a script. It
            # governs the INLINE groups only: a collapsed group is a disclosure
            # that always renders its count, and `Done (0)` costs one line.
            "hidden": not tasks and key not in ALWAYS_SHOWN and key not in COLLAPSED,
            "collapsed": key in COLLAPSED,
            "tasks": tasks,
        })
    return groups


def task_groups(tasks_data: dict, executions: dict) -> list[dict]:
    """The roadmap split into `GROUPS`, in order, each with its count.

    Pure and read-only: it builds a `TaskRegistry` from the same JSON the loop
    persists and asks it, which is a constructor plus lookups. No mutating
    registry method is reachable from here, and nothing is written.

    An EMPTY list means the task graph could not be read at all — a roadmap
    with no tasks in it still returns every group in `GROUPS`, zeroed, so the two
    cannot be confused. Anything unparseable lands here: a record shaped for a
    different schema, a dependency naming a task that does not exist (which
    `from_dict` tolerates and `state_of` then raises on). The page says so in
    those words, because "no tasks" and "unreadable" call for opposite
    reactions, and the flat `roadmap` list still renders from the raw JSON
    beside it.
    """
    try:
        registry = TaskRegistry.from_dict(
            {"tasks": list((tasks_data or {}).get("tasks") or ())}
        )
        return _grouped(registry, executions or {})
    except (StateError, TaskGraphError, AttributeError, KeyError, TypeError, ValueError):
        return []


# ---- the dependency graph -----------------------------------------------------
#
# Measured 2026-08-19: 132 tasks, 150 `depends_on` edges, 41 tasks carrying at
# least one dependency, and the most depended-upon ids are ingest-02 (7
# dependents), port-01 (6), inbox-03 (5), stream-01 (5), prov-01 (5). None of
# that was on the page: the roadmap is a flat list grouped by state, so "port-01
# is blocked and six tasks are waiting behind it" was a fact an operator could
# only get by reading `tasks.json` by hand. What is held up, and by what, is the
# one question this panel answers.
#
# BUILT FROM THE RAW ROWS, never from a `TaskRegistry`, and that is the
# load-bearing choice rather than a convenience. `TaskRegistry.from_dict` runs
# `_check_acyclic` (tasks.py), so a cyclic `tasks.json` raises `TaskGraphError`
# and `task_groups` returns `[]` — at which point every other panel on the page
# degrades to "the task graph could not be read". A graph built from a registry
# would therefore show NOTHING in exactly the case where a cycle is the thing
# worth showing. Reading the raw rows means a cycle, a dangling `depends_on` and
# a hand-edited self-edge all render, and each is reported as what it is.
#
# (What `from_dict` actually tolerates and `state_of` then rejects is a DANGLING
# dependency: `_check_acyclic` looks each dep up with `.get`, so an unknown id
# is not a cycle, and `state_of` later does `self._tasks[dep]` and raises
# `KeyError`. Cycles are refused at load. Both cases land here as a drawn node.)
#
# Display only. Nothing in this section writes anything, and `depends_on`,
# dependency resolution and `next_ready()` are untouched — the panel reports the
# graph the loop already has.

#: Every state a drawn node can carry: the `TaskState` vocabulary the Roadmap
#: panel already groups by, plus `unknown` for an id that is depended ON but is
#: not a task at all. Derived from `GROUPS` rather than spelled again, so a
#: seventh state cannot reach the page with no mark against it — the page's
#: `DMARK` table is pinned to this tuple by a test.
DEP_NODE_STATES: tuple[str, ...] = tuple(
    state.value for _key, _label, state in GROUPS
) + ("unknown",)


def _dep_rows(tasks_data: dict) -> list[dict]:
    """`{id, title, status, depends_on}` per raw row, in file order.

    Tolerant on purpose — this is the read that has to survive a file no
    registry will load. A row that is not a mapping, or carries no id, is
    skipped; the FIRST row wins a duplicate id (`from_dict` raises on one, and
    raising is what this read exists to avoid).
    """
    rows: list[dict] = []
    seen: set[str] = set()
    for raw in (tasks_data or {}).get("tasks") or ():
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("id") or "")
        if not task_id or task_id in seen:
            continue
        seen.add(task_id)
        declared = raw.get("depends_on") or ()
        # A STRING is not a list of ids. `_validate_depends_on` refuses one on
        # the way in, but `from_dict` bypasses that gate, and `tuple("ab")`
        # would silently become two one-character dependencies. A row shaped
        # wrongly declares none here rather than declaring nonsense.
        deps = (
            tuple(dep for dep in declared if isinstance(dep, str) and dep)
            if isinstance(declared, (list, tuple)) else ()
        )
        rows.append({
            "id": task_id,
            "title": str(raw.get("title") or ""),
            "status": str(raw.get("status") or "pending"),
            "depends_on": deps,
        })
    return rows


def _dep_states(rows: list[dict], groups: list[dict] | None) -> tuple[dict[str, str], str]:
    """`({task_id: state}, source)` — the registry's answer where there is one.

    `groups` is the payload `task_groups` already built from
    `TaskRegistry.state_of()`, so the graph's node states are the SAME states
    the Roadmap panel groups by and cannot disagree with them.

    The fallback is reached only when that registry did not load at all (a
    cycle, a shape `Task(**raw)` refuses), which is the case this panel exists
    to render. It mirrors `state_of`'s branch order exactly, with one deliberate
    difference: a dependency naming a task that does not exist reads as
    incomplete (BLOCKED) instead of raising `KeyError`. `source` travels with it
    so the page can say which of the two an operator is reading.
    """
    from_registry: dict[str, str] = {}
    for group in groups or ():
        grouped_state = group.get("state")
        if not grouped_state:
            continue
        for task in group.get("tasks") or ():
            from_registry[task["id"]] = grouped_state
    if from_registry:
        return from_registry, "registry"
    status = {row["id"]: row["status"] for row in rows}
    states: dict[str, str] = {}
    for row in rows:
        stored = row["status"]
        if stored == "completed":
            states[row["id"]] = TaskState.COMPLETED.value
        elif stored == "blocked":
            states[row["id"]] = TaskState.BLOCKED_BY_OPERATOR.value
        elif stored == "retired":
            states[row["id"]] = TaskState.RETIRED.value
        elif any(status.get(dep) != "completed" for dep in row["depends_on"]):
            states[row["id"]] = TaskState.BLOCKED.value
        elif stored == "in_progress":
            states[row["id"]] = TaskState.IN_PROGRESS.value
        else:
            states[row["id"]] = TaskState.READY.value
    return states, "status"


def _dep_sccs(nodes: list[str], deps: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components, ITERATIVELY.

    Iterative because "must not hang and must not blow up" is a requirement of
    this panel rather than a preference: the registry refuses a cycle at load,
    so the only file that reaches here with one is a hand-edited or corrupt one,
    and that is precisely when a recursion limit or an unbounded walk would take
    the whole page down.

    A component of more than one node is a cycle; a single node is one only when
    it depends on itself. Nodes and each node's dependencies are walked in the
    order given (the caller sorts both), so the components — and the cycle paths
    derived from them below — are deterministic.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, cursor = work[-1]
            if cursor == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            descended = False
            children = deps[node]
            while cursor < len(children):
                child = children[cursor]
                cursor += 1
                if child not in index:
                    work[-1] = (node, cursor)
                    work.append((child, 0))
                    descended = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if descended:
                continue
            work[-1] = (node, cursor)
            if low[node] == index[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                components.append(component)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return components


def _dep_cycle_path(members: list[str], deps: dict[str, list[str]]) -> list[str]:
    """One concrete cycle inside a cyclic component, as ids.

    In `depends_on` order — `["a", "b", "a"]` reads "a depends on b depends on
    a" — because that is the direction the file declares and the direction an
    operator has to edit. Every node of a component this size lies on a cycle,
    so following the first in-component dependency from the lowest id revisits a
    node in at most `len(members)` steps; the walk is bounded by that fact, not
    by a cap.
    """
    inside = set(members)
    node = min(members)
    seen: dict[str, int] = {}
    path: list[str] = []
    while node not in seen:
        seen[node] = len(path)
        path.append(node)
        node = next(dep for dep in deps[node] if dep in inside)
    return path[seen[node]:] + [node]


def dependency_graph(tasks_data: dict, groups: list[dict] | None = None) -> dict:
    """The `depends_on` relation as a drawable, cycle-safe layered graph.

    One edge per declared `(dependency, dependent)` pair, pointing from the
    dependency to the dependent — the direction work flows, so an arrow into a
    blocked node is the thing being waited for.

    Only the CONNECTED subgraph is drawn. 91 of the 132 tasks declare no
    dependency and have none declared on them; rendering them as isolated dots
    is what makes the 41 that matter unreadable. `omitted` counts them, so the
    omission is on the page rather than silent, and `omitted + shown == total`
    holds for every payload.

    Layout is by LAYER: a node's layer is one past the highest layer of anything
    it depends on, so a task is drawn after everything it depends on. Cycles
    cannot satisfy that and are not made to: the graph is condensed on its
    strongly connected components first, the condensation is a DAG by
    construction, and the layering runs over that — so the members of a cycle
    share a layer, everything downstream of them still lands after them, and no
    edge is dropped to make the picture acyclic. Each cycle is reported in
    `cycles` with a concrete path.
    """
    rows = _dep_rows(tasks_data)
    states, states_from = _dep_states(rows, groups)
    known = {row["id"] for row in rows}
    titles = {row["id"]: row["title"] for row in rows}

    # ONE edge per pair, in declaration order. A dependency listed twice on the
    # same task is one relation stated twice, not two edges — and the duplicate
    # would draw exactly on top of the first, so the drawing could not show it
    # either way.
    edges: list[tuple[str, str]] = []
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        for dep in row["depends_on"]:
            pair = (dep, row["id"])
            if pair in pairs:
                continue
            pairs.add(pair)
            edges.append(pair)

    drawn = {node for pair in pairs for node in pair}
    deps: dict[str, list[str]] = {node: [] for node in drawn}
    dependents: dict[str, list[str]] = {node: [] for node in drawn}
    for dep, dependent in edges:
        deps[dependent].append(dep)
        dependents[dep].append(dependent)
    for node in drawn:
        deps[node].sort()
        dependents[node].sort()

    ordered = sorted(drawn)
    components = _dep_sccs(ordered, deps)
    component_of = {
        node: number for number, members in enumerate(components) for node in members
    }
    cyclic_members = {
        node
        for members in components
        if len(members) > 1 or members[0] in deps[members[0]]
        for node in members
    }
    cycles = [
        {"nodes": sorted(members), "path": _dep_cycle_path(members, deps)}
        for members in components
        if len(members) > 1 or members[0] in deps[members[0]]
    ]
    cycles.sort(key=lambda cycle: cycle["nodes"])

    # Longest-path layering over the CONDENSATION, by Kahn. The condensation of
    # any directed graph is acyclic, so this terminates on a cyclic input as
    # readily as on an acyclic one — that is the whole reason the components are
    # computed first rather than the layering being given a visited set and a
    # prayer.
    upstream: dict[int, set[int]] = {number: set() for number in range(len(components))}
    downstream: dict[int, set[int]] = {number: set() for number in range(len(components))}
    for dep, dependent in edges:
        source, target = component_of[dep], component_of[dependent]
        if source != target:
            upstream[target].add(source)
            downstream[source].add(target)
    remaining = {number: len(items) for number, items in upstream.items()}
    queue = sorted(number for number, count in remaining.items() if count == 0)
    depth: dict[int, int] = {}
    while queue:
        number = queue.pop(0)
        depth[number] = 1 + max((depth[up] for up in upstream[number]), default=-1)
        for down in sorted(downstream[number]):
            remaining[down] -= 1
            if remaining[down] == 0:
                queue.append(down)

    layer_of = {node: depth[component_of[node]] for node in ordered}
    placed = sorted(ordered, key=lambda node: (layer_of[node], node))
    filled: dict[int, int] = {}
    nodes = []
    for node in placed:
        layer = layer_of[node]
        row_index = filled.get(layer, 0)
        filled[layer] = row_index + 1
        nodes.append({
            "id": node,
            "title": titles.get(node, ""),
            # A drawn id that is not a task cannot have a state, and inventing
            # one would be the page asserting something the registry refuses to.
            "state": states.get(node, "unknown") if node in known else "unknown",
            "known": node in known,
            "layer": layer,
            "row": row_index,
            "cyclic": node in cyclic_members,
            "depends_on": list(deps[node]),
            "dependents": len(dependents[node]),
        })

    return {
        "nodes": nodes,
        "edges": [
            {"from": dep, "to": dependent,
             # An edge INSIDE a cycle: the one kind whose dependent cannot be
             # drawn after its dependency. Marked rather than dropped.
             "cycle": component_of[dep] == component_of[dependent]}
            for dep, dependent in edges
        ],
        "cycles": cycles,
        "total": len(rows),
        "shown": len(known & drawn),
        # Tasks in no relation at all — neither depending on anything nor
        # depended on. Counted, never drawn.
        "omitted": len(known - drawn),
        # Ids something depends on that are not tasks. `from_dict` accepts such
        # a row and `state_of` then raises on it, so these are drawn as nodes in
        # the `unknown` state rather than becoming a missing edge.
        "unknown": sorted(drawn - known),
        "layers": max(filled) + 1 if filled else 0,
        "states_from": states_from,
    }


# ---- the summary an operator reads first --------------------------------------
#
# The page listed tasks and answered none of the three questions the operator
# actually arrives with: how much is done, how much is moving, and is the queue
# converging. On 2026-08-06 that took a script to answer by hand — 66 tasks, 17
# completed, 23 in progress, 18 pending, 8 blocked — and the flat list is still
# what shipped.
#
# The TASK-STATE counts are derived from `GROUPS`, i.e. from
# `TaskRegistry.state_of()` and nothing else. Not "also from state_of" — from
# the SAME `groups` payload the roadmap panel below renders, computed once in
# `collect()` and passed to both. A second `TaskRegistry.from_dict` here would
# be correct-by-test rather than correct-by-construction, and the requirement is
# that the summary cannot disagree with what actually dispatches.
#
# The in-progress PUBLICATION breakdown is a different question with a different
# source, and saying otherwise is how a claim rots: `state_of()` knows a task is
# IN_PROGRESS and cannot know whether its candidate reached a branch. That comes
# from the task's execution record (`candidate_sha`, `intended_remote*`) plus the
# one cached `git ls-remote` — evidence, not registry state — which is exactly
# why an unreadable remote there renders `unknown` rather than a verdict.


#: One count per `TaskState`, in the order the tiles are read: `(count key,
#: tile label, GROUPS key)`. The GROUPS key is what pins each row to a state —
#: the summary counts what the Roadmap panel groups, never a rollup of its own.
#:
#: ONE STATE PER COUNT, and the count key IS `TaskState.value`. The previous
#: shape folded READY ∪ BLOCKED into `pending` and then spent the freed name on
#: BLOCKED_BY_OPERATOR, so the word `blocked` meant the quarantine up here and
#: "waiting on a dependency" in the Roadmap panel below and in the per-state
#: dict carried beside it — two states under one word, on one page, which is
#: the exact confusion `TaskState` was split up to end. It came from the 2026-08-06 hand count, which read STORED
#: STATUS STRINGS (`RETIRED` did not exist yet): stored `pending` really is
#: READY ∪ BLOCKED and stored `blocked` really is BLOCKED_BY_OPERATOR. That is a
#: fact about the FILE FORMAT, not a vocabulary the page should adopt — the
#: derived split is the whole reason `state_of()` exists.
#:
#: The vocabulary is `TaskRegistry.summary()`'s, which the loop already puts in
#: front of the reviewer every round: ready / blocked / quarantined / retired.
#: The labels only ever WIDEN a word ("blocked on a dependency", and the
#: quarantine takes the Roadmap group's own "needs a human"); none of them
#: renames a state or covers two.
STAT_BUCKETS: tuple[tuple[str, str, str], ...] = (
    ("completed", "completed", "done"),
    ("in_progress", "in progress", "in_progress"),
    ("ready", "ready", "ready"),
    ("blocked", "blocked on a dependency", "blocked"),
    ("blocked_by_operator", "needs a human", "needs_human"),
    ("retired", "retired", "retired"),
)

#: The groups whose tasks count as OPEN work — everything that is neither
#: finished nor deliberately dropped. This is the denominator's other half:
#: `open + completed == total - retired`, which is what makes the percentage
#: mean anything.
#:
#: RETIRED is excluded from BOTH sides on purpose. Counting a retirement as
#: outstanding understates progress (nobody will ever do it), and counting it as
#: done overstates it (nobody ever did it); the honest move is to take it out of
#: the fraction entirely and report the denominator alongside the percentage so
#: the exclusion is visible rather than assumed.
OPEN_GROUPS = frozenset({"in_progress", "ready", "blocked", "needs_human"})

#: The three things `in_progress` actually means, plus the one honest non-answer.
#: This breakdown is the part of the summary that carries information: a flat
#: "in progress: 23" hides that twelve of those tasks were holding unpublished
#: candidates on 2026-08-06, each pinning a `task_base_sha` and so each a
#: `task_base_behind_head` park waiting for the next branch move — the failure
#: that stopped the loop twice on 2026-08-04.
IN_PROGRESS_KINDS = ("published", "unpublished_candidate", "no_candidate", "unknown")


def in_progress_rows(tasks: list[dict], executions: dict, remote_ok: bool,
                     refs: dict, remote_name: str = "origin") -> list[dict]:
    """Classify each in-progress task into one of `IN_PROGRESS_KINDS`.

    Pure: `refs` is `{branch: sha}` as already read by `_remote_refs`, and
    `remote_ok` is whether that read succeeded. No git, no network, no lock.

    The rules, and the reason each is the safe direction:

    * **No `candidate_sha` in the record → `no_candidate`**, decided from the
      record alone and REGARDLESS of whether the remote answered. There is
      nothing to publish, so reachability cannot change the answer, and routing
      it through `unknown` would hide the "dispatched, nothing committed" count
      behind a network failure.
    * **Candidate, and the branch on the remote is AT that sha → `published`.**
      B10 should then have completed the task, so a non-zero count here means
      something is not retiring.
    * **Candidate, remote readable, branch absent or at a DIFFERENT sha →
      `unpublished_candidate`.** A branch head that is not this commit does not
      carry this commit; the detail names both shas so the verdict is
      inspectable rather than asserted.
    * **Candidate, and the remote could not be read → `unknown`.** An
      unreachable remote must never render as "not published": it cannot tell
      "no such branch" from "the branch is right there", and inventing the
      alarming one out of a network hiccup is the failure `_remote_refs`
      already returns a `(reachable, refs)` PAIR to prevent.

    `intended_remote` is the remote the RECORD names. A record that names one
    we did not read is `unknown` for the same reason — but a record that names
    NOTHING is the ordinary shape, not a foreign remote, so it defaults to
    `remote_name`, the remote `_remote_refs` actually polled. Reading absence as
    "some other remote" would make the whole breakdown read `unknown` against
    live data while every test still passed.
    """
    out = []
    for task in tasks:
        task_id = task.get("id") or ""
        record = executions.get(task_id) or {}
        candidate = str(record.get("candidate_sha") or "")
        branch = branch_for(task_id, record)
        named = str(record.get("intended_remote") or "").strip() or remote_name
        published = refs.get(branch) or ""
        if not candidate:
            kind = "no_candidate"
            detail = ("dispatched, nothing committed — a timeout, a refusal, or "
                      "an abandoned round")
        elif not remote_ok:
            kind = "unknown"
            detail = (f"{named} could not be read, so whether {candidate[:12]} was "
                      f"published is unknown — never reported as not published")
        elif named != remote_name:
            kind = "unknown"
            detail = (f"the record names remote {named}, which was not read — "
                      f"whether {candidate[:12]} was published is unknown")
        elif published == candidate:
            kind = "published"
            detail = (f"{candidate[:12]} is on {named}/{branch} — B10 should have "
                      f"completed this task, so it is retireable")
        else:
            kind = "unpublished_candidate"
            where = f"that branch is at {published[:12]}" if published else "no such branch"
            detail = (f"{candidate[:12]} is not on {named}/{branch} ({where}) — it "
                      f"pins task_base_sha, so the next branch move parks it")
        out.append({"id": task_id, "title": task.get("title") or "",
                    "branch": branch, "sha": candidate[:12],
                    "kind": kind, "detail": detail})
    return out


def _area_of(task_id: str) -> str:
    """The family a task belongs to: the id prefix before the first `-`.

    `inbox-03` → `inbox`, `dash-05` → `dash`. Not a stored field and not a
    lookup table — an area is invented by whoever names a task, so deriving it
    from the id is the only reading that stays true when a new family appears.
    An id with no separator is its own area; an empty id says so rather than
    creating a blank row.
    """
    head = str(task_id or "").split("-")[0].strip()
    return head or "(unnamed)"


def _tally(tasks: list[dict], key, sort_by_key: bool) -> list[dict]:
    """`[{"key": …, "count": n}, …]`, ordered so the answer is readable.

    `sort_by_key` is the priority case: 1 outranks 2 outranks the default 100,
    so the natural order IS the answer and re-sorting by count would scramble
    it. Areas have no order of their own, so they lead with the biggest.
    """
    counts: dict = {}
    for task in tasks:
        bucket = key(task)
        counts[bucket] = counts.get(bucket, 0) + 1
    items = sorted(counts.items()) if sort_by_key else sorted(
        counts.items(), key=lambda kv: (-kv[1], str(kv[0]))
    )
    return [{"key": k, "count": n} for k, n in items]


def roadmap_stats(groups: list[dict], executions: dict, remote_ok: bool,
                  refs: dict, remote_name: str = "origin") -> dict:
    """The counts, the in-progress breakdown, and the two open-work splits.

    `groups` is the payload `task_groups()` already produced, so every TASK
    STATE here came from `TaskRegistry.state_of()` by construction, one count
    per state, keyed by `TaskState.value`. The in-progress publication
    breakdown is the one thing that does NOT come from `state_of()` — see
    `in_progress_rows`, which reads execution records against the cached remote
    refs and reports `unknown` when that remote could not be read.

    An EMPTY `groups` means the task graph would not load as a registry (see
    `task_groups`), and this reports `readable: False` rather than a confident
    row of zeros — "no tasks" and "unreadable" call for opposite reactions, and
    a summary is exactly the panel where a fabricated zero would be believed.

    Read-only and lock-free: it takes already-read values and does arithmetic.
    """
    if not groups:
        return {
            "readable": False,
            "total": 0,
            "counts": {name: 0 for name, _label, _key in STAT_BUCKETS},
            "tiles": [],
            "open": 0,
            "denominator": 0,
            "percent_done": None,
            "in_progress": {"counts": {k: 0 for k in IN_PROGRESS_KINDS}, "rows": []},
            "open_by_priority": [],
            "open_by_area": [],
            "line": "the task graph could not be read — no counts",
        }

    by_key = {g["key"]: g for g in groups}
    counts = {
        name: int(by_key.get(key, {}).get("count", 0))
        for name, _label, key in STAT_BUCKETS
    }
    total = sum(int(g["count"]) for g in groups)
    open_tasks = [t for g in groups if g["key"] in OPEN_GROUPS for t in g["tasks"]]
    # `total - retired`, spelled from the buckets rather than recomputed, so the
    # figure under the percentage is the same number the retired count shows.
    denominator = total - counts["retired"]
    rows = in_progress_rows(
        by_key.get("in_progress", {}).get("tasks", []),
        executions, remote_ok, refs, remote_name,
    )
    return {
        "readable": True,
        "total": total,
        # Keyed by `TaskState.value`, one entry per state. There is no separate
        # `by_state` any more BECAUSE there is no roll-up left to compensate
        # for: two dicts of the same numbers under different names is how the
        # page came to have two words for one state in the first place.
        "counts": counts,
        # The same counts as an ordered list carrying the LABEL each state is
        # rendered under, so the page never spells a state itself. Each row
        # names its state AND the `GROUPS` key it was counted from, so
        # label-to-state is assertable directly rather than by position.
        "tiles": [
            {"state": name, "group": key, "label": label, "count": counts[name]}
            for name, label, key in STAT_BUCKETS
        ],
        "open": len(open_tasks),
        "denominator": denominator,
        # `None`, never 0, when there is nothing to be a fraction of: 0% of an
        # empty roadmap is a verdict nobody measured.
        "percent_done": (
            round(100.0 * counts["completed"] / denominator, 1) if denominator else None
        ),
        "in_progress": {
            "counts": {k: sum(1 for r in rows if r["kind"] == k) for k in IN_PROGRESS_KINDS},
            "rows": rows,
        },
        "open_by_priority": _tally(open_tasks, lambda t: t.get("priority", 100), True),
        "open_by_area": _tally(open_tasks, lambda t: _area_of(t.get("id") or ""), False),
        # `TaskRegistry.summary()`'s own sentence, state for state and word for
        # word — the loop puts that string in front of the reviewer on every
        # round, so the page must not make a second authority out of one fact.
        # It omits only what belongs to a dispatch decision rather than to a
        # count: summary()'s priority-1 breakdown and its `next ready:` tail.
        "line": (
            f"{total} tasks: {counts['completed']} completed, "
            f"{counts['in_progress']} in progress, {counts['ready']} ready, "
            f"{counts['blocked']} blocked, "
            f"{counts['blocked_by_operator']} quarantined, "
            f"{counts['retired']} retired"
        ),
    }


def pipeline(state: dict, agents: list, blockers: list) -> list[dict]:
    """The loop's stages, each with a state the page can render.

    Derived rather than stored: the orchestrator persists a phase, not a
    pipeline position, so this maps one to the other in a single place instead
    of scattering the logic through the template.
    """
    phase = state.get("phase") or ""
    ex = state.get("task_execution") or {}
    has_unit = bool(ex.get("task_id"))
    candidate = ex.get("candidate_sha")
    decision = state.get("last_decision")
    parked = phase == "needs_user" or bool(blockers)

    def st(active, done=False):
        if parked and active:
            return "blocked"
        return "active" if active else ("done" if done else "idle")

    return [
        {"key": "orchestrator", "label": "Orchestrator", "detail": f"phase {phase or '—'}",
         "state": "blocked" if parked else ("active" if phase else "idle")},
        {"key": "worker", "label": "Worker repo", "detail": ex.get("task_branch") or "—",
         "state": st(has_unit and not candidate, done=bool(candidate))},
        {"key": "agents", "label": f"Agents ({len(agents)})",
         "detail": ", ".join(a["domain"][:28] for a in agents) or "none in flight",
         "state": st(bool(agents), done=bool(candidate))},
        {"key": "commit", "label": "Commit", "detail": (candidate or "—")[:12],
         "state": "done" if candidate else st(False)},
        {"key": "review", "label": "ChatGPT review",
         "detail": f"decision {decision}" if decision else "awaiting",
         # `delivering` is part of getting the packet in front of the reviewer
         # (an oversized diff sent as numbered parts), so it reads as the same
         # step here rather than as a separate stall.
         "state": st(phase in ("delivering", "submitting", "awaiting"), done=bool(decision))},
        {"key": "publisher", "label": "Publisher", "detail": "exact-SHA push",
         "state": st(decision == "push" and phase == "executing")},
    ]


def _remote_refs(repo: Path, ttl: int = 60) -> tuple[bool, list[dict]]:
    """`(reachable, [{"ref","sha","full"}, …])` for `origin`, cached per repo.

    `reachable` is the whole reason this returns a pair. A failed `ls-remote`
    and a remote with no branches both produce an empty list, and the merge
    column must NOT read those the same way: no branches means work was never
    published, a failed lookup means nothing is known. Collapsing them would
    invent an alarming state out of a network hiccup.

    Cached for `ttl` seconds because the page polls every 2s and this is the
    only call that touches the network.
    """
    entry = _REMOTE_CACHE.get(str(repo))
    now = time.time()
    if entry and now - entry["at"] <= ttl:
        return entry["ok"], entry["refs"]
    code, out = _run_status(["git", "ls-remote", "--heads", "origin"], cwd=repo, timeout=15)
    refs = []
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            # `full` is what ancestry is asked about; `sha` stays the short
            # display value the Git panel has always shown.
            refs.append({"ref": ref.replace("refs/heads/", ""), "sha": sha[:12],
                         "full": sha.strip()})
    ok = code == 0
    _REMOTE_CACHE[str(repo)] = {"at": now, "ok": ok, "refs": refs}
    return ok, refs


def collect(repo: Path) -> dict:
    sd = repo / ".autoloop"
    state = _json(sd / "state.json") or {}
    # `tasks.json` only exists once a registry has been saved; before that the
    # CLI seeds from the tracked file. Reading only the former showed an empty
    # roadmap while `next-task` correctly reported rt-01.
    #
    # Through `_tasks_file`, so this is the same path `/api/priority` writes:
    # the row's number is read back from disk after a save, and a read that
    # went somewhere else would report the edit as lost.
    tasks = _json(_tasks_file(repo)) or {}
    if not tasks.get("tasks"):
        seeded = _json(repo / "autoloop" / "seed_tasks.json")
        if isinstance(seeded, list):
            tasks = {"tasks": seeded}

    # --- loop liveness -------------------------------------------------
    # Read the LOCK, which is the authority, rather than matching a process
    # name. Two bugs lived here: the file was read as "lock.json" when it is
    # named LOCK, so lock_pid was always empty; and the process pattern was
    # "autoloop run --continuous", which never matches a loop started with
    # `autoloop start` — that command calls the run path IN-PROCESS, so the
    # argv still says "start". Together they reported "stopped" while the loop
    # was demonstrably executing (2026-08-03).
    #
    # `LoopLock` is now the single source: it is boot-aware, so a lock left by
    # a power cut whose pid has since been reused reads as stale rather than
    # live — a distinction a bare `ps -p` cannot make.
    from .lock import LoopLock

    lock_info = LoopLock(sd).read()
    lock_alive = lock_info is not None and LoopLock.is_live(lock_info)
    lock_pid = str(lock_info.pid) if lock_info else ""
    # Kept for display only. Never the authority: it is the check that was
    # wrong before, and both spellings of the command are matched now.
    pids = [
        p
        for p in _run(["pgrep", "-f", "autoloop (start|run)"]).split()
        if p
    ]

    if lock_alive:
        health = ("good", "running")
    elif lock_info is not None:
        health = ("critical", "stopped — stale lock")
    else:
        health = ("warning", "stopped")

    # --- audit run: which domains have landed --------------------------
    runs = sorted((sd / "audit").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if (sd / "audit").is_dir() else []
    run_dir = runs[0] if runs else None
    completed = sorted(p.stem for p in (run_dir / "raw").glob("*")) if run_dir and (run_dir / "raw").is_dir() else []

    # --- blockers -------------------------------------------------------
    blockers = []
    for f in sorted((sd / "blockers").glob("blk-*.json")) if (sd / "blockers").is_dir() else []:
        b = _json(f)
        if b and not b.get("resolved_at"):
            blockers.append({
                "id": b.get("id"), "kind": b.get("kind"), "code": b.get("code"),
                "question": (b.get("question") or "")[:400], "task": b.get("task_id"),
            })

    # --- roadmap --------------------------------------------------------
    roadmap = []
    for t in tasks.get("tasks", []):
        roadmap.append({
            "id": t.get("id"), "title": t.get("title"),
            "status": t.get("status") or "pending", "reason": (t.get("blocked_reason") or "")[:200],
            # Read straight off the raw row, like everything else here: this
            # list stays the tolerant view that renders even when the graph
            # does not load as a registry.
            "superseded_by": list(t.get("superseded_by") or ()),
            # Ascending: 1 outranks 2, default 100 sorts last (tasks.Task.priority).
            "priority": t.get("priority", 100),
        })
    roadmap.sort(key=lambda r: (r.get("priority", 100), r.get("id") or ""))
    # Read ONCE and shared: the grouped roadmap and the merge panel ask the
    # same records different questions (which commit is under review now, which
    # commit landed), and two globs of the same directory could disagree
    # mid-write.
    executions = _executions(repo)

    # The grouped read of the same rows, hoisted out of the payload literal
    # because the flat list below borrows one answer from it.
    groups = task_groups(tasks, executions)
    # A retirement that predates `TaskState.RETIRED` is migrated when the
    # registry LOADS (`tasks._migrate_retirements`), and only the loop ever
    # writes `tasks.json` — so between this page reading the file and the loop
    # next saving it, the raw row still says `blocked` while the grouped read
    # says retired. Left alone, the app-task panel would mark brw-02 "blocked"
    # two panels below the Retired group listing it, which is the exact
    # two-meanings-one-word confusion this change exists to end. The registry's
    # derived state wins where they disagree; the raw read stays the fallback
    # for everything else, including a graph that would not load at all.
    _retired_ids = {t["id"] for g in groups if g["key"] == "retired" for t in g["tasks"]}
    for row in roadmap:
        if row["id"] in _retired_ids:
            row["status"] = "retired"

    # --- recent events --------------------------------------------------
    events = []
    tp = sd / "transcript.jsonl"
    if tp.exists():
        try:
            for line in tp.read_text().splitlines()[-14:]:
                if not line.strip():
                    continue
                r = json.loads(line)
                d = r.get("data") or {}
                detail = d.get("decision") or d.get("code") or d.get("error") or d.get("result") or ""
                events.append({"ts": (r.get("ts") or "")[11:19], "type": r.get("type"),
                               "detail": str(detail)[:120]})
        except (OSError, ValueError):
            pass

    # --- git (local is cheap; remote cached — never poll the network at 2s)
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo).strip()
    head = _run(["git", "rev-parse", "HEAD"], cwd=repo).strip()
    dirty = len([x for x in _run(["git", "status", "--porcelain"], cwd=repo).splitlines() if x])
    remote_ok, remote_refs = _remote_refs(repo)
    # `{branch: full sha}` from that ONE cached `ls-remote`. The merge panel and
    # the in-progress breakdown both ask what a branch is at; they must ask the
    # same read, not the network twice.
    ref_shas = {r["ref"]: r.get("full") or r.get("sha") or "" for r in remote_refs}

    live_agents_cache = live_agents()
    ex = state.get("task_execution") or {}
    return {
        "health": {"role": health[0], "label": health[1], "pids": pids,
                   "lock_pid": lock_pid, "lock_alive": lock_alive},
        "session": {
            "phase": state.get("phase"), "iteration": state.get("iteration"),
            "session_id": (state.get("session_id") or "")[:12],
            "last_decision": state.get("last_decision"),
            "question": state.get("question"),
            "updated_at": (state.get("updated_at") or "")[11:19],
        },
        "task": {
            "id": ex.get("task_id"), "branch": ex.get("task_branch"),
            "worker": ex.get("worktree_path"),
            "base": (ex.get("task_base_sha") or "")[:12],
            "candidate": (ex.get("candidate_sha") or "")[:12],
            "round": ex.get("review_round"), "attempts": ex.get("attempt_count"),
        },
        "agents": live_agents_cache,
        # `None` when nothing is executing — the page then renders no figures at
        # all rather than the last round's. See `worker_progress`.
        "progress": worker_progress(state),
        "audit": {"run": run_dir.name if run_dir else None, "completed": completed},
        "events": events,
        "blockers": blockers,
        "roadmap": roadmap,
        # The same tasks, split by `TaskRegistry.state_of()`. `roadmap` stays
        # the flat, tolerant read of the raw JSON — the app-task panel joins
        # against it, and it still renders when the graph itself does not load.
        "groups": groups,
        # The top-of-page counts, derived from `groups` — the SAME payload the
        # roadmap panel renders, so the summary cannot disagree with the list
        # under it or with what the loop dispatches. `remote_ok` / `ref_shas`
        # are the cached `_remote_refs` read above, reused rather than
        # re-polled: this is the only network call on the page and it must
        # never be made twice per request.
        "stats": roadmap_stats(groups, executions, remote_ok, ref_shas),
        # The same rows again, read as a RELATION rather than as a list: one
        # edge per declared `depends_on`, pointing from the dependency to the
        # dependent. Built from the raw JSON, not from a registry, so a cyclic
        # or dangling file still draws — see `dependency_graph`. `groups` is
        # passed in rather than re-derived so each node's state is the state the
        # Roadmap panel already shows for it.
        "depgraph": dependency_graph(tasks, groups),
        "inbox": _pending_inbox(repo),
        "app_tasks": app_tasks(repo, report_glob=_audit_report_glob(repo)),
        "pipeline": pipeline(state, live_agents_cache, blockers),
        # Completed is not integrated: which finished tasks are actually in the
        # branch, decided by git ancestry rather than by their status.
        "merge": merge_report(repo, roadmap, head, remote_ok, remote_refs, branch,
                              executions),
        "git": {"branch": branch, "head": head[:12], "dirty": dirty, "remote": remote_refs},
        "served_at": time.strftime("%H:%M:%S"),
        # `stale` means the file on disk has changed since this process
        # imported it — restart the dashboard to pick the change up.
        "build": {"running": _IMPORT_STAMP, "on_disk": _source_stamp(),
                  "stale": _IMPORT_STAMP != _source_stamp()},
    }


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Autoloop — live</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
/* Roles, not raw hex, so light/dark swap in one place. Surfaces are the
   reference palette's: page plane, chart surface, and the node fill the marks
   were validated against. Status colours are FIXED — never themed. */
:root{color-scheme:light;
      --surface:#f9f9f7;--card:#fcfcfb;--soft:#f4f3f0;
      --ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--line:#e1e0d9;--axis:#c3c2b7;
      --mark-active:#2a78d6;--mark-done:#52514e;--mark-idle:#c3c2b7;
      --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;
      /* The roadmap docket's own ramp, `--rm-` prefixed and declared in all
         three theme scopes below. Neutrals biased toward verdigris: this panel
         is an instrument you steer the loop with, so it reads as one surface
         rather than as another table. The BAND ramp is deliberately separate
         from --rm-accent — priority decides execution order, which is a
         different axis from "this is the roadmap", and folding the two into one
         hue leaves nothing to encode order with. Never a --good/--warning/
         --critical: a p0 is not a health verdict. */
      --rm-ground:#F2F6F5;--rm-surface:#FFFFFF;--rm-surface-2:#F7FAF9;
      --rm-ink:#141D1B;--rm-ink-soft:#3C4B48;--rm-muted:#5B6B68;
      --rm-line:#DBE5E2;--rm-line-soft:#E8EFED;
      --rm-accent:#2F7268;--rm-accent-soft:#E2EFEC;
      --rm-band-urgent:#A83C2A;--rm-band-active:#966C15;
      --rm-band-queued:#3B6096;--rm-band-parked:#6C7A77}
/* Dark is SELECTED, not flipped: its own steps, validated against #2a2a27.
   Declared under both scopes so the toggle wins either way — the :not() guard
   lets an explicit light stamp beat OS-dark. */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
      --surface:#0d0d0d;--card:#1a1a19;--soft:#2a2a27;
      --ink:#fff;--ink2:#c3c2b7;--muted:#898781;--line:#2c2c2a;--axis:#383835;
      --mark-active:#3987e5;--mark-done:#c3c2b7;--mark-idle:#383835;
      --rm-ground:#0E1413;--rm-surface:#151E1C;--rm-surface-2:#111917;
      --rm-ink:#E2EBE8;--rm-ink-soft:#C0CDC9;--rm-muted:#8FA09C;
      --rm-line:#243130;--rm-line-soft:#1C2726;
      --rm-accent:#63B9A8;--rm-accent-soft:#172A27;
      --rm-band-urgent:#E0866F;--rm-band-active:#D3A44E;
      --rm-band-queued:#84A9DB;--rm-band-parked:#8B9895}}
:root[data-theme="dark"]{color-scheme:dark;
      --surface:#0d0d0d;--card:#1a1a19;--soft:#2a2a27;
      --ink:#fff;--ink2:#c3c2b7;--muted:#898781;--line:#2c2c2a;--axis:#383835;
      --mark-active:#3987e5;--mark-done:#c3c2b7;--mark-idle:#383835;
      --rm-ground:#0E1413;--rm-surface:#151E1C;--rm-surface-2:#111917;
      --rm-ink:#E2EBE8;--rm-ink-soft:#C0CDC9;--rm-muted:#8FA09C;
      --rm-line:#243130;--rm-line-soft:#1C2726;
      --rm-accent:#63B9A8;--rm-accent-soft:#172A27;
      --rm-band-urgent:#E0866F;--rm-band-active:#D3A44E;
      --rm-band-queued:#84A9DB;--rm-band-parked:#8B9895}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--ink);font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 72px}
header{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}
/* The page name and the task titles are the two editorial voices on a page
   otherwise made of machine data, so both take ui-serif; ids, ordinals, counts
   and the descriptions themselves take ui-monospace; every other piece of
   chrome stays on the system sans set on `body`. */
h1{font-size:15px;margin:0;letter-spacing:.02em;
   font-family:ui-serif,"New York","Iowan Old Style",Georgia,serif}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2);margin:0 0 10px}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.pill{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:4px 11px;font-size:12.5px;background:var(--card)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
/* The one tile allowed a status role, and only when the count is non-zero:
   "finished work is not in your branch" IS a health verdict, unlike `phase` or
   `iteration`. Reuses the existing --warning variable rather than declaring a
   new colour, and the value still ships an icon + the word, so the border is
   never the only thing saying it. */
.tile.warn{border-color:var(--warning)}
.k{font-size:11px;color:var(--ink2);text-transform:uppercase;letter-spacing:.06em}
/* Two open-work breakdowns side by side on a wide screen, stacked when there is
   no room. No new colours: the tables are the same `table` rule as everywhere
   else, and the headings the same h2. */
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}
/* Proportional figures on standalone values — tabular-nums makes `121` look
   loose at display sizes. It belongs on columns that align vertically, which is
   `code` in the tables below, not here. */
.v{font-size:18px;margin-top:2px;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:5px 8px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;color:var(--ink2);font-weight:500;text-transform:uppercase;letter-spacing:.05em}
tr:last-child td{border-bottom:0}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink2);
     font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.muted{color:var(--ink2)}.empty{color:var(--ink2);font-style:italic;font-size:13px}
.scroll{overflow-x:auto}
/* pipeline */
/* height:auto, not a fixed px: the viewBox owns the aspect ratio, so the
   container is sized to its content instead of letterboxing ~48px of dead
   space under the nodes. */
#flow{width:100%;height:auto;display:block}
.node rect{fill:var(--soft);stroke:var(--line);stroke-width:1;rx:8;cursor:pointer}
.node.sel rect{stroke:var(--ink2);stroke-width:2}
.node:focus{outline:none}
.node:focus rect{stroke:var(--ink);stroke-width:2}
.node text{font-size:11.5px;fill:var(--ink);pointer-events:none}
.node .sub{font-size:10px;fill:var(--ink2)}
/* Solid hairlines, one shade off the surface — never dashed. */
.edge{stroke:var(--line);stroke-width:2;fill:none;marker-end:url(#ar)}
.edge.on{stroke:var(--axis)}
.badge{font-size:10px;fill:var(--ink2)}
/* Legend: identity is never colour-alone, so each key is swatch + icon + word.
   Present because the diagram carries four distinct states. */
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:10px;font-size:12px;color:var(--ink2)}
.legend span{display:inline-flex;align-items:center;gap:6px}
.legend i{width:10px;height:10px;border-radius:50%;display:inline-block;font-style:normal}
/* dependency graph — the pipeline's rules at a second scale: hand-built SVG,
   nothing fetched, and identity carried by icon + word as well as by fill. Its
   own selectors rather than the pipeline's `.node`/`.edge` because the two
   graphs answer different questions and must stay restyleable apart. The svg
   is sized in px from its own content and sits in a `.scroll`: shrinking a
   40-node graph to the column width would take the labels below reading size,
   and an unreadable label is the same as no label. --critical is the one
   status role used here, on the cycle, which IS a health verdict — and the
   cycle is also named in words above the drawing and marked in the table
   below, so the colour is never the only thing saying it. */
#depsvg{display:block}
.dnode rect{fill:var(--soft);stroke:var(--line);stroke-width:1;rx:7;cursor:pointer}
.dnode.sel rect{stroke:var(--ink2);stroke-width:2}
.dnode.cyc rect{stroke:var(--critical);stroke-width:2}
.dnode:focus{outline:none}
.dnode:focus rect{stroke:var(--ink);stroke-width:2}
.dnode text{font-size:11px;fill:var(--ink);pointer-events:none}
.dnode .tid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.dnode .sub{font-size:9.5px;fill:var(--ink2)}
/* Solid hairlines, like the pipeline's: a dashed back-edge would encode "this
   is the odd one" in a channel the rest of the page does not use. */
.dedge{stroke:var(--line);stroke-width:1.5;fill:none;marker-end:url(#dar)}
.dedge.on{stroke:var(--axis);stroke-width:2}
.dedge.cyc{stroke:var(--critical);marker-end:url(#dcar)}
/* Roadmap group headings. The count sits IN the heading rather than under it:
   "Needs a human 0" is the whole sentence an operator came to read, and a
   number one line away from its label is a number nobody reads. */
h3.gh{font-size:11px;margin:16px 0 6px;color:var(--ink);font-weight:600;
      text-transform:uppercase;letter-spacing:.06em}
h3.gh:first-child{margin-top:0}
.gc{color:var(--ink2);font-weight:400;font-variant-numeric:tabular-nums}
details{margin-top:10px}
summary{font-size:12px;color:var(--ink2);cursor:pointer}
summary:hover{color:var(--ink)}
/* ---- the roadmap docket -------------------------------------------------
   A row is a <details>: the summary is the docket line an operator scans and
   the body is the task's COMPLETE description, so the answer to "what does
   this queued task actually say" is on the page instead of in tasks.json.
   Every colour here is an --rm- token declared in all three theme scopes, so
   the panel survives with no reference file to copy from, and none of them is
   a status role. Sizes are in the roadmap's own scale rather than the page's
   because the description is the content here, not a cell in a table. */
#rmbar{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-bottom:11px}
#rmq{flex:1 1 260px;min-width:0;padding:8px 11px;border-radius:8px;
     border:1px solid var(--rm-line);background:var(--rm-surface);color:var(--rm-ink);
     font:inherit;font-size:13.5px}
#rmq::placeholder{color:var(--rm-muted)}
#rmq:focus-visible,#rmexpand:focus-visible,#roadmap summary:focus-visible{
     outline:2px solid var(--rm-accent);outline-offset:2px}
#rmcount{font-size:12.5px;color:var(--rm-muted);font-variant-numeric:tabular-nums}
#roadmap{background:var(--rm-ground);border:1px solid var(--rm-line);
     border-radius:11px;padding:13px 13px 4px}
#roadmap h3.gh{color:var(--rm-ink)}
#roadmap .gc{color:var(--rm-muted)}
#roadmap .empty,#roadmap .muted{color:var(--rm-muted)}
#roadmap details.task{margin:0 0 9px;background:var(--rm-surface);
     border:1px solid var(--rm-line);border-radius:10px;overflow:hidden}
/* The band is the priority ramp, never the accent: it encodes the order these
   rows are selected in, which the accent does not say anything about. */
#roadmap details.task[data-band="urgent"]{border-left:3px solid var(--rm-band-urgent)}
#roadmap details.task[data-band="active"]{border-left:3px solid var(--rm-band-active)}
#roadmap details.task[data-band="queued"]{border-left:3px solid var(--rm-band-queued)}
#roadmap details.task[data-band="parked"]{border-left:3px solid var(--rm-band-parked)}
#roadmap details.task>summary{display:grid;grid-template-columns:46px 1fr auto;gap:13px;
     align-items:baseline;padding:11px 14px;cursor:pointer;list-style:none;
     font-size:14px;color:var(--rm-ink)}
#roadmap details.task>summary::-webkit-details-marker{display:none}
#roadmap details.task>summary:hover .ttl{color:var(--rm-accent)}
/* The left rail. It is the position next_ready() picks from, which is why a
   row that cannot be picked shows a dash at chrome size instead of a number:
   an ordinal on an unpickable row would be a claim about dispatch order. */
#roadmap .ord{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
     font-variant-numeric:tabular-nums;font-size:20px;font-weight:600;
     color:var(--rm-muted);text-align:right;line-height:1.15}
#roadmap .ord.none{font-size:12.5px;font-weight:400;opacity:.65}
#roadmap .mid{min-width:0}
#roadmap .tid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
     font-size:11.5px;color:var(--rm-accent)}
#roadmap .ttl{font-family:ui-serif,"New York","Iowan Old Style",Georgia,serif;
     font-size:16px;line-height:1.3;margin-top:2px;color:var(--rm-ink)}
#roadmap .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:7px}
#roadmap .chip{font-size:10.5px;letter-spacing:.04em;text-transform:uppercase;
     padding:2px 7px;border-radius:5px;border:1px solid var(--rm-line);
     color:var(--rm-muted);background:var(--rm-surface-2);white-space:nowrap}
#roadmap .chip.p{border-color:transparent;color:var(--rm-surface);font-weight:600}
#roadmap .chip.p[data-band="urgent"]{background:var(--rm-band-urgent)}
#roadmap .chip.p[data-band="active"]{background:var(--rm-band-active)}
#roadmap .chip.p[data-band="queued"]{background:var(--rm-band-queued)}
#roadmap .chip.p[data-band="parked"]{background:var(--rm-band-parked)}
#roadmap .chip.wait{border-color:var(--rm-band-active);color:var(--rm-band-active)}
#roadmap .size{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
     color:var(--rm-muted);font-variant-numeric:tabular-nums;white-space:nowrap}
#roadmap .body{border-top:1px solid var(--rm-line-soft);padding:14px 14px 16px;
     background:var(--rm-surface-2)}
/* pre-wrap, not a clamp: these are hard-wrapped plain text with ALL-CAPS heads
   and indented lists, so the shape carries meaning a reflow would destroy. */
#roadmap pre.desc{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;max-width:92ch;
     font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;
     line-height:1.62;color:var(--rm-ink-soft)}
#roadmap .meta{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:14px;
     padding-top:11px;border-top:1px dashed var(--rm-line);font-size:12px;color:var(--rm-muted)}
#roadmap .meta input.pri{background:var(--rm-surface);color:var(--rm-ink);
     border-color:var(--rm-line)}
#roadmap .meta button.save{background:var(--rm-surface);color:var(--rm-ink-soft);
     border-color:var(--rm-line)}
#roadmap .meta button.save:hover{color:var(--rm-accent);border-color:var(--rm-accent)}
@media (max-width:560px){
  #roadmap details.task>summary{grid-template-columns:34px 1fr}
  #roadmap .size{grid-column:2}}
.themetog{margin-left:auto;font-size:12px;background:var(--card);color:var(--ink2);
          border:1px solid var(--line);border-radius:999px;padding:4px 11px;cursor:pointer}
.themetog:hover{color:var(--ink)}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .12s;background:var(--card);
     border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12.5px;max-width:330px;
     box-shadow:0 6px 22px rgba(0,0,0,.13);z-index:9}
.row-active{background:var(--soft)}
input.pri{width:64px;font:inherit;padding:3px 6px;border:1px solid var(--line);
  border-radius:6px;background:var(--card);color:var(--ink);font-variant-numeric:tabular-nums}
button.save{font:inherit;font-size:12px;padding:3px 10px;border:1px solid var(--line);
  border-radius:6px;background:var(--card);color:var(--ink2);cursor:pointer}
button.save:hover{color:var(--ink)}
button.save[disabled]{opacity:.5;cursor:default}
.saved{color:var(--good)}.savefail{color:var(--critical)}
/* New-task form. No new hexes — feedback reuses .saved/.savefail above, and
   the fields sit on the same card/line roles as everything else. */
form.newtask{display:grid;gap:9px;max-width:760px}
form.newtask label{display:block;font-size:11px;color:var(--ink2);
  text-transform:uppercase;letter-spacing:.05em}
form.newtask input,form.newtask textarea{display:block;width:100%;margin-top:3px;
  font:13px/1.45 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;padding:5px 7px;
  border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);
  text-transform:none;letter-spacing:normal}
form.newtask textarea[name="approved_paths"]{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
form.newtask .two{display:grid;grid-template-columns:2fr 1fr;gap:9px}
form.newtask .actions{display:flex;align-items:center;gap:8px}
</style>
<div class="wrap">
  <header>
    <h1>AUTOLOOP</h1>
    <span class="pill"><span class="dot" id="hdot"></span><span id="hlabel">…</span></span>
    <span class="muted" style="font-size:12px" id="served"></span>
    <button class="themetog" id="themetog" type="button">◐ theme</button>
  </header>

  <div id="stale" style="display:none;border:1px solid var(--warning);border-radius:8px;
       padding:9px 12px;margin-bottom:14px;font-size:13px"></div>

  <!-- FIRST on the page, above every list, because it is what an operator reads
       first and it was not on the page at all. Counting 66 tasks by hand took a
       script (2026-08-06); the answer belongs here.

       The task-state counts are derived from `TaskRegistry.state_of()` via the
       same `groups` payload the Roadmap panel below renders — one count per
       state, under the state's own name — so the numbers up here cannot
       disagree with the rows down there, and no word up here can mean a
       different state down there. The in-progress publication breakdown is the
       one part that is NOT state_of(): it reads execution records against the
       cached remote refs. -->
  <section id="summary">
    <h2>Roadmap — how much is done, how much is moving, is it converging</h2>
    <div id="statline" style="font-size:13px;margin-bottom:11px"></div>
    <div class="grid" id="stattiles"></div>
    <div id="statwiphead" style="font-size:13px;margin:4px 0 9px"></div>
    <div id="statwip" class="scroll"></div>
    <div class="cols" id="statopen" style="margin-top:16px"></div>
    <p class="muted" style="font-size:12px;margin:12px 0 0">
      The task counts come from <code>TaskRegistry.state_of()</code> — the
      function the loop dispatches on — never from the stored status string,
      which cannot tell Ready from Blocked and spells a quarantine and a
      retirement the same way. Each word means here exactly what it means in
      the Roadmap panel below and in <code>autoloop next-task</code>:
      <b>blocked</b> is waiting on an incomplete dependency and resolves itself,
      <b>needs a human</b> is the quarantine that waits on you, <b>retired</b>
      waits on nobody. The in-progress breakdown is NOT from
      <code>state_of()</code> — a registry knows a task is in progress and
      cannot know where its commit went. The candidate comes from the task's
      execution record and publication from one cached
      <code>git ls-remote</code>, so a remote that could not be read reads
      <b>unknown</b> rather than not-published. Retired tasks are left out of
      the percentage on BOTH sides: counting a deliberately dropped task as
      outstanding understates progress, counting it as done overstates it.</p>
  </section>

  <div class="grid" id="tiles"></div>

  <!-- Live progress for the task executing NOW. STATIC markup, and outside
       `#tiles` on purpose: `render()` rewrites that grid's innerHTML whenever
       the payload changes, so a value updated on every tick regardless would be
       clobbered on the next payload-change tick. Hidden until a payload carries
       a running task — an idle loop shows nothing here, never the last round's
       numbers. -->
  <section id="progressbox" style="display:none">
    <h2>Live progress — read from the worker repo, not from the agent</h2>
    <div id="progress"></div>
  </section>

  <section>
    <h2>Pipeline — hover or focus for detail, click to inspect</h2>
    <svg id="flow" viewBox="0 0 1140 74" preserveAspectRatio="xMidYMid meet">
      <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="var(--line)"/></marker></defs>
      <g id="edges"></g><g id="nodes"></g>
    </svg>
    <div id="detail" class="muted" style="font-size:12.5px;margin-top:6px"></div>
    <div class="legend" id="legend"></div>
    <!-- The table view is the WCAG-clean twin: every stage's full detail as
         text, so a tooltip is never the only way to read a value, and the
         dark-mode contrast WARN on `critical` has its required relief. -->
    <details><summary>Table view — all stages as text</summary>
      <div id="flowtable" class="scroll"></div></details>
  </section>

  <!-- Directly under the pipeline, and the count is also a tile above it: the
       sentence "N finished tasks are not in your branch" is the point of this
       panel, and a number nobody scrolls to says nothing.

       Grouped by state, in triage order, the same way the Roadmap panel below
       is: the three groups that need a human render inline in `#merged`, and
       `Merged` — the group that only ever grows and that nobody has to act on —
       is a counted <details> in STATIC markup, for the same reason `#donebox`
       is. `render()` rewrites only its summary text and the rows inside it, so
       a disclosure the operator opened stays open across the 2s poll instead of
       snapping shut. `#mergehead` stays ABOVE the groups: it says which branch
       "merged" is relative to, and grouping must not push that out of view. -->
  <section>
    <h2>Merged into the branch — completed is published, not integrated</h2>
    <div id="mergehead" style="font-size:13px;margin-bottom:9px"></div>
    <div id="merged" class="scroll"></div>
    <details id="mgmergedbox">
      <summary id="mgmergedsum">✓ Merged</summary>
      <div id="mgmergedrows" class="scroll"></div>
      <p class="muted" style="font-size:12px;margin:9px 0 0">
        Merged is the one group here nobody has to act on: the work is in this
        branch. Collapsed is not hidden — the count is in the summary above, and
        a merged row's whole value is that it stays findable when someone asks
        where a task's commit went.</p>
    </details>
    <p class="muted" style="font-size:12px;margin:9px 0 0">
      Merged means git's own answer: the branch sha is an ancestor of this
      checkout's HEAD. Never the task status, the execution record or a name
      match — those all read "done" for work that was only ever pushed to a side
      branch. <b>unknown</b> means git could not answer (origin unreachable, or
      the repository would not read); it is never reported as not-merged — which
      is why that group renders with its count even at zero.</p>
  </section>

  <!-- The roadmap read as a RELATION rather than as a list. A second hand-built
       SVG and not a replacement for the pipeline flow above: that one is the
       loop's phase progression, this one is what is waiting on what. Nothing is
       fetched at runtime and no library is loaded, for the same reason nothing
       else on this page is.

       `#deptablebox` is STATIC markup, like `#mgmergedbox` and `#donebox`:
       `renderDeps` writes its summary text and its rows and NEVER its `.open`,
       so an operator reading the edge list keeps it open across the 2s poll.
       The svg lives in a `.scroll` because it is sized in px from its own
       content — see the CSS note.

       The section carries `#deppanel` because `depsBusy` asks "is the operator
       holding anything in HERE" — a focused node, a hovered node, a text
       selection anchored inside it — and needs one static ancestor to ask
       `contains()` of. It is never rebuilt, which is also what makes it the
       place to bind the settle listeners once. -->
  <section id="deppanel">
    <h2>Dependency graph — every depends_on, dependency → dependent</h2>
    <div id="depnote" style="font-size:13px;margin-bottom:9px"></div>
    <div class="scroll">
      <svg id="depsvg" viewBox="0 0 1140 90">
        <defs>
          <marker id="dar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--line)"/></marker>
          <!-- Its own marker rather than one that inherits the stroke: an arrow
               that stayed line-coloured on a cycle edge would leave the edge
               half-marked. -->
          <marker id="dcar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--critical)"/></marker>
        </defs>
        <g id="depedges"></g><g id="depnodes"></g>
      </svg>
    </div>
    <div id="depdetail" class="muted" style="font-size:12.5px;margin-top:6px"></div>
    <div class="legend" id="deplegend"></div>
    <details id="deptablebox"><summary id="deptablesum">Table view — every edge as text</summary>
      <div id="deptable" class="scroll"></div></details>
    <p class="muted" style="font-size:12px;margin:9px 0 0">
      An arrow points from a dependency to the task waiting on it, and a task is
      drawn after everything it depends on. Only tasks in at least one
      dependency relation are drawn: the line above says how many were left out,
      and a task in no relation at all is held up by nothing and holds nothing
      up. A cycle cannot be laid out in that order and is not drawn as though it
      could be — its members share a column, they are marked, and the path is
      spelled out above. The number on the right of a node is how many tasks are
      waiting behind it. Display only: nothing here edits depends_on, and the
      order the loop dispatches in is still next_ready()'s.</p>
  </section>

  <!-- Grouped by state, in triage order. `Retired` and `Done` are <details>
       in STATIC markup rather than inside the container `render()` rebuilds,
       for the same reason `#flowtable` is: expanding one would otherwise snap
       shut on the next payload change. Only the summary text and the rows are
       rewritten, so each starts collapsed on load and stays where the operator
       put it after that. Each is looked up by GROUP KEY, never by "the first
       collapsed group". -->
  <section>
    <h2>Roadmap — grouped by state; open a row for its full description</h2>
    <!-- STATIC, like the new-task form and for the same reason: `renderRoadmap`
         rewrites `#roadmap` on every payload change, so a search box living
         inside it would have its half-typed text erased every 2s and would
         rebind its listener each time. The filter reads this field's CURRENT
         value on every render, so a search survives the poll. -->
    <div id="rmbar">
      <input type="search" id="rmq" aria-label="Search tasks"
             placeholder="Search ids, titles, and full descriptions…">
      <button class="save" type="button" id="rmexpand">Expand all</button>
      <span id="rmcount"></span>
    </div>
    <div id="roadmap" class="scroll"></div>
    <details id="retiredbox">
      <summary id="retiredsum">⊘ Retired</summary>
      <div id="retired" class="scroll"></div>
      <p class="muted" style="font-size:12px;margin:9px 0 0">
        Retired means superseded: the work is not waiting on a dependency and
        not waiting on you — it happened, or stopped, under another task id.
        Nothing here is a call to action, and nothing here is ever deleted:
        the successor column IS the record that (say) brw-07 + brw-08 continue
        brw-02 / brw-04. A blank successor means the task went stale rather
        than being replaced.</p>
    </details>
    <details id="donebox">
      <summary id="donesum">✓ Done</summary>
      <div id="done" class="scroll"></div>
      <p class="muted" style="font-size:12px;margin:9px 0 0">
        Done means the reviewed candidate landed on the task's OWN side branch
        (AUTOLOOP_TODO B10) — published, not merged into this branch. Whether
        the work is actually here is a different question, answered by the
        merged-into-the-branch panel above.</p>
    </details>
    <div id="queued" class="muted" style="font-size:12px;margin-top:9px"></div>
  </section>

  <!-- STATIC markup, outside anything `render()` rewrites. The roadmap table is
       rebuilt from scratch on every payload change (2s poll), so a form living
       inside it would erase half-typed text and rebind its listener each time.
       Its listener is attached once, at script load. -->
  <section>
    <h2>New task — queued to the inbox, merged by the loop</h2>
    <form class="newtask" id="newtask" autocomplete="off">
      <div class="two">
        <label>task id<input name="id" required placeholder="dash-03"></label>
        <label>priority<input name="priority" type="number" step="1" value="100"></label>
      </div>
      <label>title<input name="title" required placeholder="what the task delivers"></label>
      <label>description
        <textarea name="description" rows="4" required
          placeholder="what to build, and the constraints that are load-bearing"></textarea></label>
      <label>approved paths — one per line, each typed in full
        <textarea name="approved_paths" rows="4" required
          placeholder="autoloop/dashboard.py"></textarea></label>
      <div class="actions"><button class="save" type="submit">Queue task</button>
        <button class="save" type="button" id="ntdetect">Detect paths</button>
        <span id="ntnote"></span></div>
    </form>
    <p class="muted" style="font-size:12px;margin:9px 0 0">
      Approved paths are the task's authorization: an agent may write these and
      nothing else. Nothing is inferred from the title and there is no wildcard —
      list every file, or a directory with a trailing <code>/</code>. Each one is
      validated by the registry on merge, so a bad path is refused there and
      reported, not silently accepted here. The repository's doc trackers
      (<code>TRACKER_PATHS</code> in <code>autoloop/tasks.py</code> — a fixed
      constant, not a config value) are always allowed and need not be
      listed.</p>
  </section>
  <section><h2>Language-app tasks</h2><div id="apptasks" class="scroll"></div></section>
  <section><h2>Blockers</h2><div id="blockers"></div></section>
  <section><h2>Recent events</h2><div id="events" class="scroll"></div></section>
  <section><h2>Git</h2><div id="git" class="scroll"></div></section>
</div>
<div id="tip"></div>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const rows = (head, body) => body ? `<table><tr>${head.map(h=>`<th>${h}</th>`).join("")}</tr>${body}</table>` : `<p class="empty">none</p>`;
// state -> icon + word. Colour never carries meaning on its own: every mark
// ships both, and the table view below repeats all of it as text.
const MARK = {active:["▶","running"],done:["✓","done"],blocked:["■","blocked"],idle:["·","idle"]};
// One hue for the stage running NOW (categorical slot 1, validated); finished
// stages recede to secondary ink; idle sits on the axis neutral. `blocked` is
// the only genuine health verdict, so it is the only status colour here — see
// the MARKS comment in dashboard.py for the validator runs behind these.
const FILL = {active:"var(--mark-active)",done:"var(--mark-done)",
              blocked:"var(--critical)",idle:"var(--mark-idle)"};
const ORDER = ["active","done","blocked","idle"];
let SEL = null, LAST = null, LASTJSON = null;

const tip = document.getElementById("tip");
function showTip(e, html){ tip.innerHTML = html; tip.style.opacity = 1;
  const x = Math.min(e.clientX + 14, innerWidth - 346); tip.style.left = x + "px";
  tip.style.top = Math.min(e.clientY + 14, innerHeight - 120) + "px"; }
function hideTip(){ tip.style.opacity = 0; }

function drawFlow(d){
  const N = d.pipeline, W = 1140, w = 158, h = 54, gap = (W - N.length*w) / (N.length - 1), y = 8;
  const edges = [], nodes = [];
  N.forEach((n, i) => {
    const x = i * (w + gap);
    if (i) { const px = (i-1)*(w+gap)+w, on = N[i-1].state==="done" || N[i-1].state==="active";
      edges.push(`<path class="edge${on?" on":""}" d="M${px+4},${y+h/2} L${x-6},${y+h/2}"/>`); }
    const [ic, word] = MARK[n.state] || MARK.idle;
    // Only ever render a label that fits: ~24 chars at 10px inside a 158px
    // box, with an ellipsis so truncation is visible rather than silent.
    const det = (n.detail || ""), shown = det.length > 24 ? det.slice(0,23) + "…" : det;
    // tabindex/role: keyboard focus must reveal exactly what hover reveals.
    nodes.push(`<g class="node${SEL===n.key?" sel":""}" data-k="${esc(n.key)}" tabindex="0"
      role="button" aria-label="${esc(n.label)} — ${esc(word)}. ${esc(det)}"
      transform="translate(${x},${y})">
      <rect width="${w}" height="${h}"/>
      <circle cx="13" cy="15" r="5" fill="${FILL[n.state]}"/>
      <text x="26" y="19">${esc(n.label)}</text>
      <text class="sub" x="11" y="36">${esc(ic)} ${esc(word)}</text>
      <text class="sub" x="11" y="49">${esc(shown)}</text></g>`);
  });
  document.getElementById("edges").innerHTML = edges.join("");
  document.getElementById("nodes").innerHTML = nodes.join("");
  document.querySelectorAll("#nodes .node").forEach(g => {
    const n = N.find(x => x.key === g.dataset.k);
    const body = `<b>${esc(n.label)}</b> — ${esc((MARK[n.state]||MARK.idle)[1])}`
               + `<br><span class="muted">${esc(n.detail)}</span>`;
    g.addEventListener("mousemove", e => showTip(e, body));
    g.addEventListener("mouseleave", hideTip);
    // Keyboard parity: focus anchors the same tooltip to the node's own box.
    g.addEventListener("focus", () => {
      const r = g.getBoundingClientRect();
      showTip({clientX: r.left, clientY: r.bottom - 8}, body);
    });
    g.addEventListener("blur", hideTip);
    const pick = () => { SEL = SEL === n.key ? null : n.key; render(LAST, true); };
    g.addEventListener("click", pick);
    g.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
  });

  document.getElementById("legend").innerHTML = ORDER.map(s =>
    `<span><i style="background:${FILL[s]}"></i>${esc(MARK[s][0])} ${esc(MARK[s][1])}</span>`).join("");

  document.getElementById("flowtable").innerHTML = rows(["stage","state","detail"],
    N.map(n => `<tr><td>${esc(n.label)}</td><td>${esc(MARK[n.state][0])} ${esc(MARK[n.state][1])}</td>
      <td class="muted">${esc(n.detail || "—")}</td></tr>`).join(""));

  document.getElementById("detail").textContent = SEL
    ? `${SEL}: ${(N.find(x=>x.key===SEL)||{}).detail || ""}`
    : "no stage selected";
}

// Elapsed time, as a duration a human reads at a glance rather than a raw
// second count. `null`/`undefined` is UNKNOWN and says so — never 0s.
function fmtDur(s){
  if (typeof s !== "number") return "unknown";
  const t = Math.max(0, Math.floor(s)), h = Math.floor(t/3600),
        m = Math.floor((t%3600)/60), sec = t%60;
  return (h ? `${h}h ` : "") + (h || m ? `${m}m ` : "") + `${sec}s`;
}

// Live progress for the task executing now. Every number here came from git in
// the worker repo; nothing the agent reported about itself is consulted, and an
// unreadable worker prints "unknown" rather than a zero it did not measure.
function renderProgress(p){
  const box = document.getElementById("progressbox");
  const el = document.getElementById("progress");
  // Falsy means NOTHING is executing. Hide the whole section: figures left
  // beside an idle loop read as live activity, which is the failure this panel
  // exists to prevent.
  if (!p) { box.style.display = "none"; el.innerHTML = ""; return; }
  box.style.display = "";
  const known = typeof p.insertions === "number";
  // A capped scan counts fewer lines than were written, never more, so the
  // figure is marked as the lower bound it is.
  const ge = p.partial ? "≥" : "";
  const lines = known
    ? `<b>+${ge}${esc(p.insertions)}</b> <b>−${ge}${esc(p.deletions)}</b> in `
      + `<b>${ge}${esc(p.files)}</b> file(s)`
    : `<b>lines changed unknown</b>`;
  const why = known
    ? `git diff --numstat against ${esc(p.base)} plus untracked files in `
      + `<code>${esc(p.worker)}</code>`
      + (p.partial ? " — capped scan, so the line counts are a lower bound" : "")
    : `${esc(p.note || "the worker repo could not be read")} — shown as `
      + `unknown, never as zero: zero would mean the agent has written nothing.`;
  el.innerHTML = `<div class="v">▶ ${esc(p.task_id)} · ${lines} · `
    + `${esc(fmtDur(p.elapsed_seconds))} since dispatch</div>`
    + `<p class="muted" style="font-size:12px;margin:6px 0 0">${why}</p>`;
}

// ---- the summary, at the top -------------------------------------------------
// The three questions an operator arrives with — how much is done, how much is
// moving, is the queue converging — answered before anything they have to
// scroll for. Every figure here was computed on the backend and this function
// does no arithmetic of its own, so it cannot drift from the Roadmap panel
// below. The state counts came from `TaskRegistry.state_of()` and carry their
// LABELS with them, so this template never spells a state itself; the
// in-progress breakdown came from the execution records plus one cached
// `ls-remote`, which is why `unknown` is one of its states and not an error.
//
// The in-progress breakdown is the part that carries information: `in progress:
// 23` says nothing, `twelve holding unpublished candidates` says the loop is
// about to park on task_base_behind_head. Each state ships an icon and a word —
// colour never carries meaning alone on this page.
const WIP = {published:["✓","published to its side branch"],
             unpublished_candidate:["▲","holding an unpublished candidate"],
             no_candidate:["·","no candidate committed"],
             unknown:["?","publication unknown"]};

function renderStats(s){
  const line = document.getElementById("statline");
  const tiles = document.getElementById("stattiles");
  const wiphead = document.getElementById("statwiphead");
  const wip = document.getElementById("statwip");
  const open = document.getElementById("statopen");
  // Unreadable is not zero. A summary is the one panel where a fabricated 0
  // would be believed, so an unloadable graph says so and shows nothing else.
  if (!s || !s.readable) {
    line.innerHTML = `<p class="empty">the task graph could not be read — `
      + `tasks.json did not load as a registry, so there are no counts.</p>`;
    tiles.innerHTML = ""; wiphead.innerHTML = ""; wip.innerHTML = ""; open.innerHTML = "";
    return;
  }
  const c = s.counts || {};
  const pct = typeof s.percent_done === "number" ? `${s.percent_done}%` : "—";
  // Completed against not-completed is the convergence number. The denominator
  // is spelled out beside it so the retired exclusion is visible rather than an
  // assumption the reader has to make.
  line.innerHTML = `<b>${esc(s.line)}</b><br><span class="muted">`
    + `${esc(pct)} done — ${esc(c.completed)} completed against ${esc(s.open)} still `
    + `open, out of ${esc(s.denominator)} tasks that are not retired. `
    + `${esc(c.retired)} retired task(s) are excluded from both sides.</span>`;
  const w = (s.in_progress || {}).counts || {};
  const nUnpub = w.unpublished_candidate || 0;
  // One tile per TaskState, LABEL AND ALL from the payload: the backend pins
  // each label to a `GROUPS` state, so this template cannot invent a word for a
  // state or quietly put two states under one tile. The pct tile is labelled
  // "% done" rather than "done", because Done is already the completed group's
  // name below and a tile reading `done 41%` beside a group reading `Done 17`
  // is the same two-meanings problem in a smaller place.
  tiles.innerHTML = [
    ["tasks", s.total],
    ...(s.tiles || []).map(t => [t.label, t.count]),
    ["% done (retired excluded)", pct],
    // The one tile allowed a status role here, and only when it is non-zero:
    // an unpublished candidate IS a health verdict — it pins a task_base_sha.
    ["unpublished candidates", (nUnpub ? "▲ " : "✓ ") + nUnpub, nUnpub ? "warn" : ""],
  ].map(([k, v, cls]) => `<div class="tile ${cls || ""}"><div class="k">${esc(k)}</div>`
      + `<div class="v">${esc(v ?? "—")}</div></div>`).join("");

  wiphead.innerHTML = (nUnpub
      ? `<b>▲ ${nUnpub} in-progress task(s) hold a candidate that is NOT published.</b> `
        + `Each pins a task_base_sha, so each is a task_base_behind_head park waiting `
        + `to happen the next time the branch moves.`
      : `✓ no in-progress task is holding an unpublished candidate.`)
    + ((w.published || 0) ? ` <span class="muted">✓ ${w.published} already published to `
        + `a side branch — B10 should have completed those, so a non-zero count means `
        + `something is not retiring.</span>` : "")
    + ((w.no_candidate || 0) ? ` <span class="muted">· ${w.no_candidate} dispatched with `
        + `nothing committed.</span>` : "")
    + ((w.unknown || 0) ? ` <span class="muted">? ${w.unknown} unknown — the remote could `
        + `not be read, so publication is unverified rather than absent.</span>` : "");
  wip.innerHTML = rows(["task","branch","candidate","state","why"],
    ((s.in_progress || {}).rows || []).map(r => {
      const [ic, word] = WIP[r.kind] || WIP.unknown;
      return `<tr class="${r.kind === "unpublished_candidate" ? "row-active" : ""}">
        <td><code>${esc(r.id)}</code></td><td><code>${esc(r.branch)}</code></td>
        <td><code>${esc(r.sha || "—")}</code></td><td>${esc(ic)} ${esc(word)}</td>
        <td class="muted">${esc(r.detail)}</td></tr>`;
    }).join(""));

  // The open work, twice. The counts say how much is left; these say whether
  // what is left is worth doing next — 31 open reads differently once eleven of
  // them are p2, and once eight turn out to be one over-split family.
  const tally = (label, col, list, fmt) => `<div><h2>${esc(label)}</h2>`
    + rows([col, "open"], (list || []).map(r =>
        `<tr><td>${esc(fmt(r.key))}</td><td><code>${esc(r.count)}</code></td></tr>`).join(""))
    + `</div>`;
  open.innerHTML =
      tally(`Open by priority (${s.open})`, "priority", s.open_by_priority, k => `p${k}`)
    + tally(`Open by area (${s.open})`, "area", s.open_by_area, k => k);
}

// ---- the roadmap docket ------------------------------------------------------
//
// One <details> per live task: the summary is the docket line, the body is the
// task's COMPLETE description. The panel used to send id/title/priority and
// nothing else, so the operator's actual question about a queued task — what
// does it say — could only be answered by opening `.autoloop/tasks.json`, which
// is the file the whole roadmap is steered from.
//
// Every group here was still decided by `TaskRegistry.state_of()` on the
// backend — the same function the loop dispatches on. NOTHING in this block
// reads a status string: `blocked` on disk means QUARANTINED, while the Blocked
// group means "waiting on an incomplete dependency", and a page that mixed
// those up would send an operator to answer a blocker that does not exist.
// Retired is the third of those and needs nobody at all — its row names the
// successor instead of asking for anything.
//
// Priority editing is unchanged and lives in the opened body. It writes
// tasks.json IMMEDIATELY (2026-08-16), under the fine-grained mutex the loop's
// own saves take, and the row then shows the value the server read BACK from
// the file. A Save is "saved", not "queued": an operator steers with priority,
// so a change that lands on the loop's next drain has missed the decision it
// was for. Creation is still queued to the inbox — that one carries
// authorization.
const GICON = {in_progress:"▶", needs_human:"■", ready:"○", blocked:"◍",
               retired:"⊘", done:"✓"};
// Which rows the operator has opened, by task id. Rebuilt DOM loses the `open`
// attribute, and a successful priority save clears LASTJSON to force exactly
// that rebuild — so without this, editing a priority would snap the row you
// were reading shut. Restored in the same pass that binds the buttons.
const RMOPEN = new Set();

// Everything between the two markers below is PURE — payload in, HTML or a
// boolean out — and is lifted verbatim by the tests, which run it under node
// against a hostile description. Keep it free of the DOM and of module state,
// or those tests stop being able to reach it, and keep each marker alone on
// its line: the tests split on it, so trailing prose would land in the
// extracted script as a syntax error.
// PURE_ROADMAP_START
// The priority BAND, kept separate from the accent because it encodes a
// different axis: which rows run first. A missing/non-numeric priority parks,
// which is where the registry's own default (100) lands anyway.
const rmBand = p => typeof p !== "number" ? "parked"
  : p <= 2 ? "urgent" : p <= 5 ? "active" : p <= 8 ? "queued" : "parked";
// The filter matches DESCRIPTION TEXT as well as id and title. Half of what an
// operator is looking for ("which task mentioned tasks.json?") is only in the
// description, and a search that read titles alone would answer "no results"
// for a task that is right there.
const rmMatch = (t, f) => !f
  || String(t.id || "").toLowerCase().includes(f)
  || String(t.title || "").toLowerCase().includes(f)
  || String(t.description || "").toLowerCase().includes(f);
const priCell = t => `<input class="pri" type="number" step="1" value="${esc(t.priority)}"
         data-id="${esc(t.id)}" aria-label="priority for ${esc(t.id)}">
        <button class="save" data-id="${esc(t.id)}">Save</button>
        <span class="note" data-id="${esc(t.id)}"></span>`;
// EVERY interpolation goes through esc(). A description is untrusted text out
// of tasks.json — anyone who can write that file can write `<script>` — and it
// is the one field on this page long enough that nobody would notice it had
// been read as markup rather than shown as text.
const rmRow = t => {
  const band = rmBand(t.priority);
  const desc = String(t.description || "");
  const waits = t.waits_on || [];
  const chars = typeof t.chars === "number" ? t.chars : desc.length;
  // No ordinal means the loop cannot pick this row at all — it is in progress,
  // quarantined, waiting on a dependency, retired or done. A number there
  // would claim a place in an order it is not in.
  const ord = typeof t.ordinal === "number"
    ? `<span class="ord">${esc(t.ordinal)}</span>`
    : `<span class="ord none" title="not in the ready order">—</span>`;
  const chips = [
    `<span class="chip p" data-band="${band}">p${esc(t.priority)}</span>`,
    waits.length ? `<span class="chip wait">waits on ${esc(waits.join(", "))}</span>` : "",
    (t.superseded_by || []).length
      ? `<span class="chip">superseded by ${esc((t.superseded_by || []).join(", "))}</span>` : "",
  ].filter(Boolean).join("");
  return `<details class="task" data-band="${band}" data-id="${esc(t.id)}">
      <summary>${ord}<span class="mid"><span class="tid">${esc(t.id)}</span>
        <div class="ttl">${esc(t.title)}</div>
        <div class="chips">${chips}</div></span>
      <span class="size">${esc(chars)} ch</span></summary>
      <div class="body">
        <pre class="desc">${desc ? esc(desc) : "no description recorded"}</pre>
        <div class="meta"><span>${esc(t.detail || "—")}</span>
          <span>priority ${priCell(t)}</span></div>
      </div></details>`;
};
// PURE_ROADMAP_END

function renderRoadmap(d){
  const groups = d.groups || [];
  const board = document.getElementById("roadmap");
  const countEl = document.getElementById("rmcount");
  if (!groups.length) {
    // A roadmap with no tasks still renders every group zeroed, so an empty
    // list can only mean the graph itself did not load. Say which: "no tasks"
    // and "unreadable" call for opposite reactions.
    board.innerHTML = `<p class="empty">the task graph could not be read — `
      + `tasks.json did not load as a registry, so nothing here is grouped. `
      + `The tasks themselves are still listed by the loop's own commands.</p>`;
    countEl.textContent = "";
    return;
  }
  const f = (document.getElementById("rmq").value || "").trim().toLowerCase();
  // `hidden` is the backend's call, not this template's: an empty group is
  // dropped EXCEPT "Needs a human", which renders and says none. Silence and
  // "nothing needs you" must not look the same.
  const shown = groups.filter(g => !g.collapsed && !g.hidden);
  let total = 0, matched = 0;
  const body = shown.map(g => {
    const hits = g.tasks.filter(t => rmMatch(t, f));
    total += g.tasks.length; matched += hits.length;
    // The heading count stays the STATE count, never the filtered one: the
    // summary tiles at the top are derived from this same payload, and a
    // heading that shrank as you typed would disagree with them.
    return `<h3 class="gh">${esc(GICON[g.key] || "·")} ${esc(g.label)} `
      + `<span class="gc">${esc(g.count)}</span></h3>`
      + (hits.length ? hits.map(rmRow).join("")
         : `<p class="empty">${g.tasks.length ? "nothing here matches that" : "none"}</p>`);
  }).join("");
  board.innerHTML = body
    + `<p class="muted" style="font-size:12px;margin:5px 0 12px">Open a row for the `
    + `task's complete description, exactly as tasks.json holds it. The number on `
    + `the left is execution order, not an index: it is the position `
    + `next_ready() will select from, so a task waiting on a dependency has none `
    + `— it cannot be picked whatever its priority. Lower priority runs first; `
    + `100 is the default. Save writes tasks.json straight away — the number that `
    + `appears beside the field is read back from the file, so it is what the loop `
    + `will read. Only priority is editable here.</p>`;
  countEl.textContent = f ? `${matched} of ${total} shown` : `${total} live task(s)`;

  // Open state is restored HERE rather than in `rmRow`, which keeps that
  // function pure. `toggle` does not bubble, so it is bound per row: a
  // delegated listener on #roadmap would simply never fire.
  board.querySelectorAll("details.task").forEach(el => {
    const id = el.dataset.id;
    if (RMOPEN.has(id)) el.open = true;
    el.addEventListener("toggle", () => {
      if (el.open) RMOPEN.add(id); else RMOPEN.delete(id);
    });
  });

  // SCOPED to #roadmap on purpose. These rows are rebuilt as fresh DOM on every
  // render, so rebinding them each time is free — but the new-task form's
  // button and this panel's Expand-all share the `save` class for styling and
  // are STATIC, so an unscoped `button.save` would hand each of them one extra
  // click listener per poll, each looking up an `input.pri[data-id="undefined"]`
  // that does not exist.
  document.querySelectorAll("#roadmap button.save").forEach(btn => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      const input = document.querySelector(`input.pri[data-id="${CSS.escape(id)}"]`);
      const note = document.querySelector(`span.note[data-id="${CSS.escape(id)}"]`);
      const value = parseInt(input.value, 10);
      if (!Number.isInteger(value)) { note.className = "note savefail"; note.textContent = " ✗ not a number"; return; }
      btn.disabled = true; note.className = "note"; note.textContent = " …";
      try {
        const r = await fetch("/api/priority", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-Autoloop": "1"},
          body: JSON.stringify({id, priority: value}),
        });
        const body = await r.json();
        // The number shown back is the server's READ-BACK from tasks.json, not
        // the one that was typed, and the input is set to it: if the two ever
        // disagree the field says what actually persisted rather than what was
        // asked for. A failure says so and leaves the typed value alone — a row
        // that silently snapped back is the bug this endpoint exists to fix.
        if (r.ok) {
          input.value = body.priority;
          note.className = "note saved"; note.textContent = ` ✓ saved (${esc(body.priority)})`;
        }
        else { note.className = "note savefail"; note.textContent = " ✗ " + (body.error || r.status); }
      } catch (e) {
        note.className = "note savefail"; note.textContent = " ✗ " + e;
      } finally { btn.disabled = false; LASTJSON = null; }
    });
  });
}

// ---- the merge panel, grouped by state ---------------------------------------
//
// The same shape as the roadmap docket below and for the same reason: on
// 2026-08-18 this panel rendered 61 rows flat, of which four needed a human,
// and the counts that could have said so were computed and thrown away. The
// three actionable groups render inline; `Merged` is a counted disclosure in
// STATIC markup, so an operator who opens it keeps it open across the 2s poll.
//
// The hide/show policy is the BACKEND's (`MERGE_GROUPS`), never this template's:
// every group renders with its count, including `Unknown (0)`. Nothing here
// filters rows or re-derives a state — `r.state` arrives decided by git.
//
// Everything between the markers is lifted verbatim by the tests, which run it
// under node against a stub document to prove an opened disclosure survives a
// refresh. Keep each marker alone on its line and keep this region free of
// module state other than `esc` / `rows`, or those tests stop being able to
// reach it.
// MERGE_PANEL_START
// Three real states plus unknown, every one of them icon + word: a colour
// alone could not tell "not published" from "not merged", and those need
// different actions (nothing was ever pushed vs it is pushed and stranded).
const MS = {merged:["✓","merged"], unmerged:["▲","NOT merged"],
            unpublished:["○","not published"], unknown:["?","unknown"]};
const msRow = r => {
  const [ic, word] = MS[r.state] || MS.unknown;
  return `<tr class="${r.state === "unmerged" ? "row-active" : ""}">
    <td><code>${esc(r.id)}</code></td><td><code>${esc(r.branch)}</code></td>
    <td><code>${esc(r.sha || "—")}</code></td><td>${esc(ic)} ${esc(word)}</td>
    <td class="muted">${esc(r.detail)}</td></tr>`;
};
// The row content is EXACTLY what it was before the grouping — same five
// columns, same icon + word, same `row-active` on an unmerged row. Grouping is
// a display axis over the rows, not a new reading of them.
const msTable = g => rows(["task","branch","sha","state","why"],
                          (g.rows || []).map(msRow).join(""));
const msHeading = g => `<h3 class="gh">${esc((MS[g.key] || MS.unknown)[0])} `
  + `${esc(g.label)} <span class="gc">${esc(g.count)}</span></h3>`;
function renderMerge(d){
  const mg = d.merge || {counts:{}, rows:[], groups:[], base_branch:"HEAD", base_head:""};
  const mc = mg.counts || {};
  const nUnmerged = mc.unmerged || 0, nUnknown = mc.unknown || 0;
  const mrows = mg.rows || [];
  const base = `${esc(mg.base_branch)} (${esc(mg.base_head || "?")})`;
  // ABOVE the groups and outside every one of them: this line names the branch
  // "merged" is relative to, and an answer of "merged" is meaningless without
  // it. Grouping must never push it out of view.
  document.getElementById("mergehead").innerHTML = (nUnmerged
      ? `<b>▲ ${nUnmerged} completed task(s) are published but NOT in ${base}.</b> `
        + `Their code is finished and is not in your branch.`
      : `✓ none of the ${mrows.length} completed task(s) are missing from ${base}.`)
    + (nUnknown ? ` <span class="muted">? ${nUnknown} unknown — git could not answer, `
                  + `so they are not counted either way.</span>` : "")
    + ((mc.unpublished || 0) ? ` <span class="muted">○ ${mc.unpublished} completed with `
                               + `no side branch at all.</span>` : "");
  const groups = mg.groups || [];
  // Every group the backend did NOT collapse, in the order it sent them, each
  // with its heading and count — an empty one included. `Unknown (0)` costs one
  // line, and an unknown row means the sweep could not judge a branch: the
  // state most worth seeing and the easiest to hide by accident. `rows()`
  // prints "none" for the body, so an empty group reads as answered rather
  // than as a section that failed to render.
  document.getElementById("merged").innerHTML =
    groups.filter(g => !g.collapsed).map(g => msHeading(g) + msTable(g)).join("")
    || `<p class="empty">no merge groups in this payload — the panel could not `
       + `be grouped, and the counts above are all there is.</p>`;
  // By KEY, never by scanning for the group that carries the collapsed flag.
  // There is one collapsed group here today and two in the roadmap panel, and
  // a predicate that finds "the collapsed one" is wrong the moment a second
  // appears — silently, by filling this disclosure with someone else's rows.
  const gMerged = groups.find(g => g.key === "merged");
  // The summary text and the row body, and NOTHING ELSE. The <details> itself
  // is static markup this function does not rebuild and never writes a
  // disclosure state to: an operator who opened it keeps it open across every
  // refresh, and writing the payload's default back here each tick would snap
  // it shut every 2 seconds, which is exactly what this must not do.
  document.getElementById("mgmergedsum").textContent =
    gMerged ? `✓ ${gMerged.label} (${gMerged.count})` : "✓ Merged";
  document.getElementById("mgmergedrows").innerHTML =
    gMerged ? msTable(gMerged) : `<p class="empty">unknown</p>`;
}
// MERGE_PANEL_END

// ---- the dependency graph ----------------------------------------------------
//
// Everything between the markers is lifted verbatim by the tests and run under
// node against a stub document, so it may reference `esc`, `rows` and its own
// declarations and NOTHING else on this page — the listener wiring itself lives
// in `bindDeps` below the region for exactly that reason, reached from in here
// only through the `DEPBIND` hook, which defaults to a no-op. The refresh gate
// IS in the region, because when this panel may redraw is behaviour and
// behaviour only a browser can run is behaviour nothing in CI checks. Keep each
// marker alone on its line.
// DEPGRAPH_START
// Layer and row arrive COMPUTED from `dependency_graph` on the backend, and
// this function does no ordering of its own. That is what keeps "a task is
// drawn after everything it depends on" a property of the payload — assertable
// in a Python test — rather than of a browser nobody runs in CI.
let DSEL = null;
// Icon + word for every state in `DEP_NODE_STATES`. A state missing from here
// renders an empty node, so the two are pinned equal by a test.
const DMARK = {completed:["✓","completed"], in_progress:["▶","in progress"],
               ready:["○","ready"], blocked:["◍","blocked"],
               blocked_by_operator:["■","needs a human"], retired:["⊘","retired"],
               unknown:["?","not a task"]};
// The same tokens the rest of the page uses; no new colours. --critical is the
// one status role, on the state that genuinely is a verdict, and every node
// ships its icon and word beside the dot.
const DFILL = {completed:"var(--mark-done)", in_progress:"var(--mark-active)",
               ready:"var(--axis)", blocked:"var(--warning)",
               blocked_by_operator:"var(--critical)", retired:"var(--mark-idle)",
               unknown:"var(--muted)"};
// The WIDE spelling, for every place that has room for it. `blocked` ON DISK
// means quarantined and `blocked` here means waiting on a dependency — two
// states under one word on one page is the exact confusion `TaskState` was
// split up to end, and it is why `STAT_BUCKETS` ships "blocked on a dependency"
// rather than "blocked". A 126px node box cannot hold that, so the short word
// stays in the box and never travels alone into a context that can hold more:
// the legend, the edge table and the tooltip all take this one.
const DLONG = {blocked:"blocked on a dependency",
               unknown:"depended on, but not a task in the registry"};
const DORDER = ["in_progress","blocked_by_operator","blocked","ready",
                "retired","completed","unknown"];
// `pad` is headroom for the arcs a cycle edge draws OVER the nodes; without it
// they would be clipped by the viewBox and a cycle would look like a gap.
const DGEO = {w:126, h:34, gapx:54, gapy:10, pad:48};
const dgX = n => n.layer * (DGEO.w + DGEO.gapx);
const dgY = n => DGEO.pad + n.row * (DGEO.h + DGEO.gapy);
const dgWord = s => DMARK[s] || DMARK.unknown;
const dgLong = s => DLONG[s] || dgWord(s)[1];
// One path per edge, and every edge gets one — including the two shapes a
// straight left-to-right line cannot express.
const dgPath = (a, b) => {
  const ax = dgX(a), ay = dgY(a), bx = dgX(b), by = dgY(b);
  if (a.id === b.id) {
    // A self-edge is a one-node cycle. A zero-length path would draw nothing
    // at all, which is the one way a cycle could silently vanish from here.
    const cx = ax + DGEO.w / 2;
    return `M${cx - 16},${ay} C${cx - 36},${ay - 42} ${cx + 36},${ay - 42} ${cx + 16},${ay}`;
  }
  if (bx > ax) {
    const x1 = ax + DGEO.w, y1 = ay + DGEO.h / 2, x2 = bx, y2 = by + DGEO.h / 2;
    const c = Math.max(24, (x2 - x1) * 0.5);
    return `M${x1},${y1} C${x1 + c},${y1} ${x2 - c},${y2} ${x2},${y2}`;
  }
  // Same column or backwards — only an edge inside a cycle can be either, and
  // it is drawn as an arc over the top rather than dropped to tidy the picture.
  const c1 = ax + DGEO.w / 2, c2 = bx + DGEO.w / 2;
  return `M${c1},${ay} C${c1},${ay - 40} ${c2},${by - 40} ${c2},${by}`;
};
const dgNode = n => {
  const [ic, word] = dgWord(n.state);
  const deps = (n.depends_on || []).join(", ") || "nothing drawn";
  // The count of tasks waiting BEHIND this one — the figure the panel exists
  // for ("port-01 is blocked and six tasks are waiting on it"). Absent rather
  // than 0 when nothing waits: a zero on every leaf is noise.
  const behind = n.dependents
    ? `<text class="badge" x="${DGEO.w - 8}" y="14" text-anchor="end">${esc(n.dependents)}↴</text>`
    : "";
  return `<g class="dnode${DSEL === n.id ? " sel" : ""}${n.cyclic ? " cyc" : ""}"
      data-id="${esc(n.id)}" tabindex="0" role="button"
      aria-label="${esc(n.id)} — ${esc(dgLong(n.state))}${n.cyclic ? ", in a dependency cycle" : ""}. Depends on ${esc(deps)}. ${esc(n.dependents)} task(s) depend on it."
      transform="translate(${dgX(n)},${dgY(n)})">
    <rect width="${DGEO.w}" height="${DGEO.h}"/>
    <circle cx="9" cy="10" r="4" fill="${DFILL[n.state] || DFILL.unknown}"/>
    <text class="tid" x="18" y="14">${esc(n.id)}</text>${behind}
    <text class="sub" x="8" y="27">${esc(ic)} ${esc(word)}${n.cyclic ? " · in a cycle" : ""}</text></g>`;
};
function renderDeps(d){
  const g = d.depgraph || {};
  const nodes = g.nodes || [], edges = g.edges || [], cycles = g.cycles || [];
  const at = {};
  nodes.forEach(n => { at[n.id] = n; });
  // The omitted count is rendered whether or not anything was omitted: "91 were
  // left out" and "this is the whole graph" are different claims, and a panel
  // that only spoke up sometimes would leave a reader guessing which they had.
  const head = nodes.length
    ? `<b>${esc(edges.length)} dependency edge(s)</b> across ${esc(nodes.length)} node(s).`
    : `<b>no task declares a dependency</b> — there is no graph to draw.`;
  const left = ` <span class="muted">${esc(g.omitted || 0)} of ${esc(g.total || 0)} task(s) `
    + `are in no dependency relation at all and are not drawn.</span>`;
  const ghosts = (g.unknown || []).length
    ? ` <span class="muted">? ${esc((g.unknown || []).length)} id(s) are depended on but are `
      + `not tasks: ${esc((g.unknown || []).join(", "))}.</span>`
    : "";
  const cyc = cycles.length
    ? `<div><b>⚠ ${esc(cycles.length)} dependency cycle(s):</b> `
      + cycles.map(c => `<code>${esc((c.path || []).join(" → "))}</code>`).join("; ")
      + ` — each task depends on the next, so nothing in one can ever become ready. `
      + `The registry refuses to load a file like this, which is why the panels `
      + `above say the task graph could not be read.</div>`
    : `<div class="muted">✓ no dependency cycle.</div>`;
  // Said out loud when it is true, because it changes what the states MEAN: the
  // registry could not be built, so these came from the stored status fields
  // rather than from state_of().
  const src = g.states_from === "status"
    ? `<div class="muted">The registry would not load, so each state here is derived `
      + `from the stored status field rather than from state_of().</div>`
    : "";
  document.getElementById("depnote").innerHTML = head + left + ghosts + cyc + src;

  const svg = document.getElementById("depsvg");
  const columns = Math.max(1, g.layers || 1);
  const deep = nodes.reduce((most, n) => Math.max(most, n.row + 1), 1);
  const width = columns * (DGEO.w + DGEO.gapx) - DGEO.gapx;
  const height = DGEO.pad + deep * (DGEO.h + DGEO.gapy);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  // An edge whose endpoints are both drawn — which is every edge, since a node
  // is drawn precisely because an edge touches it.
  document.getElementById("depedges").innerHTML = edges.map(e => {
    const a = at[e.from], b = at[e.to];
    if (!a || !b) return "";
    const on = DSEL && (DSEL === e.from || DSEL === e.to);
    return `<path class="dedge${e.cycle ? " cyc" : ""}${on ? " on" : ""}" d="${dgPath(a, b)}"/>`;
  }).join("");
  document.getElementById("depnodes").innerHTML = nodes.map(dgNode).join("");

  const present = DORDER.filter(s => nodes.some(n => n.state === s));
  document.getElementById("deplegend").innerHTML = present.map(s =>
    `<span><i style="background:${DFILL[s]}"></i>${esc(dgWord(s)[0])} ${esc(dgLong(s))}</span>`
  ).join("") + (cycles.length
    ? `<span><i style="background:var(--critical)"></i>⚠ in a cycle</span>` : "");

  // The WCAG-clean twin, and the place every edge is legible as text: a
  // drawing is not a reading of the relation for someone who cannot see it.
  document.getElementById("deptablesum").textContent =
    `Table view — all ${edges.length} edge(s) as text`;
  document.getElementById("deptable").innerHTML =
    // Two state columns, each headed by WHOSE state it is: an ambiguous header
    // defeats the point of the table, which is to be the readable twin.
    rows(["dependency", "dependency state", "waiting task", "waiting-task state",
          "note"], edges.map(e => {
      const a = at[e.from] || {}, b = at[e.to] || {};
      return `<tr><td><code>${esc(e.from)}</code></td>
        <td>${esc(dgWord(a.state)[0])} ${esc(dgLong(a.state))}</td>
        <td><code>${esc(e.to)}</code></td>
        <td>${esc(dgWord(b.state)[0])} ${esc(dgLong(b.state))}</td>
        <td class="muted">${e.cycle ? "⚠ in a dependency cycle" : ""}</td></tr>`;
    }).join(""));

  const sel = DSEL ? at[DSEL] : null;
  document.getElementById("depdetail").textContent = sel
    ? `${sel.id} — ${dgLong(sel.state)}; depends on `
      + `${(sel.depends_on || []).join(", ") || "nothing"}; `
      + `${sel.dependents} task(s) depend on it`
    : "no task selected";
}

// ---- when this panel is allowed to redraw ------------------------------------
//
// `renderDeps` above REPLACES every element it writes. Two rules decide when it
// may, and the panel owns both rather than inheriting the page's:
//
//   1. PANEL-LOCAL CHANGE DETECTION. `render`'s `LASTJSON` guard fires on the
//      whole payload, so an agent starting, a blocker opening or any other
//      figure moving used to rebuild this graph even though not one dependency
//      had changed. The signature here is `depgraph` and nothing else.
//   2. AN INTERACTION GUARD. Even a genuinely changed graph waits while the
//      operator is holding this panel. The newer payload is HELD, not dropped,
//      and `render` calls in here on every 2s tick — so it lands within one
//      tick of the interaction ending whether or not a listener fires.
//
// `force` is the panel's OWN concept and is passed by exactly one caller: the
// node picker in `bindDeps`, which changed `DSEL` and needs the selection to
// move under the operator's cursor. It is deliberately NOT `render`'s `force` —
// that one is also raised by a click on a PIPELINE node (`drawFlow`'s picker
// calls `render(LAST, true)`), and threading it through here would let an
// unrelated click replace the dep node someone is hovering.
let DEPJSON = null;   // signature of the graph currently on screen
let DEPDRAWN = null;  // the payload it was drawn from — what a selection redraws
let DEPHELD = null;   // a newer payload, held while the operator is mid-gesture
// The listener wiring, installed from outside this region because it reaches
// the page's tooltip helpers, `render` and `LAST` — none of which the node
// harness has. Left at this no-op when the region is lifted on its own; a test
// pins the real assignment, since an unassigned hook would silently bind
// nothing on the page while every node test still passed.
let DEPBIND = () => {};
const depSig = p => JSON.stringify((p || {}).depgraph || null);
// Is the operator holding this panel right now? Three ways to be, and each is a
// gesture a rebuilt DOM destroys: keyboard focus on a node (the focus ring and
// its tooltip), the pointer over one (the tooltip follows the mouse), and a text
// selection anchored inside the panel (the edge table is there to be copied).
// An OPEN <details> is not one of them — nothing rebuilds it, and a disclosure
// left open for an hour must not freeze the graph inside it.
function depsBusy(){
  const panel = document.getElementById("deppanel");
  if (!panel) return false;
  // Switched windows: the focus ring is still on a node but nobody is there, and
  // freezing until they come back and click would be the worse failure.
  if (document.hasFocus && !document.hasFocus()) return false;
  const active = document.activeElement;
  if (active && active !== document.body && panel.contains(active)) return true;
  if (panel.querySelector(".dnode:hover")) return true;
  const picked = typeof window !== "undefined" && window.getSelection
    ? window.getSelection() : null;
  if (picked && picked.rangeCount && !picked.isCollapsed) {
    const at = picked.anchorNode;
    const el = at && (at.nodeType === 1 ? at : at.parentNode);
    if (el && panel.contains(el)) return true;
  }
  return false;
}
function updateDeps(d, force){
  if (!d) return;
  const sig = depSig(d);
  // Nothing about the dependencies changed. Whatever else on the page did is
  // not this panel's business, and rebuilding for it is what threw away a
  // hovered node every two seconds.
  if (!force && sig === DEPJSON) { if (depSig(DEPHELD) === sig) DEPHELD = null; return; }
  if (!force && depsBusy()) { DEPHELD = d; return; }
  // A forced redraw draws `DEPDRAWN` — the payload already on screen — so a
  // selection click moves the highlight WITHOUT slipping a held graph in under
  // the cursor. The held one is therefore only cleared when this draw actually
  // is it.
  if (depSig(DEPHELD) === sig) DEPHELD = null;
  DEPJSON = sig;
  DEPDRAWN = d;
  renderDeps(d);
  DEPBIND(d);
}
// DEPGRAPH_END

// The listeners, outside the pure region: they reach `showTip`, `render` and
// `LAST`, none of which the node harness has. Rebound on every rebuild for the
// same reason the roadmap's Save buttons are — these nodes are fresh DOM each
// time — and scoped to #depnodes so the pipeline's own nodes are untouched.
function bindDeps(d){
  const at = {};
  ((d.depgraph || {}).nodes || []).forEach(n => { at[n.id] = n; });
  document.querySelectorAll("#depnodes .dnode").forEach(el => {
    const n = at[el.dataset.id];
    if (!n) return;
    const body = `<b>${esc(n.id)}</b> — ${esc(dgLong(n.state))}`
      + (n.title ? `<br>${esc(n.title)}` : "")
      + `<br><span class="muted">depends on `
      + `${esc((n.depends_on || []).join(", ") || "nothing")} · `
      + `${esc(n.dependents)} task(s) depend on it`
      + `${n.cyclic ? " · in a dependency cycle" : ""}</span>`;
    el.addEventListener("mousemove", e => showTip(e, body));
    el.addEventListener("mouseleave", hideTip);
    // Keyboard parity with hover, exactly as the pipeline's nodes have.
    el.addEventListener("focus", () => {
      const r = el.getBoundingClientRect();
      showTip({clientX: r.left, clientY: r.bottom - 8}, body);
    });
    el.addEventListener("blur", hideTip);
    // This panel only, and from the payload already drawn: selecting a node is
    // not a reason to rebuild the roadmap under someone, and it is not a reason
    // to let a held graph in either.
    const pick = () => {
      DSEL = DSEL === n.id ? null : n.id;
      updateDeps(DEPDRAWN || LAST, true);
    };
    el.addEventListener("click", pick);
    el.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
  });
}
DEPBIND = bindDeps;

// The held payload lands SOONER than the next tick when a gesture ends inside
// the panel. Polish, not the mechanism: `render` calls `updateDeps` on every
// 2s tick regardless, which is what actually guarantees a held update is never
// lost — including for the gesture these miss (a drag-select that starts in the
// edge table and is released outside the panel). Bound ONCE, on the static
// section, so they do not accumulate per poll. `settle` never forces: if the
// operator is still holding something, `updateDeps` simply holds it again.
const deppanel = document.getElementById("deppanel");
if (deppanel) {
  const settle = () => { if (DEPHELD) updateDeps(DEPHELD); };
  // Deferred by a turn: on `focusout` and `mouseup` the browser has not yet
  // moved `document.activeElement` / collapsed the selection, so an immediate
  // read would still see the gesture that just ended.
  deppanel.addEventListener("focusout", () => setTimeout(settle, 0));
  deppanel.addEventListener("mouseup", () => setTimeout(settle, 0));
  deppanel.addEventListener("mouseleave", settle);
}

function render(d, force){
  if (!d) return;
  // No skeleton flash on refetch: a 2s poll that rebuilt identical DOM threw
  // away hover state and any text selection for nothing. Re-render only when
  // the payload actually changed (or a click forced it). `served_at` is
  // excluded from the signature on purpose — it ticks every poll, so leaving
  // it in would make the guard never fire; its own line is updated below
  // regardless, so "updated HH:MM:SS" still moves.
  // `progress` is excluded for the same reason — its elapsed clock ticks every
  // poll — and is rendered BELOW, before the guard, so the live figures move
  // without rebuilding the rest of the page.
  const {served_at, progress, ...rest} = d;
  const sig = JSON.stringify(rest);
  document.getElementById("served").textContent = `updated ${esc(served_at)}`;
  renderProgress(progress);
  // A stale process serves the old PAGE forever, which looks exactly like a
  // missing feature. Say so instead of letting someone wonder.
  const stale = document.getElementById("stale");
  if (d.build && d.build.stale) {
    stale.style.display = "block";
    stale.textContent = `\u26a0 This page is STALE — dashboard.py changed on disk `
      + `(running ${d.build.running}, on disk ${d.build.on_disk}). `
      + `Restart the dashboard to load it.`;
  } else { stale.style.display = "none"; }

  // ---- the dependency graph ----------------------------------------------
  // ABOVE the page-wide guard, and with no `force` passed, because this panel
  // carries its own: `updateDeps` redraws only when the `depgraph` slice itself
  // changed, and not even then while the operator is holding a node or a
  // selection. Being called on EVERY tick is the point — it is what lets a held
  // payload land within one tick of the gesture ending, with no listener
  // involved. See the rules block above `updateDeps`.
  updateDeps(d);

  if (!force && sig === LASTJSON) return;
  LASTJSON = sig; LAST = d;
  document.getElementById("hdot").style.background = `var(--${d.health.role})`;
  const hi = {good:"●",warning:"◐",serious:"◑",critical:"■"}[d.health.role] || "●";
  document.getElementById("hlabel").textContent =
    `${hi} ${d.health.label}` + (d.health.pids.length ? ` · pid ${d.health.pids.join(", ")}` : "");

  // Inside the change guard, unlike `renderProgress`: nothing here ticks on a
  // clock, so rebuilding it every 2s would throw away text selection for
  // nothing.
  renderStats(d.stats);

  const t = d.task;
  // Completed-but-not-in-the-branch, above the fold. The tile carries the icon
  // and the word as well as the number, so the border colour is decoration.
  // Read here as well as in `renderMerge` on purpose: this tile is the panel's
  // sentence said above the fold, and a number nobody scrolls to says nothing.
  const mc = (d.merge || {}).counts || {};
  const nUnmerged = mc.unmerged || 0;
  document.getElementById("tiles").innerHTML = [
    ["phase", d.session.phase], ["iteration", d.session.iteration],
    ["agents live", d.agents.length], ["open blockers", d.blockers.length],
    ["unit", t.id || "—"], ["candidate", t.candidate || "—"],
    ["not merged", (nUnmerged ? "▲ " : "✓ ") + nUnmerged, nUnmerged ? "warn" : ""],
  ].map(([k,v,cls]) => `<div class="tile ${cls || ""}"><div class="k">${esc(k)}</div>`
      + `<div class="v">${esc(v ?? "—")}</div></div>`).join("");

  drawFlow(d);

  // ---- merged vs unmerged ------------------------------------------------
  // Built by `renderMerge` above, grouped by state — the four rows that need a
  // human are no longer buried under 57 that do not.
  renderMerge(d);

  // ---- roadmap, grouped by state ----------------------------------------
  // Built by `renderRoadmap` above \u2014 the same function the search box calls, so
  // typing a filter cannot produce a different panel from the one a poll
  // builds, and the rows an operator opened survive both. Its comment block
  // carries the rules (state_of, never the status string; priority applied
  // immediately, creation still queued) that used to sit here.
  const groups = d.groups || [];
  renderRoadmap(d);
  // Collapsed, counted, and never carrying a priority input: the Save binding
  // in `renderRoadmap` is scoped to #roadmap, so a button here would render
  // with no listener \u2014 and re-prioritising a finished or retired task means
  // nothing.
  //
  // By KEY, never by scanning for the first group carrying the collapsed flag.
  // With two collapsed groups that predicate hands both disclosures the same
  // one, and the failure is silent: the Done box would quietly fill with
  // retirements. A test asserts the old expression is absent from this script,
  // so do not quote it back into a comment here.
  const byKey = k => groups.find(g => g.key === k);
  const gRetired = byKey("retired");
  document.getElementById("retiredsum").textContent =
    gRetired ? `\u2298 Retired (${gRetired.count})` : "\u2298 Retired";
  // The successor column is the point of this table \u2014 it is the machine
  // readable half of what used to be prose in `blocked_reason`. A retirement
  // with no successor says so in words rather than leaving the cell blank.
  document.getElementById("retired").innerHTML = gRetired
    ? rows(["task","title","superseded by"], gRetired.tasks.map(t =>
        `<tr><td><code>${esc(t.id)}</code></td>
          <td>${esc((t.title||"").slice(0,70))}</td>
          <td class="muted">${(t.superseded_by||[]).length
              ? (t.superseded_by||[]).map(s => `<code>${esc(s)}</code>`).join(", ")
              : esc(t.detail || "no successor recorded")}</td></tr>`).join(""))
    : `<p class="empty">unknown</p>`;
  const gDone = byKey("done");
  document.getElementById("donesum").textContent =
    gDone ? `\u2713 Done (${gDone.count})` : "\u2713 Done";
  document.getElementById("done").innerHTML = gDone
    ? rows(["task","title"], gDone.tasks.map(t =>
        `<tr><td><code>${esc(t.id)}</code></td>
          <td>${esc((t.title||"").slice(0,70))}</td></tr>`).join(""))
    : `<p class="empty">unknown</p>`;

  // A queued creation request carries approved_paths, and the loop merges it
  // without asking again — so the paths are spelled out here rather than
  // counted. Visibility between submit and merge is the mitigation.
  const q = d.inbox || [];
  document.getElementById("queued").textContent = q.length
    ? `${q.length} queued request(s) awaiting the loop: ` +
      q.map(r => r.kind === "priority"
        ? `${r.id} → priority ${r.priority}`
        : `new task ${r.id} (priority ${r.priority ?? 100}) may write: `
          + ((r.approved_paths || []).join(", ") || "nothing — undispatchable")).join(" · ")
    : "no queued requests";

  // which app task is actually being worked
  const activeId = t.id;
  const inRoadmap = new Set((d.roadmap||[]).map(r => r.id));
  document.getElementById("apptasks").innerHTML = rows(["task","pri","state","title"],
    (d.app_tasks||[]).map(a => {
      const r = (d.roadmap||[]).find(x => x.id === a.id);
      let mark = "· not queued", cls = "";
      if (a.id === activeId) { mark = "▶ in progress"; cls = "row-active"; }
      // Retired is checked BEFORE blocked. `collect()` overlays the registry's
      // derived state onto these rows, but a retirement written straight into
      // tasks.json by a future writer would still arrive as its own status —
      // and "■ blocked" on a superseded task is the misread this whole change
      // is about.
      else if (r && r.status === "retired") mark = "⊘ retired";
      else if (r && r.status === "blocked") mark = "■ blocked";
      else if (r && r.status === "completed") mark = "✓ done";
      else if (r) mark = "○ queued";
      return `<tr class="${cls}"><td><code>${esc(a.id)}</code></td><td>${esc(a.priority)}</td>
        <td>${esc(mark)}</td><td class="tt" data-s="${esc(a.title)}">${esc(a.title.slice(0,96))}${a.title.length>96?"…":""}</td></tr>`;
    }).join(""))
    + `<p class="muted" style="font-size:12px;margin:9px 0 0">${(d.app_tasks||[]).length} proposed in ${esc((d.app_tasks[0]||{}).source||"audit report")} · ${inRoadmap.size} in the loop's registry</p>`;
  document.querySelectorAll(".tt").forEach(td => {
    td.addEventListener("mousemove", e => showTip(e, esc(td.dataset.s)));
    td.addEventListener("mouseleave", hideTip);
  });

  document.getElementById("blockers").innerHTML = d.blockers.length
    ? d.blockers.map(b=>`<div style="margin-bottom:9px"><code>${esc(b.id)}</code> · ${esc(b.kind)} · <code>${esc(b.code)}</code><div>${esc(b.question)}</div></div>`).join("")
    : `<p class="empty">none</p>`;

  document.getElementById("events").innerHTML = rows(["time","event","detail"],
    (d.events||[]).slice().reverse().map(e=>`<tr><td><code>${esc(e.ts)}</code></td><td>${esc(e.type)}</td><td class="muted">${esc(e.detail)}</td></tr>`).join(""));

  document.getElementById("git").innerHTML = rows(["ref","sha"],
    [`<tr><td>local <code>${esc(d.git.branch)}</code>${d.git.dirty?` · <span class="muted">${d.git.dirty} dirty</span>`:""}</td><td><code>${esc(d.git.head)}</code></td></tr>`]
      .concat((d.git.remote||[]).map(r=>`<tr><td>origin/${esc(r.ref)}</td><td><code>${esc(r.sha)}</code></td></tr>`)).join(""));
}

// ---- new-task form -----------------------------------------------------
// Bound ONCE, here, because the form is static markup. Inside `render()` it
// would gain a duplicate listener on every 2s poll and double-submit.
//
// Nothing about a path is guessed: the textarea is split on lines, each line
// trimmed, blanks dropped, and what remains is sent verbatim. There is no
// client-side path validator on purpose — `TaskRegistry.add_many` is the single
// authority (`inbox.py`'s docstring commits to that), and a second rule set here
// would drift from it and start refusing paths the registry accepts.
const ntform = document.getElementById("newtask");
const ntnote = document.getElementById("ntnote");

// "Detect paths" FILLS the field; it never submits. Every suggestion arrives
// with the reason it was proposed, appended as a trailing comment so the
// operator can judge each line rather than trusting the list — and so a line
// whose reason does not fit the task is the obvious one to delete. The
// comments are stripped again on submit, since a path is what the registry
// validates.
document.getElementById("ntdetect").addEventListener("click", async () => {
  const el = ntform.elements["approved_paths"];
  ntnote.className = ""; ntnote.textContent = " scanning…";
  try {
    const r = await fetch("/api/suggest-paths", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Autoloop": "1"},
      body: JSON.stringify({title: ntform.elements["title"].value,
                            description: ntform.elements["description"].value}),
    });
    const body = await r.json();
    if (!r.ok) { ntnote.className = "savefail"; ntnote.textContent = " \u2717 " + (body.error || r.status); return; }
    const found = body.suggestions || [];
    if (!found.length) {
      ntnote.className = "savefail";
      ntnote.textContent = " \u2717 nothing detected \u2014 name a file, folder or function in the description";
      return;
    }
    // Doubled, per the rule this file documents a few lines below: PAGE is a
    // plain Python string, so a single escape is decoded HERE and splits the
    // JS literal across two physical lines. That is a SyntaxError, and one
    // syntax error kills the whole script — every dynamic section then renders
    // empty while the static markup still shows, which looks like a dead
    // dashboard rather than a typo (2026-08-04).
    const existing = el.value.split("\\n").map(s => s.split("#")[0].trim()).filter(Boolean);
    const merged = existing.concat(
      found.filter(s => !existing.includes(s.path)).map(s => `${s.path}  # ${s.reason}`));
    el.value = merged.join("\\n");
    ntnote.className = "saved";
    ntnote.textContent = ` \u2713 ${found.length} suggested \u2014 check each line, then queue`;
  } catch (err) { ntnote.className = "savefail"; ntnote.textContent = " \u2717 " + err; }
});
ntform.addEventListener("submit", async e => {
  e.preventDefault();
  const val = n => String(ntform.elements[n].value || "");
  // One path per line, then trim each: a textarea entry can arrive CRLF
  // terminated, and a stray carriage return would be refused by the registry as
  // trailing whitespace, with an error pointing at an invisible character.
  // (The escape is doubled because PAGE is a plain Python string, not a raw
  // one — a single one would be decoded here and break the JS literal.)
  // Split off any "  # reason" the detector appended: the operator reads those,
  // the registry validates paths.
  const paths = val("approved_paths").split("\\n")
    .map(s => s.split("#")[0].trim()).filter(Boolean);
  const priority = parseInt(val("priority"), 10);
  const fail = msg => { ntnote.className = "savefail"; ntnote.textContent = " ✗ " + msg; };
  if (!Number.isInteger(priority)) return fail("priority must be a whole number");
  // A UI precondition, not validation: a task with no approved paths merges
  // fine and can then never be dispatched, because an empty scope is how
  // "nothing authorized yet" is spelled.
  if (!paths.length) return fail("name at least one approved path");
  const btn = ntform.querySelector("button[type=submit]");
  btn.disabled = true; ntnote.className = ""; ntnote.textContent = " …";
  try {
    const r = await fetch("/api/task", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Autoloop": "1"},
      body: JSON.stringify({id: val("id").trim(), title: val("title").trim(),
                            description: val("description").trim(),
                            priority, approved_paths: paths}),
    });
    const body = await r.json();
    if (r.ok) {
      ntnote.className = "saved";
      ntnote.textContent = ` ✓ queued ${body.queued} — ${paths.length} approved path(s)`;
      ntform.reset();
    } else { fail(body.error || r.status); }
  } catch (err) { fail(err); }
  finally { btn.disabled = false; LASTJSON = null; }
});

// ---- roadmap search + expand-all ---------------------------------------
// Bound ONCE, here, because both controls are static markup — inside
// `renderRoadmap` they would gain a duplicate listener on every 2s poll.
//
// The filter re-renders from LAST rather than refetching: it is a view of the
// payload already on the page, so typing must not wait on a round trip, and
// `renderRoadmap` reads the field's current value on every poll so a search
// survives the refresh instead of being wiped by it.
document.getElementById("rmq").addEventListener("input", () => {
  if (LAST) renderRoadmap(LAST);
});
document.getElementById("rmexpand").addEventListener("click", () => {
  const btn = document.getElementById("rmexpand");
  const els = [...document.querySelectorAll("#roadmap details.task")];
  // Open everything unless everything is already open, then collapse — one
  // button, and its label says which it will do next.
  const open = els.some(el => !el.open);
  els.forEach(el => {
    el.open = open;
    if (open) RMOPEN.add(el.dataset.id); else RMOPEN.delete(el.dataset.id);
  });
  btn.textContent = open ? "Collapse all" : "Expand all";
});

// Theme toggle. The stamp goes on <html>, which both dark scopes key off, so
// an explicit choice beats the OS setting in BOTH directions.
const tog = document.getElementById("themetog");
const applyTheme = t => {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
  tog.textContent = t === "dark" ? "◑ dark" : t === "light" ? "◐ light" : "◐ theme";
};
applyTheme(localStorage.getItem("al-theme"));
tog.addEventListener("click", () => {
  const now = document.documentElement.getAttribute("data-theme");
  const next = now === "dark" ? "light" : now === "light" ? "" : "dark";
  next ? localStorage.setItem("al-theme", next) : localStorage.removeItem("al-theme");
  applyTheme(next);
});

async function tick(){ try { render(await (await fetch("/api/state")).json()); } catch {} }
tick(); setInterval(tick, 2000);
</script>
"""


#: Fields `/api/task` accepts. Deliberately NARROWER than the inbox's own
#: `ALLOWED_FIELDS`: `depends_on`, `validation` and `validation_cwd` are not on
#: the form, so a request carrying one did not come from this page. Refused
#: rather than dropped, for the reason `inbox.ALLOWED_FIELDS` gives — a request
#: naming a field the receiver ignores has not done what its author intended.
TASK_REQUEST_FIELDS = frozenset({"id", "title", "description", "priority", "approved_paths"})

#: Fields `/api/priority` accepts — an existing task's id and a number, and
#: that is the whole vocabulary. It is the same set `inbox.MUTATION_PAYLOAD`
#: allows a queued `priority` request (`{kind, id, priority}` minus the kind
#: discriminator, which this endpoint has no use for), spelled here because
#: this route no longer passes through the inbox's shape gate on its way to the
#: registry. Anything else is refused rather than dropped: a caller that sent
#: `approved_paths` here meant something this endpoint must never do.
PRIORITY_REQUEST_FIELDS = frozenset({"id", "priority"})


class Handler(BaseHTTPRequestHandler):
    repo = Path(".")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        """The write paths. There are two, and they are deliberately unequal.

        `/api/task` (creation) and `/api/suggest-paths` still touch nothing the
        loop observes: a creation request goes to the inbox, which lives
        OUTSIDE the checkout, and is merged by the loop between steps.

        `/api/priority` writes `.autoloop/tasks.json` IMMEDIATELY (2026-08-16).
        That is a real departure from the read-only posture this file was
        written around, and it is bounded rather than assumed safe:

          * ONE field, on an existing task. `TaskStore.apply_priority` cannot
            create the registry, add or remove a task, or reach `status`,
            `approved_paths` or `depends_on`.
          * It takes the fine-grained task-file mutex, never `LoopLock` (held
            for the whole run), and holds it for the milliseconds of
            load/mutate/save. The loop's own saves take the same mutex, so
            neither side can lose the other's update.
          * Nothing else in the state dir is written: not `state.json`, not an
            execution record, not a blocker.
          * The write is attested in a ledger BESIDE `workers_root`, outside
            the checkout, which is what lets the loop's escape detector tell an
            operator priority edit from an agent writing into the checkout —
            see `tasks.MutationLedger` and
            `orchestrator._operator_priority_exemption`. Without it a routine
            priority edit during a round would park the loop loop-fatal, which
            would make steering the loop a way to stop it. Two records per
            write: an INTENT before it (so a ledger that cannot be appended to
            leaves `tasks.json` untouched, and this endpoint reports the failure
            rather than showing a value that never landed) and a COMPLETE after
            it (the only one that ever silences the detector).

        Everything else this page does is still reads.

        The page has no authentication and binds 127.0.0.1 only, so the residual
        risk is a *local* page in the same browser posting here. Two cheap
        mitigations, neither claimed to be more than that: a custom header,
        which a cross-origin form post cannot set without a CORS preflight this
        server never approves; and an Origin check when one is present.

        **The blast radius is no longer just a priority.** `/api/priority` still
        cannot express anything but a number against an existing id — now
        applied rather than queued, which changes WHEN it takes effect and not
        what it can say. But `/api/task` queues a CREATION request, and a new
        task carries
        `approved_paths` — the scope a write-capable agent is later authorized
        against. So a local page that can reach this port can queue a task
        naming paths nobody typed, and the loop merges the inbox without asking
        again. What bounds it, honestly stated:

          * `TaskRegistry.add_many` validates every path on merge (exact
            repository-relative paths, no globs, no '..', no absolute paths),
            and `orchestrator` re-checks symlink traversal at dispatch — so the
            paths are well-formed, not that they are *wanted*.
          * A queued request is visible as text on this page (`_pending_inbox`
            carries the paths) and in the loop's drain output before anything
            runs against it.
          * It creates a task; THIS endpoint cannot widen an existing one.
            `TASK_REQUEST_FIELDS` carries no `kind`, so every request it
            queues is a creation. (The inbox FILE format has carried an
            `approved_paths` mutation kind since 2026-08-16 — see
            `docs/SECURITY.md` S30 — but nothing here can reach it, and
            wiring a route to it is the change S30 names as the one that
            would make that finding materially worse.)

        That is a real widening over the read-only tracker, and it is recorded
        as such in `docs/SECURITY.md` rather than left implied.
        """
        # Drain the body BEFORE any refusal. Each response closes the
        # connection, and closing one with unread data still in the receive
        # buffer makes the OS send an RST — which can discard the 403 that was
        # just written, so the caller sees a connection error instead of the
        # reason it was refused.
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
        except (ValueError, OSError) as exc:
            return self._json_response(400, {"error": f"bad request: {exc}"})
        if self.headers.get("X-Autoloop") != "1":
            return self._json_response(403, {"error": "missing X-Autoloop header"})
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            return self._json_response(403, {"error": "cross-origin refused"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            return self._json_response(400, {"error": f"bad request: {exc}"})
        if self.path.startswith("/api/priority"):
            return self._submit_priority(body)
        if self.path.startswith("/api/suggest-paths"):
            return self._suggest_paths(body)
        if self.path.startswith("/api/task"):
            return self._submit_task(body)
        return self._json_response(404, {"error": "unknown endpoint"})

    def _suggest_paths(self, body: dict) -> None:
        """Propose the paths a task probably touches. READ-ONLY.

        It queues nothing, and that is the point rather than an omission.
        `approved_paths` is authorization, and `docs/SECURITY.md` finding #2
        exists because the executor's own report must never define its own
        scope — so this fills a form field the operator then reads and
        submits, and the submit is still the only thing that queues anything.
        A suggestion the operator does not send has authorized nothing.
        """
        from .path_suggest import suggest

        text = " ".join(
            str(body.get(k) or "") for k in ("title", "description")
        ).strip()
        if not text:
            return self._json_response(400, {"error": "nothing to read: title/description empty"})
        try:
            found = suggest(text, self.repo)
        except OSError as exc:
            return self._json_response(500, {"error": f"could not scan the repo: {exc}"})
        return self._json_response(200, {"suggestions": [s.as_dict() for s in found]})

    def _submit_priority(self, body: dict) -> None:
        """Apply a priority IMMEDIATELY, and report what is now on disk.

        Not queued. An operator sets a priority to steer what the loop does
        next, and a value that lands minutes later — whenever the loop happens
        to drain the inbox between steps — has already missed the decision it
        was for. Worse, the page kept re-rendering from `tasks.json` in the
        meantime, so a save that worked and a save that did not looked exactly
        alike and the operator resubmitted (two such resubmissions sat in the
        inbox on 2026-08-05).

        No `LoopLock` is taken — it is held for the whole run, so waiting for it
        would mean waiting for the loop to stop. `TaskStore.apply_priority`
        takes the fine-grained mutex instead, for the milliseconds of
        load/mutate/save, and every writer of that file takes the same one.

        THE RESPONSE CARRIES A READ-BACK, never the number that was posted:
        `apply_priority` re-reads the file after writing it and returns the
        stored task, so `priority` here is what a fresh reader would see. That
        is the whole point — echoing the input would report success identically
        whether or not anything reached the disk.

        Still the narrowest possible write: one integer field on one existing
        task. It cannot create a registry, cannot add or remove a task, and
        cannot touch `status`, `approved_paths` or `depends_on`. Creation stays
        queued through the inbox (`/api/task`), because a new task carries
        authorization and the loop merging it is the review step.
        """
        from .errors import StateError, TaskGraphError

        unknown = sorted(set(body) - PRIORITY_REQUEST_FIELDS)
        if unknown:
            # Refused, never ignored. The queued route was gated by
            # `inbox.check_request_shape`, which refuses a `priority` request
            # carrying anything else; applying directly must not become the lax
            # door into the same field set, so the same rule is spelled here
            # for the same reason it is there — a request naming a field the
            # receiver drops has not done what its author intended.
            return self._json_response(
                400,
                {"error": f"unknown field(s) {unknown}; a priority edit carries "
                          f"only {sorted(PRIORITY_REQUEST_FIELDS)}"},
            )
        try:
            task_id = str(body["id"])
            priority = int(body["priority"])
        except (ValueError, KeyError, TypeError) as exc:
            return self._json_response(400, {"error": f"bad request: {exc}"})
        try:
            task = _task_store(self.repo).apply_priority(task_id, priority)
        except TaskGraphError as exc:
            # An unknown id or a non-integer priority, in the registry's own
            # words — the same authority a queued request would have been
            # refused by, just reported now instead of in a drain log later.
            return self._json_response(400, {"error": str(exc)})
        except (StateError, OSError) as exc:
            # A failed write REPORTS. The row must not snap back to the old
            # value with nothing said: that is indistinguishable from the bug
            # this endpoint exists to fix.
            return self._json_response(500, {"error": f"could not apply: {exc}"})
        return self._json_response(
            200,
            {
                "id": task.id,
                "priority": task.priority,
                "applied": True,
                "note": "written to tasks.json and read back",
            },
        )

    def _submit_task(self, body: dict) -> None:
        """Queue a new task through the same inbox `add-task` uses.

        No field is invented here. `approved_paths` arrives as the operator
        typed it — split into lines and stripped by the page, forwarded
        verbatim — and this deliberately runs NO path validator of its own:
        `TaskRegistry.add_many` is the single authority on merge (the same gate
        a ChatGPT `plan` passes), and a second rule set here would drift from it
        and start refusing paths the registry accepts, or worse, accepting ones
        it does not.
        """
        unknown = sorted(set(body) - TASK_REQUEST_FIELDS)
        if unknown:
            return self._json_response(
                400,
                {"error": f"unknown field(s) {unknown}; allowed: {sorted(TASK_REQUEST_FIELDS)}"},
            )
        try:
            raw_paths = body.get("approved_paths") or []
            if not isinstance(raw_paths, list):
                raise TypeError("approved_paths must be a list of strings")
            paths = [p for p in (str(x).strip() for x in raw_paths) if p]
            spec = {
                "kind": "task",
                "id": str(body["id"]).strip(),
                "title": str(body["title"]).strip(),
                "description": str(body["description"]).strip(),
                "priority": int(body.get("priority", 100)),
            }
        except (KeyError, TypeError, ValueError) as exc:
            return self._json_response(400, {"error": f"bad request: {exc}"})
        if not paths:
            # A UI precondition rather than validation: the registry accepts an
            # empty scope, and `effective_approved_paths` then keeps returning
            # (), so the orchestrator refuses to dispatch the task forever. A
            # form that queues an undispatchable task is a trap, so say it here.
            return self._json_response(
                400,
                {"error": "approved_paths: name at least one path — a task with no "
                          "authorized scope can never be dispatched"},
            )
        spec["approved_paths"] = paths
        return self._queue(
            lambda inbox: inbox.submit(spec),
            {"queued": spec["id"], "priority": spec["priority"], "approved_paths": paths},
        )

    def _queue(self, submit, payload: dict) -> None:
        """Run one inbox submit and turn its failures into responses.

        Creation only, since 2026-08-16: `/api/priority` no longer queues
        anything, so this is the task-creation path's error handling. Kept as
        its own method rather than inlined — the inbox's two failure modes
        (a refused shape, an unwritable directory) map to different status
        codes, and that mapping should live in one place if a second queued
        endpoint ever appears.
        """
        from .inbox import InboxError, TaskInbox

        try:
            path = submit(TaskInbox(_inbox_dir(self.repo)))
        except InboxError as exc:
            return self._json_response(400, {"error": str(exc)})
        except OSError as exc:
            return self._json_response(500, {"error": f"could not queue: {exc}"})
        return self._json_response(
            200,
            {**payload, "file": path.name, "note": "applied by the loop on its next run"},
        )

    def _json_response(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.startswith("/api/state"):
            payload = json.dumps(collect(self.repo)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # a poll every 2s would bury the terminal


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autoloop-dashboard")
    ap.add_argument("--repo", type=Path, default=Path.cwd(),
                    help="checkout whose .autoloop/ to read (default: cwd)")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    if not (repo / ".autoloop").is_dir():
        print(f"no .autoloop/ under {repo} — pass --repo")
        return 1
    Handler.repo = repo
    # 127.0.0.1, never 0.0.0.0: this exposes repo paths, branch names and
    # blocker questions, and has no authentication of any kind.
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"autoloop dashboard → http://127.0.0.1:{args.port}  (read-only, reading {repo})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
