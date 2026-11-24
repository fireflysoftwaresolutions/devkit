# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# lib/devkit/visual/noderun.py stops a node driver that outlived its timeout with what it started. The capture driver
# (capture.cjs) starts the design detector detached, in a process group of its own, and the detector's command line
# does not name the run's folder: when the run is stopped from outside (store.py's capture timeout, or Ctrl-C), only
# the driver's parentage leads to it. A stand-in driver here starts a detector the way capture.cjs does (detached,
# stdout piped, the other streams ignored): a wrapper process that runs the detector, which runs its engine as a
# child of its own, the way `dev impeccable detect` does. None of them answers, and each writes its pid to a file
# outside the run's folder. After the timeout, none of the three may be left.
# The tests need node and skip without it. Tests carry comments, not docstrings: `dev test prove` leaves out tests
# with a docstring.
# Run: python3 -m unittest tests.test_visual_noderun
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit.visual import noderun  # noqa: E402

NODE = shutil.which("node")
TIMEOUT = 8  # seconds: time for the wrapper, the detector and its engine to start, even on a loaded Mac

# argv: the pid file. Each process appends its pid, the engine last, and then waits two minutes.
ENGINE = "require('fs').appendFileSync(process.argv[1], String(process.pid) + '\\n'); setTimeout(() => {}, 120000);"
DETECTOR = ("require('fs').appendFileSync(process.argv[1], String(process.pid) + '\\n'); "
            "require('child_process').spawn(process.execPath, ['-e', " + json.dumps(ENGINE) + ", process.argv[1]], "
            "{ stdio: ['ignore', 'inherit', 'ignore'] }).on('exit', () => process.exit(1));")
WRAPPER = ("import os, subprocess, sys; f = open(sys.argv[-1], 'a'); f.write(str(os.getpid()) + '\\n'); f.close(); "
           "sys.exit(subprocess.call(sys.argv[1:]))")
# argv: the wrapper's command as JSON. Starts it as capture.cjs starts the detector, then hangs.
DRIVER = r"""
const [command, ...args] = JSON.parse(process.argv[1]);
require('child_process').spawn(command, args, { stdio: ['ignore', 'pipe', 'ignore'], detached: true });
setInterval(() => {}, 1000);
"""


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
class TheTimeoutStopsTheDriversDetachedChildren(unittest.TestCase):
    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="visual-noderun-test-"))
        self.addCleanup(shutil.rmtree, self.top, True)
        self.pid_file = self.top / "detector.pid"
        self.addCleanup(self._kill)

    def _pids(self):
        try:
            return [int(line) for line in self.pid_file.read_text().split()]
        except (OSError, ValueError):
            return []

    def _kill(self):
        # what the code under test left running is stopped here, by pid, so a failing test leaves nothing
        for pid in self._pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def test_a_hung_detector_started_detached_is_stopped_with_its_wrapper_and_engine(self):
        wrapper = [sys.executable, "-c", WRAPPER, NODE, "-e", DETECTOR, str(self.pid_file)]
        with self.assertRaises(subprocess.TimeoutExpired):
            noderun.run([NODE, "-e", DRIVER, json.dumps(wrapper)], timeout=TIMEOUT, within=self.top)
        pids = self._pids()
        self.assertEqual(len(pids), 3, f"the wrapper, the detector and its engine did not all start: {pids}")
        for pid, who in zip(pids, ("wrapper", "detector", "engine")):
            deadline = time.monotonic() + 5
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_alive(pid), f"the {who} {pid} outlived the stopped run")


if __name__ == "__main__":
    unittest.main()
