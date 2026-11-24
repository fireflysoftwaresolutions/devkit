# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev ci --run --resume`'s partial rerun of a test step (testx.partial) past its time limit: every process the step
started stops, as the full run's step runner (gitx._step_shell) does. Hermetic: a tiny unittest project in a
temporary folder (test_testx.Env) whose test starts a child process and hangs.
Run: python3 -m unittest tests.test_testx_partial -v"""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env  # noqa: E402

HANGS = {
    "tests/test_hang.py": """
        import os
        import subprocess
        import sys
        import time
        import unittest


        class HangTests(unittest.TestCase):
            def test_hangs(self):
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
                with open(os.environ["CHILD_PID_FILE"], "w") as fh:
                    fh.write(str(child.pid))
                time.sleep(120)
        """,
}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PartialLimitTests(unittest.TestCase):
    # the runner and the child its test started both stop once the partial rerun passes its limit
    def test_a_partial_rerun_past_its_limit_leaves_none_of_its_processes_running(self):
        with Env() as e:
            r = e.repo(HANGS)
            runner = next(x for x in testx.detect(str(r)) if x["name"] == "unittest")
            pid_file = e.root / "child.pid"
            failed = [{"id": "tests.test_hang.HangTests.test_hangs", "file": "tests/test_hang.py"}]
            t0 = time.monotonic()
            res = testx.partial(str(r), runner, failed=failed, env={"CHILD_PID_FILE": str(pid_file)}, timeout=3)
            self.assertLess(time.monotonic() - t0, 60, res)
            pid = int(pid_file.read_text())
            self.addCleanup(lambda: alive(pid) and os.kill(pid, 9))
            end = time.monotonic() + 10  # an orphan is reaped by launchd or init a moment after it ends
            while alive(pid) and time.monotonic() < end:
                time.sleep(0.05)
            self.assertFalse(alive(pid), "the test's child process outlived the partial rerun's limit")


if __name__ == "__main__":
    unittest.main()
