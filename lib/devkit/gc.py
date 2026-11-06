# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Disk reclamation: worktrees whose work is merged, regenerable build output, stale temp.

Safety rules (checked when planning AND again right before acting):
  * never touch a checkout with uncommitted changes, untracked files, or commits not on a remote;
  * never touch a checkout where any process (agent session, shell, editor, server) is working;
  * never remove main checkouts; never remove Codex-app worktrees (the app manages them);
  * only delete build output that git reports as ignored AND whose name is a known artefact;
  * every action is logged in the handoff repo (log/YYYY-MM.md).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import core, registry
from .core import CFG, LOGDIR, git, gitout, human_kb, is_active, real, tilde

ARTEFACTS = {"node_modules", ".venv", "venv", "dist", "build", "target", ".next", ".nuxt", ".angular", ".turbo",
             ".parcel-cache", ".cache", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
             ".gradle", ".build", "DerivedData", ".output", ".svelte-kit", "storybook-static", ".nx"}


def _log(msg: str):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    f = LOGDIR / time.strftime("%Y-%m.md")
    if not f.exists():
        f.write_text(f"# Activity log {time.strftime('%Y-%m')}\n\n")
    with f.open("a") as fh:
        fh.write(f"- {core.fmt_ts(time.time())} · {msg}\n")


def artefacts(path: str, with_sizes=True):
    out = gitout(path, "status", "--ignored", "--porcelain", "--untracked-files=normal", timeout=60)
    found = []
    for ln in out.splitlines():
        if not ln.startswith("!! "):
            continue
        rel = ln[3:].rstrip("/")
        name = os.path.basename(rel)
        full = os.path.join(path, rel)
        if name in ARTEFACTS or (name == "vendor" and os.path.exists(os.path.join(os.path.dirname(full), "composer.json"))):
            if os.path.isdir(full) and not os.path.islink(full):
                found.append((full, core.du_kb(full) if with_sizes else None))
    return found


def _temp_entries():
    roots = [Path(real(os.environ.get("CLAUDE_CODE_TMPDIR", "/tmp"))) / f"claude-{os.getuid()}", core.TMP_ROOT]
    for root in roots:
        try:
            for e in os.scandir(root):
                yield root, e
        except OSError:
            continue


def _newest_mtime(path: str, limit=2000) -> float:
    newest, n = os.lstat(path).st_mtime, 0
    for dirpath, dirnames, filenames in os.walk(path):
        for f in filenames + dirnames:
            try:
                newest = max(newest, os.lstat(os.path.join(dirpath, f)).st_mtime)
            except OSError:
                pass
            n += 1
            if n > limit:
                return newest
    return newest


def plan(reg: dict, with_sizes=True) -> dict:
    g = CFG["gc"]
    grace, idle_art, temp_age = g["grace_hours"] * 3600, g["idle_artefact_days"] * 86400, g["temp_days"] * 86400
    now = time.time()
    actions, notes = [], []
    active_slugs = {core.claude_slug(c) for c in core.active_cwds()}
    for p in reg["projects"]:
        for w in p["worktrees"]:
            br = w.get("branch") or "detached"
            if w.get("missing"):
                actions.append({"kind": "prune-meta", "project": p["id"], "main": p["path"], "path": w["path"],
                                "reason": "registered worktree no longer exists on disk", "kb": 0, "auto": True})
                continue
            label = f"{p['id']} · {br} · {tilde(w['path'])}"
            idle = now - (w.get("last_activity") or now)
            if w.get("active"):
                notes.append(f"keep   {label} — active session")
                continue
            if w.get("locked"):
                notes.append(f"keep   {label} — locked")
                continue
            if w.get("modified") or w.get("untracked"):
                notes.append(f"keep   {label} — uncommitted work ({registry._state(w)})")
                continue
            if w.get("unpushed"):
                notes.append(f"keep   {label} — {w['unpushed']} commit(s) not on any remote")
                continue
            pr = w.get("pr")
            removable = w.get("location") != "codex"
            if removable and pr and pr["state"] in ("MERGED", "CLOSED") and idle > grace:
                actions.append({"kind": "remove-worktree", "project": p["id"], "main": p["path"], "path": w["path"],
                                "branch": br, "github": p.get("github"), "pr": pr,
                                "reason": f"PR #{pr['number']} {pr['state'].lower()}, idle {core.ago(now - idle)}",
                                "kb": w.get("size_kb") or 0, "auto": True})
            elif removable and w.get("ahead_default") == 0 and idle > grace:
                actions.append({"kind": "remove-worktree", "project": p["id"], "main": p["path"], "path": w["path"],
                                "branch": br, "github": p.get("github"), "pr": None,
                                "reason": f"no commits beyond origin/{p['default_branch']}, idle {core.ago(now - idle)}",
                                "kb": w.get("size_kb") or 0, "auto": True})
            elif idle > idle_art:
                arts = artefacts(w["path"], with_sizes)
                if arts:
                    actions.append({"kind": "prune-artefacts", "project": p["id"], "path": w["path"], "branch": br,
                                    "items": [a for a, _ in arts], "kb": sum(k or 0 for _, k in arts),
                                    "reason": f"idle {int(idle // 86400)}d; regenerable build output", "auto": True})
                else:
                    notes.append(f"keep   {label} — idle, nothing to prune")
            else:
                why = f"PR #{pr['number']} {pr['state'].lower()}" if pr else "work in progress"
                notes.append(f"keep   {label} — {why}, idle {core.ago(now - idle).replace(' ago', '')}")
    for root, e in _temp_entries():
        try:
            if e.name in active_slugs or any(e.name.startswith(s) for s in active_slugs):
                continue
            age = now - _newest_mtime(e.path) if e.is_dir(follow_symlinks=False) else now - e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if age > temp_age:
            kb = core.du_kb(e.path, timeout=60) if with_sizes else None
            actions.append({"kind": "purge-temp", "path": e.path, "kb": kb or 0, "auto": True,
                            "reason": f"temp entry untouched for {int(age // 86400)}d"})
    return {"actions": actions, "notes": notes,
            "reclaimable_kb": sum(a.get("kb") or 0 for a in actions)}


