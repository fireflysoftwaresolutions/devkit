# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Route a crew member's devkit calls to its lane.

A crew member's shell call runs `dev progress …` and `dev crew …` as the member's lane when the variable
DEVKIT_FROM_AGENT=<agent id> stands in front of each of them. route() writes it there for a call it can place
exactly; the hook passes the rewritten command to the agent's shell.

    route(cmd, aid) -> str | None        the command with each lane call marked, None when there is none to place
    holds_lane_call(cmd, cwd) -> bool    whether the command runs a lane call anywhere, nested shells included
"""
from __future__ import annotations

import ast
import os
import re
import shlex

from .readonly import check_refusal, program_write_reason
from .shellparse import (crew_kind, data_heredoc_end, dev_args, EXPECT_RUN, INTERP, RESERVED, runs_one_command,
                         shell_calls, SHELLS, split_commands, TCL, unwrap)

_DEV_IN_CODE = re.compile(r"(?<![\w.-])dev[\s'\",\[\]]{1,6}(progress|crew)\b(?:[\s'\",\]]{1,6}(-{0,2}[A-Za-z][\w-]*))?")
_SPAWNS = re.compile(
    r"\bsubprocess\b|\bos\.(?:system|popen|exec\w*|spawn\w*|posix_spawn\w*)\b|\bpty\.spawn\b|\bchild_process\b|"
    r"\b(?:execSync|spawnSync|execFile\w*|spawn|exec|system|popen|proc_open|shell_exec|passthru)\s*\(|"
    r"\bIO\.popen\b|\bOpen3\b|\bProcess\.(?:spawn|exec)\b|\bDeno\.(?:run|Command)\b|\bBun\.(?:spawn|\$)|"
    r"\bcreate_subprocess_(?:shell|exec)\b|"
    # AppleScript: do shell script runs a line; Terminal's do script, iTerm's write text and keystroke type one into
    # a terminal
    r"\bdo (?:shell )?script\b|\bwrite text\b|\bkeystroke\b")
_SPAWNS_QUOTED = re.compile(r"`[^`]*`|\bqx\b|%x[\[{(]")  # backticks run a command in perl, ruby and php only

# A crew member's compound command with lane calls in it (`cd <worktree> && dev progress step "…"`, `dev crew inbox |
# tail -5`, `git status && dev crew inbox`) is routed too when the shell reads it as a flat list of simple commands
# the router can place exactly: words, `&&`, `||`, `;`, `|` and newlines, no substitution but a quoted heredoc message,
# no subshell, group, control word, comment, background job or heredoc, no redirect but to /dev/null or between stderr
# and stdout. It holds one lane call, which starts its pipeline (nothing feeds it), is typed as `dev …` or a literal
# path to it, and may be followed only by read-only filters (`| tail -5`, `| grep x`); it gets DEVKIT_FROM_AGENT in
# front of it. Every other command of the list is a read (_route_read), and is judged again as any command of the lane
# (crew_shell_verdict with `routed`).
_ROUTE_REDIRECT = re.compile(r"(?:[12]?>&[12]|(?:[12]|&)?>>?[ \t]*/dev/null)(?=[ \t\n;&|]|$)")
_ROUTE_FILTERS = frozenset({"head", "tail", "grep", "egrep", "fgrep", "rg", "wc", "cat", "sort", "uniq", "cut", "tr",
                            "nl", "column", "sed", "jq"})
LITERAL_DEV = re.compile(r"(?:~?[A-Za-z0-9_.+-]*/)*dev\Z")


def _route_tokens(cmd: str):
    """The words, operators and redirects of a command in the router's flat grammar, as [(kind, text, start)];
    None for anything outside it."""
    out, i, n = [], 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c in " \t":
            i += 1
            continue
        if cmd.startswith(("&&", "||"), i):
            out.append(("op", cmd[i:i + 2], i))
            i += 2
            continue
        if c in ";|\n":
            out.append(("op", ";" if c == "\n" else c, i))
            i += 1
            continue
        m = _ROUTE_REDIRECT.match(cmd, i)
        if m:
            out.append(("redir", m.group(0), i))
            i = m.end()
            continue
        if c in "&<>(){}#`":
            return None
        start, q = i, None
        while i < n:
            c = cmd[i]
            if q == "'":
                q = None if c == "'" else q
                i += 1
                continue
            if cmd.startswith("$(", i):
                end = data_heredoc_end(cmd, i)
                if end < 0:
                    return None
                i = end
                continue
            if c == "`":
                return None
            if q == '"':
                if c == "\\":
                    i += 2
                    continue
                q = None if c == '"' else q
                i += 1
                continue
            if c == "\\":
                if cmd[i + 1:i + 2] in ("\n", ""):
                    return None
                i += 2
                continue
            if c in "'\"":
                q = c
            elif c in " \t\n;&|<>()":
                break
            i += 1
        if q:
            return None
        out.append(("word", cmd[start:i], start))
    return out


def _route_read(argv, text) -> bool:
    """True when one command of a routed list only reads: `cd`, or a reader, a read of git, gh or dev the read-only
    allowlist names, with no substitution; never an interpreter, a shell or a project check, which run code."""
    inner = unwrap(argv)
    if not inner or not runs_one_command(text):
        return False
    name = os.path.basename(inner[0])
    if INTERP.match(name) or name in SHELLS or name in ("eval", "source", ".", "xargs", "find") or \
            check_refusal(inner) is None or crew_kind(dev_args(inner) or []) in ("team", "sync"):
        return False
    return program_write_reason(argv, os.getcwd()) is None


def _route_plan(cmd: str):
    """For a compound command in the router's grammar: the offsets of its lane calls' first words, [] when it has
    none; None when it is outside the grammar or a lane call is where the router cannot place it."""
    toks = _route_tokens(cmd or "")
    if not toks:
        return None
    pipes, pipe, words, last = [], [], [], ";"
    for kind, text, at in toks:
        if kind == "op":
            if not words:
                return None  # an empty command: a leading operator, `&& &&`, `| |`
            pipe.append(words)
            words = []
            if text != "|":
                pipes.append(pipe)
                pipe = []
            last = text
        elif kind == "word":
            words.append((text, at))
        elif not words:
            return None  # a redirect before the command word
    if words:
        pipe.append(words)
    elif last != ";":
        return None  # a pipe, `&&` or `||` with nothing after it
    if pipe:
        pipes.append(pipe)
    lanes = []
    for pipe in pipes:
        for k, words in enumerate(pipe):
            if words[0][0] in RESERVED or words[0][0] in ("for", "function", "[[", "]]"):
                return None  # a control word: the list is not flat
            text = cmd[words[0][1]:words[-1][1] + len(words[-1][0])]
            try:
                argv = shlex.split(text)
            except ValueError:
                return None
            if crew_kind(dev_args(unwrap(argv)) or []) != "lane":
                if not _route_read(argv, text):
                    return None  # the rewrite approves nothing but the lane calls and reads
                continue
            if k or not LITERAL_DEV.match(words[0][0]) or not runs_one_command(text):
                return None  # fed by a pipe, behind a wrapper or a variable, or holding a substitution
            for fw in pipe[1:]:
                ftext = cmd[fw[0][1]:fw[-1][1] + len(fw[-1][0])]
                try:
                    fargv = shlex.split(ftext)
                except ValueError:
                    return None
                if not fargv or fargv[0] not in _ROUTE_FILTERS or not runs_one_command(ftext) or \
                        program_write_reason(fargv, os.getcwd()):
                    return None
            lanes.append(words[0][1])
    return lanes


def route(cmd: str, aid: str):
    """The command a crew member's shell call becomes so that each of its `dev progress …` and `dev crew ask|answer|
    inbox|…` calls acts as the member's lane (DEVKIT_FROM_AGENT=<aid> in front of each), or None when it has no lane
    call or one the router cannot place. A single call is the whole command (a quoted heredoc message is data); a
    compound command follows _route_plan. The result is read back with devkit's own tokenizer: each lane call it
    finds must carry the variable, or the command is not routed."""
    if not isinstance(cmd, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(aid or "")):
        return None
    head = f"DEVKIT_FROM_AGENT={aid}"
    try:
        argv = shlex.split(cmd) if runs_one_command(cmd) else None
    except ValueError:
        argv = None
    if argv and LITERAL_DEV.match(cmd.split(None, 1)[0]) and crew_kind(dev_args(argv) or []) == "lane":
        return f"{head} {cmd.strip()}"
    plan = _route_plan(cmd)
    if not plan or len(plan) != 1:
        return None  # one lane call per command: two are two commands
    out = cmd
    for at in sorted(plan, reverse=True):
        out = out[:at] + head + " " + out[at:]
    segs, before = split_commands(out), split_commands(cmd)
    if segs is None or before is None or len(segs) != len(before):
        return None
    marked = 0
    for argv0, _ in segs:
        if crew_kind(dev_args(unwrap(argv0)) or []) == "lane":
            if argv0[:1] != [head]:
                return None
            marked += 1
    return out if marked == len(plan) else None


def _dev_calls(argv, code) -> list:
    """The `dev progress|crew` calls of one program, as [(kind, args)]."""
    if code is not None:  # an interpreter: only a program that can start a process runs dev
        fam = re.sub(r"[0-9.]+$", "", os.path.basename(argv[0]).casefold())
        if not _code_spawns(code, fam):
            return []
        found = [[m.group(1)] + ([m.group(2)] if m.group(2) else []) for m in _DEV_IN_CODE.finditer(code)]
        return [(crew_kind(a), a) for a in found if crew_kind(a)]
    args = dev_args(argv)
    if args is None and argv[0][:1] in ("$", "`") and argv[1:2] in (["progress"], ["crew"]):
        args = argv[1:]  # `$D crew …`: a program devkit cannot see, called the way dev is
    kind = crew_kind(args) if args is not None else ""
    return [(kind, args)] if kind else []


