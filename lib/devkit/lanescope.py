# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Where a crew lane's file scope lands.

A lane's scope is a list of globs. A relative glob counts from the checkout of the run's repository the session
works in (scope_base), never reaching outside it, also through a link; an absolute or ~ glob names a folder anywhere.
worktree_state and written_paths tell what a shell call wrote, and scope_owner_across which member of a live run holds
a file.

    scope_base(project, cwd) -> str                  the folder relative globs count from
    scope_holds(path, cwd, globs, base=None) -> bool whether one of the globs holds the path
    scope_rel(path, cwd, base=None) -> str           the path relative to that folder ('..' outside it)
    scope_owner_across(path, runs=None, ...)         (member, globs) of the live lane that holds the path, or None
    worktree_state(top) / written_paths(before, after)
    current_project(cwd) / in_checkout(path)
"""
from __future__ import annotations

import os
from pathlib import Path

from . import core
from .core import DEV, is_temp, is_under, real
from .shellparse import UNKNOWN

def _glob_hit(path: str, g: str) -> bool:
    """Does scope glob `g` hold `path`? By the lanes' own rules (progress.scope_match): `*` stays inside one segment,
    so `src/pkg/*.py` holds no file of src/pkg/studio/, and a folder holds what lies below it. The edit check and
    `dev commit` must read a scope the same way, or one lane commits another's file."""
    from . import progress
    return progress.scope_match(path, g)


def scope_holds(path, cwd, globs, base=None) -> bool:
    """True when `path` is in one of `globs`, by the crew scope rules: a glob relative to the lane's repository
    (`base`, from scope_base; without it the repository of `cwd`) such as `lib/**`, `docs/` or a file never reaches
    outside it, also through a link (`shared/**` where shared leads elsewhere: the write lands where it leads); an
    absolute or ~ glob names a folder anywhere, such as the project's plans in the handoff repository."""
    ap = os.path.normpath(os.path.join(str(cwd), str(path)))
    top = _scope_top(cwd, base)
    rel = scope_rel(ap, cwd, top)
    inside = not rel.startswith("..") and not os.path.relpath(real(ap), real(top)).startswith("..")
    for g in globs or []:
        if g.startswith(("/", "~")):
            ga = os.path.expanduser(g)
            if _glob_hit(ap, ga) or _glob_hit(real(ap), ga) or _glob_hit(real(ap), _real_glob(ga)):
                return True
        elif inside and _glob_hit(rel, g):
            return True
    return False


def _scope_top(cwd, base=None) -> str:
    """The folder a lane's scope globs count from: `base`, else the repository of `cwd`, else `cwd`."""
    return str(base) if base else (core.gitout(cwd, "rev-parse", "--show-toplevel") or str(cwd))


def _real_glob(g: str) -> str:
    """An absolute glob with its literal folder part resolved (/tmp/x/** → /private/tmp/x/**)."""
    parts = g.split("/")
    k = next((j for j, p in enumerate(parts) if any(ch in p for ch in "*?[")), len(parts))
    return "/".join([real("/".join(parts[:k]) or "/")] + parts[k:]) if k else g


def scope_rel(path, cwd, base=None) -> str:
    """`path` relative to the folder a lane's scope globs count from (`base`, else the repository of `cwd`); it
    starts with '..' outside it. Symbolic links are resolved when the plain paths disagree (/var and /private/var)."""
    ap = os.path.normpath(os.path.join(str(cwd), str(path)))
    top = _scope_top(cwd, base)
    rel = os.path.relpath(ap, top)
    return os.path.relpath(real(ap), real(top)) if rel.startswith("..") else rel


