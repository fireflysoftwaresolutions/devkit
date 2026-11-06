# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Progress, timing and predictability for agent sessions.

Runs     STATE/runs/<agent>-<session>.json — the piece of work a session is doing: goal, plan steps (each with an
         estimate, a state, an owner lane and its measured time), the activity intervals, subagent lanes, updates and
         state (running | blocked | done). Written by `dev progress` and the hooks, read by `dev progress`, presence
         (other machines) and the dashboard.

Time model (deterministic: every function takes `now`, and the tests use a fixed clock)
  active time  the union of the intervals in which someone works on the run: the main agent's turns (UserPromptSubmit
               to Stop) and each subagent (SubagentStart to SubagentStop). Waiting for the developer is not counted,
               and parallel agents are not counted twice. Wall-clock time is shown next to it.
  steps        a step's actual time is the active time between its start and its end; for a subagent's step, that
               subagent's own time.
  pace         actual ÷ estimate over the steps done in this run, clamped to 0.5–2.5; before any step is done, the
               project's history (`dev time`); otherwise 1.
  ETA          per lane (the main agent, each subagent): the estimates of its open steps × pace, minus the time
               already spent on a running step; the ETA is the longest lane, since lanes run in parallel. Shown as a
               range.
  subagents    the hooks see who calls (Claude Code and Codex pass `agent_id` to every hook fired inside a
               subagent): a subagent's own `dev progress` calls go to its lane and never advance the main plan.
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

from . import core, state, ui
from .core import STATE

RUNS = STATE / "runs"
TIMING = STATE / "timing"
PENDING = STATE / "timing" / "pending"
SESSIONS = STATE / "sessions"


def cfg() -> dict:
    return {"nudge_minutes": 8, "notify": True, "notify_after_minutes": 10, "slow_tool_seconds": 20,
            "max_parallel": 4, "stale_agent_hours": 6, **(core.CFG.get("progress") or {})}


# --------------------------------------------------------------------------- which session am I?

def current_session(cwd: str | None = None):
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


def session_path(agent: str, sid: str) -> Path:
    return SESSIONS / f"{agent}-{sid}.json"


def load_run(agent: str, sid: str):
    """The session's run, or None. Inside the record's lock (a writer that will save it), a run that does not parse
    raises state.StateBusy instead of reading as missing, so it is never written over."""
    p = run_path(agent, sid)
    return state.load(p) if state.held(p) else core.read_json(p, None)


def _tx(agent: str, sid: str):
    """The session's lock around `load_run → change → _save` (state.hold): the run is read and written inside one
    hold, so no other hook or command changes it in between. No git, subprocess or network call inside it."""
    return state.hold(run_path(agent, sid))


def _save(run: dict, touch_session: bool = True):
    """Write the run atomically through state. The caller loaded it inside the same _tx."""
    p = run_path(run["agent"], run["session"])
    with state.hold(p):
        state.replace(p, run, indent=1)
        if touch_session:  # the session's last update to the developer
            try:
                state.update(session_path(run["agent"], run["session"]),
                             lambda s: s.update(last_update=time.time()), create=False)
            except state.StateBusy:
                pass  # audited by state; the run itself is saved


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


def records(days: int = 30, kind: str | None = None, project: str | None = None) -> list:
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


def calibration(project: str | None = None) -> dict:
    """How long things really take here: estimate accuracy, turn length, slow commands."""
    runs = [r for r in records(60, "run", project) if r.get("estimate")]
    ratios = [r["secs"] / 60 / r["estimate"] for r in runs if r["estimate"] > 0]
    turns = [r["secs"] / 60 for r in records(30, "turn", project)]
    return {"runs": len(runs), "ratio": round(statistics.median(ratios), 2) if ratios else None,
            "turn_median": round(statistics.median(turns), 1) if turns else None,
            "turn_p90": round(sorted(turns)[int(0.9 * (len(turns) - 1))], 1) if turns else None}


PACE_MIN, PACE_MAX = 0.5, 2.5


def parse_plan(plan) -> list:
    """"design=30m; build=2h after design; docs" -> [(name, minutes or None, [steps it waits for])]."""
    items = plan if isinstance(plan, (list, tuple)) else str(plan or "").split(";")
    out = []
    for raw in items:
        raw = str(raw or "").strip()
        if not raw:
            continue
        after = []
        m = re.match(r"^(.*?)\s+after\s+(.+)$", raw, re.I)
        if m:
            raw, after = m.group(1).strip(), [x.strip() for x in re.split(r"[,&]|\band\b", m.group(2)) if x.strip()]
        m = re.match(r"^(.*?)\s*[=@]\s*(\d[\dhm.\s]*)$", raw)
        if m and parse_minutes(m.group(2)):
            out.append((m.group(1).strip(), parse_minutes(m.group(2)), after))
        else:
            out.append((raw, None, after))
    return out


def _new_step(name, est=None):
    return {"name": name, "est": est, "est_auto": est is None, "state": "pending", "owner": "main", "label": "",
            "started": None, "finished": None}


# --------------------------------------------------------------------------- the plan's dependencies (a DAG)
#
# A step's `after` lists, by name, the steps it waits for: the plan is a directed acyclic graph, and the functions
# below are the only place that decides about it. Every transition that starts, owns or closes a step asks them.
#   entering   start and add resolve each `after` to one step of the plan and refuse a name that is not a step, a
#              name that matches several, two steps with one name, and a cycle (spelled out step by step)
#   done       a dependency is satisfied when its step is done or skipped (DONE)
#   waiting    a step with an open dependency (deps_open) is never started: start and the main lane's advance pass it
#              over, begin refuses it, assign and a subagent's start leave it pending (the subagent's lane runs
#              without it, and Morpheus is told), and the launch queue (dispatch.ready) holds it back
#   closing    check_close refuses a step whose dependencies are open (--force when the order really changed)
#   reopening  the reopened (or un-skipped) step's dependents, direct and further down, that are done or running stay
#              so, flagged `recheck` (their work used the old version); a main-lane step that now waits for it goes
#              back to pending. The flag clears when that dependent is reopened itself, to be redone. A reopened
#              step a member did goes back to Morpheus, still assigned to that member, so it can be launched again.
# A name in `after` that is not a step of the plan cannot be entered; in a run edited by hand it is not counted.

DONE = ("done", "skipped")


def _by_name(plan) -> dict:
    return {s["name"]: s for s in plan or []}


def deps_open(run, s) -> list:
    """The steps `s` waits for that are not done or skipped yet, in the order of its `after`."""
    steps = _by_name((run or {}).get("plan"))
    return [a for a in s.get("after") or [] if a in steps and steps[a].get("state") not in DONE]


def ready(run) -> list:
    """The pending steps whose dependencies are all done or skipped, in plan order."""
    return [s for s in (run or {}).get("plan") or [] if s.get("state") == "pending" and not deps_open(run, s)]


def _cycle(plan):
    """The first dependency cycle, as names [a, b, …, a] (a waits for b, …, which waits for a), or None."""
    steps, color = _by_name(plan), {}

    def visit(name, path):
        color[name] = "open"
        for a in steps[name].get("after") or []:
            if a not in steps:
                continue
            if color.get(a) == "open":
                return path[path.index(a):] + [a]
            if not color.get(a):
                found = visit(a, path + [a])
                if found:
                    return found
        color[name] = "closed"
        return None

    for s in plan or []:
        if not color.get(s["name"]):
            found = visit(s["name"], [s["name"]])
            if found:
                return found
    return None


def _cycle_error(cyc) -> "StepError":
    links = ", ".join(f"“{a}” waits for “{b}”" for a, b in zip(cyc, cyc[1:]))
    return StepError(f"the plan's dependencies form a cycle: {links}. Remove one of these `after`s.")


def _duplicate_name(plan):
    seen = set()
    for s in plan or []:
        if s["name"].lower() in seen:
            return s["name"]
        seen.add(s["name"].lower())
    return None


def validate_plan(plan) -> None:
    """Refuse a plan the dependency engine cannot run: two steps with one name, a dependency that is not a step of the
    plan, or a cycle. Raises StepError naming the problem."""
    dup = _duplicate_name(plan)
    if dup:
        raise StepError(f"two steps are named “{dup}”: give each step its own name, so `after` can name it")
    steps = _by_name(plan)
    for s in plan or []:
        for a in s.get("after") or []:
            if a not in steps:
                raise StepError(f"“{s['name']}” waits for “{a}”, which is not a step of the plan")
    cyc = _cycle(plan)
    if cyc:
        raise _cycle_error(cyc)


def topo_order(plan) -> list:
    """The steps in an order that respects their dependencies and stays as close to the plan's order as it can: the
    next step is always the first one in plan order whose dependencies are placed. A valid plan order comes back
    unchanged. Raises StepError on a cycle."""
    steps, placed, left, out = _by_name(plan), set(), list(plan or []), []
    while left:
        i = next((i for i, s in enumerate(left)
                  if all(a in placed or a not in steps for a in s.get("after") or [])), None)
        if i is None:
            raise _cycle_error(_cycle(left) or [])
        s = left.pop(i)
        out.append(s)
        placed.add(s["name"])
    return out


def _resolve_after(plan, s, refs) -> list:
    """`after` as written (a step's name, its number, or a part of its name that only one step has) -> the names of
    the steps of `plan` it means. Raises StepError for one that is not a step of the plan or matches several."""
    out = []
    for ref in refs or []:
        r = str(ref).strip()
        hit = [x for x in plan if x["name"].lower() == r.lower()]
        if not hit and r.isdigit() and 1 <= int(r) <= len(plan):
            hit = [plan[int(r) - 1]]
        if not hit:
            hit = [x for x in plan if x is not s and r.lower() in x["name"].lower()]
        if not hit:
            raise StepError(f"“{s['name']}” waits for “{r}”, which is not a step of the plan: "
                            + "; ".join(f"{i}. {x['name']}" for i, x in enumerate(plan, 1)))
        if len(hit) > 1:
            raise StepError(f"“{s['name']}” waits for “{r}”, which matches several steps ("
                            + ", ".join(f"“{x['name']}”" for x in hit) + "): name the one you mean")
        if hit[0]["name"] not in out:
            out.append(hit[0]["name"])
    return out


def _build_steps(items, plan) -> list:
    """parse_plan items -> their new steps, with `after` resolved against `plan` plus the new steps. The plan they make
    together is validated (validate_plan) before anything changes: on error, `plan` is untouched."""
    new = [_new_step(n, e) for n, e, _ in items]
    full = list(plan or []) + new
    dup = _duplicate_name(full)
    if dup:
        raise StepError(f"two steps are named “{dup}”: give each step its own name, so `after` can name it")
    for s, (_, _, after) in zip(new, items):
        s["after"] = _resolve_after(full, s, after)
    validate_plan(full)
    return new


