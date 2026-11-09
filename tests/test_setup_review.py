# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup` when the person changes course: answers changed on the review of a saved setup are applied, Ctrl+C or
Ctrl+D at any point ends with a summary, a resumed setup keeps the earlier log, and a Mac whose earlier install never
finished is offered to continue it. The wizard runs in-process on the fake Mac of test_setup_flow (scripted keys,
stubbed commands and sign-ins); every file goes to a temporary folder.
"""
import argparse
import contextlib
import io
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

from devkit import core, tui, ui, wizard  # noqa: E402
import test_setup_flow as flow  # noqa: E402  (the fake Mac, its keys and the saved-run writer)
from test_wizard import line_mode  # noqa: E402  (the line prompts with detection and commands stubbed)

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


class LockAgainTests(unittest.TestCase):
    """The settings lock writes the Claude Code mode into managed settings, so it is one of the steps done with the
    answers."""

    def test_answers_changed_on_the_review_lock_claude_code_settings_again(self):
        """A paused setup whose lock is done, resumed through Review with changed answers: the lock runs again with
        them (dev config lock), never "done earlier", so the managed settings cannot keep the old mode."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            answers = dict(wizard.tui_defaults(flow.Mac().survey()), lock=True)
            flow.saved_run(tmp, {"save": "done", "configure": "done", "signin-claude": "done", "lock": "done",
                                 "doctor": "pending"}, answers=answers)
            mac = flow.Mac(saved=dict(BOTH, lock=True))
            r = flow.run_wizard(mac, REVIEW + CLAUDE_ONLY + ENTER + ENTER, tmp=tmp)  # Apply, then Enter my password
        self.assertEqual(r.cfg["agents"], {"claude": True, "codex": False})
        self.assertEqual([flow.dev_args(a) for a in mac.attached if flow.dev_args(a)], [["config", "lock"]])
        self.assertEqual(r.state("lock"), "done")


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


def review_text(off: str, cols=80) -> tuple:
    """The review of a rerun where the saved setup had both agents and `off` is turned off -> (lines, the words
    joined by single spaces)."""
    d = flow.Mac(saved=BOTH).survey()
    w = wizard._Wizard(flow.FakeTerm(cols=cols), skip_packages=True)
    w.d, w.saved_a = d, wizard.tui_defaults(d, detected=False)
    w.a, w.baseline = wizard.Answers(w.saved_a), wizard.Answers(w.saved_a)
    w._set_agents("claude" if off == "codex" else "codex")
    lines = [tui.strip(x) for x in w.review()]
    return lines, " ".join(" ".join(x.split()) for x in lines)


class ScreenWidthTests(unittest.TestCase):
    """The screens at 80 and 60 columns: rows on one line where they fit, nothing cut."""

    def test_every_review_row_fits_on_one_line_at_80_columns(self):
        text = wizard.preview(width=80, color=False)["W-7"]
        self.assertIn("  6  Docker               3 CPUs · 4 GB memory · 80 GB disk · starts at the end\n", text)
        rows = text.split("  1  ")[1].split("Packages")[0]
        self.assertEqual([x for x in rows.splitlines() if x.startswith(" " * 26)], [])

    def test_a_welcome_row_that_fits_stays_on_one_line_at_80_columns(self):
        text = wizard.preview(width=80, color=False)["W-2"]
        self.assertIn("  ✓ Mac          macOS 26.2 · Apple silicon · 10 CPUs · 16 GB memory · Homebrew\n", text)

    def test_the_banner_shows_the_whole_copyright_at_80_and_60_columns(self):
        with mock.patch.object(ui, "width", lambda: 120):  # the large wordmark, whatever this terminal is
            for width in (80, 60):
                for name in ("W-2", "W-17"):
                    with self.subTest(width=width, screen=name):
                        lines = wizard.preview(width=width, color=False)[name].splitlines()
                        at = lines.index(f"  v{core.VERSION} · setup")
                        rights = lines[at + 1:lines.index("", at)]
                        self.assertEqual(" · ".join(x.strip() for x in rights), core.COPYRIGHT)
                        self.assertEqual(len(rights), 1 if width == 80 else 2)  # at 60, broken at the " · "
                        self.assertEqual([x for x in lines if len(x) > width - 1 or x.endswith("…")], [])

    def test_the_review_says_which_permissive_modes_turning_an_agent_off_removes(self):
        said = {"codex": "! devkit removes its rules, hooks and skills from Codex, and the full-access modes it set "
                         "(no approvals, no sandbox). Codex and your own settings stay. To remove the app: brew "
                         "uninstall --cask codex",
                "claude": "! devkit removes its rules, hooks and skills from Claude Code, and the no-prompt mode it "
                          "set (bypassPermissions). Claude Code and your own settings stay. To remove the app: "
                          f"{wizard.REMOVE_APP['claude']}"}
        for off, sentence in said.items():
            for cols in (100, 80, 60):
                with self.subTest(off=off, cols=cols):
                    lines, text = review_text(off, cols)
                    self.assertIn(sentence, text)
                    self.assertEqual([x for x in lines if len(x) > cols - 1], [])


