# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Read shell command text as a POSIX shell splits it.

The words, operators, redirects, heredocs and substitutions of a command line; the prefixes, wrappers and runners
that start another program (env, sudo, xargs, find -exec, parallel, a package manager's run, tmux and screen); the
programs an interpreter's code starts; and the devkit calls a command makes. It decides nothing and refuses nothing:
the call ledger, lane routing, the crew gates' write attribution and devkit's own git commands read commands through
it.

    split_commands(cmd) -> [(argv, redirects)] | None     the simple commands of a line, None when it does not parse
    shell_calls(cmd, cwd) -> [(argv, code)] | None        every command a line runs, nested shells and code included
    unwrap(argv) -> argv                                  the program a command runs, without its prefixes
    dev_args(argv) -> [str] | None                        the words after `dev`, None when argv is no devkit call
    crew_kind(args) -> "lane" | "team" | "sync" | "read" | ""
    resolve(path, cwd) -> Path                            a word as a path, UNKNOWN when the shell decides it later
"""
from __future__ import annotations

import filecmp
import functools
import json
import os
import re
import shlex
import shutil
from pathlib import Path

from . import core
from .core import DEV, is_temp, is_under, real

PREFIX_CMDS = {"sudo", "doas", "env", "command", "nohup", "time", "exec", "builtin", "nice", "caffeinate", "stdbuf",
               "ionice", "setsid", "noglob", "nocorrect"}
# zsh's precommand modifier `-` (it runs the next word with a dash before its argv[0]): a prefix only where a command
# starts, never in PREFIX_CMDS, where it would end env's own options (`env - cmd` empties the environment)
_DASH_PRECOMMAND = "-"
# a wrapper's flags that take a value, so `nice -n 5 rm`, `env -u X rm`, `sudo -u me rm` and `exec -a x rm` run rm:
# the value is skipped with the flag
_PREFIX_VALUE_FLAGS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U", "-T", "-R", "--user", "--group", "--host",
             "--prompt", "--close-from", "--chdir", "--role", "--type", "--other-user", "--command-timeout", "--chroot"},
    "doas": {"-u", "-C"}, "env": {"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-P"},
    "nice": {"-n", "--adjustment"}, "exec": {"-a"}, "time": {"-f", "--format", "-o", "--output"},
    "caffeinate": {"-t", "-w"}, "ionice": {"-c", "-n", "-p", "--class", "--classdata", "--pid"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"}}
# shell keywords that start a segment before the program it runs: `if x; then rm -rf ~; fi`, `{ cmd; }`, `! cmd`
RESERVED = frozenset({"!", "{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "case",
                       "esac", "select", "coproc"})


def code_span(value) -> str:
    """`value` quoted as one Markdown code span that nothing inside it can end: a fence one backtick longer than its
    longest run of backticks, and a space inside each fence when it starts or ends with a backtick, or with a space at
    both ends (Markdown takes one off each end). An empty value is a span of one space. A reader sees the value whole,
    and the audit trail reads it as one quote and masks it."""
    s = str(value)
    if not s:
        return "` `"
    fence = "`" * (max(map(len, re.findall(r"`+", s)), default=0) + 1)
    pad = " " if "`" in (s[0], s[-1]) or (s[0] == s[-1] == " " and s.strip(" ")) else ""
    return f"{fence}{pad}{s}{pad}{fence}"


UNKNOWN = Path("/__devkit_unknown__")


_NUL = chr(0)


def _c_path(p) -> str:
    """A path as the system reads it, a C string: up to its first NUL. os.path raises on a NUL, and a reader that
    raises lets the call through."""
    return str(p).split(_NUL, 1)[0]


def _path_text(path):
    """A shell word's path text with its variables and `~` expanded, or None when shell state the hook cannot see
    decides it."""
    p = os.path.expanduser(os.path.expandvars(_c_path(path)))
    return None if "$" in p or "`" in p else p


def resolve(path, cwd) -> Path:
    """Absolute real path, or UNKNOWN when it depends on shell state the hook cannot see ($VAR, $(…), `…`). A NUL ends
    the path (_c_path), and the folder's."""
    p = _path_text(path)
    if p is None:
        return UNKNOWN
    if not os.path.isabs(p):
        if cwd is None or str(cwd) == str(UNKNOWN):
            return UNKNOWN
        p = os.path.join(_c_path(cwd), p)
    return Path(real(p))


def _newlines_to_semicolons(cmd: str, op: str = ";") -> str:
    """`cmd` with each newline outside quotes made a `;` (`op` for tmux_list_refusal). A backslash outside single
    quotes escapes the next character, as the tokenizer (shlex, posix) reads it, so `echo \\'` opens no quote that
    would hide the next line. `#` is an ordinary character: the words after one are read as more
    commands, which can only add a refusal, and a quote in a comment makes a line devkit cannot read
    (UNREADABLE_REASON). A backslash-newline is read as given here; check_bash reads it as bash does too."""
    out, q, i, n = [], None, 0, len(cmd)
    while i < n:
        ch = cmd[i]
        if ch == "\\" and q != "'" and i + 1 < n and cmd[i + 1] != "\n":
            out.append(cmd[i:i + 2])
            i += 2
            continue
        if q:
            if ch == q:
                q = None
        elif ch in ("'", '"'):
            q = ch
        elif ch == "\n":
            ch = op
        elif ch == "=" and proc_subst_at(cmd, i):  # zsh's `=(…)` is read as the `<(…)` it runs as
            ch = "<"
        out.append(ch)
        i += 1
    return "".join(out)


class _HeredocMatch:
    """What _HEREDOC_AT.match finds: group(0) the operator and its delimiter word as written; group(1) the delimiter
    when any part of the word is quoted (the body is text), group(3) when none is (the shell expands the body)."""

    def __init__(self, text, start, end, delim, quoted):
        self._text, self._start, self._end, self._delim, self._quoted = text, start, end, delim, quoted

    def group(self, k=0):
        if k == 0:
            return self._text
        if k == 1:
            return self._delim if self._quoted else None
        return None if k == 2 or self._quoted else self._delim

    def start(self, k=0):
        return self._start

    def end(self, k=0):
        return self._end


class _HeredocAt:
    """The heredoc operator at s[i] and its delimiter word, read as the shells read it: the whole word up to a blank
    or an operator, with its quotes removed (`E\\OF`, `E''OF`, `"E"OF` and `E'OF'` all end at a line `EOF`), quoted
    when any part of it is."""

    _OP = re.compile(r"<<-?[ \t]*")

    def match(self, s, i=0):
        m = self._OP.match(s, i)
        if not m:
            return None
        j, n, word, quoted = m.end(), len(s), [], False
        while j < n and s[j] not in " \t\n;&|<>()":
            c = s[j]
            if c in "'\"":
                k = s.find(c, j + 1)
                if k < 0 or "\n" in s[j + 1:k]:
                    return None
                word.append(s[j + 1:k])
                quoted, j = True, k + 1
            elif c == "\\":
                if j + 1 >= n or s[j + 1] == "\n":
                    return None
                word.append(s[j + 1])
                quoted, j = True, j + 2
            elif c in "$`":
                return None  # a delimiter the shell would expand: no heredoc devkit can read
            else:
                word.append(c)
                j += 1
        delim = "".join(word)
        if not delim:
            return None
        return _HeredocMatch(s[i:j], i, j, delim, quoted)


_HEREDOC_AT = _HeredocAt()

# `#` starts a comment only at the start of a word, as bash and zsh read it: after a blank, a newline, an operator or
# a redirection. A `)` that ends a substitution ends no word, so `x$(true)#c` and `<(true)#<(…)` are one word each,
# where `(echo a)#c` ends at the `#`.
_COMMENT_AFTER = frozenset(" \t\n;&|()<>")
_FUNCTION_PARENS = re.compile(r"\([ \t]*\)")


def _paren_kind(s: str, k: int) -> str:
    """What the `(` at s[k] opens: 'w' a substitution, part of the word around it (`$(`, `$((`, `<(`, `>(`, zsh's
    `=(` at the start of a word); 'o' an operator (a subshell, `((`, a function's `()`); '?' a parenthesis the shells
    read differently (bash reads `a=(1 2)#c` as one word, zsh ends it at the `#`; extglob `@(…)`; glob qualifiers)."""
    prev = s[k - 1] if k else ""
    if prev in ("$", "<", ">") or (prev == "=" and (k < 2 or s[k - 2] in _COMMENT_AFTER)):
        return "w"
    if not prev or prev in _COMMENT_AFTER or _FUNCTION_PARENS.match(s, k):
        return "o"
    return "?"


def proc_subst_at(s: str, i: int) -> bool:
    """True when s[i] opens a process substitution: `<(…)`, `>(…)`, or zsh's `=(…)` at the start of a word, which runs
    its command as `<(…)` does and stands for a temporary file that holds what it wrote (`. =(echo …)` runs it)."""
    return s[i + 1:i + 2] == "(" and (s[i] in "<>" or (s[i] == "=" and (i == 0 or s[i - 1] in _COMMENT_AFTER)))


class Parens:
    """The parentheses a scanner of `s` has opened and closed, to tell where a `#` starts a comment."""

    def __init__(self, s: str):
        self.s, self.opened, self.closed = s, [], {}

    def open(self, k: int):
        self.opened.append(_paren_kind(self.s, k))

    def close(self, k: int, kind: str = ""):
        """The `)` at s[k] closes the last parenthesis opened (`kind`: one the scanner skipped whole)."""
        self.closed[k] = kind or (self.opened.pop() if self.opened else "o")

    def comment(self, i: int, lean: bool) -> bool:
        """True when the `#` at s[i] starts a comment. `lean`: the reading of a parenthesis the shells read
        differently ('?'): a comment (True) where reading it as words could hide a line (a heredoc's `<<WORD` after
        it), words (False) where reading it as a comment could (a substitution after it)."""
        s = self.s
        if s[i] != "#" or (i and s[i - 1] not in _COMMENT_AFTER):
            return False
        kind = self.closed.get(i - 1, "o") if i and s[i - 1] == ")" else "o"
        return kind == "o" or (kind == "?" and lean)


def _braced_end(s: str, i: int) -> int:
    """The index after the `}` or `]` that ends the `${…}` or `$[…]` at s[i], or len(s) when none does. Its quotes,
    nested braces and substitutions are read as the shell reads them."""
    closers, j, n = [{"{": "}", "[": "]"}[s[i + 1]]], i + 2, len(s)
    while j < n and closers:
        d = s[j]
        if d == "\\":
            j += 2
            continue
        if d == "'":
            k = s.find("'", j + 1)
            j = n if k < 0 else k + 1
            continue
        if d == '"':
            k = j + 1
            while k < n and s[k] != '"':
                k += 2 if s[k] == "\\" else 1
            j = k + 1
            continue
        if d in "{[(":
            closers.append({"{": "}", "[": "]", "(": ")"}[d])
        elif d == closers[-1]:
            closers.pop()
        j += 1
    return min(j, n)


# a substitution that prints the shell's folder: a `$(pwd)/x` the reading of a command reads as `./x`, below the
# folder the command runs in (_pwd_is_here)
_PWD_FORM = r"(?:\$\([ \t]*pwd(?:[ \t]+-[LP]+)*[ \t]*\)|`[ \t]*pwd(?:[ \t]+-[LP]+)*[ \t]*`)"
_PRINTS_A_FOLDER = re.compile(_PWD_FORM)
# what may replace pwd, in any quoting: a function (`pwd() {…}`, zsh `'pwd'() {…}`, `function pwd`, zsh
# `functions[pwd]=…`) or an alias
_PWD_REDEFINED = re.compile(r"p['\"\\]*w['\"\\]*d['\"\\]*[ \t]*\(|(?:^|[;&|(){}\n])[ \t]*(?:functions?|alias)\b")
# what a line may hold before a pwd whose folder devkit knows: cds to a folder it reads, each ending its own
# command, then the words of the pwd's own command (no other substitution, group, pipe or separator)
_PWD_CD_WORD = (r"(?:[^\s;&|()<>{}`$'\"\\+-][^\s;&|()<>{}`$'\"\\]*|\"[^\"`$\\]*\"|'[^']*'|"
                r"\"\$\([ \t]*git[ \t]+rev-parse[ \t]+(?:--show-toplevel|--show-cdup|--git-common-dir)[ \t]*\)"
                r"(?:/[^\"`$\\]*)?\")")
_PWD_CONTEXT = re.compile(r"[ \t\n]*(?:(?:cd|pushd)(?:[ \t]+(?:-[LP]+|--))*[ \t]+" + _PWD_CD_WORD +
                          r"[ \t]*&&[ \t\n]*)*(?:[^;&|(){}\n`$\\]|" + _PWD_FORM + r")*")
_PWD_WORD_END = frozenset(" \t\n;&|)<>/")


def _pwd_is_here(cmd: str, i: int, j: int, quoted: bool) -> bool:
    """True when the `$(pwd)`, `$(pwd -P)` or `` `pwd` `` at cmd[i:j] prints the folder devkit tracks for its
    command, so a reading may put `.` in its place: a `/`, a space or the end of the word follows it (a glued
    `$(pwd)x` is `<folder>x`), and only cds devkit reads come before its command (_PWD_CONTEXT). The caller has
    checked that nothing in the command replaces pwd."""
    if not _PRINTS_A_FOLDER.fullmatch(cmd, i, j):
        return False
    nxt = cmd[j:j + 1]
    if quoted and nxt == '"':
        nxt = cmd[j + 1:j + 2]
    elif quoted and nxt != "/":
        return False
    return (not nxt or nxt in _PWD_WORD_END) and _PWD_CONTEXT.fullmatch(cmd, 0, i) is not None


def split_heredocs(cmd: str, mask: str | None = None, top: bool = False):
    """-> (the command without its heredoc bodies and terminator lines, [(body, expands)]). A `<<WORD` counts only
    where the shell reads it: outside quotes and comments, and not `<<<` (a here-string), so a quoted "<<EOF" in a
    commit message hides no line. `expands`: the delimiter is unquoted, so the shell runs `$(…)` in the body.
    `mask` (for _substitution_readings): the command keeps its heredoc bodies as they are, and each `$(…)`, backtick
    and `${…}` holding one outside quotes becomes `mask`, each `<(…)`, `>(…)` and `=(…)` SUBST_WORD and each
    `$((…))` 0, the one word the shell makes of it. `top`: `cmd` is the command, or the line of a shell that starts
    it; then a `$(pwd)` that prints the folder the command runs in (_pwd_is_here), quoted or not, is `.` in each
    reading, unless the command replaces pwd (_PWD_REDEFINED); any other `$(pwd)` is masked like any substitution."""
    out, bodies, pending = [], [], []
    i, n, q, parens = 0, len(cmd), None, Parens(cmd)
    here = mask is not None and top and "pwd" in cmd and \
        not _PWD_REDEFINED.search(cmd)
    while i < n:
        c = cmd[i]
        if q == "'":
            q = None if c == "'" else q
        elif q == '"':
            if c == "\\" and i + 1 < n:
                out.append(cmd[i:i + 2])
                i += 2
                continue
            if mask is not None and c in "$`" and (j := (close_paren(cmd, i + 2) if cmd.startswith("$(", i) else
                                                         cmd.find("`", i + 1) + 1 if c == "`" else 0)) > 0 and \
                    _PRINTS_A_FOLDER.fullmatch(cmd, i, j):
                out.append("." if here and _pwd_is_here(cmd, i, j, True) else mask)  # any other quoted pwd: masked
                i = j
                continue
            q = None if c == '"' else q
        elif c == "\\" and i + 1 < n:
            out.append(cmd[i:i + 2])
            i += 2
            continue
        elif c in "'\"":
            q = c
        elif c == "#" and parens.comment(i, True):
            j = cmd.find("\n", i)
            j = n if j < 0 else j
            out.append(cmd[i:j])
            i = j
            continue
        elif cmd.startswith("${", i) or cmd.startswith("$[", i):  # `${x/<<E/}`, `$[1<<E]`: no heredoc in either
            j = _braced_end(cmd, i)
            span = cmd[i:j]
            out.append(mask if mask is not None and ("(" in span or "`" in span) else span)
            i = j
            continue
        elif cmd.startswith("$((", i) or cmd.startswith("((", i):  # arithmetic: `1<<x` is a shift
            j, depth = i + (1 if c == "$" else 0), 0
            while j < n:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
                if depth == 0:
                    break
            if depth == 0:
                parens.close(j - 1, "w" if c == "$" else "o")
            out.append("0" if mask is not None and c == "$" else cmd[i:j])
            i = j
            continue
        elif mask is not None and (cmd.startswith("$(", i) or proc_subst_at(cmd, i)) and \
                (j := close_paren(cmd, i + 2)) > 0:
            out.append(SUBST_WORD if c != "$" else "." if here and _pwd_is_here(cmd, i, j, False) else mask)
            parens.close(j - 1, "w")
            i = j
            continue
        elif mask is not None and c == "`" and (j := cmd.find("`", i + 1)) > 0:
            out.append("." if here and _pwd_is_here(cmd, i, j + 1, False) else mask)
            i = j + 1
            continue
        elif c == "(":
            parens.open(i)
        elif c == ")":
            parens.close(i)
        elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i) and cmd[i - 1:i] != "<":
            m = _HEREDOC_AT.match(cmd, i)
            if m:
                pending.append((m.group(1) or m.group(2) or m.group(3), bool(m.group(3)) and "\\" not in m.group(0),
                                m.group(0).startswith("<<-")))
                out.append(m.group(0))
                i = m.end()
                continue
        elif c == "\n" and pending:
            out.append(c)
            i += 1
            begin = i
            for delim, expands, dash in pending:
                body = []
                while i < n:
                    j = cmd.find("\n", i)
                    j = n if j < 0 else j
                    ln, start, i = cmd[i:j], i, j + 1
                    if ln.strip() == delim:
                        break
                    if ln.startswith(delim + ")"):  # bash 3.2 ends it here in a `$(…)` and reads on from the `)`,
                        i = start + len(delim)      # zsh does not: read it as bash does, so what bash runs is seen
                        break                       # (B-fy5f5i8c1hx)
                    body.append(ln.lstrip("\t") if dash else ln)  # `<<-`: bash and zsh drop each line's leading tabs
                bodies.append(("\n".join(body), expands))
            if mask is not None:
                out.append(cmd[begin:i])
            pending = []
            continue
        out.append(c)
        i += 1
    return "".join(out), bodies


