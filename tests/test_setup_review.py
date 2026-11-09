# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup` when the person changes course: answers changed on the review of a saved setup are applied, Ctrl+C or
Ctrl+D at any point ends with a summary, a resumed setup keeps the earlier log, and a Mac whose earlier install never
finished is offered to continue it. The wizard runs in-process on the fake Mac of test_setup_flow (scripted keys,
stubbed commands and sign-ins); every file goes to a temporary folder.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from devkit import tui, wizard  # noqa: E402
import test_setup_flow as flow  # noqa: E402  (the fake Mac, its keys and the saved-run writer)

DOWN, ENTER = flow.DOWN, flow.ENTER
REVIEW = DOWN + ENTER                       # W-17: Review my answers first
CLAUDE_ONLY = b"2" + DOWN + ENTER + ENTER   # the review's Agents section: Claude Code only, its mode kept
BOTH = {"stacks": ["python"], "agents": {"claude": True, "codex": True}}


def setUpModule():
    tui.set_color(False)


def tearDownModule():
    tui.set_color()


def execs(mac) -> list:
    return [n for e, n in mac.events if e == "exec"]


class ReviewOfASavedSetupTests(unittest.TestCase):
    """W-17 Review: what the person changes there is what devkit saves and installs."""

    def apply_review(self, states, keys):
        """A saved run with these step states, resumed through Review with `keys` -> (result, mac, the agents the
        config file held when `dev install` ran)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            flow.saved_run(tmp, states)
            mac = flow.Mac(saved=BOTH)
            at_install = []
            mac.outcome["install"] = lambda on_line: at_install.append(
                json.loads((tmp / "config.json").read_text())["agents"]) or 0
            r = flow.run_wizard(mac, REVIEW + keys + ENTER, tmp=tmp)
        return r, mac, at_install

    def test_agents_changed_on_the_review_of_a_paused_setup_are_saved_before_dev_install(self):
        r, mac, at_install = self.apply_review(flow.PAUSED, CLAUDE_ONLY)
        self.assertEqual(r.cfg["agents"], {"claude": True, "codex": False})
        self.assertEqual(at_install, [{"claude": True, "codex": False}])
        self.assertEqual((r.state("save"), r.state("configure")), ("done", "done"))
        self.assertEqual(r.rc, 0)

    def test_changed_answers_run_set_up_devkit_again_though_it_was_done(self):
        done = {"save": "done", "configure": "done", "signin-claude": "pending", "doctor": "pending"}
        r, mac, at_install = self.apply_review(done, CLAUDE_ONLY)
        self.assertEqual(at_install, [{"claude": True, "codex": False}])
        self.assertEqual(r.cfg["agents"], {"claude": True, "codex": False})


class InterruptedMac(flow.Mac):
    """A Mac where Ctrl+C lands while the wizard reads the agents' status: `after_sign_in` right after a sign-in
    (the second Ctrl+C of a stopped sign-in), else at the first status read of the sign-ins."""

    def __init__(self, after_sign_in, **kw):
        super().__init__(**kw)
        self.after_sign_in, self.armed = after_sign_in, not after_sign_in

    def attach(self, argv, timeout=None, device=None, clock=None):
        self.armed = self.armed or self.after_sign_in
        return super().attach(argv, timeout, device, clock)

    def status(self, include_account=True):
        if self.armed:
            self.armed = False
            raise KeyboardInterrupt
        return super().status(include_account)


class SecondCtrlCTests(unittest.TestCase):
    """Ctrl+C in the sign-ins and the check ends with the summary, never with nothing on the screen."""

    def test_ctrl_c_right_after_a_stopped_sign_in_counts_as_the_stopped_sign_in(self):
        mac = InterruptedMac(after_sign_in=True, claude="signed-in", codex="installed")
        mac.sign_ins["codex"] = ["interrupted"]
        r = flow.run_wizard(mac, flow.BOTH_FROM_CLAUDE + ENTER, device=os.devnull)
        self.assertEqual(mac.titles().count("Codex is not signed in yet"), 1)
        self.assertIn("Reason: you stopped the sign-in.", r.text)
        self.assertEqual(r.step("signin-codex")["reason"], wizard.SKIPPED_BY_CHOICE)
        self.assertIn("devkit is ready.", r.text)
        self.assertEqual(r.rc, 0)

    def test_ctrl_c_in_the_sign_ins_pauses_the_setup_with_its_summary(self):
        mac = InterruptedMac(after_sign_in=False, claude="signed-in", codex="installed")
        r = flow.run_wizard(mac, flow.BOTH_FROM_CLAUDE, device=os.devnull)
        self.assertRegex(r.text, r"Setup paused\. \d of \d steps are done\.")
        self.assertIn("dev setup --resume", r.text.split("Setup paused")[1])
        self.assertEqual(r.state("doctor"), "pending")
        self.assertEqual(r.rc, 130)


if __name__ == "__main__":
    unittest.main()
