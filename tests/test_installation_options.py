# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Saved installation choices and the tools required by selected capabilities."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / 'lib'))
from devkit import cli, core, setup, toolchain, wizard  # noqa: E402
from tests.test_wizard import FakeTerm, fake_survey, line_mode  # noqa: E402


class SavedSelectionTests(unittest.TestCase):
    def test_tui_preserves_saved_empty_selections(self):
        saved = {'stacks': [], 'hosts': [], 'clouds': []}
        result = wizard.tui_defaults(fake_survey(saved=saved, clouds=('kubernetes',)))
        self.assertEqual(result['stacks'], [])
        self.assertEqual(result['hosts'], ['github'])
        self.assertEqual(result['clouds'], [])

    def test_tui_preserves_saved_nonempty_selections(self):
        saved = {'stacks': ['python'], 'hosts': ['github', 'bitbucket'], 'clouds': ['aws']}
        result = wizard.tui_defaults(fake_survey(saved=saved, clouds=('kubernetes',)))
        self.assertEqual(result['stacks'], ['python'])
        self.assertEqual(result['hosts'], ['github', 'bitbucket'])
        self.assertEqual(result['clouds'], ['aws'])

    def test_tui_saved_empty_clouds_are_authoritative(self):
        result = wizard.tui_defaults(fake_survey(saved={'clouds': []}, clouds=('kubernetes',)))
        self.assertEqual(result['clouds'], [])

    def test_tui_absent_keys_keep_discovery(self):
        result = wizard.tui_defaults(fake_survey(saved={}, clouds=('kubernetes',)))
        self.assertEqual(result['stacks'], ['java', 'node'])
        self.assertEqual(result['hosts'], ['github', 'gitlab'])
        self.assertEqual(result['clouds'], ['kubernetes'])

    def test_tui_without_discovery_keeps_saved_empty_values(self):
        result = wizard.tui_defaults(fake_survey(saved={'stacks': [], 'hosts': [], 'clouds': []},
                                              clouds=('kubernetes',)), detected=False)
        self.assertEqual((result['stacks'], result['hosts'], result['clouds']), ([], ['github'], []))

    def test_line_mode_preserves_saved_empty_selections(self):
        rc, cfg, _, _ = line_mode(saved={'stacks': [], 'hosts': [], 'clouds': []}, clouds=('kubernetes',))
        self.assertEqual(rc, 0)
        self.assertEqual(cfg['stacks'], [])
        self.assertEqual(cfg['hosts'], ['github'])
        self.assertEqual(cfg['clouds'], [])

    def test_line_mode_preserves_saved_nonempty_selections(self):
        saved = {'stacks': ['python'], 'hosts': ['github', 'bitbucket'], 'clouds': ['aws']}
        rc, cfg, _, _ = line_mode(saved=saved, clouds=('kubernetes',))
        self.assertEqual(rc, 0)
        self.assertEqual(cfg['stacks'], ['python'])
        self.assertEqual(cfg['hosts'], ['github', 'bitbucket'])
        self.assertEqual(cfg['clouds'], ['aws'])

    def test_line_mode_saved_empty_clouds_are_authoritative(self):
        rc, cfg, _, _ = line_mode(saved={'clouds': []}, clouds=('kubernetes',))
        self.assertEqual(rc, 0)
        self.assertEqual(cfg['clouds'], [])

    def test_line_mode_absent_keys_keep_discovery(self):
        rc, cfg, _, _ = line_mode(saved={}, clouds=('kubernetes',))
        self.assertEqual(rc, 0)
        self.assertEqual(cfg['stacks'], list(wizard.FIRST_STACKS))
        self.assertEqual(cfg['hosts'], ['github', 'gitlab'])
        self.assertEqual(cfg['clouds'], ['kubernetes'])


class ConfigureOnlyTests(unittest.TestCase):
    def test_package_skip_survives_the_pinned_copy_restart(self):
        class RestartFinished(Exception):
            pass

        def restart(executable, argv):
            args = cli.build_parser().parse_args(argv[argv.index("install"):])
            self.assertEqual(cli.cmd_install(args), 0)
            raise RestartFinished()

        with mock.patch.object(setup, "_choose_copy", side_effect=[setup.DEVKIT_HOME / "new-pin", setup.DEVKIT_HOME]), \
                mock.patch.object(setup.os, "execv", side_effect=restart), \
                mock.patch.object(core, "is_current_pin", return_value=False), \
                mock.patch.object(setup, "setup_tools") as install, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RestartFinished):
                setup.run_install(only={"tools"}, as_pinned=True, skip_packages=True)
        install.assert_not_called()

    def test_skip_packages_reaches_the_installer_without_changing_saved_choices(self):
        for skip in (False, True):
            with self.subTest(skip=skip):
                installed = []
                w = wizard._Wizard(FakeTerm(), skip_packages=skip)
                w.a = {"commit_template": True}

                def child(argv, *args, **kwargs):
                    # Cross the actual wizard -> CLI -> install boundary, with only the tools
                    # step enabled in this hermetic test. The real machine is never configured.
                    command = argv[argv.index("install"):]
                    parsed = cli.build_parser().parse_args(command)
                    parsed.only, parsed.as_pinned = "tools", True
                    return cli.cmd_install(parsed)

                with mock.patch.object(wizard, "_exec", side_effect=child), \
                        mock.patch.object(setup, "_choose_copy", return_value=setup.DEVKIT_HOME), \
                        mock.patch.object(core, "is_current_pin", return_value=False), \
                        mock.patch.object(setup, "setup_tools", side_effect=lambda: installed.append("tools")), \
                        contextlib.redirect_stdout(io.StringIO()):
                    result = w._configure(mock.Mock(), 0)
                self.assertIsNone(result)
                self.assertEqual(installed, [] if skip else ["tools"])
                self.assertEqual(w.a, {"commit_template": True})


