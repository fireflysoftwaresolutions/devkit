# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The welcome the developer reads when a session starts: `dev status` in at most five lines, said once.

It answers what a developer asks first, in that order: does anything need me, what is running, what is next, where
am I. The first line carries the verdict on its own (the ChatGPT app may show only that line); every other line
starts with a one-character rail mark, in a fixed order, so the eye learns where to look:

    devkit · billing · 1 thing needs you
    ! Decision waits: export cancelled invoices too?
    ▸ Invoice export · step 3 of 7 · ~15 min left · 2 crew working
    › Next: wire the export endpoint
    · feat/invoice-export · 3 changed files · CI running, watched

`!` needs you, `▸` running (or what happened since you left), `✓` the last update, `›` next, `·` where (branch,
changes, CI; outside a project, the recent projects), `+` a devkit update. The SessionStart hook sends it as the
`systemMessage` on Claude Code and Codex, and tells the model the developer saw it (for_model).

It reads devkit's recorded state only: no network, no model, and no git when the hook hands it the inspection
context_for made. Each source is read on its own: one that fails removes its line, and when the needs cannot be read
the verdict is "see dev status", never "nothing needs you". It never raises: on any error it says less, or nothing.

Widths are sized for an 80-column terminal under each surface's prefix: the first line at most 46 columns (Claude
Code's `SessionStart:startup says: ` takes 32), every other line at most 70 (the hanging indent takes 5). The copy
follows the UX designer's rules (plain words, no Markdown, no ids, no paths, crew members as "Role (Name)") and
lives here, in one place, so it can be translated later.
"""
from __future__ import annotations

import os
import re
import time
import traceback
import unicodedata
from pathlib import Path

from . import core

HEAD_MAX = 46  # the first line, under Claude Code's 32-column prefix at 80 columns
LINE_MAX = 70  # every other line, under a 5-column hanging indent, with room to spare
BRANCH_MAX = 38  # a branch in the footer, cut in the middle past this
BRANCH_MIN = 20  # how far a branch is cut before the footer drops its CI segment
GOAL_MIN = 16  # a run's goal is never cut below this: the crew count, then the time left go first
NAME_MAX = 17  # a project id in a line outside a project, as the headline's room holds it (cut_id)
BUDGET = {"startup": 5, "workspace": 4, "resume": 3, "clear": 1, "compact": 1}
KINDS = ("startup", "resume", "clear", "compact")
RAIL = ("!", "▸", "✓", "›", "·", "+")
RUNNING_STALE_S = 3 * 3600  # a CI run recorded as running longer ago than this is not said to run
# Claude Desktop may prefix every line of a hook message with "SessionStart:<source> says: ". When the developer's
# eye check confirms it, "short" gives it the headline and the first need only.
DESKTOP_MODE = "full"

SPEAKER = "devkit · "
GUARD_OFF = "The guard is off in this session: restart without it"
OLDER = "This session runs an older devkit: restart it"
EXT_ACK = "Check that the last dev ext ack was yours: dev audit --action ext"
EXT_CHANGED = "Extensions changed outside devkit: dev ext ls"
LAYOUT = "outside devkit's folder layout: dev status explains"
NEW_PROJECT = "No history yet: say what you want to build"
CONTEXT = ("The developer saw this devkit welcome: {lines}. Do not repeat it in your first reply; ask the waiting "
           "decisions first.")

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]")
_TASK_ID = re.compile(r"(?<![\w-])[TFBS]-[a-z0-9]{6,}(?![\w-])")
_MARKS = set("·…▸✓✗›–")
_TRAILING = " ,;:.!?-–·/("


# --------------------------------------------------------------------------- the public entry points


def text(agent: str, kind: str, cwd: str, *, data: dict = None, repo: dict = None, top: str = None,
         reg: dict = None, runs: list = None, roster=None, older: bool = False, ext_changed: bool = False,
         ext_ack: bool = False, mode: str = "full", now: float = None) -> str:
    """The welcome for one session start, "" when there is nothing to say or it cannot be told.

    `kind`: the hook's source (startup, resume, clear, compact; any other counts as startup). `repo`: the project
    as registry.inspect_repo describes it, which context_for already made (None: inspect it here, read-only, without
    pull requests); `top`: the checkout's root ("" outside a repository, None: ask git). `reg`, `runs`, `roster`:
    the registry, devkit's live runs and the crew roster (read when None). `older`, `ext_changed`, `ext_ack`: the
    notices the hook already read (an older devkit copy, extensions changed outside devkit, the last `dev ext ack`
    to confirm), which become need lines. `mode`: "full", or "short" (the headline and the first need)."""
    try:
        return "\n".join(_lines(agent, kind, cwd, data or {}, repo, top, reg, runs, roster,
                                {"older": older, "ext_changed": ext_changed, "ext_ack": ext_ack}, mode,
                                now or time.time()))
    except Exception:
        _log("text")
        return ""


def needs(agent: str, cwd: str, *, data: dict = None, repo: dict = None, runs: list = None, older: bool = False,
          ext_changed: bool = False, ext_ack: bool = False, now: float = None):
    """What needs the developer in this project, as need lines without their rail mark, in the spec's order:
    the guard off, an older devkit copy, decisions (oldest first), extensions accepted, the last `dev ext ack`,
    extensions changed, the folder layout. None when they cannot be read."""
    from . import progress
    data = data or {}
    me = (agent, str(data.get("session_id") or ""))
    runs = _try("runs", progress.active_runs) if runs is None else runs
    if runs is None or not repo:
        return None
    mine = [r for r in runs if isinstance(r, dict) and r.get("project") == repo.get("id")]
    return _try("needs", lambda: _needs(me, mine, {"older": older, "ext_changed": ext_changed, "ext_ack": ext_ack},
                                        layout=[repo]))


def wanted(agent: str, data: dict, top: str) -> bool:
    """Whether this session start gets a welcome: never a headless run (claude -p, the SDK, codex exec, devkit's own
    model calls), a crew member's session (the hook input names its agent, or the hooks set its lane), or a scratch
    repository in a temp folder."""
    from . import progress
    data = data or {}
    if data.get("agent_id") or os.environ.get("DEVKIT_FROM_AGENT"):
        return False
    if top and core.is_temp(top):
        return False
    sess = core.read_json(progress.session_path(agent, str(data.get("session_id") or "-")), None)
    return not progress.headless(sess if isinstance(sess, dict) else {})


def mode_for(surface: str) -> str:
    """The welcome's mode on a surface (canary.surface): "short" on Claude Desktop once DESKTOP_MODE says so."""
    return DESKTOP_MODE if surface == "Claude Desktop" else "full"


