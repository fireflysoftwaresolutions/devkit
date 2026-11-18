# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The security gate rejects incomplete scans and accounts for generated sources."""
import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/security_corpus.py"


class SecurityCorpusTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "the qualified security gate is missing")
        spec = importlib.util.spec_from_file_location("security_corpus", SCRIPT)
        self.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.gate)
        self.tmp = tempfile.TemporaryDirectory(prefix="devkit-security-test-")
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "evidence"

    def test_every_template_has_hashed_generated_replacements_and_tests_are_kept(self):
        manifest = self.gate.build(ROOT, self.out)
        templates = {str(p.relative_to(ROOT)) for p in (ROOT / "templates/ci").rglob("*.yml")}
        self.assertEqual(len(templates), 49)
        self.assertEqual(templates, {p for p in manifest["replacements"] if p.startswith("templates/")})
        for source in templates:
            self.assertTrue(manifest["replacements"][source]["artifacts"], source)
            self.assertEqual(manifest["sources"][source], self.gate.sha256(ROOT / source))
        self.assertTrue((self.out / "qualified/tests/test_security_corpus.py").is_file())
        self.assertFalse((self.out / "qualified/templates/ci/github/job.yml").exists())
        native = manifest["complementary_coverage"]["templates/macos/DevKit.m"]
        self.assertEqual(native["status"], "declaration; requires separate successful macOS CI receipt")
        self.assertEqual(native["source_sha256"], self.gate.sha256(ROOT / "templates/macos/DevKit.m"))
        self.assertNotIn("templates/macos/DevKit.m", manifest["required_sources"])
        self.assertTrue((self.out / "qualified/templates/macos/DevKit.m").is_file())
        for artifact, digest in manifest["artifacts"].items():
            self.assertEqual(digest, self.gate.sha256(self.out / "qualified" / artifact))
        sys.path.insert(0, str(ROOT / "lib"))
        from devkit import dashboard
        self.assertEqual((self.out / "qualified/generated/dashboard.js").read_text(), dashboard.JS)

    def test_local_ci_planner_runs_the_qualified_scanner(self):
        from devkit import gitx
        jobs = gitx.plan(str(ROOT))
        job = next(job for job in jobs if job["id"] == "security" and job["file"] == "ci.yml")
        scanner = next(step for step in job["steps"] if step["name"] == "Code (semgrep)")
        self.assertIsNone(job["skip"])
        self.assertIsNone(scanner["skip"])
        self.assertFalse(scanner["blocking"])
        self.assertIn(self.gate.ENGINE_IMAGE, scanner["run"])
        self.assertIn("python3 scripts/security_corpus.py --output /evidence", scanner["run"])
        native = next(step for job in jobs for step in job["steps"] if step["name"] == "Native static analysis")
        self.assertIsNone(native["skip"])
        self.assertFalse(native["blocking"])

    def test_native_receipt_binds_success_to_source_workflow_and_command(self):
        receipt_path = Path(self.tmp.name) / "native.json"
        with mock.patch.object(self.gate.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, "Apple clang test\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]):
            self.assertEqual(self.gate.native_analysis(ROOT, receipt_path), 0)
        receipt = json.loads(receipt_path.read_text())
        contract = self.gate.native_contract(ROOT)
        self.assertTrue(self.gate.native_receipt_matches(receipt, contract))
        self.assertEqual(receipt["source_sha256"], self.gate.sha256(ROOT / "templates/macos/DevKit.m"))
        self.assertEqual(receipt["workflow_sha256"], self.gate.sha256(ROOT / ".github/workflows/ci.yml"))
        for key, value in (("source_sha256", "changed"), ("workflow_sha256", "changed"),
                           ("command_sha256", "changed"), ("returncode", 1), ("passed", False)):
            self.assertFalse(self.gate.native_receipt_matches(dict(receipt, **{key: value}), contract), key)
        with mock.patch.object(self.gate.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, "Apple clang test\n", ""),
            subprocess.CompletedProcess([], 1, "", "analyzer finding"),
        ]):
            self.assertEqual(self.gate.native_analysis(ROOT, receipt_path), 1)
        self.assertFalse(json.loads(receipt_path.read_text())["passed"])

    def test_native_coverage_requires_the_exact_required_workflow_step_and_source(self):
        source = Path(self.tmp.name) / "native-source"
        (source / ".github/workflows").mkdir(parents=True)
        (source / "templates/macos").mkdir(parents=True)
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        native = source / "templates/macos/DevKit.m"
        native.write_text((ROOT / "templates/macos/DevKit.m").read_text())
        for changed in (workflow.replace("Native static analysis", "Removed native analysis"),
                        workflow.replace("--native-output", "--render-only"),
                        workflow.replace("runs-on: macos-latest", "runs-on: ubuntu-24.04")):
            (source / ".github/workflows/ci.yml").write_text(changed)
            with self.assertRaisesRegex(ValueError, "native.*gate"):
                self.gate.native_contract(source)
        (source / ".github/workflows/ci.yml").write_text(workflow)
        native.unlink()
        with self.assertRaises(FileNotFoundError):
            self.gate.native_contract(source)

    def test_an_uncovered_template_fails_closed(self):
        templates = Path(self.tmp.name) / "templates"
        shutil.copytree(ROOT / "templates/ci", templates)
        (templates / "github/new-job.yml").write_text("new: job\n")
        with self.assertRaisesRegex(ValueError, "uncovered.*new-job"):
            self.gate.render_pipelines(templates, self.out)

    def test_missing_required_template_and_unresolved_token_fail_closed(self):
        templates = Path(self.tmp.name) / "templates"
        shutil.copytree(ROOT / "templates/ci", templates)
        missing = templates / "github/setup/python-poetry.yml"
        missing.unlink()
        with self.assertRaises(FileNotFoundError):
            self.gate.render_pipelines(templates, self.out / "missing")
        shutil.copy(ROOT / "templates/ci/github/setup/python-poetry.yml", missing)
        with (templates / "github/pipeline.yml").open("a") as stream:
            stream.write("\n# @{unresolved:token}\n")
        with self.assertRaisesRegex(ValueError, "unresolved"):
            self.gate.render_pipelines(templates, self.out / "unresolved")

    def test_unmapped_parse_errors_and_all_timeouts_fail(self):
        mapped = {"templates/ci/github/job.yml": {"artifacts": ["generated/pipeline.yml"]}}
        parse = {"type": "Syntax error", "path": "templates/ci/github/job.yml"}
        self.assertEqual(self.gate.scan_errors({"errors": [parse]}, mapped), [])
        partial = dict(parse, type=["PartialParsing", [{"path": parse["path"]}]])
        self.assertEqual(self.gate.scan_errors({"errors": [partial]}, mapped), [])
        nested_unmapped = dict(parse, type=["PartialParsing", [{"path": "tests/broken.py"}]])
        self.assertEqual(self.gate.scan_errors({"errors": [nested_unmapped]}, mapped), [nested_unmapped])
        self.assertEqual(self.gate.scan_errors({"errors": [dict(parse, path="tests/broken.py")]}, mapped)[0]["path"], "tests/broken.py")
        for error in (dict(parse, type="Timeout"), dict(parse, type="Out of memory")):
            self.assertEqual(self.gate.scan_errors({"errors": [error]}, mapped), [error])
        timeout = {"message": "Fixpoint timeout", "location": {"path": "generated/dashboard.js"}}
        self.assertEqual(self.gate.scan_errors({"errors": [], "time": {"fixpoint_timeouts": [timeout]}}), [timeout])

    def test_findings_fail_even_when_process_reports_success(self):
        result = {"check_id": "new.rule", "path": "tests/security.py", "start": {"line": 1}, "end": {"line": 1}}
        report = {"version": "1.182.0", "results": [result], "errors": [], "paths": {"scanned": ["tests/security.py"]}}
        verdict = self.gate.evaluate(report, 0, self.out)
        self.assertFalse(verdict["passed"])
        self.assertEqual(verdict["findings"], [result])
        report["results"] = []
        self.assertTrue(self.gate.evaluate(report, 0, self.out)["passed"])
        for status in (1, 2, 3, 124):
            self.assertFalse(self.gate.evaluate(report, status, self.out)["passed"])
        report["version"] = "1.183.0"
        self.assertFalse(self.gate.evaluate(report, 0, self.out)["passed"])

    def test_missing_report_fields_and_zero_targets_are_incomplete(self):
        for report in ({}, {"version": "1.182.0", "results": [], "errors": [], "paths": {"scanned": []}}):
            self.assertFalse(self.gate.evaluate(report, 0, self.out)["passed"])

    def test_silently_skipped_test_source_fails_coverage(self):
        manifest = {"artifacts": {"generated/dashboard.js": "hash"},
                    "required_sources": ["tests/test_security.py", "bin/dev"]}
        report = {"paths": {"scanned": ["./generated/dashboard.js", "bin/dev"]}}
        self.assertEqual(self.gate.coverage_gaps(manifest, report), ["tests/test_security.py"])
        report["paths"]["scanned"].append("tests/test_security.py")
        self.assertEqual(self.gate.coverage_gaps(manifest, report), [])

    def test_reviewed_regex_disposition_is_bound_to_exact_source_and_span(self):
        target = self.out / "generated/dashboard.js"
        target.parent.mkdir(parents=True)
        target.write_text(self.gate.REVIEWED_HIGHLIGHT)
        line = self.gate.REVIEWED_HIGHLIGHT.splitlines()[3]
        start = line.index("new RegExp(src, \"gi\")")
        finding = {"check_id": self.gate.REVIEWED_RULE, "path": "generated/dashboard.js",
                   "start": {"line": 4, "col": start + 1},
                   "end": {"line": 4, "col": start + 1 + len('new RegExp(src, "gi")')}}
        self.assertTrue(self.gate.reviewed_finding(finding, self.out))
        report = {"version": "1.182.0", "results": [finding], "errors": [],
                  "paths": {"scanned": ["generated/dashboard.js"]}}
        self.assertTrue(self.gate.evaluate(report, 1, self.out)["passed"])
        report["results"].append(dict(finding, check_id="another.rule"))
        self.assertFalse(self.gate.evaluate(report, 1, self.out)["passed"])
        self.assertFalse(self.gate.reviewed_finding(dict(finding, path="tests/other.js"), self.out))
        self.assertFalse(self.gate.reviewed_finding(dict(finding, check_id="another.rule"), self.out))
        target.write_text(self.gate.REVIEWED_HIGHLIGHT.replace('tk.map(t => t.replace', 'tk.map(t => t.other'))
        self.assertFalse(self.gate.reviewed_finding(finding, self.out))

    def assert_python_disposition(self, path, rule, context, expression, changed_expression, context_mutations=()):
        target = self.out / path
        target.parent.mkdir(parents=True)
        target.write_text(context)
        offset = context.index(expression)
        line = context[:offset].count("\n") + 1
        col = offset - context.rfind("\n", 0, offset)
        finding = {"check_id": rule, "path": path, "start": {"line": line, "col": col},
                   "end": {"line": line, "col": col + len(expression)}}
        self.assertTrue(self.gate.reviewed_finding(finding, self.out))
        report = {"version": "1.182.0", "results": [finding], "errors": [], "paths": {"scanned": [path]}}
        verdict = self.gate.evaluate(report, 1, self.out)
        self.assertTrue(verdict["passed"])
        self.assertTrue(verdict["dispositions"][0]["reason"])
        self.assertEqual(verdict["dispositions"][0]["expression"], expression)
        production = (ROOT / path).read_text()
        production_offset = production.index(expression)
        production_line = production[:production_offset].count("\n") + 1
        production_col = production_offset - production.rfind("\n", 0, production_offset)
        production_finding = dict(finding, start={"line": production_line, "col": production_col},
                                  end={"line": production_line, "col": production_col + len(expression)})
        self.assertTrue(self.gate.reviewed_finding(production_finding, ROOT))
        self.assertFalse(self.gate.reviewed_finding(dict(finding, check_id="another.rule"), self.out))
        self.assertFalse(self.gate.reviewed_finding(dict(finding, path="tests/another.py"), self.out))
        target.write_text(context + "\n" + expression + "\n")
        other_line = context.count("\n") + 2
        other_call = dict(finding, start={"line": other_line, "col": 1},
                          end={"line": other_line, "col": len(expression) + 1})
        self.assertFalse(self.gate.reviewed_finding(other_call, self.out))
        target.write_text(context.replace(expression, changed_expression))
        self.assertFalse(self.gate.reviewed_finding(finding, self.out))
        target.write_text(context.replace("def ", "def other_", 1))
        self.assertFalse(self.gate.reviewed_finding(finding, self.out))
        for before, after in context_mutations:
            target.write_text(context.replace(before, after))
            self.assertFalse(self.gate.reviewed_finding(finding, self.out))

    def test_swapped_parent_fixture_disposition_requires_exact_hermetic_test(self):
        source = (ROOT / "tests/test_messaging.py").read_text()
        start = source.index("    def test_canonical_preparation_never_chmods_a_swapped_parent_target(self):")
        end = source.index("\n    def test_replay_repairs_missing_notification_marker", start)
        context = ("from tests.test_peers import PeerCase, CLAUDE_A, CLAUDE_B, sender\n\n"
                   "class PeerIntegrityTests(PeerCase):\n" + source[start:end])
        self.assert_python_disposition("tests/test_messaging.py",
            "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
            context, "os.chmod(outside, 0o755)", "os.chmod(outside, 0o777)", context_mutations=(
                ('outside = self.root / "outside-state"', 'outside = self.root.parent / "outside-state"'),
                ("os.chmod(outside, 0o755)", "os.chmod(destination, 0o755)"),
                ("self.assertEqual(0o755, stat.S_IMODE(outside.stat().st_mode))", "self.assertTrue(True)"),
                ("class PeerIntegrityTests(PeerCase):", "class PeerIntegrityTests(unittest.TestCase):"),
            ))

    # the walk's starting handle (a pin, the owner-checked state root, or /) is part of what the review covers
    OPEN_BASE_MUTATIONS = (("if os.fstat(fd).st_uid not in (0, os.getuid()):", "if False:"),
                           ("fd = os.open(str(real), os.O_RDONLY | os.O_DIRECTORY)",
                            "fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY)"))

    def test_directory_disposition_requires_owned_descriptor_and_restrictive_mode(self):
        self.assert_python_disposition("lib/devkit/state.py",
            "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
            self.gate.REVIEWED_OPEN_BASE + "\n\n" + self.gate.REVIEWED_DIRECTORY,
            "os.fchmod(fd, 0o700)", "os.fchmod(fd, 0o777)", context_mutations=self.OPEN_BASE_MUTATIONS)

    def test_write_disposition_requires_checked_directory_descriptor_and_restrictive_mode(self):
        self.assert_python_disposition("lib/devkit/state.py",
            "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
            self.gate.REVIEWED_OPEN_BASE + "\n\n" + self.gate.REVIEWED_DIRECTORY + "\n\n"
            + self.gate.REVIEWED_STATE_WRITE, "os.fchmod(directory, 0o700)", "os.fchmod(directory, 0o777)",
            context_mutations=self.OPEN_BASE_MUTATIONS)

    def test_descriptor_dispositions_reject_changed_owner_validation(self):
        target = self.out / "lib/devkit/state.py"
        target.parent.mkdir(parents=True)
        original = (ROOT / "lib/devkit/state.py").read_text()
        for expression in ("os.fchmod(fd, 0o700)", "os.fchmod(directory, 0o700)"):
            offset = original.index(expression)
            line = original[:offset].count("\n") + 1
            col = offset - original.rfind("\n", 0, offset)
            finding = {"check_id": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
                       "path": "lib/devkit/state.py", "start": {"line": line, "col": col},
                       "end": {"line": line, "col": col + len(expression)}}
            target.write_text(original)
            self.assertTrue(self.gate.reviewed_finding(finding, self.out))
            target.write_text(original.replace("info.st_uid not in (0, os.getuid())", "False"))
            self.assertFalse(self.gate.reviewed_finding(finding, self.out))

    def test_obsolete_path_chmod_has_no_disposition(self):
        target = self.out / "lib/devkit/state.py"
        target.parent.mkdir(parents=True)
        expression = "os.chmod(directory, 0o700)"
        target.write_text("def private_directories(path):\n    " + expression + "\n")
        finding = {"check_id": "python.lang.security.audit.insecure-file-permissions.insecure-file-permissions",
                   "path": "lib/devkit/state.py", "start": {"line": 2, "col": 5},
                   "end": {"line": 2, "col": 5 + len(expression)}}
        self.assertFalse(self.gate.reviewed_finding(finding, self.out))

    def test_a_span_across_lines_matches_only_where_its_expression_ends(self):
        target = self.out / "tests/page.html"
        target.parent.mkdir(parents=True)
        expression = '<form method="post">\n    <button>Save</button>\n  </form>'
        context = "<main>\n  " + expression + "\n</main>\n"
        target.write_text(context)
        review = {"rule": "html.form", "path": "tests/page.html", "context": context, "expressions": (expression,),
                  "reason": "a fixture form"}
        finding = {"check_id": "html.form", "path": "tests/page.html", "start": {"line": 2, "col": 3},
                   "end": {"line": 4, "col": 10}}
        with mock.patch.object(self.gate, "REVIEWED_DISPOSITIONS", (review,)):
            self.assertTrue(self.gate.reviewed_finding(finding, self.out))
            # the end a single-line span would have, and an end one line short, are other spans
            self.assertFalse(self.gate.reviewed_finding(
                dict(finding, end={"line": 2, "col": 3 + len(expression)}), self.out))
            self.assertFalse(self.gate.reviewed_finding(dict(finding, end={"line": 3, "col": 10}), self.out))

    def test_visual_folder_mode_disposition_requires_the_owner_only_folder_it_creates(self):
        review = next(r for r in self.gate.REVIEWED_DISPOSITIONS if r["path"] == "lib/devkit/visual/compare.py")
        self.assert_python_disposition("lib/devkit/visual/compare.py", self.gate.FILE_MODE_RULE, review["context"],
            "os.chmod(path, 0o700)", "os.chmod(path, 0o777)", context_mutations=(
                ("path.mkdir(mode=0o700)", "path.mkdir(mode=0o777)"),))

    def test_every_reviewed_disposition_matches_the_source_it_was_reviewed_on(self):
        # a change to reviewed code drops its disposition, so the gate fails until someone reviews the new code
        for review in self.gate.REVIEWED_DISPOSITIONS:
            if review["path"].startswith("generated/"):
                continue
            with self.subTest(path=review["path"], expressions=review["expressions"]):
                text = (ROOT / review["path"]).read_text(encoding="utf-8")
                self.assertEqual(1, text.count(review["context"]))
                for dependency in review.get("required_contexts", ()):
                    self.assertEqual(1, text.count(dependency))
                for expression in review["expressions"]:
                    self.assertIn(expression, review["context"])
                self.assertTrue(review["reason"].strip())

    def test_cli_persists_raw_replacement_evidence_and_rejects_incomplete_scans(self):
        # A process boundary stand-in exercises orchestration, JSON and exits;
        # it is deliberately not evidence that the real Semgrep engine passed.
        scanner = Path(self.tmp.name) / "semgrep"
        scanner.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys
from pathlib import Path
if "--version" in sys.argv:
    print("1.182.0")
    sys.exit(0)
paths = sorted(str(p) for p in Path(".").rglob("*") if p.is_file())
report = {"version": "1.182.0", "results": [], "errors": [], "paths": {"scanned": paths}}
case = os.environ.get("CORPUS_TEST_CASE", "complete")
status = 0
if Path.cwd().name == "raw":
    report["errors"].append({"code": 3, "type": "Syntax error", "path": "templates/ci/github/job.yml"})
    if case.startswith("raw-empty-"):
        report["errors"] = []
        status = int(case[-1])
    elif case.startswith("raw-mapped-"):
        status = int(case[-1])
        report["errors"][0]["code"] = status
    elif case == "raw-mismatched-code":
        status = 2
else:
    # the engine's taint budget reads the process CPU time its parallel workers share: only a serial scan is stable
    serial = "--jobs" in sys.argv and sys.argv[sys.argv.index("--jobs") + 1] == "1"
    if case == "fixpoint" or not serial:
        report["time"] = {"fixpoint_timeouts": [{"message": "Fixpoint timeout"}]}
    elif case == "test-skipped":
        report["paths"]["scanned"].remove("tests/test_security_corpus.py")
    elif case == "parse-error":
        report["errors"].append({"type": "Syntax error", "path": "tests/test_security_corpus.py"})
    elif case == "finding":
        report["results"].append({"check_id": "security.test", "path": "tests/test_security_corpus.py"})
