# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev sync` between two Macs that share the handoff repository's remote, when both changed a project's note: two
# clones of a bare remote in a temporary folder (tests/test_work.py's TwoMacs), the pull and push of `dev sync`
# (cli.commit_push), no network and no background sync.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_sync
import contextlib
import io
import json
import re
import shlex
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(TREE / "tests"))

from devkit import audit, cli, core, lessons, notes, registry, setup, work  # noqa: E402
from test_work import A, B, TwoMacs, _git  # noqa: E402

PID = "acme/api"
# The merge rules devkit 26.11.14 and older wrote in a handoff repository: none for the month's activity log (log/).
OLDER_RULES = ("# devkit: safe merges between machines and developers\nsessions/** merge=union\n"
               "presence/*.json merge=ours\nPROJECTS.md merge=ours\nTASKS.md merge=ours\nACTIVITY.md merge=ours\n"
               "registry.json merge=ours\n")


def note_text(auto: str, state: str = "") -> str:
    # A note as registry.update_note writes it: the template, the auto block with `auto` in it, the tasks block of the
    # work items this Mac has, and `state` as the hand-written Current state when given.
    text = notes.new_note(PID).replace(f"{registry.AUTO_START}\n{registry.AUTO_END}",
                                       f"{registry.AUTO_START}\n{auto}\n{registry.AUTO_END}")
    text = text.replace("## Tasks\n### Now\n### Next\n### Later\n### Done\n",
                        "## Tasks\n" + registry.render_tasks_block(PID) + "\n")
    if state:
        text = re.sub(r"(?m)^_What works, what is half done.*$", state, text)
    return text


def write_note(root: Path, text: str, pid: str = PID) -> Path:
    f = root / "projects" / f"{pid}.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    return f


def sync() -> str:
    # `dev sync`'s commit, pull and push on this Mac, with its push due -> what it warned about
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        cli.commit_push(quiet=True, urgent=True)
    return err.getvalue()


class NoteConflictTests(unittest.TestCase):
    def test_two_macs_writing_a_note_whose_generated_blocks_differ_both_reach_the_remote(self):
        # Each Mac creates the project's note with its own auto block (its own checkout and branch) and adds a work
        # item; this Mac syncs after the other pushed: the sync finishes, everything is pushed, the note has one auto
        # block and its hand-written text, and its tasks block, regenerated after the pull, lists both Macs' items.
        with TwoMacs() as m:
            with m.on_other():
                work.create(PID, "Their item", a=B)
                write_note(m.other, note_text("- **Main checkout:** `feat/bob` · clean"))
                _git(m.other, "add", "-A")
                _git(m.other, "commit", "-qm", "note from the other Mac")
                _git(m.other, "push", "-q")
            work.create(PID, "Our item", a=A)
            f = write_note(m.here, note_text("- **Main checkout:** `main` · clean"))
            work._commit([f], "note from this Mac", push=False)
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", warned)
            self.assertFalse(work.push_failed_file().exists(), m.problem())
            pushed = _git(m.here, "show", f"origin/main:projects/{PID}.md")
            self.assertNotIn("<<<<<<<", pushed)
            self.assertEqual(pushed.count(registry.AUTO_START), 1)
            self.assertIn("## Purpose\n_What this project is", pushed)
            tasks = pushed.split(registry.TASKS_START, 1)[1].split(registry.TASKS_END, 1)[0]
            self.assertIn("Their item", tasks)
            self.assertIn("Our item", tasks)

    def test_the_sync_waits_for_a_note_writer_before_its_rebase_touches_any_note(self):
        # The same two notes, while another process of this Mac writes notes (it holds the notes lock, as dev note set
        # or an embargo does). The sync fetches, then asks for the notes lock before git rewrites any note, and
        # rebases only once the writer is done: the writer never sees a rebase under way, and the sync still pushes
        # the note with both Macs' items in its tasks block.
        from devkit import core
        with TwoMacs() as m:
            with m.on_other():
                work.create(PID, "Their item", a=B)
                write_note(m.other, note_text("- **Main checkout:** `feat/bob` · clean"))
                _git(m.other, "add", "-A")
                _git(m.other, "commit", "-qm", "note from the other Mac")
                _git(m.other, "push", "-q")
            work.create(PID, "Our item", a=A)
            f = write_note(m.here, note_text("- **Main checkout:** `main` · clean"))
            work._commit([f], "note from this Mac", push=False)
            held, asked, seen, order = threading.Event(), threading.Event(), [], []
            real = registry.notes_lock

            def notes_lock(wait=30.0):  # the sync asks for the notes lock
                if threading.current_thread() is threading.main_thread():
                    asked.set()
                return real(wait)

            def writer():  # another process's note writer: a hold of the notes lock of its own
                with core.Lock("notes") as got:
                    held.set()
                    if got and asked.wait(10):  # while the sync waits: no rebase under way, the fetched commits not in
                        seen.append((m.stopped(), _git(m.here, "rev-list", "--count", "HEAD..origin/main") != "0"))
                    order.append("writer done")

            t = threading.Thread(target=writer)
            t.start()
            self.assertTrue(held.wait(10))
            with mock.patch.object(registry, "notes_lock", notes_lock):
                warned = sync()
            order.append("sync done")
            t.join(30)
            self.assertEqual(seen, [([], True)], "the rebase began before the writer was done")
            self.assertEqual(order, ["writer done", "sync done"])
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", warned)
            pushed = _git(m.here, "show", f"origin/main:projects/{PID}.md")
            self.assertNotIn("<<<<<<<", pushed)
            tasks = pushed.split(registry.TASKS_START, 1)[1].split(registry.TASKS_END, 1)[0]
            self.assertIn("Their item", tasks)
            self.assertIn("Our item", tasks)

    def test_a_conflict_in_a_notes_hand_written_text_keeps_the_remotes_and_still_pushes_everything_else(self):
        # Both Macs rewrite the Current state of acme/api's note; this Mac's commit also changes acme/web's note, and it
        # added a work item. The sync finishes: acme/web's note and the item reach the remote, acme/api's note is the
        # remote's, and this Mac's version is saved where the warning says, with a command that brings it back.
        with TwoMacs() as m:
            f = write_note(m.here, note_text("- **Main checkout:** `main`", "State: a draft."))
            web = write_note(m.here, "# acme/web\n\nState: a draft.\n", pid="acme/web")
            work._commit([f, web], "notes", push=False)
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            with m.on_other():
                write_note(m.other, note_text("- **Main checkout:** `main`", "State: their words."))
                _git(m.other, "commit", "-qam", "their state")
                _git(m.other, "push", "-q")
            write_note(m.here, note_text("- **Main checkout:** `main`", "State: our words."))
            web.write_text("# acme/web\n\nState: shipped.\n")
            work._commit([f, web], "our state", push=False)
            iid = work.create("acme/web", "Ship the web", a=A)["meta"]["id"]
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", warned)
            self.assertIn("State: shipped.", _git(m.here, "show", "origin/main:projects/acme/web.md"))
            _git(m.here, "show", f"origin/main:items/acme/web/{iid}.md")
            self.assertIn("State: their words.", f.read_text())
            self.assertNotIn("State: our words.", f.read_text())
            self.assertIn(f"projects/{PID}.md", warned)
            cmd = re.search(r"`(git -C .+? merge-file .+?)`", warned)
            self.assertIsNotNone(cmd, warned)
            saved = [w for w in shlex.split(cmd.group(1)) if w.endswith(".md")][-1]
            self.assertIn("State: our words.", Path(saved).read_text())
            recs = audit.query(days=1, action="handoff.note-conflict")
            self.assertEqual([(r["outcome"], r["target"]) for r in recs], [("blocked", f"projects/{PID}.md")])
            # the command in the warning brings this Mac's lines back next to the remote's, between conflict markers
            subprocess.run(shlex.split(cmd.group(1)), capture_output=True)
            merged = f.read_text()
            self.assertIn("State: our words.", merged)
            self.assertIn("State: their words.", merged)
            self.assertIn("<<<<<<<", merged)

    def test_two_local_commits_that_conflict_on_one_note_come_back_whole_with_the_command(self):
        # This Mac committed twice to its note while the other Mac rewrote the same line: the first commit changed the
        # Current state and added a paragraph, the second changed the Current state again. The sync keeps the remote's
        # note and prints one command, which brings back both commits' changes: the paragraph and the last state.
        with TwoMacs() as m:
            f = write_note(m.here, note_text("- auto", "State: base."))
            work._commit([f], "base", push=False)
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            with m.on_other():
                write_note(m.other, note_text("- auto", "State: theirs."))
                _git(m.other, "commit", "-qam", "theirs")
                _git(m.other, "push", "-q")
            write_note(m.here, note_text("- auto", "State: ours one.") + "\nParagraph P: keep me.\n")
            work._commit([f], "ours 1: state and a paragraph", push=False)
            write_note(m.here, note_text("- auto", "State: ours two.") + "\nParagraph P: keep me.\n")
            work._commit([f], "ours 2: state again", push=False)
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertNotIn("Paragraph P", f.read_text())
            cmds = set(re.findall(r"`(git -C .+? merge-file .+?)`", warned))
            self.assertEqual(len(cmds), 1, warned)
            subprocess.run(shlex.split(cmds.pop()), capture_output=True)
            merged = f.read_text()
            self.assertIn("Paragraph P: keep me.", merged)
            self.assertIn("State: ours two.", merged)
            self.assertIn("State: theirs.", merged)

    def test_notes_set_aside_by_two_pulls_in_the_same_second_keep_both_copies(self):
        # Two pulls, each stopping on this Mac's change to acme/api's Current state, save their copies in the same
        # second (a fixed clock): the second copy does not replace the first, each holds its own pull's text, and the
        # problem shown by dev status names the note once.
        from devkit import core

        def strftime(fmt, *a):
            return "20261109-120000" if fmt == "%Y%m%d-%H%M%S" else time.strftime(fmt, *a)

        clock = mock.Mock(wraps=time, strftime=strftime)
        with TwoMacs() as m, mock.patch.object(work, "time", clock), \
                mock.patch.object(registry, "STATE", core.STATE):
            f = write_note(m.here, note_text("- auto", "State: base."))
            work._commit([f], "base", push=False)
            _git(m.here, "push", "-q")
            for n in ("one", "two"):
                _git(m.other, "pull", "-q")
                with m.on_other():
                    write_note(m.other, note_text("- auto", f"State: theirs {n}."))
                    _git(m.other, "commit", "-qam", f"theirs {n}")
                    _git(m.other, "push", "-q")
                write_note(m.here, note_text("- auto", f"State: ours {n}."))
                work._commit([f], f"ours {n}", push=False)
                sync()
            copies = sorted((core.STATE / "handoff-conflicts").rglob("*.mine.md"))
            self.assertEqual(sorted(re.search(r"State: ours \w+\.", c.read_text()).group(0) for c in copies),
                             ["State: ours one.", "State: ours two."])
            issues = registry.handoff_conflict_issues()
            self.assertEqual(len(issues), 1)
            self.assertIn("acme/api: its handoff note was set aside after a sync conflict: ", issues[0][1])
            self.assertIn(" and 1 older copy (", issues[0][1])


