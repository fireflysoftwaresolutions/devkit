# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's drift checks: the places where one part of devkit describes another, compared so they cannot drift apart
as devkit grows (D1-D11 of the crew gap review 2).

  D1  every `enforced` row of the enforcement matrix names a function that imports and that a test names
  D2  every `planned` row names an open work item or plan step (machine state: the live run and the handoff items)
  D3  every public function of the crew's modules has a caller outside its own definition and the tests
  D4  every command and option of `dev` is in docs/commands.md (and every command in the using-devkit table), and
      every `dev …` command the instructions, skills, docs and the roster name resolves in the parser
  D5  the hook events the agents register match the hooks table of docs/agents.md and the counts of docs/surfaces.md
  D6  every configuration key the code reads is in docs/configuration.md
  D7  RACI hygiene per kind of work: A not in C or I, nobody in both C and I, A in R only with a recorded reason
      (`self_approval`), knowledge docs that exist and at least one knowledge skill; the same RACI rules on the
      team's changes (machine state: what they add to devkit's roster)
  D8  every skill the crew names is installed for the agent (machine state, for `dev doctor`)
  D9  every procedure phase's proof and every command its gate names are in that phase's `devkit` list
  D10 every kind of work can be reached from files (its own `files` globs or `dispatch.FILE_HINTS`) or says it has
      none (`no_files`); every open lesson has anchors (machine state)
  D11 every row of the guardrail table in docs/crew.md cites a matrix id (`E-…`), and never a `planned` row as fact
  D12 the test suite is hermetic: tests/__init__.py activates tests/_hermetic.py, and every documented command that
      discovers tests/ runs it as a package (`-t .`), the only way unittest imports tests/__init__.py
  D13 the shell completion checked in (shell/completions/_dev and dev.bash) is what the parser generates now, and
      every completion rule (lib/devkit/completion.py RULES) names an argument the parser has
  D14 every skill devkit names (the crew, the skill rules, the using-devkit table, AGENTS.md section 9) comes from
      a source devkit installs, and none that the crew, a rule or the table asks a model to load is one only the
      developer can run (`disable-model-invocation`)

Each check returns findings: (check, item, evidence, fix). `item` is stable across runs (no line numbers), so a known
finding can be listed by it. The checks read files and import devkit's modules only: no network and no subprocess.
`run()` returns every finding; `run(machine=False)` only those that depend on the repository alone (CI).

