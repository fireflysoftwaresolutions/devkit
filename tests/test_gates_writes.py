# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Which files the crew gates credit to a build member (SubagentStop): only those its edit calls or its shell commands
# wrote. A file another lane changed during the member's lane is not the member's because one of its commands named
# it (a read: cat, grep, sed -n, git diff), and a file another caller edited with its own edit tool meanwhile (the
# session's edit record, progress.note_edit) is that caller's unless a redirection of the member wrote into it.
# Found by the Senior engineer's gate replay: docs findings for members that only read a doc another lane changed.
# Built on the crew gate fixtures of tests/test_crew_gates.py. Tests carry comments, not docstrings: `dev test prove`
# leaves out tests with a docstring. Run: python3 -m unittest tests.test_gates_writes
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import gates  # noqa: E402
from devkit import progress as pg  # noqa: E402
from tests.test_crew_gates import KID_DOCS, GateCase  # noqa: E402

GUIDE = "# Guide\n\nThe writer's lane adds this page while the member works.\n"


class BuildMemberCredit(GateCase):
    def setUp(self):
        super().setUp()
        calls = mock.patch.object(pg, "CALLS", Path(self.tmp.name) / "calls")
        calls.start()
        self.addCleanup(calls.stop)

    def finished(self):
        # Kid changed code and ran every proof but the docs one: only docs it wrote owe `dev docs check`
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.complete()
        self.tx.bash("bin/dev test prove")
        return self

    def another_lane_writes_the_guide(self):
        p = self.repo / "docs" / "guide.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(GUIDE)
        return p

    def gates_of_lane(self):
        return gates.crew_gates(KID_DOCS, self.tx.save(), str(self.repo), session="s1", lane="a_kid")

    def test_a_doc_the_member_only_read_owes_it_nothing(self):
        self.finished()
        self.another_lane_writes_the_guide()
        self.tx.bash("grep -n 'adds this page' docs/guide.md").bash("sed -n 1,3p docs/guide.md")
        self.tx.bash("cd " + str(self.repo) + " && git diff --stat -- docs/guide.md | head -5")
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [])

    def test_a_doc_another_caller_edited_is_theirs_though_the_members_script_named_it(self):
        self.finished()
        guide = self.another_lane_writes_the_guide()
        pg.note_edit("claude", {"tool_name": "Write", "session_id": "s1", "agent_id": "a_ghost",
                                "tool_input": {"file_path": str(guide)}}, now=time.time())
        # a program of the member that may write the doc: with the edit record, the other caller's edit is theirs;
        # without it, the member may have written the doc and owes its proof and skill
        self.tx.bash("python3 - <<'EOF'\nopen('docs/guide.md', 'a').write('')\nEOF")
        self.assertEqual(self.rules(self.gates_of_lane()), [])
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [("proofs", "dev docs check"), ("skills", "docs")])

    def test_a_doc_the_members_program_only_read_owes_it_nothing(self):
        # the guard reads the program: it opens the doc to read and writes nothing, so the doc is not the member's
        self.finished()
        self.another_lane_writes_the_guide()
        self.tx.bash("python3 - <<'EOF'\nprint(open('docs/guide.md').read().count('lane'))\nEOF")
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [])

    def test_the_edit_record_keeps_a_lanes_edits_for_its_whole_life(self):
        t0 = time.time() - 3 * 3600  # an edit three hours ago, then another one now
        pg.note_edit("claude", {"tool_name": "Edit", "session_id": "s1", "agent_id": "a_ghost",
                                "tool_input": {"file_path": "/repo/docs/guide.md"}}, now=t0)
        pg.note_edit("claude", {"tool_name": "Edit", "session_id": "s1", "agent_id": "a_tank",
                                "tool_input": {"file_path": "/repo/api.py"}}, now=time.time())
        self.assertEqual([e["path"] for e in pg.edits_since("s1", t0 - 60)], ["/repo/docs/guide.md", "/repo/api.py"])


if __name__ == "__main__":
    unittest.main()
