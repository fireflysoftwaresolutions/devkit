# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The registry's word for a pull request's checks (B3 of the pipeline security review). A draft's run skips every job
# that has the draft gate, and `dev pr ready` starts the real run, so a rollup whose every check was skipped means no
# real run yet: "none", never "passing". Pure functions, no state. Run: python3 -m unittest tests.test_registry -v
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import registry  # noqa: E402


class ChecksTests(unittest.TestCase):
    def test_every_check_skipped_is_no_checks_yet_and_a_skip_beside_a_pass_is_passing(self):
        draft_run = [{"name": "tests", "conclusion": "SKIPPED", "status": "COMPLETED"},
                     {"name": "security", "conclusion": "skipped", "status": "COMPLETED"}]
        self.assertEqual(registry._checks(draft_run), "none")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"conclusion": "SUCCESS"}]), "passing")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"status": "QUEUED"}]), "pending")
        self.assertEqual(registry._checks([{"conclusion": "SKIPPED"}, {"conclusion": "FAILURE"}]), "failing")


if __name__ == "__main__":
    unittest.main()
