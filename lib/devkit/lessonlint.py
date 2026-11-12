# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson lint: what a lesson must look like before it is recorded, what holds it back from recall, and whether its
prevention still exists.

    check_anchors(anchors) -> [str]       the anchor gate: a command anchor that is one word too broad to recall by
                                          (a common program or shell, a shell keyword or builtin, a variable), an
                                          error anchor shorter than 12 characters or on the stoplist, and a file
                                          anchor that matches every file, are refused
    check_shape(title, happened, evidence) -> [str]
                                          the capture shape: the title is the rule, one line of at most 80
                                          characters, not starting with "["; what happened or evidence is given
    screen(fields) -> str                 the reason to hold (quarantine) a lesson, or "": text that tells agents to
                                          override the developer or the guard (an escape hatch or the caller's
                                          identity set as a variable, --no-verify with or without its dashes, and
                                          the like), a secret-like value, a URL, a pipe
                                          into a shell, download-and-run, a path in the agents' or devkit's
                                          configuration folders, or invisible characters, in any field; a command
                                          substitution or a backtick command only beside a destructive command in
                                          the same field (rm -rf, sudo, chmod -R 777, a forced git push, fetched
                                          code a shell runs), a substitution run as code (eval, source, ., exec, a
                                          shell's -c or script, where a backtick command starts) or decoding inside
                                          a substitution (base64 -d, xxd -r, printf escapes, openssl -d), and those
                                          only in the fields an agent acts on (the title, Prevention, Fix), not in
                                          the ones that explain the mistake
    lint(lessons, repo_root=None) -> [{id, kind, problem, fix}]
                                          recorded lessons that recall cannot use well: broad, missing and wrong
                                          anchors, file anchors that match no file, no root cause, held lessons
    prevention_gone(lesson, repo_root) -> str
                                          why the test, guard rule, skill or lint rule a prevented lesson names is
                                          gone, or "" (also "" when the reference names nothing that can be checked)

Every function takes plain data (the lesson dicts lessons.parse returns) and is pure but for reading files; nothing
here writes. The error and reason texts are single lines: callers join them, store a reason in front matter
(`quarantine: <reason>`), and print fixes as they are.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import unicodedata
from pathlib import Path

# --------------------------------------------------------------------------- the anchor gate

BARE_PROGRAMS = frozenset(("python3", "python", "git", "dev", "cp", "mkdir", "sed", "cat", "cd", "ls", "echo", "bash",
                           "sh", "node", "npm"))
# also too broad as a one-word command anchor, though often a second word of a good one (`git grep`, `make test`), so
# not in BARE_PROGRAMS, which lint also reads as "several programs in one anchor": the other shells, and the tools of
# almost every script or session
EVERYDAY_PROGRAMS = frozenset((
    "zsh", "dash", "ksh", "fish", "csh", "tcsh", "rm", "mv", "ln", "touch", "chmod", "chown", "grep", "egrep", "rg",
    "awk", "gawk", "find", "xargs", "head", "tail", "sort", "uniq", "wc", "cut", "tr", "tee", "printf", "env", "sudo",
    "curl", "wget", "tar", "make", "docker", "kubectl", "pytest", "pip", "pip3", "uv", "npx", "pnpm", "yarn", "go",
    "cargo", "java", "mvn", "gradle", "ruby", "perl", "php", "gh", "jq", "rsync", "ssh", "scp", "kill", "ps", "which",
    "brew", "open", "sleep", "date", "diff"))
# words of the shell itself: a keyword or builtin alone matches every loop, block or script that uses it
SHELL_WORDS = frozenset((
    "for", "while", "until", "if", "then", "else", "elif", "fi", "do", "done", "case", "esac", "select", "function",
    "time", "export", "set", "unset", "source", ".", "eval", "exec", "command", "builtin", "alias", "local", "declare",
    "typeset", "readonly", "trap", "return", "exit", "shift", "pushd", "popd", "true", "false", "test", "[", "[[",
    "read", "wait"))
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=.*")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]+")  # PYTHONPATH: a variable's name, never a program's
_PYTHON = re.compile(r"python\d*(?:\.\d+)?")
ERROR_MIN = 12
# error anchors that match ordinary output: the spec's list, and the generic messages any shell or OS prints
ERROR_STOPLIST = frozenset(("file(s)", "still fail", "error", "failed", "not found", "permission denied",
                            "no such file or directory", "command not found", "operation not permitted",
                            "traceback (most recent call last)"))
_EDGE = " \t.,:;!?\"'`“”‘’"


def _words(command: str) -> list:
    try:
        return shlex.split(command or "")
    except ValueError:
        return (command or "").split()


def _bare(word: str) -> bool:
    name = word.rsplit("/", 1)[-1]
    return name in BARE_PROGRAMS or bool(_PYTHON.fullmatch(name))


def _norm_error(text: str) -> str:
    return " ".join(str(text).split()).casefold().strip(_EDGE)


# what a hint shows as a command anchor that names the risky use of a bare program
EXAMPLES = {"python3": "python3 -c", "python": "python -c", "bash": "bash -c", "sh": "sh -c", "node": "node -e",
            "git": "git push --force", "dev": "dev progress step", "npm": "npm run <script>", "sed": "sed -i",
            "zsh": "zsh -c", "rm": "rm -rf", "find": "find -delete", "sudo": "sudo rm"}


