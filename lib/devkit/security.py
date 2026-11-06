# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
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


def staged_secrets(cwd) -> list:
    """Secrets in the staged changes (redacted findings), or [] — used by the guard before `git commit`."""
    if not core.have("gitleaks") or not core.gitout(cwd, "rev-parse", "--show-toplevel"):
        return []
    rc, out, _ = _run(["gitleaks", "git", "--staged", "--no-banner", "--redact", "--exit-code", "3",
                       "--report-format", "json", "--report-path", "-", "."], cwd, timeout=20)
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
        report("secrets (gitleaks" + (", history)" if history else ")"), rc == 0,
               "none found" if rc == 0 else f"{len(found)} finding(s)" if rc == 3 else f"error: {err.strip()[:80]}",
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
                    path = (key.get("verbatim_path") or key.get("given_path") or "").lstrip("./")
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
