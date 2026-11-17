#!/usr/bin/env python3
# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Build and scan a qualified source snapshot, retaining raw diagnostics as evidence.

Run with the workflow's digest-pinned Semgrep image. Evidence must live outside
the checkout. No rules are disabled: the gate evaluates the complete JSON report,
including parse failures and fixpoint timeouts that need not affect its exit code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from devkit import pipelines, security  # noqa: E402

ENGINE_VERSION = "1.182.0"
ENGINE_IMAGE = "semgrep/semgrep:1.182.0@sha256:4a34048c04fef049f71665047f074cffb00cc3d89482f965a5cd151d589237af"
NATIVE_SOURCE = "templates/macos/DevKit.m"
NATIVE_WORKFLOW = ".github/workflows/ci.yml"
NATIVE_COMMAND = ["xcrun", "clang", "--analyze", "-fobjc-arc", "-fblocks", "-Werror",
                  "-Xanalyzer", "-analyzer-werror", "-Xanalyzer", "-analyzer-output=text", NATIVE_SOURCE]
NATIVE_RUN = '''native_dir=${DEVKIT_NATIVE_EVIDENCE:-}
if [ -z "$native_dir" ]; then native_dir=$(mktemp -d "${TMPDIR:-/tmp}/devkit-native.XXXXXX"); fi
echo "Native analysis evidence: $native_dir"
python3 scripts/security_corpus.py --native-output "$native_dir/native.json"'''
EXECUTABLE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".zsh",
                       ".yml", ".yaml", ".html", ".c", ".h", ".m", ".cpp", ".go", ".java", ".rb", ".php"}
REVIEWED_RULE = "javascript.lang.security.audit.detect-non-literal-regexp.detect-non-literal-regexp"
# Security review: every literal metacharacter is escaped before alternation;
# only these two constructor spans in this unchanged function prefix qualify.
REVIEWED_HIGHLIGHT = r'''function highlight(el, q, cls) {
  const tk = tokens(q).filter(t => t.length > 1); if (!tk.length) return 0;
  const src = "(" + tk.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")";
  const re = new RegExp(src, "gi"), probe = new RegExp(src, "i");'''