def append(root: Path, rel: str, line: str, heading: str = "") -> None:
    # A line added at the end of an append-only log, as gc._log and work.log_session write it (the heading first when
    # the file is new)
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((f.read_text() if f.exists() else heading + "\n\n") + f"- {line}\n")


class AppendOnlyLogTests(unittest.TestCase):
    def test_a_mac_whose_own_unpushed_commit_adds_the_log_rule_still_pulls_a_conflict_in_the_months_log(self):
        # The live failure: the remote has an older devkit's merge rules, and since the last pull both Macs added a line
        # to the month's activity log and wrote the project's note. This Mac then ran `dev update` (dev install adds
        # the log/ rule) and committed, so the rule is only in its own unpushed commit, which git does not apply to
        # that commit's own rebase. The sync still finishes: the remote gets both Macs' lines once each, the note with
        # one auto block, and the new rule, which every Mac's git applies from then on.
        with TwoMacs() as m:
            (m.here / ".gitattributes").write_text(OLDER_RULES)
            append(m.here, "log/2026-11.md", "2026-11-10 09:00 · gc · removed a merged worktree", "# Activity log 2026-11")
            write_note(m.here, note_text("- **Main checkout:** `main` · clean"))
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "start")
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            with m.on_other():
                append(m.other, "log/2026-11.md", "2026-11-11 10:00 · free · docker trim: from the other Mac")
                write_note(m.other, note_text("- **Main checkout:** `feat/bob` · clean"))
                _git(m.other, "commit", "-qam", "the other Mac's sync")
                _git(m.other, "push", "-q")
            with mock.patch.object(setup, "HANDOFF", m.here):
                setup.handoff_merge_rules()
            append(m.here, "log/2026-11.md", "2026-11-11 10:05 · adopt · from this Mac")
            write_note(m.here, note_text("- **Main checkout:** `fix/sync` · clean"))
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "this Mac's sync")
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", m.problem() + warned)
            self.assertFalse(work.push_failed_file().exists(), m.problem())
            log = _git(m.here, "show", "origin/main:log/2026-11.md").splitlines()
            self.assertEqual(log, ["# Activity log 2026-11", "", "- 2026-11-10 09:00 · gc · removed a merged worktree",
                                   "- 2026-11-11 10:00 · free · docker trim: from the other Mac",
                                   "- 2026-11-11 10:05 · adopt · from this Mac"])
            pushed = _git(m.here, "show", f"origin/main:projects/{PID}.md")
            self.assertNotIn("<<<<<<<", pushed)
            self.assertEqual(pushed.count(registry.AUTO_START), 1)
            self.assertEqual(_git(m.here, "check-attr", "merge", "--", "log/2026-12.md"), "log/2026-12.md: merge: union")
            self.assertIn("log/** merge=union", _git(m.here, "show", "origin/main:.gitattributes").splitlines())

    def test_two_macs_that_start_the_same_new_month_logs_keep_one_heading_and_both_lines(self):
        # A handoff repository with no merge rules at all (made by hand, or imported): on the first sync of a month both
        # Macs created the month's activity log and session log. The sync finishes, and each pushed log has its heading
        # once and both Macs' lines.
        with TwoMacs() as m:
            logs = {"log/2026-12.md": "# Activity log 2026-12",
                    f"sessions/{PID}/2026-12.md": f"# Sessions · {PID} · 2026-12"}
            with m.on_other():
                for rel, heading in logs.items():
                    append(m.other, rel, "from the other Mac", heading)
                _git(m.other, "add", "-A")
                _git(m.other, "commit", "-qm", "the other Mac's December")
                _git(m.other, "push", "-q")
            for rel, heading in logs.items():
                append(m.here, rel, "from this Mac", heading)
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "this Mac's December")
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", m.problem() + warned)
            for rel, heading in logs.items():
                self.assertEqual(_git(m.here, "show", f"origin/main:{rel}").splitlines(),
                                 [heading, "", "- from the other Mac", "- from this Mac"], rel)


