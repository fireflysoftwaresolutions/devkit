# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shell completion for `dev`, generated from its own parser, and the live values it offers.

`dev completion zsh|bash` prints the scripts that shell/completions/_dev and shell/completions/dev.bash hold. Both come
from cli.build_parser(): every command, subcommand, option and choice, with the help text as the description, so a new
command or flag is offered as soon as the parser has it. selfcheck D13 fails when a checked-in script differs from what
the parser generates now, or when a rule below names an argument the parser no longer has.

Live values (projects, initiatives, task ids, plan steps, crew members, kinds of work, plans, branches, worktrees,
pinned copies) come from `dev __complete <kind>`, which bin/dev answers without building the parser (about 35 ms with
Homebrew's Python, 100 ms with /usr/bin/python3). The shell side keeps them for a few seconds and waits at most 0.1 s
(_dev_wait) for the command, 0.5 s (_dev_first_wait) when it has no values of that kind yet, and whole seconds in
bash 3.2: past it, it offers the last values it had and picks up the new ones on a later Tab.

The zsh helpers (fzf pickers and their key bindings) are on only when the developer said yes to `dev setup shell`;
helpers_mode() reads that answer and shell/devkit.zsh reads the same file."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import core

# kind: (what one value is, for the shell's heading; whether the values depend on the folder you are in)
KINDS = {
    "projects": ("project", False),
    "initiatives": ("initiative", False),
    "tasks": ("task", True),
    "steps": ("plan step", True),
    "crew": ("crew member", False),
    "activities": ("kind of work", False),
    "plans": ("plan", True),
    "branches": ("branch", True),
    "worktrees": ("worktree", True),
    "versions": ("pinned copy", False),
    "places": ("place", False),
}
OLD_KINDS = {"aliases": "projects"}  # what older shell files ask for
GIT_TIMEOUT = 2  # seconds; the shell stops waiting long before

# Where each live value is offered: (command path, argument, values). A path word "*" matches any command there; the
# argument is a positional's name (its dest) or one of an option's strings. Values are parts joined by "+": a live kind
# (KINDS) with an optional ":arg" ("@1": the first argument typed after the command, such as the project of
# `dev go <project> <branch>`), "words:a,b" (fixed words), "files", "dirs", or "exec:<command>" (the rest of the line
# completes as that command's). selfcheck D13 reports a rule that matches no argument of the parser.
RULES = (
    (("go",), "query", "projects"),
    (("go",), "branch", "worktrees:@1"),
    (("status",), "query", "projects"),
    (("open",), "query", "projects"),
    (("claude",), "query", "projects"),
    (("codex",), "query", "projects"),
    (("claude",), "--worktree", "worktrees:@1"),
    (("codex",), "--worktree", "worktrees:@1"),
    (("handoff",), "query", "projects"),
    (("sec",), "query", "projects"),
    (("du",), "query", "projects"),
    (("clone",), "repo", "projects:ids"),
    (("ci",), "query", "words:status,audit,init,watch+projects"),
    (("alias",), "alias", "words:rm"),
    (("audit",), "--project", "projects:ids"),
    (("session",), "--project", "projects:ids"),
    (("initiative", "new"), "--projects", "projects:ids"),
    (("initiative", "add"), "slug", "initiatives"),
    (("initiative", "rm"), "slug", "initiatives"),
    (("initiative", "show"), "slug", "initiatives"),
    (("task", "link"), "--to", "initiatives"),
    (("task", "*"), "selector", "tasks"),
    (("task", "show"), "selector", "tasks:all"),
    (("task", "history"), "selector", "tasks:all"),
    (("task", "reopen"), "selector", "tasks:all"),
    (("progress", "*"), "--step", "steps"),
    (("progress", "step"), "--approves", "steps"),
    (("progress", "assign"), "which", "steps"),
    (("progress", "begin"), "which", "steps"),
    (("progress", "reopen"), "which", "steps"),
    (("progress", "skip"), "which", "steps"),
    (("progress", "assign"), "--to", "crew"),
    (("progress", "assign"), "--activity", "activities"),
    (("crew", "raci"), "work", "activities+crew"),
    (("crew", "show"), "name", "crew"),
    (("crew", "set"), "name", "crew"),
    (("crew", "brief"), "name", "crew"),
    (("crew", "brief"), "--step", "steps"),
    (("crew", "brief"), "--activity", "activities"),
    (("crew", "ask"), "--to", "crew"),
    (("crew", "ask"), "--approves", "steps"),
    (("crew", "delegate"), "--to", "crew"),
    (("crew", "delegate"), "--after", "steps"),
    (("crew", "decline"), "step", "steps"),
    (("crew", "next"), "--first", "steps"),
    (("audit",), "--member", "crew"),
    (("lesson", "add"), "--activity", "activities"),
    (("lesson", "anchor"), "--activity", "activities"),
    (("plan", "revise"), "path", "plans"),
    (("plan", "approve"), "path", "plans"),
    (("plan", "supersede"), "path", "plans"),
    (("wt", "new"), "--repo", "projects"),
    (("wt", "new"), "--base", "branches"),
    (("wt", "rm"), "target", "worktrees"),
    (("branch", "new"), "--base", "branches"),
    (("branch", "embargo"), "branch", "branches"),
    (("backup",), "branches", "words:policy+branches"),
    (("rebase",), "--base", "branches"),
    (("pr", "create"), "--base", "branches"),
    (("ci",), "--branch", "branches"),
    (("run", "ls"), "--branch", "branches"),
    (("test",), "--since", "branches"),
    (("test", "*"), "--since", "branches"),
    (("stubs",), "--since", "branches"),
    (("diff",), "args", "branches+files"),
    (("install",), "--version", "versions"),
    (("adopt",), "path", "dirs"),
    (("files",), "path", "dirs"),
    (("find",), "path", "dirs"),
    (("handoff-import",), "staging", "dirs"),
    (("git",), "-C", "dirs"),
    (("git",), "args", "exec:git"),
    (("gh",), "args", "exec:gh"),
    (("diagram",), "rest", "words:render,check,new+files"),
)
# Arguments named like these take a project, an initiative, a file or a folder in every command (unless a rule says
# otherwise).
DESTS = {"project": "projects", "initiative": "initiatives", "path": "files", "paths": "files", "tests": "files",
         "transcript": "files", "body_file": "files", "notes_file": "files", "export": "files", "out": "files",
         "write": "files", "cwd": "dirs"}
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")
_COMMA = re.compile(r"^comma list: ([a-z0-9-]+(?:,[a-z0-9-]+)+)")


# --------------------------------------------------------------------------- the parser, as a tree

def short(text, width: int = 72) -> str:
    """A help text as a one-line description: up to the first ";", ": ", " (", " · " or " — " outside parentheses
    (when at least 12 characters come before it), then at most `width` characters, cut at a word."""
    t = " ".join(str(text or "").split())
    depth, cut = 0, len(t)
    for i, ch in enumerate(t):
        if ch == "(" and not (i >= 12 and t.startswith(" (", i - 1)):
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        if depth == 0 and i >= 12 and any(t.startswith(s, i) for s in ("; ", ": ", " (", " · ", " — ")):
            cut = i
            break
    t = t[:cut].rstrip(" .,;")
    if len(t) > width:
        head = t[:width - 1]
        if t[width - 1] != " ":
            head = head.rsplit(" ", 1)[0]
        t = head.rstrip(" .,;") + "…"
    return t


def _parse_values(spec: str) -> dict:
    v = {"live": [], "words": [], "files": "", "exec": ""}
    for part in spec.split("+"):
        name, _, arg = part.partition(":")
        if name == "words":
            v["words"] += arg.split(",")
        elif name in ("files", "dirs"):
            v["files"] = name
        elif name == "exec":
            v["exec"] = arg
        elif name in KINDS:
            v["live"].append((name, arg))
        else:
            raise ValueError(f"completion rule: unknown values {part!r}")
    return v


def _rule(path: tuple, a, used: set):
    """The values the last matching RULE gives the argument (specific rules follow the general ones), else None."""
    keys = set(a.option_strings) or {a.dest}
    hit = None
    for i, (rpath, arg, _) in enumerate(RULES):
        if arg in keys and len(rpath) == len(path) and all(r in ("*", w) for r, w in zip(rpath, path)):
            hit = i
            used.add(i)
    return None if hit is None else _parse_values(RULES[hit][2])


def _values(a, path: tuple, used: set) -> dict:
    """What completes an argument's value: {message, choices, comma, live, words, files, exec}."""
    import argparse
    v = _rule(path, a, used) or {"live": [], "words": [], "files": "", "exec": ""}
    v.update(choices=[], comma=[])
    metavar = a.metavar if isinstance(a.metavar, str) else ""
    if a.choices and not v["live"] and not v["words"]:
        v["choices"] = [str(c) for c in a.choices]
    elif "|" in metavar and all(_WORD.match(w) for w in metavar.split("|")) and not v["live"] and not v["words"]:
        v["choices"] = metavar.split("|")
    elif not any((v["live"], v["words"], v["files"], v["exec"])):
        m = _COMMA.match(a.help or "") if a.help is not argparse.SUPPRESS else None
        if m:
            v["comma"] = m.group(1).split(",")
        elif a.dest in DESTS and DESTS[a.dest] in KINDS:
            v["live"] = [(DESTS[a.dest], "")]
        elif a.dest in DESTS:
            v["files"] = DESTS[a.dest]
    label = KINDS[v["live"][0][0]][0] if v["live"] else ""
    v["message"] = label or (metavar if metavar and "|" not in metavar else a.dest).replace("_", " ").lower()
    return v


def tree(parser=None) -> dict:
    """-> the root command: {path, help, options, positionals, commands, rules_used}. Hidden commands and arguments
    (help SUPPRESS: `dev hook`, `dev __complete`) are left out, as `dev --help` leaves them out."""
    import argparse
    if parser is None:
        from . import cli
        parser = cli.build_parser()
    used = set()

    def node(p, path, help_):
        groups = {}
        for g in p._mutually_exclusive_groups:
            strings = [s for a in g._group_actions for s in a.option_strings]
            for a in g._group_actions:
                groups[id(a)] = strings
        opts, pos, cmds = [], [], []
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                hidden = {c.dest for c in a._choices_actions if c.help == argparse.SUPPRESS}
                helps = {c.dest: c.help for c in a._choices_actions}
                for name, child in a.choices.items():
                    if name not in hidden:
                        cmds.append(node(child, path + (name,), helps.get(name) or child.description or ""))
                continue
            if a.help == argparse.SUPPRESS:
                continue
            if a.option_strings:
                takes = 0 if a.nargs == 0 else (a.nargs or 1)
                repeat = isinstance(a, (argparse._AppendAction, argparse._AppendConstAction, argparse._CountAction))
                plain = "show the help of dev, or of the command after it" if a.dest == "help" else a.dest
                opts.append({"strings": list(a.option_strings), "help": short(a.help or plain.replace("_", " ")),
                             "takes": takes, "repeat": repeat, "exclusive": groups.get(id(a), []),
                             "help_action": isinstance(a, argparse._HelpAction),
                             "values": _values(a, path, used) if takes else None})
            else:
                pos.append({"name": a.dest, "nargs": a.nargs, "values": _values(a, path, used)})
        if cmds and pos:
            raise ValueError(f"completion: `dev {' '.join(path)}` has both arguments and subcommands; teach "
                             "lib/devkit/completion.py how to complete it")
        return {"path": path, "help": short(help_), "options": opts, "positionals": pos, "commands": cmds}

    root = node(parser, (), "")
    root["rules_used"] = used
    return root


def walk(node):
    yield node
    for c in node["commands"]:
        yield from walk(c)


def unused_rules(root=None) -> list:
    """The RULES that match no argument of the parser: a renamed command or argument that lost its completion."""
    root = root or tree()
    return [r for i, r in enumerate(RULES) if i not in root["rules_used"]]


# --------------------------------------------------------------------------- zsh

def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _zfn(path) -> str:
    return "_dev" + "".join("__" + re.sub(r"[^A-Za-z0-9]", "_", w) for w in path)


def _zexpl(s: str) -> str:
    return "[" + s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]") + "]"


