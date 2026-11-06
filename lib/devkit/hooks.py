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
        rec = {"agent": agent, "session": sid, "cwd": cwd, "top": top, "started": time.time(),
               "surface": surface(agent, data),
               "head": core.gitout(top, "rev-parse", "HEAD") if top else "",
               "branch": core.gitout(top, "branch", "--show-current") if top else ""}
        try:  # the one writer that creates a session record; one another hook created meanwhile stays
            state.update(f, lambda d: state.SKIP if d else rec)
        except state.StateBusy:
            pass
    return f


def session_start(agent: str):
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    sid = data.get("session_id") or f"pid{os.getppid()}"
    f = _register_session(agent, sid, cwd, data)
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
    _spawn(["config", "repair", "--quiet"])  # restore any devkit-managed setting changed outside devkit
    try:
        from . import work
        work.request_sync("session start", delay=1)  # pull other machines' claims and publish this session
    except Exception:
        pass
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


def handoff_staleness(top: str, note: Path, state_file: Path, email: str, now: float) -> list:
    """Commits on HEAD by `email` newer than the last change of the note's human sections (Current state, Next
    steps, Decisions…) -> their hashes. A task or session line written by devkit is not a change; a teammate's commit
    pulled in is not this developer's work. The first time a note is seen, the handoff history says when it changed."""
    if not note.exists():
        return []
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
            out.append(sha)
    return out


def _handoff_nudge(agent: str, data: dict, sess: dict):
    """End of every development turn: the commits made since the handoff note last really changed → update the note
    and the tasks now, not only at the end. The repository is where the session works now (it may have started
    outside one, as Claude Desktop sessions do), and the check re-arms after each update."""
    if data.get("stop_hook_active") or not core.CFG.get("handoff", {}).get("enforce", True):
        return None
    top = core.gitout(data.get("cwd") or os.getcwd(), "rev-parse", "--show-toplevel") or sess.get("top") or ""
    if not top or core.is_temp(top):
        return None
    from . import work
    pid = _session_project(data, sess)
    email = core.gitout(top, "config", "user.email")
    if not pid or not email:
        return None
    stale = handoff_staleness(top, core.note_path(pid), HANDOFF_CHECK / (pid.replace("/", "__") + ".json"), email,
                              time.time())
    if not stale:
        return None
    a = work.actor(agent, sess.get("session") or "")
    mine = [i["meta"]["id"] for i in work.all_items(pid) if i["meta"].get("status") == "doing" and
            str(i["meta"].get("claimed_by") or "").startswith(f"{a['user']}@{a['host']}")]
    tasks = (f"Tasks: keep {', '.join(mine)} current (`dev task done <id>` once verified; `dev task add \"…\" --why "
             f"\"…\"` for follow-ups)." if mine else
             "Tasks: claim the work item for this work (`dev task start <id>`) or add it (`dev task add \"…\" --now`).")
    return (f"devkit: {len(stale)} commit(s) in {pid} are newer than its handoff note. Update the note now with the "
            f"devkit handoff skill (/devkit:handoff): Current state, Next steps, Decisions and gotchas, short and true. "
            f"{tasks} devkit asks again after every turn with new commits, so the note never falls behind.")


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


def _stub_gate(data: dict, sess: dict):
    """Unfinished work the turn left in the files it edited (stubs, placeholders, TODOs without a task) sends the
    turn back: devkit builds things for real. Fails open."""
    try:
        top, files = _turn_files(data, sess)
        if not files:
            return None
        from . import stubs
        msg = stubs.stop_gate(top, files)
        if msg:
            from . import audit
            audit.record("stubs.gate", top, msg[:300], "blocked")
        return msg
    except Exception:
        _log(f"stop stubs: {traceback.format_exc()[-300:]}")
        return None


