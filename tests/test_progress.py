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

from devkit import canary  # noqa: E402
from devkit import progress as pg  # noqa: E402


class TempState:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        from devkit import audit
        self.saved = (pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg._publish, os.environ.get("DEVKIT_SESSION"))
        self.audit_saved = audit.AUDIT
        audit.AUDIT = root / "audit"  # tests never write to the real audit trail
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
        from devkit import audit
        audit.AUDIT = self.audit_saved
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
            run = pg.step("parser done", nxt="wire the endpoint", check="12 tests pass")
            self.assertEqual(run["step"], 1)
            text = pg.render_update(run, branch="feat/export")
            self.assertTrue(canary.has_marker(text), text)  # the update is the status block: it passes the canary
            self.assertEqual(canary.format_issues(text), [], text)
            self.assertIn("1/3", text)
            self.assertIn("**Next:** wire the endpoint", text)
            self.assertIn("**Check:** ✓ 12 tests pass", text)
            self.assertIn("⚠ not verified", pg.render_update(pg.step("endpoint wired"), branch="x"))
            run = pg.step("shipped", kind="done")  # the last step was the running one: a legitimate close
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


class TimeModelTests(unittest.TestCase):
    """Deterministic: every call gets a fixed `now`."""
    T0 = 1_800_000_000.0

    def m(self, minutes):
        return self.T0 + minutes * 60

    def test_active_time_is_the_union_of_turns_and_agents(self):
        run = {"v": 2, "started": self.T0, "plan": [], "agents": {}, "intervals": [
            [self.m(0), self.m(10), "turn", "main"], [self.m(5), self.m(20), "agent", "a1"],
            [self.m(40), self.m(45), "turn", "main"]]}
        now = self.m(60)
        self.assertAlmostEqual(pg.active_minutes(run, now=now), 25)  # 0-20 and 40-45; the developer's wait is out
        self.assertAlmostEqual(pg.wall_minutes(run, now), 60)
        self.assertAlmostEqual(pg.active_minutes(run, now=now, lane="a1"), 15)
        self.assertAlmostEqual(pg.active_minutes(run, now=now, lane="main"), 15)
        run["intervals"].append([self.m(50), None, "turn", "main"])  # an open turn counts up to now
        self.assertAlmostEqual(pg.active_minutes(run, now=now), 35)

    def test_parallel_lanes_and_pace(self):
        with TempState():
            run = pg.start("Feature", plan=["design=10m", "build=20m", "docs=30m"], now=self.m(0))
            self.assertEqual([s["est"] for s in run["plan"]], [10, 20, 30])
            run = pg.assign("build", "Neo", now=self.m(0))
            self.assertEqual(run["plan"][1]["state"], "pending")  # waits for its subagent
            pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": "Neo: build the parser",
                                                                                "subagent_type": "devkit:neo"}})
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo"}, True)
            run = pg.load_run("claude", "s1")
            self.assertEqual(run["plan"][1]["owner"], "a1")
            self.assertEqual(run["agents"]["a1"]["label"], "Neo: build the parser")
            # the main agent finishes design in 20 active minutes (estimate 10): pace 2
            run["intervals"].append([self.m(0), self.m(20), "turn", "main"])
            pg._save(run)
            run = pg.step("design done", now=self.m(20))
            self.assertEqual(run["plan"][0]["state"], "done")
            self.assertEqual(run["plan"][2]["state"], "running")  # the main lane moves on to docs
            p, basis = pg.pace(run, self.m(20))
            # 20 actual for 10 estimated, shrunk toward 1 with W = 30: (20 + 30) / (10 + 30) = 1.25
            self.assertAlmostEqual(p, 1.25)
            self.assertEqual(basis, "this run")
            lanes = pg.lanes(run, self.m(20))
            self.assertAlmostEqual(lanes["main"], 37.5)  # docs 30m x pace 1.25
            self.assertAlmostEqual(pg.eta(run, self.m(20)), 37.5)  # the longest lane, not the sum
            # a subagent's own update never advances the main plan
            run = pg.step("parser half done", from_agent="a1", now=self.m(25))
            self.assertEqual(run["plan"][2]["state"], "running")
            self.assertEqual(run["agents"]["a1"]["done"], 1)
            # when the subagent stops, its step is done with its own time
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo"}, False)
            run = pg.load_run("claude", "s1")
            self.assertEqual(run["plan"][1]["state"], "done")
            self.assertEqual(run["agents"]["a1"]["state"], "done")
            self.assertIn("Agents: \u2713 Neo: build the parser", pg.render_update(run, branch="main", now=self.m(30)))

    def test_a_step_closed_instantly_does_not_move_the_pace(self):
        with TempState():
            pg.start("Release", plan=["prep=25m", "build=15m", "docs=60m"], now=self.m(0))
            run = pg.load_run("claude", "s1")
            run["intervals"].append([self.m(0), None, "turn", "main"])
            pg._save(run)
            run = pg.step("prep was already done", now=self.m(0.2))  # 0.2 of 25 minutes: bookkeeping
            self.assertEqual(pg.pace(run, self.m(0.2)), (1.0, "estimate"))
            self.assertAlmostEqual(pg.eta(run, self.m(0.2)), 75, 0)  # build 15 + docs 60, untouched

    def test_wrong_closes_are_refused_and_can_be_undone(self):
        with TempState():
            pg.start("Release", plan=["time model=60m", "integrate dev test=25m", "docs=30m after integrate dev test"],
                     now=self.m(0))
            # the mistake that started this: closing "integrate dev test" with work that was something else
            with self.assertRaises(pg.StepError) as e:
                pg.step("time model and crew coordination built", which="integrate dev test", now=self.m(5))
            self.assertIn("does not describe", str(e.exception))
            with self.assertRaises(pg.StepError) as e:  # the running step is "time model": the text must match it
                pg.step("docs written", now=self.m(5))
            self.assertIn("“docs”", str(e.exception))  # and devkit names the step it sounds like
            with self.assertRaises(pg.StepError):  # docs waits for integrate dev test
                pg.step("docs written", which="docs", now=self.m(5))
            run = pg.step("time model built", now=self.m(10))
            self.assertEqual(run["plan"][0]["state"], "done")
            with self.assertRaises(pg.StepError):  # already done
                pg.step("time model built", which="time model", now=self.m(11))
            run = pg.reopen("time model", "closed too early: ETA bug found", now=self.m(12))
            self.assertEqual(run["plan"][0]["state"], "running")
            self.assertEqual(run["plan"][1]["state"], "pending")
            run = pg.add_step("fix the ETA bug=15m", why="found while testing", now=self.m(13))
            self.assertEqual(run["plan"][-1]["name"], "fix the ETA bug")
            run = pg.reopen("docs", "not needed: covered by the guide", skip=True, now=self.m(14))
            hist = [(h["step"], h["from"], h["to"]) for h in run["history"]]
            self.assertIn(("time model", "done", "running"), hist)
            self.assertIn(("docs", "pending", "skipped"), hist)
            with self.assertRaises(pg.StepError):  # every reopen or skip needs a reason
                pg.reopen("time model", "", now=self.m(15))

    def test_a_subagents_step_is_not_closed_from_outside(self):
        with TempState():
            pg.start("Feature", plan=["api", "ui"], now=self.m(0))
            pg.assign("ui", "Trinity", now=self.m(0))
            pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": "Trinity: ui"}})
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:trinity"}, True,
                           now=self.m(1))
            with self.assertRaises(pg.StepError) as e:
                pg.step("ui done", which="ui", now=self.m(2))
            self.assertIn("still working on it", str(e.exception))

    def test_step_by_name_number_and_unknown(self):
        with TempState():
            pg.start("Release", plan=["tests", "build", "tag"], now=self.m(0))
            run = pg.step("tagged", which="3", now=self.m(5))
            self.assertEqual(run["plan"][2]["state"], "done")
            self.assertEqual(run["plan"][0]["state"], "running")
            with self.assertRaises(ValueError):
                pg.step("x", which="deploy", now=self.m(6))

    def test_old_runs_are_upgraded(self):
        old = {"agent": "claude", "session": "s1", "started": self.T0, "steps": 4, "step": 2, "estimate": 40,
               "state": "running", "updates": [], "plan": ["a", "b", "c", "d"]}
        run = pg.normalize(old)
        self.assertEqual([s["state"] for s in run["plan"]], ["done", "done", "pending", "pending"])
        self.assertAlmostEqual(pg.eta(run, self.m(30)), 20)  # two steps of 10m left, no pace evidence yet

    def test_an_upgraded_run_counts_wall_time_until_activity_was_recorded(self):
        old = {"agent": "claude", "session": "s1", "started": self.T0, "steps": 2, "step": 0, "estimate": 120,
               "state": "running", "updates": [], "plan": ["a", "b"]}
        run = pg.normalize(dict(old))
        run["tracked_since"] = self.m(180)  # three hours of work before activity was recorded
        run["intervals"] = [[self.m(180), self.m(190), "turn", "main"]]
        self.assertAlmostEqual(pg.active_minutes(run, now=self.m(200)), 190)  # 180 wall + 10 active
        legacy = dict(old, v=2, intervals=[[self.m(180), self.m(190), "turn", "main"]], plan=[], agents={})
        self.assertAlmostEqual(pg.active_minutes(pg.normalize(legacy), now=self.m(200)), 190)

    def test_subagent_progress_calls_are_routed_to_their_lane(self):
        from devkit import hooks
        sub = {"agent_id": "a1b2", "tool_name": "Bash", "tool_input": {"command": 'dev progress step "parser done"'}}
        self.assertEqual(hooks._route_subagent_progress(sub)["command"],
                         'DEVKIT_FROM_AGENT=a1b2 dev progress step "parser done"')
        self.assertIsNone(hooks._route_subagent_progress(dict(sub, agent_id=None)))  # the main agent: untouched
        compound = dict(sub, tool_input={"command": "dev progress step x && rm -rf build"})
        self.assertIsNone(hooks._route_subagent_progress(compound))  # never approve anything but that one command
        self.assertIsNone(hooks._route_subagent_progress(dict(sub, tool_input={"command": "git status"})))


