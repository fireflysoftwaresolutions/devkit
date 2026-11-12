# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Work items, locks, history, plans and handoff isolation. Run: python3 -m unittest discover -s tests -v"""
import contextlib
import io
import json
import os
import re
import shlex
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
        with TempHandoff(), tempfile.TemporaryDirectory() as state, mock.patch.object(core, "STATE", Path(state)):
            work.create("acme/api", "Later thing", bucket="later", a=A)
            now = work.create("acme/api", "Now thing", bucket="now", priority="high", a=A)
            work.show("acme/api")  # numbers point into the list `dev task` showed (tests/test_task_numbers.py)
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


class NowLimitTests(unittest.TestCase):
    """More than five open items in a project's Now gets a one-line warning; nothing is refused."""

    def test_more_than_five_in_now_warns_with_how_many_and_how_many_are_being_worked_on(self):
        with TempHandoff():
            items = [work.create("acme/api", f"Now item {n}", bucket="now", a=A) for n in range(6)]
            work.claim(items[0], a=A)
            work.claim(items[1], a=B)
            self.assertEqual(work.now_warning("acme/api"),
                             "6 items in Now in acme/api, 2 being worked on: more than 5 hides what comes first; "
                             "move what waits with dev task move <id> --to next --why \"…\"")

    def test_five_open_items_in_now_and_closed_or_other_buckets_do_not_warn(self):
        with TempHandoff():
            for n in range(5):
                work.create("acme/api", f"Now item {n}", bucket="now", a=A)
            work.create("acme/api", "Next item", bucket="next", a=A)
            work.create("acme/other", "Another project's Now item", bucket="now", a=A)
            shipped = work.create("acme/api", "Shipped item", bucket="now", a=A)
            work.release(shipped, a=A, status="done")
            self.assertEqual(work.now_warning("acme/api"), "")
            work.create("acme/api", "Sixth open item", bucket="now", a=A)
            self.assertTrue(work.now_warning("acme/api").startswith("6 items in Now in acme/api, 0 being worked on"))


class IsoTimeTests(unittest.TestCase):
    # A lock time is UTC. Read on a Mac in Madrid, a summer time used to come out an hour late (local summer time
    # was taken for standard time), so locks ran out an hour early or late for seven months of the year.
    def test_a_utc_time_reads_the_same_in_summer_and_in_winter_wherever_the_mac_is(self):
        saved = os.environ.get("TZ")

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = "Europe/Madrid"
        time.tzset()
        cases = {"summer": ("2026-07-01T12:00:00Z", 1782907200), "winter": ("2026-01-15T12:00:00Z", 1768478400)}
        for season, (text, epoch) in cases.items():
            with self.subTest(season):
                self.assertEqual(work.parse_iso(text), epoch)
                self.assertEqual(work.now_iso(epoch), text)


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

    def test_a_closed_item_keeps_its_state_until_it_is_reopened(self):
        with TempHandoff():
            for closed in ("done", "cancelled"):
                it = work.create("acme/api", f"Export invoices ({closed})", why="a reason", a=A)
                work.release(work.load(it["path"]), a=A, status=closed)
                for op, changes in (("move", {"bucket": "later"}), ("block", {"status": "blocked"}),
                                    ("cancel", {"status": "cancelled"}), ("unblock", {"status": "todo"})):
                    with self.subTest(closed=closed, op=op):
                        with self.assertRaises(ValueError) as cm:
                            work.update(work.load(it["path"]), op, why="a reason", a=A, **changes)
                        self.assertIn(f"is {closed}", str(cm.exception))
                again = work.load(it["path"])
                self.assertEqual((again["meta"]["status"], again["meta"]["bucket"]), (closed, "next"))
                work.reopen(again, why="one case is left", a=A)
                work.update(work.load(it["path"]), "move", why="start now", a=A, bucket="now")
                self.assertEqual(work.load(it["path"])["meta"]["bucket"], "now")

    def test_a_closed_item_is_neither_released_finished_again_nor_claimed(self):
        # dev task release, done and start on a closed item: each one would reopen or rewrite how it was closed
        with TempHandoff():
            for closed in ("done", "cancelled"):
                it = work.create("acme/api", f"Export invoices ({closed})", why="a reason", a=A)
                work.release(work.load(it["path"]), a=A, status=closed)
                for what, call in (("release", lambda: work.release(work.load(it["path"]), a=A, status="todo")),
                                   ("done", lambda: work.release(work.load(it["path"]), a=A, status="done")),
                                   ("start", lambda: work.claim(work.load(it["path"]), a=A))):
                    with self.subTest(closed=closed, command=what):
                        with self.assertRaises(ValueError) as cm:
                            call()
                        self.assertIn(f"is {closed}", str(cm.exception))
                        self.assertIn("dev task reopen", str(cm.exception))
                again = work.load(it["path"])["meta"]
                self.assertEqual((again["status"], again.get("claimed_by") or ""), (closed, ""))

    def test_only_a_blocked_item_can_be_unblocked(self):
        with TempHandoff():
            it = work.create("acme/api", "Export invoices", why="a reason", a=A)
            with self.assertRaises(ValueError) as cm:
                work.update(work.load(it["path"]), "unblock", why="a reason", a=A, status="doing")
            self.assertIn("is todo, not blocked", str(cm.exception))
            work.update(work.load(it["path"]), "block", why="waits on the API", a=A, status="blocked")
            work.update(work.load(it["path"]), "unblock", why="the API is ready", a=A, status="todo")
            self.assertEqual(work.load(it["path"])["meta"]["status"], "todo")

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

        live = [{"project": "acme/api", "branch": "feat/lock-job", "active": True}]
        jobs = {"lock renewal": (1, lambda: work.renew_locks(A, hours=8, sessions=live), "lock-renewal-skipped"),
                "expired-lock release": (-1, work.expire_locks, "lock-release-skipped")}
        with TempHandoff() as h, mock.patch.object(audit, "AUDIT", h.root / "audit"), \
                mock.patch.object(audit, "policy_file", lambda: h.root / "no-policy.json"):
            for job, (hours, run, op) in jobs.items():
                with self.subTest(job):
                    it = work.create("acme/api", f"Item for the {job}", body="Context.\n\nKeep this line.", a=A)
                    it = work.claim(it, a=A, hours=hours, branch="feat/lock-job")
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