def _test_gate(data: dict, sess: dict):
    """Tests the turn edited that cannot fail (no assertion, trivial or swallowed assertions, empty bodies) send the
    turn back with the findings (testaudit.stop_gate). Fails open."""
    try:
        top, edited = _turn_files(data, sess)
        if not top:
            return None
        try:
            from .testx import is_test_file
        except Exception:
            is_test_file = TEST_FILE_RE.search
        files = [f for f in edited if is_test_file(f)]
        if not files:
            return None
        from . import testaudit
        msg = testaudit.stop_gate(top, sorted(set(files)))
        if msg:
            _log(f"stop tests: {len(set(files))} test file(s) with tests that cannot fail")
            from . import audit
            audit.record("tests.gate", top, msg[:300], "blocked")
        return msg
    except Exception:
        _log(f"stop tests: {traceback.format_exc()[-300:]}")
        return None


def stop(agent: str):
    data = _stdin_json()
    from . import canary
    sid = data.get("session_id") or "-"
    sf = _session_file(agent, sid)
    out = {}
    if sf.exists():  # only sessions that started with devkit active (they loaded the global instructions)
        sess = state.read(sf) or {}
        before = copy.deepcopy(sess)  # the gates below take seconds: only what this hook changes is written back
        reasons = []
        try:  # devkit's process is for development turns only (a recipe or a consulting deck gets nothing)
            from . import scope
            dev_turn = scope.decide(data, sess)
        except Exception:
            dev_turn = True
        if not dev_turn:
            _log(f"stop {agent}: not development work ({sess.get('turn_class') or 'no signal'}); devkit stays out")
        res = canary.check(agent, data) if dev_turn else None
        if res:
            ok, pid, line = res
            if ok and line:
                sess["status"] = line
                branch = core.gitout(sess.get("top") or data.get("cwd") or ".", "branch", "--show-current")
                canary.save_status(pid, agent, line, branch or sess.get("branch"))
            elif not ok:
                from . import audit
                audit.record("canary.miss", pid or "", "final message without the status block", "miss")
                sess["canary_missed"] = True  # later prompts in this session get a reminder
                if agent == "claude":
                    out["systemMessage"] = canary.miss_message(agent, sid)
                if core.CFG.get("canary", {}).get("enforce", True):
                    text_ = data.get("last_assistant_message") or canary.last_assistant_text(
                        data.get("transcript_path")) or ""
                    reasons.append(canary.miss_reason(text_) + " Reply now with the block as the very first lines: "
                                   "`**[Firefly> DevKit]** <project> · `<branch>` · <task>`, then the bullets "
                                   "**Done:**, **Check:** (✓/⚠/✗ + evidence) and **Next:** (global instructions §0).")
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
                    canary.save_status(pid_, agent, line, core.gitout(top, "branch", "--show-current"))
                    if sess.get("canary_missed"):
                        canary.record_corrected(agent, sid, pid_, line)
        try:
            from . import skillguard
            why = skillguard.on_stop(agent, data, sess) if dev_turn else None
            if why:
                reasons.append(why)
        except Exception:
            _log(f"stop skills {agent}: {traceback.format_exc()[-300:]}")
        if dev_turn and not data.get("stop_hook_active"):
            for gate in (_stub_gate, _test_gate):
                why = gate(data, sess)
                if why:
                    reasons.append(why)
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
                    reasons.append("devkit: this session fixed or reverted something (" + "; ".join(fixes[:3]) +
                                   "). If it was a mistake that could happen again, record a lesson: `dev lesson add "
                                   "\"<title>\" --cause \"…\" --prevent \"…\" --evidence \"commit …\"` (or "
                                   "`dev lesson recur <id>` if a known pitfall repeated). Skip it for a trivial fix.")
            except Exception:
                _log(f"stop lessons {agent}: {traceback.format_exc()[-300:]}")
        try:  # crew members may be writing plans right now; their own `dev plan revise` records them
            from . import progress as _pg
            busy = _pg.crew_working(_pg.load_run(agent, sid) or {})
        except Exception:
            busy = []
        plans = _plans_without_reason(sess, _session_project(data, sess)) if dev_turn and not busy else []
        if plans and not data.get("stop_hook_active"):
            reasons.append("devkit: you changed plan(s) without recording why: " + ", ".join(plans[:4]) +
                           ". Run `dev plan revise <file> --why \"…\"` for each (it versions the plan and logs the "
                           "reason), then finish.")
        nudge = _handoff_nudge(agent, data, sess) if dev_turn else None
        if nudge:
            reasons.append(nudge)
        if reasons:
            out.update({"decision": "block", "reason": " ".join(reasons)})
        else:
            from . import progress
            progress.on_turn_end(agent, sid, sess)
            sess["turn_started"] = None
        _patch_quietly(sf, before, sess)  # a subagent that started meanwhile stays; an ended session stays ended
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
    settings, where devkit manages some; a project's settings get none."""
    from . import guard, policy
    flat = policy.claude_desired()[0] if managed else {}
    want = {p[1]: json.dumps(v) for p, v in flat.items() if len(p) == 2 and p[0] == "env"
            and str(p[1]).startswith("DEVKIT_")}
    try:
        have = guard.devkit_env(Path(path).read_text())
    except OSError:
        return []
    changed = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
    if changed:
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
    if sid:
        _spawn_sync(["--session", f"{agent}:{sid}", "--finish"])