def _zmsg(s: str) -> str:
    return s.replace("\\", "\\\\").replace(":", "\\:")


def _zword(s: str) -> str:
    return re.sub(r"([\s()\\'\"$`;&|<>*?\[\]{}#~])", r"\\\1", s)


def _zaction(v: dict) -> str:
    # An action that starts with a space is called as written: _arguments puts no compadd options before its
    # arguments, which _dev_live and _dev_exec read by position.
    if v["exec"]:
        return f" _dev_exec {v['exec']}"
    if v["live"]:
        kinds = ",".join(k for k, _ in v["live"])
        arg = ",".join(a for _, a in v["live"] if a) or "-"
        return " " + " ".join(["_dev_live", kinds, arg] + [_zword(w) for w in v["words"]] +
                              (["--files" if v["files"] == "files" else "--dirs"] if v["files"] else []))
    if v["words"] and v["files"]:
        return " " + " ".join(["_dev_live", "-", "-"] + [_zword(w) for w in v["words"]] + ["--files"])
    if v["choices"] or v["words"]:
        return "(" + " ".join(_zword(w) for w in v["choices"] + v["words"]) + ")"
    if v["comma"]:
        return " ".join(["_values", "-s", ",", _zword(v["message"])] + [_zword(w) for w in v["comma"]])
    if v["files"] == "dirs":
        return "_files -/"
    if v["files"]:
        return "_files"
    return " "


