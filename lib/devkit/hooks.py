# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Hook entry points for Claude Code and Codex (`dev hook <event> --agent claude|codex`).

  session-start  inject project context (handoff note, git state, commands) and record the session start
  stop           debounced background refresh of the map + session log (long-lived Desktop/app sessions)
  session-end    final background sync for the session
  pre-tool       before a tool call: the call ledger, lane routing, advice and lessons (never denies or asks; `allow`
                 only on a Codex rewrite of one plain devkit call, or where Codex never asks)

Hooks must be fast and must never break a session: every handler catches all
errors, logs them to ~/.local/state/devkit/logs/hooks.log and exits 0.
"""
from __future__ import annotations

import copy
import hmac
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
    """One line in the hooks log, under its lock (core.file_lock): `dev sleep` trims the log under the same lock,
    so a line is never written into a copy the trim is about to replace. A lock still busy after HOOK_WAIT seconds
    drops the line, as a write error does: logging never fails a hook."""
    try:
        d = STATE / "logs"
        d.mkdir(parents=True, exist_ok=True)
        log = d / "hooks.log"
        with core.file_lock(log, state.HOOK_WAIT) as got:
            if got:
                with log.open("a") as f:
                    f.write(f"{core.fmt_ts(time.time())} {msg}\n")
    except OSError:
        pass


_PAYLOAD = {}  # the payload this hook run read, for run() to name its session once the handler is done


def _stdin_json() -> dict:
    global _PAYLOAD
    try:
        data = sys.stdin.read()
        parsed = core.load_json(data) if data.strip() else {}
    except (ValueError, OSError):  # not JSON, or nested too deep: the hook goes on with no payload, failing open
        parsed = {}
    _PAYLOAD = parsed if isinstance(parsed, dict) else {}
    return parsed


def _swallowed(component: str, exc: BaseException, impact: str = "minor"):
    """An error a hook handler goes on without, recorded as a devkit bug of `impact` (bugs.IMPACT; minor unless it
    stopped the developer's work) once an hour per component (its hook event): bugs.swallowed, which never raises."""
    try:
        from . import bugs
    except Exception:  # the recorder cannot load: the handler goes on as it always did
        return
    bugs.swallowed(component, exc, impact)


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
    # It never carries a call token: a detached worker acts for no call (identity.detached_env).
    from . import identity
    with open(STATE / "logs" / logname, "a") as log:
        subprocess.Popen([sys.executable, "-I", "-S", str(core.release_home() / "bin" / "dev"), *args],  # its shebang
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True,
                         env=identity.detached_env())


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
            s = core.load_json(f.read_text())
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


_SEEN = {}  # cwd -> what context_for inspected there last ("top", "repo"): the welcome reuses that one inspection
# the shapes a field of another Mac's presence must have to reach the model as written: a user name and a hostname
# (or machine id), an agent's name, a git branch name. None holds a space, a quote or a control character.
_PLAIN_WHO = re.compile(r"[A-Za-z0-9._-]{1,40}@[A-Za-z0-9._-]{1,80}")
_PLAIN_AGENT = re.compile(r"[a-z][a-z0-9-]{0,19}")
_PLAIN_BRANCH = re.compile(r"[A-Za-z0-9._/+-]{1,100}")


def _presence_words(t: dict, s_: dict, team: list) -> str:
    """Another Mac's presence as the session-start context names it, `user@hostname (agent, branch)`. Presence is
    another person's text and the model reads this context, so each field shows only in its plain shape (its label
    only when it looks like a hostname, work.display's rule, else its host), else a neutral word: no sentence or
    escape code from a presence file reaches the model."""
    from . import work
    label, host = str(t.get("label") or ""), str(t.get("host") or "")
    who = work.display(f"{t.get('user')}@{label if work._LABEL.fullmatch(label) else host}", team)
    agent, branch = str(s_.get("agent") or ""), str(s_.get("branch") or "")
    return (f"{who if _PLAIN_WHO.fullmatch(who) else 'someone on another Mac'} "
            f"({agent if _PLAIN_AGENT.fullmatch(agent) else 'an agent'}, "
            f"{branch if _PLAIN_BRANCH.fullmatch(branch) else 'a branch'})")


def context_for(cwd: str, agent: str) -> str:
    """The workspace context the model reads at session start. What it inspects stays in _SEEN for the welcome:
    "top" (the checkout's root, "" outside a repository) and "repo" (the project, as registry.inspect_repo gave it);
    and for the hook that knows the session, "lessons": the known pitfalls it shows (_session_lessons_shown)."""
    from . import registry
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    seen = _SEEN[cwd] = {"top": top or ""}
    lines = ["## devkit · workspace context", "",
             "For development work only (code, debugging, tests, builds, deploys, releases, developer tools). If a "
             "request is something else (a recipe, a general question, consulting work with the Firefly consulting "
             "kit), answer it normally: no status block and no devkit process.", ""]
    if top and core.is_temp(top):
        lines.append(f"Scratch repository in a temp dir ({tilde(top)}): not tracked in the handoff repo. "
                     f"Real projects live under {tilde(DEV)}/<owner>/<repo>.")
        return "\n".join(lines)
    if not top:
        reg = core.read_registry({}) or {}
        ps = sorted(reg.get("projects", []), key=lambda p: -(p.get("last_activity") or 0))
        lines.append(f"You are in {tilde(cwd)}, not inside a project.")
        if ps:
            lines.append("Recently active: " + ", ".join(f"{p['id']} ({core.ago(p.get('last_activity'))})" for p in ps[:6]))
        lines.append(f"Projects map: {tilde(HANDOFF / 'PROJECTS.md')}. Work inside a project folder; see the "
                     f"workspace rules in your global instructions.")
        return "\n".join(lines)
    main = registry.main_of(Path(top)) or Path(top)
    p = registry.inspect_repo(main, with_prs=True, refresh_prs=False, pr_max_age=86400)
    seen["repo"] = p
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
    try:  # the branch map: which branches live only on this Mac, built offline
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
    ns = notesmod.summarize(note.read_text()) if note.exists() else None
    if ns:
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
        # an initiative folder whose README cannot be read lists no member (notes.initiatives_of), and hides nothing
        scopes = [p["id"]] + [f"initiatives/{slug}" for slug in _initiatives(p["id"])]
        for sc in scopes:
            for it in work.all_items(sc):
                holder, expired = work.lock_holder(it)
                if holder and not expired:
                    entry = f"{it['meta']['id']} “{it['meta']['title']}” ({sc})"
                    at = work.parse_iso(it["meta"].get("claimed_at")) or None  # a former hostname of this Mac's
                    (mine if work.mine(holder, me, at=at) else theirs).append(f"{entry} — {holder}")
        if mine:
            lines.append("Your claimed tasks: " + "; ".join(x.rsplit(" — ", 1)[0] for x in mine[:6]))
        if theirs:
            lines.append("Claimed by others — do NOT work on these: " + "; ".join(theirs[:6]))
        team = work.team()
        others = [t for t in team  # this Mac's presence under a former hostname is still its own
                  if not work.mine(f"{t['user']}@{t['host']}", me, at=work.parse_iso(t.get("updated")) or None)]
        busy = [_presence_words(t, s_, team) for t in others
                for s_ in t.get("sessions", []) if s_.get("project") == p["id"]]
        if busy:
            lines.append("Also working on this project right now: " + ", ".join(busy[:5]) +
                         ". Coordinate through tasks (claim before you start) and pull before you push.")
    except Exception as e:
        _swallowed("session-start", e)
    try:
        from . import lessons
        inits = _initiatives(p["id"])
        try:  # the lessons of the files this branch changes come first
            from . import testx
            changed = testx.changed_files(top)
        except Exception as e:
            changed = []
            _swallowed("session-start", e)
        known = lessons.context_lines(p["id"], inits, limit=PITFALLS_MAX, files=changed)
        seen["lessons"] = re.findall(r"^Lesson (L-[0-9A-Za-z]{1,40}) \(recorded by ", "\n".join(known), re.M)
        if known:
            lines.append("Known pitfalls (lessons from earlier mistakes; follow their prevention, and `dev lesson recur "
                         "<id> --evidence …` if one happens again):")
            lines.append(lessons.ADVICE)
            lines += ["- " + x for x in known]
        try:  # recall leaves out the lesson files it cannot read: name them, so they are repaired, not forgotten
            bad = [x for sc in [p["id"], *(f"initiatives/{s}" for s in inits), "global"]
                   for x in lessons.unreadable(sc) if not lessons.is_archived(x)]
        except Exception as e:
            bad = []
            _swallowed("session-start", e)
        if bad:
            lines.append(_unreadable_lessons(bad))
    except Exception as e:
        _swallowed("session-start", e)
    try:
        from . import delivery
        dl = delivery.summary_line(p["id"])
        lines.append(dl if dl else "Delivery policy: not recorded yet — `dev delivery` detects and recommends one "
                                   "(`release-policy` skill) before the first release.")
    except Exception as e:
        _swallowed("session-start", e)
    try:  # a set locale fixes the reply language; with auto, each prompt gets its own line (language.prompt_line)
        from . import language
        said = language.session_line()
        if said:
            lines.append(said)
    except Exception as e:
        _swallowed("session-start", e)
    lines.append("Long work: `dev progress start \"<goal>\" --plan \"a; b; c\" --estimate 30m`, then `dev progress step` "
                 "and an update to the developer every few minutes (`communicating` skill: templates for updates, "
                 "decisions, PRs, handovers; records stay in American English, and replies follow the developer's "
                 "language).")
    lines.append("devkit manual: the `using-devkit` skill (session protocol, which tool for which job, project and "
                 "initiative rules, the status line).")
    lines.append(f"Handoff note: {tilde(note)} — its Now tasks and current state are below; read the rest (decisions "
                 f"and gotchas, Next tasks, recent sessions) before starting, and update it with the devkit `handoff` "
                 f"skill before you finish or when the user says handoff/pause/wrap up.")
    lines += ["", "--- handoff note ---", _note_excerpt(ns)]
    return "\n".join(lines)


def _mode_line(agent: str, sid: str) -> str:
    """The session start's one line on the mode decisions follow (progress.mode_shown): this session's live run's, or
    the saved configuration's when it has none. "" when it cannot be read."""
    from . import crew, progress
    try:
        mode, source = progress.mode_shown(progress.load_run(agent, sid) if sid else None)
        return crew.mode_line({"mode": mode, "mode_source": source}).strip() + " (`dev crew mode` shows or changes it)."
    except Exception as e:
        _swallowed("session-start", e)
        return ""


def _asks_first(agent: str, sid: str) -> bool:
    """Whether the model asks this session's waiting decisions first: decisions of this session's own run wait for
    the developer. In guided mode (and with a configuration devkit cannot read, where decisions wait as in guided
    mode) every pending one does; in auto mode only those auto cannot take, one that approves steps or has no
    recommendation. Another session's decisions are asked in that session."""
    from . import progress
    try:
        run = progress.load_run(agent, sid) if sid else None
        waiting = progress.pending_decisions(run) if run else []
        if waiting and progress.mode_shown(run)[0] == "auto":
            waiting = [d for d in waiting if d.get("approves") or not d.get("recommend")]
        return bool(waiting)
    except Exception as e:
        _swallowed("session-start", e)
        return False


def _unreadable_lessons(bad: list) -> str:
    """The session start's line for the lesson files recall cannot read (lessons.unreadable's findings)."""
    n, ids = len(bad), ", ".join(x["id"] for x in bad[:3]) + (" …" if len(bad) > 3 else "")
    return (f"{n} lesson file{'s' if n != 1 else ''} cannot be read, so no recall shows "
            f"{'them' if n != 1 else 'it'}: {ids} (`dev lesson lint` names each with its fix).")


def _session_lessons_shown(data: dict, seen: dict):
    """The known pitfalls the session's context showed (context_for, which counted them) are pending for its main
    agent until a refusal or an output shows their error again (missed) or the session ends (heeded)."""
    try:
        _shown(data, (seen or {}).get("lessons") or [], "session", count=False)
    except Exception as e:
        _swallowed("session-start", e)


def _initiatives(project: str) -> list:
    """The initiatives `project` is a member of, in the handoff the hooks read: their lessons are the project's too,
    at the session start and on every call and output (notes.initiatives_of)."""
    from . import notes
    return notes.initiatives_of(project, HANDOFF)


def _note_excerpt(ns) -> str:
    """The part of the handoff note (its notes.summarize) the session starts with: the Now tasks, then the current
    state, which comes last because a cut to CONTEXT_MAX takes it from its end. The rest stays in the note."""
    if not ns or not (ns["tasks"]["now"] or ns["state"]):
        return "(The note has no Now tasks or current state yet: fill Current state as you learn the project.)"
    now = ns["tasks"]["now"]
    parts = []
    if now:
        parts.append("Tasks, Now:\n" + "\n".join("- " + _cut_task(x) for x in now[:NOW_MAX])
                     + (f"\n- … {len(now) - NOW_MAX} more (`dev task`)" if len(now) > NOW_MAX else ""))
    if ns["state"]:  # never more than the whole context holds
        parts.append("Current state:\n" + ns["state"][:CONTEXT_MAX])
    return "\n\n".join(parts)


def _cut_task(title: str) -> str:
    """A task's title on one line of at most TASK_MAX characters."""
    title = " ".join(title.split())
    return title if len(title) <= TASK_MAX else title[:TASK_MAX - 1].rstrip() + "…"


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
    try:  # the agent's own process is recorded by _register_session (_claim_process)
        from . import progress
        rec_ = state.read(f) or {}
        more = {"transcript": data["transcript_path"]} if data.get("transcript_path") else {}
        if more and any(rec_.get(k) != v for k, v in more.items()):
            state.update(f, lambda d: d.update(more), create=False)
        progress.resume_orphaned(agent, sid)
    except Exception:
        _log(f"session-start pid {agent}: {traceback.format_exc()[-300:]}")
    try:  # the calls a killed session left open end now, with the audit its SessionEnd would have made
        from . import identity
        identity.sweep_gone()
    except Exception as e:
        _swallowed("session-start", e)
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
    except Exception as e:
        _swallowed("session-start", e)
    # An app's host cwd can stay outside the repository after its own run binds this session. Resume and
    # compaction must inject that checkout's context without rewriting the host location in the session record.
    context_cwd = ((state.read(f) or {}).get("top") or cwd) if agent == "codex" else cwd
    _SEEN.pop(context_cwd, None)
    brief = _continuity("on_session_start", agent, data)
    context = _with_catalog(context_for(context_cwd, agent), agent)
    seen = _SEEN.pop(context_cwd, {})  # what context_for inspected, for the welcome
    _session_lessons_shown(data, seen)
    shown = [older] if older else []  # the notices the developer must see when no welcome carries them
    told = [older] if older else []  # the notices the model reads, before the workspace context: a cut keeps them
    said = set()  # the notices this start has: the welcome turns them into need lines
    try:  # devkit notices from files only, no network; the daily update line is the welcome's
        from . import ext
        notices = (("ext_changed", lambda: ext.session_line(cwd), False),  # changed outside devkit, audited once
                   ("ext_accepted", lambda: ext.accepted_line(), True),  # accepted since the developer's last ack
                   # that last ack with its terminal, at every session start for a day after it (ext.ACK_SHOWN_FOR)
                   ("ext_ack", lambda: ext.last_ack_line(), True))
    except Exception:
        notices = ()
        _log(f"session-start notices {agent}: {traceback.format_exc()[-300:]}")
    for key, notice, to_developer in notices:
        try:
            line = notice()
            if line:
                told.append(line)
                said.add(key)
            if line and to_developer:
                shown.append(line)
        except Exception:
            _log(f"session-start notice {agent}: {traceback.format_exc()[-300:]}")
    wanted, hello = _welcome(agent, data, cwd, seen, bool(older), said)
    seen_note = ""
    if hello:  # first of all, in the part of the context a preview keeps when the whole is too long
        from . import welcome
        seen_note = welcome.for_model(hello, _asks_first(agent, str(data.get("session_id") or "")))
    else:  # no welcome to show it (and mark the day only then): the model hears the daily update line, as before
        try:
            from . import updatecheck
            line = updatecheck.session_line(installed=updatecheck.installed_version())
            told += [line] if line else []
        except Exception:
            _log(f"session-start update notice {agent}: {traceback.format_exc()[-300:]}")
    # what matters most first: a cut to CONTEXT_MAX takes the handoff note's excerpt, then the catalog, from the end
    text = _fit("\n\n".join(x for x in (seen_note, brief, *told, _mode_line(agent, str(data.get("session_id") or "")),
                                         context) if x))
    # the developer reads the welcome (Claude Code at the session's top, Codex above its first reply); without one,
    # the notices themselves. A headless run, a crew member's session and a scratch repository get neither.
    message = hello or ("\n\n".join(shown) if wanted else "")
    if agent == "codex" or message:
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        print(json.dumps(dict({"systemMessage": message}, **out) if message else out))
    else:
        print(text)


def _welcome(agent: str, data: dict, cwd: str, seen: dict, older: bool, said: set) -> tuple:
    """-> (whether this start gets a welcome, the welcome or ""). Not wanted for a headless run, a crew member's
    session or a scratch repository (welcome.wanted). "" when context_for did not say where the session is (then
    the notices show as before), and on any error: the hook never fails for it."""
    try:
        from . import welcome
        from .canary import surface
        if not welcome.wanted(agent, data, seen.get("top", "")):
            return False, ""
        if "top" not in seen:
            return True, ""
        return True, welcome.text(agent, str(data.get("source") or "startup"), cwd, data=data,
                                  repo=seen.get("repo"), top=seen["top"], older=older,
                                  ext_changed="ext_changed" in said, ext_ack="ext_ack" in said,
                                  mode=welcome.mode_for(surface(agent, data)))
    except Exception:
        _log(f"session-start welcome {agent}: {traceback.format_exc()[-300:]}")
        return True, ""


def _continuity(fn: str, agent: str, data: dict, **kw):
    """Call a continuity hook entry point. Each fails open itself; this also keeps an import error from
    reaching the hook. -> its text ("" on any error)."""
    try:
        from . import continuity
        return getattr(continuity, fn)(agent, data, **kw) or ""
    except Exception:
        _log(f"continuity {fn} {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _prompt_refresh(data: dict, force: bool = False):
    """The shell prompt's facts for the event's folder: a few file reads, no git, network or subprocess.
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
    if agent == "codex":  # a request can arrive after the last tool result, just before compaction
        try:
            from . import progress
            progress.note_midturn(agent, data)
        except Exception as e:
            _swallowed("pre-compact-requests", e)
    text = _continuity("on_pre_compact", agent, data)
    if text and agent == "claude" and not data.get("agent_id"):
        print(text)


def post_compact(agent: str):
    """PostCompact: closes the compaction and keeps "Not in the summary" for the next hook. Prints nothing: the
    output of this event is display only, and the summary is compared, never re-injected."""
    _continuity("on_post_compact", agent, _stdin_json())


CONTEXT_MAX = 9000  # UTF-8 bytes of the context a session starts with: Claude Code cuts a hook's output past 10,000
# characters to a 2,000-character preview, and Codex past about 2,500 tokens, which it counts as 4 bytes each
PITFALLS_MAX = 3  # known pitfalls in that context, the most relevant first
NOW_MAX = 5  # Now tasks of the handoff note in that context, each on one line of at most TASK_MAX characters: the
TASK_MAX = 160  # note's current state comes after them and keeps its room
CUT_LINE = "… cut here to keep devkit's context under the hooks' size limit: the handoff note has the rest."
CATALOG_MAX = 8000  # characters of the catalog's index at most (about 2,300 with devkit's 34 kinds of work)


def _fit(text: str, limit: int = CONTEXT_MAX) -> str:
    """`text` cut at whole lines to `limit` UTF-8 bytes, from its end, with CUT_LINE last; as it is when it fits.
    The session's context puts what matters most first, so the cut takes the handoff note's excerpt first."""
    if len(text.encode("utf-8")) <= limit:
        return text
    room = limit - len(CUT_LINE.encode("utf-8")) - 1
    out, size = [], 0
    for ln in text.split("\n"):
        n = len(ln.encode("utf-8")) + 1
        if size + n > room:
            if not out:  # one line longer than the limit: its start
                out.append(ln.encode("utf-8")[:room].decode("utf-8", "ignore"))
            break
        out.append(ln)
        size += n
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out + [CUT_LINE])


def catalog_context(agent: str) -> str:
    """The catalog of ids the model declares, as an index: every kind of work by its id and name, then where the rest
    is (who does and approves each one, the crew's rules, the skills each one requires), the same for every agent.
    Cut at whole kinds of work to CATALOG_MAX, keeping its last lines."""
    try:
        from . import crew, raci, skillguard
        kinds = [f"- `{a['id']}` {a['name']}" for a in raci.activities(crew.load())]
        rules = tilde(skillguard.RULES_FILE)
    except Exception:
        _log(f"catalog {agent}: {traceback.format_exc()[-300:]}")
        return ""
    head = ["devkit catalog · declare these ids, whatever language the developer writes in; devkit enforces on them "
            "and never on words.",
            "Kinds of work: one per plan step (`[id]` in a plan spec, as in `api=1h after design [backend]`, or "
            "`--activity <id>` when you assign it) and per brief (`--activity <id>`). Without one, a step takes its "
            "member's Responsible kind of work, then its files'."]
    tail = ['Lesson anchors, where a lesson applies: dev lesson add "…" --happened "…" --cause "…" --prevent "…" '
            '--file <glob> '
            '--command "<command words>" --activity <id> --error "<output text>" --stack <name>.',
            f"The rest: who does and who approves each kind of work, `dev crew raci [<id>]`; every crew rule and the "
            f"check that enforces it, `dev crew rules`; the skills each kind of work, file and command requires, "
            f"{rules} (a brief carries them, and the Stop hook checks them)."]
    room = CATALOG_MAX - len("\n".join(head + tail)) - 1
    shown = []
    for i, ln in enumerate(kinds):
        more = f"- … {len(kinds) - i} more: `dev crew raci`"
        if len(ln) + 1 + (len(more) + 1 if i < len(kinds) - 1 else 0) > room:
            shown.append(more)
            break
        shown.append(ln)
        room -= len(ln) + 1
    return "\n".join(head + shown + tail)


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
    work.replace_session_line(pid, key, line)  # Stop runs repeatedly: one line per session, under the log's lock


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


HANDOFF_BLOCK = {"block_after_commits": 5, "block_after_minutes": 60}  # handoff.*


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
    only when it matters: handoff.block_after_commits of them (5), the oldest older than
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
            work.mine(i["meta"].get("claimed_by") or "", a, at=work.parse_iso(i["meta"].get("claimed_at")) or None)]
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


PLAN_WRITES_KEPT = 200  # plan files a session record remembers writing, newest last
PLAN_WRITE_SLACK = 1.0  # seconds a plan's change may precede its shell call's ledger entry (file time resolution)


def _plans_without_reason(sess: dict) -> list:
    """The plan and spec files this session's own tool calls wrote (its record's `plans_written`, _note_plan_writes)
    that still hold, without `dev plan revise`, the text the session's last call left in them
    (plans.unrevised_change_of): a change a pull of the handoff repository or another session made since is not this
    session's. Each one counts in the project folder it is in, whatever folder the session works in now. A plan
    another session changed, in this project or another, is never on the
    list, even for a session outside any project: revising it would break the one-project rule and disturb the session
    that works there."""
    from . import plans as plansmod
    out = []
    for rel, left in (sess.get("plans_written") or {}).items():
        f = HANDOFF / rel
        if f.is_file() and plansmod.unrevised_change_of(f, left):
            out.append(rel)
    return sorted(out)


def _plan_rel(path) -> str:
    """A file's path in the handoff repository when it is a plan or spec (plans/<project>/…/<name>.md), else ""."""
    try:
        rel = Path(os.path.realpath(os.path.expanduser(str(path)))).relative_to(os.path.realpath(HANDOFF))
    except (ValueError, OSError):
        return ""
    return rel.as_posix() if rel.parts[:1] == ("plans",) and rel.suffix == ".md" else ""


def _plans_written_by(data: dict, failed: bool = False) -> list:
    """The plan files a finished tool call wrote, as paths in the handoff repository. An edit call: the files it names
    (progress._edit_paths), unless it failed. A shell call that may write (its call-ledger entry has no `reads`
    verdict): each plan that changed between that entry and now and whose file name the command names. A plan that
    changed during the call without being named was someone else's (another session, a pull of the handoff
    repository). Read before the call leaves the ledger."""
    from . import progress
    if data.get("tool_name") not in SHELL_TOOLS:
        return [] if failed else [r for r in map(_plan_rel, progress._edit_paths(data)) if r]
    text = "\n".join(_shell_texts(data.get("tool_input")))  # quotes and heredocs kept: a plan's path is often quoted
    pdir = HANDOFF / "plans"
    if ".md" not in text or not pdir.is_dir():
        return []
    entry = progress.call_entry(data)
    if not entry.get("ts") or entry.get("reads"):
        return []
    start, end = float(entry["ts"]) - PLAN_WRITE_SLACK, time.time() + PLAN_WRITE_SLACK
    out = []
    for f in pdir.rglob("*.md"):
        if not re.search(r"(?<![\w.-])" + re.escape(f.name) + r"(?![\w.-])", text):
            continue
        try:
            if start <= f.stat().st_mtime <= end:
                out.append(f.relative_to(HANDOFF).as_posix())
        except OSError:
            continue
    return out


def _note_plan_writes(agent: str, data: dict, failed: bool = False):
    """PostToolUse or PostToolUseFailure: the session record keeps the plan files this call wrote
    (_plans_written_by), each with the text the call left in it (plans.mark), the only ones the plan check at the end
    of the turn answers for. Fails open: a busy record drops this one note (state audits it)."""
    try:
        found = _plans_written_by(data, failed)
        sf = _session_file(agent, data.get("session_id") or "-")
        if not found or not sf.exists():
            return
        from . import plans as plansmod
        left = {rel: plansmod.mark(HANDOFF / rel) for rel in found}

        def add(d):
            kept = {k: v for k, v in (d.get("plans_written") or {}).items() if k not in found}
            kept.update(left)
            d["plans_written"] = dict(list(kept.items())[-PLAN_WRITES_KEPT:])
        state.update(sf, add, create=False)
    except state.StateBusy:
        pass
    except Exception:
        _log(f"plan writes {agent}: {traceback.format_exc()[-300:]}")


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
    """The crew members a final message's prose names outside `<role> (<Name>)` (the developer's role rule,
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


def _ci_watch_gate(agent: str, data: dict, defer: bool = False):
    """A turn that sent work to origin and leaves its unfinished CI run with nothing watching it that wakes this agent
    goes back with the command to start (ciwatch.stop_gate); with `defer`, a run devkit's background watch follows is
    left to the one wait. The turn's stop.block names the rule. Fails open."""
    try:
        from . import ciwatch
        return ciwatch.stop_gate(agent, data, defer=defer)
    except Exception:
        _log(f"stop ci watch: {traceback.format_exc()[-300:]}")
        return None


def _wait_gate(agent: str, data: dict, ci_apart: bool = False):
    """A main-session turn that ends while events can still arrive (a member works, a launch is pending, a step waits
    for a slot, CI it sent has not finished) and leaves nothing armed that wakes it goes back once with the wait to
    arm (watch.stop_gate); the turn's stop.block names the rule. Fails open."""
    try:
        from . import watch
        return watch.stop_gate(agent, data, ci_apart=ci_apart)
    except Exception:
        _log(f"stop wait: {traceback.format_exc()[-300:]}")
        return None


def _watch_reasons(agent: str, data: dict) -> list:
    """The two wake rules as one instruction -> [(rule, text)]. When the one wait is asked, a run devkit's background
    watch follows is left to it, so the CI rule names only a run nothing follows; and when the CI rule asks for its
    own watch, the wait does not claim to cover that run."""
    wait = _wait_gate(agent, data)
    ci = _ci_watch_gate(agent, data, defer=bool(wait))
    if ci and wait:
        wait = _wait_gate(agent, data, ci_apart=True)
    return [(rule, why) for rule, why in (("watch", ci), ("wait", wait)) if why]


def turn_checklist(agent: str, data: dict, sess: dict) -> list:
    """What the Stop gate would ask at this point of the turn, so it reaches the agent before the end
    -> [(rule, text)] still open: handoff (the developer's commits newer than the project's note),
    skills (required skills not loaded for the files changed), tests (tests the turn edited that cannot fail), stubs
    (unfinished work without a task). The Stop gate's own functions; nothing is marked as asked, each fails open.
    The status block is in every closing reminder."""
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
    for rule, why in _watch_reasons(agent, dict(data, stop_hook_active=False)):  # after a push, members at work
        items.append((rule, why.replace("devkit: ", "", 1)))
    if agent == "claude" and not data.get("agent_id") and not any(rule in ("watch", "wait") for rule, _ in items):
        try:  # other sessions on this Mac and nothing armed: a note from one would wait for the developer's next message
            from . import peers
            hint = peers.standby_hint(agent, data.get("session_id") or "")
            if hint:
                items.append(("standby", hint))
        except Exception:
            _log(f"checklist standby: {traceback.format_exc()[-300:]}")
    return items


def stop(agent: str):
    data = _stdin_json()
    from . import canary
    if agent == "codex":  # the turn's commands whose rollout record came after their PostToolUse get their ledger line
        try:  # only the line: Stop cannot give the agent the root-cause paragraph, so the next failure counts it
            _codex_settle(agent, data, loop=False)
        except Exception as e:
            _swallowed("stop", e)
    sid = data.get("session_id") or "-"
    sf = _session_file(agent, sid)
    out = {}
    if sf.exists():  # only sessions that started with devkit active (they loaded the global instructions)
        _prompt_refresh(data, force=True)
        sess = state.read(sf) or {}
        before = copy.deepcopy(sess)  # the gates below take seconds: only what this hook changes is written back
        # reasons: (rule, text) that send the turn back, the rule for the audit; notes: (rule, text) of the
        # presentation rules (status, roles, progress, language), which never send a turn back: the developer has
        # already read the reply, so the miss is recorded and reminded at the next prompt (canary.reminder), or joins
        # a send-back for more work
        reasons, notes, ended = [], [], False
        classed = None  # what continuity keeps the turn's prompts by: None when the classifier failed
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
                canary.save_status(pid, agent, line, branch or sess.get("branch"), path=top_ or None,
                                   session=data.get("session_id") or None)
            elif not ok:
                from . import audit
                audit.record("canary.miss", pid or "", "final message without the status block", "miss")
                sess["canary_missed"] = True  # later prompts in this session get a reminder
                if agent == "claude":
                    out["systemMessage"] = canary.miss_message(agent, sid)
                if core.CFG.get("canary", {}).get("enforce", True):
                    text_ = data.get("last_assistant_message") or canary.last_assistant_text(
                        data.get("transcript_path")) or ""
                    notes.append(("status", canary.status_reason(text_)))
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
                    canary.save_status(pid_, agent, line, core.gitout(top, "branch", "--show-current"), path=top,
                                       session=data.get("session_id") or None)
                    if sess.get("canary_missed"):
                        canary.record_corrected(agent, sid, pid_, line)
        if dev_turn and not data.get("stop_hook_active") and sess.get("roles_told", _UNSET) != sess.get("turn_started"):
            names = _unroled(data, "transcript_path")  # the role rule, once per turn
            if names:
                sess["roles_told"] = sess.get("turn_started")
                notes.append(("roles", f"devkit: your final message {canary.roles_reason(names)}"))
        why = canary.progress_gate(agent, data, sess) if dev_turn else ""  # the Progress rule, once per turn
        if why:
            notes.append(("progress", why))
        why = canary.language_gate(agent, data, sess) if dev_turn else ""  # the reply language, once per turn
        if why:
            notes.append(("language", why))
        why = canary.you_gate(agent, data, sess) if dev_turn else ""  # a decision asked of the developer in auto
        if why:
            notes.append(("you", why))
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
            reasons += _watch_reasons(agent, data)  # members or CI can still answer: what wakes the idle session
        try:  # lessons the reply names were applied: that use keeps them alive
            from . import lessons as _les
            said = data.get("last_assistant_message") or ""
            ids = sorted(set(re.findall(r"\bL-[0-9a-z]{9,}\b", said)))
            if ids:
                _les.track(ids, "applied")
        except Exception as e:
            _swallowed("stop", e)
        if dev_turn and not data.get("stop_hook_active") and not sess.get("lesson_nudged") and sess.get("top"):
            try:
                from . import lessons
                fixes = lessons.fixed_in_session(sess["top"], sess.get("head"))
                if fixes:
                    sess["lesson_nudged"] = True
                    reasons.append(("lesson", _lesson_nudge(fixes)))
            except Exception:
                _log(f"stop lessons {agent}: {traceback.format_exc()[-300:]}")
        try:  # crew members may be writing plans right now; their own `dev plan revise` records them
            from . import progress as _pg
            busy = _pg.crew_working(_pg.load_run(agent, sid) or {})
        except Exception:
            busy = []
        plans = _plans_without_reason(sess) if dev_turn and not busy else []
        if plans and not data.get("stop_hook_active"):
            reasons.append(("plans", "devkit: you changed plan(s) without recording why: " + ", ".join(plans[:4]) +
                            ". Run `dev plan revise <file> --why \"…\"` for each (it versions the plan and logs the "
                            "reason), then finish."))
        # while crew members work, the commits are theirs and the tech lead records them when the run ends: the
        # reminder before the end only (turn_checklist)
        nudge = _handoff_nudge(agent, data, sess) if dev_turn and not busy else None
        if nudge:
            reasons.append(("handoff", nudge))
        _audit_turn_end(agent, data, sess, reasons, notes)
        if reasons:  # more work: the agent writes again, so the presentation misses ride along, told once
            said = " ".join(text for _, text in reasons)
            if notes:
                said += " Also, in that message: " + " ".join(t.replace("devkit: ", "", 1) for _, t in notes)
            out.update({"decision": "block", "reason": f"{said} {canary.NO_REPEAT}"})
        else:
            if notes or sess.get("presentation_due"):  # the next prompt's context reminds them (_presentation_due)
                due = _presentation_kept(sess.get("presentation_due"), notes, dev_turn)
                if due:
                    sess["presentation_due"] = due
                else:
                    sess.pop("presentation_due", None)
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


def _audit_turn_end(agent: str, data: dict, sess: dict, reasons: list, notes: list = ()):
    """Each rule that sends the turn back is a `stop.block`; when the turn ends, each item the reminder gave
    this turn (turn_checklist) that the turn then satisfied is a `stop.prevented`; each presentation rule the final
    message missed is a `canary.remind`, `recorded` when the next prompt reminds it and `blocked` when it joined a
    send-back for other work (it never sends a turn back of its own). `dev canary` counts them per rule and day. Fails
    open."""
    try:
        told = sess.get("checklist_told") or {}
        told = told.get("rules") or [] if told.get("turn") == sess.get("turn_started") else []
        blocked = [r for r, _ in reasons]
        prevented = [] if blocked else [r for r in told if r not in blocked]
        reminded = list(dict.fromkeys(r for r, _ in notes))
        if not blocked and not prevented and not reminded:
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
        for rule in reminded:  # told in the send-back's reason, or left for the next prompt's reminder
            audit.record("canary.remind", pid or ref, f"{rule} · the turn of {turn}",
                         "blocked" if blocked else "recorded", rule=rule, project=pid, session=ref)
    except Exception:
        _log(f"stop audit {agent}: {traceback.format_exc()[-300:]}")


_UNREAD = object()  # config_change: a settings file core.read_json could not read


def _quietly(component: str, fn, *args):
    """fn(*args) for a step a refusal goes on without (a log line, the background repair): its error recorded
    (_swallowed), never raised. -> its value, or None when it failed."""
    try:
        return fn(*args)
    except Exception as e:
        _swallowed(component, e)
        return None


def _not_settings(value) -> str:
    """What JSON that is not an object is, in the words of a refusal (config_change)."""
    kind = ("JSON null" if value is None else "JSON true or false" if isinstance(value, bool) else
            "a JSON number" if isinstance(value, (int, float)) else "a JSON string" if isinstance(value, str) else
            "a JSON array")
    return f"it is {kind}, not a settings object"


def config_change(agent: str):
    """Claude ConfigChange. The user settings: refuse changes to devkit-managed keys, then restore the file. The
    project and local settings: refuse a DEVKIT_* variable in their env block and remove it, and refuse a
    disableAllHooks that is not false, left in the file (the repository's; dev doctor names it); devkit manages
    nothing else there. Exit 2 refuses; hooks.run turns any exception into exit 0, which would let the change pass, so
    an error of the check itself refuses the change too, recorded as a devkit bug that blocked the developer."""
    try:
        _config_change(agent)
    except SystemExit:
        raise
    except Exception as e:
        _swallowed("config-change", e, "blocked")
        what = str(_PAYLOAD.get("file_path") or "settings")
        _quietly("config-change", _log, f"config-change blocked, its check failed: {type(e).__name__}")
        _quietly("config-change", _audit_blocked, what, f"failed: {type(e).__name__}")
        print(f"devkit's check of this settings change failed ({type(e).__name__}), so it cannot tell whether the "
              f"change turns off what devkit manages: refused the change. The file still holds the change, and a new "
              f"session reads it: undo the change if you did not mean it, and run dev bug report so devkit's failure "
              f"gets fixed.", file=sys.stderr)
        sys.exit(2)


def _audit_blocked(target: str, detail: str):
    from . import audit
    audit.record("config.blocked", target, detail, "blocked")


def _config_change(agent: str):
    data = _stdin_json()
    from . import policy
    path = data.get("file_path") or str(core.CLAUDE_HOME / "settings.json")
    project = data.get("source") in ("project_settings", "local_settings")
    # Claude Code reads its settings with Node's JSON.parse, which refuses NaN and Infinity: read as Python reads them,
    # they would pass a check of a file Claude Code then reads differently
    settings = core.read_json(path, _UNREAD, allow_nan=False)
    if settings is _UNREAD and not project and not os.path.lexists(path):
        settings = {}  # no user settings: none of devkit's managed keys, checked as an empty file is
    if settings is _UNREAD:
        _refuse_unreadable(path, project)
        return
    if not project and not isinstance(settings, dict):
        _refuse_unchecked(path, project, _not_settings(settings))
    try:
        drift = [] if project else [d for d in policy.diff_claude(settings)
                                    if not d.key.startswith("hooks.") or policy.lock_status() == "off"]
        env_keys, fixed = _devkit_env_fix(path, settings, managed=not project)
        # project and local settings: every hook off, devkit's too (the user settings' is drift, policy.diff_claude)
        off = settings.get("disableAllHooks", False) if project and isinstance(settings, dict) else False
    except Exception as e:  # a hook that crashed exits without blocking: the change would pass unchecked
        _refuse_unchecked(path, project, type(e).__name__)
    if not drift and not env_keys and off is False:
        return
    unwritten = ""
    if fixed is not None:
        try:
            core.replace_text(path, fixed)  # its own temporary name: sessions start at once
        except OSError as e:  # still refused: the refusal says the values stay as the change left them
            unwritten = f"{type(e).__name__}: {e.strerror or e}"
    ks = sorted({d.key for d in drift} | {f"env.{k}" for k in env_keys})
    if off is not False:
        ks = sorted(ks + ["disableAllHooks"])
    keys = ", ".join(ks[:6]) + (f" (+{len(ks) - 6} more)" if len(ks) > 6 else "")
    if not project and core.older_copy():
        # The user settings differ from what this older copy would write, most likely because a newer release was
        # installed. It writes nothing; the pinned release's repair (_spawn runs it) puts back what really drifted
        # from the installed configuration, a tampered DEVKIT_* variable included.
        notice = _quietly("config-change", _older_copy_notice, agent, data.get("session_id") or "",
                          f"{tilde(path)} ({keys})") or ""
        _log(f"config-change left to the pinned release ({agent}): {keys}")
        _quietly("config-change", _spawn, ["config", "repair", "--quiet"])
        if env_keys:
            print(f"devkit's DEVKIT_* variables say who calls devkit and where its state lives, so they change "
                  f"only with devkit's configuration: refused {', '.join(f'env.{k}' for k in env_keys)}. "
                  f"{notice}".strip(), file=sys.stderr)
            sys.exit(2)
        if notice:
            print(json.dumps({"systemMessage": notice}))
        return
    _log(f"config-change blocked ({agent}): {keys}")
    from . import audit
    audit.record("config.blocked", path, keys, "blocked")
    if project:
        said = []
        if env_keys:
            env = ", ".join(f"env.{k}" for k in env_keys)
            done = (f"refused {env} in {path}, and could not remove them ({unwritten}): remove them from the file"
                    if unwritten else f"removed {env} from {path}")
            said.append(f"devkit's DEVKIT_* variables say who calls devkit and where its state lives, so no settings "
                        f"file sets them: {done}. Change devkit's configuration with dev config.")
        if off is not False:
            said.append(_hooks_off_refusal(path, json.dumps(off)))
        print(" ".join(said), file=sys.stderr)
        sys.exit(2)
    _quietly("config-change", policy._log, f"blocked change to devkit-managed keys: {keys}")
    _quietly("config-change", _spawn, ["config", "repair", "--quiet"])
    more = (f" It could not put devkit's values back ({unwritten}) in {tilde(path)}: make the file writable, then run "
            f"dev config repair." if unwritten else "")
    print(f"devkit manages these settings: {keys}. Change devkit's configuration instead "
          f"(config/claude/settings.json in the devkit repo or ~/.config/devkit/config.json) and run `dev install`."
          f"{more}", file=sys.stderr)
    sys.exit(2)


def _hooks_off_refusal(path: str, value: str) -> str:
    """The refusal of a disableAllHooks that is not false in a project's or local settings (config_change)."""
    return (f"disableAllHooks turns every hook off, devkit's too: refused disableAllHooks {value} in {tilde(path)}, "
            f"left in the file as written. In that file, set it to false or remove it: a new session reads it, and "
            f"would start without devkit's hooks.")


def _refuse_unchecked(path: str, project: bool, why: str):
    """Refuse a change to a settings file devkit read but could not check (config_change): user settings that are not
    a JSON object, or checks that raised (`why`: the error's name), on JSON whose shape they do not take (a hooks
    event that is not a list) or on devkit's own configuration (policy.claude_desired). Exit 2, recorded, and the file
    never rewritten: devkit names what to fix."""
    from . import audit, policy
    own = None
    if not project:  # the project's and local settings are checked against no configuration of devkit's
        try:
            policy.claude_desired()
        except Exception as e:
            own = e
    if own is not None:
        _log(f"config-change blocked, devkit's configuration failed: {type(own).__name__}")
        audit.record("config.blocked", path, "unchecked: devkit's configuration", "blocked")
        print(f"devkit's own configuration failed to load ({type(own).__name__}), so it cannot check the settings it "
              f"manages in {tilde(path)}: refused the change, the file left as it is. Run dev doctor.", file=sys.stderr)
        sys.exit(2)
    what = (f"{tilde(path)} for devkit's DEVKIT_* variables" if project else
            f"the settings it manages in {tilde(path)}")
    _log(f"config-change blocked, settings devkit could not check: {why}")
    audit.record("config.blocked", path, "unchecked", "blocked")
    print(f"devkit could not check {what} ({why}): refused the change, the file left "
          f"as it is. Make it a settings object as Claude Code documents it (hooks and env as objects); if it is one, "
          f"run dev doctor.", file=sys.stderr)
    sys.exit(2)


def _unreadable_why(raw: bytes) -> str:
    """Why devkit cannot read a settings file's bytes, in the words of a refusal: a byte that is not UTF-8 (Claude Code
    reads it replaced), not JSON, nested deeper than core.JSON_MAX_DEPTH (Claude Code reads it all the same), NaN or
    Infinity (Node refuses them, Python reads them), or a value Python's parser refuses (an integer of more digits
    than it converts), named as the parser names it."""
    why = []
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        why.append("it holds a byte that is not UTF-8")
    try:
        core.load_json(raw.decode("utf-8", "replace"), allow_nan=False)
    except json.JSONDecodeError:
        why.append("it is not JSON")
    except core.JSONDepthError:
        why.append(f"it nests deeper than {core.JSON_MAX_DEPTH} levels")
    except core.JSONNonFiniteError as e:
        why.append(f"it holds {e}")
    except ValueError as e:
        why.append(f"it holds a value devkit's JSON reader cannot take: {str(e).split(';')[0][:160]}")
    return ", and ".join(why) or "it changed while devkit read it"


def _refuse_unreadable(path: str, project: bool):
    """A settings file devkit cannot read (core.read_json gave nothing: a byte that is not UTF-8, not JSON, or nested
    deeper than core.JSON_MAX_DEPTH, all of which Claude Code may still read), recorded and refused with exit 2 as
    config_change refuses a file it can read, and never rewritten: devkit names what to fix. The user settings are
    refused whole, since devkit cannot check the keys it manages there (policy.diff_claude), naming the DEVKIT_*
    variables they set (policy.devkit_env) that differ from devkit's. A project's or local settings are refused when
    their text sets any DEVKIT_* variable, or a disableAllHooks that is not false (policy.hooks_off). The text is read
    as Claude Code reads it, a byte that is not UTF-8 replaced; a file whose text devkit then fails to read, or a
    failure of devkit's own configuration, is refused too: a hook that crashed would let the change pass."""
    from . import audit, policy
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        if project:  # gone or unreadable: no agent reads it either
            return
        _refuse_unread_user(path, f"it does not open: {type(e).__name__}", "")
    why = _unreadable_why(raw)
    try:
        have = policy.devkit_env(raw.decode("utf-8", "replace"))
    except Exception as e:  # what it sets is unknown: a hook that crashed would let the change pass
        _log(f"config-change blocked, settings devkit cannot read: {type(e).__name__}")
        audit.record("config.blocked", path, "unreadable", "blocked")
        print(f"devkit cannot read {tilde(path)} ({why}), so it cannot tell whether it sets devkit's DEVKIT_* "
              f"variables, which say who calls devkit and where its state lives: refused the change. Make the file "
              f"plain JSON.", file=sys.stderr)
        sys.exit(2)
    if not project:
        try:
            want = {p[1]: json.dumps(v) for p, v in policy.claude_desired()[0].items()
                    if len(p) == 2 and p[0] == "env" and str(p[1]).startswith("DEVKIT_")}
        except Exception as e:  # devkit's own configuration, not the file: still refused, the keys left unnamed
            _log(f"config-change blocked, devkit's configuration failed: {type(e).__name__}")
            _refuse_unread_user(path, why, f" devkit's own configuration also failed to load ({type(e).__name__}): "
                                           f"run dev doctor.")
        keys = ", ".join(f"env.{k}" for k in sorted(k for k, v in have.items() if want.get(k) != v))
        _refuse_unread_user(path, why, f" It sets devkit's DEVKIT_* variables {keys}: remove them." if keys else "",
                            keys)
    keys = ", ".join(f"env.{k}" for k in sorted(have))
    off = policy.hooks_off(raw.decode("utf-8", "replace"))
    if not keys and off is None:
        return
    _log(f"config-change blocked, settings devkit cannot parse: {keys or 'disableAllHooks'}")
    audit.record("config.blocked", path, ", ".join(k for k in ("disableAllHooks" if off is not None else "", keys) if k),
                 "blocked")
    said = [f"devkit cannot parse {tilde(path)} ({why})."]
    if keys:
        said.append(f"It sets devkit's DEVKIT_* variables, which say who calls devkit and where its state lives: "
                    f"refused {keys}. Remove them from the file; change devkit's configuration with dev config.")
    if off is not None:
        said.append(_hooks_off_refusal(path, off))
    print(" ".join(said), file=sys.stderr)
    sys.exit(2)


def _refuse_unread_user(path: str, why: str, more: str, keys: str = ""):
    """Refuse a change to the user settings devkit cannot read (_refuse_unreadable): exit 2, recorded with the DEVKIT_*
    keys it names, or as unreadable."""
    from . import audit
    _log(f"config-change blocked, user settings devkit cannot read: {keys or why}")
    audit.record("config.blocked", path, keys or "unreadable", "blocked")
    print(f"devkit cannot read {tilde(path)} ({why}), so it cannot check the settings devkit manages there: refused "
          f"the change. Make the file plain JSON (UTF-8, nested at most {core.JSON_MAX_DEPTH} levels).{more}",
          file=sys.stderr)
    sys.exit(2)


def _devkit_env_change(path: str, settings: dict, managed: bool = True) -> list:
    """The DEVKIT_* variables of the settings' env block that differ from what devkit manages (set,
    changed or removed), put back to devkit's values in the file (_devkit_env_fix). [] when none differ."""
    changed, fixed = _devkit_env_fix(path, settings, managed)
    if fixed is not None:
        core.replace_text(path, fixed)  # its own temporary name: sessions start at once
    return changed


def _devkit_env_fix(path: str, settings: dict, managed: bool = True) -> tuple:
    """The DEVKIT_* variables of the settings' env block that differ from what devkit manages (set, changed or
    removed), and the file's text with devkit's values put back, or None when it is not to be written. `managed`: the
    user settings, where devkit manages some; a project's settings get none. A copy older than the pinned release
    reports the user settings' variables but writes nothing: its values may be the older ones (the pinned release
    repairs). A project's settings get every DEVKIT_* variable removed by any copy: that holds in every release.
    -> (variables, text or None)."""
    from . import policy
    flat = policy.claude_desired()[0] if managed else {}
    want = {p[1]: json.dumps(v) for p, v in flat.items() if len(p) == 2 and p[0] == "env"
            and str(p[1]).startswith("DEVKIT_")}
    try:
        have = policy.devkit_env(Path(path).read_text())
    except OSError:
        return [], None
    changed = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
    if not changed or (managed and core.older_copy()):
        return changed, None
    env = {k: v for k, v in (settings.get("env") or {}).items() if not str(k).startswith("DEVKIT_")}
    settings["env"] = {**env, **{k: json.loads(v) for k, v in want.items()}}
    if not settings["env"]:
        settings.pop("env")
    return changed, json.dumps(settings, indent=2, allow_nan=False) + "\n"  # Node reads no NaN or Infinity


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
    _lessons_end(data, "main")  # what the session was shown and did not hit again was heeded, in any of its turns
    if sid:
        try:  # its open calls end with it, before the sync removes the session's record
            from . import identity
            identity.end_session(agent, str(sid))
        except Exception as e:
            _swallowed("session-end", e)
        _spawn_sync(["--session", f"{agent}:{sid}", "--finish"])


# --------------------------------------------------------------------------- lane routing and write detection

# Claude Code's Monitor runs its `command` in the same shell as Bash: it is judged, gated and counted as one
SHELL_TOOLS = ("Bash", "Monitor", "exec_command", "shell", "local_shell", "unified_exec")


def _watch_writes(agent: str, data: dict):
    """Write detection, the backstop of review and read-only lanes: before such a lane's shell call, what
    `git status` lists in the run's repository goes into the call's ledger entry; PostToolUse compares
    (_check_writes), so a write an allowed check made (a test run) is seen too."""
    aid = data.get("agent_id")
    if not aid or data.get("tool_name") not in SHELL_TOOLS or _headless():
        return
    from . import lanescope, progress
    run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
    if not lane or progress.lane_tools(run, aid) not in ("review", "read"):
        return
    base = lanescope.scope_base((run or {}).get("project"), data.get("cwd") or os.getcwd())
    before = lanescope.worktree_state(base)
    if before is not None:
        progress.call_note(data, wt_base=base, wt_before=before)


def _check_writes(agent: str, data: dict):
    """PostToolUse of a review or read-only lane's shell call: the paths it wrote (lanescope.written_paths between the
    states before and after it) go to progress.scope_written. Before the call leaves the ledger."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return
    from . import lanescope
    try:
        from . import progress
        e = progress.call_entry(data)
        if not isinstance(e.get("wt_before"), dict) or not e.get("wt_base"):
            return
        before = {k: tuple(v) if isinstance(v, list) else v for k, v in e["wt_before"].items()}  # JSON keeps lists
        paths = lanescope.written_paths(before, lanescope.worktree_state(e["wt_base"]))
        if paths:
            progress.scope_written(agent, data.get("session_id") or "-", data["agent_id"], paths,
                                   call=data if agent == "codex" else None)
    except Exception as ex:
        _swallowed("scope-write", ex)


def _reconcile(agent: str, data: dict):
    """The reconciler at every hook of the session (progress.reconcile_hook). Never raises."""
    if _headless():
        return
    try:
        from . import progress
        progress.reconcile_hook(agent, data)
    except Exception:
        _log(f"reconcile {agent}: {traceback.format_exc()[-300:]}")


def _close_call(data: dict, failed: bool = False):
    """The call's entry leaves the ledger: it ran (PostToolUse, PostToolUseFailure) or never will (refused). An edit
    call in flight that failed or was refused writes nothing, so its record goes; one that ran gives way to the
    record of its result at the end of its PostToolUse (progress.note_edit). `failed`: the call failed though its
    event is PostToolUse (a Codex command, which has no PostToolUseFailure)."""
    if data.get("tool_name") in ("Write", "Edit", "MultiEdit") and data.get("hook_event_name") != "PostToolUse":
        try:
            from . import progress
            progress.edit_dropped(data)
        except Exception:
            _log(f"edit record drop: {traceback.format_exc()[-300:]}")
    if data.get("tool_name") in SHELL_TOOLS or data.get("tool_name") == "apply_patch":
        try:
            from . import identity, progress
            entry = progress.call_entry(data)
            # a call that got no result (refused, failed) is told apart for the text pin's audit and order rule
            ended = "failed" if failed else {"PostToolUse": "", "PreToolUse": "refused",
                                             "PermissionDenied": "denied"}.get(str(data.get("hook_event_name")), "failed")
            if data.get("tool_name") in SHELL_TOOLS and data.get("hook_event_name") not in ("PreToolUse",
                                                                                          "PermissionDenied"):
                identity.keep_background(entry)  # a background call closes as it starts: kept for its reads
            if data.get("tool_name") in SHELL_TOOLS or entry.get("agent") == "codex":
                progress.call_closed(data, ended=ended)
            if data.get("tool_name") in SHELL_TOOLS and entry.get("agent") and data.get("tool_use_id"):
                _pin_audits(entry, data)
                # its token dies with it: its pin and use mark go, and the session record lists the call as closed
                identity.note_closed(entry["agent"], str(data.get("session_id") or ""), str(data["tool_use_id"]),
                                     entry=entry, ended=ended)
        except Exception:
            _log(f"ledger close: {traceback.format_exc()[-300:]}")


PIN_LATE_S = 30  # seconds after its call opened past which a pin's birth means someone wrote it again


def _pin_audits(entry: dict, data: dict):
    """When a stamped call ends: what its pin shows, as audit records (flagged; the uses themselves were judged when
    they happened). calls.pin-contested: another process wrote the pin first, or the call is marked contested in the
    ledger. calls.pin-late: the pin was born more than PIN_LATE_S after the call opened. calls.pin-replaced: its inode
    is not the one its first verified use saw. caller.token-unexplained-use: the tech lead's call runs no dev command,
    yet a dev command resolved its token (a script run by path may explain it)."""
    from . import identity, shellparse
    if data.get("hook_event_name") == "PreToolUse" or not entry.get("pin"):
        return
    pin, tid, sid = Path(str(entry["pin"])), str(data.get("tool_use_id") or ""), str(data.get("session_id") or "")
    used = identity._read_used(sid, tid)
    found = []
    mark = identity._contest_mark(entry)  # its pin's marker, or its contest flag in the ledger when no marker was made
    if mark:
        found.append(("calls.pin-contested", "another process wrote this call's pin before its shell did"
                      if mark == "pin" else "this call is marked contested: its contest flag is in the ledger"))
    try:
        st = os.stat(pin)
        born = getattr(st, "st_birthtime", st.st_ctime)
        if born > float(entry.get("ts") or 0) + PIN_LATE_S:
            found.append(("calls.pin-late", f"its pin was written {born - float(entry.get('ts') or 0):.0f} s after "
                                            f"the call opened"))
        if used.get("inode") and used["inode"] != st.st_ino:
            found.append(("calls.pin-replaced", "its pin is not the file its first verified use saw"))
    except OSError:
        pass
    if identity.used_path(sid, tid).exists() and str(entry.get("caller") or "main") == "main":
        try:
            runs = [shellparse.dev_args(a) for a, _ in shellparse.shell_calls(str(entry.get("cmd") or ""), None) or []]
        except Exception:
            runs = [None]
        if not any(r for r in runs) and not entry.get("cut"):
            found.append(("caller.token-unexplained-use", "a dev command resolved this call's token, and its text "
                                                          "runs no dev command", {}))
    unlisted = [u for u in identity.read_uses(sid, tid) if u.get("change") and not u.get("listed")]
    if unlisted:  # code the call started (a script, a test, a git hook) changed state as the call's caller
        found.append(("caller.token-unlisted-use", f"{len(unlisted)} state change(s) through this call's token by "
                                                   f"dev commands its text does not run: code it started ran them",
                      {"count": len(unlisted), "by": str(entry.get("caller") or "main")[:80]}))
    for action, why, *more in found:
        try:
            from . import audit
            audit.record(action, tid[:80] or "-", why, "flagged", tool_use_id=tid[:80], session=sid[:80],
                         host_agent=str(entry.get("agent") or ""), command=str(entry.get("cmd") or "")[:300],
                         **(more[0] if more else {}))
        except Exception as e:
            _swallowed("post-tool", e)


def _rewrite(agent: str, updated: dict) -> dict:
    """A PreToolUse that changes the tool's input. Claude Code applies updatedInput with no permission decision, and
    its own permission settings then apply to the new input. Codex applies it only with
    `permissionDecision: "allow"`, the one permission decision this hook ever makes (it never denies or asks), and
    only on a rewrite _codex_may_rewrite lets through."""
    out = {"hookEventName": "PreToolUse", "updatedInput": updated}
    if agent == "codex":
        out["permissionDecision"] = "allow"
    return out


# A word no shell expands: single-quoted; double-quoted with no $, backtick or backslash; or unquoted, made only of
# characters neither bash nor zsh gives a meaning, and not starting with = or ~ (zsh's =(…) and =cmd, the home folder).
# Each word ends at a space, a tab or the end of the command, so no word runs into another.
_PLAIN_WORD = re.compile(r"""[ \t]*('[^']*'|"[^"$`\\]*"|[A-Za-z0-9_./:@%+,-][A-Za-z0-9_./:@%+,=-]*)(?=[ \t]|\Z)""")
# the variables laneroute.route, _watch_route, _speaker_route and the call token put in front of a devkit call
_DEVKIT_VARIABLE = re.compile(r"(?:DEVKIT_FROM_AGENT|DEVKIT_SESSION|DEVKIT_SPEAKER|DEVKIT_CALL)=[A-Za-z0-9_.:-]+\Z")
_SH_FAMILY = frozenset({"bash", "zsh", "sh"})  # the shells that know `export …; readonly …;`


def _sh_family(agent: str, data: dict) -> bool:
    """Does the call's shell read `export` and `readonly`? Claude Code's Bash runs bash or zsh; a Codex exec runs the
    `shell` its input names, else the user's login shell (fish and nushell have no `readonly`)."""
    if agent != "codex":
        return True
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    shell = inp.get("shell") or os.environ.get("SHELL") or "/bin/zsh"
    return os.path.basename(str(shell)) in _SH_FAMILY


def _token_skipped(agent: str, data: dict, why: str):
    """A shell call that goes out without its call token: audited (calls.token-skipped). In a session whose calls carry
    tokens, its state changes are then refused, with the fix to run it again (identity.NO_TOKEN)."""
    try:
        from . import audit
        audit.record("calls.token-skipped", str(data.get("tool_use_id") or "-")[:80], why, "flagged",
                     tool_use_id=str(data.get("tool_use_id") or "")[:80], session=str(data.get("session_id") or "")[:80],
                     host_agent=agent, lane=str(data.get("agent_id") or "main")[:80])
    except Exception as e:
        _swallowed("pre-tool", e)


def _unasked(agent: str, data: dict) -> bool:
    """Does the agent run this call without checking it against the developer's rules? Codex where it never asks
    (_codex_never_asks); Claude Code in bypassPermissions (the payload's permission_mode). Elsewhere a prefix the
    developer's allow rules do not know would make the call prompt."""
    return _codex_never_asks(data) if agent == "codex" else data.get("permission_mode") == "bypassPermissions"


def _with_token(agent: str, data: dict, new, token: str):
    """The shell call's input (`new`, the routed one, else its own) with the call token, and who pins the call
    ("shell" or "dev"), or None when this call's form cannot carry one:
    - where the agent never asks (_unasked) and the shell is sh, bash or zsh: the pin's write first
      (identity.pin_stamp), then `export DEVKIT_CALL=<v>; readonly DEVKIT_CALL;`, on every call;
    - elsewhere (a Claude Code permission mode that checks rules, a Codex that asks, fish or nushell): the plain form
      `DEVKIT_CALL=<v> dev …`, on one plain devkit call only, whose dev process pins the call itself.
    A token never costs a rewrite Codex would apply (_codex_may_rewrite), and never makes one either: where Codex
    asks, `allow` would skip the developer's approval, so only a call devkit rewrites anyway carries it. The leading
    space keeps the stamped command out of the shell's history."""
    from . import identity
    if agent == "codex" and new is None and not _codex_never_asks(data):
        return None
    base = new or data.get("tool_input") or {}
    cmds = _shell_inputs({"tool_name": data.get("tool_name"), "tool_input": base})
    if len(cmds) != 1 or not cmds[0][0]:
        return None
    key, cmd = cmds[0]
    var = identity.CALL_VAR
    if not _unasked(agent, data) or not _sh_family(agent, data):
        if not _plain_devkit_call(cmd):
            return None
        at = 0  # after devkit's own variables, right before `dev`
        while True:
            m = _PLAIN_WORD.match(cmd, at)
            if not m or not _DEVKIT_VARIABLE.match(m.group(1)):
                break
            at = m.end()
        lead = cmd[:at].strip(" \t")
        out, by = {**base, key: f"{lead + ' ' if lead else ''}{var}={token} {cmd[at:].lstrip(' ' + chr(9))}"}, "dev"
    else:
        pin = identity.pin_path(str(data.get("session_id") or ""), str(data.get("tool_use_id") or ""))
        out, by = {**base, key: f" {identity.pin_stamp(pin)}export {var}={token}; readonly {var}; {cmd}"}, "shell"
    return (out, by) if agent != "codex" or _codex_may_rewrite(data, out) else None


def _hook_launched_by(pid: int) -> bool:
    """Did agent process `pid` start this hook, directly or through one shell, as Claude Code and Codex run their
    hooks? An agent further up started some other program that runs the hook (a test runner, a script), so the
    calls it records are not that agent's shell calls."""
    parent = os.getppid()
    if parent == pid:
        return True
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(parent)], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == str(pid)


def _stamped_token(agent: str, data: dict, new, token: str, digest: str):
    """The input a shell call goes out with when it carries its call token, None when it carries none (it then goes
    out as `new`, the routed input, or as it is). The token goes out only once its ledger entry holds the nonce's hash
    (read back) and the session record lists the call under its hold (identity.note_opened, which also marks the
    session as one whose calls carry tokens, even for a Codex call whose form cannot carry one). A failure stamps no
    token and is audited, and so is a rewrite that could not take the token."""
    from . import identity, progress
    sid, tid = str(data.get("session_id") or ""), str(data.get("tool_use_id") or "")
    if not token:
        return None
    formed = _with_token(agent, data, new, token)
    if formed is None:
        if new is not None:
            _token_skipped(agent, data, "the rewrite would not carry the token: it goes out without it")
        identity.note_opened(agent, sid)
        return None
    tokened, by = formed
    host = (os.environ.get(identity.THREAD_VAR) or "").strip()[:80]
    if agent == "claude" and host:  # a Claude Code started from a Codex shell: its commands carry that thread
        progress.call_note(data, host_thread=host)
    owner = progress.call_entry(data).get("agent_pid")
    launched = isinstance(owner, int) and _hook_launched_by(owner)
    if isinstance(owner, int) and not launched:
        progress.call_note(data, agent_pid=None)  # an agent further up started what runs this hook, not the hook
    pin = str(identity.pin_path(sid, tid))
    try:
        identity.make_pin_folder(sid)
    except OSError:
        _token_skipped(agent, data, "its pin folder could not be made")
        return None
    # the call's pin, who writes it, the process that starts the agent's calls (the agent, or its launcher), and a
    # background call as such (its record is kept for its reads and waits once it closes, identity.keep_background)
    background = _background_call(data)
    progress.call_note(data, pin=pin, pin_by=by, **({"launcher_pid": os.getppid()} if launched else {}),
                       **({"background": True} if background else {}))
    entry = progress.call_entry(data)
    ttl = identity.codex_ttl(data.get("tool_input")) if agent == "codex" else None
    if not hmac.compare_digest(str(entry.get("token_sha256") or ""), digest) or \
            ttl is not None and entry.get("ttl") != ttl or agent == "claude" and entry.get("host_thread", "") != host \
            or entry.get("pin") != pin or entry.get("pin_by") != by or bool(entry.get("background")) != background:
        _token_skipped(agent, data, "its call ledger entry does not hold the token")
        return None
    if not identity.note_opened(agent, sid, tid, every=_unasked(agent, data) and _sh_family(agent, data)):
        _token_skipped(agent, data, "the session record is busy")
        return None
    return tokened


def _text_pinnable(agent: str, data: dict) -> bool:
    """Does this shell call go out exactly as the agent wrote it, its dev command named by the text pin
    (identity._text_verdict)? Where the agent checks the developer's permission rules on the command it runs (not
    _unasked), on sh, bash or zsh, for one dev call of plain words with nothing in front (_bare_dev_call): the host
    then judges that very text, so an allow rule such as Bash(dev progress:*) matches it, and devkit changes nothing
    the host judges. On Codex, only when Codex would not apply devkit's rewrite (_pre_tool)."""
    if data.get("tool_name") not in SHELL_TOOLS or _unasked(agent, data) or not _sh_family(agent, data):
        return False
    cmds = _shell_inputs(data)
    return len(cmds) == 1 and bool(cmds[0][0]) and _bare_dev_call(cmds[0][1])


def _bare_dev_call(cmd: str) -> bool:
    """One dev call made only of plain words (_PLAIN_WORD), `dev` first as the shell finds it on PATH, unquoted, and
    nothing in front: no variable, no path, no separator, redirection, newline or word a shell expands."""
    if not isinstance(cmd, str):
        return False
    words, at = [], 0
    while True:
        m = _PLAIN_WORD.match(cmd, at)
        if not m:
            break
        words.append(m.group(1))
        at = m.end()
    return not cmd[at:].strip(" \t") and len(words) > 1 and words[0] == "dev"


def _background_call(data: dict) -> bool:
    """Does this shell call run in the background (Bash's run_in_background, or a Monitor), so its call closes as soon
    as it starts?"""
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    return inp.get("run_in_background") is True or data.get("tool_name") == "Monitor"


def _text_call(agent: str, data: dict):
    """Record a call that goes out as written for the text pin: its entry names its pin (pin_by "text"), the loop
    that launches the agent's calls when the agent's hook runs below one, a background call as such, and on Claude
    Code the Codex thread it carries; the session record lists it as opened. A failure is audited
    (calls.token-skipped) and still marks the session as one whose calls are named by kernel facts
    (identity.note_opened): its dev command then falls to the ledger's rules, and a state change is refused."""
    from . import identity, progress
    sid, tid = str(data.get("session_id") or ""), str(data.get("tool_use_id") or "")
    owner = progress.call_entry(data).get("agent_pid")
    launched = isinstance(owner, int) and _hook_launched_by(owner)
    if isinstance(owner, int) and not launched:
        progress.call_note(data, agent_pid=None)  # an agent further up started what runs this hook, not the hook
    try:
        identity.make_pin_folder(sid)
    except OSError:
        _token_skipped(agent, data, "its pin folder could not be made")
        identity.note_opened(agent, sid)  # still a session whose calls are named by kernel facts, never by text alone
        return
    background = _background_call(data)
    host = (os.environ.get(identity.THREAD_VAR) or "").strip()[:80] if agent == "claude" else ""
    fields = {"pin": str(identity.pin_path(sid, tid)), "pin_by": "text",
              **({"launcher_pid": os.getppid()} if launched else {}), **({"background": True} if background else {}),
              **({"host_thread": host} if host else {})}
    progress.call_note(data, **fields)
    entry = progress.call_entry(data)
    if any(entry.get(k) != v for k, v in fields.items()):
        _token_skipped(agent, data, "its call ledger entry does not hold its pin")
        identity.note_opened(agent, sid)
        return
    if not identity.note_opened(agent, sid, tid):
        _token_skipped(agent, data, "the session record is busy")
        identity.note_opened(agent, sid)


def _identity_strip_advice(agent: str, data: dict) -> list:
    """A crew member's command that strips or sets devkit's identity variables (env -u, env -i, unset, an assignment
    to DEVKIT_CALL or CODEX_THREAD_ID): advice, and an audit record (caller.spoof-suspect). A typo guard, not a
    control: the call token's bindings are what holds (identity._verify)."""
    from . import laneroute
    cmds = _shell_inputs(data)
    if not data.get("agent_id") or len(cmds) != 1:
        return []
    found = laneroute.identity_strips(cmds[0][1])
    if not found:
        return []
    try:
        from . import audit
        audit.record("caller.spoof-suspect", str(data.get("agent_id"))[:80], ", ".join(found)[:300], "flagged",
                     tool_use_id=str(data.get("tool_use_id") or "")[:80], session=str(data.get("session_id") or "")[:80],
                     host_agent=agent, lane=str(data.get("agent_id"))[:80])
    except Exception as e:
        _swallowed("pre-tool", e)
    return [f"this command changes devkit's identity variables ({', '.join(found)}): devkit names who runs a "
            "command by its call token, so a dev call that drops or sets them acts as nobody; run dev commands as "
            "they are, in your lane"]


def _plain_devkit_call(cmd: str) -> bool:
    """Is a shell command one bare devkit coordination call made only of plain words (_PLAIN_WORD): devkit's own
    variables (_DEVKIT_VARIABLE), then `dev` as the shell finds it on PATH and one of SPEAKER_COMMANDS, unquoted? Any
    other word in front (`PATH=…`, `./dev`, a quoted `dev`), a separator, a redirection, a newline or a word a shell
    expands (`$…`, `=(…)`, `*(e:…:)`, `{a,b}`, `~`) makes it something else."""
    if not isinstance(cmd, str):
        return False
    words, at = [], 0
    while True:
        m = _PLAIN_WORD.match(cmd, at)
        if not m:
            break
        words.append(m.group(1))
        at = m.end()
    if cmd[at:].strip(" \t") or not words:
        return False
    i = 0
    while i < len(words) and _DEVKIT_VARIABLE.match(words[i]):
        i += 1
    return len(words) > i + 1 and words[i] == "dev" and words[i + 1] in SPEAKER_COMMANDS


def _toml_top_level(text: str, key: str):
    """The string value of `key` at the top level of a TOML text (before its first table), as setup reads Codex's
    approval_policy (setup._without_devkit_modes), or None."""
    for line in text.splitlines():
        if re.match(r"\s*\[", line):
            return None
        m = re.match(r"""\s*%s\s*=\s*(["'])([^"']*)\1\s*(#.*)?$""" % re.escape(key), line)
        if m:
            return m.group(2)
    return None


def _codex_never_asks(data: dict) -> bool:
    """Does Codex run this call without asking the developer, whatever devkit answers? Only when every setting devkit
    can read says its approval policy is `never`: devkit's own codex.approval_policy (as setup reads it to write
    Codex's configuration, `never` unless set), the top level of Codex's config.toml (what setup writes; a profile
    or another table does not count), and the call's permission_mode, which Codex sends as `bypassPermissions` only
    for a turn whose policy is never (so it shows a profile or a command-line flag that config.toml does not). A
    setting that is missing or cannot be read counts as asking."""
    if str((core.CFG.get("codex") or {}).get("approval_policy", "never")) != "never":
        return False
    if data.get("permission_mode") != "bypassPermissions":
        return False
    try:
        text = (Path(core.CODEX_HOME) / "config.toml").read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return _toml_top_level(text, "approval_policy") == "never"


def _codex_may_rewrite(data: dict, updated: dict) -> bool:
    """May this Codex call get the new input `updated`? Codex applies a changed input only with `allow` (_rewrite),
    which also skips any approval Codex would ask for the whole call, so devkit never approves a command the
    developer's Codex settings would ask about: Codex never asks (_codex_never_asks), or the call's input is its
    command alone, before and after the rewrite, and the new command is one bare devkit coordination call of plain
    words (_plain_devkit_call). Otherwise the call runs as it is, and gets no permission decision."""
    if _codex_never_asks(data):
        return True
    cmds = _shell_inputs({"tool_name": data.get("tool_name"), "tool_input": updated})
    inp = data.get("tool_input")
    return len(cmds) == 1 and bool(cmds[0][0]) and isinstance(inp, dict) and set(inp) == set(updated) == \
        {cmds[0][0]} and _plain_devkit_call(cmds[0][1])


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


def _codex_session_assignment(agent: str, data: dict):
    """A shell-safe root identity from the trusted Codex hook for the main thread's calls, None for a malformed or
    missing root (no rewrite then); empty for Claude, and for a crew member's calls, whose session devkit finds by
    their lane: a member's shell never carries the root thread's id."""
    if agent != "codex":
        return ""
    sid = data.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", sid):
        return None
    return "" if data.get("agent_id") else f"DEVKIT_SESSION=codex:{sid}"


def _route_subagent_progress(data: dict, agent: str = "claude"):
    """Inside a subagent (the hook input carries `agent_id`, in Claude Code and Codex), each `dev progress …` or
    `dev crew ask|answer|inbox|decide|decisions|mode|delegate|decline|route` call of its shell command gets
    DEVKIT_FROM_AGENT, so it updates that subagent's lane (its messages, decisions and delegations come from it, and
    `decide` refuses it) and never advances the main plan (laneroute.route). A single call is the whole command (a
    quoted heredoc message is data); a compound one (`cd <worktree> && dev progress step "…" | tail -3`) gets the
    variable on each lane call the router places."""
    aid = data.get("agent_id")
    inp = data.get("tool_input") or {}
    cmds = _shell_inputs(data)  # Bash's command or Codex's cmd: one, held as text, which the rewrite replaces
    if not aid or len(cmds) != 1 or not cmds[0][0]:
        return None
    key, cmd = cmds[0]
    from . import laneroute, shellparse
    new = laneroute.route(cmd, str(aid)) or _watch_route(cmd, str(aid))
    if new is None:
        return _stamp_member(data, agent, key, cmd)
    stamp = _codex_session_assignment(agent, data)
    if stamp is None:
        return None
    if stamp:
        new = f"{stamp} {new}" if shellparse.runs_one_command(cmd) else f"export {stamp}; {new}"
    return {**inp, key: new}


def _can_stamp(data: dict, agent: str) -> bool:
    """True when the hook may export a member's lane variable for its whole command: an agent id the shell reads as
    a plain word, on Claude Code and Codex alike (a Codex stamp goes out only where Codex never asks:
    _codex_may_rewrite)."""
    return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(data.get("agent_id") or "")))


