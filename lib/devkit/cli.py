# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev` command line: argument parsing and command implementations."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import core, gc, registry, ui
from .core import CFG, DEV, HANDOFF, LOCAL, STATE, WT_ROOT, ago, human_kb, tilde

COPYRIGHT = core.COPYRIGHT

COMMANDS = [
    ("Projects", [
        ("ls [--all]", "The projects on this Mac: branch, your changes, checks, worktrees, activity"),
        ("go <name> [branch]", "Print a project's path, or its worktree's (shell: `dcd <name>` jumps there)"),
        ("status [name]", "What needs you, checks, the crew and the last update of a project"),
        ("alias [<alias> [project]|rm]", "Short project names shared by the team; dev status, go, open, wt take them"),
        ("clone <owner/repo|url>", "Clone by host/owner/repo (GitHub, GitLab, Bitbucket, any git URL)"),
        ("new <name> [--github]", "Start a project in _local/<name>, or create it on GitHub"),
        ("adopt <path>", "Move a misplaced repo into the canonical layout (fixes worktrees + agent history)"),
        ("open [query]", "Open a project in your editor"),
    ]),
    ("Portfolio", [
        ("dashboard", "Open the live command centre: projects, tasks, background work, team, health"),
        ("overview [--json]", "Portfolio digest for you and your agents: status, Now tasks, last sessions"),
        ("standup [--days N]", "What happened in the last N days and what is in progress"),
        ("initiative [new|add|ls|show]", "Cross-project initiatives: member projects, workstreams, progress"),
        ("task [add|start|done|ls]", "Tasks (Now / Next / Later / Done) of a project, or of an initiative with -i"),
        ("note set k=v …", "Project status, priority, milestone, due date, tags"),
        ("plan new|revise|approve|ls", "Versioned plans and specs; every change records who and why"),
        ("team", "Who is working on what, on every machine; who holds which task"),
        ("progress [start|step|done]", "Live progress of agent work with ETA; `dev time` for how long things take"),
        ("files [path]", "Compact, gitignore-aware tree with git status and sizes (token-light)"),
        ("find <pattern> · grep <regex>", "Find files and search code with capped, compact output"),
        ("diff [args]", "Token-light git diff: stat first, short hunks, lock and generated files folded"),
        ("rm · mv · cp", "Safe file operations: Trash with undo, git-aware, refuse in-use or unsaved work"),
        ("delivery [apply]", "Versioning, release, test and deploy policy of a project: detect, recommend, record, gaps"),
        ("time [--days 7]", "How long things take: turns, slow commands, subagents, estimate accuracy"),
        ("handoff-import <dir> --slug s", "Adopt an old handoff repository as an initiative (from a staging folder)"),
        ("lesson add|ls|scan|review", "Learn from mistakes: lessons recalled in every session, repeats automated"),
        ("jobs [show|run|set|enable|disable|journal]",
         "Scheduled jobs (sync, sleep, gc, repair, audit, update-check, doctor, lessons)"),
        ("audit [--days 7] [--team]", "The audit trail: every change, decision and job run, per machine and team"),
        ("sleep [--dry-run]", "Consolidate and forget (daily): unused lessons fade, duplicates merge, old records go"),
        ("report [--days 7]", "Status report for initiatives and projects (Markdown, for the team or management)"),
    ]),
    ("Agents", [
        ("claude [query] [-w branch]", "Start Claude Code in a project (optionally in its own worktree)"),
        ("codex [query] [-w branch]", "Start Codex in a project (optionally in its own worktree)"),
        ("impeccable <verb> [args…]", "Run the impeccable design engine devkit installed (its digest checked, no shell)"),
        ("handoff [query] [--edit|--log …]", "Show, edit or append to the project's handoff note"),
        ("session brief|carry", "What devkit kept through compaction, /clear and resume; carry moves the run here"),
    ]),
    ("Worktrees", [
        ("wt new <branch>", "Create a managed worktree in ~/Development/.worktrees/<owner>/<repo>/<branch>"),
        ("wt ls [--all]", "List worktrees with PR state, live sessions and size"),
        ("wt rm <branch|path>", "Remove a worktree safely (refuses if work would be lost)"),
    ]),
    ("Delivery", [
        ("ci [--commands] · audit · init",
         "What CI runs (run it locally first); `ci audit [--fix]` diagnoses pipelines on any host; `ci init` sets one up"),
        ("branch new|rename|ls|clean", "Branches from an up-to-date main, with valid names; where each one lives "
                                       "(ls); tidy merged ones"),
        ("commit · push · rebase", "Conventional commits, push after local CI evidence (dev ci --run), rebase on main"),
        ("pr create|ls|checks|merge", "PRs with the template, checks with failing logs, safe merges"),
        ("release [--dry-run]", "Tag and publish by the delivery policy, with the CHANGELOG section"),
        ("run ls|view --failed|rerun", "CI runs, failing logs trimmed; rerun only with --flaky \"reason\""),
        ("issue ls|view|create", "Issues, compact"),
        ("git … · gh …", "Escape hatches to git and gh, with the same policy checks"),
        ("stubs [--staged|--all]", "Unfinished work: stubs, placeholders, TODOs without a task (also checked on commit)"),
        ("test [run|plan|prove|audit]", "Tests: last failures first, then what your changes can affect; prove and "
                                        "audit check that tests can fail"),
        ("crew [show|brief|route|delegate|next|owner|ask|mode|decide|graph|rules|raci]",
         "The specialist agents: who does what, briefs, routing, delegation, who holds a file, decisions, the live "
         "graph, the rules, the RACI"),
        ("ci status [--watch]", "Live CI/CD runs (GitHub, GitLab, Bitbucket, Azure): jobs, steps, elapsed, ETA"),
        ("watch [--wait] · watches", "One wait for every devkit event (questions, decisions, ready steps, members, "
                                     "CI): it wakes an idle session once per event; watches lists the waits"),
        ("calver [--next]", "Current and next CalVer version YY.MM.PATCH from the repository's tags"),
        ("sec scan [--history]", "Security: secrets (gitleaks), dependencies (osv-scanner), SAST (Semgrep), Trivy"),
        ("docs check [--online]", "Check docs: Markdown structure, links and anchors, prose (Vale), diagrams"),
        ("diagram render|check|new", "Branded, validated SVG diagrams (flow, C4, state, sequence, ER, timeline)"),
    ]),
    ("Tracking and disk", [
        ("canary", "How often Claude/Codex replies carried the “[Firefly> DevKit]” instruction marker"),
        ("sync", "Refresh the projects map and handoff notes, commit and push the handoff repo"),
        ("backup [<branch>…] [--all]", "Copy branches with commits only on this Mac to the remote (no CI run)"),
        ("du [<name>] [--plan|--refresh]", "What fills your disk, why, and the plan to free it safely"),
        ("free [<n>…|--safe]", "Free items of the dev du plan: asks first, refuses work not committed or pushed"),
        ("gc [--apply]", "The same as dev free; --apply runs the unattended cleanup of the nightly gc job"),
    ]),
    ("Environment", [
        ("setup [--yes]", "The installation and configuration wizard (first install, or to change choices)"),
        ("setup shell", "The zsh helpers: fzf pickers for tasks, projects and branches, and their keys"),
        ("completion zsh|bash", "Tab completion for dev, generated from its own commands and options"),
        ("doctor", "Verify the whole setup (git, gh, Claude, Codex, skills, Docker, automation)"),
        ("agents [test [claude|codex]]", "Claude Code and Codex: installed, signed in, plan, quota; live self-tests"),
        ("config [--check]|repair|lock|…", "The agent settings devkit manages: what differs, why, the fix"),
        ("guard check <cmd>|--write p", "Simulate what the guard decides for a command or a file write"),
        ("plugin validate", "Validate the devkit plugin with Claude Code's validator and Codex's parser"),
        ("ext ls|outdated|update|add|remove|enable|disable|ack",
         "Third-party plugins, skills and MCP servers: pins, updates, drift, your own set"),
        ("desktop pack|status", "Claude Desktop extension: build the .mcpb bundle, check what is installed"),
        ("install [--only step]", "Pin the checkout read-only for the hooks, then apply the configuration"),
        ("update [--check]", "Move devkit to the newest release (or pull its branch), pin it and re-apply it; "
                             "--check only tells what is new"),
        ("docker up|down|status|trim|prune", "Run Docker on a Colima VM; trim returns its free space to macOS"),
        ("version", "Show the devkit version"),
    ]),
]


# --------------------------------------------------------------------------- helpers

def run(args) -> int:
    """Run a program in the foreground with an argument list (never a shell); returns its exit code."""
    import subprocess
    try:
        return subprocess.call([str(x) for x in args])
    except OSError as e:
        ui.err(f"{args[0]}: {e}")
        return 127


def _reg(prs=True, refresh=False, sizes=False):
    return registry.build(with_prs=prs, refresh_prs=refresh, with_sizes=sizes)


_ARGV = []  # the arguments of this dev command (main sets them), so a tie can print the command for each match


def _resolve(reg, query, cwd=None, note=False, here=False):
    """Project from a query (an id, alias, path or part of a name), or from cwd when no query. A tie lists every
    match with the command for it and exits 2; devkit never guesses (decision (a)). `note`: when the match was not
    exact, say on stderr which project was picked (`kit → kit-consulting`). `here`: the project must be on this Mac."""
    if not query:
        p, w = registry.project_for_cwd(reg, cwd or os.getcwd())
        if not p:
            ui.err(f"not inside a project ({tilde(os.getcwd())}); pass a project name or alias (dev ls lists them)")
            sys.exit(2)
        return p
    m = registry.match(reg, query)
    found = m["projects"]
    amap = registry.alias_map(reg["projects"])
    if not found:
        ui.err(f"no project matches “{query}”: dev ls lists the projects on this Mac, dev ls --all the portfolio")
        sys.exit(2)
    if len(found) > 1:
        ui.err(f"“{query}” matches {len(found)} projects. Pick one:")
        argv = _ARGV or ["status", query]
        for p in found:
            alias = amap.get(p["id"], p["id"])
            where = "on this Mac" if registry.is_local(p) else "not on this Mac"
            print(f"    {ui.bold(alias)}  {ui.dim(p['id'] + ' · ' + where)}", file=sys.stderr)
            print(ui.command("dev " + " ".join(alias if x == query else x for x in argv), 6), file=sys.stderr)
        sys.exit(2)
    p = found[0]
    if here and not registry.is_local(p):
        ui.err(f"{amap.get(p['id'], p['id'])} is not on this Mac. Get it first:")
        print(ui.command(f"dev clone {p['id']}", 4), file=sys.stderr)
        sys.exit(2)
    if note and not m["exact"]:
        print(ui.dim(f"{query} → {amap.get(p['id'], p['id'])}"), file=sys.stderr)
    return p


def _state(co):
    s = registry._state(co)
    return ui.green(s) if s == "clean" else ui.yellow(s)


def commit_push(push=True, quiet=False):
    """Commit what changed in the handoff repository and push it. A rebase left stopped is aborted first, a pull
    settles conflicts in items and stops cleanly on any other (work.pull_rebase), and every failure is recorded in
    handoff-push-failed.json, which `dev status` shows until a push succeeds."""
    from . import audit, work
    root = work.HANDOFF  # the repository work.pull_rebase works on, read now (tests point it elsewhere)
    if not (root / ".git").exists():
        return
    failed = work.push_failed_file()
    with work._repo_lock() as got:
        if not got:
            if not quiet:
                ui.warn("handoff sync skipped: another devkit process is using the handoff repository")
            return
        work.recover_rebase("dev sync")
        core.git(root, "add", "-A")
        rc, _, _ = core.git(root, "diff", "--cached", "--quiet")
        if rc != 0:
            n = len(core.gitout(root, "diff", "--cached", "--name-only").splitlines())
            core.git(root, "commit", "-q", "-m", f"sync: {n} file(s) from {os.uname().nodename.split('.')[0]}")
        if not push or not core.gitout(root, "remote"):
            return
        core.git(root, "fetch", "-q", "origin", timeout=60)
        ok, err = True, ""
        if core.gitout(root, "rev-list", "--count", "HEAD..@{u}") not in ("", "0"):
            ok, err = work.pull_rebase()
        ahead = core.gitout(root, "rev-list", "--count", "@{u}..HEAD")
        if ok and ahead not in ("", "0"):
            rc, _, err = core.git(root, "push", "-q", timeout=120)
            if rc != 0:  # someone pushed in between: rebase on it and try once more
                ok, err = work.pull_rebase()
                rc, _, err = core.git(root, "push", "-q", timeout=120) if ok else (1, "", err)
            ok = rc == 0
            if ok:
                audit.record("handoff.push", core.handoff_repo(), f"{ahead} commit(s)")
                if failed.exists():
                    failed.unlink()
            else:
                audit.record("handoff.push", core.handoff_repo(), err[:300], "failed")
                failed.write_text(json.dumps({"ts": int(time.time()), "error": err[:300]}))
                try:  # each live session hears of it once, with its next wake or hook
                    from . import watch
                    watch.notice_all("notice", f"push:{int(time.time())}",
                                     f"the handoff push failed at {time.strftime('%H:%M')}: "
                                     f"{' '.join(err.split())[:200] or 'no reason given'}. dev status shows it; "
                                     "the next dev sync tries again")
                except Exception:
                    pass
        if not ok and not quiet:
            ui.warn(f"handoff push failed: {err}")


# --------------------------------------------------------------------------- dashboard & help

def print_help():
    print(ui.banner("help"))
    for group, cmds in COMMANDS:
        print(ui.section(group))
        for c, desc in cmds:
            print(f"  {ui.cyan('dev ' + c):<44} {desc}" if not ui.COLOR else f"  {ui.cyan(('dev ' + c).ljust(34))} {desc}")
    print(ui.section("Shell"))
    print(f"  {ui.cyan('dcd <query>'.ljust(34))} cd into a project   ·   {ui.cyan('dwt <branch>')} create a worktree and cd into it")
    print(f"\n  {ui.dim('Docs: ' + tilde(core.DEVKIT_HOME / 'docs') + ' · dev <command> --help for options')}")
    print(f"  {ui.dim(COPYRIGHT)}\n")


def _attention(issues) -> list:
    """Registry issues as (headline, command, detail), similar ones grouped, commands split out of the text."""
    import re as _re
    out, stray = [], {}
    for kind, msg in issues:
        m = _re.match(r"(.+?): worktree outside the managed locations: (.+)$", msg)
        if m:
            stray.setdefault(m.group(1), []).append(m.group(2))
            continue
        cmd = ""
        cm = _re.search(r"→\s*`([^`]+)`", msg)
        if cm:
            cmd, msg = cm.group(1), msg[:cm.start()].rstrip(" ;,→")
        lm = _re.match(r"(\S+) lives at (\S+); canonical is (\S+)", msg)
        if lm:
            msg = f"{lm.group(1).split('/')[-1]} is outside the workspace layout (should be in {lm.group(3)})"
        out.append((msg, cmd, ""))
    for pid, paths in stray.items():
        names = ", ".join(Path(p).name for p in paths)
        out.append((f"{len(paths)} worktree(s) of {pid.split('/')[-1]} outside the managed folder: {names}",
                    "", "dev adopt moves them with the repository; dev wt ls shows them"))
    return out


def _wrap(text: str, indent: int, width_: int) -> list:
    import textwrap
    return textwrap.wrap(text, max(30, width_ - indent), break_on_hyphens=False, break_long_words=False) or [""]


def cmd_home(a):
    print(ui.banner(tilde(DEV)))
    W = min(ui.width(), 110)
    print(f"  {ui.bold(ui.greeting(core.gh_user().get('name', '')))}")
    reg = _reg(prs=True)
    ps = reg["projects"]
    p, w = registry.project_for_cwd(reg, os.getcwd())
    if p:
        co = w or p["main"]
        print(ui.section(p["id"].split("/")[-1] + ui.dim(f"  {p['id']}") if "/" in p["id"] else p["id"]))
        print(f"  {ui.cyan(co.get('branch') or '-')}  {_state(co)}")
        sl = p.get("status_line") or registry.status_line(p["id"])
        if sl and sl.get("line"):
            from . import canary
            segs = canary.segments(sl["line"])
            done = next((x for x in segs[2:] if x[:1] not in "✓⚠✗–▸" and not x.lower().startswith("next")), "")
            nxt = next((x[len("next →"):].strip() for x in segs if x.lower().startswith("next")), "")
            if done:
                who = f"({sl.get('agent', '')}, {ago(sl.get('ts'))})"
                lines = _wrap(f"{done} {who}", 8, W)
                lines[-1] = lines[-1][:-len(who)] + ui.dim(who) if lines[-1].endswith(who) else lines[-1]
                print(f"  {ui.dim('last')}  " + "\n        ".join(lines))
            if nxt:
                print(f"  {ui.dim('next')}  " + "\n        ".join(_wrap(nxt, 8, W)))
    else:
        print(f"  {ui.dim('Not inside a project. `dev ls` lists them; `dcd <name>` jumps to one.')}")
    n_wt = sum(len(x["worktrees"]) for x in ps)
    live = sum(1 for x in ps for y in x["worktrees"] if y.get("active")) + sum(1 for x in ps if x["main"].get("active"))
    last = core.read_json(STATE / "last-sync.json", {}) or {}
    from . import canary
    print(ui.section("Workspace"))
    print(f"  {len(ps)} projects · {n_wt} worktrees · {live} live sessions · synced {ago(last.get('ts'))}")
    good, scored = canary.format_rate(7)
    sm = canary.summary(7)
    if sm:
        ok_, tot = sum(o for o, _ in sm.values()), sum(t for _, t in sm.values())
        print(ui.dim(f"  agents followed the rules in {100 * ok_ // tot}% of replies this week"
                     + (f", {100 * good // scored}% in the full format" if scored else "")))
    items = _attention(reg["issues"])
    if items:
        print(ui.section(f"Needs attention · {len(items)}"))
        for head, cmd, detail in items[:5]:
            lines = _wrap(head, 5, W)
            print(f"  {ui.yellow('!')}  " + "\n     ".join(lines))
            if cmd:
                print(f"     {ui.cyan(cmd)}")
            if detail:
                print(f"     {ui.dim(detail)}")
        if len(items) > 5:
            print(f"  {ui.dim(f'+{len(items) - 5} more: dev dashboard')}")
    print(f"\n  {ui.dim('dev status · dev ls · dev dashboard · dev help')}\n")


# --------------------------------------------------------------------------- projects

def _changes(co: dict, short: bool = False) -> str:
    """A checkout's state in plain words: "78 commits to push, 1 new file not added" (short: "78 to push, 1 new file")."""
    if not co:
        return ""
    bits = []
    push = co.get("ahead") or co.get("unpushed") or 0
    if co.get("modified"):
        n = co["modified"]
        bits.append(f"{n} changed" if short else f"{n} changed file{'s' if n != 1 else ''}")
    if push:
        bits.append(f"{push} to push" if short else f"{push} commit{'s' if push != 1 else ''} to push")
    if co.get("behind"):
        n = co["behind"]
        bits.append(f"{n} to pull" if short else f"{n} commit{'s' if n != 1 else ''} to pull")
    if co.get("untracked"):
        n = co["untracked"]
        bits.append(f"{n} new file{'s' if n != 1 else ''}" + ("" if short else " not added"))
    return ", ".join(bits)


def _working_here(p: dict, sessions=()) -> bool:
    """A session works in this project now: a live agent session, or a process inside one of its folders."""
    if any(s.get("project") == p["id"] and s.get("active") for s in sessions or []):
        return True
    return bool((p.get("main") or {}).get("active") or any(w.get("active") for w in p.get("worktrees") or []))


def _checks_cell(p: dict, offline: bool):
    """(glyph, word) for the CHECKS column: the open PR's checks, else the branch's latest CI run (cached for 15 min)."""
    co = p.get("main") or {}
    pr = next((x for x in p.get("prs_open") or [] if x.get("branch") == co.get("branch")), None)
    state = {"passing": "success", "failing": "failed", "pending": "running"}.get((pr or {}).get("checks"))
    if not state and not offline and p.get("path") and co.get("branch"):
        try:
            from . import cistatus
            runs = cistatus.runs_for(p["path"], co.get("branch"), limit=1, detail=True, max_age=900).get("runs")
            state = runs[0]["status"] if runs else None
        except Exception:
            state = None
    return {"success": (ui.glyph("ok"), "passed"), "failed": (ui.glyph("fail"), "failed"),
            "running": (ui.glyph("running"), "running"), "queued": (ui.glyph("running"), "queued")}.get(
        state, (ui.glyph("none"), ""))


def _ls_rows(reg: dict, offline: bool = True) -> list:
    amap = registry.alias_map(reg["projects"])
    here, _ = registry.split_local(reg["projects"])
    rows = []
    for p in here:
        co = p.get("main") or {}
        g, word = _checks_cell(p, offline)
        size = (p.get("size_kb") or 0) + sum(w.get("size_kb") or 0 for w in p.get("worktrees") or [])
        rows.append({"alias": amap.get(p["id"], p["id"]), "id": p["id"], "name": p.get("name") or "",
                     "working": _working_here(p, reg.get("sessions")), "layout": not p.get("canonical_ok", True),
                     "branch": co.get("branch") or "not on a branch", "changes": _changes(co, short=True),
                     "checks": g, "checks_word": word, "worktrees": len(p.get("worktrees") or []),
                     "last": p.get("last_activity"), "size_kb": size if p.get("size_kb") is not None else None,
                     "path": p.get("path")})
    rows.sort(key=lambda r: -(r["last"] or 0))
    return rows


def _project_table(rows: list, cols: int) -> list:
    """The projects on this Mac, one row each (Sati's `dev ls`): the alias you type, the branch, your changes,
    checks, worktrees and last activity; at 100 columns and wider also the size and the repository name."""
    wide = cols >= 100
    W = ui.text_width(cols) if not wide else min(cols, 120)
    heads = ["NAME", "BRANCH", "YOUR CHANGES", "CHECKS", "WORKTREES" if wide else "WT", "LAST"] + (
        ["SIZE"] if wide else [])
    cells = []
    for r in rows:
        name = r["alias"] + (f" ({r['name']})" if wide and r["name"] and r["name"].lower() != r["alias"] else "")
        cells.append([name + (" !" if r["layout"] else ""), r["branch"], r["changes"] or ui.glyph("none"),
                      r["checks"] + (" " + r["checks_word"] if wide and r["checks_word"] else ""), str(r["worktrees"]),
                      ui.rel_time(r["last"], short=not wide) if not r["working"] else "now"]
                     + ([ui.size_text(r["size_kb"])] if wide else []))
    widths = [max([len(h)] + [ui.visible_len(c[i]) for c in cells]) for i, h in enumerate(heads)]
    room = W - 5 - 2 * (len(heads) - 1)
    for i, floor in ((2, 18), (1, 12), (0, 12)):  # your changes, then the branch, then the name give way
        over = sum(widths) - room
        if over > 0:
            widths[i] = max(floor, widths[i] - over)
    right = {4} | ({6} if wide else set())

    def fmt(vals, style=None):
        out = []
        for i, v in enumerate(vals):
            v = ui.fit_cell(v, widths[i])
            pad = " " * (widths[i] - ui.visible_len(v))
            out.append(pad + v if i in right else v + pad)
        return "  ".join(out).rstrip()
    lines = ["     " + ui.dim(fmt(heads))]
    for r, c in zip(rows, cells):
        mark = ui.green(ui.glyph("session")) if r["working"] else " "
        text = fmt(c)
        if r["layout"]:
            text = text.replace(" !", " " + ui.amber("!"), 1)
        lines.append(f"  {mark}  {text}")
    return lines


def _ls_legend(rows: list, cols: int) -> list:
    bits = []
    if any(r["working"] for r in rows):
        bits.append(f"{ui.green(ui.glyph('session'))} a session is working in it now")
    lay = [r for r in rows if r["layout"]]
    if lay:
        bits.append(f"{ui.amber('!')} outside devkit's folder layout" +
                    (f": dev adopt {tilde(lay[0]['path'])}" if cols >= 100 and len(lay) == 1 else ""))
    return ["  " + "    ".join(bits)] if bits else []


def _empty_mac(n_portfolio: int, cols: int) -> list:
    lists = f" (dev ls --all lists {n_portfolio})" if n_portfolio else ""
    return (ui.para(f"No projects on this Mac yet. Get one from your portfolio{lists}:", cols)
            + [ui.command("dev clone <name>", 4)] + ui.para("Start a new one:", cols)
            + [ui.command("dev new <name>", 4)])


def _portfolio_footer(n: int) -> str:
    if not n:
        return ""
    return (f"  {n} more project{'s' if n != 1 else ''} in your portfolio "
            f"{'are' if n != 1 else 'is'} not on this Mac: {ui.cyan('dev ls --all')}")