def _zoption(o: dict) -> str:
    strings = o["strings"]
    if o["help_action"]:
        head = "(- *)"
    elif o["repeat"]:
        head = "*"
    else:
        excl = sorted(set(o["exclusive"]) | (set(strings) if len(strings) > 1 else set()))
        head = f"({' '.join(excl)})" if excl else ""
    tail = _zexpl(o["help"])
    if o["takes"]:
        v = o["values"]
        tail += (":" if o["takes"] != "?" else "::") + _zmsg(v["message"]) + ":" + _zaction(v)
    forms = [s + (("=" if s.startswith("--") else "+") if o["takes"] else "") for s in strings]
    if len(forms) == 1:
        return _q(head + forms[0] + tail)
    return (_q(head) if head else "") + "{" + ",".join(forms) + "}" + _q(tail)


def _zpositional(p: dict) -> list:
    v, nargs = p["values"], p["nargs"]
    body = _zmsg(v["message"]) + ":" + _zaction(v)
    if nargs == "...":
        return [_q("*::" + body)]
    if nargs in ("*", "+"):
        return [_q("*:" + body)]
    if nargs == "?":
        return [_q("::" + body)]
    return [_q(":" + body)] * (nargs if isinstance(nargs, int) else 1)


def _zfunction(n: dict) -> list:
    name, path = _zfn(n["path"]), n["path"]
    specs = [_zoption(o) for o in n["options"]]
    out = [f"{name}() {{"]
    if n["commands"]:
        out += ["  local curcontext=$curcontext state state_descr line ret=1", "  typeset -A opt_args"]
        if not path:
            out += ["  local -a _dev_words", '  _dev_words=("${words[@]}")']
        specs += [_q("1: :->cmds"), _q("*:: :->args")]
    else:
        specs += [s for p in n["positionals"] for s in _zpositional(p)]
    flags = "-C -s -S" if n["commands"] else "-s -S"
    out.append(f"  _arguments {flags} : \\")
    out += [f"    {s} \\" for s in specs[:-1]] + [f"    {specs[-1]}" + (" && ret=0" if n["commands"] else "")]
    if n["commands"]:
        label = "dev " + " ".join(path) + " command" if path else "dev command"
        ctx = "-".join(("dev",) + path)
        out += ["  case $state in", "    (cmds)", "      local -a cmds", "      cmds=("]
        out += [f"        {_q(_zmsg(c['path'][-1]) + ':' + c['help'])}" for c in n["commands"]]
        out += ["      )", f"      _describe -t commands {_q(label)} cmds && ret=0 ;;", "    (args)",
                f"      curcontext=${{curcontext%:*:*}}:{ctx}-$words[1]:", "      case $words[1] in"]
        out += [f"        ({c['path'][-1]}) {_zfn(c['path'])} && ret=0 ;;" for c in n["commands"]]
        out += ["      esac ;;", "  esac", "  return ret"]
    out.append("}")
    return out