def for_model(shown: str) -> str:
    """The paragraph the model reads in its context: the welcome the developer saw, so its first reply does not say
    it again. "" when nothing was shown."""
    lines = [ln.strip() for ln in (shown or "").splitlines() if ln.strip()]
    return CONTEXT.format(lines=" / ".join(lines)) if lines else ""


# --------------------------------------------------------------------------- text: clean, measure and cut


def clean(value) -> str:
    """Text from a record (a decision's question, a run's goal, a status block, a name) as one plain line: no escape
    or control sequence, no backtick, no task id, no emoji or other symbol but the rail marks; letters, digits and
    punctuation of every script stay, so a question reads as it was written."""
    s = _ANSI.sub("", unicodedata.normalize("NFC", str(value or "")))
    s = _TASK_ID.sub("", s.replace("`", ""))
    out = []
    for c in s:
        if c.isspace():
            out.append(" ")
        elif 32 <= ord(c) < 127 or c in _MARKS or unicodedata.category(c)[0] in "LMNP":
            out.append(c)
    return " ".join("".join(out).split())


def width(s: str) -> int:
    """Display columns: East Asian wide and full-width characters take two, combining marks none."""
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def _prefix(s: str, room: int) -> str:
    out, used = [], 0
    for c in s:
        w = width(c)
        if used + w > room:
            break
        out.append(c)
        used += w
    return "".join(out)


