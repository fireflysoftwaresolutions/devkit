# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev` command line: argument parsing and command implementations."""
from __future__ import annotations

import argparse
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
        ("ls", "List every project: stack, branch, state, worktrees, PRs, activity, size"),
        ("go <query>", "Print a project's path (shell: `dcd <query>` jumps there)"),
        ("status", "Status of the project you are in, with what needs attention"),
        ("clone <owner/repo|url>", "Clone by host/owner/repo (GitHub, GitLab, Bitbucket, any git URL)"),
        ("new <name> [--github]", "Start a project in _local/<name>, or create it on GitHub"),
        ("adopt <path>", "Move a misplaced repo into the canonical layout (fixes worktrees + agent history)"),
        ("open [query]", "Open a project in your editor"),
    ]),
    ("Portfolio", [
        ("dashboard", "Open the HTML command centre: projects, tasks, activity, explorer, health"),
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
        ("jobs [show|run|set|enable|disable|journal]", "Scheduled jobs (sync, sleep, gc, repair, audit, doctor, lessons)"),
        ("audit [--days 7] [--team]", "The audit trail: every change, decision and job run, per machine and team"),
        ("sleep [--dry-run]", "Consolidate and forget (daily): unused lessons fade, duplicates merge, old records go"),
        ("report [--days 7]", "Status report for initiatives and projects (Markdown, for the team or management)"),
    ]),
    ("Agents", [
        ("claude [query] [-w branch]", "Start Claude Code in a project (optionally in its own worktree)"),
        ("codex [query] [-w branch]", "Start Codex in a project (optionally in its own worktree)"),
        ("handoff [query] [--edit|--log …]", "Show, edit or append to the project's handoff note"),
    ]),
    ("Worktrees", [
        ("wt new <branch>", "Create a managed worktree in ~/Development/.worktrees/<owner>/<repo>/<branch>"),
        ("wt ls [--all]", "List worktrees with PR state, live sessions and size"),
        ("wt rm <branch|path>", "Remove a worktree safely (refuses if work would be lost)"),
    ]),
    ("Delivery", [
        ("ci [--commands] · audit · init",
         "What CI runs (run it locally first); `ci audit [--fix]` diagnoses pipelines on any host; `ci init` sets one up"),
        ("branch new|rename|ls|clean", "Branches from an up-to-date main, with valid names; tidy merged ones"),
        ("commit · push · rebase", "Conventional commits, push after local CI evidence (dev ci --run), rebase on main"),
        ("pr create|ls|checks|merge", "PRs with the template, checks with failing logs, safe merges"),
        ("release [--dry-run]", "Tag and publish by the delivery policy, with the CHANGELOG section"),
        ("run ls|view --failed|rerun", "CI runs, failing logs trimmed; rerun only with --flaky \"reason\""),
        ("issue ls|view|create", "Issues, compact"),
        ("git … · gh …", "Escape hatches to git and gh, with the same policy checks"),
        ("stubs [--staged|--all]", "Unfinished work: stubs, placeholders, TODOs without a task (also checked on commit)"),
        ("crew [show|brief|route|delegate|next|ask|mode|decide]", "The specialist agents: who does what, briefs, routing, delegation, decisions"),
        ("ci status [--watch]", "Live CI/CD runs (GitHub, GitLab, Bitbucket, Azure): jobs, steps, elapsed, ETA"),
        ("calver [--next]", "Current and next CalVer version YY.MM.PATCH from the repository's tags"),
        ("sec scan [--history]", "Security: secrets (gitleaks), dependencies (osv-scanner), SAST (Semgrep), Trivy"),
        ("docs check [--online]", "Check docs: Markdown structure, links and anchors, prose (Vale), diagrams"),
        ("diagram render|check|new", "Branded, validated SVG diagrams (flow, C4, state, sequence, ER, timeline)"),
    ]),
    ("Tracking and disk", [
        ("canary", "How often Claude/Codex replies carried the “[Firefly> DevKit]” instruction marker"),
        ("sync", "Refresh the projects map and handoff notes, commit and push the handoff repo"),
        ("gc [--apply]", "Show (or reclaim) merged worktrees, stale build output and temp files"),
        ("du", "Disk usage of projects, worktrees, agent data, temp and Docker"),
    ]),
    ("Environment", [
        ("setup [--yes]", "The installation and configuration wizard (first install, or to change choices)"),
        ("doctor", "Verify the whole setup (git, gh, Claude, Codex, skills, Docker, automation)"),
        ("agents [test [claude|codex]]", "Claude Code and Codex: installed, signed in, plan, quota; live self-tests"),
        ("config diff|repair|lock|…", "Preview and enforce the agent settings devkit manages"),
        ("guard check <cmd>|--write p", "Simulate what the guard decides for a command or a file write"),
        ("plugin validate", "Validate the devkit plugin with Claude Code's validator and Codex's parser"),
        ("desktop pack|status", "Claude Desktop extension: build the .mcpb bundle, check what is installed"),
        ("install [--only step]", "Apply the devkit configuration (idempotent)"),
        ("update", "Pull the latest devkit and re-apply it"),
        ("docker up|down|status|prune", "Run Docker on a Colima VM"),
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


def _resolve(reg, query, cwd=None):
    """Project from a query, or from cwd when no query. Exits with a message when ambiguous/unknown."""
    if not query:
        p, w = registry.project_for_cwd(reg, cwd or os.getcwd())
        if not p:
            ui.err(f"not inside a project ({tilde(os.getcwd())}); pass a project name")
            sys.exit(2)
        return p
    found = registry.find(reg, query)
    if len(found) == 1:
        return found[0]
    if not found:
        ui.err(f"no project matches “{query}” — `dev ls` lists them")
        sys.exit(2)
    if shutil.which("fzf") and sys.stdin.isatty() and sys.stderr.isatty():
        import subprocess
        r = subprocess.run(["fzf", "--height=40%", "--reverse", f"--query={query}", "--prompt=project> "],
                           input="\n".join(p["id"] for p in found), capture_output=True, text=True)
        pick = r.stdout.strip()
        for p in found:
            if p["id"] == pick:
                return p
        sys.exit(130)
    ui.err(f"“{query}” matches several projects: " + ", ".join(p["id"] for p in found))
    sys.exit(2)


def _state(co):
    s = registry._state(co)
    return ui.green(s) if s == "clean" else ui.yellow(s)


def commit_push(push=True, quiet=False):
    if not (HANDOFF / ".git").exists():
        return
    core.git(HANDOFF, "add", "-A")
    rc, _, _ = core.git(HANDOFF, "diff", "--cached", "--quiet")
    if rc != 0:
        n = len(core.gitout(HANDOFF, "diff", "--cached", "--name-only").splitlines())
        core.git(HANDOFF, "commit", "-q", "-m", f"sync: {n} file(s) from {os.uname().nodename.split('.')[0]}")
    if not push or not core.gitout(HANDOFF, "remote"):
        return
    from . import audit
    failed = STATE / "handoff-push-failed.json"
    core.git(HANDOFF, "fetch", "-q", "origin", timeout=60)
    if core.gitout(HANDOFF, "rev-list", "--count", "HEAD..@{u}") not in ("", "0"):
        core.git(HANDOFF, "pull", "-q", "--rebase", "--autostash", timeout=120)
    ahead = core.gitout(HANDOFF, "rev-list", "--count", "@{u}..HEAD")
    if ahead not in ("", "0"):
        rc, _, err = core.git(HANDOFF, "push", "-q", timeout=120)
        if rc != 0:  # someone pushed in between: rebase on it and try once more
            core.git(HANDOFF, "pull", "-q", "--rebase", "--autostash", timeout=120)
            rc, _, err = core.git(HANDOFF, "push", "-q", timeout=120)
        if rc == 0:
            audit.record("handoff.push", core.handoff_repo(), f"{ahead} commit(s)")
            if failed.exists():
                failed.unlink()
        else:
            audit.record("handoff.push", core.handoff_repo(), err[:300], "failed")
            failed.write_text(json.dumps({"ts": int(time.time()), "error": err[:300]}))
            if not quiet:
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

def cmd_ls(a):
    reg = _reg(prs=not a.offline)
    rows = []
    for p in reg["projects"]:
        co = p["main"]
        act = sum(1 for w in p["worktrees"] if w.get("active")) + (1 if co.get("active") else 0)
        rows.append([ui.bold(p["id"]) if p["canonical_ok"] else ui.yellow(p["id"] + " *"),
                     ", ".join(p["stack"]) or "-", co.get("branch") or "-", _state(co) if co else "-",
                     str(len(p["worktrees"])) if p["worktrees"] else "-",
                     str(len(p["prs_open"])) if p.get("prs_known") else "-",
                     (ui.ACTIVE + " " if act else "") + ago(p.get("last_activity")), human_kb(p.get("size_kb"))])
    if a.names:
        print("\n".join(p["id"] for p in reg["projects"]))
        return 0
    print(ui.table(rows, ["PROJECT", "STACK", "BRANCH", "STATE", "WT", "PRS", "ACTIVITY", "SIZE"],
                   max_widths={1: 28, 2: 30}))
    if any(not p["canonical_ok"] for p in reg["projects"]):
        print(f"\n  {ui.yellow('*')} {ui.dim('not in the canonical layout — `dev adopt <path>`')}")
    return 0


def cmd_go(a):
    p = _resolve(_reg(prs=False), a.query)
    print(p["path"])
    return 0


def _print_status(p, w=None):
    co = w or p["main"]
    print(ui.section(p["id"]))
    print(ui.kv("path", tilde(w["path"] if w else p["path"]) + ("" if p["canonical_ok"] else ui.yellow(f"  (canonical: {tilde(p['canonical'])})"))))
    if p.get("remote"):
        print(ui.kv("remote", p["remote"]))
    print(ui.kv("stack", ", ".join(p["stack"]) or "-"))
    if p.get("commands"):
        print(ui.kv("commands", " · ".join(p["commands"])))
    print(ui.kv("branch", f"{co.get('branch')} · {_state(co)}" + (f" · PR #{co['pr']['number']} {co['pr']['state'].lower()}" if co.get("pr") else "")))
    if co.get("last_commit"):
        print(ui.kv("last commit", f"{core.fmt_ts(co.get('last_commit_ts'))} {co.get('last_commit')[:70]}"))
    if p["worktrees"]:
        live = sum(1 for x in p["worktrees"] if x.get("active"))
        print(ui.kv("worktrees", f"{len(p['worktrees'])} ({live} live) — dev wt ls"))
    if p.get("prs_open"):
        print(ui.kv("open PRs", ", ".join(f"#{x['number']}" for x in p["prs_open"][:10])))
    print(ui.kv("handoff", tilde(core.note_path(p["id"]))))
    try:
        from . import cistatus
        res = cistatus.runs_for(w["path"] if w else p["path"], co.get("branch"), limit=1, detail=True, max_age=60)
        if res.get("runs"):
            r0 = res["runs"][0]
            print(ui.kv("CI", cistatus.describe(r0)))
            if r0.get("jobs"):
                print(" " * 17 + cistatus.job_summary(r0["jobs"]))
                shown, more = cistatus.jobs_to_show(r0["jobs"], 3)
                for j in shown:
                    print(" " * 17 + cistatus.describe_job(j))
        elif res.get("error") and res.get("provider"):
            print(ui.kv("CI", ui.dim(res["error"][:100])))
    except Exception:
        pass
    sl = p.get("status_line") or registry.status_line(p["id"])
    if sl and sl.get("line"):
        print(ui.kv("last status", ui.dim(f"{sl.get('agent', '')} · {ago(sl.get('ts'))}")))
        print(ui.status_block(sl["line"]))


def cmd_status(a):
    reg = _reg(prs=True)
    p, w = registry.project_for_cwd(reg, os.getcwd()) if not a.query else (_resolve(reg, a.query), None)
    if not p:
        return cmd_home(a)
    _print_status(p, w)
    mine = [m for k, m in reg["issues"] if p["id"] in m or tilde(p["path"]) in m]
    if mine:
        print(ui.section("Needs attention"))
        for m in mine:
            print(f"  {ui.WARN} {m}")
    print()
    return 0


def _trust_codex(path: Path):
    try:
        from .codexrpc import CodexRPC
        with CodexRPC(timeout=15) as rpc:
            rpc.write([("projects", {str(path): {"trust_level": "trusted"}}, "upsert")])
    except Exception:
        pass


def cmd_clone(a):
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
    if core.real(old_main) == core.real(core.DEVKIT_HOME):
        import subprocess  # devkit moved itself: its paths are embedded in hooks, links and launchd — re-apply them
        print(f"  {ui.ARROW} devkit moved itself; re-applying its configuration from {tilde(dest)}")
        return subprocess.call([sys.executable, str(dest / "bin" / "dev"), "install"])
    return cmd_sync(argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=False,
                                       quiet=True))


def cmd_open(a):
    p = _resolve(_reg(prs=False), a.query)
    editor = CFG.get("editor") or os.environ.get("EDITOR", "code")
    return run([editor, p["path"]])


# --------------------------------------------------------------------------- agents

def _launch(tool: str, a):
    reg = _reg(prs=False)
    p = _resolve(reg, a.query)
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


def cmd_wt(a):
    reg = _reg(prs=a.wt_cmd == "ls")
    if a.wt_cmd == "new":
        from .guard import branch_violation
        v = branch_violation(a.branch)
        if v:
            ui.err(v)
            return 2
        p = _resolve(reg, a.repo)
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
                    ok, msg = gc.remove_worktree(p["path"], w["path"], w.get("branch"), p.get("github"))
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
        with core.Lock("notes", wait=60):
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
            results = gc.apply(gc.plan(reg, with_sizes=True), only_auto=True)  # only when the gc job is not installed
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
            from . import sleep as _sleep
            if a.scheduled and _sleep.due() and not (core.HOME / "Library" / "LaunchAgents" /
                                                     "com.devkit.job.sleep.plist").exists():
                _sleep.run(quiet=True)  # only when the sleep job is not installed
        except Exception as e:
            if not a.quiet:
                ui.warn(f"sleep: {e}")
        try:
            from . import work
            if a.renew:
                work.renew_locks(work.actor(a.renew))
            if a.scheduled:
                work.pull()
                work.expire_locks()
            work.write_presence(reg.get("sessions") or [])
        except Exception as e:
            if not a.quiet:
                ui.warn(f"work items: {e}")
        with core.Lock("notes", wait=60):
            changed = registry.write_outputs(reg)
        try:
            from . import dashboard
            dashboard.write(reg)
        except Exception as e:  # never let the view break the sync
            if not a.quiet:
                ui.warn(f"dashboard: {e}")
        if a.session and a.finish:
            agent, _, sid = a.session.partition(":")
            for suffix in (".json", ".stop"):
                f = STATE / "sessions" / f"{agent}-{sid}{suffix}"
                if f.exists():
                    f.unlink()
        commit_push(push=CFG["sync"].get("push", True) and not a.no_push, quiet=a.quiet)
        (STATE / "last-sync.json").write_text(json.dumps({"ts": time.time(), "changed": changed,
                                                         "gc": [m for _, m in results]}))
        if a.scheduled:
            from . import canary
            canary.prune()
            _rotate_log(STATE / "logs" / "sync.log")
            _expire_sessions()
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
    print(ui.section("Scanning (PR states refreshed from GitHub)"))
    reg = registry.build(with_prs=True, refresh_prs=True, with_sizes=True)
    pl = gc.plan(reg, with_sizes=True)
    if pl["actions"]:
        print(ui.section(f"Reclaimable: {human_kb(pl['reclaimable_kb'])}"))
        for x in pl["actions"]:
            label = {"remove-worktree": "remove worktree", "prune-artefacts": "prune build output",
                     "purge-temp": "purge temp", "prune-meta": "prune metadata"}[x["kind"]]
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
    for ok, msg in gc.apply(pl, only_auto=getattr(a, "auto_only", False)):
        print(f"  {ui.OK if ok else ui.WARN} {msg}")
    return cmd_sync(argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=False,
                                       quiet=False, renew=None))


