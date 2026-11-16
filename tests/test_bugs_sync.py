# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The registry on this machine: the cache read only from the bot's records, the outbox that the sender rebuilds and
re-checks before anything leaves, consent for one GitHub login, the hourly sync, and the nightly trim."""
import contextlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import bugs, cli, core, sleep  # noqa: E402

FP = "3f9a1c2e7b10"
NEW_FP = "0123456789ab"
NOW = time.time()
DAY = 86400.0
TEAM_HINT = {"steps": [{"hint": "from-root", "dev": "progress step"}], "author_assoc": "MEMBER", "author_hash": "h1",
             "evidence": {"successes": 1}, "confirmations": [], "failures": []}


def _record(fp=FP, **extra):
    rec = {"devkit_bug": 1, "fingerprint": fp, "kind": "crash", "component": "progress",
           "title": "crash in dev progress step: KeyError", "first_version": "26.11.5", "versions": ["26.11.5"],
           "voters": {"v1": {"impact": "blocked", "ts": NOW}}, "impacts": {"blocked": 1, "detoured": 0, "minor": 0},
           "score": 4.5, "last_seen": NOW, "status": "open", "fixed_in": "", "detours": [], "report": {}}
    rec.update(extra)
    return rec


def _bot(body, cid=10):
    return {"id": cid, "user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}


def _issue(number, opener="someone", **extra):
    issue = {"number": number, "state": "open", "closed_at": None, "updated_at": "2026-11-08T10:00:00Z",
             "labels": [{"name": "devkit-bug"}], "user": {"login": opener}}
    issue.update(extra)
    return issue


class FakeClient:
    """GitHub as the sender and the cache see it, in memory."""

    def __init__(self, issues=(), comments=None, login="ana", fail=False, private_fails=False):
        self._issues, self._comments, self._login = list(issues), dict(comments or {}), login
        self.fail, self.private_fails = fail, private_fails
        self.created, self.posted, self.private = [], [], []

    def _net(self):
        if self.fail:
            raise OSError("network is unreachable")

    def issues(self, label, since):
        self._net()
        return list(self._issues)

    def comments(self, number):
        self._net()
        return list(self._comments.get(number, []))

    def create_issue(self, title, body):
        self._net()
        self.created.append((title, body))
        return 100 + len(self.created)

    def comment(self, number, body):
        self._net()
        self.posted.append((number, body))

    def login(self):
        return self._login

    def private_report(self, title, body):
        self._net()
        if self.private_fails:
            raise RuntimeError("private vulnerability reporting is not enabled")
        self.private.append((title, body))


def _payload(kind, fp=FP, **fields):
    base = {"devkit_bug": 1, "type": kind, "fingerprint": fp, "version": "26.11.7"}
    if kind == "report":
        base.update({"impact": "blocked", "kind": "crash", "command": "progress step", "exc_class": "KeyError",
                     "frames": ["work.actor_str"]})
    elif kind == "vote":
        base.update({"impact": "blocked", "window": "2026-11-08"})
    elif kind == "detour":
        base.update({"steps": [{"hint": "from-root", "dev": "progress step"}], "evidence": {"successes": 1}})
    base.update(fields)
    return base


def _sent(body):
    return bugs.parse_payload(body)


class StateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (bugs.BUGS, bugs.CRASHES)
        bugs.BUGS, bugs.CRASHES = root / "bugs", root / "crashes"
        self.deny = mock.patch.object(bugs, "deny_words", lambda: frozenset({"acme", "payments-core"}))
        self.deny.start()

    def tearDown(self):
        self.deny.stop()
        bugs.BUGS, bugs.CRASHES = self.saved
        self.tmp.cleanup()

    def cache_bug(self, rec=None, number=42):
        rec = rec or _record()
        self.fetches = getattr(self, "fetches", 0) + 1  # the issue changed since the last fetch
        issue = _issue(number, updated_at=f"2026-11-08T10:00:{self.fetches:02d}Z")
        r = bugs.fetch(FakeClient([issue], {number: [_bot(bugs.render_record(rec))]}), now=NOW)
        self.assertTrue(r["ok"], r)

    def edit_item(self, item_id, change):
        f = bugs.BUGS / "outbox" / f"{item_id}.json"
        item = json.loads(f.read_text())
        change(item)
        f.write_text(json.dumps(item))


class AutomaticReportTests(StateCase):
    def capture(self, **extra):
        values = {"exc_class": "KeyError", "command": "progress step", **extra}
        return bugs.record_occurrence("crash", NEW_FP, **values)

    def envelopes(self):
        return [json.loads(p.read_text()) for p in (bugs.BUGS / "outbox-v2").glob("*.json")]

    def test_explicit_security_report_is_private_before_automatic_capture(self):
        bugs.file_report(title="Unexpected result", security=True)
        self.assertEqual([i["destination"] for i in self.envelopes()], ["private"])

    def test_security_title_is_classified_before_automatic_capture(self):
        bugs.file_report(title="A token leak occurred")
        self.assertEqual([i["destination"] for i in self.envelopes()], ["private"])
        self.assertNotIn("token leak", json.dumps(self.envelopes()))

    def test_explicit_security_guard_report_is_private_before_automatic_capture(self):
        bugs.note_refusal("security-report", "_main_gate:registry", "git")
        bugs.file_report(refusal_id="_main_gate:registry", session="security-report", security=True)
        self.assertEqual([i["destination"] for i in self.envelopes()], ["private"])

    def test_exception_security_classification_precedes_display_truncation(self):
        for message in ("x" * 3000 + " token leak", "x" * 9000):
            for handler in (lambda exc: bugs.hook_error("prompt", exc),
                            lambda exc: bugs.swallowed("prompt", exc),
                            lambda exc: bugs.handle_crash([], exc)):
                with self.subTest(size=len(message), handler=handler):
                    handler(ValueError(message))
        self.assertTrue(self.envelopes())
        self.assertEqual({i["destination"] for i in self.envelopes()}, {"private"})

    def test_authenticated_warm_capture_does_not_discover_source_symbols(self):
        with mock.patch.object(bugs, "install_channel", return_value="release"):
            bugs._prepare_capture_vocabulary()
            bugs._module_functions.cache_clear()
            bugs.guard_refusal_ids.cache_clear()
            with mock.patch("ast.parse", side_effect=AssertionError("foreground AST discovery")):
                first = self.capture(frames=("bugs.handle_crash",))
                second = self.capture(frames=("bugs.handle_crash",))
        self.assertEqual([first["report_status"], second["report_status"]], ["queued", "coalesced"])
        self.assertEqual(self.envelopes()[0]["payload"]["frames"], ["bugs.handle_crash"])

    def test_automatic_guard_report_preserves_its_canonical_public_word(self):
        with mock.patch.object(bugs, "install_channel", return_value="release"):
            bugs._prepare_capture_vocabulary()
            fp = bugs.guard_fingerprint("_main_gate:registry", "git")
            bugs.record_occurrence("guard", fp, refusal_id="_main_gate:registry", word="git")
        payload = self.envelopes()[0]["payload"]
        self.assertEqual((payload["fingerprint"], payload["word"]), (fp, "git"))

    def test_fixed_acknowledged_bug_gets_bounded_regression_feedback(self):
        self.capture()
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        fp = self.envelopes()[0]["payload"]["fingerprint"]
        self.cache_bug(_record(fp=fp, status="fixed", fixed_in=core.VERSION), number=101)
        with mock.patch.object(bugs.time, "time", return_value=NOW + 2 * DAY):
            self.capture()
            bugs.flush(client, now=NOW + 2 * DAY)
            self.capture()
            bugs.flush(client, now=NOW + 2 * DAY + 60)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(len(client.posted), 1)
        self.assertEqual(client.posted[0][0], 101)
        self.assertEqual(_sent(client.posted[0][1])["fingerprint"], fp)

    def test_feedback_pointer_survives_flush_before_acknowledged_envelope_replacement(self):
        self._feedback_pointer_overlap(threaded=False)

    def test_feedback_pointer_survives_another_thread_flushing_during_capture(self):
        self._feedback_pointer_overlap(threaded=True)

    def _feedback_pointer_overlap(self, *, threaded):
        self.capture()
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        fp = self.envelopes()[0]["payload"]["fingerprint"]
        self.cache_bug(_record(fp=fp, status="fixed", fixed_in=core.VERSION), number=101)
        write = bugs._write_json
        overlapped = []

        def concurrent_flush(path, value):
            if path.parent.name == "outbox-v2" and value.get("feedback_pending") and not overlapped:
                overlapped.append(True)
                if threaded:
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(bugs.flush, client, now=NOW + 2 * DAY).result(timeout=5)
                else:
                    bugs.flush(client, now=NOW + 2 * DAY)
            return write(path, value)

        with mock.patch.object(bugs.time, "time", return_value=NOW + 2 * DAY), \
                mock.patch.object(bugs, "_write_json", side_effect=concurrent_flush):
            self.capture()
        self.assertEqual(overlapped, [True])
        self.assertEqual(bugs.flush(client, now=NOW + 2 * DAY)["sent"], 1)
        self.assertEqual((len(client.created), len(client.posted)), (1, 1))
        self.assertEqual(self.envelopes()[0]["state"], "acknowledged")

    def test_regression_feedback_waits_when_existing_issue_lookup_is_unavailable(self):
        self.capture()
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        fp = self.envelopes()[0]["payload"]["fingerprint"]
        self.cache_bug(_record(fp=fp, status="fixed", fixed_in=core.VERSION), number=101)
        with mock.patch.object(bugs.time, "time", return_value=NOW + 2 * DAY):
            self.capture()
        with mock.patch.object(bugs, "_issue_number", return_value=None):
            result = bugs.flush(client, now=NOW + 2 * DAY)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(self.envelopes()[0]["error_code"], "lookup_failed")

    def test_regression_comment_reconciles_after_accepted_then_disconnected(self):
        class Accepted(FakeClient):
            def comment(self, number, body):
                super().comment(number, body)
                raise OSError("accepted then disconnected")

            def reconcile_report(self, intent):
                return 101 if self.posted and intent["marker"] in self.posted[0][1] else None

        self.capture()
        client = Accepted()
        bugs.flush(client, now=NOW + 60)
        fp = self.envelopes()[0]["payload"]["fingerprint"]
        self.cache_bug(_record(fp=fp, status="fixed", fixed_in=core.VERSION), number=101)
        with mock.patch.object(bugs.time, "time", return_value=NOW + 2 * DAY):
            self.capture()
        self.assertEqual(bugs.flush(client, now=NOW + 2 * DAY)["failed"], 1)
        self.assertEqual(bugs.flush(client, now=NOW + 4 * DAY)["sent"], 1)
        self.assertEqual((len(client.created), len(client.posted)), (1, 1))
        self.assertEqual(self.envelopes()[0]["state"], "acknowledged")

    def test_detection_persists_report_without_a_hook_consumer(self):
        occurrence = self.capture()
        self.assertEqual(occurrence.get("report_status"), "queued")
        self.assertEqual(len(self.envelopes()), 1)
        self.assertEqual(self.envelopes()[0]["payload"]["exc_class"], "KeyError")

    def test_sharing_turned_off_holds_public_automatic_reports(self):
        bugs.set_consent("off", "old-account")
        self.capture()
        client = FakeClient(login="new-account")
        result = bugs.flush(client, now=NOW + 60)
        self.assertEqual((result["sent"], result["held"]), (0, 1))
        self.assertEqual(client.created, [])
        self.assertEqual(self.envelopes()[0]["state"], "pending")

    def test_bugs_share_off_holds_public_reports_and_still_sends_private_ones(self):
        class Private(FakeClient):
            def private_report(self, title, body):
                super().private_report(title, body)
                return "GHSA-2345-6789-cfgh"
        self.capture()
        self.capture(text="token leaked in guard")
        client = Private()
        with mock.patch.dict(core.CFG, {"bugs": {"share": "off", "sync": True}}):
            result = bugs.flush(client, now=NOW + 60)
        self.assertEqual((client.created, client.posted, len(client.private)), ([], [], 1))
        self.assertEqual((result["sent"], result["held"]), (1, 1))

    def test_a_held_public_report_sends_once_sharing_is_back_on(self):
        self.capture()
        client = FakeClient()
        with mock.patch.dict(core.CFG, {"bugs": {"share": "off", "sync": True}}):
            bugs.flush(client, now=NOW + 60)
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 1)
        self.assertEqual(len(client.created), 1)

    def test_bugs_sync_false_stops_every_send(self):
        self.capture()
        client = FakeClient()
        with mock.patch.dict(core.CFG, {"bugs": {"share": None, "sync": False}}):
            result = bugs.flush(client, now=NOW + 60)
        self.assertEqual(result["sent"], 0)
        self.assertIn("bugs.sync", result["error"])
        self.assertEqual((client.created, client.posted, client.private), ([], [], []))
        self.assertEqual(self.envelopes()[0]["state"], "pending")

    def test_eight_days_offline_preserves_unacknowledged_report(self):
        class Offline(FakeClient):
            def login(self):
                raise OSError("offline")

        self.capture()
        bugs.flush(Offline(), now=NOW + 8 * DAY)
        items = self.envelopes()
        self.assertEqual(len(items), 1)
        self.assertIn(items[0]["state"], ("retry", "blocked_transport"))
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 10 * DAY)["sent"], 1)

    def test_repeated_detection_coalesces_and_distinct_exception_survives(self):
        self.capture()
        self.capture()
        self.capture(exc_class="ValueError")
        self.assertEqual(sorted(i["count"] for i in self.envelopes()), [1, 2])

    def test_unknown_exception_and_local_frame_never_leave_machine(self):
        self.capture(exc_class="CustomerBillingError", frames=("private.customer",))
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        payload = _sent(client.created[0][1])
        self.assertEqual(payload["exc_class"], "Exception")
        self.assertEqual(payload["frames"], ["<local>"])
        self.assertNotIn("Customer", client.created[0][1])

    def test_changed_payload_or_copied_provenance_cannot_authorize_send(self):
        self.capture()
        path = next((bugs.BUGS / "outbox-v2").glob("*.json"))
        item = json.loads(path.read_text())
        item["report_id"] = "a" * 32
        path.write_text(json.dumps(item))
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 0)
        self.assertEqual(client.created, [])
        self.assertEqual(self.envelopes()[0]["state"], "quarantined")

    def test_security_classification_survives_removal_of_raw_text(self):
        self.capture(text="token leaked in guard")
        client = FakeClient(private_fails=True)
        bugs.flush(client, now=NOW + 60)
        self.assertEqual(client.created, [])
        self.assertEqual(self.envelopes()[0]["destination"], "private")
        self.assertNotIn("token leaked", json.dumps(self.envelopes()))

    def test_capture_uses_no_subprocess_or_private_word_discovery(self):
        with mock.patch.object(core, "sh", side_effect=AssertionError("subprocess")), \
                mock.patch.object(bugs, "deny_words", side_effect=AssertionError("private discovery")):
            self.assertEqual(self.capture().get("report_status"), "queued")

    def test_public_uncertain_send_reconciles_marker_before_retry(self):
        class AcceptThenDisconnect(FakeClient):
            def create_issue(self, title, body):
                super().create_issue(title, body)
                raise OSError("accepted then disconnected")

            def reconcile_report(self, intent):
                return 101 if intent["marker"] in self.created[0][1] else None

        self.capture()
        client = AcceptThenDisconnect()
        bugs.flush(client, now=NOW + 60)
        self.assertEqual(bugs.flush(client, now=NOW + 2 * DAY)["sent"], 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(self.envelopes()[0]["remote_id"], 101)

    def test_private_uncertain_send_never_reposts_under_another_principal(self):
        self.capture(text="credential failure")
        first = FakeClient(private_fails=True)
        bugs.flush(first, now=NOW + 60)
        second = FakeClient(login="other")
        bugs.flush(second, now=NOW + 2 * DAY)
        self.assertEqual((second.private, second.created), ([], []))
        self.assertEqual(self.envelopes()[0]["state"], "blocked_transport")

    def test_same_failure_after_acknowledgment_does_not_open_another_issue(self):
        self.capture()
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        self.capture()
        bugs.flush(client, now=NOW + 120)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(self.envelopes()[0]["count"], 2)

    def test_a_known_public_bug_gets_a_report_comment(self):
        occ = self.capture()
        item = self.envelopes()[0]
        self.cache_bug(_record(fp=item["payload"]["fingerprint"]))
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual(client.created, [])
        self.assertEqual(client.posted[0][0], 42)
        self.assertIn(occ["report_id"], client.posted[0][1])

    def test_lock_contention_spools_then_coalesces_without_loss(self):
        self.capture()
        @contextlib.contextmanager
        def busy(*args, **kwargs):
            yield False
        with mock.patch.object(core, "file_lock", busy):
            self.assertEqual(self.capture().get("report_status"), "queued")
        self.assertEqual(len(self.envelopes()), 2)
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(sum(i["count"] for i in self.envelopes()), 2)

    def test_a_storm_against_a_quarantined_report_adds_to_one_replacement(self):
        self.capture()
        path = next((bugs.BUGS / "outbox-v2").glob("*.json"))
        item = json.loads(path.read_text())
        item["state"] = "quarantined"
        path.write_text(json.dumps(item))
        for _ in range(5):
            self.assertIn(self.capture()["report_status"], ("queued", "coalesced"))
        self.assertEqual(sorted(i["count"] for i in self.envelopes()), [1, 5])
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)  # the replacement, never merged into evidence
        self.assertEqual(len(client.created), 1)

    def test_a_storm_while_the_report_is_locked_adds_to_one_spool(self):
        self.capture()
        canonical = next((bugs.BUGS / "outbox-v2").glob("*.json"))
        real = core.file_lock

        @contextlib.contextmanager
        def busy(path, wait=30.0):
            if Path(path) == canonical:
                yield False
                return
            with real(path, wait) as got:
                yield got
        with mock.patch.object(core, "file_lock", busy):
            for _ in range(5):
                self.capture()
        self.assertEqual(sorted(i["count"] for i in self.envelopes()), [1, 5])
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual([i["count"] for i in self.envelopes()], [6])

    def test_merging_spooled_reports_does_not_use_the_send_limit(self):
        import builtins
        self.capture()

        @contextlib.contextmanager
        def busy(*args, **kwargs):
            yield False
        with mock.patch.object(core, "file_lock", busy):
            for _ in range(60):
                self.capture()
        names = sorted(n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, Exception))
        for name in [n for n in names if n != "KeyError"][:50]:
            self.capture(exc_class=name)
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 50)

    def schedule(self):
        return bugs.BUGS / "outbox-v2-schedule.jsonl"

    def test_the_schedule_keeps_one_line_per_waiting_report_across_flushes(self):
        class Offline(FakeClient):
            def login(self):
                raise OSError("offline")
        for name in ("KeyError", "ValueError", "TypeError"):
            self.capture(exc_class=name)
        for n in range(10):
            bugs.flush(Offline(), now=NOW + 60 + n)
        self.assertEqual(len(self.schedule().read_text().splitlines()), 3)
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 3 * DAY)["sent"], 3)

    def test_a_pointer_whose_report_never_appears_is_retired(self):
        (bugs.BUGS / "outbox-v2").mkdir(parents=True)
        orphan = "e" * 64 + ".json"
        self.schedule().write_text(orphan + "\n")
        self.schedule().chmod(0o600)
        for n in range(30):  # more than a day of hourly syncs
            bugs.flush(FakeClient(), now=NOW + n)
        self.assertNotIn(orphan, self.schedule().read_text())

    def test_a_report_still_undelivered_thirty_days_after_its_last_occurrence_is_retired(self):
        self.capture()
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 31 * DAY)["sent"], 0)
        self.assertEqual(self.envelopes()[0]["state"], "expired")
        health = bugs.outbox_status(now=NOW + 31 * DAY)
        self.assertEqual((health["pending"], health["expired"]), (0, 1))
        self.assertNotIn(next((bugs.BUGS / "outbox-v2").glob("*.json")).name, self.schedule().read_text())

    def test_a_new_occurrence_revives_a_retired_report(self):
        self.capture()
        bugs.flush(FakeClient(), now=NOW + 31 * DAY)
        with mock.patch.object(bugs.time, "time", return_value=NOW + 32 * DAY):
            self.assertEqual(self.capture()["report_status"], "queued")
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 32 * DAY + 60)["sent"], 1)
        self.assertEqual([i["count"] for i in self.envelopes()], [2])

    def test_a_capture_while_the_schedule_is_compacted_is_kept(self):
        class Offline(FakeClient):
            def login(self):
                raise OSError("offline")
        self.capture()
        real, threads = bugs._compact_names, []

        def during(tail):
            t = threading.Thread(target=self.capture, kwargs={"exc_class": "ValueError"})
            t.start()
            t.join(0.3)  # it waits for the compaction's lock; without one it would append to the replaced file
            threads.append(t)
            return real(tail)
        with mock.patch.object(bugs, "_compact_names", side_effect=during):
            bugs.flush(Offline(), now=NOW + 60)
        for t in threads:
            t.join(5)
        self.assertEqual(len(threads), 1)
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 3 * DAY)["sent"], 2)

    def test_payload_tampering_and_symlink_queue_files_never_send(self):
        self.capture()
        path = next((bugs.BUGS / "outbox-v2").glob("*.json"))
        item = json.loads(path.read_text())
        item["payload"]["title"] = "private customer name"
        path.write_text(json.dumps(item))
        link = path.with_name("symlink.json")
        link.symlink_to(path)
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        self.assertEqual(client.created, [])

    def test_private_acknowledgment_persists_valid_ghsa_identifier(self):
        class Private(FakeClient):
            def private_report(self, title, body):
                super().private_report(title, body)
                return "GHSA-2345-6789-cfgh"
        self.capture(text="ｔｏｋｅｎ leak")
        client = Private()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual(self.envelopes()[0]["remote_id"], "GHSA-2345-6789-cfgh")
        self.assertEqual(client.created, [])

    def test_legacy_report_without_capture_binding_stays_quarantined(self):
        bugs.set_consent("on", "ana")
        box = bugs.BUGS / "outbox"
        box.mkdir(parents=True)
        path = box / "1.json"
        old = {"id": "1", "ts": NOW, "first_ts": NOW, "payload": _payload("report"), "held": False}
        path.write_text(json.dumps(old))
        client = FakeClient()
        bugs.flush(client, now=NOW + 9 * DAY)
        self.assertTrue(path.exists())
        self.assertEqual(client.created, [])
        self.assertEqual(json.loads(path.read_text())["state"], "quarantined")

    def test_storage_failure_preserves_command_result_and_reports_truthful_state(self):
        with mock.patch.object(bugs, "_write_json", return_value=False):
            record = self.capture()
        self.assertEqual(record.get("report_status"), "storage_unavailable")
        self.assertEqual(self.envelopes(), [])

    def test_status_exposes_pending_age_and_transport_state_without_raw_errors(self):
        class Offline(FakeClient):
            def login(self):
                raise OSError("private customer secret")
        with mock.patch.object(bugs.time, "time", return_value=NOW):
            self.capture()
        bugs.flush(Offline(), now=NOW + DAY)
        health = bugs.outbox_status(now=NOW + DAY)
        self.assertEqual(health["pending"], 1)
        self.assertEqual(health["oldest_pending_seconds"], DAY)
        self.assertEqual(health["blocked_reasons"], {"authentication": 1})
        self.assertNotIn("customer", json.dumps(health))

    def test_ten_thousand_occurrences_keep_one_bounded_envelope(self):
        for _ in range(10000):
            self.capture()
        records = self.envelopes()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["count"], 10000)
        self.assertLessEqual(len(json.dumps(records[0]).encode()), 16384)
        self.assertLessEqual(len(json.dumps(records[0]["payload"]).encode()), 8192)

    def test_capture_lock_waits_share_one_hundred_millisecond_budget(self):
        waits = []
        @contextlib.contextmanager
        def busy(path, wait):
            waits.append(wait)
            yield False
        with mock.patch.object(core, "file_lock", busy), \
                mock.patch.object(bugs.time, "monotonic", side_effect=[0, 0.06, 0.1, 0.1, 0.1]):
            result = self.capture()
        self.assertEqual(result["report_status"], "queued")
        self.assertLessEqual(sum(waits), 0.1)  # the report's own file and its overflow file share the budget

    def test_acknowledgment_write_failure_reconciles_without_reposting(self):
        class Server(FakeClient):
            def reconcile_report(self, intent):
                return 101 if self.created else None
        self.capture()
        client = Server()
        real = bugs._write_json
        def fail_ack(path, item, *args, **kwargs):
            return False if isinstance(item, dict) and item.get("state") == "acknowledged" else real(path, item, *args, **kwargs)
        with mock.patch.object(bugs, "_write_json", side_effect=fail_ack):
            self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 0)
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 1)
        self.assertEqual(len(client.created), 1)

    def test_flush_sends_at_most_fifty_due_envelopes(self):
        import builtins
        names = sorted(n for n, v in vars(builtins).items() if isinstance(v, type) and issubclass(v, Exception))
        for name in names[:52]:
            self.capture(exc_class=name)
        client = FakeClient()
        result = bugs.flush(client, now=NOW + 60)
        self.assertEqual(result["sent"], 50)
        self.assertEqual(len(client.created), 50)
        self.assertEqual(bugs.outbox_status()["pending"], 2)

    def test_missing_client_credentials_marks_durable_transport_failure(self):
        self.capture()
        with mock.patch.object(bugs, "default_client", side_effect=OSError("missing credentials")):
            bugs.sync(now=NOW + 60)
        self.assertEqual(self.envelopes()[0]["state"], "blocked_transport")
        self.assertEqual(self.envelopes()[0]["error_code"], "authentication")

    def test_reused_login_cannot_reconcile_another_account_uncertain_write(self):
        class Principal(FakeClient):
            account = "account:1"
            def principal(self):
                return self.account
            def reconcile_report(self, intent):
                return "GHSA-2345-6789-cfgh"
        self.capture(text="credential failure")
        client = Principal(private_fails=True)
        bugs.flush(client, now=NOW + 60)
        client.account = "account:2"
        self.assertEqual(bugs.flush(client, now=NOW + 2 * DAY)["sent"], 0)
        self.assertEqual(self.envelopes()[0]["error_code"], "principal_changed")

    def test_github_transport_pins_provider_and_retains_account_identity(self):
        calls = []
        def gh(args, **kwargs):
            calls.append(args)
            return 0, json.dumps({"login": "ana", "id": 123}), ""
        with mock.patch("devkit.gitx.GH", side_effect=gh):
            client = bugs.GhClient()
            self.assertEqual(client.login(), "ana")
            self.assertEqual(client.principal(), "github.com:123")
        self.assertEqual(calls[0][:3], ["api", "--hostname", "github.com"])

    def test_private_reconciliation_stops_at_a_finite_page_budget(self):
        calls = []
        def gh(args, **kwargs):
            calls.append((args, kwargs))
            return 0, json.dumps([{"description": "other report"}] * 100), ""
        with mock.patch("devkit.gitx.GH", side_effect=gh):
            with self.assertRaisesRegex(RuntimeError, "page budget"):
                bugs.GhClient().reconcile_report({"destination": "private", "marker": "missing"})
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(0 < kwargs["timeout"] <= 10 for _, kwargs in calls))

    def test_public_reconciliation_reads_the_next_bounded_page(self):
        pages = [[{"body": "other"}] * 100, [{"body": "report-marker"}]]
        with mock.patch("devkit.gitx.GH", side_effect=lambda *a, **k: (0, json.dumps(pages.pop(0)), "")):
            result = bugs.GhClient().reconcile_report({"destination": "public", "marker": "report-marker",
                                                     "issue_number": 7})
        self.assertEqual(result, 7)

    def test_capture_preserves_an_unreadable_previous_envelope(self):
        self.capture()
        path = next((bugs.BUGS / "outbox-v2").glob("*.json"))
        path.write_text("corrupt retained envelope")
        self.capture()
        self.assertEqual(path.read_text(), "corrupt retained envelope")
        self.assertEqual(len(list((bugs.BUGS / "outbox-v2").glob("*.json"))), 2)

    def test_delivery_prepares_released_symbols_for_later_foreground_capture(self):
        with mock.patch.object(bugs, "install_channel", return_value="release"):
            bugs.flush(FakeClient(), now=NOW)
            self.capture(frames=("bugs.handle_crash", "bugs.private_customer_function"))
        self.assertEqual(self.envelopes()[0]["payload"]["frames"], ["bugs.handle_crash", "<local>"])

    def test_bounded_scan_resumes_fairly_past_deferred_reports(self):
        for n in range(251):
            with mock.patch.object(core, "VERSION", f"26.11.{n}"):
                self.capture()
        for path in (bugs.BUGS / "outbox-v2").glob("*.json"):
            item = json.loads(path.read_text())
            if item["payload"]["version"] != "26.11.250":
                item["next_try"] = NOW + 30 * DAY
                path.write_text(json.dumps(item))
        client = FakeClient()
        first = bugs.flush(client, now=NOW + 60)
        self.assertEqual(first.get("examined"), 250)
        self.assertEqual(first["sent"], 0)
        second = bugs.flush(client, now=NOW + 120)
        self.assertLessEqual(second["examined"], 250)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(bugs.outbox_status()["pending"], 250)

    def test_concurrent_capture_retains_every_occurrence(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            records = list(pool.map(lambda n: self.capture(exc_class="KeyError" if n % 2 else "ValueError"), range(100)))
        self.assertTrue(all(r["report_status"] in ("queued", "coalesced") for r in records))
        self.assertEqual(sum(r["count"] for r in self.envelopes()), 100)

    def test_oversized_raw_classification_is_private_without_scanning_unbounded_text(self):
        with mock.patch.object(bugs, "_fold", side_effect=AssertionError("unbounded classification")):
            record = self.capture(text="x" * 100000)
        self.assertEqual(record["report_status"], "queued")
        self.assertEqual(self.envelopes()[0]["destination"], "private")

    def test_pointer_seen_before_envelope_creation_is_retried(self):
        real = bugs._write_json
        client = FakeClient()
        def interleaved(path, item, *args, **kwargs):
            if path.parent == bugs.BUGS / "outbox-v2":
                bugs.flush(client, now=NOW + 60)
            return real(path, item, *args, **kwargs)
        with mock.patch.object(bugs, "_write_json", side_effect=interleaved):
            self.capture()
        self.assertEqual(client.created, [])
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 1)

    def test_requeue_survives_cursor_write_failure_without_duplicate_send(self):
        self.capture()
        real = bugs._write_json
        class Offline(FakeClient):
            def login(self):
                raise OSError("offline")
        def fail_cursor(path, item, *args, **kwargs):
            return False if path.name == "outbox-v2-cursor.json" else real(path, item, *args, **kwargs)
        with mock.patch.object(bugs, "_write_json", side_effect=fail_cursor):
            bugs.flush(Offline(), now=NOW + 60)
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 2 * DAY)["sent"], 1)
        self.assertEqual(len(client.created), 1)

    def test_malformed_schedule_pointer_does_not_starve_a_later_valid_report(self):
        bugs.BUGS.mkdir(parents=True)
        schedule = bugs.BUGS / "outbox-v2-schedule.jsonl"
        schedule.write_text("../private-path\n")
        schedule.chmod(0o600)
        self.capture()
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual(len(client.created), 1)

    def test_capture_during_transport_is_retained_without_reposting(self):
        from concurrent.futures import ThreadPoolExecutor
        started, release = threading.Event(), threading.Event()
        class PausedServer(FakeClient):
            def create_issue(self, title, body):
                started.set()
                if not release.wait(5):
                    raise AssertionError("capture did not release fake server")
                return super().create_issue(title, body)
        self.capture()
        client = PausedServer()
        with ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(bugs.flush, client, NOW + 60)
            try:
                self.assertTrue(started.wait(5))
                self.assertEqual(self.capture()["report_status"], "queued")
            finally:
                release.set()
            self.assertEqual(sent.result()["sent"], 1)
        bugs.flush(client, now=NOW + 120)
        self.assertEqual(sum(r["count"] for r in self.envelopes()), 2)
        self.assertEqual(len(client.created), 1)

    def test_copied_valid_binding_under_another_queue_key_cannot_send(self):
        self.capture()
        path = next((bugs.BUGS / "outbox-v2").glob("*.json"))
        copied = path.with_name("f" * 64 + ".json")
        copied.write_bytes(path.read_bytes())
        copied.chmod(0o600)
        bugs._schedule_report(copied.name)
        path.unlink()
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 0)
        self.assertEqual(json.loads(copied.read_text())["state"], "quarantined")


class LocalFileTests(StateCase):
    def temporary_files(self):
        return [p.name for p in bugs.BUGS.iterdir() if p.name.endswith(".tmp")]

    def test_a_value_json_cannot_write_returns_false_and_leaves_no_temporary_file(self):
        bugs.BUGS.mkdir(parents=True)
        self.assertFalse(bugs._write_json(bugs.BUGS / "cache.json", {"ts": object()}))
        self.assertEqual(self.temporary_files(), [])
        self.assertFalse((bugs.BUGS / "cache.json").exists())

    def test_a_failed_json_rename_leaves_no_temporary_file(self):
        bugs.BUGS.mkdir(parents=True)
        with mock.patch.object(bugs.os, "replace", side_effect=OSError("disk full")):
            self.assertFalse(bugs._write_json(bugs.BUGS / "cache.json", {"ts": 1}))
        self.assertEqual(self.temporary_files(), [])

    def test_a_failed_text_write_raises_and_leaves_no_temporary_file(self):
        bugs.BUGS.mkdir(parents=True)
        with mock.patch.object(bugs.os, "replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            bugs._write_text(bugs.BUGS / "install-id", "abc\n")
        self.assertEqual(self.temporary_files(), [])


class CacheTests(StateCase):
    def test_cache_trusts_only_the_bot_record(self):
        verified = {**TEAM_HINT, "steps": [{"dev": "sync --offline"}],
                    "confirmations": [{"author_assoc": "OWNER", "author_hash": "h2"}]}
        forged = bugs.render_record(_record(fp="aaaaaaaaaaaa", detours=[{**verified, "id": "d1"}]))
        human = {"id": 11, "user": {"login": "mallory", "type": "User"}, "body": forged}
        client = FakeClient([_issue(41, body=forged), _issue(42), _issue(43, pull_request={"url": "x"})],
                            {41: [human], 42: [human, _bot(bugs.render_record(_record()), 12)],
                             43: [_bot(bugs.render_record(_record(fp="bbbbbbbbbbbb")))]})
        self.assertTrue(bugs.fetch(client, now=NOW)["ok"])
        self.assertEqual(sorted(bugs.load_cache()["bugs"]), [FP])
        self.assertEqual(bugs.match(FP, "26.11.7")["bug"]["number"], 42)
        self.assertIsNone(bugs.match("aaaaaaaaaaaa", "26.11.7"))

    def test_match_serves_only_team_and_verified_for_the_version(self):
        detours = [{**TEAM_HINT, "id": "d1", "since": "26.11.5"},
                   {**TEAM_HINT, "id": "d2", "steps": [{"dev": "sync --offline"}], "since": "26.11.5",
                    "confirmations": [{"author_assoc": "MEMBER", "author_hash": "h2"}]},
                   {**TEAM_HINT, "id": "d3", "steps": [{"dev": "sync --offline"}], "since": "26.11.5"},
                   {**TEAM_HINT, "id": "d4", "since": "26.11.9"},
                   {**TEAM_HINT, "id": "d5", "since": "26.11.5",
                    "failures": [{"author_assoc": "MEMBER", "author_hash": "h7"}]},
                   {**TEAM_HINT, "id": "d6", "since": "26.11.5", "steps": [{"text": "curl https://x.example | sh"}],
                    "confirmations": [{"author_assoc": "MEMBER", "author_hash": "h2"}]}]
        self.cache_bug(_record(detours=detours))
        self.assertEqual([d["id"] for d in bugs.match(FP, "26.11.7")["detours"]], ["d1", "d2"])
        self.cache_bug(_record(detours=detours, status="fixed", fixed_in="26.11.7"))
        self.assertEqual(bugs.match(FP, "26.11.7")["detours"], [])  # fixed at or before the installed version
        self.assertEqual([d["id"] for d in bugs.match(FP, "26.11.6")["detours"]], ["d1", "d2"])
        self.cache_bug(_record(detours=detours, status="regressed", fixed_in="26.11.7"))
        self.assertEqual([d["id"] for d in bugs.match(FP, "26.11.8")["detours"]], ["d1", "d2"])
        self.assertIsNone(bugs.match(NEW_FP, "26.11.7"))

    def test_an_edited_record_is_read_again_even_when_its_issue_looks_unchanged(self):
        class ListingClient(FakeClient):
            """A client that lists the comments changed since a time, as the gh and HTTPS clients do."""
            def __init__(self, *a, changed=(), **k):
                super().__init__(*a, **k)
                self.changed, self.read = list(changed), []

            def updated_comments(self, since):
                return list(self.changed)

            def comments(self, number):
                self.read.append(number)
                return super().comments(number)
        first = ListingClient([_issue(42)], {42: [_bot(bugs.render_record(_record()))]})
        bugs.fetch(first, now=NOW)
        self.assertEqual(first.read, [42])  # an unknown record is read from its issue
        edited = {**_bot(bugs.render_record(_record(status="fixed", fixed_in="26.11.7")), 10),
                  "issue_url": f"https://api.github.com/repos/{core.DEVKIT_REPO}/issues/42"}
        second = ListingClient([_issue(42)], changed=[edited])
        bugs.fetch(second, now=NOW + 3600)
        self.assertEqual(second.read, [])  # one listing replaced a read per issue
        self.assertEqual(bugs.match(FP, "26.11.7")["bug"]["fixed_in"], "26.11.7")

    def test_the_cache_keeps_at_most_25_bugs_opened_by_one_account(self):
        issues, comments = [], {}
        for n in range(30):
            fp = f"{n:012x}"
            issues.append(_issue(n + 1, opener="flooder"))
            comments[n + 1] = [_bot(bugs.render_record(_record(fp=fp, score=float(n))))]
        issues.append(_issue(99, opener="ana"))
        comments[99] = [_bot(bugs.render_record(_record(fp="ffffffffffff")))]
        bugs.fetch(FakeClient(issues, comments), now=NOW)
        cached = bugs.load_cache()["bugs"]
        self.assertEqual(sum(1 for e in cached.values() if e["opener"] == "flooder"), 25)
        self.assertIn("ffffffffffff", cached)


def _held(payload, **extra):
    """Queue an item while sharing is off, then share again: it waits for the developer's approval of its payload."""
    bugs.set_consent("off", "")
    try:
        return bugs.queue({"payload": payload, **extra})
    finally:
        bugs.set_consent("on", "ana")