@contextlib.contextmanager
def lessons_of(root: Path):
    # The lessons of one Mac's handoff repository (TwoMacs points the rest of devkit at it)
    with mock.patch.object(lessons, "LESSONS", root / "lessons"), mock.patch.object(lessons, "HANDOFF", root):
        yield


@contextlib.contextmanager
def lesson_macs():
    # TwoMacs with this Mac's lessons, their usage and state in the test's folder, and no model to judge them
    with TwoMacs() as m, lessons_of(m.here), mock.patch.object(lessons, "STATE", m.base / "state"), \
            mock.patch.object(lessons, "USAGE_LOCAL", m.base / "state" / "lessons-usage.json"), \
            mock.patch.dict(core.CFG, {"ai": {"enabled": False}}):
        yield m


def shared_lesson(m, title: str = "Migration ran twice", scope: str = "global") -> str:
    # A global lesson recorded on this Mac, pushed, and pulled on the other one. -> its id
    lid = lessons.add(scope, title, files=["db/**"], happened="it ran twice in CI", cause="the job retried a step",
                      prevention="make the migration idempotent", a=A)["meta"]["id"]
    _git(m.here, "push", "-q")
    _git(m.other, "pull", "-q")
    return lid


def unpushed(m) -> str:
    return _git(m.here, "rev-list", "--count", "@{u}..HEAD")


class LessonConflictTests(unittest.TestCase):
    def test_two_macs_recording_a_repeat_of_the_same_lesson_both_count_after_the_sync(self):
        # Each Mac recorded a repeat of the same lesson since the last pull (`dev lesson recur`), as two Macs did on
        # the live handoff repository: both changed its occurrences, Evidence, changelog and version. This Mac's sync
        # finishes and pushes a lesson that counts both repeats, keeps both evidence lines, and lists both changelog
        # entries, this Mac's on top, numbered after the other Mac's.
        with TwoMacs() as m, lessons_of(m.here), mock.patch.object(lessons, "STATE", m.base / "state"), \
                mock.patch.object(lessons, "USAGE_LOCAL", m.base / "state" / "lessons-usage.json"), \
                mock.patch.dict(core.CFG, {"ai": {"enabled": False}}):
            lid = lessons.add("global", "Migration ran twice", files=["db/**"], happened="it ran twice in CI",
                              cause="the job retried a step", prevention="make the migration idempotent",
                              a=A)["meta"]["id"]
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            with m.on_other(), lessons_of(m.other):
                lessons.recur(lessons.find(lid), "CI run 8 on the other Mac", a=B)
                _git(m.other, "push", "-q")
            lessons.recur(lessons.find(lid), "CI run 7 on this Mac", a=A)
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", m.problem() + warned)
            pushed = lessons.parse(_git(m.here, "show", f"origin/main:lessons/global/{lid}.md") + "\n")
        self.assertEqual((pushed["meta"]["occurrences"], pushed["meta"]["version"]), (3, 3))
        evidence = [ln.split(": ", 1)[1] for ln in pushed["sections"]["Evidence"].splitlines() if ln.startswith("- ")]
        self.assertEqual(evidence, ["CI run 8 on the other Mac", "CI run 7 on this Mac"])
        self.assertEqual([(e.split(" · ")[0], e.split("why: ")[-1]) for e in pushed["changelog"]][:2],
                         [("v3", "CI run 7 on this Mac"), ("v2", "CI run 8 on the other Mac")])
        self.assertEqual(len(pushed["changelog"]), 3)

    def test_two_macs_that_change_a_lessons_status_differently_still_push_and_keep_this_macs_version_aside(self):
        # The Weave Mac's failure: since the last pull this Mac retired a lesson and the other Mac prevented it, and
        # each wrote other files. Neither change can be applied to the other, so every sync aborted its rebase and
        # kept this Mac's commits unpushed. The sync pushes them now: the lesson keeps the other Mac's status with
        # both changelog entries, and this Mac's version is saved aside, with the command that brings it back.
        from test_testx import Env, dev
        base_test = ("import unittest\nimport db\nclass MigrationTests(unittest.TestCase):\n"
                     "    def test_first_run(self):\n"
                     "        self.assertEqual(db.migrate([]), ['export_date'])\n")
        repeated_test = ("    def test_once(self):\n"
                         "        self.assertEqual(db.migrate(db.migrate([])), ['export_date'])\n")
        ref = "tests/test_db.py::test_db.MigrationTests.test_once"
        with Env() as evidence, lesson_macs() as m:
            repo = evidence.repo({"db.py": "def migrate(columns): return columns + ['export_date']\n",
                                  "tests/test_db.py": base_test, ".gitignore": "__pycache__/\n"})
            (repo / "db.py").write_text("def migrate(columns):\n"
                                       "    return columns if 'export_date' in columns else columns + ['export_date']\n")
            (repo / "tests/test_db.py").write_text(base_test + repeated_test)
            rc, out, err = dev(repo, "prove", "--isolated-imports", "--runs", "1", "--json")
            self.assertEqual(rc, 0, out + err)
            lid = shared_lesson(m, scope="acme/proj")
            with m.on_other(), lessons_of(m.other):
                (m.other / "registry.json").write_text(json.dumps({"projects": [
                    {"id": "acme/proj", "path": str(repo)}]}))
                prevented = lessons.prevent(lessons.find(lid), "test", ref, "causal migration proof passed", a=B)
                self.assertEqual(prevented["meta"]["status"], "prevented")
                work.create("acme/api", "The other Mac's item", why="a reason", a=B)
                _git(m.other, "push", "-q")
            lessons.retire(lessons.find(lid), "not needed any more", a=A)
            work.create("acme/api", "This Mac's item", why="a reason", a=A)
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(unpushed(m), "0", m.problem() + warned)
            pushed = lessons.parse(_git(m.here, "show", f"origin/main:lessons/acme/proj/{lid}.md") + "\n")
            aside = sorted((m.base / "state" / "handoff-conflicts" / "lessons" / "acme" / "proj").glob(f"{lid}.*.mine.md"))
            self.assertEqual(len(aside), 1, warned)
            saved = lessons.parse(aside[0].read_text())
            items = _git(m.here, "ls-tree", "-r", "--name-only", "origin/main", "items/acme/api").splitlines()
        self.assertEqual(pushed["meta"]["status"], "prevented")
        self.assertEqual([e.split(" · ")[3].split(" — ")[0] for e in pushed["changelog"]],
                         ["retired", f"prevented by test: {ref}", "recorded"])
        self.assertEqual(saved["meta"]["status"], "retired")
        self.assertIn(f"lessons/acme/proj/{lid}.md", warned)
        self.assertEqual(len(items), 2, items)

    def test_a_lesson_this_mac_archived_while_the_other_counted_a_repeat_keeps_the_repeat_and_pushes(self):
        # This Mac's nightly `dev sleep` archived a lesson while the other Mac recorded a repeat of it: the archive's
        # removal of the lesson's active file meets the other Mac's change to that file. The sync pushes this Mac's
        # commits, and the remote keeps the lesson at its active path with the repeat counted.
        with lesson_macs() as m:
            lid = shared_lesson(m)
            with m.on_other(), lessons_of(m.other):
                lessons.recur(lessons.find(lid), "CI run 8 on the other Mac", a=B)
                _git(m.other, "push", "-q")
            lessons.archive(lessons.find(lid), "idle for six months", a=A)
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(unpushed(m), "0", m.problem() + warned)
            pushed = lessons.parse(_git(m.here, "show", f"origin/main:lessons/global/{lid}.md") + "\n")
        self.assertEqual(pushed["meta"]["occurrences"], 2)
        self.assertIn("CI run 8 on the other Mac", pushed["sections"]["Evidence"])