ZSH_HEAD = r"""#compdef dev
# zsh completion for devkit's `dev` command. Generated from dev's own parser (lib/devkit/cli.py, build_parser) by
# `dev completion zsh`: do not edit it by hand. After changing a command, run
#   bin/dev completion zsh > shell/completions/_dev
# CI fails while this file differs from the parser (selfcheck D13).

# _dev_live KINDS ARG [WORD…] [--files|--dirs]: offer the fixed WORDs, then devkit's live values of each kind in KINDS
# (comma-separated; ARG narrows them: - for none, @N for the Nth argument typed after the command), then files or
# folders.
_dev_live() {
  local kinds=$1 arg=$2 kind files= ret=1
  local -i i seen=0
  shift 2
  local -a fixed vals
  if [[ $arg == @<-> ]]; then
    for (( i = 2; i < CURRENT; i++ )); do
      [[ $words[i] == -* ]] && continue
      (( ++seen == ${arg#@} )) && { arg=${(Q)words[i]}; break }
    done
    [[ $arg == @<-> ]] && arg=-
  fi
  while (( $# )); do
    case $1 in
      (--files|--dirs) files=$1 ;;
      (*) fixed+=("$1") ;;
    esac
    shift
  done
  (( $#fixed )) && _describe -t commands 'subcommand' fixed && ret=0
  for kind in ${(s:,:)kinds}; do
    [[ $kind == - ]] && continue
    _dev_values $kind $arg
    vals=("${reply[@]}")
    (( $#vals )) && _describe -V -t $kind "${_dev_label[$kind]:-$kind}" vals && ret=0
  done
  [[ $files == --files ]] && _files && ret=0
  [[ $files == --dirs ]] && _files -/ && ret=0
  return ret
}

# _dev_values KIND [ARG]: $reply = the lines of `dev __complete KIND [ARG]`, as value:description, for the project or
# initiative the command line names (-p, -i). They are kept in this shell for $_dev_ttl seconds (10). A Tab that has
# older values to offer waits at most $_dev_wait seconds (0.1) for the command, and the first of its kind in this shell
# $_dev_first_wait (0.5); past it, it offers what it has, and the command finishes in the background for a later Tab.
# A fetch whose file is still missing after $_dev_stale seconds (30) died: the next Tab drops it and starts another.
_dev_values() {
  emulate -L zsh
  setopt extended_glob
  zmodload zsh/datetime 2>/dev/null
  zmodload -F zsh/files b:zf_rm b:zf_mkdir b:zf_mv 2>/dev/null
  local kind=$1 arg=${2:#-} key f out dir=${XDG_CACHE_HOME:-$HOME/.cache}/devkit/complete
  local -a cmd line old
  local -i i fd ok=0
  local -F now=$EPOCHREALTIME ttl=${_dev_ttl:-10} wait=${_dev_wait:-0.1} stale=${_dev_stale:-30}
  reply=()
  cmd=(dev __complete $kind)
  line=("${(@Q)_dev_words}")
  for (( i = 1; i < $#line; i++ )); do
    case $line[i] in
      (-p|--project) cmd+=(--project "$line[i+1]") ;;
      (--project=*) cmd+=(--project "${line[i]#--project=}") ;;
      (-i|--initiative) cmd+=(--initiative "$line[i+1]") ;;
      (--initiative=*) cmd+=(--initiative "${line[i]#--initiative=}") ;;
    esac
  done
  [[ -n $arg ]] && cmd+=("$arg")
  key="${(j: :)${(@q)cmd[3,-1]}}"
  (( ${_dev_here[(Ie)$kind]} )) && key+=" @$PWD"
  if (( ${+_dev_cache[$key]} && now - _dev_cache_at[$key] < ttl )); then
    reply=("${(@f)_dev_cache[$key]}")
    reply=(${reply:#})
    return 0
  fi
  f=$_dev_pending[$key]
  if [[ -n $f && ! -e $f ]] && (( now - ${_dev_pending_at[$key]:-0} >= stale )); then
    zf_rm -f -- $f.part
    unset "_dev_pending[$key]" "_dev_pending_at[$key]"
    f=
  fi
  if [[ -z $f ]]; then
    if [[ ! -d $dir ]]; then
      zf_mkdir -p -m 700 -- $dir 2>/dev/null || return 1
    fi
    if (( ! _dev_swept )); then
      _dev_swept=1
      old=($dir/*(N.mh+24))
      (( $#old )) && zf_rm -f -- $old
    fi
    (( ${+_dev_cache[$key]} )) || wait=${_dev_first_wait:-0.5}
    f=$dir/$$.$(( ++_dev_seq ))
    exec {fd}< <(command "${cmd[@]}" >|$f.part 2>/dev/null; zf_mv -f -- $f.part $f 2>/dev/null; print)
    read -t $wait -u $fd out && ok=1
    exec {fd}<&-
    if (( ! ok )); then
      _dev_pending[$key]=$f
      _dev_pending_at[$key]=$now
    fi
  fi
  if [[ -e $f ]]; then
    out=$(<$f)
    zf_rm -f -- $f
    unset "_dev_pending[$key]" "_dev_pending_at[$key]"
    _dev_cache[$key]=$out
    _dev_cache_at[$key]=$now
  fi
  reply=("${(@f)_dev_cache[$key]}")
  reply=(${reply:#})
  return 0
}

# _dev_exec COMMAND: complete the rest of the line as COMMAND's own (dev git …, dev gh …).
_dev_exec() {
  words=($1 "${words[@]}")
  (( CURRENT++ ))
  _normal
}
"""

ZSH_TAIL = r"""
if [[ $funcstack[1] == _dev ]]; then
  _dev "$@"
elif (( $+functions[compdef] )); then
  compdef _dev dev
fi
"""


def zsh(root=None) -> str:
    """The zsh completion script (shell/completions/_dev)."""
    root = root or tree()
    labels = " ".join(f"{k} {_q(label)}" for k, (label, _) in KINDS.items())
    here = " ".join(k for k, (_, h) in KINDS.items() if h)
    out = [ZSH_HEAD.rstrip("\n"), "",
           "typeset -gA _dev_cache _dev_cache_at _dev_pending _dev_pending_at",
           "typeset -gi _dev_seq _dev_swept",
           f"typeset -gA _dev_label=({labels})",
           f"typeset -ga _dev_here=({here})"]
    for n in walk(root):
        out += [""] + _zfunction(n)
    return "\n".join(out) + "\n" + ZSH_TAIL