class ToolchainApplicabilityTests(unittest.TestCase):
    def test_tools_repair_missing_npm_clis_even_when_homebrew_is_complete(self):
        for stacks, frameworks, binary, package in (([], ["angular"], "ng", "@angular/cli"),
                                                    ([], ["react"], "typescript-language-server", "typescript-language-server"),
                                                    (["python"], [], "pyright", "pyright")):
            for auto in (True, False):
                with self.subTest(binary=binary, auto=auto), contextlib.ExitStack() as stack:
                    cfg = core.deep_merge(core.DEFAULTS, {"stacks": stacks, "frameworks": frameworks,
                        "agents": {"claude": False, "codex": False}, "hosts": [], "clouds": [],
                        "packages": {"auto": auto}})
                    stack.enter_context(mock.patch.dict(core.CFG, cfg, clear=True))
                    stack.enter_context(mock.patch.object(core, "have", side_effect=lambda name: name != binary))
                    stack.enter_context(mock.patch.object(core, "sh", return_value=(0, "", "")))
                    install = stack.enter_context(mock.patch.object(wizard.subprocess, "call", return_value=0))
                    stack.enter_context(mock.patch.object(wizard, "install_playwright_skill"))
                    with contextlib.redirect_stdout(io.StringIO()):
                        setup.setup_tools()
                    npm = [call.args[0] for call in install.call_args_list if call.args[0][0] == "npm"]
                    self.assertEqual(bool(npm), auto)
                    if auto:
                        self.assertIn(package, npm[0])

    def test_package_auto_off_does_not_install_missing_host_extension(self):
        for auto in (True, False):
            with self.subTest(auto=auto):
                calls = []

                def command(argv, **kwargs):
                    calls.append(argv)
                    return (1, "", "missing") if argv[:3] == ["az", "extension", "show"] else (0, "", "")

                with mock.patch.dict(core.CFG, {"hosts": ["azure-devops"], "clouds": [], "packages": {"auto": auto}}), \
                        mock.patch.object(core, "have", return_value=True), \
                        mock.patch.object(wizard, "missing_packages", return_value=[]), \
                        mock.patch.object(core, "sh", side_effect=command), contextlib.redirect_stdout(io.StringIO()):
                    setup.setup_tools()
                installs = [a for a in calls if a[:3] == ["az", "extension", "add"]]
                self.assertEqual(bool(installs), auto)

    def test_omitted_kubernetes_excludes_both_packages_and_checks(self):
        cfg = {'hosts': ['github'], 'clouds': []}
        self.assertNotIn('kubernetes', toolchain.selected(cfg))
        packages = wizard.filtered(SOURCE / 'Brewfile', set(wizard.BASE_GROUPS) | set(toolchain.groups(cfg)))
        self.assertNotIn('brew "helm"', packages)
        self.assertNotIn('brew "kubernetes-cli"', packages)

    def test_selected_kubernetes_requires_helm(self):
        with mock.patch.object(core, 'have', side_effect=lambda cli: cli == 'kubectl'), \
                mock.patch.object(core, 'sh', return_value=(0, 'sandbox-context', '')):
            result = toolchain.check('kubernetes')
        self.assertFalse(result['installed'], result)
        self.assertIn('helm', result['detail'])

    def test_kubernetes_with_both_clis_and_context_is_ready(self):
        with mock.patch.object(core, 'have', side_effect=lambda cli: cli in {'kubectl', 'helm'}), \
                mock.patch.object(core, 'sh', return_value=(0, 'sandbox-context', '')):
            result = toolchain.check('kubernetes')
        self.assertTrue(result['installed'] and result['signed_in'], result)

    def test_azure_devops_requires_extension_even_when_token_exists(self):
        with mock.patch.dict(os.environ, {'AZURE_DEVOPS_EXT_PAT': 'test-token'}, clear=True), \
                mock.patch.object(core, 'have', side_effect=lambda cli: cli == 'az'), \
                mock.patch.object(core, 'sh', return_value=(1, '', 'extension not installed')):
            result = toolchain.check('azure-devops')
        self.assertFalse(result['installed'] and result['signed_in'], result)

    def test_azure_devops_with_extension_and_token_is_ready(self):
        with mock.patch.dict(os.environ, {'AZURE_DEVOPS_EXT_PAT': 'test-token'}, clear=True), \
                mock.patch.object(core, 'have', side_effect=lambda cli: cli == 'az'), \
                mock.patch.object(core, 'sh', return_value=(0, '{}', '')):
            result = toolchain.check('azure-devops')
        self.assertTrue(result['installed'] and result['signed_in'], result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