The work state of a project (`work_state`, for `dev doctor` and the dashboard's Health view) is machine state, not a
drift between devkit's parts: the work items `dev task reconcile` would fix, and the open runs that stopped moving
or began before the latest release, each with the command that fixes it. It reads git and the run files, and never
writes.
"""
from __future__ import annotations

import ast
import fnmatch
import os
import re
import time
from collections import namedtuple
from pathlib import Path

from . import core

Finding = namedtuple("Finding", "check item evidence fix")

D3_MODULES = ("crew", "lessons", "guard", "raci", "gates", "ai", "progress", "hooks")
MATRIX_ID = re.compile(r"`(E-[a-z0-9]+(?:-[a-z0-9]+)*)`")
TASK_ID = re.compile(r"\b[TFBS]-[0-9a-z]{6,}\b")
CLOSED_STEPS = ("done", "skipped")
CLOSED_ITEMS = ("done", "cancelled")
NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                      "fourteen fifteen sixteen".split())}
CHECK_DOCS = ("agents/AGENTS.md", "docs", "plugin/skills", "config/crew.json")


def _home(home=None) -> Path:
    return Path(home or core.DEVKIT_HOME)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _rel(path: Path, home: Path) -> str:
    try:
        return str(Path(path).relative_to(home))
    except ValueError:
        return str(path)


def _roster(roster=None, team: bool = False) -> dict:
    """The repository's roster (CI and the tests), or with the team's changes from the handoff repository."""
    if roster is not None:
        return roster
    from . import crew
    return crew.load(team=team)


# --------------------------------------------------------------------------- the enforcement matrix

def matrix_rows(rows=None) -> list:
    """The matrix as dicts. A row is (rule, where, ref, state), or (id, rule, where, ref, state) once rows carry a
    stable id (`E-…`)."""
    if rows is None:
        from . import crew
        rows = crew.ENFORCEMENT
    out = []
    for r in rows:
        r = tuple(r)
        rid, rest = (r[0], r[1:]) if len(r) == 5 else ("", r)
        rule, where, ref, state = rest
        out.append({"id": rid, "rule": rule, "where": where, "ref": ref, "state": state})
    return out


def _row_name(row: dict) -> str:
    return row["id"] or row["rule"]


def _tests_text(home: Path) -> str:
    return "\n".join(_read(f) for f in sorted((home / "tests").glob("*.py")) if f.name != "test_consistency.py")


def d1_enforced_rows(rows=None, home=None) -> list:
    """D1: an `enforced` row whose function does not import, or that no test in tests/ names."""
    import importlib
    home = _home(home)
    tests = _tests_text(home)
    out = []
    for row in matrix_rows(rows):
        if row["state"] != "enforced":
            continue
        mod, _, fn = row["ref"].rpartition(".")
        try:
            ok = bool(mod) and callable(getattr(importlib.import_module(mod), fn, None))  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import (the name comes from crew.ENFORCEMENT, a constant in devkit's source)
        except ImportError:
            ok = False
        if not ok:
            out.append(Finding("D1", _row_name(row), f"`{row['ref']}` does not import",
                               "fix the reference in crew.ENFORCEMENT, or mark the row planned with its step"))
        elif not re.search(r"(?<![A-Za-z0-9_])" + re.escape(fn) + r"(?![A-Za-z0-9_])", tests):
            out.append(Finding("D1", _row_name(row), f"no test in tests/ names `{fn}`",
                               f"add a test that calls `{fn}` and fails when the rule is broken"))
    return out


def live_plan(project: str = "fireflysoftwaresolutions/devkit") -> tuple:
    """-> ({step name: state}, {work item id: status}) from the project's active runs and its handoff items."""
    from . import progress, work
    steps = {}
    for run in reversed(progress.active_runs()):  # oldest first: the newest run's state wins
        if run.get("project") == project:
            for s in run.get("plan") or []:
                if s.get("name"):
                    steps[s["name"]] = s.get("state") or "pending"
    items = {}
    for it in work.all_items(project):
        meta = it.get("meta") or {}
        if meta.get("id"):
            items[meta["id"]] = meta.get("status") or ""
    return steps, items


def d2_planned_rows(steps=None, items=None, rows=None) -> list:
    """D2 (machine state): a `planned` row that names no work item or plan step, or one that is already closed."""
    if steps is None or items is None:
        live_steps, live_items = live_plan()
        steps = live_steps if steps is None else steps
        items = live_items if items is None else items
    by_name = {str(k).strip().lower(): (k, v) for k, v in steps.items()}
    out = []
    for row in matrix_rows(rows):
        if row["state"] != "planned":
            continue
        ref, name = row["ref"], _row_name(row)
        ids = TASK_ID.findall(ref)
        if ids:
            for tid in ids:
                status = items.get(tid)
                if status is None:
                    out.append(Finding("D2", name, f"names {tid}, which is not a work item",
                                       "name the open task that adds the rule"))
                elif status in CLOSED_ITEMS:
                    out.append(Finding("D2", name, f"names {tid}, which is {status}",
                                       "mark the row enforced with its function, or name the task that is left"))
            continue
        hit = by_name.get(ref.strip().lower())
        if hit is None:
            out.append(Finding("D2", name, f"`{ref}` is no plan step and names no task",
                               "name the task (T-…) or the plan step that adds the rule"))
        elif hit[1] in CLOSED_STEPS:
            out.append(Finding("D2", name, f"plan step `{hit[0]}` is {hit[1]}",
                               "mark the row enforced with its function, or name the step that is left"))
    return out


# --------------------------------------------------------------------------- unwired code

def _py_files(home: Path) -> list:
    files = sorted((home / "lib" / "devkit").rglob("*.py"))
    files += [f for f in sorted((home / "bin").glob("*")) if f.is_file() and _read(f).startswith("#!") and
              "python" in _read(f).splitlines()[0]]
    return files


def _refs(tree):
    """{name: count} of the names and attributes a tree uses (imports included)."""
    from collections import Counter
    out = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out[node.id] += 1
        elif isinstance(node, ast.Attribute):
            out[node.attr] += 1
        elif isinstance(node, ast.alias):
            out[node.asname or node.name.split(".")[-1]] += 1
    return out


def d3_unwired(home=None, modules=D3_MODULES) -> list:
    """D3: a public function of the crew's modules with no reference outside its own definition and the tests."""
    home = _home(home)
    trees = {}
    for f in _py_files(home):
        try:
            trees[f] = ast.parse(_read(f))
        except SyntaxError:
            continue
    refs = {f: _refs(t) for f, t in trees.items()}
    total = {}
    for c in refs.values():
        for k, n in c.items():
            total[k] = total.get(k, 0) + n
    out = []
    for mod in modules:
        path = home / "lib" / "devkit" / f"{mod}.py"
        tree = trees.get(path)
        if tree is None:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
                continue
            inside = _refs(node).get(node.name, 0)  # its own recursion does not count
            if total.get(node.name, 0) - inside <= 0:
                out.append(Finding("D3", f"{mod}.{node.name}", f"lib/devkit/{mod}.py:{node.lineno} has no caller "
                                   "outside its definition and the tests",
                                   "call it where it belongs, or remove it"))
    return out


# --------------------------------------------------------------------------- commands

def _parser():
    from . import cli
    return cli.build_parser()


def _subactions(p):
    import argparse
    return [a for a in p._actions if isinstance(a, argparse._SubParsersAction)]


def _hidden_names(sub) -> set:
    import argparse
    return {a.dest for a in sub._choices_actions if a.help == argparse.SUPPRESS}


def command_tree(parser=None) -> list:
    """-> [(path words, parser)] for every command `dev` shows, top-level first. Hidden commands (help SUPPRESS) and
    aliases are left out."""
    parser = parser or _parser()
    out, seen = [], set()

    def walk(p, path):
        for sub in _subactions(p):
            hidden = _hidden_names(sub)
            for name, sp in sub.choices.items():
                if name in hidden or id(sp) in seen:
                    continue
                seen.add(id(sp))
                out.append((path + (name,), sp))
                walk(sp, path + (name,))
    walk(parser, ())
    return out


def _options(p, parent=None) -> list:
    """-> [option strings of one option] a command shows, without help and without the options it repeats from
    its parent command (`-p`/`--project` on every `dev task` verb)."""
    import argparse
    inherited = {tuple(a.option_strings) for a in (parent._actions if parent is not None else [])}
    return [a.option_strings for a in p._actions if a.option_strings and a.help != argparse.SUPPRESS and
            not isinstance(a, argparse._HelpAction) and tuple(a.option_strings) not in inherited]


def _names_option(section: str, strings) -> bool:
    return any(re.search(r"(?<![\w-])" + re.escape(o) + r"(?![\w-])", section) for o in strings)


def _sections(text: str) -> dict:
    """{top-level command: its text} from the `### `dev <cmd> …`` sections of docs/commands.md."""
    out, cur = {}, []
    for line in text.splitlines():
        if re.match(r"^#{2,3} ", line):
            cur = re.findall(r"`dev ([a-z][a-z0-9-]*)", line) if line.startswith("### ") else []
        for c in cur:
            out[c] = out.get(c, "") + line + "\n"
    return out


def _documents_verb(section: str, path: tuple) -> bool:
    """The section names the subcommand in code: `dev task add …`, a verb table's `add …`, or an alternative of
    `start|release`."""
    alts = r"(?:[a-z0-9-]+\\?\|)*" + re.escape(path[-1]) + r"(?![a-z0-9-])"
    return bool(re.search(r"`(?:dev " + re.escape(" ".join(path[:-1])) + r" )?" + alts, section))


def d4_commands(home=None, parser=None) -> list:
    """D4: a command or option missing from docs/commands.md, a top-level command missing from the using-devkit
    table, or a `dev …` command in the instructions, skills, docs or roster that does not resolve in the parser."""
    home = _home(home)
    parser = parser or _parser()
    text = _read(home / "docs" / "commands.md")
    sections = _sections(text)
    table = "\n".join(l for l in _read(home / "plugin" / "skills" / "using-devkit" / "SKILL.md").splitlines()
                      if l.startswith("|"))
    out = []
    parents = {(): parser}
    for path, p in command_tree(parser):
        parents[path] = p
        if path == ("help",):
            continue  # `dev help` is `dev --help`, described at the top of the page
        cmd = "dev " + " ".join(path)
        section = sections.get(path[0], "")
        if len(path) == 1:
            if not section:
                out.append(Finding("D4", f"commands.md: {cmd}", f"no `### `{cmd} …`` section",
                                   f"document `{cmd}` in docs/commands.md"))
            if not re.search(r"`dev " + re.escape(path[0]) + r"(?![a-z0-9-])", table):
                out.append(Finding("D4", f"using-devkit: {cmd}", "not in the using-devkit table",
                                   f"name `{cmd}` in the row of its job in plugin/skills/using-devkit/SKILL.md"))
        elif not _documents_verb(section, path):
            out.append(Finding("D4", f"commands.md: {cmd}", f"the `dev {path[0]}` section does not name `{path[-1]}`",
                               f"document `{cmd}` in docs/commands.md"))
        for strings in _options(p, parents.get(path[:-1])):
            if not section or _names_option(section, strings):
                continue  # no section: the missing section is the finding
            opt = max(strings, key=len)
            out.append(Finding("D4", f"commands.md: {cmd} {opt}", f"the `dev {path[0]}` section does not name `{opt}`",
                               f"document `{opt}` of `{cmd}` in docs/commands.md"))
    out += d4_unresolved(home, parser)
    return out


_SPAN = re.compile(r"`(dev [^`\n]+)`")
_FENCE = re.compile(r"^```.*?^```", re.S | re.M)
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")


def _tokens(cmd: str) -> list:
    cmd = cmd.replace("\\|", "|")  # a `|` escaped inside a Markdown table
    cmd = re.sub(r"\"[^\"]*\"|'[^']*'|“[^”]*”|<[^>]*>|\[[^\]]*\]|\{[^}]*\}|\([^)]*\)", " ARG ", cmd)
    return cmd.split()


def resolve(cmd: str, parser=None):
    """-> None when `dev …` resolves in the parser, else why not. Words name commands while the parser has them;
    `a|b` alternatives are each checked; an option must belong to the command it follows (to one of the
    alternatives just named: `dev pr create|checks --watch|merge`)."""
    parser = parser or _parser()
    toks = _tokens(cmd)
    if not toks or toks[0] != "dev":
        return "not a dev command"
    p, path, alts = parser, ["dev"], []
    toks = toks[1:]
    while toks:
        t = toks.pop(0)
        if re.match(r"^--?[a-zA-Z]", t):
            for alt in t.split("|"):
                opt = alt.split("=")[0].rstrip(".,;:")
                act = next((a for q in [p] + alts for a in q._actions if opt in a.option_strings), None)
                if act is None and re.match(r"^--?[a-zA-Z][a-zA-Z0-9-]*$", opt):
                    return f"`{' '.join(path)}` has no option `{opt}`"
                if act is not None and act.nargs != 0 and "=" not in alt and toks and t == alt:
                    toks.pop(0)  # the option's value
            continue
        subs = _subactions(p)
        if not subs:
            break
        names = t.split("|")
        if not all(_WORD.match(n) for n in names):
            if t.startswith("-"):
                continue
            break
        choices = subs[0].choices
        known = [n for n in names if n in choices]
        positional = [a for a in p._actions if not a.option_strings and a is not subs[0] and a.dest != "help"]
        if len(known) != len(names):
            if positional:  # a word the command takes as an argument
                break
            bad = next(n for n in names if n not in choices)
            return f"`{' '.join(path)}` has no command `{bad}`"
        p, alts = choices[names[0]], [choices[n] for n in names[1:]]
        path.append(names[0])
    return None


def _dev_spans(text: str) -> list:
    spans = list(_SPAN.findall(text))
    for block in _FENCE.findall(text):
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("dev "):
                spans.append(line.split("  #")[0].split(" # ")[0])
    return spans


def _doc_files(home: Path) -> list:
    out = []
    for rel in CHECK_DOCS:
        p = home / rel
        if p.is_dir():
            out += sorted(p.rglob("*.md"))
        elif p.exists():
            out.append(p)
    return out


def _roster_commands(x, key="") -> list:
    """The commands a roster names: its `devkit` lists, its `proof`s, and `dev …` in backticks in any text (the
    routing `signals` are words, not commands)."""
    if isinstance(x, str):
        return list(_SPAN.findall(x)) + ([x] if key in ("devkit", "proof") and x.startswith("dev ") else [])
    if isinstance(x, dict):
        return [s for k, v in x.items() if k != "signals" for s in _roster_commands(v, k)]
    if isinstance(x, list):
        return [s for v in x for s in _roster_commands(v, key)]
    return []


def d4_unresolved(home=None, parser=None) -> list:
    """D4, second half: `dev …` commands in AGENTS.md, the skills, the docs and config/crew.json that the parser does
    not know."""
    home = _home(home)
    parser = parser or _parser()
    out, seen = [], set()
    for f in _doc_files(home):
        rel = _rel(f, home)
        if f.suffix == ".json":
            spans = _roster_commands(core.read_json(f, {}) or {})
        else:
            spans = _dev_spans(_read(f))
        for span in spans:
            span = " ".join(span.split())
            if (rel, span) in seen:
                continue
            seen.add((rel, span))
            why = resolve(span, parser)
            if why:
                out.append(Finding("D4", f"{rel}: {span}", why, "fix the command, or add it to the parser"))
    return out


# --------------------------------------------------------------------------- hooks

def _table(text: str, heading: str) -> list:
    """The rows (lists of cells) of the first table after `heading`."""
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return []
    rows, started = [], False
    for l in lines[start + 1:]:
        if l.startswith("|"):
            started = True
            cells = [c.strip() for c in re.split(r"(?<!\\)\|", l.strip()[1:-1])]
            if not all(set(c) <= set("-: ") for c in cells):
                rows.append(cells)
        elif started or l.startswith("#"):
            break
    return rows[1:]  # without the header


def registered_events(home=None) -> dict:
    home = _home(home)
    claude = (core.read_json(home / "config" / "claude" / "settings.json", {}) or {}).get("hooks") or {}
    codex = core.read_json(home / "config" / "codex" / "hooks.json", {}) or {}
    return {"claude": set(claude), "codex": set(codex.get("hooks") or codex)}


def d5_hook_events(home=None) -> list:
    """D5: the events in config/claude/settings.json and config/codex/hooks.json against the hooks table of
    docs/agents.md and the counts in docs/surfaces.md."""
    home = _home(home)
    reg = registered_events(home)
    documented = {"claude": set(), "codex": set()}
    for row in _table(_read(home / "docs" / "agents.md"), "## Hooks"):
        if len(row) < 3:
            continue
        for ev in (e.strip() for e in row[0].split("·")):
            for agent, cell in (("claude", row[1]), ("codex", row[2])):
                if cell not in ("—", "-", ""):
                    documented[agent].add(ev)
    out = []
    for agent, name in (("claude", "Claude Code"), ("codex", "Codex")):
        for ev in sorted(reg[agent] - documented[agent]):
            out.append(Finding("D5", f"agents.md: {ev} ({agent})", f"{name} registers {ev}; the hooks table has no "
                               f"{name} entry for it", "add the event to the hooks table of docs/agents.md"))
        for ev in sorted(documented[agent] - reg[agent]):
            out.append(Finding("D5", f"agents.md: {ev} ({agent})", f"the hooks table lists {ev} for {name}, which "
                               "does not register it", "fix the hooks table of docs/agents.md"))
    surfaces = _read(home / "docs" / "surfaces.md")
    hooks_row = next((r for r in _table(surfaces, "## What each surface can do") if r and r[0] == "Hooks"), None)
    if hooks_row:
        for col, agent in ((1, "claude"), (2, "claude"), (3, "codex"), (4, "codex")):
            if col >= len(hooks_row):
                continue
            m = re.search(r"\b(" + "|".join(NUMBERS) + r"|\d+)\s+events\b", hooks_row[col], re.I)
            if m:
                n = NUMBERS.get(m.group(1).lower(), None)
                n = int(m.group(1)) if n is None else n
                if n != len(reg[agent]):
                    out.append(Finding("D5", f"surfaces.md: Hooks column {col} ({agent})",
                                       f"says {m.group(1)} events; {len(reg[agent])} are registered",
                                       "fix the Hooks row of docs/surfaces.md"))
    m = re.search(r"The (\w+) hook events of Claude Code are ([^.]+)\.", " ".join(surfaces.split()))
    if m:
        listed = {e.strip() for e in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(2)) if e.strip()}
        n = NUMBERS.get(m.group(1).lower(), -1)
        if n != len(reg["claude"]) or listed != reg["claude"]:
            missing = sorted(reg["claude"] - listed)
            out.append(Finding("D5", "surfaces.md: the Claude Code event list",
                               f"says {m.group(1)} ({len(listed)} named); {len(reg['claude'])} are registered"
                               + (f", missing {', '.join(missing)}" if missing else ""),
                               "fix the event list in docs/surfaces.md"))
    return out


