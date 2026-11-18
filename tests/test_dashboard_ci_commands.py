# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The dashboard's rows for a failed CI run (the list of what needs you, the CI section, the CI drawer) copy a command
that works from any terminal folder: `dev run view` reads the current folder's repository, so the command first goes
to the project's checkout, as the Now queue's row does (dashnow). The page from the dashboard tests' fixture, in
headless Chromium. Run: python3 -m unittest tests.test_dashboard_ci_commands -v"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

import test_dashboard as T  # noqa: E402  (the browser harness and the fixture)
from devkit import dashboard  # noqa: E402

CI_ROWS = """await go("#/now?all=1&kind=" + encodeURIComponent("CI watch"));
    out.cmds = $$("main .act button").map(b => b.title);"""
WIDGET = "cd -- /Users/t/Development/acme/widget && dev run view 18562211 --failed"


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class CiCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.data = T.fixture_data(Path(cls.tmp.name) / "handoff", T.NOW)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self, steps, hash_, path=None) -> dict:
        d = json.loads(json.dumps(self.data))
        if path:
            next(p for p in d["projects"] if p["id"] == "acme/widget")["path"] = path
        out = T.browse(dashboard.page(d), steps, 1440, hash_, now=T.NOW)
        self.assertIsNone(out.get("probeError") or out.get("pageError"))
        return out

    def commands(self, path=None) -> list:
        return self.probe(CI_ROWS, "#/now", path)["cmds"]

    def test_the_failed_run_row_copies_the_log_command_from_the_projects_checkout(self):
        self.assertEqual(self.commands(), [WIDGET])

    def test_a_checkout_path_with_a_space_is_quoted_for_the_shell(self):
        self.assertEqual(self.commands("/Users/t/My Work/acme/widget"),
                         ["cd -- '/Users/t/My Work/acme/widget' && dev run view 18562211 --failed"])

    def test_the_ci_section_and_the_ci_drawer_copy_it_from_the_projects_checkout_too(self):
        out = self.probe("""await go("#/background");
            out.row = $$(".cirow .ca button").map(b => b.title);
            await go("#/background?modal=ci%3Aacme%2Fwidget%40feat%2Fx");
            out.drawer = $$("#modal .cmdr button").map(b => b.title).filter(c => c.includes("run view"));
            out.foot = $$("#modal .mf button").map(b => [b.title, b.textContent.trim()]);""", "#/background")
        self.assertEqual(out["row"], [WIDGET])
        self.assertEqual(out["drawer"], [WIDGET])
        self.assertEqual(out["foot"], [[WIDGET, "Copy dev run view 18562211 --failed"]])


if __name__ == "__main__":
    unittest.main()
