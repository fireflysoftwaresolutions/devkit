# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The test suite's one isolation point: the developer's real devkit state stays out of reach for the whole run.

`activate()` runs before any test module imports devkit (tests/__init__.py calls it; so do the guard test modules,
for a run of one of them alone). It:

1. records where the developer's real devkit lives: the state, configuration and install roots, the handoff
   repository (from the real configuration, read once) and the agents' homes;
2. points HOME at a temporary home for the run (below /var/tmp, see home_base), so devkit's state, configuration,
   dev root and handoff, and the agents' homes, all resolve below it, in this process and in every process a test
   starts;
3. drops what would move one of those roots elsewhere or names the developer's live session: DEVKIT_CONFIG,
   DEV_ROOT, CODEX_HOME, CLAUDE_CONFIG_DIR, the XDG_*_HOME folders and git's global configuration, which then follow
   HOME (the run's, or the one a test makes for itself; exporting them pinned to the run's home would override a
   test's own, and devkit refuses a DEV_ROOT or DEVKIT_CONFIG that does not match its HOME); the agents' markers and
   session ids, their socket, token and env file (CLAUDE_ENV_FILE); and devkit's own DEVKIT_* switches. A run under
   an agent then behaves like a run in CI, which has none of them. DEVKIT_NO_NOTIFY=1 is set, as CI sets it, and
   the audit hook refuses osascript (a security notice ignores that switch): a test never shows the developer a
   notification;
4. snapshots the real state, configuration and handoff roots (for test_zz_real_state_untouched);
5. installs an audit hook that refuses, and records, any file access of this process below the real roots, and any
   process started with the real home, with no HOME (Path.home() then falls back to the real one), with a variable
   or an argument that names a real root, or in a real root;
6. puts the run's stand-ins for codex, claude, brew and gh first on PATH and names them in DEVKIT_TEST_PATH, which
   devkit keeps first, adding none of Homebrew's folders, in this process and in every process that inherits the
   variable; the audit hook refuses, and records, a start of one of the four from outside the temporary folders
   (tests/_sandbox.py has the net and what a test that builds its own environment uses);
7. removes the temporary home when the run ends.

A second call (tests/__init__.py, then a guard module) changes nothing. When devkit was imported first, `activate()`
does nothing either: its paths are already bound to the real home, so the run is not hermetic, and test_hermetic
says so with the command to use.

How test_zz tells this run's changes from those of the developer's running sessions, which keep writing the real
state through devkit's hooks while the suite runs (`report()`):
- in an isolated run, this process cannot reach the real roots (the hook) and the processes it starts get the run's
  home and nothing that names a real root (the hook again), so whatever they write carries the run's temporary home
  in its path or its bytes. A change that carries it is a leak; one that does not was written by another process and
  is only reported, on stderr;
- in a run that is not isolated there is no such mark: every change counts, but those below the files the live hooks
  own (LIVE_WRITERS), which only count when they carry a mark.
"""
from __future__ import annotations

import atexit
import fcntl
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _sandbox  # noqa: E402  (the net against this Mac's real codex, claude, brew and gh)

TREE = Path(__file__).resolve().parents[1]
MARK = "DEVKIT_TEST_HERMETIC"  # the temporary home of the run, for the processes the tests start

# Variables that name the developer's live session or reach it. Every DEVKIT_* switch goes too, but DEVKIT_HOME,
# which names the tree under test.
SCRUB = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
         "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_TMPDIR", "CLAUDE_PID", "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR",
         "CLAUDE_JOB_DIR", "CLAUDE_PLUGIN_ROOT", "CLAUDE_EFFORT", "CLAUDE_MODEL", "CODEX_THREAD_ID", "CODEX_SANDBOX",
         "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_MANAGED_BY_NPM", "GH_CONFIG_DIR",
         "_",  # the shell's path of the command it last ran, which can name a real devkit copy
         # the locations devkit and the tools it runs take from the environment before HOME: without them every one
         # follows HOME, the run's or the one a test makes for itself
         "DEVKIT_CONFIG", "DEV_ROOT", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
         "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
KEEP = {"DEVKIT_HOME": str(TREE), "DEVKIT_NO_NOTIFY": "1"}  # the DEVKIT_* the run sets itself
# The net's variables (tests/_sandbox.py), which name folders and files below the run's own root only: its stand-ins,
# and the log of what the launcher of older devkit copies refused.
NET = ("DEVKIT_TEST_PATH", "DEVKIT_TEST_TRIPWIRE")

# What devkit's live hooks (the developer's running sessions) write below the real roots; used when the run is not
# isolated (see the module docstring).
LIVE_WRITERS = {
    "state": ("audit/*.jsonl", "calls/**", "canary.jsonl", "continuity/**", "dashboard/**", "last-sync.json",
              "lessons-usage.json", "locks/**", "logs/**", "progress.publish", "quota/**", "runs/**", "sessions/**",
              "status/**", "timing/**", "tests/**", "ci/**", "ci-pushes.json", "cache/**", "captures/**",
              "providers.json", "trash.jsonl", "backups/**", "jobs/**", "prs/**", "handoff-check/**",
              "instructions/**", "lessons-events.jsonl", "lessons-cap-restore.json", "lessons-merge-verdicts.json",
              "lessons/locks/**", "bugs/**", "crashes/**"),
    "config": (),
    "handoff": (".git/**", "log/**", "sessions/**", "presence/**", "projects/**", "plans/**", "items/**",
                "initiatives/**", "crew/**", "lessons/**", "PROJECTS.md", "TASKS.md", "ACTIVITY.md",
                "registry.json"),
}

_S = {"active": False, "late": False, "owner": None, "root": None, "home": None, "real": {}, "snapshot": {},
      "violations": [], "allow": 0, "scrubbed": [], "prefixes": [], "named": None, "homes": set(), "own": set()}


# --------------------------------------------------------------------------- the developer's real devkit

def real_roots(home: str, environ=None) -> dict:
    """Where the developer's devkit lives, from their home: name -> absolute path. `handoff` follows the real
    configuration's handoff_dir or dev_root (DEV_ROOT when set), like devkit does."""
    env = os.environ if environ is None else environ
    h = Path(home)
    cfg = {}
    try:
        cfg = json.loads((h / ".config" / "devkit" / "config.json").read_text())
    except (OSError, ValueError):
        pass

    def expand(p):
        p = os.path.expandvars(str(p))
        if p == "~" or p.startswith("~/"):
            p = str(h) + p[1:]
        return os.path.abspath(p)

    dev = expand(env.get("DEV_ROOT") or cfg.get("dev_root") or "~/Development")
    handoff = expand(cfg["handoff_dir"]) if cfg.get("handoff_dir") else os.path.join(dev, ".handoff")
    return {"state": str(h / ".local" / "state" / "devkit"), "config": str(h / ".config" / "devkit"),
            "install": str(h / ".local" / "share" / "devkit"), "handoff": handoff,
            "claude": str(h / ".claude"), "codex": str(h / ".codex"), "agents": str(h / ".agents")}


def real_homes() -> set:
    """The developer's home, as HOME names it and as the account database does (Path.home() without HOME)."""
    homes = {os.path.realpath(os.path.expanduser("~"))}
    try:
        import pwd
        homes.add(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError, OSError):
        pass
    return homes


# --------------------------------------------------------------------------- snapshots and the diff

def snapshot(roots: dict) -> dict:
    """name -> {relative path: (mtime_ns, size, inode)} of every file below each root (symlinks not followed)."""
    out = {}
    _S["allow"] += 1
    try:
        for name, root in roots.items():
            files = {}
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    stack.append(e.path)
                                else:
                                    st = e.stat(follow_symlinks=False)
                                    files[os.path.relpath(e.path, root)] = (st.st_mtime_ns, st.st_size, st.st_ino)
                            except OSError:
                                continue
                except OSError:
                    continue
            out[name] = files
    finally:
        _S["allow"] -= 1
    return out


def _live(name: str, rel: str) -> bool:
    return any(fnmatch.fnmatch(rel, g) or (g.endswith("/**") and rel.startswith(g[:-3] + "/"))
               for g in LIVE_WRITERS.get(name, ()))


def _carries(path: str, since: int, marks) -> bool:
    """True when the bytes of `path` from offset `since` contain one of `marks` (at most 4 MB are read)."""
    _S["allow"] += 1
    try:
        with open(path, "rb") as f:
            f.seek(since)
            data = f.read(4 << 20)
    except OSError:
        return False
    finally:
        _S["allow"] -= 1
    return any(m.encode() in data for m in marks if m)


def outputs(roots: dict, snap: dict, fds=(1, 2)) -> set:
    """The files below `roots` that this process's stdout and stderr (`fds`) write to, as "<name>: <relative path>".
    A caller that sends the run's output below a real root (dev ci --run streams its steps into
    state/ci/logs/<key>/) chose that file; the run's own output there carries its mark but is no leak."""
    own = set()
    for fd in fds:
        try:
            st = os.fstat(fd)
        except OSError:
            continue
        for name, root in roots.items():
            for rel, entry in snap.get(name, {}).items():
                if entry[2] != st.st_ino:
                    continue
                try:
                    if os.lstat(os.path.join(root, rel)).st_dev == st.st_dev:
                        own.add(f"{name}: {rel}")
                except OSError:
                    continue
    return own


def changes(before: dict, after: dict, roots: dict, marks, own=()) -> tuple:
    """(leaks, others): the files below `roots` created, changed or removed between the snapshots, as
    "<name>: <relative path> (<how>)". With `marks` (an isolated run), a change whose path or new bytes (an append's
    new bytes only) carry one is a leak and any other is another process's. Without (a run that is not isolated),
    every change is a leak, but a live writer's (LIVE_WRITERS), which is another process's. The run's own output
    files (`own`, from outputs()) are left out."""
    leaks, others = [], []
    marks = [m for m in marks or () if m]
    for name, root in roots.items():
        b, a = before.get(name, {}), after.get(name, {})
        for rel in sorted(set(b) | set(a)):
            old, new = b.get(rel), a.get(rel)
            if old == new or f"{name}: {rel}" in own:
                continue
            entry = f"{name}: {rel} ({'created' if old is None else 'removed' if new is None else 'changed'})"
            grown = old is not None and new is not None and new[2] == old[2] and new[1] >= old[1]
            marked = bool(marks) and (any(m in rel for m in marks) or (
                new is not None and _carries(os.path.join(root, rel), old[1] if grown else 0, marks)))
            if marked or (not marks and not _live(name, rel)):
                leaks.append(entry)
            elif not _live(name, rel):
                others.append(entry)
    return leaks, others


# --------------------------------------------------------------------------- the audit hook

_WRITE_EVENTS = {"os.remove": (0,), "os.rename": (0, 1), "os.mkdir": (0,), "os.rmdir": (0,), "os.chmod": (0,),
                 "os.chown": (0,), "os.utime": (0,), "os.symlink": (1,), "os.link": (1,), "os.truncate": (0,),
                 "shutil.rmtree": (0,), "shutil.copyfile": (1,), "shutil.copytree": (1,), "shutil.move": (0, 1),
                 "shutil.copymode": (1,), "shutil.copystat": (1,)}
_READ_EVENTS = {"os.listdir": (0,), "os.scandir": (0,), "glob.glob": (0,), "shutil.copyfile": (0,),
                "shutil.copytree": (0,), "sqlite3.connect": (0,)}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_LOCATIONS = ("DEVKIT_CONFIG", "DEV_ROOT", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "CODEX_HOME",
              "CLAUDE_CONFIG_DIR", "CLAUDE_ENV_FILE")


# Where an event that takes a folder descriptor carries it: path argument -> dir_fd argument (CPython's os module;
# a negative value is the working folder). A relative name with one is relative to that folder, not the working one.
_DIR_FD = {"os.remove": {0: 1}, "os.rmdir": {0: 1}, "os.mkdir": {0: 2}, "os.chmod": {0: 2}, "os.chown": {0: 3},
           "os.utime": {0: 3}, "os.rename": {0: 2, 1: 3}, "os.symlink": {1: 2}, "os.link": {0: 2, 1: 3}}


def _fd_folder(fd):
    """The folder an open descriptor names, or None when the system cannot tell."""
    try:
        if hasattr(fcntl, "F_GETPATH"):  # macOS
            return os.fsdecode(fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0]) or None
        return os.readlink(f"/proc/self/fd/{fd}")
    except (OSError, ValueError):
        return None


