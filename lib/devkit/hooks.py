# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hook entry points for Claude Code and Codex (`dev hook <event> --agent claude|codex`).

  session-start  inject project context (handoff note, git state, commands) and record the session start
  stop           debounced background refresh of the map + session log (long-lived Desktop/app sessions)
  session-end    final background sync for the session
  guard          pre-tool-use workspace rules (see guard.py)

Hooks must be fast and must never break a session: every handler catches all
errors, logs them to ~/.local/state/devkit/logs/hooks.log and exits 0.
"""
from __future__ import annotations

import copy
import io
import json
import re
import os
import shlex
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import core, state
from .core import DEV, HANDOFF, STATE, tilde

STOP_DEBOUNCE_S = 120  # session line, lock renewal and presence at most every 2 min


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
    """The session's record. It is written only through state (update, or patch for a copy read earlier)."""
    return STATE / "sessions" / f"{agent}-{sid}.json"


def _patch_quietly(path: Path, before: dict, after: dict):
    """state.patch for a hook that goes on whatever happens: a busy or unreadable record drops this hook's change
    whole (state audits it), and the hook still answers."""
    try:
        state.patch(path, before, after)
    except state.StateBusy:
        pass


def _spawn(args, logname="sync.log"):
    """Detached background `dev …` so the hook returns immediately. It runs the release install.json pins
    (core.release_home): a session that still runs an older copy starts the installed release, never itself."""
    # The child gets its own copy of the log's descriptor; ours closes once it is started (an open one warns at exit).
    with open(STATE / "logs" / logname, "a") as log:
        subprocess.Popen([sys.executable, "-I", "-S", str(core.release_home() / "bin" / "dev"), *args],  # its shebang
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)


def _older_copy_notice(agent: str, sid: str, what: str) -> str:
    """When this copy is older than the pinned release: record the skip (core.older_copy_skip) and return what the
    session hears, the first time only for each session and pinned release; "" otherwise."""
    old = core.older_copy_skip(what)
    if not old:
        return ""
    who, first = f"{agent}:{sid or '-'}", []

    def mark(d):
        if d.get("pinned") != old["pinned"]:
            d.clear()
            d["pinned"] = old["pinned"]
        told = d.setdefault("told", [])
        if who in told:
            return state.SKIP
        told.append(who)
        del told[:-200]
        first.append(True)

    try:
        state.update(STATE / "older-copy.json", mark)
    except state.StateBusy:
        return ""
    return core.older_copy_words(old) if first else ""


def _spawn_sync(args):
    _spawn(["sync", "--quiet", *args])


# --------------------------------------------------------------------------- sessions to restart after an install

AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def sessions_to_restart(sessions_dir=None, alive=None) -> list:
    """The Claude Code and Codex sessions that use devkit and still run, from devkit's session records (each holds the
    agent process its SessionStart recorded): [{agent, session, project, branch, devkit, started}], oldest first. A
    process devkit cannot check counts as running."""
    if alive is None:
        from .approvals import pid_alive as alive
    d = Path(sessions_dir or core.STATE / "sessions")
    out = []
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            s = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(s, dict) or s.get("agent") not in AGENT_NAMES or not s.get("pid"):
            continue
        if alive(s["pid"], s.get("pid_start")) is False:
            continue
        out.append({"agent": s["agent"], "session": str(s.get("session") or ""),
                    "project": s.get("project") or tilde(s.get("top") or s.get("cwd") or "?"),
                    "branch": s.get("branch") or "", "devkit": s.get("devkit") or "",
                    "started": float(s.get("started") or 0)})
    return sorted(out, key=lambda x: x["started"])


def restart_lines(sessions: list) -> list:
    return [f"  {AGENT_NAMES[s['agent']]} · {s['project']}" + (f" ({s['branch']})" if s["branch"] else "")
            + (f" · devkit {s['devkit']}" if s["devkit"] else "") + f" · session {s['session'][:8]}"
            + f" · since {core.fmt_ts(s['started'])}" for s in sessions]


