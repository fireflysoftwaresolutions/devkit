# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test audit` reads its flags whole, as every `dev` command does (cli.Parser), also when its module loads only
at run time and testx parses the flags it kept for it: argparse would read `--chang` as `--changed`. The audit module
is a stand-in. Run: python3 -m unittest tests.test_testx_flags -v"""
import argparse
import contextlib
import io
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import testx  # noqa: E402


class AuditFlagTests(unittest.TestCase):
    def test_an_abbreviated_flag_of_a_late_loaded_audit_is_refused(self):
        ran = []
        audit = types.SimpleNamespace(add_arguments=lambda p: p.add_argument("--changed", action="store_true"),
                                      cmd=lambda a: ran.append(a.changed) or 0)
        kept = argparse.Namespace(audit_placeholder=True, audit_args=["--chang"])
        with mock.patch.object(testx, "_audit_module", return_value=audit), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = testx._audit(kept)
        self.assertEqual((rc, ran), (2, []))
        self.assertIn("unrecognized arguments: --chang", err.getvalue())


if __name__ == "__main__":
    unittest.main()
