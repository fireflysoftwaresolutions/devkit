# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev crew graph` as the developer reads it: a lane once, under the step it works now, in the state `dev crew owner`
# reads (working, stale, ended); skipped steps folded into one line; and outside a project (or with --global) every
# live session of this Mac, each with its run, lanes and what waits, by ids that name their session. Hermetic:
# progress state and the audit trail live in temporary folders. Run: python3 -m unittest tests.test_crew_graph
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from devkit import crew, ui  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_crew import TempState, run_cli  # noqa: E402

T0 = int(time.time()) // 60 * 60 - 7200  # two hours ago: lane states compare with the real clock
OTHER = "5e1f0a2b-7c3d-4e5f-8a9b-0c1d2e3f4a5b"  # a made-up session of another project, never a real one
SAFE_ID = re.compile(r"^[a-z][a-z0-9]*$")


def m(minutes):
    return T0 + minutes * 60


def launch(aid, kind, label, at, sid="s1"):
    pg.note_agent_launch("claude", {"session_id": sid, "tool_use_id": f"toolu_{aid}",
                                    "tool_input": {"description": label, "subagent_type": kind}}, now=at)
    pg.on_subagent("claude", {"session_id": sid, "agent_id": aid, "agent_type": kind}, True, now=at)
    pg.launch_result("claude", {"session_id": sid, "tool_use_id": f"toolu_{aid}", "tool_response": {"agentId": aid}},
                     now=at)


def text(g, **kw) -> list:
    return [ln.rstrip() for ln in ui.ANSI_RE.sub("", crew.graph_text(g, **kw)).splitlines()]


class LaneTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)

    def test_a_resumed_member_shows_once_under_the_step_it_works_now_with_that_steps_time(self):
        # the DevEx engineer finished "fix" and was resumed for "crew record fixes": one lane owns both steps
        pg.start("Hotfix", plan="fix=30m; crew record fixes=30m after fix", now=m(0))
        pg.assign("fix", "Kid", now=m(0), why="test")
        launch("a1", "devkit:kid", "Kid: fix crew message ids", m(1))
        run = pg.load_run("claude", "s1")
        run["plan"][0].update(state="done", owner="a1", started=m(1), finished=m(40))
        run["plan"][1].update(state="running", owner="a1", label="Kid", started=m(100))
        run["agents"]["a1"]["updates"] = [{"ts": m(118), "kind": "note", "text": "working"}]
        pg.run_path("claude", "s1").write_text(json.dumps(run))
        g = crew.session_graph(pg.load_run("claude", "s1"), now=m(120))
        lines = text(g)
        lane_lines = [ln for ln in lines if "DevEx engineer (Kid)" in ln and " 1. " not in ln and " 2. " not in ln]
        self.assertEqual(len([ln for ln in lane_lines if "working" in ln]), 1, "\n".join(lines))
        (working,) = [ln for ln in lane_lines if "working" in ln]
        self.assertIn("crew record fixes", working)
        self.assertNotIn("fix crew message ids", "\n".join(lines))
        step2 = next(n for n in g["nodes"] if n["kind"] == "step" and n["ref"] == 2)
        lane = next(n for n in g["nodes"] if n["kind"] == "lane")
        self.assertEqual((lane["step"], lane["min"]), ("s2", step2["min"]))
        self.assertTrue(working.endswith(crew._mins(step2["min"])), working)
        # under the step it finished, the lane shows as done, once
        i = next(k for k, ln in enumerate(lines) if " 1. fix" in ln)
        self.assertIn("DevEx engineer (Kid) · done", lines[i + 1])

    def test_each_lane_shows_as_dev_crew_owner_reads_it(self):
        pg.start("Release", plan="api=30m; ui=30m", now=m(0))
        pg.assign("api", "Tank", now=m(0), why="test")
        pg.assign("ui", "Trinity", now=m(0), why="test")
        launch("a1", "devkit:tank", "Tank: api", m(1))  # silent since: stale by now
        launch("a2", "devkit:trinity", "Trinity: ui", m(115))
        g = crew.session_graph(pg.load_run("claude", "s1"), now=m(120))
        states = {n["ref"]: n["state"] for n in g["nodes"] if n["kind"] == "lane"}
        self.assertEqual(states, {"a1": "stale", "a2": "working"})
        joined = "\n".join(text(g))
        self.assertIn("Backend engineer (Tank) · api · stale", joined)
        self.assertIn("Frontend engineer (Trinity) · ui · working", joined)

    def test_skipped_steps_fold_into_one_line_unless_asked(self):
        pg.start("Release", plan="build=30m; docs=10m; demo=10m; ship=10m after build", now=m(0))
        pg.reopen("docs", "not in this release", skip=True, now=m(1))
        pg.reopen("demo", "not in this release", skip=True, now=m(1))
        g = crew.session_graph(pg.load_run("claude", "s1"), now=m(5))
        folded = text(g)
        self.assertFalse([ln for ln in folded if "· skipped ·" in ln], "\n".join(folded))
        self.assertIn("– 2 skipped steps: 2. docs, 3. demo · dev crew graph --all shows them",
                      [ln.split("─ ", 1)[-1] for ln in folded])
        shown = text(g, skipped=True)
        self.assertEqual(len([ln for ln in shown if "· skipped ·" in ln]), 2)


