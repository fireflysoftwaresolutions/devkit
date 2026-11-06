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

import hashlib
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import core, state, ui
from .core import STATE

RUNS = STATE / "runs"
TIMING = STATE / "timing"
PENDING = STATE / "timing" / "pending"
SESSIONS = STATE / "sessions"
QUOTA = STATE / "quota"  # the plan's usage windows, recorded by the status line (hooks.statusline)
CALLS = STATE / "calls"  # the call ledger (spec C4): <session>/<tool_use_id>.json, one per open shell call


TEAM_POLICY = core.HANDOFF / "policy" / "progress.json"  # the team's ceiling on max_parallel (spec P-fxtwig4hqvx R1)


def cfg() -> dict:
    """The progress section: devkit's defaults (core.DEFAULTS), with each valid value of the configuration; a wrong
    value keeps its default (`dev config validate` names it)."""
    return core.progress_config(core.CFG.get("progress") or {})[0]


# --------------------------------------------------------------------------- which session am I?

def current_session(cwd: str | None = None):
    """-> (agent, session id) of the agent session running this command; see resolve_session."""
    return resolve_session(cwd)[:2]


def resolve_session(cwd: str | None = None):
    """-> (agent, session id, how) of the session running this command (spec C4):
    ancestry  the nearest parent process whose pid a session record holds (the agent's own process, recorded at
              SessionStart), so two sessions in one repository each find their own; DEVKIT_SESSION only chooses
              among the sessions of that process (after /clear)
    env       DEVKIT_SESSION, which Claude Code exports to the session's shells (no parent matched)
    recency   the most recently active devkit session in the same repository (Codex without a recorded pid)
    shell     none of these: a shell of the developer's, with a session of its own"""
    env = os.environ.get("DEVKIT_SESSION", "")
    pids = _ancestor_pids()
    held = {}
    for f in (SESSIONS.glob("*.json") if pids and SESSIONS.exists() else []):
        s = core.read_json(f, {}) or {}
        if isinstance(s.get("pid"), int) and s["pid"] in pids and s.get("agent") and s.get("session"):
            held.setdefault(s["pid"], []).append((s.get("started") or 0, s["agent"], s["session"]))
    for pid in pids:
        if pid in held:
            named = [c for c in held[pid] if f"{c[1]}:{c[2]}" == env]
            _, agent, sid = named[0] if named else max(held[pid])
            return agent, sid, "ancestry"
    if ":" in env:
        agent, sid = env.split(":", 1)
        return agent, sid, "env"
    cwd = cwd or os.getcwd()
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
    best = None
    for f in SESSIONS.glob("*.json") if SESSIONS.exists() else []:
        s = core.read_json(f, {}) or {}
        if (s.get("top") or s.get("cwd")) == top and (not best or f.stat().st_mtime > best[0]):
            best = (f.stat().st_mtime, s.get("agent"), s.get("session"))
    if best and time.time() - best[0] < 12 * 3600:
        return best[1], best[2], "recency"
    return os.environ.get("DEVKIT_AGENT") or "human", f"shell-{os.getppid()}", "shell"


def _ps() -> dict:
    """{pid: (parent pid, command line)} of every process, from `ps`; {} when ps gives no answer."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,args="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    procs = {}
    for ln in out.splitlines():
        parts = ln.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return procs


_ANCESTRY: dict = {}


def _ancestor_pids() -> list:
    """This process's parents, nearest first, up to launchd (one `ps` per process)."""
    if os.getpid() not in _ANCESTRY:
        procs, chain, pid = _ps(), [], os.getppid()
        while pid > 1 and pid not in chain and len(chain) < 64:
            chain.append(pid)
            pid = procs.get(pid, (0, ""))[0]
        _ANCESTRY[os.getpid()] = chain
    return _ANCESTRY[os.getpid()]


def agent_pid(procs: dict | None = None):
    """The agent process (Claude Code, Codex) this process runs below: its nearest parent whose command line is an
    agent's. None outside an agent."""
    procs = _ps() if procs is None else procs
    pid, seen = os.getpid(), 0
    while pid in procs and seen < 64:
        pid, seen = procs[pid][0], seen + 1
        if pid in procs and core.is_agent_process(procs[pid][1]):
            return pid
    return None


# The call ledger (decision D3, contract C4, security consultation §1). The PreToolUse hook writes one entry per
# shell call of an agent session, keyed by the call's tool_use_id, with who made it: the subagent's agent_id from the
# harness payload, or main. Its PostToolUse or PostToolUseFailure, or the hook's own refusal, removes it. A `dev`
# command finds its session (resolve_session), then the open calls whose text holds its own arguments, in order:
# main only when every match is main's, the member when any is a member's, unknown with none. No variable, parent
# process or recency ever makes a caller main. Hooks never ask who calls: their payload says it.

CALL_TTL = 600  # an open call expires after its tool's timeout, 10 minutes at most; it is audited then
CALL_TEXT = 4096  # characters of a call's command kept, redacted (its start and its end when it is longer)
NOT_A_LANE = ("main", "unknown", "unverified", "human")


def _safe_name(s) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(s or ""))[:120]