class OutboxTests(StateCase):
    def setUp(self):
        super().setUp()
        bugs.set_consent("on", "ana")

    def test_optional_legacy_delivery_respects_the_same_run_budget(self):
        for _ in range(60):
            bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 50)
        self.assertEqual(len(list((bugs.BUGS / "outbox").glob("*.json"))), 10)

    def test_legacy_delivery_resumes_after_250_deferred_reports(self):
        for _ in range(250):
            item = bugs.queue({"payload": _payload("report", fp=NEW_FP)})
            self.edit_item(item, lambda it: it.update(next_try=NOW + 3600))
        bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        original = os.scandir
        @contextlib.contextmanager
        def ordered(path):
            with original(path) as entries:
                yield iter(sorted(entries, key=lambda entry: int(entry.name[:-5]) if entry.name[:-5].isdigit() else 0))
        with mock.patch.object(bugs.os, "scandir", ordered):
            self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["sent"], 0)
            self.assertEqual(bugs.flush(FakeClient(), now=NOW + 120)["sent"], 1)
            bugs.queue({"payload": _payload("report", fp=NEW_FP)})
            self.assertEqual(bugs.flush(FakeClient(), now=NOW + 120)["sent"], 0)
            self.assertEqual(bugs.flush(FakeClient(), now=NOW + 120)["sent"], 1)
        self.assertEqual(len(list((bugs.BUGS / "outbox").glob("*.json"))), 250)

    def test_legacy_stale_sequence_admits_contiguous_overflow(self):
        for _ in range(250):
            item = bugs.queue({"payload": _payload("report", fp=NEW_FP)})
            self.edit_item(item, lambda it: it.update(next_try=NOW + 3600))
        bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        bugs._write_json(bugs.BUGS / "outbox" / ".seq", 250)
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["sent"], 0)
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["sent"], 1)

    def test_legacy_report_budget_without_sequence_metadata(self):
        for _ in range(60):
            bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        (bugs.BUGS / "outbox" / ".seq").unlink()
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["sent"], 50)

    def test_legacy_missing_sequence_recovers_later_ids_without_losing_deferred_reports(self):
        for _ in range(250):
            item = bugs.queue({"payload": _payload("report", fp=NEW_FP)})
            self.edit_item(item, lambda it: it.update(next_try=NOW + 3600))
        bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        (bugs.BUGS / "outbox" / ".seq").write_text("corrupt")
        sends = [bugs.flush(FakeClient(), now=NOW + 60)["sent"] for _ in range(3)]
        self.assertEqual(sends, [0, 0, 1])
        self.assertEqual(len(list((bugs.BUGS / "outbox").glob("*.json"))), 250)

    def test_legacy_scan_skips_malformed_and_deferred_items_within_its_budget(self):
        for _ in range(60):
            item = bugs.queue({"payload": _payload("report", fp=NEW_FP)})
            self.edit_item(item, lambda it: it.update(next_try=NOW + 3600))
        (bugs.BUGS / "outbox" / "malformed.json").write_text("{")
        for _ in range(60):
            bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["sent"], 50)
        self.assertEqual(len(list((bugs.BUGS / "outbox").glob("*.json"))), 71)

    def test_legacy_sender_bounds_record_reads_with_many_malformed_entries(self):
        box = bugs.BUGS / "outbox"
        box.mkdir(parents=True, exist_ok=True)
        for index in range(1, 301):
            (box / f"{index}.json").write_text("{")
        with mock.patch.object(bugs, "_load_item", wraps=bugs._load_item) as loaded:
            self.assertEqual(bugs._flush_legacy(FakeClient(), now=NOW)["sent"], 0)
        self.assertEqual(loaded.call_count, 250)
        self.assertEqual(len(list(box.glob("*.json"))), 300)

    def test_flush_rechecks_items(self):
        self.cache_bug()
        edited = bugs.queue({"payload": _payload("detour")})
        self.edit_item(edited, lambda it: it["payload"].update(cwd="/Users/ana/acme"))
        changed = _held(_payload("detour", steps=[{"text": "Run the step twice."}]))
        good = _held(_payload("detour", steps=[{"text": "Wait a minute, then run it again."}]))
        self.assertEqual(sorted(i["id"] for i in bugs.pending()), sorted([changed, good]))
        bugs.approve(changed)
        bugs.approve(good)
        self.assertEqual({i["id"]: i["approved"] for i in bugs.pending()}, {changed: True, good: True})
        self.edit_item(changed, lambda it: it["payload"]["steps"][0].update(text="Run it three times."))
        self.assertEqual({i["id"]: i["approved"] for i in bugs.pending()}, {changed: False, good: True})
        client = FakeClient()
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual({k: counts[k] for k in ("sent", "held", "skipped")}, {"sent": 1, "held": 1, "skipped": 1})
        self.assertEqual([(n, _sent(b)["steps"]) for n, b in client.posted],
                         [(42, [{"text": "Wait a minute, then run it again."}])])

    def test_a_private_word_or_a_secret_in_any_field_holds_the_item(self):
        # not only free text: a valid exception class made from a project's name, a project named like a devkit
        # command, a token where a class name goes; each waits in `dev bug pending`, its problem naming the field
        token = "ghp_" + "A1b2C3d4E5f6" * 3
        made = (("aaaaaaaaaaaa", {"command": "", "exc_class": "PaymentsCoreError"}, "exc_class"),
                ("bbbbbbbbbbbb", {"command": "progress step"}, "command"),
                ("cccccccccccc", {"command": "", "exc_class": token}, "exc_class"))
        items = {bugs.queue({"payload": _payload("report", fp=fp, **fields)}): field for fp, fields, field in made}
        client = FakeClient()
        with mock.patch.object(bugs, "deny_words", lambda: frozenset({"acme", "payments-core", "progress"})):
            counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((client.created, client.posted, counts["sent"]), ([], [], 0))
        pending = {i["id"]: i.get("problem") or "" for i in bugs.pending()}
        self.assertEqual(set(pending), set(items))
        for item_id, field in items.items():
            self.assertIn(field, pending[item_id])
            self.assertNotIn("A1b2C3d4", pending[item_id])

    def test_the_sender_reads_the_need_for_review_from_the_item_not_its_held_flag(self):
        # an outbox file rewritten with held false: its free text still waits for the developer's approval
        payload = {**_payload("report", fp=NEW_FP, title="dev status breaks in the billing monorepo"), "kind": "command"}
        item = _held(payload)
        self.edit_item(item, lambda it: it.update(held=False))
        client = FakeClient()
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((client.created, client.posted, counts["held"]), ([], [], 1))
        self.assertEqual([i["id"] for i in bugs.pending()], [item])
        bugs.approve(item)
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 1)  # the developer's approval sends it

    def test_flush_refuses_another_login(self):
        self.cache_bug()
        bugs.queue({"payload": _payload("detour")})
        client = FakeClient(login="bob")
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((client.posted, client.created), ([], []))
        self.assertIn("ana", counts["error"])
        self.assertEqual(len(list((bugs.BUGS / "outbox").glob("*.json"))), 1)

    def test_nothing_is_sent_without_consent(self):
        bugs.set_consent("off", "ana")
        bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["held"], 1)
        self.assertEqual(client.created, [])

    def test_security_items_never_reach_issues(self):
        item = _held(_payload("report", fp=NEW_FP, frames=["guard.check_git"]))
        self.assertTrue(bugs.pending()[0]["security"])
        bugs.approve(item)
        failing = FakeClient(private_fails=True)
        self.assertEqual(bugs.flush(failing, now=NOW + 60)["held"], 1)
        self.assertEqual((failing.created, failing.posted), ([], []))
        self.assertEqual([i["id"] for i in bugs.pending()], [item])  # still waiting for the developer
        ok = FakeClient()
        self.assertEqual(bugs.flush(ok, now=NOW + 60)["sent"], 1)
        self.assertEqual((len(ok.private), ok.created, ok.posted), (1, [], []))
        self.assertEqual(bugs.pending(), [])

    def test_offline_keeps_cache_and_outbox(self):
        self.cache_bug()
        before = (bugs.BUGS / "cache.json").read_bytes()
        bugs.queue({"payload": _payload("vote")})
        later = NOW + 2 * 3600  # past the random delay of up to an hour
        result = bugs.sync(FakeClient(fail=True), now=later)
        self.assertFalse(result["fetch"]["ok"])
        self.assertIn("unreachable", result["fetch"]["error"])
        self.assertEqual((bugs.BUGS / "cache.json").read_bytes(), before)
        self.assertEqual(result["flush"]["failed"], 1)
        item = json.loads(next((bugs.BUGS / "outbox").glob("*.json")).read_text())
        self.assertEqual(item["attempts"], 1)
        self.assertGreater(item["next_try"], later)
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=item["next_try"] + 1)["sent"], 1)  # the next good sync sends it
        self.assertEqual([n for n, _ in client.posted], [42])

    def test_votes_go_out_once_per_issue_per_day(self):
        self.cache_bug()
        for impact in ("minor", "blocked", "detoured"):
            bugs.queue({"payload": _payload("vote", impact=impact)})
        client = FakeClient()
        day = (int((NOW + 2 * 3600) // DAY) + 1) * DAY + 12 * 3600  # noon UTC, past the random delay
        self.assertEqual(bugs.flush(client, now=day)["sent"], 1)
        self.assertEqual([(n, _sent(b)["impact"]) for n, b in client.posted], [(42, "blocked")])
        bugs.queue({"payload": _payload("vote", impact="detoured")})
        bugs.flush(client, now=day + 60)
        self.assertEqual(len(client.posted), 1)  # this machine already voted on #42 today
        bugs.queue({"payload": _payload("vote", impact="detoured")})
        bugs.flush(client, now=day + DAY)
        self.assertEqual(len(client.posted), 2)

    def test_a_new_bug_is_filed_once(self):
        client = FakeClient()
        bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual(len(client.created), 1)
        title, body = client.created[0]
        self.assertEqual(_sent(body)["fingerprint"], NEW_FP)
        self.assertEqual(title, bugs.title_for(_payload("report", fp=NEW_FP)))
        bugs.queue({"payload": _payload("report", fp=NEW_FP, impact="detoured")})
        bugs.flush(client, now=NOW + 120)
        self.assertEqual(len(client.created), 1)  # the second report goes to the issue this machine filed
        self.assertEqual([n for n, _ in client.posted], [101])

    def test_a_new_bug_opens_an_issue_people_can_read(self):
        # the title from the title table with no [bug] prefix, and the body the workflow writes after its first fold,
        # with the report in its one data block
        client = FakeClient()
        p = _payload("report", fp=NEW_FP)
        bugs.queue({"payload": p})
        bugs.flush(client, now=NOW + 60)
        (title, body), = client.created
        self.assertEqual(title, bugs.title_for(p))
        self.assertFalse(title.startswith("[bug]"))
        self.assertEqual(_sent(body), p)
        self.assertIn("\n- **Status:** Open. Not triaged yet.\n", body)
        self.assertIn("\n- **Impact:** Blocks work for everyone who hit it (1 person).\n", body)
        self.assertIn("Safe failure metadata is reported automatically", body)

    def test_each_comment_opens_with_its_plain_line(self):
        self.cache_bug()
        bugs.queue({"payload": _payload("vote")})
        bugs.queue({"payload": _payload("detour")})
        client = FakeClient()
        bugs.flush(client, now=NOW + 2 * 3600)
        self.assertEqual(sorted(_sent(b)["type"] for _, b in client.posted), ["detour", "vote"])
        for _, body in client.posted:
            self.assertEqual(body, bugs.render_comment(_sent(body)))
            self.assertTrue(body.startswith(("**devkit vote** · blocked · devkit 26.11.7 · ",
                                             "**Workaround proposed** for devkit 26.11.7 and later: ")), body)

    def test_a_private_report_reads_like_a_public_issue(self):
        p = _payload("report", fp=NEW_FP, frames=["guard.check_git"])
        bugs.approve(bugs.queue({"payload": p}))
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        (title, body), = client.private
        self.assertEqual(title, bugs.title_for(p))
        self.assertEqual(_sent(body), p)
        self.assertIn("\n- **Status:** Open. Not triaged yet.\n", body)

    def test_a_vote_carries_no_count_of_how_often(self):
        # how often a bug hit this Mac in a day is deferred: no vote this client sends carries it, and the schema
        # every client and the workflow share refuses one that does
        self.cache_bug()
        bugs.queue({"payload": _payload("vote")})
        client = FakeClient()
        bugs.flush(client, now=NOW + 2 * 3600)
        self.assertNotIn("count", _sent(client.posted[0][1]))
        self.assertEqual(bugs.validate_payload(_payload("vote", count="2 to 9")), ["unknown field count"])

    def test_a_new_report_goes_only_to_an_issue_the_registry_labeled(self):
        class Searching(FakeClient):
            def __init__(self, hits):
                super().__init__()
                self.hits = hits

            def find(self, fp):
                return self.hits
        label = [{"name": "devkit-bug"}]
        quoting = Searching([3, {"number": 4, "labels": []}, {"number": 5, "labels": label, "pull_request": {}},
                             {"number": 6, "labels": [["devkit-bug"]]}])  # issues that only quote the fingerprint
        bugs.queue({"payload": _payload("report", fp=NEW_FP)})
        self.assertEqual(bugs.flush(quoting, now=NOW + 60)["sent"], 1)
        self.assertEqual((len(quoting.created), quoting.posted), (1, []))
        registry = Searching([{"number": 4, "labels": []}, {"number": 9, "labels": label}])
        bugs.queue({"payload": _payload("report", fp="0a0b0c0d0e0f")})
        bugs.flush(registry, now=NOW + 120)
        self.assertEqual(([n for n, _ in registry.posted], registry.created), ([9], []))

    def test_the_gh_search_asks_only_for_registry_issues(self):
        asked = []

        def gh(args, timeout=60):
            asked.append(args)
            return 0, json.dumps({"items": [{"number": 4, "labels": []},
                                            {"number": 9, "labels": [{"name": "devkit-bug"}]}]}), ""
        with mock.patch("devkit.gitx.GH", gh):
            found = bugs.GhClient("owner/registry").find(NEW_FP)
        self.assertEqual([i["number"] for i in found], [9])
        self.assertIn(f"q=repo:owner/registry is:issue label:devkit-bug in:body {NEW_FP}", asked[0])

    def test_an_old_item_that_never_went_out_is_retained_after_7_days(self):
        bugs.queue({"payload": _payload("detour")})  # no issue is known for it, so it cannot go anywhere
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["skipped"], 1)
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 8 * DAY)["skipped"], 1)
        self.assertEqual(len(list((bugs.BUGS / "outbox").glob("*.json"))), 1)

    def test_drop_removes_a_held_item(self):
        item = _held(_payload("report", fp=NEW_FP, title="It fails on the second run"))
        self.assertEqual([i["id"] for i in bugs.pending()], [item])
        bugs.drop(item)
        self.assertEqual(bugs.pending(), [])
        with self.assertRaises(KeyError):
            bugs.approve(item)


