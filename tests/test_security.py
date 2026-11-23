# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Scanner process boundaries are mocked; scan's status and rendered report are real."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import cli, security  # noqa: E402


class ScannerFailures(unittest.TestCase):
    CLEAN = {
        "gitleaks": [], "osv-scanner": {"results": []},
        "semgrep": {"results": [], "errors": []},
        "trivy": {"SchemaVersion": 2, "Results": []}, "zizmor": [],
    }

    def scan(self, tool, rc, out, err="", history=True, ignored=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if ignored:
                subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
                (root / ".gitignore").write_text("app.py\n")
                (root / "app.py").write_text("# ignored fixture\n")
            workflows = root / ".github" / "workflows"
            workflows.mkdir(parents=True)
            (workflows / "test.yml").write_text("name: test\n")
            output = io.StringIO()
            with patch.object(security.core, "have", side_effect=lambda name: name == tool), \
                    patch.object(security, "_run", return_value=(rc, out, err)), redirect_stdout(output):
                status = security.scan(root, history=history)
            return status, output.getvalue()

    def assert_error(self, tool, rc, out, err=""):
        status, output = self.scan(tool, rc, out, err)
        self.assertEqual(status, 1, output)
        self.assertIn("error:", output)
        self.assertNotIn("DUMMY_PRIVATE_VALUE", output)
        return output

    def test_operational_failures_are_errors_without_echoing_stderr(self):
        for tool, clean in self.CLEAN.items():
            for rc in (2, 124):
                with self.subTest(tool=tool, rc=rc):
                    output = self.assert_error(tool, rc, json.dumps(clean), "DUMMY_PRIVATE_VALUE")
                    self.assertIn(str(rc), output)

    def test_empty_or_invalid_json_never_reports_clean(self):
        for tool in self.CLEAN:
            for out in ("", "{DUMMY_PRIVATE_VALUE", "null", '"DUMMY_PRIVATE_VALUE"'):
                with self.subTest(tool=tool, out=out):
                    self.assert_error(tool, 0, out)

    def test_report_containers_and_entries_are_validated(self):
        bad_reports = {
            "gitleaks": ({}, [None], [{}]),
            "osv-scanner": ([], {}, {"results": {}}, {"results": [None]},
                            {"results": [{"packages": "bad"}]}),
            "semgrep": ([], {}, {"results": {}}, {"results": [None]},
                        {"results": [{"extra": "bad"}]}),
            "trivy": ([], {}, {"Results": {}}, {"Results": [None]},
                      {"Results": [{"Vulnerabilities": "bad"}]}),
            "zizmor": ({}, [None], [{"locations": "bad"}]),
        }
        for tool, reports in bad_reports.items():
            for report in reports:
                with self.subTest(tool=tool, report=report):
                    self.assert_error(tool, 0, json.dumps(report))

    def test_semgrep_errors_fail_even_with_success_exit_and_partial_findings(self):
        hit = {"extra": {"severity": "WARNING"}, "path": "app.py", "start": {"line": 7}, "check_id": "test.rule"}
        for hits in ([], [hit]):
            report = {"results": hits, "errors": [{"message": "DUMMY_PRIVATE_VALUE"}]}
            output = self.assert_error("semgrep", 0, json.dumps(report))
            self.assertIn("1 scanner error", output)
            if hits:
                self.assertIn("app.py:7", output)

    def test_osv_no_packages_exit_cannot_hide_failures_or_findings(self):
        self.assert_error("osv-scanner", 129, "", "No package sources found DUMMY_PRIVATE_VALUE")
        self.assert_error("osv-scanner", 128, "{bad")
        status, output = self.scan("osv-scanner", 128, "", "No package sources found")
        self.assertEqual(status, 0, output)
        self.assertIn("no lockfiles", output)

    def test_findings_exit_requires_reported_findings(self):
        for tool, codes in (("gitleaks", (3,)), ("osv-scanner", (1,)),
                            ("semgrep", (1,)), ("zizmor", (11, 12, 13, 14))):
            for rc in codes:
                with self.subTest(tool=tool, rc=rc):
                    self.assert_error(tool, rc, json.dumps(self.CLEAN[tool]))

    def test_partial_trivy_findings_survive_a_process_failure(self):
        report = {"Results": [{"Target": "app", "Vulnerabilities": [
            {"Severity": "HIGH", "PkgName": "demo", "InstalledVersion": "1", "VulnerabilityID": "CVE-test"}]}]}
        output = self.assert_error("trivy", 1, json.dumps(report), "DUMMY_PRIVATE_VALUE")
        self.assertIn("CVE-test", output)

    def test_valid_reports_and_missing_tools_keep_existing_policy(self):
        for tool, report in self.CLEAN.items():
            with self.subTest(tool=tool):
                status, output = self.scan(tool, 0, json.dumps(report))
                self.assertEqual(status, 0, output)
                self.assert_error(tool, 124, json.dumps(report))
        status, output = self.scan("missing", 0, "")
        self.assertEqual(status, 0, output)
        self.assertIn("not installed", output)

    def test_findings_are_preserved_without_exposing_secret_content(self):
        reports = {
            "gitleaks": (3, [{"RuleID": "demo-rule", "File": "app.py", "StartLine": 7,
                              "Secret": "DUMMY_PRIVATE_VALUE"}]),
            "osv-scanner": (1, {"results": [{"packages": [{"package": {"name": "demo", "version": "1"},
                               "vulnerabilities": [{"id": "CVE-test", "summary": "demo"}]}]}]}),
            "semgrep": (1, {"results": [{"extra": {"severity": "WARNING"}, "path": "app.py",
                           "start": {"line": 7}, "check_id": "demo-rule"}], "errors": []}),
            "trivy": (0, {"Results": [{"Target": "app.py", "Secrets": [{"Severity": "HIGH", "StartLine": 7,
                        "RuleID": "demo-rule", "Match": "DUMMY_PRIVATE_VALUE"}]}]}),
            "zizmor": (14, [{"ident": "demo-rule", "desc": "demo", "determinations": {"severity": "high"},
                             "locations": []}]),
        }
        for tool, (rc, report) in reports.items():
            with self.subTest(tool=tool):
                status, output = self.scan(tool, rc, json.dumps(report))
                self.assertEqual(status, 1, output)
                self.assertNotIn("error:", output)
                self.assertNotIn("DUMMY_PRIVATE_VALUE", output)
                self.assertIn("demo", output)
                partial = self.assert_error(tool, 124, json.dumps(report))
                self.assertIn("demo", partial)

    def test_ignored_secrets_do_not_hide_scanner_failure(self):
        report = json.dumps([{"RuleID": "demo-rule", "File": "app.py", "StartLine": 7}])
        for rc, expected in ((3, 0), (2, 1), (124, 1)):
            with self.subTest(rc=rc):
                status, output = self.scan("gitleaks", rc, report, history=False, ignored=True)
                self.assertEqual(status, expected, output)
                if expected:
                    self.assertIn("error:", output)
                else:
                    self.assertIn("1 in git-ignored files skipped", output)
        malformed = json.dumps([{"RuleID": "demo-rule", "File": "app.py", "StartLine": 7}, None])
        status, output = self.scan("gitleaks", 3, malformed, history=False, ignored=True)
        self.assertEqual(status, 1, output)
        self.assertIn("error:", output)

    def test_valid_partial_findings_survive_invalid_entries(self):
        hit = {"extra": {"severity": "WARNING"}, "path": "app.py", "start": {"line": 7}, "check_id": "test.rule"}
        output = self.assert_error("semgrep", 0, json.dumps({"results": [hit, None], "errors": []}))
        self.assertIn("app.py:7", output)
        report = [{"ident": "demo-rule", "desc": "demo", "determinations": {"severity": "high"}, "locations": []}, None]
        output = self.assert_error("zizmor", 14, json.dumps(report))
        self.assertIn("demo-rule", output)

    def test_info_findings_remain_nonblocking_but_scanner_errors_fail(self):
        hit = {"extra": {"severity": "INFO"}, "path": "app.py", "start": {"line": 7}, "check_id": "test.rule"}
        for rc in (0, 1):
            status, output = self.scan("semgrep", rc, json.dumps({"results": [hit], "errors": []}))
            self.assertEqual(status, 0, output)
            self.assertIn("0 error/warning, 1 info", output)
        self.assert_error("semgrep", 0, json.dumps({"results": [hit], "errors": [{"type": "Timeout"}]}))

    # a scan is complete only without errors, rule timeouts, fixpoint timeouts and skipped rules: the qualification
    # gate's rule. Each one is named by its type, path and rule (8 at most, then a count), never by its message.
    def test_every_incomplete_analysis_fails_and_is_named_without_its_message(self):
        fixpoint = {"error_type": "Fixpoint timeout", "severity": "warn", "message": "DUMMY_PRIVATE_VALUE",
                    "location": {"path": "lib/shellparse.py", "start": {"line": 1}, "end": {"line": 1}},
                    "rule_id": "python.flask.security.injection.nan-injection.nan-injection"}
        for report in ({"results": [], "errors": [], "time": {"fixpoint_timeouts": [fixpoint]}},
                       {"results": [], "errors": [], "skipped_rules": [{"rule_id": "demo.skipped", "details": "x",
                                                                         "position": {"line": 1, "col": 1}}]}):
            with self.subTest(report=sorted(report)):
                output = self.assert_error("semgrep", 0, json.dumps(report))
                self.assertIn("1 scanner error(s); scan incomplete", output)
        output = self.assert_error("semgrep", 0, json.dumps({"results": [], "errors": [], "time": {
            "fixpoint_timeouts": [fixpoint]}}))
        self.assertIn("Fixpoint timeout  lib/shellparse.py  python.flask.security.injection.nan-injection.nan-injection",
                      output)
        errors = [{"code": 2, "level": "warn", "type": "Timeout", "rule_id": f"demo.rule{i}", "path": f"src/m{i}.py",
                   "message": "DUMMY_PRIVATE_VALUE"} for i in range(8)]
        errors.append({"code": 3, "level": "warn", "type": ["PartialParsing", [{"path": "src/p.py"}]],
                       "path": "src/p.py", "message": "DUMMY_PRIVATE_VALUE"})
        output = self.assert_error("semgrep", 0, json.dumps({"results": [], "errors": errors,
                                                             "time": {"fixpoint_timeouts": [fixpoint]}}))
        self.assertIn("10 scanner error(s); scan incomplete", output)
        self.assertIn("Timeout  src/m7.py  demo.rule7", output)
        self.assertNotIn("src/p.py", output)
        self.assertIn("2 more scanner error(s)", output)


class SemgrepCommandTests(unittest.TestCase):
    """The Semgrep command `dev sec scan` and the qualification gate (scripts/security_corpus.py) share."""
    BOTO = "python.boto3.security.hardcoded-token.hardcoded-token"
    FLASK = "python.flask.security.injection.nan-injection.nan-injection"
    DJANGO = "python.django.security.nan-injection.nan-injection"

    def command(self, files, exclude=()):
        with tempfile.TemporaryDirectory() as directory:
            for name, text in files.items():
                path = Path(directory, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            return security.semgrep_command(Path(directory), exclude=exclude)

    @staticmethod
    def left_out(argv):
        return {argv[i + 1] for i, word in enumerate(argv[:-1]) if word == "--exclude-rule"}

    # serial (Semgrep's taint budget reads CPU time its parallel workers share) and without a wall-clock rule timeout,
    # so the verdict depends on neither the runner's core count nor its speed
    def test_the_scan_is_serial_and_has_no_wall_clock_rule_timeout(self):
        argv = self.command({})
        self.assertEqual(argv[:4], ["semgrep", "scan", "--config", "p/default"])
        self.assertEqual({flag: argv[argv.index(flag) + 1] for flag in ("--jobs", "--timeout", "--timeout-threshold")},
                         {"--jobs": "1", "--timeout": "0", "--timeout-threshold": "0"})

    # a rule that can only match code using a package is left out only when no Python file of the tree imports it:
    # an import statement counts, a mention in text or in a string does not
    def test_a_package_rule_is_left_out_only_when_no_python_file_imports_the_package(self):
        both = {self.BOTO, self.FLASK, self.DJANGO}
        cases = (({"app.py": "import os\n"}, both),
                 ({"web/app.py": "from flask import Flask\n"}, {self.BOTO, self.DJANGO}),
                 ({"web/app.py": "    import flask.json as fj\n"}, {self.BOTO, self.DJANGO}),
                 ({"web/views.py": "from django.http import HttpResponse\n"}, {self.BOTO, self.FLASK}),
                 ({"svc/aws.py": "import json, botocore.session as session\n"}, {self.FLASK, self.DJANGO}),
                 ({"tools/deploy": "#!/usr/bin/env python3\nimport boto3\n"}, {self.FLASK, self.DJANGO}),
                 ({"notes.md": "import flask\n", "app.py": "text = 'import flask'  # from flask import x\n"}, both))
        for files, excluded in cases:
            with self.subTest(files=files):
                self.assertEqual(self.left_out(self.command(files)), excluded)
        # a folder the scan leaves out is no reason to keep the rule
        argv = self.command({"node_modules/pkg/app.py": "import flask\n"}, exclude=["node_modules"])
        self.assertEqual(self.left_out(argv), both)
        self.assertEqual(argv[argv.index("--exclude") + 1], "node_modules")

    # dev sec scan runs that command and says which rules it left out, and why
    def test_dev_sec_scan_runs_the_shared_command_and_names_the_rules_it_leaves_out(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "app.py").write_text("from flask import Flask\n")
            output = io.StringIO()
            with patch.object(security.core, "have", side_effect=lambda name: name == "semgrep"), \
                    patch.object(security, "_run", side_effect=lambda argv, cwd: calls.append(argv) or (
                        0, json.dumps({"results": [], "errors": []}), "")), redirect_stdout(output):
                status = security.scan(Path(directory))
            self.assertEqual(calls, [security.semgrep_command(Path(directory), exclude=security.SKIP) + ["--quiet"]])
        self.assertEqual(status, 0, output.getvalue())
        self.assertIn(f"left out {self.BOTO}: no Python file here imports boto3 or botocore", output.getvalue())
        self.assertNotIn(f"left out {self.FLASK}", output.getvalue())


class SecCommandTests(unittest.TestCase):
    # `dev sec scan .` in a folder outside any checkout (an exported copy of a repository) crashed with TypeError:
    # "." matched a portfolio project by its name (acme/.github starts with "."), which is not on this Mac and has no
    # folder. A folder is scanned where it is; a project that is not on this Mac is refused with the command to get it.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name).resolve() / "export"
        self.folder.mkdir()
        (self.folder / "app.py").write_text("x = 1\n")
        portfolio_only = {"id": "acme/.github", "owner": "acme", "name": ".github", "path": None, "local": False,
                          "main": {}, "worktrees": [], "remote": "", "default_branch": "main"}
        self.reg = {"projects": [portfolio_only], "issues": [], "sessions": []}

    def sec(self, *argv):
        scanned, out, err, before = [], io.StringIO(), io.StringIO(), os.getcwd()
        os.chdir(self.folder)
        try:
            with patch.object(cli, "_reg", lambda **kw: self.reg), \
                    patch.object(cli.registry, "lookup", lambda *a, **k: None), \
                    patch.object(cli.registry, "load_aliases", lambda: {}), \
                    patch.object(security, "scan", lambda top, **kw: scanned.append(Path(top)) or 0), \
                    redirect_stdout(out), redirect_stderr(err):
                try:
                    rc = cli.main(["sec", "scan", *argv])
                except SystemExit as e:
                    rc = e.code
        finally:
            os.chdir(before)
        return rc, scanned, out.getvalue() + err.getvalue()

    def test_a_folder_outside_any_checkout_is_scanned_where_it_is(self):
        for query in (".", "./", str(self.folder)):
            with self.subTest(query=query):
                rc, scanned, said = self.sec(query)
                self.assertEqual((rc, scanned), (0, [self.folder]), said)

    def test_a_project_that_is_not_on_this_mac_is_refused_with_the_command_to_get_it(self):
        rc, scanned, said = self.sec(".git")
        self.assertEqual((rc, scanned), (2, []), said)
        self.assertIn("dev clone acme/.github", said)
        self.assertNotIn("TypeError", said)
