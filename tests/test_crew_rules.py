# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The rules catalog (`dev crew rules`, crew.ENFORCEMENT) holds the coordination rules devkit now enforces, each with
the check that does it: a free lane takes the oldest step waiting for its member, the one its launch names first
(E-claim, coord.claim); a state change needs its call's token or is refused (E-call-token, identity.speaker); a
blocked step frees its lane (E-blocked-frees, coord.block); a command acts as the call that typed it, by the call
token (E-caller); and a member the crew gates let through with findings approves nothing, and the tech lead hears
them (E-gate-findings, gates.let_through, enforced now that the gates judge every stop).

Run: python3 -m unittest tests.test_crew_rules -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import crew  # noqa: E402
from test_crew import run_cli  # noqa: E402
from test_progress import TempState  # noqa: E402

NEW = {"E-claim": "devkit.coord.claim", "E-call-token": "devkit.identity.speaker",
       "E-blocked-frees": "devkit.coord.block"}


def rows() -> dict:
    return {row[0]: row for row in crew.ENFORCEMENT}


class CoordinationRuleTests(unittest.TestCase):
    def test_the_coordination_rules_are_enforced_by_the_checks_that_do_it(self):
        found = {rid: (rows()[rid][3], rows()[rid][4]) for rid in NEW if rid in rows()}
        self.assertEqual(found, {rid: (ref, "enforced") for rid, ref in NEW.items()})
        checked = {rid: ok for (rid, *_), (*_, ok) in zip(crew.ENFORCEMENT, crew.enforcement())}
        self.assertEqual([rid for rid in NEW if not checked[rid]], [])  # each check imports

    def test_gate_findings_are_enforced_now_that_the_gates_judge_every_stop(self):
        rid, rule, where, ref, state = rows()["E-gate-findings"]
        self.assertEqual((ref, state), ("devkit.gates.let_through", "enforced"))
        self.assertIn("every stop", where)

    def test_the_caller_rule_names_the_call_token_not_the_call_ledger(self):
        rid, rule, where, ref, state = rows()["E-caller"]
        self.assertIn("call token", where)
        self.assertNotIn("bash -c", rule)  # a script run by path or a $(…) now acts as the call that typed it
        self.assertEqual(state, "enforced")

    def test_dev_crew_rules_lists_the_coordination_rules_as_enforced(self):
        with TempState():
            rc, out, _ = run_cli("crew", "rules")
        self.assertEqual(rc, 0, out)
        for rid in [*NEW, "E-gate-findings"]:
            with self.subTest(rule=rid):
                line = next((ln for ln in out.splitlines() if ln.lstrip().startswith(rid + " ")), "")
                self.assertIn("enforced", line, out)


if __name__ == "__main__":
    unittest.main()
