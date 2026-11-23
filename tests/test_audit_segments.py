# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The audit trail in segments: one segment per UTC day (and per 16 MiB), closed segments gzip-compressed and listed in
# a manifest that anchors the chain across segments and indexes them by day, time span and action; month files of an
# earlier devkit read as they are; dev.sync and handoff.push summarized at the default level; each closed segment
# published once; and `dev audit reseal`, which verifies first and keeps the old chain heads in a map. Every test
# writes and reads real files in a temporary folder.
import argparse
import calendar
import contextlib
import gzip
import hashlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core  # noqa: E402
from tests.test_audit import AuditCase  # noqa: E402

DAY = 86400


def noon(days_ago: int, now: float = None) -> float:
    """Noon (UTC) of the UTC day `days_ago` days before `now`."""
    y, m, d = time.gmtime((now or time.time()) - days_ago * DAY)[:3]
    return float(calendar.timegm((y, m, d, 12, 0, 0, 0, 0, 0)))


def utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


class SegmentCase(AuditCase):
    def setUp(self):
        super().setUp()
        self.elsewhere = self.root / "elsewhere"
        os.environ["DEVKIT_AGENT"] = "claude"  # an agent's session unless a test says otherwise

    def write(self, ts: float, n: int = 1, action: str = "dev.task", target: str = "add", **extra):
        # n records at `ts`, appended with the clock at their own time -> the records
        out = []
        for i in range(n):
            out.append(audit._append(audit.build(action, f"{target} {i}", "", "ok", extra, now=ts + i), now=ts + i))
        return out

    def seg(self, ts: float, seq: int = 1) -> Path:
        return audit.AUDIT / f"{utc_day(ts)}.{seq:02d}.jsonl"

    def closed(self, ts: float, seq: int = 1) -> Path:
        d = utc_day(ts)
        return audit.AUDIT / d[:7] / f"{d}.{seq:02d}.jsonl.gz"

    def manifest(self) -> dict:
        return json.loads((audit.AUDIT / "manifest.json").read_text())

    def stored(self, path: Path) -> list:
        data = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
        return [json.loads(ln) for ln in data.splitlines()]

    def as_developer(self):
        # the developer's own terminal: a terminal on stdin and no agent marker or parent (backup.developer_here)
        from devkit import backup
        return mock.patch.object(backup, "developer_here", return_value=True)

    def run_cmd(self, *argv):
        a = audit.add_arguments(argparse.ArgumentParser()).parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = audit.cmd_audit(a)
        return rc, out.getvalue() + err.getvalue()


class WriteTests(SegmentCase):
    def test_records_of_two_utc_days_go_to_two_segments_and_the_second_links_to_the_first(self):
        d1, d2 = noon(2), noon(1)
        first = self.write(d1, 2)
        second = self.write(d2, 1)
        self.assertEqual([r["id"] for r in self.stored(self.seg(d1))], [r["id"] for r in first])
        self.assertEqual([r["id"] for r in self.stored(self.seg(d2))], [second[0]["id"]])
        self.assertEqual(second[0]["prev"], first[-1]["hash"])
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 3)

    def test_a_full_segment_starts_the_next_sequence_of_the_same_day(self):
        d = noon(1)
        one = self.write(d, 1)
        with mock.patch.object(audit, "SEGMENT_LIMIT", self.seg(d).stat().st_size):
            two = self.write(d + 60, 2)
        self.assertEqual(len(self.stored(self.seg(d, 1))), 1)
        self.assertEqual(len(self.stored(self.seg(d, 2))), 1)
        self.assertEqual(len(self.stored(self.seg(d, 3))), 1)
        self.assertEqual(two[0]["prev"], one[0]["hash"])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_a_record_whose_clock_went_back_stays_in_the_open_segment(self):
        later = self.write(noon(1), 1)
        back = self.write(noon(3), 1)
        self.assertFalse(self.seg(noon(3)).exists())
        self.assertEqual([r["id"] for r in self.stored(self.seg(noon(1)))], [later[0]["id"], back[0]["id"]])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_the_open_segment_is_private(self):
        self.write(noon(0), 1)
        self.assertEqual(oct(self.seg(noon(0)).stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(oct(audit.AUDIT.stat().st_mode & 0o777), oct(0o700))


class CloseTests(SegmentCase):
    def test_the_job_compresses_a_past_day_and_lists_it_in_the_manifest(self):
        d = noon(1)
        recs = self.write(d, 3) + self.write(d + 10, 1, action="git.backup", target="main")
        plain = self.seg(d).read_bytes()
        self.assertEqual(audit.close(now=noon(0)), [self.closed(d)])
        self.assertFalse(self.seg(d).exists())
        self.assertEqual(gzip.decompress(self.closed(d).read_bytes()), plain)
        self.assertEqual(oct(self.closed(d).stat().st_mode & 0o777), oct(0o600))
        entry = self.manifest()["units"][0]
        self.assertEqual({k: entry[k] for k in ("name", "kind", "file", "bytes", "sha256", "records", "head", "link")},
                         {"name": f"{utc_day(d)}.01", "kind": "day",
                          "file": f"{utc_day(d)[:7]}/{utc_day(d)}.01.jsonl.gz",
                          "bytes": len(plain), "sha256": hashlib.sha256(plain).hexdigest(), "records": 4,
                          "head": recs[-1]["hash"], "link": recs[-1]["hash"]})
        self.assertEqual((entry["lo"], entry["hi"]), (recs[0]["ts"], recs[-1]["ts"]))
        self.assertEqual((entry["days"], entry["actions"]), ({utc_day(d): 4}, {"dev.task": 3, "git.backup": 1}))
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 4)
        self.assertEqual(audit.close(now=noon(0)), [])  # closed once

    def test_todays_open_segment_stays_open_when_the_job_runs(self):
        self.write(noon(0), 2)
        self.assertEqual(audit.close(now=noon(0) + 3600), [])
        self.assertTrue(self.seg(noon(0)).is_file())
        self.assertFalse((audit.AUDIT / "manifest.json").exists())

    def test_a_segment_after_a_compressed_one_links_to_its_manifest_link(self):
        before = self.write(noon(2), 2)
        audit.close(now=noon(1))
        after = self.write(noon(1), 1)
        self.assertEqual(after[0]["prev"], self.manifest()["units"][0]["link"])
        self.assertEqual(after[0]["prev"], before[-1]["hash"])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_verify_names_a_compressed_segment_changed_or_removed_and_a_manifest_entry_changed(self):
        d = noon(2)
        self.write(d, 3)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        gz, mf = self.closed(d), audit.AUDIT / "manifest.json"
        keep = (gz.read_bytes(), mf.read_bytes())
        cases = {
            "edited": lambda: gz.write_bytes(gzip.compress(gzip.decompress(keep[0]).replace(b'"add 1"', b'"add 7"'))),
            "removed": gz.unlink,
            "manifest": lambda: mf.write_text(keep[1].decode().replace('"records": 3', '"records": 2')),
        }
        for name, change in cases.items():
            with self.subTest(change=name):
                change()
                res = audit.verify(team=False)
                self.assertFalse(res["ok"])
                self.assertIn(gz.name, res["first"]["path"] + res["first"]["problem"])
                gz.write_bytes(keep[0])
                mf.write_bytes(keep[1])
        self.assertTrue(audit.verify(team=False)["ok"])


class OlderMonthFileTests(SegmentCase):
    def test_the_first_segment_links_to_the_end_of_an_earlier_devkits_month_file_and_leaves_it_as_it_is(self):
        month = audit.AUDIT / time.strftime("%Y-%m.jsonl")
        olds = [self.old_append(time.time() - 300 + i) for i in range(3)]
        before = month.read_bytes()
        audit.record("dev.task", "after the update")
        self.assertEqual(month.read_bytes(), before)
        seg = self.stored(self.seg(time.time()))
        self.assertEqual(seg[0]["prev"], olds[-1]["hash"])
        self.assertEqual(self.manifest()["origin"],
                         {"unit": month.stem, "bytes": len(before), "link": olds[-1]["hash"]})
        self.assertEqual(len(audit.query(action="dev.task")), 4)
        self.assertTrue(audit.verify(team=False)["ok"])
        self.old_append(time.time())  # a session still on the earlier devkit appends to its month file
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(len(audit.query(action="dev.task")), 5)

    def test_a_month_file_is_compressed_only_after_its_month_has_passed(self):
        last_month = audit.months_back(time.time(), 1)
        y, m = int(last_month[:4]), int(last_month[5:])
        ts = time.mktime((y, m, 14, 12, 0, 0, 0, 0, -1))
        ids = [self.old_append(ts)["id"], self.old_append(time.time() - 60)["id"], audit.record("dev.task", "new")["id"]]
        closed = audit.close()
        self.assertEqual(closed, [audit.AUDIT / last_month / f"{last_month}.jsonl.gz"])
        self.assertTrue((audit.AUDIT / time.strftime("%Y-%m.jsonl")).is_file())  # an earlier devkit may append
        entry = next(u for u in self.manifest()["units"] if u["name"] == last_month)
        self.assertEqual((entry["kind"], entry["records"]), ("month", 1))
        self.assertEqual([r["id"] for r in audit.query(action="dev.task", since=last_month + "-01")], ids)
        self.assertTrue(audit.verify(team=False)["ok"])


    def test_a_month_file_written_again_after_it_was_compressed_never_replaces_it(self):
        last_month = audit.months_back(time.time(), 1)
        y, m = int(last_month[:4]), int(last_month[5:])
        ts = time.mktime((y, m, 14, 12, 0, 0, 0, 0, -1))
        first = self.old_append(ts)
        audit.record("dev.task", "new")
        audit.close()
        gz = audit.AUDIT / last_month / f"{last_month}.jsonl.gz"
        kept = gz.read_bytes()
        late = self.old_append(ts + 60)  # a session still on an earlier devkit, whose clock says that month
        self.assertEqual(audit.close(), [])
        self.assertEqual(gz.read_bytes(), kept)
        self.assertTrue((audit.AUDIT / f"{last_month}.jsonl").is_file())
        ids = {r["id"] for r in audit.query(action="dev.task", since=last_month + "-01")}
        self.assertTrue({first["id"], late["id"]} <= ids, ids)
        audit.publish()
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["unpublished"], 2)  # the late line, and today's open segment