def cut(s: str, room: int) -> str:
    """At most `room` columns: at the last word that fits, trailing punctuation dropped, then one "…". Text with
    no spaces between its words (Chinese, Japanese) is cut at a character."""
    if width(s) <= room:
        return s
    head = _prefix(s, max(0, room - 1))
    nxt = s[len(head):len(head) + 1]
    # a head that ends at a space already ends at a word; else drop the word the cut went through
    if not head.endswith(" ") and " " in head.strip() and nxt not in ("", " ") and width(nxt) == 1:
        head = head[:head.rstrip().rfind(" ")]
    return head.rstrip(_TRAILING) + "…"


def cut_name(s: str, room: int) -> str:
    """A name (a project, a branch type) at most `room` columns, cut at its end."""
    return s if width(s) <= room else _prefix(s, max(0, room - 1)) + "…"


def cut_id(name: str, room: int) -> str:
    """A project's name at most `room` columns, its repository name whole: a project id (owner/repo, which names a
    project whose repository name another project shares) loses its owner from the left, as in "…solutions/devkit";
    only a repository name longer than `room` by itself is cut, at its end."""
    if width(name) <= room:
        return name
    repo = name.rsplit("/", 1)[-1]
    if width(repo) > room:
        return cut_name(repo, room)
    tail = _prefix(name[::-1], max(0, room - 1))[::-1]
    return "…" + tail if len(tail) > len(repo) else repo


def cut_middle(s: str, room: int) -> str:
    """A branch at most `room` columns, cut in the middle: the type prefix and the tail stay."""
    if width(s) <= room:
        return s
    head = (room + 1) // 2 + 2
    tail = _prefix(s[::-1], room - 1 - head)[::-1]
    return _prefix(s, head) + "…" + tail


def ago(ts, now: float) -> str:
    """12 min ago, 2 h ago, yesterday, 3 days ago."""
    s = max(0.0, now - float(ts or 0))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    return "yesterday" if s < 2 * 86400 else f"{int(s // 86400)} days ago"


def _left(minutes) -> str:
    if not minutes or minutes <= 0:
        return ""
    if minutes < 60:
        return f"~{max(1, round(minutes))} min left"
    return f"~{round(minutes / 60, 1):g} h left"


def _lower_first(s: str) -> str:
    """"Export cancelled…" after a colon reads "export cancelled…"; an acronym (API) stays."""
    return s[0].lower() + s[1:] if len(s) > 1 and s[0].isupper() and not s[1].isupper() else s


def _count(n: int, one: str) -> str:
    return f"{n} {one if n == 1 else one + 's'}"


# --------------------------------------------------------------------------- reading devkit's records


def _log(what: str):
    try:
        from .hooks import _log as log
        log(f"welcome {what}: {traceback.format_exc()[-300:]}")
    except Exception:
        pass


def _try(what: str, fn, default=None):
    try:
        return fn()
    except Exception:
        _log(what)
        return default


def _lines(agent, kind, cwd, data, repo, top, reg, runs, roster, flags, mode, now) -> list:
    from . import prompt, registry
    me = (agent, str(data.get("session_id") or ""))
    kind = kind if kind in KINDS else "startup"
    if kind == "compact":
        line = _try("compact", lambda: _compact(agent, me[1], now), "")
        return [line] if line else []
    if top is None:
        top = core.gitout(cwd, "rev-parse", "--show-toplevel") or ""
    if repo is None and top:
        main = registry.main_of(Path(top)) or Path(top)
        repo = _try("inspect", lambda: registry.inspect_repo(main, with_prs=False))
        if not repo:
            return []  # the project cannot be told: silence beats a wrong welcome
    reg = _try("registry", lambda: prompt._registry(False), {"projects": []}) if reg is None else reg
    if roster is None:
        from . import crew
        roster = _try("roster", crew.load)
    if repo is None:
        return _workspace(kind, me, reg, runs, roster, flags, mode, now)
    return _project(kind, me, cwd, repo, reg, runs, roster, flags, mode, now)


