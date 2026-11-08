# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The skills gate of the crew gates (SubagentStop): which skills a member owes, from the files it changed. Built on
# the crew gate fixtures of tests/test_crew_gates.py. Tests carry comments, not docstrings: `dev test prove` leaves
# out tests with a docstring. Run: python3 -m unittest tests.test_gates_skills
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import gates, skillguard  # noqa: E402
from tests.test_crew_gates import GOOD_TEST, KID, KID_DOCS, GateCase  # noqa: E402

UI_HAVE = {"test-driven-development", "testing", "docs", "communicating", "frontend-design", "impeccable"}


class ScratchFiles(GateCase):
    def setUp(self):
        super().setUp()
        skillguard.installed = lambda agent: UI_HAVE

    # a probe the member writes in its scratch folder, outside every repository, owes no skill; the same code in
    # the repository is UI work and owes the UI skills
    def test_a_scratch_file_owes_no_skill_and_the_same_file_in_the_repository_does(self):
        scratch = Path(self.tmp.name) / "scratchpad" / "kid" / "probe.py"
        scratch.parent.mkdir(parents=True)
        text = 'HTML = "<!doctype html><style>p{}</style>"\n'
        scratch.write_text(text)
        self.tx.call("Write", f"File created successfully at: {scratch}", file_path=str(scratch), content=text)
        self.complete()
        self.assertEqual(self.rules(self.gates(KID)), [])
        self.write("lib/page.py", text)
        self.assertEqual(sorted(i for r, i in self.rules(self.gates(KID)) if r == "skills"),
                         ["frontend-design", "impeccable"])


class ReviewMembers(GateCase):
    # a reviewer reads a doc another lane changes during its lane: the reviewer never edits, so the change owes it
    # no skill
    def test_a_reviewer_owes_no_skill_for_a_file_it_only_read(self):
        reviewer = {"id": "seraph", "name": "Seraph", "title": "Code reviewer", "tools": "review", "procedure": []}
        self.tx.bash("git diff -- README.md")
        (self.repo / "README.md").write_text("# repo\n\nThe writer's lane adds a line while the review runs.\n")
        self.assertEqual([i for r, i in self.rules(self.gates(reviewer)) if r == "skills"], [])


class RealRoster(GateCase):
    # the gates on config/crew.json as it ships, with every skill it names installed
    def setUp(self):
        super().setUp()
        skillguard.installed = lambda agent: {"devsecops", "semgrep", "github-actions-hardening", "communicating",
                                              "testing", "threat-model-analyst", "sharp-edges"}

    def skills_owed(self, member):
        return sorted(i for r, i in self.rules(self.gates(member)) if r == "skills")

    # the Security engineer's scan owes devsecops and its proof, not a developer-approved Semgrep run or workflow
    # hardening with no workflow in scope
    def test_the_security_engineers_scan_owes_devsecops(self):
        self.tx.skill("devkit:communicating").bash("dev sec scan")
        self.assertEqual(self.skills_owed("keymaker"), ["devsecops"])

    # recording the step at hand-off owes no skill: the brief already carries the report format and SubagentStop
    # checks the status block
    def test_a_hand_off_owes_no_communicating_skill(self):
        self.tx.bash('dev progress step "done" --check "12 tests pass"')
        self.assertEqual({m: self.skills_owed(m) for m in ("neo", "seraph", "sati", "hamann")},
                         {"neo": [], "seraph": [], "sati": [], "hamann": []})


class GatedSkills(unittest.TestCase):
    # what the brief tells a member the gate checks is what the gate checks: each phase's skills, and whether every
    # stop owes them (a proof that is not about files) or only work in that phase does
    def test_each_phase_names_its_skills_and_whether_every_stop_owes_them(self):
        got = [(g["phase"], g["skill"], g["always"]) for g in gates.gated_skills(KID_DOCS)]
        self.assertEqual(got, [("Build", "superpowers:test-driven-development", False), ("Document", "docs", False),
                               ("Verify", "testing", False), ("Hand off", "communicating", True)])

    # its scan proof is owed at every stop, so devsecops is the one skill every stop owes
    def test_the_security_engineer_owes_devsecops_at_every_stop(self):
        always = {g["skill"] for g in gates.gated_skills("keymaker") if g["always"]}
        self.assertEqual(always, {"devsecops"})


class CodexMembers(GateCase):
    # a Codex member's transcript (Codex rollout records): what it ran, read and patched
    def codex(self, *calls):
        recs = [{"timestamp": "2026-11-08T09:00:00.000Z", "type": "session_meta", "payload": {"id": "c1"}}]
        for kind, value in calls:
            if kind == "shell":
                pl = {"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": value})}
            else:
                pl = {"type": "custom_tool_call", "name": "apply_patch",
                      "input": f"*** Begin Patch\n*** Add File: {value}\n+x\n*** End Patch"}
            recs.append({"timestamp": "2026-11-08T09:00:01.000Z", "type": "response_item", "payload": pl})
        self.tx.path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
        return str(self.tx.path)

    # judged by Codex's own skills: one it read through its shell counts, one only Claude has is not owed
    def test_a_codex_member_is_judged_by_codex_skills(self):
        skillguard.installed = lambda agent: {"testing"} if agent == "codex" else \
            {"test-driven-development", "testing", "communicating"}
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_calc.py").write_text(GOOD_TEST)
        path = self.codex(("shell", "cat ~/.agents/skills/testing/SKILL.md"), ("patch", "tests/test_calc.py"),
                          ("shell", "dev test"), ("shell", 'dev progress step "built it" --check "12 tests pass"'))
        self.assertEqual(self.rules(gates.crew_gates(KID, path, str(self.repo))), [])


if __name__ == "__main__":
    unittest.main()