def _write_private(path: Path, doc: dict):
    """An atomic write, 0600 in 0700 folders, without a lock: each entry has one writer."""
    for d in (path.parent.parent, path.parent):  # the ledger, and the session's folder in it
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def call_opened(agent: str, data: dict, now: float | None = None):
    """PreToolUse of a shell call: its ledger entry {tool_use_id, session, agent, caller (agent_id or main), depth,
    ts, ttl, cmd_sha256, cmd (redacted, at most CALL_TEXT), cut}."""
    tid, sid = _safe_name(data.get("tool_use_id")), _safe_name(data.get("session_id"))
    inp = data.get("tool_input") or {}
    cmd = inp.get("command")
    if isinstance(cmd, list):
        cmd = " ".join(shlex.quote(str(c)) for c in cmd)
    if not tid or not sid or not isinstance(cmd, str) or not cmd.strip():
        return
    from . import audit
    now = now or time.time()
    aid = str(data.get("agent_id") or "")[:80]
    depth = 0
    if aid:
        lane = ((load_run(agent, data.get("session_id")) or {}).get("agents") or {}).get(aid) or {}
        depth = int(lane.get("depth") or 1)
    timeout = inp.get("timeout")
    ttl = min(CALL_TTL, max(1.0, timeout / 1000)) if isinstance(timeout, (int, float)) and timeout > 0 else CALL_TTL
    text = audit.redact(cmd)
    cut = len(text) > CALL_TEXT
    if cut:
        text = text[:CALL_TEXT // 2] + " … " + text[-CALL_TEXT // 2:]
    _write_private(CALLS / sid / f"{tid}.json", {
        "tool_use_id": tid, "session": sid, "agent": agent, "caller": aid or "main", "depth": depth,
        "ts": round(now, 3), "ttl": ttl, "cmd_sha256": hashlib.sha256(cmd.encode()).hexdigest(), "cmd": text,
        "cut": cut})


def call_entry(data: dict) -> dict:
    """The open ledger entry of this call; {} when it has none."""
    tid, sid = _safe_name(data.get("tool_use_id")), _safe_name(data.get("session_id"))
    e = core.read_json(CALLS / sid / f"{tid}.json", None) if tid and sid else None
    return e if isinstance(e, dict) else {}


def call_note(data: dict, **fields):
    """Add fields to this call's open ledger entry (the hooks keep a read-only lane's worktree state there)."""
    e = call_entry(data)
    if e:
        _write_private(CALLS / _safe_name(data.get("session_id")) / f"{_safe_name(data.get('tool_use_id'))}.json",
                       {**e, **fields})


def call_closed(data: dict):
    """PostToolUse or PostToolUseFailure of a shell call, or the hook refused it: its entry leaves the ledger."""
    tid, sid = _safe_name(data.get("tool_use_id")), _safe_name(data.get("session_id"))
    if tid and sid:
        try:
            (CALLS / sid / f"{tid}.json").unlink()
        except OSError:
            pass


def open_calls(sid: str, now: float | None = None) -> list:
    """The session's open calls, oldest first. An entry past its ttl got no result (the tool was cut off, or another
    hook refused it): it is removed and audited as calls.unclosed."""
    now = now or time.time()
    try:
        files = [f for f in (CALLS / _safe_name(sid)).iterdir() if f.suffix == ".json" and not f.name.startswith(".")]
    except OSError:
        return []
    out = []
    for f in files:
        e = core.read_json(f, None)
        if not isinstance(e, dict):
            continue
        if now - (e.get("ts") or 0) > (e.get("ttl") or CALL_TTL):
            try:
                f.unlink()
            except OSError:
                continue  # another process removed it first, and audits it
            _audit("calls.unclosed", e.get("caller") or "?", f"{str(e.get('cmd') or '')[:200]} (no result within "
                   f"{e.get('ttl') or CALL_TTL:g} s)", "expired", session=str(sid)[:40])
            continue
        out.append(e)
    return sorted(out, key=lambda e: e.get("ts") or 0)


def _names_args(entry: dict, args: list) -> bool:
    """True when a call's text holds these arguments in order (as typed, or once the shell removed the quotes). A cut
    member's call holds anything: what was cut away may be the command."""
    if entry.get("cut") and entry.get("caller") != "main":
        return True
    args = [a for a in args if a]
    if not args:
        return False
    texts = [str(entry.get("cmd") or "")]
    try:
        texts.append(" ".join(shlex.split(texts[0])))
    except ValueError:
        pass
    for text in texts:
        at = 0
        for a in args:
            i = text.find(a, at)
            if i < 0:
                break
            at = i + len(a)
        else:
            return True
    return False


def caller(cwd: str | None = None, argv=None, now: float | None = None) -> dict:
    """Who runs this `dev` command -> {agent, session, how, caller, calls}. caller: main, a member's agent_id (the
    most restrictive when several match), unknown (no open call holds the command, or the session was only
    guessed by recency), unverified (Codex, until its spawned agents' hooks carry agent_id: S3), or human (a
    developer's shell with no agent session). `argv`: the command's arguments, default sys.argv[1:]."""
    agent, sid, how = resolve_session(cwd)
    out = {"agent": agent, "session": sid, "how": how, "caller": "unknown", "calls": []}
    if how == "shell":
        return dict(out, caller="human")
    if agent == "codex":
        return dict(out, caller="unverified")
    args = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
    hits = [c for c in open_calls(sid, now) if _names_args(c, args)]
    out["calls"] = [c.get("tool_use_id") for c in hits]
    members = sorted({str(c.get("caller")) for c in hits if c.get("caller") != "main"})
    if members:
        run = load_run(agent, sid) or {}
        out["caller"] = max(members, key=lambda m: (TOOL_LEVELS.index(lane_tools(run, m) or "read"),
                                                    ((run.get("agents") or {}).get(m) or {}).get("depth") or 0))
    elif hits and how != "recency":
        out["caller"] = "main"
    return out


def _as_lane(from_agent, cwd: str | None = None):
    """The lane a command acts as: DEVKIT_FROM_AGENT when the hooks set it, else the member the call ledger names.
    Variables only narrow (spec C4): an empty or missing one grants nothing. None for Morpheus."""
    if from_agent:
        return from_agent
    who = caller(cwd).get("caller")
    return who if who and who not in NOT_A_LANE else None


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


def fmt_about(m: float) -> str:
    """An estimate or a time left, as the dashboard shows it (fmtAbout): to 5 minutes, and to the minute under 10,
    halves up as Math.round does, so "~" claims no more precision than an estimate has. Measured times keep
    fmt_min."""
    m = max(0.0, float(m or 0))
    r = int(m + 0.5)
    return fmt_min(r if r < 10 else int(m / 5 + 0.5) * 5)


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
    """"design=30m; build=2h after design [backend]; docs" -> [(name, minutes or None, [steps it waits for],
    activity id or "")]. The [id] tag declares the step's kind of work (an id from dev crew raci)."""
    from . import raci
    items = plan if isinstance(plan, (list, tuple)) else str(plan or "").split(";")
    out = []
    for raw in items:
        raw, act = raci.split_activity(str(raw or "").strip())  # before the `after` split, or the tag lands in it
        if not raw:
            continue
        after = []
        m = re.match(r"^(.*?)\s+after\s+(.+)$", raw, re.I)
        if m:
            raw, after = m.group(1).strip(), [x.strip() for x in re.split(r"[,&]|\band\b", m.group(2)) if x.strip()]
        m = re.match(r"^(.*?)\s*[=@]\s*(\d[\dhm.\s]*)$", raw)
        if m and parse_minutes(m.group(2)):
            out.append((m.group(1).strip(), parse_minutes(m.group(2)), after, act))
        else:
            out.append((raw, None, after, act))
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
    together is validated (validate_plan) before anything changes: on error, `plan` is untouched. A declared kind of
    work ([id] in the spec) is stored on its step as explicit; an unknown id is refused."""
    new = [_new_step(it[0], it[1]) for it in items]
    full = list(plan or []) + new
    dup = _duplicate_name(full)
    if dup:
        raise StepError(f"two steps are named “{dup}”: give each step its own name, so `after` can name it")
    for s, it in zip(new, items):
        s["after"] = _resolve_after(full, s, it[2])
        if len(it) > 3 and it[3]:
            s["activity"], s["activity_from"] = _activity_id(it[3]), "explicit"
    validate_plan(full)
    return new


def _activity_id(ref: str) -> str:
    """A declared kind of work -> its RACI id ("backend", "Back-end services and APIs" -> "backend"), or StepError
    naming the ids."""
    from . import raci
    try:
        return raci.resolve_activity(ref)["id"]
    except ValueError as e:
        raise StepError(str(e)) from None


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
          now: float | None = None, mode: str = "", replace: bool = False, why: str = "") -> dict:
    """A new run for this session. A live run is replaced only with `replace` and a reason (spec C4). From a crew
    member: its lane's own plan inside the main run, never a new main run."""
    now = now or time.time()
    agent, sid = current_session(cwd)
    from_agent = _as_lane(from_agent, cwd)
    if from_agent:  # a subagent planning its own work: a lane of the main run, never a new main run
        if replace:
            _main_only(from_agent, "replaces a run")
        with _tx(agent, sid):
            run = normalize(load_run(agent, sid) or {})
            if not run.get("goal"):
                raise StepError("no progress run in this session: Morpheus starts one with dev progress start")
            lane = _lane(run, from_agent, now)
            lane.update(goal=goal, plan=[x[0] for x in parse_plan(plan)], done=0)
            run["updates"].append({"ts": round(now), "kind": "agent", "agent": from_agent, "text": f"plan: {goal}"})
            _save(run)
            return run
    _main_only(None, "replaces a run" if replace else "starts a run", cwd)
    if replace and not (why or "").strip():
        raise StepError("say why the live run is replaced: --why \"…\" (it is kept in the new run)")
    sf = SESSIONS / f"{agent}-{sid}.json"
    sess = core.read_json(sf, {}) or {}
    project = sess.get("project") or _project(cwd)
    est = parse_minutes(estimate)
    cal = calibration(project)
    items = parse_plan(plan)
    if not items and steps:
        items = [(f"step {i + 1}", None, [], "") for i in range(steps)]
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
    with _tx(agent, sid):
        live = load_run(agent, sid)  # a run that does not parse raises StateBusy here: it is never written over
        if live and live.get("goal") and live.get("state") != "done":
            if not replace:
                done_ = sum(1 for s in live.get("plan") or [] if s.get("state") in DONE)
                raise StepError(f"a run is live in this session: “{live['goal']}” ({done_} of "
                                f"{len(live.get('plan') or [])} steps). Add to it (dev progress add), end it (dev "
                                f"progress done), or replace it: dev progress start … --replace --why \"<reason>\"")
            run["replaced"] = {"goal": live["goal"], "started": live.get("started"), "ts": round(now),
                               "why": why.strip()[:300]}
            _audit("progress.replace", live["goal"][:120], why.strip()[:300], project=run.get("project") or "")
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


def _main_only(from_agent, what: str, cwd: str | None = None):
    """Only Morpheus (the main session) changes the main plan, sets the mode and records the developer's answers; a
    crew member asks him. The call ledger must name main (spec C4): a lane variable, a parent process or recency
    never does."""
    if from_agent:
        raise StepError(f"only Morpheus {what}: ask him with dev crew ask \"…\" (your own steps: dev progress add, "
                        "dev progress step)")
    who = caller(cwd)
    c = who.get("caller")
    if c in ("main", "human"):
        return
    if c == "unverified":
        _audit("caller.unverified", what, f"{who.get('agent')} session {str(who.get('session'))[:40]}: devkit cannot "
               f"verify who calls yet (spec C4, S3)", "allowed")
        return
    _audit("caller.refused", what, f"caller {c}; session found by {who.get('how')}", "deny",
           session=str(who.get("session"))[:40])
    if c == "unknown" or not c:
        raise StepError(f"only Morpheus {what}, and no open call of this session typed this command: type the "
                        f"command itself, not through a script or another shell, as a command of its own (spec C4)")
    raise StepError(f"only Morpheus {what}: this command runs inside a crew member's call ({c[:16]}). Ask him with "
                    f"dev crew ask \"…\"")


def _step_text(s: dict, label: str = "") -> str:
    """What a step is about, for the RACI: its name, its description, and the task in its label ("Tank: the API")."""
    task = label.split(":", 1)[1] if ":" in (label or "") else ""
    return " ".join(x for x in (s.get("name") or "", s.get("desc") or "", task) if x)


def _raci_assign(run: dict, s: dict, label: str, why: str, now: float, activity: str = "", files=None):
    """The crew's RACI decides who does a step: refuse a member who may not take its kind of work (unless `why`,
    which is recorded), and remember that kind of work for the approval at the end of the run. The kind of work
    rests on language-neutral anchors (raci.anchor): `activity` or the one declared on the step earlier, then the
    member's Responsible activity, then the files in scope (`files`, else the step's), then the words."""
    try:
        from . import crew, raci
        mem = crew._for_label(label)
        text = _step_text(s, label)
        explicit = activity or (s.get("activity") if s.get("activity_from") == "explicit" else "")
        files = files or s.get("scope")
        act, src = raci.anchor(text, member=mem["id"] if mem else None, files=files, activity=explicit or None)
        if act:
            s["activity"], s["activity_from"] = act["id"], src
        if not mem:
            return
        reason = raci.check_assignment(mem["id"], text, activity=explicit or None, files=files)
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
           from_agent: str | None = None, why: str = "", activity: str = "", allow: str = "") -> dict:
    """Hand a plan step to a subagent: the next subagent launched with this description (or a running lane with this
    label) owns it, and its time is that subagent's time. `scope`: the files it owns (comma-separated globs); the
    guard keeps that subagent's edits inside them. The crew's RACI must make the member Responsible for the step's
    kind of work; `why` overrides it, recorded. `activity`: the step's kind of work, an id from dev crew raci, which
    then decides whatever language the step is written in. `allow`: the tier-1 actions (push, pr) the member may
    take in this step, each only with the developer's evidence for it (their grant, or a verified decision; spec
    C6); the step's `allow` lists those that had it."""
    _main_only(from_agent, "assigns the plan's steps", cwd)
    activity = _activity_id(activity) if activity else ""
    wanted = [x.strip().lower() for x in (allow or "").split(",") if x.strip()]
    if any(x not in TIER1 for x in wanted):
        raise StepError(f"a member's step may be allowed {' and '.join(TIER1)} only; merge, release, publish, deploy "
                        f"and every other irreversible action stay the main session's (spec C6)")
    globs = [x.strip() for x in scope.split(",") if x.strip()] if scope else []
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise ValueError(f"no step “{which}” in the plan")
        _raci_assign(run, s, label, why, now, activity=activity, files=globs or None)
        owner = s.get("owner") or "main"
        if owner != "main" and s.get("state") != "done" and not _label_matches(label, (run.get("agents") or {}).get(owner)
                                                                                or {}):
            old = ((run.get("agents") or {}).get(owner) or {}).get("persona") or owner
            _log_change(run, s, s.get("state"), "pending", "main", f"reassigned from {old} to {label}", now)
            s.update(owner="main", state="pending")  # the other member's lane no longer owns it
        s["label"] = label
        if wanted:
            held = dict(s.get("allow_evidence") or {})
            for act in wanted:
                held[act] = held.get(act) or _action_evidence(agent, sid, run, act, "", now, any_target=True)
            s["allow_evidence"] = {k: v for k, v in held.items() if v}
            s["allow"] = [a for a in wanted if a in s["allow_evidence"]]
            missing = [a for a in wanted if a not in s["allow"]]
            _audit("crew.allow", s["name"], f"{label} may {', '.join(s['allow']) or 'take no irreversible action'}"
                   + (f"; no grant of the developer for {', '.join(missing)}" if missing else ""),
                   project=run.get("project") or "")
        if globs:
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
            lane = run["agents"][lane_id]
            if s.get("scope"):
                lane["scope"] = s["scope"]
            if lane.get("binding") == "unbound":  # Morpheus confirms a lane devkit had no launch record of
                lane["binding"] = "confirmed"
                _audit("crew.confirm", lane.get("persona") or label, f"Morpheus assigned “{s['name']}” to a lane "
                       f"without a launch record", project=run.get("project") or "", agent_id=lane_id[:16])
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
    for k, lane in (run.get("agents") or {}).items():
        if k == me or lane.get("state") != "running":
            continue
        if _in_globs(rel_path, lane.get("scope") or []):
            return k, lane
    return None, None


def _in_globs(rel_path: str, globs) -> bool:
    """A repo-relative path matches one of a scope's globs ("lib/parser/**", "docs/", "README.md")."""
    import fnmatch
    return any(fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(rel_path, g.rstrip("/") + "/**") or
               rel_path.startswith(g.rstrip("*").rstrip("/") + "/") or rel_path == g for g in globs)


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


def crew_ask(text: str, to: str | None = None, from_agent: str | None = None, cwd: str | None = None, now: float | None = None,
             kind: str = "message") -> dict:
    """A question or request between crew members (default: to Morpheus, the main session). It reaches the recipient
    with its next tool call (PostToolUse) or prompt. The audit trail keeps its kind and length, never its text (crew
    messages quote the developer's requests)."""
    now = now or time.time()
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise ValueError("no progress run in this session: Morpheus starts one with dev progress start")
        run = normalize(run)
        m = _post(run, from_agent or "main", _addr(run, to or ""), text, now)
        _save(run, touch_session=False)
    _audit("crew.message", f"{m['from_name']} → {m['to_name']}", "", project=run.get("project") or "", id=m["id"],
           type=kind, count=len(m["text"]))
    return m


