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

import copy
import hashlib
import json
import os
import re
import shlex
import unicodedata
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


def resolve_session(cwd: str | None = None, argv=None):
    """-> (agent, session id, how) of the session running this command (spec C4):
    ancestry  the nearest parent process whose pid a session record holds (the agent's own process, recorded at
              SessionStart), so two sessions in one repository each find their own; of that process's sessions, the
              one whose call ledger holds an open call for this very command, else the one DEVKIT_SESSION names
              (after /clear), else the newest
    ledger    no session of this process holds an open call for it, and exactly one recent session of this
              repository that no live process holds does: Claude Code gave the running conversation a new session id
              (a compaction), and the older record still holds the process until the new one registers it
    env       DEVKIT_SESSION, which Claude Code exports to the session's shells (no parent matched)
    recency   the most recently active devkit session in the same repository (Codex without a recorded pid)
    shell     none of these: a shell of the developer's, with a session of its own
    `argv`: the command's arguments, default sys.argv[1:] (the ledger's match, as caller reads it)."""
    env = os.environ.get("DEVKIT_SESSION", "")
    args = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
    pids = _ancestor_pids()
    held = {}
    for f in (SESSIONS.glob("*.json") if pids and SESSIONS.exists() else []):
        s = core.read_json(f, {}) or {}
        if isinstance(s.get("pid"), int) and s["pid"] in pids and s.get("agent") and s.get("session"):
            held.setdefault(s["pid"], []).append((s.get("started") or 0, s["agent"], s["session"]))
    mine = next((held[pid] for pid in pids if pid in held), None)
    if mine:
        typed = [c for c in mine if _ledger_holds(c[2], args)]
        if typed:
            _, agent, sid = max(typed)
            return agent, sid, "ancestry"
    cwd = cwd or os.getcwd()
    found = _pidless_typed(args, {c[2] for c in mine or []}, pids, cwd)
    if found:
        return found[0], found[1], "ledger"
    if mine:
        named = [c for c in mine if f"{c[1]}:{c[2]}" == env]
        _, agent, sid = named[0] if named else max(mine)
        return agent, sid, "ancestry"
    if ":" in env:
        agent, sid = env.split(":", 1)
        return agent, sid, "env"
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
    best = None
    for f in SESSIONS.glob("*.json") if SESSIONS.exists() else []:
        s = core.read_json(f, {}) or {}
        if (s.get("top") or s.get("cwd")) == top and (not best or f.stat().st_mtime > best[0]):
            best = (f.stat().st_mtime, s.get("agent"), s.get("session"))
    if best and time.time() - best[0] < 12 * 3600:
        return best[1], best[2], "recency"
    return os.environ.get("DEVKIT_AGENT") or "human", f"shell-{os.getppid()}", "shell"


def _ledger_holds(sid: str, args: list) -> bool:
    """Does this session's call ledger hold an open call whose own text typed these arguments? A cut call matches
    only by the text it kept here: choosing a session needs the command itself."""
    return bool([a for a in args if a]) and any(_names_args(dict(e, cut=False), args) for e in open_calls(sid))


def _pid_running(pid) -> bool:
    """A process with this pid runs (or devkit cannot tell, which counts as running: such a record is not taken)."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, TypeError, ValueError):
        return True
    return True


def _ledger_sessions(args: list) -> list:
    """Every session whose call ledger holds an open call that typed these arguments, [(agent, session id)]."""
    if not [a for a in args if a]:
        return []
    try:
        dirs = sorted(d.name for d in CALLS.iterdir() if d.is_dir())
    except OSError:
        return []
    out = []
    for sid in dirs:
        hit = next((e for e in open_calls(sid) if _names_args(dict(e, cut=False), args)), None)
        if hit:
            out.append((str(hit.get("agent") or ""), str(hit.get("session") or sid)))
    return out


def _pidless_typed(args: list, skip: set, pids: list, cwd: str):
    """The one recent session (12 hours) of this repository that no live process holds and whose call ledger holds an
    open call for this command -> (agent, session id), or None (none, or several). Only for a process that has a
    parent besides launchd: a detached command is never matched by the ledger alone."""
    if not pids:
        return None
    hits = [(a, s) for a, s in _ledger_sessions(args) if s not in skip]
    found, top = [], None
    for agent, sid in hits:
        f = session_path(agent, sid)
        rec = core.read_json(f, {}) or {}
        pid = rec.get("pid")
        if rec.get("agent") != agent or (isinstance(pid, int) and (pid in pids or _pid_running(pid))):
            continue
        try:
            if time.time() - f.stat().st_mtime > 12 * 3600:
                continue
        except OSError:
            continue
        top = top or core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
        if (rec.get("top") or rec.get("cwd")) == top:
            found.append((agent, sid))
    return found[0] if len(found) == 1 else None


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
        os.chmod(d, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (0700 is owner-only: this tightens the folder)
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


EDIT_TOOLS = ("Write", "Edit", "MultiEdit")  # the tools that name the one file they change (tool_input.file_path)
EDIT_SLACK = 2.0  # seconds an edit may precede a shell call's start and still fall in its window (hook ordering)
EDIT_KEEP = 12 * 3600  # seconds the edit record keeps a call: a lane's whole life, which the crew gates read
EDIT_MAX = 1000  # edit calls the record keeps at most, newest last


def _edits_path(sid: str) -> Path:
    """The session's recent edit calls, beside its open calls (a dot-file: the ledger's readers skip it)."""
    return CALLS / _safe_name(sid) / ".edits.json"


def note_edit(agent: str, data: dict, now: float | None = None):
    """PostToolUse of a Write, Edit or MultiEdit: the file it changed, who changed it (the agent_id, or main) and when,
    kept for EDIT_KEEP, so write detection credits a file to whoever really edited it (scope_written) and the crew
    gates credit a member only with what it wrote (gates.crew_gates). Under the record's lock; a busy lock drops this
    one record (state audits it)."""
    if data.get("tool_name") not in EDIT_TOOLS:
        return
    path, sid = (data.get("tool_input") or {}).get("file_path"), _safe_name(data.get("session_id"))
    if not sid or not isinstance(path, str) or not path.strip():
        return
    now = now or time.time()
    who = str(data.get("agent_id") or "main")[:80]

    def add(doc):
        kept = [e for e in doc.get("edits") or [] if isinstance(e, dict) and now - (e.get("ts") or 0) <= EDIT_KEEP]
        doc["edits"] = (kept + [{"ts": round(now, 3), "caller": who, "path": path[:1024]}])[-EDIT_MAX:]
    try:
        state.update(_edits_path(sid), add)
    except state.StateBusy:
        pass


def edits_since(sid: str, since: float, now: float | None = None) -> list:
    """The edit calls of the session recorded from `since` (less EDIT_SLACK) to now: [{ts, caller, path}]."""
    now = now or time.time()
    doc = core.read_json(_edits_path(sid), {}) or {}
    return [e for e in doc.get("edits") or [] if isinstance(e, dict)
            and since - EDIT_SLACK <= (e.get("ts") or 0) <= now + EDIT_SLACK]


def _edited(edit_path: str, rel: str) -> bool:
    """Does an edit call's file (absolute, as the tool names it) name `rel`, a path relative to the repository?"""
    e, r = str(edit_path or "").replace("\\", "/"), str(rel or "").replace("\\", "/")
    while r.startswith("./"):  # only a leading "./": a dotfile (.github/…, .env) keeps its dot
        r = r[2:]
    return bool(r) and (e == r or e.endswith("/" + r))


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
    args = [str(a) for a in (sys.argv[1:] if argv is None else argv)]
    agent, sid, how = resolve_session(cwd, args)
    out = {"agent": agent, "session": sid, "how": how, "caller": "unknown", "calls": []}
    if how == "shell":
        return dict(out, caller="human")
    if agent == "codex":
        return dict(out, caller="unverified")
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


def _settle(run: dict):
    """What every save keeps true, whichever path changed the run (a command, a hook, the reconciler): a
    ready-to-launch notice belongs to a pending step, so a step that leaves pending is announced again once it is
    ready again; and a run blocked on a step (`blocked_on`) stays blocked only while that step is."""
    for s in run.get("plan") or []:
        if isinstance(s, dict) and s.get("ready_told") and s.get("state") != "pending":
            s.pop("ready_told", None)
    if run.get("state") != "blocked":
        run.pop("blocked_on", None)
    elif run.get("blocked_on"):
        s = _by_name(run.get("plan") or []).get(run["blocked_on"])
        if not s or s.get("state") != "blocked":
            run["state"] = "running"
            run.pop("blocked_on", None)