def _stamp_member(data: dict, agent: str, key: str, cmd: str):
    """A member's command that runs lane calls the router cannot place one by one (`dev crew inbox 2>&1 | tail`, two
    steps in a list, a loop, `NO_COLOR=1 dev crew rules`, a lane call next to a write in the member's scratch folder)
    gets the lane variable exported in front (`export <LANE_VAR>=<agent id>; …`), so every dev call it starts, at any
    depth and in the background, acts as the member. On Codex the session's identity is exported first
    (_codex_session_assignment), as the router's placement carries it. -> the new tool input, or None."""
    from . import identity, laneroute
    if not _can_stamp(data, agent) or not laneroute.holds_lane_call(cmd, _shell_cwd(data)):
        return None
    session = _codex_session_assignment(agent, data)
    if session is None:
        return None
    inp = data.get("tool_input") or {}
    # readonly: no read, loop, printf -v or assignment of the command reassigns it in this shell
    var = identity.LANE_VAR
    lead = f"export {session}; " if session else ""
    return {**inp, key: f"{lead}export {var}={data['agent_id']}; readonly {var}; {cmd}"}


def _watch_route(cmd: str, aid: str):
    """A member's `dev watch …` call that is the whole command: with DEVKIT_FROM_AGENT=<aid> in front, so its wait
    takes the member's own events, never the main session's. The call ledger names the member only while the call
    is open, and a background command's call closes as soon as it starts. None for any other command."""
    from . import shellparse
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", aid) or not shellparse.runs_one_command(cmd):
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if not argv or os.path.basename(argv[0]) != "dev" or argv[1:2] != ["watch"]:
        return None
    return f"DEVKIT_FROM_AGENT={aid} {cmd.strip()}"


