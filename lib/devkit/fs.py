# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Token-light, safe file commands for agents and people: rm, mv, cp, files, find, grep, diff.

Claude Code and Codex call these through their shell tool, so the output follows four rules:
  * compact by default, in a deterministic order;
  * plain text (no colour) when stdout is not a TTY;
  * every list is capped and ends with an explicit `+N more — narrow with …` line;
  * a refusal names the reason and the flag that overrides it (or says there is none), so one
    retry fixes the command. A refused batch changes nothing.

`dev rm` never unlinks. It moves paths to the macOS Trash (/usr/bin/trash) and appends one line per
item to STATE/trash.jsonl, so `dev rm --undo` can put them back. `trash_paths` is the only function
that touches the Trash; tests replace it.

Stdlib only, Python 3.9 compatible.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import core, guard, ui
from .gc import ARTEFACTS

CAP_FILES, CAP_FIND, CAP_GREP, CAP_PER_FILE, CAP_DIFF, FOLD_AT = 80, 60, 60, 5, 300, 300
LINE_WIDTH = 160
BIG_FILE, BIG_DIR = 100 * 1024, 1024 * 1024          # sizes are shown from here on
GROUP_MIN_FILES, GROUP_MIN_SAME = 20, 8               # dirs with >20 files fold extensions with >=8 members
NOISE = set(ARTEFACTS) | {".git", ".hg", ".svn", ".DS_Store"}
HEAVY = {".git", "node_modules", ".venv", "venv"}
INDEXERS = ("mds", "mdworker", "mdsync", "fseventsd", "Spotlight", "corespotlightd", "revisiond", "lsof")
# Gitignored files that git cannot restore and that usually hold local configuration, keys or data.
SENSITIVE_RE = re.compile(r"^(\.env(\..+)?|.*\.(pem|key|p12|pfx|jks|keystore|kdbx|tfstate|tfvars|sqlite3?|db)|"
                          r"id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|\.npmrc|\.pypirc|\.netrc|.*credentials.*|"
                          r".*secrets?.*|.*\.local(\..+)?)$", re.I)
SIBLING_RE = re.compile(r"^(?P<base>.+?)(?:[-_. ](?:copy|copia|pr-?\d+|old|new|bak|backup|orig|tmp|temp|wip|v?\d+)"
                        r"|\s\(\d+\))$", re.I)
HANDOFF_DIRS = {"projects", "plans", "items", "sessions", "presence", "log", "initiatives"}
HANDOFF_DEVKIT_ONLY = {"items", "sessions", "presence", "log"}
TRASH_BIN = "/usr/bin/trash"
LSOF = "/usr/sbin/lsof" if os.path.exists("/usr/sbin/lsof") else "lsof"
USE_FD = True  # tests switch fd off so results do not depend on what is installed

FLAG_ORDER = ("-r", "--tracked", "--discard", "--force-in-use", "--outside")
FLAG_ATTR = {"-r": "recursive", "--tracked": "tracked", "--discard": "discard", "--force-in-use": "force_in_use",
             "--outside": "outside"}

# --------------------------------------------------------------------------- small helpers

_TOP: dict = {}


def _reset():
    """Forget cached repository roots and process cwds (state can change between commands)."""
    _TOP.clear()
    core._CWDS = None


def _run(args, cwd=None, timeout=120, env=None):
    """Like core.sh, but never fails on non-UTF-8 output (file contents, odd file names) and keeps it as is."""
    try:
        p = subprocess.run([str(x) for x in args], cwd=cwd, capture_output=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env or core.GIT_ENV, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 127, "", str(e)


def _git(cwd, *args, timeout=120):
    return _run(["git", "-C", str(cwd), "-c", "core.quotepath=off", *args], timeout=timeout)


def _gitout(cwd, *args, timeout=120) -> str:
    rc, out, _ = _git(cwd, *args, timeout=timeout)
    return out if rc == 0 else ""


def _tty() -> bool:
    return ui.COLOR and sys.stdout.isatty()


def _paint(fn, s):
    return fn(s) if _tty() else s


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _hb(n) -> str:
    """Human size from bytes."""
    if n is None:
        return "-"
    return f"{int(n)} B" if n < 1024 else core.human_kb(n / 1024)


def _target(p) -> str:
    """Absolute path with every parent resolved but the last component kept (a symlink stays the link)."""
    a = os.path.abspath(os.path.expanduser(str(p)))
    parent, name = os.path.split(a)
    return os.path.join(core.real(parent), name) if name else a


def _under(path, root) -> bool:
    """`path` (already absolute, see _target) is `root` or inside it. Does not follow a final symlink."""
    r = core.real(root).rstrip("/")
    return not r or path == r or path.startswith(r + "/")


def is_temp(path) -> bool:
    """Temp roots and the agent scratchpad. Tests replace this to model a workspace outside temp."""
    return any(_under(path, r) for r in core.temp_roots())


def _cwd() -> str:
    try:
        return core.real(os.getcwd())
    except OSError:
        return ""


def _rel(path, base=None) -> str:
    """Relative to `base` (default: cwd) when inside it, else ~-abbreviated absolute."""
    base = core.real(base) if base else _cwd()
    path = str(path)
    if base and path == base:
        return "."
    if base and path.startswith(base.rstrip("/") + "/"):
        return path[len(base.rstrip("/")) + 1:]
    return core.tilde(path)


def _where(t) -> str:
    if is_temp(t):
        return "temp"
    return "dev" if _under(t, core.DEV) else "outside"


def repo_top(path):
    """Work-tree root of the repository that contains `path` (nearest existing directory), or None."""
    d = path
    while d and not (os.path.isdir(d) and not os.path.islink(d)):
        nd = os.path.dirname(d)
        if nd == d:
            break
        d = nd
    if d not in _TOP:
        rc, out, _ = _git(d, "rev-parse", "--show-toplevel", timeout=20)
        _TOP[d] = core.real(out.strip()) if rc == 0 and out.strip() else None
    return _TOP[d]


def _status(cwd, *pathspec):
    """git status as [(code, path relative to the repository root)]; code is M, A, D, R, C, U or ??."""
    out = _gitout(cwd, "status", "--porcelain=v1", "-z", "-uall", "--", *pathspec, timeout=60)
    toks, res, i = out.split("\0"), [], 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if len(t) < 4:
            continue
        xy, p = t[:2], t[3:]
        if xy[0] in "RC":
            i += 1  # the source path of a rename/copy follows
        res.append(("??" if xy == "??" else (xy[0] if xy[0] not in " ?" else xy[1]), p))
    return res


def _head(top) -> str:
    """HEAD, or the empty tree in a repository without commits."""
    if _git(top, "rev-parse", "-q", "--verify", "HEAD")[0] == 0:
        return "HEAD"
    return _gitout(top, "hash-object", "-t", "tree", "/dev/null").strip()


def _read(path) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _size(t, is_dir) -> int:
    if is_dir:
        kb = core.du_kb(t, timeout=60)
        return (kb or 0) * 1024
    try:
        return os.lstat(t).st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------- safety checks

def _never_reason(t):
    """System and home folders that no flag can delete or replace."""
    home = core.real(core.HOME)
    exact = {"/", "/Users", "/System", "/Library", "/Applications", "/usr", "/usr/local", "/bin", "/sbin", "/opt",
             "/opt/homebrew", "/Volumes", "/cores", "/dev", "/private", "/private/var", "/private/etc", "/etc",
             "/var", "/tmp", "/private/tmp", "/private/var/folders", home}
    exact |= {os.path.join(home, d) for d in ("Library", "Documents", "Desktop", "Downloads", "Pictures", "Movies",
                                              "Music", "Applications", "Public", ".Trash", ".ssh", ".gnupg", ".config",
                                              ".local", ".claude", ".codex", ".agents", ".aws", ".kube", ".docker")}
    if t in {core.real(p) for p in exact}:
        return f"{core.tilde(t)} is a system or home folder"
    for u in ("/System", "/usr/bin", "/usr/lib", "/usr/libexec", "/usr/sbin", "/usr/share", "/bin", "/sbin",
              "/private/etc", "/Library", "/Applications", "/opt/homebrew", os.path.join(home, ".ssh"),
              os.path.join(home, ".gnupg"), os.path.join(home, "Library", "Keychains")):
        if _under(t, u):
            where = "is" if t == core.real(u) else f"is inside {core.tilde(core.real(u))},"
            return f"{core.tilde(t)} {where} system files or credentials"
    return None


def _nested_repo(t, limit=4000):
    """First repository (a .git entry) strictly below `t`, skipping build output; None if none is found."""
    n = 0
    for dp, dns, fns in os.walk(t):
        if dp != t and (".git" in dns or ".git" in fns):
            return dp
        dns[:] = [d for d in dns if d not in ARTEFACTS and not (dp == t and d == ".git")]
        n += 1
        if n > limit:
            return None
    return None


ASK = "no override; ask the user"


def _structure_reason(t, is_dir, temp):
    """Workspace structure that is never deleted or moved. -> (reason, what to do instead) or None."""
    named = [(core.DEV, "the workspace root"), (core.HANDOFF, "the handoff repository"),
             (core.WT_ROOT, "the managed worktrees root"), (core.LOCAL, "the _local projects root"),
             (core.TMP_ROOT, "the devkit temp root"), (core.DEVKIT_HOME, "the devkit installation"),
             (core.STATE, "devkit's state folder"),
             # after `dev install` DEVKIT_HOME is the pinned copy when devkit runs from it: name the others apart
             (core.source_home(), "the devkit checkout"), (core.runtime_home(), "the pinned devkit copy the hooks run"),
             (core.pin_root(), "devkit's pinned releases")]
    for root, what in named:
        if t == core.real(root):
            return what, ASK
    if t in core.temp_roots():
        return "a temp root itself", "delete what is inside it instead"
    if temp:
        return None  # scratch area: throwaway clones and repositories may go
    if os.path.basename(t) == ".git":
        return "a .git directory: it is the repository's history", ASK
    if "/.git/" in t and not (t.endswith(".lock") and not is_dir):  # a stale *.lock may go (in-use check applies)
        return "inside .git: repository internals", "use git itself (git gc, git worktree prune, git stash drop …)"
    gitlink = os.path.join(t, ".git")
    if is_dir and os.path.isdir(gitlink):
        return "a repository checkout: deleting it can lose unpushed work and stashes", ASK
    if is_dir and os.path.isfile(gitlink):
        if "/worktrees/" in _read(gitlink):
            return "a git worktree", f"use `dev wt rm {core.tilde(t)}` (it checks for unpushed work)"
        return "a submodule checkout", "use `git submodule deinit` and `git rm` in the parent repository"
    dev = core.real(core.DEV)
    if is_dir and os.path.dirname(t) == dev:
        return "a top-level workspace folder (owner or host)", ASK
    if is_dir and os.path.dirname(t) == core.real(core.LOCAL):
        return "a _local project folder", ASK
    ho = core.real(core.HANDOFF)
    if _under(t, ho) and t != ho:
        rel = t[len(ho) + 1:]
        if rel in HANDOFF_DIRS:
            return "a managed handoff folder", ASK
        if rel.split("/")[0] in HANDOFF_DEVKIT_ONLY or rel in guard.GENERATED:
            return "managed by devkit", "use `dev task …`, `dev handoff --log …` or `dev sync`"
    if is_dir and _under(t, dev):
        nested = _nested_repo(t)
        if nested:
            return f"it contains the repository {_rel(nested)}", "delete what is inside instead, or ask the user"
    return None


def open_by(path, is_dir, timeout=10):
    """Processes with files open at or below `path`. -> ([(command, pid)], problem or None)."""
    rc, out, err = core.sh([LSOF, "-w", "-Fpc"] + (["+D", path] if is_dir else [path]), timeout=timeout)
    if rc == 127 and not out:
        return [], ("lsof timed out" if "timed out" in err.lower() else (err or "lsof failed"))
    holders, pid = {}, None
    for ln in out.splitlines():
        if ln[:1] == "p":
            pid = int(ln[1:]) if ln[1:].isdigit() else None
        elif ln[:1] == "c" and pid and pid != os.getpid() and not ln[1:].startswith(INDEXERS):
            holders[pid] = ln[1:]
    return sorted((c, p) for p, c in holders.items()), None


def _in_use(t, is_dir):
    """-> (reason, overridable) or None."""
    cwd = _cwd()
    if cwd and _under(cwd, t):
        return "your working directory is inside it (cd out first)", False
    active = is_dir and core.is_active(t)
    holders, problem = open_by(t, is_dir)
    if holders:
        names = ", ".join(f"{c} (pid {p})" for c, p in holders[:3])
        return f"in use by {names}" + (f" +{len(holders) - 3} more" if len(holders) > 3 else ""), True
    if active:
        return "a process is working inside it", True
    if problem:
        return f"could not check whether it is in use ({problem})", True
    return None


def _sensitive_in(t, is_dir, limit=3000):
    """Names of local-only files (.env, keys, databases …) at or below `t`."""
    if not is_dir:
        return [os.path.basename(t)] if SENSITIVE_RE.match(os.path.basename(t)) else []
    found, n = [], 0
    for dp, dns, fns in os.walk(t):
        dns[:] = [d for d in dns if d not in ARTEFACTS]
        for f in fns:
            n += 1
            if SENSITIVE_RE.match(f):
                found.append(os.path.relpath(os.path.join(dp, f), t))
            if n > limit or len(found) >= 5:
                return found
    return found


def _count_lines(path) -> str:
    try:
        if os.path.getsize(path) > 1024 * 1024:
            return _hb(os.path.getsize(path))
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return "?"
    if b"\0" in data[:8192]:
        return _hb(len(data))
    return _plural(data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0), "line")


