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


if __name__ == "__main__":
    unittest.main()