def _next_id(items: list, prefix: str) -> str:
    """The id after the highest one kept: the lists keep their last 200 entries, so a count would repeat ids."""
    nums = [int(x["id"][len(prefix):]) for x in items if str(x.get("id") or "")[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def _post(run, frm: str, to: str, text: str, now: float, from_name: str = "") -> dict:
    """Add a message to the run (the caller saves it). -> the message."""
    msgs = run.setdefault("messages", [])
    m = {"id": _next_id(msgs, "M"), "ts": round(now), "from": frm, "to": to, "text": text.strip()[:1500],
         "delivered": False}
    m["from_name"], m["to_name"] = from_name or _name_of(run, frm), _name_of(run, to)
    msgs.append(m)
    run["messages"] = msgs[-200:]
    return m


def crew_answer(msg_id: str, text: str, from_agent: str | None = None, cwd: str | None = None, now: float | None = None) -> dict:
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        q = next((m for m in run.get("messages") or [] if m["id"].lower() == str(msg_id).lower()), None)
        if not q:
            raise ValueError(f"no message {msg_id}")
        q["answered"] = True
        _save(run, touch_session=False)
    a = crew_ask(text, to=q["from"] if q["from"] != "main" else "main", from_agent=from_agent, cwd=cwd, now=now,
                 kind="answer")
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        for m in run.get("messages") or []:
            if m["id"] == a["id"]:
                m["reply_to"], m["to"], m["to_name"] = q["id"], q["from"], q["from_name"]
        _save(run, touch_session=False)
    return a


def crew_inbox(from_agent: str | None = None, cwd: str | None = None, mark: bool = True) -> list:
    """Messages for the caller (the main session, or the subagent whose lane this is) not delivered yet."""
    from_agent = _as_lane(from_agent, cwd)
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
                "external-message", "push-destructive", "pr-write", "api-write")
TIER1 = ("push", "pr")  # a member's step grant covers these only (spec C6)
NEVER_PREAPPROVED = ("publish", "deploy")
PREAPPROVAL_HOURS = 8
PREAPPROVAL_USES = {"merge": 1, "release": 1}  # every other action: 10


def mode_of(run) -> str:
    return (run or {}).get("mode") or ((core.CFG.get("work") or {}).get("mode")) or "guided"


def _live_preapprovals(run, now: float) -> list:
    if mode_of(run) != "auto" or not run or run.get("state") == "done":
        return []
    return [p for p in run.get("preapprovals") or [] if p.get("uses_left", 0) > 0 and now <= p.get("expires", 0)
            and p.get("evidence")]


def unbacked_approvals(run, now: float | None = None) -> list:
    """The pre-approvals auto mode was given that have no grant of the developer behind them: they open nothing."""
    live = {f"{p['action']} {p['target']}" for p in _live_preapprovals(run, now or time.time())}
    return [x for x in (run or {}).get("approved") or [] if x not in live]


def approved_actions(run, now: float | None = None) -> list:
    """The irreversible actions auto mode may decide by itself: those with a live pre-approval the developer granted."""
    return sorted({p["action"] for p in _live_preapprovals(run, now or time.time())})


def _action_evidence(agent: str, sid: str, run: dict, action: str, target: str, now: float, any_target=False):
    """Use the developer's evidence for one action (a pre-approval's or a step grant's): their live grant of that
    action and target (any target with `any_target`), else a verified, unused decision naming the action. -> the
    evidence to keep with it, or None."""
    def granted(x):
        return x.get("channel") == "E2" and bool(x.get("expires"))
    got = None
    if not any_target:
        got = take_evidence(agent, sid, f"{action} {target}", now, pick=granted)
    else:
        for e in evidence(agent, sid, lambda x: granted(x) and str(x.get("covers") or "").partition(" ")[0] == action,
                          now):
            got = _consume(agent, sid, e, now)
            if got:
                break
    if got:
        return {k: got.get(k) for k in ("channel", "ts", "covers", "transcript_uuid", "excerpt")}
    d = next((x for x in run.get("decisions") or [] if x.get("verified") and x.get("by") == "developer"
              and x.get("irreversible") == action and not x.get("used")
              and (any_target or not x.get("target") or _norm(x["target"]) == _norm(target))), None)
    if d:
        d["used"] = now  # the caller saves the run
        return {"channel": "decision", "ts": d.get("decided"), "covers": d["id"],
                **{k: (d.get("evidence") or {}).get(k) for k in ("transcript_uuid", "excerpt")}}
    return None


def _preapproval_items(approve: str, cwd) -> list:
    """`push feat/*, release v26.11.4` -> [(action, target)]; refused: an unknown action, an item without its one
    target, publishing and deploying, and what the guard's target rules refuse (spec C6)."""
    items = [x.split() for x in (approve or "").split(",") if x.strip()]
    bad = [" ".join(x) for x in items if x[0].lower() not in IRREVERSIBLE]
    if bad:
        raise StepError(f"unknown action(s) {', '.join(bad)}; known: {', '.join(IRREVERSIBLE)}")
    out = []
    for x in items:
        act = x[0].lower()
        if len(x) != 2:
            raise StepError(f"name one target for each pre-approval: “{act} <target>”, such as push feat/*, "
                            f"release v26.11.4 or merge 142 (a pre-approval without one would cover everything)")
        if act in NEVER_PREAPPROVED:
            raise StepError(f"{act} is never pre-approved: the developer approves each one (grant {act} <target>)")
        from .guard import preapproval_problem
        problem = preapproval_problem(act, x[1], cwd or os.getcwd())
        if problem:
            raise StepError(problem)
        out.append((act, x[1]))
    return out


def set_mode(mode: str, approve: str = "", cwd: str | None = None, now: float | None = None) -> dict:
    """guided: decisions that are the developer's wait for the developer; auto: the crew goes on with the recommended
    option and records it. `approve`: irreversible actions pre-approved for auto mode, each with its target
    (`push feat/*, release v26.11.4`). A pre-approval opens something only with the developer's evidence (spec C5):
    their grant of that action and target, or a verified decision naming it; it has its uses (1 for merge and
    release, 10 for the others) and ends after 8 hours, with the run, or when the mode goes back to guided."""
    now = now or time.time()
    if mode not in ("guided", "auto"):
        raise StepError("mode is guided or auto")
    _main_only(None, "sets the mode", cwd)
    items = _preapproval_items(approve, cwd) if mode == "auto" else []
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        if not run.get("goal"):
            raise StepError("no progress run in this session: dev progress start \"<goal>\" --plan \"…\" --mode " + mode)
        keep = [p for p in _live_preapprovals(dict(run, mode="auto"), now) if (p["action"], p["target"]) in items]
        for act, target in items:
            if any(p["action"] == act and p["target"] == target for p in keep):
                continue
            ev = _action_evidence(agent, sid, run, act, target, now)
            if ev:
                keep.append({"id": f"P{len(run.get('preapprovals') or []) + len(keep) + 1}", "action": act,
                             "target": target, "repo": run.get("project") or "", "uses_left":
                             PREAPPROVAL_USES.get(act, 10), "expires": now + PREAPPROVAL_HOURS * 3600, "evidence": ev,
                             "ts": now})
        approved = [f"{a} {t}" for a, t in items]
        run["mode"], run["approved"], run["preapprovals"] = mode, approved, keep
        run.pop("approved_evidence", None)
        unbacked = unbacked_approvals(run, now)
        _audit("crew.mode", mode, "approved: " + (", ".join(approved) or "nothing")
               + (f"; no grant of the developer for {', '.join(unbacked)}: it opens nothing" if unbacked else ""),
               project=run.get("project") or "")
        run.setdefault("history", []).append({"ts": round(now), "step": "(mode)", "from": None, "to": mode,
                                              "by": "main", "why": "approved: " + (", ".join(approved) or "nothing")})
        _save(run)
    return run


def crew_decision(question: str, options=None, recommend: str = "", irreversible: str = "", from_agent: str | None = None,
                  cwd: str | None = None, now: float | None = None, target: str = "", approves: str = "") -> dict:
    """A decision point. -> the decision record. In auto mode a reversible decision is taken at once with the
    recommendation; in guided mode, or for an irreversible action not pre-approved, it waits for the developer: the
    asking member's lane waits and its step is blocked until Morpheus records the answer (crew_decide). `target`:
    what the irreversible action acts on (a branch, a tag, a PR number): a verified decision opens that action on
    that target once (spec C6)."""
    now = now or time.time()
    from_agent = _as_lane(from_agent, cwd)
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
        target = (target or "").strip()
        if target and (not act or len(target.split()) != 1):
            raise StepError("--target goes with --irreversible and names one thing: a branch, a tag, a PR number")
        decs = run.setdefault("decisions", [])
        d = {"id": _next_id(decs, "D"), "ts": round(now), "question": question.strip()[:800], "options": opts,
             "recommend": rec, "irreversible": act, "target": target, "from": from_agent or "main",
             "from_name": _name_of(run, from_agent or "main"), "state": "pending", "answer": "", "by": ""}
        if approves:  # the developer's answer approves these steps (approvals.verdicts), verified by C5 evidence
            from . import approvals
            try:
                d["approves"] = approvals.decision_steps(run, approves)
            except approvals.ApprovalError as e:
                raise StepError(str(e))
        auto = mode_of(run) == "auto" and (not act or act in approved_actions(run, now))
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


def crew_decide(decision_id: str, answer: str, cwd: str | None = None, now: float | None = None) -> dict:
    """Record the developer's answer (Morpheus runs it after asking): the waiting member's step runs again, and a
    message with the answer waits for that member (delivered when Morpheus resumes it). Who decided is computed,
    never typed (spec C5): the developer, verified, when an unused piece of their evidence (E1, E2) given after the
    decision was shown covers it; otherwise Morpheus, unverified, and an irreversible decision stays pending."""
    now = now or time.time()
    _main_only(None, "records the developer's answers", cwd)
    agent, sid = current_session(cwd)
    answer = answer.strip()[:800]

    def find(run):
        d = _decision(run, decision_id)
        if not d:
            raise StepError(f"no decision {decision_id}")
        if d["state"] == "decided" and d.get("by") != "auto" and d.get("verified") is not False:
            raise StepError(f"{d['id']} was already decided by {d['by']}: “{d['answer']}”")
        return d
    run0 = normalize(load_run(agent, sid) or {})
    ev = _decision_evidence(agent, sid, run0, find(run0), answer, now)  # the transcript is read outside the lock
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        d = find(run)
        if d["state"] == "decided" and d.get("by") != "auto" and not ev:
            raise StepError(f"{d['id']} was already recorded by Morpheus without the developer's evidence: “"
                            f"{d['answer']}”. Only their own answer replaces it: ask with the question tool, "
                            f"exactly “{ask_text(d)}”, or they type: approve {d['id']}: <option>")
        if not ev and d.get("irreversible"):
            d.update(proposed=answer, by="morpheus", verified=False, proposed_ts=round(now))
            _save(run, touch_session=False)
            _audit("crew.decided", d["id"], answer[:300], "blocked", project=run.get("project") or "",
                   by="morpheus", verified=False, irreversible=d["irreversible"])
            raise StepError(f"{d['id']} leads to an irreversible action ({d['irreversible']}), and devkit has no "
                            f"answer of the developer to it after it was shown: it stays pending. Ask with the "
                            f"question tool, exactly “{ask_text(d)}”"
                            + (f" with the options {' | '.join(d['options'])}" if d.get("options") else "")
                            + f", or they type a line of its own: approve {d['id']}" + (": <option>" if d.get(
                                "options") else "") + ". Then run this again.")
        prev = d.get("answer")
        d.update(state="decided", answer=answer, by="developer" if ev else "morpheus", verified=bool(ev),
                 decided=round(now))
        d.pop("proposed", None)
        d.pop("proposed_ts", None)
        if ev:
            d["evidence"] = {k: ev.get(k) for k in ("channel", "ts", "covers", "transcript_uuid", "excerpt")}
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
    _audit("crew.decided", d["id"], d["answer"][:300], project=run.get("project") or "", by=d["by"],
           overrides=d.get("overrides") or "", verified=d["verified"], channel=(ev or {}).get("channel") or "",
           transcript_uuid=(ev or {}).get("transcript_uuid") or "", session=f"{agent}:{sid}")
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


def _told(d: dict) -> bool:
    return bool(d.get("told") or d.get("surfaced"))  # `surfaced`: runs written before spec C5


def decisions_context(agent: str, sid: str) -> str:
    """For the main session: decisions waiting for the developer, newest first, once per decision."""
    run = load_run(agent, sid)
    if not run or not any(not _told(d) for d in pending_decisions(run)):
        return ""  # the usual case, without the lock
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        new = [d for d in pending_decisions(run) if not _told(d)]
        if not new:
            return ""
        for d in new:
            d["told"] = round(time.time())  # delivered to Morpheus; `shown` is when the developer saw it
            d.pop("surfaced", None)
        _save(run, touch_session=False)
    lines = ["devkit · decisions waiting for the developer (" + mode_of(run) + " mode). Ask with your question tool "
             "(AskUserQuestion), its question exactly as quoted below and the options as listed, or show the "
             "decision in your final message and let the developer type `approve D<n>[: <option>]`. Then record "
             "the answer with `dev crew decide <id> \"<answer>\"` (devkit records who decided from that evidence) "
             "and resume the member who asked (SendMessage / send_input):"]
    for d in new:
        opts = (" Options: " + " | ".join(d["options"])) if d["options"] else ""
        lines.append(f"- {d['id']} from {d['from_name']}: “{ask_text(d)}”{opts}"
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


# --------------------------------------------------------------------------- developer evidence (spec D4, C5)
#
# A decision, or one irreversible action on one target, is the developer's only through two channels the hooks see:
# E1, the question tool's answer to the recorded "D<n> · <question>", and E2, a grant line the developer types. Each
# entry lives in the session record (`human`), covers one decision or one action and target, counts only after the
# decision was shown, is used once and expires. It is confirmed at use time against the transcript, where Claude
# Code marks the developer's own prompts (origin human): a hook event typed through a shell, a `-p` prompt, a task
# notification or a relayed comment never counts. Agents' text, tool results and crew messages never reach it.

GRANT_TTL = 30 * 60  # seconds a `grant <action> <target>` stays usable
HUMAN_KEEP = 50  # evidence entries kept per session
HUMAN_IDS_KEEP = 2000  # ids of the prompts and answers evidence came from, kept per session (B3: each counts once)
QUESTION_TOOLS = ("AskUserQuestion",)  # Codex's request_user_input joins when its payload is known (spec F0, S3)
GRANT_CLASSES = ("push", "pr", "push-destructive", "pr-write", "merge", "release", "publish", "deploy", "api-write",
                 "external-message", "delete-data", "migrate-production", "spend")  # C6's classes and IRREVERSIBLE
HEADLESS_SURFACES = ("SDK", "claude -p", "codex exec")  # nobody at the keyboard: DEVKIT_PREAPPROVE only
TRANSCRIPT_DAYS = 30  # Claude Code's default cleanupPeriodDays: older evidence may have lost its transcript
_APPROVE_LINE = re.compile(r"(?i)^approve\s+(D\d+)\s*(?::\s*(\S.*?))?\s*$")
_GRANT_LINE = re.compile(r"(?i)^grant\s+([a-z][a-z-]*)\s+(\S+)\s*$")
_PLAIN_YES = re.compile(r"(?i)^\s*yes\s*[.!]?\s*$")
_RELAYED = re.compile(r"^\s*(?:\[[^\]\n]{0,120}\bsent to (?:Claude|Codex)\b[^\]\n]*\]|\[SYSTEM NOTIFICATION|"
                      r"<task-notification|<system-reminder|<agent-message|<command-name>|<local-command)",
                      re.IGNORECASE | re.MULTILINE)
_DECISION_REF = re.compile(r"\bD(\d+)\b")
_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)


def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def ask_text(d: dict) -> str:
    """The question to put to the developer with the question tool, exactly: only an answer to this text is E1."""
    return f"{d['id']} · {d['question']}"


def option_of(d: dict, text):
    """The decision's option an answer names (the label, with or without the question tool's "(Recommended)", or
    the option's letter: "B" for "B: reject"), else None."""
    t = _norm(_RECOMMENDED.sub("", str(text or "")))
    for o in d.get("options") or []:
        if t and (t == _norm(o) or (":" in o and t == _norm(o.split(":", 1)[0]))):
            return o
    return None


def headless(sess: dict) -> bool:
    """A session with nobody at the keyboard (`claude -p`, the SDK, `codex exec`, devkit's own headless calls)."""
    return str((sess or {}).get("surface") or "") in HEADLESS_SURFACES or os.environ.get("DEVKIT_HEADLESS") == "1"


def preapproved(cls: str, target: str) -> bool:
    """A headless session's pre-approval: `DEVKIT_PREAPPROVE="release v26.11.4, push feat/*"` in the hook
    environment, set by whoever launched the session; `dev` commands never read it (spec C5)."""
    import fnmatch
    for item in (os.environ.get("DEVKIT_PREAPPROVE") or "").split(","):
        parts = item.split()
        if len(parts) == 2 and parts[0].lower() == str(cls).lower() and fnmatch.fnmatchcase(str(target), parts[1]):
            return True
    return False


def _own_lines(text: str) -> list:
    """The lines the developer typed as their own: outside fenced code blocks, quotes and indented code."""
    out, fence = [], False
    for ln in str(text or "").splitlines():
        s = ln.strip()
        if s.startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence or s.startswith(">") or ln.startswith(("    ", "\t")):
            continue
        out.append(s)
    return out


def _decision(run, did):
    return next((x for x in (run or {}).get("decisions") or [] if x["id"].lower() == str(did).lower()), None)


def _seen_ids(sess: dict) -> set:
    """The prompt and question tool ids this session recorded evidence from: the kept entries' and the longer list."""
    return set((sess or {}).get("human_ids") or []) | {str(h.get("transcript_uuid") or "")
                                                       for h in (sess or {}).get("human") or [] if isinstance(h, dict)}


def _refuse_replay(agent: str, sid: str, uid: str, what: str):
    _audit("crew.evidence-refused", what[:200], f"replayed: the session recorded the evidence of {uid[:80]} already, "
           f"and a prompt or an answer counts once (B3)", "refused", session=f"{agent}:{sid}"[:60])


def _keep_human(agent: str, sid: str, new: list, extra=None) -> list:
    """Keep the developer's new evidence entries (and `extra` fields) on the session record -> the entries kept. A
    prompt or a question tool answer is evidence once (B3): an entry whose id (prompt_id, tool_use_id) the session
    recorded before is dropped and audited as a replay, under the session's lock, so two copies of one event keep
    one. The ids outlive the entries (HUMAN_IDS_KEEP), so an old grant's prompt cannot come back either."""
    kept, replayed = [], []

    def fn(s):
        seen = _seen_ids(s)
        kept[:], replayed[:] = [], []
        for e in new:
            uid = str(e.get("transcript_uuid") or "")
            (replayed if uid and uid in seen else kept).append(e)
        s["human"] = ((s.get("human") or []) + kept)[-HUMAN_KEEP:]
        ids = [str(e["transcript_uuid"]) for e in kept if e.get("transcript_uuid")]
        s["human_ids"] = list(dict.fromkeys((s.get("human_ids") or []) + ids))[-HUMAN_IDS_KEEP:]
        s.update(extra or {})
    state.update(session_path(agent, sid), fn, create=False)
    for e in replayed:
        _refuse_replay(agent, sid, str(e.get("transcript_uuid")), f"{e.get('channel')} {e.get('covers')}")
    for e in kept:
        _audit("crew.evidence", e["covers"], e["excerpt"], channel=e["channel"], transcript_uuid=e["transcript_uuid"],
               session=f"{agent}:{sid}", answer=e.get("answer") or "")
    return kept


_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def harness_transcript(agent: str, sid: str) -> str:
    """The session's own transcript, where the harness writes it (spec C5, release blocker B1): Claude Code's
    ~/.claude/projects/<project folder>/<session id>.jsonl, found from the session id alone, never from a path a
    hook event or a caller names. '' when there is none or more than one, or the id is not a plain session id."""
    if agent != "claude" or not _SESSION_ID.fullmatch(str(sid or "")):
        return ""
    try:
        hits = sorted((core.CLAUDE_HOME / "projects").glob(f"*/{sid}.jsonl"))
    except OSError:
        return ""
    return str(hits[0]) if len(hits) == 1 else ""


def _foreign_transcript(agent: str, sid: str, named) -> bool:
    """A hook event that names a transcript other than the session's own (a forged event or path, B1): its
    evidence is refused and recorded. An event that names none, or the session's own, is fine."""
    if agent != "claude" or not named:
        return False
    named = str(named)
    own = harness_transcript(agent, sid)
    if own:
        return os.path.realpath(named) != os.path.realpath(own)
    p = Path(named)  # not written yet (the first prompt): it must be where the harness will write it
    return not (p.name == f"{sid}.jsonl" and p.parent.parent == core.CLAUDE_HOME / "projects")


def _entry(agent, channel, covers, excerpt, uuid, transcript, run, now, **more) -> dict:
    e = {"ts": now, "agent": agent, "channel": channel, "covers": covers, "excerpt": str(excerpt)[:200],
         "transcript_uuid": str(uuid or ""), "transcript": str(transcript or ""),
         "run": (run or {}).get("started"), "used": None, "expires": None, "repo": (run or {}).get("project") or ""}
    e.update(more)
    return e


def _refuse_forged(agent: str, sid: str, named, what: str):
    _audit("crew.evidence-refused", what[:200], f"the event names the transcript {core.tilde(str(named))[:200]}, "
           f"not the session's own: its evidence counts for nothing (B1)", "refused", session=f"{agent}:{sid}"[:60])


def on_human_prompt(agent: str, data: dict, now: float | None = None) -> list:
    """UserPromptSubmit: the E2 lines of the developer's prompt -> the entries recorded. A line of its own, outside
    code blocks and quotes: `approve D<n>[: <option>]` or `grant <action> <target>`; a plain "yes" covers the one
    decision the last final message showed. Relayed comments, notifications and headless sessions record nothing."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    sess = state.read(session_path(agent, sid)) if sid else None
    text = str(data.get("prompt") or "")
    if sess is None or headless(sess) or _RELAYED.search(text):
        return []
    if _foreign_transcript(agent, sid, data.get("transcript_path")):
        _refuse_forged(agent, sid, data.get("transcript_path"), "a prompt")
        return []
    run = load_run(agent, sid)
    live = run if run and run.get("goal") and run.get("state") != "done" else None
    uuid, tp, new = data.get("prompt_id") or "", harness_transcript(agent, sid), []
    if uuid and str(uuid) in _seen_ids(sess):  # the same prompt again (B3): `dev hook prompt` fed its event back
        _refuse_replay(agent, sid, str(uuid), "a prompt")
        return []
    repo = sess.get("project") or (live or {}).get("project") or ""  # a grant holds in this repository only
    for ln in _own_lines(text):
        mt = _APPROVE_LINE.match(ln)
        if mt and live:
            new.append(_entry(agent, "E2", mt.group(1).upper(), ln, uuid, tp, live, now,
                              answer=(mt.group(2) or "").strip()[:300], repo=repo, session=sid))
            continue
        mt = _GRANT_LINE.match(ln)
        if mt and mt.group(1).lower() in GRANT_CLASSES:
            new.append(_entry(agent, "E2", f"{mt.group(1).lower()} {mt.group(2)}", ln, uuid, tp, live, now,
                              expires=now + GRANT_TTL, repo=repo, session=sid))
    if not new and live and _PLAIN_YES.match(text):
        ids = (sess.get("last_shown") or {}).get("ids") or []
        d = _decision(live, ids[0]) if len(ids) == 1 else None
        if d and d.get("state") == "pending" and not d.get("irreversible"):
            new.append(_entry(agent, "E2", d["id"], text.strip(), uuid, tp, live, now, answer="", plain=True,
                              repo=repo, session=sid))
    if new or (tp and sess.get("transcript") != tp):
        new = _keep_human(agent, sid, new, {"transcript": tp} if tp else None)
    return new


def on_question(agent: str, data: dict, now: float | None = None) -> list:
    """PostToolUse of the question tool in the main session: the decisions it asked are shown, and an answer to the
    exact `ask_text` of a decision is E1 evidence for it (one of its options; free text counts only when the
    decision is reversible). -> the entries recorded."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    if data.get("agent_id") or data.get("tool_name") not in QUESTION_TOOLS or not sid:
        return []
    sess = state.read(session_path(agent, sid))
    run = load_run(agent, sid)
    if sess is None or headless(sess) or not run or not run.get("decisions"):
        return []
    if _foreign_transcript(agent, sid, data.get("transcript_path")):
        _refuse_forged(agent, sid, data.get("transcript_path"), "a question tool answer")
        return []
    if data.get("tool_use_id") and str(data["tool_use_id"]) in _seen_ids(sess):  # the same answer again (B3)
        _refuse_replay(agent, sid, str(data["tool_use_id"]), "a question tool answer")
        return []
    qs = [q for q in (data.get("tool_input") or {}).get("questions") or [] if isinstance(q, dict)]
    resp = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}
    answers = resp.get("answers") if isinstance(resp.get("answers"), dict) else {}
    pending = {d["id"] for d in pending_decisions(run)}
    exact = {_norm(ask_text(d)): d for d in run["decisions"]}
    shown, new = set(), []
    for q in qs:
        qt = str(q.get("question") or "")
        shown.update(f"D{n}" for n in _DECISION_REF.findall(qt) if f"D{n}" in pending)
        d, label = exact.get(_norm(qt)), answers.get(qt)
        if not d or not isinstance(label, str) or not label.strip():
            continue
        opt = option_of(d, label)
        new.append(_entry(agent, "E1", d["id"], f"{qt[:120]} = {label[:70]}", data.get("tool_use_id"),
                          harness_transcript(agent, sid), run, now, answer=(opt or label.strip())[:300],
                          free=opt is None, question=qt[:800], label=label[:300], session=sid))
    if shown:
        _mark_shown(agent, sid, shown, now)
    if new:
        new = _keep_human(agent, sid, new)
    return new