# --------------------------------------------------------------------------- configuration keys

def _cfg_path(node, in_progress: bool):
    """The configuration key a node reads: () for core.CFG itself, ("progress",) for progress.cfg(), else None."""
    if isinstance(node, ast.Name) and node.id == "CFG":
        return ()
    if isinstance(node, ast.Attribute) and node.attr == "CFG":
        return ()
    if isinstance(node, ast.Call) and not node.args:
        f = node.func
        if (in_progress and isinstance(f, ast.Name) and f.id == "cfg") or \
                (isinstance(f, ast.Attribute) and f.attr == "cfg" and isinstance(f.value, ast.Name) and
                 f.value.id == "progress"):
            return ("progress",)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _cfg_path(node.values[0], in_progress)
    key = None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and \
            node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        base, key = node.func.value, node.args[0].value
    elif isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
            base, key = node.value, sl.value
    if key is None:
        return None
    parent = _cfg_path(base, in_progress)
    return None if parent is None else parent + (key,)


def config_reads(home=None) -> dict:
    """{key path: first place it is read} for core.CFG and progress.cfg(), including progress.cfg()'s defaults."""
    home = _home(home)
    out = {}
    for f in sorted((home / "lib" / "devkit").rglob("*.py")):
        try:
            tree = ast.parse(_read(f))
        except SyntaxError:
            continue
        in_progress = f.name == "progress.py"
        for node in ast.walk(tree):
            if in_progress and isinstance(node, ast.FunctionDef) and node.name == "cfg":
                for d in ast.walk(node):
                    if isinstance(d, ast.Dict):
                        for k in d.keys:
                            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                out.setdefault("progress." + k.value, f"{_rel(f, home)}:{node.lineno}")
            path = _cfg_path(node, in_progress) if isinstance(node, (ast.Call, ast.Subscript)) else None
            if path:
                out.setdefault(".".join(path), f"{_rel(f, home)}:{node.lineno}")
    return out


