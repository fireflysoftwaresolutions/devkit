# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# `dev doctor`'s crew checks (lib/devkit/setup.py): each skill the crew names but this Mac has not installed for an
# agent (selfcheck D8) shows as a failed check with the line that fixes it. The tests use a small roster and agent
# folders of their own, never the developer's. Run: python3 -m unittest tests.test_doctor
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import setup  # noqa: E402

ROSTER = {"members": [{"id": "niobe", "skills": ["testing", "terraform-style-guide"],
                       "procedure": [{"phase": "Plan", "skills": ["superpowers:brainstorming"]}]}],
          "activities": [{"id": "iac", "knowledge": {"skills": ["testing"]}}]}


class CrewSkillCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def agent(self, name, *skills):
        # An agent's home and skills folder with these skills installed (`plugin:skill` in the plugin cache).
        home, sdir = self.root / name, self.root / name / "skills"
        for s in skills:
            f = (home / "plugins/cache/mk" / s.split(":")[0] / "1.0/skills" / s.split(":")[1] / "SKILL.md") \
                if ":" in s else sdir / s / "SKILL.md"
            f.parent.mkdir(parents=True)
            f.write_text("x")
        return home, sdir

    def test_a_crew_skill_missing_for_an_agent_fails_with_its_fix(self):
        homes = {"claude": self.agent("claude", "testing", "superpowers:brainstorming")}
        [(group, name, ok, detail, fix)] = setup.crew_skill_checks(["claude"], roster=ROSTER, homes=homes)
        self.assertEqual((group, name, ok), ("Agents", "crew skills (claude)", False))
        self.assertIn("terraform-style-guide", detail)
        self.assertIn("1 of 3", detail)
        self.assertIn("skills.groups", fix)
        self.assertIn("dev install --only skills", fix)

    def test_every_crew_skill_installed_passes_for_each_agent(self):
        everything = ("testing", "terraform-style-guide", "superpowers:brainstorming")
        homes = {"claude": self.agent("claude", *everything), "codex": self.agent("codex", *everything)}
        rows = setup.crew_skill_checks(["claude", "codex"], roster=ROSTER, homes=homes)
        self.assertEqual([(n, ok) for _, n, ok, _, _ in rows], [("crew skills (claude)", True),
                                                               ("crew skills (codex)", True)])
        self.assertIn("3 skills", rows[0][3])

    def test_a_missing_plugin_skill_names_the_agents_install(self):
        homes = {"codex": self.agent("codex", "testing", "terraform-style-guide")}
        [(_, _, ok, detail, fix)] = setup.crew_skill_checks(["codex"], roster=ROSTER, homes=homes)
        self.assertFalse(ok)
        self.assertIn("superpowers:brainstorming", detail)
        self.assertEqual(fix, "dev install --only codex")


class CrewRaciCheckTests(unittest.TestCase):
    BASE = {"activities": [{"id": "api", "R": ["tank"], "A": "seraph", "C": [], "I": []}]}

    def test_a_raci_error_the_team_added_fails_with_the_command_that_changes_it(self):
        team = {"activities": [{"id": "api", "R": ["tank"], "A": "seraph", "C": ["seraph"], "I": []}]}
        [(group, name, ok, detail, fix)] = setup.crew_raci_checks(base=self.BASE, team=team)
        self.assertEqual((group, name, ok), ("Agents", "crew RACI (team changes)", False))
        self.assertIn("api: A seraph in C", detail)
        self.assertIn("dev crew raci api --set", fix)

    def test_team_changes_without_raci_errors_pass(self):
        [(_, _, ok, _, _)] = setup.crew_raci_checks(base=self.BASE, team=self.BASE)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
