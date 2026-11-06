# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Hook entry points for Claude Code and Codex (`dev hook <event> --agent claude|codex`).

  session-start  inject project context (handoff note, git state, commands) and record the session start
  stop           debounced background refresh of the map + session log (long-lived Desktop/app sessions)
  session-end    final background sync for the session
  guard          pre-tool-use workspace rules (see guard.py)

Hooks must be fast and must never break a session: every handler catches all
errors, logs them to ~/.local/state/devkit/logs/hooks.log and exits 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import core
from .core import DEV, HANDOFF, STATE, tilde

STOP_DEBOUNCE_S = 600


def _log(msg: str):
    try:
        d = STATE / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "hooks.log").open("a") as f:
            f.write(f"{core.fmt_ts(time.time())} {msg}\n")
    except OSError:
        pass


def _stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        return json.loads(data) if data.strip() else {}
    except (ValueError, OSError):
        return {}


def _session_file(agent: str, sid: str) -> Path:
    return STATE / "sessions" / f"{agent}-{sid}.json"


def _spawn(args, logname="sync.log"):
    """Detached background `dev …` so the hook returns immediately."""
    log = open(STATE / "logs" / logname, "a")
    subprocess.Popen([sys.executable, str(core.DEVKIT_HOME / "bin" / "dev"), *args],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)


def _spawn_sync(args):
    _spawn(["sync", "--quiet", *args])


# --------------------------------------------------------------------------- session start

def _clone_node_modules(wt: Path, main: Path):
    """Fresh worktree + identical lockfile → copy-on-write clone of main's node_modules (APFS: ~0 bytes)."""
    if (wt / "node_modules").exists() or not (main / "node_modules").is_dir():
        return None
    for lock in ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock"):
        a, b = wt / lock, main / lock
        if a.exists() and b.exists():
            if a.read_bytes() != b.read_bytes():
                return None
            rc, _, _ = core.sh(["cp", "-cR", str(main / "node_modules"), str(wt / "node_modules")], timeout=90)
            return "node_modules cloned copy-on-write from the main checkout (same lockfile; no reinstall needed)" \
                if rc == 0 else None
    return None


def context_for(cwd: str, agent: str) -> str:
    from . import registry
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    lines = ["## devkit · workspace context", ""]
    if top and core.is_temp(top):
        lines.append(f"Scratch repository in a temp dir ({tilde(top)}): not tracked in the handoff repo. "
                     f"Real projects live under {tilde(DEV)}/<owner>/<repo>.")
        return "\n".join(lines)
    if not top:
        reg = core.read_json(HANDOFF / "registry.json", {}) or {}
        ps = sorted(reg.get("projects", []), key=lambda p: -(p.get("last_activity") or 0))
        lines.append(f"You are in {tilde(cwd)}, not inside a project.")
        if ps:
            lines.append("Recently active: " + ", ".join(f"{p['id']} ({core.ago(p.get('last_activity'))})" for p in ps[:6]))
        lines.append(f"Projects map: {tilde(HANDOFF / 'PROJECTS.md')}. Work inside a project folder; see the "
                     f"workspace rules in your global instructions.")
        return "\n".join(lines)
    main = registry.main_of(Path(top)) or Path(top)
    p = registry.inspect_repo(main, with_prs=True, refresh_prs=False, pr_max_age=86400)
    p["main"]["active"] = True
    if registry.update_note(p):
        pass
    wt = next((w for w in p["worktrees"] if core.real(w["path"]) == core.real(top)), None)
    co = wt or p["main"]
    where = (f"worktree `{co.get('branch')}` at {tilde(top)} (main checkout: {tilde(p['path'])})" if wt
             else f"main checkout {tilde(top)} on `{co.get('branch')}`")
    pr = co.get("pr")
    lines.append(f"Project: **{p['id']}**" + (f" ({', '.join(p['stack'])})" if p["stack"] else ""))
    lines.append(f"Checkout: {where} · {registry._state(co)}" + (f" · PR #{pr['number']} {pr['state'].lower()}" if pr else ""))
    if p.get("commands"):
        lines.append("Commands: " + " · ".join(f"`{c}`" for c in p["commands"]))
    others = [w for w in p["worktrees"] if not wt or w["path"] != wt["path"]]
    if others:
        live = sum(1 for w in others if w.get("active"))
        lines.append(f"Other worktrees of this repo: {len(others)}" + (f" ({live} with live sessions)" if live else "")
                     + " — `dev wt ls`. Do not edit them from this session.")
    if not p["canonical_ok"]:
        lines.append(f"Layout: this repo should live at {tilde(p['canonical'])}; the user will move it with "
                     f"`dev adopt`. Keep working here; do not move it yourself.")
    if wt:
        cloned = _clone_node_modules(Path(top), main)
        if cloned:
            lines.append(cloned)
    st = registry.status_line(p["id"])
    if st:
        lines.append(f"Last agent status ({st.get('agent')}, {core.ago(st.get('ts'))}): {st.get('line')}")
    from . import notes as notesmod
    note = core.note_path(p["id"])
    if note.exists():
        ns = notesmod.summarize(note.read_text())
        meta = f"status {ns['status']} · priority {ns['priority']}" + (f" · milestone {ns['milestone']}" if ns["milestone"] else "")
        lines.append(f"Project: {meta} · tasks: {len(ns['tasks']['now'])} now, {ns['tasks']['open']} open "
                     f"(`dev task` lists them; `dev task add|start|done` edits them)")
    lines.append(f"Handoff note: {tilde(note)} — read it before starting; update it with the devkit `handoff` skill "
                 f"before you finish or when the user says handoff/pause/wrap up.")
    body = registry.human_sections(p["id"])
    if body:
        lines += ["", "--- handoff note ---", body]
    return "\n".join(lines)