# --------------------------------------------------------------------------- guard

SHELL_TOOLS = ("Bash", "exec_command", "shell", "local_shell", "unified_exec")
# what a subagent runs as itself: dev progress …, and dev crew ask, answer, inbox, decide, decisions, mode, delegate,
# decline and route
DEV_LANE_CMD = re.compile(r"^\s*(?:\S*/)?dev\s+(?:progress(?:\s|$)|"
                          r"crew\s+(?:ask|answer|inbox|decide|decisions|mode|delegate|decline|route)(?:\s|$))")


def _runs_more_than_one_command(cmd: str) -> bool:
    """True when the shell would do more than run this one program: an operator, redirect or newline outside quotes,
    a command substitution where the shell expands it (unquoted or inside double quotes), or an unterminated quote.
    Single quotes are literal; `;` inside any quotes is just text."""
    i, n, q = 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if q == "'":
            q = None if c == "'" else q
        elif q == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                q = None
            elif c == "`" or cmd.startswith("$(", i):
                return True
        elif c == "\\":
            i += 1
        elif c in "'\"":
            q = c
        elif c in ";&|<>\n`" or cmd.startswith("$(", i):
            return True
        i += 1
    return q is not None


def _unroutable_lane_command(data: dict, agent: str = "claude"):
    """Inside a subagent, a shell command that runs a dev progress / dev crew call devkit cannot route to the
    subagent's lane would act as the main session: advance the main plan, or record the developer's decision. The
    guard judges what the shell runs (guard.crew_shell_verdict): read-only crew commands and text that only mentions
    lane commands pass; a team change, or `dev crew sync` outside the step's scope, is refused. -> the refusal, or None."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return None
    if _route_subagent_progress(data):
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(shlex.quote(str(c)) for c in cmd) if isinstance(cmd, list) else str(cmd)
    aid, scope, member = data["agent_id"], None, str(data["agent_id"])
    try:
        from . import progress
        run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
        if lane:
            scope, member = progress.lane_scope(run, aid), _member_folder(run, aid)
    except Exception:
        _log(f"lane command {agent}: {traceback.format_exc()[-300:]}")
    from . import guard
    v = guard.crew_shell_verdict(cmd, data.get("cwd") or os.getcwd(), scope, member)
    return v[1] if v else None


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


def _route_subagent_progress(data: dict):
    """Inside a subagent (the hook input carries `agent_id`, in Claude Code and Codex), a single `dev progress …`
    or `dev crew ask|answer|inbox|decide|decisions|mode|delegate|decline|route` command gets DEVKIT_FROM_AGENT, so it
    updates that subagent's lane (its messages, decisions and delegations come from it, and `decide` refuses it) and
    never advances the main plan.
    Compound commands are left alone: the rewrite must not approve anything but that one command."""
    aid = data.get("agent_id")
    inp = data.get("tool_input") or {}
    cmd = inp.get("command")
    if not aid or data.get("tool_name") not in SHELL_TOOLS or not isinstance(cmd, str) or not DEV_LANE_CMD.match(cmd):
        return None
    if _runs_more_than_one_command(cmd):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(aid)):
        return None
    return {**inp, "command": f"DEVKIT_FROM_AGENT={aid} {cmd.strip()}"}


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch")


COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+(\S+)\s+)?commit\b|\bdev\s+commit\b")


def _stub_commit_gate(data: dict):
    """A commit that would include unfinished work (stubs, placeholders, TODOs without a task) is refused.
    DEVKIT_ALLOW_STUBS=1 when the developer accepts them."""
    if data.get("tool_name") not in SHELL_TOOLS or os.environ.get("DEVKIT_ALLOW_STUBS"):
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    m = COMMIT_RE.search(command_words(cmd))  # a commit the command runs, not the text it writes
    if not m or "DEVKIT_ALLOW_STUBS=1" in cmd or "--wip" in cmd:
        return None
    try:
        cwd = data.get("cwd") or os.getcwd()
        where = m.group(1) or cwd
        top = core.gitout(where if os.path.isabs(where) else os.path.join(cwd, where), "rev-parse", "--show-toplevel")
        if not top:
            return None
        from . import stubs
        why = stubs.commit_gate(top, cmd)
        return ("deny", why) if why else None
    except Exception:
        _log(f"stub commit gate: {traceback.format_exc()[-300:]}")
        return None


def _crew_limits(agent: str, data: dict):
    """Coordination guardrails -> ("deny", reason) or None:
    - at most `progress.max_parallel` crew members at once (default 4; 1 while the 5-hour usage is above
      progress.quota_percent), counting the helpers members launch, and no launch deeper than progress.max_depth;
    - the main session does not edit files a running crew member owns (DEVKIT_SCOPE_OVERRIDE=1 for an emergency)."""
    try:
        from . import progress
        sid = data.get("session_id") or "-"
        run = progress.load_run(agent, sid)
        if not run or run.get("state") == "done":
            return None
        run = progress.normalize(run)
        progress.expire_stale(run)
        tool = data.get("tool_name")
        if tool in ("Agent", "Task", "spawn_agent"):  # at every depth (spec F3): a member's helper takes a slot too
            why = progress.launch_verdict(agent, data, run)
            if why:
                return ("deny", why)
        if tool in EDIT_TOOLS and not data.get("agent_id") and not os.environ.get("DEVKIT_SCOPE_OVERRIDE"):
            inp = data.get("tool_input") or {}
            path = inp.get("file_path") or inp.get("notebook_path") or ""
            if path:
                cwd = data.get("cwd") or os.getcwd()
                top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
                rel = os.path.relpath(os.path.normpath(path if os.path.isabs(path) else os.path.join(cwd, path)), top)
                k, lane = progress.scope_owner(run, rel)
                if lane:
                    who = lane.get("persona") or lane.get("label") or "a crew member"
                    return ("deny", f"{rel} belongs to {who} while they work (scope {', '.join(lane['scope'])}). Ask "
                                    f"them (dev crew ask --to \"{who}\" \"…\") or wait until they finish; "
                                    f"DEVKIT_SCOPE_OVERRIDE=1 only for an emergency.")
    except Exception:
        _log(f"crew limits {agent}: {traceback.format_exc()[-300:]}")
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
    launches has the member's tool level and scope, never more (spec F3). -> the refusal, or None."""
    aid = data.get("agent_id")
    if not aid or data.get("tool_name") not in EDIT_TOOLS:
        return None
    try:
        from . import progress
        run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
        if not lane:
            return None
        if lane.get("binding") == "conflict":  # a launch binds once: a second agent claiming it edits nothing
            return ("this agent claims a launch that another agent is bound to, so devkit lets it read but not edit. "
                    "Report what you need to change, and Morpheus checks the launch (dev crew graph).")
        if lane.get("binding") == "unbound":  # its launch record was lost: restricted until Morpheus assigns it
            return ("devkit has no record of the launch that started this agent, so it may read but not edit. Report "
                    "what you need to change; Morpheus can assign you your step (dev progress assign \"<step>\" "
                    "--to \"<you>\"), which lifts this.")
        chain = [run["agents"][k] for k in progress.lane_chain(run, aid)]
        inp = data.get("tool_input") or {}
        paths = [inp.get("file_path") or inp.get("notebook_path") or ""]
        if data.get("tool_name") == "apply_patch" or not paths[0]:
            body = inp.get("input") or inp.get("patch") or inp.get("command") or ""
            paths = re.findall(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", str(body)) or paths
        cwd = data.get("cwd") or os.getcwd()
        top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
        name = lane.get("persona") or lane.get("label") or "this crew member"
        head = chain[-1].get("persona") or chain[-1].get("label") or "its crew member"
        if len(chain) > 1:  # a helper: the limits it inherits are its member's
            name = f"this helper of {head}"
        level = progress.lane_tools(run, aid)
        layers = progress.lane_scope(run, aid)
        member = _member_folder(run, aid)
        from . import guard
        for p_ in [x for x in paths if x]:  # each member keeps to its own folder of the session scratchpad
            why = guard.scratch_refusal(os.path.normpath(os.path.join(cwd, p_)), member)
            if why:
                return why
        # its scratch folder and temp files, and this project's plans that its step's scope names (guard rules)
        paths = [p_ for p_ in paths if p_ and not guard.member_write_exempt(p_, cwd, layers, member)]
        if not paths:
            return None
        if level in ("review", "read"):
            return (f"{name} is {'review-only' if level == 'review' else 'read-only'}"
                    f"{f', like {head}' if len(chain) > 1 else ''}: report the change with file:line and the fix, "
                    f"and Morpheus assigns it. It may write its scratch folder, and this project's plans in the "
                    f"handoff repository when its step's scope names them.")
        if not layers:
            return None
        for p_ in paths:
            ap = os.path.normpath(p_ if os.path.isabs(p_) else os.path.join(cwd, p_))
            rel = os.path.relpath(ap, top)
            if rel.startswith(".."):  # a worktree of the same repository: the scope's paths are relative to it
                wt = _repo_top_of(ap)
                if wt and _same_repository(wt, top):
                    rel = os.path.relpath(os.path.realpath(ap), wt)
            if not progress.in_scope(rel, layers):
                return (f"{rel} is outside {name}'s scope ({' and '.join(', '.join(g) for g in layers)}): another "
                        f"crew member may own it. "
                        f"Ask Morpheus to widen it: dev crew ask \"may I change {rel}? why: …\"")
    except Exception:
        return None
    return None


def guard(agent: str):
    if os.environ.get("DEVKIT_GUARD", "").lower() == "off":
        return
    data = _stdin_json()
    try:
        from . import progress
        progress.tool_started(data)
    except Exception:
        pass
    if data.get("agent_id"):  # late binding (spec D2): this call already runs with its launch's step and scope
        try:
            from . import progress
            progress.bind(agent, data)
        except Exception:
            _log(f"guard bind {agent}: {traceback.format_exc()[-300:]}")
    from . import guard as g
    verdict = g.check(data.get("tool_name") or "", data.get("tool_input") or {}, data.get("cwd") or os.getcwd(), agent)
    if not verdict:
        verdict = _crew_limits(agent, data) or _stub_commit_gate(data)
    if not verdict:
        if data.get("tool_name") in ("Agent", "Task", "spawn_agent"):  # a subagent launch: its label for progress
            try:
                from . import progress
                progress.note_agent_launch(agent, data)
            except Exception:
                pass
        why = _scope_violation(agent, data)
        if why:
            _log(f"guard {agent} deny: {why[:160]}")
            from . import audit
            audit.record("guard.deny", (data.get("tool_input") or {}).get("file_path") or "", why[:400], "deny",
                         tool=data.get("tool_name") or "", rule="crew-scope")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": why}}))
            return
        routed = _route_subagent_progress(data)
        if routed:
            _log(f"progress {agent}: a subagent's dev progress call goes to its lane")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                     "updatedInput": routed}}))
            return
        why = _unroutable_lane_command(data, agent)
        if why:
            from . import audit
            audit.record("guard.deny", (data.get("tool_input") or {}).get("command", "")[:200], why, "deny",
                         tool=data.get("tool_name") or "", rule="crew-lane")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                     "permissionDecisionReason": why}}))
            return
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
                print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                         "updatedInput": updated}}))
        return
    decision, reason = verdict
    if agent == "codex" and decision == "ask":
        decision = "deny"
    _log(f"guard {agent} {decision}: {reason[:160]}")
    from . import audit
    tin = data.get("tool_input") or {}
    audit.record(f"guard.{decision}", tin.get("command") or tin.get("file_path") or data.get("tool_name") or "",
                 reason[:400], decision, tool=data.get("tool_name") or "")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": decision,
                                             "permissionDecisionReason": reason}}))


