"""Read-only live tracker for a running Autoloop, served on localhost.

    python -m autoloop.dashboard --repo /path/to/checkout --port 8787

Why this exists: the audit fans out six headless `claude -p` subagents whose
output is captured only when each one FINISHES. From outside, a working loop and
a wedged loop look identical — an empty `raw/` directory and a silent stdout.
This reads the state the loop already writes, plus the process table, and shows
what is happening now.

**It never writes.** Not to `.autoloop/`, not to the repo, not to git. That is
not politeness: the loop's escape detector requires the primary checkout to be
clean before every write-capable agent invocation, so a tracker that touched the
working tree would make the next task refuse. Everything here is `read_text`,
`glob`, and `ps`.

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


def _run(args, cwd=None, timeout=8):
    # `git status` refreshes the index and rewrites `.git/index` as a side
    # effect, so a "read-only" tracker polling every 2s was in fact writing to
    # the repository it observes. `--no-optional-locks` exists for exactly this
    # case: it tells git not to take the index lock or write refreshed state.
    if args and args[0] == "git":
        args = ["git", "--no-optional-locks", *args[1:]]
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except (subprocess.SubprocessError, OSError):
        return ""


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
    out, seen = [], set()
    for tid, prio, title in _RT_ROW.findall(text):
        if tid in seen:
            continue
        seen.add(tid)
        clean = re.sub(r"[*`]", "", title)
        clean = re.sub(r"\s+", " ", clean).strip()
        out.append({"id": tid, "priority": prio, "title": clean[:180],
                    "source": reports[0].name})
        if len(out) >= limit:
            break
    return out



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
    until the loop next runs."""
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
    pids = [p for p in _run(["pgrep", "-f", "autoloop run --continuous"]).split() if p]
    lock = _json(sd / "lock.json") or {}
    lock_pid = str(lock.get("pid", "")) if lock else ""
    lock_alive = bool(lock_pid) and bool(_run(["ps", "-p", lock_pid]))

    if pids:
        health = ("good", "running")
    elif lock and not lock_alive:
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

function render(d, force){
  if (!d) return;
  // No skeleton flash on refetch: a 2s poll that rebuilt identical DOM threw
  // away hover state and any text selection for nothing. Re-render only when
  // the payload actually changed (or a click forced it). `served_at` is
  // excluded from the signature on purpose — it ticks every poll, so leaving
  // it in would make the guard never fire; its own line is updated below
  // regardless, so "updated HH:MM:SS" still moves.
  const {served_at, ...rest} = d;
  const sig = JSON.stringify(rest);
  document.getElementById("served").textContent = `updated ${esc(served_at)}`;
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

  document.querySelectorAll("button.save").forEach(btn => {
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

  const q = d.inbox || [];
  document.getElementById("queued").textContent = q.length
    ? `${q.length} queued request(s) awaiting the loop: ` +
      q.map(r => r.kind === "priority" ? `${r.id} → priority ${r.priority}` : `new task ${r.id}`).join(", ")
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
        server never approves; and an Origin check when one is present. The
        blast radius is bounded by what the endpoint can express — a task
        priority. It cannot change `approved_paths`, so it cannot widen what any
        agent may touch.
        """
        if self.headers.get("X-Autoloop") != "1":
            return self._json_response(403, {"error": "missing X-Autoloop header"})
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(("http://127.0.0.1", "http://localhost")):
            return self._json_response(403, {"error": "cross-origin refused"})
        if not self.path.startswith("/api/priority"):
            return self._json_response(404, {"error": "unknown endpoint"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            task_id = str(body["id"])
            priority = int(body["priority"])
        except (ValueError, KeyError, TypeError) as exc:
            return self._json_response(400, {"error": f"bad request: {exc}"})

        from .inbox import InboxError, TaskInbox

        try:
            path = TaskInbox(_inbox_dir(self.repo)).submit_priority(task_id, priority)
        except InboxError as exc:
            return self._json_response(400, {"error": str(exc)})
        except OSError as exc:
            return self._json_response(500, {"error": f"could not queue: {exc}"})
        return self._json_response(
            200,
            {
                "queued": task_id,
                "priority": priority,
                "file": path.name,
                "note": "applied by the loop on its next run",
            },
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
