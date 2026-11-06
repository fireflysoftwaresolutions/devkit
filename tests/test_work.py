# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Work items, locks, history, plans and handoff isolation. Run: python3 -m unittest discover -s tests -v"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, guard, plans, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
B = {"user": "bob", "host": "mbp2", "agent": "codex", "session": "s2"}


class TempHandoff:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / ".handoff"
        root.mkdir()
        self.saved = (core.HANDOFF, work.HANDOFF, work.ITEMS, work.SESSIONS, work.PRESENCE, plans.PLANS,
                      plans.HANDOFF)
        core.HANDOFF = work.HANDOFF = plans.HANDOFF = root
        work.ITEMS, work.SESSIONS, work.PRESENCE = root / "items", root / "sessions", root / "presence"
        plans.PLANS = root / "plans"
        self.root = root
        return self

    def __exit__(self, *exc):
        (core.HANDOFF, work.HANDOFF, work.ITEMS, work.SESSIONS, work.PRESENCE, plans.PLANS, plans.HANDOFF) = self.saved
        self.tmp.cleanup()


class ItemTests(unittest.TestCase):
    def test_create_versions_and_reasons(self):
        with TempHandoff():
            it = work.create("acme/api", "Export invoices", why="customer request", a=A)
            self.assertTrue(it["path"].exists())
            self.assertEqual(it["meta"]["version"], 1)
            self.assertIn("customer request", it["changelog"][0])
            with self.assertRaises(ValueError):
                work.update(work.load(it["path"]), "edit", a=A, title="Export invoices as CSV")
            it = work.update(work.load(it["path"]), "edit", why="scope: CSV only", a=A, title="Export invoices as CSV")
            self.assertEqual(it["meta"]["version"], 2)
            self.assertIn("why: scope: CSV only", it["changelog"][0])
            again = work.load(it["path"])
            self.assertEqual(again["meta"]["title"], "Export invoices as CSV")
            self.assertEqual(len(again["changelog"]), 2)

    def test_locks_between_people(self):
        with TempHandoff():
            it = work.create("acme/api", "Fix login", a=A)
            it = work.claim(it, a=A)
            self.assertEqual(it["meta"]["status"], "doing")
            self.assertTrue(it["meta"]["claimed_by"].startswith("ana@mbp1"))
            with self.assertRaises(ValueError):
                work.claim(work.load(it["path"]), a=B)
            with self.assertRaises(ValueError):
                work.release(work.load(it["path"]), a=B)
            with self.assertRaises(ValueError):
                work.update(work.load(it["path"]), "edit", why="x", a=B, title="Hijack")
            it = work.release(work.load(it["path"]), a=A, status="done")
            self.assertEqual(it["meta"]["status"], "done")
            self.assertEqual(it["meta"]["claimed_by"], "")

    def test_expired_lock_can_be_taken(self):
        with TempHandoff():
            it = work.claim(work.create("acme/api", "Stale", a=A), a=A)
            it["meta"]["lock_expires"] = work.now_iso(time.time() - 60)
            it["path"].write_text(work.render(it))
            taken = work.claim(work.load(it["path"]), a=B)
            self.assertTrue(taken["meta"]["claimed_by"].startswith("bob@mbp2"))
            self.assertEqual(len(work.expire_locks()), 0)

    def test_find_and_summary(self):
        with TempHandoff():
            work.create("acme/api", "Later thing", bucket="later", a=A)
            now = work.create("acme/api", "Now thing", bucket="now", priority="high", a=A)
            self.assertEqual(work.find("1", "acme/api")["meta"]["id"], now["meta"]["id"])
            self.assertEqual(work.find(now["meta"]["id"], "acme/api")["meta"]["id"], now["meta"]["id"])
            self.assertEqual(work.find("later", "acme/api")["meta"]["title"], "Later thing")
            work.release(work.load(now["path"]), a=A, status="done")
            s = work.summary("acme/api")
            self.assertEqual((s["counts"]["open"], s["counts"]["done"]), (1, 1))
            self.assertEqual(work.progress("acme/api"), (50, "tasks"))

    def test_unique_ids(self):
        ids = {work.new_id() for _ in range(2000)}
        self.assertEqual(len(ids), 2000)

    def test_session_log_is_append_only(self):
        with TempHandoff():
            work.log_session("acme/api", "first")
            work.log_session("acme/api", "second")
            self.assertEqual(work.recent_sessions("acme/api")[:2], ["second", "first"])


