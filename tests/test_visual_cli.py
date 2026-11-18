# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# `dev visual` (lib/devkit/visual/commands.py, wired into lib/devkit/cli.py): every command of the spec's command line
# parses, and the handlers drive the store, the map, and the report. Each test reads a command line with dev's own
# parser (cli.parse) and runs its handler, in a temporary handoff repository, state folder, and configuration
# (core.HANDOFF, core.STATE, core.CONFIG_FILE, and work's item folder); captures are real, of a page served on
# 127.0.0.1 (tests/test_visual_store.py has the page). Without node, gitleaks, or the pinned Playwright and its
# Chromium, the tests that capture skip and say which is missing.
# Tests carry comments, not docstrings: `dev test prove` leaves out tests with a docstring.
# Run: python3 -m unittest tests.test_visual_cli
import contextlib
import io
import json
import os
import re
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import cli, work  # noqa: E402
from devkit.visual import commands, store  # noqa: E402
from devkit.visual import map as vmap  # noqa: E402
import test_visual_store as base  # noqa: E402

P = base.P
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class CliCase(base.StoreCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(commands, "_project", lambda a: getattr(a, "project", None) or P),
                  mock.patch.object(work, "HANDOFF", self.tmp / "handoff"),
                  mock.patch.object(work, "ITEMS", self.tmp / "handoff" / "items")):
            p.start()
            self.addCleanup(p.stop)

    def dev(self, *argv):
        # -> (exit code, stdout, stderr) of `dev visual <argv>` as dev parses it
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            a = cli.parse(["visual", *argv])
            rc = a.fn(a)
        return rc, ANSI.sub("", out.getvalue()), ANSI.sub("", err.getvalue())

    def ids(self, kind, text):
        return re.findall(rf"\b{kind}-[0-9a-z]{{7,40}}\b", text)


class ParserTest(unittest.TestCase):
    def test_dev_visual_is_one_command_built_alone_with_every_command_of_the_spec(self):
        alone = cli.build_parser(only="visual")
        self.assertIsNotNone(alone)
        visual = next(a for a in alone._actions if a.nargs == "A...").choices["visual"]
        names = next(a for a in visual._actions if a.nargs == "A...").choices
        self.assertEqual(sorted(names), sorted(["start", "screen", "component", "journey", "section", "product",
                                                "map", "capture", "check", "signal", "pass", "describe", "marks",
                                                "judge", "finding", "verify", "compare", "report", "share", "ls"]))

    def test_the_project_option_reads_the_same_before_and_after_the_command(self):
        for argv in (["visual", "-p", "acme/billing", "ls"], ["visual", "ls", "-p", "acme/billing"],
                     ["visual", "map", "node", "add", "home", "--title", "Home", "-p", "acme/billing"]):
            with self.subTest(argv=argv):
                self.assertEqual(cli.parse(argv).project, "acme/billing")
        self.assertIsNone(cli.parse(["visual", "ls"]).project)

    def test_the_spec_examples_parse_into_the_values_the_store_takes(self):
        a = cli.parse(["visual", "finding", "add", "VC-g33z1a8k0e", "--element", "E57", "--pass", "layout", "--rule",
                       "wcag:2.5.8", "ds:space-4", "--severity", "P1", "--title", "t", "--guide", "dialog-element"])
        self.assertEqual((a.capture, a.element, a.pass_name, a.rule, a.severity, a.guide),
                         ("VC-g33z1a8k0e", "E57", "layout", ["wcag:2.5.8", "ds:space-4"], "P1", "dialog-element"))
        a = cli.parse(["visual", "start", "--scope", "product", "Q?", "--viewport", "1440x900", "390x844", "--theme",
                       "light", "dark"])
        self.assertEqual((a.scope, a.question, a.viewport, a.theme), ("product", "Q?", ["1440x900", "390x844"],
                                                                      ["light", "dark"]))
        a = cli.parse(["visual", "capture", "http://127.0.0.1:4200/x", "--do", "click #a", "--do", "wait #b",
                       "--viewport", "390x844", "--no-check"])
        self.assertEqual((a.do, a.no_check, a.source), (["click #a", "wait #b"], True, "http://127.0.0.1:4200/x"))
        a = cli.parse(["visual", "journey", "walk", "--path", "home > invoices.list", "--do", "click #invoices",
                       "--base", "http://127.0.0.1:4200/"])
        self.assertEqual((a.journey, a.path, a.do, a.base), (None, "home > invoices.list", ["click #invoices"],
                                                             "http://127.0.0.1:4200/"))
        self.assertIsNone(cli.parse(["visual", "journey", "walk", "J:export-month"]).base)


