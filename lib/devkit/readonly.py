# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Whether a shell command can write files.

A classifier, not a rule: it reads a command's words and says whether every part of it only reads. The crew gates
use it to attribute a write to the shell call that could have made it, and the call ledger to mark the calls that
write nothing. It fails toward "may write": a program it does not know, a substitution it cannot read, or a redirect
into a file all count as writes.

    write_reason(cmd, cwd) -> str | None          why the command may write, None when it only reads
    program_write_reason(argv, cwd) -> str | None the same for one command's words, prefixes included
    redirect_reads(target, cwd) -> bool           a redirect to /dev/null, a standard stream or a temporary folder
"""
from __future__ import annotations

import itertools
import os
import re
import tempfile
from pathlib import Path

from . import core
from .codexlaunch import app_scratch_allows
from .core import is_temp, real
from .shellparse import (arith_end, arith_hazards, arith_unsafe_names, ASSIGN, close_paren, code_span, crew_kind,
                         dev_args, dev_sets, dev_words, heredoc_bodies, INTERP, interpreter_program, numeric_pipeline,
                         opt_value, Parens, positionals, proc_subst_at, program_source, resolve, runner_fills,
                         shell_args, SHELLS, split_commands, split_heredocs, strip_heredocs, subst, SUBST_WORD, UNKNOWN,
                         unwrap, with_positionals, xargs_command, xargs_parts)

# a line with a word that starts with `#` and a backslash-newline at its end: bash ends the comment at the newline
_COMMENT_CONTINUES = re.compile(r"(?m)^([^\n]*(?<![^ \t\n;&|()])#[^\n]*)\\\n")


def _continued_readings(cmd: str) -> list:
    """The other readings of a command with a backslash-newline: every pair removed, as bash joins the words (`dev \\`
    and `install` on the next line), and the backslash of a line with a `#` word dropped with its newline kept, as
    bash ends a comment at its newline (`ls # x \\` and `dev install`). Each one is judged as well as the command as
    given, so a reading can only add a reason; no grammar is read. [] for a command without a backslash-newline."""
    if "\\\n" not in cmd:
        return []
    joined = cmd.replace("\\\n", "")
    comments = _COMMENT_CONTINUES.sub(lambda m: m.group(1) + "\n", cmd).replace("\\\n", "")
    return list(dict.fromkeys(r for r in (joined, comments) if r != cmd))


def _shell_line(args, name="bash"):
    """The -c line of a shell's arguments (shell_args), or None: a script file or its input is not a line given
    here, and a shell that only parses (`bash -n -c …`) runs none."""
    kind, k, parse_only = shell_args(args, name)
    if kind != "line" or parse_only:
        return None
    return with_positionals(args[k], args[k + 1:])


def _xargs_runs(args) -> list:
    """The command xargs runs with `{}` where the words it reads go: replaced (-I), or appended. [] for an xargs
    with no command, which runs echo."""
    words, rep = xargs_parts(args)
    if not words:
        return []
    return [w.replace(rep, "{}") for w in words] if rep else [*words, "{}"]


def _find_commands(args) -> list:
    """The commands find's -exec, -execdir, -ok and -okdir run, as their words give them (up to `;` or `+`)."""
    out = []
    for k, a in enumerate(args):
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            sub = args[k + 1:]
            out.append(list(sub[:next((j for j, x in enumerate(sub) if x in (";", "+")), len(sub))]))
    return out


def _input_runs(words, depth: int = 0) -> bool:
    """True when these words, which a runner runs with `{}` where its input goes, take their program from the input:
    the wrappers leave no command but a filled-in word fed them (`env {}`, `timeout 5 {}`, `env -S {}`), the program
    or a dev call's command is filled in (`{} install`, `dev {}`), or a shell line or eval's words the input fills in
    run one (`sh -c {}`, `sh -c '"$@"' sh {}`, at any depth the shell rules read)."""
    words = [str(w) for w in words]
    run = unwrap(words)
    if not run:
        return any(runner_fills(w) for w in words)
    if runner_fills(run[0]):
        return True
    dargs = dev_args(run)
    if dargs and runner_fills(dargs[0]):
        return True
    if program_source(run) in ("filled", "none"):  # `xargs sh`, `xargs python3 -c`, `xargs -I{} node`
        return True
    if depth >= 4:  # too deep to read: it may run what its input names (fails closed)
        return True
    name = os.path.basename(run[0])
    line = _shell_line(run[1:], name) if name in SHELLS else " ".join(run[1:]) if name == "eval" else None
    if not line:
        return False
    segs = split_commands(line)
    if segs is None:  # a line the tokenizer cannot read: the walkers refuse it as unreadable, with that reason
        return False
    return any(_input_runs(argv0, depth + 1) or _input_command(unwrap(argv0), depth + 1) for argv0, _ in segs)


def _input_command(argv, depth: int = 0) -> bool:
    """True when this command (its words without prefixes, as unwrap gives them) runs a command its input names:
    its own program is filled in (GNU parallel's appended `{}` after only a wrapper, `parallel env`), an xargs whose
    words take their program from what it reads (_input_runs: `xargs env`, `xargs -I{} {} x`, `xargs sh -c`), or a
    find whose -exec does with the files it finds (`find . -exec {} \\;`, `-exec env {} \\;`). An xargs with no
    command runs echo; a find -exec of wrappers alone (`-exec env \\;`) runs them with nothing added."""
    if not argv:
        return False
    if runner_fills(argv[0]):
        return True
    dargs = dev_args(argv)
    if dargs and runner_fills(dargs[0]):
        return True
    if program_source(argv, whole=True) == "filled":  # parallel's `sh {}`, `python3 {}`: a script the input names
        return True
    name, args = os.path.basename(str(argv[0])), [str(a) for a in argv[1:]]
    if name == "xargs":
        runs = _xargs_runs(args)
        return bool(runs) and _input_runs(runs, depth)
    if name == "find":
        return any(sub and _input_runs(sub, depth) for sub in _find_commands(args))
    return False


