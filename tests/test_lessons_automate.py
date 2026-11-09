# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""From lesson to check: every third occurrence opens one work item to automate the prevention (a bug when the lesson
is about devkit), never a second while the first is open; review flags a prevention that failed within a week and
lists first the lessons agents missed after seeing them."""
import time
import unittest

from test_lessons_base import A, DAY, LessonCase

from devkit import lessons, work


def recur(les, times):
    for i in range(times):
        les = lessons.recur(les, f"seen again {i}", a=A)
    return les


class WorkItemTests(LessonCase):
    def items(self, scope="acme/api"):
        return work.all_items(scope)

    def test_the_third_recurrence_opens_exactly_one_work_item(self):
        les = recur(self.add("Never edit an applied migration", files=["db/**"]), 1)
        self.assertEqual(self.items(), [])
        les = recur(les, 1)  # the third occurrence
        items = self.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["meta"]["type"], "task")
        self.assertIn(les["meta"]["id"], items[0]["meta"]["title"])
        self.assertEqual(lessons.find(les["meta"]["id"])["meta"]["task_ref"], items[0]["meta"]["id"])

    def test_no_second_item_while_the_first_is_open(self):
        les = recur(self.add("Never edit an applied migration", files=["db/**"]), 5)  # the sixth occurrence
        self.assertEqual(len(self.items()), 1)
        first = self.items()[0]
        work.update(first, "done", "the migration lint landed", A, status="done")
        les = recur(les, 3)  # the ninth
        self.assertEqual(len(self.items()), 2)
        self.assertNotEqual(lessons.find(les["meta"]["id"])["meta"]["task_ref"], first["meta"]["id"])

    def test_a_lesson_about_devkit_opens_a_bug(self):
        recur(self.add("Run dev progress step on its own", commands=["dev progress step"]), 2)
        self.assertEqual([x["meta"]["type"] for x in self.items()], ["bug"])


class ReviewTests(LessonCase):
    def test_a_recurrence_within_a_week_of_its_prevention_flags_it(self):
        quick = lessons.prevent(self.add("Never edit an applied migration", files=["db/**"]), "test",
                                "tests/test_db.py", "the test refuses edits", a=A)
        quick = lessons.recur(quick, "it happened anyway", a=A)
        held = lessons.prevent(self.add("Tag only from the release branch", files=["CHANGELOG.md"]), "guard",
                               "tag-rule", "the guard refuses other tags", a=A)
        text = held["path"].read_text()
        stamp = held["changelog"][0].split(" · ")[1]
        held["path"].write_text(text.replace(stamp, work.now_iso(time.time() - 10 * DAY)))  # prevented 10 days ago
        lessons.recur(lessons.find(held["meta"]["id"]), "it happened again", a=A)
        failed = lessons.review()["prevention_failed"]
        self.assertEqual([x["lesson"]["meta"]["id"] for x in failed], [quick["meta"]["id"]])
        self.assertEqual((failed[0]["by"], failed[0]["ref"]), ("test", "tests/test_db.py"))

    def test_lessons_missed_twice_after_being_shown_come_first(self):
        once = self.add("Never edit an applied migration", files=["db/**"])
        twice = self.add("Tag only from the release branch", files=["CHANGELOG.md"])
        thrice = self.add("Run the integration tests with Docker", files=["it/**"])
        lessons.track([once["meta"]["id"]], "missed")
        lessons.track([twice["meta"]["id"]] * 2, "missed")
        lessons.track([thrice["meta"]["id"]] * 3, "missed")
        self.assertEqual(self.ids(lessons.review()["automate"]), [thrice["meta"]["id"], twice["meta"]["id"]])


if __name__ == "__main__":
    unittest.main()