def _command_errors(command: str) -> list:
    """Why a command anchor is too broad to recall a lesson by, or [] when it is not: one word that is a common
    program (BARE_PROGRAMS, EVERYDAY_PROGRAMS, by any path or python version), a shell keyword or builtin, or a
    variable (its name, or an assignment)."""
    words = _words(command)
    if len(words) != 1:
        return []
    word = words[0]
    name = word.rsplit("/", 1)[-1]
    if _bare(word) or name in EVERYDAY_PROGRAMS:
        example = EXAMPLES.get(name, f"{name} <the flag or argument that goes wrong>")
        return [f"the command anchor `{command}` alone matches every {name} call: anchor the words that go wrong "
                f"too, such as `{example}`"]
    if word in SHELL_WORDS:
        return [f"the command anchor `{command}` is a shell keyword or builtin, in almost every script: anchor the "
                f"command that goes wrong instead, the program with its words"]
    if _ASSIGNMENT.fullmatch(word) or _ENV_NAME.fullmatch(word):
        return [f"the command anchor `{command}` is a variable, not a command: anchor the command that goes wrong "
                f"with it, the program with its words"]
    return []


_EVERY_FILE = re.compile(r"[*/.?]*")  # `**`, `*`, `**/*`, `./**`, `.`: a file anchor that matches every file


def _file_errors(anchor: str) -> list:
    if _EVERY_FILE.fullmatch(anchor):
        return [f"the file anchor `{anchor}` matches every file: anchor the folder or files the mistake is in, such "
                f"as `src/billing/**`"]
    return []


def _error_errors(error: str) -> list:
    text = " ".join(str(error).split())
    if _norm_error(text) in ERROR_STOPLIST:
        return [f"the error anchor `{text}` matches ordinary output: anchor the distinctive part of the message"]
    if len(text) < ERROR_MIN:
        return [f"the error anchor `{text}` is {len(text)} characters: give at least {ERROR_MIN} characters of the "
                f"text the output shows"]
    return []


def check_anchors(anchors: dict) -> list:
    """The anchor gate for new anchors: [error text], [] when they pass. A command anchor too broad to recall by
    (_command_errors), an error anchor that is short or ordinary output, and a file anchor that matches every file
    are refused. Kinds of work and stack are not judged here (lessons._anchor_problems checks kinds of work against
    the RACI); lint lists the file anchors that match no file."""
    anchors = anchors or {}
    out = []
    for c in anchors.get("commands") or []:
        if str(c).strip():
            out += _command_errors(str(c).strip())
    for f in anchors.get("files") or []:
        if str(f).strip():
            out += _file_errors(str(f).strip())
    for e in anchors.get("errors") or []:
        if str(e).strip():
            out += _error_errors(e)
    return out


# --------------------------------------------------------------------------- the capture shape

TITLE_MAX = 80
_LINE_BREAKS = re.compile(r"[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]")


def check_shape(title: str, happened: str, evidence: list) -> list:
    """The capture shape: [error text], [] when it fits."""
    t = str(title or "").strip()
    out = []
    if not t:
        out.append("a lesson needs a title: the rule to follow, in at most 80 characters")
    else:
        if _LINE_BREAKS.search(t):
            out.append("the title must be one line: put the story in --happened")
        if len(t) > TITLE_MAX:
            out.append(f"the title is {len(t)} characters: make it the rule to follow in at most {TITLE_MAX} "
                       f"(the story goes in --happened)")
        if t.startswith("["):
            out.append("the title cannot start with “[”: the front matter would read it as a list")
    if not str(happened or "").strip() and not any(str(e or "").strip() for e in evidence or []):
        out.append("say what happened (--happened) or give the evidence (--evidence): a commit, PR, run or the "
                   "command and its output")
    return out


# --------------------------------------------------------------------------- screening

# characters that hide or reorder text: zero-width, direction controls, the BOM, and Unicode tag characters
_HIDDEN = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")
_DASHES = str.maketrans({c: "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"})
# Cyrillic and Greek letters drawn like Latin ones, and the dotless i and j, read as those Latin letters
_LOOKALIKES = str.maketrans(dict(zip(
    "\u0430\u0441\u0435\u04bb\u0456\u0458\u04cf\u043e\u0440\u051b\u0455\u051d\u0445\u0443\u0501"
    "\u0410\u0412\u0415\u04ba\u0406\u0408\u041a\u041c\u041d\u041e\u0420\u051a\u0405\u0422\u051c\u0425\u0423\u04c0"
    "\u03b1\u03b5\u03b9\u03ba\u03bd\u03bf\u03c1\u03c4\u03c5\u03c7"
    "\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7"
    "\u0131\u0237",
    "acehijlopqswxyd" "ABEHIJKMHOPQSTWXYI" "aeikvoptux" "ABEZHIKMNOPTYX" "ij")))
_OVERRIDE = re.compile(
    r"\bignore\s+(?:all\s+|any\s+|every\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|earlier|preceding)\b"
    r"|\b(?:ignore|disregard|forget)\s+(?:(?:all|every|any)\s+(?:of\s+)?)?(?:the\s+|your\s+|my\s+|these\s+|those\s+)?"
    r"(?:previous|prior|above|earlier|preceding)\s+(?:instructions?|rules?|guidance|guidelines?|directions?"
    r"|directives?)\b"
    r"|\byou\s+are\s+now\b|\bnew\s+instructions?\b"
    r"|\bdisregard\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|instructions|rules|developer|user"
    r"|guard)\b"
    r"|\balways\s+allow\b"
    r"|\b(?:disable|turn\s+off|switch\s+off|bypass|skip|circumvent|deactivate|silence)\s+(?:the\s+|all\s+|any\s+"
    r"|devkit'?s?\s+|its\s+)*(?:guard|hooks?|sandbox|verification|permission\s+checks?|safeguards?)\b"
    r"|\b(?:override|overrule|disobey)\s+(?:the\s+)?(?:developer|user|guard)\b"
    r"|\bno-verify\b|--dangerously-[\w-]+|\bdangerously[\s_-]*(?:disable|skip|bypass)\w*|\bbypasspermissions\b",
    re.I)