TITLE = "dev status prints nothing after a fresh clone"


class FreeTextDeliveryTests(StateCase):
    # With bugs.sync on and the developer's consent covering it, a report or a detour written in an agent's own words
    # goes out like any other item: cleaned when it is filed, re-checked when it is sent, and never held for review.

    def report(self, fp=NEW_FP, title=TITLE, **fields):
        return bugs.queue({"payload": {**_payload("report", fp=fp, title=title), "kind": "command", **fields}})

    def test_only_the_text_devkit_cleaned_at_filing_goes_out_without_review(self):
        bugs.set_consent("on", "ana")
        sent = self.report()
        changed = self.report(fp="0a0b0c0d0e0f", title="dev status prints nothing in a new checkout")
        self.edit_item(changed, lambda it: it["payload"].update(title="dev status prints nothing in a new clone"))
        client = FakeClient()
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((counts["sent"], counts["held"]), (1, 1))
        self.assertEqual([_sent(body).get("title") for _, body in client.created], [TITLE])
        self.assertFalse((bugs.BUGS / "outbox" / f"{sent}.json").exists())
        self.assertEqual([i["id"] for i in bugs.pending()], [changed])  # edited after filing: the developer decides

    def test_a_detour_in_an_agents_words_goes_out_under_reports_consent(self):
        bugs.set_consent("reports", "ana")
        self.cache_bug()
        bugs.queue({"payload": _payload("detour", steps=[{"text": "Wait a minute, then run it again."}])})
        self.assertEqual(bugs.pending(), [])
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual([(n, _sent(b)["steps"]) for n, b in client.posted],
                         [(42, [{"text": "Wait a minute, then run it again."}])])

    def test_consent_and_bugs_sync_at_filing_decide_whether_free_text_waits(self):
        waiting = []
        for n, (share, sync) in enumerate(((None, True), ("off", True), ("on", False))):
            (bugs.BUGS / "consent.json").unlink(missing_ok=True)
            if share:
                bugs.set_consent(share, "ana")
            with mock.patch.dict(core.CFG, {"bugs": {"sync": sync}}):
                waiting.append(self.report(fp=f"{n:012x}", title=f"{TITLE}, case {n}"))
        bugs.set_consent("on", "ana")
        automatic = self.report()
        self.assertEqual([i["id"] for i in bugs.pending()], waiting)  # consent given later releases none of them
        client = FakeClient()
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((counts["sent"], counts["held"]), (1, 3))
        self.assertEqual([_sent(body)["fingerprint"] for _, body in client.created], [NEW_FP])
        self.assertFalse((bugs.BUGS / "outbox" / f"{automatic}.json").exists())

    def test_a_report_with_the_title_of_one_already_queued_adds_nothing(self):
        bugs.set_consent("on", "ana")
        first = bugs.file_report(title=TITLE, session="s1")
        again = bugs.file_report(title="  Dev status prints nothing after a fresh  clone ", session="s2")
        self.assertEqual((again["id"], again["fingerprint"], again.get("duplicate")),
                         (first["id"], first["fingerprint"], True))
        self.assertEqual(len(bugs.occurrences()), 1)  # nothing recorded for it, so no automatic report opens an issue
        self.assertEqual(self.report(fp="0a0b0c0d0e0f"), first["id"])  # two filings at once: the queue drops it too
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        self.assertEqual([_sent(b)["title"] for _, b in client.created if _sent(b).get("title")], [TITLE])

    def test_a_security_report_with_the_title_of_a_queued_public_one_still_goes_to_private_reporting(self):
        bugs.set_consent("on", "ana")
        public = bugs.file_report(title=TITLE, session="s1")
        private = bugs.file_report(title=TITLE, session="s2", security=True)
        self.assertEqual((private["duplicate"], private["security"]), (False, True))
        self.assertNotEqual(private["id"], public["id"])
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        titled = [[_sent(b)["title"] for _, b in sent if _sent(b).get("title")]
                  for sent in (client.created, client.private)]
        self.assertEqual(titled, [[TITLE], [TITLE]])  # one public issue and one private report, besides the automatic

    def test_a_public_report_with_the_title_of_a_queued_private_one_is_dropped(self):
        # a title an agent just filed as a security matter never goes out as a public issue beside it
        bugs.set_consent("on", "ana")
        private = bugs.file_report(title=TITLE, session="s1", security=True)
        public = bugs.file_report(title=TITLE, session="s2")
        self.assertEqual((public["duplicate"], public["id"]), (True, private["id"]))
        client = FakeClient()
        bugs.flush(client, now=NOW + 60)
        titled = [[_sent(b)["title"] for _, b in sent if _sent(b).get("title")]
                  for sent in (client.created, client.private)]
        self.assertEqual(titled, [[], [TITLE]])

    def test_after_sharing_is_turned_off_no_item_asks_for_consent_to_the_new_text(self):
        bugs.set_consent("on", "ana")
        marked = self.report()
        bugs.set_consent("off", "ana")
        self.assertEqual([(i["id"], i["consent"]) for i in bugs.pending()], [(marked, False)])
        self.assertNotIn("consent", bugs.status_line(NOW)["text"])

    def test_a_copied_outbox_file_is_sent_once(self):
        bugs.set_consent("on", "ana")
        sent = self.report()
        box = bugs.BUGS / "outbox"
        copy = str(int(sent) + 1)
        (box / f"{copy}.json").write_text((box / f"{sent}.json").read_text())  # the same item again, its id included
        bugs._write_json(box / ".seq", int(copy))
        client = FakeClient()
        self.assertEqual(sum(bugs.flush(client, now=NOW + 60 * i)["sent"] for i in range(1, 4)), 1)
        self.assertEqual([i["id"] for i in bugs.pending()], [copy])  # the copy waits for the developer

    def test_free_text_filed_under_an_older_consent_waits_and_goes_once_the_developer_agrees_to_the_new_text(self):
        # consent given to a text that said nothing about sending free text without review covers none of it: the
        # report and the security report stay queued, never lost, and go once the developer agrees to the new text
        bugs._write_json(bugs.BUGS / "consent.json", {"login": "ana", "share": "on", "ts": NOW})  # before versions
        worded = self.report()
        private = bugs.queue({"payload": _payload("report", fp="0a0b0c0d0e0f", frames=["guard.check_git"])})
        client = FakeClient()
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((counts["sent"], counts["held"], client.created, client.private), (0, 2, [], []))
        self.assertEqual([i["id"] for i in bugs.pending()], [worded, private])
        bugs.set_consent("on", "ana")
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 2)
        self.assertEqual(([_sent(b)["title"] for _, b in client.created], len(client.private)), ([TITLE], 1))

    def test_an_item_queued_under_the_held_contract_stays_held_until_the_developer_sends_it(self):
        bugs.set_consent("on", "ana")
        box = bugs.BUGS / "outbox"
        box.mkdir(parents=True, exist_ok=True)
        old = {"id": "7", "payload": {**_payload("report", fp=NEW_FP, title=TITLE), "kind": "command"},
               "legacy_version": 2, "ts": NOW, "first_ts": NOW, "attempts": 0, "next_try": 0, "security": False,
               "held": True, "not_before": 0}  # as the previous version queued free text
        (box / "7.json").write_text(json.dumps(old))
        fresh = bugs._load_item(self.report(fp="0a0b0c0d0e0f", title="dev status prints nothing at all"))
        forged = str(int(fresh["id"]) + 1)  # the old item again, with the marks of the newer one copied in
        (box / f"{forged}.json").write_text(json.dumps({**old, **{k: v for k, v in fresh.items() if k not in old},
                                                        "held": False, "id": forged}))
        client = FakeClient()
        counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual((counts["sent"], counts["held"]), (1, 2))
        self.assertEqual([_sent(b)["title"] for _, b in client.created], ["dev status prints nothing at all"])
        self.assertEqual([i["id"] for i in bugs.pending()], ["7", forged])
        line = bugs.status_line(NOW + 60)["text"]
        self.assertIn("2 items wait for your review", line)
        self.assertIn("`dev bug send <id>` sends one", line)
        bugs.approve("7")
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 1)

    def test_a_problem_the_recheck_cannot_remove_keeps_the_item_held(self):
        bugs.set_consent("on", "ana")
        self.report()
        private = self.report(fp="0a0b0c0d0e0f", title="dev status breaks in the billing monorepo")
        client = FakeClient()
        with mock.patch.object(bugs, "deny_words", lambda: frozenset({"acme", "billing"})):
            counts = bugs.flush(client, now=NOW + 60)
        self.assertEqual(counts["sent"], 1)
        self.assertEqual([_sent(b)["fingerprint"] for _, b in client.created], [NEW_FP])
        (held,) = bugs.pending()
        self.assertEqual(held["id"], private)
        self.assertIn("title", held["problem"])
        self.assertEqual(bugs.flush(client, now=NOW + 120)["sent"], 0)  # still the developer's, the word or not

    def test_a_security_item_goes_at_once_to_private_reporting_only(self):
        bugs.set_consent("on", "ana")
        bugs.queue({"payload": _payload("report", fp=NEW_FP, frames=["guard.check_git"])})
        self.assertEqual(bugs.pending(), [])
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 1)
        self.assertEqual((len(client.private), client.created, client.posted), (1, [], []))
        refused = self.report(fp="0a0b0c0d0e0f", title="dev status prints the token of a session")
        closed = FakeClient(private_fails=True)  # private reporting off, or a private repository
        self.assertEqual(bugs.flush(closed, now=NOW + 120)["held"], 1)
        self.assertEqual((closed.created, closed.posted, closed.private), ([], [], []))
        self.assertEqual(bugs.flush(client, now=NOW + 180)["sent"], 0)  # it waits for the developer, as before
        (held,) = bugs.pending()
        self.assertEqual((held["id"], held["security"]), (refused, True))
        bugs.approve(refused)
        self.assertEqual(bugs.flush(client, now=NOW + 240)["sent"], 1)
        self.assertEqual((len(client.private), client.created, client.posted), (2, [], []))