def closed_after(first: str):
    """Typed answers for the line prompts: the defaults, both agents, and Ctrl+D at the first prompt that contains
    `first`; every prompt after that reads a closed input too, as a real terminal does."""
    closed = []

    def typed(prompt):
        if closed or first in prompt:
            closed.append(prompt)
            raise EOFError
        return "1" if "Which coding agents" in prompt else ""
    typed.closed = closed
    return typed


class CtrlDTests(unittest.TestCase):
    """Ctrl+D (a closed input) at a line prompt is an answer, never a crash."""

    def test_ctrl_d_at_a_sign_in_prompt_skips_that_sign_in(self):
        typed = closed_after("Sign in to Codex")
        rc, cfg, out, calls = line_mode(yes=False, typed=typed, device=os.devnull)
        self.assertRegex(out, r"– Codex +skipped · sign in later with codex login")
        self.assertIn("devkit is ready.", out)
        self.assertEqual(line_mode.run["steps"][-1]["id"], "doctor")
        self.assertEqual(rc, 0)
        self.assertIn("What now?", typed.closed[-1])  # the summary's menu read the closed input too, and finished

    def test_ctrl_d_in_the_questions_quits_with_nothing_changed(self):
        rc, cfg, out, calls = line_mode(yes=False, typed=closed_after("Development root"))
        self.assertIn("Nothing changed. Run dev setup when you are ready.", out)
        self.assertEqual((rc, cfg, calls), (1, None, []))


class SetupLogTests(unittest.TestCase):
    def test_a_resumed_setup_adds_to_the_log_the_earlier_summary_named(self):
        earlier = "$ dev install\ndevkit hit a bug while pinning its copy\n"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            flow.saved_run(tmp, flow.PAUSED)
            log = tmp / "state" / "logs" / "setup.log"
            log.parent.mkdir(parents=True)
            log.write_text(earlier)
            r = flow.run_wizard(flow.Mac(saved=BOTH), ENTER, resume=True, tmp=tmp)
            text, mode = log.read_text(), log.stat().st_mode & 0o777
        self.assertEqual(r.rc, 0)
        self.assertTrue(text.startswith(earlier), text[:200])
        self.assertRegex(text[len(earlier):], r"^\n──── dev setup · \d{4}-\d\d-\d\d \d\d:\d\d:\d\d ────\n\n\$ ")
        self.assertEqual(mode, 0o600)


