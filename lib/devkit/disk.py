# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Disk use on this Mac and the plan to free it (spec P-fxs316lh3nw R4-R8, Sati's `dev du` design, Hamann's (b)).

`dev du` measures each project on this Mac (its main checkout, managed worktrees, Claude Code and Codex worktrees and
copies outside the layout, with their largest ignored folders found by measuring), Colima and Docker, and the agents'
data and caches. It saves the measurement with its time and ends with a plan ranked by the space each item frees.

`dev free <n>` deletes plan item n only when the plan the developer saw is still the current one; it lists what goes,
asks once, checks safety again at that moment (changes, commits not pushed, a program using the folder), never
touches Docker volumes, and records every deletion in the audit trail. `dev gc` is the same command under its old
name. Deletion reuses gc's engine: `git worktree remove` through gc.remove_worktree, and only folders git ignores.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from . import core, gc, registry, ui

MIN_FOLDER_KB = 50 * 1024      # folders under 50 MB are not listed
MIN_ITEM_KB = 100 * 1024       # plan items under 100 MB are left out
SMALL_PROJECT_KB = 100 * 1024  # projects under 100 MB get one line per kind of checkout
AUTO_REFRESH_S = 12 * 3600     # an older measurement is taken again before it is shown
SAME_SIZE = 0.10               # a plan item whose size moved less than this is the same item
WORKERS = 4

DEPS = {"node_modules", ".venv", "venv", ".pnpm-store", "bower_components", ".bundle", "Pods", "jspm_packages",
        ".yarn"}
BUILD = {"dist", "build", "target", "out", ".next", ".nuxt", ".output", ".svelte-kit", "DerivedData", ".build",
         "storybook-static", "_build", ".vercel", ".expo"}
CACHES = {".cache", ".turbo", ".parcel-cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
          ".gradle", ".angular", ".nx", "coverage", ".tox", ".nox", ".hypothesis", ".sass-cache", ".eslintcache"}
KIND = {"build": "build output", "deps": "dependencies", "cache": "caches", "other": "other ignored files"}
LOCATION = {"canonical": "worktree", "claude": "claude", "codex": "codex", "temp": "temp", "stray": "copy"}
GROUP = {"worktree": "Worktrees", "claude": "Claude Code worktrees", "codex": "Codex worktrees",
         "temp": "Temporary worktrees", "copy": "Copies outside the layout"}
IMAGES = {"semgrep": "security scanner", "osv-scanner": "security scanner", "trivy": "security scanner",
          "gitleaks": "secret scanner", "zizmor": "workflow scanner"}
CODEX_PARTS = {"plugins": "Codex's plugins", "vendor_imports": "Codex's imported tools", "cache": "Codex's cache",
               "log": "Codex's logs", "logs": "Codex's logs", "sqlite": "Codex's databases"}


def cache_file() -> Path:
    return core.STATE / "cache" / "disk.json"


def seen_file() -> Path:
    return core.STATE / "cache" / "disk-seen.json"


def _disk_usage():
    """(total KB, free KB) of the volume that holds the home folder."""
    u = shutil.disk_usage(str(core.HOME))
    return u.total // 1024, u.free // 1024


def _processes() -> list:
    """The command line of every process, to see whether a tool that owns a cache is running."""
    return core.sh(["ps", "-axo", "command="], timeout=10)[1].splitlines()


# --------------------------------------------------------------------------- measuring: folders and checkouts


def du_many(paths, timeout=900) -> dict:
    """{path: KB} for many folders, with one du process per hundred folders."""
    out, paths = {}, list(paths)
    for i in range(0, len(paths), 100):
        _, text, _ = core.sh(["du", "-sk", *paths[i:i + 100]], timeout=timeout)
        for ln in text.splitlines():
            kb, _, p = ln.partition("\t")
            if kb.strip().isdigit():
                out[p] = int(kb)
    return out


def classify(full: str) -> str:
    """What an ignored folder is: deps, build, cache or other. Names first, then contents: a virtual environment is
    known by its pyvenv.cfg and a cache by the CACHEDIR.TAG that cargo, pytest and others write."""
    name = os.path.basename(full)
    if name in DEPS or os.path.exists(os.path.join(full, "pyvenv.cfg")) or (
            name == "vendor" and os.path.exists(os.path.join(os.path.dirname(full), "composer.json"))):
        return "deps"
    if name in BUILD:
        return "build"
    if name in CACHES or os.path.exists(os.path.join(full, "CACHEDIR.TAG")):
        return "cache"
    return "other"


def ignored_folders(path: str, skip=()) -> list:
    """The folders git ignores in a checkout, at any depth, measured and classified, largest first: every build
    folder (dev free deletes them all) and the others from MIN_FOLDER_KB up. `skip`: nested checkouts."""
    rc, out, _ = core.git(path, "status", "--ignored", "--porcelain", "-z", "--untracked-files=normal", timeout=180)
    if rc != 0:
        return []
    skip = [core.real(s) for s in skip]
    rels = []
    for entry in out.split("\0"):
        if not (entry.startswith("!! ") and entry.endswith("/")):
            continue
        rel = entry[3:].rstrip("/")
        full = os.path.join(path, rel)
        rf = core.real(full)
        if os.path.islink(full) or not os.path.isdir(full) or any(
                rf == s or rf.startswith(s + "/") or s.startswith(rf + "/") for s in skip):
            continue
        rels.append(rel)
    sizes = du_many(os.path.join(path, r) for r in rels)
    found = []
    for rel in rels:
        full = os.path.join(path, rel)
        kb, kind = sizes.get(full), classify(full)
        if kb is None or (kind != "build" and kb < MIN_FOLDER_KB):
            continue
        f = {"rel": rel, "kb": kb, "kind": kind}
        if kind == "build" and os.path.basename(rel) == "target" and os.path.exists(
                os.path.join(os.path.dirname(full), "Cargo.toml")):
            f["rust"] = True
        found.append(f)
    return sorted(found, key=lambda f: -f["kb"])


def _merged(kind: str, info: dict, has_remote: bool) -> bool:
    # its pull request, or the registry's merge test (registry.merge_states: an ancestor of origin/<default>, its own
    # merged pull request, or merged by content)
    pr = info.get("pr") or None
    return kind != "main" and has_remote and (bool(pr and pr.get("state") in ("MERGED", "CLOSED"))
                                              or info.get("ahead_default") == 0
                                              or info.get("merged") in ("ancestor", "pr", "content"))


def _checkout(kind: str, info: dict, kb, folders: list, has_remote: bool, held: str = "") -> dict:
    # unpushed: only the commits with no equivalent in origin/<default> (registry.unpushed_work); held: why
    # gc.remove_worktree keeps a merged checkout for its ignored files (gc.ignored_hold), or ""
    pr = info.get("pr") or None
    how = info.get("merged")
    merged = _merged(kind, info, has_remote)
    return {"kind": kind, "path": info["path"], "name": os.path.basename(info["path"]), "kb": kb or 0,
            "branch": info.get("branch"), "pr": {"number": pr["number"], "state": pr["state"]} if pr else None,
            "merged": merged, "how": how if merged else None, "ahead_default": info.get("ahead_default") or 0,
            "merged_pr": info.get("merged_pr") if merged and how == "pr" else None,
            "unpushed": registry.unpushed_work(info),
            "modified": info.get("modified") or 0, "untracked": info.get("untracked") or 0,
            "active": bool(info.get("active")), "locked": bool(info.get("locked")),
            "last_activity": info.get("last_activity") or 0, "folders": folders, "held": held if merged else ""}


