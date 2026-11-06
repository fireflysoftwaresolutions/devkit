# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
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
        ("ci [--commands]", "Show what GitHub Actions runs, so you run it locally before the one push"),
        ("calver [--next]", "Current and next CalVer version YY.MM.PATCH from the repository's tags"),
        ("sec scan [--history]", "Security: secrets (gitleaks), dependencies (osv-scanner), SAST (Semgrep), Trivy"),
        ("docs check [--online]", "Check docs: Markdown structure, links and anchors, prose (Vale), diagrams"),
        ("diagram render|check|new", "Branded, validated SVG diagrams (flow, C4, state, sequence, ER, timeline)"),
    ]),
    ("Tracking and disk", [
        ("canary", "How often Claude/Codex replies carried the “DevKit ·” instruction marker"),
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
    core.git(HANDOFF, "fetch", "-q", "origin", timeout=60)
    if core.gitout(HANDOFF, "rev-list", "--count", "HEAD..@{u}") not in ("", "0"):
        core.git(HANDOFF, "pull", "-q", "--rebase", "--autostash", timeout=120)
    if core.gitout(HANDOFF, "rev-list", "--count", "@{u}..HEAD") != "0":
        rc, _, err = core.git(HANDOFF, "push", "-q", timeout=120)
        if rc != 0 and not quiet:
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


def cmd_home(a):
    print(ui.banner(tilde(DEV)))
    print(f"  {ui.bold(ui.greeting(core.gh_user().get('name', '')))} {ui.dim('Here is your workspace.')}")
    reg = _reg(prs=True)
    ps = reg["projects"]
    p, w = registry.project_for_cwd(reg, os.getcwd())
    if p:
        _print_status(p, w)
    n_wt = sum(len(x["worktrees"]) for x in ps)
    live = sum(1 for x in ps for y in x["worktrees"] if y.get("active")) + sum(1 for x in ps if x["main"].get("active"))
    last = core.read_json(STATE / "last-sync.json", {}) or {}
    print(ui.section("Workspace"))
    print(ui.kv("projects", f"{len(ps)} · worktrees {n_wt} · live sessions {live}"))
    print(ui.kv("handoff", f"{tilde(HANDOFF)} → {core.handoff_repo()} · last sync {ago(last.get('ts'))}"))
    from . import canary
    print(ui.kv("canary", canary.summary_line(7) + ui.dim("  (DevKit · marker, 7 days)")))
    recent = sorted(ps, key=lambda x: -(x.get("last_activity") or 0))[:5]
    if recent:
        print(ui.kv("recent", ", ".join(f"{x['id']} {ui.dim(ago(x.get('last_activity')))}" for x in recent)))
    if reg["issues"]:
        print(ui.section(f"Needs attention ({len(reg['issues'])})"))
        for kind, msg in reg["issues"][:6]:
            print(f"  {ui.WARN} {msg}")
        if len(reg["issues"]) > 6:
            print(f"  {ui.dim('… `dev status` / PROJECTS.md for the rest')}")
    print(f"\n  {ui.cyan('tip')} {ui.dim(ui.tip())}")
    print(f"  {ui.dim('dev help · dev ls · dev doctor')}\n")


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
        if a.scheduled and CFG["gc"].get("auto"):
            results = gc.apply(gc.plan(reg, with_sizes=True), only_auto=True)
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
    for ok, msg in gc.apply(pl, only_auto=False):
        print(f"  {ui.OK if ok else ui.WARN} {msg}")
    return cmd_sync(argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=False,
                                       quiet=False))


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


def cmd_ci(a):
    from . import ci
    top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if a.query:
        top = _resolve(_reg(prs=False), a.query)["path"]
    if not top:
        ui.err("not inside a repository")
        return 2
    if a.commands:
        print("\n\n".join(ci.local_commands(Path(top))))
        return 0
    print(ci.render(Path(top), ui))
    if a.runs:
        print(ui.section("Recent runs"))
        run(["gh", "run", "list", "--limit", "8"])
    return 0


def cmd_canary(a):
    from . import canary
    rs = canary.records(a.days)
    print(ui.section(f"Instruction canary “{canary.MARKER} ·” — last {a.days} days"))
    if not rs:
        print("  no tracked replies yet (replies in sessions under ~/Development are checked after every turn)")
        return 0
    for agent, (ok, tot) in sorted(canary.summary(a.days).items()):
        pct = 100 * ok // tot
        mark = ui.OK if pct >= 95 else ui.WARN if pct >= 80 else ui.FAIL
        print(f"  {mark} {agent.capitalize():<8} {ok}/{tot} replies carried the marker ({pct}%)")
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


def cmd_task(a):
    from . import work
    scope, label = _scope(a)
    act = a.task_cmd or "ls"
    try:
        if act == "add":
            bucket = "now" if a.now else "later" if a.later else "next"
            it = work.create(scope, " ".join(a.text), kind=a.type, bucket=bucket, priority=a.priority,
                             body=a.body or "", why=a.why or "", due=a.due or "")
            print(f"{ui.OK} {it['meta']['id']} added to {bucket} · {label}")
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
            path = plans.new(scope, " ".join(a.title))
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
        print("    " + ui.dim(" · ".join(m["id"] + ("" if m.get("local") else " (not cloned)") for m in i["members"])))
        for t in i["tasks"]["now"][:3]:
            print(f"    {ui.cyan('▸')} {t}")
    for x in data["projects"]:
        if x["status"] in ("archived",) and not a.all:
            continue
        badge = {"active": ui.green, "blocked": ui.red, "paused": ui.yellow}.get(x["status"], ui.dim)(x["status"])
        pr = ui.yellow(" !high") if x["priority"] == "high" else ""
        live = f" {ui.ACTIVE} {x['live_sessions']} live" if x["live_sessions"] else ""
        print(f"\n  {ui.bold(x['id'])}  {badge}{pr}{live}  {ui.dim(ago(x['last_activity']))}")
        if x["purpose"]:
            print(f"    {ui.dim(x['purpose'])}")
        if x["status_line"]:
            print(f"    {ui.cyan('◆')} {x['status_line']['line']}  {ui.dim(x['status_line']['agent'] + ' · ' + ago(x['status_line']['ts']))}")
        for t in x["now"][:3]:
            print(f"    {ui.cyan('▸')} {t}")
        if x["open_tasks"] > len(x["now"][:3]):
            print(f"    {ui.dim(str(x['open_tasks']) + ' open task(s) — dev task -p ' + x['id'])}")
        if x["last_session"]:
            print(f"    {ui.dim('last: ' + x['last_session'][:140])}")
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


def cmd_agents(a):
    from . import providers
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
    for verb, h in (("start", "claim the lock and move to Now (doing)"), ("claim", "same as start"),
                    ("release", "give the lock back (back to todo)"), ("done", "mark done and release"),
                    ("block", "mark blocked (needs --why)"), ("cancel", "cancel (needs --why)"),
                    ("show", "an item with its history"), ("history", "an item's changelog")):
        t = tsp.add_parser(verb, help=h)
        t.add_argument("selector", nargs="+", help="id (T-…), number from `dev task`, or a unique part of the title")
        t.add_argument("--why", help="the reason (recorded in the changelog and the commit)")
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
    p = add("note", cmd_note, "Project metadata in the handoff note")
    p.add_argument("set", choices=["set"])
    p.add_argument("pairs", nargs="+", help="status=… priority=… milestone=… due=YYYY-MM-DD tags=a,b progress=0-100")
    p.add_argument("-p", "--project")
    p.add_argument("-i", "--initiative")
    p = add("plan", cmd_plan, "Versioned plans and specs: new, revise --why, approve, ls")
    p.add_argument("-p", "--project")
    p.add_argument("-i", "--initiative")
    psp = p.add_subparsers(dest="plan_cmd")
    x = psp.add_parser("new", help="create a plan in plans/<scope>/")
    x.add_argument("title", nargs="+")
    for verb in ("revise", "approve", "supersede"):
        x = psp.add_parser(verb, help=f"{verb} a plan (records version, who and why)")
        x.add_argument("path")
        x.add_argument("--why", help="the reason for the change (required for revise)")
    psp.add_parser("ls", help="list plans")
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
    p.add_argument("action", nargs="?", choices=["status", "test"], default="status")
    p.add_argument("agent", nargs="?", choices=["claude", "codex", "all"])
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
    p = add("gc", cmd_gc, "Reclaim disk safely")
    p.add_argument("--apply", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    add("du", cmd_du, "Disk usage report").add_argument("--refresh", action="store_true")
    p = add("canary", cmd_canary, "Instruction-compliance canary report")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--limit", type=int, default=20)
    p = add("ci", cmd_ci, "Show what GitHub Actions runs, to run it locally first")
    p.add_argument("query", nargs="?")
    p.add_argument("--commands", action="store_true", help="print only the run commands")
    p.add_argument("--runs", action="store_true", help="also list recent CI runs")
    add("doctor", cmd_doctor, "Verify the setup")
    add("install", cmd_install, "Apply configuration").add_argument(
        "--only", help="comma list: config,cli,git,handoff,instructions,skills,claude,codex,docker,launchd")
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
    try:
        return a.fn(a) or 0
    except KeyboardInterrupt:
        return 130