def _numstat(top, rels):
    """{path: '+a -d'} of the working tree against HEAD (staged and unstaged together)."""
    if not rels:
        return {}
    out = _gitout(top, "diff", "--numstat", "--no-renames", "-z", _head(top), "--", *rels[:200], timeout=60)
    res = {}
    for t in out.split("\0"):
        parts = t.split("\t")
        if len(parts) == 3:
            res[parts[2]] = "binary" if parts[0] == "-" else f"+{parts[0]} -{parts[1]}"
    return res


def _loss_lines(top, changed, untracked, sens, cap=6):
    ns = _numstat(top, changed)
    lines = [f"{_rel(os.path.join(top, p))} | {ns.get(p, 'changed')}" for p in changed]
    lines += [f"{_rel(os.path.join(top, p))} | untracked, {_count_lines(os.path.join(top, p))}" for p in untracked]
    lines += [f"{_rel(os.path.join(top, p))} | gitignored local file" for p in sens]
    return lines[:cap] + ([f"+{len(lines) - cap} more"] if len(lines) > cap else [])


def _git_view(t, is_dir, top):
    """What deleting `t` would lose, as git sees it."""
    rel = os.path.relpath(t, top)
    v = {"kind": "", "label": "", "needs": {}, "detail": [], "git_rm": False}
    if _git(top, "check-ignore", "-q", "--", rel)[0] == 0:
        parts = rel.split("/")
        if any(p in ARTEFACTS for p in parts) or (
                parts[-1] == "vendor" and os.path.exists(os.path.join(os.path.dirname(t), "composer.json"))):
            v.update(kind="artefact", label="gitignored build output")
            return v
        sens = _sensitive_in(t, is_dir)
        if sens:
            v.update(kind="local", label="gitignored local file")
            v["needs"]["--discard"] = ("gitignored local files git cannot restore (" + ", ".join(sens[:3]) + ")"
                                       if is_dir else "a gitignored local file git cannot restore")
        else:
            v.update(kind="ignored", label="gitignored")
        return v
    tracked = [x for x in _gitout(top, "ls-files", "-z", "--", rel, timeout=60).split("\0") if x]
    changes = _status(top, rel)
    changed = [p for c, p in changes if c != "??"]
    untracked = [p for c, p in changes if c == "??"]
    sens = []
    if is_dir:
        ign = _gitout(top, "ls-files", "-oi", "--exclude-standard", "--directory", "-z", "--", rel, timeout=60)
        sens = [x.rstrip("/") for x in ign.split("\0") if x and SENSITIVE_RE.match(os.path.basename(x.rstrip("/")))]
    lost = []
    if is_dir:
        if tracked:
            v["needs"]["--tracked"] = f"{_plural(len(tracked), 'tracked file')} (the deletion is staged, like git rm)"
        if changed:
            lost.append(_plural(len(changed), "uncommitted change"))
        if untracked:
            lost.append(_plural(len(untracked), "untracked file"))
        if sens:
            lost.append(_plural(len(sens), "gitignored local file"))
        v["detail"] = _loss_lines(top, changed, untracked, sens)
    else:
        if tracked:
            v["needs"]["--tracked"] = "tracked by git (the deletion is staged, like git rm)"
        if changed:
            lost.append(f"uncommitted changes ({_numstat(top, [rel]).get(rel, 'changed')})")
        if untracked:
            lost.append(f"untracked, it exists only on disk ({_count_lines(t)})")
    if lost:
        v["needs"]["--discard"] = ", ".join(lost) + " would be lost"
    v["git_rm"] = bool(tracked)
    if tracked:
        v.update(kind="tracked", label="tracked" + (", with uncommitted changes" if changed else ""))
    elif untracked:
        v.update(kind="untracked", label="untracked")
    else:
        v.update(kind="empty", label="no tracked or untracked content")
    return v


# --------------------------------------------------------------------------- the Trash and the manifest

def _parse_trash(out: str, paths) -> dict:
    """`trash -v` prints `# Moved "SRC" to "DEST"` per item -> {src: dest}."""
    moved = {}
    for ln in out.splitlines():
        for p in paths:
            pre = f'# Moved "{p}" to "'
            if ln.startswith(pre) and ln.endswith('"'):
                moved[p] = ln[len(pre):-1]
    return moved


def trash_paths(paths) -> dict:
    """Move `paths` to the macOS Trash. -> {path: (where it went or None, error or None)}.

    The only function that touches the Trash; tests replace it so they never fill the real one."""
    paths = list(paths)
    if os.access(TRASH_BIN, os.X_OK):
        rc, out, err = core.sh([TRASH_BIN, "-v", *paths], timeout=900)
        moved = _parse_trash(out, paths)
        res = {}
        for p in paths:
            if p in moved:
                res[p] = (moved[p], None)
            elif not os.path.lexists(p):
                res[p] = (None, None)  # gone, but the Trash did not say where
            else:
                m = re.search(r'Code=-?\d+ "([^"]+)"', err or "")
                res[p] = (None, m.group(1) if m else (err.splitlines()[-1] if err else f"trash exited {rc}"))
        return res
    # No /usr/bin/trash (older macOS): devkit's own bin; `dev rm --undo` restores from it the same way.
    box = core.STATE / "trash" / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    res = {}
    for i, p in enumerate(paths):
        dest = box / f"{i}-{os.path.basename(p)}"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(p, str(dest))
            res[p] = (str(dest), None)
        except OSError as e:
            res[p] = (None, str(e))
    return res


def _manifest() -> Path:
    return core.STATE / "trash.jsonl"


def _record(entries):
    f = _manifest()
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n")


def _history() -> list:
    """`dev rm` runs, oldest first: [{batch, ts, cwd, op, items, restored}]."""
    batches, order, restored = {}, [], set()
    for ln in _read(_manifest()).splitlines():
        try:
            e = json.loads(ln)
        except ValueError:
            continue
        if e.get("restored"):
            restored.add(e["restored"])
            continue
        b = e.get("batch")
        if not b:
            continue
        if b not in batches:
            batches[b] = {"batch": b, "ts": e.get("ts"), "cwd": e.get("cwd") or "", "op": e.get("op", "rm"), "items": []}
            order.append(b)
        batches[b]["items"].append(e)
    return [dict(batches[b], restored=b in restored) for b in order]


