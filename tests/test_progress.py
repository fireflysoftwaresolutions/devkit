# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Progress, timing and predictability. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import canary, core  # noqa: E402
from devkit import progress as pg  # noqa: E402


def boundary(root, real_caller=False) -> list:
    # what every progress test fakes, as patchers to start and stop: devkit's configuration (its defaults, never the
    # developer's ~/.config/devkit), this test process's real parents (they never name a session), the call ledger
    # (in the test's folder) and who calls: Morpheus, from a call of his. real_caller: the tests of caller identity
    # (tests/test_caller_identity.py) use devkit's own ledger and session lookup instead.
    out = [mock.patch.dict(core.CFG, {"progress": {}, "work": {}}),
           mock.patch.object(pg, "_ancestor_pids", lambda: [], create=True),
           mock.patch.object(pg, "CALLS", Path(root) / "calls", create=True)]
    if not real_caller:
        out.append(mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}, create=True))
    return out


class TempState:
    def __init__(self, real_caller=False):
        self.real_caller = real_caller

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        from devkit import audit
        self.saved = (pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg.QUOTA, pg._publish,
                      os.environ.get("DEVKIT_SESSION"))
        self.audit_saved = audit.AUDIT
        audit.AUDIT = root / "audit"  # tests never write to the real audit trail
        pg.RUNS, pg.TIMING, pg.SESSIONS = root / "runs", root / "timing", root / "sessions"
        pg.PENDING = pg.TIMING / "pending"
        pg.QUOTA = root / "quota"  # the real 5-hour usage never changes the parallel limit under test
        self.boundary = boundary(root, self.real_caller)
        for p in self.boundary:
            p.start()
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
        pg.RUNS, pg.TIMING, pg.PENDING, pg.SESSIONS, pg.QUOTA, pg._publish, env = self.saved
        for p in reversed(self.boundary):
            p.stop()
        if env is None:
            os.environ.pop("DEVKIT_SESSION", None)
        else:
            os.environ["DEVKIT_SESSION"] = env
        self.tmp.cleanup()