def _needs(me, runs, flags, layout=(), workspace: dict = None) -> list:
    """The need lines (without the rail mark); raises when a source cannot be read. `workspace`: outside a project,
    {project id: alias}; each project's line then starts with its alias."""
    from . import ext, progress, registry
    out = []
    if os.environ.get("DEVKIT_GUARD", "").lower() == "off":
        out.append(GUARD_OFF)
    if flags.get("older"):
        out.append(OLDER)
    waiting = []
    for r in runs:
        other = (r.get("agent") or "claude", str(r.get("session") or "")) != me
        for d in progress.pending_decisions(r):
            waiting.append((float(d.get("ts") or 0), r, d, other))
    for _, r, d, other in sorted(waiting, key=lambda x: x[0]):
        where = " in another session" if other else ""
        label = f"{_alias(workspace, r.get('project'))} · decision waits{where}: " if workspace is not None \
            else f"Decision waits{where}: "
        q = _lower_first(clean(d.get("question"))) or "a decision for you"
        out.append(label + cut(q, LINE_MAX - 2 - width(label)))
    accepted = len(ext.pending_acceptances())
    if accepted:
        out.append(f"{_count(accepted, 'extension')} accepted since your last check: dev ext ack")
    if flags.get("ext_ack"):
        out.append(EXT_ACK)
    if flags.get("ext_changed"):
        out.append(EXT_CHANGED)
    for p in layout:
        if registry.is_local(p) and not p.get("canonical_ok", True):
            out.append(f"{_alias(workspace, p['id'])} · {LAYOUT}" if workspace is not None
                       else f"This project is {LAYOUT}")
    return out


def _alias(aliases: dict, pid, room: int = None) -> str:
    """A project's short name: its alias, else the repository name. A project id, which names a project whose
    repository name another project shares, is cut to `room` columns (NAME_MAX by default), its repository name
    whole (cut_id)."""
    name = clean((aliases or {}).get(pid) or str(pid or "").rsplit("/", 1)[-1])
    return cut_id(name, NAME_MAX if room is None else room) if "/" in name else name


def _checkout(repo: dict, cwd: str):
    """-> (the worktree cwd is in or None, the checkout: that worktree or the main one)."""
    from . import registry
    _p, wt = registry.project_for_cwd({"projects": [repo]}, cwd)
    co = wt or repo.get("main")
    if not isinstance(co, dict):
        raise ValueError("the checkout is not readable")
    return wt, co


def _working_run(mine: list, now: float):
    from . import registry
    working = [r for r in mine if registry.run_working(r, now)]
    return next((r for r in working if r.get("plan")), working[0] if working else None)


def _run_line(run: dict, crew_now: dict, me, now: float, name: str = "") -> tuple:
    """-> (the run line without its rail mark, its "step N of M" or ""). The goal is cut last: under GOAL_MIN
    columns, the crew count goes, then the time left, then the project's `name` that opens the line outside a
    project (cut_id); the step and "in another session" always stay."""
    from . import progress
    plan = (crew_now or {}).get("plan") or {}
    step = f"step {min(plan['done'] + 1, plan['total'])} of {plan['total']}" if plan.get("total") else ""
    left = _left(plan.get("left_min"))
    busy = sum(1 for m in (crew_now or {}).get("members") or [] if m.get("state", "working") == "working")
    crew = f"{busy} crew working" if busy else ""
    other = (run.get("agent") or "claude", str(run.get("session") or "")) != me and not progress._session_gone(run)
    there = "in another session" if other else ""
    goal = clean(run.get("goal")) or "Work in progress"
    room = LINE_MAX - 2 - (width(name) + 3 if name else 0)
    for drop in ((), ("crew",), ("crew", "left")):
        rest = [x for k, x in (("step", step), ("left", left), ("crew", crew), ("there", there))
                if x and k not in drop]
        fixed = sum(width(" · " + x) for x in rest)
        if width(goal) + fixed <= room or room - fixed >= GOAL_MIN or drop == ("crew", "left"):
            break
    if name and width(goal) + fixed > room and room - fixed < GOAL_MIN:
        name = cut_id(name, max(1, LINE_MAX - 2 - 3 - fixed - min(GOAL_MIN, width(goal))))
        room = LINE_MAX - 2 - width(name) - 3
    return (f"{name} · " if name else "") + " · ".join([cut(goal, max(1, room - fixed))] + rest), step