def _save(run: dict, touch_session: bool = True):
    """Write the run atomically through state, settled first (_settle). The caller loaded it inside the same _tx."""
    _settle(run)
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
    activity id or "")]. The [id] tag declares the step's kind of work (an id from dev crew raci). A name that still
    holds an estimate ("a=5h b", two specs run together) is refused: StepError."""
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
        name, est = (m.group(1).strip(), parse_minutes(m.group(2))) if m and parse_minutes(m.group(2)) else (raw, None)
        if "=" in name or re.search(r"@\s*\d", name):
            raise StepError(f"step “{name}” holds another estimate: separate the steps with ; "
                            f"(dev progress add \"a=5h; b=2h\")")
        out.append((name, est, after, act))
    return out


def spec_args(args) -> list:
    """The step specs of `dev progress add <spec>…`: each argument with a space in it is a spec of its own (the shell
    kept it whole, so it was quoted), and arguments without one are the words of one spec typed without quotes
    (review=20m after api); a group of words that opens with `after` or a [tag] finishes the spec before it. Each
    spec may hold several steps separated by ;."""
    out, words = [], []

    def flush():
        if words:
            grp = " ".join(words)
            if out and (grp.lower().startswith("after ") or grp.startswith("[")):
                out[-1] += " " + grp
            else:
                out.append(grp)
            words.clear()
    for a in args if isinstance(args, (list, tuple)) else [args]:
        a = str(a or "").strip()
        if not a:
            continue
        if re.search(r"\s", a):
            flush()
            out.append(a)
        else:
            words.append(a)
    flush()
    return [p.strip() for s in out for p in s.split(";") if p.strip()]


def _new_step(name, est=None):
    return {"name": name, "est": est, "est_auto": est is None, "state": "pending", "owner": "main", "label": "",
            "started": None, "finished": None}


# --------------------------------------------------------------------------- the plan's dependencies (a DAG)
#
# A step's `after` lists, by name, the steps it waits for: the plan is a directed acyclic graph, and the functions
# below are the only place that decides about it. Every transition that starts, owns or closes a step asks them.
#   entering   start and add resolve each `after` to one step of the plan and refuse a name that is not a step, a
#              name that matches several, two steps with one name, and a cycle (spelled out step by step)
#   done       a dependency is satisfied when its step is done or skipped (DONE); for a review step (is_review_step)
#              an unverified one is too, since that review is what approves it (spec F6, B-fy3i742ews7)
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


def is_review_step(s) -> bool:
    """A review step, by the one rule approvals.is_review keeps (spec F6): its kind of work is review, or the member
    it is assigned to works review-only. The same rule decides whether its ✓ verifies the work it follows
    (_verified_by_review) and approves it (approvals.from_review). A RACI that cannot be read makes no step a review
    step, so the step keeps waiting."""
    from . import approvals
    return approvals.is_review(s)


def deps_open(run, s) -> list:
    """The steps `s` waits for that are not done or skipped yet, in the order of its `after`. A review step does not
    wait for an unverified one: its Check is the verdict that approves that work (spec F6)."""
    steps = _by_name((run or {}).get("plan"))
    out = [a for a in s.get("after") or [] if a in steps and steps[a].get("state") not in DONE]
    if any(steps[a].get("state") == "unverified" for a in out) and is_review_step(s):
        out = [a for a in out if steps[a].get("state") != "unverified"]
    return out


def ready(run) -> list:
    """The pending steps whose dependencies are all satisfied (deps_open), in plan order."""
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
    """An ETA range, rounded like every estimate (fmt_about): "~25m–45m"."""
    a, b = fmt_about(lo), fmt_about(hi)
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


STEP_NAME_MAX = 40  # characters of a step's name on the Progress line, so the line stays one line
STEPS_NAMED = 3  # running steps the Progress line names; the rest are counted ("+2")


def step_counts(run: dict) -> tuple:
    """(done, the steps that count, skipped): a skipped step counts in neither of the first two numbers, so
    "2 of 5 steps" means two finished steps of the five that will be worked on."""
    plan = (normalize(run) or {}).get("plan") or []
    skipped = sum(1 for s in plan if s.get("state") == "skipped")
    return sum(1 for s in plan if s.get("state") == "done"), len(plan) - skipped, skipped


def _short(name: str, limit: int = STEP_NAME_MAX) -> str:
    """A step's name within `limit` characters, cut at a word with "…"."""
    name = " ".join(str(name or "").split())
    if len(name) <= limit:
        return name
    cut = name[:limit - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:·") + "…"


def _steps_in(plan: list, state: str) -> str:
    """The plan's steps in `state` as "2. guard findings, 4. welcome research" (their number in the plan, as the
    checklist under the status block shows them), at most STEPS_NAMED and "+N" for the rest; '' when none."""
    hits = [f"{i}. {_short(s.get('name'))}" for i, s in enumerate(plan, 1) if s.get("state") == state]
    if len(hits) > STEPS_NAMED:
        return ", ".join(hits[:STEPS_NAMED]) + f" +{len(hits) - STEPS_NAMED}"
    return ", ".join(hits)


def headline(run: dict, now: float | None = None, states=None) -> str:
    """The status block's Progress line, also what dev status, dev crew and the dashboard show for a run:
    '14 of 21 steps · 11 skipped · now 28. guard findings, 33. welcome research · 42m active (1h05m wall) of ~3h00m
    · ETA ~24m–37m · 2 agents working · 1 agent stale'. Skipped steps count in neither number (step_counts); the
    running steps (or the blocked ones) carry their number in the plan; then the members that still work and those
    that went stale (lane_states, read when `states` is None); one that ended is not counted."""
    now = now or time.time()
    run = normalize(run)
    plan = run.get("plan") or []
    done, counted, skipped = step_counts(run)
    act, wall = active_minutes(run, now=run.get("finished") or now), wall_minutes(run, now)
    parts = [f"{done} of {counted} steps" if plan else "no plan"]
    if skipped:
        parts.append(f"{skipped} skipped")
    for state, word in (("running", "now"), ("blocked", "blocked")):
        named = _steps_in(plan, state)
        if named:
            parts.append(f"{word} {named}")
    parts.append(f"{fmt_min(act)} active" + (f" ({fmt_min(wall)} wall)" if wall - act >= 5 else ""))
    if run.get("estimate"):
        parts[-1] += f" of ~{fmt_about(run.get('estimate_cal') or run['estimate'])}"
    if run.get("state") == "running":
        lo, hi = eta_range(run, now)
        parts.append(f"ETA {fmt_range(lo, hi)}")
    else:
        parts.append(run.get("state", ""))
    live = [v["state"] for v in (lane_states(run, now) if states is None else states).values()]
    for word in ("working", "stale"):
        n = live.count(word)
        if n:
            parts.append(f"{n} agent{'s' if n > 1 else ''} {word}")
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
                raise StepError("no progress run in this session: the tech lead starts one with dev progress start")
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
            # its pending decisions and waiting questions belong to it: closed, and kept with the reason in the run
            # that replaces it
            closed = _close_decisions(live, f"run replaced: {why.strip()[:240]}", now, history_run=run)
            closed_msgs = _close_messages(live, f"run replaced: {why.strip()[:240]}", now)
            run["replaced"] = {"goal": live["goal"], "started": live.get("started"), "ts": round(now),
                               "why": why.strip()[:300]}
            if closed:
                run["replaced"]["decisions"] = closed
            if closed_msgs:
                run["replaced"]["messages"] = closed_msgs
            _audit("progress.replace", live["goal"][:120], why.strip()[:300], project=run.get("project") or "")
            _audit_closed(run, closed)
            _audit_closed_messages(run, closed_msgs)
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


def _rechecked(run, s) -> bool:
    """A step flagged recheck that finishes now, after every reopened step it used is closed again, did its work on
    their new version: its flag clears. -> whether it cleared"""
    by_name = _by_name(run.get("plan") or [])
    if s.get("recheck") and all((by_name.get(n) or {}).get("state", "done") in DONE for n in s["recheck"]):
        s.pop("recheck")
        return True
    return False


def _finish_step(run, s, now, agent, sid):
    if s.get("state") == "done":
        return
    s.update(state="done", finished=now)
    _rechecked(run, s)
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
        raise StepError(f"only the tech lead {what}: ask them with dev crew ask \"…\" (your own steps: dev progress "
                        "add, dev progress step)")
    who = caller(cwd)
    c = who.get("caller")
    if c in ("main", "human"):
        return
    if c == "unverified":
        _audit("caller.unverified", what, f"{who.get('agent')} session {str(who.get('session'))[:40]}: devkit cannot "
               f"verify who calls yet (spec C4, S3)", "allowed")
        return
    here = f"{who.get('agent')}:{who.get('session')}"
    elsewhere = [f"{a}:{s}" for a, s in _ledger_sessions(sys.argv[1:]) if f"{a}:{s}" != here]
    _audit("caller.refused", what, f"caller {c}; session found by {who.get('how')}"
           + (f"; the open call is in {', '.join(x[:40] for x in elsewhere[:3])}" if elsewhere else ""), "deny",
           session=str(who.get("session"))[:40])
    if (c == "unknown" or not c) and elsewhere:
        raise StepError(f"only the tech lead {what}, and devkit took this command for session {here} (found by "
                        f"{who.get('how')}), whose call ledger holds no open call for it; the open call that typed it "
                        f"is in session {', '.join(elsewhere[:3])}, which devkit does not take: a live process other "
                        f"than this one holds it, it belongs to another repository or is older than 12 hours, or "
                        f"more than one session holds the call. If that is this "
                        f"conversation under a new session id (a compaction), its next prompt records this process "
                        f"on it: send one message, then run the command again")
    if c == "unknown" or not c:
        raise StepError(f"only the tech lead {what}, and no open call of this session typed this command: type the "
                        f"command itself, not through a script or another shell, as a command of its own (spec C4)")
    raise StepError(f"only the tech lead {what}: this command runs inside a crew member's call ({c[:16]}). Ask them "
                    f"with dev crew ask \"…\"")


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
    """Hand a plan step to a subagent: the next subagent launched with this description owns it, or a working lane
    of that member whose launch names the step and that holds no other step (_launch_takes; never a lane busy with
    another step, B-fy9639hfz5t, nor one whose member ended or went stale, working_agents), and its time is that
    subagent's time. `scope`: the files it owns (comma-separated globs); the guard keeps that subagent's edits inside
    them, and only a working member's scope can refuse it. The crew's RACI must make the member Responsible for
    the step's kind of work; `why` overrides it, recorded. `activity`: the step's kind of work, an id from dev crew
    raci, which then decides whatever language the step is written in. `allow`: the tier-1 actions (push, pr) the
    member may take in this step, each only with the developer's evidence for it (their grant, or a verified
    decision; spec C6); the step's `allow` lists those that had it."""
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
        globs, rewrites = _scope_globs(globs, run.get("project") or "", cwd)
        _tell_rewrites(run, s["name"], rewrites, now)
        _raci_assign(run, s, label, why, now, activity=activity, files=globs or None)
        owner = s.get("owner") or "main"
        if owner != "main" and s.get("state") != "done" and not _label_matches(label, (run.get("agents") or {}).get(owner)
                                                                                or {}):
            old = ((run.get("agents") or {}).get(owner) or {}).get("persona") or owner
            _log_change(run, s, s.get("state"), "pending", "main", f"reassigned from {old} to {label}", now)
            s.update(owner="main", state="pending")  # the other member's lane no longer owns it
        s["label"] = label
        s["assigned_at"] = round(now, 3)  # a step assigned after the developer's mid-turn message is their request
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
        live = working_agents(run, now, own=True)  # one that ended or went stale takes no step and holds no files
        if globs:
            for _, lane in live:
                if not _label_matches(label, lane):
                    clash = [g for g in globs for h in lane.get("scope") or [] if _globs_overlap(g, h)]
                    if clash:
                        from . import crew
                        raise StepError(f"{', '.join(clash)} overlaps the files "
                                        f"{crew.who(lane.get('persona') or lane.get('label'))} owns while working "
                                        f"({', '.join(lane.get('scope'))}). Give each member different files, or "
                                        f"wait until they finish.")
            s["scope"] = globs
        working = [k for k, v in live if _label_matches(label, v)]
        # its launch's lane first; else a working lane of the member with no other open step (a member resumed
        # with SendMessage keeps its first launch's words: B-fxriigsni7c, B-fyj09eek926)
        lane_id = None if deps_open(run, s) else next(
            (k for k in working if s.get("owner") == k or _launch_takes(run, k, s)),
            next((k for k in working if not _open_step_of(run, k, s)), None))
        if working and not lane_id and not deps_open(run, s) and s.get("state") not in DONE:
            from . import crew
            busy = run["agents"][working[0]]
            other = _open_step_of(run, working[0], s)
            _post(run, "devkit", "main", f"{crew.who(busy.get('persona') or label)} works on “{other['name']}” "
                  f"(one open step per lane): “{s['name']}” waits for a launch of its own (dev crew next), or for "
                  f"that step to end, and that lane's report cannot close it", now, from_name="devkit")
        if lane_id:
            _take_step(run, lane_id, s, now)
            if run["agents"][lane_id].get("binding") == "unbound":  # Morpheus confirms a lane with no launch record
                run["agents"][lane_id]["binding"] = "confirmed"
                _audit("crew.confirm", run["agents"][lane_id].get("persona") or label, f"Morpheus assigned "
                       f"“{s['name']}” to a lane without a launch record", project=run.get("project") or "",
                       agent_id=lane_id[:16])
        elif s.get("state") == "running" and (s.get("owner") or "main") == "main":
            s["state"] = "pending"  # waits for its subagent
        _advance_main(run, now)
        _save(run)
    return run


def _open_step_of(run: dict, aid: str, s: dict | None = None):
    """The step lane `aid` still works on other than `s` (not done, skipped or unverified: a step that ended
    unverified waits for the tech lead's evidence, not for the member), or None: a lane works one step at a time."""
    return next((x for x in run.get("plan") or [] if x is not s and x.get("owner") == aid
                 and x.get("state") not in DONE + ("unverified",)), None)


def _take_step(run: dict, aid: str, s: dict, now: float):
    """Lane `aid` takes step `s`: it owns it and runs it, its scope becomes the step's, and its member's step closes
    reach the plan."""
    s.update(owner=aid, state="running", started=s.get("started") or now)
    lane = run["agents"][aid]
    lane.pop("handed_over", None)  # Morpheus gives it a step of its own
    if s.get("scope"):
        lane["scope"] = s["scope"]
        _hand_scope(run, s, aid, now)


def _take_waiting_step(run: dict, aid: str, now: float, sess=None):
    """A member resumed (SendMessage) that holds no open step takes the one step assigned to it that waits for a
    launch (pending on the tech lead, its dependencies done), as a new launch would; with two or more waiting it
    takes none, and the tech lead's `dev progress assign` picks. Never another member's step, and none while a new
    launch of its own member is on its way (that launch takes the step). `sess`: the session record, for its
    launches. -> the step taken, or None."""
    lane = (run.get("agents") or {}).get(aid) or {}
    if not lane.get("persona") and not lane.get("label") or _open_step_of(run, aid):
        return None
    me = (lane.get("persona") or "").lower()
    if me and any(r.get("state") == "pending" and not r.get("agent_id") and now - (r.get("ts") or now) < LAUNCH_TTL
                  and (persona(r.get("type") or "", r.get("label") or "") or "").lower() == me
                  for r in ((sess or {}).get("launches") or {}).values() if isinstance(r, dict)):
        return None
    waiting = [x for x in run.get("plan") or [] if (x.get("owner") or "main") == "main" and x.get("state") == "pending"
               and x.get("label") and _label_matches(x["label"], lane) and not deps_open(run, x)]
    if len(waiting) != 1:
        return None
    s = waiting[0]
    _take_step(run, aid, s, now)
    _log_change(run, s, "pending", "running", "main", "its member was resumed and holds no other step", now)
    return s


def _launch_takes(run: dict, aid: str, s: dict) -> bool:
    """May this working lane take step `s` when Morpheus assigns it (B-fy9639hfz5t)? Only when its launch names the
    step, the way _bind_step reads a launch: the lane holds no other open step (one step per launch), and its
    description's task text names some of the step's words and none of its member's other waiting steps better. A
    step assigned while its member works on another one waits for a launch of its own."""
    lane = (run.get("agents") or {}).get(aid) or {}
    plan = run.get("plan") or []
    if any(x is not s and x.get("owner") == aid and x.get("state") not in DONE for x in plan):
        return False
    text = _task_text(lane.get("label") or "")
    mine = _name_score(text, s["name"])
    rivals = [x for x in plan if x is not s and x.get("state") not in DONE and x.get("label")
              and (x.get("owner") or "main") == "main" and _label_matches(x["label"], lane)]
    return bool(mine[1]) and all(mine >= _name_score(text, x["name"]) for x in rivals)


def _checkouts(base: str) -> list:
    """The real paths of every checkout of the repository at `base` (its main checkout and its worktrees) that is
    still there, deepest first, so a worktree inside the main checkout wins over it. git lists a worktree whose
    folder was deleted, marked prunable, until `git worktree prune`; it holds no files, so it is left out, and a glob
    into it is refused like one into any worktree that is not there. [] outside a repository: nothing to count from."""
    out = core.gitout(base, "worktree", "list", "--porcelain") if base and os.path.isdir(base) else ""
    tops = []
    for block in out.split("\n\n"):  # one block per checkout: "worktree <path>", then HEAD, branch, prunable, …
        lines = block.splitlines()
        path = next((ln[len("worktree "):] for ln in lines if ln.startswith("worktree ")), "")
        if path and os.path.isdir(path) and not any(ln.startswith("prunable") for ln in lines):
            tops.append(core.real(path))
    return sorted(tops, key=len, reverse=True)


def _scope_globs(globs: list, project: str, cwd: str | None) -> tuple:
    """Step scopes count from the run's repository: the Edit guard matches a member's file by its path relative to
    the checkout that holds it (hooks._scope_violation), so an absolute or ~ glob inside a checkout never matched
    (L-fzi4pkv6bbh). -> (globs, rewrites): such a glob in its repository-relative form, with "old → new" for the
    tech lead; absolute globs outside the repository (a scratch folder, the project's plans) as given. A glob is
    rewritten only when that is exact: its fixed folder (the part before the first wildcard) lies in one checkout.
    Raises StepError when it is not: the fixed folder holds checkouts (a wildcard or the folder spans several), or it
    is a managed worktree folder of the project with no checkout there (a removed worktree, a typo). A guess there
    could cover files the tech lead did not name, or keep a scope that never matches."""
    if not any(g.startswith(("/", "~")) for g in globs):
        return globs, []
    from . import guard
    base = guard.scope_base(project, cwd or os.getcwd())
    tops = _checkouts(base)
    managed = core.real(core.WT_ROOT / project) if project else ""
    fix = ("Give the files repository-relative instead, such as lib/** or tests/test_x.py: a repository-relative "
           f"scope matches them in every checkout of {project or 'the repository'}.")
    out, rewrites = [], []
    for g in globs:
        if not g.startswith(("/", "~")):
            out.append(g)
            continue
        ga = os.path.expanduser(g)
        parts = ga.rstrip("/").split("/")
        k = next((i for i, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
        fixed, rest = core.real("/".join(parts[:k]) or "/"), "/".join(parts[k:])
        top = next((t for t in tops if core.is_under(fixed, t)), "")
        if top:
            rel = os.path.relpath(fixed, top)
            new = "/".join(x for x in (rel if rel != "." else "", rest) if x) or "**"
            new += "/" if ga.endswith("/") and not rest and new != "**" else ""
            out.append(new)
            rewrites.append(f"{g} → {new}")
            continue
        held = [t for t in tops if core.is_under(t, fixed)]
        if held:
            raise StepError(f"--scope {g} holds {len(held)} checkout{'s' if len(held) > 1 else ''} of "
                            f"{project or 'the repository'} ({', '.join(core.tilde(t) for t in held[:3])}), so devkit "
                            f"cannot tell which files you mean. {fix}")
        if managed and core.is_under(fixed, managed):
            raise StepError(f"--scope {g} points into {core.tilde(managed)}, but no checkout of {project} is there "
                            f"(git worktree list). {fix}")
        out.append(g)
    return out, rewrites


def _tell_rewrites(run: dict, step: str, rewrites: list, now: float):
    """Tell the tech lead which globs of a step's scope _scope_globs kept repository-relative (assign, delegate)."""
    if rewrites:
        _post(run, "devkit", "main", f"the scope of “{step}” is kept repository-relative, the form the Edit guard "
              f"matches: {'; '.join(rewrites)}. It holds those files in every checkout of "
              f"{run.get('project') or 'the repository'}.", now, from_name="devkit")


def _globs_overlap(a: str, b: str) -> bool:
    """Could two path globs match the same file? (conservative: compares the fixed prefixes)"""
    import fnmatch
    pa, pb = re.split(r"[*?\[]", a, maxsplit=1)[0], re.split(r"[*?\[]", b, maxsplit=1)[0]
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    return pa.startswith(pb) or pb.startswith(pa)


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


# --------------------------------------------------------------------------- who holds a file (dev crew owner)
#
# A lane its run lists as running holds its files only while its member works (hotfix 26.11.6). The member ended when
# devkit or Claude Code saw it end, whichever session id the end came under: its lane ended in any run of this Mac, or
# Claude Code's own record of the agent (agent-<id>.meta.json, beside whichever session transcript it now sits) says
# finished, or its transcript ends with its interruption, and nothing was written after. It is stale when its session
# ended or its session's process is gone, or when it has been silent for progress.stale_agent_minutes. Neither holds a
# file. scope_holders decides for the edit check (hooks._crew_limits) and for `dev crew owner` alike.

END_GRACE = 5  # seconds: Claude Code writes an agent's finished record moments after the agent's last transcript line


def _lane_activity(lane: dict) -> float:
    """The last time a run saw this lane at work: its start or resume, its hooks (last_seen), its updates."""
    seen = [lane.get("started"), lane.get("resumed_at"), lane.get("recovered"), lane.get("last_seen")]
    seen += [u.get("ts") for u in lane.get("updates") or [] if isinstance(u, dict)]
    return max([float(t) for t in seen if isinstance(t, (int, float))] or [0.0])


def _known_transcripts(folder=None) -> list:
    """The main transcripts of the sessions devkit has a record of on this Mac (`folder`: the session records,
    SESSIONS by default): Claude Code keeps each agent's record in a folder beside the transcript of the session the
    conversation runs in now, which a compaction can change."""
    out = []
    folder = Path(folder) if folder else SESSIONS
    for f in sorted(folder.glob("*.json")) if folder.exists() else []:
        t = (core.read_json(f, {}) or {}).get("transcript")
        if isinstance(t, str) and t.endswith(".jsonl") and t not in out:
            out.append(t)
    return out


def agent_facts(runs: list, transcripts=None):
    """-> `facts(agent id)`: {"ended": when devkit or Claude Code last saw the agent end (None when neither did),
    "how": what saw it, "active": its last activity anywhere}. Read across every run in `runs` (a lane of that agent
    that ended there, and the activity each run records) and Claude Code's record of the agent beside every
    transcript devkit knows (approvals.meta_reader: finished, interrupted, its own transcript's last write)."""
    from . import approvals
    paths = _known_transcripts() if transcripts is None else transcripts
    readers = [r for r in (approvals.meta_reader(t) for t in paths) if r]
    seen = {}

    def facts(aid: str) -> dict:
        if aid in seen:
            return seen[aid]
        ended, how, active = None, "", 0.0
        for run in runs:
            ln = (run.get("agents") or {}).get(aid) if isinstance(run, dict) else None
            if not isinstance(ln, dict):
                continue
            active = max(active, _lane_activity(ln))
            if ln.get("state") in ENDED_LANE + ("unverified",) and isinstance(ln.get("finished"), (int, float)):
                if ended is None or ln["finished"] > ended:
                    ended, how = float(ln["finished"]), (f"its lane ended {ln['state']} in "
                                                         f"{run.get('agent') or 'claude'}:{str(run.get('session'))[:12]}")
        for read in readers:
            m = read(aid) or {}
            if m.get("transcript_mtime"):
                active = max(active, float(m["transcript_mtime"]))
            t = float(m["mtime"]) if m.get("finished") and m.get("mtime") else m.get("stopped")
            if t and (ended is None or t > ended):
                ended, how = float(t), ("Claude Code recorded it finished" if m.get("finished") else
                                        "its transcript ends with its interruption")
        seen[aid] = {"ended": ended, "how": how, "active": active}
        return seen[aid]
    return facts


def _session_gone(run: dict, alive=None) -> str:
    """Why the session that holds this run no longer works, or '': its record is gone (SessionEnd deletes it), it
    ended, or its process is gone (approvals.pid_alive). Unknown counts as working: a run that names no session, a
    record without a pid, a process devkit cannot check."""
    from . import approvals
    if not run.get("session"):
        return ""
    rec = core.read_json(session_path(run.get("agent") or "claude", str(run["session"])), None)
    if not isinstance(rec, dict):
        return "its session ended (its record is gone)"
    if rec.get("ended"):
        return "its session ended"
    pid = rec.get("pid")
    if pid and (alive or approvals.pid_alive)(pid, rec.get("pid_start")) is False:
        return "its session's process is gone"
    return ""


def _clock(ts: float, now: float) -> str:
    """'23:41 (4 min ago)'."""
    ago = max(0.0, now - ts) / 60
    return time.strftime("%H:%M", time.localtime(ts)) + (" (just now)" if ago < 1 else f" ({fmt_min(ago)} ago)")


def lane_ended(run: dict, lane: dict, f: dict):
    """When and how the member of a lane its run lists as running ended -> {"at", "how"}, or None while it may still
    work: devkit or Claude Code saw it end (`f`, agent_facts of its agent) at or after its last activity anywhere.
    A lane with no activity recorded counts from its run's start."""
    last = max(_lane_activity(lane), f["active"]) or float(run.get("started") or 0)
    if f["ended"] is not None and f["ended"] + END_GRACE >= last:
        return {"at": f["ended"], "how": f["how"]}
    return None


def lane_state(run: dict, aid: str, lane: dict, facts, now: float | None = None, alive=None, own: bool = False,
               gone: str | None = None) -> dict:
    """How a lane its run lists as running really is -> {"state": "working" | "stale" | "ended", "last": its last
    activity, "why"}. ended: devkit or Claude Code saw it end after its last activity (lane_ended); stale: its
    session ended or its process is gone (not asked for the caller's own session; `gone`, when given, is
    _session_gone already read), or it has been silent for progress.stale_agent_minutes (a lane with no activity
    recorded, in a run with no start, is unknown, never silent); working: anything else."""
    from . import approvals
    now = now or time.time()
    f = facts(aid)
    last = max(_lane_activity(lane), f["active"]) or float(run.get("started") or 0)  # none recorded: since its run began
    end = lane_ended(run, lane, f)
    if end:
        return {"state": "ended", "last": last, "why": f"{end['how']} at {_clock(end['at'], now)}"}
    gone = "" if own else (_session_gone(run, alive) if gone is None else gone)
    if gone:
        return {"state": "stale", "last": last, "why": gone}
    quiet = approvals.limits()["stale_minutes"]
    if last and now - last > quiet * 60:  # no activity known at all, not even its run's start: unknown, not silent
        return {"state": "stale", "last": last, "why": f"silent for {fmt_min((now - last) / 60)} "
                                                       f"(progress.stale_agent_minutes is {quiet:g})"}
    return {"state": "working", "last": last, "why": ""}


def lane_states(run: dict, now: float | None = None, facts=None, alive=None, own: bool = False) -> dict:
    """{lane id: lane_state} for every lane `run` lists as running: whether its member still works, went stale or
    ended, as scope_holders decides for the files it holds. Everything that counts or lists the crew at work
    (headline, crew_summary, slot_use, brief, working_agents, the continuity brief, the dashboard) reads it, so they
    agree with `dev crew owner`. `facts`: agent_facts over this Mac's live runs and `run`, read when None. `own`: the
    run is the caller's own, whose session works since the caller runs in it, so its session is not asked (as
    scope_holders reads its own run). Unknown counts as working."""
    lanes = {k: v for k, v in ((run or {}).get("agents") or {}).items()
             if isinstance(v, dict) and v.get("state") == "running"}
    if not lanes:
        return {}
    now = now or time.time()
    facts = agent_facts(active_runs() + [run]) if facts is None else facts
    gone = "" if own else _session_gone(run, alive)
    return {aid: lane_state(run, aid, lane, facts, now, alive, gone=gone) for aid, lane in lanes.items()}


def working_agents(run, now: float | None = None, states=None, own: bool = False) -> list:
    """The lanes `run` lists as running whose member still works (lane_states; `own`: the caller's own run) ->
    [(id, lane)], as running_agents lists them; a member that ended or went stale is not busy and holds no slot."""
    states = lane_states(run, now, own=own) if states is None else states
    return [(k, v) for k, v in running_agents(run) if (states.get(k) or {}).get("state", "working") == "working"]


def lane_gone(aid: str, lane, states: dict):
    """("ended" | "stale", why) when the member of lane `aid` no longer works, as `dev crew owner` reads it, else
    None: its run closed the lane (done, stopped, lost, unverified), or lists it as running while its member ended or
    went stale (`states`, lane_states of its run). A lane the run does not know, or a state not read, counts as
    working (unknown)."""
    if not isinstance(lane, dict) or not lane.get("state"):
        return None
    if lane["state"] != "running":
        return "ended", f"its lane is {lane['state']}"
    lv = states.get(aid) or {}
    return (lv["state"], lv.get("why") or "") if lv.get("state") in ("ended", "stale") else None


def _member_lanes(run, ref: str, now: float | None = None) -> tuple:
    """The lanes of the member `ref` names in the caller's own run -> (working, gone): [(id, lane)] whose member still
    works, and [(id, lane, lane_gone)] whose member ended or went stale."""
    hits = [(k, v) for k, v in (run.get("agents") or {}).items() if isinstance(v, dict) and _label_matches(ref, v)]
    states = lane_states(run, now, own=True) if any(v.get("state") == "running" for _, v in hits) else {}
    marks = [(k, v, lane_gone(k, v, states)) for k, v in hits]
    return [(k, v) for k, v, g in marks if not g], [(k, v, g) for k, v, g in marks if g]


def _holder(run: dict, aid: str, lane: dict, globs, facts, now: float, alive, own: bool) -> dict:
    from . import crew
    m = crew._for_lane(lane)
    who = crew.who(m) if m else (lane.get("persona") or lane.get("label") or "a crew member")
    st = next((s for s in run.get("plan") or [] if isinstance(s, dict) and s.get("owner") == aid
               and s.get("state") not in DONE), None)
    lv = lane_state(run, aid, lane, facts, now, alive, own)
    to = m["id"] if m else shlex.quote(lane.get("label") or who)
    do = {"working": f"wait until they finish, or ask: dev crew ask --to {to} \"…\"",
          "stale": "nothing: a stale lane holds nothing",
          "ended": "nothing: they ended, so they hold nothing"}[lv["state"]]
    return {"who": who, "member": m["id"] if m else None, "lane": aid, "agent": run.get("agent") or "claude",
            "session": str(run.get("session") or ""), "run": run.get("goal") or "",
            "step": (st or {}).get("name") or "", "scope": sorted(g for g in globs if isinstance(g, str)),
            "own": own, "to": to, "do": do, **lv}


def scope_holders(path, agent: str, sid: str, cwd: str | None = None, me: str = "main", now: float | None = None,
                  alive=None) -> list:
    """Every crew lane its run lists as running whose files hold `path`, in the order the edit check reads them:
    other sessions' live runs first (guard.scope_owner_across, given one lane at a time: the lane's scope and its
    open steps' scopes, in the checkout that holds the path), then this session's own run (the lane's scope, counted
    from the run's repository; `me`, the caller's own lane, excluded). Each holder: who, member, lane, agent,
    session, run, step, scope, own, to, do, and its lane_state (state, last, why). Only a working holder holds the
    file: the edit check refuses on the first one (scope_holder) and `dev crew owner` exits 1 for it."""
    from . import guard
    now = now or time.time()
    cwd = str(cwd or os.getcwd())
    ap = os.path.normpath(os.path.join(cwd, str(path)))
    runs = [r for r in active_runs() if isinstance(r, dict)]
    mine = load_run(agent, sid)
    every = runs + ([mine] if isinstance(mine, dict) and not any(
        str(r.get("session")) == str(sid) and r.get("agent") == agent for r in runs) else [])
    facts, tops, out = agent_facts(every), {}, []

    def top(run):
        key = (run.get("agent"), str(run.get("session")), run.get("project"))
        if key not in tops:
            tops[key] = guard._run_checkout(run)
        return tops[key]
    for run in runs:
        if run.get("state") == "done" or str(run.get("session") or "") == str(sid):
            continue
        for aid, lane in (run.get("agents") or {}).items():
            if not isinstance(lane, dict) or lane.get("state") != "running":
                continue
            if guard.scope_owner_across(ap, runs=[dict(run, agents={aid: lane})], me_session=sid, checkout_of=top):
                globs = set(lane.get("scope") or []) | {g for s in run.get("plan") or [] if isinstance(s, dict)
                                                        and s.get("owner") == aid and s.get("state") not in DONE
                                                        for g in s.get("scope") or []}
                out.append(_holder(run, aid, lane, globs, facts, now, alive, own=False))
    if isinstance(mine, dict) and mine.get("state") != "done":
        run = normalize(mine)
        expire_stale(run, now)
        rel = guard.scope_rel(path, cwd, guard.scope_base(run.get("project"), cwd))
        for aid, lane in (run.get("agents") or {}).items():
            if aid != me and isinstance(lane, dict) and lane.get("state") == "running" and \
                    _in_globs(rel, lane.get("scope") or []):
                out.append(_holder(run, aid, lane, lane.get("scope") or [], facts, now, alive, own=True))
    return out


def scope_holder(path, agent: str, sid: str, cwd: str | None = None, me: str = "main", now: float | None = None):
    """The first working holder of `path` (scope_holders), or None: what the edit check refuses on."""
    return next((h for h in scope_holders(path, agent, sid, cwd, me, now) if h["state"] == "working"), None)


def shown_path(path, cwd: str | None = None) -> str:
    """A path as the developer reads it: relative to `cwd` when it lies inside it, else as given."""
    cwd = str(cwd or os.getcwd())
    ap = os.path.normpath(os.path.join(cwd, str(path)))
    rel = os.path.relpath(ap, cwd)
    return str(path) if rel.startswith("..") else rel


def holder_refusal(h: dict, path, cwd: str | None = None, now: float | None = None) -> str:
    """The edit check's refusal for a working holder: who, their scope, where (another session's run), their last
    activity, how to ask them, and the command that checks again."""
    shown = shown_path(path, cwd)
    where = "" if h.get("own") else (f", in another session of this repository (run «{str(h.get('run'))[:60]}», "
                                     f"session {h['session'][:12]})")
    return (f"{shown} belongs to {h['who']} while they work (scope {', '.join(h['scope'])}){where}; last activity "
            f"{_clock(h['last'], now or time.time())}. Ask them (dev crew ask --to {h['to']} \"…\") or wait until "
            f"they finish, and check again with dev crew owner {shlex.quote(shown)}; DEVKIT_SCOPE_OVERRIDE=1 only "
            f"for an emergency.")


def owner_text(rows: list, now: float | None = None) -> str:
    """`dev crew owner`: each path, free or held, and every lane whose scope covers it with its state and what to do."""
    now = now or time.time()
    lines = []
    for r in rows:
        lines.append(f"{r['path']}  " + (f"{ui.OK} free" if r["free"] else f"{ui.WARN} held"))
        for h in r["holders"]:
            why = f": {h['why']}" if h.get("why") else ""
            lines.append(f"  {h['who']} · {h['state']}{why} · last activity {_clock(h['last'], now)}")
            where = "this session's run" if h.get("own") else f"session {h['session'][:12]}"
            lines.append(ui.dim(f"    run «{str(h['run'])[:60]}» · {where}" + (f" · step {h['step']}" if h['step'] else "")
                                + f" · scope {', '.join(h['scope'])}"))
            lines.append(f"    {h['do']}")
    return "\n".join(lines)


def close_quiet_lanes(run: dict, why: str, now: float | None = None, alive=None) -> list:
    """The lanes this run lists as running whose member ended or went stale (lane_state) close, as the reconciler
    closes them (approvals.apply): an interrupted one stopped (approvals.stop_moves), one that finished with no stop
    in this run unverified with its running steps, a stale one lost with its running steps back with Morpheus. Each
    transition's reason in the run's history starts with `why`. Changes the run in place. -> the transitions made"""
    from . import approvals
    now = now or time.time()
    runs = [r for r in active_runs() if isinstance(r, dict)
            and (r.get("agent"), str(r.get("session"))) != (run.get("agent"), str(run.get("session")))] + [run]
    facts, moves = agent_facts(runs), []
    for aid, lane in sorted((run.get("agents") or {}).items()):
        if not isinstance(lane, dict) or lane.get("state") != "running":
            continue
        lv = lane_state(run, aid, lane, facts, now, alive)
        if lv["state"] == "working":
            continue
        mine = [s for s in run.get("plan") or [] if s.get("owner") == aid and s.get("state") == "running"]
        reason = f"{why}: {lv['why']}"
        if lv["state"] == "ended" and "interruption" in lv["why"]:
            moves += approvals.stop_moves(run, aid, reason, at=lv["last"])
        elif lv["state"] == "ended":
            reason += "; no report reached this run, so no evidence"
            moves += [approvals._lane_move(aid, "running", "unverified", reason, at=facts(aid)["ended"])]
            moves += [approvals._step_move(s, "unverified", aid, reason) for s in mine]
        else:
            reason += "; the step waits for a new owner"
            moves += [approvals._lane_move(aid, "running", "lost", reason, at=lv["last"])]
            moves += [approvals._step_move(s, "pending", "main", reason) for s in mine]
    return approvals.apply(run, moves, now)


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


_TECH_LEAD = re.compile(r"(?:the\s+)?(?:main|lead|morpheus|tech[\s-]?lead)(?:\s*\((?:morpheus|(?:the\s+)?tech[\s-]?lead)"
                        r"\))?", re.IGNORECASE)


def _addr(run, ref: str, now: float | None = None):
    """'Morpheus', 'the tech lead', 'Tech lead (Morpheus)', 'lead', 'main', '' -> 'main'; a lane id -> that lane; a
    crew name or label -> the lane of that member that still works (_member_lanes, as `dev crew owner` reads it), or
    the name, for their next launch: a member with no lane, or whose lanes ended or went stale, where nobody reads."""
    r = " ".join((ref or "").split())
    if not r or _TECH_LEAD.fullmatch(r):
        return "main"
    agents = run.get("agents") or {}
    if r in agents:
        return r
    working, _ = _member_lanes(run, r, now)
    if working:  # the member's working lane, bound to its launch, before an older one
        return max(working, key=lambda kv: (kv[1].get("binding", "exact") == "exact", kv[1].get("started") or 0))[0]
    return "name:" + r


def _waits_notice(run, m: dict, frm: str, now: float | None = None) -> str:
    """For a message addressed to a member's next launch (_addr) because the lanes they had ended or went stale: what
    tells its sender so, and what to do (the tech lead launches them again; a member asks the tech lead to), or ''."""
    _, gone = _member_lanes(run, m["to"][5:], now)
    if not gone:
        return ""
    from . import crew
    _, lane, (state, why) = max(gone, key=lambda x: x[1].get("started") or 0)
    mem = crew._for_lane(lane)
    who = crew.who(mem) if mem else crew.who(_lane_name(lane))
    again = (f"launch them again (dev crew brief {mem['id'] if mem else shlex.quote(m['to'][5:])} \"…\")"
             if frm == "main" else "ask the tech lead to launch them again (dev crew ask \"…\")")
    return (f"{who} {'went stale' if state == 'stale' else 'ended'}: {why}. {m['id']} waits for their next launch: "
            f"{again}, or ask someone else (dev crew ask --to <member> \"…\").")


def _reply_to(run, aid: str, now: float | None = None, own: bool = True) -> str:
    """Where a reply to lane `aid` goes (the answer to its question, the result of its decision): that lane while its
    member works, as `dev crew owner` reads it (lane_gone over lane_states of `run`, the caller's own unless `own` is
    False; a lane the run does not know counts as working); else the member's name, which crew_ask addresses (_addr)
    to their working lane or to their next launch, telling the sender (_waits_notice). A lane with no name to address
    keeps its id."""
    lane = (run.get("agents") or {}).get(aid)
    if not isinstance(lane, dict) or not (lane.get("persona") or lane.get("label")):
        return aid
    states = lane_states(run, now, own=own) if lane.get("state") == "running" else {}
    return _lane_name(lane) if lane_gone(aid, lane, states) else aid


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
    with its next tool call (PostToolUse) or prompt. `kind`: "message" (a question, or a notice when CI watch or devkit
    sends it), "notice" (it tells, and waits for no answer) or "answer". The audit trail keeps its kind and length,
    never its text (crew messages quote the developer's requests). A message longer than MESSAGE_MAX is refused whole,
    never cut: the reader would get half a text without knowing it."""
    now = now or time.time()
    size = len((text or "").strip())
    if size > MESSAGE_MAX:
        raise ValueError(f"the message is {size} characters and a crew message holds at most {MESSAGE_MAX}: send it "
                         "in parts, or write the long text to a file in your scratch folder and send its path")
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    return _ask_into(agent, sid, text, to, from_agent or "main", now, kind)


def _ask_into(agent: str, sid: str, text: str, to, frm: str, now: float, kind: str = "message") -> dict:
    """crew_ask into the run of session `agent`:`sid`, from `frm` (a lane of that run, or "main")."""
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise ValueError("no progress run in this session: the tech lead starts one with dev progress start")
        run = normalize(run)
        m = _post(run, frm, _addr(run, to or "", now), text, now, kind=kind if kind in ("notice", "answer") else "")
        told = _waits_notice(run, m, frm, now) if m["to"].startswith("name:") and frm not in NOTICE_SENDERS else ""
        if told:  # never silently: the sender learns with its next tool call that nobody reads it now
            _post(run, "devkit", frm, told, now, from_name="devkit", kind="notice")
        _save(run, touch_session=False)
    _audit("crew.message", f"{m['from_name']} → {m['to_name']}", "", project=run.get("project") or "", id=m["id"],
           type=kind, count=len(m["text"]))
    return m


def _next_id(items: list, prefix: str) -> str:
    """The id after the highest one kept: the lists keep their last 200 entries, so a count would repeat ids."""
    nums = [int(x["id"][len(prefix):]) for x in items if str(x.get("id") or "")[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


NOTICE_SENDERS = ("ci", "devkit")  # CI watch and devkit itself tell; they never ask
MESSAGE_MAX = 1500  # characters in one crew message; crew_ask refuses a longer one, devkit's own notices stay under it


def _post(run, frm: str, to: str, text: str, now: float, from_name: str = "", kind: str = "") -> dict:
    """Add a message to the run (the caller saves it). `kind`: question, notice or answer; without it, a notice from
    CI watch or devkit, else a question. -> the message."""
    msgs = run.setdefault("messages", [])
    m = {"id": _next_id(msgs, "M"), "ts": round(now), "from": frm, "to": to, "text": text.strip()[:MESSAGE_MAX],
         "delivered": False, "kind": kind or ("notice" if frm in NOTICE_SENDERS else "question")}
    m["from_name"], m["to_name"] = from_name or _name_of(run, frm), _name_of(run, to)
    msgs.append(m)
    run["messages"] = msgs[-200:]
    return m


def _asks(m: dict) -> bool:
    """Does the message ask something? A message written before messages had a kind asks unless CI watch or devkit
    sent it or it answers another."""
    if m.get("kind"):
        return m["kind"] == "question"
    return not (m.get("from") in NOTICE_SENDERS or m.get("from_name") == "devkit" or m.get("reply_to"))


def _unanswered(run) -> list:
    """The messages of `run` that ask a question nobody has answered, and that did not close with their run."""
    msgs = [m for m in (run or {}).get("messages") or [] if isinstance(m, dict)]
    replied = {m.get("reply_to") for m in msgs if m.get("reply_to")}
    return [m for m in msgs if _asks(m) and not m.get("answered") and not m.get("closed") and m.get("id") not in replied]


def _open_questions(run) -> list:
    """The unanswered questions of `run` that someone still waits on: never a moot one (is_moot_message)."""
    return [m for m in _unanswered(run) if not is_moot_message(run, m)]


def is_moot_message(run, m: dict) -> bool:
    """A question to the tech lead that nobody waits on any more: the lane that asked it has ended (done, stopped,
    lost, or closed unverified) and owns no running or blocked step. Not answered and not closed, it is moot: never
    waiting, never waking, shown as moot. A question of the tech lead's, or of a lane the run does not know, never
    is."""
    if not isinstance(m, dict) or m.get("to") != "main" or not _asks(m) or m.get("answered") or m.get("closed"):
        return False
    lane = ((run or {}).get("agents") or {}).get(m.get("from") or "")
    if m.get("from") in (None, "", "main") or not isinstance(lane, dict) or \
            lane.get("state") not in ENDED_LANE + ("unverified",):
        return False
    return not any(isinstance(s, dict) and s.get("owner") == m.get("from") and s.get("state") in ("running", "blocked")
                   for s in (run or {}).get("plan") or [])


def moot_messages(run) -> list:
    """The unanswered questions to the tech lead nobody waits on any more (is_moot_message)."""
    return [m for m in _unanswered(run) if is_moot_message(run, m)]


def moot_message_reason(run, m: dict) -> str:
    """Why a moot question needs nobody, in one clause: "Backend engineer (Tank) ended (done) and holds no open
    step"."""
    from . import crew
    lane = ((run or {}).get("agents") or {}).get(m.get("from") or "") or {}
    return f"{crew.who(m.get('from_name') or _name_of(run, m.get('from') or ''))} ended ({lane.get('state') or '?'}) " \
           f"and holds no open step"


def waiting_messages(run, to: str = "main") -> list:
    """The messages to `to` (the tech lead by default) that ask a question nobody has answered yet: delivered or
    not, they wait. A notice (CI watch, a step or launch notice, a decision's result) never waits, nor does any
    question of a run that is done: its questions closed with it (_close_messages), runs saved before that included."""
    if (run or {}).get("state") == "done":
        return []
    return [m for m in _open_questions(run) if m.get("to") == to]


def _close_messages(run, reason: str, now: float) -> list:
    """A run ends or is replaced: each question in it that still waits for an answer, to the tech lead or to a
    member, closes with the reason, and takes no answer any more. -> copies of the closed messages, for the audit
    after the save."""
    closed = []
    for m in _open_questions(run):
        m.update(closed=round(now), reason=reason[:300])
        closed.append(dict(m))
    return closed


def _audit_closed_messages(run, closed: list):
    for m in closed:  # never the text: crew messages quote the developer's requests
        _audit("crew.message-closed", m["id"], m.get("reason") or "", "closed", project=(run or {}).get("project") or "",
               why=m.get("reason") or "")


# --------------------------------------------------------------------------- ids across runs
# Message and decision ids (M3, D2) are numbered per run, so two runs of one project both hold an M3. Where a run's
# message or decision is shown to anyone but that run's own session and members, it carries the run's session:
# M3@9448c4d0 (shown_id), which dev crew answer resolves in that run (run_of_prefix).

SESSION_PREFIX = 8  # characters of a session id a qualified id names at least
_QUALIFIED = re.compile(r"^([A-Za-z]\d+)@([0-9A-Za-z][0-9A-Za-z._-]*)$")


def _run_sessions() -> list:
    """(agent, session id) of every run file on this Mac (<agent>-<session id>.json)."""
    out = []
    for f in RUNS.glob("*.json") if RUNS.exists() else []:
        agent, _, sid = f.stem.partition("-")
        if agent and sid:
            out.append((agent, sid))
    return out


_PREFIXES: dict = {}  # (runs folder, its mtime, session id) -> prefix: a page lists many ids of one run


def session_prefix(sid: str) -> str:
    """The shortest prefix of session id `sid`, 8 characters at least (the whole id when shorter), that no other run
    of this Mac shares: what a qualified id names its run by."""
    sid = str(sid or "")
    try:  # the folder's mtime changes when a run file is added or removed, the only change a prefix depends on
        key = (str(RUNS), RUNS.stat().st_mtime_ns, sid)
    except OSError:
        key = None
    if key in _PREFIXES:
        return _PREFIXES[key]
    others = {s.lower() for _, s in _run_sessions() if s != sid}
    n = min(SESSION_PREFIX, len(sid))
    while n < len(sid) and any(o.startswith(sid[:n].lower()) for o in others):
        n += 1
    if key:
        if len(_PREFIXES) > 512:
            _PREFIXES.clear()
        _PREFIXES[key] = sid[:n]
    return sid[:n]


def shown_id(run, item_id: str, caller=None) -> str:
    """A message or decision id of `run` as shown to `caller`, the (agent, session id) that reads it: bare in the
    caller's own run, whose main session and members answer by it; qualified with the run's session everywhere else
    (M3@9448c4d0), the dashboard and a terminal with no session included (`caller` None). A run that names no
    session keeps the bare id."""
    sid = (run or {}).get("session")
    if not item_id or not sid:
        return item_id
    if caller and tuple(caller[:2]) == (run.get("agent") or "claude", sid):
        return item_id
    return f"{item_id}@{session_prefix(sid)}"


def split_id(ref) -> tuple:
    """'M3@9448c4d0' -> ('M3', '9448c4d0'); 'M3' -> ('M3', '')."""
    ref = str(ref or "").strip()
    mt = _QUALIFIED.match(ref)
    return (mt.group(1), mt.group(2)) if mt else (ref, "")


def run_of_prefix(prefix: str) -> tuple:
    """The (agent, session id) of the one run on this Mac whose session id starts with `prefix`. Raises ValueError
    for a prefix shorter than 8 characters (unless it is a whole session id), one no run has, and one that more than
    one run shares."""
    p = str(prefix or "").lower()
    hits = [(a, s) for a, s in _run_sessions() if s.lower().startswith(p)]
    exact = [(a, s) for a, s in hits if s.lower() == p]
    if len(exact) == 1:
        return exact[0]
    if len(p) < SESSION_PREFIX:
        raise ValueError(f"@{prefix} is too short: name the run by at least {SESSION_PREFIX} characters of its "
                         f"session id, as dev status prints it")
    if not hits:
        raise ValueError(f"no run of a session {prefix}… on this Mac: dev status lists the questions that wait")
    if len(hits) > 1:
        raise ValueError(f"@{prefix} names {len(hits)} runs ({', '.join(f'{a}:{s[:12]}' for a, s in hits)}): give "
                         f"more characters of the session id")
    return hits[0]


def _waiting_elsewhere(msg_id: str, own: tuple, cwd: str | None = None) -> list:
    """The qualified ids of the questions to the tech lead with id `msg_id` that wait in this Mac's other active runs
    of the caller's project (of every project when it is unknown). Read before any lock: _project runs git."""
    mine = load_run(*own) or {}
    project = mine.get("project") or (core.read_json(session_path(*own), {}) or {}).get("project") or _project(cwd)
    out = []
    for r in active_runs():
        if (r.get("agent") or "claude", r.get("session")) == own or (project and r.get("project") != project):
            continue
        if any(str(m.get("id") or "").lower() == msg_id.lower() for m in waiting_messages(r)):
            out.append(shown_id(r, msg_id, own))
    return out


def _closed_why(run, m: dict) -> str:
    """Why a question takes no answer any more ('' while it may): it closed with its run, or its run is done."""
    if m.get("closed"):
        return m.get("reason") or "its run ended"
    return "its run ended" if (run or {}).get("state") == "done" and _asks(m) else ""


def crew_answer(msg_id: str, text: str, from_agent: str | None = None, cwd: str | None = None, now: float | None = None) -> dict:
    """Answer crew message `msg_id` (`dev crew answer`): mark it answered and post the answer, in the run that holds
    it, to the lane that asked while its member works (_reply_to of that run). A qualified id (M3@9448c4d0) names
    that run by its session; a bare one is the caller's own run's. The tech lead's or the developer's bare id whose
    message in the caller's run is missing, or is no question waiting for an answer, while another active run of the
    project holds one that waits by that id, is refused with the qualified ids to use: it never answers a message
    other than the one shown. A crew member answers the messages of its own run only."""
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    own = (agent, sid)
    bare, prefix = split_id(msg_id)
    elsewhere = []
    if prefix:
        target = run_of_prefix(prefix)
        if target != own and from_agent:
            raise ValueError(f"{msg_id} is a message of another session's run: a crew member answers the messages of "
                             f"its own run only. Ask the tech lead: dev crew ask \"…\"")
    else:  # a member's bare id is its own run's: its inbox shows only those
        target, elsewhere = own, ([] if from_agent else _waiting_elsewhere(bare, own, cwd))
    with _tx(*target):
        run = normalize(load_run(*target) or {})
        q = next((m for m in run.get("messages") or [] if str(m.get("id") or "").lower() == bare.lower()), None)
        if elsewhere and (not q or q not in waiting_messages(run, q.get("to") or "main")):
            what = f"{bare} in this session's run is no question waiting for an answer" if q else \
                f"this session's run has no message {bare}"
            raise ValueError(f"{what}, and another run waits on its own {bare}. Answer the one dev status lists by its "
                             "qualified id: " + " · ".join(f'dev crew answer {x} "…"' for x in elsewhere))
        if not q:
            raise ValueError(f"no message {msg_id}")
        if _closed_why(run, q):
            raise ValueError(f"{msg_id} was closed ({_closed_why(run, q)}): it takes no answer. Ask again with dev "
                             f"crew ask if it still matters")
        q["answered"] = True
        _save(run, touch_session=False)
    # the asking lane while its member works, else the member by name; another session's run is not the caller's own
    to = _reply_to(run, q["from"], now, own=target == own)
    a = _ask_into(target[0], target[1], text, to, from_agent or "main", now or time.time(), kind="answer")
    with _tx(*target):
        run = normalize(load_run(*target) or {})
        for m in run.get("messages") or []:
            if m["id"] == a["id"]:
                m["reply_to"] = q["id"]
                if to == q["from"]:  # by the lane's id, even one the run no longer knows (_addr would read a name)
                    m["to"], m["to_name"] = q["from"], q["from_name"]
        _save(run, touch_session=False)
    return a


def crew_inbox(from_agent: str | None = None, cwd: str | None = None, mark: bool = True) -> list:
    """Messages for the caller (the main session, or the subagent whose lane this is) not delivered yet."""
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    run = load_run(agent, sid)
    return _take_messages(agent, sid, from_agent or "main", mark) if run else []


def answered_by_sendmessage(agent: str, data: dict, now: float | None = None) -> list:
    """PostToolUse of a SendMessage from the main session: its text answers the open questions that the member it went
    to (a lane id, or a name its lanes answer to) asked the tech lead. Each is marked answered via SendMessage, the
    text is kept as the answer (delivered already: SendMessage carried it), and the audit trail records
    crew.message-answered with the channel. -> the ids answered"""
    inp = data.get("tool_input") or {}
    to, text = str(inp.get("to") or "").strip(), str(inp.get("message") or "").strip()
    resp = data.get("tool_response")
    sid = data.get("session_id") or ""
    if data.get("agent_id") or not sid or not to or not text or to == "main" or \
            (isinstance(resp, dict) and resp.get("success") is False):
        return []
    now = now or time.time()
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not isinstance(run, dict) or run.get("state") not in ("running", "blocked"):
            return []
        run = normalize(run)
        agents = run.get("agents") or {}
        lanes = [to] if to in agents else [k for k, v in agents.items() if isinstance(v, dict) and
                                           _label_matches(to, v)]
        asked = [m for m in _unanswered(run) if m.get("to") == "main" and m.get("from") in lanes]
        if not asked:
            return []
        for q in asked:
            q.update(answered=True, via="SendMessage")
        a = _post(run, "main", asked[-1]["from"], text, now, kind="answer")
        a.update(reply_to=asked[-1]["id"], delivered=True, via="SendMessage", to_name=asked[-1].get("from_name") or
                 a.get("to_name"))
        _save(run, touch_session=False)
    for q in asked:  # never the text: crew messages quote the developer's requests
        _audit("crew.message-answered", q["id"], f"answered via SendMessage to {q.get('from_name') or q['from']}",
               "answered", project=run.get("project") or "", id=a["id"], channel="SendMessage", count=len(text))
    return [q["id"] for q in asked]


def _take_messages(agent, sid, me: str, mark=True, select=None, limit=None) -> list:
    """The messages for `me` not delivered yet; with mark, they are marked delivered (once each, under the lock).
    `select(run, message)`: only the messages it keeps; `limit`: at most that many, oldest first (watch.mail). Every
    hook call asks: the lock is taken only when there is something to take."""
    def mine(run):
        run = normalize(run)
        lane = (run.get("agents") or {}).get(me) or {}
        new = [m for m in run.get("messages") or [] if not m.get("delivered") and (
            m.get("to") == me or ((m.get("to") or "").startswith("name:") and me != "main"
                                  and _label_matches(m["to"][5:], lane))) and (select is None or select(run, m))]
        return run, new[:limit] if limit is not None else new
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
NEVER_PREAPPROVED = ("publish", "deploy", "unknown", "developer-only", "backup",
                     "embargo")  # the developer grants each one; checked again when a pre-approval is used
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
    action and target (any target with `any_target`), else a verified, unused decision naming the action and a
    target (that target unless `any_target`; a decision without one backs nothing, M277) whose answer approves
    the action (approves_action). -> the evidence to keep
    with it, with the `target` pattern and the `repo` it covers (a step grant opens only those), or None."""
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
        return {**{k: got.get(k) for k in ("channel", "ts", "covers", "transcript_uuid", "excerpt")},
                "target": str(got.get("covers") or "").partition(" ")[2], "repo": got.get("repo") or ""}
    def covers(x):
        return any_target or _norm(x["target"]) == _norm(target)
    d = next((x for x in _decision_candidates(run, action, covers) if approves_action(x)), None)
    if not d:
        _audit_refused(run, action, target, covers, f"{agent}:{sid}")
    if d:
        d["used"] = now  # the caller saves the run
        return {"channel": "decision", "ts": d.get("decided"), "covers": d["id"],
                **{k: (d.get("evidence") or {}).get(k) for k in ("transcript_uuid", "excerpt")},
                "target": d["target"], "repo": run.get("project") or ""}
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


DECISION_QUESTION_MAX = 300  # characters: one plain question the developer reads at a glance
_LETTERED = re.compile(r"([A-Za-z])\s*:\s*(\S.*)")


def _cli_quote(text) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", str(text)) + '"'


def decision_shape(question: str, options="", recommend: str = "", irreversible: str = "", target: str = "",
                   approves: str = "", performs: str = "") -> tuple:
    """`dev crew ask --decision` in the shape the developer can be asked nicely: a question under 300 characters,
    2 to 4 options, each "<letter>: <what it means>", and a recommendation that is one of them. An irreversible
    approval asked without options (what the guard tells an agent to run) is a yes-or-no question, "A: yes | B: no",
    A recommended unless the recommendation is no. The labels devkit offers for the options (ask_labels) fit
    together in an audit record (under audit.VALUE_CUT characters), so `dev audit verify` can rebuild the decision
    from it (the Code reviewer's finding 2). -> (options, the recommended option's letter). Raises StepError with the
    corrected command when the shape is wrong."""
    q = " ".join(str(question or "").split())
    opts = [o.strip() for o in (options if isinstance(options, list) else str(options or "").split("|")) if o.strip()]
    rec = (recommend or "").strip()
    if not opts and irreversible:
        opts = ["A: yes", "B: no"]
        rec = rec or "A"
    parsed = [_LETTERED.fullmatch(o) for o in opts]
    letters = [mt.group(1).upper() for mt in parsed if mt]
    lettered = all(parsed) and len(set(letters)) == len(letters)
    fixed = opts if lettered else [
        f"{chr(65 + i)}: " + (mt.group(2).strip() if mt else f"<what {o} means>" if len(o) == 1 else o)
        for i, (mt, o) in enumerate(zip(parsed, opts))]
    hit = option_of({"options": fixed}, rec)
    letter = _letter(hit) if hit else ""
    problems = []
    unnamed = bool(irreversible and target and not _target_named(target, q))
    if unnamed:
        problems.append(f"the question names what {irreversible} acts on, {target}, as a word of its own: the "
                        f"developer reads it there")
    if len(q) > DECISION_QUESTION_MAX:
        problems.append(f"the question has {len(q)} characters: keep it under {DECISION_QUESTION_MAX}, one plain "
                        f"question, and put the details in the options or your report")
    if not 2 <= len(opts) <= 4:
        problems.append(f"a decision has 2 to 4 options, it has {len(opts)}"
                        + (": merge or drop some" if len(opts) > 4 else ": add the alternative"))
    elif not lettered:
        problems.append("each option opens with its own letter, “A: <what it means>”")
    if not letter:
        problems.append(f"the recommendation “{rec}” is not one of the options" if rec else
                        "name the option you recommend by its letter")
    each = None
    if lettered and letter and 2 <= len(opts) <= 4:
        from . import audit
        act = (irreversible or "").strip().lower()
        shape = {"id": "D0", "options": opts, "recommend": letter, "irreversible": act,
                 "target": (target or "").strip()}
        perf = _named_option(opts, performs, prefix=True) if act and performs else None
        if perf:
            shape["performs"] = perf
        size = len(" | ".join(ask_labels(shape)))
        if size >= audit.VALUE_CUT:
            each = (audit.VALUE_CUT - 1 - (size - sum(len(o) for o in opts))) // len(opts)
            problems.append(f"as devkit offers them, the options are {size} characters together, and an audit record "
                            f"keeps {audit.VALUE_CUT} characters of them: keep them to at most {each} characters "
                            f"each, and put the details in your report")
    if not problems:
        return opts, letter
    shown = (fixed + [f"{chr(65 + i)}: <another choice>" for i in range(len(fixed), 2)])[:4]
    if each is not None:
        shown = [f"{_letter(o)}: <at most {each} characters>" for o in shown]
    shown_q = f"{q} ({target})" if unnamed else q
    cmd = (f"dev crew ask --decision "
           f"{_cli_quote(shown_q if len(shown_q) <= DECISION_QUESTION_MAX else '<the question>')} "
           f"--options {_cli_quote(' | '.join(shown))} --recommend {letter or 'A'}"
           + (f" --irreversible {irreversible}" if irreversible else "") + (f" --target {target}" if target else "")
           + (f" --performs {_cli_quote(performs)}" if performs else "")
           + (f" --approves {_cli_quote(approves)}" if approves else ""))
    raise StepError("; ".join(problems) + f". Ask it like this (the developer sees the recommended option first): "
                    f"{cmd}")


ASK_PER_CALL = 4  # the question tool asks at most four questions per call


def _ask_context(run: dict, d: dict) -> str:
    """One line for the developer: who asks, what it blocks, what an approval lets the crew do."""
    from . import crew
    blocks = d["blocks"] if "blocks" in d else [s["name"] for s in run.get("plan") or [] if s.get("waiting") == d["id"]]
    out = f"{crew.who(d.get('from_name') or 'main')} asks" + (
        f"; it blocks {', '.join(f'“{b}”' for b in blocks)}" if blocks else "") + "."
    if d.get("irreversible"):
        out += f" It leads to an irreversible {d['irreversible']}" + (f" of {d['target']}" if d.get("target") else "") \
               + "."
    if d.get("approves"):
        out += f" Your answer approves or sends back {', '.join(f'“{s}”' for s in d['approves'])}."
    return out


def _recommended(d: dict):
    return _named_option(d.get("options"), d.get("recommend"), prefix=True)


def ask_calls(run: dict, ids=None) -> dict:
    """The question tool calls that ask the developer decisions (`ids`, else every pending one, oldest first):
    {"calls": [{"questions": [...]}], "typed": [ids]}. Each question is "D<n> · <question> <who asks, what it
    blocks>" with the header "D<n>" and devkit's labels (ask_labels): the recommended option first, marked
    "(Recommended)", and an irreversible decision's performing option with what it runs. Reversible decisions go
    together, at most four per call; an irreversible one goes in a call of its own, since another question in its
    call could redefine its options (_not_devkits_words); the calls go in the order of their first decision.
    A decision without 2 to 4 options goes to `typed`: the developer types its approve line. Raises StepError for
    an id the run lacks or one the developer already decided."""
    if ids:
        picked = []
        for i in ids:
            d = _decision(run, i)
            if not d:
                raise StepError(f"no decision {i} in this session's run: dev crew decisions")
            if d.get("state") == "decided" and d.get("verified"):
                raise StepError(f"{d['id']} was already decided by the developer: “{d.get('answer')}”")
            if _not_open(d):
                raise StepError(_not_open(d))
            picked += [d] if d not in picked else []
        picked.sort(key=lambda x: int(x["id"][1:]))
    else:
        picked = pending_decisions(run)
    together, alone, typed = [], [], []
    for d in picked:
        opts = d.get("options") or []
        if not 2 <= len(opts) <= 4:
            typed.append(d["id"])
            continue
        q = {"question": f"{ask_text(d)} {_ask_context(run, d)}", "header": d["id"], "multiSelect": False,
             "options": [{"label": label, "description": ""} for label in ask_labels(d)]}
        (alone if d.get("irreversible") else together).append(q)
    calls = [together[i:i + ASK_PER_CALL] for i in range(0, len(together), ASK_PER_CALL)] + [[q] for q in alone]
    calls.sort(key=lambda c: int(c[0]["header"][1:]))
    return {"calls": [{"questions": c} for c in calls], "typed": typed}


def crew_decision(question: str, options=None, recommend: str = "", irreversible: str = "", from_agent: str | None = None,
                  cwd: str | None = None, now: float | None = None, target: str = "", approves: str = "",
                  performs: str = "") -> dict:
    """A decision point. -> the decision record. In auto mode a reversible decision is taken at once with the
    recommendation; in guided mode, or for an irreversible action not pre-approved, it waits for the developer: the
    asking member's lane waits and its step is blocked until Morpheus records the answer (crew_decide). `target`:
    what the irreversible action acts on (a branch, a tag, a PR number): a verified decision opens that action on
    that target once (spec C6), and only when the developer chose the option that performs it: `performs`, else the
    one option that reads yes or approve, else the recommended one (approves_action)."""
    now = now or time.time()
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise StepError("no progress run in this session: the tech lead starts one with dev progress start")
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
        performs = (performs or "").strip()
        if performs and not (act and opts):
            raise StepError("--performs goes with --irreversible and --options: it names the option that performs "
                            "the action")
        perf = ""
        if act and opts:
            perf = _named_option(opts, performs, prefix=True) if performs else _default_performs(opts, rec)
            if perf and performs and _refuses(perf):  # an explicit "B: no" would let a "no" open the action
                raise StepError(f"--performs “{performs}” names “{perf}”, which reads as not doing it: "
                                f"{' | '.join(opts)}. Name the option that performs {act} with --performs; only the "
                                f"developer's choice of it opens {act}")
            if not perf:
                raise StepError((f"--performs “{performs}” is not one of the options" if performs else
                                 f"no option clearly performs {act}: none reads yes or approve, and the recommended "
                                 f"one is missing or reads as not doing it")
                                + f": {' | '.join(opts)}. Name the option that performs {act} with --performs; only "
                                  f"the developer's choice of it opens {act}")
        decs = run.setdefault("decisions", [])
        d = {"id": _next_id(decs, "D"), "ts": round(now), "question": question.strip()[:800], "options": opts,
             "recommend": rec, "irreversible": act, "target": target, "from": from_agent or "main",
             "from_name": _name_of(run, from_agent or "main"), "state": "pending", "answer": "", "by": ""}
        if perf:
            d["performs"] = perf  # the option whose choice opens the action (approves_action)
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
                    lane["waiting"] = d["id"]
                for s in run.get("plan") or []:
                    if s.get("owner") == from_agent and s.get("state") == "running":
                        _log_change(run, s, "running", "blocked", from_agent, f"waiting for the developer: {d['id']}", now)
                        s["state"], s["waiting"] = "blocked", d["id"]
                        # as asked: deciding unblocks them, and devkit's context line still names them (asked_how)
                        d.setdefault("blocks", []).append(s["name"])
            if not auto and not rec and mode_of(run) == "auto":
                d["note"] = "auto mode, but no recommendation was given: the developer decides"
        decs.append(d)
        run["decisions"] = decs[-200:]
        _save(run, touch_session=False)
    _audit("crew.decision", d["id"], d["question"][:300], d["state"], project=run.get("project") or "",
           by=d.get("by") or "", answer=d.get("answer") or "", irreversible=d.get("irreversible") or "",
           mode=mode_of(run), asked_by=d["from_name"], options=d.get("options") or [],
           labels=" | ".join(ask_labels(d)), session=f"{agent}:{sid}")  # verify reruns the matcher against them
    if d["state"] == "pending":
        _notify_idle(agent, sid, d)
    return d


def _notify_idle(agent: str, sid: str, d: dict):
    """A decision for the developer while the main session is idle (its turn ended): a desktop notification too, so
    the developer hears of it when nobody looks at the terminal. In a turn, the tech lead asks it."""
    try:
        sess = state.read(session_path(agent, sid)) or {}
    except Exception:
        return
    if sess and not sess.get("turn_started"):
        from . import crew
        notify(f"Decision {d['id']} waits for you", f"{crew.who(d.get('from_name') or '')}: {d.get('question') or ''}")


def crew_decide(decision_id: str, answer: str, cwd: str | None = None, now: float | None = None) -> dict:
    """Record the developer's answer (Morpheus runs it after asking): the waiting member's step runs again, and a
    message with the answer waits for that member (delivered when Morpheus resumes it, or to their next launch when
    their lane ended: _reply_to). Who decided is computed, never typed (spec C5): the developer, verified, when an
    unused piece of their evidence (E1, E2) given after the decision was shown covers it; otherwise Morpheus,
    unverified, and an irreversible decision stays pending."""
    now = now or time.time()
    _main_only(None, "records the developer's answers", cwd)
    agent, sid = current_session(cwd)
    decision_id = _own_decision_id(decision_id, agent, sid, "answered")
    answer = answer.strip()[:800]

    def find(run):
        d = _decision(run, decision_id)
        if not d:
            raise StepError(f"no decision {decision_id}")
        if _not_open(d):
            raise StepError(_not_open(d))
        if d["state"] == "decided" and d.get("by") != "auto" and d.get("verified") is not False:
            raise StepError(f"{d['id']} was already decided by {d['by']}: “{d['answer']}”")
        return d
    run0 = normalize(load_run(agent, sid) or {})
    ev = _decision_evidence(agent, sid, run0, find(run0), answer, now)  # the transcript is read outside the lock
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        d = find(run)
        if d["state"] == "decided" and d.get("by") != "auto" and not ev:
            raise StepError(f"{d['id']} was already recorded by the tech lead without the developer's evidence: “"
                            f"{d['answer']}”. Only their own answer replaces it. {_how_to_ask(d)}")
        if not ev and d.get("irreversible"):
            d.update(proposed=answer, by="morpheus", verified=False, proposed_ts=round(now))
            _save(run, touch_session=False)
            _audit("crew.decided", d["id"], answer[:300], "blocked", project=run.get("project") or "",
                   by="morpheus", verified=False, irreversible=d["irreversible"], options=d.get("options") or [])
            raise StepError(f"{d['id']} leads to an irreversible action ({d['irreversible']}), and devkit has no "
                            f"answer of the developer to it after it was shown: it stays pending. "
                            f"{_how_to_ask(d)}. Then run this again.")
        prev = d.get("answer")
        d.update(state="decided", answer=answer, by="developer" if ev else "morpheus", verified=bool(ev),
                 decided=round(now))
        d.pop("proposed", None)
        d.pop("proposed_ts", None)
        if ev:
            d["evidence"] = {k: ev.get(k) for k in ("channel", "ts", "covers", "transcript_uuid", "excerpt")}
            d["chosen"] = _chosen(d, ev)  # the developer's own choice; it alone opens an action (approves_action)
        if prev and prev != d["answer"]:
            d["overrides"] = prev  # the developer changed an automatic decision
        _unblock(run, d, now)
        _save(run, touch_session=False)
    _audit("crew.decided", d["id"], d["answer"][:300], project=run.get("project") or "", by=d["by"],
           overrides=d.get("overrides") or "", verified=d["verified"], channel=(ev or {}).get("channel") or "",
           transcript_uuid=(ev or {}).get("transcript_uuid") or "", session=f"{agent}:{sid}",
           options=d.get("options") or [])
    if d["from"] != "main":  # the asking lane while its member works, else the member's working lane or next launch
        crew_ask(f"Decision {d['id']} — {d['question'][:120]} → {d['answer']}", to=_reply_to(run, d["from"], now),
                 cwd=cwd, now=now, kind="notice")
    return d


def _own_decision_id(decision_id: str, agent: str, sid: str, verb: str) -> str:
    """The bare id of a decision of the caller's own run (agent, sid): a qualified id (D2@9448c4d0) of that session
    loses its session; one of another session's run is refused, since a decision is `verb` (answered, withdrawn) in
    the session that owns it, where the developer's evidence lives (its transcript)."""
    bare, prefix = split_id(decision_id)
    if not prefix:
        return bare
    try:
        target = run_of_prefix(prefix)
    except ValueError as e:
        raise StepError(str(e))
    if target != (agent, sid):
        raise StepError(f"{decision_id} is a decision of another session's run ({target[0]}:{target[1][:12]}): it is "
                        f"{verb} in the session that owns it, where the developer's evidence lives (its transcript). "
                        f"There the developer types approve {bare}, and its tech lead runs dev crew decide {bare} "
                        f"\"…\" or dev crew withdraw {bare} --why \"…\"")
    return bare


def _release(run, d: dict, why: str, now: float) -> list:
    """The steps blocked on decision `d`, once it is decided or withdrawn (`why` says which): each runs again while
    its member works, and waits for a new launch (pending, back with Morpheus, its scope kept, announced ready) when
    its member ended or went stale, as `dev crew owner` reads it (lane_gone over lane_states of the caller's own run;
    a lane the run does not know counts as working). A lane never runs again because of an answer (its wait never
    changed its state); a step that moved on meanwhile stays as it is. -> the steps sent back for a new launch"""
    from . import crew
    agents, states, back = run.get("agents") or {}, None, []
    for s in run.get("plan") or []:
        if s.get("waiting") != d["id"]:
            continue
        s.pop("waiting", None)
        if s.get("state") != "blocked":
            continue
        owner = s.get("owner") or "main"
        lane = agents.get(owner)
        if states is None and isinstance(lane, dict) and lane.get("state") == "running":
            states = lane_states(run, now, own=True)
        g = lane_gone(owner, lane, states or {}) if owner != "main" else None
        if g:
            _log_change(run, s, "blocked", "pending", "main", f"{why}; its owner {crew.who(_lane_name(lane))} had "
                        f"{'gone stale' if g[0] == 'stale' else 'ended'}: {g[1]}, so the step waits for a new launch",
                        now)
            s.update(state="pending", owner="main")
            if lane["state"] != "running":
                s["lost_from"] = owner  # a lost lane seen again takes it back (approvals.reconcile)
            else:  # never from a lane its run lists as running: approvals.reconcile counts that lane working through
                s.pop("lost_from", None)  # finish_grace, and would hand the step back to it at the next hook
            back.append(s)
        else:
            _log_change(run, s, "blocked", "running", "main", why, now)
            s["state"] = "running"
    for lane in agents.values():
        if lane.get("waiting") == d["id"]:
            lane.pop("waiting", None)
            lane.pop("state_before_wait", None)  # kept by runs saved before an answer stopped reviving lanes
    return back


def _unblock(run, d: dict, now: float):
    """A decided decision unblocks what waits on it (_release)."""
    _release(run, d, f"{d['id']} decided: {d['answer'][:80]}", now)


def crew_withdraw(decision_id: str, why: str, from_agent: str | None = None, cwd: str | None = None,
                  now: float | None = None) -> dict:
    """The tech lead withdraws a pending decision (`dev crew withdraw <id> --why`): state withdrawn, with the reason
    in the run's history and the audit trail. A step blocked on it runs again, or waits for a new launch when its
    member ended or went stale (_release); the asking member is told to go on without an answer when its run still
    lists its lane as running, or when the step it blocked waits for a new launch: its lane while it works, else its
    working lane or next launch (_reply_to). Never the developer's answer: no evidence, no `chosen`, so it opens no
    irreversible action (approves_action and use_grant read decided ones only), and it takes no answer later."""
    _main_only(from_agent, "withdraws decisions", cwd)
    now = now or time.time()
    why = " ".join(str(why or "").split())
    if not why:
        raise StepError("say why: dev crew withdraw <id> --why \"…\" (it goes into the run's history and the audit)")
    agent, sid = current_session(cwd)
    decision_id = _own_decision_id(decision_id, agent, sid, "withdrawn")
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        d = _decision(run, decision_id)
        if not d:
            raise StepError(f"no decision {decision_id} in this session's run: dev crew decisions")
        if d.get("state") != "pending":
            raise StepError(_not_open(d) or f"{d['id']} is already decided ({d.get('answer')}): only a pending "
                                            f"decision is withdrawn")
        d.update(state="withdrawn", withdrawn=round(now), why=why[:300])
        _decision_history(run, d, "withdrawn", why, now)
        back = _release(run, d, f"{d['id']} withdrawn: {why[:80]}", now)
        lane = (run.get("agents") or {}).get(d.get("from") or "main")
        tell = d.get("from") not in (None, "", "main") and isinstance(lane, dict) \
            and (lane.get("state") == "running" or bool(back))  # it may wait, or the launch that takes its step will
        _save(run, touch_session=False)
    _audit("crew.withdrawn", d["id"], why[:300], "withdrawn", project=run.get("project") or "", by="morpheus",
           why=why[:300], was="pending", session=f"{agent}:{sid}")
    if tell:  # the asking lane while its member works, else the member's working lane or next launch
        crew_ask(f"Decision {d['id']} was withdrawn by the tech lead: {why}. Go on without it; it is not the "
                 f"developer's answer.", to=_reply_to(run, d["from"], now), cwd=cwd, now=now, kind="notice")
    return d


def _how_to_ask(d: dict) -> str:
    """How the tech lead gets the developer's own answer to a decision, for refusals."""
    typed = f"they type a line of its own: approve {d['id']}" + (": <option>" if d.get("options") else "")
    if d.get("irreversible") and d.get("options"):
        return (f"Ask it with the question tool in devkit's exact call, as dev crew decisions --ask {d['id']} prints "
                f"it: one question, “{ask_text(d)}”, its own header {d['id']}, and labels only, "
                f"{' | '.join(ask_labels(d))}, word for word (a reworded question or label, a description or "
                f"preview, or another question in the call counts for nothing), or {typed}")
    if d.get("irreversible"):
        return f"Show it in your final message: {typed}"
    return (f"Ask it with the question tool (dev crew decisions --ask {d['id']} prints “{ask_text(d)}”; reword it "
            f"as you like, keeping “{d['id']} ·” up front and the option letters), or {typed}")


ENDED_LANE = ("done", "stopped", "lost")  # a lost lane that is seen again runs again, and its decisions with it


def _awaiting(run) -> list:
    """Every decision in state pending, moot ones included: the developer's evidence is matched against these, so an
    answer to a moot decision still counts and their latest word on it still voids an earlier yes."""
    return [d for d in (run or {}).get("decisions") or [] if isinstance(d, dict) and d.get("state") == "pending"]


def is_moot(run, d: dict) -> bool:
    """A pending decision nobody waits for: the crew lane that asked it has ended (done, stopped, lost, or closed
    unverified by the sweep) and no open step is blocked on it. The tech lead's own decisions, and those of a lane the run does not know, never are."""
    if not isinstance(d, dict) or d.get("state") != "pending":
        return False
    lane = ((run or {}).get("agents") or {}).get(d.get("from") or "main")
    if d.get("from") in (None, "", "main") or not isinstance(lane, dict) or \
            lane.get("state") not in ENDED_LANE + ("unverified",):
        return False
    return not any(isinstance(s, dict) and s.get("waiting") == d.get("id") and s.get("state") == "blocked"
                   for s in (run or {}).get("plan") or [])


def pending_decisions(run) -> list:
    """The decisions that need the developer: pending and not moot. Closed, withdrawn and decided ones never do."""
    return [d for d in _awaiting(run) if not is_moot(run, d)]


def moot_decisions(run) -> list:
    return [d for d in _awaiting(run) if is_moot(run, d)]


def decision_state(run, d: dict) -> str:
    """A decision's state as shown: "moot" for a pending one nobody waits for, else its recorded state."""
    return "moot" if is_moot(run, d) else str(d.get("state") or "")


def _moot_reason(run, d: dict) -> str:
    from . import crew
    return f"{crew.who(d.get('from_name') or _name_of(run, d.get('from') or 'main'))} ended, and no step waits on " \
           f"{d.get('id')}"


def moot_text(run, d: dict) -> str:
    """Why a moot decision needs nobody, in one line: "Moot: Backend engineer (Tank) ended, and no step waits on
    D1."."""
    return f"Moot: {_moot_reason(run, d)}."


def _decision_history(run, d: dict, to: str, why: str, now: float, was: str = "pending"):
    run.setdefault("history", []).append({"ts": round(now), "step": f"(decision {d['id']})", "from": was, "to": to,
                                          "by": "main", "why": (why or "")[:300]})
    run["history"] = run["history"][-200:]


def _close_decisions(run, reason: str, now: float, history_run=None) -> list:
    """A run ends or is replaced: each of its pending decisions (moot ones too) becomes state closed with the
    reason, and a line in the history of `history_run` (the run itself, or the run that replaces it). -> copies of
    the closed decisions, for the audit after the save."""
    closed = []
    for d in _awaiting(run):
        d.update(state="closed", closed=round(now), reason=reason[:300])
        _decision_history(history_run if history_run is not None else run, d, "closed", reason, now)
        closed.append(dict(d))
    return closed


def _audit_closed(run, closed: list):
    for d in closed:
        _audit("crew.decision-closed", d["id"], d.get("reason") or "", "closed", project=(run or {}).get("project") or "",
               why=d.get("reason") or "", was="pending")


def _not_open(d: dict) -> str:
    """Why a closed or withdrawn decision takes no answer and no question any more; '' for any other."""
    if d.get("state") == "withdrawn":
        return f"{d['id']} was withdrawn by the tech lead ({d.get('why') or 'no reason'}): it takes no answer. Ask a " \
               f"new decision if it matters again"
    if d.get("state") == "closed":
        return f"{d['id']} was closed ({d.get('reason') or 'its run ended'}): it takes no answer. Ask a new decision " \
               f"if it matters again"
    return ""


def _ci(agent, sid, mark: bool = True) -> list:
    """The CI results waiting in the session record of a session without a run (ciwatch.take_events), each taken
    once: the CI source of watch.mail. [] when the record cannot be read."""
    try:
        from . import ciwatch
        return ciwatch.take_events(agent, sid, mark=mark)
    except Exception:
        return []


def _mail(agent, sid, me: str) -> str:
    """watch.mail: crew messages, decisions, ready steps, CI results and notices new for `me`, each told once."""
    from . import watch
    return watch.mail(agent, sid, me)


def _told(d: dict) -> bool:
    return bool(d.get("told") or d.get("surfaced"))  # `surfaced`: runs written before spec C5


def take_decisions(agent: str, sid: str, mark: bool = True) -> tuple:
    """The decisions waiting for the developer that the main session was not told yet -> (run, [decision]); with
    mark, each is marked told in the same locked write, so no later hook or wait tells it again."""
    run = load_run(agent, sid)
    if not run or not any(not _told(d) for d in pending_decisions(run)):
        return run, []  # the usual case, without the lock
    if not mark:
        return run, [dict(d) for d in pending_decisions(normalize(run)) if not _told(d)]
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        new = [d for d in pending_decisions(run) if not _told(d)]
        if not new:
            return run, []
        for d in new:
            d["told"] = round(time.time())  # delivered to Morpheus; `shown` is when the developer saw it
            d.pop("surfaced", None)
        _save(run, touch_session=False)
    return run, [dict(d) for d in new]


def decisions_context(agent: str, sid: str, taken: list | None = None) -> str:
    """For the main session: decisions waiting for the developer, newest first, once per decision. `taken`: a list
    that receives the decisions told (watch.mail renders them one per line for a wait)."""
    run, new = take_decisions(agent, sid)
    if taken is not None:
        taken.extend(new)
    return decisions_text(run, new) if new else ""


def decisions_text(run: dict, new: list) -> str:
    """The main session's context for decisions that wait for the developer: how to ask them, then one line each."""
    from . import crew
    lines = ["devkit · decisions waiting for the developer (" + mode_of(run) + " mode). Ask them with your "
             "question tool (AskUserQuestion) in the calls `dev crew decisions --ask` prints: a plain question with "
             "its context, “D<n> ·” up front, the option letters, the recommended option first. Reword a reversible "
             "one freely; send an irreversible one as devkit's exact call, word for word: one question, its own "
             "header, labels only, or the answer counts for nothing. Or show "
             "them in your final message and let the developer type `approve D<n>[: <option>]`. Then record the "
             "answer with `dev crew decide <id> \"<answer>\"` (devkit records who decided from that evidence) and "
             "resume the member who asked (SendMessage / send_input):"]
    for d in new:
        opts = (" Options: " + " | ".join(d["options"])) if d["options"] else ""
        lines.append(f"- {d['id']} from {crew.who(d['from_name'])}: “{ask_text(d)}”{opts}"
                     + (f" Recommended: {d['recommend']}." if d.get("recommend") else "")
                     + (f" Irreversible: {d['irreversible']}." if d.get("irreversible") else "")
                     + (f" Only “{performing_option(d)}” opens it." if performing_option(d) else ""))
    return "\n".join(lines)


def waiting_context(agent: str, sid: str, since, now: float) -> str:
    """UserPromptSubmit, when the developer is back: the decisions that wait for them, were not shown to them since
    their last prompt (`since`) and are not answered in this one, in one line for the tech lead to ask them
    together. Once per batch: again only when a new decision joins the ones already listed."""
    run = load_run(agent, sid)
    pend = pending_decisions(run)
    if not pend:
        return ""  # the usual case, without reading the session
    sess = state.read(session_path(agent, sid)) or {}
    latest = {_norm(e.get("covers")): e for e in sess.get("human") or [] if isinstance(e, dict) and _live(e, now, run)}
    answered = {c for c, e in latest.items() if not e.get("void")}  # a void as the latest word: ask it again
    waiting = [d for d in pend if _norm(d["id"]) not in answered and not (since and (d.get("shown") or 0) >= since)]
    if not waiting or all(d.get("reminded") for d in waiting):
        return ""
    ids = [d["id"] for d in waiting]
    k = max(1, len(ask_calls(run, ids)["calls"]))  # an irreversible one goes in a call of its own; all pending here
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        for d in run.get("decisions") or []:
            if d["id"] in ids:
                d["reminded"] = round(now)
        _save(run, touch_session=False)
    n = len(ids)
    return (f"devkit · {n} decision{'s wait' if n > 1 else ' waits'} for you ({', '.join(ids)}): ask "
            f"{'them' if n > 1 else 'it'} now in "
            + ("one question call; `dev crew decisions --ask` prints it." if k == 1 else
               f"{k} question calls, one after the other; `dev crew decisions --ask` prints them."))


def messages_context(agent: str, sid: str, me: str, select=None, limit=None, taken: list | None = None) -> str:
    """The hook text that delivers the pending messages of `me` (the main session or a crew lane), each once.
    `select` and `limit` as _take_messages; `taken`: a list that receives the messages delivered (watch.mail)."""
    msgs = _take_messages(agent, sid, me, select=select, limit=limit)
    if taken is not None:
        taken.extend(msgs)
    return messages_text(msgs, normalize(load_run(agent, sid) or {}) if msgs and me == "main" else None)


def messages_text(msgs: list, run=None) -> str:
    """The hook text for crew messages taken for their recipient: how to answer, then one line each; with the run, a
    question nobody waits on any more says it is moot, and why."""
    if not msgs:
        return ""
    from . import crew
    lines = ["devkit · crew messages for you (answer with `dev crew answer <id> \"…\"`):"]
    for m in msgs:
        moot = run is not None and is_moot_message(run, m)
        lines.append(f"- {m['id']} from {crew.who(m.get('from_name'))}: {m['text']}"
                     + (f" (answer to {m['reply_to']})" if m.get("reply_to") else "")
                     + (f" (moot: {moot_message_reason(run, m)})" if moot else ""))
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
QUESTION_TOOLS = ("AskUserQuestion",)  # Codex's request_user_input joins when its payload is known (spec F0, S3):
# until then a Codex answer is shown in the final message and typed back as `approve D<n>: <option>`
GRANT_CLASSES = ("push", "pr", "push-destructive", "pr-write", "merge", "release", "publish", "deploy", "api-write",
                 "external-message", "delete-data", "migrate-production", "spend",
                 "unknown",  # C6's classes, IRREVERSIBLE, and a command known only when it runs (cmd-<hash>, RT1/RT2)
                 "developer-only", "backup", "embargo")  # B2: the developer's own decisions (guard.DEVELOPER_GRANTS)
HEADLESS_SURFACES = ("SDK", "claude -p", "codex exec")  # nobody at the keyboard: DEVKIT_PREAPPROVE only
TRANSCRIPT_DAYS = 30  # Claude Code's default cleanupPeriodDays: older evidence may have lost its transcript
# `approve D3`, or `approve D3@9448c4d0` as a decision listed outside its own session shows it (shown_id): groups are
# the decision, the session prefix it names, and the answer
_APPROVE_LINE = re.compile(r"(?i)^approve\s+(D\d+)(?:@([0-9a-z][0-9a-z._-]*))?\s*(?::\s*(\S.*?))?\s*$")


def _names_session(prefix: str, sid: str) -> bool:
    """Does the session prefix of a qualified approve line name session `sid`? At least 8 characters of it, or all."""
    p, s = str(prefix or "").lower(), str(sid or "").lower()
    return len(p) >= min(SESSION_PREFIX, len(s)) and bool(s) and s.startswith(p)
_GRANT_LINE = re.compile(r"(?i)^grant\s+([a-z][a-z-]*)\s+(\S+)\s*$")
_PLAIN_YES = re.compile(r"(?i)^\s*yes\s*[.!]?\s*$")
_RELAYED = re.compile(r"^\s*(?:\[[^\]\n]{0,120}\bsent to (?:Claude|Codex)\b[^\]\n]*\]|\[SYSTEM NOTIFICATION|"
                      r"<task-notification|<system-reminder|<agent-message|<command-name>|<local-command)",
                      re.IGNORECASE | re.MULTILINE)
_DECISION_REF = re.compile(r"\bD(\d+)\b")
_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)
_ASKED_ID = re.compile(r"\s*(D\d+)\s*[·:]", re.IGNORECASE)  # a question that opens with "D4 ·" or "D4:"
_HEADER_ID = re.compile(r"D\d+", re.IGNORECASE)  # a question whose header is "D4"
_LABEL_LETTER = re.compile(r"\s*([A-Za-z])\s*(?:[:.)·–—-]|$)")  # a label that opens with an option's letter: "A: …"


def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def ask_text(d: dict) -> str:
    """The decision as devkit words it for the developer: "D<n> · <question>". The tech lead may reword it and add
    context; the "D<n> ·" up front (or the header D<n>) binds the answer to it (asked_decision). An irreversible
    decision's question says, on its one line, which answer runs which action on which target: "D1 · Release now?
    (choosing “A: release” runs release v26.11.4)"; without options, "(answering “yes” runs …)"."""
    text = f"{d['id']} · {d['question']}"
    act = d.get("irreversible")
    if not act:
        return text
    runs = " ".join(f"{act} {d.get('target') or ''}".split())
    if d.get("options"):
        perf = performing_option(d)
        return f"{text} (choosing “{perf}” runs {runs})" if perf else text
    return f"{text} (answering “yes” runs {runs})"


def _letter(option: str) -> str:
    """"B" for "B: reject"; "" for an option without a one-letter label."""
    head, sep, _ = str(option).partition(":")
    return head.strip().upper() if sep and len(head.strip()) == 1 and head.strip().isalpha() else ""


def option_of(d: dict, text):
    """The decision's option an answer names: the option, with or without the question tool's "(Recommended)", its
    letter ("B" for "B: reject") or its words without the letter ("reject"); None when none or several do."""
    return _named_option(d.get("options"), text)


def asked_decision(q: dict):
    """The decision a question of the question tool asks: its question opens with "D<n> ·" (or "D<n>:"), or its
    header is "D<n>". None when it names none, or the two name different decisions."""
    a = _ASKED_ID.match(str(q.get("question") or ""))
    h = _HEADER_ID.fullmatch(str(q.get("header") or "").strip())
    ids = ({a.group(1).upper()} if a else set()) | ({h.group(0).upper()} if h else set())
    return ids.pop() if len(ids) == 1 else None


def _offered(label, offered) -> bool:
    return _norm(_RECOMMENDED.sub("", str(label))) in {_norm(_RECOMMENDED.sub("", str(o))) for o in offered or []}


def _plain(text) -> str:
    return " ".join(str(text or "").split())


def _free(text) -> str:
    """The developer's own text as an audit record keeps it: its length, even when it reads like an option ("B")."""
    return f"[free text: {len(str(text))} characters]"


def _ask_options(d: dict) -> list:
    """(option, label) in the order devkit offers a decision's options (ask_calls): the recommended one first, its
    label marked "(Recommended)", then the others. An irreversible decision's performing option carries what it
    runs, "A: yes (release v26.11.4)": performing_option, so --performs drives the label and approves_action alike
    (the Security engineer's question audit decision, rule 5)."""
    opts = d.get("options") or []
    rec, perf = _recommended(d), performing_option(d)
    runs = " ".join(f"{d.get('irreversible') or ''} {d.get('target') or ''}".split())
    order = ([rec] if rec else []) + [o for o in opts if o != rec]
    return [(o, o + (f" ({runs})" if o == perf and runs else "") + (" (Recommended)" if o == rec else ""))
            for o in order]


def ask_labels(d: dict) -> list:
    """A decision's option labels as devkit offers them, in order (_ask_options)."""
    return [label for _, label in _ask_options(d)]


def _devkit_option(d: dict, label):
    """The option whose label devkit offers (ask_labels) `label` is, word for word, with or without its
    "(Recommended)"; None for any other text."""
    lab = _plain(label)
    return next((o for o, own in _ask_options(d) if lab in (_plain(own), _plain(_RECOMMENDED.sub("", own)))), None)


def asked_how(run, d: dict, question) -> str:
    """"exact" when a question is devkit's own: ask_text(d), alone or followed by devkit's context for it
    (_ask_context), whitespace and case aside; "reworded" for any other text."""
    q = _norm(question)
    return "exact" if q in (_norm(ask_text(d)), _norm(f"{ask_text(d)} {_ask_context(run or {}, d)}")) else "reworded"


def label_match(d: dict, label, offered) -> tuple:
    """How a question tool answer picks an option of a reversible decision -> (option or None, match). "exact": a
    label devkit offers for it, word for word (ask_labels); "letter": an offered label that names an option by its
    letter ("B", "A: Skip them"); "words": an offered label that names one by its label in another case or spacing,
    or by its words ("reject"); "free": the developer's own text (Other) or an offered label that names no option,
    such as a letter `d` does not have."""
    if not _offered(label, offered):
        return None, "free"
    opt = _devkit_option(d, label)
    if opt:
        return opt, "exact"
    opt = option_of(d, label)
    if opt:
        return opt, "letter" if _norm(_RECOMMENDED.sub("", str(label))) == _letter(opt).casefold() else "words"
    mt = _LABEL_LETTER.match(_RECOMMENDED.sub("", str(label)))
    opt = next((o for o in d.get("options") or [] if mt and _letter(o) == mt.group(1).upper()), None)
    return opt, "letter" if opt else "free"


_QUESTION_FIELDS = {"question", "header", "options", "multiSelect"}  # all a question of devkit's exact call holds
_OPTION_FIELDS = {"label", "description"}  # all an option of it holds, the description empty
# all the call's top level holds: devkit's questions, and what Claude Code (2.1.319) adds to the tool input and its
# result when the developer answers, their answers and notes (a note is refused on its own). Never a title shown
# above the questions, metadata, chat text (response), a follow-up request or an idle timeout (afkTimeoutMs: the
# option they had selected, not clicked): the Code reviewer's finding 3
_CALL_FIELDS = {"questions", "answers", "annotations"}


def _blank(value) -> bool:
    """No text in `value`: None, a blank string, or a list or dict of blanks. Anything else (a number, a flag) is
    not blank, so an unknown shape fails closed."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return all(_blank(v) for v in (value.values() if isinstance(value, dict) else value))
    return False


def _not_devkits_words(run, d: dict, q: dict, label, asked: str, call, top, notes=None) -> str:
    """Why a question tool answer is no evidence for an irreversible decision, '' when it is: only devkit's exact
    call counts (the Security engineer's question audit decision, rule 1, and the Code reviewer's findings 1 to 3 on
    it), an allowlist of its form, since everything the tool shows the developer can redefine the options. The call
    (`call`, its questions as sent) asks this one question and nothing else; its top level (`top`: the hook's tool
    input, or the result the transcript keeps) holds nothing beyond _CALL_FIELDS; the question is devkit's (asked_how)
    and holds only question, header (empty or the decision's id), options and multiSelect false; each option holds
    only its label and an empty description, so no description or preview says "hold" beside the label that
    releases; the labels are exactly devkit's (ask_labels: every option once, "(Recommended)" only on the
    recommended one); the answer carries no note of the developer's (`notes`, the result's annotations: the call
    asks one question, so any text there qualifies this click, "only after Monday"); and the clicked label is one of
    them word for word: no letter and no word matching, so "A", "Yes" or "A: Yes, hold it" count for nothing.
    Reasons name no text of the call."""
    if not isinstance(call, list) or call != [q]:
        return "the call asks other questions beside it"
    if not isinstance(top, dict) or set(top) - _CALL_FIELDS:
        return "the call carries a field devkit does not send"
    if asked != "exact":
        return "its question is not devkit's own; send it as dev crew decisions --ask prints it"
    if set(q) - _QUESTION_FIELDS:
        return "the question carries a field devkit does not send"
    if q.get("header", "") not in ("", d["id"]):
        return f"its header is neither {d['id']} nor empty"
    raw = q.get("options") if isinstance(q.get("options"), list) else []
    opts = [o for o in raw if isinstance(o, dict)]
    if q.get("multiSelect", False) is not False or len(opts) != len(raw):
        return "the call does not offer one plain choice"
    if any(set(o) - _OPTION_FIELDS for o in opts):
        return "an option carries a field other than its label"
    if any(not isinstance(o.get("description", ""), str) or o.get("description", "").strip() for o in opts):
        return "an option carries a description"
    if not _blank(notes):
        return "the developer added a note to the answer"
    offered = [_plain(o.get("label")) for o in opts]
    if sorted(_plain(_RECOMMENDED.sub("", x)) for x in offered) != \
            sorted(_plain(_RECOMMENDED.sub("", x)) for x in ask_labels(d)):
        return "its labels are not exactly the decision's options as devkit words them"
    rec = next((_plain(_RECOMMENDED.sub("", x)) for x in ask_labels(d) if _RECOMMENDED.search(x)), None)
    if any(_RECOMMENDED.search(x) and _plain(_RECOMMENDED.sub("", x)) != rec for x in offered):
        return "“(Recommended)” marks an option devkit does not recommend"
    if _plain(label) not in offered or not _devkit_option(d, label):
        return "the answer is not one of devkit's labels word for word"
    return ""


def _target_named(target, text) -> bool:
    """Does `text` name `target` as a word of its own, any case, a tag with or without its "v"? "v26.11.4" is not
    named by "v26.11.40", "xv26.11.4" or "v26.11.4-rc1", and PR "1" not by "D1" or "PR 12" (the Code reviewer's
    finding 3); a sentence's full stop after it is fine."""
    t = str(target or "").strip()
    if not t:
        return True
    core = re.sub(r"^[vV](?=\d)", "", t)
    v = "[vV]?" if core[:1].isdigit() else ""
    return re.search(rf"(?<![\w./-]){v}{re.escape(core)}(?![\w/-]|\.\w)", str(text or ""), re.IGNORECASE) is not None


def _names_target(d: dict, question: str) -> bool:
    """An irreversible decision's question names what the action acts on (_target_named)."""
    return _target_named(d.get("target"), question)


# An irreversible decision opens its action only when the developer's answer chooses the option that performs it
# (26.11.3, the Senior engineer's probe: "approve D1: no" opened the release). Without options, only an answer that
# reads approve, approved or yes (or the bare `approve D<n>` line) does. Everything else opens nothing.
_APPROVE_WORDS = frozenset({"approve", "approved", "yes"})
# an option whose words start like this reads as not doing it, so it is never the default performing option
_REFUSE_WORDS = frozenset({"no", "not", "don't", "dont", "never", "reject", "wait", "hold", "cancel", "abort", "stop",
                           "skip", "keep", "later", "changes", "decline", "deny", "postpone", "nope", "nah",
                           "pause", "defer", "delay", "halt", "block", "refuse", "veto", "nay", "none", "negative",
                           "abstain", "pass", "stay", "leave", "n"})


def _option_text(o) -> str:
    """An option's words without its letter: "A: approve" -> "approve"; an option without a letter as it is."""
    return _norm(str(o).split(":", 1)[1] if ":" in str(o) else o)


def _named_option(opts, text, prefix: bool = False):
    """The one option `text` names: its label, its letter, its words without the letter ("approve" for "A:
    approve"), and with `prefix` the start of its label (as --recommend is read). None when none or several do."""
    t = _norm(_RECOMMENDED.sub("", str(text or "")))
    if not t:
        return None
    tests = [lambda o: t == _norm(o), lambda o: ":" in o and t == _norm(o.split(":", 1)[0]),
             lambda o: t == _option_text(o)] + ([lambda o: _norm(o).startswith(t)] if prefix else [])
    for match in tests:
        hits = [o for o in opts or [] if match(o)]
        if hits:
            return hits[0] if len(hits) == 1 else None
    return None


def _refuses(option) -> bool:
    # the first word, whatever punctuation or quotes surround it: "No.", "no, wait", "(no)", "'no'", "‘no’" and
    # "Wait," all read as not doing it; an apostrophe counts only inside a word ("don't"); NFKC folds fullwidth letters
    t = unicodedata.normalize("NFKC", _option_text(option)).lower().translate({0x2018: "'", 0x2019: "'", 0x02BC: "'"})
    words = re.findall(r"[a-z]+(?:'[a-z]+)*", t)
    return bool(words) and (words[0] in _REFUSE_WORDS or words[:2] == ["do", "not"])


def _default_performs(opts, recommend):
    """The option that performs an irreversible decision's action when --performs names none: the one option that
    reads yes or approve, else the recommended one unless it reads as not doing it; None otherwise."""
    yes = [o for o in opts or [] if _option_text(o) in _APPROVE_WORDS]
    if yes:
        return yes[0] if len(yes) == 1 else None
    rec = _named_option(opts, recommend, prefix=True)
    return rec if rec and not _refuses(rec) else None


def performing_option(d: dict):
    """The option of an irreversible decision whose choice opens its action, or None (no options, or none performs
    it). Recorded when it was asked (`performs`); a decision recorded before that gets the same default now."""
    opts = d.get("options") or []
    if not d.get("irreversible") or not opts:
        return None
    if "performs" in d:
        return d["performs"] if d["performs"] in opts else None
    return _default_performs(opts, d.get("recommend"))


def _said(d: dict, e: dict) -> str:
    """What one evidence entry of the developer answered: its answer, or for a bare `approve D<n>` of a decision
    with options, the recommended option."""
    return e.get("answer") or (d.get("recommend") if e.get("channel") == "E2" and d.get("options") else "")


def _chosen(d: dict, e: dict) -> str:
    """The developer's choice on a decision, from their own evidence entry, never from the answer the tech lead
    types: the option it names, else its text (their own text, typed under Other, names no option); a bare
    `approve D<n>` without options is "approve"."""
    said = _said(d, e)
    if said:
        return said if e.get("free") else (option_of(d, said) or said)
    return "approve" if e.get("channel") == "E2" else ""


def approves_action(d: dict) -> bool:
    """Does the developer's verified answer to this irreversible decision approve its action? With options: its
    choice is the performing option. Without: it reads approve, approved or yes. Anything else, and a decision not
    decided by the developer with evidence, approves nothing. A decision recorded before `chosen` is judged by its
    recorded answer, which crew_decide already held to the developer's own."""
    if not (d.get("irreversible") and d.get("state") == "decided" and d.get("by") == "developer"
            and d.get("verified") is True):
        return False
    chose = d.get("chosen") if "chosen" in d else d.get("answer")
    if d.get("options"):
        want = performing_option(d)
        return bool(want) and _named_option(d["options"], chose) == want
    return _norm(chose).rstrip(".!").strip() in _APPROVE_WORDS


def _answer_label(d: dict) -> str:
    """The developer's answer as an audit record may keep it: the option it names, else its length only."""
    chose = str(d.get("chosen") if "chosen" in d else d.get("answer") or "")
    return _named_option(d.get("options") or [], chose) or f"[free text: {len(chose)} characters]"


def _decision_candidates(run, action: str, covers) -> list:
    """The developer's verified, unused decisions that name `action` and a target `covers(d)` accepts."""
    return [d for d in (run or {}).get("decisions") or [] if d.get("state") == "decided" and d.get("verified") is True
            and d.get("by") == "developer" and d.get("irreversible") == action and d.get("target")
            and not d.get("used") and covers(d)]


def _audit_refused(run, action: str, target: str, covers, session: str):
    """Audit each decision that names this action and target but whose answer does not approve it: the decision's
    id and the option the developer chose, never the free text of the answer."""
    for d in _decision_candidates(run, action, covers):
        if not approves_action(d):
            _audit("crew.grant-refused", f"{action} {target}".strip(),
                   f"decision {d['id']}: the developer's answer is not the option that performs {action}", "refused",
                   project=(run or {}).get("project") or "", session=session, id=d["id"], answer=_answer_label(d))


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
    run = load_run(agent, sid) if any(e.get("answer") for e in kept) else None  # an answer names an option or none
    for e in kept:
        if e.get("void"):
            continue  # no evidence: its refusal is audited as crew.evidence-refused
        d = _decision(run, e["covers"]) or {}
        said = e.get("answer") or ""
        _audit("crew.evidence", e["covers"], e["excerpt"], channel=e["channel"], transcript_uuid=e["transcript_uuid"],
               session=f"{agent}:{sid}", answer=_free(said) if e.get("free") else (option_of(d, said) or said),
               options=d.get("options") or [], match=e.get("match") or "", asked=e.get("asked") or "",
               label=e.get("label") if e.get("offered") else "")  # never the developer's own text (Other)
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
    decision the last final message showed. `approve D<n>@<session>[: <option>]`, as a decision listed outside its
    own session shows it, counts only in the session it names, and records nothing typed in another. Relayed
    comments, notifications and headless sessions record nothing."""
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
        if mt and mt.group(2) and not _names_session(mt.group(2), sid):
            continue  # a decision of another session's run, typed here: it is evidence only in that session
        if mt and live:
            new.append(_entry(agent, "E2", mt.group(1).upper(), ln, uuid, tp, live, now,
                              answer=(mt.group(3) or "").strip()[:300], repo=repo, session=sid))
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
    """PostToolUse of the question tool in the main session: the decisions it asked are shown, and an answer to a
    question that names one decision (asked_decision: "D<n> ·" up front or the header D<n>) is E1 evidence for it.
    A reversible decision's question may be reworded: the option its label picks (label_match), else the
    developer's own text. An irreversible one counts only on devkit's own words (_not_devkits_words), and its
    question names its target; an answer to it that is refused, in any form, is kept as a void entry (`void`, no
    text of the answer), the developer's latest word on it, so it ends their earlier answers (_decision_evidence).
    So is an answer to any other question of the call that asks it or names its target (_speaks_of, the Code
    reviewer's P16), and to a crew member's call (`agent_id`, P17), which is never evidence and shows nothing.
    Each entry keeps how it matched (`match`) and whether the question was devkit's (`asked`). A call that asks one
    decision twice counts for neither question. Each refusal is audited as crew.evidence-refused, with its reason and
    never the text. -> the entries recorded."""
    now = now or time.time()
    sid = data.get("session_id") or ""
    member = bool(data.get("agent_id"))
    if data.get("tool_name") not in QUESTION_TOOLS or not sid:
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
    top = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    call = top.get("questions")
    qs = [q for q in call or [] if isinstance(q, dict)]
    resp = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}
    answers = resp.get("answers") if isinstance(resp.get("answers"), dict) else {}
    pending = {d["id"] for d in _awaiting(run)}
    ids = [asked_decision(q) for q in qs]
    shown, new, refused, voided = set(), [], {}, set()
    for q, did in ([] if member else zip(qs, ids)):
        qt = str(q.get("question") or "")
        shown.update(x for x in [f"D{n}" for n in _DECISION_REF.findall(qt)] + [did] if x in pending)
        d, label = _decision(run, did) if did else None, answers.get(qt)
        if not d or not isinstance(label, str) or not label.strip():
            continue
        offered = [o.get("label") for o in q.get("options") or [] if isinstance(o, dict)]
        asked, why = asked_how(run, d, qt), ""
        if ids.count(did) > 1:  # which of its answers is the developer's? Neither (the Code reviewer's finding 5)
            why = f"the call asks {did} {ids.count(did)} times, so none of its answers counts"
        elif not _names_target(d, qt):
            why = f"the question does not name {d['target']}"
        elif d.get("irreversible"):
            why = _not_devkits_words(run, d, q, label, asked, call, top, resp.get("annotations"))
        if why:
            refused.setdefault((did, why), True)
            if d.get("irreversible") and d["id"] not in voided:  # their latest word: no yes (the tech lead's decision)
                voided.add(d["id"])
                new.append(_entry(agent, "E1", d["id"], f"[refused: {why}]", data.get("tool_use_id"),
                                  harness_transcript(agent, sid), run, now, void=True, session=sid))
            continue
        opt, match = (_devkit_option(d, label), "exact") if d.get("irreversible") else label_match(d, label, offered)
        new.append(_entry(agent, "E1", d["id"], f"{qt[:120]} = {label[:70]}", data.get("tool_use_id"),
                          harness_transcript(agent, sid), run, now, answer=(opt or label.strip())[:300],
                          free=opt is None, match=match, asked=asked, offered=_offered(label, offered),
                          question=qt[:800], label=label[:300], session=sid))
    taken = {e["covers"] for e in new if not e.get("void")}
    notes = resp.get("annotations") if isinstance(resp.get("annotations"), dict) else {}
    for d in _awaiting(run):  # P16, P17: the developer's latest word on it too, fail closed
        if not d.get("irreversible") or d["id"] in voided | taken or \
                not any(_speaks_of(d, q, answers, notes) for q in qs):
            continue
        what = f"asks {d['id']} or names {d.get('target') or 'its target'}"
        why = f"a crew member's question {what}" if member else f"a question {what} outside devkit's exact call"
        refused.setdefault((d["id"], why), True)
        voided.add(d["id"])
        new.append(_entry(agent, "E1", d["id"], f"[refused: {why}]", data.get("tool_use_id"),
                          harness_transcript(agent, sid), run, now, void=True, session=sid))
    if shown:
        _mark_shown(agent, sid, shown, now)
    for did, why in refused:
        _audit("crew.evidence-refused", did, f"a question tool answer: {why}", "refused",
               session=f"{agent}:{sid}"[:60])
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
    named = sorted((d["id"] for d in _awaiting(run) if d["id"] in ids), key=lambda x: int(x[1:]))
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


def _own_record(r, e: dict) -> bool:
    """Is transcript record `r` evidence entry `e`'s own, the developer's act, outside any subagent? E2: a prompt
    record with its prompt id, origin human, holding the line. E1: the question tool's result for its tool_use_id
    with that answer, from a call that offered its label when it names an option."""
    if not isinstance(r, dict) or r.get("type") != "user" or r.get("isSidechain"):
        return False
    uuid = str(e.get("transcript_uuid") or "")
    if e.get("channel") == "E2":
        return r.get("promptId") == uuid and (r.get("origin") or {}).get("kind") == "human" and \
            (not e.get("excerpt") or _norm(e["excerpt"]) in _norm(_text_of(r.get("message"))))
    if e.get("channel") != "E1":
        return False
    blocks = (r.get("message") or {}).get("content")
    res = r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else {}
    asked = next((q for q in res.get("questions") or [] if isinstance(q, dict)
                  and q.get("question") == e.get("question")), {})
    offered = [o.get("label") for o in asked.get("options") or [] if isinstance(o, dict)]
    return any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == uuid
               for b in blocks or []) and \
        (not e.get("question") or (res.get("answers") or {}).get(e["question"]) == e.get("label")) \
        and (e.get("free") is not False or _offered(e.get("label"), offered))


def confirmed(e: dict, sid: str = "") -> bool:
    """Does the session's own transcript (harness_transcript, from the session id: `sid` or the entry's) show this
    entry as the developer's own act (_own_record)? An entry that carries another transcript's path is refused (B1).
    Codex transcripts are not read yet (spec F0, S3), so a Codex entry has nothing but its hook event behind it and
    is never confirmed (The Keymaker): it opens nothing."""
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
            if _own_record(r, e):
                return True
    except OSError:
        return False
    return False


def _texts(*values) -> list:
    """Every string in `values`, through nested lists and dicts (without recursion)."""
    out, todo = [], list(values)
    while todo:
        v = todo.pop()
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            todo.extend(v.values())
        elif isinstance(v, list):
            todo.extend(v)
    return out


def _speaks_of(d: dict, q: dict, answers: dict, notes: dict) -> bool:
    """Does question `q` of a question tool call speak of irreversible decision `d`? It asks it (asked_decision), or
    its text, header or options, or the answer and note given to it (`answers`, `notes`: the call's, by question),
    name its target. The tech lead's decision on the Code reviewer's P16, fail closed: an answer to such a question
    is the developer's latest word on `d` too, and a wrong void only means devkit asks again."""
    if asked_decision(q) == d["id"]:
        return True
    target = str(d.get("target") or "").strip()
    qt = q.get("question") if isinstance(q.get("question"), str) else None
    return bool(target) and any(_target_named(target, t) for t in _texts(q, answers.get(qt), notes.get(qt)))


def _word_on(d: dict, r: dict) -> bool:
    """Is transcript record `r` a word of the developer on irreversible decision `d`: a question tool result, the main
    session's or a crew member's, with a question that speaks of it (_speaks_of), or a prompt they typed with an
    `approve D<n>` line for it? A line qualified with any session counts too: a word that may be on it ends an
    earlier yes rather than letting it stand."""
    res = r.get("toolUseResult")
    if isinstance(res, dict) and isinstance(res.get("questions"), list):
        answers = res.get("answers") if isinstance(res.get("answers"), dict) else {}
        notes = res.get("annotations") if isinstance(res.get("annotations"), dict) else {}
        if any(isinstance(q, dict) and _speaks_of(d, q, answers, notes) for q in res["questions"]):
            return True
    if r.get("isSidechain") or (r.get("origin") if isinstance(r.get("origin"), dict) else {}).get("kind") != "human":
        return False
    msg = r.get("message") if isinstance(r.get("message"), dict) else {}
    return any(mt and mt.group(1).upper() == d["id"].upper()
               for mt in map(_APPROVE_LINE.match, _own_lines(_text_of(msg))))


def _member_results(path, since: str):
    """The tool results in the session's crew member transcripts (Claude Code keeps a member's at <main transcript
    without .jsonl>/subagents/agent-<id>.jsonl) not timed before `since`, the ISO time of a main transcript record.
    Each file is read whole: its records are not in time order (Claude Code writes a message queued for a member when
    the member takes it, with the time it was queued, minutes back), so no record's time says where to stop. A result
    in the same millisecond as `since`, a result without a time, and every result without `since` count (fail
    closed). Raises OSError when the members' folder is there but cannot be listed, a member file is not a regular
    file or cannot be read, or holds a result that is not JSON (torn or damaged): then nothing tells what a member
    was answered."""
    folder = Path(path).with_suffix("") / "subagents"
    try:
        with os.scandir(folder) as it:
            files = sorted((f.name, f.is_file()) for f in it if f.name.startswith("agent-") and f.name.endswith(".jsonl"))
    except FileNotFoundError:
        return  # no member has run in this session
    for name, regular in files:
        if not regular:  # a named pipe would block the read; anything but a file tells nothing
            raise OSError(f"{name} is not a regular file")
        if _torn(folder / name):  # its last record is still being written, maybe before its toolUseResult key
            raise OSError(f"{name} ends in a line still being written")
        for ln in _lines_back(folder / name, b"toolUseResult"):
            try:
                r = json.loads(ln)
            except (ValueError, RecursionError):
                # torn (still being written) or damaged: it may be the developer's answer
                raise OSError(f"{name} holds a result that cannot be read") from None
            if not isinstance(r, dict) or r.get("type") != "user" or "toolUseResult" not in r:
                continue
            ts = r.get("timestamp")
            if since and isinstance(ts, str) and ts < since:
                continue
            yield r


def _torn(path) -> bool:
    """Does the file end in a line with no newline, one Claude Code is still writing?"""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        if not fh.tell():
            return False
        fh.seek(-1, 2)
        return fh.read(1) != b"\n"


def _said_later(path, e: dict, d: dict) -> bool:
    """Has the developer said more on irreversible decision `d` after evidence entry `e`? The transcript, not the
    session record, is the authority on their latest word (the tech lead's decision on the Code reviewer's H2: a hook
    drops the void it cannot write while the session's lock is busy). A word on it (_word_on) after e's own record
    (_own_record) in the main transcript, or after that record's time in a crew member's. True too when e's record
    is not there or the transcript cannot be read: then nothing tells what came after it."""
    key = json.dumps(str(e.get("transcript_uuid") or "")).encode()
    try:
        last = next(_lines_back(path, b""), b"")  # b"" when the file ends with a newline
        if last and (b'"type":"user"' in last or b'"type": "user"' in last):
            return True  # a record still being written may be the answer: Claude Code writes its result key late
        for ln in _lines_back(path, b""):
            if key not in ln and b"toolUseResult" not in ln and b'"origin"' not in ln:
                continue
            try:
                r = json.loads(ln)
            except (ValueError, RecursionError):
                if b"toolUseResult" in ln:  # a result after e's record that cannot be read may be the answer
                    return True
                continue
            if not isinstance(r, dict) or r.get("type") != "user":
                continue
            if key in ln and _own_record(r, e):
                since = r.get("timestamp") if isinstance(r.get("timestamp"), str) else ""
                return any(_word_on(d, x) for x in _member_results(path, since))
            if _word_on(d, r):
                return True
    except OSError:
        return True
    return True


def _live(e: dict, now: float, run=None) -> bool:
    if e.get("used") is not None or (e.get("expires") and now > e["expires"]):
        return False
    return run is None or (e.get("run") == run.get("started") and run.get("state") != "done")


def evidence(agent: str, sid: str, pick, now: float | None = None, run=None) -> list:
    """The session's live, unused entries that `pick(entry)` accepts and the transcript confirms, oldest first. A
    void entry is never evidence."""
    now = now or time.time()
    sess = state.read(session_path(agent, sid)) or {}
    if headless(sess):
        return []
    return [e for e in sess.get("human") or [] if _live(e, now, run) and not e.get("void") and pick(e)
            and confirmed(e, sid)]


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
    them whose answer approves the action (approves_action; one that does not is audited as crew.grant-refused),
    or in auto mode a pre-approval with uses left (never for publish or deploy); a headless session only by
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
        ev = ((s or {}).get("allow_evidence") or {}).get(action) or {}
        if not (s and ev.get("target") and _same_repo(ev.get("repo"), repo)
                and _target_covers(action, ev["target"], target)):
            return None  # the grant covers its evidence's target in its repository only (M277)
        return _opened(f"step:{s['name']}", "step", None, None)
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

    def covers(d):
        return _target_covers(action, d["target"], target)

    def decision(r):
        return next((d for d in _decision_candidates(r, action, covers) if approves_action(d)), None)

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
    _audit_refused(live, action, target, covers, f"{agent}:{sid}")
    return None


def _when(ts) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def evidence_lines(entries: list) -> str:
    """The echo of what the developer just granted or approved, for the next context; a void entry approves nothing."""
    out = []
    for e in entries:
        if e.get("void"):
            continue
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
    the developer chose something else, so Morpheus records their answer and not his own. For an irreversible
    decision only the developer's latest word on it counts, over all its entries (the tech lead's decision on the
    Code reviewer's finding 1 on the exact call): a void (an answer refused in any form), their own text, a question
    tool answer outside devkit's exact call (one recorded before it was required) or an entry the transcript does not
    confirm makes it None, even after an earlier "yes", until they answer again. The transcript has the last word
    (_said_later, the Code reviewer's H2): any later question that asks it or names its target, the main session's
    or a member's, and any later `approve D<n>` line for it make it None too, with or without its void entry."""
    def ours(e):
        return _norm(e.get("covers")) == _norm(d["id"]) and d.get("shown") is not None and e["ts"] >= d["shown"]
    if d.get("irreversible"):
        sess = state.read(session_path(agent, sid)) or {}
        mine = [] if headless(sess) else [x for x in sess.get("human") or [] if _live(x, now, run) and ours(x)]
        e = mine[-1] if mine else {}
        if not e or e.get("void") or e.get("plain") or e.get("free") or (
                e.get("channel") == "E1" and (e.get("match"), e.get("asked")) != ("exact", "exact")) \
                or not confirmed(e, sid) or _said_later(harness_transcript("claude", sid or e.get("session") or ""),
                                                        e, d):
            return None
    else:
        found = evidence(agent, sid, ours, now, run)
        if not found:
            return None
        e = found[-1]  # the developer's latest word on it
    said = _said(d, e)
    if said:
        want = said if e.get("free") else (option_of(d, said) or said)  # their own text names no option
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
    if ev["channel"] == "E1" and ev.get("match"):
        return _rederive(rec, tp, agent, sid)
    return None


def _answered_call(path, uuid: str):
    """The question tool's result for the call `uuid` in a transcript ({questions, answers}), or None."""
    for ln in _lines_back(path, json.dumps(uuid).encode()):
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(r, dict) or r.get("type") != "user" or r.get("isSidechain"):
            continue
        blocks = (r.get("message") or {}).get("content")
        if any(isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id") == uuid
               for b in blocks or []):
            return r.get("toolUseResult") if isinstance(r.get("toolUseResult"), dict) else None
    return None


def _decision_from_record(did: str, ref: dict):
    """A decision rebuilt from its crew.decision record: its options, its action, and from the labels devkit
    offered (ref.labels) the recommended option, the performing one and the target; no question. None when the
    record kept only the start of its options or labels (audit.VALUE_CUT characters, the Code reviewer's finding 2):
    the rest is unknown, so the decision is too."""
    from . import audit
    if any(len(str(ref.get(k) or "")) >= audit.VALUE_CUT for k in ("options", "labels")):
        return None
    opts = [x for x in str(ref.get("options") or "").split(" | ") if x]
    act = str(ref.get("irreversible") or "")
    d = {"id": did, "question": None, "options": opts, "irreversible": act, "target": "", "recommend": ""}
    for label in (x for x in str(ref.get("labels") or "").split(" | ") if x):
        base = _plain(_RECOMMENDED.sub("", label))
        o = next((o for o in opts if base == o or base.startswith(f"{o} (")), None)
        if o and _RECOMMENDED.search(label):
            d["recommend"] = o
        if o and act and base.startswith(f"{o} ({act}") and base.endswith(")"):
            d["performs"], d["target"] = o, base[len(o) + 2 + len(act):-1].strip()
    return d


def _decision_as_asked(agent: str, sid: str, did: str, ts: float):
    """The decision `did` the session asked before its evidence at `ts` -> (run, d, why not): the session's run while
    no later crew.decision record shows `did` asked again (it holds the question, so `asked` is re-derived too), else
    the decision rebuilt from its last crew.decision record before `ts` (run None); (None, None, why) when that
    record cannot rebuild it; (None, None, '') without either."""
    from . import audit
    recs = [r for r in audit.query(action="crew.decision") if r.get("target") == did
            and (r.get("ref") or {}).get("session") == f"{agent}:{sid}"]
    run = load_run(agent, sid)
    d = _decision(run, did) if run else None
    if d and not any(float(r.get("ts") or 0) > ts for r in recs):
        return run, d, ""
    before = [r for r in recs if float(r.get("ts") or 0) <= ts]
    if not before:
        return None, None, ""
    d = _decision_from_record(did, before[-1].get("ref") or {})
    return None, d, "" if d else (f"its crew.decision record keeps only the first {audit.VALUE_CUT} characters of "
                                  f"its options or labels")


def _rederive(rec: dict, tp, agent: str, sid: str):
    """`dev audit verify` for a question tool answer (the Security engineer's question audit decision): rerun the
    matcher on the transcript's question and label against the decision, and name each of answer, match, label and
    asked the record states otherwise (asked only while the run holds the decision's question). -> the problem,
    or None."""
    from . import audit
    ev, ref, did = rec.get("evidence") or {}, rec.get("ref") or {}, str(rec.get("target") or "")
    head = f"evidence E1 {ev.get('transcript_uuid') or '-'} for {did}"
    res = _answered_call(tp, str(ev.get("transcript_uuid") or "")) or {}
    qs = [q for q in res.get("questions") or [] if isinstance(q, dict) and asked_decision(q) == did]
    label = (res.get("answers") or {}).get(qs[0].get("question")) if len(qs) == 1 else None
    if not isinstance(label, str) or not label.strip():
        return f"{head}: the call in the transcript does not ask {did} once with an answer"
    run, d, why = _decision_as_asked(agent, sid, did, float(rec.get("ts") or 0))
    if not d:  # nothing to rerun the matcher against: no problem of the record, so a note at most
        return audit.Note(f"{head}: devkit cannot rebuild {did} to rerun the matcher: {why}") if why else None
    q, rx = qs[0], audit._extra_rules(audit.settings()["redact"]["extra"])
    offered = [o.get("label") for o in q.get("options") or [] if isinstance(o, dict)]
    asked = asked_how(run, d, q.get("question")) if d.get("question") else None
    if d.get("irreversible"):
        why = _not_devkits_words(run, d, q, label, asked or "exact", res.get("questions"), res, res.get("annotations"))
        if why:
            return f"{head}: the transcript's answer is no evidence for an irreversible decision ({why})"
        opt, match = _devkit_option(d, label), "exact"
    else:
        opt, match = label_match(d, label, offered)
    said = (opt or label.strip())[:300]
    want = {"answer": audit._value(audit._option(said, d["options"]) if opt else _free(said), rx), "match": match,
            "label": audit.evidence_label(label, rx) if _offered(label, offered) else None, "asked": asked}
    got = {"answer": ref.get("answer"), "match": ev.get("match"), "label": ref.get("label"), "asked": ev.get("asked")}
    diff = [k for k in want if (k != "asked" or asked) and _plain(want[k] or "") != _plain(got[k] or "")]
    return f"{head}: the transcript shows another {', '.join(diff)} than the record" if diff else None


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


def check_close(run, target, text, explicit, force=False, now: float | None = None) -> str:
    """The safety checks before a step is closed. Raises StepError with what to do instead. A running step its crew
    member owns is refused while that member works; one whose member ended or went stale (lane_gone, as `dev crew
    owner` reads it) closes on the tech lead's evidence like his own. -> '' or, for such a step, the words that tell
    the run's history its owner had ended."""
    plan = run.get("plan") or []
    if target is None:
        raise StepError("no open step to close. Add the work as a step first: dev progress add \"<step>=<estimate>\"")
    if target.get("state") in ("done", "skipped"):
        raise StepError(f"step “{target['name']}” is already {target['state']}; to change it: "
                        f"dev progress reopen \"{target['name']}\" --why \"…\"")
    owner = target.get("owner") or "main"
    gone = ""
    if owner != "main" and target.get("state") == "running":
        from . import crew
        lane = (run.get("agents") or {}).get(owner)
        g = lane_gone(owner, lane, lane_states(run, now, own=True) if (lane or {}).get("state") == "running" else {})
        if g:
            gone = (f"its owner {crew.who(_lane_name(lane))} had {'gone stale' if g[0] == 'stale' else 'ended'}: "
                    f"{g[1]}")
        elif not force:
            raise StepError(f"step “{target['name']}” belongs to {crew.who(_lane_name(lane or {}))}, "
                            f"still working on it; it closes when that agent finishes. Use --force only if it is "
                            f"really done.")
    if force:
        return gone
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
    return gone


def _refuse_members_working(run, now: float | None = None) -> dict:
    """The run ends only when no crew member still works on a running step: ending it would close work its member
    still does, and leave its lane running inside a finished run. A step whose owner ended or went stale (lane_gone
    over lane_states of the caller's own run, as check_close and working_agents read it) does not hold the end; a
    lane the run has no record of is unknown, which counts as working. Neither --force nor --why passes it. Raises
    StepError naming each working member and step, and what to do instead. -> {step name: the words that tell the
    run's history its owner had ended}, for the steps the end closes over an owner that ended"""
    from . import crew
    owned = [s for s in run.get("plan") or [] if s.get("state") == "running" and (s.get("owner") or "main") != "main"]
    if not owned:
        return {}
    lanes_ = run.get("agents") or {}
    states = lane_states(run, now, own=True) if any((lanes_.get(s["owner"]) or {}).get("state") == "running"
                                                    for s in owned) else {}
    working, gone = [], {}
    for s in owned:
        lane = lanes_.get(s["owner"])
        g = lane_gone(s["owner"], lane, states)
        if g:
            gone[s["name"]] = (f"its owner {crew.who(_lane_name(lane))} had "
                               f"{'gone stale' if g[0] == 'stale' else 'ended'}: {g[1]}")
        else:
            working.append(s)
    if not working:
        return gone
    names = "; ".join(f"“{s['name']}” by {crew.who(_lane_name(lanes_.get(s['owner']) or {}))}" for s in working)
    first = working[0]["name"]
    raise StepError(f"crew members still work on steps of this run: {names}. Wait for their reports (each step closes "
                    f"when its member stops), or skip a step on purpose: dev progress skip \"{first}\" --why \"…\"")


def _lane_name(lane: dict) -> str:
    return lane.get("persona") or lane.get("label") or "a subagent"


def add_step(spec, cwd: str | None = None, now: float | None = None, why: str = "",
             from_agent: str | None = None) -> dict:
    """Add a step discovered during the work: "name=30m after other", or a list of such specs (spec_args), each a
    step. From a crew member, the step goes to its own lane's plan; the main plan is Morpheus's."""
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


def confirm(which, why: str, cwd: str | None = None, now: float | None = None, from_agent: str | None = None) -> dict:
    """`dev progress confirm <step> --why "…"`: a step flagged recheck still holds with the new version of the
    reopened steps it used, so its flag clears, with the reason in the run's history and the audit trail (the other
    way out is to reopen and redo it). Refused without a reason, for a step with no flag, and while a step it used is
    open again: there is nothing to check it against yet."""
    _main_only(from_agent, "confirms the plan's steps", cwd)
    now = now or time.time()
    why = " ".join(str(why or "").split())
    if not why:
        raise StepError("say why it still holds: --why \"…\" (it goes into the run's history)")
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise StepError(f"no step “{which}” in the plan")
        used = list(s.get("recheck") or [])
        if not used:
            raise StepError(f"step “{s['name']}” is not flagged recheck: nothing to confirm")
        by_name = _by_name(run["plan"])
        still = [n for n in used if (by_name.get(n) or {}).get("state", "done") not in DONE]
        if still:
            raise StepError(f"“{s['name']}” used {', '.join(f'“{n}”' for n in still)}, open again: confirm it once "
                            f"{'that is' if len(still) == 1 else 'they are'} closed, or reopen “{s['name']}” and redo "
                            f"it (dev progress reopen \"{s['name']}\" --why \"…\")")
        s.pop("recheck")
        run.setdefault("history", []).append({"ts": round(now), "step": s["name"], "from": s.get("state"),
                                              "to": s.get("state"), "by": "main",
                                              "why": f"confirmed after {', '.join(used)} changed: {why}"[:300]})
        run["history"] = run["history"][-200:]
        run["updates"].append({"ts": round(now), "kind": "confirm", "text": why, "step": s["name"]})
        run["updates"] = run["updates"][-40:]
        _save(run)
    _audit("plan.confirmed", s["name"], f"after {', '.join(used)} changed: {why}"[:300],
           project=run.get("project") or "", run=run.get("goal", "")[:80], by=_name_of(run, "main"))
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
    finished steps the Check approves or sends back (approvals.record). kind "done" ends the run, refused while a
    crew member still works on a step (_refuse_members_working), while an approval is owed or a step is flagged
    recheck once the plan is closed, unless `why` (approvals.end_check), or while a decision still needs the
    developer, unless `force`; the decisions still pending (moot ones too) and the questions still waiting for an
    answer close with the run."""
    now = now or time.time()
    closed, closed_msgs = [], []
    agent, sid = current_session(cwd)
    from_agent = _as_lane(from_agent, cwd)
    if kind == "done" and not from_agent:
        _main_only(None, "ends the run", cwd)
    if not load_run(agent, sid):  # no run yet: Morpheus's step starts one (start publishes, so never in the lock)
        if from_agent:
            raise StepError("no progress run in this session, so this step has no lane to go to: the tech lead starts "
                            "the run (dev progress start); until then, report it in your message")
        start(text if kind != "step" else "work in progress", cwd=cwd, now=now)
        from_agent_lane = False
    else:
        from_agent_lane = bool(from_agent)
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if from_agent_lane:  # a subagent's update: its lane only
            run = normalize(run)
            named = _find_step(run, which) if which not in (None, "") else None
            if which not in (None, "") and not named:
                raise StepError(f"no step “{which}” in the plan: " + "; ".join(
                    f"{i}. {s['name']}" for i, s in enumerate(run.get("plan") or [], 1)))
            lane = _lane(run, from_agent, now)
            u = {"ts": round(now), "kind": kind, "text": text, "check": check}
            if named:  # the step this evidence is about: the lane's end closes it (_steps_about)
                u["step"] = named["name"]
            lane["updates"] = (lane.get("updates") or [])[-19:] + [u]
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
            gone = ""
            if not plan:  # a run without a plan: each step is recorded as it comes
                target = _new_step(text[:60], est=None)
                plan.append(target)
                _allocate(run)
            else:
                gone = check_close(run, target, text, explicit, force, now)
            old_state = target.get("state")
            _finish_step(run, target, now, agent, sid)
            _log_change(run, target, old_state, "done", "main",
                        f"{gone}; closed on the tech lead's evidence: {check or text}" if gone else check or text, now)
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
                run["blocked_on"] = cur["name"]  # the run runs again once this step moves (_settle)
        elif kind == "done":
            waiting = pending_decisions(run)
            if waiting and not force:
                raise StepError("decisions still waiting for the developer: " + ", ".join(
                    f"{d['id']} ({d['question'][:60]})" for d in waiting) + ". Ask, then dev crew decide <id> \"…\".")
            gone = _refuse_members_working(run, now)
            open_ = [s for s in plan if s.get("state") not in ("done", "skipped") and s.get("state") != "running"]
            if open_ and not force:
                raise StepError("not finished: " + ", ".join(f"“{s['name']}” ({s.get('state')})" for s in open_) +
                                ". Close or skip each one (dev progress skip \"<step>\" --why \"…\"), or use --force.")
            closes = {s["name"] for s in plan if s.get("state") != "done" and (s.get("state") == "running" or s is target)}
            from . import approvals
            closed_plan = copy.deepcopy(run)  # the plan as this end leaves it: its approvals are checked on that
            for s in closed_plan["plan"]:
                if s["name"] in closes:
                    s.update(state="done", finished=now)
                elif s.get("state") not in DONE:
                    s["state"] = "skipped"
            for s in closed_plan["plan"]:
                if s["name"] in closes:
                    _rechecked(closed_plan, s)  # as _finish_step leaves it
            try:
                over = approvals.end_check(closed_plan, why, now, lambda s_: member_of_step(closed_plan, s_))
            except approvals.ApprovalError as e:
                raise StepError(str(e))
            if over:
                run.setdefault("raci_overrides", []).append(over)
            for s in plan:
                if s.get("state") != "done":
                    old_state = s.get("state")
                    if s["name"] in closes:
                        _finish_step(run, s, now, agent, sid)
                    else:
                        s["state"] = "skipped"
                    _log_change(run, s, old_state, s["state"], "main",
                                f"{gone[s['name']]}; closed with the run's end: {text}" if s["name"] in gone else text,
                                now)
            closed = _close_decisions(run, "run ended", now)
            closed_msgs = _close_messages(run, "run ended", now)
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
    _audit_closed(run, closed)
    _audit_closed_messages(run, closed_msgs)
    _publish()
    if kind in ("blocked", "done"):
        notify(f"{run.get('project') or 'devkit'} · {kind}", text)
    return run


def _who_label(label: str) -> str:
    """A lane label with the member role first: "Tank: the API" -> "Backend engineer (Tank): the API", "Seraph" ->
    "Code reviewer (Seraph)"; a label that names no member ("general-purpose", "Explore: find X") stays as written."""
    from . import crew
    task = crew.task_of(label)
    return f"{crew.who(label)}: {task}" if task != (label or "").strip() else crew.who(label)


def crew_summary(run: dict, limit: int = 4, now: float | None = None) -> str:
    """Who in the crew works on what in this run, running first, then the slots in use and why the limit is lower
    (spec R7): "Tank ▸ the API · Kid ✓ installer · 1 of 4 slots in use". '' without crew members (the agent's own
    helpers are not the crew)."""
    from . import crew
    now = now or time.time()
    states, lanes = lane_states(run, now), []
    for aid, lane in (run.get("agents") or {}).items():
        m = crew._for_label(lane.get("persona") or "") if lane.get("persona") else None
        if not m:
            continue
        step_ = next((s["name"] for s in run.get("plan") or [] if s.get("owner") == aid), "")
        what = step_ or (lane.get("label") or "").split(":", 1)[-1].strip()
        mark = {"running": "▸", "done": "✓", "lost": "✗", "stopped": "✗"}.get(lane.get("state"), "·")
        live = (states.get(aid) or {}).get("state", "working")
        if lane.get("state") == "running" and live == "stale":  # a member that went quiet works no longer
            mark, what = "✗", f"{what} (stale)"
        elif lane.get("state") == "running" and live == "ended":  # its stop never reached the run, nor its report
            stopped = "interruption" in states[aid]["why"]
            mark, what = ("✗", f"{what} (stopped)") if stopped else ("⚠", f"{what} (ended, no report)")
        works = lane.get("state") == "running" and live == "working"
        lanes.append((not works, f"{crew.who(m)} {mark} {what}".strip()))
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
    crew_line = crew_summary(run, now=now)
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
    moot = moot_decisions(run)
    if moot:  # asked by a lane that ended, and no step waits on them: not the developer's, on a line of their own
        lines += [""] + [f"Moot: {d['id']} · “{(d.get('question') or '')[:70]}”: {_moot_reason(run, d)}. It closes "
                         f"with the run, or: dev crew withdraw {d['id']} --why \"…\"" for d in moot]
    if plan:
        lines.append("")
        for i, s in enumerate(plan, 1):
            mark = {"done": "✓", "running": "▸", "blocked": "■", "skipped": "–"}.get(s.get("state"), "○")
            main = (s.get("owner") or "main") == "main"
            waits = deps_open(run, s) if s.get("state") in ("pending", "blocked") else []
            parts = [] if main else [_who_label(agents.get(s["owner"], {}).get("label") or "subagent")]
            if waits:
                parts.append("waiting for " + ", ".join(waits) + (f", then {_who_label(s['label'])}"
                                                                  if main and s.get("label") else ""))
            elif main and s.get("label"):
                parts.append(f"waiting for {_who_label(s['label'])}")
            if s.get("recheck"):
                parts.append(f"recheck: {', '.join(s['recheck'])} reopened")
            changed = other_writes_for(run, s) if s.get("state") not in DONE else []
            if changed:
                parts.append("changed by another lane: " + ", ".join(f"{p} ({by}, {c})" for p, by, c in changed[:5])
                             + (f" and {len(changed) - 5} more" if len(changed) > 5 else ""))
            who = "".join(f" · {p}" for p in parts)
            if s.get("state") in ("done", "running"):
                tm = f" · {fmt_min(step_minutes(run, s, now))} of ~{fmt_about(s.get('est') or 0)}"
            else:
                tm = f" · ~{fmt_about(s.get('est') or 0)}"
            lines.append(f"{mark} {i}. {s['name']}{who}{tm}")
    if agents:
        parts = []
        for aid, a in agents.items():
            mins = active_minutes(run, a.get("started"), a.get("finished") or now, now, aid)
            parts.append(f"{'▸' if a.get('state') == 'running' else '✓'} "
                         f"{_who_label(a.get('label') or a.get('type') or aid[:8])} {fmt_min(mins)}")
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


def notify(title: str, text: str, security: bool = False):
    """A macOS desktop notification for the developer. Never fails. DEVKIT_NO_NOTIFY and `progress.notify: false`
    turn progress notices off; a security notice (`security=True`, such as an extension accepted) ignores both,
    because an agent can set either. The text and title reach osascript as arguments, never inside the script, so
    they need no escaping (AppleScript rejects the \\uXXXX escapes json.dumps writes) and cannot run as script; the
    absolute path keeps an `osascript` planted earlier on PATH from running."""
    if not security and (not cfg().get("notify") or os.environ.get("DEVKIT_NO_NOTIFY")):
        return
    # a lone surrogate (JSON can carry one) cannot reach a process: it would raise here, and the notice would not show
    text, title = (str(v).encode("utf-8", "replace").decode("utf-8") for v in (text, title))
    try:
        core.sh(["/usr/bin/osascript", "-e", "on run argv",
                 "-e", "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run",
                 "--", text[:180], "devkit · " + title[:60]], timeout=5)
    except Exception:
        pass


def active_runs(max_age_h: float = 12) -> list:
    out = []
    for f in RUNS.glob("*.json") if RUNS.exists() else []:
        r = core.read_json(f, None)
        if r and r.get("state") != "orphaned" and time.time() - f.stat().st_mtime < max_age_h * 3600:
            out.append(r)  # an orphaned run (its session ended, approvals.reconcile) is not live
    return sorted(out, key=lambda r: -r.get("started", 0))


def _shown_lane(lane: dict, live) -> str:
    """A lane's state as brief shows it: "running" only while its member works (`live`, its lane_state), "stale" or
    "ended" for one its run still lists as running, else its recorded state."""
    if lane.get("state") != "running" or not live:
        return lane.get("state")
    return {"working": "running"}.get(live["state"], live["state"])


def brief(run: dict, now: float | None = None) -> dict:
    """Compact form for presence and the dashboard. A lane its run lists as running shows as its member really is
    (lane_states): "running" while it works, else "stale" or "ended", with `live` and `why`."""
    now = now or time.time()
    run = normalize(run)
    states = lane_states(run, now)
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
            "agents": [{"label": a.get("label") or a.get("type") or k[:8], "state": _shown_lane(a, states.get(k)),
                        "min": round(active_minutes(run, a.get("started"), a.get("finished") or now, now, k), 1),
                        **({"live": states[k]["state"], "why": states[k]["why"]} if k in states else {})}
                       for k, a in (run.get("agents") or {}).items()],
            "headline": headline(run, now, states), "last": last.get("text", ""), "next": last.get("next", ""),
            "updated": last.get("ts")}


# --------------------------------------------------------------------------- hooks

STATUS_Q = re.compile(r"(c[oó]mo (va|vas|vamos|lo llevas)|qu[eé] (te )?falta|en qu[eé] (estado|punto)|"
                      r"how('?s| is) it going|where are we|what'?s left|status\??$|progress\??$|\beta\b|"
                      r"dame un resumen|give me (a|an) (summary|update)|update\?$|novedades)", re.I)


def _sess(agent, sid):
    f = SESSIONS / f"{agent}-{sid}.json"
    return f, (core.read_json(f, {}) or {}) if f.exists() else {}


def on_prompt(agent: str, data: dict, now: float | None = None):
    """UserPromptSubmit: start the turn clock; answer status questions from real progress data; when decisions wait
    for the developer, tell the tech lead to ask them together (waiting_context)."""
    sid = data.get("session_id") or "-"
    f, s = _sess(agent, sid)
    now = now or time.time()
    if f.exists():
        try:
            state.update(f, lambda d: d.update({"turn_started": now, "last_update": now, "nudges": 0,
                                                "last_prompt": now, "request_ts": None}), create=False)
        except state.StateBusy:
            pass  # audited by state: this prompt's clock is dropped, the hook still answers
    _run_event(agent, sid, lambda run: (_close(run, "turn", None, now), _open(run, "turn", "main", now)))
    prompt = (data.get("prompt") or "")[:400]
    mail = "\n\n".join(x for x in (_mail(agent, sid, "main"), waiting_context(agent, sid, s.get("last_prompt"), now))
                       if x) if f.exists() else ""
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
    apply_stops(agent, sid)  # a member's report that found the lock busy, before its lane is judged without it
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
    for a stopped agent, so its lane ends here, at the stop call's result (lane_stopped). -> the transitions made"""
    resp = data.get("tool_response")
    if data.get("tool_name") not in STOP_TOOLS or not isinstance(resp, dict):
        return []  # PreToolUse has no result yet: the stop may still fail
    inp = data.get("tool_input") or {}
    tid = str(resp.get("task_id") or inp.get("task_id") or inp.get("shell_id") or "")
    if resp.get("task_type") not in (None, "local_agent") or not AGENT_ID.fullmatch(tid):
        return []
    now = now or time.time()
    return lane_stopped(tid, f"stopped by TaskStop at {time.strftime('%H:%M', time.localtime(now))}, before its report",
                        agent=agent, sid=data.get("session_id") or "-", now=now)


def lane_stopped(task_or_agent_id: str, reason: str, agent: str | None = None, sid: str | None = None,
                 now: float | None = None) -> list:
    """A subagent stopped before its report (TaskStop, an interruption): Claude Code fires no SubagentStop for it.
    `task_or_agent_id`: its agent id (TaskStop's task id for an agent) or its launch's tool_use_id. Its launch stops
    holding a crew slot; its lane ends stopped, which frees the lane's slot, and each open step it owns waits for a
    new launch, unless a live lane of the same member holds it (approvals.stop_moves); the run's history and the
    audit trail record each change with `reason`. A stopped agent that works again (SendMessage) resumes its lane and
    takes its slot back. The session defaults to the current one. -> the transitions made"""
    from . import approvals
    now = now or time.time()
    if not (agent and sid):
        agent, sid = current_session()
    tid = str(task_or_agent_id or "")
    if not AGENT_ID.fullmatch(tid):
        return []
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
    for rsid in [sid] + runs_listing(agent, aid, sid):  # every run that lists it, whichever id the stop came under
        why = reason if rsid == sid else f"{reason}; the stop came under {agent}:{sid[:12]}, the conversation's " \
                                         f"newer session id"
        with _tx(agent, rsid):
            run = load_run(agent, rsid)
            if not isinstance(run, dict):
                continue
            held = [st for st in run.get("plan") or [] if st.get("owner") == aid and st.get("state") not in DONE]
            here = approvals.apply(run, approvals.stop_moves(run, aid, why, at=now), now)
            for st in held:  # a live lane of its member took the step: its scope goes with it
                if st.get("owner") not in (aid, "main", None):
                    _hand_scope(run, st, st["owner"], now)
            if here:
                _save(run, touch_session=False)
        if here:
            approvals.audit_moves(run, here)
            made += here
    return made


def live_holder(run: dict, aid: str, s: dict):
    """The live lane of the same member that holds a stopped lane's step: another working lane of that member,
    launched by Morpheus and bound to its launch, that holds no open step and whose launch names this step best (as
    _bind_step chooses). None when there is none, or two name it equally."""
    from . import crew
    agents = run.get("agents") or {}
    member = crew._for_lane(agents.get(aid) or {})
    if not member:
        return None
    scored = []
    for k, lane in agents.items():
        if k == aid or lane.get("state") != "running" or lane.get("binding", "exact") not in ("exact", "confirmed") \
                or (lane.get("parent") or "main") != "main" or (crew._for_lane(lane) or {}).get("id") != member["id"]:
            continue
        if any(st.get("owner") == k and st.get("state") not in DONE for st in run.get("plan") or []):
            continue  # one step at a time
        sc = _name_score(_task_text(lane.get("label") or ""), s["name"])
        if sc[1]:
            scored.append((sc, k))
    if not scored:
        return None
    best = max(sc for sc, _ in scored)
    top = [k for sc, k in scored if sc == best]
    return top[0] if len(top) == 1 else None


LIVE_RUN = ("running", "blocked", "orphaned")  # a run a session still works on, or one its ended session left


def adopt_process_run(agent: str, new_sid: str, pid: int, now: float | None = None) -> dict | None:
    """A session registered in agent process `pid` that an older session of the same agent holds, or held until this
    registration superseded it (Claude Code gives a running conversation a new session id after a compaction): the
    older session's open run moves to `new_sid`, as dev session carry moves one, working lanes included, since
    their hooks now carry the new id; the older record's launches and subagents go with it, so a launch keeps its slot
    and its agent binds and stops as before. Never over an open run of the new session (audited as refused). The run's
    history and the audit trail (session.adopt) record the move, and the older record notes it (run_moved_to). The
    registration path calls it (hooks._claim_process). -> {from, to, goal}, or None when nothing moved."""
    now = now or time.time()
    if not new_sid or not isinstance(pid, int) or isinstance(pid, bool):
        return None
    me = core.read_json(session_path(agent, new_sid), {}) or {}
    olds = []
    for f in SESSIONS.glob(f"{_safe_name(agent)}-*.json") if SESSIONS.exists() else []:
        s = core.read_json(f, {}) or {}
        sid = str(s.get("session") or "")
        if s.get("agent") != agent or not sid or sid == new_sid or pid not in (s.get("pid"), s.get("pid_superseded")):
            continue
        if float(s.get("started") or 0) > float(me.get("started") or now):
            continue  # a newer session never hands its run to an older one
        if (core.read_json(run_path(agent, sid), {}) or {}).get("state") in LIVE_RUN:
            olds.append((float(s.get("started") or 0), sid))
    if not olds:
        return None
    old = max(olds)[1]
    src, dst = run_path(agent, old), run_path(agent, new_sid)
    a, b = sorted((src, dst), key=str)
    refused, taken = None, False
    with state.hold(a), state.hold(b):  # one lock per session covers its record and its run
        run = state.load(src)
        if not isinstance(run, dict) or run.get("state") not in LIVE_RUN:
            return None
        there = state.load(dst)
        if isinstance(there, dict) and there.get("state") in LIVE_RUN:
            refused = there.get("goal") or "a run"
        else:
            why = (f"moved from {agent}:{old} to {agent}:{new_sid}: Claude Code gave the conversation in process "
                   f"{pid} a new session id")
            taken = take_back(run, why, now)
            run.update(session=new_sid, carried_from=old, carried_at=round(now))
            run.setdefault("history", []).append({"ts": round(now), "step": "(run)", "from": run.get("state"),
                                                  "to": run.get("state"), "by": "devkit", "why": why})
            run["history"] = run["history"][-200:]
            state.replace(dst, run, indent=1)
            state.remove(src)
            so, sn = state.load(session_path(agent, old)), state.load(session_path(agent, new_sid))
            if isinstance(so, dict) and isinstance(sn, dict):
                for key in ("launches", "subagents"):
                    mine = sn.setdefault(key, {})
                    for k, v in (so.pop(key, None) or {}).items():
                        mine.setdefault(k, v)
                so.update(run_moved_to=new_sid, run_moved_at=round(now))
                state.replace(session_path(agent, old), so)
                state.replace(session_path(agent, new_sid), sn)
    project = (run or {}).get("project") or ""
    if refused:
        _audit("session.adopt", f"{agent}:{old[:12]}", f"the run «{str(run.get('goal'))[:80]}» stays: "
               f"{agent}:{new_sid[:12]} already has an open run «{str(refused)[:80]}»", "refused", project=project)
        return None
    if taken:
        audit_taken_back(run, f"moved to {agent}:{new_sid[:12]}")
    _audit("session.adopt", f"{agent}:{new_sid[:12]}", f"run «{str(run.get('goal'))[:80]}» from {agent}:{old[:12]} "
           f"(process {pid})", "ok", project=project, session=f"{agent}:{new_sid[:12]}")
    return {"from": old, "to": new_sid, "goal": run.get("goal")}


def resume_orphaned(agent: str, sid: str, now: float | None = None) -> bool:
    """A resumed session takes back its run the reconciler orphaned when the session ended (spec C8). -> resumed"""
    now = now or time.time()
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not take_back(run, "its session resumed", now):
            return False
        _save(run, touch_session=False)
    audit_taken_back(run, "its session resumed")
    return True


def take_back(run, why: str, now: float) -> bool:
    """An orphaned run (its session ended, approvals.reconcile) runs again in a session that works on it: resumed, or
    carried to the session /clear started. Its history says why. Changes the run in place; the caller saves it, then
    calls audit_taken_back. -> whether it was orphaned"""
    if not isinstance(run, dict) or run.get("state") != "orphaned":
        return False
    was = run.pop("orphaned", None) or {}
    run["state"] = "running"
    run.setdefault("history", []).append({"ts": round(now), "step": "(run)", "from": "orphaned", "to": "running",
                                          "by": "devkit", "why": f"{why} (orphaned: "
                                          f"{str(was.get('why') or 'its session ended')[:120]})"})
    run["history"] = run["history"][-200:]
    return True


def audit_taken_back(run: dict, why: str):
    _audit("run.running", run.get("goal") or "run", why, "running", project=run.get("project") or "", by="devkit",
           was="orphaned")


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
    """PostToolUse: time the call; record an edit call's file (note_edit); nudge for a progress update after a long
    silence."""
    try:
        note_edit(agent, data)
    except Exception:
        pass  # a hook fails open: the edit record only spares a reading lane a false flag
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
    mail = _mail(agent, sid, me)  # what is new for the caller, taken once whichever of a hook and a wait reads it
    if me != "main":  # inside a subagent: deliver its messages; updates to the developer are the main session's job
        return mail or None
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
    from . import crew
    who, label = crew.who(lane.get("persona") or lane.get("label") or "a subagent"), lane.get("label") or ""
    scored = [(_name_score(_task_text(label), st["name"]), st) for st in mine]
    best = max(sc for sc, _ in scored)
    top = [st for sc, st in scored if sc == best]
    if not best[1] or len(top) > 1:
        names = ", ".join(f"“{st['name']}”" for st in (top if best[1] else mine))
        what = f"names {names} equally" if best[1] else f"names none of {crew.who(mine[0]['label'])}'s steps ({names})"
        how = (f"If this launch does one of them, link it: dev progress assign \"<step>\" --to \"{mine[0]['label']}\""
               if best[1] else "Each waits for a launch whose description names it (dev progress assign links a "
               "step only to such a launch, B-fy9639hfz5t)")
        _post(run, "devkit", "main", f"{who} started as “{label}”, which {what}: the steps stay with you. {how}",
              now, from_name="devkit")
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
        _hand_scope(run, st, aid, now)


def _hand_scope(run: dict, st: dict, to: str, now: float):
    """Step `st` and its scope now belong to lane `to`. Every other lane that holds no open step but keeps a scope of
    its own that overlaps it (the scope of a step it held before, such as a stopped lane's) loses that scope and reads
    only (`handed_over`) until Morpheus assigns it a step, so two lanes never write the same files, and a lane
    without a scope is not left free to write anywhere."""
    globs = st.get("scope") or []
    if not globs:
        return
    plan = run.get("plan") or []
    for k, lane in (run.get("agents") or {}).items():
        own = lane.get("scope") or []
        if k == to or not own or to in lane_chain(run, k):
            continue
        if any(x.get("owner") == k and x.get("state") not in DONE for x in plan):
            continue  # it works on a step of its own
        if not any(_globs_overlap(g, h) for g in globs for h in own):
            continue
        lane.pop("scope")
        lane["handed_over"] = {"ts": round(now), "step": st.get("name"), "to": to[:16], "scope": own}
        _audit("crew.scope-handed", lane.get("persona") or lane.get("label") or k[:8], f"“{st.get('name')}” and "
               f"{', '.join(own)} went to {_name_of(run, to)}: this lane may read but not write until a step is "
               f"assigned to it", "restricted", project=run.get("project") or "", agent_id=k[:16])


def _lane_event(run, aid, kind, tid, rec, binding, now, start, waiting, own=True, sess=None):
    """The run's side of a binding (spec C3): the agent's lane, created provisional at its start when a launch is
    waiting (no step, no scope, until it binds), resumed when the agent works again after it had stopped, and bound
    to its launch: label, parent, depth, persona and the step the launch names (none for a member's helper, which
    works inside its member's step; spec F3). `own`: the hook is the agent's own start or tool
    call, so a finished lane resumes; never for its launch's result or its stop. A resumed lane takes a crew slot
    like a launch: while none is free it stays as it was, reads only (`waits_slot`), and Morpheus is told once with
    the launch's message; each later call of the agent tries again. `sess`: the session record, held by the caller."""
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
        why = _resume_refusal(run, aid, sess if isinstance(sess, dict) else {}, now)
        if why:
            if not lane.get("waits_slot"):
                from . import crew
                _post(run, "devkit", "main", f"devkit: {crew.who(lane.get('persona') or lane.get('label') or kind)} "
                      f"works again, and a resumed member takes a crew slot like a launch. {why} Until a slot is "
                      f"free it may read but not write; it takes its slot at its first tool call after one frees.",
                      now, from_name="devkit", kind="notice")
                _audit("crew.queued", lane.get("persona") or lane.get("label") or aid[:8], why[:300], "queued",
                       project=run.get("project") or "", agent_id=aid[:16])
            lane["waits_slot"] = {"ts": round(now), "why": why[:400]}
        else:
            lane.pop("waits_slot", None)
            lane.update(state="running", finished=None, resumed=(lane.get("resumed") or 0) + 1, resumed_at=now)
            _open(run, "agent", aid, now)
            _audit("crew.resume", lane.get("persona") or lane.get("label") or aid[:8], lane.get("label") or "",
                   project=run.get("project") or "", agent_id=aid[:16])
            if lane.get("parent", "main") == "main":  # a member, not a helper: it takes the step waiting for it
                _take_waiting_step(run, aid, now, sess if isinstance(sess, dict) else None)
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
                                                   own=resume, sess=s))


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
    Lanes bound before launch records existed count while they run. A lane whose member ended or went stale
    (lane_states) holds no slot, whatever its run still lists."""
    now = now or time.time()
    if sess is None:
        sess = core.read_json(session_path(run.get("agent") or "", run.get("session") or ""), {}) if run else {}
    sess = sess if isinstance(sess, dict) else {}
    agents, subs = (run or {}).get("agents") or {}, sess.get("subagents") or {}
    out = []
    for tid, rec in (sess.get("launches") or {}).items():
        aid = rec.get("agent_id")
        if rec.get("state") == "pending" and now - (rec.get("ts") or now) < LAUNCH_TTL:
            key, name = tid, rec.get("label") or rec.get("type") or "a launch"
        elif rec.get("state") == "started" and aid not in agents and aid in subs:
            key, name = aid, rec.get("label") or str(aid)[:8]  # working in a session without a run
        else:
            continue  # a launch that holds no slot: no cost to work out
        out.append((key, name, launch_cost(run, rec.get("type") or "", rec.get("label") or "",
                                           rec.get("parent") or "main")))
    try:
        from . import crew
    except Exception:
        crew = None
    bound = {k: v for k, v in agents.items()
             if v.get("state") == "running" and v.get("binding", "exact") in ("exact", "confirmed")}
    states = lane_states(run, now) if bound else {}
    for k, v in bound.items():
        if (states.get(k) or {}).get("state", "working") == "working":  # one that ended or went stale holds none
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
    """A step the developer asked for in the current turn, by Morpheus (not delegated by a member): added since its
    prompt, or added or assigned since a message they typed mid-turn (`request_ts`, note_midturn). True for every step
    when the turn is unknown (no prompt recorded). A plan step merely assigned in the turn, with no newer request,
    stays plan work."""
    turn = (sess or {}).get("turn_started")
    if not turn:
        return True
    if (s.get("requested_by") or "main") != "main":
        return False
    if _step_added(run, s) >= float(turn):
        return True
    req = (sess or {}).get("request_ts")
    return bool(req) and float(s.get("assigned_at") or 0) >= float(req)


MIDTURN_READ = 400_000  # bytes of new transcript lines one hook reads at most; the rest waits for the next hook
_WAKE_PROMPTS = ("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")


def _past_line(transcript, pos: int, start: int) -> int:
    """The offset just after the line that runs on past `pos`, so the next read starts at a whole line; `start` while
    that line is still being written (no end yet)."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(pos)
            while True:
                chunk = fh.read(MIDTURN_READ)
                if not chunk:
                    return start
                i = chunk.find(b"\n")
                if i >= 0:
                    return pos + i + 1
                pos += len(chunk)
    except OSError:
        return start


def midturn_requests(transcript, offset=None, since=None) -> tuple:
    """The messages the developer typed while the session worked, in the transcript lines after `offset` -> ([{"text",
    "ts"}], the offset to keep). Claude Code runs no UserPromptSubmit for them: it records each as an attachment of
    type queued_command with commandMode "prompt" and origin.kind "human". Never a task notification, nor any other
    queued command. A line cut mid-write is read at the next call, whole; a transcript shorter than `offset` (rotated)
    is read from its start; with no offset, only its last MIDTURN_READ bytes. `since`: only messages at or after it
    (the turn's prompt). They move the request clock and continuity only, never approval evidence: the attachment is
    the harness's format, not a contract."""
    try:
        size = os.path.getsize(transcript)
    except (OSError, TypeError, ValueError):
        return [], offset
    start = offset if isinstance(offset, int) and 0 <= offset <= size else (0 if isinstance(offset, int) else
                                                                           max(0, size - MIDTURN_READ))
    try:
        with open(transcript, "rb") as fh:
            fh.seek(start)
            data = fh.read(MIDTURN_READ)
    except OSError:
        return [], offset
    if start and not isinstance(offset, int):  # a tail read starts mid-line: from the next whole one
        cut = data.find(b"\n")
        data, start = (data[cut + 1:], start + cut + 1) if cut >= 0 else (b"", start)
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) < MIDTURN_READ:
            return [], start  # a line still being written: read it whole at the next call
        return [], _past_line(transcript, start + len(data), start)  # one line longer than a read: skip to its end
    out = []
    import datetime as dt
    for raw in data[:end + 1].splitlines():
        if b"queued_command" not in raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        a = rec.get("attachment") if isinstance(rec, dict) else None
        if not isinstance(a, dict) or a.get("type") != "queued_command" or a.get("commandMode") != "prompt" or \
                not isinstance(a.get("origin"), dict) or a["origin"].get("kind") != "human":
            continue
        text = str(a.get("prompt") or "")
        if not text.strip() or text.lstrip().startswith(_WAKE_PROMPTS):
            continue
        try:
            ts = dt.datetime.fromisoformat(str(a.get("timestamp") or rec.get("timestamp")).replace("Z", "+00:00")
                                           ).timestamp()
        except ValueError:
            continue
        if since and ts < float(since):
            continue
        out.append({"text": text, "ts": ts})
    return out, start + end + 1


def note_midturn(agent: str, data: dict) -> list:
    """The main session's hooks (PreToolUse of a launch, PostToolUse): the messages the developer typed mid-turn
    since the last hook (midturn_requests, from the offset the session record keeps, read and moved under its lock so
    each is counted once) become the developer's latest request: `request_ts`, which is_latest_request reads, and a
    prompt of the turn in continuity. -> the messages found"""
    if data.get("agent_id") or agent != "claude":
        return []
    sid = data.get("session_id") or ""
    f = session_path(agent, sid)
    if not sid or not f.exists():
        return []
    sess = state.read(f) or {}
    try:  # the usual case: nothing written since the last hook read it, without the lock
        if os.path.getsize(data.get("transcript_path") or sess.get("transcript")) == sess.get("midturn_offset"):
            return []
    except (OSError, TypeError, ValueError):
        return []
    found = []

    def read(s):
        transcript = data.get("transcript_path") or s.get("transcript")
        if not transcript:
            return state.SKIP
        new, off = midturn_requests(transcript, s.get("midturn_offset"), since=s.get("turn_started"))
        if off == s.get("midturn_offset") and not new:
            return state.SKIP
        s["midturn_offset"] = off
        if new:
            s["request_ts"] = max([float(s.get("request_ts") or 0)] + [r["ts"] for r in new])
            found.extend(new)
    try:
        state.update(f, read, create=False)
    except state.StateBusy:
        return []  # audited by state; the next hook reads them
    if found:
        from . import continuity
        for r in found:
            continuity.on_midturn(agent, sid, r["text"], r["ts"])
    return found


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
    grants nothing); read too for a lane whose step and scope another lane took (`handed_over`, until Morpheus
    assigns it a step) and for a resumed lane that waits for a crew slot (`waits_slot`); else its crew member's
    `tools`, else all (an ad-hoc agent)."""
    if lane.get("binding") in ("conflict", "unbound", "pending") or lane.get("handed_over") or \
            (lane.get("waits_slot") and lane.get("state") != "running"):
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
    """A repo-relative path is inside every layer of a lane_scope (the same glob rules as scope_holders)."""
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
        try:  # it rides along with the next wake of each live run's main session: slots frees or holds queued steps
            from . import watch
            watch.notice_all("notice", f"limit:{agent}:{limit}:{round(now or time.time())}",
                             f"crew slots {old} → {limit}: {why or 'progress.max_parallel'}", agent=agent)
        except Exception:
            pass


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
        return (f"This launch would run {depth} levels below the tech lead; the limit is {most} (progress.max_depth). "
                f"Do this part yourself, or name it in your report and the tech lead assigns it.")
    inp = data.get("tool_input") or {}
    kind = str(inp.get("subagent_type") or inp.get("agent_type") or "")
    label = str(inp.get("description") or inp.get("nickname") or inp.get("name") or "")
    cost = launch_cost(run, kind, label, parent)
    if cost <= 0:
        return None  # a scout lookup costs no slot (progress.slot_weights)
    b = slot_budget(agent, run, sess, now)
    why = _slot_refusal(run, sess, b, cost, parent, kind, label)
    if why is not None:
        return why or None  # "": it took the slot kept for the developer's own request
    if parent != "main" or (sess.get("turn_started") and _for_the_developer(run, sess, kind, label)):
        return None  # the developer's request of this turn is not held back by the plan's order (R4)
    return _order_verdict(run, sess, kind, label, b, cost, now)


def _slot_refusal(run, sess: dict, b: dict, cost: float, parent: str, kind: str, label: str):
    """The slot rules a launch and a resumed lane share (spec R3, R4): the reason no slot is free; "" when it takes
    the slot progress.reserve keeps, for the developer's own request (audited); None when it fits below the reserve."""
    used, limit, reserve = b["used"], b["limit"], b["reserve"]
    if used + cost > limit + 1e-9:
        names = ", ".join(_who_label(name) for _, name, _ in b["busy"])
        head = f"{_n(used)} of {limit} crew slots are in use ({names}): the limit is {limit} at once"
        head += (f", lowered from {b['max']}: {b['why']}" if b["why"] else "") + \
            (f", and this launch takes {_n(cost)}" if cost != 1 else "")
        if parent != "main":
            return (f"{head}. A helper takes a slot like any crew member, so do this part yourself, or name it in "
                    f"your report and the tech lead assigns it.")
        return (f"{head}. Wait for one to finish (you are notified), or give the work to one of them (SendMessage / "
                f"send_input). progress.max_parallel in devkit config changes the limit.")
    if used + cost > limit - reserve + 1e-9:  # it would take the slot kept for the developer (spec R4)
        if parent == "main" and _for_the_developer(run, sess, kind, label):
            _audit("crew.reserve", label or kind or "a launch", f"{_n(used + cost)} of {limit} slots: the slot "
                   f"progress.reserve keeps for the developer's own request", project=(run or {}).get("project") or "")
            return ""
        return (f"{_n(used)} of {limit} crew slots are in use, and the last {reserve} is kept for the developer's own "
                f"request (progress.reserve): queued plan work waits for a free slot. Launch it when a member "
                f"finishes (dev crew next), or give it to a working member (SendMessage / send_input).")
    return None