def documented_keys(home=None) -> set:
    home = _home(home)
    keys = set()
    for line in _read(home / "docs" / "configuration.md").splitlines():
        if line.startswith("| `"):
            first = re.split(r"(?<!\\)\|", line.strip()[1:])[0]
            keys |= set(re.findall(r"`([a-z_][a-zA-Z0-9_.<>\"-]*)`", first))
    return keys


def _key_documented(key: str, docs: set) -> bool:
    for d in docs:
        pat = re.escape(d).replace(re.escape("<name>"), r"[^.]+")
        pat = re.sub(r"\\<[^>]*\\>", r"[^.]+", pat)
        if re.fullmatch(pat, key) or d.startswith(key + ".") or key.startswith(d + ".") or \
                re.match(pat + r"\.", key):
            return True
    return False


def d6_config_keys(home=None) -> list:
    """D6: a configuration key the code reads that docs/configuration.md does not list."""
    home = _home(home)
    docs = documented_keys(home)
    reads = config_reads(home)
    out = []
    for key in sorted(reads):
        if any(k.startswith(key + ".") for k in reads):
            continue  # a parent of a key read deeper: documenting the deeper key documents it
        if not _key_documented(key, docs):
            out.append(Finding("D6", key, f"read at {reads[key]}; not in docs/configuration.md",
                               f"add `{key}` with its default and effect to docs/configuration.md"))
    return out


# --------------------------------------------------------------------------- the roster and the RACI

def raci_errors(roster=None) -> list:
    """The RACI rules of crew review 2 (R2-9), per kind of work: the Accountable member is not also Consulted or
    Informed, nobody is both Consulted and Informed, and an Accountable member that is also Responsible approves its
    own work only with a recorded reason (`self_approval`). D7 applies them to devkit's roster, `d7_team_raci` to the
    team's changes; `raci.check` can turn them into errors."""
    out = []
    for a in _roster(roster).get("activities") or []:
        if not isinstance(a, dict):
            continue
        aid, acc = a.get("id") or "?", a.get("A")
        c, i, r = set(a.get("C") or []), set(a.get("I") or []), set(a.get("R") or [])
        if acc in c:
            out.append(Finding("D7", f"{aid}: A {acc} in C", f"{acc} approves `{aid}` and is consulted on it",
                               "drop the Accountable member from C"))
        if acc in i:
            out.append(Finding("D7", f"{aid}: A {acc} in I", f"{acc} approves `{aid}` and is informed of it",
                               "drop the Accountable member from I"))
        for m in sorted(c & i):
            out.append(Finding("D7", f"{aid}: {m} in C and I", f"{m} is both consulted on and informed of `{aid}`",
                               "keep the member in one of C and I"))
        if acc in r and not str(a.get("self_approval") or "").strip():
            out.append(Finding("D7", f"{aid}: A {acc} in R", f"{acc} approves its own `{aid}` work, with no reason",
                               "name another approver, or record why with `self_approval`"))
    return out


def d7_team_raci(base=None, team=None) -> list:
    """D7 on the team's changes (machine state: crew/crew.json in the handoff repository): the RACI errors the
    merged roster has and devkit's own roster does not, as `team: …` findings with the command that changes it."""
    known = {f.item for f in raci_errors(_roster(base))}
    team = team if team is not None else _roster(None, team=True)
    return [Finding("D7", f"team: {f.item}", f"the team's crew changes: {f.evidence}",
                    f"{f.fix}: dev crew raci {f.item.split(':', 1)[0]} --set \"…\" --why \"…\"")
            for f in raci_errors(team) if f.item not in known]


def d7_raci_hygiene(roster=None, home=None) -> list:
    """D7: per kind of work, the RACI rules (`raci_errors`), a knowledge doc that does not exist, or no knowledge
    skill."""
    home = _home(home)
    out = raci_errors(roster)
    for a in _roster(roster).get("activities") or []:
        if not isinstance(a, dict):
            continue
        aid = a.get("id") or "?"
        know = a.get("knowledge") or {}
        for doc in know.get("docs") or []:
            if not (home / doc).exists():
                out.append(Finding("D7", f"{aid}: doc {doc}", f"`{doc}` does not exist", "fix the path, or write it"))
        if not know.get("skills"):
            out.append(Finding("D7", f"{aid}: no skill", f"`{aid}` names no knowledge skill",
                               "add the skill that holds this kind of work's know-how"))
    return out


