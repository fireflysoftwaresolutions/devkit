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
    # `dev sync`'s commit, pull and push on this Mac -> what it warned about
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        cli.commit_push(quiet=True)
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
            self.assertIn("1 handoff note was set aside after a sync conflict (acme/api):", issues[0][1])


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


if __name__ == "__main__":
    unittest.main()