def _at(p, dir_fd):
    """`p` as an absolute path, relative to the folder `dir_fd` names when it is one; None when that is unknown."""
    if not isinstance(dir_fd, int) or dir_fd < 0 or isinstance(p, int) or p is None:
        return _path(p)
    try:
        p = os.fsdecode(os.fspath(p))
    except TypeError:
        return None
    if os.path.isabs(p):
        return os.path.normpath(p)
    folder = _fd_folder(dir_fd)
    return os.path.normpath(os.path.join(folder, p)) if folder else None


def _in_fd_walk() -> bool:
    """Whether the caller is shutil.rmtree's walk by folder descriptors: its relative names are below the folder the
    shutil.rmtree event named, which the hook judged already, and the open event does not carry the descriptor."""
    f = sys._getframe(2)
    for _ in range(4):
        if f is None:
            return False
        if f.f_code.co_name.startswith("_rmtree_safe_fd") and f.f_globals.get("__name__") == "shutil":
            return True
        f = f.f_back
    return False


def _path(p):
    if isinstance(p, int) or p is None:
        return None
    try:
        p = os.fsdecode(os.fspath(p))
    except TypeError:
        return None
    try:
        return os.path.normpath(os.path.join(os.getcwd(), p)) if not os.path.isabs(p) else os.path.normpath(p)
    except OSError:
        return os.path.normpath(p)