def _ci(repo: dict, co: dict, now: float) -> tuple:
    """-> (the CI segment in words, the recorded detail), from devkit's records only (prompt.checks_detail and the
    live watches): never the network."""
    from . import ciwatch, prompt
    branch = co.get("branch")
    d = prompt.checks_detail(repo.get("remote") or "", branch) if branch else {}
    st = d.get("state")
    if st == "running":
        if now - float(d.get("at") or d.get("ts") or 0) > RUNNING_STALE_S:
            return "", d
        tops = {core.real(x) for x in (repo.get("path"), co.get("path")) if x}
        watched = any(w.get("status") == "live" and w.get("ref") == branch and core.real(w.get("top") or "") in tops
                      for w in _try("watches", lambda: ciwatch.watches(now), []) or [])
        return "CI running" + (", watched" if watched else ""), d
    if st == "passed":
        return f"CI passed {ago(d.get('at'), now)}", d
    if st == "failed":
        job = clean(d.get("failed"))
        return (f"CI failed: {job}" if job else f"CI failed {ago(d.get('at'), now)}"), d
    return "", d


def _footer(repo: dict, co: dict, now: float) -> str:
    from . import cli
    branch = clean(co.get("branch"))
    head = clean(co.get("head"))
    if not head:
        changes = ["no commits yet"]
    else:
        changes = [cli._changes(co) or "no changes", cli._changes(co, short=True) or "no changes"]
    ci = _try("ci", lambda: _ci(repo, co, now)[0], "")
    room = LINE_MAX - 2
    wheres = [cut_middle(branch, n) for n in (BRANCH_MAX, BRANCH_MIN)] if branch else \
        [f"detached at {head or 'an unknown commit'}"]
    for c in dict.fromkeys((ci, ci.replace(", watched", ""), "")):  # CI goes last: the changes and the branch shorten
        for where in wheres:
            for ch in changes:
                line = " · ".join(x for x in (where, ch, c) if x)
                if width(line) <= room:
                    return line
    return cut(f"{wheres[-1]} · {changes[-1]}", room)


def _next_step(run, last: dict, repo: dict) -> str:
    from . import notes, progress, registry
    if run:
        s = next(iter(progress.ready(run)), None)
        if s:
            return clean(s.get("title") or registry.plain_step(s.get("name") or ""))
    if (last or {}).get("next"):
        return clean(last["next"])
    f = core.note_path(repo["id"])
    if f.exists():
        t = notes.summarize(f.read_text()).get("tasks") or {}
        first = next(iter((t.get("now") or []) + (t.get("next") or [])), "")
        return _lower_first(clean(first))
    return ""


def _update(now: float):
    """-> (the update line, a function that marks today as told), or ("", None)."""
    from . import updatecheck
    doc = updatecheck.read_cache()
    installed = updatecheck.installed_version()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    if not updatecheck.pending(doc, installed) or doc.get("notified") == today:
        return "", None
    return (f"devkit {clean(doc['newest'])} is out: run dev update in a terminal",
            lambda: updatecheck.session_line(installed=installed, today=today))