def _context_out(event: str, text: str):
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def morpheus_hint(text: str, roster=None) -> str:
    """The route Morpheus takes for a development request: each kind of work it contains (the crew's RACI), who does
    it, who approves it, and whom to consult. '' when the request is not clearly any kind of work."""
    try:
        from . import raci
        if len(str(text or "").split()) < 4:
            return ""
        every = raci.classify(text, roster, limit=30)
        ranked = [x for x in every if x[1] >= raci.MIN_SCORE][:4]
    except Exception:
        return ""
    mentioned = [x for x in every if x[1] >= 2]
    if len(mentioned) >= 4:  # it names many kinds of work (often as examples): Morpheus decides, no single route
        names_ = ", ".join(raci._lower(a["name"]) for a, _, _ in mentioned[:6])
        return (f"devkit · you are Morpheus: this request mentions {len(mentioned)} kinds of work ({names_}). Decide "
                "whether it is one change to how devkit or the crew works, or several pieces of work; plan one step "
                "per kind of work and route each by the RACI (`dev crew raci`).")
    if not ranked:
        return ""
    parts = []
    for act, _, hits in ranked:
        line = f"{raci._lower(act['name'])} → {raci.names(act.get('R'), roster)} does it"
        if act.get("A") not in (act.get("R") or []):
            line += f", {raci.names([act['A']], roster)} approves"
        if act.get("C"):
            line += f", consult {raci.names(act['C'], roster)}"
        parts.append(line)
    if len(parts) == 1:
        head = f"devkit · you are Morpheus: this request is {parts[0]} (the crew's RACI)."
    else:
        head = ("devkit · you are Morpheus: this request spans " + str(len(parts)) + " kinds of work (the crew's "
                "RACI): " + "; ".join(parts) + ". Plan one step per kind of work and assign each to its member.")
    return (head + " One kind of work under ten minutes: do it yourself. Otherwise plan it (`dev progress start "
            "--plan`), assign the steps (`dev progress assign <step> --to <member>`) and brief the members "
            "(`dev crew brief <member> \"<task>\"`); the run ends when each approver has reviewed.")


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
        _register_session(agent, sid, cwd, data)
        extra = context_for(cwd, agent)
        _log(f"prompt {agent}: session {sid[:12]} had no SessionStart; devkit context injected")
    raw = (data.get("prompt") or "").lstrip()
    if raw.startswith(("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")) or \
            "<task-notification>" in raw[:400]:
        _context_out("UserPromptSubmit", extra)  # background events are not the developer's request
        return
    text = progress.on_prompt(agent, data)
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
    _context_out("UserPromptSubmit", "\n\n".join(x for x in (extra, route, text) if x))


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


