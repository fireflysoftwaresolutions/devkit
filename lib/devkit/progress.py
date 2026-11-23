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
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from . import coord, core, state, ui
from .core import STATE

RUNS = STATE / "runs"
TIMING = STATE / "timing"
PENDING = STATE / "timing" / "pending"
SESSIONS = STATE / "sessions"
QUOTA = STATE / "quota"  # the plan's usage windows, recorded by the status line (hooks.statusline)
CALLS = STATE / "calls"  # the call ledger: <session>/<tool_use_id>.json, one per open shell call


TEAM_POLICY = core.HANDOFF / "policy" / "progress.json"  # the team's ceiling on max_parallel


def cfg() -> dict:
    """The progress section: devkit's defaults (core.DEFAULTS), with each valid value of the configuration; a wrong
    value keeps its default (`dev config validate` names it)."""
    return core.progress_config(core.CFG.get("progress") or {})[0]


# --------------------------------------------------------------------------- which session am I?

def current_session(cwd: str | None = None):
    """-> (agent, session id) of the agent session running this command; see resolve_session."""
    return resolve_session(cwd)[:2]


def resolve_session(cwd: str | None = None, argv=None):
    """-> (agent, session id, how) of the session running this command:
    ancestry  the nearest parent process whose pid a session record holds (the agent's own process, recorded at
              SessionStart), so two sessions in one repository each find their own; of that process's sessions, the
              one whose call ledger holds an open call for this very command, else the one DEVKIT_SESSION names
              (after /clear), else the newest. Codex's shared process instead requires a consistent explicit root,
              unique child ownership or an exact executable call when several threads are registered
    ledger    no session of this process holds an open call for it, and exactly one recent session of this
              repository that no live process holds does: Claude Code gave the running conversation a new session id
              (a compaction), and the older record still holds the process until the new one registers it
    env       DEVKIT_SESSION, which Claude Code exports to the session's shells (no parent matched)
    recency   the most recently active devkit session in the same repository, for a process that carries an agent's
              marker (core.AGENT_MARKERS) or whose parents devkit could not read (a detached command, whose parent is
              launchd): a Codex thread whose process devkit never recorded, a detached command of a session. A guess:
              it never makes the caller the tech lead (identity._find)
    shell     none of these, or a process whose parents devkit read, none of them a session's process, with no
              agent's marker in its environment (Claude Code and Codex mark every command they run): the developer's
              own terminal, a shell of the developer's with a session of its own, however recently an agent session
              worked in this repository
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
    cwd = cwd or os.getcwd()
    if mine:
        if all(c[1] == "codex" for c in mine):
            return "codex", _codex_process_session(mine, env, args, cwd), "ancestry"
        # the Codex app runs several threads in one process, so the pid names them all: the thread Codex names to
        # the command (CODEX_THREAD_ID) is the one, when this process runs it
        thread = os.environ.get("CODEX_THREAD_ID", "")
        named = [c for c in mine if c[1] == "codex" and c[2] == thread] if thread else []
        if named:
            return named[0][1], named[0][2], "ancestry"
        typed = [c for c in mine if _ledger_holds(c[2], args)]
        if len(typed) > 1:
            here = _same_repo_sessions(typed, cwd)
            typed = here or typed
        if typed:
            _, agent, sid = max(typed)
            return agent, sid, "ancestry"
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
    if pids and not any(os.environ.get(k) for k in core.AGENT_MARKERS):  # the developer's own terminal
        return "human", f"shell-{os.getppid()}", "shell"
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
    best = None
    for f in SESSIONS.glob("*.json") if SESSIONS.exists() else []:
        s = core.read_json(f, {}) or {}
        if (s.get("top") or s.get("cwd")) == top and (not best or f.stat().st_mtime > best[0]):
            best = (f.stat().st_mtime, s.get("agent"), s.get("session"))
    if best and time.time() - best[0] < 12 * 3600:
        return best[1], best[2], "recency"
    return os.environ.get("DEVKIT_AGENT") or "human", f"shell-{os.getppid()}", "shell"


def _codex_process_session(mine: list, env: str, args: list, cwd: str) -> str:
    """Resolve threads sharing one Codex process without using recency or the working directory as identity.
    The hook's explicit root survives redaction and closed background calls; a child's recorded ownership must
    agree with it. Without a stamp, only one exact executable dev call can distinguish several roots."""
    roots = {c[2] for c in mine}
    named = env.removeprefix("codex:") if env.startswith("codex:") else ""
    thread = os.environ.get("CODEX_THREAD_ID", "")
    if env and (not named or named not in roots):
        raise CodexSessionError("the Codex session stamp does not name a session of this process; no run was selected")
    if named and thread in roots and named != thread:
        raise CodexSessionError("the Codex session stamp and thread identify different sessions; no run was selected")
    named = named or (thread if thread in roots else "")
    lane = (os.environ.get("DEVKIT_FROM_AGENT") or "").strip()[:80]
    if lane:
        owners = [sid for sid in roots if lane in ((load_run("codex", sid) or {}).get("agents") or {})
                  or any(e.get("caller") == lane for e in open_calls(sid))]
        if len(owners) != 1 or (named and owners[0] != named):
            raise CodexSessionError("cannot identify this Codex crew member's parent session uniquely: its lane must "
                                    "belong to exactly one session and agree with its root stamp; no run was selected")
        return owners[0]
    if named:
        return named
    typed = [sid for sid in roots if _codex_call_holds(sid, args, cwd)]
    if len(typed) == 1:
        return typed[0]
    if not typed and len(roots) == 1:
        return next(iter(roots))  # one registered thread: the process itself is unambiguous
    raise CodexSessionError("cannot identify this Codex session uniquely: run the command with its hook's session stamp; "
                            "several threads share this process and no unique exact call identifies it")


def _codex_call_holds(sid: str, args: list, cwd: str) -> bool:
    """An exact dev invocation in an open call, never text merely printed or quoted by another program.
    Redacted or truncated arguments may not identify a command, so they never choose another root."""
    if not args:
        return False
    from . import shellparse
    return any(shellparse.dev_args(argv) == args for entry in open_calls(sid) if not entry.get("cut")
               for argv, _ in shellparse.shell_calls(str(entry.get("cmd") or ""), cwd) or [])


def _same_repo_sessions(cands: list, cwd: str) -> list:
    """Of several sessions (started, agent, session id) one process runs, those of the repository the command runs in
    (their record's top, or cwd): two threads of one Codex process that typed the same command at once."""
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
    out = []
    for c in cands:
        rec = core.read_json(session_path(c[1], c[2]), {}) or {}
        if (rec.get("top") or rec.get("cwd")) == top:
            out.append(c)
    return out


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


def _ledger_sessions(args: list, pids=None) -> list:
    """Every session whose call ledger holds an open call that typed these arguments, [(agent, session id)].
    `pids`: only calls an agent process among them made (a call whose agent process was not recorded counts)."""
    if not [a for a in args if a]:
        return []
    try:
        dirs = sorted(d.name for d in CALLS.iterdir() if d.is_dir())
    except OSError:
        return []
    out = []
    for sid in dirs:
        hit = next((e for e in open_calls(sid) if _names_args(dict(e, cut=False), args) and
                    (pids is None or not isinstance(e.get("agent_pid"), int) or e["agent_pid"] in pids)), None)
        if hit:
            out.append((str(hit.get("agent") or ""), str(hit.get("session") or sid)))
    return out


def _pidless_typed(args: list, skip: set, pids: list, cwd: str):
    """The one recent session (12 hours) of this repository that no live process holds and whose call ledger holds an
    open call for this command -> (agent, session id), or None (none, or several). Only for a process that has a
    parent besides launchd: a detached command is never matched by the ledger alone."""
    if not pids:
        return None
    # only calls of an agent process this command runs below: the developer typing the same command in a terminal
    # while the agent's call is open is not that session
    hits = [(a, s) for a, s in _ledger_sessions(args, pids) if s not in skip]
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


_ANCESTRY: dict = {}  # {this process's pid: {"chain": its parents' pids, "args": {parent pid: command line}}}


def _ancestor_pids() -> list:
    """This process's parents, nearest first, up to launchd (one `ps` per process)."""
    if os.getpid() not in _ANCESTRY:
        procs, chain, pid = _ps(), [], os.getppid()
        while pid > 1 and pid not in chain and len(chain) < 64:
            chain.append(pid)
            pid = procs.get(pid, (0, ""))[0]
        _ANCESTRY[os.getpid()] = {"chain": chain, "args": {p: procs[p][1] for p in chain if p in procs}}
    return _ANCESTRY[os.getpid()]["chain"]


def _parents_of(pid: int):
    """-> (the parents of process `pid`, nearest first, up to launchd; {parent pid: command line} of those whose
    command line devkit read). From this process's own parents when `pid` is one of them, else from `ps` while `pid`
    still runs (a process that ended has no parents left to read)."""
    chain = _ancestor_pids()
    if pid in chain:
        return chain[chain.index(pid) + 1:], (_ANCESTRY.get(os.getpid()) or {}).get("args") or {}
    if not _pid_running(pid):
        return [], {}
    procs, above, p = _ps(), [], pid
    while p in procs and len(above) < 64:
        p = procs[p][0]
        if p <= 1 or p in above:
            break
        above.append(p)
    return above, {p: procs[p][1] for p in above if p in procs}


def nested(sess: dict) -> bool:
    """Does this session's agent process run below another agent session's process? Then another session started it
    (`claude -p`, `codex exec`, or any entrypoint set on a command line), whatever surface it recorded, and its
    prompts are that session's text. A parent counts when another session's record holds its pid and it is an agent
    process (core.is_agent_process), or devkit could not read its command line: a pid an ended session left behind
    that now names a shell or a terminal is no session. Codex threads of one process share its pid, never a parent."""
    pid = (sess or {}).get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    above, args = _parents_of(pid)
    cands = {p for p in above if p > 1 and (p not in args or core.is_agent_process(args[p]))}
    if not cands:
        return False
    me = ((sess or {}).get("agent"), (sess or {}).get("session"))
    for f in SESSIONS.glob("*.json") if SESSIONS.exists() else []:
        s = core.read_json(f, {}) or {}
        if isinstance(s, dict) and s.get("pid") in cands and (s.get("agent"), s.get("session")) != me:
            return True
    return False


def _terminal_of(pid):
    """The controlling terminal of process `pid` (`ttys001`), "" when it has none (`ps` prints `??`: a process a GUI
    app or launchd started, or one below an agent's Bash tool shell, which runs without one), None when ps gives no
    answer."""
    try:
        out = subprocess.run(["ps", "-o", "tty=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    tty = out.stdout.strip()
    if out.returncode != 0 or not tty:
        return None
    return "" if tty.strip("?-") == "" else tty


def _exe_path(pid) -> str:
    """The real executable of process `pid`, from the kernel (libproc's proc_pidpath), never its command line, which
    the process sets itself (`exec -a`). "" when it cannot be read."""
    try:
        import ctypes
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        buf = ctypes.create_string_buffer(4096)
        n = lib.proc_pidpath(int(pid), buf, ctypes.c_uint32(4096))
    except (OSError, AttributeError, TypeError, ValueError):
        return ""
    return buf.value.decode("utf-8", "replace") if n > 0 else ""


# An application's own executable: <Name>.app/Contents/MacOS/<exe>, in no other bundle (a helper app inside
# Contents/Frameworks, or a tool in Contents/Helpers, is not the application)
_APP_MAIN = re.compile(r"^/(?:[^/]+/)*[^/]+\.app/Contents/MacOS/[^/]+$")


def _app_main(path: str) -> bool:
    return bool(_APP_MAIN.fullmatch(path or "")) and path.count(".app/") == 1


_ATTACHED: dict = {}  # {pid: _attachment(pid)}, only for this process's own parents, whose pids cannot be reused


def _attachment(pid):
    """What the agent process `pid` runs attached to: "terminal" when it has a controlling terminal (`claude` or
    `claude -p` typed in a terminal, tmux, ssh), "app" when an application's own executable (_app_main, by its real
    path) is among its parents (Claude Desktop, VS Code, Cursor, the ChatGPT app), "" when it runs with neither: a
    background launch (`nohup … &`, `( … & )`) whose shell exited, so launchd adopted it and the nested-session
    rule sees no agent above it. None when devkit cannot see the process (it ended, or ps gave no answer)."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return None
    if pid in _ATTACHED:
        return _ATTACHED[pid]
    tty = _terminal_of(pid) if _pid_running(pid) else None
    if tty is None:
        return None
    if tty:
        got = "terminal"
    else:
        above, _ = _parents_of(pid)
        got = "app" if any(_app_main(_exe_path(p)) for p in above if p > 1) else ""
    if pid in _ancestor_pids():
        _ATTACHED[pid] = got
    return got


def detached(sess: dict) -> bool:
    """Does this session's agent process run with no terminal and no application above it (_attachment ""), so that
    nobody can be at its keyboard, whatever surface it inherited? Such a session is headless, and devkit cannot tell
    it from a nested one."""
    return _attachment((sess or {}).get("pid")) == ""


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


# The call ledger. The PreToolUse hook writes one entry per
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


# What a shell call may run and still write no file (call_reads): devkit's own commands that change only devkit's
# records (None: every subcommand; `dev crew sync` writes the generated agent files, so it never counts), and tools
# that read and run no code. A test run, a build, a script, an interpreter or a package manager runs code that can
# write: it is a writer, whatever the read-only lanes may run.
DEVKIT_READS = {"watch": None, "progress": None, "crew": None, "status": None,
                "session": ("ls", "inbox", "claims", "escalations")}
PURE_READS = frozenset({"cd", "pushd", "ls", "cat", "head", "tail", "grep", "rg", "wc", "find", "git"})
GIT_READS = frozenset({"status", "log", "diff", "show"})
FIND_RUNS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"})
_SUBSTITUTED = re.compile(r"[$<>]\(|`|\\\n")  # a substitution, or a continued line, runs what the words do not show


def _devkit_read(argv) -> bool:
    """True for one `dev` call of DEVKIT_READS, its prefixes included."""
    from . import shellparse
    dev = shellparse.dev_args(shellparse.unwrap(list(argv)))
    pos = shellparse.dev_words(dev) if dev is not None else []
    if not pos or pos[0] not in DEVKIT_READS or shellparse.crew_kind(dev) == "sync":
        return False
    subs = DEVKIT_READS[pos[0]]
    return subs is None or (len(pos) > 1 and pos[1] in subs)


def _pure_read(argv0, here) -> bool:
    """True for one command of PURE_READS that runs no code: git only as status, log, diff or show, find without
    FIND_RUNS, and each one as the read-only lanes' verdict lets it run (no `git -c`, `--output`, `rg --pre` or
    environment that makes it run another program)."""
    from . import readonly, shellparse
    argv = shellparse.unwrap(list(argv0))
    name = os.path.basename(argv[0]) if argv else ""
    if name not in PURE_READS or (name == "find" and FIND_RUNS & set(argv)):
        return False
    if name == "git":
        args, k = argv[1:], 0
        while k < len(args) and args[k].startswith("-"):  # git's own options; these four take the next word
            k += 2 if args[k] in ("-C", "-c", "--git-dir", "--work-tree") else 1
        if (args[k] if k < len(args) else "") not in GIT_READS:
            return False
    return readonly.program_write_reason(list(argv0), here) is None


def call_reads(cmd: str, cwd) -> bool:
    """True when a shell call writes no file, so it is nobody's alibi for a reading lane's write (scope_written):
    each part of it is one of devkit's own record commands (DEVKIT_READS) or a tool that only reads (_pure_read),
    with no redirect into a file, no substitution and no nested shell. False for anything else, and when devkit
    cannot parse it: the call may write, as every call could before calls had a verdict."""
    from . import readonly, shellparse
    try:
        segs = shellparse.split_commands(shellparse.strip_heredocs(cmd), stripped=True)
        if not segs or _SUBSTITUTED.search(cmd):
            return False
        here = str(cwd or os.getcwd())
        for argv0, redirs in segs:
            if {">|", ">>|"} & set(argv0) or not all(readonly.redirect_reads(r, here) for r in redirs):
                return False
            if not (_devkit_read(argv0) or _pure_read(argv0, here)):
                return False
            argv = shellparse.unwrap(list(argv0))
            if os.path.basename(argv[0]) in ("cd", "pushd") and len(argv) > 1 and argv[1] != "-":
                here = str(shellparse.resolve(argv[1], here))
        return True
    except Exception:
        return False


def call_opened(agent: str, data: dict, now: float | None = None, token_sha256: str = ""):
    """PreToolUse of a shell call or accepted Codex patch: its ledger entry {tool_use_id, session, agent, caller, depth,
    ts, ttl, cmd_sha256, cmd (redacted, at most CALL_TEXT), cut}, and reads: true when it writes no file
    (call_reads). `token_sha256`: the SHA-256 (64 hex characters) of the call token's nonce, written in the same
    atomic write, so an entry of a call that carries a token never exists without its hash. Raises ValueError for
    any other value."""
    if token_sha256 and not re.fullmatch(r"[0-9a-f]{64}", str(token_sha256)):
        raise ValueError("token_sha256 must be 64 lowercase hex characters")
    tid, sid = _safe_name(data.get("tool_use_id")), _safe_name(data.get("session_id"))
    inp = data.get("tool_input") or {}
    cmd = inp.get("command") or inp.get("cmd")  # Codex's exec_command sends its command as `cmd`
    edit_paths = _edit_paths(data) if agent == "codex" and data.get("tool_name") == "apply_patch" else []
    if edit_paths:
        cmd = "apply_patch"  # patch content is data, never an executable command or evidence of CLI authority
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
    redacted = text != cmd  # a word that reads as a secret (`ci-token=15m`) is no longer in the kept text
    cut = len(text) > CALL_TEXT
    if cut:
        text = text[:CALL_TEXT // 2] + " … " + text[-CALL_TEXT // 2:]
    where = _call_cwd(data)  # a Codex exec_command runs in its own workdir
    agent_proc = _call_agent_pid()
    # what the kept text lost, cut away or redacted, is matched by the hashes of the `dev` commands the call runs
    dev_args = _dev_arg_hashes(cmd, where) if (cut or redacted) and not edit_paths else []
    _write_private(CALLS / sid / f"{tid}.json", {
        "tool_use_id": tid, "session": sid, "agent": agent, "caller": aid or "main", "depth": depth,
        "ts": round(now, 3), "ttl": ttl, "cmd_sha256": hashlib.sha256(cmd.encode()).hexdigest(), "cmd": text,
        "cut": cut, **({"dev_args": dev_args} if dev_args else {}),
        **({"token_sha256": token_sha256} if token_sha256 else {}),
        **({"agent_pid": agent_proc} if isinstance(agent_proc, int) else {}),
        **({"edit_paths": edit_paths} if edit_paths else
           {"reads": True} if call_reads(cmd, where) else {})})


def _call_agent_pid():
    """The agent process (Claude Code, Codex) the hook that opens a call runs below: the nearest of this process's
    first parents whose command line is an agent's; None when devkit cannot tell. A `dev` command the call runs has
    that process among its own parents. The parents this process read already, else one small `ps` per parent (a
    few ms each, never the whole process table on every shell call)."""
    known = _ANCESTRY.get(os.getpid())
    if known:
        return next((p for p in known["chain"] if core.is_agent_process(known["args"].get(p, ""))), None)
    pid = os.getppid()
    for _ in range(4):  # the hook's shell, then the agent: an agent further up is not this hook's
        if pid <= 1:
            return None
        try:
            out = subprocess.run(["ps", "-o", "ppid=,args=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=2).stdout.split(None, 1)
        except (OSError, subprocess.SubprocessError):
            return None
        if not out or not out[0].isdigit():
            return None
        if core.is_agent_process(out[1] if len(out) > 1 else ""):
            return pid
        pid = int(out[0])
    return None


def _call_cwd(data: dict) -> str:
    """The folder a shell call runs in: a Codex call's `workdir` (relative to the hook's cwd), else that cwd."""
    cwd = data.get("cwd") or ""
    wd = (data.get("tool_input") or {}).get("workdir") if isinstance(data.get("tool_input"), dict) else None
    if isinstance(wd, str) and wd.strip():
        from . import shellparse
        return str(shellparse.resolve(wd, cwd or os.getcwd()))
    return cwd


def args_hash(args) -> str:
    """The sha256 of a `dev` invocation's exact argument list, every word in order (never a prefix)."""
    return hashlib.sha256(json.dumps([str(a) for a in args]).encode()).hexdigest()


def _dev_arg_hashes(cmd: str, cwd) -> list:
    """args_hash of each `dev` invocation a command runs, as the shell splits its words (shellparse.shell_calls): what a
    call whose text the ledger cuts keeps of its `dev` commands, so a long command is still matched by its own words,
    whole (a 7 KB `dev progress assign --scope …` lost the middle of its scope to the cut)."""
    try:
        from . import shellparse
        found = [shellparse.dev_args(argv) for argv, _ in shellparse.shell_calls(cmd, cwd) or []]
    except Exception:
        return []
    return list(dict.fromkeys(args_hash(d) for d in found if d))


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


SHELL_KEEP = 2 * CALL_TTL  # seconds the record of closed shell calls keeps one: past the longest call it can overlap
SHELL_MAX = 500  # closed shell calls the record keeps at most, newest last


def _shells_path(sid: str) -> Path:
    """The session's recently closed shell calls, beside its open ones (a dot-file: the ledger's readers skip it)."""
    return CALLS / _safe_name(sid) / ".shells.json"


def call_closed(data: dict):
    """PostToolUse or PostToolUseFailure of a shell call, or the hook refused it: its entry leaves the ledger. A call
    that ran stays in the record of closed shell calls for SHELL_KEEP, {caller, opened, closed, reads (when it
    writes no file, call_reads)}, so write detection knows who else had a writing shell call during a reading lane's
    call even when it closed first (scope_written). A call refused at PreToolUse never ran and is not kept. Under the
    record's lock; a busy lock drops this one record."""
    tid, sid = _safe_name(data.get("tool_use_id")), _safe_name(data.get("session_id"))
    if not (tid and sid):
        return
    entry = CALLS / sid / f"{tid}.json"
    e = core.read_json(entry, None) if data.get("hook_event_name") != "PreToolUse" else None
    try:
        entry.unlink()
    except OSError:
        pass
    if not isinstance(e, dict) or not e.get("ts"):
        return
    now = time.time()

    def add(doc):
        kept = [c for c in doc.get("calls") or [] if isinstance(c, dict) and now - (c.get("closed") or 0) <= SHELL_KEEP]
        doc["calls"] = (kept + [{"caller": str(e.get("caller") or "main")[:80], "opened": e["ts"],
                                 "closed": round(now, 3), **({"reads": True} if e.get("reads") else {}),
                                 **({"edit_paths": e["edit_paths"]} if e.get("agent") == "codex" and
                                    e.get("edit_paths") else {})}])[-SHELL_MAX:]
    try:
        state.update(_shells_path(sid), add)
    except state.StateBusy:
        pass


def shells_since(sid: str, since: float, now: float | None = None) -> list:
    """The session's shell calls that closed at `since` or later and had opened by `now`: [{caller, opened,
    closed, reads}], the closed calls that overlap that window."""
    now = now or time.time()
    doc = core.read_json(_shells_path(sid), {}) or {}
    return [c for c in doc.get("calls") or [] if isinstance(c, dict)
            and (c.get("closed") or 0) >= since and (c.get("opened") or 0) <= now]


EDIT_TOOLS = ("Write", "Edit", "MultiEdit")  # the tools that name the one file they change (tool_input.file_path)
_EDIT_TEMP = re.compile(r"\.tmp\.\d+\.[0-9a-f]+$")  # the temporary file an edit tool writes, then renames into place
EDIT_SLACK = 2.0  # seconds an edit may precede a shell call's start and still fall in its window (hook ordering)
EDIT_KEEP = 12 * 3600  # seconds the edit record keeps a call: a lane's whole life, which the crew gates read
EDIT_MAX = 1000  # edit calls the record keeps at most, newest last


def _edits_path(sid: str) -> Path:
    """The session's recent edit calls, beside its open calls (a dot-file: the ledger's readers skip it)."""
    return CALLS / _safe_name(sid) / ".edits.json"


PATCH_FILE = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+?)\s*$", re.M)  # Codex apply_patch


def _edit_paths(data: dict) -> list:
    """The files an edit call changed: a Write, Edit or MultiEdit's file_path, or each file a Codex apply_patch names
    (its Add, Update, Delete and Move lines), a relative one under the folder the patch ran in. [] for other calls."""
    inp, tool = data.get("tool_input") or {}, data.get("tool_name")
    if tool in EDIT_TOOLS:
        path = inp.get("file_path")
        return [path] if isinstance(path, str) and path.strip() else []
    if tool != "apply_patch":
        return []
    body = inp.get("input") or inp.get("patch") or inp.get("command") or ""
    if isinstance(body, list):
        body = "\n".join(str(x) for x in body)
    base = str(inp.get("workdir") or data.get("cwd") or "")
    return [p if os.path.isabs(p) or not base else os.path.join(base, p) for p in PATCH_FILE.findall(str(body))]