def measure_projects(reg: dict, workers: int = WORKERS):
    """-> (the projects on this Mac with their checkouts measured, largest first; how many are only in the
    portfolio). Only local projects are measured (R1)."""
    here, away = registry.split_local(reg.get("projects") or [])
    amap = registry.alias_map(reg.get("projects") or [])
    jobs = []
    for i, p in enumerate(here):
        nested = [w["path"] for w in p.get("worktrees") or [] if w.get("path")]
        if p.get("path") and os.path.isdir(p["path"]):
            jobs.append((i, "main", dict(p.get("main") or {}, path=p["path"]), nested))
        for w in p.get("worktrees") or []:
            if not w.get("missing") and w.get("path") and os.path.isdir(w["path"]):
                jobs.append((i, LOCATION.get(w.get("location"), "copy"), w, []))

    def one(job):
        i, kind, info, nested = job
        kb = core.du_kb(info["path"], timeout=900, exclude="worktrees" if kind == "main" else None)
        removable = kind != "codex" and _merged(kind, info, bool(here[i].get("remote")))
        held = gc.ignored_hold(here[i]["path"], info["path"])[1] if removable else ""  # remove_worktree's own check
        return kb, ignored_folders(info["path"], nested), held

    with concurrent.futures.ThreadPoolExecutor(max(1, workers)) as ex:
        results = list(ex.map(one, jobs))
    out = []
    for i, p in enumerate(here):
        has_remote = bool(p.get("remote"))
        cos = [_checkout(kind, info, kb, folders, has_remote, held)
               for (j, kind, info, _), (kb, folders, held) in zip(jobs, results) if j == i]
        order = ["main", "worktree", "claude", "codex", "temp", "copy"]
        cos.sort(key=lambda c: (order.index(c["kind"]), -c["kb"]) if c["kind"] != "main" else (0, 0))
        out.append({"id": p["id"], "alias": amap.get(p["id"], p["id"]), "name": p.get("name") or "",
                    "path": p.get("path"), "remote": has_remote, "github": p.get("github"),
                    "canonical_ok": p.get("canonical_ok", True), "default_branch": p.get("default_branch") or "main",
                    "kb": sum(c["kb"] for c in cos), "checkouts": cos})
    out.sort(key=lambda x: -x["kb"])
    registry.remember_sizes({c["path"]: c["kb"] for p in out for c in p["checkouts"]})
    return out, len(away)


# --------------------------------------------------------------------------- measuring: Colima and Docker


def docker_kb(s) -> int:
    """KB from Docker's decimal sizes: 2.472GB, 59.5MB, 22.6kB, 0B."""
    m = re.match(r"\s*([\d.]+)\s*([kKmMgGtT]?)[bB]", str(s or ""))
    if not m:
        return 0
    return int(float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}[m.group(2).lower()] / 1024)


def _int(v) -> int:
    v = str(v or "").strip()
    return int(v) if v.isdigit() else 0


def _json_lines(text: str) -> list:
    out = []
    for ln in (text or "").splitlines():
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return [x for x in out if isinstance(x, dict)]


def colima_home(home=None) -> Path:
    return Path(os.environ.get("COLIMA_HOME") or Path(home or core.HOME) / ".colima")


def colima_profiles() -> list:
    """[{"name", "status", "disk"}] from `colima list --json`, one per profile."""
    rc, out, _ = core.sh(["colima", "list", "--json"], timeout=20)
    return _json_lines(out) if rc == 0 else []


def colima_info(home=None):
    """Colima's disk on the Mac and inside its virtual machine, or None when Colima is not set up."""
    ch = colima_home(home)
    if not core.have("colima") or not ch.exists():
        return None
    profiles = colima_profiles()
    running = [str(p.get("name") or "default") for p in profiles if str(p.get("status", "")).lower() == "running"]
    used, seen = None, set()
    for name in running:
        rc, out, _ = core.sh(["colima", "ssh", "-p", name, "--", "df", "-k", "/var/lib/docker", "/"], timeout=30)
        for ln in out.splitlines()[1:] if rc == 0 else []:
            parts = ln.split()
            if len(parts) >= 3 and parts[2].isdigit() and (name, parts[0]) not in seen:
                seen.add((name, parts[0]))
                used = (used or 0) + int(parts[2])
    return {"home": str(ch), "on_mac_kb": core.du_kb(ch, timeout=120) or 0,
            "logical_kb": sum(int(p.get("disk") or 0) for p in profiles) // 1024,
            "profiles": [str(p.get("name") or "default") for p in profiles], "running": running, "vm_used_kb": used}


def docker_info():
    """What Docker holds (`docker system df`) and its images with whether a container uses them, or None when
    Docker is not installed; {"reachable": False} when it does not answer."""
    if not core.have("docker"):
        return None
    if core.sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)[0] != 0:
        return {"reachable": False}
    df = {}
    for row in _json_lines(core.sh(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)[1]):
        key = {"Images": "images", "Containers": "containers", "Local Volumes": "volumes",
               "Build Cache": "build_cache"}.get(row.get("Type"))
        if key:
            df[key] = {"count": _int(row.get("TotalCount")), "active": _int(row.get("Active")),
                       "kb": docker_kb(row.get("Size")), "reclaimable_kb": docker_kb(row.get("Reclaimable"))}
    used_by = set(core.sh(["docker", "ps", "-a", "--format", "{{.Image}}"], timeout=30)[1].split())
    images = []
    for im in _json_lines(core.sh(["docker", "image", "ls", "--format", "{{json .}}"], timeout=60)[1]):
        repo, tag, iid = im.get("Repository") or "<none>", im.get("Tag") or "<none>", im.get("ID") or ""
        names = {repo, f"{repo}:{tag}", iid} | ({f"{repo}:latest"} if tag == "latest" else set())
        in_use = bool(names & used_by) or any(u and iid.startswith(u) for u in used_by) or \
            str(im.get("Containers") or "0") not in ("0", "N/A", "")
        images.append({"repo": repo, "tag": tag, "id": iid, "kb": docker_kb(im.get("Size")), "in_use": in_use})
    images.sort(key=lambda x: -x["kb"])
    total = sum(v["kb"] for v in df.values())
    return {"reachable": True, "df": df, "images": images, "total_kb": total}


# --------------------------------------------------------------------------- measuring: agent data and caches


def _env_path(name, default) -> Path:
    return Path(os.environ[name]) if os.environ.get(name) else Path(default)


def agent_data(home=None, tmp=None, dev_tmp=None) -> list:
    """The agents' data and the package caches, each with what it is, whether it is safe to clear, and the command
    of its own tool that clears it (None: devkit lists it but does not free it)."""
    home = Path(home or core.HOME)
    tmp = Path(tmp or core.real(os.environ.get("CLAUDE_CODE_TMPDIR", "/tmp")))
    claude, codex = home / ".claude", (core.CODEX_HOME if home == core.HOME else home / ".codex")
    rows = [
        ("brew", "Homebrew downloads", _env_path("HOMEBREW_CACHE", home / "Library" / "Caches" / "Homebrew"),
         "safe", "downloaded again when needed", ["brew", "cleanup", "--prune=all"],
         "Downloaded again if you reinstall an old version."),
        ("claude-temp", "Claude Code temp files", tmp / f"claude-{os.getuid()}", "safe",
         "macOS empties it at restart", None, None),
        ("playwright", "Playwright browsers",
         _env_path("PLAYWRIGHT_BROWSERS_PATH", home / "Library" / "Caches" / "ms-playwright"), "safe",
         "browser tests download them again", ["npx", "--yes", "playwright", "uninstall", "--all"],
         "Browser tests download them again ({size})."),
        ("claude-chats", "Claude Code chats", claude / "projects", "keep", "your chat history, used to resume",
         None, None),
        ("codex-chats", "Codex chats", codex / "sessions", "keep", "your Codex chat history, used to resume",
         None, None),
        ("codex", "Codex app data", codex, "keep", "Codex's settings and tools", None, None),
        ("npm", "npm cache", home / ".npm", "safe", "downloaded again when needed",
         ["npm", "cache", "clean", "--force"], "npm downloads packages again when needed."),
        ("pnpm", "pnpm store", _env_path("PNPM_STORE_DIR", home / "Library" / "pnpm" / "store"), "safe",
         "packages no project uses are downloaded again when needed", ["pnpm", "store", "prune"],
         "Removes the packages no project uses; pnpm downloads them again when needed."),
        ("uv", "uv cache", _env_path("UV_CACHE_DIR", home / ".cache" / "uv"), "safe", "downloaded again when needed",
         ["uv", "cache", "clean"], "uv downloads packages again when needed."),
        ("devkit-tmp", "devkit scratch files", Path(dev_tmp or core.TMP_ROOT), "safe",
         "when no agent session is running", None, None),
    ]
    rows = [r for r in rows if r[2].exists()]

    def size(r):
        if r[0] == "codex":  # its chats and worktrees are counted on their own rows
            return core.du_kb(r[2], timeout=300, exclude="sessions") or 0
        return core.du_kb(r[2], timeout=300) or 0

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        sizes = list(ex.map(size, rows))
    out = []
    for (key, label, path, safety, why, cmd, risk), kb in zip(rows, sizes):
        if key == "codex":
            kb = max(0, kb - (core.du_kb(path / "worktrees", timeout=120) or 0 if (path / "worktrees").exists() else 0))
            part = _largest_part(path, {"sessions", "worktrees"})
            if part and part[1] >= kb * 0.3:
                why = f"{ui.size_text(part[1])} of it is {CODEX_PARTS.get(part[0], 'its ' + part[0] + ' folder')}"
        out.append({"key": key, "label": label, "path": str(path), "kb": kb, "safety": safety, "why": why,
                    "cmd": cmd, "risk": risk})
    return sorted(out, key=lambda r: -r["kb"])