def cmd_du(a):
    reg = registry.build(with_prs=False, with_sizes=True, refresh_sizes=a.refresh)
    rows = []
    for p in sorted(reg["projects"], key=lambda x: -(x.get("size_kb") or 0)):
        wt_kb = sum(w.get("size_kb") or 0 for w in p["worktrees"])
        rows.append([p["id"], human_kb(p.get("size_kb")), human_kb(wt_kb) if p["worktrees"] else "-",
                     str(len(p["worktrees"]))])
    print(ui.section("Projects (main checkout · its worktrees)"))
    print(ui.table(rows, ["PROJECT", "MAIN", "WORKTREES", "N"]))
    print(ui.section("Agent data, temp, caches"))
    extra = [("Claude transcripts (~/.claude/projects)", core.CLAUDE_HOME / "projects"),
             ("Claude temp", Path(core.real("/tmp")) / f"claude-{os.getuid()}"),
             ("Codex home (~/.codex)", core.CODEX_HOME), ("devkit temp (~/Development/.tmp)", core.TMP_ROOT),
             ("npm cache", core.HOME / ".npm"), ("Homebrew cache", core.HOME / "Library" / "Caches" / "Homebrew"),
             ("Colima VM disk", core.HOME / ".colima")]
    for label, path in extra:
        if path.exists():
            print(ui.kv(label, human_kb(core.du_kb(path, timeout=120)), key_width=42))
    if core.have("docker") and core.sh(["docker", "info"], timeout=10)[0] == 0:
        print(ui.section("Docker"))
        print("\n".join("  " + ln for ln in core.sh(["docker", "system", "df"], timeout=30)[1].splitlines()))
    return 0


