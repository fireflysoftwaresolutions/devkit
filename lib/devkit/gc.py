# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
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
    from . import audit
    audit.record("ops", msg.split(" · ")[0][:60], msg)
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


def _branch_plan(p: dict):
    """`dev branch prune`'s plan for the project's main checkout on this Mac (the same merge test), or None when it
    cannot be made (no checkout here, not a repository, git or GitHub failing)."""
    if not p.get("path") or not os.path.isdir(p["path"]):
        return None
    from . import branches
    try:
        return branches.prune_plan(p["path"])
    except Exception:  # one broken repository must not stop the cleanup of the others
        return None


def _moved_at(top: str, name: str) -> int:
    """When the local branch last moved: its newest reflog entry, else its last commit. Seconds since the epoch."""
    import re
    stamps = [int(x) for x in re.findall(r"@\{(\d+)\}", gitout(top, "reflog", "show", "--date=unix", "--format=%gd",
                                                             f"refs/heads/{name}", "--", timeout=30))]
    return max(stamps) if stamps else int(gitout(top, "log", "-1", "--format=%ct", f"refs/heads/{name}") or 0)


def _branch_actions(p: dict, bp: dict, grace: float, now: float, actions: list, notes: list) -> None:
    """The merged branches the nightly job deletes: a local branch without a worktree that has not moved for the grace
    period (the worktree loop takes those with one), and a remote branch whose pull request merged or that devkit itself
    pushed as a backup (dev backup, dev sync); anyone else's merged remote branch waits for dev branch prune --apply."""
    for r in bp["branches"]:
        if r["action"] != "delete" or (r["where"] == "local" and r["worktree"]):
            continue
        label = f"{p['id']} · {'origin/' if r['where'] == 'remote' else ''}{r['name']}"
        if r["where"] == "remote" and not (r["pr_merged"] or r["backup"]):
            notes.append(f"keep   {label} — {r['why']}, but devkit did not push it and no pull request of it merged: "
                         f"dev branch prune --apply deletes it")
            continue
        moved = _moved_at(bp["top"], r["name"]) if r["where"] == "local" else 0
        if r["where"] == "local" and now - moved < grace:
            notes.append(f"keep   {label} — {r['why']}, but it moved {core.ago(moved)}")
            continue
        actions.append({"kind": "prune-branch", "project": p["id"], "main": p["path"], "where": r["where"],
                        "branch": r["name"], "path": label.split(" · ", 1)[1], "reason": r["why"], "kb": 0,
                        "auto": True, "plan": bp, "row": r})


def plan(reg: dict, with_sizes=True) -> dict:
    g = CFG["gc"]
    grace, idle_art, temp_age = g["grace_hours"] * 3600, g["idle_artefact_days"] * 86400, g["temp_days"] * 86400
    now = time.time()
    actions, notes = [], []
    active_slugs = {core.claude_slug(c) for c in core.active_cwds()}
    for p in reg["projects"]:
        bp = _branch_plan(p)
        rows = {(r["where"], r["name"]): r for r in (bp or {}).get("branches") or []}
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
            row = rows.get(("local", br))
            merged = row if row and row["state"] == "merged" and core.real(row["worktree"] or "") == core.real(
                w["path"]) else None  # the merge test of dev branch prune: ancestor, its pull request, or content
            if w.get("unpushed") and not merged:
                notes.append(f"keep   {label} — {w['unpushed']} commit(s) not on any remote")
                continue
            pr = w.get("pr")
            removable = w.get("location") != "codex"
            if merged and removable and idle > grace:
                actions.append({"kind": "remove-worktree", "project": p["id"], "main": p["path"], "path": w["path"],
                                "branch": br, "github": p.get("github"), "pr": None,
                                "merged": {"base": bp["base"], "sha": merged["sha"], "prs": bp.get("prs")},
                                "reason": f"{merged['merge']}, idle {core.ago(now - idle)}",
                                "kb": w.get("size_kb") or 0, "auto": True})
            elif removable and pr and pr["state"] in ("MERGED", "CLOSED") and idle > grace:
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
        if bp:
            _branch_actions(p, bp, grace, now, actions, notes)
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