def _largest_part(path: Path, leave_out: set):
    _, text, _ = core.sh(["du", "-k", "-d", "1", str(path)], timeout=120)
    parts = []
    for ln in text.splitlines():
        kb, _, p = ln.partition("\t")
        name = os.path.basename(p)
        if kb.isdigit() and p != str(path) and name not in leave_out:
            parts.append((name, int(kb)))
    return max(parts, key=lambda x: x[1]) if parts else None


# --------------------------------------------------------------------------- the plan


def _plural(n: int, word: str, many: str = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _changed(c: dict) -> int:
    return (c.get("modified") or 0) + (c.get("untracked") or 0)


def blocker_from(c: dict):
    """Why a checkout may hold work (measured), or None: a program in it, changed files, commits not pushed, ignored
    files gc.remove_worktree would keep it for."""
    n = _changed(c)
    if c.get("active"):
        return f"a program is working in {c['name']}. Close it first."
    if n:
        return f"{c['name']} has {_plural(n, 'changed file')}. Commit or discard {'it' if n == 1 else 'them'} first."
    if c.get("unpushed"):
        k = c["unpushed"]
        return (f"{c['name']} has {_plural(k, 'commit')} that {'is' if k == 1 else 'are'} not pushed. "
                f"Push {'it' if k == 1 else 'them'} first.")
    if c.get("held"):
        return f"{c['name']} stays: {c['held']}."
    return None


def _build_items(p: dict, c: dict) -> list:
    """A checkout's build output as plan items. What its tool fills again (gc.tool_owned: .next, .nuxt, .svelte-kit,
    DerivedData) is safe. Generic output names (dist, build, target, out, .output) can hold files no build step wrote,
    so they are their own item, check first, naming each folder with its size: only its number frees it, never
    dev free --safe."""
    folders = [f for f in c["folders"] if f["kind"] == "build"]
    where = {"main": f"{p['alias']}'s main checkout", "copy": f"the {c['name']} copy",
             "claude": f"the Claude Code worktree {c['name']}", "codex": f"the Codex worktree {c['name']}"}.get(
        c["kind"], f"the {c['name']} worktree")
    n = _changed(c)
    stay = f" Your {_plural(n, 'changed file')} stay." if n else ""
    busy = f"a program is working in {c['name']}. Close it first." if c["active"] else ""
    rebuilt = [f for f in folders if gc.tool_owned(os.path.join(c["path"], f["rel"]))]
    other = [f for f in folders if f not in rebuilt]
    shown = ", ".join(f"{ui.short_path(f['rel'], 40)} ({ui.size_text(f['kb'])})" for f in other[:5]) + (
        f" and {len(other) - 5} more" if len(other) > 5 else "")
    rust = " Cargo builds the Rust part again in a few minutes." if any(f.get("rust") for f in other) else ""
    check = f"No tool is known to fill {shown} again, and people keep files there too: look inside first."
    items = []
    for key, what, fs, safety, risk in (
            ("build", f"Build output in {where}", other, "check first", f"{check}{rust}{stay}"),
            ("build-rebuilt", f"Regenerable build output in {where}", rebuilt, "safe",
             f"Rebuilt by the next build.{stay}")):
        if fs:
            kb = sum(f["kb"] for f in fs)
            items.append({"key": f"{key}:{c['path']}", "kind": "build", "kb": kb, "what": what,
                          "safety": "check first" if busy else safety, "risk": busy or risk, "runs": None,
                          "project": p["alias"],
                          "targets": [{"path": c["path"], "name": c["name"], "folders": [f["rel"] for f in fs],
                                       "kb": kb}]})
    return items


def _removal(p: dict, c: dict) -> dict:
    return {"path": c["path"], "name": c["name"], "kb": c["kb"], "main": p["path"], "branch": c.get("branch"),
            "github": p.get("github"), "pr": c.get("pr"), "default": p.get("default_branch")}


def _project_items(p: dict) -> list:
    items, groups = [], {}
    for c in p["checkouts"]:
        if c["kind"] == "main":
            if not c["active"]:
                items += _build_items(p, c)
            continue
        removable = c["merged"] and c["kind"] not in ("codex",) and not c["locked"]
        if not removable:
            if c["kind"] != "codex":
                items += _build_items(p, c)
            continue
        if c["kind"] == "copy":
            why = blocker_from(c)
            pr = c.get("pr")
            risk = (f"PR {pr['number']} is {pr['state'].lower()}; its branch stays on GitHub." if pr and p.get("github")
                    else f"PR {pr['number']} is {pr['state'].lower()}; its branch stays on the remote." if pr
                    else f"PR {c['merged_pr']} merged it into {p['default_branch']}." if c.get("how") == "pr"
                    else f"Its commits have an equivalent in {p['default_branch']} (merged by content)."
                    if c.get("how") == "content" else f"Its branch has no commits beyond {p['default_branch']}.")
            items.append({"key": f"copy:{c['path']}", "kind": "copy", "kb": c["kb"], "what": f"The {c['name']} copy",
                          "safety": "check first" if why else "safe", "risk": why or risk, "runs": None,
                          "project": p["alias"], "targets": [_removal(p, c)]})
            continue
        groups.setdefault((c["kind"], bool(blocker_from(c))), []).append(c)
    for (kind, blocked), cs in sorted(groups.items()):
        label = {"claude": "Claude Code worktree", "temp": "temporary worktree"}.get(kind, "worktree")
        what = f"{_plural(len(cs), label)} of {p['alias']}"
        if blocked:
            reasons = [blocker_from(c) for c in cs]
            risk = reasons[0] + (f" {_plural(len(reasons) - 1, 'other has', 'others have')} work too."
                                 if len(reasons) > 1 else "")
        else:
            risk = ("All merged into main, no changes." if len(cs) > 1 else "Merged into main, no changes.").replace(
                "main", p["default_branch"])
            risk += (" Their chats stay, but those sessions can't continue in these folders." if kind == "claude"
                     else " Their branches stay on the remote.")
        items.append({"key": f"worktrees:{kind}:{int(blocked)}:{p['path']}", "kind": "worktrees",
                      "kb": sum(c["kb"] for c in cs), "what": what, "safety": "check first" if blocked else "safe",
                      "risk": risk, "runs": None, "project": p["alias"], "targets": [_removal(p, c) for c in cs]})
    return items


def returnable_kb(colima: dict, docker: dict) -> int:
    """Space freed inside Colima's virtual machine that macOS has not got back: the disk on the Mac minus what the
    virtual machine uses (or, when devkit cannot look inside it, minus what Docker reports)."""
    if not colima:
        return 0
    used = colima.get("vm_used_kb")
    if used is None:
        used = (docker or {}).get("total_kb") or 0
    return max(0, (colima.get("on_mac_kb") or 0) - used)


def plan(report: dict) -> list:
    """The reclaim plan: actions ranked by the space they free, numbered from 1, each with its safety, its risk and
    the command devkit runs. Items under MIN_ITEM_KB are left out; volumes and chats never appear."""
    items = []
    for p in report.get("projects") or []:
        items += _project_items(p)
    col, dock = report.get("colima"), report.get("docker") or {}
    if col:
        ret = col.get("returnable_kb") or 0
        running = bool(col.get("running"))
        items.append({"key": "trim", "kind": "trim", "kb": ret, "what": "Return Colima's free space to macOS",
                      "safety": "safe" if running else "check first",
                      "risk": "Deletes nothing; Docker keeps running." if running
                      else "Colima is not running. Start it first: dev docker up", "runs": "dev docker trim",
                      "project": None, "targets": [{"path": col["home"], "kb": ret}]})
    if dock.get("reachable"):
        unused = [im for im in dock.get("images") or [] if not im["in_use"]]
        kb = sum(im["kb"] for im in unused)
        if unused:
            items.append({"key": "images", "kind": "images", "kb": kb,
                          "what": f"{_plural(len(unused), 'Docker image')}, "
                                  f"{'none' if len(unused) == len(dock['images']) else 'not'} in use",
                          "safety": "safe", "risk": f"A tool that uses them downloads them again ({ui.size_text(kb)}).",
                          "runs": "docker image prune -a -f, then the trim" if col else "docker image prune -a -f",
                          "project": None, "targets": [{"images": [im["repo"] for im in unused], "kb": kb}]})
        bc = (dock.get("df") or {}).get("build_cache") or {}
        if bc.get("reclaimable_kb"):
            items.append({"key": "builder", "kind": "builder", "kb": bc["reclaimable_kb"], "what": "Docker build cache",
                          "safety": "safe", "risk": "The next image build takes longer.",
                          "runs": "docker builder prune -a -f, then the trim" if col else "docker builder prune -a -f",
                          "project": None, "targets": [{"kb": bc["reclaimable_kb"]}]})
    for r in report.get("agent_data") or []:
        if r.get("cmd") and r["safety"] == "safe" and core.have(r["cmd"][0]):
            items.append({"key": f"cache:{r['key']}", "kind": "cache", "kb": r["kb"], "what": r["label"],
                          "safety": "safe", "risk": (r.get("risk") or "").replace("{size}", f"about {_approx(r['kb'])}"),
                          "runs": " ".join(r["cmd"]), "project": None,
                          "targets": [{"path": r["path"], "key": r["key"], "cmd": r["cmd"], "kb": r["kb"]}]})
    items = sorted((i for i in items if i["kb"] >= MIN_ITEM_KB), key=lambda i: -i["kb"])
    for n, i in enumerate(items, 1):
        i["n"] = n
    return items


def measure(reg: dict, home=None, tmp=None, now=None) -> dict:
    """Measure everything `dev du` shows and build the plan. -> the report (not saved)."""
    with concurrent.futures.ThreadPoolExecutor(3) as ex:
        projects = ex.submit(measure_projects, reg)
        colima = ex.submit(colima_info, home)
        docker = ex.submit(docker_info)
        agents = ex.submit(agent_data, home, tmp)
        (projs, away), col, dock, data = projects.result(), colima.result(), docker.result(), agents.result()
    if col:
        col["returnable_kb"] = returnable_kb(col, dock)
    report = {"version": 1, "measured": now or time.time(), "projects": projs, "portfolio_only": away,
              "colima": col, "docker": dock, "agent_data": data, "freed": {}}
    report["plan"] = plan(report)
    return report


def save(report: dict):
    f = cache_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f".disk.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(report))
    os.replace(tmp, f)


def load():
    rep = core.read_json(cache_file(), None)
    return rep if isinstance(rep, dict) and rep.get("version") == 1 and "plan" in rep else None


def measure_now(refresh_prs: bool = False) -> dict:
    """Measure this Mac now and save it (the slow path: `--refresh`, the first run, or a stale cache)."""
    if sys.stderr.isatty():
        print(ui.dim("  Measuring disk use on this Mac (a minute or two the first time)…"), file=sys.stderr)
    rep = measure(registry.build(with_prs=True, refresh_prs=refresh_prs, with_sizes=False))
    save(rep)
    return rep


# --------------------------------------------------------------------------- the plan the developer saw


def _signature(report: dict) -> list:
    return [[i["n"], i["key"], i["kb"]] for i in report.get("plan") or []]


def mark_seen(report: dict):
    """Remember the plan as the developer saw it: `dev free <n>` acts only on that plan."""
    f = seen_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"measured": report.get("measured"), "shown": time.time(), "plan": _signature(report)}))