def _protected(p) -> str:
    """The name of the real root `p` lies below, or ''."""
    if not p:
        return ""
    for name, root in _S["prefixes"]:
        if p == root or p.startswith(root + os.sep):
            return name
    return ""


def _named(text) -> str:
    """The name of a real root that a command-line argument or a variable's value names, or ''."""
    if not isinstance(text, (str, bytes)) or not _S["named"]:
        return ""
    m = _S["named"].search(os.fsdecode(text))
    return next((name for name, root in _S["prefixes"] if m and m.group(0) == root), "") if m else ""


def _refuse(kind: str, what: str, detail: str):
    _S["violations"].append(f"{kind} {what}: {detail}")
    raise PermissionError(f"hermetic test run: {kind} of the developer's real devkit is refused: {what} ({detail}); "
                          "use the temporary home of the run (tests/_hermetic.py)")


def _child(argv, env, cwd) -> str:
    """Why a process started with `argv`, `env` and `cwd` would reach the developer's real devkit, or ''."""
    words = [argv] if isinstance(argv, (str, bytes)) else list(argv or ())
    for w in words:
        root = _named(w)
        if root:
            return f"an argument names the real {root} root"
    if _protected(_path(cwd)):
        return f"its folder is in the real {_protected(_path(cwd))} root"
    if env is None:
        return ""
    get = (lambda k: env.get(k.encode()) or env.get(k)) if any(isinstance(k, bytes) for k in env) else env.get
    home = get("HOME")
    home = os.fsdecode(home) if isinstance(home, bytes) else home
    if not home:
        return "no HOME: Path.home() falls back to the real home"
    if os.path.realpath(home) in _S["homes"]:
        return f"HOME={home} is the real home"
    for k in _LOCATIONS:
        v = get(k)
        if v and (_named(v) or os.path.realpath(os.fsdecode(v)) in _S["homes"]):
            return f"{k}={os.fsdecode(v)} names the real devkit"
    return ""


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
# osascript as the program of a shell line: where a command starts (the line's start, or after ; & | ( ) { } ` or a
# new line), past variable assignments and the launchers that run the next word, by any path, quoted or not
_OSASCRIPT_LINE = re.compile(
    r"(?:^|[;&|(){}`\n])\s*"
    r"(?:(?:[A-Za-z_]\w*=\S*|(?:\S*/)?(?:env|exec|command|nohup|sudo|xcrun|time|nice|timeout)(?:\s+(?:-\S+|\d\S*))*)"
    r"\s+)*[\"']?(?:\S*/)?osascript[\"']?(?=\s|$|[;&|)}`])")