def launch(aid, kind, label, at=None, sid="s1"):
    # Claude Code's sequence for one subagent launch: the Agent call (PreToolUse, with its tool_use_id), the agent's
    # start, and the call's result naming the agent it started (the lane binds to its launch exactly)
    pg.note_agent_launch("claude", {"session_id": sid, "tool_use_id": f"toolu_{aid}",
                                    "tool_input": {"description": label, "subagent_type": kind}}, now=at)
    pg.on_subagent("claude", {"session_id": sid, "agent_id": aid, "agent_type": kind}, True, now=at)
    pg.launch_result("claude", {"session_id": sid, "tool_use_id": f"toolu_{aid}", "tool_response": {"agentId": aid}},
                     now=at)


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
            launch("a1", "devkit:neo", "Neo: build the parser")
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
            launch("a1", "devkit:trinity", "Trinity: ui", self.m(1))
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
            launch("a1", "devkit:neo", "Neo: build it", self.m(1))
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
            launch("a2", "devkit:seraph", "Seraph: review the parser", self.m(5))
            self.assertIn("Review the parser", pg.on_tool_done("claude", {"session_id": "s1", "agent_id": "a2"}))

    def test_scope_follows_the_member_into_a_worktree_of_the_same_repository(self):
        import subprocess
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as d:
            main, wt, other = Path(d) / "api", Path(d) / "wt", Path(d) / "other"
            for r in (main, other):
                subprocess.run(["git", "init", "-q", str(r)], check=True)
                subprocess.run(["git", "-C", str(r), "-c", "user.email=a@example.com", "-c", "user.name=a", "commit",
                                "-q", "--allow-empty", "-m", "init"], check=True)
            subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "feat/x", str(wt)], check=True)
            pg.start("Feature", plan=["installer"], now=self.m(0))
            pg.assign("installer", "Kid", scope="install.sh,tests/test_install*.py", now=self.m(0))
            launch("a1", "devkit:kid", "Kid: installer", self.m(1))
            edit = lambda p: {"session_id": "s1", "agent_id": "a1", "tool_name": "Edit", "cwd": str(main),  # noqa: E731
                              "tool_input": {"file_path": str(p)}}
            from unittest import mock
            real_temp = hooks.core.is_temp  # the fixture lives in a temp folder; real worktrees do not
            mock.patch.object(hooks.core, "is_temp", lambda p: real_temp(p) and not str(p).startswith(d)).start()
            self.addCleanup(mock.patch.stopall)
            self.assertIsNone(hooks._scope_violation("claude", edit(wt / "install.sh")))
            self.assertIsNone(hooks._scope_violation("claude", edit(wt / "tests" / "test_install_curl.py")))  # new folder
            self.assertIn("outside Kid's scope", hooks._scope_violation("claude", edit(wt / "lib" / "cli.py")))
            self.assertIn("outside Kid's scope", hooks._scope_violation("claude", edit(other / "install.sh")))

    def test_scope_keeps_parallel_agents_apart(self):
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as repo:
            pg.start("Feature", plan=["parser", "ui"], now=self.m(0))
            pg.assign("parser", "Neo", scope="lib/parser/**,tests/test_parser.py", now=self.m(0))
            launch("a1", "devkit:neo", "Neo: parser", self.m(1))
            edit = lambda p: {"session_id": "s1", "agent_id": "a1", "tool_name": "Edit", "cwd": repo,  # noqa: E731
                              "tool_input": {"file_path": os.path.join(repo, p)}}
            self.assertIsNone(hooks._scope_violation("claude", edit("lib/parser/core.py")))
            self.assertIsNone(hooks._scope_violation("claude", edit("tests/test_parser.py")))
            why = hooks._scope_violation("claude", edit("lib/ui/app.ts"))
            self.assertIn("outside Neo's scope", why)
            self.assertIn("dev crew ask", why)
            self.assertIsNone(hooks._scope_violation("claude", dict(edit("lib/ui/app.ts"), agent_id=None)))  # main
            with tempfile.TemporaryDirectory() as scratch:  # scratch files outside the repository are not shared
                self.assertIsNone(hooks._scope_violation("claude", edit(os.path.join(scratch, "notes.md"))))
            other = os.path.expanduser("~/Development/acme/billing/src/app.py")  # another repository is not scratch
            self.assertIn("outside Neo's scope", hooks._scope_violation("claude", edit(other)))

    def test_only_launched_subagents_owe_a_report_and_corrections_are_recorded(self):
        import contextlib
        import io
        from unittest import mock
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as d:
            saved, saved_sf = canary.LOG, hooks._session_file
            canary.LOG = Path(d) / "canary.jsonl"
            hooks._session_file = lambda agent, sid: pg.SESSIONS / f"{agent}-{sid}.json"  # noqa: E731
            gates_off = mock.patch.dict(hooks.core.CFG, {"crew": {"gates": {"enforce": False}}})
            gates_off.start()  # this test is the report canary's; the crew gates only record here (their own test
            self.addCleanup(gates_off.stop)  # is test_a_member_missing_a_proof_is_sent_back_once)

            def stop(payload):
                out = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                    hooks.subagent_stop("claude")
                return out.getvalue()
            try:
                internal = {"session_id": "s1", "agent_id": "c1", "last_assistant_message": "<analysis> The summary"}
                self.assertEqual(stop(internal), "")  # the agent's own helper (compaction): no brief, no report owed
                self.assertEqual(canary.records(1), [])
                tr = Path(d) / "agent-a1.jsonl"
                tr.write_text("")
                (Path(d) / "agent-a1.meta.json").write_text(json.dumps({"toolUseId": "toolu_1",
                                                                        "description": "Neo: parser"}))
                launched = {"session_id": "s1", "agent_id": "a1", "agent_type": "general-purpose",
                            "agent_transcript_path": str(tr), "last_assistant_message": "Done. The parser works."}
                sent_back = json.loads(stop(launched))  # launched with the Agent tool: sent back once
                self.assertEqual(sent_back["decision"], "block")
                self.assertIn("DevKit · Senior engineer (Neo)]", sent_back["reason"])  # by the name its launch gave it
                fixed = dict(launched, stop_hook_active=True, last_assistant_message=(
                    "**[Firefly> DevKit · Neo]** api · `main` · parser\n- **Done:** x\n- **Check:** \u2713 3 tests\n"
                    "- **Next:** review"))
                self.assertEqual(stop(fixed), "")
                self.assertEqual(canary.by_member(1), {"Neo": (0, 1)})  # the first report missed; it stays a miss
                self.assertEqual(canary.corrected(1), 1)  # and the correction is on record
                with mock.patch.dict(hooks.core.CFG, {"canary": {"enforce": False}}):
                    self.assertEqual(stop(dict(launched, agent_id="a2")), "")  # measured, not sent back
                self.assertEqual(canary.by_member(1), {"Neo": (0, 2)})
            finally:
                canary.LOG, hooks._session_file = saved, saved_sf

    def test_a_member_missing_a_proof_is_sent_back_once(self):
        import contextlib
        import io
        from unittest import mock
        from devkit import hooks
        with TempState(), tempfile.TemporaryDirectory() as d:
            saved, saved_sf = canary.LOG, hooks._session_file
            canary.LOG = Path(d) / "canary.jsonl"
            hooks._session_file = lambda agent, sid: pg.SESSIONS / f"{agent}-{sid}.json"  # noqa: E731

            def stop(payload):
                out = io.StringIO()
                with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
                    hooks.subagent_stop("claude")
                return out.getvalue()
            try:
                tr = Path(d) / "agent-k1.jsonl"  # Kid's own transcript: it ran the tests, never recorded its step
                tr.write_text("\n".join(json.dumps(r) for r in (
                    {"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [
                        {"type": "tool_use", "id": "toolu_0001", "name": "Bash", "input": {"command": "bin/dev test"}}]}},
                    {"type": "user", "isSidechain": True, "message": {"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_0001", "content": "ok", "is_error": False}]}},
                )) + "\n")
                (Path(d) / "agent-k1.meta.json").write_text(json.dumps({"toolUseId": "toolu_k",
                                                                        "description": "Kid: installer"}))
                report = ("**[Firefly> DevKit · DevEx engineer (Kid)]** api · `main` · installer\n- **Done:** x\n"
                          "- **Check:** ✓ 3 tests\n- **Next:** review")  # the status block is there
                data = {"session_id": "s1", "agent_id": "k1", "agent_type": "general-purpose", "cwd": d,
                        "agent_transcript_path": str(tr), "last_assistant_message": report}
                first = json.loads(stop(data) or "{}")
                self.assertEqual(first.get("decision"), "block")
                self.assertIn("dev progress step", first.get("reason", ""))
                self.assertEqual(stop(dict(data, stop_hook_active=True)), "")  # once, like the canary
            finally:
                canary.LOG, hooks._session_file = saved, saved_sf

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
                with canary.LOG.open("a") as fh:  # an agent's own helper recorded by an older devkit, marked internal
                    fh.write(json.dumps({"ts": time.time(), "kind": "crew", "member": "Agent", "ok": False,
                                         "internal": True}) + "\n")
                self.assertEqual(canary.by_member(1), {"Neo": (1, 2)})
                self.assertEqual(canary.summary(1), {})  # crew reports are counted apart from the main replies
                self.assertIn("crew 1/2", canary.summary_line(1))
            finally:
                canary.LOG = saved