def note_edit(agent: str, data: dict, now: float | None = None):
    """PostToolUse of a Write, Edit or MultiEdit, or of a Codex apply_patch: each file it changed, who changed it (the
    agent_id, or main) and when, kept for EDIT_KEEP, so write detection credits a file to whoever really edited it
    (scope_written) and the crew gates credit a member only with what it wrote (gates.crew_gates). Under the record's
    lock; a busy lock drops this one record (state audits it)."""
    response = data.get("tool_response")
    if agent == "codex" and (data.get("error") or data.get("is_error") or isinstance(response, dict) and
                             (response.get("isError") or response.get("error") or response.get("success") is False)):
        return  # an explicitly failed edit may have written, but its target list is not proof of authorship
    paths, sid = _edit_paths(data), _safe_name(data.get("session_id"))
    if not sid or not paths:
        return
    now = now or time.time()
    who = str(data.get("agent_id") or "main")[:80]
    tid = _safe_name(data.get("tool_use_id"))

    def add(doc):
        kept = [e for e in doc.get("edits") or [] if isinstance(e, dict) and now - (e.get("ts") or 0) <= EDIT_KEEP]
        doc["edits"] = (kept + [{"ts": round(now, 3), "caller": who, "path": p[:1024]} for p in paths])[-EDIT_MAX:]
        if tid:  # the record of the call in flight gives way to the record of its result
            doc["open"] = [e for e in doc.get("open") or [] if isinstance(e, dict) and e.get("tid") != tid]
    try:
        state.update(_edits_path(sid), add)
    except state.StateBusy:
        pass


def edit_opened(agent: str, data: dict, now: float | None = None):
    """PreToolUse of a Write, Edit or MultiEdit: its file and who changes it, as an edit in flight
    {tid, ts, caller, path} (edits_open). The tool writes the file before its PostToolUse records the result
    (note_edit, which takes this record's place), and a reading lane's check that runs in between must see that
    another writer was at work (scope_written). A refused call never opens one; a call that failed drops it
    (edit_dropped). Under the record's lock; a busy lock drops this one record."""
    if data.get("tool_name") not in EDIT_TOOLS:
        return
    paths, sid, tid = _edit_paths(data), _safe_name(data.get("session_id")), _safe_name(data.get("tool_use_id"))
    if not (sid and tid and paths):
        return
    now = now or time.time()
    who = str(data.get("agent_id") or "main")[:80]

    def add(doc):
        kept = [e for e in doc.get("open") or [] if isinstance(e, dict) and now - (e.get("ts") or 0) <= CALL_TTL
                and e.get("tid") != tid]
        doc["open"] = (kept + [{"tid": tid, "ts": round(now, 3), "caller": who, "path": p[:1024]}
                               for p in paths])[-EDIT_MAX:]
    try:
        state.update(_edits_path(sid), add)
    except state.StateBusy:
        pass


def edit_dropped(data: dict):
    """An edit call in flight that will not write: refused, or it failed (PostToolUseFailure). Its record goes."""
    sid, tid = _safe_name(data.get("session_id")), _safe_name(data.get("tool_use_id"))
    if not (sid and tid) or not _edits_path(sid).exists():
        return

    def drop(doc):
        doc["open"] = [e for e in doc.get("open") or [] if isinstance(e, dict) and e.get("tid") != tid]
    try:
        state.update(_edits_path(sid), drop)
    except state.StateBusy:
        pass


def edits_open(sid: str, now: float | None = None) -> list:
    """The session's edit calls in flight (edit_opened): [{tid, ts, caller, path}], none older than CALL_TTL, the
    longest a call waits for its result."""
    now = now or time.time()
    doc = core.read_json(_edits_path(sid), {}) or {}
    return [e for e in doc.get("open") or [] if isinstance(e, dict) and now - (e.get("ts") or 0) <= CALL_TTL]


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
    if entry.get("agent") == "codex" and "edit_paths" in entry:
        return False
    if entry.get("cut") and entry.get("caller") != "main":
        return True
    if args and args_hash(args) in (entry.get("dev_args") or []):  # a cut call's own dev command, word for word
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
            i = _find_word(text, a, at)
            if i < 0:
                break
            at = i + len(a)
        else:
            return True
    return not entry.get("cut") and _runs_with_expansions(texts[0], args)


_WORD_EDGE = " \t\n'\"`;&|(){}<>"  # what may stand next to a whole shell word: space, a quote, a shell operator


def _find_word(text: str, word: str, at: int) -> int:
    """Where `word` stands in `text`, from `at`, as a whole shell word: the start of the text or a _WORD_EDGE before
    it, the end or one after it ("task" in `dev task`, never in `tests/test_task_numbers.py`); -1 when it does not."""
    i = text.find(word, at)
    while i >= 0:
        end = i + len(word)
        if (i == 0 or text[i - 1] in _WORD_EDGE) and (end == len(text) or text[end] in _WORD_EDGE):
            return i
        i = text.find(word, i + 1)
    return -1