# The dev commands whose caller devkit must know, stamped with who runs them: the tech lead's (DEVKIT_SPEAKER=main)
# and a crew member's (DEVKIT_FROM_AGENT=<agent id>). A member's `dev progress` and `dev crew` calls go through
# laneroute.route, which places one lane call per command.
SPEAKER_COMMANDS = ("watch", "session", "crew", "progress")
MEMBER_STAMPED = ("watch", "session")
# what may run around a stamped dev call in a compound command: changing folder, reading, and the wrappers whose own
# commands are listed too (eval, a shell's -c script). Anything else keeps the command unstamped, so the rewrite never
# carries another program
_AROUND = frozenset({"cd", "pushd", "popd", "tail", "head", "grep", "egrep", "fgrep", "rg", "wc", "cat", "jq", "sort",
                     "uniq", "cut", "tr", "echo", "printf", "true", ":"})
_WRAPPERS = frozenset({"eval", "sh", "bash", "zsh", "dash"})
_QUIET = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "-"})  # redirection targets that write no file


def _script_of(argv: list):
    """The script an eval or a shell's -c runs, or None for any other call."""
    name = os.path.basename(argv[0]) if argv else ""
    if name == "eval":
        return " ".join(argv[1:])
    if name in _WRAPPERS:
        k = next((j for j, a in enumerate(argv[1:], 1) if re.fullmatch(r"[-+][A-Za-z]*c[A-Za-z]*", a)), 0)
        return argv[k + 1] if k and k + 1 < len(argv) else None
    return None


