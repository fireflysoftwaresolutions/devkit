# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Work items, locks, history, plans and handoff isolation. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import io
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, core, guard, plans, work  # noqa: E402

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
    def test_a_finished_item_reopens_only_with_a_reason(self):
        with TempHandoff():
            it = work.create("acme/api", "Export invoices", why="customer request", a=A)
            it = work.release(work.load(it["path"]), why="shipped", status="done", a=A)
            with self.assertRaises(ValueError):
                work.reopen(work.load(it["path"]), "", a=A)  # a reason is required
            it = work.reopen(work.load(it["path"]), "closed too early: the review is still open", a=A)
            self.assertEqual(it["meta"]["status"], "todo")
            self.assertIn("why: closed too early: the review is still open", it["changelog"][0])
            with self.assertRaises(ValueError):
                work.reopen(work.load(it["path"]), "again", a=A)  # an open item has nothing to reopen

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

    # An id is the time in seconds since 2026 and 6 random base36 characters. Ids drawn in the same second differ
    # only by those 6 (36**6 values), so 2000 of them collide in about 1 run of 1,100 (the birthday bound): the old
    # test_unique_ids failed that way once on Python 3.9 (1999 of 2000). What new_id guarantees is the scope check.
    SECOND = work.EPOCH + 36 ** 4  # 2026-01-20T10:33:36Z: the time part is "10000"

    def test_ids_drawn_in_one_second_share_the_time_and_differ_in_their_six_random_characters(self):
        with mock.patch.object(work.time, "time", lambda: self.SECOND):
            ids = [work.new_id() for _ in range(2000)]
        self.assertEqual([i for i in ids if not re.fullmatch(r"T-10000[0-9a-z]{6}", i)], [])
        self.assertGreaterEqual(len(set(ids)), 1998)  # three collisions or more: about 1 run in 10**10

    def test_a_new_id_never_takes_one_already_used_in_its_scope(self):
        draws = iter("aaaaaa" "bbbbbb")
        with TempHandoff(), mock.patch.object(work.time, "time", lambda: self.SECOND), \
                mock.patch("secrets.choice", lambda alphabet: next(draws)):
            taken = work.item_path("acme/api", "T-10000aaaaaa")
            taken.parent.mkdir(parents=True)
            taken.write_text("---\nid: T-10000aaaaaa\n---\n")
            self.assertEqual(work.new_id("task", "acme/api"), "T-10000bbbbbb")

    def test_session_log_is_append_only(self):
        with TempHandoff():
            work.log_session("acme/api", "first")
            work.log_session("acme/api", "second")
            self.assertEqual(work.recent_sessions("acme/api")[:2], ["second", "first"])