def crew_skills(roster=None) -> dict:
    """{skill: [where the crew names it]} from members, their procedure phases and the kinds of work."""
    out = {}
    roster = _roster(roster)
    for m in roster.get("members") or []:
        for s in m.get("skills") or []:
            out.setdefault(s, []).append(m["id"])
        for ph in m.get("procedure") or []:
            for s in ph.get("skills") or []:
                out.setdefault(s, []).append(f"{m['id']} {ph.get('phase')}")
    for a in roster.get("activities") or []:
        for s in (a.get("knowledge") or {}).get("skills") or []:
            out.setdefault(s, []).append(f"activity {a.get('id')}")
    return {k: sorted(set(v)) for k, v in out.items()}


AGENT_HOMES = {"claude": (core.CLAUDE_HOME, core.CLAUDE_HOME / "skills"),
               "codex": (core.CODEX_HOME, core.AGENTS_HOME / "skills")}


def skill_installed(name: str, agent: str = "claude", agent_home=None, skills_dir=None) -> bool:
    """A bare skill is a folder in the agent's skills directory or in an installed plugin; `plugin:skill` is a skill
    of that installed plugin (`<agent home>/plugins/cache/<marketplace>/<plugin>/<version>/skills/<skill>`)."""
    home, sdir = AGENT_HOMES.get(agent, AGENT_HOMES["claude"])
    home, sdir = Path(agent_home or home), Path(skills_dir or sdir)
    cache = home / "plugins" / "cache"
    if ":" in name:
        plugin, skill = name.split(":", 1)
        return any(cache.glob(f"*/{plugin}/*/skills/{skill}/SKILL.md"))
    return (sdir / name / "SKILL.md").exists() or any(cache.glob(f"*/*/*/skills/{name}/SKILL.md"))


def skill_group(name: str, home=None) -> str:
    """The config/skills.json group that provides a skill: `core`, a group name, `plugin <name>`, or `unknown`."""
    if ":" in name:
        return "plugin " + name.split(":", 1)[0]
    home = _home(home)
    if (home / "plugin" / "skills" / name / "SKILL.md").exists():
        return "plugin devkit"
    for src in (core.read_json(home / "config" / "skills.json", {}) or {}).get("sources") or []:
        if name in (src.get("skills") or {}):
            return src.get("group") or "core"
    return "unknown"


def d8_uninstalled_skills(agent: str = "claude", roster=None, home=None, agent_home=None, skills_dir=None) -> list:
    """D8 (machine state, for `dev doctor`): a skill the crew names that is not installed for `agent`, with the
    config/skills.json group that provides it and the command that installs it."""
    out = []
    for name, where in sorted(crew_skills(_roster(roster, team=True)).items()):  # the crew this Mac runs
        if skill_installed(name, agent, agent_home, skills_dir):
            continue
        group = skill_group(name, home)
        if group.startswith("plugin "):
            fix = f"dev install --only {agent}"
        elif group == "core":
            fix = "dev install --only skills"
        elif group == "unknown":
            fix = "add it to config/skills.json, or drop it from the crew"
        else:
            fix = f'add "{group}" to skills.groups in ~/.config/devkit/config.json, then dev install --only skills'
        out.append(Finding("D8", f"{agent}: {name}", f"named by the crew but not installed: {name} (group {group}); "
                           f"named by {', '.join(where[:4])}", fix))
    return out


SKILL_TOKEN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?::[a-z0-9]+(?:-[a-z0-9]+)*)?$")
NOT_SKILLS = {"dev"}  # backticked words in the skill lists that name a program, not a skill


def _plugin_names(home: Path) -> set:
    """The plugins devkit installs for either agent: `superpowers` from `superpowers@claude-plugins-official`."""
    out = set()
    for agent in ("claude", "codex"):
        conf = core.read_json(home / "config" / agent / "plugins.json", {}) or {}
        out |= {str(p).split("@")[0] for p in list(conf.get("plugins") or []) + list(conf.get("disabled") or [])}
    return out


def _frontmatter_user_only(path: Path) -> bool:
    head = _read(path)[:2000]
    return bool(re.search(r"^disable-model-invocation:\s*true\s*$", head.split("\n---", 1)[0], re.M | re.I))


def user_only_skills(home=None) -> set:
    """Skills only the developer can run (their SKILL.md sets `disable-model-invocation: true`, so a model cannot load
    them): those config/skills.json lists as `user_only`, and devkit's own skills whose frontmatter says so."""
    home = _home(home)
    out = set()
    for src in (core.read_json(home / "config" / "skills.json", {}) or {}).get("sources") or []:
        out |= set(src.get("user_only") or [])
    for f in sorted((home / "plugin" / "skills").glob("*/SKILL.md")):
        if _frontmatter_user_only(f):
            out.add(f.parent.name)
    return out


def skill_resolves(name: str, home=None) -> bool:
    """A source devkit installs provides the skill: devkit's own plugin, config/skills.json, a plugin of
    config/<agent>/plugins.json (`superpowers:brainstorming`, or `frontend-design` from its own plugin), or the
    packages step (playwright-cli)."""
    home = _home(home)
    if ":" in name:
        plugin, skill = name.split(":", 1)
        if plugin == "devkit":
            return (home / "plugin" / "skills" / skill / "SKILL.md").exists()
        return plugin in _plugin_names(home)
    if skill_group(name, home) != "unknown" or name in _plugin_names(home):
        return True
    from . import ext
    return name in ext._devkits_skill_names()


def _table_skills(text: str, heading: str) -> list:
    """The backticked skill names in the last column of the first table after `heading`."""
    out = []
    for row in _table(text, heading):
        out += [t for t in re.findall(r"`([^`]+)`", row[-1]) if SKILL_TOKEN.match(t) and t not in NOT_SKILLS]
    return out


def skill_names(home=None, roster=None) -> dict:
    """{skill: [where devkit names it]}: the crew (members, phases, kinds of work), the skill rules (every rule, its
    group on or not), the Skills column of the using-devkit table, and section 9 of AGENTS.md ("AGENTS.md §9")."""
    home = _home(home)
    out = {k: list(v) for k, v in crew_skills(_roster(roster)).items()}
    rules = (core.read_json(home / "config" / "skill-rules.json", {}) or {}).get("rules") or []
    commands = {c for r in rules for c in r.get("commands") or [] if " " not in c}  # `pytest` is a command
    for r in rules:
        for agent, names in (r.get("skills") or {}).items():
            for s in names:
                out.setdefault(s, []).append(f"skill rule {r.get('id')} ({agent})")
    using = _read(home / "plugin" / "skills" / "using-devkit" / "SKILL.md")
    for s in _table_skills(using, "## Which tool for which job"):
        out.setdefault(s, []).append("the using-devkit table")
    agents = _read(home / "agents" / "AGENTS.md")
    m = re.search(r"^## 9\..*?(?=^## 10\.)", agents, re.M | re.S)
    for t in re.findall(r"`([^`]+)`", m.group(0) if m else ""):
        if SKILL_TOKEN.match(t) and t not in NOT_SKILLS and t not in commands:
            out.setdefault(t, []).append("AGENTS.md §9")
    return {k: sorted(set(v)) for k, v in out.items()}