_RO_READERS = frozenset({
    "cat", "head", "tail", "wc", "ls", "tree", "file", "stat", "du", "pwd", "basename", "dirname", "realpath",
    "readlink", "which", "type", "echo", "printf", "date", "diff", "cmp", "comm", "cut", "tr", "sort", "uniq",
    "column", "nl", "od", "xxd", "strings", "jq", "grep", "egrep", "fgrep", "rg", "find", "sed", "yq", "shasum",
    "md5", "sha256sum", "cksum", "uname",
    # what the machine runs and is: no option of these writes or runs a program (sysctl is judged in _ro_reader)
    "ps", "pgrep", "sw_vers", "id", "whoami", "printenv", "uptime", "groups", "locale", "getconf", "sysctl", "seq",
    # shell builtins that change nothing outside the shell
    "cd", "pushd", "popd", "true", "false", "test", "[", "[[", "read", "set", "wait", "sleep", "export", "unset",
    "local", "declare", "typeset", "readonly", "for", "select", "case", "in", "esac", "break", "continue",
    # more readers: a column joiner, a manual page and its filter, the open files of processes
    "paste", "col", "man", "lsof"})
# a reader's own flags that write a file or run a program
_RO_FLAGS = {"rg": ("--pre",), "sort": ("-o", "--output"), "tree": ("-o",), "file": ("-C", "--compile"),
             "date": ("-s", "--set"), "yq": ("-i", "--inplace", "-s", "--split-exp")}
# the project's checks, as (program, the words that must follow it, flags that make it write); a program's path and
# version suffix do not matter (/usr/bin/python3, ./mvnw)
_RO_CHECKS = (
    ("pytest", (), ()), ("py.test", (), ()), ("python", ("-m", "pytest"), ()), ("python", ("-m", "unittest"), ()),
    ("python", ("-m", "mypy"), ()), ("python", ("-m", "ruff", "check"), ("--fix", "--unsafe-fixes", "--add-noqa")),
    ("npm", ("test",), ()), ("npm", ("run", "test"), ()), ("pnpm", ("test",), ()), ("pnpm", ("run", "test"), ()),
    ("yarn", ("test",), ()), ("yarn", ("run", "test"), ()), ("vitest", ("run",), ("-u", "--update")),
    ("mvnw", ("test",), ()), ("mvnw", ("verify",), ()), ("gradlew", ("test",), ()), ("go", ("test",), ()),
    ("cargo", ("test",), ()), ("ruff", ("check",), ("--fix", "--unsafe-fixes", "--add-noqa", "--watch")),
    ("mypy", (), ()), ("pyright", (), ()), ("tsc", (), ()), ("eslint", (), ("--fix",)), ("prettier", (), ("--write", "-w")),
    ("shellcheck", (), ()), ("markdownlint", (), ("--fix",)), ("markdownlint-cli2", (), ("--fix",)),
    ("semgrep", (), ("--autofix", "-o", "--output")), ("gitleaks", (), ("-r", "--report-path")),
    ("osv-scanner", (), ("--output",)), ("trivy", (), ("-o", "--output")),
    ("node", ("--test",), ("--test-update-snapshots",)))
_RO_RUNNERS = frozenset({"npx", "bunx", "uvx", "pnpx", "uv", "poetry", "pipenv", "pdm", "hatch", "rye", "pnpm"})
_RO_ENV = re.compile(r"^(?:LC_[A-Z]+|LANG|LANGUAGE|TZ|NO_COLOR|FORCE_COLOR|CLICOLOR(?:_FORCE)?|TERM|COLUMNS|LINES|CI|"
                     r"PYTHONDONTWRITEBYTECODE|PYTHONUNBUFFERED|PYTHONHASHSEED|PYTHONIOENCODING)$")
_RO_GIT_READ = frozenset({"status", "log", "show", "diff", "blame", "annotate", "grep", "ls-files", "ls-tree",
                          "rev-parse", "rev-list", "describe", "shortlog", "cat-file", "merge-base", "show-ref",
                          "for-each-ref", "name-rev", "range-diff", "diff-tree", "diff-files", "diff-index",
                          "check-ignore", "check-attr", "whatchanged", "version", "help"})
_RO_GIT_GLOBAL = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs", "--no-replace-objects"})
_RO_GH_READ = frozenset({"view", "list", "diff", "checks", "status"})
# the work-management reads (overview, standup, report, team, initiatives, plans) change no record: they refresh only
# devkit's own caches of pull requests and agent providers, as `dev status` does (`dev report -o` writes, refused)
_RO_DEV = {"status": None, "grep": None, "find": None, "files": None, "diff": None, "stubs": None, "help": None,
           "version": None, "sec": ("scan",), "test": ("", "run", "plan", "audit", "prove"),
           "pr": ("diff",), "docs": ("check",),
           "plan": ("", "ls", "show"), "overview": None, "standup": None, "report": None, "team": None,
           "initiative": ("", "ls", "show"),
           "lesson": ("", "ls", "list", "show", "add", "recur", "lint", "eval", "review", "scan"),
           "task": ("", "ls", "list", "show"),
           "ci": ("", "status", "ls", "list"),
           "delivery": ("", "show"), "audit": ("", "verify", "show", "stats", "config"), "completion": None,
           "__complete": None}
_SED_ADDR = r"(?:\d+|\$|/(?:[^/\\\n]|\\.)*/I?)"
_SED_PRINT = re.compile(rf"^(?:{_SED_ADDR}(?:\s*,\s*(?:{_SED_ADDR}|[+~]\d+))?)?\s*(?:p|q\d*)$")  # N,+M: GNU


def _family(name: str) -> str:
    """A program's name without its folder, version suffix and case: /usr/bin/python3.12 -> python, ./mvnw -> mvnw."""
    n = os.path.basename(str(name)).casefold()
    return re.sub(r"[0-9.]+$", "", n) if re.match(r"^(?:python|pypy|perl|ruby|node)", n) else n


def _flag_hits(args, flags) -> str:
    """The first of `flags` among `args` (`--output x`, `--output=x`, a short flag in a cluster: `-wo` has -w), or ''."""
    for a in args:
        for f in flags:
            if a == f or (f.startswith("--") and a.startswith(f + "=")) or \
                    (len(f) == 2 and f[0] == "-" and a[:1] == "-" and a[1:2] != "-" and f[1] in a[1:]):
                return f
    return ""