def _mark_shown(agent: str, sid: str, ids, now: float):
    with _tx(agent, sid):
        run = load_run(agent, sid)
        hit = [d for d in (run or {}).get("decisions") or [] if d["id"] in ids and not d.get("shown")]
        for d in hit:
            d["shown"] = now
        if hit:
            _save(run, touch_session=False)


def on_final_message(agent: str, sid: str, text: str, now: float | None = None):
    """Stop: the developer saw the final message, so each pending decision it names is shown (from now on evidence
    for it counts), and a plain "yes" next covers it when it was the only one."""
    now = now or time.time()
    f = session_path(agent, sid)
    if not f.exists():
        return
    ids = {f"D{n}" for n in _DECISION_REF.findall(str(text or ""))}
    run = load_run(agent, sid)
    named = sorted((d["id"] for d in pending_decisions(run) if d["id"] in ids), key=lambda x: int(x[1:]))
    if named:
        _mark_shown(agent, sid, set(named), now)
    elif not ((state.read(f) or {}).get("last_shown") or {}).get("ids"):
        return  # nothing shown now or before: no write
    try:
        state.update(f, lambda s: s.update(last_shown={"ts": now, "ids": named}), create=False)
    except state.StateBusy:
        pass  # audited by state: a plain "yes" next covers nothing, which is the safe side