def cmd_ls(a):
    reg = _reg(prs=not a.offline)
    cols = ui.width()
    here, away = registry.split_local(reg["projects"])
    if a.names:
        print("\n".join(p["id"] for p in (reg["projects"] if a.all else here)))
        return 0
    rows = _ls_rows(reg, offline=a.offline)
    if getattr(a, "json", False):
        print(json.dumps({"local": rows, "portfolio": [{"id": p["id"], "alias": registry.alias_map(
            reg["projects"]).get(p["id"])} for p in away]}, indent=2, ensure_ascii=False))
        return 0
    print()
    if not rows:
        print("\n".join(_empty_mac(len(away), cols)))
    else:
        print(ui.rule_title(f"Projects on this Mac · {len(rows)}", cols))
        print("\n".join(_project_table(rows, cols)))
        print()
        print("\n".join(_ls_legend(rows, cols)))
    if a.all and away:
        print()
        print(ui.rule_title(f"In your portfolio, not on this Mac · {len(away)}", cols))
        amap = registry.alias_map(reg["projects"])
        by_owner = {}
        for p in sorted(away, key=lambda x: x["id"]):
            by_owner.setdefault(p.get("owner") or "", []).append(amap.get(p["id"], p["id"]))
        ncol = 3 if cols >= 100 else 2
        cw = (ui.text_width(cols) - 4) // ncol
        for owner, names in by_owner.items():
            print(f"  {ui.bold(owner or 'other')} {ui.dim('· ' + str(len(names)))}")
            per = -(-len(names) // ncol)
            for i in range(per):
                cells = [names[i + k * per] for k in range(ncol) if i + k * per < len(names)]
                print("    " + "".join(ui.fit_cell(c, cw - 2).ljust(cw) for c in cells).rstrip())
        print()
        example = amap.get(away[0]["id"], away[0]["id"])
        print(f"  Get one on this Mac, for example {example}:")
        print(ui.command(f"dev clone {example}", 4))
    elif away:
        print(_portfolio_footer(len(away)))
    print()
    return 0


def cmd_alias(a):
    """dev alias · dev alias <alias> [project] [--move] · dev alias rm <alias>"""
    reg = _reg(prs=False)
    ps = reg["projects"]
    if a.alias == "rm":
        if not a.project:
            ui.err("name the alias to remove: dev alias rm <alias>")
            return 2
        try:
            pid = registry.remove_alias(a.project)
        except registry.AliasError as e:
            ui.err(str(e))
            return 2
        now_ = registry.alias_map(ps).get(pid, pid)
        _prompt_refresh(reg)
        print(f"{ui.OK} removed {a.project}: {pid} is {ui.bold(now_)} again")
        return 0
    if a.alias:
        p = _resolve(reg, a.project) if a.project else _resolve(reg, None)
        try:
            prev = registry.set_alias(a.alias.lower(), p["id"], ps, move=a.move)
        except registry.AliasError as e:
            ui.err(str(e))
            return 2
        moved = f" (moved from {prev.rsplit('/', 1)[-1]})" if prev else ""
        _prompt_refresh(reg)
        print(f"{ui.OK} {ui.bold(a.alias.lower())} is now the alias of {p['id']}{moved}. Try it:")
        print(ui.command(f"dev status {a.alias.lower()}", 4))
        return 0
    explicit = registry.load_aliases()
    amap = registry.alias_map(ps, explicit)
    by_pid = {rec["project"]: (al, rec) for al, rec in explicit.items()}
    here, _ = registry.split_local(ps)
    show = here + [p for p in ps if p["id"] in by_pid and p not in here]
    rows = []
    for p in show:
        al, rec = by_pid.get(p["id"], (amap.get(p["id"], p["id"]), None))
        who = f"{rec.get('by') or 'someone'}, {rec.get('at') or ''}".rstrip(", ") if rec else ui.dim("repository name")
        rows.append([ui.bold(al), p["id"], who])
    print()
    if rows:
        print(ui.table(rows, ["ALIAS", "PROJECT", "SET BY"]))
    else:
        print("  No projects on this Mac yet: dev ls --all lists the portfolio.")
    print(f"\n  {ui.dim('Set one: dev alias <alias> [project] · remove it: dev alias rm <alias>')}\n")
    return 0


# --------------------------------------------------------------------------- the prompt segment (R9)

def _prompt_refresh(reg, cwd=None):
    """Commands that just read the facts give them to the prompt's cache too (never fails the command)."""
    from . import prompt
    prompt.safe_refresh(cwd, reg=reg, force=True)


def cmd_prompt(a):
    """dev prompt: what the prompt shows here and how it reaches the prompt · dev prompt refresh: write the cache for
    every project on this Mac."""
    from . import prompt
    if a.action == "refresh":
        done = prompt.refresh(reg=_reg(prs=False))
        print(f"{ui.OK} the prompt knows {len(done)} project{'s' if len(done) != 1 else ''} on this Mac")
        return 0
    m = prompt.mode()
    seg, ask = prompt.show(os.getcwd())
    print()
    if not m or m == "off":
        print(f"  {ui.glyph('none')} The prompt helper is off. Add it:")
        print(ui.command("dev setup prompt", 4))
    elif seg:
        print(f"  {seg}" + (f"  {ask}" if ask else ""))
        print(f"  {ui.dim(f'Shown through {m}. Take it out: dev setup prompt --remove')}")
    else:
        print(f"  {ui.glyph('none')} Nothing here: this folder is not in a devkit project, or the prompt has not "
              "seen it yet (dev prompt refresh).")
    print()
    return 0


# --------------------------------------------------------------------------- dev status (Sati's design, R3)

def _ci_for(path, branch):
    try:
        from . import cistatus
        return cistatus.runs_for(path, branch, limit=1, detail=True, max_age=60)
    except Exception:
        return {"runs": [], "error": "unavailable"}


def _has_ci_files(path) -> bool:
    root = Path(path or ".")
    return any((root / f).exists() for f in (".github/workflows", ".gitlab-ci.yml", "bitbucket-pipelines.yml",
                                             "azure-pipelines.yml"))


def _checks_view(res: dict, co: dict, path) -> dict:
    """The latest checks of this branch (or its open PR), in words."""
    runs = (res or {}).get("runs") or []
    pr = co.get("pr") if isinstance(co.get("pr"), dict) else None
    where = f"PR {pr['number']}" if pr and pr.get("state") == "OPEN" else (co.get("branch") or "this branch")
    if not runs:
        if (res or {}).get("error") and (res or {}).get("provider"):
            return {"state": "unknown", "text": f"{ui.glyph('none')} could not read the checks: "
                                                f"{str(res['error'])[:80]}", "command": "dev ci status"}
        if co.get("branch") and not co.get("upstream") and (co.get("unpushed") or co.get("ahead") or not pr):
            return {"state": "none", "text": f"{ui.glyph('none')} not run yet: nothing is pushed on this branch"}
        if not _has_ci_files(path):
            return {"state": "none", "text": f"{ui.glyph('none')} this project has no checks yet. Set them up: "
                                             f"dev ci init", "command": ""}
        return {"state": "none", "text": f"{ui.glyph('none')} no run on {where} yet"}
    r = runs[0]
    st = r.get("status")
    if st in ("running", "queued"):
        jobs = r.get("jobs") or []
        done = sum(1 for j in jobs if j.get("status") not in ("running", "queued"))
        left = f", about {ui.duration(r['eta'])} left" if r.get("eta") else ""
        prog = f": {done} of {len(jobs)} jobs done" if jobs else ""
        return {"state": "running", "text": f"{ui.glyph('running')} running on {where}{prog}{left}",
                "id": r.get("id"), "url": r.get("url")}
    when = ui.rel_time(r.get("finished") or r.get("started"))
    num = f"run {r['number']}, " if r.get("number") else ""
    if st == "success":
        return {"state": "passed", "text": f"{ui.glyph('ok')} passed on {where}, {when} ({num}took "
                                           f"{ui.duration(r.get('elapsed'))})", "id": r.get("id"), "url": r.get("url")}
    if st == "failed":
        bad = next((j.get("name") for j in r.get("jobs") or [] if j.get("status") == "failed"), "")
        return {"state": "failed", "text": f"{ui.glyph('fail')} failed on {where}, {when}" + (f": {bad}" if bad else ""),
                "command": "dev run view --failed", "id": r.get("id"), "url": r.get("url")}
    return {"state": st or "unknown", "text": f"{ui.glyph('none')} {st} on {where}, {when}"}


def _crew_now(runs: list, roster, now: float) -> dict:
    """Who works now in the project's working runs (the shared rule: registry.run_working), each as role and name
    with what the step does, never its code (decision (c)); the plan's steps done and the time left."""
    from . import crew as crew_mod, progress as pg
    working = [r for r in runs if registry.run_working(r, now)]
    out = {"working": bool(working), "members": [], "plan": None, "goal": ""}
    facts = pg.agent_facts(runs) if working else None
    for run in working:
        plan = run.get("plan") or []
        for lid, lane in (run.get("agents") or {}).items():
            if lane.get("state") != "running":
                continue
            lv = pg.lane_state(run, lid, lane, facts, now)  # an ended member is not working; a stale one says so
            if lv["state"] == "ended":
                continue
            m = crew_mod._for_lane(lane, roster)
            who = crew_mod.who(m) if m else (lane.get("persona") or lane.get("label") or "a crew member")
            steps = [s for s in plan if isinstance(s, dict) and s.get("owner") == lid and s.get("state") in
                     ("running", "blocked")]
            what = [s.get("title") or registry.plain_step(s.get("name") or "") for s in steps]
            if not steps and ":" in (lane.get("label") or ""):
                what = [registry.plain_step(lane["label"].split(":", 1)[1])]
            what = [x for x in what if x]
            out["members"].append({"who": who, "on": what, "member": (m or {}).get("id", ""), "state": lv["state"]})
        if plan and out["plan"] is None:
            done, total, _skipped = pg.step_counts(run)  # skipped steps count in neither, as in the Progress line
            try:
                left = pg.eta(run, now)
            except Exception:
                left = None
            out["plan"] = {"done": done, "total": total, "left_min": left}
            out["goal"] = run.get("goal") or ""
    return out


def _where_from(p: dict, co: dict, sl: dict, parsed: dict) -> str:
    """Which checkout a saved update came from, when it is not the one shown: "in the kit-consulting-ci copy
    (fix/chat-subagent-progress)"."""
    cur_path, cur_branch = co.get("path") or p.get("path") or "", co.get("branch") or ""
    path, branch = sl.get("path") or "", parsed.get("branch") or sl.get("branch") or ""
    wts = p.get("worktrees") or []

    def describe(w):
        b = w.get("branch") or "not on a branch"
        loc = w.get("location")
        if loc == "stray":
            return f"in the {Path(w['path']).name} copy ({b})"
        if loc == "claude":
            return f"in a Claude Code worktree ({b})"
        if loc == "codex":
            return f"in a Codex worktree ({b})"
        return f"in the {b} worktree"
    if path and cur_path and core.real(path) != core.real(cur_path):
        w = next((x for x in wts if core.real(x.get("path") or "") == core.real(path)), None)
        if w:
            return describe(w)
        if p.get("path") and core.real(path) == core.real(p["path"]):
            return f"in the main checkout ({branch})" if branch else "in the main checkout"
        return f"in {tilde(path)}"
    if branch and cur_branch and branch != cur_branch and not path:
        w = next((x for x in wts if x.get("branch") == branch), None)
        return describe(w) if w else f"on {branch}"
    return ""


def _layout_need(p: dict, alias: str, standalone: bool) -> dict:
    copies = [w for w in p.get("worktrees") or [] if w.get("location") == "stray"]
    n = len(copies)
    with_ = f", with {n} cop{'ies' if n != 1 else 'y'} next to it" if n else ""
    subject = "This project is" if standalone else f"{alias} is"
    return {"kind": "layout", "text": f"{subject} outside devkit's folder layout{with_}, so some devkit commands skip "
                                      "it. Close its sessions, then move it" + (" with all its worktrees:" if standalone
                                                                                 else ":"),
            "command": f"dev adopt {tilde(p.get('path') or '')}", "project": p["id"]}


def _asker(name, roster):
    """-> (the crew member named `name` or None, how a sentence names them: "The backend engineer (Tank)")."""
    from . import crew as crew_mod
    m = crew_mod.member(name, roster) if name else None
    return m, f"The {crew_mod.who(m)[:1].lower()}{crew_mod.who(m)[1:]}" if m and m.get("title") else (
        (m or {}).get("name") or name or "Morpheus")


def _session_of(ref: str) -> str:
    """'the session' for a bare id, 'session 9448c4d0' for a qualified one (progress.shown_id)."""
    from . import progress as pg
    prefix = pg.split_id(ref)[1]
    return f"session {prefix}" if prefix else "the session"


def _message_need(msg: dict, roster, ref: str = "") -> dict:
    """A crew question to the tech lead that nobody answered yet (progress.waiting_messages); `ref`: its id as shown
    to the caller (progress.shown_id), which `dev crew answer` takes."""
    _, who = _asker(msg.get("from_name"), roster)
    ref = ref or msg.get("id")
    return {"kind": "message", "id": ref, "text": f"{who} asks: {(msg.get('text') or '').rstrip()} Answer it in the "
                                                  f"chat with {_session_of(ref)}, or:",
            "command": f"dev crew answer {ref} \"…\"", "ts": msg.get("ts")}


def _decision_need(d: dict, roster, ref: str = "") -> dict:
    """A decision that needs the developer; `ref`: its id as shown to the caller (progress.shown_id). Its answer is
    typed in the chat of the session that owns it: `approve D3`, or `approve D3@9448c4d0` for another session's run,
    which that session alone takes as the developer's evidence."""
    m, who = _asker(d.get("from_name"), roster)
    q = (d.get("question") or "").rstrip()
    rec = f" {m['name'] if m else who} recommends {d['recommend']}." if d.get("recommend") else ""
    ref = ref or d.get("id")
    answer = f"approve {ref}" + (f": {d['recommend']}" if d.get("recommend") else "")
    where = "Answer in the chat with the session:" if ref == d.get("id") else \
        f"Answer it in the chat with {_session_of(ref)}:"
    return {"kind": "decision", "id": ref, "text": f"{who} asks: {q}{rec} {where}", "command": answer,
            "ts": d.get("ts")}


def _issue_needs(reg: dict, p: dict) -> list:
    out = []
    for kind, msg in reg.get("issues") or []:
        if kind == "layout" or "worktree outside the managed locations" in msg:
            continue  # the layout item covers them, with dev adopt
        if p["id"] in msg or (p.get("path") and tilde(p["path"]) in msg):
            head, cmd, _ = _attention([(kind, msg)])[0]
            out.append({"kind": kind, "text": head.replace(f"{p['id']}: ", "", 1) + ".", "command": cmd})
    return out


def _status_for(sl: dict, caller) -> dict:
    """The saved status to show: this session's own latest when the record keeps one per session (`sessions`, keyed
    "<agent>:<session id>"), else the project's latest."""
    own = (sl.get("sessions") or {}).get(f"{caller[0]}:{caller[1]}") if caller and isinstance(sl.get("sessions"),
                                                                                                dict) else None
    return own if isinstance(own, dict) and own.get("line") else sl


def _status_who(sl: dict, caller) -> dict:
    """Who saved a status that names its session: {"who": "this session"} for the caller's own, else {"who": "Codex
    session @0199a213", "by": "Codex @0199a213"}; {} for a record without a session (an older devkit's)."""
    sid, agent = str(sl.get("session") or ""), str(sl.get("agent") or "")
    if not sid:
        return {}
    if caller and (agent, sid) == (str(caller[0]), str(caller[1])):
        return {"who": "this session", "by": ""}
    name = AGENT_NAMES.get(agent, agent or "agent")
    return {"who": f"{name} session @{sid[:8]}", "by": f"{name} @{sid[:8]}"}


def status_view(reg: dict, p: dict, w: dict = None, runs=None, ci=None, roster=None, now=None, caller=None) -> dict:
    """Everything `dev status` shows for one project, in Sati's order: what needs you, what happens now (checks, the
    crew, the plan), the last update, then the details. `--json` prints this. `caller`: the (agent, session id) that
    runs it, whose own run's messages and decisions keep their bare ids; every other run's carry their session
    (progress.shown_id)."""
    from . import canary, crew as crew_mod, progress as pg
    now = now or time.time()
    roster = roster or crew_mod.load()
    amap = registry.alias_map(reg["projects"])
    alias = amap.get(p["id"], p["id"])
    co = dict(w or p.get("main") or {})
    co.setdefault("path", (w or {}).get("path") or p.get("path"))
    local = registry.is_local(p)
    runs = pg.active_runs() if runs is None else runs
    mine = [r for r in runs if r.get("project") == p["id"]]
    # what needs you is what is open now; the saved block's You line is history once the developer acted on it
    needs = [_decision_need(d, roster, pg.shown_id(r, d.get("id"), caller)) for r in mine
             for d in pg.pending_decisions(r)]
    needs += [_message_need(m, roster, pg.shown_id(r, m.get("id"), caller)) for r in mine
              for m in pg.waiting_messages(r)]
    sl = _status_for(p.get("status_line") or registry.status_line(p["id"]) or {}, caller)
    parsed = canary.parse_line(sl.get("line") or "") if sl.get("line") else {}
    if local and not p.get("canonical_ok", True):
        needs.append(_layout_need(p, alias, True))
    needs += _issue_needs(reg, p) if local else []
    checks = _checks_view(ci if ci is not None else _ci_for(co.get("path"), co.get("branch")), co, co.get("path")) \
        if local else {"state": "none", "text": f"{ui.glyph('none')} not on this Mac"}
    crew_now = _crew_now(mine, roster, now)
    last = None
    if sl.get("line"):
        last = {"agent": (sl.get("agent") or "").capitalize() or "an agent", **_status_who(sl, caller), "ts": sl.get("ts"),
                "when": ui.rel_time(sl.get("ts"), now), "title": parsed.get("task") or "",
                "done": parsed.get("done") if parsed else sl["line"], "check": parsed.get("check", ""),
                "next": parsed.get("next", ""), "where": _where_from(p, co, sl, parsed) if parsed and local else ""}
    wts = p.get("worktrees") or []
    live = sum(1 for x in wts if x.get("active"))
    details = {"branch": co.get("branch"), "changes": _changes(co), "commit": co.get("last_commit") or "",
               "commit_ts": co.get("last_commit_ts"), "worktrees": len(wts), "worktrees_in_use": live,
               "folder": co.get("path") or p.get("path"), "notes": str(core.note_path(p["id"])),
               "commands": p.get("commands") or []}
    from . import branches as branchmap
    return {"project": p["id"], "alias": alias, "local": local, "branch": co.get("branch"), "needs_you": needs,
            "checks": checks, "crew": crew_now, "last_update": last,
            "branches": branchmap.ordered(p.get("branches")) if local else [], "details": details,
            "tasks": ((p.get("note_summary") or {}).get("tasks") or {}), "plans": p.get("plans") or []}


AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex"}


def peer_name(agent, prefix) -> str:
    """Another agent session as every surface names it: "Codex @0199a213"."""
    p = str(prefix or "")
    return f"{AGENT_NAMES.get(str(agent or ''), str(agent or 'agent'))} {p if p.startswith('@') else '@' + p[:8]}"


def quoted(text, limit: int = 300) -> str:
    """Another session's words as one inert quoted span (watch.clean: no line break, backtick, escape sequence,
    direction mark or quote mark of its own), cut at `limit` characters."""
    from . import watch
    return f"“{watch.clean(text, limit)}”"


def _split_key(k) -> tuple:
    agent, _, sid = str(k or "").partition(":")
    return agent, sid


def _escalation_view(e: dict, names: dict, limit: int = 300) -> dict:
    agent, sid = _split_key(e.get("from"))
    return {"id": e.get("id"), "ts": e.get("ts"), "question": quoted(e.get("question"), limit),
            "options": [quoted(o, limit)[1:-1] for o in e.get("options") or []], "recommend": e.get("recommend") or "",
            "from": names.get(e.get("from")) or peer_name(agent, sid[:8]),
            "parties": [names.get(p) or peer_name(*_split_key(p)) for p in e.get("parties") or []]}


def sessions_view(project, caller=None, now=None) -> dict:
    """The Sessions block of `dev status`: the live agent sessions of `project` (every one with None), a count of the
    others on this Mac, this session's overlaps, the claims on the project, this session's questions that wait for
    an answer, its unread messages, and the open escalations this project's sessions are party to. Another session's
    words appear only quoted and cleaned (a claim's reason, an escalation's question and options), never a message's
    text. It never raises: {"error": …} says what could not be read."""
    from . import peers
    now = now or time.time()
    try:
        me = peers.key(*caller) if caller and caller[0] and caller[1] else None
        rows = peers.directory(now=now, me=me, include_idle=True)
        names = {r["key"]: peer_name(r["agent"], r["prefix"]) for r in rows}
        pl = (project or "").lower()
        live = [r for r in rows if r["live"]]
        here = [r for r in live if not pl or r["project"].lower() == pl]
        mine = {r["key"] for r in rows if r.get("me")} | ({me} if me else set())
        out = {"project": project, "here": [], "others": len(live) - len(here), "overlaps": [], "claims": [],
               "asked": [], "inbox": None, "escalations": [], "error": ""}
        for r in here:
            run = r.get("run") or {}
            state = {"working": "working", "waiting": "waiting", "idle": "idle"}.get(r["state"], r["state"])
            text = f"{names[r['key']]} on {r['branch'] or 'no branch'}, {state}"
            if run.get("goal"):
                head = f" ({run['headline']})" if run.get("headline") else ""
                text += f": {quoted(run['goal'], 120)[1:-1]}{head}"
            if r.get("crew"):
                text += f", {len(r['crew'])} crew"
            text += " · this session" if r.get("me") else f" · {ui.rel_time(r['last'], now)}"
            out["here"].append({k: r.get(k) for k in ("key", "agent", "prefix", "project", "branch", "state", "last",
                                                      "me", "run", "crew", "claims", "initiatives")} | {"text": text})
        n, me_here = len(here), any(r.get("me") for r in here)
        if project:
            word = "none here" if not n else "only this one here" if n == 1 and me_here else f"{n} here"
            out["summary"] = word + (f", {out['others']} more on this Mac" if out["others"] else "")
        else:
            out["summary"] = f"{n} live on this Mac" if n else "none live on this Mac"
        if me:
            agent, sid = caller[0], caller[1]
            for o in peers.overlaps(agent, sid, now=now):
                if o["kind"] == "claim":
                    continue  # the claims below say it, with the holder's reason
                ot = o["other"]
                who = f"{peer_name(ot['agent'], ot['prefix'])} ({ot['project'] or '-'} · {ot['branch'] or '-'})"
                text = {"checkout": f"{who} works in this same checkout",
                        "branch": f"{who} is on this branch in another checkout ({tilde(ot['worktree'])})",
                        "repo": f"{who} works in another worktree of this repository ({tilde(ot['worktree'])})",
                        "item": f"{who} works on a branch linked to your work item {o['key'].rsplit(':', 1)[-1]}"
                        }.get(o["kind"], o.get("detail") or "")
                out["overlaps"].append({"kind": o["kind"], "level": o["level"], "other": ot, "text": text})
            for q in peers.open_questions(agent, sid, now=now):
                to = q.get("to") or []
                who = names.get(to[0]) or peer_name(*_split_key(to[0])) if len(to) == 1 else f"{len(to)} sessions"
                out["asked"].append({"id": q["id"], "to": to, "text": f"{q['id']} to {who} waits for an answer"})
            unread = peers.inbox(agent, sid)
            if unread:
                senders = list(dict.fromkeys(
                    peer_name((m.get("from") or {}).get("agent") or "devkit", ((m.get("from") or {}).get("session")
                                                                               or "")[:8])
                    if (m.get("from") or {}).get("session") else "devkit" for m in unread))
                joined = senders[0] if len(senders) == 1 else ", ".join(senders[:-1]) + " and " + senders[-1]
                out["inbox"] = {"unread": len(unread), "from": senders,
                                "text": f"{len(unread)} unread from {joined}: dev session inbox"}
        for c in peers.claims(now=now, project=project or None):
            h = c["holder"]
            who = "this session" if h["key"] in mine else names.get(h["key"]) or peer_name(h["agent"], h["prefix"])
            queue = len(c.get("queue") or [])
            text = (f"{c['resource']} held by {who} until {time.strftime('%H:%M', time.localtime(c['until']))}: "
                    f"{quoted(c['why'], 200)}" + (f" · {queue} waiting for it" if queue else ""))
            out["claims"].append({"resource": c["resource"], "holder": h["key"], "mine": h["key"] in mine,
                                  "until": c["until"], "why": quoted(c["why"], 200), "queue": queue, "text": text})
        projects = {r["key"]: r["project"].lower() for r in rows}
        for e in peers.escalations("open"):
            keys = [e.get("from")] + list(e.get("parties") or [])
            if not pl or mine & set(keys) or any(projects.get(k) == pl for k in keys):
                out["escalations"].append(_escalation_view(e, names))
        return out
    except Exception as e:  # a store that cannot be read says so; dev status still shows the rest
        return {"project": project, "here": [], "others": 0, "overlaps": [], "claims": [], "asked": [], "inbox": None,
                "escalations": [], "summary": "could not be read", "error": f"{type(e).__name__}: {e}"[:200]}


def _escalation_need(e: dict) -> dict:
    """An open escalation needs the developer: settled only by a line they type in any agent session."""
    rec = f" {e['from']} recommends {e['recommend']}." if e.get("recommend") else ""
    letters = [o.split(":", 1)[0] for o in e.get("options") or [] if ":" in o]
    choice = e.get("recommend") or ("<" + " or ".join(letters) + ", or your words>" if letters else "<your choice>")
    return {"kind": "escalation", "id": e["id"], "ts": e.get("ts"),
            "text": f"Sessions ask you to settle: {e['question']} Options: {'; '.join(e.get('options') or [])}.{rec} "
                    f"Type it in any agent session:", "command": f"settle {e['id']}: {choice}"}


def add_sessions(v: dict, project, caller=None, now=None) -> dict:
    """`dev status` gains its Sessions block (v["sessions"]) and the open escalations among what needs you."""
    s = sessions_view(project, caller, now)
    v["sessions"] = s
    v["needs_you"] = list(v.get("needs_you") or []) + [_escalation_need(e) for e in s["escalations"]]
    return v


def render_sessions(s: dict, cols: int) -> list:
    """The Sessions block: the summary, this project's sessions, then overlaps, claims, questions asked, unread."""
    if not s:
        return []
    out = ["", "  " + ui.bold(f"Sessions · {s.get('summary') or ''}".rstrip(" ·"))]
    if s.get("error"):
        return out + ui.row("", f"{ui.glyph('none')} {s['error']}", cols)
    for i, r in enumerate(s["here"]):
        out += ui.row("Here" if i == 0 else "", r["text"], cols)
    for o in s["overlaps"]:
        warn = o.get("level") == "warn"
        out += ui.row("Overlap", (f"{ui.glyph('needs')} " if warn else "") + o["text"], cols,
                      style=ui.amber if warn else None)
    for i, c in enumerate(s["claims"]):
        out += ui.row("Claims" if i == 0 else "", c["text"], cols)
    for i, q in enumerate(s["asked"]):
        out += ui.row("Asked" if i == 0 else "", q["text"], cols)
    if s.get("inbox"):
        out += ui.row("Inbox", s["inbox"]["text"], cols)
    return out


def _render_needs(needs: list, cols: int, title: str = "Needs you") -> list:
    out = [""]
    if not needs:
        return out + ["  " + ui.bold(title), f"  {ui.green(ui.glyph('ok'))}  Nothing needs you right now."]
    out.append("  " + ui.amber(f"{title} · {len(needs)}"))
    for n in needs:
        out += ui.para(n["text"], cols, indent=5, first="  " + ui.amber("!") + "  ")
        if n.get("command"):
            out.append(ui.command(n["command"], 5))
    return out


def render_status(v: dict, cols: int = None) -> list:
    """Sati's `dev status` screen as lines: fits the terminal (text wraps at 100 columns), reads the same with
    NO_COLOR=1, and never cuts a word or a status block."""
    cols = cols or ui.width()
    W = ui.text_width(cols)
    d = v["details"]
    out = ui.title_line(v["alias"], v["project"] if v["project"] != v["alias"] else "", d.get("branch") or "", cols)
    out += _render_needs(v["needs_you"], cols)
    out += ["", "  " + ui.bold("Right now")]
    ck = v["checks"]
    out += ui.row("Checks", ck["text"], cols)
    if ck.get("command"):
        out.append(ui.command(ck["command"], 2 + ui.LABEL_W))
    cr = v["crew"]
    if cr["members"]:
        busy = sum(1 for m in cr["members"] if m.get("state", "working") == "working")
        stale = len(cr["members"]) - busy
        out += ui.row("Crew", f"{ui.glyph('working')} {busy} working" + (f", {stale} stale" if stale else ""), cols,
                      style=None)
        for m in cr["members"][:6]:
            text = m["who"] + (" · stale" if m.get("state") == "stale" else "") + \
                (" on " + ", ".join(m["on"]) if m["on"] else "")
            out += ui.row("", text, cols)
        if len(cr["members"]) > 6:
            out += ui.row("", f"and {len(cr['members']) - 6} more (dev progress)", cols)
    else:
        out += ui.row("Crew", f"{ui.glyph('none')} nobody is working now", cols)
    if cr.get("plan") and cr["working"]:
        pl = cr["plan"]
        left = f", about {ui.duration(pl['left_min'] * 60)} left" if pl.get("left_min") else ""
        out += ui.row("Plan", f"{pl['done']} of {pl['total']} steps done{left}", cols)
    if v.get("now_warning"):
        out += ui.row("Tasks", f"{ui.glyph('needs')} {v['now_warning']}", cols, style=ui.amber)
    out += render_sessions(v.get("sessions"), cols)
    last = v.get("last_update")
    if last:
        head = f"Last update from {last.get('who') or last['agent']}, {last['when']}" + \
            (f", {last['where']}" if last.get("where") else "")
        head += f": {last['title']}" if last.get("title") else ""
        out += [""] + ["  " + ui.bold(x) for x in ui.wrap(head, W - 3)]
        if last.get("done"):
            out += ui.row("Done", last["done"], cols)
        if last.get("check"):
            g = last["check"][:1]
            style = ui.green if g == "✓" else ui.red if g == "✗" else ui.yellow if g == "⚠" else None
            out += ui.row("Check", last["check"], cols, style=style)
        if last.get("next"):
            out += ui.row("Next", last["next"], cols)
    if v.get("branches"):  # where each branch lives (spec P-fy3bwa0pshk R8)
        from . import branches as branchmap
        out += branchmap.status_lines(v["branches"], cols)
    out += ["", "  " + ui.bold("Details")]
    if not v.get("local"):
        out += ui.row("Folder", "not on this Mac. Get it:", cols)
        out.append(ui.command(f"dev clone {v['project']}", 2 + ui.LABEL_W))
        d = dict(d, folder=None, commit=None, worktrees=0)
    else:
        branch = d.get("branch") or "not on a branch"
        out += ui.row("Branch", branch + (f": {d['changes']}" if d.get("changes") else ": nothing to commit or push"),
                      cols)
    if d.get("commit"):
        out += ui.row("Commit", (f"{ui.clock_time(d['commit_ts'])}, " if d.get("commit_ts") else "") + d["commit"],
                      cols)
    if d.get("worktrees"):
        use = f"{d['worktrees_in_use']} in use" if d["worktrees_in_use"] else "none in use"
        out += ui.row("Worktrees", f"{d['worktrees']}, {use} (dev wt ls)", cols)
    room = W - 2 - ui.LABEL_W - 1
    if d.get("folder"):
        out += ui.row("Folder", ui.short_path(d["folder"], room), cols)
    out += ui.row("Notes", ui.short_path(d["notes"], room), cols)
    if d.get("commands"):
        out += ui.row("Commands", ", ".join(d["commands"]), cols)
    if v.get("show_all"):
        t = v.get("tasks") or {}
        out += ["", "  " + ui.bold("Tasks")]
        for bucket in ("now", "next", "later"):
            if t.get(bucket):
                out += ui.row(bucket.capitalize(), "; ".join(t[bucket][:8]), cols)
        if not any(t.get(b) for b in ("now", "next", "later")):
            out.append(f"  {ui.glyph('none')} no open tasks (dev task add \"…\" adds one)")
        if v.get("plans"):
            out += ["", "  " + ui.bold("Plans")]
            for pl in v["plans"][:8]:
                out += ui.para(pl["title"], cols, indent=4)
    out += ["", "  " + ui.dim("More: dev status --all (tasks and plans), dev status --json (for tools)")]
    return out


def home_view(reg: dict, runs=None, name: str = None, roster=None, offline: bool = True, caller=None) -> dict:
    """`dev status` outside a project: what needs you across this Mac's projects, then the projects. `caller`: as
    status_view takes it."""
    from . import crew as crew_mod, progress as pg
    roster = roster or crew_mod.load()
    runs = pg.active_runs() if runs is None else runs
    here, away = registry.split_local(reg["projects"])
    amap = registry.alias_map(reg["projects"])
    needs = []
    for p in here:
        alias = amap.get(p["id"], p["id"])
        for r in runs:
            if r.get("project") == p["id"]:
                for d in pg.pending_decisions(r):
                    n = _decision_need(d, roster, pg.shown_id(r, d.get("id"), caller))
                    n.update(text=f"{alias}: {n['text'][:1].lower()}{n['text'][1:]}", command=f"dev status {alias}")
                    needs.append(n)
        if not p.get("canonical_ok", True):
            needs.append(_layout_need(p, alias, False))
    working = sum(1 for s in reg.get("sessions") or [] if s.get("active"))
    return {"greeting": ui.greeting(name if name is not None else core.gh_user().get("name", "")),
            "local": len(here), "portfolio_only": len(away), "sessions_working": working, "needs_you": needs,
            "projects": _ls_rows(reg, offline=offline)}


def render_home(v: dict, cols: int = None) -> list:
    cols = cols or ui.width()
    n, s = v["local"], v["sessions_working"]
    head = f"On this Mac: {n} project{'s' if n != 1 else ''}" + (
        f", {s} session{'s' if s != 1 else ''} working" if s else "")
    out = ["", ui.rule_title(head, cols)]
    out += ui.para(f"{v['greeting']} You are not inside a project; dev status <name> shows one from here.", cols)
    out += _render_needs(v["needs_you"], cols)
    out += render_sessions(v.get("sessions"), cols)
    if v["projects"]:
        out += ["", "  " + ui.bold("Projects")] + _project_table(v["projects"], cols)
    else:
        out += [""] + _empty_mac(v["portfolio_only"], cols)
    out.append("")
    foot = _portfolio_footer(v["portfolio_only"])
    if foot:
        out.append(foot)
    try:
        from . import canary
        sm = canary.summary(7)
        ok_, tot = sum(o for o, _ in sm.values()), sum(t for _, t in sm.values())
        if tot:
            out.append(f"  {ui.dim(f'Agents followed devkit’s rules in {100 * ok_ // tot}% of replies this week (dev canary).')}")
    except Exception:
        pass
    return out + [""]


def cmd_status(a):
    reg = _reg(prs=True)
    if a.query:
        p, w = _resolve(reg, a.query), None
    else:
        p, w = registry.project_for_cwd(reg, os.getcwd())
    caller = _status_caller()
    if not p:
        v = home_view(reg, offline=False, caller=caller)
        v["needs_you"] += _bug_needs()
        add_sessions(v, None, caller)
        _prompt_refresh(reg)
        if getattr(a, "json", False):
            print(json.dumps(v, indent=2, ensure_ascii=False, default=str))
            return 0
        print("\n".join(render_home(v)))
        return 0
    v = status_view(reg, p, w, caller=caller)
    from . import work
    v["now_warning"] = work.now_warning(p["id"]) if p.get("id") else ""  # a folder with no project id has no tasks
    where = ((w or {}).get("path") or p.get("path")) if a.query else os.getcwd()
    v["needs_you"] += devkit_needs(where)  # the plugins of the project asked about, not of the folder it runs in
    add_sessions(v, p.get("id"), caller)
    _prompt_refresh(reg, where)
    v["show_all"] = bool(getattr(a, "all", False))
    if getattr(a, "json", False):
        print(json.dumps(v, indent=2, ensure_ascii=False, default=str))
        return 0
    print("\n".join(render_status(v)))
    print()
    return 0


def _status_caller():
    """The (agent, session id) `dev status` runs in, as `dev crew answer` resolves it, so an id shown bare answers
    the same run; None when it cannot be told (then every run's ids carry their session)."""
    from . import progress as pg
    try:
        return pg.current_session()
    except Exception:
        return None


def devkit_needs(cwd: str = None) -> list:
    """`dev status` "Needs you": devkit's own notices, from recorded state only (no network): a newer release, and
    extensions changed outside devkit."""
    out = []
    try:
        from . import ext, updatecheck
        line = updatecheck.status_line(installed=updatecheck.installed_version())
        if line:
            out.append({"kind": "devkit", "text": line, "command": "dev update --check"})
        out += ext.status_needs(cwd)
    except Exception as e:  # a notice never breaks dev status
        out.append({"kind": "devkit", "text": f"devkit's notices could not be read ({type(e).__name__})",
                    "command": ""})
    return out + _bug_needs()


def _bug_needs() -> list:
    """`dev status`: one line when items of devkit's bug registry wait for review or devkit bugs hit this Mac today."""
    try:
        from . import bugs
        line = bugs.status_line()
    except Exception as e:  # a notice never breaks dev status
        return [{"kind": "devkit", "text": f"devkit's bug registry could not be read ({type(e).__name__})",
                 "command": ""}]
    return [{"kind": "devkit", **line}] if line else []


def cmd_branch(a):
    """`dev branch ls` is the branch map (branches.py, spec P-fy3bwa0pshk R7); new, rename and clean stay in gitx."""
    if getattr(a, "branch_cmd", None) in ("ls", "embargo", "prune"):
        from . import branches as branchmap
        return {"ls": branchmap.cmd_ls, "embargo": branchmap.cmd_embargo, "prune": branchmap.cmd_prune}[a.branch_cmd](a)
    from . import gitx
    return gitx.cmd_branch(a)


def cmd_go(a):
    p = _resolve(_reg(prs=False), a.query, note=True, here=True)
    if not getattr(a, "branch", None):
        print(p["path"])
        return 0
    from . import completion
    trees = completion.worktrees_of(p["path"])
    want = a.branch.strip().lower()
    hits = [t for t in trees if t[0].lower() == want] or [t for t in trees if want in t[0].lower()]
    if len(hits) != 1:
        alias = registry.alias_map(_reg(prs=False)["projects"]).get(p["id"], p["id"])
        ui.err(f"{len(hits) or 'no'} worktrees of {alias} match “{a.branch}”"
               + (": pick one" if hits else ("; its worktrees:" if trees else "; it has none (dev wt new <branch>)")))
        for b, where in hits or trees:
            print(f"    {ui.bold(b)}  {ui.dim(where)}", file=sys.stderr)
        return 2
    print(os.path.expanduser(hits[0][1]))
    return 0



def _trust_codex(path: Path):
    try:
        from .codexrpc import CodexRPC
        with CodexRPC(timeout=15) as rpc:
            rpc.write([("projects", {str(path): {"trust_level": "trusted"}}, "upsert")])
    except Exception:
        pass


def cmd_clone(a):
    if "/" not in a.repo and ":" not in a.repo and "@" not in a.repo:  # an alias or a name from the portfolio
        a.repo = _resolve(_reg(prs=False), a.repo)["id"]
    src = core.clone_source(a.repo)
    if not src:
        ui.err("expected owner/repo, host/group/repo or a git URL (https or ssh)")
        return 2
    pid, url, host = src
    dest = core.canonical_path(pid)
    if dest.exists():
        print(dest)
        ui.warn(f"already cloned at {tilde(dest)}")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    if host == "github.com" and core.have("gh") and "://" not in a.repo and "@" not in a.repo:
        owner_repo = pid if "/" in pid and not pid.startswith("github.com/") else pid.split("/", 1)[1]
        args = ["gh", "repo", "clone", owner_repo, str(dest)]
    else:
        args = ["git", "clone", url, str(dest)]
    rc = run(args)
    if rc != 0:
        return rc
    _trust_codex(dest)
    registry.update_note(registry.inspect_repo(dest, with_prs=False))
    print(dest)
    return 0


def cmd_new(a):
    if a.github:
        owner = a.owner or core.github_user()
        dest = core.canonical_path(f"{owner.lower()}/{a.name.lower()}")
        if dest.exists():
            ui.err(f"{tilde(dest)} already exists")
            return 2
        vis = "--public" if a.public else "--private"
        rc = run(["gh", "repo", "create", f"{owner}/{a.name}", vis, "--add-readme"])
        if rc != 0:
            return rc
        return cmd_clone(argparse.Namespace(repo=f"{owner}/{a.name}"))
    dest = LOCAL / a.name
    if dest.exists():
        ui.err(f"{tilde(dest)} already exists")
        return 2
    dest.mkdir(parents=True)
    if not a.no_git:
        core.git(dest, "init", "-q", "-b", "main")
    _trust_codex(dest)
    print(dest)
    ui.warn(f"local project created; publish later with `gh repo create --source {tilde(dest)}` then `dev adopt {tilde(dest)}`") \
        if not a.no_git else None
    return 0


def cmd_adopt(a):
    path = Path(core.real(a.path))
    main = registry.main_of(path) or path
    p = registry.inspect_repo(main, with_prs=False)
    if p["canonical_ok"]:
        print(f"{ui.OK} {p['id']} is already at {tilde(main)}")
        return 0
    dest = Path(p["canonical"])
    if dest.exists():
        ui.err(f"{tilde(dest)} already exists — resolve it by hand")
        return 2
    busy = [x for x in [p["path"]] + [w["path"] for w in p["worktrees"] if not w.get("missing")] if core.is_active(x)]
    if busy:
        ui.err("processes are working in this repo; close those sessions/shells first:\n    " +
               "\n    ".join(tilde(b) for b in busy))
        return 3
    print(ui.section(f"Adopting {p['id']}"))
    old_main = Path(p["path"])
    moved = {}
    for w in p["worktrees"]:
        if w.get("missing"):
            continue
        wp = Path(w["path"])
        if core.is_under(wp, old_main):
            moved[str(wp)] = str(dest / wp.relative_to(old_main))
        elif w.get("location") == "stray":
            target = WT_ROOT / p["id"] / wp.name
            target.parent.mkdir(parents=True, exist_ok=True)
            rc, _, err = core.git(old_main, "worktree", "move", str(wp), str(target), timeout=300)
            if rc != 0:
                ui.err(f"git worktree move {tilde(wp)}: {err}")
                return 1
            print(f"  {ui.OK} worktree {tilde(wp)} → {tilde(target)}")
            moved[str(wp)] = str(target)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old_main, dest)
    print(f"  {ui.OK} {tilde(old_main)} → {tilde(dest)}")
    repair = [v for v in moved.values() if Path(v).exists()]
    core.git(dest, "worktree", "repair", *repair, timeout=120)
    print(f"  {ui.OK} git worktree links repaired ({len(repair)})")
    projects = core.CLAUDE_HOME / "projects"
    pairs = [(str(old_main), str(dest))] + list(moved.items())
    renamed = 0
    for old, new in pairs:
        src, dst = projects / core.claude_slug(old), projects / core.claude_slug(new)
        if src.exists():
            if dst.exists():
                for f in src.iterdir():
                    if not (dst / f.name).exists():
                        shutil.move(str(f), str(dst / f.name))
                shutil.rmtree(src, ignore_errors=True)
            else:
                src.rename(dst)
            renamed += 1
    print(f"  {ui.OK} Claude Code history moved for {renamed} path(s) (`claude --resume` works from the new path)")
    _trust_codex(dest)
    gc._log(f"adopt · {p['id']}: {tilde(old_main)} → {tilde(dest)}")
    print(f"  {ui.WARN} Claude Desktop / Codex app chats opened at the old path should be restarted from {tilde(dest)}")
    if core.real(old_main) == core.real(core.source_home()):
        import subprocess  # devkit moved itself: its paths are embedded in hooks, links and launchd — re-apply them
        print(f"  {ui.ARROW} devkit moved itself; re-applying its configuration from {tilde(dest)}")
        return subprocess.call([sys.executable, "-I", "-S", str(dest / "bin" / "dev"), "install"])  # as its shebang
    return cmd_sync(argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=False,
                                       quiet=True))