def _git_handoff(root: Path) -> None:
    """The temporary handoff as a git repository without a remote, so `work._commit` really commits."""
    for args in (["init", "-q"], ["config", "user.name", "Ana Dev"], ["config", "user.email", "ana@example.com"],
                 ["commit", "-q", "--allow-empty", "-m", "start"]):
        rc, out, err = core.sh(["git", "-C", str(root), *args])
        if rc != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {err or out}")


def _subjects(root: Path) -> list:
    return core.gitout(root, "log", "--format=%s").splitlines()


class StaleLockTests(unittest.TestCase):
    LIVE = {"project": "acme/api", "branch": "feat/export", "active": True}

    def claimed(self, hours=1.0, branch="feat/export", title="Export invoices"):
        return work.claim(work.create("acme/api", title, a=A), a=A, hours=hours, branch=branch)

    def test_a_lock_renews_while_a_live_session_is_on_its_branch(self):
        with TempHandoff():
            it = self.claimed(hours=1)  # an hour left of 8: renewal is due
            self.assertEqual(work.renew_locks(A, hours=8, sessions=[self.LIVE]), 1)
            left = work.parse_iso(work.load(it["path"])["meta"]["lock_expires"]) - time.time()
            self.assertGreater(left, 7.9 * 3600)

    def test_a_closed_items_lock_does_not_renew_while_a_live_session_is_on_its_branch(self):
        # an item closed with its lock still set (an older devkit, or two Macs' changes merged): staying on its
        # branch must not keep a finished item locked
        for status in ("done", "cancelled"):
            with self.subTest(status), TempHandoff():
                it = self.claimed(hours=1)
                work.update(work.load(it["path"]), "edit", why="closed with its lock", a=A, status=status)
                before = work.load(it["path"])["meta"]["lock_expires"]
                self.assertEqual(work.renew_locks(A, hours=8, sessions=[self.LIVE]), 0)
                self.assertEqual(work.load(it["path"])["meta"]["lock_expires"], before)

    def test_a_lock_runs_out_when_no_live_session_is_on_its_branch(self):
        cases = {"no session": ("feat/export", []),
                 "a session on another branch": ("feat/export", [dict(self.LIVE, branch="feat/other")]),
                 "an ended session on the branch": ("feat/export", [dict(self.LIVE, active=False)]),
                 "the same branch in another project": ("feat/export", [dict(self.LIVE, project="acme/web")]),
                 "an item claimed on no branch": ("", [self.LIVE])}
        for case, (branch, sessions) in cases.items():
            with self.subTest(case), TempHandoff():
                it = self.claimed(hours=1, branch=branch)
                before = work.load(it["path"])["meta"]["lock_expires"]
                self.assertEqual(work.renew_locks(A, hours=8, sessions=sessions), 0)
                self.assertEqual(work.load(it["path"])["meta"]["lock_expires"], before)

    def test_an_expired_lock_sends_a_doing_item_back_to_todo_and_says_whose_lock_ran_out_when(self):
        with TempHandoff():
            doing = self.claimed(hours=-1)
            blocked = self.claimed(hours=-1, title="Import invoices")
            work.update(work.load(blocked["path"]), "block", why="waits for the bank's API", a=A, status="blocked")
            holder, expires = doing["meta"]["claimed_by"], doing["meta"]["lock_expires"]
            self.assertEqual(len(work.expire_locks()), 2)
            after = work.load(doing["path"])
            self.assertEqual((after["meta"]["status"], after["meta"]["claimed_by"], after["meta"]["lock_expires"]),
                             ("todo", "", ""))
            self.assertIn(f"lock of {holder} expired at {expires}", after["changelog"][0])
            self.assertIn("back to todo", after["changelog"][0])
            kept = work.load(blocked["path"])
            self.assertEqual((kept["meta"]["status"], kept["meta"]["claimed_by"]), ("blocked", ""))
            self.assertNotIn("back to todo", kept["changelog"][0])

    def test_expired_locks_are_released_in_one_commit_that_counts_the_items_back_to_todo(self):
        with TempHandoff() as h, mock.patch.object(audit, "AUDIT", h.root / "audit"):
            _git_handoff(h.root)
            doing = self.claimed(hours=-1)
            blocked = self.claimed(hours=-1, title="Import invoices")
            work.update(work.load(blocked["path"]), "block", why="waits for the bank's API", a=A, status="blocked")
            before = _subjects(h.root)
            work.expire_locks()
            self.assertEqual(_subjects(h.root)[1:], before)  # exactly one new commit
            self.assertEqual(_subjects(h.root)[0], "release 2 expired lock(s), 1 item(s) back to todo")
            files = core.gitout(h.root, "show", "--name-only", "--format=", "HEAD").splitlines()
            self.assertEqual(sorted(files), sorted(str(p["path"].relative_to(h.root)) for p in (doing, blocked)))