def _ro_env(name: str, value: str = "") -> str | None:
    """Why a read-only lane may not set variable `name` for a command, or None. Pagers, external diff and git
    configuration variables make a reader run a program."""
    if _RO_ENV.match(name) or (name in ("PAGER", "GIT_PAGER", "MANPAGER") and value in ("", "cat")):
        return None
    return f"setting {name} makes the command run another program or act differently"


def _ro_sed(args) -> str | None:
    scripts, files, quiet, i = [], [], False, 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--quiet", "--silent"):
            quiet = True
        elif a in ("-E", "-r", "--regexp-extended", "-s", "--separate", "-u", "--unbuffered", "-z", "--null-data",
                   "--posix", "--debug", "--sandbox"):
            pass
        elif a in ("-e", "--expression"):
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 1
        elif a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch == "n":
                    quiet = True
                elif ch == "e":
                    tail = a[k + 2:]
                    scripts.append(tail or (args[i + 1] if i + 1 < len(args) else ""))
                    i += 0 if tail else 1
                    break
                elif ch not in "Ersuz":
                    return f"{code_span(f'sed -{ch}')} writes files or reads a script file"
        elif a.startswith("-"):
            return f"{code_span(f'sed {a}')} writes files or reads a script file"
        else:
            files.append(a)
        i += 1
    if not scripts and files:
        scripts = files[:1]
    for s in scripts:
        if quiet and all(not p.strip() or _SED_PRINT.match(p.strip()) for p in re.split(r"[;\n]", s)):
            continue
        if not _sed_prints_only(s):
            return (f"the sed script {code_span(s.strip())} can write or run a file (its w, r or e command, or a form "
                    f"devkit does not read); print with sed -n and a line range, or a substitution to standard output")
    return None


_SED_FLAGS = frozenset("gpiIMm0123456789")


def _sed_part(s: str, i: int, delim: str) -> int:
    """The index after the next unescaped `delim` from i (a sed regex or replacement), or -1."""
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == "\n":
            return -1
        if s[i] == delim:
            return i + 1
        i += 1
    return -1


def _sed_address(s: str, i: int) -> int:
    """The index after one sed address at i (a line number, `$`, `/re/`, `\\cREc`, or `first~step`), i when none,
    -1 when it does not end."""
    if i < len(s) and s[i] == "$":
        return i + 1
    m = re.match(r"\d+(?:~\d+)?", s[i:])
    if m:
        return i + m.end()
    if i < len(s) and s[i] == "/":
        j = _sed_part(s, i + 1, "/")
        return j if j < 0 else j + (1 if s[j:j + 1] in ("I", "M") else 0)
    if s[i:i + 1] == "\\" and i + 1 < len(s) and s[i + 1] not in "\n\\":
        return _sed_part(s, i + 2, s[i + 1])
    return i


def _sed_prints_only(script: str) -> bool:
    """True when a sed script only prints what it reads to standard output: addresses, blocks, labels and branches,
    and the commands s (without the w and e flags), y, d, D, p, P, n, N, g, G, h, H, x, l, =, q, Q and z. False for w,
    W, r, R, e, F, v and anything devkit does not read here, so it fails closed."""
    s, i, n = script, 0, len(script)
    while i < n:
        if s[i] in " \t\n;{}!":
            i += 1
            continue
        if s[i] == "#":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        j = _sed_address(s, i)
        if j < 0:
            return False
        if j > i and j < n and s[j] == ",":
            j = _sed_address(s, j + 1)
            if j < 0:
                return False
        while j < n and s[j] in " \t!":
            j += 1
        if j >= n:
            return j > i
        c, i = s[j], j + 1
        if c in "dDpPnNgGhHxlz={}":
            continue
        if c in "qQ":
            while i < n and s[i].isdigit():
                i += 1
            continue
        if c in ":bt":
            while i < n and s[i] not in ";\n}":
                i += 1
            continue
        if c in "sy":
            if i >= n or s[i] in "\\\n ":
                return False
            d = s[i]
            i = _sed_part(s, i + 1, d)
            i = _sed_part(s, i, d) if i > 0 else -1
            if i < 0:
                return False
            if c == "s":
                while i < n and s[i] in _SED_FLAGS:
                    i += 1
            if i < n and s[i] not in " \t;\n}":
                return False  # a w or e flag, or a file name after it
            continue
        return False
    return True


def _ro_git(args) -> str | None:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-C" or a in ("--git-dir", "--work-tree"):
            i += 2
            continue
        if a in _RO_GIT_GLOBAL or a.startswith(("--git-dir=", "--work-tree=")):
            i += 1
            continue
        if a in ("--version", "--help", "-h"):
            return None
        return f"{code_span(f'git {a}')} can make git run a program or change its configuration"
    if i >= len(args):
        return None
    sub, rest = args[i], args[i + 1:]
    if any(r == "--output" or r.startswith(("--output=", "--output-directory")) for r in rest):
        return f"{code_span(f'git {sub} --output')} writes a file"
    pos = [r for r in rest if not r.startswith("-")]
    if sub in _RO_GIT_READ:
        if sub == "grep" and any(r.startswith(("-O", "--open-files-in-pager")) for r in rest):
            return "`git grep -O` runs a program"
        return None
    flags = {r.split("=", 1)[0] for r in rest if r.startswith("-")}
    listing = bool(flags & {"-l", "--list"})
    if sub == "reflog":
        return f"{code_span(f'git reflog {pos[0]}')} changes the reflog" if pos and pos[0] in ("expire", "delete", "drop") \
            else None
    if sub == "branch":
        ok = {"-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose", "-l", "--list", "--show-current",
              "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort", "--format",
              "--column", "--no-column", "--color", "--no-color", "-i", "--ignore-case", "--abbrev"}
        vals = positionals(rest, {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
                                   "--format"})
        return None if flags <= ok and (listing or not vals) else "`git branch` with these arguments changes branches"
    if sub == "tag":
        ok = {"-l", "--list", "--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
              "--format", "--column", "--no-column", "-i", "--ignore-case", "--color", "-v", "--verify"}
        num = {f for f in flags if re.match(r"^-n\d*$", f)}
        vals = positionals(rest, {"--contains", "--no-contains", "--merged", "--no-merged", "--points-at", "--sort",
                                   "--format"})
        lists = listing or bool(flags & {"-v", "--verify"})
        return None if flags - num <= ok and (lists or not vals) else "`git tag` with these arguments changes tags"
    if sub == "remote":
        return None if not pos or pos[0] in ("show", "get-url") else \
            f"{code_span(f'git remote {pos[0]}')} changes the remotes"
    if sub == "stash":
        return None if pos[:1] in (["list"], ["show"]) else "`git stash` without list or show changes the work tree"
    if sub == "worktree":
        return None if pos[:1] == ["list"] else "`git worktree` without list changes the worktrees"
    if sub == "config":
        reads = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
        writes = {"--unset", "--unset-all", "--add", "--replace-all", "--rename-section", "--remove-section",
                  "--edit", "-e"}
        if flags & writes or not (flags & reads or pos[:1] in (["get"], ["list"])):
            return "`git config` without --get or --list changes the configuration"
        return None
    return f"{code_span(f'git {sub}')} is not a read"