def _writes(cmd: str, depth: int = 0) -> bool:
    """Does a shell command redirect output into a file (`>`, `>>`, `&>`, `>|`; not `2>&1` or /dev/null), here or in
    a script it runs through eval or -c? A command the shell reader cannot split counts as one that writes."""
    from . import shellparse
    segs = shellparse.split_commands(cmd) if depth < 4 else None
    if segs is None:
        return True
    for argv0, redirs in segs:
        if any(t in (">|", ">>|") for t in argv0) or any(str(r) not in _QUIET for r in redirs):
            return True
        script = _script_of(list(argv0))
        if script is not None and _writes(script, depth + 1):
            return True
    return False


def _stampable(cmd: str, cwd: str, wanted) -> bool:
    """Does a compound command run a dev call of `wanted`, with only `cd`, reads and wrappers around it and no
    redirection into a file, at any depth the shell reader sees (pipelines, lists, eval, sh -c, subshells, substitutions)?"""
    from . import shellparse
    calls = shellparse.shell_calls(cmd, cwd)
    hit = False
    for argv, code in calls or []:
        name = os.path.basename(argv[0]) if argv else ""
        if name == "dev" and len(argv) > 1 and argv[1] in wanted:
            hit = True
        elif _script_of(argv) is not None:
            continue  # eval, sh -c: the shell reader lists the commands they run, read here one by one
        elif not (name in _AROUND and code is None):
            return False
    return hit and not _writes(cmd)


def _speaker_route(data: dict, agent: str = "claude"):
    """Who runs a shell call's dev commands, stamped so identity.speaker knows it (rules 2 and 3): the tech lead's
    `dev watch|session|crew|progress` get DEVKIT_SPEAKER=main, a crew member's `dev watch|session` its
    DEVKIT_FROM_AGENT. A call that is the whole command gets the variable in front of it; a compound one (a pipeline,
    a list, eval, sh -c, a subshell) gets it exported for the whole command, when only `cd`, reads and wrappers run
    around the dev call. A background call leaves the call ledger as soon
    as it starts, so without the stamp devkit could not tell a wait's caller. Codex may not apply a rewrite; its open
    call in the ledger then names who calls. -> the new tool input, or None."""
    inp = data.get("tool_input") or {}
    cmds = _shell_inputs(data)  # Bash's command or Codex's cmd: one, held as text, which the stamp goes on
    if len(cmds) != 1 or not cmds[0][0]:
        return None
    key, cmd = cmds[0]
    aid = str(data.get("agent_id") or "")
    if aid and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", aid):
        return None
    var, value, wanted = ("DEVKIT_FROM_AGENT", aid, MEMBER_STAMPED) if aid else \
        ("DEVKIT_SPEAKER", "main", SPEAKER_COMMANDS)
    stamp = _codex_session_assignment(agent, data)
    if stamp is None:
        return None
    assignments = f"{var}={value}" + (f" {stamp}" if stamp else "")
    from . import shellparse
    try:
        argv = shlex.split(cmd) if shellparse.runs_one_command(cmd) else []
    except ValueError:
        return None
    if len(argv) > 1 and os.path.basename(argv[0]) == "dev":  # one dev call, the whole command
        return {**inp, key: f"{assignments} {cmd.strip()}"} if argv[1] in wanted else None
    if not _stampable(cmd, _shell_cwd(data), wanted):  # eval, sh -c, ( … ) and compound commands
        return None
    return {**inp, key: f"export {assignments}; {cmd}"}


PITFALL_REPEAT_S = 3600  # a lesson is recalled to the same agent at most once an hour


def _headless() -> bool:
    """devkit's own headless model call, which ai.ask marks in its environment: its hooks only judge tools."""
    return os.environ.get("DEVKIT_HEADLESS") == "1"


PITFALL_SHOW = 3  # lessons a call, its output or a refusal shows, the most relevant first
LESSON_HITS_MAX = 20  # lessons a call, its output or a refusal is matched against to find a miss
CALLS_KEPT = 20  # calls whose shown lessons the session record keeps, so a call's own output is no miss of them
BRIEFS_KEPT = 50  # launches whose brief lessons wait for their member's first call


def _lesson_agent(data: dict) -> str:
    """Who in the session a lesson reached, for its outcome: the member's agent id, or "main"."""
    return str(data.get("agent_id") or "main")


def _session_record(agent: str, data: dict) -> dict:
    sf = _session_file(agent, data.get("session_id") or "-")
    return (state.read(sf) or {}) if sf.exists() else {}


def _pitfalls(agent: str, data: dict, found: list, remember: bool = True, channel: str = "call") -> str:
    """The context line for the lessons a call or its output met, each once an hour per agent of the session. What
    it shows, after that filter, is recorded as shown on `channel` (lessons.shown)."""
    if not found:
        return ""
    from . import lessons
    who, now = _lesson_agent(data), time.time()
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
            if channel == "call":
                _note_call(d, data, [x["meta"]["id"] for x in found])
        try:
            state.update(sf, mark, create=False)
        except state.StateBusy:
            pass  # audited by state: the lesson may come again, which is the safe side
    _shown(data, [x["meta"]["id"] for x in found], channel)
    return ("devkit · known pitfalls for this call (lessons from earlier mistakes; follow their prevention):\n"
            + "\n".join(lessons.pitfall_lines(found)))


def _shown(data: dict, ids: list, channel: str, count: bool = True):
    """Lessons that reached the agent of this hook event: counted on `channel`, and pending for that agent until a
    refusal or an output shows their error again (missed) or the session or its lane ends (heeded). devkit's own
    headless calls record nothing."""
    if ids and not _headless():
        from . import lessons
        lessons.shown(ids, channel, str(data.get("session_id") or ""), _lesson_agent(data), count=count)


def _note_call(sess: dict, data: dict, ids: list):
    """Remember, in the session record, the lessons shown with this call (by its tool_use_id): that call's own output
    is no miss of them, since the agent read them only as the call ran."""
    tid = str(data.get("tool_use_id") or "")
    if tid and ids:
        calls = dict(sess.get("lessons_by_call") or {})
        calls[tid] = list(dict.fromkeys((calls.get(tid) or []) + list(ids)))
        sess["lessons_by_call"] = dict(list(calls.items())[-CALLS_KEPT:])


def _lesson_hits(data: dict, ids: list, *, project: str = ""):
    """A refusal or a tool's output showed these lessons' error anchors: those shown to the agent earlier in this
    session and still pending are missed (lessonevents.on_hit). Nothing is read when nothing was hit."""
    sid = str(data.get("session_id") or "")
    ids = [x for x in dict.fromkeys(ids) if x]
    if ids and sid and not _headless():
        from . import lessonevents
        lessonevents.on_hit(ids, sid, _lesson_agent(data),
                            source_event=str(data.get("tool_use_id") or ""), project=project)


def _lesson_nudge(fixes: list) -> str:
    """The Stop hook's question, once per session, after a fix or revert commit (lessons.fixed_in_session): whether
    the mistake deserves a lesson."""
    return ("devkit: this session fixed or reverted something (" + "; ".join(fixes[:3]) + "). If it was a mistake "
            "that could happen again, record a lesson: `dev lesson add \"<the rule>\" --happened \"…\" --cause \"…\" "
            "--prevent \"…\" --file <glob> --command \"<command words>\" --error \"<output text>\" --evidence "
            "\"commit …\"` (keep the anchors that say where it applies: a lesson with none is refused), or "
            "`dev lesson recur <id> --evidence \"…\"` if a known pitfall repeated. Skip it for a trivial fix.")


def _lessons_end(data: dict, who: str):
    """The main session ended (SessionEnd: an exit, a /clear, a logout), or a member's lane did: the lessons still
    pending for that agent were heeded (lessonevents.on_end). A turn's end resolves nothing: a lesson shown in turn
    one is missed when its mistake comes back in turn five."""
    sid = str(data.get("session_id") or "")
    if not sid or not who or _headless():
        return
    try:
        from . import lessonevents
        lessonevents.on_end(sid, who)
    except Exception as e:
        _swallowed("lessons", e)


def _brief_lessons(prompt) -> list:
    """The lesson ids a crew brief carries, as lessons.pitfall_lines writes them."""
    return list(dict.fromkeys(re.findall(r"(?m)^- Lesson (L-[0-9A-Za-z]{1,40}) \(recorded by ", str(prompt or ""))))


def _stash_brief(agent: str, data: dict, prompt):
    """A launch's brief carried lessons: keep their ids by the launch's tool_use_id until the member's first call,
    which binds it to its agent id (_brief_shown). They were counted when the brief was written."""
    tid, ids = str(data.get("tool_use_id") or ""), _brief_lessons(prompt)
    sf = _session_file(agent, data.get("session_id") or "-")
    if not (tid and ids and sf.exists()) or _headless():
        return
    now = time.time()

    def put(d):
        briefs = {k: v for k, v in (d.get("brief_lessons") or {}).items()
                  if isinstance(v, dict) and now - float(v.get("ts") or 0) < 86400}
        briefs[tid] = {"ids": ids, "ts": now}
        d["brief_lessons"] = dict(list(briefs.items())[-BRIEFS_KEPT:])
    try:
        state.update(sf, put, create=False)
    except state.StateBusy:
        pass  # audited by state: the brief's lessons go uncounted as outcomes, never shown twice


def _brief_shown(agent: str, data: dict, sess: dict):
    """A member's call, once its launch is bound: the lessons its brief carried are recorded as shown to it on the
    `brief` channel (once; the brief already counted them)."""
    aid, briefs = str(data.get("agent_id") or ""), sess.get("brief_lessons") or {}
    if not aid or not briefs:
        return
    tid = ((sess.get("subagents") or {}).get(aid) or {}).get("launch") or next(
        (k for k, v in (sess.get("launches") or {}).items() if isinstance(v, dict) and v.get("agent_id") == aid), "")
    if tid not in briefs:
        return
    taken = {}

    def take(d):
        rest = dict(d.get("brief_lessons") or {})
        if tid not in rest:
            return state.SKIP
        taken.update(rest.pop(tid) or {})
        d["brief_lessons"] = rest
    try:
        state.update(_session_file(agent, data.get("session_id") or "-"), take, create=False)
    except state.StateBusy:
        return
    _shown(data, taken.get("ids") or [], "brief", count=False)


def _call_pitfalls(agent: str, data: dict, remember: bool = True, channel: str = "call", show: bool = True,
                   skip=(), advised: str = "") -> str:
    """PreToolUse: the open lessons anchored on the command this call runs or the file it writes, and, for a call
    the hook advises on (`advised`: the advice's text), those anchored on that text as an error, first. A lesson whose
    error anchor the advice shows is first resolved as missed when it was shown to the agent earlier and still
    pending: the mistake came back. Running a lesson's command again is no miss, since the right way runs it too (`dev progress step`,
    `dev test prove`). Then what is shown is recorded. `show=False` only resolves (a Codex call, which reads no
    context). `skip`: the lesson of a lesson rule this call matched, which the rule already said and recorded."""
    try:
        sess = _session_record(agent, data)
        project = str(sess.get("project") or "")
        if not project:
            return ""
        from . import lessons
        _brief_shown(agent, data, sess)
        inp = data.get("tool_input") or {}
        cmd = _command_of(data)  # Bash's command or Codex's cmd
        files = [x for x in (inp.get("file_path"), inp.get("notebook_path")) if x]
        if not (cmd or files or advised):
            return ""
        pool = lessons.recall_pool(project, _initiatives(project))  # loaded once for both questions
        found = lessons.for_call(project, files=files, command=cmd, limit=LESSON_HITS_MAX, pool=pool) \
            if cmd or files else []
        by_advice = lessons.for_output(project, advised, limit=LESSON_HITS_MAX, pool=pool) if advised else []
        skip = {x for x in skip if x}
        _lesson_hits(data, [x["meta"]["id"] for x in by_advice if x["meta"]["id"] not in skip], project=project)
        if not show:
            return ""
        picked = list({x["meta"]["id"]: x for x in by_advice + found if x["meta"]["id"] not in skip}.values())
        return _pitfalls(agent, data, picked[:PITFALL_SHOW], remember, channel)
    except Exception as exc:
        _swallowed("lessons", exc)
        _log(f"pitfalls {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _output_text(data: dict) -> str:
    r = data.get("tool_response")
    if isinstance(r, dict):
        r = "\n".join(str(r.get(k) or "") for k in ("stdout", "stderr", "output", "error"))
    return (str(r or "") + "\n" + str(data.get("error") or ""))[-50_000:]


# Codex has no PostToolUseFailure event, and its PostToolUse result is the command's output alone (Codex 0.167): the
# exit code is in the rollout, in the CommandExecution item whose id is the hook's tool_use_id. A call whose item is
# not on disk yet is unknown, never a success: its ledger line waits until the session's next PostToolUse or Stop finds
# the item (_codex_settle), or CODEX_SETTLE_S passes and the call leaves with no line. A rollout that writes no such
# items (a Codex older than CODEX_RECORDS_SINCE) gets each line at once, as a success, as before Codex wrote them.
CODEX_SETTLE_S = 600
CODEX_TAIL = 4_000_000  # bytes at the end of the rollout searched for the items
CODEX_RECORDS_SINCE = (0, 167)  # the first Codex CLI that writes a CommandExecution item for each command


def codex_rollout(transcript) -> tuple:
    """({item id: CommandExecution item} in the last CODEX_TAIL bytes of a Codex rollout, whether the rollout is known
    to write those items: its tail holds an item record of any kind (the Codex writing it now writes them, also in a
    session another Codex started and this one resumed), or its session_meta names Codex CODEX_RECORDS_SINCE or
    later, or a source build (0.0.0)). ({}, False) when it cannot be read."""
    try:
        with open(transcript, "rb") as fh:
            first = fh.readline()
            size = fh.seek(0, 2)
            fh.seek(max(0, size - CODEX_TAIL))
            lines = fh.read().splitlines()
    except (OSError, TypeError, ValueError):
        return {}, False
    items, writer = {}, False
    for ln in lines:
        if not writer and b'"item_completed"' in ln:  # parsed until the first: a command's output may quote one
            writer = _item_record(ln)
        if b'"CommandExecution"' not in ln:
            continue
        try:
            rec = core.load_json(ln)
        except ValueError:
            continue
        p = rec.get("payload") if isinstance(rec, dict) and isinstance(rec.get("payload"), dict) else {}
        item = p.get("item") if rec.get("type") == "event_msg" and p.get("type") == "item_completed" else None
        if isinstance(item, dict) and item.get("type") == "CommandExecution" and item.get("id"):
            # the thread Codex ran it on, from its own record (_codex_thread_mismatch)
            items[str(item["id"])] = dict(item, thread_id=p["thread_id"]) if isinstance(p.get("thread_id"), str) \
                else item
    version = _codex_version(first)
    return items, bool(items) or writer or version >= CODEX_RECORDS_SINCE or version == (0, 0, 0)


def _item_record(line: bytes) -> bool:
    """Is a rollout line an item record (event_msg item_completed) that Codex wrote itself?"""
    try:
        rec = core.load_json(line)
    except ValueError:
        return False
    p = rec.get("payload") if isinstance(rec, dict) and isinstance(rec.get("payload"), dict) else {}
    return rec.get("type") == "event_msg" and p.get("type") == "item_completed" and isinstance(p.get("item"), dict)


def _codex_version(line: bytes) -> tuple:
    """The Codex CLI version a rollout's first record (its session_meta) names, as numbers; () when it names none."""
    try:
        rec = core.load_json(line)
    except ValueError:
        return ()
    meta = rec.get("payload") if isinstance(rec, dict) and rec.get("type") == "session_meta" else None
    version = meta.get("cli_version") if isinstance(meta, dict) else None
    return tuple(int(x) for x in re.findall(r"\d+", str(version))[:3]) if version else ()


def _rollout_once(read: dict, transcript) -> tuple:
    """codex_rollout of `transcript`, read once per hook: `read` keeps what this hook read."""
    key = str(transcript or "")
    if key not in read:
        read[key] = codex_rollout(key) if key else ({}, False)
    return read[key]


def codex_outcome(item):
    """True when a CommandExecution item succeeded, False when it failed or was declined, None while unknown."""
    if not isinstance(item, dict):
        return None
    code, status = item.get("exit_code"), item.get("status")
    if _codex_watch_control(item):
        return True
    if status in ("failed", "declined") or (isinstance(code, int) and not isinstance(code, bool) and code != 0):
        return False
    return True if status == "completed" or code == 0 else None


def _codex_watch_control(item: dict) -> bool:
    """A completed standalone wait's documented non-error outcome, never a shell's unrelated failure.

    Codex reports nonzero commands as failed even for watch's control-flow exits. Require both the literal wait
    invocation and its complete matching terminal notice; a declined call, parser error or compound shell command
    remains a failure. The rollout and its raw exit status are left intact.
    """
    from . import ciwatch, laneroute, shellparse, watch
    code, status, command = item.get("exit_code"), item.get("status"), item.get("command")
    if type(code) is not int or code not in (watch.EXIT["nothing"], watch.EXIT["timeout"], watch.EXIT["superseded"],
                                           ciwatch.EXIT["none"]) \
            or status not in ("completed", "failed"):
        return False
    if not isinstance(command, list) or len(command) != 3 or not all(isinstance(x, str) for x in command) \
            or os.path.basename(command[0]) not in ("sh", "bash", "zsh") or command[1] not in ("-c", "-lc"):
        return False
    if not shellparse.runs_one_command(command[2]):
        return False
    try:
        args = shlex.split(command[2])
    except ValueError:
        return False
    while args and re.fullmatch(r"DEVKIT_(?:SPEAKER|FROM_AGENT|SESSION)=[A-Za-z0-9_.:-]+", args[0]):
        args.pop(0)
    if len(args) < 3 or not laneroute.LITERAL_DEV.fullmatch(args[0]):
        return False
    ci = args[1:3] == ["ci", "watch"]
    if not ci and (args[1] != "watch" or code == ciwatch.EXIT["none"]):
        return False
    args = args[3:] if ci else args[2:]
    if "--wait" not in args:
        return False
    # A narrow literal argv: no wrappers, options the CLI rejects, expansion, substitution or extra programs.
    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
    flags = ("--wait", "--quiet", "--json") if ci else ("--wait", "--standby", "--json")
    values = ("--timeout", "--branch", "--sha", "--tag", "--pr", "-p", "--project") if ci else \
        ("--timeout", "--for", "--only")
    while args:
        arg = args.pop(0)
        key, eq, value = arg.partition("=")
        if key in flags and not eq:
            continue
        if key not in values:
            return False
        if not eq:
            if not args:
                return False
            value = args.pop(0)
        if key == "--timeout" and not re.fullmatch(number, value):
            return False
        if key == "--for" and value != "me":
            return False
        if key == "--only" and any(x.strip() not in watch.KINDS for x in value.split(",") if x.strip()):
            return False
    output = item.get("aggregated_output")
    if not isinstance(output, str):
        return False
    text = output.strip()
    if ci:
        return _codex_ci_watch_control(code, text, number)
    if code == watch.EXIT["superseded"]:
        return text == "watch: a newer wait of this session took over; this one ends"
    if code == watch.EXIT["nothing"]:
        return text in (
            "watch: the session ended",
            "watch: nothing can arrive for you: none of your questions waits for an answer. End the turn without a wait.",
            "watch: nothing can arrive for this session: no crew member works, no launch is pending, no step waits "
            "for a slot, no other session owes it an answer and no CI run is unfinished. End the turn without a wait.")
    arm = r"dev watch --wait(?: --for me)? --timeout \d+(?: --standby)?"
    return bool(re.fullmatch(
        r"watch: (?:nothing arrived in " + number + r" min|stopped before anything arrived|"
        r"this conversation goes on under session codex:[A-Za-z0-9_.:-]{1,12}…? "
        r"\(a compaction gave it a new id\)) · arm again: " + arm, text))


def _codex_ci_watch_control(code: int, text: str, number: str) -> bool:
    """Only CI watch's timeout/no-result notices; a pipeline failure or unexpected output stays a failure."""
    from . import ciwatch
    lines = text.splitlines()
    if not lines or any(not re.fullmatch(r"job .+ · .+ #\d+", line) for line in lines[:-1]):
        return False
    last = lines[-1]
    if code == ciwatch.EXIT["timeout"]:
        return bool(re.fullmatch(
            r"CI still running · .+ @ [0-9a-f]+(?: after " + number + r" min(?: \([^\n]*\))?)?"
            r": this watch (?:ended first|was stopped before the run ended); re-arm: dev ci watch --wait .+", last))
    if code == ciwatch.EXIT["none"]:
        return bool(re.fullmatch(r"CI none · .+ @ [0-9a-f]+: no pipeline run appeared for this commit(?: \([^\n]*\))?",
                                 last) or re.fullmatch(
            r"CI (?:skipped|neutral|stale)(?: and (?:skipped|neutral|stale))* · .+ @ [0-9a-f]+ · .+ · "
            + number + r" min", last))
    return False


def _codex_executed(item) -> bool:
    """Whether a command ran, independently of success; declined or pre-execution failures prove nothing."""
    if not isinstance(item, dict):
        return False
    code, status = item.get("exit_code"), item.get("status")
    return status == "completed" or status == "failed" and isinstance(code, int) and not isinstance(code, bool)


def _codex_call(agent: str, data: dict) -> tuple:
    """(outcome, context) of a PostToolUse: for a Codex command, its outcome from the rollout (codex_outcome) and the
    context of the session's earlier commands settled now (_codex_settle); (True, "") for any other call."""
    if agent != "codex" or data.get("tool_name") not in SHELL_TOOLS:
        return True, ""
    data["_codex_command_executed"] = False  # derived only from the rollout, never from the incoming payload
    data["_codex_lesson_failure"] = False
    read = {}  # the rollouts this hook read, once each
    try:
        settled = _codex_settle(agent, data, read=read)
    except Exception:
        _log(f"post-tool settle {agent}: {traceback.format_exc()[-300:]}")
        settled = ""
    try:
        items, writes = _rollout_once(read, data.get("transcript_path"))
        item = items.get(str(data.get("tool_use_id") or ""))
        _codex_thread_mismatch(agent, data, item)
        data["_codex_command_executed"] = _codex_executed(item)
        observed = (state.read(_codex_pending(str(data.get("session_id") or ""))) or {}).get("observed") or []
        data["_codex_lesson_failure"] = _codex_lesson_failure(item) and _codex_observation_key(data) not in observed
        outcome = codex_outcome(item) if item else (None if writes else True)
    except Exception:
        _log(f"post-tool outcome {agent}: {traceback.format_exc()[-300:]}")
        outcome = None
    return outcome, settled


def _codex_thread_mismatch(agent: str, data: dict, item):
    """A Codex exec whose dev command resolved its caller by the call's token (its use mark exists) ran on a thread that is
    not that caller's, by the rollout's own CommandExecution record: audited caller.mismatch, after the fact (a main
    token used in a member's write_stdin into the tech lead's exec shows here). The tech lead's thread is the
    session's; a member's is its own, or the session's where Codex shares it."""
    from . import identity, progress
    sid, tid = str(data.get("session_id") or ""), str(data.get("tool_use_id") or "")
    thread = str((item or {}).get("thread_id") or "") if isinstance(item, dict) else ""
    if agent != "codex" or not thread or not sid or not tid or not identity.used_path(sid, tid).exists():
        return
    caller = str(progress.call_entry(data).get("caller") or "main")[:80]
    if thread in ((sid,) if caller == "main" else (caller, sid)):
        return
    try:
        from . import audit
        audit.record("caller.mismatch", tid[:80], f"its dev command resolved {caller}, and Codex ran the command on "
                     f"thread {thread[:80]}", "flagged", tool_use_id=tid[:80], session=sid[:80], host_agent=agent,
                     thread=thread[:80])
    except Exception as e:
        _swallowed("post-tool", e)


def _codex_pending(sid: str) -> Path:
    return STATE / "codex-pending" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', sid)[:80]}.json"


def _codex_observation_key(data: dict) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([_lesson_agent(data), str(data.get("tool_use_id") or "")]).encode()).hexdigest()


