# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# devkit's two work modes, as the developer meets them: auto (the default: the crew takes the recommended option of a
# decision, records it and goes on) and guided (a decision waits for the developer, and the member that asked is
# blocked). Every scenario runs the real code in the shared sandbox (test_crew's TempState: progress state, the audit
# trail and devkit's configuration file in a temporary folder) and reads the saved mode through devkit's own
# configuration reader from that sandbox's file, never a stand-in that answers "guided". The developer's
# ~/.config/devkit/config.json is never read or written: setUp refuses to run when the sandbox's configuration file
# is not in its temporary folder. Tests carry comments, not docstrings: `dev test prove` leaves out tests with a
# docstring. Run: python3 -m unittest tests.test_modes
import json
import os
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import _hermetic  # noqa: E402

_hermetic.activate()

sys.path.insert(0, str(HERE.parent / "lib"))
from devkit import audit, core  # noqa: E402
from devkit import progress as pg  # noqa: E402
import test_hooks_welcome  # noqa: E402
from test_crew import TempState, run_cli  # noqa: E402
from test_progress import NEO_REPORT, launch  # noqa: E402

READ_SAVED = core.config_snapshot  # devkit's own reader, taken before any sandbox replaces it
ASKS_THE_DEVELOPER = re.compile(r"ask the waiting decisions|ask (?:it|them) now|AskUserQuestion|ask the developer|"
                                r"decisions? waits? for (?:you|the developer)", re.I)


def below(path, root) -> bool:
    p, r = os.path.realpath(str(path)), os.path.realpath(str(root))
    return p == r or p.startswith(r + os.sep)


def use_saved_configuration(case, root):
    # the sandbox's configuration file is the saved configuration, read by devkit's own reader
    if not below(core.CONFIG_FILE, root):
        case.fail(f"the sandbox's configuration file {core.CONFIG_FILE} is outside its folder {root}: refusing to "
                  f"write a saved mode there (tests/test_sandbox_config.py)")
    p = mock.patch.object(core, "config_snapshot", READ_SAVED)
    p.start()
    case.addCleanup(p.stop)


def save(text):
    # what `dev config set` or `dev config edit` leaves on disk; None: no configuration saved at all
    if text is None:
        core.CONFIG_FILE.unlink(missing_ok=True)
        return
    core.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    core.CONFIG_FILE.write_text(text if isinstance(text, str) else json.dumps(text))


