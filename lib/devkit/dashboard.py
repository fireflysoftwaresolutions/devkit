# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev dashboard`: the project command centre.

A single self-contained HTML file (inline CSS, JS and SVG; data embedded as
JSON) rendered from the registry that `dev sync` produces, plus the handoff
repository's Markdown files for the Explorer. Nothing is fetched at runtime
except the Manrope web font (the page falls back to the system sans offline).

Views: Overview · Projects · Tasks · Activity · Explorer · Health, a global
faceted search, a command palette (⌘K / Ctrl+K) and keyboard navigation.
View and filter state live in the URL hash (`#/<view>?k=v&…`), so a view can
be bookmarked or shared, and the back button works.

Security: every string from notes, files and git is inserted with
`textContent` / DOM APIs (never innerHTML), links are limited to http(s),
mailto and in-page anchors, and the embedded JSON escapes `<`, `>` and `&`.

Brand: Firefly Software (fireflysoftwaresolutions/brand-kit) — charcoal and
paper surfaces, amber as the firefly's light (sparingly), Manrope. Chart marks
use amber-family steps validated with the dataviz palette checks
(#c38530 on the dark surface, #9d671f on the light surface).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import core

EXPLORER_CAP = 2_000_000          # bytes of Markdown embedded for the Explorer (newest files first)
ROOT_FILES = ("PROJECTS.md", "TASKS.md", "ACTIVITY.md", "README.md")
WORKTREE_KEYS = ("path", "branch", "location", "active", "locked", "modified", "untracked", "unpushed", "pr",
                 "last_activity", "size_kb", "missing", "ahead", "behind")


# --------------------------------------------------------------------------- data collection

def safe_json(obj) -> str:
    """JSON that is safe inside <script type="application/json">: no raw <, > or & anywhere."""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)
    return (s.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def explorer_files(handoff: Path, cap: int = EXPLORER_CAP):
    """Markdown files of the handoff repo, newest first, until `cap` bytes. -> (files, meta)."""
    root = Path(handoff)
    cands = set()
    for name in ROOT_FILES:
        if (root / name).is_file():
            cands.add(root / name)
    for sub in ("projects", "plans", "log"):
        d = root / sub
        if d.is_dir():
            cands.update(p for p in d.rglob("*.md") if p.is_file())

    def mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0

    files, total, omitted = [], 0, 0
    for f in sorted(cands, key=lambda p: (p.name not in ROOT_FILES, -mtime(p))):
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        size = len(text.encode("utf-8", "replace"))
        if total + size > cap:
            omitted += 1
            continue
        total += size
        files.append({"path": str(f.relative_to(root)), "mtime": int(mtime(f)), "size": size, "text": text})
    return files, {"capped": omitted > 0, "omitted": omitted, "bytes": total, "cap": cap}


def _session_ts(line: str) -> int:
    m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})", line or "")
    if not m:
        return 0
    try:
        return int(time.mktime(time.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M")))
    except (ValueError, OverflowError):
        return 0


def derived_issues(reg: dict, now: float = None) -> list:
    """Attention items computed from the registry: [{kind, project, text}]."""
    now = now or time.time()
    out = []
    ids = [p.get("id", "") for p in reg.get("projects", [])]
    for item in reg.get("issues") or []:
        kind, msg = (item[0], item[1]) if isinstance(item, (list, tuple)) and len(item) >= 2 else ("info", str(item))
        pid = next((i for i in sorted(ids, key=len, reverse=True) if i and i in msg), None)
        out.append({"kind": kind, "project": pid, "text": msg})
    for p in reg.get("projects", []):
        pid = p.get("id")
        ns = p.get("note_summary") or {}
        tasks = ns.get("tasks") or {}
        if ns.get("status") == "blocked":
            state = [re.sub(r"^\s*([-*+]|\d+\.)\s*(\[[ xX~]\]\s*)?", "", ln).strip()
                     for ln in (ns.get("state") or "").splitlines() if ln.strip()]
            out.append({"kind": "blocked", "project": pid,
                        "text": f"{pid} is blocked" + (f" — {state[0][:140]}" if state else "")})
        for t in tasks.get("overdue") or []:
            due = re.search(r"\bdue:(\d{4}-\d{2}-\d{2})\b", t)
            text = re.sub(r"\s*\bdue:\d{4}-\d{2}-\d{2}\b", "", t)
            out.append({"kind": "overdue", "project": pid,
                        "text": f"Overdue in {pid}: {text}" + (f" (due {due.group(1)})" if due else "")})
        if ns:
            last = max([_session_ts(s) for s in ns.get("sessions") or []] or [0])
            la = p.get("last_activity") or 0
            recent = sum(v for k, v in (p.get("activity") or {}).items()
                         if k >= time.strftime("%Y-%m-%d", time.localtime(now - 7 * 86400)))
            if la and recent and la - last > 3 * 86400:
                since = (f"has no session since {time.strftime('%Y-%m-%d', time.localtime(last))}" if last
                         else "has no session in its log yet")
                out.append({"kind": "stale-note", "project": pid,
                            "text": f"Handoff note of {pid} {since}, but the repo has {recent} commit(s) this week "
                                    f"— ask the agent for a handoff"})
        for w in p.get("worktrees") or []:
            pr = w.get("pr") or {}
            if pr.get("state") in ("MERGED", "CLOSED") and not w.get("active"):
                out.append({"kind": "reclaimable", "project": pid,
                            "text": f"Worktree `{w.get('branch')}` of {pid}: PR #{pr.get('number')} "
                                    f"{str(pr.get('state')).lower()} — `dev gc` reclaims it"})
    return out


def guard_events(limit: int = 80) -> list:
    """Recent guard decisions from ~/.local/state/devkit/logs/hooks.log."""
    f = core.STATE / "logs" / "hooks.log"
    out = []
    try:
        lines = f.read_text(errors="replace").splitlines()[-4000:]
    except OSError:
        return out
    for ln in lines:
        m = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) guard (\w+) (\w+): (.*)$", ln)
        if m:
            out.append({"ts": m.group(1), "agent": m.group(2), "decision": m.group(3), "reason": m.group(4)})
    return out[-limit:]


def _canary():
    try:
        from . import canary
        summary = {a: list(v) for a, v in canary.summary(7).items()}
        records = [{k: r.get(k) for k in ("ts", "agent", "project", "ok", "excerpt", "session")}
                   for r in canary.records(30)][-2000:]
        return {"summary": summary, "records": records}
    except Exception:
        return {"summary": {}, "records": []}


def _ops():
    try:
        from . import registry
        return registry.ops_log(60)
    except Exception:
        return []


def _project(p: dict) -> dict:
    keep = ("id", "owner", "name", "path", "remote", "github", "default_branch", "canonical", "canonical_ok",
            "stack", "commands", "prs_open", "prs_known", "size_kb", "last_activity", "note", "note_summary",
            "activity", "status_line", "plans", "git")
    out = {k: p.get(k) for k in keep}
    main = p.get("main") or {}
    out["main"] = {k: main.get(k) for k in ("branch", "modified", "untracked", "ahead", "behind", "unpushed",
                                             "ahead_default", "last_commit", "last_commit_ts", "last_activity",
                                             "active", "pr")}
    out["worktrees"] = [{k: w.get(k) for k in WORKTREE_KEYS} for w in p.get("worktrees") or []]
    return out


def collect(reg: dict, handoff: Path = None, now: float = None) -> dict:
    handoff = Path(handoff) if handoff else core.HANDOFF
    files, meta = explorer_files(handoff)
    return {
        "v": 1,
        "version": core.VERSION,
        "copyright": core.COPYRIGHT,
        "generated": int(reg.get("generated") or now or time.time()),
        "rendered": int(now or time.time()),
        "host": reg.get("host") or os.uname().nodename.split(".")[0],
        "dev_root": core.tilde(reg.get("dev_root") or core.DEV),
        "handoff": core.tilde(handoff),
        "projects": [_project(p) for p in reg.get("projects", [])],
        "issues": derived_issues(reg, now),
        "canary": _canary(),
        "ops": _ops(),
        "guard": guard_events(),
        "drift": reg.get("drift"),
        "files": files,
        "files_meta": meta,
    }


# --------------------------------------------------------------------------- page

SPARK = ('<svg class="spark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" '
         'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="2.5"/>'
         '<path d="M12 2.5V6"/><path d="M12 18v3.5"/><path d="M2.5 12H6"/><path d="M18 12h3.5"/>'
         '<path d="M8.5 8.5h.01"/><path d="M15.5 8.5h.01"/><path d="M8.5 15.5h.01"/><path d="M15.5 15.5h.01"/></svg>')
FAVICON = ("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
           "stroke='%23ffb34a' stroke-width='2' stroke-linecap='round'%3E%3Ccircle cx='12' cy='12' r='3'/%3E"
           "%3Cpath d='M12 2.5V6M12 18v3.5M2.5 12H6M18 12h3.5'/%3E%3C/svg%3E")

LIGHT = """
  color-scheme: light;
  --page:#f3f1eb; --surface:#ffffff; --surface-2:#faf9f6; --surface-3:#efece4;
  --line:#d8d4ca; --line-soft:#e7e3da; --text:#272820; --heading:#10110f; --text-2:#62645b; --muted:#62645b;
  --accent:#ffb34a; --accent-ink:#10110f; --accent-text:#855414; --focus:#855414;
  --mark:#9d671f; --mark-soft:rgba(157,103,31,.10); --deemph:#c4bfb2; --grid:#ebe8e0; --axis:#c9c4b8;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b; --good-text:#006300; --bad-text:#a52a2a;
  --hl:rgba(255,179,74,.45); --overlay:rgba(16,17,15,.42);
  --shadow:0 1px 2px rgba(16,17,15,.05),0 10px 30px rgba(16,17,15,.07);
"""
DARK = """
  color-scheme: dark;
  --page:#10110f; --surface:#171815; --surface-2:#1d1e1a; --surface-3:#24251f;
  --line:#3a3c35; --line-soft:#262822; --text:#f3f1eb; --heading:#ffffff; --text-2:#bfb8ab; --muted:#9d988c;
  --accent:#ffb34a; --accent-ink:#10110f; --accent-text:#ffb34a; --focus:#ffb34a;
  --mark:#c38530; --mark-soft:rgba(195,133,48,.14); --deemph:#4f524a; --grid:#262822; --axis:#474a42;
  --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b; --good-text:#3cc23c; --bad-text:#ec7b7b;
  --hl:rgba(255,179,74,.35); --overlay:rgba(0,0,0,.55);
  --shadow:0 1px 2px rgba(0,0,0,.4),0 12px 32px rgba(0,0,0,.35);
"""

CSS = r"""
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--page);color:var(--text);font:400 15px/1.55 "Manrope","Manrope Variable",system-ui,-apple-system,"Segoe UI",Arial,sans-serif;font-feature-settings:"ss01","cv11";}
a{color:inherit;text-underline-offset:3px;text-decoration-color:var(--line)}
a:hover{text-decoration-color:currentColor}
button,input,select{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px;border-radius:6px}
.skip{position:absolute;left:-999px;top:8px;background:var(--surface);padding:8px 12px;border-radius:8px;z-index:100}
.skip:focus{left:8px}
code,kbd,pre{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:.86em}
kbd{border:1px solid var(--line);border-bottom-width:2px;border-radius:5px;padding:0 5px;background:var(--surface-2);color:var(--text-2);font-size:.75rem;line-height:1.4}
.muted{color:var(--muted)} .t2{color:var(--text-2)} .mono{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-size:.84em}
.label{font-size:.75rem;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--text-2)}
.num{font-variant-numeric:tabular-nums}
/* header */
.top{position:sticky;top:0;z-index:30;display:flex;align-items:center;gap:16px;padding:10px 24px;background:color-mix(in srgb,var(--page) 86%,transparent);backdrop-filter:saturate(1.2) blur(12px);-webkit-backdrop-filter:blur(12px);border-bottom:1px solid var(--line-soft)}
.brand{display:flex;align-items:center;gap:10px;min-width:0;cursor:pointer;background:none;border:0;padding:0}
.brand .spark{width:26px;height:26px;color:var(--accent);flex:none}
.brand .word{font-weight:700;font-size:1.15rem;letter-spacing:-.03em;color:var(--heading)}
.brand .sub{font-size:.8rem;color:var(--text-2);white-space:nowrap}
@media (max-width:1560px){.brand .sub{display:none}}
[hidden]{display:none!important}
.tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;margin-left:8px}
.tabs::-webkit-scrollbar{display:none}
.tab{border:0;background:none;padding:7px 11px;border-radius:999px;color:var(--text-2);font-weight:600;font-size:.88rem;cursor:pointer;white-space:nowrap}
.tab:hover{background:var(--surface-2);color:var(--text)}
.tab[aria-selected="true"]{background:var(--surface);color:var(--heading);box-shadow:inset 0 0 0 1px var(--line)}
.tab .k{margin-left:6px;font-size:.7rem;color:var(--muted)}
.spacer{flex:1}
.search{position:relative;flex:0 1 300px;min-width:160px}
.search input{width:100%;padding:8px 36px 8px 34px;border-radius:999px;border:1px solid var(--line);background:var(--surface);outline:none}
.search input:focus{border-color:var(--focus);box-shadow:0 0 0 3px var(--mark-soft)}
.search .ico{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.search kbd{position:absolute;right:12px;top:50%;transform:translateY(-50%)}
.btn{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:var(--surface);border-radius:999px;padding:7px 12px;cursor:pointer;font-size:.85rem;font-weight:600;white-space:nowrap}
.btn:hover{border-color:var(--text-2)}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:transparent}
.btn.ghost{border-color:transparent;background:none}
.btn.sm{padding:4px 10px;font-size:.78rem}
.btn[aria-pressed="true"]{background:var(--surface-3);border-color:var(--text-2)}
.meta{display:flex;align-items:center;gap:12px;font-size:.8rem;color:var(--text-2);white-space:nowrap}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:.78rem;font-weight:600;background:var(--surface);cursor:pointer}
/* filters */
.filters{position:sticky;top:57px;z-index:20;display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 24px;background:var(--page);border-bottom:1px solid var(--line-soft)}
.facet{position:relative}
.facet>summary{list-style:none;cursor:pointer}
.facet>summary::-webkit-details-marker{display:none}
.facet[open]>summary .btn{border-color:var(--text-2)}
.menu{position:absolute;top:calc(100% + 6px);left:0;z-index:40;min-width:220px;max-height:340px;overflow:auto;background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:6px}
.menu label{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:8px;cursor:pointer;font-size:.86rem}
.menu label:hover{background:var(--surface-2)}
.menu .count{margin-left:auto;color:var(--muted);font-size:.78rem}
.chip{display:inline-flex;align-items:center;gap:6px;padding:3px 6px 3px 10px;border-radius:999px;background:var(--surface-3);font-size:.78rem;font-weight:600}
.chip button{border:0;background:none;cursor:pointer;color:var(--text-2);padding:0 4px;border-radius:999px}
.chip button:hover{color:var(--text)}
.toggle{display:inline-flex;align-items:center;gap:6px;font-size:.84rem;color:var(--text-2);cursor:pointer;padding:6px 8px;border-radius:999px}
.toggle input{accent-color:var(--mark)}
/* layout */
main{max-width:1440px;margin:0 auto;padding:24px 24px 64px;outline:none}
.hero{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:16px;margin:8px 0 22px}
.hero h1{font-weight:400;font-size:clamp(2.4rem,4.5vw,4.2rem);line-height:1.04;letter-spacing:-.045em;margin:0;color:var(--heading)}
.hero h1 .stop{color:var(--accent)}
.hero p{margin:6px 0 0;color:var(--text-2);font-size:1.05rem}
h2.sec{display:flex;align-items:baseline;gap:10px;font-size:1.25rem;font-weight:500;letter-spacing:-.025em;margin:34px 0 12px;color:var(--heading)}
h2.sec .label{margin-left:auto}
.grid{display:grid;gap:16px}
.cols-2{grid-template-columns:minmax(0,1.6fr) minmax(0,1fr)}
.card{background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;padding:16px;box-shadow:var(--shadow)}
.card.flat{box-shadow:none}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(168px,1fr));gap:12px}
.kpi{background:var(--surface);border:1px solid var(--line-soft);border-radius:14px;padding:14px 16px;cursor:pointer;text-align:left;transition:border-color .15s,transform .15s}
.kpi:hover{border-color:var(--line);transform:translateY(-1px)}
.kpi .label{display:block;margin-bottom:6px;text-transform:none;letter-spacing:.01em;font-size:.78rem}
.kpi .val{font-size:1.9rem;font-weight:600;letter-spacing:-.03em;line-height:1.1;color:var(--heading)}
.kpi .sub{font-size:.78rem;color:var(--text-2);margin-top:4px;display:flex;align-items:center;gap:6px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none}
.live{background:var(--good);box-shadow:0 0 0 3px color-mix(in srgb,var(--good) 25%,transparent)}
@keyframes pulse{50%{box-shadow:0 0 0 6px color-mix(in srgb,var(--good) 0%,transparent)}}
.live.anim{animation:pulse 2s infinite}
/* status badges (status tokens + icon + label) */
.badge{display:inline-flex;align-items:center;gap:5px;font-size:.74rem;font-weight:650;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--text);background:var(--surface-2);white-space:nowrap}
.badge .ic{font-size:.7rem;line-height:1}
.s-active .ic{color:var(--good)} .s-paused .ic{color:var(--warning)} .s-blocked .ic{color:var(--critical)}
.s-done .ic,.s-archived .ic,.s-untracked .ic{color:var(--muted)}
.p-high .ic{color:var(--accent-text)} .p-low .ic{color:var(--muted)}
.stack{display:inline-block;font-size:.72rem;padding:1px 7px;border-radius:6px;background:var(--surface-3);color:var(--text-2);margin:0 4px 4px 0}
/* attention */
.attn{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.attn li{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:12px;background:var(--surface);border:1px solid var(--line-soft)}
.attn .ic{flex:none;width:20px;text-align:center}
.attn .kind{font-size:.72rem;font-weight:650;color:var(--text-2);text-transform:uppercase;letter-spacing:.05em;flex:none;min-width:84px}
.k-blocked .ic,.k-overdue .ic{color:var(--critical)} .k-reclaimable .ic{color:var(--good)} .k-stale-note .ic,.k-layout .ic,.k-worktree .ic,.k-work .ic{color:var(--warning)}
/* projects */
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px}
.toolbar .count{font-size:.9rem;color:var(--text-2);margin-right:auto}
.toolbar select{padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:var(--surface);font-size:.84rem}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.cards>*,.grid>*,.kpis>*,.board>*{min-width:0}
.statusline,.lastsess,.tcard{overflow-wrap:anywhere}
.pcard{position:relative;min-width:0;display:flex;flex-direction:column;gap:10px;background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;padding:16px;cursor:pointer;text-align:left;transition:border-color .15s,box-shadow .15s,transform .15s}
.pcard:hover,.pcard:focus-visible{border-color:var(--line);box-shadow:var(--shadow);transform:translateY(-1px)}
.pcard .top-row{display:flex;align-items:flex-start;gap:8px}
.pcard .title{font-weight:650;font-size:1.02rem;letter-spacing:-.02em;color:var(--heading);word-break:break-word}
.pcard .owner{display:block;font-size:.74rem;color:var(--muted);font-weight:500}
.pcard .badges{margin-left:auto;display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end}
.gitline{font-size:.8rem;color:var(--text-2);display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.statusline{font-size:.82rem;border-left:2px solid var(--accent);padding:4px 0 4px 10px;color:var(--text)}
.statusline .who{display:block;font-size:.72rem;color:var(--muted)}
.tasklist{list-style:none;margin:0;padding:0;display:grid;gap:4px;font-size:.84rem}
.tasklist li{display:flex;gap:8px;align-items:flex-start}
.tasklist .box{flex:none;width:14px;height:14px;margin-top:3px;border:1.5px solid var(--text-2);border-radius:4px}
.tasklist .box.doing{border-color:var(--mark);background:var(--mark-soft)}
.tasklist .box.done{background:var(--text-2);border-color:var(--text-2)}
.meter{height:6px;border-radius:999px;background:var(--mark-soft);overflow:hidden}
.meter i{display:block;height:100%;border-radius:999px;background:var(--mark)}
.pfoot{display:flex;align-items:center;gap:10px;font-size:.76rem;color:var(--text-2);margin-top:auto;white-space:nowrap}
.pfoot>span{white-space:nowrap}
.pfoot .spk{margin-right:auto}
.lastsess{font-size:.76rem;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dense .pcard{padding:12px;gap:6px}
.dense .cards{grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px}
.dense .tasklist,.dense .lastsess{display:none}
.group-h{display:flex;align-items:baseline;gap:8px;margin:22px 0 10px;font-weight:650}
.group-h .muted{font-weight:500;font-size:.85rem}
table.t{width:100%;border-collapse:collapse;font-size:.85rem;background:var(--surface);border-radius:14px;overflow:hidden;border:1px solid var(--line-soft)}
table.t th,table.t td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line-soft);vertical-align:top}
table.t th{font-size:.72rem;font-weight:650;letter-spacing:.05em;text-transform:uppercase;color:var(--text-2);background:var(--surface-2);position:sticky;top:0}
table.t th button{all:unset;cursor:pointer} table.t th button:focus-visible{outline:2px solid var(--focus)}
table.t tr[data-nav]{cursor:pointer} table.t tr[data-nav]:hover td{background:var(--surface-2)}
.dense table.t td,.dense table.t th{padding:5px 10px}
.tablewrap{overflow-x:auto;border-radius:14px}
/* kanban */
.board{display:grid;grid-template-columns:repeat(4,minmax(240px,1fr));gap:14px;align-items:start}
.col{background:var(--surface-2);border:1px solid var(--line-soft);border-radius:16px;padding:12px;min-height:120px}
.col h3{display:flex;align-items:center;gap:8px;margin:2px 4px 10px;font-size:.92rem;font-weight:650}
.col h3 .n{margin-left:auto;color:var(--muted);font-weight:600;font-size:.8rem}
.tcard{display:block;width:100%;text-align:left;background:var(--surface);border:1px solid var(--line-soft);border-radius:12px;padding:10px 12px;margin-bottom:8px;cursor:pointer;font-size:.86rem}
.tcard:hover,.tcard:focus-visible{border-color:var(--line)}
.tcard .proj{display:flex;flex-wrap:wrap;gap:4px 6px;align-items:center;margin-top:6px;font-size:.74rem;color:var(--text-2)}
.due{font-size:.72rem;font-weight:650;padding:0 6px;border-radius:6px;background:var(--surface-3);white-space:nowrap}
.due.over{color:var(--bad-text)}
/* charts */
figure.chart{margin:0}
figure.chart figcaption{display:flex;align-items:baseline;gap:10px;margin-bottom:8px}
figure.chart figcaption .ttl{font-weight:650;color:var(--heading)}
figure.chart figcaption .sub{font-size:.8rem;color:var(--text-2)}
figure.chart figcaption .btn{margin-left:auto}
.chartbox{position:relative;width:100%}
.chartbox svg{display:block;width:100%;height:auto;overflow:visible}
.chartbox svg:focus-visible{outline:2px solid var(--focus);outline-offset:4px}
.ax{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.vlabel{fill:var(--text);font-size:11px;font-weight:650}
.bar{fill:var(--mark);transition:opacity .12s}
.bar.dim{opacity:.35}
.spk rect{fill:var(--deemph)} .spk rect.cur{fill:var(--mark)}
.hbar{display:grid;grid-template-columns:minmax(120px,32%) 1fr auto;gap:8px 12px;align-items:center;font-size:.84rem}
.hbar .trk{height:10px}
.hbar .trk i{display:block;height:100%;border-radius:0 4px 4px 0;background:var(--mark);min-width:2px}
.hbar button{all:unset;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hbar button:hover{text-decoration:underline}
.tip{position:fixed;z-index:80;pointer-events:none;background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);padding:8px 10px;font-size:.8rem;min-width:140px;max-width:280px}
.tip strong{font-size:1rem;color:var(--heading)}
.tip ul{list-style:none;margin:6px 0 0;padding:0;display:grid;gap:2px}
.tip li{display:flex;gap:8px;justify-content:space-between}
/* timeline */
.tl{list-style:none;margin:0;padding:0}
.tl .day{font-size:.74rem;font-weight:650;text-transform:uppercase;letter-spacing:.06em;color:var(--text-2);margin:16px 0 6px}
.tl .it{display:grid;grid-template-columns:48px 1fr;gap:10px;padding:7px 0;border-bottom:1px solid var(--line-soft);font-size:.84rem}
.tl .it .tm{color:var(--muted);font-variant-numeric:tabular-nums;font-size:.78rem;padding-top:1px}
.tl .who{font-weight:650;margin-right:6px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:2px;background:var(--surface)}
.seg button{border:0;background:none;padding:4px 12px;border-radius:999px;cursor:pointer;font-size:.8rem;font-weight:600;color:var(--text-2)}
.seg button[aria-pressed="true"]{background:var(--surface-3);color:var(--heading)}
/* explorer */
.explorer{display:grid;grid-template-columns:320px minmax(0,1fr);gap:16px;align-items:start}
.tree{position:sticky;top:72px;max-height:calc(100vh - 92px);overflow:auto;background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;padding:10px}
.tree input{width:100%;padding:7px 10px;border-radius:10px;border:1px solid var(--line);background:var(--surface-2);margin-bottom:8px}
.tree details{margin-left:4px}
.tree summary{cursor:pointer;padding:4px 6px;border-radius:8px;font-size:.84rem;font-weight:650;list-style:none;display:flex;gap:6px;align-items:center}
.tree summary::before{content:"▸";color:var(--muted);font-size:.7rem;transition:transform .12s}
.tree details[open]>summary::before{transform:rotate(90deg)}
.tree summary::-webkit-details-marker{display:none}
.tree .file{display:block;width:100%;text-align:left;border:0;background:none;padding:4px 8px 4px 22px;border-radius:8px;font-size:.82rem;cursor:pointer;color:var(--text-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tree .file:hover{background:var(--surface-2);color:var(--text)}
.tree .file[aria-current="true"]{background:var(--surface-3);color:var(--heading);font-weight:650}
.viewer{background:var(--surface);border:1px solid var(--line-soft);border-radius:16px;min-height:60vh}
.viewer .vh{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line-soft);position:sticky;top:64px;background:var(--surface);border-radius:16px 16px 0 0;z-index:5}
.viewer .vh .path{font-weight:650;color:var(--heading);word-break:break-all}
.viewer .vh input{padding:6px 10px;border-radius:999px;border:1px solid var(--line);background:var(--surface-2);min-width:180px}
.notice{font-size:.8rem;color:var(--text-2);background:var(--surface-2);border:1px dashed var(--line);border-radius:10px;padding:8px 10px;margin-bottom:10px}
/* markdown */
.md{padding:18px 22px 26px;font-size:.93rem;line-height:1.65;overflow-wrap:anywhere}
.md h1,.md h2,.md h3,.md h4{color:var(--heading);letter-spacing:-.02em;line-height:1.25;margin:1.3em 0 .5em}
.md h1{font-size:1.55rem;font-weight:500} .md h2{font-size:1.2rem;font-weight:600} .md h3{font-size:1rem;font-weight:650}
.md h1:first-child,.md h2:first-child{margin-top:0}
.md p{margin:.5em 0} .md ul,.md ol{margin:.4em 0;padding-left:1.4em} .md li{margin:.15em 0}
.md li.task{list-style:none;margin-left:-1.3em}
.md li.task input{margin:0 8px 0 0;accent-color:var(--mark);vertical-align:-2px}
.md li.task.done>span{color:var(--muted);text-decoration:line-through}
.md code{background:var(--surface-3);padding:1px 5px;border-radius:5px}
.md pre{background:var(--surface-2);border:1px solid var(--line-soft);padding:12px 14px;border-radius:10px;overflow:auto}
.md pre code{background:none;padding:0}
.md blockquote{margin:.6em 0;padding:.2em 0 .2em 12px;border-left:3px solid var(--line);color:var(--text-2)}
.md table{border-collapse:collapse;margin:.8em 0;font-size:.84rem;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--line-soft);padding:6px 10px;text-align:left}
.md th{background:var(--surface-2)}
.md hr{border:0;border-top:1px solid var(--line-soft);margin:1.2em 0}
.md em{color:var(--text-2)}
.md .fm{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
mark{background:var(--hl);color:inherit;border-radius:3px;padding:0 1px}
mark.cur{outline:2px solid var(--accent)}
/* drawer, palette, sheet */
.scrim{position:fixed;inset:0;background:var(--overlay);z-index:60;opacity:0;transition:opacity .18s}
.scrim.on{opacity:1}
.drawer{position:fixed;top:0;right:0;bottom:0;width:min(760px,100vw);z-index:70;background:var(--page);border-left:1px solid var(--line);box-shadow:var(--shadow);overflow:auto;transform:translateX(100%);transition:transform .22s ease}
.drawer.on{transform:none}
.drawer .dh{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:flex-start;padding:18px 22px 14px;background:var(--page);border-bottom:1px solid var(--line-soft)}
.drawer .dh h2{margin:0;font-size:1.55rem;font-weight:450;letter-spacing:-.035em;line-height:1.2;color:var(--heading);word-break:break-word}
.drawer .db{padding:16px 22px 40px;display:grid;gap:14px}
.drawer .md{padding:0;font-size:.9rem}
.drawer .sect h3{margin:0 0 8px;font-size:.78rem;font-weight:650;letter-spacing:.06em;text-transform:uppercase;color:var(--text-2)}
.cmd{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line-soft);border-radius:10px;padding:6px 6px 6px 12px;font-family:ui-monospace,Menlo,monospace;font-size:.8rem}
.cmd span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cmds{display:grid;gap:6px}
.palette{position:fixed;inset:10vh 0 auto 0;margin:0 auto;width:min(680px,94vw);z-index:90;background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);overflow:hidden}
.palette input{width:100%;border:0;border-bottom:1px solid var(--line-soft);padding:16px 18px;font-size:1.02rem;background:none;outline:none}
.palette ul{list-style:none;margin:0;padding:6px;max-height:56vh;overflow:auto}
.palette li{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:10px;cursor:pointer;font-size:.9rem}
.palette li[aria-selected="true"]{background:var(--surface-3)}
.palette li .grp{margin-left:auto;font-size:.72rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.palette .ft{display:flex;gap:14px;padding:8px 14px;border-top:1px solid var(--line-soft);font-size:.75rem;color:var(--text-2)}
.sheet{position:fixed;inset:12vh 0 auto 0;margin:0 auto;width:min(560px,94vw);z-index:90;background:var(--surface);border:1px solid var(--line);border-radius:16px;box-shadow:var(--shadow);padding:18px 22px}
.sheet h2{margin:0 0 10px;font-size:1.2rem;font-weight:600}
.sheet dl{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;margin:0;font-size:.88rem}
.sheet dt{text-align:right}
.toast{position:fixed;bottom:24px;left:50%;transform:translate(-50%,20px);opacity:0;z-index:95;background:var(--heading);color:var(--page);padding:8px 14px;border-radius:999px;font-size:.84rem;font-weight:600;transition:all .2s;pointer-events:none}
.toast.on{opacity:1;transform:translate(-50%,0)}
.empty{text-align:center;padding:40px 16px;color:var(--text-2);background:var(--surface);border:1px dashed var(--line);border-radius:16px}
.empty strong{display:block;color:var(--heading);font-size:1.05rem;margin-bottom:6px}
.empty code{background:var(--surface-3);padding:2px 6px;border-radius:6px}
.err{position:fixed;left:12px;right:12px;bottom:12px;z-index:99;background:#5a1d1d;color:#fff;padding:10px 14px;border-radius:10px;font-size:.85rem}
.results .res{display:block;width:100%;text-align:left;border:1px solid var(--line-soft);background:var(--surface);border-radius:12px;padding:10px 14px;margin-bottom:8px;cursor:pointer}
.results .res:hover,.results .res:focus-visible{border-color:var(--line)}
.results .rt{display:flex;gap:8px;align-items:center;font-weight:650;font-size:.9rem;color:var(--heading)}
.results .rs{font-size:.82rem;color:var(--text-2);margin-top:4px}
.rtype{font-size:.68rem;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:1px 6px;border-radius:5px;background:var(--surface-3);color:var(--text-2)}
.foot{max-width:1440px;margin:0 auto;padding:18px 24px 40px;display:flex;flex-wrap:wrap;gap:8px 18px;font-size:.78rem;color:var(--muted);border-top:1px solid var(--line-soft)}
.foot .spark{width:14px;height:14px;color:var(--accent);vertical-align:-2px}
@media (max-width:1100px){.cols-2{grid-template-columns:1fr}.board{grid-template-columns:repeat(2,minmax(220px,1fr))}.top .sub{display:none}}
@media (max-width:760px){
  .top{flex-wrap:wrap;gap:8px 10px;padding:10px 14px}.tabs{order:3;width:100%;margin:0}.search{order:2;flex:1 1 100%}.meta .tick{display:none}
  .filters{position:static;padding:8px 14px;overflow-x:auto;flex-wrap:nowrap}main{padding:16px 14px 48px}
  .explorer{grid-template-columns:1fr}.tree{position:static;max-height:none}.viewer .vh{position:static}
  .board{grid-template-columns:1fr}.cards{grid-template-columns:minmax(0,1fr)}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
  .pfoot{flex-wrap:wrap;white-space:normal}.meta .tick{display:none}#palbtn{display:none}.toolbar select{max-width:46vw}
  .hbar{grid-template-columns:minmax(90px,38%) 1fr auto}
}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
@media print{
  :root{--page:#fff;--surface:#fff;--surface-2:#fff;--shadow:none}
  .top,.filters,.drawer,.scrim,.palette,.sheet,.toast,.btn,.seg,.toolbar select,.tree input,.viewer .vh input{display:none!important}
  main{max-width:none;padding:0}.pcard,.card,.col,.kpi{break-inside:avoid;box-shadow:none}
  .explorer{grid-template-columns:1fr}.tree{display:none}a{text-decoration:none}
}
"""

JS = r"""
(() => {
"use strict";
window.addEventListener("error", e => { const b = document.createElement("div"); b.className = "err"; b.id = "dk-error";
  b.textContent = "Dashboard error: " + (e.message || e.error); document.body.append(b); });
const D = JSON.parse(document.getElementById("devkit-data").textContent);
const VIEWS = [["overview","Overview","o"],["projects","Projects","p"],["tasks","Tasks","t"],["activity","Activity","a"],["explorer","Explorer","e"],["health","Health","h"]];
const DEFAULT = {view:"overview", q:"", status:[], priority:[], owner:[], stack:[], tag:[], live:false, prs:false, attn:false,
  sort:"activity", group:"none", layout:"cards", dense:false, p:"", f:"", fq:"", range:30, types:[], tp:""};
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const SVGNS = "http://www.w3.org/2000/svg";
const store = { get(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };

/* ---------------------------------------------------------------- DOM helpers (textContent only) */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) for (const k in props) {
    const v = props[k];
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
    else if (k === "href") el.setAttribute("href", safeUrl(v));
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const c of kids.flat(Infinity)) { if (c == null || c === false) continue; el.append(c.nodeType ? c : document.createTextNode(String(c))); }
  return el;
}
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const k in attrs || {}) if (attrs[k] != null) el.setAttribute(k, attrs[k]);
  append(el, kids); return el;
}
function safeUrl(u) { u = String(u || "").trim(); return /^(https?:|mailto:|#)/i.test(u) ? u : "#"; }
function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

/* ---------------------------------------------------------------- time */
const nowS = () => Date.now() / 1000;
function rel(ts) {
  if (!ts) return "—"; const d = Math.max(0, nowS() - ts);
  if (d < 60) return "just now"; if (d < 3600) return Math.floor(d / 60) + " min ago";
  if (d < 86400) return Math.floor(d / 3600) + " h ago"; if (d < 86400 * 14) return Math.floor(d / 86400) + " d ago";
  return Math.floor(d / 604800) + " w ago";
}
function fmt(ts) { if (!ts) return "—"; return new Date(ts * 1000).toLocaleString(undefined, {year:"numeric", month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}); }
function tm(ts) { const d = new Date(ts * 1000); return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0"); }
function lkey(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0"); }
function days(n) { const out = [], b = new Date(D.generated * 1000); b.setHours(12, 0, 0, 0);
  for (let i = n - 1; i >= 0; i--) { const d = new Date(b); d.setDate(b.getDate() - i); out.push(lkey(d)); } return out; }
function sessTs(line) { const m = /^(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2})/.exec(line || ""); return m ? new Date(m[1] + "T" + m[2] + ":" + m[3] + ":00").getTime() / 1000 : 0; }
function sessText(s) { return String(s || "").slice(16).replace(/^\s*·\s*/, ""); }
function shortDay(k) { const d = new Date(k + "T12:00:00"); return d.toLocaleDateString(undefined, {month: "short", day: "numeric"}); }
function compact(n) { return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "K" : n >= 1e3 ? (n / 1e3).toFixed(1) + "K" : String(n); }
function kb(k) { if (k == null) return "—"; return k >= 1048576 ? (k / 1048576).toFixed(1) + " GB" : k >= 1024 ? Math.round(k / 1024) + " MB" : k + " KB"; }

/* ---------------------------------------------------------------- data model */
const ISSUES = D.issues || [];
const P = (D.projects || []).map((p, i) => {
  const ns = p.note_summary || null;
  const t = (ns && ns.tasks) || {now: [], next: [], later: [], done_recent: [], open: 0, done: 0, overdue: []};
  const id = p.id || p.name || "project-" + i;
  const owner = id.includes("/") ? id.slice(0, id.lastIndexOf("/")) : "(local)";
  const wts = p.worktrees || [];
  const live = (p.main && p.main.active ? 1 : 0) + wts.filter(w => w.active).length;
  const status = ns ? (ns.status || "active") : "untracked";
  const o = Object.assign({}, p, {id, ns, t, owner, wts, live, status, priority: (ns && ns.priority) || "normal",
    tags: (ns && ns.tags) || [], stack: p.stack || [], prs: (p.prs_open || []).length, act: p.activity || {},
    attn: ISSUES.filter(x => x.project === id).length, sessions: (ns && ns.sessions) || []});
  o.hay = [id, p.name, o.stack.join(" "), (p.main || {}).branch, o.tags.join(" "), ns && ns.purpose, ns && ns.state,
    t.now.join(" "), t.next.join(" "), t.later.join(" "), p.status_line && p.status_line.line].filter(Boolean).join(" \n ").toLowerCase();
  return o;
});
const BYID = new Map(P.map(p => [p.id, p]));
const FILES = D.files || [];
const FILEMAP = new Map(FILES.map(f => [f.path, f]));
const STATUS = {active: ["●", "Active"], paused: ["❚❚", "Paused"], blocked: ["⊘", "Blocked"], done: ["✓", "Done"], archived: ["▢", "Archived"], untracked: ["○", "No note"]};
const PRIO = {high: ["▲", "High"], normal: ["", "Normal"], low: ["▼", "Low"]};
const PRANK = {high: 0, normal: 1, low: 2};
const SRANK = {blocked: 0, active: 1, paused: 2, untracked: 3, done: 4, archived: 5};
function statusBadge(s) { const [ic, l] = STATUS[s] || STATUS.active; return h("span", {class: "badge s-" + s, title: "Status: " + l}, h("span", {class: "ic", "aria-hidden": "true", text: ic}), l); }
function prioBadge(p) { if (p === "normal" || !PRIO[p]) return null; const [ic, l] = PRIO[p]; return h("span", {class: "badge p-" + p, title: "Priority: " + l}, h("span", {class: "ic", "aria-hidden": "true", text: ic}), l); }
function stripMarker(line) { return String(line || "").replace(/^[\W_]*DevKit\s*·?\s*/, ""); }
function projectFor(path) {
  let best = null;
  for (const p of P) { if ((path === "projects/" + p.id + ".md" || path.startsWith("plans/" + p.id + "/")) && (!best || p.id.length > best.id.length)) best = p; }
  return best;
}

/* ---------------------------------------------------------------- state + URL hash */
let S = Object.assign({}, DEFAULT);
function encode(st) {
  const parts = [];
  for (const k of Object.keys(DEFAULT)) {
    if (k === "view") continue; const v = st[k], d = DEFAULT[k];
    if (JSON.stringify(v) === JSON.stringify(d)) continue;
    parts.push(k + "=" + encodeURIComponent(Array.isArray(v) ? v.join(",") : v === true ? "1" : String(v)));
  }
  return "#/" + st.view + (parts.length ? "?" + parts.join("&") : "");
}
function decode(hash) {
  const m = /^#\/([a-z]+)(?:\?(.*))?$/.exec(hash || ""); if (!m) return null;
  const st = Object.assign({}, DEFAULT);
  st.view = (VIEWS.some(v => v[0] === m[1]) || m[1] === "search") ? m[1] : "overview";
  for (const kv of (m[2] || "").split("&")) {
    if (!kv) continue; const i = kv.indexOf("="); const k = i < 0 ? kv : kv.slice(0, i); const v = i < 0 ? "" : decodeURIComponent(kv.slice(i + 1));
    if (!(k in DEFAULT)) continue; const d = DEFAULT[k];
    st[k] = Array.isArray(d) ? (v ? v.split(",") : []) : typeof d === "boolean" ? v === "1" : typeof d === "number" ? (+v || d) : v;
  }
  return st;
}
function set(patch, push) {
  Object.assign(S, patch);
  const hs = encode(S);
  if (push && location.hash !== hs) { location.hash = hs; return; }   /* hashchange renders */
  if (location.hash !== hs) history.replaceState(null, "", hs);
  store.set("devkit.state", S); render();
}
window.addEventListener("hashchange", () => { const st = decode(location.hash); if (st) { S = st; store.set("devkit.state", S); render(); } });

/* ---------------------------------------------------------------- filtering */
function tokens(q) { return String(q || "").toLowerCase().split(/\s+/).filter(Boolean); }
function matchFacets(p) {
  if (S.status.length && !S.status.includes(p.status)) return false;
  if (S.priority.length && !S.priority.includes(p.priority)) return false;
  if (S.owner.length && !S.owner.includes(p.owner)) return false;
  if (S.stack.length && !p.stack.some(x => S.stack.includes(x))) return false;
  if (S.tag.length && !p.tags.some(x => S.tag.includes(x))) return false;
  if (S.live && !p.live) return false; if (S.prs && !p.prs) return false; if (S.attn && !p.attn) return false;
  return true;
}
function filtered(useQ) { const tk = useQ ? tokens(S.q) : []; return P.filter(p => matchFacets(p) && tk.every(t => p.hay.includes(t))); }
function facetCount() { return S.status.length + S.priority.length + S.owner.length + S.stack.length + S.tag.length + (S.live ? 1 : 0) + (S.prs ? 1 : 0) + (S.attn ? 1 : 0); }

/* ---------------------------------------------------------------- markdown (safe subset, DOM only) */
function inline(text, q) {
  const frag = document.createDocumentFragment();
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\[([^\]]+)\]\(([^)\s]+)\))|(<(https?:\/\/[^>\s]+)>)|(https?:\/\/[^\s)]+)|((?:^|(?<=\s))_[^_\s][^_]*_(?=\s|$|[.,;:]))/g;
  let last = 0, m;
  const src = String(text || "");
  while ((m = re.exec(src))) {
    if (m.index > last) frag.append(document.createTextNode(src.slice(last, m.index)));
    if (m[1]) frag.append(h("code", {text: m[1].slice(1, -1)}));
    else if (m[2]) frag.append(h("strong", null, inline(m[2].slice(2, -2))));
    else if (m[3]) frag.append(link(m[4], m[5]));
    else if (m[6]) frag.append(link(m[7], m[7]));
    else if (m[8]) frag.append(link(m[8], m[8]));
    else if (m[9]) frag.append(h("em", null, inline(m[9].trim().replace(/^_|_$/g, ""))));
    last = re.lastIndex;
  }
  if (last < src.length) frag.append(document.createTextNode(src.slice(last)));
  return frag;
}
function link(label, url) {
  const kids = label === url ? [label] : [inline(label)];
  if (/^https?:|^mailto:/i.test(url)) return h("a", {href: url, target: "_blank", rel: "noopener noreferrer"}, kids);
  const path = String(url).replace(/^\.\//, "").replace(/#.*$/, "");
  if (FILEMAP.has(path)) return h("a", {href: encode(Object.assign({}, S, {view: "explorer", f: path, fq: ""}))}, kids);
  return h("span", null, kids);
}
function md(text) {
  const root = h("div", {class: "md"});
  let src = String(text || "").replace(/<!--[\s\S]*?-->/g, "");
  const fm = /^---\n([\s\S]*?)\n---\n/.exec(src);
  if (fm) {
    const box = h("div", {class: "fm"});
    for (const ln of fm[1].split("\n")) { const i = ln.indexOf(":"); if (i > 0 && ln.slice(i + 1).trim() && ln.slice(i + 1).trim() !== "[]") box.append(h("span", {class: "badge"}, h("span", {class: "t2", text: ln.slice(0, i) + ":"}), " " + ln.slice(i + 1).trim())); }
    if (box.childNodes.length) root.append(box);
    src = src.slice(fm[0].length);
  }
  const lines = src.split("\n");
  let i = 0, para = [];
  const flush = () => { if (para.length) { root.append(h("p", null, inline(para.join(" ")))); para = []; } };
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) { flush(); const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++; root.append(h("pre", null, h("code", {text: buf.join("\n")}))); continue; }
    let m = /^(#{1,6})\s+(.*)$/.exec(ln);
    if (m) { flush(); root.append(h("h" + Math.min(4, m[1].length), null, inline(m[2]))); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(ln)) { flush(); root.append(h("hr")); i++; continue; }
    if (/^\s*\|/.test(ln) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush(); const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const head = cells(ln); i += 2; const tb = h("tbody");
      while (i < lines.length && /^\s*\|/.test(lines[i])) { tb.append(h("tr", null, cells(lines[i]).map(c => h("td", null, inline(c))))); i++; }
      root.append(h("table", null, h("thead", null, h("tr", null, head.map(c => h("th", null, inline(c))))), tb)); continue;
    }
    if (/^>\s?/.test(ln)) { flush(); const buf = []; while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, "")); root.append(h("blockquote", null, inline(buf.join(" ")))); continue; }
    m = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(ln);
    if (m) {
      flush(); const ordered = /\d/.test(m[2]); const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && (m = /^(\s*)([-*+]|\d+\.)\s+(.*)$/.exec(lines[i]))) {
        const depth = Math.min(3, Math.floor(m[1].length / 2)); let body = m[3]; const tm = /^\[( |x|X|~)\]\s+(.*)$/.exec(body);
        let li;
        if (tm) { const done = tm[1].toLowerCase() === "x"; li = h("li", {class: "task" + (done ? " done" : ""), style: depth ? "margin-left:" + depth * 1.2 + "em" : null},
            h("input", {type: "checkbox", disabled: true, checked: done, "aria-label": done ? "done" : "open"}), h("span", null, inline(tm[2]))); }
        else li = h("li", {style: depth ? "margin-left:" + depth * 1.2 + "em" : null}, inline(body));
        list.append(li); i++;
      }
      root.append(list); continue;
    }
    if (!ln.trim()) { flush(); i++; continue; }
    para.push(ln.trim()); i++;
  }
  flush();
  return root;
}
function highlight(el, q, cls) {
  const tk = tokens(q).filter(t => t.length > 1); if (!tk.length) return 0;
  const src = "(" + tk.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")";
  const re = new RegExp(src, "gi"), probe = new RegExp(src, "i");
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {acceptNode: n => n.parentNode && !/^(SCRIPT|STYLE|MARK)$/.test(n.parentNode.nodeName) && probe.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT});
  const nodes = []; let n; while ((n = walker.nextNode())) nodes.push(n);
  let count = 0;
  for (const node of nodes) {
    const frag = document.createDocumentFragment(); let last = 0; const s = node.nodeValue; re.lastIndex = 0; let m;
    while ((m = re.exec(s))) { if (m.index > last) frag.append(s.slice(last, m.index)); frag.append(h("mark", {class: cls || null, text: m[0]})); last = re.lastIndex; count++; }
    if (last < s.length) frag.append(s.slice(last)); node.parentNode.replaceChild(frag, node);
  }
  return count;
}

/* ---------------------------------------------------------------- shared widgets */
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("on"), 1600); }
function copy(text) {
  const done = () => toast("Copied: " + text);
  if (navigator.clipboard && window.isSecureContext !== false) navigator.clipboard.writeText(text).then(done, () => fallback());
  else fallback();
  function fallback() { const ta = h("textarea", {style: "position:fixed;opacity:0"}); ta.value = text; document.body.append(ta); ta.select(); try { document.execCommand("copy"); done(); } catch (e) { toast("Copy failed"); } ta.remove(); }
}
function cmdRow(cmd) { return h("div", {class: "cmd"}, h("span", {text: cmd, title: cmd}), h("button", {class: "btn sm", type: "button", onclick: () => copy(cmd), "aria-label": "Copy " + cmd}, "Copy")); }
function empty(title, ...body) { return h("div", {class: "empty"}, h("strong", {text: title}), ...body); }
function tip(x, y, node) { const t = $("#tip"); clear(t).append(node); t.hidden = false; const r = t.getBoundingClientRect();
  let left = x + 14, top = y + 14; if (left + r.width > innerWidth - 8) left = x - r.width - 14; if (top + r.height > innerHeight - 8) top = y - r.height - 14;
  t.style.left = Math.max(8, left) + "px"; t.style.top = Math.max(8, top) + "px"; }
function untip() { $("#tip").hidden = true; }
function sparkline(p, n) {
  const ks = days(n || 30), vals = ks.map(k => p.act[k] || 0), max = Math.max(1, ...vals), w = 96, hh = 24, bw = w / ks.length;
  const total = vals.reduce((a, b) => a + b, 0);
  const svg = sv("svg", {class: "spk", width: w, height: hh, viewBox: "0 0 " + w + " " + hh, role: "img", "aria-label": total + " commits in the last " + ks.length + " days"});
  vals.forEach((v, i) => { const bh = v ? Math.max(2, (v / max) * (hh - 2)) : 1; svg.append(sv("rect", {x: (i * bw + 0.5).toFixed(1), y: (hh - bh).toFixed(1), width: Math.max(1, bw - 1.5).toFixed(1), height: bh.toFixed(1), rx: v ? 1 : 0, class: i >= ks.length - 7 ? "cur" : null})); });
  return svg;
}
function niceMax(v) { if (v <= 4) return 4; const p = Math.pow(10, Math.floor(Math.log10(v))); const m = v / p; return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p; }

/* column chart: commits per day (single series), crosshair-free per-column hover, keyboard arrows, table twin */
function activityChart(projects, n, title) {
  const ks = days(n);
  const series = ks.map(k => { const per = []; let total = 0; for (const p of projects) { const v = p.act[k] || 0; if (v) { per.push([p.id, v]); total += v; } } per.sort((a, b) => b[1] - a[1]); return {k, total, per}; });
  const total = series.reduce((a, s) => a + s.total, 0);
  const fig = h("figure", {class: "chart card flat"});
  const box = h("div", {class: "chartbox"});
  let tableOn = false;
  const tbtn = h("button", {class: "btn sm", type: "button", "aria-pressed": "false", onclick: () => { tableOn = !tableOn; tbtn.setAttribute("aria-pressed", String(tableOn)); draw(); }}, "Table");
  fig.append(h("figcaption", null, h("span", {class: "ttl", text: title || "Commits per day"}), h("span", {class: "sub", text: total + " commits · last " + n + " days · " + projects.length + " projects"}), tbtn), box);
  let active = -1;
  function show(i, evt) {
    active = i; const s = series[i]; if (!s) return;
    $$(".bar", box).forEach((b, j) => b.classList.toggle("dim", j !== i));
    const node = h("div", null, h("strong", {text: s.total + (s.total === 1 ? " commit" : " commits")}), h("div", {class: "t2", text: shortDay(s.k)}),
      s.per.length ? h("ul", null, s.per.slice(0, 6).map(([id, v]) => h("li", null, h("span", {text: id}), h("span", {class: "num", text: String(v)}))), s.per.length > 6 ? h("li", {class: "t2", text: "+" + (s.per.length - 6) + " more"}) : null) : null);
    let x, y; if (evt && evt.clientX != null) { x = evt.clientX; y = evt.clientY; } else { const r = box.querySelectorAll(".hit")[i].getBoundingClientRect(); x = r.left + r.width / 2; y = r.top + 20; }
    tip(x, y, node);
  }
  function hide() { active = -1; $$(".bar", box).forEach(b => b.classList.remove("dim")); untip(); }
  function draw() {
    clear(box); untip();
    if (tableOn) {
      box.append(h("div", {class: "tablewrap"}, h("table", {class: "t"}, h("thead", null, h("tr", null, h("th", {text: "Day"}), h("th", {text: "Commits"}), h("th", {text: "Projects"}))),
        h("tbody", null, series.slice().reverse().map(s => h("tr", null, h("td", {text: shortDay(s.k)}), h("td", {class: "num", text: String(s.total)}), h("td", {class: "t2", text: s.per.map(([id, v]) => id + " " + v).join(", ") || "—"})))))));
      return;
    }
    const W = Math.max(280, box.clientWidth || 800), H = 200, padL = 34, padB = 26, padT = 18, iw = W - padL - 8, ih = H - padB - padT;
    const max = niceMax(Math.max(1, ...series.map(s => s.total))), band = iw / series.length, bw = Math.min(24, Math.max(3, band * 0.62));
    const svg = sv("svg", {viewBox: "0 0 " + W + " " + H, width: W, height: H, role: "img", tabindex: "0",
      "aria-label": (title || "Commits per day") + ": " + total + " commits over " + n + " days. Use arrow keys to read each day."});
    for (let g = 0; g <= 4; g++) { const v = (max / 4) * g, y = padT + ih - (v / max) * ih;
      svg.append(sv("line", {x1: padL, x2: W - 8, y1: y.toFixed(1), y2: y.toFixed(1), stroke: g ? "var(--grid)" : "var(--axis)", "stroke-width": 1}));
      svg.append(sv("text", {x: padL - 6, y: (y + 4).toFixed(1), "text-anchor": "end", class: "ax"}, compact(Math.round(v)))); }
    const maxI = series.reduce((bi, s, i) => s.total > series[bi].total ? i : bi, 0);
    const every = Math.ceil(series.length / Math.max(2, Math.floor(iw / 70)));
    series.forEach((s, i) => {
      const cx = padL + band * i + band / 2, x = cx - bw / 2;
      if (s.total) { const bh = Math.max(2, (s.total / max) * ih), y = padT + ih - bh, r = Math.min(4, bw / 2, bh);
        svg.append(sv("path", {class: "bar", d: "M" + x + "," + (padT + ih) + "V" + (y + r) + "Q" + x + "," + y + " " + (x + r) + "," + y + "H" + (x + bw - r) + "Q" + (x + bw) + "," + y + " " + (x + bw) + "," + (y + r) + "V" + (padT + ih) + "Z"}));
        if (i === maxI) svg.append(sv("text", {x: cx, y: y - 5, "text-anchor": "middle", class: "vlabel"}, String(s.total))); }
      if (i % every === 0 || i === series.length - 1) svg.append(sv("text", {x: cx, y: H - 8, "text-anchor": "middle", class: "ax"}, shortDay(s.k)));
      const hit = sv("rect", {class: "hit", x: padL + band * i, y: padT, width: band, height: ih, fill: "transparent"});
      hit.addEventListener("pointermove", e => show(i, e)); hit.addEventListener("pointerleave", hide);
      svg.append(hit);
    });
    svg.addEventListener("keydown", e => { if (e.key === "ArrowRight" || e.key === "ArrowLeft") { e.preventDefault(); const j = active < 0 ? series.length - 1 : Math.max(0, Math.min(series.length - 1, active + (e.key === "ArrowRight" ? 1 : -1))); show(j); } else if (e.key === "Escape") hide(); });
    svg.addEventListener("blur", hide);
    box.append(svg);
    if (!total) box.append(h("p", {class: "t2", text: "No commits in this range."}));
  }
  requestAnimationFrame(draw);
  if (window.ResizeObserver) { const ro = new ResizeObserver(debounce(() => { if (box.isConnected) draw(); else ro.disconnect(); }, 120)); ro.observe(box); }
  return fig;
}
function rankedBars(rows, fmtv, onclick) {
  const max = Math.max(1, ...rows.map(r => r[1]));
  return h("div", {class: "hbar"}, rows.map(([label, v, id]) => [
    onclick ? h("button", {type: "button", onclick: () => onclick(id || label), title: label, text: label}) : h("span", {text: label, title: label}),
    h("div", {class: "trk", role: "img", "aria-label": label + ": " + fmtv(v)}, h("i", {style: "width:" + Math.max(0.5, (v / max) * 100).toFixed(1) + "%"})),
    h("span", {class: "num t2", text: fmtv(v)})]));
}

/* ---------------------------------------------------------------- header, filters */
function renderHeader() {
  const tabs = $("#tabs"); clear(tabs);
  for (const [v, label, key] of VIEWS) tabs.append(h("button", {class: "tab", role: "tab", "aria-selected": String(S.view === v), type: "button", onclick: () => go(v), title: label + " (g " + key + ")"}, label));
  const q = $("#q"); if (document.activeElement !== q) q.value = S.q;
  const c = D.canary || {}; let ok = 0, tot = 0; for (const a in c.summary || {}) { ok += c.summary[a][0]; tot += c.summary[a][1]; }
  const cb = $("#canary"); clear(cb);
  if (tot) { const pct = Math.round(100 * ok / tot), st = pct >= 95 ? ["●", "var(--good)"] : pct >= 80 ? ["▲", "var(--warning)"] : ["⊘", "var(--critical)"];
    cb.append(h("span", {style: "color:" + st[1], "aria-hidden": "true", text: st[0]}), "Canary " + pct + "%"); cb.title = ok + " of " + tot + " replies carried the DevKit marker (7 days)"; }
  else { cb.append("Canary —"); cb.title = "No tracked replies yet"; }
}
function facetMenu(key, label, values) {
  const sel = S[key];
  const det = h("details", {class: "facet"});
  det.append(h("summary", null, h("span", {class: "btn sm"}, label + (sel.length ? " · " + sel.length : ""), h("span", {"aria-hidden": "true", text: "▾"}))));
  const menu = h("div", {class: "menu", role: "group", "aria-label": label});
  if (!values.length) menu.append(h("div", {class: "t2", style: "padding:8px", text: "No values"}));
  for (const [v, n] of values) {
    const cb = h("input", {type: "checkbox", checked: sel.includes(v)});
    cb.addEventListener("change", () => { const next = cb.checked ? sel.concat([v]) : sel.filter(x => x !== v); set({[key]: next}, true); });
    menu.append(h("label", null, cb, h("span", {text: v}), h("span", {class: "count num", text: String(n)})));
  }
  det.append(menu);
  return det;
}
function counts(fn) { const m = new Map(); for (const p of P) for (const v of [].concat(fn(p))) if (v) m.set(v, (m.get(v) || 0) + 1); return Array.from(m.entries()).sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(b[0])); }
function renderFilters() {
  const bar = $("#filters"); clear(bar);
  if (S.view === "explorer" || S.view === "health") { bar.hidden = true; return; } bar.hidden = false;
  bar.append(h("span", {class: "label", text: "Filter"}),
    facetMenu("status", "Status", counts(p => p.status)), facetMenu("priority", "Priority", counts(p => p.priority)),
    facetMenu("owner", "Owner / host", counts(p => p.owner)), facetMenu("stack", "Stack", counts(p => p.stack)),
    facetMenu("tag", "Tag", counts(p => p.tags)));
  for (const [k, l] of [["live", "Live session"], ["prs", "Open PRs"], ["attn", "Needs attention"]]) {
    const cb = h("input", {type: "checkbox", checked: S[k]}); cb.addEventListener("change", () => set({[k]: cb.checked}, true));
    bar.append(h("label", {class: "toggle"}, cb, l));
  }
  const chips = [];
  for (const k of ["status", "priority", "owner", "stack", "tag"]) for (const v of S[k]) chips.push(h("span", {class: "chip"}, v, h("button", {type: "button", "aria-label": "Remove " + v, onclick: () => set({[k]: S[k].filter(x => x !== v)}, true), text: "×"})));
  if (chips.length || facetCount()) bar.append(...chips, h("button", {class: "btn sm ghost", type: "button", onclick: () => set({status: [], priority: [], owner: [], stack: [], tag: [], live: false, prs: false, attn: false}, true)}, "Clear filters"));
}

/* ---------------------------------------------------------------- views */
function kpis(ps) {
  const ks = days(14), last7 = ks.slice(7), prev7 = ks.slice(0, 7);
  const sum = keys => ps.reduce((a, p) => a + keys.reduce((b, k) => b + (p.act[k] || 0), 0), 0);
  const c7 = sum(last7), p7 = sum(prev7), delta = c7 - p7;
  const now = ps.reduce((a, p) => a + p.t.now.length, 0), open = ps.reduce((a, p) => a + p.t.open, 0);
  const wts = ps.reduce((a, p) => a + p.wts.length, 0), recl = ps.reduce((a, p) => a + p.wts.filter(w => w.pr && /MERGED|CLOSED/.test(w.pr.state) && !w.active).length, 0);
  const live = ps.reduce((a, p) => a + p.live, 0);
  const c = D.canary || {}; let ok = 0, tot = 0; for (const a in c.summary || {}) { ok += c.summary[a][0]; tot += c.summary[a][1]; }
  const tile = (label, val, sub, onclick, extra) => h("button", {class: "kpi", type: "button", onclick}, h("span", {class: "label", text: label}), h("div", {class: "val", text: val}), h("div", {class: "sub"}, extra || null, sub));
  return h("section", {class: "kpis", "aria-label": "Key figures"},
    tile("Active projects", String(ps.filter(p => p.status === "active").length), "of " + ps.length + " tracked", () => go("projects", {status: ["active"]})),
    tile("In progress", String(now), open + " open tasks", () => go("tasks")),
    tile("Open PRs", String(ps.reduce((a, p) => a + p.prs, 0)), ps.filter(p => p.prs).length + " projects", () => go("projects", {prs: true})),
    tile("Live sessions", String(live), live ? "agents or shells working now" : "nobody working now", () => go("projects", {live: true}), live ? h("span", {class: "dot live anim"}) : null),
    tile("Worktrees", String(wts), recl ? recl + " reclaimable · dev gc" : "none reclaimable", () => go("health")),
    tile("Commits · 7 days", compact(c7), (delta >= 0 ? "↑ " : "↓ ") + Math.abs(delta) + " vs previous 7 days", () => go("activity", {range: 7})),
    tile("Canary · 7 days", tot ? Math.round(100 * ok / tot) + "%" : "—", tot ? ok + " of " + tot + " replies" : "no tracked replies", () => go("health")));
}
function attention(ps, limit) {
  const ids = new Set(ps.map(p => p.id));
  const items = ISSUES.filter(x => !x.project || ids.has(x.project));
  if (!items.length) return h("p", {class: "t2", text: "Nothing needs attention. ✓"});
  const icon = {blocked: "⊘", overdue: "◷", reclaimable: "↺", "stale-note": "✎", layout: "⌂", worktree: "⑂", work: "!", error: "⊘"};
  const ul = h("ul", {class: "attn"});
  for (const it of items.slice(0, limit || 999)) {
    ul.append(h("li", {class: "k-" + it.kind}, h("span", {class: "ic", "aria-hidden": "true", text: icon[it.kind] || "•"}), h("span", {class: "kind", text: it.kind.replace("-", " ")}),
      h("span", null, inline(it.text)), it.project && BYID.has(it.project) ? h("button", {class: "btn sm ghost", type: "button", "data-nav": "", onclick: () => openProject(it.project), style: "margin-left:auto"}, "Open") : null));
  }
  if (limit && items.length > limit) ul.append(h("li", null, h("button", {class: "btn sm ghost", type: "button", onclick: () => go("projects", {attn: true}), text: "+" + (items.length - limit) + " more"})));
  return ul;
}
function viewOverview(main) {
  const ps = filtered(false);
  const nowTasks = ps.reduce((a, p) => a + p.t.now.length, 0);
  main.append(h("section", {class: "hero"}, h("div", null,
    h("h1", null, "Projects", h("span", {class: "stop", text: "."})),
    h("p", null, ps.filter(p => p.status === "active").length + " active · " + nowTasks + " tasks in progress · " + ps.reduce((a, p) => a + p.live, 0) + " live sessions · synced " + rel(D.generated))),
    h("div", null, h("button", {class: "btn", type: "button", onclick: () => openPalette()}, "Jump to… ", h("kbd", {text: "⌘K"})))));
  main.append(kpis(ps));
  main.append(h("h2", {class: "sec"}, "Needs attention", h("span", {class: "label", text: ISSUES.length + " items"})), attention(ps, 8));
  const grid = h("div", {class: "grid cols-2"});
  const left = h("div", null, activityChart(ps, 30, "Commits per day"));
  const statuses = ps.filter(p => p.status_line).sort((a, b) => (b.status_line.ts || 0) - (a.status_line.ts || 0)).slice(0, 6);
  left.append(h("h2", {class: "sec"}, "Latest agent status", h("span", {class: "label", text: "DevKit · lines"})),
    statuses.length ? h("div", {class: "grid"}, statuses.map(p => h("button", {class: "pcard", type: "button", "data-nav": "", onclick: () => openProject(p.id)},
      h("div", {class: "statusline"}, h("span", {class: "who", text: p.id + " · " + (p.status_line.agent || "agent") + " · " + rel(p.status_line.ts)}), inline(stripMarker(p.status_line.line))))))
      : empty("No status lines yet", "Every Claude/Codex reply that starts with ", h("code", {text: "DevKit ·"}), " is captured here."));
  const right = h("div", null, h("h2", {class: "sec", style: "margin-top:0"}, "Now", h("span", {class: "label", text: nowTasks + " in progress"})), nowBoard(ps, 10),
    h("h2", {class: "sec"}, "Recent activity"), timeline(ps, 14));
  grid.append(left, right); main.append(grid);
}
function nowBoard(ps, limit) {
  const rows = []; for (const p of ps) for (const t of p.t.now) rows.push([p, t]);
  if (!rows.length) return empty("Nothing in progress", "Start one: ", h("code", {text: 'dev task add "…" --now'}));
  rows.sort((a, b) => PRANK[a[0].priority] - PRANK[b[0].priority] || (b[0].last_activity || 0) - (a[0].last_activity || 0));
  return h("div", null, rows.slice(0, limit).map(([p, t]) => taskCard(p, t, "Now")),
    rows.length > limit ? h("button", {class: "btn sm ghost", type: "button", onclick: () => go("tasks"), text: "All " + rows.length + " in progress →"}) : null);
}
function dueOf(t) { const m = /\bdue:(\d{4}-\d{2}-\d{2})\b/.exec(t); return m ? m[1] : ""; }
function taskText(t) { return String(t).replace(/\s*\bdue:\d{4}-\d{2}-\d{2}\b/, ""); }
function taskLine(t, cls) { const due = dueOf(t), over = due && due < lkey(new Date());
  return h("li", null, h("span", {class: "box " + (cls || ""), "aria-hidden": "true"}), h("span", null, inline(taskText(t)), due ? h("span", {class: "due" + (over ? " over" : ""), style: "margin-left:6px", text: (over ? "overdue " : "due ") + due}) : null)); }
function taskCard(p, t, bucket) {
  const due = /\bdue:(\d{4}-\d{2}-\d{2})\b/.exec(t), over = due && due[1] < lkey(new Date());
  return h("button", {class: "tcard", type: "button", "data-nav": "", onclick: () => openProject(p.id, "tasks")},
    h("div", null, inline(t.replace(/\s*\bdue:\d{4}-\d{2}-\d{2}\b/, ""))),
    h("div", {class: "proj"}, h("span", {class: "dot", style: "background:var(--" + (bucket === "Now" ? "mark" : "deemph") + ")"}), h("span", {text: p.id}),
      due ? h("span", {class: "due" + (over ? " over" : ""), text: (over ? "overdue " : "due ") + due[1]}) : null));
}
function timelineItems(ps, nDays) {
  const cutoff = nowS() - nDays * 86400, items = [];
  for (const p of ps) for (const s of p.sessions) { const ts = sessTs(s); if (ts >= cutoff) items.push({ts, p, text: sessText(s)}); }
  for (const ln of D.ops || []) { const ts = sessTs(ln); if (ts >= cutoff) items.push({ts, text: ln.slice(19), op: true}); }
  for (const r of (D.canary || {}).records || []) if (!r.ok && r.ts >= cutoff) items.push({ts: r.ts, text: "Canary miss (" + r.agent + "): " + (r.excerpt || ""), canary: true, p: BYID.get(r.project)});
  return items.sort((a, b) => b.ts - a.ts);
}
function timeline(ps, nDays, limit) {
  const items = timelineItems(ps, nDays).slice(0, limit || 40);
  if (!items.length) return empty("No recent activity", "Sessions and operations from the last " + nDays + " days appear here.");
  const ul = h("ul", {class: "tl"}); let day = "";
  for (const it of items) {
    const k = lkey(new Date(it.ts * 1000));
    if (k !== day) { day = k; ul.append(h("li", {class: "day", text: shortDay(k)})); }
    ul.append(h("li", {class: "it"}, h("span", {class: "tm", text: tm(it.ts)}),
      h("div", null, it.p ? h("a", {href: encode(Object.assign({}, S, {p: it.p.id})), class: "who", text: it.p.id, onclick: e => { e.preventDefault(); openProject(it.p.id); }}) : h("span", {class: "who t2", text: it.op ? "ops" : ""}), inline(it.text))));
  }
  return ul;
}
function sortProjects(ps) {
  const by = {activity: (a, b) => (b.last_activity || 0) - (a.last_activity || 0), priority: (a, b) => PRANK[a.priority] - PRANK[b.priority] || SRANK[a.status] - SRANK[b.status] || (b.last_activity || 0) - (a.last_activity || 0),
    name: (a, b) => a.id.localeCompare(b.id), tasks: (a, b) => b.t.open - a.t.open, size: (a, b) => (b.size_kb || 0) - (a.size_kb || 0), attention: (a, b) => b.attn - a.attn || (b.last_activity || 0) - (a.last_activity || 0)};
  return ps.slice().sort(by[S.sort] || by.activity);
}
function projectCard(p) {
  const m = p.main || {}, st = [];
  if (m.modified) st.push(m.modified + " modified"); if (m.untracked) st.push(m.untracked + " untracked"); if (m.ahead) st.push("↑" + m.ahead); if (m.behind) st.push("↓" + m.behind);
  const total = p.t.open + p.t.done, pct = total ? Math.round(100 * p.t.done / total) : 0;
  const act30 = Object.values(p.act).reduce((a, b) => a + b, 0);
  return h("button", {class: "pcard", type: "button", "data-nav": "", "data-id": p.id, onclick: () => openProject(p.id), "aria-label": "Open " + p.id},
    h("div", {class: "top-row"}, h("div", null, h("span", {class: "owner", text: p.owner}), h("span", {class: "title", text: p.name || p.id})),
      h("div", {class: "badges"}, p.live ? h("span", {class: "badge", title: "Live session"}, h("span", {class: "dot live"}), "Live") : null, statusBadge(p.status), prioBadge(p.priority))),
    p.stack.length ? h("div", null, p.stack.map(s => h("span", {class: "stack", text: s}))) : null,
    p.git === false ? null : h("div", {class: "gitline"}, h("span", {class: "mono", text: "⎇ " + (m.branch || "—")}), h("span", {text: st.join(" · ") || "clean"}), m.pr ? h("span", {text: "PR #" + m.pr.number + " " + String(m.pr.state).toLowerCase()}) : null),
    p.status_line ? h("div", {class: "statusline"}, h("span", {class: "who", text: (p.status_line.agent || "agent") + " · " + rel(p.status_line.ts)}), inline(stripMarker(p.status_line.line))) : null,
    p.t.now.length ? h("ul", {class: "tasklist", "aria-label": "In progress"}, p.t.now.slice(0, 3).map(t => taskLine(t, "doing"))) : null,
    total ? h("div", null, h("div", {class: "gitline", style: "justify-content:space-between"}, h("span", {text: p.t.open + " open · " + p.t.done + " done"}), h("span", {class: "num", text: pct + "%"})),
      h("div", {class: "meter", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Tasks done"}, h("i", {style: "width:" + pct + "%"}))) : null,
    p.sessions[0] ? h("div", {class: "lastsess", title: p.sessions[0]}, "Last session " + rel(sessTs(p.sessions[0])) + " · ", inline(sessText(p.sessions[0]))) : null,
    h("div", {class: "pfoot"}, h("span", {class: "spk"}, sparkline(p, 30)), h("span", {class: "num", text: act30 + " commits/30d"}), p.prs ? h("span", {text: p.prs + " PR"}) : null,
      p.wts.length ? h("span", {text: p.wts.length + " wt"}) : null, h("span", {text: kb(p.size_kb)})));
}
function projectTable(ps) {
  const th = (label, key) => h("th", {scope: "col", "aria-sort": S.sort === key ? "descending" : null}, key ? h("button", {type: "button", onclick: () => set({sort: key}), text: label + (S.sort === key ? " ↓" : "")}) : label);
  return h("div", {class: "tablewrap"}, h("table", {class: "t"},
    h("thead", null, h("tr", null, th("Project", "name"), th("Status", "priority"), h("th", {text: "Branch / state"}), th("Now / open", "tasks"), h("th", {text: "PRs"}), h("th", {text: "WT"}), h("th", {text: "30 days"}), th("Last activity", "activity"), th("Size", "size"))),
    h("tbody", null, ps.map(p => { const m = p.main || {};
      return h("tr", {"data-nav": "", tabindex: "0", onclick: () => openProject(p.id), onkeydown: e => { if (e.key === "Enter") openProject(p.id); }},
        h("td", null, h("strong", {text: p.id}), p.live ? h("span", {class: "dot live", style: "margin-left:6px", title: "Live session"}) : null),
        h("td", null, statusBadge(p.status), " ", prioBadge(p.priority)),
        h("td", {class: "mono"}, (m.branch || "—") + (m.modified || m.untracked ? " · " + ((m.modified || 0) + (m.untracked || 0)) + " changed" : "")),
        h("td", {class: "num", text: p.t.now.length + " / " + p.t.open}), h("td", {class: "num", text: p.prs_known ? String(p.prs) : "—"}),
        h("td", {class: "num", text: String(p.wts.length)}), h("td", null, sparkline(p, 30)), h("td", {text: rel(p.last_activity)}), h("td", {class: "num", text: kb(p.size_kb)})); }))));
}
function viewProjects(main) {
  const ps = sortProjects(filtered(true));
  const tb = h("div", {class: "toolbar"}, h("span", {class: "count", text: ps.length + " of " + P.length + " projects" + (S.q ? " matching “" + S.q + "”" : "")}));
  const sel = (key, label, opts) => { const s = h("select", {"aria-label": label}); for (const [v, l] of opts) s.append(h("option", {value: v, selected: S[key] === v}, l)); s.addEventListener("change", () => set({[key]: s.value}, true)); return s; };
  tb.append(sel("group", "Group by", [["none", "No grouping"], ["owner", "Group: owner"], ["status", "Group: status"], ["priority", "Group: priority"]]),
    sel("sort", "Sort by", [["activity", "Sort: activity"], ["priority", "Sort: priority"], ["attention", "Sort: attention"], ["tasks", "Sort: open tasks"], ["name", "Sort: name"], ["size", "Sort: size"]]),
    h("div", {class: "seg", role: "group", "aria-label": "Layout"}, ["cards", "table"].map(l => h("button", {type: "button", "aria-pressed": String(S.layout === l), onclick: () => set({layout: l}, true)}, l === "cards" ? "Cards" : "Table"))),
    h("button", {class: "btn sm", type: "button", "aria-pressed": String(S.dense), onclick: () => set({dense: !S.dense}, true)}, "Dense"));
  main.append(h("div", {class: S.dense ? "dense" : null}, tb));
  const wrap = h("div", {class: S.dense ? "dense" : null}); main.append(wrap);
  if (!ps.length) { wrap.append(empty("No projects match", facetCount() || S.q ? h("button", {class: "btn", type: "button", onclick: () => set({q: "", status: [], priority: [], owner: [], stack: [], tag: [], live: false, prs: false, attn: false}, true)}, "Clear search and filters") : ["Clone one with ", h("code", {text: "dev clone owner/repo"})])); return; }
  const groups = new Map();
  for (const p of ps) { const g = S.group === "owner" ? p.owner : S.group === "status" ? (STATUS[p.status] || [0, p.status])[1] : S.group === "priority" ? PRIO[p.priority][1] : ""; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(p); }
  for (const [g, list] of groups) {
    if (g) wrap.append(h("div", {class: "group-h"}, h("span", {text: g}), h("span", {class: "muted", text: list.length + (list.length === 1 ? " project" : " projects")})));
    wrap.append(S.layout === "table" ? projectTable(list) : h("div", {class: "cards"}, list.map(projectCard)));
  }
}
const SHOWN = {};
function viewTasks(main) {
  const ps = filtered(false).filter(p => !S.tp || p.id === S.tp), tk = tokens(S.q);
  const buckets = {Now: [], Next: [], Later: [], Done: []};
  for (const p of ps) for (const [b, list] of [["Now", p.t.now], ["Next", p.t.next], ["Later", p.t.later], ["Done", p.t.done_recent]])
    for (const t of list) if (tk.every(x => (t + " " + p.id).toLowerCase().includes(x))) buckets[b].push([p, t]);
  const projSel = h("select", {"aria-label": "Project"}); projSel.append(h("option", {value: ""}, "All projects"));
  for (const p of P.filter(matchFacets)) projSel.append(h("option", {value: p.id, selected: S.tp === p.id}, p.id));
  projSel.addEventListener("change", () => set({tp: projSel.value}, true));
  const total = Object.values(buckets).reduce((a, b) => a + b.length, 0);
  main.append(h("div", {class: "toolbar"}, h("span", {class: "count", text: total + " tasks" + (S.q ? " matching “" + S.q + "”" : "")}), projSel));
  if (!total) { main.append(empty("No tasks yet", "Add one from any terminal: ", h("code", {text: 'dev task add "…" --now -p <project>'}), " — or ask Claude/Codex to plan the work; they keep the board in each handoff note.")); return; }
  const board = h("div", {class: "board"});
  for (const b of ["Now", "Next", "Later", "Done"]) {
    const list = buckets[b], n = SHOWN[b] || 150;
    const col = h("section", {class: "col", "aria-label": b}, h("h3", null, h("span", {class: "dot", style: "background:var(--" + (b === "Now" ? "mark" : b === "Done" ? "good" : "deemph") + ")"}), b, h("span", {class: "n num", text: String(list.length)})));
    if (!list.length) col.append(h("p", {class: "t2", style: "margin:4px", text: b === "Now" ? "Nothing in progress." : "Empty."}));
    for (const [p, t] of list.slice(0, n)) col.append(taskCard(p, t, b));
    if (list.length > n) col.append(h("button", {class: "btn sm", type: "button", onclick: () => { SHOWN[b] = n + 300; render(); }, text: "Show " + Math.min(300, list.length - n) + " more"}));
    board.append(col);
  }
  main.append(board);
}
function viewActivity(main) {
  const ps = filtered(true), n = S.range === 7 ? 7 : 30;
  main.append(h("div", {class: "toolbar"}, h("span", {class: "count", text: "Activity across " + ps.length + " projects"}),
    h("div", {class: "seg", role: "group", "aria-label": "Date range"}, [7, 30].map(r => h("button", {type: "button", "aria-pressed": String(n === r), onclick: () => set({range: r}, true)}, "Last " + r + " days")))));
  main.append(activityChart(ps, n, "Commits per day"));
  const ks = days(n); const rows = ps.map(p => [p.id, ks.reduce((a, k) => a + (p.act[k] || 0), 0), p.id]).filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]).slice(0, 15);
  const grid = h("div", {class: "grid cols-2", style: "margin-top:16px"});
  grid.append(h("div", null, h("h2", {class: "sec", style: "margin-top:0"}, "Most active projects", h("span", {class: "label", text: "commits · " + n + " days"})),
    rows.length ? h("div", {class: "card flat"}, rankedBars(rows, v => String(v), id => openProject(id))) : empty("No commits in this range")),
    h("div", null, h("h2", {class: "sec", style: "margin-top:0"}, "Timeline", h("span", {class: "label", text: "sessions · ops · canary"})), timeline(ps, n, 200)));
  main.append(grid);
}
function fileTree() {
  const tree = {workspace: [], log: [], owners: new Map()};
  for (const f of FILES) {
    if (!f.path.includes("/")) { tree.workspace.push(f); continue; }
    if (f.path.startsWith("log/")) { tree.log.push(f); continue; }
    const p = projectFor(f.path);
    const pid = p ? p.id : f.path.replace(/^(projects|plans)\//, "").replace(/\.md$/, "").split("/").slice(0, 2).join("/");
    const owner = pid.includes("/") ? pid.slice(0, pid.lastIndexOf("/")) : "(other)";
    if (!tree.owners.has(owner)) tree.owners.set(owner, new Map());
    const pm = tree.owners.get(owner); if (!pm.has(pid)) pm.set(pid, []); pm.get(pid).push(f);
  }
  return tree;
}
function viewExplorer(main) {
  if (!FILES.length) { main.append(empty("The handoff repository has no Markdown files yet", "Run ", h("code", {text: "dev sync"}), " to generate the map and notes.")); return; }
  const tree = fileTree();
  const pick = S.f && FILEMAP.has(S.f) ? FILEMAP.get(S.f) : (FILEMAP.get("PROJECTS.md") || FILES[0]);
  const wrap = h("div", {class: "explorer"});
  const nav = h("nav", {class: "tree", "aria-label": "Handoff files"});
  const filt = h("input", {type: "search", placeholder: "Filter files…", "aria-label": "Filter files"});
  nav.append(filt);
  const listBox = h("div"); nav.append(listBox);
  const fileBtn = f => h("button", {class: "file", type: "button", "data-nav": "", "aria-current": String(f === pick), title: f.path, "data-path": f.path.toLowerCase(),
    onclick: () => set({f: f.path, fq: S.fq}, true)}, f.path.split("/").pop());
  const openState = store.get("devkit.tree", {});
  const group = (key, label, kids) => { const d = h("details", {open: openState[key] !== false || kids.some(k => k.contains && k.querySelector && k.querySelector('[aria-current="true"]'))}, h("summary", null, label), kids);
    d.addEventListener("toggle", () => { openState[key] = d.open; store.set("devkit.tree", openState); }); return d; };
  if (tree.workspace.length) listBox.append(group("ws", "Workspace", tree.workspace.map(fileBtn)));
  for (const [owner, projects] of Array.from(tree.owners.entries()).sort()) {
    const kids = [];
    for (const [pid, files] of Array.from(projects.entries()).sort()) kids.push(group("p:" + pid, pid.slice(pid.lastIndexOf("/") + 1) || pid, files.sort((a, b) => a.path.localeCompare(b.path)).map(fileBtn)));
    listBox.append(group("o:" + owner, owner, kids));
  }
  if (tree.log.length) listBox.append(group("log", "Activity log", tree.log.sort((a, b) => b.path.localeCompare(a.path)).map(fileBtn)));
  filt.addEventListener("input", debounce(() => { const v = filt.value.toLowerCase().trim();
    $$(".file", nav).forEach(b => { const f = FILEMAP.get(b.title); const hit = !v || b.dataset.path.includes(v) || (f && f.text.toLowerCase().includes(v)); b.hidden = !hit; });
    $$("details", nav).forEach(d => { if (v) d.open = !!d.querySelector(".file:not([hidden])"); }); }, 120));
  const viewer = h("article", {class: "viewer", "aria-label": "File viewer"});
  const fq = h("input", {type: "search", placeholder: "Find in file…", value: S.fq, "aria-label": "Find in file"});
  const cnt = h("span", {class: "t2 num"});
  const proj = projectFor(pick.path);
  viewer.append(h("div", {class: "vh"}, h("span", {class: "path", text: pick.path}), h("span", {class: "t2", text: "· " + rel(pick.mtime) + " · " + (pick.size / 1024).toFixed(1) + " KB"}),
    h("span", {class: "spacer"}), fq, cnt,
    h("button", {class: "btn sm", type: "button", onclick: () => copy(D.handoff + "/" + pick.path)}, "Copy path"),
    proj ? h("button", {class: "btn sm", type: "button", onclick: () => openProject(proj.id)}, "Open project") : null));
  const meta = D.files_meta || {};
  const body = h("div"); viewer.append(body);
  function paint() {
    clear(body);
    if (meta.capped) body.append(h("div", {class: "notice", style: "margin:12px 16px 0", text: meta.omitted + " older file(s) are not embedded (cap " + Math.round((meta.cap || 0) / 1e6) + " MB, newest first). Open them in " + D.handoff + "."}));
    const doc = md(pick.text); body.append(doc);
    const n = highlight(doc, fq.value, null); cnt.textContent = fq.value ? n + " match" + (n === 1 ? "" : "es") : "";
    const first = doc.querySelector("mark"); if (first) { first.classList.add("cur"); first.scrollIntoView({block: "center"}); }
  }
  fq.addEventListener("input", debounce(() => { S.fq = fq.value; history.replaceState(null, "", encode(S)); store.set("devkit.state", S); paint(); }, 150));
  fq.addEventListener("keydown", e => { if (e.key === "Enter") { const ms = $$("mark", body); if (!ms.length) return; const i = ms.findIndex(m => m.classList.contains("cur")); ms.forEach(m => m.classList.remove("cur")); const nx = ms[(i + (e.shiftKey ? -1 : 1) + ms.length) % ms.length]; nx.classList.add("cur"); nx.scrollIntoView({block: "center"}); } });
  paint();
  wrap.append(nav, viewer); main.append(wrap);
  const cur = $('.file[aria-current="true"]', nav); if (cur) cur.scrollIntoView({block: "nearest"});
}
function viewHealth(main) {
  const c = D.canary || {}, sum = c.summary || {}, recs = c.records || [];
  main.append(h("h2", {class: "sec", style: "margin-top:0"}, "Instruction canary", h("span", {class: "label", text: "replies starting with “DevKit ·” · 7 days"})));
  const agents = Object.keys(sum);
  if (!agents.length) main.append(empty("No tracked replies yet", "Claude and Codex replies in sessions under ", h("code", {text: D.dev_root}), " are checked after every turn."));
  else main.append(h("div", {class: "kpis"}, agents.map(a => { const [ok, tot] = sum[a], pct = Math.round(100 * ok / tot), st = pct >= 95 ? ["●", "good", "On track"] : pct >= 80 ? ["▲", "warning", "Slipping"] : ["⊘", "critical", "Not following"];
    return h("div", {class: "kpi"}, h("span", {class: "label", text: a[0].toUpperCase() + a.slice(1)}), h("div", {class: "val", text: pct + "%"}),
      h("div", {class: "meter", style: "margin:8px 0", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": a + " canary"}, h("i", {style: "width:" + pct + "%;background:var(--" + st[1] + ")"})),
      h("div", {class: "sub"}, h("span", {style: "color:var(--" + st[1] + ")", "aria-hidden": "true", text: st[0]}), st[2] + " · " + ok + "/" + tot)); })));
  const misses = recs.filter(r => !r.ok).slice(-50).reverse();
  if (misses.length) main.append(h("h2", {class: "sec"}, "Deviations", h("span", {class: "label", text: misses.length + " in 30 days"})),
    h("div", {class: "tablewrap"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["When", "Agent", "Project", "Reply began with"].map(x => h("th", {text: x})))),
      h("tbody", null, misses.map(r => h("tr", null, h("td", {text: fmt(r.ts)}), h("td", {text: r.agent}), h("td", {text: r.project || "—"}), h("td", {class: "t2", text: r.excerpt || ""})))))));
  const g = D.guard || [];
  main.append(h("h2", {class: "sec"}, "Guard decisions", h("span", {class: "label", text: g.length + " recent"})),
    g.length ? h("div", {class: "tablewrap"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["When", "Agent", "Decision", "Reason"].map(x => h("th", {text: x})))),
      h("tbody", null, g.slice().reverse().map(e => h("tr", null, h("td", {class: "num", text: e.ts}), h("td", {text: e.agent}), h("td", null, h("span", {class: "badge"}, h("span", {class: "ic", style: "color:var(--" + (e.decision === "deny" ? "critical" : "warning") + ")", text: e.decision === "deny" ? "⊘" : "?"}), e.decision)), h("td", null, inline(e.reason))))))) : h("p", {class: "t2", text: "The guard has not blocked anything recently."}));
  const drift = D.drift;
  main.append(h("h2", {class: "sec"}, "Configuration drift"));
  if (drift == null) main.append(h("p", {class: "t2"}, "No drift report in this registry. Run ", h("code", {text: "dev config diff"}), " to compare the devkit configuration with Claude Code and Codex."));
  else if (!drift.length) main.append(h("p", {class: "t2", text: "✓ Claude Code and Codex match the devkit configuration."}));
  else main.append(h("ul", {class: "attn"}, drift.map(d => h("li", {class: "k-layout"}, h("span", {class: "ic", text: "≠"}), h("span", null, typeof d === "string" ? d : [d.tool || d.target, d.key, (d.current !== undefined ? JSON.stringify(d.current) : ""), "→", (d.desired !== undefined ? JSON.stringify(d.desired) : "")].filter(Boolean).join(" "))))));
  const wts = []; for (const p of P) for (const w of p.wts) wts.push([p, w]);
  const recl = wts.filter(([, w]) => w.pr && /MERGED|CLOSED/.test(w.pr.state) && !w.active);
  main.append(h("h2", {class: "sec"}, "Worktrees", h("span", {class: "label", text: wts.length + " · " + recl.length + " reclaimable"})));
  if (!wts.length) main.append(h("p", {class: "t2", text: "No worktrees. Create one with dev wt new <type>/<topic> when work must run in parallel."}));
  else main.append(h("div", {class: "tablewrap"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Project", "Branch", "PR", "State", "Where", "Session", "Size"].map(x => h("th", {text: x})))),
    h("tbody", null, wts.sort((a, b) => (b[1].size_kb || 0) - (a[1].size_kb || 0)).map(([p, w]) => h("tr", {"data-nav": "", tabindex: "0", onclick: () => openProject(p.id)},
      h("td", {text: p.id}), h("td", {class: "mono", text: w.branch || "detached"}), h("td", {text: w.pr ? "#" + w.pr.number + " " + String(w.pr.state).toLowerCase() : "—"}),
      h("td", {text: w.missing ? "missing" : [w.modified ? w.modified + " modified" : "", w.untracked ? w.untracked + " untracked" : "", w.unpushed ? w.unpushed + " unpushed" : ""].filter(Boolean).join(" · ") || "clean"}),
      h("td", {text: w.location || ""}), h("td", null, w.active ? h("span", {class: "badge"}, h("span", {class: "dot live"}), "Live") : rel(w.last_activity)), h("td", {class: "num", text: kb(w.size_kb)})))))));
  const sizes = P.map(p => [p.id, (p.size_kb || 0) + p.wts.reduce((a, w) => a + (w.size_kb || 0), 0), p.id]).filter(r => r[1] > 0).sort((a, b) => b[1] - a[1]).slice(0, 12);
  main.append(h("h2", {class: "sec"}, "Disk", h("span", {class: "label", text: "checkout + worktrees"})),
    sizes.length ? h("div", {class: "card flat"}, rankedBars(sizes, kb, id => openProject(id))) : h("p", {class: "t2", text: "Sizes are measured by the hourly sync (dev sync --scheduled)."}),
    h("p", {class: "t2"}, "Reclaim safely with ", h("code", {text: "dev gc"}), " (dry run) and ", h("code", {text: "dev gc --apply"}), "."));
}

/* ---------------------------------------------------------------- global search */
let IDX = null;
function buildIndex() {
  IDX = [];
  for (const p of P) {
    IDX.push({type: "project", pid: p.id, title: p.id, text: [p.ns && p.ns.purpose, p.ns && p.ns.state, p.stack.join(" "), p.tags.join(" ")].filter(Boolean).join(" · ")});
    for (const [b, list] of [["Now", p.t.now], ["Next", p.t.next], ["Later", p.t.later], ["Done", p.t.done_recent]]) for (const t of list) IDX.push({type: "task", pid: p.id, title: t, text: b + " · " + p.id});
    for (const s of p.sessions) IDX.push({type: "session", pid: p.id, title: s, text: p.id});
    for (const pl of p.plans || []) IDX.push({type: "plan", pid: p.id, title: pl.title, text: pl.path, file: pl.path});
  }
  for (const f of FILES) { const pr = projectFor(f.path); IDX.push({type: f.path.startsWith("plans/") ? "plan" : f.path.startsWith("projects/") ? "note" : "file", pid: pr ? pr.id : "", title: f.path, text: f.text, file: f.path}); }
  for (const it of IDX) it.lc = (it.title + "\n" + it.text).toLowerCase();
}
function search(q) {
  if (!IDX) buildIndex(); const tk = tokens(q); if (!tk.length) return [];
  const allowed = new Set(filtered(false).map(p => p.id)); const out = [];
  for (const it of IDX) {
    if (it.pid && !allowed.has(it.pid) && facetCount()) continue;
    if (!tk.every(t => it.lc.includes(t))) continue;
    const tl = it.title.toLowerCase(); let score = tk.reduce((a, t) => a + (tl.includes(t) ? 3 : 1), 0) + ({project: 3, task: 2, note: 1.5, plan: 1.5, session: 1, file: 0.5}[it.type] || 0);
    out.push([score, it]);
  }
  return out.sort((a, b) => b[0] - a[0]).map(x => x[1]);
}
function plain(t) { return String(t || "").replace(/<!--[\s\S]*?-->/g, " ").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1").replace(/[*`]+/g, "").replace(/(^|\s)[-+]\s(\[[ xX~]\]\s)?/g, "$1").replace(/^#+\s*/gm, "").replace(/\s+/g, " ").trim(); }
function snippet(text, q) {
  text = plain(text); const tk = tokens(q); const lc = text.toLowerCase(); let i = -1; for (const t of tk) { i = lc.indexOf(t); if (i >= 0) break; }
  if (i < 0) return text.slice(0, 160); const s = Math.max(0, i - 70); return (s ? "…" : "") + text.slice(s, s + 180).replace(/\s+/g, " ") + (s + 180 < text.length ? "…" : "");
}
function viewSearch(main) {
  const res = search(S.q);
  const types = ["project", "task", "note", "plan", "session", "file"], counts = {}; for (const t of types) counts[t] = 0; for (const r of res) counts[r.type]++;
  const shown = res.filter(r => !S.types.length || S.types.includes(r.type));
  main.append(h("div", {class: "toolbar"}, h("span", {class: "count", text: res.length + " results for “" + S.q + "”"}),
    h("div", {class: "seg", role: "group", "aria-label": "Result types"}, types.filter(t => counts[t]).map(t => h("button", {type: "button", "aria-pressed": String(S.types.includes(t)),
      onclick: () => set({types: S.types.includes(t) ? S.types.filter(x => x !== t) : S.types.concat([t])}, true)}, t + "s " + counts[t])))));
  if (!shown.length) { main.append(empty("Nothing found", "Try fewer words, or clear filters. Search covers projects, tasks, notes, plans, sessions and every Markdown file in the handoff repo.")); return; }
  const box = h("div", {class: "results"});
  for (const r of shown.slice(0, 300)) {
    const el = h("button", {class: "res", type: "button", "data-nav": "", onclick: () => r.file ? set({view: "explorer", f: r.file, fq: S.q}, true) : openProject(r.pid, r.type === "task" ? "tasks" : null)},
      h("div", {class: "rt"}, h("span", {class: "rtype", text: r.type}), h("span", null, inline(r.type === "file" || r.type === "note" || r.type === "plan" ? r.title : r.title.slice(0, 200)))),
      h("div", {class: "rs"}, r.pid && r.type !== "project" ? h("span", {text: r.pid + " · "}) : null, document.createTextNode(snippet(r.text, S.q))));
    highlight(el, S.q); box.append(el);
  }
  if (shown.length > 300) box.append(h("p", {class: "t2", text: (shown.length - 300) + " more — refine the search."}));
  main.append(box);
}

/* ---------------------------------------------------------------- project drawer */
function openProject(id, section) { set({p: id}, true); if (section) setTimeout(() => { const el = $("#sec-" + section); if (el) el.scrollIntoView({block: "start"}); }, 60); }
function closeDrawer() { set({p: ""}, true); }
let lastFocus = null;
function renderDrawer() {
  const dr = $("#drawer"), scrim = $("#scrim"); const p = BYID.get(S.p);
  if (!p) { if (!dr.hidden) { dr.classList.remove("on"); scrim.classList.remove("on"); setTimeout(() => { dr.hidden = true; scrim.hidden = true; }, 200); if (lastFocus) lastFocus.focus(); } return; }
  if (dr.hidden) lastFocus = document.activeElement;
  clear(dr); dr.hidden = false; scrim.hidden = false; requestAnimationFrame(() => { dr.classList.add("on"); scrim.classList.add("on"); });
  const ns = p.ns || {}, m = p.main || {};
  const sect = (id, title, ...body) => h("section", {class: "sect card flat", id: "sec-" + id}, h("h3", {text: title}), ...body);
  const head = h("div", {class: "dh"}, h("div", {style: "flex:1;min-width:0"}, h("div", {class: "t2", style: "font-size:.8rem", text: p.owner}), h("h2", {id: "drawer-title", text: p.name || p.id}),
    h("div", {style: "display:flex;flex-wrap:wrap;gap:6px;margin-top:8px"}, statusBadge(p.status), prioBadge(p.priority), ns.milestone ? h("span", {class: "badge", text: "◆ " + ns.milestone}) : null, ns.due ? h("span", {class: "badge", text: "due " + ns.due}) : null,
      p.tags.map(t => h("span", {class: "stack", text: "#" + t})), p.live ? h("span", {class: "badge"}, h("span", {class: "dot live"}), "Live session") : null)),
    h("button", {class: "btn", type: "button", onclick: closeDrawer, "aria-label": "Close (Esc)"}, "✕"));
  const body = h("div", {class: "db"});
  if (p.status_line) body.append(h("div", {class: "statusline"}, h("span", {class: "who", text: "Latest status · " + (p.status_line.agent || "agent") + " · " + rel(p.status_line.ts) + (p.status_line.branch ? " · " + p.status_line.branch : "")}), inline(stripMarker(p.status_line.line))));
  body.append(sect("go", "Work on it", h("div", {class: "cmds"}, cmdRow("dcd " + p.id), cmdRow("dev claude " + p.id), cmdRow("dev codex " + p.id), cmdRow("dev task add \"…\" --now -p " + p.id), cmdRow("dev handoff " + p.id), cmdRow("cd " + (p.path || "")))));
  if (!p.ns) body.append(sect("note", "Handoff note", h("p", {class: "t2"}, "No handoff note yet. ", h("code", {text: "dev sync"}), " creates it; agents fill it in with the devkit-handoff skill.")));
  if (ns.purpose) body.append(sect("purpose", "Purpose", md(ns.purpose)));
  if (ns.state) body.append(sect("state", "Current state", md(ns.state)));
  const tlist = (label, items, cls) => items.length ? [h("div", {class: "label", style: "margin:8px 0 4px", text: label + " · " + items.length}), h("ul", {class: "tasklist"}, items.map(t => taskLine(t, cls)))] : null;
  body.append(sect("tasks", "Tasks", p.t.open + p.t.done ? [tlist("Now", p.t.now, "doing"), tlist("Next", p.t.next, ""), tlist("Later", p.t.later, ""), tlist("Done (recent)", p.t.done_recent, "done")] : h("p", {class: "t2"}, "No tasks. ", h("code", {text: "dev task add \"…\" --now -p " + p.id}))));
  if (ns.decisions) body.append(sect("decisions", "Decisions and gotchas", md(ns.decisions)));
  if (ns.links) body.append(sect("links", "Links", md(ns.links)));
  if ((p.plans || []).length) body.append(sect("plans", "Plans", h("ul", {class: "tasklist"}, p.plans.map(pl => h("li", null, h("a", {href: encode(Object.assign({}, S, {view: "explorer", f: pl.path, p: ""})), text: pl.title}), h("span", {class: "t2", text: " · " + rel(pl.mtime)}))))));
  body.append(sect("git", "Repository", h("div", {class: "gitline"}, h("span", {class: "mono", text: "⎇ " + (m.branch || "—")}), m.last_commit ? h("span", {text: "last commit " + rel(m.last_commit_ts) + " · " + m.last_commit}) : null),
    p.remote ? h("p", {class: "t2", style: "margin:6px 0 0"}, p.github ? h("a", {href: "https://github.com/" + p.github, target: "_blank", rel: "noopener noreferrer", text: p.remote}) : p.remote) : null,
    !p.canonical_ok && p.canonical ? h("p", {class: "t2"}, "Not at its canonical path (", h("code", {text: p.canonical}), ") — ", h("code", {text: "dev adopt " + p.path})) : null,
    (p.commands || []).length ? h("div", {style: "margin-top:8px"}, p.commands.map(c => h("code", {style: "margin-right:8px", text: c}))) : null,
    h("div", {style: "margin-top:10px"}, sparkline(p, 30))));
  if ((p.prs_open || []).length) body.append(sect("prs", "Open pull requests", h("ul", {class: "tasklist"}, p.prs_open.map(pr => h("li", null, p.github ? h("a", {href: "https://github.com/" + p.github + "/pull/" + pr.number, target: "_blank", rel: "noopener noreferrer", text: "#" + pr.number}) : "#" + pr.number, " ", h("span", {text: pr.title || ""}), pr.draft ? h("span", {class: "badge", text: "draft"}) : null, h("span", {class: "t2 mono", text: " " + (pr.branch || "")}))))));
  if (p.wts.length) body.append(sect("worktrees", "Worktrees", h("div", {class: "tablewrap"}, h("table", {class: "t"}, h("thead", null, h("tr", null, ["Branch", "PR", "State", "Session", "Size"].map(x => h("th", {text: x})))),
    h("tbody", null, p.wts.map(w => h("tr", null, h("td", {class: "mono", text: w.branch || "detached"}), h("td", {text: w.pr ? "#" + w.pr.number + " " + String(w.pr.state).toLowerCase() : "—"}), h("td", {text: w.missing ? "missing" : ((w.modified || 0) + (w.untracked || 0)) ? ((w.modified || 0) + (w.untracked || 0)) + " changed" : "clean"}), h("td", {text: w.active ? "live" : rel(w.last_activity)}), h("td", {class: "num", text: kb(w.size_kb)}))))))));
  if (p.sessions.length) body.append(sect("sessions", "Session log", h("ul", {class: "tasklist"}, p.sessions.map(s => h("li", null, h("span", {class: "t2 num", style: "flex:none", text: s.slice(0, 16)}), h("span", null, inline(sessText(s))))))));
  if (p.note) body.append(h("p", {class: "t2"}, "Source: ", h("a", {href: encode(Object.assign({}, S, {view: "explorer", f: p.note, p: ""})), text: p.note}), " in " + D.handoff));
  dr.append(head, body);
  head.lastChild.focus({preventScroll: true});
}

/* ---------------------------------------------------------------- command palette */
function fuzzy(q, s) {
  const toks = String(q || "").toLowerCase().split(/\s+/).filter(Boolean); if (!toks.length) return 1;
  let total = 0; s = s.toLowerCase();
  for (const t of toks) { const sc = fuzzy1(t, s); if (!sc) return 0; total += sc; }
  return total;
}
function fuzzy1(q, s) {
  const i = s.indexOf(q); if (i >= 0) return 100 - Math.min(i, 80) + (i === 0 ? 20 : 0);
  let qi = 0, score = 0, prev = -2; for (let j = 0; j < s.length && qi < q.length; j++) if (s[j] === q[qi]) { score += prev === j - 1 ? 3 : 1; prev = j; qi++; }
  return qi === q.length ? score : 0;
}
function paletteItems() {
  const items = [];
  for (const [v, label, key] of VIEWS) items.push({grp: "View", label: "Go to " + label, hint: "g " + key, run: () => go(v)});
  items.push({grp: "Action", label: "Toggle theme (light/dark)", run: toggleTheme}, {grp: "Action", label: "Theme: follow system", run: () => { store.set("devkit.theme", null); try { localStorage.removeItem("devkit.theme"); } catch (e) {} delete document.documentElement.dataset.theme; }},
    {grp: "Action", label: "Toggle dense mode", run: () => set({dense: !S.dense}, true)}, {grp: "Action", label: "Projects as table", run: () => go("projects", {layout: "table"})},
    {grp: "Action", label: "Projects as cards", run: () => go("projects", {layout: "cards"})}, {grp: "Action", label: "Clear search and filters", run: () => set({q: "", status: [], priority: [], owner: [], stack: [], tag: [], live: false, prs: false, attn: false}, true)},
    {grp: "Action", label: "Keyboard shortcuts", hint: "?", run: openSheet}, {grp: "Action", label: "Print this view", run: () => window.print()});
  for (const p of P) {
    items.push({grp: "Project", label: p.id, hint: p.status, run: () => openProject(p.id)});
    for (const [l, c] of [["Copy: dcd ", "dcd "], ["Copy: dev claude ", "dev claude "], ["Copy: dev codex ", "dev codex "], ["Copy: dev handoff ", "dev handoff "]]) items.push({grp: "Command", label: l + p.id, run: () => copy(c + p.id)});
    items.push({grp: "Command", label: "Copy: dev task add … -p " + p.id, run: () => copy("dev task add \"…\" --now -p " + p.id)});
    for (const t of p.t.now.concat(p.t.next).slice(0, 40)) items.push({grp: "Task", label: t, hint: p.id, run: () => openProject(p.id, "tasks")});
  }
  for (const f of FILES) items.push({grp: "File", label: f.path, run: () => set({view: "explorer", f: f.path, fq: "", p: ""}, true)});
  return items;
}
let PAL = null;
function openPalette() {
  const pal = $("#palette"); if (!pal.hidden) return; lastFocus = document.activeElement;
  const all = paletteItems(); let sel = 0, shown = [];
  const input = h("input", {type: "text", placeholder: "Jump to a project, file, task or view… or run an action", "aria-label": "Command", "aria-controls": "pal-list", role: "combobox", "aria-expanded": "true", autocomplete: "off"});
  const list = h("ul", {id: "pal-list", role: "listbox"});
  clear(pal).append(input, list, h("div", {class: "ft"}, h("span", null, h("kbd", {text: "↑↓"}), " move"), h("span", null, h("kbd", {text: "↵"}), " run"), h("span", null, h("kbd", {text: "esc"}), " close")));
  pal.hidden = false; $("#scrim").hidden = false; $("#scrim").classList.add("on");
  function draw() {
    const q = input.value.trim();
    shown = all.map(it => [fuzzy(q, it.label + " " + (it.hint || "")), it]).filter(x => x[0] > 0).sort((a, b) => b[0] - a[0]).slice(0, 60).map(x => x[1]);
    sel = Math.min(sel, Math.max(0, shown.length - 1)); clear(list);
    shown.forEach((it, i) => { const li = h("li", {role: "option", id: "pal-" + i, "aria-selected": String(i === sel), onclick: () => run(i), onmousemove: () => { if (sel !== i) { sel = i; paint(); } }}, h("span", null, inline(it.label)), it.hint ? h("span", {class: "t2", text: it.hint}) : null, h("span", {class: "grp", text: it.grp})); list.append(li); });
    if (!shown.length) list.append(h("li", {class: "t2", text: "No match"}));
    paint();
  }
  function paint() { $$("li[role=option]", list).forEach((li, i) => li.setAttribute("aria-selected", String(i === sel))); input.setAttribute("aria-activedescendant", "pal-" + sel); const cur = $("#pal-" + sel); if (cur) cur.scrollIntoView({block: "nearest"}); }
  function run(i) { const it = shown[i]; closePalette(); if (it) it.run(); }
  input.addEventListener("input", () => { sel = 0; draw(); });
  input.addEventListener("keydown", e => { if (e.key === "ArrowDown") { e.preventDefault(); sel = Math.min(shown.length - 1, sel + 1); paint(); } else if (e.key === "ArrowUp") { e.preventDefault(); sel = Math.max(0, sel - 1); paint(); } else if (e.key === "Enter") { e.preventDefault(); run(sel); } });
  draw(); input.focus(); PAL = pal;
}
function closePalette() { const pal = $("#palette"); if (pal.hidden) return; pal.hidden = true; if (!S.p) { $("#scrim").classList.remove("on"); $("#scrim").hidden = true; } if (lastFocus && lastFocus.focus) lastFocus.focus(); }
function openSheet() {
  const sh = $("#sheet"); const rows = [["/", "Search"], ["⌘K · Ctrl K", "Command palette"], ["g o / p / t / a / e / h", "Overview · Projects · Tasks · Activity · Explorer · Health"], ["j / k", "Next / previous item"], ["↵", "Open the focused item"], ["Esc", "Close drawer, palette or clear search"], ["← →", "Read the chart day by day (chart focused)"], ["?", "This sheet"]];
  clear(sh).append(h("h2", {text: "Keyboard shortcuts"}), h("dl", null, rows.map(([k, v]) => [h("dt", null, h("kbd", {text: k})), h("dd", {style: "margin:0", text: v})])), h("p", {class: "t2", style: "margin:14px 0 0;font-size:.8rem", text: "State lives in the URL: bookmark or share any view. The page reloads itself every 2 minutes while visible."}),
    h("div", {style: "text-align:right;margin-top:10px"}, h("button", {class: "btn", type: "button", onclick: closeSheet}, "Close")));
  sh.hidden = false; $("#scrim").hidden = false; $("#scrim").classList.add("on"); $("button", sh).focus();
}
function closeSheet() { $("#sheet").hidden = true; if (!S.p && $("#palette").hidden) { $("#scrim").classList.remove("on"); $("#scrim").hidden = true; } }

/* ---------------------------------------------------------------- navigation, theme, keyboard */
function go(view, patch) { set(Object.assign({view, p: ""}, patch || {}), true); window.scrollTo(0, 0); }
function toggleTheme() {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark"; document.documentElement.dataset.theme = next; store.set("devkit.theme", next);
  const b = $("#theme"); if (b) b.setAttribute("aria-label", "Switch to " + (next === "dark" ? "light" : "dark") + " theme");
  render();
}
let gAt = 0;
document.addEventListener("keydown", e => {
  const tag = (e.target && e.target.tagName) || "", typing = /^(INPUT|TEXTAREA|SELECT)$/.test(tag) || (e.target && e.target.isContentEditable);
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); if ($("#palette").hidden) openPalette(); else closePalette(); return; }
  if (e.key === "Escape") {
    if (!$("#palette").hidden) { closePalette(); return; } if (!$("#sheet").hidden) { closeSheet(); return; } if (S.p) { closeDrawer(); return; }
    if (e.target.id === "q" && S.q) { set({q: "", view: S.view === "search" ? (store.get("devkit.prevView", "overview")) : S.view}, true); return; }
    if (typing) e.target.blur(); return;
  }
  if (!$("#drawer").hidden && e.key === "Tab") { const f = $$("#drawer button, #drawer a[href], #drawer [tabindex]").filter(x => !x.disabled); if (f.length) { const i = f.indexOf(document.activeElement); if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); } else if (!e.shiftKey && i === f.length - 1) { e.preventDefault(); f[0].focus(); } } }
  if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "/") { e.preventDefault(); $("#q").focus(); $("#q").select(); return; }
  if (e.key === "?") { e.preventDefault(); openSheet(); return; }
  if (gAt && Date.now() - gAt < 1200) { gAt = 0; const v = VIEWS.find(x => x[2] === e.key.toLowerCase()); if (v) { e.preventDefault(); go(v[0]); } return; }
  if (e.key === "g") { gAt = Date.now(); return; }
  if (e.key === "j" || e.key === "k") {
    e.preventDefault(); const scope = !$("#drawer").hidden ? $("#drawer") : $("#view");
    const items = $$("[data-nav]", scope).filter(x => x.offsetParent !== null); if (!items.length) return;
    const i = items.indexOf(document.activeElement); const n = items[i < 0 ? 0 : Math.max(0, Math.min(items.length - 1, i + (e.key === "j" ? 1 : -1)))];
    n.focus(); n.scrollIntoView({block: "nearest"});
  }
});

/* ---------------------------------------------------------------- render */
function render() {
  document.body.classList.toggle("dense", !!S.dense);
  renderHeader(); renderFilters();
  const main = clear($("#view"));
  ({overview: viewOverview, projects: viewProjects, tasks: viewTasks, activity: viewActivity, explorer: viewExplorer, health: viewHealth, search: viewSearch}[S.view] || viewOverview)(main);
  renderDrawer();
  tick();
}
function tick() { const t = $("#ticker"); if (t) { t.textContent = "Updated " + rel(D.generated); t.title = "Last sync " + fmt(D.generated) + " on " + D.host; } }

/* ---------------------------------------------------------------- boot */
(function boot() {
  const app = $("#app-top");
  app.append(
    h("button", {class: "brand", type: "button", onclick: () => go("overview"), "aria-label": "devkit — overview"}),
    h("nav", {class: "tabs", id: "tabs", role: "tablist", "aria-label": "Views"}),
    h("span", {class: "spacer"}),
    h("div", {class: "search", role: "search"}, h("span", {class: "ico", "aria-hidden": "true", text: "⌕"}), h("input", {id: "q", type: "search", placeholder: "Search projects, tasks, notes, plans…", "aria-label": "Search everything", autocomplete: "off"}), h("kbd", {text: "/"})),
    h("button", {class: "btn", id: "palbtn", type: "button", onclick: openPalette, "aria-label": "Command palette", title: "Command palette (⌘K)"}, h("kbd", {text: "⌘K"})),
    h("div", {class: "meta"}, h("button", {class: "pill", id: "canary", type: "button", onclick: () => go("health")}), h("span", {class: "tick", id: "ticker"})),
    h("button", {class: "btn ghost", id: "theme", type: "button", onclick: toggleTheme, "aria-label": "Toggle theme", title: "Toggle theme"}, "◐"));
  const brand = $(".brand", app); brand.append($("#spark-tpl").content.cloneNode(true)); brand.append(h("span", {class: "word", text: "devkit"}), h("span", {class: "sub", text: "Project command centre"}));
  const q = $("#q");
  q.addEventListener("input", debounce(() => {
    const v = q.value.trim();
    if (v && !["projects", "tasks", "search", "activity"].includes(S.view)) { store.set("devkit.prevView", S.view); set({q: v, view: "search"}, true); }
    else if (!v && S.view === "search") set({q: "", view: store.get("devkit.prevView", "overview")}, true);
    else set({q: v});
  }, 140));
  q.addEventListener("keydown", e => { if (e.key === "Enter") { const first = $("[data-nav]", $("#view")); if (first) first.focus(); } });
  document.addEventListener("click", e => { $$(".facet[open]").forEach(d => { if (!d.contains(e.target)) d.open = false; }); }, true);
  $("#scrim").addEventListener("click", () => { if (!$("#palette").hidden) closePalette(); else if (!$("#sheet").hidden) closeSheet(); else closeDrawer(); });
  const st = decode(location.hash) || store.get("devkit.state", null);
  if (st) S = Object.assign({}, DEFAULT, st); if (!location.hash) history.replaceState(null, "", encode(S));
  render();
  try { const y = sessionStorage.getItem("devkit.scroll"); if (y) { window.scrollTo(0, +y); sessionStorage.removeItem("devkit.scroll"); } } catch (e) {}
  setInterval(tick, 30000);
  setInterval(() => { if (document.visibilityState === "visible" && $("#palette").hidden && document.activeElement !== q && !/^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "")) {
    try { sessionStorage.setItem("devkit.scroll", String(window.scrollY)); } catch (e) {} location.reload(); } }, 120000);
})();
})();
"""

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="devkit __VERSION__">
<title>devkit · Project command centre</title>
<link rel="icon" href="__FAVICON__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Manrope:wght@300..800&display=swap">
<script>(function(){try{var t=JSON.parse(localStorage.getItem("devkit.theme"));if(t==="light"||t==="dark")document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>
:root{__LIGHT__}
@media (prefers-color-scheme: dark){:root:where(:not([data-theme="light"])){__DARK__}}
:root[data-theme="dark"]{__DARK__}
:root[data-theme="light"]{__LIGHT__}
__CSS__
</style>
</head>
<body>
<a class="skip" href="#view">Skip to content</a>
<template id="spark-tpl">__SPARK__</template>
<header class="top" id="app-top" role="banner"></header>
<div class="filters" id="filters" role="region" aria-label="Filters"></div>
<main id="view" tabindex="-1" aria-live="polite"></main>
<footer class="foot">__SPARK__ <span>__COPYRIGHT__</span><span>devkit __VERSION__</span><span>Generated by devkit on __HOST__ · __WHEN__</span><span>Data: __HANDOFF__</span><span>Press <kbd>?</kbd> for shortcuts</span></footer>
<div class="scrim" id="scrim" hidden></div>
<aside class="drawer" id="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" hidden></aside>
<div class="palette" id="palette" role="dialog" aria-modal="true" aria-label="Command palette" hidden></div>
<div class="sheet" id="sheet" role="dialog" aria-modal="true" aria-label="Keyboard shortcuts" hidden></div>
<div class="tip" id="tip" role="tooltip" hidden></div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script id="devkit-data" type="application/json">__DATA__</script>
<script>__JS__</script>
</body>
</html>
"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def render(reg: dict, handoff: Path = None, now: float = None) -> str:
    """The complete, self-contained dashboard HTML for a registry."""
    data = collect(reg, handoff=handoff, now=now)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["generated"]))
    page = HTML
    for key, val in (("__LIGHT__", LIGHT), ("__DARK__", DARK), ("__CSS__", CSS), ("__SPARK__", SPARK),
                     ("__FAVICON__", FAVICON), ("__VERSION__", _esc(core.VERSION)), ("__COPYRIGHT__", _esc(core.COPYRIGHT)),
                     ("__HOST__", _esc(data["host"])), ("__WHEN__", _esc(when)), ("__HANDOFF__", _esc(data["handoff"]))):
        page = page.replace(key, val)
    # data and script last, so placeholders inside them are never substituted
    return page.replace("__JS__", JS.replace("</", "<\\/")).replace("__DATA__", safe_json(data))


def write(reg: dict, handoff: Path = None) -> Path:
    """Render to ~/.local/state/devkit/dashboard/index.html (atomic) and return the path."""
    out = core.STATE / "dashboard" / "index.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".html.tmp")
    tmp.write_text(render(reg, handoff=handoff), encoding="utf-8")
    os.replace(tmp, out)
    return out


def open_in_browser(path) -> None:
    """Open the dashboard in the default browser (macOS `open`)."""
    subprocess.run(["open", str(path)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