class PlanTests(unittest.TestCase):
    def test_versioned_plan(self):
        with TempHandoff():
            path = plans.new("acme/api", "Invoice export", a=A)
            self.assertFalse(plans.needs_revision(path))
            path.write_text(path.read_text().replace("## Goal", "## Goal\nExport CSV and XLSX."))
            self.assertTrue(plans.needs_revision(path))
            with self.assertRaises(ValueError):
                plans.revise(path, "", a=A)
            meta = plans.revise(path, "customer asked for XLSX too", a=A)
            self.assertEqual(int(meta["version"]), 2)
            self.assertFalse(plans.needs_revision(path))
            self.assertIn("why: customer asked for XLSX too", path.read_text())

    def test_raw_plan_is_adopted(self):
        with TempHandoff() as h:
            raw = h.root / "plans" / "acme" / "api" / "raw.md"
            raw.parent.mkdir(parents=True)
            raw.write_text("# Raw plan\n\nSteps.\n")
            self.assertTrue(plans.needs_revision(raw))
            meta = plans.revise(raw, "written by brainstorming", a=A)
            self.assertEqual((meta["status"], int(meta["version"])), ("draft", 1))
            self.assertFalse(plans.needs_revision(raw))


class TemplateTests(unittest.TestCase):
    def test_task_bodies_by_type(self):
        body = work.task_body("bug", "Login times out on Safari", ["Safari login works", "regression test added"])
        self.assertIn("## Symptom\n\nLogin times out on Safari", body)
        self.assertIn("- [ ] Safari login works\n- [ ] regression test added", body)
        self.assertIn("## Steps to reproduce", body)
        for kind, section in (("feature", "## Why"), ("spike", "## Timebox"), ("task", "## Done when")):
            with self.subTest(kind=kind):
                self.assertIn(section, work.task_body(kind))

    def test_plan_kinds(self):
        for kind, section in (("plan", "## Non-goals"), ("spec", "## Acceptance criteria"),
                              ("spike", "## Recommendation"), ("bugfix", "## Root cause"), ("adr", "## Consequences")):
            with self.subTest(kind=kind):
                text = plans.template(kind, "Invoice export")
                self.assertTrue(text.startswith("# Invoice export"))
                self.assertIn(section, text)
        with self.assertRaises(ValueError):
            plans.template("novel", "x")
        with TempHandoff():
            path = plans.new("acme/api", "Event store", a=A, kind="adr")
            self.assertIn("kind: adr", path.read_text())
            self.assertFalse(plans.needs_revision(path))

    def test_delegate_message(self):
        from devkit import cli
        with TempHandoff():
            it = work.create("acme/api", "Export invoices as CSV", a=A, due="2026-11-20",
                             body="Finance copies invoices by hand.\n\nDone when: Finance exported October")
            msg = cli.delegate_message(it, "maria", "", "acme/api")
            self.assertTrue(msg.startswith("Hi @maria, could you take this task"))
            self.assertIn("Done means: Finance exported October", msg)
            self.assertIn("Deadline: 2026-11-20", msg)
            self.assertIn(f"dev task start {it['meta']['id']} -p acme/api", msg)

    def test_scope_flags_before_or_after_the_verb(self):
        from devkit import cli
        ap = cli.build_parser()
        for argv in (["task", "done", "3", "-p", "acme/x"], ["task", "-p", "acme/x", "done", "3"]):
            a = ap.parse_args(argv)
            self.assertEqual((a.project, a.task_cmd), ("acme/x", "done"))
        a = ap.parse_args(["task", "delegate", "T-1", "--to", "maria", "-i", "lending"])
        self.assertEqual((a.initiative, a.to), ("lending", "maria"))
        self.assertEqual(ap.parse_args(["plan", "new", "X", "-p", "a/b", "--kind", "adr"]).project, "a/b")

    def test_report_health_rules(self):
        from devkit import report
        self.assertEqual(report.health("blocked", "", 50, {}, 0)[0], "red")
        self.assertEqual(report.health("active", "2000-01-01", 50, {}, 0)[0], "red")
        self.assertEqual(report.health("active", "", 50, {"blocked": 2}, 0)[0], "amber")
        self.assertEqual(report.health("active", "", 50, {}, time.time())[0], "green")
        self.assertEqual(report.health("done", "", 100, {}, 0)[0], "done")