def _ro_gh(args) -> str | None:
    pos = [a for a in args if not a.startswith("-")]
    if not pos or pos[0] in ("help", "version") or args[:1] in (["--version"], ["--help"]):
        return None
    if pos[0] in ("pr", "issue", "run", "repo", "release"):
        act = pos[1] if len(pos) > 1 else ""
        return None if act in _RO_GH_READ else f"{code_span(f'gh {pos[0]} {act}')} is not a read"
    if pos[0] == "api":
        attached = next((a[2:] for a in args if a.startswith("-X") and len(a) > 2), "")
        method = (opt_value(args, ("-X", "--method")) or attached or "GET").upper()
        fields = _flag_hits(args, ("-f", "-F", "--field", "--raw-field", "--input"))
        if method != "GET" or fields:
            return f"`gh api` with {'-X ' + method if method != 'GET' else fields} writes through the API"
        return None
    return f"{code_span(f'gh {pos[0]}')} is not a read"


# a read-only lane's test runs: the options each runner may take that load no code and write no file (flags, and
# options with a value, in the spaced or the `=` form); any other option is refused
_RO_TEST_FLAGS = {
    "unittest": frozenset({"-v", "-q", "-f", "-b", "--verbose", "--quiet", "--failfast", "--buffer"}),
    "pytest": frozenset({"-q", "-v", "-x", "-s", "--lf", "--ff", "--co", "--collect-only", "--last-failed",
                         "--failed-first", "--exitfirst", "--quiet", "--verbose", "-l", "--showlocals"}),
    "node": frozenset({"--test-only"})}
_RO_TEST_VALUES = {
    "unittest": frozenset({"-k", "-s", "-p", "-t", "--start-directory", "--pattern", "--top-level-directory"}),
    "pytest": frozenset({"-k", "-m", "--tb", "-r", "--maxfail"}),
    "node": frozenset({"--test-name-pattern", "--test-reporter"})}
_NODE_REPORTERS = frozenset({"spec", "tap", "dot", "junit", "lcov"})


def _ro_tracked_tests(argv, cwd) -> str | None:
    """Why a read-only lane's test run is refused, or None: pytest, python3 -m pytest|unittest and node --test run
    only the repository's tests, never the files a member wrote: no file, folder or module they are given (nor the
    working folder when they are given none, nor a discovery's start and top folders) may lie in a temporary folder,
    where the scratchpads are (_in_temp_folder); pytest's -p, -c, --rootdir, --confcutdir and --basetemp, which
    load or write elsewhere, are refused."""
    fam, rest = _family(argv[0]), [str(a) for a in argv[1:]]
    if fam in ("python", "pypy") and rest[:2] in (["-m", "pytest"], ["-m", "unittest"]):
        kind, rest = rest[1], rest[2:]
    elif fam in ("pytest", "py.test"):
        kind = "pytest"
    elif fam == "node" and rest[:1] == ["--test"]:
        kind, rest = "node", rest[1:]
    else:
        return None
    paths, k = [], 0
    discover = kind == "unittest" and rest[:1] == ["discover"]
    if discover:
        rest = rest[1:]
    flags, values = _RO_TEST_FLAGS[kind], _RO_TEST_VALUES[kind]
    while k < len(rest):
        a = rest[k]
        if a == "--":
            paths += [x.split("::", 1)[0] for x in rest[k + 1:]]
            break
        if not a.startswith("-") or a == "-":
            if discover:
                paths.append(a if len(paths) != 1 else "")  # start folder, pattern, top folder
            else:
                paths.append(a.split("::", 1)[0])
            k += 1
            continue
        flag, eq, val = a.partition("=")
        if flag in values or (not a.startswith("--") and a[:2] in values and len(a) > 2):
            name = flag if flag in values else a[:2]
            val = val if eq else (a[2:] if name == a[:2] and len(a) > 2 and name != flag else
                                  (rest[k + 1] if k + 1 < len(rest) else ""))
            k += 1 if (eq or (name != flag)) else 2
            if discover and name in ("-s", "--start-directory", "-t", "--top-level-directory"):
                paths.append(val)
            elif kind == "node" and name == "--test-reporter" and val not in _NODE_REPORTERS:
                return f"{code_span('--test-reporter=' + val)} loads a reporter module; use spec, tap, dot or junit"
            continue
        if a in flags or (not a.startswith("--") and len(a) > 2 and all(("-" + c) in flags for c in a[1:])):
            k += 1
            continue
        return (f"{code_span(a)} is no option a read-only lane's test run may take: it may load code or write a file; "
                f"run the repository's tests with the plain options (-v, -q, -x, -k, …), or ask the tech lead to "
                f"run it (`dev crew ask \"…\"`)")
    paths = [x for x in paths if x] or ["."]
    for x in paths:
        cand = [x]
        if kind == "unittest" and not discover and "/" not in x and not x.endswith(".py") and x != ".":
            cand = [x.replace(".", "/") + ".py", x.replace(".", "/")]  # a module: its file or its package
        if any(_in_temp_folder(resolve(c, cwd)) and not _in_work_tree(resolve(c, cwd)) for c in cand):
            return (f"{code_span(x)} is in a temporary or scratch folder, where a member writes its own files: a "
                    f"read-only lane runs the repository's tests, never a program of its own; ask the tech lead to "
                    f"run it (`dev crew ask \"…\"`)")
    return None