class ItemRecordTests(unittest.TestCase):
    def test_an_item_body_keeps_text_that_looks_like_record_structure(self):
        shapes = {
            "a fenced changelog heading in the middle":
                "Intro.\n\n```markdown\n## Changelog\n- 26.11.0 first release\n```\n\n## Done when\n- [ ] it ships",
            "a fenced changelog heading at the end": "The file:\n\n~~~\n## Changelog\n- v1 · first\n~~~",
            "two fenced changelog headings":
                "```text\n## Changelog\n```\n\nBetween.\n\n````md\n```\n## Changelog\n```\n````",
            "frontmatter-like lines": "Context.\n\n---\nstatus: done\nversion: 9\n---\n\nEnd.",
        }
        with TempHandoff():
            for shape, body in shapes.items():
                with self.subTest(shape):
                    it = work.create("acme/api", f"Item with {shape}", body=body, why="a reason", a=A)
                    work.update(work.load(it["path"]), "move", why="start now", a=A, bucket="now")
                    again = work.load(it["path"])
                    self.assertEqual(again["body"], body)
                    self.assertEqual([e.split(" · ")[0] for e in again["changelog"]], ["v2", "v1"])
                    self.assertEqual(again["meta"]["bucket"], "now")

    def test_a_changelog_entry_written_on_several_lines_is_kept(self):
        with TempHandoff():
            it = work.create("acme/api", "Export invoices", why="first line\nsecond line", a=A)
            work.update(work.load(it["path"]), "move", why="start now", a=A, bucket="now")
            log = work.load(it["path"])["changelog"]
            self.assertEqual(len(log), 2)
            self.assertTrue(log[1].endswith("why: first line\nsecond line"), log[1])

    def test_a_write_may_drop_only_the_text_it_edits(self):
        with TempHandoff():
            it = work.create("acme/api", "Export invoices", body="Context.", why="a reason", a=A)
            stale = work.load(it["path"])
            on_disk = it["path"].read_text().replace("Context.", "Context.\n\nAdded by hand after the load.")
            it["path"].write_text(on_disk)
            with self.assertRaises(ValueError) as cm:
                work.update(stale, "move", why="start now", a=A, bucket="now")
            self.assertIn("Added by hand after the load.", str(cm.exception))
            self.assertEqual(it["path"].read_text(), on_disk)
            # an edit replaces the body and the title it read, so it may drop their old lines
            work.update(work.load(it["path"]), "edit", why="CSV only", a=A, body="Only this.", title="Export CSV")
            again = work.load(it["path"])
            self.assertEqual((again["body"], again["title"]), ("Only this.", "Export CSV"))

    def test_a_task_whose_title_is_in_brackets_is_found_moved_and_finished(self):
        title = "[API] Export invoices [v2]"
        with TempHandoff():
            it = work.create("acme/api", title, why="a reason", a=A, links=["initiative:billing"])
            for selector in ("Export invoices", "[API]"):
                self.assertEqual(work.find(selector, "acme/api")["meta"]["id"], it["meta"]["id"])
            work.update(work.find("[API]", "acme/api"), "move", why="start now", a=A, bucket="now")
            work.release(work.load(it["path"]), a=A, status="done")
            again = work.load(it["path"])
            self.assertEqual((again["title"], again["meta"]["title"]), (title, title))
            self.assertEqual((again["meta"]["bucket"], again["meta"]["status"]), ("now", "done"))
            self.assertEqual(again["meta"]["links"], ["initiative:billing"])
            self.assertIn(f"\n# {title}\n", it["path"].read_text())

    def test_a_write_keeps_the_frontmatter_lines_devkit_does_not_know(self):
        extra = ["estimate: 3", "labels: [api, export]", "reviewers:", "  - bob", "# set by the sprint tool"]
        with TempHandoff():
            it = work.create("acme/api", "Export invoices", why="a reason", a=A)
            text = it["path"].read_text()
            it["path"].write_text(text.replace("\n---\n# ", "\n" + "\n".join(extra) + "\n---\n# ", 1))
            work.update(work.load(it["path"]), "move", why="start now", a=A, bucket="now")
            front, _ = work.split_frontmatter(it["path"].read_text())
            self.assertEqual([ln for ln in front.splitlines() if ln in extra], extra)
            self.assertEqual(work.load(it["path"])["meta"]["bucket"], "now")


class WriteRecordTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "record.md"

    def test_a_write_may_not_drop_lines_of_the_frontmatter(self):
        cases = {  # (the file on disk, the text a misread would write, the first line it would lose)
            "another tool's frontmatter":
                ("---\nauthor: ana\n---\n# Plan\n\nBody.\n", "---\nid: P-x\n---\n# Plan\n\nBody.\n", "author: ana"),
            "a first --- rule": ("---\n\n# Plan\n\nIntro.\n\n---\n\nMore.\n", "---\nid: P-x\n---\nMore.\n", "# Plan"),
            "a key devkit does not know": ("---\nid: T-x\nversion: 1\nestimate: 3\n---\n# Task\n",
                                           "---\nid: T-x\nversion: 2\n---\n# Task\n", "estimate: 3"),
        }
        for shape, (before, after, first) in cases.items():
            with self.subTest(shape):
                self.path.write_text(before)
                with self.assertRaises(work.RecordLossError) as cm:
                    work.write_record(self.path, after)
                self.assertIn(f"first “{first}”", str(cm.exception))
                self.assertEqual(self.path.read_text(), before)

    def test_a_reader_that_opened_the_record_before_a_write_reads_the_old_version_whole(self):
        old = "---\nid: T-x\nversion: 1\n---\n# Task\n\nOld body.\n"
        self.path.write_text(old)
        with open(self.path) as reader:
            work.write_record(self.path, old.replace("version: 1", "version: 2") + "\nNew line.\n")
            self.assertEqual(reader.read(), old)
        self.assertTrue(self.path.read_text().endswith("Old body.\n\nNew line.\n"))
        self.assertEqual([p.name for p in self.dir.iterdir()], ["record.md"])

    def test_a_write_that_fails_leaves_the_record_and_no_temporary_file(self):
        old = "---\nid: T-x\nversion: 1\n---\n# Task\n"
        self.path.write_text(old)
        with mock.patch.object(work.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                work.write_record(self.path, old.replace("version: 1", "version: 2"))
        self.assertEqual(self.path.read_text(), old)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["record.md"])


class LockJobReportTests(unittest.TestCase):
    def test_a_lock_job_reports_the_item_it_cannot_rewrite(self):
        real = work.render

        def misreading(item):  # a parser bug: a line of the body goes missing
            return real(item).replace("\nKeep this line.\n", "\n", 1)

        jobs = {"lock renewal": (1, lambda: work.renew_locks(A, hours=8), "lock-renewal-skipped"),
                "expired-lock release": (-1, work.expire_locks, "lock-release-skipped")}
        with TempHandoff() as h, mock.patch.object(audit, "AUDIT", h.root / "audit"), \
                mock.patch.object(audit, "policy_file", lambda: h.root / "no-policy.json"):
            for job, (hours, run, op) in jobs.items():
                with self.subTest(job):
                    it = work.create("acme/api", f"Item for the {job}", body="Context.\n\nKeep this line.", a=A)
                    it = work.claim(it, a=A, hours=hours)
                    before = it["path"].read_text()
                    err = io.StringIO()
                    with mock.patch.object(work, "render", misreading), contextlib.redirect_stderr(err):
                        self.assertFalse(run())  # nothing renewed or released
                    self.assertEqual(it["path"].read_text(), before)
                    self.assertIn(f"{job}: left ", err.getvalue())
                    self.assertIn(it["path"].name, err.getvalue())
                    self.assertIn("1 line(s)", err.getvalue())
                    # the background sync keeps its last log line as the job's audit detail, so the warning names the
                    # file and the count, never the item's text
                    self.assertNotIn("Keep this line", err.getvalue())
                    self.assertNotIn("Context.", err.getvalue())
                    recs = [r for r in audit.query(days=1, action="handoff.record-skipped")
                            if r["target"] == f"items/acme/api/{it['path'].name}"]
                    self.assertEqual([(r["outcome"], r["detail"]) for r in recs],
                                     [("blocked", f"{it['meta']['id']}: {op}")])


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
            (Path(d) / "logs").mkdir()
            state = mock.patch.object(work.core, "STATE", Path(d))  # sync.log opens below it, whatever ran before
            state.start()
            self.addCleanup(state.stop)
            handles = []
            subprocess.Popen = lambda *a, **k: (spawned.append(a[0]), handles.append(k["stdout"]))
            try:
                self.assertTrue(work.request_sync("commit abc"))
                self.assertFalse(work.request_sync("push"))
                self.assertFalse(work.request_sync("task done"))
                self.assertEqual(len(spawned), 1)
                self.assertIn("--delay", spawned[0])
                # the child has its own copy of sync.log; this process closes its handle (no ResourceWarning)
                self.assertTrue(handles[0].closed, "request_sync left its sync.log handle open")
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
                        "PROJECTS.md", "registry.json", "audit/ana@mbp1/2026-11.jsonl", "lessons/global/L-x.md",
                        "policies/acme/one.json"):
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