# --------------------------------------------------------------------------- bash

def _btoken(v: dict) -> str:
    """One value spec as a bash word: l:<kinds>:<arg>:<words>:<files> (live), c:<words> (choices), m:<words> (comma
    list), f (files), d (folders), n (nothing)."""
    files = {"files": "f", "dirs": "d"}.get(v["files"], "")
    if v["live"] or (v["words"] and files):
        kinds = ",".join(k for k, _ in v["live"]) or "-"
        arg = ",".join(a for _, a in v["live"] if a) or "-"
        return f"l:{kinds}:{arg}:{','.join(v['words'])}:{files}"
    if v["choices"] or v["words"]:
        return "c:" + ",".join(v["choices"] + v["words"])
    if v["comma"]:
        return "m:" + ",".join(v["comma"])
    if v["exec"]:
        return "f"
    return files or "n"


def _bcase(n: dict) -> list:
    cmds = " ".join(c["path"][-1] for c in n["commands"])
    opts = " ".join(s for o in n["options"] for s in o["strings"])
    vals = " ".join(f"{s}={_btoken(o['values'])}" for o in n["options"] if o["takes"] for s in o["strings"])
    pos = " ".join(("*" if p["nargs"] in ("*", "+", "...") else "") + _btoken(p["values"]) for p in n["positionals"])
    sets = [f"c={_q(cmds)}" if cmds else "", f"o={_q(opts)}" if opts else "", f"v={_q(vals)}" if vals else "",
            f"p={_q(pos)}" if pos else ""]
    return [f"    {_q(' '.join(n['path']))}) {' '.join(s for s in sets if s)} ;;"]