def _in_work_tree(path) -> bool:
    """True when `path` (or the folder it would be in) lies in a git work tree: a repository's files, not a scratch
    folder (a session scratchpad is none, and a read-only lane cannot make one)."""
    p = Path(str(path))
    while not p.exists() and p != p.parent:
        p = p.parent
    folder = p if p.is_dir() else p.parent
    return bool(core.gitout(str(folder), "rev-parse", "--show-toplevel").strip())


def _in_temp_folder(path) -> bool:
    """True when `path` lies in a temporary folder (/tmp, the system's per-user one), where the session scratchpads
    are: the only place a read-only lane writes."""
    try:
        real = os.path.realpath(str(path))
    except (OSError, ValueError):
        return True
    roots = {"/tmp", "/private/tmp", "/var/folders", "/private/var/folders", os.path.realpath(tempfile.gettempdir())}
    return any(real == r or real.startswith(r.rstrip("/") + "/") for r in roots)


def _ro_inline(argv, cwd, bodies):
    """For an interpreter: '' when it runs a program typed in the command (-c, -e, -p, `-` or its input), which counts
    as a possible write, since the classifier does not read the program; None when it runs a file or a module, which
    the other rules judge."""
    fam = _family(argv[0])
    if fam not in ("python", "pypy", "node"):
        return None
    inline = False
    for a in argv[1:]:
        if a in ("-c", "-e", "--eval", "-p", "--print", "-") or (a.startswith("-") and not a.startswith("--") and
                                                                  a[-1:] in ("c", "e", "p")):
            inline = True
            break
        if a in ("-m",) or not a.startswith("-"):
            return None
    if not inline and not bodies:
        return None
    return "" if interpreter_program(argv, cwd, bodies)[0] else None


# a shell variable a read-only lane may set alone (`S=/path; ls $S`): a lowercase name, or one capital letter with
# digits, which no program reads from its environment; PATH, PAGER, IFS and the other known names stay refused
_SHELL_VAR = re.compile(r"^(?!(?:path|fpath|cdpath|manpath|module_path|mailpath|histchars|fignore|psvar)$)"
                        r"(?:[a-z_][a-z0-9_]*|[A-Z][0-9]*|"  # zsh ties path to PATH, fpath to FPATH, …
                        # a short capital name (S, SP, OUT, LOG2) is no variable a program reads, unless the shell or
                        # the system already exports it
                        r"(?!(?:PATH|HOME|IFS|ENV|PS[0-9]|USER|PWD|TERM|LANG|MAIL|LESS|TMP|TEMP|BASH|ZSH|HOST|CI|TZ)$)"
                        r"[A-Z][A-Z0-9_]{1,3})$")


def _version_only(name: str, args) -> bool:
    """`python3 --version`, `bash --version`, `node --version`, `python3 -V`: an interpreter or shell asked only for
    its version, which runs no program."""
    return list(args) == ["--version"] or (_family(name) in ("python", "pypy") and list(args) in (["-V"], ["-VV"]))


_AWK_RUNS = re.compile(r"\bsystem\b|\bgetline\b|@(?:load|include|namespace)\b|\bclose\b|\bfflush\b")
# what may come right before a `/` that starts a regex rather than a division: an operator, an opening bracket, a
# statement's start, or a keyword that takes an expression (the cut awk's own grammar makes)
_AWK_REGEX_AFTER = frozenset("(,~!{};&|=?:+-*%^<>\n")
_AWK_REGEX_KEYWORDS = frozenset({"print", "printf", "return", "in", "case", "do", "else"})


def _awk_writes(prog: str) -> bool:
    """True when an awk program writes to a file or a command: a `|` that is no `||` (a pipe to a command, or one
    getline reads), or a `>` at the top level of a print or printf statement (`print $1 > "f"`, `printf(...) >> f`),
    where awk reads it as a redirection; awk reads the statement on past a newline after `,`, `&&`, `||` or a
    backslash. A `>` anywhere else (`NR>=2050`, `print (a > b)`) compares. Strings, regex
    literals and comments are skipped; a `/` starts a regex where awk's grammar reads one (after an operator, a
    statement's start, a keyword that takes an expression, or the condition of if, while or for), else it divides. A
    string or regex that does not end counts as a write, so a program devkit cannot read fails closed."""
    i, n = 0, len(prog)
    printing, at, prev, word, opened = False, 0, "\n", "", []  # opened: per open bracket, whether it is a condition
    while i < n:
        c = prog[i]
        if c in "\"/" and (c == '"' or prev in _AWK_REGEX_AFTER or word in _AWK_REGEX_KEYWORDS):
            j = i + 1
            while j < n and prog[j] != c:
                if prog[j] == "\n":
                    return True
                if c == "/" and prog[j] == "[":  # a bracket expression may hold a `/`: `/[/]/`
                    k = j + (2 if prog.startswith("[^", j) else 1)
                    k = prog.find("]", k + 1 if prog[k:k + 1] == "]" else k)
                    if k < 0:
                        return True
                    j = k + 1
                    continue
                j += 2 if prog[j] == "\\" else 1
            if j >= n:
                return True
            i, prev, word = j + 1, "x", ""
            continue
        if c.isalnum() or c == "_":
            j = i
            while j < n and (prog[j].isalnum() or prog[j] == "_"):
                j += 1
            word = prog[i:j]
            if word in ("print", "printf"):
                printing, at = True, len(opened)
            i, prev = j, "x"
            continue
        if c == "#":  # a comment, to the end of its line
            j = prog.find("\n", i)
            i = n if j < 0 else j
            continue
        if prog.startswith(("++", "--"), i):  # `i++ / 2` divides: what follows ++ or -- is no regex
            i, prev, word = i + 2, "x", ""
            continue
        if c == "|":
            if prog.startswith("||", i):
                i, prev, word = i + 2, "|", ""
                continue
            return True
        if c == ">" and printing and len(opened) <= at:
            return True
        if c in "([":
            opened.append(c == "(" and word in ("if", "while", "for"))
        elif c in ")]":
            condition = opened.pop() if opened else False
            if condition:  # `if (a) /re/`: a statement starts after the condition
                i, prev, word = i + 1, ";", ""
                continue
        elif c in ";\n{}":  # a `;` of a for header ends no statement, nor does a newline after `,`, `&&`, `||` or `\\`
            printing = printing and (len(opened) > at if c == ";" else c == "\n" and prev in ",&|\\")
        if not c.isspace() or (c == "\n" and not printing):  # `print a,` and blank lines: still continuing
            prev, word = c, ""
        i += 1
    return False