# variables that say who is calling devkit or where it reads: set by an agent, they point devkit at another session or
# state tree (guard.py refuses them for devkit calls); the guard's own one-command hatches join them when screened
IDENTITY_VARS = ("DEVKIT_SESSION", "DEVKIT_FROM_AGENT", "DEVKIT_AGENT", "DEVKIT_CONFIG", "DEV_ROOT", "HOME")


def _hatch_set(text: str):
    """An assignment (NAME=…) of one of the guard's escape hatches (guard.ENV_HATCHES, read at each call so the screen
    follows the guard) or of IDENTITY_VARS; any DEVKIT_ assignment when the guard cannot be read."""
    try:
        from .guard import ENV_HATCHES
        names = "|".join(sorted(re.escape(n) for n in set(ENV_HATCHES) | set(IDENTITY_VARS)))
    except Exception:
        names = "|".join(IDENTITY_VARS) + r"|DEVKIT_[A-Z0-9_]+"
    return re.search(r"(?<![\w$])(?:" + names + r")=", text)


def _override(text: str):
    """Override phrasing, read casefolded, or an escape hatch or identity set (case-sensitive, as a shell reads it)."""
    return _OVERRIDE.search(text.casefold()) or _hatch_set(text)


def _normal(text: str) -> str:
    """Text as the screens read it: compatibility forms folded (NFKC: fullwidth letters become plain ones), combining
    marks dropped, Unicode dashes read as "-", and Cyrillic and Greek look-alikes read as the Latin letters they copy."""
    bare = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return unicodedata.normalize("NFKC", bare).translate(_DASHES).translate(_LOOKALIKES)


_SHELLS =r"(?:(?:ba|z|da|k|c|tc|fi)?sh|python[\d.]*|perl|ruby|node|php|pwsh|powershell|iex)"
_DOWNLOAD_RUN = re.compile(
    r"<\(\s*(?:curl|wget|fetch|iwr|irm)\b"
    r"|\b(?:curl|wget|fetch)\b[^\n]{0,200}?(?:&&|;|\|\|)\s*(?:sudo\s+)?(?:" + _SHELLS + r"|chmod\s+\+x|source|\.)\s"
    r"|\biex\b\s*\(?\s*(?:iwr|irm|invoke-webrequest|invoke-restmethod|new-object)\b",
    re.I)
_PIPE_SHELL = re.compile(r"(?<!\|)\|(?!\|)\s*(?:(?:sudo|env|xargs|exec|command)\b[^|;&\n]{0,80}?\s)?(?:/[\w./-]*/)?"
                         + _SHELLS + r"\b", re.I)
_SUBST = re.compile(r"\$\(")
_BACKTICK = re.compile(r"`[^`]+`")
# the commands that make a command substitution or a backtick command dangerous: rm with both recursive and force,
# sudo, chmod -R 777, a forced git push (not --force-with-lease or --force-if-includes), and fetched code that a shell
# runs (handed to eval, source, exec or a shell's -c, substituted where a command starts, or saved to a file that is
# then run as ./file)
_RM = re.compile(r"\brm((?:[ \t]+-[\w-]+)+)", re.I)
_CHMOD = re.compile(r"\bchmod((?:[ \t]+[^\s;&|()`]+)+)", re.I)
_PUSH = re.compile(r"\bgit\b[^\n;&|()`]{0,200}?\bpush\b([^\n;&|()`]*)", re.I)
_SUDO = re.compile(r"\bsudo\b", re.I)
_FETCH = r"(?:sudo\s+)?(?:curl|wget|fetch|iwr|irm|invoke-webrequest|invoke-restmethod)\b"
_FETCH_RUN = re.compile(
    r"(?:\beval|\bsource|\bexec|(?<![^\s;&|])\.|\b" + _SHELLS + r"(?:\s+-\w+)*\s+-\w*c)\s+[\"']?(?:\$\(|`)\s*" + _FETCH
    + r"|(?:^|[;&|({]|\b(?:run|then|do)\b)\s*\$\(\s*" + _FETCH
    + r"|\b(?:curl|wget)\b[^\n]{0,200}?(?:&&|;|\|\|)\s*(?:sudo\s+)?\./",
    re.I | re.M)


def _flags(args: str):
    """The short option letters and the long options in a command's arguments."""
    words = args.split()
    return "".join(w[1:] for w in words if w.startswith("-") and not w.startswith("--")), set(words)


def _rm_rf(text: str) -> bool:
    for m in _RM.finditer(text):
        short, words = _flags(m.group(1))
        if ("r" in short.lower() or "--recursive" in words) and ("f" in short or "--force" in words):
            return True
    return False


def _chmod_777(text: str) -> bool:
    for m in _CHMOD.finditer(text):
        short, words = _flags(m.group(1))
        if ("R" in short or "--recursive" in words) and words & {"777", "0777"}:
            return True
    return False