def _trash_and_record(items, op, why=""):
    """Trash planned items and append them to the manifest. -> (ok items, [(item, error)])."""
    batch = f"{int(time.time() * 1000):x}-{os.getpid()}"
    res = trash_paths([i["path"] for i in items])
    ok, bad, recs, cwd, now = [], [], [], _cwd(), round(time.time(), 3)
    for i in items:
        dest, e = res.get(i["path"], (None, "not processed"))
        if e:
            bad.append((i, e))
            continue
        ok.append(i)
        recs.append({"ts": now, "batch": batch, "op": op, "cwd": cwd, "path": i["path"], "trashed_to": dest,
                     "bytes": i.get("bytes", 0), "dir": bool(i.get("is_dir")), "kind": i.get("kind", ""),
                     "repo": i.get("top"), "git_rm": bool(i.get("git_rm")),
                     "reason": (i.get("label") or "") + (f": {why}" if why else "")})
    if recs:
        _record(recs)
    return ok, bad


# --------------------------------------------------------------------------- dev rm

def _plan_rm(raw, a):
    t = _target(raw)
    it = {"raw": raw, "path": t, "show": _rel(t), "is_dir": False, "refuse": None, "fix": "", "detail": [],
          "label": "", "kind": "", "top": None, "git_rm": False, "missing": False, "bytes": 0}
    r = _never_reason(t)  # protected folders are refused whether or not they exist on this machine
    if r:
        it.update(refuse=r, fix=ASK)
        return it
    if not os.path.lexists(t):
        it["missing"] = True
        if not a.missing_ok:
            it.update(refuse="no such file or directory", fix="check the path (`dev find <name>`)")
        return it
    is_dir = os.path.isdir(t) and not os.path.islink(t)
    it["is_dir"] = is_dir
    if is_dir:
        it["show"] = it["show"].rstrip("/") + "/"
    where = _where(t)
    r = _structure_reason(t, is_dir, where == "temp")
    if r:
        it.update(refuse=r[0], fix=r[1])
        return it
    needs = {}
    if is_dir:
        needs["-r"] = "a directory"
    if where == "temp":
        it.update(kind="temp", label="temp")
    elif "/.git/" in t:  # only *.lock files get here (_structure_reason): stale locks of a crashed git
        it.update(kind="git-lock", label="git lock file (no git process holds it)")
    else:
        top = repo_top(t)
        it["top"] = top
        if top:
            v = _git_view(t, is_dir, top)
            it.update(kind=v["kind"], label=v["label"], detail=v["detail"], git_rm=v["git_rm"])
            needs.update(v["needs"])
        elif where == "dev":
            it.update(kind="unversioned", label="not under git")
            needs["--discard"] = "not under git: only the Trash will keep it"
        else:
            it.update(kind="outside", label="outside the workspace")
        if where == "outside":
            needs["--outside"] = f"outside {core.tilde(core.real(core.DEV))}, the scratchpad and temp roots"
    u = _in_use(t, is_dir)
    if u and not u[1]:
        it.update(refuse=u[0], fix="no override")
        return it
    if u:
        needs["--force-in-use"] = u[0]
    missing = [f for f in FLAG_ORDER if f in needs and not getattr(a, FLAG_ATTR[f])]
    if missing:
        it.update(refuse="; ".join(needs[f] for f in missing), fix="retry with " + " ".join(missing))
    return it


def _print_refusals(cmd, refused, total, verb):
    ui.err(f"dev {cmd} refused {len(refused)} of {_plural(total, 'path')}; nothing was {verb}")
    for i in refused:
        print(f"  {i['show']}: {i['refuse']} → {i['fix']}", file=sys.stderr)
        for d in i.get("detail") or []:
            print(f"      {d}", file=sys.stderr)


def cmd_rm(a):
    """Safe delete: to the Trash, with a manifest; refuses work that would be lost."""
    _reset()
    if a.list:
        return _rm_list()
    if a.undo is not None:
        return _rm_undo(a.undo)
    if not a.paths:
        ui.err("dev rm: nothing to delete — dev rm <paths…> · dev rm --undo [N] · dev rm --list")
        return 2
    items, seen = [], set()
    for raw in a.paths:
        t = _target(raw)
        if t not in seen:
            seen.add(t)
            items.append(_plan_rm(raw, a))
    # A path inside a directory of the same run goes with that directory.
    dirs = [i["path"] for i in items if i["is_dir"] and not i["refuse"]]
    items = [i for i in items if not any(i["path"] != d and _under(i["path"], d) for d in dirs)]
    refused = [i for i in items if i["refuse"]]
    if refused:
        _print_refusals("rm", refused, len(items), "deleted")
        return 1
    todo = [i for i in items if not i["missing"]]
    if not todo:
        print("nothing to delete")
        return 0
    for i in todo:
        i["bytes"] = _size(i["path"], i["is_dir"])
    total = sum(i["bytes"] for i in todo)
    if a.dry_run:
        print(f"would trash {_plural(len(todo), 'item')} ({_hb(total)}) · nothing changed")
        for i in todo:
            print(f"  {i['show']}  {i['label']}{' → git rm' if i['git_rm'] else ''} · {_hb(i['bytes'])}")
        return 0
    ok, bad = _trash_and_record(todo, "rm", a.why)
    by_repo = {}
    for i in ok:
        if i["git_rm"] and i["top"]:
            by_repo.setdefault(i["top"], []).append(os.path.relpath(i["path"], i["top"]))
    for top, rels in by_repo.items():  # keep the index right: the deletion is staged, as with `git rm`
        _git(top, "rm", "-r", "-q", "--cached", "-f", "--ignore-unmatch", "--", *rels)
    staged = sum(len(v) for v in by_repo.values())
    if ok:
        what = ok[0]["show"] if len(ok) == 1 else _plural(len(ok), "item")
        print(f"trashed {what} ({_hb(sum(i['bytes'] for i in ok))})"
              + (f" · {_plural(staged, 'deletion')} staged in git" if staged else "") + " · undo: dev rm --undo")
    for i, e in bad:
        ui.err(f"could not trash {i['show']}: {e}")
    return 1 if bad else 0


def _rm_undo(n) -> int:
    open_ = [b for b in _history() if not b["restored"]]
    if not open_:
        print("nothing to undo (dev rm --list shows recent deletions)")
        return 1
    todo = list(reversed(open_[-max(1, n):]))  # newest first
    restored, size, problems = 0, 0, []
    for b in todo:
        for e in b["items"]:
            src, dst = e.get("trashed_to"), e["path"]
            if not src or not os.path.lexists(src):
                problems.append(f"{core.tilde(dst)}: no longer in the Trash")
                continue
            if os.path.lexists(dst):
                problems.append(f"{core.tilde(dst)}: exists, not overwritten (the trashed copy is {core.tilde(src)})")
                continue
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)
            except OSError as ex:
                problems.append(f"{core.tilde(dst)}: {ex}")
                continue
            restored += 1
            size += e.get("bytes") or 0
            if e.get("git_rm") and e.get("repo"):
                _git(e["repo"], "reset", "-q", "--", os.path.relpath(dst, e["repo"]))
        _record([{"restored": b["batch"], "ts": round(time.time(), 3)}])
    print(f"restored {_plural(restored, 'item')} ({_hb(size)}) from {_plural(len(todo), 'dev rm run')}")
    for p in problems:
        ui.warn(p)
    return 0 if restored and not problems else 1


def _rm_list(limit=10) -> int:
    hist = _history()
    if not hist:
        print("no deletions recorded")
        return 0
    print("recent deletions, newest first (dev rm --undo N restores the newest N not yet restored)")
    for b in reversed(hist[-limit:]):
        names = [_rel(e["path"], b["cwd"] or None) + ("/" if e.get("dir") else "") for e in b["items"]]
        shown = ", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
        size = sum(e.get("bytes") or 0 for e in b["items"])
        tag = " · restored" if b["restored"] else (f" · {b['op']}" if b["op"] != "rm" else "")
        print(f"  {core.ago(b['ts'])} · {_plural(len(names), 'item')} · {_hb(size)} · {shown} · "
              f"in {core.tilde(b['cwd'])}{tag}")
    if len(hist) > limit:
        print(f"  +{len(hist) - limit} older — {core.tilde(_manifest())}")
    return 0


# --------------------------------------------------------------------------- dev mv / dev cp

def _layout_reason(final, src, is_dir, op):
    """The guard's workspace-layout rules, applied to the destination of a copy or move."""
    if is_temp(final) or not _under(final, core.DEV):
        return None
    v = guard.layout_violation(Path(final), creating_dir=is_dir)
    if v:
        return v
    name = os.path.basename(final)
    if guard.BACKUP_RE.search(name) and not name.startswith("."):
        return (f"`{name}` is a backup-style name; git already keeps history (edit in place, or put throwaway "
                f"copies in a temp dir)")
    if op == "cp" and is_dir and os.path.lexists(os.path.join(src, ".git")):
        return ("copying a repository makes a sibling copy; use `dev wt new <branch>` for a parallel checkout, "
                "or copy into a temp dir")
    m = SIBLING_RE.match(name)
    if is_dir and m and os.path.lexists(os.path.join(os.path.dirname(final), m.group("base"), ".git")):
        return f"`{name}` looks like a sibling copy of the repository `{m.group('base')}`; use `dev wt new <branch>`"
    return None


def _walk_files(root, skip=()):
    """Files below `root` (absolute), not descending into `skip` dirs."""
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if os.path.join(dp, d) not in skip]
        for f in fns:
            yield os.path.join(dp, f)
        for d in dns:
            if os.path.islink(os.path.join(dp, d)):
                yield os.path.join(dp, d)