_EXPANSION = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)?|\$[A-Za-z_]\w*|\$[0-9@*#?$!-]|`[^`]*`?|\$")
# The commands whose arguments may come from a shell expansion: they read or acknowledge the caller's own messages
# and decide nothing. A command that decides, answers or assigns needs its words as typed, so that no other process
# running while the tech lead's loop is open can pass as the tech lead with words of its own.
EXPANDED_COMMANDS = (("crew", "read"), ("crew", "ack"), ("session", "read"), ("session", "ack"))


def _runs_with_expansions(text: str, args: list) -> bool:
    """Does the call's text run `dev` with these arguments where it typed a shell expansion in their place (a loop's
    `"$ref"`, `${id}`, `$(…)`)? Only for EXPANDED_COMMANDS, typed as they are. Each other word must be the argument
    as typed, or a pattern whose expansions stand for any text."""
    if tuple(args[:2]) not in EXPANDED_COMMANDS or not any(c in text for c in "$`"):
        return False
    from . import shellparse
    try:
        calls = shellparse.shell_calls(text, None) or []
    except Exception:
        return False
    for argv, _ in calls:
        words = shellparse.dev_args(argv)
        if not words or len(words) != len(args) or words[:2] != args[:2]:
            continue
        if all(w == a or (_EXPANSION.search(w) and re.fullmatch(
                "".join(".*" if _EXPANSION.fullmatch(p) else re.escape(p)
                        for p in re.split(f"({_EXPANSION.pattern})", w) if p), a, re.S))
               for w, a in zip(words, args)):
            return True
    return False


def caller(cwd: str | None = None, argv=None, now: float | None = None) -> dict:
    """Who runs this `dev` command, as the commands only the tech lead may run read it (identity.speaker, the one
    definition) -> {agent, session, how, caller, calls}. caller: main (an open call of main's typed it, in a session
    not guessed by recency: a lane variable, the tech lead's stamp, a parent process or recency never make it main),
    a member's agent_id (its lane mark, else the most restrictive member whose open call typed it), unknown,
    unverified (Codex, whose main thread's hooks carry no mark), or human (a developer's shell with no agent session).
    how: how the session was found (resolve_session). `argv`: the command's arguments, default sys.argv[1:]."""
    from . import identity
    found = identity._find(cwd, argv, now, settle=False)
    sp = found.speaker
    out = {"agent": found.agent, "session": sp.session, "how": found.session_how, "caller": "unknown", "calls": []}
    if sp.how == "human":
        return dict(out, caller="human")
    if getattr(found, "refusal", ""):  # identity refuses this call (a failed or missing call token)
        return dict(out, caller="unknown", why=found.refusal)
    if found.agent == "codex" and sp.how not in ("token", "thread", "text-pinned"):
        return dict(out, caller="unverified")
    out["calls"] = list(found.calls)
    if sp.how == "stamped" and sp.lane != "main":
        out["caller"] = sp.lane
    elif found.ledger:
        out["caller"] = found.ledger
    return out


def _as_lane(from_agent, cwd: str | None = None):
    """The lane a command acts as: `from_agent` (DEVKIT_FROM_AGENT, as the command passes it), else the crew member
    identity.speaker names: its lane mark, or its open call in the ledger (Claude Code and Codex alike). Variables
    only narrow: an empty or missing one grants nothing. None for Morpheus."""
    from . import identity
    if from_agent:  # the lane variable counts for a member whose call is open (identity.verified_lane): a command
        # may have given it another member's id; a lane a caller in devkit's own code names is that caller's
        return identity.verified_lane(from_agent, cwd) if from_agent == os.environ.get(identity.LANE_VAR) else \
            from_agent
    lane = identity.acting_lane(cwd)  # refuses a command several members' open calls hold alike
    return lane if lane and lane not in NOT_A_LANE else None


def run_path(agent: str, sid: str) -> Path:
    return RUNS / f"{agent}-{sid}.json"


def session_path(agent: str, sid: str) -> Path:
    return SESSIONS / f"{agent}-{sid}.json"


def load_run(agent: str, sid: str):
    """The session's run, or None. Inside the record's lock (a writer that will save it), a run that does not parse
    raises state.StateBusy instead of reading as missing, so it is never written over."""
    p = run_path(agent, sid)
    from . import messaging
    run = state.load_bounded(p, messaging.STORE_MAX)
    if not isinstance(run, dict):
        return run
    canonical = state.load_bounded(_message_path(messaging.run_id(run)), messaging.STORE_MAX)
    if canonical:
        if canonical.get("messaging_schema") != 2:
            raise messaging.MessageError("corrupt_state", "upgrade and restart for this messaging schema")
        canonical = messaging.migrate(canonical)
        for field in ("messages", "decisions"):
            run[field] = _merged(canonical.get(field) or [], run.get(field), field)
        for field in ("run_id", "messaging_schema"):
            run[field] = canonical.get(field)
    return run


# what an older devkit's hooks turn on in the run file (a session not restarted after an upgrade knows no canonical
# store): flags that only ever turn on, and the reason that goes with closing
_PROJECTED_FLAGS = {"messages": ("answered", "delivered", "closed"), "decisions": ("told", "surfaced")}


def _merged(canonical: list, projected, field: str) -> list:
    """The canonical messages or decisions, with what an older devkit's hook wrote to the run file alone: an item only
    the run file has (one that reuses a stored id for other text gets the next free id), and a flag it turned on. That
    hook showed a message whole when it delivered it, so it never comes back as unread (migrated_delivered)."""
    out = [dict(item) for item in canonical if isinstance(item, dict)]
    by_id = {item.get("id"): item for item in out}
    body = "text" if field == "messages" else "question"
    for item in projected if isinstance(projected, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        known = by_id.get(item["id"])
        if known is not None and known.get(body) != item.get(body):
            item, known = dict(item, id=_next_id(out, item["id"][:1])), None
            for stale in ("ref", "seq", "schema", "body_bytes", "body_sha256", "run_id"):
                item.pop(stale, None)
        if known is None:
            out.append(dict(item))
            by_id[item["id"]] = out[-1]
            continue
        for flag in _PROJECTED_FLAGS[field]:
            if item.get(flag) and not known.get(flag):
                known[flag] = item[flag]
                if flag == "closed" and item.get("reason"):
                    known.setdefault("reason", item["reason"])
                if flag == "delivered":
                    known["migrated_delivered"] = True
    return out


def _message_path(run_id):
    return RUNS.parent / "messaging-v2" / "runs" / (run_id + ".json")


def _archive_run(run):
    from . import messaging
    doc = messaging.migrate(run)
    path = RUNS.parent / "messaging-v2" / "archive" / (doc["run_id"] + ".json")
    state.replace(path, doc, durable=True, max_bytes=messaging.STORE_MAX)
    if state.load_bounded(path, messaging.STORE_MAX) != doc:
        raise messaging.MessageError("persistence_uncertain", "archive verification failed; original run retained")


def _tx(agent: str, sid: str):
    """The session's lock around `load_run → change → _save` (state.hold): the run is read and written inside one
    hold, so no other hook or command changes it in between. No git, subprocess or network call inside it."""
    return state.hold(run_path(agent, sid))


def _settle(run: dict):
    """What every save keeps true, whichever path changed the run (a command, a hook, the reconciler): a
    ready-to-launch notice belongs to a pending step, so a step that leaves pending is announced again once it is
    ready again; and a run blocked on a step (`blocked_on`) stays blocked only while that step is."""
    for message in run.get("messages") or []:
        target = message.get("pending_launch")
        if target and str(message.get("to", "")).startswith("name:"):
            matches = [aid for aid, lane in (run.get("agents") or {}).items()
                       if not lane.get("finished") and lane.get("started", 0) >= message.get("created_at", message.get("ts", 0))
                       and _label_matches(target, lane)]
            if len(matches) == 1:
                message["to"] = matches[0]
                message["resolved_launch"] = matches[0]
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
    """Write the run atomically through state, settled first (_settle) and in the coordination shape (_coord_shape).
    The caller loaded it inside the same _tx."""
    _coord_shape(run)
    _settle(run)
    p = run_path(run["agent"], run["session"])
    with state.hold(p):
        from . import messaging
        canonical = messaging.migrate(run)
        try:
            state.replace(_message_path(canonical["run_id"]), canonical,
                          durable=True, max_bytes=messaging.STORE_MAX)
        except (OSError, state.StateBusy) as error:
            raise messaging.MessageError("persistence_uncertain", "retry the same request key; " + str(error)) from error
        run.update(canonical)
        try:
            state.replace(p, run, durable=True, max_bytes=messaging.STORE_MAX)
        except OSError as error:
            raise messaging.MessageError("persistence_uncertain", "canonical content committed; retry the same request key") from error
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
        if m:  # split into steps when the plan is known (_resolve_after): a step's own name may hold "and"
            raw, after = m.group(1).strip(), [m.group(2).strip()]
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
#              an unverified one is too, since that review is what approves it
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
    """A review step, by the one rule approvals.is_review keeps: its kind of work is review, or the member
    it is assigned to works review-only. The same rule decides whether its ✓ verifies the work it follows
    (_verified_by_review) and approves it (approvals.from_review). A RACI that cannot be read makes no step a review
    step, so the step keeps waiting."""
    from . import approvals
    return approvals.is_review(s)


def deps_open(run, s) -> list:
    """The steps `s` waits for that are not done or skipped yet, in the order of its `after`. A review step does not
    wait for an unverified one: its Check is the verdict that approves that work."""
    return coord.deps_open(run, s, review=is_review_step)


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


AFTER_SPLIT = re.compile(r"[,&]|\band\b")  # "after a, b & c and d": the steps it waits for


def _after_refs(plan, refs) -> list:
    """`after` texts -> one reference per step: a text that is a step's whole name stays one, so a name that holds
    "and" can be named; any other text splits at commas, "&" and "and"."""
    out = []
    for ref in refs or []:
        r = str(ref).strip()
        if any(x["name"].lower() == r.lower() for x in plan):
            out.append(r)
        else:
            out += [p.strip() for p in AFTER_SPLIT.split(r) if p.strip()]
    return out


def _resolve_after(plan, s, refs) -> list:
    """`after` as written (a step's name, its number, or a part of its name that only one step has; several joined
    by commas, "&" or "and") -> the names of the steps of `plan` it means. Raises StepError for one that is not a
    step of the plan or matches several."""
    out = []
    for ref in _after_refs(plan, refs):
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


def _coord_shape(run: dict):
    """The run as the coordination rules read it, written on every save: a pending step's legacy queue entry
    (`waits_lane`) becomes its intent, a step that is not pending keeps none, and a lane keeps no hand-over; its
    `scope` and `checkout` mirror the steps it runs (lane_scope derives its authority from those steps, not from
    the mirror); its `checkout` is that of those steps, which the
    edit advice counts the scope from. A run from an older devkit continues with no other change."""
    for s in run.get("plan") or []:
        if not isinstance(s, dict):
            continue
        if s.get("state") == "pending" and "waits_lane" in s:
            it = coord.intent(s)
            s.pop("waits_lane", None)
            if it:
                s["intent"] = it
        elif s.get("state") != "pending":
            coord.drop_intent(s)
    for aid, lane in (run.get("agents") or {}).items():
        if not isinstance(lane, dict):
            continue
        if run.get("plan"):  # a run without a plan keeps a lane's scope as given (lane_scope)
            held = coord.scope(run, aid)
            if held:  # a mirror of the steps it runs, for readers of the record; never read for authority
                lane["scope"] = held
            else:
                lane.pop("scope", None)
        lane.pop("handed_over", None)
        home = _lane_checkout(run, aid)
        if home:
            lane["checkout"] = home
        else:
            lane.pop("checkout", None)


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
        ratio = run.get("cal_ratio")
        run["estimate_cal"] = round(run["estimate"] * ratio, 1) if ratio else run["estimate"]


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


def _bind_run_project(agent: str, sid: str, cwd: str | None, project: str):
    """Bind a registered projectless Codex session to its own observed repository.

    A main progress run or an executed main tool call supplies the checkout. Keep the host's cwd: app chats may
    live outside the checkout. Never move a session that already names a
    repository, and never manufacture a session record for a command whose SessionStart was not observed.
    """
    if agent != "codex" or not project:
        return
    path = session_path(agent, sid)
    rec = core.read_json(path, {}) or {}
    if not isinstance(rec, dict) or not rec or rec.get("project") or rec.get("top"):
        return
    top = core.gitout(cwd or os.getcwd(), "rev-parse", "--show-toplevel")
    if not top or core.is_temp(top):
        return
    facts = {"project": project, "top": top,
             "branch": core.gitout(top, "branch", "--show-current"),
             "head": core.gitout(top, "rev-parse", "HEAD")}

    def bind(d):
        if d.get("project") or d.get("top"):
            return state.SKIP
        d.update(facts)
    state.update(path, bind, create=False)


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
    """The plan's steps in `state` as "2. parser findings, 4. welcome research" (their number in the plan, as the
    checklist under the status block shows them), at most STEPS_NAMED and "+N" for the rest; '' when none."""
    hits = [f"{i}. {_short(s.get('name'))}" for i, s in enumerate(plan, 1) if s.get("state") == state]
    if len(hits) > STEPS_NAMED:
        return ", ".join(hits[:STEPS_NAMED]) + f" +{len(hits) - STEPS_NAMED}"
    return ", ".join(hits)


def headline(run: dict, now: float | None = None, states=None) -> str:
    """The status block's Progress line, also what dev status, dev crew and the dashboard show for a run:
    '14 of 21 steps · 11 skipped · now 28. parser findings, 33. welcome research · 42m active (1h05m wall) of ~3h00m
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
    """A new run for this session. A live run is replaced only with `replace` and a reason. From a crew
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
           "mode": mode if mode else mode_of(None), "mode_source": "run" if mode else "config"}
    mode_of(run)  # validate before saving any run state
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
            if not mode and _chosen(live) and live.get("mode") in ("auto", "guided"):
                # the mode the tech lead set for the live run stays: replacing the plan is not a change of mode
                run["mode"], run["mode_source"] = live["mode"], "run"
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
        if live:
            _archive_run(live)
        _save(run)
    _bind_run_project(agent, sid, cwd, project)
    _publish()
    return run


def _lane(run, agent_id, now, label="", kind=""):
    lane = run["agents"].setdefault(agent_id, {"label": label, "type": kind, "started": now, "finished": None,
                                               "state": "running", "updates": [], "done": 0,
                                               "assigned_steps": []})
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


def _named_by(plan: list, text: str, closed: bool = False):
    """The open step (with `closed`, the done or skipped one) a description names by starting with its exact name
    and a colon ("api: the export endpoint"), else None."""
    m = re.match(r"\s*([^:\n]{1,120}):\s", text or "")
    name = m.group(1).strip().lower() if m else ""
    return next((s for s in plan if name and s["name"].lower() == name and (s.get("state") in DONE) == closed),
                None)


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
    """Keep one main step running: the first ready one (its dependencies done) not handed to a subagent. Before that,
    each lane that is free claims what waits for it (_claim_free_lanes)."""
    _claim_free_lanes(run, now)
    plan = run.get("plan") or []
    if any(s.get("state") == "running" and (s.get("owner") or "main") == "main" for s in plan):
        return
    nxt = next((s for s in ready(run) if (s.get("owner") or "main") == "main" and not s.get("label")), None)
    if nxt:
        nxt.update(state="running", started=now)
        coord._history(run, nxt, "pending", "running", "main", "the tech lead's next ready step", now)


def _main_only(from_agent, what: str, cwd: str | None = None):
    """Only Morpheus (the main session) changes the main plan, sets the mode and records the developer's answers; a
    crew member asks him. The call ledger must name main: a lane variable, a parent process or recency
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
               f"verify who calls yet", "allowed")
        return
    if who.get("why"):
        raise StepError(f"only the tech lead {what}: {who['why']}")
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
                        f"command itself, not through a script or another shell, as a command of its own")
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
           from_agent: str | None = None, why: str = "", activity: str = "") -> dict:
    """Hand a plan step to a subagent: the step keeps an intent for the member, or for the one lane its launch
    description names, and the first of the member's working lanes that is free takes it, oldest intent first
    (_route_to_lane; never a lane busy with another step, nor one whose member ended or went stale, working_agents,
    which takes it when it works again); its time is that subagent's time. `scope`: the files it owns (comma-separated globs); the pre-tool hook advises that subagent when
    it edits outside them, and `dev crew owner` names it as their holder. The crew's RACI must make the member
    Responsible for the step's kind of work; `why` overrides it, recorded. `activity`: the step's kind of work, an id
    from dev crew raci, which then decides whatever language the step is written in."""
    _main_only(from_agent, "assigns the plan's steps", cwd)
    activity = _activity_id(activity) if activity else ""
    globs = [x.strip() for x in scope.split(",") if x.strip()] if scope else []
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise ValueError(f"no step “{which}” in the plan")
        checkout = _scope_checkout(globs, run.get("project") or "", cwd)
        globs, rewrites = _scope_globs(globs, run.get("project") or "", cwd)
        _tell_rewrites(run, s["name"], rewrites, now)
        _raci_assign(run, s, label, why, now, activity=activity, files=globs or None)
        owner = s.get("owner") or "main"
        if owner in run.get("agents", {}):
            _remember_step(run["agents"][owner], s)
        agents = run.get("agents") or {}
        held = [owner] if owner != "main" else [k for k in _queued_lanes(s) if k in agents]  # owns it, or queues it
        if held and s.get("state") != "done" and not any(_label_matches(label, agents.get(k) or {}) for k in held):
            old = (agents.get(held[0]) or {}).get("persona") or held[0]
            _log_change(run, s, s.get("state"), "pending", "main", f"reassigned from {old} to {label}", now)
            if owner == "main":  # it was queued for those lanes, whose member was told it would join them
                _tell_unqueued(run, s, held, label, now)
            s.update(owner="main", state="pending")  # the other member's lane no longer owns it
            coord.drop_intent(s)  # nor takes it when its open step closes
        s["label"] = label
        s["assigned_at"] = round(now, 3)  # a step assigned after the developer's mid-turn message is their request
        live = working_agents(run, now, own=True)  # one that ended or went stale takes no step and holds no files
        if globs:
            top = checkout or _scope_top(run, cwd)
            for aid, lane in live:
                held = coord.scope(run, aid)  # the files of the steps it runs, never a stored copy
                if not _label_matches(label, lane) and _same_checkout(checkout, _lane_checkout(run, aid)):
                    clash = list(dict.fromkeys(g for g in globs for h in held if _globs_overlap(g, h, top)))
                    if clash:
                        from . import crew
                        raise StepError(f"{', '.join(clash)} overlaps the files "
                                        f"{crew.who(lane.get('persona') or lane.get('label'))} owns while working "
                                        f"({', '.join(held)}). Give each member different files, or "
                                        f"wait until they finish; for members in different worktrees, give each "
                                        f"scope as paths in its worktree (<worktree>/lib/**): scopes of two "
                                        f"checkouts do not clash.")
            s["scope"] = globs
            if checkout:
                s["checkout"] = checkout
            else:
                s.pop("checkout", None)
        unbound = [k for k, v in live if v.get("binding") == "unbound" and _label_matches(label, v)]
        if len(unbound) == 1:  # Morpheus confirms the one lane with no launch record his assignment names
            run["agents"][unbound[0]]["binding"] = "confirmed"
            _audit("crew.confirm", run["agents"][unbound[0]].get("persona") or label, f"Morpheus assigned "
                   f"“{s['name']}” to a lane without a launch record", project=run.get("project") or "",
                   agent_id=unbound[0][:16])
        _route_to_lane(run, s, label, live, now)
        _advance_main(run, now)
        _save(run)
    return run


def _scope_top(run: dict, cwd: str | None) -> str:
    """The checkout a run's repository-relative scope globs count from (lanescope.scope_base)."""
    try:
        from . import lanescope
        return lanescope.scope_base(run.get("project") or "", cwd or os.getcwd())
    except Exception:
        return ""


def _lane_checkout(run: dict, aid: str) -> str:
    """The checkout the scope of lane `aid` counts in: that of the steps it runs (_scope_checkout), '' for none."""
    return next((x.get("checkout") for x in coord.held(run, aid) if x.get("checkout")), "")


def _tell_unqueued(run: dict, s: dict, lanes: list, label: str, now: float):
    """Step `s`, queued for `lanes` (waits_lane), now goes to `label`: each of those lanes that still runs gets a
    devkit notice naming the step and its new owner, which its next hook and `dev watch --wait` deliver, so its
    member stops expecting the step it was told would join its lane (_route_to_lane)."""
    from . import crew
    agents = run.get("agents") or {}
    for aid in lanes:
        if (agents.get(aid) or {}).get("state") != "running":
            continue
        cur = _open_step_of(run, aid, s)
        _post(run, "devkit", aid, f"devkit: the tech lead gave “{s['name']}” to {crew.who(label)}, so it no longer "
              f"joins your lane" + (f"; go on with “{cur['name']}”" if cur else ""), now, from_name="devkit",
              kind="notice")


def _route_to_lane(run: dict, s: dict, label: str, live: list, now: float):
    """Step `s`, just assigned to `label` or reopened, goes to its member by an intent kept on the step
    (coord.set_intent): bound to one lane when `label` is the launch description of exactly one of the member's
    lanes, live or ended, or when it is reopened for the live lane that held it (its owner on entry); else to the
    member, whichever of its lanes claims it first. Then each live lane of the member (`live`: working_agents) that
    is free claims (_claim_lane), so the step runs now when one is free and its dependencies are done. Otherwise it
    waits in its intent, which outlives the member's lanes: the lane that frees first, a resume, or a new launch that
    names it takes it, oldest intent first. A closed step goes to no lane: reopening it gives it back. A blocked step
    keeps waiting for its decision. Morpheus and the member are told why it waits. -> the lane that took it, or None."""
    from . import crew
    s["assignment_revision"] = (s.get("assignment_revision") or 0) + 1
    agents = run.get("agents") or {}
    lanes = [k for k, v in live if _label_matches(label, v) and (v.get("parent") or "main") == "main"]
    if s.get("state") in DONE + ("unverified",):
        coord.drop_intent(s)
        if lanes:
            _post(run, "devkit", "main", f"“{s['name']}” is {s.get('state')}, so {crew.who(label)}'s lane does not "
                  f"take it again: to have them work on it again, reopen it (dev progress reopen \"{s['name']}\" "
                  f"--why \"…\"), and their lane takes it with its scope", now, from_name="devkit")
        return None
    owner = s.get("owner") or "main"
    if s.get("state") == "running" and owner == "main":
        s["state"] = "pending"  # the tech lead's own step goes to its member
        coord._history(run, s, "running", "pending", "main", f"assigned to {label}", now)
    if s.get("state") == "blocked":
        return None  # it waits for its decision; the answer returns it to its lane (coord.unblock)
    if s.get("state") == "running" and owner in agents and _label_matches(label, agents[owner]):
        coord.drop_intent(s)
        return owner  # its member's lane works on it already
    named = [k for k, v in agents.items() if ":" in label and (v.get("parent") or "main") == "main"
             and label.strip().lower() == (v.get("label") or "").strip().lower() and _label_matches(label, v)]
    bound = owner if owner in lanes else named[0] if len(named) == 1 else None
    coord.set_intent(run, s, coord.member_of(label) or label, bound, now)
    for k in ([bound] if bound in lanes else []) + [k for k in lanes if k != bound]:
        _claim_lane(run, k, now)
        if s.get("state") == "running":
            return s.get("owner")
    if bound in lanes or (not bound and len(lanes) == 1):
        aid = bound if bound in lanes else lanes[0]
        before = _closes_before(run, aid, s)
        if before:
            _post(run, "devkit", aid, f"devkit: the tech lead gave you “{s['name']}”: it joins your lane once "
                  f"{_quoted_and(before)} close{'s' if len(before) == 1 else ''} (dev progress step \"…\" --step "
                  f"\"{before[0]}\" --check \"…\"), and its files become your scope then", now, from_name="devkit",
                  kind="notice")
    if lanes and not deps_open(run, s):
        _post(run, "devkit", "main", lane_wait(run, s, live), now, from_name="devkit")
    return None


def _quoted_and(names: list) -> str:
    """'“a”', '“a” and “b”', '“a”, “b” and “c”'."""
    q = [f"“{n}”" for n in names]
    return q[0] if len(q) == 1 else ", ".join(q[:-1]) + " and " + q[-1]


def _queued_lanes(s: dict) -> list:
    """The lanes pending step `s` is bound to by its intent (coord.queued_lanes): one lane, or [] when it goes to
    whichever lane of its member claims it first. The one reader of a step's queue, for runs written before intents
    too (their `waits_lane`)."""
    return coord.queued_lanes(s)


def _exact_intent(run, s, aid):
    """Pending step `s` is bound to lane `aid` by its intent, and its label still names that lane's member."""
    it = coord.intent(s) if s.get("state") == "pending" else None
    return bool(it and it.get("lane") == aid and
                _label_matches(s.get("label") or "", (run.get("agents") or {}).get(aid) or {}))


def _queued_for(run: dict, aid: str) -> list:
    """The pending steps whose intent may go to lane `aid` (bound to it, or to its member), in the order it claims
    them (coord.claimable): answered blocked steps first, then those bound to it, then its member's, oldest first."""
    lane = (run.get("agents") or {}).get(aid) or {}
    marks = []
    for i, x in enumerate(run.get("plan") or []):
        it = coord.intent(x) if x.get("state") == "pending" else None
        if not it:
            continue
        other = (run.get("agents") or {}).get(it["lane"]) or {} if it["lane"] else {}
        if it["lane"] == aid or (it["lane"] and other.get("state") != "running" and _label_matches(it["member"], lane)
                                 and (lane.get("parent") or "main") == "main") \
                or (not it["lane"] and _label_matches(it["member"], lane)
                                 and (lane.get("parent") or "main") == "main" and not lane.get("follow_up")):
            marks.append((0 if it["front"] else 1 if it["lane"] else 2, it["ts"] or 0, i, x))
    return [x for *_, x in sorted(marks, key=lambda t: t[:3])]


def _closes_before(run: dict, aid: str, s: dict) -> list:
    """The names of the steps that close before queued step `s` joins lane `aid`: its open step, then the steps
    queued for it ahead of `s`."""
    cur = _open_step_of(run, aid, s)
    queue = _queued_for(run, aid)
    ahead = queue[:next((i for i, x in enumerate(queue) if x is s), len(queue))]
    return ([cur["name"]] if cur else []) + [x["name"] for x in ahead]


def lane_wait(run: dict, s: dict, live: list) -> str:
    """Why step `s`, whose member works (`live`: working_agents), waits for that member, and what to do: the one
    advice `dev progress assign` and `dev progress reopen` post and `dev crew next` gives (dispatch._sorted). Its
    intent goes to the member's lane that frees first (or the one lane it is bound to), once the steps before it
    close, and the member's own `dev progress step --step` closes it. '' when its member does not work."""
    from . import crew
    label = s.get("label") or ""
    lanes = [k for k, v in live if _label_matches(label, v)]
    if not lanes:
        return ""
    who, name = crew.who(label), s["name"]
    bound = next((k for k in lanes if k in _queued_lanes(s)), None)
    if not bound and len(lanes) > 1:
        busy = [x["name"] for x in (_open_step_of(run, k, s) for k in lanes) if x]
        agents = run.get("agents") or {}
        labels = [lab for lab in (agents[k].get("label") or "" for k in lanes)
                  if lab and sum(1 for _, v in live if _label_matches(lab, v)) == 1]
        one = ("; to give it to one of them, assign it with that lane's launch description: "
               + " · ".join(f'dev progress assign "{name}" --to "{lab}"' for lab in labels)) if labels else ""
        return (f"{who} works in {len(lanes)} lanes: “{name}” joins the first of them whose step closes"
                + (f" ({_quoted_and(busy)})" if busy else "") + one)
    aid = bound or lanes[0]
    cur = _open_step_of(run, aid, s)
    head = f"{who} works on “{cur['name']}”" if cur else f"{who} works with no step"
    before = _closes_before(run, aid, s)
    when = f"once {_quoted_and(before)} close{'s' if len(before) == 1 else ''}" if before else "now"
    return (f"{head}: “{name}” joins that lane {when}; send it to them (SendMessage / send_input), and their "
            f"dev progress step --step \"{name}\" closes it")


def _launch_named(run: dict, aid: str, lane: dict):
    """The step of its member that lane `aid`'s launch description names best, alone (as _bind_step scores it),
    among the pending steps that may go to it; None when it names none of them, or two equally."""
    task = _task_text(lane.get("label") or "")
    scored = [(_name_score(task, x["name"]), x["name"]) for x in _queued_for(run, aid)]
    if not scored:
        return None
    best = max(sc for sc, _ in scored)
    top = [n for sc, n in scored if sc == best]
    return top[0] if best[1] and len(top) == 1 else None


def _claim_lane(run: dict, aid: str, now: float, member_wide: bool = True, told=()) -> list:
    """Lane `aid`, when it is free, takes the steps the claim rule gives it (coord.claim): the one its launch names,
    else the oldest intent for it; a reviewer's lane goes on taking ready reviews, since reviews share a lane. Each
    step is recorded in the lane, and its member is told when its launch did not name it, unless the step is in
    `told` (the caller tells the lane itself, as dev crew delegate does). `member_wide` False: a resume whose member
    has a new launch on its way leaves the member's steps to that launch. -> the steps taken"""
    from . import crew
    lane = (run.get("agents") or {}).get(aid)
    taken = []
    while coord.can_claim(lane) and len(taken) < 20:
        named = _launch_named(run, aid, lane)
        s = coord.claimable(run, aid, named, member_wide, review=is_review_step)
        if s is None:
            break
        who = crew.who(lane.get("persona") or s.get("label") or "")
        why = (f"{who}'s launch names it" if s["name"] == named else f"{who} holds no other step now: it took this "
               f"step, the oldest waiting for it")
        coord.claim(run, aid, now, why, named, member_wide, review=is_review_step)
        _remember_step(lane, s)
        lane.pop("handed_over", None)
        _audit("crew.claim", s["name"], why, project=run.get("project") or "", agent_id=aid[:16])
        if s["name"] == named:
            s.pop("joined", None)
        else:
            s["joined"] = aid  # its launch does not name it: only the lane's own evidence closes it (_steps_about)
        if s["name"] != named and s["name"] not in told:
            rest = [x["name"] for x in _queued_for(run, aid)]
            scope = f" (your scope: {', '.join(s['scope'])})" if s.get("scope") else ""
            _post(run, "devkit", aid, f"devkit: “{s['name']}” is your step now{scope}: close it with dev progress "
                  f"step \"…\" --step \"{s['name']}\" --check \"…\"" + (f"; then “{rest[0]}” joins your lane" if rest
                                                                         else ""), now, from_name="devkit",
                  kind="notice")
        taken.append(s)
    return taken


def _claim_free_lanes(run: dict, now: float, told=()):
    """Every lane that can take a step claims what waits for it (_claim_lane), oldest lane first: after any change
    that frees a lane, readies a step or gives one an intent. A lane the record lists as running whose member ended
    or went stale (lane_states, read only when some lane has a step to claim) takes nothing. `told`: the steps whose
    lane the caller tells itself."""
    agents = run.get("agents") or {}
    states = None
    for aid in sorted(agents, key=lambda k: (agents[k].get("started") or 0, k)):
        if not coord.can_claim(agents[aid]) or coord.claimable(run, aid, review=is_review_step) is None:
            continue
        if states is None:
            states = lane_states(run, now, own=True)
        if (states.get(aid) or {}).get("state", "working") != "working":
            continue
        _claim_lane(run, aid, now, told=told)


def _background_running(agent: str, sid: str, aid: str) -> list:
    """The shell commands crew lane `aid` put in the background that have not ended (gates.background_work on the
    member's transcript, which Claude Code keeps beside the session's) -> [{"id", "command"}]; [] when devkit cannot
    read it (Codex, no transcript yet)."""
    if agent != "claude" or not aid:
        return []
    try:
        from . import continuity, gates
        path = continuity.member_transcript((state.read(session_path(agent, sid)) or {}).get("transcript") or "", aid)
        return gates.background_work(path) if path and os.path.isfile(path) else []
    except Exception:
        return []


def _member_close_refused(run: dict, st: dict, aid: str, check: str, running=()) -> str:
    """Why a member's `dev progress step "…" --step <name>` cannot close that step, with what to do; '' when it can,
    when the step is closed already, or when it is a review, which closes with the member's report. `running`: the
    member's background commands that have not ended (_background_running): a step whose work may still run does
    not close on a ✓. The command is refused before anything is recorded, so the member runs it again once it can
    close the step."""
    from . import approvals
    name = st["name"]
    if st.get("state") in DONE:
        return ""
    if st.get("owner") == aid:
        review = approvals.is_review(st)
        if running and not review and str(check or "").strip():
            n = len(running)
            cmds = "; ".join(f"“{b.get('command')}”" for b in running[:3]) + (f" and {n - 3} more" if n > 3 else "")
            return (f"“{name}” stays {st.get('state')}: {n} of your background commands {'have' if n > 1 else 'has'} "
                    f"not ended ({cmds}), so its work may still run and devkit recorded nothing. Run this again once "
                    f"{'they end' if n > 1 else 'it ends'} (its task notification), or stop {'them' if n > 1 else 'it'} "
                    f"(TaskStop) if {'they are' if n > 1 else 'it is'} not this step's work; dev progress note \"…\" "
                    f"--step \"{name}\" keeps partial evidence meanwhile")
        if str(check or "").strip() or review:
            return ""
        return (f"“{name}” stays {st.get('state')}: a step closes on its evidence, so run this again with --check "
                f"\"✓ <tests run, output>\" (dev progress note \"…\" --step \"{name}\" keeps partial evidence and "
                f"leaves it open); devkit recorded nothing")
    if st.get("state") == "pending" and any(st is x for x in _queued_for(run, aid)):
        before = _closes_before(run, aid, st)
        if before:
            return (f"“{name}” joins your lane once {_quoted_and(before)} close{'s' if len(before) == 1 else ''}, so it "
                    f"cannot close yet and devkit recorded nothing: close “{before[0]}” first (dev progress step "
                    f"\"…\" --step \"{before[0]}\" --check \"…\"), then run this again")
        deps = deps_open(run, st)
        return (f"“{name}” waits for {', '.join(deps)}, so it cannot close yet and devkit recorded nothing: it joins "
                f"your lane once {'that is' if len(deps) == 1 else 'they are'} done; run this again then")
    holder = "the tech lead's" if (st.get("owner") or "main") == "main" else f"{_name_of(run, st['owner'])}'s"
    return (f"“{name}” is not your step ({holder}), so devkit cannot close it and recorded nothing: ask the tech lead "
            f"to assign it to you (dev crew ask \"…\"), or record your work without --step")


def _open_step_of(run: dict, aid: str, s: dict | None = None):
    """The step lane `aid` holds (runs) that keeps it from taking `s`, or None (coord.busy): a lane works one step
    at a time, and a blocked or unverified step does not hold it. Reviews are the exception: for a review `s` the
    lane's running reviews do not count, and one report ends them all (_end_lane_steps)."""
    review = s is not None and is_review_step(s)
    return next((x for x in coord.held(run, aid) if x is not s and not (review and is_review_step(x))), None)


def _take_step(run: dict, aid: str, s: dict, now: float):
    """Lane `aid` takes step `s`: it owns it and runs it, its scope is the step's while it runs (lane_scope), and its
    member's step closes reach the plan."""
    s.update(owner=aid, state="running", started=s.get("started") or now)
    coord.drop_intent(s)
    lane = run["agents"][aid]
    _remember_step(lane, s)
    lane.pop("handed_over", None)


def _remember_step(lane: dict, step: dict):
    """Keep actual ownership across reopen/reassignment; launch wording is not ownership."""
    held = lane.setdefault("assigned_steps", [])
    if step["name"] not in held:
        held.append(step["name"])


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
    """Step scopes count from the run's repository: the edit advice matches a member's file by its path relative to
    the checkout that holds it (hooks._scope_advice), so an absolute or ~ glob inside a checkout never matched.
    -> (globs, rewrites): such a glob in its repository-relative form, with "old → new" for the
    tech lead; absolute globs outside the repository (a scratch folder, the project's plans) as given; a glob that
    names a whole checkout (`<worktree>/**`, `<worktree>/`) as that checkout's top-level entries (_checkout_entries),
    never a bare `**`. A glob is rewritten only when that is exact: its fixed folder (the part before the first
    wildcard) lies in one checkout.
    Raises StepError when it is not: the fixed folder holds checkouts (a wildcard or the folder spans several), or it
    is a managed worktree folder of the project with no checkout there (a removed worktree, a typo). A guess there
    could cover files the tech lead did not name, or keep a scope that never matches."""
    if not any(g.startswith(("/", "~")) for g in globs):
        return globs, []
    from . import lanescope
    base = lanescope.scope_base(project, cwd or os.getcwd())
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
            if new == "**":  # the whole checkout: the entries it holds, never a bare **
                entries = _checkout_entries(top)
                if not entries:
                    raise StepError(f"--scope {g} names {core.tilde(top)}, which holds no file git knows. {fix}")
                out.extend(entries)
                rewrites.append(f"{g} → {', '.join(entries)}")
                continue
            new += "/" if ga.endswith("/") and not rest else ""
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
    return list(dict.fromkeys(out)), rewrites


def _scope_checkout(globs: list, project: str, cwd: str | None) -> str:
    """The checkout a scope counts in: the one checkout of the run's repository that holds every glob, when each is
    an absolute or ~ path inside it (`<worktree>/lib/**`); '' otherwise (a repository-relative glob, globs of two
    checkouts, or none). A repository-relative scope matches in every checkout, so it overlaps another member's in
    any of them; one that names its checkout overlaps only a scope of the same checkout or of none (_same_checkout):
    the same files of two worktrees are merge work, not two members writing one file."""
    if not globs or not all(str(g).startswith(("/", "~")) for g in globs):
        return ""
    from . import lanescope
    tops = _checkouts(lanescope.scope_base(project, cwd or os.getcwd()))
    held = set()
    for g in globs:
        parts = os.path.expanduser(g).rstrip("/").split("/")
        k = next((i for i, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
        fixed = core.real("/".join(parts[:k]) or "/")
        held.add(next((t for t in tops if core.is_under(fixed, t)), ""))
    return held.pop() if len(held) == 1 and "" not in held else ""


def _same_checkout(a, b) -> bool:
    """Do two scopes count in the same checkout (_scope_checkout)? A scope that names none counts in every one."""
    return not a or not b or a == b


def _checkout_entries(top: str) -> list:
    """The repository-relative globs that hold every file of the checkout at `top`: each top-level folder as
    `<name>/**` and each top-level file by its name, from git's own list (tracked files and the untracked ones it
    does not ignore). A scope that names a whole checkout becomes these, so it reads as the files it holds."""
    heads = {}
    for p in core.gitout(top, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0"):
        head, sep, _ = p.partition("/")
        if head:
            heads[head] = heads.get(head, False) or bool(sep)
    return sorted(f"{h}/**" if folder else h for h, folder in heads.items())


def _tell_rewrites(run: dict, step: str, rewrites: list, now: float):
    """Tell the tech lead which globs of a step's scope _scope_globs kept repository-relative (assign, delegate)."""
    if rewrites:
        _post(run, "devkit", "main", f"the scope of “{step}” is kept repository-relative, the form the edit advice "
              f"matches: {'; '.join(rewrites)}. It holds those files in every checkout of "
              f"{run.get('project') or 'the repository'}.", now, from_name="devkit")


# How a lane's scope glob holds a path. The edit check (in_scope), the holders of a path (scope_holders) and the
# overlap check that keeps two working lanes apart (_globs_overlap) read globs by the same rules (coord.segments), so
# two lanes whose scopes do not overlap never write the same file:
#   - a glob is matched segment by segment: `*`, `?` and `[…]` match inside one segment and never cross `/`, and `**`
#     as a whole segment matches any number of segments, none included (`docs/**/*.md` holds docs/guide.md);
#   - a glob holds everything under a folder it matches: `docs/`, `lib/parser`, `lib/parser/**` and `src/*` hold the
#     files below those folders, `src/pkg/*.py` holds no file of src/pkg/studio/; a literal name that is a file in
#     the checkout (`docs/crew.md`) has nothing under it, so no other glob overlaps it there;
#   - a relative glob counts from its repository and holds no path outside it (a `..` segment), `**` included; an
#     absolute or ~ glob names a folder anywhere (a scratch folder, the project's plans) and holds absolute paths;
#   - on a volume that ignores case (macOS's default), `Readme.md` and `README.md` are one file to both checks.

def _glob_segments(glob: str) -> list:
    """A scope glob as its segments, ending with the "**" that holds what lies under a folder it matches."""
    return coord.segments(glob)


_FOLDS = {}


def _folds_case(base: str | None = None) -> bool:
    """Does the volume of `base` (else the home folder) ignore case? Checked once per folder: the folder's own path
    in the other case names it too."""
    p = os.path.realpath(base or os.path.expanduser("~"))
    if p not in _FOLDS:
        other = p.swapcase()
        _FOLDS[p] = other != p and os.path.exists(other) and os.path.samefile(p, other)
    return _FOLDS[p]


def scope_match(path, glob, fold: bool | None = None) -> bool:
    """Does scope glob `glob` hold `path` (the rules above)? A relative glob holds a path relative to the lane's
    repository, an absolute or ~ one an absolute path; a path with a `..` segment lies outside and is held by none.
    `fold`: compare without case (None: when the home folder's volume ignores it, as _globs_overlap does)."""
    g, p = str(glob or ""), str(path or "")
    g = os.path.expanduser(g) if g.startswith("~") else g
    if not g or not p or g.startswith("/") != p.startswith("/"):
        return False
    if _folds_case() if fold is None else fold:
        g, p = g.casefold(), p.casefold()
    segs = [s for s in p.split("/") if s not in ("", ".")]
    return bool(segs) and ".." not in segs and coord.segments_match(segs, coord.segments(g))


def _glob_real(g: str) -> str:
    """An absolute or ~ glob with its literal folders resolved (/tmp/x/** → /private/tmp/x/**)."""
    parts = os.path.expanduser(g).split("/")
    k = next((i for i, p in enumerate(parts) if re.search(r"[*?\[]", p)), len(parts))
    return "/".join([core.real("/".join(parts[:k]) or "/")] + parts[k:])


def _path_kind(top: str):
    """What a glob's literal path is in checkout `top` -> "dir", "file" or None (nothing there): the answer
    coord.names_folder asks for. None without a checkout."""
    if not top or not os.path.isdir(top):
        return None

    def kind(glob):
        p = glob if glob.startswith("/") else os.path.join(top, glob)
        return "dir" if os.path.isdir(p) else "file" if os.path.exists(p) else None
    return kind


def _globs_overlap(a: str, b: str, top: str = "") -> bool:
    """Could two scope globs hold the same file? True when at least one path matches both by scope_match's rules,
    compared without case, since macOS's disk ignores it. `src/pkg/*.py` and `src/pkg/studio/host.py` do not
    overlap, nor do `tests/unit/test_parser*.py` and `tests/unit/test_cli*.py`, so two lanes can split one folder;
    `tests/unit/**` overlaps every file under tests/unit/. A literal name is a folder or a file as checkout `top`
    has it (a suffix makes it a file when nothing is there yet), so `docs/crew.md` and `docs/**/bug*.md` do not
    overlap. A relative glob never overlaps an absolute one: the absolute globs a step keeps lie outside its
    repository (_scope_globs)."""
    a, b = str(a or ""), str(b or "")
    if not a or not b or a.startswith(("/", "~")) != b.startswith(("/", "~")):
        return False
    if a.startswith(("/", "~")):
        a, b = _glob_real(a), _glob_real(b)
    return coord.globs_overlap(a, b, kind=_path_kind(top or "/" if a.startswith("/") else top))


def _in_globs(rel_path: str, globs) -> bool:
    """A repo-relative path is held by one of a scope's globs ("lib/parser/**", "docs/", "README.md"): scope_match."""
    return any(scope_match(rel_path, g) for g in globs or [] if isinstance(g, str))


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
    other sessions' live runs first (lanescope.scope_owner_across, given one lane at a time: the lane's scope and its
    open steps' scopes, in the checkout that holds the path), then this session's own run (the lane's scope, counted
    from the run's repository; `me`, the caller's own lane, excluded). Each holder: who, member, lane, agent,
    session, run, step, scope, own, to, do, and its lane_state (state, last, why). Only a working holder holds the
    file: the edit advice names the first one (scope_holder) and `dev crew owner` exits 1 for it."""
    from . import lanescope
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
            tops[key] = lanescope.run_checkout(run)
        return tops[key]
    for run in runs:
        if run.get("state") == "done" or str(run.get("session") or "") == str(sid):
            continue
        for aid, lane in (run.get("agents") or {}).items():
            if not isinstance(lane, dict) or lane.get("state") != "running":
                continue
            # a scope given in one checkout (_scope_checkout) counts there, else in the checkout its session works in
            if lanescope.scope_owner_across(ap, runs=[dict(run, agents={aid: lane})], me_session=sid,
                                            checkout_of=lambda r, lane=lane: lane.get("checkout") or top(r)):
                out.append(_holder(run, aid, lane, coord.scope(run, aid), facts, now, alive, own=False))
    if isinstance(mine, dict) and mine.get("state") != "done":
        run = normalize(mine)
        expire_stale(run, now)
        rel = lanescope.scope_rel(path, cwd, lanescope.scope_base(run.get("project"), cwd))
        for aid, lane in (run.get("agents") or {}).items():
            if aid == me or not isinstance(lane, dict) or lane.get("state") != "running":
                continue
            # a scope given in one checkout counts there: the same relative files of another checkout are not its
            home = _lane_checkout(run, aid)
            here = lanescope.scope_rel(path, cwd, home) if home else rel
            if _in_globs(here, coord.scope(run, aid)):
                out.append(_holder(run, aid, lane, coord.scope(run, aid), facts, now, alive, own=True))
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
    """The edit advice for a file a working member holds: who, their scope, where (another session's run), their last
    activity, how to ask them, and the command that checks again."""
    shown = shown_path(path, cwd)
    where = "" if h.get("own") else (f", in another session of this repository (run «{str(h.get('run'))[:60]}», "
                                     f"session {h['session'][:12]})")
    return (f"{shown} belongs to {h['who']} while they work (scope {', '.join(h['scope'])}){where}; last activity "
            f"{_clock(h['last'], now or time.time())}. Ask them (dev crew ask --to {h['to']} \"…\") or wait until "
            f"they finish, and check again with dev crew owner {shlex.quote(shown)}.")


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


def _addr(run, ref: str, now: float | None = None, pending_launch=False):
    """'Morpheus', 'the tech lead', 'Tech lead (Morpheus)', 'lead', 'main', '' -> 'main'; a lane id -> that lane; a
    crew name or label -> its sole working lane (_member_lanes, as `dev crew owner` reads it). A member with no
    working lane requires an explicit pending_launch request; exact lane IDs remain bound to that recipient."""
    r = " ".join((ref or "").split())
    if not r or _TECH_LEAD.fullmatch(r):
        return "main"
    agents = run.get("agents") or {}
    if r in agents:
        return r
    working, _ = _member_lanes(run, r, now)
    if len(working) > 1:
        from .messaging import MessageError
        raise MessageError("ambiguous_target", "name an exact lane: " + ", ".join(k for k, _ in working))
    if working:
        return working[0][0]
    if pending_launch:
        return "name:" + r
    from .messaging import MessageError
    raise MessageError("not_found", "no eligible lane matches; use its exact lane ID or request --pending-launch")


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
    """Keep replies bound to the asking lane, including after it stops.

    A replacement with the same role is a distinct recipient. Forwarding requires
    a fresh message explicitly addressed by the tech lead.
    """
    return aid


def _ended_asker_notice(run, recipient: str, what: str, now: float):
    """A reply (an answer, a decision's result, a withdrawal) stays addressed to the lane that asked (_reply_to). When
    that lane has ended, nobody reads it there: a devkit notice tells the tech lead, who forwards it to a lane that
    works (dev crew ask --to) or launches the member again. The caller saves the run."""
    lane = (run.get("agents") or {}).get(recipient)
    if recipient in ("", "main") or not isinstance(lane, dict) or lane.get("state") not in ENDED_LANE + ("unverified",):
        return
    who = _name_of(run, recipient)
    _post(run, "devkit", "main", f"{who} ended before {what} reached them: it stays addressed to their lane, which "
          f"nobody reads now. Forward it (dev crew ask --to <member> \"…\") or launch them again.", now,
          from_name="devkit", kind="notice")


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
             kind: str = "message", request_id: str = "", pending_launch: bool = False) -> dict:
    """A question or request between crew members (default: to Morpheus, the main session). It reaches the recipient
    with its next tool call (PostToolUse) or prompt. `kind`: "message" (a question, or a notice when CI watch or devkit
    sends it), "notice" (it tells, and waits for no answer) or "answer". The audit trail keeps its kind and length,
    never its text (crew messages quote the developer's requests). A message longer than MESSAGE_MAX is refused whole,
    never cut: the reader would get half a text without knowing it."""
    now = now or time.time()
    from . import messaging
    messaging.validate_body(text)
    request_id = _message_request_key(request_id, cwd, ("crew ask", kind, to or "", pending_launch, text))
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    return _ask_into(agent, sid, text, to, from_agent or "main", now, kind, request_id=request_id,
                     pending_launch=pending_launch)


def _ask_into(agent: str, sid: str, text: str, to, frm: str, now: float, kind: str = "message", request_id="",
              pending_launch=False) -> dict:
    """crew_ask into the run of session `agent`:`sid`, from `frm` (a lane of that run, or "main")."""
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            raise ValueError("no progress run in this session: the tech lead starts one with dev progress start")
        run = normalize(run)
        before = len(run.get("messages") or [])
        m = _post(run, frm, _addr(run, to or "", now, pending_launch=pending_launch), text, now,
                  kind=kind if kind in ("notice", "answer") else "", request_id=request_id)
        if len(run.get("messages") or []) == before:  # its request key sent it before: that message, once
            return m
        if m["to"].startswith("name:"):
            m["pending_launch"] = str(to)
        told = _waits_notice(run, m, frm, now) if m["to"].startswith("name:") and frm not in NOTICE_SENDERS else ""
        if told:  # never silently: the sender learns with its next tool call that nobody reads it now
            _post(run, "devkit", frm, told, now, from_name="devkit", kind="notice")
        _save(run, touch_session=False)
    _audit("crew.message", f"{m['from_name']} → {m['to_name']}", "", project=run.get("project") or "", id=m["id"],
           type=kind, count=len(m["text"]))
    _notify_message(agent, sid, m)
    return m


def _message_request_key(value, cwd=None, args=()):
    """The idempotency key of a send: the caller's --request-id, else the one open tool call that typed this command
    with a digest of what it sends (`args`: the command, its target and its body), so two sends in one shell call are
    two messages while the same send repeated in that call is one; with no single open call, a fresh key."""
    from . import identity, messaging
    if value:
        if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise messaging.MessageError("invalid_text", "invalid request key")
        return value
    found = identity._find(cwd, None, None, settle=False)
    if len(found.calls) != 1:
        return str(uuid.uuid4())
    digest = hashlib.sha256(messaging.encoded([str(a) for a in args])).hexdigest()[:32]
    return f"native:{found.calls[0]}:{digest}"


def _notify_message(agent, sid, message):
    """A desktop alert for a crew message to the tech lead while no wait is armed for the main session (an armed wait
    wakes it). A message between agents never alerts the developer; a decision for them alerts on its own
    (_notify_idle)."""
    if message.get("to") != "main" or not cfg().get("notify") or os.environ.get("DEVKIT_NO_NOTIFY"):
        return
    try:
        from . import notifications, watch
        if watch.armed(agent, sid, "main"):
            return
        notifications.notify_event({"event_id": message["ref"] + ":" + str(message.get("generation", 1)),
                                    "recipient": f"{agent}:{sid}:{message['to']}",
                                    "reference": message["ref"], "category": "crew"})
    except Exception:
        pass


def _next_id(items: list, prefix: str) -> str:
    """The id after the highest one kept: the lists keep their last 200 entries, so a count would repeat ids."""
    nums = [int(x["id"][len(prefix):]) for x in items if str(x.get("id") or "")[len(prefix):].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


NOTICE_SENDERS = ("ci", "devkit")  # CI watch and devkit itself tell; they never ask
MESSAGE_MAX = 65536  # UTF-8 bytes; accepted message bodies are never shortened


def _post(run, frm: str, to: str, text: str, now: float, from_name: str = "", kind: str = "", request_id="",
          reply_to="") -> dict:
    """Add a message to the run (the caller saves it). `kind`: question, notice or answer; without it, a notice from
    CI watch or devkit, else a question. devkit's own notices and CI results quote what they saw (a log line, an
    error), so their text is stored with secrets redacted; an agent's text with a secret is refused. -> the message."""
    from . import messaging
    devkits = frm in NOTICE_SENDERS or from_name == "devkit"
    body = messaging.validate_body(text, redact=devkits)
    msgs = run.get("messages", [])
    rid = messaging.run_id(run)
    mid = _next_id(msgs, "M")
    m = {"id": mid, "ref": mid + "@r-" + rid, "schema": 2, "run_id": rid,
         "seq": len(msgs) + 1, "generation": 1, "receipts": {},
         "ts": round(now), "created_at": now, "from": frm, "to": to, **body,
         "delivered": False, "kind": kind or ("notice" if frm in NOTICE_SENDERS else "question")}
    if reply_to:
        m["reply_to"] = reply_to
    m["from_name"], m["to_name"] = from_name or _name_of(run, frm), _name_of(run, to)
    if run.get("agent") == "codex" and to == "main" and frm not in ("", "main") and m["kind"] == "question":
        # Steps are identified by their unique names within this run, as dependencies and transitions are.
        m["steps"] = [s["name"] for s in run.get("plan") or [] if isinstance(s, dict) and s.get("name")
                      and s.get("owner") == frm and s.get("state") not in DONE]
    return messaging.append_message(run, m, request_id=request_id, redact=devkits)


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
    lost, or closed unverified) and owns no running or blocked step. Codex also keeps questions for pending or
    unverified work actionable, including its recorded originating steps after reassignment. A moot question
    never waits or wakes and is shown as moot. A question of the tech lead's, or of an unknown lane, never is."""
    if not isinstance(m, dict) or m.get("to") != "main" or not _asks(m) or m.get("answered") or m.get("closed"):
        return False
    lane = ((run or {}).get("agents") or {}).get(m.get("from") or "")
    if m.get("from") in (None, "", "main") or not isinstance(lane, dict) or \
            lane.get("state") not in ENDED_LANE + ("unverified",):
        return False
    codex = (run or {}).get("agent") == "codex"
    steps = m.get("steps") if codex and isinstance(m.get("steps"), list) else []
    return not any(isinstance(s, dict) and (s.get("owner") == m.get("from") or s.get("name") in steps)
                   and (s.get("state") not in DONE if codex else s.get("state") in ("running", "blocked"))
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


UNREAD_CLOSED = "its run ended before it was read"


def _close_messages(run, reason: str, now: float) -> list:
    """A run ends or is replaced: each question in it that still waits for an answer, to the tech lead or to a
    member, closes with the reason, and takes no answer any more. Every other message nobody has acknowledged (a
    report, a notice, a CI result) closes too, with UNREAD_CLOSED: its run's work is over, so it is no longer
    delivered again every minute (_due), and `dev crew read` still shows it. -> copies of the closed messages, for
    the audit after the save."""
    from . import messaging
    closed = []
    for m in _open_questions(run):
        m.update(closed=round(now), reason=reason[:300])
        closed.append(dict(m))
    for m in run.get("messages") or []:
        if isinstance(m, dict) and not m.get("closed") and not m.get("receipts") and not m.get("answered") \
                and not messaging.delivered_before_upgrade(m):  # one of those never comes back anyway
            m.update(closed=round(now), reason=UNREAD_CLOSED)
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
    if p.startswith("r-"):
        from . import messaging
        rid = messaging.run_id({"run_id": p[2:]})
        run = state.load_bounded(_message_path(rid), messaging.STORE_MAX)
        if not run:
            raise messaging.MessageError("not_found", "canonical run is unavailable")
        current = load_run(run["agent"], run["session"])
        if not current or messaging.run_id(current) != rid:
            raise messaging.MessageError("stale_target", "run was replaced; its messages remain readable")
        return run["agent"], run["session"]
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


def _answered(q: dict, a: dict, who: str) -> None:
    """Question `q` is answered by message `a`, which `who` (main or a lane id) sent: answering reads the question,
    so it carries `who`'s receipt, leaves their inbox and is never delivered to them again as unread."""
    from . import messaging
    q.update(answered=True, answered_by=a["ref"])
    q.setdefault("receipts", {}).setdefault(who, messaging.delivery_receipt(q))


def crew_answer(msg_id: str, text: str, from_agent: str | None = None, cwd: str | None = None, now: float | None = None,
                request_id: str = "") -> dict:
    """Answer crew message `msg_id` (`dev crew answer`): mark it answered and post the answer, in the run that holds
    it, to the lane that asked while its member works (_reply_to of that run). A qualified id (M3@9448c4d0) names
    that run by its session; a bare one is the caller's own run's. The tech lead's or the developer's bare id whose
    message in the caller's run is missing, or is no question waiting for an answer, while another active run of the
    project holds one that waits by that id, is refused with the qualified ids to use: it never answers a message
    other than the one shown. A crew member answers the messages of its own run only."""
    from . import messaging
    body = messaging.validate_body(text)
    request_id = _message_request_key(request_id, cwd, ("crew answer", msg_id, text))
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
        if from_agent and q.get("to") != from_agent:
            raise messaging.MessageError("not_recipient", "only the exact recipient may answer this question")
        if not _asks(q):
            raise messaging.MessageError("not_found", "message is not a question")
        previous = next((m for m in run.get("messages", []) if request_id and m.get("request_id") == request_id
                         and m.get("from") == (from_agent or "main")), None)
        if previous:
            if previous.get("body_sha256") != body["body_sha256"] or previous.get("reply_to") != q["id"]:
                raise messaging.MessageError("idempotency_conflict", "request key already used")
            _save(run, touch_session=False)  # a retry also completes a previously uncertain directory fsync
            return previous
        if q.get("answered"):
            raise messaging.MessageError("already_answered", q.get("answered_by") or "question has an answer")
        if _closed_why(run, q):
            raise ValueError(f"{msg_id} was closed ({_closed_why(run, q)}): it takes no answer. Ask again with dev "
                             f"crew ask if it still matters")
        a = _post(run, from_agent or "main", q["from"], text, now or time.time(),
                  kind="answer", request_id=request_id, reply_to=q["id"])
        a["to_name"] = q["from_name"]
        _answered(q, a, from_agent or "main")
        _ended_asker_notice(run, a["to"], f"the answer {a['id']} to their question {q['id']}", now or time.time())
        _save(run, touch_session=False)
    _audit("crew.message", f"{a['from_name']} → {a['to_name']}", "", project=run.get("project") or "",
           id=a["id"], type="answer", count=len(a["text"]))
    _notify_message(*target, a)
    return a


def _command_session(cwd=None):
    """The session of the main command that runs this, or None when the caller is a crew member, another session,
    or a caller devkit cannot place exactly (a recency guess never counts)."""
    who = caller(cwd)
    if who.get("caller") == "unverified":
        from . import identity
        found = identity._find(cwd, exact=True, settle=False)
        if (found.ledger == "main" and found.calls and not found.members
                and found.speaker.lane == "main" and found.session_how != "recency"):
            who = dict(who, caller="main", agent=found.agent, session=found.speaker.session)
    if who.get("caller") not in ("main", "human"):
        return None
    agent, sid = current_session(cwd)
    if who.get("session") and (who.get("agent"), who["session"]) != (agent, sid):
        return None
    return agent, sid


def _readable_message(ref, from_agent=None, cwd=None):
    from . import messaging
    if from_agent is None and _command_session(cwd) is None:
        why = caller(cwd).get("why")  # identity's reason, such as the plain-call fix
        raise messaging.MessageError("not_recipient", "the main session's own command reads its messages"
                                     + (f": {why}" if why else ""))
    lane = _as_lane(from_agent, cwd) or "main"
    agent, sid = current_session(cwd)
    bare, suffix = split_id(ref)
    if suffix.startswith("r-"):
        rid = messaging.run_id({"run_id": suffix[2:]})
        run = state.load_bounded(_message_path(rid), messaging.STORE_MAX)
        if run:
            run = messaging.migrate(run)
    else:
        target = run_of_prefix(suffix) if suffix else (agent, sid)
        run = load_run(*target)
    if not run or (run.get("agent"), run.get("session")) != (agent, sid):
        raise messaging.MessageError("not_recipient", "reference is outside this authenticated run")
    item = next((m for m in run.get("messages", []) + run.get("decisions", []) if m.get("id") == bare), None)
    if not item:
        raise messaging.MessageError("not_found", "message is unavailable")
    if lane != "main" and lane not in (item.get("from"), item.get("to")):
        raise messaging.MessageError("not_recipient", "reference belongs to another lane")
    return run, item, lane


def crew_read(ref, *, cursor="", from_agent=None, cwd=None):
    from . import messaging
    _, item, _ = _readable_message(ref, from_agent, cwd)
    return messaging.read_page(item, cursor=cursor)


def crew_ack(ref, *, digest, generation, from_agent=None, cwd=None):
    from . import messaging
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run, item, lane = _readable_message(ref, from_agent, cwd)
        result = messaging.acknowledge(run, item.get("ref", item["id"]), recipient=lane,
                                       digest=digest, generation=generation)
        # An archived run is updated only in the canonical namespace.
        state.replace(_message_path(messaging.run_id(run)), run, durable=True, max_bytes=messaging.STORE_MAX)
    return result


def crew_inbox(from_agent: str | None = None, cwd: str | None = None, mark: bool = True, all_: bool = False) -> list:
    """Read the caller's unacknowledged messages, or history with all_. The legacy mark argument has no effect."""
    from_agent = _as_lane(from_agent, cwd)
    agent, sid = current_session(cwd)
    run = load_run(agent, sid)
    if not run:
        return []
    me = from_agent or "main"
    run = normalize(run)
    lane = (run.get("agents") or {}).get(me) or {}
    return [dict(m, new=not bool(m.get("receipts"))) for m in run.get("messages") or []
            if _addressed_to(m, me, lane) and (all_ or not ((m.get("receipts") or {}).get(me) or m.get("closed")))]


def _addressed_to(m: dict, me: str, lane: dict) -> bool:
    """Is message `m` for `me` (the main session or a lane id, whose lane record is `lane`)? Its address, or a
    member's name its lane answers to."""
    to = m.get("to") or ""
    return to == me


def answered_by_sendmessage(agent: str, data: dict, now: float | None = None) -> list:
    """PostToolUse of a SendMessage from the main session: its text answers the open questions it names (M12) that the
    member it went to (a lane id, or a name its lanes answer to) asked the tech lead. A text that names none answers
    none (a new work item is no answer), and a devkit notice tells the tech lead which still wait and how to close
    them. A name that fits several lanes with open questions answers none: SendMessage reached only one of them. Each
    is marked answered via SendMessage, the text is kept as the answer (delivered already: SendMessage carried it), and
    the audit trail records crew.message-answered with the channel. -> the ids answered"""
    inp = data.get("tool_input") or {}
    to, text = str(inp.get("to") or "").strip(), str(inp.get("message") or "")
    resp = data.get("tool_response")
    sid = data.get("session_id") or ""
    if data.get("agent_id") or not sid or not to or to == "main" or \
            not isinstance(resp, dict) or resp.get("success") is not True:
        return []
    from . import messaging
    capture_error = ""
    try:
        messaging.validate_body(text)
    except messaging.MessageError as error:
        capture_error = error.code
    now = now or time.time()
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not isinstance(run, dict) or run.get("state") not in ("running", "blocked"):
            return []
        run = normalize(run)
        agents = run.get("agents") or {}
        lanes = [to] if to in agents else [k for k, v in agents.items() if isinstance(v, dict) and
                                           _label_matches(to, v)]
        native_target = resp.get("recipient") or resp.get("agent_id") or resp.get("to")
        if len(lanes) != 1 or (native_target and native_target not in (to, lanes[0])):
            return []
        if capture_error:
            call = str(data.get("tool_use_id") or "unavailable")
            call = re.sub(r"[^A-Za-z0-9_.:-]", "", call)[:160]
            _post(run, "devkit", "main", "capture_incomplete: native answer was not accepted (" + capture_error
                  + "); inspect native call " + call + " and answer the exact question with dev crew answer.",
                  now, kind="notice", request_id="capture:" + call)
            _save(run, touch_session=False)
            return []
        asked = [m for m in _unanswered(run) if m.get("to") == "main" and m.get("from") in lanes]
        if len({m.get("from") for m in asked}) > 1:  # the name fits several lanes that asked: SendMessage reached one
            return []
        named = set(re.findall(r"\bM\d+(?:@[0-9A-Za-z][0-9A-Za-z._-]*)?(?![\w@])", text))
        if asked and not named:  # it may be a new work item: the questions stay open, and the tech lead hears so
            from . import crew
            ids = [m["id"] for m in asked]
            one = len(ids) == 1
            _post(run, "devkit", "main",
                  f"devkit: your SendMessage to {crew.who(asked[0].get('from_name')) or to} named no question, so "
                  f"{', '.join(ids)} still wait{'s' if one else ''} for an answer. If it answered "
                  f"{'it' if one else 'them'}, close {'it' if one else 'each'}: "
                  + " · ".join(f'dev crew answer {i} "…"' for i in ids)
                  + f"; with SendMessage, name the question in the text (\"{ids[0]}: …\").",
                  now, from_name="devkit", kind="notice")
            _save(run, touch_session=False)
            return []
        def names_question(ref, question):
            bare, suffix = split_id(ref)
            if bare != question.get("id"):
                return False
            if not suffix:
                return True
            if suffix.startswith("r-"):
                return ref == question.get("ref")
            try:
                return run_of_prefix(suffix) == (agent, sid)
            except ValueError:
                return False
        asked = [m for m in asked if any(names_question(ref, m) for ref in named)]
        if not asked:
            return []
        a = _post(run, "main", asked[-1]["from"], text, now, kind="answer",
                  request_id="native:" + str(data["tool_use_id"]) if data.get("tool_use_id") else "")
        a.update(reply_to=asked[-1]["id"], delivered=True, via="SendMessage", to_name=asked[-1].get("from_name") or
                 a.get("to_name"))
        a["reply_refs"] = [q.get("ref", q["id"]) for q in asked]
        for q in asked:
            _answered(q, a, "main")
            q["via"] = "SendMessage"
        _save(run, touch_session=False)
    for q in asked:  # never the text: crew messages quote the developer's requests
        _audit("crew.message-answered", q["id"], f"answered via SendMessage to {q.get('from_name') or q['from']}",
               "answered", project=run.get("project") or "", id=a["id"], channel="SendMessage", count=len(text))
    return [q["id"] for q in asked]


def _due(m: dict, me: str, lane: dict) -> bool:
    """Is message `m` still to deliver to `me`: addressed to it, no receipt of `me`, not delivered for good by an
    earlier devkit, and never delivered or delivered over a minute ago without being read."""
    from . import messaging
    return (not (m.get("receipts") or {}).get(me) and not messaging.delivered_before_upgrade(m)
            and not m.get("closed") and (not m.get("delivered") or time.time() - m.get("notification_at", 0) >= 60)
            and _addressed_to(m, me, lane))


def _take_messages(agent, sid, me: str, mark=True, select=None, limit=None) -> list:
    """The messages for `me` not delivered yet; with mark, they are marked delivered (once each, under the lock).
    `select(run, message)`: only the messages it keeps; `limit`: at most that many, oldest first (watch.mail). Every
    hook call asks: the lock is taken only when there is something to take. A message delivered but not read comes
    back after a minute (one its preview showed whole is read on delivery: acknowledge_on_delivery); one an earlier
    devkit delivered before messages had receipts never comes back."""
    def mine(run):
        run = normalize(run)
        lane = (run.get("agents") or {}).get(me) or {}
        new = []
        for m in run.get("messages") or []:
            if limit is not None and len(new) >= limit:
                break
            if _due(m, me, lane) and (select is None or select(run, m)):
                new.append(m)
        return run, new
    run = load_run(agent, sid)
    if not run:
        return []
    run, new = mine(run)
    if not new:
        return []
    if not mark:
        return [dict(m) for m in new]
    with _tx(agent, sid):
        run, new = mine(load_run(agent, sid) or {})
        out = [dict(m) for m in new]
        for m in new:
            m["delivered"] = True
            m["notification_at"] = time.time()
        if out:
            _save(run, touch_session=False)
    return out


def claim_message(agent, sid, me: str, mid: str, select=None):
    """Claim message `mid` for `me` before its line is written (the wait): under the run's lock, and only while it is
    still due (the test _take_messages applies, then `select(run, message)`), it is marked delivered. -> (the message
    as it was before the claim, the claim's time), or None when it is no longer due: a hook or another wait took it
    since it was read. unclaim_message gives a claim back."""
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        lane = (run.get("agents") or {}).get(me) or {}
        for m in run.get("messages") or []:
            if m.get("id") != mid:
                continue
            if not _due(m, me, lane) or (select is not None and not select(run, m)):
                return None
            before, now = dict(m), time.time()
            m["delivered"], m["notification_at"] = True, now
            _save(run, touch_session=False)
            return before, now
    return None


def unclaim_message(agent, sid, before: dict, at: float):
    """Give back a claim (claim_message) whose line could not be written: the message is due again as it was before
    the claim. One claimed again since keeps its new claim."""
    with _tx(agent, sid):
        run = load_run(agent, sid)
        for m in (run or {}).get("messages") or []:
            if m.get("id") != before.get("id"):
                continue
            if m.get("notification_at") == at:
                for field in ("delivered", "notification_at"):
                    if field in before:
                        m[field] = before[field]
                    else:
                        m.pop(field, None)
                _save(run, touch_session=False)
            return


def read_on_delivery(m: dict) -> bool:
    """Does delivering the message's preview count as reading it? For devkit's own notices and CI results (their
    line is the digest an agent acts on), and for a body the preview shows whole. A longer body is read with
    dev crew read and acknowledged."""
    from . import messaging
    return m.get("from") in NOTICE_SENDERS or m.get("from_name") == "devkit" or \
        messaging.whole_in_preview(m.get("text"))


def acknowledge_on_delivery(agent, sid, me: str, shown: list):
    """Record `me`'s receipt for each message of `shown` (delivered in this hook text or wait line) that its delivery
    read whole (read_on_delivery), so it never comes back."""
    ids = {m.get("id") for m in shown if read_on_delivery(m)}
    if not ids:
        return
    from . import messaging
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if not run:
            return
        changed = False
        for m in run.get("messages") or []:
            if m.get("id") in ids and not (m.get("receipts") or {}).get(me):
                m.setdefault("receipts", {})[me] = messaging.delivery_receipt(m)
                changed = True
        if changed:
            _save(run, touch_session=False)


def mode_of(run) -> str:
    return mode_context(run)[0]


INVALID_CONFIG = "invalid configuration"  # the mode's source when the saved configuration cannot be read


def mode_shown(run) -> tuple:
    """(mode, source) for a view (the status, the graph, the dashboard, a notice): mode_context, or ("invalid",
    "invalid configuration") when the configuration or the run's mode cannot be read, so a view says so instead of
    failing. Never raises. Decisions follow mode_context, which reads an invalid configuration as guided."""
    try:
        mode, source = mode_context(run)
    except StepError:
        return "invalid", INVALID_CONFIG
    return ("invalid", source) if source == INVALID_CONFIG else (mode, source)


def _chosen(run) -> bool:
    """Whether the run's mode is an explicit choice (mode_source run: `dev crew mode` or `dev progress start --mode`).
    A run recorded without an origin (26.11.18 and earlier) kept the configuration of its day, not a choice; unless it
    holds `approved`, which only `dev crew mode` wrote then: that mode was the developer's explicit choice."""
    source = (run or {}).get("mode_source")
    return source == "run" or (source is None and "approved" in (run or {}))


def mode_context(run, snapshot=None) -> tuple:
    """The mode decisions follow: the configuration (auto by default), unless the live run records an explicit choice
    (_chosen). A run with the legacy origin follows the configuration. A configuration devkit cannot read never
    decides for the developer and never stops the work: ("guided", "invalid configuration"), so decisions wait for
    the developer until it is repaired. Raises StepError for a run whose own mode or origin cannot be read."""
    cfg, errors = core.config_snapshot() if snapshot is None else snapshot
    if errors:
        return "guided", INVALID_CONFIG
    run = run if run and run.get("state") != "done" else {}
    source = run.get("mode_source")
    if source not in (None, "config", "run", "legacy"):
        raise StepError("invalid mode origin; use dev crew mode inherit")
    if _chosen(run):
        mode, source = run.get("mode"), "run"
    else:
        mode, source = (cfg.get("work") or {}).get("mode", "auto"), "config"
    if mode not in ("auto", "guided"):
        raise StepError("invalid mode; use dev crew mode auto, guided or inherit")
    return mode, source


def set_mode(mode: str, cwd: str | None = None, now: float | None = None, taken: list | None = None) -> dict:
    """Set the run's mode (guided: decisions wait for the developer; auto: the crew goes on with the recommended
    option), or `inherit` the saved configuration's. When the run then follows auto, each decision that waits and
    that auto mode would have taken when it was asked is taken now (_take_recommended); `taken`: a list that receives
    copies of them. The audit record and the run's history keep the mode asked (inherit too) and the one it resolves
    to."""
    now = now or time.time()
    if mode not in ("guided", "auto", "inherit"):
        raise StepError("mode is guided, auto or inherit")
    _main_only(None, "sets the mode", cwd)
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        if not run.get("goal"):
            raise StepError("no progress run in this session: dev progress start \"<goal>\" --plan \"…\" --mode " + mode)
        run["mode_source"] = "config" if mode == "inherit" else "run"
        run["mode"] = mode_of(None) if mode == "inherit" else mode
        resolved = mode_of(run)
        took = _take_recommended(run, now) if resolved == "auto" else []
        run.setdefault("history", []).append({"ts": round(now), "step": "(mode)", "from": None, "to": mode,
                                              "resolved": resolved, "by": "main", "why": "mode set"})
        _save(run)
    _audit("crew.mode", mode, "mode override updated", project=run.get("project") or "",
           mode_source=run["mode_source"], mode=resolved)
    for d in took:
        _audit("crew.decided", d["id"], d["answer"][:300], project=run.get("project") or "", by="auto", mode="auto",
               why=f"dev crew mode {mode} took the recommended option", session=f"{agent}:{sid}",
               options=d.get("options") or [])
    if taken is not None:
        taken.extend(dict(d) for d in took)
    return run


def take_in_saved_auto(agent: str, sid: str, now: float | None = None) -> list:
    """The prompt hook's part of the modes: a run that follows the saved configuration, now auto, takes each decision
    still waiting from guided time that auto would have taken when it was asked (_take_recommended), as
    `dev crew mode auto` takes them. Without it such a decision waited for nobody: the session start's note, which
    reads auto, no longer asked it. Nothing while the configuration cannot be read (mode_context reads it as guided).
    -> the decisions taken"""
    now = now or time.time()
    run = load_run(agent, sid)
    if not run or run.get("state") not in ("running", "blocked") or not any(
            d.get("recommend") and not d.get("approves") for d in pending_decisions(run)):
        return []
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        try:
            auto = run.get("state") in ("running", "blocked") and mode_of(run) == "auto"
        except StepError:
            auto = False
        took = _take_recommended(run, now) if auto else []
        if took:
            _save(run, touch_session=False)
    for d in took:
        _audit("crew.decided", d["id"], d["answer"][:300], project=run.get("project") or "", by="auto", mode="auto",
               why="the saved mode is auto: took the recommended option", session=f"{agent}:{sid}",
               options=d.get("options") or [])
    return took


def _take_recommended(run, now: float) -> list:
    """Auto mode takes each decision that waits for the developer and that it would have taken when it was asked
    (crew_decision): one with a recommendation that approves no step. Each is decided by auto with its
    recommendation, as an automatic decision the developer can still override (crew_decide); what waits on it runs
    again (_unblock), and a notice with the option goes to the member that asked. A decision that approves steps, or
    has no recommendation, keeps waiting. The caller saves the run. -> the decisions taken"""
    from . import messaging
    took = []
    for d in pending_decisions(run):
        if not d.get("recommend") or d.get("approves"):
            continue
        d.update(state="decided", answer=d["recommend"], by="auto", decided=round(now))
        d.pop("note", None)
        option = _recommended(d) or d["answer"]
        _decision_history(run, d, "decided", f"auto mode took the recommended option {option}", now)
        _unblock(run, d, now)
        if d.get("from") not in (None, "", "main"):
            notice = _post(run, "main", d["from"], f"{d['id']} was taken in auto mode with the recommended option: "
                           f"{option}. Go on with it.", now, kind="notice",
                           reply_to=d.get("ref") or f"{d['id']}@r-{messaging.run_id(run)}")
            d["answer_ref"] = notice["ref"]
            _ended_asker_notice(run, d["from"], f"the result of their decision {d['id']} ({notice['id']})", now)
        took.append(d)
    return took


DECISION_QUESTION_MAX = 300  # characters: one plain question the developer reads at a glance
_LETTERED = re.compile(r"([A-Za-z])\s*:\s*(\S.*)")


def _cli_quote(text) -> str:
    return '"' + re.sub(r'(["\\$`])', r"\\\1", str(text)) + '"'


def decision_shape(question: str, options="", recommend: str = "", approves: str = "") -> tuple:
    """`dev crew ask --decision` in the shape the developer can be asked nicely: a question under 300 characters,
    2 to 4 options, each "<letter>: <what it means>", and a recommendation that is one of them. The labels devkit
    offers for the options (ask_labels) fit together in an audit record (under audit.VALUE_CUT characters), so
    `dev audit verify` can rebuild the decision from it. -> (options, the recommended option's letter). Raises
    StepError with the corrected command when the shape is wrong."""
    q = " ".join(str(question or "").split())
    opts = [o.strip() for o in (options if isinstance(options, list) else str(options or "").split("|")) if o.strip()]
    rec = (recommend or "").strip()
    parsed = [_LETTERED.fullmatch(o) for o in opts]
    letters = [mt.group(1).upper() for mt in parsed if mt]
    lettered = all(parsed) and len(set(letters)) == len(letters)
    fixed = opts if lettered else [
        f"{chr(65 + i)}: " + (mt.group(2).strip() if mt else f"<what {o} means>" if len(o) == 1 else o)
        for i, (mt, o) in enumerate(zip(parsed, opts))]
    hit = option_of({"options": fixed}, rec)
    letter = _letter(hit) if hit else ""
    problems = []
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
        size = len(" | ".join(ask_labels({"id": "D0", "options": opts, "recommend": letter})))
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
    cmd = (f"dev crew ask --decision "
           f"{_cli_quote(q if len(q) <= DECISION_QUESTION_MAX else '<the question>')} "
           f"--options {_cli_quote(' | '.join(shown))} --recommend {letter or 'A'}"
           + (f" --approves {_cli_quote(approves)}" if approves else ""))
    raise StepError("; ".join(problems) + f". Ask it like this (the developer sees the recommended option first): "
                    f"{cmd}")


ASK_PER_CALL = 4  # the question tool asks at most four questions per call


def _ask_context(run: dict, d: dict) -> str:
    """One line for the developer: who asks, what it blocks, what their answer approves."""
    from . import crew
    blocks = d["blocks"] if "blocks" in d else [s["name"] for s in run.get("plan") or [] if s.get("waiting") == d["id"]]
    out = f"{crew.who(d.get('from_name') or 'main')} asks" + (
        f"; it blocks {', '.join(f'“{b}”' for b in blocks)}" if blocks else "") + "."
    if d.get("approves"):
        out += f" Your answer approves or sends back {', '.join(f'“{s}”' for s in d['approves'])}."
    return out


def _recommended(d: dict):
    return _named_option(d.get("options"), d.get("recommend"), prefix=True)


def ask_calls(run: dict, ids=None) -> dict:
    """The question tool calls that ask the developer decisions (`ids`, else every pending one, oldest first):
    {"calls": [{"questions": [...]}], "typed": [ids]}. Each question is "D<n> · <question> <who asks, what it
    blocks>" with the header "D<n>" and devkit's labels (ask_labels): the recommended option first, marked
    "(Recommended)". At most four questions go in one call. A decision without 2 to 4 options goes to `typed`: the
    tech lead shows it in the final message and records the developer's reply. Raises StepError for an id the run
    lacks or one already decided by the tech lead or the developer."""
    if ids:
        picked = []
        for i in ids:
            d = _decision(run, i)
            if not d:
                raise StepError(f"no decision {i} in this session's run: dev crew decisions")
            if d.get("state") == "decided" and d.get("by") != "auto":
                raise StepError(f"{d['id']} was already decided: “{d.get('answer')}”")
            if _not_open(d):
                raise StepError(_not_open(d))
            picked += [d] if d not in picked else []
        picked.sort(key=lambda x: int(x["id"][1:]))
    else:
        picked = pending_decisions(run)
    asked, typed = [], []
    for d in picked:
        opts = d.get("options") or []
        if not 2 <= len(opts) <= 4:
            typed.append(d["id"])
            continue
        asked.append({"question": f"{ask_text(d)} {_ask_context(run, d)}", "header": d["id"], "multiSelect": False,
                      "options": [{"label": label, "description": ""} for label in ask_labels(d)]})
    calls = [asked[i:i + ASK_PER_CALL] for i in range(0, len(asked), ASK_PER_CALL)]
    return {"calls": [{"questions": c} for c in calls], "typed": typed}


def crew_decision(question: str, options=None, recommend: str = "", from_agent: str | None = None,
                  cwd: str | None = None, now: float | None = None, approves: str = "") -> dict:
    """A decision point. -> the decision record. In auto mode a decision with a recommendation is taken at once with
    it; in guided mode, without a recommendation, or when it approves steps (only the developer's answer approves
    them: approvals.verdicts), it waits for the developer: the asking member's lane waits and its step is blocked
    until the tech lead records the answer (crew_decide)."""
    from . import messaging
    messaging.validate_body(question)
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
        decs = run.setdefault("decisions", [])
        d = {"id": _next_id(decs, "D"), "ts": round(now), "question": question, "options": opts,
             "recommend": rec, "from": from_agent or "main", "from_name": _name_of(run, from_agent or "main"),
             "state": "pending", "answer": "", "by": ""}
        if approves:  # the developer's answer approves these steps (approvals.verdicts)
            from . import approvals
            try:
                d["approves"] = approvals.decision_steps(run, approves)
            except approvals.ApprovalError as e:
                raise StepError(str(e))
        mode = mode_of(run)
        auto = mode == "auto"
        if auto and rec and not d.get("approves"):
            d.update(state="decided", answer=rec, by="auto", decided=round(now))
        else:
            if from_agent:
                lane = (run.get("agents") or {}).get(from_agent)
                if lane:
                    lane["waiting"] = d["id"]
                for s in coord.held(run, from_agent):
                    if is_review_step(s) and any(not is_review_step(x) for x in coord.held(run, from_agent)):
                        continue  # the lane's build step waits; its reviews close with its report
                    _audit("plan.blocked", s["name"], f"waiting for the developer: {d['id']}",
                           project=run.get("project") or "", by=_name_of(run, from_agent))
                    coord.block(run, s, d["id"], now, f"waiting for the developer: {d['id']}", by=from_agent)
                    # as asked: deciding unblocks them, and devkit's context line still names them
                    d.setdefault("blocks", []).append(s["name"])
                _claim_free_lanes(run, now)  # the blocked step frees its lane for the member's next step
            if auto and d.get("approves"):
                d["note"] = (f"auto mode, but it approves {', '.join(f'“{s}”' for s in d['approves'])}: only the "
                             f"developer's answer approves steps, so it waits for the developer")
            elif auto:
                d["note"] = "auto mode, but no recommendation was given: the developer decides"
        decs.append(d)
        run["decisions"] = decs
        _save(run, touch_session=False)
    _audit("crew.decision", d["id"], d["question"][:300], d["state"], project=run.get("project") or "",
           by=d.get("by") or "", answer=d.get("answer") or "", mode=mode, asked_by=d["from_name"],
           options=d.get("options") or [], labels=" | ".join(ask_labels(d)), session=f"{agent}:{sid}")
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
    """Record the developer's answer, as the tech lead got it (the question tool, or their reply): the waiting
    member's step runs again, and a message with the answer waits for that member (delivered when the tech lead
    resumes it, or to their next launch when their lane ended: _reply_to). An answer replaces an automatic decision
    (`overrides` keeps the automatic one); a decision the tech lead already recorded is final."""
    now = now or time.time()
    _main_only(None, "records the developer's answers", cwd)
    agent, sid = current_session(cwd)
    decision_id = _own_decision_id(decision_id, agent, sid, "answered")
    from . import messaging
    messaging.validate_body(answer)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        d = _decision(run, decision_id)
        if not d:
            raise StepError(f"no decision {decision_id}")
        if _not_open(d):
            raise StepError(_not_open(d))
        if d["state"] == "decided" and d.get("by") != "auto":
            raise StepError(f"{d['id']} was already decided by {d['by']}: “{d['answer']}”")
        prev = d.get("answer")
        d.update(state="decided", answer=answer, by="morpheus", decided=round(now))
        for old in ("proposed", "proposed_ts"):  # what an earlier version kept while it waited for evidence
            d.pop(old, None)
        if prev and prev != d["answer"]:
            d["overrides"] = prev  # the developer changed an automatic decision
        back = _unblock(run, d, now)
        if d["from"] != "main":
            notice = _post(run, "main", d["from"], d["answer"], now, kind="notice",
                           reply_to=d.get("ref") or f"{d['id']}@r-{messaging.run_id(run)}")
            d["answer_ref"] = notice["ref"]
            _ended_asker_notice(run, d["from"], f"the result of their decision {d['id']} ({notice['id']})", now)
        _save(run, touch_session=False)
    _audit("crew.decided", d["id"], d["answer"][:300], project=run.get("project") or "", by=d["by"],
           overrides=d.get("overrides") or "", session=f"{agent}:{sid}", options=d.get("options") or [])
    return dict(d, sent_back=[dict(s) for s in back])  # for the command's own lines; never saved


def _own_decision_id(decision_id: str, agent: str, sid: str, verb: str) -> str:
    """The bare id of a decision of the caller's own run (agent, sid): a qualified id (D2@9448c4d0) of that session
    loses its session; one of another session's run is refused, since a decision is `verb` (answered, withdrawn) in
    the session that owns it, where the developer is asked it."""
    bare, prefix = split_id(decision_id)
    if not prefix:
        return bare
    try:
        target = run_of_prefix(prefix)
    except ValueError as e:
        raise StepError(str(e))
    if target != (agent, sid):
        raise StepError(f"{decision_id} is a decision of another session's run ({target[0]}:{target[1][:12]}): it is "
                        f"{verb} in the session that owns it, where the developer is asked it: there its tech lead "
                        f"runs dev crew decide {bare} \"…\" or dev crew withdraw {bare} --why \"…\"")
    return bare


def _release(run, d: dict, why: str, now: float) -> list:
    """The steps blocked on decision `d`, once it is decided or withdrawn (`why` says which), go back by
    coord.unblock: each runs again in its lane while its member works there and the lane is free; it waits first in
    that lane's intents while the lane works on another step; and first in its member's intents, for that member's
    next lane, when its member ended or went stale, as `dev crew owner` reads it (lane_gone over lane_states of the
    caller's own run; a lane the run does not know counts as working). A lane never runs again because of an answer
    (its wait never changed its state); a step that moved on meanwhile stays as it is. Then every free lane claims.
    -> the steps that wait for a lane"""
    from . import crew
    agents, states, back = run.get("agents") or {}, None, []
    for s in run.get("plan") or []:
        if s.get("waiting") != d["id"]:
            continue
        if s.get("state") != "blocked":
            s.pop("waiting", None)
            continue
        owner = s.get("owner") or "main"
        lane = agents.get(owner)
        if states is None and isinstance(lane, dict) and lane.get("state") == "running":
            states = lane_states(run, now, own=True)
        g = lane_gone(owner, lane, states or {}) if owner != "main" else None
        text = why if not g else (f"{why}; its owner {crew.who(_lane_name(lane))} had "
                                  f"{'gone stale' if g[0] == 'stale' else 'ended'}: {g[1]}")
        if coord.unblock(run, s, not g, now, text, review=is_review_step) == "pending":
            back.append(s)
            if g:  # its lane takes it back once its member works again, after now (approvals._worked_since)
                s.update(lost_from=owner, lost_at=now)
            if not g:
                _post(run, "devkit", owner, f"devkit: {d['id']} is answered, so “{s['name']}” comes back to you: it "
                      f"joins your lane first once {_quoted_and([x['name'] for x in coord.held(run, owner)][:1])} "
                      f"closes", now, from_name="devkit", kind="notice")
        _audit(f"plan.{s['state']}", s["name"], text[:300], project=run.get("project") or "", by="main")
    for lane in agents.values():
        if lane.get("waiting") == d["id"]:
            lane.pop("waiting", None)
            lane.pop("state_before_wait", None)  # kept by runs saved before an answer stopped reviving lanes
    _claim_free_lanes(run, now)
    return [x for x in back if x.get("state") == "pending"]


def _unblock(run, d: dict, now: float) -> list:
    """A decided decision unblocks what waits on it (_release). -> the steps sent back for a new launch"""
    return _release(run, d, f"{d['id']} decided: {d['answer'][:80]}", now)


def crew_withdraw(decision_id: str, why: str, from_agent: str | None = None, cwd: str | None = None,
                  now: float | None = None) -> dict:
    """The tech lead withdraws a pending decision (`dev crew withdraw <id> --why`): state withdrawn, with the reason
    in the run's history and the audit trail. A step blocked on it runs again, or waits for a new launch when its
    member ended or went stale (_release); the original asking lane is told to go on without an answer when its run
    still lists that lane as running, or when the step it blocked waits for a new launch. Its replacement does not
    inherit the notice (_reply_to). Never the developer's answer: it approves no step, and it takes no answer
    later."""
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
        if not tell:
            _ended_asker_notice(run, d.get("from") or "", f"the withdrawal of their decision {d['id']}", now)
        _save(run, touch_session=False)
    _audit("crew.withdrawn", d["id"], why[:300], "withdrawn", project=run.get("project") or "", by="morpheus",
           why=why[:300], was="pending", session=f"{agent}:{sid}")
    if tell:  # keep the original recipient even if another lane takes its step
        crew_ask(f"Decision {d['id']} was withdrawn by the tech lead: {why}. Go on without it; it is not the "
                 f"developer's answer.", to=_reply_to(run, d["from"], now), cwd=cwd, now=now, kind="notice")
    return dict(d, sent_back=[dict(s) for s in back])  # for the command's own lines; never saved


ENDED_LANE = ("done", "stopped", "lost")  # a lost lane that is seen again runs again, and its decisions with it


def _awaiting(run) -> list:
    """Every decision in state pending, moot ones included: an answer to a moot one is still recorded."""
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
    return bool(d.get("told") or d.get("surfaced"))  # `surfaced`: what older runs recorded instead of `told`


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


def untell_decisions(agent: str, sid: str, ids) -> None:
    """Give back decisions taken as told whose line never reached the main session (a wait's write that failed, a
    hook's frame that never went out): each pending one is told again by the next hook or wait."""
    ids = set(ids or ())
    if not ids:
        return
    with _tx(agent, sid):
        run = load_run(agent, sid)
        hit = [d for d in pending_decisions(run or {}) if d.get("id") in ids and d.get("told")]
        for d in hit:
            d.pop("told", None)
        if hit:
            _save(run, touch_session=False)


def untell_ready(agent: str, sid: str, names) -> None:
    """Give back steps taken as told ready to launch (dispatch.take_ready) whose line never reached the main session:
    each is told again while it is still ready."""
    names = set(names or ())
    if not names:
        return
    with _tx(agent, sid):
        run = load_run(agent, sid)
        hit = [s for s in (run or {}).get("plan") or [] if s.get("name") in names and s.get("ready_told")]
        for s in hit:
            s.pop("ready_told", None)
        if hit:
            _save(run, touch_session=False)


def decisions_context(agent: str, sid: str, taken: list | None = None) -> str:
    """For the main session: decisions waiting for the developer, newest first, once per decision. `taken`: a list
    that receives the decisions told (watch.mail renders them one per line for a wait)."""
    run = load_run(agent, sid)
    if not run or all(_told(d) for d in pending_decisions(run)):
        return ""  # the usual case: nothing new, no lock and no configuration read
    # the mode is read before the locked write that marks them told: a mode that cannot be read never loses one
    mode = mode_shown(run)[0]
    run, new = take_decisions(agent, sid)
    if taken is not None:
        taken.extend(new)
    return decisions_text(run, new, mode) if new else ""


def mode_words(mode: str) -> str:
    """"auto mode", "guided mode", or "mode unknown" for mode_shown's "invalid"."""
    return "mode unknown" if mode == "invalid" else f"{mode} mode"


def decisions_text(run: dict, new: list, mode: str = "") -> str:
    """The main session's context for decisions that wait for the developer: how to ask them, then one line each.
    `mode`: as mode_shown read it (read here when not given)."""
    from . import crew
    lines = ["devkit · decisions waiting for the developer (" + mode_words(mode or mode_shown(run)[0]) + "). Ask them "
             "with your question tool (AskUserQuestion) in the calls `dev crew decisions --ask` prints: a plain "
             "question with its context, “D<n> ·” up front, the option letters, the recommended option first; reword "
             "them freely, or ask them in your final message. Then record the answer with `dev crew decide <id> "
             "\"<answer>\"` and resume the member who asked (SendMessage / send_input):"]
    for d in new:
        opts = (" Options: " + " | ".join(d["options"])) if d["options"] else ""
        lines.append(f"- {d['id']} from {crew.who(d['from_name'])}: “{ask_text(d)}”{opts}"
                     + (f" Recommended: {d['recommend']}." if d.get("recommend") else ""))
    return "\n".join(lines)


def waiting_context(agent: str, sid: str, since, now: float) -> str:
    """UserPromptSubmit, when the developer is back: the decisions that wait for them and were not shown to them since
    their last prompt (`since`), in one line for the tech lead to ask them together. Once per batch: again only when
    a new decision joins the ones already listed."""
    run = load_run(agent, sid)
    pend = pending_decisions(run)
    if not pend:
        return ""  # the usual case, without the lock
    waiting = [d for d in pend if not (since and (d.get("shown") or 0) >= since)]
    if not waiting or all(d.get("reminded") for d in waiting):
        return ""
    ids = [d["id"] for d in waiting]
    k = max(1, len(ask_calls(run, ids)["calls"]))
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
    shown = []
    text = messages_text(msgs, normalize(load_run(agent, sid) or {}) if msgs and me == "main" else None, shown=shown)
    acknowledge_on_delivery(agent, sid, me, shown)
    return text


def messages_text(msgs: list, run=None, shown: list | None = None) -> str:
    """The hook text for crew messages taken for their recipient: how to answer, then one line each; with the run, a
    question nobody waits on any more says it is moot, and why. `shown`: a list that receives the messages whose
    frame the text holds (one that did not fit is left out, and comes back)."""
    if not msgs:
        return ""
    from . import messaging
    lines = ["devkit · crew messages: a complete one is the whole message; read every page of a preview before acting "
             "and acknowledge its exact digest."]
    budget = messaging.OUTPUT_MAX - 160 - len(lines[0].encode("utf-8"))
    omitted = 0
    for m in msgs:
        frame = messaging.notification(m, remaining_bytes=budget, read=read_on_delivery(m))
        if frame:
            lines.append(frame)
            budget -= len(frame.encode("utf-8")) + 1
            if shown is not None:
                shown.append(m)
        else:
            omitted += 1
    if omitted:
        lines.append(f"{omitted} previews omitted; dev crew inbox lists their references.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- decisions as the developer reads them

_RECOMMENDED = re.compile(r"\s*\(recommended\)\s*$", re.IGNORECASE)


def _norm(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def ask_text(d: dict) -> str:
    """The decision as devkit words it for the developer: "D<n> · <question>". The tech lead may reword it and add
    context, keeping "D<n> ·" up front and the option letters."""
    return f"{d['id']} · {d['question']}"


def _letter(option: str) -> str:
    """"B" for "B: reject"; "" for an option without a one-letter label."""
    head, sep, _ = str(option).partition(":")
    return head.strip().upper() if sep and len(head.strip()) == 1 and head.strip().isalpha() else ""


def option_of(d: dict, text):
    """The decision's option an answer names: the option, with or without the question tool's "(Recommended)", its
    letter ("B" for "B: reject") or its words without the letter ("reject"); None when none or several do."""
    return _named_option(d.get("options"), text)


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


def ask_labels(d: dict) -> list:
    """A decision's option labels as devkit offers them (ask_calls): the recommended option first, marked
    "(Recommended)", then the others in their order."""
    opts = d.get("options") or []
    rec = _recommended(d)
    return [o + (" (Recommended)" if o == rec else "") for o in ([rec] if rec else []) + [o for o in opts if o != rec]]


def _decision(run, did):
    return next((x for x in (run or {}).get("decisions") or [] if x["id"].lower() == str(did).lower()), None)


# --------------------------------------------------------------------------- the developer's prompts and sessions

HEADLESS_SURFACES = ("SDK", "claude -p", "codex exec")  # nobody at the keyboard
# a prompt that relays something rather than the developer's own words: a comment sent to the agent, a notification,
# a reminder or a command's output
_RELAYED = re.compile(r"^\s*(?:\[[^\]\n]{0,120}\bsent to (?:Claude|Codex)\b[^\]\n]*\]|\[SYSTEM NOTIFICATION|"
                      r"<task-notification|<system-reminder|<agent-message|<command-name>|<local-command)",
                      re.IGNORECASE | re.MULTILINE)
# D3, or D3@9448c4d0 as a decision listed outside its own session shows it (shown_id): the number and the prefix


def headless(sess: dict) -> bool:
    """A session with nobody at the keyboard (`claude -p`, the SDK, `codex exec`, devkit's own headless calls), or a
    session another agent session started (nested), whatever surface it inherited: Claude Code rewrites an inherited
    `cli` entrypoint to `sdk-cli` for `-p`, but keeps an inherited `claude-desktop` or `claude-vscode` and any value
    set on the command line. A detached session (no terminal and no application above its process) is one too: a
    background launch from an agent's shell that launchd adopted. None of its prompts is the developer's."""
    return _headless_surface(sess) or nested(sess) or detached(sess)


def _headless_surface(sess: dict) -> bool:
    return str((sess or {}).get("surface") or "") in HEADLESS_SURFACES or os.environ.get("DEVKIT_HEADLESS") == "1"


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


_CODEX_TURN_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CODEX_PROMPT_ID = re.compile(rf"codex-prompt:({_CODEX_TURN_ID}):([0-9a-f]{{64}})")


def _codex_prompt_id(turn: str, text: str) -> str:
    """One exact typed prompt within a Codex turn; identical text in that turn remains one-use."""
    if not re.fullmatch(_CODEX_TURN_ID, turn):
        return ""
    return f"codex-prompt:{turn}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _codex_prompt_parts(identity: str) -> tuple[str, str]:
    """(turn, text digest) of a Codex prompt identity (_codex_prompt_id), or (turn, "") for a bare turn id that
    older records keep; ("", "") for anything else, a malformed composite identity included."""
    match = _CODEX_PROMPT_ID.fullmatch(identity)
    if match:
        return match.group(1), match.group(2)
    legacy = not identity.casefold().startswith("codex-prompt") and _SESSION_ID.fullmatch(identity)
    return (identity, "") if legacy else ("", "")


_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def harness_transcript(agent: str, sid: str) -> str:
    """The session's own transcript, where the harness writes it, found from the session id alone, never from a path
    a hook event or a caller names: Claude Code's
    ~/.claude/projects/<project folder>/<session id>.jsonl, or Codex's rollout of the thread,
    ~/.codex/sessions/YYYY/MM/DD/rollout-<time>-<thread id>.jsonl (or archived_sessions/, where `codex archive`
    moves it), when it opens with that thread's own session_meta (_codex_meta): a subagent's rollout, which copies its
    parent's history, is never a session's. '' when there is none or more than one, or the id is not a plain id."""
    sid = str(sid or "")
    if agent not in ("claude", "codex") or not _SESSION_ID.fullmatch(sid):
        return ""
    try:
        if agent == "claude":
            hits = sorted((core.CLAUDE_HOME / "projects").glob(f"*/{sid}.jsonl"))
        else:
            name = f"rollout-*-{sid}.jsonl"
            hits = sorted([*(core.CODEX_HOME / "sessions").glob(f"*/*/*/{name}"),
                           *(core.CODEX_HOME / "archived_sessions").glob(name),
                           *(core.CODEX_HOME / "archived_sessions").glob(f"*/*/*/{name}")])
    except OSError:
        return ""
    if len(hits) != 1 or (agent == "codex" and _codex_meta(hits[0], sid) is None):
        return ""
    return str(hits[0])


CODEX_META_MAX = 8 << 20  # bytes of a rollout's first line read for its session_meta (it holds the instructions)


def _codex_meta(path, sid: str):
    """The session_meta payload a Codex rollout opens with, when it is thread `sid`'s own and no subagent's (no
    `source.subagent`, parent thread or copied history); None otherwise or when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            first = json.loads(fh.readline(CODEX_META_MAX) or b"null")
    except (OSError, ValueError, RecursionError):
        return None
    p = first.get("payload") if isinstance(first, dict) and first.get("type") == "session_meta" else None
    if not isinstance(p, dict) or p.get("id") != sid:
        return None
    src = p.get("source")
    if (isinstance(src, dict) and "subagent" in src) or p.get("parent_thread_id") \
            or p.get("subagent_history_start_ordinal") is not None:
        return None
    return p


def _codex_prompt(r, sid: str):
    """The text of Codex rollout record `r` when it is a prompt the developer typed in thread `sid`: an `event_msg`
    `item_completed` whose item is a `UserMessage` of that thread, which Codex 0.167 writes for each typed prompt
    after UserPromptSubmit. Agent messages, tool calls and their output, and what Codex or a hook adds to the context
    (developer messages, the AGENTS.md block in the user's role) are other records. None for any other record."""
    p = r.get("payload") if isinstance(r, dict) and r.get("type") == "event_msg" else None
    item = p.get("item") if isinstance(p, dict) and p.get("type") == "item_completed" else None
    if not isinstance(item, dict) or item.get("type") != "UserMessage" or p.get("thread_id") != sid:
        return None
    return "\n".join(b["text"] for b in item.get("content") or []
                     if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))


def prompt_turn_id(data: dict) -> str:
    """The native id a UserPromptSubmit payload names its turn by: Claude Code's prompt_id, Codex's turn_id (Codex
    sends no prompt id): the raw id."""
    data = data if isinstance(data, dict) else {}
    return str(data.get("prompt_id") or data.get("turn_id") or "")


def midturn_of(sess, prompt_id) -> bool:
    """Is a UserPromptSubmit with native turn id `prompt_id` (prompt_turn_id) a message typed while the session's turn
    runs? Claude Code runs the hook for such a message with the running turn's prompt_id, Codex for a steering message
    with the running turn's turn_id. The session record keeps the native id of the prompt that opened the running turn
    (`turn_prompt`, which the prompt hook writes after this is read) and the turn's start (`turn_started`, which Stop
    clears when the turn ends). False on any doubt: no id, no turn running, another id. The prompt hook
    (hooks._midturn_prompt) decides by this."""
    return bool(prompt_id) and isinstance(sess, dict) and bool(sess.get("turn_started")) \
        and sess.get("turn_prompt") == str(prompt_id)


# A mid-turn message's attachment is stamped when the developer types it, and its prompt hook runs when the turn
# absorbs it, after the tool call that was running ends (0.4 s seen live, 163 s after a long call): the attachment
# must lie within the running turn, from its start to the hook, give or take MIDTURN_GRACE seconds of clock skew.



def begin(which, cwd: str | None = None, now: float | None = None, from_agent: str | None = None) -> dict:
    """Mark a step as started now (parallel work the main agent does itself). The run remembers it (`begun`), so the
    tech lead's next `dev progress step` without --step closes that step or nothing (_refuse_other_than_begun).
    Refused for a step already done or skipped: there is nothing to begin, and the refusal says how to add evidence
    to it or reopen it."""
    _main_only(from_agent, "starts the plan's steps", cwd)
    now = now or time.time()
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        s = _find_step(run, which)
        if not s:
            raise ValueError(f"no step “{which}” in the plan")
        if s.get("state") in DONE:
            raise StepError(f"step {_closed_already(s)}, so there is nothing to begin (dev progress history says who "
                            f"closed it). To add your evidence to it: {_evidence_line(s)}; to work on it again: "
                            f"dev progress reopen \"{s['name']}\" --why \"…\"")
        waiting = deps_open(run, s)
        if waiting:
            raise StepError(f"step “{s['name']}” waits for {', '.join(waiting)}, not done yet: it starts when "
                            f"{'that is' if len(waiting) == 1 else 'they are'} done. If the order really changed, "
                            f"skip what it waits for: dev progress skip \"{waiting[0]}\" --why \"…\"")
        old = s.get("state")
        s.update(state="running", started=s.get("started") or now)
        if old != "running":
            _log_change(run, s, old, "running", "main", "the tech lead began it (dev progress begin)", now)
        run["begun"] = s["name"]
        _save(run)
    return run


def _closed_already(s) -> str:
    return f"“{s['name']}” is already {s.get('state')}"


def _evidence_line(s) -> str:
    """How the tech lead adds evidence to a step that is already closed: confirm clears a recheck flag, a note with
    --step keeps the evidence on the step."""
    if s.get("recheck"):
        return f"dev progress confirm \"{s['name']}\" --why \"…\""
    return f"dev progress note \"…\" --step \"{s['name']}\" --check \"…\""


def _refuse_other_than_begun(run, target, text):
    """The tech lead's `dev progress step` without --step closes the step he meant or nothing. A description that
    names a closed step ("v23-verify: …") and a step begun with `dev progress begin` that is closed or is not the
    target are refused, with the commands that do what he meant: otherwise the running step, a different one, closed
    in its place. Raises StepError."""
    plan = run.get("plan") or []
    named = _named_by(plan, text, closed=True)
    if named is not None:
        raise StepError(f"step {_closed_already(named)}, so this closes nothing. To add your evidence to it: "
                        f"{_evidence_line(named)}; to close another step, name it: --step \"<step>\"")
    begun = _by_name(plan).get(run.get("begun") or "")
    if begun is None or begun is target:
        return
    other = f"“{target['name']}”" if target else "another step"
    close_other = f"--step \"{target['name']}\"" if target else "--step \"<step>\""
    if begun.get("state") in DONE:
        raise StepError(f"step “{begun['name']}”, which you began, is already {begun.get('state')}, so this would "
                        f"close {other} instead. To add your evidence to “{begun['name']}”: {_evidence_line(begun)}; "
                        f"to close {other}, name it: {close_other}")
    raise StepError(f"you began step “{begun['name']}”, but this would close {other}: name the step you finished, "
                    f"--step \"{begun['name']}\" or {close_other}")


class StepError(ValueError):
    """A step change devkit refuses, with what to do instead."""


class CodexSessionError(StepError):
    """A Codex command has no consistent session owner; report a refusal rather than a crash."""


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
    if a == b:  # the same word, however short: ci, qa, db
        return True
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


def mark_calls(marks: dict | None = None) -> dict:
    """Resolve who runs this `dev` process once, at its start (cli.main), when its call carries the call token: each
    history entry it writes then names how the caller was found and the call's tool_use_id (`how`, `tid`). `marks`
    sets them as given ({} clears them). -> the marks"""
    if marks is None:
        from . import identity
        token = identity.parse_token(os.environ.get(identity.CALL_VAR) or "")
        marks = {}
        if token:
            try:
                marks = {"how": identity.speaker().how, "tid": token[0]}
            except Exception:
                marks = {"how": "unknown", "tid": token[0]}
    coord.HISTORY_MARKS.clear()
    coord.HISTORY_MARKS.update(marks)
    return dict(coord.HISTORY_MARKS)


def _log_change(run, s, old, new, who, why, now):
    run.setdefault("history", []).append({"ts": round(now), "step": s["name"], "from": old, "to": new,
                                          "by": who or "main", "why": (why or "")[:300], **coord.HISTORY_MARKS})
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
            previous_owner = s.get("owner") or "main"
            if previous_owner != "main":
                previous = run.get("agents", {}).get(s["owner"])
                if previous is not None:
                    _remember_step(previous, s)
                s["owner"] = "main"  # back to Morpheus, still assigned to its member: dev crew next launches it again
            if old in DONE:
                _recheck_dependents(run, s, now)
            if not s.get("label") and not deps_open(run, s):
                for x in run["plan"]:  # the reopened step becomes the main lane's current one
                    if x is not s and x.get("state") == "running" and (x.get("owner") or "main") == "main":
                        x["state"] = "pending"
                        coord._history(run, x, "running", "pending", "main", f"“{s['name']}” was reopened and "
                                       f"runs in its place", now)
                s["state"] = "running"
                s["started"] = s.get("started") or now
            elif s.get("label"):  # its member still works: back to their lane, now or once its open step closes
                # Routing must see the former exact owner before it records the
                # durable intent; ownership itself remains pending on main.
                s["owner"] = previous_owner
                _route_to_lane(run, s, s["label"], working_agents(run, now, own=True), now)
                if s.get("state") == "pending":
                    s["owner"] = "main"
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
         now: float | None = None, which=None, check: str = "") -> dict:
    """An update while a step is still running: recorded and shown (it resets the silence timer), and no step
    changes state. `dev progress step` is for a step that is finished. `which` and `check`: partial evidence for a
    step that is not finished (`dev progress note "…" --step <name> --check "…"`), kept on
    that step (`partial`, its last 5) and in the update, while the step stays as it is; a member's goes on a step its
    lane holds, and stays in its lane for any other step."""
    now = now or time.time()
    from_agent = _as_lane(from_agent, cwd)
    if not (text or "").strip():
        raise StepError("say what is happening: dev progress note \"<what is happening>\" --next \"<what is next>\"")
    agent, sid = current_session(cwd)
    with _tx(agent, sid):
        run = normalize(load_run(agent, sid) or {})
        if not run.get("goal"):
            raise StepError("no progress run in this session: dev progress start \"<goal>\" --plan \"…\"")
        named = _find_step(run, which) if which not in (None, "") else None
        if which not in (None, "") and not named:
            raise StepError(f"no step “{which}” in the plan: " + "; ".join(
                f"{i}. {s['name']}" for i, s in enumerate(run.get("plan") or [], 1)))
        u = {"ts": round(now), "kind": "note", "text": text.strip(), "next": nxt, "check": (check or "").strip()}
        if named:
            u["step"] = named["name"]
            if u["check"] and (not from_agent or named.get("owner") == from_agent):
                named["partial"] = (named.get("partial") or [])[-4:] + [
                    {"ts": u["ts"], "by": from_agent or "main", "text": u["text"], "check": u["check"]}]
        if from_agent:  # a member's note stays in its lane
            lane = _lane(run, from_agent, now)
            lane["updates"] = (lane.get("updates") or [])[-19:] + [u]
            run["updates"].append({**u, "kind": "agent", "agent": from_agent})
        else:
            run["updates"].append(u)
            if named and named["name"] == run.get("begun") and named.get("state") in DONE:
                run.pop("begun", None)  # his evidence is on the begun step, which someone else closed
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
    """The tech lead's `dev progress step --approves "<steps>"`: `by`'s verdict on the named steps, from the step's
    Check, recorded at once; a member's waits in its lane (_approve_later). ApprovalError becomes
    StepError."""
    from . import approvals
    try:
        approvals.record(run, by, approves, approvals.verdict_of(check), now, check=check,
                         doer_of=lambda s: member_of_step(run, s))
    except approvals.ApprovalError as e:
        raise StepError(str(e))


def _approve_later(run: dict, lane: dict, by: str, approves: str, check: str, now: float) -> str:
    """A member's `dev progress step --approves "<steps>"` from its lane: checked now as approvals.record would record
    it, so a verdict it could never give is refused at once, then kept in the lane (`verdicts`), where it counts only
    at the lane's end, once the crew gates passed that stop cleanly (_lane_verdicts), as a review step's Check does.
    -> the line the member hears."""
    from . import approvals
    try:  # on a copy: nothing is recorded yet
        entry = approvals.record({**run, "approvals": list(run.get("approvals") or [])}, by, approves,
                                 approvals.verdict_of(check), now, check=check, doer_of=lambda s: member_of_step(run, s))
    except approvals.ApprovalError as e:
        raise StepError(str(e))
    lane["verdicts"] = (lane.get("verdicts") or [])[-19:] + [{"ts": now, "by": by, "steps": entry["steps"],
                                                              "check": str(check)[:300]}]
    names = ", ".join(f"“{n}”" for n in entry["steps"])
    return f"your verdict on {names} counts once your report passes the crew gates"


def _lane_verdicts(run: dict, lane: dict, aid: str, passed: bool, now: float):
    """At a lane's end, the `--approves` verdicts its member gave (_approve_later): each is recorded as given, at the
    time it was given (approvals.record), when the crew gates passed this stop cleanly (`passed`), and is not counted
    otherwise, with the reason in the history of each step it named and in the audit trail, so that work stays owed
    until a verdict the gates pass. A verdict the run no longer takes (the step was reopened since) is audited."""
    from . import approvals
    held = lane.pop("verdicts", None) or []
    who = lane.get("persona") or "the member"
    steps = {s.get("name"): s for s in run.get("plan") or []}
    for v in held:
        names = [n for n in v.get("steps") or [] if n in steps]
        if not passed:
            why = (f"{who}'s verdict on it (dev progress step --approves) is not counted: the crew gates did not pass "
                   f"its report (sent back, only recorded, or not judged)")
            run.setdefault("_uncounted", []).extend(f"its verdict on “{n}”" for n in names)
            for n in names:
                st = steps[n]
                run.setdefault("history", []).append({"ts": round(now), "step": n, "from": st.get("state") or "-",
                                                      "to": st.get("state") or "-", "by": aid, "why": why})
            run["history"] = (run.get("history") or [])[-200:]
            _audit("crew.approval-refused", ", ".join(names) or "?", why, "refused", project=run.get("project") or "")
            continue
        try:
            approvals.record(run, v["by"], names, approvals.verdict_of(v.get("check")), v["ts"], check=v.get("check"),
                             doer_of=lambda s_: member_of_step(run, s_))
        except (approvals.ApprovalError, KeyError, TypeError) as e:
            _audit("crew.approval-refused", ", ".join(names) or "?", str(e)[:300], "refused",
                   project=run.get("project") or "")


def _tell_uncounted(run: dict, lane: dict, now: float, findings: str = ""):
    """At a lane's end, one devkit notice to the tech lead: what the crew gates still found in the stop they let
    through (`findings`, the hook's summary, hooks._findings_told), and the reviews and verdicts its member gave that
    do not count (_end_member_step, _lane_verdicts), since the gates did not pass its report. Nothing when there is
    neither."""
    held = list(dict.fromkeys(run.pop("_uncounted", None) or []))
    if not held and not findings:
        return
    from . import crew
    who = crew.who(lane.get("persona") or lane.get("label") or "a member")
    owed = f"{_quoted_list(held)} count{'s' if len(held) == 1 else ''} for nothing yet" if held else ""
    if findings:
        text = (f"devkit: {who} stopped, and the crew gates let it through with what is still missing: {findings}. "
                + (f"So {owed}: check its work, or send it back with the fix (resume the member, or launch the review "
                   f"again)." if held else "A review or verdict of that stop counts for nothing: check its work, or "
                   "send it back with the fix."))
    else:
        text = (f"devkit: {who}'s report did not pass the crew gates, so {owed}: the work stays owed until a report "
                f"the gates pass (resume the member, or launch the review again)")
    _post(run, "devkit", "main", text, now, from_name="devkit", kind="notice")


def _quoted_list(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def step(text: str, nxt: str = "", cwd: str | None = None, kind: str = "step", check: str = "", which=None,
         from_agent: str | None = None, now: float | None = None, force: bool = False, why: str = "",
         approves: str = "") -> dict:
    """Record a step of the run: Morpheus's closes a plan step, a member's goes to its lane; `approves` names the
    finished steps the Check approves or sends back (approvals.record): Morpheus's verdict counts at once, a member's
    once the crew gates pass its report (_approve_later). kind "done" ends the run, refused while a
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
    # read before the lock: the member's transcript can be long
    running = _background_running(agent, sid, from_agent) if from_agent_lane and kind == "step" and \
        str(check or "").strip() else []
    with _tx(agent, sid):
        run = load_run(agent, sid)
        if from_agent_lane:  # a subagent's update: its lane only
            run = normalize(run)
            named = _find_step(run, which) if which not in (None, "") else None
            if which not in (None, "") and not named:
                raise StepError(f"no step “{which}” in the plan: " + "; ".join(
                    f"{i}. {s['name']}" for i, s in enumerate(run.get("plan") or [], 1)))
            before = coord.states(run)
            implicit = named is None and kind in ("step", "blocked")
            if implicit:  # the step its lane runs, as the tech lead's record without --step closes his own
                builds = [x for x in coord.held(run, from_agent) if not is_review_step(x)]
                if len(builds) > 1:
                    raise StepError("your lane runs several steps (" + ", ".join(x["name"] for x in builds) +
                                    "): say which with --step; devkit recorded nothing")
                named = builds[0] if builds else None
            refused = _member_close_refused(run, named, from_agent, check, running) \
                if kind == "step" and named and (not implicit or str(check or "").strip()) else ""
            if refused:
                raise StepError(refused)
            lane = _lane(run, from_agent, now)
            u = {"ts": round(now), "kind": kind, "text": text, "check": check}
            if named:  # the step this evidence is about: the lane's end closes it (_steps_about)
                u["step"] = named["name"]
            lane["updates"] = (lane.get("updates") or [])[-19:] + [u]
            if kind == "step":
                lane["done"] = (lane.get("done") or 0) + 1
            held = ""
            if approves:
                from . import crew
                mem = crew._for_lane(lane)
                if not mem:
                    raise StepError("this lane names no crew member, so it approves nothing: report the verdict, and "
                                    "the step's approver records it")
                held = _approve_later(run, lane, mem["id"], approves, check, now)
            run["updates"].append({"ts": round(now), "kind": "agent", "agent": from_agent, "text": text, "check": check})
            run["updates"] = run["updates"][-40:]
            outcome = _member_close(run, named, lane, from_agent, u, now, agent, sid) if kind == "step" and named \
                else None
            if kind == "blocked" and named:
                if named.get("owner") == from_agent and named.get("state") == "running":
                    coord.block(run, named, "", now, text, by=from_agent)
                    _audit("plan.blocked", named["name"], text[:300], project=run.get("project") or "",
                           by=_name_of(run, from_agent))
                    _claim_free_lanes(run, now)  # the blocked step frees its lane for the member's next step
                    outcome = {"step": named["name"], "state": "blocked", "closed": False, "why": ""}
                else:
                    outcome = {"step": named["name"], "state": named.get("state"), "closed": False,
                               "why": f"“{named['name']}” is not a step your lane runs, so devkit blocked nothing"}
            if outcome is None and kind in ("step", "blocked"):
                outcome = {"step": "", "state": "", "closed": False,
                           "why": "recorded in your lane; it closes no step, since your lane runs none"}
            if held:  # the command's line says why nothing is approved yet
                outcome = dict(outcome or {"step": "", "state": "", "closed": False},
                               why="; ".join(x for x in ((outcome or {}).get("why"), held) if x))
            run["step"] = sum(1 for x in run.get("plan") or [] if x.get("state") in DONE)
            _save(run)
            if outcome:
                # for the command's own line; never saved: the steps whose state this record changed, and the
                # verdict it leaves for the crew gates
                run["lane_outcome"] = dict(outcome, changed=coord.changes(before, run), held=bool(held))
            return run
        run = normalize(run or {})
        plan = run["plan"]
        target = _find_step(run, which) if which else None
        if which and not target:
            raise StepError(f"no step “{which}” in the plan: " + "; ".join(f"{i}. {s['name']}" for i, s in
                                                                          enumerate(plan, 1)))
        if kind == "step":
            target = target or _named_by(plan, text)  # "ci-watch: …" names the open step ci-watch
            explicit = target is not None
            if not target:
                running = [s for s in plan if s.get("state") == "running" and (s.get("owner") or "main") == "main"]
                if len(running) > 1:
                    raise StepError("several steps are running (" + ", ".join(s["name"] for s in running) +
                                    "): say which with --step")
                target = (running or [None])[0] or next((s for s in plan if s.get("state") in ("pending", "blocked")
                                                         and (s.get("owner") or "main") == "main"
                                                         and not s.get("label")), None)
                if plan:
                    _refuse_other_than_begun(run, target, text)
            gone = ""
            if not plan:  # a run without a plan: each step is recorded as it comes
                target = _new_step(text[:60], est=None)
                plan.append(target)
                _allocate(run)
            else:
                gone = check_close(run, target, text, explicit, force, now)
            old_state = target.get("state")
            _finish_step(run, target, now, agent, sid)
            if (_by_name(plan).get(run.get("begun") or "") or {}).get("state", "done") in DONE:
                run.pop("begun", None)  # the begun step is closed: the next step without --step is free again
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
                _audit("plan.blocked", cur["name"], text[:300], project=run.get("project") or "", by="morpheus")
                coord.block(run, cur, "", now, text)
                run["blocked_on"] = cur["name"]  # the run runs again once this step moves (_settle)
                _claim_free_lanes(run, now)  # a member's blocked step frees its lane for its next step
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
    """Who in the crew works on what in this run, running first, then the slots in use and why the limit is lower:
    "Tank ▸ the API · Kid ✓ installer · 1 of 4 slots in use". '' without crew members (the agent's own
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
    moot = moot_decisions(run)
    if moot:  # asked by a lane that ended, and no step waits on them: not the developer's, on a line of their own
        lines += [""] + [f"Moot: {d['id']} · “{(d.get('question') or '')[:70]}”: {_moot_reason(run, d)}. It closes "
                         f"with the run, or: dev crew withdraw {d['id']} --why \"…\"" for d in moot]
    if plan:
        lines.append("")
        lines += plan_lines(run, now)
    if agents:
        parts = []
        for aid, a in agents.items():
            mins = active_minutes(run, a.get("started"), a.get("finished") or now, now, aid)
            parts.append(f"{'▸' if a.get('state') == 'running' else '✓'} "
                         f"{_who_label(a.get('label') or a.get('type') or aid[:8])} {fmt_min(mins)}")
        lines.append("Agents: " + " · ".join(parts))
    return "\n".join(lines)


def step_marks(run: dict) -> dict:
    """{step name: what its line shows that a change can move (state, owner, label, estimate, dependencies)}: taken
    before and after a change, the steps whose marks differ are the ones the change touched (changed_lines)."""
    return {s["name"]: (s.get("state"), s.get("owner"), s.get("label"), s.get("est"), tuple(s.get("after") or ()))
            for s in normalize(run).get("plan") or []}


def changed_lines(run: dict, before: dict, now: float | None = None) -> str:
    """What `dev progress assign`, `add`, `begin`, `reopen`, `skip` and `confirm` print: the line of each step the
    change touched (step_marks before it), then the run's headline. `--verbose` prints the whole update instead."""
    now = now or time.time()
    after = step_marks(run)
    names = {name for name, mark in after.items() if before.get(name) != mark}
    names |= {s["name"] for s in normalize(run).get("plan") or [] if s.get("state") == "running"} if not names else set()
    lines = plan_lines(run, now, only=names)
    return "\n".join(lines + [f"  {headline(run, now)} · the whole plan: add --verbose"])


def plan_lines(run: dict, now: float, only=None) -> list:
    """One line per plan step, with its state, owner, what it waits for and its time; `only`: those step names."""
    run = normalize(run)
    plan, agents, lines = run.get("plan") or [], run.get("agents") or {}, []
    for i, s in enumerate(plan, 1):
        if only is not None and s["name"] not in only:
            continue
        mark = {"done": "✓", "running": "▸", "blocked": "■", "skipped": "–"}.get(s.get("state"), "○")
        main = (s.get("owner") or "main") == "main"
        waits = deps_open(run, s) if s.get("state") in ("pending", "blocked") else []
        parts = [] if main else [_who_label(agents.get(s["owner"], {}).get("label") or "subagent")]
        if waits:
            parts.append("waiting for " + ", ".join(waits) + (f", then {_who_label(s['label'])}"
                                                              if main and s.get("label") else ""))
        elif main and s.get("label") and s.get("state") not in DONE:  # a finished step waits for nobody
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
    return lines


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


def notify(title: str, text: str, security: bool = False, event=None):
    """Delegate safe desktop notifications after preferences; security notices bypass those preferences.
    A committed event carries metadata only. Adapter acceptance never acknowledges a message."""
    if not security and (not cfg().get("notify") or os.environ.get("DEVKIT_NO_NOTIFY")):
        return
    # a lone surrogate (JSON can carry one) cannot reach a process: it would raise here, and the notice would not show
    try:
        from . import notifications
        if event is not None:
            return notifications.notify_event(dict(event, category="security") if security else event)
        return notifications.notify(title, text, category="security" if security else "progress")
    except Exception:
        pass


def active_runs(max_age_h: float = 12, first=None) -> list:
    """The live runs on this Mac, newest first. `first`: an (agent, session id) whose run comes ahead of every other
    one, so `dev progress` shows the caller's own run first however recently another session started one (a
    status block once took the step counts of the run listed first)."""
    out = []
    for f in RUNS.glob("*.json") if RUNS.exists() else []:
        r = core.read_json(f, None)
        if r and r.get("state") != "orphaned" and time.time() - f.stat().st_mtime < max_age_h * 3600:
            out.append(r)  # an orphaned run (its session ended, approvals.reconcile) is not live
    out.sort(key=lambda r: -r.get("started", 0))
    if first:
        agent, sid = first
        out.sort(key=lambda r: not (r.get("agent") == agent and str(r.get("session") or "") == str(sid)))
    return out


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


def on_prompt(agent: str, data: dict, now: float | None = None, midturn: bool = False):
    """UserPromptSubmit: start the turn clock; answer status questions from real progress data; when decisions wait
    for the developer, tell the tech lead to ask them together (waiting_context). midturn: the message was typed
    while the session works (the hook carries the running turn's prompt_id), so the running turn goes on: its
    turn_started, the per-turn flags tied to it, its reminder count and its timing interval stay, and the message
    becomes the developer's latest request (request_ts, which is_latest_request reads)."""
    sid = data.get("session_id") or "-"
    f, s = _sess(agent, sid)
    now = now or time.time()
    if f.exists():
        fields = {"last_prompt": now, "request_ts": now} if midturn else \
            {"turn_started": now, "last_update": now, "nudges": 0, "last_prompt": now, "request_ts": None}
        try:
            state.update(f, lambda d: d.update(fields), create=False)
        except state.StateBusy:
            pass  # audited by state: this prompt's clock is dropped, the hook still answers
    if not midturn:
        _run_event(agent, sid, lambda run: (_close(run, "turn", None, now), _open(run, "turn", "main", now)))
    taken = ""
    try:
        took = take_in_saved_auto(agent, sid, now)
        taken = "devkit · " + "; ".join(
            f"auto mode (the saved configuration) took {d['id']} with the recommended option {_recommended(d) or d['answer']}"
            for d in took) + ". The developer can revisit it with dev crew decisions." if took else ""
    except Exception:
        pass  # the decisions keep waiting, and are listed below
    prompt = (data.get("prompt") or "")[:400]
    mail = "\n\n".join(x for x in (taken, _mail(agent, sid, "main"),
                                    waiting_context(agent, sid, s.get("last_prompt"), now)) if x) \
        if f.exists() else taken
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
    """Every hook of the session: the agent whose hook this is was seen (its lane's `last_seen`), and
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
    if agent == "codex":
        from . import codexlaunch
        stopped += codexlaunch.reconcile_lifecycle(run, sess, now)
        if stopped:
            run = load_run(agent, sid)
            sess = state.read(session_path(agent, sid))
    aid = str(data.get("agent_id") or "")
    lane = (run.get("agents") or {}).get(aid) if aid else None
    seen = bool(lane) and now - float(lane.get("last_seen") or 0) >= LAST_SEEN_EVERY
    # the meta files beside the session's own transcript, never beside a path the event names
    fx = approvals.facts(run, sess, now, meta=approvals.meta_reader(  # Codex keeps no meta files by its rollout
        harness_transcript(agent, sid) if agent == "claude" else ""))
    if aid in fx["lanes"]:
        fx["lanes"][aid]["last_seen"] = now
    lim = approvals.limits()
    moves = approvals.reconcile(run, fx, now, lim)
    if not (seen or moves):
        return stopped if agent == "codex" else []
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
    """PostToolUse of TaskStop on a subagent's task: Claude Code fires no SubagentStop
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
            here = approvals.apply(run, approvals.stop_moves(run, aid, why, at=now, now=now), now)
            if here:
                _save(run, touch_session=False)
        if here:
            approvals.audit_moves(run, here)
            made += here
    return made


def live_holder(run: dict, aid: str, s: dict, now: float | None = None):
    """The live lane of the same member that holds a stopped lane's step: another working lane of that member (one
    whose member still works, as `dev crew owner` reads it: working_agents), launched by Morpheus and bound to its
    launch, that holds no open step and whose launch names this step best (as _bind_step chooses). None when there
    is none, or two name it equally."""
    from . import crew
    agents = run.get("agents") or {}
    member = crew._for_lane(agents.get(aid) or {})
    if not member:
        return None
    working = {k for k, _ in working_agents(run, now, own=True)}
    scored = []
    for k, lane in agents.items():
        if k == aid or k not in working or lane.get("binding", "exact") not in ("exact", "confirmed") \
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
        run = load_run(agent, old)
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
            _save(run, touch_session=False)
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
    """A resumed session takes back its run the reconciler orphaned when the session ended. -> resumed"""
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
LAUNCH_KEEP = 24 * 3600  # launch records are kept a day, the newest LAUNCH_MAX of them
LAUNCH_MAX = 100
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
BOUND = ("exact", "conflict", "unbound", "confirmed")  # a lane whose binding no later hook changes


def _launches(s: dict, now: float) -> dict:
    """The session's launch records, {tool_use_id: record}, without day-old ones. The queue of
    descriptions that came before them (`pending_agents`, taken first in, first out) is dropped."""
    s.pop("pending_agents", None)
    ls = {k: v for k, v in (s.get("launches") or {}).items()
          if isinstance(v, dict) and now - (v.get("ts") or now) < LAUNCH_KEEP}
    if len(ls) > LAUNCH_MAX:
        ls = dict(sorted(ls.items(), key=lambda kv: kv[1].get("ts") or 0)[-LAUNCH_MAX:])
    s["launches"] = ls
    return ls


def _native_launch_label(agent: str, inp: dict) -> str:
    """Codex v2 names a launch with task_name; its message is the private brief."""
    if agent != "codex" or not isinstance(inp.get("task_name"), str) or not inp["task_name"].strip():
        return ""
    from . import codexlaunch
    return codexlaunch.label_of({"task": inp["task_name"], "type": inp.get("agent_type") or "default"})


def _note_agent_launch(agent: str, data: dict, now: float | None = None):
    """PreToolUse of a subagent launch (Claude's Agent/Task, Codex's spawn_agent): a
    launch record keyed by the call's tool_use_id, with its description, type, caller (the launching subagent's
    agent_id, or main) and depth (1 for Morpheus's launches, 2 for a helper a member launches). The agent it starts binds to exactly this record (see bind); nothing takes it by order. A
    launch without a tool_use_id (Codex today) holds a crew slot for LAUNCH_TTL and never binds. Runs under
    note_agent_launch's hold."""
    sid = data.get("session_id") or "-"
    f = session_path(agent, sid)
    s = state.load(f)
    if s is None:
        return
    inp = data.get("tool_input") or {}
    label = inp.get("description") or inp.get("nickname") or inp.get("name") or _native_launch_label(agent, inp) or \
        str(inp.get("message") or inp.get("prompt") or "").strip().split("\n")[0][:60]
    now = now or time.time()
    tid = str(data.get("tool_use_id") or "")
    ls = _launches(s, now)
    parent = str(data.get("agent_id") or "main")
    entry = {"tool_use_id": tid or None, "label": str(label)[:80],
             "type": inp.get("subagent_type") or inp.get("agent_type") or "", "parent": parent,
             "depth": launch_depth(s, None, parent), "ts": round(now, 1), "state": "pending", "agent_id": None}
    if agent == "codex":
        entry["run_started"] = (load_run(agent, sid) or {}).get("started")
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
    written after the SubagentStart hooks return, so it is there from the agent's first tool call on.
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
    from . import crew
    sw, tw = _words(name), _words(text)
    hit = sum(1 for b in sw if any(a == b or _same_stem(a, b) for a in tw))
    key = crew.codex_task_name(name)  # the task_name a Codex spawn passes: "step_1a2b3c4d" for "計画"
    same = text.strip().lower() == name.strip().lower() or bool(key) and crew.codex_task_name(text) == key
    if same and not sw:  # a name without words a launch can name: its whole name, or its task_name, names it
        return (True, 1, 1.0)
    return (same, hit, hit / len(sw) if sw else 0.0)


def _bind_step(run: dict, aid: str, lane: dict, now: float):
    """Give a newly bound lane its step. Its launch's task text is scored against all its member's steps, done ones
    included ("Neo: session graph" → "session graph command"): a launch that names a done or skipped step best, and
    none of the steps waiting for its member, is a follow-up of finished work (`follow_up`), left as it is, and takes
    no step its member was given by role ("Tank: api follow-up" next to "export"); one that names a waiting step too
    ("Seraph: review the release" next to a done "review" and a waiting "review-27") is scored by the waiting steps
    alone. One that names a step its member's ended lane left unverified looks at it again; otherwise the lane claims
    (_claim_lane): the step it names first, else the oldest step waiting for its member. A named step whose
    dependencies are open waits, and Morpheus is told."""
    plan = run.get("plan") or []
    lanes = run.get("agents") or {}
    mine = [st for st in plan if st.get("label") and _label_matches(st["label"], lane) and (
        st.get("state") in DONE or (st.get("state") == "pending" and (st.get("owner") or "main") == "main")
        or st.get("state") == "unverified" and st.get("owner") != aid
        and (lanes.get(st.get("owner")) or {}).get("state") in ENDED_LANE + ("unverified",))]
    from . import crew
    who, label = crew.who(lane.get("persona") or lane.get("label") or "a subagent"), lane.get("label") or ""
    scored = [(_name_score(_task_text(label), st["name"]), st) for st in mine]
    best = max((sc for sc, _ in scored), default=(False, 0, 0.0))
    top = [st for sc, st in scored if best[1] and sc == best]
    st = top[0] if len(top) == 1 else None
    if st is not None and st.get("state") in DONE:
        waiting = [(sc, x) for sc, x in scored if x.get("state") not in DONE and sc[1]]
        if not waiting:  # it names finished work and no step waiting for its member: it binds no step
            lane["follow_up"] = st["name"]
            return
        best = max(sc for sc, _ in waiting)  # a word it shares with a finished step does not hide a waiting one
        top = [x for sc, x in waiting if sc == best]
        st = top[0] if len(top) == 1 else None
    if st is not None and st.get("state") == "unverified":
        if deps_open(run, st) or coord.busy(run, aid, st, review=is_review_step):
            return  # a dependency was reopened since: dev progress reopen gives it back once that is done
        st.pop("recheck", None)  # it is redone now
        st["reopened"] = (st.get("reopened") or 0) + 1
        _take_step(run, aid, st, now)
        _log_change(run, st, "unverified", "running", aid, f"a new launch of {who}, “{label}”, names it: the new "
                    f"lane works on it again", now)
        return
    _claim_lane(run, aid, now)
    if st is not None and st.get("state") == "pending" and deps_open(run, st):
        w = ", ".join(deps_open(run, st))  # launched too early: the step keeps waiting for its dependencies
        _post(run, "devkit", "main", f"{who} started, but “{st['name']}” waits for {w}, not done yet: the step "
              f"stays pending, and this lane takes it once {w} is done if it is free then", now, from_name="devkit")


def _launch_on_its_way(lane: dict, sess, now: float) -> bool:
    """Does a new launch of this lane's member wait to start (a pending launch in the session record, within
    LAUNCH_TTL)? Then that launch, not this resumed lane, takes the steps given to the member by role."""
    me = (lane.get("persona") or "").lower()
    return bool(me) and any(
        r.get("state") == "pending" and not r.get("agent_id") and now - (r.get("ts") or now) < LAUNCH_TTL
        and (persona(r.get("type") or "", r.get("label") or "") or "").lower() == me
        for r in (((sess or {}) if isinstance(sess, dict) else {}).get("launches") or {}).values()
        if isinstance(r, dict))


def _lane_event(run, aid, kind, tid, rec, binding, now, start, waiting, own=True, sess=None):
    """The run's side of a binding: the agent's lane, created provisional at its start when a launch is
    waiting (no step, no scope, until it binds), resumed when the agent works again after it had stopped, and bound
    to its launch: label, parent, depth, persona and the step the launch names (none for a member's helper, which
    works inside its member's step). `own`: the hook is the agent's own start or tool
    call, so a finished lane resumes; never for its launch's result or its stop. A resumed lane takes a crew slot
    like a launch: while none is free it stays as it was, reads only (`waits_slot`), and Morpheus is told once with
    the launch's message; each later call of the agent tries again. `sess`: the session record, held by the caller."""
    lane = run["agents"].get(aid)
    if run.get("agent") == "codex" and rec and (
            rec.get("run_started") != run.get("started") if "run_started" in rec else
            (rec.get("ts") or 0) < run.get("started", 0)):
        return  # a prior run's recorded launch cannot acquire this run's assignments
    if lane and own and lane.get("parent", "main") == "main":
        if "assigned_steps" not in lane:  # a lane recorded before lanes kept their steps
            lane["assigned_steps"] = []
            for step in run.get("plan", []):
                if step.get("owner") == aid:
                    _remember_step(lane, step)
        intent = [s for s in run.get("plan", []) if _exact_intent(run, s, aid)]
        if lane.get("state") != "running" and intent and not any(
                s.get("state") == "pending" and not deps_open(run, s) for s in intent):
            return  # closed work or unfinished dependencies cannot readmit a finished lane
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
            if run.get("agent") == "codex":
                lane.pop("over_limit", None)  # a refused Codex launch has now passed every admission check
            lane.update(state="running", finished=None, resumed=(lane.get("resumed") or 0) + 1, resumed_at=now)
            _open(run, "agent", aid, now)
            _audit("crew.resume", lane.get("persona") or lane.get("label") or aid[:8], lane.get("label") or "",
                   project=run.get("project") or "", agent_id=aid[:16])
            if lane.get("parent", "main") == "main":  # a member, not a helper: it takes what waits for it
                _claim_lane(run, aid, now, member_wide=not _launch_on_its_way(lane, sess, now))
    elif own and lane.get("parent", "main") == "main":
        # an assignment may follow the admission hook: the next own hook of a free lane claims it
        _claim_lane(run, aid, now, member_wide=not _launch_on_its_way(lane, sess, now))
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
    """Late binding, at every hook from inside a subagent (its tool calls, its stop). SubagentStart
    carries neither the launch's tool_use_id nor a meta file yet, so an agent that starts before its launch's
    result binds at its first hook, when the meta file exists. A known agent whose lane had stopped resumes it at
    a tool call (SendMessage); `resume=False` at its stop. The usual case, a bound working lane, costs one read of
    the run."""
    aid, sid = str(data.get("agent_id") or ""), data.get("session_id") or "-"
    if not AGENT_ID.fullmatch(aid):
        return
    run = load_run(agent, sid)
    lane = ((run.get("agents") or {}).get(aid) or {}) if isinstance(run, dict) and run.get("state") != "done" else {}
    if lane.get("state") == "running" and lane.get("binding", "exact") in BOUND and \
            lane.get("parent", "main") != "main":
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
    the launch's tool_use_id, an exact binding without the meta file. A background launch's result comes
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
    """PostToolUseFailure of a subagent launch (an unknown agent type, an interrupt): its launch
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
    """What one launch costs (progress.slot_weights): a scout lookup, a review-only or read-only member (the
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
    """The crew slots in use, [(id, name, cost)]: launches whose agent has not started yet, and working
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
    """The crew's slots now -> {used (the costs summed), limit, max (progress.max_parallel), reserve
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
    """'1 of 3 slots in use (lowered from 6: 5-hour usage at 87 %)'."""
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
    "ts"}], the offset to keep). Claude Code's prompt hook for them carries the running turn's prompt_id, not theirs;
    the transcript is where each is its own: Claude Code records each as an attachment of
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


def _codex_midturn_key(request: dict) -> str:
    return hashlib.sha256((str(request["ts"]) + "\n" + request["text"]).encode()).hexdigest()


def _codex_midturn_requests(transcript, sid: str, cursor: dict, since: float, tail=False) -> tuple:
    """Read one bounded chunk of the root rollout's typed requests, never approval evidence.

    Start at the beginning on first use so an upgraded long-running session can recover its current turn. Keep
    partial lines for the next hook; discard oversized records in bounded chunks. File identity catches rotation
    even when the replacement is larger. The request watermark rejects replayed history after rotation. A separate
    tail cursor sees the newest requests immediately while the forward cursor recovers older current-turn requests.
    """
    import datetime as dt
    cursor = dict(cursor)
    try:
        with open(transcript, "rb") as fh:
            stat = os.fstat(fh.fileno())
            identity = [stat.st_dev, stat.st_ino]
            start = cursor.get("offset", 0)
            if cursor.get("file") != identity or not isinstance(start, int) or not 0 <= start <= stat.st_size:
                start = 0
                cursor["discard"] = False
            cursor["file"] = identity
            if tail and stat.st_size - start > MIDTURN_READ:
                start = stat.st_size - MIDTURN_READ
                fh.seek(start - 1)
                cursor["discard"] = fh.read(1) != b"\n"
            fh.seek(start)
            chunk = fh.read(MIDTURN_READ)
    except OSError:
        return [], cursor
    if cursor.get("discard"):
        cut = chunk.find(b"\n")
        if cut < 0:
            cursor["offset"] = start + len(chunk)
            return [], cursor
        start += cut + 1
        chunk = chunk[cut + 1:]
        cursor["discard"] = False
    end = chunk.rfind(b"\n")
    if end < 0:
        # A full chunk without an end is an oversized record; no unbounded scan to find its end.
        cursor["discard"] = len(chunk) == MIDTURN_READ
        cursor["offset"] = start + len(chunk) if cursor["discard"] else start
        return [], cursor
    out = []
    watermark = max(float(since), float(cursor.get("ts") or 0))
    seen = list(cursor.get("seen") or [])
    for raw in chunk[:end + 1].splitlines():
        if b'"UserMessage"' not in raw:
            continue
        try:
            rec = json.loads(raw)
            text = _codex_prompt(rec, sid)
            ts = dt.datetime.fromisoformat(str(rec.get("timestamp")).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
            continue
        if not text or not text.strip() or text.lstrip().startswith(_WAKE_PROMPTS) or ts < watermark:
            continue
        key = _codex_midturn_key({"ts": ts, "text": text})
        if ts > watermark:
            watermark, seen = ts, []
        if key in seen:
            continue
        seen = (seen + [key])[-128:]
        out.append({"text": text, "ts": ts})
    cursor.update(offset=start + end + 1, ts=watermark, seen=seen)
    return out, cursor


def _note_codex_midturn(data: dict) -> list:
    """Recover steering only from an existing main session's own validated interactive rollout."""
    sid = str(data.get("session_id") or "")
    if data.get("agent_id") or not _SESSION_ID.fullmatch(sid):
        return []
    f = session_path("codex", sid)
    sess = state.read(f) or {}
    if sess.get("agent") != "codex" or sess.get("session") != sid or not sess.get("turn_started"):
        return []
    transcript = harness_transcript("codex", sid)
    if not transcript or (data.get("transcript_path") and
                          os.path.realpath(data["transcript_path"]) != os.path.realpath(transcript)):
        return []
    meta = _codex_meta(transcript, sid) or {}
    if not meta or meta.get("originator") == "codex_exec" or meta.get("source") == "exec":
        return []
    found = []

    def read(s):
        since = float(s["turn_started"])
        backlog, cursor = _codex_midturn_requests(transcript, sid, s.get("codex_midturn") or {}, since)
        latest, tail = _codex_midturn_requests(transcript, sid, s.get("codex_midturn_tail") or {}, since, tail=True)
        pending = {k: ts for k, ts in (s.get("codex_midturn_pending") or {}).items() if ts >= since}
        new = []
        for request in backlog:
            key = _codex_midturn_key(request)
            if key not in pending:
                new.append(request)
            pending.pop(key, None)
        for request in latest:
            key = _codex_midturn_key(request)
            # The forward cursor already delivered these. The tail's watermark is deliberately independent:
            # seeing a recent request must never suppress an older unrecorded constraint during backfill.
            if request["ts"] < cursor.get("ts", since) or key in cursor.get("seen", []) or key in pending:
                continue
            pending[key] = request["ts"]
            new.append(request)
        pending = {k: ts for k, ts in pending.items() if ts >= cursor.get("ts", since)}
        if cursor == s.get("codex_midturn") and tail == s.get("codex_midturn_tail") and not new:
            return state.SKIP
        s["codex_midturn"] = cursor
        s["codex_midturn_tail"] = tail
        s["codex_midturn_pending"] = pending
        if new:
            s["request_ts"] = max([float(s.get("request_ts") or 0)] + [r["ts"] for r in new])
            found.extend(new)
    try:
        state.update(f, read, create=False)
    except state.StateBusy:
        return []
    if found:
        from . import continuity
        for r in found:
            continuity.on_midturn("codex", sid, r["text"], r["ts"])
    return found


def note_midturn(agent: str, data: dict) -> list:
    """The main session's hooks (PreToolUse of a launch, PostToolUse): the messages the developer typed mid-turn
    since the last hook (midturn_requests, from the offset the session record keeps, read and moved under its lock so
    each is counted once) become the developer's latest request: `request_ts`, which is_latest_request reads, and a
    prompt of the turn in continuity. A message read here is never a grant or an approval: only its prompt hook's
    delivery is (on_human_prompt). -> the messages found"""
    if agent == "codex":
        return _note_codex_midturn(data)
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


# Nested agents. A subagent can launch agents of its own; such a helper gets its parent's
# tool level and scope, never more, and counts against the parallel limit. Permissions are computed from the lanes
# and steps each time, never stored on the helper. The hooks' advice (_crew_limits_advice, _scope_advice) and the
# crew gates' write attribution call these.

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
    record of (until Morpheus assigns it a step), or that has not met its launch yet (a missing binding
    grants nothing); read too for a resumed lane that waits for a crew slot (`waits_slot`); else its crew member's
    `tools`, else all (an ad-hoc agent)."""
    if lane.get("state") not in (None, "running") or \
            lane.get("binding") in ("conflict", "unbound", "pending") or \
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
    """The scope a lane edits in, as layers [[globs], …]: one per lane in its parent chain that has a scope, the
    globs of the steps it runs (coord.scope), never a stored copy. A path must match every layer, so a helper stays
    inside its member's files. A crew member's lane in a run with a plan that runs no step holds no file: its layer
    is empty ([]), so a blocked, closed or reassigned step's files are no longer its own. [] when nothing is scoped:
    a lane that runs steps without a scope, a helper, or an agent outside the crew."""
    plan = (run or {}).get("plan") or []
    layers = []
    for k in lane_chain(run, aid):
        lane = run["agents"][k]
        globs = coord.scope(run, k)
        if not globs and not plan and lane.get("scope"):
            globs = sorted(lane["scope"])  # a run without a plan, recorded before scopes came from steps
        if globs:
            layers.append(globs)
        elif plan and lane.get("persona") and (lane.get("parent") or "main") == "main" and not coord.held(run, k):
            layers.append([])
    return layers


def in_scope(rel_path: str, layers: list) -> bool:
    """A repo-relative path is inside every layer of a lane_scope (the same glob rules as scope_holders); an empty
    layer holds nothing."""
    return all(_in_globs(rel_path, layer) for layer in layers)


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
    """The share of max_parallel the crew may use at this usage: the factor of the highest
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


QUOTA_MARGIN = 2.0  # percentage points usage must fall below a step of progress.quota_steps before slots come back


def parallel_limit(agent: str, now: float | None = None) -> tuple:
    """How many crew members may work at once -> (limit, why it is lower than progress.max_parallel, or ""): the
    configured max_parallel, under the team's ceiling, scaled by the plan's recorded usage (progress.quota_steps,
    5-hour and weekly windows; an expired reading counts for nothing), never below 1. The limit drops as soon as
    usage reaches a step, and comes back up only once usage is QUOTA_MARGIN points below it, so usage hovering at a
    step does not flip it at every reading. Each change is audited."""
    c = cfg()
    top, why = int(c["max_parallel"]), ""
    team = team_ceiling()
    if team is not None and team < top:
        top, why = team, f"the team's ceiling is {team} ({core.tilde(TEAM_POLICY)})"
    readings = [(label, used) for label, used in (("5-hour", quota_used(agent, now)), ("weekly", weekly_used(agent, now)))
                if used is not None]

    def at(margin: float) -> tuple:
        limit, said = top, why
        for label, used in readings:
            n = max(1, int(top * quota_share(used + margin, c) + 1e-9))
            if n < limit:
                limit, said = n, f"{label} usage at {used:.0f} %"
        return limit, said
    limit, said = at(0.0)
    last = (core.read_json(QUOTA / f"{agent}.limit.json", {}) or {}).get("limit")
    if isinstance(last, int) and not isinstance(last, bool) and last < limit:  # going up: past the margin only
        up, up_said = at(QUOTA_MARGIN)
        if up < limit:  # usage is still within the margin of the step it fell below
            limit, said = max(last, up), up_said
    _note_limit(agent, limit, said, int(c["max_parallel"]), now)
    return limit, said


def _note_limit(agent: str, limit: int, why: str, configured: int, now: float | None):
    """Audit each change of the effective limit once: the last one is kept beside the quota reading."""
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
            watch.notice_all("notice", f"limit:{agent}", f"crew slots {old} → {limit}: "
                             f"{why or 'progress.max_parallel'}", agent=agent, replace=True)  # the newest only
        except Exception:
            pass


def launch_verdict(agent: str, data: dict, run: dict, now: float | None = None):
    """Why a subagent launch (Agent, Task, spawn_agent) must not run -> the reason, or None. It applies at every
    depth: a helper a member launches takes a slot like the member did, and no launch goes deeper than
    progress.max_depth."""
    now = now or time.time()
    sess = core.read_json(session_path(agent, data.get("session_id") or "-"), {}) or {}
    sess = sess if isinstance(sess, dict) else {}
    return _launch_verdict(agent, data, run, sess, now)


def _launch_verdict(agent: str, data: dict, run: dict, sess: dict, now: float):
    """The launch checks with a session already read by the caller (also used for refused-lane readmission)."""
    parent = str(data.get("agent_id") or "main")
    depth, most = launch_depth(sess, run, parent), int(cfg()["max_depth"])
    if depth > most:
        return (f"This launch would run {depth} levels below the tech lead; the limit is {most} (progress.max_depth). "
                f"Do this part yourself, or name it in your report and the tech lead assigns it.")
    inp = data.get("tool_input") or {}
    kind = str(inp.get("subagent_type") or inp.get("agent_type") or "")
    label = str(inp.get("description") or inp.get("nickname") or inp.get("name") or _native_launch_label(agent, inp))
    cost = launch_cost(run, kind, label, parent)
    if cost <= 0:
        return None  # a scout lookup costs no slot (progress.slot_weights)
    b = slot_budget(agent, run, sess, now)
    why = _slot_refusal(run, sess, b, cost, parent, kind, label)
    if why is not None:
        return why or None  # "": it took the slot kept for the developer's own request
    if parent != "main" or (sess.get("turn_started") and _for_the_developer(run, sess, kind, label)):
        return None  # the developer's request of this turn is not held back by the plan's order
    return _order_verdict(run, sess, kind, label, b, cost, now)


def _slot_refusal(run, sess: dict, b: dict, cost: float, parent: str, kind: str, label: str):
    """The slot rules a launch and a resumed lane share: the reason no slot is free; "" when it takes
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
        return (f"{head}. Wait for one to finish (you are notified), or give the work to one of them: dev progress "
                f"assign \"<step>\" --to <member> queues it for their lane, then send it to them (SendMessage / "
                f"send_input). progress.max_parallel in devkit config changes the limit.")
    if used + cost > limit - reserve + 1e-9:  # it would take the slot kept for the developer
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
    if run.get("agent") == "codex" and lane.get("over_limit"):
        # A previously refused launch was never admitted. A free slot alone must not clear a depth or ordering
        # refusal; evaluate the original launch's parent, role and step against the current plan and budget.
        data = {"agent_id": lane.get("parent") or "main",
                "tool_input": {"agent_type": lane.get("type") or "",
                               "description": lane.get("label") or ""}}
        return _launch_verdict(run.get("agent") or "claude", data, run, sess, now)
    try:
        from . import crew
        m = crew._for_lane(lane)
    except Exception:
        m = None
    # Admission charges for the member's restored tools, not the temporary read
    # restriction that its new assignment will clear in _take_step.
    level = max((_lane_level(dict(run["agents"][k], state="running")) for k in lane_chain(run, aid)),
                key=TOOL_LEVELS.index, default="all")  # its level once it works, whatever it waits for now
    cost = _cost(m, level)
    if cost <= 0:
        return None
    b = slot_budget(run.get("agent") or "claude", run, sess, now)
    return _slot_refusal(run, sess, b, cost, lane.get("parent") or "main", lane.get("type") or "",
                         lane.get("label") or "") or None


def _for_the_developer(run, sess, kind: str, label: str) -> bool:
    """A launch for what the developer asked in the current turn: it names no open plan step (ad-hoc work
    for their request), or one added in this turn."""
    st = named_step(run, kind, label)
    return st is None or is_latest_request(run, sess, st)


def _order_verdict(run, sess, kind: str, label: str, b: dict, cost: float, now: float):
    """Morpheus launches the ready steps in their recorded order (dispatch.order): a launch for a lower
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


def on_subagent(agent: str, data: dict, started: bool, now: float | None = None, findings: str = ""):
    """SubagentStart / SubagentStop; see _on_subagent. Serialized per session like note_agent_launch: one hold covers
    the session record and the run (the run's save inside re-enters it). The stops that found the lock busy earlier
    go first. A stop that finds it busy now waits in a pending file (_defer_stop) instead of being dropped: the
    member's report is its step's evidence. `findings`: what the crew gates still found in a stop they let through
    (hooks._findings_told), which the lane's end tells the tech lead."""
    sid = data.get("session_id") or "-"
    entered = False
    try:
        with state.hold(session_path(agent, sid)):
            entered = True
            _apply_stops(agent, sid)
            return _on_subagent(agent, data, started, now, findings)
    except state.StateBusy:
        if entered or started:
            raise  # a record that does not parse, or a start: a start binds again at the agent's first tool call
        _defer_stop(agent, sid, data, now or time.time(), findings)


def _stops_dir(agent: str, sid: str) -> Path:
    """The pending SubagentStops of one session, one file per agent: <state>/stops/<agent>-<sid>/<agent id>.json."""
    return SESSIONS.parent / "stops" / f"{_safe_name(agent)}-{_safe_name(sid)}"


def _defer_stop(agent: str, sid: str, data: dict, now: float, findings: str = ""):
    """Keep a SubagentStop the session's lock could not take, with when it happened, for the next hook (apply_stops).
    Without an agent id there is nothing to apply it to: it is dropped, as before, and audited."""
    aid = str(data.get("agent_id") or data.get("subagent_id") or "")
    if not AGENT_ID.fullmatch(aid):
        _audit("crew.stop-dropped", aid or "?", "the session's lock was busy and the stop names no agent", "dropped")
        return
    _write_private(_stops_dir(agent, sid) / f"{aid}.json", {**data, "stopped_at": now,
                                                              **({"gate_findings": findings} if findings else {})})
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
                _on_subagent(agent, data, False, data.get("stopped_at"), str(data.get("gate_findings") or ""))
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


def _on_subagent(agent: str, data: dict, started: bool, now: float | None = None, findings: str = ""):
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
        # the crew gates' pass token for this agent (gates.check writes it only on a clean pass, in the session record
        # the hook writes before it reaches this stop); it is spent on the stop it counts for
        from . import gates
        tokens = s.get(gates.PASSED)
        token = tokens.get(aid) if isinstance(tokens, dict) else None
        spent = []

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
            passed = _passed_now(token, lane or {}, now)
            spent.append(passed)
            _end_lane_steps(run, lane or {}, aid, report, now, agent, rsid, passed=passed)
            _lane_verdicts(run, lane or {}, aid, passed, now)
            # what the gates still found goes in the session's own run, once
            # a run that does not list the lane names the member from the stop's own agent type
            _tell_uncounted(run, lane or {"persona": kind}, now, findings if rsid == sid else "")
            run["step"] = sum(1 for x in run.get("plan") or [] if x.get("state") in ("done", "skipped"))
            _advance_main(run, now)
        _run_event(agent, sid, finish)
        for other in runs_listing(agent, aid, sid):  # every run that lists it frees its lane, whichever id it came under
            _run_event(agent, other, lambda run, o=other: finish(run, o))
        if any(spent):
            tokens.pop(aid, None)
            if not tokens:
                s.pop(gates.PASSED, None)
    state.replace(f, s)


def _passed_now(token, lane: dict, now: float) -> bool:
    """The crew gates passed the stop that ends this lane at `now` cleanly: their token (gates.PASSED, the time that
    stop passed) was written during the lane's latest run, at or after its start or last resume, and no later than
    this stop. A token of an earlier stop, or of a later one when this stop is applied late, does not count, nor does
    any token for a lane with no start."""
    if isinstance(token, bool) or not isinstance(token, (int, float)):
        return False
    since = lane.get("resumed_at") or lane.get("started")
    return isinstance(since, (int, float)) and since <= token <= now


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


def _shell_writers(run: dict, calls: list, chain: set) -> set:
    """Of the callers of these shell calls (ledger entries or closed records), those whose shell may write files:
    main (the tech lead), and each lane outside the reading lane's chain that works with build or all tools, a
    helper's call counting for the lanes above it. A call that reads (call_reads) counts for nobody."""
    callers = {str(e.get("caller") or "") for e in calls if not e.get("reads") and
               not (run.get("agent") == "codex" and "edit_paths" in e)}
    out = {"main"} if "main" in callers else set()
    for c in callers - {"main", ""}:
        out.update(k for k in lane_chain(run, c) if k not in chain and lane_tools(run, k) in ("build", "all"))
    return out


def _certain_writes(writes: dict) -> list:
    """The files of a lane's scope_writes that nobody else could have written (a record from before the certain list
    names its paths when its certainty is certain)."""
    if "certain" in writes:
        return list(writes.get("certain") or [])
    return list(writes.get("paths") or []) if writes.get("certainty") == "certain" else []


def _or_names(run: dict, writers: list) -> str:
    """'Backend engineer (Tank) or Tech lead (Morpheus)': up to three writers, by role and name."""
    from . import crew
    names = [crew.who(_name_of(run, k)) for k in writers[:3]] + ([f"{len(writers) - 3} more"] if len(writers) > 3 else [])
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def scope_written(agent: str, sid: str, aid: str, paths: list, now: float | None = None,
                  call: dict | None = None) -> dict | None:
    """A review or read-only lane's shell call wrote these files (write detection). A file is credited to whoever's
    own edit call (Write, Edit, MultiEdit, Codex apply_patch) touched that path during the call (note_edit: a build
    lane, its helper or the tech lead, never the reading lane itself): devkit audits it as attributed and flags
    nothing, since the edit record is exact. An edit tool's temporary file (<file>.tmp.<pid>.<hex>) counts as its
    file. Every other file is held against the reading lane in `scope_writes`. It is "possible" when another writer
    was at work on it: an edit call of that file still in flight (edits_open: the tool writes before its result is
    recorded), or a shell call that overlapped the reading lane's call, still open or already closed (open_calls,
    shells_since), whose caller, for a file in a working build lane's scope, must be that lane or the tech lead.
    Codex also keeps accepted patch calls pending or closed, as possible writers only of their declared paths. A call
    that writes no file (call_reads: `git status`, `dev watch --wait`) makes nobody a writer. With no such writer it
    is "certain": a script of the reading lane can write into a working member's scope, so a scope alone is no alibi.
    Only a certain file makes the lane's report unverified (_end_member_step). An open step whose scope holds a held
    file keeps it (`other_writes`), which the review step after it lists; Morpheus gets one short crew notice naming
    the files and the possible writers; and the audit trail a scope.write record per certainty. -> the lane's
    scope_writes, or None without a lane or when it has none."""
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
        current = call_entry(call) if agent == "codex" and call else {}
        start = current.get("ts") or min((e.get("ts") or now for e in calls if e.get("caller") == aid),
                                        default=now - CALL_TTL)
        overlap = calls + shells_since(sid, start, now)
        shells = _shell_writers(run, overlap, chain)  # who else had a writing shell call
        edits = [e for e in edits_since(sid, start, now) if str(e.get("caller") or "") not in chain]
        inflight = [e for e in edits_open(sid, now) if str(e.get("caller") or "") not in chain]
        base = current.get("wt_base")

        def matches(path, rel):
            return core.real(path) == core.real(os.path.join(base, rel)) if base else _edited(path, rel)

        owned, held, maybe_by = {}, {}, {}  # credited by edit; held against the reading lane; possible files by writers
        for p in paths:
            names = {p, _EDIT_TEMP.sub("", p)}  # an edit tool writes <file>.tmp.<pid>.<hex>, then renames it
            by = next((str(e.get("caller")) for e in edits for n in names if matches(e.get("path"), n)), None)
            if by:
                owned.setdefault(by, []).append(p)
                continue
            holders = {k for k in builders for n in names if layers[k] and in_scope(n, layers[k])}
            editing = {str(e.get("caller")) for e in inflight for n in names if matches(e.get("path"), n)}
            pending = _shell_writers(run, [{k: v for k, v in e.items() if k != "edit_paths"} for e in overlap
                                          if agent == "codex" and base and
                                          any(matches(path, p) for path in e.get("edit_paths") or [])], chain)
            candidates = shells | pending
            by_shell = ((candidates & holders) if holders else candidates - {"main"}) | (candidates & {"main"})
            writers = sorted((by_shell | editing) - {"main"}) + sorted((by_shell | editing) & {"main"})
            held.setdefault("possible" if writers else "certain", []).append(p)
            if writers:
                how = "the shell call of" if set(writers) <= by_shell else "written by"
                maybe_by.setdefault((tuple(writers), how), []).append(p)
        who = lane.get("persona") or lane.get("label") or aid
        name = crew.who(who)
        loose = sorted(p for files in held.values() for p in files)
        if loose:
            had = lane.get("scope_writes") or {}
            sure = sorted(set(_certain_writes(had)) | set(held.get("certain") or []))
            lane["scope_writes"] = {"ts": round(now), "paths": sorted(set(had.get("paths") or []) | set(loose))[:20],
                                    "certainty": "certain" if sure else "possible", "certain": sure[:20]}
            marked = []
            for c, files in held.items():
                marked += [n for n in _mark_other_writes(run, files, chain, aid, name, c, now) if n not in marked]
            level = "review-only" if lane_tools(run, aid) == "review" else "read-only"
            maybe = [f"{_shown(files)} (possibly {how} {_or_names(run, list(ks))})"
                     for (ks, how), files in maybe_by.items()]
            if held.get("certain"):
                text = (f"devkit: {name} works {level}, and its shell call wrote {_shown(held['certain'])} while no "
                        "other writer was at work. Its report counts as unverified: check the change (git diff), keep "
                        "or revert it, then close its step with evidence." +
                        (f" Also changed during it: {'; '.join(maybe)}." if maybe else ""))
            else:
                text = (f"devkit: during a shell call of {name}, which works {level}, {' and '.join(maybe)} changed; "
                        "this does not unverify its report.")
            listed = f" The review after {', '.join(marked)} lists these files." if marked else ""
            _post(run, aid, "main", text + listed, now, from_name="devkit", kind="notice")
            _save(run)
        writes = dict(lane.get("scope_writes") or {}) or None
    for k, mine in owned.items():
        _audit("scope.write", _name_of(run, k), f"attributed: {', '.join(mine[:20])}"[:600], "attributed",
               lane=k[:16], paths=mine[:20], count=len(mine), project=run.get("project") or "",
               why=f"edited by {_name_of(run, k)} with its own edit tool during {who}'s shell call")
    for c, files in held.items():
        extra = {"severity": "warning"} if c == "certain" else \
            {"writers": sorted({_name_of(run, k) for ks, _ in maybe_by for k in ks})}
        _audit("scope.write", who, f"{c}: {', '.join(files[:20])}"[:600], "detected", lane=aid[:16],
               paths=files[:20], certainty=c, count=len(files), project=run.get("project") or "", **extra)
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
    """Of the open steps a lane holds at its end (`mine`), those its evidence is about: the step its
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


def _end_lane_steps(run: dict, lane: dict, aid: str, report: str, now: float, agent: str, sid: str,
                    passed: bool = False):
    """A lane's end at its SubagentStop: the steps its evidence is about end as its report or its last Check says
    (_end_member_step); any other step it holds goes back to pending, still its member's, with the reason in its
    history, so a lane's ✓ never closes work it did not do. `passed`: the crew gates passed this
    stop cleanly (their token, _passed_now); without it, its review steps approve nothing (_end_member_step)."""
    mine = [st for st in run.get("plan") or [] if st.get("owner") == aid and st.get("state") not in DONE]
    about = _steps_about(lane, aid, report, mine)
    who = lane.get("persona") or "the subagent"
    from . import approvals
    for st in mine:
        if st.get("state") == "blocked" and (not any(st is x for x in about)
                                             or approvals.ending(lane, report)[0] != "done"):
            continue  # it waits for its decision: the answer gives it to its member's next lane (coord.unblock)
        if any(st is x for x in about):
            _end_member_step(run, st, lane, aid, report, now, agent, sid, passed=passed)
            continue
        old = st.get("state")
        st.update(owner="main", state="pending", started=None)
        st.pop("joined", None)
        coord.set_intent(run, st, coord.member_of(st.get("label")) or who, None, now)
        _log_change(run, st, old, "pending", "devkit", f"{who} ended, and neither its launch, its report nor its dev "
                    f"progress step names this step: it waits for its member's next lane", now)


def _end_member_step(run: dict, st: dict, lane: dict, aid: str, report: str, now: float, agent: str, sid: str,
                     closing: bool = False, passed: bool = False):
    """A member's step at its SubagentStop: done with the evidence of its report's Check or its last
    `dev progress step --check` (approvals.ending), and a review step's Check is its verdict on the work it follows
    (approvals.from_review), whose ✓ also ends done the unverified work it approves (_verified_by_review);
    otherwise unverified, for Morpheus to close with evidence or reopen. A review or read-only lane whose shell wrote
    files nobody else could have written (scope_writes, certain) ends unverified whatever it reports; a possible
    write leaves its ending to its report. A review step ends done only when the crew gates passed this stop cleanly
    (`passed`, their token): sent back and then let through unjudged, findings only recorded, a gate that could not
    judge, a stop that never reached them or whose token the session record did not take end it unverified, since
    its Check is no approval. Other steps end as their evidence says. `closing`: the member closes it itself while
    it works on (_member_close), which never happens to a review step."""
    from . import approvals
    old = st.get("state")
    state_, why, check = approvals.ending(lane, report)
    sure = _certain_writes(lane.get("scope_writes") or {})
    if state_ == "done" and sure:
        state_, why = "unverified", f"its shell call wrote {', '.join(sure[:3])} while no other writer was at work"
    if state_ == "done" and not passed and approvals.is_review(st):
        run.setdefault("_uncounted", []).append(f"the review “{st['name']}”")
        state_, why = "unverified", ("the crew gates did not pass its report (sent back, only recorded, or not "
                                     "judged), so its Check approves nothing: launch the review again for a verdict "
                                     "the gates pass")
    who = lane.get("persona") or "subagent"
    if state_ != "done":
        if approvals.is_review(st) and _review_verdict(lane, st, report) == "changes":
            st["verdict"] = "changes"  # finished, changes requested: the fix after it may start (coord.deps_open)
        st["state"] = "unverified"
        _log_change(run, st, old, "unverified", aid, f"{who} {'closed it' if closing else 'stopped'}; unverified: "
                    f"{why}", now)
        return
    _finish_step(run, st, now, agent, sid)
    _log_change(run, st, old, "done", aid, f"{who} {'closed it' if closing else 'finished'}; {why}", now)
    try:
        _verified_by_review(run, st, check, now, agent, sid, aid)
        approvals.from_review(run, st, check, now, doer_of=lambda s_: member_of_step(run, s_))
    except approvals.ApprovalError as e:  # a review that may approve nothing: its step still ends
        _audit("crew.approval-refused", st.get("name") or "", str(e)[:300], "refused", project=run.get("project") or "")


def _review_verdict(lane: dict, st: dict, report: str) -> str:
    """The verdict of a review step's member (approvals.verdict_of): from its last `dev progress step --step` record
    of that step, else its report's Check. '' when it gave none."""
    from . import approvals, canary
    ups = [u for u in lane.get("updates") or [] if u.get("kind") == "step" and u.get("step") == st.get("name")]
    check = (ups[-1].get("check") if ups else "") or (canary.parse_status(report or "") or {}).get("check") or ""
    try:
        return approvals.verdict_of(check)
    except approvals.ApprovalError:
        return ""


def _member_close(run: dict, st: dict, lane: dict, aid: str, u: dict, now: float, agent: str, sid: str) -> dict:
    """A member's `dev progress step "…" --step <name> --check "…"`: when its lane holds that step,
    the step ends now, by the rules of the member's end (_end_member_step on this update's Check: done with a ✓, else
    unverified), so the steps after it need not wait for the member's report while the lane works on. A review step
    (approvals.is_review) stays running: its Check is the member's approval of the work it follows, which counts only
    once the member's report passes the crew gates, so it ends at the lane's end (_end_lane_steps, which SubagentStop
    reaches after the gates judged the report; this update's step name binds it there). A step the lane
    does not hold, or one already done, keeps the evidence in the lane only. -> {step, state, closed, why}, which the
    command's line reports."""
    from . import approvals
    name, state_ = st["name"], st.get("state")
    if st.get("owner") != aid:
        holder = "the tech lead's" if (st.get("owner") or "main") == "main" else f"{_name_of(run, st['owner'])}'s"
        return {"step": name, "state": state_, "closed": False,
                "why": f"“{name}” is not your step ({holder}), so it stays {state_}; your evidence is in your lane"}
    if state_ in DONE:
        return {"step": name, "state": state_, "closed": False, "why": f"“{name}” is {state_} already"}
    if not str(u.get("check") or "").strip():
        return {"step": name, "state": state_, "closed": False,
                "why": f"“{name}” stays {state_}: a step closes on its evidence, --check \"✓ <tests run, output>\""}
    if approvals.is_review(st):
        return {"step": name, "state": state_, "closed": False,
                "why": f"“{name}” is a review, so it stays {state_}: its Check is your verdict, which counts once your "
                       f"report passes the crew gates, and the step closes with your report"}
    _end_member_step(run, st, dict(lane, updates=[u]), aid, "", now, agent, sid, closing=True)
    run["step"] = sum(1 for x in run.get("plan") or [] if x.get("state") in DONE)
    _advance_main(run, now)
    return {"step": name, "state": st.get("state"), "closed": True, "why": ""}


def crew_working(run: dict, now: float | None = None) -> list:
    """The crew members working right now in the caller's own run (lanes whose member still works, as `dev crew
    owner` reads them: working_agents, so one that ended or went stale is not), with a persona; [] when none."""
    return [lane.get("persona") for _, lane in working_agents(run or {}, now, own=True) if lane.get("persona")]


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