def strip_heredocs(cmd: str) -> str:
    """Drop heredoc bodies (they are data, not commands): keep the line with `<<EOF`, skip to the terminator."""
    return split_heredocs(cmd)[0]


_PUNCT = ";&|()<>"  # the characters the tokenizer cuts words at


def _bare_tokens(line: str, tokens: list):
    """For each token of `line` (as split_commands' tokenizer reads it), whether it is written without quotes or
    escapes: `case`, `in`, `esac`, `;;` and `)` are the shell's own only then, and `";;"` or `\\)` are words. Each
    quoted or escaped character is read again as `x`, which cuts no word, so the tokens line up one to one. None
    when they do not."""
    out, q, i, n = [], None, 0, len(line)
    while i < n:
        c = line[i]
        if q is None and c == "\\" and i + 1 < n:
            out.append("\\x")
            i += 2
            continue
        if q is None:
            q = c if c in ("'", '"') else None
            out.append(c)
        elif c == q:
            q = None
            out.append(c)
        elif q == '"' and c == "\\" and i + 1 < n:
            out.append("xx")
            i += 2
            continue
        else:
            out.append("x")
        i += 1
    try:
        lex = shlex.shlex("".join(out), posix=True, punctuation_chars=_PUNCT)
        lex.whitespace_split = True
        lex.commenters = ""
        masked = list(lex)
    except ValueError:
        return None
    return [a == b for a, b in zip(tokens, masked)] if len(masked) == len(tokens) else None


def _case_pattern(tokens: list, bare: list, k: int):
    """The index of the `)` that ends the case pattern starting at word k (`a|b)`, `*.py)`), or None when the words
    from k are not one: a pattern is words joined by a bare `|` and ended by a bare `)`. A word with a substitution
    (`$(…)`, a backquote, `<(…)`) is never one, so the command the shell runs to read it stays in sight."""
    j, n = k, len(tokens)
    while j < n:
        w = tokens[j]
        if bare[j] and set(w) <= set(_PUNCT) or "$(" in w or "`" in w or "<(" in w or ">(" in w:
            return None
        j += 1
        if j < n and bare[j] and tokens[j] == "|":
            j += 1
            continue
        return j if j < n and bare[j] and tokens[j].startswith(")") and set(tokens[j]) <= set(_PUNCT) else None
    return None


def _paren_step(frames: list, t: str, argv, glued: bool):
    """Read the parentheses of punctuation token `t` into `frames`, the open `$(…)`, groups, `<(…)`/`>(…)` and case
    statements (_case_word) of a command line. `argv`: the words of the command so far when its last word is the token
    before `t`. -> (the word list of the command a `$(…)` is a word of, whether the next word is glued to it) when `t`
    is only `)` and its last one ends that `$(…)`; the words after it go into that list, so they are that command's
    and never a command of their own. A pattern's `)` in a case statement ends the pattern; a `)` the frames cannot
    pair ends nothing."""
    ended = None
    for i, c in enumerate(t):
        top = frames[-1] if frames else None
        pattern = top is not None and top[0] == "case" and top[1] == "pattern"
        if c == "(":
            if not i and argv and argv[-1].endswith("$") and set(t) <= set(";&|()"):  # `$(`, `$((`
                frames.append(["subst", argv])
            elif i and t[i - 1] in "<>":
                frames.append(["proc"])
            elif not pattern:  # `case $x in (a)`: the pattern's `(` opens nothing
                frames.append(["group"])
        elif c == ")":
            ended = None
            if pattern:
                top[1] = "body"
            elif top is not None and top[0] != "case":
                frames.pop()
                ended = top if top[0] == "subst" else None
        elif top is not None and top[0] == "case" and top[1] == "body" and (";;" in t or ";&" in t):
            top[1] = "pattern"  # an arm's end: a pattern or `esac` comes next
    return (ended[1], glued) if ended and set(t) == {")"} else None


def _case_word(frames: list, t: str, before: list):
    """Read word `t` into `frames` (_paren_step): `case` at a command's start opens a case statement, its `in` starts
    the patterns, and `esac` at a command's start closes it. `before`: the command's words before `t`."""
    top = frames[-1] if frames else None
    start = all(a in RESERVED for a in before)
    if top is not None and top[0] == "case":
        if top[1] == "head" and t == "in":
            top[1] = "pattern"
            return
        if start and t == "esac":
            frames.pop()
            return
        if top[1] == "pattern":  # a pattern's word
            return
    if start and t == "case":
        frames.append(["case", "head"])


def _arith_words(line: str) -> str:
    """`line` with each arithmetic expansion `$((…))` outside single quotes written as `0`, the one word the shell
    makes of it: its body is an expression, never a command (`$((n*2))`, `$((a > b))`), and the substitutions in it
    are read by subst with the other expansions. A `$((date)|wc -c)` is a command substitution and stays. An
    arithmetic command `((…))` (also a for loop's header) is `((0))`: its body is an expression too."""
    if "((" not in line:
        return line
    out, i, n, q = [], 0, len(line), None
    while i < n:
        c = line[i]
        if q == "'":
            q = None if c == "'" else q
        elif c == "\\":
            out.append(line[i:i + 2])
            i += 2
            continue
        elif c == "'" and q is None:
            q = "'"
        elif c == '"':
            q = None if q == '"' else '"'
        elif line.startswith("$((", i) and (end := arith_end(line, i + 3)) is not None:
            out.append("0")
            i = end + 2
            continue
        elif line.startswith("((", i) and q is None and (not i or line[i - 1] not in "$<>=" and
                                                         not line[i - 1].isalnum()) and \
                (end := arith_end(line, i + 2)) is not None:
            out.append("((0))")  # `((n > 2))`, `for ((i=0; i<3; i++))`: an expression, no redirect, no list
            i = end + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def split_commands(cmd: str, ops: bool = False, newline: str = ";", inputs: list | None = None,
                   stripped: bool = False, heredocs: list | None = None, modes: list | None = None):
    """-> list of (argv, redirect_targets). None when the command cannot be tokenised. With `ops`, each entry is
    (argv, redirect_targets, the operator after the segment ('|', '&&', ';', … or ''), its here-strings (<<< …)).
    `newline`: the operator a newline outside quotes becomes (`&` for tmux_list_refusal only). `inputs`: a list that
    gets, for each segment, the files its input is redirected from (`< f`, `0< f`, `<> f`). `stripped`: `cmd` is
    already without its heredoc bodies (strip_heredocs); stripping it again would read the line after a `<<EOF` as
    a body and hide it. `heredocs`: a list that gets, for each segment, whether a heredoc is its input. `modes`: a
    list that gets, for each segment, how each of its redirect targets is opened, in order: "truncate" (`>`, `&>`),
    "append" (`>>`, `&>>`) or "overwrite" (`<>`, which keeps what it does not write over)."""
    try:
        line = _arith_words(_newlines_to_semicolons(cmd if stripped else strip_heredocs(cmd), newline))
        lex = shlex.shlex(line, posix=True, punctuation_chars=_PUNCT)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens, glued = [], []
        for t in lex:  # after a `)`, shlex has read the character that follows it, which a word may be glued to
            at = lex.instream.tell()
            tokens.append(t)
            glued.append(bool(at) and line[at - 1] not in _PUNCT and not line[at - 1].isspace())
    except ValueError:
        return None
    segs, argv, redirs, expect_redir, here, herestr = [], [], [], False, [], False
    fd, stdin, ins, hd, opened, mode = None, False, [], False, [], "truncate"
    # frames: _paren_step's; into: [the words of the command a `$(…)` that just ended is a word of, whether the next
    # word is glued to it, whether words went there]; cur: the words the word at token `last` went to
    frames, into, cur, last = [], None, None, -1
    # bare: _bare_tokens', read only for a case statement, whose patterns (`*)`, `a|b)`) are words the shell matches
    # and never runs; skip: the index of the `)` that ends the pattern being passed over
    bare = _bare_tokens(line, tokens) if "case" in tokens else None
    skip = -1
    for k, t in enumerate(tokens):
        ended = None
        if k < skip:
            continue
        if bare is not None and frames and frames[-1][0] == "case" and expect_redir is False:
            top = frames[-1]
            if top[1] == "body" and bare[k] and set(t) <= set(";&") and (";;" in t or ";&" in t):
                top[1] = "pattern"  # an arm's end, `;;`, `;&` or `;;&`: a pattern or `esac` comes next
            elif top[1] == "pattern" and not (bare[k] and t == "esac") and \
                    (end := _case_pattern(tokens, bare, k)) is not None:
                skip = end
                continue
        if set(t) <= set(_PUNCT) and ("(" in t or ")" in t):
            ended = _paren_step(frames, t, cur if last == k - 1 else None, glued[k])
        nxt = tokens[k + 1] if k + 1 < len(tokens) else ""
        if t.isdigit() and nxt[:1] in ("<", ">"):
            fd = t
            continue  # file-descriptor prefix: the 2 in 2>&1 / 2>file
        if t and set(t) <= set("<>&-") and "&" in t and ("<" in t or ">" in t):
            if nxt.isdigit() or nxt == "-":
                expect_redir = None  # fd duplication: skip the fd number
            else:
                expect_redir = ">" in t
                mode = "append" if ">>" in t else "truncate"
            fd = None
            continue
        if t and set(t) <= set(";&|()"):
            if argv or redirs or (into and into[2]):  # the part after a `$(…)` keeps its operator and redirects
                segs.append((argv, redirs, t, here) if ops else (argv, redirs))
                if inputs is not None:
                    inputs.append(ins)
                if heredocs is not None:
                    heredocs.append(hd)
                if modes is not None:
                    modes.append(opened)
            argv, redirs, here, ins, hd, opened = [], [], [], [], False, []
            into = [*ended, False] if ended else None
            continue
        if t and (set(t) <= set("<>") or t == ">|"):  # `>|` truncates like `>`, even where noclobber is set
            herestr = t == "<<<"
            hd = hd or (t == "<<" and fd in (None, "0"))
            stdin = t in ("<", "<>") and fd in (None, "0")
            fd = None
            expect_redir = ">" in t
            mode = "append" if ">>" in t else "overwrite" if t == "<>" else "truncate"
            if not expect_redir:
                expect_redir = None  # input redirect: skip next token
            continue
        if expect_redir is not False:
            if stdin:
                ins.append(t)
            if expect_redir and t not in ("<(", ">("):  # `> >(…)` writes into a substitution, whose command is read
                redirs.append(t)
                opened.append(mode)
            elif herestr:
                here.append(t)
            expect_redir, herestr, stdin = False, False, False
            continue
        if into is not None and not argv:  # `ls $(echo x)/*`: the rest of the word, and the words after it, are the
            outer = into[0]                  # command's the `$(…)` is a word of, as the shell reads them
            if bare is None or bare[k]:  # a quoted `"in"` or `"esac"` is a word
                _case_word(frames, t, outer)
            if into[1] and not into[2]:
                outer[-1] += t
            else:
                outer.append(t)
            into[2], cur = True, outer
        else:
            if bare is None or bare[k]:
                _case_word(frames, t, argv)
            argv.append(t)
            cur = argv
        last = k
    if argv or redirs or (into and into[2]):
        segs.append((argv, redirs, "", here) if ops else (argv, redirs))
        if inputs is not None:
            inputs.append(ins)
        if heredocs is not None:
            heredocs.append(hd)
        if modes is not None:
            modes.append(opened)
    return segs


def _prefix_opts(name, argv, i):
    """The options of prefix command `name` from argv[i] on -> ([(flag, value or None)], index of the first word after
    them). A short cluster ends at a flag that takes a value: `env -iC dir`, `sudo -iu root` (the value is the rest of
    the word or the next word)."""
    takes, opts = _PREFIX_VALUE_FLAGS.get(name, ()), []
    while i < len(argv) and argv[i].startswith("-"):
        f = argv[i]
        i += 1
        if f == "--":
            break
        if f.startswith("--"):
            key, eq, val = f.partition("=")
            if not eq and key in takes and i < len(argv):
                val, i = argv[i], i + 1
            opts.append((key, val if eq or key in takes else None))
            continue
        for j in range(1, len(f)):
            if "-" + f[j] in takes:
                val = f[j + 1:]
                if not val and i < len(argv):
                    val, i = argv[i], i + 1
                opts.append(("-" + f[j], val))
                break
            opts.append(("-" + f[j], None))
    return opts, i


def strip_prefix(argv):
    i = 0
    while i < len(argv):
        a = argv[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\+?=", a) or a == _DASH_PRECOMMAND:  # `X=1`, and `PATH+=:/x` appends
            i += 1
        elif os.path.basename(a) in PREFIX_CMDS:
            i = _prefix_opts(os.path.basename(a), argv, i + 1)[1]
        else:
            break
    return argv[i:]


# the prefix flags that run the command in another folder: `env -C dir`, `env --chdir=dir`, `sudo -D dir`
_CHDIR_OPTS = {"env": ("-C", "--chdir"), "sudo": ("-D", "--chdir")}


def _prefix_chdir(pre, cwd):
    """The folder a segment's command runs in after its prefix words `pre` (`env -C <repo> git push` pushes from
    <repo>); UNKNOWN when the folder is known only at run time."""
    i = 0
    while i < len(pre):
        name = os.path.basename(pre[i])
        if name not in PREFIX_CMDS:
            i += 1
            continue
        opts, i = _prefix_opts(name, pre, i + 1)
        for flag, val in opts:
            if flag in _CHDIR_OPTS.get(name, ()) and val is not None:
                cwd = str(resolve(val, cwd)) if val else cwd
    return cwd


def positionals(args, value_opts):
    pos, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos += args[i + 1:]
            break
        if a.startswith("-"):
            if a in value_opts and "=" not in a:
                i += 1
        else:
            pos.append(a)
        i += 1
    return pos


def opt_value(args, opts):
    """Value of the first of `opts` in args (`-b name`, `--create=name`)."""
    for i, a in enumerate(args):
        if a in opts and i + 1 < len(args):
            return args[i + 1]
        for o in opts:
            if o.startswith("--") and a.startswith(o + "="):
                return a.split("=", 1)[1]
    return None


def _blank_quoted(cmd: str) -> str:
    """`cmd` with the text inside its quotes blanked (the quotes stay): a quoted message, note or evidence is data,
    whatever it says (`--evidence "pytest | tail -1 && git commit hid it"`). The substitutions a double-quoted
    string runs are read on their own (_pipe_gate_closed)."""
    out, q = [], None
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\" and q != "'":
            out.append("  " if q else cmd[i:i + 2])
            i += 2
            continue
        if q:
            if c == q:
                q = None
                out.append(c)
            else:
                out.append("\n" if c == "\n" else " ")
        else:
            if c in "'\"":
                q = c
            out.append(c)
        i += 1
    return "".join(out)


_FILTERS = frozenset({"tail", "head", "grep", "egrep", "fgrep", "rg", "cat", "wc", "jq", "cut", "sort", "uniq", "tr",
                      "less", "more"})

_CHECK_TOOLS = frozenset({"pytest", "tox", "nox", "jest", "vitest", "mocha", "tsc", "ruff", "mypy", "eslint",
                          "shellcheck", "phpunit", "pest", "rspec", "phpstan", "psalm"})

# N2: what a segment runs one level down, which the rules on destructive commands read as they read the top level: a
# shell's -c line, eval's words, and the command find -exec and xargs run (so `find … -exec sh -c '<line>'` is read
# too). Their own rules judge a command that takes the file names they substitute ({}), as rm, cp or mkdir do.
_PATH_TAKERS = frozenset({"rm", "unlink", "mkdir", "touch", "cp", "mv", "rsync", "ditto"})


_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_BASH_LIKE = frozenset({"bash", "sh"})  # sh is bash on macOS
# bash's long options, which it reads with one dash as with two (`bash -norc` is `bash --norc`, not -n -o rc -c):
# those that take the next word as their value, and those that only change how it reports or which startup files it
# skips. The others (login, debugger, wordexp, dump-strings, …) may run startup files or code: a shell given one runs.
_BASH_LONG_VALUE = frozenset({"rcfile", "init-file"})
_BASH_LONG_QUIET = frozenset({"norc", "noprofile", "noediting", "posix", "verbose", "restricted"})
_BASH_LONG = _BASH_LONG_VALUE | _BASH_LONG_QUIET | frozenset({
    "debug", "debugger", "dump-po-strings", "dump-strings", "help", "login", "pretty-print", "protected",
    "rpm-requires", "version", "wordexp"})
# the option letters and `-o` names a bash that only parses knows to run nothing: set's flags, -r, -s, -c, -n
_BASH_QUIET_LETTERS = frozenset("abefhkmnptuvxBCEHPTrsc")
_BASH_QUIET_SET = frozenset({
    "allexport", "braceexpand", "emacs", "errexit", "errtrace", "functrace", "hashall", "histexpand", "history",
    "ignoreeof", "interactivecomments", "keyword", "monitor", "noclobber", "noexec", "noglob", "nolog", "notify",
    "nounset", "onecmd", "physical", "pipefail", "posix", "privileged", "verbose", "vi", "xtrace"})
_BASH_QUIET_SHOPT = frozenset({"extglob", "globstar", "nullglob", "failglob", "dotglob", "nocaseglob", "nocasematch",
                               "expandaliases"})


def shell_args(args, name="bash") -> tuple:
    """How a shell reads its words -> (kind, k, parse_only). kind: "line" (the -c command line is args[k], its $0,
    $1… follow it), "script" (a script file at args[k]), "stdin" (-s: its program comes from its input, and its $1…
    start at args[k]), "input" (no word left: it reads its input) or "none" (-c and no line). bash, zsh, sh, dash and
    ksh read options up to the first word that is not one, -c among them, so the line is the first word after all of
    them (`bash -c -e 'line'`, `bash -c +x 'line'`, `zsh -c -o errexit 'line'`); csh, tcsh and fish take the word right
    after -c. bash and sh read their long options with one dash too (`-norc`, `-rcfile f`, _BASH_LONG), and take +c,
    +s and +l as -c, -s and -l. parse_only: one of those five only reads and parses its program, and runs none of it
    nor its startup files: -n, -o noexec or zsh's --noexec is on (the last of -n, +n, -o noexec, +o noexec wins), it
    is no interactive or login shell (-i, -l, --login, --rcfile), where -n does not hold, and, for bash and sh, every
    option word is one devkit knows runs nothing; any other word is judged as running."""
    posix, bashlike = name in _POSIX_SHELLS, name in _BASH_LIKE
    line = stdin = noexec = runs = False
    i, leading = 0, bashlike  # bash reads its long options before any short one: `bash -n -posix` is -n -p -o …
    while i < len(args):
        a = args[i]
        if a in ("--", "-"):
            i += 1
            break
        long = a[2:] if a.startswith("--") else a[1:] if leading and a[:1] == "-" and a[1:] in _BASH_LONG else None
        leading = leading and long is not None
        if long is not None:
            key = long.split("=", 1)[0].casefold()
            opt = re.sub(r"[_-]", "", key)
            noexec = True if opt == "noexec" else False if opt == "exec" else noexec
            runs = runs or opt in ("login", "interactive", "rcfile", "initfile") or \
                (bashlike and key not in _BASH_LONG_QUIET)
            i += 2 if key in _BASH_LONG_VALUE and "=" not in long else 1
            continue
        if len(a) < 2 or a[0] not in "-+":
            break
        on, names = a[0] == "-", []
        for ch in a[1:]:
            if ch in "oO":  # its option name is the next word: -o noexec, -eo pipefail, -O extglob
                names.append((ch, args[i + 1 + len(names)] if i + 1 + len(names) < len(args) else ""))
            elif ch == "n":
                noexec = on
            elif ch == "c" and (on or bashlike):  # bash reads +c as -c, +s as -s and +l as -l
                line = True
            elif ch == "s" and (on or bashlike):
                stdin = True
            elif ch in "il" and (on or (bashlike and ch == "l")):
                runs = True
            elif bashlike and ch not in _BASH_QUIET_LETTERS:  # -D, -i… or a letter it does not know
                runs = True
        for ch, opt in names:
            opt = re.sub(r"[_-]", "", opt.casefold())
            if ch == "o" and opt in ("noexec", "exec"):
                noexec = (opt == "noexec") == on
            if bashlike and opt not in (_BASH_QUIET_SET if ch == "o" else _BASH_QUIET_SHOPT):
                runs = True
        i += 1 + len(names)
        if line and not posix:  # csh, tcsh, fish: the word after -c is the line
            break
    parse_only = posix and noexec and not runs
    if line:
        return ("line" if i < len(args) else "none"), i, parse_only
    if stdin:
        return "stdin", i, parse_only
    return ("script" if i < len(args) else "input"), i, parse_only


def _patch_lines(patch: str) -> list:
    """An apply_patch envelope's lines as Codex's parser reads them: split on `\n` only, with one `\r` before it
    dropped. str.splitlines() also splits on `\r`, `\x0b`, `\x0c`, `\x1c`-`\x1e`, `\x85`, U+2028 and U+2029, which
    cut a header path that Codex reads whole. A final `\n` ends the last line, as for splitlines."""
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in str(patch).split("\n")]
    return lines[:-1] if lines and lines[-1] == "" else lines


