# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A member's stop that the crew gates let through with findings (sent back once in that lane life already): the
# SubagentStop hook reads what they still found (gates.let_through) and the tech lead hears it as devkit's notice,
# named by the member's role, since that stop's Check approves nothing. Through Claude Code's SubagentStop hook, with
# the real gates (test_approvals_token.StopCase: the Code reviewer holds "review").
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_hooks_gate_findings -v
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from test_approvals_token import StopCase  # noqa: E402


class LetThroughFindingsTests(StopCase):
    def notices(self):
        return [m["text"] for m in self.run_().get("messages") or [] if m.get("to") == "main"
                and m.get("from") == "devkit" and "crew gates let it through" in str(m.get("text"))]

    def test_the_tech_lead_hears_what_the_gates_still_found_in_a_stop_they_let_through(self):
        self.assertEqual(json.loads(self.stop() or "{}").get("decision"), "block")  # no dev progress step
        self.assertEqual(self.notices(), [])  # a send-back tells the member, not the tech lead
        self.assertEqual(self.stop(stop_hook_active=True), "")  # still none: let through
        told = self.notices()
        self.assertEqual(len(told), 1, told)
        self.assertIn("Code reviewer (Seraph) stopped", told[0])
        self.assertIn("proofs: dev progress step", told[0])


if __name__ == "__main__":
    unittest.main()
