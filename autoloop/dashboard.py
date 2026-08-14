"""Read-only live tracker for a running Autoloop, served on localhost.

    python -m autoloop.dashboard --repo /path/to/checkout --port 8787

Why this exists: the audit fans out six headless `claude -p` subagents whose
output is captured only when each one FINISHES. From outside, a working loop and
a wedged loop look identical — an empty `raw/` directory and a silent stdout.
This reads the state the loop already writes, plus the process table, and shows
what is happening now.

**It never writes to what it observes.** Not to `.autoloop/`, not to the repo,
not to git. That is not politeness: the loop's escape detector requires the
primary checkout to be clean before every write-capable agent invocation, so a
tracker that touched the working tree would make the next task refuse. Reading
is `read_text`, `glob`, and `ps`.

There IS one write path, and it deliberately points elsewhere: `Handler.do_POST`
queues operator requests — a task priority, or a new task — into the `TaskInbox`
directory OUTSIDE the checkout, which the loop drains between steps. So a submit
is safe at any instant, including mid-agent, and the loop stays the only writer
of `tasks.json`. A new task carries `approved_paths`; read `do_POST`'s docstring
and `docs/SECURITY.md` S28 before widening what that endpoint accepts.

The one thing it learns that is not already on disk: which domain each in-flight
agent is on, parsed from the process table. Agent prompts carry
`Your domain: <title>.`, so a live fan-out is legible without changing the loop.
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
_REMOTE_CACHE: dict = {"at": 0.0, "refs": []}


def _run_checked(args, cwd=None, timeout=8) -> str | None:
    """Run a command, returning `None` when it FAILED and its stdout when it did
    not — so an empty success and a failure are two different answers.

    `_run` below collapses both to `""`, which is fine wherever empty means
    "nothing to show". It is a lie for the progress figures: an unreadable
    worker repo would render as zero lines changed, and zero is precisely the
    alarming state ("the agent has written nothing"), invented out of a
    directory we could not read.
    """
    # `git status` refreshes the index and rewrites `.git/index` as a side
    # effect, so a "read-only" tracker polling every 2s was in fact writing to
    # the repository it observes. `--no-optional-locks` exists for exactly this
    # case: it tells git not to take the index lock or write refreshed state.
    # It costs nothing in accuracy: git still compares CONTENT for entries whose
    # stat data looks dirty; what it skips is writing the refreshed index back.
    # Never drop it to "speed up" a call — every git read here runs against a
    # repo the loop is actively writing.
    if args and args[0] == "git":
        args = ["git", "--no-optional-locks", *args[1:]]
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None
    return p.stdout if p.returncode == 0 else None


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


def app_tasks(repo: Path, limit: int = 40) -> list[dict]:
    """The language-app backlog, read from the newest committed audit report.

    The loop's own registry holds only what has been seeded; the audit report is
    where the proposed work actually lives, so a tracker that showed the
    registry alone would claim there is one task when there are fifteen.
    """
    reports = sorted((repo / "docs").glob("AUDIT_*.md"), reverse=True)
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


def _inbox_dir(repo: Path) -> Path:
    """Where task requests go. Resolved through the loop's own config so the
    dashboard and `add-task` always agree, with the documented default as the
    fallback for a dashboard pointed at a checkout that has no config yet."""
    try:
        import tomllib

        data = tomllib.loads((repo / ".autoloop" / "config.toml").read_text(encoding="utf-8"))
        workers_root = (data.get("paths") or {}).get("workers_root")
        if workers_root:
            return Path(workers_root).expanduser().parent / "inbox"
    except (OSError, ValueError, KeyError):
        pass
    return Path.home() / ".autoloop" / "inbox"


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
         "state": st(phase in ("submitting", "awaiting"), done=bool(decision))},
        {"key": "publisher", "label": "Publisher", "detail": "exact-SHA push",
         "state": st(decision == "push" and phase == "executing")},
    ]


def collect(repo: Path) -> dict:
    sd = repo / ".autoloop"
    state = _json(sd / "state.json") or {}
    # `tasks.json` only exists once a registry has been saved; before that the
    # CLI seeds from the tracked file. Reading only the former showed an empty
    # roadmap while `next-task` correctly reported rt-01.
    tasks = _json(sd / "tasks.json") or {}
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
            # Ascending: 1 outranks 2, default 100 sorts last (tasks.Task.priority).
            "priority": t.get("priority", 100),
        })
    roadmap.sort(key=lambda r: (r.get("priority", 100), r.get("id") or ""))

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
    now = time.time()
    if now - _REMOTE_CACHE["at"] > 60:
        refs = []
        for line in _run(["git", "ls-remote", "--heads", "origin"], cwd=repo, timeout=15).splitlines():
            sha, _, ref = line.partition("\t")
            if ref:
                refs.append({"ref": ref.replace("refs/heads/", ""), "sha": sha[:12]})
        _REMOTE_CACHE.update(at=now, refs=refs)

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
        "inbox": _pending_inbox(repo),
        "app_tasks": app_tasks(repo),
        "pipeline": pipeline(state, live_agents_cache, blockers),
        "git": {"branch": branch, "head": head[:12], "dirty": dirty, "remote": _REMOTE_CACHE["refs"]},
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
      --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
/* Dark is SELECTED, not flipped: its own steps, validated against #2a2a27.
   Declared under both scopes so the toggle wins either way — the :not() guard
   lets an explicit light stamp beat OS-dark. */
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
      --surface:#0d0d0d;--card:#1a1a19;--soft:#2a2a27;
      --ink:#fff;--ink2:#c3c2b7;--muted:#898781;--line:#2c2c2a;--axis:#383835;
      --mark-active:#3987e5;--mark-done:#c3c2b7;--mark-idle:#383835}}
:root[data-theme="dark"]{color-scheme:dark;
      --surface:#0d0d0d;--card:#1a1a19;--soft:#2a2a27;
      --ink:#fff;--ink2:#c3c2b7;--muted:#898781;--line:#2c2c2a;--axis:#383835;
      --mark-active:#3987e5;--mark-done:#c3c2b7;--mark-idle:#383835}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--ink);font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 72px}
header{display:flex;align-items:center;gap:12px;margin-bottom:18px;flex-wrap:wrap}
h1{font-size:15px;margin:0;letter-spacing:.02em}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--ink2);margin:0 0 10px}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto}
.pill{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:4px 11px;font-size:12.5px;background:var(--card)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.k{font-size:11px;color:var(--ink2);text-transform:uppercase;letter-spacing:.06em}
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
details{margin-top:10px}
summary{font-size:12px;color:var(--ink2);cursor:pointer}
summary:hover{color:var(--ink)}
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

  <section>
    <h2>Roadmap — set priority, then Save</h2>
    <div id="roadmap" class="scroll"></div>
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
      reported, not silently accepted here. The doc trackers
      (<code>docs/SUMMARY.md</code>, <code>docs/TESTS.md</code>,
      <code>docs/SECURITY.md</code>, <code>docs/COMMON_ERRORS.md</code>) are
      always allowed and need not be listed.</p>
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
  if (!force && sig === LASTJSON) return;
  LASTJSON = sig; LAST = d;
  document.getElementById("hdot").style.background = `var(--${d.health.role})`;
  const hi = {good:"●",warning:"◐",serious:"◑",critical:"■"}[d.health.role] || "●";
  document.getElementById("hlabel").textContent =
    `${hi} ${d.health.label}` + (d.health.pids.length ? ` · pid ${d.health.pids.join(", ")}` : "");

  const t = d.task;
  document.getElementById("tiles").innerHTML = [
    ["phase", d.session.phase], ["iteration", d.session.iteration],
    ["agents live", d.agents.length], ["open blockers", d.blockers.length],
    ["unit", t.id || "—"], ["candidate", t.candidate || "—"],
  ].map(([k,v]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${esc(v ?? "—")}</div></div>`).join("");

  drawFlow(d);

  // ---- roadmap priority editor ------------------------------------------
  // Writes go to the task INBOX, never to tasks.json: the loop is the only
  // registry writer, and a write into .autoloop/ mid-run would trip the
  // escape detector. So a Save is "queued", applied on the loop's next run.
  const RS = {completed:["\u2713","done"],in_progress:["\u25b6","in progress"],
              blocked:["\u25a0","blocked"],blocked_by_operator:["\u25a0","quarantined"],
              pending:["\u25cb","queued"]};
  document.getElementById("roadmap").innerHTML = rows(["task","priority","state","title"],
    (d.roadmap||[]).map(t => {
      const [ic,word] = RS[t.status] || RS.pending;
      return `<tr><td><code>${esc(t.id)}</code></td>
        <td><input class="pri" type="number" step="1" value="${esc(t.priority)}"
             data-id="${esc(t.id)}" aria-label="priority for ${esc(t.id)}">
            <button class="save" data-id="${esc(t.id)}">Save</button>
            <span class="note" data-id="${esc(t.id)}"></span></td>
        <td>${esc(ic)} ${esc(word)}</td>
        <td>${esc((t.title||"").slice(0,80))}</td></tr>`;
    }).join(""))
    + `<p class="muted" style="font-size:12px;margin:9px 0 0">Lower number runs first; 100 is the default. Saved changes queue in the inbox and apply on the loop's next run.</p>`;

  // SCOPED to #roadmap on purpose. These rows are rebuilt as fresh DOM on every
  // render, so rebinding them each time is free — but the new-task form's
  // button shares the `save` class for styling and is STATIC, so an unscoped
  // `button.save` would hand it one extra click listener per poll, each looking
  // up an `input.pri[data-id="undefined"]` that does not exist.
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
        if (r.ok) { note.className = "note saved"; note.textContent = " ✓ queued"; }
        else { note.className = "note savefail"; note.textContent = " ✗ " + (body.error || r.status); }
      } catch (e) {
        note.className = "note savefail"; note.textContent = " ✗ " + e;
      } finally { btn.disabled = false; LASTJSON = null; }
    });
  });

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


class Handler(BaseHTTPRequestHandler):
    repo = Path(".")

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
        """The ONE write path, and it writes only to the task inbox.

        The inbox lives OUTSIDE the checkout, so this does not touch the repo,
        `.autoloop/`, or git — the read-only property the rest of this file
        depends on is unchanged, and a submit is safe while an agent is running
        (a write into the state dir would trip the escape detector and park the
        loop loop-fatal).

        The page has no authentication and binds 127.0.0.1 only, so the residual
        risk is a *local* page in the same browser posting here. Two cheap
        mitigations, neither claimed to be more than that: a custom header,
        which a cross-origin form post cannot set without a CORS preflight this
        server never approves; and an Origin check when one is present.

        **The blast radius is no longer just a priority.** `/api/priority` still
        cannot express anything but a number against an existing id. But
        `/api/task` queues a CREATION request, and a new task carries
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
          * It creates a task; it cannot widen an EXISTING one. There is
            deliberately no "edit approved_paths" request kind.

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
        try:
            task_id = str(body["id"])
            priority = int(body["priority"])
        except (ValueError, KeyError, TypeError) as exc:
            return self._json_response(400, {"error": f"bad request: {exc}"})
        return self._queue(
            lambda inbox: inbox.submit_priority(task_id, priority),
            {"queued": task_id, "priority": priority},
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
        """Run one inbox submit and turn its failures into responses. Shared so
        both endpoints report a refusal identically."""
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