REVIEWED_DIRECTORY = '''@contextmanager
def _directory(path, *, create=False):
    """Walk with no-follow directory handles; never resolve a checked ancestor again."""
    path = _absolute(path)
    pins = _directories()
    base, fd = _open_base(path, pins, create)
    active = "messaging-v2" in base.parts
    try:
        for name in path.relative_to(base).parts:
            active = active or name == "messaging-v2"
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                child = os.open(name, flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise StateBusy("unsafe state path: symbolic link or non-directory") from None
                raise
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid not in (0, os.getuid()) or (active and info.st_uid != os.getuid()):
                raise StateBusy("unsafe state directory: owner mismatch")
            if create and active:
                os.fchmod(fd, 0o700)
        yield fd
    finally:
        os.close(fd)
'''
# Security review: the walk starts from a pinned handle, the state root resolved once and owner-checked, or /;
# every part below it is opened without following links. Both state.py dispositions require this exact text.
REVIEWED_OPEN_BASE = '''_TRUSTED = {}  # devkit's state root as given -> the real folder it resolves to, resolved once per process


def _trusted_root(path):
    """(the state root, its real folder) when `path` is the state root (core.STATE) or inside it, else (None, None).
    The root and what lies above it (a home folder reached through a link, /Users/me -> /Volumes/Data/me) are the
    system's and the user's, so they are resolved once and followed; every part below the root is walked without
    following a link."""
    from . import core
    root = _absolute(core.STATE)
    if path != root and root not in path.parents:
        return None, None
    if str(root) not in _TRUSTED:
        _TRUSTED[str(root)] = Path(os.path.realpath(root))
    return root, _TRUSTED[str(root)]


def _open_base(path, pins, create):
    """(the folder the walk starts from, its handle): a pinned ancestor, else the state root as it resolves, else /."""
    base = next((p for p in (path,) + tuple(path.parents) if p in pins), None)
    if base is not None:
        return base, os.dup(pins[base])
    root, real = _trusted_root(path)
    if root is None:
        base = Path(path.anchor)
        return base, os.open(str(base), os.O_RDONLY | os.O_DIRECTORY)
    if create:
        os.makedirs(real, 0o700, exist_ok=True)
    fd = os.open(str(real), os.O_RDONLY | os.O_DIRECTORY)
    if os.fstat(fd).st_uid not in (0, os.getuid()):
        os.close(fd)
        raise StateBusy("unsafe state directory: owner mismatch")
    return root, fd
'''
REVIEWED_STATE_WRITE = '''def _write(path: Path, doc: dict, indent, durable=False, max_bytes=None):
    text = json.dumps(doc, indent=indent, ensure_ascii=not durable,
                      separators=(",", ":") if durable and indent is None else None)
    if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
        raise StateBusy("capacity: record exceeds the storage limit")
    if durable or "messaging-v2" in path.parts:
        with _directory(path.parent, create=True) as directory:
            _record_info(directory, path.name)
            os.fchmod(directory, 0o700)
'''
REVIEWED_PERMISSION_FIXTURE = '''    def test_canonical_preparation_never_chmods_a_swapped_parent_target(self):
        import os
        import stat
        destination = peers.box_path("claude", CLAUDE_A)
        namespace = peers.PEERS.parent / "messaging-v2"
        namespace.mkdir(mode=0o700)
        outside = self.root / "outside-state"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)
        original = state.secure_path
        swapped = []
        def swap_after_check(path):
            original(path)
            if path == destination and not swapped:
                namespace.rename(self.root / "held-namespace")
                namespace.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
        with patch.object(state, "secure_path", side_effect=swap_after_check):
            with self.assertRaises((state.StateBusy, peers.PeerError)):
                peers._ready(destination)
        self.assertTrue(swapped)
        self.assertEqual(0o755, stat.S_IMODE(outside.stat().st_mode))
        self.assertEqual([], list(outside.iterdir()))
'''
REVIEWED_DISPOSITIONS = (
    {"rule": REVIEWED_RULE, "path": "generated/dashboard.js", "context": REVIEWED_HIGHLIGHT,
     "expressions": ('new RegExp(src, "gi")', 'new RegExp(src, "i")'),
     "reason": "highlight escapes all literal regex metacharacters before constructing alternation"},
    {"rule": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
     "path": "lib/devkit/state.py", "context": REVIEWED_DIRECTORY, "required_contexts": (REVIEWED_OPEN_BASE,),
     "expressions": ("os.fchmod(fd, 0o700)",),
     "reason": "tightens the fstat-validated owned directory descriptor from a no-follow walk to owner-only 0700"},
    {"rule": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
     "path": "lib/devkit/state.py", "context": REVIEWED_STATE_WRITE,
     "required_contexts": (REVIEWED_DIRECTORY, REVIEWED_OPEN_BASE), "expressions": ("os.fchmod(directory, 0o700)",),
     "reason": "tightens the directory descriptor returned by the ownership-checked no-follow walk to 0700"},
    {"rule": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
     "path": "tests/test_messaging.py", "context": REVIEWED_PERMISSION_FIXTURE,
     "required_contexts": ("class PeerIntegrityTests(PeerCase):\n",
                           "from tests.test_peers import PeerCase, CLAUDE_A, CLAUDE_B, sender\n"),
     "expressions": ("os.chmod(outside, 0o755)",),
     "reason": "hermetic swapped-parent fixture sets 0755 under PeerCase's temporary root to verify it stays unchanged"},
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def native_contract(source: Path) -> dict:
    """Declare complementary coverage only when the required macOS gate exists."""
    workflow = source / NATIVE_WORKFLOW
    tree = pipelines.load_yaml(workflow.read_text(encoding="utf-8"))
    job = tree.get("jobs", {}).get("test", {})
    steps = [step for step in job.get("steps", []) if step.get("name") == "Native static analysis"]
    if (not str(job.get("runs-on", "")).startswith("macos-") or job.get("continue-on-error")
            or job.get("if", "") not in ("", "github.event.pull_request.draft == false")
            or len(steps) != 1 or steps[0].get("run", "").strip() != NATIVE_RUN
            or steps[0].get("if") or steps[0].get("continue-on-error")):
        raise ValueError("native analysis requires its exact mandatory macOS workflow gate")
    return {"status": "declaration; requires separate successful macOS CI receipt",
            "source": NATIVE_SOURCE, "source_sha256": sha256(source / NATIVE_SOURCE),
            "workflow": NATIVE_WORKFLOW, "workflow_sha256": sha256(workflow),
            "job": "test", "step": "Native static analysis", "artifact": "native-analysis-<python-version>",
            "command": NATIVE_COMMAND,
            "command_sha256": hashlib.sha256(json.dumps(NATIVE_COMMAND, separators=(",", ":")).encode()).hexdigest()}


def native_receipt_matches(receipt: dict, contract: dict) -> bool:
    return (receipt.get("passed") is True and receipt.get("returncode") == 0
            and bool(receipt.get("tool_version")) and all(receipt.get(key) == contract[key] for key in (
                "source", "source_sha256", "workflow", "workflow_sha256", "command", "command_sha256")))


def native_analysis(source: Path, output: Path) -> int:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("native analysis evidence must be outside the source checkout")
    contract = native_contract(source)
    version = subprocess.run(["xcrun", "clang", "--version"], cwd=source, check=True,
                             capture_output=True, text=True, timeout=30).stdout.strip()
    result = subprocess.run(NATIVE_COMMAND, cwd=source, capture_output=True, text=True, timeout=120)
    receipt = dict(contract, status="executed native analysis", tool_version=version,
                   returncode=result.returncode, passed=result.returncode == 0,
                   stdout=result.stdout, stderr=result.stderr)
    receipt["passed"] = native_receipt_matches(receipt, native_contract(source))
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json(output, receipt)
    print(json.dumps({"native_analysis_passed": receipt["passed"], "receipt": str(output)}))
    return 0 if receipt["passed"] else 1


def projects() -> dict:
    """Exercise every shipped pipeline template through the production generator."""
    py = {"pyproject.toml": '[project]\nname="fixture"\nversion="0.1.0"\nrequires-python=">=3.11"\n',
          "tests/test_example.py": "def test_example():\n    assert sum([1, 2]) == 3\n"}
    node = {"package.json": json.dumps({"name": "fixture", "scripts": {
        "test": "vitest run", "build": "tsc", "lint": "eslint .", "typecheck": "tsc --noEmit"}}),
        "pnpm-lock.yaml": "lockfileVersion: 9\n", ".nvmrc": "22\n"}
    stacks = {
        "python-uv": dict(py, **{"uv.lock": ""}),
        "python-pip": dict(py, **{"requirements.txt": "pytest==8.3.5\n"}),
        "python-poetry": dict(py, **{"poetry.lock": ""}),
        "node": node,
        "java-maven": {"pom.xml": "<project><properties><java.version>21</java.version></properties>"
                       "<artifactId>testcontainers</artifactId></project>", "mvnw": "#!/bin/sh\n",
                       "src/test/java/ExampleTest.java": ""},
        "java-gradle": {"build.gradle.kts": "plugins { checkstyle }\n", "gradlew": "#!/bin/sh\n",
                        "src/test/java/ExampleTest.java": ""},
        "go": {"go.mod": "module example.test/fixture\n\ngo 1.25\n", "go.sum": "",
               "main.go": "package main\nfunc main() {}\n", "main_test.go": "package main\n"},
        "php": {"composer.json": json.dumps({"require-dev": {"phpunit/phpunit": "11", "phpstan/phpstan": "2"}}),
                "composer.lock": "{}", "tests/ExampleTest.php": ""},
    }
    stacks["docker"] = dict(node, **{"Dockerfile": "FROM node:24\n"})
    stacks["mixed"] = dict(stacks["python-uv"], **stacks["docker"], **{
        "compose.test.yml": "services:\n  test:\n    build: .\n    command: echo fixture\n"})
    return stacks


def render_pipelines(templates: Path, output: Path) -> dict:
    """Map template reads to the generated file set for each stack/provider pair."""
    output.mkdir(parents=True, exist_ok=True)
    inventory = {str(p.relative_to(templates)) for p in templates.rglob("*")
                 if p.suffix in (".yml", ".yaml")}
    seen, replacements = set(), {}
    previous_templates, previous_tpl = pipelines.TEMPLATES, pipelines._tpl

    def tracked(name):
        seen.add(name)
        return (templates / name).read_text(encoding="utf-8")

    pipelines.TEMPLATES, pipelines._tpl = templates, tracked
    try:
        with tempfile.TemporaryDirectory(prefix="inputs-", dir=output) as scratch:
            for stack, files in projects().items():
                project = Path(scratch) / stack
                for name, content in files.items():
                    path = project / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                for provider in pipelines.PROVIDERS:
                    seen.clear()
                    generated, _ = pipelines.generate(project, provider,
                        policy={"tag_format": "vYY.MM.PATCH"}, branch="main")
                    artifacts = []
                    for name, content in generated.items():
                        if "@{" in content:
                            raise ValueError(f"unresolved template token in {stack}/{provider}/{name}")
                        pipelines.load_yaml(content)
                        relative = f"generated/pipelines/{stack}/{provider}/{name}"
                        path = output / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(content, encoding="utf-8")
                        artifacts.append(relative)
                    for name in seen:
                        replacements.setdefault(name, []).extend(artifacts)
        missing = inventory - replacements.keys()
        if missing:
            raise ValueError("uncovered pipeline templates: " + ", ".join(sorted(missing)))
        if replacements.keys() - inventory:
            raise ValueError("generator read a template outside the YAML inventory")
        return {"templates/ci/" + name: {"artifacts": sorted(set(paths)),
                "reason": "production pipelines.generate dependency; stack/provider output set"}
                for name, paths in sorted(replacements.items())}
    finally:
        pipelines.TEMPLATES, pipelines._tpl = previous_templates, previous_tpl


def source_files(source: Path) -> list:
    result = subprocess.run(["git", "-c", f"safe.directory={source}", "ls-files", "--cached", "--others",
                             "--exclude-standard", "-z"],
                            cwd=source, check=True, capture_output=True)
    names = sorted(set(result.stdout.decode().split("\0")) - {""})
    files = []
    for name in names:
        path = source / name
        if path.is_symlink():
            raise ValueError(f"source symlink needs an explicit scan policy: {name}")
        if path.is_file():
            files.append(name)
    return files


def build(source: Path, output: Path) -> dict:
    source, output = source.resolve(), output.resolve()
    if output == source or source in output.parents:
        raise ValueError("security evidence must be outside the source checkout")
    output.mkdir(parents=True, exist_ok=True)
    qualified, raw = output / "qualified", output / "raw"
    qualified.mkdir()
    raw.mkdir()
    files = source_files(source)
    hashes = {name: sha256(source / name) for name in files}
    native = native_contract(source)
    if NATIVE_SOURCE not in hashes:
        raise ValueError("native source is missing from the git source inventory")
    replacements = render_pipelines(source / "templates/ci", qualified)
    web = source / "lib/devkit/web"
    sections = sorted((web / "sections").glob("*.js"))
    parts = [web / "app.js", *sections, web / "boot.js"]
    # Keep the production dashboard.JS concatenation byte-for-byte; the test
    # compares the result with that production constant to catch order drift.
    script = parts[0].read_text(encoding="utf-8") + "\n".join(
        p.read_text(encoding="utf-8") for p in sections) + parts[-1].read_text(encoding="utf-8")
    (qualified / "generated/dashboard.js").write_text(script, encoding="utf-8")
    for part in parts:
        replacements[str(part.relative_to(source))] = {"artifacts": ["generated/dashboard.js"],
                                                       "reason": "production dashboard.JS ordered concatenation"}
    if replacements.keys() - hashes.keys():
        raise ValueError("replacement source missing from git source inventory")
    if any(name.startswith("generated/") for name in files):
        raise ValueError("source collides with the generated artifact namespace")
    for name in files:
        target = (raw if name in replacements else qualified) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    # An explicit empty file disables Semgrep's built-in test/ and tests/ skips.
    # The qualified tree retains the repository's own documented ignore policy.
    (raw / ".semgrepignore").write_text("", encoding="utf-8")
    if not (qualified / ".semgrepignore").is_file():
        raise ValueError("qualified scan requires the repository's explicit .semgrepignore")
    artifacts = {str(p.relative_to(qualified)): sha256(p)
                 for p in sorted((qualified / "generated").rglob("*")) if p.is_file()}
    manifest = {"schema": 1, "engine": {"version": ENGINE_VERSION, "image": ENGINE_IMAGE},
                "rules": {"config": "p/default", "identity": "mutable registry pack; observed rule IDs in scan reports"},
                "sources": hashes, "artifacts": artifacts, "replacements": replacements,
                "dashboard_order": [str(p.relative_to(source)) for p in parts],
                "qualified_sources": [n for n in files if n not in replacements],
                "complementary_coverage": {NATIVE_SOURCE: native},
                "required_sources": [n for n in files if n not in replacements and n != NATIVE_SOURCE and (
                    Path(n).suffix in EXECUTABLE_SUFFIXES or (source / n).read_bytes().startswith(b"#!"))],
                "passed": False}
    write_json(output / "manifest.json", manifest)
    return manifest


def scan_errors(report: dict, replacements=None) -> list:
    """Only raw syntax errors for an explicitly mapped input are expected."""
    replacements = replacements or {}
    errors = []
    for error in report.get("errors", []):
        path = error.get("path", "").removeprefix("./")
        kind = error.get("type")
        syntax = kind in ("Syntax error", "Other syntax error", "Partial parsing")
        if isinstance(kind, list) and len(kind) == 2 and kind[0] == "PartialParsing":
            syntax = bool(kind[1]) and all(location.get("path", "").removeprefix("./") in replacements
                                          for location in kind[1])
        if syntax and path in replacements:
            continue
        errors.append(error)
    errors.extend(report.get("time", {}).get("fixpoint_timeouts", []))
    errors.extend(report.get("time", {}).get("timeouts", []))
    errors.extend(report.get("skipped_rules", []))
    return errors


def finding_disposition(finding: dict, qualified: Path):
    """A reviewed rule/path alone grants nothing: context and exact span must match."""
    start, end = finding.get("start", {}), finding.get("end", {})
    for review in REVIEWED_DISPOSITIONS:
        if finding.get("check_id") != review["rule"] or finding.get("path") != review["path"]:
            continue
        text = (qualified / review["path"]).read_text(encoding="utf-8")
        context = review["context"]
        required = review.get("required_contexts", ())
        if text.count(context) != 1 or any(text.count(dependency) != 1 for dependency in required):
            continue
        for expression in review["expressions"]:
            offset = text.index(context) + context.index(expression)
            line = text[:offset].count("\n") + 1
            col = offset - text.rfind("\n", 0, offset)
            if (start.get("line"), start.get("col"), end.get("line"), end.get("col")) == (
                    line, col, line, col + len(expression)):
                return {"check_id": review["rule"], "path": review["path"], "start": start, "end": end,
                        "expression": expression, "reason": review["reason"],
                        "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                        "required_context_sha256": [hashlib.sha256(dependency.encode()).hexdigest()
                                                    for dependency in required]}
    return None


