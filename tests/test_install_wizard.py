# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev setup` as a first-time user meets it after the installer: the line prompts say that they take every default
when no terminal can answer, a missing GitHub sign-in names the exact command that fixes it, and the first welcome
screen says what devkit does and what the handoff repository is. Detection and commands are stubbed; the config goes
to a temporary folder.
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

from devkit import core, providers, toolchain, tui, ui, wizard  # noqa: E402
from test_wizard import FakeTerm, fake_survey  # noqa: E402

GH_LOGIN = "gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist"


class _Stdin(io.StringIO):
    def isatty(self):
        return False


def fake_exec(argv, on_line=None, log=None, env=None, timeout=None):
    """Every command the setup runs: done at once; `dev doctor` reports its checks passed."""
    if str(argv[-1]) == "doctor" and on_line:
        on_line("  ✓ 1/1 checks passed")
    return wizard._Exit(0)


def line_prompts(assume_yes=False, login="ada"):
    """`dev setup` through the line prompts with stdin not a terminal -> (exit code, output without colors)."""
    user = {"login": login, "name": "Ada", "id": 1} if login else {}
    with tempfile.TemporaryDirectory() as d:
        with mock.patch.object(wizard, "CONFIG_FILE", Path(d) / "config.json"), \
                mock.patch.object(wizard, "STATE", Path(d)), mock.patch.object(wizard, "terminal_device", lambda: None), \
                mock.patch.object(wizard, "_hw", lambda: (12, 36, "26.1", "arm64")), \
                mock.patch.object(core, "gh_user", lambda: user), \
                mock.patch.object(core, "have", lambda t: t in ("gh", "brew")), \
                mock.patch.object(providers, "status", lambda include_account=True: {
                    "claude": {"installed": True, "logged_in": True}, "codex": {"installed": True, "logged_in": True}}), \
                mock.patch.object(toolchain, "detect", lambda: {"hosts": ["github"], "clouds": [], "evidence": {}}), \
                mock.patch.object(wizard, "_exec", fake_exec), \
                mock.patch("subprocess.call", side_effect=AssertionError("every command goes through _exec")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("no command may really run here")), \
                mock.patch("sys.stdin", _Stdin()), mock.patch("sys.stdout", io.StringIO()) as out:
            rc = wizard.run(assume_yes=assume_yes, skip_packages=True)
    return rc, ui.ANSI_RE.sub("", out.getvalue())


class FirstRunTests(unittest.TestCase):
    def test_line_prompts_say_they_take_the_defaults_when_no_terminal_can_answer(self):
        rc, text = line_prompts()
        self.assertEqual(rc, 0)
        self.assertIn("No terminal to answer from, so every question takes its default (as with --yes)", text)

    def test_without_github_sign_in_it_names_the_exact_command(self):
        rc, text = line_prompts(assume_yes=True, login="")
        self.assertEqual(rc, 2)
        self.assertIn(f"devkit needs your GitHub account, and you are not signed in. Run {GH_LOGIN}, then dev setup",
                      text)

    def test_the_first_welcome_says_what_devkit_and_the_handoff_repository_are(self):
        w = wizard._Wizard(FakeTerm())
        w.d = fake_survey()
        text = " ".join(tui.strip(ln).strip() for ln in w.welcome())
        self.assertIn("devkit sets up Claude Code and Codex with shared rules and a safety guard", text)
        self.assertIn("keeps your project notes, tasks and plans in a private GitHub repository.", text)
        self.assertIn("Nothing changes until you choose Apply.", text)
        w.d = fake_survey(saved={"dev_root": "~/Development"})
        again = " ".join(tui.strip(ln).strip() for ln in w.welcome())
        self.assertNotIn("private GitHub repository", again)
        self.assertIn("devkit is already set up here.", again)


class ResumeTests(unittest.TestCase):
    """`dev setup --resume` continues the last setup where it stopped, or says there is nothing to continue."""

    def test_dev_setup_resume_continues_the_saved_setup(self):
        from devkit import cli
        with mock.patch.object(wizard, "run", return_value=0) as run:
            self.assertEqual(cli.main(["setup", "--resume"]), 0)
        self.assertTrue(run.call_args.kwargs.get("resume"))

    def test_without_a_saved_setup_resume_says_to_run_dev_setup(self):
        """W-18: nothing to continue is not an error."""
        with tempfile.TemporaryDirectory() as d, mock.patch.object(wizard, "STATE", Path(d)), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            rc = wizard.run(resume=True)
        self.assertEqual(rc, 0)
        text = ui.ANSI_RE.sub("", out.getvalue())
        self.assertIn("Nothing to continue: no setup is saved on this Mac.", text)
        self.assertIn("To set up devkit, run dev setup.", text)


if __name__ == "__main__":
    unittest.main()
