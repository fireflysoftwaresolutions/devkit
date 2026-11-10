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
   os.system) and records it, so the test fails even where devkit turns the error into an exit code (the hermetic
   run fails each test that tripped it, in any run; test_zz reports it again in a whole run). The same hook refuses a
   devkit child whose environment the test built without env(), which would put Homebrew's folders first again: a
   bin/dev, a `dev` command or Python running devkit (-m, -c, a script that imports it), as the program, through a
   launcher or in a shell line, with an environment that has no DEVKIT_TEST_PATH (devkit_starts).
4. The launcher (boot()): an older devkit copy, whose core.py predates DEVKIT_TEST_PATH, starts through this file. It
   puts the stand-ins first again once the copy's core.py is imported, refuses any start of the four from outside
   DEVKIT_TEST_PATH's folders and any devkit process the copy would start (that one would run without the net), and
   writes each refusal to DEVKIT_TEST_TRIPWIRE, which the run reports.

In a test: give every process the test starts env(...) (with the test's own stand-ins, if any), and start an older
release with boot(<copy>/bin/dev, ...). Standard library only and Python 3.9, because this file also runs as a
program: `python3 -I -S tests/_sandbox.py <copy>/bin/dev <args>`.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
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
# a new line), past the keywords that run the next word (if, then, elif, else, do, while, until, !), variable
# assignments (whose value ends where a command substitution, $( or `, starts its own command) and the launchers that
# run the next word. Names in any case: macOS finds codex as Codex.
_COMMAND = re.compile(
    r"(?:^|[;&|(){}`\n])\s*"
    r"(?:(?:(?:if|then|elif|else|do|while|until|!)(?=\s)|[A-Za-z_]\w*=(?:[^\s$`]|\$(?!\())*"
    r"|(?:\S*/)?(?:env|exec|command|nohup|sudo|xcrun|time|nice|timeout)(?:\s+(?:-\S+|\d\S*))*)"
    r"\s+)*[\"']?([^\s;&|(){}`\"']+)", re.IGNORECASE)
_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}  # run the next word
_DEVKIT = re.compile(r"/bin/dev(?=$|[\s'\"])")
_IMPORTS_DEVKIT = re.compile(r"\bfrom\s+devkit\b|\bimport\s+(?:[\w.]+\s*,\s*)*devkit\b"
                             r"|(?:\bimport_module|\b__import__)\(\s*['\"]devkit\b")
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
        atexit.register(_remove, _OWN[0], os.getpid())
    return list(_OWN)


def _remove(folder: str, owner: int):
    if os.getpid() == owner:  # a forked child exits with the folder its parent still uses
        shutil.rmtree(folder, ignore_errors=True)


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
    while i < len(words) and os.path.basename(words[i]).lower() in _LAUNCHERS:  # env X=1 codex, nohup gh, xcrun …
        i += 1
        while i < len(words) and (words[i].startswith("-") or re.match(r"[A-Za-z_]\w*=", words[i])):
            i += 1
    if i < len(words):
        out.append((words[i], env, cwd))
    if words and os.path.basename(words[0]).lower() in _SHELLS:  # sh -c "<line>", bash -euc "<line>"
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
        name = os.path.basename(word).lower()  # macOS file names ignore case: Codex is codex
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


def _shell_commands(line: str) -> list:
    """The words of each command of a shell line (split at ; & | ( ) and new lines, quotes removed), past the keywords
    that run the next word; the line split on blanks when it does not parse (an open quote)."""
    try:
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()`\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = line.split()
    commands, words = [], []
    for t in tokens + [";"]:
        if t and all(c in ";&|()`\n" for c in t):
            while words and words[0] in _KEYWORDS:
                words.pop(0)
            if words:
                commands.append(words)
            words = []
        else:
            words.append(t)
    return commands


