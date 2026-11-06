# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev docs check`: Markdown structure, links and anchors, prose, and diagrams — in one pass.

Uses the repository's own configuration when it has one (.markdownlint*, .vale.ini, lychee.toml),
otherwise devkit's defaults in config/docs/. Each checker is optional: a missing tool is reported,
not fatal. Exit code 1 when any checker reports errors.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import core, ui
from .core import DEVKIT_HOME, HOME

VALE_STYLES = HOME / ".local" / "share" / "devkit" / "vale" / "styles"
VALE_INI = HOME / ".local" / "share" / "devkit" / "vale" / ".vale.ini"
MD_SKIP = ("node_modules", ".venv", "vendor", "dist", ".git", "build", "target", ".worktrees")


def ensure_vale() -> bool:
    """Write devkit's Vale config with its styles path and download the style packages (once)."""
    if not core.have("vale"):
        return False
    VALE_STYLES.mkdir(parents=True, exist_ok=True)
    text = (DEVKIT_HOME / "config" / "docs" / "vale.ini").read_text().replace("{{STYLES}}", str(VALE_STYLES))
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
            lines, errors = [], []
            try:
                data = json.loads(p.stdout or "{}")
            except ValueError:
                data = {}
                errors.append("vale output could not be read: " + (p.stdout or p.stderr).strip()[:200])
            for ln in (p.stderr or "").splitlines():  # e.g. E201 invalid front matter: Vale skipped that file
                if ln.strip() and ("E1" in ln or "E2" in ln or "Error" in ln):
                    errors.append("vale: " + ln.strip()[:200])
            for f, alerts in sorted(data.items()) if isinstance(data, dict) else []:
                for al in alerts:
                    ln = f"{f}:{al.get('Line')}:{al.get('Span', [0])[0]}: {al.get('Severity')} {al.get('Check')}: " \
                         f"{al.get('Message')}"
                    lines.append(ln)
                    if al.get("Severity") == "error":
                        errors.append(ln)
            lines += [e for e in errors if e.startswith("vale")]
            print(f"  {ui.OK if not lines else (ui.FAIL if errors else ui.WARN)} {len(lines)} finding(s)"
                  f"{f', {len(errors)} error(s)' if errors else ''}")
            for ln in lines[:40]:
                print(f"    {ui.dim(ln)}")
            failed |= bool(errors)
        else:
            print(f"  {ui.WARN} vale not available (brew install vale)")

    svgs = [p for p in root.rglob("*.svg") if not any(s in p.parts for s in MD_SKIP)] if not paths else []
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
