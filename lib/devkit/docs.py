# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev docs check`: Markdown structure, links and anchors, prose, and diagrams — in one pass.

Uses the repository's own configuration when it has one (.markdownlint*, .vale.ini, lychee.toml),
otherwise devkit's defaults in config/docs/. Each checker is optional: a missing tool is reported,
not fatal. Exit code 1 when any checker reports errors.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import core, ui
from .core import DEVKIT_HOME, HOME

VALE_DIR = HOME / ".local" / "share" / "devkit" / "vale"
VALE_STYLES = VALE_DIR / "styles"  # the style packages, downloaded once and shared by every devkit copy
VALE_SHARED_NAME = ".vale.ini"  # the one config older devkit copies share in VALE_DIR; `dev gc` removes it
COPY_LINE = "# devkit copy: "  # a config's first line: the folder of the copy that wrote it, for `dev gc`


def vale_ini_of(home: Path) -> Path:
    """The Vale config of the devkit copy in `home`, named after its folder. Each copy writes its own: a branch
    checkout's docs check runs with that branch's rules and never rewrites the one the pinned copy's sessions read."""
    home = Path(home).resolve()
    name = "".join(c if c.isalnum() or c in "._-" else "-" for c in home.name) or "devkit"
    return VALE_DIR / "configs" / f"{name}-{hashlib.sha256(str(home).encode()).hexdigest()[:10]}.vale.ini"


def vale_copy(ini: Path):
    """The folder of the devkit copy that wrote a Vale config of VALE_DIR/configs (its first line), or None for a
    config that names none (written before configs named their copy) or cannot be read."""
    try:
        with open(ini, encoding="utf-8") as f:
            first = f.readline().rstrip("\n")
    except (OSError, UnicodeDecodeError):
        return None
    if not first.startswith(COPY_LINE):
        return None
    return first[len(COPY_LINE):] or None


VALE_INI = vale_ini_of(DEVKIT_HOME)
MD_SKIP = ("node_modules", ".venv", "vendor", "dist", ".git", "build", "target", ".worktrees")
# Folders of test fixtures: the images there are what a test loads, not a picture a reader sees, so the diagram rules
# (a title, no scripts, readable type) do not apply to them.
FIXTURE_DIRS = ("fixtures", "__fixtures__", "testdata")
VALE_WARNINGS_SHOWN = 40  # Vale's errors are all listed; its warnings up to this many, then a count and the command


def ensure_vale() -> bool:
    """Write this copy's Vale config with the shared styles path and download the style packages (once). Its first line
    names the copy's folder, so `dev gc` removes the config once the copy is gone."""
    if not core.have("vale"):
        return False
    VALE_STYLES.mkdir(parents=True, exist_ok=True)
    text = f"{COPY_LINE}{Path(DEVKIT_HOME).resolve()}\n" + \
        (DEVKIT_HOME / "config" / "docs" / "vale.ini").read_text().replace("{{STYLES}}", str(VALE_STYLES))
    core.write_if_changed(VALE_INI, text)
    if not (VALE_STYLES / "Google").exists() or not (VALE_STYLES / "write-good").exists():
        core.sh(["vale", "--config", str(VALE_INI), "sync"], timeout=300)
    return (VALE_STYLES / "Google").exists()


def markdown_files(root: Path, paths=None) -> list:
    if paths:
        out = []
        for p in paths:
            p = Path(p)
            out += [str(x) for x in p.rglob("*.md")] if p.is_dir() else [str(p)]
        return out
    out = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in MD_SKIP and not d.startswith(".")]
        out += [os.path.join(dirpath, f) for f in files if f.endswith(".md")]
    return sorted(out)


def _vale_stops(stderr: str):
    """-> ([the JSON objects Vale writes for a file it stopped on: Path, Line, Code, Text], the rest of stderr)."""
    stops, rest, i, dec = [], [], 0, json.JSONDecoder()
    while i < len(stderr):
        j = stderr.find("{", i)
        if j == -1:
            rest.append(stderr[i:])
            break
        try:
            obj, end = dec.raw_decode(stderr, j)
        except ValueError:
            rest.append(stderr[i:j + 1])
            i = j + 1
            continue
        rest.append(stderr[i:j])
        if isinstance(obj, dict) and obj.get("Code"):
            stops.append(obj)
        else:
            rest.append(stderr[j:end])
        i = end
    return stops, "".join(rest)