class DeclaredActivityTests(unittest.TestCase):
    # A step's kind of work rests on language-neutral anchors (spec C13, Kid's M41 and M45): an explicit activity id
    # (--activity on assign, [id] in a plan spec), the assigned member's Responsible activity, the files in its scope.
    T0 = 1_800_000_000.0

    def step(self, name):
        return next(s for s in pg.load_run("claude", "s1")["plan"] if s["name"] == name)

    def test_assign_stores_an_explicit_activity_and_checks_the_member_against_it(self):
        with TempState():
            pg.start("Feature", plan=["paso 2", "paso 3"], now=self.T0)
            pg.assign("paso 2", "Tank", activity="backend", now=self.T0)
            s = self.step("paso 2")
            self.assertEqual((s["activity"], s["activity_from"]), ("backend", "explicit"))
            with self.assertRaises(pg.StepError) as e:
                pg.assign("paso 3", "Tank", activity="ux", now=self.T0)
            self.assertIn("--to Sati", str(e.exception))
            with self.assertRaises(pg.StepError) as e:
                pg.assign("paso 3", "Tank", activity="nope", now=self.T0)
            self.assertIn("backend", str(e.exception))  # the refusal names the ids
            self.assertEqual(self.step("paso 3")["label"], "")

    def test_an_explicit_activity_stays_when_the_step_is_assigned_again(self):
        with TempState():
            pg.start("Feature", plan=["paso 2"], now=self.T0)
            pg.assign("paso 2", "Tank", activity="backend", now=self.T0)
            with self.assertRaises(pg.StepError):
                pg.assign("paso 2", "Sati", now=self.T0)  # still back-end work: Sati is not on it
            self.assertEqual(self.step("paso 2")["activity"], "backend")

    def test_files_in_scope_of_the_members_own_kind_confirm_an_assignment_the_words_would_refuse(self):
        with TempState():
            pg.start("Feature", plan=["build the JSON API server"], now=self.T0)
            pg.assign("build the JSON API server", "Kid", scope="bin/**", now=self.T0)
            s = self.step("build the JSON API server")
            self.assertEqual((s["label"], s["activity"], s["activity_from"]), ("Kid", "tooling", "member"))

    def test_a_plan_spec_declares_its_kind_of_work_after_its_dependencies(self):
        with TempState():
            pg.start("Feature", plan="design=30m; api=1h after design [backend]", now=self.T0)
            s = self.step("api")
            self.assertEqual((s["after"], s["est"], s["activity"], s["activity_from"]),
                             (["design"], 60, "backend", "explicit"))
            pg.add_step("docs=30m [docs]", now=self.T0)
            self.assertEqual(self.step("docs")["activity"], "docs")
            with self.assertRaises(pg.StepError) as e:
                pg.add_step("more=10m [nope]", now=self.T0)
            self.assertIn("no kind of work", str(e.exception))
            self.assertEqual([x["name"] for x in pg.load_run("claude", "s1")["plan"]], ["design", "api", "docs"])


