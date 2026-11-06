# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Lessons: record, recall, recur, prevent, review, signals; the pipe-gate guard rule."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, guard, lessons, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
IDENT = ["-c", "user.email=t@example.com", "-c", "user.name=t"]


class LessonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (lessons.LESSONS, work.HANDOFF)
        lessons.LESSONS, work.HANDOFF = root / "lessons", root

    def tearDown(self):
        lessons.LESSONS, work.HANDOFF = self.saved
        self.tmp.cleanup()

    def test_record_needs_cause_and_prevention(self):
        with self.assertRaises(ValueError):
            lessons.add("acme/api", "x", cause="", prevention="p", a=A)
        les = lessons.add("acme/api", "Tag before CHANGELOG", kind="process", cause="checklist in a wiki",
                          prevention="release only with dev release", evidence=["PR #212"], a=A)
        again = lessons.load(les["path"])
        self.assertEqual((again["meta"]["status"], again["meta"]["occurrences"]), ("open", 1))
        self.assertIn("- PR #212", again["sections"]["Evidence"])
        self.assertEqual(again["sections"]["Prevention"], "release only with dev release")

    def test_recur_prevent_retire_and_review(self):
        les = lessons.add("acme/api", "Flaky clock test", kind="bug", cause="real clock", prevention="fixed clock",
                          a=A)
        les = lessons.recur(les, "CI run 77", a=A)
        self.assertEqual(les["meta"]["occurrences"], 2)
        self.assertEqual([x["meta"]["id"] for x in lessons.review()["recurring"]], [les["meta"]["id"]])
        les = lessons.prevent(les, "test", "tests/test_clock.py::test_fixed", "the test pins the clock", a=A)
        self.assertEqual((les["meta"]["status"], les["meta"]["prevention"]), ("prevented", "test"))
        self.assertEqual(lessons.review()["recurring"], [])
        with self.assertRaises(ValueError):
            lessons.retire(les, "", a=A)
        les = lessons.retire(les, "clock module removed", a=A)
        self.assertEqual(les["meta"]["status"], "retired")
        self.assertEqual(len(les["changelog"]), 4)

    def test_recall_ranks_open_severe_repeated_and_includes_global(self):
        low = lessons.add("acme/api", "Typo in docs", severity="low", cause="c", prevention="p", a=A)
        high = lessons.add("acme/api", "Data loss on migration", severity="high", cause="c", prevention="p", a=A)
        glob = lessons.add("global", "Commit after a pipe", severity="high", cause="c", prevention="pipefail", a=A)
        lessons.recur(glob, "again", a=A)
        lessons.add("acme/other", "Not ours", severity="high", cause="c", prevention="p", a=A)
        ids = [x["meta"]["id"] for x in lessons.relevant("acme/api")]
        self.assertEqual(ids[0], glob["meta"]["id"])
        self.assertLess(ids.index(high["meta"]["id"]), ids.index(low["meta"]["id"]))
        self.assertNotIn("Not ours", " ".join(lessons.context_lines("acme/api")))
        self.assertTrue(any("pipefail" in ln for ln in lessons.context_lines("acme/api")))

    def test_signals_and_session_fixes(self):
        d = tempfile.mkdtemp()
        core.sh(["git", "init", "-q", d])
        core.sh(["git", *IDENT, "-C", d, "commit", "-q", "--allow-empty", "-m", "feat: start"])
        start = core.gitout(d, "rev-parse", "HEAD")
        core.sh(["git", *IDENT, "-C", d, "commit", "-q", "--allow-empty", "-m", "fix(api): null invoice total"])
        core.sh(["git", *IDENT, "-C", d, "commit", "-q", "--allow-empty", "-m", 'Revert "feat: start"'])
        kinds = [k for k, _, _ in lessons.signals(None, d, 14)]
        self.assertIn("fix", kinds)
        self.assertIn("revert", kinds)
        self.assertEqual(len(lessons.fixed_in_session(d, start)), 2)


class PipeGateTests(unittest.TestCase):
    def test_commit_after_a_pipe_is_refused(self):
        cwd = str(core.DEV)
        deny = guard.check("Bash", {"command": "python3 -m unittest 2>&1 | tail -1 && git commit -m x"}, cwd, "claude")
        self.assertEqual(deny[0], "deny")
        for ok in ("set -o pipefail; npm test | tee log && git push", "npm test && git push",
                   'out=$(npm test | tail -1); [ "$out" = OK ] && git commit -m x', "git log | head -3"):
            with self.subTest(cmd=ok):
                v = guard.check("Bash", {"command": ok}, cwd, "claude")
                self.assertTrue(v is None or v[0] != "deny" or "pipeline" not in v[1])


if __name__ == "__main__":
    unittest.main()