def _lines_back(path, key: bytes, block: int = 1 << 20):
    """The lines of a file that contain `key`, last first, reading backwards in blocks."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        pos, rest = fh.tell(), b""
        while pos > 0:
            n = min(block, pos)
            pos -= n
            fh.seek(pos)
            parts = (fh.read(n) + rest).split(b"\n")
            rest = parts[0]
            for ln in reversed(parts[1:]):
                if key in ln:
                    yield ln
        if key in rest:
            yield rest


def _text_of(msg) -> str:
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text") or "" for b in c or [] if isinstance(b, dict) and b.get("type") == "text")


def confirmed(e: dict, sid: str = "") -> bool:
    """Does the session's own transcript (harness_transcript, from the session id: `sid` or the entry's) show this
    entry as the developer's own act? E2: a prompt record with its prompt id, origin human, outside any subagent,
    holding the line. E1: the question tool's result for its tool_use_id with that answer. An entry that carries
    another transcript's path is refused (B1). Codex transcripts are not read yet (spec F0, S3), so a Codex entry has
    nothing but its hook event behind it and is never confirmed (The Keymaker): it opens nothing."""
    if e.get("agent") == "codex":
        return False
    uuid, path = str(e.get("transcript_uuid") or ""), harness_transcript("claude", sid or e.get("session") or "")
    if not uuid or not path:
        return False
    if e.get("transcript") and os.path.realpath(str(e["transcript"])) != os.path.realpath(path):
        return False
    try:
        for ln in _lines_back(path, json.dumps(uuid).encode()):
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("type") != "user" or r.get("isSidechain"):
                continue
            if e.get("channel") == "E2":
                if r.get("promptId") == uuid and (r.get("origin") or {}).get("kind") == "human" and \
                        (not e.get("excerpt") or _norm(e["excerpt"]) in _norm(_text_of(r.get("message")))):
                    return True
            elif e.get("channel") == "E1":
                blocks = (r.get("message") or {}).get("content")
                res = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
                if any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == uuid
                       for b in blocks or []) and \
                        (not e.get("question") or (res.get("answers") or {}).get(e["question"]) == e.get("label")):
                    return True
    except OSError:
        return False
    return False


def _live(e: dict, now: float, run=None) -> bool:
    if e.get("used") is not None or (e.get("expires") and now > e["expires"]):
        return False
    return run is None or (e.get("run") == run.get("started") and run.get("state") != "done")


def evidence(agent: str, sid: str, pick, now: float | None = None, run=None) -> list:
    """The session's live, unused entries that `pick(entry)` accepts and the transcript confirms, oldest first."""
    now = now or time.time()
    sess = state.read(session_path(agent, sid)) or {}
    if headless(sess):
        return []
    return [e for e in sess.get("human") or [] if _live(e, now, run) and pick(e) and confirmed(e, sid)]


def _consume(agent: str, sid: str, e: dict, now: float) -> dict | None:
    """Mark one entry used, under the session's lock; None when another process used it first."""
    out = {}

    def fn(s):
        for x in s.get("human") or []:
            if x.get("ts") == e["ts"] and x.get("covers") == e["covers"] and x.get("used") is None:
                x["used"] = now
                out.update(x)
                return None
        return state.SKIP
    state.update(session_path(agent, sid), fn, create=False)
    return out or None


def take_evidence(agent: str, sid: str, covers: str, now: float | None = None, run=None, pick=None):
    """Use one live entry that covers `covers` ("D3" or "release v26.11.4") -> the entry, marked used, or None.
    Raises state.StateBusy when the session's lock is busy (the gate denies a member and fails open for main)."""
    now = now or time.time()
    for e in evidence(agent, sid, lambda x: _norm(x.get("covers")) == _norm(covers) and (pick or bool)(x), now, run):
        got = _consume(agent, sid, e, now)
        if got:
            return got
    return None


def _target_covers(action: str, pattern: str, target: str) -> bool:
    """Does a grant's target pattern cover this target? The guard's rule (spec C6: push globs never match the
    default branch, tags match with or without v)."""
    from .guard import target_matches
    return bool(target_matches(action, pattern, target))


def _same_repo(have, want) -> bool:
    """A grant holds in its own repository only: both known and the same (a missing one grants nothing, P2)."""
    return bool(have) and bool(want) and str(have).lower() == str(want).lower()


def _opened(id_, source, uses_left, expires) -> dict:
    return {"id": id_, "source": source, "uses_left": uses_left, "expires": expires}


def use_grant(agent: str, sid: str, *, action: str, target: str, repo: str = "", lane: str | None = None,
              consume: bool = True, now: float | None = None):
    """What opens one irreversible action (spec C6, The Keymaker's gate) -> {id, source, uses_left, expires}, or
    None. `lane` None is the main session: a live E2 grant of that action and target, a verified decision naming
    them, or in auto mode a pre-approval with uses left (never for publish or deploy); a headless session only by
    DEVKIT_PREAPPROVE in the hook environment. A member's lane: only a grant of the running step it owns, for push
    and PR create. `consume` False peeks, so the gate checks every action of a command before it uses any; True
    takes one use under the session's lock. state.StateBusy and every other error reach the caller: the gate
    denies a member and fails open for the main session."""
    now = now or time.time()
    action = str(action or "").lower()
    sess = state.read(session_path(agent, sid)) or {}
    run = load_run(agent, sid)
    live = run if run and run.get("goal") and run.get("state") != "done" else None
    if lane:
        if action not in TIER1 or not live:
            return None
        s = next((x for x in live.get("plan") or [] if x.get("owner") == lane and x.get("state") == "running"
                  and action in (x.get("allow") or [])), None)
        return _opened(f"step:{s['name']}", "step", None, None) if s else None
    if headless(sess):
        return _opened("DEVKIT_PREAPPROVE", "preapproval", None, None) if preapproved(action, target) else None

    def grants(e):
        cls, _, pattern = str(e.get("covers") or "").partition(" ")
        return e.get("channel") == "E2" and bool(e.get("expires")) and cls == action and \
            _same_repo(e.get("repo"), repo) and _target_covers(action, pattern, target)
    for e in evidence(agent, sid, grants, now):
        eid = f"E2:{e.get('transcript_uuid') or round(e['ts'])}:{e['covers']}"
        if not consume:
            return _opened(eid, "E2", 1, e["expires"])
        got = _consume(agent, sid, e, now)
        if got:
            _audit("crew.grant-used", f"{action} {target}", f"E2 grant “{got['covers']}”", channel="E2",
                   transcript_uuid=got.get("transcript_uuid") or "", session=f"{agent}:{sid}")
            return _opened(eid, "E2", 0, got["expires"])
    if not live or not _same_repo(live.get("project"), repo):
        return None

    def decision(r):
        return next((d for d in r.get("decisions") or [] if d.get("state") == "decided" and d.get("verified")
                     and d.get("by") == "developer" and d.get("irreversible") == action and d.get("target")
                     and not d.get("used") and _target_covers(action, d["target"], target)), None)

    def preapproval(r):
        if action in NEVER_PREAPPROVED:
            return None
        return next((p for p in _live_preapprovals(r, now) if p["action"] == action and _same_repo(p.get("repo"), repo)
                     and _target_covers(action, p["target"], target)), None)
    for source, find in (("decision", decision), ("preapproval", preapproval)):
        hit = find(live)
        if not hit:
            continue
        if not consume:
            return _opened(hit["id"], source, hit.get("uses_left", 1), hit.get("expires"))
        with _tx(agent, sid):
            r = load_run(agent, sid) or {}
            hit = find(r)
            if not hit:
                continue
            if source == "decision":
                hit["used"] = now
            else:
                hit["uses_left"] -= 1
            _save(r, touch_session=False)
        _audit("crew.grant-used", f"{action} {target}", f"{source} {hit['id']}", project=r.get("project") or "",
               session=f"{agent}:{sid}")
        return _opened(hit["id"], source, hit.get("uses_left", 0), hit.get("expires"))
    return None