class HeldPolicyTests(unittest.TestCase):
    def test_a_team_policy_this_mac_cannot_load_never_stops_a_pull(self):
        # This Mac's policy/audit.json is broken and not committed while the other Mac pushes a change to the same
        # file: the pull's git reset --keep refused to touch it, so this Mac stopped pulling and pushing for good. Now
        # the pull keeps origin's version in the repository, moves this Mac's text under devkit's state with its time
        # and reason, records it, and goes on; devkit says so until the developer drops the held copy.
        with TwoMacs() as m:
            policy = m.here / "policy" / "audit.json"
            policy.parent.mkdir(parents=True)
            policy.write_text(json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}"]}}))
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "team policy")
            _git(m.here, "push", "-q")
            _git(m.other, "pull", "-q")
            theirs = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}", "zulu-[a-z]+"]}})
            (m.other / "policy" / "audit.json").write_text(theirs)
            (m.other / "other.md").write_text("from the other Mac\n")
            _git(m.other, "add", "-A")
            _git(m.other, "commit", "-qm", "the team adds a pattern")
            _git(m.other, "push", "-q")
            broken = {"policy/audit.json": '{"redact": ', "policy/progress.json": "{"}  # one changed, one new
            for rel, body in broken.items():
                (m.here / rel).write_text(body)
            _git(m.here, "fetch", "-q", "origin")
            ok, problem = work.rebase_fetched()
            self.assertTrue(ok, problem)
            self.assertEqual(policy.read_text(), theirs)  # origin's version, in the repository
            self.assertTrue((m.here / "other.md").exists())
            self.assertFalse((m.here / "policy" / "progress.json").exists())
            self.assertEqual(_git(m.here, "status", "--porcelain"), "")
            held = work.held_policies()
            self.assertEqual(sorted((h["path"], h["why"]) for h in held),
                             [("policy/audit.json", "is not valid JSON"),
                              ("policy/progress.json", "is not valid JSON")])
            for h in held:
                saved = Path(h["saved"])
                self.assertEqual(saved.read_text(), broken[h["path"]])  # this Mac's text, to repair
                self.assertTrue(saved.is_relative_to(core.STATE))
                self.assertNotIn(str(m.here), str(saved))
                self.assertEqual(saved.stat().st_mode & 0o077, 0)  # it may hold a secret
                self.assertRegex(h["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            records = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertEqual(sorted(r["target"] for r in records), sorted(broken))
            self.assertEqual({r["severity"] for r in records}, {"critical"})
            line = work.held_policy_line()
            for h in held:
                self.assertIn(h["path"], line)
                self.assertIn(core.tilde(h["saved"]), line)
            self.assertIn("is not valid JSON", line)
            self.assertIn("dev audit config", line)
            # the next pull holds nothing again, and the notice ends once the developer drops (deletes) each copy
            self.assertEqual(work.hold_broken_policies(m.here), [])
            for h in held:
                Path(h["saved"]).unlink()
            self.assertEqual((work.held_policies(), work.held_policy_line()), ([], ""))

    def test_a_hold_whose_reason_cannot_be_written_is_still_listed_and_never_stops_the_sync(self):
        # a full disk or a read-only state folder: the copy is held all the same, and listed by its own name
        with TwoMacs() as m:
            (m.here / "policy").mkdir()
            (m.here / "policy" / "audit.json").write_text('{"redact": ')
            real = work._write_private

            def write(path, data):
                if path.suffix == ".why":
                    raise OSError(28, "No space left on device")
                real(path, data)

            with mock.patch.object(work, "_write_private", write):
                held = work.hold_broken_policies(m.here)
            self.assertEqual([(p, why) for p, why, _ in held], [("policy/audit.json", "is not valid JSON")])
            self.assertFalse((m.here / "policy" / "audit.json").exists())
            listed = work.held_policies()
            self.assertEqual([h["path"] for h in listed], ["policy/audit.json"])
            self.assertRegex(listed[0]["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d$")
            self.assertIn("policy/audit.json", work.held_policy_line())

    def test_a_moved_policy_is_put_back_when_git_cannot_restore_the_repository_version(self):
        # A file this Mac cannot read is moved, not copied; when the checkout after the move failed (another git
        # held index.lock), the file stayed gone and the next git add -A committed a deletion of the team policy.
        with TwoMacs() as m:
            policy = m.here / "policy" / "audit.json"
            policy.parent.mkdir()
            policy.write_text(json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}"]}}))
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "team policy")
            policy.write_text('{"redact": ')
            policy.chmod(0)
            lock = m.here / ".git" / "index.lock"
            lock.write_text("")
            try:
                held = work.hold_broken_policies(m.here)
            finally:
                lock.unlink()
                policy.chmod(0o600)
            self.assertEqual([(p, why, saved) for p, why, saved in held], [("policy/audit.json", "cannot be read", None)])
            self.assertEqual(policy.read_text(), '{"redact": ')  # back where it was, never a deletion to commit
            self.assertEqual((_git(m.here, "diff", "--name-only"), _git(m.here, "diff", "--cached", "--name-only")),
                             ("policy/audit.json", ""))
            self.assertEqual(work.held_policies(), [])

    def test_a_policy_file_that_loads_is_never_held(self):
        with TwoMacs() as m:
            (m.here / "policy").mkdir()
            (m.here / "policy" / "audit.json").write_text(json.dumps({"redact": {"extra": ["zulu-[a-z]+"]}}))
            (m.here / "policy" / "notes.md").write_text("{")  # not a policy file
            self.assertEqual(work.hold_broken_policies(m.here), [])
            self.assertEqual(len(_git(m.here, "status", "--porcelain", "--untracked-files=all").splitlines()), 2)
            self.assertEqual(work.held_policies(), [])


class TeamPolicySyncTests(unittest.TestCase):
    def test_a_sync_never_publishes_a_broken_team_policy_and_never_stops(self):
        # A cut-off policy/audit.json once reached every Mac, where it withholds all audit text; left uncommitted
        # instead, it stopped this Mac's pulls once the other Mac changed the same file. Each sync now moves this
        # Mac's text aside, keeps origin's version, pulls and pushes the rest, and says where the text went, until
        # the developer drops it; a repaired change is published like any other.
        with TwoMacs() as m:
            remote = m.base / "remote.git"
            self.assertNotEqual(core.HANDOFF.resolve(), Path("~/Development/.handoff").expanduser().resolve())
            policy = m.here / "policy" / "audit.json"
            policy.parent.mkdir(parents=True)
            policy.write_text(json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}"]}}))
            sync()
            _git(m.other, "pull", "-q")
            theirs = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}", "zulu-[a-z]+"]}})
            (m.other / "policy" / "audit.json").write_text(theirs)
            (m.other / "other.md").write_text("from the other Mac\n")
            _git(m.other, "add", "-A")
            _git(m.other, "commit", "-qm", "the team adds a pattern")
            _git(m.other, "push", "-q")
            for body, why in (('{"redact": ', "is not valid JSON"),
                              (json.dumps({"audit.redact.extra": ["acmetok-[a-z0-9]{14}"]}), "has an unknown key")):
                with self.subTest(policy=body):
                    policy.write_text(body)
                    (m.here / "policy" / "progress.json").write_text("{")  # a new file, broken too
                    write_note(m.here, note_text("synced with " + why))
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(cli.commit_push(quiet=False, urgent=True), "pushed")
                    self.assertEqual(_git(remote, "show", "main:policy/audit.json"), theirs)
                    self.assertEqual(policy.read_text(), theirs)
                    self.assertNotIn("policy/progress.json", _git(remote, "ls-tree", "-r", "--name-only", "main"))
                    self.assertIn("synced with " + why, _git(remote, "show", f"main:projects/{PID}.md"))
                    self.assertTrue((m.here / "other.md").exists())
                    for said in ("policy/audit.json", why, "policy/progress.json", core.tilde(work.held_policy_dir())):
                        self.assertIn(said, err.getvalue())
            held = work.held_policies()
            self.assertEqual(sorted(h["path"] for h in held), ["policy/audit.json"] * 2 + ["policy/progress.json"] * 2)
            blocked = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertEqual(len(blocked), 4)
            for h in held:  # dropped: no sync says it any more
                Path(h["saved"]).unlink()
            write_note(m.here, note_text("after the drop"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.commit_push(quiet=False, urgent=True), "pushed")
            self.assertNotIn("policy", err.getvalue())
            repaired = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}", "zulu-[a-z]+", "quebec-[a-z]+"]}})
            policy.write_text(repaired)
            sync()
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), repaired)

    TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"

    def published(self, m) -> tuple:
        # this Mac's team policy, synced once: -> (the file, its last good text, the remote)
        good = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}"]}})
        policy = m.here / "policy" / "audit.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(good)
        sync()
        return policy, good, m.base / "remote.git"

    def test_a_broken_policy_that_cannot_be_moved_aside_is_never_committed_or_pushed(self):
        # The held copy cannot be written (the state folder is not writable, or the disk is full): the sync once
        # recorded it and went on, and its git add -A committed and pushed the broken file, a pasted token with it.
        # Now the file stays out of every commit and on this Mac, and the rest of the sync is pushed.
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            work.held_policy_dir().parent.mkdir(parents=True, exist_ok=True)
            work.held_policy_dir().write_text("a file where the folder goes")
            broken = '{"redact": ' + json.dumps(self.TOKEN)
            policy.write_text(broken)
            write_note(m.here, note_text("pushed beside a policy that stays"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.commit_push(quiet=False, urgent=True), "pushed")
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), good)
            self.assertIn("pushed beside a policy that stays", _git(remote, "show", f"main:projects/{PID}.md"))
            found = subprocess.run(["git", "-C", str(remote), "log", "--all", "-p"], capture_output=True, text=True)
            self.assertNotIn(self.TOKEN, found.stdout)
            self.assertEqual(policy.read_text(), broken)  # this Mac's text is not lost
            self.assertIn("policy/audit.json", _git(m.here, "status", "--porcelain"))
            self.assertEqual(_git(m.here, "diff", "--cached", "--name-only"), "")
            self.assertEqual(work.held_policies(), [])
            blocked = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertTrue(blocked)
            self.assertEqual({(r["target"], r["severity"]) for r in blocked}, {("policy/audit.json", "critical")})
            self.assertIn("could not be moved", err.getvalue())
            self.assertNotIn(self.TOKEN, err.getvalue())

    def test_a_broken_policy_already_committed_on_this_mac_is_never_pushed(self):
        # Committed by a raw git commit, dev initiative import, or an older devkit whose push failed: the sync's
        # check saw only changes not committed yet, so the push published it. The push is refused, said and
        # recorded with the file, the reason and the repair, until a commit repairs it.
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            policy.write_text('{"redact": ' + json.dumps(self.TOKEN))
            _git(m.here, "commit", "-qam", "a raw commit")
            write_note(m.here, note_text("waits for the repair"))
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.commit_push(quiet=False, urgent=True), "failed")
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), good)
            found = subprocess.run(["git", "-C", str(remote), "log", "--all", "-p"], capture_output=True, text=True)
            self.assertNotIn(self.TOKEN, found.stdout)
            refused = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertEqual([(r["target"], r["severity"]) for r in refused], [("policy/audit.json", "critical")])
            failed = work.push_failed_file().read_text()  # dev status shows it until a push succeeds
            for said in ("policy/audit.json (commit ", "is not valid JSON", "dev audit config",
                         "git reset --soft @{u}"):
                self.assertIn(said, err.getvalue())
                self.assertIn(said, failed)
            # a repair committed on top would publish the pasted token in the history: the refusal says to undo it
            self.assertNotIn("commit the repair", err.getvalue())
            repaired = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}", "zulu-[a-z]+"]}})
            policy.write_text(repaired)
            _git(m.here, "reset", "-q", "--soft", "@{u}")
            self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), repaired)
            self.assertIn("waits for the repair", _git(remote, "show", f"main:projects/{PID}.md"))
            found = subprocess.run(["git", "-C", str(remote), "log", "--all", "-p"], capture_output=True, text=True)
            self.assertNotIn(self.TOKEN, found.stdout)
            # a committed deletion publishes nothing broken
            policy.unlink()
            _git(m.here, "commit", "-qam", "drop the team policy")
            self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertNotIn("policy/audit.json", _git(remote, "ls-tree", "-r", "--name-only", "main"))

    def test_a_broken_policy_in_an_older_unpushed_commit_is_never_pushed_after_a_repair_on_top(self):
        # The check read only HEAD's version: an older devkit's commit with a pasted token, then a good commit on top,
        # pushed both, and the token reached the remote's history. Every unpushed commit is checked, the refusal
        # names the file and the commit, and its repair (git reset --soft @{u}) pushes one clean commit.
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            policy.write_text('{"redact": ' + json.dumps(self.TOKEN))
            _git(m.here, "commit", "-qam", "an older devkit's commit")
            broken_at = _git(m.here, "rev-parse", "--short", "HEAD")
            repaired = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{15}"]}})
            policy.write_text(repaired)
            _git(m.here, "commit", "-qam", "a repair on top")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.commit_push(quiet=False, urgent=True), "failed")
            found = subprocess.run(["git", "-C", str(remote), "log", "--all", "-p"], capture_output=True, text=True)
            self.assertNotIn(self.TOKEN, found.stdout)
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), good)
            for said in ("policy/audit.json", broken_at, "is not valid JSON", "git reset --soft @{u}"):
                self.assertIn(said, err.getvalue())
                self.assertIn(said, work.push_failed_file().read_text())
            self.assertNotIn(self.TOKEN, err.getvalue())
            refused = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertEqual([r["target"] for r in refused], ["policy/audit.json"])
            _git(m.here, "reset", "-q", "--soft", "@{u}")
            self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), repaired)
            found = subprocess.run(["git", "-C", str(remote), "log", "--all", "-p"], capture_output=True, text=True)
            self.assertNotIn(self.TOKEN, found.stdout)

    def test_a_broken_policy_the_remote_already_has_lets_this_macs_other_commits_through(self):
        # An older Mac pushed a broken policy/audit.json; this Mac's commits, the merge a sync makes among them,
        # change other files only: they publish nothing broken, so they are pushed.
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            (m.here / "policy" / "progress.json").write_text('{"max_parallel": 3}')
            sync()
            _git(m.other, "pull", "-q")
            (m.other / "policy" / "audit.json").write_text("{")
            _git(m.other, "commit", "-qam", "an older Mac broke it")
            _git(m.other, "push", "-q")
            _git(m.here, "fetch", "-q", "origin")
            (m.here / "policy" / "progress.json").write_text('{"max_parallel": 2}')
            _git(m.here, "commit", "-qam", "this Mac's change")
            _git(m.here, "merge", "-q", "--no-edit", "@{u}")
            self.assertEqual(work.unpublishable_commits(m.here), [])
            write_note(m.here, note_text("pushed past the remote's broken file"))
            self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
            self.assertEqual(_git(remote, "show", "main:policy/progress.json"), '{"max_parallel": 2}')
            self.assertIn("pushed past the remote's broken file", _git(remote, "show", f"main:projects/{PID}.md"))
            # a merge that writes a broken version itself publishes it, so it is refused
            _git(m.here, "checkout", "-qb", "side", "origin/main")
            (m.here / "side.md").write_text("a side change\n")
            _git(m.here, "add", "side.md")
            _git(m.here, "commit", "-qm", "a side change")
            _git(m.here, "checkout", "-q", "main")
            _git(m.here, "merge", "-q", "--no-ff", "--no-commit", "side")
            (m.here / "policy" / "progress.json").write_text("[")
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "a merge that broke it")
            self.assertEqual([(rel, sha) for rel, _, sha in work.unpublishable_commits(m.here)],
                             [("policy/progress.json", _git(m.here, "rev-parse", "--short", "HEAD"))])

    def test_the_policy_check_reads_only_the_policy_files_each_unpushed_commit_changed(self):
        # A long unpushed history (a Mac offline for weeks) is checked by one git log and one read per policy change,
        # never a read of every file at every commit. With no upstream, every commit is checked.
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            for n in range(60):
                write_note(m.here, note_text(f"offline day {n}"))
                _git(m.here, "add", "-A")
                _git(m.here, "commit", "-qm", f"day {n}")
                if n == 30:
                    policy.write_text("{")
                    _git(m.here, "commit", "-qam", "broken mid-way")
                    policy.write_text(good)
                    _git(m.here, "commit", "-qam", "repaired")
            reads = []
            real = work._blob
            with mock.patch.object(work, "_blob", lambda root, spec: reads.append(spec) or real(root, spec)):
                refused = work.unpublishable_commits(m.here)
            self.assertEqual([(rel, why) for rel, why, _ in refused], [("policy/audit.json", "is not valid JSON")])
            self.assertEqual(len(reads), 2)
            _git(m.here, "branch", "--unset-upstream")
            self.assertEqual([rel for rel, _, _ in work.unpublishable_commits(m.here)], ["policy/audit.json"])

    def test_a_staged_good_policy_is_kept_when_its_working_copy_is_held(self):
        # git checkout HEAD -- path reset the index too, so a good version staged before the copy broke was lost.
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            staged = json.dumps({"redact": {"extra": ["acmetok-[a-z0-9]{14}", "staged-[a-z]+"]}})
            policy.write_text(staged)
            _git(m.here, "add", "policy/audit.json")
            policy.write_text('{"redact": ')
            sync()
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), staged)
            self.assertEqual(policy.read_text(), staged)
            self.assertEqual([Path(h["saved"]).read_text() for h in work.held_policies()], ['{"redact": '])
        # a staged version that is broken too is held as well, and the repository keeps its last commit
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            policy.write_text("[1")
            _git(m.here, "add", "policy/audit.json")
            policy.write_text('{"redact": ')
            sync()
            self.assertEqual(_git(remote, "show", "main:policy/audit.json"), good)
            self.assertEqual(policy.read_text(), good)
            self.assertEqual(_git(m.here, "status", "--porcelain"), "")
            self.assertEqual(sorted(Path(h["saved"]).read_text() for h in work.held_policies()),
                             ["[1", '{"redact": '])

    def test_a_staged_deletion_is_kept_when_a_broken_new_copy_is_held(self):
        # git checkout HEAD -- path put the file back in the index too, so the deletion the developer staged was lost
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            _git(m.here, "rm", "-q", "--cached", "policy/audit.json")
            policy.write_text('{"redact": ')
            notices = []
            from devkit import watch
            with mock.patch.object(watch, "notice_all", lambda *a, **k: notices.append(a[2])):
                sync()
            self.assertNotIn("policy/audit.json", _git(remote, "ls-tree", "-r", "--name-only", "main"))
            self.assertFalse(policy.exists())
            self.assertEqual(_git(m.here, "status", "--porcelain"), "")
            self.assertEqual([(h["path"], Path(h["saved"]).read_text()) for h in work.held_policies()],
                             [("policy/audit.json", '{"redact": ')])
            self.assertEqual(len(notices), 1)
            self.assertIn("the deletion staged before it", notices[0])

    def test_a_refused_push_is_recorded_once_per_file_and_reason_and_said_at_every_sync(self):
        # Each sync recorded the same refusal again: three syncs gave three critical records, three failed pushes and
        # three session notices. A refusal is recorded once per file and reason, and said at every sync.
        from devkit import watch
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            policy.write_text('{"redact": ')
            _git(m.here, "commit", "-qam", "a raw commit")
            notices = []
            with mock.patch.object(watch, "notice_all", lambda *a, **k: notices.append(a)):
                said = []
                for _ in range(3):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(cli.commit_push(quiet=False, urgent=True), "failed")
                    said.append(err.getvalue())
                for each in said:
                    self.assertRegex(each, r"policy/audit\.json \(commit \w+\) is not valid JSON")

                def records(outcome):
                    return [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == outcome]

                self.assertEqual((len(records("blocked")), len(records("failed")), len(notices)), (1, 1, 1))
                # another reason for the same file is recorded once more
                policy.write_text(json.dumps({"audit.redact.extra": ["acmetok-[a-z0-9]{14}"]}))
                _git(m.here, "commit", "-qam", "another raw commit")
                for _ in range(2):
                    self.assertEqual(cli.commit_push(quiet=True, urgent=True), "failed")
                self.assertEqual((len(records("blocked")), len(records("failed")), len(notices)), (2, 2, 2))
                # once the push goes through, the same refusal later is recorded again
                _git(m.here, "reset", "-q", "--hard", "@{u}")
                write_note(m.here, note_text("between two refusals"))
                self.assertEqual(cli.commit_push(quiet=True, urgent=True), "pushed")
                policy.write_text('{"redact": ')
                _git(m.here, "commit", "-qam", "a raw commit again")
                self.assertEqual(cli.commit_push(quiet=True, urgent=True), "failed")
                self.assertEqual((len(records("blocked")), len(records("failed")), len(notices)), (3, 3, 3))

    def test_a_refusal_of_many_files_keeps_its_repair_in_every_short_copy_and_records_a_new_file(self):
        # The repair came after the list of files, so with two or more files the saved copy (300 characters), the
        # session notice (200) and dev status (120) lost it; and a refusal was compared on those 300 characters, so a
        # new file past them got no record or notice. The repair comes first, and refusals compare (file, reason).
        from devkit import watch
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            names = ["audit", "progress", "ops", "alpha", "bravo"]
            for name in names:
                (m.here / "policy" / f"{name}.json").write_text("{")
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "five broken files")
            notices = []
            with mock.patch.object(watch, "notice_all", lambda *a, **k: notices.append(a[2])):
                self.assertEqual(cli.commit_push(quiet=True, urgent=True), "failed")
                saved = json.loads(work.push_failed_file().read_text())["error"]
                with mock.patch.object(registry, "STATE", core.STATE):  # dev status reads the same file
                    shown = registry._handoff_issues()[0][1]
                for short in (saved, notices[0], shown):
                    self.assertIn("git reset --soft @{u}", short)
                self.assertIn("dev audit config", saved)
                self.assertIn("5 file(s): policy/alpha.json (commit ", saved)
                self.assertTrue(work.push_handoff()[1].endswith("; and 2 more"))  # whole, it counts the rest

                def failed():
                    return [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "failed"]

                self.assertEqual((len(failed()), len(notices)), (1, 1))
                self.assertEqual(cli.commit_push(quiet=True, urgent=True), "failed")  # the same refusal: said only
                self.assertEqual((len(failed()), len(notices)), (1, 1))
                (m.here / "policy" / "zulu.json").write_text("[")  # a sixth file, past every cut
                _git(m.here, "add", "-A")
                _git(m.here, "commit", "-qm", "a sixth")
                self.assertEqual(cli.commit_push(quiet=True, urgent=True), "failed")
                self.assertEqual((len(failed()), len(notices)), (2, 2))

    def test_a_policy_that_cannot_be_moved_aside_is_recorded_once_and_said_at_every_sync(self):
        with TwoMacs() as m:
            policy, good, remote = self.published(m)
            work.held_policy_dir().parent.mkdir(parents=True, exist_ok=True)
            work.held_policy_dir().write_text("a file where the folder goes")
            policy.write_text('{"redact": ')
            said = []
            for _ in range(3):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(cli.commit_push(quiet=False, urgent=True), "nothing")
                said.append(err.getvalue())
            self.assertEqual(["could not be moved" in each for each in said], [True] * 3)
            blocked = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertEqual([(r["target"], r["severity"]) for r in blocked], [("policy/audit.json", "critical")])
            # moved aside once the folder can be written: that hold is recorded, and a later stuck one again
            work.held_policy_dir().unlink()
            sync()
            work.held_policy_dir().rename(m.base / "held-elsewhere")
            work.held_policy_dir().write_text("a file where the folder goes")
            policy.write_text('{"redact": ')
            sync()
            blocked = [r for r in audit.query() if r["action"] == "handoff.push" and r["outcome"] == "blocked"]
            self.assertEqual(len(blocked), 3)