def parse_patch_paths(patch: str):
    """apply_patch envelope -> [(op, path)] for Add/Update/Delete/Move, its lines read as Codex reads them."""
    out = []
    for ln in _patch_lines(patch):
        m = re.match(r"^\*\*\* (Add|Update|Delete) File: (.+)$", ln.strip())
        if m:
            out.append((m.group(1).lower(), m.group(2).strip()))
        m = re.match(r"^\*\*\* Move to: (.+)$", ln.strip())
        if m:
            out.append(("add", m.group(1).strip()))
    return out


def _read_small(p, limit: int = 1 << 19) -> str:
    """A file's text, or "" for no file, one larger than `limit`, or one the system cannot open or read."""
    try:
        str(p).encode("utf-8")  # pathlib's is_file() reads a name it cannot encode as no file (Python 3.9)
        p = Path(str(p))
        if p != UNKNOWN and p.is_file() and p.stat().st_size <= limit:
            return p.read_text(errors="replace")
    except (UnicodeError, ValueError, OSError):  # a lone surrogate, a NUL in the path, permissions: unread
        pass
    return ""


# the shells and interpreters devkit reads, by the names they are installed under, to know them by their file
_KNOWN_PROGRAMS = ("bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "python3", "python", "pypy3", "node",
                   "nodejs", "deno", "bun", "perl", "ruby", "php", "osascript", "lua", "tclsh", "Rscript", "swift",
                   "awk", "gawk", "mawk", "nawk", "expect", "wish")
_PROGRAM_DIRS = ("/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")


def _program_family(name) -> str:
    """A program's name without its version and folder: python3.13 and python3 are python, ksh93 is ksh."""
    fam = re.sub(r"[0-9.]+$", "", os.path.basename(str(name or ""))).casefold()
    return {"pypy": "python", "nodejs": "node"}.get(fam, fam)


def _reads_programs(name) -> bool:
    """True for a shell or an interpreter devkit reads by its name."""
    base = os.path.basename(str(name or ""))
    return base in SHELLS or bool(INTERP.match(base)) or _program_family(base) in SHELLS


@functools.lru_cache(maxsize=1)
def _program_files() -> tuple:
    """({(device, inode): path}, {size: [path]}) of the shells and interpreters installed here, each by its real
    path. A file with more than two names is left out: one binary that many programs share and that picks what to
    be by the name it is called by (macOS's /usr/bin shims) says nothing about which program a copy of it is."""
    nodes, sizes = {}, {}
    for name in _KNOWN_PROGRAMS:
        for p in [os.path.join(d, name) for d in _PROGRAM_DIRS] + [shutil.which(name) or ""]:
            try:
                real = os.path.realpath(p) if p else ""
                st = os.stat(real) if real else None
            except (OSError, ValueError):
                continue
            if st is None or st.st_nlink > 2 or not _reads_programs(real) or (st.st_dev, st.st_ino) in nodes:
                continue
            nodes[(st.st_dev, st.st_ino)] = real
            sizes.setdefault(st.st_size, []).append(real)
    return nodes, sizes


def _identify(word, cwd) -> str:
    """The real path of the shell or interpreter the program word `word` runs when its name does not say so, else
    '': a path or a name on the PATH whose links lead to one (`./bash-link`), or a file that is one installed here
    under another name (a hard link, a renamed copy). A word that names one already, or that names nothing devkit
    can find, is ''."""
    word = str(word or "")
    if not word or _reads_programs(word) or re.search(r"[$`*?\[{]", word):
        return ""
    if "/" in word:
        p = resolve(word, cwd)
        if p == UNKNOWN:
            return ""
        real = str(p)
    else:
        found = shutil.which(word)
        if not found:
            return ""
        real = os.path.realpath(found)
    if _reads_programs(real):
        return real
    try:
        st = os.stat(real)
    except (OSError, ValueError):
        return ""
    nodes, sizes = _program_files()
    if (st.st_dev, st.st_ino) in nodes:
        return nodes[(st.st_dev, st.st_ino)]
    for known in sizes.get(st.st_size, []):
        try:
            if filecmp.cmp(real, known, shallow=False):
                return known
        except OSError:
            continue
    return ""


def _as_run(argv, cwd) -> list:
    """The readings of a segment's program: as it is written, and, when the file it runs is a shell or an
    interpreter under another name (_identify: `./bash-link -c …`, a renamed copy of zsh), as that program too. A
    refusal of either stands: a binary that picks what to be by the name it is called by runs as written, any other
    as what it is."""
    real = _identify(argv[0], cwd) if argv else ""
    return [argv, [real, *argv[1:]]] if real and _program_family(real) != _program_family(argv[0]) else [argv]


SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})

INTERP = re.compile(r"^(?:python[0-9.]*|pypy[0-9.]*|node|nodejs|deno|bun|perl[0-9.]*|ruby|php|osascript|lua|tclsh|"
                     r"Rscript|swift|awk|gawk|mawk|nawk|expect|wish)$")

_READERS = {"cat", "head", "tail", "less", "more", "bat", "ls", "stat", "file", "wc", "grep", "egrep", "fgrep", "rg",
            "ag", "ack", "jq", "diff", "cmp", "comm", "md5", "md5sum", "shasum", "sha1sum", "sha256sum", "cksum", "du",
            "df", "realpath", "readlink", "basename", "dirname", "test", "[", "[[", "echo", "printf", "hexdump", "xxd",
            "od", "strings", "column", "cut", "tr", "nl", "fold", "paste", "tree", "lsof", "true", "false", "type",
            "which", "export", "declare", "local", "readonly", "typeset", "set", "unset", "alias", "popd", "wait",
            "sleep", "date", "pwd", "whoami", "id", "uname", "env", "printenv"}
_CREATORS = {"tee", "touch", "mkdir", "mkfifo", "truncate"}  # write the paths they name
_CHANGERS = {"rm", "rmdir", "unlink", "shred", "srm", "ln", "chmod", "chown", "chgrp", "chflags", "xattr", "setfacl"}
_COPIERS = {"cp", "gcp", "install", "ditto", "rsync", "scp"}
_MOVERS = {"mv", "gmv"}

_AGENT_CLIS = {"claude", "codex"}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

_SHELL_GLOB = frozenset("{*?[")
# How xargs reads its own options (BSD and GNU), so a value never passes for its command (`xargs -tn 1 dev`, `xargs
# --max-args 1 dev` ran `dev <input>` while devkit read `1` as the command): short options cluster, and one of
# _XARGS_SHORT takes the rest of its word or the next word as its value; GNU's -e, -i and -l take one only attached;
# a long option may be any unique prefix of its name, with its value after `=` or, when it requires one, as the next
# word. The first word that is no option, or the word after `--`, is the command.
_XARGS_SHORT = frozenset("adEIJLnPRsS")
_XARGS_SHORT_ATTACHED = frozenset("eil")
_XARGS_LONG = {"--arg-file": True, "--delimiter": True, "--max-args": True, "--max-procs": True, "--max-chars": True,
               "--process-slot-var": True, "--eof": False, "--replace": False, "--max-lines": False, "--null": None,
               "--open-tty": None, "--interactive": None, "--no-run-if-empty": None, "--show-limits": None,
               "--verbose": None, "--exit": None, "--version": None, "--help": None}


def xargs_parts(args) -> tuple:
    """-> (words, replstr): the command xargs runs as its arguments give it, and the string it replaces with what it
    reads (-I, -J, -i, --replace), or None when it appends what it reads after the words instead."""
    i, rep = 0, None
    args = [str(a) for a in args]
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if a == "--":
            i += 1
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            hits = [k for k in _XARGS_LONG if k == name] or [k for k in _XARGS_LONG if k.startswith(name)]
            takes = any(_XARGS_LONG[k] for k in hits)
            if not eq and takes:
                val, i = (args[i + 1] if i + 1 < len(args) else ""), i + 1
            if hits == ["--replace"]:
                rep = val if eq else "{}"
            i += 1
            continue
        k = 1
        while k < len(a):
            c = a[k]
            if c in _XARGS_SHORT:
                val = a[k + 1:]
                if not val:
                    i += 1
                    val = args[i] if i < len(args) else ""
                if c in "IJ":
                    rep = val
                break
            if c in _XARGS_SHORT_ATTACHED:
                if c == "i":
                    rep = a[k + 1:] or "{}"
                break
            k += 1
        i += 1
    return args[i:], rep


def xargs_command(args) -> list:
    """The command xargs runs, as its arguments give it (before the words it adds from its input), with its
    replace string (-I, -J, -i) written `{}`, the runners' fill, so the rules on a filled-in word read it."""
    words, rep = xargs_parts(args)
    return [w.replace(rep, "{}") for w in words] if rep else words


def _shell_made(word) -> bool:
    """A word the shell may turn into other words before claude or codex reads it: one that holds
    `$` or a backtick (a variable, "$@", a substitution, ANSI-C quoting `$'…'`), or one without whitespace that holds
    `{`, `*`, `?` or `[` (brace expansion, a glob). devkit reads words with their quotes removed, so a quoted `*`
    counts too; a word with whitespace was quoted, and in quotes the shell expands only `$` and backticks."""
    w = str(word)
    return "$" in w or "`" in w or (not any(c.isspace() for c in w) and bool(_SHELL_GLOB & set(w)))


_JS_RUNTIMES = frozenset({"node", "nodejs", "bun", "deno"})

_AGENT_PACKAGES = {"claude-code": "claude", "codex": "codex"}  # npm packages whose binary is claude or codex (RT8)


def _agent_binary(path: str) -> str:
    """'claude' or 'codex' when a path names one of their binaries: a file named so, or a file in Claude Code's
    versions folder (`~/.local/bin/claude` links to `~/.local/share/claude/versions/<version>`)."""
    p = Path(path)
    if p.name in _AGENT_CLIS:
        return p.name
    return "claude" if (p.parent.name, p.parent.parent.name) == ("versions", "claude") and p.name else ""


def agent_cli(word, cwd=None, search=False) -> str:
    """'claude' or 'codex' when a command word runs one of them: the binary, by its name, by Claude Code's versions
    folder, or by a path that resolves to either (a link, `..`; a relative one from `cwd`, the folder the command runs
    in: N3); with `search`, a bare name the PATH resolves to either (a link named otherwise); or the npm package a
    runner such as npx, bunx, pnpm dlx or yarn dlx starts (`@anthropic-ai/claude-code@latest`, `claude-code`,
    `@openai/codex`); '' otherwise (RT8)."""
    text = str(word or "")
    name = _agent_binary(text)
    if not name and "/" in text:
        path = os.path.expanduser(os.path.expandvars(text))
        if not os.path.isabs(path) and cwd and str(cwd) != str(UNKNOWN):
            path = os.path.join(str(cwd), path)
        name = _agent_binary(os.path.realpath(_c_path(path))) if os.path.isabs(path) and "$" not in path else ""
    elif not name and search and text and not set(text) & set("$`"):
        found = shutil.which(text)
        name = _agent_binary(os.path.realpath(found)) if found else ""
    if name:
        return name
    pkg = str(word or "")
    if pkg.startswith("@"):  # @scope/name@version
        scope, _, rest = pkg.partition("/")
        pkg = rest.partition("@")[0] if scope in ("@anthropic-ai", "@openai") else ""
    else:
        pkg = pkg.partition("@")[0]
    return _AGENT_PACKAGES.get(pkg.casefold(), "")


def _repo_script(argv, cwd) -> bool:
    """True when an interpreter runs a script file inside the workspace (a repository's own code, such as a test)
    rather than inline code, its input or a file elsewhere."""
    for a in argv[1:]:
        if a in ("-", "--", "-c", "-e", "-E", "--eval", "-p", "--print", "-r", "-m") or \
                (a.startswith("-") and not a.startswith("--") and a[-1:] in ("c", "e", "m")):
            return False
        if a.startswith("-"):
            continue
        return _workspace_file(resolve(a, cwd))
    return False


def _workspace_file(t: Path) -> bool:
    """True when a resolved path is inside the workspace (the dev root) and not in a temp folder: a repository's own
    code, which the gate does not read for actions."""
    return t != UNKNOWN and is_under(t, DEV) and not is_temp(t)


def heredoc_bodies(cmd: str) -> list:
    """The heredoc bodies and here-strings of a command: the text an interpreter or shell reads from its input."""
    text, bodies = split_heredocs(cmd)
    return [b for b, _ in bodies] + [next(g for g in m.groups() if g is not None) for m in
                                     re.finditer(r"<<<\s*(?:'([^']*)'|\"((?:[^\"\\]|\\.)*)\"|(\S+))", text)]


_FIND_RUNS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})  # find's primaries that run a command


