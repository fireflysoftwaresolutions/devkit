# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The night's lessons work in `dev sleep`: the outcome events are folded before strength is judged, the event log is
pruned to 45 days, a prevented lesson whose prevention is gone is reopened once, a dry run changes nothing, and the
summary counts what changed."""
import contextlib
import io
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from test_lessons_base import A, DAY, LessonCase

from devkit import bugs, core, gc, lessonevents, lessons, sleep


class SleepCase(LessonCase):
    def setUp(self):
        super().setUp()
        state = self.root / "state"
        for obj, name, value in ((sleep, "STATE", state), (sleep, "LAST", state / "sleep.json"),
                                 (lessonevents, "EVENTS", state / "lessons-events.jsonl")):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.dict(core.CFG, {"github_user": "ana"}),  # publish_usage names the file by the user
                  mock.patch.object(gc, "_log", lambda msg: None),
                  mock.patch.object(bugs, "trim", lambda now=None: {})):
            p.start()
            self.addCleanup(p.stop)
        self.prune = mock.patch.object(lessonevents, "prune", create=True, return_value=0).start()
        self.addCleanup(mock.patch.stopall)
        self.repo = self.root / "repo"
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "test_here.py").write_text("def test_here():\n    pass\n")
        self.projects({"acme/api": str(self.repo)})

    def projects(self, paths: dict):
        """The projects map: registry.json with each project's checkout (None when not on this machine)."""
        f = core.HANDOFF / "registry.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"projects": [{"id": pid, "path": p} for pid, p in paths.items()]}))

    def sleep(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = sleep.run(**kw)
        return result, out.getvalue()

    def reopen_entries(self, les):
        return [e for e in lessons.find(les["meta"]["id"])["changelog"] if "reopened" in e]


class FoldTests(SleepCase):
    def test_the_night_folds_outcome_events_before_it_judges_strength(self):
        heeded = self.add("Heeded lately", files=["src/a/**"])
        idle = self.add("Idle for months", files=["src/b/**"])
        lessons.track([idle["meta"]["id"]], "lookup")  # this machine has usage, so the night publishes it
        future = time.time() + 200 * DAY
        lessonevents.record("shown", heeded["meta"]["id"], "call", "claude", "s1", now=future - 3 * DAY)
        lessonevents.on_end("s1", "claude", now=future - 2 * DAY)

        result, _ = self.sleep(quiet=True, now=future)

        self.assertEqual(self.active_ids(), [heeded["meta"]["id"]])  # heeded two days ago: alive
        self.assertEqual(self.archived_ids(), [idle["meta"]["id"]])  # the same age with no outcome: forgotten
        self.assertEqual(lessons.usage().get(heeded["meta"]["id"], {}).get("heeded"), 1)  # the team's counts have it
        self.assertEqual(result["counts"]["folded"], 1)


class PruneTests(SleepCase):
    def test_the_event_log_is_pruned_to_45_days(self):
        self.prune.return_value = 7
        result, out = self.sleep()
        self.assertEqual(self.prune.call_count, 1)
        self.assertEqual(self.prune.call_args.kwargs["days"], 45)
        self.assertEqual(result["counts"]["pruned"], 7)
        self.assertIn("7 pruned", out)

    def test_a_fold_that_fails_keeps_the_log_whole(self):
        with mock.patch.object(lessonevents, "fold", side_effect=OSError("disk full")):
            result, out = self.sleep()
        self.prune.assert_not_called()  # the events not yet counted stay in the log for the next night
        self.assertIn("disk full", out)
        self.assertEqual(result["counts"]["pruned"], 0)

    def test_a_consolidation_that_fails_lets_the_rest_of_the_night_run(self):
        trimmed = []
        with mock.patch.object(lessons, "consolidate", side_effect=OSError("git mv failed")), \
                mock.patch.object(lessons, "publish_usage", side_effect=OSError("usage locked")), \
                mock.patch.object(bugs, "trim", lambda now=None: trimmed.append(1) or {}):
            result, out = self.sleep()
        self.assertIn("git mv failed", out)
        self.assertIn("usage locked", out)
        self.prune.assert_called_once()  # the event log is still pruned
        self.assertEqual(trimmed, [1])  # and the registry's local files trimmed
        self.assertTrue(sleep.LAST.exists())  # and the night recorded


class PreventionCheckTests(SleepCase):
    def test_a_prevented_lesson_whose_test_is_gone_is_reopened_once(self):
        gone = lessons.prevent(self.add("Covered by a deleted test", files=["src/a/**"]),
                               "test", "tests/test_gone.py", "covered", a=A)
        held = lessons.prevent(self.add("Covered by a test still there", files=["src/b/**"]),
                               "test", "tests/test_here.py", "covered", a=A)

        first, _ = self.sleep(quiet=True)
        second, _ = self.sleep(quiet=True)

        self.assertEqual(lessons.find(gone["meta"]["id"])["meta"]["status"], "open")
        self.assertEqual(lessons.find(held["meta"]["id"])["meta"]["status"], "prevented")
        entries = self.reopen_entries(gone)
        self.assertEqual(len(entries), 1)
        self.assertIn("tests/test_gone.py no longer exists", entries[0])
        self.assertEqual([(k, i) for k, i, _ in first["lessons"] if k == "reopen"], [("reopen", gone["meta"]["id"])])
        self.assertEqual([k for k, _, _ in second["lessons"] if k == "reopen"], [])

    def test_a_project_not_checked_out_on_this_mac_is_skipped(self):
        self.projects({"acme/api": str(self.repo), "acme/web": None, "acme/old": str(self.root / "moved-away")})
        rule = "no-such-rule-zq9"  # in no guard and no lesson rule: gone wherever it is judged
        skipped = [lessons.prevent(self.add(f"Guarded in {s}", scope=s, files=["src/**"]), "guard", rule, "guarded",
                                   a=A) for s in ("acme/web", "acme/old")]
        checked = lessons.prevent(self.add("Guarded here", files=["lib/**"]), "guard", rule, "guarded", a=A)

        result, _ = self.sleep(quiet=True)

        self.assertEqual([i for k, i, _ in result["lessons"] if k == "reopen"], [checked["meta"]["id"]])
        for les in skipped:
            self.assertEqual(lessons.find(les["meta"]["id"])["meta"]["status"], "prevented")

    def test_global_and_initiative_lessons_are_checked_for_guard_rules_only(self):
        # guard rules exist on every machine; the tests and skills such a lesson names belong to no checkout here
        rule = "no-such-rule-zq9"
        guarded = [lessons.prevent(self.add(f"Guarded in {s}", scope=s, files=["src/**"]), "guard", rule, "guarded",
                                   a=A)["meta"]["id"] for s in ("global", "initiatives/billing")]
        unjudged = [lessons.prevent(self.add("Covered by a deleted test", scope="global", files=["lib/**"]), "test",
                                    "tests/test_gone.py", "covered", a=A),
                    lessons.prevent(self.add("Covered by a missing skill", scope="global", files=["web/**"]), "skill",
                                    "no-such-skill-zq9", "covered", a=A),
                    lessons.prevent(self.add("Guarded by a file that moved", scope="global", files=["ops/**"]),
                                    "guard", "lib/devkit/guard_gone.py NO_SUCH_GATE", "guarded", a=A)]

        result, _ = self.sleep(quiet=True)

        self.assertEqual(sorted(i for k, i, _ in result["lessons"] if k == "reopen"), sorted(guarded))
        for les in unjudged:
            self.assertEqual(lessons.find(les["meta"]["id"])["meta"]["status"], "prevented")


class DryRunTests(SleepCase):
    def snapshot(self) -> dict:
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def test_a_dry_run_changes_nothing(self):
        gone = lessons.prevent(self.add("Covered by a deleted test", files=["src/a/**"]),
                               "test", "tests/test_gone.py", "covered", a=A)
        lessons.archive(self.add("Taken by the old cap", files=["src/c/**"]), "over the active cap for acme/api", A)
        lessons.track([gone["meta"]["id"]], "lookup")
        lessonevents.record("shown", gone["meta"]["id"], "call", "claude", "s1", now=time.time() - 3 * DAY)
        lessonevents.on_end("s1", "claude", now=time.time() - 2 * DAY)
        before = self.snapshot()

        result, out = self.sleep(dry_run=True)

        self.assertEqual(self.snapshot(), before)
        self.prune.assert_not_called()
        self.assertIn("dry run", out)
        self.assertEqual(sorted(k for k, _, _ in result["lessons"]), ["reopen", "restore"])  # what a real night does


class SummaryTests(SleepCase):
    def test_the_summary_counts_restored_merged_archived_reopened_and_pruned(self):
        lessons.archive(self.add("Taken by the old cap", files=["src/c/**"]), "over the active cap for acme/api", A)
        lessons.prevent(self.add("Covered by a deleted test", files=["src/a/**"]), "test", "tests/test_gone.py",
                        "covered", a=A)
        where = {"commands": ["make deploy"], "errors": ["deploy key was rejected"], "files": ["deploy/**"]}
        self.add("Deploy key rejected on the first run", **where)
        self.add("Deploy key rejected again", new=True, why="a near-duplicate for the merge", **where)
        retired = lessons.retire(self.backdate(self.add("No longer applies", files=["old/**"]), 60), "obsolete", a=A)
        self.prune.return_value = 4

        result, out = self.sleep(now=time.time() + 40 * DAY)

        self.assertEqual(result["counts"], {"restored": 1, "merged": 1, "archived": 1, "reopened": 1, "pruned": 4,
                                            "folded": 0})
        self.assertIn("1 restored · 1 merged · 1 archived · 1 reopened", out)
        self.assertIn("0 folded · 4 pruned", out)
        self.assertIn(retired["meta"]["id"], self.archived_ids())
        self.assertEqual(json.loads(sleep.LAST.read_text())["counts"]["reopened"], 1)


if __name__ == "__main__":
    unittest.main()