BASH_HEAD = r"""# bash completion for devkit's `dev` command. Generated from dev's own parser (lib/devkit/cli.py, build_parser) by
# `dev completion bash`: do not edit it by hand. Load it from your bash startup file with:
#   eval "$(dev completion bash)"
# After changing a command, run: bin/dev completion bash > shell/completions/dev.bash
# CI fails while this file differs from the parser (selfcheck D13).

# _dev_values KIND [ARG]: _dev_reply = the values of `dev __complete KIND [ARG]` (descriptions dropped) for the project
# or initiative the line names (-p, -i), kept for $_dev_ttl seconds (10). A Tab that has older values to offer waits at
# most $_dev_wait seconds (0.1) for the command, and the first of its kind in this shell $_dev_first_wait (0.5); bash
# 3.2 waits whole seconds, so 1 s. Past it, the last values are offered and the command finishes in the background for
# a later Tab. A fetch whose file is still missing after $_dev_stale seconds (30) died: the next Tab starts another.
_dev_values() {
  local kind=$1 arg=$2 key var at= f= since= line w ok= wait=${_dev_wait:-0.1} i
  local dir=${XDG_CACHE_HOME:-$HOME/.cache}/devkit/complete
  local -a cmd vals
  _dev_reply=()
  cmd=(dev __complete "$kind")
  for (( i = 1; i < COMP_CWORD; i++ )); do
    case ${COMP_WORDS[i]} in
      -p|--project|-i|--initiative)
        w=${COMP_WORDS[i+1]}
        [[ $w == = ]] && w=${COMP_WORDS[i+2]}
        case ${COMP_WORDS[i]} in
          -p|--project) cmd+=(--project "$w") ;;
          *) cmd+=(--initiative "$w") ;;
        esac ;;
    esac
  done
  [[ -n $arg && $arg != - ]] && cmd+=("$arg")
  key="${cmd[*]:2}"
  case " $_dev_here " in *" $kind "*) key="$key @$PWD" ;; esac
  # The letters are spelled out: under a UTF-8 locale, bash 3.2's [a-zA-Z] also matches letters such as ñ, which no
  # variable name may hold, so a folder named diseño would break every Tab (and eval would run a word of its path).
  var=_dev_c_${key//[^abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789]/_}
  eval "at=\${${var}_at:-}; f=\${${var}_pending:-}; since=\${${var}_since:-0}"
  if [[ -n $at ]] && (( SECONDS - at < ${_dev_ttl:-10} )); then
    eval "_dev_reply=(\"\${${var}[@]}\")"
    return 0
  fi
  if [[ -n $f && ! -e $f ]] && (( SECONDS - since >= ${_dev_stale:-30} )); then
    rm -f "$f.part"
    f=
  fi
  if [[ -z $f ]]; then
    [[ -d $dir ]] || mkdir -p -m 700 "$dir" 2>/dev/null || return 1
    if [[ -z $_dev_swept ]]; then
      _dev_swept=1
      find "$dir" -maxdepth 1 -type f -mmin +1440 -delete 2>/dev/null
    fi
    f=$dir/$$.$RANDOM$RANDOM
    [[ -n $at ]] || wait=${_dev_first_wait:-0.5}
    if (( BASH_VERSINFO[0] < 4 )); then
      wait=${wait%%.*}
      (( ${wait:-0} < 1 )) && wait=1
    fi
    exec 9< <("${cmd[@]}" >"$f.part" 2>/dev/null; mv -f "$f.part" "$f" 2>/dev/null; echo)
    IFS= read -r -t "$wait" -u 9 line && ok=1
    exec 9<&-
    [[ -n $ok ]] || eval "${var}_pending=\$f; ${var}_since=\$SECONDS"
  fi
  if [[ -e $f ]]; then
    vals=()
    while IFS= read -r line; do
      line=${line//\\:/$'\001'}
      line=${line%%:*}
      [[ -n $line ]] && vals+=("${line//$'\001'/:}")
    done < "$f"
    rm -f "$f"
    eval "${var}_pending=; ${var}_at=\$SECONDS; ${var}=(\"\${vals[@]}\")"
  fi
  eval "_dev_reply=(\"\${${var}[@]}\")"
  return 0
}

# No word below is split by an unquoted expansion, which would also expand it as a file pattern: `*f` (any number of
# files) would match main.tf. Specs are split by pattern matching, compgen's output one line at a time, and no shell
# option changes, so a Ctrl-C during a Tab leaves the shell as it was.

# _dev_add ARGS…: add the words `compgen ARGS…` prints to COMPREPLY, one per line, as they are.
_dev_add() {
  local x
  while IFS= read -r x; do
    COMPREPLY+=("$x")
  done < <(compgen "$@")
}

# _dev_files -f|-d: add the files (-f) or folders (-d) that start with the word being typed to COMPREPLY, each escaped
# as the shell reads it back: x\[1\].txt, not a pattern that names x1.txt when the line runs.
_dev_files() {
  local x q
  while IFS= read -r x; do
    printf -v q '%q' "$x"
    COMPREPLY+=("$q")
  done < <(compgen "$1" -- "$_dev_cur")
}

# _dev_complete_with TOKEN: COMPREPLY = what TOKEN (from _dev_spec) offers for the word being typed: c:<words>
# (choices), m:<words> (a comma list), f (files), d (folders), l:<kinds>:<arg>:<words>:<f|d> (live values), n.
# compgen splits a word list on IFS: the default, set as a local so a value the user gave IFS cannot change the split.
_dev_complete_with() {
  local t=$1 cur=$_dev_cur kinds arg words files k v q w pre IFS=$' \t\n'  # nosemgrep: bash.lang.security.ifs-tampering.ifs-tampering (local)
  case $t in
    c:*) w=${t#c:}
         _dev_add -W "${w//,/ }" -- "$cur" ;;
    m:*) w=${t#m:}
         pre=
         [[ $cur == *,* ]] && pre=${cur%,*},
         _dev_add -P "$pre" -W "${w//,/ }" -- "${cur##*,}" ;;
    f|d) _dev_files "-$t" ;;
    l:*) IFS=: read -r _ kinds arg words files <<< "$t"
         IFS=$' \t\n'  # nosemgrep: bash.lang.security.ifs-tampering.ifs-tampering (the local, back to the default)
         [[ $arg == @* ]] && arg=${_dev_args[${arg#@} - 1]:--}
         _dev_add -W "${words//,/ }" -- "$cur"
         kinds=$kinds,
         while [[ -n $kinds ]]; do
           k=${kinds%%,*}
           kinds=${kinds#*,}
           [[ -z $k || $k == - ]] && continue
           _dev_values "$k" "$arg"
           for v in "${_dev_reply[@]}"; do
             [[ $v == "$cur"* ]] || continue
             printf -v q '%q' "$v"
             COMPREPLY+=("$q")
           done
         done
         [[ $files == [fd] ]] && _dev_files "-$files" ;;
  esac
  return 0
}

# _dev: the completion function bash calls on Tab. It splits the specs on the default IFS, set as a local so a value
# the user gave IFS cannot change the split.
_dev() {
  local c o v p path= w i n=0 want= tok t rest _dev_cur=${COMP_WORDS[COMP_CWORD]} IFS=$' \t\n'  # nosemgrep: bash.lang.security.ifs-tampering.ifs-tampering (local)
  local -a _dev_args
  COMPREPLY=()
  _dev_spec ""
  for (( i = 1; i < COMP_CWORD; i++ )); do
    w=${COMP_WORDS[i]}
    if [[ -n $want ]]; then
      [[ $w == = ]] && continue
      want=
      continue
    fi
    case $w in
      -*=*) continue ;;
      -*) [[ " $v " == *" $w="* ]] && want=1
          continue ;;
    esac
    if (( n == 0 )) && [[ -n $c && " $c " == *" $w "* ]]; then
      path="${path:+$path }$w"
      _dev_spec "$path"
      continue
    fi
    _dev_args+=("$w")
    n=$((n + 1))
  done
  if [[ -n $want ]]; then
    w=${COMP_WORDS[COMP_CWORD-1]}
    [[ $w == = ]] && w=${COMP_WORDS[COMP_CWORD-2]}
    tok=
    if [[ " $v " == *" $w="* ]]; then
      tok=" $v "
      tok=${tok#*" $w="}
      tok=${tok%% *}
    fi
    _dev_complete_with "${tok:-n}"
    return 0
  fi
  if [[ $_dev_cur == -* ]]; then
    _dev_add -W "$o" -- "$_dev_cur"
    return 0
  fi
  if [[ -n $c ]]; then
    _dev_add -W "$c" -- "$_dev_cur"
    return 0
  fi
  tok=
  rest="$p "
  i=0
  while [[ $rest == *" "* ]]; do
    t=${rest%% *}
    rest=${rest#* }
    if [[ $t == \** ]]; then
      tok=${t#\*}
      break
    fi
    if (( i == n )); then
      tok=$t
      break
    fi
    i=$((i + 1))
  done
  _dev_complete_with "${tok:-n}"
  return 0
}
"""


def bash(root=None) -> str:
    """The bash completion script (shell/completions/dev.bash), for bash 3.2 (macOS) and later."""
    root = root or tree()
    here = " ".join(k for k, (_, h) in KINDS.items() if h)
    out = [BASH_HEAD.rstrip("\n"), "", f"_dev_here={_q(here)}", "",
           "# _dev_spec PATH: the commands (c), options (o), options that take a value with what completes it (v), and "
           "what", "# completes each argument (p) of the dev command PATH.",
           "_dev_spec() {", "  c= o= v= p=", '  case "$1" in']
    for n in walk(root):
        out += _bcase(n)
    out += ["  esac", "}", "", "complete -F _dev dev"]
    return "\n".join(out) + "\n"


