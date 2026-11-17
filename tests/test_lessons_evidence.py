# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Evidence must establish behavior before a lesson leaves ordinary recall."""
import time
import threading
from unittest import mock

from test_lessons_base import A, LessonCase
from devkit import lessons, lessonevents, core, work, testx


class EvidenceTests(LessonCase):
    def test_external_import_mutation_cannot_leave_a_suppression_receipt(self):
        import json
        import os
        from test_testx import Env, PROVE_BASE, MUL, TEST_MUL, with_tests, dev
        with Env() as env:
            repo = env.repo(PROVE_BASE)
            external = env.root / "external"
            external.mkdir()
            factor = external / "factor.py"
            factor.write_text("VALUE = 1\n")
            (repo / "calc.py").write_text(MUL + "\nimport factor\n"
                                         "def mul(a, b): return a * b * factor.VALUE\n")
            (repo / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (core.HANDOFF / "registry.json").parent.mkdir(parents=True, exist_ok=True)
            (core.HANDOFF / "registry.json").write_text(json.dumps({"projects": [
                {"id": "acme/proj", "path": str(repo)}]}))
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(external), "PYTHONDONTWRITEBYTECODE": "1"}):
                rc, out, err = dev(repo, "prove", "--runs", "1", "--json")
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(json.loads(out)["tests"][0]["verdict"], "proves the change")
                self.assertNotIn("prevention_eligible", json.loads(out))  # ordinary proof output stays unchanged
                ref = "tests/test_calc.py::test_calc.CalcTests.test_mul"
                les = self.add("Verify external multiplication factor", scope="acme/proj", files=["calc.py"])
                lessons.update(les, "legacy prevention", "old receipt", a=A, status="prevented",
                               prevention="test", prevention_ref=ref)
                factor.write_text("VALUE = 0\n")
                rc, out, err = dev(repo, "run", "tests/test_calc.py", "--json")
                self.assertEqual(rc, 1, out + err)
                self.assertIsNone(testx.prevention_proof(repo, ref))
                self.assertTrue(lessons.unverified(lessons.find(les["meta"]["id"])))

    def test_unresolved_recurrences_do_not_starve_the_next_valid_event(self):
        from devkit import state
        lid = self.add("Check migration ordering", files=["db/**"])["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            unresolved = {f"{n:064x}": {"id": "L-missing000", "project": "acme/api"} for n in range(50)}
            state.update(lessonevents._recurrences_path(), lambda doc: doc.update(unresolved))
            lessonevents.record("shown", lid, "call", "codex", "s1")
            lessonevents.on_hit([lid], "s1", "codex", source_event="valid-51", project="acme/api")
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(lessonevents.drain_recurrences(), 1)
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 2)
            self.assertEqual(state.read(lessonevents._recurrences_path()), {})  # evidence for no lesson leaves

    def queue(self, entries):
        from devkit import state
        state.update(lessonevents._recurrences_path(), lambda doc: doc.update(entries))

    def queued(self):
        from devkit import state
        return state.read(lessonevents._recurrences_path()) or {}

    def test_a_malformed_queue_entry_leaves_the_queue(self):
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            self.queue({"not-a-hash": {"id": "L-gf000000001", "project": "acme/api", "ts": int(time.time())},
                        "a" * 64: "not an event"})
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(self.queued(), {})

    def test_evidence_for_a_lesson_outside_its_project_leaves_the_queue(self):
        lid = self.add("Check migration ordering", files=["db/**"])["meta"]["id"]  # a lesson of acme/api
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            self.queue({"b" * 64: {"id": lid, "project": "acme/web", "ts": int(time.time())}})
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(self.queued(), {})
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 1)

    def test_evidence_older_than_thirty_days_leaves_the_queue_unapplied(self):
        lid = self.add("Check migration ordering", files=["db/**"])["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            self.queue({"c" * 64: {"id": lid, "project": "acme/api", "ts": int(time.time() - 31 * 86400)}})
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(self.queued(), {})
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 1)

    def test_a_full_queue_of_evidence_for_no_lesson_still_takes_new_evidence(self):
        lid = self.add("Check migration ordering", files=["db/**"])["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            self.queue({f"{n:064x}": {"id": "L-gf000000001", "project": "acme/api", "ts": int(time.time())}
                        for n in range(512)})
            for _ in range(11):  # the nightly drains of a week and a half, fifty entries each
                lessonevents.drain_recurrences()
            lessonevents.record("shown", lid, "call", "codex", "s1")
            self.assertEqual(lessonevents.on_hit([lid], "s1", "codex", source_event="new-1", project="acme/api"),
                             [lid])
            self.assertEqual(lessonevents.drain_recurrences(), 1)

    def test_session_text_in_another_agents_name_is_not_exposure_in_this_session(self):
        lid = self.add("Check migration ordering", files=["db/**"])["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            lessonevents.record("shown", lid, "call", "target", "other-session")
            lessonevents.on_hit([lid], "target", "target", source_event="tool-1", project="acme/api")
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 1)

    def test_maintenance_does_not_hold_the_hot_queue_lock_during_lesson_writes(self):
        first = self.add("Check migration ordering", files=["db/**"])["meta"]["id"]
        second = self.add("Check parser boundaries", files=["parser/**"])["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            for lid in (first, second):
                lessonevents.record("shown", lid, "call", "codex", "s1")
            lessonevents.on_hit([first], "s1", "codex", source_event="tool-1", project="acme/api")
            original = lessons.recur

            def during_write(*args, **kwargs):
                thread = threading.Thread(target=lessonevents.on_hit, args=([second], "s1", "codex"),
                                          kwargs={"source_event": "tool-2", "project": "acme/api"})
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                return original(*args, **kwargs)

            with mock.patch.object(lessons, "recur", side_effect=during_write):
                self.assertEqual(lessonevents.drain_recurrences(), 1)
            self.assertEqual(lessonevents.drain_recurrences(), 1)
            self.assertEqual(lessons.find(second)["meta"]["occurrences"], 2)

    def test_maintenance_replay_after_lesson_write_does_not_repeat_occurrence(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        lid = les["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            lessonevents.record("shown", lid, "call", "codex", "s1")
            lessonevents.on_hit([lid], "s1", "codex", source_event="tool-1", project="acme/api")
            original = lessons.recur

            def interrupted(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError("interrupted before queue acknowledgement")

            with mock.patch.object(lessons, "recur", side_effect=interrupted):
                self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(lessonevents.drain_recurrences(), 1)
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 2)

    def test_malformed_local_proof_is_unknown_not_a_recall_crash(self):
        import json
        path = testx._proof_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checks": {"test.py::test_check": {"ts": "not a timestamp"}}}))
        self.assertIsNone(testx.prevention_proof(self.root, "test.py::test_check"))

    def test_real_causal_proof_prevents_only_the_exact_scope_and_check(self):
        import json
        from test_testx import Env, PROVE_BASE, MUL, TEST_MUL, with_tests, dev
        with Env() as env:
            repo = env.repo(PROVE_BASE)
            (repo / "tests" / "test_calc.py").write_text(with_tests(TEST_MUL))
            (repo / "calc.py").write_text(MUL)
            core.HANDOFF.mkdir(parents=True, exist_ok=True)
            (core.HANDOFF / "registry.json").write_text(json.dumps({"projects": [
                {"id": "acme/proj", "path": str(repo)}, {"id": "acme/other", "path": str(repo)}]}))
            rc, out, err = dev(repo, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            ref = "tests/test_calc.py::test_calc.CalcTests.test_mul"
            les = self.add("Verify multiplication", scope="acme/proj", files=["calc.py"])
            checked = lessons.prevent(les, "test", ref, "causal proof passed", a=A)
            self.assertEqual(checked["meta"]["status"], "prevented")
            self.assertEqual(lessons.unverified(checked), "")
            related = self.add("Verify multiplication inputs", scope="acme/proj", files=["calc.py"])
            lessons.prevent(related, "test", ref, "same proven check", a=A)
            cache = {}
            with mock.patch.object(testx, "_proof_snapshot", wraps=testx._proof_snapshot) as snapshots:
                self.assertEqual([lessons.unverified(lessons.find(x["meta"]["id"]), cache) for x in (les, related)],
                                 ["", ""])
                self.assertEqual(snapshots.call_count, 1)
            other = self.add("Verify multiplication elsewhere", scope="acme/other", files=["calc.py"])
            elsewhere = lessons.prevent(other, "test", ref, "same name", a=A)
            self.assertEqual(elsewhere["meta"]["status"], "prevented")
            self.assertTrue(lessons.unverified(elsewhere))
            (repo / "calc.py").write_text("def mul(a, b): return 0\n")
            self.assertTrue(lessons.unverified(lessons.find(les["meta"]["id"])))
            self.assertEqual(lessons.recall_pool("acme/proj"), [])

    def test_third_distinct_failure_creates_one_task_and_replay_does_not_increment(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        lid = les["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            for source in ("first", "second", "second"):
                lessonevents.record("shown", lid, "call", "codex", "s1")
                lessonevents.on_hit([lid], "s1", "codex", source_event=source, project="acme/api")
                lessonevents.drain_recurrences()
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 3)
            self.assertEqual(len(work.all_items("acme/api")), 1)
            self.assertEqual(sum(e["event"] == "missed" for e in lessonevents._events()), 2)

    def test_one_source_event_changes_occurrences_once_even_after_replay(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        event = "a" * 64
        once = lessons.recur(les, "observed failure", a=A, source_event=event)
        twice = lessons.recur(once, "observed failure", a=A, source_event=event)
        self.assertEqual(twice["meta"]["occurrences"], 2)

    def test_failure_is_queued_on_hot_path_and_applied_by_maintenance_once(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        lid = les["meta"]["id"]
        with mock.patch.object(lessonevents, "EVENTS", self.root / "events.jsonl"):
            lessonevents.record("shown", lid, "call", "codex", "s1")
            for _ in range(2):
                lessonevents.on_hit([lid], "s1", "codex", source_event="tool-123", project="acme/api")
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 1)
            self.assertEqual(lessonevents.drain_recurrences(), 1)
            self.assertEqual(lessonevents.drain_recurrences(), 0)
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 2)

    def test_session_completion_does_not_strengthen_a_lesson(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        lid = les["meta"]["id"]
        now = time.time() + 86400
        self.assertEqual(lessons.strength(les, {lid: {"heeded": 20, "heeded_last": now}}, now),
                         lessons.strength(les, {}, now))

    def test_a_prevention_without_a_receipt_is_prevented_and_its_changelog_says_unverified(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        changed = lessons.prevent(les, "test", "tests/test_missing.py", "covers migration", a=A)
        self.assertEqual(changed["meta"]["status"], "prevented")
        self.assertIn("prevented by test: tests/test_missing.py", changed["changelog"][0])
        self.assertIn("unverified: ", changed["changelog"][0])

    def test_a_prevented_lesson_without_a_receipt_leaves_recall_and_is_flagged_unverified(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        lessons.update(les, "legacy prevention", "old client", a=A, status="prevented",
                       prevention="test", prevention_ref="tests/test_missing.py")
        self.assertNotIn(les["meta"]["id"], self.ids(lessons.recall_pool("acme/api")))
        self.assertTrue(lessons.unverified(lessons.find(les["meta"]["id"])))

    def test_an_open_lesson_is_never_flagged_unverified(self):
        les = self.add("Check the migration boundary", files=["db/**"])
        self.assertEqual(lessons.unverified(les), "")
