# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Doctor requirements follow the selected installation, with all host probes isolated."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import (audit, canary, cli, codexrpc, core, crew, desktop, ext, jobs,
                    policy, providers, registry, selfcheck, setup, skillguard, toolchain)


class DoctorProfilesTests(unittest.TestCase):
    def doctor_output(self, agents, installed=(), claude_mode=None, validate_plugins=False, desktop_apps=False):
        """Exercise doctor's selection, replacing unrelated host probes with local fixtures."""
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            stack.enter_context(mock.patch.dict(core.CFG, {"agents": agents, "stacks": []}))
            for name in ("HOME", "CLAUDE_HOME", "CODEX_HOME", "AGENTS_HOME", "STATE", "HANDOFF"):
                folder = root / name.lower()
                folder.mkdir()
                stack.enter_context(mock.patch.object(setup, name, folder))
            if claude_mode is not None:
                stack.enter_context(mock.patch.dict(core.CFG, {"claude": {"default_mode": claude_mode}}))
                (setup.CLAUDE_HOME / "settings.json").write_text(json.dumps({
                    "permissions": {"defaultMode": claude_mode}}))
            stack.enter_context(mock.patch.object(setup, "ZSHRC", root / "zshrc"))
            stack.enter_context(mock.patch.object(setup, "CONFIG_FILE", root / "config.json"))
            stack.enter_context(mock.patch.object(setup, "CLAUDE_APP", root if desktop_apps else root / "absent"))
            stack.enter_context(mock.patch.object(setup, "CHATGPT_APP", root / "absent"))
            stack.enter_context(mock.patch.object(desktop, "status", return_value={"installed": False}))
            stack.enter_context(mock.patch.object(desktop, "bypass_mode", return_value=(None, "")))
            stack.enter_context(mock.patch.object(setup, "_surface_sessions", return_value=[]))
            stack.enter_context(mock.patch.object(core, "have", lambda name: name in installed))
            stack.enter_context(mock.patch.object(core, "gh_user", return_value={}))
            stack.enter_context(mock.patch.object(core, "gitout", return_value=""))
            for obj in (core, setup):
                stack.enter_context(mock.patch.object(obj, "sh", return_value=(1, "", "not installed")))
            for obj, name, result in (
                (registry, "discover", ([], [], [])),
                (selfcheck, "work_state_checks", []), (selfcheck, "old_copy_checks", []),
                (crew, "sync", []), (crew, "load", {"members": []}), (crew, "codex_roles", {}),
                (setup, "crew_skill_checks", []), (setup, "crew_raci_checks", []),
                (setup, "impeccable_checks", []), (setup, "pin_checks", []),
                (setup, "codex_hook_checks", []), (setup, "codex_git_attribution", None),
                (setup, "app_checks", []), (setup, "build_codex_plugin", root),
                (setup, "plugin_copy_stale", True),
                (providers, "status", {key: {"name": key, "installed": key in installed}
                                        for key in ("claude", "codex")}),
                (ext, "doctor_checks", []), (cli, "plugin_checks", []),
                (jobs, "jobs", {}), (audit, "publish_checks", []),
                (canary, "summary", {}), (canary, "summary_line", "none"),
                (toolchain, "selected", []), (policy, "project_overrides", []),
                (skillguard, "coverage", [("debug", ["root-cause"])]),
            ):
                if (obj is cli and name == "plugin_checks" and validate_plugins) or \
                        (obj is setup and name == "app_checks" and desktop_apps):
                    continue
                stack.enter_context(mock.patch.object(obj, name, return_value=result))
            rpc = mock.MagicMock()
            rpc.__enter__.return_value.read.return_value = {"config": {}}
            rpc.__enter__.return_value.models.return_value = []
            stack.enter_context(mock.patch.object(codexrpc, "CodexRPC", return_value=rpc))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = setup.doctor()
            return result, output.getvalue()

    def test_codex_only_does_not_require_claude_settings(self):
        _, output = self.doctor_output({"claude": False, "codex": True}, installed=("codex",))
        self.assertNotIn("dev install --only claude", output)

    def test_claude_only_does_not_check_a_present_but_disabled_codex(self):
        _, output = self.doctor_output({"claude": True, "codex": False}, installed=("claude", "codex"))
        self.assertNotIn("approval/sandbox", output)

    def test_disabled_agents_do_not_need_skill_rule_coverage(self):
        for disabled in ("claude", "codex"):
            with self.subTest(disabled=disabled):
                agents = {key: key != disabled for key in ("claude", "codex")}
                _, output = self.doctor_output(agents)
                self.assertNotIn(f"skills for every rule ({disabled})", output)

    def test_missing_selected_agents_remain_failed_rows(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                selected = {name: name == agent for name in ("claude", "codex")}
                result, output = self.doctor_output(selected)
                self.assertEqual(result, 1)
                row = next(line for line in output.splitlines() if f"{agent} installed + signed in" in line)
                self.assertIn(setup.ui.FAIL, row)
                self.assertIn(f"install {agent} and sign in", output)

    def test_disabled_agents_do_not_run_plugin_validators(self):
        for disabled, label in (("claude", "Claude Code validator"), ("codex", "Codex parser")):
            with self.subTest(disabled=disabled):
                _, output = self.doctor_output({key: key != disabled for key in ("claude", "codex")},
                                               installed=("claude", "codex"), validate_plugins=True)
                self.assertNotIn(label, output)

    def test_claude_desktop_is_checked_without_the_codex_binary(self):
        _, output = self.doctor_output({"claude": True, "codex": False}, installed=("claude",), desktop_apps=True)
        self.assertIn("devkit extension", output)
        self.assertIn("dev install --only desktop", output)

    def test_selected_claude_accepts_the_configured_permission_mode(self):
        for mode in ("acceptEdits", "default"):
            with self.subTest(mode=mode):
                _, output = self.doctor_output({"claude": True, "codex": False},
                                               installed=("claude",), claude_mode=mode)
                row = next(line for line in output.splitlines() if mode in line and "permission" in line.lower())
                self.assertIn(setup.ui.OK, row)


class DesktopProfilesTests(unittest.TestCase):
    def test_install_does_not_offer_a_disabled_claude_desktop_integration(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(core.CFG, {"agents": {"claude": False, "codex": True}}), \
                mock.patch.object(setup, "CLAUDE_APP", Path(tmp)), \
                mock.patch.object(desktop, "status", return_value={"installed": False}), \
                mock.patch.object(desktop, "pack", return_value={"bundle": str(Path(tmp) / "devkit.mcpb")}), \
                mock.patch.object(sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            setup.setup_desktop()
        self.assertIn("turned off", output.getvalue())
        self.assertNotIn("open ", output.getvalue())

    def app_rows(self, agents):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp)
            with mock.patch.dict(core.CFG, {"agents": agents}), \
                    mock.patch.object(setup, "CLAUDE_APP", app), \
                    mock.patch.object(setup, "CHATGPT_APP", app), \
                    mock.patch.object(setup, "CHATGPT_CODEX", app / "absent"), \
                    mock.patch.object(setup, "_surface_sessions", return_value=[]), \
                    mock.patch.object(canary, "by_surface", return_value={}), \
                    mock.patch.object(desktop, "status", return_value={"installed": False}), \
                    mock.patch.object(desktop, "bypass_mode", return_value=(None, "")):
                return setup.app_checks()

    def test_codex_only_does_not_require_the_claude_desktop_extension(self):
        rows = self.app_rows({"claude": False, "codex": True})
        self.assertFalse([row for row in rows if row[0] == "Claude Desktop"], rows)

    def test_selected_claude_desktop_missing_extension_still_fails(self):
        rows = self.app_rows({"claude": True, "codex": False})
        extension = next(row for row in rows if row[1] == "devkit extension")
        self.assertFalse(extension[2])
        self.assertEqual(extension[4], "dev install --only desktop")


class ExtensionProfilesTests(unittest.TestCase):
    def extension_rows(self, selected):
        team = {key: {"plugins": ["required@example"], "meta": {"required@example": {
            "why": "Required workflow", "license": "MIT", "required": True}}}
                for key in ("claude", "codex")}
        team["skills"] = {"sources": []}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(core.CFG, {"agents": selected, "ext": {}, "skills": {"groups": []}}))
            for name, result in (("team", team), ("observe", {}), ("read_lock", {}),
                                 ("project_plugins", {}), ("managed_plugins", {})):
                stack.enter_context(mock.patch.object(ext, name, return_value=result))
            stack.enter_context(mock.patch.object(ext, "report_drift", return_value=[]))
            return ext.doctor_checks()

    def test_missing_extensions_of_disabled_agents_are_not_reported_as_required(self):
        for disabled in ("claude", "codex"):
            with self.subTest(disabled=disabled):
                rows = self.extension_rows({key: key != disabled for key in ("claude", "codex")})
                required = next(row for row in rows if row[1] == "required extensions on")
                self.assertNotIn(f"({disabled})", required[3])

    def test_missing_extensions_of_selected_agents_remain_required(self):
        rows = self.extension_rows({"claude": True, "codex": False})
        required = next(row for row in rows if row[1] == "required extensions on")
        self.assertFalse(required[2])
        self.assertIn("(claude)", required[3])


class OptionalSkillProfilesTests(unittest.TestCase):
    def test_doctor_coverage_respects_optional_groups_without_disabling_work_rules(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent), \
                    mock.patch.dict(core.CFG, {"skills": {"groups": []}}), \
                    mock.patch.object(skillguard, "installed", return_value=set()):
                gaps = dict(skillguard.coverage(agent))
                self.assertNotIn("postgres", gaps)
                self.assertIn("api", gaps)
                pg = next(r for r in skillguard.rules() if r["id"] == "postgres")
                self.assertIn("supabase-postgres-best-practices", skillguard.required(pg, agent))

    def test_all_groups_enables_optional_work_rules(self):
        with mock.patch.dict(core.CFG, {"skills": {"groups": ["all"]}}):
            self.assertIn("terraform", [r["id"] for r in skillguard.rules()])

    def check_postgres(self, groups):
        roster = {"members": [{"id": "dozer", "skills": ["supabase-postgres-best-practices"]}], "activities": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            return setup.crew_skill_checks(["claude"], roster=roster,
                                           homes={"claude": (root, root / "skills")}, groups=groups)[0]

    def test_explicitly_omitted_optional_group_does_not_require_its_skill(self):
        row = self.check_postgres([])
        self.assertTrue(row[2], row[3])

    def test_selected_optional_group_still_requires_its_skill(self):
        row = self.check_postgres(["postgres"])
        self.assertFalse(row[2])
        self.assertIn("supabase-postgres-best-practices", row[3])

    def test_all_groups_requires_an_opt_in_skill(self):
        roster = {"members": [{"id": "niobe", "skills": ["terraform-style-guide"]}], "activities": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            row = setup.crew_skill_checks(["claude"], roster=roster,
                                          homes={"claude": (root, root / "skills")}, groups=["all"])[0]
        self.assertFalse(row[2], row[3])


if __name__ == "__main__":
    unittest.main()
