# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Who does what: routing, delegation between crew members, declines and dispatch of ready work."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import dispatch  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState  # noqa: E402

T0 = 1_800_000_000.0


def m(minutes):
    return T0 + minutes * 60


def launch(aid, kind, label, at):
    pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": label, "subagent_type": kind}})
    pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind}, True, now=at)


class RouteTests(unittest.TestCase):
    def best(self, task, files=None):
        return dispatch.route(task, files)[0][0]["id"]

    def test_routes_by_role_and_files(self):
        self.assertEqual(self.best("add a database migration for the invoices table"), "dozer")
        self.assertEqual(self.best("the CI pipeline is slow and flaky"), "link")
        self.assertEqual(self.best("threat model for the new login and secrets handling"), "keymaker")
        self.assertEqual(self.best("review this change before the merge"), "seraph")
        self.assertEqual(self.best("change the button", files=["web/src/app/Button.tsx"]), "trinity")
        self.assertEqual(self.best("update it", files=["terraform/main.tf"]), "niobe")
        self.assertEqual(self.best("write the runbook and release notes"), "ghost")
        self.assertEqual(self.best("something vague nobody owns"), "neo")  # the generalist

    def test_reasons_are_given(self):
        member, score, why = dispatch.route("add a database migration")[0]
        self.assertGreater(score, 0)
        self.assertTrue(any("migration" in w for w in why), why)


class DelegationTests(unittest.TestCase):
    def test_delegate_to_a_working_member_goes_to_its_lane(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            r = dispatch.delegate("add a nullable column for the export date", to="Dozer", from_agent="a1",
                                  scope="db/**", now=m(2))
            self.assertEqual(r["member"]["id"], "dozer")
            self.assertEqual(r["lane"], "a2")
            run = pg.load_run("claude", "s1")
            step = run["plan"][-1]
            self.assertEqual((step["owner"], step["state"], step["requested_by"]), ("a2", "running", "a1"))
            self.assertIn("db/**", run["agents"]["a2"]["scope"])
            mail = pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a2"})
            self.assertIn("Neo delegates", mail)
            self.assertIn("dev crew decline", mail)

    def test_delegate_to_an_idle_member_asks_morpheus_to_launch_it(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a1", "devkit:neo", "Neo: parser", m(1))
            r = dispatch.delegate("review the parser before merge", to="auto", from_agent="a1", now=m(2))
            self.assertEqual(r["member"]["id"], "seraph")
            self.assertIsNone(r["lane"])
            ctx = pg.on_tool_done("claude", {"session_id": "s1"})
            self.assertIn("Neo delegates", ctx)
            self.assertIn("ready to launch", ctx)
            self.assertIn("dev crew brief seraph", ctx)
            self.assertNotIn("ready to launch", pg.on_tool_done("claude", {"session_id": "s1"}) or "")  # told once

    def test_dependencies_and_capacity_decide_what_is_ready(self):
        with TempState():
            pg.start("Feature", plan=["design=30m", "api=1h after design", "ui=1h after design"], now=m(0))
            pg.assign("api", "Tank", now=m(0))
            pg.assign("ui", "Trinity", now=m(0))
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual(r["launch"], [])
            self.assertEqual(sorted(w for _, w in r["waiting"]), ["after design", "after design"])
            pg.step("design done", check="ADR-12 written", now=m(30))
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual(sorted(s["name"] for s in r["launch"]), ["api", "ui"])  # both, in parallel
            for i, who in enumerate(("neo", "mouse", "dozer", "link")):  # all four slots busy
                launch(f"b{i}", f"devkit:{who}", f"{who.title()}: x", m(31))
            r = dispatch.ready(pg.load_run("claude", "s1"))
            self.assertEqual((r["launch"], len(r["queued"])), ([], 2))

    def test_decline_returns_the_step_to_morpheus(self):
        with TempState():
            pg.start("Feature", plan=["parser=30m"], now=m(0))
            launch("a2", "devkit:dozer", "Dozer: schema", m(1))
            dispatch.delegate("add the export column", to="Dozer", now=m(2))
            step = pg.load_run("claude", "s1")["plan"][-1]["name"]
            with self.assertRaises(pg.StepError):
                dispatch.decline(step, "", from_agent="a2", now=m(3))  # a reason is required
            s = dispatch.decline(step, "it needs a data migration plan first", from_agent="a2", now=m(3))
            self.assertEqual((s["state"], s["owner"], s["label"]), ("pending", "main", ""))
            ctx = pg.on_tool_done("claude", {"session_id": "s1"})
            self.assertIn("declined", ctx)

    def test_route_the_whole_plan(self):
        with TempState():
            pg.start("Feature", plan=["requirements and acceptance criteria", "database migration",
                                      "pipeline caching"], now=m(0))
            out = dispatch.assign_plan(apply=True, now=m(1))
            self.assertEqual([x[1]["id"] for x in out], ["oracle", "dozer", "link"])
            run = pg.load_run("claude", "s1")
            self.assertEqual([s["label"] for s in run["plan"]], ["The Oracle", "Dozer", "Link"])


if __name__ == "__main__":
    unittest.main()