def cmd_open(a):
    p = _resolve(_reg(prs=False), a.query, note=True, here=True)
    editor = CFG.get("editor") or os.environ.get("EDITOR", "code")
    return run([editor, p["path"]])


# --------------------------------------------------------------------------- agents

def _launch(tool: str, a):
    reg = _reg(prs=False)
    p = _resolve(reg, a.query, note=True, here=True)
    path = p["path"]
    if a.worktree:
        path = wt_new(p, a.worktree, base=None, quiet=True)
        if not path:
            return 1
    if not core.have(tool):
        ui.err(f"{tool} is not installed")
        return 127
    os.chdir(path)
    print(f"{ui.ARROW} {tool} in {tilde(path)}", file=sys.stderr)
    os.execvp(tool, [tool, *a.args])


def cmd_claude(a):
    return _launch("claude", a)


def cmd_codex(a):
    return _launch("codex", a)


def cmd_handoff_import(a):
    from . import migrate
    return migrate.run(Path(a.staging), a.slug, dry_run=a.dry_run)


def cmd_handoff(a):
    reg = _reg(prs=False)
    p = _resolve(reg, a.query)
    f = core.note_path(p["id"])
    registry.update_note(p)
    if a.log:
        from . import work
        ac = work.actor(a.agent if a.agent != "human" else None)
        sf = work.log_session(p["id"], f"{core.fmt_ts(time.time())} · {ac['user']}@{ac['host']} · {ac['agent']} · {a.log}")
        work._commit([sf], f"session log: {p['id']} ({work.actor_str(ac)})")
        print(f"{ui.OK} logged in {tilde(sf)}")
    elif a.path:
        print(f)
    elif a.edit:
        editor = os.environ.get("EDITOR") or CFG.get("editor") or "vi"
        return run([editor, str(f)])
    else:
        print(f.read_text())
    return 0


# --------------------------------------------------------------------------- worktrees