class CrewCoordinationTests(unittest.TestCase):
    T0 = 1_800_000_000.0

    def m(self, minutes):
        return self.T0 + minutes * 60

    def test_dependencies_make_the_critical_path(self):
        with TempState():
            run = pg.start("Feature", plan=["design=30m", "api=60m after design", "ui=40m after design",
                                            "docs=20m after api, ui"], now=self.m(0))
            self.assertEqual(run["plan"][3]["after"], ["api", "ui"])
            # all on the main lane: one step at a time, 150 minutes
            self.assertAlmostEqual(pg.eta(run, self.m(0)), 150)
            # ui handed to a subagent: it runs in parallel with api after design, so 30 + 60 + 20 = 110
            run = pg.assign("ui", "Trinity", now=self.m(0))
            self.assertAlmostEqual(pg.eta(run, self.m(0)), 110)

    def test_messages_reach_the_right_crew_member(self):
        with TempState():
            pg.start("Feature", plan=["build"], now=self.m(0))
            pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": "Neo: build it",
                                                                                "subagent_type": "devkit:neo"}})
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo"}, True,
                           now=self.m(1))
            q = pg.crew_ask("Should the parser accept empty lines?", from_agent="a1", now=self.m(2))
            self.assertEqual((q["to"], q["to_name"]), ("main", "Morpheus"))
            # Morpheus gets it with his next tool call; the subagent does not
            self.assertIsNone(pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a1"}))
            ctx = pg.on_tool_done("claude", {"session_id": "s1"})
            self.assertIn("M1 from Neo", ctx)
            self.assertNotIn("M1", pg.on_tool_done("claude", {"session_id": "s1"}) or "")  # delivered once
            pg.crew_answer("M1", "Yes, skip them", now=self.m(3))
            ctx = pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a1"})
            self.assertIn("from Morpheus: Yes, skip them (answer to M1)", ctx)
            # a message to a crew member who has not started yet waits for them
            pg.crew_ask("Review the parser when Neo is done", to="Seraph", now=self.m(4))
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a2", "agent_type": "devkit:seraph"}, True,
                           now=self.m(5))
            self.assertIn("Review the parser", pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a2"}))

    def test_scope_keeps_parallel_agents_apart(self):
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as repo:
            pg.start("Feature", plan=["parser", "ui"], now=self.m(0))
            pg.assign("parser", "Neo", scope="lib/parser/**,tests/test_parser.py", now=self.m(0))
            pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": "Neo: parser"}})
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo"}, True,
                           now=self.m(1))
            edit = lambda p: {"session_id": "s1", "agent_id": "a1", "tool_name": "Edit", "cwd": repo,  # noqa: E731
                              "tool_input": {"file_path": os.path.join(repo, p)}}
            self.assertIsNone(hooks._scope_violation("claude", edit("lib/parser/core.py")))
            self.assertIsNone(hooks._scope_violation("claude", edit("tests/test_parser.py")))
            why = hooks._scope_violation("claude", edit("lib/ui/app.ts"))
            self.assertIn("outside Neo's scope", why)
            self.assertIn("dev crew ask", why)
            self.assertIsNone(hooks._scope_violation("claude", dict(edit("lib/ui/app.ts"), agent_id=None)))  # main

    def test_crew_reports_are_checked_like_the_main_canary(self):
        with TempState(), tempfile.TemporaryDirectory() as d:
            saved = canary.LOG
            canary.LOG = Path(d) / "canary.jsonl"
            try:
                good = {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo",
                        "last_assistant_message": "**[Firefly> DevKit · Neo]** api · `main` · parser\n- **Done:** x\n"
                                                  "- **Check:** \u2713 12 tests\n- **Next:** review"}
                self.assertTrue(canary.check_subagent("claude", good, {}, "acme/api", "Neo"))
                bad = dict(good, last_assistant_message="Done. The parser works.")
                self.assertFalse(canary.check_subagent("claude", bad, {}, "acme/api", "Neo"))
                self.assertEqual(canary.by_member(1), {"Neo": (1, 2)})
                self.assertEqual(canary.summary(1), {})  # crew reports are counted apart from the main replies
                self.assertIn("crew 1/2", canary.summary_line(1))
            finally:
                canary.LOG = saved


