# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/noderun.py runs the node drivers of visual diagnosis that start a browser: the marks
# (judge.py), the comparison (compare.py), and the crawl and the walk (map.py's _run_node). Playwright starts
# Chromium detached, in a process group of its own, with its profile under TMPDIR, so stopping node alone leaves the
# browser running. A stand-in node script plays each driver here: it starts a detached child the way Playwright
# starts Chromium (stdio ignored, a --user-data-dir under its TMPDIR on its command line), writes the child's pid to
# a file (and its own pid beside it), and then hangs, fails, or prints what it is given. After a timeout or a failure
# neither the child nor the driver may be left.
# The tests need node and skip without it. Tests carry comments, not docstrings: `dev test prove` leaves out tests
# with a docstring.
# Run: python3 -m unittest tests.test_visual_runner
import json
import os
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit.visual import compare as vcompare  # noqa: E402
from devkit.visual import judge  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
from devkit.visual import noderun  # noqa: E402
from devkit.visual import store  # noqa: E402

NODE = shutil.which("node")
TIMEOUT = 3  # seconds: long enough for node to start the child, short for a test

# argv: the mode (hang, fail, print, where), the file the child's pid goes to, the text `print` writes
STANDIN = r"""
const [mode, pidFile, text] = process.argv.slice(2);
const {spawn} = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
if (mode === 'print') {
  process.stdout.write(text);
  process.exit(0);
}
if (mode === 'where') {
  const tmp = os.tmpdir();
  process.stdout.write(JSON.stringify({tmp, mode: fs.statSync(tmp).mode & 0o777, cwd: process.cwd()}));
  process.exit(0);
}
const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)', '--',
                                       `--user-data-dir=${path.join(os.tmpdir(), 'profile')}`],
                    {detached: true, stdio: 'ignore'});
child.unref();
fs.writeFileSync(pidFile, String(child.pid));
fs.writeFileSync(`${pidFile}.driver`, String(process.pid));
if (mode === 'fail') {
  process.stderr.write('✗ The stand-in failed.\n');
  process.exit(1);
}
setInterval(() => {}, 1000);
"""


