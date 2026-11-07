# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The enforced rows of crew.ENFORCEMENT that no test named (selfcheck D1, crew review 2 R2-5): each test calls the
# row's function and the place that enforces it, so it fails when the check is removed. Tests use devkit's own roster
# and a handoff repository of their own, never the developer's. Run: python3 -m unittest tests.test_enforced_rows
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import core, crew, guard, progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

T0 = 1_800_000_000


def m(minutes):
    return T0 + minutes * 60


class EnforcedRowCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.handoff = Path(self.tmp.name) / "handoff"
        self.handoff.mkdir()
        self.state = TempState()
        self.state.__enter__()
        self.p = [mock.patch.object(core, "HANDOFF", self.handoff),  # no team changes: devkit's roster
                  mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "ana", "GIT_AUTHOR_EMAIL": "ana@example.com",
                                               "GIT_COMMITTER_NAME": "ana", "GIT_COMMITTER_EMAIL": "ana@example.com",
                                               "DEVKIT_NO_AUTOSYNC": "1"})]
        for x in self.p:
            x.start()
        crew._CACHE.clear()

    def tearDown(self):
        for x in reversed(self.p):
            x.stop()
        crew._CACHE.clear()
        self.state.__exit__(None, None, None)
        self.tmp.cleanup()


class ApprovalsOwedTests(EnforcedRowCase):
    # E-approvals: dev progress done refuses while approvals_owed names an Accountable member who has not approved
    def test_approvals_owed_names_the_approver_and_stops_the_run(self):
        pg.start("API", plan=["build the JSON API server"], now=m(0))
        pg.assign("build the JSON API server", "Tank", now=m(0))
        launch("a1", "devkit:tank", "Tank: build the JSON API server", m(1))
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:tank",
                                  "last_assistant_message": "**[Firefly> DevKit · Crew member]** api · `main` · step\n- **Done:** the step\n- **Check:** ✓ its tests pass\n- **Next:** Morpheus"}, False, now=m(9))  # with its evidence (F6)
        self.assertEqual(pg.approvals_owed(pg.load_run("claude", "s1")), [("seraph", ["build the JSON API server"])])
        with self.assertRaises(pg.StepError) as e:
            pg.step("API shipped", kind="done", force=True, now=m(10))
        self.assertIn("not approved yet", str(e.exception))
        self.assertIn("Seraph", str(e.exception))
        self.assertNotEqual(pg.load_run("claude", "s1")["state"], "done")


class DepsOpenTests(EnforcedRowCase):
    # E-deps: a step whose `after` is not done yet does not start; deps_open names what it waits for
    def test_deps_open_names_the_unfinished_step_and_begin_refuses(self):
        pg.start("Work", plan=["design", "api=1h after design"], now=m(0))
        run = pg.load_run("claude", "s1")
        api = next(s for s in run["plan"] if s["name"] == "api")
        self.assertEqual(pg.deps_open(run, api), ["design"])
        with self.assertRaises(pg.StepError) as e:
            pg.begin("api", now=m(1))
        self.assertIn("waits for design", str(e.exception))
        pg.step("design done", check="the design is written", which="design", now=m(30))
        run = pg.load_run("claude", "s1")
        self.assertEqual(pg.deps_open(run, next(s for s in run["plan"] if s["name"] == "api")), [])
        self.assertEqual(next(s for s in pg.begin("api", now=m(31))["plan"] if s["name"] == "api")["state"],
                         "running")


class MainOnlyTests(EnforcedRowCase):
    # E-main-only: only Morpheus plans, assigns and changes the mode; _main_only refuses a crew member's call
    def test_main_only_refuses_a_member_and_a_members_call(self):
        with self.assertRaises(pg.StepError) as e:
            pg._main_only("tank", "sets the mode")
        self.assertIn("dev crew ask", str(e.exception))
        pg.start("Work", plan=["a"], now=m(0))
        member_call = {"caller": "toolu_tank01", "how": "ledger", "agent": "claude", "session": "s1"}
        with mock.patch.object(pg, "caller", lambda *a, **k: member_call):
            with self.assertRaises(pg.StepError) as e:
                pg.set_mode("auto", now=m(1))
            self.assertIn("only the tech lead sets the mode", str(e.exception))
        self.assertEqual(pg.mode_of(pg.load_run("claude", "s1")), "guided")  # unchanged
        self.assertEqual(pg.mode_of(pg.set_mode("auto", now=m(2))), "auto")  # Morpheus himself may


class ForgedEnvReasonTests(unittest.TestCase):
    # E-forged-env: the guard refuses a devkit call with a forged HOME or DEVKIT_* variable, with forged_env_reason
    def test_forged_env_reason_is_the_guards_answer_to_a_forged_home(self):
        v = guard.check("Bash", {"command": "HOME=/tmp/e-forged dev progress done shipped"}, str(core.DEV), "claude")
        self.assertEqual(v[0], "deny")
        self.assertIn(guard.forged_env_reason(["HOME"], "a devkit command").split(". ")[0], v[1])
        v = guard.check("Bash", {"command": "DEVKIT_SESSION=claude:main dev crew answer 1 ok"}, str(core.DEV),
                        "claude")
        self.assertEqual(v[0], "deny")
        self.assertIn("DEVKIT_SESSION", v[1])
        self.assertIsNone(guard.check("Bash", {"command": "HOME=/tmp/e-forged npm test"}, str(core.DEV), "claude"))


class TeamWriteTests(EnforcedRowCase):
    # E-team-reason: _team_write refuses a crew change without its reason and records the reason of one with it
    def test_team_write_needs_a_reason_and_records_it(self):
        subprocess.run(["git", "init", "-q", str(self.handoff)], check=True)
        with self.assertRaises(ValueError) as e:
            crew.set_raci("frontend", {"C": ["sati"]}, "  ")
        self.assertIn("needs its reason", str(e.exception))
        self.assertFalse(crew.overlay_path().exists())
        crew.set_raci("frontend", {"C": ["sati"]}, "Sati reviews every screen", by="ana@mbp")
        ov = core.read_json(crew.overlay_path(), {})
        self.assertEqual(ov["changelog"][-1]["why"], "Sati reviews every screen")
        with self.assertRaises(ValueError):
            crew._team_write(ov, "direct", "")


if __name__ == "__main__":
    unittest.main()
