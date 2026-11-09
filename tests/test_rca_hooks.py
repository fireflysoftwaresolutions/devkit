# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The patch-loop stop: when an agent runs the same command and it fails twice in a row with the same error, the
# failure hook tells it once to stop changing code and find the root cause first, with the lessons and devkit bugs
# that match the error; the lessons it shows count as shown on the output channel, like every other lesson a hook
# shows; and a third identical failure in a crew member's lane tells the tech lead once. The same command means the
# same command words, whatever paths its arguments name; the same error means the same error lines, whatever paths,
# numbers or one-time codes they carry.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_rca_hooks
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import bugs, core, lessons  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_lessons_hooks import LessonHookCase  # noqa: E402

STOP = ("The same failure twice. Stop changing code and find the root cause first (the root-cause skill): reproduce "
        "it, check the known lessons and devkit bugs below, prove the cause, then fix.")
NOTHING_KNOWN = "No known lesson or devkit bug matches this error yet."
CHECKSUM = "[ERROR] Validate failed: checksum mismatch for migration version {v} in {path} after {ms} ms"


class PatchLoopCase(LessonHookCase):
    def failing(self, cmd, text, tid, aid=None, interrupted=False):
        # A shell call that failed, as Claude Code reports it; -> the context the hook added
        d = dict(self.bash(cmd, aid=aid, tid=tid), hook_event_name="PostToolUseFailure", error="Exit code 1\n" + text,
                 is_interrupt=interrupted)
        return self.context(self.hook("post-tool-failure", d))

    def succeed(self, cmd, tid):
        d = dict(self.bash(cmd, tid=tid), hook_event_name="PostToolUse",
                 tool_response={"stdout": "BUILD SUCCESS", "stderr": ""})
        return self.context(self.hook("post-tool", d))

    def checksum(self, tid, aid=None, v=3):
        return self.failing(f"./mvnw -f /work/app-{tid}/pom.xml flyway:validate",
                         CHECKSUM.format(v=v, path=f"/work/app-{tid}/V{v}__orders.sql", ms=100 + v), tid, aid=aid)

    def run_with_lane(self, aid, persona):
        pg.RUNS.mkdir(parents=True, exist_ok=True)
        run = {"v": 2, "agent": "claude", "session": "s1", "goal": "Fix the migrations", "state": "running",
               "intervals": [], "plan": [], "agents": {aid: {"persona": persona, "state": "running", "parent": "main"}}}
        (pg.RUNS / "claude-s1.json").write_text(json.dumps(run))

    def lead_notices(self):
        run = pg.load_run("claude", "s1") or {}
        return [m for m in run.get("messages") or [] if m.get("to") == "main" and m.get("kind") == "notice"]


class StopTests(PatchLoopCase):
    def test_the_second_identical_failure_adds_the_root_cause_paragraph_with_the_matched_lesson(self):
        lid = self.lesson("Validated the migrations without the database", errors=["checksum mismatch for migration"])
        first = self.checksum("t1", v=3)
        second = self.checksum("t2", v=4)  # other paths in the command and the output, other numbers: the same failure
        self.assertNotIn(STOP, first)
        self.assertIn(STOP, second)
        self.assertIn(f"- Lesson {lid} (recorded by", second.split(STOP, 1)[1])

    def test_the_paragraph_comes_once_per_command_in_a_session(self):
        contexts = [self.checksum(f"t{i}") for i in range(1, 5)]
        self.assertEqual(sum(c.count(STOP) for c in contexts), 1)
        self.assertIn(STOP, contexts[1])
        npm = [self.failing("npm test", "Error: Cannot find module 'left-pad'", f"n{i}") for i in range(1, 3)]
        self.assertIn(STOP, npm[1])  # another command has its own

    def test_another_error_is_not_the_same_failure_until_it_comes_twice_itself(self):
        self.checksum("t1")
        refused = ["Connection refused: localhost:5432", "Connection refused: localhost:5433"]
        outs = [self.failing("./mvnw -f /work/app/pom.xml flyway:validate", text, f"t{i}")
                for i, text in enumerate(refused, 2)]
        self.assertNotIn(STOP, outs[0])
        self.assertIn(STOP, outs[1])

    def test_another_command_with_the_same_error_is_not_the_same_failure_until_it_fails_twice_itself(self):
        self.checksum("t1")
        outs = [self.failing(f"./mvnw -f /work/{tid}/pom.xml flyway:migrate", CHECKSUM.format(v=3, path="/w/V3.sql",
                                                                                            ms=1), tid)
                for tid in ("t2", "t3")]
        self.assertNotIn(STOP, outs[0])
        self.assertIn(STOP, outs[1])

    def test_a_success_of_the_command_in_between_starts_the_count_again(self):
        self.checksum("t1")
        self.succeed("./mvnw -f /work/app/pom.xml flyway:validate", "t2")
        self.assertNotIn(STOP, self.checksum("t3"))
        self.assertIn(STOP, self.checksum("t4"))

    def test_an_interrupted_call_is_no_failure(self):
        self.checksum("t1")
        stopped = self.failing("./mvnw -f /work/app/pom.xml flyway:validate", CHECKSUM.format(v=3, path="/w", ms=1),
                               "t2", interrupted=True)
        self.assertNotIn(STOP, stopped)
        self.assertIn(STOP, self.checksum("t3"))

    def test_each_agent_counts_its_own_failures(self):
        self.checksum("t1")  # the main session
        self.assertNotIn(STOP, self.checksum("t2", aid="a_tank"))
        self.assertIn(STOP, self.checksum("t3", aid="a_tank"))

    def test_with_nothing_known_about_the_error_the_paragraph_says_so(self):
        self.failing("make build", "fatal: the linker gave up on target x86_64", "t1")
        out = self.failing("make build", "fatal: the linker gave up on target x86_64", "t2")
        self.assertIn(STOP + "\n" + NOTHING_KNOWN, out)