def wt_new(p, branch, base=None, quiet=False, deps=True):
    main = Path(p["path"])
    slug = branch.replace("/", "-")
    path = WT_ROOT / p["id"] / slug
    if path.exists():
        if not quiet:
            ui.warn(f"worktree already exists: {tilde(path)}")
        return str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if p.get("remote"):
        core.git(main, "fetch", "-q", "origin", timeout=120)
    if core.git(main, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] == 0:
        args = ["worktree", "add", str(path), branch]
    elif core.git(main, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}")[0] == 0:
        args = ["worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"]
    else:
        start = base or (f"origin/{p['default_branch']}" if p.get("remote") else "HEAD")
        args = ["worktree", "add", "--no-track", "-b", branch, str(path), start]
    rc, _, err = core.git(main, *args, timeout=300)
    if rc != 0:
        ui.err(f"git worktree add failed: {err}")
        return None
    from . import gitx
    linked = gitx._link_new_branch(p.get("id"), branch)
    if linked:  # always said: it changes a work item
        print(f"{ui.OK} {linked}", file=sys.stderr)
    inc = main / ".worktreeinclude"
    if inc.exists():
        import fnmatch
        pats = [ln.strip() for ln in inc.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
        for root, dirs, files in os.walk(main):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", ".claude")]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), main)
                if any(fnmatch.fnmatch(rel, pt) for pt in pats) and core.git(main, "check-ignore", "-q", rel)[0] == 0:
                    (path / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(main / rel, path / rel)
    msg = ""
    if deps:
        from .hooks import _clone_node_modules
        msg = _clone_node_modules(path, main) or ""
    if not quiet:
        print(f"{ui.OK} worktree {ui.bold(branch)} at {tilde(path)}", file=sys.stderr)
        if msg:
            print(f"{ui.OK} {msg}", file=sys.stderr)
        if (path / "uv.lock").exists():
            print(f"{ui.INFO} Python: run `uv sync` (uv links from its cache; near-zero extra disk)", file=sys.stderr)
    return str(path)


def _wt_merged(p: dict, branch: str):
    """{base, sha, prs} when the worktree's branch is merged into origin/<default> as an ancestor, by its own pull
    request or by content (the merge test of dev branch prune, on the registry's cached pull requests: no GitHub
    call), so `dev wt rm` may remove it with commits on no remote."""
    if not branch:
        return None
    from . import branches
    default = p.get("default_branch") or "main"
    base = f"origin/{default}" if core.git(p["path"], "rev-parse", "--verify", "-q",
                                           f"refs/remotes/origin/{default}")[0] == 0 else default
    prs = branches.cached_prs(p["github"]) if p.get("github") else None
    m = branches.merge_test(p["path"], branch, base, prs=prs)
    return {"base": base, "sha": m["sha"], "prs": prs} if m["merged"] else None


def cmd_wt(a):
    reg = _reg(prs=a.wt_cmd == "ls")
    if a.wt_cmd == "new":
        from .guard import branch_violation
        v = branch_violation(a.branch)
        if v:
            ui.err(v)
            return 2
        p = _resolve(reg, a.repo, note=True, here=True)
        path = wt_new(p, a.branch, base=a.base, deps=not a.no_deps)
        if path:
            print(path)
            return 0
        return 1
    if a.wt_cmd == "ls":
        if a.all:
            ps = reg["projects"]
        else:
            p, _ = registry.project_for_cwd(reg, os.getcwd())
            ps = [p] if p else reg["projects"]
        rows = []
        for p in ps:
            for w in p["worktrees"]:
                pr = w.get("pr")
                rows.append([p["id"], w.get("branch") or "detached",
                             f"#{pr['number']} {pr['state'].lower()}" if pr else "-",
                             "missing" if w.get("missing") else _state(w), w.get("location"),
                             ui.ACTIVE + " live" if w.get("active") else ui.IDLE + " " + ago(w.get("last_activity")),
                             human_kb(w.get("size_kb")), tilde(w["path"])])
        if not rows:
            print("  no worktrees")
            return 0
        print(ui.table(rows, ["PROJECT", "BRANCH", "PR", "STATE", "WHERE", "SESSION", "SIZE", "PATH"],
                       max_widths={0: 36, 1: 34, 7: 70}))
        return 0
    if a.wt_cmd == "rm":
        target = a.target
        for p in reg["projects"]:
            for w in p["worktrees"]:
                if w.get("branch") == target or core.real(w["path"]) == core.real(os.path.expanduser(target)):
                    if w.get("missing"):
                        core.git(p["path"], "worktree", "prune")
                        print(f"{ui.OK} pruned missing worktree {tilde(w['path'])}")
                        return 0
                    ok, msg = gc.remove_worktree(p["path"], w["path"], w.get("branch"), p.get("github"),
                                                 merged=_wt_merged(p, w.get("branch")))
                    print(f"{ui.OK if ok else ui.FAIL} {msg}")
                    return 0 if ok else 1
        ui.err(f"no worktree matches “{target}” — `dev wt ls --all`")
        return 2
    return 2


# --------------------------------------------------------------------------- tracking & disk

def cmd_sync(a):
    from . import hooks
    STATE.mkdir(parents=True, exist_ok=True)
    reasons = []
    if getattr(a, "delay", 0):  # a queued near-real-time run: let a burst of events settle, then take them all
        time.sleep(min(int(a.delay), 120))
        from . import work as _w
        try:
            reasons = [x for x in _w.SYNC_PENDING.read_text().splitlines() if x.strip()]
            _w.SYNC_PENDING.unlink()
        except OSError:
            pass
    if a.session:
        with registry.notes_lock(wait=60):  # reentrant: the note writes inside take it again
            hooks.record_session(a.session)
    with core.Lock("sync", wait=300 if (a.session or a.scheduled) else 10) as ok:
        if not ok:
            if not a.quiet:
                ui.warn("another sync is running")
            return 0
        reg = registry.build(with_prs=True, refresh_prs=a.scheduled, with_sizes=a.scheduled,
                             refresh_sizes=a.scheduled) if not a.offline else \
            registry.build(with_prs=True, refresh_prs=False, with_sizes=False, pr_max_age=10 ** 9)
        results = []
        if a.scheduled and CFG["gc"].get("auto") and not (core.HOME / "Library" / "LaunchAgents" /
                                                          "com.devkit.job.gc.plist").exists():
            # only when the gc job is not installed; an offline or no-push sync deletes nothing on origin
            results = gc.apply(gc.plan(reg, with_sizes=True), only_auto=True, remote=not (a.offline or a.no_push))
            if any(ok_ and m.startswith("removed") for ok_, m in results):
                core._CWDS = None
                reg = registry.build(with_prs=True, refresh_prs=False, with_sizes=False)
        if a.scheduled:
            try:
                from . import providers
                reg["providers"] = providers.status(include_account=False)
            except Exception:
                pass
            try:
                from . import policy
                reg["drift"] = [d.as_dict() for d in policy.diff()]
            except Exception:
                pass
            try:
                from . import approvals
                approvals.sweep()
            except Exception:
                pass
        try:
            from . import sleep as _sleep
            if a.scheduled and _sleep.due() and not (core.HOME / "Library" / "LaunchAgents" /
                                                     "com.devkit.job.sleep.plist").exists():
                _sleep.run(quiet=True)  # only when the sleep job is not installed
        except Exception as e:
            if not a.quiet:
                ui.warn(f"sleep: {e}")
        if (a.scheduled or a.finish) and not a.offline and not a.no_push:
            try:  # back up the branches whose commits exist only on this Mac (spec P-fy3bwa0pshk R4)
                from . import backup
                backup.sync(reg, quiet=a.quiet)
            except Exception as e:  # never let a backup break the sync
                if not a.quiet:
                    ui.warn(f"backup: {e}")
        if a.scheduled and not a.offline and (CFG.get("bugs") or {}).get("sync", True):
            try:  # devkit's bug registry: refresh the known bugs, send what the outbox holds
                from . import bugs
                bugs.sync()
            except Exception as e:  # never let the registry break the sync
                if not a.quiet:
                    ui.warn(f"bug registry: {e}")
        if a.session and a.finish:  # the session ended: off the map and the team's presence before they are written
            agent, _, sid = a.session.partition(":")
            from . import state as _state
            for suffix in (".json", ".stop"):
                f = STATE / "sessions" / f"{agent}-{sid}{suffix}"
                if f.exists():  # under the record's lock, so a hook that already read it cannot write it back
                    _state.remove(f) if suffix == ".json" else f.unlink()
            reg["sessions"] = [s_ for s_ in reg.get("sessions") or []
                               if not (s_.get("agent") == agent and s_.get("session") == sid)]
        try:
            from . import work
            if a.renew:
                work.renew_locks(work.actor(a.renew), sessions=reg.get("sessions"))
            if a.scheduled:
                work.pull()
                work.expire_locks()
            work.write_presence(reg.get("sessions") or [])
        except Exception as e:
            if not a.quiet:
                ui.warn(f"work items: {e}")
        with registry.notes_lock(wait=60):  # reentrant: write_outputs and update_note take it again
            changed = registry.write_outputs(reg)
        try:
            from . import dashboard
            dashboard.write(reg)
        except Exception as e:  # never let the view break the sync
            if not a.quiet:
                ui.warn(f"dashboard: {e}")
        commit_push(push=CFG["sync"].get("push", True) and not a.no_push, quiet=a.quiet)
        (STATE / "last-sync.json").write_text(json.dumps({"ts": time.time(), "changed": changed,
                                                         "gc": [m for _, m in results]}))
        if a.scheduled:
            from . import canary
            canary.prune()
            _rotate_log(STATE / "logs" / "sync.log")
            _expire_sessions()
            try:  # continuity records unchanged for 7 days, and clear links older than 10 minutes (spec C12)
                from . import continuity
                continuity.expire()
            except Exception as e:
                if not a.quiet:
                    ui.warn(f"continuity: {e}")
        if not a.quiet:
            print(f"{ui.OK} synced {len(reg['projects'])} projects · {len(changed)} file(s) changed"
                  + (f" · gc: {len(results)} action(s)" if results else "") + f" · {tilde(HANDOFF / 'PROJECTS.md')}")
    return 0


def _rotate_log(f: Path, limit=2_000_000):
    try:
        if f.exists() and f.stat().st_size > limit:
            f.replace(f.with_suffix(".log.1"))
    except OSError:
        pass


def _expire_sessions(days=7):
    d = STATE / "sessions"
    if not d.exists():
        return
    for f in d.iterdir():
        try:
            if time.time() - f.stat().st_mtime > days * 86400:
                f.unlink()
        except OSError:
            pass


def cmd_gc(a):
    """`dev gc` is `dev free` under its old name (Hamann's decision (b)); `--apply` (and `-v`) keep the unattended
    cleanup that scripts and the nightly gc job run."""
    if a.apply or a.verbose:
        return _gc_unattended(a)
    from . import disk
    return disk.cmd_free(a)


def _gc_unattended(a):
    print(ui.section("Scanning (PR states refreshed from GitHub)"))
    reg = registry.build(with_prs=True, refresh_prs=True, with_sizes=True)
    pl = gc.plan(reg, with_sizes=True)
    if pl["actions"]:
        print(ui.section(f"Reclaimable: {human_kb(pl['reclaimable_kb'])}"))
        for x in pl["actions"]:
            label = {"remove-worktree": "remove worktree", "prune-artefacts": "prune build output",
                     "purge-temp": "purge temp", "prune-meta": "prune metadata",
                     "prune-branch": f"delete {x.get('where', '')} branch"}[x["kind"]]
            print(f"  {ui.ARROW} {label:<19} {tilde(x['path'])}  {ui.dim(x['reason'] + ' · ' + human_kb(x.get('kb')))}")
    else:
        print(f"  {ui.OK} nothing to reclaim")
    if a.verbose and pl["notes"]:
        print(ui.section("Kept"))
        for n in pl["notes"]:
            print(f"  {ui.IDLE} {n}")
    elif pl["notes"]:
        kept = len(pl["notes"])
        print(f"\n  {ui.dim(f'{kept} item(s) kept on purpose — `dev gc -v` lists them')}")
    if not a.apply or not pl["actions"]:
        if pl["actions"]:
            print(f"\n  {ui.dim('dry run — `dev gc --apply` reclaims it')}")
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            ui.err("non-interactive: pass --yes to apply")
            return 2
        if input(f"\n  Apply {len(pl['actions'])} action(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    print(ui.section("Applying"))
    for ok, msg in gc.apply(pl, only_auto=getattr(a, "auto_only", False), remote=True):
        print(f"  {ui.OK if ok else ui.WARN} {msg}")
    return cmd_sync(argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=False,
                                       quiet=False, renew=None))


def _du_rows(reg: dict) -> list:
    """[alias, main size, worktrees size, worktrees] for the projects on this Mac (R1), largest first."""
    here, _ = registry.split_local(reg["projects"])
    amap = registry.alias_map(reg["projects"])
    rows = []
    for p in sorted(here, key=lambda x: -(x.get("size_kb") or 0)):
        wts = p.get("worktrees") or []
        wt_kb = sum(w.get("size_kb") or 0 for w in wts)
        rows.append([amap.get(p["id"], p["id"]), human_kb(p.get("size_kb")), human_kb(wt_kb) if wts else "-",
                     str(len(wts))])
    return rows


def cmd_du(a):
    from . import disk
    return disk.cmd_du(a)


def cmd_free(a):
    from . import disk
    return disk.cmd_free(a)


# --------------------------------------------------------------------------- environment

def cmd_doctor(a):
    from . import setup
    return setup.doctor()


def cmd_install(a):
    from . import setup
    why = (a.why or "").strip()
    if a.unverified and not why:
        ui.err('--unverified needs --why "<reason>": it is recorded in the audit')
        return 2
    if why and not a.unverified:
        ui.err("--why goes with --unverified (the reason to install a commit that is neither a release nor tested)")
        return 2
    only = {x.strip() for x in a.only.split(",") if x.strip()} if a.only else None
    rc = setup.run_install(only=only, unverified_why=why if a.unverified else None, version=a.version,
                           as_pinned=a.as_pinned, commit_template=getattr(a, "commit_template", None),
                           assume_yes=bool(getattr(a, "yes", False)))
    if rc == 0 and (not only or "claude" in only):  # what was installed: `dev config` tells new from removed
        try:
            write_install_record(create=not only)  # only a whole install that finished creates it
        except Exception as e:
            ui.warn(f"could not record what was installed ({e}); dev config may show removed settings as new")
    return rc


RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def newest_release(tags):
    """The newest release tag (vYY.MM.PATCH) among `tags`, compared as numbers: v26.11.10 is newer than v26.11.2.
    None when no tag is a release (other tags, such as v26.12.0-rc1 or nightly, are not)."""
    found = [(tuple(int(x) for x in m.groups()), t) for t in tags if (m := RELEASE_TAG_RE.match(t.strip()))]
    return max(found)[1] if found else None


def update_checkout(home: Path):
    """Bring the devkit checkout at `home` up to date -> (exit code, message, whether to run `dev install`).

    Detached at a release tag (how the installer leaves it): fetch the tags and check out the newest release when it
    is newer. On a branch: `git pull --ff-only`. Refused, with nothing changed, when tracked files have local changes
    or HEAD is detached somewhere that is not a release."""
    where = tilde(home)
    rc, dirty, err = core.git(home, "status", "--porcelain", "--untracked-files=no")
    if rc != 0:
        return rc, f"{where} is not a git checkout: {err}", False
    if dirty:
        return 1, (f"{where} has local changes, so nothing was updated. Commit or discard them first "
                   f"(git -C {where} status)."), False
    branch = core.gitout(home, "symbolic-ref", "-q", "--short", "HEAD")
    if branch:
        rc, out, err = core.git(home, "pull", "--ff-only", timeout=120)
        return rc, f"{branch}: {out or err}", rc == 0
    current = newest_release(core.gitout(home, "tag", "--points-at", "HEAD").split())
    if not current:
        at = core.gitout(home, "rev-parse", "--short", "HEAD")
        return 1, (f"{where} is at commit {at}, which is neither a release nor a branch, so nothing was updated. "
                   f"Check out a release (git -C {where} checkout <tag>) or a branch, then run dev update."), False
    rc, out, err = core.git(home, "fetch", "--quiet", "--tags", timeout=120)
    if rc != 0:
        return rc, f"git fetch --tags failed: {err or out}", False
    newest = newest_release(core.gitout(home, "tag", "--list", "v*").split() + [current])
    if newest == current:
        return 0, f"already at the newest release, {current}", False
    rc, out, err = core.git(home, "-c", "advice.detachedHead=false", "checkout", "--quiet", newest, timeout=60)
    if rc != 0:
        return rc, f"git checkout {newest} failed: {err or out}", False
    return 0, f"{current} → {newest}", True


def cmd_update(a):
    if getattr(a, "check", False):  # read-only: agents may run it (guard)
        from . import updatecheck
        return updatecheck.cmd_check(a)
    if getattr(a, "quiet", False) or getattr(a, "record", False):
        ui.err("--quiet and --record go with --check")
        return 2
    from . import hooks
    if not hooks.confirm_restart(assume_yes=bool(getattr(a, "yes", False))):  # before anything changes
        print(f"  {ui.FAIL} stopped before any change: restart those sessions, or run dev update again and go on")
        return 1
    home = core.source_home()  # the checkout; `dev` itself may run from the pinned copy, which git never changes
    print(f"{ui.ARROW} updating {tilde(home)}")
    rc, message, install = update_checkout(home)
    print(f"  {ui.OK if rc == 0 else ui.FAIL} {message}")
    if rc != 0 or not install:
        return rc
    # pins the release; the developer already chose to go on above, so the install does not ask again
    os.execv(sys.executable, [sys.executable, "-I", "-S", str(home / "bin" / "dev"), "install", "--yes"])


def cmd_docker(a):
    d = CFG["docker"]
    if a.action == "up":
        if core.sh(["colima", "status"], timeout=20)[0] == 0:
            print(f"{ui.OK} Colima is already running")
        else:
            args = ["colima", "start", "--cpu", str(d["cpu"]), "--memory", str(d["memory"]), "--disk", str(d["disk"]),
                    "--vm-type", d["vm_type"], "--mount-type", d["mount_type"]]
            if d.get("rosetta") and d["vm_type"] == "vz":
                args.append("--vz-rosetta")
            print(f"{ui.ARROW} {' '.join(args)}")
            rc = run(args)
            if rc != 0:
                return rc
        rc, out, _ = core.sh(["docker", "info", "--format", "{{.ServerVersion}} · {{.NCPU}} CPU · {{.MemTotal}} bytes · {{.OperatingSystem}}"], timeout=30)
        print(f"{ui.OK} docker {out}" if rc == 0 else f"{ui.FAIL} docker is not reachable")
        return 0 if rc == 0 else 1
    if a.action == "down":
        return run(["colima", "stop"])
    if a.action == "status":
        run(["colima", "status"])
        if core.sh(["docker", "info"], timeout=10)[0] == 0:
            run(["docker", "system", "df"])
        return 0
    if a.action == "trim":
        from . import disk
        return disk.cmd_trim(a)
    if a.action == "prune":
        run(["docker", "system", "prune", "-f"])
        return run(["docker", "builder", "prune", "-f"])
    if a.action in ("autostart-on", "autostart-off"):
        verb = "start" if a.action.endswith("on") else "stop"
        return run(["brew", "services", verb, "colima"])
    return 2


def _print_ci_runs(res, now=None, a_=None):
    from . import cistatus
    name = {"github": "GitHub Actions", "gitlab": "GitLab CI", "bitbucket": "Bitbucket Pipelines",
            "azure": "Azure Pipelines"}.get(res.get("provider"), "CI")
    print(ui.section(f"{name} · {res.get('repo') or '-'}" + (f" · {res['branch']}" if res.get("branch") else "")))
    if res.get("error"):
        print(f"  {ui.WARN} {res['error']}")
        return
    if not res["runs"]:
        print("  no runs")
        return
    color = {"success": ui.green, "failed": ui.red, "running": ui.cyan, "queued": ui.yellow}
    for r in res["runs"]:
        print("  " + color.get(r["status"], ui.dim)(cistatus.describe(r, now)) +
              (ui.dim(f"  {r['branch']}") if not res.get("branch") and r.get("branch") else ""))
        if r.get("jobs"):
            print("      " + ui.dim(cistatus.job_summary(r["jobs"])))
            shown, more = cistatus.jobs_to_show(r["jobs"], 99 if getattr(a_, "all_jobs", False) else 6)
            for j in shown:
                print("      " + color.get(j["status"], ui.dim)(cistatus.describe_job(j)))
            if more:
                print("      " + ui.dim(f"… +{more} more running (dev ci status --all-jobs)"))
        if r["status"] in cistatus.RUNNING and r.get("url"):
            print("      " + ui.dim(r["url"]))


def cmd_ci_status(a, top):
    from . import cistatus
    branch = None if a.all_branches else (a.branch or core.gitout(top, "branch", "--show-current") or None)
    while True:
        res = cistatus.runs_for(top, branch, limit=a.limit, detail=True, max_age=0 if a.watch else 20)
        if a.json:
            print(json.dumps(res, indent=1))
            return 0
        running = [r for r in res["runs"] if r["status"] in cistatus.RUNNING]
        quiet_wait = a.watch and not sys.stdout.isatty() and running  # agents: no output until the run ends
        if a.watch and sys.stdout.isatty():
            print("\033[2J\033[H", end="")
        if not quiet_wait:
            _print_ci_runs(res, a_=a)
        if not a.watch or not running:
            failed = [r for r in res["runs"][:1] if r["status"] == "failed"]
            if failed and res.get("provider") == "github":
                print(ui.dim(f"\n  failing log: gh run view {failed[0]['id']} --log-failed"))
            return 1 if failed else 0
        time.sleep(15)


def cmd_ci(a):
    from . import ci
    if a.query == "watches":  # the live watches, and the expired ones with the command to re-arm each
        from . import ciwatch
        return ciwatch.cmd_watches(a)
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if a.query and a.query not in ("status", "audit", "init", "watch", "watches"):
        top = _resolve(_reg(prs=False), a.query)["path"]
    if getattr(a, "project", None):
        top = _resolve(_reg(prs=False), a.project)["path"]
    if a.query == "watch" and getattr(a, "wait", False):  # an agent waits on one run: its output wakes the agent
        from . import ciwatch
        return ciwatch.cmd_wait(a, top)
    if not top:
        ui.err("not inside a repository")
        return 2
    if a.query in ("audit", "init"):  # pipelines: diagnose and fix, or set one up
        from . import pipelines
        return (pipelines.cmd_audit if a.query == "audit" else pipelines.cmd_init)(a, top)
    if a.query == "watch":  # follow the runs to the end; diagnose a failure
        return cmd_ci_watch(a, top)
    from . import gitx
    r = gitx.ci_command(a, top)  # --run, --mark-passed, --evidence: local CI evidence for dev push
    if r is not None:
        return r
    if a.query == "status" or a.status:
        return cmd_ci_status(a, top)
    if a.commands:
        print("\n\n".join(ci.local_commands(Path(top))))
        return 0
    print(ci.render(Path(top), ui))
    if a.runs:
        print(ui.section("Recent runs"))
        run(["gh", "run", "list", "--limit", "8"])
    return 0


def cmd_ci_watch(a, top):
    from . import ciwatch
    res = ciwatch.watch(top, branch=a.branch, sha=getattr(a, "sha", None), timeout_min=getattr(a, "timeout", 90) or 90,
                        quiet=getattr(a, "quiet", False) or a.json)
    if getattr(a, "session", None) and ":" in a.session:
        ciwatch.deliver(*a.session.split(":", 1), res)
    if a.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        print(ciwatch.message(res).replace("devkit · ", "", 1))
    return {"passed": 0, "failed": 1, "timeout": 3, "none": 4}[res["state"]]


def cmd_canary(a):
    from . import canary
    print(ui.section(f"Stop hook — last {a.days} days (aim: no turn sent back)"))
    for line in canary.stop_block_lines(a.days):
        print(line)
    rs = canary.records(a.days)
    print(ui.section(f"Instruction canary {canary.MARKER} — last {a.days} days"))
    if not rs:
        print("  no tracked replies yet (replies in sessions under ~/Development are checked after every turn)")
        return 0
    surf = canary.by_surface(a.days)
    for agent, (ok, tot) in sorted(canary.summary(a.days).items()):
        pct = 100 * ok // tot
        mark = ui.OK if pct >= 95 else ui.WARN if pct >= 80 else ui.FAIL
        print(f"  {mark} {agent.capitalize():<8} {ok}/{tot} replies carried the marker ({pct}%)")
        for (ag, where), (sok, stot) in sorted(surf.items()):
            if ag == agent and len([k for k in surf if k[0] == agent]) > 1:
                print(f"      {ui.dim(where.ljust(16))} {sok}/{stot} ({100 * sok // stot}%)")
    fixed = canary.corrected(a.days)
    if fixed:
        print(f"  {ui.dim(f'{fixed} miss(es) corrected after the Stop hook reminder (not counted above)')}")
    print(ui.dim("  checked on development turns only; a recipe or consulting work is not scored"))
    misses = [r for r in rs if not r["ok"]]
    if misses:
        print(ui.section(f"Deviations ({len(misses)})"))
        for r in misses[-a.limit:]:
            print(f"  {ui.FAIL} {core.fmt_ts(r['ts'])} {r['agent']:<7} {r.get('project') or '-':<40} "
                  f"{ui.dim(r.get('excerpt', '')[:70])}")
    return 0


def _note_project(a):
    """The note to edit: an initiative (-i) or a project (-p, default: the one you are in)."""
    slug = getattr(a, "initiative", None)
    if slug:
        f = registry.initiative_path(slug)
        if not f.exists():
            ui.err(f"no initiative “{slug}” — `dev initiative ls`")
            sys.exit(2)
        return {"id": f"initiative:{slug}"}, f
    reg = _reg(prs=False)
    p = _resolve(reg, getattr(a, "project", None))
    f = core.note_path(p["id"])
    if p.get("local", True) and p.get("path"):
        registry.update_note(p)
    return p, f


def cmd_initiative(a):
    from . import notes
    act = a.init_cmd or "ls"
    if act == "new":
        f = registry.initiative_path(a.slug)
        if f.exists():
            ui.err(f"initiative {a.slug} already exists: {tilde(f)}")
            return 2
        projects = [p.strip().lower() for p in (a.projects or "").split(",") if p.strip()]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(notes.new_initiative(a.title or a.slug.replace("-", " ").title(), core.github_user(), projects))
        print(f"{ui.OK} initiative {ui.bold(a.slug)} → {tilde(f)}")
        return 0
    if act in ("add", "rm"):
        f = registry.initiative_path(a.slug)
        if not f.exists():
            ui.err(f"no initiative “{a.slug}”")
            return 2
        with registry.notes_lock(wait=30):  # from the read to the write: another writer of the README waits
            text = f.read_text()
            meta, _ = notes.split_frontmatter(text)
            cur = [p.lower() for p in (meta.get("projects") or [])]
            pid = a.project.lower()
            cur = sorted(set(cur) | {pid}) if act == "add" else [p for p in cur if p != pid]
            text = notes.set_meta(text, projects=cur)
            if act == "add" and f"### {pid}" not in text:
                text = text.replace("## Tasks\n", f"### {pid}\n_Role in the initiative, state, next step._\n\n## Tasks\n",
                                    1)
            core.write_if_changed(f, text)
        print(f"{ui.OK} {a.slug}: projects = {', '.join(cur) or '-'}")
        return 0
    reg = registry.build(with_prs=False)
    inits = reg["initiatives"]
    if act == "show":
        i = next((x for x in inits if x["id"] == a.slug), None)
        if not i:
            ui.err(f"no initiative “{a.slug}”")
            return 2
        print(ui.section(f"{i['title']}  {ui.dim(i['status'] + ' · ' + i['priority'])}"))
        pct = f"{i['progress']}% ({i['progress_source']})" if i["progress"] is not None else "-"
        print(ui.kv("progress", pct) + (f"   milestone {i['milestone']}" if i["milestone"] else "") +
              (f"   due {i['due']}" if i["due"] else ""))
        if i["goal"]:
            print(ui.kv("goal", i["goal"][:200]))
        for m in i["members"]:
            print(f"  {ui.ACTIVE if m.get('local') else ui.IDLE} {m['id']:<44} {m['status']:<8} "
                  f"{(str(m['progress']) + '%') if m['progress'] is not None else '':>5}  "
                  f"{ui.dim((m['workstream'] or '').splitlines()[0][:70] if m['workstream'] else '')}")
        for t in i["tasks"]["now"]:
            print(f"  {ui.cyan('▸')} {t}")
        print(ui.kv("note", tilde(HANDOFF / i["note"])))
        return 0
    if not inits:
        hint = 'no initiatives yet — dev initiative new <slug> --title "…" --projects owner/a,owner/b'
        print(f"  {ui.dim(hint)}")
        return 0
    rows = [[ui.bold(i["id"]), i["status"], i["priority"],
             f"{i['progress']}%" if i["progress"] is not None else "-", i["milestone"] or "-", i["due"] or "-",
             str(len(i["projects"])), str(i["tasks"]["open"])] for i in inits]
    print(ui.table(rows, ["INITIATIVE", "STATUS", "PRIORITY", "PROGRESS", "MILESTONE", "DUE", "PROJECTS", "OPEN"]))
    return 0


def _scope(a):
    """-> (scope, label): an initiative (-i) or a project (-p, default: the one you are in)."""
    if getattr(a, "initiative", None):
        if not registry.initiative_path(a.initiative).exists():
            ui.err(f"no initiative “{a.initiative}” — `dev initiative ls`")
            sys.exit(2)
        return f"initiatives/{a.initiative}", f"initiative {a.initiative}"
    p, _ = _note_project(a)
    return p["id"], p["id"]


def _print_items(scope, label, show_all=False, rebind=True):
    """The open items, numbered for `dev task <verb> <n>`. rebind: `dev task` itself, whose numbers the next verbs use;
    the list printed after a write keeps the numbers of the list shown before and is numbered only while it is that
    list (work.show)."""
    from . import work
    items = work.all_items(scope)
    ordered, numbered = work.show(scope, items, rebind=rebind)
    print(ui.section(f"{label} · tasks"))
    if not items:
        hint = 'no tasks yet — dev task add "…" --now'
        print(f"  {ui.dim(hint)}")
        return
    me = f"{work.actor()['user']}@{work.actor()['host']}"
    for n, it in enumerate(ordered, 1):
        m = it["meta"]
        holder, expired = work.lock_holder(it)
        lock = ""
        if holder and not expired:
            lock = ui.green(" 🔒 you") if holder.startswith(me) else ui.yellow(f" 🔒 {holder.split(' ')[0]}")
        st = {"doing": ui.cyan("doing"), "blocked": ui.red("blocked")}.get(m.get("status"), ui.dim(m.get("bucket", "")))
        pr = ui.yellow(" !") if m.get("priority") == "high" else ""
        num = str(n).rjust(2) if numbered else " ·"
        print(f"  {ui.cyan(num)} {ui.dim(m['id'])} {st:<8} {m['title']}{pr}{lock} {ui.dim('v' + str(m['version']))}")
    if ordered and not numbered:
        why = "this write changed the list" if not rebind else "devkit could not record the list"
        print(f"  {ui.dim(f'no numbers: {why}; dev task numbers it again, and ids always work')}")
    done = sorted([i for i in items if i["meta"].get("status") == "done"], key=lambda i: i["meta"].get("updated", ""),
                  reverse=True)
    for it in (done if show_all else done[:5]):
        print(f"   {ui.green('✓')} {ui.dim(it['meta']['id'])} {ui.dim(it['meta']['title'])}")


def delegate_message(it: dict, who: str, why: str, label: str) -> str:
    """A short, plain message the developer sends to the teammate who takes over a task."""
    m, body = it["meta"], (it.get("body") or "").strip()
    sections, cur = {"": []}, ""
    for ln in body.splitlines():
        if ln.startswith("## "):
            cur = ln[3:].strip().lower()
            sections[cur] = []
        else:
            sections.setdefault(cur, []).append(ln)
    filled = lambda lines: [x for x in lines if x.strip() and not re.match(r"^\s*(-\s+(\[ \]\s+)?|\d+\.\s+)?_.*_\s*$", x)]  # noqa: E731
    criteria = [re.sub(r"^\s*- \[[ x]\]\s*", "", x).strip() for x in filled(sections.get("done when", []))]
    inline = next((ln.split(":", 1)[1].strip() for ln in body.splitlines()
                   if ln.lower().startswith(("done when:", "done means:", "acceptance:"))), "")
    done_when = "; ".join(criteria) or inline
    ctx_lines = []
    for key in ("", "why", "context", "symptom", "question", "scope"):
        ctx_lines += [x.strip() for x in filled(sections.get(key, []))
                      if not x.lower().startswith(("done when:", "done means:", "acceptance:"))]
    context = " ".join(ctx_lines)[:400]
    links = [x for x in (m.get("links") or []) if x]
    scope = it.get("scope") or m.get("scope")
    flag = f"-i {scope.split('/', 1)[1]}" if scope.startswith("initiatives/") else f"-p {scope}"
    lines = [f"Hi @{who}, could you take this task: “{m['title']}”?", ""]
    if why or context:
        lines.append(f"Why it matters: {why or context}")
    if context and why:
        lines.append(f"Context: {context}")
    lines.append(f"Done means: {done_when or 'the change is merged with tests, and the task is marked done.'}")
    lines.append(f"Deadline: {m.get('due') or 'none set, so tell me if it will take more than a few days.'}")
    ref = f"Task {m['id']} in {label}" + (f" · links: {', '.join(links)}" if links else "")
    lines += [ref, "",
              f"To pick it up, run `dev task start {m['id']} {flag}`. It locks the task so nobody else starts it, "
              f"and `dev task show {m['id']} {flag}` shows its full history. Ask me anything here."]
    return "\n".join(lines)


def _task_branch(scope: str) -> str:
    """The branch checked out here when it belongs to the task's project and is not a permanent one: `dev task start`
    links it (`branch:<name>`), so the branch map finds the task without its id in the branch name."""
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    branch = core.gitout(top, "branch", "--show-current") if top else ""
    if not branch or branch in ("main", "master", "develop", "trunk", "gh-pages"):
        return ""
    main = registry.main_of(Path(top)) or Path(top)
    pid = core.project_identity(main, core.gitout(top, "config", "--get", "remote.origin.url"))[0]
    return branch if pid == scope and branch != registry.default_branch(main) else ""


def _checkout_of(scope: str) -> str:
    """A checkout of the project on this Mac: the repository you are in when it is that project, else its folder in
    the workspace. '' when there is none."""
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if top:
        main = registry.main_of(Path(top)) or Path(top)
        if core.project_identity(main, core.gitout(top, "config", "--get", "remote.origin.url"))[0] == scope:
            return top
    d = core.DEV / scope
    return str(d) if (d / ".git").exists() else ""


def reconcile_text(r: dict, label: str, applied: bool) -> str:
    """`dev task reconcile`'s report: each kind of stale item, what --apply does with it, then what changed."""
    def row(i, more=""):
        return f"    {i['id']}  {i['title']}{more}"
    out = [ui.section(f"{label} · reconcile")]
    if r["lockless_doing"]:
        out.append(f"  Doing without a lock ({len(r['lockless_doing'])}): back to todo with --apply")
        out += [row(i) for i in r["lockless_doing"]]
    if r["merged"]:
        out.append(f"  Open, but their branch is merged ({len(r['merged'])}): done with --apply")
        out += [row(i, f" · {i['branch']} merged in {i['commit'][:7]}" + (f" (PR #{i['pr']})" if i.get("pr") else ""))
                for i in r["merged"]]
    if r["duplicates"]:
        out.append(f"  Same title ({len(r['duplicates'])} group(s)): review them, then "
                   f"dev task cancel <id> --why \"duplicate of <id>\"")
        out += [f"    {', '.join(i['id'] + ' (' + i['status'] + ')' for i in g)}  {g[0]['title']}"
                for g in r["duplicates"]]
    fixable = len(r["lockless_doing"]) + len(r["merged"])
    if not fixable and not r["duplicates"]:
        out.append(f"{ui.OK} nothing stale in {label}")
    elif applied:
        out.append(f"{ui.OK} changed {len(r['changed'])} of {fixable} item(s) in one commit")
    elif fixable:
        out.append(ui.dim(f"  nothing changed · dev task reconcile --apply fixes the {fixable} item(s) marked "
                          f"\"with --apply\""))
    return "\n".join(out)


def _task_reconcile(a, scope: str, label: str) -> int:
    """`dev task reconcile`: the stale items of a project, from its items and the merges on its default branch."""
    from . import gitx, work
    if scope.startswith("initiatives/"):
        ui.err("dev task reconcile works on a project, whose git history says what was merged: use -p <project>")
        return 2
    top = _checkout_of(scope)
    if not top:
        ui.err(f"no checkout of {scope} on this Mac: run dev task reconcile inside it (dev go {scope})")
        return 2
    default = registry.default_branch(Path(top))
    if core.gitout(top, "remote"):
        rc, _, err = core.git(top, "fetch", "-q", "origin", default, timeout=60)
        if rc != 0:
            ui.warn(f"git fetch failed ({(err or '').strip()[:120]}); using the last known origin/{default}")
    if a.apply:
        work.pull()
    r = work.reconcile(scope, gitx.merged_into_default(top, default, work.linked_branches(scope)), apply=a.apply)
    if a.json:
        print(json.dumps(r, indent=1, ensure_ascii=False))
    else:
        print(reconcile_text(r, label, a.apply))
    return 0


def cmd_task(a):
    from . import work
    scope, label = _scope(a)
    act = a.task_cmd or "ls"
    try:
        if act == "add":
            bucket = "now" if a.now else "later" if a.later else "next"
            body = a.body or ("" if a.no_template else work.task_body(a.type, a.why or "", a.done))
            it = work.create(scope, " ".join(a.text), kind=a.type, bucket=bucket, priority=a.priority,
                             body=body, why=a.why or "", due=a.due or "")
            print(f"{ui.OK} {work.acted('added', it)} to {bucket} · {label}")
            if "_" in body and not a.body:
                print(ui.dim(f"  fill in the description: dev task edit {it['meta']['id']} --body-file <file> --why \"…\""))
            if a.claim:
                work.claim(it, why=a.why or "", branch=_task_branch(scope))
                print(f"{ui.OK} claimed by you")
            if bucket == "now" and work.now_warning(scope):
                ui.warn(work.now_warning(scope))
        elif act in ("start", "claim"):
            it = work.claim(work.find(" ".join(a.selector), scope), why=a.why or "", branch=_task_branch(scope))
            print(f"{ui.OK} {work.acted('claimed', it)} until {it['meta']['lock_expires']} — others see it as yours")
        elif act == "release":
            it = work.release(work.find(" ".join(a.selector), scope), why=a.why or "", status="todo")
            print(f"{ui.OK} {work.acted('released', it)}")
        elif act == "done":
            it = work.find(" ".join(a.selector), scope)
            it = work.release(it, why=a.why or "", status="done")
            print(f"{ui.OK} {work.acted('done', it)}")
        elif act == "reopen":
            it = work.reopen(work.find(" ".join(a.selector), scope), why=a.why or "")
            print(f"{ui.OK} {work.acted('reopened', it)} (todo) · why: {a.why}")
        elif act == "block":
            it = work.update(work.find(" ".join(a.selector), scope), "block", why=a.why or "", status="blocked")
            print(f"{ui.OK} {work.acted('blocked', it)}")
        elif act == "unblock":
            it = work.find(" ".join(a.selector), scope)
            holder, expired = work.lock_holder(it)
            it = work.update(it, "unblock", why=a.why or "", status="doing" if holder and not expired else "todo")
            print(f"{ui.OK} {work.acted('unblocked', it)} ({it['meta']['status']})")
        elif act == "link":
            it = work.find(" ".join(a.selector), scope)
            tag = f"initiative:{a.to}"
            if not registry.initiative_path(a.to).exists():
                ui.err(f"no initiative “{a.to}” — `dev initiative ls`")
                return 2
            links = list(dict.fromkeys((it["meta"].get("links") or []) + [tag]))
            it = work.update(it, "link", why=a.why or "", links=links)
            print(f"{ui.OK} {work.acted('linked', it)}: counts toward initiative {a.to}")
        elif act == "cancel":
            it = work.find(" ".join(a.selector), scope)
            it = work.update(it, "cancel", why=a.why or "", status="cancelled", claimed_by="", claimed_at="",
                             lock_expires="")
            print(f"{ui.OK} {work.acted('cancelled', it)}")
        elif act == "move":
            it = work.update(work.find(" ".join(a.selector), scope), "move", why=a.why or "", bucket=a.to)
            print(f"{ui.OK} {work.acted('moved', it)} → {a.to}")
            if a.to == "now" and work.now_warning(scope):
                ui.warn(work.now_warning(scope))
        elif act == "edit":
            changes = {}
            if a.title:
                changes["title"] = a.title
            if a.body_file:
                changes["body"] = Path(a.body_file).read_text()
            if a.priority_set:
                changes["priority"] = a.priority_set
            if a.due:
                changes["due"] = a.due
            if not changes:
                ui.err("nothing to change: --title, --body-file, --priority or --due")
                return 2
            it = work.update(work.find(" ".join(a.selector), scope), "edit", why=a.why or "", **changes)
            print(f"{ui.OK} {work.acted('edited', it)} v{it['meta']['version']}")
        elif act == "delegate":
            it = work.find(" ".join(a.selector), scope)
            who = a.to.lstrip("@")
            if (it["meta"].get("claimed_by") or "").startswith(work.actor()["user"] + "@"):
                it = work.release(it, why=f"handed to {who}", status="todo")
            changes = {"assignee": who}
            if a.due:
                changes["due"] = a.due
            it = work.update(it, "assign", why=a.why or f"delegated to {who}", **changes)
            msg = delegate_message(it, who, a.why or "", label)
            print(msg)
            if not a.no_copy and core.have("pbcopy"):
                core.sh(["pbcopy"], input=msg)
                print(ui.dim("\n(copied to the clipboard — paste it to your teammate)"))
            return 0
        elif act == "reconcile":
            return _task_reconcile(a, scope, label)
        elif act in ("show", "history"):
            it = work.find(" ".join(a.selector), scope)
            m = it["meta"]
            if act == "show":
                print(ui.section(f"{m['id']} · {m['title']}"))
                for k in ("type", "status", "bucket", "priority", "assignee", "claimed_by", "lock_expires", "due",
                          "version", "created", "created_by", "updated", "updated_by"):
                    if m.get(k):
                        print(ui.kv(k, m[k], 12))
                if it["body"]:
                    print("\n" + "\n".join("  " + ln for ln in it["body"].splitlines()))
            print(ui.section("History"))
            for e in it["changelog"]:
                print(f"  {ui.dim('•')} {e}")
            return 0
    except ValueError as e:
        ui.err(str(e))
        return 2
    _print_items(scope, label, getattr(a, "all", False), rebind=act == "ls")
    return 0


def cmd_plan(a):
    from . import plans
    act = a.plan_cmd or "ls"
    try:
        if act == "new":
            scope, _ = _scope(a)
            path = plans.new(scope, " ".join(a.title), kind=a.kind)
            print(f"{ui.OK} plan → {tilde(path)}  (edit it, then `dev plan revise {tilde(path)} --why \"…\"`)")
            return 0
        if act in ("revise", "approve", "supersede"):
            path = Path(os.path.expanduser(a.path)).resolve()
            if not path.is_file():
                ui.err(f"no plan at {a.path}: check the path; `dev plan ls` lists the plans")
                return 2
            status = {"approve": "approved", "supersede": "superseded"}.get(act)
            meta = plans.revise(path, a.why or ("approved" if act == "approve" else ""), status=status)
            print(f"{ui.OK} {meta['id']} v{meta['version']} · {meta['status']}")
            return 0
    except ValueError as e:
        ui.err(str(e))
        return 2
    scope = None
    if a.project or a.initiative:
        scope, _ = _scope(a)
    rows = []
    for p_ in plans.list_plans(scope):
        flag = ui.yellow(" ✎ needs `dev plan revise`") if p_["needs_revision"] else ""
        rows.append([p_["id"], p_["status"], f"v{p_['version']}", p_["title"][:50] + flag,
                     tilde(p_["path"].relative_to(HANDOFF))])
    if not rows:
        print("  no plans yet — dev plan new \"<title>\"")
        return 0
    print(ui.table(rows, ["ID", "STATUS", "VER", "TITLE", "PATH"], max_widths={4: 60}))
    return 0


def cmd_progress(a):
    from . import progress as pg
    act = a.progress_cmd
    from_agent = os.environ.get("DEVKIT_FROM_AGENT") or None  # set by the hooks inside a subagent
    if act == "start":
        names = [x.strip() for x in (a.plan or "").split(";") if x.strip()]
        try:
            run = pg.start(" ".join(a.goal), a.steps or len(names), a.estimate or "", plan=names,
                           from_agent=from_agent, mode=getattr(a, "mode", "") or "",
                           replace=getattr(a, "replace", False), why=getattr(a, "why", "") or "")
        except ValueError as e:
            ui.err(str(e))
            return 2
        if from_agent:
            print(f"{ui.OK} lane plan recorded · {' '.join(a.goal)}")
            return 0
        cal = pg.calibration(run.get("project"))
        print(f"{ui.OK} run started · {run['goal']} · {pg.mode_of(run)} mode" + (f" · {a.steps} steps" if a.steps else "")
              + (f" · estimate {pg.fmt_min(run['estimate'])}" if run["estimate"] else ""))
        hints = []
        if cal["ratio"] and run["estimate"]:
            hints.append(f"runs here take {cal['ratio']}× their estimate → plan for ~{_about(run['estimate_cal'])}")
        if cal["turn_median"]:
            hints.append(f"median turn {pg.fmt_min(cal['turn_median'])}, p90 {pg.fmt_min(cal['turn_p90'])}")
        if hints:
            print("  " + ui.dim(" · ".join(hints)))
        print(ui.dim("  update with: dev progress step \"<what finished>\" --check \"<evidence>\" --next \"<what is next>\""))
        return 0
    if act == "note":
        try:
            run = pg.note(" ".join(a.text), a.next or "", from_agent=from_agent)
        except ValueError as e:
            ui.err(str(e))
            return 2
        if from_agent:
            print(f"{ui.OK} noted in your lane · {' '.join(a.text)}")
            return 0
        print(pg.render_update(run))
        return 0
    if act in ("step", "block", "done"):
        kind = {"step": "step", "block": "blocked", "done": "done"}[act]
        check = getattr(a, "check", "") or ""
        if act == "step" and not from_agent and not check and not getattr(a, "no_check", False):
            ui.err("closing a step needs its evidence: --check \"<tests run, output, CI result>\" "
                   "(or --no-check when there is nothing to verify, which the update shows as ⚠)")
            return 2
        before = {s["name"]: s.get("state") for s in (pg.normalize(pg.load_run(*pg.current_session()) or {})
                                                     .get("plan") or [])} if not from_agent else {}
        try:
            run = pg.step(" ".join(a.text), a.next or "", kind=kind, check=check, which=getattr(a, "step", None),
                          from_agent=from_agent, force=getattr(a, "force", False), why=getattr(a, "why", "") or "",
                          approves=getattr(a, "approves", "") or "")
        except ValueError as e:
            ui.err(str(e))
            return 2
        if from_agent:
            lane = (run.get("agents") or {}).get(from_agent) or {}
            print(f"{ui.OK} {lane.get('persona') or lane.get('label') or 'lane'} · {' '.join(a.text)}")
            return 0
        print(pg.render_update(run))
        changed = [(i, s) for i, s in enumerate(run.get("plan") or [], 1) if before.get(s["name"]) != s.get("state")]
        if changed:
            print(ui.dim("  changed: " + " · ".join(f"{i}. {s['name']} {before.get(s['name']) or 'new'} → "
                                                    f"{s.get('state')}" for i, s in changed)))
        return 0
    if act in ("assign", "begin", "add", "reopen", "skip", "confirm"):
        try:
            if act == "assign":
                extra = {}
                if getattr(a, "activity", None):  # the kind of work, whatever language the step is written in
                    from . import raci
                    extra["activity"] = raci.resolve_activity(a.activity)["id"]
                run = pg.assign(a.which, " ".join(a.to), scope=getattr(a, "scope", "") or "", from_agent=from_agent,
                                why=getattr(a, "why", "") or "", allow=getattr(a, "allow", "") or "", **extra)
            elif act == "begin":
                run = pg.begin(a.which, from_agent=from_agent)
            elif act == "add":
                run = pg.add_step(pg.spec_args(a.spec), why=getattr(a, "why", "") or "", from_agent=from_agent)
                if from_agent:
                    print(f"{ui.OK} added to your lane's plan · {' '.join(a.spec)}")
                    return 0
            elif act == "confirm":
                run = pg.confirm(a.which, a.why or "", from_agent=from_agent)
            else:
                run = pg.reopen(a.which, a.why or "", skip=act == "skip", from_agent=from_agent)
        except ValueError as e:
            ui.err(str(e))
            return 2
        print(pg.render_update(run))
        return 0
    if act == "history":
        run = pg.normalize(pg.load_run(*pg.current_session()) or {})
        for h in run.get("history") or []:
            print(f"  {core.fmt_ts(h['ts'])}  {h['step']:<32} {h.get('from') or '-':>8} → {h['to']:<8} "
                  f"{ui.dim(h.get('by', ''))}  {h.get('why', '')[:80]}")
        return 0
    if act == "time":
        print(pg.report(a.days, a.project))
        return 0
    runs = pg.active_runs()
    from . import work
    others = [(t["user"] + "@" + t["host"], s_) for t in work.team() for s_ in t.get("sessions", [])
              if s_.get("progress") and not (t["user"] == work.actor()["user"] and t["host"] == work.actor()["host"])]
    print(ui.section("Progress"))
    if not runs and not others:
        print("  no runs — agents start one with dev progress start \"<goal>\" --steps N --estimate 30m")
    for r in runs:
        mark = {"running": ui.cyan("▸"), "blocked": ui.red("■"), "done": ui.green("✓")}.get(r.get("state"), "·")
        last = (r.get("updates") or [{}])[-1]
        print(f"  {mark} {ui.bold(r.get('goal', ''))}  {ui.dim(r.get('agent', '') + ' · ' + (r.get('project') or '-'))}")
        print(f"    {pg.headline(r)}" + (f" · last: {last.get('text')}" if last.get("text") else "")
              + (f" · next: {last.get('next')}" if last.get("next") else ""))
    for who, s_ in others:
        p_ = s_["progress"]
        print(f"  {ui.ACTIVE} {who} · {s_.get('agent')} · {s_.get('project')} · {p_.get('goal')} — "
              f"{p_.get('step')}/{p_.get('steps') or '?'}" + (f" · ETA ~{_about(p_['eta_min'])}" if p_.get("eta_min")
                                                               else ""))
    print()
    return 0


def _about(m) -> str:
    """An estimate or a time left, as the dashboard shows it (fmtAbout): to 5 minutes, and to the minute under 10,
    so "~" claims no more precision than an estimate has. Measured times keep progress.fmt_min."""
    from . import progress as pg
    m = max(0.0, float(m or 0))
    r = int(m + 0.5)  # halves round up, as Math.round does on the dashboard
    return pg.fmt_min(r if r < 10 else int(m / 5 + 0.5) * 5)


def cmd_report(a):
    from . import report
    text = report.build(a.days, a.initiative, a.project)
    if a.out:
        Path(a.out).expanduser().write_text(text)
        print(f"{ui.OK} {tilde(Path(a.out).expanduser())}")
    else:
        print(text)
    return 0


def cmd_delivery(a):
    from . import delivery
    if a.initiative:
        scope, repo = f"initiatives/{a.initiative}", None
    else:
        repo = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
        if a.project:
            repo = _resolve(_reg(prs=False), a.project).get("path")
        if not repo:
            ui.err("not inside a repository (or pass -p <project> / -i <initiative>)")
            return 2
        scope = delivery.scope_for(repo)
    if a.delivery_cmd == "apply":
        base = (delivery.load(scope) or (delivery.recommend(delivery.detect(repo)) if repo else {}))
        for kv in a.set or []:
            k, _, v = kv.partition("=")
            try:
                delivery.set_field(base, k, v)
            except ValueError as e:
                ui.err(str(e))
                return 2
        if not (a.why or "").strip():
            ui.err("recording a delivery policy needs the reason: --why \"…\"")
            return 2
        if repo:  # the host deletes a merged pull request's branch, as the policy says (on unless set to false)
            want = base.get("delete_branch_on_merge") is not False
            got, line = delivery.branch_cleanup(repo, want)
            print(f"{ui.OK if got is not None else ui.WARN} {line}")
            base["delete_branch_on_merge"] = got
        try:
            rec = delivery.record(scope, base, a.why or "")
        except ValueError as e:
            ui.err(str(e))
            return 2
        print(f"{ui.OK} delivery policy for {scope} recorded (v{rec['version']}) → "
              f"{tilde(delivery.policy_path(scope))}")
        if a.tasks and repo:
            for sev, gap, fix in delivery.gaps(delivery.detect(repo), rec, True):
                from . import work
                it = work.create(scope, gap[:1].upper() + gap[1:], kind="task", bucket="next" if sev != "high" else "now",
                                 priority="high" if sev == "high" else "normal",
                                 body=work.task_body("task", f"Delivery policy gap ({sev}). Fix: {fix}"),
                                 why="gap found by dev delivery")
                print(f"  {ui.OK} {it['meta']['id']} {gap}")
        return 0
    if not repo:
        p = delivery.load(scope)
        print(json.dumps(p, indent=1) if p else f"no policy recorded for {scope} — dev delivery apply -i … --set "
                                                 f"release_train=\"…\" --why \"…\"")
        return 0
    rep = delivery.report(repo, scope)
    if a.json:
        print(json.dumps(rep, indent=1, ensure_ascii=False))
        return 0
    d, rec, recm = rep["detected"], rep["recorded"], rep["recommended"]
    print(ui.section(f"Delivery · {scope}"))
    rows = [("kind", d["kind"]), ("versioning", f"{d['versioning'] or 'none'}" + (f" (latest {d['latest_tag']})" if d.get("latest_tag") else "")),
            ("version file", f"{d['version_source']} = {d['version']}" if d.get("version_source") else "none"),
            ("release tools", ", ".join(d["release_tools"]) or "none"), ("changelog", d["changelog"] or "none"),
            ("CI", ", ".join(d["ci"]) or "none"), ("test gate", "; ".join(d["test_gate"]) or "none"),
            ("branches", d["branch_model"]), ("publishes", ", ".join(d["publish"]) or "nothing"),
            ("deploys", ", ".join(d["deploy"]) or "nothing")]
    print(ui.dim("  detected in the repository"))
    for k, v in rows:
        print(ui.kv(k, v))
    pol = rec or recm
    print(ui.dim("\n  " + (f"recorded policy v{rec['version']} ({rec.get('updated_by')})" if rec else
                            "recommended policy (not recorded yet)")))
    for k in ("versioning", "tag_format", "release_flow", "bump", "changelog", "test_gate"):
        v = pol.get(k)
        print(ui.kv(k, "; ".join(v) if isinstance(v, list) else (v or "-")))
    if rep["gaps"]:
        print(ui.section(f"Gaps ({len(rep['gaps'])})"))
        color = {"high": ui.red, "medium": ui.yellow, "low": ui.dim}
        for sev, gap, fix in rep["gaps"]:
            print(f"  {color[sev](sev.ljust(6))} {gap}\n         {ui.dim('fix: ' + fix)}")
    else:
        print(f"\n  {ui.OK} no gaps")
    if not rec:
        print(ui.dim("\n  record it: dev delivery apply --why \"…\" [--set versioning=semver] [--tasks]"))
    return 0


def _lesson_scope(a):
    if getattr(a, "global_", False):
        return "global"
    if getattr(a, "initiative", None):
        return f"initiatives/{a.initiative}"
    if getattr(a, "project", None):
        return _resolve(_reg(prs=False), a.project)["id"]
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        return "global"
    from . import delivery
    return delivery.scope_for(top)


def _lesson_anchors(a) -> dict:
    """The anchors given on the command line: where a lesson applies, in terms that mean the same in any language."""
    return {"files": getattr(a, "file", None) or [], "commands": getattr(a, "command", None) or [],
            "activities": getattr(a, "activity", None) or [], "errors": getattr(a, "error", None) or [],
            "stack": getattr(a, "stack", None) or []}


def _lesson_author() -> str:
    """Who records a lesson, as recall names it: the developer by their git user name in their own terminal; the
    crew member whose lane runs the command, as `<Role> (<Name>)` (its lane as `dev commit` finds it: DEVKIT_FROM_AGENT,
    else the call the hooks hold); the tech lead in an agent's main session. '' when it cannot be told (a script, or
    a lane the run does not know): the lesson then names the agent or the user and host, as before."""
    if core.set_by_developer():
        return core.gitout(Path.home(), "config", "--get", "user.name", timeout=10).strip()
    from . import crew, progress, work
    try:
        aid = progress._as_lane(os.environ.get("DEVKIT_FROM_AGENT") or None)
        if aid:
            _, lane = progress.lane_of(*progress.current_session(), aid)
            mem = crew._for_lane(lane) if lane else None
            return crew.who(mem) if mem else ""
    except (OSError, ValueError) as e:  # a busy run record too (StateBusy): who wrote it never stops the lesson
        print(ui.dim(f"  could not tell which crew lane runs this ({type(e).__name__}): the lesson names the agent"),
              file=sys.stderr)
        return ""
    return crew.who("main") if work.actor()["agent"] != "human" else ""


def _lesson_scoped(a) -> bool:
    """Did the command name a scope (-p, -i or --global)? Without one, `dev lesson lint` checks every scope."""
    return bool(getattr(a, "project", None) or getattr(a, "initiative", None) or getattr(a, "global_", False))


def _lesson_root(scope: str):
    """The checkout of a project scope on this Mac, against which `dev lesson lint` checks file anchors; None for the
    global and initiative scopes, and for a project not cloned here."""
    if not scope or scope == "global" or scope.startswith("initiatives/"):
        return None
    path = core.canonical_path(scope)
    return path if path.is_dir() else None


LINT_ORDER = ("no-cause", "missing", "quarantined", "broad", "wrong", "no-file")  # what recall cannot show comes first


def _lesson_lint(a, lessons) -> int:
    """`dev lesson lint`: lessonlint.lint over the active lessons (one scope, or every scope), each project's file
    anchors checked against its own checkout, printed by kind with the command that fixes each finding. Exit 1 when
    something needs fixing."""
    from . import lessonlint
    items = lessons.all_lessons(_lesson_scope(a) if _lesson_scoped(a) else None)
    scopes = {}
    for x in items:
        scopes.setdefault(x["meta"].get("scope") or "", []).append(x)
    found = [f for scope, group in scopes.items() for f in lessonlint.lint(group, _lesson_root(scope))]
    if not found:
        print(f"{ui.OK} {len(items)} lesson{'s' if len(items) != 1 else ''}: nothing to fix")
        return 0
    rank = {k: i for i, k in enumerate(LINT_ORDER)}
    print(ui.section(f"Lesson lint · {len(found)} finding{'s' if len(found) != 1 else ''} in "
                     f"{len({f['id'] for f in found})} of {len(items)} lessons"))
    for kind in sorted({f["kind"] for f in found}, key=lambda k: (rank.get(k, len(rank)), k)):
        mine = [f for f in found if f["kind"] == kind]
        print(ui.section(f"{kind} · {len(mine)}"))
        for f in mine:
            print(f"  {ui.WARN} {f['id']}  {f['problem']}\n      {ui.dim('fix: ' + f['fix'])}")
    return 1


def _lesson_eval(a, lessons) -> int:
    """`dev lesson eval`: for each repeat this Mac recorded (`dev lesson recur`) in the window, whether recall showed
    the lesson in that session before it (lessonevents.coverage)."""
    from . import lessonevents
    c = lessonevents.coverage(a.days)
    print(ui.section(f"Recall coverage · last {c['days']} days"))
    if not c["recurrences"]:
        print(f"  no repeat recorded here in the last {c['days']} days (a repeat is a `dev lesson recur` on this Mac)")
        return 0
    n = c["recurrences"]
    rate = f" ({round(c['coverage'] * 100)}%)" if c["coverage"] is not None else ""
    print(f"  {n} repeat{'s' if n != 1 else ''} · {c['shown_before']} shown before{rate}"
          + (f" · {c['unknown']} with no session recorded" if c["unknown"] else ""))
    titles = {x["meta"]["id"]: x["meta"]["title"] for x in lessons.all_lessons() + lessons.all_lessons(archived=True)}
    newest = sorted(c["items"], key=lambda it: -it["ts"])
    for it in newest[:EVAL_SHOWN]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(it["ts"]))
        if it["shown_before"] is None:
            mark, how = ui.INFO, "no session recorded"
        elif it["shown_before"]:
            mark, how = ui.OK, "shown before by " + ", ".join(it["channels"])
        else:
            mark, how = ui.FAIL, "not shown before"
        print(f"  {mark} {it['id']}  {when}  {how}" + (f"  {ui.dim(titles[it['id']])}" if it["id"] in titles else ""))
    if len(newest) > EVAL_SHOWN:
        print(ui.dim(f"  … and {len(newest) - EVAL_SHOWN} older repeats (--days narrows the window)"))
    return 0


EVAL_SHOWN = 25  # repeats `dev lesson eval` lists, newest first; the totals count them all


def _lesson_review(lessons) -> int:
    """`dev lesson review`: what to act on, most urgent first (lessons missed twice after being shown, preventions
    that failed, recurring lessons, held lessons, stale ones), then how often shown lessons were heeded per channel
    over 30 days and the lessons missed most (lessonevents.rates)."""
    from . import lessonevents
    r = lessons.review()
    held = r.get("held")
    if held is None:  # a review() that does not list held lessons: the holds its lessons carry in their front matter
        held = [{"lesson": x, "reason": x["meta"]["quarantine"]} for x in lessons.all_lessons()
                if x["meta"].get("quarantine")]
    use = lessons.usage()
    print(ui.section(f"Lessons · {r['total']} · " + ", ".join(f"{v} {k}" for k, v in sorted(r["counts"].items()))))

    def named(x) -> str:
        return f"{x['meta']['title']}  {ui.dim(x['meta']['scope'])}"
    if r["automate"]:
        print(ui.section("Missed twice or more after being shown — automate these"))
        for x in r["automate"]:
            n = int((use.get(x["meta"]["id"]) or {}).get("missed") or 0)
            print(f"  {ui.FAIL} {x['meta']['id']} missed {n}× {named(x)}")
    if r["prevention_failed"]:
        print(ui.section("Preventions that failed — fix or strengthen them"))
        for f in r["prevention_failed"]:
            ref = f" ({f['ref']})" if f["ref"] else ""
            print(f"  {ui.FAIL} {f['lesson']['meta']['id']} prevented by {f['by']}{ref}, repeated {f['days']} days "
                  f"later · {named(f['lesson'])}")
    if r["recurring"]:
        print(ui.section("Recurring and not prevented — automate these"))
        for x in r["recurring"]:
            print(f"  {ui.WARN} {x['meta']['id']} ×{x['meta']['occurrences']} {named(x)}")
    if held:
        print(ui.section("Held by the screen — the developer releases them"))
        for h in held:
            x = h["lesson"]
            print(f"  {ui.WARN} {x['meta']['id']} {named(x)}\n      {ui.dim('held: ' + h['reason'])}"
                  f"\n      {ui.dim('dev lesson release ' + x['meta']['id'] + ' --why “…” (the developer only)')}")
    if r["stale"]:
        print(ui.section("Not seen for 4 months — retire or confirm"))
        for x in r["stale"]:
            print(f"  {ui.dim('·')} {x['meta']['id']} {x['meta']['title']}  {ui.dim('last ' + x['meta']['last_seen'])}")
    if not any((r["automate"], r["prevention_failed"], r["recurring"], held, r["stale"])):
        print(f"  {ui.OK} nothing to act on")
    rates = lessonevents.rates(30)
    print(ui.section(f"Shown lessons heeded · last {rates['days']} days"))
    for ch, c in rates["channels"].items():
        if not (c["shown"] or c["heeded"] or c["missed"]):
            print(f"  {ch:<8} nothing shown")
            continue
        pct = f"{round(c['heeded_rate'] * 100)}% heeded" if c["heeded_rate"] is not None else "none resolved yet"
        print(f"  {ch:<8} {c['shown']} shown · {c['heeded']} heeded · {c['missed']} missed · {pct}")
    if rates["most_missed"]:
        print("  missed most: " + ", ".join(f"{m['id']} ×{m['missed']}" for m in rates["most_missed"][:5]))
    return 0


def _lesson_prevent(a, les, lessons) -> int:
    """`dev lesson prevent`: record what prevents a lesson (`--ref`), or, with `--by guard --match|--error`, propose a
    lesson rule (lessonrules.propose, which refuses an unsafe or untested pattern with its reason) and record that
    rule as the prevention, so the nightly check finds it gone if the rule is removed."""
    rule_given = bool(a.match or a.error)
    if rule_given and a.by != "guard":
        raise ValueError("--match and --error propose a lesson rule the guard enforces: use them with --by guard")
    if a.block and not rule_given:
        raise ValueError("--block asks for a lesson rule that refuses: give its pattern with --match or --error")
    if rule_given and a.ref:
        raise ValueError("give what prevents it (--ref) or a lesson rule to propose (--match or --error), not both")
    if not rule_given:
        if not (a.ref or "").strip():
            raise ValueError("name what prevents it: --ref \"…\"" + (
                "; or propose a lesson rule: --match \"<command words>\" or --error \"<output text>\""
                if a.by == "guard" else ""))
        les = lessons.prevent(les, a.by, a.ref, a.why or "")
        print(f"{ui.OK} {les['meta']['id']} prevented by {a.by}: {a.ref}")
        return 0
    if not (a.why or "").strip():
        raise ValueError("say why this prevents it: --why \"…\"")
    from . import lessonrules
    rule = lessonrules.propose(les, match=a.match or "", error=a.error or "", block=a.block)
    les = lessons.prevent(les, "guard", rule["id"], a.why)
    print(f"{ui.OK} {les['meta']['id']} prevented by lesson rule {rule['id']} ({rule['mode']}), in "
          f"lessons/rules/{rule['scope']}.json")
    print(ui.dim("  warn: the guard adds the lesson to each matching call and counts a repeat after the warning; only "
                 "the developer's typed `grant lesson-block <lesson>`, then `dev lesson prevent --by guard <lesson> "
                 "--block` in a terminal, makes it refuse" if rule["mode"] == "warn" else
                 "  block: on this machine the guard refuses each matching call with the lesson's prevention as the "
                 "reason; elsewhere it warns until that developer grants it too"))
    return 0


# A hook's denial as the agent's tool output shows it, or the guard's lesson-rule refusal (gates.DENIAL_RE reads the
# whole tool result; an anchor is a piece of one).
_HOOK_DENIAL = re.compile(r"\b(?:PreToolUse|PermissionRequest)\b[^\n]*\bhook (?:blocking )?error\b", re.I)
_GUARD_TEXTS: list = []


def _guard_texts() -> list:
    """The text of the guard's refusals as its source holds it: every string literal of guard.py of 12 characters or
    more but the docstrings (an f-string's literal parts each on their own), whitespace folded, case-folded."""
    if not _GUARD_TEXTS:
        import ast
        from . import guard
        tree = ast.parse(Path(guard.__file__).read_text())
        docs = set()
        for n in ast.walk(tree):
            body = getattr(n, "body", None)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and body and \
                    isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docs.add(id(body[0].value))
        _GUARD_TEXTS.extend(" ".join(n.value.split()).casefold() for n in ast.walk(tree)
                            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
                            and len(n.value) >= 12)
    return _GUARD_TEXTS


def _refusal_anchor(error: str) -> bool:
    """Is this error anchor a piece of a guard refusal: the hook's denial line, or text of one of the guard's own
    refusal messages?"""
    if _HOOK_DENIAL.search(error or ""):
        return True
    text = " ".join(str(error or "").split()).casefold().strip(" .,:;!?`\"'")
    try:
        return len(text) >= 12 and any(text in t for t in _guard_texts())
    except (OSError, SyntaxError, ValueError):
        return False  # the hint is advice: without the guard's source, give none


def _registry_hint(scope: str, anchored: dict) -> list:
    """For a lesson outside devkit's own repository whose anchors are guard refusals or `dev` commands: the lines that
    point it to devkit's bug registry, which fixes devkit's friction once for every devkit user. [] otherwise."""
    if scope == core.DEVKIT_REPO:
        return []
    refusals = [e for e in anchored.get("errors") or [] if _refusal_anchor(e)]
    devs = [c for c in anchored.get("commands") or [] if os.path.basename((c.split() or [""])[0]) == "dev"]
    if not (refusals or devs):
        return []
    what = f"the guard's refusal “{refusals[0]}”" if refusals else f"the devkit command `{devs[0]}`"
    lines = [f"  {ui.WARN} this lesson is about devkit itself ({what}). devkit's bug registry fixes that once for "
             f"every devkit user:",
             f"      dev bug report --guard <refusal id>   a refusal that should not have happened (with no id: this "
             f"session's last refusal)"]
    if devs:
        lines.append("      dev bug report --title \"…\"            a dev command that misbehaves without an error")
    return lines


def _anchor_flags(x):
    x.add_argument("--file", action="append", help="a file or glob it applies to (repeat)")
    x.add_argument("--command", action="append", help="the command words it applies to, e.g. \"git push --force\"")
    x.add_argument("--activity", action="append", help="the kind of work it applies to: an id from the crew's RACI")
    x.add_argument("--error", action="append", help="an error signature: text the tool output shows when it happens")
    x.add_argument("--stack", action="append", help="the stack it applies to: python, java, angular, …")


def cmd_lesson(a):
    from . import lessons
    act = a.lesson_cmd or "ls"
    try:
        if act == "add":
            scope = _lesson_scope(a)
            les = lessons.add(scope, " ".join(a.title), kind=a.kind, severity=a.severity, happened=a.happened or "",
                              cause=a.cause or "", fix=a.fix or "", prevention=a.prevent or "",
                              detection=a.detect or "", evidence=a.evidence or [], tags=a.tag or [], why=a.why or "",
                              retire_when=a.retire_when or "", new=a.new, author=_lesson_author(),
                              **_lesson_anchors(a))
            print(f"{ui.OK} {les['meta']['id']} recorded in {scope} → {tilde(les['path'])}")
            if les["meta"].get("quarantine"):
                print(f"  {ui.WARN} held, not recalled: {les['meta']['quarantine']}. Only the developer releases it: "
                      f"dev lesson release {les['meta']['id']} --why …")
            if not any(_lesson_anchors(a).values()):
                print(ui.dim("  no anchors: only the session start recalls it. Add where it applies: dev lesson anchor "
                             + les["meta"]["id"] + " --file … --command … --activity … --error … --why …"))
            for line in _registry_hint(scope, lessons.anchors(les)):
                print(line)
            print(ui.dim("  automate it when you can: dev lesson prevent " + les["meta"]["id"] +
                         " --by test|lint|guard|skill|ci --ref … --why …"))
            return 0
        if act == "lint":
            return _lesson_lint(a, lessons)
        if act == "eval":
            return _lesson_eval(a, lessons)
        if act == "supersede":
            old = lessons.supersede(lessons.find(a.old), lessons.find(a.by), a.why or "")
            new = old["meta"]["superseded_by"]
            print(f"{ui.OK} {old['meta']['id']} superseded by {new}: recall shows only {new}")
            return 0
        if act == "release":
            les = lessons.release(lessons.find(a.lesson), a.why or "")
            print(f"{ui.OK} {les['meta']['id']} released: recall can show it again")
            return 0
        if act == "anchor":
            les = lessons.set_anchors(lessons.find(" ".join(a.selector)), a.why or "", **_lesson_anchors(a))
            print(f"{ui.OK} {les['meta']['id']} anchored · " + " · ".join(
                f"{k} {', '.join(v)}" for k, v in lessons.anchors(les).items() if v))
            return 0
        if act == "restore":
            les = lessons.restore(" ".join(a.selector), a.why or "")
            print(f"{ui.OK} {les['meta']['id']} restored to {les['meta']['scope']}")
            return 0
        if act in ("show", "recur", "prevent", "retire"):
            les = lessons.find(" ".join(a.selector))
            lessons.track([les["meta"]["id"]], "recur" if act == "recur" else "lookup")
            if act == "recur":
                les = lessons.recur(les, a.evidence or "", a.why or "")
                print(f"{ui.OK} {les['meta']['id']} happened {les['meta']['occurrences']}× — "
                      + ("automate the prevention now (dev lesson prevent)" if les["meta"]["occurrences"] >= 2 else "noted"))
                return 0
            if act == "prevent":
                return _lesson_prevent(a, les, lessons)
            if act == "retire":
                les = lessons.retire(les, a.why or "")
                print(f"{ui.OK} {les['meta']['id']} retired")
                return 0
            m = les["meta"]
            print(ui.section(f"{m['id']} · {m['title']}"))
            for k in ("scope", "kind", "severity", "status", "occurrences", "prevention", "prevention_ref",
                      "first_seen", "last_seen", "version"):
                if m.get(k) not in (None, "", []):
                    print(ui.kv(k, m[k], 14))
            for sec, text in les["sections"].items():
                if text and text != "_—_":
                    print(f"\n  {ui.bold(sec)}\n" + "\n".join("    " + ln for ln in text.splitlines()))
            print(ui.section("History"))
            for e in les["changelog"]:
                print(f"  {ui.dim('•')} {e}")
            return 0
        if act == "scan":
            pid, repo = None, core.gitout(os.getcwd(), "rev-parse", "--show-toplevel") or None
            if repo:
                from . import delivery
                pid = delivery.scope_for(repo)
            sig = lessons.signals(pid, repo, a.days)
            print(ui.section(f"Signals worth a lesson · last {a.days} days" + (f" · {pid}" if pid else "")))
            if not sig:
                print(f"  {ui.OK} nothing new")
            for kind, detail, cmd in sig:
                print(f"  {ui.WARN} {kind:<7} {detail}\n          {ui.dim(cmd)}")
            return 0
        if act == "review":
            return _lesson_review(lessons)
    except ValueError as e:
        ui.err(str(e))
        return 2
    # ls
    items = lessons.search(a.search) if getattr(a, "search", None) else lessons.all_lessons(
        None if a.all else _lesson_scope(a))
    if not a.all and not getattr(a, "search", None) and _lesson_scope(a) != "global":
        items += lessons.all_lessons("global")
    items = [x for x in items if a.all or x["meta"].get("status") != "retired"]
    print(ui.section(f"Lessons · {len(items)}"))
    if not items:
        print("  none yet — record one after a fix, a revert or a repeated mistake: dev lesson add \"…\" --happened \"…\" "
              "--cause \"…\" --prevent \"…\" --file <glob> --command \"<command words>\" --error \"<output text>\"")
    for x in sorted(items, key=lessons._score, reverse=True):
        m = x["meta"]
        st = {"open": ui.yellow("open"), "prevented": ui.green(f"prevented·{m.get('prevention')}"),
              "retired": ui.dim("retired")}.get(m.get("status"), m.get("status"))
        print(f"  {ui.dim(m['id'])} {st:<20} ×{m['occurrences']} {m['title']}  {ui.dim(m['scope'] + ' · ' + m['severity'])}")
    return 0


def cmd_bug(a):
    """`dev bug`: devkit's own bugs, from this Mac's copy of the public registry (bugs.py, docs/bug-registry.md).
    An agent session sees only the fields of the registry's bot-written records and their generated titles."""
    from . import bugs
    act = getattr(a, "bug_cmd", None) or "ls"
    try:
        return {"ls": _bug_ls, "show": _bug_show, "search": _bug_search, "report": _bug_report,
                "detour": _bug_detour, "top": _bug_top, "sync": _bug_sync, "pending": _bug_pending,
                "send": _bug_send, "drop": _bug_drop, "confirm": _bug_confirm, "share": _bug_share}[act](a, bugs)
    except ValueError as e:
        ui.err(str(e))
        return 1


def _bug_session() -> tuple:
    from . import progress as pg
    try:
        agent, sid = pg.current_session()
        return str(agent or ""), str(sid or "")
    except Exception:  # no session to name: the ledger and this session's occurrences are then empty
        return "", ""


def _bug_cache_line(bugs, cache: dict, shown: int) -> str:
    if not cache.get("ts"):
        return "devkit's bug registry has not been read on this Mac yet: `dev bug sync` reads it."
    c = bugs.consent()
    line = f"devkit's bug registry · {shown} bug{'s' if shown != 1 else ''} · read {ui.rel_time(cache['ts'])}"
    if c["share"] == "off":
        line += " · sharing is off: reports and votes are kept locally"
    return line


def _bug_rows(bugs, entries: list, now: float) -> list:
    out = []
    for e in entries:
        rec = e.get("record") or {}
        n = bugs.people(rec, now)
        votes = f"{n['all']} {'person' if n['all'] == 1 else 'people'}" + \
            (f", {n['blocked']} blocked" if n["blocked"] else "")
        state = "fixed in " + rec["fixed_in"] if rec.get("fixed_in") else str(e.get("state") or rec.get("status"))
        out.append(f"  #{e.get('number')}  {rec.get('fingerprint')}  {bugs._title(rec)}")
        out.append(ui.dim(f"       score {bugs.score(rec, now):.1f} · {votes} · {bugs.detour_status(rec)} · {state}"))
    return out


def _bug_ls(a, bugs):
    if getattr(a, "local", False):
        return _bug_local(bugs)
    cache, now = bugs.load_cache(), time.time()
    mine = {o.get("fingerprint") for o in bugs.occurrences()} if getattr(a, "mine", False) else None
    entries = []
    for fp, e in cache["bugs"].items():
        rec = e.get("record") or {}
        if (getattr(a, "open", False) and e.get("state") != "open") or \
                (getattr(a, "fixed", False) and not (rec.get("fixed_in") or e.get("state") == "closed")) or \
                (getattr(a, "kind", None) and rec.get("kind") != a.kind) or (mine is not None and fp not in mine):
            continue
        entries.append(e)
    entries.sort(key=lambda e: (-bugs.score(e.get("record") or {}, now), e.get("number") or 0))
    print(_bug_cache_line(bugs, cache, len(entries)))
    print("\n".join(_bug_rows(bugs, entries, now)))
    return 0


def _bug_local(bugs):
    """`dev bug ls --local`: the failures recorded on this Mac, newest first, with their bug number when known."""
    groups = {}
    for o in bugs.occurrences():
        groups.setdefault(o.get("fingerprint"), []).append(o)
    known = bugs.load_cache()["bugs"]
    print(f"On this Mac · {len(groups)} devkit failure{'s' if len(groups) != 1 else ''} recorded")
    for fp, occ in sorted(groups.items(), key=lambda g: -(g[1][-1].get("ts") or 0)):
        last = occ[-1]
        what = f"dev {last['command']}" if last.get("command") else last.get("event") or \
            " on ".join(x for x in (last.get("refusal_id"), last.get("word")) if x) or ""
        times = f"{len(occ)} time{'s' if len(occ) != 1 else ''}, last {ui.rel_time(last.get('ts'))}"
        where = f"#{known[fp].get('number')}" if fp in known else "not in the registry"
        print(f"  {fp}  {last.get('kind') or '?'}  {what}  {times} · {where}" +
              (" · private (security)" if last.get("security") else ""))
    if groups:
        print(ui.dim("  dev bug show <fingerprint> for one; dev bug report <fingerprint> files it"))
    return 0


def _bug_show(a, bugs):
    fp = bugs.find_bug(a.bug)
    local = bugs.occurrences(fp or a.bug)
    if fp is None:
        if not local:
            raise ValueError(f"no bug {a.bug} in the cache or on this Mac: `dev bug sync` refreshes the cache")
        last = local[-1]
        print(f"Bug {a.bug} · not in the registry yet · {last.get('kind')}")
        print(f"  This Mac   {len(local)} occurrence{'s' if len(local) != 1 else ''}, last "
              f"{ui.rel_time(last.get('ts'))}")
        print(ui.dim(f"  dev bug report {a.bug} files it"))
        return 0
    entry, now = bugs.load_cache()["bugs"][fp], time.time()
    person = core.set_by_developer()
    rec = entry["record"] if person else bugs.agent_view(entry["record"])
    n = bugs.people(entry["record"], now)
    print(f"Bug #{entry.get('number')} · {fp} · {bugs._title(rec)}")
    print(f"  Kind       {rec.get('kind')} · component {rec.get('component')} · status {rec.get('status')} "
          f"(issue {entry.get('state')})")
    print(f"  Versions   {', '.join(rec.get('versions') or []) or '—'} · first {rec.get('first_version') or '—'} · "
          f"fixed in {rec.get('fixed_in') or '—'}")
    print(f"  Votes      {n['all']} {'person' if n['all'] == 1 else 'people'} in 30 days: " +
          ", ".join(f"{n[k]} {k}" for k in bugs.IMPACT) + f" · score {bugs.score(entry['record'], now):.1f}")
    detours = rec.get("detours") or []
    lines = [f"{d.get('id')} · {d.get('trust') or bugs._trust(d)} · {bugs._detour_text(d)}" for d in detours]
    if not person and rec.get("proposed"):
        k = rec["proposed"]
        lines.append(f"{k} proposed detour{'s' if k != 1 else ''} (a person sees {'them' if k != 1 else 'it'} with "
                     f"`dev bug show` in a terminal)")
    for i, ln in enumerate(lines or ["none"]):
        print(f"  {'Detours' if i == 0 else '':<10} {ln}")
    if local:
        print(f"  This Mac   {len(local)} occurrence{'s' if len(local) != 1 else ''}, last "
              f"{ui.rel_time(local[-1].get('ts'))}")
    related = _bug_lessons(entry["record"])
    if related:
        print(f"  Lessons    {', '.join(related)}")
    print(f"  Issue      {bugs.issue_url(entry['number'])}" if isinstance(entry.get("number"), int) else "")
    return 0


def _bug_lessons(rec: dict) -> list:
    """Lessons that name the bug's fingerprint, refusal id or `dev` command, as `L-… title`."""
    try:
        from . import lessons
        keys = [rec.get("fingerprint")] + [(rec.get("report") or {}).get(k) for k in ("refusal_id", "command")]
        found = {}
        for k in (k for k in keys if k):
            for x in lessons.search(k):
                found.setdefault(x["meta"]["id"], f"{x['meta']['id']} {x['meta']['title'][:60]}")
        return list(found.values())[:5]
    except Exception:  # lessons are a hint here: a bug shows without them
        return []


def _bug_search(a, bugs):
    words = [w.lower() for w in a.words]
    cache, now = bugs.load_cache(), time.time()
    hits = [e for fp, e in cache["bugs"].items()
            if all(w in bugs._title(e.get("record") or {}).lower() or fp.startswith(w) for w in words)]
    hits.sort(key=lambda e: (-bugs.score(e.get("record") or {}, now), e.get("number") or 0))
    print(f"{len(hits)} bug{'s' if len(hits) != 1 else ''} match {' '.join(a.words)!r}")
    print("\n".join(_bug_rows(bugs, hits, now)))
    return 0


def _bug_queued(bugs, r: dict, what: str):
    """How a queued item leaves: held for review, private, or with the next sync; and the consent it still needs."""
    if r["security"]:
        print(f"  Held for the developer: it goes only to private vulnerability reporting, never to public issues. "
              f"`dev bug pending` shows it; `dev bug send {r['id']}` sends it.")
    elif r["held"]:
        print(f"  Held for the developer's review: `dev bug pending` shows it exactly as it would be sent; "
              f"`dev bug send {r['id']}` sends it.")
    else:
        print(f"  The {what} goes out with the next `dev bug sync` (the hourly sync runs it too).")
    share = bugs.consent()["share"]
    if share is None:
        print("  Posting needs the developer's consent once: ask them to type `grant bug-share on` in the chat, or "
              "to run `dev bug share on` in a terminal.")
    elif share == "off":
        print("  Sharing is off: it is kept locally.")
    elif share == "reports" and what == "vote":
        print("  Sharing is set to reports: votes are kept locally.")


def _bug_report(a, bugs):
    agent, session = _bug_session()
    r = bugs.file_report(a.fingerprint, title=a.title, refusal_id=a.guard, security=a.security, session=session,
                         agent=agent)
    print(f"{ui.OK} queued {r['type']} {r['id']} for devkit bug {r['fingerprint']}")
    _bug_queued(bugs, r, r["type"])
    if not core.set_by_developer():
        print("  Stop this piece of work and ask the developer; independent work can go on.")
    return 0


def _bug_detour(a, bugs):
    steps = [bugs.parse_hint(v) if kind == "hint" else {kind: v} for kind, v in a.steps or []]
    if not steps:
        raise ValueError("a detour has 1 to 5 steps: --hint, --dev or --text")
    r = bugs.file_detour(a.fingerprint, steps, _bug_session()[1])
    print(f"{ui.OK} queued detour {r['id']} for devkit bug {a.fingerprint} ({r['why']})")
    _bug_queued(bugs, r, "detour")
    return 0


def _bug_top(a, bugs):
    found = bugs.top(limit=10)
    if not found:
        print("No open devkit bugs in the cache." + ("" if bugs.load_cache().get("ts") else
                                                     " `dev bug sync` reads the registry."))
        return 0
    print(f"Hotfix recommendations · {len(found)} open bug{'s' if len(found) != 1 else ''} by score")
    for i, b in enumerate(found, 1):
        print(f"  {i}. #{b['number']}  score {b['score']:.1f}  {b['title']} — {b['reason']}")
    scope = found[:5]
    print("Suggested hotfix scope: " + ", ".join(f"#{b['number']}" for b in scope))
    if not getattr(a, "task", False):
        print(ui.dim("  dev bug top --task adds them as work items with the issue link"))
        return 0
    from . import work
    have = {ln for it in work.all_items(core.DEVKIT_REPO) for ln in it["meta"].get("links") or []}
    for b in scope:
        if b["url"] in have:
            print(ui.dim(f"  #{b['number']} already has a work item"))
            continue
        why = f"devkit bug #{b['number']} ({b['title']}): {b['reason']}. Registry issue: {b['url']}"
        it = work.create(core.DEVKIT_REPO, f"Fix devkit bug #{b['number']}: {b['title']}"[:120], kind="bug",
                         links=[b["url"]], why=why,
                         body=work.task_body("bug", why, [f"A failing test reproduces devkit bug #{b['number']} and "
                                                          f"now passes",
                                                          f"The fix's pull request says `Fixes #{b['number']}`"]))
        print(f"{ui.OK} {it['meta']['id']} added for #{b['number']}")
    return 0


def _bug_sync(a, bugs):
    r = bugs.sync()
    f, s = r["fetch"], r["flush"]
    if f.get("ok"):
        print(f"{ui.OK} registry read: {f['bugs']} bug{'s' if f['bugs'] != 1 else ''} in the cache")
    else:
        print(f"{ui.FAIL} registry not read ({f.get('error')}): the cache from "
              f"{ui.rel_time(bugs.load_cache().get('ts')) if bugs.load_cache().get('ts') else 'never'} stays")
    counts = ", ".join(f"{s.get(k, 0)} {k}" for k in ("sent", "held", "failed", "skipped"))
    print(f"{ui.OK if not s.get('error') else ui.WARN} outbox: {counts}" +
          (f" · {s['error']}" if s.get("error") else ""))
    return 0 if f.get("ok") else 1


def _bug_pending(a, bugs):
    items = bugs.pending()
    if not items:
        print("Nothing waits for review.")
        return 0
    print(f"{len(items)} item{'s' if len(items) != 1 else ''} held for review, each exactly as it would be sent:")
    for it in items:
        where = "private vulnerability reporting" if it.get("security") else "a public issue comment"
        state = "approved, goes with the next sync" if it.get("approved") else f"dev bug send {it.get('id')}"
        print(f"\n  {it.get('id')} · {it['payload'].get('type')} · {it['payload'].get('fingerprint')} · to {where} · "
              f"{state}" + (f" · {it['problem']}" if it.get("problem") else ""))
        print(bugs.render_payload(it["payload"]))
    return 0


def _bug_send(a, bugs):
    try:
        bugs.approve(a.id)
    except KeyError:
        raise ValueError(f"no outbox item {a.id}: `dev bug pending` lists them") from None
    print(f"{ui.OK} item {a.id} approved as printed by `dev bug pending`; a change to it now stops it")
    s = bugs.flush()
    print(f"{ui.OK if not s.get('error') else ui.WARN} outbox: " +
          ", ".join(f"{s.get(k, 0)} {k}" for k in ("sent", "held", "failed", "skipped")) +
          (f" · {s['error']}" if s.get("error") else ""))
    return 0


def _bug_drop(a, bugs):
    try:
        bugs.drop(a.id)
    except KeyError:
        raise ValueError(f"no outbox item {a.id}: `dev bug pending` lists them") from None
    print(f"{ui.OK} item {a.id} dropped")
    return 0


def _bug_confirm(a, bugs):
    item = bugs.confirm(a.bug, a.detour)
    print(f"{ui.OK} queued confirmation {item} of detour {a.detour} on bug {a.bug}; it goes out with the next "
          f"`dev bug sync` and counts when you are an owner or member other than the detour's author")
    return 0


def _bug_share(a, bugs):
    if a.setting == "off":
        bugs.set_consent("off", "")
        print(f"{ui.OK} sharing is off: reports, votes and detours are kept on this Mac")
        return 0
    try:
        login = bugs.default_client().login()
    except Exception as e:  # gh missing or offline: consent needs the account it is for
        raise ValueError(f"cannot tell which GitHub account would post ({type(e).__name__}): run `gh auth login`, "
                         f"then try again") from None
    if not login:
        raise ValueError("no GitHub login to post as: run `gh auth login`, then try again")
    print(bugs.consent_text(login))
    bugs.set_consent(a.setting, login)
    print(f"{ui.OK} sharing is {'on' if a.setting == 'on' else 'on for reports and detours, without votes'} for "
          f"`{login}`")
    return 0


def cmd_sleep(a):
    from . import sleep
    sleep.run(dry_run=a.dry_run, quiet=a.quiet)
    return 0


def cmd_jobs(a):
    from . import jobs as J
    act = a.jobs_cmd or "ls"
    try:
        if act == "exec":
            return J.execute(a.name)
        if act == "run":
            print(f"{ui.ARROW} running {a.name} now (log: {tilde(J.LOGS / (a.name + '.log'))})")
            rc = J.execute(a.name)
            r = J.last_run(a.name) or {}
            print(f"{ui.OK if rc == 0 else ui.FAIL} {a.name} · exit {rc} · {r.get('duration', 0):.0f}s · {r.get('summary', '')}")
            return rc
        if act in ("enable", "disable"):
            J.set_schedule(a.name, enabled=(act == "enable"), why=a.why or "")
            print(f"{ui.OK} {a.name} {act}d")
            return 0
        if act == "set":
            j = J.set_schedule(a.name, every=a.every, at=a.at, weekly=a.weekly, why=a.why or "")
            print(f"{ui.OK} {a.name}: {J.schedule_text(j)}")
            return 0
        if act == "install":
            for state, name, detail in J.install():
                print(f"  {ui.OK if state in ('ok', 'loaded', 'legacy', 'disabled') else ui.FAIL} {name:<9} {state:<9} {ui.dim(detail)}")
            return 0
        if act == "logs":
            f = J.LOGS / f"{a.name}.log"
            lines = f.read_text(errors="ignore").splitlines()[-a.lines:] if f.exists() else ["(no runs yet)"]
            print("\n".join(lines))
            return 0
        if act == "journal":
            rows = []
            for r in J.journal(a.days, getattr(a, "name", None))[-a.lines:]:
                rows.append([core.fmt_ts(r["start"]), r["job"], "skipped" if r.get("rc") is None else
                             ("ok" if r["rc"] == 0 else f"exit {r['rc']}"), f"{r.get('duration', 0):.0f}s",
                             (r.get("summary") or "")[:70]])
            print(ui.table(rows, ["STARTED", "JOB", "RESULT", "TOOK", "LAST LINE"]) if rows else "  no runs yet")
            return 0
        if act == "show":
            j = J.jobs()[a.name]
            print(ui.section(f"job {a.name}"))
            for k, v in (("about", j.get("about")), ("command", "dev " + " ".join(j["cmd"])),
                         ("schedule", J.schedule_text(j)), ("enabled", "yes" if j.get("enabled") else "no"),
                         ("loaded", "yes" if J.loaded(a.name) else "no"), ("timeout", f"{j.get('timeout')} min"),
                         ("notify", j.get("notify")), ("log", tilde(J.LOGS / (a.name + ".log")))):
                print(ui.kv(k, v, 10))
            for r in J.journal(30, a.name)[-8:]:
                ok = r.get("rc") == 0
                print(f"  {ui.OK if ok else ui.FAIL} {core.fmt_ts(r['start'])}  {r.get('duration', 0):>5.0f}s  "
                      f"{ui.dim((r.get('summary') or '')[:80])}")
            return 0
    except (ValueError, KeyError) as e:
        ui.err(str(e))
        return 2
    rows = []
    for name, j in J.jobs().items():
        r = J.last_run(name) or {}
        last = "never" if not r else (core.ago(r["start"]) + " · " + ("ok" if r.get("rc") == 0 else
                                                                       "skipped" if r.get("rc") is None else f"exit {r['rc']}"))
        nxt = J.next_run(j)
        state = ("on" if J.loaded(name) else "not loaded") if j.get("enabled") else "off"
        rows.append([name, state, J.schedule_text(j), last, core.fmt_ts(nxt)[5:] if nxt else "-", j.get("about", "")[:60]])
    print(ui.section("Scheduled jobs (launchd)"))
    print(ui.table(rows, ["JOB", "STATE", "SCHEDULE", "LAST RUN", "NEXT", "WHAT"]))
    print(ui.dim("\n  dev jobs show|run|logs <job> · dev jobs set <job> --every 30m|--at 03:30|--weekly 'mon 09:00' · "
                 "dev jobs enable|disable <job> · dev jobs journal"))
    return 0


def cmd_team(a):
    from . import work
    t = work.team(max_age_h=a.hours)
    print(ui.section(f"Team · machines active in the last {a.hours} h"))
    if not t:
        print("  no presence yet (each machine publishes it on sync)")
    for m in t:
        print(f"\n  {ui.bold(m['user'] + '@' + m['host'])}  {ui.dim('updated ' + m.get('updated', ''))}")
        for s_ in m.get("sessions", []):
            print(f"    {ui.ACTIVE} {s_.get('agent')} · {s_.get('project')}@{s_.get('branch')}  {ui.dim((s_.get('status') or '')[:80])}")
        for l_ in m.get("locks", []):
            print(f"    🔒 {l_['id']} {l_['title']}  {ui.dim(l_['scope'] + ' · until ' + l_['until'])}")
    print()
    return 0


def cmd_note(a):
    from . import notes
    p, f = _note_project(a)
    kv = {}
    for item in a.pairs:
        if "=" not in item:
            ui.err(f"expected key=value, got “{item}” (keys: {', '.join(notes.META_KEYS)})")
            return 2
        k, v = item.split("=", 1)
        if k not in notes.META_KEYS and not (getattr(a, "initiative", None) and k in notes.INITIATIVE_KEYS):
            ui.err(f"unknown key “{k}” (keys: {', '.join(notes.META_KEYS)})")
            return 2
        kv[k] = v
    with registry.notes_lock(wait=30):  # from the read to the write: another writer of the note waits
        try:
            text = notes.set_meta(f.read_text(), **kv)
        except ValueError as e:
            ui.err(str(e))
            return 2
        core.write_if_changed(f, text)
    meta, _ = notes.split_frontmatter(text)
    print(f"{ui.OK} {p['id']}: " + " · ".join(f"{k} {v}" for k, v in meta.items() if v))
    return 0


def overview_data(days=7):
    reg = registry.build(with_prs=True, refresh_prs=False, pr_max_age=10 ** 9)  # fresh, local, cached PR data
    from . import branches as branchmap, canary
    import datetime as dt
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    out = []
    for p in reg["projects"]:
        ns = p.get("note_summary") or {}
        commits = sum(v for k, v in (p.get("activity") or {}).items() if k >= since)
        live = sum(1 for w in p.get("worktrees", []) if w.get("active")) + (1 if (p.get("main") or {}).get("active") else 0)
        out.append({"id": p["id"], "status": ns.get("status", "active"), "priority": ns.get("priority", "normal"),
                    "milestone": ns.get("milestone"), "due": ns.get("due"), "purpose": (ns.get("purpose") or "")[:160],
                    "now": (ns.get("tasks") or {}).get("now", []), "open_tasks": (ns.get("tasks") or {}).get("open", 0),
                    "last_session": (ns.get("sessions") or [None])[0], "status_line": p.get("status_line"),
                    "branch": (p.get("main") or {}).get("branch"), "open_prs": len(p.get("prs_open", [])),
                    "worktrees": len(p.get("worktrees", [])), "live_sessions": live, f"commits_{days}d": commits,
                    "branches": branchmap.counts(p.get("branches")),
                    "last_activity": p.get("last_activity"), "path": p["path"]})
    order = {"blocked": 0, "active": 1, "paused": 2, "done": 3, "archived": 4}
    out.sort(key=lambda x: (order.get(x["status"], 9), x["priority"] != "high", -(x["last_activity"] or 0)))
    for x in out:
        p = next((q for q in reg["projects"] if q["id"] == x["id"]), {})
        x["progress"], x["local"], x["initiatives"] = p.get("progress"), p.get("local", True), p.get("initiatives", [])
    return {"generated": reg.get("generated"), "days": days, "projects": out, "issues": reg.get("issues", []),
            "canary": canary.summary_line(7), "initiatives": reg.get("initiatives", [])}


def cmd_overview(a):
    data = overview_data(a.days)
    if a.json:
        print(json.dumps(data, indent=1, ensure_ascii=False))
        return 0
    at_risk = sum((x.get("branches") or {}).get("not_backed_up", 0) for x in data["projects"])
    print(ui.section(f"Portfolio · {len(data.get('initiatives', []))} initiatives · {len(data['projects'])} projects"
                     f" · canary {data['canary']}" + (f" · {at_risk} branches not backed up" if at_risk else "")))
    for i in data.get("initiatives", []):
        pct = f"{i['progress']}%" if i["progress"] is not None else "–"
        bar = ""
        if i["progress"] is not None:
            n = int(i["progress"] / 10)
            bar = ui.green("█" * n) + ui.dim("░" * (10 - n))
        print(f"\n  {ui.bold('◆ ' + i['title'])}  {i['status']} · {i['priority']}  {bar} {pct}"
              + (f"  {ui.dim('milestone ' + i['milestone'])}" if i["milestone"] else "")
              + (f"  {ui.dim('due ' + i['due'])}" if i["due"] else ""))
        mem = i["members"]
        local = [m for m in mem if m.get("local")]
        names = [m["id"].split("/")[-1] for m in (local + [m for m in mem if not m.get("local")])][:6]
        more = f" +{len(mem) - len(names)} more" if len(mem) > len(names) else ""
        print("    " + ui.dim(f"{len(mem)} project(s), {len(local)} cloned here: " + ", ".join(names) + more))
        t_ = i["tasks"]
        print("    " + ui.dim(f"tasks: {t_.get('doing', 0)} doing · {t_.get('blocked', 0)} blocked · {t_.get('open', 0)} open "
                              f"· {t_.get('done', 0)} done — dev initiative show {i['id']}"))
        for t in t_["now"][:3]:
            print(f"    {ui.cyan('▸')} {t}")
    in_init = {m["id"] for i in data.get("initiatives", []) for m in i["members"]}
    hidden = 0
    for x in data["projects"]:
        if x["status"] in ("archived",) and not a.all:
            continue
        if x["id"] in in_init and not a.all and not x["live_sessions"] and not x.get("local"):
            hidden += 1
            continue
        badge = {"active": ui.green, "blocked": ui.red, "paused": ui.yellow}.get(x["status"], ui.dim)(x["status"])
        pr = ui.yellow(" !high") if x["priority"] == "high" else ""
        live = f" {ui.ACTIVE} {x['live_sessions']} live" if x["live_sessions"] else ""
        print(f"\n  {ui.bold(x['id'])}  {badge}{pr}{live}  {ui.dim(ago(x['last_activity']))}")
        if x["purpose"]:
            print(f"    {ui.dim(x['purpose'])}")
        if x["status_line"]:
            print(f"    {ui.status_pills(x['status_line']['line'])}  {ui.dim(x['status_line']['agent'] + ' · ' + ago(x['status_line']['ts']))}")
        bc = x.get("branches") or {}
        if bc.get("not_backed_up") or bc.get("embargoed"):  # where the work lives (spec P-fy3bwa0pshk R8)
            text = f"{bc['total']} branch{'es' if bc['total'] != 1 else ''}" + "".join(
                f" · {bc[k]} {label}" for k, label in (("not_backed_up", "not backed up"), ("embargoed", "embargoed"))
                if bc.get(k))
            print(f"    {ui.amber(text) if bc.get('not_backed_up') else text}  {ui.dim('dev branch ls')}")
        for t in x["now"][:3]:
            print(f"    {ui.cyan('▸')} {t}")
        if x["open_tasks"] > len(x["now"][:3]):
            print(f"    {ui.dim(str(x['open_tasks']) + ' open task(s) — dev task -p ' + x['id'])}")
        if x["last_session"]:
            print(f"    {ui.dim('last: ' + x['last_session'][:140])}")
    if hidden:
        print(f"\n  {ui.dim(f'{hidden} initiative member project(s) not cloned here are summarised above — dev overview --all')}")
    if data["issues"]:
        print(ui.section(f"Needs attention ({len(data['issues'])})"))
        for _, m in data["issues"][:8]:
            print(f"  {ui.WARN} {m}")
    print()
    return 0


def cmd_standup(a):
    import datetime as dt
    since = (dt.datetime.now() - dt.timedelta(days=a.days)).strftime("%Y-%m-%d %H:%M")
    reg = _reg(prs=False)
    print(ui.section(f"Standup · since {since}"))
    any_ = False
    for p in reg["projects"]:
        ns = p.get("note_summary") or {}
        sess = [s for s in ns.get("sessions", []) if s[:16] >= since]
        done = [t for t in (ns.get("tasks") or {}).get("done_recent", []) if t[-10:] >= since[:10]]
        now = (ns.get("tasks") or {}).get("now", [])
        commits = core.gitout(p["path"], "log", "--all", f"--since={a.days}.days", "--author",
                              core.sh(["git", "config", "user.email"])[1] or ".", "--format=%s").splitlines() \
            if p.get("main") else []
        if not (sess or done or commits):
            continue
        any_ = True
        print(f"\n  {ui.bold(p['id'])}")
        if done:
            print("    " + ui.green("done: ") + "; ".join(t.rsplit(" ✓", 1)[0] for t in done))
        if commits:
            print(f"    {ui.dim(str(len(commits)) + ' commit(s):')} " + "; ".join(commits[:4]) + (" …" if len(commits) > 4 else ""))
        for s_ in sess[:4]:
            print(f"    {ui.dim('session')} {s_[11:160]}")
        if now:
            print("    " + ui.cyan("now: ") + "; ".join(now[:3]))
    if not any_:
        print(f"  {ui.dim('nothing recorded in this window')}")
    print()
    return 0


def cmd_config(a):
    from . import policy, setup
    act = a.config_cmd or "diff"
    if act == "show":
        print(json.dumps({"config_file": str(core.CONFIG_FILE), "devkit": str(core.DEVKIT_HOME),
                          "effective": CFG}, indent=2, ensure_ascii=False))
        return 0
    if act == "validate":
        return _config_validate()
    if act == "set":
        if not a.key or a.value is None:
            ui.err("dev config set <key> <value>, such as: dev config set progress.max_parallel 6")
            return 2
        try:
            old, new = core.config_set(a.key, a.value)
        except core.ConfigError as e:
            ui.err(str(e))
            return 2
        print(f"{ui.OK} {a.key}: {json.dumps(old, ensure_ascii=False)} → {json.dumps(new, ensure_ascii=False)} "
              f"in {tilde(core.CONFIG_FILE)} (audited)")
        return 0
    if act == "edit":
        editor = os.environ.get("EDITOR") or CFG.get("editor") or "vi"
        run([editor, str(core.CONFIG_FILE)])
        if _config_validate() != 0:
            return 2
        print(f"{ui.ARROW} run `dev config diff` to preview, `dev install` to apply")
        return 0
    if act == "lock":
        print(f"{ui.ARROW} installing {policy.MANAGED_FILE} (admin password required)")
        rc = policy.lock()
        print(f"{ui.OK} Claude Code settings locked: user settings can no longer override devkit's keys" if rc == 0
              else f"{ui.FAIL} lock failed")
        return rc
    if act == "unlock":
        rc = policy.unlock()
        print(f"{ui.OK} lock removed" if rc == 0 else f"{ui.FAIL} unlock failed")
        return rc
    if act == "repair":
        with setup.quiet_mode(True):
            fixed = policy.repair()
        if fixed:
            write_install_record(create=False)  # a repair after a crashed install is no finished install
        if not a.quiet:
            print(f"{ui.OK} restored {len(fixed)} setting(s)" if fixed else f"{ui.OK} nothing to repair")
            for d in fixed:
                print(f"    {d.target:<12} {d.key}")
        return 0
    if act == "status":
        print(ui.kv("lock", policy.lock_status(), 10))
        log = STATE / "logs" / "config.log"
        if log.exists():
            print(ui.section("Recent config events"))
            print("\n".join("  " + ln for ln in log.read_text().splitlines()[-15:]))
        return 0
    broken = _config_broken()
    if broken:
        print(f"{ui.FAIL} {broken} Fix it:")
        print(ui.command("dev config edit", 2))
        return 2
    try:
        from .setup import load_conf
        hooks_conf = load_conf("claude/settings.json").get("hooks") or {}
    except Exception:
        hooks_conf = {}
    try:
        lock = policy.lock_status()
    except Exception:
        lock = ""
    return config_report(policy.diff(), core.read_json(_install_record_file(), {}) or {}, hooks_conf,
                         check=getattr(a, "check", False), lock=lock)


# --------------------------------------------------------------------------- dev config as a report (R11)

# What each devkit hook does, in one line, for `dev config` (the command's subcommand after `dev hook`).
HOOK_INFO = {
    "session-start": "when a session starts, devkit gives it the project, its handoff note and the known pitfalls.",
    "guard": "before a shell command, a file write or a crew launch, devkit's guard checks the workspace rules.",
    "stop": "when a reply ends, devkit checks its status block and keeps it as the project's latest status.",
    "session-end": "when a session ends, devkit closes its records and syncs the handoff repository.",
    "config-change": "when settings change, devkit keeps the settings it manages in place.",
    "instructions-loaded": "when instructions load, devkit notes which files the session read.",
    "prompt": "when you send a message, devkit adds progress, crew messages and the route for the request.",
    "post-tool": "after a tool runs, devkit records progress and timing and reminds the agent to update you.",
    "post-tool-failure": "when a tool call fails, devkit closes its record; when starting a crew member fails, devkit "
                         "frees its crew slot at once.",
    "subagent-start": "when a crew member starts, devkit binds it to its step and its brief.",
    "subagent-stop": "when a crew member stops, devkit checks its report and its step.",
}
# Plain names for the settings people meet most often; the key follows in parentheses.
KEY_INFO = {"permissions.defaultMode": "Who may run commands without asking",
            "user.name": "Your name on commits", "user.email": "Your email on commits",
            "core.excludesFile": "The files git ignores everywhere",
            "cleanupPeriodDays": "How many days Claude Code keeps chats",
            "skipDangerousModePermissionPrompt": "Skip the warning for the mode that never asks",
            "fastModePerSessionOptIn": "Fast mode is chosen per session"}
TARGETS = {"claude": "Claude Code  ~/.claude/settings.json", "codex": "Codex  ~/.codex/config.toml",
           "instructions": "Agent instructions", "skills": "Skills", "git": "git  ~/.gitconfig"}


def _install_record_file() -> Path:
    return core.STATE / "config-installed.json"


_DEV_PATH = re.compile(r"'?[^'\s]*/bin/dev'?")


def _norm_cmd(cmd: str) -> str:
    """A hook command with devkit's path shown as `dev`: the same hook whichever pinned copy installed it."""
    return _DEV_PATH.sub("dev", str(cmd or ""), count=1).strip()


def _config_broken() -> str:
    f = core.CONFIG_FILE
    if not f.exists():
        return ""
    try:
        json.loads(f.read_text())
    except ValueError as e:
        line = f"line {e.lineno}: {e.msg[:1].lower()}{e.msg[1:]}" if hasattr(e, "lineno") else str(e)
        return f"{tilde(f)} is not valid JSON ({line})."
    except OSError as e:
        return f"{tilde(f)} can't be read ({e.strerror})."
    return ""


def write_install_record(create: bool = True):
    """What `dev install` (or `dev config repair`) just installed, so `dev config` can tell a setting that is new in
    this devkit version from one removed outside devkit. The file also says that a whole `dev install` finished on
    this Mac (wizard.installed_before), so only such an install creates it (`create`); a repair, which an agent's
    session-start hook runs even after a crashed install, and `dev install --only` update it only when it exists."""
    from . import policy
    if not create and not _install_record_file().is_file():
        return
    rec = {"ts": round(time.time()), "version": core.VERSION}
    try:
        flat, hooks = policy.claude_desired()
        rec["claude"] = {"keys": {policy._key(k): v for k, v in flat.items()},
                         "hooks": {ev: [_norm_cmd(c) for c in cmds] for ev, cmds in hooks.items()}}
    except Exception:
        pass
    rec["instructions"] = {"keys": {tilde(t): _sha(t) for t in (core.CLAUDE_HOME / "CLAUDE.md",
                                                                core.CODEX_HOME / "AGENTS.md") if t.exists()}}
    f = _install_record_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rec, indent=1, ensure_ascii=False))


