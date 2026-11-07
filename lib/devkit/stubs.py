# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (this file defines the markers it looks for)
"""Unfinished work: stubs, placeholders and TODOs left in delivered code.

devkit builds things for real. A marker of unfinished work is allowed only when it is planned and tracked: the line
names its task (T-xxxxxxxxxx, from `dev task add`), its issue (#123 or an issue URL), or gives an explicit reason
(`devkit: allow-stub <reason>`). Everything else is reported:

  todo              TODO, FIXME, XXX, HACK
  not-implemented   raise NotImplementedError, NotImplementedException, throw new Error("not implemented"),
                    UnsupportedOperationException("not implemented"), todo!(), unimplemented!(), panic("todo")
  placeholder       TBD, "to be implemented", "coming soon", lorem ipsum, and "placeholder" or "stub" in a comment
                    (first, or in one clause with "for now", "until", "temporary" and the like)
  empty-body        a Python function whose body is only `...` (abstract methods, Protocols, @overload and .pyi
                    files are fine)

Used by the Stop hook (the files a development turn changed), the guard (a commit that would include them), and
`dev stubs` (a change or a whole repository).
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from pathlib import Path

CODE_EXT = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go", ".rs", ".rb",
            ".php", ".cs", ".swift", ".scala", ".c", ".h", ".cpp", ".hpp", ".m", ".sh", ".bash", ".zsh", ".sql",
            ".vue", ".svelte", ".html", ".css", ".scss", ".yml", ".yaml", ".toml", ".tf", ".gradle", ".xml", ".md",
            ".mdx"}
SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "__pycache__/", ".venv/", "venv/", "target/",
             ".next/", "coverage/")
COMMENT = r"(?:(?:^|\s)(?:#|//|/\*|<!--|--)|^\s*\*)\s*"  # a real comment marker, not ** or ;
# up to 40 characters inside one clause: a stub word and its time word ("for now", "until") belong to the same
# statement; across a `;`, `!`, `?` or a period that ends a sentence they say two things ("in a temporary state;
# nothing is stubbed"). A dot inside a name (cli.main, 1.2) is no boundary. Punctuation, so no words of a language
CLAUSE = r"(?:[^.;!?]|\.(?=\S)){0,40}"
RULES = [
    ("todo", re.compile(r"\b(TODO|FIXME)\b|\b(XXX|HACK)\b(?=\s*[:(\-]|\s*$)")),
    ("not-implemented", re.compile(
        r"raise\s+NotImplementedError|NotImplementedException|"
        r"throw\s+new\s+(?:Error|UnsupportedOperationException|RuntimeException|IllegalStateException)\(\s*['\"]"
        r"\s*(?:not\s+(?:yet\s+)?implemented|todo|implement\s+me)|"
        r"\btodo!\(|\bunimplemented!\(|panic\(\s*['\"](?:todo|not\s+implemented)", re.I)),
    ("placeholder", re.compile(
        r"\bTBD\b|\bto\s+be\s+implemented\b|\bcoming\s+soon\b|\blorem\s+ipsum\b|"
        + COMMENT + r"(?:placeholder|stub(?:bed)?)\b|"  # the comment says what it is: "# placeholder", "// stub: …"
        + COMMENT + r".*\b(?:placeholder|stub(?:bed)?|dummy|fake)\b" + CLAUSE + r"\b(?:for\s+now|until|later|"
        r"temporar\w*|implementation|replace\s+(?:this|with|me))\b|"
        + COMMENT + r".*\b(?:for\s+now|temporar\w*)\b" + CLAUSE + r"\b(?:placeholder|stub(?:bed)?)\b", re.I)),
]
ALLOW = re.compile(r"\bT-[0-9a-z]{6,}\b|(?<![\w&])#\d{1,7}\b|/issues/\d+|devkit:\s*allow-stub\b\s*\S+", re.I)
FILE_PRAGMA = "devkit: allow-stub-words"


def _wanted(rel: str) -> bool:
    if any(rel.startswith(d) or f"/{d}" in rel for d in SKIP_DIRS):
        return False
    return Path(rel).suffix.lower() in CODE_EXT or Path(rel).name in ("Dockerfile", "Makefile")


def _ellipsis_ok(lines: list, i: int, path: str) -> bool:
    """`...` as a body is fine in .pyi files, Protocols, abstract methods and overloads."""
    if path.endswith(".pyi"):
        return True
    ctx = "\n".join(lines[max(0, i - 8): i])
    return bool(re.search(r"@(abc\.)?abstractmethod|@(typing\.)?overload|\(\s*(typing\.)?Protocol\b|Protocol\[", ctx))


MD_MARKER = re.compile(r"^\s*(?:[-*>]\s+)?(?:\*\*)?(TODO|FIXME|XXX)\b\s*(?:\*\*)?[:(]|<!--\s*(TODO|FIXME)\b|"
                       r"\bTBD\b|\bcoming\s+soon\b|\blorem\s+ipsum\b", re.I)


def _scan_markdown(path: str, lines: list, targets) -> list:
    """Docs talk about TODOs; a doc is unfinished only where it says so: a line or note that starts with TODO or
    FIXME, an HTML comment, TBD, "coming soon", lorem ipsum. Inline code and fenced blocks are examples, not work."""
    out, fence, in_fence = [], set(), False
    for n, ln in enumerate(lines, 1):
        if re.match(r"\s*(```|~~~)", ln):
            in_fence = not in_fence
            fence.add(n)
            continue
        if in_fence:
            fence.add(n)
    for n in targets:
        if n < 1 or n > len(lines) or n in fence:
            continue
        ln = re.sub(r"`[^`]*`", "``", lines[n - 1])
        if ALLOW.search(lines[n - 1]):
            continue
        if MD_MARKER.search(ln):
            out.append({"file": path, "line": n, "rule": "placeholder" if re.search(r"TBD|coming|lorem", ln, re.I)
                        else "todo", "text": lines[n - 1].strip()[:140]})
    return out


def scan_text(path: str, text: str, only_lines=None) -> list:
    """Findings in one file: [{file, line, rule, text}]. `only_lines`: the 1-based lines to check (added lines)."""
    lines = text.splitlines()
    if FILE_PRAGMA in "\n".join(lines[:30]):
        return []
    if path.endswith((".md", ".mdx")):
        return _scan_markdown(path, lines, sorted(only_lines) if only_lines is not None else range(1, len(lines) + 1))
    out = []
    targets = sorted(only_lines) if only_lines is not None else range(1, len(lines) + 1)
    for n in targets:
        if n < 1 or n > len(lines):
            continue
        ln = lines[n - 1]
        if ALLOW.search(ln):
            continue
        for rule, rx in RULES:
            if path.endswith((".md", ".mdx")) and rule == "not-implemented":
                continue
            if rx.search(ln):
                if rule == "not-implemented" and re.search(r"\babstract\b", "\n".join(lines[max(0, n - 4): n]), re.I):
                    break  # an abstract hook of a base class: subclasses implement it
                out.append({"file": path, "line": n, "rule": rule, "text": ln.strip()[:140]})
                break
        else:
            if path.endswith((".py", ".pyi")) and re.fullmatch(r"\s*\.\.\.\s*(#.*)?", ln):
                prev = next((lines[j] for j in range(n - 2, max(-1, n - 6), -1) if lines[j].strip()), "")
                if re.match(r"\s*(async\s+)?def\s|.*\)\s*(->.*)?:\s*$", prev) and not _ellipsis_ok(lines, n - 1, path):
                    out.append({"file": path, "line": n, "rule": "empty-body", "text": prev.strip()[:140]})
    return out


def _git(top: str, *args) -> str:
    try:
        return subprocess.run(["git", "-C", top, *args], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def added_lines(top: str, files=None, staged: bool = False, base: str = "HEAD") -> dict:
    """{repo-relative path: set(added line numbers in the working tree, or in the index with staged)}; untracked
    files count as wholly added."""
    args = ["diff", "-U0", "--no-color", "--no-ext-diff"]
    args += ["--cached"] if staged else [base]
    if files:
        args += ["--"] + list(files)
    out, cur = {}, None
    for ln in _git(top, *args).splitlines():
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            cur = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
            if cur:
                out.setdefault(cur, set())
        elif ln.startswith("@@") and cur:
            m = re.search(r"\+(\d+)(?:,(\d+))?", ln)
            if m:
                start, count = int(m.group(1)), int(m.group(2) or 1)
                out[cur].update(range(start, start + count))
    if not staged:
        untracked = _git(top, "ls-files", "--others", "--exclude-standard", *(["--"] + list(files) if files else []))
        for p in untracked.splitlines():
            out[p] = None  # the whole file
    return out


def scan_changes(top: str, files=None, staged: bool = False, base: str = "HEAD") -> list:
    findings = []
    for rel, lines in added_lines(top, files, staged, base).items():
        if not _wanted(rel):
            continue
        if staged:
            text = _git(top, "show", f":{rel}")
        else:
            try:
                text = (Path(top) / rel).read_text(errors="replace")
            except OSError:
                continue
        if lines is not None and not lines:
            continue
        findings += scan_text(rel, text, lines)
    return findings


def scan_repo(top: str, paths=None) -> list:
    files = _git(top, "ls-files", *(["--"] + list(paths) if paths else [])).splitlines()
    findings = []
    for rel in files:
        if not _wanted(rel):
            continue
        try:
            findings += scan_text(rel, (Path(top) / rel).read_text(errors="replace"))
        except OSError:
            continue
    return findings


def message(findings: list, limit: int = 8) -> str:
    lines = [f"- {f['file']}:{f['line']} {f['rule']}: {f['text']}" for f in findings[:limit]]
    more = f"\n- … and {len(findings) - limit} more (dev stubs)" if len(findings) > limit else ""
    return ("These lines leave unfinished work (stubs, placeholders or TODOs):\n" + "\n".join(lines) + more +
            "\nBuild it for real now. If deferring it is part of the plan, record the task (dev task add \"…\" --why "
            "\"…\") and name it on the line, for example TODO(T-fwk6abc123): …; an issue (#123) works too.")


def stop_gate(top: str, files: list) -> str | None:
    """The Stop hook: the files a development turn changed."""
    rels = []
    for f in files:
        try:
            rels.append(os.path.relpath(f, top))
        except ValueError:
            continue
    found = scan_changes(top, rels) if rels else []
    return message(found) if found else None


def commit_gate(top: str, command: str) -> str | None:
    """The guard, before `git commit` / `dev commit`: the staged changes, plus what `git add`, `-a` or `--all` in the
    same command would stage."""
    files, everything = [], bool(re.search(r"\bgit\s+add\s+(-A|--all|\.)(\s|$)|\bcommit\b[^|;&]*\s-(a|[a-zA-Z]*a[a-zA-Z]*)\b"
                                           r"|\bdev\s+commit\b[^|;&]*\s(-a|--all)\b", command))
    for m in re.finditer(r"\bgit\s+add\s+([^;&|]+)", command):
        files += [x for x in m.group(1).split() if not x.startswith("-")]
    found = scan_changes(top, staged=True)
    if everything:
        found += scan_changes(top)
    elif files:
        found += scan_changes(top, files)
    seen, uniq = set(), []
    for f in found:
        k = (f["file"], f["line"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    if not uniq:
        return None
    return message(uniq) + " Commit after that (DEVKIT_ALLOW_STUBS=1 only when the developer accepts them)."


def matches_any(rel: str, globs) -> bool:
    return any(fnmatch.fnmatch(rel, g) for g in globs or [])


def cmd(a) -> int:
    from . import core, ui
    if getattr(a, "project", None):  # another project, by id or a unique part of its name
        from . import cli
        top = cli._resolve(cli._reg(prs=False), a.project)["path"]
    else:
        top = core.gitout(os.getcwd(), "rev-parse", "--show-toplevel")
    if not top:
        ui.err("not inside a repository")
        return 2
    if a.all:
        found = scan_repo(top, a.paths or None)
    else:
        found = scan_changes(top, a.paths or None, staged=a.staged, base=a.since or "HEAD")
    if a.json:
        import json
        print(json.dumps(found, ensure_ascii=False))
        return 1 if found else 0
    what = "the repository" if a.all else ("the staged changes" if a.staged else f"the changes since {a.since or 'HEAD'}")
    print(ui.section(f"Unfinished work in {what}"))
    if not found:
        print(f"  {ui.OK} none: no stubs, placeholders or untracked TODOs")
        return 0
    by = {}
    for f in found:
        by.setdefault(f["file"], []).append(f)
    for path, fs in by.items():
        print(f"  {ui.bold(path)}")
        for f in fs:
            print(f"    {str(f['line']).rjust(5)}  {ui.yellow(f['rule'].ljust(16))} {ui.dim(f['text'])}")
    print(ui.dim(f"\n  {len(found)} finding(s). Build them for real, or track each one: dev task add \"…\" --why \"…\", "
                 f"then TODO(T-…) on the line."))
    return 1


def register(add):
    p = add("stubs", cmd, "Unfinished work (stubs, placeholders, TODOs without a task) in your changes or the repo")
    p.add_argument("paths", nargs="*", help="limit to these paths")
    p.add_argument("--staged", action="store_true", help="only what is staged for the next commit")
    p.add_argument("--since", help="changes since this ref (default: uncommitted changes, against HEAD)")
    p.add_argument("--all", action="store_true", help="every tracked file, not only the changes")
    p.add_argument("--json", action="store_true")
    p.add_argument("-p", "--project", help="another project (id or a unique part of its name)")