def _still_safe(path: str):
    """Re-verify a linked worktree right before deleting anything in it. -> (ok, reason)."""
    core._CWDS = None  # refresh process list
    if is_active(path):
        return False, "a process is now working in it"
    rc, out, _ = git(path, "status", "--porcelain")
    if rc != 0:
        return False, "git status failed"
    if out.strip():
        return False, "it has uncommitted or untracked changes"
    if int(gitout(path, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0):
        return False, "it has commits that are on no remote"
    return True, ""


def remove_worktree(main: str, path: str, branch: str = None, github: str = None, require_pr=None, log=True):
    """Safe removal used by `dev wt rm` and gc. -> (ok, message)."""
    ok, why = _still_safe(path)
    if not ok:
        return False, f"kept {tilde(path)}: {why}"
    if require_pr is not None and github and branch:
        fresh = registry.pr_fresh(github, branch)
        if fresh == "unknown" or not fresh or fresh.get("state") not in ("MERGED", "CLOSED"):
            return False, f"kept {tilde(path)}: PR state could not be confirmed as merged/closed"
    kb = core.du_kb(path, timeout=120) or 0
    rc, _, err = git(main, "worktree", "remove", path, timeout=300)
    if rc != 0:
        return False, f"git worktree remove failed for {tilde(path)}: {err}"
    msg = f"removed worktree {tilde(path)} ({branch})"
    if branch and branch != "detached":
        if int(gitout(main, "rev-list", "--count", branch, "--not", "--remotes") or 1) == 0:
            git(main, "branch", "-D", branch)
            msg += "; local branch deleted (still on the remote)"
    tmp = Path(real(os.environ.get("CLAUDE_CODE_TMPDIR", "/tmp"))) / f"claude-{os.getuid()}" / core.claude_slug(path)
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    msg += f"; freed {human_kb(kb)}"
    if log:
        _log("gc · " + msg)
    return True, msg


def apply(pl: dict, only_auto=True) -> list:
    results = []
    for a in pl["actions"]:
        if only_auto and not a.get("auto"):
            continue
        k = a["kind"]
        if k == "remove-worktree":
            ok, msg = remove_worktree(a["main"], a["path"], a.get("branch"), a.get("github"),
                                      require_pr=a.get("pr"))
        elif k == "prune-artefacts":
            ok, why = _still_safe(a["path"])
            if not ok:
                results.append((False, f"kept artefacts in {tilde(a['path'])}: {why}"))
                continue
            freed = []
            for item in a["items"]:
                rc, _, _ = git(a["path"], "check-ignore", "-q", item)
                if rc == 0 and os.path.isdir(item) and not os.path.islink(item):
                    shutil.rmtree(item, ignore_errors=True)
                    freed.append(os.path.relpath(item, a["path"]))
            ok, msg = True, f"pruned {', '.join(freed)} in {tilde(a['path'])}; freed {human_kb(a['kb'])}"
            _log("gc · " + msg)
        elif k == "purge-temp":
            p = a["path"]
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass
            ok, msg = True, f"purged temp {tilde(p)} ({human_kb(a['kb'])})"
        elif k == "prune-meta":
            git(a["main"], "worktree", "prune")
            ok, msg = True, f"pruned stale worktree metadata in {tilde(a['main'])}"
            _log("gc · " + msg)
        else:
            continue
        results.append((ok, msg))
    if core.have("docker") and CFG["gc"].get("docker", True):
        rc, _, _ = core.sh(["docker", "info"], timeout=10)
        if rc == 0:
            core.sh(["docker", "image", "prune", "-f"], timeout=120)
            core.sh(["docker", "builder", "prune", "-f", "--filter", "until=168h"], timeout=300)
            results.append((True, "docker: pruned dangling images and build cache older than 7 days"))
    return results
