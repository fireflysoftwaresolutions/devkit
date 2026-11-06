# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Git hosts, CI/CD and clouds: selection, Brewfile groups, detection and local checks."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, toolchain, wizard  # noqa: E402


class ToolchainTests(unittest.TestCase):
    def test_selection_and_groups(self):
        cfg = {"hosts": ["gitlab", "azure-devops"], "clouds": ["aws", "kubernetes", "terraform"]}
        self.assertEqual(toolchain.selected(cfg)[0], "github")  # always
        self.assertEqual(toolchain.groups(cfg), ["gitlab", "azure-devops", "aws", "kubernetes", "terraform"])

    def test_every_group_exists_in_the_brewfile(self):
        brewfile = Path(core.DEVKIT_HOME / "Brewfile")
        for key, (_, cli, _, _, group) in toolchain.ALL.items():
            if group:
                with self.subTest(key=key):
                    self.assertTrue(wizard.filtered(brewfile, {group}), f"Brewfile has no # @{group} section")

    def test_detect_from_workspace_and_config_folders(self):
        with tempfile.TemporaryDirectory() as d:
            dev, home = Path(d) / "Development", Path(d) / "home"
            (dev / "gitlab.com" / "g").mkdir(parents=True)
            (dev / "bitbucket.org").mkdir()
            (home / ".aws").mkdir(parents=True)
            (home / ".kube").mkdir()
            (home / ".kube" / "config").write_text("apiVersion: v1")
            saved = (core.DEV, toolchain.HOME, core.have)
            core.DEV, toolchain.HOME = dev, home
            core.have = lambda tool: False
            try:
                found = toolchain.detect()
            finally:
                core.DEV, toolchain.HOME, core.have = saved
            self.assertEqual(found["hosts"], ["github", "gitlab", "bitbucket"])
            self.assertEqual(found["clouds"], ["aws", "kubernetes"])

    def test_token_based_hosts(self):
        old = {k: os.environ.pop(k, None) for k in ("BITBUCKET_TOKEN", "BITBUCKET_USERNAME", "BITBUCKET_APP_PASSWORD")}
        try:
            self.assertFalse(toolchain.check("bitbucket")["signed_in"])
            os.environ["BITBUCKET_USERNAME"], os.environ["BITBUCKET_APP_PASSWORD"] = "u", "p"
            self.assertTrue(toolchain.check("bitbucket")["signed_in"])
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