def _codex_lesson_failure(item) -> bool:
    """Only a terminal executed nonzero result proves failure; declines and interruptions are advisory."""
    return (isinstance(item, dict) and item.get("status") in ("completed", "failed")
            and type(item.get("exit_code")) is int and item["exit_code"] != 0 and codex_outcome(item) is False)


def _codex_defer(agent: str, data: dict):
    """A Codex command whose CommandExecution item is not on disk yet: what its ledger line and its root-cause
    paragraph need, kept for _codex_settle. Its output stays out (it may hold secrets): only its error key
    (_call_keys), devkit's own marker lines (the ledger's codes, the registry lines) and the ids of the lessons it
    recalls; the command is kept with its secrets redacted, for the tech lead's notice."""
    sid = str(data.get("session_id") or "")
    if not sid:
        return
    from . import audit, bugs
    keep = {k: data.get(k) for k in ("session_id", "tool_name", "tool_use_id", "agent_id", "cwd", "transcript_path")}
    keep.update(tool_input={"command": audit.redact(_command_of(data))[:4000], "workdir": _shell_cwd(data)},
                tool_response="\n".join(m.group(0) for m in bugs.MARKER_RE.finditer(_output_text(data))),
                keys=list(_call_keys(data, False)), lessons=[x["meta"]["id"] for x in _recall_quietly(agent, data)],
                lesson_evidence=_deferred_lesson_evidence(agent, data),
                ts=time.time())

    def remember(doc):
        calls = doc.get("calls") or []
        if _codex_observation_key(data) in (doc.get("observed") or []):
            return state.SKIP
        if any(c.get("tool_use_id") == keep.get("tool_use_id") and c.get("agent_id") == keep.get("agent_id")
               for c in calls):
            return state.SKIP  # a replay cannot replace eligibility with this call's own advisory exposure
        doc["calls"] = calls[-49:] + [keep]
    state.update(_codex_pending(sid), remember)


def _deferred_lesson_evidence(agent: str, data: dict) -> dict:
    """Snapshot matched IDs and earlier exposure before pending output advice. Never retain output or rule text."""
    from . import lessonevents, lessons
    sess, text = _session_record(agent, data), _output_text(data)
    project, sid, who = str(sess.get("project") or ""), str(data.get("session_id") or ""), _lesson_agent(data)
    if not project or not sid or _headless():
        return {}
    rules = _matching_output_rules(project, text)[:LESSON_HITS_MAX]
    found = lessons.for_output(project, text, limit=LESSON_HITS_MAX, initiatives=_initiatives(project))
    ids = list(dict.fromkeys([r["lesson"] for r in rules] + [x["meta"]["id"] for x in found]))
    ids = [lid for lid in ids if re.fullmatch(r"L-[0-9A-Za-z]{1,40}", lid)][:LESSON_HITS_MAX]
    same = set((sess.get("lessons_by_call") or {}).get(str(data.get("tool_use_id") or "")) or [])
    shown = {e["id"] for e in lessonevents._events(sid)
             if e.get("agent") == who and e.get("event") == "shown"}
    return {"project": project, "eligible": [lid for lid in ids if lid in shown and lid not in same],
            "rules": [[r["id"], r["lesson"]] for r in rules
                      if r["lesson"] in ids and re.fullmatch(r"LR-[0-9a-f]{10}", r["id"])]}


def _settled_lesson_hits(data: dict):
    """Apply the deferred call's bounded matches using its original exposure, lane, and source identity."""
    from . import lessonevents, lessonrules
    evidence = data.get("lesson_evidence") or {}
    if not evidence or _headless():
        return
    eligible = evidence.get("eligible") or []
    missed = lessonevents.on_hit(eligible, str(data.get("session_id") or ""), _lesson_agent(data),
                                source_event=str(data.get("tool_use_id") or ""),
                                project=str(evidence.get("project") or ""))
    for rid, lid in evidence.get("rules") or []:
        lessonrules.record_hit(rid, lid in missed)


def _codex_settle(agent: str, data: dict, loop: bool = True, read: dict = None) -> str:
    """The session's deferred Codex commands (_codex_defer) whose CommandExecution item is in their rollout now get
    their ledger line; a failure counts toward the patch-loop stop, whose root-cause paragraph comes back when it is
    due. Eligible lesson and rule matches settle on a proven failure even when `loop` is False (Stop, which has no
    context to give): the agent is not marked told and no lesson counts as shown; the next failure counts the
    streak from the ledger. One still missing after
    CODEX_SETTLE_S leaves with no line and a hooks.log entry. Only the calls of this hook's own agent settle here
    (the same agent_id, or none for the main session): Codex members share the main session's session_id, and a
    paragraph given in another agent's hook would reach the wrong agent. Each rollout is read once, before the
    pending record's lock (`read`: the rollouts the hook read already). -> the context to give."""
    sid = str(data.get("session_id") or "")
    f = _codex_pending(sid) if sid else None
    if not f or not f.exists():
        return ""
    settled, now, who = [], time.time(), str(data.get("agent_id") or "")
    read = {} if read is None else read
    mine = [c for c in (state.read(f) or {}).get("calls") or [] if str(c.get("agent_id") or "") == who]
    if not mine:
        return ""
    for c in mine:
        _rollout_once(read, c.get("transcript_path"))

    def take(doc):
        keep = []
        observed = doc.get("observed") or []
        for c in doc.get("calls") or []:
            if str(c.get("agent_id") or "") != who:  # another agent's call: it waits for that agent's hooks
                keep.append(c)
                continue
            got = read.get(str(c.get("transcript_path") or ""))  # None: deferred after the read, for the next hook
            item = got[0].get(str(c.get("tool_use_id") or "")) if got else None
            outcome = codex_outcome(item)
            if outcome is None and now - (c.get("ts") or 0) < CODEX_SETTLE_S:
                keep.append(c)
            else:
                settled.append((c, outcome, _codex_executed(item), _codex_lesson_failure(item)))
                key = _codex_observation_key(c)
                observed = [k for k in observed if k != key] + [key]
        doc["calls"] = keep
        doc["observed"] = observed[-CALLS_KEPT:]
    state.update(f, take, create=False)
    out = []
    for c, outcome, executed, failed in settled:
        if outcome is None:
            _log(f"post-tool {agent}: no CommandExecution record for {c.get('tool_use_id')} after {CODEX_SETTLE_S} s; "
                 f"the call leaves the ledger unsettled")
            continue
        if outcome or executed:
            try:
                _codex_bind_tool_project(agent, c)
            except Exception as e:
                _swallowed("post-tool-project", e)
        key, sig = c.get("keys") or _call_keys(c, False)  # computed when the call was deferred, from its output
        keys = (key, "" if outcome else sig)
        _output_bugs(agent, c, outcome, keys)
        if failed:
            _settled_lesson_hits(c)
        if loop and not outcome and _patch_loop(agent, c, keys):
            found = _recalled(agent, c, c.get("lessons") or [])
            try:
                _shown(c, [x["meta"]["id"] for x in found], "output")
            except Exception as e:
                _swallowed("post-tool", e)
            out.append(_root_cause(c, found))
    return "\n\n".join(out)


def _recall_quietly(agent: str, data: dict) -> list:
    """Display-only recall for a deferred call's later root-cause paragraph; it never counts a hit."""
    try:
        project = str(_session_record(agent, data).get("project") or "")
        from . import lessons
        return lessons.for_output(project, _output_text(data), limit=LOOP_SHOW, initiatives=_initiatives(project)) \
            if project else []
    except Exception as exc:
        _swallowed("lessons", exc)
        _log(f"settle recall {agent}: {traceback.format_exc()[-300:]}")
        return []


def _recalled(agent: str, data: dict, ids: list) -> list:
    """The lessons `ids` (a deferred call's recall, _codex_defer) as the recall pool has them now, in that order."""
    if not ids:
        return []
    try:
        project = str(_session_record(agent, data).get("project") or "")
        from . import lessons
        pool = {x["meta"]["id"]: x for x in lessons.recall_pool(project, _initiatives(project))} if project else {}
    except Exception as exc:
        _swallowed("lessons", exc)
        _log(f"settle lessons {agent}: {traceback.format_exc()[-300:]}")
        return []
    return [pool[i] for i in ids if i in pool]


def _output_pitfalls(agent: str, data: dict, take: int = 0, taken: list = None, *, failed=False) -> str:
    """PostToolUse and PostToolUseFailure: the lesson rules whose error text the tool's output shows, then the open
    lessons whose error signature it shows. Only an observed failed call can mark a previously shown lesson missed;
    successful or pending output can quote historical errors without repeating them. `take`:
    the best that many lessons go to `taken` instead, for the root-cause paragraph of this call (_root_cause)."""
    try:
        sess, text = _session_record(agent, data), _output_text(data)
        project = str(sess.get("project") or "")
        if not project or not text.strip():
            return ""
        from . import lessons
        same_call = set((sess.get("lessons_by_call") or {}).get(str(data.get("tool_use_id") or "")) or [])
        rules, said = _output_rules(data, project, text, same_call, failed=failed)
        found = [x for x in lessons.for_output(project, text, limit=LESSON_HITS_MAX, initiatives=_initiatives(project))
                 if x["meta"]["id"] not in said]
        if failed:
            _lesson_hits(data, [x["meta"]["id"] for x in found if x["meta"]["id"] not in same_call], project=project)
        if take and taken is not None:
            taken.extend(found[:take])
            found = found[take:]
        return "\n\n".join(x for x in (rules, _pitfalls(agent, data, found[:PITFALL_SHOW], channel="output")) if x)
    except Exception as exc:
        _swallowed("lessons", exc)
        _log(f"pitfalls {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _matching_output_rules(project: str, text: str) -> list:
    """Active output rules in the project's scopes, independent of the context display budget."""
    from . import lessonrules
    scopes = lessonrules.all_scopes()
    if not scopes or _headless():
        return []
    slugs = [s.split("/", 1)[1] for s in scopes if s.startswith("initiatives/")]
    if slugs:
        from .notes import initiative_members
        slugs = [s for s in slugs if project in initiative_members(s)]
    return lessonrules.check_output(text, lessonrules.scopes_for(project, slugs))


def _output_rules(data: dict, project: str, text: str, same_call=(), *, failed=False) -> tuple:
    """Show matching output rules; only observed failures record hits and misses of earlier exposure."""
    from . import lessonrules
    rules = _matching_output_rules(project, text)
    if not rules:
        return "", set()
    from . import lessonevents, lessons
    sid, who, lines, said = str(data.get("session_id") or ""), _lesson_agent(data), [], set()
    for r in rules[:PITFALL_SHOW]:
        lid = r["lesson"]
        missed = failed and bool(sid) and lid not in same_call and lid in lessonevents.on_hit(
            [lid], sid, who, source_event=str(data.get("tool_use_id") or ""), project=project)
        if failed:
            lessonrules.record_hit(r["id"], missed)
        _shown(data, [lid], "output")
        lines.append(f"devkit · lesson rule {r['id']} matches this output, from {lessonrules.describe(r)}.")
        said.add(lid)
    return "\n".join(lines + [lessons.ADVICE]), said


def _shell_texts(inp) -> list:
    """The shell commands a tool input carries as plain words, for the rules that read words (command_words): its
    `command`, then the `cmd` Codex's exec_command sends instead, each once; a list joined word by word without
    quotes, so the words of `["bash", "-lc", "dev push"]` stay readable. The shell reader's parsers take
    shellparse.tool_commands (_shell_inputs) instead."""
    out = []
    for key in ("command", "cmd"):
        v = inp.get(key) if isinstance(inp, dict) else None
        v = " ".join(str(x) for x in v) if isinstance(v, list) else v
        if isinstance(v, str) and v and v not in out:
            out.append(v)
    return out


def _command_of(data: dict) -> str:
    """The first shell command a call carries, as plain words (_shell_texts: `command`, then Codex's `cmd`)."""
    return next(iter(_shell_texts(data.get("tool_input"))), "")


def _shell_inputs(data: dict) -> list:
    """Each shell command a shell call carries, as the shell reader reads them (shellparse.tool_commands: `command`, then the
    `cmd` Codex's exec_command sends instead; a list quoted word by word) -> [(key, command)], the key the input
    holds it under, for a rewrite ('' for a list, which no rewrite replaces). [] for a call that is no shell call."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return []
    from . import shellparse
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    return [(next((k for k in ("command", "cmd") if inp.get(k) == c), ""), c) for c in shellparse.tool_commands(inp)]


def _shell_cwd(data: dict) -> str:
    """The folder a shell call runs in: Codex's `workdir` (relative to the session's folder); else the
    payload's cwd."""
    cwd = data.get("cwd") or os.getcwd()
    inp = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    wd = inp.get("workdir")
    if isinstance(wd, str) and wd:
        from . import shellparse
        return str(shellparse.resolve(wd, cwd))
    return cwd


def _interrupted(data: dict) -> bool:
    resp = data.get("tool_response")
    return bool(data.get("is_interrupt")) or (isinstance(resp, dict) and bool(resp.get("interrupted")))


def _call_keys(data: dict, ok: bool) -> tuple:
    """(command key, error key) of a shell call for the patch-loop count (bugs.command_key, bugs.error_signature);
    the error key only for a failure that was not an interruption. ('', '') for any other tool."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return "", ""
    from . import bugs
    key = bugs.command_key(command_words(_command_of(data)))
    if ok or not key or _interrupted(data):
        return key, ""
    return key, bugs.error_signature(_output_text(data))


def _output_bugs(agent: str, data: dict, ok: bool, keys=None, ledger: bool = True) -> str:
    """PostToolUse (ok) and PostToolUseFailure: the call's line in the session ledger of devkit's bug registry, and,
    for a marker of devkit's own failure in the output, the known detour or the new-bug paragraph. Also outside
    projects; a marker that names no occurrence of this session does nothing. `keys`: the call's (command key,
    error key) when the caller has them (_call_keys); `ledger` False: no line yet (a Codex command whose outcome is
    still unknown, _codex_defer)."""
    sid = str(data.get("session_id") or "")
    if not sid:
        return ""
    try:
        from . import bugs
        text = _output_text(data)
        cmd = _command_of(data)
        codes = bugs.marker_codes(text)
        ok = ok and not _interrupted(data)
        if ledger:
            cmd_key, sig = keys if keys is not None else _call_keys(data, ok)
            bugs.ledger_append(sid, data.get("tool_name") or "", ok, bugs.dev_words(command_words(cmd)), codes,
                               cmd_key=cmd_key, sig=sig, who=_lesson_agent(data))
        return bugs.context_for_output(text, sid) if codes else ""
    except Exception:
        _log(f"bugs {agent}: {traceback.format_exc()[-300:]}")
        return ""


# The patch-loop stop. An agent whose command fails twice in a row with the same error is told once to find the root
# cause before it changes more code; a crew member that fails a third time is reported to the tech lead once.
ROOT_CAUSE_STOP = ("The same failure twice. Stop changing code and find the root cause first (the root-cause skill): "
                   "reproduce it, check the known lessons and devkit bugs below, prove the cause, then fix.")
NOTHING_KNOWN = "No known lesson or devkit bug matches this error yet."
LOOP_STOP_AT = 2  # identical failures in a row that stop the agent
LOOP_LEAD_AT = 3  # identical failures in a row in a crew lane that the tech lead hears about
LOOP_SHOW = 3  # lessons, and devkit bugs, the paragraph lists


def _patch_loop(agent: str, data: dict, keys: tuple) -> bool:
    """PostToolUseFailure of a shell call, after its ledger line: how many times in a row this agent's command
    failed with this error (bugs.failure_streak). The third time in a crew member's lane, once: a notice to the tech
    lead (_tell_lead). -> True the second time, once per command and agent in the session: the root-cause paragraph
    is due (_root_cause)."""
    cmd_key, sig = keys
    sid = str(data.get("session_id") or "")
    if not (sid and cmd_key and sig):
        return False
    try:
        from . import bugs
        who = _lesson_agent(data)
        count, told = bugs.failure_streak(sid, who, cmd_key, sig)
        stop = count >= LOOP_STOP_AT and "agent" not in told
        if stop:
            bugs.note_loop(sid, who, cmd_key, "agent")
        if count >= LOOP_LEAD_AT and data.get("agent_id") and "lead" not in told and _tell_lead(agent, data):
            bugs.note_loop(sid, who, cmd_key, "lead")
        return stop
    except Exception:
        _log(f"patch loop {agent}: {traceback.format_exc()[-300:]}")
        return False


def _root_cause(data: dict, found: list) -> str:
    """The root-cause paragraph: ROOT_CAUSE_STOP, then the lessons whose error anchor the output shows (`found`, the
    best LOOP_SHOW of the output recall) and the registry's devkit bugs the output shows (bugs.registry_lines), or
    NOTHING_KNOWN."""
    from . import bugs, lessons
    try:
        known = bugs.registry_lines(_output_text(data), str(data.get("session_id") or ""), limit=LOOP_SHOW)
    except Exception:
        _log(f"patch loop bugs: {traceback.format_exc()[-300:]}")
        known = []
    lines = [ROOT_CAUSE_STOP] + lessons.pitfall_lines(found) + known
    return "\n".join(lines + ([] if found or known else [NOTHING_KNOWN]))


def _tell_lead(agent: str, data: dict) -> bool:
    """A crew member's third identical failure: one notice from devkit to the tech lead in the run's messages,
    naming the member, the command, and how to reach it. -> posted (False when the run has no lane for it)."""
    from . import crew, progress
    sid, aid = str(data.get("session_id") or ""), str(data.get("agent_id") or "")
    with progress._tx(agent, sid):
        run = progress.load_run(agent, sid)
        if not isinstance(((run or {}).get("agents") or {}).get(aid), dict):
            return False
        run = progress.normalize(run)
        lane = run["agents"][aid]
        mem = crew._for_lane(lane)
        who = crew.who(mem) if mem else crew.who(lane.get("persona") or lane.get("label") or "A crew member")
        ask = f'dev crew ask --to {mem["id"]} "…"' if mem else 'dev crew ask --to <member> "…"'
        progress._post(run, "devkit", "main",
                       f"devkit: {who} hit the same failure a third time: `{_inline(_command_of(data))}` failed again "
                       f"with the same error after devkit told it to stop and find the root cause. Check on it "
                       f"({ask}), or stop it and route the debugging (`dev crew raci debug` names who takes it).",
                       time.time(), from_name="devkit", kind="notice")
        progress._save(run, touch_session=False)
    return True


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")
AGENT_LAUNCHES = ("Agent", "Task", "spawn_agent")
ADVICE = "devkit · advice: "  # the head of each advice line the PreToolUse hook adds to a call's context


COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+(\S+)\s+)?commit\b|\bdev\s+commit\b")
# the options of `dev commit` that take a value (gitx.register): the word after one is its value, never an option
_COMMIT_VALUES = frozenset({"-m", "--message", "--why", "--cause", "--body", "--verified", "--refs", "--breaking"})


def _wip_commit(cmd: str) -> bool:
    """True when a command runs `dev commit --wip`, which commits unfinished work on purpose: the option among dev's
    own options (before a `--`), never a message's text."""
    from . import shellparse
    for argv0, _ in shellparse.split_commands(cmd or "") or []:
        args = shellparse.dev_args(shellparse.strip_prefix(list(argv0))) or []
        if args[:1] != ["commit"]:
            continue
        opts, i = (args[:args.index("--")] if "--" in args else args), 1
        while i < len(opts):
            if opts[i] == "--wip":
                return True
            i += 2 if opts[i] in _COMMIT_VALUES else 1
    return False