class WorkingTreeTests(unittest.TestCase):
    def test_a_pull_that_settles_a_conflict_never_takes_away_or_rewrites_this_macs_files(self):
        # While a pull settles a conflict, other devkit processes on this Mac keep reading and writing the handoff
        # repository. A lesson recorded only here stays readable, and a file a writer changed but has not committed
        # yet keeps the writer's text, during the pull and after it.
        with lesson_macs() as m:
            draft = m.here / "plans" / "acme" / "api" / "draft.md"
            draft.parent.mkdir(parents=True)
            draft.write_text("the first draft\n")
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "a plan")
            lid = shared_lesson(m)
            with m.on_other(), lessons_of(m.other):
                lessons.recur(lessons.find(lid), "CI run 8 on the other Mac", a=B)
                _git(m.other, "push", "-q")
            lessons.recur(lessons.find(lid), "CI run 7 on this Mac", a=A)
            mine = lessons.add("global", "Only this Mac has it", files=["api/**"], happened="it broke here",
                               cause="a cause", prevention="a prevention", a=A)["meta"]["id"]
            draft.write_text("the writer's text\n")
            seen, real = [], work.merge_lesson

            def watching(*texts):
                seen.append(((lessons.LESSONS / "global" / f"{mine}.md").exists(), draft.read_text()))
                return real(*texts)
            with mock.patch.object(work, "merge_lesson", watching):
                ok, problem = work.pull_rebase()
            self.assertTrue(ok, problem)
            self.assertEqual(seen, [(True, "the writer's text\n")])
            self.assertEqual(draft.read_text(), "the writer's text\n")
            self.assertEqual(_git(m.here, "status", "--porcelain", "--", "plans"), " M plans/acme/api/draft.md")
            self.assertEqual(lessons.find(lid)["meta"]["occurrences"], 3)

    def test_a_rebase_git_cannot_run_falls_back_to_a_merge_that_pushes_every_commit(self):
        # When the rebase cannot finish (here a pre-rebase hook refuses it, as any git failure would), the pull merges
        # the remote's commits instead of keeping this Mac's commits unpushed: the remote gets both Macs' changes in
        # one merge commit, and a conflict in the month's log keeps both lines, the remote's first.
        with TwoMacs() as m:
            append(m.here, "log/2026-11.md", "2026-11-10 09:00 · gc · removed a merged worktree",
                   "# Activity log 2026-11")
            _git(m.here, "add", "-A")
            _git(m.here, "commit", "-qm", "start")
            iid = m.shared_item("Export invoices")
            hook = m.here / ".git" / "hooks" / "pre-rebase"
            hook.parent.mkdir(parents=True, exist_ok=True)
            hook.write_text("#!/bin/sh\nexit 1\n")
            hook.chmod(0o755)
            with m.on_other():
                append(m.other, "log/2026-11.md", "2026-11-11 10:00 · free · from the other Mac")
                _git(m.other, "commit", "-qam", "the other Mac's log")
                work.create("acme/api", "Import invoices", why="a reason", a=B)
                _git(m.other, "push", "-q")
            append(m.here, "log/2026-11.md", "2026-11-11 10:05 · adopt · from this Mac")
            work.update(m.item(iid), "edit", why="our wording", a=A, body="Our text.")
            warned = sync()
            self.assertEqual(m.stopped(), [], m.problem())
            self.assertEqual(unpushed(m), "0", m.problem() + warned)
            parents = _git(m.here, "rev-list", "--parents", "-1", "origin/main").split()
            log = _git(m.here, "show", "origin/main:log/2026-11.md").splitlines()
            self.assertEqual(m.remote_item(iid)["body"].strip(), "Our text.")
            titles = [x["meta"]["title"] for x in work.all_items("acme/api")]
        self.assertEqual(len(parents), 3, "not a merge commit")
        self.assertEqual(log[-2:], ["- 2026-11-11 10:00 · free · from the other Mac",
                                    "- 2026-11-11 10:05 · adopt · from this Mac"])
        self.assertEqual(sorted(titles), ["Export invoices", "Import invoices"])

    def test_a_lesson_only_this_mac_has_is_found_while_a_rebase_is_left_stopped(self):
        # The Weave Mac's `dev lesson recur <id>`: a pull's rebase was left stopped on a conflict (an older devkit's
        # sync, or `git pull --rebase` by hand) before it replayed this Mac's commit of the lesson, so the lesson's
        # file was not in the working tree. The lesson is found, and its repeat counted.
        with lesson_macs() as m:
            iid = m.shared_item("Export invoices")
            with m.on_other():
                work.update(m.item(iid), "edit", why="their wording", a=B, body="Their text.")
                _git(m.other, "push", "-q")
            work.update(m.item(iid), "edit", why="our wording", a=A, body="Our text.")
            lid = lessons.add("global", "Only this Mac has it", files=["api/**"], happened="it broke here",
                              cause="a cause", prevention="a prevention", a=A)["meta"]["id"]
            self.assertNotEqual(core.git(m.here, "pull", "-q", "--rebase")[0], 0)
            self.assertEqual(m.stopped(), ["rebase-merge"])
            warned = io.StringIO()
            with contextlib.redirect_stderr(warned):
                les = lessons.recur(lessons.find(lid), "CI run 9", a=A)
            self.assertEqual(les["meta"]["occurrences"], 2)
            self.assertEqual(m.stopped(), [])
            self.assertIn("a lesson read aborted it", warned.getvalue())