class BackgroundSendTests(StateCase):
    # Filing an item that goes out without review starts the send at once, in the background, as the hourly sync
    # would run it: a detached `dev bug sync` of the pinned release, one at a time.

    def setUp(self):
        super().setUp()
        self.started = []
        state = mock.patch.object(core, "STATE", bugs.BUGS.parent)  # the outbox the background sync sends
        state.start()
        self.addCleanup(state.stop)
        popen = mock.patch("subprocess.Popen", self.popen)
        popen.start()
        self.addCleanup(popen.stop)

    def popen(self, argv, **kw):
        self.started.append((list(argv), kw))
        return mock.Mock(pid=os.getpid())  # still running while the test runs

    def test_filing_a_report_starts_one_background_send_only_when_it_goes_without_review(self):
        self.assertTrue(bugs.file_report(title=f"{TITLE}, case 1", session="s1")["held"])  # no consent: it waits
        bugs.set_consent("on", "ana")
        with mock.patch.dict(core.CFG, {"bugs": {"sync": False}}):
            self.assertTrue(bugs.file_report(title=f"{TITLE}, case 2", session="s1")["held"])
        self.assertEqual(self.started, [])  # nothing starts for either
        r = bugs.file_report(title=TITLE, session="s1")
        self.assertEqual((r["held"], r["sending"]), (False, True))
        (argv, kw), = self.started
        self.assertEqual(argv[-2:], ["bug", "sync"])
        self.assertTrue(kw.get("start_new_session"))
        again = bugs.file_report(title="dev status prints nothing in a second clone", session="s1")
        self.assertEqual((again["held"], again["sending"], len(self.started)), (False, True, 1))  # the running one

    def test_a_report_filed_while_a_send_runs_goes_out_before_that_send_ends(self):
        bugs.set_consent("on", "ana")
        self.assertTrue(bugs.file_report(title=TITLE, session="s1")["sending"])
        second, filed = "dev status prints nothing in a second clone", []

        class Filing(FakeClient):
            def create_issue(self, title, body):
                if not filed:  # another agent files a report while this send runs
                    filed.append(bugs.file_report(title=second, session="s2"))
                return super().create_issue(title, body)
        client = Filing()
        bugs.sync(client)  # the background send: send.json names this process
        self.assertEqual((filed[0]["sending"], len(self.started)), (True, 1))  # the running send takes it
        titles = [_sent(b).get("title") for _, b in client.created]
        self.assertEqual([t for t in titles if t], [TITLE, second])
        with mock.patch.object(bugs.time, "time", return_value=time.time() + 61):  # that send has ended
            r = bugs.file_report(title="dev status prints nothing in a third clone", session="s3")
        self.assertEqual((r["sending"], len(self.started)), (True, 2))

    def test_a_background_send_never_starts_within_a_minute_of_the_previous_start(self):
        bugs.set_consent("on", "ana")
        with mock.patch.object(bugs.time, "time", return_value=NOW):
            self.assertTrue(bugs.file_report(title=TITLE, session="s1")["sending"])
        with mock.patch.object(bugs, "_alive", lambda pid: False):  # that send has ended
            with mock.patch.object(bugs.time, "time", return_value=NOW + 59):
                r = bugs.file_report(title="dev status prints nothing in a second clone", session="s1")
            self.assertEqual((r["sending"], len(self.started)), (False, 1))  # it waits for the hourly sync
            with mock.patch.object(bugs.time, "time", return_value=NOW + 61):
                r = bugs.file_report(title="dev status prints nothing in a third clone", session="s1")
        self.assertEqual((r["sending"], len(self.started)), (True, 2))

    def test_no_background_send_starts_for_a_report_the_limit_keeps_for_a_later_send(self):
        bugs.set_consent("on", "ana")
        for n in range(10):
            _agent_report(n)
        with mock.patch.object(bugs.time, "time", return_value=_noon(NOW)):
            self.assertEqual(bugs.flush(FakeClient(), now=_noon(NOW))["sent"], 10)
            r = bugs.file_report(title=TITLE, session="s1")
        self.assertEqual((r["held"], r["sending"], self.started), (False, False, []))