def confirm_restart(assume_yes=False, ask=None, say=print, interactive=None, sessions_dir=None, alive=None) -> bool:
    """`dev install` and `dev update`, before they change anything: name the running sessions that use devkit, each
    of which keeps its hooks until it is restarted -> whether to go on. With assume_yes, or with no terminal to ask
    in, it prints the list and goes on; in a terminal it asks once (Enter goes on, n stops with nothing changed)."""
    sessions = sessions_to_restart(sessions_dir, alive)
    if not sessions:
        return True
    n = len(sessions)
    say(f"{n} running session{'s use' if n > 1 else ' uses'} devkit; restart each after this to load the new "
        f"hooks (until then it keeps the old ones, which change no setting):")
    for line in restart_lines(sessions):
        say(line)
    if interactive is None:
        try:
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
        except (AttributeError, ValueError):
            interactive = False
    if assume_yes or not interactive:
        return True
    try:
        answer = (ask or input)("  Go on? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        return False
    return answer.strip().lower() not in ("n", "no")


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
    lines = ["## devkit · workspace context", "",
             "For development work only (code, debugging, tests, builds, deploys, releases, developer tools). If a "
             "request is something else (a recipe, a general question, consulting work with the Firefly consulting "
             "kit), answer it normally: no status block and no devkit process.", ""]
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
    try:  # the branch map (spec P-fy3bwa0pshk, R8): which branches live only on this Mac, built offline
        from . import branches as branchmap
        bl = branchmap.context_line(p.get("branches") or [])
    except Exception:
        bl = ""
        _log(f"context branches: {traceback.format_exc()[-300:]}")
    if bl:
        lines.append(bl)
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


def _register_session(agent: str, sid: str, cwd: str, data: dict = None) -> Path:
    f = _session_file(agent, sid)
    if not f.exists():
        top = core.gitout(cwd, "rev-parse", "--show-toplevel")  # git before the lock
        from .canary import surface
        rec = {"agent": agent, "session": sid, "cwd": cwd, "top": top, "started": time.time(), "devkit": core.VERSION,
               "surface": surface(agent, data),
               "head": core.gitout(top, "rev-parse", "HEAD") if top else "",
               "branch": core.gitout(top, "branch", "--show-current") if top else ""}
        try:  # the one writer that creates a session record; one another hook created meanwhile stays
            state.update(f, lambda d: state.SKIP if d else rec)
        except state.StateBusy:
            pass
    try:
        _claim_process(agent, sid, f)
    except Exception:
        _log(f"register pid {agent}: {traceback.format_exc()[-300:]}")
    return f


def _claim_process(agent: str, sid: str, f: Path):
    """Record the agent process this session runs in (pid, pid_start), on every way a session registers: SessionStart,
    or the first prompt of a session that had none (Claude Code gives a running conversation a new session id after a
    compaction). A `dev` command below that process then finds this session by its parents (progress.resolve_session).
    An older Claude Code record that holds the same process is kept, marked superseded_by this session, and gives the
    pid up (pid_superseded): one Claude Code process runs one conversation, and its shells still name the older id
    in DEVKIT_SESSION. Codex runs several threads in one process, so its records keep their pids."""
    from . import approvals, progress
    pid = progress.agent_pid()
    if not pid:
        return
    rec = state.read(f) or {}
    start = approvals.pid_start(pid)  # ps, before any lock
    if rec.get("pid") != pid or rec.get("pid_start") != start:
        state.update(f, lambda d: d.update(pid=pid, pid_start=start), create=False)
    if agent != "claude":
        return
    took = False
    for g in sorted(f.parent.glob(f"{agent}-*.json")):
        if g == f:
            continue
        other = state.read(g) or {}
        if other.get("agent") != agent or other.get("pid") != pid or \
                (other.get("pid_start") and start and other["pid_start"] != start) or \
                float(other.get("started") or 0) > float(rec.get("started") or time.time()):
            continue

        def supersede(d):
            if d.get("pid") != pid:
                return state.SKIP
            d["pid_superseded"] = d.pop("pid")
            d.update(superseded_by=sid, superseded_at=time.time())
        try:
            took = bool((state.update(g, supersede, create=False) or {}).get("superseded_by") == sid) or took
        except state.StateBusy:
            _log(f"register pid {agent}: {g.name} busy, still holds pid {pid}")
    if took:
        try:  # the older session's open run and its lanes go on in this one
            progress.adopt_process_run(agent, sid, pid)
        except Exception:
            _log(f"register run {agent}: {traceback.format_exc()[-300:]}")


def session_start(agent: str):
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    sid = data.get("session_id") or f"pid{os.getppid()}"
    f = _register_session(agent, sid, cwd, data)
    try:  # the agent's own process (spec C4) is recorded by _register_session (_claim_process)
        from . import progress
        rec_ = state.read(f) or {}
        more = {"transcript": data["transcript_path"]} if data.get("transcript_path") else {}
        if more and any(rec_.get(k) != v for k, v in more.items()):
            state.update(f, lambda d: d.update(more), create=False)
        progress.resume_orphaned(agent, sid)
    except Exception:
        _log(f"session-start pid {agent}: {traceback.format_exc()[-300:]}")
    try:  # the project id (timing, progress) and DEVKIT_SESSION for this session's shell commands (Claude)
        from . import progress
        rec = state.read(f) or {}
        if not rec.get("project") and rec.get("top") and not core.is_temp(rec["top"]):
            project = progress._project(cwd)  # git, before the lock
            try:
                state.update(f, lambda d: d.update(project=project), create=False)
            except state.StateBusy:
                pass  # audited by state; DEVKIT_SESSION below matters more
        env_file = os.environ.get("CLAUDE_ENV_FILE")
        if env_file:
            with open(env_file, "a") as fh:
                fh.write(f"export DEVKIT_SESSION={agent}:{sid}\nexport DEVKIT_AGENT={agent}\n")
    except Exception:
        _log(f"session-start env {agent}: {traceback.format_exc()[-300:]}")
    try:  # an older copy than the pinned release repairs nothing itself: the session hears it once
        older = _older_copy_notice(agent, sid, "the session-start repair")
    except Exception:
        older = ""
        _log(f"session-start older copy {agent}: {traceback.format_exc()[-300:]}")
    _spawn(["config", "repair", "--quiet"])  # the pinned release restores any devkit-managed setting changed outside
    try:
        from . import work
        work.request_sync("session start", delay=1)  # pull other machines' claims and publish this session
    except Exception:
        pass
    text = "\n\n".join(x for x in (_continuity("on_session_start", agent, data),
                                   _with_catalog(context_for(cwd, agent), agent)) if x)
    shown = [older] if older else []  # the notices the developer must see too, not only the agent (Claude: a system message)
    text += f"\n\n{older}" if older else ""
    try:  # devkit notices from files only, no network (spec P-fym583l7sg1)
        from . import ext, updatecheck
        notices = ((lambda: updatecheck.session_line(installed=updatecheck.installed_version()), False),  # daily
                   (lambda: ext.session_line(cwd), False),  # extensions changed outside devkit, each audited once
                   (lambda: ext.accepted_line(), True),  # extensions accepted since the developer's last ack
                   (lambda: ext.last_ack_line(), True))  # that last ack, once, with its terminal
    except Exception:
        notices = ()
        _log(f"session-start notices {agent}: {traceback.format_exc()[-300:]}")
    for notice, to_developer in notices:
        try:
            line = notice()
            text += f"\n\n{line}" if line else ""
            if line and to_developer:
                shown.append(line)
        except Exception:
            _log(f"session-start notice {agent}: {traceback.format_exc()[-300:]}")
    if os.environ.get("DEVKIT_GUARD", "").lower() == "off":  # spec F8, rule 5: never silent
        text = ("devkit · the guard is off in this session (DEVKIT_GUARD=off): it judges no tool call, so the "
                "workspace rules, the crew's limits and the irreversible-action gate do not hold. That is recorded "
                "once as guard.off in the audit trail. Start the session without it to turn the guard back on.\n\n"
                + text)
    if agent == "codex":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    elif shown:  # Claude Code shows a system message to the developer, as the canary does at the Stop hook
        print(json.dumps({"systemMessage": "\n\n".join(shown),
                          "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}))
    else:
        print(text)


def _continuity(fn: str, agent: str, data: dict, **kw):
    """Call a continuity hook entry point (spec C12). Each fails open itself; this also keeps an import error from
    reaching the hook. -> its text ("" on any error)."""
    try:
        from . import continuity
        return getattr(continuity, fn)(agent, data, **kw) or ""
    except Exception:
        _log(f"continuity {fn} {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _prompt_refresh(data: dict, force: bool = False):
    """The shell prompt's facts for the event's folder (spec R9): a few file reads, no git, network or subprocess.
    A tool call refreshes at most every 5 s; `force` for the events that change what the prompt shows."""
    try:
        from . import prompt
        prompt.safe_refresh(data.get("cwd") or os.getcwd(), force=force)
    except Exception:
        _log(f"prompt refresh: {traceback.format_exc()[-300:]}")


def pre_compact(agent: str):
    """PreCompact: the checkpoint and the compaction's record; in Claude's main session, devkit's preservation
    instructions on stdout, which Claude adds to the summarizer's. Never blocks: always exit 0, never a decision
    (a member's stdout is ignored, and Codex gets nothing)."""
    data = _stdin_json()
    text = _continuity("on_pre_compact", agent, data)
    if text and agent == "claude" and not data.get("agent_id"):
        print(text)


def post_compact(agent: str):
    """PostCompact: closes the compaction and keeps "Not in the summary" for the next hook. Prints nothing: the
    output of this event is display only, and the summary is compared, never re-injected."""
    _continuity("on_post_compact", agent, _stdin_json())


CATALOG_MAX = 8000  # characters of the catalog in the session's context (about 7,200 with the members' roles)


def catalog_context(agent: str) -> str:
    """The catalog of ids the model declares (C13.1), cut at whole lines to CATALOG_MAX, with where the rest is."""
    try:
        from . import crew
        text = crew.catalog(agent=agent)
    except Exception:
        _log(f"catalog {agent}: {traceback.format_exc()[-300:]}")
        return ""
    if len(text) <= CATALOG_MAX:
        return text
    tail = "… the rest: `dev crew raci` (kinds of work), `dev crew rules` (skills), `dev lesson ls` (anchors)."
    out, size = [], len(tail) + 1
    for ln in text.splitlines():
        if size + len(ln) + 1 > CATALOG_MAX:
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out + [tail])


def _with_catalog(text: str, agent: str) -> str:
    """A project's context with the catalog, before the handoff note: the note is the part a cut may lose."""
    if "\nHandoff note: " not in text:
        return text  # not a project (a scratch repository, or outside any): no development catalog
    cat = catalog_context(agent)
    if not cat:
        return text
    head, sep, note = text.partition("\n\n--- handoff note ---")
    return head + "\n\n" + cat + sep + note


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


HANDOFF_CHECK = STATE / "handoff-check"  # per project: the hash of the note's human sections and when it changed
GENERATED_SECTIONS = ("Session log", "Tasks")


def _human_digest(text: str) -> str:
    """The note without the sections devkit writes itself."""
    import hashlib
    from . import notes as notesmod
    sec = notesmod.sections(text)
    for k in GENERATED_SECTIONS:
        sec.pop(k, None)
    return hashlib.sha256(json.dumps(sec, sort_keys=True).encode()).hexdigest()


def _human_changed_at(note: Path, digest: str) -> float:
    """When the note's human sections took their current form: walk the handoff repository's history of the note back
    to the revision where they differ. A note outside git (or with an uncommitted human edit) falls back to its file
    time."""
    repo = core.gitout(note.parent, "rev-parse", "--show-toplevel")
    if not repo:
        return note.stat().st_mtime
    rel = os.path.relpath(note.resolve(), Path(repo).resolve())
    since = None
    for ln in core.gitout(repo, "log", "-n", "60", "--format=%ct %H", "--", rel).splitlines():
        ts, _, sha = ln.partition(" ")
        if not ts.isdigit() or _human_digest(core.gitout(repo, "show", f"{sha}:{rel}")) != digest:
            break
        since = float(ts)
    return since if since is not None else note.stat().st_mtime


def handoff_staleness(top: str, note: Path, state_file: Path, email: str, now: float) -> tuple:
    """Commits on HEAD by `email` newer than the last change of the note's human sections (Current state, Next
    steps, Decisions…) -> ([(hash, commit time)], newest first; when those sections last changed). A task or session
    line written by devkit is not a change; a teammate's commit pulled in is not this developer's work. The first
    time a note is seen, the handoff history says when it changed."""
    if not note.exists():
        return [], now
    digest = _human_digest(note.read_text())
    st = core.read_json(state_file, {}) or {}
    if st.get("hash") != digest:
        st = {"hash": digest, "changed": now if st else _human_changed_at(note, digest)}
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(st))
    out = []
    for ln in core.gitout(top, "log", "-n", "300", "--format=%ct%x09%ce%x09%h", "HEAD").splitlines():
        ts, ce, sha = (ln.split("\t") + ["", "", ""])[:3]
        if not ts.isdigit() or int(ts) <= st["changed"]:
            break  # newest first: everything below is older than the note's last change
        if ce.lower() == email.lower():
            out.append((sha, int(ts)))
    return out, st["changed"]


HANDOFF_BLOCK = {"block_after_commits": 5, "block_after_minutes": 60}  # handoff.* (threaded turn endings, R3)


def _handoff_limits() -> tuple:
    """(commits, minutes) after which the handoff gate sends the turn back; below them the reminder is enough."""
    cfg = core.CFG.get("handoff") or {}
    out = []
    for k, default in HANDOFF_BLOCK.items():
        try:
            out.append(max(1, int(cfg.get(k, default))))
        except (TypeError, ValueError):
            out.append(default)
    return tuple(out)


def _handoff_state(agent: str, data: dict, sess: dict, now=None):
    """(project, [(hash, time)] of the developer's commits newer than the note's last real update, when it changed)
    in the repository the session works in now (it may have started outside one, as Claude Desktop sessions do).
    None where the check does not apply: a member (Morpheus records their work), handoff.enforce off, no project or
    no git identity."""
    if data.get("agent_id") or not core.CFG.get("handoff", {}).get("enforce", True):
        return None
    top = core.gitout(data.get("cwd") or os.getcwd(), "rev-parse", "--show-toplevel") or sess.get("top") or ""
    if not top or core.is_temp(top):
        return None
    pid = _session_project(data, sess)
    email = core.gitout(top, "config", "user.email")
    if not pid or not email:
        return None
    stale, changed = handoff_staleness(top, core.note_path(pid), HANDOFF_CHECK / (pid.replace("/", "__") + ".json"),
                                    email, now or time.time())
    return pid, stale, changed


def _handoff_nudge(agent: str, data: dict, sess: dict, now=None):
    """End of a development turn: the commits made since the handoff note last really changed send the turn back
    only when it matters (R3): handoff.block_after_commits of them (5), the oldest older than
    handoff.block_after_minutes (60), or a milestone after one of them (the end of the run, a work item closed, a
    push, a PR, a release). Below that, the reminder before the end said it (turn_checklist). Never for a member, and
    the Stop hook skips it while members work. The check re-arms after each update of the note."""
    if data.get("stop_hook_active"):
        return None
    now = now or time.time()
    st = _handoff_state(agent, data, sess, now)
    if not st or not st[1]:
        return None
    pid, stale, changed = st
    commits, minutes = _handoff_limits()
    oldest = min(ts for _, ts in stale)
    # a milestone makes the note due for the commits made before it (so it also came after the note changed); one
    # that came before every unrecorded commit found the note current
    mts, milestone = next(((ts, w) for ts, w in reversed(sess.get("milestones") or []) if ts >= oldest), (0, None))
    if len(stale) >= commits:
        why = ""
    elif now - oldest >= minutes * 60:
        why = f" The oldest is {int((now - oldest) // 60)} minutes old."
    elif milestone:
        before = sum(1 for _, ts in stale if ts <= mts)
        who = "They come" if before == len(stale) else f"{before} of them come{'s' if before == 1 else ''}"
        why = f" {who} before a milestone: after `{milestone}` the note is due."
    else:
        return None
    from . import work
    a = work.actor(agent, sess.get("session") or "")
    mine = [i["meta"]["id"] for i in work.all_items(pid) if i["meta"].get("status") == "doing" and
            str(i["meta"].get("claimed_by") or "").startswith(f"{a['user']}@{a['host']}")]
    tasks = (f"Tasks: keep {', '.join(mine)} current (`dev task done <id>` once verified; `dev task add \"…\" --why "
             f"\"…\"` for follow-ups)." if mine else
             "Tasks: claim the work item for this work (`dev task start <id>`) or add it (`dev task add \"…\" --now`).")
    return (f"devkit: {len(stale)} commit(s) in {pid} are newer than its handoff note.{why} Update the note now "
            f"with the devkit handoff skill (/devkit:handoff): Current state, Next steps, Decisions and gotchas, short "
            f"and true. {tasks}")


def _session_project(data: dict, sess: dict) -> str:
    """The project this session works on now: the repository of its current folder (it may have started outside one),
    else the one it started in. "" outside any project (a portfolio session)."""
    top = core.gitout(data.get("cwd") or os.getcwd(), "rev-parse", "--show-toplevel") or sess.get("top") or ""
    if not top or core.is_temp(top):
        return ""
    from .registry import main_of
    main = main_of(Path(top)) or Path(top)
    return core.project_identity(main, core.gitout(main, "config", "--get", "remote.origin.url"))[0] or ""


def _plans_without_reason(sess: dict, pid=None) -> list:
    """Plan/spec files changed in this session whose version was not bumped (no `dev plan revise`). Only the plans of
    the session's project (`pid`): another session may be changing another project's plans at the same time. Without
    a project (a portfolio session), every plan."""
    started = sess.get("started") or 0
    pdir = HANDOFF / "plans" / pid if pid else HANDOFF / "plans"
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


TEST_FILE_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+\.py$|_test\.(py|go)$|\.(spec|test)\.[jt]sx?$|"
                          r"(Test|Tests|IT)\.(java|kt)$|Test\.php$")


def _turn_files(data: dict, sess: dict):
    """(repo top, [existing files inside it that this turn edited])."""
    from . import scope
    cwd = data.get("cwd") or os.getcwd()
    top = sess.get("top") or core.gitout(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return None, []
    files = []
    for e in scope.turn_actions(data.get("transcript_path")).get("edits") or []:
        for f in (e or "").split():
            fp = Path(f) if os.path.isabs(f) else Path(cwd) / f
            if fp.exists() and core.is_under(str(fp), top):
                files.append(str(fp))
    return top, sorted(set(files))


_UNSET = object()


def _unroled(data: dict, transcript: str) -> list:
    """The crew members a final message's prose names outside `<role> (<Name>)` (the developer's role rule, M346;
    canary.bare_names) -> their names; [] when canary.enforce or canary.roles is off. Fails open."""
    cfg = core.CFG.get("canary", {}) or {}
    if not (cfg.get("enforce", True) and cfg.get("roles", True)):
        return []
    try:
        from . import canary
        text = data.get("last_assistant_message") or canary.last_assistant_text(data.get(transcript)) or ""
        return canary.bare_names(text)
    except Exception:
        _log(f"roles: {traceback.format_exc()[-300:]}")
        return []


def _stub_findings(data: dict, sess: dict) -> tuple:
    """(repository, the unfinished work the turn left in the files it edited, or None). Fails open."""
    try:
        top, files = _turn_files(data, sess)
        if not files:
            return top, None
        from . import stubs
        return top, stubs.stop_gate(top, files)
    except Exception:
        _log(f"stop stubs: {traceback.format_exc()[-300:]}")
        return None, None


def _stub_gate(data: dict, sess: dict):
    """Unfinished work the turn left in the files it edited (stubs, placeholders, TODOs without a task) sends the
    turn back: devkit builds things for real. Fails open."""
    top, msg = _stub_findings(data, sess)
    if msg:
        from . import audit
        audit.record("stubs.gate", top, msg[:300], "blocked")
    return msg


def _test_findings(data: dict, sess: dict) -> tuple:
    """(repository, the findings on the tests the turn edited that cannot fail, or None). Fails open."""
    try:
        top, edited = _turn_files(data, sess)
        if not top:
            return None, None
        try:
            from .testx import is_test_file
        except Exception:
            is_test_file = TEST_FILE_RE.search
        files = [f for f in edited if is_test_file(f)]
        if not files:
            return top, None
        from . import testaudit
        return top, testaudit.stop_gate(top, sorted(set(files)))
    except Exception:
        _log(f"stop tests: {traceback.format_exc()[-300:]}")
        return None, None


def _test_gate(data: dict, sess: dict):
    """Tests the turn edited that cannot fail (no assertion, trivial or swallowed assertions, empty bodies) send the
    turn back with the findings (testaudit.stop_gate). Fails open."""
    top, msg = _test_findings(data, sess)
    if msg:
        _log("stop tests: test files with tests that cannot fail")
        from . import audit
        audit.record("tests.gate", top, msg[:300], "blocked")
    return msg


def _ci_watch_gate(agent: str, data: dict):
    """A turn that sent work to origin and leaves its unfinished CI run with nothing watching it that wakes this agent
    goes back with the command to start (ciwatch.stop_gate); the turn's stop.block names the rule. Fails open."""
    try:
        from . import ciwatch
        return ciwatch.stop_gate(agent, data)
    except Exception:
        _log(f"stop ci watch: {traceback.format_exc()[-300:]}")
        return None


def turn_checklist(agent: str, data: dict, sess: dict) -> list:
    """What the Stop gate would ask at this point of the turn, so it reaches the agent before the end (threaded turn
    endings, R1) -> [(rule, text)] still open: handoff (the developer's commits newer than the project's note),
    skills (required skills not loaded for the files changed), tests (tests the turn edited that cannot fail), stubs
    (unfinished work without a task). The Stop gate's own functions; nothing is marked as asked, each fails open.
    The status block is in every closing reminder (R5)."""
    items = []
    try:
        st = _handoff_state(agent, data, sess)
        if st and st[1]:
            n = len(st[1])
            items.append(("handoff", f"{n} commit{'s' if n != 1 else ''} since the handoff note of {st[0]}: update it "
                                     f"(the handoff skill) before your final message."))
    except Exception:
        _log(f"checklist handoff: {traceback.format_exc()[-300:]}")
    try:
        from . import skillguard
        why = skillguard.on_stop(agent, data, copy.deepcopy(sess))  # on a copy: nothing is marked as asked
        if why:
            items.append(("skills", why.replace("devkit: ", "", 1)))
    except Exception:
        _log(f"checklist skills: {traceback.format_exc()[-300:]}")
    for rule, find in (("tests", _test_findings), ("stubs", _stub_findings)):
        msg = find(data, sess)[1]
        if msg:
            items.append((rule, msg))
    why = _ci_watch_gate(agent, data)  # right after a push, before the turn can end on a promise
    if why:
        items.append(("watch", why.replace("devkit: ", "", 1)))
    return items


def stop(agent: str):
    data = _stdin_json()
    from . import canary
    sid = data.get("session_id") or "-"
    sf = _session_file(agent, sid)
    out = {}
    if sf.exists():  # only sessions that started with devkit active (they loaded the global instructions)
        try:  # the developer saw this message: the decisions it names are shown from now on (spec C5)
            from . import progress as _shown
            _shown.on_final_message(agent, sid, data.get("last_assistant_message") or canary.last_assistant_text(
                data.get("transcript_path")) or "")
        except Exception:
            _log(f"stop shown {agent}: {traceback.format_exc()[-300:]}")
        _prompt_refresh(data, force=True)  # the decisions the reply showed leave the prompt at once
        sess = state.read(sf) or {}
        before = copy.deepcopy(sess)  # the gates below take seconds: only what this hook changes is written back
        reasons, ended = [], False  # reasons: (rule, text), the rule for the audit (R4)
        classed = None  # what continuity keeps the turn's prompts by: None when the classifier failed (change 5)
        try:  # devkit's process is for development turns only (a recipe or a consulting deck gets nothing)
            from . import scope
            dev_turn = scope.decide(data, sess)
            classed = bool(dev_turn)
        except Exception:
            dev_turn = True
        if not dev_turn:
            _log(f"stop {agent}: not development work ({sess.get('turn_class') or 'no signal'}); devkit stays out")
        res = canary.check(agent, data) if dev_turn else None
        if res:
            ok, pid, line = res
            if ok and line:
                sess["status"] = line
                where = sess.get("top") or data.get("cwd") or "."
                branch = core.gitout(where, "branch", "--show-current")
                top_ = sess.get("top") or core.gitout(where, "rev-parse", "--show-toplevel")  # the session's checkout
                canary.save_status(pid, agent, line, branch or sess.get("branch"), path=top_ or None)
            elif not ok:
                from . import audit
                audit.record("canary.miss", pid or "", "final message without the status block", "miss")
                sess["canary_missed"] = True  # later prompts in this session get a reminder
                if agent == "claude":
                    out["systemMessage"] = canary.miss_message(agent, sid)
                if core.CFG.get("canary", {}).get("enforce", True):
                    text_ = data.get("last_assistant_message") or canary.last_assistant_text(
                        data.get("transcript_path")) or ""
                    reasons.append(("status", canary.miss_reason(text_) + " Reply now with the block as the very "
                                    "first lines: `**[Firefly> DevKit]** <project> · `<branch>` · <task>`, then the "
                                    "bullets **Done:**, **Check:** (✓/⚠/✗ + evidence) and **Next:** (global "
                                    "instructions §0)."))
        elif data.get("stop_hook_active"):
            text = data.get("last_assistant_message") or canary.last_assistant_text(data.get("transcript_path")) or ""
            if canary.has_marker(text):  # the corrected reply after a reminder: keep it as the latest status
                line = canary.status_text(text)
                sess["status"] = line
                top = sess.get("top") or ""
                if top and not core.is_temp(top):
                    from .registry import main_of
                    m = main_of(Path(top)) or Path(top)
                    pid_ = core.project_identity(m, core.gitout(m, "config", "--get", "remote.origin.url"))[0]
                    canary.save_status(pid_, agent, line, core.gitout(top, "branch", "--show-current"), path=top)
                    if sess.get("canary_missed"):
                        canary.record_corrected(agent, sid, pid_, line)
        if dev_turn and not data.get("stop_hook_active") and sess.get("roles_told", _UNSET) != sess.get("turn_started"):
            names = _unroled(data, "transcript_path")  # the role rule (M346), once per turn: a false one cannot loop
            if names:
                sess["roles_told"] = sess.get("turn_started")
                reasons.append(("roles", f"devkit: your final message {canary.roles_reason(names)} Reply again with "
                                         f"the message fixed."))
        try:
            from . import skillguard
            why = skillguard.on_stop(agent, data, sess) if dev_turn else None
            if why:
                reasons.append(("skills", why))
        except Exception:
            _log(f"stop skills {agent}: {traceback.format_exc()[-300:]}")
        if dev_turn and not data.get("stop_hook_active"):
            for rule, gate in (("stubs", _stub_gate), ("tests", _test_gate)):
                why = gate(data, sess)
                if why:
                    reasons.append((rule, why))
            why = _ci_watch_gate(agent, data)  # Claude and Codex: each is told the watch that wakes it
            if why:
                reasons.append(("watch", why))
        try:  # lessons the reply names were applied: that use keeps them alive
            from . import lessons as _les
            said = data.get("last_assistant_message") or ""
            ids = sorted(set(re.findall(r"\bL-[0-9a-z]{9,}\b", said)))
            if ids:
                _les.track(ids, "applied")
        except Exception:
            pass
        if dev_turn and not data.get("stop_hook_active") and not sess.get("lesson_nudged") and sess.get("top"):
            try:
                from . import lessons
                fixes = lessons.fixed_in_session(sess["top"], sess.get("head"))
                if fixes:
                    sess["lesson_nudged"] = True
                    reasons.append(("lesson", "devkit: this session fixed or reverted something (" + "; ".join(fixes[:3]) +
                                   "). If it was a mistake that could happen again, record a lesson: `dev lesson add "
                                   "\"<title>\" --cause \"…\" --prevent \"…\" --evidence \"commit …\"` (or "
                                   "`dev lesson recur <id>` if a known pitfall repeated). Skip it for a trivial fix."))
            except Exception:
                _log(f"stop lessons {agent}: {traceback.format_exc()[-300:]}")
        try:  # crew members may be writing plans right now; their own `dev plan revise` records them
            from . import progress as _pg
            busy = _pg.crew_working(_pg.load_run(agent, sid) or {})
        except Exception:
            busy = []
        plans = _plans_without_reason(sess, _session_project(data, sess)) if dev_turn and not busy else []
        if plans and not data.get("stop_hook_active"):
            reasons.append(("plans", "devkit: you changed plan(s) without recording why: " + ", ".join(plans[:4]) +
                            ". Run `dev plan revise <file> --why \"…\"` for each (it versions the plan and logs the "
                            "reason), then finish."))
        # while crew members work, the commits are theirs and the tech lead records them when the run ends (R3): the
        # reminder before the end only (turn_checklist)
        nudge = _handoff_nudge(agent, data, sess) if dev_turn and not busy else None
        if nudge:
            reasons.append(("handoff", nudge))
        _audit_turn_end(agent, data, sess, reasons)
        if reasons:
            out.update({"decision": "block", "reason": " ".join(text for _, text in reasons)})
        else:
            from . import progress
            progress.on_turn_end(agent, sid, sess)
            sess["turn_started"] = None
            ended = True
        _patch_quietly(sf, before, sess)  # a subagent that started meanwhile stays; an ended session stays ended
        if ended:  # the turn is not sent back: keep its prompts only for development work, refresh the checkpoint
            _continuity("on_stop", agent, data, sess=sess, dev_turn=classed)
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


def _audit_turn_end(agent: str, data: dict, sess: dict, reasons: list):
    """R4: each rule that sends the turn back is a `stop.block`; when the turn ends, each item the reminder gave
    this turn (turn_checklist) that the turn then satisfied is a `stop.prevented`. `dev canary` counts them per rule
    and day. Fails open."""
    try:
        told = sess.get("checklist_told") or {}
        told = told.get("rules") or [] if told.get("turn") == sess.get("turn_started") else []
        blocked = [r for r, _ in reasons]
        prevented = [] if blocked else [r for r in told if r not in blocked]
        if not blocked and not prevented:
            return
        from . import audit
        pid = _session_project(data, sess) or sess.get("project") or ""
        ref = f"{agent}:{(data.get('session_id') or '')[:12]}"
        turn = time.strftime("%H:%M", time.localtime(sess.get("turn_started") or time.time()))
        for rule in dict.fromkeys(blocked):
            audit.record("stop.block", pid or ref, f"{rule} · the turn of {turn}", "blocked", rule=rule, project=pid,
                         session=ref)
        for rule in prevented:
            audit.record("stop.prevented", pid or ref, f"{rule} · the turn of {turn}", "ok", rule=rule, project=pid,
                         session=ref)
    except Exception:
        _log(f"stop audit {agent}: {traceback.format_exc()[-300:]}")


def config_change(agent: str):
    """Claude ConfigChange. The user settings: refuse changes to devkit-managed keys, then restore the file. The
    project and local settings (spec C11): refuse a DEVKIT_* variable in their env block and remove it; devkit
    manages nothing else there."""
    data = _stdin_json()
    from . import policy
    path = data.get("file_path") or str(core.CLAUDE_HOME / "settings.json")
    settings = core.read_json(path, None)
    if settings is None:
        return
    project = data.get("source") in ("project_settings", "local_settings")
    drift = [] if project else [d for d in policy.diff_claude(settings)
                                if not d.key.startswith("hooks.") or policy.lock_status() == "off"]
    env_keys = _devkit_env_change(path, settings, managed=not project)
    if not drift and not env_keys:
        return
    ks = sorted({d.key for d in drift} | {f"env.{k}" for k in env_keys})
    keys = ", ".join(ks[:6]) + (f" (+{len(ks) - 6} more)" if len(ks) > 6 else "")
    if not project and core.older_copy():
        # The user settings differ from what this older copy would write, most likely because a newer release was
        # installed. It writes nothing; the pinned release's repair (_spawn runs it) puts back what really drifted
        # from the installed configuration, a tampered DEVKIT_* variable included.
        notice = _older_copy_notice(agent, data.get("session_id") or "", f"{tilde(path)} ({keys})")
        _log(f"config-change left to the pinned release ({agent}): {keys}")
        _spawn(["config", "repair", "--quiet"])
        if env_keys:
            print(f"devkit's DEVKIT_* variables say who calls devkit and how its safeguards behave, so they change "
                  f"only with devkit's configuration (spec C11): refused {', '.join(f'env.{k}' for k in env_keys)}. "
                  f"{notice}".strip(), file=sys.stderr)
            sys.exit(2)
        if notice:
            print(json.dumps({"systemMessage": notice}))
        return
    _log(f"config-change blocked ({agent}): {keys}")
    from . import audit
    audit.record("config.blocked", path, keys, "blocked")
    if project:
        print(f"devkit's DEVKIT_* variables say who calls devkit and how its safeguards behave, so no settings file "
              f"sets them (spec C11): removed {keys} from {path}. The developer changes safeguards in a terminal "
              f"(dev config).", file=sys.stderr)
        sys.exit(2)
    policy._log(f"blocked change to devkit-managed keys: {keys}")
    _spawn(["config", "repair", "--quiet"])
    print(f"devkit manages these settings: {keys}. Change devkit's configuration instead "
          f"(config/claude/settings.json in the devkit repo or ~/.config/devkit/config.json) and run `dev install`.",
          file=sys.stderr)
    sys.exit(2)


def _devkit_env_change(path: str, settings: dict, managed: bool = True) -> list:
    """The DEVKIT_* variables of the settings' env block that differ from what devkit manages (spec C11: set,
    changed or removed), put back to devkit's values in the file. [] when none differ. `managed`: the user
    settings, where devkit manages some; a project's settings get none. A copy older than the pinned release reports
    the user settings' variables but writes nothing: its values may be the older ones (the pinned release repairs).
    A project's settings get every DEVKIT_* variable removed by any copy: that holds in every release."""
    from . import guard, policy
    flat = policy.claude_desired()[0] if managed else {}
    want = {p[1]: json.dumps(v) for p, v in flat.items() if len(p) == 2 and p[0] == "env"
            and str(p[1]).startswith("DEVKIT_")}
    try:
        have = guard.devkit_env(Path(path).read_text())
    except OSError:
        return []
    changed = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
    if changed and (not managed or not core.older_copy()):
        env = {k: v for k, v in (settings.get("env") or {}).items() if not str(k).startswith("DEVKIT_")}
        settings["env"] = {**env, **{k: json.loads(v) for k, v in want.items()}}
        if not settings["env"]:
            settings.pop("env")
        tmp = Path(f"{path}.devkit.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n")
        os.replace(tmp, path)
    return changed


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
    _continuity("on_session_end", agent, data)  # /clear: the clear link, and the old checkpoint's prompts go
    if sid:
        _spawn_sync(["--session", f"{agent}:{sid}", "--finish"])


# --------------------------------------------------------------------------- guard

# Claude Code's Monitor runs its `command` in the same shell as Bash: it is judged, gated and counted as one
SHELL_TOOLS = ("Bash", "Monitor", "exec_command", "shell", "local_shell", "unified_exec")


def _unroutable_lane_command(data: dict, agent: str = "claude"):
    """Inside a subagent, a shell command that runs a dev progress / dev crew call devkit cannot route to the
    subagent's lane would act as the main session: advance the main plan, or record the developer's decision. The
    guard judges what the shell runs (guard.crew_shell_verdict): read-only crew commands and text that only mentions
    lane commands pass; a team change, or `dev crew sync` outside the step's scope, is refused. -> the refusal, or None."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return None
    if _route_subagent_progress(data, agent):
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(shlex.quote(str(c)) for c in cmd) if isinstance(cmd, list) else str(cmd)
    from . import guard
    routed = isinstance((data.get("tool_input") or {}).get("command"), str) and \
        guard.lane_route(cmd, data["agent_id"]) is not None
    return _lane_verdict(data, agent, cmd, routed)


def _lane_verdict(data: dict, agent: str, cmd: str, routed: bool = False):
    """guard.crew_shell_verdict for a subagent's shell command, with its lane's scope, member, tool level and
    repository: the refusal, or None. `routed`: the lane router gives the command's lane calls to the lane."""
    from . import guard
    aid, sid, cwd = data["agent_id"], data.get("session_id") or "-", data.get("cwd") or os.getcwd()
    scope = member = level = base = None  # no lane names no member, so no member folder applies (M124)
    try:
        from . import progress
        run, lane = progress.lane_of(agent, sid, aid)
        if lane:  # the lane's own tool level and the run's repository (spec F7, G13)
            scope, member = progress.lane_scope(run, aid), _member_folder(run, aid)
            level = progress.lane_tools(run, aid)
            base = guard.scope_base((run or {}).get("project"), cwd)
        # the lane's id finds its step grant for a tier-1 push or PR (spec C6, M180)
        v = guard.crew_shell_verdict(cmd, cwd, scope, member, level=level, base=base, lane=aid, session=sid,
                                     agent=agent, routed=routed)
    except Exception as e:  # unchecked is not allowed: the member's command fails closed (the Keymaker's review)
        _log(f"crew-lane {agent} {aid}: {traceback.format_exc()[-300:]}")
        return (f"devkit could not check this command against your lane ({type(e).__name__}), so it is refused "
                f"rather than run unchecked. Run it once more; if it is refused again, ask the tech lead with "
                f"`dev crew ask \"…\"` and name the command.")
    return v[1] if v else None


def _lane_rule(why: str) -> str:
    """The rule a lane command's refusal is recorded under: readonly (the records the read-only allowlist grows
    from), hatch (an escape hatch a member used), else crew-lane."""
    if why.startswith("read-only lane"):
        return "readonly"
    if why.startswith("Crew members do not use devkit's escape hatches"):
        return "hatch"
    return "crew-lane"


def _watch_writes(agent: str, data: dict):
    """Write detection, the backstop of review and read-only lanes (spec F7): before such a lane's shell call, what
    `git status` lists in the run's repository goes into the call's ledger entry; PostToolUse compares
    (_check_writes), so a write an allowed check made (a test run) is seen too."""
    aid = data.get("agent_id")
    if not aid or data.get("tool_name") not in SHELL_TOOLS or _headless():
        return
    from . import guard as g, progress
    run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
    if not lane or progress.lane_tools(run, aid) not in ("review", "read"):
        return
    base = g.scope_base((run or {}).get("project"), data.get("cwd") or os.getcwd())
    before = g.worktree_state(base)
    if before is not None:
        progress.call_note(data, wt_base=base, wt_before=before)


def _check_writes(agent: str, data: dict):
    """PostToolUse of a review or read-only lane's shell call: the paths it wrote (guard.written_paths between the
    states before and after it) go to progress.scope_written. Before the call leaves the ledger."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return
    from . import guard as g
    try:
        from . import progress
        e = progress.call_entry(data)
        if not isinstance(e.get("wt_before"), dict) or not e.get("wt_base"):
            return
        before = {k: tuple(v) if isinstance(v, list) else v for k, v in e["wt_before"].items()}  # JSON keeps lists
        paths = g.written_paths(before, g.worktree_state(e["wt_base"]))
        if paths:
            progress.scope_written(agent, data.get("session_id") or "-", data["agent_id"], paths)
    except Exception as ex:
        g.failopen("scope-write", ex)


def _reconcile(agent: str, data: dict):
    """The reconciler at every hook of the session (spec C7; progress.reconcile_hook). Never raises."""
    if _headless():
        return
    try:
        from . import progress
        progress.reconcile_hook(agent, data)
    except Exception:
        _log(f"reconcile {agent}: {traceback.format_exc()[-300:]}")


def _close_call(data: dict):
    """The call's entry leaves the ledger: it ran (PostToolUse, PostToolUseFailure) or never will (refused)."""
    if data.get("tool_name") in SHELL_TOOLS:
        try:
            from . import progress
            progress.call_closed(data)
        except Exception:
            _log(f"ledger close: {traceback.format_exc()[-300:]}")


def _rewrite(agent: str, updated: dict) -> dict:
    """A PreToolUse that changes the tool's input and approves nothing: Claude Code applies updatedInput without a
    permission decision, and its own permission check then runs on the new input (spec S2, C4)."""
    out = {"hookEventName": "PreToolUse", "updatedInput": updated}
    if agent == "codex":  # S2 was measured on Claude Code; Codex keeps the explicit allow until it is measured there
        out["permissionDecision"] = "allow"
    return out


# What a crew member may not do to the variables that say who calls devkit (spec C4, security consultation §1
# point 5): set, export or unset a DEVKIT_* variable, or run a program with `env -i` or `env -u DEVKIT_*`, anywhere
# the shell runs it (nested shells, eval, command substitutions) or in a program's own code. The call ledger already
# names the member whatever the variables say; this refuses the attempt, so it shows. Text that only mentions a
# variable (a quoted message, a search, a heredoc written to a file) is not a change.
_IDENTITY_CODE = re.compile(
    r"environ\s*\[\s*['\"]DEVKIT_\w*['\"]\s*\]\s*=(?!=)|\bdel\s+(?:os\.)?environ\s*\[\s*['\"]DEVKIT_|"
    r"environ\.(?:pop|setdefault|__delitem__|__setitem__)\s*\(\s*['\"]DEVKIT_|environ\.clear\s*\(|"
    r"environ\.update\s*\([^)]*DEVKIT_|"
    r"\b(?:un)?setenv\s*\(\s*['\"]DEVKIT_|\bputenv\s*\(\s*['\"]DEVKIT_|process\.env\.DEVKIT_\w*\s*=(?!=)|"
    r"\bdelete\s+process\.env\.DEVKIT_|\bENV\s*\[\s*['\"]DEVKIT_\w*['\"]\s*\]\s*=(?!=)|\bENV\.delete\s*\(\s*['\"]"
    r"DEVKIT_|\$ENV\s*\{\s*['\"]?DEVKIT_")
_ASSIGN_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PREFIX_WORDS = ("sudo", "nohup", "command", "exec", "nice", "time", "builtin", "caffeinate", "setsid")


def _prefix_identity(argv0) -> list:
    """The identity changes in the words before a segment's program: VAR=… assignments, and env's -i, -u NAME,
    --unset=NAME and its own assignments."""
    found, i, words = [], 0, list(argv0)
    while i < len(words):
        w, name = words[i], os.path.basename(words[i])
        if _ASSIGN_WORD.match(w):
            found += [w.split("=", 1)[0]] if w.startswith("DEVKIT_") else []
        elif name == "env":
            while i + 1 < len(words) and (words[i + 1].startswith("-") or _ASSIGN_WORD.match(words[i + 1])):
                i += 1
                o, unset = words[i], ""
                if _ASSIGN_WORD.match(o):
                    unset = o.split("=", 1)[0]  # env DEVKIT_X=… sets it: the same change
                elif o in ("-", "-i", "--ignore-environment"):
                    found.append("env -i")
                elif o in ("-u", "--unset"):
                    i += 1
                    unset = words[i] if i < len(words) else ""
                elif o.startswith("--unset="):
                    unset = o.split("=", 1)[1]
                elif o[:1] == "-" and o[1:2] != "-":  # short options together: -i, -iu NAME, -uNAME
                    before, u, after = o[1:].partition("u")
                    if "i" in before:
                        found.append("env -i")
                    if u and not after:
                        i += 1
                        after = words[i] if i < len(words) else ""
                    unset = after if u else ""
                if unset.startswith("DEVKIT_"):
                    found.append(unset)
        elif name not in _PREFIX_WORDS:
            break
        i += 1
    return found


def identity_change(cmd: str, cwd=None) -> list:
    """The DEVKIT_* identity changes a shell command makes, where the shell runs them -> names ([] when none)."""
    from . import guard as g
    cwd = cwd or os.getcwd()
    calls = g.shell_calls(cmd, cwd)
    if calls is None:  # the shell would read it another way than the tokenizer: judge the words
        return ["DEVKIT_*"] if re.search(r"\bDEVKIT_\w*\s*=|\bunset\b[^\n;|&]*\bDEVKIT_|\benv\s+-", cmd) else []
    found, texts = [], [cmd]
    for argv, code in calls:
        name = os.path.basename(argv[0]) if argv else ""
        if code is not None:
            found += ["DEVKIT_* in a program's environment"] if _IDENTITY_CODE.search(code) else []
            continue
        if name == "unset":
            found += [a for a in argv[1:] if a.startswith("DEVKIT_") or "$" in a]
        found += [x for x in g._exports(name, argv[1:]) if x.startswith("DEVKIT_") or x == "?"]
        if name in g._SHELLS:
            texts += [argv[k + 1] for k in range(1, len(argv) - 1) if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", argv[k])]
        elif name == "eval":
            texts.append(" ".join(argv[1:]))
    seen = set()
    while texts and len(seen) < 32:
        t = texts.pop()
        if t in seen:
            continue
        seen.add(t)
        for argv0, _ in g.split_commands(t) or []:
            found += _prefix_identity(argv0)
        texts += g._expansions(t)
    return sorted(set(found))


def _identity_verdict(data: dict):
    """Inside a subagent: ("deny", why) for a shell command that changes who calls devkit; None otherwise."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(shlex.quote(str(c)) for c in cmd) if isinstance(cmd, list) else str(cmd)
    try:
        names = identity_change(cmd, data.get("cwd") or os.getcwd())
    except Exception:
        _log(f"identity check: {traceback.format_exc()[-300:]}")
        names = ["DEVKIT_*"] if "DEVKIT_" in cmd else []  # a crew rule fails closed when the command names one
    if not names:
        return None
    return ("deny", f"This changes {', '.join(names[:4])}: devkit's DEVKIT_* variables say who calls devkit, and a "
                    f"crew member may not set, export or unset them, or run a program with env -i or env -u DEVKIT_* "
                    f"(spec C4). devkit knows who you are from its call ledger, whatever the variables say. Run the "
                    f"command without the change; searching for or quoting the names is fine.")


def _member_folder(run: dict, aid: str) -> str:
    """The id of the crew member whose scratch folder (scratchpad/<id>/) a lane writes in: its member's, for a helper
    the member that launched it; the agent id when the lane names no member."""
    try:
        from . import crew, progress
        chain = progress.lane_chain(run, aid) or [aid]
        head = (run.get("agents") or {}).get(chain[-1]) or {}
        mem = crew.member(head["persona"]) if head.get("persona") else None
        return str((mem or {}).get("id") or aid)
    except Exception:
        return str(aid)


def _route_subagent_progress(data: dict, agent: str = "claude"):
    """Inside a subagent (the hook input carries `agent_id`, in Claude Code and Codex), each `dev progress …` or
    `dev crew ask|answer|inbox|decide|decisions|mode|delegate|decline|route` call of its shell command gets
    DEVKIT_FROM_AGENT, so it updates that subagent's lane (its messages, decisions and delegations come from it, and
    `decide` refuses it) and never advances the main plan (guard.lane_route). A single call is the whole command (a
    quoted heredoc message is data). A compound command (`cd <worktree> && dev progress step "…" | tail -3`) is
    routed only when the lane verdict allows everything else it runs: the rewrite approves nothing but the lane
    calls."""
    aid = data.get("agent_id")
    inp = data.get("tool_input") or {}
    cmd = inp.get("command")
    if not aid or data.get("tool_name") not in SHELL_TOOLS or not isinstance(cmd, str):
        return None
    from . import guard
    new = guard.lane_route(cmd, str(aid))
    if new is None:
        return None
    if not guard.runs_one_command(cmd) and _lane_verdict(data, agent, cmd, routed=True):
        return None  # _unroutable_lane_command gives the refusal
    return {**inp, "command": new}


_HOOK_TEXT = re.compile(r"\bdevkit\.hooks\b|\bfrom\s+devkit\s+import\b[^\n]*\bhooks\b|(?:^|[\s;&|(`'\"])(?:\S*/)?dev\s+"
                        r"hook\s")
_RUNNERS = ("run", "Popen", "call", "check_call", "check_output", "system", "popen", "spawn", "spawnv", "spawnvp",
            "execv", "execvp", "execl", "execlp", "getoutput", "getstatusoutput", "main")


def _runs_dev_hook(cmd: str, cwd: str, depth: int = 0) -> bool:
    """Does a shell command run `dev hook …`, directly, in a nested shell, or from a program an interpreter runs?"""
    from . import guard
    calls = guard.shell_calls(cmd, cwd) if depth < 4 else []
    if calls is None:  # a command the guard cannot parse: judge its text
        return bool(_HOOK_TEXT.search(cmd))
    return any((guard._dev_args(argv) or [None])[0] == "hook" or (code and _program_runs_hooks(code, cwd, depth))
               for argv, code in calls)


def _program_runs_hooks(code: str, cwd: str, depth: int) -> bool:
    """Does a Python program load devkit's hooks (`import devkit.hooks`, `from devkit import hooks`) or run `dev
    hook` (a subprocess or os call, or devkit's cli.main(["hook", …]))? A program that only writes those words, as
    a string to a file, runs nothing. Code that is not Python is judged by its text."""
    import ast
    from . import guard
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return bool(_HOOK_TEXT.search(code))
    for n in ast.walk(tree):
        if isinstance(n, ast.Import) and any(a.name == "devkit.hooks" or a.name.startswith("devkit.hooks.")
                                             for a in n.names):
            return True
        if isinstance(n, ast.ImportFrom) and n.module and (n.module.startswith("devkit.hooks") or (
                n.module == "devkit" and any(a.name == "hooks" for a in n.names))):
            return True
        if not isinstance(n, ast.Call) or not n.args:
            continue
        name = n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        if name not in _RUNNERS:
            continue
        first = n.args[0]
        if isinstance(first, (ast.List, ast.Tuple)):
            argv = [x.value for x in first.elts if isinstance(x, ast.Constant) and isinstance(x.value, str)]
            if (name == "main" and argv[:1] == ["hook"]) or (argv and (guard._dev_args(argv) or [None])[0] == "hook"):
                return True
        elif isinstance(first, ast.Constant) and isinstance(first.value, str) and \
                _runs_dev_hook(first.value, cwd, depth + 1):
            return True
    return False


PITFALL_REPEAT_S = 3600  # a lesson is recalled to the same agent at most once an hour


def _headless() -> bool:
    """devkit's own headless model call, which ai.ask marks in its environment: its hooks only judge tools."""
    return os.environ.get("DEVKIT_HEADLESS") == "1"


def _pitfalls(agent: str, data: dict, found: list, remember: bool = True) -> str:
    """The context line for the lessons a call or its output met, each once an hour per agent of the session."""
    if not found:
        return ""
    from . import lessons
    who, now = data.get("agent_id") or "main", time.time()
    sf = _session_file(agent, data.get("session_id") or "-")
    if remember and sf.exists():
        seen = (state.read(sf) or {}).get("pitfalls_seen") or {}
        found = [x for x in found if now - float(seen.get(f"{who}:{x['meta']['id']}") or 0) > PITFALL_REPEAT_S]
        if not found:
            return ""

        def mark(d):
            kept = {k: v for k, v in (d.get("pitfalls_seen") or {}).items() if now - float(v or 0) < PITFALL_REPEAT_S}
            kept.update({f"{who}:{x['meta']['id']}": now for x in found})
            d["pitfalls_seen"] = kept
        try:
            state.update(sf, mark, create=False)
        except state.StateBusy:
            pass  # audited by state: the lesson may come again, which is the safe side
    return ("devkit · known pitfalls for this call (lessons from earlier mistakes; follow their prevention):\n"
            + "\n".join(lessons.pitfall_lines(found)))


def _project_of(agent: str, data: dict) -> str:
    sf = _session_file(agent, data.get("session_id") or "-")
    return str((state.read(sf) or {}).get("project") or "") if sf.exists() else ""


def _call_pitfalls(agent: str, data: dict, remember: bool = True) -> str:
    """PreToolUse: the open lessons anchored on the command this call runs or the file it writes."""
    try:
        project = _project_of(agent, data)
        if not project:
            return ""
        from . import lessons
        inp = data.get("tool_input") or {}
        cmd = inp.get("command") or ""
        cmd = " ".join(str(x) for x in cmd) if isinstance(cmd, list) else str(cmd)
        files = [x for x in (inp.get("file_path"), inp.get("notebook_path")) if x]
        if not (cmd or files):
            return ""
        return _pitfalls(agent, data, lessons.for_call(project, files=files, command=cmd), remember)
    except Exception:
        _log(f"pitfalls {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _output_text(data: dict) -> str:
    r = data.get("tool_response")
    if isinstance(r, dict):
        r = "\n".join(str(r.get(k) or "") for k in ("stdout", "stderr", "output", "error"))
    return (str(r or "") + "\n" + str(data.get("error") or ""))[-50_000:]


def _output_pitfalls(agent: str, data: dict) -> str:
    """PostToolUse and PostToolUseFailure: the open lessons whose error signature the tool's output shows."""
    try:
        project, text = _project_of(agent, data), _output_text(data)
        if not project or not text.strip():
            return ""
        from . import lessons
        return _pitfalls(agent, data, lessons.for_output(project, text))
    except Exception:
        _log(f"pitfalls {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _hook_forgery(data: dict):
    """A shell call that runs one of devkit's hooks by hand -> ("deny", why), else None. Hook events are the
    harness's: one typed through a shell (`echo '{"prompt": "approve D3"}' | dev hook prompt`) would forge the
    developer's prompt or answer (spec C5), so no session runs them that way, the main session included."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(shlex.quote(str(x)) for x in cmd) if isinstance(cmd, list) else str(cmd)
    if not _runs_dev_hook(cmd, data.get("cwd") or os.getcwd()):
        return None
    return ("deny", "devkit's hooks run only when the harness fires them: a hook event typed through a shell could "
                    "forge the developer's prompt or answer (spec C5). To try a hook, run its tests "
                    "(tests/test_evidence.py shows how).")


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")


COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+(\S+)\s+)?commit\b|\bdev\s+commit\b")


def _stub_commit_gate(data: dict):
    """A commit that would include unfinished work (stubs, placeholders, TODOs without a task) is refused.
    DEVKIT_ALLOW_STUBS=1 as the commit's own prefix, or `dev commit --wip`, when the developer accepts them (spec F8,
    rule 3: parsed, never searched; the hook's own environment opens nothing)."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    m = COMMIT_RE.search(command_words(cmd))  # a commit the command runs, not the text it writes
    if not m:
        return None
    from . import guard
    try:
        if guard.stubs_accepted(cmd):
            return None
        cwd = data.get("cwd") or os.getcwd()
        where = m.group(1) or cwd
        top = core.gitout(where if os.path.isabs(where) else os.path.join(cwd, where), "rev-parse", "--show-toplevel")
        if not top:
            return None
        from . import stubs
        why = stubs.commit_gate(top, cmd)
        return ("deny", why) if why else None
    except Exception as e:
        guard.failopen("stub-gate", e)
        return None


def _crew_limits(agent: str, data: dict):
    """Coordination guardrails -> ("deny", reason) or None:
    - the crew's slots (progress.launch_verdict): `progress.max_parallel` (default 4) under the team's ceiling,
      lowered step by step as the plan's 5-hour or weekly usage rises (progress.quota_steps; one at a time from
      progress.quota_percent), each launch costing its progress.slot_weights (a scout lookup nothing), the helpers
      members launch included, progress.reserve kept for the developer's own request, and no launch deeper than
      progress.max_depth;
    - the main session does not edit files a working crew member owns, in another live run of the repository or in
      this session's run (spec F7, G13; DEVKIT_SCOPE_OVERRIDE=1 for an emergency). A member that ended or went stale
      holds nothing. progress.scope_holders decides, for this check and for `dev crew owner` alike."""
    from . import guard
    try:
        from . import progress
        sid = data.get("session_id") or "-"
        tool = data.get("tool_name")
        main_edit = tool in EDIT_TOOLS and not data.get("agent_id") and not os.environ.get("DEVKIT_SCOPE_OVERRIDE")
        inp = data.get("tool_input") or {}
        path = (inp.get("file_path") or inp.get("notebook_path") or "") if main_edit else ""
        cwd = data.get("cwd") or os.getcwd()
        if path:  # other sessions' members first (a session with no run respects them too), then this session's
            h = progress.scope_holder(path, agent, sid, cwd)
            if h:
                return ("deny", progress.holder_refusal(h, path, cwd))
        if tool not in ("Agent", "Task", "spawn_agent"):
            return None
        run = progress.load_run(agent, sid)
        if not run or run.get("state") == "done":
            return None
        run = progress.normalize(run)
        progress.expire_stale(run)
        why = progress.launch_verdict(agent, data, run)  # at every depth (spec F3): a member's helper takes a slot too
        if why:
            return ("deny", why)
    except Exception as e:
        guard.failopen("crew-limits", e)
    return None


def _repo_top_of(path: str) -> str:
    """The git top level (real path) of the nearest existing folder of `path`; '' outside any repository."""
    d = os.path.dirname(os.path.realpath(path))
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    top = core.gitout(d, "rev-parse", "--show-toplevel") if d else ""
    return os.path.realpath(top) if top else ""


def _same_repository(a: str, b: str) -> bool:
    """Two checkouts of one repository (the main checkout and its worktrees share a git common dir)."""
    def common(x):
        c = core.gitout(x, "rev-parse", "--path-format=absolute", "--git-common-dir")
        return os.path.realpath(c) if c else ""
    ca = common(a)
    return bool(ca) and ca == common(b)


def _scope_violation(agent: str, data: dict):
    """A crew member assigned a scope (dev progress assign … --scope "globs") edits only those files, so parallel
    agents never write the same file, and review-only and read-only members edit nothing. A helper a member
    launches has the member's tool level and scope, never more (spec F3). Every subagent's file and MCP tools are
    judged by guard.member_tool_verdict (spec F8): no sending or publishing, no MCP writes from a review or read-only
    lane or outside the step's scope, no edit of the secret scan's ignore list. Scopes count from the run's
    repository (spec F7, G13). -> the refusal, or None."""
    aid, tool = data.get("agent_id"), str(data.get("tool_name") or "")
    if not aid or not (tool in EDIT_TOOLS or tool.startswith("mcp__")):
        return None
    from . import guard
    try:
        from . import progress
        run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
        cwd = data.get("cwd") or os.getcwd()
        inp = data.get("tool_input") or {}
        top = guard.scope_base((run or {}).get("project"), cwd)
        level = layers = member = None
        if lane:
            level, layers, member = progress.lane_tools(run, aid), progress.lane_scope(run, aid), \
                _member_folder(run, aid)
        if tool != "apply_patch":
            v = guard.member_tool_verdict(tool, inp, cwd, level=level, member=member, scope=layers, base=top)
            if v:
                return v[1]
        if not lane or tool not in EDIT_TOOLS:
            return None
        if lane.get("binding") == "conflict":  # a launch binds once: a second agent claiming it edits nothing
            return ("this agent claims a launch that another agent is bound to, so devkit lets it read but not edit. "
                    "Report what you need to change, and the tech lead checks the launch (dev crew graph).")
        if lane.get("binding") == "pending":  # no launch named it yet: it reads until it binds (spec C3)
            return ("devkit has not matched this agent to the launch that started it yet, so it may read but not "
                    "edit. It binds at its next tool call when its launch is known; if this stays, report what you "
                    "need to change, and the tech lead assigns you your step (dev progress assign).")
        if lane.get("binding") == "unbound":  # its launch record was lost: restricted until Morpheus assigns it
            return ("devkit has no record of the launch that started this agent, so it may read but not edit. Report "
                    "what you need to change; the tech lead can assign you your step (dev progress assign \"<step>\" "
                    "--to \"<you>\"), which lifts this.")
        chain = [run["agents"][k] for k in progress.lane_chain(run, aid)]
        paths = [inp.get("file_path") or inp.get("notebook_path") or ""]
        if tool == "apply_patch" or not paths[0]:
            body = inp.get("input") or inp.get("patch") or inp.get("command") or ""
            paths = re.findall(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", str(body)) or paths
        name = _who(lane.get("persona") or lane.get("label") or "this crew member")
        head = _who(chain[-1].get("persona") or chain[-1].get("label") or "its crew member")
        if len(chain) > 1:  # a helper: the limits it inherits are its member's
            name = f"this helper of {head}"
        for p_ in [x for x in paths if x]:  # each member keeps to its own folder of the session scratchpad
            why = guard.scratch_refusal(os.path.normpath(os.path.join(cwd, p_)), member)
            if why:
                return why
        # its scratch folder and temp files, and this project's plans that its step's scope names (guard rules)
        paths = [p_ for p_ in paths if p_ and not guard.member_write_exempt(p_, cwd, layers, member, base=top)]
        if not paths:
            return None
        if level in ("review", "read"):
            return (f"{name} is {'review-only' if level == 'review' else 'read-only'}"
                    f"{f', like {head}' if len(chain) > 1 else ''}: report the change with file:line and the fix, "
                    f"and the tech lead assigns it. It may write its scratch folder, and this project's plans in the "
                    f"handoff repository when its step's scope names them.")
        if not layers:
            return None
        for p_ in paths:
            ap = os.path.normpath(p_ if os.path.isabs(p_) else os.path.join(cwd, p_))
            rel = guard.scope_rel(ap, cwd, top)
            if rel.startswith(".."):  # a worktree of the same repository: the scope's paths are relative to it
                wt = _repo_top_of(ap)
                if wt and _same_repository(wt, top):
                    rel = os.path.relpath(os.path.realpath(ap), wt)
            if not progress.in_scope(rel, layers):
                return (f"{rel} is outside {name}'s scope ({' and '.join(', '.join(g) for g in layers)}): another "
                        f"crew member may own it. "
                        f"Ask the tech lead to widen it: dev crew ask \"may I change {rel}? why: …\"")
    except Exception as e:
        guard.failopen("crew-scope", e)
        return None
    return None


def _guard_off(agent: str, data: dict):
    """DEVKIT_GUARD=off (spec F8, rule 5): the guard judges nothing, and that is recorded once per session as
    guard.off (the session record's `guard_off`), so the session's next context and the audit trail show it."""
    sid = data.get("session_id") or "-"
    sf = _session_file(agent, sid)
    first = []

    def mark(d):
        if (d or {}).get("guard_off"):
            return state.SKIP
        first.append(True)
        return {**(d or {}), "guard_off": round(time.time())}
    try:
        if sf.exists():
            state.update(sf, mark)
        else:
            first.append(True)  # no session record to remember it in: every call is recorded
    except Exception as e:
        from . import guard as g
        g.failopen("guard-off", e)
    if first:
        from . import audit
        audit.record("guard.off", data.get("tool_name") or "", "DEVKIT_GUARD=off: the guard judges nothing in this "
                     "session", "allow", rule="guard-off", session=sid, tool=data.get("tool_name") or "")


def _irreversible_main(agent: str, data: dict):
    """The main session's irreversible-action gate (spec C6, F5 tiers): a shell command's tier-2 actions, and an MCP
    tool that sends or publishes outside this machine, need the developer's live grant while
    safeguards.irreversible_main enforces. Crew members are judged in their lane (crew_shell_verdict,
    member_tool_verdict). -> ("deny", why) or None; the gate records each action and fails open itself."""
    if data.get("agent_id"):
        return None
    from . import guard as g
    tool, inp = str(data.get("tool_name") or ""), data.get("tool_input") or {}
    cwd, sid = data.get("cwd") or os.getcwd(), data.get("session_id") or ""
    if tool in SHELL_TOOLS:
        cmd = inp.get("command") or ""
        cmd = " ".join(shlex.quote(str(c)) for c in cmd) if isinstance(cmd, list) else str(cmd)
        return g.irreversible_verdict(cmd, cwd, agent=agent, session=sid)
    if tool.startswith("mcp__"):
        return g.irreversible_tool_verdict(tool, inp, cwd, agent=agent, session=sid)
    return None


def _record_bypasses(agent: str, data: dict):
    """An allowed call of the main session that used an escape hatch (spec F8, rule 2): one guard.bypass record per
    use, and the use kept in the session record (`bypasses`), so the next prompt names it."""
    from . import guard as g
    uses = g.last_bypasses()
    if data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS or not uses:
        return
    sid = data.get("session_id") or "-"
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(shlex.quote(str(c)) for c in cmd) if isinstance(cmd, list) else str(cmd)
    try:
        from . import progress
        mode = progress.mode_of(progress.load_run(agent, sid) or {})
    except Exception:
        mode = ""
    g.record_bypasses(cmd, session=sid, lane="main", mode=mode)
    sf = _session_file(agent, sid)
    if not sf.exists():
        return
    now = round(time.time(), 3)  # ms: two uses in one second are two uses

    def keep(d):
        d = d or {}
        d["bypasses"] = (d.get("bypasses") or [])[-19:] + [{"ts": now, "flag": b["flag"], "rule": b["rule"]}
                                                           for b in uses]
        return d
    state.update(sf, keep)


def bypass_context(agent: str, sid: str) -> str:
    """The escape hatches this session used since its last prompt, and DEVKIT_GUARD=off once (spec F8): what the next
    context names, so a use is never silent. Marks them shown."""
    sf = _session_file(agent, sid)
    if not sid or not sf.exists():
        return ""
    lines = []

    def show(d):
        d = d or {}
        seen = d.get("bypasses_shown") or 0
        new = [b for b in d.get("bypasses") or [] if (b.get("ts") or 0) > seen]
        if d.get("guard_off") and not d.get("guard_off_shown"):
            lines.append(f"DEVKIT_GUARD=off at {time.strftime('%H:%M', time.localtime(d['guard_off']))}: the guard "
                         f"judged nothing")
            d["guard_off_shown"] = True
        lines.extend(f"{b['flag']} at {time.strftime('%H:%M', time.localtime(b['ts']))} (it skipped the "
                     f"{b['rule']} rule)" for b in new)
        if not lines:
            return state.SKIP
        d["bypasses_shown"] = max([seen] + [b.get("ts") or 0 for b in new])
        return d
    state.update(sf, show)
    if not lines:
        return ""
    return ("devkit · escape hatches used in this session, each recorded as guard.bypass in the audit trail: you "
            "used " + "; ".join(lines) + ". Say so in your report when it mattered.")


def guard(agent: str):
    if os.environ.get("DEVKIT_GUARD", "").lower() == "off":
        try:
            _guard_off(agent, _stdin_json())
        except Exception:
            _log(f"guard off {agent}: {traceback.format_exc()[-300:]}")
        return
    data = _stdin_json()
    held = {}
    try:
        _guard(agent, data, held)
    except Exception as e:
        if not _guard_failed(agent, data, held, e):
            raise  # run() logs it and records guard.failopen: the call runs


def _guard(agent: str, data: dict, held: dict):
    """The guard hook's judgment of one call. `held` keeps what it has decided so far for _guard_failed: the verdict
    (`verdict`) and whether every rule has run on an allowed call (`done`)."""
    headless = _headless()  # devkit's own headless call: judged, never recorded
    try:
        from . import progress
        if not headless:
            progress.tool_started(data)
    except Exception:
        pass
    _reconcile(agent, data)
    if data.get("agent_id") and not headless:  # late binding (spec D2): this call already runs with its launch's step and scope
        try:
            from . import progress
            progress.bind(agent, data)
        except Exception:
            _log(f"guard bind {agent}: {traceback.format_exc()[-300:]}")
    if data.get("tool_name") in SHELL_TOOLS and not headless:  # the call ledger (spec C4): who makes this call
        try:
            from . import progress
            progress.call_opened(agent, data)
        except Exception:
            _log(f"guard ledger {agent}: {traceback.format_exc()[-300:]}")
    from . import guard as g
    verdict = _hook_forgery(data) or \
        g.check(data.get("tool_name") or "", data.get("tool_input") or {}, data.get("cwd") or os.getcwd(), agent)
    if not verdict:
        verdict = _identity_verdict(data) or _crew_limits(agent, data) or _stub_commit_gate(data) or \
            _irreversible_main(agent, data)
    held["verdict"] = verdict
    held["rule"] = None  # a verdict of the guard's own rules is recorded with no rule of its own
    if verdict and (verdict[0] == "deny" or agent == "codex"):  # Codex turns ask into deny below
        _close_call(data)  # a refused call never runs: it leaves no open entry
    if not verdict:
        if data.get("tool_name") in ("Agent", "Task", "spawn_agent"):  # a subagent launch: its label for progress
            try:
                from . import progress
                progress.note_agent_launch(agent, data)
            except Exception:
                pass
        why = _scope_violation(agent, data)
        if why:
            held["verdict"], held["rule"] = ("deny", why), "crew-scope"
            _log(f"guard {agent} deny: {why[:160]}")
            _close_call(data)
            from . import audit
            audit.record("guard.deny", (data.get("tool_input") or {}).get("file_path") or data.get("tool_name") or "",
                         why[:400], "deny", tool=data.get("tool_name") or "", rule="crew-scope",
                         agent_id=data.get("agent_id") or "")
            held["recorded"] = True  # a failure after this keeps the refusal, and records it no second time
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": _with_pitfalls(agent, data, why)}}))
            return
        routed = _route_subagent_progress(data, agent)
        if routed:
            out = _rewrite(agent, routed)
            held["done"] = True  # judged and routed to its lane: a failure adding its context must not refuse it
            try:
                out = _with_context(agent, out, data)
            except Exception as e:  # without the pitfalls, the call still goes to its lane
                g.failopen("guard-context", e)
            _log(f"progress {agent}: a subagent's dev progress call goes to its lane")
            print(json.dumps({"hookSpecificOutput": out}))
            return
        why = _unroutable_lane_command(data, agent)
        if why:
            held["verdict"], held["rule"] = ("deny", why), _lane_rule(why)
            _close_call(data)
            from . import audit
            audit.record("guard.deny", (data.get("tool_input") or {}).get("command", "")[:200], why, "deny",
                         tool=data.get("tool_name") or "", rule=held["rule"], agent_id=data.get("agent_id") or "")
            held["recorded"] = True
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": _with_pitfalls(agent, data, why)}}))
            return
        held["done"] = True  # every rule has run and allowed the call: a later failure only loses context
        if data.get("tool_name") in ("Agent", "Task"):  # subagent briefs get the skills their work requires
            try:
                from . import skillguard
                sf_ = _session_file(agent, data.get("session_id") or "-")
                sess_ = (core.read_json(sf_, {}) or {}) if sf_.exists() else {}
                updated = skillguard.on_agent_brief(agent, data.get("tool_input") or {}, _session_project(data, sess_))
            except Exception:
                updated = None
            if updated:
                _log(f"skills {agent}: appended required skills and known pitfalls to a subagent brief")
                print(json.dumps({"hookSpecificOutput": _rewrite(agent, updated)}))
                return
        try:
            _record_bypasses(agent, data)
            _watch_writes(agent, data)
        except Exception as e:
            g.failopen("guard-allow", e)
        out = _with_context(agent, {"hookEventName": "PreToolUse"}, data)
        if out.get("additionalContext"):
            print(json.dumps({"hookSpecificOutput": out}))
        return
    decision, reason = verdict
    if agent == "codex" and decision == "ask":
        decision = "deny"
    reason = _with_pitfalls(agent, data, reason)
    _log(f"guard {agent} {decision}: {reason[:160]}")
    from . import audit
    tin = data.get("tool_input") or {}
    audit.record(f"guard.{decision}", tin.get("command") or tin.get("file_path") or data.get("tool_name") or "",
                 reason[:400], decision, tool=data.get("tool_name") or "", agent_id=data.get("agent_id") or "")
    held["recorded"] = True
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


# The guard fails closed for a call that acts (D10 = C, docs/guard.md › Fail-open design): a crash while judging a
# shell command, a file write or patch, or an MCP tool would otherwise skip every rule for that call, and the command
# text is the agent's to shape (26.11.4 failed open on an xargs with no command in a program's code). WebFetch fails
# closed too: its rule keeps secrets out of the URLs it sends (webfetch_verdict). A call that only reads or launches
# (Read, Grep, Glob, WebSearch, and Agent, Task or Codex's spawn_agent: each subagent call passes the guard again)
# still fails open, so a guard bug never blinds the session that has to diagnose it. Kept here, not in guard.py, so it
# holds when guard.py itself cannot load.
_ACTING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch", "WebFetch"})
GUARD_ERROR_REASON = ("devkit's guard could not judge this call (an internal error, {err}, recorded in the audit trail "
                      "as rule guard-error), so it is refused rather than run unchecked. Rewrite it more simply (one "
                      "command per call, or a script file) and try once more; if it is refused again, stop and tell "
                      "the developer: they can run it in their own terminal, where the guard does not apply, and "
                      "`dev doctor` and the hooks log show the error.")


def _acts(data: dict) -> bool:
    """True for a call the guard must judge before it runs: a shell command (any tool input with a command, which
    guard.check reads as one, Monitor's included), a file write or patch, a WebFetch, an MCP tool."""
    tool, inp = str(data.get("tool_name") or ""), data.get("tool_input")
    return tool in SHELL_TOOLS or tool in _ACTING_TOOLS or tool.startswith("mcp__") or \
        (isinstance(inp, dict) and isinstance(inp.get("command"), (str, list)))


def _guard_failed(agent: str, data: dict, held: dict, exc: BaseException) -> bool:
    """The guard hook raised while it handled a call. A deny or ask it had already reached still stands, recorded
    once, under its own rule (`held`: `rule`, and `recorded` once the hook wrote it); a call that acts and was not
    yet judged in full is refused (GUARD_ERROR_REASON) and recorded as guard.deny, rule guard-error.
    -> False when the call may run (judged and allowed, or one that only reads): run() records guard.failopen."""
    verdict = held.get("verdict")
    if verdict and verdict[0] in ("deny", "ask"):
        decision, reason = ("deny" if agent == "codex" else verdict[0]), str(verdict[1])
        rule, detail = held.get("rule"), f"{str(verdict[1])[:300]} (hook error after it: {type(exc).__name__}: {exc})"
    elif held.get("done") or not _acts(data):
        return False
    else:
        decision, reason = "deny", GUARD_ERROR_REASON.format(err=type(exc).__name__)
        rule, detail = "guard-error", f"{reason[:300]} ({type(exc).__name__}: {exc})"[:400]
    _log(f"guard {agent} {decision} (guard-error):\n{traceback.format_exc()}")
    try:
        _close_call(data)  # a refused call never runs: it leaves no open entry
    except Exception:
        pass
    try:
        from . import audit
        tin = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        if not held.get("recorded"):
            audit.record(f"guard.{decision}", str(tin.get("command") or tin.get("file_path") or
                                                  data.get("tool_name") or "")[:400],
                         detail[:400], decision, tool=data.get("tool_name") or "", agent_id=data.get("agent_id") or "",
                         **({"rule": rule} if rule else {}))
    except Exception:
        pass  # the refusal stands without its record
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))
    return True


def _with_pitfalls(agent: str, data: dict, reason: str) -> str:
    """A refusal with the lessons of the call it refuses, every time (a refusal is read when it happens)."""
    known = "" if _headless() else _call_pitfalls(agent, data, remember=False)
    return f"{reason}\n\n{known}" if known else reason


def _with_context(agent: str, out: dict, data: dict) -> dict:
    """A PreToolUse output with the call's pitfalls as context (Claude reads it; Codex gets them with refusals)."""
    if agent != "claude" or _headless():
        return out
    known = _call_pitfalls(agent, data)
    return dict(out, additionalContext=known) if known else out


def _context_out(event: str, text: str):
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def morpheus_hint(text: str, roster=None) -> str:
    """The tech lead's reminder for a development request: declare its kinds of work by their ids from the catalog
    and route each by the crew's RACI. It is the same in every language: the ids decide, never the request's words.
    '' for a request too short to be work ("continue")."""
    t = str(text or "").strip()
    if len(t.split()) < 4 and len(t) < 30:
        return ""
    return (f"devkit · you are the {_lead(roster)}: declare the kinds of work in this request by their ids from the "
            "catalog (`dev crew raci` lists them), whatever language it is in, and route each to its Responsible "
            "member. One kind of work under ten minutes: do it yourself. Otherwise plan one step per kind of work "
            "(`dev progress start --plan`), assign each with its id (`dev progress assign <step> --to <member> "
            "--activity <id>`) and brief the member (`dev crew brief <member> \"<task>\" --activity <id>`); the run "
            "ends when each approver has reviewed.")


def prompt(agent: str):
    """UserPromptSubmit: start the turn clock; answer "how is it going?" from real progress records.

    Safety net: a session without a devkit record never ran SessionStart (hook failure, an untrusted Codex hook,
    an old client). Register it now and inject the full devkit context, so every session works with devkit."""
    from . import progress
    data = _stdin_json()
    sid = data.get("session_id") or ""
    extra = ""
    _reconcile(agent, data)
    if sid and not _session_file(agent, sid).exists():
        cwd = data.get("cwd") or os.getcwd()
        _register_session(agent, sid, cwd, data)
        extra = _with_catalog(context_for(cwd, agent), agent)
        _log(f"prompt {agent}: session {sid[:12]} had no SessionStart; devkit context injected")
    elif sid and agent == "claude" and not (state.read(_session_file(agent, sid)) or {}).get("pid"):
        try:  # a record an earlier prompt made without its process
            _claim_process(agent, sid, _session_file(agent, sid))
        except Exception:
            _log(f"prompt pid {agent}: {traceback.format_exc()[-300:]}")
    raw = (data.get("prompt") or "").lstrip()
    background = _background_told(agent, data)  # a member's completion notice is often this very prompt
    if raw.startswith(("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")) or \
            "<task-notification>" in raw[:400]:
        # background events are not the developer's request
        _context_out("UserPromptSubmit", "\n\n".join(x for x in (extra, background) if x))
        return
    # the prompt waits as pending in the checkpoint until Stop classes the turn; then what waits for the main session
    # (a late compaction's brief, "Not in the summary"), first in the context
    carried = _continuity("on_prompt", agent, data)
    granted = ""
    try:  # the developer's grant lines (spec C5, E2): recorded, and echoed so Morpheus and the developer see them
        granted = progress.evidence_lines(progress.on_human_prompt(agent, data))
    except Exception:
        _log(f"prompt evidence {agent}: {traceback.format_exc()[-300:]}")
    text = progress.on_prompt(agent, data)
    _prompt_refresh(data, force=True)  # an answered decision leaves the prompt at once
    try:
        sf_ = _session_file(agent, sid) if sid else None
        if sf_ and sf_.exists() and (core.read_json(sf_, {}) or {}).get("canary_missed"):
            text = ("devkit · reminder: this session missed the status block before. Your final message (everything "
                    "after your last tool call) opens with **[Firefly> DevKit]** <project> · `<branch>` · <task>, then "
                    "Done / Check / Next (global instructions §0). Nothing above it: put the verdict, a correction or "
                    "a comment on the last command below the block.") + ("\n\n" + text if text else "")
    except Exception:
        pass
    route = ""
    if (core.CFG.get("crew") or {}).get("route_hint", True):
        route = morpheus_hint(data.get("prompt") or "")
    hatches = ""
    try:
        hatches = bypass_context(agent, sid)
    except Exception:
        _log(f"prompt hatches {agent}: {traceback.format_exc()[-300:]}")
    _context_out("UserPromptSubmit", "\n\n".join(x for x in (carried, extra, granted, background, hatches, route, text)
                                                 if x))


DELIVERY = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(commit|push|merge|tag|rebase|cherry-pick|revert)\b|"
                      r"\bgh\s+(pr\s+(create|merge)|release\s+create)\b")


def command_words(cmd: str) -> str:
    """A shell command without its heredoc bodies and quoted strings: what it runs, not the text it writes."""
    out, skip = [], None
    for ln in (cmd or "").splitlines():
        if skip is not None:
            if ln.strip() == skip:
                skip = None
            continue
        m = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", ln)
        if m:
            skip = m.group(1)
        out.append(ln)
    text = "\n".join(out)
    text = re.sub(r"'[^']*'", "''", text)
    return re.sub(r'"(?:\\.|[^"\\])*"', '""', text)


CI_TRIGGER = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b|\bdev\s+push\b|"
                        r"\b(?:gh|dev)\s+pr\s+(?:create|merge|ready)\b|\bdev\s+release\b")


