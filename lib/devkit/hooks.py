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
import re
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import core
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


def _register_session(agent: str, sid: str, cwd: str, data: dict = None) -> Path:
    f = _session_file(agent, sid)
    if not f.exists():
        top = core.gitout(cwd, "rev-parse", "--show-toplevel")
        f.parent.mkdir(parents=True, exist_ok=True)
        from .canary import surface
        f.write_text(json.dumps({"agent": agent, "session": sid, "cwd": cwd, "top": top, "started": time.time(),
                                 "surface": surface(agent, data),
                                 "head": core.gitout(top, "rev-parse", "HEAD") if top else "",
                                 "branch": core.gitout(top, "branch", "--show-current") if top else "",
                                 "note_hash": _note_hash(top) if top and not core.is_temp(top) else ""}))
    return f


def session_start(agent: str):
    data = _stdin_json()
    cwd = data.get("cwd") or os.getcwd()
    sid = data.get("session_id") or f"pid{os.getppid()}"
    f = _register_session(agent, sid, cwd, data)
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
        sess = core.read_json(sf, {}) or {}
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
        plans = _plans_without_reason(sess) if dev_turn else []
        if plans and not data.get("stop_hook_active"):
            reasons.append("devkit: you changed plan(s) without recording why: " + ", ".join(plans[:4]) +
                           ". Run `dev plan revise <file> --why \"…\"` for each (it versions the plan and logs the "
                           "reason), then finish.")
        nudge = _handoff_nudge(agent, data, sess) if dev_turn else None
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
    from . import audit
    audit.record("config.blocked", path, keys, "blocked")
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

SHELL_TOOLS = ("Bash", "exec_command", "shell", "local_shell", "unified_exec")
# what a subagent runs as itself: dev progress …, and dev crew ask, answer, inbox, decide, decisions, mode, delegate,
# decline and route
DEV_LANE_CMD = re.compile(r"^\s*(?:\S*/)?dev\s+(?:progress(?:\s|$)|"
                          r"crew\s+(?:ask|answer|inbox|decide|decisions|mode|delegate|decline|route)(?:\s|$))")


ANY_LANE_CMD = re.compile(r"(?:^|[\s;&|(`])(?:\S*/)?dev\s+(?:progress|crew)\b")


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


def _unroutable_lane_command(data: dict):
    """Inside a subagent, a dev progress / dev crew command devkit cannot route to the subagent's lane (it is part of
    a compound command) would act as the main session: advance the main plan, or record the developer's decision.
    Refused, with the fix."""
    if not data.get("agent_id") or data.get("tool_name") not in SHELL_TOOLS:
        return None
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    if not ANY_LANE_CMD.search(cmd) or _route_subagent_progress(data):
        return None
    return ("Run `dev progress …` and `dev crew …` as a command of its own (no `&&`, `;`, pipes or `$(…)`), so devkit "
            "can route it to your lane. Inside a compound command it would act as Morpheus.")


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
    - at most `progress.max_parallel` crew members at once (default 4);
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
        if tool in ("Agent", "Task", "spawn_agent") and not data.get("agent_id"):
            busy = progress.running_agents(run)
            limit = progress.cfg()["max_parallel"]
            if len(busy) >= limit:
                names = ", ".join(v.get("persona") or v.get("label") or k[:8] for k, v in busy)
                return ("deny", f"{len(busy)} crew members are working ({names}); the limit is {limit} at once. Wait "
                                f"for one to finish (you are notified), or give the work to one of them "
                                f"(SendMessage / send_input). progress.max_parallel in devkit config changes the limit.")
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