# --------------------------------------------------------------------------- environment

def cmd_doctor(a):
    from . import setup
    return setup.doctor()


def cmd_install(a):
    from . import setup
    setup.run_install(only=set(a.only.split(",")) if a.only else None)
    return 0


def cmd_update(a):
    print(f"{ui.ARROW} updating {tilde(core.DEVKIT_HOME)}")
    rc, out, err = core.git(core.DEVKIT_HOME, "pull", "--ff-only", timeout=120)
    print(f"  {ui.OK if rc == 0 else ui.FAIL} {out or err}")
    if rc != 0:
        return rc
    os.execv(sys.executable, [sys.executable, str(core.DEVKIT_HOME / "bin" / "dev"), "install"])


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
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if a.query and a.query not in ("status", "audit", "init", "watch"):
        top = _resolve(_reg(prs=False), a.query)["path"]
    if getattr(a, "project", None):
        top = _resolve(_reg(prs=False), a.project)["path"]
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
        text = f.read_text()
        meta, _ = notes.split_frontmatter(text)
        cur = [p.lower() for p in (meta.get("projects") or [])]
        pid = a.project.lower()
        cur = sorted(set(cur) | {pid}) if act == "add" else [p for p in cur if p != pid]
        text = notes.set_meta(text, projects=cur)
        if act == "add" and f"### {pid}" not in text:
            text = text.replace("## Tasks\n", f"### {pid}\n_Role in the initiative, state, next step._\n\n## Tasks\n", 1)
        with core.Lock("notes", wait=30):
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