def _png(width=4, height=3):
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    chunk = lambda tag, body: (struct.pack(">I", len(body)) + tag + body  # noqa: E731
                               + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _alive(pid: int) -> bool:
    # a process that ended but was not reaped yet (a zombie) has ended
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


@unittest.skipUnless(NODE, "node is not on PATH")
class _StandIn(unittest.TestCase):
    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="visual-runner-test-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.script = self.top / "standin.cjs"
        self.script.write_text(STANDIN)
        self.pid_file = self.top / "child.pid"
        self.driver_file = self.top / "child.pid.driver"
        self.addCleanup(self._kill_child)

    def _kill_child(self):
        # a child or a driver the code under test left running is stopped here, so a failing test leaves nothing
        for path in (self.pid_file, self.driver_file):
            try:
                os.kill(int(path.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def node(self, mode, text=""):
        # a `node` that runs the stand-in in `mode`, whatever the caller passes it
        fake = self.top / f"node-{mode}"
        fake.write_text(f"#!/bin/sh\nexec {shlex.quote(NODE)} {shlex.quote(str(self.script))} {mode} "
                        f"{shlex.quote(str(self.pid_file))} {shlex.quote(text)} \"$@\"\n")
        fake.chmod(0o755)
        return str(fake)

    def assertNoChildLeft(self):
        self.assertTrue(self.pid_file.exists(), "the stand-in did not start its child")
        for what, path in (("the detached child", self.pid_file), ("the driver", self.driver_file)):
            pid = int(path.read_text())
            deadline = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(pid), f"{what} {pid} is still running")


class TheRunner(_StandIn):
    def test_a_run_past_its_timeout_stops_the_child_its_driver_started_detached(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            noderun.run([self.node("hang")], timeout=TIMEOUT)
        self.assertNoChildLeft()

    def test_a_failed_run_stops_the_child_its_driver_started_detached(self):
        p = noderun.run([self.node("fail")], timeout=30)
        self.assertEqual((p.returncode, p.stderr.strip()), (1, "✗ The stand-in failed."))
        self.assertNoChildLeft()

    def test_the_driver_gets_a_private_temporary_folder_deleted_when_it_ends(self):
        p = noderun.run([self.node("where")], timeout=30, within=self.top)
        seen = json.loads(p.stdout)
        tmp = Path(seen["tmp"]).resolve()  # on macOS /var is /private/var; node's cwd is the resolved path
        self.assertEqual(tmp.parent, self.top.resolve())
        self.assertEqual((seen["mode"], Path(seen["cwd"])), (0o700, tmp))
        self.assertFalse(tmp.exists())

    def test_a_folder_the_caller_gives_is_the_drivers_and_stays(self):
        own = self.top / "own"
        own.mkdir(mode=0o700)
        p = noderun.run([self.node("where")], timeout=30, tmpdir=own)
        self.assertEqual(json.loads(p.stdout)["tmp"], str(own))
        self.assertTrue(own.is_dir())

    def test_the_input_reaches_the_driver_and_its_output_comes_back(self):
        p = noderun.run([NODE, "-e", "process.stdin.pipe(process.stdout)"], "the input", timeout=30)
        self.assertEqual((p.returncode, p.stdout), (0, "the input"))

    def test_a_relative_temporary_folder_is_refused_before_the_driver_starts(self):
        # stopped later by that relative name, its browser would be found by any path that holds it
        with self.assertRaises(ValueError):
            noderun.run([self.node("hang")], timeout=TIMEOUT, tmpdir="devkit-node-relative", cwd=self.top)
        self.assertFalse(self.pid_file.exists(), "the driver started")


class StoppingByFolder(_StandIn):
    # noderun.stop finds the browser by the run's folder on its command line: only as a path, and only an absolute one

    def sleeper(self, *args):
        # a process in a session of its own whose command line ends with args; stopped when the test ends
        p = subprocess.Popen([NODE, "-e", "setInterval(() => {}, 1000)", "--", *args], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def test_a_folder_that_is_not_absolute_is_refused_and_nothing_is_stopped(self):
        driver = self.sleeper()
        for folder in ("", ".", "tmp", "devkit-node-x/", "/"):
            with self.subTest(folder=folder):
                # killpg records instead of killing: with the empty folder the old matching named every process here
                with mock.patch.object(noderun.os, "killpg") as killed:
                    with self.assertRaises(ValueError):
                        noderun.stop(driver, folder)
                killed.assert_not_called()

    def test_the_folder_matches_as_a_path_and_not_as_the_start_of_another_name(self):
        folder = str(self.top / "devkit-node-run")
        named = {"inside it": self.sleeper(f"--user-data-dir={folder}/profile"),
                 "itself, the last argument": self.sleeper(f"--user-data-dir={folder}"),
                 "itself, then another argument": self.sleeper(folder, "--port=0")}
        others = {"a longer name": self.sleeper(f"--user-data-dir={folder}-other/profile"),
                  "inside another path": self.sleeper(f"--user-data-dir=/elsewhere{folder}/profile")}
        noderun.stop(self.sleeper(), folder)
        for what, p in named.items():
            with self.subTest(stopped=what):
                deadline = time.monotonic() + 5
                while _alive(p.pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(_alive(p.pid), f"the process naming the folder {what} is still running")
        for what, p in others.items():
            with self.subTest(kept=what):
                self.assertTrue(_alive(p.pid), f"the process naming {what} was stopped")


class TheMarks(_StandIn):
    def test_marks_past_their_timeout_stop_the_browser_and_write_nothing(self):
        pw = {"node": self.node("hang"), "core": str(self.top)}
        with mock.patch.object(vcompare, "_playwright", return_value=pw), \
                mock.patch.object(judge, "MARKS_TIMEOUT", TIMEOUT):
            with self.assertRaises(store.VisualError) as caught:
                judge._draw({"tiles": []}, self.top / "work")
        self.assertEqual(str(caught.exception), f"The marks were not drawn within {TIMEOUT} s. Nothing was written.")
        self.assertNoChildLeft()
        self.assertEqual(list((self.top / "work").iterdir()), [])

    def test_a_driver_that_answers_with_what_devkit_cannot_read_is_a_refusal_not_a_traceback(self):
        job = {"tiles": [{"y": 0, "h": 3, "reuse": False}]}
        for said in ("", "not json", "[]", '{"tiles": "x", "marked": "AA=="}', '{"tiles": [], "marked": "AA=="}',
                     '{"tiles": [null], "marked": 7}', '{"tiles": [null], "marked": "A"}'):
            with self.subTest(said=said):
                pw = {"node": self.node("print", said), "core": str(self.top)}
                with mock.patch.object(vcompare, "_playwright", return_value=pw):
                    with self.assertRaises(store.VisualError) as caught:
                        judge._draw(job, self.top / "work")
                self.assertEqual(str(caught.exception), "The marks driver answered with something devkit cannot "
                                                        "read. Check the browser: dev doctor. Nothing was written.")


class TheComparison(_StandIn):
    def test_a_comparison_past_its_timeout_stops_the_browser(self):
        pw = {"node": self.node("hang"), "core": str(self.top)}
        with mock.patch.object(vcompare, "_playwright", return_value=pw), \
                mock.patch.object(vcompare, "GRACE", 0):
            with self.assertRaises(vcompare.CompareError) as caught:
                vcompare.compare_pictures(_png(), _png(), work=str(self.top), timeout=TIMEOUT)
        self.assertEqual(str(caught.exception), f"The comparison did not finish in {TIMEOUT} s. Nothing was compared.")
        self.assertNoChildLeft()
        self.assertEqual(sorted(p.name for p in self.top.iterdir()),
                         sorted(["standin.cjs", "child.pid", "child.pid.driver", "node-hang"]))

    def test_a_driver_that_answers_with_what_devkit_cannot_read_is_a_refusal_not_a_traceback(self):
        # CompareError is what every caller turns into a VisualError (dev visual compare, verify, the marks)
        for said in ("", "not json", "[]", '{"requests": 5}', '{"errors": "x"}', '{"state": "done"}',
                     '{"state": "done", "result": {"diff": 7}}', '{"state": "done", "result": {"diff": "A"}}',
                     '{"state": "done", "result": {"diff": "AA==", "inside": "most"}}'):
            with self.subTest(said=said):
                pw = {"node": self.node("print", said), "core": str(self.top)}
                with mock.patch.object(vcompare, "_playwright", return_value=pw):
                    with self.assertRaises(vcompare.CompareError) as caught:
                        vcompare.compare_pictures(_png(), _png(), work=str(self.top))
                self.assertEqual(str(caught.exception), "The compare page's driver answered with something devkit "
                                                        "cannot read. Check the browser: dev doctor. Nothing was "
                                                        "compared.")


class TheCrawl(_StandIn):
    def test_a_crawl_or_walk_past_its_timeout_stops_the_browser(self):
        with mock.patch.object(vmap, "_node", return_value=self.node("hang")):
            with self.assertRaises(vmap.MapError) as caught:
                vmap.run_crawler({"url": "http://127.0.0.1:9/"}, timeout=TIMEOUT)
        self.assertEqual(str(caught.exception), f"The browser did not finish within {TIMEOUT} s. Nothing was written.")
        self.assertNoChildLeft()

    def test_a_crawl_that_fails_stops_the_browser_and_says_what_crawl_cjs_said(self):
        with mock.patch.object(vmap, "_node", return_value=self.node("fail")):
            with self.assertRaises(vmap.MapError) as caught:
                vmap.run_crawler({"url": "http://127.0.0.1:9/"}, timeout=30)
        self.assertEqual(str(caught.exception), "The stand-in failed.")
        self.assertNoChildLeft()


if __name__ == "__main__":
    unittest.main()
