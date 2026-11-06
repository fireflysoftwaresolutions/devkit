# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Progress, timing and predictability for agent sessions.

Runs     STATE/runs/<agent>-<session>.json — the piece of work a session is doing: goal, steps (k of n), estimate,
         updates, state (running | blocked | done), ETA. Written by `dev progress`, read by hooks, `dev progress`,
         presence (other machines) and the dashboard.
Timing   STATE/timing/<YYYY-MM>.jsonl — one record per slow tool call, turn and subagent:
         {ts, kind: tool|turn|subagent|run, agent, session, project, what, secs[, estimate]}.
Calibration: finished runs record estimate vs actual, so the next estimate is corrected (`dev time`).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from pathlib import Path

from . import core, ui
from .core import STATE

RUNS = STATE / "runs"
TIMING = STATE / "timing"
PENDING = STATE / "timing" / "pending"
SESSIONS = STATE / "sessions"


def cfg() -> dict:
    return {"nudge_minutes": 8, "notify": True, "notify_after_minutes": 10, "slow_tool_seconds": 20,
            **(core.CFG.get("progress") or {})}


# --------------------------------------------------------------------------- which session am I?

def current_session(cwd: str = None):
    """-> (agent, session id) of the agent session running this command.

    Claude Code exports DEVKIT_SESSION through CLAUDE_ENV_FILE at session start. Otherwise (Codex, or a shell)
    the most recently active devkit session in the same repository is used."""
    env = os.environ.get("DEVKIT_SESSION", "")
    if ":" in env:
        agent, sid = env.split(":", 1)
        return agent, sid
    cwd = cwd or os.getcwd()
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
    best = None
    for f in SESSIONS.glob("*.json") if SESSIONS.exists() else []:
        s = core.read_json(f, {}) or {}
        if (s.get("top") or s.get("cwd")) == top and (not best or f.stat().st_mtime > best[0]):
            best = (f.stat().st_mtime, s.get("agent"), s.get("session"))
    if best and time.time() - best[0] < 12 * 3600:
        return best[1], best[2]
    return os.environ.get("DEVKIT_AGENT") or "human", f"shell-{os.getppid()}"


def run_path(agent: str, sid: str) -> Path:
    return RUNS / f"{agent}-{sid}.json"


def load_run(agent: str, sid: str):
    return core.read_json(run_path(agent, sid), None)