def _print_items(scope, label, show_all=False):
    from . import work
    items = work.all_items(scope)
    ordered = work.ordered_open(items)
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
        print(f"  {ui.cyan(str(n).rjust(2))} {ui.dim(m['id'])} {st:<8} {m['title']}{pr}{lock} {ui.dim('v' + str(m['version']))}")
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
            print(f"{ui.OK} {it['meta']['id']} added to {bucket} · {label}")
            if "_" in body and not a.body:
                print(ui.dim(f"  fill in the description: dev task edit {it['meta']['id']} --body-file <file> --why \"…\""))
            if a.claim:
                work.claim(it, why=a.why or "")
                print(f"{ui.OK} claimed by you")
        elif act in ("start", "claim"):
            it = work.claim(work.find(" ".join(a.selector), scope), why=a.why or "")
            print(f"{ui.OK} {it['meta']['id']} claimed until {it['meta']['lock_expires']} — others see it as yours")
        elif act == "release":
            it = work.release(work.find(" ".join(a.selector), scope), why=a.why or "", status="todo")
            print(f"{ui.OK} {it['meta']['id']} released")
        elif act == "done":
            it = work.find(" ".join(a.selector), scope)
            it = work.release(it, why=a.why or "", status="done")
            print(f"{ui.OK} {it['meta']['id']} done")
        elif act == "block":
            it = work.update(work.find(" ".join(a.selector), scope), "block", why=a.why or "", status="blocked")
            print(f"{ui.OK} {it['meta']['id']} blocked")
        elif act == "unblock":
            it = work.find(" ".join(a.selector), scope)
            holder, expired = work.lock_holder(it)
            it = work.update(it, "unblock", why=a.why or "", status="doing" if holder and not expired else "todo")
            print(f"{ui.OK} {it['meta']['id']} unblocked ({it['meta']['status']})")
        elif act == "link":
            it = work.find(" ".join(a.selector), scope)
            tag = f"initiative:{a.to}"
            if not registry.initiative_path(a.to).exists():
                ui.err(f"no initiative “{a.to}” — `dev initiative ls`")
                return 2
            links = list(dict.fromkeys((it["meta"].get("links") or []) + [tag]))
            it = work.update(it, "link", why=a.why or "", links=links)
            print(f"{ui.OK} {it['meta']['id']} counts toward initiative {a.to}")
        elif act == "cancel":
            it = work.find(" ".join(a.selector), scope)
            it = work.update(it, "cancel", why=a.why or "", status="cancelled", claimed_by="", claimed_at="",
                             lock_expires="")
            print(f"{ui.OK} {it['meta']['id']} cancelled")
        elif act == "move":
            it = work.update(work.find(" ".join(a.selector), scope), "move", why=a.why or "", bucket=a.to)
            print(f"{ui.OK} {it['meta']['id']} → {a.to}")
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
            print(f"{ui.OK} {it['meta']['id']} v{it['meta']['version']}")
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
    _print_items(scope, label, getattr(a, "all", False))
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
        run = pg.start(" ".join(a.goal), a.steps or len(names), a.estimate or "", plan=names, from_agent=from_agent,
                       mode=getattr(a, "mode", "") or "")
        if from_agent:
            print(f"{ui.OK} lane plan recorded · {' '.join(a.goal)}")
            return 0
        cal = pg.calibration(run.get("project"))
        print(f"{ui.OK} run started · {run['goal']} · {pg.mode_of(run)} mode" + (f" · {a.steps} steps" if a.steps else "")
              + (f" · estimate {pg.fmt_min(run['estimate'])}" if run["estimate"] else ""))
        hints = []
        if cal["ratio"] and run["estimate"]:
            hints.append(f"runs here take {cal['ratio']}× their estimate → plan for ~{pg.fmt_min(run['estimate_cal'])}")
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
                          from_agent=from_agent, force=getattr(a, "force", False), why=getattr(a, "why", "") or "")
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
    if act in ("assign", "begin", "add", "reopen", "skip"):
        try:
            if act == "assign":
                run = pg.assign(a.which, " ".join(a.to), scope=getattr(a, "scope", "") or "", from_agent=from_agent,
                                why=getattr(a, "why", "") or "")
            elif act == "begin":
                run = pg.begin(a.which, from_agent=from_agent)
            elif act == "add":
                run = pg.add_step(" ".join(a.spec), why=getattr(a, "why", "") or "", from_agent=from_agent)
                if from_agent:
                    print(f"{ui.OK} added to your lane's plan · {' '.join(a.spec)}")
                    return 0
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
              f"{p_.get('step')}/{p_.get('steps') or '?'}" + (f" · ETA ~{p_['eta_min']}m" if p_.get("eta_min") else ""))
    print()
    return 0


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