def d14_skill_names(home=None, roster=None) -> list:
    """D14: a skill devkit names that no source it installs provides, or one that the crew, a skill rule or the
    using-devkit table asks a model to load although only the developer can run it (`user_only_skills`) or devkit's
    own skill replaces it (config/skills.json `superseded`: devkit wins where a plugin skill differs). AGENTS.md may
    name such a skill as the developer's own slash command."""
    home = _home(home)
    out = []
    user_only = user_only_skills(home)
    superseded = (core.read_json(home / "config" / "skills.json", {}) or {}).get("superseded") or {}
    for name, where in sorted(skill_names(home, roster).items()):
        if not skill_resolves(name, home):
            out.append(Finding("D14", f"unknown: {name}", f"no source devkit installs provides {name}; named by "
                               f"{', '.join(where[:4])}", "fix the name, or add its source to config/skills.json"))
        asked = [w for w in where if w != "AGENTS.md §9"]
        if name.split(":")[-1] in user_only and asked:
            out.append(Finding("D14", f"user-only: {name}", f"{name} is the developer's slash command (a model cannot "
                               f"load it), but {', '.join(asked[:4])} ask a model to load it",
                               "name a skill a model can load there, and suggest the slash command to the developer"))
        if name in superseded and asked:
            by = superseded[name]
            out.append(Finding("D14", f"superseded: {name}", f"{by.get('by')} replaces {name} ({by.get('why')}), but "
                               f"{', '.join(asked[:4])} name it", f"name {by.get('by')} instead"))
    return out


def d9_phase_proofs(roster=None) -> list:
    """D9: a procedure phase whose proof is not in its own `devkit` list, or whose gate names a `dev …` command (in
    backticks) that is not in that list."""
    out = []
    for m in _roster(roster).get("members") or []:
        for ph in m.get("procedure") or []:
            listed = [" ".join(x.split()) for x in ph.get("devkit") or []]
            where = f"{m['id']} {ph.get('phase')}"

            def known(cmd):
                cmd = " ".join(cmd.split())
                return any(cmd == x or cmd.startswith(x + " ") for x in listed)
            proof = ph.get("proof")
            if proof and not known(proof):
                out.append(Finding("D9", f"{where}: proof {proof}", f"the proof `{proof}` is not in the phase's devkit "
                                   "list", "add it to the phase's `devkit` list, or fix the proof"))
            for cmd in re.findall(r"`(dev [^`]+)`", ph.get("gate") or ""):
                if not known(cmd):
                    out.append(Finding("D9", f"{where}: gate {cmd}", f"the gate names `{cmd}`, which is not in the "
                                       "phase's devkit list", "add it to the phase's `devkit` list"))
    return out


def d10_file_anchors(roster=None, hints=None) -> list:
    """D10: a kind of work that neither its own `files` globs nor a `dispatch.FILE_HINTS` glob reaches (directly by
    its id, or through the member whose first Responsible kind of work it is) and that does not say it has no files
    (`no_files`)."""
    from . import raci
    roster = _roster(roster)
    if hints is None:
        from .dispatch import FILE_HINTS as hints
    reached = set()
    for _, target in hints:
        if raci.activity(target, roster):
            reached.add(target)
        else:
            a = raci.member_activity(target, "", roster)
            if a:
                reached.add(a["id"])
    reached |= {a.get("id") for a in raci.activities(roster) if a.get("files")}
    out = []
    for a in raci.activities(roster):
        if a.get("id") not in reached and not str(a.get("no_files") or "").strip():
            out.append(Finding("D10", f"{a.get('id')}: no file anchor",
                               "no `files` glob of its own and no FILE_HINTS glob lead to it",
                               "add its globs as `files` in config/crew.json, or record why it has none with "
                               "`no_files`"))
    return out


def d10_lesson_anchors(lessons_=None) -> list:
    """D10 (machine state): an open lesson with no anchor (files, commands, kinds of work, errors, stack)."""
    from . import lessons
    if lessons_ is None:
        lessons_ = lessons.all_lessons()
    out = []
    for les in lessons_:
        meta = les.get("meta") or {}
        if meta.get("status") == "open" and not any(lessons.anchors(les).values()):
            out.append(Finding("D10", f"lesson {meta.get('id')}", f"“{meta.get('title')}” has no anchors",
                               f"dev lesson anchor {meta.get('id')} --file … --command … --why \"…\""))
    return out


def d11_guardrails(home=None, rows=None) -> list:
    """D11: a row of the guardrail table in docs/crew.md that cites no matrix id, an unknown one, or a `planned` row
    as fact."""
    home = _home(home)
    by_id = {r["id"]: r for r in matrix_rows(rows) if r["id"]}
    out = []
    for row in _table(_read(home / "docs" / "crew.md"), "## Guardrails"):
        if not row:
            continue
        text = " ".join(row)
        ids = MATRIX_ID.findall(text)
        name = row[0]
        if not ids:
            out.append(Finding("D11", name, "cites no matrix id",
                               "cite the row of `dev crew rules` it describes (`E-…`)"))
        for rid in ids:
            r = by_id.get(rid)
            if r is None:
                out.append(Finding("D11", name, f"cites `{rid}`, which is not in the matrix", "fix the id"))
            elif r["state"] == "planned" and "planned" not in text.lower():
                out.append(Finding("D11", name, f"states `{rid}` as fact; the matrix has it planned",
                                   "say it is planned, or wait until the row is enforced"))
    return out


SUITE_RUN = re.compile(r"(?:\S*python3?|\bpython3?)\s+-m\s+unittest\s+discover\b[^`\n|&;]*")
SUITE_DOCS = (".github/workflows/*.yml", ".github/workflows/*.yaml", "*.md", "docs/*.md", "docs/**/*.md")


def d12_hermetic_tests(home=None) -> list:
    """D12: the test suite keeps the developer's real devkit out of reach. tests/__init__.py activates
    tests/_hermetic.py, and every documented command that discovers tests/ runs it as a package (`-t .`): with
    tests/ as the top level, unittest never imports tests/__init__.py and the run uses the real home."""
    home = _home(home)
    out = []
    init = _read(home / "tests" / "__init__.py")
    if "_hermetic.activate()" not in init or not (home / "tests" / "_hermetic.py").exists():
        out.append(Finding("D12", "tests/__init__.py: does not activate tests/_hermetic.py",
                           "the suite runs with the developer's home and devkit's real state",
                           "keep tests/_hermetic.py and call _hermetic.activate() in tests/__init__.py"))
    seen = set()
    for pattern in SUITE_DOCS:
        for path in sorted(home.glob(pattern)):
            rel = _rel(path, home)
            if rel in seen or not path.is_file():
                continue
            seen.add(rel)
            for m in SUITE_RUN.finditer(_read(path)):
                cmd = " ".join(m.group(0).split())
                words = cmd.split()
                if not any(w in ("-s", "--start-directory") and i + 1 < len(words) and
                           words[i + 1].rstrip("/") in ("tests", "./tests") for i, w in enumerate(words)):
                    continue
                if any(w in ("-t", "--top-level-directory") or w.startswith("--top-level-directory=")
                       for w in words):
                    continue
                out.append(Finding("D12", f"{rel}: {cmd}", "runs tests/ as the top level, so tests/__init__.py "
                                   "and the suite's isolation never run",
                                   "add -t . (python3 -m unittest discover -s tests -t .)"))
    return out


