# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# devkit's drift checks (lib/devkit/selfcheck.py, D1-D11 of the crew gap review 2), so CI catches the moment one
# part of devkit stops matching another. Each repository check runs on the real tree and must find exactly the drift
# listed in KNOWN: a new finding fails (fix it, or list it with the task that removes it), and so does a listed one
# that is gone (take it out of KNOWN). The machine checks (D2, D8, the lesson half of D10) read this Mac's state, so
# their tests run on small fixtures; `dev doctor` runs them for real. Tests carry comments, not docstrings: `dev test
# prove` leaves out tests with a docstring (B-fxntwum5l62). Run: python3 -m unittest tests.test_consistency
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cli, selfcheck  # noqa: E402


def owned(owner, *items):
    return {item: owner for item in items}


# Drift that exists today, each with the review finding, its owner and the task or plan step that removes it.
# Plan steps are those of the 26.11.3 run ("step 17 crew docs" is `dev progress` step 17).
KNOWN = {
    "D1": owned("R2-5 · QA engineer (Mouse): a test that calls the function · Morpheus to assign (M102)",
                "A run ends only after each Accountable member approved",
                "No step starts before its dependencies are done",
                "Only Morpheus plans, assigns, decides and changes the mode",
                "A forged HOME, DEVKIT_CONFIG or dev link cannot point devkit at fake state",
                "Team changes to the crew carry a reason"),
    "D3": {**owned("R2-1 · Backend engineer (Tank) · step 62 language-neutral hooks",
                   "crew.catalog", "lessons.for_call", "lessons.for_output"),
           **owned("found by D3, not in review 2 · wire or remove · Morpheus to assign (M102)",
                   "lessons.stats_json")},
    "D4": {**owned("R2-13 · Technical writer (Ghost) · step 17 crew docs",
                   "commands.md: dev progress assign --activity", "commands.md: dev crew brief --activity",
                   "commands.md: dev lesson add --file", "commands.md: dev lesson add --command",
                   "commands.md: dev lesson add --activity", "commands.md: dev lesson add --error",
                   "commands.md: dev lesson add --stack", "commands.md: dev lesson anchor",
                   "commands.md: dev lesson anchor --file", "commands.md: dev lesson anchor --command",
                   "commands.md: dev lesson anchor --activity", "commands.md: dev lesson anchor --error",
                   "commands.md: dev lesson anchor --stack", "commands.md: dev audit --quiet",
                   "commands.md: dev pr create --fill", "commands.md: dev pr diff --stat",
                   "commands.md: dev mcp", "commands.md: dev desktop"),
           # a real bug: argparse refuses `dev lesson ls --search …`; the option belongs before `ls`
           **owned("found by D4 · Technical writer (Ghost) · step 17 crew docs",
                   'docs/lessons.md: dev lesson ls --search "release"', "docs/lessons.md: dev lesson ls --all",
                   'plugin/skills/learning-loop/SKILL.md: dev lesson ls --search "<area>"'),
           **owned("R2-14 · Technical writer (Ghost), DevEx engineer (Kid) consulted · step 17 crew docs",
                   *("using-devkit: dev " + c for c in (
                       "adopt", "open", "claude", "codex", "handoff-import", "note", "sleep", "stubs", "dashboard",
                       "agents", "plugin", "sync", "gc", "du", "canary", "rebase", "issue", "git", "gh", "mcp",
                       "desktop", "install", "setup", "update", "docker", "version")))},
    "D5": owned("R2-13 · Technical writer (Ghost) · T-fxrngz6uua0 docs follow-ups (hook events)",
                "agents.md: PostToolUseFailure (claude)", "surfaces.md: Hooks column 1 (claude)",
                "surfaces.md: Hooks column 2 (claude)", "surfaces.md: the Claude Code event list"),
    "D6": owned("R2-13 · Technical writer (Ghost) · step 17 crew docs",
                "ai", "crew.gates.enforce", "crew.route_hint", "dashboard.idle_minutes", "progress.max_depth",
                "progress.quota_percent"),
    "D7": {**owned("R2-9 · Tech lead (Morpheus) with dev crew raci --set; Kid makes raci.check refuse them",
                   "cicd: A keymaker in C", "performance: A morpheus in I", "tooling: ghost in C and I",
                   "release: ghost in C and I", "plan: A morpheus in R", "scope: A hamann in R",
                   "incident: A lock in R"),
           **owned("R2-9 · a second approver for compliance is the developer's decision",
                   "compliance: A persephone in R"),
           **owned("R2-8 · Tech lead (Morpheus) with dev crew raci --set", "lookup: no skill")},
    "D9": owned("R2-6 · Tech lead (Morpheus) for the roster, DevEx engineer (Kid) for gates.py",
                "architect Record: proof dev plan", "trainman Prove: proof dev ci"),
    "D10": owned("R2-10 · DevEx engineer (Kid): FILE_HINTS globs, or `no_files` with its reason",
                 *(a + ": no file anchor" for a in (
                     "accessibility", "ai", "architecture", "backup", "codebase", "compliance", "data-analysis",
                     "dependencies", "design-system", "feature", "i18n", "incident", "lookup", "migration",
                     "performance", "plan", "redteam", "release", "requirements", "research", "review", "scope",
                     "security", "sso", "ux"))),
    "D11": owned("R2-5 · Tech lead (Morpheus) gives the matrix rows ids; Technical writer (Ghost) cites them · "
                 "step 17 crew docs",
                 "At most four members at once", "Separate files for parallel members", "A member stays in its scope",
                 "The main session respects a member's files", "Read-only members never edit", "No nested launches",
                 "Steps change only with evidence", "Mistakes are fixable and visible",
                 "A member that never reports back", "Every report is checked", "Decisions that are yours",
                 "Only Morpheus records your answers"),
}