def cmd_lesson(a):
    from . import lessons
    act = a.lesson_cmd or "ls"
    try:
        if act == "add":
            scope = _lesson_scope(a)
            les = lessons.add(scope, " ".join(a.title), kind=a.kind, severity=a.severity, happened=a.happened or "",
                              cause=a.cause or "", fix=a.fix or "", prevention=a.prevent or "",
                              detection=a.detect or "", evidence=a.evidence or [], tags=a.tag or [], why=a.why or "",
                              retire_when=a.retire_when or "")
            print(f"{ui.OK} {les['meta']['id']} recorded in {scope} → {tilde(les['path'])}")
            print(ui.dim("  automate it when you can: dev lesson prevent " + les["meta"]["id"] +
                         " --by test|lint|guard|skill|ci --ref … --why …"))
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
                les = lessons.prevent(les, a.by, a.ref, a.why or "")
                print(f"{ui.OK} {les['meta']['id']} prevented by {a.by}: {a.ref}")
                return 0
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
            r = lessons.review()
            print(ui.section(f"Lessons · {r['total']} · " + ", ".join(f"{v} {k}" for k, v in sorted(r["counts"].items()))))
            if r["recurring"]:
                print(ui.section("Recurring and not prevented — automate these"))
                for x in r["recurring"]:
                    print(f"  {ui.WARN} {x['meta']['id']} ×{x['meta']['occurrences']} {x['meta']['title']}  "
                          f"{ui.dim(x['meta']['scope'])}")
            if r["stale"]:
                print(ui.section("Not seen for 4 months — retire or confirm"))
                for x in r["stale"]:
                    print(f"  {ui.dim('·')} {x['meta']['id']} {x['meta']['title']}  {ui.dim('last ' + x['meta']['last_seen'])}")
            if not r["recurring"] and not r["stale"]:
                print(f"  {ui.OK} nothing to review")
            return 0
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
        print("  none yet — record one after a fix, a revert or a repeated mistake: dev lesson add \"…\" --cause … --prevent …")
    for x in sorted(items, key=lessons._score, reverse=True):
        m = x["meta"]
        st = {"open": ui.yellow("open"), "prevented": ui.green(f"prevented·{m.get('prevention')}"),
              "retired": ui.dim("retired")}.get(m.get("status"), m.get("status"))
        print(f"  {ui.dim(m['id'])} {st:<20} ×{m['occurrences']} {m['title']}  {ui.dim(m['scope'] + ' · ' + m['severity'])}")
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


