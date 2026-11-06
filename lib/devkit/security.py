# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev sec scan`: secrets, vulnerable dependencies, code patterns, and filesystem/IaC misconfiguration.

  gitleaks     secrets in the working tree and (with --history) the git history
  osv-scanner  known vulnerabilities in lockfiles and manifests (OSV database)
  semgrep      SAST rules (p/default; --config to change), metrics off
  trivy        HIGH/CRITICAL vulnerabilities, misconfiguration (Dockerfile, Terraform, Kubernetes) and secrets
  zizmor       GitHub Actions workflows: template injection, untrusted triggers, unpinned actions, permissions

Every tool is optional; a missing one is reported. Exit 1 when any scanner reports findings.
Also used by the guard: `staged_secrets()` blocks a commit that contains a secret.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from . import core, ui

SKIP = ["node_modules", ".venv", "venv", "vendor", "dist", "build", "target", ".git", ".worktrees", ".next"]


def _run(args, cwd, timeout=900):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        return 124, "", str(e)


def staged_secrets(cwd, adds=None, all_tracked=False) -> list:
    """Secrets in what the commit will contain (redacted findings), or [] — used by the guard before `git commit`.

    `adds` are the argument lists of `git add` commands that run earlier in the same shell command, and
    `all_tracked` is `git commit -a`: they are applied to a temporary copy of the index, so `git add . && git commit`
    is checked as well as an already staged change."""
    import os
    import shutil
    import subprocess
    import tempfile
    if not core.have("gitleaks") or not core.gitout(cwd, "rev-parse", "--show-toplevel"):
        return []
    env = dict(os.environ, **core.GIT_ENV) if isinstance(core.GIT_ENV, dict) else dict(os.environ)
    tmpdir = None
    if adds or all_tracked:
        tmpdir = tempfile.mkdtemp(prefix="devkit-index-")
        index = core.gitout(cwd, "rev-parse", "--git-path", "index")
        index = index if os.path.isabs(index) else os.path.join(core.gitout(cwd, "rev-parse", "--show-toplevel"), index)
        tmp_index = os.path.join(tmpdir, "index")
        if os.path.exists(index):
            shutil.copy2(index, tmp_index)
        env["GIT_INDEX_FILE"] = tmp_index
        steps = [list(a) for a in adds or [] if not set(a) & {"-p", "--patch", "-i", "--interactive", "-e", "--edit"}]
        if all_tracked:
            steps.append(["-u"])
        for a in steps:
            subprocess.run(["git", "add", *a], cwd=cwd, env=env, capture_output=True, timeout=30)
    try:
        p = subprocess.run(["gitleaks", "git", "--staged", "--no-banner", "--redact", "--exit-code", "3",
                            "--report-format", "json", "--report-path", "-", "."], cwd=cwd, env=env,
                           capture_output=True, text=True, timeout=30)
        rc, out = p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if rc != 3:
        return []
    try:
        return [f"{x.get('RuleID')} in {x.get('File')}:{x.get('StartLine')}" for x in json.loads(out or "[]")]
    except ValueError:
        return ["secret-like content in the staged changes"]


