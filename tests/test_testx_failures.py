# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev test`'s recorded last failures, which run first: one that no longer names a test. Hermetic: tiny git
repositories in temporary folders (test_testx.Env); pytest itself need not be installed, since the failure must not
run. Run: python3 -m unittest tests.test_testx_failures -v"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devkit import testx  # noqa: E402
from test_testx import Env, dev  # noqa: E402

# a production module named like a test outside pytest's testpaths: devkit no longer selects it, but an older run
# recorded its "test" as failed (pytest ran it by name and found no `session` fixture)
PROBE = {
    "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    "app/__init__.py": "",
    "app/test_probe.py": "def test_db(session):\n    return session\n",
    "tests/test_app.py": "from app import test_probe\n\n\ndef test_probe():\n    assert test_probe.test_db(1) == 1\n",
}


class NoLongerATestTests(unittest.TestCase):
    # the recorded failure of a file that is no test here any more is forgotten and named, and runs no more
    def test_a_recorded_failure_of_a_file_that_is_no_test_is_forgotten(self):
        with Env() as e:
            r = e.repo(PROBE, branch=None)
            p = testx.state_path(r)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"failures": {str(r): [
                {"runner": "pytest", "root": ".", "id": "app/test_probe.py::test_db", "file": "app/test_probe.py",
                 "line": 1, "message": "fixture 'session' not found", "ts": 1}]}}))
            rc, out, err = dev(r, "--json")
            self.assertEqual(0, rc, out + err)
            self.assertEqual([], [s for s in json.loads(out)["steps"] if s["kind"] == "failed"])
            self.assertEqual([], (testx.load_state(r).get("failures") or {}).get(str(r)) or [])
            self.assertIn("app/test_probe.py::test_db", err)


if __name__ == "__main__":
    unittest.main()