SCRIPTS = {"zsh": ("shell/completions/_dev", zsh), "bash": ("shell/completions/dev.bash", bash)}


def drift(home=None, root=None) -> list:
    """-> [(file, what is wrong, the fix)]: a checked-in script that differs from the parser, and a rule that matches
    no argument (selfcheck D13)."""
    home = Path(home or core.DEVKIT_HOME)
    root = root or tree()
    out = []
    for shell, (rel, make) in SCRIPTS.items():
        try:
            have = (home / rel).read_text()
        except OSError:
            have = None
        want = make(root)
        if have != want:
            changed = len(set((have or "").splitlines()) ^ set(want.splitlines()))
            out.append((rel, "missing" if have is None else f"differs from the parser ({changed} lines)",
                        f"bin/dev completion {shell} > {rel}"))
    for path, arg, spec in unused_rules(root):
        out.append((f"rule: dev {' '.join(path)} {arg}", f"matches no argument of the parser ({spec})",
                    "rename it in RULES of lib/devkit/completion.py, or remove it"))
    return out


# --------------------------------------------------------------------------- live values

# What a listing must not draw: the control characters a terminal acts on (C0, DEL and C1: ESC sequences that set the
# window title or colors, CSI) and the bidirectional overrides and isolates that reorder the text around them.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def _one_line(s) -> str:
    """A value or description as one line of plain text: control characters become spaces, runs of spaces one."""
    return " ".join(_CONTROL.sub(" ", str(s or "")).split())


def _registry() -> dict:
    return core.read_json(core.HANDOFF / "registry.json", {}) or {}


def _project(reg: dict, query, cwd: str):
    """The project a query names (a unique match), else the one cwd is in (a worktree of it, else the deepest checkout
    that holds cwd), else None."""
    projects = [p for p in reg.get("projects") or [] if isinstance(p, dict) and p.get("id")]
    if query:
        from . import registry
        m = registry.match({**reg, "projects": projects}, query)
        return m["projects"][0] if len(m.get("projects") or []) == 1 else None
    here, best = core.real(cwd), None
    for p in projects:
        if any(w.get("path") and core.is_under(here, w["path"]) for w in p.get("worktrees") or []):
            return p
        if p.get("path") and core.is_under(here, p["path"]) and (best is None or len(p["path"]) > len(best["path"])):
            best = p
    return best


def _git(cwd, *args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                           env=core.GIT_ENV)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _projects(arg="", **_):
    from . import registry
    ps = _registry().get("projects") or []
    amap = registry.alias_map(ps)
    here, away = registry.split_local(ps)
    out = []
    for p in here + away:
        alias, note = amap.get(p["id"], p["id"]), "" if p in here else " · not on this Mac"
        out.append((p["id"], (alias if alias != p["id"] else "") + note) if arg == "ids" else (alias, p["id"] + note))
    return out


def _initiatives(**_):
    return [(i["id"], i.get("title") or "") for i in _registry().get("initiatives") or [] if i.get("id")]


def _scope(project, initiative, cwd):
    if initiative:
        return f"initiatives/{initiative}"
    p = _project(_registry(), project, cwd)
    return p["id"] if p else ""


def _tasks(project=None, initiative=None, arg="", cwd="", **_):
    from . import work
    scope = _scope(project, initiative, cwd)
    if not scope:
        return []
    items = work.all_items(scope)
    out = []
    for it in work.ordered_open(items):
        m = it["meta"]
        state = "" if m.get("status") == "todo" else f" ({m.get('status')})"
        out.append((m["id"], f"{m.get('bucket', '')}{state} · {it.get('title') or m.get('title', '')}"))
    if arg == "all":
        closed = [i for i in items if i["meta"].get("status") in ("done", "cancelled")]
        for it in sorted(closed, key=lambda i: i["meta"].get("updated", ""), reverse=True)[:30]:
            out.append((it["meta"]["id"], f"{it['meta'].get('status')} · {it.get('title') or ''}"))
    return out


def _steps(project=None, cwd="", **_):
    """The plan steps of the newest live run of the project you are in (a developer's shell has no session of its
    own, so the project's run is the one meant): the ones still to do first, each with its number in the plan."""
    p = _project(_registry(), project, cwd)
    runs = core.STATE / "runs"
    best = None
    for f in runs.glob("*.json") if p and runs.is_dir() else []:
        try:
            age = time.time() - f.stat().st_mtime
        except OSError:
            continue
        r = core.read_json(f, None) if age < 12 * 3600 else None
        if isinstance(r, dict) and r.get("project") == p["id"] and r.get("state") in ("running", "blocked") \
                and (best is None or (r.get("started") or 0) > (best.get("started") or 0)):
            best = r
    open_, closed = [], []
    for i, s in enumerate((best or {}).get("plan") or [], 1):
        if isinstance(s, dict) and s.get("name"):
            who = s.get("label") or ("" if s.get("owner") in (None, "", "main") else s.get("owner"))
            row = (s["name"], f"step {i} · {s.get('state', '')}" + (f" · {who}" if isinstance(who, str) and who
                                                                      else ""))
            (closed if s.get("state") in ("done", "skipped") else open_).append(row)  # progress.DONE
    return open_ + closed


def _crew(**_):
    from . import crew
    out = []
    for m in crew.members(crew.load()):
        role = m.get("title") or m.get("role") or ""
        out.append((m["id"], f"{role} ({m.get('name')})" if role else m.get("name", "")))
    return out


def _activities(**_):
    from . import crew
    return [(a["id"], a.get("name", "")) for a in crew.load().get("activities") or [] if a.get("id")]