def _note_hash(top: str) -> str:
    """Hash of the human sections of the project's note (to tell whether the session updated it)."""
    import hashlib
    from .registry import main_of
    main = main_of(Path(top)) or Path(top)
    pid = core.project_identity(main, core.gitout(main, "config", "--get", "remote.origin.url"))[0]
    f = core.note_path(pid)
    if not f.exists():
        return ""
    from . import notes as notesmod
    sec = notesmod.sections(f.read_text())
    sec.pop("Session log", None)
    return hashlib.sha1(json.dumps(sec, sort_keys=True).encode()).hexdigest()


def session_start(agent: str):
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    sid = data.get("session_id") or f"pid{os.getppid()}"
    f = _session_file(agent, sid)
    if not f.exists():
        top = core.gitout(cwd, "rev-parse", "--show-toplevel")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"agent": agent, "session": sid, "cwd": cwd, "top": top, "started": time.time(),
                                 "head": core.gitout(top, "rev-parse", "HEAD") if top else "",
                                 "branch": core.gitout(top, "branch", "--show-current") if top else "",
                                 "note_hash": _note_hash(top) if top and not core.is_temp(top) else ""}))
    _spawn(["config", "repair", "--quiet"])  # restore any devkit-managed setting changed outside devkit
    text = context_for(cwd, agent)
    if agent == "codex":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    else:
        print(text)


# --------------------------------------------------------------------------- session end / stop

def record_session(spec: str):
    """`agent:session_id` → add/replace the session line in the project's handoff note."""
    from . import registry
    agent, _, sid = spec.partition(":")
    f = _session_file(agent, sid)
    s = core.read_json(f, None)
    if not s or not s.get("top"):
        return
    top = s["top"]
    if not os.path.isdir(top):
        return
    main = registry.main_of(Path(top)) or Path(top)
    remote = core.gitout(main, "config", "--get", "remote.origin.url")
    pid, _, _ = core.project_identity(main, remote)
    head = s.get("head")
    subjects = core.gitout(top, "log", "--format=%s", f"{head}..HEAD").splitlines() if head else []
    dirty = len([ln for ln in core.gitout(top, "status", "--porcelain").splitlines() if ln.strip()])
    if not subjects and not dirty and not s.get("status"):
        return
    branch = core.gitout(top, "branch", "--show-current") or s.get("branch") or "detached"
    parts = [core.fmt_ts(s["started"]), agent, f"`{branch}`"]
    if subjects:
        shown = "; ".join(subjects[:4]) + (f" (+{len(subjects) - 4} more)" if len(subjects) > 4 else "")
        parts.append(f"{len(subjects)} commit(s): {shown}")
    if dirty:
        parts.append(f"{dirty} uncommitted file(s)")
    if s.get("status"):
        parts.append(f"status: {s['status'][:160]}")
    if not core.note_path(pid).exists():
        p = registry.inspect_repo(main, with_prs=False)
        registry.update_note(p)
    registry.add_session_line(pid, " · ".join(parts), sid[:12])