def _since(run: dict, me, repo: dict, co: dict, roster, now: float) -> str:
    """What happened since the developer left this session (its last turn's end): crew members that finished, and a
    CI result recorded after it."""
    from . import crew, progress
    sess = core.read_json(progress.session_path(me[0], me[1]), None) or {}
    left_at = float(sess.get("turn_ended") or sess.get("last_prompt") or 0)
    if not left_at:
        return ""
    names = [crew.who(lane.get("persona") or lane.get("label") or "", roster)
             for lane in ((run or {}).get("agents") or {}).values()
             if isinstance(lane, dict) and lane.get("state") == "done" and float(lane.get("finished") or 0) > left_at]
    items = []
    if names:
        joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        items.append(f"{joined} finished")
    word, d = _try("ci", lambda: _ci(repo, co, now), ("", {}))
    if d.get("state") in ("passed", "failed") and float(d.get("at") or 0) > left_at:
        job = clean(d.get("failed"))
        items.append("CI passed" if d["state"] == "passed" else f"CI failed: {job}" if job else "CI failed")
    if not items:
        return ""
    line = "Since you left: " + " · ".join(items)
    if names and width(line) > LINE_MAX - 2:
        items[0] = f"{len(names)} crew finished"
        line = "Since you left: " + " · ".join(items)
    return cut(line, LINE_MAX - 2)


def _headline(name: str, verdict: str) -> str:
    room = HEAD_MAX - width(SPEAKER) - width(" · " + verdict)
    return f"{SPEAKER}{cut_id(clean(name) or 'project', room)} · {verdict}"


def _verdict(found, working: bool, new: bool) -> str:
    if found is None:
        return "see dev status"
    if found:
        return "1 thing needs you" if len(found) == 1 else f"{len(found)} things need you"
    return "work in progress" if working else "new project" if new else "nothing needs you"


def _assemble(head: str, found, always: list, optional: list, budget: int, mode: str) -> list:
    """The headline, the first need, the lines that always stay, then the optional ones in priority order while the
    budget holds; laid out in the rail's fixed order. A third need and more fold into "And N more"."""
    found = found or []
    if mode == "short":
        return [head] + (["! " + cut(found[0], LINE_MAX - 2)] if found else [])
    second = []
    if len(found) == 2:
        second = [("!", found[1])]
    elif len(found) > 2:
        second = [("!", f"And {len(found) - 1} more: dev status lists them")]
    keep = ([("!", found[0])] if found else []) + [x for x in always if x[1]]
    for mark, line in second + [x for x in optional if x[1]]:
        if 1 + len(keep) < budget:
            keep.append((mark, line))
    keep.sort(key=lambda x: RAIL.index(x[0]))  # stable: the first need stays before the second
    return [head] + [f"{mark} {cut(line, LINE_MAX - 2)}" for mark, line in keep]


def _project(kind, me, cwd, repo, reg, runs, roster, flags, mode, now) -> list:
    from . import cli, progress, registry
    projects = list((reg or {}).get("projects") or [])
    if not any(isinstance(p, dict) and p.get("id") == repo.get("id") for p in projects):
        projects.append(repo)
    name = _try("alias", lambda: registry.alias_map(projects).get(repo["id"])) or repo.get("name") or \
        str(repo.get("id") or "").rsplit("/", 1)[-1]
    runs = _try("runs", progress.active_runs) if runs is None else runs
    mine = [r for r in runs or [] if isinstance(r, dict) and r.get("project") == repo.get("id")]
    found = _try("needs", lambda: _needs(me, mine, flags, layout=[repo])) if runs is not None else None
    wt_co = _try("checkout", lambda: _checkout(repo, cwd))
    wt, co = wt_co if wt_co else (None, None)
    view = _try("status", lambda: cli.status_view({"projects": projects, "issues": []}, repo, wt, runs=mine,
                                                  ci={"runs": []}, roster=roster, now=now, caller=me)) \
        if co and runs is not None else None
    run = _try("run", lambda: _working_run(mine, now)) if view else None
    run_line, step = _try("run line", lambda: _run_line(run, view["crew"], me, now), ("", "")) if run else ("", "")
    new = bool(co) and not clean(co.get("head"))
    verdict = _verdict(found, bool(run_line), new)
    if kind == "clear":
        if found:
            return [_headline(name, verdict)]
        if found is None or not run_line:
            return []
        return [_headline(name, f"run open, {step}" if step else "run open")]
    head = _headline(name, verdict)
    if kind == "resume":
        since = _try("since", lambda: _since(run, me, repo, co, roster, now), "") if co else ""
        return _assemble(head, found, [("▸", since)], [], BUDGET["resume"], mode)
    last = (view or {}).get("last_update") or {}
    last_line = ""
    if last.get("done") and last.get("ts"):
        when = ago(last["ts"], now)
        last_line = f"Last: {cut(clean(last['done']), LINE_MAX - 2 - width('Last: ') - width(' · ' + when))} · {when}"
    nxt = _lower_first(_try("next", lambda: _next_step(run, last, repo), "")) if not new else ""
    nxt = NEW_PROJECT if new else f"Next: {nxt}" if nxt else ""
    footer = _try("footer", lambda: _footer(repo, co, now), "") if co else ""
    update, mark = _try("update", lambda: _update(now), ("", None))
    lines = _assemble(head, found, [("▸", run_line), ("·", footer)],
                      [("✓", last_line), ("›", nxt), ("+", update)],
                      BUDGET["startup"], mode)
    if mark and lines[-1].startswith("+ "):
        _try("update mark", mark)
    return lines


