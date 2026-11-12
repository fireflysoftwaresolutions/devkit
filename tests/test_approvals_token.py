# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""An approval counts only after a clean pass of the crew gates (spec F6). gates.check writes a pass token, the agent
id and the time its stop passed, only when it judged that stop and found nothing missing (tests/test_crew_gates.py,
PassTokenTests). At the lane's end, a review step's Check and a member's `dev progress step --approves` approve only
with the token of that stop. Without one (sent back and let through, findings only recorded, a crash before or in the
gates, a turn devkit stays out of, a session record too busy to take the token) nothing approves, and the work stays
owed. Run: python3 -m unittest tests.test_approvals_token"""
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import canary, gates, hooks, lessonevents, state  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402


class StopCase(unittest.TestCase):
    # The Backend engineer builds "api" and closes it itself; the Code reviewer reviews it in "review" and closes that
    # itself too, which keeps the review running until its lane ends. Each stop goes through the SubagentStop hook:
    # the canary, the crew gates, then the lane's end.
    REPORT = ("**[Firefly> DevKit · Code reviewer (Seraph)]** api · `main` · review\n- **Done:** read the diff\n"
              "- **Check:** ✓ read the diff, 9 tests pass\n- **Next:** the tech lead merges")

    def at(self, minutes):
        return self.t0 + minutes * 60

    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600  # the hook ends a lane at the real time, so the work comes before it
        pg.start("Work", plan="api=10m; review=10m after api", now=self.at(0))
        pg.assign("api", "Tank", now=self.at(0))
        launch("a_tank", "devkit:tank", "Tank: api", self.at(1))
        pg.step("api built", which="api", check="✓ 12 tests pass", from_agent="a_tank", now=self.at(10))
        pg.assign("review", "Seraph", now=self.at(11))
        launch("a_seraph", "devkit:seraph", "Code reviewer (Seraph): review", self.at(12))
        pg.step("review done", which="review", check="✓ read the diff, 9 tests pass", from_agent="a_seraph",
                now=self.at(20))

    def run_(self):
        return pg.load_run("claude", "s1")

    def plan(self):
        return {s["name"]: s for s in self.run_()["plan"]}

    def session_file(self):
        return pg.SESSIONS / "claude-s1.json"

    def tokens(self):
        return json.loads(self.session_file().read_text()).get(gates.PASSED) or {}

    def transcript(self, *commands):
        # the Code reviewer's own transcript: the shell commands it ran. Without `dev progress step` among them the
        # crew gates send its report back once, then let the next stop through
        tr = Path(self.state.tmp.name) / "agent-a_seraph.jsonl"
        records = []
        for i, command in enumerate(commands, 1):
            records += [{"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [
                            {"type": "tool_use", "id": f"toolu_{i:04}", "name": "Bash", "input": {"command": command}}]}},
                        {"type": "user", "isSidechain": True, "message": {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": f"toolu_{i:04}", "content": "ok"}]}}]
        tr.write_text("".join(json.dumps(r) + "\n" for r in records))
        return tr

    def passing(self):
        # a transcript the crew gates pass cleanly
        tr = self.transcript('dev progress step "review done" --step review --check "✓ read the diff, 9 tests pass"')
        self.assertEqual(gates.crew_gates("seraph", str(tr), ""), [])
        return tr

    def stop(self, tr=None, **extra):
        # the Code reviewer's SubagentStop through the hook
        root = Path(self.state.tmp.name)
        data = {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph",
                "agent_transcript_path": str(tr or self.transcript()), "last_assistant_message": self.REPORT, **extra}
        out = io.StringIO()
        with mock.patch.object(hooks, "STATE", root), mock.patch.object(canary, "LOG", root / "canary.jsonl"), \
                mock.patch.object(lessonevents, "EVENTS", root / "lessons-events.jsonl"), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.subagent_stop("claude")
        return out.getvalue()

    def assert_review_approves_nothing(self):
        # the review ended without a pass of the gates for this stop: unverified, and the work it follows still owed
        self.assertEqual(self.plan()["review"]["state"], "unverified")
        why = [h for h in self.run_()["history"] if h["step"] == "review"][-1]["why"]
        self.assertIn("crew gates did not pass its report", why)
        self.assert_owed()

    def assert_owed(self):
        run = self.run_()
        self.assertEqual(run.get("approvals") or [], [])
        self.assertIn(("seraph", ["api"]), pg.approvals_owed(run))
        with self.assertRaises(pg.StepError) as e:  # force: only the approval stands in the way
            pg.step("all done", kind="done", force=True)
        self.assertIn("not approved yet", str(e.exception))


class MemberVerdictTests(StopCase):
    # A member's `dev progress step --check "✓ …" --approves api` from its lane is kept in the lane and counts at the
    # lane's end, only with the crew gates' pass token for that stop, as a review step's Check does.
    def verdict(self):
        return pg.step("verdict", check="✓ read the diff", from_agent="a_seraph", approves="api", now=self.at(21))

    def test_a_verdict_from_a_lane_the_gates_send_back_approves_nothing(self):
        run = self.verdict()
        self.assertEqual(run.get("approvals") or [], [])  # held in the lane, not recorded yet
        self.assertIn("crew gates", run["lane_outcome"]["why"])
        self.assertEqual(json.loads(self.stop() or "{}").get("decision"), "block")
        self.assertEqual(self.stop(stop_hook_active=True), "")  # let through without a pass
        why = [h for h in self.run_()["history"] if h["step"] == "api"][-1]["why"]
        self.assertIn("not counted", why)
        self.assert_review_approves_nothing()

    def test_skipping_the_review_after_a_send_back_leaves_the_work_owed(self):
        self.verdict()
        self.stop()
        self.stop(stop_hook_active=True)
        pg.reopen("review", why="the review lane ended", skip=True)
        with self.assertRaises(pg.StepError) as e:
            pg.step("all done", kind="done")
        self.assertIn("not approved yet", str(e.exception))
        self.assertEqual(self.run_().get("approvals") or [], [])

    def test_a_verdict_from_a_lane_lands_once_its_report_passes_the_gates(self):
        self.verdict()
        self.assertEqual(self.run_().get("approvals") or [], [])
        self.assertEqual(self.stop(self.passing()), "")
        mine = [a for a in self.run_()["approvals"] if a["via"] == "step"]
        self.assertEqual([(a["by"], a["steps"], a["verdict"], a["ts"]) for a in mine],
                         [("seraph", ["api"], "approved", self.at(21))])  # as given, at the time it was given
        self.assertEqual(pg.approvals_owed(self.run_()), [])
        self.assertNotIn("a_seraph", self.tokens())  # spent on this stop


class ReviewTokenTests(StopCase):
    # A review step's Check approves only with the pass token of the stop that ends its lane: any path that never
    # reaches a clean pass of gates.check, or whose token the session record never took, approves nothing.
    def test_a_crash_before_the_gates_approves_nothing(self):
        tr = self.passing()
        with mock.patch.object(canary, "check_subagent", side_effect=RuntimeError("the canary broke")):
            self.assertEqual(self.stop(tr), "")
        self.assert_review_approves_nothing()

    def test_a_turn_devkit_stays_out_of_approves_nothing(self):
        state.update(self.session_file(), lambda s: s.update(turn_class="other"))
        self.assertEqual(self.stop(self.passing()), "")
        self.assert_review_approves_nothing()

    def test_a_pass_the_busy_session_record_could_not_take_approves_nothing(self):
        tr = self.passing()
        with mock.patch.object(state, "patch", side_effect=state.StateBusy("held by another hook")):
            self.assertEqual(self.stop(tr), "")  # the hook drops its write and goes on
        self.assert_review_approves_nothing()
        self.assertNotIn("a_seraph", self.tokens())

    def test_a_token_left_from_an_earlier_stop_approves_nothing(self):
        state.update(self.session_file(), lambda s: s.update({gates.PASSED: {"a_seraph": self.at(5)}}))
        with mock.patch.object(canary, "check_subagent", side_effect=RuntimeError("the canary broke")):
            self.stop(self.passing())  # the gates never judge this stop
        self.assert_review_approves_nothing()

    def test_a_token_written_after_a_stop_does_not_approve_that_stop(self):
        # a stop applied late (its hook found the session busy) never takes the pass of a later stop
        state.update(self.session_file(), lambda s: s.update({gates.PASSED: {"a_seraph": self.at(40)}}))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph",
                                  "last_assistant_message": self.REPORT}, False, now=self.at(30))
        self.assert_review_approves_nothing()


class NonReviewCloseTests(unittest.TestCase):
    # The Release engineer builds the pipeline (cicd), which the Security engineer approves; the Security engineer's
    # own threat model (security) is not a review step. Its close lands at once, while the verdict the same command
    # gives on the pipeline waits for the crew gates.
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        t0 = time.time() - 3600
        pg.start("Work", plan="pipeline=10m; threat=10m after pipeline", now=t0)
        pg.assign("pipeline", "Link", activity="cicd", now=t0)
        launch("a_link", "devkit:link", "Release engineer (Link): pipeline", t0 + 60)
        pg.step("pipeline built", which="pipeline", check="✓ 3 jobs green locally", from_agent="a_link",
                now=t0 + 600)
        pg.assign("threat", "Keymaker", activity="security", now=t0 + 660)
        launch("a_key", "devkit:keymaker", "Security engineer (The Keymaker): threat", t0 + 720)
        self.t0 = t0

    def test_a_members_own_close_of_a_non_review_step_lands_at_once_while_its_verdict_waits(self):
        run = pg.step("threat model done", which="threat", check="✓ 4 threats, each with a control",
                      from_agent="a_key", approves="pipeline", now=self.t0 + 1200)
        threat = next(s for s in run["plan"] if s["name"] == "threat")
        self.assertEqual(threat["state"], "done")
        self.assertTrue(run["lane_outcome"]["closed"])
        self.assertEqual(run.get("approvals") or [], [])
        self.assertIn(("keymaker", ["pipeline"]), pg.approvals_owed(run))


if __name__ == "__main__":
    unittest.main()
