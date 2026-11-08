# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The skills gate of the crew gates (SubagentStop): which skills a member owes, from the files it changed. Built on
# the crew gate fixtures of tests/test_crew_gates.py. Tests carry comments, not docstrings: `dev test prove` leaves
# out tests with a docstring. Run: python3 -m unittest tests.test_gates_skills
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import skillguard  # noqa: E402
from tests.test_crew_gates import KID, GateCase  # noqa: E402

UI_HAVE = {"test-driven-development", "testing", "docs", "communicating", "frontend-design", "impeccable"}


class ScratchFiles(GateCase):
    def setUp(self):
        super().setUp()
        skillguard.installed = lambda agent: UI_HAVE

    # a probe the member writes in its scratch folder, outside every repository, owes no skill
    def test_a_scratch_file_outside_the_repository_owes_no_skill(self):
        scratch = Path(self.tmp.name) / "scratchpad" / "kid" / "probe.py"
        scratch.parent.mkdir(parents=True)
        text = 'HTML = "<!doctype html><style>p{}</style>"\n'
        scratch.write_text(text)
        self.tx.call("Write", f"File created successfully at: {scratch}", file_path=str(scratch), content=text)
        self.complete()
        self.assertEqual(self.rules(self.gates(KID)), [])

    # the same code inside the repository is UI work: the member owes the UI skills
    def test_the_same_file_in_the_repository_owes_the_ui_skills(self):
        self.write("lib/page.py", 'HTML = "<!doctype html><style>p{}</style>"\n')
        self.complete()
        self.assertEqual(sorted(i for r, i in self.rules(self.gates(KID)) if r == "skills"),
                         ["frontend-design", "impeccable"])


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


if __name__ == "__main__":
    unittest.main()