def normalize(run: dict) -> dict:
    """Bring a run to the current shape (runs written before 26.11.1 had a step counter and a list of names)."""
    if not run:
        return run
    if run.get("v") == 2:
        if "tracked_since" not in run and "messages" not in run and run.get("intervals"):
            run["tracked_since"] = min(iv[0] for iv in run["intervals"])  # converted before activity was recorded
        return run
    names = [x for x in (run.get("plan") or []) if isinstance(x, str)]
    n = max(run.get("steps") or 0, len(names))
    plan = [_new_step(x) for x in names] + [_new_step(f"step {i + 1}") for i in range(len(names), n)]
    for i, s in enumerate(plan):
        if i < (run.get("step") or 0):
            s.update(state="done", untimed=True)
    # activity was not recorded before the upgrade: until now the run's time is wall-clock time
    run.update(v=2, plan=plan, intervals=run.get("intervals") or [], agents=run.get("agents") or {},
               tracked_since=time.time())
    _allocate(run)
    return run


def _default_step_minutes(project) -> float:
    steps = [r["secs"] / 60 for r in records(60, "step", project) if r.get("secs")]
    return round(statistics.median(steps), 1) if len(steps) >= 3 else 10.0


def _allocate(run):
    """Steps without an estimate share what the total estimate leaves; with no total, the median step here."""
    plan = run.get("plan") or []
    total = run.get("estimate") or 0
    known = sum(s["est"] for s in plan if s.get("est") and not s.get("est_auto"))
    auto = [s for s in plan if s.get("est_auto") or not s.get("est")]
    if auto:
        share = (total - known) / len(auto) if total and total > known else _default_step_minutes(run.get("project"))
        for s in auto:
            s["est"], s["est_auto"] = round(max(1.0, share), 1), True
    if not total and plan:
        run["estimate"] = round(sum(s["est"] for s in plan), 1)


def _open(run, kind, ident, now):
    if not any(iv[1] is None and iv[2] == kind and iv[3] == ident for iv in run["intervals"]):
        run["intervals"].append([round(now, 1), None, kind, ident])


def _close(run, kind, ident, now):
    for iv in run["intervals"]:
        if iv[1] is None and iv[2] == kind and (ident is None or iv[3] == ident):
            iv[1] = round(max(now, iv[0]), 1)


def _union_seconds(ivs, t0, t1, now) -> float:
    segs = sorted((max(a, t0), min(now if b is None else b, t1)) for a, b, *_ in ivs)
    total, cur = 0.0, None
    for s, e in segs:
        if e <= s:
            continue
        if cur is None or s > cur[1]:
            total += (cur[1] - cur[0]) if cur else 0
            cur = [s, e]
        else:
            cur[1] = max(cur[1], e)
    return total + ((cur[1] - cur[0]) if cur else 0)


def active_minutes(run, t0=None, t1=None, now=None, lane=None) -> float:
    """Active minutes in [t0, t1]: all lanes, or one lane ("main" = the main agent's turns, else a subagent id).
    A run with no recorded activity (no hooks) falls back to wall-clock time."""
    now = now or time.time()
    t0, t1 = t0 or run.get("started") or now, min(t1 or now, now)
    ivs = run.get("intervals") or []
    if lane == "main":
        ivs = [iv for iv in ivs if iv[2] == "turn"]
    elif lane:
        ivs = [iv for iv in ivs if iv[3] == lane]
    if not run.get("intervals"):
        return max(0.0, (t1 - t0) / 60)
    since = run.get("tracked_since") or 0  # an upgraded run: wall-clock time until activity was recorded
    if since and t0 < since and not lane:
        return max(0.0, (min(t1, since) - t0) / 60) + (_union_seconds(ivs, since, t1, now) / 60 if t1 > since else 0)
    return _union_seconds(ivs, t0, t1, now) / 60


def wall_minutes(run, now=None) -> float:
    now = now or time.time()
    return max(0.0, ((run.get("finished") or now) - run.get("started", now)) / 60)


def step_minutes(run, s, now=None) -> float:
    if not s.get("started"):
        return 0.0
    lane = None if (s.get("owner") or "main") == "main" else s["owner"]
    return active_minutes(run, s["started"], s.get("finished") or now, now, lane)


def pace(run, now=None):
    """-> (pace, basis). Actual ÷ estimate over this run's finished steps, shrunk toward the prior (the project's
    history, else 1) in proportion to the evidence: (actual + W·prior) ÷ (estimate + W), W = a quarter of the total
    estimate (at least 30 minutes). A step closed in under 10% of its estimate is bookkeeping (the work happened
    elsewhere) and does not count. Clamped to 0.5–2.5."""
    prior = min(PACE_MAX, max(PACE_MIN, run.get("cal_ratio") or 1.0))
    est = act = 0.0
    for s in run.get("plan") or []:
        if s.get("state") != "done" or not s.get("est") or not s.get("started") or not s.get("finished") \
                or s.get("untimed"):
            continue
        a = step_minutes(run, s, now)
        if a < 0.1 * s["est"]:
            continue
        est, act = est + s["est"], act + a
    if est < 1:
        return prior, "history" if run.get("cal_ratio") else "estimate"
    w = max(30.0, 0.25 * sum((s.get("est") or 0) for s in run.get("plan") or []))
    return round(min(PACE_MAX, max(PACE_MIN, (act + w * prior) / (est + w))), 3), "this run"


def lanes(run, now=None) -> dict:
    """Minutes left per lane (the main agent and each subagent with open steps)."""
    now = now or time.time()
    p, _ = pace(run, now)
    out = {}
    if not run.get("plan"):  # no plan: the total estimate minus the active time so far
        est = run.get("estimate") or 0
        return {"main": max(0.0, est * p - active_minutes(run, now=now))} if est else {}
    for s in run.get("plan") or []:
        if s.get("state") in ("done", "skipped"):
            continue
        need = (s.get("est") or 0) * p
        if s.get("state") == "running":
            need = max(need - step_minutes(run, s, now), 0.15 * need)
        lane = s.get("owner") or "main"
        out[lane] = out.get(lane, 0.0) + need
    return out


def schedule(run, now=None) -> list:
    """Minutes from now at which each plan step finishes: a step starts when the steps it waits for (`after`) are done
    and its lane is free (one lane works on one step at a time; subagent lanes run in parallel with the main one).
    -> [(step, finish)] in plan order; done steps finish at 0."""
    now = now or time.time()
    p, _ = pace(run, now)
    plan = run.get("plan") or []
    by_name = _by_name(plan)
    try:
        order = topo_order(plan)  # a step is scheduled after the steps it waits for, wherever the plan lists it
    except StepError:
        order = plan  # a run edited by hand into a cycle: plan order, with the pessimistic guess below
    finish, lane_free, ends = {}, {}, {}
    for s in order:
        if s.get("state") in DONE:
            finish[s["name"]] = ends[id(s)] = 0.0
            continue
        dur = (s.get("est") or 0) * p
        if s.get("state") == "running":
            dur = max(dur - step_minutes(run, s, now), 0.15 * dur)
        lane = s.get("owner") or "main"
        if lane == "main" and s.get("label"):
            lane = "label:" + s["label"]  # waits for its own subagent, in parallel
        ready = max((finish.get(a, sum((x.get("est") or 0) * p for x in plan)) for a in s.get("after") or []
                     if a in by_name), default=0.0)
        start = 0.0 if s.get("state") == "running" else max(ready, lane_free.get(lane, 0.0))
        end = start + dur
        lane_free[lane] = max(lane_free.get(lane, 0.0), end)
        finish[s["name"]] = ends[id(s)] = end
    return [(s, ends[id(s)]) for s in plan]


def eta(run: dict, now: float | None = None) -> float:
    """Minutes left: the critical path through the plan (dependencies and lanes)."""
    if (run or {}).get("v") != 2:
        run = normalize(dict(run or {}))
    if not run.get("plan"):
        return round(max(lanes(run, now).values(), default=0.0), 1)
    return round(max((f for _, f in schedule(run, now)), default=0.0), 1)


def eta_range(run, now=None):
    """(low, high) minutes: ±25% once measured steps set the pace, wider before."""
    e = eta(run, now)
    spread = 0.25 if pace(run, now)[1] == "this run" else 0.4  # narrower only once real steps measured the pace
    return round(e * (1 - spread * 0.8), 1), round(e * (1 + spread), 1)


def fmt_range(lo, hi) -> str:
    a, b = fmt_min(lo), fmt_min(hi)
    return f"~{a}" if a == b else f"~{a}–{b}"


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


def headline(run: dict, now: float | None = None) -> str:
    """'2/6 · 42m active (1h05m wall) of ~3h00m · ETA ~24m–37m · 2 agents working'"""
    now = now or time.time()
    run = normalize(run)
    plan = run.get("plan") or []
    done = sum(1 for s in plan if s.get("state") in ("done", "skipped"))
    act, wall = active_minutes(run, now=run.get("finished") or now), wall_minutes(run, now)
    parts = [f"{done}/{len(plan)}" if plan else "no plan",
             f"{fmt_min(act)} active" + (f" ({fmt_min(wall)} wall)" if wall - act >= 5 else "")]
    if run.get("estimate"):
        parts[-1] += f" of ~{fmt_min(run.get('estimate_cal') or run['estimate'])}"
    if run.get("state") == "running":
        lo, hi = eta_range(run, now)
        parts.append(f"ETA {fmt_range(lo, hi)}")
    else:
        parts.append(run.get("state", ""))
    working = sum(1 for a in (run.get("agents") or {}).values() if a.get("state") == "running")
    if working:
        parts.append(f"{working} agent{'s' if working > 1 else ''} working")
    return " · ".join(parts)


def start(goal: str, steps: int = 0, estimate: str = "", cwd: str | None = None, plan=None, from_agent: str | None = None,
          now: float | None = None, mode: str = "") -> dict:
    now = now or time.time()
    agent, sid = current_session(cwd)
    if from_agent:  # a subagent planning its own work: a lane of the main run, never a new main run
        with _tx(agent, sid):
            run = normalize(load_run(agent, sid) or {})
            if run.get("goal"):
                lane = _lane(run, from_agent, now)
                lane.update(goal=goal, plan=[x[0] for x in parse_plan(plan)], done=0)
                run["updates"].append({"ts": round(now), "kind": "agent", "agent": from_agent,
                                       "text": f"plan: {goal}"})
                _save(run)
                return run
    sf = SESSIONS / f"{agent}-{sid}.json"
    sess = core.read_json(sf, {}) or {}
    project = sess.get("project") or _project(cwd)
    est = parse_minutes(estimate)
    cal = calibration(project)
    items = parse_plan(plan)
    if not items and steps:
        items = [(f"step {i + 1}", None, []) for i in range(steps)]
    new = _build_steps(items, [])  # an unknown dependency or a cycle is refused before anything is saved
    run = {"v": 2, "agent": agent, "session": sid, "project": project, "goal": goal, "estimate": est,
           "estimate_cal": round(est * cal["ratio"], 1) if est and cal["ratio"] else est, "cal_ratio": cal["ratio"],
           "started": now, "state": "running", "updates": [], "intervals": [], "agents": {},
           "plan": new, "messages": [], "decisions": [],
           "mode": mode if mode in ("guided", "auto") else ((core.CFG.get("work") or {}).get("mode") or "guided")}
    _allocate(run)
    run["steps"], run["step"] = len(run["plan"]), 0
    _advance_main(run, now)  # the first step that waits for nothing
    if sess.get("turn_started") and (sess.get("turn_started") or 0) >= (sess.get("turn_ended") or 0):
        _open(run, "turn", "main", now)  # started inside a turn: that turn is active time
    _save(run)
    _publish()
    return run