@unittest.skipUnless(base.BROWSERS, base.NO_BROWSER or "")
class ScreenAuditTest(CliCase):
    def test_dev_visual_screen_opens_a_screen_audit_and_captures_with_no_map(self):
        # VD-AC-41: one screen audit, one capture, no map file
        rc, out, err = self.dev("screen", self.url, "--viewport", "390x844")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^✓ VA-[0-9a-z]+  screen · /invoices\.html \(no map: the captures attach when one "
                                   r"exists\)$")
        self.assertRegex(lines[1], r"^✓ VC-[0-9a-z]+  invoices\.html · 390×844 light · \d+ signals?$")
        self.assertIn("next: work through screen passes 1–10, then dev visual signal ls VA-", out)
        audit, cap = self.ids("VA", lines[0])[0], self.ids("VC", lines[1])[0]
        self.assertEqual(self.record(audit)["scope"], "screen")
        self.assertEqual(self.record(cap)["audit"], audit)
        self.assertFalse((self.records / "map").exists())
        rc, out, _ = self.dev("ls")
        self.assertIn(audit, out)
        self.assertIn("1 capture", out)
        self.assertIn("0 of 10 passes recorded", out)

    def test_capture_without_an_open_audit_refuses_and_says_how_to_start(self):
        rc, out, err = self.dev("capture", self.url)
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("No audit is open in this session. For one screen: dev visual screen <url>.", err)
        self.assertFalse((self.records / "captures").exists())

    def test_passes_findings_and_signals_go_through_the_commands(self):
        rc, out, err = self.dev("screen", self.url, "--viewport", "390x844")
        self.assertEqual(rc, 0, err)
        cap = self.ids("VC", out)[0]
        c = self.record(cap)
        export = self.element(c, '[data-testid="export"]')
        rc, out, err = self.dev("finding", "add", cap, "--element", export["id"], "--pass", "layout", "--rule",
                                "wcag:2.5.8", "ds:space-4", "--severity", "P1", "--title", "Export touches Amount",
                                "--impact", "A clerk taps Amount", "--evidence", "40×30 px overlap",
                                "--fix", "Space them", "--guide", "dialog-element")
        self.assertEqual(rc, 0, err)
        fid = self.ids("VF", out)[0]
        f = self.record(fid)
        self.assertEqual((f["anchor"]["element"], f["rule"], f["guide"]), (export["id"], ["wcag:2.5.8", "ds:space-4"],
                                                                            "dialog-element"))
        rc, out, err = self.dev("pass", cap, "hierarchy", "--clean", "the title reads first")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.record(cap)["passes"]["hierarchy"], {"result": "clean", "note": "the title reads first"})
        rc, out, _ = self.dev("signal", "ls", "--untriaged")
        refs = re.findall(rf"{cap}#\d+", out)
        self.assertTrue(refs)
        rc, out, err = self.dev("signal", "dismiss", refs[0], "--why", "the design's overlap", "--model", "human")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.dev("signal", "ls", "--untriaged")
        self.assertNotIn(refs[0] + " ", out)
        rc, out, err = self.dev("finding", "set", fid, "--status", "verified", "--why", "looks right")
        self.assertEqual(rc, 1)
        self.assertIn("Only dev visual verify marks a finding verified", err)
        rc, out, _ = self.dev("finding", "ls", "--severity", "P1")
        self.assertIn(fid, out)
        rc, out, _ = self.dev("finding", "show", fid)
        self.assertIn("Guide:", out)
        self.assertIn("dialog-element", out)

    def test_a_finding_becomes_a_work_item_that_names_it_and_that_it_names(self):
        # VD-AC-54, R26
        rc, out, err = self.dev("screen", self.url, "--viewport", "390x844", "--no-check")
        cap = self.ids("VC", out)[0]
        rc, out, err = self.dev("finding", "add", cap, "--region", "0,0,100,40", "--pass", "layout", "--rule",
                                "nielsen:8", "--severity", "P1", "--title", "Cramped bar", "--impact", "i",
                                "--evidence", "e", "--fix", "x")
        fid = self.ids("VF", out)[0]
        rc, out, err = self.dev("finding", "task", fid)
        self.assertEqual(rc, 0, err)
        iid = re.findall(r"\bB-[0-9a-z]+\b", out)[0]
        self.assertEqual(self.record(fid)["task"], iid)
        item = next(it for it in work.all_items(P) if it["meta"]["id"] == iid)
        self.assertIn(f"visual:{fid}", item["meta"]["links"])
        self.assertEqual(item["meta"]["priority"], "high")
        rc, _out, err = self.dev("finding", "task", fid)
        self.assertEqual(rc, 1)
        self.assertIn(iid, err)

    def test_the_report_is_written_into_the_store_and_its_summary_printed(self):
        # VD-AC-27 through the command: the question in the Markdown; the HTML file 0600 in 0700 folders
        rc, out, err = self.dev("screen", self.url, "--viewport", "390x844", "--no-check")
        audit = self.ids("VA", out)[0]
        rc, out, err = self.dev("report", "--md")
        self.assertEqual(rc, 0, err)
        self.assertIn("invoices.html", out)
        rc, out, err = self.dev("report", audit)
        self.assertEqual(rc, 0, err)
        page = self.local / "reports" / audit / "index.html"
        self.assertEqual(out.strip(), f"✓ {page}")
        self.assertEqual(stat.S_IMODE(page.stat().st_mode), 0o600)
        for folder in (self.local / "reports", page.parent):
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        rc, out, err = self.dev("report", "VC-g33nothere1")
        self.assertEqual(rc, 1)
        self.assertIn("audit (VA-…)", err)