def unwrap(argv, more: bool = False, stop=()):
    """The command a segment really runs: without env/sudo/nohup prefixes, shell keywords (`then`, `do`, `{`, `!`),
    `timeout N`, `uv run`, the package runners (npx, npm exec|x, bun x, yarn exec, `pnpm|yarn <claude|codex>`) and
    the wrappers. A screen or tmux call that terminal_typing_refusal refuses is returned as it is,
    for the walker to refuse; `more`: a runner (xargs) adds words after these; `stop`: launchers (screen, tmux)
    returned as they are. A segment that starts with a find primary or operator (`-exec`, `-name`, `,`) is the rest of
    a find expression the tokenizer split at `\\;`, `\\(` or `\\)`, and is read as find's."""
    while True:
        argv = strip_prefix(argv)
        if not argv:
            return argv
        n = os.path.basename(argv[0])
        if argv[0] in ("for", "select") and argv[2:3] == ["do"]:  # `for a do $a …`: a loop over $@ needs no `;`
            argv = argv[3:]
        elif argv[0] == "function" and len(argv) > 2:  # `function f { gh …; }`: the body's first command
            argv = argv[2:]
        elif argv[0] == "case":  # `case $f in a)`: a subject and a pattern, no command (their substitutions are
            return []  # judged as commands of their own)
        elif argv[0] in RESERVED:
            argv = argv[1:]
        elif n in ("timeout", "gtimeout"):
            rest = argv[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            argv = rest[1:]
        elif n in _PACKAGE_MANAGERS and _pm_runs(n, (pm := _pm_verb(argv[1:]))[0]):
            argv = _runner_command(pm[1], calls=n == "npm")
        elif n in ("npx", "pnpx", "bunx", "uvx"):
            argv = _runner_command(argv[1:], calls=n == "npx")
        elif n in ("pnpm", "yarn") and agent_cli((_runner_command(argv[1:]) or [""])[0]):
            argv = _runner_command(argv[1:])  # `pnpm claude …` runs the package's bin, or the PATH's
        elif n in _WRAPPERS:
            argv = _wrapped(n, argv[1:])
        elif n in ("screen", "tmux") and n in stop:
            return argv
        elif n in ("screen", "tmux"):
            refused, run = (_screen_parse if n == "screen" else _tmux_parse)([str(a) for a in argv[1:]], more)
            if refused:  # it types into a session: the call itself is what the walker refuses
                return argv
            argv = run
        elif n == "expect":
            run = _expect_command(argv[1:])
            if run is None:  # a script file or its input: the interpreter, read as one (interpreter_program)
                return argv
            argv = run
        elif n == "yarn" and argv[1:3] == ["workspaces", "foreach"]:  # runs `yarn <command>` in each workspace
            rest, i = argv[3:], 0
            while i < len(rest) and rest[i].startswith("-"):
                i += 2 if rest[i] in ("-j", "--jobs", "--include", "--exclude", "--from") else 1
            return [argv[0], *rest[i:]]
        elif n == "yarn" and argv[1:2] == ["workspace"] and len(argv) > 2:  # `yarn workspace <name> <command>`
            return [argv[0], *argv[3:]]
        elif (n == "," or (n.startswith("-") and len(n) > 1)) and (any(a in _FIND_RUNS for a in argv) or
                                                                   argv[0] in _FIND_PRIMARIES):
            return ["find", *argv]
        else:
            return argv


# options of the package runners that take a value (`npx -p <pkg>`, `npm exec --package <pkg>`, pnpm's `--filter`,
# uv's `--with <pkg>`), so the word after one is no command; a runner's `-c '<line>'` (npx, npm exec) hands the line to
# a shell. The package managers' own options before their verb (`npm --prefix . exec`, `uv --directory . run`) are read
# with the same list.
_RUNNER_VALUES = frozenset({"-p", "--package", "-w", "--workspace", "--prefix", "--registry", "--cache", "--userconfig",
                            "--loglevel", "--script-shell", "--filter", "-F", "-C", "--dir", "--cwd",
                            "--with", "--python", "--directory", "--project", "--env-file", "--group", "--extra",
                            "--index", "--with-requirements", "--with-editable", "--from", "--default-index",
                            "--index-url", "--extra-index-url", "--find-links", "-f", "--cache-dir", "--config-file",
                            "--package", "-P", "--only-group", "--no-group", "--python-preference", "--color"})
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun", "uv", "poetry", "pipenv", "pdm", "hatch", "rye"})


def _pm_verb(args) -> tuple:
    """A package manager's verb and the words after it, past its own options and their values (_RUNNER_VALUES):
    `npm --silent exec -- x` -> ("exec", ["--", "x"])."""
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        i += 2 if args[i] in _RUNNER_VALUES else 1
    return (args[i] if i < len(args) else ""), list(args[i + 1:])


def _pm_runs(n: str, verb: str) -> bool:
    """True when this package manager's verb runs the command after it (npm exec|x, pnpm exec|dlx, yarn exec|dlx,
    bun x, uv|poetry|pipenv|pdm|hatch|rye run)."""
    return verb in {"npm": ("exec", "x"), "pnpm": ("exec", "dlx"), "yarn": ("exec", "dlx"), "bun": ("x",)}.get(
        n, ("run",))


def _runner_command(args, calls=False) -> list:
    """The command a package runner runs, from its arguments: the first word that is no option (or the words after
    `--`), past the values of _RUNNER_VALUES; with `calls`, a `-c|--call '<line>'` comes back as `sh -c <line>`.
    [] when it names none."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return list(args[i + 1:])
        if calls and (a in ("-c", "--call") or a.startswith("--call=")):
            line = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            return ["sh", "-c", line] if line else []
        if a.startswith("-"):
            i += 2 if a in _RUNNER_VALUES else 1
            continue
        return list(args[i:])
    return []


# programs that run the command they are given: `script -q /dev/null cmd`, `watch -n 600 cmd`, GNU
# `parallel cmd ::: args` and `sem`, `arch -arm64 cmd`, `flock file cmd`, `unbuffer cmd`, `chronic cmd`
_WRAPPERS = frozenset({"script", "watch", "parallel", "sem", "arch", "flock", "unbuffer", "chronic"})
_PARALLEL_VALUES = frozenset(
    "-j --jobs -P --max-procs -S --sshlogin --sshloginfile --slf --joblog -a --arg-file --delay --timeout -N -n "
    "--max-args -L --max-lines --colsep -C -d --delimiter -E -I --results --res --tmpdir --workdir --wd --halt "
    "--memfree --load --retries --transferfile --tf --return --basefile --bf --env --tagstring --rpl "
    "--sqlmaster --sqlworker --id --semaphorename --fg-timeout -s --max-chars --arg-sep --arg-file-sep --limit "
    "--ssh --filter -J --profile --block --block-size --recend --recstart --termseq --sshdelay --nice --ctag "
    "--ctagstring --tag-string --onall-sep --hostgroups --wait-retry --er --extensionreplace --bnr "
    "--basenamereplace --dnr --dirnamereplace --bner --basenameextensionreplace --seqreplace --slotreplace".split())
# GNU parallel's options that name a string it replaces with what it reads, as `{}` is: -I and --replace (-i) for `{}`
# itself, the ones for {.} {/} {//} {/.} {#} {%}, and --rpl, whose value starts with the string (`--rpl '{P} s/x/y/'`).
# Parallel takes a long option by any unique prefix of its name, so a prefix of one of these counts as it.
_PARALLEL_REPLACES = ("--replace", "--extensionreplace", "--er", "--basenamereplace", "--bnr", "--dirnamereplace",
                      "--dnr", "--basenameextensionreplace", "--bner", "--seqreplace", "--slotreplace", "--rpl")
_PARALLEL_OPTIONAL = {"-i": "{}", "--replace": "{}", "-e": None, "--eof": None, "-l": None, "--max-lines": None}
_BRACED = re.compile(r"\{[^{}]*\}")  # any {…} in a word parallel runs: {}, {.}, {..}, {+/}, {1}, {=perl=}, an --rpl tag
_WRAP_VALUES = {"script": {"-t", "-F", "-T", "-E", "--echo", "-o", "--output-limit", "-I", "--log-in", "-O",
                           "--log-out", "-B", "--log-io", "-m", "--logging-format", "--log-timing"},
                "watch": {"-n", "--interval", "-q", "--equexit"}, "parallel": _PARALLEL_VALUES, "sem": _PARALLEL_VALUES,
                "arch": {"-e", "-d"}, "flock": {"-w", "--timeout", "-E", "--conflict-exit-code"}}


def _wrapped(n, args):
    """The command a wrapper runs, as argv: a command line it hands to a shell comes back as `sh -c <line>`, and one
    GNU parallel appends its inputs to ends with `{}` (known only when it runs). [] when it runs none."""
    if n in ("parallel", "sem"):
        return _parallel_command([str(a) for a in args], appends=n == "parallel")
    takes, i, direct = _WRAP_VALUES.get(n, set()), 0, False
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if a == "--":
            i += 1
            break
        if n in ("script", "flock") and (a in ("-c", "--command") or a.startswith("--command=")):
            line = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            return ["sh", "-c", line] if line else []
        direct = direct or (n == "watch" and a in ("-x", "--exec"))
        i += 2 if a in takes else 1
    rest = args[i:]
    if n in ("script", "flock"):  # script [file [command …]] (BSD), flock <file> <command …>
        return rest[1:]
    if n == "watch":  # without -x, watch hands the words to `sh -c`
        return rest if direct or not rest else ["sh", "-c", " ".join(rest)]
    return rest


def _parallel_option(a, args, i):
    """One GNU parallel option at args[i] -> (index after it and its value, [replacement strings it names]). A short
    cluster ends at a flag that takes a value (`-kj1`, `-kI%`); a long option is matched by a unique prefix, with its
    value after `=` or in the next word; an optional value (-i, --replace, -e, -l) is the next word when that word is
    no option, and for -l only a number, as parallel's option parser reads them."""
    nxt = args[i + 1] if i + 1 < len(args) else None
    if a.startswith("--"):
        name, eq, val = a.partition("=")
        reps = [r for r in _PARALLEL_REPLACES if r == name] or \
            [r for r in _PARALLEL_REPLACES if len(name) > 3 and r.startswith(name) and name not in _PARALLEL_VALUES]
        if name in _PARALLEL_OPTIONAL or any(r == "--replace" for r in reps) and not eq:
            key = name if name in _PARALLEL_OPTIONAL else "--replace"
            if not eq and nxt is not None and not nxt.startswith("-") and (key != "--max-lines" or nxt.isdigit()):
                val, i = nxt, i + 1
            elif not eq:
                val = _PARALLEL_OPTIONAL[key] or ""
        elif not eq and (name in _PARALLEL_VALUES or reps):
            val, i = (nxt or ""), i + 1
        if reps:
            return i + 1, [val.split()[0] if "--rpl" in reps and val.split() else val]
        return i + 1, []
    k = 1
    while k < len(a):
        c = "-" + a[k]
        if c in _PARALLEL_OPTIONAL or c in _PARALLEL_VALUES:
            val = a[k + 1:]
            if not val and c in _PARALLEL_VALUES:
                val, i = (nxt or ""), i + 1
            elif not val:
                if nxt is not None and not nxt.startswith("-") and (c != "-l" or nxt.isdigit()):
                    val, i = nxt, i + 1
                else:
                    val = _PARALLEL_OPTIONAL[c] or ""
            return i + 1, [val] if c in ("-I", "-i") else []
        k += 1
    return i + 1, []


def _parallel_command(args, appends: bool = True) -> list:
    """The command GNU parallel (or sem) runs, as argv, with `{}` where what it reads goes: each string an option
    names for its input (-I, --replace, --er, --bnr, --dnr, --rpl, …) and each {…} written `{}` (_BRACED), and `{}`
    appended when the command has none, as parallel appends its input (`appends`; sem reads no input and runs its
    command as written). A command given as one word with whitespace is a shell line (`parallel 'gzip -9 {}' ::: a`):
    `sh -c <line>`. With no command, each input is a command line (`:::` words) or a line of a file (`::::`, -a,
    --arg-file), else of its own input: `sh -c`, `sh <file>`, or `sh -s`."""
    reps, files, i = [], [], 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        if a == "--":
            i += 1
            break
        if a in ("-a", "--arg-file"):  # a file of inputs, each a command line when no command is given
            files.append(args[i + 1] if i + 1 < len(args) else "")
        elif a.startswith("--arg-file="):
            files.append(a.split("=", 1)[1])
        elif a.startswith("-a"):
            files.append(a[2:])
        i, more = _parallel_option(a, args, i)
        reps += more
    rest = [str(x) for x in args[i:]]
    cut = next((k for k, x in enumerate(rest) if x in (":::", "::::", ":::+", "::::+")), len(rest))
    cmd, inputs = rest[:cut], rest[cut + 1:]
    if cut < len(rest) and rest[cut].startswith("::::"):
        files, inputs = files + [x for x in inputs if x not in (":::", "::::", ":::+", "::::+")], []
    if not cmd:
        if inputs:
            return ["sh", "-c", "; ".join(inputs)]
        return ["sh", files[0]] if files else ["sh", "-s"]
    for r in (r for r in reps if r):
        cmd = [w.replace(r, "{}") for w in cmd]
    cmd = [_BRACED.sub("{}", w) for w in cmd]
    if len(cmd) == 1 and any(ch.isspace() for ch in cmd[0]):
        return ["sh", "-c", cmd[0]]
    return cmd if not appends or any("{}" in w for w in cmd) else cmd + ["{}"]


# screen, tmux and expect start a command in a terminal of their own, which is re-parented to launchd and has a pty;
# devkit reads the command each one starts, as a shell line where the tool hands it to one. What screen and tmux
# would type into a session is not read: the reader reports it as such (terminal_typing_refusal). Screen
# expands $VAR, ${VAR} and $:cap: in each -X word and `stuff -k` types a terminal key, so no reading of it held.
_SCREEN_FLAGS = frozenset("46aAdDfiLlmnOqrRUvx")  # screen's options without a value (-dm, -fn, -ln, -U, …)
_SCREEN_VALUES = frozenset("ehpsStT")  # with one: the rest of the word, or the next word
_SCREEN_SESSION = frozenset("dDrRx")  # last in its word, the next word may name a session (-r x, -x x, -d x)


def _screen_parse(args, more: bool = False) -> tuple:
    """-> (refused, command): whether a screen call may send a command to a session or holds what devkit does not
    read, and the command it starts ([] for none). Its options are read as screen reads them: letters in one word,
    a value as the rest of the word or the next word, and an optional session name after -d, -D, -r, -R, -x, -ls,
    -list and -wipe. Refused: -X and -Q (they send a screen command: stuff, exec, …), -c (a configuration file), a
    letter or long option screen does not have, a word the shell or a runner fills in (it may be -X), a session name
    followed by more words (the command may start at either), and with `more` (xargs adds words) a call whose
    command the words it adds could still change."""
    i, n = 0, len(args)
    while i < n:
        a = args[i]
        if _shell_made(a):
            return True, []
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        i += 1
        if a == "-Logfile":
            i += 1
            continue
        if a.startswith("--"):
            if a in ("--help", "--version"):
                continue
            return True, []
        session = a in ("-ls", "-list", "-wipe")
        for k, c in enumerate(a[1:] if not session else "", 1):
            if c in _SCREEN_VALUES:
                if k == len(a) - 1:
                    if i < n and _shell_made(args[i]):
                        return True, []
                    i += 1
                break
            if c not in _SCREEN_FLAGS:
                return True, []
            session = c in _SCREEN_SESSION and k == len(a) - 1
        if session and i < n and not args[i].startswith("-"):
            if _shell_made(args[i]) or i + 1 < n:
                return True, []
            i += 1
    command = list(args[i:])
    if command and _shell_made(command[0]):
        return True, []
    return (more and not command), command


_TMUX_COMMANDS = {  # every tmux command (3.5) -> its alias; tmux also takes a unique prefix of the name
    "attach-session": "attach", "bind-key": "bind", "break-pane": "breakp", "capture-pane": "capturep",
    "choose-buffer": "", "choose-client": "", "choose-tree": "", "clear-history": "clearhist",
    "clear-prompt-history": "clearphist", "clock-mode": "", "command-prompt": "", "confirm-before": "confirm",
    "copy-mode": "", "customize-mode": "", "delete-buffer": "deleteb", "detach-client": "detach",
    "display-menu": "menu", "display-message": "display", "display-panes": "displayp", "display-popup": "popup",
    "find-window": "findw", "has-session": "has", "if-shell": "if", "join-pane": "joinp", "kill-pane": "killp",
    "kill-server": "", "kill-session": "", "kill-window": "killw", "last-pane": "lastp", "last-window": "last",
    "link-window": "linkw", "list-buffers": "lsb", "list-clients": "lsc", "list-commands": "lscm",
    "list-keys": "lsk", "list-panes": "lsp", "list-sessions": "ls", "list-windows": "lsw", "load-buffer": "loadb",
    "lock-client": "lockc", "lock-server": "lock", "lock-session": "locks", "move-pane": "movep",
    "move-window": "movew", "new-session": "new", "new-window": "neww", "next-layout": "nextl",
    "next-window": "next", "paste-buffer": "pasteb", "pipe-pane": "pipep", "previous-layout": "prevl",
    "previous-window": "prev", "refresh-client": "refresh", "rename-session": "rename", "rename-window": "renamew",
    "resize-pane": "resizep", "resize-window": "resizew", "respawn-pane": "respawnp", "respawn-window": "respawnw",
    "rotate-window": "rotatew", "run-shell": "run", "save-buffer": "saveb", "select-layout": "selectl",
    "select-pane": "selectp", "select-window": "selectw", "send-keys": "send", "send-prefix": "",
    "server-access": "", "set-buffer": "setb", "set-environment": "setenv", "set-hook": "", "set-option": "set",
    "set-window-option": "setw", "show-buffer": "showb", "show-environment": "showenv", "show-hooks": "",
    "show-messages": "showmsgs", "show-options": "show", "show-prompt-history": "showphist",
    "show-window-options": "showw", "source-file": "source", "split-window": "splitw", "start-server": "start",
    "suspend-client": "suspendc", "swap-pane": "swapp", "swap-window": "swapw", "switch-client": "switchc",
    "unbind-key": "unbind", "unlink-window": "unlinkw", "wait-for": "wait",
}
_TMUX_ALIASES = {**{a: c for c, a in _TMUX_COMMANDS.items() if a}, "split-pane": "split-window",
                 "splitp": "split-window", "server-info": "show-messages", "info": "show-messages",
                 "choose-window": "choose-tree", "choose-session": "choose-tree"}  # and tmux's default command-alias
_TMUX_TYPES = frozenset({"send-keys", "send-prefix", "paste-buffer", "set-buffer", "load-buffer", "pipe-pane",
                         "command-prompt"})
_TMUX_CARRIES = {  # a command that runs the command (or the file of commands) it carries -> its options that take a
    # value, and how many words it takes before that command
    "run-shell": ("cdt", 0), "if-shell": ("t", 0), "source-file": ("t", 0), "set-hook": ("t", 1),
    "bind-key": ("NT", 1), "confirm-before": ("cpt", 0), "display-menu": ("bcCHsStTxy", 0),
    "display-panes": ("dt", 0), "choose-tree": ("FfKOt", 0), "choose-buffer": ("FfKOt", 0),
    "choose-client": ("FfKOt", 0),
}
_TMUX_STARTS = {  # a command that starts a shell command (read as one) -> its options that take a value
    "new-session": "ceFfnstxy", "new-window": "ceFnt", "split-window": "ceFlpt", "respawn-pane": "cet",
    "respawn-window": "cet", "display-popup": "bcdehsStTwxy",
}
_TMUX_SETS = frozenset({"set-option", "set-window-option"})
# the options whose value is a command tmux runs (a hook is one too), matched as tmux matches a name: by a prefix
_TMUX_RUN_OPTIONS = ("default-command", "default-shell", "command-alias", "copy-command", "editor", "lock-command",
                     "after-", "alert-activity", "alert-bell", "alert-silence", "client-active", "client-attached",
                     "client-detached", "client-focus-in", "client-focus-out", "client-resized",
                     "client-session-changed", "client-light-theme", "client-dark-theme", "command-error",
                     "pane-died", "pane-exited", "pane-focus-in", "pane-focus-out", "pane-mode-changed",
                     "pane-set-clipboard", "pane-title-changed", "session-closed", "session-created",
                     "session-renamed", "session-window-changed", "window-layout-changed", "window-linked",
                     "window-pane-changed", "window-renamed", "window-resized", "window-unlinked")
_TMUX_FORMAT = re.compile(r"#\{[^{}]*\}")  # a format, whose braces and ? the shell leaves alone in a quoted word
_RUNNER_FILLS = re.compile(r"(?<!#)\{\d*(?:\.|//?|/\.|#|%)?\}")  # {} and parallel's {.} {/} {#} {%} {1} …
# a format with the E or T modifier, also in a `;` list of modifiers (#{E:…}, #{T;=10:…}, #{p10;E:…}): tmux expands the
# value again, so a value built from pieces (an option set to `#`, then `(cmd)`) becomes a #() that runs
_TMUX_REEXPANDS = re.compile(r"#\{[^}]*?(?<=[{;])[ET][;:]")


def _tmux_name(word):
    """The tmux command `word` names, as tmux finds it: an alias, the full name, or a unique prefix of one; None for a
    word that names no command or more than one."""
    if word in _TMUX_ALIASES:
        return _TMUX_ALIASES[word]
    if word in _TMUX_COMMANDS:
        return word
    hits = [c for c in _TMUX_COMMANDS if word and c.startswith(word)]
    return hits[0] if len(hits) == 1 else None


def _tmux_unread(word, line: bool = False) -> bool:
    """A tmux word devkit cannot read: `#(…)` (a format that runs a shell command), a format that expands a value
    again (the E or T modifier, _TMUX_REEXPANDS), `$` or a backtick anywhere (the shell can build a
    `#(` from them, as `"$X(cmd)"` with X='#' does), or, in a word without whitespace (it may be unquoted), a glob, a
    brace expansion or a runner's {}: the shell or the runner may make it a `;` and a command. `line`: a word of the
    shell command a command starts (_TMUX_STARTS), which devkit reads as `sh -c`, so there `$` and a backtick count
    only in a word without whitespace (`tmux new -d 'echo $HOME'` stays open). A format's own braces and ? are left
    alone."""
    w = str(word)
    if "#(" in w or _TMUX_REEXPANDS.search(w) or (not line and ("$" in w or "`" in w)):
        return True
    if any(c.isspace() for c in w):
        return False
    while _TMUX_FORMAT.search(w):
        w = _TMUX_FORMAT.sub("", w)
    return "$" in w or "`" in w or bool(set(w) & set("*?[")) or bool(_RUNNER_FILLS.search(w)) or \
        bool(re.search(r"\{[^{}]*(?:,|\.\.)[^{}]*\}", w))


def _tmux_args(words, values) -> tuple:
    """-> (the option letters, the words after the options) of one tmux command, read as tmux reads them: letters in
    one word, a value as the rest of the word or the next word, options until `--` or the first other word."""
    letters, k = set(), 0
    while k < len(words) and words[k].startswith("-") and len(words[k]) > 1:
        w = words[k]
        k += 1
        if w == "--":
            break
        for j, c in enumerate(w[1:], 1):
            letters.add(c)
            if c in values:
                k += 0 if j < len(w) - 1 else 1
                break
    return letters, list(words[k:])


def _tmux_one(cmd) -> tuple:
    """-> (refused, line) for one tmux command: refused when it types into a pane, carries a command, sets an option
    whose value is one or appends to or expands formats into a value (-a, -F), is a command devkit does not know,
    or holds a word it cannot read; the shell line of a command it starts (new-session, new-window, split-window,
    respawn-*, display-popup), else None."""
    if not cmd:
        return False, None
    name = _tmux_name(cmd[0])
    line = _tmux_args(cmd[1:], _TMUX_STARTS[name])[1] if name in _TMUX_STARTS else []
    if name is None or name in _TMUX_TYPES or any(_tmux_unread(w) for w in cmd[:len(cmd) - len(line)]) or \
            any(_tmux_unread(w, line=True) for w in line):
        return True, None
    if name in _TMUX_CARRIES:
        values, names = _TMUX_CARRIES[name]
        letters, pos = _tmux_args(cmd[1:], values)
        return len(pos) > names or (name == "set-hook" and "R" in letters), None
    if name in _TMUX_SETS:
        letters, pos = _tmux_args(cmd[1:], "t")
        if letters & {"a", "F"}:  # a value built from pieces (`#`, then `(cmd)`) can be a #() no word shows
            return True, None
        option = (pos or [""])[0].split("[", 1)[0]
        return bool(option) and any(o.startswith(option) or option.startswith(o) for o in _TMUX_RUN_OPTIONS), None
    if name in _TMUX_STARTS:
        pos = _tmux_args(cmd[1:], _TMUX_STARTS[name])[1]
        return False, " ".join(pos) or None
    return False, None


def _tmux_parse(args, more: bool = False) -> tuple:
    """-> (refused, command): whether a tmux call types into a pane or runs what devkit does not read
    (_tmux_one; a `;` list, from a word that is or ends in `;`; a configuration file, -f; with `more`, the words xargs
    adds, which may start a command after a `;`), and what it starts, as `sh -c <line>`: the shell command of a
    command that starts one, or the line of `tmux -c <line>`. [] when it starts none (`tmux ls`, `tmux attach`).
    Agents have no need for tmux command lists, so a list is not read (tmux_list_refusal for the list the tokenizer
    splits at `\\;`)."""
    i, n = 0, len(args)
    while i < n and args[i].startswith("-") and args[i] != "-":
        a = args[i]
        i += 1
        if a == "--":
            break
        if _tmux_unread(a):
            return True, []
        for k, c in enumerate(a[1:], 1):
            if c in "cfLST":
                value = a[k + 1:]
                if not value:
                    value, i = (args[i] if i < n else ""), i + 1
                if c == "f" or _tmux_unread(value):
                    return True, []
                if c == "c":  # tmux -c runs the line in a shell, as a login shell does, and no tmux command
                    return False, ["sh", "-c", value]
                break
            if c not in "2CDdlNquUvV":
                return True, []
    if more:
        return True, []
    words = args[i:]
    if any(w.endswith(";") for w in words):  # tmux ends a command at a word that ends in `;` (`x;`, `'sh;'`, `"\\;"`)
        return True, []
    refused, line = _tmux_one(words)
    return refused, ([] if refused or not line else ["sh", "-c", line])


# whitespace within the line only: a newline is a separator itself (\s* after one spanned every line after it)
EXPECT_RUN = re.compile(r"(?:^|[;\n\[{]|\bthen\b)[ \t]*(?:spawn|exec|system)[ \t]+([^;\n\]}]*)")
TCL = frozenset({"tclsh", "expect", "wish"})
_EXPECT_SPAWN_OPTS = frozenset({"-noecho", "-console", "-nottycopy", "-nottyinit", "-pty", "-ignore", "-open",
                                "-leaveopen"})


def _tcl_lines(script) -> list:
    """The command lines a Tcl program (tclsh, expect, wish) starts: each `spawn`, `exec` and `system` command, its
    Tcl words read as shell words, past spawn's own options."""
    lines = []
    for m in EXPECT_RUN.finditer(script or ""):
        words = m.group(1).replace("{", " ").replace("}", " ").split()
        while words and words[0] in _EXPECT_SPAWN_OPTS:
            words = words[2:] if words[0] in ("-ignore", "-open", "-leaveopen") else words[1:]
        if words:
            lines.append(" ".join(words))
    return lines


def _expect_command(args):
    """What `expect -c '<script>'` runs, as `sh -c <lines>` (_tcl_lines); [] when it runs none devkit can see.
    None when expect also runs a script file or its input (`expect x.exp`, `expect -f x.exp`, `expect < x.exp`): it is
    then an interpreter whose program the walkers read from the folder the command runs in (interpreter_program)."""
    scripts, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "-c":
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if a in ("-f", "-b", "--", "-") or not a.startswith("-"):
            return None
        i += 2 if a == "-D" else 1
    if not scripts:
        return None  # no -c: it reads its commands from its input
    lines = [line for s in scripts for line in _tcl_lines(s)]
    return ["sh", "-c", "; ".join(lines)] if lines else []


_PY_FLAGS = frozenset("uIBEsSOqbdvxRP")  # python's own flags without a value; -W and -X take one


def dev_args(argv):
    """The arguments of a `dev` call (dev, bin/dev, python3 bin/dev, python3 -m devkit.cli, also after the
    interpreter's own flags: python3 -u -I -B …), else None. An empty argv (an `xargs` with no command, which runs
    echo, after xargs_command and unwrap) is no dev call."""
    if not argv:
        return None
    n = os.path.basename(argv[0])
    if n == "dev":
        return _dev_command(argv[1:])
    if INTERP.match(n) and len(argv) > 1:
        i = 1
        while i < len(argv) and argv[i].startswith("-") and argv[i] not in ("-m", "-c", "-", "--"):
            a = argv[i]
            if a in ("-W", "-X"):
                i += 2
            elif a[1:2] in ("W", "X") or set(a[1:]) <= _PY_FLAGS:
                i += 1
            else:
                break
        if i < len(argv) and os.path.basename(argv[i]) == "dev":
            return _dev_command(argv[i + 1:])
        if argv[i:i + 2] in (["-m", "devkit"], ["-m", "devkit.cli"]):
            return _dev_command(argv[i + 2:])
    return None


def runner_fills(word) -> bool:
    """A word a runner fills in when it runs: `{}` (find, xargs' replace string as xargs_command writes it) and GNU
    parallel's {.} {/} {#} {1} …"""
    return bool(_RUNNER_FILLS.search(str(word)))


_SHELL_VALUE_OPTS = ("-o", "+o", "-O", "+O", "--rcfile", "--init-file")
# per interpreter family: the options whose value is its program (code, a script or a module), the ones whose value
# is code it loads before it (node -r, perl -M, ruby -r), and the ones whose value is no code (python -W ignore)
_PROGRAM_OPTS = {"python": ("-c", "-m"), "node": ("-e", "--eval", "-p", "--print"), "perl": ("-e", "-E"),
                 "ruby": ("-e",), "php": ("-r", "-f"), "awk": ("-f",), "expect": ("-c", "-f", "-b"), "": ("-e",)}
_LOAD_OPTS = {"node": ("-r", "--require", "--import", "--loader", "--experimental-loader", "--preload"),
              "perl": ("-M", "-m", "-I"), "ruby": ("-r", "-I")}
_PLAIN_VALUE_OPTS = {"python": ("-W", "-X"), "node": ("--env-file", "--input-type", "-C", "--conditions", "--title"),
                     "php": ("-d", "-c"), "awk": ("-v", "-F")}
_FAMILY = {"pypy": "python", "nodejs": "node", "bun": "node", "deno": "node", "gawk": "awk", "mawk": "awk",
           "nawk": "awk"}


def _option_value(args, i, letter_at):
    """The value of the option at args[i] whose letter is at `letter_at` in a short cluster (`-Bc code`, `-pe code`,
    `-c code`), or after `=` in a long one: the rest of the word, else the next word -> (value or None, next index)."""
    a = args[i]
    if a.startswith("--"):
        if "=" in a:
            return a.split("=", 1)[1], i + 1
        return (args[i + 1], i + 2) if i + 1 < len(args) else (None, i + 1)
    rest = a[letter_at + 1:]
    if rest:
        return rest, i + 1
    return (args[i + 1], i + 2) if i + 1 < len(args) else (None, i + 1)


def program_source(run, whole: bool = False) -> str | None:
    """How a shell or interpreter in a runner's command gets its program: 'filled' when its code, script or module, or
    code it loads first, is a word the runner fills in (`sh {}`, `python3 -c {}`, `node -r {}`, `. {}`), 'none' when
    the words give it no program (only its options, or nothing, or -s: it reads its input), 'given' when they do;
    None for another program. `whole`: only a word that is a fill by itself counts (`sh {}`), not one that holds a
    fill (`python3 -c 'json.dump({}, f)'`), for words no runner is known to fill (a segment's own command)."""
    filled = (lambda w: bool(_RUNNER_FILLS.fullmatch(str(w).strip()))) if whole else runner_fills
    name = os.path.basename(str(run[0]))
    args = [str(a) for a in run[1:]]
    if name in ("source", "."):
        return "filled" if args and filled(args[0]) else "given" if args else "none"
    shell = name in SHELLS
    if not shell and not INTERP.match(name):
        return None
    fam = re.sub(r"[0-9.]+$", "", name.casefold())
    fam = _FAMILY.get(fam, fam)
    program = _PROGRAM_OPTS.get(fam, _PROGRAM_OPTS[""])
    loads, plain = _LOAD_OPTS.get(fam, ()), _PLAIN_VALUE_OPTS.get(fam, ())
    i = 0
    while i < len(args):
        a = args[i]
        if filled(a):
            return "filled"
        if a == "--":
            i += 1
            break
        if a == "-" or (shell and a.startswith("-") and not a.startswith("--") and "s" in a[1:]):
            return "none"  # it reads its program from its input
        if shell and a.startswith("-") and not a.startswith("--") and "c" in a[1:]:
            # a line the input is only part of is read word by word where it runs (_input_runs, the walker); the
            # line is the first word after the options (shell_args)
            kind, k, _ = shell_args(args, name)
            line = args[k].strip() if kind == "line" else None
            return "none" if line is None else "filled" if _RUNNER_FILLS.fullmatch(line) else "given"
        if shell and a in _SHELL_VALUE_OPTS:
            i += 2
            continue
        if not shell and a.startswith("--"):
            flag = a.split("=", 1)[0]
            if flag in program or flag in loads:
                val, i = _option_value(args, i, 0)
                if val is not None and filled(val):
                    return "filled"
                if flag in program:
                    return "given" if val else "none"
                continue
            i += 2 if flag in plain and "=" not in a else 1
            continue
        if not shell and a.startswith("-") and len(a) > 1:
            hit = next((k for k in range(1, len(a)) if "-" + a[k] in program or "-" + a[k] in loads), None)
            if hit is None:
                i += 2 if a in plain else 1
                continue
            val, i = _option_value(args, i, hit)
            if val is not None and filled(val):
                return "filled"
            if "-" + a[hit] in program:
                return "given" if val else "none"
            continue
        if a.startswith(("-", "+")):
            i += 1
            continue
        return "given"  # the script (an awk program), with what follows as its arguments
    rest = args[i:]
    if rest:
        return "filled" if filled(rest[0]) else "given"
    return "none"


def _dev_command(args) -> list:
    """A dev call's words from its command on. dev's top-level parser takes only -h/--help and a `--` before the
    command, and neither hides it: `dev -- install` runs install, and an older dev runs `dev -h release`."""
    i = 0
    while i < len(args) and (args[i] in ("--", "-h") or (len(args[i]) > 2 and "--help".startswith(args[i]))):
        i += 1
    return args[i:]


# How devkit reads a dev command's flags: the way dev's parser does. argparse ends the options at `--` (a
# later word is an argument: `dev backup x -- --dry-run` pushes x), keeps the last of a repeated option, and takes a
# short option's value attached (`-px`). dev's parsers take no abbreviations; an older checkout's bin/dev does, so a
# prefix of a flag that adds an action counts as the flag, while only the exact flag skips one.
def _dev_opts(args) -> list:
    """The words dev's parser reads as options: those before the `--` that ends them."""
    return args[:args.index("--")] if "--" in args else list(args)


def _dev_skips(args, *flags) -> bool:
    """True when dev's parser reads one of `flags`, each of which skips an action (--dry-run, --help, --offline): the
    exact flag before `--`. A prefix skips nothing, and after `--` the word is an argument."""
    return any(a in flags for a in _dev_opts(args))


def dev_sets(args, *flags) -> str:
    """The first of `flags` a dev call sets, each of which adds an action or a refusal: the flag, `--flag=value`, a
    prefix of a long flag (`--sched`), or a short flag in a cluster before a value (`-xo`, `-o/tmp/x`). Counted after
    `--` too, so a spelling devkit does not expect fails closed."""
    for a in args:
        if a.startswith("--"):
            w = a.split("=", 1)[0]
            hit = next((f for f in flags if f.startswith("--") and len(w) > 2 and f.startswith(w)), "")
        elif a[:1] == "-" and len(a) > 1:
            hit = ""
            for c in a[1:]:
                hit = next((f for f in flags if f == "-" + c), "")
                if hit or "-" + c in _DEV_VALUES:
                    break
        else:
            continue
        if hit:
            return hit
    return ""


def dev_words(args) -> list:
    """The words dev's parser reads as arguments (command, subcommand, branches, PR numbers): an option's value is
    not one (`dev lesson --search add retire` runs retire), and after `--` every word is one."""
    return positionals(args, _DEV_VALUES)


_POSITIONAL = re.compile(r'"\$[@*]"|\$[@*]|\$\{(\d+)\}|\$(\d)')


# the words that can give a line's $1… other values than the words after it, wherever they stand once quotes are
# removed (`s""hift` is shift): shift, getopts' shift, eval and source, which run text devkit reads as a string,
# zsh's argv, and a function, whose $1… are its own arguments
_REBINDERS = frozenset({"shift", "getopts", "eval", "source", "function", "argv"})
_FUNCTION_DEF = re.compile(r"\([ \t]*\)")  # `f() …`, `f ()`, zsh's anonymous `() { … }`


_LOOPS = frozenset({"while", "until", "for", "select", "repeat", "foreach"})


def _rebind_cut(line: str) -> int:
    """How much of a -c line sees the words after it as $0, $1…: the index where the first thing that may give them
    other values takes effect, read as the shell reads its words once quotes are removed (`s""hift` is shift). That
    is the end of the command holding shift, getopts, source, `.`, set with operands, zsh's argv or `1=…` (their own
    words still see the old values); the start of eval, whose text may define a function, of a function (`function
    f`, the `(` of `f()` or of zsh's anonymous `() { … }`), and of a for or select loop without an `in` list, which
    loops over them. 0 when a loop before that point may run its text
    again after it, or when devkit cannot read the line's words (backticks, `$'…'`); len(line) when nothing can."""
    words, ck = _shell_words(line), _code_kinds(line)
    if words is None or ck is None:
        return 0
    code = "".join(c if k == "C" else " " for c, k in zip(line, ck[0]))
    m = _FUNCTION_DEF.search(code)
    cut = m.start() if m else len(line)
    for k, (text, offs, _, at_cmd) in enumerate(words):
        if not offs or offs[0] >= cut:
            continue
        rest = [w[0] for w in words[k + 1:]]
        if (at_cmd and text in ("for", "select") and (len(rest) < 2 or rest[1] != "in")) or \
                text in ("eval", "function"):  # text that runs later, maybe as a function with its own $1
            cut = min(cut, offs[0])
            break
        rebinds = text in _REBINDERS or bool(re.match(r"(?:\d+|argv(?:\[[^]]*\])?)\+?=", text)) or \
            (at_cmd and text == ".")
        if at_cmd and text == "set":
            j = 0
            while j < len(rest) and not words[k + 1 + j][3]:
                opt = rest[j]
                if not re.fullmatch(r"[-+][A-Za-z]+", opt) or "A" in opt:  # an operand, `--`, `-` or zsh's -A
                    rebinds = True
                    break
                j += 2 if opt.endswith("o") else 1  # `-o pipefail`: the option's name
        if rebinds:
            end = re.search(r"[;&|\n]", code[offs[-1]:])
            cut = min(cut, offs[-1] + end.start() if end else len(line))
            break
    if cut < len(line) and any(at_cmd and text in _LOOPS and offs and offs[0] < cut
                               for text, offs, _, at_cmd in words):
        return 0
    return cut


def with_positionals(line: str, words) -> str:
    """A shell's -c line with the words after it in place of $0, $1…, ${1} and "$@" or $* (the words after $0),
    quoted as the shell passes them, so devkit reads what runs (`sh -c 'claude "$@"' _ plugin install x`). A line
    that can bind them to other values (_rebind_cut: `shift; $1 …`, `s""et -- gh; "$@"`) keeps them as variables from
    there on, which the gate judges as unknown where they name a program."""
    cut = _rebind_cut(line) if words else 0
    if not cut:
        return line

    def sub(m):
        n = m.group(1) or m.group(2)
        if n is None:
            return " ".join(shlex.quote(w) for w in words[1:])
        return shlex.quote(words[int(n)]) if int(n) < len(words) else ""
    return _POSITIONAL.sub(sub, line[:cut]) + line[cut:]


# the files that are a command's input or another of its descriptors: what `source`, `.`, a shell or an interpreter
# reads from them is what the command feeds it
_FED_FILES = re.compile(r"/dev/(?:stdin|fd/\d+)|/proc/(?:self|thread-self|\d+)/fd/\d+")


def _descriptor(word, cwd):
    """The descriptor file `word` opens (`/dev/stdin`, `/dev/fd/3`, `/proc/self/fd/0`) as the system opens it: made
    absolute in the folder the command runs in, with `//`, `.` and `..` read as the kernel reads them and each
    symbolic link followed, so `/dev/fd//0`, `//dev/stdin`, `cd /dev && . stdin` and a link to /dev/stdin are all
    its input. False for any other file; None when devkit cannot tell which file it is (a variable, a
    substitution, a glob, a folder only the run knows, a loop of links)."""
    word = str(word)
    if not word or re.search(r"[$`*?\[]", word):
        return None
    p = _path_text(word)
    if p is None:
        return None
    if not os.path.isabs(p):
        if cwd is None or str(cwd) == str(UNKNOWN):
            return None
        p = os.path.join(_c_path(cwd), p)
    parts, at, hops = [x for x in p.split("/") if x], "/", 0
    while True:
        whole = os.path.normpath(os.path.join(at, *parts))
        if _FED_FILES.fullmatch(whole):
            return whole
        if not parts:
            return False
        part = parts.pop(0)
        if part == ".":
            continue
        if part == "..":
            at = os.path.dirname(at)
            continue
        step = os.path.join(at, part)
        try:
            link = os.readlink(step)
        except (OSError, ValueError):  # no link (or nothing there): the path goes on from it
            at = step
            continue
        hops += 1
        if hops > 40:
            return None
        parts, at = [x for x in link.split("/") if x] + parts, "/" if link.startswith("/") else at


def _source_args(args) -> list:
    """The words of `source` or `.` after `--`, which ends their options in bash and zsh."""
    return list(args[1:]) if list(args[:1]) == ["--"] else list(args)


def _source_target(args, cwd):
    """Which file `source` or `.` reads, as _descriptor tells: its path ("<(" for a process substitution), False for
    a file devkit reads, None when devkit cannot tell (also after an option it does not read)."""
    args = _source_args(args)
    if not args:
        return False
    if args[0] == "<(":
        return "<("
    if args[0].startswith("-") and args[0] != "-":
        return None
    return _descriptor(args[0], cwd)


def _reads_input(word, cwd) -> bool:
    """True when the file `word` names is the command's input (_descriptor: `/dev/stdin`, `/dev/fd/0`, `//dev/stdin`,
    a link to it)."""
    d = word if isinstance(word, str) and _FED_FILES.fullmatch(word) else _descriptor(word, cwd)
    return bool(d) and bool(re.search(r"/(?:stdin|0)\Z", d))


def _sourced(args, cwd, bodies) -> list:
    """What `source` or `.` runs -> [(text, typed, path)], as for a shell's script (_shell_sources): the file it
    names; for its input or another descriptor (`. /dev/stdin <<< '…'`, `source /dev/fd/3`), the command's heredocs
    and here-strings and what a pipe feeds it (`bodies`), also when devkit cannot tell which file it reads
    (`source $f <<< '…'`); for a process substitution (`source <(echo …)`), what its command writes (_produced)."""
    target, args = _source_target(args, cwd), _source_args(args)
    if not args:
        return []
    if target == "<(":
        sub = unwrap(args[1:])
        return [(_produced(sub, cwd), False, _cat_path(sub, cwd))] if sub else []
    if target is not False:
        return [(b, True, None) for b in bodies]
    path = resolve(args[0], cwd)
    return [(_read_small(path), False, path)]


def _shell_inputs(args, cwd, bodies, name="bash") -> list:
    """The scripts a shell runs: its -c string, a script file, or its input (the command's heredocs). With -s it reads
    its input even when words follow, and they are its $1, $2 … (`bash -s plugin install x <<< 'claude "$@"'`)."""
    return [text for text, _, _ in _shell_sources(args, cwd, bodies, name)]


def _shell_sources(args, cwd, bodies, name="bash") -> list:
    """_shell_inputs as (text, typed, path): typed when the command holds the text (a -c line, a heredoc), False for a
    script file read from disk, whose resolved path is the third item (None for the others). A shell that only
    parses its program (`bash -n x.sh`, shell_args) runs none: []."""
    kind, k, parse_only = shell_args(args, name)
    if parse_only or kind == "none":
        return []
    if kind == "line":
        return [(with_positionals(args[k], args[k + 1:]), True, None)]
    if kind == "stdin":
        return [(with_positionals(b, ["sh", *args[k:]]), True, None) for b in bodies]
    if kind == "script" and _descriptor(args[k], cwd) is False:
        path = resolve(args[k], cwd)
        return [(_read_small(path), False, path)]
    if kind == "script":  # `bash /dev/stdin <<< '…'`, `bash $f <<< '…'`: what the command feeds it, as for source
        return [(b, True, None) for b in bodies]
    return [(b, True, None) for b in bodies]


def interpreter_program(argv, cwd, bodies):
    """-> (code, args, module, in_place): the program text an interpreter runs (inline, a script file or its input)."""
    fam = re.sub(r"[0-9.]+$", "", os.path.basename(argv[0]).casefold())
    args, code, script, module, inplace, i = argv[1:], [], None, None, False, 0
    # Node checks syntax without executing the target. Additional options can preload code,
    # so only the plain check form qualifies; every other form keeps normal inspection.
    if argv[0] in ("node", "nodejs") and len(args) == 2 and args[0] in ("--check", "-c") \
            and args[1] and not args[1].startswith("-") \
            and not os.environ.get("NODE_OPTIONS"):
        return "", [], None, False
    while i < len(args):
        a, nxt = args[i], args[i + 1] if i + 1 < len(args) else ""
        short = a.startswith("-") and not a.startswith("--") and len(a) > 1
        if fam in ("awk", "gawk", "mawk", "nawk"):
            if a in ("-i", "--include") and nxt.startswith("inplace") or \
                    re.fullmatch(r"(?:-i|--include=)inplace(?:\.awk)?", a):  # gawk rewrites its files in place
                inplace, i = True, i + (2 if a in ("-i", "--include") else 1)
                continue
            if a == "-f":
                script, i = nxt, i + 2
                continue
            if a in ("-v", "-F"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            if not code and script is None:
                code.append(a)
            i += 1
            break
        if fam == "expect":  # expect [-dinN] [-D n] [-c cmds]… [[-f|-b|--] cmdfile] [args]: -c runs before the file
            if a == "-c":
                code.append(nxt)
                i += 2
                continue
            if a in ("-f", "-b", "--"):
                script, i = nxt or None, i + 2
                break
            if a == "-" or not a.startswith("-"):
                script, i = a, i + 1
                break
            i += 2 if a == "-D" else 1
            continue
        if fam in ("python", "pypy"):
            if short and a.endswith("c"):
                code.append(nxt)
                i += 2
                break
            if a == "-m":
                module, i = nxt, i + 2
                break
        elif fam in ("perl", "ruby") and short and a[-1] in "eE" or \
                a in {"node": ("-e", "--eval", "-p", "--print"), "bun": ("-e", "--eval", "-p", "--print"),
                      "nodejs": ("-e", "--eval", "-p", "--print"), "deno": ("eval",), "php": ("-r",),
                      "osascript": ("-e",), "lua": ("-e",), "rscript": ("-e",), "swift": ("-e",)}.get(fam, ()):
            code.append(nxt)
            inplace = inplace or (fam in ("perl", "ruby") and "i" in a[1:-1])
            i += 2
            continue
        if short and fam in ("perl", "ruby") and "i" in a[1:]:
            inplace = True
        if a == "-":  # the program comes from the input (a heredoc); what follows are its arguments
            script, i = a, i + 1
            break
        if a.startswith("-"):
            i += 1
            continue
        if not code and script is None:
            script, i = a, i + 1
        break
    rest = args[i:]
    if script and script != "-" and _reads_input(script, cwd):  # its input, as `-` is
        script = "-"
    if fam in ("node", "nodejs", "bun", "deno"):  # the code node preloads (-r, --require=, --import=): read as its own
        for k, a in enumerate(args[:i]):
            flag, eq, val = a.partition("=")
            if flag in ("-r", "--require", "--import", "--loader", "--experimental-loader", "--preload"):
                val = val if eq else (args[k + 1] if k + 1 < len(args) else "")
                if val.startswith((".", "/", "~")) or os.sep in val:
                    code.append(_read_small(_run_target(resolve(val, cwd), fam)))
    if script and script != "-":
        code.append(_read_small(_run_target(resolve(script, cwd), fam)))
    elif module and fam in ("python", "pypy") and not any(re.fullmatch(r"-[A-Za-z]*[IP][A-Za-z]*", a)
                                                          for a in args[:i]):
        code.append(_read_small(_module_file(module, cwd)))  # `python3 -m pkg.run` from the working folder
    elif not code and module is None:
        code += list(bodies)
    return "\n".join(code), rest, module, inplace


def _run_target(path, fam):
    """The file an interpreter runs for `path`: for a folder, Python's `__main__.py`, or node's package.json main
    (else index.js); the path itself otherwise."""
    try:
        p = Path(str(path))
        if not p.is_dir():
            return path
        if fam in ("python", "pypy"):
            return p / "__main__.py"
        try:
            main = json.loads((p / "package.json").read_text()).get("main") or ""
        except (OSError, ValueError, AttributeError):
            main = ""
        for c in ([main, main + ".js"] if main else []) + ["index.js", "index.mjs", "index.cjs"]:
            if (p / c).is_file():
                return p / c
        return p / "index.js"
    except (OSError, ValueError):
        return path


def _module_file(module, cwd):
    """The file `python3 -m module` runs from the working folder, which comes first on its path: `a/b.py`, or
    `a/b/__main__.py` for a package; UNKNOWN when the working folder holds neither (the standard library's or an
    installed one, judged by its name)."""
    base = Path(str(cwd or os.getcwd())).joinpath(*module.split("."))
    for c in (base.with_name(base.name + ".py"), base / "__main__.py"):
        if c.is_file():
            return c
    return UNKNOWN


_CREW_LANE = frozenset({"ask", "answer", "inbox", "read", "ack", "decide", "decisions", "mode", "delegate", "decline", "route"})
_CREW_READ = frozenset({"graph", "show", "rules", "history", "next", "owner"})  # owner: who holds a file

_DATA_HEREDOC = re.compile(r"\$\(cat <<(['\"])([A-Za-z_][A-Za-z0-9_]*)\1\n")


def data_heredoc_end(cmd: str, i: int) -> int:
    """At a `$(` the shell expands: the end of `$(cat <<'TAG'` + body + a line `TAG` + `)`, a substitution that
    only prints its quoted heredoc (data, nothing runs), else -1. The body ends at its first `TAG` line, as in the
    shell, and `)` must follow that line at once. A body line that starts with the tag but is more than the tag
    (`TAG)`, `TAG )`) fails closed: bash 3.2 (macOS /bin/bash) ends the heredoc there and runs what follows, while
    zsh reads on, so the shells disagree on where the data stops (B-fy5f5i8c1hx)."""
    m = _DATA_HEREDOC.match(cmd, i)
    if not m:
        return -1
    tag, j = m.group(2), m.end()
    while True:
        nl = cmd.find("\n", j)
        if nl < 0:
            return -1
        line = cmd[j:nl]
        if line == tag:
            return nl + 2 if cmd[nl + 1:nl + 2] == ")" else -1
        if line.startswith(tag):
            return -1
        j = nl + 1


def runs_one_command(cmd: str) -> bool:
    """True when the shell does nothing but run this one program: no operator, redirect or newline outside
    quotes, no command substitution where the shell expands it (unquoted or in double quotes), no unterminated quote.
    A substitution that only prints a quoted heredoc (`"$(cat <<'EOF'` … `EOF` + `)"`, how a long message is passed
    to `dev crew ask`) is data: it runs `cat` and nothing else. Single quotes are literal: a `$(` inside them is text,
    so it never hides the operators around it."""
    i, n, q = 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if q == "'":
            q = None if c == "'" else q
        elif cmd.startswith("$(", i):
            end = data_heredoc_end(cmd, i)
            if end < 0:
                return False
            i = end
            continue
        elif q == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                q = None
            elif c == "`":
                return False
        elif c == "\\":
            i += 1
        elif c in "'\"":
            q = c
        elif c in ";&|<>\n`":
            return False
        i += 1
    return q is None


def arith_end(s: str, j: int):
    """The index of the `))` that closes the arithmetic `$((` whose body starts at s[j], or None when the inner
    parenthesis closes into anything else: bash and zsh then run `$((date)|wc -c)` as `$( (date) | wc -c )`."""
    depth, n, q = 0, len(s), None
    while j < n:
        c = s[j]
        if q:
            q = None if c == q else q
        elif c == "\\":
            j += 2
            continue
        elif c in "'\"`":
            q = c
        elif c == "(":
            depth += 1
        elif c == ")":
            if not depth:
                return j if s.startswith("))", j) else None
            depth -= 1
        j += 1
    return None


# the programs whose output is a number: a name the command sets from one of them is a number in arithmetic
_ARITH_BINDERS = re.compile(r"(?:^|[\s;&|(`{])(?:read|mapfile|readarray|printf|declare|typeset|local|readonly|export|"
                            r"getopts|unset|select|eval|source|\.)(?=\s)")
# an integer declaration (declare -i, typeset -i, local -i): its names take the arithmetic value of what they are
# given, so they bind no text; -n (a name reference) binds another name and stays a binder
_INT_DECL = re.compile(r"\b(?:declare|typeset|local|readonly|export)((?:[ \t]+-[A-Za-z]{1,8}){1,4})")


def numeric_pipeline(body: str) -> bool:
    """True when a substitution's command prints only a number: `wc` reading its input, `seq`, `date +%s`-like formats,
    `grep -c` on one file, `git rev-list --count`, or `grep -n … <one file> | cut -d: -f1`, any of them followed by
    head, tail, sort or uniq. Its output is spliced into an arithmetic expression, so any other output may be code."""
    if not body or re.search(r"[;&\n`]|\$\(", body):
        return False
    try:
        stages = [shlex.split(st) for st in re.split(r"(?<!\|)\|(?!\|)", body)]
    except ValueError:
        return False
    if not stages or any(not st for st in stages):
        return False
    while len(stages) > 1 and os.path.basename(stages[-1][0]) in ("head", "tail", "sort", "uniq"):
        stages.pop()

    def operands(words):
        out, skip = [], False
        for w in words:
            if skip:
                skip = False
            elif w == "<":
                skip = True
            elif not w.startswith(("-", "<")):
                out.append(w)
        return out
    last = stages[-1]
    name, args = os.path.basename(last[0]), last[1:]
    opts = [a for a in args if a.startswith("-") and not a.startswith("--")]
    if name == "wc":
        return not operands(args)
    if name == "seq":
        return not any(a.startswith(("-f", "-s")) for a in args)
    if name == "date":
        return any(re.fullmatch(r"\+(?:%[sYmdHMSjuwNyCVUW]|[0-9 :._-])+", a) for a in args)
    if name == "grep":
        return any("c" in o[1:] for o in opts) and not any(set(o[1:]) & set("rRH") for o in opts) and \
            len(operands(args)) <= 2
    if name == "git":
        return args[:1] == ["rev-list"] and "--count" in args
    if name == "cut" and "".join(args) in ("-d:-f1", "-f1-d:", "-d:-f1-1") and len(stages) >= 2:
        first = stages[0]
        fopts = [a for a in first[1:] if a.startswith("-") and not a.startswith("--")]
        return os.path.basename(first[0]) == "grep" and any("n" in o[1:] for o in fopts) and \
            not any(set(o[1:]) & set("rRHc") for o in fopts) and len(operands(first[1:])) == 2 and \
            all(os.path.basename(st[0]) in ("head", "tail", "sort", "uniq") for st in stages[1:-1])
    return False


def _number_value(value: str, s: str = "", seen=frozenset()) -> bool:
    """True when an assignment's value is a number: digits, an arithmetic `$((…))` (its own names are judged where it
    stands), a substitution that prints only a number (numeric_pipeline), or an expression of numbers and names
    that are numbers themselves (`i=i+1`, judged in command `s`)."""
    v = value
    if v.startswith("$((") and arith_end(v, 3) is not None:
        return True
    if v.startswith(("$(", '"$(')):
        k = 2 if v[0] == "$" else 3
        end = close_paren(v, k)
        return end > k and numeric_pipeline(v[k:end - 1])
    if v[:1] in ("'", '"'):
        close = v.find(v[0], 1)
        v = v[1:close] if close > 0 else ""
    else:
        v = re.match(r"[^\s;&|)]*", v).group(0)
    if re.fullmatch(r"-?[0-9]+", v):
        return True
    if not re.fullmatch(r"[A-Za-z0-9_+*/%()-]{1,200}", v):
        return False
    names = set(re.findall(r"(?<![0-9A-Za-z_#])[A-Za-z_][A-Za-z0-9_]*", v))
    return all(_arith_name_safe(n, s, seen) for n in names)


def _arith_name_safe(word: str, s: str, seen=frozenset()) -> bool:
    """True when command `s` gives name `word` numbers only: it sets it, every value it gives it is a number
    (_number_value, a name built from itself counting as its other values do), a `for` loop over numbers sets it
    otherwise, and nothing else in `s` can bind it (read, printf -v, declare without -i, eval, source, …)."""
    if word in seen:
        return True
    seen = seen | {word}
    w = re.escape(word)
    plain = _INT_DECL.sub(lambda m: m.group(0) if ("i" not in m.group(1) or "n" in m.group(1)) else
                          " " * len(m.group(0)), s)  # an integer declaration binds no text
    if re.search(_ARITH_BINDERS.pattern + r"[^;&|\n]*(?<![\w$])" + w + r"\b", plain) or \
            re.search(r"(?:^|[\s;&|(`{])(?:eval|source|\.)\s", s):
        return False
    values = [s[m.end():] for m in re.finditer(r"(?:^|[\s;&|(`{])" + w + r"\+?=(?!=)", s)]
    loops = re.findall(r"(?:^|[\s;&|(`{])for\s+" + w + r"\s+in\s+([^;\n]*)", s)
    return bool(values or loops) and all(_number_value(v, s, seen) for v in values) and \
        all(re.fullmatch(r"\s*\$\(seq(?:\s+-?[0-9]+){1,3}\)\s*", lst) or
            all(re.fullmatch(r"-?[0-9]+|\{-?[0-9]+\.\.-?[0-9]+\}", item) for item in lst.split()) for lst in loops)


def arith_unsafe_names(body: str, s: str) -> list:
    """What an arithmetic expression reads whose value may be code, as words a command would run: `$name` for a name
    command `s` does not give numbers only (_arith_name_safe), `$(…)` for a substitution whose output may be any text
    (numeric_pipeline). The shell reads each value as an expression, and `x='a[$(cmd)]'; echo $((x))` runs cmd."""
    bare, out = body, []
    for _ in range(8):  # each substitution is a command of its own; its output is spliced into the expression
        m = re.search(r"\$\(|`", bare)
        if not m:
            break
        if m.group(0) == "`":
            k = bare.find("`", m.end())
            inner = bare[m.end():k] if k >= 0 else bare[m.end():]
            bare = bare[:m.start()] + " 0 " + (bare[k + 1:] if k >= 0 else "")
        else:
            k = close_paren(bare, m.end())
            inner = bare[m.end():k - 1] if k > m.end() else bare[m.end():]
            bare = bare[:m.start()] + " 0 " + (bare[k:] if k > m.end() else "")
        if not numeric_pipeline(inner):
            out.append("$(" + inner + ")")
    names = sorted(set(re.findall(r"(?<![0-9A-Za-z_#])[A-Za-z_][A-Za-z0-9_]*", bare)))
    return out + ["$" + w for w in names if not _arith_name_safe(w, s)]


def _blank_single(s: str) -> str:
    """`s` with the text inside its single quotes blanked: the shell expands nothing there, while it expands
    `${…}` inside double quotes."""
    out, q = [], None
    for c in s:
        if q == "'":
            q = None if c == "'" else q
            out.append(c if c in "'\n" else " ")
        else:
            if c == '"':
                q = None if q == '"' else '"'
            elif c == "'" and q is None:
                q = "'"
            out.append(c)
    return "".join(out)


_LET = re.compile(r"(?:^|[;&|(\n{])[ \t]*(?:(?:then|do|else|!)[ \t]+)?let[ \t]+([^;&|\n)]{1,400})")
_TEST_ARITH = re.compile(r"(?<!\S)(\S{1,400})[ \t]+-(?:eq|ne|lt|le|gt|ge)[ \t]+(\S{1,400})")
_SUBSCRIPT_EXPAND = re.compile(r"\$\{[#!]?[A-Za-z_]\w{0,80}\[([^\]\n]{0,200})\]")
_SUBSCRIPT_SET = re.compile(r"(?:^|[;&|\s(])[A-Za-z_]\w{0,80}\[([^\]\n]{0,200})\]\+?=")
_SUBSCRIPT_WORD = re.compile(r"(?:^|[\s'\"])[A-Za-z_]\w{0,80}\[([^\]\n]{0,200})\]")
_SUBSCRIPT_CMD = re.compile(r"(?:^|[;&|\n(])[ \t]*(unset|read|printf)\b([^;&|\n]{0,400})")
_SUBSTRING = re.compile(r"\$\{[A-Za-z_]\w{0,80}:(?![-=+?])([^}\n]{0,400})\}")
_DOLLAR_BRACKET = re.compile(r"\$\[([^\]\n]{0,400})\]")


# a builtin that sets a variable by a name it is given, or `${!n}`: _name_targets reads them
_NAME_HINT = re.compile(r"\$\{!|\b(?:declare|typeset|local|readonly|export|printf|read|mapfile|readarray|getopts)\b")


def _unquote(w: str) -> str:
    return w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "'\"" else w


def arith_hazards(s: str) -> list:
    """What a command line reads as an arithmetic expression outside `$((…))` (read in subst) and whose value may be
    code (arith_unsafe_names): `((…))` and `for ((…))`, `let`, the operands of [[ … -eq … ]] and the other integer
    comparisons, `$[…]`, the values of an integer declaration and of the names it declares, a substring's offset and
    length, and an array subscript (also in zsh). `test` and `[` compare integers without evaluating them."""
    if not any(t in s for t in ("((", "let", "[[", "$[", "[", "-i", ":")) and not _NAME_HINT.search(s):
        return []
    try:
        text = strip_heredocs(s)
    except Exception:
        text = s
    plain, single = _blank_quoted(text), _blank_single(text)
    exprs = []
    i = 0
    while True:
        i = plain.find("((", i)
        if i < 0:
            break
        if i == 0 or plain[i - 1] not in "$<>=" and not plain[i - 1].isalnum():
            end = arith_end(text, i + 2)
            if end is not None:
                exprs.append(text[i + 2:end])
        i += 2
    for m in _LET.finditer(plain):
        exprs += [_unquote(w) for w in text[m.start(1):m.end(1)].split()]
    i = 0
    while True:
        i = plain.find("[[", i)
        if i < 0:
            break
        j = plain.find("]]", i + 2)
        span, held = text[i + 2:j if j > 0 else len(text)], []
        for _ in range(16):  # each substitution is one operand, whatever spaces it holds
            k = span.find("$(")
            if k < 0:
                break
            e = close_paren(span, k + 2)
            if e <= k:
                break
            held.append(span[k:e])
            span = span[:k] + f"\0{len(held) - 1}\0" + span[e:]
        for m in _TEST_ARITH.finditer(span):
            exprs += [re.sub(r"\0(\d+)\0", lambda h: held[int(h.group(1))], _unquote(g)) for g in m.groups()]
        i = j if j > 0 else len(text)
    ints = set()
    for m in _INT_DECL.finditer(plain):
        if "i" in m.group(1) and "n" not in m.group(1):
            rest = re.match(r"[^;&|\n]*", text[m.end():]).group(0)
            for w in rest.split():
                name, eq, val = w.partition("=")
                if re.fullmatch(r"[A-Za-z_]\w*", name):
                    ints.add(name)
    for name in ints:
        for m in re.finditer(r"(?:^|[\s;&|(`{])" + re.escape(name) + r"\+?=(?!=)", text):
            exprs.append(_unquote(re.match(r"\"[^\"]*\"|'[^']*'|[^\s;&|)]*", text[m.end():]).group(0)))
    exprs += [m.group(1) for m in _DOLLAR_BRACKET.finditer(single)]
    exprs += [m.group(1) for m in _SUBSCRIPT_EXPAND.finditer(single)]
    exprs += [text[m.start(1):m.end(1)] for m in _SUBSCRIPT_SET.finditer(plain)]
    for m in _SUBSCRIPT_CMD.finditer(plain):
        words = text[m.start(2):m.end(2)]
        if m.group(1) == "printf":
            pv = re.search(r"-v[ \t]+(\S+)", words)
            words = pv.group(1) if pv else ""
        exprs += [k.group(1) for k in _SUBSCRIPT_WORD.finditer(words)]
    for m in _SUBSTRING.finditer(single):
        exprs += m.group(1).split(":")
    names, more = _name_targets(text, plain, single)
    exprs += more
    out = [h for h in names]
    for e in exprs:
        if e.strip() in ("", "@", "*"):
            continue
        out += [h for h in arith_unsafe_names(e, text) if h not in out]
    return out


# the builtins that set a variable by a name they are given, and what follows them up to the command's end
_NAME_CMD = re.compile(r"(?:^|[;&|\n({`])[ \t]*(?:(?:then|do|else|!|time)[ \t]+)*"
                       r"(declare|typeset|local|readonly|export|printf|read|mapfile|readarray|getopts)[ \t]"
                       r"([^;&|\n]{0,400})")
_INDIRECT = re.compile(r"\$\{!([A-Za-z_]\w{0,80})(?=[}:#%/^,])")  # `${!n}`: the variable whose name n holds
_READ_VALUE_OPTS = "dinNptu"  # read's options that take a value (-t's is a number zsh evaluates)


def _name_safe(word: str, s: str) -> bool:
    """True when command `s` gives name `word` only literal variable names as values (`n=out`, `for n in a b`) and
    nothing else in `s` can bind it, so a name computed from it (`printf -v "$n"`, `declare "$n"=1`, `${!n}`) is a
    plain name, never `a[$(cmd)]`, whose subscript bash and zsh evaluate."""
    w = re.escape(word)
    if re.search(_ARITH_BINDERS.pattern + r"[^;&|\n]*(?<![\w$])" + w + r"\b", s) or \
            re.search(r"(?:^|[\s;&|(`{])(?:eval|source|\.)\s", s):
        return False
    values = [_unquote(re.match(r"\"[^\"]*\"|'[^']*'|[^\s;&|)]*", s[m.end():]).group(0))
              for m in re.finditer(r"(?:^|[\s;&|(`{])" + w + r"\+?=(?!=)", s)]
    loops = re.findall(r"(?:^|[\s;&|(`{])for\s+" + w + r"\s+in\s+([^;\n]*)", s)
    ident = re.compile(r"[A-Za-z_]\w{0,80}")
    return bool(values or loops) and all(ident.fullmatch(v) for v in values) and \
        all(all(ident.fullmatch(_unquote(item)) for item in lst.split()) for lst in loops)


def _name_targets(text: str, plain: str, single: str):
    """-> (hazards, expressions) of the names a command's builtins set or read by a name they compute: a target of
    declare, typeset, local, readonly, export, printf -v, read, mapfile or getopts, and `${!n}`, whose value is a name
    the shell evaluates (bash and zsh run `a[$(cmd)]` there, as `declare "$x"=1` does). A hazard is `$n` for a name
    the command does not give literal names only (_name_safe), or a substitution in the target; the expressions are a
    literal target's subscript and zsh's `read -t` value, which the arithmetic reading judges."""
    hazards, exprs = [], []

    def computed(t):
        if "$(" in t or "`" in t:
            hazards.append(t)
            return
        for n in re.findall(r"\$\{?([A-Za-z_]\w{0,80})", t):
            if not _name_safe(n, text) and "$" + n not in hazards:
                hazards.append("$" + n)
    if _NAME_CMD.search(plain):
        for m in _NAME_CMD.finditer(plain):
            name, words = m.group(1), text[m.start(2):m.end(2)]
            try:
                args = shlex.split(words)
            except ValueError:
                args = words.split()
            targets, k = [], 0
            if name == "printf":
                for k, a in enumerate(args):
                    if a == "-v" and k + 1 < len(args):
                        targets.append(args[k + 1])
                    elif a.startswith("-v") and len(a) > 2:
                        targets.append(a[2:])
            elif name == "getopts":
                targets = args[1:2]
            else:
                while k < len(args):
                    a = args[k]
                    if re.fullmatch(r"[0-9]*(?:<|>|>>|<<<|<>|&>|>&|<&)[0-9-]*", a):  # a redirect and its target
                        k += 2
                        continue
                    if re.match(r"[0-9]*[<>]", a):  # one glued to its target, `<f.txt`
                        k += 1
                        continue
                    if name == "read" and a[:1] == "-" and len(a) > 1 and a[-1] in _READ_VALUE_OPTS:
                        if a[-1] == "t" and k + 1 < len(args):
                            exprs.append(args[k + 1])
                        k += 2
                        continue
                    if a[:1] in ("-", "+") and len(a) > 1:
                        k += 1
                        continue
                    targets.append(a.split("=", 1)[0] if name in ("declare", "typeset", "local", "readonly",
                                                                  "export") else a)
                    k += 1
            for t in targets:
                if "$" in t or "`" in t:
                    computed(t)
                elif "[" in t and t.endswith("]"):
                    exprs.append(t[t.index("[") + 1:-1])
    for m in _INDIRECT.finditer(single):
        computed("$" + m.group(1))
    return hazards, exprs


def subst(s: str, quotes: bool, procs: list | None = None) -> list:
    """The bodies of the substitutions the shell runs in `s`: `$(…)`, backticks, and with `quotes` (a command line,
    not a heredoc body) `<(…)`, `>(…)` and zsh's `=(…)` outside quotes. `procs`: a list that gets, for each body,
    whether it is a process substitution."""
    out, i, n, q, parens = [], 0, len(s), None, Parens(s)
    while i < n:
        c = s[i]
        if q == "'":
            q = None if c == "'" else q
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if quotes and q is None and c == "'":
            q = "'"
        elif quotes and c == '"':
            q = None if q == '"' else '"'
        elif quotes and q is None and c == "#" and parens.comment(i, False):
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        elif s.startswith("$((", i) and (end := arith_end(s, i + 3)) is not None:
            # arithmetic: the shell runs only the substitutions inside it, and reads each name's value as an
            # expression, so a name whose value may hold `a[$(cmd)]` runs cmd: it is judged as `$name`, a program
            # known only when it runs (arith_unsafe_names)
            body = s[i + 3:end]
            inner = subst(body, False) + arith_unsafe_names(body, s)
            out += inner
            if procs is not None:
                procs += [False] * len(inner)
            parens.close(end + 1, "w")  # `$((1))# <(…)`: a `#` glued to it starts no comment
            i = end + 2
            continue
        elif s.startswith("$(", i) or (quotes and q is None and proc_subst_at(s, i)):
            # `<(…)`, `>(…)` and zsh's `=(…)`: process substitutions, which the shell runs as `$(…)` (never in double
            # quotes or in a heredoc body, where they are text)
            j, depth, qq, pending = i + 2, 1, None, []
            while j < n and depth:
                d = s[j]
                if qq:
                    if d == "\\" and qq == '"':
                        j += 1
                    elif d == qq:
                        qq = None
                elif d == "\\":
                    j += 1
                elif d in "'\"":
                    qq = d
                elif d == "(":
                    depth += 1
                elif d == ")":
                    depth -= 1
                elif d == "<" and s.startswith("<<", j) and not s.startswith("<<<", j) and s[j - 1:j] != "<" and \
                        (m := _HEREDOC_AT.match(s, j)):  # the shell reads a heredoc inside `$(…)`, even in "…"
                    pending.append(m.group(1) or m.group(2) or m.group(3))
                    j = m.end()
                    continue
                elif d == "\n" and pending:  # its body is text: an apostrophe there opens no quote
                    j = _past_heredoc_bodies(s, j + 1, pending)
                    pending = []
                    continue
                j += 1
            out.append(s[i + 2:j - 1] if depth == 0 else s[i + 2:])
            if procs is not None:
                procs.append(c != "$")
            if depth == 0:
                parens.close(j - 1, "w")
            i = j
            continue
        elif c == "`":
            j = i + 1
            while j < n and s[j] != "`":
                j += 2 if s[j] == "\\" else 1
            out.append(s[i + 1:j])
            if procs is not None:
                procs.append(False)
            i = j + 1
            continue
        elif quotes and q is None and c == "(":
            parens.open(i)
        elif quotes and q is None and c == ")":
            parens.close(i)
        i += 1
    if quotes:  # the other places the shell reads a value as an arithmetic expression: a value that may be code
        hazards = arith_hazards(s)  # is judged as a program known only when it runs
        out += hazards
        if procs is not None:
            procs += [False] * len(hazards)
    return out


def _past_heredoc_bodies(s: str, i: int, delims) -> int:
    """The index after the bodies of heredocs whose bodies start at `i`, one per delimiter in order, each ending at
    its terminator line, or, as bash 3.2 reads it in a `$(…)`, right after a delimiter a `)` follows (split_heredocs
    reads them the same way)."""
    n = len(s)
    for delim in delims:
        while i < n:
            j = s.find("\n", i)
            j = n if j < 0 else j
            ln, start, i = s[i:j], i, j + 1
            if ln.strip() == delim:
                break
            if ln.startswith(delim + ")"):
                i = start + len(delim)
                break
    return min(i, n)


def expansions(cmd: str) -> list:
    """The command substitutions the shell runs: `$(…)` and backticks outside single quotes and comments (inside
    double quotes too), and in heredoc bodies whose delimiter is unquoted."""
    text, bodies = split_heredocs(cmd)
    out = subst(text, True)
    for body, expands in bodies:
        if expands:
            out += subst(body, False)
    return out


def shell_calls(cmd: str, cwd, depth: int = 0, where: bool = False):
    """Every program a shell command runs, as (argv, code): each segment's command words without prefixes or
    keywords, the commands of nested shells and of the substitutions the shell expands, and for an interpreter the
    program it runs (inline, its input, or a script outside the workspace; None for a repository's own script).
    Text the command only carries is not a call. None when the command cannot be parsed. With `where`, each call is
    (argv, code, the folder it runs in): after the `cd` and `pushd` before it and its prefix's `env -C` or `sudo -D`,
    UNKNOWN when only the run knows it (a variable, `find -execdir`). A level deeper than it reads is None too:
    the callers judge the words, as for a command they cannot parse."""
    if depth > 4:
        return None if str(cmd or "").strip() else []
    segs = split_commands(cmd)
    if segs is None:
        return None
    bodies, out, ins = heredoc_bodies(cmd), [], []
    osegs = split_commands(cmd, ops=True, inputs=ins)
    osegs = osegs if osegs is not None and len(osegs) == len(segs) else None
    for k, (argv0, _) in enumerate(segs):
        argv = unwrap(argv0)
        if not argv:
            continue
        if os.path.basename(argv[0]) in ("cd", "pushd"):
            cwd = str(core.HOME) if len(argv) == 1 else cwd if argv[1] == "-" else str(resolve(argv[1], cwd))
            continue
        here = _prefix_chdir(argv0[:len(argv0) - len(strip_prefix(argv0))], cwd) if where else cwd
        for run_as in _as_run(argv, cwd):  # also as the shell or interpreter its file is (`./bash-link`)
            fed = []
            if osegs is not None:  # what a pipe or `< f` feeds a shell, an interpreter or `. /dev/stdin` (_fed_program)
                run_as, fed = _fed_program(run_as, osegs, k, ins[k] if k < len(ins) else [], cwd)
            more = _program_calls(run_as, here, bodies + [t for t, _ in fed], depth, where)
            if more is None:
                return None
            out += more
    for text in expansions(cmd):
        more = shell_calls(text, cwd, depth + 1, where)
        if more is None:
            return None
        out += more
    return out


def _program_calls(argv, cwd, bodies, depth, where=False):
    name, args = os.path.basename(argv[0]), argv[1:]

    def call(a, code, at=cwd):
        return (a, code, at) if where else (a, code)

    if INTERP.match(name) and dev_args(argv) is None:
        return [call(argv, None if _repo_script(argv, cwd) else interpreter_program(argv, cwd, bodies)[0])]
    out, texts, inners = [call(argv, None)], [], []
    if name in SHELLS:
        texts = _shell_inputs(args, cwd, bodies, name)
    elif name in ("source", ".") and args:
        texts = [t for t, _, _ in _sourced(args, cwd, bodies)]
    elif name == "eval":
        texts = [" ".join(args)]
    elif name == "xargs":
        inners = [(unwrap(xargs_command(args)), cwd)]
    elif name == "find":
        for k, a in enumerate(args):
            if a in ("-exec", "-execdir", "-ok", "-okdir"):
                sub = args[k + 1:]
                at = str(UNKNOWN) if where and a in ("-execdir", "-okdir") else cwd  # each found file's folder
                inners.append((unwrap(sub[:next((j for j, x in enumerate(sub) if x in (";", "+")), len(sub))]), at))
    for text in texts:
        more = shell_calls(text, cwd, depth + 1, where) if text else []
        if more is None:
            return None
        out += more
    for inner, at in inners:
        if inner:
            more = _program_calls(inner, at, bodies, depth + 1, where)
            if more is None:
                return None
            out += more
    return out


def crew_kind(args) -> str:
    """For the arguments of a `dev` call: 'lane' (a call devkit routes to the caller's lane), 'read' (read-only crew),
    'sync' (`dev crew sync`), 'team' (changes the team's crew or briefs a member), '' (no progress or crew call)."""
    pos = dev_words(args)
    if not pos or pos[0] not in ("progress", "crew"):
        return ""
    if _dev_skips(args, "-h", "--help"):
        return "read"
    if pos[0] == "progress":
        return "lane"
    rest = args[args.index("crew") + 1:]
    sub = pos[1] if len(pos) > 1 else None
    if sub is None or sub in _CREW_READ:
        return "read"
    if sub == "raci":
        return "team" if dev_sets(rest, "--set") else "read"
    if sub == "sync":
        return "read" if _dev_skips(rest, "--check") else "sync"
    return "lane" if sub in _CREW_LANE else "team"


SUBST_WORD = "/dev/fd/63"  # what a substitution stands for while the command around it is read


def close_paren(s: str, j: int) -> int:
    """The index after the `)` that closes a `(` just before j, or -1."""
    depth, q = 1, None
    while j < len(s):
        d = s[j]
        if q:
            if d == "\\" and q == '"':
                j += 1
            elif d == q:
                q = None
        elif d == "\\":
            j += 1
        elif d in "'\"":
            q = d
        elif d == "(":
            depth += 1
        elif d == ")":
            depth -= 1
            if not depth:
                return j + 1
        j += 1
    return -1


# The value options of the dev commands whose words devkit reads (tests/test_cli_wiring.py checks it against the
# parser: a value it does not know reads as an argument, a flag it takes for a value hides the next word).
_DEV_VALUES = frozenset({
    "-m", "--message", "--body-file", "--title", "--base", "--body", "--version", "--notes-file", "-p", "--project",
    "-i", "--initiative", "--label", "--state", "--assignee", "--interval", "--timeout", "--lines", "--limit",
    "--branch", "--flaky", "--why", "--action", "--actor", "--agent", "--breaking", "--category", "--cause",
    "--commit-template",
    "--days", "--delay", "--flush-at", "--format", "--grep", "--lang", "--log", "--member", "--note", "--only",
    "--out", "--outcome",
    "--pr", "--provider", "--refs", "--tag", "--renew", "--runner", "--search", "--session", "--severity", "--sha", "--since", "--stack",
    "--step-timeout", "--until", "--verified", "-n", "-o",
    # dev lesson prevent and supersede (--by), prevent (--ref, --match), prevent, add and anchor (--error): without
    # them `prevent --by rule L-x` reads `rule` as the lesson
    "--by", "--ref", "--match", "--error"})

# find's tests and operators: a segment that starts with one is the rest of a find expression the tokenizer split at
# an escaped parenthesis (`find . \\( -name a -o -name b \\)`)
_FIND_PRIMARIES = frozenset("""-name -iname -path -ipath -wholename -iwholename -regex -iregex -type -newer -o -or -a
    -and -not -print -print0 -prune -size -mtime -mmin -atime -amin -ctime -cmin -maxdepth -mindepth -empty -perm -user
    -group -links -depth -follow -lname -ilname -uid -gid -nouser -nogroup -true -false -ls -quit -samefile -inum
    -newermt -newerct""".split())


def _reads_stdin(args, name="bash") -> bool:
    """True when a shell with these arguments runs a program it reads from its input: no -c and no script file (or
    -s), and it does not only parse it (shell_args)."""
    kind, _, parse_only = shell_args(args, name)
    return kind in ("stdin", "input") and not parse_only


def _fed_program(argv, segs, k, files, cwd) -> tuple:
    """What a shell or interpreter that reads its program from its input is fed (`cat f | sh`, `sh < f`, `0< f sh`,
    `python3 < f`, `node - < f`, `python3 /dev/stdin < f`), and the process substitution it runs as its script
    (`sh <(cat f)`, read as `sh -` fed what cat writes) -> (argv, [(text, path)]), where path is the file a text was
    read from (a redirect, `cat f`), or None for words the command holds (echo, printf). A program given in the words
    (`sh -c …`, `python3 x.py`) is fed nothing here. `source` and `.` are fed the same way when their file is their
    input (`echo '…' | . /dev/stdin`, `source /dev/stdin < f`)."""
    name = os.path.basename(argv[0]) if argv else ""
    sourced = name in ("source", ".") and len(argv) > 1 and _source_target(argv[1:], cwd) not in (False, "<(")
    if not argv or (name not in SHELLS and not INTERP.match(name) and not sourced) or \
            (name in SHELLS and shell_args(argv[1:], name)[2]):  # `bash -n < f` only parses what it reads
        return argv, []
    texts = []
    if sourced:
        pass
    elif "<(" in argv[1:]:  # `sh <(cat f)`: the tokenizer leaves the substitution's words in the segment
        j = argv.index("<(")
        if all(str(a).startswith("-") for a in argv[1:j]):
            sub = unwrap(argv[j + 1:])
            texts.append((_produced(sub, cwd), _cat_path(sub, cwd)) if sub else ("", None))
            argv = [*argv[:j], "-"]
    stdin = sourced or (_reads_stdin(argv[1:], name) if name in SHELLS else
                        program_source(argv, whole=True) == "none") or \
        any(_reads_input(a, cwd) for a in argv[1:])
    if stdin:
        prev = unwrap(segs[k - 1][0]) if k else []
        if prev and segs[k - 1][2] in ("|", "|&"):
            texts.append((_produced(prev, cwd), _cat_path(prev, cwd)))
        for f in files:
            try:
                texts.append((_read_small(resolve(f, cwd)), resolve(f, cwd)))
            except ValueError:  # a name the system cannot encode (a lone surrogate): unread
                pass
    return argv, [(t, path) for t, path in texts if t]


def _cat_path(argv, cwd):
    """The file whose text _produced returns for `cat f`, resolved; None for any other command."""
    if argv and os.path.basename(argv[0]) == "cat" and len(argv) == 2 and not argv[1].startswith("-"):
        return resolve(argv[1], cwd)
    return None


def _produced(argv, cwd) -> str:
    """What a pipe's first command writes, when devkit can know it: echo's and printf's words, a file `cat` reads."""
    name, args = os.path.basename(argv[0]), argv[1:]
    if name == "echo":
        while args and re.match(r"^-[neE]+$", args[0]):
            args = args[1:]
        return " ".join(args).replace("\\n", "\n")
    if name == "printf" and args:
        rest = iter(args[1:])
        return re.sub(r"%[sbdq]", lambda _: next(rest, ""), args[0]).replace("\\n", "\n").replace("\\t", " ")
    if name == "cat" and len(args) == 1 and not args[0].startswith("-"):
        return _read_small(resolve(args[0], cwd))
    return ""


# the programs whose subcommand decides what the gate finds (_publish_actions, _deploy_actions, _opaque_actions)
_GATE_CLIS = frozenset({"npm", "pnpm", "yarn", "bun", "twine", "uv", "poetry", "hatch", "flit", "pdm", "cargo", "gem",
                        "mvn", "mvnw", "gradle", "gradlew", "docker", "podman", "helm", "changeset", "lerna", "vsce",
                        "ovsx", "kubectl", "oc", "terraform", "tofu", "pulumi", "fly", "flyctl", "vercel", "netlify",
                        "firebase", "gcloud", "aws", "wrangler", "serverless", "sls", "cdk", "sam", "eb", "railway",
                        "make", "gmake", "just", "task", "go-task"})

_KEEP_START = frozenset({"if", "then", "elif", "else", "while", "until", "do", "{", "!", "time"})

# the words that keep the next word at the place of a command (`then . f`, `builtin . f`)
_CMD_KEEP = _KEEP_START | {"builtin", "command", "exec", "noglob", "nocorrect", "-", "repeat"}


def _code_kinds(s: str):
    """For each character of a command: 'C' (code the shell reads, also inside a `$(…)` in double quotes), 'D'
    (double-quoted text), 'S' (single-quoted, escaped or a comment), with its parenthesis depth; None for backticks."""
    kinds, deps, stack, depth, i, n, parens = [], [], [("C", 0)], 0, 0, len(s), Parens(s)

    def put(k, count=1):
        kinds.extend([k] * count)
        deps.extend([depth] * count)

    while i < n:
        ctx, c = stack[-1][0], s[i]
        if ctx == "S":
            put("S")
            if c == "'":
                stack.pop()
            i += 1
            continue
        if c == "\\":
            put("S", min(2, n - i))
            i += 2
            continue
        if c == "`":
            return None
        if ctx == "D":
            if s.startswith("$(", i):
                put("D", 2)
                depth += 1
                stack.append(("C", depth))
                parens.opened.append("w")
                i += 2
                continue
            put("D")
            if c == '"':
                stack.pop()
            i += 1
            continue
        if c == "'":
            stack.append(("S", depth))
            put("S")
        elif c == '"':
            stack.append(("D", depth))
            put("D")
        elif c == "#" and parens.comment(i, False):
            j = s.find("\n", i)
            j = n if j < 0 else j
            put("S", j - i)
            i = j
            continue
        elif c == "(":
            depth += 1
            parens.open(i)
            put("C")
        elif c == ")":
            parens.close(i)
            put("C")
            if len(stack) > 1 and stack[-1] == ("C", depth):
                stack.pop()  # the end of a `$(…)` opened inside double quotes
            depth -= 1
        else:
            put("C")
        i += 1
    return kinds, deps


def _shell_words(s: str):
    """The words of a command line as the shell reads them once quotes and backslashes are removed, also inside a
    `$(…)`, as (text, the offset in `s` of each character, plain, at the place of a command), where plain means no
    quote, backslash or `$` in it; None for backticks and ANSI-C or locale quotes (`$'…'`, `$"…"`), whose text
    devkit does not decode. Operators separate words, so `a;b` is two; a redirection's target is no command."""
    words, cur, offs, plain, i, n, parens = [], [], [], True, 0, len(s), Parens(s)
    stack, depth = [], []  # quote contexts: "'", '"', "(" for a `$(…)` inside double quotes, with its paren depth
    state = {"open": True, "target": False, "keep": False}  # a command word may still come; the next word is a redirection's

    def end(before_redirect=False):
        nonlocal cur, offs, plain
        if cur or not plain:
            text = "".join(cur)
            fd = before_redirect and (text.isdigit() or (plain and re.fullmatch(r"\{\w+\}", text)))
            at_cmd = state["open"] and not state["target"] and not fd
            words.append((text, offs, plain, at_cmd))
            keep = text in _CMD_KEEP or (plain and ASSIGN.match(text)) or \
                (state["keep"] and (text.startswith("-") or text.isdigit()))  # `command -p . f`, `repeat 3 . f`
            if at_cmd and not keep:
                state["open"] = False
            state["keep"] = bool(at_cmd and keep)
            state["target"] = False
        cur, offs, plain = [], [], True

    while i < n:
        c, ctx = s[i], stack[-1] if stack else ""
        if ctx == "'":
            if c == "'":
                stack.pop()
            else:
                cur.append(c)
                offs.append(i)
            i += 1
            continue
        if c == "`" or (c == "$" and ctx != '"' and s[i + 1:i + 2] in ("'", '"')):
            return None
        if c == "\\":
            nxt = s[i + 1:i + 2]
            if ctx == '"' and nxt not in ("$", "`", '"', "\\", "\n"):
                cur.append(c)
                offs.append(i)
                i += 1
                continue
            if nxt != "\n":  # a backslash-newline joins two lines
                plain = False
                if nxt:
                    cur.append(nxt)
                    offs.append(i + 1)
            i += 2
            continue
        if ctx == '"':
            if c == '"':
                stack.pop()
                i += 1
                continue
            if s.startswith("$(", i):
                end()
                stack.append("(")
                depth.append(0)
                parens.opened.append("w")
                state.update(open=True, target=False, keep=False)
                i += 2
                continue
            plain = plain and c != "$"
            cur.append(c)
            offs.append(i)
            i += 1
            continue
        if c == "#" and not cur and plain and parens.comment(i, False):
            # a comment, to the end of its line: its words count, its quotes do not
            j = s.find("\n", i)
            j = n if j < 0 else j
            for m in re.finditer(r"\S+", s[i:j]):
                words.append((m.group(0), list(range(i + m.start(), i + m.end())), True, False))
            i = j
            continue
        if c in "'\"":
            stack.append(c)
            plain = False
            i += 1
            continue
        if c in " \t":
            end()
            i += 1
            continue
        if c in "<>":
            end(before_redirect=True)
            state["target"] = True
            i += 1
            continue
        if c in "\n;&|()":
            end()
            if c == "(":
                parens.open(i)
            elif c == ")":
                parens.close(i)
            if c == ")" and ctx == "(":
                if depth[-1]:
                    depth[-1] -= 1
                else:
                    stack.pop()
                    depth.pop()
            elif c == "(" and ctx == "(":
                depth[-1] += 1
            state.update(open=True, target=False, keep=False)
            i += 1
            continue
        plain = plain and c != "$"
        cur.append(c)
        offs.append(i)
        i += 1
    end()
    return words


def tool_commands(tool_input) -> list:
    """The shell commands a tool input carries (Bash, Monitor, Codex's exec_command), each once: its `command`, then
    its `cmd`, which Codex's exec_command may send instead; a list is quoted word by word. [] for none."""
    out = []
    for key in ("command", "cmd"):
        cmd = tool_input.get(key) if isinstance(tool_input, dict) else None
        if isinstance(cmd, list):
            cmd = " ".join(shlex.quote(str(c)) for c in cmd)
        if isinstance(cmd, str) and cmd and cmd not in out:
            out.append(cmd)
    return out


# The command words devkit knows: the programs its rules reason about by name. A bug report may carry one of them
# as the refused or failing program (bugs.public_word); any other program is sent as `<program>`, so a project's own
# tool names never leave this machine.
_RULE_PROGRAMS = frozenset("""git gh dev rm unlink find xargs sed awk perl ruby php python python3 node npx launchctl
    tmux screen ssh autossh osascript open curl wget env sudo tee cp mv ln mkdir touch chmod cat ls echo printf cd
    pushd claude codex""".split())


def _command_words() -> frozenset:
    sets = (_RULE_PROGRAMS, PREFIX_CMDS, _FILTERS, _CHECK_TOOLS, _PATH_TAKERS, _POSIX_SHELLS, _READERS, _CREATORS,
            _CHANGERS, _COPIERS, _MOVERS, SHELLS, _KNOWN_PROGRAMS, _JS_RUNTIMES, _AGENT_CLIS, _GATE_CLIS, _WRAPPERS,
            TCL)
    word = re.compile(r"[A-Za-z][A-Za-z0-9._+-]{0,30}")
    return frozenset(w for s in sets for w in s if isinstance(w, str) and word.fullmatch(w))


COMMAND_WORDS = _command_words()