CI_TRIGGER = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b|\bdev\s+push\b|\b(?:gh|dev)\s+pr\s+(?:create|merge)\b|"
                        r"\bdev\s+release\b")


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


def closing_reminder(data: dict, sess: dict, now: float) -> str:
    """PostToolUse, main session, development turns: right after a call that usually ends the turn, the status block
    the final message opens with, with this project and branch filled in. Records when it reminded in `sess`."""
    what = _turn_ending(data)
    if not what or data.get("agent_id"):
        return ""
    if sess.get("turn_class") == "other" and os.environ.get("DEVKIT_SCOPE") != "all":
        return ""
    last = sess.get("closing_nudge") or 0
    if last >= (sess.get("turn_started") or 0) and now - last < CLOSING_GAP:
        return ""
    sess["closing_nudge"] = now
    # the repository of the command's folder (the session may have started outside one), else the one it started in
    top = (core.gitout(data["cwd"], "rev-parse", "--show-toplevel") if data.get("cwd") else "") or sess.get("top") or ""
    if top:
        from .registry import main_of
        project = (main_of(Path(top)) or Path(top)).name
    else:
        project = (sess.get("project") or "").rstrip("/").rsplit("/", 1)[-1] or "<project>"
    branch = (core.gitout(top, "branch", "--show-current") if top else "") or "-"
    return (f"devkit · after {what}: if you end the turn here (to wait, or because the work is done), your final "
            f"message opens with the status block: **[Firefly> DevKit]** {project} · `{branch}` · <task>, then "
            f"Done / Check / Next. Nothing above it: a waiting note or a comment on this output goes below the block.")