def scan(root: Path, history=False, verbose=False, sast_config="p/default") -> int:
    root = Path(root)
    failed = False
    results = []

    def report(name, ok, summary, details=()):
        nonlocal failed
        failed |= not ok
        results.append((name, ok, summary))
        print(f"  {ui.OK if ok else ui.FAIL} {name:<28} {summary}")
        for d in list(details)[: (60 if verbose else 8)]:
            print(f"      {ui.dim(d)}")

    print(ui.section(f"Security scan · {core.tilde(root)}"))
    # 1. secrets
    if core.have("gitleaks"):
        args = ["gitleaks", "git" if history else "dir", "--no-banner", "--redact", "--report-format", "json",
                "--report-path", "-", "--exit-code", "3"]
        args += ["."] if history else [str(root)]
        rc, out, err = _run(args, root)
        try:
            found = json.loads(out or "[]") if rc in (0, 3) else []
        except ValueError:
            found = []
        skipped = 0
        if found and not history:  # files git ignores (caches, builds, node_modules) are never committed
            ignored = set(subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin"], text=True,
                                         input="\n".join(str(x.get("File")) for x in found), capture_output=True,
                                         timeout=60).stdout.splitlines())
            kept = [x for x in found if str(x.get("File")) not in ignored]
            skipped, found = len(found) - len(kept), kept
            if not found:
                rc = 0
        report("secrets (gitleaks" + (", history)" if history else ")"), rc == 0,
               ("none found" if rc == 0 else f"{len(found)} finding(s)" if rc == 3 else f"error: {err.strip()[:80]}")
               + (f" · {skipped} in git-ignored files skipped" if skipped else ""),
               [f"{x.get('RuleID')}  {x.get('File')}:{x.get('StartLine')}" for x in found])
    else:
        report("secrets", True, ui.dim("gitleaks not installed — brew install gitleaks"))
    # 2. dependencies
    if core.have("osv-scanner"):
        rc, out, err = _run(["osv-scanner", "scan", "source", "-r", "--format", "json", "--verbosity", "error",
                             str(root)], root)
        vulns = []
        try:
            for res in json.loads(out or "{}").get("results", []):
                for pkg in res.get("packages", []):
                    for v in pkg.get("vulnerabilities", []):
                        vulns.append(f"{pkg['package'].get('name')}@{pkg['package'].get('version')}  {v.get('id')}  "
                                     f"{(v.get('summary') or '')[:70]}")
        except (ValueError, KeyError):
            pass
        no_lock = "No package sources found" in (out + err)
        report("dependencies (osv-scanner)", rc in (0, 128) or no_lock and not vulns,
               "no lockfiles" if no_lock else ("no known vulnerabilities" if not vulns else f"{len(vulns)} vulnerable"),
               vulns)
    else:
        report("dependencies", True, ui.dim("osv-scanner not installed — brew install osv-scanner"))
    # 3. SAST
    if core.have("semgrep"):
        rc, out, err = _run(["semgrep", "scan", "--config", sast_config, "--metrics", "off", "--disable-version-check", "--json", "--quiet",
                             *sum((["--exclude", s] for s in SKIP), [])], root)
        try:
            hits = json.loads(out or "{}").get("results", [])
        except ValueError:
            hits = []
        sev = [h for h in hits if (h.get("extra", {}).get("severity") or "").upper() in ("ERROR", "WARNING")]
        report(f"code (semgrep {sast_config})", not sev and rc in (0, 1),
               "no findings" if not hits else f"{len(sev)} error/warning, {len(hits) - len(sev)} info",
               [f"{h['extra'].get('severity')}  {h['path']}:{h['start']['line']}  {h['check_id'].split('.')[-1]}"
                for h in sev])
    else:
        report("code (SAST)", True, ui.dim("semgrep not installed — brew install semgrep"))
    # 4. filesystem: vulns + misconfig + secrets
    if core.have("trivy"):
        rc, out, err = _run(["trivy", "fs", "--disable-telemetry", "--skip-version-check", "--quiet", "--scanners", "vuln,misconfig,secret", "--severity",
                             "HIGH,CRITICAL", "--format", "json", "--exit-code", "0",
                             *sum((["--skip-dirs", s] for s in SKIP), []), str(root)], root)
        items = []
        try:
            for r in json.loads(out or "{}").get("Results", []) or []:
                for v in r.get("Vulnerabilities") or []:
                    items.append(f"{v.get('Severity')}  {v.get('PkgName')} {v.get('InstalledVersion')}  {v.get('VulnerabilityID')}")
                for m in r.get("Misconfigurations") or []:
                    items.append(f"{m.get('Severity')}  {r.get('Target')}  {m.get('ID')} {m.get('Title', '')[:60]}")
                for s in r.get("Secrets") or []:
                    items.append(f"{s.get('Severity')}  {r.get('Target')}:{s.get('StartLine')}  {s.get('RuleID')}")
        except ValueError:
            pass
        report("filesystem & IaC (trivy)", not items, "no HIGH/CRITICAL findings" if not items else f"{len(items)} finding(s)",
               items)
    else:
        report("filesystem & IaC", True, ui.dim("trivy not installed — brew install trivy"))
    # 5. GitHub Actions workflows: injection, untrusted triggers, unpinned actions, excessive permissions
    wf = root / ".github" / "workflows"
    if wf.is_dir() and any(wf.glob("*.y*ml")):
        if core.have("zizmor"):
            rc, out, err = _run(["zizmor", "--offline", "--format", "json", "--min-severity", "medium", "."], root)
            items = []
            try:
                for f in json.loads(out or "[]"):
                    l0 = (f.get("locations") or [{}])[0]
                    key = ((l0.get("symbolic") or {}).get("key") or {}).get("Local") or {}
                    path = re.sub(r"^(?:\./)+", "", key.get("verbatim_path") or key.get("given_path") or "")
                    row = (((l0.get("concrete") or {}).get("location") or {}).get("start_point") or {}).get("row")
                    where = f"{path}:{row + 1}" if path and row is not None else path
                    items.append(f"{(f.get('determinations') or {}).get('severity', '?')}  {where}  "
                                 f"{f.get('ident')}: {f.get('desc', '')[:70]}")
            except (ValueError, AttributeError):
                if rc not in (0,) and not out:
                    items.append((err or "zizmor failed").strip()[:160])
            report("GitHub Actions (zizmor)", not items, "no findings" if not items else f"{len(items)} finding(s)",
                   items)
        else:
            report("GitHub Actions", True, ui.dim("zizmor not installed — brew install zizmor"))
    print()
    return 1 if failed else 0
