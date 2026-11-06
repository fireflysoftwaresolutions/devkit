# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Scheduled jobs (launchd plists, schedules, runs, journal) and the audit trail."""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, jobs  # noqa: E402


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (jobs.JOURNAL, jobs.LOGS, core.CONFIG_FILE, dict(core.CFG.get("jobs") or {}), jobs.install,
                      audit.AUDIT, core.CFG.get("sync"))
        core.CFG["sync"] = {}
        jobs.JOURNAL, jobs.LOGS = root / "journal.jsonl", root / "logs"
        core.CONFIG_FILE = root / "config.json"
        core.CFG["jobs"] = {}
        jobs.install = lambda quiet=False: []
        audit.AUDIT = root / "audit"

    def tearDown(self):
        jobs.JOURNAL, jobs.LOGS, core.CONFIG_FILE, j, jobs.install, audit.AUDIT, sync = self.saved
        core.CFG["jobs"] = j
        core.CFG["sync"] = sync
        self.tmp.cleanup()

    def test_default_jobs_and_plists(self):
        js = jobs.jobs()
        self.assertTrue({"sync", "sleep", "gc", "repair", "audit", "doctor", "lessons"} <= set(js))
        p = jobs.plist_for("sleep", js["sleep"])
        self.assertEqual(p["Label"], "com.devkit.job.sleep")
        self.assertEqual(p["StartCalendarInterval"], {"Hour": 3, "Minute": 30})
        self.assertEqual(p["ProgramArguments"][-3:], ["jobs", "exec", "sleep"])
        self.assertEqual(jobs.plist_for("sync", js["sync"])["StartInterval"], 3600)
        self.assertEqual(jobs.plist_for("doctor", js["doctor"])["StartCalendarInterval"]["Weekday"], 1)

    def test_wizard_sync_interval_sets_the_sync_job(self):
        core.CFG["sync"] = {"interval_minutes": 30}
        self.assertEqual(jobs.plist_for("sync", jobs.jobs()["sync"])["StartInterval"], 1800)
        core.CFG["jobs"] = {"sync": {"every": 15}}  # an explicit job schedule wins
        self.assertEqual(jobs.jobs()["sync"]["every"], 15)

    def test_set_schedule_persists_and_validates(self):
        j = jobs.set_schedule("gc", at="02:15", why="quieter hour")
        self.assertEqual((j.get("at"), j.get("every")), ("02:15", None))
        self.assertEqual(json.loads(core.CONFIG_FILE.read_text())["jobs"]["gc"]["at"], "02:15")
        self.assertEqual(jobs.schedule_text(jobs.set_schedule("gc", every="2h")), "every 2 h")
        self.assertEqual(jobs.schedule_text(jobs.set_schedule("gc", weekly="Sunday 07:05")), "weekly, sun 07:05")
        self.assertFalse(jobs.set_schedule("gc", enabled=False)["enabled"])
        for bad in ({"at": "25:00"}, {"every": "soon"}, {"weekly": "someday 9"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                jobs.set_schedule("gc", **bad)
        with self.assertRaises(ValueError):
            jobs.set_schedule("nope", at="01:00")
        self.assertTrue(any(r["action"] == "jobs.set" for r in audit.records(1)))

    def test_execute_records_journal_log_and_audit(self):
        core.CFG["jobs"] = {"probe": {"cmd": ["version"], "every": 5, "about": "test", "notify": "never"}}
        rc = jobs.execute("probe")
        self.assertEqual(rc, 0)
        run = jobs.last_run("probe")
        self.assertEqual(run["rc"], 0)
        self.assertTrue((jobs.LOGS / "probe.log").read_text().startswith("\n=== "))
        self.assertTrue(any(r["action"] == "job.probe" for r in audit.records(1)))

    def test_next_run(self):
        now = time.mktime((2026, 11, 5, 22, 0, 0, 0, 0, -1))  # a Thursday
        nr = jobs.next_run({"enabled": True, "at": "03:30"}, now)
        self.assertEqual(time.localtime(nr)[2:5], (6, 3, 30))
        nr = jobs.next_run({"enabled": True, "weekly": "mon 09:00"}, now)
        self.assertEqual(time.localtime(nr)[2:5], (9, 9, 0))
        self.assertIsNone(jobs.next_run({"enabled": False, "every": 5}, now))


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = audit.AUDIT
        audit.AUDIT = Path(self.tmp.name) / "audit"

    def tearDown(self):
        audit.AUDIT = self.saved
        self.tmp.cleanup()

    def test_record_redacts_and_filters(self):
        fake = "ghp_" + "a" * 36
        audit.record("dev.push", f"push with {fake}", "password=hunter2", "ok")
        audit.record("guard.deny", "rm -rf src", "steered to dev rm", "deny")
        rs = audit.records(1)
        self.assertEqual(len(rs), 2)
        self.assertNotIn(fake, json.dumps(rs))
        self.assertNotIn("hunter2", json.dumps(rs))
        self.assertEqual([r["action"] for r in audit.records(1, action="guard")], ["guard.deny"])

    def test_only_changing_commands_are_audited(self):
        for argv in (["task", "add", "x"], ["rm", "a.txt"], ["release"], ["jobs", "set", "gc", "--at", "01:00"],
                     ["lesson", "add", "x"], ["gc", "--apply"], ["sleep"]):
            with self.subTest(argv=argv):
                self.assertTrue(audit.audited(argv))
        for argv in (["ls"], ["status"], ["task"], ["task", "ls"], ["files"], ["jobs"], ["jobs", "logs", "gc"],
                     ["lesson", "ls"], ["gc"], ["sleep", "--dry-run"], ["rm", "--dry-run", "x"], ["audit"]):
            with self.subTest(argv=argv):
                self.assertFalse(audit.audited(argv))


if __name__ == "__main__":
    unittest.main()
