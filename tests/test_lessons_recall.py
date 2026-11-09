# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Recall measures nothing itself and reaches the agent early: recall functions count no use (the caller tracks what it
showed), strength rewards a lesson that keeps mattering instead of punishing one that is shown often, a brief with no
anchored lesson gets the project's recurring ones, the session start puts the branch's files first and rotates, only
lessons with a root cause and an anchor are recalled, and each line says who recorded it."""
import re
import time
import unittest

from test_lessons_base import A, DAY, LessonCase

from devkit import lessons


class MeasureTests(LessonCase):
    def test_recall_counts_nothing(self):
        self.add("Never edit an applied migration", files=["db/**"], commands=["flyway repair"],
                 errors=["checksum mismatch for migration"], activities=["database"])
        lessons.for_task("acme/api", activity="database", files=["db/V3.sql"])
        lessons.for_call("acme/api", files=["db/V3.sql"], command="flyway repair")
        lessons.for_output("acme/api", "ERROR: checksum mismatch for migration 3")
        lessons.relevant("acme/api")
        self.assertEqual(lessons.usage(), {})

    def test_heeded_and_missed_keep_a_lesson_strong(self):
        les = self.backdate(self.add("Never edit an applied migration", files=["db/**"]), 100)
        lid, now = les["meta"]["id"], time.time()
        idle = lessons.strength(les, {}, now)
        self.assertGreater(lessons.strength(les, {lid: {"heeded": 1, "heeded_last": now - DAY}}, now), 4 * idle)
        self.assertGreater(lessons.strength(les, {lid: {"missed": 1, "missed_last": now - DAY}}, now), 4 * idle)


class BriefTests(LessonCase):
    def test_a_brief_with_no_anchored_lesson_gets_the_projects_recurring_ones(self):
        recurring = []
        for i in range(4):
            les = self.add(f"Recurring mistake {i}", files=[f"lib/part{i}/**"])
            recurring.append(lessons.recur(les, "again", a=A)["meta"]["id"])
        self.add("Seen once", files=["lib/once/**"])
        picked = self.ids(lessons.for_task("acme/api", activity="architecture", files=["docs/adr/0001.md"]))
        self.assertEqual(len(picked), 3)
        self.assertTrue(set(picked) <= set(recurring))


class SessionStartTests(LessonCase):
    def test_the_lessons_of_the_branchs_files_come_first(self):
        for i in range(3):
            strong = self.add(f"Strong lesson {i}", severity="high", files=[f"lib/part{i}/**"])
            lessons.recur(strong, "again", a=A)
        weak = self.add("Invoices round half-even", severity="low", files=["src/billing/**"])
        top = lessons.relevant("acme/api", limit=1, files=["src/billing/invoice.py"])
        self.assertEqual(self.ids(top), [weak["meta"]["id"]])

    def test_ten_session_starts_show_more_than_three_lessons(self):
        for i in range(9):
            les = self.add(f"Lesson number {i}", severity="high" if i < 3 else "medium", files=[f"src/area{i}/**"])
            for _ in range(5 if i < 3 else 0):  # three strong lessons that would win every start
                les = lessons.recur(les, "again", a=A)
        shown = set()
        for _ in range(10):
            for line in lessons.context_lines("acme/api", limit=3):
                shown.update(re.findall(r"L-[0-9a-z]+", line))
        self.assertGreater(len(shown), 3)

    def test_only_lessons_with_a_root_cause_and_an_anchor_are_recalled(self):
        good = self.add("Never edit an applied migration", files=["db/**"])
        self.add("A lesson with nowhere to apply")
        causeless = self.add("A lesson with no cause on record", files=["web/**"])
        text = causeless["path"].read_text().replace("the cause of A lesson with no cause on record", "_—_")
        causeless["path"].write_text(text)
        self.assertEqual(self.ids(lessons.relevant("acme/api")), [good["meta"]["id"]])
        self.assertEqual(self.ids(lessons.for_call("acme/api", files=["web/app.ts"])), [])


class RecallLineTests(LessonCase):
    def test_each_line_says_who_recorded_it_and_when_under_the_advice_line(self):
        les = self.add("Never edit an applied migration", files=["db/**"], author="Senior engineer (Neo)",
                       prevention="add a new migration instead")
        lid, day = les["meta"]["id"], les["meta"]["first_seen"]
        lines = lessons.pitfall_lines([les])
        self.assertEqual(lines[0], lessons.ADVICE)
        self.assertEqual(lines[1], f"- Lesson {lid} (recorded by Senior engineer (Neo), {day}): Never edit an applied "
                                   f"migration — prevention: add a new migration instead")
        self.assertEqual(lessons.context_lines("acme/api"), [lines[1][2:]])


if __name__ == "__main__":
    unittest.main()