def _workspace(kind, me, reg, runs, roster, flags, mode, now) -> list:
    """Outside a project: the same grammar, with the project as a segment of each line."""
    from . import cli, progress, registry
    projects = [p for p in (reg or {}).get("projects") or [] if isinstance(p, dict) and p.get("id")]
    here = _try("local", lambda: registry.split_local(projects)[0], []) or []
    alias = _try("alias", lambda: registry.alias_map(projects), {}) or {}
    runs = _try("runs", progress.active_runs) if runs is None else runs
    found = _try("needs", lambda: _needs(me, [r for r in runs if isinstance(r, dict)], flags, layout=here,
                                         workspace=alias)) if runs is not None else None
    run = _try("run", lambda: _working_run([r for r in runs or [] if isinstance(r, dict)], now))
    run_line = ""
    if run:
        run_line = _try("run line", lambda: _run_line(run, cli._crew_now([run], roster, now), me, now,
                                                      name=_alias(alias, run.get("project"), LINE_MAX))[0], "")
    head = _headline("workspace", _verdict(found, bool(run_line), False))
    if kind == "clear":
        return [head] if found else []
    if kind == "resume":
        return _assemble(head, found, [], [], BUDGET["resume"], mode)
    recent = sorted(here, key=lambda p: -float(p.get("last_activity") or 0))[:3]
    items = [f"{_alias(alias, p['id'])} {ago(p.get('last_activity'), now)}" for p in recent
             if p.get("last_activity")]
    while items and width("Recent: " + " · ".join(items)) > LINE_MAX - 2:
        items.pop()
    update, mark = _try("update", lambda: _update(now), ("", None))
    lines = _assemble(head, found, [("▸", run_line), ("·", "Recent: " + " · ".join(items) if items else "")],
                      [("+", update)], BUDGET["workspace"], mode)
    if mark and lines[-1].startswith("+ "):
        _try("update mark", mark)
    return lines


def _compact(agent: str, sid: str, now: float) -> str:
    """After a compaction: what continuity carried into the new context (its brief's requests, run and decisions),
    in one line; "" when it carried nothing."""
    from . import continuity, progress
    rec = continuity.read(agent, sid) or {}
    cp = rec.get("checkpoint") or {}
    prompts = continuity._fresh(cp.get("prompts"), now)
    run = continuity._live_run(agent, sid)
    decisions = len(progress.pending_decisions(run)) if run else len(cp.get("decisions") or [])
    parts = [_count(len(prompts), "request") if prompts else "", "the run" if run or cp.get("run") else "",
             _count(decisions, "decision") if decisions else ""]
    parts = [x for x in parts if x]
    if not parts:
        return ""
    line = f"{SPEAKER}kept " + ", ".join(parts)
    return line if width(line) <= HEAD_MAX else line.replace("the run", "run")