def _resume_refusal(run: dict, aid: str, sess: dict, now: float):
    """Why lane `aid`, not working now, may not work again: a resumed lane takes a crew slot like a launch, at its
    own cost, with the launch's message. None when a slot is free."""
    lane = (run.get("agents") or {}).get(aid) or {}
    try:
        from . import crew
        m = crew._for_lane(lane)
    except Exception:
        m = None
    level = max((_lane_level(dict(run["agents"][k], state="running")) for k in lane_chain(run, aid)),
                key=TOOL_LEVELS.index, default="all")  # its level once it works, whatever it waits for now
    cost = _cost(m, level)
    if cost <= 0:
        return None
    b = slot_budget(run.get("agent") or "claude", run, sess, now)
    return _slot_refusal(run, sess, b, cost, lane.get("parent") or "main", lane.get("type") or "",
                         lane.get("label") or "") or None


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
    the session record and the run (the run's save inside re-enters it). The stops that found the lock busy earlier
    go first. A stop that finds it busy now waits in a pending file (_defer_stop) instead of being dropped: the
    member's report is its step's evidence."""
    sid = data.get("session_id") or "-"
    entered = False
    try:
        with state.hold(session_path(agent, sid)):
            entered = True
            _apply_stops(agent, sid)
            return _on_subagent(agent, data, started, now)
    except state.StateBusy:
        if entered or started:
            raise  # a record that does not parse, or a start: a start binds again at the agent's first tool call
        _defer_stop(agent, sid, data, now or time.time())


def _stops_dir(agent: str, sid: str) -> Path:
    """The pending SubagentStops of one session, one file per agent: <state>/stops/<agent>-<sid>/<agent id>.json."""
    return SESSIONS.parent / "stops" / f"{_safe_name(agent)}-{_safe_name(sid)}"


def _defer_stop(agent: str, sid: str, data: dict, now: float):
    """Keep a SubagentStop the session's lock could not take, with when it happened, for the next hook (apply_stops).
    Without an agent id there is nothing to apply it to: it is dropped, as before, and audited."""
    aid = str(data.get("agent_id") or data.get("subagent_id") or "")
    if not AGENT_ID.fullmatch(aid):
        _audit("crew.stop-dropped", aid or "?", "the session's lock was busy and the stop names no agent", "dropped")
        return
    _write_private(_stops_dir(agent, sid) / f"{aid}.json", {**data, "stopped_at": now})
    _audit("crew.stop-deferred", aid[:16], "the session's lock was busy: applied at the next hook", "deferred")


def apply_stops(agent: str, sid: str) -> int:
    """The SubagentStops that found the session's lock busy, applied now in the order they happened, each once, at
    the time it happened. Left for the next hook while the lock is still busy. Every hook calls it (reconcile_hook,
    on_subagent), and so does the scheduled sweep. -> how many were applied"""
    d = _stops_dir(agent, sid)
    if not d.is_dir() or not any(d.glob("*.json")):
        return 0  # the usual case, without the lock
    try:
        with state.hold(session_path(agent, sid)):
            return _apply_stops(agent, sid)
    except state.StateBusy:
        return 0


def _apply_stops(agent: str, sid: str) -> int:
    """apply_stops under the session's hold."""
    d = _stops_dir(agent, sid)
    if not d.is_dir():
        return 0
    def when(stop):
        data = stop[0]
        return (data.get("stopped_at") or 0) if isinstance(data, dict) else 0
    n = 0
    for data, p in sorted(((core.read_json(p, None), p) for p in d.glob("*.json")), key=when):
        if isinstance(data, dict):
            try:
                _on_subagent(agent, data, False, data.get("stopped_at"))
            except state.StateBusy:
                return n  # a record that does not parse: kept for the next hook
            except Exception as e:
                _audit("crew.stop-dropped", str(data.get("agent_id") or "?")[:16], f"could not be applied: {e}"[:300],
                       "dropped")
            n += 1
        p.unlink(missing_ok=True)
    return n


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

        def finish(run, rsid=sid):
            _close(run, "agent", aid, now)
            lane = (run.get("agents") or {}).get(aid)
            if lane and lane.get("binding") == "pending" and not lane.get("updates") and \
                    not any(st.get("owner") == aid for st in run.get("plan") or []):
                del run["agents"][aid]  # never bound, never reported: a helper that started while a launch waited
                return
            if lane:
                if rsid != sid:  # the stop came under the conversation's newer session id (a compaction)
                    run.setdefault("history", []).append({
                        "ts": round(now), "step": f"(lane {lane.get('persona') or lane.get('label') or aid})",
                        "from": lane.get("state"), "to": "done", "by": "devkit",
                        "why": f"ended: its stop came under {agent}:{sid[:12]}, the conversation's newer session id"})
                    run["history"] = run["history"][-200:]
                lane.update(state="done", finished=now)
                lane.pop("waits_slot", None)  # it ended while it waited: a later resume asks for a slot anew
                _audit("crew.finish", lane.get("persona") or lane.get("label") or aid,
                       f"{active_minutes(run, lane.get('started'), now, now, aid):.1f} active minutes",
                       project=run.get("project") or "", agent_id=aid[:16])
            _end_lane_steps(run, lane or {}, aid, report, now, agent, rsid)
            run["step"] = sum(1 for x in run.get("plan") or [] if x.get("state") in ("done", "skipped"))
            _advance_main(run, now)
        _run_event(agent, sid, finish)
        for other in runs_listing(agent, aid, sid):  # every run that lists it frees its lane, whichever id it came under
            _run_event(agent, other, lambda run, o=other: finish(run, o))
    state.replace(f, s)


def runs_listing(agent: str, aid: str, skip_sid: str) -> list:
    """The session ids of this agent's other live runs on this Mac that list agent `aid` as running: after a
    compaction gives the conversation a new session id, a member's stop comes under the new id while the run that
    launched it still lists it under the old one."""
    if not AGENT_ID.fullmatch(str(aid or "")):
        return []
    out = []
    for f in sorted(RUNS.glob(f"{_safe_name(agent)}-*.json")) if RUNS.exists() else []:
        r = core.read_json(f, None)
        if isinstance(r, dict) and r.get("state") in LIVE_RUN and r.get("agent", agent) == agent and \
                str(r.get("session") or "") not in ("", str(skip_sid)) and \
                ((r.get("agents") or {}).get(aid) or {}).get("state") == "running":
            out.append(str(r["session"]))
    return out


def _shown(paths: list) -> str:
    """'a.py, b.py and 4 more': up to five paths for a notice."""
    return ", ".join(paths[:5]) + (f" and {len(paths) - 5} more" if len(paths) > 5 else "")


def scope_written(agent: str, sid: str, aid: str, paths: list, now: float | None = None) -> dict | None:
    """A review or read-only lane's shell call wrote these files (write detection). A file is credited to whoever's
    own Write, Edit or MultiEdit call touched that path during the call (note_edit: a build lane, its helper or the
    tech lead, never the reading lane itself): devkit audits it as attributed and flags nothing, since an edit opens
    no shell call and the edit record is exact (B-fy1ae3k2ao6). Every other file is held against the reading lane in
    `scope_writes`, so its report is unverified and its step cannot end done on its word: a script of the reading
    lane can write into a working member's scope, so a scope is no alibi. A held file is "possible" when a build lane
    that works now and whose scope holds it, or any working build lane for a file outside every scope, had a shell
    call open (its shell may have written it), else "certain". An open step whose scope holds a held file keeps it
    (`other_writes`), which the review step after it lists; Morpheus gets one crew notice naming the files; and the
    audit trail a scope.write record per certainty. -> the lane's scope_writes, or None without a lane or when it
    has none."""
    from . import crew
    now = now or time.time()
    paths = sorted({str(p) for p in paths or [] if p})
    if not paths:
        return None
    states = lane_states(load_run(agent, sid) or {}, now, own=True)  # read before the lock: it reads other runs
    with _tx(agent, sid):
        run = load_run(agent, sid)
        lane = ((run or {}).get("agents") or {}).get(aid)
        if not lane:
            return None
        chain = set(lane_chain(run, aid))
        builders = [k for k, v in run["agents"].items() if k not in chain and v.get("state") == "running"
                    and (states.get(k) or {}).get("state", "working") == "working"
                    and lane_tools(run, k) in ("build", "all")]
        layers = {k: lane_scope(run, k) for k in builders}
        calls = open_calls(sid, now)
        calling = {str(e.get("caller") or "") for e in calls} - {"main", ""}
        busy = {k for k in builders if any(k in lane_chain(run, c) for c in calling)}
        start = min((e.get("ts") or now for e in calls if e.get("caller") == aid), default=now - CALL_TTL)
        edits = [e for e in edits_since(sid, start, now) if str(e.get("caller") or "") not in chain]
        owned, held = {}, {}  # credited to the caller whose edit touched it; held against the reading lane, by certainty
        for p in paths:
            by = next((str(e.get("caller")) for e in edits if _edited(e.get("path"), p)), None)
            if by:
                owned.setdefault(by, []).append(p)
                continue
            holders = [k for k in builders if layers[k] and in_scope(p, layers[k])]
            maybe = (set(holders) & busy) if holders else busy
            held.setdefault("possible" if maybe else "certain", []).append(p)
        who = lane.get("persona") or lane.get("label") or aid
        name = crew.who(who)
        loose = sorted(p for files in held.values() for p in files)
        if loose:
            had = lane.get("scope_writes") or {}
            sure = "certain" in held or had.get("certainty") == "certain"
            lane["scope_writes"] = {"ts": round(now), "paths": sorted(set(had.get("paths") or []) | set(loose))[:20],
                                    "certainty": "certain" if sure else "possible"}
            marked = []
            for c, files in held.items():
                marked += [n for n in _mark_other_writes(run, files, chain, aid, name, c, now) if n not in marked]
            parts = []
            if held.get("certain"):
                parts.append(f"its shell call wrote {_shown(held['certain'])}, which nobody else edited meanwhile")
            if held.get("possible"):
                shells = ", ".join(crew.who(_name_of(run, k)) for k in sorted(busy)[:3])
                parts.append(f"its shell call wrote {_shown(held['possible'])} (possibly the shell call of {shells}, "
                             f"open at the time)")
            for k, files in owned.items():
                parts.append(f"{_shown(files)} changed during it, edited by {crew.who(_name_of(run, k))} with its own "
                             f"edit tool, so devkit credits {'them' if len(files) > 1 else 'it'} to them")
            listed = f" The review after {', '.join(marked)} lists these files." if marked else ""
            level = lane_tools(run, aid)
            _post(run, aid, "main", f"devkit: {name} works {'review-only' if level == 'review' else 'read-only'}, "
                  "and " + "; and ".join(parts) + "." + listed + " Its report counts as unverified: check the change "
                  "(git diff), keep or revert it, then close its step with evidence.", now, from_name="devkit",
                  kind="notice")
            _save(run)
        writes = dict(lane.get("scope_writes") or {}) or None
    for k, mine in owned.items():
        _audit("scope.write", _name_of(run, k), f"attributed: {', '.join(mine[:20])}"[:600], "attributed",
               lane=k[:16], paths=mine[:20], count=len(mine), project=run.get("project") or "",
               why=f"edited by {_name_of(run, k)} with its own edit tool during {who}'s shell call")
    for c, files in held.items():
        _audit("scope.write", who, f"{c}: {', '.join(files[:20])}"[:600], "detected", lane=aid[:16],
               paths=files[:20], certainty=c, count=len(files), project=run.get("project") or "",
               **({"severity": "warning"} if c == "certain" else {}))
    return writes


def _mark_other_writes(run: dict, files: list, chain: set, by: str, by_name: str, certainty: str, now: float) -> list:
    """The open steps outside the writing lane whose scope holds one of these files keep them as changed by another
    lane (`other_writes`), so the review of that work lists them (other_writes_for). -> the names of those steps."""
    out = []
    for st in run.get("plan") or []:
        if st.get("owner") in chain or st.get("state") in DONE or not st.get("scope"):
            continue
        mine = [p for p in files if in_scope(p, [st["scope"]])]
        if mine:
            marks = st.setdefault("other_writes", [])
            marks.append({"ts": round(now), "by": by[:16], "by_name": by_name, "paths": mine[:20],
                          "certainty": certainty})
            st["other_writes"] = marks[-20:]
            out.append(st["name"])
    return out


def other_writes_for(run: dict, st: dict) -> list:
    """For a review step: the files another lane changed in the scope of the steps it comes after, [(path, who,
    certainty)], each path once, a certain record before a possible one. [] for any other step."""
    if not is_review_step(st):
        return []
    steps, seen, out = _by_name(run.get("plan")), set(), []
    marks = [m for n in st.get("after") or [] for m in (steps.get(n) or {}).get("other_writes") or []]
    for m in sorted(marks, key=lambda m: m.get("certainty") != "certain"):
        for p in m.get("paths") or []:
            if p not in seen:
                seen.add(p)
                out.append((p, m.get("by_name") or "another lane", m.get("certainty") or "possible"))
    return out


def _verified_by_review(run: dict, st: dict, check: str, now: float, agent: str, sid: str, aid: str) -> list:
    """A review step (approvals.is_review, the rule deps_open and from_review use too) that ended done with Check ✓:
    the unverified steps it comes after whose approver is its member (approvals.approver, never their own work) end
    done, the review being their evidence, so from_review records the approval on them. Any other unverified step
    stays so, for Morpheus to close with evidence (and its own approver to approve). -> the names of the steps it
    closed."""
    from . import approvals, raci
    if not approvals.is_review(st):
        return []
    try:
        if approvals.verdict_of(check) != "approved":
            return []
    except approvals.ApprovalError:
        return []
    who = member_of_step(run, st)
    if not who:
        return []
    steps, out = _by_name(run.get("plan")), []
    for n in st.get("after") or []:
        x = steps.get(n)
        if not x or x.get("state") != "unverified":
            continue
        did = member_of_step(run, x)
        if did == who or approvals.approver(raci.step_activity(x), did) != who:
            continue
        _finish_step(run, x, now, agent, sid)
        _log_change(run, x, "unverified", "done", aid, f"verified by the review “{st['name']}”: {check}", now)
        out.append(n)
    return out


def _steps_about(lane: dict, aid: str, report: str, mine: list) -> list:
    """Of the open steps a lane holds at its end (`mine`), those its evidence is about (B-fy9639hfz5t): the step its
    launch names (of the steps that did not join it by delegation, those its description's task text names best, as
    _bind_step chose), each step a `dev progress step --step` of the lane names, and each step whose name its
    report's status block (task or Done) or one of its `dev progress step` texts spells out."""
    from . import canary
    ups = [u for u in lane.get("updates") or [] if u.get("kind") == "step"]
    named = {u.get("step") for u in ups if u.get("step")}
    task = _task_text(lane.get("label") or "")
    scored = [(_name_score(task, st["name"]), st) for st in mine if st.get("joined") != aid]
    best = max((sc for sc, _ in scored), default=(False, 0, 0.0))
    launched = [st for sc, st in scored if best[1] and sc == best]
    p = canary.parse_status(report or "") or {}
    said = " ".join([p.get("task") or "", p.get("done") or ""] + [str(u.get("text") or "") for u in ups]).lower()
    return [st for st in mine if st["name"] in named or any(st is x for x in launched)
            or re.search(r"(?<!\w)" + re.escape(st["name"].lower()) + r"(?!\w)", said)]


def _end_lane_steps(run: dict, lane: dict, aid: str, report: str, now: float, agent: str, sid: str):
    """A lane's end at its SubagentStop: the steps its evidence is about end as its report or its last Check says
    (_end_member_step); any other step it holds goes back to pending, still its member's, with the reason in its
    history, so a lane's ✓ never closes work it did not do (B-fy9639hfz5t)."""
    mine = [st for st in run.get("plan") or [] if st.get("owner") == aid and st.get("state") not in DONE]
    about = _steps_about(lane, aid, report, mine)
    who = lane.get("persona") or "the subagent"
    for st in mine:
        if any(st is x for x in about):
            _end_member_step(run, st, lane, aid, report, now, agent, sid)
            continue
        old = st.get("state")
        st.update(owner="main", state="pending", started=None)
        st.pop("joined", None)
        _log_change(run, st, old, "pending", "devkit", f"{who} ended, and neither its launch, its report nor its dev "
                    f"progress step names this step: it waits for a launch of its own", now)


def _end_member_step(run: dict, st: dict, lane: dict, aid: str, report: str, now: float, agent: str, sid: str):
    """A member's step at its SubagentStop (spec F6, R2-4): done with the evidence of its report's Check or its last
    `dev progress step --check` (approvals.ending), and a review step's Check is its verdict on the work it follows
    (approvals.from_review), whose ✓ also ends done the unverified work it approves (_verified_by_review);
    otherwise unverified, for Morpheus to close with evidence or reopen. A review or read-only lane whose shell wrote
    files (scope_writes) ends unverified whatever it reports."""
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
        _verified_by_review(run, st, check, now, agent, sid, aid)
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
    runs = [r for r in active_runs() if r.get("state") in ("running", "blocked")]  # a finished run is not "now"
    if runs:
        out.append(ui.section("Runs now"))
        for r in runs[:8]:
            out.append(f"  {r.get('agent')} · {r.get('project') or '-'} · {r.get('goal')} — {headline(r)}")
    if len(out) == 1:
        out.append("  no timing data yet (devkit records turns, slow commands, subagents and runs as agents work)")
    return "\n".join(out)


_register_checks()  # `dev audit verify` checks every piece of developer evidence (spec C5, K1)
