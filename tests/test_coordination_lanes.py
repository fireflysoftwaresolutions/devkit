# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Crew lanes and the plan steps they hold: how a member's record closes a step, which steps a lane takes when its
step closes or it is resumed, whose files a lane may write, and when two scopes clash. Each test reproduces a fault
the crew hit on devkit 26.11.25 (2026-11-19), at the function that decides it, with the inputs of that day.
Run: python3 -m unittest tests.test_coordination_lanes"""
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import progress as pg  # noqa: E402
from test_approvals_token import StopCase  # noqa: E402
from test_progress import TempState, launch, member_call, progress_cli_out  # noqa: E402

KID_REPORT = ("**[Firefly> DevKit · DevEx engineer (Kid)]** devkit · `fix/open-bugs` · {task}\n- **Done:** {task}\n"
              "- **Check:** ✓ 605 tests pass\n- **Next:** the Code reviewer reviews it")


class LaneCase(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.t0 = time.time() - 3600  # the work comes before the real time a hook ends a lane at

    def m(self, minutes):
        return self.t0 + minutes * 60

    def run_(self):
        return pg.load_run("claude", "s1")

    def held(self, name):
        st = next(s for s in self.run_()["plan"] if s["name"] == name)
        return st.get("owner"), st.get("state")

    def scope_of(self, aid):
        return sorted({g for layer in pg.lane_scope(self.run_(), aid) for g in layer})


class MemberRecordTests(LaneCase):
    def test_a_members_checked_record_without_step_closes_its_lanes_only_running_step(self):
        # registry #56: `dev progress step "…" --check "✓ …"` from a member printed ✓ and closed no plan step, so
        # the member reported its step done while the plan kept it running
        pg.start("Hotfix", plan=["parser=30m", "docs=15m"], now=self.m(0))
        pg.assign("parser", "Neo", scope="lib/devkit/shellparse.py", why="test", now=self.m(0))
        launch("a_neo", "devkit:neo", "Neo: parser timing fix", at=self.m(1))
        self.assertEqual(self.held("parser"), ("a_neo", "running"))
        with member_call("a_neo"):
            rc, out, err = progress_cli_out("step", "the parser times out after 2 s", "--check", "✓ 46 tests pass")
        self.assertEqual(rc, 0, err)
        self.assertIn("✓", out)
        self.assertEqual(self.held("parser"), ("a_neo", "done"), out)


class QueuedStepTests(LaneCase):
    def test_a_lane_whose_step_closes_takes_the_first_of_two_steps_assigned_to_its_member(self):
        # the DevEx engineer's three steps were assigned by role before the launch, whose description
        # named launch-warn. When launch-warn closed, the lane took neither reassign-notice nor lesson-review ("with
        # two waiting devkit gives a lane neither"), and its scope stayed launch-warn's
        pg.start("Open bugs", plan=["launch-warn=45m", "reassign-notice=45m", "lesson-review=60m"], now=self.m(0))
        pg.assign("launch-warn", "Kid", scope="lib/devkit/hooks.py", why="test", now=self.m(0))
        pg.assign("reassign-notice", "Kid", scope="lib/devkit/dispatch.py", why="test", now=self.m(0))
        pg.assign("lesson-review", "Kid", scope="lib/devkit/lessons.py", why="test", now=self.m(0))
        launch("a_kid", "devkit:kid", "Kid: launch-warn hook notice", at=self.m(1))
        self.assertEqual(self.held("launch-warn"), ("a_kid", "running"))
        pg.step("launch-warn committed", which="launch-warn", check="✓ 605 tests pass", from_agent="a_kid",
                now=self.m(20))
        self.assertEqual((self.held("reassign-notice"), self.held("lesson-review")),
                         (("a_kid", "running"), ("main", "pending")))
        self.assertEqual(self.scope_of("a_kid"), ["lib/devkit/dispatch.py"])


class BlockedStepTests(LaneCase):
    def test_a_blocked_step_leaves_its_lane_free_for_the_members_next_step_and_its_files(self):
        # the Release engineer's copy-release was blocked on the Actions budget; ci-redirect, assigned to him next, only
        # "joins your lane once copy-release closes", so dev commit refused ci-redirect's files and his
        # `dev progress step --step ci-redirect` was refused
        pg.start("Release", plan=["copy-release=60m", "ci-redirect=60m"], now=self.m(0))
        pg.assign("copy-release", "Link", scope="docs/copy.md", why="test", now=self.m(0))
        launch("a_link", "devkit:link", "Link: copy-release fork release", at=self.m(1))
        pg.step("PR #20's run started no job: the Actions budget is spent", kind="blocked", which="copy-release",
                now=self.m(30))
        self.assertEqual(self.held("copy-release"), ("a_link", "blocked"))
        pg.assign("ci-redirect", "Link", scope="lib/devkit/gitx.py,tests/test_gitx.py", why="test", now=self.m(31))
        self.assertEqual(self.held("ci-redirect"), ("a_link", "running"))
        self.assertEqual(self.scope_of("a_link"), ["lib/devkit/gitx.py", "tests/test_gitx.py"])


class ResumedLaneTests(LaneCase):
    def test_a_resumed_lane_takes_the_one_step_assigned_to_its_member_by_role(self):
        # a step assigned by role to a member whose lane had ended stayed the tech lead's when the member was resumed
        # with SendMessage, so the member worked read-only and its first writes were credited as unverified
        pg.start("Hotfix", plan=["test-guard=30m", "hermetic-fix=20m"], now=self.m(0))
        pg.assign("test-guard", "Mouse", scope="tests/_hermetic.py", why="test", now=self.m(0))
        launch("a_mouse", "devkit:mouse", "Mouse: test-guard detached sync refusal", at=self.m(1))
        pg.step("test-guard committed", which="test-guard", check="✓ 27 tests pass", from_agent="a_mouse",
                now=self.m(20))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_mouse", "agent_type": "devkit:mouse",
                                  "last_assistant_message": KID_REPORT.format(task="test-guard")}, False,
                       now=self.m(21))
        pg.assign("hermetic-fix", "Mouse", scope="tests/test_hermetic.py", why="test", now=self.m(25))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_mouse", "agent_type": "devkit:mouse"}, True,
                       now=self.m(26))
        self.assertEqual(self.held("hermetic-fix"), ("a_mouse", "running"))
        self.assertEqual(self.scope_of("a_mouse"), ["tests/test_hermetic.py"])


class ScopeClashTests(LaneCase):
    def test_a_glob_of_files_does_not_clash_with_other_files_it_cannot_hold(self):
        # `docs/**/bug*.md` was refused as overlapping the DevEx engineer's docs/crew.md and
        # docs/agents.md, which no path it holds can be
        pg.start("Docs", plan=["launch-warn=45m", "registry-docs=30m"], now=self.m(0))
        pg.assign("launch-warn", "Kid", scope="docs/crew.md,docs/agents.md", why="test", now=self.m(0))
        launch("a_kid", "devkit:kid", "Kid: launch-warn hook notice", at=self.m(1))
        try:
            pg.assign("registry-docs", "Ghost", scope="docs/**/bug*.md", why="test", now=self.m(2))
        except pg.StepError as e:
            self.fail(f"refused as a clash: {e}")
        self.assertEqual(next(s for s in self.run_()["plan"] if s["name"] == "registry-docs")["scope"],
                         ["docs/**/bug*.md"])


class ReviewAfterSendBackTests(StopCase):
    # The Code reviewer's final review and the Product owner's tabs-approve: the crew gates sent the member back once,
    # its next stop was let through, the tech lead reopened the step and resumed the member, who recorded it with
    # `dev progress step --step review --approves api` and a ✓ Check and stopped with a report the gates pass. The
    # review stayed unverified ("the crew gates did not pass its report"), and the work it approves stayed owed
    def test_a_resumed_reviewers_clean_report_approves_after_an_earlier_send_back(self):
        self.assertEqual(self.stop()[:1], "{")  # sent back: the transcript records no step
        self.stop(stop_hook_active=True)  # let through without a pass
        self.assertEqual(self.plan()["review"]["state"], "unverified")
        pg.reopen("review", why="the reviewer is resumed to give the verdict again", now=self.at(30))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_seraph", "agent_type": "devkit:seraph"}, True,
                       now=self.at(31))
        self.assertEqual(self.plan()["review"]["state"], "running")
        pg.step("review done", which="review", check="✓ read the diff, 9 tests pass", from_agent="a_seraph",
                approves="api", now=self.at(40))
        self.stop(self.passing())
        why = [h["why"] for h in self.run_()["history"] if h["step"] == "review"][-1]
        self.assertEqual(self.plan()["review"]["state"], "done", why)
        self.assertEqual(pg.approvals_owed(self.run_()), [])


if __name__ == "__main__":
    unittest.main()
