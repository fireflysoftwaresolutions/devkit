# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev sec scan`: secrets, vulnerable dependencies, code patterns, and filesystem/IaC misconfiguration.

  gitleaks     secrets in the working tree and (with --history) the git history
  osv-scanner  known vulnerabilities in lockfiles and manifests (OSV database)
  semgrep      SAST rules (p/default; --config to change), metrics off, with the qualification gate's command and
               completeness rule (semgrep_command, semgrep_problems)
  trivy        HIGH/CRITICAL vulnerabilities, misconfiguration (Dockerfile, Terraform, Kubernetes) and secrets
  zizmor       GitHub Actions workflows: template injection, untrusted triggers, unpinned actions, permissions

Every tool is optional; a missing one is reported. Exit 1 for findings or an incomplete/invalid scan. Findings a
repository has reviewed and accepted are not waived here: they stay in that repository's own qualification gate
(devkit's is scripts/security_corpus.py).
Also used by `dev commit` and `dev git commit`: `staged_secrets()` refuses a commit that contains a secret.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import core, ui

SKIP = ["node_modules", ".venv", "venv", "vendor", "dist", "build", "target", ".git", ".worktrees", ".next"]

# Registry rules that can only match code that imports one of their packages, and whose taint search over every
# string literal runs past Semgrep's fixpoint budget on large modules (measured on devkit). Each is left out only
# when the scanned tree imports none of its packages, so it never hides a finding it could make.
SEMGREP_PACKAGE_RULES = {
    "python.boto3.security.hardcoded-token.hardcoded-token": ("boto3", "botocore"),
    "python.flask.security.injection.nan-injection.nan-injection": ("flask",),
    # measured on GitHub's ubuntu runner: a large module's level runs past the budget for this rule there
    "python.django.security.nan-injection.nan-injection": ("django",),
}
_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import\b|import[ \t]+([^\n#;]+))", re.M)


def _imported_packages(root, exclude=()) -> set:
    """The top-level packages that the tree's Python files import, from line-anchored import statements: a mention in
    text or in a string is no import. Python files are .py and .pyi files and extensionless python scripts."""
    names, skip = set(), set(exclude) | {".git"}
    for folder, subs, files in os.walk(root):
        subs[:] = [s for s in subs if s not in skip]
        for name in files:
            path, module = os.path.join(folder, name), name.endswith((".py", ".pyi"))
            if (not module and "." in name) or os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as fh:
                    if not module and not re.match(rb"#![^\n]*python", fh.readline(200)):
                        continue
                    fh.seek(0)
                    text = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            for m in _IMPORT.finditer(text):
                words = [m.group(1)] if m.group(1) else [w.split()[0] for w in m.group(2).split(",") if w.split()]
                names.update(w.split(".")[0] for w in words)
    return names


def semgrep_command(root, config="p/default", exclude=()) -> list:
    """The Semgrep command for a tree, shared by `dev sec scan` and the qualification gate
    (scripts/security_corpus.py), so both reach the same verdict on the same tree.

    Serial (--jobs 1): Semgrep's taint fixpoint budget reads CPU time its parallel workers share, so a parallel scan
    stops analyses at random. No per-rule wall-clock timeout (--timeout 0, --timeout-threshold 0): the verdict does
    not depend on the runner's speed. Rule ids as the rules file wrote them (--no-rewrite-rule-ids): Semgrep prefixes
    each id read from a local file with the file's folders, so a reviewed finding or an --exclude-rule would no longer
    match; registry ids are never prefixed. SEMGREP_PACKAGE_RULES are left out when the tree imports none of their
    packages; `exclude`: folder names the scan skips (and the import check with it). -> the argument list, with a
    JSON report on stdout; the caller adds its own options and targets (default: the working directory)."""
    imported = _imported_packages(root, exclude)
    argv = ["semgrep", "scan", "--config", config, "--no-rewrite-rule-ids", "--metrics", "off",
            "--disable-version-check", "--json", "--jobs", "1", "--timeout", "0", "--timeout-threshold", "0"]
    for rule, packages in SEMGREP_PACKAGE_RULES.items():
        if not imported & set(packages):
            argv += ["--exclude-rule", rule]
    for name in exclude:
        argv += ["--exclude", name]
    return argv


def semgrep_left_out(argv) -> list:
    """The package rules a Semgrep command leaves out -> [(rule, packages)]."""
    return [(argv[i + 1], SEMGREP_PACKAGE_RULES.get(argv[i + 1], ())) for i, word in enumerate(argv[:-1])
            if word == "--exclude-rule"]


def _word(value) -> str:
    """A report field shown as one short word: an error's type, path or rule, never its message."""
    if isinstance(value, list):
        value = value[0] if value else ""
    return re.sub(r"[\x00-\x1f\x7f]", "?", value)[:160] if isinstance(value, str) else ""


def semgrep_problems(report) -> list:
    """Why a Semgrep JSON report is incomplete, by the qualification gate's rule: its errors (rule timeouts among
    them), its fixpoint timeouts, and the rules it skipped -> [(type, path, rule)], never a message."""
    report = _object(report)
    timing = _object(report.get("time") or {})
    out = []
    for e in _records(report.get("errors") or []):
        out.append((_word(e.get("type")) or "error", _word(e.get("path")), _word(e.get("rule_id"))))
    for e in list(_records(timing.get("fixpoint_timeouts") or [])) + list(_records(timing.get("timeouts") or [])):
        where = e.get("location") if isinstance(e.get("location"), dict) else {}
        out.append((_word(e.get("error_type")) or "timeout", _word(where.get("path") or e.get("path")),
                    _word(e.get("rule_id"))))
    for e in _records(report.get("skipped_rules") or []):
        out.append(("Skipped rule", "", _word(e.get("rule_id"))))
    return out


def _run(args, cwd, timeout=900):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError) as e:
        return 124, "", str(e)


GITLEAKS_DEFAULT = "[extend]\nuseDefault = true\n"  # gitleaks' own built-in rules, and nothing else
GITLEAKS_ENV = ("GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML")  # where gitleaks would read a configuration from
STAGED_TIMEOUT = 30


def _from_head(top: str, name: str, folder: str, default: str) -> str:
    """The file `name` as the HEAD commit of the checkout `top` holds it, else `default`, written in `folder` under a
    name without the leading dot -> its path."""
    rc, text, _ = core.git(top, "show", f"HEAD:{name}")
    path = os.path.join(folder, name.lstrip("."))
    with open(path, "w") as fh:
        fh.write(text + "\n" if rc == 0 else default)
    return path


def committed_scan(top: str, folder: str, env: dict) -> tuple:
    """How gitleaks scans the repository of the checkout `top` with only what its HEAD commit holds -> (args, env,
    cwd). `args` name the configuration (.gitleaks.toml as HEAD holds it, else gitleaks' default rules,
    GITLEAKS_DEFAULT) and the ignore list (.gitleaksignore as HEAD holds it, else an empty one), both written in
    `folder`, a temporary folder of the scan's own. `env` is the given one without a configuration gitleaks would
    read from it (GITLEAKS_ENV), with GIT_DIR naming the repository, `folder` as its work tree and an index file it
    names by a relative path made absolute; `cwd` is `folder`: the scan's source path, which the caller names `.`.
    gitleaks reads `<source>/.gitleaksignore` whatever --gitleaks-ignore-path says, so it never runs in the
    checkout, where an untracked or changed .gitleaks.toml or .gitleaksignore next to the change could switch the
    scan off. `dev commit` (staged_secrets) and a backup's push (backup.scan) both scan this way. Raises OSError when
    git cannot name the repository's folder."""
    gitdir = core.gitout(top, "rev-parse", "--absolute-git-dir")
    if not gitdir:
        raise OSError(f"git cannot name the repository folder of {top}")
    env = dict(env)
    for name in GITLEAKS_ENV:
        env.pop(name, None)
    env.update(GIT_DIR=gitdir, GIT_WORK_TREE=folder)
    if env.get("GIT_INDEX_FILE"):
        env["GIT_INDEX_FILE"] = os.path.abspath(env["GIT_INDEX_FILE"])
    args = ["--config", _from_head(top, ".gitleaks.toml", folder, GITLEAKS_DEFAULT),
            "--gitleaks-ignore-path", _from_head(top, ".gitleaksignore", folder, "")]
    return args, env, folder


def staged_secrets(cwd, adds=None, all_tracked=False, skipped=None) -> list:
    """Secrets in what the commit will contain (redacted findings), or [] — used by `dev commit` before it commits.

    `adds` are the argument lists of `git add` commands that run earlier in the same shell command, and
    `all_tracked` is `git commit -a`: they are applied to a temporary copy of the index, so `git add . && git commit`
    is checked as well as an already staged change. gitleaks runs with the configuration and the ignore list the
    HEAD commit holds (committed_scan), never with ones the working tree or the environment add. `skipped`: a list
    that receives why the scan did not run (gitleaks missing, past STAGED_TIMEOUT, or failed); its findings are
    then []."""
    import shutil
    import tempfile
    skip = skipped.append if skipped is not None else (lambda why: None)
    top = core.gitout(cwd, "rev-parse", "--show-toplevel")
    if not top:
        return []
    if not core.have("gitleaks"):
        skip("gitleaks is not installed (brew install gitleaks)")
        return []
    env = dict(os.environ, **core.GIT_ENV) if isinstance(core.GIT_ENV, dict) else dict(os.environ)
    tmpdir = tempfile.mkdtemp(prefix="devkit-scan-")
    try:
        if adds or all_tracked:
            index = core.gitout(cwd, "rev-parse", "--git-path", "index")
            index = index if os.path.isabs(index) else os.path.join(top, index)
            tmp_index = os.path.join(tmpdir, "index")
            if os.path.exists(index):
                shutil.copy2(index, tmp_index)
            env["GIT_INDEX_FILE"] = tmp_index
            steps = [list(a) for a in adds or []
                     if not set(a) & {"-p", "--patch", "-i", "--interactive", "-e", "--edit"}]
            if all_tracked:
                steps.append(["-u"])
            for a in steps:
                subprocess.run(["git", "add", *a], cwd=cwd, env=env, capture_output=True, timeout=30)
        committed, scan_env, scan_cwd = committed_scan(top, tmpdir, env)
        p = subprocess.run(["gitleaks", "git", "--staged", "--no-banner", "--redact", "--exit-code", "3", *committed,
                            "--report-format", "json", "--report-path", "-", "."], cwd=scan_cwd,
                           env=scan_env, capture_output=True, text=True, timeout=STAGED_TIMEOUT)
        rc, out = p.returncode, p.stdout
    except subprocess.TimeoutExpired:
        skip(f"the scan did not finish within {STAGED_TIMEOUT} s")
        return []
    except OSError as e:
        skip(f"gitleaks could not start ({type(e).__name__})")
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if rc not in (0, 3):  # its output can quote the change: only the exit code is said
        skip(f"gitleaks failed (exit {rc})")
        return []
    if rc != 3:
        return []
    try:
        return [f"{x.get('RuleID')} in {x.get('File')}:{x.get('StartLine')}" for x in json.loads(out or "[]")]
    except ValueError:
        return ["secret-like content in the staged changes"]


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def _records(value):
    if not isinstance(value, list):
        raise ValueError("expected a list")
    for item in value:
        yield _object(item)


def _text(value):
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def scan(root: Path, history=False, verbose=False, sast_config="p/default") -> int:
    root = Path(root)
    failed = False

    def report(name, ok, summary, details=()):
        nonlocal failed
        failed |= not ok
        print(f"  {ui.OK if ok else ui.FAIL} {name:<28} {summary}")
        for d in list(details)[: (60 if verbose else 8)]:
            print(f"      {ui.dim(d)}")

    def scanned(name, rc, accepted, items, clean, summary, error="", finding_codes=()):
        # Neither stderr nor decoding exceptions are safe to print: either can contain source secrets.
        if rc not in accepted:
            error = f"scanner failed (exit {rc})" + (f"; {error}" if error else "")
        elif rc in finding_codes and not items and not error:
            error = f"exit {rc} reported findings without a finding report"
        report(name, not error and not items, "error: " + error if error else summary if items else clean, items)

    print(ui.section(f"Security scan · {core.tilde(root)}"))
    if core.have("gitleaks"):
        args = ["gitleaks", "git" if history else "dir", "--no-banner", "--redact", "--report-format", "json",
                "--report-path", "-", "--exit-code", "3"]
        args += ["."] if history else [str(root)]
        rc, out, _ = _run(args, root)
        found, error, skipped = [], "", 0
        try:
            for item in _records(json.loads(out)):
                _text(item["RuleID"])
                _text(item["File"])
                if type(item["StartLine"]) is not int:
                    raise ValueError("expected a line number")
                found.append(item)
        except (ValueError, KeyError, TypeError):
            error = "invalid JSON report or schema"
        if found and not history:
            try:
                ignored_run = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin"], text=True,
                                             input="\n".join(x["File"] for x in found), capture_output=True,
                                             timeout=60)
                if ignored_run.returncode not in (0, 1):
                    error = "git ignore check failed"
                else:
                    ignored = set(ignored_run.stdout.splitlines())
                    kept = [x for x in found if x["File"] not in ignored]
                    skipped, found = len(found) - len(kept), kept
                    if skipped and not found and rc == 3:
                        rc = 0
            except (OSError, subprocess.TimeoutExpired):
                error = "git ignore check failed"
        details = [f"{x['RuleID']}  {x['File']}:{x['StartLine']}" for x in found]
        suffix = f" · {skipped} in git-ignored files skipped" if skipped else ""
        scanned("secrets (gitleaks" + (", history)" if history else ")"), rc, (0, 3), details,
                "none found" + suffix, f"{len(found)} finding(s)" + suffix, error, (3,))
    else:
        report("secrets", True, ui.dim("gitleaks not installed — brew install gitleaks"))

    if core.have("osv-scanner"):
        rc, out, _ = _run(["osv-scanner", "scan", "source", "-r", "--format", "json", "--verbosity", "error",
                           str(root)], root)
        vulns, error = [], ""
        try:
            # OSV's documented no-packages exit may have no JSON output.
            if rc != 128 or out.strip():
                for res in _records(_object(json.loads(out))["results"]):
                    for pkg in _records(res["packages"]):
                        package = _object(pkg["package"])
                        for v in _records(pkg.get("vulnerabilities", [])):
                            vulns.append(f"{_text(package['name'])}@{_text(package['version'])}  {_text(v['id'])}  "
                                         f"{_text(v.get('summary') or '')[:70]}")
            if rc == 128 and vulns:
                error = "no-packages exit with vulnerability findings"
        except (ValueError, KeyError, TypeError):
            error = "invalid JSON report or schema"
        scanned("dependencies (osv-scanner)", rc, (0, 1, 128), vulns,
                "no lockfiles" if rc == 128 else "no known vulnerabilities", f"{len(vulns)} vulnerable", error, (1,))
    else:
        report("dependencies", True, ui.dim("osv-scanner not installed — brew install osv-scanner"))

    if core.have("semgrep"):
        command = semgrep_command(root, sast_config, exclude=SKIP)
        rc, out, _ = _run(command + ["--quiet"], root)
        details, error, total, problems = [], "", 0, []
        try:
            data = _object(json.loads(out))
            for h in _records(data["results"]):
                severity = _text(_object(h["extra"])["severity"]).upper()
                path, rule = _text(h["path"]), _text(h["check_id"])
                line = _object(h["start"])["line"]
                if type(line) is not int:
                    raise ValueError("expected a line number")
                total += 1
                if severity in ("ERROR", "WARNING"):
                    details.append(f"{severity}  {path}:{line}  {rule.split('.')[-1]}")
            problems = semgrep_problems(data)
            if problems:
                error = f"{len(problems)} scanner error(s); scan incomplete"
            elif rc == 1 and not total:
                error = "exit 1 reported findings without a finding report"
        except (ValueError, KeyError, TypeError):
            error = "invalid JSON report or schema"
        summary = f"{len(details)} error/warning, {total - len(details)} info"
        scanned(f"code (semgrep {sast_config})", rc, (0, 1), details,
                summary if total else "no findings", summary, error)
        for kind, path, rule in problems[:8]:
            print(f"      {ui.dim('  '.join((kind, path or '-', rule or '-')))}")
        if len(problems) > 8:
            print(f"      {ui.dim('… ' + str(len(problems) - 8) + ' more scanner error(s)')}")
        for rule, packages in semgrep_left_out(command):
            why = "no Python file here imports " + " or ".join(packages)
            print(f"      {ui.dim('left out ' + rule + ': ' + why)}")
    else:
        report("code (SAST)", True, ui.dim("semgrep not installed — brew install semgrep"))

    if core.have("trivy"):
        rc, out, _ = _run(["trivy", "fs", "--disable-telemetry", "--skip-version-check", "--quiet", "--scanners", "vuln,misconfig,secret", "--severity",
                           "HIGH,CRITICAL", "--format", "json", "--exit-code", "0",
                           *sum((["--skip-dirs", s] for s in SKIP), []), str(root)], root)
        items, error = [], ""
        try:
            data = _object(json.loads(out))
            # Trivy omits Results when no targets have reportable results.
            if "Results" not in data and type(data.get("SchemaVersion")) is not int:
                raise ValueError("missing results")
            results = data.get("Results")
            for r in _records([] if results is None else results):
                for field in ("Vulnerabilities", "Misconfigurations", "Secrets"):
                    values = r.get(field)
                    for item in _records([] if values is None else values):
                        severity = _text(item["Severity"])
                        if field == "Vulnerabilities":
                            items.append(f"{severity}  {_text(item['PkgName'])} {_text(item['InstalledVersion'])}  "
                                         f"{_text(item['VulnerabilityID'])}")
                        elif field == "Misconfigurations":
                            items.append(f"{severity}  {_text(r['Target'])}  {_text(item['ID'])} "
                                         f"{_text(item.get('Title', ''))[:60]}")
                        else:
                            line = item["StartLine"]
                            if type(line) is not int:
                                raise ValueError("expected a line number")
                            items.append(f"{severity}  {_text(r['Target'])}:{line}  {_text(item['RuleID'])}")
        except (ValueError, KeyError, TypeError):
            error = "invalid JSON report or schema"
        scanned("filesystem & IaC (trivy)", rc, (0,), items, "no HIGH/CRITICAL findings",
                f"{len(items)} finding(s)", error)
    else:
        report("filesystem & IaC", True, ui.dim("trivy not installed — brew install trivy"))

    wf = root / ".github" / "workflows"
    if wf.is_dir() and any(wf.glob("*.y*ml")):
        if core.have("zizmor"):
            rc, out, _ = _run(["zizmor", "--offline", "--format", "json", "--min-severity", "medium", "."], root)
            items, error = [], ""
            try:
                for f in _records(json.loads(out)):
                    locations = list(_records(f["locations"]))
                    l0 = locations[0] if locations else {}
                    key = _object(_object(l0.get("symbolic") or {}).get("key") or {}).get("Local") or {}
                    key = _object(key)
                    path = re.sub(r"^(?:\./)+", "", _text(key.get("verbatim_path") or key.get("given_path") or ""))
                    point = _object(_object(l0.get("concrete") or {}).get("location") or {}).get("start_point") or {}
                    row = _object(point).get("row")
                    if row is not None and type(row) is not int:
                        raise ValueError("expected a row number")
                    where = f"{path}:{row + 1}" if path and row is not None else path
                    items.append(f"{_text(_object(f['determinations'])['severity'])}  {where}  "
                                 f"{_text(f['ident'])}: {_text(f.get('desc', ''))[:70]}")
            except (ValueError, KeyError, TypeError):
                error = "invalid JSON report or schema"
            # Zizmor uses 11–14 for findings by severity, 1–3 for execution/collection errors.
            scanned("GitHub Actions (zizmor)", rc, (0, 11, 12, 13, 14), items, "no findings",
                    f"{len(items)} finding(s)", error, (11, 12, 13, 14))
        else:
            report("GitHub Actions", True, ui.dim("zizmor not installed — brew install zizmor"))
    print()
    return 1 if failed else 0
