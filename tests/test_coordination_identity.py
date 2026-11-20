# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who calls: the tech lead's own commands are the tech lead's whatever the crew members' open calls say, and a
command the tech lead types whole, a nested read included, is the tech lead's. Each test reproduces a fault the crew
hit on devkit 26.11.25 (2026-11-19) through the real command line and call ledger, with that day's command texts.
Run: python3 -m unittest tests.test_coordination_identity"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import progress as pg  # noqa: E402
from test_caller_identity import CallerCase  # noqa: E402
from test_progress import launch  # noqa: E402


class CoordinationCallerCase(CallerCase):
    def plan_with_link(self):
        # the Release engineer works on sig2625; ci-redirect is the tech lead's to start
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.start("Release", plan=["sig2625", "ci-redirect"])
            pg.assign("sig2625", "Link", why="the test's assignment")
        launch("a_link", "devkit:link", "Link: sig2625 copy release")

    def state_of(self, name):
        return next(s for s in self.run_()["plan"] if s["name"] == name).get("state")


class MemberWaitTests(CoordinationCallerCase):
    def test_the_tech_leads_begin_stays_theirs_while_a_member_waits_on_a_question_that_quotes_it(self):
        # three tries of the tech lead's `dev progress begin ci-redirect` were refused as "inside a
        # crew member's call" while the Release engineer's `dev crew ask` waited for its answer; that question quoted
        # the command, and the call ledger took the member's open call for the one that typed it
        self.plan_with_link()
        self.call('dev crew ask "dev commit refuses my ci-redirect commit: my lane\'s scope is still sig2625\'s '
                  '(blocked, not closed), and dev progress begin is the tech lead\'s. Please start ci-redirect for '
                  'my lane (dev progress begin ci-redirect, or whatever links it)."', aid="a_link")
        self.call("dev progress begin ci-redirect 2>&1 | tail -6")
        rc, out = self.dev("progress", "begin", "ci-redirect")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state_of("ci-redirect"), "running")


class NestedReadTests(CoordinationCallerCase):
    def test_the_tech_lead_acknowledges_with_a_digest_read_in_the_same_command(self):
        # `dev crew ack REF --sha256 "$(dev crew read REF --json | python3 -c …)" --generation 1`
        # was refused as not_recipient in the main session; the same two calls typed one after the other passed
        self.plan_with_link()
        report = pg.crew_ask("Report: " + "x" * 400, from_agent="a_link")
        ref = report["id"]
        self.call(f'dev crew read {ref} --json')
        rc, out = self.dev("crew", "read", ref, "--json")
        self.assertEqual(rc, 0, out)
        page = json.loads(out)
        self.call(f'dev crew ack {ref} --sha256 "$(dev crew read {ref} --json | python3 -I -c \'import json,sys; '
                  f'print(json.load(sys.stdin)["digest"])\')" --generation 1')
        rc, out = self.dev("crew", "ack", ref, "--sha256", page["digest"], "--generation", str(page["generation"]))
        self.assertEqual(rc, 0, out)
        self.assertIn("main", next(m for m in self.run_()["messages"] if m["id"] == ref).get("receipts") or {})


if __name__ == "__main__":
    unittest.main()
