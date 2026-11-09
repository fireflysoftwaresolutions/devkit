# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Capture right, once: a mistake already on record is recurred, not recorded again; archived lessons are found and
come back when they recur; one id is one file; the anchor gate, the capture shape and the screen run on every add;
a lesson records who wrote it; a superseded lesson leaves recall."""
import unittest
from unittest import mock

from test_lessons_base import A, LessonCase

from devkit import lessons

HUMAN = {"user": "ana", "host": "mbp1", "agent": "human", "session": ""}


class DuplicateTests(LessonCase):
    def test_a_lesson_sharing_an_error_anchor_is_refused_with_the_recur_command(self):
        old = self.add("Never edit an applied migration", errors=["checksum mismatch for migration"],
                       files=["db/**"], activities=["database"])
        lid = old["meta"]["id"]
        with self.assertRaises(ValueError) as e:
            self.add("Edited an applied migration again", errors=["Checksum mismatch for migration"],
                     files=["db/**"])
        self.assertIn(f"looks like {lid}", str(e.exception))
        self.assertIn(f"dev lesson recur {lid} --evidence", str(e.exception))
        self.assertIn("--new --why", str(e.exception))
        self.assertEqual(self.active_ids(), [lid])
        other = self.add("Index the new foreign keys", files=["db/**"], activities=["database"])  # same files only
        self.assertEqual(self.active_ids(), sorted([lid, other["meta"]["id"]]))

    def test_new_records_it_anyway_with_a_reason(self):
        old = self.add("Never edit an applied migration", errors=["checksum mismatch for migration"])
        with self.assertRaises(ValueError):
            self.add("Repair the history only after a fix", errors=["checksum mismatch for migration"], new=True)
        les = self.add("Repair the history only after a fix", errors=["checksum mismatch for migration"], new=True,
                       why="a different mistake: repairing instead of fixing")
        self.assertEqual(len(self.active_ids()), 2)
        self.assertIn(old["meta"]["id"], les["changelog"][0])

    def test_an_archived_lesson_sharing_a_command_anchor_is_found(self):
        old = lessons.archive(self.add("Never force push a shared branch", commands=["git push --force"]),
                              "merged into L-other", A)
        self.add("Force pushed main again", commands=["git push --force"], scope="acme/web")  # another project's
        with self.assertRaises(ValueError) as e:
            self.add("Force pushed main again", commands=["git push --force"])
        self.assertIn(f"looks like {old['meta']['id']}", str(e.exception))


class ArchiveTests(LessonCase):
    def test_find_searches_the_archive(self):
        les = lessons.archive(self.add("Never echo equals signs in zsh", commands=["echo ======"]),
                              "merged into L-other", A)
        self.assertEqual(lessons.find(les["meta"]["id"])["path"], les["path"])
        self.assertEqual(lessons.find("equals signs in zsh")["meta"]["id"], les["meta"]["id"])

    def test_recur_on_an_archived_lesson_restores_it_and_counts_the_repeat(self):
        les = lessons.archive(self.add("Never echo equals signs in zsh", commands=["echo ======"]),
                              "over the active cap for acme/api", A)
        back = lessons.recur(lessons.find(les["meta"]["id"]), "zsh said ===== not found", a=A)
        self.assertEqual((back["meta"]["occurrences"], back["meta"]["status"]), (2, "open"))
        self.assertEqual(self.active_ids(), [les["meta"]["id"]])
        self.assertEqual(self.archived_ids(), [])
        self.assertIn("restored from the archive", back["changelog"][0])

    def test_a_lesson_both_archived_and_active_resolves_to_one_file(self):
        les = self.add("Never echo equals signs in zsh", commands=["echo ======"])
        stale = lessons.LESSONS / "archive" / "acme/api" / les["path"].name  # a restore that raced an archive
        stale.parent.mkdir(parents=True)
        stale.write_text(les["path"].read_text())
        les = lessons.recur(lessons.find(les["meta"]["id"]), "again", a=A)  # the active copy is newer: v2
        newer = les["path"].read_text().replace("occurrences: 2", "occurrences: 5").replace("version: 2", "version: 9")
        stale.write_text(newer)  # this time the archived copy saw more
        back = lessons.recur(lessons.find(les["meta"]["id"]), "and again", a=A)
        self.assertEqual(back["meta"]["occurrences"], 6)
        self.assertEqual(self.archived_ids(), [])
        self.assertEqual(self.active_ids(), [les["meta"]["id"]])


class CheckTests(LessonCase):
    def test_add_refuses_what_the_anchor_gate_refuses(self):
        with mock.patch.object(self.lint, "check_anchors", return_value=["the command anchor `python3` is bare"]):
            with self.assertRaises(ValueError) as e:
                self.add("Run scripts from the repository root", commands=["python3"])
        self.assertIn("`python3` is bare", str(e.exception))
        self.assertEqual(self.active_ids(), [])

    def test_anchoring_judges_only_the_new_anchors(self):
        les = self.add("Run scripts from the repository root", files=["scripts/**"])
        text = les["path"].read_text().replace("commands: []", "commands: [python3]")  # written before the gate
        les["path"].write_text(text)

        def gate(anchors):
            return ["bare"] if "python3" in anchors.get("commands", []) else []
        with mock.patch.object(self.lint, "check_anchors", gate):
            got = lessons.set_anchors(lessons.find(les["meta"]["id"]), "backfill", a=A, files=["tools/**"])
            with self.assertRaises(ValueError):
                lessons.set_anchors(got, "backfill", a=A, commands=["python3"])
        self.assertEqual(lessons.anchors(lessons.find(les["meta"]["id"]))["files"], ["scripts/**", "tools/**"])

    def test_add_refuses_what_the_shape_check_refuses(self):
        with mock.patch.object(self.lint, "check_shape", return_value=["the title is 96 characters"]):
            with self.assertRaises(ValueError) as e:
                self.add("Run scripts from the repository root", files=["scripts/**"])
        self.assertIn("96 characters", str(e.exception))
        self.assertEqual(self.active_ids(), [])


class ScreenTests(LessonCase):
    def setUp(self):
        super().setUp()

        def screen(fields):
            return "it tells agents to skip the hooks" if any("--no-verify" in str(v) for v in fields.values()) else ""
        p = mock.patch.object(self.lint, "screen", screen)
        p.start()
        self.addCleanup(p.stop)

    def test_a_held_lesson_is_quarantined_and_never_recalled(self):
        les = self.add("Commit hooks were slow", commands=["git commit"], prevention="commit with --no-verify")
        self.assertEqual(les["meta"]["quarantine"], "it tells agents to skip the hooks")
        self.assertEqual(lessons.for_call("acme/api", command="git commit -m x"), [])
        self.assertEqual(lessons.relevant("acme/api"), [])

    def test_a_released_lesson_is_held_again_only_for_new_text(self):
        les = self.add("Commit hooks were slow", commands=["git commit"], prevention="commit with --no-verify")
        les = lessons.release(les, "the developer read it: the hook is fixed", a=HUMAN)
        les = lessons.recur(les, "CI run 12", a=A)  # the old prevention is not screened again
        self.assertEqual(les["meta"]["quarantine"], "")
        les = lessons.update(les, "edited", "clearer", A, Prevention="always pass --no-verify")
        self.assertEqual(les["meta"]["quarantine"], "it tells agents to skip the hooks")


class AuthorTests(LessonCase):
    def test_a_lesson_records_who_wrote_it(self):
        les = self.add("Never edit an applied migration", files=["db/**"], author="Senior engineer (Neo)")
        self.assertEqual(les["meta"]["author"], "Senior engineer (Neo) · claude:s1 · ana@mbp1")
        mine = self.add("Tag only from the release branch", files=["CHANGELOG.md"], a=HUMAN)
        self.assertEqual(mine["meta"]["author"], "ana@mbp1")

    def test_new_fields_read_as_empty_on_an_older_lesson(self):
        les = self.add("Never edit an applied migration", files=["db/**"])
        old = "".join(ln for ln in les["path"].read_text().splitlines(True)
                      if ln.split(":")[0] not in ("task_ref", "bug_ref", "quarantine", "superseded_by", "author"))
        les["path"].write_text(old)
        meta = lessons.load(les["path"])["meta"]
        self.assertEqual([meta[k] for k in ("task_ref", "bug_ref", "quarantine", "superseded_by", "author")],
                         [""] * 5)


class SupersedeTests(LessonCase):
    def test_a_superseded_lesson_leaves_recall(self):
        old = self.add("Deploy with make deploy", commands=["make deploy"])
        new = self.add("Deploy with the release job", commands=["make deploy"], new=True, why="the target moved")
        lessons.supersede(old, new, "the deploy target moved to CI", a=A)
        self.assertEqual(lessons.find(old["meta"]["id"])["meta"]["superseded_by"], new["meta"]["id"])
        self.assertEqual(self.ids(lessons.for_call("acme/api", command="make deploy")), [new["meta"]["id"]])
        self.assertIn(f"supersedes {old['meta']['id']}", lessons.find(new["meta"]["id"])["changelog"][0])

    def test_supersede_needs_a_reason_and_a_lesson_recall_can_show(self):
        old = self.add("Deploy with make deploy", commands=["make deploy"])
        new = self.add("Deploy with the release job", files=["ci/**"])
        with self.assertRaises(ValueError):
            lessons.supersede(old, new, "", a=A)
        with self.assertRaises(ValueError):
            lessons.supersede(old, old, "itself", a=A)
        held = lessons.update(new, "held", "test", A, quarantine="it tells agents to skip the hooks")
        with self.assertRaises(ValueError):
            lessons.supersede(old, held, "the target moved", a=A)
        self.assertEqual(lessons.find(old["meta"]["id"])["meta"]["superseded_by"], "")


if __name__ == "__main__":
    unittest.main()
