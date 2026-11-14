# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# dev doctor's handoff note row: a project's note longer than the handoff skill allows ("under ~150 lines") is a
# warning with its size and the fix; a note within it adds no row. Tests carry comments, not docstrings: `dev test
# prove` leaves out tests with a docstring. Run: python3 -m unittest tests.test_selfcheck_handoff
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import core, selfcheck  # noqa: E402
import test_work  # noqa: E402


class HandoffNoteSize(unittest.TestCase):
    def setUp(self):
        self.t = int(time.time())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.top = Path(self.tmp.name) / "api"
        self.top.mkdir()
        subprocess.run(["git", "-C", str(self.top), "init", "-q", "-b", "main"], check=True, capture_output=True)
        self.h = test_work.TempHandoff().__enter__()
        self.addCleanup(self.h.__exit__)
        self._notes = core.NOTES
        core.NOTES = self.h.root / "projects"
        self.addCleanup(setattr, core, "NOTES", self._notes)

    def note(self, lines: int):
        p = core.note_path("acme/api")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"line {i}\n" for i in range(lines)))

    def rows(self):
        return {name: (ok, detail, fix) for _, name, ok, detail, fix in
                selfcheck.work_state_checks(self.top, "acme/api", runs=[], now=self.t)}

    # a note past the handoff skill's size is a warning naming its length, the limit and the fix; a note within it
    # adds no row, and the work state row stays the only one
    def test_a_long_handoff_note_is_a_doctor_warning_and_a_short_one_is_not(self):
        limit = selfcheck.handoff_max_lines()
        self.note(limit + 50)
        ok, detail, fix = self.rows()["handoff note"]
        self.assertFalse(ok)
        self.assertIn(f"{limit + 50} lines", detail)
        self.assertIn(f"~{limit}", detail)
        self.assertIn("handoff skill", fix)
        self.note(20)
        self.assertEqual(list(self.rows()), ["work state"])

    # the limit is the one the handoff skill states
    def test_the_limit_is_the_handoff_skills(self):
        with tempfile.TemporaryDirectory() as d:
            skill = Path(d) / "plugin" / "skills" / "handoff" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("- Keep the whole note under ~40 lines; move long material into `plans/`.\n")
            self.assertEqual(selfcheck.handoff_max_lines(home=d), 40)

    def test_generated_blocks_do_not_use_the_authored_note_budget(self):
        limit = selfcheck.handoff_max_lines()
        generated = "generated task\n" * (limit + 1)
        for kind in ("auto", "tasks"):
            start, end = f"<!-- dev:{kind}:start -->", f"<!-- dev:{kind}:end -->"
            with self.subTest(kind=kind, complete=True):
                self.note(limit)
                note = core.note_path("acme/api")
                note.write_text(note.read_text() + f"{start}\n{generated}{end}\n")
                self.assertEqual(list(self.rows()), ["work state"])
            # A malformed block, a quoted example or an unrelated comment cannot
            # hide authored material from the size warning.
            for block in (f"{start}\n{generated}", f"{generated}{end}\n",
                          f"{start}\n<!-- dev:other:end -->\n{generated}",
                          f"{start}\n<!-- dev:tasks:start -->\n{generated}<!-- dev:tasks:end -->\n{end}\n",
                          f"prefix {start}\n{generated}{end}\n",
                          f"```markdown\n{start}\n{generated}{end}\n```\n",
                          f"<!-- user:start -->\n{generated}<!-- user:end -->\n"):
                with self.subTest(kind=kind, block=block.splitlines()[0]):
                    self.note(1)
                    note.write_text(note.read_text() + block)
                    ok, detail, _ = self.rows()["handoff note"]
                    self.assertFalse(ok)
                    self.assertIn(f"{len(note.read_text().splitlines())} lines", detail)

    def test_long_authored_notes_report_their_own_count(self):
        limit = selfcheck.handoff_max_lines()
        self.note(limit + 1)
        note = core.note_path("acme/api")
        note.write_text("<!-- dev:auto:start -->\nmetadata\n<!-- dev:auto:end -->\n" + note.read_text() +
                        "<!-- dev:tasks:start -->\n" + "task\n" * limit + "<!-- dev:tasks:end -->\n")
        ok, detail, _ = self.rows()["handoff note"]
        self.assertFalse(ok)
        self.assertIn(f"{limit + 1} lines of authored content", detail)


if __name__ == "__main__":
    unittest.main()