@unittest.skipUnless(base.BROWSERS, base.NO_BROWSER or "")
class MapCommandsTest(CliCase):
    def test_map_declare_check_and_draw_go_through_the_map_and_the_store(self):
        for argv in (["map", "node", "add", "home", "--title", "Home", "--route", "/", "--why", "start"],
                     ["map", "node", "add", "invoices.list", "--title", "Invoices", "--route", "/invoices.html"],
                     ["map", "node", "add", "settings.legacy", "--title", "Legacy settings", "--route", "/legacy"],
                     ["map", "edge", "add", "home", "invoices.list", "--trigger", 'link "Invoices"'],
                     ["map", "state", "add", "invoices.list", "list"]):
            rc, out, err = self.dev(*argv)
            self.assertEqual(rc, 0, (argv, err))
        self.assertEqual(vmap.load(P)["version"], 5)
        self.assertEqual(vmap.load(P)["changelog"][0]["by"], "ancongui")
        rc, out, err = self.dev("start", "--scope", "product", "Can a clerk find the invoices?", "--viewport",
                                "390x844")
        audit = self.ids("VA", out)[0]
        self.assertEqual(self.record(audit)["map_version"], 5)
        rc, out, err = self.dev("capture", self.url, "--state", "list", "--no-check")
        self.assertEqual(rc, 0, err)
        cap = self.record(self.ids("VC", out)[0])
        self.assertEqual(cap["map"]["node"], "invoices.list")
        rc, out, err = self.dev("map", "check")
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, rf"{audit}#\d+  orphan +settings\.legacy")
        stored = self.record(audit)["signals"]
        self.assertIn("orphan", [s["check"] for s in stored])
        rc, again, err = self.dev("map", "check")
        self.assertEqual([s["n"] for s in self.record(audit)["signals"]], [s["n"] for s in stored])
        rc, out, err = self.dev("map", "draw")
        self.assertEqual(rc, 0, err)
        light = self.local / "map" / "diagrams" / "map-light.svg"
        self.assertIn(f"✓ map  {light}", out)
        self.assertEqual(stat.S_IMODE(light.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(light.parent.stat().st_mode), 0o700)
        self.assertTrue((self.records / "map" / "diagrams" / "map.json").is_file())

    @unittest.skipUnless(commands._owner("walk", "save"), "walk.py keeps the journey walks")
    def test_map_check_reads_the_journey_walks_kept_under_the_audit(self):
        # a walked journey over its budget is a journey-steps signal; the same journey not walked gives none
        for argv in (["map", "node", "add", "home", "--title", "Home", "--route", "/"],
                     ["map", "node", "add", "invoices.list", "--title", "Invoices", "--route", "/invoices.html"],
                     ["map", "edge", "add", "home", "invoices.list", "--trigger", 'link "Invoices"'],
                     ["map", "journey", "add", "J:find", "--actor", "clerk", "--task", "find the invoices", "--path",
                      "home > invoices.list", "--budget", "1"]):
            rc, _out, err = self.dev(*argv)
            self.assertEqual(rc, 0, (argv, err))
        rc, out, _ = self.dev("start", "--scope", "product", "Can a clerk find the invoices?")
        audit = self.ids("VA", out)[0]
        rc, out, _ = self.dev("map", "check")
        self.assertNotIn("journey-steps", out)
        commands._owner("walk", "save")(P, audit, {"journey": "J:find", "map_version": 4, "broken": None, "steps": [
            {"step": 1, "node": "home", "edge": None, "status": 200, "redirects": 0, "keeps": {}},
            {"step": 2, "node": "invoices.list", "edge": "home>invoices.list", "status": 200, "redirects": 0,
             "keeps": {}}]})
        rc, out, err = self.dev("map", "check")
        self.assertEqual(rc, 0, err)
        self.assertRegex(out, rf"{audit}#\d+  journey-steps +J:find takes 2 steps; its budget is 1")

    def test_map_refusals_print_why_and_exit_1(self):
        rc, out, err = self.dev("map", "edge", "add", "home", "nowhere", "--trigger", "x")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("✗ "), err)
        self.assertFalse((self.records / "map").exists())