def same_plan(seen, report: dict) -> bool:
    """The plan the developer saw is the current one: the same items in the same order, each about the same size."""
    if not isinstance(seen, dict):
        return False
    a, b = seen.get("plan") or [], _signature(report)
    if len(a) != len(b):
        return False
    for (n1, k1, kb1), (n2, k2, kb2) in zip(a, b):
        if n1 != n2 or k1 != k2 or abs((kb1 or 0) - (kb2 or 0)) > SAME_SIZE * max(kb1 or 0, kb2 or 0, 1):
            return False
    return True


# --------------------------------------------------------------------------- rendering (Sati's design)

LABEL_W, SIZE_W = 30, 7


def _approx(kb) -> str:
    """Whole numbers from 10 GB up (about 33 GB, 798 GB), else the usual size."""
    if kb and kb >= 10 * 1024 * 1024:
        return f"{int(round(kb / 1024 / 1024))} GB"
    return ui.size_text(kb or 0)


def _cmd(s: str) -> str:
    return ui.cyan(s)


def _row(label: str, kb, state: str, cols: int, indent: int = 2, flag: bool = False) -> list:
    """`  Label                    915 MB  state…` with the state wrapped under its own column."""
    text = ui.fit_cell(("! " if flag else "") + label, LABEL_W - indent - 1)
    head = " " * indent + text + " " * (LABEL_W - indent - len(text))
    if flag:  # the needs-you glyph, amber; it reads the same without color
        head = " " * indent + ui.amber("!") + head[indent + 1:]
    size = ui.size_text(kb).rjust(SIZE_W)
    lead = " " * (LABEL_W + SIZE_W + 2)
    pieces = ui.wrap(state, ui.text_width(cols) - len(lead) - 1) if state else [""]
    return [(head + size + "  " + pieces[0]).rstrip()] + [lead + x for x in pieces[1:]]


def _folders_text(folders: list, kinds: int = 2) -> str:
    by = {}
    for f in folders:
        by.setdefault(f["kind"], []).append(f)
    ranked = sorted(by.items(), key=lambda kv: -sum(f["kb"] for f in kv[1]))
    parts = []
    for kind, fs in ranked:
        total = sum(f["kb"] for f in fs)
        if total < MIN_FOLDER_KB or len(parts) >= kinds:
            continue
        big = [f for f in fs if f["kb"] >= MIN_FOLDER_KB][:3]
        inner = len(big) > 1 and big[0]["kb"] >= 1024 * 1024
        names = ", ".join(ui.short_path(f["rel"], 40) + (f" {ui.size_text(f['kb'])}" if inner else "") for f in big)
        parts.append(f"{KIND[kind]} {ui.size_text(total)}" + (f" ({names})" if names else ""))
    return " · ".join(parts)


def _activity(c: dict, now) -> str:
    return "in use" if c.get("active") else ui.rel_time(c.get("last_activity"), now)


