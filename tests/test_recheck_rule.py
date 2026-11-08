"""The recheck rule: a finished step that used a dependency that was reopened stays open for recheck.

dev progress done refuses while approvals.recheck_open lists a step; a skipped step owes nothing."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import approvals  # noqa: E402


class RecheckOpenTests(unittest.TestCase):
    def test_a_finished_step_flagged_for_recheck_is_listed_with_the_steps_it_used(self):
        run = {"plan": [{"name": "design", "state": "done"},
                        {"name": "api", "state": "done", "recheck": ["design"]},
                        {"name": "ui", "state": "pending", "recheck": ["design", "api"]}]}
        self.assertEqual(approvals.recheck_open(run), [("api", ["design"]), ("ui", ["design", "api"])])

    def test_a_skipped_step_and_a_cleared_flag_owe_no_recheck(self):
        run = {"plan": [{"name": "api", "state": "skipped", "recheck": ["design"]},
                        {"name": "ui", "state": "done", "recheck": []},
                        {"name": "docs", "state": "done"}]}
        self.assertEqual(approvals.recheck_open(run), [])

    def test_a_run_without_a_plan_owes_nothing(self):
        self.assertEqual(approvals.recheck_open({}), [])


if __name__ == "__main__":
    unittest.main()