class ReconcileTests(unittest.TestCase):
    SHA = "abc1234" + "0" * 33
    MERGED = {"feat/export": {"sha": SHA, "pr": 7}}

    def backlog(self):
        """A doing item an old expiry left without a lock, an item of a merged branch, two items with one title,
        and a claimed item on a branch that is not merged."""
        lockless = work.create("acme/api", "Fix login", a=A)
        lockless["meta"]["status"] = "doing"
        work.write_record(lockless["path"], work.render(lockless))
        merged = work.claim(work.create("acme/api", "Export invoices", a=A), a=A, branch="feat/export")
        dups = [work.create("acme/api", "Add the CSV export", a=A), work.create("acme/api", "add the  CSV export.", a=A)]
        held = work.claim(work.create("acme/api", "Refunds", a=A), a=A, branch="feat/refunds")
        return lockless, merged, dups, held

    def test_reconcile_reports_the_stale_items_and_changes_nothing(self):
        with TempHandoff():
            lockless, merged, dups, held = self.backlog()
            files = {p: p.read_text() for p in (work.ITEMS / "acme" / "api").glob("*.md")}
            r = work.reconcile("acme/api", self.MERGED)
            self.assertEqual([i["id"] for i in r["lockless_doing"]], [lockless["meta"]["id"]])
            self.assertEqual([(i["id"], i["branch"], i["commit"], i["pr"]) for i in r["merged"]],
                             [(merged["meta"]["id"], "feat/export", self.SHA, 7)])
            self.assertEqual([sorted(i["id"] for i in g) for g in r["duplicates"]],
                             [sorted(d["meta"]["id"] for d in dups)])
            self.assertEqual(r["changed"], [])
            self.assertEqual({p: p.read_text() for p in files}, files)

    def test_reconcile_apply_closes_the_items_of_merged_branches_with_the_merge_commit(self):
        with TempHandoff():
            _, merged, _, held = self.backlog()
            r = work.reconcile("acme/api", self.MERGED, apply=True, a=A)
            self.assertIn(merged["meta"]["id"], r["changed"])
            after = work.load(merged["path"])
            self.assertEqual((after["meta"]["status"], after["meta"]["claimed_by"]), ("done", ""))
            self.assertIn("feat/export merged in abc1234 (PR #7)", after["changelog"][0])
            self.assertEqual(work.load(held["path"])["meta"]["status"], "doing")

    def test_reconcile_apply_sends_doing_items_without_a_lock_back_to_todo(self):
        with TempHandoff():
            lockless, _, dups, held = self.backlog()
            work.reconcile("acme/api", self.MERGED, apply=True, a=A)
            after = work.load(lockless["path"])
            self.assertEqual(after["meta"]["status"], "todo")
            self.assertIn("doing without a lock: back to todo", after["changelog"][0])
            self.assertEqual([work.load(d["path"])["meta"]["version"] for d in dups], [1, 1])  # reported, not changed
            self.assertTrue(work.load(held["path"])["meta"]["claimed_by"].startswith("ana@mbp1"))


class PresenceTests(unittest.TestCase):
    # Ended sessions showed as live to the team: presence dropped each session's `active` flag, and the sync that
    # finishes a session published presence before it removed that session.
    def test_a_session_that_ended_is_not_shown_to_the_team_as_working(self):
        import argparse
        from devkit import cli, dashboard, hooks, registry
        sessions = [dict(agent="claude", session="ended1", project="acme/api", branch="feat/a", active=True),
                    dict(agent="codex", session="live2", project="acme/api", branch="feat/b", active=True),
                    dict(agent="claude", session="idle3", project="acme/web", branch="fix/c", active=False)]
        with TempHandoff() as h:
            state = h.root.parent / "state"
            (state / "sessions").mkdir(parents=True)
            (state / "sessions" / "claude-ended1.json").write_text("{}")
            args = argparse.Namespace(session="claude:ended1", finish=True, offline=True, scheduled=False,
                                      no_push=True, quiet=True, renew=None, delay=0)
            with mock.patch.object(cli, "STATE", state), mock.patch.object(core, "STATE", state), \
                    mock.patch.object(registry, "build", lambda **kw: {"projects": [], "sessions": list(sessions)}), \
                    mock.patch.object(registry, "write_outputs", lambda reg: []), \
                    mock.patch.object(dashboard, "write", lambda reg: None), \
                    mock.patch.object(hooks, "record_session", lambda s: None), \
                    mock.patch.object(cli, "commit_push", lambda **kw: None):
                self.assertEqual(cli.cmd_sync(args), 0)
            self.assertEqual([(s["agent"], s["branch"]) for t in work.team() for s in t["sessions"]],
                             [("codex", "feat/b")])
            self.assertFalse((state / "sessions" / "claude-ended1.json").exists())

    def test_presence_publishes_a_summary_of_each_run_and_not_its_plan(self):
        # Presence carried each session's whole run, every plan step and crew lane: 49 KB for one Mac, changed on most
        # handoff commits, and above 256 KB the dashboard hides that Mac. A session in a run of 60 steps (40 done,
        # 2 skipped, 2 running, 1 blocked) with 6 lanes (4 working) publishes the goal, the step counts, the steps
        # running now, the crew at work and the ETA, and another Mac's dashboard still shows the run.
        import time as _time
        from devkit import dashnow
        states = ["done"] * 40 + ["skipped"] * 2 + ["running", "running", "blocked"] + ["pending"] * 15
        plan = [{"name": f"{i + 1}. step {i + 1}: what this step builds, checks and hands over to the next one",
                 "state": st, "est": 30, "min": 12.5, "owner": "main" if i % 2 else "Backend engineer (Tank)"}
                for i, st in enumerate(states)]
        lanes = [{"label": f"Backend engineer (Tank) · lane {i}", "state": "running" if i < 4 else "done", "min": 9.5,
                  "live": "working", "why": "reported 2 min ago"} for i in range(6)]
        now = _time.time()
        brief = {"goal": "Hotfix 26.11.9", "state": "running", "step": 41, "steps": 60, "estimate": 120,
                 "started": round(now - 18000), "eta_min": 40, "eta_lo": 30, "eta_hi": 60, "active_min": 291.0,
                 "wall_min": 305.0, "pace": 1.1, "plan": plan, "agents": lanes, "headline": "41 of 58 steps",
                 "last": "Lane tests green", "next": "three commits", "updated": round(now - 60)}
        with TempHandoff():
            work.write_presence([dict(agent="claude", session="s1", project="acme/api", branch="main", active=True,
                                      started=round(now - 18000), progress=brief)], a=A)
            f = work.PRESENCE / "ana@mbp1.json"
            p = json.loads(f.read_text())["sessions"][0]["progress"]
            self.assertEqual((p["goal"], p["steps_done"], p["steps_total"], p["steps_skipped"], p["crew"]),
                             ("Hotfix 26.11.9", 40, 58, 2, 4))
            self.assertEqual((p["eta_min"], p["eta_lo"], p["eta_hi"]), (40, 30, 60))
            self.assertEqual([c.split(":")[0] for c in p["current"]], ["43. step 43", "44. step 44", "45. step 45"])
            self.assertNotIn("agents", p)
            self.assertEqual([s["state"] for s in p["plan"]], ["running", "running", "blocked"])
            self.assertLess(f.stat().st_size, 2500)
            run = dashnow._remote_run(json.loads(f.read_text())["sessions"][0], "mbp1", "ana", int(now), now)
            self.assertEqual((run["goal"], len(run["plan"])), ("Hotfix 26.11.9", 3))