class LessonTrackingTests(PatchLoopCase):
    def test_the_lessons_the_paragraph_shows_are_recorded_as_shown_on_the_output_channel(self):
        lid = self.lesson("Validated the migrations without the database", errors=["checksum mismatch for migration"])
        self.checksum("t1")  # the output recall shows it once an hour; the paragraph shows it anyway
        second = self.checksum("t2")
        self.assertEqual(second.count(lid), 1)
        self.assertEqual(lessons.usage()[lid]["shown_output"], 2)
        self.assertEqual(self.events(lid), [("shown", "output", "main"), ("missed", "output", "main"),
                                            ("shown", "output", "main")])

    def test_a_lesson_the_paragraph_lists_is_not_listed_or_counted_twice(self):
        self.checksum("t1")
        lid = self.lesson("Validated the migrations without the database", errors=["checksum mismatch for migration"])
        second = self.checksum("t2")  # the output recall would show it too
        self.assertEqual(second.split(STOP, 1)[1].count(lid), 1)
        self.assertEqual(second.count(lid), 1)
        self.assertEqual(lessons.usage()[lid]["shown_output"], 1)


class DevkitBugTests(PatchLoopCase):
    def test_a_devkit_failure_names_its_bug_from_the_registry(self):
        fp = bugs.fingerprint("crash", exc_class="KeyError", frames=("devkit/progress.py:close_step",))
        bugs.BUGS.mkdir(parents=True, exist_ok=True)
        (bugs.BUGS / "cache.json").write_text(json.dumps({"ts": time.time(), "etag": "", "bugs": {fp: {
            "number": 41, "state": "open", "record": {"title": "progress step crashes on a closed step",
                                                      "status": "open", "fixed_in": "", "detours": []}}}}))
        outs = []
        for tid in ("t1", "t2"):  # each crash prints a marker with a fresh one-time code
            occ = bugs.record_occurrence("crash", fp, command="progress step", exc_class="KeyError", session="s1")
            outs.append(self.failing('dev progress step "migrations" --check "validate ran"',
                                  bugs.notice(occ, bugs.match(fp, core.VERSION)), tid))
        self.assertNotIn(STOP, outs[0])
        listed = outs[1].split(STOP, 1)[1]
        self.assertIn(f"- devkit bug #41 (`{fp}`): progress step crashes on a closed step.", listed)
        self.assertIn(f"`dev bug show {fp}`", listed)


class LeadNoticeTests(PatchLoopCase):
    def test_a_third_identical_failure_in_a_crew_lane_sends_the_tech_lead_one_notice(self):
        self.run_with_lane("a_neo", "Neo")
        self.checksum("t1", aid="a_neo")
        self.checksum("t2", aid="a_neo")
        self.assertEqual(self.lead_notices(), [])  # twice: the member is told, not the lead
        self.checksum("t3", aid="a_neo")
        self.checksum("t4", aid="a_neo")
        notices = self.lead_notices()
        self.assertEqual(len(notices), 1, notices)
        self.assertEqual(notices[0]["from"], "devkit")
        self.assertIn("Senior engineer (Neo) hit the same failure a third time", notices[0]["text"])
        self.assertIn("`./mvnw -f /work/app-t3/pom.xml flyway:validate`", notices[0]["text"])
        self.assertIn('dev crew ask --to neo "…"', notices[0]["text"])

    def test_only_a_crew_lane_failing_a_third_time_sends_a_notice(self):
        self.run_with_lane("a_neo", "Neo")
        for tid in ("t1", "t2", "t3"):
            self.checksum(tid)  # the main session: it is the tech lead
        self.assertEqual(self.lead_notices(), [])
        for tid in ("t4", "t5", "t6"):
            self.checksum(tid, aid="a_neo")
        self.assertEqual(len(self.lead_notices()), 1)


if __name__ == "__main__":
    unittest.main()