def _plans(project=None, initiative=None, cwd="", **_):
    from . import plans
    scope = _scope(project, initiative, cwd)
    if not scope:
        return []
    rows = plans.list_plans(scope)
    rows.sort(key=lambda r: -(r["path"].stat().st_mtime if r["path"].exists() else 0))
    return [(core.tilde(r["path"]), f"{r['title']} · {r['status']}") for r in rows]


def _branches(cwd="", **_):
    out = _git(cwd, "for-each-ref", "--sort=-committerdate", "--count=300", "--format=%(refname:short)\t%(subject)",
               "refs/heads", "refs/remotes/origin")
    rows = []
    for ln in out.splitlines():
        name, _, subject = ln.partition("\t")
        if name and name not in ("origin", "origin/HEAD"):
            rows.append((name, subject))
    return rows


def worktrees_of(main) -> list:
    """-> [(branch, ~/path)] of the worktrees of the checkout at `main`, from git itself (the registry can lag behind
    a `dev wt new`); the main checkout is left out."""
    rows, cur = [], {}
    for ln in (_git(main, "worktree", "list", "--porcelain") + "\n").splitlines():
        if ln.startswith("worktree "):
            cur = {"path": ln[9:]}
        elif ln.startswith("branch "):
            cur["branch"] = ln[7:].replace("refs/heads/", "", 1)
        elif not ln.strip() and cur:
            rows.append(cur)
            cur = {}
    top = core.real(main)
    return [(w.get("branch") or Path(w["path"]).name, core.tilde(w["path"])) for w in rows
            if core.real(w["path"]) != top]


def _worktrees(project=None, arg="", cwd="", **_):
    p = _project(_registry(), arg or project, cwd)
    return worktrees_of(p["path"]) if p and p.get("path") else []


def _versions(**_):
    out = []
    root = core.pin_root()
    for d in sorted(root.iterdir(), reverse=True) if root.is_dir() else []:
        man = core.read_json(d / core.PIN_MANIFEST, None) if d.is_dir() and not d.name.startswith(".") else None
        if isinstance(man, dict) and man.get("commit"):
            out.append((d.name, f"{man.get('version', '')} · {str(man.get('commit'))[:7]}"))
    return out


def _places(**_):
    """Every checkout on this Mac, main ones and worktrees: path -> alias and branch (for the dcd picker)."""
    from . import registry
    ps = _registry().get("projects") or []
    amap = registry.alias_map(ps)
    out = []
    for p in registry.split_local(ps)[0]:
        alias = amap.get(p["id"], p["id"])
        if p.get("path") and os.path.isdir(p["path"]):
            out.append((p["path"], alias))
        for w in p.get("worktrees") or []:
            if w.get("path") and os.path.isdir(w["path"]):
                out.append((w["path"], f"{alias} · {w.get('branch') or Path(w['path']).name}"))
    return out


_LIVE = {"projects": _projects, "initiatives": _initiatives, "tasks": _tasks, "steps": _steps, "crew": _crew,
         "activities": _activities, "plans": _plans, "branches": _branches, "worktrees": _worktrees,
         "versions": _versions, "places": _places}


def values(kind: str, project=None, initiative=None, arg="", cwd=None) -> list:
    """-> [(value, description)] of a live kind. Completion never fails loudly: unreadable state offers nothing."""
    kind = OLD_KINDS.get(kind, kind)
    try:
        rows = _LIVE[kind](project=project, initiative=initiative, arg=arg or "", cwd=cwd or os.getcwd())
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []
    seen, out = set(), []
    for v, d in rows:
        v = _one_line(v)
        if v and v not in seen:
            seen.add(v)
            out.append((v, _one_line(d)[:100]))
    return out


def render(rows, tab: bool = False) -> str:
    """value:description lines for zsh's _describe (a colon in a value escaped), or value<TAB>description lines."""
    if tab:
        return "".join(f"{v}\t{d}\n" for v, d in rows)
    return "".join(v.replace(":", "\\:") + (f":{d}" if d else "") + "\n" for v, d in rows)


def add_complete_arguments(p):
    p.add_argument("kind", help="what to list: " + ", ".join(KINDS))
    p.add_argument("arg", nargs="?", default="", help="narrows it: tasks all, projects ids, worktrees <project>")
    p.add_argument("-p", "--project", help="the project (default: the one you are in)")
    p.add_argument("-i", "--initiative", help="an initiative instead of a project")
    p.add_argument("--tab", action="store_true", help="value<TAB>description lines (for fzf)")
    return p


def main(argv) -> int:
    """`dev __complete …` without building dev's parser (bin/dev calls it first). None: a kind it does not know,
    which the full command line handles."""
    import argparse
    if not argv or OLD_KINDS.get(argv[0], argv[0]) not in KINDS:
        return None
    a = add_complete_arguments(argparse.ArgumentParser(prog="dev __complete", allow_abbrev=False)).parse_args(argv)
    sys.stdout.write(render(values(a.kind, a.project, a.initiative, a.arg), tab=a.tab))
    return 0


# --------------------------------------------------------------------------- the zsh helpers (dev setup shell)

def helpers_file() -> Path:
    """The developer's answer to `dev setup shell`: "on" or "off". shell/devkit.zsh reads the same file."""
    return core.STATE / "shell" / "helpers"


def helpers_mode() -> str:
    """"on", "off", or "" before the developer answered."""
    try:
        m = helpers_file().read_text().strip()
    except OSError:
        return ""
    return m if m in ("on", "off") else ""


def set_helpers_mode(mode: str):
    if mode not in ("on", "off"):
        raise ValueError(f"unknown helpers mode {mode!r}: on or off")
    path = helpers_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".helpers.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(mode + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
