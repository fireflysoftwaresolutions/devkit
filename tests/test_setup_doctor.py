# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""`dev doctor` rows that check what an installed tool needs on this Mac (lib/devkit/setup.py). The Applications
folder is a temporary one; nothing reads or starts this Mac's browsers.
Run: python3 -m unittest discover -s tests -t ."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import setup  # noqa: E402

CHROME_MCP = {"command": "npx", "args": ["-y", "chrome-devtools-mcp@1.12.0", "--no-usage-statistics"]}


class ChromeForDevToolsMcpTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.apps = Path(tmp.name)
        p = mock.patch.object(setup, "CHROME_APPS_DIR", self.apps)
        p.start()
        self.addCleanup(p.stop)

    def install(self, app):
        exe = self.apps / f"{app}.app" / "Contents" / "MacOS" / app
        exe.parent.mkdir(parents=True)
        exe.write_text("")
        exe.chmod(0o755)

    def row(self, servers):
        return setup.chrome_mcp_check(servers)

    def test_the_server_without_google_chrome_fails_with_the_way_to_install_it(self):
        _, name, ok, detail, fix = self.row({"claude": {"chrome-devtools": CHROME_MCP}, "codex": {}})
        self.assertFalse(ok)
        self.assertEqual(name, "Google Chrome for the Chrome DevTools MCP")
        self.assertIn("Google Chrome.app is not installed", detail)
        self.assertIn("chrome-devtools (Claude)", detail)
        self.assertIn("brew install --cask google-chrome", fix)

    def test_the_server_passes_once_google_chrome_is_installed(self):
        self.install("Google Chrome")
        self.assertTrue(self.row({"claude": {"chrome-devtools": CHROME_MCP}, "codex": {"chrome-devtools": CHROME_MCP}})[2])

    def test_another_channel_needs_its_own_app_and_a_browser_of_its_own_needs_none(self):
        self.install("Google Chrome")
        canary = dict(CHROME_MCP, args=CHROME_MCP["args"] + ["--channel", "canary"])
        _, _, ok, detail, _ = self.row({"codex": {"devtools": canary}})
        self.assertFalse(ok)
        self.assertIn("Google Chrome Canary.app is not installed", detail)
        for extra in (["--executablePath", "/opt/chromium"], ["-e", "/opt/chromium"],
                      ["--browserUrl=http://127.0.0.1:9222"], ["--ws-endpoint", "ws://127.0.0.1:9222/x"]):
            with self.subTest(extra=extra):
                own = dict(CHROME_MCP, args=CHROME_MCP["args"] + ["--channel", "canary"] + extra)
                self.assertIsNone(self.row({"codex": {"devtools": own}}))  # nothing on this Mac to check

    def test_no_row_without_the_server(self):
        self.assertIsNone(self.row({"claude": {"angular-cli": {"command": "npx", "args": ["@angular/cli", "mcp"]}}}))


class DevOnPathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home, self.copy, self.other = root / "home", root / "release" / "bin", root / "opt" / "bin"
        self.link = self.home / ".local" / "bin" / "dev"
        for d in (self.link.parent, self.copy, self.other):
            d.mkdir(parents=True)
        for d in (self.copy, self.other):
            (d / "dev").write_text("#!/bin/sh\n")
            (d / "dev").chmod(0o755)
        p = mock.patch.object(setup, "HOME", self.home)
        p.start()
        self.addCleanup(p.stop)

    def row(self, *path):
        return setup.dev_on_path_check(os.pathsep.join(str(d) for d in path))

    def test_the_link_dev_install_makes_first_on_path_passes(self):
        self.link.symlink_to(self.copy / "dev")
        self.assertTrue(self.row(self.link.parent, self.other)[2])

    def test_a_missing_link_fails_though_another_dev_is_on_path(self):
        _, _, ok, detail, fix = self.row(self.link.parent, self.other)
        self.assertFalse(ok)
        self.assertIn("~/.local/bin/dev is missing", detail.replace(str(self.home), "~"))
        self.assertIn("dev install --only cli", fix)

    def test_another_dev_earlier_on_path_fails(self):
        self.link.symlink_to(self.copy / "dev")
        _, _, ok, detail, _ = self.row(self.other, self.link.parent)
        self.assertFalse(ok)
        self.assertIn(str(self.other / "dev"), detail)


if __name__ == "__main__":
    unittest.main()