class SyncRequestTests(unittest.TestCase):
    def test_events_share_one_queued_sync(self):
        import os
        import subprocess
        spawned = []
        saved = (work.SYNC_PENDING, subprocess.Popen, os.environ.pop("DEVKIT_NO_AUTOSYNC", None))
        with tempfile.TemporaryDirectory() as d:
            work.SYNC_PENDING = Path(d) / "sync.pending"
            subprocess.Popen = lambda *a, **k: spawned.append(a[0])
            try:
                self.assertTrue(work.request_sync("commit abc"))
                self.assertFalse(work.request_sync("push"))
                self.assertFalse(work.request_sync("task done"))
                self.assertEqual(len(spawned), 1)
                self.assertIn("--delay", spawned[0])
                self.assertEqual(work.SYNC_PENDING.read_text().splitlines(), ["commit abc", "push", "task done"])
            finally:
                work.SYNC_PENDING, subprocess.Popen, env = saved
                if env is not None:
                    os.environ["DEVKIT_NO_AUTOSYNC"] = env

    def test_sync_job_and_bulk_imports_do_not_request(self):
        import os
        os.environ["DEVKIT_NO_AUTOSYNC"] = "1"
        try:
            self.assertFalse(work.request_sync("bulk"))
        finally:
            del os.environ["DEVKIT_NO_AUTOSYNC"]


class IsolationTests(unittest.TestCase):
    def setUp(self):  # the fixture repos live in a system temp dir; treat them as real projects here
        self._is_temp = guard.is_temp
        guard.is_temp = lambda p: False

    def tearDown(self):
        guard.is_temp = self._is_temp

    def test_handoff_isolation(self):
        with TempHandoff() as h, tempfile.TemporaryDirectory() as repo:
            core.sh(["git", "init", "-q", repo])
            core.sh(["git", "-C", repo, "remote", "add", "origin", "https://github.com/acme/one.git"])
            init = h.root / "initiatives" / "platform" / "README.md"
            init.parent.mkdir(parents=True)
            init.write_text("---\nkind: initiative\ntitle: Platform\nprojects: [acme/one, acme/three]\n---\n# Platform\n")
            check = lambda rel: guard.check("Write", {"file_path": str(h.root / rel)}, repo, "claude")  # noqa: E731
            self.assertIsNone(check("projects/acme/one.md"))
            self.assertEqual(check("projects/acme/two.md")[0], "deny")
            self.assertIsNone(check("plans/acme/one/design.md"))
            self.assertEqual(check("plans/acme/two/design.md")[0], "deny")
            self.assertIsNone(check("initiatives/platform/README.md"))
            self.assertEqual(check("initiatives/other/README.md")[0], "deny")
            for rel in ("items/acme/one/T-x.md", "sessions/acme/one/2026-11.md", "presence/ana@mbp1.json",
                        "PROJECTS.md", "registry.json"):
                with self.subTest(rel=rel):
                    self.assertEqual(check(rel)[0], "deny")
            portfolio = guard.check("Write", {"file_path": str(h.root / "projects/acme/two.md")}, str(h.root), "claude")
            self.assertIsNone(portfolio)

    def test_cross_project_dev_commands(self):
        with TempHandoff() as h, tempfile.TemporaryDirectory() as repo:
            core.sh(["git", "init", "-q", repo])
            core.sh(["git", "-C", repo, "remote", "add", "origin", "https://github.com/acme/one.git"])
            bash = lambda cmd: guard.check("Bash", {"command": cmd}, repo, "claude")  # noqa: E731
            self.assertEqual(bash("dev task add x -p acme/two")[0], "deny")
            self.assertIsNone(bash("dev task add x -p acme/one"))
            self.assertIsNone(bash("DEVKIT_CROSS_PROJECT=1 dev task add x -p acme/two"))
            self.assertEqual(bash("dev initiative add platform acme/one")[0], "deny")


if __name__ == "__main__":
    unittest.main()
