# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Framework choices drive tools and agent capabilities without altering legacy installs."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import cli, core, registry, setup, wizard  # noqa: E402
from tests.test_wizard import FakeTerm, fake_survey  # noqa: E402


class FrameworkProfileTests(unittest.TestCase):
    def setUp(self):
        from devkit import profiles
        self.profiles = profiles

    def test_legacy_node_keeps_angular_but_explicit_empty_does_not(self):
        legacy = self.profiles.resolve({"stacks": ["node"]})
        explicit = self.profiles.resolve({"stacks": ["node"], "frameworks": []})
        self.assertTrue(legacy["legacy_frameworks"])
        self.assertIn("angular", legacy["package_groups"])
        self.assertFalse(explicit["legacy_frameworks"])
        self.assertNotIn("angular", explicit["package_groups"])
        self.assertTrue(self.profiles.extension_selected({"stacks": []}, "mcp", "angular-cli", "claude"))
        self.assertFalse(self.profiles.extension_selected({"frameworks": []}, "mcp", "angular-cli", "claude"))

    def test_frameworks_imply_runtimes_without_mutating_saved_choices(self):
        cfg = {"stacks": [], "frameworks": ["angular", "spring", "laravel", "nextjs"]}
        result = self.profiles.resolve(cfg)
        self.assertEqual(result["stacks"], ["node", "java", "php"])
        self.assertEqual(result["implied_stacks"], ["node", "java", "php"])
        self.assertEqual(cfg["stacks"], [])
        self.assertIn("angular", result["package_groups"])

    def test_react_and_next_share_skills_without_angular_tools(self):
        for framework in ("react", "nextjs"):
            cfg = {"stacks": [], "frameworks": [framework]}
            self.assertEqual(self.profiles.resolve(cfg)["package_groups"], ["node"])
            self.assertTrue(self.profiles.extension_selected(cfg, "skill", "react-best-practices", "codex"))
            self.assertFalse(self.profiles.extension_selected(cfg, "skill", "angular-developer", "codex"))

    def test_mixed_sources_are_filtered_per_skill(self):
        cfg = {"stacks": ["python"], "frameworks": []}
        self.assertFalse(self.profiles.extension_selected(cfg, "skill", "spring-security", "claude"))
        self.assertFalse(self.profiles.extension_selected(cfg, "skill", "java-junit", "claude"))
        self.assertTrue(self.profiles.extension_selected(cfg, "skill", "documentation-writer", "claude"))
        self.assertTrue(self.profiles.extension_selected(cfg, "skill", "threat-model-analyst", "claude"))

    def test_explicit_extension_selection_does_not_override_disabled_agent(self):
        cfg = {"stacks": [], "frameworks": [], "agents": {"claude": False, "codex": True}}
        for opts in ({"explicit": True}, {"required": True}):
            self.assertTrue(self.profiles.extension_selected(cfg, "mcp", "angular-cli", "codex", **opts))
            self.assertFalse(self.profiles.extension_selected(cfg, "mcp", "angular-cli", "claude", **opts))

    def test_invalid_framework_selection_is_not_legacy_or_empty(self):
        for value in (None, "react", {}, ["unknown"], [None]):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "frameworks"):
                self.profiles.resolve({"frameworks": value})

    def test_detection_only_suggests_supported_frameworks(self):
        self.assertEqual(self.profiles.suggested_frameworks(["java", "angular", "nextjs", "angular", "python"]),
                         ["angular", "nextjs"])