def post_tool(agent: str):
    """PostToolUse: time slow calls; remind the agent to update the developer after a long silence; sync the
    handoff repository right after a delivery event (commit, push, merge, tag, PR, release); at a call that usually
    ends the turn, show the status block the final message opens with."""
    from . import progress
    data = _stdin_json()
    try:
        if data.get("tool_name") in ("Agent", "Task"):  # the launch names the agent it started: an exact binding
            progress.launch_result(agent, data)
        if data.get("agent_id"):
            progress.bind(agent, data)
    except Exception:
        _log(f"post-tool bind {agent}: {traceback.format_exc()[-300:]}")
    try:
        cmd = (data.get("tool_input") or {}).get("command") or ""
        if isinstance(cmd, list):
            cmd = " ".join(cmd)
        words = command_words(cmd)
        if data.get("tool_name") in ("Bash", "exec_command", "shell") and DELIVERY.search(words):
            from . import work
            work.request_sync("delivery: " + DELIVERY.search(words).group(0))
        if data.get("tool_name") in SHELL_TOOLS and CI_TRIGGER.search(words) and not data.get("agent_id"):
            from . import ciwatch  # a push or PR starts CI: watch it, and tell this session how it ended
            cwd = data.get("cwd") or os.getcwd()
            top = core.gitout(cwd, "rev-parse", "--show-toplevel")
            if top and not core.is_temp(top):
                ciwatch.start_background(top, session=f"{agent}:{data.get('session_id') or ''}")
    except Exception:
        pass
    closing = ""
    try:
        sf = _session_file(agent, data.get("session_id") or "-")
        if sf.exists():  # only sessions that started with devkit active
            sess = state.read(sf) or {}
            before = copy.deepcopy(sess)
            closing = closing_reminder(data, sess, time.time())
            if closing:  # only what the reminder changed, on the fresh record: a background launch's SubagentStart
                _patch_quietly(sf, before, sess)  # runs at the same moment and claims its launch entry
    except Exception:
        _log(f"post-tool closing {agent}: {traceback.format_exc()[-300:]}")
    _context_out("PostToolUse", "\n\n".join(x for x in (_bulk_rewrite_note(data), closing,
                                                        progress.on_tool_done(agent, data)) if x))