def _noon(ts, days=0):
    """Noon on this Mac's calendar, `days` days after the day of `ts`: well inside one calendar day, DST or not."""
    t = time.localtime(ts)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday + days, 12, 0, 0, 0, 0, -1))


def _agent_report(n, title=None):
    """Queue report `n` (its fingerprint is `n` in hex) written in an agent's own words, each with its own title: with
    consent and bugs.sync on, it goes out without review."""
    title = title or f"{TITLE}, case {n}"
    return bugs.queue({"payload": {**_payload("report", fp=f"{n:012x}", title=title), "kind": "command"}})


@contextlib.contextmanager
def _zone(name):
    """This process in time zone `name` (TZ), then back in its own."""
    saved = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


class SendLimitTests(StateCase):
    # At most ten items written in an agent's own words go out without review in any 24 hours on this Mac, counted
    # when they are sent, by their UTC times. The rest stay queued, never held for review, and go with the next send
    # the limit allows, oldest first.

    def test_the_eleventh_report_waits_until_the_oldest_of_the_ten_was_sent_more_than_24_hours_ago(self):
        bugs.set_consent("on", "ana")
        for n in range(11):
            _agent_report(n)
        today = _noon(NOW)
        self.assertEqual(bugs.flush(FakeClient(fail=True), now=today)["failed"], 11)  # a failed send counts nothing
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=today + 2 * 3600)["sent"], 10)
        self.assertEqual(bugs.pending(), [])  # it waits for the limit, not for the developer
        self.assertEqual(bugs.flush(client, now=_noon(NOW, 1))["sent"], 0)  # another calendar day, 22 hours later
        self.assertEqual(bugs.flush(client, now=today + 2 * 3600 + DAY + 1)["sent"], 1)
        self.assertEqual([_sent(b)["fingerprint"] for _, b in client.created], [f"{n:012x}" for n in range(11)])
        self.assertEqual(list((bugs.BUGS / "outbox").glob("*.json")), [])

    def test_switching_to_a_time_zone_on_another_calendar_day_does_not_reset_the_limit(self):
        bugs.set_consent("on", "ana")
        for n in range(11):
            _agent_report(n)
        client = FakeClient()
        with _zone("Pacific/Kiritimati"):  # UTC+14
            self.assertEqual(bugs.flush(client, now=NOW)["sent"], 10)
        with _zone("Pacific/Pago_Pago"):  # UTC-11: the calendar day before, at the same moment
            self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 0)
            self.assertEqual(bugs.flush(client, now=NOW + DAY + 1)["sent"], 1)

    def test_a_send_time_in_the_future_counts_as_a_full_limit(self):
        bugs.set_consent("on", "ana")
        _agent_report(1)
        bugs._write_json(bugs.BUGS / bugs.CAP_STATE, {"sent": [NOW + 3600]})  # the clock was set back since
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW)["sent"], 0)
        self.assertEqual(bugs.flush(client, now=NOW + 3601)["sent"], 1)

    def test_a_send_counts_from_when_it_went_out_not_from_when_its_pass_started(self):
        bugs.set_consent("on", "ana")
        for n in range(11):
            _agent_report(n)
        start = _noon(NOW)
        clock = [start]

        class Slow(FakeClient):
            def create_issue(self, title, body):
                clock[0] = start + 3600  # the pass's first send takes an hour
                return super().create_issue(title, body)
        with mock.patch.object(bugs.time, "time", lambda: clock[0]):  # as `dev bug sync` runs it: the clock goes on
            self.assertEqual(bugs.flush(Slow())["sent"], 10)
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=start + DAY + 60)["sent"], 0)  # 24 hours after the pass started
        self.assertEqual(bugs.flush(client, now=start + 3600 + DAY + 1)["sent"], 1)

    def test_a_limit_record_that_cannot_be_read_counts_as_a_full_limit_for_24_hours(self):
        bugs.set_consent("on", "ana")
        _agent_report(1)
        record = bugs.BUGS / bugs.CAP_STATE
        record.write_text('{"sent": [')  # a write cut short
        record.chmod(0o600)
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW)["sent"], 0)
        self.assertEqual(bugs.flush(client, now=NOW + DAY + 1)["sent"], 1)

    def test_security_reports_count_and_votes_and_automatic_metadata_reports_do_not(self):
        bugs.set_consent("on", "ana")
        self.cache_bug()  # the vote's bug, issue 42
        bugs.queue({"payload": _payload("report", fp=NEW_FP, frames=["guard.check_git"])})  # to private reporting
        bugs.queue({"payload": _payload("vote")})
        bugs.record_occurrence("crash", "0a0b0c0d0e0f", exc_class="KeyError", command="progress step")  # metadata
        for n in range(11):
            _agent_report(n)
        client = FakeClient()
        bugs.flush(client, now=time.time() + 2 * 3600)  # past the vote's random delay
        self.assertEqual((len(client.private), [n for n, _ in client.posted]), (1, [42]))
        worded = [_sent(b)["fingerprint"] for _, b in client.created if _sent(b).get("title")]
        self.assertEqual(worded, [f"{n:012x}" for n in range(9)])  # the security report took the first of the ten
        self.assertEqual(len(client.created), 10)  # the nine and the metadata report's issue

    def test_an_item_the_developer_approved_goes_out_although_the_limit_is_full(self):
        bugs.set_consent("on", "ana")
        held = _agent_report(99, title="dev status breaks in the billing monorepo")
        with mock.patch.object(bugs, "deny_words", lambda: frozenset({"acme", "billing"})):
            bugs.flush(FakeClient(), now=NOW)  # held with its problem: the developer decides
        for n in range(10):
            _agent_report(n)
        client = FakeClient()
        self.assertEqual(bugs.flush(client, now=NOW + 60)["sent"], 10)
        bugs.approve(held)
        self.assertEqual(sum(bugs.flush(client, now=NOW + 120 + i)["sent"] for i in range(2)), 1)
        self.assertEqual(_sent(client.created[-1][1])["title"], "dev status breaks in the billing monorepo")

    def test_the_next_send_the_limit_allows_starts_with_the_oldest_report_it_kept_back(self):
        bugs.set_consent("on", "ana")
        for n in range(15):
            _agent_report(n)
        client = FakeClient()
        # five items a pass, as when automatic metadata reports leave the outbox's scan little room
        self.assertEqual([bugs._flush_legacy(client, _noon(NOW), scan_limit=5)["sent"] for _ in range(3)], [5, 5, 0])
        for n in range(15, 20):
            _agent_report(n)  # filed after the limit kept 10 to 14 back, and ahead of them in the scan
        self.assertEqual(bugs._flush_legacy(client, _noon(NOW) + DAY + 1, scan_limit=5)["sent"], 5)
        self.assertEqual([_sent(b)["fingerprint"] for _, b in client.created], [f"{n:012x}" for n in range(15)])