class ReusedBranchTests(unittest.TestCase):
    # Branch names come back (fix/login-timeout): a merge of the name's earlier use closed a new item linked to it.
    def test_a_merge_counts_for_an_item_only_when_it_is_newer_than_the_items_link_to_the_branch(self):
        sha = "def5678" + "0" * 33
        with TempHandoff():
            it = work.claim(work.create("acme/api", "Fix the login timeout again", a=A), a=A, branch="fix/login-timeout")
            linked = work.parse_iso(it["meta"]["claimed_at"])
            old = {"fix/login-timeout": {"sha": sha, "pr": 3, "at": linked - 30 * 86400}}
            self.assertEqual(work.reconcile("acme/api", old, apply=True, a=A)["merged"], [])
            self.assertEqual(work.load(it["path"])["meta"]["status"], "doing")
            new = {"fix/login-timeout": {"sha": sha, "pr": 9, "at": linked + 60}}
            self.assertEqual([i["pr"] for i in work.reconcile("acme/api", new)["merged"]], [9])


class ReconcileReportTests(unittest.TestCase):
    REPORT = {"scope": "acme/api", "lockless_doing": [{"id": "T-1", "title": "Fix login", "status": "doing"}],
              "merged": [{"id": "T-2", "title": "Export invoices", "status": "doing", "branch": "feat/export",
                          "commit": "abc1234" + "0" * 33, "pr": 7}],
              "duplicates": [[{"id": "T-3", "title": "Add the CSV export", "status": "todo"},
                              {"id": "T-4", "title": "add the CSV export.", "status": "done"}]], "changed": []}

    def test_the_report_names_each_stale_item_and_what_apply_does_with_it(self):
        from devkit import cli
        text = cli.reconcile_text(self.REPORT, "acme/api", applied=False)
        self.assertIn("Doing without a lock (1): back to todo with --apply\n    T-1  Fix login", text)
        self.assertIn("Open, but their branch is merged (1): done with --apply\n"
                      "    T-2  Export invoices · feat/export merged in abc1234 (PR #7)", text)
        self.assertIn("    T-3 (todo), T-4 (done)  Add the CSV export", text)
        self.assertIn("nothing changed · dev task reconcile --apply fixes the 2 item(s)", text)


