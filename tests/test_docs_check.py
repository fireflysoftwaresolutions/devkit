# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""dev docs check never reports success when it cannot read a checker's output (lesson: hidden failures)."""
import io
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


if __name__ == "__main__":
    unittest.main()
