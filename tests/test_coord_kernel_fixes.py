# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Crew coordination at its edges: a lane that runs no step holds no file, and `dev commit` says so.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_coord_kernel_fixes -v
import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import gitx  # noqa: E402


class UnassignedLaneCommitTests(unittest.TestCase):
    def test_a_commit_by_a_lane_that_runs_no_step_says_it_runs_none_and_names_no_empty_scope(self):
        # lane_scope gives a crew lane that runs no step one empty layer: it holds no file
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gitx._lane_refusal([[]], ["server/api.py"])
        text = err.getvalue()
        self.assertEqual(rc, 2)
        self.assertIn("runs no step", text)
        self.assertIn("server/api.py", text)
        self.assertNotIn("()", text)

if __name__ == "__main__":
    unittest.main()