class ReadTests(SegmentCase):
    def test_a_query_reads_only_the_closed_segments_its_window_or_action_reaches(self):
        written = []
        for back in range(30, 0, -1):
            written += self.write(noon(back), 1)
            if back == 12:
                written += self.write(noon(12) + 60, 1, action="git.embargo", target="fix/x")
        audit.close(now=noon(0))
        written += self.write(noon(0), 1)
        with mock.patch.object(audit, "read_trail", wraps=audit.read_trail) as reads:
            got = audit.query(days=1)
        names = sorted(Path(c.args[0]).name for c in reads.call_args_list)
        self.assertEqual([r["id"] for r in got], [r["id"] for r in written if r["ts"] >= time.time() - DAY])
        self.assertLessEqual(len(names), 3, names)
        self.assertIn(self.seg(noon(0)).name, names)
        with mock.patch.object(audit, "read_trail", wraps=audit.read_trail) as reads:
            got = audit.query(action="git.embargo", days=60)
        names = sorted(Path(c.args[0]).name for c in reads.call_args_list)
        self.assertEqual([r["target"] for r in got], ["fix/x 0"])
        self.assertEqual(names, sorted([self.closed(noon(12)).name, self.seg(noon(0)).name]))

    def test_an_embargo_and_a_backup_in_compressed_segments_are_still_found(self):
        from devkit import branches
        rec = audit.build("git.embargo", "fix/unshipped", "set: an unshipped security fix", "ok",
                          {"project": "acme/app", "branch": "fix/unshipped"}, now=noon(3))
        audit._append(rec, now=noon(3))
        bk = audit.build("git.backup", "feat/x", "pushed", "ok", {"project": "acme/app", "branch": "feat/x"},
                         now=noon(3) + 5)
        audit._append(bk, now=noon(3) + 5)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        self.assertTrue(self.closed(noon(3)).is_file())
        branches._TRAIL_CACHE.clear()
        branches._AUDIT_CACHE.clear()
        self.assertIn("fix/unshipped", branches.embargo_trail("acme/app"))
        self.assertEqual(branches.backups("acme/app")["feat/x"]["backup"], bk["ts"])
        audit.publish()
        audit.AUDIT = self.elsewhere  # another Mac reads it from the handoff copy
        branches._TRAIL_CACHE.clear()
        self.assertIn("fix/unshipped", branches.embargo_trail("acme/app"))