def _heavy_in(src):
    """[(path, bytes)] of .git, node_modules and .venv dirs at or below `src`."""
    found = []
    if os.path.basename(src) in HEAVY:
        found.append(src)
    else:
        for dp, dns, _ in os.walk(src):
            hit = [d for d in dns if d in HEAVY]
            found += [os.path.join(dp, d) for d in hit]
            dns[:] = [d for d in dns if d not in HEAVY]
    return [(p, _size(p, True) if k < 5 else None) for k, p in enumerate(found)]


def _describe(p) -> str:
    try:
        st = os.lstat(p)
    except OSError:
        return _rel(p)
    if stat.S_ISDIR(st.st_mode):
        n = sum(1 for _ in _walk_files(p))
        return f"{_rel(p)}/ ({_plural(n, 'file')})"
    return f"{_rel(p)} ({_hb(st.st_size)}, changed {core.ago(st.st_mtime)})"


def _plan_cp_mv(op, src, final, a):
    it = {"src": src, "dst": final, "path": src, "show": f"{_rel(src)} → {_rel(final)}", "refuse": None, "fix": "",
          "is_dir": False, "overwrite": [], "git_mv": None, "skip": [], "files": 0, "bytes": 0}
    if not os.path.lexists(src):
        it.update(refuse="no such file or directory", fix="check the path (`dev find <name>`)")
        return it
    is_dir = os.path.isdir(src) and not os.path.islink(src)
    it["is_dir"] = is_dir
    if op == "mv":
        r = _never_reason(src)
        if r:
            it.update(refuse=r, fix=ASK)
            return it
        s = _structure_reason(src, is_dir, is_temp(src))
        if s:
            fix = "use `dev adopt <path>` to move a repository into the layout" if "repository" in s[0] else s[1]
            it.update(refuse=s[0], fix=fix)
            return it
    if final == src or (is_dir and _under(final, src)):
        it.update(refuse="the destination is the source or inside it", fix="pick another destination")
        return it
    parent = os.path.dirname(final)
    if not os.path.isdir(parent):
        it.update(refuse=f"the destination folder {_rel(parent)} does not exist",
                  fix=f"create it first (mkdir -p {_rel(parent)})")
        return it
    r = _never_reason(final)
    if r:
        it.update(refuse=r, fix=ASK)
        return it
    r = _layout_reason(final, src, is_dir, op)
    if r:
        it.update(refuse=r, fix="no override")
        return it
    if op == "cp" and is_dir and not a.recursive:
        it.update(refuse="a directory", fix="retry with -r")
        return it
    if os.path.lexists(final):
        final_dir = os.path.isdir(final) and not os.path.islink(final)
        if op == "cp" and is_dir and final_dir:  # merge: only files present on both sides are replaced
            over = [os.path.join(final, os.path.relpath(f, src)) for f in _walk_files(src)]
            over = [p for p in over if os.path.lexists(p)]
        else:
            over = [final]
        if over and not a.force:
            shown = ", ".join(_describe(p) for p in over[:3]) + (f" +{len(over) - 3} more" if len(over) > 3 else "")
            it.update(refuse=f"would overwrite {shown}",
                      fix="retry with --force (the old version goes to the Trash)")
            return it
        it["overwrite"] = over
    if op == "cp":
        if is_dir:
            heavy = _heavy_in(src)
            if heavy and not (a.skip_heavy or a.include_heavy):
                desc = ", ".join(f"{_rel(p)}/ ({_hb(b) if b is not None else '?'})" for p, b in heavy[:4])
                more = f" +{len(heavy) - 4} more" if len(heavy) > 4 else ""
                it.update(refuse=f"contains {desc}{more}",
                          fix="retry with --skip-heavy (copy without them) or --include-heavy (copy them too)")
                return it
            it["skip"] = [p for p, _ in heavy] if a.skip_heavy else []
            files = list(_walk_files(src, set(it["skip"])))
            it["files"] = len(files)
            it["bytes"] = sum(os.lstat(f).st_size for f in files if os.path.lexists(f))
        else:
            it["files"], it["bytes"] = 1, os.lstat(src).st_size
        return it
    ws, wd = _where(src), _where(final)
    ts, td = repo_top(src), repo_top(parent)
    cross = None
    if ws == "dev" and wd == "outside":
        cross = "moves it out of the workspace"
    elif ws == "dev" and wd == "dev" and ts != td:
        cross = (f"moves it across projects ({core.tilde(ts) if ts else 'no repository'} → "
                 f"{core.tilde(td) if td else 'no repository'})")
    if cross and not a.cross:
        it.update(refuse=cross, fix="retry with --cross")
        return it
    if ts and ts == td and _gitout(ts, "ls-files", "-z", "--", os.path.relpath(src, ts)).strip("\0"):
        it["git_mv"] = ts
    return it


def _replace(items, op):
    """Trash what a --force copy or move replaces. -> error text or None."""
    olds = [{"path": p, "show": _rel(p), "is_dir": os.path.isdir(p) and not os.path.islink(p),
             "bytes": _size(p, os.path.isdir(p) and not os.path.islink(p)), "kind": "overwritten",
             "label": f"replaced by dev {op}", "top": repo_top(p)} for it in items for p in it["overwrite"]]
    if not olds:
        return None
    _, bad = _trash_and_record(olds, op)
    return "; ".join(f"{i['show']}: {e}" for i, e in bad) or None


def _copy_move(a, op) -> int:
    _reset()
    verb = "moved" if op == "mv" else "copied"
    if len(a.paths) < 2:
        ui.err(f"dev {op}: need a source and a destination (dev {op} SRC… DEST)")
        return 2
    raw_dest = a.paths[-1]
    dest = _target(raw_dest)
    into = os.path.isdir(dest)
    if len(a.paths) > 2 and not into:
        ui.err(f"dev {op}: {_rel(dest)} is not a directory (several sources need a destination folder)")
        return 2
    if raw_dest.endswith("/") and not into:
        ui.err(f"dev {op}: the destination folder {_rel(dest)} does not exist (mkdir -p {_rel(dest)} first)")
        return 2
    items = []
    for s in a.paths[:-1]:
        src = _target(s)
        items.append(_plan_cp_mv(op, src, os.path.join(dest, os.path.basename(src)) if into else dest, a))
    refused = [i for i in items if i["refuse"]]
    if refused:
        _print_refusals(op, refused, len(items), verb)
        return 1
    if a.dry_run:
        print(f"would {'move' if op == 'mv' else 'copy'} {_plural(len(items), 'item')} · nothing changed")
        for i in items:
            bits = (["git mv"] if i["git_mv"] else []) + ([f"replaces {len(i['overwrite'])}"] if i["overwrite"] else [])
            if op == "cp":
                bits.insert(0, f"{_plural(i['files'], 'file')}, {_hb(i['bytes'])}")
                bits += [f"skips {_rel(p)}/" for p in i["skip"]]
            print(f"  {i['show']}" + (f"  ({', '.join(bits)})" if bits else ""))
        return 0
    problem = _replace(items, op)
    if problem:
        ui.err(f"dev {op}: could not move the files it would replace to the Trash ({problem}); nothing was {verb}")
        return 1
    done, failed, git_mv = [], [], 0
    for i in items:
        src, dst = i["src"], i["dst"]
        try:
            if op == "mv" and i["git_mv"]:
                top = i["git_mv"]
                if i["overwrite"]:
                    _git(top, "rm", "-q", "--cached", "-f", "--ignore-unmatch", "--", os.path.relpath(dst, top))
                rc, _, err = _git(top, "mv", "-f", "--", os.path.relpath(src, top), os.path.relpath(dst, top))
                if rc:
                    raise OSError(err or "git mv failed")
                git_mv += 1
            elif op == "mv":
                shutil.move(src, dst)
            elif i["is_dir"]:
                skip = set(i["skip"])
                ignore = (lambda d, names: [n for n in names if os.path.join(d, n) in skip]) if skip else None
                shutil.copytree(src, dst, symlinks=True, ignore=ignore, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
            done.append(i)
        except (OSError, shutil.Error) as e:
            failed.append((i, e))
    if done:
        what = _rel(done[0]["src"]) + ("/" if done[0]["is_dir"] else "") if len(done) == 1 else _plural(len(done), "item")
        to = _rel(dest) + ("/" if into else "")
        bits = []
        if op == "cp":
            bits.append(f"{_plural(sum(i['files'] for i in done), 'file')}, {_hb(sum(i['bytes'] for i in done))}")
            skipped = [p for i in done for p in i["skip"]]
            if skipped:
                bits.append("skipped " + ", ".join(_rel(p) + "/" for p in skipped[:4])
                            + (f" +{len(skipped) - 4}" if len(skipped) > 4 else ""))
        elif git_mv:
            bits.append("git mv" if git_mv == len(done) else f"git mv: {git_mv}")
        replaced = sum(len(i["overwrite"]) for i in done)
        if replaced:
            bits.append(f"replaced {replaced}, old version in the Trash: dev rm --list")
        print(f"{verb} {what} → {to}" + (f" ({'; '.join(bits)})" if bits else ""))
    for i, e in failed:
        ui.err(f"could not {op} {i['show']}: {e}")
    return 1 if failed else 0


def cmd_mv(a):
    """Move or rename without clobbering; git mv for tracked files."""
    return _copy_move(a, "mv")


def cmd_cp(a):
    """Copy without clobbering; -r refuses .git, node_modules and .venv unless asked."""
    return _copy_move(a, "cp")


# --------------------------------------------------------------------------- listing (files, find, grep fallback)

def _listing(root, top, all_=False):
    """Entries below `root`, relative to it. -> (files, dirs, ignored, repos).

    In a repository: tracked + untracked-but-not-ignored files (git ls-files), with the ignored entries
    collapsed (`node_modules/`). Elsewhere, or with all_: a walk that skips .git (and build output
    unless all_)."""
    files, dirs, ignored, repos = set(), set(), [], set()
    if top and not all_:
        out = _gitout(root, "ls-files", "-co", "--exclude-standard", "-z", timeout=120)
        gone = set(_gitout(root, "ls-files", "-d", "-z", timeout=60).split("\0"))
        for p in out.split("\0"):
            if not p or p in gone:
                continue
            if p.endswith("/"):
                repos.add(p.rstrip("/"))  # an untracked nested repository
            else:
                files.add(p)
        ign = _gitout(root, "ls-files", "-oi", "--exclude-standard", "--directory", "-z", timeout=60)
        ignored = sorted(x for x in ign.split("\0") if x)
    else:
        for dp, dns, fns in os.walk(root):
            rd = os.path.relpath(dp, root)
            rd = "" if rd == "." else rd + "/"
            keep = []
            for d in sorted(dns):
                if d == ".git" or (not all_ and d in NOISE):
                    ignored.append(rd + d + "/")
                elif os.path.islink(os.path.join(dp, d)):
                    files.add(rd + d)
                else:
                    keep.append(d)
                    dirs.add(rd + d)
            dns[:] = keep
            for f in fns:
                if all_ or f not in NOISE:
                    files.add(rd + f)
    for f in list(files) + list(repos):
        parent = f.rpartition("/")[0]
        while parent and parent not in dirs:
            dirs.add(parent)
            parent = parent.rpartition("/")[0]
    return sorted(files), sorted(dirs | repos), ignored, repos


def _marks(root, top) -> dict:
    """{path relative to root: git status code}."""
    res = {}
    for code, p in _status(root, "."):
        full = os.path.join(top, p)
        if _under(full, root):
            res[os.path.relpath(full, root)] = code
    return res


class _Node:
    __slots__ = ("dirs", "files", "count", "size", "marks", "repo", "rows")

    def __init__(self):
        self.dirs, self.files, self.count, self.size, self.marks, self.repo, self.rows = {}, [], 0, 0, {}, False, None


def _build_tree(root, files, dirs, marks, repos):
    top = _Node()

    def chain(rel):
        n, out = top, [top]
        for part in (rel.split("/") if rel else []):
            n = n.dirs.setdefault(part, _Node())
            out.append(n)
        return out

    for d in dirs:
        chain(d)
    for r in repos:
        chain(r)[-1].repo = True
    for f in files:
        try:
            st = os.lstat(os.path.join(root, f))
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):  # a submodule (gitlink) or nested repository
            chain(f)[-1].repo = True
            continue
        parent, _, name = f.rpartition("/")
        mark = marks.get(f)
        nodes = chain(parent)
        nodes[-1].files.append((name, st.st_size, mark))
        for n in nodes:
            n.count += 1
            n.size += st.st_size
            if mark:
                n.marks[mark] = n.marks.get(mark, 0) + 1
    return top