def d13_completion(home=None, parser=None) -> list:
    """D13: the shell completion checked in (shell/completions/_dev and dev.bash) is what dev's parser generates now,
    and every completion rule still names an argument of the parser."""
    from . import completion
    root = completion.tree(parser or _parser())
    return [Finding("D13", item, evidence, fix) for item, evidence, fix in completion.drift(_home(home), root)]


# --------------------------------------------------------------------------- work state (dev doctor, the dashboard)

RUN_OPEN = ("running", "blocked")
RUN_FIX = 'dev progress done "<outcome>" in its session, or dev progress step'


def open_runs() -> list:
    """This Mac's progress runs that are still open (running or blocked), however long ago they last moved: the run
    files as stored."""
    from . import progress
    out = []
    for f in sorted(progress.RUNS.glob("*.json")) if progress.RUNS.is_dir() else []:
        r = core.read_json(f, None)
        if isinstance(r, dict) and r.get("state") in RUN_OPEN:
            out.append(r)
    return out


def last_step_change(run: dict) -> float:
    """When the run's plan last moved: its newest step change, a step starting or finishing, or the run starting.
    A decision's line in the history is not a step change."""
    ts = [run.get("started")]
    ts += [x.get("ts") for x in run.get("history") or []
           if isinstance(x, dict) and not str(x.get("step") or "").startswith("(")]
    ts += [s.get(k) for s in run.get("plan") or [] if isinstance(s, dict) for k in ("started", "finished")]
    return max((t for t in ts if isinstance(t, (int, float)) and not isinstance(t, bool)), default=0)


def latest_tag(top) -> tuple:
    """(name, epoch seconds) of the repository's newest tag, ("", 0) when it has none."""
    out = core.gitout(str(top), "for-each-ref", "--sort=-creatordate", "--count=1",
                      "--format=%(refname:short)%09%(creatordate:unix)", "refs/tags")
    name, _, at = out.partition("\t")
    try:
        return name, int(at)
    except ValueError:
        return "", 0


def merges(top) -> dict:
    """The branches merged into the checkout's default branch, as origin has it at the last fetch (never fetched
    here): gitx.merged_into_default."""
    from . import gitx, registry
    return gitx.merged_into_default(str(top), registry.default_branch(Path(top)))


def _listed(items: list, more: str = "", n: int = 3) -> str:
    shown = ", ".join(f"{i['id']} {i['title']}" + (more and more.format(**i)) for i in items[:n])
    return shown + (f", +{len(items) - n} more" if len(items) > n else "")


def _stale_runs(scope: str, runs: list, now: float, hours: float, tag: tuple) -> list:
    name, tag_at = tag
    rows = []
    for r in sorted((r for r in runs if isinstance(r, dict)), key=lambda r: f"{r.get('agent')}-{r.get('session')}"):
        if r.get("project") != scope or r.get("state") not in RUN_OPEN:
            continue
        why = []
        if tag_at and (r.get("started") or 0) < tag_at:
            why.append(f"began before {name}, the latest release")
        quiet = now - last_step_change(r)
        if quiet > hours * 3600:
            why.append(f"no step change for {int(quiet // 3600)} h")
        if why:
            rid = f"{r.get('agent')}-{r.get('session')}"
            rows.append({"kind": "run", "run": rid, "text": f"Run “{r.get('goal') or rid}” ({rid}): " + ", and ".join(why),
                         "fix": RUN_FIX})
    return rows


def work_state(scope: str, top, *, runs=None, now=None, lock_hours=None, merged=None, tag=None) -> dict:
    """What is stale in `scope`'s work, from its checkout `top`: items doing without a live lock, open items of a
    branch merged into the default branch and items that share a title (work.reconcile, without --apply), and the
    project's open runs whose plan has not moved for more than `work.lock_hours` or that began before the latest
    release tag. Reads only. `runs`: run records (default: open_runs()); `merged`, `tag`: the git facts (default:
    merges(top), latest_tag(top)). -> {"project", "rows": [{"kind", "text", "fix"}], "count"}"""
    from . import work
    now = now or time.time()
    hours = float(lock_hours or (core.CFG.get("work") or {}).get("lock_hours", 8))
    rep = work.reconcile(scope, merges(top) if merged is None else merged)
    rows, apply = [], f"dev task -p {scope} reconcile --apply"
    if rep["lockless_doing"]:
        rows.append({"kind": "lockless_doing", "fix": apply,
                     "text": f"{len(rep['lockless_doing'])} doing without a lock: {_listed(rep['lockless_doing'])}"})
    if rep["merged"]:
        more = [dict(i, pr_=f", PR #{i['pr']}" if i.get("pr") else "") for i in rep["merged"]]
        rows.append({"kind": "merged", "fix": apply, "text": f"{len(more)} open, but "
                     f"{'its branch is' if len(more) == 1 else 'their branches are'} merged: "
                     f"{_listed(more, ' ({branch}{pr_})')}"})
    for g in rep["duplicates"]:
        keep = next((i for i in g if i.get("status") == "done"), None) or min(g, key=lambda i: i["id"])
        cancel = [i for i in sorted(g, key=lambda i: i["id"]) if i is not keep and i.get("status") in work.OPEN]
        rows.append({"kind": "duplicates", "text": "Same title: " + ", ".join(f"{i['id']} ({i['status']})" for i in g)
                     + f" “{g[0]['title']}”", "fix": " · ".join(f'dev task -p {scope} cancel {i["id"]} --why "duplicate of '
                                                              f'{keep["id"]}"' for i in cancel)})
    rows += _stale_runs(scope, open_runs() if runs is None else runs, now, hours,
                        latest_tag(top) if tag is None else tag)
    count = len(rep["lockless_doing"]) + len(rep["merged"]) + len(rep["duplicates"]) + \
        sum(1 for x in rows if x["kind"] == "run")
    return {"project": scope, "rows": rows, "count": count}


HANDOFF_SKILL = Path("plugin") / "skills" / "handoff" / "SKILL.md"
HANDOFF_MAX_LINES = 150  # when the handoff skill states no size