def _hook_group(hooks_conf: dict, event: str, cmd: str):
    for g in hooks_conf.get(event) or []:
        for h in g.get("hooks") or []:
            if _norm_cmd(h.get("command")) == _norm_cmd(cmd):
                return g, h
    return {}, {}


def _sha(path) -> str:
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def _instructions(d) -> tuple:
    """(state, version that wrote it) of an instructions file: missing, hand-written, outdated or current."""
    path = Path(os.path.expanduser(d.key))
    try:
        from .setup import instructions_state
        state = instructions_state(path)
    except Exception:
        state = "outdated" if d.current is not None else "missing"
    try:
        m = re.search(r"generated by devkit (\S+)", path.read_text()[:200])
    except OSError:
        m = None
    return state, m.group(1) if m else ""


def _kind(d, record: dict) -> str:
    """new (in this devkit version, not installed yet) · removed (outside devkit) · changed (outside devkit)."""
    from . import policy
    rec = record.get(d.target) or {}
    if d.target == "instructions":
        state, _ = _instructions(d)
        if state == "missing":
            return "removed" if d.key in (rec.get("keys") or {}) else "new"
        if state == "hand-written":
            return "changed"
        old = (rec.get("keys") or {}).get(d.key)
        return "changed" if old and old != _sha(os.path.expanduser(d.key)) else "new"
    if d.key.startswith("hooks.") and d.target == "claude":
        installed = [_norm_cmd(c) for c in (rec.get("hooks") or {}).get(d.key[6:], [])]
        return "removed" if _norm_cmd(d.desired) in installed else "new"
    keys = rec.get("keys") or {}
    if d.current is policy.MISSING:
        return "removed" if d.key in keys else "new"
    if d.key in keys and keys[d.key] == d.current:
        return "new"  # what devkit installed last time: this version wants another value
    return "changed"