class CrewGuardrailTests(unittest.TestCase):
    T0 = 1_800_000_000.0

    def m(self, minutes):
        return self.T0 + minutes * 60

    def launch(self, aid, kind, label, at):
        pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": label,
                                                                            "subagent_type": kind}})
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": aid, "agent_type": kind}, True, now=at)

    def test_at_most_four_members_at_once(self):
        from devkit import hooks
        with TempState():
            pg.start("Big change", plan=["a", "b"], now=time.time())
            for i, who in enumerate(("neo", "trinity", "tank", "dozer")):
                self.launch(f"a{i}", f"devkit:{who}", f"{who.title()}: part {i}", time.time())
            spawn = {"session_id": "s1", "tool_name": "Agent", "tool_input": {"description": "Mouse: tests"}}
            verdict = hooks._crew_limits("claude", spawn)
            self.assertEqual(verdict[0], "deny")
            self.assertIn("the limit is 4", verdict[1])
            self.assertIsNone(hooks._crew_limits("claude", dict(spawn, agent_id="a0")))  # not the main's launch

    def test_overlapping_scopes_are_refused(self):
        with TempState():
            pg.start("Feature", plan=["api", "ui"], now=self.m(0))
            pg.assign("api", "Tank", scope="services/**", now=self.m(0))
            self.launch("a1", "devkit:tank", "Tank: api", self.m(1))
            with self.assertRaises(pg.StepError) as e:
                pg.assign("ui", "Trinity", scope="services/web/**", now=self.m(2))
            self.assertIn("overlaps the files Tank owns", str(e.exception))
            pg.assign("ui", "Trinity", scope="web/**", now=self.m(2))  # disjoint: fine

    def test_the_main_session_respects_a_members_files(self):
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as repo:
            pg.start("Feature", plan=["parser", "docs"], now=time.time())
            pg.assign("parser", "Neo", scope="lib/parser/**", now=time.time())
            self.launch("a1", "devkit:neo", "Neo: parser", time.time())
            edit = {"session_id": "s1", "tool_name": "Edit", "cwd": repo,
                    "tool_input": {"file_path": os.path.join(repo, "lib/parser/core.py")}}
            verdict = hooks._crew_limits("claude", edit)
            self.assertEqual(verdict[0], "deny")
            self.assertIn("belongs to Neo", verdict[1])
            other = dict(edit, tool_input={"file_path": os.path.join(repo, "docs/guide.md")})
            self.assertIsNone(hooks._crew_limits("claude", other))
            os.environ["DEVKIT_SCOPE_OVERRIDE"] = "1"
            try:
                self.assertIsNone(hooks._crew_limits("claude", edit))
            finally:
                del os.environ["DEVKIT_SCOPE_OVERRIDE"]

    def test_a_member_that_never_reports_back_is_released(self):
        with TempState():
            pg.start("Feature", plan=["parser", "docs"], now=self.m(0))
            pg.assign("parser", "Neo", now=self.m(0))
            self.launch("a1", "devkit:neo", "Neo: parser", self.m(1))
            run = pg.normalize(pg.load_run("claude", "s1"))
            self.assertFalse(pg.expire_stale(run, now=self.m(60)))
            self.assertTrue(pg.expire_stale(run, now=self.m(7 * 60)))  # 6 hours without a report
            self.assertEqual(run["agents"]["a1"]["state"], "lost")
            self.assertEqual((run["plan"][0]["state"], run["plan"][0]["owner"]), ("pending", "main"))
            self.assertIn("stopped reporting", run["history"][-1]["why"])