def _rows(node):
    """Rows for a node's direct children: dirs first, then files; big same-extension groups folded."""
    if node.rows is not None:
        return node.rows
    rows = []
    for name in sorted(node.dirs, key=lambda s: (s.lower(), s)):
        ch = node.dirs[name]
        rows.append({"type": "dir", "name": name + "/", "files": ch.count, "bytes": ch.size,
                     "git": dict(sorted(ch.marks.items())), "repo": ch.repo, "_node": ch})
    files = sorted(node.files, key=lambda f: (f[0].lower(), f[0]))
    groups = {}
    if len(files) > GROUP_MIN_FILES:
        for f in files:
            ext = os.path.splitext(f[0])[1].lower()
            if ext and not f[2]:  # changed files always stay visible
                groups.setdefault(ext, []).append(f)
        groups = {e: g for e, g in groups.items() if len(g) >= GROUP_MIN_SAME}
    folded = {f[0] for g in groups.values() for f in g}
    rows += [{"type": "file", "name": f[0], "bytes": f[1], "git": f[2]} for f in files if f[0] not in folded]
    rows += [{"type": "group", "name": "*" + e, "count": len(groups[e]), "bytes": sum(x[1] for x in groups[e])}
             for e in sorted(groups)]
    node.rows = rows
    return rows


def _need(node, level, depth) -> int:
    rows = _rows(node)
    n = len(rows)
    if level < depth:
        n += sum(_need(r["_node"], level + 1, depth) for r in rows if r["type"] == "dir")
    return n