def _lane(run, agent_id, now, label="", kind=""):
    lane = run["agents"].setdefault(agent_id, {"label": label, "type": kind, "started": now, "finished": None,
                                               "state": "running", "updates": [], "done": 0})
    if label and not lane.get("label"):
        lane["label"] = label
    return lane


def _find_step(run, which):
    """A plan step by 1-based number or by (part of) its name."""
    plan = run.get("plan") or []
    if which is None or which == "":
        return None
    w = str(which).strip()
    if w.isdigit() and 1 <= int(w) <= len(plan):
        return plan[int(w) - 1]
    lw = w.lower()
    return next((s for s in plan if s["name"].lower() == lw), None) or \
        next((s for s in plan if lw in s["name"].lower()), None)


def _finish_step(run, s, now, agent, sid):
    if s.get("state") == "done":
        return
    s.update(state="done", finished=now)
    s["started"] = s.get("started") or now
    record("step", agent, sid, s["name"], step_minutes(run, s, now) * 60, run.get("project", ""),
           estimate=s.get("est") or 0, est_auto=bool(s.get("est_auto")), owner="main" if s.get("owner") == "main"
           else "agent")


def _advance_main(run, now):
    """Keep one main step running: the first ready one (its dependencies done) not handed to a subagent."""
    plan = run.get("plan") or []
    if any(s.get("state") == "running" and (s.get("owner") or "main") == "main" for s in plan):
        return
    nxt = next((s for s in ready(run) if (s.get("owner") or "main") == "main" and not s.get("label")), None)
    if nxt:
        nxt.update(state="running", started=now)


def _main_only(from_agent, what: str):
    """Only Morpheus (the main session) changes the main plan; a crew member asks him."""
    if from_agent:
        raise StepError(f"only Morpheus {what}: ask him with dev crew ask \"…\" (your own steps: dev progress add, "
                        "dev progress step)")


def _step_text(s: dict, label: str = "") -> str:
    """What a step is about, for the RACI: its name, its description, and the task in its label ("Tank: the API")."""
    task = label.split(":", 1)[1] if ":" in (label or "") else ""
    return " ".join(x for x in (s.get("name") or "", s.get("desc") or "", task) if x)


def _raci_assign(run: dict, s: dict, label: str, why: str, now: float):
    """The crew's RACI decides who does a step: refuse a member who is not Responsible for it (unless `why`, which
    is recorded), and remember the step's kind of work for the approval at the end of the run."""
    try:
        from . import crew, raci
        mem = crew._for_label(label)
        text = _step_text(s, label)
        top = raci.best(text)
        if top:
            s["activity"] = top[0]["id"]
        if not mem:
            return
        reason = raci.check_assignment(mem["id"], text)
    except StepError:
        raise
    except Exception:
        return  # a broken roster must not stop the work; `dev crew sync --check` reports it
    if reason and not why:
        raise StepError(reason)
    if reason:
        run.setdefault("raci_overrides", []).append({"ts": round(now), "step": s["name"], "to": mem["name"],
                                                     "why": why})
        from . import audit
        audit.record("raci.override", s["name"], f"{mem['name']}: {why}"[:300], "override")


def assign(which, label: str, cwd: str | None = None, now: float | None = None, scope: str = "",
           from_agent: str | None = None, why: str = "") -> dict:
    """Hand a plan step to a subagent: the next subagent launched with this description (or a running lane with this
    label) owns it, and its time is that subagent's time. `scope`: the files it owns (comma-separated globs); the
    guard keeps that subagent's edits inside them. The crew's RACI must make the member Responsible for the step's
    kind of work; `why` overrides it, recorded."""
    _main_only(from_agent, "assigns the plan's steps")
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise ValueError(f"no step “{which}” in the plan")
        _raci_assign(run, s, label, why, now)
        owner = s.get("owner") or "main"
        if owner != "main" and s.get("state") != "done" and not _label_matches(label, (run.get("agents") or {}).get(owner)
                                                                                or {}):
            old = ((run.get("agents") or {}).get(owner) or {}).get("persona") or owner
            _log_change(run, s, s.get("state"), "pending", "main", f"reassigned from {old} to {label}", now)
            s.update(owner="main", state="pending")  # the other member's lane no longer owns it
        s["label"] = label
        if scope:
            globs = [x.strip() for x in scope.split(",") if x.strip()]
            for k, lane in (run.get("agents") or {}).items():
                if lane.get("state") == "running" and not _label_matches(label, lane):
                    clash = [g for g in globs for h in lane.get("scope") or [] if _globs_overlap(g, h)]
                    if clash:
                        raise StepError(f"{', '.join(clash)} overlaps the files {lane.get('persona') or lane.get('label')} "
                                        f"owns while working ({', '.join(lane.get('scope'))}). Give each member "
                                        f"different files, or wait until they finish.")
            s["scope"] = globs
        lane_id = None if deps_open(run, s) else next((k for k, v in (run.get("agents") or {}).items()
                                                       if v.get("state") == "running" and _label_matches(label, v)), None)
        if lane_id:
            s.update(owner=lane_id, state="running", started=s.get("started") or now)
            if s.get("scope"):
                run["agents"][lane_id]["scope"] = s["scope"]
        elif s.get("state") == "running" and (s.get("owner") or "main") == "main":
            s["state"] = "pending"  # waits for its subagent
        _advance_main(run, now)
        _save(run)
    return run


def _globs_overlap(a: str, b: str) -> bool:
    """Could two path globs match the same file? (conservative: compares the fixed prefixes)"""
    import fnmatch
    pa, pb = re.split(r"[*?\[]", a, maxsplit=1)[0], re.split(r"[*?\[]", b, maxsplit=1)[0]
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    return pa.startswith(pb) or pb.startswith(pa)


def scope_owner(run, rel_path: str, me: str = "main"):
    """The running crew member (other than `me`) whose scope covers this path -> (lane id, lane) or (None, None)."""
    import fnmatch
    for k, lane in (run.get("agents") or {}).items():
        if k == me or lane.get("state") != "running":
            continue
        for g in lane.get("scope") or []:
            if fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(rel_path, g.rstrip("/") + "/**") or \
                    rel_path.startswith(g.rstrip("*").rstrip("/") + "/") or rel_path == g:
                return k, lane
    return None, None


def running_agents(run) -> list:
    return [(k, v) for k, v in (run.get("agents") or {}).items() if v.get("state") == "running"]


def expire_stale(run, now=None) -> bool:
    """Close lanes and intervals left open by a subagent that never reported back (crashed, or the session was
    resumed without it), so they neither inflate the time nor block new launches. -> changed"""
    now = now or time.time()
    limit = cfg()["stale_agent_hours"] * 3600
    changed = False
    for k, lane in (run.get("agents") or {}).items():
        since = max(lane.get("started") or now, lane.get("resumed_at") or 0)  # a resumed lane: since it resumed
        if lane.get("state") == "running" and now - since > limit:
            last = max([lane.get("started") or now] + [u.get("ts", 0) for u in lane.get("updates") or []])
            lane.update(state="lost", finished=last)
            _audit("crew.lost", lane.get("persona") or lane.get("label") or k, "no report for "
                   f"{cfg()['stale_agent_hours']}h", "lost", project=run.get("project") or "")
            _close(run, "agent", k, last)
            for s in run.get("plan") or []:
                if s.get("owner") == k and s.get("state") == "running":
                    _log_change(run, s, "running", "pending", "devkit", f"{lane.get('persona') or 'subagent'} "
                                f"stopped reporting; the step waits for a new owner", now)
                    s.update(state="pending", owner="main")
            changed = True
    return changed


def _label_matches(label: str, lane: dict) -> bool:
    """A step assigned to "Neo" belongs to the lane launched as "Neo: build the parser", or as devkit:neo."""
    lab = (label or "").strip().lower()
    if not lab:
        return False
    names = [(lane.get("label") or "").lower(), (lane.get("persona") or "").lower(), (lane.get("type") or "").lower()]
    return any(n and (n == lab or n.startswith(lab + ":") or n.startswith(lab + " ") or n.endswith(":" + lab)
                      or lab in n.split(":")[0]) for n in names)


def persona(agent_type: str = "", label: str = "") -> str:
    """The crew member behind a subagent: from its type (devkit:neo, neo) or its description ("Neo: …")."""
    cands = [(agent_type or "").split(":")[-1].strip(), (label or "").split(":")[0].strip()]
    try:
        from . import crew
        for cand in cands:
            m = crew.member(cand) if cand else None
            if m and not m.get("main"):
                return m["name"]
    except Exception:
        pass
    if ":" in (agent_type or "") and cands[0]:  # devkit:neo without the roster at hand
        return cands[0].replace("-", " ").title()
    if ":" in (label or "") and cands[1] and len(cands[1].split()) <= 3:  # "Neo: build the parser"
        return cands[1]
    return ""


def lane_of(agent: str, sid: str, agent_id: str):
    """(run, lane) for a subagent of this session, or (run, None)."""
    run = load_run(agent, sid)
    if not run:
        return None, None
    run = normalize(run)
    return run, (run.get("agents") or {}).get(agent_id)


def _addr(run, ref: str):
    """'Morpheus'/'lead'/'main'/'' -> 'main'; a crew name or label -> that lane's id (or the name, for later)."""
    r = (ref or "").strip()
    if not r or r.lower() in ("main", "lead", "morpheus", "tech lead"):
        return "main"
    agents = run.get("agents") or {}
    if r in agents:
        return r
    hits = [(k, v) for k, v in agents.items() if _label_matches(r, v)]
    if hits:  # the member's working lane, bound to its launch, before an older or finished one
        return max(hits, key=lambda kv: (kv[1].get("state") == "running", kv[1].get("binding", "exact") == "exact",
                                         kv[1].get("started") or 0))[0]
    return "name:" + r


def _name_of(run, addr: str) -> str:
    if addr == "main":
        return "Morpheus"
    if addr == "ci":
        return "CI watch"
    if addr.startswith("name:"):
        return addr[5:]
    lane = (run.get("agents") or {}).get(addr) or {}
    return lane.get("persona") or lane.get("label") or "subagent"


def crew_ask(text: str, to: str | None = None, from_agent: str | None = None, cwd: str | None = None, now: float | None = None) -> dict:
    """A question or request between crew members (default: to Morpheus, the main session). It reaches the recipient
    with its next tool call (PostToolUse) or prompt."""
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise ValueError("no progress run in this session: Morpheus starts one with dev progress start")
        run = normalize(run)
        m = _post(run, from_agent or "main", _addr(run, to or ""), text, now)
        _save(run, touch_session=False)
    _audit("crew.message", f"{m['from_name']} → {m['to_name']}", m["text"][:300], project=run.get("project") or "",
           id=m["id"])
    return m


