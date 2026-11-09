# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The suite's safety net against this Mac's real coding agents, installer and GitHub CLI: codex, claude, brew, gh.

devkit puts Homebrew's folders and ~/.local/bin first on PATH when it is imported (lib/devkit/core.py), so a stand-in
a test puts first on PATH loses to the real program, in the test's own process and in every devkit process it starts.
The net has four parts:

1. Stand-ins (stand_ins()): a folder holding the four programs, each of which prints that it is a stand-in and exits
   1. tests/_hermetic.py makes one for the run and puts it first on PATH; a test that needs its own stand-ins puts its
   folder ahead of the run's (env(stubs=[...])).
2. DEVKIT_TEST_PATH names those folders. devkit keeps them first in every process it runs as and adds none of
   Homebrew's folders (core.sandbox_path), but only when HOME and every folder named lie below the system's temporary
   folders, which a real Mac's HOME never does. env() sets it, with PATH, for every process a test starts.
3. The tripwire (refusal()): whether a process start would run one of the four from outside the allowed folders.
   tests/_hermetic.py's audit hook refuses such a start in the test process (subprocess, os.exec*, os.posix_spawn*,
   os.system) and records it, so the run fails (test_zz) even where devkit turns the error into an exit code.
4. The launcher (boot()): an older devkit copy, whose core.py predates DEVKIT_TEST_PATH, starts through this file. It
   puts the stand-ins first again once the copy's core.py is imported, refuses any start of the four from outside
   DEVKIT_TEST_PATH's folders and any devkit process the copy would start (that one would run without the net), and
   writes each refusal to DEVKIT_TEST_TRIPWIRE, which the run reports.