def _value(v) -> str:
    from . import policy
    if v is policy.MISSING or v is None:
        return "not set"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or "(empty list)"
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


KINDS = {"new": ("new", "New in this devkit version, not installed yet", "Install it:", "dev install"),
         "removed": ("removed", "Removed outside devkit", "Put it back:", "dev config repair"),
         "changed": ("changed", "Changed outside devkit", "Put devkit's value back:", "dev config repair")}


def _drift_lines(d, kind: str, hooks_conf: dict, cols: int) -> list:
    g_name, title, fix_text, fix = KINDS[kind]
    mark = {"new": ui.green, "removed": ui.red, "changed": ui.yellow}[kind](ui.glyph(g_name))
    out = [f"  {mark}  {ui.bold(title)}"]
    body = []
    if d.key.startswith("hooks.") and d.target == "claude":
        event = d.key[6:]
        g, h = _hook_group(hooks_conf, event, d.desired)
        cmd = _norm_cmd(d.desired)
        sub = cmd.split()[2] if cmd.startswith("dev hook ") and len(cmd.split()) > 2 else ""
        body += ui.para("A hook: " + HOOK_INFO.get(sub, "devkit runs a command at this moment."), cols, indent=5)
        limit = f" ({h['timeout']} s limit)" if h.get("timeout") else ""
        body += ui.row("Event", event, cols, indent=7)
        body += ui.row("Matches", ", ".join((g.get("matcher") or "").split("|")) or "everything", cols, indent=7)
        body += ui.row("Runs", cmd + limit, cols, indent=7)
    elif d.target == "instructions":
        agent = "Codex" if d.key.endswith("AGENTS.md") else "Claude Code"
        _, ver = _instructions(d)
        text = {"new": f"{agent}'s global instructions ({d.key}) come from devkit {ver or 'an older version'}; this "
                       "version writes newer ones.",
                "removed": f"{agent}'s global instructions ({d.key}) are missing.",
                "changed": f"{agent}'s global instructions ({d.key}) were edited by hand. Personal rules belong in "
                           "~/.config/devkit/AGENTS.personal.md, which dev install adds to them."}[kind]
        body += ui.para(text, cols, indent=5)
    elif d.target == "skills":
        body += ui.para(f"A skill devkit installs is not linked: {d.key}", cols, indent=5)
    else:
        name = KEY_INFO.get(d.key)
        body += ui.para(f"{name} ({d.key})" if name else d.key, cols, indent=5)
        if isinstance(d.desired, list) and isinstance(d.current, list):
            add = [x for x in d.desired if x not in d.current]
            gone = [x for x in d.current if x not in d.desired]
            if add:
                body += ui.row("devkit adds", _value(add), cols, lw=17, indent=7)
            if gone:
                body += ui.row("extra now", _value(gone), cols, lw=17, indent=7)
        else:
            body += ui.row("devkit's value", _value(d.desired), cols, lw=17, indent=7)
            body += ui.row("installed now", _value(d.current), cols, lw=17, indent=7)
    out += body + [" " * 5 + fix_text, ui.command(fix, 5)]
    if kind == "changed" and d.target != "instructions":
        out += ui.para("Or keep yours by adding it to your devkit config: dev config edit", cols, indent=5)
    return out