class GuidedAndAutoTests(unittest.TestCase):
    T0 = 1_800_000_000.0

    def m(self, minutes):
        return self.T0 + minutes * 60

    def neo(self):
        pg.assign("parser", "Neo", now=self.m(0))
        pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": "Neo: parser"}})
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo"}, True,
                       now=self.m(1))

    def test_guided_waits_for_the_developer_and_resumes(self):
        with TempState():
            pg.start("Feature", plan=["parser", "docs"], now=self.m(0), mode="guided")
            self.neo()
            d = pg.crew_decision("Skip empty lines or reject the file?", options="A: skip | B: reject",
                                 recommend="A", from_agent="a1", now=self.m(2))
            self.assertEqual((d["id"], d["state"]), ("D1", "pending"))
            run = pg.load_run("claude", "s1")
            self.assertEqual(run["plan"][0]["state"], "blocked")
            self.assertEqual(run["agents"]["a1"]["waiting"], "D1")
            ctx = pg.on_tool_done("claude", {"session_id": "s1"})  # Morpheus is told to ask the developer
            self.assertIn("D1 from Neo", ctx)
            self.assertIn("AskUserQuestion", ctx)
            with self.assertRaises(pg.StepError):  # no finishing with a decision open
                pg.step("all done", kind="done", now=self.m(3))
            pg.crew_decide("D1", "B: reject", now=self.m(4))
            run = pg.load_run("claude", "s1")
            self.assertEqual(run["plan"][0]["state"], "running")
            self.assertNotIn("waiting", run["agents"]["a1"])
            mail = pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a1"})  # delivered on resume
            self.assertIn("D1", mail)
            self.assertIn("B: reject", mail)

    def test_auto_decides_with_the_recommendation_but_not_irreversible_actions(self):
        with TempState():
            pg.start("Feature", plan=["parser", "release"], now=self.m(0), mode="auto")
            self.neo()
            d = pg.crew_decision("Skip empty lines?", options="A: skip | B: reject", recommend="A", from_agent="a1",
                                 now=self.m(2))
            self.assertEqual((d["state"], d["answer"], d["by"]), ("decided", "A", "auto"))
            self.assertEqual(pg.load_run("claude", "s1")["plan"][0]["state"], "running")  # Neo keeps working
            push = pg.crew_decision("Push the release branch?", recommend="yes", irreversible="push", now=self.m(3))
            self.assertEqual(push["state"], "pending")  # irreversible: the developer decides
            pg.set_mode("auto", approve="push", now=self.m(4))
            again = pg.crew_decision("Push the fix branch?", recommend="yes", irreversible="push", now=self.m(5))
            self.assertEqual(again["state"], "decided")  # pre-approved
            over = pg.crew_decide("D1", "B: reject", now=self.m(6))  # the developer overrides an automatic choice
            self.assertEqual((over["answer"], over["overrides"]), ("B: reject", "A"))
            with self.assertRaises(pg.StepError):
                pg.crew_decision("Pick one", options="A | B", recommend="C", now=self.m(7))
            with self.assertRaises(pg.StepError):
                pg.set_mode("auto", approve="launch-missiles")