def _when(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def evidence_lines(entries: list) -> str:
    """The echo of what the developer just granted or approved, for the next context."""
    out = []
    for e in entries:
        how = "by your prompt" if e["channel"] == "E2" else "by your answer to the question tool"
        if e.get("expires"):
            out.append(f"granted: {e['covers']} {how} at {_when(e['ts'])} (one use, until {_when(e['expires'])})")
        else:
            out.append(f"approved: {e['covers']}{': ' + e['answer'] if e.get('answer') else ''} {how} at "
                       f"{_when(e['ts'])} (record it with dev crew decide {e['covers']} \"<the answer>\")")
    return ("devkit · " + "; ".join(out)) if out else ""


def _live_grants(agent: str, sid: str, now: float) -> list:
    sess = state.read(session_path(agent, sid)) or {}
    return [e for e in sess.get("human") or [] if e.get("expires") and _live(e, now)]


def _decision_evidence(agent: str, sid: str, run: dict, d: dict, answer: str, now: float):
    """The entry that makes `answer` the developer's decision, used now; None without one. Raises StepError when
    the developer chose something else, so Morpheus records their answer and not his own."""
    def fits(e):
        if d.get("shown") is None or e["ts"] < d["shown"]:
            return False
        if (e.get("plain") or e.get("free")) and d.get("irreversible"):
            return False
        return True
    found = evidence(agent, sid, lambda e: _norm(e.get("covers")) == _norm(d["id"]) and fits(e), now, run)
    if not found:
        return None
    e = found[-1]  # the developer's latest word on it
    said = e.get("answer") or (d.get("recommend") if e["channel"] == "E2" and d.get("options") else "")
    if said:
        want = option_of(d, said) or said
        if not (option_of(d, answer) == want or _norm(answer) == _norm(want)):
            raise StepError(f"{d['id']}: the developer chose “{want}” ({'question tool' if e['channel'] == 'E1' else 'prompt'}"
                            f" at {_when(e['ts'])}); record that: dev crew decide {d['id']} \"{want}\"")
    return _consume(agent, sid, e, now)


def evidence_check(rec: dict, path, line):
    """`dev audit verify` (spec C5, K1): each recorded piece of developer evidence must point to a real message of
    the developer in the session's transcript. -> the problem, or None."""
    if rec.get("action") != "crew.evidence":
        return None
    ev, ref = rec.get("evidence") or {}, rec.get("ref") or {}
    agent, _, sid = str(ref.get("session") or "").partition(":")
    if ev.get("channel") not in ("E1", "E2") or agent != "claude":
        return None
    uuid = str(ev.get("transcript_uuid") or "")
    tp = harness_transcript(agent, sid)  # the harness's own file, never a path a record or an event names (B1)
    if not tp or not Path(tp).exists():
        old = time.time() - float(rec.get("ts") or 0) > TRANSCRIPT_DAYS * 86400
        return None if old else (f"evidence {ev['channel']} {uuid or '-'} for {rec.get('target')}: the session's "
                                 f"transcript is gone, so no message of the developer backs it")
    e = {"agent": agent, "channel": ev["channel"], "transcript_uuid": uuid, "transcript": str(tp)}
    if not confirmed(e, sid):
        return (f"evidence {ev['channel']} {uuid or '-'} for {rec.get('target')}: no message of the developer in "
                f"{core.tilde(tp)} backs it")
    return None


def _register_checks():
    from . import audit
    if evidence_check not in audit.CHECKS:
        audit.CHECKS.append(evidence_check)


def begin(which, cwd: str | None = None, now: float | None = None, from_agent: str | None = None) -> dict:
    """Mark a step as started now (parallel work the main agent does itself)."""
    _main_only(from_agent, "starts the plan's steps", cwd)
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
    from_agent = _as_lane(from_agent, cwd)
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
            for name, *_ in items:
                if name.lower() in (x.lower() for x in plan):
                    raise StepError(f"step “{name}” is already in your lane's plan")
                plan.append(name)
                run["updates"].append({"ts": round(now), "kind": "agent", "agent": from_agent, "text": f"added: {name}"})
            _save(run)
            return run
        for name, *_ in items:
            if _find_step(run, name) and _find_step(run, name)["name"].lower() == name.lower():
                raise StepError(f"step “{name}” is already in the plan")
        for s in _build_steps(items, run["plan"]):  # an unknown dependency or a cycle is refused: the plan stays as it is
            s["added"] = round(now, 1)  # its age, and whether it is the developer's latest request (dispatch.order)
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
    _main_only(from_agent, "skips the plan's steps" if skip else "reopens the plan's steps", cwd)
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
            if old not in ("done", "skipped", "blocked", "unverified"):
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
    from_agent = _as_lane(from_agent, cwd)
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
    """The crew member who did a step: its lane's member, or Morpheus for the main session's own steps
    (approvals.doer)."""
    from . import approvals
    return approvals.doer(run, s)


def approvals_owed(run: dict) -> list:
    """[(approver id, [step names])] still to approve the run's finished work (approvals.owed: an explicit verdict
    after the work, never the doer's). [] when the roster cannot be read (the check never blocks on a broken
    roster)."""
    try:
        from . import approvals
        return approvals.owed(run, lambda s: member_of_step(run, s))
    except Exception:
        return []


def _approve(run: dict, by: str, approves: str, check: str, now: float):
    """`dev progress step --approves "<steps>"`: `by`'s verdict on the named steps, from the step's Check (spec F6,
    R2-3). ApprovalError becomes StepError."""
    from . import approvals
    try:
        approvals.record(run, by, approves, approvals.verdict_of(check), now, check=check,
                         doer_of=lambda s: member_of_step(run, s))
    except approvals.ApprovalError as e:
        raise StepError(str(e))


def step(text: str, nxt: str = "", cwd: str | None = None, kind: str = "step", check: str = "", which=None,
         from_agent: str | None = None, now: float | None = None, force: bool = False, why: str = "",
         approves: str = "") -> dict:
    """Record a step of the run: Morpheus's closes a plan step, a member's goes to its lane; `approves` names the
    finished steps the Check approves or sends back (approvals.record). kind "done" ends the run, refused while an
    approval is owed or a step is flagged recheck, unless `why` (approvals.end_check)."""
    now = now or time.time()
    agent, sid = current_session(cwd)
    from_agent = _as_lane(from_agent, cwd)
    if kind == "done" and not from_agent:
        _main_only(None, "ends the run", cwd)
    if not load_run(agent, sid):  # no run yet: Morpheus's step starts one (start publishes, so never in the lock)
        if from_agent:
            raise StepError("no progress run in this session, so this step has no lane to go to: Morpheus starts "
                            "the run (dev progress start); until then, report it in your message")
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
            if approves:
                from . import crew
                mem = crew._for_lane(lane)
                if not mem:
                    raise StepError("this lane names no crew member, so it approves nothing: report the verdict, and "
                                    "the step's approver records it")
                _approve(run, mem["id"], approves, check, now)
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
            if approves:
                _approve(run, "morpheus", approves, check, now)
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
            from . import approvals
            try:
                over = approvals.end_check(run, why, now, lambda s_: member_of_step(run, s_))
            except approvals.ApprovalError as e:
                raise StepError(str(e))
            if over:
                run.setdefault("raci_overrides", []).append(over)
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


def crew_summary(run: dict, limit: int = 4, now: float | None = None) -> str:
    """Who in the crew works on what in this run, running first, then the slots in use and why the limit is lower
    (spec R7): "Tank ▸ the API · Kid ✓ installer · 1 of 4 slots in use". '' without crew members (the agent's own
    helpers are not the crew)."""
    from . import crew
    lanes = []
    for aid, lane in (run.get("agents") or {}).items():
        m = crew._for_label(lane.get("persona") or "") if lane.get("persona") else None
        if not m:
            continue
        step_ = next((s["name"] for s in run.get("plan") or [] if s.get("owner") == aid), "")
        what = step_ or (lane.get("label") or "").split(":", 1)[-1].strip()
        mark = {"running": "▸", "done": "✓", "lost": "✗", "stopped": "✗"}.get(lane.get("state"), "·")
        lanes.append((lane.get("state") != "running", f"{crew.who(m)} {mark} {what}".strip()))
    if not lanes:
        return ""
    lanes.sort(key=lambda x: x[0])
    parts = [x for _, x in lanes[:limit]] + ([f"+{len(lanes) - limit}"] if len(lanes) > limit else [])
    try:
        parts.append(slots_text(slot_budget(run.get("agent") or "claude", run, None, now)))
    except Exception:
        pass  # the line still names the crew
    return " · ".join(parts)


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
    grants = _live_grants(run.get("agent") or "", run.get("session") or "", now) if run.get("session") else []
    if grants:  # what the developer granted, echoed in the status until it is used or expires (spec C5)
        lines += ["", "Granted: " + "; ".join(f"{e['covers']} by your prompt at {_when(e['ts'])}, until "
                                              f"{_when(e['expires'])}" for e in grants)]
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
        if r and r.get("state") != "orphaned" and time.time() - f.stat().st_mtime < max_age_h * 3600:
            out.append(r)  # an orphaned run (its session ended, approvals.reconcile) is not live
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


LAST_SEEN_EVERY = 60  # seconds: a member's hooks refresh its lane's last_seen at most this often


def reconcile_hook(agent: str, data: dict, now: float | None = None) -> list:
    """Every hook of the session (spec C7, C3): the agent whose hook this is was seen (its lane's `last_seen`), and
    the reconciler's transitions are made on the session's own run (approvals.reconcile, then apply on the freshly
    read run; launch transitions on the session record). The facts are read outside the lock. -> transitions made"""
    from . import approvals
    sid = data.get("session_id") or "-"
    now = now or time.time()
    stopped = agent_stopped(agent, data, now)
    run = load_run(agent, sid)
    if not isinstance(run, dict) or run.get("state") not in approvals.LIVE:
        return stopped
    sess = state.read(session_path(agent, sid))
    aid = str(data.get("agent_id") or "")
    lane = (run.get("agents") or {}).get(aid) if aid else None
    seen = bool(lane) and now - float(lane.get("last_seen") or 0) >= LAST_SEEN_EVERY
    # the meta files beside the session's own transcript, never beside a path the event names (B1)
    fx = approvals.facts(run, sess, now, meta=approvals.meta_reader(harness_transcript(agent, sid)))
    if aid in fx["lanes"]:
        fx["lanes"][aid]["last_seen"] = now
    lim = approvals.limits()
    moves = approvals.reconcile(run, fx, now, lim)
    if not (seen or moves):
        return []
    made = []
    with _tx(agent, sid):
        doc = load_run(agent, sid)
        if not isinstance(doc, dict) or doc.get("state") not in approvals.LIVE:
            return []
        ln = (doc.get("agents") or {}).get(aid) if aid else None
        if ln is not None and seen:
            ln["last_seen"] = round(now, 1)
        made = approvals.apply(doc, approvals.reconcile(doc, fx, now, lim), now)
        if made or seen:
            _save(doc, touch_session=False)
    launches = [m for m in moves if m["kind"] == "launch"]
    if launches:
        with state.hold(session_path(agent, sid)):
            s = state.load(session_path(agent, sid))
            if isinstance(s, dict):
                done = approvals.apply_session(s, launches, now)
                if done:
                    state.replace(session_path(agent, sid), s)
                    made += done
    if made:
        approvals.audit_moves(doc, made)
    return stopped + made


STOP_TOOLS = ("TaskStop", "KillShell")  # Claude Code's tool that stops a background task (KillShell: older name)


def agent_stopped(agent: str, data: dict, now: float | None = None) -> list:
    """PostToolUse of TaskStop on a subagent's task (spec R6, B-fxsvxz9n78p): Claude Code fires no SubagentStop
    for a stopped agent, so its slot is freed here, at the stop call's result. Its launch record stops holding a
    slot; its working lane becomes stopped and the steps it owns unverified (no report, so no evidence), for
    Morpheus to close with evidence or reopen. A stopped agent that works again (SendMessage) resumes its lane and
    takes its slot back. -> the transitions made"""
    resp = data.get("tool_response")
    if data.get("tool_name") not in STOP_TOOLS or not isinstance(resp, dict):
        return []  # PreToolUse has no result yet: the stop may still fail
    inp = data.get("tool_input") or {}
    tid = str(resp.get("task_id") or inp.get("task_id") or inp.get("shell_id") or "")
    if resp.get("task_type") not in (None, "local_agent") or not AGENT_ID.fullmatch(tid):
        return []
    from . import approvals
    sid, now = data.get("session_id") or "-", now or time.time()
    f, aid = session_path(agent, sid), tid
    with state.hold(f):
        s = state.load(f)
        if isinstance(s, dict):
            ls = _launches(s, now)
            key = next((k for k, v in ls.items() if v.get("agent_id") == tid), None) or (tid if tid in ls else None)
            if key:
                aid = ls[key].get("agent_id") or aid
                if ls[key].get("state") in ("pending", "started"):
                    ls[key].update(state="stopped", stopped=round(now, 1))
            sub = (s.get("subagents") or {}).pop(aid, None)
            if sub and sub.get("started"):
                record("subagent", agent, sid, sub.get("label") or sub.get("type") or aid, now - sub["started"],
                       s.get("project") or "")
            state.replace(f, s)
    made = []
    with _tx(agent, sid):
        run = load_run(agent, sid)
        lane = ((run or {}).get("agents") or {}).get(aid) if isinstance(run, dict) else None
        if lane and lane.get("state") == "running":
            why = f"stopped by TaskStop at {time.strftime('%H:%M', time.localtime(now))}, before its report: no evidence"
            moves = [{"kind": "lane", "id": aid, "from": "running", "to": "stopped", "why": why, "at": now}]
            moves += [{"kind": "step", "id": st["name"], "from": "running", "to": "unverified", "owner_from": aid,
                       "owner": aid, "why": why} for st in run.get("plan") or []
                      if st.get("owner") == aid and st.get("state") == "running"]
            made = approvals.apply(run, moves, now)
            if made:
                _save(run, touch_session=False)
    if made:
        approvals.audit_moves(run, made)
    return made


def resume_orphaned(agent: str, sid: str, now: float | None = None) -> bool:
    """A resumed session takes back its run the reconciler orphaned when the session ended (spec C8). -> resumed"""
    now = now or time.time()
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not isinstance(run, dict) or run.get("state") != "orphaned":
            return False
        was = run.pop("orphaned", None) or {}
        run["state"] = "running"
        run.setdefault("history", []).append({"ts": round(now), "step": "(run)", "from": "orphaned", "to": "running",
                                              "by": "devkit", "why": f"its session resumed (orphaned: "
                                              f"{str(was.get('why') or 'its session ended')[:120]})"})
        _save(run, touch_session=False)
    _audit("run.running", run.get("goal") or "run", "its session resumed", "running", project=run.get("project") or "",
           by="devkit", was="orphaned")
    return True


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
BOUND = ("exact", "conflict", "unbound", "confirmed")  # a lane whose binding no later hook changes


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
    launch record keyed by the call's tool_use_id, with its description, type, caller (the launching subagent's
    agent_id, or main) and depth (1 for Morpheus's launches, 2 for a helper a member launches; spec C2). The agent it starts binds to exactly this record (see bind); nothing takes it by order. A
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
    parent = str(data.get("agent_id") or "main")
    entry = {"tool_use_id": tid or None, "label": str(label)[:80],
             "type": inp.get("subagent_type") or inp.get("agent_type") or "", "parent": parent,
             "depth": launch_depth(s, None, parent), "ts": round(now, 1), "state": "pending", "agent_id": None}
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
    `unbound` when the meta file names a launch devkit has no record of (the record was lost: the state was busy at
    the launch, or it was dropped after a day), with a record made from the meta file that is never stored;
    (None, None, None) while nothing names one."""
    ls = _launches(s, now)
    tid = next((k for k, v in ls.items() if v.get("agent_id") == aid), None)
    if tid:
        return tid, ls[tid], "exact"
    meta = _launch_meta(data)
    tid = str(meta.get("toolUseId") or "")
    rec = ls.get(tid) if tid else None
    if tid and not rec:
        return tid, {"label": str(meta.get("description") or "")[:80], "type": str(meta.get("agentType") or ""),
                     "parent": None}, "unbound"
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
    to its launch: label, parent, depth, persona and the step the launch names (none for a member's helper, which
    works inside its member's step; spec F3). `own`: the hook is the agent's own start or tool
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
        parent = rec.get("parent") or "main"
        lane.update(binding="exact", launch=tid, label=label, parent=parent,
                    depth=rec.get("depth") or (1 if parent == "main" else 2),
                    persona=persona(kind or rec.get("type") or "", label))
        _audit("crew.bind", lane["persona"] or label, label, project=run.get("project") or "", agent_id=aid[:16],
               launch=str(tid)[:40])
        if lane.get("state") == "running" and parent == "main":
            _bind_step(run, aid, lane, now)
    elif binding == "conflict" and lane.get("binding") == "pending":
        lane["binding"] = "conflict"
        _audit("crew.bind-conflict", lane.get("persona") or kind or aid[:8], f"claims launch {tid}, which another "
               f"agent is bound to", "deny", project=run.get("project") or "", agent_id=aid[:16])
    elif binding == "unbound" and lane.get("binding") == "pending":  # restricted until Morpheus links a step to it
        label = rec.get("label") or lane.get("label") or kind
        lane.update(binding="unbound", launch=tid, label=label, persona=persona(kind or rec.get("type") or "", label))
        _audit("crew.unbound", lane["persona"] or label or aid[:8], f"launch {str(tid)[:40]} has no record: the "
               f"agent may read, and takes no step, scope or slot until Morpheus assigns it a step", "restricted",
               project=run.get("project") or "", agent_id=aid[:16])


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
    if lane.get("state") == "running" and lane.get("binding", "exact") in BOUND:
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


SCOUTS = ("sparks", "scout")  # the crew's lookup scout: its launches cost progress.slot_weights.scout


def _member_named(kind: str = "", label: str = ""):
    """The crew member a launch's type or description names (devkit:seraph, "Seraph: review"), or None."""
    try:
        from . import crew
        name = persona(kind, label)
        return (crew.member(name) if name else None) or crew.member((kind or "").split(":")[-1]) or None
    except Exception:
        return None


def _cost(member, level: str | None) -> float:
    """What one launch costs (spec R3, progress.slot_weights): a scout lookup, a review-only or read-only member (the
    most restrictive tool level along its parent chain), or a builder (everything else)."""
    w = cfg()["slot_weights"]
    if member and member.get("id") in SCOUTS:
        return float(w["scout"])
    return float(w[level]) if level in ("review", "read") else float(w["builder"])


def launch_cost(run, kind: str = "", label: str = "", parent: str = "main") -> float:
    """The slots a launch takes before its agent works: by its member's tool level and, for a member's helper, the
    level of its parent's chain (a helper of a reviewer reviews too)."""
    m = _member_named(kind, label)
    level = m["tools"] if m and m.get("tools") in TOOL_LEVELS else "all"
    if parent and parent != "main":
        up = lane_tools(run or {}, parent)
        if up:
            level = max((level, up), key=TOOL_LEVELS.index)
    return _cost(m, level)


def step_cost(run, s: dict) -> float:
    """The slots a plan step's member takes when launched for it."""
    return launch_cost(run, "", f"{s.get('label') or ''}: {s.get('name') or ''}")


def slot_use(run: dict, sess: dict | None = None, now: float | None = None) -> list:
    """The crew slots in use, [(id, name, cost)] (spec D2, R3): launches whose agent has not started yet, and working
    lanes bound to a launch (a resumed one too), each at its cost. A provisional lane counts through its launch.
    Lanes bound before launch records existed count while they run."""
    now = now or time.time()
    if sess is None:
        sess = core.read_json(session_path(run.get("agent") or "", run.get("session") or ""), {}) if run else {}
    sess = sess if isinstance(sess, dict) else {}
    agents, subs = (run or {}).get("agents") or {}, sess.get("subagents") or {}
    out = []
    for tid, rec in (sess.get("launches") or {}).items():
        aid = rec.get("agent_id")
        cost = launch_cost(run, rec.get("type") or "", rec.get("label") or "", rec.get("parent") or "main")
        if rec.get("state") == "pending" and now - (rec.get("ts") or now) < LAUNCH_TTL:
            out.append((tid, rec.get("label") or rec.get("type") or "a launch", cost))
        elif rec.get("state") == "started" and aid not in agents and aid in subs:
            out.append((aid, rec.get("label") or str(aid)[:8], cost))  # working in a session without a run
    try:
        from . import crew
    except Exception:
        crew = None
    for k, v in agents.items():
        if v.get("state") == "running" and v.get("binding", "exact") in ("exact", "confirmed"):
            m = crew._for_lane(v) if crew else None
            out.append((k, v.get("persona") or v.get("label") or k[:8], _cost(m, lane_tools(run, k))))
    return out


def slots(run: dict, sess: dict | None = None, now: float | None = None) -> list:
    """The crew slots in use, [(id, name)]; see slot_use for what each costs."""
    return [(k, name) for k, name, _ in slot_use(run, sess, now)]


def slot_budget(agent: str, run: dict | None = None, sess: dict | None = None, now: float | None = None) -> dict:
    """The crew's slots now (spec R3, R4, R7) -> {used (the costs summed), limit, max (progress.max_parallel), reserve
    (the slots kept for the developer's own request: progress.reserve, never the last slot of a limit of one), why
    (the limit is lower), busy [(id, name, cost)]}."""
    limit, why = parallel_limit(agent, now)
    busy = slot_use(run, sess, now) if run else []
    c = cfg()
    return {"used": round(sum(x for *_, x in busy), 2), "limit": limit, "max": int(c["max_parallel"]),
            "reserve": min(int(c["reserve"]), max(0, limit - 1)), "why": why, "busy": busy}


def _n(x) -> str:
    return f"{float(x):g}"


def slots_text(b: dict) -> str:
    """'1 of 3 slots in use (lowered from 6: 5-hour usage at 87 %)' (spec R7)."""
    out = f"{_n(b['used'])} of {b['limit']} slots in use"
    return out + (f" (lowered from {b['max']}: {b['why']})" if b["limit"] < b["max"] and b.get("why") else "")


def _step_added(run: dict, s: dict) -> float:
    """When a plan step joined the plan: its own time, or the run's start for the steps it started with."""
    return float(s.get("added") or (run or {}).get("started") or 0)


def named_step(run: dict, kind: str = "", label: str = ""):
    """The open plan step a launch of Morpheus's names, as _bind_step would bind it (of the steps assigned to its
    member, the one its task text names best, and only one), or None."""
    pseudo = {"label": label, "type": kind, "persona": persona(kind, label)}
    mine = [st for st in (run or {}).get("plan") or [] if st.get("label") and st.get("state") not in DONE
            and (st.get("owner") or "main") == "main" and _label_matches(st["label"], pseudo)]
    if not mine:
        return None
    scored = [(_name_score(_task_text(label), st["name"]), st) for st in mine]
    best = max(sc for sc, _ in scored)
    top = [st for sc, st in scored if sc == best]
    return top[0] if best[1] and len(top) == 1 else None


def is_latest_request(run: dict, sess: dict, s: dict) -> bool:
    """A step the developer asked for in the current turn: added since its prompt, by Morpheus (not delegated by a
    member). True for every step when the turn is unknown (no prompt recorded)."""
    turn = (sess or {}).get("turn_started")
    return not turn or (_step_added(run, s) >= float(turn) and (s.get("requested_by") or "main") == "main")


# Nested agents (spec F3, contract C3). A subagent can launch agents of its own; such a helper gets its parent's
# tool level and scope, never more, and counts against the parallel limit. Permissions are computed from the lanes
# and steps each time, never stored on the helper. The hooks (_crew_limits, _scope_violation) and the guard's shell
# rules call these.

TOOL_LEVELS = ("all", "build", "review", "read")  # least to most restrictive (crew.json `tools`)


def lane_chain(run: dict, aid: str) -> list:
    """The lane and the lanes above it, [aid, parent, grandparent, …], up to Morpheus's launch; [] when `aid` has no
    lane. Stops at a parent with no lane in this run (a helper of an agent devkit never saw) and at a cycle."""
    agents, out, cur = (run or {}).get("agents") or {}, [], str(aid or "")
    while cur and cur != "main" and cur in agents and cur not in out:
        out.append(cur)
        cur = str(agents[cur].get("parent") or "main")
    return out


def _lane_level(lane: dict) -> str:
    """One lane's own tool level: read for a lane whose launch another agent is bound to, that devkit has no launch
    record of (until Morpheus assigns it a step), or that has not met its launch yet (spec C3: a missing binding
    grants nothing), else its crew member's `tools`, else all (an ad-hoc agent)."""
    if lane.get("binding") in ("conflict", "unbound", "pending"):
        return "read"
    try:
        from . import crew
        m = crew._for_lane(lane)
    except Exception:
        m = None
    return m["tools"] if m and m.get("tools") in TOOL_LEVELS else "all"


def lane_tools(run: dict, aid: str):
    """The tool level a lane works with: the most restrictive along its parent chain, so a general-purpose helper
    of a review-only member is review-only too. One of TOOL_LEVELS, or None when `aid` has no lane."""
    chain = lane_chain(run, aid)
    if not chain:
        return None
    return max((_lane_level(run["agents"][k]) for k in chain), key=TOOL_LEVELS.index)


def lane_scope(run: dict, aid: str) -> list:
    """The scope a lane edits in, as layers [[globs], …]: one per lane in its parent chain that has a scope (the
    lane's own globs and those of the open steps it owns). A path must match every layer, so a helper stays inside
    its member's files, and a helper with a scope of its own inside both. [] when nothing is scoped."""
    plan = (run or {}).get("plan") or []
    layers = []
    for k in lane_chain(run, aid):
        globs = set(run["agents"][k].get("scope") or [])
        for st in plan:
            if st.get("owner") == k and st.get("state") not in DONE:
                globs.update(st.get("scope") or [])
        if globs:
            layers.append(sorted(globs))
    return layers


def in_scope(rel_path: str, layers: list) -> bool:
    """A repo-relative path is inside every layer of a lane_scope (the same glob rules as scope_owner)."""
    return all(_in_globs(rel_path, layer) for layer in layers if layer)


def launch_depth(sess: dict, run: dict | None, parent: str) -> int:
    """How deep a launch by `parent` (an agent_id, or main) sits: 1 for Morpheus's, its parent's depth + 1 for a
    subagent's. A parent devkit has no record of counts as depth 1."""
    if not parent or parent == "main":
        return 1
    lane = ((run or {}).get("agents") or {}).get(parent) or {}
    rec = next((v for v in ((sess or {}).get("launches") or {}).values() if isinstance(v, dict)
                and v.get("agent_id") == parent), None) or {}
    return int(lane.get("depth") or rec.get("depth") or 1) + 1


def _window_used(agent: str, now: float, key: str):
    w = (core.read_json(QUOTA / f"{agent}.json", {}) or {}).get(key) or {}
    if not isinstance(w, dict) or w.get("used_percentage") is None or (w.get("resets_at") or 0) <= now:
        return None
    try:
        return float(w["used_percentage"])
    except (TypeError, ValueError):
        return None


def quota_used(agent: str, now: float | None = None):
    """The share of the plan's 5-hour window used, in percent, as the status line last recorded it; None without
    a reading or when that window has reset since."""
    return _window_used(agent, now or time.time(), "five_hour")


def weekly_used(agent: str, now: float | None = None):
    """The share of the plan's weekly window used, in percent; None without a reading or once it has reset."""
    return _window_used(agent, now or time.time(), "seven_day")


def quota_share(used: float, c: dict | None = None) -> float:
    """The share of max_parallel the crew may use at this usage (spec R2): the factor of the highest
    progress.quota_steps step at or below it (1 below every step), and 0 (one member) from progress.quota_percent."""
    c = c or cfg()
    steps = {float(k): float(v) for k, v in (c.get("quota_steps") or {}).items()}
    steps[float(c["quota_percent"])] = 0.0
    hit = [pct for pct in steps if used >= pct]
    return steps[max(hit)] if hit else 1.0


def team_ceiling():
    """The team's ceiling on progress.max_parallel (`max_parallel` in policy/progress.json of the handoff
    repository), or None."""
    v = (core.read_json(TEAM_POLICY, {}) or {}).get("max_parallel") if TEAM_POLICY.exists() else None
    return v if core.PROGRESS_RULES["max_parallel"][0](v) else None


def parallel_limit(agent: str, now: float | None = None) -> tuple:
    """How many crew members may work at once -> (limit, why it is lower than progress.max_parallel, or ""): the
    configured max_parallel, under the team's ceiling, scaled by the plan's recorded usage (progress.quota_steps,
    5-hour and weekly windows; an expired reading counts for nothing), never below 1. Each change is audited."""
    c = cfg()
    top, why = int(c["max_parallel"]), ""
    team = team_ceiling()
    if team is not None and team < top:
        top, why = team, f"the team's ceiling is {team} ({core.tilde(TEAM_POLICY)})"
    limit = top
    for label, used in (("5-hour", quota_used(agent, now)), ("weekly", weekly_used(agent, now))):
        if used is None:
            continue
        n = max(1, int(top * quota_share(used, c) + 1e-9))
        if n < limit:
            limit, why = n, f"{label} usage at {used:.0f} %"
    _note_limit(agent, limit, why, int(c["max_parallel"]), now)
    return limit, why


def _note_limit(agent: str, limit: int, why: str, configured: int, now: float | None):
    """Audit each change of the effective limit once (spec R7): the last one is kept beside the quota reading."""
    f = QUOTA / f"{agent}.limit.json"
    if (core.read_json(f, {}) or {}).get("limit", configured) == limit and (f.exists() or limit == configured):
        return
    was = {}

    def fn(d):
        was.update(d)
        if d.get("limit", configured) == limit and d:
            return state.SKIP
        d.update(limit=limit, why=why, ts=round(now or time.time()))
        return None
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        state.update(f, fn)
    except Exception:
        return
    old = was.get("limit", configured)
    if old != limit:
        _audit("crew.limit", f"{limit} crew slots", f"{old} → {limit}: {why or 'progress.max_parallel'}",
               project="")


def launch_verdict(agent: str, data: dict, run: dict, now: float | None = None):
    """Why a subagent launch (Agent, Task, spawn_agent) must not run -> the reason, or None. It applies at every
    depth: a helper a member launches takes a slot like the member did, and no launch goes deeper than
    progress.max_depth."""
    now = now or time.time()
    sess = core.read_json(session_path(agent, data.get("session_id") or "-"), {}) or {}
    sess = sess if isinstance(sess, dict) else {}
    parent = str(data.get("agent_id") or "main")
    depth, most = launch_depth(sess, run, parent), int(cfg()["max_depth"])
    if depth > most:
        return (f"This launch would run {depth} levels below Morpheus; the limit is {most} (progress.max_depth). Do "
                f"this part yourself, or name it in your report and Morpheus assigns it.")
    inp = data.get("tool_input") or {}
    kind = str(inp.get("subagent_type") or inp.get("agent_type") or "")
    label = str(inp.get("description") or inp.get("nickname") or inp.get("name") or "")
    cost = launch_cost(run, kind, label, parent)
    if cost <= 0:
        return None  # a scout lookup costs no slot (progress.slot_weights)
    b = slot_budget(agent, run, sess, now)
    used, limit, reserve = b["used"], b["limit"], b["reserve"]
    if used + cost > limit + 1e-9:
        names = ", ".join(name for _, name, _ in b["busy"])
        head = f"{_n(used)} of {limit} crew slots are in use ({names}): the limit is {limit} at once"
        head += (f", lowered from {b['max']}: {b['why']}" if b["why"] else "") + \
            (f", and this launch takes {_n(cost)}" if cost != 1 else "")
        if parent != "main":
            return (f"{head}. A helper takes a slot like any crew member, so do this part yourself, or name it in "
                    f"your report and Morpheus assigns it.")
        return (f"{head}. Wait for one to finish (you are notified), or give the work to one of them (SendMessage / "
                f"send_input). progress.max_parallel in devkit config changes the limit.")
    if used + cost > limit - reserve + 1e-9:  # it would take the slot kept for the developer (spec R4)
        if parent == "main" and _for_the_developer(run, sess, kind, label):
            _audit("crew.reserve", label or kind or "a launch", f"{_n(used + cost)} of {limit} slots: the slot "
                   f"progress.reserve keeps for the developer's own request", project=(run or {}).get("project") or "")
            return None
        return (f"{_n(used)} of {limit} crew slots are in use, and the last {reserve} is kept for the developer's own "
                f"request (progress.reserve): queued plan work waits for a free slot. Launch it when a member "
                f"finishes (dev crew next), or give it to a working member (SendMessage / send_input).")
    return _order_verdict(run, sess, kind, label, b, cost, now) if parent == "main" else None


def _for_the_developer(run, sess, kind: str, label: str) -> bool:
    """A launch for what the developer asked in the current turn (spec R4): it names no open plan step (ad-hoc work
    for their request), or one added in this turn."""
    st = named_step(run, kind, label)
    return st is None or is_latest_request(run, sess, st)


def _order_verdict(run, sess, kind: str, label: str, b: dict, cost: float, now: float):
    """Morpheus launches the ready steps in their recorded order (spec R5, dispatch.order): a launch for a lower
    step is refused when it would leave no room for a higher one, unless he recorded why (dev crew next --first)."""
    st = named_step(run, kind, label)
    if not st or st["name"] in ((run or {}).get("order_why") or {}):
        return None
    from . import dispatch
    ranked = dispatch.order(run, now, sess)
    names = [s["name"] for s, _ in ranked]
    if st["name"] not in names or names.index(st["name"]) == 0:
        return None
    first, why = ranked[0]
    if b["limit"] - b["reserve"] - b["used"] - cost + 1e-9 >= step_cost(run, first):
        return None  # both fit
    return (f"“{first['name']}” comes first ({why}), and launching {label or kind} for “{st['name']}” would leave no "
            f"slot for it. Launch {first.get('label') or 'its member'} for “{first['name']}” first (dev crew next), "
            f"or record why this one goes ahead: dev crew next --first {shlex.quote(st['name'])} --why \"…\"")


def on_subagent(agent: str, data: dict, started: bool, now: float | None = None):
    """SubagentStart / SubagentStop; see _on_subagent. Serialized per session like note_agent_launch: one hold covers
    the session record and the run (the run's save inside re-enters it)."""
    with state.hold(session_path(agent, data.get("session_id") or "-")):
        return _on_subagent(agent, data, started, now)


def _stop_report(data: dict) -> str:
    """A SubagentStop's report: the member's last message (SubagentStart sends none)."""
    return str(data.get("last_assistant_message") or "")


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
    report = "" if started else _stop_report(data)
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
                if st.get("owner") == aid and st.get("state") not in DONE:
                    _end_member_step(run, st, lane or {}, aid, report, now, agent, sid)
            run["step"] = sum(1 for x in run.get("plan") or [] if x.get("state") in ("done", "skipped"))
            _advance_main(run, now)
        _run_event(agent, sid, finish)
    state.replace(f, s)


def scope_written(agent: str, sid: str, aid: str, paths: list, now: float | None = None) -> dict | None:
    """A review or read-only lane's shell call wrote these files (spec F7, write detection): the lane keeps them in
    `scope_writes`, so its report is unverified and its step cannot end done on its word; Morpheus gets a crew
    message; and the audit trail a scope.write record. "possible" when another lane that may write works in the run
    at the time (the file may be its), else "certain". -> the lane's scope_writes, or None without a lane."""
    now = now or time.time()
    paths = sorted({str(p) for p in paths or [] if p})
    if not paths:
        return None
    with _tx(agent, sid):
        run = load_run(agent, sid)
        lane = ((run or {}).get("agents") or {}).get(aid)
        if not lane:
            return None
        chain = set(lane_chain(run, aid))
        others = [k for k, v in run["agents"].items() if k not in chain and v.get("state") == "running"
                  and lane_tools(run, k) in ("build", "all")]
        certainty = "possible" if others else "certain"
        level = lane_tools(run, aid)
        had = lane.get("scope_writes") or {}
        lane["scope_writes"] = {"ts": round(now), "paths": sorted(set(had.get("paths") or []) | set(paths))[:20],
                                "certainty": "certain" if "certain" in (had.get("certainty"), certainty) else
                                certainty}
        who = lane.get("persona") or lane.get("label") or aid
        shown = ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")
        maybe = f" (possibly another member's: {', '.join(_name_of(run, k) for k in others[:3])} works too)" \
            if others else ""
        _post(run, aid, "main", f"devkit: {who} works {'review-only' if level == 'review' else 'read-only'}, and "
              f"its shell call wrote {shown}{maybe}. Its report counts as unverified: check the change (git diff), "
              f"keep or revert it, then close its step with evidence.", now, from_name="devkit")
        _save(run)
        writes = dict(lane["scope_writes"])
    _audit("scope.write", who, f"{certainty}: {', '.join(paths[:20])}"[:600], "detected", lane=aid[:16],
           paths=paths[:20], certainty=certainty, count=len(paths), project=run.get("project") or "",
           **({"severity": "warning"} if certainty == "certain" else {}))
    return writes


def _end_member_step(run: dict, st: dict, lane: dict, aid: str, report: str, now: float, agent: str, sid: str):
    """A member's step at its SubagentStop (spec F6, R2-4): done with the evidence of its report's Check or its last
    `dev progress step --check` (approvals.ending), and a review step's Check is its verdict on the work it follows
    (approvals.from_review); otherwise unverified, for Morpheus to close with evidence or reopen. A review or
    read-only lane whose shell wrote files (scope_writes) ends unverified whatever it reports."""
    from . import approvals
    old = st.get("state")
    state_, why, check = approvals.ending(lane, report)
    writes = lane.get("scope_writes") or {}
    if state_ == "done" and writes.get("paths"):
        state_, why = "unverified", f"its shell call wrote {', '.join(writes['paths'][:3])} ({writes.get('certainty')})"
    who = lane.get("persona") or "subagent"
    if state_ != "done":
        st["state"] = "unverified"
        _log_change(run, st, old, "unverified", aid, f"{who} stopped; unverified: {why}", now)
        return
    _finish_step(run, st, now, agent, sid)
    _log_change(run, st, old, "done", aid, f"{who} finished; {why}", now)
    try:
        approvals.from_review(run, st, check, now, doer_of=lambda s_: member_of_step(run, s_))
    except approvals.ApprovalError as e:  # a review that may approve nothing: its step still ends
        _audit("crew.approval-refused", st.get("name") or "", str(e)[:300], "refused", project=run.get("project") or "")


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


_register_checks()  # `dev audit verify` checks every piece of developer evidence (spec C5, K1)