def _forced_push(text: str) -> bool:
    for m in _PUSH.finditer(text):
        short, words = _flags(m.group(1))
        if "f" in short or "--force" in words:
            return True
    return False


def _destructive(text: str) -> bool:
    return bool(_SUDO.search(text) or _FETCH_RUN.search(text)) or _rm_rf(text) or _chmod_777(text) or \
        _forced_push(text)


def _dangerous(text: str) -> bool:
    """Download-and-run, a pipe into a shell, or a destructive command (_destructive)."""
    return bool(_DOWNLOAD_RUN.search(text) or _PIPE_SHELL.search(text)) or _destructive(text)


def _dangerous_subst(text: str) -> bool:
    return bool(_SUBST.search(text)) and _dangerous(text)


def _dangerous_backtick(text: str) -> bool:
    return bool(_BACKTICK.search(text)) and _dangerous(text)


# a substitution run as code, whatever is inside it: handed to eval, source, ., exec, a shell's or interpreter's code
# or script, or placed where a backtick command starts; and decoding inside a substitution (base64 -d, xxd -r, printf
# escapes, openssl -d and the like). A placeholder ($(...), $(…), <(...)) names the construct and runs nothing.
_PLACEHOLDER = re.compile(r"[$<]\([ \t]*(?:\.{2,})?[ \t]*\)")
_OPENS = re.compile(r"[$<]\(|`")
_TICK_SUBST = re.compile(r"`[ \t]*[\"']?\$\((?![ \t]*(?:\.{2,})?[ \t]*\))")
# "." runs a file only where a command starts, and never ends a sentence
_RUNNER = re.compile(r"(?<![\w.-])(?:(?P<code>eval|exec|source|(?<![^\s;&|({`])\.(?![ \t]+(?-i:[A-Z])))|" + _SHELLS
                     + r")(?=[ \t])", re.I)
_CODE_OPTION = re.compile(r"-[A-Za-z]*[cer]")  # -c, -xc, -e, -le, -r: the code is the next word
_VALUE_OPTIONS = frozenset(("-o", "+o", "-O", "+O", "-W", "-X", "--rcfile", "--init-file"))
_REDIRECT = re.compile(r"\d*(?:<<?<?|>>?)&?")  # a redirection alone: its target is the next word
_OPTION = re.compile(r"[-+]|\d*[<>]")
_SENTENCE_END = re.compile(r"[.!?][ \t]+[A-Z]")
_SUBST_OPEN = re.compile(r"[$<]\(|=`")
_HEREDOC_OR_CASE = re.compile(r"(?<!<)<<(?!<)|\bcase\b")
_ESCAPES = r"\\(?:x[0-9a-f]|[0-7]{3}|u[0-9a-f]{4}|U[0-9a-f]{8})"
_DECODE = re.compile(
    r"\b(?:base64|base32|basenc)\b[^\n]*?[ \t](?:-[a-z]*d[a-z]*|--decode)\b"
    r"|\bopenssl\b[^\n]*?[ \t]-d\b"
    r"|\bxxd\b[^\n]*?[ \t]-[a-z]*r"
    r"|\buudecode\b"
    r"|\b(?:printf|print|echo)\b[^\n]*?" + _ESCAPES
    + r"|\$'[^'\n]*?" + _ESCAPES
    + r"|\b(?:b(?:16|32|64|85)decode|a85decode|decodebytes|unhexlify|fromhex|frombase64string|atob)\b"
    r"|\bbuffer\.from\b[^\n]*?\b(?:base64|hex)\b",
    re.I)
SCAN_MAX = 100_000  # characters the scans below read in one text; past it the text is held, never let through


def _quote_end(text: str, j: int) -> int:
    """The index after the quote that opens at text[j] ("…" and $'…' honor backslash escapes, '…' does not); the end of
    the text when it never closes."""
    q, n = text[j], len(text)
    escapes = q == '"' or (j > 0 and text[j - 1] == "$")
    j += 1
    while j < n and text[j] != q:
        j += 2 if escapes and text[j] == "\\" else 1
    return min(j + 1, n)


def _command_words(text: str, i: int):
    """The words of the shell command that goes on at text[i], as written, each with the index after it, then (None,
    the index where the reading stopped). Quotes, escapes and parentheses keep a word whole; outside them the command
    ends at ;, &, |, a line break, an unmatched ), the end of a sentence (". " and a capital letter), or a backtick that
    does not start the first word (it closes a backtick command). A backtick that starts the first word opens an
    old-style substitution, read to the next backtick."""
    n, j, start, depth = len(text), i, None, 0
    first = True
    while j < n:
        c = text[j]
        if not depth:
            if c in " \t":
                if start is not None:
                    yield text[start:j], j
                    start, first = None, False
                j += 1
                continue
            if (c in ";&|\n)" or (c == "`" and not (first and start is None))
                    or (c in ".!?" and _SENTENCE_END.match(text, j))):
                break
            if start is None:
                start = j
            if c == "`":
                end = text.find("`", j + 1)
                j = n if end < 0 else end + 1
                continue
        if c == "\\":
            j += 2
        elif c in "\"'":
            j = _quote_end(text, j)
        else:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            j += 1
    end = min(j, n)
    if start is not None:
        yield text[start:end], end
    yield None, end


def _has_subst(word: str) -> bool:
    return bool(_OPENS.search(_PLACEHOLDER.sub("", word)))