class RepositoryDrift(unittest.TestCase):
    def assert_known(self, check, findings):
        found = {f.item: f for f in findings}
        known = KNOWN.get(check, {})
        new = [found[i] for i in sorted(set(found) - set(known))]
        gone = sorted(set(known) - set(found))
        lines = [f"new drift: {f.item} · {f.evidence} · fix: {f.fix}" for f in new]
        lines += [f"fixed, take it out of KNOWN[{check!r}]: {i} ({known[i]})" for i in gone]
        self.assertFalse(lines, f"{check}:\n  " + "\n  ".join(lines))

    # D1: every enforced row of crew.ENFORCEMENT imports and is named by a test
    def test_d1_enforced_rows_import_and_are_tested(self):
        self.assert_known("D1", selfcheck.d1_enforced_rows())

    # D3: no public function of the crew's modules is left without a caller
    def test_d3_public_functions_are_wired(self):
        self.assert_known("D3", selfcheck.d3_unwired())

    # D4: every command and option is documented, and every documented dev command parses
    def test_d4_commands_are_documented_and_resolve(self):
        self.assert_known("D4", selfcheck.d4_commands())

    # D5: the registered hook events match the hooks table and the counts on the surfaces page
    def test_d5_hook_events_match_the_docs(self):
        self.assert_known("D5", selfcheck.d5_hook_events())

    # D6: every configuration key the code reads is in docs/configuration.md
    def test_d6_config_keys_are_documented(self):
        self.assert_known("D6", selfcheck.d6_config_keys())

    # D7: RACI hygiene for every kind of work in config/crew.json
    def test_d7_raci_hygiene(self):
        self.assert_known("D7", selfcheck.d7_raci_hygiene())

    # D9: each phase's proof and the commands its gate names are in the phase's devkit list
    def test_d9_phase_proofs_are_listed(self):
        self.assert_known("D9", selfcheck.d9_phase_proofs())

    # D10: every kind of work can be reached from files, or says it has none
    def test_d10_kinds_of_work_have_file_anchors(self):
        self.assert_known("D10", selfcheck.d10_file_anchors())

    # D11: each guardrail row of docs/crew.md cites the matrix row it describes
    def test_d11_guardrails_cite_the_matrix(self):
        self.assert_known("D11", selfcheck.d11_guardrails())

    # run() covers D1 to D11, so a check added to the module is also one `dev doctor` and CI run
    def test_run_covers_every_check(self):
        ids = {cid for cid, _ in selfcheck.REPO_CHECKS + selfcheck.MACHINE_CHECKS}
        self.assertEqual(ids, {f"D{i}" for i in range(1, 12)})