class OwnersTest(CliCase):
    def test_a_command_whose_module_is_not_here_refuses_and_writes_nothing(self):
        with mock.patch.object(commands, "_owner", return_value=None):
            for argv, says in ((["verify", "VF-g33nothere1"], "the verification (compare.py)"),
                               (["compare", "VC-a1b2c3d4", "VC-a1b2c3d5"], "the comparison (compare.py)"),
                               (["describe", "VC-a1b2c3d4"], "the description's text view"),
                               (["marks", "VC-a1b2c3d4"], "the tiles and the set-of-marks copy"),
                               (["judge", "VC-a1b2c3d4", "--pass", "task", "--note", "n"], "the judgment layer"),
                               (["check", "VC-a1b2c3d4"], "the checks again on a recipe"),
                               (["journey", "walk", "J:export-month"], "the journey walk")):
                with self.subTest(argv=argv):
                    rc, out, err = self.dev(*argv)
                    self.assertEqual((rc, out), (1, ""))
                    self.assertIn(f"runs {says}, which this devkit does not have yet. Nothing was done.", err)
        self.assertFalse(self.records.exists())

    def test_a_command_whose_module_is_here_runs_its_entry_with_the_project(self):
        seen = []

        def entry(a):
            seen.append((a.visual_cmd, a.project_id, a.record))
            return 3
        with mock.patch.object(commands, "_owner", return_value=entry):
            rc, _out, _err = self.dev("verify", "VF-g33z2c4h1m", "--accept", "--why", "the reviewer saw it")
        self.assertEqual((rc, seen), (3, [("verify", P, "VF-g33z2c4h1m")]))

    def test_an_owner_module_is_found_only_when_its_file_is_here(self):
        self.assertIsNone(commands._owner("no_such_module", "cmd"))
        self.assertIs(commands._owner("map", "check"), vmap.check)

    def test_the_crawl_commands_call_the_maps_crawl_when_it_is_there(self):
        calls = []

        def crawl(project, url, **kw):
            calls.append((project, url, kw["within"], kw["depth"], kw["max_pages"], kw["by"]))
            return {"map": {"version": 3, "nodes": [1, 2], "edges": [1]},
                    "summary": {"lines": ["stopped at depth 3 · 0 links left"], "nodes": 2}}
        with mock.patch.object(vmap, "crawl", crawl, create=True):
            rc, out, err = self.dev("map", "crawl", "http://127.0.0.1:4200/", "--max", "5")
        self.assertEqual(rc, 0, err)
        self.assertEqual(calls, [(P, "http://127.0.0.1:4200/", None, 3, 5, "ancongui")])
        self.assertIn("✓ map v3 · 2 screens · 1 edge", out)
        self.assertIn("  stopped at depth 3 · 0 links left", out)
        self.assertNotIn("nodes", out)
        if not hasattr(vmap, "crawl"):
            rc, out, err = self.dev("map", "crawl", "http://127.0.0.1:4200/")
            self.assertEqual(rc, 1)
            self.assertIn("the crawler", err)


if __name__ == "__main__":
    unittest.main()