def _ro_awk(args) -> str | None:
    """Why an awk call of a read-only lane is refused, or None: its program (inline, `-e` or `--source`) may only read
    its input and print to standard output: no print to a file or a command (_awk_writes), no system(), getline,
    @load or @include; no program file and no option that loads one. A comparison with `>` reads."""
    progs, i, inline = [], 0, True
    while i < len(args):
        a = args[i]
        if a in ("-F", "-v", "--field-separator", "--assign"):
            i += 2
            continue
        if a.startswith(("-F", "-v", "--field-separator=", "--assign=")):
            i += 1
            continue
        if a in ("-e", "--source"):
            progs.append(args[i + 1] if i + 1 < len(args) else "")
            inline = False
            i += 2
            continue
        if a == "--":
            i += 1
            break
        if a.startswith("-"):
            return f"{code_span(f'awk {a}')} reads a program file, loads an extension or changes how awk runs"
        break
    if inline:
        if i >= len(args):
            return "`awk` without a program"
        progs.append(args[i])
    for p in progs:
        if _AWK_RUNS.search(p) or _awk_writes(p):
            return ("the awk program writes to a file or a command, or runs one (`print > file`, `|`, system(), "
                    "getline); print to standard output only")
    return None


def _mask_substitutions(s: str):
    """(the command with each `$(…)`, backtick and `<(…)`/`>(…)`/`=(…)` the shell runs replaced by a placeholder
    word, the commands inside them), or None for a substitution that does not end, or an arithmetic `$((…))` that
    reads a name whose value may be code (arith_unsafe_names: the shells evaluate a name's value as an expression).
    Arithmetic on numbers is the word 0, and the substitutions inside it are commands of their own. Single quotes and
    comments are text."""
    out, inner, i, n, q, parens = [], [], 0, len(s), None, Parens(s)
    while i < n:
        c = s[i]
        if q == "'":
            q = None if c == "'" else q
            out.append(c)
            i += 1
            continue
        if c == "\\":
            out.append(s[i:i + 2])
            i += 2
            continue
        if s.startswith("$((", i) and (end := arith_end(s, i + 3)) is not None:
            body = s[i + 3:end]
            if arith_unsafe_names(body, s):
                return None
            inner += subst(body, False)
            out.append("0")
            parens.close(end + 1, "w")
            i = end + 2
            continue  # `$((date)|wc -c)` is no arithmetic: read below as the substitution it is
        if s.startswith("$(", i) or (q is None and proc_subst_at(s, i)):
            j = close_paren(s, i + 2)
            if j < 0:
                return None
            inner.append(s[i + 2:j - 1])
            # a substitution that can only print a number (a line number for a sed address) stands as one
            out.append("1" if numeric_pipeline(s[i + 2:j - 1]) else SUBST_WORD)
            parens.close(j - 1, "w")
            i = j
            continue
        if c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            if j >= n:
                return None
            inner.append(s[i + 1:j])
            out.append(SUBST_WORD)
            i = j + 1
            continue
        if q is None and c == "'":
            q = "'"
        elif c == '"':
            q = None if q == '"' else '"'
        elif q is None and c == "#" and parens.comment(i, False):
            j = s.find("\n", i)
            j = n if j < 0 else j
            out.append(s[i:j])
            i = j
            continue
        elif q is None and c == "(":
            parens.open(i)
        elif q is None and c == ")":
            parens.close(i)
        out.append(c)
        i += 1
    return "".join(out), inner


# `dev impeccable`: the engine's verbs that only read, with what each may take: verb -> (flags that take a value: the
# kind of value, flags alone, whether local paths may follow)
_IMPECCABLE_READS = {
    "context": ({"--target": "path"}, frozenset(), False),
    "detect": ({"--scope": "names"}, frozenset({"--json", "--quiet", "--list-rules", "--no-config", "--no-advisory",
                                                "--no-inline-ignores", "--no-design-system", "--help"}), True),
    "doctor": ({"--target": "path"}, frozenset({"--json", "--help"}), False),
}
# the helper forms that write nothing: verb -> its sub-verbs
_IMPECCABLE_READ_HELPERS = {"surface-brief": ("read",), "critique-storage": ("slug", "trend")}
# a local path: one that starts ./, ../, / (never //) or ~/, `.` or `..`, or one whose first part has only ASCII
# letters, digits, _ and - and is not a host on its own (localhost, or a number such as 2130706433 or 0x7f000001);
# never with a colon (a scheme, a port, an IPv6 address)
_LOCAL_PATH = re.compile(r"(?:\.\.?/|/(?!/)|~/|\.\.?\Z"
                         r"|(?!(?:localhost|[0-9]+|0x[0-9a-f]+)(?:/|\Z))[A-Za-z0-9_-]+(?:/|\Z))", re.I)
_NAMES = re.compile(r"[a-z]+(?:,[a-z]+)*\Z")  # detect's --scope: design domains, comma-separated