def post_tool_failure(agent: str):
    """PostToolUseFailure: a subagent launch that failed (an unknown agent type, an interrupt) starts no agent, so its
    launch record leaves at once: it never labels another agent or holds a crew slot (spec F0, S6)."""
    from . import progress
    data = _stdin_json()
    if data.get("tool_name") in ("Agent", "Task", "spawn_agent"):
        progress.launch_failed(agent, data)


def subagent_start(agent: str):
    from . import progress
    progress.on_subagent(agent, _stdin_json(), True)


def _launch_meta(data: dict) -> dict:
    """What launched this subagent: Claude Code keeps `<transcript>.meta.json` (toolUseId, description) next to the
    transcript of every subagent the Agent tool started. {} for the agent's own helpers (compaction, suggestions)."""
    p = str(data.get("agent_transcript_path") or "")
    if not p.endswith(".jsonl"):
        return {}
    meta = core.read_json(Path(p[:-len(".jsonl")] + ".meta.json"), {}) or {}
    return meta if isinstance(meta, dict) and (meta.get("toolUseId") or meta.get("description")) else {}


def _who(name: str) -> str:
    """A crew member as developers see it, role first ("Backend engineer (Tank)"); the name alone otherwise."""
    try:
        from . import crew
        m = crew._for_label(name)
        return crew.who(m) if m else name
    except Exception:
        return name