def handoff_max_lines(home=None) -> int:
    """The size the handoff skill sets for a project's note ("Keep the whole note under ~150 lines")."""
    m = re.search(r"under ~?(\d+) lines", _read(_home(home) / HANDOFF_SKILL))
    return int(m.group(1)) if m else HANDOFF_MAX_LINES


def handoff_size_rows(scope: str) -> list:
    """`dev doctor`'s warning when the project's handoff note is longer than the handoff skill allows; [] when it is
    within it or there is no note."""
    note = core.note_path(scope)
    try:
        n = len(note.read_text(encoding="utf-8", errors="ignore").splitlines())
    except OSError:
        return []
    limit = handoff_max_lines()
    if n <= limit:
        return []
    return [("Workspace", "handoff note", False, f"{scope}: {core.tilde(note)} has {n} lines, over the ~{limit} the "
             "handoff skill sets", "rewrite Current state as it stands and move history and long material into "
             "plans/ (the handoff skill, /devkit:handoff)")]


def work_state_checks(top=None, scope=None, *, runs=None, now=None) -> list:
    """`dev doctor`'s "work state" row for the project you are in, [] outside one: (group, name, ok, detail, fix);
    and a "handoff note" warning when the project's note is longer than the handoff skill allows."""
    top = top or core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if not top or core.real(top) == core.real(core.HANDOFF):
        return []
    if scope is None:
        from . import registry
        main = registry.main_of(Path(top)) or Path(top)
        scope = core.project_identity(main, core.gitout(str(main), "config", "--get", "remote.origin.url"))[0]
    size = handoff_size_rows(scope)
    try:
        r = work_state(scope, top, runs=runs, now=now)
    except (OSError, ValueError, KeyError) as e:  # an item or run file it cannot read: say so, never stop the doctor
        return [("Workspace", "work state", False, f"{scope}: the work state could not be read ({e})",
                 f"dev task -p {scope} reconcile")] + size
    if not r["rows"]:
        return [("Workspace", "work state", True, f"{scope}: no stale work items or runs", "")] + size
    return [("Workspace", "work state", False, f"{scope}: " + "; ".join(x["text"] for x in r["rows"]),
             " · ".join(dict.fromkeys(x["fix"] for x in r["rows"] if x["fix"])))] + size


# --------------------------------------------------------------------------- devkit copies older than the pin

_GENERATED = re.compile(r"<!-- generated by devkit (\S+) from")


def _copy_version(path: Path):
    """A pinned copy's release numbers, from its manifest or else its folder name (26.11.4-89079aa) -> tuple|None."""
    man = core.read_json(path / core.PIN_MANIFEST, {}) or {}
    return core.version_key(man.get("version") if isinstance(man, dict) and man.get("version")
                            else path.name.rsplit("-", 1)[0])


def _hook_commands(claude_home: Path, codex_home: Path, managed: Path) -> list:
    """[(where, command)] of every hook and the status line the agents run."""
    out = []
    for label, f in (("Claude", claude_home / "settings.json"), ("Claude managed settings", managed),
                     ("Codex", codex_home / "hooks.json")):
        data = core.read_json(f, {}) if f and f.is_file() else {}
        data = data if isinstance(data, dict) else {}
        for event, groups in sorted((data.get("hooks") or {}).items()):
            for g in groups or []:
                for h in (g or {}).get("hooks") or []:
                    out.append((f"{label} {event}", str((h or {}).get("command") or "")))
        if label == "Claude":
            out.append(("Claude status line", str((data.get("statusLine") or {}).get("command") or "")))
    return out


def old_copy_checks(claude_home=None, codex_home=None, managed=None) -> list:
    """`dev doctor`'s rows on the devkit copies older than the release install.json pins: hooks, the status line and
    the instructions an older copy wrote back, and other copies whose bin/dev still runs their own code (a session
    that started before `dev install` runs them) -> [(group, name, ok, detail, fix)]; [] when no pin holds up."""
    pin = core.pinned_release()
    if not pin:
        return []
    from . import policy
    claude_home, codex_home = Path(claude_home or core.CLAUDE_HOME), Path(codex_home or core.CODEX_HOME)
    managed = Path(managed or policy.MANAGED_FILE)
    root, want = Path(core.real(core.pin_root())), core.version_key(pin["version"])
    older = []
    for where, cmd in _hook_commands(claude_home, codex_home, managed):
        m = re.search(re.escape(str(root)) + r"/([^/'\"\s]+)/", cmd) or \
            re.search(re.escape(str(core.pin_root())) + r"/([^/'\"\s]+)/", cmd)
        if m and m.group(1) != pin["name"]:
            got = _copy_version(root / m.group(1))
            if got and got < want:
                older.append(f"{where} runs devkit {'.'.join(map(str, got))} ({m.group(1)})")
    for f in (claude_home / "CLAUDE.md", codex_home / "AGENTS.md"):
        try:
            m = _GENERATED.search(f.read_text()[:300])
        except OSError:
            m = None
        got = core.version_key(m.group(1)) if m else None
        if got and got < want:
            older.append(f"{core.tilde(f)} is devkit {m.group(1)}'s")
    rows = [("Pinned install", "agents run the pinned release", not older,
             f"hooks, status line and instructions are devkit {pin['version']}'s" if not older else
             "; ".join(older[:5]) + (f" (+{len(older) - 5} more)" if len(older) > 5 else "")
             + f": older than the installed devkit {pin['version']}",
             "dev config repair, then restart the sessions that started before dev install")]
    stale = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.name.startswith(".") or d.name == pin["name"] or not core.is_pinned(d):
            continue
        to = core.retired_to(d)
        if not to or core.real(to) != core.real(pin["path"]):
            stale.append(d.name)
    rows.append(("Pinned install", "older copies retired", not stale,
                 f"every other copy's bin/dev runs devkit {pin['version']}" if not stale else
                 f"{', '.join(stale)} still run their own code: a session that started before dev install can write "
                 f"their hooks and instructions back", "dev config repair (the installed release retires them)"))
    return rows


# --------------------------------------------------------------------------- all of them

REPO_CHECKS = (("D1", d1_enforced_rows), ("D3", d3_unwired), ("D4", d4_commands), ("D5", d5_hook_events),
               ("D6", d6_config_keys), ("D7", d7_raci_hygiene), ("D9", d9_phase_proofs), ("D10", d10_file_anchors),
               ("D11", d11_guardrails), ("D12", d12_hermetic_tests), ("D13", d13_completion),
               ("D14", d14_skill_names))
MACHINE_CHECKS = (("D2", d2_planned_rows), ("D7", d7_team_raci),
                  ("D8", lambda: d8_uninstalled_skills("claude") + d8_uninstalled_skills("codex")),
                  ("D10", d10_lesson_anchors))


def run(machine: bool = True) -> list:
    """Every finding, by check id. `machine=False`: only the checks that read the repository alone."""
    out = []
    for _, fn in REPO_CHECKS + (MACHINE_CHECKS if machine else ()):
        out += fn()
    return sorted(out, key=lambda f: (int(f.check[1:]), f.item))