# lesson L-fwyp0n9vzv6: regex rewrites of code and prose changed what they should not, twice
def _bulk_rewrite_note(data: dict) -> str:
    """A shell command that rewrote files with a regex (sed -i, perl -pi, a script with re.sub that writes files):
    the agent reads every changed line before it tests, syncs or commits. The guard judges what the shell runs, so a
    message that only mentions a rewrite gets no note (B-fxn17sez8cj)."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return ""
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(shlex.quote(str(c)) for c in cmd) if isinstance(cmd, list) else str(cmd)
    from . import guard
    if not guard.bulk_rewrite(cmd, data.get("cwd") or os.getcwd()):
        return ""
    return ("A regex just rewrote files. Read every changed line (`git diff -U0 -- <files>`; for untracked files, "
            "compare with what they said before) before you test, sync or commit: a pattern cannot parse nested "
            "expressions or prose, and it changes text it should not. Prefer exact-string edits that must match once.")


# lesson L-fwrw94p19ce: a note written after the last tool call ("waiting for Ghost", a comment on the output) landed
# above the status block, and the Stop hook had to send the turn back. These calls usually end a turn.
LAUNCH_TOOLS = {"Agent": "a subagent launch", "Task": "a subagent launch", "spawn_agent": "a subagent launch",
                "Monitor": "a monitor"}
CLOSING_CMD = re.compile(r"\bdev\s+(?:progress\s+done|handoff\s+--log|sync|task\s+done|push|pr\s+(?:create|merge)|"
                         r"release)\b|\bgit\s+(?:-C\s+\S+\s+)?push\b|\bgh\s+pr\s+(?:create|merge)\b")
CLOSING_GAP = 120  # seconds: parallel launches, or a closing command right after one, get one reminder
COMMIT_CMD = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b|\bdev\s+commit\b")
# what makes the handoff note due at once (R3): the end of the run, a work item closed, a push, a PR, a release. An
# intermediate `dev progress step` is not one: a crew run closes a step every few minutes
MILESTONE_CMD = re.compile(r"\bdev\s+(?:progress\s+done|task\s+done|push|pr\s+(?:create|merge)|release)\b|"
                           r"\bgit\s+(?:-C\s+\S+\s+)?push\b|\bgh\s+(?:pr\s+(?:create|merge)|release\s+create)\b")


def _shell_words(data: dict) -> str:
    if (data.get("tool_name") or "") not in SHELL_TOOLS:
        return ""
    cmd = (data.get("tool_input") or {}).get("command") or ""
    return command_words(" ".join(cmd) if isinstance(cmd, list) else str(cmd))


def note_milestone(data: dict, sess: dict, now: float) -> bool:
    """A main-session command that makes the handoff note due (MILESTONE_CMD) -> recorded in `sess`, the last 20."""
    if data.get("agent_id"):
        return False
    m = MILESTONE_CMD.search(_shell_words(data))
    if not m:
        return False
    sess["milestones"] = (sess.get("milestones") or [])[-19:] + [[round(now), " ".join(m.group(0).split())]]
    return True


def _turn_ending(data: dict) -> str:
    """What makes this tool call a likely end of the turn, in words; '' when nothing does."""
    tool = data.get("tool_name") or ""
    if tool in LAUNCH_TOOLS:
        return LAUNCH_TOOLS[tool]
    if tool not in SHELL_TOOLS:
        return ""
    tin = data.get("tool_input") or {}
    if tin.get("run_in_background"):
        return "a background command"
    cmd = tin.get("command") or ""
    m = CLOSING_CMD.search(command_words(" ".join(cmd) if isinstance(cmd, list) else str(cmd)))
    return f"`{' '.join(m.group(0).split())}`" if m else ""


def closing_reminder(data: dict, sess: dict, now: float, agent: str = "claude") -> str:
    """PostToolUse, main session, development turns: right after a call that usually ends the turn, the status block
    the final message opens with, with this project and branch filled in; there and after every commit, the end of
    turn checklist's open items (turn_checklist), each once per turn (threaded turn endings, R2; on Codex, its shell
    calls, R6). Records in `sess` when it reminded and what it told."""
    what = _turn_ending(data)
    committed = bool(COMMIT_CMD.search(_shell_words(data)))
    if not (what or committed) or data.get("agent_id"):
        return ""
    if sess.get("turn_class") == "other" and os.environ.get("DEVKIT_SCOPE") != "all":
        return ""
    turn = sess.get("turn_started") or 0
    told = sess.get("checklist_told") or {}
    if told.get("turn") != turn:
        told = {"turn": turn, "rules": []}
    items = [(r, t) for r, t in turn_checklist(agent, data, sess) if r not in told["rules"]]
    if items:
        sess["checklist_told"] = {"turn": turn, "rules": told["rules"] + [r for r, _ in items]}
    todo = ("devkit · before your final message: " + " ".join(f"({i}) {t}" for i, (_, t) in enumerate(items, 1))
            if items else "")
    last = sess.get("closing_nudge") or 0
    if not what or (last >= turn and now - last < CLOSING_GAP):
        return todo
    sess["closing_nudge"] = now
    # the repository of the command's folder (the session may have started outside one), else the one it started in
    top = (core.gitout(data["cwd"], "rev-parse", "--show-toplevel") if data.get("cwd") else "") or sess.get("top") or ""
    if top:
        from .registry import main_of
        project = (main_of(Path(top)) or Path(top)).name
    else:
        project = (sess.get("project") or "").rstrip("/").rsplit("/", 1)[-1] or "<project>"
    branch = (core.gitout(top, "branch", "--show-current") if top else "") or "-"
    block = (f"devkit · after {what}: if you end the turn here (to wait, or because the work is done), your final "
             f"message opens with the status block: **[Firefly> DevKit]** {project} · `{branch}` · <task>, then "
             f"Done / Check / Next. Nothing above it: a waiting note or a comment on this output goes below the block. "
             f"Name crew members as <role> (<Name>), such as Backend engineer (Tank), never by the name alone.")
    return "\n\n".join(x for x in (block, todo) if x)


NOTICE_COMMAND_MAX = 80  # characters of a member's command the tech lead's context shows


def _inline(text, limit: int = NOTICE_COMMAND_MAX) -> str:
    """Text a member wrote, as one inert code span of the tech lead's context: backticks dropped (it cannot close
    its span), every run of whitespace, newlines and carriage returns included, one space, at most `limit`
    characters with "…" at the cut. Then no part of it can start a line that reads as devkit's own."""
    s = " ".join(str(text or "").replace("`", "").split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def crew_background_notice(sess: dict) -> str:
    """Main session: the members whose lane closed at SubagentStop while their background work still ran (kept by
    the crew gates, `gates._keep_for_lead`, under the name devkit's launch records give them), each told once:
    Claude Code lists such a member until that work ends. Each command renders as one inert span (`_inline`),
    whatever wrote the record. Removes what it tells from `sess`, one member at a time, so a member kept meanwhile is
    not lost."""
    kept = sess.get("crew_background") or {}
    parts = []
    for aid in list(kept):
        rec = kept.pop(aid) or {}
        tasks = [t for t in rec.get("tasks") or [] if isinstance(t, dict) and t.get("id")]
        if not tasks:
            continue
        ids = ", ".join("`" + str(t["id"]) + "`" for t in tasks)
        cmds = "; ".join("`" + _inline(t.get("command")) + "`" for t in tasks[:2]) + (" …" if len(tasks) > 2 else "")
        parts.append(f"{rec.get('who') or 'a crew member'} task{'s' if len(tasks) > 1 else ''} {ids} ({cmds}); stop "
                     f"it with TaskStop `{aid}` only if you no longer need it")
    if not parts:
        return ""
    return ("devkit · Stopped with background work still running (it may still be working, and may resume when that "
            "work ends): " + ". ".join(parts) + ". Claude Code lists a "
            "member until its background work ends.")


def crew_finished_notice(sess: dict) -> str:
    """Main session: the members it launched that finished (kept by subagent_stop in `sess["crew_finished"]`, only
    for a launch devkit recorded from the main session), one line each, told once. Claude Code has listed a finished
    member as running long after its report arrived, and only TaskStop on its agent id removed it (L-fzh8oq4aby8). A
    member that stopped with background work still running is named by crew_background_notice instead. Removes what
    it tells, one member at a time, like that notice."""
    done = sess.get("crew_finished") or {}
    lines = []
    for aid in list(done):
        who = done.pop(aid) or "A crew member"
        lines.append(f"devkit · {who} finished (agent `{aid}`). If Claude Code still lists it as running, TaskStop "
                     f"`{aid}` ends it.")
    return "\n".join(lines)


def crew_notices(sess: dict) -> str:
    """What the main session hears about its members' lifecycle at its next tool call or prompt: the members that
    finished, and those that stopped with background work still running."""
    return "\n\n".join(x for x in (crew_finished_notice(sess), crew_background_notice(sess)) if x)


def _background_told(agent: str, data: dict) -> str:
    """crew_notices for the main session, taken under the session record's lock (as ciwatch.pending_events takes CI
    results): what it tells leaves the record in the same write, and a busy record tells nothing now and all of it
    at the next hook. A copy read earlier and patched later told a notice again whenever the patch was dropped."""
    if data.get("agent_id"):
        return ""
    try:
        sf = _session_file(agent, data.get("session_id") or "-")
        if not sf.exists():
            return ""
        sess = state.read(sf) or {}
        if not (sess.get("crew_background") or sess.get("crew_finished")):
            return ""  # the usual case, without the lock
        told = []

        def take(s):
            if not (s.get("crew_background") or s.get("crew_finished")):
                return state.SKIP
            told[:] = [crew_notices(s)]
        state.update(sf, take, create=False)
        return told[0] if told else ""
    except state.StateBusy:
        return ""  # audited by state; the notice stays for the next hook
    except Exception:
        _log(f"background notice {agent}: {traceback.format_exc()[-300:]}")
        return ""


def post_tool(agent: str):
    """PostToolUse: time slow calls; remind the agent to update the developer after a long silence; sync the
    handoff repository right after a delivery event (commit, push, merge, tag, PR, release); at a call that usually
    ends the turn, show the status block the final message opens with; in the main session, name the members that
    finished, and those that finished with background work still running (crew_notices)."""
    from . import progress
    data = _stdin_json()
    _check_writes(agent, data)
    _close_call(data)
    _reconcile(agent, data)
    _prompt_refresh(data)
    try:
        if data.get("tool_name") in ("Agent", "Task"):  # the launch names the agent it started: an exact binding
            progress.launch_result(agent, data)
        if data.get("agent_id"):
            progress.bind(agent, data)
    except Exception:
        _log(f"post-tool bind {agent}: {traceback.format_exc()[-300:]}")
    # main: a compaction only the transcript shows (its brief) or "Not in the summary"; a member: its reminder after
    # its own compaction. Each once
    carried = _continuity("on_tool", agent, data)
    answered = ""
    if data.get("tool_name") in progress.QUESTION_TOOLS:  # the developer's answer to a decision (spec C5, E1)
        try:
            answered = progress.evidence_lines(progress.on_question(agent, data))
        except Exception:
            _log(f"post-tool question {agent}: {traceback.format_exc()[-300:]}")
    try:
        cmd = (data.get("tool_input") or {}).get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        words = command_words(cmd)
        if data.get("tool_name") in SHELL_TOOLS and DELIVERY.search(words):
            from . import work
            work.request_sync("delivery: " + DELIVERY.search(words).group(0))
        if data.get("tool_name") in SHELL_TOOLS and CI_TRIGGER.search(words) and not data.get("agent_id"):
            from . import ciwatch  # a push or PR starts CI: watch it, and tell this session how it ended
            # what the command sent to the remote, in the repository it names: a pushed branch or tag, a merge
            # commit on the base branch, a release tag. A local branch or an unpushed commit starts no pipeline
            raw = (data.get("tool_input") or {}).get("command") or ""  # a list stays a list: its shell's script
            for top, ref, sha in ciwatch.targets(raw, data.get("cwd") or os.getcwd()):
                if not core.is_temp(top):
                    ciwatch.start_background(top, session=f"{agent}:{data.get('session_id') or ''}", branch=ref,
                                             sha=sha)
    except Exception:
        pass
    closing = background = ""
    try:
        sf = _session_file(agent, data.get("session_id") or "-")
        if sf.exists():  # only sessions that started with devkit active
            sess = state.read(sf) or {}
            before = copy.deepcopy(sess)
            closing = closing_reminder(data, sess, time.time(), agent=agent)
            if note_milestone(data, sess, time.time()) or sess != before:  # only what changed, on the fresh record:
                _patch_quietly(sf, before, sess)  # a background launch's SubagentStart claims its entry meanwhile
    except Exception:
        _log(f"post-tool closing {agent}: {traceback.format_exc()[-300:]}")
    background = _background_told(agent, data)  # under the lock: told once, nothing while the record is busy
    _context_out("PostToolUse", "\n\n".join(x for x in (carried, answered, background, _output_pitfalls(agent, data),
                                                        _bulk_rewrite_note(data), closing,
                                                        progress.on_tool_done(agent, data)) if x))


def post_tool_failure(agent: str):
    """PostToolUseFailure: a subagent launch that failed (an unknown agent type, an interrupt) starts no agent, so its
    launch record leaves at once: it never labels another agent or holds a crew slot (spec F0, S6). A shell call
    that failed or was interrupted has ended: it leaves the call ledger."""
    from . import progress
    data = _stdin_json()
    _check_writes(agent, data)  # a call that failed may have written too
    _close_call(data)
    if data.get("tool_name") in ("Agent", "Task", "spawn_agent"):
        progress.launch_failed(agent, data)
    _context_out("PostToolUseFailure", _output_pitfalls(agent, data))


def subagent_start(agent: str):
    from . import progress
    data = _stdin_json()
    progress.on_subagent(agent, data, True)
    _make_scratch_folder(agent, data)
    _prompt_refresh(data, force=True)  # a lane opened: the prompt's crew count follows


def _claude_tmp() -> Path:
    """Where Claude Code keeps its per-session folders, scratchpads among them."""
    return Path("/tmp") / f"claude-{os.getuid()}"


def _make_scratch_folder(agent: str, data: dict):
    """Creates a crew member's own folder in the session scratchpad, scratchpad/<member id>/, when its lane opens: the
    brief names it and the guard lets the member write only there, but a read-only member may not create a folder.
    Only when Claude Code made the session's scratchpad (<tmp>/claude-<uid>/<project key>/<session id>/scratchpad),
    and only for a crew member: a helper that names no member gets nothing."""
    try:
        sid, aid = str(data.get("session_id") or ""), str(data.get("agent_id") or "")
        transcript = str(data.get("transcript_path") or "")
        if agent != "claude" or not (sid and aid and transcript):
            return
        pad = _claude_tmp() / Path(transcript).parent.name / sid / "scratchpad"
        if not pad.is_dir():
            return
        from . import progress
        run = progress.load_run(agent, sid) or {}
        member = _member_folder(run, aid)
        if member and member != aid and re.fullmatch(r"[a-z0-9][a-z0-9-]*", member):
            (pad / member).mkdir(exist_ok=True)
    except Exception:
        _log(f"scratch folder {agent}: {traceback.format_exc()[-300:]}")


def _launch_meta(data: dict) -> dict:
    """What launched this subagent: Claude Code keeps `<transcript>.meta.json` (toolUseId, description) next to the
    transcript of every subagent the Agent tool started. {} for the agent's own helpers (compaction, suggestions)."""
    p = str(data.get("agent_transcript_path") or "")
    if not p.endswith(".jsonl"):
        return {}
    meta = core.read_json(Path(p[:-len(".jsonl")] + ".meta.json"), {}) or {}
    return meta if isinstance(meta, dict) and (meta.get("toolUseId") or meta.get("description")) else {}


def _launch_record(sess: dict, meta: dict, aid: str):
    """The launch record devkit wrote for this subagent: the one its meta file names, else the one already bound to
    it; None when there is none or another agent holds it."""
    ls = (sess or {}).get("launches") or {}
    tid = str((meta or {}).get("toolUseId") or "")
    rec = ls.get(tid) if tid else None
    if not isinstance(rec, dict):
        rec = next((v for v in ls.values() if isinstance(v, dict) and v.get("agent_id") == aid), None)
    return rec if isinstance(rec, dict) and rec.get("agent_id") in (None, "", aid) else None


def _launched_by_main(sess: dict, lane, meta: dict, aid: str) -> bool:
    """Do devkit's own records show that the main session launched this subagent? Its lane's parent, which binding
    copies from the launch record; else the launch record the agent's meta file names, or the one already bound to
    it, unless another agent holds that record. A launch description proves nothing: a member writes its helpers'
    descriptions. With no record, False (fail closed)."""
    if lane and lane.get("parent"):
        return lane["parent"] == "main"
    return (_launch_record(sess, meta, aid) or {}).get("parent") == "main"


def _lead_name(sess: dict, run, lane, meta: dict, aid: str, name: str, by_main: bool) -> str:
    """How the tech lead's context names this subagent, from devkit's records only: `name` as a member, when the
    main session launched it (`by_main`, from _launched_by_main); "a helper of <member>" when a member the main
    session launched launched it, that member named from the main session's own launch; "" when the records show
    neither. Never the helper's own type or description: its member chose both."""
    if by_main:
        return _who(name)
    parent = str((lane or {}).get("parent") or (_launch_record(sess, meta, aid) or {}).get("parent") or "")
    if not parent or parent == "main":
        return ""
    plane = ((run or {}).get("agents") or {}).get(parent)
    if not _launched_by_main(sess, plane, {}, parent):
        return ""
    from . import progress
    rec = _launch_record(sess, {}, parent) or {}
    label = (plane or {}).get("label") or rec.get("label") or ""
    boss = (plane or {}).get("persona") or progress.persona(rec.get("type") or "", label) or \
        label.split(":")[0].strip()
    return f"a helper of {_who(boss)}" if boss else ""


def _who(name: str) -> str:
    """A crew member as developers see it, role first ("Backend engineer (Tank)"); the name alone otherwise."""
    try:
        from . import crew
        return crew.who(name)
    except Exception:
        return name


def _lead(roster=None) -> str:
    """The main session as developers read it: "Tech lead (Morpheus)"; "tech lead" when the roster is unreadable."""
    try:
        from . import crew
        lead = crew.who("main", roster)
        return lead if lead != "main" else "tech lead"
    except Exception:
        return "tech lead"


def subagent_stop(agent: str):
    """SubagentStop: a crew member's report must start with its own status block (the canary covers the crew too);
    without it the member is sent back once to write it, and the correction is recorded like the main canary's. Only
    subagents someone launched owe a report (a lane, or the Agent tool's launch record); the agent's own helpers do
    not. Then its lane and step are closed. The compaction summarizer's stop is none of these: it returns at once,
    and nothing is written or bound (spec F2 amendment)."""
    from . import progress
    data = _stdin_json()
    out = None
    try:
        from . import continuity
        if continuity.is_summarizer(agent, data, state.read(_session_file(agent, data.get("session_id") or "-"))):
            return
    except Exception:
        _log(f"subagent-stop summarizer {agent}: {traceback.format_exc()[-300:]}")
    try:
        sid = data.get("session_id") or "-"
        aid = data.get("agent_id") or ""
        if aid:  # an agent that used only tools without hooks binds now, before its report is checked
            try:
                progress.bind(agent, data, resume=False)  # a stop never resumes a finished lane
            except Exception:
                _log(f"subagent-stop bind {agent}: {traceback.format_exc()[-300:]}")
        sf = _session_file(agent, sid)
        sess = (state.read(sf) or {}) if sf.exists() else None
        before = copy.deepcopy(sess)  # the report check reads the transcript: only this hook's changes are written
        if sess is not None and sess.get("turn_class") != "other":
            from . import canary
            run, lane = progress.lane_of(agent, sid, aid)
            meta = _launch_meta(data)
            by_main = _launched_by_main(sess, lane, meta, aid)  # read before a pending lane is dropped
            if lane and lane.get("binding") == "pending":
                lane = None  # never bound to a launch: a helper owes no report
            label = (lane or {}).get("label") or meta.get("description") or ""
            name = (lane or {}).get("persona") or progress.persona(data.get("agent_type") or "", label) \
                or label.split(":")[0].strip() or "Agent"
            pid = (run or {}).get("project") or sess.get("project")
            pending = sess.setdefault("crew_pending", {})
            # the role rule (M346), once per member and joined to the first send-back: a false one cannot loop
            roles = [] if data.get("stop_hook_active") or not (lane or meta) or aid in sess.get("crew_roles", {}) \
                else _unroled(data, "agent_transcript_path")
            also = f" Also, your report {canary.roles_reason(roles)}" if roles else ""
            if roles:
                sess.setdefault("crew_roles", {})[aid] = name
            if not (lane or meta):
                pass  # not launched by anyone: no brief, so no report is owed
            elif data.get("stop_hook_active"):
                first = next((ln.strip() for ln in (data.get("last_assistant_message") or "").splitlines()
                              if ln.strip()), "")
                if aid in pending and canary.MARKER_RE.match(first):
                    canary.record_corrected(agent, sid, pid, f"crew {pending.pop(aid)}")
                    _patch_quietly(sf, before, sess)
            elif not canary.check_subagent(agent, data, sess, pid, name) and \
                    core.CFG.get("canary", {}).get("enforce", True):  # off: measured, not sent back
                from . import audit
                audit.record("canary.crew-miss", pid or "", f"{name}'s report had no status block", "miss")
                pending[aid] = name
                _patch_quietly(sf, before, sess)
                out = {"decision": "block", "reason":
                       f"devkit · end your report with your status block first, exactly: **[Firefly> DevKit · {_who(name)}]** "
                       f"<project> · `<branch>` · <task>, then - **Done:**, - **Check:** (✓/⚠/✗ + evidence) and "
                       f"- **Next:**; then what you deliver and \"Needs from the tech lead:\" (or nothing). Nothing "
                       f"above the block.{also}"}
            elif run and lane and aid not in sess.setdefault("crew_comms", {}) and \
                    progress.lane_unreported(run, aid) and core.CFG.get("canary", {}).get("enforce", True):
                from . import audit  # the crew talks through devkit: the step's evidence goes in the run, live
                audit.record("crew.unreported", pid or "", f"{name} stopped without dev progress step", "miss")
                sess["crew_comms"][aid] = name
                _patch_quietly(sf, before, sess)
                out = {"decision": "block", "reason":
                       f"devkit · {_who(name)}, record your work through devkit before you stop: `dev progress step "
                       f"\"<what finished>\" --check \"<evidence>\"` (it goes to your lane, and the tech lead and the "
                       f"developer see it live). Questions and decisions go through `dev crew ask`, not only in the "
                       f"report. Then end with the same report.{also}"}
            elif roles:
                from . import audit
                audit.record("canary.crew-roles", pid or "", f"{name}'s report named {', '.join(roles)} without "
                                                             f"their role", "miss")
                _patch_quietly(sf, before, sess)
                out = {"decision": "block", "reason": f"devkit · {_who(name)}, your report {canary.roles_reason(roles)} "
                                                      f"Then end with the same report, fixed."}
            if out is None and (lane or meta):  # the crew gates: proofs, skills, stubs, tests, lessons; once
                from . import gates
                ref = copy.deepcopy(sess)
                out = gates.check(name, data, sess, pid,
                                  lead_name=_lead_name(sess, run, lane, meta, aid, name, by_main))
                if out is None and agent == "claude" and aid and by_main and \
                        aid not in (sess.get("crew_background") or {}):
                    # it finished: the tech lead hears it once, with the id Claude Code's TaskStop takes
                    # (crew_finished_notice); only for an agent devkit saw the main session launch, since a member
                    # writes its helpers' labels; Codex has no TaskStop and no such agent list
                    sess.setdefault("crew_finished", {})[aid] = _who(name)
                if sess != ref:  # only the gates' own mark and this one, on the fresh record
                    _patch_quietly(sf, ref, sess)
    except Exception:
        _log(f"subagent-stop {agent}: {traceback.format_exc()[-300:]}")
    if out:
        print(json.dumps(out))
        return
    progress.on_subagent(agent, data, False)
    _continuity("on_subagent_stop", agent, data)  # a last read of its transcript: a compaction no hook saw
    _prompt_refresh(data, force=True)  # a lane closed: the prompt's crew count follows


QUOTA = STATE / "quota"


def _branch_of(path: str):
    """(repository name, branch) by reading .git directly: the status line must not spawn git."""
    d = Path(path or ".").resolve()
    for p in [d, *d.parents]:
        g = p / ".git"
        if g.exists():
            try:
                if g.is_file():  # a worktree: "gitdir: <main>/.git/worktrees/<name>"
                    g = Path(g.read_text().split(":", 1)[1].strip())
                head = (g / "HEAD").read_text().strip()
            except OSError:
                return p.name, ""
            return p.name, head.rsplit("/", 1)[-1] if head.startswith("ref:") else head[:7]
    return d.name, ""


def statusline(agent: str):
    """Claude Code's status line. Records the plan's usage windows (rate_limits: 5-hour and 7-day, the only place
    Claude Code reports them) for doctor, the dashboard and the self-test, then draws one short line:
    project · branch · model · context used · 5h and 7d usage."""
    data = _stdin_json()
    rl = data.get("rate_limits") or {}
    if rl.get("five_hour") or rl.get("seven_day"):
        try:
            f = QUOTA / f"{agent}.json"
            old = core.read_json(f, {}) or {}
            rec = {"ts": int(time.time()), "five_hour": rl.get("five_hour"), "seven_day": rl.get("seven_day")}
            if any(old.get(k) != rec[k] for k in ("five_hour", "seven_day")) or rec["ts"] - (old.get("ts") or 0) > 300:
                QUOTA.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                tmp.write_text(json.dumps(rec))
                os.replace(tmp, f)
        except Exception:
            pass
    ws = data.get("workspace") or {}
    repo, branch = _branch_of(ws.get("current_dir") or data.get("cwd") or os.getcwd())
    model = ((data.get("model") or {}).get("display_name") or "").replace("Claude ", "")
    ctx = (data.get("context_window") or {}).get("used_percentage")
    plain = os.environ.get("NO_COLOR")  # Claude Code renders ANSI colors in the status line
    dim, amber, red, off = ("",) * 4 if plain else ("\033[2m", "\033[38;2;255;179;74m", "\033[31m", "\033[0m")

    def pct(label, v):
        if v is None:
            return None
        v = round(float(v))
        tone = red if v >= 95 else amber if v >= 80 else ""
        return f"{dim}{label}{off} {tone}{v}%{off if tone else ''}"
    parts = [repo + (f" {dim}·{off} {branch}" if branch else ""), model or None, pct("ctx", ctx),
             pct("5h", (rl.get("five_hour") or {}).get("used_percentage")),
             pct("7d", (rl.get("seven_day") or {}).get("used_percentage"))]
    print(f" {dim}·{off} ".join(p for p in parts if p))


HANDLERS = {"session-start": session_start, "stop": stop, "session-end": session_end, "guard": guard,
            "config-change": config_change, "instructions-loaded": instructions_loaded, "prompt": prompt,
            "post-tool": post_tool, "post-tool-failure": post_tool_failure, "subagent-start": subagent_start,
            "subagent-stop": subagent_stop, "statusline": statusline, "pre-compact": pre_compact,
            "post-compact": post_compact}


# --------------------------------------------------------------------------- payload capture (spec C9)

CAPTURES = STATE / "captures"
CAPTURE_KEEP = 20  # captures kept per event (tool events: per event and tool)
CAPTURE_CUT = 200  # characters kept of prompts, tool input and output, the last assistant message and a
                  # compaction's summary
CAPTURE_CUT_KEYS = ("prompt", "tool_input", "tool_response", "last_assistant_message", "compact_summary")
_CAPTURE_TOKEN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}|\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{4,}")
_CAPTURE_SECRET_KEY = re.compile(r"(?i)token|secret|passw|api[_-]?key|authorization|cookie|credential")


