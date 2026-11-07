# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The handoff check at the end of a turn: commits newer than the note's last real update. Run: python3 -m unittest"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import hooks  # noqa: E402

NOTE = """# acme/api

## Current state

Export works for CSV.

## Next steps

1. PDF export.

## Tasks

- `T-1` CSV export

## Session log

- 2026-11-06 · ana@mbp1 · claude
"""
T0 = 1_793_000_000  # a fixed clock: every time below is relative to it


def commit(repo: Path, msg: str, at: int, email: str = "ana@example.com", files=None):
    env = dict(os.environ, GIT_AUTHOR_DATE=f"@{at} +0000", GIT_COMMITTER_DATE=f"@{at} +0000",
               GIT_AUTHOR_NAME="ana", GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME="ana", GIT_COMMITTER_EMAIL=email)
    for name, text in (files or {"f.txt": msg}).items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True, env=env)


class HandoffStalenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo, self.note, self.state = root / "repo", root / "note.md", root / "state.json"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.note.write_text(NOTE)
        os.utime(self.note, (T0, T0))  # the note was last written at T0

    def tearDown(self):
        self.tmp.cleanup()

    def stale(self, now):
        return hooks.handoff_staleness(str(self.repo), self.note, self.state, "ana@example.com", now)[0]

    def test_commits_after_the_note_make_it_stale_until_the_note_changes(self):
        commit(self.repo, "feat: pdf export", T0 + 600)
        self.assertEqual(len(self.stale(T0 + 700)), 1)  # first sight: the note's file time is its last change
        self.note.write_text(NOTE.replace("Export works for CSV.", "Export works for CSV and PDF."))
        self.assertEqual(self.stale(T0 + 800), [])  # updated: re-armed from now
        commit(self.repo, "fix: pdf margins", T0 + 900)
        self.assertEqual(len(self.stale(T0 + 1000)), 1)  # every new batch of commits asks again, not once a session

    def test_generated_sections_and_other_peoples_commits_do_not_count(self):
        commit(self.repo, "feat: pdf export", T0 + 600)
        self.assertEqual(len(self.stale(T0 + 700)), 1)
        self.note.write_text(NOTE.replace("- `T-1` CSV export", "- `T-1` CSV export ✓").replace(
            "claude\n", "claude\n- 2026-11-06 · ana@mbp1 · codex\n"))
        self.assertEqual(len(self.stale(T0 + 800)), 1)  # a task or session line is not an update of the note
        commit(self.repo, "chore: merge main", T0 + 900, email="bo@example.com")
        self.assertEqual(len(self.stale(T0 + 1000)), 1)  # a teammate's commit pulled in is not this session's work

    def test_first_sight_reads_the_handoff_history_not_the_file_time(self):
        handoff = Path(self.tmp.name) / "handoff"
        subprocess.run(["git", "init", "-q", str(handoff)], check=True)
        note = handoff / "projects" / "acme" / "api.md"
        commit(handoff, "note: current state", T0, files={"projects/acme/api.md": NOTE})
        commit(self.repo, "feat: pdf export", T0 + 300)
        tasks_only = NOTE.replace("- `T-1` CSV export", "- `T-1` CSV export ✓")
        commit(handoff, "tasks: T-1 done", T0 + 500, files={"projects/acme/api.md": tasks_only})
        os.utime(note, (T0 + 900, T0 + 900))  # rewritten later by devkit, still no human change
        stale = hooks.handoff_staleness(str(self.repo), note, self.state, "ana@example.com", T0 + 1000)[0]
        self.assertEqual(len(stale), 1)  # the human sections last changed at T0, before the commit

    def test_no_commits_since_the_note_is_not_stale(self):
        commit(self.repo, "feat: old work", T0 - 600)
        self.assertEqual(self.stale(T0 + 100), [])
        self.assertEqual(hooks.handoff_staleness(str(self.repo), self.note.with_name("none.md"), self.state,
                                                 "ana@example.com", T0)[0], [])  # no note, nothing to check



class PlanReasonScopeTests(unittest.TestCase):
    """A session answers for its own project's plans, not for a plan another session changed in another project."""

    def test_only_this_projects_plans_are_checked(self):
        with tempfile.TemporaryDirectory() as d:
            handoff = Path(d)
            mine = handoff / "plans" / "acme" / "api" / "2026-11-06-export.md"
            theirs = handoff / "plans" / "acme" / "web" / "2026-11-06-redesign.md"
            for f in (mine, theirs):
                f.parent.mkdir(parents=True)
                f.write_text("# A plan edited without dev plan revise\n")  # no body_sha: needs a recorded revision
            saved = hooks.HANDOFF
            hooks.HANDOFF = handoff
            try:
                sess = {"started": T0}
                self.assertEqual(hooks._plans_without_reason(sess, "acme/api"),
                                 ["plans/acme/api/2026-11-06-export.md"])
                self.assertEqual(len(hooks._plans_without_reason(sess, None)), 2)  # outside a project: all of them
            finally:
                hooks.HANDOFF = saved

if __name__ == "__main__":
    unittest.main()