def _post(run, frm: str, to: str, text: str, now: float, from_name: str = "") -> dict:
    """Add a message to the run (the caller saves it). -> the message."""
    msgs = run.setdefault("messages", [])
    m = {"id": f"M{len(msgs) + 1}", "ts": round(now), "from": frm, "to": to, "text": text.strip()[:1500],
         "delivered": False}
    m["from_name"], m["to_name"] = from_name or _name_of(run, frm), _name_of(run, to)
    msgs.append(m)
    run["messages"] = msgs[-200:]
    return m


def crew_answer(msg_id: str, text: str, from_agent: str | None = None, cwd: str | None = None, now: float | None = None) -> dict:
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        q = next((m for m in run.get("messages") or [] if m["id"].lower() == str(msg_id).lower()), None)
        if not q:
            raise ValueError(f"no message {msg_id}")
        q["answered"] = True
        _save(run, touch_session=False)
    a = crew_ask(text, to=q["from"] if q["from"] != "main" else "main", from_agent=from_agent, cwd=cwd, now=now)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        for m in run.get("messages") or []:
            if m["id"] == a["id"]:
                m["reply_to"], m["to"], m["to_name"] = q["id"], q["from"], q["from_name"]
        _save(run, touch_session=False)
    return a


def crew_inbox(from_agent: str | None = None, cwd: str | None = None, mark: bool = True) -> list:
    """Messages for the caller (the main session, or the subagent whose lane this is) not delivered yet."""
    agent, sid = current_session(cwd)
    run = load_run(agent, sid)
    return _take_messages(agent, sid, from_agent or "main", mark) if run else []


def _take_messages(agent, sid, me: str, mark=True) -> list:
    """The messages for `me` not delivered yet; with mark, they are marked delivered (once each, under the lock).
    Every hook call asks: the lock is taken only when there is something to take."""
    def mine(run):
        run = normalize(run)
        lane = (run.get("agents") or {}).get(me) or {}
        return run, [m for m in run.get("messages") or [] if not m.get("delivered") and (
            m.get("to") == me or ((m.get("to") or "").startswith("name:") and me != "main"
                                  and _label_matches(m["to"][5:], lane)))]
    run = load_run(agent, sid)
    if not run or not mine(run)[1]:
        return []
    if not mark:
        return [dict(m) for m in mine(run)[1]]
    with _tx(agent, sid):
        run, new = mine(load_run(agent, sid) or {})
        out = [dict(m) for m in new]
        for m in new:
            m["delivered"] = True
        if out:
            _save(run, touch_session=False)
    return out


IRREVERSIBLE = ("push", "pr", "merge", "release", "deploy", "delete-data", "migrate-production", "spend", "publish",
                "external-message")


def mode_of(run) -> str:
    return (run or {}).get("mode") or ((core.CFG.get("work") or {}).get("mode")) or "guided"


def set_mode(mode: str, approve: str = "", cwd: str | None = None, now: float | None = None) -> dict:
    """guided: decisions that are the developer's wait for the developer; auto: the crew goes on with the recommended
    option and records it. `approve`: irreversible actions pre-approved for auto mode (comma-separated)."""
    now = now or time.time()
    if mode not in ("guided", "auto"):
        raise StepError("mode is guided or auto")
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        if not run.get("goal"):
            raise StepError("no progress run in this session: dev progress start \"<goal>\" --plan \"…\" --mode " + mode)
        approved = [x.strip().lower() for x in (approve or "").split(",") if x.strip()]
        bad = [x for x in approved if x not in IRREVERSIBLE]
        if bad:
            raise StepError(f"unknown action(s) {', '.join(bad)}; known: {', '.join(IRREVERSIBLE)}")
        run["mode"], run["approved"] = mode, approved if mode == "auto" else []
        _audit("crew.mode", mode, "approved: " + (", ".join(approved) or "nothing"), project=run.get("project") or "")
        run.setdefault("history", []).append({"ts": round(now), "step": "(mode)", "from": None, "to": mode,
                                              "by": "main", "why": "approved: " + (", ".join(approved) or "nothing")})
        _save(run)
    return run


def crew_decision(question: str, options=None, recommend: str = "", irreversible: str = "", from_agent: str | None = None,
                  cwd: str | None = None, now: float | None = None) -> dict:
    """A decision point. -> the decision record. In auto mode a reversible decision is taken at once with the
    recommendation; in guided mode, or for an irreversible action not pre-approved, it waits for the developer: the
    asking member's lane waits and its step is blocked until Morpheus records the answer (crew_decide)."""
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise StepError("no progress run in this session: Morpheus starts one with dev progress start")
        run = normalize(run)
        opts = [o.strip() for o in (options if isinstance(options, list) else str(options or "").split("|")) if o.strip()]
        rec = (recommend or "").strip()
        if rec and opts and not any(o.lower().startswith(rec.lower()) for o in opts):
            raise StepError(f"the recommendation “{rec}” is not one of the options: {' | '.join(opts)}")
        act = (irreversible or "").strip().lower()
        if act and act not in IRREVERSIBLE:
            raise StepError(f"unknown irreversible action “{act}”; known: {', '.join(IRREVERSIBLE)}")
        decs = run.setdefault("decisions", [])
        d = {"id": f"D{len(decs) + 1}", "ts": round(now), "question": question.strip()[:800], "options": opts,
             "recommend": rec, "irreversible": act, "from": from_agent or "main",
             "from_name": _name_of(run, from_agent or "main"), "state": "pending", "answer": "", "by": ""}
        auto = mode_of(run) == "auto" and (not act or act in (run.get("approved") or []))
        if auto and rec:
            d.update(state="decided", answer=rec, by="auto", decided=round(now))
        else:
            if from_agent:
                lane = (run.get("agents") or {}).get(from_agent)
                if lane:
                    lane["state_before_wait"] = lane.get("state")
                    lane["waiting"] = d["id"]
                for s in run.get("plan") or []:
                    if s.get("owner") == from_agent and s.get("state") == "running":
                        _log_change(run, s, "running", "blocked", from_agent, f"waiting for the developer: {d['id']}", now)
                        s["state"], s["waiting"] = "blocked", d["id"]
            if not auto and not rec and mode_of(run) == "auto":
                d["note"] = "auto mode, but no recommendation was given: the developer decides"
        decs.append(d)
        run["decisions"] = decs[-200:]
        _save(run, touch_session=False)
    _audit("crew.decision", d["id"], d["question"][:300], d["state"], project=run.get("project") or "",
           by=d.get("by") or "", answer=d.get("answer") or "", irreversible=d.get("irreversible") or "",
           mode=mode_of(run), asked_by=d["from_name"])
    return d


def crew_decide(decision_id: str, answer: str, cwd: str | None = None, now: float | None = None, by: str = "developer") -> dict:
    """Record the developer's answer (Morpheus runs it after asking): the waiting member's step runs again, and a
    message with the answer waits for that member (delivered when Morpheus resumes it)."""
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        d = next((x for x in run.get("decisions") or [] if x["id"].lower() == str(decision_id).lower()), None)
        if not d:
            raise StepError(f"no decision {decision_id}")
        if d["state"] == "decided" and d.get("by") != "auto":
            raise StepError(f"{d['id']} was already decided by {d['by']}: “{d['answer']}”")
        prev = d.get("answer")
        d.update(state="decided", answer=answer.strip()[:800], by=by, decided=round(now))
        if prev and prev != d["answer"]:
            d["overrides"] = prev  # the developer changed an automatic decision
        for s in run.get("plan") or []:
            if s.get("waiting") == d["id"]:
                _log_change(run, s, s.get("state"), "running", "main", f"{d['id']} decided: {d['answer'][:80]}", now)
                s["state"] = "running"
                s.pop("waiting", None)
        for lane in (run.get("agents") or {}).values():
            if lane.get("waiting") == d["id"]:
                lane.pop("waiting", None)
                lane["state"] = lane.pop("state_before_wait", None) or "running"
        _save(run, touch_session=False)
    _audit("crew.decided", d["id"], d["answer"][:300], project=run.get("project") or "", by=by,
           overrides=d.get("overrides") or "")
    if d["from"] != "main":
        crew_ask(f"Decision {d['id']} — {d['question'][:120]} → {d['answer']}", to=d["from"], cwd=cwd, now=now)
    return d


def pending_decisions(run) -> list:
    return [d for d in (run or {}).get("decisions") or [] if d.get("state") == "pending"]


def _ci(agent, sid) -> str:
    try:
        from . import ciwatch
        return ciwatch.pending_events(agent, sid)
    except Exception:
        return ""


def _ready(agent, sid) -> str:
    try:
        from . import dispatch
        return dispatch.ready_context(agent, sid)
    except Exception:
        return ""


def decisions_context(agent: str, sid: str) -> str:
    """For the main session: decisions waiting for the developer, newest first, once per decision."""
    run = load_run(agent, sid)
    if not run or not any(not d.get("surfaced") for d in pending_decisions(run)):
        return ""  # the usual case, without the lock
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        new = [d for d in pending_decisions(run) if not d.get("surfaced")]
        if not new:
            return ""
        for d in new:
            d["surfaced"] = True
        _save(run, touch_session=False)
    lines = ["devkit · decisions waiting for the developer (" + mode_of(run) + " mode). Ask with your question tool "
             "(AskUserQuestion / request_user_input) using the decision template, then record the answer with "
             "`dev crew decide <id> \"<answer>\"` and resume the member who asked (SendMessage / send_input):"]
    for d in new:
        opts = (" Options: " + " | ".join(d["options"])) if d["options"] else ""
        lines.append(f"- {d['id']} from {d['from_name']}: {d['question']}{opts}"
                     + (f" Recommended: {d['recommend']}." if d.get("recommend") else "")
                     + (f" Irreversible: {d['irreversible']}." if d.get("irreversible") else ""))
    return "\n".join(lines)


def messages_context(agent: str, sid: str, me: str) -> str:
    """The hook text that delivers a crew member's pending messages."""
    msgs = _take_messages(agent, sid, me)
    if not msgs:
        return ""
    lines = ["devkit · crew messages for you (answer with `dev crew answer <id> \"…\"`):"]
    for m in msgs:
        lines.append(f"- {m['id']} from {m.get('from_name')}: {m['text']}"
                     + (f" (answer to {m['reply_to']})" if m.get("reply_to") else ""))
    return "\n".join(lines)


def begin(which, cwd: str | None = None, now: float | None = None, from_agent: str | None = None) -> dict:
    """Mark a step as started now (parallel work the main agent does itself)."""
    _main_only(from_agent, "starts the plan's steps")
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise ValueError(f"no step “{which}” in the plan")
        if s.get("state") != "done":
            waiting = deps_open(run, s)
            if waiting:
                raise StepError(f"step “{s['name']}” waits for {', '.join(waiting)}, not done yet: it starts when "
                                f"{'that is' if len(waiting) == 1 else 'they are'} done. If the order really changed, "
                                f"skip what it waits for: dev progress skip \"{waiting[0]}\" --why \"…\"")
            s.update(state="running", started=s.get("started") or now)
        _save(run)
    return run