def _runs_substitution(text: str) -> bool:
    """A substitution where a backtick command starts, or one a runner runs: any word of eval, source, . or exec, and
    for a shell or interpreter its options, its input, its code (the word after -c, -e or -r) or its script (the first
    word that is no option nor an option's value)."""
    if _TICK_SUBST.search(text):
        return True
    budget = SCAN_MAX
    for m in _RUNNER.finditer(text):
        shell, expect, end = not m.group("code"), "", m.end()
        for word, end in _command_words(text, m.end()):
            if word is None:
                break
            if _has_subst(word):
                return True
            if not shell:
                continue
            if expect == "code":
                break
            if expect == "value":
                expect = ""
            elif _CODE_OPTION.fullmatch(word):
                expect = "code"
            elif word in _VALUE_OPTIONS or _REDIRECT.fullmatch(word):
                expect = "value"
            elif not _OPTION.match(word):
                break  # the script, with no substitution in it
        budget -= end - m.end()
        if budget < 0:
            return True
    return False


def _substitution_end(text: str, i: int) -> int:
    """Where the substitution whose text starts at text[i] ends: the ) that closes it, read as a shell does (quotes,
    escapes, nested substitutions and parentheses, backtick commands, # comments). The end of the text when it never
    closes, or when it holds a here-document or a case pattern, whose ) a shell reads otherwise."""
    stack, j, n = ["("], i, len(text)
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "`":
            end = text.find("`", j + 1)
            j = n if end < 0 else end
        elif stack[-1] == '"':
            if c == '"':
                stack.pop()
            elif text.startswith("$(", j):
                stack.append("(")
                j += 1
        elif c == '"':
            stack.append('"')
        elif c == "'":
            j = _quote_end(text, j) - 1
        elif c == "#" and (j == i or text[j - 1].isspace()):
            end = text.find("\n", j)
            j = n if end < 0 else end
        elif c == "(":
            stack.append("(")
        elif c == ")":
            stack.pop()
            if not stack:
                return n if _HEREDOC_OR_CASE.search(text, i, j) else j
        j += 1
    return n


def _decodes_in_substitution(text: str) -> bool:
    """A decoder inside a $( … ), a <( … ) or a backtick command assigned with =`…`."""
    budget = SCAN_MAX
    for m in _SUBST_OPEN.finditer(text):
        if _PLACEHOLDER.match(text, m.start()):
            continue
        if m.group() == "=`":
            end = text.find("`", m.end())
            end = len(text) if end < 0 else end
        else:
            end = _substitution_end(text, m.end())
        budget -= end - m.end()
        if budget < 0 or _DECODE.search(text, m.end(), end):
            return True
    return False


_URL = re.compile(r"\b[a-z][a-z0-9+.-]{1,15}://\S|\bwww\.[a-z0-9-]+\.[a-z]", re.I)
_CONFIG = re.compile(r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+|/root)/(?:\.claude(?:\.json|/|\b)|\.codex\b"
                     r"|\.local/share/devkit\b|\.config/devkit\b)")
# (what the reason says, the test), in the order a reason is chosen
SCREENS = (
    ("has invisible or direction-changing characters", _HIDDEN.search),
    ("has a secret-like value", None),  # the audit redactor's patterns, loaded when first used
    ("tells agents to override the developer or the guard", _override),
    ("downloads and runs code", _DOWNLOAD_RUN.search),
    ("pipes into a shell", _PIPE_SHELL.search),
    ("has a command substitution with a destructive command", _dangerous_subst),
    ("has a backtick command with a destructive command", _dangerous_backtick),
    ("runs a command substitution as code", _runs_substitution),
    ("decodes text in a command substitution", _decodes_in_substitution),
    ("has a URL", _URL.search),
    ("names a path in the agents' or devkit's configuration folders", _CONFIG.search),
)
# the sections that explain a mistake rather than tell an agent what to do: a destructive command in a substitution or
# a backtick command there, a substitution run as code and decoding in a substitution are the story of what ran, so
# those four screens skip them (every other screen still reads them, download-and-run and a pipe into a shell included)
EXPLANATION = frozenset(("what happened", "root cause", "detection", "evidence"))
_ACTING_ONLY = frozenset((_dangerous_subst, _dangerous_backtick, _runs_substitution, _decodes_in_substitution))


def _secret(text: str):
    from .audit import SECRET
    return SECRET.search(text)


def _strings(value) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [x for v in value for x in _strings(v)]
    return []


def _field(key) -> str:
    return " ".join(str(key).split()).lstrip("[")[:40] or "text"


def _explains(key) -> bool:
    return " ".join(str(key).split()).casefold() in EXPLANATION


def screen(fields: dict) -> str:
    """Why a lesson must be held instead of recalled, as one line ("Prevention pipes into a shell"), or "". Every
    string value is screened, whatever its key, and so are lists of strings. A command substitution or a backtick
    command alone holds nothing: lessons name them to warn against them ("never use $(...) in a lane"). One holds the
    lesson beside a destructive command in the same value (_destructive), when it is run as code (handed to eval,
    source, ., exec or a shell, or where a backtick command starts) or when it decodes text, and only outside What
    happened, Root cause, Detection and Evidence (in the title, Prevention, Fix and any other key), because those four
    explain what ran; download-and-run and a pipe into a shell hold in every field on their own. Text is compared as
    _normal reads it (NFKC, combining marks dropped, Unicode dashes read as "-", Cyrillic and Greek look-alikes read as
    Latin), and override phrasing casefolded. The reason never quotes the text it found, so a secret is never copied
    into the lesson's front matter."""
    texts = [(key, s, _normal(s)) for key, value in (fields or {}).items() for s in _strings(value) if s]
    for what, test in SCREENS:
        for key, raw, normal in texts:
            if test in _ACTING_ONLY and _explains(key):
                continue
            if (test or _secret)(raw if test is _HIDDEN.search else normal):
                return f"{_field(key)} {what}"
    return ""