def _impeccable_reason(args) -> str | None:
    """Why `dev impeccable` with `args`, the words after it as typed, may write, or None for a read verb with its
    reading flags on local paths. dev drops a first `--` and passes the rest to the engine as they are."""
    args = [str(w) for w in args]
    args = args[1:] if args[:1] == ["--"] else args
    if not args:
        return "`dev impeccable` with no verb"
    verb, rest = args[0], args[1:]
    if rest[:1] and rest[0] in _IMPECCABLE_READ_HELPERS.get(verb, ()):
        return None
    if verb not in _IMPECCABLE_READS:
        return f"{code_span('dev impeccable ' + verb)} writes files or starts a session"
    takes, alone, paths = _IMPECCABLE_READS[verb]
    i = 0
    while i < len(rest):
        a = rest[i]
        flag, eq, value = a.partition("=")
        if flag in takes:
            kind = takes[flag]
            value = value if eq else (rest[i + 1] if i + 1 < len(rest) else "")
            i += 1 if eq else 2
        else:
            i += 1
            if a in alone:
                continue
            if a.startswith("-") or not paths:
                return f"{code_span(f'dev impeccable {verb} {a}')} is not one of its reading flags or a local path"
            kind, value = "path", a
        if kind == "names":
            if not _NAMES.match(value):
                return f"{code_span(f'dev impeccable {verb} {a}')} gives {flag} something other than design domains"
        elif ":" in value or not _LOCAL_PATH.match(value):
            return f"{code_span(f'dev impeccable {verb} {a}')} may name a URL or a host, which the engine fetches"
    return None


def _dev_reason(args) -> str | None:
    """Why `dev` with `args` may write, or None: a crew call (`dev progress`, `dev crew`), a wait, or one of devkit's
    reading commands (_RO_DEV) with none of its writing options."""
    if crew_kind(args):
        return None
    pos = dev_words(args)  # options take values (--search add): the subcommand is the next word
    sub = pos[0] if pos else ""
    if sub == "impeccable":  # its words go to the engine as they are
        return _impeccable_reason(args[args.index(sub) + 1:])
    if sub == "watch":  # the caller's own wait
        return None
    if sub in _RO_DEV and (_RO_DEV[sub] is None or (pos[1] if len(pos) > 1 else "") in _RO_DEV[sub]):
        if sub == "docs" and dev_sets(args, "--fix"):
            return "`dev docs check --fix` changes documentation files"
        if sub == "ci" and dev_sets(args, "--run", "--resume"):
            return "`dev ci --run` runs the pipeline's build and package steps"
        if sub == "audit" and dev_sets(args, "--publish", "-o", "--out"):
            return "`dev audit --publish` commits the audit trail to the handoff repository, and `-o` writes a file"
        if sub == "report" and dev_sets(args, "-o", "--out"):
            return "`dev report -o` writes a file"
        return None
    return f"{code_span('dev ' + (' '.join(pos[:2]) or '…'))} changes devkit's records or the repository"


def check_refusal(argv) -> str | None:
    """None when `argv` is one of the project's checks (_RO_CHECKS) run without a flag that makes it write; else why
    not ('' when it is not a check at all)."""
    fam, rest = _family(argv[0]), argv[1:]
    for prog, words, writes in _RO_CHECKS:
        if fam != prog or [_family(w) if k == 0 else w for k, w in enumerate(rest[:len(words)])] != list(words):
            continue
        f = _flag_hits(rest[len(words):], writes)
        if f:
            return f"{code_span(' '.join([prog, *words, f]))} writes files"
        if prog == "tsc" and "--noEmit" not in rest:
            return "`tsc` without --noEmit writes the compiled files"
        if prog == "prettier" and not {"--check", "-c", "--list-different", "-l"} & set(rest):
            return "`prettier` without --check writes the formatted files"
        return None
    return ""


def _input_command_reason(argv) -> str | None:
    """Why a command (a segment's words without its prefixes) may write because its input names what it runs: an
    xargs, find -exec or parallel whose program comes from what it reads (_input_command), or `xargs dev` and `xargs
    dev ext`, whose devkit command does; None otherwise."""
    argv = list(argv or ())
    if argv and os.path.basename(str(argv[0])) == "xargs":
        dargs = dev_args(unwrap(xargs_command([str(a) for a in argv[1:]])))
        if dargs is not None and (not dargs or dargs == ["ext"]):
            return "`xargs dev` takes devkit's command from its input"
    return "it runs a command its input names" if _input_command(argv) else None


def _reader_reason(name, args, cwd) -> str | None:
    """Why a program named `name` with `args` may write, or None: a reader with none of its writing flags, a read of
    git or gh, or an xargs or find -exec that runs only such commands."""
    if name not in _RO_READERS and name not in ("git", "gh", "xargs"):
        return f"{code_span(name)} is not on the read-only allowlist"
    if name == "sysctl" and any("=" in a or a in ("-w", "-f", "-p", "--write", "--load", "--system") or
                                (a[:1] == "-" and a[1:2] != "-" and set(a[1:]) & set("wfp")) for a in args):
        return "`sysctl` with a value, -w, -f or -p changes the kernel's settings"
    f = _flag_hits(args, _RO_FLAGS.get(name, ()))
    if f:
        return f"{code_span(f'{name} {f}')} writes a file or runs a program"
    if name in ("export", "local", "declare", "typeset", "readonly"):
        for a in args:
            if not a.startswith(("-", "+")):
                why = _ro_env(*(a.split("=", 1) + [""])[:2])
                if why:
                    return why
    if name == "sed":
        return _ro_sed(args)
    if name == "git":
        return _ro_git(args)
    if name == "gh":
        return _ro_gh(args)
    if name in ("uniq", "xxd") and len([a for a in args if not a.startswith("-")]) > 1:
        return f"{code_span(name)} writes its second file argument"
    if name == "find":
        for k, a in enumerate(args):
            if a in ("-delete", "-fprint", "-fprint0", "-fprintf", "-fls"):
                return f"{code_span(f'find {a}')} deletes or writes files"
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                inner = args[k + 1:]
                inner = inner[:next((j for j, x in enumerate(inner) if x in (";", "+")), len(inner))]
                why = _program_reason(inner, cwd) if inner else None
                if why:
                    return why
    if name == "xargs":
        cmd = xargs_command(args)
        return _program_reason(cmd, cwd) if cmd else None
    return None