def capture_enabled() -> bool:
    return os.environ.get("DEVKIT_HOOK_CAPTURE", "").strip() == "1"


def _scrub(value, cut: bool, key: str = ""):
    """A payload value with tokens removed; strings under a secret-looking key dropped whole; strings under the
    CAPTURE_CUT_KEYS cut to CAPTURE_CUT characters."""
    if isinstance(value, str):
        if _CAPTURE_SECRET_KEY.search(key):
            return "[redacted]"
        from .audit import SECRET
        s = _CAPTURE_TOKEN.sub("[redacted]", SECRET.sub("[redacted]", value))
        return s[:CAPTURE_CUT] + "…" if cut and len(s) > CAPTURE_CUT else s
    if isinstance(value, dict):
        return {k: _scrub(v, cut or k in CAPTURE_CUT_KEYS, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, cut, key) for v in value]
    return value


def capture(event: str, agent: str, data) -> Path | None:
    """Keep this hook payload, redacted, in STATE/captures/<agent>/<event>-<n>.json: the last CAPTURE_KEEP per event,
    and per tool for tool events (`PreToolUse.Agent-3.json`), so a Bash call does not push an Agent launch out.
    Contract tests and spikes are built from these real payloads. The path, or None; never raises."""
    try:
        if not isinstance(data, dict):
            return None
        name = str(data.get("hook_event_name") or event) + (f".{data['tool_name']}" if data.get("tool_name") else "")
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:120]
        d = CAPTURES / agent
        d.mkdir(parents=True, exist_ok=True)
        body = json.dumps(_scrub(data, False), indent=1, sort_keys=True, ensure_ascii=False)
        pat = re.compile(re.escape(name) + r"-(\d+)\.json$")
        for _ in range(5):  # two hooks of one event at once: the second takes the next number
            nums = sorted(int(m.group(1)) for p in d.iterdir() if (m := pat.match(p.name)))
            n = nums[-1] + 1 if nums else 1
            path = d / f"{name}-{n}.json"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(body)
            for old in nums[:max(0, len(nums) + 1 - CAPTURE_KEEP)]:
                (d / f"{name}-{old}.json").unlink(missing_ok=True)
            return path
    except Exception:
        _log(f"capture {event} {agent}: {traceback.format_exc()[-300:]}")
    return None


