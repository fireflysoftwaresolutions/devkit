# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Writes to files that several devkit processes share on one Mac: generated files (core.write_if_changed), records
# (work.write_record) and the month's session log (work.log_session, work.replace_session_line). Concurrency is
# real: separate Python processes, started together through tests/_sandbox.py's net, each with a timeout, and every
# process a test starts has ended when the test returns.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_shared_writes
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(TREE / "tests"))
import _sandbox  # noqa: E402

from devkit import core  # noqa: E402

TIMEOUT = 60  # seconds one writer process may take


def together(codes, cwd) -> list:
    # Run each code in its own Python process with devkit importable; they start writing at the same moment (each
    # waits for a shared go-file first). -> their stdout, in order. A process that fails or runs past TIMEOUT fails
    # the test, and every process has ended when this returns.
    go = Path(cwd) / "go"
    head = textwrap.dedent(f"""\
        import os, sys, time
        sys.path.insert(0, {str(TREE / 'lib')!r})
        from pathlib import Path
        from devkit import core
        while not Path({str(go)!r}).exists():
            time.sleep(0.001)
        """)
    procs = []
    try:
        for code in codes:
            procs.append(subprocess.Popen([sys.executable, "-I", "-c", head + textwrap.dedent(code)], cwd=str(cwd),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          stdin=subprocess.DEVNULL, env=_sandbox.env()))
        go.touch()
        out = []
        for p in procs:
            so, se = p.communicate(timeout=TIMEOUT)
            if p.returncode:
                raise AssertionError(f"writer exit {p.returncode}\n{so}\n{se}")
            out.append(so)
        return out
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


class WriteIfChangedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_two_processes_rewriting_one_file_at_once_never_fail_and_leave_one_writers_whole_text(self):
        # Two processes regenerate the same file 200 times each at the same moment: no write fails, and the file
        # holds one writer's whole text, never a mix.
        f = self.dir / "note.md"
        writer = """
            errors = 0
            for i in range(200):
                try:
                    core.write_if_changed(Path({f!r}), "{who}\\n" * 2000 + str(i) + "\\n")
                except OSError:
                    errors += 1
            print(errors)
            """
        out = together([writer.format(f=str(f), who="ana"), writer.format(f=str(f), who="bob")], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "writes that failed, per process")
        lines = f.read_text().splitlines()
        self.assertEqual(len(lines), 2001)
        self.assertEqual(len(set(lines[:-1])), 1, f"the file mixes both writers' text: {set(lines[:-1])}")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["go", "note.md"])  # no temporary file left

    def test_a_rewrite_keeps_the_files_own_permissions(self):
        # A file only its owner may read (a token, a private note) stays that way when devkit rewrites it.
        f = self.dir / "private.json"
        f.write_text("{}\n")
        f.chmod(0o600)
        self.assertTrue(core.write_if_changed(f, '{"a": 1}\n'))
        self.assertEqual(f.read_text(), '{"a": 1}\n')
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)


# A writer of a record: reads it, adds a changelog entry of its own (as `dev plan revise` and `dev task` do) and
# writes it back, `rounds` times; with locked=True it holds the record's lock across its read. -> the entries it was
# told were written, as JSON.
RECORD_WRITER = """
    import json
    from devkit import work
    a = {{"user": "{who}", "host": "mac", "agent": "claude", "session": "{who}"}}
    path, told = Path({path!r}), []
    for i in range({rounds}):
        try:
            with (work.record_lock(path) if {locked} else __import__("contextlib").nullcontext()):
                it = work.parse(path.read_text())
                work._touch(it, a, "edit", why=f"{who} {{i}}")
                work.write_record(path, work.render(it))
            told.append(f"{who} {{i}}")
        except work.RecordLossError:
            pass
    print(json.dumps(told))
    """


class WriteRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "T-1.md"
        from devkit import work
        it = {"meta": {"id": "T-1", "type": "task", "title": "Export invoices", "status": "todo", "version": 1},
              "body": "", "changelog": ["v1 · 2026-11-09T10:00:00Z · ana@mac (claude:ana) · create"]}
        self.path.write_text(work.render(it))

    def entries(self) -> list:
        import re
        return re.findall(r"(?m)^- v(\d+) · .* — why: (\w+ \d+)$", self.path.read_text())

    def test_writers_racing_on_one_record_are_never_told_a_write_succeeded_that_the_record_lost(self):
        # Four processes each change the record 40 times at once, every one from the version it read: a write the
        # record cannot keep is refused (RecordLossError); every write reported as done is in the record, each with a
        # version of its own.
        import json
        out = together([RECORD_WRITER.format(who=w, path=str(self.path), rounds=40, locked=False)
                        for w in ("ana", "bob", "cid", "dee")], self.dir)
        told = sorted(e for o in out for e in json.loads(o))
        kept = self.entries()
        self.assertEqual(told, sorted(why for _, why in kept), "told it was written, but the record lost it")
        versions = [v for v, _ in kept]
        self.assertEqual(len(versions), len(set(versions)), "two writes share a version")

    def test_writers_holding_the_records_lock_across_their_read_all_succeed_in_turn(self):
        # The same race, each writer holding the record's lock from its read to its write: they take turns, none is
        # refused, and the versions run without a gap.
        import json
        out = together([RECORD_WRITER.format(who=w, path=str(self.path), rounds=25, locked=True)
                        for w in ("ana", "bob", "cid")], self.dir)
        told = [e for o in out for e in json.loads(o)]
        self.assertEqual(len(told), 75)
        kept = self.entries()
        self.assertEqual(sorted(told), sorted(why for _, why in kept))
        self.assertEqual(sorted(int(v) for v, _ in kept), list(range(2, 77)))


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        from devkit import work
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.saved = work.SESSIONS
        work.SESSIONS = self.dir / "sessions"
        self.addCleanup(setattr, work, "SESSIONS", self.saved)

    def month(self) -> Path:
        import time
        return self.dir / "sessions" / "acme" / "widget" / f"{time.strftime('%Y-%m')}.md"

    def test_a_session_line_rewritten_on_every_turn_never_drops_a_line_another_session_appends(self):
        # Session A rewrites its own line 60 times (the Stop hook) while session B appends lines (dev handoff --log)
        # until A is done: every line B appended is kept, and A's line is there once, as A last wrote it.
        done = self.dir / "a-done"
        setup = f"""
            from devkit import work
            work.SESSIONS = Path({str(self.dir / 'sessions')!r})
            """
        rewrite = setup + f"""
            for i in range(60):
                work.replace_session_line("acme/widget", "<!-- s:sA -->", f"session A turn {{i}} <!-- s:sA -->")
            Path({str(done)!r}).touch()
            """
        append = setup + f"""
            n = 0
            while not Path({str(done)!r}).exists():
                work.log_session("acme/widget", f"bob line {{n}}")
                n += 1
            print(n)
            """
        written = int(together([rewrite, append], self.dir)[1])
        text = self.month().read_text()
        self.assertGreater(written, 20)
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith("- bob line ")]), written,
                         "lines of session B were lost")
        self.assertEqual([ln for ln in text.splitlines() if "<!-- s:sA -->" in ln],
                         ["- session A turn 59 <!-- s:sA -->"])

    def test_a_session_line_is_appended_when_the_month_has_none_and_replaced_in_place_when_it_has_one(self):
        from devkit import work
        work.log_session("acme/widget", "first")
        f = work.replace_session_line("acme/widget", "<!-- s:s1 -->", "s1 turn 1 <!-- s:s1 -->")
        work.log_session("acme/widget", "last")
        work.replace_session_line("acme/widget", "<!-- s:s1 -->", "s1 turn 2 <!-- s:s1 -->")
        self.assertEqual(f, self.month())
        self.assertEqual(f.read_text().splitlines()[2:], ["- first", "- s1 turn 2 <!-- s:s1 -->", "- last"])


if __name__ == "__main__":
    unittest.main()
