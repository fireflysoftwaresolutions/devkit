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
    init_dir = HANDOFF / "initiatives"
    if init_dir.exists():
        for d in sorted(init_dir.iterdir()):
            f2 = d / "README.md"
            if not f2.exists():
                continue
            ins = notesmod.summarize(f2.read_text())
            if p["id"] not in ins["projects"]:
                continue
            pct = f"{ins['progress']}%" if ins["progress"] is not None else "-"
            ws = (ins["workstreams"].get(p["id"]) or "").replace("\n", " ")[:220]
            lines.append(f"Initiative **{ins['title'] or d.name}** ({ins['status']}, {pct}; members: "
                         f"{', '.join(ins['projects'])}). This project's workstream: {ws or '-'}. "
                         f"Initiative Now: {'; '.join(ins['tasks']['now'][:3]) or '-'}. "
                         f"Note: {tilde(f2)} (update it for cross-project facts; `dev task -i {d.name}`).")
    try:
        from . import work
        me = work.actor(agent)
        mine, theirs = [], []
        scopes = [p["id"]] + [f"initiatives/{d.name}" for d in (HANDOFF / "initiatives").glob("*")
                              if p["id"] in notesmod.summarize((d / "README.md").read_text())["projects"]] \
            if (HANDOFF / "initiatives").exists() else [p["id"]]
        for sc in scopes:
            for it in work.all_items(sc):
                holder, expired = work.lock_holder(it)
                if holder and not expired:
                    entry = f"{it['meta']['id']} “{it['meta']['title']}” ({sc})"
                    (mine if holder.startswith(f"{me['user']}@{me['host']}") else theirs).append(f"{entry} — {holder}")
        if mine:
            lines.append("Your claimed tasks: " + "; ".join(x.rsplit(" — ", 1)[0] for x in mine[:6]))
        if theirs:
            lines.append("Claimed by others — do NOT work on these: " + "; ".join(theirs[:6]))
        others = [t for t in work.team() if not (t["user"] == me["user"] and t["host"] == me["host"])]
        busy = [f"{t['user']}@{t['host']} ({s_.get('agent')}, {s_.get('branch')})" for t in others
                for s_ in t.get("sessions", []) if s_.get("project") == p["id"]]
        if busy:
            lines.append("Also working on this project right now: " + ", ".join(busy[:5]) +
                         ". Coordinate through tasks (claim before you start) and pull before you push.")
    except Exception:
        pass
    try:
        from . import lessons
        inits = [d.name for d in (HANDOFF / "initiatives").glob("*") if (d / "README.md").exists() and
                 p["id"] in notesmod.summarize((d / "README.md").read_text())["projects"]] \
            if (HANDOFF / "initiatives").exists() else []
        known = lessons.context_lines(p["id"], inits)
        if known:
            lines.append("Known pitfalls (lessons from earlier mistakes; follow their prevention, and `dev lesson recur "
                         "<id> --evidence …` if one happens again):")
            lines += ["- " + x for x in known]
    except Exception:
        pass
    try:
        from . import delivery
        dl = delivery.summary_line(p["id"])
        lines.append(dl if dl else "Delivery policy: not recorded yet — `dev delivery` detects and recommends one "
                                   "(`release-policy` skill) before the first release.")
    except Exception:
        pass
    lang = (core.CFG.get("language") or "en-US")
    if lang.lower() not in ("en-us", "en"):
        lines.append(f"Language: this developer set devkit's language to {lang}: write replies, commits, PRs and docs "
                     f"in it unless they ask otherwise.")
    lines.append("Long work: `dev progress start \"<goal>\" --plan \"a; b; c\" --estimate 30m`, then `dev progress step` "
                 "and an update to the developer every few minutes (`communicating` skill: templates for updates, "
                 "decisions, PRs, handovers; plain American English).")
    lines.append("devkit manual: the `using-devkit` skill (session protocol, which tool for which job, project and "
                 "initiative rules, the status line).")
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
    from . import work
    sec = notesmod.sections(f.read_text())
    sec.pop("Session log", None)
    sec.pop("Tasks", None)
    sec["_items"] = sorted(f"{i['meta']['id']}:{i['meta'].get('version')}" for i in work.all_items(pid))
    return hashlib.sha256(json.dumps(sec, sort_keys=True).encode()).hexdigest()