class CommandResolution(unittest.TestCase):
    # Every command and option the parser has, written out as a docs line would, resolves; the same line with an
    # unknown last command or option does not. Enumerates the whole tree, so the resolver's rules hold for all of it.
    def test_every_parser_command_and_option_resolves_and_a_wrong_one_does_not(self):
        parser = cli.build_parser()
        tree = selfcheck.command_tree(parser)
        self.assertGreater(len(tree), 100)
        for path, p in tree:
            cmd = "dev " + " ".join(path)
            self.assertIsNone(selfcheck.resolve(cmd, parser), cmd)
            for strings in selfcheck._options(p):
                for opt in strings:
                    self.assertIsNone(selfcheck.resolve(f"{cmd} {opt} VALUE", parser), f"{cmd} {opt}")
            self.assertIsNotNone(selfcheck.resolve(cmd + " --no-such-option", parser), cmd)
            subs = selfcheck._subactions(p)
            positional = [a for a in p._actions if not a.option_strings and a.dest != "help" and a not in subs]
            if subs and not positional:
                self.assertIsNotNone(selfcheck.resolve(cmd + " nosuchverb", parser), cmd)

    # An option of the parent command written after the subcommand is refused, as argparse refuses it
    def test_a_parent_option_after_the_subcommand_does_not_resolve(self):
        self.assertIn("--search", selfcheck.resolve('dev lesson ls --search "x"') or "")
        self.assertIsNone(selfcheck.resolve('dev lesson --search "x" ls'))
        self.assertIsNone(selfcheck.resolve("dev task -i payments"))
        self.assertIsNone(selfcheck.resolve('dev pr create|checks --watch|merge'))
        self.assertIn("nosuch", selfcheck.resolve("dev pr create|nosuch") or "")


class MachineDrift(unittest.TestCase):
    ROWS = (("Closed step", "w", "step a", "planned"), ("Open step", "w", "step b", "planned"),
            ("Closed task", "w", "T-abcdef1 the parser", "planned"), ("Unknown", "w", "nothing like it", "planned"),
            ("Enforced", "w", "devkit.selfcheck.run", "enforced"))

    # D2: a planned row whose step or task is closed, or that names neither, is drift; an open one is not
    def test_d2_planned_rows_name_open_work(self):
        found = selfcheck.d2_planned_rows(steps={"step a": "done", "Step B": "running"},
                                          items={"T-abcdef1": "done"}, rows=self.ROWS)
        self.assertEqual(sorted(f.item for f in found), ["Closed step", "Closed task", "Unknown"])
        self.assertIn("is done", next(f.evidence for f in found if f.item == "Closed step"))

    # D8: a crew skill missing for the agent is named with its group and install command; installed ones are not
    def test_d8_uninstalled_skills_name_their_group(self):
        with tempfile.TemporaryDirectory() as d:
            home, skills = Path(d) / "agent", Path(d) / "skills"
            for f in (home / "plugins/cache/mk/superpowers/1.0/skills/brainstorming/SKILL.md",
                      skills / "testing" / "SKILL.md"):
                f.parent.mkdir(parents=True)
                f.write_text("x")
            skills_named = ["testing", "terraform-style-guide", "superpowers:brainstorming",
                            "superpowers:writing-plans"]
            roster = {"members": [{"id": "niobe", "skills": skills_named}], "activities": []}
            found = {f.item: f for f in selfcheck.d8_uninstalled_skills("claude", roster=roster, agent_home=home,
                                                                        skills_dir=skills)}
        self.assertEqual(sorted(found), ["claude: superpowers:writing-plans", "claude: terraform-style-guide"])
        self.assertIn("(group iac)", found["claude: terraform-style-guide"].evidence)
        self.assertIn("skills.groups", found["claude: terraform-style-guide"].fix)
        self.assertEqual(found["claude: superpowers:writing-plans"].fix, "dev install --only claude")

    # D10, lessons: an open lesson without anchors is drift; an anchored or prevented one is not
    def test_d10_open_lessons_have_anchors(self):
        lessons = [{"meta": {"id": "L-1", "title": "bare", "status": "open"}},
                   {"meta": {"id": "L-2", "title": "anchored", "status": "open", "files": ["lib/x.py"]}},
                   {"meta": {"id": "L-3", "title": "automated", "status": "prevented"}}]
        self.assertEqual([f.item for f in selfcheck.d10_lesson_anchors(lessons)], ["lesson L-1"])


if __name__ == "__main__":
    unittest.main()