def _runs_osascript(event, args) -> bool:
    """Whether a process start runs osascript: its program (Popen's executable, the path posix_spawn and exec run) or
    its argv[0] is osascript, a shell runs it in its line (`sh -c`, shell=True, os.system), or a launcher at the start
    of its argv runs it (`env X=1 osascript`)."""
    if event == "os.system":  # (command,)
        return bool(_OSASCRIPT_LINE.search(os.fsdecode(args[0] or "")))
    program, argv = args[0], args[1]  # Popen: (executable, args, cwd, env); posix_spawn and exec: (path, argv, env)
    words = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or ())
    words = [os.fsdecode(w) for w in words if isinstance(w, (str, bytes, os.PathLike))]
    starts = ([os.fsdecode(program)] if isinstance(program, (str, bytes, os.PathLike)) else []) + words[:1]
    names = {os.path.basename(w) for w in starts} | {os.path.basename(w.split(None, 1)[0]) for w in starts if w.split()}
    if "osascript" in names:
        return True
    if names & _SHELLS:
        return any(_OSASCRIPT_LINE.search(w) for w in words[1:])
    return bool(words and _OSASCRIPT_LINE.match(" ".join(words)))


def _test_frame() -> str:
    """The test method on the stack, as "<file>:<line> <name>", or ''."""
    f = sys._getframe(2)
    while f is not None:
        name = os.path.basename(f.f_code.co_filename)
        if name.startswith("test_") and f.f_code.co_name.startswith("test"):
            return f"{name}:{f.f_lineno} {f.f_code.co_name}"
        f = f.f_back
    return ""


