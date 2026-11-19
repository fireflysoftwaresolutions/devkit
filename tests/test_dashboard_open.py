# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev dashboard` opens the page in the developer's browser, but never from a devkit that runs in a test sandbox (a
temporary HOME, core.sandbox_path): a test's devkit child would open a browser on the developer's screen. `open` is
a stand-in. Run: python3 -m unittest tests.test_dashboard_open -v"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, dashboard  # noqa: E402


class OpenInBrowserTests(unittest.TestCase):
    def test_the_page_opens_in_the_browser_only_outside_a_test_sandbox(self):
        page = Path("/var/tmp/devkit-test/dashboard.html")
        with mock.patch.object(dashboard.subprocess, "run") as run:
            with mock.patch.object(core, "sandbox_path", return_value=["/var/tmp/devkit-test/bin"]):
                dashboard.open_in_browser(page)
            self.assertEqual(run.call_args_list, [], "a test sandbox's devkit opened the developer's browser")
            with mock.patch.object(core, "sandbox_path", return_value=[]):
                dashboard.open_in_browser(page)
        self.assertEqual([c.args[0] for c in run.call_args_list], [["open", str(page)]])


if __name__ == "__main__":
    unittest.main()