class DamageTests(SegmentCase):
    def test_a_manifest_cannot_point_a_reader_or_a_prune_outside_the_audit_folder(self):
        old = noon(0, time.time() - 16 * 31 * DAY)
        self.write(old, 1)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        outside = audit.AUDIT.parent / "outside.txt"
        outside.write_text("not the audit trail\n")
        mf = audit.AUDIT / "manifest.json"
        m = json.loads(mf.read_text())
        m["units"][0]["file"] = "../outside.txt"
        m["units"].append({**m["units"][1], "name": "../../outside"})
        mf.write_text(json.dumps(m))
        self.assertNotIn(outside, audit.trail_files())
        self.assertEqual([u["name"] for u in audit._units()], [f"{utc_day(old)}.01", f"{utc_day(noon(1))}.01"])
        audit.prune()
        self.assertEqual(outside.read_text(), "not the audit trail\n")
        self.assertFalse(self.closed(old).exists())

    def test_a_damaged_count_file_loses_no_record(self):
        self.write(noon(1), 1)
        (audit.AUDIT / ".quiet.json").write_text(json.dumps({
            utc_day(noon(1)): {"dev.sync": {"n": "many", "last": "late"}, "handoff.push": {"n": 2, "last": "x"}},
            "not a day": {"dev.sync": {"n": 3}}}))
        rec = self.write(noon(0), 1)[0]
        self.assertEqual(self.stored(self.seg(noon(0)))[-1]["id"], rec["id"])
        sums = [r for r in self.stored(self.seg(noon(1))) if (r.get("ref") or {}).get("type") == "summary"]
        self.assertEqual([(r["action"], r["evidence"]["count"]) for r in sums], [("handoff.push", 2)])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_a_cut_compressed_segment_is_reported_and_the_rest_still_reads(self):
        self.write(noon(2), 2)
        later = self.write(noon(1), 1)
        audit.close(now=noon(0))
        gz = self.closed(noon(2))
        gz.write_bytes(gz.read_bytes()[:-12])
        self.assertEqual([r["id"] for r in audit.query(days=30)], [later[0]["id"]])
        res = audit.verify(team=False)
        self.assertFalse(res["ok"])
        self.assertEqual((res["first"]["path"], res["first"]["kind"]), (str(gz), "unreadable"))

    def test_a_unit_reads_line_by_line_compressed_or_not_from_any_audit_folder(self):
        self.write(noon(2), 3)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        other = self.root / "other-audit"
        files = audit.trail_files(root=audit.AUDIT)
        self.assertEqual(files, [self.closed(noon(2)), self.closed(noon(1))])
        for f in files:
            with audit.open_trail(f) as fh:
                self.assertEqual(list(fh), audit.read_trail(f).splitlines(keepends=True))
        self.assertEqual(audit.trail_files(root=other), [])


class QuietTests(SegmentCase):
    def test_dev_sync_and_handoff_push_are_summarized_once_a_day_at_the_default_level(self):
        for _ in range(50):
            audit.record("dev.sync", ["--quiet"], "", "ok")
        for _ in range(3):
            audit.record("handoff.push", "acme/handoff", "1 commit(s)")
        audit.record("handoff.push", "acme/handoff", "rejected", "failed")
        audit.record("dev.sync", ["--quiet"], "", "exit 1")
        today = self.seg(time.time())
        self.assertEqual([r["outcome"] for r in self.stored(today) if r["action"] in ("dev.sync", "handoff.push")],
                         ["failed", "failed"])
        audit.close(now=time.time() + DAY)
        recs = self.stored(self.closed(time.time()))
        sums = [r for r in recs if (r.get("ref") or {}).get("type") == "summary"]
        self.assertEqual(sorted((r["action"], r["evidence"]["count"], r["outcome"]) for r in sums),
                         [("dev.sync", 50, "ok"), ("handoff.push", 3, "ok")])
        self.assertTrue(all(utc_day(r["ts"]) == utc_day(time.time()) for r in sums))
        self.assertIn("50", next(r for r in sums if r["action"] == "dev.sync")["detail"])
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_level_all_keeps_each_one(self):
        core.CFG["audit"]["level"] = "all"
        for _ in range(5):
            audit.record("dev.sync", ["--quiet"], "", "ok")
        self.assertEqual(len([r for r in self.stored(self.seg(time.time())) if r["action"] == "dev.sync"]), 5)


class PublishTests(SegmentCase):
    def test_each_closed_segment_is_published_once_with_a_manifest(self):
        a, b = self.write(noon(2), 2), self.write(noon(1), 2)
        self.write(noon(0), 1)  # today's: still open, not published
        audit.close()
        changed = audit.publish()
        mine = audit.team_dir()
        files = [mine / utc_day(noon(n))[:7] / f"{utc_day(noon(n))}.01.jsonl" for n in (2, 1)]
        self.assertEqual(sorted(changed), sorted(files + [mine / "manifest.json"]))
        for f, recs in zip(files, (a, b)):
            self.assertEqual([r["id"] for r in self.stored(f)], [r["id"] for r in recs])
        team = json.loads((mine / "manifest.json").read_text())
        self.assertEqual([u["name"] for u in team["units"]], [f.name[:-6] for f in files])
        self.assertEqual(audit.publish(), [])  # written once
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["unpublished"], 1)
        audit.AUDIT = self.elsewhere
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(sorted(r["id"] for r in audit.query(team=True, days=7)), sorted(r["id"] for r in a + b))

    def test_a_published_segment_that_differs_is_reported_and_never_overwritten(self):
        self.write(noon(2), 2)
        self.write(noon(1), 1)
        audit.close()
        audit.publish()
        f = audit.team_dir() / utc_day(noon(2))[:7] / f"{utc_day(noon(2))}.01.jsonl"
        f.write_bytes(f.read_bytes().replace(b'"add 1"', b'"add 9"'))
        changed = f.read_bytes()
        self.write(noon(0), 1)
        self.assertNotIn(f, audit.publish())
        self.assertEqual(f.read_bytes(), changed)
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertTrue(any(f.name in p["path"] + p["problem"] for p in res["problems"]), res["problems"])
        refused = [r for r in audit.query(action="audit.publish") if r["outcome"] == "blocked"]
        self.assertEqual([r["severity"] for r in refused], ["critical"])


class PruneTests(SegmentCase):
    def test_prune_removes_old_months_of_segments_and_the_first_kept_one_still_verifies(self):
        now = time.time()
        old = noon(0, now - 16 * 31 * DAY)
        gone = self.write(old, 2)
        self.write(noon(3), 1)
        self.write(noon(2), 1)
        audit.close(now=noon(0))
        audit.publish()
        out = audit.prune(now=now)
        self.assertFalse(self.closed(old).exists())
        self.assertEqual(out["removed"], [utc_day(old)[:7]])
        units = self.manifest()["units"]
        self.assertEqual([u["name"] for u in units], [f"{utc_day(noon(n))}.01" for n in (3, 2)])
        self.assertEqual(self.manifest()["start"], {"unit": f"{utc_day(old)}.01", "link": gone[-1]["hash"]})
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])


