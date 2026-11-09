# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Sleep: usage, strength decay, merging, forgetting, restoring and hygiene (simulated time)."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, lessons, sleep, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
DAY = 86400


class SleepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL, sleep.STATE, sleep.LAST, core.HANDOFF)
        lessons.LESSONS, work.HANDOFF, core.HANDOFF = root / "handoff" / "lessons", root / "handoff", root / "handoff"
        lessons.USAGE_LOCAL = root / "state" / "lessons-usage.json"
        sleep.STATE, sleep.LAST = root / "state", root / "state" / "sleep.json"
        self.root = root
        self.ai_off = mock.patch.dict(core.CFG, {"ai": {"enabled": False}})  # never a real model call in tests
        self.ai_off.start()
        self.addCleanup(self.ai_off.stop)

    def tearDown(self):
        (lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL, sleep.STATE, sleep.LAST, core.HANDOFF) = self.saved
        self.tmp.cleanup()

    def add(self, title, scope="acme/api", **kw):
        return lessons.add(scope, title, cause=kw.pop("cause", "c " + title), prevention=kw.pop("prevention", "p"),
                           happened=kw.pop("happened", "it happened: " + title), a=A, **kw)

    def test_use_keeps_a_lesson_alive_and_unused_ones_fade(self):
        used, idle = self.add("Used often"), self.add("Never used")
        now = time.time() + 120 * DAY
        lessons.track([used["meta"]["id"]] * 1, "applied")
        u = lessons.usage()
        u[used["meta"]["id"]]["applied_last"] = now - DAY  # used yesterday, in the simulated future
        self.assertGreater(lessons.strength(used, u, now), 4 * lessons.strength(idle, u, now))

    def test_shown_but_never_used_loses_strength(self):
        les = self.add("Noise")
        u = {les["meta"]["id"]: {"shown": 40}}
        self.assertLess(lessons.strength(les, u), lessons.strength(les, {}))

    def test_merge_duplicates_and_keep_the_global_one(self):
        where = {"commands": ["git commit"], "errors": ["FAILED tests/test_api.py"], "activities": ["testing"]}
        g = self.add("Commit chained after a pipe hides failing tests", scope="global",
                     cause="tail exit code hides failing tests", prevention="use pipefail before commit", **where)
        p = self.add("Un commit tras un pipe oculta tests rotos", cause="tail oculta el código de salida",
                     prevention="usa pipefail antes del commit", stack=["python"], new=True,
                     why="a near-duplicate in another language, for the merge", **where)
        acts = lessons.consolidate()
        self.assertIn(("merge", p["meta"]["id"]), [(k, i) for k, i, _ in acts])
        kept = lessons.find(g["meta"]["id"])
        self.assertEqual(kept["meta"]["occurrences"], 2)
        self.assertEqual(lessons.anchors(kept)["stack"], ["python"])  # the merged lesson keeps both anchors
        self.assertTrue((lessons.LESSONS / "archive" / "acme/api" / p["path"].name).exists())

    def test_forget_and_restore(self):
        prevented = self.add("Old prevented")
        lessons.prevent(prevented, "test", "tests/x.py", "covered", a=A)
        retired = self.add("Old retired")
        lessons.retire(retired, "no longer applies", a=A)
        fresh = self.add("Fresh and open")
        future = time.time() + 200 * DAY
        acts = {i: k for k, i, _ in lessons.consolidate(now=future)}
        self.assertEqual(acts.get(prevented["meta"]["id"]), "forget")
        self.assertEqual(acts.get(retired["meta"]["id"]), "forget")
        self.assertEqual(acts.get(fresh["meta"]["id"]), "forget")  # open, 200 days unused and weak
        self.assertEqual(lessons.all_lessons(), [])
        back = lessons.restore(fresh["meta"]["id"], "it came back", a=A)
        self.assertEqual(back["meta"]["status"], "open")
        self.assertEqual([x["meta"]["id"] for x in lessons.all_lessons()], [fresh["meta"]["id"]])

    def test_dry_run_changes_nothing(self):
        les = self.add("Retired soon")
        lessons.retire(les, "obsolete", a=A)
        acts = lessons.consolidate(dry_run=True, now=time.time() + 40 * DAY)
        self.assertEqual([k for k, _, _ in acts], ["forget"])
        self.assertEqual(len(lessons.all_lessons()), 1)

    def test_prevented_lessons_leave_the_context(self):
        a_ = self.add("Open one", files=["src/a/**"])
        b_ = self.add("Prevented one", files=["src/b/**"])
        lessons.prevent(b_, "guard", "rule", "automated", a=A)
        ids = [x["meta"]["id"] for x in lessons.relevant("acme/api")]
        self.assertEqual(ids, [a_["meta"]["id"]])
        self.assertEqual(len(lessons.search("Prevented")), 1)  # still searchable

    def test_hygiene_trims_only_devkit_records(self):
        st = self.root / "state"
        (st / "runs").mkdir(parents=True)
        old_run = st / "runs" / "claude-x.json"
        old_run.write_text("{}")
        os.utime(old_run, (time.time() - 30 * DAY, time.time() - 30 * DAY))
        new_run = st / "runs" / "claude-y.json"
        new_run.write_text("{}")
        (st / "timing").mkdir()
        (st / "timing" / "2020-01.jsonl").write_text("{}\n")
        plan = sleep.hygiene_plan()
        paths = {Path(f).name for _, f, _ in plan}
        self.assertIn("claude-x.json", paths)
        self.assertIn("2020-01.jsonl", paths)
        self.assertNotIn("claude-y.json", paths)
        sleep.apply_hygiene(plan)
        self.assertFalse(old_run.exists())
        self.assertTrue(new_run.exists())


if __name__ == "__main__":
    unittest.main()