class ConsentTests(StateCase):
    def test_consent_is_private_and_the_config_can_switch_it_off(self):
        bugs.set_consent("reports", "ana")
        c = bugs.consent()
        self.assertEqual((c["login"], c["share"]), ("ana", "reports"))
        self.assertEqual(stat.S_IMODE(os.stat(bugs.BUGS / "consent.json").st_mode), 0o600)
        with mock.patch.dict(core.CFG, {"bugs": {"share": "off", "sync": True}}):
            self.assertEqual(bugs.consent()["share"], "off")
        with self.assertRaises(ValueError):
            bugs.set_consent("maybe", "ana")

    def test_reports_only_consent_sends_no_votes(self):
        bugs.set_consent("reports", "ana")
        self.cache_bug()
        bugs.queue({"payload": _payload("vote")})
        bugs.queue({"payload": _payload("detour")})
        client = FakeClient()
        bugs.flush(client, now=NOW + 2 * 3600)
        self.assertEqual([_sent(b)["type"] for _, b in client.posted], ["detour"])


class TrimTests(StateCase):
    def test_trim_keeps_recent_state(self):
        occ = bugs.BUGS / "occurrences.jsonl"
        occ.parent.mkdir(parents=True)
        occ.write_text(json.dumps({"ts": NOW - 91 * DAY, "code": "old"}) + "\n" +
                       json.dumps({"ts": NOW - DAY, "code": "new"}) + "\n")
        ledger = bugs.BUGS / "ledger"
        ledger.mkdir()
        bugs.CRASHES.mkdir()
        for folder, name, age in ((ledger, "s-old.jsonl", 8), (ledger, "s-new.jsonl", 1),
                                  (bugs.CRASHES, "old.txt", 31), (bugs.CRASHES, "new.txt", 1)):
            (folder / name).write_text("x")
            os.utime(folder / name, (NOW - age * DAY, NOW - age * DAY))
        self.assertEqual(bugs.trim(now=NOW), {"occurrences": 1, "ledgers": 1, "crashes": 1})
        self.assertEqual([json.loads(x)["code"] for x in occ.read_text().splitlines()], ["new"])
        self.assertEqual(sorted(p.name for p in ledger.iterdir()), ["s-new.jsonl"])
        self.assertEqual(sorted(p.name for p in bugs.CRASHES.iterdir()), ["new.txt"])


