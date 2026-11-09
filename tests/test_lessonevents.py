# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson outcome events: what reached an agent (shown), what it repeated anyway (missed), what it did not
(heeded); the fold into the usage counts, the rates per channel, and the recall coverage of recorded recurrences."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, lessonevents, lessons, state  # noqa: E402

T = 1_794_000_000.0  # a fixed clock
L1, L2, L3, L4 = "L-fx1aaaaaaa1", "L-fx2bbbbbbb2", "L-fx3ccccccc3", "L-fx4ddddddd4"
SID = "3890e084-8091-4eb8-ace8-1ab104d9a409"


class EventsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (lessonevents.EVENTS, lessons.USAGE_LOCAL, audit.AUDIT)
        lessonevents.EVENTS = root / "state" / "lessons-events.jsonl"
        lessons.USAGE_LOCAL = root / "state" / "lessons-usage.json"
        audit.AUDIT = root / "state" / "audit"

    def tearDown(self):
        lessonevents.EVENTS, lessons.USAGE_LOCAL, audit.AUDIT = self.saved
        self.tmp.cleanup()

    def lines(self) -> list:
        return [json.loads(x) for x in lessonevents.EVENTS.read_text().splitlines()]

    def usage(self) -> dict:
        return json.loads(lessons.USAGE_LOCAL.read_text())