def _trip(why: str):
    """Refuse a start of this Mac's real codex, claude, brew or gh, and record it for test_zz."""
    where = _test_frame()
    _S["violations"].append(f"tripwire {why}" + (f" (in {where})" if where else ""))
    raise PermissionError(f"test safety: refused to start {why}; a test starts the stand-ins first on PATH, never "
                          "the real program (tests/_sandbox.py)")


def _hook(event, args):
    if _S["allow"] or not _S["active"]:
        return
    if event in ("subprocess.Popen", "os.posix_spawn", "os.exec", "os.system") and _runs_osascript(event, args):
        # a security notice ignores DEVKIT_NO_NOTIFY (devkit's acceptance notice), so the run refuses the program
        _refuse("process", "osascript", "a test never shows the developer a desktop notification")
    if event in _sandbox.EVENTS:
        why = _sandbox.refusal(event, args)
        if why:
            _trip(why)
    if event == "open":
        rel = isinstance(args[0], (str, bytes, os.PathLike)) and not os.path.isabs(os.fsdecode(os.fspath(args[0])))
        p = "" if rel and _in_fd_walk() else _protected(_path(args[0]))
        if p:
            mode, flags = args[1], args[2] if len(args) > 2 else 0
            write = any(c in mode for c in "wax+") if isinstance(mode, str) else bool((flags or 0) & _WRITE_FLAGS)
            _refuse("write" if write else "read", _path(args[0]), f"open {mode or flags!r}, real {p} root")
        return
    if event == "subprocess.Popen":  # (executable, args, cwd, env)
        why = _child(args[1], args[3], args[2])
        if why:
            _refuse("process", str(args[1])[:200], why)
        return
    if event in ("os.posix_spawn", "os.exec"):  # (path, argv, env)
        why = _child(args[1], args[2], None)
        if why:
            _refuse("process", str(args[0])[:200], why)
        return
    for kind, table in (("write", _WRITE_EVENTS), ("read", _READ_EVENTS)):
        idx = table.get(event)
        if idx:
            fds = _DIR_FD.get(event, {})
            for i in idx:
                if i < len(args):
                    at = _at(args[i], args[fds[i]] if i in fds and fds[i] < len(args) else None)
                    p = _protected(at)
                    if p:
                        _refuse(kind, at, f"{event}, real {p} root")
            return


# --------------------------------------------------------------------------- activation

def home_base() -> str:
    """Where the run's home goes: /var/tmp. The guard treats /tmp, /var/folders and $TMPDIR as scratch, where any
    write is allowed (core.temp_roots); a home there would make every path below it scratch, and the guard tests
    that need a home outside scratch (a real home never is one) would pass or fail for that reason alone."""
    return "/var/tmp" if os.path.isdir("/var/tmp") and os.access("/var/tmp", os.W_OK) else tempfile.gettempdir()


def _cleanup(root: str, owner: int):
    if os.getpid() == owner:
        shutil.rmtree(root, ignore_errors=True)