class ResealTests(SegmentCase):
    # records sealed under older cleaning rules, as test_audit's PersonalDataTests seal them
    def sealed_record(self, ts: float, text: str = "Ana wants the Acme roadmap by Friday") -> dict:
        return {"v": 2, "id": audit.new_id(ts), "ts": ts, "time": audit.iso(ts),
                "actor": {"user": "fixture-user", "host": "fixture-host", "agent": "claude", "session": ""},
                "action": "crew.message", "category": "change", "severity": "info", "target": "Neo → Persephone",
                "detail": text, "outcome": "ok", "ref": {"id": "M3"}}

    def scene(self):
        # an earlier devkit's month file of last month with a record of message text and a v1 line with a home path,
        # published by day; then segments on two days, the first holding another old record, closed and published
        last_month = audit.months_back(time.time(), 1)
        y, m = int(last_month[:4]), int(last_month[5:])
        ts = calendar.timegm((y, m, 14, 12, 0, 0, 0, 0, 0))
        month = audit.AUDIT / f"{time.strftime('%Y-%m', time.localtime(ts))}.jsonl"
        audit._private_dir(audit.AUDIT)
        month.write_text(json.dumps({"ts": ts - 60, "user": "fixture-user", "host": "mac", "agent": "claude",
                                     "action": "dev.rm", "target": "/Users/fixture-user/Development/acme/build",
                                     "detail": "", "outcome": "ok"}) + "\n")
        self.old_append(ts, self.sealed_record(ts))
        self.old_append(ts + 1, target="clean")
        self.old_append(ts + DAY, target="next day")
        audit._append(self.sealed_record(noon(2), "the Acme roadmap again"), now=noon(2))
        self.write(noon(2) + 5, 1)
        self.write(noon(1), 1)
        audit.close()
        audit.publish()
        return month

    def trail_text(self) -> str:
        out = b""
        for f in audit.trail_files(team=True):
            out += audit.read_trail(f)
        return out.decode()

    def test_reseal_shows_what_it_would_rewrite_and_changes_nothing(self):
        self.scene()
        before = {f: f.read_bytes() for f in audit.AUDIT.rglob("*") if f.is_file()}
        plan = audit.reseal(apply=False)
        self.assertTrue(plan["ok"])
        self.assertEqual(sorted((u["name"], u["records"], u["lines"]) for u in plan["units"]),
                         sorted([(audit.months_back(time.time(), 1), 1, 1), (f"{utc_day(noon(2))}.01", 1, 0)]))
        self.assertEqual({f: f.read_bytes() for f in audit.AUDIT.rglob("*") if f.is_file()}, before)
        rc, out = self.run_cmd("reseal")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing changed", out)
        self.assertIn("dev audit reseal --apply", out)

    def test_reseal_refuses_a_chain_that_does_not_verify(self):
        self.scene()
        gz = self.closed(noon(1))
        gz.write_bytes(gzip.compress(gzip.decompress(gz.read_bytes()).replace(b'"add 0"', b'"add 5"')))
        before = {f: f.read_bytes() for f in audit.AUDIT.rglob("*.gz")}
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertFalse(res["ok"])
        self.assertIn("does not verify", res["refused"])
        self.assertEqual({f: f.read_bytes() for f in audit.AUDIT.rglob("*.gz")}, before)
        self.assertFalse((audit.AUDIT / "reseal").exists())
        rec = audit.query(action="audit.reseal")[-1]
        self.assertEqual((rec["outcome"], rec["severity"]), ("blocked", "critical"))

    def test_reseal_from_an_agents_session_is_refused_with_the_command_to_run(self):
        self.scene()
        before = {f: f.read_bytes() for f in audit.AUDIT.rglob("*.gz")}
        rc, out = self.run_cmd("reseal", "--apply")
        self.assertEqual(rc, 1)
        self.assertIn("your own terminal", out)
        self.assertEqual({f: f.read_bytes() for f in audit.AUDIT.rglob("*.gz")}, before)
        rec = audit.query(action="audit.reseal")[-1]
        self.assertEqual(rec["outcome"], "blocked")

    def test_reseal_rewrites_old_records_keeps_the_old_heads_and_the_trail_verifies(self):
        month = self.scene()
        name = month.stem
        old_entry = next(u for u in json.loads((audit.AUDIT / "manifest.json").read_text())["units"]
                         if u["name"] == name)
        self.assertIn("roadmap", self.trail_text())
        self.assertIn("/Users/fixture-user", self.trail_text())
        v1_before = [{k: v for k, v in r.items() if k != "id"} for r in audit.query(days=60) if r["v"] == 1]
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertTrue(res["ok"], res)
        text = self.trail_text()
        self.assertNotIn("roadmap", text)
        self.assertNotIn("/Users/fixture-user", text)
        rmap = json.loads(Path(res["map"]).read_text())
        unit = next(u for u in rmap["units"] if u["name"] == name)
        self.assertEqual((unit["old"]["sha256"], unit["old"]["head"], unit["old"]["link"]),
                         (old_entry["sha256"], old_entry["head"], old_entry["link"]))
        self.assertNotEqual(unit["new"]["sha256"], unit["old"]["sha256"])
        # the segment after the resealed one still names that one's old end: the map bridges it
        after = self.stored(self.closed(noon(1)))[0]
        self.assertIn([after["prev"], audit._ends(audit.read_trail(self.closed(noon(2))))["link"]], rmap["links"])
        self.assertEqual([{k: v for k, v in r.items() if k != "id"} for r in audit.query(days=60) if r["v"] == 1],
                         v1_before)  # an older line reads back as before; only its id, its line's hash, changes
        rec = audit.query(action="audit.reseal")[-1]
        self.assertEqual((rec["outcome"], rec["evidence"]["check"]),
                         ("ok", "sha256:" + hashlib.sha256(Path(res["map"]).read_bytes()).hexdigest()))
        self.assertEqual([r["id"] for r in audit.query(days=60, team=True) if r.get("cleaned")], [])
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        audit.AUDIT = self.elsewhere  # another Mac verifies the rewritten handoff copy
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])

    def test_a_month_file_written_again_after_it_closed_is_resealed_and_the_closed_months_copy_left_alone(self):
        last_month = audit.months_back(time.time(), 1)
        y, m = int(last_month[:4]), int(last_month[5:])
        ts = calendar.timegm((y, m, 14, 12, 0, 0, 0, 0, 0))
        self.old_append(ts)
        audit.record("dev.task", "new")
        audit.close()
        audit.publish()
        day = audit.team_dir() / last_month / f"{last_month}-14.jsonl"
        published = day.read_bytes()
        # an earlier devkit writes the month again, a line exactly as long as the published day, so its offsets match
        rec = self.sealed_record(ts + 60, "Acme roadmap")
        probe = dict(rec, prev="", hash="0" * 64)
        rec["detail"] += "x" * (len(published) - len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode()) - 1)
        self.old_append(ts + 60, rec)
        late = audit.AUDIT / f"{last_month}.jsonl"
        self.assertEqual(late.stat().st_size, len(published))
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertTrue(res["ok"], res)
        self.assertNotIn(b"roadmap", late.read_bytes())
        self.assertEqual(day.read_bytes(), published)
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])

    def test_verify_refuses_a_reseal_map_that_no_record_backs(self):
        self.scene()
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertTrue(audit.verify()["ok"])
        f = Path(res["map"])
        data = json.loads(f.read_text())
        data["units"][0]["old"]["link"] = "0" * 64
        f.write_text(json.dumps(data))
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertTrue(any(f.name in p["path"] + p["problem"] for p in res["problems"]), res["problems"])