class GlobalViewTests(unittest.TestCase):
    # outside a project, `dev crew graph` drew only this session's run; now every live session of this Mac
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        pg.start("Release the API", plan="build=30m", now=m(110))  # this session, acme/api
        (pg.SESSIONS / f"claude-{OTHER}.json").write_text(json.dumps(
            {"agent": "claude", "session": OTHER, "started": m(100), "project": "acme/web"}))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{OTHER}"}):
            pg.start("Checkout page", plan="page=30m", now=m(100))
            pg.assign("page", "Trinity", now=m(100), why="test")
            launch("w1", "devkit:trinity", "Trinity: page", m(101), sid=OTHER)
            pg.crew_decision("Card form: hosted or our own?", options="A: hosted | B: our own", recommend="A",
                             from_agent="w1", now=m(102))
            pg.crew_ask("Which currency format?", from_agent="w1", now=m(103))

    def cli(self, *args, project=""):
        with mock.patch.object(pg, "_project", lambda cwd=None: project):
            return run_cli("crew", "graph", *args)

    def test_outside_a_project_the_graph_shows_every_live_session_with_qualified_ids(self):
        rc, out, _ = self.cli()
        self.assertEqual(rc, 0)
        self.assertIn("Crew graph · every live session on this Mac (global view) · 2 sessions", out)
        self.assertIn("Session graph · acme/api", out)
        self.assertIn("Session graph · acme/web", out)
        self.assertIn("D1@5e1f0a2b · Frontend engineer (Trinity) asks: Card form: hosted or our own?", out)
        self.assertIn("M1@5e1f0a2b · Frontend engineer (Trinity) → Tech lead (Morpheus)", out)
        self.assertLess(out.index("Session graph · acme/api"), out.index("Session graph · acme/web"))  # yours first

    def test_inside_a_project_the_graph_shows_its_sessions_and_global_shows_all(self):
        out = self.cli(project="acme/api")[1]
        self.assertIn("Crew graph · acme/api · 1 session", out)
        self.assertNotIn("Checkout page", out)
        self.assertIn("Checkout page", self.cli("--global", project="acme/api")[1])

    def test_json_and_mermaid_hold_every_session(self):
        doc = json.loads(self.cli("--json")[1])
        self.assertEqual((doc["view"], [g["session"]["project"] for g in doc["sessions"]]),
                         ("global", ["acme/api", "acme/web"]))
        self.assertIn("D1@5e1f0a2b", [n["ref"] for g in doc["sessions"] for n in g["nodes"] if n["kind"] == "decision"])
        mm = self.cli("--mermaid")[1]
        self.assertTrue(mm.startswith("flowchart TD\n"), mm[:40])
        self.assertEqual(mm.count("subgraph g1[") + mm.count("subgraph g2["), 2)
        ids = set(re.findall(r"^\s*([A-Za-z][A-Za-z0-9_]*)[\[\(\{>]", mm, re.M))
        self.assertTrue(ids and all(SAFE_ID.match(i) for i in ids), ids)
        self.assertEqual(len(ids & {"g1main", "g2main"}), 2)


if __name__ == "__main__":
    unittest.main()
