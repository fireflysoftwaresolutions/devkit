# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Codex plugins have no slash commands: devkit's Codex copy carries each command as a skill of the same name."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import setup  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class CodexPluginTests(unittest.TestCase):
    def test_every_command_becomes_a_skill_and_shared_skills_stay(self):
        saved = setup.CODEX_MARKET
        with tempfile.TemporaryDirectory() as d:
            setup.CODEX_MARKET = Path(d) / "market"
            try:
                root = setup.build_codex_plugin()
            finally:
                setup.CODEX_MARKET = saved
            skills = {p.name for p in (root / "plugin" / "skills").iterdir()}
            shared = {p.name for p in (ROOT / "plugin" / "skills").iterdir() if (p / "SKILL.md").exists()}
            commands = {p.stem for p in (ROOT / "plugin" / "commands").glob("*.md")}
            self.assertTrue(shared <= skills)
            self.assertTrue(commands <= skills, commands - skills)
            self.assertFalse(commands & shared, "a command and a skill share a name: Claude Code would show two")
            for name in commands:
                text = (root / "plugin" / "skills" / name / "SKILL.md").read_text()
                m = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", text)
                self.assertIsNotNone(m, name)
                self.assertEqual(m.group(1), name)
                self.assertTrue(json.loads(m.group(2)))
                self.assertNotIn("$ARGUMENTS", text)
            market = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
            self.assertEqual(market["plugins"][0]["source"]["path"], "./plugin")
            self.assertTrue((root / "plugin" / ".codex-plugin" / "plugin.json").exists())



class InstalledCurrencyTests(unittest.TestCase):
    """doctor must tell an outdated install from a current one, for Claude and Codex alike, not just that a file
    exists: the instructions and the crew roles are compared with what this checkout would write."""

    def test_instructions_are_current_only_when_they_match_this_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "AGENTS.md"
            self.assertEqual(setup.instructions_state(f), "missing")
            f.write_text("# my own rules\n")
            self.assertEqual(setup.instructions_state(f), "hand-written")
            f.write_text(setup.render_instructions())
            self.assertEqual(setup.instructions_state(f), "current")
            f.write_text(setup.render_instructions().replace("dev progress note", "dev progress step", 1))
            self.assertEqual(setup.instructions_state(f), "outdated")  # an older devkit wrote it

    def test_the_newest_plugin_copy_is_compared_by_version_not_by_text(self):
        with tempfile.TemporaryDirectory() as d:
            src, cache = Path(d) / "src", Path(d) / "cache"
            (src / "skills").mkdir(parents=True)
            (src / "skills" / "a.md").write_text("new\n")
            for v, body in (("26.11.2", "old\n"), ("26.11.10", "new\n")):
                (cache / v / "skills").mkdir(parents=True)
                (cache / v / "skills" / "a.md").write_text(body)
            self.assertFalse(setup.plugin_copy_stale(cache, src))  # 26.11.10 is the newest, and it is current

    def test_stale_crew_roles_are_named(self):
        from devkit import crew
        roles = crew.codex_roles()
        with tempfile.TemporaryDirectory() as d:
            for name, role in roles.items():
                (Path(d) / f"{name}.toml").write_text(setup.codex_role_text(role))
            self.assertEqual(setup.stale_codex_roles(Path(d), roles), [])
            (Path(d) / "neo.toml").write_text(setup.codex_role_text(roles["neo"]).replace("Neo", "Neo (old)", 1))
            (Path(d) / "tank.toml").unlink()
            self.assertEqual(setup.stale_codex_roles(Path(d), roles), ["neo", "tank"])

if __name__ == "__main__":
    unittest.main()