class ResealDeveloperTests(SegmentCase):
    sealed_record = ResealTests.sealed_record
    scene = ResealTests.scene

    def test_an_agent_session_that_names_itself_human_cannot_reseal(self):
        self.scene()
        before = {f: f.read_bytes() for f in audit.AUDIT.rglob("*.gz")}
        # an agent's session sets the label devkit records as the actor; its own marker is still there
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "DEVKIT_AGENT": "human", "DEVKIT_SESSION": "x"}):
            self.assertEqual(audit.actor()["agent"], "human")
            res = audit.reseal(apply=True)
        self.assertFalse(res["ok"])
        self.assertIn("your own terminal", res["refused"])
        self.assertEqual({f: f.read_bytes() for f in audit.AUDIT.rglob("*.gz")}, before)
        self.assertFalse((audit.AUDIT / "reseal").exists())
        self.assertIn("roadmap", "".join(audit.read_trail(f).decode() for f in audit.trail_files()))
        rec = audit.query(action="audit.reseal")[-1]
        self.assertEqual(rec["outcome"], "blocked")


class ResealControlTests(SegmentCase):
    # a pattern of audit.redact.extra (this Mac's config, or the team policy any teammate can commit) that matches what
    # devkit's own controls read back from the trail
    sealed_record = ResealTests.sealed_record
    scene = ResealTests.scene

    def test_an_embargo_is_still_found_after_a_reseal_under_a_pattern_that_matches_its_branch(self):
        from devkit import branches
        sha = "a" * 40
        audit._append(audit.build("git.embargo", "fix/unshipped", "set: an unshipped security fix", "ok",
                                  {"project": "acme/app", "branch": "fix/unshipped", "commit": sha}, now=noon(3)),
                      now=noon(3))
        self.write(noon(3) + 5, 1, target="pushed fix/unshipped")
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        audit.publish(now=noon(0))
        self.policy({"redact": {"extra": ["fix/unshipped", "an unshipped security fix", "a{40}"]}})
        with self.as_developer():
            res = audit.reseal(apply=True, now=noon(0))
        self.assertTrue(res["ok"], res)
        stored = self.stored(self.closed(noon(3)))
        self.assertNotIn("fix/unshipped", stored[1]["target"])  # the reseal ran: the other record was cleaned
        self.assertEqual((stored[0]["branch"], stored[0]["target"], stored[0]["detail"], stored[0]["evidence"]),
                         ("fix/unshipped", "fix/unshipped", "set: an unshipped security fix", {"commit": sha}))
        branches._TRAIL_CACHE.clear()
        self.assertEqual(branches.embargo_trail("acme/app")["fix/unshipped"]["commits"], [sha])
        audit.AUDIT = self.elsewhere  # another Mac reads the embargo from the handoff copy only
        branches._TRAIL_CACHE.clear()
        self.assertEqual(list(branches.embargo_trail("acme/app")), ["fix/unshipped"])

    def test_a_reseal_keeps_the_id_and_check_of_an_earlier_reseals_record(self):
        self.scene()
        with self.as_developer():
            res = audit.reseal(apply=True)
        rid, check = res["id"], "sha256:" + hashlib.sha256(Path(res["map"]).read_bytes()).hexdigest()
        audit._append(self.sealed_record(time.time(), "the Acme roadmap once more"))
        self.policy({"redact": {"extra": [rid, check]}})
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["units"])
        stored = [r for f in audit.trail_files() for r in self.stored(f) if r.get("action") == "audit.reseal"]
        self.assertEqual((stored[0]["ref"]["id"], stored[0]["evidence"]["check"]), (rid, check))
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])


class WrittenControlTests(SegmentCase):
    # the same patterns when devkit first writes the record
    sealed_record = ResealTests.sealed_record
    scene = ResealTests.scene

    def test_an_embargo_set_under_a_pattern_that_matches_its_branch_is_found(self):
        from devkit import branches
        sha = "b" * 40
        self.policy({"redact": {"extra": ["fix/unshipped", "set: ", "b{40}"]}})
        audit.record("git.embargo", "fix/unshipped", "set: an unshipped security fix", "ok", project="acme/app",
                     branch="fix/unshipped", commit=sha)
        branches._TRAIL_CACHE.clear()
        self.assertEqual(branches.embargo_trail("acme/app").get("fix/unshipped", {}).get("commits"), [sha])
        other = audit.record("dev.task", "pushed fix/unshipped")
        self.assertNotIn("fix/unshipped", other["target"])  # every other record is cleaned as before

    def test_a_reseal_written_under_a_pattern_that_matches_map_checks_still_verifies(self):
        self.scene()
        self.policy({"redact": {"extra": ["sha256:[0-9a-f]{64}", "[0-9A-Z]{26}"]}})
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertTrue(res["ok"], res)
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])


