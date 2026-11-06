# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Project discovery and inspection, the projects map and the per-project handoff notes."""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

from . import core, notes as notesmod
from .core import (DEV, HANDOFF, LOCAL, NOTES, RESERVED_TOP, STATE, WT_ROOT, ago, canonical_path, fmt_ts, git,
                   gitout, human_kb, is_active, is_under, note_path, project_identity, read_json, sh, tilde,
                   write_if_changed)

AUTO_START = "<!-- dev:auto:start -->"
AUTO_END = "<!-- dev:auto:end -->"
LOG_HEADER = "## Session log"
SKIP_DIRS = {"node_modules", ".git", "vendor", "dist", "build", "target", ".venv", "venv", ".next", "__pycache__"}

# --------------------------------------------------------------------------- discovery


def main_of(path: Path):
    """Main checkout of the repo that contains `path` (works from linked worktrees)."""
    common = gitout(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        return None
    c = Path(common)
    return c.parent if c.name == ".git" else None


def _walk_repos(root: Path, depth: int):
    """Repository roots below a host folder (any nesting depth up to `depth`), not descending into repos."""
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except OSError:
        return
    for e in entries:
        if not e.is_dir(follow_symlinks=False) or e.name.startswith(".") or e.name in SKIP_DIRS:
            continue
        p = Path(e.path)
        g = p / ".git"
        if g.is_dir():
            yield p
        elif g.is_file():
            m = main_of(p)
            if m:
                yield m
        elif depth > 1:
            yield from _walk_repos(p, depth - 1)


def discover():
    """-> (main checkouts, plain non-git projects in _local, layout issues)."""
    mains, plain, issues = [], [], []
    seen = set()

    def add_main(p: Path):
        rp = core.real(p)
        if rp not in seen:
            seen.add(rp)
            mains.append(Path(rp))

    if not DEV.exists():
        return mains, plain, issues
    for top in sorted(DEV.iterdir(), key=lambda p: p.name.lower()):
        if not top.is_dir() or top.name.startswith(".") or top.name in RESERVED_TOP:
            continue
        g = top / ".git"
        if g.exists():
            m = top if g.is_dir() else main_of(top)
            if m:
                add_main(m)
            if g.is_dir():
                issues.append(("layout", f"{tilde(top)} is a repo directly in {tilde(DEV)}"))
            continue
        if "." in top.name:  # host folder: gitlab.com/<group>/<subgroup>/<repo>, bitbucket.org/<ws>/<repo>, …
            for m in _walk_repos(top, 6):
                add_main(m)
            continue
        for sub in sorted(top.iterdir(), key=lambda p: p.name.lower()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            sg = sub / ".git"
            if sg.is_dir():
                add_main(sub)
            elif sg.is_file():
                m = main_of(sub)
                if m:
                    add_main(m)
            else:
                issues.append(("layout", f"{tilde(sub)} is not a git repo (non-repo projects go in {tilde(LOCAL)})"))
    if LOCAL.exists():
        for sub in sorted(LOCAL.iterdir(), key=lambda p: p.name.lower()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            if (sub / ".git").is_dir():
                add_main(sub)
            elif not (sub / ".git").exists():
                plain.append(sub)
    if WT_ROOT.exists():  # managed worktrees whose main checkout lives outside DEV
        for dirpath, dirnames, _ in os.walk(WT_ROOT):
            if os.path.isfile(os.path.join(dirpath, ".git")):
                m = main_of(Path(dirpath))
                if m:
                    add_main(m)
                dirnames[:] = []
            elif dirpath.count(os.sep) - str(WT_ROOT).count(os.sep) > 6:
                dirnames[:] = []
    return mains, plain, issues


# --------------------------------------------------------------------------- inspection


def parse_status(out: str) -> dict:
    lines = out.splitlines()
    info = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "upstream_gone": False,
            "modified": 0, "untracked": 0}
    if lines and lines[0].startswith("## "):
        h = lines[0][3:]
        for prefix in ("No commits yet on ", "Initial commit on "):
            if h.startswith(prefix):
                h = h[len(prefix):]
        bracket = ""
        if h.endswith("]") and " [" in h:
            h, bracket = h.rsplit(" [", 1)
            bracket = bracket[:-1]
        branch, upstream = (h.split("...", 1) + [None])[:2] if "..." in h else (h, None)
        if branch.startswith("HEAD (no branch)"):
            branch = None
        info.update(branch=branch, upstream=upstream)
        for part in bracket.split(", "):
            if part.startswith("ahead "):
                info["ahead"] = int(part[6:])
            elif part.startswith("behind "):
                info["behind"] = int(part[7:])
            elif part == "gone":
                info["upstream_gone"] = True
    for ln in lines[1:]:
        if ln.startswith("??"):
            info["untracked"] += 1
        elif ln.strip():
            info["modified"] += 1
    return info


def default_branch(main: Path) -> str:
    ref = gitout(main, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref.startswith("origin/"):
        return ref[len("origin/"):]
    for b in ("main", "master", "develop"):
        if git(main, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{b}")[0] == 0:
            return b
    return gitout(main, "branch", "--show-current") or "main"


def last_activity(path: Path, gitdir: str, commit_ts: int) -> int:
    ts = [commit_ts or 0]
    for f in ("index", "HEAD", "logs/HEAD", "FETCH_HEAD"):
        try:
            ts.append(int(os.stat(os.path.join(gitdir, f)).st_mtime))
        except OSError:
            pass
    tdir = core.CLAUDE_HOME / "projects" / core.claude_slug(path)
    try:
        for e in os.scandir(tdir):
            if e.name.endswith(".jsonl"):
                ts.append(int(e.stat().st_mtime))
    except OSError:
        pass
    return max(ts)


def inspect_checkout(path: Path, default: str, has_remotes: bool) -> dict:
    rc, out, _ = git(path, "status", "--porcelain=v1", "-b")
    if rc != 0:
        return {"path": str(path), "error": "git status failed"}
    st = parse_status(out)
    st["path"] = str(path)
    gitdir = gitout(path, "rev-parse", "--absolute-git-dir")
    st["head"] = gitout(path, "rev-parse", "--short", "HEAD")
    st["unpushed"] = int(gitout(path, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0) if has_remotes else 0
    st["ahead_default"] = int(gitout(path, "rev-list", "--count", f"origin/{default}..HEAD") or 0) if has_remotes else 0
    lc = gitout(path, "log", "-1", "--format=%ct%x1f%s")
    ts, _, subject = lc.partition("\x1f")
    st["last_commit_ts"] = int(ts) if ts.isdigit() else 0
    st["last_commit"] = subject
    st["last_activity"] = last_activity(path, gitdir, st["last_commit_ts"]) if gitdir else st["last_commit_ts"]
    return st


def list_worktrees(main: Path) -> list:
    out = gitout(main, "worktree", "list", "--porcelain")
    wts, cur = [], {}
    for ln in out.splitlines() + [""]:
        if not ln.strip():
            if cur:
                wts.append(cur)
            cur = {}
            continue
        key, _, val = ln.partition(" ")
        if key == "worktree":
            cur["path"] = val
        elif key == "branch":
            cur["branch"] = val.replace("refs/heads/", "", 1)
        elif key in ("locked", "prunable", "bare", "detached"):
            cur[key] = val or True
        elif key == "HEAD":
            cur["head"] = val[:8]
    return wts


def classify_location(p: Path, main: Path) -> str:
    if is_under(p, main / ".claude" / "worktrees"):
        return "claude"
    if is_under(p, WT_ROOT):
        return "canonical"
    if is_under(p, core.CODEX_HOME / "worktrees"):
        return "codex"
    if core.is_temp(p):
        return "temp"
    return "stray"


def _pkg_deps(pkg: Path) -> dict:
    d = read_json(pkg, {}) or {}
    deps = {}
    for k in ("dependencies", "devDependencies", "peerDependencies"):
        deps.update(d.get(k) or {})
    return deps


def detect_stack(root: Path) -> list:
    found = []

    def scan(d: Path):
        f = lambda n: (d / n).exists()  # noqa: E731
        if f("pom.xml"):
            found.append("java/maven")
        if f("build.gradle") or f("build.gradle.kts"):
            found.append("java/gradle")
        if f("pyproject.toml") or f("requirements.txt") or f("setup.py"):
            found.append("python")
        if f("composer.json"):
            found.append("laravel" if f("artisan") else "symfony" if f("symfony.lock") else "php")
        if f("package.json"):
            deps = _pkg_deps(d / "package.json")
            if f("angular.json") or "@angular/core" in deps:
                found.append("angular")
            if "next" in deps:
                found.append("nextjs")
            elif "react" in deps:
                found.append("react")
            if "@nestjs/core" in deps:
                found.append("nestjs")
            found.append("typescript" if (f("tsconfig.json") or "typescript" in deps) else "node")
        if f("Cargo.toml"):
            found.append("rust")
        if f("go.mod"):
            found.append("go")
        if f("Dockerfile") or f("docker-compose.yml") or f("compose.yaml") or f("compose.yml"):
            found.append("docker")

    scan(root)
    try:
        for sub in sorted(os.scandir(root), key=lambda e: e.name):
            if sub.is_dir() and not sub.name.startswith(".") and sub.name not in SKIP_DIRS:
                scan(Path(sub.path))
    except OSError:
        pass
    return sorted(set(found), key=found.index)


def detect_commands(root: Path) -> list:
    """Short hints the agent would otherwise spend turns discovering."""
    hints = []
    pkg = root / "package.json"
    if pkg.exists():
        pm = ("pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists()
              else "bun" if (root / "bun.lockb").exists() or (root / "bun.lock").exists() else "npm")
        scripts = list(((read_json(pkg, {}) or {}).get("scripts") or {}).keys())
        keep = [s for s in scripts if s in ("dev", "start", "build", "test", "lint", "typecheck", "check",
                                             "format", "e2e", "test:unit", "test:e2e", "verify")]
        hints.append(f"{pm} run <{'|'.join(keep[:8])}>" if keep else f"{pm} (no standard scripts)")
    if (root / "mvnw").exists():
        hints.append("./mvnw verify")
    elif (root / "pom.xml").exists():
        hints.append("mvn verify")
    if (root / "gradlew").exists():
        hints.append("./gradlew build")
    elif (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        hints.append("gradle build")
    if (root / "uv.lock").exists():
        hints.append("uv sync · uv run pytest")
    elif (root / "poetry.lock").exists():
        hints.append("poetry install · poetry run pytest")
    elif (root / "pyproject.toml").exists():
        hints.append("uv venv && uv pip install -e . · pytest")
    if (root / "composer.json").exists():
        scripts = list(((read_json(root / "composer.json", {}) or {}).get("scripts") or {}).keys())
        hints.append("composer install" + (f" · composer <{'|'.join(scripts[:6])}>" if scripts else ""))
        if (root / "artisan").exists():
            hints.append("php artisan test")
    if (root / "Makefile").exists():
        try:
            targets = re.findall(r"^([A-Za-z][\w.-]*):(?!=)", (root / "Makefile").read_text(errors="ignore"), re.M)
            if targets:
                hints.append(f"make <{'|'.join(dict.fromkeys(targets[:8]))}>")
        except OSError:
            pass
    return hints


# --------------------------------------------------------------------------- GitHub PRs


def prs(slug: str, refresh=False, max_age=900):
    """Recent PRs of `owner/repo` (cached). None when gh is unavailable."""
    f = STATE / "prs" / (slug.replace("/", "__") + ".json")
    if not refresh and f.exists() and time.time() - f.stat().st_mtime < max_age:
        return read_json(f)
    rc, out, _ = sh(["gh", "pr", "list", "-R", slug, "--state", "all", "--limit", "200", "--json",
                     "number,state,headRefName,title,url,updatedAt,isDraft"], timeout=30)
    if rc != 0:
        return read_json(f) if f.exists() else None
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(out)
    return json.loads(out or "[]")


def pr_for_branch(pr_list, branch: str):
    if not pr_list or not branch:
        return None
    mine = [p for p in pr_list if p.get("headRefName") == branch]
    if not mine:
        return None
    mine.sort(key=lambda p: (p.get("state") != "OPEN", p.get("updatedAt", "")), reverse=False)
    open_ = [p for p in mine if p.get("state") == "OPEN"]
    return open_[0] if open_ else sorted(mine, key=lambda p: p.get("updatedAt", ""), reverse=True)[0]


def pr_fresh(slug: str, branch: str):
    """Authoritative PR state for one branch (used right before destructive actions)."""
    rc, out, _ = sh(["gh", "pr", "list", "-R", slug, "--head", branch, "--state", "all", "--json",
                     "number,state,updatedAt,headRefName"], timeout=30)
    if rc != 0:
        return "unknown"
    return pr_for_branch(json.loads(out or "[]"), branch)


# --------------------------------------------------------------------------- sizes


def sizes(paths, refresh=False, max_age=3600) -> dict:
    cache_f = STATE / "cache" / "sizes.json"
    cache = read_json(cache_f, {}) or {}
    now = time.time()
    for p in paths:
        hit = cache.get(p)
        if refresh or not hit or now - hit[1] > max_age:
            kb = core.du_kb(p, exclude="worktrees")
            if kb is not None:
                cache[p] = [kb, now]
    cache = {k: v for k, v in cache.items() if os.path.exists(k)}
    cache_f.parent.mkdir(parents=True, exist_ok=True)
    cache_f.write_text(json.dumps(cache))
    return {p: cache.get(p, [None, None])[0] for p in paths}


# --------------------------------------------------------------------------- registry


def inspect_repo(main: Path, with_prs=True, refresh_prs=False, pr_max_age=900) -> dict:
    remote = gitout(main, "config", "--get", "remote.origin.url")
    pid, owner, name = project_identity(main, remote)
    has_remotes = bool(gitout(main, "remote"))
    default = default_branch(main) if has_remotes else (gitout(main, "branch", "--show-current") or "main")
    gh_slug = f"{owner}/{name}" if remote and "github.com" in remote and owner else None
    pr_list = prs(gh_slug, refresh=refresh_prs, max_age=pr_max_age) if (with_prs and gh_slug and core.have("gh")) else None
    co = inspect_checkout(main, default, has_remotes)
    wts = []
    for w in list_worktrees(main)[1:]:
        if w.get("bare"):
            continue
        p = Path(w["path"])
        if not p.exists():
            wts.append({"path": str(p), "branch": w.get("branch"), "missing": True, "location": "missing"})
            continue
        wi = inspect_checkout(p, default, has_remotes)
        wi.update(location=classify_location(p, main), locked=bool(w.get("locked")), active=is_active(p))
        pr = pr_for_branch(pr_list, wi.get("branch"))
        wi["pr"] = {"number": pr["number"], "state": pr["state"], "updatedAt": pr.get("updatedAt"),
                    "title": pr.get("title")} if pr else None
        wts.append(wi)
    main_pr = pr_for_branch(pr_list, co.get("branch")) if co.get("branch") != default else None
    co["pr"] = {"number": main_pr["number"], "state": main_pr["state"], "title": main_pr.get("title")} if main_pr else None
    canon = canonical_path(pid)
    activity = max([co.get("last_activity", 0)] + [w.get("last_activity", 0) for w in wts])
    return {
        "id": pid, "owner": owner, "name": name or main.name, "path": str(main), "remote": remote,
        "github": gh_slug, "default_branch": default, "canonical": str(canon),
        "canonical_ok": core.real(canon) == core.real(main), "stack": detect_stack(main),
        "commands": detect_commands(main), "main": co, "worktrees": wts,
        "prs_open": [{"number": p["number"], "title": p["title"], "branch": p["headRefName"], "draft": p.get("isDraft")}
                     for p in (pr_list or []) if p.get("state") == "OPEN"],
        "prs_known": pr_list is not None, "note": str(note_path(pid).relative_to(HANDOFF)),
        "last_activity": activity,
    }


def _main_active(main: Path, wts) -> bool:
    """Main checkout is active when a process works in it but outside its nested worktrees."""
    p = core.real(main)
    nested = [core.real(w["path"]) for w in wts]
    for c in core.active_cwds():
        if c == p or c.startswith(p + "/"):
            if not any(c == n or c.startswith(n + "/") for n in nested):
                return True
    return False


def build(with_prs=True, refresh_prs=False, with_sizes=False, refresh_sizes=False, pr_max_age=900) -> dict:
    mains, plain, issues = discover()
    projects = []
    for m in mains:
        try:
            pr = inspect_repo(m, with_prs=with_prs, refresh_prs=refresh_prs, pr_max_age=pr_max_age)
        except Exception as e:  # one broken repo must not break the map
            issues.append(("error", f"{tilde(m)}: {e}"))
            continue
        pr["main"]["active"] = _main_active(m, pr["worktrees"])
        projects.append(pr)
    for p in plain:
        projects.append({"id": f"_local/{p.name}", "owner": None, "name": p.name, "path": str(p), "remote": "",
                         "github": None, "git": False, "canonical": str(p), "canonical_ok": True,
                         "stack": detect_stack(p), "commands": detect_commands(p), "main": {}, "worktrees": [],
                         "prs_open": [], "note": str(note_path(f"_local/{p.name}").relative_to(HANDOFF)),
                         "last_activity": int(os.stat(p).st_mtime)})
    if with_sizes:
        paths = [p["path"] for p in projects] + [w["path"] for p in projects for w in p["worktrees"]
                                                 if not w.get("missing")]
        sz = sizes(paths, refresh=refresh_sizes)
    else:
        sz = {k: v[0] for k, v in (read_json(STATE / "cache" / "sizes.json", {}) or {}).items()}
    for p in projects:
        p["size_kb"] = sz.get(p["path"])
        for w in p["worktrees"]:
            w["size_kb"] = sz.get(w["path"])
    for p in projects:
        if not p["canonical_ok"]:
            issues.append(("layout", f"{p['id']} lives at {tilde(p['path'])}; canonical is {tilde(p['canonical'])} "
                                     f"→ `dev adopt {tilde(p['path'])}`"))
        for w in p["worktrees"]:
            if w.get("location") == "stray":
                issues.append(("worktree", f"{p['id']}: worktree outside the managed locations: {tilde(w['path'])}"))
            if w.get("missing"):
                issues.append(("worktree", f"{p['id']}: worktree registered but missing on disk ({tilde(w['path'])}) "
                                           f"→ `dev gc --apply` prunes it"))
            idle_d = (time.time() - w.get("last_activity", 0)) / 86400
            if (w.get("modified") or w.get("untracked")) and idle_d > 7 and not w.get("active"):
                issues.append(("work", f"{p['id']}: uncommitted work idle {int(idle_d)}d in {tilde(w['path'])}"))
            if w.get("unpushed") and not w.get("active") and idle_d > 2:
                issues.append(("work", f"{p['id']}: {w['unpushed']} unpushed commit(s) on {w.get('branch')}"))
    for p in projects:
        enrich(p)
    projects.sort(key=lambda p: (p["id"].startswith("_local/"), p["id"]))
    return {"version": 1, "host": socket.gethostname().split(".")[0], "dev_root": str(DEV),
            "generated": int(time.time()), "projects": projects, "issues": issues}


def commit_activity(path: str, days: int = 30) -> dict:
    """{YYYY-MM-DD: commits} over all branches for the last `days` days."""
    import datetime as dt
    out = gitout(path, "log", "--all", f"--since={days}.days", "--format=%ct", timeout=30)
    counts = {}
    for ln in out.splitlines():
        if ln.isdigit():
            d = dt.date.fromtimestamp(int(ln)).isoformat()
            counts[d] = counts.get(d, 0) + 1
    return counts


def status_line(pid: str):
    return read_json(STATE / "status" / (pid.replace("/", "__") + ".json"), None)


def plans_for(pid: str) -> list:
    d = HANDOFF / "plans" / pid
    out = []
    if d.is_dir():
        for f in sorted(d.rglob("*.md"), key=lambda x: -x.stat().st_mtime):
            title = next((ln[2:].strip() for ln in f.read_text(errors="ignore").splitlines() if ln.startswith("# ")),
                         f.stem)
            out.append({"title": title, "path": str(f.relative_to(HANDOFF)), "mtime": int(f.stat().st_mtime)})
    return out[:20]


def enrich(p: dict):
    """Add handoff-note summary, activity, latest agent status line and plans to a registry entry."""
    f = note_path(p["id"])
    p["note_summary"] = notesmod.summarize(f.read_text()) if f.exists() else None
    if p.get("main") and p.get("git", True) is not False:
        p["activity"] = commit_activity(p["path"])
    p["status_line"] = status_line(p["id"])
    p["plans"] = plans_for(p["id"])


def ops_log(limit: int = 60) -> list:
    """Recent entries of log/YYYY-MM.md (newest first)."""
    out = []
    for f in sorted((HANDOFF / "log").glob("*.md"), reverse=True):
        for ln in reversed(f.read_text(errors="ignore").splitlines()):
            if ln.startswith("- "):
                out.append(ln[2:])
            if len(out) >= limit:
                return out
    return out


def find(reg: dict, query: str):
    """Resolve a project by id, name, path or substring. -> list of matches."""
    q = (query or "").lower().strip().rstrip("/")
    ps = reg["projects"]
    if not q:
        return []
    exact = [p for p in ps if q in (p["id"], p["name"].lower(), core.real(q) if q.startswith(("/", "~")) else "")
             or core.real(p["path"]) == core.real(os.path.expanduser(q))]
    if exact:
        return exact
    return [p for p in ps if q in p["id"]]


def project_for_cwd(reg: dict, cwd: str):
    """-> (project, worktree-or-None) containing cwd."""
    rc = core.real(cwd)
    best = None
    for p in reg["projects"]:
        for w in p["worktrees"]:
            if is_under(rc, w["path"]):
                return p, w
        if is_under(rc, p["path"]) and (best is None or len(p["path"]) > len(best["path"])):
            best = p
    return (best, None) if best else (None, None)


# --------------------------------------------------------------------------- rendering


def _state(co: dict) -> str:
    if not co:
        return "no git"
    bits = []
    if co.get("modified"):
        bits.append(f"{co['modified']} modified")
    if co.get("untracked"):
        bits.append(f"{co['untracked']} untracked")
    if co.get("ahead"):
        bits.append(f"↑{co['ahead']}")
    if co.get("behind"):
        bits.append(f"↓{co['behind']}")
    if co.get("unpushed") and not co.get("ahead"):
        bits.append(f"{co['unpushed']} unpushed")
    return ", ".join(bits) or "clean"


def _pr(w) -> str:
    pr = w.get("pr")
    return f"#{pr['number']} {pr['state'].lower()}" if pr else "-"


def render_map(reg: dict) -> str:
    ps = reg["projects"]
    n_wt = sum(len(p["worktrees"]) for p in ps)
    active = sum(1 for p in ps for w in p["worktrees"] if w.get("active")) + sum(1 for p in ps if p["main"].get("active"))
    total_kb = sum((p.get("size_kb") or 0) + sum(w.get("size_kb") or 0 for w in p["worktrees"]) for p in ps)
    out = ["# Projects map", "",
           f"_Generated by `dev sync` on {reg['host']} · {fmt_ts(reg['generated'])} · do not edit by hand; "
           f"handoff notes live in [`projects/`](projects/)._", "",
           f"**{len(ps)} projects** · **{n_wt} worktrees** · **{active} active sessions** · "
           f"**{human_kb(total_kb) if total_kb else '-'}** on disk", ""]
    try:
        from . import canary
        out += [f"**Instruction canary (7 days):** {canary.summary_line(7)} — replies starting with `DevKit ·`", ""]
    except Exception:
        pass
    if reg["issues"]:
        out += ["## Needs attention", ""] + [f"- **{k}** — {m}" for k, m in reg["issues"]] + [""]
    groups = {}
    for p in ps:
        groups.setdefault(p["id"].rsplit("/", 1)[0], []).append(p)
    for owner in sorted(groups, key=lambda o: (o == "_local", o)):
        out += [f"## {owner}", "", "| Project | Status | Tasks | Stack | Branch | State | Worktrees | Open PRs | "
                "Last activity | Size | Note |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for p in groups[owner]:
            co = p["main"]
            act = sum(1 for w in p["worktrees"] if w.get("active"))
            wt = f"{len(p['worktrees'])}" + (f" ({act} active)" if act else "") if p["worktrees"] else "-"
            branch = co.get("branch") or "-"
            ns = p.get("note_summary") or {}
            st = ns.get("status", "-") + (" ·" + ns["priority"] if ns.get("priority") not in (None, "normal") else "")
            tk = f"{len(ns['tasks']['now'])} now / {ns['tasks']['open']} open" if ns else "-"
            out.append(f"| [{p['name']}]({p['remote'] or tilde(p['path'])}) | {st} | {tk} | "
                       f"{', '.join(p['stack']) or '-'} | `{branch}` | {_state(co)} | {wt} | "
                       f"{len(p['prs_open']) if p.get('prs_known') else '-'} | {fmt_ts(p['last_activity'])} | "
                       f"{human_kb(p.get('size_kb'))} | [note]({p['note']}) |")
        out.append("")
    wts = [(p, w) for p in ps for w in p["worktrees"]]
    if wts:
        out += ["## Worktrees", "", "| Project | Branch | PR | State | Where | Session | Last activity | Size |",
                "|---|---|---|---|---|---|---|---|"]
        for p, w in sorted(wts, key=lambda t: -(t[1].get("last_activity") or 0)):
            out.append(f"| {p['id']} | `{w.get('branch') or 'detached'}` | {_pr(w)} | "
                       f"{'missing' if w.get('missing') else _state(w)} | {w.get('location')} · `{tilde(w['path'])}` | "
                       f"{'active' if w.get('active') else '-'} | {fmt_ts(w.get('last_activity'))} | "
                       f"{human_kb(w.get('size_kb'))} |")
        out.append("")
    return "\n".join(out)


NOTE_TEMPLATE = notesmod.TEMPLATE  # kept for backward compatibility


def render_auto(p: dict) -> str:
    co = p["main"]
    lines = [AUTO_START, f"_Auto-refreshed by `dev sync` · {fmt_ts(time.time())}. Edit outside this block._", ""]
    lines.append(f"- **Path:** `{tilde(p['path'])}`" + ("" if p["canonical_ok"] else f" (canonical: `{tilde(p['canonical'])}`)"))
    if p.get("remote"):
        lines.append(f"- **Remote:** {p['remote']} · default branch `{p.get('default_branch')}`")
    if p["stack"]:
        lines.append(f"- **Stack:** {', '.join(p['stack'])}")
    if p.get("commands"):
        lines.append(f"- **Commands:** {' · '.join('`' + c + '`' for c in p['commands'])}")
    if co:
        lines.append(f"- **Main checkout:** `{co.get('branch')}` · {_state(co)} · last commit {fmt_ts(co.get('last_commit_ts'))}"
                     f" “{(co.get('last_commit') or '')[:80]}”")
    for w in p["worktrees"]:
        lines.append(f"- **Worktree** `{w.get('branch')}` · {_pr(w)} · {'missing' if w.get('missing') else _state(w)} · "
                     f"{w.get('location')} · {'active session' if w.get('active') else 'idle ' + ago(w.get('last_activity'))}")
    if p.get("prs_open"):
        lines.append("- **Open PRs:** " + ", ".join(f"#{x['number']} {x['title'][:60]}" for x in p["prs_open"][:8]))
    lines.append(AUTO_END)
    return "\n".join(lines)


def update_note(p: dict, create=True) -> bool:
    f = note_path(p["id"])
    if not f.exists():
        if not create:
            return False
        text = notesmod.new_note(p["id"])
    else:
        text = notesmod.ensure_structure(f.read_text(), p["id"])
    auto = render_auto(p)
    if AUTO_START in text and AUTO_END in text:
        pre, rest = text.split(AUTO_START, 1)
        _, post = rest.split(AUTO_END, 1)
        new = pre + auto + post
    else:
        head, _, body = text.partition("\n")
        new = head + "\n\n" + auto + "\n" + body
    if f.exists():
        text = f.read_text()
        old_wo = re.sub(r"_Auto-refreshed by `dev sync` · [^.]*\.", "", text)
        new_wo = re.sub(r"_Auto-refreshed by `dev sync` · [^.]*\.", "", new)
        if old_wo == new_wo:
            return False
    return write_if_changed(f, new)


def add_session_line(pid: str, line: str, key: str) -> bool:
    """Insert (or replace, by session key) a line at the top of the note's session log."""
    f = note_path(pid)
    if not f.exists():
        return False
    text = f.read_text()
    marker = f"<!-- s:{key} -->"
    entry = f"- {line} {marker}"
    lines = text.splitlines()
    lines = [ln for ln in lines if marker not in ln]
    try:
        i = next(i for i, ln in enumerate(lines) if ln.strip() == LOG_HEADER)
    except StopIteration:
        lines += ["", LOG_HEADER]
        i = len(lines) - 1
    j = i + 1
    while j < len(lines) and lines[j].lstrip().startswith("<!--") and "s:" not in lines[j]:
        j += 1
    lines.insert(j, entry)
    return write_if_changed(f, "\n".join(lines) + "\n")


def human_sections(pid: str, max_chars=5000) -> str:
    """The agent-maintained part of a note (auto block removed), for SessionStart context."""
    f = note_path(pid)
    if not f.exists():
        return ""
    text = f.read_text()
    if AUTO_START in text and AUTO_END in text:
        pre, rest = text.split(AUTO_START, 1)
        text = pre + rest.split(AUTO_END, 1)[1]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n… (truncated; read the full note)"
    return text


def render_tasks(reg: dict) -> str:
    """TASKS.md: every open task across projects, grouped by project, Now first."""
    out = ["# Tasks", "", "_Generated by `dev sync` from the Tasks section of each handoff note. Edit the notes "
           "(or use `dev task`), not this file._", ""]
    rows = []
    for p in reg["projects"]:
        ns = p.get("note_summary")
        if not ns or not ns["tasks"]["open"]:
            continue
        rows.append((ns["priority"] != "high", ns["status"] != "active", p["id"], p, ns))
    if not rows:
        return "\n".join(out + ["No open tasks. Add one with `dev task add \"…\" --now`."])
    total_now = sum(len(r[4]["tasks"]["now"]) for r in rows)
    total_open = sum(r[4]["tasks"]["open"] for r in rows)
    out += [f"**{total_open} open tasks** · **{total_now} in progress (Now)** across {len(rows)} projects", ""]
    for _, _, pid, p, ns in sorted(rows):
        badge = f"`{ns['status']}`" + (f" · `{ns['priority']}`" if ns["priority"] != "normal" else "") + \
            (f" · milestone {ns['milestone']}" if ns["milestone"] else "") + (f" · due {ns['due']}" if ns["due"] else "")
        out += [f"## {pid}", "", badge, ""]
        for label, key in (("Now", "now"), ("Next", "next"), ("Later", "later")):
            items = ns["tasks"][key]
            if items:
                out.append(f"**{label}**")
                out += [f"- [ ] {t}" for t in items]
                out.append("")
        if ns["tasks"]["overdue"]:
            out += ["**Overdue:** " + "; ".join(ns["tasks"]["overdue"]), ""]
    return "\n".join(out).rstrip()


def render_activity(reg: dict, days: int = 30) -> str:
    """ACTIVITY.md: recent sessions and operations, newest first."""
    out = ["# Activity", "", f"_Generated by `dev sync`: session lines from the handoff notes and operations from "
           f"`log/` over the last {days} days._", ""]
    import datetime as dt
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    items = []
    for p in reg["projects"]:
        ns = p.get("note_summary") or {}
        for ln in ns.get("sessions", []):
            if ln[:10] >= cutoff:
                items.append((ln[:16], f"**{p['id']}** · {ln[17:] if len(ln) > 17 else ln}"))
    for ln in ops_log(200):
        if ln[:10] >= cutoff:
            items.append((ln[:16], ln[19:] if len(ln) > 19 else ln))
    if not items:
        return "\n".join(out + ["No activity recorded yet."])
    day = None
    for ts, text in sorted(items, reverse=True):
        if ts[:10] != day:
            day = ts[:10]
            out += ["", f"## {day}", ""]
        out.append(f"- {ts[11:16]} · {text}")
    return "\n".join(out).rstrip()


def write_outputs(reg: dict) -> list:
    changed = []
    HANDOFF.mkdir(parents=True, exist_ok=True)
    for p in reg["projects"]:
        if update_note(p):
            changed.append(p["note"])
    write_if_changed(HANDOFF / "TASKS.md", render_tasks(reg) + "\n") and changed.append("TASKS.md")
    write_if_changed(HANDOFF / "ACTIVITY.md", render_activity(reg) + "\n") and changed.append("ACTIVITY.md")
    old = read_json(HANDOFF / "registry.json", {}) or {}
    strip = lambda r: {k: v for k, v in r.items() if k != "generated"}  # noqa: E731
    if strip(old) != strip(json.loads(json.dumps(reg))):
        write_if_changed(HANDOFF / "registry.json", json.dumps(reg, indent=1, sort_keys=False) + "\n")
        write_if_changed(HANDOFF / "PROJECTS.md", render_map(reg) + "\n")
        changed += ["registry.json", "PROJECTS.md"]
    return changed