def reviewed_finding(finding: dict, qualified: Path) -> bool:
    return finding_disposition(finding, qualified) is not None


def evaluate(report: dict, returncode: int, qualified: Path) -> dict:
    errors = scan_errors(report)
    reviewed, findings, dispositions = [], [], []
    for finding in report.get("results", []):
        disposition = finding_disposition(finding, qualified)
        if disposition:
            reviewed.append(finding)
            dispositions.append(disposition)
        else:
            findings.append(finding)
    complete = all(key in report for key in ("results", "errors", "paths")) and bool(
        report.get("paths", {}).get("scanned")) and report.get("version") == ENGINE_VERSION
    exit_consistent = returncode == 0 or (returncode == 1 and bool(reviewed or findings))
    return {"passed": complete and exit_consistent and not errors and not findings,
            "complete": complete and exit_consistent and not errors,
            "exit_consistent": exit_consistent, "errors": errors, "findings": findings,
            "reviewed_findings": reviewed, "dispositions": dispositions}


def evaluate_raw(report: dict, returncode: int, replacements: dict) -> dict:
    errors = scan_errors(report, replacements)
    expected = [error for error in report.get("errors", []) if error not in errors]
    # --error maps findings to 1. A syntax failure (2/3) needs a matching
    # diagnostic for a mapped input; an unexplained nonzero exit never passes.
    exit_consistent = (returncode == 0 or (returncode == 1 and bool(report.get("results")))
                       or (returncode in (2, 3) and any(error.get("code") == returncode for error in expected)))
    complete = all(key in report for key in ("results", "errors", "paths")) and bool(
        report.get("paths", {}).get("scanned")) and report.get("version") == ENGINE_VERSION
    return {"passed": complete and exit_consistent and not errors, "exit_consistent": exit_consistent,
            "unexpected_errors": errors,
            "expected_errors": [dict(error, replacements=replacements[error["path"].removeprefix("./")])
                                for error in expected]}