def cmd_audit(a):
    from . import audit
    if a.publish:
        changed = audit.publish()
        audit.prune()
        if changed:
            from . import work
            work._commit(changed, f"audit: {len(changed)} file(s) from {audit.actor()['host']}")
        if not a.quiet:
            print(f"{ui.OK} audit published ({len(changed)} file(s) changed)")
        return 0
    rs = audit.records(a.days, a.action, a.actor, a.team)
    if a.json:
        print(json.dumps(rs, indent=1, ensure_ascii=False))
        return 0
    print(ui.section(f"Audit · last {a.days} days" + (" · team" if a.team else "") + f" · {len(rs)} record(s)"))
    for r in rs[-a.lines:]:
        bad = r.get("outcome") not in ("ok", None)
        who = f"{r.get('user')}@{r.get('host')} {r.get('agent')}"
        print(f"  {ui.dim(core.fmt_ts(r['ts']))} {(ui.red if bad else ui.cyan)(r.get('action', ''))[:40]:<28} "
              f"{r.get('target', '')[:70]}  {ui.dim(who)}" + (f"  {ui.red(r['outcome'])}" if bad else ""))
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
    try:
        text = notes.set_meta(f.read_text(), **kv)
    except ValueError as e:
        ui.err(str(e))
        return 2
    with core.Lock("notes", wait=30):
        core.write_if_changed(f, text)
    meta, _ = notes.split_frontmatter(text)
    print(f"{ui.OK} {p['id']}: " + " · ".join(f"{k} {v}" for k, v in meta.items() if v))
    return 0


def overview_data(days=7):
    reg = registry.build(with_prs=True, refresh_prs=False, pr_max_age=10 ** 9)  # fresh, local, cached PR data
    from . import canary
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
    print(ui.section(f"Portfolio · {len(data.get('initiatives', []))} initiatives · {len(data['projects'])} projects"
                     f" · canary {data['canary']}"))
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


def cmd_dashboard(a):
    try:
        from . import dashboard
    except ImportError:
        ui.err("the HTML dashboard module is not installed in this devkit version")
        return 1
    reg = _reg(prs=True)
    path = dashboard.write(reg)
    if a.path or a.no_open:
        print(path)
    if not a.no_open and not a.path:
        dashboard.open_in_browser(path)
        print(f"{ui.OK} dashboard → {tilde(path)}")
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
    drift = policy.diff()
    print(ui.section("devkit-managed settings: desired (devkit config) vs installed"))
    if not drift:
        print(f"  {ui.OK} everything matches · lock {policy.lock_status()}")
        return 0
    cur_t = None
    for d in drift:
        if d.target != cur_t:
            cur_t = d.target
            print(f"\n  {ui.bold(cur_t)}")
        cur = ui.dim("missing") if d.current is policy.MISSING else ui.red(json.dumps(d.current, ensure_ascii=False)[:60])
        print(f"   {ui.yellow('~')} {d.key:<48} {cur} {ui.ARROW} {ui.green(json.dumps(d.desired, ensure_ascii=False)[:60])}")
    print(f"\n  {ui.dim(f'{len(drift)} difference(s) — `dev config repair` restores them; edits belong in devkit config')}")
    return 1


