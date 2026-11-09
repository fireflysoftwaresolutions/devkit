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


if __name__ == "__main__":
    unittest.main()