def _program_reason(argv0, cwd, bodies=()) -> str | None:
    """Why one command (its words, prefixes included) may write, or None. `bodies`: the command's heredoc bodies, an
    inline program's input."""
    argv = unwrap(argv0)
    pre = argv0[:len(argv0) - len(argv)]
    runner = False
    for k, a in enumerate(pre):
        n = os.path.basename(a)
        if ASSIGN.match(a):
            if not argv and _SHELL_VAR.match(a.split("=", 1)[0]):
                continue  # `S=/path` alone sets a shell variable no program reads (not exported, no known name)
            why = _ro_env(*a.split("=", 1))
            if why:
                return why
        elif n == "sudo":
            return "`sudo` runs the command as another user"
        elif n == "env" and any(x.startswith("-") for x in pre[k + 1:k + 2]):
            return "`env` with options changes the command's environment"
        elif n == "command" and any(re.fullmatch(r"-[pvV]*[vV][pvV]*", x) for x in
                                    itertools.takewhile(lambda x: x.startswith("-") and x != "--", pre[k + 1:])):
            return None  # `command -v gh dev`, `command -V x`: it only looks the names up and runs none of them
        elif n in _RO_RUNNERS:
            runner = True
            if any(str(x).startswith(("--with", "--extra-index", "--index", "--find-links", "--script", "--env-file"))
                   for x in pre[k + 1:]):
                return f"{code_span(n)} with an option that adds code or packages to the run"
    if not argv:
        return None
    why = _input_command_reason(argv)
    if why:
        return why
    name, args = os.path.basename(argv[0]), argv[1:]
    if argv[0] == SUBST_WORD:
        return "a program named by a command substitution"
    if argv[0][:1] in ("$", "`") or name.startswith("#"):
        return None if name.startswith("#") else "a program named by a variable"
    dev = dev_args(argv)
    if dev is not None:
        return _dev_reason(dev)
    why = check_refusal(argv)
    if why is None:
        return _ro_tracked_tests(argv, cwd)
    if not why and not runner and INTERP.match(name) and _ro_inline(argv, cwd, bodies) is not None:
        return "an interpreter runs a program typed in the command or fed to it"
    if not why and not runner and (INTERP.match(name) or name in SHELLS) and _version_only(name, args):
        return None
    if not why and not runner and name in SHELLS and shell_args(args, name)[2]:
        return None  # `bash -n x.sh`, `zsh -n -c '…'`: it reads and parses, and runs nothing
    if not why and not runner and _family(name) in ("awk", "gawk", "mawk", "nawk"):
        return _ro_awk(args)
    return why or (f"{code_span(name)} is not on the read-only allowlist" if runner or INTERP.match(name) or
                   name in SHELLS or name in ("eval", "source", ".") else _reader_reason(name, args, cwd))


def program_write_reason(argv, cwd) -> str | None:
    """Why one command, as its words (prefixes included), may write a file, or None when it only reads: a reader, a
    read of git, gh or dev, or one of the project's checks."""
    return _program_reason(list(argv), cwd)


def redirect_reads(target, cwd) -> bool:
    """True when a redirect to `target` leaves the files a command could care about alone: /dev/null and the standard
    streams, a registered Codex scratch folder, or a temporary folder, which holds no repository file."""
    if target in ("/dev/null", "/dev/stdout", "/dev/stderr", "-"):
        return True
    p = resolve(target, cwd)
    if p == UNKNOWN:
        return False
    return app_scratch_allows(p) or is_temp(p)


def _write_reason(cmd: str, cwd, depth: int) -> str | None:
    if not cmd or not str(cmd).strip():
        return None
    readings = _continued_readings(cmd)
    if readings:  # a backslash-newline: the readings shells use (joined; a comment ended at its newline), each judged
        return next((why for why in (_write_reason(r, cwd, depth) for r in readings) if why), None)
    hazards = arith_hazards(cmd)
    if hazards:
        return (f"{code_span(hazards[0])} is read as an arithmetic expression ((( )), let, [[ -eq ]], a subscript), "
                f"and its value may hold code the shell runs")
    masked = _mask_substitutions(strip_heredocs(cmd)) if depth < 3 else None
    if masked is None:
        return "`$((…))` or substitutions nested deeper than three levels run code that cannot be read"
    text, inner = masked
    inner += [t for body, expands in split_heredocs(cmd)[1] if expands for t in subst(body, False)]
    for sub in inner:
        why = _write_reason(sub, cwd, depth + 1)
        if why:
            return why
    segs = split_commands(text, stripped=True)
    if segs is None:
        return "the command does not parse, so what it writes cannot be told"
    here = str(cwd or os.getcwd())
    bodies = heredoc_bodies(cmd)
    for argv0, redirs in segs:
        argv0, extra = list(argv0), []
        while any(t in (">|", ">>|") for t in argv0):  # `>|` reaches argv as a token; its target follows it
            k = next(j for j, t in enumerate(argv0) if t in (">|", ">>|"))
            extra += argv0[k + 1:k + 2] or [""]
            del argv0[k:k + 2]
        bad = next((r for r in list(redirs) + extra if not redirect_reads(r, here)), None)
        if bad is not None:
            return f"the redirect to {code_span(bad)} writes a file"
        argv = unwrap(argv0)
        if argv and os.path.basename(argv[0]) in ("cd", "pushd") and len(argv) > 1 and argv[1] != "-":
            here = str(resolve(argv[1], here))
        why = _program_reason(argv0, here, bodies)
        if why:
            return why
    return None


def write_reason(cmd: str, cwd) -> str | None:
    """Why the shell command `cmd`, run in `cwd`, may write a file, or None when every part of it only reads: it
    parses, redirects only to /dev/null, the standard streams or a temporary folder, and each program it runs is a
    reader, a read of git, gh or dev, or one of the project's checks. A command substitution (`$(…)`, backticks) or a
    process substitution (`<(…)`, `>(…)`) passes when the command inside it does, by the same rules; never as the
    program a command runs, and never in arithmetic."""
    return _write_reason(cmd, cwd, 0)
