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


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class QuotedNeedsCommandTests(unittest.TestCase):
    """The commands the page builds from a failed CI run's id and a failed job's name (the command palette, the list of
    what needs you, the job drawer) quote them for the shell, as the Now queue's server does: a value with a space or a
    `;` stays one argument and never runs a second command."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = T.fixture_data(Path(cls.tmp.name) / "handoff", T.NOW)
        d["ci"]["watches"][0]["failures"][0]["id"] = "14;touch pwn"
        next(j for j in d["jobs"] if j["name"] == "doctor")["name"] = "night doctor"
        cls.page = dashboard.page(d)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_palette_and_the_job_drawer_copy_quoted_commands(self):
        out = T.browse(self.page, """document.dispatchEvent(new KeyboardEvent("keydown", {key: "k", metaKey: true, bubbles: true})); await tick(100);
            const inp = $("#palette input"); inp.value = "Copy: dev"; inp.dispatchEvent(new Event("input", {bubbles: true})); await tick(100);
            out.palette = $$("#pal-list li").map(text).filter(t => t.includes("run view") || t.includes("jobs run"));
            await go("#/background?modal=job%3Anight%20doctor"); out.job = $$("#modal button[title]").map(b => b.title).filter(t => t.startsWith("dev jobs"));""",
                       1440, "#/now", now=T.NOW)
        self.assertIsNone(out.get("probeError") or out.get("pageError"))
        self.assertIn("Copy: cd -- /Users/t/Development/acme/widget && dev run view '14;touch pwn' --failed", " ".join(out["palette"]))
        self.assertIn("Copy: dev jobs run 'night doctor'", " ".join(out["palette"]))
        self.assertTrue(out["job"], out)
        self.assertEqual([c for c in out["job"] if "night doctor" in c and "'night doctor'" not in c], [])


if __name__ == "__main__":
    unittest.main()