def holds_lane_call(cmd: str, cwd) -> bool:
    """True when a shell command runs a `dev progress` or `dev crew` lane call at any depth devkit reads (a
    pipeline, a list, a loop, a nested shell, a substitution, a program's code)."""
    calls = shell_calls(cmd, cwd)
    return bool(calls) and any(kind == "lane" for argv, code in calls for kind, _ in _dev_calls(argv, code))


# What in a crew member's command strips or sets the variables devkit reads a caller from: the lane variable, the
# call token and Codex's thread id. Read on the text, as a typo guard for the advice (hooks._identity_strip_advice):
# the call token's bindings, not this list, decide who calls.
_IDENTITY_VARS = r"(?:DEVKIT_FROM_AGENT|DEVKIT_CALL|DEVKIT_SPEAKER|DEVKIT_SESSION|CODEX_THREAD_ID)"
_STRIPS = (
    (re.compile(r"\benv\b[^;&|\n]*?\s(?:-u[ \t]*|--unset[= ])(" + _IDENTITY_VARS + r")\b"), "env -u {}"),
    (re.compile(r"\benv[ \t]+(?:-[A-Za-z]*i[A-Za-z]*|--ignore-environment|-)(?=[ \t]|$)"), "env -i"),
    (re.compile(r"\bunset\b[^;&|\n]*?\b(" + _IDENTITY_VARS + r")\b"), "unset {}"),
    (re.compile(r"(?:^|[\s;&|(`])(?:export[ \t]+|declare[ \t]+-x[ \t]+|typeset[ \t]+-x[ \t]+)?"
                r"(DEVKIT_CALL|CODEX_THREAD_ID)="), "{}="),
)