class MachineNameTests(unittest.TestCase):
    # The handoff repository is shared with the team and keeps its history: a Mac is named there by its machine id,
    # never by its hostname, which often names the person ("anas-macbook-pro"). A sync's commit message named it
    # ("sync: 3 file(s) from <hostname> (m-…)"), and so did audit records, presence and the note's auto block. Every
    # kind of file devkit writes there is made under a hostname of the test's own, synced, and the whole history of
    # the remote and this Mac's audit log (which the sync publishes byte for byte) is scanned for it.
    HOST = "zebrahost-of-ana"

    def test_nothing_a_sync_publishes_names_this_macs_hostname(self):
        import socket
        from devkit import identity
        with TwoMacs() as m, mock.patch.object(identity, "hostname", lambda: self.HOST), \
                mock.patch.object(socket, "gethostname", lambda: "Zebrahost-Of-Ana.local"):
            mid = identity.machine().id
            a = work.actor("claude", "claude:s-test")
            self.assertEqual(a["host"], mid)
            item = work.create(PID, "An item", why="a reason", a=a)
            work.claim(item, a=a, why="mine now", branch="main")
            work.write_presence([{"agent": "claude", "project": PID, "branch": "main", "started": work.now_iso(),
                                  "active": True}], a=a)
            work.log_session(PID, f"{core.fmt_ts(time.time())} · {a['user']}@{a['host']} · claude · a line")
            audit.record("dev.test", "a target", "a detail")
            project = {"id": PID, "path": str(m.base / "repo"), "canonical_ok": True, "canonical": "", "stack": [],
                       "main": None, "worktrees": []}
            write_note(m.here, note_text(registry.render_auto(project)))
            self.assertEqual(sync(), "")
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0")
            remote = m.base / "remote.git"
            history = subprocess.run(["git", "-C", str(remote), "log", "--all", "-p", "--format=%B"],
                                     capture_output=True, text=True).stdout
            self.assertIn(mid, history)  # the scan reads what the sync published: the Mac is there, by its id
            self.assertIn("presence/", history)
            self.assertIn(f"from {mid}", _git(m.here, "log", "-1", "--format=%s"))
            published = history + "".join(f.read_text() for f in sorted(audit.AUDIT.glob("*.jsonl")))
            self.assertIn('"dev.test"', published)
            self.assertNotIn("zebrahost", published.lower())


if __name__ == "__main__":
    unittest.main()