class StepError(ValueError):
    """A step change devkit refuses, with what to do instead."""


STOP_WORDS = set("a an the and or of to in on for with by from at as is are was were be been it its this that these "
                 "those into than then done finished complete completed step steps work working all now x y de la el "
                 "los las y o en con por para del un una".split())


def _words(text: str) -> set:
    out = set()
    for w in re.findall(r"[a-z0-9áéíóúñü]{2,}", (text or "").lower()):
        if w in STOP_WORDS:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)  # tests -> test
    return out


def _same_stem(a: str, b: str) -> bool:
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 3 and long_.startswith(short):  # tag / tagged, doc / documentation
        return True
    n = 0
    while n < len(short) and short[n] == long_[n]:
        n += 1
    return n >= 5 or (n >= 4 and len(short) <= 6)  # integrate / integration, build / building


def _matches(text: str, step: dict) -> bool:
    """Does the text describe this step? (shares a word stem with its name; unnamed steps always match)"""
    name = step.get("name", "")
    if re.fullmatch(r"step \d+", name):
        return True
    tw, sw = _words(text), _words(name)
    return any(_same_stem(a, b) for a in tw for b in sw)


def _audit(action: str, target: str, detail: str = "", outcome: str = "ok", **extra):
    """Every coordination event goes to the audit trail too (dev audit; published to the handoff repository)."""
    try:
        from . import audit
        audit.record(action, target, detail, outcome, **extra)
    except Exception:
        pass


def _log_change(run, s, old, new, who, why, now):
    run.setdefault("history", []).append({"ts": round(now), "step": s["name"], "from": old, "to": new,
                                          "by": who or "main", "why": (why or "")[:300]})
    run["history"] = run["history"][-200:]
    _audit(f"plan.{new}", s["name"], why or "", project=run.get("project") or "", run=run.get("goal", "")[:80],
           by=_name_of(run, who or "main") if who != "devkit" else "devkit", was=old or "")


def check_close(run, target, text, explicit, force=False):
    """The safety checks before a step is closed. Raises StepError with what to do instead."""
    plan = run.get("plan") or []
    if target is None:
        raise StepError("no open step to close. Add the work as a step first: dev progress add \"<step>=<estimate>\"")
    if target.get("state") in ("done", "skipped"):
        raise StepError(f"step “{target['name']}” is already {target['state']}; to change it: "
                        f"dev progress reopen \"{target['name']}\" --why \"…\"")
    if force:
        return
    owner = target.get("owner") or "main"
    if owner != "main" and target.get("state") == "running":
        who = (run.get("agents") or {}).get(owner, {})
        raise StepError(f"step “{target['name']}” belongs to {who.get('persona') or who.get('label') or 'a subagent'}, "
                        f"still working on it; it closes when that agent finishes. Use --force only if it is "
                        f"really done.")
    waiting = deps_open(run, target)
    if waiting:
        raise StepError(f"step “{target['name']}” waits for {', '.join(waiting)}, not done yet. Close those first, "
                        f"or use --force if the order changed.")
    if not _matches(text, target):
        better = [x for x in plan if x is not target and x.get("state") not in ("done", "skipped") and _matches(text, x)]
        hint = (f" It sounds like step {plan.index(better[0]) + 1}, “{better[0]['name']}”: --step \"{better[0]['name']}\"."
                if better else " If this is new work, add it: dev progress add \"<step>=<estimate>\", then close it.")
        raise StepError(f"“{text[:80]}” does not describe step “{target['name']}”"
                        f"{' (you named it)' if explicit else ' (the running step)'}.{hint} Use --force if it really "
                        f"is that step.")


def add_step(spec: str, cwd: str | None = None, now: float | None = None, why: str = "",
             from_agent: str | None = None) -> dict:
    """Add a step discovered during the work: "name=30m after other". From a crew member, the step goes to its own
    lane's plan; the main plan is Morpheus's."""
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        if not run.get("goal"):
            raise StepError("no progress run in this session: dev progress start \"<goal>\" --plan \"…\"")
        items = parse_plan(spec)
        if not items:
            raise StepError("give the step: dev progress add \"<name>=<estimate>\"")
        if from_agent:
            lane = _lane(run, from_agent, now)
            plan = lane.setdefault("plan", [])
            for name, _, _ in items:
                if name.lower() in (x.lower() for x in plan):
                    raise StepError(f"step “{name}” is already in your lane's plan")
                plan.append(name)
                run["updates"].append({"ts": round(now), "kind": "agent", "agent": from_agent, "text": f"added: {name}"})
            _save(run)
            return run
        for name, _, _ in items:
            if _find_step(run, name) and _find_step(run, name)["name"].lower() == name.lower():
                raise StepError(f"step “{name}” is already in the plan")
        for s in _build_steps(items, run["plan"]):  # an unknown dependency or a cycle is refused: the plan stays as it is
            run["plan"].append(s)
            _log_change(run, s, None, "pending", "main", why or "added during the work", now)
        _allocate(run)
        run["steps"] = len(run["plan"])
        _advance_main(run, now)
        _save(run)
    return run


def reopen(which, why: str, cwd: str | None = None, now: float | None = None, skip: bool = False,
           from_agent: str | None = None) -> dict:
    """Undo a wrong close (back to running or pending), or skip a step on purpose. Both need a reason."""
    _main_only(from_agent, "skips the plan's steps" if skip else "reopens the plan's steps")
    now = now or time.time()
    if not (why or "").strip():
        raise StepError("say why: --why \"…\" (it goes into the run's history)")
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise StepError(f"no step “{which}” in the plan")
        old = s.get("state")
        if skip:
            if old in ("done", "skipped"):
                raise StepError(f"step “{s['name']}” is already {old}")
            s.update(state="skipped", finished=now)
        else:
            if old not in ("done", "skipped", "blocked"):
                raise StepError(f"step “{s['name']}” is {old}, not closed")
            s.update(state="pending", finished=None)
            s.pop("recheck", None)  # it is redone now
            if (s.get("owner") or "main") != "main":
                s["owner"] = "main"  # back to Morpheus, still assigned to its member: dev crew next launches it again
            if old in DONE:
                _recheck_dependents(run, s, now)
            if not s.get("label") and not deps_open(run, s):
                for x in run["plan"]:  # the reopened step becomes the main lane's current one
                    if x is not s and x.get("state") == "running" and (x.get("owner") or "main") == "main":
                        x["state"] = "pending"
                s["state"] = "running"
                s["started"] = s.get("started") or now
            s.pop("untimed", None)
            s["reopened"] = (s.get("reopened") or 0) + 1
        _log_change(run, s, old, s["state"], "main", why, now)
        run["step"] = sum(1 for x in run["plan"] if x.get("state") in ("done", "skipped"))
        run["updates"].append({"ts": round(now), "kind": "reopen" if not skip else "skip", "text": why,
                               "step": s["name"]})
        _advance_main(run, now)
        _save(run)
    return run


def _recheck_dependents(run, s, now):
    """`s` was done (or skipped) and is open again. Its dependents, direct and further down, that are done or running
    stay so, flagged `recheck`: their work used the old version. A main-lane step running on `s` waits for it again."""
    plan = run.get("plan") or []
    down, frontier = [], [s["name"]]
    while frontier:
        name = frontier.pop(0)
        for x in plan:
            if name in (x.get("after") or []) and x is not s and all(x is not d for d in down):
                down.append(x)
                frontier.append(x["name"])
    for x in down:
        if x.get("state") not in ("done", "running"):
            continue  # not started: it simply waits
        x["recheck"] = (x.get("recheck") or []) + ([s["name"]] if s["name"] not in (x.get("recheck") or []) else [])
        _audit("plan.recheck", x["name"], f"{s['name']} was reopened", project=run.get("project") or "",
               run=run.get("goal", "")[:80])
        if x.get("state") == "running" and (x.get("owner") or "main") == "main" and deps_open(run, x):
            _log_change(run, x, "running", "pending", "main", f"waits again for {s['name']} (reopened)", now)
            x["state"] = "pending"


def note(text: str, nxt: str = "", cwd: str | None = None, from_agent: str | None = None,
         now: float | None = None) -> dict:
    """An update while a step is still running: recorded and shown (it resets the silence timer), and no step
    changes state. `dev progress step` is for a step that is finished."""
    now = now or time.time()
    if not (text or "").strip():
        raise StepError("say what is happening: dev progress note \"<what is happening>\" --next \"<what is next>\"")
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        if not run.get("goal"):
            raise StepError("no progress run in this session: dev progress start \"<goal>\" --plan \"…\"")
        u = {"ts": round(now), "kind": "note", "text": text.strip(), "next": nxt, "check": ""}
        if from_agent:  # a member's note stays in its lane
            lane = _lane(run, from_agent, now)
            lane["updates"] = (lane.get("updates") or [])[-19:] + [u]
            run["updates"].append({**u, "kind": "agent", "agent": from_agent})
        else:
            run["updates"].append(u)
        run["updates"] = run["updates"][-40:]
        _save(run)
    return run


def member_of_step(run: dict, s: dict):
    """The crew member who did a step: its lane's persona, or Morpheus for the main session's own steps."""
    from . import crew
    owner = s.get("owner") or "main"
    if owner == "main":
        return "morpheus"
    lane = (run.get("agents") or {}).get(owner) or {}
    m = crew._for_label(lane.get("persona") or lane.get("label") or s.get("label") or "")
    return m["id"] if m else None


def approvals_owed(run: dict) -> list:
    """[(Accountable member id, [step names])] still to approve the run's finished work. [] when the roster cannot
    be read (the check never blocks on a broken roster)."""
    try:
        from . import raci
        return raci.approvals_missing(run, lambda s: member_of_step(run, s))
    except Exception:
        return []


