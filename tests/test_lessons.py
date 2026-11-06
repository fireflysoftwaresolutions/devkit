# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lessons: record, recall, recur, prevent, review, signals; the pipe-gate guard rule."""
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, guard, lessons, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
IDENT = ["-c", "user.email=t@example.com", "-c", "user.name=t"]


class CrewPitfallTests(unittest.TestCase):
    """Crew members get the known pitfalls for their task (they get no session start), and learn with devkit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL)
        lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL = root / "lessons", root, root / "usage.json"
        self.regex = lessons.add("acme/api", "A regex rewrite broke the parser", cause="sed -i on nested code",
                                 prevention="exact-string edits that must match once", a=A,
                                 activities=["feature"], files=["src/parser/**"], commands=["sed -i"])
        self.push = lessons.add("acme/api", "A third push in an hour was refused", severity="high",
                                cause="speculative CI fixes", prevention="reproduce locally, push once", a=A)
        self.other = lessons.add("global", "The status block came after text", cause="a note before the block",
                                 prevention="write the block first", a=A)

    def tearDown(self):
        lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL = self.saved
        self.tmp.cleanup()

    def test_a_step_gets_the_lessons_of_its_kind_of_work_and_files_and_its_projects_high_ones(self):
        picked = [x["meta"]["id"] for x in lessons.for_task("acme/api", activity="feature",
                                                             files=["src/parser/lexer.py"])]
        self.assertEqual(picked[0], self.regex["meta"]["id"])  # its kind of work and files
        self.assertIn(self.push["meta"]["id"], picked)  # the project's high-severity lesson, always
        self.assertNotIn(self.other["meta"]["id"], picked)  # a global lesson with no anchor in common
        lessons.prevent(lessons.find(self.regex["meta"]["id"]), by="test", ref="tests/test_parser.py",
                        why="a lint now refuses sed -i", a=A)
        self.assertNotIn(self.regex["meta"]["id"],
                         [x["meta"]["id"] for x in lessons.for_task("acme/api", activity="feature")])

    def test_a_claude_launch_brief_gets_the_pitfalls_and_how_to_record_a_new_one(self):
        from devkit import skillguard
        out = skillguard.on_agent_brief("claude", {"description": "Neo: arreglo del parser",
                                                   "prompt": "Arregla el parser tras la reescritura."}, "acme/api")
        self.assertIn("## devkit · known pitfalls", out["prompt"])
        self.assertIn(self.regex["meta"]["id"], out["prompt"])  # Neo does feature work: its lessons, any language
        self.assertIn("dev lesson recur", out["prompt"])

    def test_a_launch_gets_the_skills_of_its_kind_of_work(self):
        from devkit import skillguard
        with mock.patch.object(skillguard, "installed", lambda agent: {"testing", "test-driven-development",
                                                                       "property-based-testing"}):
            out = skillguard.on_agent_brief("claude", {"description": "Mouse: tests",
                                                       "prompt": "Los tests fallan a veces; arréglalo."})
        self.assertIn("- `testing`", out["prompt"])
        self.assertIn("what devkit knows about test strategy", out["prompt"])

    def test_every_member_with_a_shell_is_told_to_record_lessons(self):
        from devkit import crew
        r = crew.load(team=False)
        self.assertIn("dev lesson add", crew.body(crew.member("tank", r), r))
        self.assertIn("so Morpheus records it as a lesson", crew.body(crew.member("sparks", r), r))  # no shell


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