def _git(root: Path, *args) -> str:
    rc, out, err = core.sh(["git", "-C", str(root), *args])
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class TwoMacs:
    """The handoff repository on a remote and on two Macs: this one (the module's HANDOFF) and another one, where
    `on_other()` runs devkit as that Mac would. Sync problems, audit records and locks stay in the test's folder,
    and no background sync starts."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        remote = self.base / "remote.git"
        self.here, self.other = self.base / "here" / ".handoff", self.base / "other" / ".handoff"
        _git(self.base, "init", "-q", "--bare", "-b", "main", str(remote))
        _git(self.base, "init", "-q", "-b", "main", str(self.here))
        for root, (name, email) in ((self.here, ("Ana Dev", "ana@example.com")),
                                    (self.other, ("Bob Dev", "bob@example.com"))):
            if root == self.other:
                _git(self.base, "clone", "-q", str(remote), str(root))
            _git(root, "config", "user.name", name)
            _git(root, "config", "user.email", email)
            if root == self.here:
                _git(root, "remote", "add", "origin", str(remote))
                _git(root, "commit", "-q", "--allow-empty", "-m", "start")
                _git(root, "push", "-q", "-u", "origin", "main")
        self.saved = (core.HANDOFF, work.HANDOFF, work.ITEMS, work.SESSIONS, work.PRESENCE, plans.PLANS,
                      plans.HANDOFF)
        self._point(self.here)
        self.stack = contextlib.ExitStack()
        from devkit import cli
        self.stack.enter_context(mock.patch.object(cli, "HANDOFF", self.here))  # never the real one
        self.stack.enter_context(mock.patch.object(core, "STATE", self.base / "state"))
        self.stack.enter_context(mock.patch.object(audit, "AUDIT", self.base / "audit"))
        self.stack.enter_context(mock.patch.object(audit, "policy_file", lambda: self.base / "no-policy.json"))
        self.stack.enter_context(mock.patch.object(work, "request_sync", lambda *a, **k: False))
        self.stack.enter_context(mock.patch.dict(core.CFG.setdefault("sync", {}), {"push": True}))
        return self

    def __exit__(self, *exc):
        self.stack.close()
        (core.HANDOFF, work.HANDOFF, work.ITEMS, work.SESSIONS, work.PRESENCE, plans.PLANS, plans.HANDOFF) = self.saved
        self.tmp.cleanup()

    @staticmethod
    def _point(root: Path) -> None:
        core.HANDOFF = work.HANDOFF = plans.HANDOFF = root
        work.ITEMS, work.SESSIONS, work.PRESENCE, plans.PLANS = root / "items", root / "sessions", \
            root / "presence", root / "plans"

    @contextlib.contextmanager
    def on_other(self):
        self._point(self.other)
        try:
            yield
        finally:
            self._point(self.here)

    def shared_item(self, title: str) -> str:
        """An item created on this Mac, pushed, and pulled on the other one. -> its id."""
        iid = work.create("acme/api", title, why="a reason", a=A)["meta"]["id"]
        _git(self.here, "push", "-q")
        _git(self.other, "pull", "-q")
        return iid

    def item(self, iid: str) -> dict:
        return work.load(work.item_path("acme/api", iid))

    def remote_item(self, iid: str) -> dict:
        return work.parse(_git(self.here, "show", f"origin/main:items/acme/api/{iid}.md") + "\n")

    def stopped(self, root: Path = None) -> list:
        """The rebase git left stopped in a handoff repository, if any."""
        return [d for d in ("rebase-merge", "rebase-apply") if ((root or self.here) / ".git" / d).exists()]

    def problem(self) -> str:
        """The sync problem devkit recorded last (git's own error when an abort failed), for failure messages."""
        f = work.push_failed_file()
        return json.loads(f.read_text())["error"] if f.exists() else "no sync problem recorded"


class HandoffSyncTests(unittest.TestCase):
    def test_a_rebase_left_stopped_is_aborted_before_the_next_commit_and_recorded_as_a_sync_problem(self):
        with TwoMacs() as m:
            iid = m.shared_item("Export invoices")
            with m.on_other():
                work.update(m.item(iid), "edit", why="their wording", a=B, body="Their text.")
                _git(m.other, "push", "-q")
            work.update(m.item(iid), "edit", why="our wording", a=A, body="Our text.")
            rc, _, _ = core.git(m.here, "pull", "-q", "--rebase")  # an older sync: it stops on the conflict
            self.assertNotEqual(rc, 0)
            self.assertEqual(m.stopped(), ["rebase-merge"])
            nxt = work.create("acme/api", "Import invoices", a=A)
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "branch", "--show-current"), "main")
            self.assertTrue(_subjects(m.here)[0].startswith(f"{nxt['meta']['id']}: create task"))
            self.assertEqual(m.item(iid)["body"], "Our text.")  # this Mac's change is kept, still to push
            recs = audit.query(days=1, action="handoff.rebase-aborted")
            self.assertEqual([(r["outcome"], r["target"]) for r in recs], [("blocked", f"items/acme/api/{iid}.md")])
            problem = json.loads(work.push_failed_file().read_text())["error"]
            self.assertIn(f"stopped on a conflict in items/acme/api/{iid}.md", problem)

    def test_an_item_read_while_a_rebase_was_left_stopped_is_read_as_this_mac_committed_it(self):
        with TwoMacs() as m:
            iid = m.shared_item("Export invoices")
            with m.on_other():
                work.update(m.item(iid), "edit", why="their wording", a=B, body="Their text.")
                _git(m.other, "push", "-q")
            work.update(m.item(iid), "edit", why="our wording", a=A, body="Our text.")
            core.git(m.here, "pull", "-q", "--rebase")  # stops with conflict markers in the item
            self.assertIn("<<<<<<<", work.item_path("acme/api", iid).read_text())
            self.assertEqual([i["body"] for i in work.all_items("acme/api")], ["Our text."])
            self.assertEqual(m.stopped(), [], m.problem())

    def test_a_change_to_an_item_the_other_mac_changed_first_is_applied_again_to_its_version(self):
        from devkit import cli
        with TwoMacs() as m:
            iid = m.shared_item("Export invoices")
            with m.on_other():
                work.update(m.item(iid), "move", why="start now", a=B, bucket="now")
                _git(m.other, "push", "-q")
            work.update(m.item(iid), "edit", why="CSV only", a=A, body="Only CSV.")
            cli.commit_push(quiet=True)  # the background `dev sync`
            self.assertEqual(m.stopped(), [], m.problem())
            pushed = m.remote_item(iid)
            self.assertEqual((pushed["meta"]["bucket"], pushed["body"], pushed["meta"]["version"]),
                             ("now", "Only CSV.", 3))
            self.assertEqual([e.split(" · ")[0] for e in pushed["changelog"]], ["v3", "v2", "v1"])
            self.assertIn("ana@mbp1 (claude:s1) · edit:  (body edited) — why: CSV only", pushed["changelog"][0])
            self.assertIn("· move: bucket=now — why: start now", pushed["changelog"][1])
            self.assertFalse(work.push_failed_file().exists())

    def test_dev_sync_keeps_both_sides_of_a_conflict_it_cannot_merge_and_still_pushes(self):
        # A file that is neither an item nor a note (a plan edited on both Macs at the same lines) never stops the
        # pull: it keeps the remote's version, saves this Mac's aside, pushes this Mac's commit, and the command its
        # warning prints brings this Mac's text back.
        from devkit import cli
        with TwoMacs() as m:
            rel = "plans/acme/api/2026-11-09-export.md"
            plan = m.here / rel
            plan.parent.mkdir(parents=True)
            plan.write_text("# Export\n\nState: draft.\n")
            work._commit([plan], "plan: export", push=False)
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            (m.other / rel).write_text("# Export\n\nState: their words.\n")
            _git(m.other, "commit", "-qam", "plan: their words")
            _git(m.other, "push", "-q")
            plan.write_text("# Export\n\nState: our words.\n")
            work._commit([plan], "plan: our words", push=False)
            warned = io.StringIO()
            with contextlib.redirect_stderr(warned):
                cli.commit_push(quiet=True)
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "branch", "--show-current"), "main")
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", m.problem())
            self.assertEqual(_git(m.here, "show", f"origin/main:{rel}"), "# Export\n\nState: their words.")
            self.assertEqual(plan.read_text(), "# Export\n\nState: their words.\n")
            cmd = re.search(r"run `(git -C [^`]+ merge-file [^`]+)`", warned.getvalue())
            self.assertIsNotNone(cmd, warned.getvalue())
            core.sh(shlex.split(cmd.group(1)))
            self.assertIn("State: our words.", plan.read_text())