def _launched(words: list, env):
    """(the words of the program a command runs, past its variable assignments and launchers; the environment it runs
    with, or None for the starting process's own; whether an env launcher built that environment without the
    starting process's: env -i, env - or env -u <name>). `env` is the start's environment (None: this process's)."""
    changed, built, i = {}, False, 0
    base = None if env is None else {os.fsdecode(k): os.fsdecode(v) for k, v in env.items()}
    while i < len(words):
        w = words[i]
        name = os.path.basename(w).lower()
        if re.match(r"[A-Za-z_]\w*=", w):
            k, v = w.split("=", 1)
            changed[k] = v
            i += 1
        elif name == "env":
            i += 1
            while i < len(words) and words[i].startswith("-"):
                o, gone = words[i], None
                if o in ("-", "--ignore-environment") or re.fullmatch(r"-[0v]*i[0iv]*", o):
                    base, changed, built = {}, {}, True
                elif o in ("-u", "--unset"):
                    i += 1
                    gone = words[i] if i < len(words) else ""
                elif o.startswith("--unset="):
                    gone = o[len("--unset="):]
                elif o.startswith("-u"):
                    gone = o[2:]
                elif o in ("-P", "-C", "-S"):  # their value is the next word
                    i += 1
                if gone is not None:
                    base = {k: v for k, v in (os.environ if base is None else base).items() if k != gone}
                    changed.pop(gone, None)
                    built = True
                i += 1
        elif name in _LAUNCHERS:
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        else:
            break
    if changed:
        base = dict(os.environ if base is None else base, **changed)
    return words[i:], base, built


def _python_runs_devkit(words: list, cwd) -> bool:
    """Whether `words` (a program and its arguments) is Python running devkit: `-m devkit[.<module>]`, `-c` code or a
    script whose text imports devkit."""
    if not words or not re.fullmatch(r"python[\d.]*", os.path.basename(words[0]).lower()):
        return False
    k = 1
    while k < len(words):
        w = words[k]
        if w == "--check-hash-based-pycs":
            k += 2
            continue
        if w.startswith("-") and w != "-":  # -I, -IS, -X utf8, -Xutf8, -Ic <code>, -m <module>, -mdevkit
            for j, flag in enumerate(w[1:], 2):
                if flag in "cmWX":
                    value = w[j:] or (words[k + 1] if k + 1 < len(words) else "")
                    if flag == "c":
                        return bool(_IMPORTS_DEVKIT.search(value))
                    if flag == "m":
                        return value.split(".")[0] == "devkit"
                    k += 0 if w[j:] else 1
                    break
            k += 1
            continue
        path = w if os.path.isabs(w) else os.path.join(os.fsdecode(cwd) if cwd else os.getcwd(), w)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return bool(_IMPORTS_DEVKIT.search(f.read(1 << 20)))
        except OSError:
            return False
    return False


def _imports_devkit(path: str) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return bool(_IMPORTS_DEVKIT.search(f.read(1 << 20)))
    except OSError:
        return False


def _is_devkit_dev(word: str, env, cwd) -> bool:
    """Whether a `dev` command runs devkit: it resolves, on the PATH it runs with, to a file that imports devkit (a
    copy's bin/dev), not to a test's own stand-in of the command."""
    if os.path.basename(word) != "dev":
        return False
    return any(_imports_devkit(p) for p in _resolve(word, os.environ if env is None else env, cwd))


def devkit_starts(event, args) -> list:
    """(the environment, whether an env launcher built it) of each devkit process a start runs: a copy's bin/dev, a
    `dev` command, or Python running devkit (as its program, through launchers, or in a shell line). The environment is
    None when the process inherits this one's unchanged."""
    if event not in EVENTS:
        return []
    if event == "os.system":
        env, cwd, commands = None, None, _shell_commands(os.fsdecode(args[0] or ""))
    else:
        words = _words(args[1])
        env, cwd = (args[3], args[2]) if event == "subprocess.Popen" else (args[2], None)
        commands = [words]
        if words and os.path.basename(words[0]).lower() in _SHELLS:
            flag = next((k for k, w in enumerate(words[1:], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", w)), None)
            if flag is not None and flag + 1 < len(words):
                commands += _shell_commands(words[flag + 1])
    out = []
    for command in commands:
        program, child, built = _launched(command, env)
        if not program:
            continue
        if any(_DEVKIT.search(w) for w in program) or _is_devkit_dev(program[0], child, cwd) \
                or _python_runs_devkit(program, cwd):
            out.append((child, built))
    if not out and event != "os.system" and starts_devkit(event, args):  # bin/dev anywhere else in the start
        out.append((_launched(_words(args[1]), env)[1], False))
    return out


def runs_devkit(event, args) -> bool:
    """Whether a process start runs devkit (devkit_starts)."""
    return bool(devkit_starts(event, args))


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
