# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Which lane and step an audit record is filed under (lib/devkit/audit.py, _context). The PreToolUse hook stamps a
# crew member's lane only on its `dev progress`, `dev crew`, `dev watch` and `dev session` calls, so a member's
# `dev commit`, `dev lesson` or `dev task` reaches the audit with no lane named. The audit read that as the tech
# lead's call and filed it under the main lane's running step (once, the Technical writer's records under the tech
# lead's "dashboard security review"). A record now takes its caller the way identity decides it: a hook's payload,
# else the lane mark, the call ledger or "nobody else can be speaking"; a caller it cannot tie to a lane gets no
# step and `crew: {"member": "unknown"}`.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_audit_lane
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import audit, hooks  # noqa: E402
from test_identity import IdentityCase  # noqa: E402

COMMIT = ["commit", "-m", "fix(api): keep the order", "--cause", "the sort key", "lib/api.py"]
TASK = ["task", "add", "rename the column"]


class AuditLaneTests(IdentityCase):
    # a run whose tech lead works on "api" while the QA engineer (Mouse) works on "tests" in a lane of its own; the
    # call ledger holds the shell calls each of them has open, as the PreToolUse hook writes them
    def setUp(self):
        super().setUp()
        self.crew()

    def dev_record(self, args):
        # what cli.main records for one `dev` command: its arguments as sys.argv, the record made inside the call
        with self.argv(*args):
            audit.record("dev." + args[0], list(args[1:]), "", "ok")
        return self.last("dev." + args[0])

    def last(self, action):
        return [r for r in audit.query(action=action, days=1)][-1]

    def test_each_callers_dev_command_is_filed_under_its_own_lane_and_step(self):
        # the member's commit carries no lane mark: the ledger's open call of the member names it
        self.call("cd /Users/dev/Development/acme/api && dev " + " ".join(
            f'"{a}"' if " " in a else a for a in COMMIT), member=True)
        mine = self.dev_record(COMMIT)
        self.assertEqual(mine.get("step"), "tests")
        self.assertEqual((mine["crew"]["member"], mine["crew"]["lane"]), ("mouse", self.member))
        # the tech lead's own command in the same moment keeps the main lane's step and names no crew member
        self.call('dev task add "rename the column"')
        lead = self.dev_record(TASK)
        self.assertEqual(lead.get("step"), "api")
        self.assertNotIn("crew", lead)

    def test_a_caller_devkit_cannot_tie_to_a_lane_gets_no_step_and_crew_unknown(self):
        # a background `dev sync` a member's commit started: no open call types it, and a crew member works, so the
        # caller could be the member as well as the tech lead
        r = self.dev_record(["sync", "--quiet"])
        self.assertNotIn("step", r)
        self.assertEqual(r["crew"], {"member": "unknown"})
        self.assertEqual(r["run"], "Feature")
        self.assertEqual(audit.query(member="unknown", days=1)[-1]["id"], r["id"])

    def test_a_record_that_names_its_crew_member_keeps_it_when_the_caller_is_unknown(self):
        # the member a record is about (the step's owner a plan record names) is data, not a guess of the caller's
        with self.argv("progress", "reconcile"):
            audit.record("plan.done", "tests", "", "ok", by="Morpheus", member="Mouse")
        r = self.last("plan.done")
        self.assertEqual(r["crew"]["member"], "mouse")
        self.assertNotIn("step", r)

    def test_a_command_both_callers_have_open_is_filed_under_neither_step(self):
        # identity gives such a call to the member (the most restrictive caller, for permissions); a record that
        # cannot tell whose it is must not guess
        self.call("dev task add 'rename the column'", member=True)
        self.call('dev task add "rename the column"')
        r = self.dev_record(TASK)
        self.assertNotIn("step", r)
        self.assertEqual(r["crew"], {"member": "unknown"})

    def test_a_hooks_record_without_an_agent_id_takes_the_caller_its_payload_names(self):
        # a hook knows who calls from its payload: a member's carries agent_id, the tech lead's none
        for member, step in ((True, "tests"), (False, "api")):
            with self.subTest(member=member), mock.patch.object(hooks, "_PAYLOAD", self.payload("git status", member)), \
                    audit.hook_identity():
                audit.record("stop.block", "acme/api", "tests · the turn of 10:00", "blocked", rule="tests")
            r = self.last("stop.block")
            self.assertEqual(r.get("step"), step)
            self.assertEqual((r.get("crew") or {}).get("lane"), self.member if member else None)


if __name__ == "__main__":
    unittest.main()