class MissingSegmentTests(SegmentCase):
    def embargo_scene(self):
        audit._append(audit.build("git.embargo", "fix/unshipped", "set: an unshipped security fix", "ok",
                                  {"project": "acme/app", "branch": "fix/unshipped"}, now=noon(3)), now=noon(3))
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        core.HANDOFF.mkdir(parents=True, exist_ok=True)

    def read_embargoes(self):
        from devkit import branches
        branches._TRAIL_CACHE.clear()
        return branches.read_embargoes("acme/app")

    def test_a_listed_segment_removed_here_makes_the_embargo_check_say_why(self):
        self.embargo_scene()
        self.assertIn("fix/unshipped", self.read_embargoes()[0])
        self.closed(noon(3)).unlink()
        embargoes, why = self.read_embargoes()
        self.assertIsNone(embargoes)
        self.assertIn(self.closed(noon(3)).name, why)

    def test_a_listed_segment_removed_from_a_handoff_copy_makes_the_embargo_check_say_why(self):
        self.embargo_scene()
        audit.publish(now=noon(0))
        audit.AUDIT = self.elsewhere  # another Mac, which reads the embargo from the handoff copy only
        self.assertIn("fix/unshipped", self.read_embargoes()[0])
        published = audit.team_dir() / utc_day(noon(3))[:7] / f"{utc_day(noon(3))}.01.jsonl"
        published.unlink()
        embargoes, why = self.read_embargoes()
        self.assertIsNone(embargoes)
        self.assertIn(published.name, why)

    def test_prune_drops_a_segment_from_each_manifest_before_it_removes_the_file(self):
        old = noon(0, time.time() - 16 * 31 * DAY)
        self.write(old, 2)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        audit.publish(now=old + DAY)  # the old segment was published while it was recent
        name = f"{utc_day(old)}.01"
        files = {self.closed(old): audit.AUDIT / "manifest.json",
                 audit.team_dir() / name[:7] / f"{name}.jsonl": audit.team_dir() / "manifest.json"}
        self.assertTrue(all(f.is_file() for f in files))
        real, seen = Path.unlink, []

        def unlink(path, *a, **kw):
            if path in files:  # does the manifest still list the segment the moment its file goes?
                seen.append(name in [e["name"] for e in json.loads(files[path].read_text())["units"]])
            return real(path, *a, **kw)
        with mock.patch.object(Path, "unlink", unlink):
            audit.prune()
        self.assertEqual(seen, [False, False])
        self.assertFalse(any(f.exists() for f in files))


class ResealBeforePublishTests(SegmentCase):
    sealed_record = ResealTests.sealed_record

    def test_a_reseal_before_the_first_publish_leaves_a_handoff_copy_that_verifies(self):
        audit._append(self.sealed_record(noon(2), "the Acme roadmap again"), now=noon(2))
        self.write(noon(2) + 5, 1)
        self.write(noon(1), 1)
        audit.close()
        self.assertFalse(audit.team_dir().exists())
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertTrue(res["ok"], res)
        for team in (False, True):
            v = audit.verify(team=team)
            self.assertTrue(v["ok"], v["first"])
        copy = audit.team_dir() / "reseal" / f"{res['id']}.json"
        self.assertEqual(copy.read_bytes() if copy.is_file() else None, Path(res["map"]).read_bytes())
        audit.AUDIT = self.elsewhere  # another Mac checks the handoff copy alone
        v = audit.verify()
        self.assertTrue(v["ok"], v["first"])


class StartTests(SegmentCase):
    # a manifest's `start`: where the chain begins once the segments before it were pruned or never published
    def cut_head(self, folder: Path, published: bool):
        # remove the oldest segment and its manifest entry, and name it as `start`, as a prune would have
        mf = folder / "manifest.json"
        m = json.loads(mf.read_text())
        head = m["units"][0]
        (folder / head["name"][:7] / (f"{head['name']}.jsonl" if published else f"{head['name']}.jsonl.gz")).unlink()
        m["units"] = m["units"][1:]
        m["start"] = {"unit": head["name"], "link": head["link"]}
        mf.write_text(json.dumps(m))
        return head["name"]

    def test_verify_refuses_a_start_that_no_prune_record_names(self):
        self.write(noon(4), 1)
        self.write(noon(3), 1)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        name = self.cut_head(audit.AUDIT, published=False)
        res = audit.verify(team=False)
        self.assertFalse(res["ok"])
        self.assertEqual(res["first"]["kind"], "start")
        self.assertIn(name, res["first"]["problem"])

    def test_a_handoff_copy_begun_after_older_segments_verifies_until_its_head_is_cut(self):
        old = noon(0, time.time() - 4 * 31 * DAY)  # older than the three months publish copies
        self.write(old, 1)
        self.write(noon(3), 1)
        self.write(noon(2), 1)
        audit.close(now=noon(0))
        audit.publish()
        self.assertEqual(json.loads((audit.team_dir() / "manifest.json").read_text())["start"]["unit"],
                         f"{utc_day(old)}.01")
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        here, audit.AUDIT = audit.AUDIT, self.elsewhere  # another Mac, before this one published what backs it
        with mock.patch.object(audit, "team_dir", return_value=core.HANDOFF / "audit" / "ana@m-another"):
            res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        self.assertTrue(any("not published yet" in n["note"] for n in res["notes"]), res["notes"])
        audit.AUDIT = here
        self.cut_head(audit.team_dir(), published=True)
        res = audit.verify()
        self.assertFalse(res["ok"])
        self.assertEqual([p["kind"] for p in res["problems"]], ["start"])


class LinkTests(SegmentCase):
    # a symbolic link committed to the handoff repository, in this Mac's copy, pointing outside it
    sealed_record = ResealTests.sealed_record
    scene = ResealTests.scene

    def outside(self) -> Path:
        d = self.root / "outside"
        d.mkdir(exist_ok=True)
        return d

    def files_of(self, folder: Path) -> dict:
        return {p.name: p.read_bytes() for p in folder.iterdir()}

    def test_publish_writes_nothing_through_a_symbolic_link_and_records_why(self):
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        audit.team_dir().mkdir(parents=True)
        (audit.team_dir() / utc_day(noon(1))[:7]).symlink_to(self.outside())
        self.assertEqual(audit.publish(now=noon(0)), [])
        self.assertEqual(self.files_of(self.outside()), {})
        rec = audit.query(action="audit.publish")[-1]
        self.assertEqual((rec["outcome"], rec["severity"]), ("blocked", "critical"))
        self.assertIn("symbolic link", rec["detail"])

    def test_a_reseal_refuses_a_handoff_copy_with_a_symbolic_link(self):
        self.scene()
        month = audit.team_dir() / utc_day(noon(2))[:7]
        outside = self.outside()
        for p in month.iterdir():
            (outside / p.name).write_bytes(p.read_bytes())
            p.unlink()
        month.rmdir()
        month.symlink_to(outside)
        before = self.files_of(outside)
        with self.as_developer():
            res = audit.reseal(apply=True)
        self.assertFalse(res["ok"])
        self.assertIn("symbolic link", res["refused"])
        self.assertEqual(self.files_of(outside), before)
        self.assertFalse((audit.AUDIT / "reseal").exists())

    def test_prune_removes_nothing_through_a_symbolic_link(self):
        old = audit.months_back(time.time(), 16)
        outside = self.outside()
        (outside / f"{old}-14.jsonl").write_text("{}\n")
        audit.team_dir().mkdir(parents=True)
        (audit.team_dir() / old).symlink_to(outside)
        out = audit.prune()
        self.assertEqual(list(self.files_of(outside)), [f"{old}-14.jsonl"])
        self.assertEqual(out["team"], [])