def step(text: str, nxt: str = "", cwd: str | None = None, kind: str = "step", check: str = "", which=None,
         from_agent: str | None = None, now: float | None = None, force: bool = False, why: str = "") -> dict:
    now = now or time.time()
    agent, sid = current_session(cwd)
    if not load_run(agent, sid):  # no run yet: start one first (start publishes, so never inside the lock)
        start(text if kind != "step" else "work in progress", cwd=cwd, now=now)
        from_agent_lane = False
    else:
        from_agent_lane = bool(from_agent)
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if from_agent_lane:  # a subagent's update: its lane only
            run = normalize(run)
            lane = _lane(run, from_agent, now)
            lane["updates"] = (lane.get("updates") or [])[-19:] + [{"ts": round(now), "kind": kind, "text": text,
                                                                    "check": check}]
            if kind == "step":
                lane["done"] = (lane.get("done") or 0) + 1
            run["updates"].append({"ts": round(now), "kind": "agent", "agent": from_agent, "text": text, "check": check})
            run["updates"] = run["updates"][-40:]
            _save(run)
            return run
        run = normalize(run or {})
        plan = run["plan"]
        target = _find_step(run, which) if which else None
        if which and not target:
            raise StepError(f"no step “{which}” in the plan: " + "; ".join(f"{i}. {s['name']}" for i, s in
                                                                          enumerate(plan, 1)))
        if kind == "step":
            explicit = target is not None
            if not target:
                running = [s for s in plan if s.get("state") == "running" and (s.get("owner") or "main") == "main"]
                if len(running) > 1:
                    raise StepError("several steps are running (" + ", ".join(s["name"] for s in running) +
                                    "): say which with --step")
                target = (running or [None])[0] or next((s for s in plan if s.get("state") in ("pending", "blocked")
                                                         and (s.get("owner") or "main") == "main"
                                                         and not s.get("label")), None)
            if not plan:  # a run without a plan: each step is recorded as it comes
                target = _new_step(text[:60], est=None)
                plan.append(target)
                _allocate(run)
            else:
                check_close(run, target, text, explicit, force)
            old_state = target.get("state")
            _finish_step(run, target, now, agent, sid)
            _log_change(run, target, old_state, "done", "main", check or text, now)
            _advance_main(run, now)
            run["state"] = "running"
        elif kind == "blocked":
            run["state"] = "blocked"
            cur = target or next((s for s in plan if s.get("state") == "running"), None)
            if cur:
                _log_change(run, cur, cur.get("state"), "blocked", "main", text, now)
                cur["state"] = "blocked"
        elif kind == "done":
            waiting = pending_decisions(run)
            if waiting and not force:
                raise StepError("decisions still waiting for the developer: " + ", ".join(
                    f"{d['id']} ({d['question'][:60]})" for d in waiting) + ". Ask, then dev crew decide <id> \"…\".")
            owed = approvals_owed(run)
            if owed and not why:
                from . import raci
                raise StepError("the work is not approved yet (the crew's RACI): " + "; ".join(
                    f"{raci.names([acc])} approves {', '.join('“' + x + '”' for x in steps)}" for acc, steps in owed) +
                    ". Add a review step for each (dev progress add \"review=20m\" then dev progress assign review --to "
                    f"{raci.handle(owed[0][0])}), or end with --why \"<reason>\".")
            if owed:
                run.setdefault("raci_overrides", []).append({"ts": round(now), "step": "(end of run)", "why": why,
                                                             "owed": [[a, s_] for a, s_ in owed]})
            open_ = [s for s in plan if s.get("state") not in ("done", "skipped") and s.get("state") != "running"]
            if open_ and not force:
                raise StepError("not finished: " + ", ".join(f"“{s['name']}” ({s.get('state')})" for s in open_) +
                                ". Close or skip each one (dev progress skip \"<step>\" --why \"…\"), or use --force.")
            for s in plan:
                if s.get("state") != "done":
                    old_state = s.get("state")
                    if s.get("state") == "running" or s is target:
                        _finish_step(run, s, now, agent, sid)
                    else:
                        s["state"] = "skipped"
                    _log_change(run, s, old_state, s["state"], "main", text, now)
            run.update(state="done", finished=now)
            _close(run, "turn", None, now)
            record("run", agent, sid, run.get("goal", ""), active_minutes(run, now=now) * 60, run.get("project", ""),
                   estimate=run.get("estimate") or 0, steps=len(plan), wall=round(wall_minutes(run, now) * 60, 1),
                   basis="active")
        run["steps"] = len(plan)
        run["step"] = sum(1 for s in plan if s.get("state") in ("done", "skipped"))
        run["updates"].append({"ts": round(now), "kind": kind, "text": text, "next": nxt, "check": check,
                               "step": target["name"] if target else ""})
        run["updates"] = run["updates"][-40:]
        _save(run)
    _publish()
    if kind in ("blocked", "done"):
        notify(f"{run.get('project') or 'devkit'} · {kind}", text)
    return run


def crew_summary(run: dict, limit: int = 4) -> str:
    """Who in the crew works on what in this run, running first: "Tank ▸ the API · Kid ✓ installer". '' without
    crew members (the agent's own helpers are not the crew)."""
    from . import crew
    lanes = []
    for aid, lane in (run.get("agents") or {}).items():
        m = crew._for_label(lane.get("persona") or "") if lane.get("persona") else None
        if not m:
            continue
        step_ = next((s["name"] for s in run.get("plan") or [] if s.get("owner") == aid), "")
        what = step_ or (lane.get("label") or "").split(":", 1)[-1].strip()
        mark = {"running": "▸", "done": "✓", "lost": "✗"}.get(lane.get("state"), "·")
        lanes.append((lane.get("state") != "running", f"{crew.who(m)} {mark} {what}".strip()))
    lanes.sort(key=lambda x: x[0])
    parts = [x for _, x in lanes[:limit]]
    return " · ".join(parts) + (f" · +{len(lanes) - limit}" if len(lanes) > limit else "")


def render_update(run: dict, branch: str | None = None, now: float | None = None) -> str:
    """The progress update as the devkit status block (global instructions §0), ready to send: the block, then the
    plan with each step's state, owner and time, then the subagents."""
    from .canary import MARKER
    now = now or time.time()
    run = normalize(run)
    last = next((u for u in reversed(run.get("updates") or []) if u.get("kind") != "agent"), {})
    if branch is None:
        branch = core.gitout(run.get("cwd") or os.getcwd(), "branch", "--show-current") or "-"
    project = (run.get("project") or "-").split("/")[-1]
    state = run.get("state")
    done = last.get("text") if last.get("kind") in ("step", "done", "blocked", "note") else ""
    check = (last.get("check") or "").strip()
    if check and check[0] not in "✓⚠✗–":
        check = "✓ " + check
    if not check:
        check = "✗ blocked" if state == "blocked" else "– the step is still running" if last.get("kind") == "note" \
            else "⚠ not verified at this step"
    nxt = last.get("next") or ("—" if state == "done" else "")
    if state == "blocked":
        nxt = f"unblock: {nxt or done}"
    plan = run.get("plan") or []
    agents = run.get("agents") or {}
    lines = [f"**{MARKER}** {project} · `{branch}` · {run.get('goal', '')}",
             f"- **Done:** {done or 'started'}",
             f"- **Check:** {check}",
             f"- **Progress:** {headline(run, now)}"]
    crew_line = crew_summary(run)
    if crew_line:
        lines.append(f"- **Crew:** {crew_line}")
    waiting = pending_decisions(run)
    if waiting:
        lines.append("- **You:** decide " + "; ".join(f"“{d['question'][:70]}”" for d in waiting[:2])
                     + (f" (+{len(waiting) - 2} more)" if len(waiting) > 2 else ""))
    lines.append(f"- **Next:** {nxt or 'continue the plan'}")
    if plan:
        lines.append("")
        for i, s in enumerate(plan, 1):
            mark = {"done": "✓", "running": "▸", "blocked": "■", "skipped": "–"}.get(s.get("state"), "○")
            main = (s.get("owner") or "main") == "main"
            waits = deps_open(run, s) if s.get("state") in ("pending", "blocked") else []
            parts = [] if main else [agents.get(s["owner"], {}).get("label") or "subagent"]
            if waits:
                parts.append("waiting for " + ", ".join(waits) + (f", then {s['label']}" if main and s.get("label")
                                                                  else ""))
            elif main and s.get("label"):
                parts.append(f"waiting for {s['label']}")
            if s.get("recheck"):
                parts.append(f"recheck: {', '.join(s['recheck'])} reopened")
            who = "".join(f" · {p}" for p in parts)
            if s.get("state") in ("done", "running"):
                tm = f" · {fmt_min(step_minutes(run, s, now))} of ~{fmt_min(s.get('est') or 0)}"
            else:
                tm = f" · ~{fmt_min(s.get('est') or 0)}"
            lines.append(f"{mark} {i}. {s['name']}{who}{tm}")
    if agents:
        parts = []
        for aid, a in agents.items():
            mins = active_minutes(run, a.get("started"), a.get("finished") or now, now, aid)
            parts.append(f"{'▸' if a.get('state') == 'running' else '✓'} {a.get('label') or a.get('type') or aid[:8]}"
                         f" {fmt_min(mins)}")
        lines.append("Agents: " + " · ".join(parts))
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


def brief(run: dict, now: float | None = None) -> dict:
    """Compact form for presence and the dashboard."""
    now = now or time.time()
    run = normalize(run)
    last = next((u for u in reversed(run.get("updates") or []) if u.get("kind") != "agent"), {})
    lo, hi = eta_range(run, now) if run.get("state") == "running" else (None, None)
    return {"goal": run.get("goal"), "state": run.get("state"), "step": run.get("step"), "steps": run.get("steps"),
            "estimate": run.get("estimate"), "started": round(run.get("started", 0)),
            "eta_min": round(eta(run, now)) if run.get("state") == "running" else None, "eta_lo": lo, "eta_hi": hi,
            "active_min": round(active_minutes(run, now=run.get("finished") or now), 1),
            "wall_min": round(wall_minutes(run, now), 1), "pace": round(pace(run, now)[0], 2),
            "plan": [{"name": s["name"], "state": s.get("state"), "est": s.get("est"),
                      "min": round(step_minutes(run, s, now), 1),
                      "owner": "main" if (s.get("owner") or "main") == "main"
                      else ((run.get("agents") or {}).get(s["owner"], {}).get("label") or "subagent")}
                     for s in run.get("plan") or []],
            "agents": [{"label": a.get("label") or a.get("type") or k[:8], "state": a.get("state"),
                        "min": round(active_minutes(run, a.get("started"), a.get("finished") or now, now, k), 1)}
                       for k, a in (run.get("agents") or {}).items()],
            "headline": headline(run, now), "last": last.get("text", ""), "next": last.get("next", ""),
            "updated": last.get("ts")}


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
        try:
            state.update(f, lambda d: d.update({"turn_started": now, "last_update": now, "nudges": 0}), create=False)
        except state.StateBusy:
            pass  # audited by state: this prompt's clock is dropped, the hook still answers
    _run_event(agent, sid, lambda run: (_close(run, "turn", None, now), _open(run, "turn", "main", now)))
    prompt = (data.get("prompt") or "")[:400]
    mail = "\n\n".join(x for x in (messages_context(agent, sid, "main"), decisions_context(agent, sid),
                                    _ready(agent, sid), _ci(agent, sid)) if x) if f.exists() else ""
    if not STATUS_Q.search(prompt):
        return mail or None
    runs = [r for r in active_runs() if r.get("session") == sid] or active_runs()[:3]
    ctx = ["devkit · the developer is asking for status. Answer with the Overall status template of the "
           "`communicating` skill: one-line verdict, then per workstream: state, evidence, ETA; CI/CD pipelines with "
           "their state, current job and step, elapsed and ETA; what is left; risks; what you need from them. Use "
           "these real records:"]
    for r in runs:
        ctx.append(f"- run “{r.get('goal')}” ({r.get('agent')}): {headline(r)}; last: "
                   f"{(r.get('updates') or [{}])[-1].get('text', '-')}")
    top = s.get("top") or ""
    if top and not core.is_temp(top):  # CI/CD runs of this branch: jobs, current step, elapsed, ETA
        try:
            from . import cistatus
            branch = core.gitout(top, "branch", "--show-current") or None
            ctx += ["- " + ln for ln in cistatus.context_lines(top, branch, limit=2)]
            ctx.append("  (report each pipeline's state, step and ETA; `dev ci status --watch` follows a run)")
        except Exception:
            pass
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
    me = data.get("agent_id") or "main"
    mail = messages_context(agent, sid, me)
    if me != "main":  # inside a subagent: deliver its messages; updates to the developer are the main session's job
        return mail or None
    mail = "\n\n".join(x for x in (mail, decisions_context(agent, sid), _ready(agent, sid), _ci(agent, sid)) if x)
    now = time.time()
    said = _last_assistant_text_ts(data.get("transcript_path")) if agent == "claude" else None
    last = max(x for x in (s.get("turn_started") or s.get("started") or now, s.get("last_update") or 0,
                           s.get("last_nudge") or 0, said or 0))
    quiet = (now - last) / 60
    if quiet < cfg()["nudge_minutes"] or (s.get("nudges") or 0) >= 6:
        return mail or None
    try:
        state.update(f, lambda d: d.update(last_nudge=now, nudges=(d.get("nudges") or 0) + 1), create=False)
    except state.StateBusy:
        pass  # audited by state; the nudge still goes out
    run = load_run(agent, sid)
    where = f" ({headline(run)})" if run else ""
    nudge = (f"devkit · {fmt_min(quiet)} since your last update to the developer{where}. Send an update (it prints "
             f"the status block): while the step is still running, `dev progress note \"<what is happening>\" --next "
             f"\"<what is next>\"`, which changes no step; when it is finished, `dev progress step \"<what finished>\" "
             f"--check \"<evidence>\"`. Never close a step that is not finished. If you are over your estimate, say so "
             f"and give a new one.")
    return "\n\n".join(x for x in (mail, nudge) if x)