def _share(total, needs):
    """Split `total` lines between children: small ones get all they need, big ones share the rest."""
    alloc, left, k = [0] * len(needs), total, len(needs)
    for i in sorted(range(len(needs)), key=lambda j: needs[j]):
        give = min(needs[i], left // k) if k else 0
        alloc[i], left, k = give, left - give, k - 1
    return alloc


def _render(node, prefix, level, depth, budget, out):
    rows = _rows(node)
    if not rows or budget <= 0:
        return
    if len(rows) > budget:
        if budget < 2:
            return
        shown, hidden = rows[:budget - 1], len(rows) - (budget - 1)
    else:
        shown, hidden = rows, 0
    left = budget - len(shown) - (1 if hidden else 0)
    exp = [i for i, r in enumerate(shown) if r["type"] == "dir" and level < depth and _rows(r["_node"])]
    alloc = dict(zip(exp, _share(left, [_need(shown[i]["_node"], level + 1, depth) for i in exp])))
    for i, r in enumerate(shown):
        row = {k: v for k, v in r.items() if k != "_node"}
        row.update(level=level, path=prefix + r["name"])
        out.append(row)
        if i in alloc:
            _render(r["_node"], prefix + r["name"], level + 1, depth, alloc[i], out)
    if hidden:
        out.append({"type": "more", "level": level, "path": prefix, "count": hidden})


def _fmt_marks(g) -> str:
    return "[" + ", ".join(f"{k} {v}" for k, v in g.items()) + "]" if g else ""


def _fmt_row(r, root) -> str:
    ind = "  " * (r["level"] - 1)
    t = r["type"]
    if t == "dir":
        if not r["files"]:
            info = "repo" if r.get("repo") else "empty"
        else:
            info = _plural(r["files"], "file") + (f", {_hb(r['bytes'])}" if r["bytes"] >= BIG_DIR else "")
        g = _fmt_marks(r["git"])
        return f"{ind}{_paint(ui.bold, r['name'])}  ({info})" + (f"  {_paint(ui.yellow, g)}" if g else "")
    if t == "file":
        return (f"{ind}{r['name']}" + (f"  {_hb(r['bytes'])}" if r["bytes"] >= BIG_FILE else "")
                + (f"  {_paint(ui.yellow, r['git'])}" if r["git"] else ""))
    if t == "group":
        return f"{ind}{r['name']} ×{r['count']}  ({_hb(r['bytes'])})"
    where = _rel(os.path.join(root, r["path"])) if r["path"] else ""
    return (f"{ind}… +{r['count']} more — dev files {where}" if where
            else f"{ind}… +{r['count']} more — narrow with dev files <subdir>, or raise --max")


def cmd_files(a):
    """Compact, gitignore-aware tree."""
    _reset()
    root = _target(a.path)
    if not os.path.lexists(root):
        ui.err(f"dev files: {a.path}: no such file or directory")
        return 2
    if not os.path.isdir(root):
        st = os.lstat(root)
        print(f"{_rel(root)}  {_hb(st.st_size)} · changed {core.ago(st.st_mtime)}")
        return 0
    root = core.real(root)
    top = repo_top(root)
    all_ = bool(a.all or (top and root != top and _git(top, "check-ignore", "-q", "--", os.path.relpath(root, top))[0] == 0))
    files, dirs, ignored, repos = _listing(root, top, all_)
    marks = _marks(root, top) if top else {}
    tree = _build_tree(root, files, dirs, marks, repos)
    counts = {}
    for c in marks.values():
        counts[c] = counts.get(c, 0) + 1
    show = "./" if root == _cwd() else _rel(root) + "/"
    head = [f"{show}  {_plural(tree.count, 'file')}, {_hb(tree.size)}"
            + (f"  git: {', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}" if counts else "")]
    if ignored:
        word = "ignored" if top and not all_ else "skipped"
        head.append(f"{word}: " + " ".join(ignored[:8]) + (f" +{len(ignored) - 8} more" if len(ignored) > 8 else "")
                    + " (dev files --all)")
    rows = []
    budget = (a.max - len(head)) if a.max else 10 ** 9
    _render(tree, "", 1, max(1, a.depth), max(1, budget), rows)
    if a.json:
        print(json.dumps({"root": core.tilde(root), "repo": bool(top), "files": tree.count, "bytes": tree.size,
                          "git": counts, "ignored": ignored, "truncated": any(r["type"] == "more" for r in rows),
                          "entries": [{k: v for k, v in r.items() if k != "name"} for r in rows]},
                         ensure_ascii=False))
        return 0
    print("\n".join(head + [_fmt_row(r, root) for r in rows]))
    return 0


# --------------------------------------------------------------------------- dev find

def _matcher(pattern, glob=False, regex=False):
    """Smart-case name matcher: a glob when the pattern looks like one (`*.py`), else a regex search."""
    if not pattern:
        return None
    flags = 0 if any(c.isupper() for c in pattern) else re.I
    looks_glob = bool(set(pattern) & set("*?[")) and not (set(pattern) & set("\\^$+(){}|")) \
        and ".*" not in pattern and ".+" not in pattern
    if glob or (looks_glob and not regex):
        return re.compile(fnmatch.translate(pattern), flags).match
    return re.compile(pattern, flags).search


def _parse_size(s):
    m = re.match(r"^([+-]?)(\d+(?:\.\d+)?)\s*([bkmgt]?)(?:i?b)?$", s.strip(), re.I)
    if not m:
        raise ValueError(f"bad --size {s!r}: use +1M (at least) or -100k (at most)")
    mult = {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[m.group(3).lower()]
    return m.group(1) or "+", float(m.group(2)) * mult


def _parse_since(s) -> float:
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(s|m|min|h|d|w)$", s.strip(), re.I)
    if m:
        unit = {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2).lower()]
        return time.time() - float(m.group(1)) * unit
    try:
        return time.mktime(time.strptime(s.strip(), "%Y-%m-%d"))
    except ValueError:
        raise ValueError(f"bad --since {s!r}: use 30m, 2h, 2d, 1w or YYYY-MM-DD") from None


def _fd_candidates(root, a, top):
    args = ["fd", "--hidden", "--color", "never", "--absolute-path", "--print0", "--exclude", ".git"]  # no -L: links stay
    if a.all:
        args.append("--no-ignore")
    elif not top:
        for n in sorted(NOISE - {".git"}):
            args += ["--exclude", n]
    if a.type:
        args += ["--type", "file" if a.type == "f" else "directory"]
    rc, out, _ = _run(args + ["--", ".", root], timeout=120)
    if rc != 0:
        return None  # any fd problem: fall back to git ls-files / the walk
    res = []
    for p in out.split("\0"):
        p = p.rstrip("/")
        if p and p != root:
            res.append((os.path.relpath(p, root), os.path.isdir(p) and not os.path.islink(p)))
    return res


def cmd_find(a):
    """fd-like name search: gitignore-aware, filtered, capped; paths relative to cwd."""
    _reset()
    root = _target(a.path)
    if not os.path.isdir(root):
        ui.err(f"dev find: {a.path} is not a directory (usage: dev find [PATTERN] [DIR])")
        return 2
    root = core.real(root)
    try:
        match = _matcher(a.pattern, a.glob, a.regex)
        since = _parse_since(a.since) if a.since else None
        size = _parse_size(a.size) if a.size else None
    except (ValueError, re.error) as e:
        ui.err(f"dev find: {e}")
        return 2
    top = repo_top(root)
    if a.changed and not top:
        ui.err("dev find: --changed needs a git repository")
        return 2
    cands = _fd_candidates(root, a, top) if (USE_FD and core.have("fd")) else None
    if cands is None:
        files, dirs, _, _ = _listing(root, top, a.all)
        cands = [(f, False) for f in files] + [(d, True) for d in dirs]
    full = a.full_path or "/" in (a.pattern or "")
    exts = {e.lower().lstrip(".") for e in (a.ext or [])}
    changed = {p for p, c in _marks(root, top).items() if c != "D"} if a.changed else None
    res = []
    for rel, is_dir in cands:
        if (a.type == "f" and is_dir) or (a.type == "d" and not is_dir) or (a.changed and is_dir and a.type != "d"):
            continue  # --changed lists files; folders only when asked for with -t d
        name = rel.rpartition("/")[2]
        if exts and (is_dir or "." not in name or name.rpartition(".")[2].lower() not in exts):
            continue
        if changed is not None and not (rel in changed if not is_dir else any(c.startswith(rel + "/") for c in changed)):
            continue
        if match and not match(rel if full else name):
            continue
        st = None
        if since is not None or size is not None or a.json:
            try:
                st = os.lstat(os.path.join(root, rel))
            except OSError:
                continue
            if since is not None and st.st_mtime < since:
                continue
            if size is not None and (is_dir or (st.st_size < size[1] if size[0] == "+" else st.st_size > size[1])):
                continue
        res.append((rel, is_dir, st))
    res.sort(key=lambda r: r[0])
    shown = res[:a.max] if a.max else res
    more = len(res) - len(shown)
    if a.json:
        print(json.dumps({"root": core.tilde(root), "total": len(res), "more": more,
                          "matches": [{"path": _rel(os.path.join(root, r)) + ("/" if d else ""),
                                       "type": "d" if d else "f", "bytes": st.st_size if st else None,
                                       "mtime": int(st.st_mtime) if st else None} for r, d, st in shown]},
                         ensure_ascii=False))
        return 0 if res else 1
    if not res:
        print("no matches")
        return 1
    for rel, is_dir, _ in shown:
        print(_rel(os.path.join(root, rel)) + ("/" if is_dir else ""))
    if more:
        print(f"+{more} more — narrow with a longer pattern, -e EXT, -t f|d or a subdirectory (or --max N)")
    return 0


# --------------------------------------------------------------------------- dev grep

def _trim(text, span=None, width=LINE_WIDTH) -> str:
    """One line, stripped, at most `width` chars, keeping the match in view."""
    s = text.rstrip("\r\n")
    lead = len(s) - len(s.lstrip())
    s = s.strip()
    if len(s) <= width:
        return s
    st = max(0, span[0] - lead) if span else 0
    start = max(0, min(st - width // 4, len(s) - width))
    return ("…" if start else "") + s[start:start + width] + ("…" if start + width < len(s) else "")


def _jtext(v) -> str:
    if not isinstance(v, dict):
        return ""
    if "text" in v:
        return v["text"]
    try:
        return base64.b64decode(v.get("bytes", "")).decode("utf-8", "replace")
    except ValueError:
        return ""


def _rg(a, extra, targets, timeout=120):
    args = ["rg", "--no-config", "--color", "never", "--hidden", "-g", "!.git"]
    args.append("-i" if a.ignore_case else "-s" if a.case_sensitive else "-S")
    if a.fixed:
        args.append("-F")
    if a.word:
        args.append("-w")
    if a.all:
        args.append("--no-ignore")
    return _run(args + extra + ["-e", a.pattern, "--"] + list(targets), timeout=timeout)


def _rg_counts(a):
    filters = [x for g in (a.glob or []) for x in ("-g", g)] + [x for t in (a.type or []) for x in ("-t", t)]
    rc, out, err = _rg(a, ["--count", "--null", "-H"] + filters, a.paths or ["."])  # explicit: never read stdin
    counts = {}
    for ln in out.split("\n"):
        if "\0" in ln:
            p, _, n = ln.partition("\0")
            if n.strip().isdigit():
                counts[p[2:] if p.startswith("./") else p] = int(n)
    return counts, (err.splitlines()[0] if rc == 2 and err else None)


def _rg_events(a, picked):
    if not picked:
        return {}
    extra = ["--json", "-m", str(max(1, a.max_per_file))] + (["-C", str(a.context)] if a.context else [])
    _, out, _ = _rg(a, extra, picked)
    ev = {}
    for ln in out.splitlines():
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("type") not in ("match", "context"):
            continue
        d = o.get("data") or {}
        p, text, span = _jtext(d.get("path")), _jtext(d.get("lines")), None
        p = p[2:] if p.startswith("./") else p
        if o["type"] == "match" and d.get("submatches"):
            b, sm = text.encode("utf-8", "replace"), d["submatches"][0]
            span = (len(b[:sm["start"]].decode("utf-8", "ignore")), len(b[:sm["end"]].decode("utf-8", "ignore")))
        ev.setdefault(p, []).append((o["type"], d.get("line_number") or 0, text, span))
    return ev


def _glob_ok(path, globs) -> bool:
    inc = [g for g in globs if not g.startswith("!")]
    exc = [g[1:] for g in globs if g.startswith("!")]

    def hit(g):
        return fnmatch.fnmatch(path, g) if "/" in g else fnmatch.fnmatch(os.path.basename(path), g)
    return (not inc or any(hit(g) for g in inc)) and not any(hit(g) for g in exc)


def _py_grep(a):
    """Fallback without ripgrep: Python regex over the gitignore-aware file list."""
    smart = not a.case_sensitive and not any(c.isupper() for c in a.pattern)
    flags = re.I if a.ignore_case or smart else 0
    pat = re.escape(a.pattern) if a.fixed else a.pattern
    if a.word:
        pat = rf"\b(?:{pat})\b"
    try:
        rx = re.compile(pat, flags)
    except re.error as e:
        return {}, {}, f"bad regex: {e}"
    if a.type:
        ui.warn("dev grep: -t needs ripgrep (brew install ripgrep); ignored")
    files = []
    for t in a.paths or ["."]:
        full = _target(t)
        if os.path.isfile(full):
            files.append(t)
        elif os.path.isdir(full):
            fl = _listing(core.real(full), repo_top(full), a.all)[0]
            files += [f if t in (".", "./") else os.path.join(t, f) for f in fl]
    if a.glob:
        files = [f for f in files if _glob_ok(f, a.glob)]
    per, ctx = max(1, a.max_per_file), max(0, a.context)
    counts, events = {}, {}
    for f in sorted(set(files)):
        try:
            with open(f, "rb") as fh:
                data = fh.read(20 * 1024 * 1024)
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
        if not hits:
            continue
        counts[f] = len(hits)
        keep, shown = set(hits[:per]), {}
        for i in hits[:per]:
            for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                if j not in shown:
                    m = rx.search(lines[j]) if j in keep else None
                    shown[j] = ("match" if j in keep else "context", j + 1, lines[j], m.span() if m else None)
        events[f] = [shown[j] for j in sorted(shown)]
    return counts, events, None


def _grep_pick(files, counts, a):
    per, budget, picked = max(1, a.max_per_file), (a.max or 10 ** 9), []
    for p in files:
        if budget <= 0:
            break
        k = min(counts[p], per)
        picked.append(p)
        budget -= k * (1 + 2 * max(0, a.context)) + (1 if counts[p] > k else 0)
    return picked


def _grep_output(a, counts, events_for) -> int:
    """Shared rendering for rg, the Python fallback and ast-grep."""
    files = sorted(counts)
    total = sum(counts.values())
    cap = a.max or 10 ** 9
    if a.files or a.count:
        order = files if a.files else sorted(files, key=lambda p: (-counts[p], p))
        shown = order[:cap]
        if a.json:
            print(json.dumps({"total": total, "files": len(files), "more": len(order) - len(shown),
                              "matches": [{"path": p, "count": counts[p]} for p in shown]}, ensure_ascii=False))
            return 0
        for p in shown:
            print(p if a.files else f"{p}: {counts[p]}")
        if len(order) > len(shown):
            print(f"+{len(order) - len(shown)} more files — narrow with a path, -g '*.ext' or -t TYPE")
        if a.count:
            print(f"total: {_plural(total, 'matching line')} in {_plural(len(files), 'file')}")
        return 0
    picked = _grep_pick(files, counts, a)
    events = events_for(picked)
    out, done, printed, cut = [], 0, 0, False
    for p in picked:
        evs = events.get(p, [])
        last, nmatch = None, 0
        for kind, ln, text, span in evs:
            if printed >= cap:
                cut = True
                break
            if a.context and last is not None and ln > last + 1:
                out.append(("sep", p, 0, "--"))
                printed += 1
            out.append((kind, p, ln, _trim(text, span)))
            printed += 1
            last = ln
            nmatch += kind == "match"
        if cut:
            break
        done += 1
        if counts[p] > nmatch:
            out.append(("more", p, counts[p] - nmatch, ""))
            printed += 1
    rest = files[done:]
    rest_lines = sum(counts[p] for p in rest)
    if cut and picked[done:done + 1]:  # matches of the file that was cut off are partly shown
        p = picked[done]
        rest_lines -= sum(1 for k, q, _, _ in out if q == p and k == "match")
    if a.json:
        print(json.dumps({"total": total, "files": len(files), "more": rest_lines,
                          "matches": [{"path": p, "line": ln, "text": t} for k, p, ln, t in out if k == "match"]},
                         ensure_ascii=False))
        return 0
    for kind, p, ln, text in out:
        if kind == "sep":
            print("--")
        elif kind == "more":
            print(f"{p}: +{ln} more")
        else:
            sep = ":" if kind == "match" else "-"
            print(f"{_paint(ui.cyan, p)}{sep}{ln}{sep} {text}")
    if rest:
        print(f"+{rest_lines} more matching lines in {_plural(len(rest), 'file')} — narrow with a path, "
              f"-g '*.ext', -t TYPE, or list files with -l")
    return 0


def _grep_ast(a) -> int:
    exe = "ast-grep" if core.have("ast-grep") else None
    if not exe:
        ui.err("dev grep --ast needs ast-grep: brew install ast-grep (or drop --ast)")
        return 2
    args = [exe, "run", "--pattern", a.pattern, "--json=stream"] + (["--lang", a.lang] if a.lang else [])
    rc, out, err = _run(args + (a.paths or ["."]), timeout=180)
    counts, events, per = {}, {}, max(1, a.max_per_file)
    for ln in out.splitlines():
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        f = m.get("file") or ""
        f = f[2:] if f.startswith("./") else f
        line = (((m.get("range") or {}).get("start") or {}).get("line") or 0) + 1
        text = ((m.get("lines") or m.get("text") or "").splitlines() or [""])[0]
        counts[f] = counts.get(f, 0) + 1
        if len(events.get(f, [])) < per:
            events.setdefault(f, []).append(("match", line, text, None))
    if not counts:
        if rc not in (0, 1) and err:
            ui.err(f"dev grep --ast: {err.splitlines()[0]}")
            return 2
        print("no matches")
        return 1
    for f in events:
        events[f].sort(key=lambda e: e[1])
    return _grep_output(a, counts, lambda picked: events)


def cmd_grep(a):
    """ripgrep with compact, capped output: file:line: text."""
    _reset()
    if a.ast:
        return _grep_ast(a)
    if core.have("rg"):
        counts, err = _rg_counts(a)
        events_for = lambda picked: _rg_events(a, picked)  # noqa: E731
    else:
        counts, events, err = _py_grep(a)
        events_for = lambda picked: events  # noqa: E731
    if not counts:
        if err:
            ui.err(f"dev grep: {err}")
            return 2
        print("no matches")
        return 1
    return _grep_output(a, counts, events_for)


# --------------------------------------------------------------------------- dev diff

GENERATED = [
    (re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|poetry\.lock|"
                r"uv\.lock|Pipfile\.lock|Cargo\.lock|composer\.lock|Gemfile\.lock|go\.sum|mix\.lock|pubspec\.lock|"
                r"Podfile\.lock|packages\.lock\.json|gradle\.lockfile|flake\.lock|\.terraform\.lock\.hcl)$"), "lock file"),
    (re.compile(r"\.min\.(js|mjs|css)$|\.(js|css)\.map$"), "minified"),
    (re.compile(r"\.snap$|(^|/)__snapshots__/"), "snapshot"),
    (re.compile(r"(^|/)(dist|build|out|target|coverage|node_modules|vendor)/"), "build output"),
    (re.compile(r"(\.pb\.go|_pb2\.py|\.g\.dart|\.freezed\.dart|\.designer\.cs)$|\.generated\."), "generated"),
]


def _generated(path):
    return next((why for rx, why in GENERATED if rx.search(path)), None)


def _parse_numstat(out):
    toks, res, i = out.split("\0"), [], 0
    while i < len(toks):
        parts = toks[i].split("\t")
        i += 1
        if len(parts) != 3:
            continue
        add, dele, path, old = parts[0], parts[1], parts[2], None
        if path == "" and i + 1 < len(toks):  # rename: old and new path follow
            old, path = toks[i], toks[i + 1]
            i += 2
        binary = add == "-"
        res.append({"path": path, "old": old, "add": 0 if binary else int(add), "del": 0 if binary else int(dele),
                    "binary": binary})
    return res


def _parse_name_status(out):
    toks, res, i = out.split("\0"), {}, 0
    while i < len(toks):
        st = toks[i]
        i += 1
        if not st:
            continue
        if st[0] in "RC" and i + 1 < len(toks):
            res[toks[i + 1]] = st[0]
            i += 2
        elif i < len(toks):
            res[toks[i]] = st[0]
            i += 1
    return res


def _parse_patch(text):
    blocks, cur = [], None
    for ln in text.split("\n"):
        if ln.startswith(("diff --git ", "diff --cc ", "diff --combined ")):
            cur = {"path": None, "old": None, "body": [], "head": ln}
            blocks.append(cur)
            continue
        if cur is None:
            continue
        if not cur["body"]:
            if ln.startswith("+++ "):
                p = ln[4:]
                cur["path"] = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
                continue
            if ln.startswith("--- "):
                p = ln[4:]
                cur["old"] = None if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
                continue
            if ln.startswith("rename to "):
                cur["path"] = ln[len("rename to "):]
                continue
            if ln.startswith("rename from "):
                cur["old"] = ln[len("rename from "):]
                continue
            if not ln.startswith(("@@", "Binary files")):
                continue  # index, mode and similarity lines carry no signal here
        cur["body"].append(ln)
    for b in blocks:
        if not b["path"]:
            b["path"] = b["old"] or b["head"].rpartition(" b/")[2]
        while b["body"] and b["body"][-1] == "":
            b["body"].pop()
    return blocks


def _split_revs(args, cwd):
    revs, paths = [], []
    for x in args:
        if paths or os.path.lexists(x):
            paths.append(x)  # git convention: once a path appears, the rest are paths
        elif ".." in x or _git(cwd, "rev-parse", "-q", "--verify", x + "^{commit}")[0] == 0:
            revs.append(x)
        else:
            paths.append(x)
    return revs, paths


def cmd_diff(a):
    """Token-light git diff: stat first, -U1 hunks, lock/generated files folded, capped."""
    _reset()
    cwd = _cwd()
    args = list(a.args or [])
    if a.no_index or (len(args) == 2 and all(os.path.lexists(x) for x in args) and not _both_tracked(args, cwd)):
        if len(args) != 2:
            ui.err("dev diff --no-index needs exactly two paths")
            return 2
        return _diff(a, cwd, None, ["--no-index"], args, [], no_index=True)
    top = repo_top(cwd)
    if not top:
        ui.err("dev diff: not inside a git repository (compare two files with: dev diff A B)")
        return 2
    revs, paths = _split_revs(args, cwd)
    return _diff(a, cwd, top, (["--cached"] if a.staged else []) + revs, paths, revs)


def _both_tracked(args, cwd) -> bool:
    top = repo_top(cwd)
    return bool(top) and _git(cwd, "ls-files", "--error-unmatch", "--", *args)[0] == 0


def _diff(a, cwd, top, pre, paths, revs, no_index=False) -> int:
    base = ["diff", "--no-color", "--no-ext-diff", "-M"] + pre

    def run(*extra, spec=None):
        return _git(cwd, *base, *extra, "--", *(paths if spec is None else spec), timeout=180)

    rc, ns, err = run("--numstat", "-z")
    if rc not in (0, 1) or (rc == 1 and not no_index):
        ui.err(f"dev diff: {(err.splitlines() or ['git diff failed'])[0]}")
        return 2
    files = _parse_numstat(ns)
    status = _parse_name_status(run("--name-status", "-z")[1]) if not no_index else {}

    def disp(p):
        return p if no_index else _rel(os.path.join(top, p))

    def name(f):
        return f"{disp(f['old'])} → {disp(f['path'])}" if f["old"] else disp(f["path"])

    untracked = []
    if not no_index and not revs and not a.staged:
        out = _gitout(cwd, "ls-files", "-o", "--exclude-standard", "-z", "--", *paths)
        untracked = [x for x in out.split("\0") if x]
    foot = []
    if untracked:
        foot.append("untracked (not in this diff): " + ", ".join(untracked[:5])
                    + (f" +{len(untracked) - 5} more" if len(untracked) > 5 else "") + " — git add -N to include")
    if not files:
        print("\n".join(["no changes" + (f" ({' '.join(pre)})" if pre else "")] + foot))
        return 0
    if a.names:
        lines = [f"{status.get(f['path'], 'M')} {name(f)}" for f in files[:a.max or None]]
        if a.max and len(files) > a.max:
            lines.append(f"+{len(files) - a.max} more — narrow with a path")
        print("\n".join(lines + foot))
        return 0
    explicit = set() if no_index else {os.path.relpath(_target(p), top) for p in paths}
    for f in files:
        why = "binary" if f["binary"] else None
        if not why and f["path"] not in explicit and not no_index:
            why = _generated(f["path"]) or ("large" if a.fold and f["add"] + f["del"] > a.fold else None)
        f["fold"] = why
    lines = [f"{_plural(len(files), 'file')} changed, +{sum(f['add'] for f in files)} -{sum(f['del'] for f in files)}"
             + (" · staged" if a.staged else "") + (f" · {' '.join(revs)}" if revs else "")]
    stat_cap = 40
    for f in files[:stat_cap]:
        n = "binary" if f["binary"] else f"+{f['add']} -{f['del']}"
        fold = f"  (folded: {f['fold']} — dev diff {disp(f['path'])})" if f["fold"] and not f["binary"] else ""
        lines.append(f"{status.get(f['path'], 'M' if not no_index else '')} {name(f)}  {n}{fold}".lstrip())
    if len(files) > stat_cap:
        lines.append(f"+{len(files) - stat_cap} more files — dev diff --names")
    lines += foot
    print("\n".join(lines))
    cap = (a.max - len(lines)) if a.max else 10 ** 9
    if a.syntax and core.have("difft"):
        env = dict(core.GIT_ENV, GIT_EXTERNAL_DIFF="difft", DFT_DISPLAY="inline", DFT_COLOR="never",
                   DFT_WIDTH=str(LINE_WIDTH))
        _, out, _ = _run(["git", "-C", cwd, "diff", "--ext-diff", *pre, "--", *paths], timeout=180, env=env)
        body = out.rstrip("\n").split("\n")
        print("\n".join(body[:max(0, cap)]))
        if len(body) > cap:
            print(f"+{len(body) - cap} lines — dev diff --syntax <file>, or --max 0")
        return 0
    if a.syntax:
        ui.warn("difftastic is not installed (brew install difftastic); showing the plain diff")
    show = [f for f in files if not f["fold"]]
    if not show:
        return 0
    spec = None
    folded = [f for f in files if f["fold"]]
    if folded and not no_index:
        excl = [":(top,exclude)" + p for f in folded for p in (f["path"], f["old"]) if p]
        spec = (list(paths) or [":(top)"]) + excl
    extra = [f"-U{max(0, a.unified)}", "--src-prefix=a/", "--dst-prefix=b/"] + (["--word-diff=plain"] if a.word else [])
    _, patch, _ = run(*extra, spec=spec)
    blocks = [b for b in _parse_patch(patch.rstrip("\n")) if b["body"]]
    out, pending = [], []
    for b in blocks:
        label = disp(b["path"])
        st = status.get(b["path"], "")
        tag = " (new)" if st == "A" else " (deleted)" if st == "D" else ""
        if cap - len(out) < 3:
            pending.append((label, len(b["body"])))
            continue
        room = cap - len(out) - 1
        out.append(_paint(ui.bold, f"── {label}{tag}"))
        if len(b["body"]) <= room:
            out += b["body"]
        else:
            out += b["body"][:room - 1]
            out.append(f"+{len(b['body']) - (room - 1)} lines — dev diff {label} --max 0")
    for label, n in pending[:10]:
        out.append(f"+{n} lines — dev diff {label}")
    if len(pending) > 10:
        out.append(f"+{len(pending) - 10} more files — dev diff --names")
    if _tty():
        out = [ui.green(ln) if ln.startswith("+") and not ln.startswith("+++") else
               ui.red(ln) if ln.startswith("-") else ui.cyan(ln) if ln.startswith("@@") else ln for ln in out]
    print("\n".join(out))
    return 0


# --------------------------------------------------------------------------- registration

def register(add):
    """Wire the commands into the `dev` parser. `add(name, fn, help)` returns an argparse subparser."""
    p = add("rm", cmd_rm, "Safe delete to the Trash; refuses tracked, unsaved, in-use or workspace paths (--undo)")
    p.add_argument("paths", nargs="*", help="files or directories")
    p.add_argument("-r", "-R", "--recursive", action="store_true", help="allow directories")
    p.add_argument("-f", dest="missing_ok", action="store_true",
                   help="ignore paths that do not exist (never overrides a safety check)")
    p.add_argument("--tracked", action="store_true", help="allow git-tracked paths (the deletion is staged, like git rm)")
    p.add_argument("--discard", action="store_true",
                   help="allow losing uncommitted, untracked or gitignored local work (it stays in the Trash)")
    p.add_argument("--force-in-use", action="store_true", help="allow paths a running process has open")
    p.add_argument("--outside", action="store_true",
                   help="allow paths outside ~/Development and temp roots (never system or home folders)")
    p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    p.add_argument("--why", default="", help="reason recorded in the manifest")
    p.add_argument("--undo", nargs="?", type=int, const=1, metavar="N", help="restore the last N dev rm runs")
    p.add_argument("--list", action="store_true", help="recent deletions")

    for name, fn, help_ in (("mv", cmd_mv, "Move or rename without clobbering; git mv for tracked files"),
                            ("cp", cmd_cp, "Copy without clobbering; -r refuses .git, node_modules, .venv unless asked")):
        p = add(name, fn, help_)
        p.add_argument("paths", nargs="+", metavar="SRC… DEST")
        p.add_argument("-f", "--force", action="store_true", help="replace what exists (the old version goes to the Trash)")
        p.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
        if name == "mv":
            p.add_argument("--cross", action="store_true", help="allow moving across repositories or out of the workspace")
        else:
            p.add_argument("-r", "-R", "--recursive", action="store_true", help="copy directories")
            p.add_argument("--skip-heavy", action="store_true", help="copy without .git, node_modules and .venv")
            p.add_argument("--include-heavy", action="store_true", help="copy .git, node_modules and .venv too")

    p = add("files", cmd_files, "Compact gitignore-aware tree: dirs first, sizes, git markers, noise folded")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("-d", "--depth", type=int, default=2, help="levels to expand (default 2)")
    p.add_argument("--all", action="store_true", help="include gitignored files and build output")
    p.add_argument("--max", type=int, default=CAP_FILES, help=f"line cap (default {CAP_FILES}, 0 = none)")
    p.add_argument("--json", action="store_true")

    p = add("find", cmd_find, "Find files by name (glob or regex), gitignore-aware, capped")
    p.add_argument("pattern", nargs="?", default="", help="glob (*.py) or regex; smart case")
    p.add_argument("path", nargs="?", default=".", help="directory to search (default .)")
    p.add_argument("-e", "--ext", action="append", help="extension, repeatable")
    p.add_argument("-t", "--type", choices=["f", "d"], help="files or directories only")
    p.add_argument("-g", "--glob", action="store_true", help="treat the pattern as a glob")
    p.add_argument("--regex", action="store_true", help="treat the pattern as a regex")
    p.add_argument("-p", "--full-path", action="store_true", help="match the path, not only the name")
    p.add_argument("--changed", action="store_true", help="only paths git reports as changed or untracked")
    p.add_argument("--since", help="changed within 30m, 2h, 2d, 1w or since YYYY-MM-DD")
    p.add_argument("--size", help="+1M (at least) or -100k (at most)")
    p.add_argument("--all", action="store_true", help="include gitignored files")
    p.add_argument("--max", type=int, default=CAP_FIND, help=f"result cap (default {CAP_FIND}, 0 = none)")
    p.add_argument("--json", action="store_true")

    p = add("grep", cmd_grep, "Search contents with ripgrep: file:line: text, trimmed and capped")
    p.add_argument("pattern")
    p.add_argument("paths", nargs="*")
    p.add_argument("-i", "--ignore-case", action="store_true")
    p.add_argument("-s", "--case-sensitive", action="store_true", help="default: smart case")
    p.add_argument("-F", "--fixed", action="store_true", help="literal string, not a regex")
    p.add_argument("-w", "--word", action="store_true", help="whole words")
    p.add_argument("-g", "--glob", action="append", help="include/exclude files: -g '*.py' -g '!tests/**'")
    p.add_argument("-t", "--type", action="append", help="ripgrep file type: py, ts, java, …")
    p.add_argument("-l", "--files", action="store_true", help="matching files only")
    p.add_argument("-C", "--context", type=int, default=0, metavar="N", help="context lines")
    p.add_argument("--count", action="store_true", help="matching lines per file")
    p.add_argument("--max", type=int, default=CAP_GREP, help=f"line cap (default {CAP_GREP}, 0 = none)")
    p.add_argument("--max-per-file", type=int, default=CAP_PER_FILE, help=f"default {CAP_PER_FILE}")
    p.add_argument("--all", action="store_true", help="include gitignored files")
    p.add_argument("--ast", action="store_true", help="structural search with ast-grep ($X metavariables)")
    p.add_argument("--lang", help="language for --ast")
    p.add_argument("--json", action="store_true")

    p = add("diff", cmd_diff, "Token-light git diff: stat first, -U1 hunks, lock/generated files folded, capped")
    p.add_argument("args", nargs="*", metavar="REV|PATH")
    p.add_argument("--staged", "--cached", action="store_true", help="staged changes")
    p.add_argument("--word", action="store_true", help="word diff")
    p.add_argument("--names", action="store_true", help="changed paths only")
    p.add_argument("--syntax", action="store_true", help="structural diff with difftastic (difft)")
    p.add_argument("-U", "--unified", type=int, default=1, metavar="N", help="context lines (default 1)")
    p.add_argument("--max", type=int, default=CAP_DIFF, help=f"total line cap (default {CAP_DIFF}, 0 = none)")
    p.add_argument("--fold", type=int, default=FOLD_AT, help=f"fold files with more changed lines (default {FOLD_AT})")
    p.add_argument("--no-index", action="store_true", help="compare two paths directly, outside git")
    return None