print(json.dumps(report))
sys.exit(status)
''')
        scanner.chmod(0o700)
        source = Path(self.tmp.name) / "source"
        shutil.copytree(ROOT / "templates/ci", source / "templates/ci")
        shutil.copytree(ROOT / "lib/devkit/web", source / "lib/devkit/web")
        shutil.copy(ROOT / ".semgrepignore", source / ".semgrepignore")
        (source / ".github/workflows").mkdir(parents=True)
        shutil.copy(ROOT / ".github/workflows/ci.yml", source / ".github/workflows/ci.yml")
        (source / "templates/macos").mkdir()
        shutil.copy(ROOT / "templates/macos/DevKit.m", source / "templates/macos/DevKit.m")
        (source / "tests").mkdir()
        (source / "tests/test_security_corpus.py").write_text("def check():\n    return 1\n")
        subprocess.run(["git", "init", "--quiet", str(source)], check=True)
        accepted = {"complete", "raw-mapped-2", "raw-mapped-3"}
        for case in ("complete", "fixpoint", "test-skipped", "parse-error", "finding",
                     "raw-empty-1", "raw-empty-2", "raw-empty-3", "raw-mapped-2", "raw-mapped-3",
                     "raw-mismatched-code"):
            with self.subTest(case=case), mock.patch.dict(os.environ, {
                "PATH": self.tmp.name + os.pathsep + os.environ["PATH"], "CORPUS_TEST_CASE": case,
            }), mock.patch.object(self.gate, "ROOT", source), redirect_stdout(io.StringIO()):
                output = self.out / case
                rc = self.gate.main(["--output", str(output)])
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(rc, 0 if case in accepted else 1)
                self.assertEqual(manifest["passed"], case in accepted)
                if not case.startswith("raw-empty-"):
                    expected = manifest["raw_scan"]["expected_errors"][0]
                    self.assertEqual(expected["path"], "templates/ci/github/job.yml")
                    self.assertTrue(expected["replacements"]["artifacts"])
                self.assertEqual(manifest["qualified_scan"]["report_sha256"],
                                 self.gate.sha256(output / "qualified.json"))


if __name__ == "__main__":
    unittest.main()