def _config_validate() -> int:
    from .core import DEFAULTS
    errors = []
    cfg = core.read_json(core.CONFIG_FILE, None)
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
    from . import wizard
    return wizard.run(assume_yes=a.yes, skip_packages=a.skip_packages)


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
    print(f"devkit {core.VERSION} · {tilde(core.DEVKIT_HOME)}\n{COPYRIGHT}")
    return 0


def cmd_hook(a):
    from . import hooks
    return hooks.run(a.event, a.agent)


def cmd_mcp(a):
    from . import mcp
    return mcp.serve()


def cmd_complete(a):
    if a.kind == "projects":
        reg = core.read_json(HANDOFF / "registry.json", {}) or {}
        print("\n".join(p["id"] for p in reg.get("projects", [])))
    elif a.kind == "branches":
        reg = core.read_json(HANDOFF / "registry.json", {}) or {}
        print("\n".join(sorted({w.get("branch") for p in reg.get("projects", []) for w in p.get("worktrees", [])
                                if w.get("branch")})))
    elif a.kind == "commands":
        print("\n".join(c.split()[0] for _, cmds in COMMANDS for c, _ in cmds))
    return 0


# --------------------------------------------------------------------------- parser

def build_parser():
    ap = argparse.ArgumentParser(prog="dev", add_help=False)
    ap.add_argument("-h", "--help", action="store_true")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_, **kw):
        p = sp.add_parser(name, help=help_, description=help_, **kw)
        p.set_defaults(fn=fn)
        return p

    add("help", lambda a: print_help(), "Show help")
    p = add("ls", cmd_ls, "List projects")
    p.add_argument("--offline", action="store_true", help="use cached PR data only")
    p.add_argument("--names", action="store_true", help="print project ids only")
    add("go", cmd_go, "Print a project's path").add_argument("query")
    add("status", cmd_status, "Project status").add_argument("query", nargs="?")
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
                    ("cancel", "cancel (needs --why)"),
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
        if verb == "done":
            x.add_argument("--why", help="end the run although an Accountable member has not approved (recorded)")
    x = gsp.add_parser("assign", help="hand a plan step to a subagent: the one launched with this description owns it")
    x.add_argument("which", help="step number or part of its name")
    x.add_argument("--to", nargs="+", required=True, help="the subagent's description or name, e.g. Neo")
    x.add_argument("--scope", help="the files it owns, comma-separated globs; the guard keeps its edits inside them")
    x.add_argument("--why", help="assign a member the crew's RACI does not make Responsible (recorded)")
    x = gsp.add_parser("begin", help="mark a plan step as started (parallel work you do yourself)")
    x.add_argument("which", help="step number or part of its name")
    x = gsp.add_parser("add", help="add a step found during the work: \"name=30m after other\"")
    x.add_argument("spec", nargs="+")
    x.add_argument("--why", help="why it was not in the plan")
    for verb, h_ in (("reopen", "undo a wrong close: the step goes back to running"),
                     ("skip", "skip a step on purpose")):
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
    x.add_argument("--happened")
    x.add_argument("--cause", help="the root cause (required)")
    x.add_argument("--fix")
    x.add_argument("--prevent", help="the rule that stops it happening again (required)")
    x.add_argument("--detect", help="how to notice it early")
    x.add_argument("--evidence", action="append", help="commit, PR, CI run, task (repeat)")
    x.add_argument("--tag", action="append")
    x.add_argument("--retire-when", help="the outside condition that makes it obsolete (a tool version, an issue)")
    x.add_argument("--why")
    for verb, h_ in (("show", "a lesson with its history"), ("recur", "it happened again (counts repeats)"),
                     ("retire", "no longer applies (needs --why)"), ("restore", "bring back a forgotten lesson")):
        x = lsp.add_parser(verb, help=h_)
        x.add_argument("selector", nargs="+")
        x.add_argument("--evidence")
        x.add_argument("--why")
    x = lsp.add_parser("prevent", help="automation now prevents it (test, lint, guard, skill, ci, rule)")
    x.add_argument("selector", nargs="+")
    x.add_argument("--by", required=True, choices=["test", "lint", "type", "guard", "skill", "ci", "rule", "doc"])
    x.add_argument("--ref", required=True, help="the test, rule or file that prevents it")
    x.add_argument("--why")
    x = lsp.add_parser("scan", help="signals worth a lesson: reverts, fixes, repeated guard refusals, canary misses")
    x.add_argument("--days", type=int, default=14)
    lsp.add_parser("review", help="recurring lessons to automate, stale ones to retire")
    lsp.add_parser("ls", help="lessons for this project, its initiatives and global ones")
    for sub in lsp.choices.values():
        sub.add_argument("-p", "--project", default=argparse.SUPPRESS)
        sub.add_argument("-i", "--initiative", default=argparse.SUPPRESS)
        sub.add_argument("--global", dest="global_", action="store_true", default=argparse.SUPPRESS)
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
    p = add("audit", cmd_audit, "The audit trail: every change, decision and job run devkit made")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--action", help="only actions starting with this, e.g. guard, job, task, rm")
    p.add_argument("--actor", help="user@host or agent")
    p.add_argument("--team", action="store_true", help="every machine (from the handoff repository)")
    p.add_argument("--json", action="store_true")
    p.add_argument("-n", "--lines", type=int, default=60)
    p.add_argument("--publish", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--quiet", action="store_true")
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
    p = add("dashboard", cmd_dashboard, "Open the HTML dashboard")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--path", action="store_true", help="print the file path only")
    p = add("config", cmd_config, "Preview and enforce devkit-managed agent settings")
    p.add_argument("config_cmd", nargs="?", choices=["diff", "repair", "show", "edit", "validate", "lock", "unlock",
                                                     "status"])
    p.add_argument("--quiet", action="store_true")
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
    p = add("sync", cmd_sync, "Refresh map + notes, commit and push")
    p.add_argument("--session", help=argparse.SUPPRESS)
    p.add_argument("--finish", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--offline", action="store_true", help="no network (cached PR data)")
    p.add_argument("--scheduled", action="store_true", help="background mode: refresh PRs and sizes, safe gc")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--renew", metavar="AGENT", help=argparse.SUPPRESS)
    p.add_argument("--delay", type=int, default=0, help=argparse.SUPPRESS)
    p = add("gc", cmd_gc, "Reclaim disk safely")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--auto-only", action="store_true", help="only what is safe without a person (the gc job)")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    add("du", cmd_du, "Disk usage report").add_argument("--refresh", action="store_true")
    p = add("canary", cmd_canary, "Instruction-compliance canary report")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    p = add("ci", cmd_ci, "What CI runs (to run it locally first), `dev ci status`: live runs on any host, "
                          "`dev ci watch`: follow a run to the end and diagnose a failure, `dev ci audit`: diagnose "
                          "pipelines, `dev ci init`: set one up")
    p.add_argument("query", nargs="?", help="a project, or `status`, `audit`, `init` or `watch`")
    p.add_argument("-p", "--project", help="the project (id or a unique part of its name) for status, audit, init "
                                           "and watch")
    p.add_argument("--sha", help="watch: the commit whose runs to follow (default: HEAD)")
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
    from . import gitx
    gitx.add_ci_arguments(p)
    from . import pipelines
    pipelines.add_ci_arguments(p)
    gitx.register(add)
    from . import testx
    testx.register(add)
    add("mcp", cmd_mcp, "Serve devkit's tools over MCP on stdio (the Claude Desktop extension runs this)")
    from . import desktop
    desktop.register(add)
    from . import crew
    crew.register(add)
    add("doctor", cmd_doctor, "Verify the setup")
    add("install", cmd_install, "Apply configuration").add_argument(
        "--only", help="comma list: config,tools,cli,git,handoff,instructions,skills,claude,codex,desktop,docker,docs,launchd")
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
    p = add("setup", cmd_setup, "Installation and configuration wizard")
    p.add_argument("-y", "--yes", action="store_true", help="accept every default (non-interactive)")
    p.add_argument("--skip-packages", action="store_true", help="do not install Homebrew/npm packages")
    add("update", cmd_update, "Update devkit")
    add("docker", cmd_docker, "Docker on Colima").add_argument(
        "action", choices=["up", "down", "status", "prune", "autostart-on", "autostart-off"])
    add("version", cmd_version, "Show version")
    p = add("hook", cmd_hook, argparse.SUPPRESS)
    p.add_argument("event")
    p.add_argument("--agent", default="claude", choices=["claude", "codex"])
    p = add("__complete", cmd_complete, argparse.SUPPRESS)
    p.add_argument("kind")
    return ap


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.help and not a.cmd:
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
    finally:
        if track:
            try:
                here = core.tilde(os.getcwd())
            except OSError:  # the folder was removed while the command ran (scratch folders)
                here = "(removed folder)"
            audit.record("dev." + argv[0], " ".join(argv[1:])[:300], "", "ok" if rc == 0 else f"exit {rc}",
                         duration=round(time.time() - start, 2), cwd=here)
            if rc == 0 and argv[0] in ("commit", "push", "pr", "release", "branch", "rebase", "wt", "adopt", "clone",
                                       "new", "delivery", "initiative", "note"):
                from . import work as _w
                _w.request_sync(f"dev {' '.join(argv[:2])}")
