# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Selected framework capabilities, extension overrides, and managed ownership."""
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from devkit import core, ext, setup, skillguard


def configured(**changes):
    cfg = core.deep_merge(core.DEFAULTS, {
        "stacks": ["python"], "frameworks": [], "ext": {},
        "agents": {"claude": True, "codex": True}, "skills": {"groups": ["all"]},
    })
    cfg.update(changes)
    return mock.patch.dict(core.CFG, cfg, clear=True)


class FrameworkExtensionTests(unittest.TestCase):
    def test_omitted_framework_mcp_is_in_inventory_but_not_installed(self):
        with configured():
            inventory = {e.id for e in ext.catalog(ext.team(), {}, None)}
            for agent in ("claude", "codex"):
                self.assertIn(f"mcp:{agent}:angular-cli", inventory)
                selected = ext.effective_mcp(agent)
                self.assertNotIn("angular-cli", selected)
                self.assertIn("chrome-devtools", selected)

    def test_claude_language_plugins_follow_effective_stacks(self):
        with configured(stacks=[], frameworks=["spring"]):
            on, off = ext.effective_claude_plugins(ext.team(), {})
        self.assertIn("jdtls-lsp@claude-plugins-official", on)
        for name in ("pyright-lsp", "typescript-lsp", "php-lsp"):
            self.assertNotIn(name + "@claude-plugins-official", on)
            self.assertIn(name + "@claude-plugins-official", off)
        self.assertIn("security-guidance@claude-plugins-official", on)

    def test_mixed_skill_source_keeps_docs_and_security_without_spring(self):
        with configured():
            sources = ext.effective_skill_sources()
        names = {name for src in sources for name in src["skills"]}
        self.assertNotIn("spring-security", names)
        self.assertNotIn("java-springboot", names)
        self.assertNotIn("java-junit", names)
        self.assertIn("documentation-writer", names)
        self.assertIn("threat-model-analyst", names)

    def test_nextjs_gets_react_skills_only_for_selected_agent(self):
        with configured(stacks=[], frameworks=["nextjs"], agents={"claude": False, "codex": True}):
            sources = ext.effective_skill_sources()
            self.assertEqual(ext.effective_mcp("claude"), {})
        names = {name for src in sources for name in src["skills"]}
        self.assertIn("react-best-practices", names)
        self.assertIn("composition-patterns", names)
        self.assertNotIn("angular-developer", names)
        self.assertTrue(sources)
        self.assertTrue(all(src.get("agents") == ["codex"] for src in sources))

    def test_explicit_enable_cannot_override_disable_or_inactive_agent(self):
        c, x = "mcp:claude:angular-cli", "mcp:codex:angular-cli"
        with configured(ext={"enable": [c, x]}, agents={"claude": False, "codex": True}):
            self.assertEqual(ext.effective_mcp("claude"), {})
            self.assertIn("angular-cli", ext.effective_mcp("codex"))
        with configured(ext={"enable": [x], "disable": [x]}):
            self.assertNotIn("angular-cli", ext.effective_mcp("codex"))

    def test_current_rows_do_not_report_unselected_framework_as_missing(self):
        with configured(), contextlib.ExitStack() as stack:
            for name in ("observe", "read_lock", "project_plugins", "managed_plugins"):
                stack.enter_context(mock.patch.object(ext, name, return_value={}))
            rows = ext.current_rows()
        angular = next(r for r in rows if r["id"] == "mcp:codex:angular-cli")
        self.assertFalse(angular["enabled"])
        self.assertFalse(angular["drift"])
        security = next(r for r in rows if r["id"] == "plugin:claude:security-guidance@claude-plugins-official")
        self.assertIn("required-off", {d["kind"] for d in security["drift"]})

    def test_baseline_coverage_omits_unselected_framework_but_task_rules_remain(self):
        with configured(), mock.patch.object(skillguard, "installed", return_value=set()):
            missing = {s for _, skills in skillguard.coverage("codex") for s in skills}
            required = {s for rule in skillguard.rules() for s in skillguard.required(rule, "codex")}
        self.assertNotIn("angular-developer", missing)
        self.assertNotIn("java-springboot", missing)
        self.assertIn("github-actions-hardening", missing)
        self.assertIn("angular-developer", required)
        self.assertIn("java-springboot", required)

    def test_crew_baseline_does_not_require_unselected_framework_skill(self):
        roster = {"members": [{"id": "trinity", "skills": ["angular-developer"]}], "activities": []}
        with tempfile.TemporaryDirectory() as tmp, configured():
            homes = {"codex": (Path(tmp), Path(tmp) / "skills")}
            [(_, _, ok, detail, fix)] = setup.crew_skill_checks(["codex"], roster=roster, homes=homes, groups=["all"])
        self.assertTrue(ok, detail)
        self.assertEqual(fix, "")

    def test_disabled_agent_cleanup_still_identifies_owned_mcp(self):
        with tempfile.TemporaryDirectory() as tmp, configured(agents={"claude": True, "codex": False}):
            self.assertEqual(ext.effective_mcp("codex"), {})
            base = Path(tmp)
            codex = base / "codex"
            codex.mkdir()
            spec = ext.team()["codex"]["mcp"]["angular-cli"]
            original = '[mcp_servers.angular-cli]\ncommand = "npx"\nargs = ' + json.dumps(spec["args"]) + '\n'
            (codex / "config.toml").write_text(original)
            with mock.patch.object(setup, "CODEX_HOME", codex), \
                    mock.patch.object(setup, "AGENTS_HOME", base / "agents"), \
                    mock.patch.object(setup, "SKILLS_SHARE", base / "skills"), \
                    mock.patch.object(setup, "codex_trusted_roots", return_value=[]), \
                    mock.patch.object(core, "have", return_value=False):
                gone = setup._remove_agent("codex", codex, dry=True)
            self.assertIn("MCP servers in config.toml (angular-cli)", gone)
            self.assertEqual((codex / "config.toml").read_text(), original)

    def test_baseline_notifications_do_not_require_extensions_of_inactive_agent(self):
        team = {agent: {"plugins": ["required@example"], "meta": {"required@example": {
            "why": "Required workflow", "license": "MIT", "required": True}}}
                for agent in ("claude", "codex")}
        team["skills"] = {"sources": []}
        for disabled in ("claude", "codex"):
            active = "codex" if disabled == "claude" else "claude"
            with self.subTest(disabled=disabled), configured(agents={active: True, disabled: False}), \
                    contextlib.ExitStack() as stack:
                observed = {f"plugin:{active}:required@example": {"installed": True, "enabled": True}}
                for name, result in (("team", team), ("observe", observed), ("read_lock", {}),
                                     ("project_plugins", {}), ("managed_plugins", {}),
                                     ("pending_acceptances", []), ("report_drift", [])):
                    stack.enter_context(mock.patch.object(ext, name, return_value=result))
                self.assertIn(f"plugin:{disabled}:required@example", {e.id for e in ext.catalog(team, {}, None)})
                self.assertIsNone(ext.session_line())
                self.assertEqual(ext.status_needs(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