def _stub_commit_advice(data: dict) -> list:
    """A commit that would include unfinished work (stubs, placeholders, TODOs without a task): stubs.commit_gate's
    finding, as advice. `dev commit --wip` commits such work on purpose, so it gets none."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return []
    out = []
    for cmd in _shell_texts(data.get("tool_input")):  # each command the call carries (Bash's command, Codex's cmd)
        m = COMMIT_RE.search(command_words(cmd))  # a commit the command runs, not the text it writes
        if not m or _wip_commit(cmd):
            continue
        cwd = _shell_cwd(data)  # Codex's workdir, if any: the repository the commit runs in
        where = m.group(1) or cwd
        top = core.gitout(where if os.path.isabs(where) else os.path.join(cwd, where), "rev-parse", "--show-toplevel")
        if not top:
            continue
        from . import stubs
        found = stubs.commit_gate(top, cmd)
        if found:
            out.append(found)
    return out


def _edit_paths(agent: str, data: dict) -> list:
    """The files an edit call writes, each once: a file tool's file_path or notebook_path, or the files a patch adds,
    updates or deletes (Codex's apply_patch, read as Codex reads it)."""
    from . import shellparse
    inp = data.get("tool_input") or {}
    paths = [inp.get("file_path") or inp.get("notebook_path") or ""]
    if data.get("tool_name") == "apply_patch" or not paths[0]:
        body = str(inp.get("input") or inp.get("patch") or inp.get("command") or "")
        paths = ([p for _, p in shellparse.parse_patch_paths(body)] if agent == "codex" else
                 re.findall(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", body)) or paths
    return [p for p in dict.fromkeys(paths) if p]


def _crew_limits_advice(agent: str, data: dict) -> list:
    """The crew's coordination facts for this call, as advice:
    - a launch past the crew's slots (progress.launch_verdict): `progress.max_parallel` (default 4) under the team's
      ceiling, lowered step by step as the plan's 5-hour or weekly usage rises (progress.quota_steps), each launch
      costing its progress.slot_weights, the helpers members launch included, progress.reserve kept for the
      developer's own request, and no launch deeper than progress.max_depth;
    - the main session editing a file a working crew member owns, in another live run of the repository or in this
      session's run. A member that ended or went stale holds nothing; progress.scope_holders decides,
      for this advice and for `dev crew owner` alike."""
    from . import progress
    sid = data.get("session_id") or "-"
    tool = data.get("tool_name")
    if tool in EDIT_TOOLS and not data.get("agent_id"):
        cwd = data.get("cwd") or os.getcwd()
        out = []
        for path in _edit_paths(agent, data):
            h = progress.scope_holder(path, agent, sid, cwd)
            if h:
                out.append(progress.holder_refusal(h, path, cwd))
        return out
    if tool not in AGENT_LAUNCHES:
        return []
    progress.note_midturn(agent, data)  # a request typed mid-turn may take the slot kept for the developer
    run = progress.load_run(agent, sid)
    if not run or run.get("state") == "done":
        return []
    run = progress.normalize(run)
    progress.expire_stale(run)
    why = progress.launch_verdict(agent, data, run)  # at every depth: a member's helper takes a slot too
    return [why] if why else []


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


def _scope_free(agent: str, data: dict, path: str, layers: list, member: str, run: dict, top: str) -> bool:
    """True when a crew member's write needs no step scope and no write tool level: a scratch file (in a temp folder
    outside the repository and outside any checkout: the session's scratchpad, its registered Codex scratch folder),
    or a plan of its run's project in the handoff repository that every layer of its step scope names. That is how a
    review-only member such as The Oracle writes the spec it was asked for."""
    from . import codexlaunch, lanescope
    cwd = data.get("cwd") or os.getcwd()
    ap = os.path.normpath(os.path.join(str(cwd), str(path)))
    if agent == "codex":
        with codexlaunch.codex_scratch_context(agent, data):
            if codexlaunch.app_scratch_allows(ap, member):
                return True
    if core.is_temp(ap) and not core.is_under(ap, top) and not _repo_top_of(ap):
        return True
    project = str((run or {}).get("project") or "")
    if not project or not layers or not core.is_under(ap, core.HANDOFF / "plans" / project):
        return False
    return all(lanescope.scope_holds(ap, cwd, layer, base=top) for layer in layers)


def _lane_scope_miss(path: str, cwd: str, top: str, layers: list, own_checkout: bool = False):
    """Why a crew lane's step scope (`layers`, counted from its repository `top`) does not hold a file write to
    `path`, or None when it does. Each layer must hold the path as written (lanescope.scope_holds) and the file the write
    reaches, links resolved: a path that does not exist yet by its nearest existing folder, and a `..` after a link
    from the link's target, as the system resolves it. So a link inside the scope that leads out of the repository,
    or into its files that the scope does not name, holds nothing. A path in another checkout of the same repository
    counts from that checkout, unless the scope was given in `top` (`own_checkout`: it holds that checkout's files
    only). An empty layer (a crew lane that runs no step, progress.lane_scope) holds nothing.
    -> (the path relative to the repository, where it lands when a link takes it out of the scope, else '')."""
    from . import lanescope
    raw = path if os.path.isabs(path) else os.path.join(cwd, path)
    ap = os.path.normpath(raw)
    base = top
    # a worktree of the same repository: the scope counts from it, unless it was given in one checkout
    if not own_checkout and lanescope.scope_rel(ap, cwd, top).startswith(".."):
        wt = _repo_top_of(ap)
        if wt and _same_repository(wt, top):
            base = wt
    rel = lanescope.scope_rel(ap, cwd, base)

    def holds(p):
        return all(lanescope.scope_holds(p, cwd, layer, base=base) for layer in layers)
    for lands in dict.fromkeys((os.path.realpath(raw), os.path.realpath(ap))):  # `..` read physically, then lexically
        inside = os.path.relpath(lands, os.path.realpath(base))
        if inside != rel and not holds(lands):  # a link takes the write out of the scope
            return rel, (core.tilde(lands) if inside.startswith("..") else inside)
    return None if holds(ap) else (rel, "")


# What an edit by a crew agent that devkit cannot place on a step is told: its edits count for no step
CODEX_UNVERIFIED = ("devkit has not verified this Codex child's launch, so its edits count for no crew step. If this "
                    "stays after its native launch records arrive, report it.")
OVER_LIMIT = ("devkit recorded this launch over the crew's limit (Codex cannot stop it before it starts): stop now and "
              "report, and the tech lead decides who does this work and when.")
BINDING_ADVICE = {
    "conflict": "this agent claims a launch that another agent is bound to, so its edits count for no crew step. Report "
                "what you change, and the tech lead checks the launch (dev crew graph).",
    "pending": "devkit has not matched this agent to the launch that started it yet, so its edits count for no crew "
               "step. It binds at its next tool call when its launch is known; if this stays, report what you change, "
               "and the tech lead assigns you your step (dev progress assign).",
    "unbound": "devkit has no record of the launch that started this agent, so its edits count for no crew step. "
               "Report what you change; the tech lead can assign you your step (dev progress assign \"<step>\" --to "
               "\"<you>\").",
}


def _scope_advice(agent: str, data: dict) -> list:
    """A crew member's file edit that its lane does not cover, as advice: an edit by a review-only or
    read-only lane, one by a lane that runs no step (it holds no file), or one outside its step's scope (a link that
    leads out of it included), which counts from the run's repository. Scratch files and the plans its scope names
    are never a miss (_scope_free). An edit by an agent devkit cannot place on a step (its launch unverified,
    unbound, claimed twice, or over the crew's limit) is named too. A helper a member launches works within the member's tool level and scope, so its advice names
    that member."""
    aid, tool = data.get("agent_id"), str(data.get("tool_name") or "")
    if not aid or tool not in EDIT_TOOLS:
        return []
    from . import lanescope, progress
    run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
    if not lane:
        return [CODEX_UNVERIFIED] if agent == "codex" else []
    if lane.get("over_limit"):  # a Codex launch over the crew's limits, told to stop (codexlaunch)
        return [OVER_LIMIT]
    if lane.get("binding") in BINDING_ADVICE:
        return [BINDING_ADVICE[lane["binding"]]]
    cwd = data.get("cwd") or os.getcwd()
    # a scope given in one checkout counts from it (progress._scope_checkout); a helper's, from its member's
    home = next((x.get("checkout") for x in ((run["agents"].get(k) or {}) for k in progress.lane_chain(run, aid))
                 if x.get("checkout")), "")
    top = home or lanescope.scope_base((run or {}).get("project"), cwd)
    layers = progress.lane_scope(run, aid)
    member = _member_folder(run, aid)
    paths = [p for p in _edit_paths(agent, data) if not _scope_free(agent, data, p, layers, member, run, top)]
    if not paths:
        return []
    chain = [run["agents"][k] for k in progress.lane_chain(run, aid)]
    name = _who(lane.get("persona") or lane.get("label") or "this crew member")
    head = _who(chain[-1].get("persona") or chain[-1].get("label") or "its crew member")
    if len(chain) > 1:  # a helper: the limits it works within are its member's
        name = f"this helper of {head}"
    level = progress.lane_tools(run, aid)
    if level in ("review", "read"):
        return [f"{name} is {'review-only' if level == 'review' else 'read-only'}"
                f"{f', like {head}' if len(chain) > 1 else ''}: report the change with file:line and the fix, and "
                f"the tech lead assigns it."]
    if not layers:
        return []
    where = " and ".join(", ".join(g) for g in layers)
    out = []
    for p_ in paths:
        miss = _lane_scope_miss(p_, cwd, top, layers, own_checkout=bool(home))
        if not miss:
            continue
        rel, lands = miss
        if not all(layers):  # its lane, or its member's, runs no step now: it holds no file
            runs = f"{head}'s lane runs" if len(chain) > 1 else "your lane runs"
            out.append(f"{rel} is not {name}'s to change: {runs} no step, so it holds no file (another crew member "
                       f"may own this one). Ask the tech lead to assign you a step: dev crew ask \"which step is "
                       f"mine? I need to change {rel} because …\"")
        elif lands:
            out.append(f"{rel} leads through a link to {lands}, which is outside {name}'s scope ({where}): the write "
                       f"lands there. Write to a path in your scope that is not a link, or ask the tech lead: "
                       f"dev crew ask \"may I change {lands}? why: …\"")
        else:
            out.append(f"{rel} is outside {name}'s scope ({where}): another crew member may own it. Ask the tech "
                       f"lead to widen it: dev crew ask \"may I change {rel}? why: …\"")
    return out


UNROUTED_LANE_CALL = ("devkit could not route the dev progress, dev crew or dev watch call in this command to your lane, "
                      "so devkit cannot tell who runs it: a change (a step, a note, a message, an answer) is refused "
                      "rather than made as the tech lead, and a read may name nobody. Run that call alone, as a "
                      "command of its own.")


def _unroutable_lane_advice(agent: str, data: dict) -> list:
    """A crew member's shell command that runs a `dev progress` or `dev crew` lane call devkit could not route or
    stamp to its lane (called only when neither did, and Codex's gate turned no rewrite down: _pre_tool then gives
    UNROUTED_LANE_CALL itself): devkit cannot name that call's caller, so its changes are refused, and the member is
    told."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return []
    from . import laneroute
    cwd = _shell_cwd(data)
    return [UNROUTED_LANE_CALL] if any(laneroute.holds_lane_call(cmd, cwd) for _, cmd in _shell_inputs(data)) else []


def _advice(agent: str, name: str, fn) -> list:
    """One advice source's lines; a failure loses only its own advice (logged)."""
    try:
        return [x for x in fn() if x]
    except Exception:
        _log(f"pre-tool advice {name} {agent}: {traceback.format_exc()[-300:]}")
        return []


def _routed(agent: str, name: str, data: dict, fn, held: list):
    """One routing step's new tool input, or None. A failure loses only its own rewrite (logged), and so does a Codex
    rewrite that _codex_may_rewrite turns down, whose step's name goes to `held`: the call then runs as it is, and
    still gets its advice and, for a crew member, the unroutable-lane advice."""
    try:
        new = fn()
    except Exception:
        _log(f"pre-tool route {name} {agent}: {traceback.format_exc()[-300:]}")
        return None
    if new is not None and agent == "codex" and not _codex_may_rewrite(data, new):
        _log(f"pre-tool route {name} {agent}: not rewritten, since Codex asks and the call is more than one plain "
             f"devkit call")
        held.append(name)
        return None
    return new


def _post_codex_advice(agent: str, data: dict, advice: list, unroutable: bool):
    """Codex reads no PreToolUse context, so a Codex crew member's advice goes to its lane as devkit's notice, which
    its next PostToolUse delivers (progress.on_tool_done); a notice still waiting there with the same text is not
    posted again. A lane call devkit could not route is copied to the tech lead, since it ran as the main session.
    Never raises (logged)."""
    from . import progress
    sid, aid = str(data.get("session_id") or ""), str(data.get("agent_id") or "")
    if agent != "codex" or not aid or not sid or not advice:
        return
    try:
        with progress._tx(agent, sid):
            run = progress.load_run(agent, sid)
            if not run:
                return
            run = progress.normalize(run)
            now = time.time()
            waiting = {m.get("text") for m in run.get("messages") or [] if isinstance(m, dict)
                       and m.get("to") == aid and m.get("from") == "devkit" and not m.get("delivered")}
            for line in advice:
                if line not in waiting:
                    progress._post(run, "devkit", aid, line, now, from_name="devkit", kind="notice")
            if unroutable:
                lane = (run.get("agents") or {}).get(aid) or {}
                who = _who(lane.get("persona") or lane.get("label") or "A crew member")
                progress._post(run, "devkit", "main",
                               f"devkit: {who} ran `{_inline(_command_of(data))}`, whose dev progress, dev crew or dev "
                               f"watch call devkit could not route to its lane, so it ran as the main session. Check "
                               f"that it closed no step of yours (dev progress reopen repairs one).", now,
                               from_name="devkit", kind="notice")
            progress._save(run, touch_session=False)
    except Exception:
        _log(f"pre-tool codex advice {agent}: {traceback.format_exc()[-300:]}")


def _launchable_steps(run: dict, lane: dict) -> list:
    """The steps a new launch of the member in `lane` can take, as progress._bind_step selects them: assigned to that
    member and still with the tech lead, or left unverified by one of its member's lanes that ended."""
    from . import progress as pg
    plan, lanes = run.get("plan") or [], run.get("agents") or {}
    mine = [st for st in plan if st.get("label") and st.get("state") not in pg.DONE
            and (st.get("owner") or "main") == "main" and pg._label_matches(st["label"], lane)]
    return mine + [st for st in plan if st.get("state") == "unverified" and st.get("label")
                   and (lanes.get(st.get("owner")) or {}).get("state") in pg.ENDED_LANE + ("unverified",)
                   and pg._label_matches(st["label"], lane)]


def _named_steps(label: str, steps: list) -> tuple:
    """Which of `steps` the launch description `label` names, scored as progress._bind_step scores it
    (progress._name_score on its task text). -> (binds, tied): `binds` the step it takes, or every review it names
    equally (one reviewer's launch holds them together); `tied` the steps it names equally otherwise. Both empty
    when it names none."""
    from . import approvals, progress as pg
    scored = [(pg._name_score(pg._task_text(label), st["name"]), st) for st in steps]
    best = max(sc for sc, _ in scored)
    top = [st for sc, st in scored if sc == best]
    if not best[1]:
        return [], []
    if len(top) == 1 or all(approvals.is_review(st) for st in top):
        return top, []
    return [], top


def _naming_description(label: str, name: str, who: str, steps: list) -> str:
    """The launch description that names step `name` alone among `steps`: the step's name put in front of the
    description's task text ("Mouse: visual tests" -> "Mouse: v23-qa visual tests", progress._task_text keeps the
    member's prefix), or, when that still names another step as well, the member's name and the step's alone
    ("Mouse: v23-qa"). '' when neither does."""
    from . import progress as pg
    task = pg._task_text(label).strip()
    prefix = label.partition(":")[0].strip() if task != (label or "").strip() else ""
    body = name if pg._words(task) <= pg._words(name) else f"{name} {task}"
    for text in (f"{prefix}: {body}" if prefix else body, f"{prefix or who}: {name}"):
        binds, _ = _named_steps(text, steps)
        if [st["name"] for st in binds] == [name]:
            return text
    return ""


def _launch_naming(agent: str, data: dict) -> tuple:
    """A crew launch from the main session, checked before it starts with the matcher that binds a launch to a step
    once it runs (progress._bind_step): a step binds only to the launch whose description names it, so a launch that
    names none of its member's steps starts with no step and has to be stopped and launched again.
    - It names none, and exactly one of its member's steps waits for a launch (pending with the tech lead, its
      dependencies done, queued for no lane): the call's new input, with that step's name in the description, and
      the line that tells the tech lead. The launch record (progress.note_agent_launch) takes the new description,
      so the agent binds that step when it starts. Recorded in the audit trail (crew.launch-named).
    - It names none and several wait, or it names several equally: no new input, and the line that names them with
      the exact description that takes each.
    - It names one (or every review it names, which one reviewer's launch holds together), its member has no step
      to take, or no step waits for it: (None, []).
    - It names one of its member's steps that is done best, and none of the steps waiting for its launch (a
      follow-up of finished work): (None, []), so it starts unchanged and takes no step. Its description is scored
      against the member's done steps too. When it names a waiting step as well, it is scored by the waiting steps
      alone, as above.
    A step waits for a launch only when its intent binds it to no lane (progress._queued_lanes, the one reader).
    Claude Code's Agent and Task calls only, launched by the main session with a devkit crew member's subagent_type.
    Codex is left as it is: it applies updatedInput only to a shell command (_codex_may_rewrite), and its spawn's
    task_name, not a description, selects the step (progress._native_launch_label, codexlaunch)."""
    inp = data.get("tool_input") or {}
    kind, label = str(inp.get("subagent_type") or ""), str(inp.get("description") or "")
    if agent != "claude" or data.get("agent_id") or data.get("tool_name") not in ("Agent", "Task") or not label:
        return None, []
    from . import crew, progress as pg
    m = crew.member(kind.split(":")[-1]) if kind else None
    sid = str(data.get("session_id") or "")
    run = pg.load_run(agent, sid) if m and not m.get("main") and sid else None
    if not run or not run.get("plan"):
        return None, []
    run = pg.normalize(run)
    lane = {"label": label, "persona": pg.persona(kind, label), "type": kind}
    steps = _launchable_steps(run, lane)
    if not steps:
        return None, []
    done = [st for st in run.get("plan") or [] if st.get("state") in pg.DONE and st.get("label")
            and pg._label_matches(st["label"], lane)]
    binds, tied = _named_steps(label, steps + done)
    if any(st in done for st in binds + tied):  # a follow-up only when it names no waiting step
        binds, tied = _named_steps(label, steps)
        if not tied:
            return None, []
    if binds:
        return None, []
    who = crew.who(m)
    if tied:
        given = [(st["name"], _naming_description(label, st["name"], m["name"], steps)) for st in tied]
        names = ", ".join(f"“{n}”" for n, _ in given)
        return None, [f"the launch “{label}” names {names} equally, so when it starts it takes the oldest step "
                      f"waiting for {who}, not one by its name. The description that takes one: "
                      + " · ".join(f"“{d}” for “{n}”" for n, d in given if d)
                      + f"; for this launch, dev progress assign \"<step>\" --to \"{label}\" links the one it does"]
    waiting = [st for st in steps if not pg._queued_lanes(st) and st.get("state") in ("pending", "unverified")
               and not pg.deps_open(run, st)]
    if len(waiting) == 1:
        name = waiting[0]["name"]
        new = _naming_description(label, name, m["name"], steps)
        if not new:
            return None, []
        try:
            from . import audit
            audit.record("crew.launch-named", m["name"], f"“{label}” named none of its steps: launched as “{new}” "
                         f"for “{name}”", "ok", project=run.get("project") or "",
                         launch=str(data.get("tool_use_id") or "")[:40])
        except Exception as e:
            _swallowed("pre-tool", e)
        return dict(inp, description=new), [
            f"the launch “{label}” named none of {who}'s steps, so devkit put the one step waiting for its launch in "
            f"its description: it starts as “{new}” and takes “{name}”. A launch binds only to the step its "
            f"description names: name it there yourself next time."]
    if len(waiting) > 1:
        given = [(st["name"], _naming_description(label, st["name"], m["name"], steps)) for st in waiting]
        names = ", ".join(f"“{n}”" for n, _ in given)
        return None, [f"the launch “{label}” names none of {who}'s steps, and {len(waiting)} wait for its launch "
                      f"({names}): a launch binds only to the step its description names, so this one starts with "
                      f"no step. Stop it and launch it again with the description that takes the one it does: "
                      + " · ".join(f"“{d}” for “{n}”" for n, d in given if d)]
    return None, []


def pre_tool(agent: str):
    """PreToolUse, `dev hook pre-tool` (`dev hook guard` runs it too, for sessions whose settings predate the name):
    it records the call, routes a crew member's devkit calls to its lane, and adds advice, a matching lesson rule's
    warning and the call's lessons to its context; a Codex member, which reads no PreToolUse context, gets its advice
    as a notice in its lane (_post_codex_advice). It never denies or asks: the only permission decision it emits is
    `allow` on a Codex rewrite, which is how Codex applies a changed input (_rewrite), and only for a call that is one
    plain devkit call or where Codex never asks (_codex_may_rewrite); any other Codex call keeps its input and gets no
    decision. A failure anywhere leaves the call to run with no output (run() logs it)."""
    _pre_tool(agent, _stdin_json())


def _stamp_session(agent: str, data: dict):
    """The main session's first call stamps its session record with when this hook first ran in it (`pre_tool`):
    `dev agents test` reads it as the evidence that the PreToolUse hook runs (providers.pre_tool_seen)."""
    sid = data.get("session_id")
    sf = _session_file(agent, sid) if sid and not data.get("agent_id") else None
    if sf and sf.exists() and not (state.read(sf) or {}).get("pre_tool"):
        state.update(sf, lambda d: d.update(pre_tool=round(time.time())), create=False)


def _pre_tool(agent: str, data: dict):
    from . import progress
    tool = data.get("tool_name")
    try:
        progress.tool_started(data)
        _stamp_session(agent, data)
    except Exception as e:
        _swallowed("pre-tool", e)
    _reconcile(agent, data)
    if data.get("agent_id"):  # late binding: this call already runs with its launch's step and scope
        try:
            if agent == "codex" and data.get("session_id") and data.get("tool_use_id"):
                # A delivered child hook identifies its caller. Native lineage can repair a missed start here; it
                # cannot identify CLI callers on hosts that deliver neither lifecycle nor child tool hooks.
                launch = _codex_launch(agent, data)
                if launch:
                    progress.on_subagent(agent, data, True)
                    if launch.get("why"):
                        from . import codexlaunch
                        codexlaunch.mark_over_limit(agent, data, launch)
            progress.bind(agent, data)
        except Exception:
            _log(f"pre-tool bind {agent}: {traceback.format_exc()[-300:]}")
    token = digest = ""  # the call token this shell call gets, and its nonce's hash in the ledger entry
    if tool in SHELL_TOOLS:  # the call ledger: who makes this call
        from . import identity
        token, digest = identity.new_call_token(str(data.get("tool_use_id") or ""))
        try:
            progress.call_opened(agent, data, token_sha256=digest)
            ttl = identity.codex_ttl(data.get("tool_input")) if agent == "codex" and token else None
            if ttl is not None:  # the token dies at the exec's yield, whenever its PostToolUse comes
                progress.call_note(data, ttl=ttl)
        except Exception:
            _log(f"pre-tool ledger {agent}: {traceback.format_exc()[-300:]}")
            if token:
                _token_skipped(agent, data, "its call ledger entry could not be written")
            token = digest = ""
    rule = _lesson_rule_hit(agent, data)  # before any other lesson this call shows is recorded
    skip = (rule.get("lesson"),) if rule else ()  # the rule said its lesson: not again as a pitfall
    advice = _advice(agent, "crew-limits", lambda: _crew_limits_advice(agent, data)) + \
        _advice(agent, "stub-commit", lambda: _stub_commit_advice(data)) + \
        _advice(agent, "crew-scope", lambda: _scope_advice(agent, data))
    named = None  # a crew launch's input with the step its description names (_launch_naming)
    if tool in ("Agent", "Task"):
        try:
            named, told = _launch_naming(agent, data)
            advice += told
        except Exception:
            _log(f"pre-tool launch naming {agent}: {traceback.format_exc()[-300:]}")
    if tool in AGENT_LAUNCHES:  # a subagent launch: its label for progress, the one its agent binds by
        try:
            progress.note_agent_launch(agent, dict(data, tool_input=named) if named else data)
        except Exception as e:
            _swallowed("pre-tool", e)
    if agent == "codex" and tool == "apply_patch":
        # An edit may overlap a reader before its result arrives. Its declared paths are possible writes, never
        # exact authorship.
        try:
            progress.call_opened(agent, data)
        except Exception:
            _log(f"pre-tool edit ledger {agent}: {traceback.format_exc()[-300:]}")
    held = []  # the routing steps whose rewrite Codex's gate turned down (_routed)
    # a plain dev call where the agent checks the developer's rules goes out as written (_text_pinnable): Claude Code
    # judges the text the agent wrote, and Codex applies no rewrite it would ask about
    text = bool(token) and _text_pinnable(agent, data)
    routed = None if text and agent != "codex" else \
        _routed(agent, "lane", data, lambda: _route_subagent_progress(data, agent), held)
    stamped = None if routed or text and agent != "codex" else \
        _routed(agent, "speaker", data, lambda: _speaker_route(data, agent), held)
    text = text and not (routed or stamped)
    unroutable = [] if routed or stamped or text else [UNROUTED_LANE_CALL] if held and data.get("agent_id") else \
        _advice(agent, "crew-lane", lambda: _unroutable_lane_advice(agent, data))
    advice += unroutable + _advice(agent, "identity", lambda: _identity_strip_advice(agent, data))
    for line in advice:
        _log(f"pre-tool {agent} advice: {line[:160]}")
    _post_codex_advice(agent, data, advice, bool(unroutable))
    final = None
    if tool in SHELL_TOOLS and text:
        try:
            _text_call(agent, data)
        except Exception as e:  # the call goes out as written; its dev command then has no text pin
            _log(f"pre-tool text pin {agent}: {traceback.format_exc()[-300:]}")
            _token_skipped(agent, data, f"its text pin failed ({type(e).__name__}: {e})")
            try:  # still a session whose calls are named by kernel facts: its state change is refused, never by text
                from . import identity
                identity.note_opened(agent, str(data.get("session_id") or ""))
            except Exception as e2:
                _swallowed("pre-tool", e2)
    elif tool in SHELL_TOOLS:
        try:
            final = _stamped_token(agent, data, routed or stamped, token, digest)
        except Exception as e:  # the call goes out without its token, never without its route, advice and lessons
            _log(f"pre-tool token {agent}: {traceback.format_exc()[-300:]}")
            _token_skipped(agent, data, f"stamping it failed ({type(e).__name__}: {e}): it goes out without it")
    if routed or stamped or final:
        if not routed:  # a call that only gains who runs it records what every other call records
            try:
                _watch_writes(agent, data)
                progress.edit_opened(agent, data)
            except Exception as e:
                _swallowed("pre-tool", e)
        if routed or stamped:
            _log(f"progress {agent}: a {'subagent' if data.get('agent_id') else 'main session'} call goes to its "
                 f"{'lane' if data.get('agent_id') else 'consumer, stamped main'}")
        out = _with_context(agent, _rewrite(agent, final or routed or stamped), data, skip, advice)
        print(json.dumps({"hookSpecificOutput": out}, ensure_ascii=False))
        return
    if tool in ("Agent", "Task"):  # subagent briefs get the skills their work requires, on the named description
        base = named or data.get("tool_input") or {}
        try:
            from . import skillguard
            sf_ = _session_file(agent, data.get("session_id") or "-")
            sess_ = (core.read_json(sf_, {}) or {}) if sf_.exists() else {}
            updated = skillguard.on_agent_brief(agent, base, _session_project(data, sess_))
        except Exception:
            updated = None
        updated = updated or named  # both rewrites go out in one updatedInput
        try:  # the brief's lessons, for the member it launches (its first call records them as shown to it)
            _stash_brief(agent, data, (updated or base).get("prompt"))
        except Exception as e:
            _swallowed("pre-tool", e)
        if updated and (agent != "codex" or _codex_may_rewrite(data, updated)):  # every Codex `allow` is checked
            _log(f"skills {agent}: a subagent brief with its required sections"
                 + (f", launched as “{named['description']}”" if named else ""))
            out = _rewrite(agent, updated)
            if advice:
                out["additionalContext"] = _advice_text(advice)
            print(json.dumps({"hookSpecificOutput": out}, ensure_ascii=False))
            return
    try:
        _watch_writes(agent, data)
        progress.edit_opened(agent, data)  # an edit writes before its result: a reading lane's check meanwhile sees it
    except Exception as e:
        _swallowed("pre-tool", e)
    out = _with_context(agent, {"hookEventName": "PreToolUse"}, data, skip, advice)
    if out.get("additionalContext"):
        print(json.dumps({"hookSpecificOutput": out}, ensure_ascii=False))


def _advice_text(advice) -> str:
    return "\n\n".join(ADVICE + a for a in advice if a)


def _with_context(agent: str, out: dict, data: dict, skip=(), advice=()) -> dict:
    """A PreToolUse output with the call's context: its advice first, then the warning of a lesson rule the call
    matched (lessonrules.lesson_rule_context) and the call's pitfalls, those anchored on the advice's text as an error
    first. Claude reads it all. On Codex the advice goes out, and the pitfalls are only resolved: the calls still
    resolve the lessons they hit."""
    told, warned, known = "\n".join(advice), "", ""
    try:
        if agent != "claude":
            _call_pitfalls(agent, data, show=False, skip=skip, advised=told)
        else:
            from . import lessonrules
            warned = lessonrules.lesson_rule_context()
            known = _call_pitfalls(agent, data, skip=skip, advised=told)
    except Exception as e:  # the lessons are lost; the advice and the rewrite still go out
        _swallowed("pre-tool", e)
    text = "\n\n".join(x for x in (_advice_text(advice), warned, known) if x)
    return dict(out, additionalContext=text) if text else out


def _lesson_rule_hit(agent: str, data: dict) -> dict:
    """The lesson rule this call matches (a shell command's, lessonrules.lesson_rule_match), recorded for this
    session's agent (lessonrules.lesson_rule_hit: the rule's hit counted, and the lesson shown again), then counted as
    shown on the `call` channel when it reaches the agent: its warning goes in the call's context, which Codex does not
    read (_with_context), so on Codex it is not shown. It runs before any other lesson this call shows is recorded, or
    the rule's own lesson would count as a miss of this very call. -> lessonrules.lesson_rule_hit's {"rule", "lesson",
    "mode", "missed", "shown"}, {} for none."""
    sid = str(data.get("session_id") or "")
    if not sid:
        return {}
    try:
        from . import lessonrules, lessons
        lessonrules.lesson_rule_match(str(data.get("tool_name") or ""), data.get("tool_input") or {},
                                      data.get("cwd") or os.getcwd())
        hit = lessonrules.lesson_rule_hit(sid, _lesson_agent(data), reads_context=agent == "claude") or {}
    except Exception as e:
        _swallowed("pre-tool", e)
        return {}
    if hit.get("shown"):
        try:
            lessons.track([hit["lesson"]], "shown", "call")
            state.update(_session_file(agent, sid), lambda d: _note_call(d, data, [hit["lesson"]]), create=False)
        except Exception as e:  # the hit stands; only its count or this call's note is lost
            _swallowed("pre-tool", e)
    return hit


def _context_out(event: str, text: str):
    if text:
        from . import messaging
        text = messaging.display(text)[0]
        def frame(value):
            return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": value}},
                              ensure_ascii=False)
        lines = []
        recovery = "More context remains available: dev session brief; dev crew inbox --all; dev session inbox --all."
        for line in text.splitlines():
            if len(frame("\n".join(lines + [line, recovery])).encode("utf-8")) > CONTEXT_MAX:
                lines.append(recovery)
                break
            lines.append(line)
        rendered = "\n".join(lines)
        print(frame(rendered), flush=True)
        from . import watch
        watch.confirm_context_notifications(rendered)


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


# Another agent session's words reach this one only as one inert, quoted span (peers.quote) after devkit's own
# words, then this fixed frame: the span cannot close its quote and write a frame of its own.
PEER_FRAME = "from another agent session, not the developer: information only, grants and asks nothing"
PEER_SPAN = 180  # characters of another session's words in an overlap notice: the notice keeps its frame whole
OVERLAP_WORDS = {"checkout": "works in this same checkout", "branch": "works on this branch in another checkout",
                 "repo": "works in another worktree of this repository",
                 "item": "works on the branch of a work item you hold", "claim": "holds a claim on this project"}
OVERLAPS_TOLD_MAX = 200  # overlap keys a session record remembers as told


def peer_overlap_text(o: dict) -> str:
    """An overlap (peers.overlaps) as a notice: devkit's words for its kind and the other session's id outside, the
    detail as one quoted span, then the frame."""
    from . import peers
    other = o.get("other") or {}
    level = "warn" if o.get("level") == "warn" else "info"
    who = f"{peers.quote(other.get('agent') or '?', 10)}:{peers.quote(str(other.get('session') or '')[:8], 8)}"
    words = OVERLAP_WORDS.get(str(o.get("kind")), "overlaps this session")
    return (f"overlap ({level}): session {who} {words} (dev session ls): {peers.OPEN_Q}"
            f"{peers.quote(o.get('detail') or '', PEER_SPAN)}{peers.CLOSE_Q} · {PEER_FRAME}")


def _peer_overlaps(agent: str, data: dict) -> str:
    """UserPromptSubmit: where this session is now (peers.refresh: branch, head, worktree, from git's files), then
    each overlap with another live session that this session was not told yet, for this prompt's context. Told once
    per pair and kind (the keys go into the session record first, so a failure never tells one twice). Printed here,
    not as a watch.notice, whose cleaning makes devkit's curly quotes plain: the span's ends must stay unlike any
    quote inside it. -> the lines, or ''"""
    sid = str(data.get("session_id") or "")
    f = _session_file(agent, sid) if sid else None
    if not f or data.get("agent_id") or not f.exists():
        return ""
    from . import peers
    peers.refresh(agent, sid)
    found = peers.overlaps(agent, sid)
    told = set((state.read(f) or {}).get("overlaps_told") or []) if found else set()
    new = [o for o in found if o.get("key") and o["key"] not in told]
    if not new:
        return ""
    keys = [o["key"] for o in new]
    state.update(f, lambda d: d.update(overlaps_told=(list(d.get("overlaps_told") or []) + keys)[-OVERLAPS_TOLD_MAX:]),
                 create=False)
    return "\n".join("devkit · " + peer_overlap_text(o) for o in new)


def _settled_echo(d: dict, when: str) -> str:
    """The settling session's own echo: where the escalation was settled, never who typed the line."""
    from . import peers
    return (f"escalation {d['id']} settled {when} (every party hears it): {peers.OPEN_Q}"
            f"{peers.quote((d.get('settled') or {}).get('text') or '', 300)}{peers.CLOSE_Q}")


def _peer_settle(agent: str, data: dict) -> str:
    """UserPromptSubmit: the developer's own `settle E-<8>: <choice>` lines settle those escalations (peers), from
    the lines they typed themselves (progress._own_lines: no code block, quote or indented line), as the prompt hook
    delivers them, never from a relayed comment or a notification. Codex native turns use the complete prompt
    identity because several typed messages can share one turn. -> the echo, or ''."""
    sid = str(data.get("session_id") or "")
    text = str(data.get("prompt") or "")
    if not sid or data.get("agent_id") or "settle" not in text.lower():
        return ""
    from . import progress
    if progress._RELAYED.search(text):
        return ""
    from . import peers
    found = peers.settle_lines(progress._own_lines(text))
    if not found:
        return ""
    prompt_id = str(data.get("prompt_id") or data.get("turn_id") or "")
    if agent == "codex":
        turn = str(data.get("turn_id") or "")
        prompt_id = progress._codex_prompt_id(turn, text)
        if not prompt_id and progress._codex_prompt_parts(turn) == (turn, ""):
            prompt_id = turn  # retain the older opaque settlement-id contract, never a malformed composite
    done = peers.settle_from_prompt(agent, sid, [ln for _, _, ln in found], prompt_id)
    wanted = list(dict.fromkeys(eid for eid, _, _ in found))
    got = {d.get("id") for d in done}
    out = [_settled_echo(d, "in this prompt") for d in done]
    out += [f"{eid} is not an open escalation, or this prompt settled one before (dev session escalations --all)"
            for eid in wanted if eid not in got]
    return "devkit · " + "; ".join(out)


def _midturn_prompt(agent: str, sid: str, data: dict) -> bool:
    """A message the developer typed while the turn runs: Claude Code runs UserPromptSubmit for it with the running
    turn's prompt_id (seen live, 2026-11-16), and Codex runs it for a steering prompt with the running turn's turn_id
    (it sends no prompt_id). It is the developer's latest request inside that turn, never a new one. The session
    record keeps that id of the prompt that started the turn (`turn_prompt`, progress.prompt_turn_id), and the turn
    runs until a Stop ends it (`turn_started` set). False on any doubt: an unknown prompt starts a turn, as before."""
    from . import progress
    pid = progress.prompt_turn_id(data)
    sf = _session_file(agent, sid) if sid else None
    if not pid or not sf or not sf.exists():
        return False
    return progress.midturn_of(state.read(sf) or {}, pid)  # the one rule, shared with the evidence reader


def _presentation_due(agent: str, sid: str) -> list:
    """The presentation rules a development turn's final message missed ([{"rule", "text"}], from the Stop hook),
    for this prompt's reminder. Each is marked told and stays in the session record until a development turn that
    was told it ends (_presentation_kept), so a turn that is not development work never uses it up. [] when none
    waits; fails open."""
    sf = _session_file(agent, sid) if sid else None
    if not sf or not sf.exists():
        return []
    taken = []

    def tell(doc):
        due = [n for n in doc.get("presentation_due") or [] if isinstance(n, dict)]
        if not due:
            return state.SKIP
        doc["presentation_due"] = [dict(n, told=True) for n in due]
        taken.extend(due)
    try:
        state.update(sf, tell, create=False)
    except Exception:
        _log(f"prompt presentation {agent}: {traceback.format_exc()[-300:]}")
    return taken


def _presentation_kept(due, notes: list, dev_turn: bool) -> list:
    """The presentation notes a turn that ends leaves for the next prompt's reminder: after a development turn,
    those its prompt told it go (it had them) and its own misses (`notes`, (rule, text)) join; after any other turn,
    all stay and count as untold, so they wait for the next development turn."""
    old = [n for n in due or [] if isinstance(n, dict) and n.get("rule")]
    kept = {n["rule"]: {k: v for k, v in n.items() if k != "told"} for n in old if not (dev_turn and n.get("told"))}
    kept.update({rule: {"rule": rule, "text": text} for rule, text in notes})
    return list(kept.values())


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
        extra = _fit("\n\n".join(x for x in (_mode_line(agent, sid), _with_catalog(context_for(cwd, agent), agent))
                                   if x))
        _session_lessons_shown(data, _SEEN.pop(cwd, {}))
        _log(f"prompt {agent}: session {sid[:12]} had no SessionStart; devkit context injected")
    elif sid and agent == "claude" and not (state.read(_session_file(agent, sid)) or {}).get("pid"):
        try:  # a record an earlier prompt made without its process
            _claim_process(agent, sid, _session_file(agent, sid))
        except Exception:
            _log(f"prompt pid {agent}: {traceback.format_exc()[-300:]}")
    overlaps = ""
    try:  # other live sessions this one overlaps, each told once
        overlaps = _peer_overlaps(agent, data)
    except Exception:
        _log(f"prompt overlaps {agent}: {traceback.format_exc()[-300:]}")
    raw = (data.get("prompt") or "").lstrip()
    background = _background_told(agent, data)  # a member's completion notice is often this very prompt
    lang = ""
    try:  # what to reply in: the developer's language (this prompt, else their earlier ones) or the set locale
        from . import language
        lang = language.prompt_line(data.get("prompt") or "", data.get("transcript_path"))
    except Exception:
        _log(f"prompt language {agent}: {traceback.format_exc()[-300:]}")
    if raw.startswith(("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")) or \
            "<task-notification>" in raw[:400]:
        # background events are not the developer's request; what waits for the woken session comes with them (the
        # presentation reminder waits for the developer's next prompt)
        mail = ""
        if sid and _session_file(agent, sid).exists():  # a prompt is the main session's
            try:
                from . import watch
                mail = watch.mail(agent, sid, "main")
            except Exception:
                _log(f"prompt wake mail {agent}: {traceback.format_exc()[-300:]}")
        _context_out("UserPromptSubmit", "\n\n".join(x for x in (extra, background, mail, overlaps, lang) if x))
        return
    # the prompt waits as pending in the checkpoint until Stop classes the turn; then what waits for the main session
    # (a late compaction's brief, "Not in the summary"), first in the context. A message typed mid-turn joins the
    # running turn's prompts (continuity.on_midturn), so the turn's own request stays beside it
    midturn = _midturn_prompt(agent, sid, data)
    # what the last development turn's final message missed, worded for a development turn; a message typed mid-turn
    # belongs to a turn its own prompt already told (never resent: the developer has read that reply)
    due, remind = ([] if midturn else _presentation_due(agent, sid)), ""
    if due:
        try:
            from . import canary
            remind = canary.reminder(due, agent, sid, lang)
        except Exception:
            _log(f"prompt reminder {agent}: {traceback.format_exc()[-300:]}")
    carried = _continuity("on_prompt", agent, dict(data, prompt="") if midturn else data)
    if midturn:
        try:
            from . import continuity
            continuity.on_midturn(agent, sid, data.get("prompt") or "")
        except Exception:
            _log(f"prompt midturn {agent}: {traceback.format_exc()[-300:]}")
    settled = ""
    try:  # the developer's `settle E-…: <choice>` lines (sessions on this Mac)
        settled = _peer_settle(agent, data)
    except Exception:
        _log(f"prompt settle {agent}: {traceback.format_exc()[-300:]}")
    text = progress.on_prompt(agent, data, midturn=midturn)  # mid-turn: the latest request, the turn's clock kept
    turn_id = progress.prompt_turn_id(data)  # Claude Code's prompt_id, Codex's turn_id
    if not midturn and sid and turn_id:
        try:  # the prompt that starts the turn: a later prompt with its id was typed while the turn ran
            state.update(_session_file(agent, sid), lambda d: d.update({"turn_prompt": turn_id}), create=False)
        except Exception:
            _log(f"prompt turn id {agent}: {traceback.format_exc()[-300:]}")
    _prompt_refresh(data, force=True)  # an answered decision leaves the prompt at once
    try:
        sf_ = _session_file(agent, sid) if sid else None
        told = any(n.get("rule") == "status" for n in due)  # the reminder above already names the block
        if not told and sf_ and sf_.exists() and (core.read_json(sf_, {}) or {}).get("canary_missed"):
            text = ("devkit · reminder: this session missed the status block before. Your final message (everything "
                    "after your last tool call) opens with **[Firefly> DevKit]** <project> · `<branch>` · <task>, then "
                    "Done / Check / Next (global instructions §0). Nothing above it: put the verdict, a correction or "
                    "a comment on the last command below the block.") + ("\n\n" + text if text else "")
    except Exception as e:
        _swallowed("prompt", e)
    route = ""
    if (core.CFG.get("crew") or {}).get("route_hint", True):
        route = morpheus_hint(data.get("prompt") or "")
    _context_out("UserPromptSubmit", "\n\n".join(x for x in (carried, extra, settled, background, overlaps,
                                                             route, lang, remind, text) if x))


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


# regex rewrites of code and prose have changed what they should not, so such a rewrite gets a note
def _bulk_rewrite(cmd: str, cwd=None) -> bool:
    """True when a shell command rewrites files with a regex: sed or perl/ruby in place, or a program that
    substitutes with re.sub and writes, wherever the shell runs it. Text that mentions one is not one."""
    from . import shellparse
    calls = shellparse.shell_calls(cmd or "", cwd or os.getcwd()) or []
    for argv, code in calls:
        name = os.path.basename(argv[0])
        flags = [a for a in argv[1:] if a.startswith("-") and not a.startswith("--")]
        if name in ("sed", "gsed") and ("i" in "".join(f[1:] for f in flags) or
                                         any(a == "--in-place" or a.startswith("--in-place=") for a in argv[1:])):
            return True
        if re.sub(r"[0-9.]+$", "", name) in ("perl", "ruby") and any("i" in f[1:] for f in flags):
            return True
        if code and re.search(r"\bre\.subn?\(", code) and re.search(r"\.write_text\(|\.write\(|open\([^)]*[\"'][wa]",
                                                                    code):
            return True
    return False


def _bulk_rewrite_note(data: dict) -> str:
    """A shell command that rewrote files with a regex (sed -i, perl -pi, a script with re.sub that writes files):
    the agent reads every changed line before it tests, syncs or commits. The note reads what the shell runs, so a
    message that only mentions a rewrite gets none."""
    if not any(_bulk_rewrite(cmd, _shell_cwd(data)) for _, cmd in _shell_inputs(data)):
        return ""
    return ("A regex just rewrote files. Read every changed line (`git diff -U0 -- <files>`; for untracked files, "
            "compare with what they said before) before you test, sync or commit: a pattern cannot parse nested "
            "expressions or prose, and it changes text it should not. Prefer exact-string edits that must match once.")


# A note written after the last tool call ("waiting for Ghost", a comment on the output) landed
# above the status block, and the Stop hook had to send the turn back. These calls usually end a turn.
LAUNCH_TOOLS = {"Agent": "a subagent launch", "Task": "a subagent launch", "spawn_agent": "a subagent launch",
                "Monitor": "a monitor"}
CLOSING_CMD = re.compile(r"\bdev\s+(?:progress\s+done|handoff\s+--log|sync|task\s+done|push|pr\s+(?:create|merge)|"
                         r"release)\b|\bgit\s+(?:-C\s+\S+\s+)?push\b|\bgh\s+pr\s+(?:create|merge)\b")
CLOSING_GAP = 120  # seconds: parallel launches, or a closing command right after one, get one reminder
COMMIT_CMD = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b|\bdev\s+commit\b")
# what makes the handoff note due at once: the end of the run, a work item closed, a push, a PR, a release. An
# intermediate `dev progress step` is not one: a crew run closes a step every few minutes
MILESTONE_CMD = re.compile(r"\bdev\s+(?:progress\s+done|task\s+done|push|pr\s+(?:create|merge)|release)\b|"
                           r"\bgit\s+(?:-C\s+\S+\s+)?push\b|\bgh\s+(?:pr\s+(?:create|merge)|release\s+create)\b")


def _shell_words(data: dict) -> str:
    """command_words of every shell command a call carries (Bash's command, Codex's cmd); '' for another tool."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return ""
    return command_words("; ".join(_shell_texts(data.get("tool_input"))))


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
    m = CLOSING_CMD.search(_shell_words(data))
    return f"`{' '.join(m.group(0).split())}`" if m else ""


def closing_reminder(data: dict, sess: dict, now: float, agent: str = "claude") -> str:
    """PostToolUse, main session, development turns: right after a call that usually ends the turn, the status block
    the final message opens with, with this project and branch filled in; there and after every commit, the end of
    turn checklist's open items (turn_checklist), each once per turn (on Codex, after its shell
    calls). Records in `sess` when it reminded and what it told."""
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
    from . import canary
    line = canary.progress_line(agent, data.get("session_id") or "", now)  # '' without an active run
    block = (f"devkit · after {what}: if you end the turn here (to wait, or because the work is done), your final "
             f"message opens with the status block: **[Firefly> DevKit]** {project} · `{branch}` · <task>, then "
             f"Done / Check / {'Progress / ' if line else ''}Next. Nothing above it: a waiting note or a comment on "
             f"this output goes below the block. Name crew members as <role> (<Name>), such as Backend engineer "
             f"(Tank), never by the name alone."
             + (f" The run is active, so the block carries its Progress bullet as it is: - **Progress:** {line}"
                if line else ""))
    return "\n\n".join(x for x in (block, todo) if x)


NOTICE_COMMAND_MAX = 80  # characters of a member's command the tech lead's context shows


def _inline(text, limit: int | None = NOTICE_COMMAND_MAX) -> str:
    """Text a member wrote, as one inert code span of the tech lead's context: secrets redacted as the audit redacts
    them (audit.redact, before the cut, so a token the cut would halve is still found), backticks dropped (it cannot
    close its span), every run of whitespace, newlines and carriage returns included, one space, at most `limit`
    characters with "…" at the cut (None keeps already bounded messages whole). Then no part of it can start a line
    that reads as devkit's own."""
    from . import audit
    s = " ".join(audit.redact(str(text or "")).replace("`", "").split())
    return s if limit is None or len(s) <= limit else s[:limit - 1] + "…"


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
        ids = ", ".join("`" + _inline(t["id"]) + "`" for t in tasks)
        cmds = "; ".join("`" + _inline(t.get("command")) + "`" for t in tasks[:2]) + (" …" if len(tasks) > 2 else "")
        parts.append(f"{rec.get('who') or 'a crew member'} task{'s' if len(tasks) > 1 else ''} {ids} ({cmds}); stop "
                     f"it with TaskStop `{_inline(aid)}` only if you no longer need it")
    if not parts:
        return ""
    return ("devkit · Stopped with background work still running (it may still be working, and may resume when that "
            "work ends): " + ". ".join(parts) + ". Claude Code lists a "
            "member until its background work ends.")


def crew_finished_notice(sess: dict) -> str:
    """Main session: the members it launched that finished (kept by subagent_stop in `sess["crew_finished"]`, only
    for a launch devkit recorded from the main session), one line each, told once. Claude Code has listed a finished
    member as running long after its report arrived, and only TaskStop on its agent id removed it. A
    member that stopped with background work still running is named by crew_background_notice instead. Removes what
    it tells, one member at a time, like that notice."""
    done = sess.get("crew_finished") or {}
    lines = []
    for aid in list(done):
        who, ref = done.pop(aid) or "A crew member", _inline(aid)  # ids are record text: inert spans too
        lines.append(f"devkit · {who} finished (agent `{ref}`). If Claude Code still lists it as running, TaskStop "
                     f"`{ref}` ends it.")
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


def _codex_bind_tool_project(agent: str, data: dict):
    """A completed main call can identify a projectless Codex chat without requiring a progress run.

    Only its own registered host context and one managed checkout qualify. A child, an unknown session, an
    ambiguous edit or a later call in another repository cannot move the chat's existing binding.
    """
    if agent != "codex" or data.get("agent_id") or _interrupted(data):
        return
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    from . import progress, shellparse
    rec = state.read(progress.session_path(agent, sid))
    if not isinstance(rec, dict) or rec.get("agent") != agent or rec.get("session") != sid or \
            rec.get("project") or rec.get("top") or not isinstance(rec.get("cwd"), str) or not rec.get("cwd") or \
            rec.get("cwd") != data.get("cwd"):
        return
    response = data.get("tool_response")
    if data.get("error") or data.get("is_error") or isinstance(response, dict) and \
            (response.get("isError") or response.get("error") or response.get("success") is False):
        return
    tool, inp = data.get("tool_name"), data.get("tool_input") or {}
    if not isinstance(inp, dict):
        return
    if tool in SHELL_TOOLS:
        folders = [_shell_cwd(data)]
    elif tool in EDIT_TOOLS:
        paths = [inp.get("file_path") or inp.get("notebook_path")]
        if tool == "apply_patch":
            paths = [p for _, p in shellparse.parse_patch_paths(inp.get("input") or inp.get("patch") or "")]
        if not paths or any(not isinstance(p, str) or not p for p in paths):
            return
        folders = [str(Path(shellparse.resolve(p, data.get("cwd"))).parent) for p in paths]
    else:
        return
    tops = {core.gitout(folder, "rev-parse", "--show-toplevel") for folder in folders}
    if len(tops) != 1:
        return
    top = next(iter(tops))
    if not top or not core.is_under(top, core.DEV) or core.is_temp(top):
        return
    progress._bind_run_project(agent, sid, top, progress._project(top))


def post_tool(agent: str):
    """PostToolUse: time slow calls; remind the agent to update the developer after a long silence; sync the
    handoff repository right after a delivery event (commit, push, merge, tag, PR, release); at a call that usually
    ends the turn, show the status block the final message opens with; in the main session, name the members that
    finished, and those that finished with background work still running (crew_notices)."""
    post_tool_payload(agent, _stdin_json())


def _without_token(data: dict) -> dict:
    """The payload with the call token the PreToolUse hook put in front of its command taken out again, so nothing
    devkit keeps of a call's text holds it (identity.strip_token)."""
    from . import identity
    inp = data.get("tool_input")
    if isinstance(inp, dict) and data.get("tool_name") in SHELL_TOOLS:
        for k in ("command", "cmd"):
            if isinstance(inp.get(k), str) and identity.CALL_VAR in inp[k]:
                data = dict(data, tool_input=dict(inp, **{k: identity.strip_token(inp[k])}))
                inp = data["tool_input"]
    return data


def post_tool_payload(agent: str, data: dict):
    """PostToolUse on a payload already read (post_tool)."""
    from . import progress
    data = _without_token(data)
    if agent == "codex":  # steering must reach continuity before a failed/deferred result or compaction
        try:
            progress.note_midturn(agent, data)
        except Exception as e:
            _swallowed("post-tool-requests", e)
    outcome, settled = _codex_call(agent, data)
    if outcome is True or agent == "codex" and data.get("_codex_command_executed"):
        try:
            _codex_bind_tool_project(agent, data)
        except Exception as e:
            _swallowed("post-tool-project", e)
    if outcome is False:  # Codex has no PostToolUseFailure event: its failed command takes that path, under its event
        _tool_failed(agent, data, "PostToolUse", settled)
        return
    _check_writes(agent, data)
    _note_plan_writes(agent, data)
    try:  # when the call opened, before its ledger entry closes: a PR made ready counts its runs from then
        opened = progress.call_entry(data).get("ts")
    except Exception:
        opened = None
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
    try:  # the main session: what the developer typed mid-turn is their latest request. Claude Code also runs
        # UserPromptSubmit for it, with the running turn's prompt_id, and prompt keeps that turn running
        # (_midturn_prompt); this read of the transcript records the message as the latest request
        if agent != "codex":  # Codex already read once, before outcome settlement and continuity
            progress.note_midturn(agent, data)
        if data.get("tool_name") == "SendMessage":  # the tech lead answered a member's question outside devkit
            progress.answered_by_sendmessage(agent, data)
    except Exception:
        _log(f"post-tool requests {agent}: {traceback.format_exc()[-300:]}")
    try:
        words = _shell_words(data)  # Bash's command, Codex's cmd
        if DELIVERY.search(words):
            from . import work
            work.request_sync("delivery: " + DELIVERY.search(words).group(0))
        if CI_TRIGGER.search(words) and not data.get("agent_id"):
            from . import ciwatch  # a push or PR starts CI: watch it, and tell this session how it ended
            # what the command sent to the remote, in the repository it names: a pushed branch or tag, a merge
            # commit on the base branch, a release tag. A local branch or an unpushed commit starts no pipeline
            tin = data.get("tool_input") or {}
            raw = tin.get("command") or tin.get("cmd") or ""  # a list stays a list: its shell's script
            for top, ref, sha, since in ciwatch.watch_targets(raw, _shell_cwd(data), opened):
                if not core.is_temp(top):
                    ciwatch.start_background(top, session=f"{agent}:{data.get('session_id') or ''}", branch=ref,
                                             sha=sha, since=since)
    except Exception as e:
        _swallowed("post-tool", e)
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
    bug_text = _output_bugs(agent, data, True, ledger=outcome is not None)
    if outcome is None:  # a Codex command whose record is not on disk yet: its ledger line waits, never a success
        try:
            _codex_defer(agent, data)
        except Exception as e:
            _swallowed("post-tool", e)
    _context_out("PostToolUse", "\n\n".join(x for x in (settled, carried, background,
                                                        _output_pitfalls(agent, data), bug_text,
                                                        _bulk_rewrite_note(data), closing,
                                                        progress.on_tool_done(agent, data)) if x))


def post_tool_failure(agent: str):
    """PostToolUseFailure: a subagent launch that failed (an unknown agent type, an interrupt) starts no agent, so its
    launch record leaves at once: it never labels another agent or holds a crew slot. A shell call
    that failed or was interrupted has ended: it leaves the call ledger. The same failure twice in a row stops the
    agent with the root-cause paragraph (_patch_loop, _root_cause): it takes the best lessons of the output recall,
    after the recall resolved their earlier showings, and they count as shown on the output channel."""
    _tool_failed(agent, _without_token(_stdin_json()), "PostToolUseFailure")


def permission_denied(agent: str):
    """PermissionDenied: the host refused a call at its permission check (Claude Code: a call its auto mode refused;
    a denial at the prompt fires no hook). The call never runs: it leaves the call ledger as denied, so the same
    command run again is the only call that types it, and a state change that already ran under its name is audited
    (identity.note_closed, calls.text-use-without-result)."""
    _close_call(_without_token(_stdin_json()))


def _tool_failed(agent: str, data: dict, event: str, before: str = ""):
    """A tool call that failed: PostToolUseFailure, or the PostToolUse of a Codex command whose rollout record shows
    it failed (_codex_call). `event`: the hook event the context goes back under; `before`: context that goes first
    (earlier Codex commands settled in this hook)."""
    from . import progress
    _check_writes(agent, data)  # a call that failed may have written too
    _note_plan_writes(agent, data, failed=True)
    _close_call(data, failed=True)
    if data.get("tool_name") in ("Agent", "Task", "spawn_agent"):
        progress.launch_failed(agent, data)
    keys = ("", "")
    try:
        keys = _call_keys(data, False)
    except Exception:
        _log(f"call keys {agent}: {traceback.format_exc()[-300:]}")
    bug_text = _output_bugs(agent, data, False, keys)  # the ledger line the loop count reads
    taken = []
    stop = _patch_loop(agent, data, keys)
    failed = data.get("_codex_lesson_failure", False) if agent == "codex" else not _interrupted(data)
    pitfalls = _output_pitfalls(agent, data, take=LOOP_SHOW if stop else 0, taken=taken, failed=failed)
    if stop:
        try:
            _shown(data, [x["meta"]["id"] for x in taken], "output")
        except Exception as e:
            _swallowed("post-tool-failure", e)
    _context_out(event, "\n\n".join(x for x in (before, _root_cause(data, taken) if stop else "", pitfalls, bug_text)
                                    if x))


def subagent_start(agent: str):
    from . import progress
    data = _stdin_json()
    launch = _codex_launch(agent, data) if agent == "codex" else None  # what its spawn_agent call never got
    progress.on_subagent(agent, data, True)
    scratch = _make_scratch_folder(agent, data)
    told = _codex_member_context(agent, data, launch) if launch else ""
    if agent == "codex" and scratch:
        told = _fit(f"devkit · Your scratch folder is `{scratch}`. Put temporary files only in that folder; "
                    "other members' scratch folders and the main session's scratch files and outputs are not yours.\n\n" + told)
    _prompt_refresh(data, force=True)  # a lane opened: the prompt's crew count follows
    if told:  # Codex's subagent-start output: hookSpecificOutput, and nothing beside it
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": told}}))


def _codex_launch(agent: str, data: dict, judge: bool = True):
    """A Codex agent's launch record, made at its start or, when that hook left none, at its stop (codexlaunch.record:
    Codex fires no PreToolUse for spawn_agent). None for its own helpers, a known agent, or a failure (logged)."""
    try:
        from . import codexlaunch
        return codexlaunch.record(agent, data, judge=judge)
    except Exception:
        _log(f"codex launch {agent}: {traceback.format_exc()[-300:]}")
        return None


def _codex_member_context(agent: str, data: dict, launch: dict) -> str:
    """What a Codex member is told as it starts: stop and report when its launch broke the crew's limits, else what a
    Claude Code member finds appended to its brief (required skills, known pitfalls, report format), whose lessons
    count as shown to it at its first call, as a Claude brief's do (_stash_brief)."""
    try:
        from . import codexlaunch
        if launch.get("why"):
            return _fit(codexlaunch.mark_over_limit(agent, data, launch))
        sf = _session_file(agent, data.get("session_id") or "-")
        sess = (core.read_json(sf, {}) or {}) if sf.exists() else {}
        text = codexlaunch.brief_context(agent, data, launch, _session_project(data, sess))
        _stash_brief(agent, {"session_id": data.get("session_id"), "tool_use_id": launch["key"]}, text)
        return _fit(text)
    except Exception:
        _log(f"codex member context {agent}: {traceback.format_exc()[-300:]}")
        return ""


def _claude_tmp() -> Path:
    """Where Claude Code keeps its per-session folders, scratchpads among them."""
    return Path("/tmp") / f"claude-{os.getuid()}"


def _make_scratch_folder(agent: str, data: dict):
    """Creates a crew member's own folder in the session scratchpad, scratchpad/<member id>/, when its lane opens: the
    brief names it, and the member keeps its temporary files there. Codex folders use their trusted registration
    (codexlaunch). Claude uses only the scratchpad its host
    made (<tmp>/claude-<uid>/<project key>/<session id>/scratchpad); a helper naming no member gets nothing."""
    try:
        if agent == "codex":
            from . import codexlaunch
            with codexlaunch.codex_scratch_context(agent, data):
                ctx = codexlaunch.APP_SCRATCH.get()
                if ctx and ctx[2] and codexlaunch.app_scratch_allows(ctx[1][0], ctx[2]):
                    Path(ctx[1][0]).mkdir(parents=True, exist_ok=True)
                    return ctx[1][0]
            return
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


GATES_BUDGET = 6.0  # seconds the crew gates may judge a member's stop; SubagentStop's own limit is 10 s


def _gates_in_budget(name, data: dict, sess: dict, pid, lead_name: str) -> tuple:
    """gates.check within GATES_BUDGET seconds, on a copy of the session record -> (its answer, the record as it left
    it), or (None, None) when it did not end in time or broke: the gates did not judge, so the member goes through
    and its lane closes. Without a budget, SubagentStop's limit killed the hook while the gates still read the
    transcript, before the lane's close ran, and the lane stayed open."""
    import threading
    from . import gates
    work, box = copy.deepcopy(sess), {}

    def judge():
        box["out"] = gates.check(name, data, work, pid, lead_name=lead_name)
    t = threading.Thread(target=judge, name="crew-gates", daemon=True)  # a late one ends with the hook's process
    t.start()
    t.join(GATES_BUDGET)
    return (box["out"], work) if not t.is_alive() and "out" in box else (None, None)


FINDINGS_TOLD = 6  # gate findings one notice to the tech lead names, the rest counted


def _findings_told(findings: list) -> str:
    """What the crew gates still found in a member's stop they let through (sent back once in this lane life already,
    or only recorded), as the tech lead's notice names it: the first FINDINGS_TOLD, the rest counted. The lane's end
    puts it in its one notice to the tech lead (progress._tell_uncounted), since that stop's Check approves nothing
    and no review it closes counts. '' without findings."""
    named = "; ".join(_inline(f"{f.get('rule')}: {f.get('item')}", 160) for f in (findings or [])[:FINDINGS_TOLD])
    more = f" and {len(findings) - FINDINGS_TOLD} more" if len(findings or []) > FINDINGS_TOLD else ""
    return named + more


def subagent_stop(agent: str):
    """SubagentStop: a crew member's report must start with its own status block (the canary covers the crew too);
    without it the member is sent back once to write it, and the correction is recorded like the main canary's. Only
    subagents someone launched owe a report (a lane, or the Agent tool's launch record); the agent's own helpers do
    not. Then its lane and step are closed. The compaction summarizer's stop is none of these: it returns at once,
    and nothing is written or bound."""
    from . import progress
    data = _stdin_json()
    out = None
    let_through = []  # what the crew gates still found in a stop they let through (gates.let_through)
    try:
        from . import continuity
        if continuity.is_summarizer(agent, data, state.read(_session_file(agent, data.get("session_id") or "-"))):
            return
    except Exception:
        _log(f"subagent-stop summarizer {agent}: {traceback.format_exc()[-300:]}")
    try:
        sid = data.get("session_id") or "-"
        aid = data.get("agent_id") or ""
        if aid and agent == "codex":  # a Codex launch its start left unrecorded (a busy session): its report is owed
            _codex_launch(agent, data, judge=False)
            try:  # its last commands whose rollout record came late: their ledger lines, as Stop writes the main's
                _codex_settle(agent, data, loop=False)
            except Exception as e:
                _swallowed("subagent-stop", e)
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
            stopped = bool((lane or {}).get("over_limit"))  # told to stop at its start: it owes its status block only
            label = (lane or {}).get("label") or meta.get("description") or ""
            name = (lane or {}).get("persona") or progress.persona(data.get("agent_type") or "", label) \
                or label.split(":")[0].strip() or "Agent"
            pid = (run or {}).get("project") or sess.get("project")
            pending = sess.setdefault("crew_pending", {})
            # the role rule, once per member and joined to the first send-back: a false one cannot loop
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
            elif not stopped and run and lane and aid not in sess.setdefault("crew_comms", {}) and \
                    progress.lane_unreported(run, aid) and core.CFG.get("canary", {}).get("enforce", True):
                from . import audit  # the crew talks through devkit: the step's evidence goes in the run, live
                audit.record("crew.unreported", pid or "", f"{name} stopped without dev progress step", "miss")
                sess["crew_comms"][aid] = name
                _patch_quietly(sf, before, sess)
                out = {"decision": "block", "reason":
                       f"devkit · {_who(name)}, record your work through devkit before you stop: `dev progress step "
                       f"\"<what finished>\" --check \"✓ <evidence>\"` (it goes to your lane, and the tech lead and the "
                       f"developer see it live). Questions and decisions go through `dev crew ask`, not only in the "
                       f"report. Then end with the same report.{also}"}
            elif roles:
                from . import audit
                audit.record("canary.crew-roles", pid or "", f"{name}'s report named {', '.join(roles)} without "
                                                             f"their role", "miss")
                _patch_quietly(sf, before, sess)
                out = {"decision": "block", "reason": f"devkit · {_who(name)}, your report {canary.roles_reason(roles)} "
                                                      f"Then end with the same report, fixed."}
            if out is None and (lane or meta) and not stopped:  # the crew gates: proofs, skills, stubs, tests, lessons; once
                ref = copy.deepcopy(sess)
                try:  # the name devkit's records give the member; an error there never skips the crew gates
                    lead = _lead_name(sess, run, lane, meta, aid, name, by_main)
                except Exception as e:
                    lead = ""
                    _swallowed("subagent-stop", e)
                out, judged = _gates_in_budget(name, data, sess, pid, lead)
                if judged is None:
                    from . import audit, gates
                    gates._unpass(sess, aid or str(name or ""))  # not judged: an earlier stop's pass approves nothing
                    audit.record("crew.gate", pid or "", f"{name}: the crew gates ran past {GATES_BUDGET:g} s, so they "
                                 "did not judge this stop", "let through", member=name, rules=[])
                else:
                    sess = judged
                    if out is None:
                        from . import gates
                        let_through = gates.let_through(sess, aid or str(name or ""))
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
    _lessons_end(data, str(data.get("agent_id") or ""))  # the member's lane ended: what it did not hit was heeded
    # the lane's end tells the tech lead once: what the gates still found, and what of its work counts for nothing
    try:
        told = _findings_told(let_through)
    except Exception as e:  # findings devkit cannot word never keep the lane from ending
        told = ""
        _swallowed("subagent-stop", e)
    progress.on_subagent(agent, data, False, findings=told)
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
                core.replace_text(f, json.dumps(rec))  # its own temporary name: sessions draw the line at once
        except Exception as e:
            _swallowed("statusline", e)
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


# "guard" is the PreToolUse handler's earlier name, kept for sessions whose installed settings still call it
HANDLERS = {"session-start": session_start, "stop": stop, "session-end": session_end, "pre-tool": pre_tool,
            "guard": pre_tool, "config-change": config_change, "instructions-loaded": instructions_loaded, "prompt": prompt,
            "post-tool": post_tool, "post-tool-failure": post_tool_failure, "subagent-start": subagent_start,
            "subagent-stop": subagent_stop, "statusline": statusline, "pre-compact": pre_compact,
            "post-compact": post_compact, "permission-denied": permission_denied}


# --------------------------------------------------------------------------- payload capture

CAPTURES = STATE / "captures"
CAPTURE_KEEP = 20  # captures kept per event (tool events: per event and tool)
CAPTURE_CUT = 200  # characters kept of prompts, tool input and output, the last assistant message and a
                  # compaction's summary
CAPTURE_CUT_KEYS = ("prompt", "tool_input", "tool_response", "last_assistant_message", "compact_summary")
_CAPTURE_TOKEN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}|\beyJ[a-z0-9_-]{8,}\.[a-z0-9_-]{8,}\.[a-z0-9_-]{4,}|"
                            r"(?-i:(?<=\bDEVKIT_CALL=)[A-Za-z0-9_.-]+)")  # and devkit's own call token
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
            data = core.load_json(raw) if raw.strip() else {}
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
    if fn is pre_tool:
        event = "pre-tool"  # records name the handler, whichever name the settings call it by
    if _headless():
        return 0  # devkit's own headless call (ai.ask): no session, no progress, no gates
    state.WAIT, state.SAY_BUSY = state.HOOK_WAIT, False  # a hook waits at most 2 s for a record, silently
    marked = _bug_marks_start(event)
    saved = {}  # the identity variables as they were: none changed until _own_identity runs
    from . import audit, watch
    with audit.hook_identity(), watch.context_notifications():
        try:
            try:
                (STATE / "logs").mkdir(parents=True, exist_ok=True)
            except OSError:  # logs that do not open stop no handler: each log write goes on without its line
                pass
            saved = _own_identity(event, agent)  # inside the try: a hook with no stdin fails open like any other error
            fn(agent)
        except SystemExit:
            raise
        except Exception as e:  # never break the agent session: logged, and the call or turn goes on
            _log(f"{event} {agent} error:\n{audit.redact(traceback.format_exc())}")  # it can quote the payload
            _bug_hook_error(event, e)
        finally:
            _restore_identity(saved)
            if marked:
                _bug_marks_end(event)
    return 0