class RecordTests(EventsTestCase):
    def test_an_event_is_one_line_in_the_log_format(self):
        lessonevents.record("shown", L1, "call", "claude", "s1", now=T + 0.7)
        self.assertEqual(self.lines(), [{"ts": int(T), "id": L1, "event": "shown", "channel": "call",
                                         "agent": "claude", "session": "s1"}])

    def test_the_log_is_private_to_the_user(self):
        lessonevents.record("shown", L1, "call", "claude", "s1", now=T)
        self.assertEqual(lessonevents.EVENTS.stat().st_mode & 0o777, 0o600)

    def test_an_unknown_event_a_bad_id_or_an_unknown_channel_is_refused(self):
        for args in (("seen", L1, "call"), ("shown", "L1; rm -rf", "call"), ("shown", L1, "chat"),
                     ("shown", L1, "")):
            with self.assertRaises(ValueError, msg=args):
                lessonevents.record(*args, "claude", "s1", now=T)
        self.assertFalse(lessonevents.EVENTS.exists())

    def test_text_that_would_break_a_line_stays_inside_its_line(self):
        lessonevents.record("shown", L1, "call", "claude\n{\"forged\": 1}", "s1\n", now=T)
        self.assertEqual(len(lessonevents.EVENTS.read_text().splitlines()), 1)

    def test_two_threads_recording_500_events_each_lose_none(self):
        def work(agent):
            for i in range(500):
                lessonevents.record("shown", L1 if i % 2 else L2, "call", agent, f"s{i % 7}", now=T + i)

        threads = [threading.Thread(target=work, args=(a,)) for a in ("claude", "codex")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        events = self.lines()  # every line parses: no two writes interleaved
        self.assertEqual(len(events), 1000)
        self.assertEqual(sum(e["agent"] == "claude" for e in events), 500)


class OutcomeTests(EventsTestCase):
    def show(self, lid, channel="call", agent="claude", session="s1", at=T):
        lessonevents.record("shown", lid, channel, agent, session, now=at)

    def test_shown_in_lists_the_lessons_shown_to_this_agent_in_this_session(self):
        self.show(L1, "call")
        self.show(L2, "session", at=T + 1)
        self.show(L1, "output", at=T + 2)  # shown again: still one pending lesson
        self.show(L3, agent="neo")
        self.show(L4, session="s2")
        self.assertEqual(lessonevents.shown_in("s1", "claude", now=T + 10), [L1, L2])

    def test_shown_in_ignores_what_is_recorded_after_now(self):
        self.show(L1, at=T + 60)
        self.assertEqual(lessonevents.shown_in("s1", "claude", now=T), [])

    def test_a_hit_after_a_show_in_the_same_session_is_missed(self):
        self.show(L1, "session")
        self.show(L1, "call", at=T + 1)
        self.assertEqual(lessonevents.on_hit([L1, L2], "s1", "claude", now=T + 5), [L1])
        missed = [e for e in self.lines() if e["event"] == "missed"]
        self.assertEqual(missed, [{"ts": int(T) + 5, "id": L1, "event": "missed", "channel": "call",
                                   "agent": "claude", "session": "s1"}])
        self.assertEqual(lessonevents.shown_in("s1", "claude", now=T + 6), [])

    def test_a_hit_by_another_agent_or_session_or_without_a_show_records_nothing(self):
        self.show(L1)
        self.assertEqual(lessonevents.on_hit([L1], "s1", "neo", now=T + 5), [])
        self.assertEqual(lessonevents.on_hit([L1], "s2", "claude", now=T + 5), [])
        self.assertEqual(lessonevents.on_hit([L2], "s1", "claude", now=T + 5), [])
        self.assertEqual([e["event"] for e in self.lines()], ["shown"])

    def test_a_second_hit_of_a_resolved_lesson_records_nothing(self):
        self.show(L1)
        lessonevents.on_hit([L1], "s1", "claude", now=T + 5)
        self.assertEqual(lessonevents.on_hit([L1, L1], "s1", "claude", now=T + 6), [])
        self.assertEqual(sum(e["event"] == "missed" for e in self.lines()), 1)

    def test_the_end_of_the_session_marks_the_rest_heeded(self):
        self.show(L1)
        self.show(L2, "brief", at=T + 1)
        lessonevents.on_hit([L1], "s1", "claude", now=T + 5)
        self.assertEqual(lessonevents.on_end("s1", "claude", now=T + 9), [L2])
        heeded = [e for e in self.lines() if e["event"] == "heeded"]
        self.assertEqual([(e["id"], e["channel"], e["ts"]) for e in heeded], [(L2, "brief", int(T) + 9)])
        self.assertEqual(lessonevents.on_end("s1", "claude", now=T + 10), [])

    def test_a_lesson_shown_again_after_it_was_resolved_is_pending_again(self):
        self.show(L1)
        lessonevents.on_end("s1", "claude", now=T + 5)
        self.show(L1, at=T + 6)
        self.assertEqual(lessonevents.on_hit([L1], "s1", "claude", now=T + 7), [L1])

    def test_two_hooks_hitting_the_same_lesson_at_once_record_one_miss(self):
        self.show(L1)
        out = []
        threads = [threading.Thread(target=lambda: out.append(lessonevents.on_hit([L1], "s1", "claude", now=T + 5)))
                   for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(len(x) for x in out), [0] * 7 + [1])
        self.assertEqual(sum(e["event"] == "missed" for e in self.lines()), 1)


class FoldTests(EventsTestCase):
    def outcomes(self):
        lessonevents.record("shown", L1, "call", "claude", "s1", now=T)
        lessonevents.record("shown", L2, "session", "claude", "s1", now=T)
        lessonevents.on_hit([L1], "s1", "claude", now=T + 10)
        lessonevents.on_end("s1", "claude", now=T + 20)
        lessonevents.record("shown", L1, "brief", "neo", "s2", now=T + 30)
        lessonevents.on_end("s2", "neo", now=T + 40)

    def test_fold_adds_heeded_and_missed_and_leaves_the_shown_counts_to_track(self):
        lessons.USAGE_LOCAL.parent.mkdir(parents=True, exist_ok=True)
        lessons.USAGE_LOCAL.write_text(json.dumps({L1: {"shown": 4, "shown_call": 2, "recur": 1}}))
        self.outcomes()
        totals = lessonevents.fold(now=T + 600)
        self.assertEqual(totals, {"heeded": 2, "missed": 1, "lessons": 2})
        u = self.usage()
        self.assertEqual({k: v for k, v in u[L1].items() if not k.endswith("_last")},
                         {"shown": 4, "shown_call": 2, "recur": 1, "missed": 1, "heeded": 1})
        self.assertEqual(u[L1]["missed_last"], int(T) + 10)
        self.assertEqual(u[L1]["heeded_last"], int(T) + 40)
        self.assertEqual({k: v for k, v in u[L2].items() if not k.endswith("_last")}, {"heeded": 1})

    def test_fold_twice_does_not_double_count(self):
        self.outcomes()
        lessonevents.fold(now=T + 600)
        first = self.usage()
        self.assertEqual(lessonevents.fold(now=T + 700), {"heeded": 0, "missed": 0, "lessons": 0})
        self.assertEqual(self.usage(), first)

    def test_fold_adds_only_the_events_recorded_since_the_last_fold(self):
        self.outcomes()
        lessonevents.fold(now=T + 600)
        lessonevents.record("shown", L1, "call", "claude", "s3", now=T + 900)
        lessonevents.on_hit([L1], "s3", "claude", now=T + 901)
        self.assertEqual(lessonevents.fold(now=T + 1600), {"heeded": 0, "missed": 1, "lessons": 1})
        self.assertEqual(self.usage()[L1]["missed"], 2)

    def test_events_of_the_last_minute_wait_for_the_next_fold(self):
        lessonevents.record("shown", L1, "call", "claude", "s1", now=T)
        lessonevents.on_end("s1", "claude", now=T + 10)
        self.assertEqual(lessonevents.fold(now=T + 30)["heeded"], 0)
        self.assertEqual(lessonevents.fold(now=T + 120)["heeded"], 1)

    def test_fold_with_nothing_to_add_writes_nothing(self):
        lessonevents.fold(now=T)
        self.assertFalse(lessons.USAGE_LOCAL.exists())

    def test_fold_keeps_what_another_writer_adds_under_the_same_lock(self):
        self.outcomes()

        def track():
            for _ in range(200):
                state.update(lessons.USAGE_LOCAL,
                             lambda u: u.setdefault(L3, {}).__setitem__("shown", u[L3].get("shown", 0) + 1))

        t = threading.Thread(target=track)
        t.start()
        lessonevents.fold(now=T + 600)
        t.join()
        self.assertEqual(self.usage()[L3]["shown"], 200)
        self.assertEqual(self.usage()[L1]["missed"], 1)


class RatesTests(EventsTestCase):
    def test_rates_count_each_channel_that_showed_a_lesson_and_list_the_most_missed(self):
        now = time.time()
        rec = lessonevents.record
        rec("shown", L1, "session", "claude", "s1", now=now - 50)
        rec("shown", L1, "call", "claude", "s1", now=now - 40)
        lessonevents.on_hit([L1], "s1", "claude", now=now - 30)
        rec("shown", L1, "brief", "neo", "s2", now=now - 50)
        lessonevents.on_end("s2", "neo", now=now - 20)
        rec("shown", L2, "call", "claude", "s3", now=now - 50)
        lessonevents.on_end("s3", "claude", now=now - 20)
        rec("shown", L3, "call", "claude", "s4", now=now - 40 * 86400)  # outside the window
        lessonevents.on_hit([L3], "s4", "claude", now=now - 40 * 86400 + 1)
        r = lessonevents.rates(30)
        self.assertEqual(r["days"], 30)
        self.assertEqual(r["channels"]["session"], {"shown": 1, "heeded": 0, "missed": 1, "heeded_rate": 0.0})
        self.assertEqual(r["channels"]["call"], {"shown": 2, "heeded": 1, "missed": 1, "heeded_rate": 0.5})
        self.assertEqual(r["channels"]["brief"], {"shown": 1, "heeded": 1, "missed": 0, "heeded_rate": 1.0})
        self.assertEqual(r["most_missed"], [{"id": L1, "shown": 3, "heeded": 1, "missed": 1}])

    def test_a_channel_with_shows_but_no_outcome_yet_has_no_rate(self):
        lessonevents.record("shown", L1, "refusal", "claude", "s1", now=time.time() - 5)
        self.assertEqual(lessonevents.rates()["channels"]["refusal"],
                         {"shown": 1, "heeded": 0, "missed": 0, "heeded_rate": None})


class CoverageTests(EventsTestCase):
    def recur(self, lid, session=SID, outcome="ok"):
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{session}" if session else ""}):
            audit.record("dev.lesson", f"recur {lid} --evidence [text not recorded: 40 characters]", "", outcome)

    def test_a_recurrence_after_a_show_in_the_same_session_is_covered(self):
        lessonevents.record("shown", L1, "call", "claude", SID, now=time.time() - 60)
        lessonevents.record("shown", L1, "session", "neo", SID, now=time.time() - 50)
        self.recur(L1)
        c = lessonevents.coverage(14)
        self.assertEqual((c["recurrences"], c["shown_before"], c["coverage"]), (1, 1, 1.0))
        self.assertEqual(c["items"][0]["id"], L1)
        self.assertTrue(c["items"][0]["shown_before"])
        self.assertEqual(c["items"][0]["channels"], ["call", "session"])

    def test_a_recurrence_never_shown_or_shown_elsewhere_or_later_is_not_covered(self):
        lessonevents.record("shown", L2, "call", "claude", "another-session", now=time.time() - 60)
        lessonevents.record("shown", L3, "call", "claude", SID, now=time.time() + 600)
        for lid in (L1, L2, L3):
            self.recur(lid)
        c = lessonevents.coverage(14)
        self.assertEqual((c["recurrences"], c["shown_before"], c["coverage"]), (3, 0, 0.0))
        self.assertEqual([x["shown_before"] for x in c["items"]], [False, False, False])

    def test_a_failed_recur_and_other_lesson_commands_are_not_recurrences(self):
        self.recur(L1, outcome="failed")
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{SID}"}):
            audit.record("dev.lesson", f"show {L1}", "", "ok")
        self.assertEqual(lessonevents.coverage(14)["recurrences"], 0)

    def test_a_recurrence_without_a_session_is_counted_as_unknown(self):
        self.recur(L1, session="")
        c = lessonevents.coverage(14)
        self.assertEqual((c["recurrences"], c["unknown"], c["coverage"]), (1, 1, None))
        self.assertIsNone(c["items"][0]["shown_before"])

    def test_recurrences_older_than_the_window_are_left_out(self):
        audit.AUDIT.mkdir(parents=True, exist_ok=True)
        old = {"ts": time.time() - 20 * 86400, "user": "ana", "host": "mbp1", "agent": "claude", "session": SID[:12],
               "action": "dev.lesson", "target": f"recur {L1}", "detail": "", "outcome": "ok"}
        (audit.AUDIT / "2026-10.jsonl").write_text(json.dumps(old) + "\n")
        self.assertEqual(lessonevents.coverage(14)["recurrences"], 0)
        self.assertEqual(lessonevents.coverage(30)["recurrences"], 1)


if __name__ == "__main__":
    unittest.main()