def _register_session(agent: str, sid: str, cwd: str) -> Path:
    f = _session_file(agent, sid)
    if not f.exists():
        top = core.gitout(cwd, "rev-parse", "--show-toplevel")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"agent": agent, "session": sid, "cwd": cwd, "top": top, "started": time.time(),
                                 "head": core.gitout(top, "rev-parse", "HEAD") if top else "",
                                 "branch": core.gitout(top, "branch", "--show-current") if top else "",
                                 "note_hash": _note_hash(top) if top and not core.is_temp(top) else ""}))
    return f


def session_start(agent: str):
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    sid = data.get("session_id") or f"pid{os.getppid()}"
    f = _register_session(agent, sid, cwd)
    try:  # the project id (timing, progress) and DEVKIT_SESSION for this session's shell commands (Claude)
        from . import progress
        rec = core.read_json(f, {}) or {}
        if not rec.get("project") and rec.get("top") and not core.is_temp(rec["top"]):
            rec["project"] = progress._project(cwd)
            f.write_text(json.dumps(rec))
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a") as fh:
                fh.write(f"export DEVKIT_SESSION={agent}:{sid}\nexport DEVKIT_AGENT={agent}\n")
    except Exception:
        _log(f"session-start env {agent}: {traceback.format_exc()[-300:]}")
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
    if not os.path.isdir(top) or core.is_temp(top):
        return  # scratch repositories are not tracked
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
    from . import work
    a = work.actor(agent, sid)
    key = f"<!-- s:{sid[:12]} -->"
    line = " · ".join(parts[:1] + [f"{a['user']}@{a['host']}"] + parts[1:]) + f" {key}"
    f = work.SESSIONS / pid / f"{time.strftime('%Y-%m')}.md"
    if f.exists() and key in f.read_text():  # replace this session's line (Stop runs repeatedly)
        txt = "\n".join(ln if key not in ln else f"- {line}" for ln in f.read_text().splitlines()) + "\n"
        f.write_text(txt)
    else:
        work.log_session(pid, line)


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


def _plans_without_reason(sess: dict) -> list:
    """Plan/spec files changed in this session whose version was not bumped (no `dev plan revise`)."""
    started = sess.get("started") or 0
    pdir = HANDOFF / "plans"
    if not pdir.exists():
        return []
    from . import plans as plansmod
    out = []
    for f in pdir.rglob("*.md"):
        try:
            if f.stat().st_mtime <= started + 1:
                continue
        except OSError:
            continue
        if plansmod.needs_revision(f):
            out.append(str(f.relative_to(HANDOFF)))
    return out


