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
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(TREE / "tests"))

from devkit import audit, cli, notes, registry, work  # noqa: E402
from test_work import A, B, TwoMacs, _git  # noqa: E402

PID = "acme/api"


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

    def test_the_tasks_block_waits_for_the_next_sync_while_another_process_writes_notes(self):
        # The same two notes, while another process holds the notes lock (dev note set, an embargo): the sync still
        # finishes and pushes, and it leaves the note as the pull settled it instead of writing the tasks block over
        # that process's write; the next sync writes it.
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
            with core.Lock("notes") as held:
                self.assertTrue(held)
                warned = sync()
            self.assertEqual(_git(m.here, "rev-list", "--count", "@{u}..HEAD"), "0", warned)
            pushed = _git(m.here, "show", f"origin/main:projects/{PID}.md")
            self.assertIn("Our item", pushed)
            self.assertNotIn("Their item", pushed)
            self.assertEqual(f.read_text().rstrip("\n"), pushed)

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


if __name__ == "__main__":
    unittest.main()
