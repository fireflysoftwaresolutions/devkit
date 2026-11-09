# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Sleep keeps what matters: archiving follows the policy table (no per-scope cap), the lessons the old cap archived
come back once, the model judges a merge pair once, and editing a lesson is not seeing it."""
import json
import time
import unittest
from unittest import mock

from test_lessons_base import A, DAY, LessonCase

from devkit import ai, lessons


class ArchivePolicyTests(LessonCase):
    def test_forty_open_lessons_a_day_old_in_one_scope_are_all_kept(self):
        made = [self.add(f"Lesson number {i}", files=[f"src/area{i}/**"]) for i in range(40)]
        acts = lessons.consolidate(now=time.time() + DAY)
        self.assertEqual([x for x in acts if x[0] == "forget"], [])
        self.assertEqual(self.active_ids(), sorted(self.ids(made)))

    def test_a_merge_keeper_survives_its_run(self):
        for i in range(28):  # stronger than the keeper will be: high severity, seen twice
            les = self.add(f"Strong lesson {i}", severity="high", files=[f"lib/part{i}/**"])
            lessons.recur(les, "seen again", a=A)
        where = {"commands": ["make deploy"], "errors": ["deploy key was rejected"], "files": ["deploy/**"]}
        x = self.add("Deploy key rejected on the first run", **where)
        y = self.add("Deploy key rejected again", new=True, why="a near-duplicate for the merge", **where)
        acts = lessons.consolidate()
        merges = [(i, d) for k, i, d in acts if k == "merge"]
        self.assertEqual(len(merges), 1)
        dropped = merges[0][0]
        keeper = ({x["meta"]["id"], y["meta"]["id"]} - {dropped}).pop()
        self.assertIn(keeper, self.active_ids())
        self.assertEqual(self.archived_ids(), [dropped])
        self.assertEqual([x for x in acts if x[0] == "forget"], [])

    def test_an_open_lesson_idle_200_days_with_three_occurrences_is_kept(self):
        les = self.add("Migration ran twice", files=["db/**"])
        les = lessons.recur(lessons.recur(les, "again", a=A), "and again", a=A)
        acts = lessons.consolidate(now=time.time() + 200 * DAY)
        self.assertNotIn(les["meta"]["id"], [i for k, i, _ in acts if k == "forget"])
        self.assertEqual(self.active_ids(), [les["meta"]["id"]])

    def test_an_edit_does_not_make_a_lesson_look_recently_seen(self):
        les = self.backdate(self.add("Old mistake", files=["old/**"]), 200)
        lessons.set_anchors(les, "backfill", a=A, files=["old/more/**"])  # an edit today, not a sighting
        acts = lessons.consolidate()
        self.assertIn(("forget", les["meta"]["id"]), [(k, i) for k, i, _ in acts])


class CapRestoreTests(LessonCase):
    def cap_archive(self, les):
        return lessons.archive(les, f"over the active cap for {les['meta']['scope']}", A)

    def test_the_first_sleep_restores_what_the_old_cap_archived_once(self):
        open_ = self.cap_archive(self.add("Open lesson the cap took", files=["a/**"]))
        prev = self.add("Prevented lesson the cap took", files=["b/**"])
        prev = self.cap_archive(lessons.prevent(prev, "test", "tests/test_b.py", "a test covers it", a=A))
        merged = lessons.archive(self.add("Merged away", files=["c/**"]), "merged into L-other", A)
        acts = lessons.consolidate()
        self.assertEqual(sorted(i for k, i, _ in acts if k == "restore"),
                         sorted([open_["meta"]["id"], prev["meta"]["id"]]))
        self.assertEqual(self.active_ids(), sorted([open_["meta"]["id"], prev["meta"]["id"]]))
        self.assertEqual(self.archived_ids(), [merged["meta"]["id"]])
        back = {x["meta"]["id"]: x for x in lessons.all_lessons()}
        self.assertEqual(back[open_["meta"]["id"]]["meta"]["status"], "open")
        self.assertEqual(back[prev["meta"]["id"]]["meta"]["status"], "prevented")  # its status before the cap
        self.assertIn("restored", back[prev["meta"]["id"]]["changelog"][0])
        self.assertTrue((lessons.STATE / "lessons-cap-restore.json").exists())
        again = lessons.consolidate()
        self.assertEqual([x for x in again if x[0] == "restore"], [])

    def test_a_dry_run_lists_the_restores_and_moves_nothing(self):
        les = self.cap_archive(self.add("Open lesson the cap took", files=["a/**"]))
        acts = lessons.consolidate(dry_run=True)
        self.assertEqual([(k, i) for k, i, _ in acts if k == "restore"], [("restore", les["meta"]["id"])])
        self.assertEqual(self.archived_ids(), [les["meta"]["id"]])
        self.assertFalse((lessons.STATE / "lessons-cap-restore.json").exists())


class MergeVerdictTests(LessonCase):
    def test_the_model_judges_a_pair_once_and_only_when_it_shares_an_error_or_command(self):
        x = self.add("Deploy key rejected", commands=["make deploy"], files=["deploy/a/**"], activities=["release"])
        y = self.add("Deploy failed on staging", commands=["make deploy"], files=["deploy/b/**"], stack=["python"],
                     new=True, why="a different failure of the same command")
        self.add("Web build broke", files=["web/**"], stack=["angular"])
        self.add("Web styles broke", files=["web/**"], activities=["frontend"])
        asked = []

        def judge(a, b):
            asked.append((a, b))
            return False
        with mock.patch.object(ai, "same_mistake", judge):
            lessons.consolidate()
            lessons.consolidate()
        self.assertEqual(len(asked), 1)
        self.assertEqual(sorted(t.splitlines()[0] for t in asked[0]),
                         sorted([x["meta"]["title"], y["meta"]["title"]]))
        memo = json.loads((lessons.STATE / "lessons-merge-verdicts.json").read_text())
        self.assertEqual([v["same"] for v in memo.values()], [False])


if __name__ == "__main__":
    unittest.main()
