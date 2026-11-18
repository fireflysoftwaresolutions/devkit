# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev diagram` reads its flags whole, as every `dev` command does (cli.Parser): argparse would read a unique prefix
as the flag, while devkit's own readers of a command line match the flag itself. Run:
python3 -m unittest tests.test_diagram_cli -v"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import diagram  # noqa: E402


class FlagTests(unittest.TestCase):
    def test_an_abbreviated_flag_is_refused_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as stop:
                diagram.main(["new", "flow", "pipeline", "--di", d])
            self.assertEqual(list(Path(d).iterdir()), [])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("unrecognized arguments: --di", err.getvalue())


if __name__ == "__main__":
    unittest.main()