def _run(args, cwd):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def check(root: Path, paths=None, online=False, fix=False, prose=True) -> int:
    files = markdown_files(root, paths)
    if not files:
        print(f"  {ui.WARN} no Markdown files")
        return 0
    rel = [os.path.relpath(f, root) for f in files]
    failed = 0
    print(ui.section(f"Markdown structure · {len(files)} file(s)"))
    if core.have("markdownlint-cli2"):
        own = any((root / n).exists() for n in (".markdownlint-cli2.jsonc", ".markdownlint-cli2.yaml",
                                                ".markdownlint.json", ".markdownlint.jsonc", ".markdownlint.yaml"))
        args = ["markdownlint-cli2"] + (["--fix"] if fix else [])
        if not own:
            args += ["--config", str(DEVKIT_HOME / "config" / "docs" / "markdownlint.jsonc")]
        rc, out = _run(args + rel, root)
        issues = [ln for ln in out.splitlines() if ":" in ln and ("MD" in ln)]
        print(f"  {ui.OK if rc == 0 else ui.FAIL} {len(issues)} issue(s)" + (" (after --fix)" if fix else ""))
        for ln in issues[:40]:
            print(f"    {ui.dim(ln)}")
        failed |= rc != 0
    else:
        print(f"  {ui.WARN} markdownlint-cli2 not installed (npm i -g markdownlint-cli2)")

    print(ui.section("Links and anchors" + (" (including external)" if online else " (local; --online for external)")))
    if core.have("lychee"):
        args = ["lychee", "--no-progress", "--include-fragments", "--format", "compact"]
        if not online:
            args.append("--offline")
        rc, out = _run(args + rel, root)
        bad = [ln for ln in out.splitlines() if ln.strip().startswith(("[ERROR]", "✗", "[404]", "[ERR]")) or "Error" in ln]
        print(f"  {ui.OK if rc == 0 else ui.FAIL} " + ("all links resolve" if rc == 0 else f"{len(bad)} broken"))
        for ln in bad[:40]:
            print(f"    {ui.dim(ln.strip())}")
        failed |= rc != 0
    else:
        print(f"  {ui.WARN} lychee not installed (brew install lychee)")

    if prose:
        print(ui.section("Prose (Vale: Google + write-good)"))
        own_ini = root / ".vale.ini"
        if core.have("vale") and (own_ini.exists() or ensure_vale()):
            cfg = str(own_ini if own_ini.exists() else VALE_INI)
            p = subprocess.run(["vale", "--config", cfg, "--output", "JSON", "--minAlertLevel", "warning"] + rel,
                               cwd=root, capture_output=True, text=True)
            errors, warnings = [], []  # (file, line) each; the tool's own errors have no file
            try:
                data = json.loads(p.stdout or "{}")
            except ValueError:
                data = {}
                errors.append(("", "vale output could not be read: " + (p.stdout or p.stderr).strip()[:200]))
            stops, rest = _vale_stops(p.stderr or "")  # e.g. E201 front matter that is not YAML: Vale skipped the file
            errors += [(s.get("Path") or "", f"{s.get('Path') or '?'}:{s.get('Line') or 1}: vale {s.get('Code')}: "
                        f"{str(s.get('Text') or '').strip()[:200]}") for s in stops]
            for ln in rest.splitlines():
                if ln.strip() and ("E1" in ln or "E2" in ln or "Error" in ln):
                    errors.append(("", "vale: " + ln.strip()[:200]))
            for f, alerts in sorted(data.items()) if isinstance(data, dict) else []:
                for al in alerts:
                    ln = f"{f}:{al.get('Line')}:{al.get('Span', [0])[0]}: {al.get('Severity')} {al.get('Check')}: " \
                         f"{al.get('Message')}"
                    (errors if al.get("Severity") == "error" else warnings).append((f, ln))
            total = len(errors) + len(warnings)
            print(f"  {ui.OK if not total else (ui.FAIL if errors else ui.WARN)} {total} finding(s)"
                  f"{f', {len(errors)} error(s)' if errors else ''}")
            for _, ln in errors + warnings[:VALE_WARNINGS_SHOWN]:  # every error, then the first warnings
                print(f"    {ui.dim(ln)}")
            rest = warnings[VALE_WARNINGS_SHOWN:]
            if rest:
                where = list(dict.fromkeys(f for f, _ in rest))
                print(f"    {len(rest)} more warning(s) in {len(where)} file(s) not listed: "
                      f"vale --config {core.tilde(cfg)} --minAlertLevel warning {' '.join(where)}")
            failed |= bool(errors)
        else:
            print(f"  {ui.WARN} vale not available (brew install vale)")

    # only the folders inside the checkout count: a checkout under testdata/ or .worktrees/ still has its images checked
    svgs = [p for p in root.rglob("*.svg") if not any(s in p.relative_to(root).parts for s in MD_SKIP + FIXTURE_DIRS)] \
        if not paths else []
    if svgs:
        print(ui.section(f"Diagrams · {len(svgs)} SVG(s)"))
        try:
            from . import diagram
            problems = []
            for s in svgs:
                for pr in diagram.check_svg(s) if hasattr(diagram, "check_svg") else []:
                    problems.append(f"{os.path.relpath(s, root)}: {pr}")
            print(f"  {ui.OK if not problems else ui.FAIL} {len(problems)} problem(s)")
            for pr in problems[:40]:
                print(f"    {ui.dim(pr)}")
            failed |= bool(problems)
        except ImportError:
            print(f"  {ui.WARN} diagram checker not available")
    print()
    return 1 if failed else 0