def _run_event(agent, sid, fn):
    """Apply fn to this session's live run (running or blocked) and save it, under the session's lock. Never raises:
    a run that is busy or does not parse drops the event (state audits it)."""
    try:
        with _tx(agent, sid):
            run = load_run(agent, sid)
            if not run or run.get("state") == "done":
                return
            run = normalize(run)
            expire_stale(run)
            fn(run)
            _save(run, touch_session=False)
    except Exception:
        pass


def note_agent_launch(agent: str, data: dict, now: float | None = None):
    """PreToolUse of a subagent launch; see _note_agent_launch. Serialized per session (state.hold): parallel
    launches and starts run in separate hook processes that update the same session record."""
    with state.hold(session_path(agent, data.get("session_id") or "-")):
        return _note_agent_launch(agent, data, now)


LAUNCH_TTL = 15 * 60  # a launch whose agent has not started by then no longer holds a crew slot
LAUNCH_KEEP = 24 * 3600  # launch records are kept a day (spec C2), the newest LAUNCH_MAX of them
LAUNCH_MAX = 100
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _launches(s: dict, now: float) -> dict:
    """The session's launch records (spec C2), {tool_use_id: record}, without day-old ones. The queue of
    descriptions that came before them (`pending_agents`, taken first in, first out) is dropped."""
    s.pop("pending_agents", None)
    ls = {k: v for k, v in (s.get("launches") or {}).items()
          if isinstance(v, dict) and now - (v.get("ts") or now) < LAUNCH_KEEP}
    if len(ls) > LAUNCH_MAX:
        ls = dict(sorted(ls.items(), key=lambda kv: kv[1].get("ts") or 0)[-LAUNCH_MAX:])
    s["launches"] = ls
    return ls


def _note_agent_launch(agent: str, data: dict, now: float | None = None):
    """PreToolUse of a subagent launch (Claude's Agent/Task, Codex's spawn_agent), after the guard allowed it: a
    launch record keyed by the call's tool_use_id, with its description, type and caller (the launching subagent's
    agent_id, or main). The agent it starts binds to exactly this record (see bind); nothing takes it by order. A
    launch without a tool_use_id (Codex today) holds a crew slot for LAUNCH_TTL and never binds. Runs under
    note_agent_launch's hold."""
    sid = data.get("session_id") or "-"
    f = session_path(agent, sid)
    s = state.load(f)
    if s is None:
        return
    inp = data.get("tool_input") or {}
    label = inp.get("description") or inp.get("nickname") or inp.get("name") or \
        str(inp.get("message") or inp.get("prompt") or "").strip().split("\n")[0][:60]
    now = now or time.time()
    tid = str(data.get("tool_use_id") or "")
    ls = _launches(s, now)
    entry = {"tool_use_id": tid or None, "label": str(label)[:80],
             "type": inp.get("subagent_type") or inp.get("agent_type") or "", "parent": data.get("agent_id") or "main",
             "ts": round(now, 1), "state": "pending", "agent_id": None}
    if tid:
        if tid in ls:
            return  # the same call recorded twice
        ls[tid] = entry
    elif any(not v.get("tool_use_id") and (v.get("label"), v.get("type")) == (entry["label"], entry["type"])
             and abs(now - (v.get("ts") or 0)) < 10 for v in ls.values()):
        return  # the same untracked launch recorded twice
    else:
        ls[f"untracked-{now:.3f}"] = entry
    state.replace(f, s)


def _launch_meta(data: dict) -> dict:
    """Claude Code's record of the Agent call that started this subagent,
    `<main transcript>/../<session>/subagents/agent-<agent_id>.meta.json`, with toolUseId and description. It is
    written after the SubagentStart hooks return (spec F0, S1), so it is there from the agent's first tool call on.
    {} before that, for the agent's own helpers, and on Codex."""
    main, aid = str(data.get("transcript_path") or ""), str(data.get("agent_id") or "")
    if not main.endswith(".jsonl") or not AGENT_ID.fullmatch(aid):
        return {}
    meta = core.read_json(Path(main[:-len(".jsonl")]) / "subagents" / f"agent-{aid}.meta.json", {}) or {}
    return meta if isinstance(meta, dict) else {}


def _claim(s: dict, data: dict, aid: str, now: float):
    """The launch this agent belongs to -> (tool_use_id, record, binding). `exact` when the launch's PostToolUse
    named this agent, or its meta file names a launch no agent has claimed (the record then gets this agent and
    state started); `conflict` when the meta file names a launch bound to another agent (a launch binds once);
    (None, None, None) while nothing names one."""
    ls = _launches(s, now)
    tid = next((k for k, v in ls.items() if v.get("agent_id") == aid), None)
    if tid:
        return tid, ls[tid], "exact"
    tid = str(_launch_meta(data).get("toolUseId") or "")
    rec = ls.get(tid) if tid else None
    if not rec:
        return None, None, None
    if rec.get("agent_id"):
        return tid, rec, "conflict"
    rec.update(agent_id=aid, state="started")
    return tid, rec, "exact"


def _task_text(label: str) -> str:
    """The task part of a launch description: "Neo: session graph" -> "session graph"."""
    head, sep, rest = (label or "").partition(":")
    return rest.strip() if sep and rest.strip() and len(head.split()) <= 3 else (label or "")


def _name_score(text: str, name: str) -> tuple:
    """How well a launch's task text names a step: (the same name, how many of the step's words it names, that
    share of the step's words). (False, 0, 0.0) when it names none."""
    sw, tw = _words(name), _words(text)
    hit = sum(1 for b in sw if any(a == b or _same_stem(a, b) for a in tw))
    return (text.strip().lower() == name.strip().lower(), hit, hit / len(sw) if sw else 0.0)


def _bind_step(run: dict, aid: str, lane: dict, now: float):
    """Give a newly bound lane the plan step its launch names (spec F2): of the steps assigned to its member that
    the dependency engine finds ready, the one whose name the launch's task text names best ("Neo: session graph"
    → "session graph command"). Never the member's first step by default: when the text names none of its steps,
    or two equally, they stay with Morpheus and he is told how to link one."""
    plan = run.get("plan") or []
    if any(st.get("owner") == aid and st.get("state") not in DONE for st in plan):
        return  # one step at a time
    mine = [st for st in plan if st.get("label") and st.get("state") not in DONE
            and (st.get("owner") or "main") == "main" and _label_matches(st["label"], lane)]
    if not mine:
        return
    who, label = lane.get("persona") or lane.get("label") or "a subagent", lane.get("label") or ""
    scored = [(_name_score(_task_text(label), st["name"]), st) for st in mine]
    best = max(sc for sc, _ in scored)
    top = [st for sc, st in scored if sc == best]
    if not best[1] or len(top) > 1:
        names = ", ".join(f"“{st['name']}”" for st in (top if best[1] else mine))
        what = f"names {names} equally" if best[1] else f"names none of {mine[0]['label']}'s steps ({names})"
        _post(run, "devkit", "main", f"{who} started as “{label}”, which {what}: the steps stay with you. If this "
              f"launch does one of them, link it: dev progress assign \"<step>\" --to \"{mine[0]['label']}\"", now,
              from_name="devkit")
        return
    st = top[0]
    if not any(x is st for x in ready(run)):
        w = ", ".join(deps_open(run, st))
        if w:  # launched too early: the step keeps waiting for its dependencies
            _post(run, "devkit", "main", f"{who} started, but “{st['name']}” waits for {w}, not done yet: the step "
                  f"stays pending and this lane runs without it. When {w} is done, link it: dev progress assign "
                  f"\"{st['name']}\" --to \"{st['label']}\"", now, from_name="devkit")
        return
    st.update(owner=aid, state="running", started=st.get("started") or now)
    if st.get("scope"):
        lane["scope"] = st["scope"]


def _lane_event(run, aid, kind, tid, rec, binding, now, start, waiting, own=True):
    """The run's side of a binding (spec C3): the agent's lane, created provisional at its start when a launch is
    waiting (no step, no scope, until it binds), resumed when the agent works again after it had stopped, and bound
    to its launch: label, persona and the step the launch names. `own`: the hook is the agent's own start or tool
    call, so a finished lane resumes; never for its launch's result or its stop."""
    lane = run["agents"].get(aid)
    if lane is None:
        if not binding and not (start and waiting):
            return  # a helper of the agent (compaction, suggestions), or an agent devkit never saw launched
        lane = _lane(run, aid, now, kind, kind)
        lane.update(binding="pending", launch=None, persona=persona(kind, ""))
        _open(run, "agent", aid, now)
        _audit("crew.start", lane["persona"] or kind or aid[:8], "waits for its launch" if not binding else
               (rec or {}).get("label") or "", project=run.get("project") or "", agent_id=aid[:16], type=kind)
    elif own and lane.get("state") != "running":  # resumed (SendMessage): its own lane, and no other step
        lane.update(state="running", finished=None, resumed=(lane.get("resumed") or 0) + 1, resumed_at=now)
        _open(run, "agent", aid, now)
        _audit("crew.resume", lane.get("persona") or lane.get("label") or aid[:8], lane.get("label") or "",
               project=run.get("project") or "", agent_id=aid[:16])
    if binding == "exact" and lane.get("binding") == "pending":
        label = rec.get("label") or lane.get("label") or kind
        lane.update(binding="exact", launch=tid, label=label, parent=rec.get("parent") or "main",
                    persona=persona(kind or rec.get("type") or "", label))
        _audit("crew.bind", lane["persona"] or label, label, project=run.get("project") or "", agent_id=aid[:16],
               launch=str(tid)[:40])
        if lane.get("state") == "running":
            _bind_step(run, aid, lane, now)
    elif binding == "conflict" and lane.get("binding") == "pending":
        lane["binding"] = "conflict"
        _audit("crew.bind-conflict", lane.get("persona") or kind or aid[:8], f"claims launch {tid}, which another "
               f"agent is bound to", "deny", project=run.get("project") or "", agent_id=aid[:16])