def coverage_gaps(manifest: dict, report: dict) -> list:
    scanned = {p.removeprefix("./") for p in report.get("paths", {}).get("scanned", [])}
    return sorted((manifest["artifacts"].keys() | set(manifest["required_sources"])) - scanned)


def scan(tree: Path, output: Path, name: str) -> tuple:
    # `dev sec scan` and this gate share one Semgrep command (security.semgrep_command): serial, with no per-rule
    # wall-clock timeout, and package rules left out only where no file imports their packages.
    command = security.semgrep_command(tree) + ["--error", "--max-target-bytes", "0", "--no-git-ignore", "."]
    with (output / f"{name}.json").open("w") as stdout, (output / f"{name}.log").open("w") as stderr:
        result = subprocess.run(command, cwd=tree, stdout=stdout, stderr=stderr, timeout=2700)
    report = json.loads((output / f"{name}.json").read_text(encoding="utf-8"))
    return report, {"command": command, "cwd": str(tree), "returncode": result.returncode,
                    "left_out_rules": [{"rule": rule, "packages_not_imported": list(packages)}
                                       for rule, packages in security.semgrep_left_out(command)],
                    "report_sha256": sha256(output / f"{name}.json")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--native-output", type=Path, help="run the separate required macOS native analyzer")
    parser.add_argument("--render-only", action="store_true", help="generate evidence inputs without claiming a scan")
    args = parser.parse_args(argv)
    try:
        if args.native_output:
            if args.render_only:
                raise ValueError("--render-only does not apply to native analysis")
            return native_analysis(ROOT, args.native_output)
        output = args.output.resolve()
        manifest = build(ROOT, output)
        if args.render_only:
            print(f"Rendered {len(manifest['artifacts'])} artifacts; manifest: {output / 'manifest.json'}")
            return 0
        version = subprocess.run(["semgrep", "--version"], check=True, capture_output=True, text=True).stdout.strip()
        manifest["engine"]["observed_version"] = version
        manifest["engine"]["version_command"] = ["semgrep", "--version"]
        write_json(output / "manifest.json", manifest)
        if version != ENGINE_VERSION:
            raise ValueError(f"expected Semgrep {ENGINE_VERSION}; got {version}")
        raw, raw_run = scan(output / "raw", output, "raw")
        raw_verdict = evaluate_raw(raw, raw_run["returncode"], manifest["replacements"])
        manifest["raw_scan"] = dict(raw_run, **raw_verdict)
        write_json(output / "manifest.json", manifest)
        report, run = scan(output / "qualified", output, "qualified")
        verdict = evaluate(report, run["returncode"], output / "qualified")
        missing = coverage_gaps(manifest, report)
        changed = [name for name, digest in manifest["sources"].items()
                   if not (ROOT / name).is_file() or sha256(ROOT / name) != digest]
        added = sorted(set(source_files(ROOT)) - manifest["sources"].keys())
        manifest["qualified_scan"] = dict(run, **verdict, unscanned_sources_or_artifacts=missing,
                                         changed_sources=changed, added_sources=added)
        manifest["passed"] = bool(verdict["passed"] and raw_verdict["passed"] and not missing
                                  and not changed and not added)
        write_json(output / "manifest.json", manifest)
        print(json.dumps({"passed": manifest["passed"], "manifest": str(output / "manifest.json"),
                          "findings": len(verdict["findings"]), "reviewed": len(verdict["reviewed_findings"]),
                          "errors": len(verdict["errors"]) + len(raw_verdict["unexpected_errors"]),
                          "unscanned_sources_or_artifacts": missing}))
        return 0 if manifest["passed"] else 1
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Security corpus incomplete: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