class CrewSafetyTests(unittest.TestCase):
    def test_a_subagent_cannot_act_as_morpheus_through_a_compound_command(self):
        from devkit import hooks
        sub = {"agent_id": "a1", "tool_name": "Bash"}
        for cmd in ('cd lib && dev progress step "x" --check y', 'dev crew decide D1 "A"; echo ok',
                    'echo $(dev crew mode auto)'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(hooks._route_subagent_progress(dict(sub, tool_input={"command": cmd})))
                self.assertIn("command of its own", hooks._unroutable_lane_command(dict(sub, tool_input={"command": cmd})))
        self.assertIsNone(hooks._unroutable_lane_command({"tool_name": "Bash", "tool_input": {
            "command": "cd lib && dev progress step x"}}))  # the main session may chain commands
        self.assertIsNone(hooks._unroutable_lane_command(dict(sub, tool_input={"command": "git status && ls"})))

    def test_quoted_operators_and_bare_commands_are_routed(self):
        from devkit import hooks
        sub = {"agent_id": "a1", "tool_name": "Bash"}
        for cmd in ('dev progress start "Docs" --plan "guide; diagram; check"', "dev progress",
                    "dev progress step 'a && b' --check 'x | y'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(hooks._route_subagent_progress(dict(sub, tool_input={"command": cmd}))["command"],
                                 f"DEVKIT_FROM_AGENT=a1 {cmd}")
        for cmd in ('dev progress step "$(rm -rf build)"', 'dev progress step "`id`"', "dev progress step x\nrm -rf b",
                    'dev progress step "x" > /etc/passwd'):
            with self.subTest(cmd=cmd):  # expansions run inside double quotes; a newline separates commands
                self.assertIsNone(hooks._route_subagent_progress(dict(sub, tool_input={"command": cmd})))
        self.assertIsNone(hooks._unroutable_lane_command(dict(sub, tool_input={"command": "dev progress"})))

    def test_a_subagent_cannot_change_the_main_plan(self):
        with TempState():
            pg.start("Release", plan=["build", "docs", "ship"], now=time.time())
            run = pg.add_step("ci-cd guide=20m", from_agent="g1")
            self.assertEqual([s["name"] for s in run["plan"]], ["build", "docs", "ship"])  # main plan untouched
            self.assertIn("ci-cd guide", run["agents"]["g1"]["plan"])  # the lane's own plan
            for act in (lambda: pg.reopen("build", "why", from_agent="g1"),
                        lambda: pg.reopen("ship", "why", skip=True, from_agent="g1"),
                        lambda: pg.assign("ship", "Tank", from_agent="g1"),
                        lambda: pg.begin("ship", from_agent="g1")):
                with self.assertRaises(pg.StepError) as e:
                    act()
                self.assertIn("dev crew ask", str(e.exception))

    def test_review_only_members_cannot_edit_in_any_agent(self):
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as repo:
            pg.start("Review", plan=["review"], now=time.time())
            pg.note_agent_launch("claude", {"session_id": "s1", "tool_input": {"description": "Seraph: review"}})
            pg.on_subagent("claude", {"session_id": "s1", "agent_id": "r1", "agent_type": "seraph"}, True)
            edit = {"session_id": "s1", "agent_id": "r1", "tool_name": "apply_patch", "cwd": repo,
                    "tool_input": {"input": "*** Begin Patch\n*** Update File: src/a.py\n"}}
            self.assertIn("review-only", hooks._scope_violation("codex", dict(edit)) or
                          hooks._scope_violation("claude", edit))

    def test_launch_lines_are_safely_quoted(self):
        from devkit import dispatch
        line = dispatch.launch_line({"label": "Tank", "name": "api", "task": 'add "export" for $USER; rm -rf /',
                                     "scope": ["services/**"]})
        self.assertTrue(line.startswith("dev crew brief tank "))
        self.assertIn("'add \"export\" for $USER; rm -rf /'", line)


if __name__ == "__main__":
    unittest.main()
