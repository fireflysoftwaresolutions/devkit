# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""dev docs check never reports success when it cannot read a checker's output (lesson: hidden failures)."""
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, docs  # noqa: E402


class ValeOutputTests(unittest.TestCase):
    def run_check(self, stdout, stderr):
        d = tempfile.mkdtemp()
        Path(d, "README.md").write_text("# Title\n\nText.\n")
        Path(d, ".vale.ini").write_text("StylesPath = s\n")
        saved = (core.have, docs.subprocess.run)
        core.have = lambda tool: tool == "vale"

        def fake_run(args, **kw):
            return subprocess.CompletedProcess(args, 1, stdout=stdout, stderr=stderr)
        docs.subprocess.run = fake_run
        try:
            with redirect_stdout(io.StringIO()) as out:
                rc = docs.check(Path(d), prose=True)
        finally:
            core.have, docs.subprocess.run = saved
        return rc, out.getvalue()

    def test_unreadable_output_fails(self):
        rc, out = self.run_check("not json at all", "")
        self.assertNotEqual(rc, 0)
        self.assertIn("error", out)

    def test_skipped_files_fail(self):
        rc, out = self.run_check("{}", "E201 Invalid front matter in plugin/commands/x.md")
        self.assertNotEqual(rc, 0)

    def test_clean_output_passes(self):
        rc, _ = self.run_check("{}", "")
        self.assertEqual(rc, 0)

    def test_error_alerts_fail(self):
        alert = '{"README.md": [{"Line": 3, "Span": [1, 4], "Severity": "error", "Check": "Google.Latin", ' \
                '"Message": "Use for example"}]}'
        rc, out = self.run_check(alert, "")
        self.assertNotEqual(rc, 0)
        self.assertIn("Google.Latin", out)

    # 192 findings with 22 errors listed 40 warnings and none of the errors: the list was sorted by file and cut at 40
    def test_every_error_is_listed_before_the_warnings_and_left_out_warnings_are_counted(self):
        def alert(line, severity, check):
            return {"Line": line, "Span": [1, 4], "Severity": severity, "Check": check, "Message": "m"}
        data = {"A.md": [alert(n, "warning", "Google.Will") for n in range(1, 61)],
                "B.md": [alert(n, "error", f"Google.Err{n}") for n in range(1, 23)],
                "C.md": [alert(1, "warning", "Google.Late")]}
        rc, out = self.run_check(json.dumps(data), "")
        self.assertNotEqual(rc, 0)
        self.assertIn("83 finding(s), 22 error(s)", out)
        listed = [ln.strip() for ln in out.splitlines() if re.match(r"\s{4}\S+\.md:\d+:\d+: ", ln)]
        errors = [ln for ln in listed if ": error " in ln]
        self.assertEqual(len(errors), 22)
        first_warning = next(k for k, ln in enumerate(listed) if ": warning " in ln)
        self.assertTrue(all(listed.index(e) < first_warning for e in errors), listed[:25])
        self.assertEqual(sum(1 for ln in listed if ": warning " in ln), 40)
        self.assertRegex(out, r"21 more warning\(s\) in 2 file\(s\) not listed: vale --config \S+ "
                              r"--minAlertLevel warning A\.md C\.md")


class SvgTests(unittest.TestCase):
    # The diagram rules (a title for people who cannot see the picture, no scripts) are for the images a reader sees.
    # A test fixture is a deliberately plain image a test loads, so the check fails a page's diagram without a title
    # and passes the same image under a test fixtures folder.
    def test_a_docs_image_without_a_title_fails_and_a_test_fixture_image_does_not(self):
        plain = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
        saved = core.have
        core.have = lambda tool: False  # no markdownlint, link checker or vale: only the image rules run
        try:
            # the folders above the checkout never count: a checkout under testdata/ or a managed worktree
            # (.worktrees/) still has its docs images checked
            for checkout, where, failing in (("", "docs/images/a.svg", True), ("", "tests/fixtures/visual/b.svg", False),
                                             ("", "test/fixtures/c.svg", False), ("", "src/__fixtures__/d.svg", False),
                                             ("", "pkg/testdata/e.svg", False), ("testdata/repo", "docs/f.svg", True),
                                             (".worktrees/repo/feat-x", "docs/g.svg", True)):
                with self.subTest(checkout=checkout, where=where), tempfile.TemporaryDirectory() as d:
                    root = Path(d, checkout)
                    root.mkdir(parents=True, exist_ok=True)
                    Path(root, "README.md").write_text("# Title\n\nText.\n")
                    Path(root, where).parent.mkdir(parents=True)
                    Path(root, where).write_text(plain)
                    with redirect_stdout(io.StringIO()) as out:
                        rc = docs.check(root)
                    self.assertEqual((rc != 0, "no <title>" in out.getvalue()), (failing, failing), out.getvalue())
        finally:
            core.have = saved


if __name__ == "__main__":
    unittest.main()