class ModeCase(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        use_saved_configuration(self, self.state.tmp.name)

    @staticmethod
    def run_():
        return pg.load_run("claude", "s1")

    @staticmethod
    def neo_works_on_the_parser():
        # the Senior engineer's lane runs the parser step, as `dev progress assign` and the launch record it
        pg.assign("parser", "Neo")
        launch("a1", "devkit:neo", "Neo: parser")

    @staticmethod
    def neo_asks():
        return run_cli("crew", "ask", "--decision", "Skip empty lines or reject the file?",
                       "--options", "A: skip them | B: reject the file", "--recommend", "A", from_agent="a1")

    @staticmethod
    def audit_records(action):
        return [r for f in sorted(audit.AUDIT.glob("*.jsonl")) for r in map(json.loads, f.read_text().splitlines())
                if r.get("action") == action]


class WhichModeTests(ModeCase):
    # with nothing saved, devkit works in auto mode, and `dev crew mode` says so
    def test_with_no_saved_configuration_the_mode_is_auto(self):
        save(None)
        self.assertEqual(("auto", "config"), pg.mode_context(None))
        rc, out, _ = run_cli("crew", "mode")
        self.assertEqual(0, rc)
        self.assertIn("Mode auto (config)", out)

    # a saved work.mode guided makes a new run guided, without any run override
    def test_a_saved_guided_mode_makes_a_new_run_guided(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.assertEqual(("guided", "config"), pg.mode_context(self.run_()))

    # `dev crew mode auto` overrides a saved guided mode for this run: a decision with a recommendation is taken
    def test_dev_crew_mode_auto_overrides_a_saved_guided_mode(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.assertEqual(0, run_cli("crew", "mode", "auto")[0])
        self.assertEqual(("auto", "run"), pg.mode_context(self.run_()))
        d = pg.crew_decision("Rewrite or patch the parser?", options="A: rewrite | B: patch", recommend="A")
        self.assertEqual(("decided", "auto"), (d["state"], d["by"]))

    # `dev crew mode guided` overrides the default auto mode for this run: a decision waits for the developer
    def test_dev_crew_mode_guided_overrides_the_saved_auto_mode(self):
        save({"work": {"mode": "auto"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.assertEqual(0, run_cli("crew", "mode", "guided")[0])
        self.assertEqual(("guided", "run"), pg.mode_context(self.run_()))
        d = pg.crew_decision("Rewrite or patch the parser?", options="A: rewrite | B: patch", recommend="A")
        self.assertEqual(("pending", ""), (d["state"], d["by"]))

    # the override lasts as long as its run: the next run follows the saved mode again
    def test_a_run_override_ends_with_its_run(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser"])
        run_cli("crew", "mode", "auto")
        pg.step("parser done", kind="done", force=True)
        pg.start("Docs", plan=["docs"])
        self.assertEqual(("guided", "config"), pg.mode_context(self.run_()))

    # `dev crew mode inherit` drops the override: the run follows the saved mode, and keeps following it when the
    # developer changes it later
    def test_dev_crew_mode_inherit_follows_the_saved_mode_again(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        run_cli("crew", "mode", "auto")
        self.assertEqual(0, run_cli("crew", "mode", "inherit")[0])
        self.assertEqual(("guided", "config"), pg.mode_context(self.run_()))
        save({"work": {"mode": "auto"}})
        self.assertEqual(("auto", "config"), pg.mode_context(self.run_()))

    # `dev progress start --mode` sets the run's mode over the saved one, and a later change of the saved mode does
    # not move it
    def test_dev_progress_start_mode_sets_the_run_mode(self):
        save(None)
        rc, _, err = run_cli("progress", "start", "Parser", "--plan", "parser; docs", "--mode", "guided")
        self.assertEqual(0, rc, err)
        self.assertEqual(("guided", "run"), pg.mode_context(self.run_()))
        save({"work": {"mode": "auto"}})
        self.assertEqual(("guided", "run"), pg.mode_context(self.run_()))

    # a saved mode devkit cannot read (a value that is neither auto nor guided, or a file that is not JSON) never
    # turns into auto: the decision waits for the developer, as in guided mode, until the configuration is repaired,
    # and nothing is decided for the developer
    def test_an_invalid_saved_mode_never_decides_automatically(self):
        for saved in ({"work": {"mode": "autopilot"}}, '{"work": {"mode": "auto"'):
            with self.subTest(saved=saved):
                save(None)
                pg.start("Parser", plan=["parser"], replace=bool(self.run_()), why="next case")
                save(saved)
                d = pg.crew_decision("Rewrite or patch the parser?", options="A: rewrite | B: patch", recommend="A")
                self.assertEqual(("pending", ""), (d["state"], d["by"]))
                self.assertEqual([], [d for d in self.run_().get("decisions") or [] if d.get("by") == "auto"])


class DecisionTests(ModeCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(save, None)

    # auto (nothing saved): a member's decision with a recommendation is taken at once with that option, and the
    # member's step keeps running
    def test_auto_takes_the_recommended_option_and_the_member_keeps_working(self):
        save(None)
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        rc, out, err = self.neo_asks()
        self.assertEqual(0, rc, err)
        run = self.run_()
        d = run["decisions"][0]
        self.assertEqual(("decided", "A", "auto"), (d["state"], d["answer"], d["by"]))
        self.assertEqual("running", run["plan"][0]["state"])
        self.assertNotIn("waiting", run["agents"]["a1"])
        self.assertEqual([], pg.pending_decisions(run))

    # auto records who took the decision and why: the audit trail names automatic mode as the decider, the option and
    # the member who asked, and the decision never claims the developer's answer
    def test_auto_records_who_decided_and_in_which_mode(self):
        save(None)
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        self.neo_asks()
        rec = self.audit_records("crew.decision")
        self.assertEqual(1, len(rec), rec)
        ref = rec[0].get("ref") or {}
        self.assertEqual(("D1", "decided", "auto", "auto", "A: skip them", "Neo"),
                         (rec[0].get("target"), ref.get("result"), ref.get("by"), ref.get("mode"), ref.get("answer"),
                          ref.get("asked_by")))
        self.assertNotIn(self.run_()["decisions"][0]["by"], ("morpheus", "developer", ""))

    # guided (saved): the decision waits for the developer, and the member that asked is blocked on it
    def test_guided_keeps_the_decision_pending_and_blocks_the_asking_member(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        rc, _, err = self.neo_asks()
        self.assertEqual(0, rc, err)
        run = self.run_()
        self.assertEqual(("pending", ""), (run["decisions"][0]["state"], run["decisions"][0]["answer"]))
        self.assertEqual(("blocked", "D1"), (run["plan"][0]["state"], run["plan"][0].get("waiting")))
        self.assertEqual("D1", run["agents"]["a1"].get("waiting"))
        self.assertEqual(["D1"], [d["id"] for d in pg.pending_decisions(run)])

    # `dev crew withdraw` closes a pending decision with its reason: the blocked member works again, and the reason
    # is in the run's history and the audit trail
    def test_dev_crew_withdraw_frees_the_member_and_records_why(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        self.neo_asks()
        rc, out, err = run_cli("crew", "withdraw", "D1", "--why", "the parser keeps both kinds of line")
        self.assertEqual(0, rc, err)
        run = self.run_()
        d = run["decisions"][0]
        self.assertEqual(("withdrawn", "the parser keeps both kinds of line", ""), (d["state"], d["why"], d["answer"]))
        self.assertEqual("running", run["plan"][0]["state"])
        self.assertNotIn("waiting", run["agents"]["a1"])
        self.assertIn("the parser keeps both kinds of line",
                      [h.get("why") for h in run["history"] if h.get("step") == "(decision D1)"])
        self.assertEqual(["the parser keeps both kinds of line"],
                         [r.get("why") for r in self.audit_records("crew.withdrawn")])

    # a decision whose asking member ended and that blocks no step is moot: it needs nobody, and the run can end
    def test_a_decision_whose_member_ended_and_blocks_no_step_is_moot(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        self.neo_asks()
        # the asking member ends with its step done (a step it only left waiting would keep waiting for the answer)
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a1", "agent_type": "devkit:neo",
                                  "last_assistant_message": NEO_REPORT.format(check="✓ 12 parser tests pass")}, False)
        run = self.run_()
        self.assertFalse(any(s.get("waiting") == "D1" and s.get("state") == "blocked" for s in run["plan"]))
        self.assertEqual([], pg.pending_decisions(run))
        self.assertEqual(["D1"], [d["id"] for d in pg.moot_decisions(run)])
        self.assertIn("no pending decisions", run_cli("crew", "decisions", "--pending")[1])


class PromptHookTests(ModeCase):
    # in auto mode the prompt hook never tells the tech lead to ask the developer: a decision taken automatically
    # needs nobody
    def test_auto_prompt_hook_never_asks_after_an_automatic_decision(self):
        save(None)
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        self.neo_asks()
        text = pg.on_prompt("claude", {"session_id": "s1", "prompt": "go on"}) or ""
        self.assertIsNone(ASKS_THE_DEVELOPER.search(text), text)

    # the developer switches a guided run to auto while a decision waits: from then on, the prompt hook never tells
    # the tech lead to ask the developer either; auto means nothing waits for the developer's answer
    def test_after_switching_to_auto_the_prompt_hook_never_asks_the_developer(self):
        save({"work": {"mode": "guided"}})
        pg.start("Parser", plan=["parser", "docs"])
        self.neo_works_on_the_parser()
        self.neo_asks()
        self.assertEqual(0, run_cli("crew", "mode", "auto")[0])
        text = pg.on_prompt("claude", {"session_id": "s1", "prompt": "go on"}) or ""
        self.assertIsNone(ASKS_THE_DEVELOPER.search(text), text)


class SessionStartTests(test_hooks_welcome.WelcomeHook):
    def setUp(self):
        super().setUp()
        use_saved_configuration(self, self.root)
        save(None)

    # in auto mode (nothing saved, no decision anywhere) the session-start context never tells the tech lead to
    # ask the developer
    def test_auto_session_start_context_never_asks_the_developer(self):
        self.assertEqual(("auto", "config"), pg.mode_context(None))
        out = self.start("claude")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIsNone(ASKS_THE_DEVELOPER.search(ctx), ASKS_THE_DEVELOPER.search(ctx) and ctx[:600])


if __name__ == "__main__":
    unittest.main()