class ConcurrencyTests(StateCase):
    def test_concurrent_queue_and_occurrences(self):
        ids, errors = [], []

        def work(tag):
            try:
                for i in range(200):
                    ids.append(bugs.queue({"payload": _payload("vote", impact="minor")}))
                    bugs.record_occurrence("hook", FP, session=f"{tag}-{i}", event="post-tool")
            except Exception as e:  # a failure in a thread must fail the test, not vanish with the thread
                errors.append(e)
        threads = [threading.Thread(target=work, args=(t,)) for t in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(ids)), 400)
        items = [json.loads(f.read_text()) for f in (bugs.BUGS / "outbox").glob("*.json")]
        self.assertEqual(sorted(i["id"] for i in items), sorted(ids))
        lines = (bugs.BUGS / "occurrences.jsonl").read_text().splitlines()
        self.assertEqual(sorted(json.loads(x)["session"] for x in lines),
                         sorted(f"{t}-{i}" for t in "ab" for i in range(200)))


class WiringTests(StateCase):
    def test_the_hourly_sync_sends_no_report_when_bugs_sync_is_false(self):
        bugs.record_occurrence("crash", NEW_FP, exc_class="KeyError")
        flushed = []
        self.run_sync(cfg={"bugs": {"share": None, "sync": False}},
                      extra=[mock.patch.object(bugs, "flush", lambda *a, **k: flushed.append(True) or {})])
        self.assertEqual(flushed, [])

    def run_sync(self, *flags, cfg=None, extra=()):
        calls = []
        stubs = [*extra, mock.patch.object(bugs, "sync", lambda: calls.append(True) or {}),
                 mock.patch("devkit.registry.build", lambda **k: {"projects": [], "sessions": []}),
                 mock.patch("devkit.registry.write_outputs", lambda reg: []),
                 mock.patch("devkit.dashboard.write", lambda reg: None),
                 mock.patch("devkit.work.pull", lambda: None), mock.patch("devkit.work.expire_locks", lambda: []),
                 mock.patch("devkit.work.write_presence", lambda s: None),
                 mock.patch("devkit.providers.status", lambda **k: {}), mock.patch("devkit.policy.diff", lambda: []),
                 mock.patch("devkit.approvals.sweep", lambda: None), mock.patch("devkit.continuity.expire",
                                                                               lambda now=None: 0),
                 mock.patch("devkit.sleep.due", lambda: False), mock.patch("devkit.canary.prune", lambda: None),
                 mock.patch.object(cli, "commit_push", lambda **k: None),
                 mock.patch.object(cli, "_expire_sessions", lambda: None),
                 mock.patch.object(cli, "_rotate_log", lambda f: None)]
        with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
            for s in stubs:
                stack.enter_context(s)
            stack.enter_context(mock.patch.object(cli, "STATE", Path(d)))
            stack.enter_context(mock.patch.dict(core.CFG, {"gc": {"auto": False}, **(cfg or {})}))
            a = cli.build_parser().parse_args(["sync", "--scheduled", "--quiet", "--no-push", *flags])
            a.fn(a)
        return calls

    def test_the_hourly_sync_sweeps_other_sessions_store(self):
        # Without the sweep nothing expires in the peers store: told messages, ended sessions' boxes, void claims and
        # old escalations pile up, and every Stop gate, welcome and dev status reads them all
        swept = []
        self.run_sync(extra=[mock.patch("devkit.peers.sweep", lambda now=None: swept.append(True) or {})])
        self.assertEqual(swept, [True])

    def test_the_hourly_sync_refreshes_the_registry_unless_offline_or_switched_off(self):
        self.assertEqual(self.run_sync(), [True])
        self.assertEqual(self.run_sync("--offline"), [])
        self.assertEqual(self.run_sync(cfg={"bugs": {"share": None, "sync": False}}), [])

    def test_sleep_trims_the_registry_state(self):
        bugs.CRASHES.mkdir(parents=True)
        old = bugs.CRASHES / "old.txt"
        old.write_text("x")
        os.utime(old, (time.time() - 40 * DAY, time.time() - 40 * DAY))
        with mock.patch("devkit.lessons.publish_usage", lambda: None), \
                mock.patch("devkit.lessons.consolidate", lambda **k: []), \
                mock.patch.object(sleep, "hygiene_plan", lambda now=None: []), \
                mock.patch.object(sleep, "LAST", Path(self.tmp.name) / "sleep.json"):
            result = sleep.run(quiet=True)
        self.assertFalse(old.exists())
        self.assertEqual(result["bugs"]["crashes"], 1)


if __name__ == "__main__":
    unittest.main()