def _handoff_nudge(agent: str, data: dict, sess: dict):
    """Once per session: commits were made but the handoff note was not touched → ask the agent to update it."""
    if data.get("stop_hook_active") or sess.get("nudged") or not sess.get("top") or core.is_temp(sess["top"]):
        return None
    if not core.CFG.get("handoff", {}).get("enforce", True):
        return None
    head = sess.get("head")
    if not head:
        return None
    n = len(core.gitout(sess["top"], "log", "--format=%h", f"{head}..HEAD").splitlines())
    if n == 0 or _note_hash(sess["top"]) != sess.get("note_hash", ""):
        return None
    return (f"devkit: this session made {n} commit(s) but the project's handoff note was not updated. Before you "
            f"finish, update it with the devkit handoff skill (/devkit:handoff) — Current state, Tasks (`dev task done <n>` / "
            f"`dev task add \"…\"`), Decisions and gotchas — then end your reply as usual.")


def stop(agent: str):
    data = _stdin_json()
    from . import canary
    sid = data.get("session_id") or "-"
    sf = _session_file(agent, sid)
    out = {}
    if sf.exists():  # only sessions that started with devkit active (they loaded the global instructions)
        sess = core.read_json(sf, {}) or {}
        res = canary.check(agent, data)
        if res:
            ok, pid, line = res
            if ok and line:
                sess["status"] = line
                branch = core.gitout(sess.get("top") or data.get("cwd") or ".", "branch", "--show-current")
                canary.save_status(pid, agent, line, branch or sess.get("branch"))
            elif not ok and agent == "claude":
                out["systemMessage"] = canary.miss_message(agent, sid)
        reason = _handoff_nudge(agent, data, sess)
        if reason:
            sess["nudged"] = True
            out.update({"decision": "block", "reason": reason})
        sf.write_text(json.dumps(sess))
    if out:
        print(json.dumps(out))
    if sid == "-":
        return
    mark = STATE / "sessions" / f"{agent}-{sid}.stop"
    try:
        if mark.exists() and time.time() - mark.stat().st_mtime < STOP_DEBOUNCE_S:
            return
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.touch()
    except OSError:
        return
    _spawn_sync(["--session", f"{agent}:{sid}", "--offline"])


def config_change(agent: str):
    """Claude ConfigChange (user_settings): refuse changes to devkit-managed keys, then restore the file."""
    data = _stdin_json()
    from . import policy
    path = data.get("file_path") or str(core.CLAUDE_HOME / "settings.json")
    settings = core.read_json(path, None)
    if settings is None:
        return
    drift = [d for d in policy.diff_claude(settings) if not d.key.startswith("hooks.") or policy.lock_status() == "off"]
    if not drift:
        return
    ks = sorted({d.key for d in drift})
    keys = ", ".join(ks[:6]) + (f" (+{len(ks) - 6} more)" if len(ks) > 6 else "")
    _log(f"config-change blocked ({agent}): {keys}")
    policy._log(f"blocked change to devkit-managed keys: {keys}")
    _spawn(["config", "repair", "--quiet"])
    print(f"devkit manages these settings: {keys}. Change devkit's configuration instead "
          f"(config/claude/settings.json in the devkit repo or ~/.config/devkit/config.json) and run `dev install`.",
          file=sys.stderr)
    sys.exit(2)


def instructions_loaded(agent: str):
    """Claude InstructionsLoaded: remember which instruction files a session loaded (canary diagnostics)."""
    data = _stdin_json()
    sid = data.get("session_id") or "-"
    f = STATE / "instructions" / f"{agent}-{sid}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps({"ts": round(time.time()), "file": data.get("file_path"), "type": data.get("memory_type"),
                             "reason": data.get("load_reason")}) + "\n")


def session_end(agent: str):
    data = _stdin_json()
    sid = data.get("session_id") or ""
    if sid:
        _spawn_sync(["--session", f"{agent}:{sid}", "--finish"])


# --------------------------------------------------------------------------- guard

def guard(agent: str):
    if os.environ.get("DEVKIT_GUARD", "").lower() == "off":
        return
    data = _stdin_json()
    from . import guard as g
    verdict = g.check(data.get("tool_name") or "", data.get("tool_input") or {}, data.get("cwd") or os.getcwd(), agent)
    if not verdict:
        return
    decision, reason = verdict
    if agent == "codex" and decision == "ask":
        decision = "deny"
    _log(f"guard {agent} {decision}: {reason[:160]}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


HANDLERS = {"session-start": session_start, "stop": stop, "session-end": session_end, "guard": guard,
            "config-change": config_change, "instructions-loaded": instructions_loaded}


def run(event: str, agent: str) -> int:
    fn = HANDLERS.get(event)
    if not fn:
        return 0
    try:
        (STATE / "logs").mkdir(parents=True, exist_ok=True)
        fn(agent)
    except SystemExit:
        raise
    except Exception:  # fail open: never break the agent session
        _log(f"{event} {agent} error:\n{traceback.format_exc()}")
    return 0
