# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The suite's safety net against this Mac's real coding agents, installer and GitHub CLI: codex, claude, brew, gh;
and against changes to this Mac's global npm packages.

devkit puts Homebrew's folders and ~/.local/bin first on PATH when it is imported (lib/devkit/core.py), so a stand-in
a test puts first on PATH loses to the real program, in the test's own process and in every devkit process it starts.
The net has five parts:

1. Stand-ins (stand_ins()): a folder holding the four programs, each of which prints that it is a stand-in and exits
   1. tests/_hermetic.py makes one for the run and puts it first on PATH; a test that needs its own stand-ins puts its
   folder ahead of the run's (env(stubs=[...])).
2. DEVKIT_TEST_PATH names those folders. devkit keeps them first in every process it runs as and adds none of
   Homebrew's folders (core.sandbox_path), but only when HOME and every folder named lie below the system's temporary
   folders, which a real Mac's HOME never does. env() sets it, with PATH, for every process a test starts.
3. The tripwire (refusal()): whether a process start would run one of the four from outside the allowed folders, or
   run npm from outside them to install, update or remove global packages (npm_change; reads such as `npm root -g`,
   which the visual tests use to find axe-core and @playwright/cli, stay allowed). tests/_hermetic.py's audit hook refuses such a start in the test process (subprocess, os.exec*, os.posix_spawn*,
   os.system) and records it, so the test fails even where devkit turns the error into an exit code (the hermetic
   run fails each test that tripped it, in any run; test_zz reports it again in a whole run). The same hook refuses a
   devkit child whose environment the test built without env(), which would put Homebrew's folders first again: a
   bin/dev, a `dev` command or Python running devkit (-m, -c, a script that imports it), as the program, through a
   launcher or in a shell line, with an environment that has no DEVKIT_TEST_PATH (devkit_starts).
4. The launcher (boot()): an older devkit copy, whose core.py predates DEVKIT_TEST_PATH, starts through this file. It
   puts the stand-ins first again once the copy's core.py is imported, refuses any start of the four from outside
   DEVKIT_TEST_PATH's folders and any devkit process the copy would start (that one would run without the net), and
   writes each refusal to DEVKIT_TEST_TRIPWIRE, which the run reports.
5. The npm guard (npm_guard()): the audit hook sees only the test process, so tests/_hermetic.py puts an `npm` first on
   PATH, ahead of Homebrew's folders, for the run and every process that inherits its PATH. It refuses a call that
   would change the global packages and writes the refusal to DEVKIT_TEST_TRIPWIRE, so the test that caused it fails;
   it passes every other call to the next npm on PATH. It stays out of DEVKIT_TEST_PATH: a test that builds a
   system-only PATH from the stand-in folders keeps npm absent.