IDENTITY_VARS = ("DEVKIT_AGENT", "DEVKIT_SESSION", "DEVKIT_FROM_AGENT", "DEVKIT_SPEAKER")


def _own_identity(event: str, agent: str) -> dict:
    """The identity this hook run's records carry (audit.actor, work.actor, audit.context): the agent of the hook's
    arguments and the session of its payload. The variables a hook inherits name whatever launched the agent: a Codex
    started from a Claude Code terminal carries that session, from a crew member's shell its lane too. The member a
    hook acts for is its payload's agent_id, never DEVKIT_FROM_AGENT, and the main session is never DEVKIT_SPEAKER
    (the stamp the PreToolUse hook puts on the main session's `dev watch` and `dev session` calls). The payload stays on stdin.
    -> the variables as they were, for _restore_identity."""
    saved = {k: os.environ.get(k) for k in IDENTITY_VARS}
    try:
        raw = sys.stdin.read()
        sys.stdin = io.StringIO(raw)
        payload = core.load_json(raw) if raw.strip() else {}
        sid = str(payload.get("session_id") or "") if isinstance(payload, dict) else ""
    except (OSError, ValueError, TypeError):
        sid = ""
    try:
        os.environ["DEVKIT_AGENT"] = agent
        if sid:
            os.environ["DEVKIT_SESSION"] = f"{agent}:{sid}"
        elif not os.environ.get("DEVKIT_SESSION", "").startswith(f"{agent}:"):
            os.environ.pop("DEVKIT_SESSION", None)
        os.environ.pop("DEVKIT_FROM_AGENT", None)
        os.environ.pop("DEVKIT_SPEAKER", None)
    except (ValueError, TypeError):  # a session id no environment can hold: the records keep what they inherited
        _log(f"{event} {agent}: identity not set: {traceback.format_exc()[-200:]}")
    return saved


def _restore_identity(saved: dict):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _bug_marks_start(event: str) -> bool:
    """devkit's bug registry: this run's start mark, and the runs killed at their time limit that left one behind.
    Not for the status line, which has no time limit and runs every few seconds."""
    global _PAYLOAD
    _PAYLOAD = {}
    if event == "statusline":
        return False
    try:
        from . import bugs
        bugs.hook_started(event, "")
        bugs.stale_hooks()
        return True
    except Exception:
        _log(f"bug marks {event}: {traceback.format_exc()[-300:]}")
        return False


def _bug_marks_end(event: str):
    try:
        from . import bugs
        bugs.hook_ended(event, str(_PAYLOAD.get("session_id") or ""))
    except Exception:
        _log(f"bug marks {event}: {traceback.format_exc()[-300:]}")


def _bug_hook_error(event: str, exc: BaseException):
    """A hook raised: a `hook` occurrence in devkit's bug registry."""
    try:
        from . import bugs
        bugs.hook_error(event, exc, str(_PAYLOAD.get("session_id") or ""))
    except Exception:
        _log(f"bug record {event}: {traceback.format_exc()[-300:]}")