# --------------------------------------------------------------------------- lint

SCRATCH = frozenset(("scratchpad", "scratch", ".tmp", "tmp"))
_SKIP_DIRS = frozenset((".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
                        ".ruff_cache", ".tox"))
WALK_MAX = 100_000  # files a walk lists; past it, an unmatched glob is not reported
_GLOB = re.compile(r"[*?\[]")
NOT_LINTED = ("retired", "dormant")


def _walk(root: Path):
    """[relative posix path] of the files under root (skipping VCS, dependency and cache folders), and whether the
    list is complete."""
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in _SKIP_DIRS]
        rel = Path(d).relative_to(root).as_posix()
        for f in files:
            out.append(f if rel == "." else f"{rel}/{f}")
            if len(out) >= WALK_MAX:
                return out, False
    return out, True


def _repo(repo_root) -> Path | None:
    """The repository folder to check against; None when none is given or it is not on this machine."""
    return Path(repo_root) if repo_root and Path(repo_root).is_dir() else None


def _scratch(anchor: str) -> bool:
    """A scratch pattern (a folder of SCRATCH anywhere in it), or a path outside the repository."""
    a = anchor.strip()
    if a.startswith(("/", "~", "$")):
        return True  # outside the repository: not this check's business
    return any(part in SCRATCH for part in a.split("/") if part and not _GLOB.search(part))


def _matches(anchor: str, path: str) -> bool:
    a = anchor[2:] if anchor.startswith("./") else anchor
    a = a.rstrip("/") if not _GLOB.search(a) else a
    if path == a or path.startswith(a + "/") or path.endswith("/" + a):
        return True
    if fnmatch.fnmatchcase(path, a) or fnmatch.fnmatchcase(path, "*/" + a):
        return True
    return a.startswith("**/") and fnmatch.fnmatchcase(path, a[3:])