def identity_strips(cmd: str) -> list:
    """The forms in a shell command that strip or set devkit's identity variables: `env -u DEVKIT_FROM_AGENT`,
    `env -i`, `unset DEVKIT_CALL`, an assignment to DEVKIT_CALL or CODEX_THREAD_ID. [] for any other command."""
    found = []
    for rx, form in _STRIPS:
        for m in rx.finditer(cmd if isinstance(cmd, str) else ""):
            text = form.format(*m.groups()) if m.groups() else form
            if text not in found:
                found.append(text)
    return found


# interpreter code that can call a function it names with a string it builds (getattr, __import__, eval, a variable
# call), so a spawn the _SPAWNS table cannot see may hide in it. Per language, and only the builtins: a method
# of the same name (re.compile, model.eval) is not one.
_DYNAMIC_CODE = {
    "python": re.compile(r"(?<![.\w])(?:getattr|eval|compile|vars|globals|locals)\s*\(|\b(?:__import__|importlib|"
                         r"__builtins__|__getattribute__|__dict__)\b"),
    "js": re.compile(r"(?<![.\w])(?:Function|eval)\s*\(|\bReflect\s*\.|\bglobalThis\s*\[|\brequire\s*\(\s*[^'\"\s)]|"
                     r"\bimport\s*\(\s*[^'\"\s)]|\bprocess\s*\.\s*(?:binding|dlopen)\b"),
    "ruby": re.compile(r"\b(?:public_send|__send__|instance_eval|class_eval|eval)\b|\.send\s*\("),
    "perl": re.compile(r"\beval\b|&\s*\{|&\$\w+|\bdo\s+[\"'$]"),
    "php": re.compile(r"\bcall_user_func\w*|\$\w+\s*\(|(?<![.\w>])eval\s*\("),
    "": re.compile(r"\beval\b|\bload(?:string)?\s*\(|\bdo\.call\b|\brun script\b"),
}
# perl's and ruby's exec and system with a list and no parentheses: exec "gh","pr","merge","12"
_BARE_LIST = re.compile(r"\b(?:exec|system)\s+((?:'[^'\n]*'|\"[^\"\n]*\")(?:\s*,\s*(?:'[^'\n]*'|\"[^\"\n]*\"))*)")