def _merged_now(path: str, branch: str, merged: dict):
    """The merge test again, right before deleting: the worktree's HEAD is still the commit the caller tested and is
    still merged into merged["base"] -> the test's result, or None."""
    from . import branches
    if not merged or not branch or gitout(path, "rev-parse", "HEAD") != merged.get("sha"):
        return None
    m = branches.merge_test(path, branch, merged["base"], ref="HEAD", prs=merged.get("prs"))
    return m if m["merged"] else None


def _still_safe(path: str, branch: str = None, merged: dict = None):
    """Re-verify a linked worktree right before deleting anything in it. `merged`: {base, sha, prs} when the caller
    found its branch merged (branches.merge_test): commits on no remote may go if they are merged into base. -> (ok,
    reason)."""
    core._CWDS = None  # refresh process list
    if is_active(path):
        return False, "a process is now working in it"
    rc, out, _ = git(path, "status", "--porcelain")
    if rc != 0:
        return False, "git status failed"
    if out.strip():
        return False, "it has uncommitted or untracked changes"
    if int(gitout(path, "rev-list", "--count", "HEAD", "--not", "--remotes") or 0) and \
            not _merged_now(path, branch, merged):
        return False, "it has commits that are on no remote" + (
            f" and not merged into {merged['base']}" if merged else "")
    return True, ""


def remove_worktree(main: str, path: str, branch: str = None, github: str = None, require_pr=None, log=True,
                    merged: dict = None):
    """Safe removal used by `dev wt rm`, gc, `dev branch prune` and `dev pr merge`. `merged`: {base, sha, prs} when the
    caller's merge test found the branch merged; it is tested again here. -> (ok, message)."""
    ok, why = _still_safe(path, branch, merged)
    if not ok:
        return False, f"kept {tilde(path)}: {why}"
    landed = _merged_now(path, branch, merged) if merged else None
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
        elif landed and gitout(main, "rev-parse", "--verify", "-q", f"refs/heads/{branch}") == merged.get("sha"):
            from . import branches
            git(main, "branch", "-D", branch)
            msg += f"; local branch deleted ({branches.merge_words(landed, merged['base'])})"
    tmp = Path(real(os.environ.get("CLAUDE_CODE_TMPDIR", "/tmp"))) / f"claude-{os.getuid()}" / core.claude_slug(path)
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    msg += f"; freed {human_kb(kb)}"
    if log:
        _log("gc · " + msg)
    return True, msg


def apply(pl: dict, only_auto=True, remote=True) -> list:
    """Carry out the plan's actions (only the automatic ones by default) -> [(ok, message)]. With remote False it keeps
    every branch on origin, for a run that may not reach it (dev sync --offline or --no-push)."""
    results = []
    for a in pl["actions"]:
        if only_auto and not a.get("auto"):
            continue
        k = a["kind"]
        if k == "remove-worktree":
            ok, msg = remove_worktree(a["main"], a["path"], a.get("branch"), a.get("github"),
                                      require_pr=a.get("pr"), merged=a.get("merged"))
        elif k == "prune-branch":
            if a["where"] == "remote" and not remote:
                results.append((False, f"kept remote branch {a['path']}: this run does not reach origin"))
                continue
            from . import branches
            done = branches.prune(a["plan"], only=lambda x, row=a["row"]: x is row)
            r = done[0] if done else dict(a["row"], result="failed", error="not deleted")
            ok = r.get("result") == "deleted"
            msg = (f"deleted {a['where']} branch {a['path']} ({a['reason']})" if ok else
                   f"kept {a['where']} branch {a['path']}: {r.get('error') or 'not deleted'}")
            _log("gc · " + msg)
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