class ClaimReachesRemoteTests(unittest.TestCase):
    def test_a_claim_that_loses_the_race_for_the_lock_fails_and_names_who_holds_it(self):
        with TwoMacs() as m:
            iid = m.shared_item("Export invoices")
            real, raced = work.write_record, []

            def write_then_the_other_mac_claims(path, text, edited=()):
                real(path, text, edited)
                if not raced:  # between this Mac's pull and its push, the other Mac claims and pushes
                    raced.append(True)
                    with m.on_other():
                        work.claim(m.item(iid), a=B)

            with mock.patch.object(work, "write_record", write_then_the_other_mac_claims):
                with self.assertRaises(ValueError) as cm:
                    work.claim(m.item(iid), a=A)
            self.assertIn("claimed by bob@mbp2", str(cm.exception))
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0")  # nothing left to conflict again
            self.assertTrue(m.item(iid)["meta"]["claimed_by"].startswith("bob@mbp2"))

    def test_a_claim_whose_lock_does_not_reach_the_remote_does_not_report_success(self):
        with TwoMacs() as m:
            iid = m.shared_item("Export invoices")
            _git(m.here, "remote", "set-url", "origin", str(m.base / "unreachable.git"))
            with self.assertRaises(ValueError) as cm:
                work.claim(m.item(iid), a=A)
            self.assertIn("the lock did not reach the remote", str(cm.exception))
            self.assertEqual(m.stopped(), [], m.problem())


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

    def test_a_revision_that_follows_at_once_is_committed_after_the_first_with_its_own_text(self):
        # Another session revises the plan just as this revision commits: it waits for that commit, so the history
        # holds this revision's version under this revision's message, then the other's under its own.
        import fcntl
        import threading
        with TwoMacs():
            path = plans.new("acme/api", "Invoice export", a=A)
            rel = str(path.relative_to(work.HANDOFF))
            settled = threading.Event()
            real_flock, real_commit = core._flock_within, work._commit

            def other_session():
                try:
                    plans.revise(path, "second", a=B)
                finally:
                    settled.set()

            other = threading.Thread(target=other_session)

            def flock_within(fd, wait):  # the other session asks for the plan's lock
                if threading.current_thread() is other:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return True
                    except BlockingIOError:
                        settled.set()  # it waits for this revision
                return real_flock(fd, wait)

            def commit(paths, message, **kw):
                if threading.current_thread() is threading.main_thread() and message.endswith("— first"):
                    other.start()
                    settled.wait(10)  # the other session waits for the plan's lock, or has revised and committed
                return real_commit(paths, message, **kw)

            with mock.patch.object(core, "_flock_within", flock_within), mock.patch.object(work, "_commit", commit):
                plans.revise(path, "first", a=A)
                other.join(30)
            subjects = _git(work.HANDOFF, "log", "-2", "--format=%s").splitlines()
            self.assertEqual([(re.findall(r" v(\d+) ", s), s.rsplit(" — ", 1)[-1]) for s in subjects],
                             [(["3"], "second"), (["2"], "first")])
            self.assertIn("\nversion: 2\n", _git(work.HANDOFF, "show", f"HEAD~1:{rel}"))
            self.assertIn("\nversion: 3\n", _git(work.HANDOFF, "show", f"HEAD:{rel}"))


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
                self.assertNotRegex(text, r"(?m)^# ")  # the front-matter title is the plan's one title (MD025)
                self.assertIn(section, text)
        with self.assertRaises(ValueError):
            plans.template("novel", "x")
        with TempHandoff():
            path = plans.new("acme/api", "Event store", a=A, kind="adr")
            self.assertIn("kind: adr", path.read_text())
            self.assertIn("title: Event store", path.read_text().splitlines())  # where the title now lives
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