def _dynamic_code(name, code) -> bool:
    """True when an interpreter's program can call a function whose name it builds at run time, judged on its
    code outside string literals: an edit script whose strings hold JavaScript with `eval(` builds nothing."""
    return bool(_dynamic_match(name, code))


def _dynamic_match(name, code):
    """The match of _dynamic_code in a program's code outside string literals, or None. Python is read with its own
    parser (_py_dynamic) and JavaScript by _js_dynamic; code that does not parse is matched by the patterns."""
    if not code:
        return None
    fam = re.sub(r"[0-9.]+$", "", os.path.basename(str(name or "")).casefold())
    fam = {"pypy": "python", "node": "js", "nodejs": "js", "deno": "js", "bun": "js"}.get(fam, fam)
    if fam == "python":
        found = _py_dynamic(code)
        if found is not False:
            return _Found(found) if found else None
    if fam == "js":
        return _js_dynamic(_outside_strings(code))
    return _DYNAMIC_CODE.get(fam, _DYNAMIC_CODE[""]).search(_outside_strings(code))


class _Found:
    """A match-like result of _py_dynamic: group(0) is the code that names a function at run time."""

    def __init__(self, text):
        self.text = text

    def group(self, _=0):
        return self.text


# The names of the Python functions and methods that start a process (subprocess, os, pty, asyncio): a lookup of one
# by name reaches a spawn devkit does not see as a call
_PY_SPAWN_NAMES = frozenset({
    "run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput", "system", "popen",
    "startfile", "spawn", "fork", "forkpty", "create_subprocess_exec", "create_subprocess_shell", "run_path",
    "run_module", "exec_module", "load_module", "CDLL", "cdll", "LoadLibrary", "dlopen"}) | frozenset(
    f"{a}{b}" for a in ("exec", "spawn", "posix_spawn") for b in ("", "l", "le", "lp", "lpe", "v", "ve", "vp", "vpe", "p"))
# the modules whose functions start a process or load code: a module named by a string is one of them only by name
_PY_SPAWN_MODULES = frozenset({"subprocess", "os", "pty", "posix", "nt", "asyncio", "multiprocessing", "runpy",
                               "importlib", "ctypes", "code", "codeop", "commands", "popen2", "platform", "shutil",
                               "webbrowser", "pdb", "builtins", "__builtin__", "operator", "sys", "concurrent"})
# attributes that reach the interpreter's internals: a sandbox escape, never a probe's need
_PY_ESCAPES = frozenset({"__builtins__", "__getattribute__", "__globals__", "__subclasses__", "__bases__", "__mro__",
                         "__code__", "__closure__", "f_globals", "f_builtins", "f_locals", "gi_frame", "__loader__",
                         "__reduce__", "__reduce_ex__"})


def _py_dynamic(code):
    """What in a Python program calls a function by a name it builds at run time, read with Python's parser: '' for
    nothing, the code text for the first such call, False when the code does not parse. Calls by a computed name:
    eval, exec, compile, breakpoint; getattr with a name that is no literal, or that is a spawn function's name
    (_PY_SPAWN_NAMES); __import__ and importlib.import_module of a name that is no literal, or of a module that can
    start a process (_PY_SPAWN_MODULES); a subscript of globals(), locals(), vars() or __dict__ by a name that is no
    literal; operator.attrgetter and methodcaller; and the interpreter's internals (_PY_ESCAPES). A lookup by a
    literal name (getattr(r, 'returncode'), vars(r)['args']) names what it calls."""
    import ast
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False

    def text(node):
        try:
            return ast.get_source_segment(code, node) or type(node).__name__
        except Exception:
            return type(node).__name__

    def literal(node):
        return isinstance(node, ast.Constant) and isinstance(node.value, str)

    def fname(node):
        f = node.func
        return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""

    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in _PY_ESCAPES:
            return text(n)
        if isinstance(n, ast.Name) and n.id in _PY_ESCAPES:
            return text(n)
        if isinstance(n, ast.Subscript):
            base = n.value
            index = getattr(ast, "Index", None)  # Python 3.8 wraps a subscript's key; 3.9 and later do not
            key = n.slice.value if index is not None and isinstance(n.slice, index) else n.slice
            scoped = (isinstance(base, ast.Call) and fname(base) in ("globals", "locals", "vars")) or \
                (isinstance(base, ast.Attribute) and base.attr == "__dict__")
            if scoped and (not literal(key) or key.value in _PY_SPAWN_NAMES):
                return text(n)
        if not isinstance(n, ast.Call):
            continue
        name = fname(n)
        args = n.args
        if (name in ("eval", "exec", "compile", "breakpoint") and isinstance(n.func, ast.Name)) or \
                name in ("attrgetter", "methodcaller"):  # a method of that name (re.compile, model.eval) is none
            return text(n)
        if name == "getattr" and isinstance(n.func, ast.Name) and \
                (len(args) < 2 or not literal(args[1]) or args[1].value in _PY_SPAWN_NAMES):
            return text(n)
        if name == "__import__" or (name == "import_module" and isinstance(n.func, (ast.Attribute, ast.Name))):
            if not args or not literal(args[0]) or args[0].value.split(".")[0] in _PY_SPAWN_MODULES:
                return text(n)
        if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Call) and \
                fname(n.func.value) in ("globals", "locals", "vars") and n.func.attr in ("get", "pop", "setdefault"):
            if not args or not literal(args[0]) or args[0].value in _PY_SPAWN_NAMES:
                return text(n)
    return ""


