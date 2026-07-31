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
import json
import re
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

#: Reserved status roles (dataviz palette). Never reused for anything else, and
#: every use in the page ships an icon + label so state is never colour-alone.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

_DOMAIN = re.compile(r"Your domain:\s*(.+?)\.")
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
        })

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
        "app_tasks": app_tasks(repo),
        "pipeline": pipeline(state, live_agents_cache, blockers),
        "git": {"branch": branch, "head": head[:12], "dirty": dirty, "remote": _REMOTE_CACHE["refs"]},
        "served_at": time.strftime("%H:%M:%S"),
    }


PAGE = """<!doctype html>
<meta charset="utf-8"><title>Autoloop — live</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--surface:#fcfcfb;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--line:#e5e4e0;--soft:#f4f3f0;
      --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root{--surface:#1a1a19;--card:#222220;--ink:#fff;--ink2:#c3c2b7;--line:#34332f;--soft:#2a2a27}}
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
.v{font-size:18px;margin-top:2px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:5px 8px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;color:var(--ink2);font-weight:500;text-transform:uppercase;letter-spacing:.05em}
tr:last-child td{border-bottom:0}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--ink2);overflow-wrap:anywhere}
.muted{color:var(--ink2)}.empty{color:var(--ink2);font-style:italic;font-size:13px}
.scroll{overflow-x:auto}
/* pipeline */
#flow{width:100%;height:132px;display:block}
.node rect{fill:var(--soft);stroke:var(--line);stroke-width:1;rx:8;cursor:pointer}
.node.sel rect{stroke:var(--ink2);stroke-width:2}
.node text{font-size:11.5px;fill:var(--ink);pointer-events:none}
.node .sub{font-size:10px;fill:var(--ink2)}
.edge{stroke:var(--line);stroke-width:2;fill:none;marker-end:url(#ar)}
.edge.on{stroke:var(--ink2)}
.badge{font-size:10px;fill:var(--ink2)}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .12s;background:var(--card);
     border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:12.5px;max-width:330px;
     box-shadow:0 6px 22px rgba(0,0,0,.13);z-index:9}
.row-active{background:var(--soft)}
</style>
<div class="wrap">
  <header>
    <h1>AUTOLOOP</h1>
    <span class="pill"><span class="dot" id="hdot"></span><span id="hlabel">…</span></span>
    <span class="muted" style="font-size:12px" id="served"></span>
  </header>

  <div class="grid" id="tiles"></div>

  <section>
    <h2>Pipeline — hover for detail, click to inspect</h2>
    <svg id="flow" viewBox="0 0 1140 132" preserveAspectRatio="xMidYMid meet">
      <defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="var(--line)"/></marker></defs>
      <g id="edges"></g><g id="nodes"></g>
    </svg>
    <div id="detail" class="muted" style="font-size:12.5px;margin-top:6px"></div>
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
// state -> icon + word. Colour never carries meaning on its own.
const MARK = {active:["▶","running"],done:["✓","done"],blocked:["■","blocked"],idle:["·","idle"]};
const FILL = {active:"var(--good)",done:"var(--ink2)",blocked:"var(--critical)",idle:"var(--line)"};
let SEL = null, LAST = null;

const tip = document.getElementById("tip");
function showTip(e, html){ tip.innerHTML = html; tip.style.opacity = 1;
  const x = Math.min(e.clientX + 14, innerWidth - 346); tip.style.left = x + "px";
  tip.style.top = Math.min(e.clientY + 14, innerHeight - 120) + "px"; }
function hideTip(){ tip.style.opacity = 0; }

function drawFlow(d){
  const N = d.pipeline, W = 1140, w = 158, h = 54, gap = (W - N.length*w) / (N.length - 1), y = 30;
  const edges = [], nodes = [];
  N.forEach((n, i) => {
    const x = i * (w + gap);
    if (i) { const px = (i-1)*(w+gap)+w, on = N[i-1].state==="done" || N[i-1].state==="active";
      edges.push(`<path class="edge${on?" on":""}" d="M${px+4},${y+h/2} L${x-6},${y+h/2}"/>`); }
    const [ic, word] = MARK[n.state] || MARK.idle;
    nodes.push(`<g class="node${SEL===n.key?" sel":""}" data-k="${esc(n.key)}" transform="translate(${x},${y})">
      <rect width="${w}" height="${h}"/>
      <circle cx="13" cy="15" r="4" fill="${FILL[n.state]}"/>
      <text x="24" y="19">${esc(n.label)}</text>
      <text class="sub" x="11" y="36">${esc(ic)} ${esc(word)}</text>
      <text class="sub" x="11" y="49">${esc((n.detail||"").slice(0,24))}</text></g>`);
  });
  document.getElementById("edges").innerHTML = edges.join("");
  document.getElementById("nodes").innerHTML = nodes.join("");
  document.querySelectorAll("#nodes .node").forEach(g => {
    const n = N.find(x => x.key === g.dataset.k);
    g.addEventListener("mousemove", e => showTip(e,
      `<b>${esc(n.label)}</b> — ${esc((MARK[n.state]||MARK.idle)[1])}<br><span class="muted">${esc(n.detail)}</span>`));
    g.addEventListener("mouseleave", hideTip);
    g.addEventListener("click", () => { SEL = SEL === n.key ? null : n.key; render(LAST); });
  });
  document.getElementById("detail").textContent = SEL
    ? `${SEL}: ${(N.find(x=>x.key===SEL)||{}).detail || ""}`
    : "no stage selected";
}

function render(d){
  if (!d) return; LAST = d;
  document.getElementById("hdot").style.background = `var(--${d.health.role})`;
  const hi = {good:"●",warning:"◐",serious:"◑",critical:"■"}[d.health.role] || "●";
  document.getElementById("hlabel").textContent =
    `${hi} ${d.health.label}` + (d.health.pids.length ? ` · pid ${d.health.pids.join(", ")}` : "");
  document.getElementById("served").textContent = `updated ${esc(d.served_at)}`;

  const t = d.task;
  document.getElementById("tiles").innerHTML = [
    ["phase", d.session.phase], ["iteration", d.session.iteration],
    ["agents live", d.agents.length], ["open blockers", d.blockers.length],
    ["unit", t.id || "—"], ["candidate", t.candidate || "—"],
  ].map(([k,v]) => `<div class="tile"><div class="k">${esc(k)}</div><div class="v">${esc(v ?? "—")}</div></div>`).join("");

  drawFlow(d);

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

async function tick(){ try { render(await (await fetch("/api/state")).json()); } catch {} }
tick(); setInterval(tick, 2000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    repo = Path(".")

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