class MachineIdentityTests(unittest.TestCase):
    # macOS renames the Mac with the network: devkit's records of this Mac (locks, presence) must stay this Mac's
    # across a rename, and another Mac's must never become this one's
    def setUp(self):
        self.handoff = TempHandoff().__enter__()
        self.addCleanup(self.handoff.__exit__, None, None, None)
        state = mock.patch.object(core, "STATE", self.handoff.root.parent / "state")
        state.start()
        self.addCleanup(state.stop)
        self.name = "Mac-158.lan"
        host = mock.patch("socket.gethostname", side_effect=lambda: self.name)
        host.start()
        self.addCleanup(host.stop)

    def me(self):
        return work.actor("claude", "s1", user="ana")

    def legacy_lock(self, holder: str, at: float):
        # a lock as devkit wrote it before the machine id: the holder names the Mac by its hostname of the time
        it = work.create("acme/api", "Fix login", a=B)
        it["meta"].update(claimed_by=f"{holder} (codex)", claimed_at=work.now_iso(at), status="doing",
                          lock_expires=work.now_iso(time.time() + 3600))
        work.write_record(it["path"], work.render(it))
        return work.load(it["path"])

    def audit_log(self, *records):
        # this Mac's own audit log as devkit wrote it before the machine id: each record names the hostname of the time
        d = core.STATE / "audit"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "2026-11.jsonl").open("a") as fh:
            for ts, host in records:
                fh.write(json.dumps({"v": 2, "ts": ts, "actor": {"user": "ana", "host": host, "agent": "claude"},
                                     "action": "guard.allow"}) + "\n")

    def test_a_cut_over_under_a_dhcp_name_leaves_another_macs_earlier_lock_alone(self):
        # this Mac has carried mac-55 for the last 2 h; another Mac carried it before and took a lock that still runs
        now = time.time()
        self.audit_log((now - 7200, "mac-55"), (now - 3600, "mac-55"))
        self.name = "mac-55.lan"
        theirs, ours = self.legacy_lock("ana@mac-55", now - 5 * 3600), self.legacy_lock("ana@mac-55", now - 1800)
        self.assertFalse(work.mine(theirs["meta"]["claimed_by"], self.me(), at=now - 5 * 3600))
        with self.assertRaises(ValueError):
            work.release(theirs, a=self.me())
        self.assertTrue(work.mine(ours["meta"]["claimed_by"], self.me(), at=now - 1800))

    def test_a_rename_between_two_calls_keeps_the_lock_this_mac_took(self):
        it = work.claim(work.create("acme/api", "Fix login", a=self.me()), a=self.me())
        self.name = "andress-mbp-2"
        it = work.update(work.load(it["path"]), "edit", why="scope", a=self.me(), title="Fix the login")
        self.assertEqual(it["meta"]["title"], "Fix the login")
        it = work.release(work.load(it["path"]), a=self.me(), status="done")
        self.assertEqual(it["meta"]["claimed_by"], "")

    def test_a_lock_under_a_hostname_this_mac_carried_then_is_its_own(self):
        self.me()  # devkit runs here as mac-158 ...
        it = self.legacy_lock("ana@mac-158", time.time())
        self.name = "andress-mbp-2"  # ... and the network renames the Mac
        self.assertTrue(work.mine(it["meta"]["claimed_by"], self.me(), at=time.time()))
        work.release(it, a=self.me())

    def test_another_macs_lock_is_never_taken_over(self):
        self.me()
        self.name = "mac-55"
        self.me()  # this Mac carries mac-55 from now on ...
        self.name = "andress-mbp-2"
        self.me()
        # ... so a mac-55 lock from yesterday is another Mac's; so are another person's and another machine id's
        for holder, at in (("ana@mac-55", time.time() - 86400), ("ana@mac-640", time.time()),
                           ("bob@mac-158", time.time()), ("ana@m-0000aaaa", time.time())):
            it = self.legacy_lock(holder, at)
            self.assertFalse(work.mine(it["meta"]["claimed_by"], self.me(), at=at), holder)
            with self.assertRaises(ValueError):
                work.release(it, a=self.me())
            with self.assertRaises(ValueError):
                work.update(work.load(it["path"]), "edit", why="scope", a=self.me(), title="mine now")

    def test_renewal_extends_this_macs_lock_under_a_former_name_keeps_its_holder_and_never_another_macs(self):
        # the holder stays as written: an older devkit session still on this Mac keeps its own lock, and a lock read
        # as this Mac's by mistake is never renamed for good (the rename waits for the contract step)
        self.me()
        ours, theirs = self.legacy_lock("ana@mac-158", time.time()), self.legacy_lock("ana@mac-55", time.time())
        for it in (ours, theirs):
            it["meta"].update(lock_expires=work.now_iso(time.time() + 60), links=["branch:feat/x"])
            work.write_record(it["path"], work.render(it))
        self.name = "andress-mbp-2"
        live = [{"active": True, "project": "acme/api", "branch": "feat/x"}]
        self.assertEqual(work.renew_locks(a=self.me(), sessions=live), 1)
        ours, theirs = work.load(ours["path"]), work.load(theirs["path"])
        self.assertEqual(ours["meta"]["claimed_by"], "ana@mac-158 (codex)")
        self.assertGreater(work.parse_iso(ours["meta"]["lock_expires"]), time.time() + 3600)
        self.assertEqual(theirs["meta"]["claimed_by"], "ana@mac-55 (codex)")

    def test_a_rename_between_two_calls_keeps_the_machine_and_its_presence_file(self):
        before = self.me()
        work.write_presence([], a=work.actor("human", user="ana"))
        self.name = "andress-mbp-2"
        after = self.me()
        work.write_presence([], a=work.actor("human", user="ana"))
        self.assertEqual(after["host"], before["host"])
        self.assertRegex(after["host"], r"^m-[0-9a-z]{8}$")
        self.assertEqual((before["label"], after["label"]), ("mac-158", "andress-mbp-2"))
        files = sorted(f.name for f in self.handoff.root.glob("presence/*.json"))
        self.assertEqual(files, [f"ana@{after['host']}.json"])
        data = json.loads((self.handoff.root / "presence" / files[0]).read_text())
        self.assertEqual((data["host"], data["label"]), (after["host"], "andress-mbp-2"))

    def test_an_older_session_still_writing_this_macs_old_presence_file_is_hidden_and_never_flaps(self):
        # an older devkit session on this Mac rewrites ana@mac-158.json; deleting it on every write made each sync
        # commit, so the file stays while it is written, and this Mac shows once in the team
        a = work.actor("human", user="ana")
        work.write_presence([], a=a)
        legacy = work.PRESENCE / "ana@mac-158.json"
        legacy.write_text(json.dumps({"user": "ana", "host": "mac-158", "updated": work.now_iso(), "sessions": [],
                                      "locks": []}))
        self.assertFalse(work.write_presence([], a=a))  # nothing changed: nothing for the sync to commit
        self.assertTrue(legacy.is_file())
        self.assertEqual([t["host"] for t in work.team()], [a["host"]])

    def test_this_macs_old_presence_file_goes_and_another_macs_stays(self):
        self.me()
        # last written 31 minutes ago: no older session of this Mac writes it any more
        legacy = {"user": "ana", "updated": work.now_iso(time.time() - 31 * 60), "sessions": [], "locks": []}
        work.PRESENCE.mkdir(parents=True, exist_ok=True)
        for host in ("mac-158", "mac-55"):  # mac-158 is this Mac's name now; mac-55 another Mac's
            (work.PRESENCE / f"ana@{host}.json").write_text(json.dumps(dict(legacy, host=host)))
        self.name = "andress-mbp-2"
        work.write_presence([], a=work.actor("human", user="ana"))
        files = sorted(f.name for f in work.PRESENCE.glob("*.json"))
        self.assertEqual(files, sorted([f"ana@{self.me()['host']}.json", "ana@mac-55.json"]))
        self.assertEqual(sorted(m["host"] for m in work.team()), sorted([self.me()["host"], "mac-55"]))

    def test_a_rename_between_two_calls_keeps_the_lesson_usage_counted_once(self):
        from devkit import lessons
        with mock.patch.object(lessons, "LESSONS", self.handoff.root / "lessons"), \
                mock.patch.object(lessons, "USAGE_LOCAL", core.STATE / "lessons-usage.json"):
            lessons.track(["L-1"], "shown")
            lessons.publish_usage(a=work.actor("human", user="ana"))
            self.name = "andress-mbp-2"
            lessons.track(["L-1"], "shown")
            lessons.publish_usage(a=work.actor("human", user="ana"))
            self.assertEqual(len(list((self.handoff.root / "lessons" / "usage").glob("*.json"))), 1)
            self.assertEqual(lessons.usage()["L-1"]["shown"], 2)

    def test_a_person_is_shown_by_the_hostname_label_of_their_machine(self):
        a = self.me()
        work.PRESENCE.mkdir(parents=True, exist_ok=True)
        for user, host, label in (("bob", "m-0000beef", "mini"), ("eve", "m-0000dead", "<b>x</b>")):
            (work.PRESENCE / f"{user}@{host}.json").write_text(json.dumps({"user": user, "host": host, "label": label,
                                                                          "updated": work.now_iso()}))
        self.assertEqual(work.display(f"ana@{a['host']}"), "ana@mac-158")  # this Mac: its hostname now
        self.assertEqual(work.display(f"ana@{a['host']} (codex:s1)"), "ana@mac-158 (codex:s1)")  # a lock holder
        self.assertEqual(work.display("bob@m-0000beef"), "bob@mini")  # another Mac: the label its presence publishes
        self.assertEqual(work.display("bob@m-0000beef", team=[{"user": "bob", "host": "m-0000beef",
                                                               "label": "studio"}]), "bob@studio")
        self.assertEqual(work.display("eve@m-0000dead"), "eve@m-0000dead")  # a label that is no hostname: the id
        self.assertEqual(work.display("cy@m-00000000"), "cy@m-00000000")  # no presence: the id
        self.assertEqual(work.display("ana@mac-55"), "ana@mac-55")  # a record from before the id names its host
        self.assertEqual(work.display(""), "")

    def test_the_notes_task_list_shows_a_lock_by_the_holders_hostname(self):
        from devkit import registry
        work.claim(work.create("acme/api", "Fix login", a=self.me()), a=self.me())
        self.assertIn("🔒 ana@mac-158", registry.render_tasks_block("acme/api"))

    def test_presence_lists_this_macs_locks_across_a_rename(self):
        it = work.claim(work.create("acme/api", "Fix login", a=self.me()), a=self.me())
        other = self.legacy_lock("ana@mac-55", time.time())
        self.name = "andress-mbp-2"
        work.write_presence([], a=work.actor("human", user="ana"))
        held = [json.loads(f.read_text()) for f in self.handoff.root.glob("presence/*.json")][0]["locks"]
        self.assertEqual([x["id"] for x in held], [it["meta"]["id"]])
        self.assertNotIn(other["meta"]["id"], [x["id"] for x in held])


if __name__ == "__main__":
    unittest.main()