# JavaScript: code that runs a string or reaches the global scope by a computed name, always; a module loaded by a
# name it computes, only with a call through a computed name (`m[k](…)`, `m[k].call(…)`), which may be a spawn
_JS_ALWAYS = re.compile(r"(?<![.\w])(?:Function|eval)\s*\(|\bReflect\s*\.|\bglobalThis\s*\[|\bprocess\s*\.\s*"
                        r"(?:binding|dlopen)\b|\bvm\s*\.\s*run\w*|\brunIn\w*Context\b")
_JS_LOADS = re.compile(r"\brequire\s*\(\s*[^'\"\s)]|\bimport\s*\(\s*[^'\"\s)]")
_JS_COMPUTED_CALL = re.compile(r"\]\s*(?:\(|\.\s*(?:call|apply|bind)\s*\()")
# a module named by strings spliced together (require('child' + '_process'), a template literal): any module
_JS_SPLICED = re.compile(r"""\b(?:require|import)\s*\(\s*(?:(['"])[^'"\n]{0,200}\1\s*\+|`)""")


def _js_dynamic(bare):
    """The match of a JavaScript program's code (outside string literals) that may call a function by a name it
    builds at run time, or None (_JS_ALWAYS, _JS_LOADS with _JS_COMPUTED_CALL)."""
    m = _JS_ALWAYS.search(bare) or _JS_SPLICED.search(bare)
    if m:
        return m
    m = _JS_LOADS.search(bare)
    return m if m and _JS_COMPUTED_CALL.search(bare) else None


# a module name is a string, and naming child_process is how JavaScript reaches its spawn functions
_SPAWN_MODULE = re.compile(r"""(?:require|import)\s*\(?\s*['"](?:node:)?child_process['"]|"""
                           r"""\bfrom\s+['"](?:node:)?child_process['"]""")


def _outside_strings(code) -> str:
    """The program text with the contents of its string literals blanked (the quotes stay), so a marker that only
    appears inside a string (JavaScript or CSS an edit script carries) is not read as the program's own code."""
    def blank(m):
        t = m.group(0)
        q = 3 if t[:3] in ("'''", '"""') else 1
        return t[:q] + re.sub(r"[^\n]", " ", t[q:-q]) + t[-q:]
    return _STR_LIT.sub(blank, code or "")


def _code_spawns(code, name="") -> bool:
    """True when a program can start a process: a spawn function in its code outside string literals, a spawning
    module it names, backticks in perl, ruby or php (where they run a command), or a function it names at run time."""
    if not code:
        return False
    fam = re.sub(r"[0-9.]+$", "", os.path.basename(str(name or "")).casefold())
    if fam in TCL and (EXPECT_RUN.search(code) or re.search(r"\bopen\s+[\"{]?\|", code)):
        return True  # Tcl starts a process with spawn, exec, system or `open "|cmd"`
    bare = _outside_strings(code)
    return bool(_SPAWNS.search(bare) or _SPAWN_MODULE.search(code) or _BARE_LIST.search(bare) or
                (fam in ("perl", "ruby", "php") and _SPAWNS_QUOTED.search(code)) or _dynamic_code(name, code))


_STR_LIT = re.compile(r"'''(.*?)'''|\"\"\"(.*?)\"\"\"|'((?:\\.|[^'\\\n])*)'|\"((?:\\.|[^\"\\\n])*)\"|`([^`]*)`", re.S)