class RouteTests(unittest.TestCase):
    def ids(self, task):
        from devkit import dispatch
        return [(m["id"], why) for m, _, why in dispatch.route(task)]

    def test_hyphenated_keywords_hit_only_as_whole_phrases(self):
        # live misroutes: "app-store" gave Switch the word "app", "write-up" gave Ghost the word "up"
        for task, member, word in (("upgrade the app from Spring Boot 2.7 to 3.3", "switch", "app"),
                                   ("set up Dependabot for the repo", "ghost", "up")):
            with self.subTest(task=task):
                hits = [why for mid, why in self.ids(task) if mid == member]
                self.assertFalse(any(f"keywords: {word}" in w for why in hits for w in why))
        switch = dict(self.ids("publish the release to the App Store"))
        self.assertTrue(any("app store" in w or "app-store" in w for w in switch.get("switch") or []))

    def test_a_planning_request_routes_to_morpheus_first(self):
        # planning is Morpheus's kind of work (the RACI's R); the route used to look only at the crew without him
        self.assertEqual(self.ids("resolve the merge conflicts and integrate the branches")[0][0], "morpheus")


class CrewGuardrailTests(unittest.TestCase):
    T0 = 1_800_000_000.0

    def m(self, minutes):
        return self.T0 + minutes * 60

    def launch(self, aid, kind, label, at):
        launch(aid, kind, label, at)

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
            self.assertEqual(hooks._crew_limits("claude", dict(spawn, agent_id="a0"))[0], "deny")  # a helper too

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
        launch("a1", "devkit:neo", "Neo: parser", self.m(1))

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
            pg.set_mode("auto", approve="push fix/*", now=self.m(4))
            again = pg.crew_decision("Push the fix branch?", recommend="yes", irreversible="push", now=self.m(5))
            self.assertEqual(again["state"], "pending")  # a pre-approval without the developer's grant opens nothing
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

    def test_a_note_reports_progress_without_closing_any_step(self):
        with TempState() as st:
            pg.start("Release", plan=["build", "ship"], now=time.time())
            before = [(s["name"], s["state"]) for s in pg.normalize(pg.load_run(*pg.current_session()))["plan"]]
            run = pg.note("PR open, its CI is running", nxt="merge when green")
            self.assertEqual([(s["name"], s["state"]) for s in run["plan"]], before)  # no step moved
            self.assertEqual((run["updates"][-1]["kind"], run["updates"][-1]["next"]), ("note", "merge when green"))
            self.assertIn("PR open, its CI is running", pg.render_update(run))
            self.assertGreater(json.loads(st.sess.read_text()).get("last_update") or 0, 0)  # the silence timer
            with self.assertRaises(pg.StepError):
                pg.note("   ")
            lane = pg.note("guide half written", from_agent="g1")["agents"]["g1"]
            self.assertEqual(lane["updates"][-1]["kind"], "note")  # a member's note stays in its lane

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
            launch("r1", "seraph", "Seraph: review")
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


class HookCallsTests(unittest.TestCase):
    def test_the_hooks_call_only_devkit_functions_that_exist(self):
        # Seraph M111 (4): the prompt hook called scope.resolve, lessons.note_correction and skillguard.on_prompt after
        # they were removed; each call failed inside a try, so every prompt lost its turn class and skills silently
        import ast
        import importlib
        from devkit import hooks
        tree = ast.parse(Path(hooks.__file__).read_text())

        def imports(body):  # `from . import x as y` written directly in this body (not in nested functions)
            out, todo = {}, list(body)
            while todo:
                n = todo.pop()
                if isinstance(n, ast.ImportFrom) and n.level == 1 and not n.module:
                    out.update({a.asname or a.name: a.name for a in n.names})
                elif not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    todo += list(ast.iter_child_nodes(n))
            return out
        top, missing = imports(tree.body), set()
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            local = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            local |= {a.arg for a in fn.args.args + fn.args.kwonlyargs}
            mods = {**{k: v for k, v in top.items() if k not in local}, **imports(fn.body)}
            missing |= {f"{n.value.id}.{n.attr} (line {n.lineno})" for n in ast.walk(fn)
                        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in mods
                        and not hasattr(importlib.import_module(f"devkit.{mods[n.value.id]}"), n.attr)}
        self.assertEqual(sorted(missing), [])


if __name__ == "__main__":
    unittest.main()
