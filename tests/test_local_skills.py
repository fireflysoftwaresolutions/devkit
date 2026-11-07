# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# devkit's own shared skills live in agents/skills. Git keeps no empty folder, so a fresh clone or a release archive
# has no agents/skills at all; the skills step must then link only the third-party skills instead of crashing.
# Run: python3 -m unittest tests.test_local_skills
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import setup  # noqa: E402


class LocalSkillsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_fresh_clone_without_the_skills_folder_has_no_local_skills(self):
        (self.home / "agents").mkdir()
        self.assertEqual(setup.local_skills(self.home), {})

    def test_only_folders_with_a_skill_file_count(self):
        root = self.home / "agents" / "skills"
        (root / "review").mkdir(parents=True)
        (root / "review" / "SKILL.md").write_text("---\nname: review\n---\n")
        (root / "notes").mkdir()
        self.assertEqual(setup.local_skills(self.home), {"review": root / "review"})


if __name__ == "__main__":
    unittest.main()