def _changes_text(c: dict, always: bool) -> str:
    m, u = c.get("modified") or 0, c.get("untracked") or 0
    if m or u:
        return ", ".join(x for x in (_plural(m, "changed file") if m else "", _plural(u, "new file") if u else "") if x)
    return "no changes" if always else ""


def state_text(c: dict, p: dict, now=None) -> str:
    """The state of a checkout in plain words: branch or PR, what is not pushed, changes, last activity."""
    default = p.get("default_branch") or "main"
    pr = c.get("pr")
    parts = []
    if c["kind"] != "main" and pr and pr["state"] in ("MERGED", "CLOSED"):
        parts.append(f"PR {pr['number']} {pr['state'].lower()}")
    elif c["kind"] != "main" and c.get("merged"):
        parts.append(f"merged into {default}" + (" by content" if c.get("how") == "content" else
                                                 f" by PR {c['merged_pr']}" if c.get("how") == "pr" else ""))
    else:
        parts.append(ui.fit_cell(c["branch"], 32) if c.get("branch") else "not on a branch")
        if pr:
            parts.append(f"PR {pr['number']} {pr['state'].lower()}")
        elif c["kind"] != "main" and p.get("github"):
            parts.append("no PR")
    if c.get("unpushed"):
        parts.append(f"{c['unpushed']} to push")
    ch = _changes_text(c, always=c["kind"] != "main")
    if ch:
        parts.append(ch)
    parts.append(_activity(c, now))
    return ", ".join(parts)


def _group_where(kind: str, cs: list) -> str:
    if kind == "claude":
        return "in .claude/worktrees"
    if kind == "codex":
        return "in ~/.codex/worktrees, managed by the Codex app"
    if kind == "copy":
        parents = {os.path.dirname(c["path"]) for c in cs}
        return f"next to the project in {ui.short_path(parents.pop(), 32)}" if len(parents) == 1 \
            else "outside the layout"
    if kind == "worktree":
        return "in " + ui.tilde_path(core.WT_ROOT)
    return "in temporary folders"


def _project_lines(p: dict, cols: int, now) -> list:
    out = ["", ui.rule_title(f"{p['alias']} · {ui.size_text(p['kb'])}", cols)]
    small = p["kb"] < SMALL_PROJECT_KB
    main = next((c for c in p["checkouts"] if c["kind"] == "main"), None)
    if main:
        out += _with_folders(_row("Main checkout", main["kb"], state_text(main, p, now), cols), main, small, 4, cols)
    groups = {}
    for c in p["checkouts"]:
        if c["kind"] != "main":
            groups.setdefault(c["kind"], []).append(c)
    for kind in ("worktree", "claude", "codex", "temp", "copy"):
        cs = groups.get(kind)
        if not cs:
            continue
        total = sum(c["kb"] for c in cs)
        if small:
            names = ", ".join(c.get("branch") or c["name"] for c in cs)
            out += _row(GROUP[kind], total, names, cols, flag=kind == "copy")
            continue
        out += _row(GROUP[kind], total, _group_where(kind, cs), cols, flag=kind == "copy")
        for c in cs:
            out += _with_folders(_row(c["name"], c["kb"], state_text(c, p, now), cols, indent=4), c, False, 6, cols)
    copies = sum(c["kb"] for c in p["checkouts"] if c["kind"] != "main")
    if main and copies > 2 * max(main["kb"], 1) and not small:
        why = "Why so big: each worktree and copy is a full checkout with its own build output and dependencies."
        seen = {}
        for c in p["checkouts"]:
            for f in c["folders"]:
                if f["kind"] == "build" and f["kb"] >= MIN_FOLDER_KB:
                    seen.setdefault(f["rel"], []).append(f["kb"])
        repeated = sorted(((max(v), k) for k, v in seen.items() if len(v) > 1), reverse=True)
        if repeated:
            why += f" Its {repeated[0][1]} folder alone takes up to {ui.size_text(repeated[0][0])} in each " \
                   f"checkout that built it."
        layout = not p.get("canonical_ok", True) or any(c["kind"] == "copy" for c in p["checkouts"])
        if layout and p.get("path"):
            why += " Fix the layout, copies included:"
        out += ui.para(why, cols)
        if layout and p.get("path"):
            out.append("  " + _cmd(f"dev adopt {ui.tilde_path(p['path'])}"))
    return out


def _with_folders(lines: list, c: dict, small: bool, indent: int, cols: int) -> list:
    if small:
        return lines
    ft = _folders_text(c["folders"])
    if not ft:
        return lines
    width = ui.text_width(cols) - 1
    if len(lines) == 1 and ui.visible_len(lines[0]) + 3 + len(ft) <= width and cols >= 100:
        return [lines[0] + "  " + ui.dim(ft)]
    return lines + [ui.dim(x) for x in ui.para(ft, cols, indent=indent)]


def _colima_lines(rep: dict, cols: int, plan_n: dict) -> list:
    col, dock = rep.get("colima"), rep.get("docker") or {}
    if not col:
        return []
    out = ["", ui.rule_title(f"Colima, the program that runs Docker · {ui.size_text(col['on_mac_kb'])}", cols)]
    text = "Colima keeps Docker in a small Linux virtual machine. Its disk is one file"
    text += (f" that can grow to {_approx(col['logical_kb'])}; today it takes {ui.size_text(col['on_mac_kb'])} on "
             f"your Mac." if col.get("logical_kb") else f"; today it takes {ui.size_text(col['on_mac_kb'])} on your Mac.")
    out += ui.para(text, cols)
    if not col.get("running"):
        out += ui.para("Colima is not running, so devkit can't look inside it. Start it to measure:", cols)
        out.append("  " + _cmd("dev docker up"))
        return out
    lines = []
    if dock.get("reachable"):
        df = dock.get("df") or {}
        imgs = dock.get("images") or []
        busy = sum(1 for im in imgs if im["in_use"])
        only = " only" if dock["total_kb"] < col["on_mac_kb"] / 2 else ""
        s = f"Docker holds{only} {ui.size_text(dock['total_kb'])} in it: {_plural(len(imgs), 'image')}"
        s += ", none in use" if imgs and not busy else f", {busy} in use" if imgs else ""
        empty = [w for k, w in (("containers", "containers"), ("volumes", "volumes"), ("build_cache", "build cache"))
                 if not (df.get(k) or {}).get("count") and not (df.get(k) or {}).get("kb")]
        full = [f"{_plural(df[k]['count'], w[:-1] if w.endswith('s') else w, w)} ({ui.size_text(df[k]['kb'])})"
                for k, w in (("containers", "containers"), ("volumes", "volumes"), ("build_cache", "build cache"))
                if (df.get(k) or {}).get("count") or (df.get(k) or {}).get("kb")]
        if full:
            s += ", " + ", ".join(full)
        if empty:
            s += ", and no " + (" or ".join([", ".join(empty[:-1]), empty[-1]]) if len(empty) > 1 else empty[0])
        lines.append(s + ".")
        vol = df.get("volumes") or {}
        if vol.get("count"):
            lines.append(f"Volumes hold your containers' data ({ui.size_text(vol['kb'])}); devkit never deletes them.")
    ret = col.get("returnable_kb") or 0
    if ret >= MIN_ITEM_KB or col["on_mac_kb"] > (dock.get("total_kb") or 0) + 2 * 1024 * 1024:  # Sati: over 2 GB
        n = plan_n.get("trim")
        lines.append(f"About {_approx(ret)} more is space Docker already freed in the virtual machine, which macOS "
                     f"has not got back. Returning it deletes nothing" + (f" (plan item {n})." if n else "."))
    out += ui.para(" ".join(lines), cols) if lines else []
    out += _image_lines(dock.get("images") or [], cols)
    return out


