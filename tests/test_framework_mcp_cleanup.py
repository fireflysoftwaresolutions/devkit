# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Framework deselection removes only exact managed MCP configurations."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from devkit import core, ext, policy, setup


class FrameworkMcpCleanupTests(unittest.TestCase):
    def test_table_removal_preserves_headers_inside_another_tables_string(self):
        kept = '[personal]\nmessage = """\n[mcp_servers.angular-cli]\npersonal text\n"""\n'
        text = kept + '[mcp_servers.angular-cli]\ncommand = "npx"\n'
        self.assertEqual(setup._without_toml_tables(text, ["mcp_servers.angular-cli"]), kept)

    def test_quoted_sibling_server_survives_managed_mcp_removal(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            base = Path(tmp)
            stack.enter_context(mock.patch.dict(core.CFG, {"frameworks": [], "ext": {}}))
            stack.enter_context(mock.patch.object(setup, "CODEX_HOME", base))
            stack.enter_context(mock.patch.object(setup, "STATE", base / "state"))
            stack.enter_context(mock.patch.object(core, "older_copy", return_value=None))
            stack.enter_context(mock.patch.object(setup, "_audit"))
            spec = ext.team()["codex"]["mcp"]["angular-cli"]
            sibling = '[mcp_servers."angular-cli.private"]\ncommand = "personal-server"\n'
            config = base / "config.toml"
            config.write_text('[mcp_servers.angular-cli]\ncommand = "npx"\nargs = '
                              + json.dumps(spec["args"]) + '\n\n' + sibling)
            self.assertEqual(setup.remove_unselected_mcp("codex"), ["angular-cli"])
            self.assertIn(sibling, config.read_text())

    def exercise_install(self, agent, managed, remove_rc=0, overlay=None):
        cfg = core.deep_merge(core.DEFAULTS, {"frameworks": [], "stacks": ["node"], "ext": overlay or {}})
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            base = Path(tmp)
            home = base / agent
            home.mkdir()
            stack.enter_context(mock.patch.dict(core.CFG, cfg, clear=True))
            spec = dict(ext.team()[agent]["mcp"]["angular-cli"])
            if not managed:
                spec["env"] = {"MY_WORKSPACE": "personal"}
            if agent == "claude":
                config = base / ".claude.json"
                config.write_text(json.dumps({"numStartups": 8, "mcpServers": {"angular-cli": spec}}))
            else:
                config = home / "config.toml"
                text = 'model = "personal"\n\n[mcp_servers.angular-cli]\ncommand = "npx"\nargs = ' + \
                    json.dumps(spec["args"]) + '\n'
                if not managed:
                    text += '[mcp_servers.angular-cli.env]\nMY_WORKSPACE = "personal"\n'
                config.write_text(text)
            original = config.read_text()
            commands = []

            def command(argv, **kwargs):
                commands.append(argv)
                if argv[:3] == ["claude", "mcp", "remove"]:
                    if remove_rc:
                        return remove_rc, "", "the agent refused removal"
                    doc = json.loads(config.read_text())
                    doc["mcpServers"].pop(argv[-1], None)
                    config.write_text(json.dumps(doc))
                return 0, "", ""

            for name, value in (("HOME", base), ("STATE", base / "state"),
                                ("CLAUDE_HOME", home if agent == "claude" else base / "claude"),
                                ("CODEX_HOME", home if agent == "codex" else base / "codex")):
                stack.enter_context(mock.patch.object(setup, name, value))
            stack.enter_context(mock.patch.object(core, "have", return_value=True))
            stack.enter_context(mock.patch.object(core, "older_copy", return_value=None))
            stack.enter_context(mock.patch.object(setup, "sh", side_effect=command))
            stack.enter_context(mock.patch.object(setup, "_audit"))
            stack.enter_context(mock.patch.object(setup, "plugin_copy_stale", return_value=False))
            stack.enter_context(mock.patch.object(setup, "_extensions"))
            stack.enter_context(mock.patch.object(policy, "lock_status", return_value="off"))
            for name in ("write_claude_settings", "write_codex_config", "write_codex_profiles", "write_codex_agents"):
                stack.enter_context(mock.patch.object(setup, name))
            stack.enter_context(mock.patch.object(setup, "build_codex_plugin", return_value=base / "plugin"))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                getattr(setup, "setup_" + agent)()
            first = config.read_text()
            with contextlib.redirect_stdout(output):
                getattr(setup, "setup_" + agent)()
            self.assertEqual(config.read_text(), first)
            self.output = output.getvalue()
            if remove_rc:
                self.assertIn("dev install --only claude", self.output)
                self.remaining = setup.remove_unselected_mcp(agent, dry=True)
            backups = [p.read_text() for p in (base / "state" / "backups").glob(config.name + ".*")]
            return original, first, backups, commands

    def test_deselection_removes_owned_mcp_and_backs_up_original(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                original, result, backups, _ = self.exercise_install(agent, managed=True)
                self.assertNotIn("angular-cli", result)
                self.assertIn(original, backups)
                self.assertIn("numStartups" if agent == "claude" else 'model = "personal"', result)

    def test_deselection_preserves_user_mcp_with_same_name_and_different_environment(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                original, result, backups, commands = self.exercise_install(agent, managed=False)
                self.assertEqual(result, original)
                self.assertEqual(backups, [])
                self.assertNotIn(["claude", "mcp", "remove", "-s", "user", "angular-cli"], commands)

    def test_failed_removal_preserves_original_and_remains_diagnosable(self):
        original, result, backups, _ = self.exercise_install("claude", managed=True, remove_rc=1)
        self.assertEqual(result, original)
        self.assertIn(original, backups)
        self.assertEqual(self.remaining, ["angular-cli"])
        self.assertIn("dev install --only claude", self.output)

    def test_removed_catalog_entry_remains_an_exact_ownership_candidate(self):
        for agent in ("claude", "codex"):
            for managed in (False, True):
                with self.subTest(agent=agent, managed=managed):
                    original, result, _, _ = self.exercise_install(
                        agent, managed, overlay={"remove": [f"mcp:{agent}:angular-cli"]})
                    if managed:
                        self.assertNotIn("angular-cli", result)
                    else:
                        self.assertEqual(result, original)

    def test_replaced_catalog_entry_keeps_both_validated_ownership_shapes(self):
        original = {"command": "npx", "args": ["-y", "example-server@1.0.0"]}
        replacement = {"command": "npx", "args": ["-y", "example-server@2.0.0"], "env": {"MODE": "owned"}}
        team = {"codex": {"mcp": {"example": original}}}
        overlay = {"add": [{"kind": "mcp", "name": "example", "agents": ["codex"],
                             **replacement,
                             "why": "Personal integration", "license": "MIT"}],
                   "remove": ["mcp:codex:example"]}
        inventory = ext.mcp_inventory("codex", team, overlay)
        for spec, owned in ((original, True), (replacement, True),
                            ({**replacement, "env": {"MODE": "personal"}}, False)):
            with self.subTest(spec=spec):
                self.assertEqual(setup._devkit_mcp(inventory, {"example": spec}), ["example"] if owned else [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