In a test: give every process the test starts env(...) (with the test's own stand-ins, if any), and start an older
release with boot(<copy>/bin/dev, ...). Standard library only and Python 3.9, because this file also runs as a
program: `python3 -I -S tests/_sandbox.py <copy>/bin/dev <args>`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

TOOLS = ("codex", "claude", "brew", "gh")
# The system's temporary folders, as written and as macOS resolves them; core.py keeps the same list.
TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/tmp", "/private/var/tmp", "/var/folders", "/private/var/folders")
ALLOWED = TEMP_ROOTS  # where the test process may start one of TOOLS from: anything a test made (a test may narrow it)
STAND_IN = ("#!/bin/sh\necho \"$(basename \"$0\"): stand-in of devkit's tests, never this Mac's real program "
            "(tests/_sandbox.py)\" >&2\nexit 1\n")
EVENTS = ("subprocess.Popen", "os.exec", "os.posix_spawn", "os.system")
_LAUNCHERS = {"env", "exec", "command", "nohup", "sudo", "xcrun", "time", "nice", "timeout"}
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
# The program of each command of a shell line: where a command starts (the line's start, or after ; & | ( ) { } ` or
# a new line), past variable assignments and the launchers that run the next word.
_COMMAND = re.compile(
    r"(?:^|[;&|(){}`\n])\s*"
    r"(?:(?:[A-Za-z_]\w*=\S*|(?:\S*/)?(?:env|exec|command|nohup|sudo|xcrun|time|nice|timeout)(?:\s+(?:-\S+|\d\S*))*)"
    r"\s+)*[\"']?([^\s;&|(){}`\"']+)")
_DEVKIT = re.compile(r"/bin/dev(?=$|[\s'\"])")
_OWN: list = []  # this process's stand-in folder when no hermetic run made one


def stand_ins(folder) -> Path:
    """Write the four stand-ins into `folder` (created) and return it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for tool in TOOLS:
        (folder / tool).write_text(STAND_IN)
        (folder / tool).chmod(0o755)
    return folder


def run_folders(environ=None) -> list:
    """The stand-in folders of this run (DEVKIT_TEST_PATH, set by tests/_hermetic.py), or one of this process's own."""
    env = os.environ if environ is None else environ
    named = [p for p in (env.get("DEVKIT_TEST_PATH") or "").split(os.pathsep) if p]
    if named or environ is not None:
        return named
    if not _OWN:
        _OWN.append(str(stand_ins(tempfile.mkdtemp(prefix="devkit-stand-ins-"))))
    return list(_OWN)


def env(base=None, stubs=(), run=True, **extra) -> dict:
    """The environment of a process a test starts: `base` (default: this process's), with the test's own stand-in
    folders `stubs`, then the run's, first on PATH and named by DEVKIT_TEST_PATH; then `extra`. With run=False the
    test's folders are the only ones: a tool they leave out stays absent (a Mac without Homebrew), since devkit adds
    no folder of its own in a test sandbox; give such a test a `base` PATH without Homebrew's folders."""
    e = dict(os.environ if base is None else base)
    first = list(dict.fromkeys([str(s) for s in stubs] + (run_folders() if run else [])))
    e["DEVKIT_TEST_PATH"] = os.pathsep.join(first)
    e["PATH"] = os.pathsep.join(dict.fromkeys(first + [p for p in (e.get("PATH") or "").split(os.pathsep) if p]))
    if os.environ.get("DEVKIT_TEST_TRIPWIRE"):
        e.setdefault("DEVKIT_TEST_TRIPWIRE", os.environ["DEVKIT_TEST_TRIPWIRE"])
    e.update({k: str(v) for k, v in extra.items()})
    return e


# --------------------------------------------------------------------------- the tripwire

def _words(argv) -> list:
    items = [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or ())
    return [os.fsdecode(w) for w in items if isinstance(w, (str, bytes, os.PathLike))]


def _search(env) -> list:
    """The folders a start with `env` searches for a bare name (os.get_exec_path takes str or bytes keys)."""
    try:
        return [os.fsdecode(d) for d in os.get_exec_path(env)]
    except (TypeError, ValueError):
        return []


def _resolve(word: str, env, cwd, also_own_path=False) -> list:
    """The files a start of `word` would run: the path it names, or the first match on the PATH of `env` (and of this
    process for posix_spawnp, which searches the caller's PATH)."""
    if "/" in word:
        p = word if os.path.isabs(word) else os.path.join(os.fsdecode(cwd) if cwd else os.getcwd(), word)
        return [p] if os.path.isfile(p) else []
    found = []
    for folders in [_search(env)] + ([_search(None)] if also_own_path and env is not None else []):
        for d in folders:
            p = os.path.join(d or ".", word)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                found.append(p)
                break
    return found


def _programs(event, args) -> list:
    """(word, env, cwd) for each program a process start runs as its own or through a launcher or a shell line."""
    if event == "os.system":  # (command,)
        return [(m.group(1), None, None) for m in _COMMAND.finditer(os.fsdecode(args[0] or ""))]
    if event == "subprocess.Popen":  # (executable, args, cwd, env)
        program, words, cwd, env = args[0], _words(args[1]), args[2], args[3]
    else:  # os.exec and os.posix_spawn: (path, argv, env)
        program, words, cwd, env = args[0], _words(args[1]), None, args[2]
    out = [(os.fsdecode(program), env, cwd)] if isinstance(program, (str, bytes, os.PathLike)) else []
    i = 0
    while i < len(words) and os.path.basename(words[i]) in _LAUNCHERS:  # env X=1 codex, nohup gh, xcrun …
        i += 1
        while i < len(words) and (words[i].startswith("-") or re.match(r"[A-Za-z_]\w*=", words[i])):
            i += 1
    if i < len(words):
        out.append((words[i], env, cwd))
    if words and os.path.basename(words[0]) in _SHELLS:  # sh -c "<line>", bash -euc "<line>"
        flag = next((k for k, w in enumerate(words[1:], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", w)), None)
        if flag is not None and flag + 1 < len(words):
            out += [(m.group(1), env, cwd) for m in _COMMAND.finditer(words[flag + 1])]
    return out


def _below(path: str, folders) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in {os.path.realpath(f) for f in folders})


def refusal(event, args, allowed=None) -> str:
    """Why a process start (an audit event of EVENTS and its arguments) is refused: it would run codex, claude, brew
    or gh from outside the `allowed` folders (default ALLOWED). '' when it is not."""
    if event not in EVENTS:
        return ""
    allowed = ALLOWED if allowed is None else allowed
    for word, env, cwd in _programs(event, args):
        name = os.path.basename(word)
        if name not in TOOLS:
            continue
        for path in _resolve(word, env, cwd, also_own_path=event == "os.posix_spawn"):
            if not _below(path, allowed):
                real = os.path.realpath(path)
                return f"{path}{f' ({real})' if real != path else ''}, this Mac's real {name}"
    return ""


def starts_devkit(event, args) -> bool:
    """Whether a process start runs a devkit copy's bin/dev (as its program, an argument, or in a shell line)."""
    if event == "os.system":
        return bool(_DEVKIT.search(os.fsdecode(args[0] or "")))
    if event not in EVENTS:
        return False
    return any(_DEVKIT.search(w) for w in _words(args[1]) + _words(args[0]))


def trips(log=None) -> list:
    """The refusals the launcher wrote to `log` (default: the run's DEVKIT_TEST_TRIPWIRE), one dict each."""
    log = log or os.environ.get("DEVKIT_TEST_TRIPWIRE")
    try:
        with open(log) as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, TypeError, ValueError):
        return []


# --------------------------------------------------------------------------- the launcher for older copies

def boot(dev, *args) -> list:
    """The command that starts an older devkit copy's `dev` (its bin/dev) with `args` inside the net."""
    return [sys.executable, "-I", "-S", os.path.abspath(__file__), str(dev), *map(str, args)]


def _launch(argv: list) -> int:
    dev = argv[0]
    folders = run_folders()
    log = os.environ.get("DEVKIT_TEST_TRIPWIRE")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(dev)), "..", "lib"))  # as bin/dev does
    try:
        import devkit.core as core  # the copy's own: it may put Homebrew's folders first on PATH
    except ImportError:
        core = None
    imported = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(folders + imported.split(os.pathsep)))
    for value in vars(core).values() if core else ():  # copies of the environment it kept then (GIT_ENV)
        if isinstance(value, dict) and value.get("PATH") == imported:
            value["PATH"] = os.environ["PATH"]

    def hook(event, args):
        if event not in EVENTS:
            return
        why = refusal(event, args, allowed=folders)
        if not why and starts_devkit(event, args):
            why = "another devkit process, which would run without the net: " + " ".join(_words(args[1]))[:200]
        if why:
            if log:
                with open(log, "a") as f:
                    f.write(json.dumps({"pid": os.getpid(), "event": event, "why": why, "dev": dev,
                                        "args": argv[1:4]}) + "\n")
            raise PermissionError(f"test safety: refused to start {why} (tests/_sandbox.py)")

    sys.addaudithook(hook)
    sys.argv = list(argv)
    import runpy
    runpy.run_path(dev, run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(_launch(sys.argv[1:]))