class ResealGapTests(SegmentCase):
    # a segment edited after the reseal's verify passed, before the reseal reads it
    sealed_record = ResealTests.sealed_record
    scene = ResealTests.scene

    def reseal_after(self, edit) -> dict:
        real = audit.verify

        def verify_then_edit(*a, **kw):
            res = real(*a, **kw)
            edit()
            return res
        with mock.patch.object(audit, "verify", side_effect=verify_then_edit), self.as_developer():
            return audit.reseal(apply=True)

    def edit_second_record(self, rehash: bool):
        # the record after the one the reseal cleans, in the segment of two days ago
        gz = self.closed(noon(2))
        lines = gzip.decompress(gz.read_bytes()).splitlines(keepends=True)
        rec = json.loads(lines[1])
        rec["target"] = "add 5"
        if rehash:
            rec["hash"] = audit.digest(rec)
        lines[1] = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        gz.write_bytes(gzip.compress(b"".join(lines)))

    def assert_refused(self, res: dict):
        self.assertFalse(res["ok"])
        self.assertIn("changed after", res["refused"])
        self.assertFalse((audit.AUDIT / "reseal").exists())
        self.assertIn('"add 5"', gzip.decompress(self.closed(noon(2)).read_bytes()).decode())
        rec = audit.query(action="audit.reseal")[-1]
        self.assertEqual((rec["outcome"], rec["severity"]), ("blocked", "critical"))
        self.assertFalse(audit.verify()["ok"])

    def test_a_record_edited_after_the_verify_is_refused_not_sealed_again(self):
        self.scene()
        self.assert_refused(self.reseal_after(lambda: self.edit_second_record(rehash=False)))

    def test_a_record_rewritten_with_its_hash_after_the_verify_is_refused(self):
        self.scene()
        self.assert_refused(self.reseal_after(lambda: self.edit_second_record(rehash=True)))


class CommandTests(SegmentCase):
    def test_dev_audit_parses_reseal_and_apply(self):
        from devkit import cli
        a = cli.build_parser().parse_args(["audit", "reseal", "--apply"])
        self.assertEqual((a.audit_cmd, a.apply), ("reseal", True))


class ManifestIndexTests(SegmentCase):
    # the manifest's index (time span, days, actions, v1 lines, head, kind) lets readers skip closed segments, so an
    # edited index must fail verify, and the embargo check must never trust it to skip a segment
    def embargo_scene(self) -> dict:
        rec = audit.build("git.embargo", "fix/unshipped", "set: an unshipped security fix", "ok",
                          {"project": "acme/app", "branch": "fix/unshipped"}, now=noon(3))
        audit._append(rec, now=noon(3))
        self.write(noon(3) + 5, 1)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        return rec

    def edit_index(self, mf: Path):
        m = json.loads(mf.read_text())
        for e in m["units"]:
            e["actions"] = {"dev.task": e["records"]}
            e["lo"] = e["hi"] = 86400.0
        mf.write_text(json.dumps(m))

    def test_verify_names_the_segment_whose_manifest_index_was_edited(self):
        self.embargo_scene()
        mf = audit.AUDIT / "manifest.json"
        keep = mf.read_bytes()
        edits = {"lo": lambda e: e["lo"] - 3600, "hi": lambda e: e["hi"] + 3600, "days": lambda e: {},
                 "actions": lambda e: {"dev.task": e["records"]}, "v1": lambda e: 1, "head": lambda e: "0" * 64,
                 "kind": lambda e: "month"}
        for field, change in edits.items():
            with self.subTest(field=field):
                m = json.loads(keep)
                m["units"][0][field] = change(m["units"][0])
                mf.write_text(json.dumps(m))
                res = audit.verify(team=False)
                self.assertFalse(res["ok"])
                self.assertEqual((res["first"]["kind"], res["first"]["path"]), ("manifest", str(self.closed(noon(3)))))
                self.assertIn(field, res["first"]["problem"])
                mf.write_bytes(keep)
        self.assertTrue(audit.verify(team=False)["ok"])

    def test_an_embargo_is_found_when_the_manifest_index_says_its_segment_holds_none(self):
        from devkit import branches
        self.embargo_scene()
        audit.publish()
        self.edit_index(audit.AUDIT / "manifest.json")
        self.edit_index(audit.team_dir() / "manifest.json")
        branches._TRAIL_CACHE.clear()
        self.assertIn("fix/unshipped", branches.embargo_trail("acme/app"))
        audit.AUDIT = self.elsewhere  # another Mac, from the handoff copy alone
        branches._TRAIL_CACHE.clear()
        self.assertIn("fix/unshipped", branches.embargo_trail("acme/app"))


