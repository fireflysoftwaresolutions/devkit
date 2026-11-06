# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Progress, timing and predictability. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import progress as pg  # noqa: E402


class TempState:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, os.environ.get("DEVKIT_SESSION"))
        pg.RUNS, pg.TIMING, pg.SESSIONS = root / "runs", root / "timing", root / "sessions"
        pg.PENDING = pg.TIMING / "pending"
        pg._publish = lambda: None
        os.environ["DEVKIT_SESSION"] = "claude:s1"
        os.environ["DEVKIT_NO_NOTIFY"] = "1"
        pg.SESSIONS.mkdir(parents=True)
        self.sess = pg.SESSIONS / "claude-s1.json"
        self.sess.write_text(json.dumps({"agent": "claude", "session": "s1", "started": time.time(),
                                         "project": "acme/api"}))
        return self

    def __exit__(self, *exc):
        pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, env = self.saved
        if env is None:
            os.environ.pop("DEVKIT_SESSION", None)
        else:
            os.environ["DEVKIT_SESSION"] = env
        self.tmp.cleanup()


class ProgressTests(unittest.TestCase):
    def test_minutes(self):
        self.assertEqual(pg.parse_minutes("45m"), 45)
        self.assertEqual(pg.parse_minutes("1h30m"), 90)
        self.assertEqual(pg.parse_minutes("2h"), 120)
        self.assertEqual(pg.parse_minutes("15"), 15)
        self.assertEqual(pg.fmt_min(95), "1h35m")

    def test_eta_blends_pace_and_estimate(self):
        now = time.time()
        run = {"started": now - 20 * 60, "steps": 4, "step": 2, "estimate": 30, "state": "running"}
        e = pg.eta(run, now)
        self.assertTrue(10 <= e <= 20, e)  # pace says 20m left, estimate says 10m
        self.assertAlmostEqual(pg.eta({"started": now - 600, "steps": 0, "step": 0, "estimate": 30}, now), 20, 0)

    def test_run_lifecycle_records_actual_vs_estimate(self):
        with TempState():
            run = pg.start("Export invoices", steps=3, estimate="30m")
            self.assertEqual(run["state"], "running")
            run = pg.step("parser done, 12 tests pass", nxt="wire the endpoint")
            self.assertEqual(run["step"], 1)
            text = pg.render_update(run)
            self.assertIn("1/3", text)
            self.assertIn("Next: wire the endpoint", text)
            run = pg.step("shipped", kind="done")
            self.assertEqual((run["state"], run["step"]), ("done", 3))
            recs = pg.records(1, "run")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["estimate"], 30)
            # the session remembers when the developer was last updated
            self.assertIn("last_update", json.loads((pg.SESSIONS / "claude-s1.json").read_text()))

    def test_calibration_corrects_estimates(self):
        with TempState():
            for actual in (40, 45, 50):  # estimates of 30m that took longer
                pg.record("run", "claude", "s1", "x", actual * 60, "acme/api", estimate=30)
            cal = pg.calibration("acme/api")
            self.assertAlmostEqual(cal["ratio"], 1.5, 1)
            os.environ["DEVKIT_SESSION"] = "claude:s1"
            run = pg.start("Next feature", estimate="20m")
            self.assertAlmostEqual(run["estimate_cal"], 30, 0)

    def test_status_question_gets_real_records(self):
        with TempState():
            pg.start("Release", steps=4, estimate="1h")
            pg.step("tests green")
            for q in ("¿cómo va?", "que te falta", "how is it going", "dame un resumen", "status?"):
                with self.subTest(q=q):
                    ctx = pg.on_prompt("claude", {"session_id": "s1", "prompt": q})
                    self.assertIn("Release", ctx)
                    self.assertIn("1/4", ctx)
            self.assertIsNone(pg.on_prompt("claude", {"session_id": "s1", "prompt": "add a CSV export"}))

    def test_silence_nudge_and_tool_timing(self):
        with TempState() as st:
            s = json.loads(st.sess.read_text())
            s["turn_started"] = time.time() - 15 * 60
            st.sess.write_text(json.dumps(s))
            pg.tool_started({"tool_use_id": "toolu_1", "tool_name": "Bash", "tool_input": {"command": "mvn verify"}})
            pend = pg.PENDING / "toolu_1.json"
            d = json.loads(pend.read_text())
            d["ts"] -= 120
            pend.write_text(json.dumps(d))
            msg = pg.on_tool_done("claude", {"session_id": "s1", "tool_use_id": "toolu_1"})
            self.assertIn("since your last update", msg)
            self.assertIsNone(pg.on_tool_done("claude", {"session_id": "s1"}))  # once per interval
            tools = pg.records(1, "tool")
            self.assertEqual(len(tools), 1)
            self.assertIn("mvn verify", tools[0]["what"])

    def test_subagents_and_turns(self):
        with TempState() as st:
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "Explore"}, True)
            self.assertIn("a1", json.loads(st.sess.read_text())["subagents"])
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1"}, False)
            self.assertEqual(len(pg.records(1, "subagent")), 1)
            pg.on_turn_end("claude", "s1", {"turn_started": time.time() - 90, "status": "DevKit · x"})
            self.assertEqual(len(pg.records(1, "turn")), 1)
            self.assertIn("Time", pg.report(1))


if __name__ == "__main__":
    unittest.main()
