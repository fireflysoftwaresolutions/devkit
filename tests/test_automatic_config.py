# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Automatic settings and setup use real sandbox files and current policy."""
import contextlib
import io
import json
import tempfile
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

    def test_missing_mode_defaults_to_auto_but_saved_guided_survives(self):
        self.path.unlink()
        self.assertEqual(core.config_snapshot()[0]["work"]["mode"], "auto")
        self.path.write_text('{"work":{"mode":"guided"}}')
        self.assertEqual(core.config_snapshot()[0]["work"]["mode"], "guided")

    def test_main_can_write_nested_setting_without_human_evidence(self):
        before = self.state.sess.read_bytes()
        old, new = core.config_set("progress.max_parallel", "6")
        self.assertEqual((old, new), (4, 6))
        self.assertEqual(json.loads(self.path.read_text())["progress"]["max_parallel"], 6)
        self.assertNotIn("human", json.loads(self.state.sess.read_text()))
        self.assertEqual(self.state.sess.read_bytes(), before)

    def test_automatic_edit_validates_and_saves_candidate(self):
        def edit(path):
            path.write_text('{"work":{"mode":"auto"},"editor":"vim"}')
            return 0
        core.config_edit(edit)
        self.assertEqual(json.loads(self.path.read_text())["editor"], "vim")

    def test_auto_can_select_guided_but_cannot_self_escalate_back(self):
        core.config_set("work.mode", "guided")
        with self.assertRaises(core.ConfigError):
            core.config_set("work.mode", "auto")
        self.assertEqual(json.loads(self.path.read_text())["work"]["mode"], "guided")

    def test_human_can_deliberately_restore_auto(self):
        core.config_set("work.mode", "guided")
        core.config_set("work.mode", "auto", developer=True)
        self.assertEqual(json.loads(self.path.read_text())["work"]["mode"], "auto")

    def test_invalid_saved_config_does_not_authorize_repair(self):
        core.config_set("agents", '{"claude":true,"codex":true}')
        self.path.write_text('{"agents":{"max_parallel":8}}')
        before = self.path.read_bytes()
        with self.assertRaises(core.ConfigError):
            core.config_set("agents", '{"claude":true,"codex":true}')
        self.assertEqual(self.path.read_bytes(), before)

    def test_member_cannot_write_even_when_mode_is_auto(self):
        core.config_set("editor", "vim")
        before = self.path.read_bytes()
        with mock.patch.object(progress, "caller", return_value={"caller": "member"}):
            with self.assertRaises(core.ConfigError):
                core.config_set("editor", "nano")
        self.assertEqual(self.path.read_bytes(), before)

    def test_revocation_between_peek_and_commit_preserves_bytes(self):
        authorize = progress.authorize_command
        calls = []
        def revoke(action, target, **kwargs):
            calls.append(kwargs.get("consume", True))
            if kwargs.get("consume", True):
                return None
            return authorize(action, target, **kwargs)
        before = self.path.read_bytes()
        with mock.patch.object(progress, "authorize_command", side_effect=revoke):
            with self.assertRaises(core.ConfigError):
                core.config_set("editor", "vim")
        self.assertEqual(calls, [False, True])
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_candidate_is_not_consumed(self):
        authorize = progress.authorize_command
        with mock.patch.object(progress, "authorize_command", wraps=authorize) as check:
            with self.assertRaises(core.ConfigError):
                core.config_set("docker", '{"cpu":0}')
        self.assertEqual([c.kwargs["consume"] for c in check.call_args_list], [False])

    def test_parent_replacement_cannot_disable_fixed_checks(self):
        core.config_set("crew", '{"gates":{"enforce":true}}')
        before = self.path.read_bytes()
        for key, value in (("crew", '{"gates":{"enforce":false}}'),
                           ("skills", '{"enforce":false}'),
                           ("audit", '{"level":"security"}')):
            with self.subTest(key=key), self.assertRaises(core.ConfigError):
                core.config_set(key, value)
            self.assertEqual(self.path.read_bytes(), before)

    def test_automatic_configuration_never_turns_an_agent_off_running_or_not(self):
        # turning an agent off takes devkit's hooks out of its next session: that is the developer's decision,
        # whether or not a session of that agent is registered (codex is not this session's agent)
        core.config_set("agents.codex", "true")
        other = dict(json.loads(self.state.sess.read_text()), agent="codex", session="s2")
        before = self.path.read_bytes()
        for registered in (False, True):
            if registered:
                progress.session_path("codex", "s2").write_text(json.dumps(other))
            for key, value in (("agents.codex", "false"), ("agents", '{"claude":false,"codex":false}')):
                with self.subTest(key=key, registered=registered), \
                        self.assertRaisesRegex(core.ConfigError, r"fixed integrity settings: .*agents\.codex"):
                    core.config_set(key, value)
                self.assertEqual(self.path.read_bytes(), before)

    def test_automatic_configuration_never_changes_whether_bug_reports_leave_this_mac(self):
        # an editor replaces the whole file, so the integrity check, not the guard's dev config set rule, decides
        self.path.write_text('{"work":{"mode":"auto"},"bugs":{"sync":false,"share":"off"}}\n')
        before = self.path.read_bytes()
        for doc in ('{"work":{"mode":"auto"},"bugs":{"sync":true,"share":"off"}}',
                    '{"work":{"mode":"auto"},"bugs":{"sync":false,"share":"reports"}}',
                    '{"work":{"mode":"auto"}}'):
            def edit(path, doc=doc):
                path.write_text(doc)
                return 0
            with self.subTest(doc=doc), \
                    self.assertRaisesRegex(core.ConfigError, r"fixed integrity settings: .*bugs\.(?:sync|share)"):
                core.config_edit(edit)
            self.assertEqual(self.path.read_bytes(), before)

    def test_editor_cannot_remove_audit_hold(self):
        core.config_set("audit.hold", "true")
        before = self.path.read_bytes()
        def edit(path):
            path.write_text('{"work":{"mode":"auto"}}')
            return 0
        with self.assertRaises(core.ConfigError):
            core.config_edit(edit)
        self.assertEqual(self.path.read_bytes(), before)

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

    def test_unattended_setup_uses_saved_defaults_even_with_terminal(self):
        from devkit import providers, toolchain, tui
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

    def test_install_auto_skips_restart_question_and_rechecks_before_changes(self):
        from devkit import hooks
        used = []
        with mock.patch.object(setup, "ensure_config", side_effect=lambda: used.append("config")), \
                mock.patch.object(setup, "_choose_copy", return_value=setup.DEVKIT_HOME), \
                mock.patch.object(core, "is_current_pin", return_value=False), \
                mock.patch.object(hooks, "confirm_restart", side_effect=lambda assume_yes: assume_yes), \
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

    def test_install_cannot_use_auto_policy_to_skip_source_verification(self):
        used = []
        with mock.patch.object(setup, "ensure_config", side_effect=lambda: used.append("config")), \
                mock.patch.object(setup, "_choose_copy", return_value=setup.DEVKIT_HOME), \
                mock.patch.object(core, "is_current_pin", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.run_install(only={"config"}, as_pinned=True), 0)
            self.assertNotEqual(setup.run_install(only={"config"}, as_pinned=True,
                                                 unverified_why="skip source checks"), 0)
        self.assertEqual(used, ["config"])

    def test_wizard_save_preserves_explicit_permissions_and_rechecks_mode(self):
        saved = {"work": {"mode": "auto"}, "codex": {"mode": "full", "approval_policy": "on-request"},
                 "github_user": "ada", "handoff_repo": "ada/handoff", "agents": {"claude": True, "codex": True}}
        self.path.write_text(json.dumps(saved))
        w = wizard._Wizard(FakeTerm(), automatic=True, unattended=True)
        w.d = fake_survey(saved=saved)
        w.a = wizard.tui_defaults(w.d)
        with mock.patch.object(wizard, "CONFIG_FILE", self.path):
            w._save(mock.Mock(), 0)
            self.assertEqual(json.loads(self.path.read_text())["codex"]["approval_policy"], "on-request")
            core.config_set("work.mode", "guided")
            before = self.path.read_bytes()
            with self.assertRaises(core.ConfigError):
                w._save(mock.Mock(), 0)
        self.assertEqual(self.path.read_bytes(), before)