class UnfinishedInstallTests(unittest.TestCase):
    """A Mac whose earlier install failed after Save your choices: config.json is saved, but no dev install finished
    (devkit records one in config-installed.json and install.json). It is an unfinished setup, never "already set
    up"."""

    def test_already_set_up_needs_a_finished_dev_install_and_its_pinned_release(self):
        pin = {"version": "26.11.9", "path": Path("/x"), "name": "26.11.9-abc"}
        for recorded, pinned, expected in ((True, pin, True), (False, pin, False), (True, {}, False)):
            with self.subTest(recorded=recorded, pinned=bool(pinned)), tempfile.TemporaryDirectory() as tmp, \
                    mock.patch.object(wizard, "STATE", Path(tmp)), \
                    mock.patch.object(core, "pinned_release", lambda p=pinned: dict(p)):
                if recorded:
                    (Path(tmp) / "config-installed.json").write_text('{"version": "26.11.9"}')
                self.assertIs(wizard.installed_before(), expected)

    def test_a_settings_repair_after_a_crashed_install_is_no_finished_install(self):
        """dev install crashed after it pinned a release and wrote Claude Code's hooks; the person opens Claude Code,
        whose session-start hook runs dev config repair, and the repair restores a setting. It may update the record
        of a finished install but never create one, and neither may a partial dev install --only claude: the next
        dev setup still offers to continue. A whole dev install that exits 0 creates the record, and a later repair
        moves it on."""
        from devkit import cli, policy, setup
        pin = {"version": "26.11.9", "path": Path("/x"), "name": "26.11.9-abc"}
        desired = [{("cleanupPeriodDays",): 30}]
        install = argparse.Namespace(why="", unverified=False, only=None, version=None, as_pinned=True)
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            tmp = Path(tmp)
            for p in (mock.patch.object(core, "STATE", tmp), mock.patch.object(wizard, "STATE", tmp),
                      mock.patch.object(core, "CLAUDE_HOME", tmp / ".claude"),
                      mock.patch.object(core, "CODEX_HOME", tmp / ".codex"),
                      mock.patch.object(core, "pinned_release", lambda: dict(pin)),
                      mock.patch.object(policy, "repair", lambda: [policy.Drift("claude", "hooks.Stop", policy.MISSING,
                                                                                "dev hook stop")]),
                      mock.patch.object(policy, "claude_desired", lambda: (desired[0], {})),
                      mock.patch.object(setup, "run_install", lambda **kw: 0),
                      mock.patch("sys.stdout", io.StringIO())):
                stack.enter_context(p)
            self.assertEqual(cli.cmd_config(argparse.Namespace(config_cmd="repair", quiet=True)), 0)
            self.assertFalse(wizard.installed_before())
            self.assertEqual(cli.cmd_install(argparse.Namespace(**dict(vars(install), only="claude"))), 0)
            self.assertFalse(wizard.installed_before())
            self.assertEqual(cli.cmd_install(install), 0)
            self.assertTrue(wizard.installed_before())
            desired[0] = {("cleanupPeriodDays",): 45}
            self.assertEqual(cli.cmd_config(argparse.Namespace(config_cmd="repair", quiet=True)), 0)
            record = json.loads((tmp / "config-installed.json").read_text())
        self.assertEqual(record["claude"]["keys"]["cleanupPeriodDays"], 45)

    def test_a_saved_config_without_a_finished_install_is_offered_to_continue(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                mac = flow.Mac(saved=BOTH)
                r = flow.run_wizard(mac, ENTER, resume=resume)  # Continue
                self.assertEqual(mac.titles()[0], "Pick up where you left off?")
                self.assertEqual(mac.prompt("Pick up where you left off?")[1],
                                 ["Continue", "Review my answers first", "Start over"])
                self.assertRegex(r.text, r"Your choices were saved on \w+, \w+ \d+ at \d\d:\d\d, but the install did "
                                         r"not finish\.")
                self.assertRegex(r.text, r"✓ Save your choices +/\S+")  # where the choices are saved
                self.assertNotIn("already set up", r.text)
                self.assertEqual(execs(mac)[-2:], ["install", "doctor"])
                self.assertEqual((r.state("configure"), r.rc), ("done", 0))

    def test_the_preview_draws_the_unfinished_install_screen_uncut(self):
        for width in (80, 60):
            with self.subTest(width=width):
                lines = wizard.preview(width=width, color=False)["W-17 unfinished install"].splitlines()
                text = " ".join(" ".join(x.split()) for x in lines)
                self.assertIn("Your choices were saved on Monday, November 9 at 10:42, but the install did not "
                              "finish.", text)
                self.assertIn("Runs the 9 steps left with your saved answers.", text)
                self.assertEqual([x for x in lines if len(x) > width - 1 or x.endswith("…")], [])

    def test_start_over_asks_which_agents_as_on_a_first_setup(self):
        mac = flow.Mac(saved=BOTH)
        r = flow.run_wizard(mac, DOWN + DOWN + ENTER + ENTER + DOWN + ENTER + ENTER)  # Start over, Claude Code only
        self.assertIn("Let's set up devkit on this Mac.", r.text)
        self.assertNotIn("already set up", r.text)
        self.assertIn("Which coding agents do you want to use?", mac.titles())
        self.assertEqual(r.cfg["agents"], {"claude": True, "codex": False})
        self.assertEqual(r.rc, 0)


if __name__ == "__main__":
    unittest.main()