def config_report(drift: list, record: dict, hooks_conf: dict, check: bool = False, cols: int = None,
                  lock: str = "") -> int:
    """`dev config`: each difference says what it is, shows readable fields and names the fix; exits 0. With
    `check` (scripts, dev doctor): one line and exit 1 when something differs, nothing and exit 0 otherwise."""
    n = len(drift)
    if check:
        if n:
            print(f"{n} setting{'s' if n != 1 else ''} differ{'s' if n == 1 else ''} from what devkit installs: run "
                  "dev config for details.")
        return 1 if n else 0
    cols = cols or ui.width()
    kinds = [(d, _kind(d, record)) for d in drift]
    counts = {}
    for _, k in kinds:
        counts[k] = counts.get(k, 0) + 1
    words = [f"{counts['new']} to install"] if counts.get("new") else []
    words += [f"{counts['changed']} changed outside devkit"] if counts.get("changed") else []
    words += [f"{counts['removed']} removed outside devkit"] if counts.get("removed") else []
    print()
    print(ui.rule_title("devkit settings: " + (", ".join(words) if words else "all installed"), cols))
    print("\n".join(ui.para("devkit compares the settings this version wants with what is installed in Claude Code, "
                            "Codex, git and your skills.", cols)))
    locked = lock == "on"
    if not drift:
        print(f"\n  {ui.green(ui.glyph('ok'))} All devkit settings are installed and match.")
        if locked:
            print(f"  {ui.dim('Settings lock is on: Claude Code can’t override these.')}")
        print()
        return 0
    cur = None
    for d, k in sorted(kinds, key=lambda x: list(TARGETS).index(x[0].target) if x[0].target in TARGETS else 99):
        if d.target != cur:
            cur = d.target
            print(f"\n  {ui.bold(TARGETS.get(cur, cur))}")
        print("\n".join(_drift_lines(d, k, hooks_conf, cols)))
    if any(k == "changed" and d.target == "claude" for d, k in kinds) and not locked:
        print()
        print("\n".join(ui.para("To stop this, lock the settings: dev config lock. It asks for your Mac password.",
                                cols)))
    print(f"\n  {ui.green(ui.glyph('ok'))} Everything else matches.\n")
    return 0


def _config_validate() -> int:
    from .core import DEFAULTS
    errors = []
    cfg = core.read_json(core.CONFIG_FILE, None)
    errors += core.config_errors(cfg or {})  # value checks: the audit retention floor, levels and formats
    if cfg is None and core.CONFIG_FILE.exists():
        errors.append(f"{tilde(core.CONFIG_FILE)} is not valid JSON")
    for k in (cfg or {}):
        if k not in DEFAULTS and k not in ("default_host", "host_dirs", "branches", "handoff"):
            errors.append(f"unknown key “{k}” in {tilde(core.CONFIG_FILE)}")
    for k, v in (cfg or {}).items():
        if k in DEFAULTS and isinstance(DEFAULTS[k], dict) and not isinstance(v, dict):
            errors.append(f"“{k}” must be an object")
    for rel in ("claude/settings.json", "claude/plugins.json", "codex/config.json", "codex/hooks.json",
                "codex/plugins.json", "skills.json"):
        try:
            json.loads((core.DEVKIT_HOME / "config" / rel).read_text())
        except (OSError, ValueError) as e:
            errors.append(f"config/{rel}: {e}")
    for e in errors:
        print(f"  {ui.FAIL} {e}")
    if not errors:
        print(f"  {ui.OK} configuration is valid")
    return 1 if errors else 0


def cmd_guard(a):
    from . import guard as g
    cwd = os.path.abspath(a.cwd or os.getcwd())
    if a.write:
        tool, ti = "Write", {"file_path": a.write}
    else:
        tool, ti = "Bash", {"command": " ".join(a.command)}
    v = g.check(tool, ti, cwd, a.agent)
    if not v:
        print(f"{ui.OK} allow   ({a.agent}, cwd {tilde(cwd)})")
        return 0
    color = ui.yellow if v[0] == "ask" else ui.red
    print(f"{color(v[0])}   ({a.agent}, cwd {tilde(cwd)})\n  {v[1]}")
    return 1


