# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson records survive concurrent writers and odd text: usage counts and recurrences are written under a lock with
an atomic replace, a half-written usage file is never written over, a fenced `## Changelog` stays in its section, a
title in brackets stays text, and dates are local like the rest of the handoff."""
import calendar
import threading
import unittest
from unittest import mock

from test_lessons_base import A, LessonCase, tz

from devkit import lessons


class ConcurrentWriteTests(LessonCase):
    def test_two_concurrent_tracks_both_count(self):
        start = threading.Barrier(2)

        def count():
            start.wait()
            for _ in range(100):
                lessons.track(["L-one"], "shown", "call")
        workers = [threading.Thread(target=count) for _ in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        got = lessons.usage()["L-one"]
        self.assertEqual((got["shown"], got["shown_call"]), (200, 200))

    def test_two_agents_recurring_the_same_lesson_both_count(self):
        les = self.add("Migration ran twice", files=["db/**"])
        mine, theirs = lessons.find(les["meta"]["id"]), lessons.find(les["meta"]["id"])  # both read it first
        lessons.recur(mine, "CI run 7", a=A)
        lessons.recur(theirs, "CI run 8", a=A)
        again = lessons.find(les["meta"]["id"])
        self.assertEqual(again["meta"]["occurrences"], 3)
        self.assertIn("CI run 7", again["sections"]["Evidence"])
        self.assertIn("CI run 8", again["sections"]["Evidence"])

    def test_a_half_written_usage_file_is_never_written_over(self):
        lessons.USAGE_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        cut = '{"L-old": {"shown": 41, "recur": 3'
        lessons.USAGE_LOCAL.write_text(cut)
        lessons.track(["L-new"], "shown")
        self.assertEqual(lessons.USAGE_LOCAL.read_text(), cut)


class RecordTextTests(LessonCase):
    def test_a_fenced_changelog_heading_inside_prevention_round_trips(self):
        prevention = ("Write the release notes from this template:\n\n```markdown\n## Changelog\n\n- v1 · fixed it\n"
                      "## Notes\n```\n\nThen run dev release.")
        les = self.add("Release notes lost their history", files=["CHANGELOG.md"], prevention=prevention)
        les = lessons.recur(lessons.find(les["meta"]["id"]), "release 3", a=A)
        again = lessons.load(les["path"])
        self.assertEqual(again["sections"]["Prevention"], prevention)
        self.assertEqual(len(again["changelog"]), 2)
        self.assertIn("happened again (#2)", again["changelog"][0])

    def test_an_unfenced_changelog_heading_inside_prevention_round_trips(self):
        # a `## Changelog` line outside fenced code started a section of its own and cut the Prevention short
        prevention = "Write the release notes with\n## Changelog\nat the end of the file"
        les = self.add("Release notes lost their history", files=["CHANGELOG.md"], prevention=prevention)
        les = lessons.recur(lessons.find(les["meta"]["id"]), "release 3", a=A)
        again = lessons.load(les["path"])
        self.assertEqual(again["sections"]["Prevention"], prevention)
        self.assertEqual(len(again["changelog"]), 2)

    def test_a_section_name_inside_what_happened_keeps_the_lesson_recurrable(self):
        # a `## Fix` line inside What happened was read as the Fix section, which the real one then emptied: every
        # later write refused to drop that text, so the lesson could never be recurred again
        happened = "the first attempt\n## Fix\nthe second attempt"
        les = self.add("Retry only idempotent calls", files=["billing/**"], happened=happened, fix="add a key")
        les = lessons.recur(lessons.find(les["meta"]["id"]), "CI run 9", a=A)
        again = lessons.load(les["path"])
        self.assertEqual((again["sections"]["What happened"], again["sections"]["Fix"]), (happened, "add a key"))
        self.assertEqual(again["meta"]["occurrences"], 2)

    def test_a_lesson_quoted_inside_what_happened_stays_in_what_happened(self):
        # text that quotes a whole lesson, its section names in order, belongs to the section it was written in
        happened = "the agent wrote a lesson by hand:\n## What happened\nit broke\n## Root cause\nno test"
        les = self.add("Record lessons with dev lesson add", files=["lessons/**"], happened=happened)
        les = lessons.recur(lessons.find(les["meta"]["id"]), "again", a=A)
        again = lessons.load(les["path"])
        self.assertEqual(again["sections"]["What happened"], happened)
        self.assertEqual(again["sections"]["Root cause"], "the cause of Record lessons with dev lesson add")

    def test_a_title_in_brackets_stays_text(self):
        les = self.add("Retries doubled the charge", files=["billing/**"])
        text = les["path"].read_text().replace("title: Retries doubled the charge",
                                               "title: [API] retries, then [billing]")
        self.assertEqual(lessons.parse(text)["meta"]["title"], "[API] retries, then [billing]")

    def test_dates_are_local(self):
        tz(self, "America/Los_Angeles")
        evening = calendar.timegm((2026, 11, 9, 5, 0, 0))  # 2026-11-08 21:00 in Los Angeles
        with mock.patch("time.time", return_value=evening):
            les = self.add("Tagged before the changelog", files=["CHANGELOG.md"])
            les = lessons.recur(les, "release 4", a=A)
        self.assertEqual((les["meta"]["first_seen"], les["meta"]["last_seen"]), ("2026-11-08", "2026-11-08"))
        self.assertIn("- 2026-11-08: release 4", les["sections"]["Evidence"])


if __name__ == "__main__":
    unittest.main()