def _common_dir(path) -> str:
    c = core.gitout(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return real(c) if c else ""


def scope_base(project, cwd) -> str:
    """The folder a crew lane's scope globs are relative to: the checkout of the run's repository
    (`project`, its id under the workspace) that the session works in, its main checkout or one of its worktrees;
    else that repository's main checkout, so a session whose folder is ~ (Claude Desktop) or another repository still
    counts from it; with no run, the checkout of `cwd`, else `cwd`."""
    cwd = str(cwd or os.getcwd())
    top = core.gitout(cwd, "rev-parse", "--show-toplevel") if os.path.isdir(cwd) else ""
    main = Path(DEV) / str(project) if project else None
    if main is not None and (main / ".git").exists():
        if top and _common_dir(top) and _common_dir(top) == _common_dir(main):
            return top
        return str(main)
    return top or cwd


def worktree_state(top) -> dict | None:
    """What `git status` lists in the checkout at `top`, as {path: (status, size, mtime_ns)} for every changed or
    untracked path (ignored paths left out); None outside a repository, when git fails, and on any other error,
    which goes to the hooks log."""
    if not top or not os.path.isdir(str(top)):
        return None
    try:
        rc, out, _ = core.sh(["git", "-C", str(top), "status", "--porcelain=v1", "-z", "--untracked-files=all",
                              "--no-renames", "--ignore-submodules=dirty"], timeout=10)
        if rc != 0:
            return None
        state = {}
        for entry in out.split("\0"):
            if len(entry) < 4:
                continue
            path = entry[3:]
            try:
                st = os.lstat(os.path.join(str(top), path))
                state[path] = (entry[:2], st.st_size, st.st_mtime_ns)
            except OSError:
                state[path] = (entry[:2], None, None)
        return state
    except Exception as e:
        _log(f"worktree_state {top}: {type(e).__name__}: {e}")
        return None


def written_paths(before, after) -> list:
    """The paths a call wrote, from worktree_state before and after it: new in the list, changed again (status, size
    or time), or gone from it (restored to the committed version). [] when either state is unknown."""
    if before is None or after is None:
        return []
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def run_checkout(run) -> str:
    """The checkout a run works in: its session's folder (the session record's `top`), else its repository's main
    checkout under the workspace; '' when neither is known."""
    try:
        from . import progress
        sess = core.read_json(progress.session_path(run.get("agent") or "claude", str(run.get("session") or "-")),
                              {}) or {}
        if isinstance(sess, dict) and sess.get("top"):
            return str(sess["top"])
    except Exception as e:
        _log(f"run_checkout: {type(e).__name__}: {e}")
    main = Path(DEV) / str(run.get("project") or "")
    return str(main) if run.get("project") and (main / ".git").exists() else ""


def scope_owner_across(path, runs=None, me_session="", checkout_of=None):
    """The crew member of a live run that owns `path` -> (who, globs) or None: a running lane, in a run of another
    session (any session when `me_session` is ''), working in the checkout that holds `path` (worktrees of one
    repository hold separate files), whose own scope or the scope of an open step it owns holds the path."""
    if runs is None:
        from . import progress
        runs = progress.active_runs()
    checkout_of = checkout_of or run_checkout
    ap = real(path)
    for run in runs or []:
        if not isinstance(run, dict) or run.get("state") == "done" or \
                (me_session and str(run.get("session") or "") == str(me_session)):
            continue
        top = checkout_of(run)
        if not top or not is_under(Path(ap), Path(real(top))):
            continue
        rel = os.path.relpath(ap, real(top))
        for k, ln in (run.get("agents") or {}).items():
            if not isinstance(ln, dict) or ln.get("state") != "running":
                continue
            globs = set(ln.get("scope") or [])
            for st in run.get("plan") or []:
                if isinstance(st, dict) and st.get("owner") == k and st.get("state") not in ("done", "skipped"):
                    globs.update(st.get("scope") or [])
            hit = any(_glob_hit(ap, os.path.expanduser(g)) if g.startswith(("/", "~")) else _glob_hit(rel, g)
                      for g in globs if isinstance(g, str) and g)
            if hit:
                return (ln.get("persona") or ln.get("label") or "a crew member", sorted(globs))
    return None


def _log(msg: str) -> None:
    """One line in the hooks log; never raises."""
    try:
        from . import hooks
        hooks._log(f"lanescope {msg}")
    except Exception:
        pass


def current_project(cwd):
    """The project id of a working folder, or None outside a project's checkout (a temporary repository and the
    handoff repository are none)."""
    if not cwd or str(cwd) == str(UNKNOWN):
        return None
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    if not top or is_temp(top) or real(top) == real(core.HANDOFF):
        return None
    from .registry import main_of
    main = main_of(Path(top)) or Path(top)
    return core.project_identity(main, core.gitout(main, "config", "--get", "remote.origin.url"))[0]


def in_checkout(path) -> bool:
    """True when `path` lies in a git checkout (a worktree, or a repository in a temporary folder)."""
    d = os.path.dirname(real(path))
    while d and not os.path.isdir(d):
        d = os.path.dirname(d)
    return bool(d) and bool(core.gitout(d, "rev-parse", "--show-toplevel"))