def _image_lines(images: list, cols: int) -> list:
    out, small = [], []
    for im in images:
        if im["kb"] < MIN_FOLDER_KB:
            small.append(im)
            continue
        kind = next((v for k, v in IMAGES.items() if k in im["repo"]), "image")
        out += _row(im["repo"], im["kb"], f"{kind}, {'in use' if im['in_use'] else 'not in use'}", cols, indent=4)
    if small:
        names = ", ".join(dict.fromkeys(im["repo"] for im in small))
        use = "in use" if all(im["in_use"] for im in small) else "not in use" if not any(
            im["in_use"] for im in small) else "some in use"
        out += _row(names, sum(im["kb"] for im in small), f"small images, {use}", cols, indent=4)
    return out


def _agent_lines(rep: dict, cols: int) -> list:
    rows = [r for r in rep.get("agent_data") or [] if r["kb"] >= MIN_FOLDER_KB // 5]  # from 10 MB
    if not rows:
        return []
    out = ["", ui.rule_title(f"Agent data and caches · {ui.size_text(sum(r['kb'] for r in rows))}", cols)]
    for r in rows:
        why = r["why"]
        out += _row(r["label"], r["kb"], f"{r['safety']} {why}" if why.startswith("when ") else
                    f"{r['safety']}: {why}", cols)
    return out


def _where_lines(rep: dict, cols: int) -> list:
    rows = []
    if rep.get("colima"):
        rows.append(("Colima (runs Docker)", rep["colima"]["on_mac_kb"]))
    rows += [(p["alias"], p["kb"]) for p in rep.get("projects") or []]
    agents = sum(r["kb"] for r in rep.get("agent_data") or [])
    if agents:
        rows.append(("Agent data and caches", agents))
    rows.sort(key=lambda r: -r[1])
    if not rows:
        return []
    top = max(r[1] for r in rows) or 1
    cells = max(12, min(40, 24 + (cols - 80) * 16 // 40))
    out = ["", "  " + ui.bold("Where the space goes")]
    for label, kb in rows:
        out.append("  " + ui.fit_cell(label, 23).ljust(23) + ui.size_text(kb).rjust(SIZE_W) + "  " +
                   ui.bar(kb / top, cells))
    return out


def _plan_lines(rep: dict, cols: int, only=None) -> list:
    items = [i for i in rep.get("plan") or [] if str(i["n"]) not in (rep.get("freed") or {})]
    if only is not None:
        items = [i for i in items if i.get("project") == only]
    total = sum(i["kb"] for i in items)
    safe = sum(i["kb"] for i in items if i["safety"] == "safe")
    if not items:
        return ["", ui.rule_title("Plan to free space", cols)] + ui.para(
            "Nothing is safe to free right now. Measure again later:", cols) + ["  " + _cmd("dev du --refresh")]
    note = "all safe" if safe == total else f"{_approx(safe)} of it safe now"
    w = ui.text_width(cols)
    s_col = w - 16
    out = ["", ui.rule_title(f"Plan to free space · about {_approx(total)}, {note}", cols),
           ui.dim("     FREES    WHAT".ljust(s_col) + "SAFETY")]
    lead = " " * 14
    for i in items:
        what = ui.wrap(i["what"], s_col - 14 - 1)
        safety = ui.green(i["safety"]) if i["safety"] == "safe" else ui.amber(i["safety"])
        out.append(f"  {str(i['n']):<3}{ui.size_text(i['kb']):<9}" + what[0].ljust(s_col - 14) + safety)
        out += [lead + x for x in what[1:]]
        cmd = f"dev free {i['n']}"
        runs = f"(runs {i['runs']})" if i.get("runs") else ""
        risk = ui.wrap(i["risk"][:1].upper() + i["risk"][1:], w - 14 - 1)
        tail = _cmd(cmd) + ("  " + ui.dim(runs) if runs else "")
        tail_len = len(cmd) + (2 + len(runs) if runs else 0)
        if cols >= 100 and len(risk) == 1 and 14 + len(risk[0]) + 3 + tail_len <= w - 1:
            out.append(lead + risk[0] + "   " + tail)
            continue
        out += [lead + x for x in risk]
        if runs and 14 + len(cmd) + 2 + len(runs) <= w - 1:
            gap = max(2, (w - 5) - 14 - len(cmd) - len(runs))
            out.append(lead + _cmd(cmd) + " " * gap + ui.dim(runs))
        else:
            out.append(lead + _cmd(cmd))
            if runs:
                out.append(lead + ui.dim(runs))
    out.append("")
    out += ui.para("Nothing has been deleted. dev free asks before it deletes anything and refuses folders with "
                   "changes that are not committed or not pushed. Free every safe item at once: dev free --safe. "
                   "Measure again: dev du --refresh", cols)
    return out


def _header(rep: dict, cols: int, now, disk) -> list:
    total, free_kb = disk or _disk_usage()
    rel = ui.rel_time(rep.get("measured"), now)
    out = ["", ui.rule_title(f"Disk use on this Mac · measured {'just now' if rel == 'now' else rel}", cols)]
    items = [i for i in rep.get("plan") or [] if str(i["n"]) not in (rep.get("freed") or {})]
    safe = sum(i["kb"] for i in items if i["safety"] == "safe")
    used = 1 - free_kb / max(total, 1)
    tail = (f"about {_approx(safe)} is safe to free: the plan at the end says how." if safe
            else "nothing is safe to free right now.")
    if used > 0.85:
        out.append("  " + ui.amber(f"Your Mac is almost full: {_approx(free_kb)} free of {_approx(total)}."))
        out += ui.para(tail[0].upper() + tail[1:], cols)
    elif used < 0.7:
        out += ui.para(f"Your Mac has {_approx(free_kb)} free of {_approx(total)}, so there is no rush. "
                       + (f"If you want space back, {tail}" if safe else tail[0].upper() + tail[1:]), cols)
    else:
        out += ui.para(f"Your Mac has {_approx(free_kb)} free of {_approx(total)}. {tail[0].upper() + tail[1:]}",
                       cols)
    freed = sum((v or {}).get("kb") or 0 for v in (rep.get("freed") or {}).values())
    if freed:
        out += [ui.dim(x) for x in ui.para(f"Since then dev free freed {ui.size_text(freed)}; dev du --refresh "
                                           f"measures again.", cols)]
    return out


def render(rep: dict, cols: int = None, now=None, project: str = None, plan_only: bool = False, disk=None) -> list:
    """The `dev du` screen as lines: the answer, where the space goes, each project, Colima, agent data and caches,
    then the plan (Sati's order). `project`: one project's section and its plan items; `plan_only`: the plan."""
    cols = cols or ui.width()
    now = now or time.time()
    if plan_only:
        return _plan_lines(rep, cols) + [""]
    plan_n = {i["kind"]: i["n"] for i in rep.get("plan") or []}
    projects = rep.get("projects") or []
    if project is not None:
        p = next((x for x in projects if x["alias"] == project), None)
        return (_project_lines(p, cols, now) + _plan_lines(rep, cols, only=project) + [""]) if p else []
    out = _header(rep, cols, now, disk) + _where_lines(rep, cols)
    for p in projects:
        out += _project_lines(p, cols, now)
    if not projects:
        out += ["", "  No projects on this Mac yet. Get one:", ui.command("dev clone <name>", 4)]
    out += _colima_lines(rep, cols, plan_n) + _agent_lines(rep, cols) + _plan_lines(rep, cols)
    away = rep.get("portfolio_only") or 0
    if away:
        out += ["", f"  {_plural(away, 'more project')} in your portfolio {'is' if away == 1 else 'are'} not on this "
                    f"Mac: {_cmd('dev ls --all')}"]
    return out + [""]


# --------------------------------------------------------------------------- dev du


def _pick(rep: dict, query: str):
    """The project of the report a query names: an alias or id, then a name that starts with it, then one that
    contains it. -> (alias, every match of the deciding level)."""
    q = (query or "").strip().lower()
    ps = rep.get("projects") or []
    for test in (lambda p: q in (p["alias"].lower(), p["id"].lower(), (p.get("name") or "").lower()),
                 lambda p: p["alias"].lower().startswith(q) or (p.get("name") or "").lower().startswith(q),
                 lambda p: q in p["alias"].lower() or q in (p.get("name") or "").lower()):
        hits = [p for p in ps if test(p)]
        if hits:
            return (hits[0]["alias"] if len(hits) == 1 else None), hits
    return None, []


def cmd_du(a) -> int:
    rep = None if a.refresh else load()
    if rep is None or time.time() - (rep.get("measured") or 0) > AUTO_REFRESH_S:
        rep = measure_now(refresh_prs=bool(a.refresh))
    if a.json:
        print(json.dumps(rep, indent=2))
        return 0
    project = None
    if getattr(a, "query", None):
        project, hits = _pick(rep, a.query)
        if not project:
            if hits:
                ui.err(f"“{a.query}” matches {len(hits)} projects. Pick one:")
                for p in hits:
                    print(ui.command(f"dev du {p['alias']}", 4), file=sys.stderr)
            else:
                ui.err(f"no project on this Mac matches “{a.query}”: dev ls lists them")
            return 2
    print("\n".join(render(rep, project=project, plan_only=bool(getattr(a, "plan", False)))))
    mark_seen(rep)
    return 0


# --------------------------------------------------------------------------- dev free


def _busy(path: str) -> bool:
    core._CWDS = None  # read the processes again: the check happens at the moment of deleting
    return core.is_active(path)


def merge_now(t: dict):
    """The merge test (branches.merge_test, on the registry's cached pull requests: no GitHub call) on a worktree's
    HEAD right before removing it: {base, sha, prs} when its branch is merged into origin/<default>, as an ancestor,
    by its own pull request or by content, for gc.remove_worktree to test again; else None. The plan read the same
    cache (registry.merge_states), so the plan and the removal agree."""
    from . import branches
    branch = t.get("branch")
    if not branch or not os.path.isdir(t["path"]):
        return None
    base = f"origin/{t.get('default') or registry.default_branch(Path(t['main']))}"
    prs = branches.cached_prs(t["github"]) if t.get("github") else None
    m = branches.merge_test(t["path"], branch, base, ref="HEAD", prs=prs)
    return {"base": base, "sha": m["sha"], "prs": prs} if m["merged"] else None


def blocker_now(path: str, name: str, merged: dict = None):
    """Why a checkout may hold work right now, or None (checked again just before removing it). `merged`: merge_now
    found its branch merged just now, so its commits on no remote have an equivalent in the default branch."""
    if not os.path.isdir(path):
        return f"{name} is no longer there. Measure again: dev du --refresh"
    if _busy(path):
        return f"a program is working in {name}. Close it first."
    rc, out, _ = core.git(path, "status", "--porcelain")
    if rc != 0:
        return f"git can't read {name}. Look at it first: git -C {ui.tilde_path(path)} status"
    n = len([x for x in out.splitlines() if x.strip()])
    if n:
        return f"{name} has {_plural(n, 'changed file')}. Commit or discard {'it' if n == 1 else 'them'} first."
    k = int(core.gitout(path, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0)
    if k and not merged:
        return (f"{name} has {_plural(k, 'commit')} that {'is' if k == 1 else 'are'} not pushed. "
                f"Push {'it' if k == 1 else 'them'} first.")
    return None


def _free_build(it: dict):
    t = it["targets"][0]
    if not os.path.isdir(t["path"]):
        return False, 0, f"{t['name']} is no longer there. Measure again: dev du --refresh"
    if _busy(t["path"]):
        return False, 0, f"a program is working in {t['name']}. Close it first."
    freed, gone, kept = 0, [], []
    for rel in t["folders"]:
        full = os.path.join(t["path"], rel)
        if os.path.islink(full) or not os.path.isdir(full):
            continue
        rc, tracked, _ = core.git(t["path"], "ls-files", "--", rel)
        if rc != 0 or tracked.strip():
            kept.append(rel)  # git tracks files in it (git add -f), so it is work, whatever check-ignore says
            continue
        if core.git(t["path"], "check-ignore", "-q", rel)[0] != 0:
            continue  # git no longer ignores it, so it may be work
        kb = core.du_kb(full, timeout=600) or 0
        shutil.rmtree(full, ignore_errors=True)
        if not os.path.exists(full):
            freed += kb
            gone.append(rel)
    held = (f"{', '.join(kept)} {'holds' if len(kept) == 1 else 'hold'} files git tracks, so "
            f"{'it stays' if len(kept) == 1 else 'they stay'}") if kept else ""
    if not gone:
        return False, 0, (f"in {t['name']}, {held}; nothing was deleted." if kept else
                          f"git no longer ignores the build output in {t['name']}, so nothing was deleted.")
    return True, freed, f"deleted {', '.join(gone)} in {ui.tilde_path(t['path'])}" + (f" ({held})" if held else "")


def _free_worktrees(it: dict):
    freed, done, refused = 0, [], []
    for t in it["targets"]:
        merged = merge_now(t)
        why = blocker_now(t["path"], t["name"], merged)
        if why:
            refused.append(why)
            continue
        pr = t.get("pr")
        ok, msg = gc.remove_worktree(t["main"], t["path"], t.get("branch") or "detached", t.get("github"),
                                     require_pr=pr if pr and pr.get("state") in ("MERGED", "CLOSED") else None,
                                     log=False, merged=merged)
        if ok:
            freed += t.get("kb") or 0
            done.append(ui.tilde_path(t["path"]))
        else:
            refused.append(msg)
    if refused:
        return False, freed, " ".join(refused) + (f" Removed: {', '.join(done)}." if done else "")
    return True, freed, "removed " + ", ".join(done)


def _free_cache(it: dict):
    t = it["targets"][0]
    cmd = t["cmd"]
    if not core.have(cmd[0]):
        return False, 0, f"{cmd[0]} is not installed, so devkit can't run {' '.join(cmd)}."
    probe = {"brew": r"Homebrew/brew\.rb|(^|/)brew(\s|$)", "playwright": r"ms-playwright|(^|/)playwright(\s|$)",
             "npm": r"(^|/)npm(\s|$)|npm-cli\.js", "pnpm": r"(^|/)pnpm(\s|$)|pnpm\.c?js", "uv": r"(^|/)uv(\s|$)"}
    needle = probe.get(t["key"])
    if needle and any(re.search(needle, p) for p in _processes()):
        return False, 0, f"{it['what']} is in use by a running program. Close it, then run dev free {it['n']} again."
    before = core.du_kb(t["path"], timeout=300) or 0
    print(ui.dim(f"  Running {' '.join(cmd)}…"))
    rc, out, err = core.sh(cmd, timeout=1800)
    after = (core.du_kb(t["path"], timeout=300) or 0) if os.path.exists(t["path"]) else 0
    if rc != 0:
        return False, max(0, before - after), f"{' '.join(cmd)} failed: {(err or out)[-200:]}"
    return True, max(0, before - after), f"ran {' '.join(cmd)}"


def _free_docker(it: dict):
    cmd = ["docker", "image", "prune", "-a", "-f"] if it["kind"] == "images" else \
        ["docker", "builder", "prune", "-a", "-f"]
    if core.sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15)[0] != 0:
        return False, 0, "Docker is not running. Start it first: dev docker up"
    rc, out, err = core.sh(cmd, timeout=900)
    if rc != 0:
        return False, 0, f"{' '.join(cmd)} failed: {(err or out)[-200:]}"
    m = re.search(r"Total reclaimed space:\s*(\S+)", out)
    return True, docker_kb(m.group(1)) if m else it["kb"], f"ran {' '.join(cmd)}"


def trim(home=None):
    """Return the space freed inside Colima's virtual machine to macOS (fstrim in each running profile).
    -> (ok, KB macOS got back, message)."""
    if not core.have("colima"):
        return False, 0, "Colima is not installed, so there is nothing to return."
    running = [str(p.get("name") or "default") for p in colima_profiles()
               if str(p.get("status", "")).lower() == "running"]
    if not running:
        return False, 0, "Colima is not running. Start it first: dev docker up"
    ch = colima_home(home)
    before = core.du_kb(ch, timeout=300) or 0
    errors = []
    for name in running:
        rc, out, err = core.sh(["colima", "ssh", "-p", name, "--", "sudo", "fstrim", "-av"], timeout=1800)
        if rc != 0:
            errors.append(f"{name}: {(err or out)[-200:]}")
    after = core.du_kb(ch, timeout=300) or 0
    got = max(0, before - after)
    if errors:
        return False, got, "fstrim failed inside Colima: " + "; ".join(errors)
    return True, got, f"ran fstrim in {', '.join(running)}"


def cmd_trim(a=None) -> int:
    print(ui.dim("  Returning Colima's free space to macOS (deletes nothing)…"))
    ok, got, msg = trim()
    if not ok:
        print(f"  {ui.red(ui.glyph('fail'))} Not returned: {msg}")
        return 1
    gc._log(f"free · docker trim: {msg}; macOS got back {ui.size_text(got)}")
    if got >= 1024:
        print(f"  {ui.green(ui.glyph('ok'))} Returned {ui.size_text(got)} to macOS.")
    else:
        print(f"  {ui.green(ui.glyph('ok'))} fstrim ran, but macOS got almost nothing back: the virtual machine's "
              f"disk had no free space to return, or it does not pass it on.")
    return 0


def _describe(it: dict) -> list:
    out = [f"  {str(it['n']):<3}{ui.size_text(it['kb']):<9}{it['what']}"]
    for t in it["targets"]:
        if t.get("folders"):
            for rel in t["folders"][:8]:
                out.append(" " * 14 + ui.dim(ui.tilde_path(os.path.join(t["path"], rel))))
            if len(t["folders"]) > 8:
                out.append(" " * 14 + ui.dim(f"and {len(t['folders']) - 8} more build folders"))
        elif t.get("main"):
            out.append(" " * 14 + ui.dim(f"{ui.tilde_path(t['path'])} ({ui.size_text(t.get('kb'))})"))
        elif t.get("images"):
            out.append(" " * 14 + ui.dim(", ".join(t["images"])))
        elif t.get("cmd"):
            out.append(" " * 14 + ui.dim(f"{ui.tilde_path(t['path'])} ({ui.size_text(t.get('kb'))})"))
    if it.get("runs"):
        out.append(" " * 14 + ui.dim(f"runs {it['runs']}"))
    return out


def free(numbers, safe_all: bool = False, yes: bool = False, ask=None, cols: int = None) -> int:
    """`dev free [<n>…] [--safe]`: without numbers, print the plan; with them, delete those items of the plan the
    developer saw (Hamann's (b)). -> exit code: 0 freed or printed, 1 refused or nothing deleted, 2 usage."""
    rep = load()
    if rep is None:
        rep = measure_now()
    if not numbers and not safe_all:
        print("\n".join(render(rep, cols=cols, plan_only=True)))
        mark_seen(rep)
        return 0
    seen = core.read_json(seen_file(), None)
    if not same_plan(seen, rep):
        print("\n".join(render(rep, cols=cols, plan_only=True)))
        mark_seen(rep)
        first = seen is None
        print("\n".join(ui.para(("dev free acts on the plan you saw last, and this is the first time you see it, so "
                                 if first else "The plan changed since you saw it, so ") +
                                "nothing was deleted. Check the plan above, then run dev free again with its numbers.",
                                cols)))
        return 1
    try:
        wanted = sorted({int(x) for x in numbers or []})
    except ValueError:
        ui.err("dev free takes plan item numbers, for example: dev free 2 3")
        return 2
    by_n = {i["n"]: i for i in rep["plan"]}
    bad = [n for n in wanted if n not in by_n]
    if bad:
        ui.err(f"there is no item {bad[0]} in the plan (it has {len(by_n)}): dev du --plan lists them")
        return 2
    freed = rep.setdefault("freed", {})
    if safe_all:
        wanted = sorted(set(wanted) | {i["n"] for i in rep["plan"] if i["safety"] == "safe"})
    chosen = [by_n[n] for n in wanted if str(n) not in freed]
    if not chosen:
        print(f"  {ui.glyph('none')} Nothing to free: {'those items were' if len(wanted) != 1 else 'that item was'} "
              f"freed already. Measure again: dev du --refresh")
        return 0
    print("\n" + ui.rule_title("What dev free deletes", cols))
    for it in chosen:
        print("\n".join(_describe(it)))
    if len(chosen) > 1:
        print(f"  In all, about {_approx(sum(i['kb'] for i in chosen))}.")
    if not yes:
        if ask is None and not sys.stdin.isatty():
            ui.err("dev free asks before it deletes anything: run it in a terminal, or pass --yes")
            return 2
        only_trim = all(i["kind"] == "trim" for i in chosen)
        prompt = "\n  Go ahead? [y/N] " if only_trim else f"\n  Delete {'it' if len(chosen) == 1 else 'them'}? [y/N] "
        if (ask or input)(prompt).strip().lower() not in ("y", "yes"):
            print("  Nothing was deleted.")
            return 1
    order = [i for i in chosen if i["kind"] != "trim"]
    trimmed = any(i["kind"] in ("trim", "images", "builder") for i in chosen) and rep.get("colima")
    results = []
    for it in order:
        fn = {"build": _free_build, "worktrees": _free_worktrees, "copy": _free_worktrees, "cache": _free_cache,
              "images": _free_docker, "builder": _free_docker}.get(it["kind"])
        results.append((it,) + (fn(it) if fn else (False, 0, "devkit does not know how to free this item.")))
    if trimmed:
        trim_item = next((i for i in chosen if i["kind"] == "trim"), None)
        ok, got, msg = trim()
        if trim_item or ok:
            results.append((trim_item or {"n": None, "what": "Return Colima's free space to macOS", "kind": "trim"},
                            ok, got, msg))
    total, failed = 0, 0
    for it, ok, kb, msg in results:
        tag = f"{it['n']}  " if len(results) > 1 and it.get("n") else ""
        if ok:
            total += kb
            if it.get("n"):
                freed[str(it["n"])] = {"at": time.time(), "kb": kb}
            gc._log(f"free · {it['what']}: {msg}; freed {ui.size_text(kb)}")
            print(f"  {ui.green(ui.glyph('ok'))} {tag}Freed {ui.size_text(kb)}." if it["kind"] != "trim" else
                  f"  {ui.green(ui.glyph('ok'))} {tag}macOS got back {ui.size_text(kb)}.")
        else:
            failed += 1
            from . import audit
            audit.record("ops", "free", f"refused {it['what']}: {msg}", "refused")
            print("\n".join(ui.para(f"Not freed: {msg}", cols, first=f"  {ui.red(ui.glyph('fail'))} {tag}")))
    if len(results) > 1:
        print(f"  Freed {ui.size_text(total)} in all.")
    save(rep)
    return 1 if failed else 0


def cmd_free(a) -> int:
    with core.Lock("free", wait=5) as ok:
        if not ok:
            ui.err("another dev free is running; wait for it to finish")
            return 1
        return free(getattr(a, "items", None) or [], safe_all=bool(getattr(a, "safe", False)),
                    yes=bool(getattr(a, "yes", False)))


def add_free_arguments(p):
    p.add_argument("items", nargs="*", metavar="n", help="plan item numbers, as dev du showed them")
    p.add_argument("--safe", action="store_true", help="every item of the plan marked safe")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask first (for scripts)")
    return p


def add_du_arguments(p):
    p.add_argument("query", nargs="?", help="one project: an alias or part of its name")
    p.add_argument("--refresh", action="store_true", help="measure again now instead of reading the cache")
    p.add_argument("--plan", action="store_true", help="only the plan to free space")
    p.add_argument("--json", action="store_true", help="the measurement and the plan as JSON (the dashboard)")
    return p