def _file_matches(anchor: str, root: Path, listing) -> bool | None:
    """Does a file anchor name a file under root? None when it cannot tell (the walk stopped early)."""
    a = anchor[2:] if anchor.startswith("./") else anchor
    if not _GLOB.search(a) and (root / a).exists():
        return True
    files, complete = listing()
    if any(_matches(a, p) for p in files):
        return True
    return False if complete else None


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[`'\"“”‘’]", " ", str(text)).split()).casefold()


def _lesson_text(les: dict) -> str:
    sections = les.get("sections") or {}
    return _plain(" ".join([str((les.get("meta") or {}).get("title") or "")] +
                           [str(v) for v in sections.values() if v and v != "_—_"]))


def _activity_known(activity: str) -> bool | None:
    try:
        from . import raci
        raci.resolve_activity(activity)
        return True
    except ValueError:
        return False
    except Exception:
        return None  # the roster could not be read: say nothing


def _finding(les_id: str, kind: str, problem: str, fix: str) -> dict:
    return {"id": les_id, "kind": kind, "problem": problem, "fix": fix}


def lint(lessons: list, repo_root=None) -> list:
    """Findings for recorded lessons, in their order: [{id, kind, problem, fix}], kind one of broad, missing, wrong,
    no-file, no-cause, quarantined. Retired and archived (dormant) lessons are past recall and skipped. File anchors
    are checked against the files under repo_root (none when it is None); scratch patterns and paths outside the
    repository are not."""
    root = _repo(repo_root)
    cache = {}

    def listing(base: Path):
        if base not in cache:
            cache[base] = _walk(base)
        return lambda: cache[base]

    def no_file(anchor: str) -> bool:
        """The anchor matches no file in the repository, nor in the handoff repository (lessons, plans, notes),
        which recall also sees; ".handoff/" in front of it names the handoff repository."""
        here = _file_matches(anchor, root, listing(root))
        if here is not False:
            return False
        handoff = _handoff()
        if not handoff.is_dir() or handoff == root:
            return True
        a = anchor[2:] if anchor.startswith("./") else anchor
        a = a[len(".handoff/"):] if a.startswith(".handoff/") else a
        return _file_matches(a, handoff, listing(handoff)) is False

    out = []
    for les in lessons or []:
        m = les.get("meta") or {}
        if m.get("status") in NOT_LINTED:
            continue
        lid = str(m.get("id") or "?")
        anchor = f"dev lesson anchor {lid}"
        an = {k: [str(x).strip() for x in (m.get(k) or []) if str(x).strip()] if isinstance(m.get(k), list) else []
              for k in ("files", "commands", "activities", "errors", "stack")}
        if str(m.get("quarantine") or "").strip():
            out.append(_finding(lid, "quarantined", f"held, not recalled: {m['quarantine']}",
                                f"check its text, then the developer releases it: dev lesson release {lid}"))
        cause = str((les.get("sections") or {}).get("Root cause") or "").strip()
        if not cause or cause == "_—_":
            out.append(_finding(lid, "no-cause", "has no root cause, so it is not recalled",
                                f"record why it happened, or retire it: dev lesson retire {lid} --why …"))
        if not any(an.values()):
            out.append(_finding(lid, "missing", "has no anchors, so it is not recalled",
                                f"{anchor} --file … --command … --error … (where the mistake happens)"))
            continue
        text = _lesson_text(les)
        for c in an["commands"]:
            words = _words(c)
            if _command_errors(c):
                out.append(_finding(lid, "broad", _command_errors(c)[0],
                                    f'{anchor} --command "{words[0]} <the words that go wrong>", then drop `{c}`'))
            elif len(words) > 1 and all(_bare(w) for w in words):
                out.append(_finding(lid, "wrong", f"the command anchor `{c}` names several programs in one: it "
                                                  f"matches only that exact sequence",
                                    f"{anchor} with one --command each, then drop `{c}`"))
            elif _plain(c) not in text:
                out.append(_finding(lid, "wrong", f"the command anchor `{c}` is named nowhere in the lesson: is it "
                                                  f"this lesson's mistake?",
                                    f"if not, {anchor} --command with the command that went wrong, then drop `{c}`"))
        for e in an["errors"]:
            for problem in _error_errors(e):
                out.append(_finding(lid, "broad", problem,
                                    f'{anchor} --error "<the distinctive part of the message>", then drop it'))
        for a in an["activities"]:
            if _activity_known(a) is False:
                out.append(_finding(lid, "wrong", f"the kind of work `{a}` is not in the crew's RACI",
                                    f"{anchor} --activity <an id from dev crew raci>, then drop `{a}`"))
        for f in an["files"]:
            if _file_errors(f):
                out.append(_finding(lid, "broad", f"the file anchor `{f}` matches every file",
                                    f"{anchor} --file <the folder or files the mistake is in>, then drop `{f}`"))
            elif root and not _scratch(f) and no_file(f):
                out.append(_finding(lid, "no-file", f"the file anchor `{f}` matches no file in the repository or "
                                                    f"the handoff repository",
                                    f"{anchor} --file with the current path, then drop `{f}`"))
    return out


# --------------------------------------------------------------------------- prevention references

DEVKIT_LIB = Path(__file__).resolve().parent
DEVKIT_HOME = DEVKIT_LIB.parents[1]
TEXT_MAX = 512 * 1024  # bytes read of one file when a name is searched for
SEARCH_MAX = 64 * 1024 * 1024  # bytes read in all; past it, a name not found is not reported
_PATH = re.compile(r"(?P<path>(?:\./)?[\w.@+-]+(?:/[\w.@+-]+)+)(?P<sym>(?:::?[\w.]+)+)?")
_TEST_DIRS = ("tests", "test", "spec", "__tests__")
_SYMBOL = re.compile(r"[A-Z][A-Z0-9_]{2,}|[A-Z][a-z0-9]+[A-Z]\w*|test_\w+")
_HEX = re.compile(r"[0-9a-f]{7,40}")
_RULE_ID = re.compile(r"[A-Z][A-Z0-9_]{2,}|[a-z0-9]+(?:-[a-z0-9]+)+|[A-Za-z]{1,4}-[0-9A-Za-z]{3,}")
_SKILL = re.compile(r"(?:(?P<plugin>[a-z0-9][a-z0-9-]*):)?(?P<name>[a-z0-9][a-z0-9-]*)")
_LINT_ID = re.compile(r"[A-Za-z][\w./-]{1,80}")


def _parts(ref: str) -> list:
    """A reference's parts: text in parentheses and quotes is a comment, and , ; · + & "and" separate parts."""
    ref = re.sub(r"\([^()]*\)|'[^'\n]*'|\"[^\"\n]*\"|“[^”\n]*”", " ", ref)
    return [p.strip() for p in re.split(r"[,;·]|\s[+&]\s|\sand\s", ref) if p.strip()]


def _is_path(token: str) -> bool:
    """A repository or handoff relative file, with or without :names after it (not a URL, not an absolute path)."""
    m = _PATH.fullmatch(token)
    if not m or "://" in token or token.startswith(("/", "~")):
        return False
    path = m.group("path")
    return bool(re.search(r"\.[A-Za-z0-9]{1,8}$", path)) or path.split("/", 1)[0] in _TEST_DIRS


def _read(path: Path, limit: int | None = TEXT_MAX) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit if limit else -1).decode("utf-8", "ignore")
    except OSError:
        return ""


def _has_word(text: str, word: str) -> bool:
    return bool(re.search(r"(?<![\w])" + re.escape(word) + r"(?![\w])", text))


def _is_test_file(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return (any(p in _TEST_DIRS for p in rel.split("/")[:-1]) or name.startswith("test_")
            or re.search(r"(?:_test\.\w+|\.(?:test|spec)\.\w+)$", name) is not None)


def _search(root: Path, word: str, only=None) -> bool | None:
    """Is the word in a file under root (only the files `only` accepts)? None when the search stopped early."""
    files, complete = _walk(root)
    budget = SEARCH_MAX
    for rel in files:
        if only and not only(rel):
            continue
        text = _read(root / rel)
        budget -= len(text)
        if _has_word(text, word):
            return True
        if budget <= 0:
            return None
    return False if complete else None


def _handoff() -> Path:
    from . import lessons
    return Path(lessons.LESSONS).parent


def _locate(rel: str, root) -> Path | None:
    """A reference's path: in the repository, else in the handoff repository; None when in neither."""
    rel = rel[2:] if rel.startswith("./") else rel
    for base in ([root] if root else []) + [_handoff()]:
        p = base / rel
        if p.exists():
            return p
        if rel.split("/", 1)[0] in _TEST_DIRS and not re.search(r"\.\w+$", rel) and p.with_suffix(".py").exists():
            return p.with_suffix(".py")
    return None


def _symbols_gone(path: Path, rel: str, symbols: list) -> str:
    text = _read(path) if path.is_file() else ""
    for s in symbols:
        if text and not _has_word(text, s):
            return f"{s} is no longer in {rel}"
    return ""


def _path_gone(token: str, rest: list, root) -> str:
    m = _PATH.fullmatch(token)
    rel, sym = (m.group("path"), m.group("sym") or "") if m else (token, "")
    found = _locate(rel, root)
    if not found:
        return f"{rel} no longer exists" if root else ""
    symbols = [s for s in re.split(r"::?|\.", sym) if s and not s.isdigit()]
    symbols += [s for t in rest for s in t.split(".") if _SYMBOL.fullmatch(s)]
    return _symbols_gone(found, rel, symbols)


def _dotted_gone(token: str, root) -> str:
    """tests.test_x.Class.test_y: the longest prefix that is a module file, then the names in it."""
    parts = token.split(".")
    for i in range(len(parts), 0, -1):
        rel = "/".join(parts[:i]) + ".py"
        if (root / rel).is_file():
            return _symbols_gone(root / rel, rel, parts[i:])
    guess = next(("/".join(parts[:i + 1]) + ".py" for i, p in enumerate(parts) if i and p.startswith("test")),
                 "/".join(parts[:2]) + ".py")
    return f"{guess} no longer exists"


def _rule_ids() -> set:
    ids = set()

    def collect(x):
        if isinstance(x, dict):
            if isinstance(x.get("id"), str):
                ids.add(x["id"])
            for v in x.values():
                collect(v)
        elif isinstance(x, list):
            for v in x:
                collect(v)

    base = _handoff() / "lessons" / "rules"
    for f in sorted(base.rglob("*.json")) if base.exists() else []:
        try:
            collect(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return ids


def _guard_rule_gone(token: str) -> str:
    source = _read(DEVKIT_LIB / "guard.py", None) + _read(DEVKIT_LIB / "lessonrules.py", None)
    if token in _rule_ids() or _has_word(source, token):
        return ""
    return f"guard rule {token} no longer exists (not in devkit's guard nor in the lesson rules)"


def _skill_gone(token: str, root) -> str:
    m = _SKILL.fullmatch(token)
    if not m or (m.group("plugin") and m.group("plugin") != "devkit"):
        return ""  # another plugin's skill: devkit cannot see its install
    name = m.group("name")
    if (DEVKIT_HOME / "plugin" / "skills" / name / "SKILL.md").is_file():
        return ""
    rules = _read(DEVKIT_HOME / "config" / "skill-rules.json")
    if f'"{name}"' in rules:
        return ""
    if root and any(p.endswith(f"skills/{name}/SKILL.md") or p == f"{name}/SKILL.md" for p in _walk(root)[0]):
        return ""
    return f"skill {name} no longer exists (not in the repository's or devkit's skills)"


def _part_gone(part: str, kind: str, root) -> str:
    tokens = [t.rstrip(".:") for t in part.split()]
    paths = [i for i, t in enumerate(tokens) if _is_path(t)]
    for n, i in enumerate(paths):
        reason = _path_gone(tokens[i], tokens[i + 1:paths[n + 1] if n + 1 < len(paths) else None], root)
        if reason:
            return reason
    for t in tokens:
        if re.fullmatch(r"tests?(?:\.\w+)+", t) and root:
            reason = _dotted_gone(t, root)
            if reason:
                return reason
    if paths:
        return ""
    if root:
        for t in tokens:
            name = t.rstrip(".:")
            if re.fullmatch(r"test_\w+", name) and _search(root, name, _is_test_file) is False:
                return f"no test named {name} in the repository any more"
    if len(tokens) != 1 or _HEX.fullmatch(tokens[0]):
        return ""  # free text or a commit: nothing to check
    token = tokens[0].rstrip(".:")
    if kind == "guard" and _RULE_ID.fullmatch(token):
        return _guard_rule_gone(token)
    if kind == "skill":
        return _skill_gone(token, root)
    if kind == "lint" and root and _LINT_ID.fullmatch(token):
        return f"lint rule {token} is no longer named in the repository" if _search(root, token) is False else ""
    return ""


def prevention_gone(lesson: dict, repo_root) -> str:
    """Why the prevention a lesson names no longer exists, or "". The reference (prevention_ref) is read as parts
    separated by , ; · + & or "and", with text in parentheses or quotes left out. A part can name a file (repository
    or handoff relative, with ::names or :names after it, or class and test names after a space), a dotted test id
    (tests.test_x.Class.test_y), a test name (test_…), and, alone, a guard rule id (in devkit's guard or the lesson
    rules), a skill (the repository's skills, devkit's skills, or devkit's skill rules) or a lint rule (named
    somewhere in the repository). The first part that is gone gives the reason. Commit hashes, other plugins' skills,
    free text, and repository paths when repo_root is None are not judged."""
    m = (lesson or {}).get("meta") or {}
    ref = str(m.get("prevention_ref") or "").strip()
    if not ref:
        return ""
    root = _repo(repo_root)
    kind = str(m.get("prevention") or "")
    for part in _parts(ref):
        reason = _part_gone(part, kind, root)
        if reason:
            return reason
    return ""