def _record_real(home: str, homes: set):
    _S["real"] = {k: os.path.normpath(v) for k, v in real_roots(home).items()}
    # each root as written and resolved (a symlinked home or handoff_dir has two spellings), longest first
    _S["prefixes"] = sorted({(k, p) for k, v in _S["real"].items() for p in (v, os.path.realpath(v))},
                            key=lambda kp: -len(kp[1]))
    _S["named"] = re.compile("(?:" + "|".join(re.escape(p) for _, p in _S["prefixes"]) + r")(?=$|[/\s'\":;])")
    _S["homes"] = homes


def scrub(environ) -> list:
    """Remove from `environ` (changed in place) what names the developer's live session or reaches it; returns the
    names of the variables it changed. Beyond the names it knows, any entry of a value (a path list is split on
    os.pathsep) that lies below a real root goes, whatever the variable: a shell exports its own path lists (zsh's
    FPATH can list a devkit release's completions) and a list of names never keeps up with them."""
    changed = []
    for k in list(environ):
        if k in SCRUB or (k.startswith("DEVKIT_") and k not in KEEP):
            changed.append(k)
            del environ[k]
            continue
        parts = environ[k].split(os.pathsep)
        kept = [p for p in parts if not (p.startswith("/") and (_protected(os.path.normpath(p)) or
                                                                 _protected(os.path.realpath(p))))]
        if len(kept) != len(parts):
            changed.append(k)
            if any(kept):
                environ[k] = os.pathsep.join(kept)
            else:
                del environ[k]
    return changed


def activate() -> dict:
    """Isolate the run (see the module docstring). Returns the state: active, late, root, home, real, violations."""
    if _S["active"] or _S["late"]:
        return _S
    if any(m == "devkit" or m.startswith("devkit.") for m in sys.modules):
        _S["late"] = True
        return _S
    inherited = os.environ.get(MARK)
    if inherited and os.path.isdir(inherited) and \
            os.path.realpath(os.environ.get("HOME", "")) == os.path.realpath(inherited):
        # a test started this process inside an isolated run: keep the run's home
        _S.update(active=True, root=os.path.dirname(inherited), home=inherited)
        return _S
    _record_real(os.path.expanduser("~"), real_homes())
    root = tempfile.mkdtemp(prefix="devkit-hermetic-", dir=home_base())
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, "Development"))
    _S["scrubbed"].extend(scrub(os.environ))
    os.environ.update({"HOME": home, MARK: home, **KEEP})
    stand_ins = str(_sandbox.stand_ins(os.path.join(root, "stand-ins")))
    os.environ.update(dict(zip(NET, (stand_ins, os.path.join(root, "tripwire.jsonl")))))
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys([stand_ins] + os.environ.get("PATH", "").split(os.pathsep)))
    snap = snapshot(watched())
    _S.update(root=root, home=home, owner=os.getpid(), snapshot=snap, own=outputs(watched(), snap))
    atexit.register(_cleanup, root, os.getpid())
    _S["active"] = True
    sys.addaudithook(_hook)
    return _S


def watch_late():
    """In a run that is not isolated (devkit was imported first), snapshot the real roots now, before the first test
    runs (unittest imports every module before it runs any test), so test_zz still sees what the tests changed."""
    if _S["late"] and not _S["owner"]:
        _record_real(os.path.expanduser("~"), real_homes())
        snap = snapshot(watched())
        _S.update(owner=os.getpid(), snapshot=snap, own=outputs(watched(), snap))


def watched() -> dict:
    """The real roots test_zz compares before and after the run: state, configuration and handoff."""
    return {k: _S["real"][k] for k in ("state", "config", "handoff") if k in _S["real"]}


def report() -> tuple:
    """(leaks, others): what the run did to the developer's real devkit (the hook's refusals, then the changed files
    that are the run's), and the files that other processes changed during the run (see the module docstring)."""
    if not _S["owner"]:
        return [], []
    root = _S["root"]
    marks = [root, os.path.realpath(root), os.path.basename(root)] if root else []
    leaks, others = changes(_S["snapshot"], snapshot(watched()), watched(), marks, own=_S["own"])
    tripped = [f"tripwire {t.get('why')} (older devkit {t.get('dev')} {' '.join(t.get('args') or [])})"
               for t in _sandbox.trips(os.path.join(root, "tripwire.jsonl"))] if root else []
    return list(_S["violations"]) + tripped + leaks, others


def state() -> dict:
    return _S