def stop(agent: str):
    data = _stdin_json()
    from . import canary
    sid = data.get("session_id") or "-"
    sf = _session_file(agent, sid)
    out = {}
    if sf.exists():  # only sessions that started with devkit active (they loaded the global instructions)
        sess = core.read_json(sf, {}) or {}
        reasons = []
        res = canary.check(agent, data)
        if res:
            ok, pid, line = res
            if ok and line:
                sess["status"] = line
                branch = core.gitout(sess.get("top") or data.get("cwd") or ".", "branch", "--show-current")
                canary.save_status(pid, agent, line, branch or sess.get("branch"))
            elif not ok:
                if agent == "claude":
                    out["systemMessage"] = canary.miss_message(agent, sid)
                if core.CFG.get("canary", {}).get("enforce", True):
                    reasons.append("Your final message must start with the [Firefly> DevKit] status block (global "
                                   "instructions, section 0): a header `**[Firefly> DevKit]** <project> · `<branch>` · "
                                   "<task>`, then the bullets **Done:**, **Check:** (✓/⚠/✗ + evidence) and **Next:**. "
                                   "Reply now, starting with that block.")
        elif data.get("stop_hook_active"):
            text = data.get("last_assistant_message") or canary.last_assistant_text(data.get("transcript_path")) or ""
            if canary.has_marker(text):  # the corrected reply after a reminder: keep it as the latest status
                line = canary.status_text(text)
                sess["status"] = line
                top = sess.get("top") or ""
                if top and not core.is_temp(top):
                    from .registry import main_of
                    m = main_of(Path(top)) or Path(top)
                    canary.save_status(core.project_identity(m, core.gitout(m, "config", "--get", "remote.origin.url"))[0],
                                       agent, line, core.gitout(top, "branch", "--show-current"))
        try:
            from . import skillguard
            why = skillguard.on_stop(agent, data, sess)
            if why:
                reasons.append(why)
        except Exception:
            _log(f"stop skills {agent}: {traceback.format_exc()[-300:]}")
        if not data.get("stop_hook_active") and not sess.get("lesson_nudged") and sess.get("top"):
            try:
                from . import lessons
                fixes = lessons.fixed_in_session(sess["top"], sess.get("head"))
                if fixes:
                    sess["lesson_nudged"] = True
                    reasons.append("devkit: this session fixed or reverted something (" + "; ".join(fixes[:3]) +
                                   "). If it was a mistake that could happen again, record a lesson: `dev lesson add "
                                   "\"<title>\" --cause \"…\" --prevent \"…\" --evidence \"commit …\"` (or "
                                   "`dev lesson recur <id>` if a known pitfall repeated). Skip it for a trivial fix.")
            except Exception:
                _log(f"stop lessons {agent}: {traceback.format_exc()[-300:]}")
        plans = _plans_without_reason(sess)
        if plans and not data.get("stop_hook_active"):
            reasons.append("devkit: you changed plan(s) without recording why: " + ", ".join(plans[:4]) +
                           ". Run `dev plan revise <file> --why \"…\"` for each (it versions the plan and logs the "
                           "reason), then finish.")
        nudge = _handoff_nudge(agent, data, sess)
        if nudge:
            sess["nudged"] = True
            reasons.append(nudge)
        if reasons:
            out.update({"decision": "block", "reason": " ".join(reasons)})
        else:
            from . import progress
            progress.on_turn_end(agent, sid, sess)
            sess["turn_started"] = None
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
    _spawn_sync(["--session", f"{agent}:{sid}", "--offline", "--renew", agent])


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
    try:
        from . import progress
        progress.tool_started(data)
    except Exception:
        pass
    from . import guard as g
    verdict = g.check(data.get("tool_name") or "", data.get("tool_input") or {}, data.get("cwd") or os.getcwd(), agent)
    if not verdict:
        if data.get("tool_name") in ("Agent", "Task"):  # subagent briefs get the skills their work requires
            try:
                from . import skillguard
                updated = skillguard.on_agent_brief(agent, data.get("tool_input") or {})
            except Exception:
                updated = None
            if updated:
                _log(f"skills {agent}: appended required skills to a subagent brief")
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                         "updatedInput": updated}}))
        return
    decision, reason = verdict
    if agent == "codex" and decision == "ask":
        decision = "deny"
    _log(f"guard {agent} {decision}: {reason[:160]}")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


def _context_out(event: str, text: str):
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def prompt(agent: str):
    """UserPromptSubmit: start the turn clock; answer "how is it going?" from real progress records.

    Safety net: a session without a devkit record never ran SessionStart (hook failure, an untrusted Codex hook,
    an old client). Register it now and inject the full devkit context, so every session works with devkit."""
    from . import progress
    data = _stdin_json()
    sid = data.get("session_id") or ""
    extra = ""
    if sid and not _session_file(agent, sid).exists():
        cwd = data.get("cwd") or os.getcwd()
        _register_session(agent, sid, cwd)
        extra = context_for(cwd, agent)
        _log(f"prompt {agent}: session {sid[:12]} had no SessionStart; devkit context injected")
    raw = (data.get("prompt") or "").lstrip()
    if raw.startswith(("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")) or \
            "<task-notification>" in raw[:400]:
        _context_out("UserPromptSubmit", extra)  # background events are not the developer's request
        return
    text = progress.on_prompt(agent, data)
    skills = ""
    try:
        from . import skillguard
        sf = _session_file(agent, sid) if sid else None
        sess = (core.read_json(sf, {}) or {}) if sf and sf.exists() else {}
        skills = skillguard.on_prompt(agent, data.get("prompt") or "", sess)
    except Exception:
        _log(f"prompt skills {agent}: {traceback.format_exc()[-300:]}")
    _context_out("UserPromptSubmit", "\n\n".join(x for x in (extra, skills, text) if x))


def post_tool(agent: str):
    """PostToolUse: time slow calls; remind the agent to update the developer after a long silence."""
    from . import progress
    _context_out("PostToolUse", progress.on_tool_done(agent, _stdin_json()))


def subagent_start(agent: str):
    from . import progress
    progress.on_subagent(agent, _stdin_json(), True)


def subagent_stop(agent: str):
    from . import progress
    progress.on_subagent(agent, _stdin_json(), False)


HANDLERS = {"session-start": session_start, "stop": stop, "session-end": session_end, "guard": guard,
            "config-change": config_change, "instructions-loaded": instructions_loaded, "prompt": prompt,
            "post-tool": post_tool, "subagent-start": subagent_start, "subagent-stop": subagent_stop}


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
