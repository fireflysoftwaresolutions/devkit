# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""`dev` command line: argument parsing and command implementations."""
from __future__ import annotations

import argparse
import json
import os
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
    ("Agents", [
        ("claude [query] [-w branch]", "Start Claude Code in a project (optionally in its own worktree)"),
        ("codex [query] [-w branch]", "Start Codex in a project (optionally in its own worktree)"),
        ("handoff [query] [--edit]", "Show or edit the project's handoff note"),
    ]),
    ("Worktrees", [
        ("wt new <branch>", "Create a managed worktree in ~/Development/.worktrees/<owner>/<repo>/<branch>"),
        ("wt ls [--all]", "List worktrees with PR state, live sessions and size"),
        ("wt rm <branch|path>", "Remove a worktree safely (refuses if work would be lost)"),
    ]),
    ("Delivery", [
        ("ci [--commands]", "Show what GitHub Actions runs, so you run it locally before the one push"),
    ]),
    ("Tracking and disk", [
        ("canary", "How often Claude/Codex replies carried the “DevKit ·” instruction marker"),
        ("sync", "Refresh the projects map and handoff notes, commit and push the handoff repo"),
        ("gc [--apply]", "Show (or reclaim) merged worktrees, stale build output and temp files"),
        ("du", "Disk usage of projects, worktrees, agent data, temp and Docker"),
    ]),
    ("Environment", [
        ("doctor", "Verify the whole setup (git, gh, Claude, Codex, skills, Docker, automation)"),
        ("install [--only step]", "Apply the devkit configuration (idempotent)"),
        ("update", "Pull the latest devkit and re-apply it"),
        ("docker up|down|status|prune", "Run Docker on a Colima VM"),
        ("version", "Show the devkit version"),
    ]),
]


# --------------------------------------------------------------------------- helpers

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


def cmd_dashboard(a):
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
        return cmd_dashboard(a)
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
    rc = os.spawnvp(os.P_WAIT, args[0], args)
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
        rc = os.spawnvp(os.P_WAIT, "gh", ["gh", "repo", "create", f"{owner}/{a.name}", vis, "--add-readme"])
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
    return cmd_sync(argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=False,
                                       quiet=True))


def cmd_open(a):
    p = _resolve(_reg(prs=False), a.query)
    editor = CFG.get("editor") or os.environ.get("EDITOR", "code")
    return os.spawnvp(os.P_WAIT, editor, [editor, p["path"]])


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
    if a.path:
        print(f)
    elif a.edit:
        editor = os.environ.get("EDITOR") or CFG.get("editor") or "vi"
        return os.spawnvp(os.P_WAIT, editor, [editor, str(f)])
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
        with core.Lock("notes", wait=60):
            changed = registry.write_outputs(reg)
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
    if not a.yes and sys.stdin.isatty():
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
            rc = os.spawnvp(os.P_WAIT, "colima", args)
            if rc != 0:
                return rc
        rc, out, _ = core.sh(["docker", "info", "--format", "{{.ServerVersion}} · {{.NCPU}} CPU · {{.MemTotal}} bytes · {{.OperatingSystem}}"], timeout=30)
        print(f"{ui.OK} docker {out}" if rc == 0 else f"{ui.FAIL} docker is not reachable")
        return 0 if rc == 0 else 1
    if a.action == "down":
        return os.spawnvp(os.P_WAIT, "colima", ["colima", "stop"])
    if a.action == "status":
        os.spawnvp(os.P_WAIT, "colima", ["colima", "status"])
        if core.sh(["docker", "info"], timeout=10)[0] == 0:
            os.spawnvp(os.P_WAIT, "docker", ["docker", "system", "df"])
        return 0
    if a.action == "prune":
        os.spawnvp(os.P_WAIT, "docker", ["docker", "system", "prune", "-f"])
        return os.spawnvp(os.P_WAIT, "docker", ["docker", "builder", "prune", "-f"])
    if a.action in ("autostart-on", "autostart-off"):
        verb = "start" if a.action.endswith("on") else "stop"
        return os.spawnvp(os.P_WAIT, "brew", ["brew", "services", verb, "colima"])
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
        os.spawnvp(os.P_WAIT, "gh", ["gh", "run", "list", "--limit", "8"])
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
        return cmd_dashboard(a) or 0
    if getattr(a, "args", None) and a.args[:1] == ["--"]:
        a.args = a.args[1:]
    try:
        return a.fn(a) or 0
    except KeyboardInterrupt:
        return 130