def _scope_violation(agent: str, data: dict):
    """A crew member assigned a scope (dev progress assign … --scope "globs") edits only those files, so parallel
    agents never write the same file. -> the refusal, or None."""
    aid = data.get("agent_id")
    if not aid or data.get("tool_name") not in EDIT_TOOLS:
        return None
    try:
        from . import progress
        import fnmatch
        run, lane = progress.lane_of(agent, data.get("session_id") or "-", aid)
        if not lane:
            return None
        scope = lane.get("scope") or []
        inp = data.get("tool_input") or {}
        paths = [inp.get("file_path") or inp.get("notebook_path") or ""]
        if data.get("tool_name") == "apply_patch" or not paths[0]:
            body = inp.get("input") or inp.get("patch") or inp.get("command") or ""
            paths = re.findall(r"\*\*\* (?:Add|Update|Delete) File: (\S+)", str(body)) or paths
        cwd = data.get("cwd") or os.getcwd()
        top = core.gitout(cwd, "rev-parse", "--show-toplevel") or cwd
        name = lane.get("persona") or lane.get("label") or "this crew member"
        try:
            from . import crew
            mem = crew.member(lane.get("persona") or "") if lane.get("persona") else None
        except Exception:
            mem = None
        if mem and mem.get("tools") in ("review", "read"):
            return (f"{name} is {'review-only' if mem['tools'] == 'review' else 'read-only'}: report the change with "
                    f"file:line and the fix, and Morpheus assigns it.")
        if not scope:
            return None
        for p_ in [x for x in paths if x]:
            ap = os.path.normpath(p_ if os.path.isabs(p_) else os.path.join(cwd, p_))
            rel = os.path.relpath(ap, top)
            if not any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.rstrip("/") + "/**") or rel.startswith(
                    g.rstrip("*").rstrip("/") + "/") or rel == g for g in scope):
                return (f"{rel} is outside {name}'s scope ({', '.join(scope)}): another crew member may own it. "
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
        why = _unroutable_lane_command(data)
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
    from . import audit
    tin = data.get("tool_input") or {}
    audit.record(f"guard.{decision}", tin.get("command") or tin.get("file_path") or data.get("tool_name") or "",
                 reason[:400], decision, tool=data.get("tool_name") or "")
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
        _register_session(agent, sid, cwd, data)
        extra = context_for(cwd, agent)
        _log(f"prompt {agent}: session {sid[:12]} had no SessionStart; devkit context injected")
    raw = (data.get("prompt") or "").lstrip()
    if raw.startswith(("<task-notification", "[SYSTEM NOTIFICATION", "<system-reminder", "<agent-message")) or \
            "<task-notification>" in raw[:400]:
        _context_out("UserPromptSubmit", extra)  # background events are not the developer's request
        return
    try:  # development or not? A follow-up with no signal keeps the previous turn's class
        from . import scope
        sf0 = _session_file(agent, sid) if sid else None
        s0 = (core.read_json(sf0, {}) or {}) if sf0 and sf0.exists() else {}
        cls = scope.resolve(raw, s0.get("turn_class"))
        if sf0 and sf0.exists():
            s0["turn_class"] = cls
            sf0.write_text(json.dumps(s0))
    except Exception:
        cls = None
    if cls == "other":  # a recipe, a general question, consulting work: no devkit context, reminders or skills
        _log(f"prompt {agent}: not development work; devkit stays out of this turn")
        _context_out("UserPromptSubmit", "")
        return
    try:
        from . import lessons as _les
        sfile = _session_file(agent, sid) if sid else None
        _proj = ((core.read_json(sfile, {}) or {}).get("project") or "") if sfile and sfile.exists() else ""
        _les.note_correction(data.get("prompt") or "", _proj)
    except Exception:
        pass
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
    skills = ""
    try:
        from . import skillguard
        sf = _session_file(agent, sid) if sid else None
        sess = (core.read_json(sf, {}) or {}) if sf and sf.exists() else {}
        skills = skillguard.on_prompt(agent, data.get("prompt") or "", sess)
    except Exception:
        _log(f"prompt skills {agent}: {traceback.format_exc()[-300:]}")
    _context_out("UserPromptSubmit", "\n\n".join(x for x in (extra, skills, text) if x))


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
_AT_CMD = r"(?:^|[;&|(\n]\s*|\bxargs\s+(?:-\S+\s+)*)"
SED_INPLACE = re.compile(_AT_CMD + r"(?:sed\s+(?:-[a-zA-Z]+\s+)*-i|perl\s+-[a-zA-Z]*i)")


def _bulk_rewrite_note(data: dict) -> str:
    """A shell command that rewrote files with a regex (sed -i, perl -pi, a script with re.sub that writes files):
    the agent reads every changed line before it tests, syncs or commits."""
    if data.get("tool_name") not in SHELL_TOOLS:
        return ""
    cmd = (data.get("tool_input") or {}).get("command") or ""
    cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
    scripted = "re.sub(" in cmd and re.search(r"\.write_text\(|\.write\(|open\([^)]*[\"']w", cmd)
    if not (scripted or SED_INPLACE.search(cmd)):
        return ""
    return ("A regex just rewrote files. Read every changed line (`git diff -U0 -- <files>`; for untracked files, "
            "compare with what they said before) before you test, sync or commit: a pattern cannot parse nested "
            "expressions or prose, and it changes text it should not. Prefer exact-string edits that must match once.")


def post_tool(agent: str):
    """PostToolUse: time slow calls; remind the agent to update the developer after a long silence; sync the
    handoff repository right after a delivery event (commit, push, merge, tag, PR, release)."""
    from . import progress
    data = _stdin_json()
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
    _context_out("PostToolUse", "\n\n".join(x for x in (_bulk_rewrite_note(data), progress.on_tool_done(agent, data)) if x))


def subagent_start(agent: str):
    from . import progress
    progress.on_subagent(agent, _stdin_json(), True)


def subagent_stop(agent: str):
    """SubagentStop: the crew member's report must start with its own status block (the canary covers the crew too);
    without it the member is sent back once to write it. Then its lane and step are closed."""
    from . import progress
    data = _stdin_json()
    out = None
    try:
        sid = data.get("session_id") or "-"
        sf = _session_file(agent, sid)
        sess = (core.read_json(sf, {}) or {}) if sf.exists() else None
        if sess is not None and sess.get("turn_class") != "other" and not data.get("stop_hook_active"):
            from . import canary
            run, lane = progress.lane_of(agent, sid, data.get("agent_id") or "")
            name = (lane or {}).get("persona") or progress.persona(data.get("agent_type") or "",
                                                                  (lane or {}).get("label") or "") \
                or ((lane or {}).get("label") or "").split(":")[0].strip() or "Agent"
            pid = (run or {}).get("project") or sess.get("project")
            if not canary.check_subagent(agent, data, sess, pid, name):
                from . import audit
                audit.record("canary.crew-miss", pid or "", f"{name}'s report had no status block", "miss")
                out = {"decision": "block", "reason":
                       f"devkit · end your report with your status block first, exactly: **[Firefly> DevKit · {name}]** "
                       f"<project> · `<branch>` · <task>, then - **Done:**, - **Check:** (✓/⚠/✗ + evidence) and "
                       f"- **Next:**; then what you deliver and \"Needs from Morpheus:\" (or nothing). Nothing above "
                       f"the block."}
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
            "post-tool": post_tool, "subagent-start": subagent_start, "subagent-stop": subagent_stop,
            "statusline": statusline}


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
