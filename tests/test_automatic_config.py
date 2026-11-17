# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""An agent's configuration changes and setup runs, against real sandbox files: devkit asks for no authorization and
keeps no setting out of reach; only the schema decides what a configuration may hold."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.test_progress import TempState
from devkit import core, progress, setup, wizard
from tests.test_wizard import FakeTerm, fake_survey


class AutomaticConfigTests(unittest.TestCase):
    def setUp(self):
        snapshot = getattr(core, "config_snapshot", lambda: (core.load_config(), []))
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name)
        self.path = self.root / "config.json"
        self.path.write_text('{"work":{"mode":"auto"}}\n')
        for patch in (mock.patch.object(core, "CONFIG_FILE", self.path),
                      mock.patch.object(core, "config_snapshot", snapshot, create=True),
                      mock.patch.object(core, "set_by_developer", return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)

    def saved(self):
        return json.loads(self.path.read_text())

    def test_missing_mode_defaults_to_auto_but_saved_guided_survives(self):
        self.path.unlink()
        self.assertEqual(core.config_snapshot()[0]["work"]["mode"], "auto")
        self.path.write_text('{"work":{"mode":"guided"}}')
        self.assertEqual(core.config_snapshot()[0]["work"]["mode"], "guided")

    def test_main_can_write_nested_setting_without_human_evidence(self):
        before = self.state.sess.read_bytes()
        old, new = core.config_set("progress.max_parallel", "6")
        self.assertEqual((old, new), (4, 6))
        self.assertEqual(self.saved()["progress"]["max_parallel"], 6)
        self.assertNotIn("human", json.loads(self.state.sess.read_text()))
        self.assertEqual(self.state.sess.read_bytes(), before)

    def test_automatic_edit_validates_and_saves_candidate(self):
        def edit(path):
            path.write_text('{"work":{"mode":"auto"},"editor":"vim"}')
            return 0
        core.config_edit(edit)
        self.assertEqual(self.saved()["editor"], "vim")

    def test_an_agent_sets_guided_and_auto_again(self):
        core.config_set("work.mode", "guided")
        core.config_set("work.mode", "auto")
        self.assertEqual(self.saved()["work"]["mode"], "auto")

    def test_a_writer_repairs_an_invalid_saved_configuration(self):
        self.path.write_text('{"agents":{"max_parallel":8}}')
        core.config_set("agents", '{"claude":true,"codex":true}')
        self.assertEqual(self.saved(), {"agents": {"claude": True, "codex": True}})

    def test_a_crew_member_writes_the_configuration_too(self):
        with mock.patch.object(progress, "caller", return_value={"caller": "member"}):
            core.config_set("editor", "nano")
        self.assertEqual(self.saved()["editor"], "nano")

    def test_the_settings_once_kept_from_agents_change_like_any_other(self):
        # turning an agent or a check off, sharing bug reports, a weaker audit: the developer's settings, which an
        # agent now changes too, through a setter or an editor
        self.path.write_text('{"work":{"mode":"auto"},"audit":{"hold":true},"bugs":{"sync":false,"share":"off"}}\n')
        for key, value in (("agents.codex", "false"), ("crew", '{"gates":{"enforce":false}}'),
                           ("skills", '{"enforce":false}'), ("audit.level", "security"), ("bugs.share", "reports")):
            with self.subTest(key=key):
                core.config_set(key, value)
        def edit(path):
            doc = json.loads(path.read_text())
            doc["audit"].pop("hold")
            doc["bugs"]["sync"] = True
            path.write_text(json.dumps(doc))
            return 0
        core.config_edit(edit)
        saved = self.saved()
        self.assertEqual((saved["agents"]["codex"], saved["crew"]["gates"]["enforce"], saved["skills"]["enforce"],
                          saved["audit"], saved["bugs"]),
                         (False, False, False, {"level": "security"}, {"sync": True, "share": "reports"}))

    def test_auto_setup_shell_needs_no_confirmation(self):
        from devkit import completion
        with mock.patch.object(completion, "helpers_file", return_value=self.root / "helpers"), \
                mock.patch("builtins.input", side_effect=AssertionError("no confirmation")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.setup_shell(), 0)
            self.assertEqual(completion.helpers_mode(), "on")
            self.assertEqual(setup.setup_shell(remove=True), 0)
            self.assertEqual(completion.helpers_mode(), "off")

    def test_auto_prompt_has_no_terminal_confirmation(self):
        from devkit import prompt
        with mock.patch.object(prompt, "STATE", self.root / "prompt"), \
                mock.patch.object(prompt, "safe_refresh"), \
                mock.patch("builtins.input", side_effect=AssertionError("no confirmation")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.setup_prompt(zshrc=self.root / "zshrc", starship=self.root / "starship",
                                               env={}), 0)
            self.assertEqual(prompt.mode(), "zsh")

    def test_a_guided_agent_runs_the_setup_commands_too(self):
        from devkit import completion
        core.config_set("work.mode", "guided")
        with mock.patch.object(completion, "helpers_file", return_value=self.root / "helpers"), \
                mock.patch("builtins.input", side_effect=AssertionError("no confirmation")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(setup.setup_shell(), 0)
            self.assertEqual(completion.helpers_mode(), "on")
            self.assertEqual(setup.setup_commit_template(repos=[]), 0)
        self.assertNotIn("Setup unchanged", out.getvalue())

    def test_unattended_setup_uses_saved_defaults_even_with_terminal(self):
        from devkit import toolchain, tui
        saved = {"work": {"mode": "auto"}, "stacks": [], "frameworks": [], "agents": {"claude": False,
                 "codex": True}, "docker": {"cpu": 3}, "sync": {"interval_minutes": 19}}
        self.path.write_text(json.dumps(saved))
        observed = []
        def apply(w):
            observed.append((dict(w.a), w.can_ask()))
            return 0
        data = fake_survey(saved=saved)
        with mock.patch.object(wizard, "CONFIG_FILE", self.path), \
                mock.patch.object(wizard, "STATE", self.root / "wizard"), \
                mock.patch.object(wizard, "detect", return_value=data), \
                mock.patch.object(core, "gh_user", return_value=data["gh"]), \
                mock.patch.object(wizard, "detect_docker", return_value={}), \
                mock.patch.object(toolchain, "detect", return_value=data["seen"]), \
                mock.patch.object(tui, "available", return_value=True), \
                mock.patch.object(wizard, "terminal_device", return_value="/dev/fake"), \
                mock.patch.object(wizard._Wizard, "apply", apply), \
                mock.patch("builtins.input", side_effect=AssertionError("no confirmation")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wizard.run(skip_packages=True), 0)
        answer, can_ask = observed[0]
        self.assertEqual((answer["stacks"], answer["frameworks"], answer["cpu"], answer["interval"]),
                         ([], [], 3, 19))
        self.assertFalse(answer["use_claude"])
        self.assertFalse(can_ask)

    def test_a_guided_agents_install_goes_on_with_no_question_while_sessions_run(self):
        from devkit import hooks
        self.path.write_text('{"work":{"mode":"guided"}}\n')
        used = []
        with mock.patch.object(setup, "ensure_config", side_effect=lambda: used.append("config")), \
                mock.patch.object(setup, "_choose_copy", return_value=setup.DEVKIT_HOME), \
                mock.patch.object(core, "is_current_pin", return_value=False), \
                mock.patch.object(hooks, "sessions_to_restart", return_value=[{"agent": "claude", "session": "s9"}]), \
                mock.patch.object(hooks, "restart_lines", return_value=["  claude s9"]), \
                mock.patch("builtins.input", side_effect=AssertionError("no confirmation")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.run_install(only={"config"}), 0)
        self.assertEqual(used, ["config"])

    def test_auto_install_reports_host_error_without_claiming_success(self):
        with mock.patch.object(setup, "ensure_config", side_effect=PermissionError("host denied")), \
                mock.patch.object(setup, "_choose_copy", return_value=setup.DEVKIT_HOME), \
                mock.patch.object(core, "is_current_pin", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            result = setup.run_install(only={"config"}, as_pinned=True)
        self.assertNotEqual(result, 0)
        self.assertIn("host denied", output.getvalue())

    def test_wizard_save_preserves_explicit_permissions_and_keeps_the_saved_mode(self):
        saved = {"work": {"mode": "auto"}, "codex": {"mode": "full", "approval_policy": "on-request"},
                 "github_user": "ada", "handoff_repo": "ada/handoff", "agents": {"claude": True, "codex": True}}
        self.path.write_text(json.dumps(saved))
        w = wizard._Wizard(FakeTerm(), unattended=True)
        w.d = fake_survey(saved=saved)
        w.a = wizard.tui_defaults(w.d)
        with mock.patch.object(wizard, "CONFIG_FILE", self.path):
            w._save(mock.Mock(), 0)
            self.assertEqual(self.saved()["codex"]["approval_policy"], "on-request")
            core.config_set("work.mode", "guided")
            w._save(mock.Mock(), 0)  # an unattended save never replaces a deliberate saved setting
        self.assertEqual(self.saved()["work"]["mode"], "guided")


if __name__ == "__main__":
    unittest.main()
