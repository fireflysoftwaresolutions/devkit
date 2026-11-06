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
    "D3": {**owned("R2-1 · Backend engineer (Tank) · step 62 language-neutral hooks",
                   "crew.catalog", "lessons.for_call", "lessons.for_output"),
           # the guard's F7 allowlist (ca3bdad) and F8 escape hatches (165a8b4) await their calls from hooks.py
           # (worktree_state has one: the crew gates read the worktree diff since a lane started)
           **owned("found by D3 · Backend engineer (Tank) · step F7 F8 hooks wiring",
                   "guard.scope_base", "guard.scope_owner_across", "guard.written_paths",
                   "guard.member_tool_verdict", "guard.record_bypasses", "guard.stubs_accepted"),
           **owned("C6 · Security engineer (The Keymaker) · step 74 F5 gate tiers: guard.irreversible_verdict calls it",
                   "progress.use_grant")},
    "D7": owned("R2-9 · the developer approves plan and scope once F6 accepts “developer” as an approver · "
                "step 36 F6 step endings and approvals",
                "plan: A morpheus in R", "scope: A hamann in R"),
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

    # D7, the RACI rules (R2-9): the approver is not consulted or informed, nobody is both, and an approver of its
    # own work records why; each rule on a small roster of its own
    def test_d7_each_raci_rule(self):
        def act(aid, **f):
            return {"id": aid, "R": ["tank"], "A": "seraph", "C": [], "I": [], **f}
        roster = {"activities": [act("ok"), act("ac", C=["seraph"]), act("ai", I=["seraph"]),
                                 act("ci", C=["kid", "neo"], I=["neo"]), act("self", R=["seraph"]),
                                 act("reason", R=["seraph"], self_approval="the reviewer reviews its own review")]}
        self.assertEqual(sorted(f.item for f in selfcheck.raci_errors(roster)),
                         ["ac: A seraph in C", "ai: A seraph in I", "ci: neo in C and I", "self: A seraph in R"])

    # D7, the team's changes (crew/crew.json in the handoff repository): only the RACI errors they add to devkit's
    # roster are named, as team findings; the roster's own known ones are D7's on the repository
    def test_d7_team_changes_name_only_the_raci_errors_they_add(self):
        base = {"activities": [{"id": "plan", "R": ["morpheus"], "A": "morpheus", "C": [], "I": []},
                               {"id": "api", "R": ["tank"], "A": "seraph", "C": [], "I": []}]}
        team = {"activities": [base["activities"][0],
                               {"id": "api", "R": ["tank"], "A": "seraph", "C": ["kid"], "I": ["kid"]},
                               {"id": "docs", "R": ["ghost"], "A": "ghost", "C": [], "I": [], "self_approval": "x"}]}
        found = selfcheck.d7_team_raci(base=base, team=team)
        self.assertEqual([f.item for f in found], ["team: api: kid in C and I"])
        self.assertIn("dev crew raci api --set", found[0].fix)
        self.assertEqual(selfcheck.d7_team_raci(base=base, team=base), [])

    # D9: each phase's proof and the commands its gate names are in the phase's devkit list
    def test_d9_phase_proofs_are_listed(self):
        self.assert_known("D9", selfcheck.d9_phase_proofs())

    # D10: every kind of work can be reached from files, or says it has none
    def test_d10_kinds_of_work_have_file_anchors(self):
        self.assert_known("D10", selfcheck.d10_file_anchors())

    # D10: a kind of work's own `files` globs reach it as FILE_HINTS do; only one with neither and no reason is drift
    def test_d10_an_activitys_own_files_reach_it(self):
        roster = {"members": [{"id": "kid"}], "activities": [
            {"id": "i18n", "R": ["kid"], "A": "kid", "files": ["**/locales/**"]},
            {"id": "plan", "R": ["kid"], "A": "kid", "no_files": "plans live in the handoff repository"},
            {"id": "lookup", "R": ["kid"], "A": "kid", "files": []}]}
        found = selfcheck.d10_file_anchors(roster=roster, hints=[])
        self.assertEqual([f.item for f in found], ["lookup: no file anchor"])

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