class FrameworkConsumerTests(unittest.TestCase):
    def test_saved_frameworks_and_agent_instructions_survive_partial_install_and_update(self):
        from devkit import hooks

        class RestartFinished(Exception):
            pass

        choices = (None, [], ["angular"], ["react"], ["nextjs"], ["nestjs"], ["spring"], ["laravel"], ["symfony"])
        for frameworks in choices:
            for selected in ("claude", "codex", "both"):
                with self.subTest(frameworks=frameworks, agents=selected), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    config = root / "config.json"
                    saved = {"stacks": ["node"] if frameworks is None else [],
                             "agents": {a: selected in (a, "both") for a in ("claude", "codex")},
                             "hosts": [], "clouds": [], "skills": {"groups": []}, "packages": {"auto": False}}
                    if frameworks is not None:
                        saved["frameworks"] = frameworks
                    original = json.dumps(saved).encode()
                    config.write_bytes(original)
                    paths = {"CONFIG_FILE": config, "DEV": root / "dev", "WT_ROOT": root / "worktrees",
                             "TMP_ROOT": root / "tmp", "LOCAL": root / "local", "STATE": root / "state",
                             "CLAUDE_HOME": root / "claude", "CODEX_HOME": root / "codex"}

                    def apply_saved():
                        with mock.patch.dict(core.CFG, core.load_config(), clear=True):
                            return setup.run_install(only={"config", "instructions"}, as_pinned=True)

                    def restart(executable, argv):
                        self.assertEqual(argv[-2:], ["install", "--yes"])
                        self.assertEqual(apply_saved(), 0)
                        raise RestartFinished()

                    with mock.patch.multiple(setup, **paths), \
                            mock.patch.object(core, "CONFIG_FILE", config), \
                            mock.patch.object(core, "source_home", return_value=root / "checkout"), \
                            mock.patch.object(core, "older_copy", return_value=None), \
                            mock.patch.object(core, "is_current_pin", return_value=False), \
                            mock.patch.object(setup, "_choose_copy", return_value=setup.DEVKIT_HOME), \
                            mock.patch.object(core, "sh", side_effect=AssertionError("no external commands")), \
                            contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(apply_saved(), 0)
                        files = {"claude": paths["CLAUDE_HOME"] / "CLAUDE.md", "codex": paths["CODEX_HOME"] / "AGENTS.md"}
                        expected = {}
                        for agent, path in files.items():
                            self.assertEqual(path.exists(), saved["agents"][agent])
                            if path.exists():
                                self.assertIn(setup.GENERATED_MARK, path.read_text()[:200])
                                expected[agent] = path.read_bytes()
                        self.assertEqual(config.read_bytes(), original)
                        self.assertEqual(apply_saved(), 0)
                        with mock.patch.dict(core.CFG, core.load_config(), clear=True), \
                                mock.patch.object(hooks, "confirm_restart", return_value=True), \
                                mock.patch.object(cli, "update_checkout", return_value=(0, "local update", True)), \
                                mock.patch.object(cli.os, "execv", side_effect=restart):
                            with self.assertRaises(RestartFinished):
                                cli.cmd_update(cli.build_parser().parse_args(["update", "--yes"]))
                        self.assertEqual(config.read_bytes(), original)
                        for agent, path in files.items():
                            self.assertEqual(path.exists(), saved["agents"][agent])
                            if agent in expected:
                                self.assertEqual(path.read_bytes(), expected[agent])
                        loaded = core.load_config()
                        for key in ("agents", "stacks", "hosts", "clouds", "skills", "packages"):
                            self.assertEqual(loaded[key], core.deep_merge(core.DEFAULTS, saved)[key])
                        if frameworks is None:
                            self.assertNotIn("frameworks", loaded)
                        else:
                            self.assertEqual(loaded["frameworks"], frameworks)

    def test_spring_detection_reads_manifests_without_executing_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pom.xml").write_text("<project><groupId>org.springframework.boot</groupId></project>")
            with mock.patch.object(core, "sh", side_effect=AssertionError("detection must not execute commands")):
                self.assertIn("spring", registry.detect_stack(root))
            (root / "pom.xml").write_text("<project><groupId>example</groupId></project>")
            self.assertNotIn("spring", registry.detect_stack(root))

    def test_invalid_frameworks_stop_install_before_pinning_or_writing(self):
        for value in (None, "angular", {}, ["unknown"], [None]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.json"
                original = json.dumps({"frameworks": value}).encode()
                config.write_bytes(original)
                with mock.patch.object(core, "CONFIG_FILE", config), \
                        mock.patch.object(setup, "CONFIG_FILE", config), \
                        mock.patch.dict(core.CFG, core.load_config(), clear=True), \
                        mock.patch.object(setup, "_choose_copy", side_effect=setup.PinError("unexpected pin boundary")) as choose, \
                        mock.patch.object(core, "sh") as command, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(setup.run_install(as_pinned=True), 2)
                    self.assertEqual(setup.doctor(), 2)
                choose.assert_not_called()
                command.assert_not_called()
                self.assertEqual(config.read_bytes(), original)

    def test_invalid_saved_frameworks_stop_update_before_checkout_mutation(self):
        from devkit import hooks
        for value in (None, "angular", {}, ["unknown"], [None]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.json"
                original = json.dumps({"frameworks": value}).encode()
                config.write_bytes(original)
                with mock.patch.object(core, "CONFIG_FILE", config), \
                        mock.patch.object(setup, "CONFIG_FILE", config), \
                        mock.patch.dict(core.CFG, core.load_config(), clear=True), \
                        mock.patch.object(hooks, "confirm_restart", return_value=True), \
                        mock.patch.object(cli, "update_checkout", return_value=(1, "unexpected checkout", False)) as checkout, \
                        mock.patch.object(cli.os, "execv") as restart, \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    result = cli.cmd_update(cli.build_parser().parse_args(["update", "--yes"]))
                self.assertEqual(result, 2)
                self.assertEqual(config.read_bytes(), original)
                checkout.assert_not_called()
                restart.assert_not_called()

    def test_line_prompt_can_explicitly_deselect_a_saved_framework(self):
        p = wizard.Prompter(False)
        p.yes = False
        with mock.patch.object(p, "read", return_value="none"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(p.multi("Frameworks", {"angular": "Angular"}, ["angular"]), [])

    def test_wizard_preserves_saved_none_and_legacy_defaults(self):
        for saved, expected in (({"frameworks": []}, []), ({"stacks": ["node"]}, None), ({}, ["angular"])):
            d = fake_survey(saved=saved)
            d["stacks_found"]["frameworks"] = {"angular": 2}
            answers = wizard.tui_defaults(d)
            self.assertEqual(answers.get("frameworks", "missing"), expected)
            cfg = wizard.build_config(saved, answers, "ada")
            if expected is None:
                self.assertNotIn("frameworks", cfg)
            else:
                self.assertEqual(cfg["frameworks"], expected)

    def test_angular_prerequisite_is_shared_by_review_package_plan_and_doctor(self):
        cfg = {"stacks": [], "frameworks": ["angular"]}
        w = wizard._Wizard(FakeTerm())
        w.d = fake_survey(saved=cfg)
        w.a = wizard.tui_defaults(w.d)
        review = dict(w.review_rows(w.a))
        self.assertIn("Node", review["Stacks"])
        self.assertIn("Angular", review["Frameworks"])
        self.assertIn("@angular/cli", w._package_plan()["npm"])
        self.assertIn("typescript", w._package_plan()["npm"])
        with mock.patch.dict(core.CFG, cfg):
            _, _, ok, detail, _ = setup.tools_check([], have=lambda t: t != "ng")
        self.assertFalse(ok)
        self.assertIn("ng", detail)

    def test_explicit_react_needs_typescript_but_no_angular_cli(self):
        cfg = {"stacks": [], "frameworks": ["react"]}
        w = wizard._Wizard(FakeTerm())
        w.a = wizard.tui_defaults(fake_survey(saved=cfg))
        self.assertNotIn("@angular/cli", w._package_plan()["npm"])
        self.assertIn("typescript", w._package_plan()["npm"])
        with mock.patch.dict(core.CFG, cfg):
            self.assertTrue(setup.tools_check([], have=lambda t: t != "ng")[2])
            self.assertFalse(setup.tools_check([], have=lambda t: t != "typescript-language-server")[2])


if __name__ == "__main__":
    unittest.main()