class InterruptedCloseTests(SegmentCase):
    # the job can stop anywhere in a close: after it wrote the compressed file, or after it listed it in the manifest
    def test_a_close_stopped_before_the_manifest_was_written_finishes_on_the_next_run(self):
        recs = self.write(noon(1), 2)
        self.write(noon(0), 1)
        with mock.patch.object(audit, "_write_manifest", side_effect=OSError("no space left on device")):
            with self.assertRaises(OSError):
                audit.close(now=noon(0))
        self.assertTrue(self.closed(noon(1)).is_file())
        self.assertTrue(self.seg(noon(1)).is_file())
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(audit.close(now=noon(0)), [self.closed(noon(1))])
        self.assertFalse(self.seg(noon(1)).exists())
        self.assertEqual([u["name"] for u in self.manifest()["units"]], [f"{utc_day(noon(1))}.01"])
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 3)
        self.assertEqual([r["id"] for r in audit.query(days=30) if r["action"] == "dev.task"][:2],
                         [r["id"] for r in recs])

    def stop_after_the_manifest(self):
        # the job is killed right after it wrote the manifest, before it removed the plain file
        real = audit._write_manifest

        def write_then_stop(*a, **kw):
            real(*a, **kw)
            raise KeyboardInterrupt("the job was killed")
        return mock.patch.object(audit, "_write_manifest", side_effect=write_then_stop)

    def test_a_close_stopped_after_the_manifest_was_written_removes_the_plain_copy_on_the_next_run(self):
        self.write(noon(1), 2)
        with self.stop_after_the_manifest(), self.assertRaises(KeyboardInterrupt):
            audit.close(now=noon(0))
        self.assertTrue(self.seg(noon(1)).is_file())
        audit.close(now=noon(0))
        self.assertFalse(self.seg(noon(1)).exists())
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 2)

    def test_a_record_after_a_close_stopped_after_the_manifest_goes_to_a_new_segment(self):
        now = noon(0)
        first = self.write(now, 2)
        with self.stop_after_the_manifest(), self.assertRaises(KeyboardInterrupt):
            audit.close(now=now + 60, force=True)
        late = self.write(now + 120, 1)
        self.assertEqual([r["id"] for r in self.stored(self.seg(now, 2))], [late[0]["id"]])
        self.assertEqual(late[0]["prev"], first[-1]["hash"])
        self.assertFalse(self.seg(now, 1).exists())
        audit.close(now=now + DAY)
        res = audit.verify(team=False)
        self.assertTrue(res["ok"], res["first"])
        self.assertEqual(res["records"], 3)


class ResealPruneTests(SegmentCase):
    sealed_record = ResealTests.sealed_record
    scene = ResealTests.scene

    def test_prune_retires_a_reseal_map_with_the_record_that_backs_it(self):
        self.scene()
        with self.as_developer():
            rid = audit.reseal(apply=True)["id"]
        maps = [audit.AUDIT / "reseal" / f"{rid}.json", audit.team_dir() / "reseal" / f"{rid}.json"]
        self.assertTrue(all(f.is_file() for f in maps))
        later = noon(0) + 14 * 31 * DAY
        self.write(later, 1)
        audit.close(now=later + DAY)
        out = audit.prune(now=later + DAY)
        self.assertEqual([f.is_file() for f in maps], [False, False])
        self.assertIn(maps[1], out["team"])
        rec = audit.query(action="audit.prune")[-1]
        self.assertIn(rid, rec["detail"])
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])
        with self.as_developer():
            self.assertTrue(audit.reseal(apply=False)["ok"])


class LatePublishTests(SegmentCase):
    def test_a_handoff_copy_begun_after_a_local_prune_starts_where_the_local_trail_starts(self):
        now = time.time()
        self.write(noon(0, now - 16 * 31 * DAY), 2)
        self.write(noon(3), 1)
        self.write(noon(2), 1)
        audit.close(now=noon(0))
        core.CFG["audit"]["publish"] = False
        audit.prune(now=now)
        core.CFG["audit"]["publish"] = True
        audit.publish()
        team = json.loads((audit.team_dir() / "manifest.json").read_text())
        self.assertEqual(team["start"], self.manifest()["start"])
        res = audit.verify()
        self.assertTrue(res["ok"], res["first"])


class ForwardTests(SegmentCase):
    def setUp(self):
        super().setUp()
        self.sink = self.root / "forwarded.jsonl"
        core.CFG["audit"]["forward"] = str(self.sink)

    def forwarded(self) -> list:
        return [json.loads(ln) for ln in self.sink.read_text().splitlines()] if self.sink.is_file() else []

    def test_the_jobs_daily_summary_is_forwarded(self):
        for _ in range(3):
            audit.record("dev.sync", ["--quiet"], "", "ok")
        audit.close(now=time.time() + DAY)
        sums = [r for r in self.forwarded() if (r.get("ref") or {}).get("type") == "summary"]
        self.assertEqual([(r["action"], r["evidence"]["count"]) for r in sums], [("dev.sync", 3)])

    def test_a_summary_written_when_a_new_day_starts_is_forwarded(self):
        self.write(noon(1), 1)
        for i in range(2):
            audit._count_quiet(audit.build("handoff.push", "acme/handoff", "1 commit(s)", "ok", now=noon(1) + i))
        today = audit.record("dev.task", "today")
        got = [(r["action"], (r.get("evidence") or {}).get("count")) for r in self.forwarded()]
        self.assertIn(("handoff.push", 2), got)
        self.assertEqual(self.forwarded()[-1]["id"], today["id"])


class LockCase(SegmentCase):
    def locked(self, path: Path) -> bool:
        # does another open of the file wait on its lock? (flock locks of two opens conflict, in one process too)
        import fcntl
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False


class MonthLockTests(LockCase):
    def test_closing_a_month_file_holds_its_lock_until_the_file_is_removed(self):
        last_month = audit.months_back(time.time(), 1)
        y, m = int(last_month[:4]), int(last_month[5:])
        month = audit.AUDIT / f"{last_month}.jsonl"
        self.old_append(time.mktime((y, m, 14, 12, 0, 0, 0, 0, -1)))
        audit.record("dev.task", "new")
        real, seen = audit._write_manifest, []

        def write(*a, **kw):
            seen.append(self.locked(month))  # an earlier devkit's writer would wait here, not append
            return real(*a, **kw)
        with mock.patch.object(audit, "_write_manifest", side_effect=write):
            self.assertEqual(audit.close(), [audit.AUDIT / last_month / f"{last_month}.jsonl.gz"])
        self.assertEqual(seen, [True])
        self.assertFalse(month.exists())


class PruneLockTests(LockCase):
    def test_prune_writes_the_manifest_under_the_append_lock(self):
        self.write(noon(0, time.time() - 16 * 31 * DAY), 2)
        self.write(noon(1), 1)
        audit.close(now=noon(0))
        real, seen = audit._write_manifest, []

        def write(path, *a, **kw):
            if Path(path) == audit.AUDIT / "manifest.json":
                seen.append(self.locked(audit.AUDIT / ".lock"))  # a writer naming the origin waits for it
            return real(path, *a, **kw)
        with mock.patch.object(audit, "_write_manifest", side_effect=write):
            self.assertEqual(len(audit.prune()["removed"]), 1)
        self.assertEqual(seen, [True])


if __name__ == "__main__":
    unittest.main()