def _attach(agent: str, sid: str, s: dict, data: dict, aid: str, kind: str, now: float, start: bool,
            resume: bool = True):
    """Bind this agent to its launch, inside the session's hold: the claim on the session record, then the lane
    on the run (lock order: session, then run). `resume`: a finished lane of this agent works again."""
    if not AGENT_ID.fullmatch(aid or ""):
        return
    tid, rec, binding = _claim(s, data, aid, now)
    waiting = any(v.get("state") == "pending" and not v.get("agent_id") and now - (v.get("ts") or now) < LAUNCH_TTL
                  for v in (s.get("launches") or {}).values())
    subs = s.setdefault("subagents", {})
    if start or binding or aid in subs:
        sub = subs.setdefault(aid, {"type": kind, "label": kind, "started": now})
        if start:
            sub["started"] = now
        if binding == "exact":
            sub.update(label=rec.get("label") or sub.get("label"), launch=tid)
    _run_event(agent, sid, lambda run: _lane_event(run, aid, kind, tid, rec, binding, now, start, waiting,
                                                   own=resume))


def bind(agent: str, data: dict, now: float | None = None, resume: bool = True):
    """Late binding (spec D2), at every hook from inside a subagent (its tool calls, its stop). SubagentStart
    carries neither the launch's tool_use_id nor a meta file yet, so an agent that starts before its launch's
    result binds at its first hook, when the meta file exists. A known agent whose lane had stopped resumes it at
    a tool call (SendMessage); `resume=False` at its stop. The usual case, a bound working lane, costs one read of
    the run."""
    aid, sid = str(data.get("agent_id") or ""), data.get("session_id") or "-"
    if not AGENT_ID.fullmatch(aid):
        return
    run = load_run(agent, sid)
    lane = ((run.get("agents") or {}).get(aid) or {}) if isinstance(run, dict) and run.get("state") != "done" else {}
    if lane.get("state") == "running" and lane.get("binding", "exact") in ("exact", "conflict"):
        return
    f = session_path(agent, sid)
    if not lane and ((state.read(f) or {}).get("subagents") or {}).get(aid, {}).get("launch"):
        return  # bound, in a session without a run
    with state.hold(f):
        s = state.load(f)
        if s is None:
            return
        before = json.dumps(s, sort_keys=True)
        kind = data.get("agent_type") or data.get("subagent_type") or lane.get("type") or ""
        _attach(agent, sid, s, data, aid, kind, now or time.time(), start=False, resume=resume)
        if json.dumps(s, sort_keys=True) != before:
            state.replace(f, s)


def launch_result(agent: str, data: dict, now: float | None = None):
    """PostToolUse of a subagent launch: its result names the agent it started (`tool_response.agentId`) next to
    the launch's tool_use_id, an exact binding without the meta file (spec F0). A background launch's result comes
    together with its SubagentStart, in either order; a foreground launch's only when the agent has ended."""
    resp = data.get("tool_response")
    aid = str(resp.get("agentId") or "") if isinstance(resp, dict) else ""
    tid, sid = str(data.get("tool_use_id") or ""), data.get("session_id") or "-"
    if not tid or not AGENT_ID.fullmatch(aid):
        return
    f = session_path(agent, sid)
    with state.hold(f):
        s = state.load(f)
        if s is None:
            return
        now = now or time.time()
        rec = _launches(s, now).get(tid)
        if not rec:
            return  # not a launch devkit recorded
        if rec.get("agent_id") and rec["agent_id"] != aid:
            _audit("crew.bind-conflict", rec.get("label") or tid, f"the launch's result names {aid[:16]}, but "
                   f"{str(rec['agent_id'])[:16]} is bound to it", "deny", project=s.get("project") or "")
            return
        rec.update(agent_id=aid, state="started")
        sub = (s.get("subagents") or {}).get(aid)
        kind = (sub or {}).get("type") or rec.get("type") or ""
        if sub is not None:
            sub.update(label=rec.get("label") or sub.get("label"), launch=tid)
        _run_event(agent, sid, lambda run: _lane_event(run, aid, kind, tid, rec, "exact", now, False, False,
                                                       own=False) if sub is not None or aid in run["agents"] else None)
        state.replace(f, s)


def launch_failed(agent: str, data: dict, now: float | None = None):
    """PostToolUseFailure of a subagent launch (an unknown agent type, an interrupt; spec F0, S6): its launch
    record leaves at once, so it never labels another agent or holds a crew slot."""
    tid, sid = str(data.get("tool_use_id") or ""), data.get("session_id") or "-"
    if not tid:
        return
    (PENDING / f"{re.sub(r'[^A-Za-z0-9_-]', '', tid)[:80]}.json").unlink(missing_ok=True)
    f = session_path(agent, sid)
    with state.hold(f):
        s = state.load(f)
        if s is None:
            return
        rec = _launches(s, now or time.time()).pop(tid, None)
        if rec is None:
            return
        state.replace(f, s)
    _audit("crew.launch-failed", rec.get("label") or tid, str(data.get("error") or "")[:200], "failed",
           project=s.get("project") or "")


def slots(run: dict, sess: dict | None = None, now: float | None = None) -> list:
    """The crew slots in use, [(id, name)] (spec D2): launches whose agent has not started yet, and working lanes
    bound to a launch (a resumed one too). A provisional lane counts through its launch; helpers never count. Lanes
    bound before launch records existed count while they run."""
    now = now or time.time()
    if sess is None:
        sess = core.read_json(session_path(run.get("agent") or "", run.get("session") or ""), {}) if run else {}
    sess = sess if isinstance(sess, dict) else {}
    agents, subs = (run or {}).get("agents") or {}, sess.get("subagents") or {}
    out = []
    for tid, rec in (sess.get("launches") or {}).items():
        aid = rec.get("agent_id")
        if rec.get("state") == "pending" and now - (rec.get("ts") or now) < LAUNCH_TTL:
            out.append((tid, rec.get("label") or rec.get("type") or "a launch"))
        elif rec.get("state") == "started" and aid not in agents and aid in subs:
            out.append((aid, rec.get("label") or str(aid)[:8]))  # working in a session without a run
    out += [(k, v.get("persona") or v.get("label") or k[:8]) for k, v in agents.items()
            if v.get("state") == "running" and v.get("binding", "exact") == "exact"]
    return out


def on_subagent(agent: str, data: dict, started: bool, now: float | None = None):
    """SubagentStart / SubagentStop; see _on_subagent. Serialized per session like note_agent_launch: one hold covers
    the session record and the run (the run's save inside re-enters it)."""
    with state.hold(session_path(agent, data.get("session_id") or "-")):
        return _on_subagent(agent, data, started, now)


def _on_subagent(agent: str, data: dict, started: bool, now: float | None = None):
    """SubagentStart: the agent binds to its launch when something names it already (the launch's result came
    first), else gets a provisional lane while a launch waits, and no lane as a helper; a known agent resumes its
    own lane and takes nothing else. SubagentStop: a last binding (an agent that used only tools without hooks),
    then its lane and its step are closed."""
    sid = data.get("session_id") or "-"
    f = session_path(agent, sid)
    s = state.load(f)  # under on_subagent's hold
    if s is None:
        return
    now = now or time.time()
    subs = s.setdefault("subagents", {})
    aid = str(data.get("agent_id") or data.get("subagent_id") or "?")
    kind = data.get("agent_type") or data.get("subagent_type") or ""
    _attach(agent, sid, s, data, aid, kind, now, start=started, resume=started)  # a stop never resumes a lane
    if not started:
        v = subs.pop(aid, None)
        if v:
            record("subagent", agent, sid, v.get("label") or v.get("type") or aid, now - v["started"],
                   s.get("project") or "")

        def finish(run):
            _close(run, "agent", aid, now)
            lane = (run.get("agents") or {}).get(aid)
            if lane and lane.get("binding") == "pending" and not lane.get("updates") and \
                    not any(st.get("owner") == aid for st in run.get("plan") or []):
                del run["agents"][aid]  # never bound, never reported: a helper that started while a launch waited
                return
            if lane:
                lane.update(state="done", finished=now)
                _audit("crew.finish", lane.get("persona") or lane.get("label") or aid,
                       f"{active_minutes(run, lane.get('started'), now, now, aid):.1f} active minutes",
                       project=run.get("project") or "", agent_id=aid[:16])
            for st in run.get("plan") or []:
                if st.get("owner") == aid and st.get("state") != "done":
                    old_state = st.get("state")
                    _finish_step(run, st, now, agent, sid)
                    _log_change(run, st, old_state, "done", aid, f"{(lane or {}).get('persona') or 'subagent'} "
                                                                 f"finished", now)
            run["step"] = sum(1 for x in run.get("plan") or [] if x.get("state") in ("done", "skipped"))
            _advance_main(run, now)
        _run_event(agent, sid, finish)
    state.replace(f, s)


def crew_working(run: dict) -> list:
    """The crew members working right now in this run (running lanes with a persona); [] when none."""
    return [lane.get("persona") for lane in (run.get("agents") or {}).values()
            if lane.get("state") == "running" and lane.get("persona")]


def lane_unreported(run: dict, aid: str, now: float | None = None, min_seconds: float = 180) -> bool:
    """A crew member who owns a plan step, worked at least `min_seconds`, and never used devkit's communication
    (`dev progress step|note` from its lane). Read-only members (no shell) report in their message instead."""
    now = now or time.time()
    lane = (run.get("agents") or {}).get(aid) or {}
    if not lane or lane.get("updates") or now - (lane.get("started") or now) < min_seconds:
        return False
    if not any(s.get("owner") == aid for s in run.get("plan") or []):
        return False
    try:
        from . import crew
        m = crew._for_label(lane.get("persona") or lane.get("label") or "")
    except Exception:
        m = None
    return not (m and m.get("tools") == "read")


def on_turn_end(agent: str, sid: str, sess: dict):
    """Stop: close the turn's activity interval; record the turn; notify when a long turn finishes."""
    now = time.time()
    _run_event(agent, sid, lambda run: _close(run, "turn", None, now))
    sess["turn_ended"] = now
    t0 = sess.get("turn_started")
    if not t0:
        return
    secs = now - t0
    record("turn", agent, sid, sess.get("status", "")[:120], secs, sess.get("project") or "")
    if secs / 60 >= cfg()["notify_after_minutes"]:
        notify(f"{agent} finished · {fmt_min(secs / 60)}", (sess.get("status") or "turn finished")[:180])


# --------------------------------------------------------------------------- reports

def report(days: int = 7, project: str | None = None) -> str:
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