def plugin_checks():
    """Validate the devkit plugin with Claude Code's validator and Codex's own manifest parser. -> [(name, ok, detail)]"""
    root = core.DEVKIT_HOME
    out = []
    if core.have("claude"):
        rc, o, e = core.sh(["claude", "plugin", "validate", str(root / "plugin")], timeout=120)
        out.append(("Claude Code validator", rc == 0 and "Validation passed" in o + e,
                    "validation passed" if rc == 0 else (o + e).strip().splitlines()[-1:] or ["failed"]))
    if core.have("codex"):
        from .codexrpc import CodexError, CodexRPC
        try:
            with CodexRPC(timeout=40) as rpc:
                for mp in (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"):
                    r = rpc.call("plugin/read", {"marketplacePath": str(root / mp), "pluginName": "devkit"})
                    p = r.get("plugin") or {}
                    sm = p.get("summary") or {}
                    itf = sm.get("interface") or {}
                    skills = [x.get("name") for x in (p.get("skills") or [])]
                    ok = (sm.get("version") or sm.get("localVersion")) == core.VERSION and itf.get("logo") and len(skills) >= 4
                    out.append((f"Codex parser ({mp.split('/')[0]})", bool(ok),
                                f"v{sm.get('version') or sm.get('localVersion')} · {itf.get('displayName')} · "
                                f"{len(skills)} skills · logo {'ok' if itf.get('logo') else 'missing'}"))
        except CodexError as e:
            out.append(("Codex parser", False, str(e)))
    return out


def cmd_plugin(a):
    res = plugin_checks()
    for name, ok, detail in res:
        detail = detail if isinstance(detail, str) else " ".join(detail)
        print(f"  {ui.OK if ok else ui.FAIL} {name:<34} {ui.dim(detail)}")
    return 0 if res and all(ok for _, ok, _ in res) else 1


CALVER_RE = re.compile(r"^v?(\d{2})\.(\d{1,2})\.(\d+)$")


def calver(path: str, today=None):
    """-> (current CalVer tag or None, next YY.MM.PATCH) from the repository's tags."""
    import datetime as dt
    today = today or dt.date.today()
    yy, mm = today.year % 100, today.month
    tags = core.gitout(path, "tag", "--list").split()
    versions = sorted((tuple(int(x) for x in m.groups()), t) for t in tags if (m := CALVER_RE.match(t)))
    current = versions[-1][1] if versions else None
    this_month = [v for v, _ in versions if v[0] == yy and v[1] == mm]
    nxt = f"{yy}.{mm}.{max(v[2] for v in this_month) + 1 if this_month else 0}"
    return current, nxt


def cmd_calver(a):
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel") or str(core.DEVKIT_HOME)
    cur, nxt = calver(top)
    if a.next:
        print(nxt)
        return 0
    print(ui.kv("repository", tilde(top), 12))
    print(ui.kv("current", cur or "no CalVer tag yet", 12))
    print(ui.kv("next", f"{nxt}  (tag v{nxt}, branch release/{nxt})", 12))
    return 0


def _quota_text(q: dict) -> str:
    if not q:
        return "-"
    if q.get("available") is None:
        return ui.dim("not exposed by the CLI")
    used = q.get("used_percent")
    reset = core.fmt_ts(q.get("resets_at")) if q.get("resets_at") else "-"
    txt = f"{used}% of the {int((q.get('window_minutes') or 0) / 1440) or '?'}-day window used · resets {reset}"
    return (ui.red("exhausted · ") + txt) if not q.get("available") else (ui.yellow(txt) if (used or 0) >= 80 else txt)


def rules_text(section: str = "") -> str:
    """devkit's global rules as `dev install` writes them, or one section ("0", "3", "crew", "status block")."""
    from . import setup
    text = setup.render_instructions().split("\n", 1)[1]  # without the generated-by line
    if not section:
        return text
    want = section.strip().lower()
    parts = re.split(r"(?m)^(?=## )", text)
    for p in parts:
        head = p.split("\n", 1)[0].lower()
        if head.startswith(f"## {want}.") or (not want.isdigit() and want in head):
            return p.strip()
    subs = re.split(r"(?m)^(?=### )", text)
    for p in subs:
        if p.startswith("### ") and want in p.split("\n", 1)[0].lower():
            return re.split(r"(?m)^## ", p)[0].strip()
    raise ValueError(f"no section {section!r}: give its number (0-12) or words from its title")


def cmd_agents(a):
    from . import providers
    if a.action == "rules":
        try:
            print(rules_text(getattr(a, "section", "") or ""))
        except ValueError as e:
            ui.err(str(e))
            return 2
        return 0
    if a.action == "test":
        targets = ["claude", "codex"] if a.agent in (None, "all") else [a.agent]
        rc = 0
        for t in targets:
            print(ui.section(f"Self-test · {t} (a short real session in a scratch repo)"))
            res = providers.selftest(t, model=a.model)
            for name, ok, detail in res["checks"]:
                print(f"  {ui.OK if ok else ui.FAIL} {name:<34} {ui.dim(str(detail))}")
            if res.get("skipped"):
                print(f"  {ui.WARN} skipped: {res['skipped']}")
            elif a.verbose and res.get("reply"):
                print(ui.dim("\n  reply: " + res["reply"][:600].replace("\n", "\n         ")))
            rc |= 0 if res["ok"] else 1
        return rc
    st = providers.status()
    if a.json:
        print(json.dumps(st, indent=1, ensure_ascii=False))
        return 0
    for key in ("claude", "codex"):
        p = st[key]
        print(ui.section(p["name"]))
        if not p.get("installed"):
            print(f"  {ui.FAIL} not installed")
            continue
        print(ui.kv("installed", f"{p.get('version')}" + ("  · desktop app present" if p.get("desktop") else ""), 12))
        signed = p.get("logged_in")
        print(ui.kv("signed in", (ui.green("yes") if signed else ui.red("no")) +
                    f" · {p.get('auth_method') or '-'} · plan {p.get('plan') or '-'}" +
                    (f" · {p['account']}" if p.get("account") else ""), 12))
        if p.get("login_expires"):
            print(ui.kv("login until", p["login_expires"][:10], 12))
        print(ui.kv("quota", _quota_text(p.get("quota")), 12))
        last = core.read_json(STATE / f"selftest-{key}.json", None)
        if last:
            print(ui.kv("self-test", (ui.green("passed") if last["ok"] else ui.red("failed")) +
                        f" {ago(last.get('ts'))} — dev agents test {key}", 12))
    print()
    return 0


def cmd_setup(a):
    if getattr(a, "what", None) == "prompt":
        from . import setup as setup_mod
        return setup_mod.setup_prompt(assume_yes=a.yes, remove=a.remove)
    if getattr(a, "what", None) == "commit-template":
        from . import setup as setup_mod
        return setup_mod.setup_commit_template(assume_yes=a.yes, remove=a.remove)
    if getattr(a, "what", None) == "shell":
        from . import setup as setup_mod
        return setup_mod.setup_shell(assume_yes=a.yes, remove=a.remove)
    from . import wizard
    if getattr(a, "preview", False):
        return wizard.print_preview()
    return wizard.run(assume_yes=a.yes, skip_packages=a.skip_packages, resume=bool(getattr(a, "resume", False)))


def cmd_docs(a):
    from . import docs
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel") or os.getcwd()
    return docs.check(Path(top), paths=a.paths or None, online=a.online, fix=a.fix, prose=not a.no_prose)


def cmd_sec(a):
    from . import security
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel") or os.getcwd()
    if a.query:
        top = _resolve(_reg(prs=False), a.query)["path"]
    return security.scan(Path(top), history=a.history, verbose=a.verbose, sast_config=a.config)


def cmd_diagram(a):
    from . import diagram
    return diagram.main(a.rest)


def cmd_version(a):
    where = tilde(core.DEVKIT_HOME)
    if core.is_pinned():
        where += f" (pinned copy of {tilde(core.source_home())})"
    elif core.is_pinned(core.runtime_home()):
        where += f" (checkout; the hooks run {tilde(core.runtime_home())})"
    print(f"devkit {core.VERSION} · {where}\n{COPYRIGHT}")
    return 0


def cmd_hook(a):
    from . import hooks
    return hooks.run(a.event, a.agent)


def cmd_mcp(a):
    from . import mcp
    return mcp.serve()


def cmd_complete(a):
    """`dev __complete <kind>`: the live values shell completion offers, as value:description lines. bin/dev answers
    the kinds of completion.KINDS before it builds this parser; this is the same answer for callers of main()."""
    from . import completion
    if a.kind == "commands":
        print("\n".join(sorted({c.split()[0] for _, cmds in COMMANDS for c, _ in cmds})))
        return 0
    if completion.OLD_KINDS.get(a.kind, a.kind) not in completion.KINDS:
        ui.err(f"unknown kind “{a.kind}”: one of {', '.join(completion.KINDS)}, commands")
        return 2
    rows = completion.values(a.kind, getattr(a, "project", None), getattr(a, "initiative", None), getattr(a, "arg", ""))
    sys.stdout.write(completion.render(rows, tab=getattr(a, "tab", False)))
    return 0


def cmd_completion(a):
    from . import completion
    sys.stdout.write(completion.SCRIPTS[a.shell][1]())
    return 0


# --------------------------------------------------------------------------- parser

class Parser(argparse.ArgumentParser):
    """dev's parser: no abbreviated flags. argparse reads a unique prefix as the flag (`dev sync --sched` ran a
    scheduled sync) while the guard matches the flag itself (B1). Subparsers are built with the parent's class, so
    every `dev` command and subcommand gets it."""

    def __init__(self, *args, allow_abbrev=False, **kwargs):
        super().__init__(*args, allow_abbrev=allow_abbrev, **kwargs)


def build_parser():
    ap = Parser(prog="dev", add_help=False)
    ap.add_argument("-h", "--help", action="store_true")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_, **kw):
        p = sp.add_parser(name, help=help_, description=help_, **kw)
        p.set_defaults(fn=fn)
        return p

    add("help", lambda a: print_help(), "Show help")
    p = add("ls", cmd_ls, "The projects on this Mac; --all adds the portfolio")
    p.add_argument("--offline", action="store_true", help="use cached PR data only, no checks")
    p.add_argument("--names", action="store_true", help="print project ids only")
    p.add_argument("--all", action="store_true", help="also the portfolio projects that are not on this Mac")
    p.add_argument("--json", action="store_true", help="the rows as JSON (for tools)")
    p = add("go", cmd_go, "Print a project's path, or the path of one of its worktrees")
    p.add_argument("query", help="an id, an alias or part of a name")
    p.add_argument("branch", nargs="?", help="a managed worktree's branch (or part of it): print that worktree's path")
    p = add("alias", cmd_alias, "Short names for projects, shared by the team: dev alias <alias> [project], "
                                "dev alias rm <alias>, dev alias (list)")
    p.add_argument("alias", nargs="?", help="the alias to set, or rm")
    p.add_argument("project", nargs="?", help="the project (default: the one you are in); after rm, the alias")
    p.add_argument("--move", action="store_true", help="take the alias from the project that has it")
    p = add("status", cmd_status, "Project status: what needs you, checks, the crew, the last update, details")
    p.add_argument("query", nargs="?", help="an id, an alias or part of a name (default: the project you are in)")
    p.add_argument("--json", action="store_true", help="the same data as JSON (for tools)")
    p.add_argument("--all", action="store_true", help="also the project's tasks and plans")
    add("clone", cmd_clone, "Clone into the canonical layout").add_argument("repo", help="owner/repo, host/group/repo or any git URL")
    p = add("new", cmd_new, "Start a new project")
    p.add_argument("name")
    p.add_argument("--github", action="store_true", help="create the repo on GitHub and clone it")
    p.add_argument("--owner", help="GitHub owner (default: you)")
    p.add_argument("--public", action="store_true", help="public repo (default private)")
    p.add_argument("--no-git", action="store_true", help="plain folder, no git init")
    add("adopt", cmd_adopt, "Move a repo into the canonical layout").add_argument("path")
    add("open", cmd_open, "Open a project in your editor").add_argument("query", nargs="?")
    for tool, fn in (("claude", cmd_claude), ("codex", cmd_codex)):
        p = add(tool, fn, f"Start {tool} in a project")
        p.add_argument("query", nargs="?")
        p.add_argument("-w", "--worktree", metavar="BRANCH", help="run in a managed worktree for BRANCH")
        p.add_argument("args", nargs=argparse.REMAINDER, help=f"arguments passed to {tool} (after --)")
    p = add("handoff-import", cmd_handoff_import, "Adopt an old handoff repository (from a staging folder) as an initiative")
    p.add_argument("staging")
    p.add_argument("--slug", required=True)
    p.add_argument("--dry-run", action="store_true")
    p = add("handoff", cmd_handoff, "Show or edit a handoff note")
    p.add_argument("query", nargs="?")
    p.add_argument("--edit", action="store_true")
    p.add_argument("--path", action="store_true")
    p.add_argument("--log", metavar="TEXT", help="append a line to the session log")
    p.add_argument("--agent", default="human", help="who is logging (human, claude, codex)")
    from . import continuity
    continuity.add_arguments(add("session", continuity.cmd_session, "Agent sessions: continuity through compaction, "
                                                                    "/clear and resume; talk, claim and escalate "
                                                                    "between sessions on this Mac"))
    p = add("task", cmd_task, "Work items of a project (or of an initiative with -i): add, claim, done, history")
    p.add_argument("-p", "--project", help="project (default: the one you are in)")
    p.add_argument("-i", "--initiative", help="an initiative instead of a project")
    p.add_argument("--all", action="store_true", help="show every done item")
    tsp = p.add_subparsers(dest="task_cmd")
    t = tsp.add_parser("add", help="add a task, feature, bug or spike")
    t.add_argument("text", nargs="+")
    t.add_argument("--now", action="store_true", help="add to Now")
    t.add_argument("--next", action="store_true", help="add to Next (default)")
    t.add_argument("--later", action="store_true", help="add to Later")
    t.add_argument("--type", default="task", choices=["task", "feature", "bug", "spike"])
    t.add_argument("--priority", default="normal", choices=["high", "normal", "low"])
    t.add_argument("--due", help="YYYY-MM-DD")
    t.add_argument("--body", help="description / acceptance criteria")
    t.add_argument("--why", help="why this work exists (recorded in the changelog)")
    t.add_argument("--claim", action="store_true", help="claim it right away")
    t.add_argument("--done", action="append", metavar="CRITERION", help="acceptance criterion (repeat for several)")
    t.add_argument("--no-template", action="store_true", help="no structured description")
    for verb, h in (("start", "claim the lock and move to Now (doing)"), ("claim", "same as start"),
                    ("release", "give the lock back (back to todo)"), ("done", "mark done and release"),
                    ("block", "mark blocked (needs --why)"), ("unblock", "back to work (todo, or doing if claimed)"),
                    ("cancel", "cancel (needs --why)"), ("reopen", "back to todo after done or cancel (needs --why)"),
                    ("show", "an item with its history"), ("history", "an item's changelog")):
        t = tsp.add_parser(verb, help=h)
        t.add_argument("selector", nargs="+", help="id (T-…), number from `dev task`, or a unique part of the title")
        t.add_argument("--why", help="the reason (recorded in the changelog and the commit)")
    t = tsp.add_parser("link", help="count a project task toward an initiative (needs --why)")
    t.add_argument("selector", nargs="+")
    t.add_argument("--to", required=True, metavar="INITIATIVE")
    t.add_argument("--why")
    t = tsp.add_parser("delegate", help="hand a task to a teammate: assign it and write the message to send")
    t.add_argument("selector", nargs="+")
    t.add_argument("--to", required=True, help="the teammate's GitHub login")
    t.add_argument("--why", help="why it matters (goes in the message and the changelog)")
    t.add_argument("--due", help="YYYY-MM-DD")
    t.add_argument("--no-copy", action="store_true", help="do not copy the message to the clipboard")
    t = tsp.add_parser("move", help="move between Now / Next / Later (needs --why)")
    t.add_argument("selector", nargs="+")
    t.add_argument("--to", required=True, choices=["now", "next", "later"])
    t.add_argument("--why")
    t = tsp.add_parser("edit", help="change title, description, priority or due date (needs --why)")
    t.add_argument("selector", nargs="+")
    t.add_argument("--title")
    t.add_argument("--body-file", help="file with the new description")
    t.add_argument("--priority", dest="priority_set", choices=["high", "normal", "low"])
    t.add_argument("--due")
    t.add_argument("--why")
    t = tsp.add_parser("reconcile", help="find stale items: doing without a lock, open although their branch is merged, "
                                         "the same title twice; --apply fixes the first two")
    t.add_argument("--apply", action="store_true", help="close the items of merged branches (done, the merge commit as "
                                                        "evidence) and send doing items without a lock back to todo")
    t.add_argument("--json", action="store_true")
    tsp.add_parser("ls", help="list open items")
    for sub in tsp.choices.values():
        sub.add_argument("-p", "--project", default=argparse.SUPPRESS, help="project (default: the one you are in)")
        sub.add_argument("-i", "--initiative", default=argparse.SUPPRESS, help="an initiative instead of a project")
    p = add("note", cmd_note, "Project metadata in the handoff note")
    p.add_argument("set", choices=["set"])
    p.add_argument("pairs", nargs="+", help="status=… priority=… milestone=… due=YYYY-MM-DD tags=a,b progress=0-100")
    p.add_argument("-p", "--project")
    p.add_argument("-i", "--initiative")
    p = add("plan", cmd_plan, "Versioned plans and specs: new, revise --why, approve, ls")
    p.add_argument("-p", "--project")
    p.add_argument("-i", "--initiative")
    psp = p.add_subparsers(dest="plan_cmd")
    x = psp.add_parser("new", help="create a plan, spec, spike, bugfix or ADR from its template")
    x.add_argument("title", nargs="+")
    x.add_argument("--kind", default="plan", choices=["plan", "spec", "spike", "bugfix", "adr"])
    for verb in ("revise", "approve", "supersede"):
        x = psp.add_parser(verb, help=f"{verb} a plan (records version, who and why)")
        x.add_argument("path")
        x.add_argument("--why", help="the reason for the change (required for revise)")
    psp.add_parser("ls", help="list plans")
    for sub in psp.choices.values():
        sub.add_argument("-p", "--project", default=argparse.SUPPRESS)
        sub.add_argument("-i", "--initiative", default=argparse.SUPPRESS)
    p = add("progress", cmd_progress, "Live progress of agent work: start, step, block, done; ETA from real pace")
    gsp = p.add_subparsers(dest="progress_cmd")
    x = gsp.add_parser("start", help="start a run: goal, steps, estimate")
    x.add_argument("goal", nargs="+")
    x.add_argument("--steps", type=int)
    x.add_argument("--estimate", help="e.g. 30m, 1h30m")
    x.add_argument("--mode", choices=["guided", "auto"],
                   help="guided: the developer's decisions wait for the developer; auto: the crew goes on with the "
                        "recommended option and records it (default: devkit config work.mode, else guided)")
    x.add_argument("--plan", help="the steps, separated by ';', each with an optional estimate: "
                                  "\"design=30m; build=2h; docs\"")
    x.add_argument("--replace", action="store_true", help="replace this session's live run (needs --why)")
    x.add_argument("--why", help="why the live run is replaced (kept in the new run)")
    x = gsp.add_parser("note", help="an update while a step is still running: no step changes state")
    x.add_argument("text", nargs="+")
    x.add_argument("--next", help="what comes next")
    for verb, h_ in (("step", "a step finished"), ("block", "blocked: why and what is needed"),
                     ("done", "finished: the outcome")):
        x = gsp.add_parser(verb, help=h_)
        x.add_argument("text", nargs="+")
        x.add_argument("--next", help="what comes next")
        x.add_argument("--check", help="the evidence for this step: tests run, command output, CI result")
        x.add_argument("--step", help="which plan step (number or part of its name); default: the running one")
        x.add_argument("--force", action="store_true", help="skip the safety checks (text must describe the step, "
                                                                "dependencies done, no subagent still on it)")
        if verb == "step":
            x.add_argument("--no-check", action="store_true", help="nothing to verify for this step (shown as ⚠)")
            x.add_argument("--approves", help="the finished steps this Check approves (✓) or sends back (✗ or ⚠): "
                                              "\"api, screens\"")
        if verb == "done":
            x.add_argument("--why", help="end the run although an Accountable member has not approved (recorded)")
    x = gsp.add_parser("assign", help="hand a plan step to a subagent: the one launched with this description owns it")
    x.add_argument("which", help="step number or part of its name")
    x.add_argument("--to", nargs="+", required=True, help="the subagent's description or name, e.g. Neo")
    x.add_argument("--scope", help="the files it owns, comma-separated globs; the guard keeps its edits inside them")
    x.add_argument("--why", help="assign a member the crew's RACI does not make Responsible (recorded)")
    x.add_argument("--activity", help="the step's kind of work, an id from dev crew raci (backend, ux, …): it decides "
                                      "the RACI check and the approver, in any language")
    x.add_argument("--allow", help="the irreversible actions this member may take in the step: push, pr (tier 1 "
                                   "only), each with the developer's grant (spec C6)")
    x = gsp.add_parser("begin", help="mark a plan step as started (parallel work you do yourself)")
    x.add_argument("which", help="step number or part of its name")
    x = gsp.add_parser("add", help="add a step found during the work: \"name=30m after other\"")
    x.add_argument("spec", nargs="+")
    x.add_argument("--why", help="why it was not in the plan")
    for verb, h_ in (("reopen", "undo a wrong close: the step goes back to running"),
                     ("skip", "skip a step on purpose"),
                     ("confirm", "a step flagged recheck still holds after the step it used was redone")):
        x = gsp.add_parser(verb, help=h_)
        x.add_argument("which", help="step number or part of its name")
        x.add_argument("--why", required=True, help="the reason (kept in the run's history)")
    gsp.add_parser("history", help="every step change: when, from → to, by whom, why")
    x = gsp.add_parser("time", help="how long things take: turns, slow commands, subagents, estimate accuracy")
    x.add_argument("--days", type=int, default=7)
    x.add_argument("-p", "--project")
    p = add("time", lambda a: (print(__import__("devkit.progress", fromlist=["x"]).report(a.days, a.project)), 0)[1],
            "How long things take: turns, slow commands, subagents, estimate accuracy")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("-p", "--project")
    p = add("delivery", cmd_delivery, "Versioning, release, test and deploy policy: detect, recommend, record, gaps")
    p.add_argument("-p", "--project")
    p.add_argument("-i", "--initiative")
    p.add_argument("--json", action="store_true")
    dsp = p.add_subparsers(dest="delivery_cmd")
    x = dsp.add_parser("apply", help="record the policy (recommended, or with --set changes) in the handoff repo")
    x.add_argument("--set", action="append", metavar="KEY=VALUE", help="e.g. versioning=semver, test_gate=\"a; b\", job_timeout_minutes=45")
    x.add_argument("--why", help="the reason (recorded in the policy changelog and the commit)")
    x.add_argument("--tasks", action="store_true", help="create a work item for every gap")
    x.add_argument("-p", "--project", default=argparse.SUPPRESS)
    x.add_argument("-i", "--initiative", default=argparse.SUPPRESS)
    p = add("lesson", cmd_lesson, "Learn from mistakes: record, recall, count repeats, prevent with automation")
    p.add_argument("-p", "--project")
    p.add_argument("-i", "--initiative")
    p.add_argument("--global", dest="global_", action="store_true", help="a lesson for every project")
    p.add_argument("--all", action="store_true", help="every scope, including retired lessons")
    p.add_argument("--search")
    lsp = p.add_subparsers(dest="lesson_cmd")
    x = lsp.add_parser("add", help="record a lesson: what happened, root cause, fix, prevention, detection")
    x.add_argument("title", nargs="+")
    x.add_argument("--kind", default="mistake", choices=["bug", "mistake", "process", "tooling", "security",
                                                         "performance", "design"])
    x.add_argument("--severity", default="medium", choices=["low", "medium", "high"])
    x.add_argument("--happened", help="what happened (this or --evidence is required)")
    x.add_argument("--cause", help="the root cause (required)")
    x.add_argument("--fix")
    x.add_argument("--prevent", help="the rule that stops it happening again (required)")
    x.add_argument("--detect", help="how to notice it early")
    x.add_argument("--evidence", action="append", help="commit, PR, CI run, task (repeat; this or --happened is "
                                                       "required)")
    x.add_argument("--tag", action="append")
    x.add_argument("--retire-when", help="the outside condition that makes it obsolete (a tool version, an issue)")
    x.add_argument("--why")
    x.add_argument("--new", action="store_true",
                   help="record it although a lesson shares an error or command anchor: another mistake (needs --why)")
    _anchor_flags(x)
    x = lsp.add_parser("anchor", help="add where a lesson applies (files, commands, kinds of work, errors, stack)")
    x.add_argument("selector", nargs="+")
    x.add_argument("--why", required=True, help="why (kept in the lesson's history)")
    _anchor_flags(x)
    for verb, h_ in (("show", "a lesson with its history"), ("recur", "it happened again (counts repeats)"),
                     ("retire", "no longer applies (needs --why)"), ("restore", "bring back a forgotten lesson")):
        x = lsp.add_parser(verb, help=h_)
        x.add_argument("selector", nargs="+")
        x.add_argument("--evidence")
        x.add_argument("--why")
    x = lsp.add_parser("prevent", help="automation now prevents it (test, lint, guard, skill, ci, rule), or a lesson "
                                       "rule the guard enforces (--by guard --match|--error)")
    x.add_argument("selector", nargs="+")
    x.add_argument("--by", required=True, choices=["test", "lint", "type", "guard", "skill", "ci", "rule", "doc"])
    x.add_argument("--ref", help="the test, rule or file that prevents it")
    x.add_argument("--match", help="with --by guard: propose a lesson rule on these command words, e.g. \"python3 -c\"")
    x.add_argument("--error", help="with --by guard: propose a lesson rule on this text of a tool's output")
    x.add_argument("--block", action="store_true", help="the lesson rule refuses from the start (the developer only)")
    x.add_argument("--why")
    x = lsp.add_parser("supersede", help="a newer lesson replaces this one: only the newer one is recalled")
    x.add_argument("old", help="the lesson replaced")
    x.add_argument("--by", required=True, help="the lesson that replaces it")
    x.add_argument("--why")
    x = lsp.add_parser("release", help="let a lesson the screen held back into recall (the developer only)")
    x.add_argument("lesson", help="the held lesson's id")
    x.add_argument("--why")
    x = lsp.add_parser("scan", help="signals worth a lesson: reverts, fixes, repeated guard refusals, canary misses")
    x.add_argument("--days", type=int, default=14)
    lsp.add_parser("review", help="what to act on: missed, failed preventions, recurring, held, stale; heeded rates")
    lsp.add_parser("lint", help="anchors to fix and lessons recall cannot show (exit 1 when there are any)")
    x = lsp.add_parser("eval", help="would recall have shown the lesson before each repeat recorded here?")
    x.add_argument("--days", type=int, default=14)
    lsp.add_parser("ls", help="lessons for this project, its initiatives and global ones")
    for sub in lsp.choices.values():
        sub.add_argument("-p", "--project", default=argparse.SUPPRESS)
        sub.add_argument("-i", "--initiative", default=argparse.SUPPRESS)
        sub.add_argument("--global", dest="global_", action="store_true", default=argparse.SUPPRESS)
    p = add("bug", cmd_bug, "devkit's own bugs: known detours, reports without project data, hotfix ranking")
    bsp = p.add_subparsers(dest="bug_cmd")
    x = bsp.add_parser("ls", help="bugs in the cache, ranked: votes, impact, detours, fix version")
    x.add_argument("--open", action="store_true", help="open bugs only")
    x.add_argument("--fixed", action="store_true", help="bugs with a fix version or closed")
    x.add_argument("--kind", choices=["crash", "hook", "guard", "command"])
    x.add_argument("--mine", action="store_true", help="bugs this Mac hit")
    x.add_argument("--local", action="store_true", help="the failures recorded on this Mac")
    x = bsp.add_parser("show", help="one bug: versions, votes, detours with their trust, related lessons")
    x.add_argument("bug", help="issue number or fingerprint")
    x = bsp.add_parser("search", help="bugs by generated title or fingerprint")
    x.add_argument("words", nargs="+")
    x = bsp.add_parser("report", help="file a bug, or vote on a known one")
    x.add_argument("fingerprint", nargs="?", default="")
    x.add_argument("--title", default="", help="a command that misbehaves without an error (held for review)")
    x.add_argument("--guard", nargs="?", const="", default=None, metavar="REFUSAL_ID",
                   help="a wrong refusal (default: this session's last one)")
    x.add_argument("--security", action="store_true", help="for private vulnerability reporting only")
    x = bsp.add_parser("detour", help="record a detour, once the session ledger shows it worked")
    x.add_argument("fingerprint")
    x.add_argument("--hint", dest="steps", action="append", type=lambda v: ("hint", v), metavar="'NAME SLOT=VALUE …'",
                   help="a hint template: use-tool, from-root, one-at-a-time, scratch-tool, sync-retry, ask-update")
    x.add_argument("--dev", dest="steps", action="append", type=lambda v: ("dev", v), metavar="'COMMAND'",
                   help="a dev command of devkit's own subcommands and flags")
    x.add_argument("--text", dest="steps", action="append", type=lambda v: ("text", v), metavar="'TEXT'",
                   help="free text, at most 200 characters (held for review)")
    x = bsp.add_parser("top", help="hotfix recommendations: open bugs by score, with a reason each")
    x.add_argument("--task", action="store_true", help="add the suggested bugs as work items with the issue link")
    bsp.add_parser("sync", help="refresh the cache and send the outbox")
    bsp.add_parser("pending", help="items held for review, printed exactly as they would be sent")
    for verb, h_ in (("send", "send a held item (developer only)"), ("drop", "drop an item (developer only)")):
        bsp.add_parser(verb, help=h_).add_argument("id")
    x = bsp.add_parser("confirm", help="confirm a detour (maintainers; developer only)")
    x.add_argument("bug", help="issue number or fingerprint")
    x.add_argument("detour", help="the detour's id, such as d1")
    x = bsp.add_parser("share", help="consent to post: on, reports (no votes) or off (developer only)")
    x.add_argument("setting", choices=["on", "reports", "off"])
    p = add("sleep", cmd_sleep, "Consolidate and forget: lessons decay unless used, duplicates merge, old records go")
    p.add_argument("--dry-run", action="store_true", help="show what would change")
    p.add_argument("--quiet", action="store_true")
    p = add("jobs", cmd_jobs, "Scheduled jobs: see, run, change, enable or disable; journal and logs")
    jsp = p.add_subparsers(dest="jobs_cmd")
    jsp.add_parser("ls", help="every job with its schedule, last run and next run")
    jsp.add_parser("install", help="write and load the launchd jobs from the config")
    for verb, h_ in (("show", "a job with its recent runs"), ("run", "run a job now"), ("exec", argparse.SUPPRESS),
                     ("enable", "turn a job on"), ("disable", "turn a job off"), ("logs", "a job's log")):
        x = jsp.add_parser(verb, help=h_)
        x.add_argument("name")
        x.add_argument("--why")
        x.add_argument("-n", "--lines", type=int, default=60)
    x = jsp.add_parser("set", help="change a schedule: --every 30m | --at 03:30 | --weekly 'mon 09:00'")
    x.add_argument("name")
    x.add_argument("--every")
    x.add_argument("--at")
    x.add_argument("--weekly")
    x.add_argument("--why")
    x = jsp.add_parser("journal", help="past runs of every job: start, result, duration, last line")
    x.add_argument("--days", type=int, default=7)
    x.add_argument("-n", "--lines", type=int, default=40)
    from . import audit as audit_mod
    audit_mod.add_arguments(add("audit", audit_mod.cmd_audit,
                                "The audit trail: query, verify, export, show, stats and config"))
    p = add("report", cmd_report, "Status report (Markdown): health, progress, done, in progress, next, risks, team")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("-i", "--initiative")
    p.add_argument("-p", "--project")
    p.add_argument("-o", "--out", help="write to a file instead of stdout")
    from . import fs
    fs.register(add)
    from . import stubs as _stubs
    _stubs.register(add)
    add("team", cmd_team, "Who is working on what, across machines and developers").add_argument(
        "--hours", type=float, default=24)
    p = add("initiative", cmd_initiative, "Cross-project initiatives (integrations, platforms, programmes)")
    isp = p.add_subparsers(dest="init_cmd")
    x = isp.add_parser("new", help="create an initiative")
    x.add_argument("slug")
    x.add_argument("--title")
    x.add_argument("--projects", help="comma-separated project ids (owner/repo)")
    for verb in ("add", "rm"):
        x = isp.add_parser(verb, help=f"{verb} a member project")
        x.add_argument("slug")
        x.add_argument("project")
    isp.add_parser("ls", help="list initiatives")
    x = isp.add_parser("show", help="an initiative with its member projects")
    x.add_argument("slug")
    p = add("overview", cmd_overview, "Portfolio digest")
    p.add_argument("--json", action="store_true")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--all", action="store_true", help="include archived projects")
    add("standup", cmd_standup, "Recent work and what is in progress").add_argument("--days", type=int, default=1)
    from . import dashserver
    dashserver.add_arguments(add("dashboard", dashserver.cmd_dashboard, "Open the live dashboard (a local, read-only app)"))
    p = add("config", cmd_config, "The agent settings devkit manages: what differs, why, and the fix")
    p.add_argument("config_cmd", nargs="?", choices=["diff", "repair", "show", "edit", "validate", "lock", "unlock",
                                                     "status", "set"])
    p.add_argument("key", nargs="?", help="set: the key, such as progress.max_parallel")
    p.add_argument("value", nargs="?", help='set: the value, as JSON when it parses (6, true, {"70": 0.5})')
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--check", action="store_true", help="one line and exit 1 when a setting differs (for scripts)")
    p = add("agents", cmd_agents, "Claude Code and Codex: install, sign-in, plan, quota, self-test")
    p.add_argument("action", nargs="?", choices=["status", "test", "rules"], default="status")
    p.add_argument("agent", nargs="?", choices=["claude", "codex", "all"])
    p.add_argument("--section", help="rules: one section, by number (0-12) or words from its title")
    p.add_argument("--json", action="store_true")
    p.add_argument("--model", help="model for the self-test session")
    p.add_argument("-v", "--verbose", action="store_true", help="show the agent's reply")
    add("calver", cmd_calver, "Current and next CalVer version (YY.MM.PATCH)").add_argument(
        "--next", action="store_true", help="print only the next version")
    p = add("plugin", cmd_plugin, "Validate the devkit plugin for Claude Code and Codex")
    p.add_argument("action", choices=["validate"])
    p = add("guard", cmd_guard, "Simulate the guard")
    p.add_argument("check", choices=["check"])
    p.add_argument("command", nargs="*", help="shell command to evaluate")
    p.add_argument("--write", metavar="PATH", help="evaluate a file write instead")
    p.add_argument("--agent", default="claude", choices=["claude", "codex"])
    p.add_argument("--cwd")
    p = add("wt", cmd_wt, "Manage worktrees")
    wsp = p.add_subparsers(dest="wt_cmd", required=True)
    w = wsp.add_parser("new", help="create a managed worktree")
    w.add_argument("branch")
    w.add_argument("--base", help="start point for a new branch (default origin/<default>)")
    w.add_argument("--repo", help="project (default: the one you are in)")
    w.add_argument("--no-deps", action="store_true", help="do not clone node_modules")
    w = wsp.add_parser("ls", help="list worktrees")
    w.add_argument("--all", action="store_true")
    w = wsp.add_parser("rm", help="remove a worktree safely")
    w.add_argument("target", help="branch name or path")
    from . import backup
    p = add("backup", backup.cmd_backup, "Copy branches whose commits exist only on this Mac to the remote, when the "
            "push starts no CI run · policy [on|off|ask]: the project's backup policy")
    backup.register(p)
    p = add("sync", cmd_sync, "Refresh map + notes, commit and push")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.add_argument("--finish", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--offline", action="store_true", help="no network (cached PR data)")
    p.add_argument("--scheduled", action="store_true", help="background mode: refresh PRs and sizes, safe gc")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--renew", metavar="AGENT", help=argparse.SUPPRESS)
    p.add_argument("--delay", type=int, default=0, help=argparse.SUPPRESS)
    from . import disk as _disk
    _disk.add_du_arguments(add("du", cmd_du, "What fills your disk, why, and the plan to free it safely"))
    _disk.add_free_arguments(add("free", cmd_free, "Free items of the dev du plan by number, or every safe one: "
                                                   "asks first, refuses work that is not committed or pushed"))
    p = _disk.add_free_arguments(add("gc", cmd_gc, "Reclaim disk: the same as dev free; --apply runs the unattended "
                                                   "cleanup of the nightly gc job"))
    p.add_argument("--apply", action="store_true", help="the unattended cleanup: merged worktrees, idle build "
                                                         "output, stale temp (asks unless -y)")
    p.add_argument("--auto-only", action="store_true", help="only what is safe without a person (the gc job)")
    p.add_argument("-v", "--verbose", action="store_true", help="with --apply's plan, also what is kept and why")
    p = add("canary", cmd_canary, "Instruction-compliance canary report")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    p = add("ci", cmd_ci, "What CI runs (to run it locally first), `dev ci status`: live runs on any host, "
                          "`dev ci watch`: follow a run to the end and diagnose a failure (`--wait`: for an agent "
                          "that waits on it), `dev ci watches`: the live and expired watches, `dev ci audit`: "
                          "diagnose pipelines, `dev ci init`: set one up")
    p.add_argument("query", nargs="?", help="a project, or `status`, `audit`, `init`, `watch` or `watches`")
    p.add_argument("-p", "--project", help="the project (id or a unique part of its name) for status, audit, init "
                                           "and watch")
    p.add_argument("--sha", help="watch: the commit whose runs to follow (default: HEAD; with --wait, what this "
                                 "session last sent to origin)")
    p.add_argument("--wait", action="store_true",
                   help="watch: for an agent that waits on the run: one line per finished job, then one line with "
                        "the conclusion; exit 0 passed, 1 failed, 3 the watch ended first (re-arm it), 4 none. Run it "
                        "as a Claude Code Monitor or background command, or in the foreground on Codex")
    p.add_argument("--tag", help="watch --wait: the runs of this tag")
    p.add_argument("--pr", help="watch --wait: the runs of this PR's head, or of its merge commit once merged")
    p.add_argument("--timeout", type=float, default=90, help="watch: minutes before giving up (default 90)")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.add_argument("--quiet", action="store_true", help="watch: no progress lines, only the result")
    p.add_argument("--commands", action="store_true", help="print only the run commands")
    p.add_argument("--runs", action="store_true", help="also list recent CI runs")
    p.add_argument("--status", action="store_true", help="live runs with job and step progress and an ETA")
    p.add_argument("--branch", help="runs of this branch (default: the current branch)")
    p.add_argument("--all-branches", action="store_true")
    p.add_argument("--watch", action="store_true", help="refresh every 15 s until no run is in progress")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--all-jobs", action="store_true", help="list every running job")
    p.add_argument("--json", action="store_true")
    from . import watch
    w = add("watch", watch.cmd_watch, "What waits for you in this session, what can still arrive, and the live "
                                      "waits; `--wait`: block until something you must act on arrives, print it once "
                                      "and exit (exit 0 events, 2 nothing can arrive, 3 timeout, 5 superseded)")
    w.add_argument("--wait", action="store_true",
                   help="block until an event arrives for you: Claude Code main session with Bash run_in_background; "
                        "a crew member or Codex in the foreground")
    w.add_argument("--for", dest="for_", choices=("me",), help="me: the caller's own events (its crew lane, or the "
                                                               "main session): the default, and the only value")
    w.add_argument("--only", help="comma list: question,decision,ready,member,ci,notice; take only these kinds")
    w.add_argument("--timeout", type=float, help="minutes before it ends with the command to arm it again (default "
                                                 "240 for the main session on Claude Code, 10 on Codex, 9 for a crew "
                                                 "member)")
    w.add_argument("--standby", action="store_true",
                   help="with --wait: wait for other agent sessions' messages even when nothing else can arrive (a "
                        "session that coordinates others; never required)")
    w.add_argument("--json", action="store_true", help="the peek as JSON (for tools)")
    w = add("watches", watch.cmd_watches, "Every wait (dev watch --wait) and CI watch: live, expired or superseded, "
                                          "with the command to arm it again, and what can still arrive")
    w.add_argument("--json", action="store_true", help="the waits as JSON (for tools)")
    from . import gitx
    gitx.add_ci_arguments(p)
    from . import pipelines
    pipelines.add_ci_arguments(p)
    gitx.register(lambda name, fn, help_, **kw: add(name, cmd_branch if name == "branch" else fn, help_, **kw))
    from . import testx
    testx.register(add)
    add("mcp", cmd_mcp, "Serve devkit's tools over MCP on stdio (the Claude Desktop extension runs this)")
    from . import desktop
    desktop.register(add)
    from . import crew
    crew.register(add)
    from . import ext
    ext.register(add)
    from . import impeccable
    impeccable.register(add)
    add("doctor", cmd_doctor, "Verify the setup")
    p = add("install", cmd_install, "Pin devkit for the hooks, then apply the configuration")
    p.add_argument("--only", help="comma list: config,tools,cli,git,handoff,instructions,skills,claude,codex,desktop,"
                                  "docker,docs,launchd,prompt,shell; re-applies them from the installed copy without "
                                  "pinning")
    p.add_argument("--version", help="switch to a pinned copy kept from an earlier install (rollback), e.g. 26.11.2")
    p.add_argument("--unverified", action="store_true",
                   help="pin HEAD although it is neither a release nor tested (`dev ci --run`); needs --why")
    p.add_argument("--why", help="the reason for --unverified, recorded in the audit")
    p.add_argument("--as-pinned", action="store_true", help=argparse.SUPPRESS)  # the second half, from the copy
    p.add_argument("--yes", "-y", action="store_true",
                   help="go on without asking when Claude Code or Codex sessions that use devkit run (they are "
                        "listed; restart each to load the new hooks)")
    p.add_argument("--commit-template", choices=["yes", "no"],
                   help="dev setup's answer for the commit template: yes sets commit.template in the repositories "
                        "without one, no leaves them; the git step records it")
    p = add("docs", cmd_docs, "Check documentation: Markdown, links and anchors, prose, diagrams")
    p.add_argument("check", choices=["check"])
    p.add_argument("paths", nargs="*", help="files or folders (default: the whole repository)")
    p.add_argument("--online", action="store_true", help="also check external links")
    p.add_argument("--fix", action="store_true", help="apply markdownlint's automatic fixes")
    p.add_argument("--no-prose", action="store_true", help="skip Vale")
    p = add("sec", cmd_sec, "Security scan: secrets, dependencies, SAST, filesystem and IaC")
    p.add_argument("scan", choices=["scan"])
    p.add_argument("query", nargs="?", help="project (default: the one you are in)")
    p.add_argument("--history", action="store_true", help="also scan the git history for secrets")
    p.add_argument("--config", default="p/default", help="Semgrep rule set (default p/default)")
    p.add_argument("-v", "--verbose", action="store_true")
    p = add("diagram", cmd_diagram, "Branded SVG diagrams from JSON specs (render · check · new)")
    p.add_argument("rest", nargs=argparse.REMAINDER, help="render <spec|dir> [--out DIR] [--png] · check <spec|svg|dir> · new <kind> <name>")
    p = add("setup", cmd_setup, "Installation and configuration wizard; dev setup prompt adds the prompt helper, "
                                "dev setup commit-template the commit template")
    p.add_argument("what", nargs="?", choices=["prompt", "commit-template", "shell"],
                   help="prompt: only the prompt helper; commit-template: only the commit template (commit.template); "
                        "shell: only the zsh helpers (fzf pickers and their keys)")
    p.add_argument("--remove", action="store_true", help="with prompt, commit-template or shell: take it out again")
    p.add_argument("-y", "--yes", action="store_true", help="accept every default (non-interactive)")
    p.add_argument("--skip-packages", action="store_true", help="do not install Homebrew/npm packages")
    p.add_argument("--resume", action="store_true",
                   help="continue the last setup: run its steps that are not done, with the saved answers")
    p.add_argument("--preview", action="store_true",
                   help="draw every screen of the setup with sample data, as wide as the terminal (COLUMNS=60 for "
                        "narrow); changes nothing")
    add("prompt", cmd_prompt, "What devkit's prompt helper shows here; refresh writes it for this Mac").add_argument(
        "action", nargs="?", choices=["refresh"])
    p = add("update", cmd_update, "Update devkit: the newest release, or a pull of its branch; --check only tells")
    p.add_argument("--check", action="store_true", help="read-only: the newest release, what changes in it, and "
                                                         "whether the checkout and the pinned copy are behind")
    p.add_argument("--quiet", action="store_true", help="with --check: print nothing (the daily job)")
    p.add_argument("--record", action="store_true", help="with --check: record the result for the session start "
                                                          "and dev status")
    p.add_argument("--yes", "-y", action="store_true",
                   help="go on without asking when Claude Code or Codex sessions that use devkit run (they are "
                        "listed; restart each to load the new hooks)")
    add("docker", cmd_docker, "Docker on Colima").add_argument(
        "action", choices=["up", "down", "status", "trim", "prune", "autostart-on", "autostart-off"])
    add("version", cmd_version, "Show version")
    p = add("completion", cmd_completion, "Tab completion for dev, generated from its own parser: zsh or bash")
    p.add_argument("shell", choices=["zsh", "bash"], help="the shell to print the completion script for")
    p = add("hook", cmd_hook, argparse.SUPPRESS)
    p.add_argument("event")
    p.add_argument("--agent", default="claude", choices=["claude", "codex"])
    from . import completion
    completion.add_complete_arguments(add("__complete", cmd_complete, argparse.SUPPRESS))
    return ap


def _crashed(argv, exc: BaseException) -> int:
    """A command raised: devkit's bug registry reports it (bugs.handle_crash). Exit 70 even when that module cannot
    load, with one plain line instead of a second traceback."""
    try:
        from . import bugs
    except Exception:
        print("devkit bug: internal error while reporting", file=sys.stderr)
        return 70
    return bugs.handle_crash(argv, exc)


def main(argv=None) -> int:
    global _ARGV
    argv = sys.argv[1:] if argv is None else argv
    _ARGV = list(argv)
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.help:  # `dev -h <command>` shows the command's help and never runs it (B1: the guard reads it as help)
        if a.cmd:
            with contextlib.suppress(SystemExit):
                ap.parse_args([a.cmd, "--help"])
        else:
            print_help()
        return 0
    if not a.cmd:
        return cmd_home(a) or 0
    if getattr(a, "args", None) and a.args[:1] == ["--"]:
        a.args = a.args[1:]
    from . import audit
    track = audit.audited(argv)
    start = time.time()
    rc = 1
    try:
        rc = a.fn(a) or 0
        return rc
    except KeyboardInterrupt:
        rc = 130
        return 130
    except Exception as e:  # devkit's own bug: a notice with its marker and exit 70, never a traceback
        rc = _crashed(argv, e)
        return rc
    finally:
        if track:
            try:
                here = core.tilde(os.getcwd())
            except OSError:  # the folder was removed while the command ran (scratch folders)
                here = "(removed folder)"
            # the arguments as a list, one word each, so the masker reads each argument whole (Code reviewer, M3)
            audit.record("dev." + argv[0], list(argv[1:]), "", "ok" if rc == 0 else f"exit {rc}",
                         duration=round(time.time() - start, 2), cwd=here)
            if rc == 0 and argv[0] in ("commit", "push", "pr", "release", "branch", "rebase", "wt", "adopt", "clone",
                                       "new", "delivery", "initiative", "note"):
                from . import work as _w
                _w.request_sync(f"dev {' '.join(argv[:2])}")
