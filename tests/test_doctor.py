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
from unittest import mock

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
        # iac is turned on here, so its skill is owed (Morpheus, M199: a group the developer asked for must install)
        homes = {"claude": self.agent("claude", "testing", "superpowers:brainstorming")}
        [(group, name, ok, detail, fix)] = setup.crew_skill_checks(["claude"], roster=ROSTER, homes=homes,
                                                                   groups=["iac"])
        self.assertEqual((group, name, ok), ("Agents", "crew skills (claude)", False))
        self.assertIn("terraform-style-guide", detail)
        self.assertIn("1 of 3", detail)
        self.assertIn("skills.groups", fix)
        self.assertIn("dev install --only skills", fix)

    def test_a_skill_of_an_opt_in_group_that_is_off_only_informs(self):
        # Morpheus, M199: the row passes, and says who names the skill and the line that turns its group on
        homes = {"claude": self.agent("claude", "testing", "superpowers:brainstorming")}
        [(_, _, ok, detail, fix)] = setup.crew_skill_checks(["claude"], roster=ROSTER, homes=homes, groups=[])
        self.assertTrue(ok)
        self.assertIn("terraform-style-guide", detail)
        self.assertIn("niobe", detail)
        self.assertIn('add "iac" to skills.groups', detail)
        self.assertEqual(fix, "")

    def test_a_missing_skill_of_a_default_group_fails_even_when_the_group_is_off(self):
        roster = {"members": [{"id": "dozer", "skills": ["supabase-postgres-best-practices"]}], "activities": []}
        homes = {"claude": self.agent("claude")}
        [(_, _, ok, detail, _)] = setup.crew_skill_checks(["claude"], roster=roster, homes=homes, groups=[])
        self.assertFalse(ok)
        self.assertIn("supabase-postgres-best-practices (postgres)", detail)

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


class RequiredToolsTests(unittest.TestCase):
    # The required-tools row follows the stacks the wizard recorded (config `stacks`): the base tools on every Mac,
    # a stack's tools only when the developer chose it. `have` stands in for PATH, so the developer's Mac never counts.
    JAVA_PHP = {"java", "mvn", "gradle", "jdtls", "php", "composer", "intelephense"}

    def setUp(self):
        # both agents on (the default), whatever the developer's own config says
        patch = mock.patch.dict(setup.CFG, {"agents": {}})
        patch.start()
        self.addCleanup(patch.stop)

    def test_python_and_node_without_java_or_php_tools_pass(self):
        # the Recommended setup: 24 tools before, 7 of them for the Java and PHP stacks nobody chose
        group, name, ok, detail, fix = setup.tools_check(["python", "node"], have=lambda t: t not in self.JAVA_PHP)
        self.assertEqual((group, name, ok), ("Toolchain", "required tools", True))
        self.assertIn("17/17 present", detail)
        self.assertIn("not chosen: Java / Spring, PHP", detail)
        self.assertIn("dev setup", detail)
        self.assertEqual(fix, "")

    def test_java_chosen_without_maven_fails_with_its_install_command(self):
        php = {"php", "composer", "intelephense"}
        _, _, ok, detail, fix = setup.tools_check(["java", "python", "node"],
                                                  have=lambda t: t != "mvn" and t not in php)
        self.assertFalse(ok)
        self.assertIn("missing: mvn", detail)
        self.assertEqual(fix, "brew install maven")

    def test_a_missing_language_server_names_its_npm_install(self):
        _, _, ok, detail, fix = setup.tools_check(["python"], have=lambda t: t != "pyright")
        self.assertFalse(ok)
        self.assertIn("missing: pyright", detail)
        self.assertEqual(fix, "npm install -g pyright")

    def test_an_agent_turned_off_in_config_is_not_required(self):
        # config agents.codex false, as `dev setup` records it: `dev install` skips Codex, so doctor must not want it
        with mock.patch.dict(setup.CFG, {"agents": {"claude": True, "codex": False}}):
            _, _, ok, detail, fix = setup.tools_check(["python"], have=lambda t: t != "codex")
        self.assertTrue(ok)
        self.assertIn("turned off: Codex", detail)
        self.assertEqual(fix, "")

    def test_an_agent_left_on_stays_required_when_the_other_is_off(self):
        with mock.patch.dict(setup.CFG, {"agents": {"claude": False}}):
            _, _, ok, detail, fix = setup.tools_check(["python"], have=lambda t: t not in ("claude", "codex"))
        self.assertFalse(ok)
        self.assertIn("missing: codex", detail)
        self.assertNotIn("claude", detail.split("missing: ")[1].split(" · ")[0])
        self.assertEqual(fix, "brew install --cask codex")


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