def subagent_stop(agent: str):
    """SubagentStop: a crew member's report must start with its own status block (the canary covers the crew too);
    without it the member is sent back once to write it, and the correction is recorded like the main canary's. Only
    subagents someone launched owe a report (a lane, or the Agent tool's launch record); the agent's own helpers do
    not. Then its lane and step are closed."""
    from . import progress
    data = _stdin_json()
    out = None
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
            if lane and lane.get("binding") == "pending":
                lane = None  # never bound to a launch: a helper owes no report
            meta = _launch_meta(data)
            label = (lane or {}).get("label") or meta.get("description") or ""
            name = (lane or {}).get("persona") or progress.persona(data.get("agent_type") or "", label) \
                or label.split(":")[0].strip() or "Agent"
            pid = (run or {}).get("project") or sess.get("project")
            pending = sess.setdefault("crew_pending", {})
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
                       f"- **Next:**; then what you deliver and \"Needs from Morpheus:\" (or nothing). Nothing above "
                       f"the block."}
            elif run and lane and aid not in sess.setdefault("crew_comms", {}) and \
                    progress.lane_unreported(run, aid) and core.CFG.get("canary", {}).get("enforce", True):
                from . import audit  # the crew talks through devkit: the step's evidence goes in the run, live
                audit.record("crew.unreported", pid or "", f"{name} stopped without dev progress step", "miss")
                sess["crew_comms"][aid] = name
                _patch_quietly(sf, before, sess)
                out = {"decision": "block", "reason":
                       f"devkit · {_who(name)}, record your work through devkit before you stop: `dev progress step "
                       f"\"<what finished>\" --check \"<evidence>\"` (it goes to your lane, and Morpheus and the "
                       f"developer see it live). Questions and decisions go through `dev crew ask`, not only in the "
                       f"report. Then end with the same report."}
            if out is None and (lane or meta):  # the crew gates: proofs, skills, stubs, tests, lessons; once
                from . import gates
                ref = copy.deepcopy(sess)
                out = gates.check(name, data, sess, pid)
                if sess != ref:  # only the gates' own mark, on the fresh record
                    _patch_quietly(sf, ref, sess)
    except Exception:
        _log(f"subagent-stop {agent}: {traceback.format_exc()[-300:]}")
    if out:
        print(json.dumps(out))
        return
    progress.on_subagent(agent, data, False)


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
            "subagent-stop": subagent_stop, "statusline": statusline}


# --------------------------------------------------------------------------- payload capture (spec C9)

CAPTURES = STATE / "captures"
CAPTURE_KEEP = 20  # captures kept per event (tool events: per event and tool)
CAPTURE_CUT = 200  # characters kept of prompts, tool input and output, and the last assistant message
CAPTURE_CUT_KEYS = ("prompt", "tool_input", "tool_response", "last_assistant_message")
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
    state.WAIT, state.SAY_BUSY = state.HOOK_WAIT, False  # a hook waits at most 2 s for a record, silently
    try:
        (STATE / "logs").mkdir(parents=True, exist_ok=True)
        fn(agent)
    except SystemExit:
        raise
    except Exception:  # fail open: never break the agent session
        _log(f"{event} {agent} error:\n{traceback.format_exc()}")
    return 0