In a test: give every process the test starts env(...) (with the test's own stand-ins, if any), and start an older
release with boot(<copy>/bin/dev, ...). Standard library only and Python 3.9, because this file also runs as a
program: `python3 -I -S tests/_sandbox.py <copy>/bin/dev <args>`, and as the npm guard,
`python3 -I -S tests/_sandbox.py --npm-guard <npm's arguments>`.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
import stat
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
# The options of each launcher that read their value from the next word, as its own parser does (`nice -n 5 codex`,
# `sudo -u root gh`, `xcrun --sdk macosx codex`); the value may also be in the same word (-n5, --signal=KILL, -uNAME).
# One rule for argument lists and shell lines (_through_launchers).
_LAUNCHER_VALUED = {
    "env": {"-u", "--unset", "-P", "-C", "--chdir", "-S", "--split-string"},
    "exec": {"-a"},
    "nice": {"-n", "--adjustment"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "sudo": {"-u", "--user", "-g", "--group", "-C", "--close-from", "-D", "--chdir", "-h", "--host", "-p",
             "--prompt", "-r", "--role", "-t", "--type", "-T", "--command-timeout", "-U", "--other-user"},
    "xcrun": {"-sdk", "--sdk", "-toolchain", "--toolchain"},
    "time": {"-o", "--output", "-f", "--format"},
}
_LAUNCHER_OPERANDS = {"timeout": 1}  # words a launcher reads after its options and before the program: the duration
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
_KEYWORDS = {"if", "then", "elif", "else", "do", "while", "until", "!", "{", "}"}  # run the next word
_DEVKIT = re.compile(r"/bin/dev(?=$|[\s'\"])")
_IMPORTS_DEVKIT = re.compile(r"\bfrom\s+devkit\b|\bimport\s+(?:[\w.]+\s*,\s*)*devkit\b"
                             r"|(?:\bimport_module|\b__import__)\(\s*['\"]devkit\b")
_OWN: list = []  # this process's stand-in folder when no hermetic run made one
# npm's commands that install, update or remove packages, with their aliases (npm 10 and 11). With -g, --global or
# --location=global, or npm_config_global or npm_config_location=global in npm's environment, they change this Mac's
# global packages; `npm link` always does, since it links into the global folder.
NPM_CHANGES = frozenset({
    "install", "add", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal", "isntall",
    "install-test", "it", "install-ci-test", "cit", "clean-install-test", "sit", "ci", "clean-install", "ic",
    "install-clean", "isntall-clean", "uninstall", "unlink", "remove", "rm", "r", "un", "update", "up", "upgrade",
    "udpate", "link", "ln", "rebuild", "rb", "dedupe", "ddp", "prune"})
_NPM_LINKS = {"link", "ln"}
# npm's commands that only read, whose arguments may name a package called like a change (`npm view install -g`)
_NPM_READS = {"root", "ls", "list", "ll", "la", "view", "v", "info", "show", "prefix", "bin", "config", "c", "get",
              "help", "outdated", "search", "s", "find", "explain", "why", "query", "doctor", "version", "fund", "audit"}
# npm's options that read their value from the next word (`--loglevel silent install`): that word is not the command
_NPM_VALUED = {"--prefix", "-C", "--location", "-L", "--registry", "--cache", "--userconfig", "--globalconfig",
               "--workspace", "-w", "--loglevel", "--tag", "--omit", "--include", "--install-strategy", "--before",
               "--otp", "--script-shell", "--auth-type"}
NPM_GUARD = "# npm guard of devkit's tests (tests/_sandbox.py)"  # the line that marks a guard among the npms on PATH


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
    no folder of its own in a test sandbox; give such a test a `base` PATH without Homebrew's folders. When `base`'s
    PATH has a real npm (Homebrew's), the run's npm guard (run=True) follows those folders on PATH, ahead of it, but
    stays out of DEVKIT_TEST_PATH; a PATH without npm keeps it absent (tests/test_fresh_install.py)."""
    e = dict(os.environ if base is None else base)
    first = list(dict.fromkeys([str(s) for s in stubs] + (run_folders() if run else [])))
    guard = [g for g in [_run_guard()] if g and run and any(
        not _is_guard(n) for n in _resolve("npm", {"PATH": e.get("PATH") or ""}, None))]
    e["DEVKIT_TEST_PATH"] = os.pathsep.join(first)
    e["PATH"] = os.pathsep.join(dict.fromkeys(first + guard +
                                              [p for p in (e.get("PATH") or "").split(os.pathsep) if p]))
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
        try:
            p = word if os.path.isabs(word) else os.path.join(os.fsdecode(cwd) if cwd else os.getcwd(), word)
        except OSError:  # this process's folder is gone: a relative path names nothing
            return []
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
        return [(w[0], None, None) for w in (_through_launchers(c)[0] for c in _shell_commands(os.fsdecode(args[0] or "")))
                if w]
    if event == "subprocess.Popen":  # (executable, args, cwd, env)
        program, words, cwd, env = args[0], _words(args[1]), args[2], args[3]
    else:  # os.exec and os.posix_spawn: (path, argv, env)
        program, words, cwd, env = args[0], _words(args[1]), None, args[2]
    out = [(os.fsdecode(program), env, cwd)] if isinstance(program, (str, bytes, os.PathLike)) else []
    _, _, commands = _commands(event, args)  # its own argv (env X=1 codex, nice -n 5 gh), then a shell line's commands
    out += [(w[0], env, cwd) for w in (_through_launchers(c)[0] for c in commands) if w]
    return out


def _below(path: str, folders) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in {os.path.realpath(f) for f in folders})


def refusal(event, args, allowed=None) -> str:
    """Why a process start (an audit event of EVENTS and its arguments) is refused: it would run codex, claude, brew
    or gh from outside the `allowed` folders (default ALLOWED), or npm from outside them to change the global packages
    (npm_change). '' when it is not."""
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
    for word, rest, env, cwd in _npm_starts(event, args):
        change = npm_change(rest, env)
        if not change:
            continue
        for path in _resolve(word, env, cwd, also_own_path=event == "os.posix_spawn"):
            if not _below(path, allowed):
                real = os.path.realpath(path)
                return (f"{path}{f' ({real})' if real != path else ''}, this Mac's real npm, to {change} global "
                        f"packages: npm {' '.join(rest)[:160]}")
    return ""


def npm_change(words, env=None) -> str:
    """The command of npm's arguments `words` when it would install, update or remove this Mac's global packages, with
    npm running in `env` (None: this process's environment); '' for a read (`npm root -g`, `npm ls -g`) or a local
    install. It fails closed: with a global marker (-g, --global or an abbreviation such as --glob, --location global,
    a --prefix naming npm's global folder, or npm_config_global, npm_config_location or npm_config_prefix saying so),
    any word that is a change command counts, unless npm's command is a known read; an option whose value we do not
    know (`--fetch-timeout 60000 install -g x`) cannot hide the command."""
    words = [os.fsdecode(w) for w in words]
    environ = os.environ if env is None else env
    prefixes = _global_prefixes(environ)
    command, value, everywhere = "", False, False
    for k, w in enumerate(words):
        name, eq, given = w.partition("=")
        if w == "-g" or (len(name) >= 4 and "--global".startswith(name) and given.lower() not in ("false", "0")
                         if eq else len(w) >= 4 and "--global".startswith(w)) \
                or w == "--location=global" or (w in ("--location", "-L") and words[k + 1:k + 2] == ["global"]) \
                or (name in ("--prefix", "-C") and eq and _same(given, prefixes)) \
                or (w in ("--prefix", "-C") and words[k + 1:k + 2] and _same(words[k + 1], prefixes)):
            everywhere = True
        if value:  # the value of the option before it
            value = False
        elif w.startswith("-"):
            value = w in _NPM_VALUED
        elif not command:
            command = w
    for k, v in environ.items():
        k, v = os.fsdecode(k).lower(), os.fsdecode(v)  # npm reads npm_config_* in any case
        if (k == "npm_config_global" and v.lower() not in ("", "false", "0")) \
                or (k == "npm_config_location" and v.lower() == "global") \
                or (k == "npm_config_prefix" and _same(v, prefixes)):
            everywhere = True
    if command in NPM_CHANGES and (everywhere or command in _NPM_LINKS):
        return command
    if everywhere and command not in _NPM_READS:
        return next((w for w in words if w in NPM_CHANGES), "")
    return ""


def _global_prefixes(env) -> set:
    """npm's global folders: Homebrew's, and the folder above each folder of `env`'s PATH that holds an npm."""
    found = {"/opt/homebrew", "/usr/local"}
    for folder in _search(env):
        npm = os.path.join(folder or ".", "npm")
        if folder and os.path.isfile(npm) and not _is_guard(npm):
            found |= {os.path.dirname(folder.rstrip("/")), os.path.dirname(os.path.dirname(os.path.realpath(npm)))}
    return {os.path.realpath(p) for p in found}


def _same(path: str, prefixes) -> bool:
    return bool(path) and os.path.realpath(os.path.expanduser(path)) in prefixes


def _npm_starts(event, args) -> list:
    """(the word that names npm, npm's arguments, the environment npm runs with or None for this process's, its
    folder) for each npm a process start runs: as its program, through launchers and variable assignments, or in a
    shell line."""
    env, cwd, commands = _commands(event, args)
    out = []
    for command in commands:
        program, child, _ = _launched(command, env)
        if program and os.path.basename(program[0]).lower() == "npm":
            out.append((program[0], program[1:], child, cwd))
    return out


def npm_guard(folder) -> Path:
    """Write the npm guard (an `npm` that runs this file with --npm-guard) into `folder` (created) and return it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    guard = folder / "npm"
    guard.write_text(f"#!/bin/sh\n{NPM_GUARD}\nexec {shlex.quote(sys.executable)} -I -S "
                     f"{shlex.quote(os.path.abspath(__file__))} --npm-guard \"$@\"\n")
    guard.chmod(0o755)
    return folder


def _run_guard() -> str:
    """The folder of the run's npm guard: the one tests/_hermetic.py recorded when it started the run (a test may
    replace this process's PATH since), else the first folder on this process's PATH whose npm is a guard ('' when
    none)."""
    hermetic = sys.modules.get("_hermetic")
    recorded = hermetic.state().get("guard") if hermetic else ""
    if recorded and _is_guard(os.path.join(recorded, "npm")):
        return recorded
    for folder in os.get_exec_path():
        if folder and _is_guard(os.path.join(folder, "npm")):
            return folder
    return ""


def _is_guard(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return NPM_GUARD.encode() in f.read(4096)
    except OSError:
        return False


def _npm_guard(args: list) -> int:
    """The npm guard: refuse a call that would change the global packages and write the refusal to
    DEVKIT_TEST_TRIPWIRE (exit 1); run any other call with the next npm on PATH that is not a guard (none: exit 127)."""
    change = npm_change(args)
    if change:
        why = f"npm {' '.join(args)[:200]}, which would {change} this Mac's global npm packages"
        log = os.environ.get("DEVKIT_TEST_TRIPWIRE")
        if log:
            with open(log, "a") as f:
                f.write(json.dumps({"pid": os.getpid(), "event": "npm", "why": why, "by": "npm guard"}) + "\n")
        print(f"npm: devkit's tests refused {why} (the npm guard, tests/_sandbox.py)", file=sys.stderr)
        return 1
    for folder in os.get_exec_path():
        npm = os.path.join(folder or ".", "npm")
        if os.path.isfile(npm) and os.access(npm, os.X_OK) and not _is_guard(npm):
            os.execv(npm, [npm, *args])
    print("npm: not found after the npm guard of devkit's tests on PATH (tests/_sandbox.py)", file=sys.stderr)
    return 127


def starts_devkit(event, args) -> bool:
    """Whether a process start runs a devkit copy's bin/dev (as its program, an argument, or in a shell line)."""
    if event == "os.system":
        return bool(_DEVKIT.search(os.fsdecode(args[0] or "")))
    if event not in EVENTS:
        return False
    return any(_DEVKIT.search(w) for w in _words(args[1]) + _words(args[0]))


def _without_comments(line: str) -> str:
    """The shell line without its comments: a # that starts a word, outside quotes, to the end of its line. A # inside
    a word (a#b) is part of it."""
    out, quote, k = [], "", 0
    while k < len(line):
        c = line[k]
        if quote:
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"':
                out.append(c)
                k += 1
                c = line[k] if k < len(line) else ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            out.append(c)
            k += 1
            c = line[k] if k < len(line) else ""
        elif c == "#" and (k == 0 or line[k - 1] in " \t\n;&|()`"):
            end = line.find("\n", k)
            k = len(line) if end < 0 else end
            continue
        out.append(c)
        k += 1
    return "".join(out)


def _shell_commands(line: str, depth: int = 0) -> list:
    """The words of each command of a shell line (split at ; & | ( ) and new lines, quotes removed, comments dropped),
    past the keywords that run the next word, and those of each command substitution ($( ) or ` `) a quoted word
    holds; the line split on blanks when it does not parse (an open quote)."""
    try:
        lex = shlex.shlex(_without_comments(line), posix=True, punctuation_chars=";&|()`\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        lex.commenters = ""
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
            if depth < 4 and ("$(" in t or "`" in t):  # "$(codex login)": the quotes kept it one word
                commands += _shell_commands(t.replace("$(", ";").replace("`", ";"), depth + 1)
    return commands


def _through_launchers(words: list) -> tuple:
    """(the words of the program a command runs, past its variable assignments and launchers with their options and
    the options' values; what they do to its environment, in order: ("set", name, value), ("unset", name) or
    ("clear",) for env -i). env -S's string is read as words of the command. One rule for argument lists and the
    commands of shell lines."""
    words, effects, i = list(words), [], 0
    while i < len(words):
        name = os.path.basename(words[i]).lower()
        if _ASSIGNMENT.match(words[i]):
            effects.append(("set",) + tuple(words[i].split("=", 1)))
            i += 1
            continue
        if name not in _LAUNCHER_VALUED and name not in _LAUNCHERS:
            break
        valued, i = _LAUNCHER_VALUED.get(name, set()), i + 1
        while i < len(words):
            o = words[i]
            if name in ("env", "sudo") and _ASSIGNMENT.match(o):
                effects.append(("set",) + tuple(o.split("=", 1)))
                i += 1
                continue
            if o == "--":
                i += 1
                break
            if o == "-" and name == "env":
                effects.append(("clear",))
                i += 1
                continue
            if not o.startswith("-") or o == "-":
                break
            opt, eq, value = o.partition("=")
            if o in valued or (eq and opt in valued) or o.startswith("--"):
                if o in valued and not eq:
                    value, i = (words[i + 1] if i + 1 < len(words) else ""), i + 1
                flags = [(opt, value)] if opt in valued else [(o, "")]
            else:  # one-letter flags in one word: -iu NAME, -n5, -0u NAME; a valued one takes the rest or the next word
                flags = []
                for j, letter in enumerate(o[1:], 2):
                    if "-" + letter in valued:
                        value = o[j:]
                        if not value:
                            value, i = (words[i + 1] if i + 1 < len(words) else ""), i + 1
                        flags.append(("-" + letter, value))
                        break
                    flags.append(("-" + letter, ""))
            i += 1
            if name != "env":
                continue
            for flag, value in flags:
                if flag in ("-i", "--ignore-environment"):
                    effects.append(("clear",))
                elif flag in ("-u", "--unset"):
                    effects.append(("unset", value))
                elif flag in ("-S", "--split-string"):
                    try:
                        words[i:i] = shlex.split(value)
                    except ValueError:
                        words[i:i] = value.split()
        if i < len(words):
            i += _LAUNCHER_OPERANDS.get(name, 0)
    return words[i:], effects


def _launched(words: list, env, unset=()):
    """(the words of the program a command runs, past its variable assignments and launchers; the environment it runs
    with, or None for the starting process's own; whether an env launcher or an earlier `unset` of the shell line
    (`unset`, its names) built that environment without the starting process's: env -i, env - or env -u <name>).
    `env` is the start's environment (None: this process's)."""
    program, effects = _through_launchers(words)
    base = None if env is None else {os.fsdecode(k): os.fsdecode(v) for k, v in env.items()}
    return (program,) + _environment([("unset", n) for n in unset] + effects, base)


def _environment(effects: list, base, built: bool = False) -> tuple:
    """(the environment `effects` (_through_launchers) make of `base` (None: this process's, unchanged), whether a
    clear or an unset built it without the starting process's)."""
    changed = {}
    for e in effects:
        if e[0] == "clear":
            base, changed, built = {}, {}, True
        elif e[0] == "unset":
            base = {k: v for k, v in (os.environ if base is None else base).items() if k != e[1]}
            changed.pop(e[1], None)
            built = True
        else:
            changed[e[1]] = e[2]
    if changed:
        base = dict(os.environ if base is None else base, **changed)
    return base, built


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
        try:
            path = w if os.path.isabs(w) else os.path.join(os.fsdecode(cwd) if cwd else os.getcwd(), w)
        except OSError:  # this process's folder is gone: a relative script names nothing
            return False
        return _imports_devkit(path)
    return False


def _imports_devkit(path: str) -> bool:
    """Whether the regular file `path` imports devkit. Opened without blocking and checked as a regular file before it
    is read: a FIFO or a device named as a script would block the start, or never end."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        with os.fdopen(fd, encoding="utf-8", errors="replace") as f:
            fd = None
            return bool(_IMPORTS_DEVKIT.search(f.read(1 << 20)))
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _is_devkit_dev(word: str, env, cwd) -> bool:
    """Whether a `dev` command runs devkit: it resolves, on the PATH it runs with, to a file that imports devkit (a
    copy's bin/dev), not to a test's own stand-in of the command."""
    if os.path.basename(word) != "dev":
        return False
    return any(_imports_devkit(p) for p in _resolve(word, os.environ if env is None else env, cwd))


def _commands(event, args) -> tuple:
    """(the environment of a process start (None: this process's), its folder, the words of each command it runs: its
    own argv, and each command of the line a shell runs with -c; or of os.system's line)."""
    if event == "os.system":
        return None, None, _shell_commands(os.fsdecode(args[0] or ""))
    if event not in EVENTS:
        return None, None, []
    words = _words(args[1])
    env, cwd = (args[3], args[2]) if event == "subprocess.Popen" else (args[2], None)
    commands = [words]
    if words and os.path.basename(words[0]).lower() in _SHELLS:
        flag = next((k for k, w in enumerate(words[1:], 1) if re.fullmatch(r"-[a-z]*c[a-z]*", w)), None)
        if flag is not None and flag + 1 < len(words):
            commands += _shell_commands(words[flag + 1])
    return env, cwd, commands


def devkit_starts(event, args) -> list:
    """(the environment, whether an env launcher built it) of each devkit process a start runs: a copy's bin/dev, a
    `dev` command, or Python running devkit (as its program, through launchers, or in a shell line). The environment is
    None when the process inherits this one's unchanged."""
    if event not in EVENTS:
        return []
    env, cwd, commands = _commands(event, args)
    out, unset = [], []
    for command in commands:
        if command[0] == "unset":  # the shell line's later commands run without those variables
            unset += [w for w in command[1:] if not w.startswith("-")]
            continue
        program, child, built = _launched(command, env, unset)
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
    sys.exit(_npm_guard(sys.argv[2:]) if sys.argv[1:2] == ["--npm-guard"] else _launch(sys.argv[1:]))
