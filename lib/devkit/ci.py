# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ci`: show what GitHub Actions will run, so it can be run locally first.

A deliberately small, dependency-free reader for .github/workflows/*.yml: it
extracts triggers, jobs, runners, matrices and each step's `uses:`/`with:` and
`run:` commands. It does not evaluate expressions; `${{ … }}` is shown verbatim.
"""
from __future__ import annotations

import re
from pathlib import Path


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def parse_workflow(text: str) -> dict:
    lines = text.splitlines()
    wf = {"name": None, "on": [], "jobs": []}
    i, n = 0, len(lines)
    section = None
    job = None
    step = None
    jobs_indent = None
    while i < n:
        raw = lines[i]
        s = raw.strip()
        if not s or s.startswith("#"):
            i += 1
            continue
        ind = _indent(raw)
        if ind == 0:
            section = s.split(":", 1)[0].strip().strip('"\'')
            if section == "name":
                wf["name"] = s.split(":", 1)[1].strip().strip('"\'')
            elif section in ("on", "true"):
                rest = s.split(":", 1)[1].strip()
                if rest:
                    wf["on"] += [t.strip() for t in rest.strip("[]").split(",") if t.strip()]
            i += 1
            continue
        if section in ("on", "true") and ind == 2:
            key = s.split(":", 1)[0].strip()
            if key and not key.startswith("-"):
                wf["on"].append(key)
        if section == "jobs":
            if jobs_indent is None:
                jobs_indent = ind
            if ind == jobs_indent and s.endswith(":"):
                job = {"id": s[:-1].strip(), "name": None, "runs_on": None, "matrix": [], "needs": None, "steps": []}
                wf["jobs"].append(job)
                step = None
            elif job is not None and ind == jobs_indent + 2:
                key, _, val = s.partition(":")
                key, val = key.strip(), val.strip()
                if key == "name":
                    job["name"] = val.strip('"\'')
                elif key == "runs-on":
                    job["runs_on"] = val
                elif key == "needs":
                    job["needs"] = val
                elif key == "strategy":
                    j = i + 1
                    while j < n and (not lines[j].strip() or _indent(lines[j]) > ind):
                        if lines[j].strip() and not lines[j].strip().startswith(("matrix:", "fail-fast", "max-parallel")):
                            job["matrix"].append(lines[j].strip())
                        j += 1
            elif job is not None and s.startswith("- ") and ind >= jobs_indent + 4:
                step = {"name": None, "uses": None, "with": [], "run": None, "if": None}
                job["steps"].append(step)
                s = s[2:].strip()
                ind += 2
                _step_kv(step, s, lines, i, ind)
            elif step is not None and ind >= jobs_indent + 6:
                _step_kv(step, s, lines, i, ind)
        i += 1
    return wf


def _step_kv(step, s, lines, i, ind):
    key, _, val = s.partition(":")
    key, val = key.strip(), val.strip()
    if key == "name":
        step["name"] = val.strip('"\'')
    elif key == "uses":
        step["uses"] = val
    elif key == "if":
        step["if"] = val
    elif key == "run":
        if val in ("|", ">", "|-", ">-", "|+"):
            body, j = [], i + 1
            while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > ind):
                body.append(lines[j])
                j += 1
            base = min((_indent(b) for b in body if b.strip()), default=0)
            step["run"] = "\n".join(b[base:] for b in body).strip("\n")
        else:
            step["run"] = val
    elif key == "with":
        j = i + 1
        while j < len(lines) and (not lines[j].strip() or _indent(lines[j]) > ind):
            if lines[j].strip():
                step["with"].append(lines[j].strip())
            j += 1


def workflows(root: Path):
    d = Path(root) / ".github" / "workflows"
    if not d.is_dir():
        return []
    out = []
    for f in sorted(list(d.glob("*.yml")) + list(d.glob("*.yaml"))):
        try:
            wf = parse_workflow(f.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        wf["file"] = f.name
        out.append(wf)
    return out


def render(root: Path, ui) -> str:
    wfs = workflows(root)
    if not wfs:
        return "  no GitHub Actions workflows in .github/workflows"
    out = []
    for wf in wfs:
        trig = ", ".join(dict.fromkeys(wf["on"])) or "?"
        out.append(ui.section(f"{wf['name'] or wf['file']}  {ui.dim(wf['file'] + ' · on: ' + trig)}"))
        for job in wf["jobs"]:
            meta = " · ".join(x for x in [job["runs_on"] and f"runs-on {job['runs_on']}",
                                          job["needs"] and f"needs {job['needs']}"] if x)
            out.append(f"  {ui.bold(job['name'] or job['id'])}  {ui.dim(meta)}")
            for m in job["matrix"][:6]:
                out.append(f"    {ui.dim('matrix ' + m)}")
            for st in job["steps"]:
                cond = f" {ui.dim('if ' + st['if'])}" if st["if"] else ""
                if st["run"]:
                    label = st["name"] or "run"
                    out.append(f"    {ui.cyan('$')} {ui.dim(label)}{cond}")
                    for ln in st["run"].splitlines():
                        out.append(f"      {ln}")
                elif st["uses"]:
                    w = f" ({', '.join(st['with'][:3])})" if st["with"] else ""
                    out.append(f"    {ui.dim('uses ' + st['uses'] + w)}{cond}")
    out.append("")
    out.append(ui.dim("  Run the `$` commands locally (same tool versions as the setup steps) before pushing. "
                      "One push, one CI run."))
    return "\n".join(out)


def local_commands(root: Path) -> list:
    """The run commands CI runs, in order, as a developer runs them here (`dev ci --commands`, the delivery policy's
    test gate): without the steps that need CI secrets, and without a hosted runner's own setup (OS packages, browser
    installs, sudo, housekeeping that deletes outside the checkout), which `dev ci --run` skips too: one classifier,
    gitx.runner_setup."""
    from . import gitx
    cmds = []
    for wf in workflows(root):
        for job in wf["jobs"]:
            for st in job["steps"]:
                if st["run"] and not re.search(r"\$\{\{\s*secrets\.", st["run"]) and \
                        not gitx.runner_setup(st["run"], root):
                    cmds.append(st["run"])
    return cmds
