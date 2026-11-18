# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Sleep: usage, strength decay, merging, forgetting, restoring and hygiene (simulated time)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, identity, lessons, registry, sleep, work  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
DAY = 86400


class SleepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL, sleep.STATE, sleep.LAST, core.HANDOFF)
        lessons.LESSONS, work.HANDOFF, core.HANDOFF = root / "handoff" / "lessons", root / "handoff", root / "handoff"
        lessons.USAGE_LOCAL = root / "state" / "lessons-usage.json"
        sleep.STATE, sleep.LAST = root / "state", root / "state" / "sleep.json"
        self.root = root
        self.ai_off = mock.patch.dict(core.CFG, {"ai": {"enabled": False}})  # never a real model call in tests
        self.ai_off.start()
        self.addCleanup(self.ai_off.stop)

    def tearDown(self):
        (lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL, sleep.STATE, sleep.LAST, core.HANDOFF) = self.saved
        self.tmp.cleanup()

    def add(self, title, scope="acme/api", **kw):
        return lessons.add(scope, title, cause=kw.pop("cause", "c " + title), prevention=kw.pop("prevention", "p"),
                           happened=kw.pop("happened", "it happened: " + title), a=A, **kw)

    def test_use_keeps_a_lesson_alive_and_unused_ones_fade(self):
        used, idle = self.add("Used often", files=["src/used/**"]), self.add("Never used", files=["src/idle/**"])
        now = time.time() + 120 * DAY
        lessons.track([used["meta"]["id"]] * 1, "applied")
        u = lessons.usage()
        u[used["meta"]["id"]]["applied_last"] = now - DAY  # used yesterday, in the simulated future
        self.assertGreater(lessons.strength(used, u, now), 4 * lessons.strength(idle, u, now))

    def test_a_lesson_shown_500_times_with_no_misses_keeps_its_strength(self):
        les, now = self.add("Often shown", files=["src/shown/**"]), time.time()
        u = {les["meta"]["id"]: {"shown": 500, "shown_call": 500}}
        self.assertEqual(lessons.strength(les, u, now), lessons.strength(les, {}, now))

    def test_merge_duplicates_and_keep_the_global_one(self):
        where = {"commands": ["git commit"], "errors": ["FAILED tests/test_api.py"], "activities": ["testing"]}
        g = self.add("Commit chained after a pipe hides failing tests", scope="global",
                     cause="tail exit code hides failing tests", prevention="use pipefail before commit", **where)
        p = self.add("Un commit tras un pipe oculta tests rotos", cause="tail oculta el código de salida",
                     prevention="usa pipefail antes del commit", stack=["python"], new=True,
                     why="a near-duplicate in another language, for the merge", **where)
        acts = lessons.consolidate()
        self.assertIn(("merge", p["meta"]["id"]), [(k, i) for k, i, _ in acts])
        kept = lessons.find(g["meta"]["id"])
        self.assertEqual(kept["meta"]["occurrences"], 2)
        self.assertEqual(lessons.anchors(kept)["stack"], ["python"])  # the merged lesson keeps both anchors
        self.assertTrue((lessons.LESSONS / "archive" / "acme/api" / p["path"].name).exists())

    def test_forget_and_restore(self):
        prevented = self.add("Old prevented", files=["src/prevented/**"])
        lessons.prevent(prevented, "test", "tests/x.py", "covered", a=A)
        retired = self.add("Old retired", files=["src/retired/**"])
        lessons.retire(retired, "no longer applies", a=A)
        fresh = self.add("Fresh and open", files=["src/fresh/**"])
        future = time.time() + 200 * DAY
        acts = {i: k for k, i, _ in lessons.consolidate(now=future)}
        self.assertEqual(acts.get(prevented["meta"]["id"]), "forget")
        self.assertEqual(acts.get(retired["meta"]["id"]), "forget")
        self.assertEqual(acts.get(fresh["meta"]["id"]), "forget")  # open, 200 days unused and weak
        self.assertEqual(lessons.all_lessons(), [])
        back = lessons.restore(fresh["meta"]["id"], "it came back", a=A)
        self.assertEqual(back["meta"]["status"], "open")
        self.assertEqual([x["meta"]["id"] for x in lessons.all_lessons()], [fresh["meta"]["id"]])

    def test_dry_run_changes_nothing(self):
        les = self.add("Retired soon", files=["src/soon/**"])
        lessons.retire(les, "obsolete", a=A)
        acts = lessons.consolidate(dry_run=True, now=time.time() + 40 * DAY)
        self.assertEqual([k for k, _, _ in acts], ["forget"])
        self.assertEqual(len(lessons.all_lessons()), 1)

    def test_prevented_lessons_leave_the_context(self):
        a_ = self.add("Open one", files=["src/a/**"])
        b_ = self.add("Prevented one", files=["src/b/**"])
        lessons.prevent(b_, "test", "tests/test_b.py", "automated", a=A)
        ids = [x["meta"]["id"] for x in lessons.relevant("acme/api")]
        self.assertEqual(ids, [a_["meta"]["id"]])
        self.assertEqual(len(lessons.search("Prevented")), 1)  # still searchable

    def test_hygiene_trims_only_devkit_records(self):
        st = self.root / "state"
        (st / "runs").mkdir(parents=True)
        old_run = st / "runs" / "claude-x.json"
        old_run.write_text("{}")
        os.utime(old_run, (time.time() - 30 * DAY, time.time() - 30 * DAY))
        new_run = st / "runs" / "claude-y.json"
        new_run.write_text("{}")
        (st / "timing").mkdir()
        (st / "timing" / "2020-01.jsonl").write_text("{}\n")
        plan = sleep.hygiene_plan()
        paths = {Path(f).name for _, f, _ in plan}
        self.assertIn("claude-x.json", paths)
        self.assertIn("2020-01.jsonl", paths)
        self.assertNotIn("claude-y.json", paths)
        sleep.apply_hygiene(plan)
        self.assertFalse(old_run.exists())
        self.assertTrue(new_run.exists())


    def test_hygiene_removes_the_codex_calls_left_waiting_for_a_day(self):
        # a Codex command waits for its rollout record at most ten minutes; a session that ended leaves its record
        # of waiting calls and that record's lock behind
        pend = self.root / "state" / "codex-pending"
        (pend / "locks").mkdir(parents=True)
        old, new = pend / "c-old.json", pend / "c-new.json"
        old_lock, new_lock = pend / "locks" / "rec-c-old.lock", pend / "locks" / "rec-c-new.lock"
        for f in (old, new, old_lock, new_lock):
            f.write_text("{}")
        for f in (old, old_lock):
            os.utime(f, (time.time() - 2 * DAY, time.time() - 2 * DAY))
        os.utime(new_lock, (time.time() - 2 * DAY, time.time() - 2 * DAY))  # the lock of a record still in use
        sleep.apply_hygiene(sleep.hygiene_plan())
        self.assertEqual(sorted(f.name for f in pend.rglob("*") if f.is_file()), ["c-new.json", "rec-c-new.lock"])

    def test_hygiene_removes_the_file_locks_nobody_holds_after_a_day(self):
        # core.file_lock leaves one lock file for each file it ever locked; one a process holds now stays
        locks = self.root / "state" / "locks" / "files"
        with mock.patch.object(core, "STATE", self.root / "state"):
            with core.file_lock(self.root / "a.md"):
                pass
            idle = set(locks.iterdir())
            with core.file_lock(self.root / "b.md"):
                held = set(locks.iterdir()) - idle
                for f in locks.iterdir():
                    os.utime(f, (time.time() - 2 * DAY, time.time() - 2 * DAY))
                sleep.apply_hygiene(sleep.hygiene_plan())
                self.assertEqual(set(locks.iterdir()), held)


GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
       "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev", "GIT_COMMITTER_EMAIL": "ana@example.com"}
ME = "m-aaaaaaaa"


# The nightly hygiene takes out of the handoff notes' auto blocks the parts of Macs that no presence record shows for
# 30 days: the presence files (their `updated`) and the commits that wrote them, since the hygiene removes the file
# of a Mac quiet for 14 days. The handoff repository is a real git repository in a temporary folder, its commits
# dated; this Mac's identity is a stand-in.
class AutoPartSleepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(core.real(tmp.name))
        self.handoff, state = self.root / "handoff", self.root / "state"
        self.now = time.time()
        for target in (mock.patch.object(core, "HANDOFF", self.handoff), mock.patch.object(work, "HANDOFF", self.handoff),
                       mock.patch.object(core, "STATE", state), mock.patch.object(sleep, "STATE", state),
                       mock.patch.object(core, "CONFIG_FILE", self.root / "config.json"),
                       mock.patch.object(identity, "machine", lambda now=None: identity.Machine(ME, "mbp-ana", {})),
                       mock.patch.dict(os.environ, GIT)):
            target.start()
            self.addCleanup(target.stop)
        self.handoff.mkdir()
        self.git("init", "-q")
        self.note = self.handoff / "projects" / "acme" / "api.md"
        self.note.parent.mkdir(parents=True)
        parts = "\n\n".join(f"{registry.AUTO_PART.format(m)}\n_Auto-refreshed by `dev sync` on mac-{m[-2:]} · 2026-09-01._"
                             f"\n\n- **Path:** `~/Development/acme/api-{m}`" for m in
                             (ME, "m-bbbbbbbb", "m-cccccccc", "m-dddddddd", "m-eeeeeeee"))
        self.note.write_text(f"# acme/api\n\n{registry.AUTO_START}\n{parts}\n{registry.AUTO_END}\n\n## Session log\n")

    def git(self, *args, days_ago: float = 0):
        at = f"@{int(self.now - days_ago * DAY)} +0000"
        subprocess.run(["git", "-C", str(self.handoff), *args], check=True, capture_output=True,
                       env=dict(os.environ, GIT_AUTHOR_DATE=at, GIT_COMMITTER_DATE=at))

    def presence(self, mid: str, days_ago: float, removed_after: float = None):
        # the Mac's presence file, written and committed `days_ago`; the hygiene removed it `removed_after` later
        f = self.handoff / "presence" / f"ana@{mid}.json"
        f.parent.mkdir(exist_ok=True)
        f.write_text(json.dumps({"user": "ana", "host": mid, "updated": work.now_iso(self.now - days_ago * DAY)}))
        self.git("add", "-A")
        self.git("commit", "-qm", f"presence {mid}", days_ago=days_ago)
        if removed_after is not None:
            self.git("rm", "-q", str(f))
            self.git("commit", "-qm", "sleep: presence removed", days_ago=days_ago - removed_after)

    def kept(self) -> set:
        text = self.note.read_text()
        return set(registry._auto_parts(text[text.index(registry.AUTO_START) + len(registry.AUTO_START):
                                             text.index(registry.AUTO_END)]))

    def test_the_parts_of_macs_no_presence_shows_for_30_days_go_and_the_rest_stay(self):
        self.presence("m-bbbbbbbb", 45, removed_after=14)  # gone: last written 45 days ago
        self.presence("m-cccccccc", 3)  # live
        self.presence("m-dddddddd", 20, removed_after=14)  # quiet 20 days: its file went, its commit is recent
        plan = [(w, f) for w, f, _ in sleep.hygiene_plan(self.now) if w == "auto-part"]
        self.assertEqual(plan, [("auto-part", self.note)] * 2)  # m-bbbbbbbb, and m-eeeeeeee no presence ever named
        sleep.apply_hygiene(sleep.hygiene_plan(self.now), self.now)
        self.assertEqual(self.kept(), {ME, "m-cccccccc", "m-dddddddd"})
        self.assertTrue(self.note.exists())
        self.assertIn("## Session log", self.note.read_text())

    def test_no_part_goes_when_presence_cannot_be_read(self):
        # no presence folder, or no git history of it (not a repository, or a shallow clone that lost the commits);
        # once it can be read again, the parts of the Macs it does not show go
        everyone, git_dir = {ME, "m-bbbbbbbb", "m-cccccccc", "m-dddddddd", "m-eeeeeeee"}, self.handoff / ".git"
        sleep.apply_hygiene(sleep.hygiene_plan(self.now), self.now)
        self.assertEqual(self.kept(), everyone, "no presence folder")
        self.presence("m-cccccccc", 3)
        git_dir.rename(self.root / "git-aside")
        sleep.apply_hygiene(sleep.hygiene_plan(self.now), self.now)
        self.assertEqual(self.kept(), everyone, "not a repository")
        (self.root / "git-aside").rename(git_dir)
        head = subprocess.run(["git", "-C", str(self.handoff), "rev-parse", "HEAD"], capture_output=True, text=True)
        (git_dir / "shallow").write_text(head.stdout)
        self.assertEqual([w for w, _, _ in sleep.hygiene_plan(self.now) if w == "auto-part"], [], "shallow")
        sleep.apply_hygiene(sleep.hygiene_plan(self.now), self.now)
        self.assertEqual(self.kept(), everyone, "shallow")
        (git_dir / "shallow").unlink()
        sleep.apply_hygiene(sleep.hygiene_plan(self.now), self.now)
        self.assertEqual(self.kept(), {ME, "m-cccccccc"})

if __name__ == "__main__":
    unittest.main()