def _capture_stdin(event: str, agent: str):
    """Read the payload once to capture it, and give the handler the same text on stdin."""
    try:
        raw = sys.stdin.read()
        sys.stdin = io.StringIO(raw)
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {"unparsed": raw[:CAPTURE_CUT]}
        capture(event, agent, data)
    except Exception:
        _log(f"capture {event} {agent}: {traceback.format_exc()[-300:]}")


def run(event: str, agent: str) -> int:
    if capture_enabled():  # DEVKIT_HOOK_CAPTURE=1: unknown events are captured too (spikes register them)
        _capture_stdin(event, agent)
    fn = HANDLERS.get(event)
    if not fn:
        return 0
    if _headless() and event != "guard":
        return 0  # devkit's own headless call (ai.ask): no session, no progress, no gates; the guard still judges
    state.WAIT, state.SAY_BUSY = state.HOOK_WAIT, False  # a hook waits at most 2 s for a record, silently
    try:
        (STATE / "logs").mkdir(parents=True, exist_ok=True)
        fn(agent)
    except SystemExit:
        raise
    except Exception as e:  # fail open: never break the agent session (guard() already refused, in _guard_failed,
        _log(f"{event} {agent} error:\n{traceback.format_exc()}")  # a call that acts and was not judged in full)
        if event == "guard":  # the guard failed open: recorded, so a rule that never runs is seen (spec G12)
            from . import guard as g
            g.failopen("guard-hook", e)
    return 0