def _save(run: dict):
    p = run_path(run["agent"], run["session"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(run, indent=1))
    sf = SESSIONS / f"{run['agent']}-{run['session']}.json"
    if sf.exists():  # the session's last update to the developer
        s = core.read_json(sf, {}) or {}
        s["last_update"] = time.time()
        sf.write_text(json.dumps(s))


# --------------------------------------------------------------------------- estimates and history

def parse_minutes(s) -> float:
    """'45m', '1h30m', '2h', '90' -> minutes."""
    if s is None or s == "":
        return 0.0
    s = str(s).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    total = 0.0
    for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*([hm])", s):
        total += float(n) * (60 if u == "h" else 1)
    return total


def fmt_min(m: float) -> str:
    m = max(0, round(m))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def records(days: int = 30, kind: str = None, project: str = None) -> list:
    out, since = [], time.time() - days * 86400
    for f in sorted(TIMING.glob("*.jsonl")) if TIMING.exists() else []:
        for ln in f.read_text().splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("ts", 0) >= since and (not kind or r.get("kind") == kind) and \
                    (not project or r.get("project") == project):
                out.append(r)
    return out


def record(kind: str, agent: str, sid: str, what: str, secs: float, project: str = "", **extra):
    TIMING.mkdir(parents=True, exist_ok=True)
    r = {"ts": round(time.time()), "kind": kind, "agent": agent, "session": sid, "project": project,
         "what": (what or "")[:160], "secs": round(secs, 1), **extra}
    with (TIMING / f"{time.strftime('%Y-%m')}.jsonl").open("a") as fh:
        fh.write(json.dumps(r) + "\n")


def calibration(project: str = None) -> dict:
    """How long things really take here: estimate accuracy, turn length, slow commands."""
    runs = [r for r in records(60, "run", project) if r.get("estimate")]
    ratios = [r["secs"] / 60 / r["estimate"] for r in runs if r["estimate"] > 0]
    turns = [r["secs"] / 60 for r in records(30, "turn", project)]
    return {"runs": len(runs), "ratio": round(statistics.median(ratios), 2) if ratios else None,
            "turn_median": round(statistics.median(turns), 1) if turns else None,
            "turn_p90": round(sorted(turns)[int(0.9 * (len(turns) - 1))], 1) if turns else None}


def eta(run: dict, now: float = None) -> float:
    """Minutes left: the observed pace per step, blended with the (calibrated) estimate."""
    now = now or time.time()
    elapsed = (now - run["started"]) / 60
    n, k = run.get("steps") or 0, run.get("step") or 0
    est = run.get("estimate_cal") or run.get("estimate") or 0
    by_est = max(0.0, est - elapsed) if est else None
    by_pace = (n - k) * elapsed / k if n and k else None
    if by_pace is not None and by_est is not None:
        w = min(1.0, k / max(1, n) * 1.5)  # trust the pace more as steps complete
        return w * by_pace + (1 - w) * by_est
    return by_pace if by_pace is not None else (by_est if by_est is not None else 0.0)


# --------------------------------------------------------------------------- the commands agents run

def _project(cwd=None) -> str:
    try:
        from .registry import main_of
        top = core.gitout(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
        if not top:
            return ""
        m = main_of(Path(top)) or Path(top)
        return core.project_identity(m, core.gitout(m, "config", "--get", "remote.origin.url"))[0]
    except Exception:
        return ""


def headline(run: dict, now: float = None) -> str:
    now = now or time.time()
    elapsed = (now - run["started"]) / 60
    n, k = run.get("steps") or 0, run.get("step") or 0
    parts = [f"{k}/{n}" if n else f"step {k}", f"{fmt_min(elapsed)} elapsed"]
    if run.get("estimate"):
        parts[-1] += f" of ~{fmt_min(run.get('estimate_cal') or run['estimate'])}"
    if run.get("state") == "running":
        parts.append(f"ETA ~{fmt_min(eta(run, now))}")
    else:
        parts.append(run.get("state", ""))
    return " · ".join(parts)


def start(goal: str, steps: int = 0, estimate: str = "", cwd: str = None, plan=None) -> dict:
    agent, sid = current_session(cwd)
    project = (core.read_json(SESSIONS / f"{agent}-{sid}.json", {}) or {}).get("project") or _project(cwd)
    est = parse_minutes(estimate)
    cal = calibration(project)
    run = {"agent": agent, "session": sid, "project": project, "goal": goal, "steps": steps, "step": 0,
           "estimate": est, "estimate_cal": round(est * cal["ratio"], 1) if est and cal["ratio"] else est,
           "started": time.time(), "state": "running", "updates": [], "plan": list(plan or [])}
    _save(run)
    _publish()
    return run


def step(text: str, nxt: str = "", cwd: str = None, kind: str = "step") -> dict:
    agent, sid = current_session(cwd)
    run = load_run(agent, sid)
    if not run:
        run = start(text if kind != "step" else "work in progress", cwd=cwd)
    if kind == "step":
        run["step"] = (run.get("step") or 0) + 1
        if run.get("steps") and run["step"] > run["steps"]:
            run["steps"] = run["step"]
        run["state"] = "running"
    elif kind in ("blocked", "done"):
        run["state"] = kind
        if kind == "done":
            run["finished"] = time.time()
            if run.get("steps"):
                run["step"] = run["steps"]
            record("run", agent, sid, run.get("goal", ""), time.time() - run["started"], run.get("project", ""),
                   estimate=run.get("estimate") or 0, steps=run.get("steps") or 0)
    run["updates"].append({"ts": round(time.time()), "kind": kind, "text": text, "next": nxt})
    run["updates"] = run["updates"][-40:]
    _save(run)
    _publish()
    if kind in ("blocked", "done"):
        notify(f"{run.get('project') or 'devkit'} · {kind}", text)
    return run


def render_update(run: dict) -> str:
    """The progress update in the communication template, ready to paste."""
    last = run["updates"][-1] if run.get("updates") else {}
    head = {"done": "Done", "blocked": "Blocked"}.get(run.get("state"), "Progress")
    lines = [f"**{head} · {run.get('goal', '')}** — {headline(run)}"]
    if run.get("plan"):  # named steps: a checklist
        k = run.get("step") or 0
        for i, name in enumerate(run["plan"], 1):
            mark = "✓" if i <= k else ("▸" if i == k + 1 and run.get("state") == "running" else "○")
            lines.append(f"  {mark} {i}. {name}")
    done = [u["text"] for u in run.get("updates", []) if u["kind"] == "step"][-3:]
    if done:
        lines.append("- Done: " + "; ".join(done))
    if last.get("next"):
        lines.append(f"- Next: {last['next']}")
    if run.get("state") == "blocked":
        lines.append(f"- Blocked: {last.get('text', '')}")
    return "\n".join(lines)


def _publish():
    """Push the new state to the team (presence + session line), debounced, in the background."""
    try:
        from . import hooks
        mark = STATE / "progress.publish"
        if mark.exists() and time.time() - mark.stat().st_mtime < 60:
            return
        mark.touch()
        agent, sid = current_session()
        hooks._spawn_sync(["--session", f"{agent}:{sid}", "--offline"])
    except Exception:
        pass


def notify(title: str, text: str):
    """macOS notification (proactive updates for the developer). Never fails."""
    if not cfg().get("notify") or os.environ.get("DEVKIT_NO_NOTIFY"):
        return
    try:
        core.sh(["osascript", "-e", f"display notification {json.dumps(text[:180])} with title "
                 f"{json.dumps('devkit · ' + title[:60])}"], timeout=5)
    except Exception:
        pass


def active_runs(max_age_h: float = 12) -> list:
    out = []
    for f in RUNS.glob("*.json") if RUNS.exists() else []:
        r = core.read_json(f, None)
        if r and time.time() - f.stat().st_mtime < max_age_h * 3600:
            out.append(r)
    return sorted(out, key=lambda r: -r.get("started", 0))


def brief(run: dict) -> dict:
    """Compact form for presence/dashboard."""
    last = run["updates"][-1] if run.get("updates") else {}
    return {"goal": run.get("goal"), "state": run.get("state"), "step": run.get("step"), "steps": run.get("steps"),
            "estimate": run.get("estimate"), "started": round(run.get("started", 0)),
            "eta_min": round(eta(run)) if run.get("state") == "running" else None, "last": last.get("text", ""),
            "next": last.get("next", ""), "updated": last.get("ts")}


# --------------------------------------------------------------------------- hooks

STATUS_Q = re.compile(r"(c[oó]mo (va|vas|vamos|lo llevas)|qu[eé] (te )?falta|en qu[eé] (estado|punto)|"
                      r"how('?s| is) it going|where are we|what'?s left|status\??$|progress\??$|\beta\b|"
                      r"dame un resumen|give me (a|an) (summary|update)|update\?$|novedades)", re.I)


def _sess(agent, sid):
    f = SESSIONS / f"{agent}-{sid}.json"
    return f, (core.read_json(f, {}) or {}) if f.exists() else {}


def on_prompt(agent: str, data: dict):
    """UserPromptSubmit: start the turn clock; answer status questions from real progress data."""
    sid = data.get("session_id") or "-"
    f, s = _sess(agent, sid)
    now = time.time()
    if f.exists():
        s.update({"turn_started": now, "last_update": now, "nudges": 0})
        f.write_text(json.dumps(s))
    prompt = (data.get("prompt") or "")[:400]
    if not STATUS_Q.search(prompt):
        return None
    runs = [r for r in active_runs() if r.get("session") == sid] or active_runs()[:3]
    ctx = ["devkit · the developer is asking for status. Answer with the Overall status template of the "
           "`communicating` skill: one-line verdict, then per workstream: state, evidence, ETA; what is left; risks; "
           "what you need from them. Use these real records:"]
    for r in runs:
        ctx.append(f"- run “{r.get('goal')}” ({r.get('agent')}): {headline(r)}; last: "
                   f"{(r.get('updates') or [{}])[-1].get('text', '-')}")
    subs = s.get("subagents") or {}
    for k, v in subs.items():
        ctx.append(f"- subagent {v.get('type', '')} {k[:8]} running for {fmt_min((now - v.get('started', now)) / 60)}")
    if len(ctx) == 1:
        ctx.append("- no progress records in this session: reconstruct from your work so far, and start one "
                   "(`dev progress start \"<goal>\" --steps N --estimate 30m`) for long work.")
    return "\n".join(ctx)


def tool_started(data: dict):
    tid = data.get("tool_use_id") or ""
    if not tid:
        return
    PENDING.mkdir(parents=True, exist_ok=True)
    inp = data.get("tool_input") or {}
    what = inp.get("command") or inp.get("description") or inp.get("file_path") or ""
    (PENDING / f"{re.sub(r'[^A-Za-z0-9_-]', '', tid)[:80]}.json").write_text(
        json.dumps({"ts": time.time(), "tool": data.get("tool_name"), "what": str(what)[:160]}))


def _last_assistant_text_ts(transcript: str):
    """Timestamp of the agent's most recent text to the developer (Claude transcript), or None."""
    try:
        p = Path(transcript)
        size = p.stat().st_size
        with p.open("rb") as fh:
            fh.seek(max(0, size - 400_000))
            tail = fh.read().decode("utf-8", "ignore").splitlines()
    except (OSError, TypeError):
        return None
    import datetime as dt
    for ln in reversed(tail):
        if '"assistant"' not in ln or '"text"' not in ln:
            continue
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content") or []
        if any(isinstance(c, dict) and c.get("type") == "text" and (c.get("text") or "").strip() for c in content):
            try:
                return dt.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError):
                return None
    return None


def on_tool_done(agent: str, data: dict):
    """PostToolUse: time the call; nudge for a progress update after a long silence."""
    sid = data.get("session_id") or "-"
    tid = re.sub(r"[^A-Za-z0-9_-]", "", data.get("tool_use_id") or "")[:80]
    pf = PENDING / f"{tid}.json"
    f, s = _sess(agent, sid)
    project = s.get("project") or ""
    if tid and pf.exists():
        p = core.read_json(pf, {}) or {}
        secs = time.time() - p.get("ts", time.time())
        try:
            pf.unlink()
        except OSError:
            pass
        if secs >= cfg()["slow_tool_seconds"]:
            record("tool", agent, sid, f"{p.get('tool')}: {p.get('what')}", secs, project)
    if not f.exists():
        return None
    now = time.time()
    said = _last_assistant_text_ts(data.get("transcript_path")) if agent == "claude" else None
    last = max(x for x in (s.get("turn_started") or s.get("started") or now, s.get("last_update") or 0,
                           s.get("last_nudge") or 0, said or 0))
    quiet = (now - last) / 60
    if quiet < cfg()["nudge_minutes"] or (s.get("nudges") or 0) >= 6:
        return None
    s["last_nudge"], s["nudges"] = now, (s.get("nudges") or 0) + 1
    f.write_text(json.dumps(s))
    run = load_run(agent, sid)
    where = f" ({headline(run)})" if run else ""
    return (f"devkit · {fmt_min(quiet)} since your last update to the developer{where}. Send a short progress update "
            f"now with the Progress template of the `communicating` skill (done · now · next · ETA, ≤ 5 lines), and "
            f"record it: `dev progress step \"<what finished>\" --next \"<what is next>\"`. If you are over your "
            f"estimate, say so and give a new one.")


def on_subagent(agent: str, data: dict, started: bool):
    sid = data.get("session_id") or "-"
    f, s = _sess(agent, sid)
    if not f.exists():
        return
    subs = s.setdefault("subagents", {})
    aid = data.get("agent_id") or data.get("subagent_id") or "?"
    if started:
        subs[aid] = {"type": data.get("agent_type") or data.get("subagent_type") or "", "started": time.time()}
    else:
        v = subs.pop(aid, None)
        if v:
            record("subagent", agent, sid, v.get("type") or aid, time.time() - v["started"], s.get("project") or "")
    f.write_text(json.dumps(s))


def on_turn_end(agent: str, sid: str, sess: dict):
    """Stop: record the turn's duration; notify the developer when a long turn finishes."""
    t0 = sess.get("turn_started")
    if not t0:
        return
    secs = time.time() - t0
    record("turn", agent, sid, sess.get("status", "")[:120], secs, sess.get("project") or "")
    if secs / 60 >= cfg()["notify_after_minutes"]:
        notify(f"{agent} finished · {fmt_min(secs / 60)}", (sess.get("status") or "turn finished")[:180])


# --------------------------------------------------------------------------- reports

def report(days: int = 7, project: str = None) -> str:
    out = [ui.section(f"Time · last {days} days" + (f" · {project}" if project else ""))]
    turns = records(days, "turn", project)
    if turns:
        m = sorted(t["secs"] / 60 for t in turns)
        out.append(ui.kv("turns", f"{len(m)} · median {fmt_min(statistics.median(m))} · p90 "
                                  f"{fmt_min(m[int(0.9 * (len(m) - 1))])} · longest {fmt_min(m[-1])}"))
    cal = calibration(project)
    if cal["ratio"]:
        word = "longer" if cal["ratio"] > 1 else "shorter"
        out.append(ui.kv("estimates", f"{cal['runs']} finished runs · they take {cal['ratio']}× the estimate "
                                      f"({word}); new estimates are corrected by this factor"))
    subs = records(days, "subagent", project)
    if subs:
        out.append(ui.kv("subagents", f"{len(subs)} · median {fmt_min(statistics.median(s['secs'] / 60 for s in subs))}"))
    tools = records(days, "tool", project)
    if tools:
        by = {}
        for t in tools:
            key = re.sub(r"\s+", " ", t["what"])[:70]
            by.setdefault(key, []).append(t["secs"])
        rows = sorted(((k, len(v), statistics.median(v), sum(v)) for k, v in by.items()), key=lambda x: -x[3])[:10]
        out.append(ui.section("Slowest commands (total time)"))
        out.append(ui.table([[k, n, f"{med:.0f}s", fmt_min(tot / 60)] for k, n, med, tot in rows],
                            ["COMMAND", "RUNS", "MEDIAN", "TOTAL"], max_widths={0: 70}))
    runs = active_runs()
    if runs:
        out.append(ui.section("Runs now"))
        for r in runs[:8]:
            out.append(f"  {r.get('agent')} · {r.get('project') or '-'} · {r.get('goal')} — {headline(r)}")
    if len(out) == 1:
        out.append("  no timing data yet (devkit records turns, slow commands, subagents and runs as agents work)")
    return "\n".join(out)
