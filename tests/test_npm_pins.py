# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Exact versions in config/npm-globals.txt (`name@version`): the install names them and runs no install script of a
pinned package, every check finds a pinned package by its name, and `dev doctor` reports an installed version that
differs from its pin, and a playwright-core inside @playwright/cli that differs from the CLI's own pin for it. npm is
never run: its calls are recorded, and its global folder is a temporary one."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, crew, toolchain, wizard  # noqa: E402
from tests import test_doctor_profiles as doctor_fixture  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PLAIN = ["npm", "install", "-g", "--no-fund", "--no-audit"]
CORE_PIN = "1.66.0-alpha-1794252756000"  # the playwright-core that @playwright/cli 0.1.24 names exactly


def write_kit(folder: Path, npm_lines: str) -> Path:
    """A devkit home with an empty Brewfile and this npm-globals.txt."""
    (folder / "config").mkdir(parents=True)
    (folder / "Brewfile").write_text("")
    (folder / "config" / "npm-globals.txt").write_text(npm_lines)
    return folder


def write_package(root: Path, name: str, version: str, deps=None) -> Path:
    """A package as npm leaves it in a global folder: <root>/<name>/package.json."""
    where = root / name
    where.mkdir(parents=True)
    (where / "package.json").write_text(json.dumps({"name": name, "version": version, "dependencies": deps or {}}))
    return where


def write_cli(root: Path, version: str, core_version: str, pinned_core: str = CORE_PIN) -> None:
    """@playwright/cli at `version`, naming playwright-core `pinned_core`, with `core_version` installed inside it."""
    cli = write_package(root, "@playwright/cli", version, {"playwright": pinned_core, "playwright-core": pinned_core})
    write_package(cli / "node_modules", "playwright-core", core_version)


class Sandbox(unittest.TestCase):
    """Every test: devkit's configuration and state in a temporary folder, a temporary devkit home and npm folder."""

    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(mock.patch.object(core, "CONFIG_FILE", tmp / "config.json"))
        stack.enter_context(mock.patch.object(core, "STATE", tmp / "state"))
        self.tmp, self.root = tmp, tmp / "npm"
        self.root.mkdir()
        stack.enter_context(mock.patch.object(wizard, "_npm_root", return_value=self.root))
        self.stack = stack

    def kit(self, npm_lines: str) -> Path:
        home = write_kit(self.tmp / "kit", npm_lines)
        self.stack.enter_context(mock.patch.object(wizard, "DEVKIT_HOME", home))
        return home


class InstallTests(Sandbox):
    def test_dev_install_names_the_pinned_versions_without_their_scripts_and_still_installs_the_skill(self):
        self.kit("# @node\ntypescript\n@playwright/cli@0.1.24\naxe-core@4.14.0\n")
        calls, skill = [], []
        with mock.patch("subprocess.call", lambda argv, **kw: calls.append(list(argv)) or 0), \
                mock.patch.object(core, "have", lambda tool: True), \
                mock.patch.object(toolchain, "postinstall", lambda keys: []), \
                mock.patch.object(wizard, "install_playwright_skill", lambda: skill.append(1) or True), \
                mock.patch("sys.stdout", io.StringIO()):
            rc = wizard.install_packages(["node"], with_claude=False)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in calls if c[0] == "npm"],
                         [PLAIN + ["typescript"], PLAIN + ["--ignore-scripts", "@playwright/cli@0.1.24", "axe-core@4.14.0"]])
        self.assertEqual(skill, [1])


class DetectionTests(Sandbox):
    def test_a_pinned_package_counts_as_installed_by_its_name_whatever_its_version(self):
        write_package(self.root, "axe-core", "4.13.0")
        with mock.patch.object(core, "have", lambda tool: tool == "playwright-cli"):
            self.assertEqual(wizard._missing_npm(["@playwright/cli@0.1.24", "axe-core@4.14.0"]), [])
        with mock.patch.object(core, "have", lambda tool: False):  # the install then names the pinned version
            self.assertEqual(wizard._missing_npm(["@playwright/cli@0.1.24", "lodash@4.17.21"]),
                             ["@playwright/cli@0.1.24", "lodash@4.17.21"])


class DoctorRowTests(Sandbox):
    PINS = "# @node\ntypescript\n@playwright/cli@0.1.24\naxe-core@4.14.0\n"

    def test_lists_each_pinned_package_installed_at_its_version(self):
        self.kit(self.PINS)
        write_cli(self.root, "0.1.24", CORE_PIN)
        write_package(self.root, "axe-core", "4.14.0")
        group, name, ok, detail, _ = wizard.npm_pin_check(["node"])
        self.assertEqual((group, ok), ("Toolchain", True))
        self.assertIn("@playwright/cli 0.1.24", detail)
        self.assertIn("axe-core 4.14.0", detail)

    def test_reports_an_installed_version_that_differs_from_its_pin_with_the_command_that_restores_it(self):
        self.kit(self.PINS)
        write_cli(self.root, "0.1.23", "1.66.0-alpha-1793000000000", pinned_core="1.66.0-alpha-1793000000000")
        write_package(self.root, "axe-core", "4.14.0")
        _, _, ok, detail, fix = wizard.npm_pin_check(["node"])
        self.assertFalse(ok)
        self.assertIn("@playwright/cli 0.1.23 installed, pinned 0.1.24", detail)
        self.assertEqual(fix, "npm install -g --ignore-scripts @playwright/cli@0.1.24")

    def test_reports_a_playwright_core_inside_the_cli_that_differs_from_the_clis_own_pin(self):
        self.kit(self.PINS)
        write_cli(self.root, "0.1.24", "1.65.0")
        write_package(self.root, "axe-core", "4.14.0")
        _, _, ok, detail, fix = wizard.npm_pin_check(["node"])
        self.assertFalse(ok)
        self.assertIn(f"playwright-core 1.65.0 inside @playwright/cli, which pins {CORE_PIN}", detail)
        self.assertEqual(fix, "npm install -g --ignore-scripts @playwright/cli@0.1.24")

    def test_a_pinned_package_that_is_not_installed_fails_the_row(self):
        self.kit(self.PINS)
        write_cli(self.root, "0.1.24", CORE_PIN)
        _, _, ok, detail, fix = wizard.npm_pin_check(["node"])
        self.assertFalse(ok)
        self.assertIn("axe-core not installed (pinned 4.14.0)", detail)
        self.assertEqual(fix, "npm install -g --ignore-scripts axe-core@4.14.0")

    def test_pins_of_a_stack_not_chosen_are_not_checked(self):
        self.kit(self.PINS)
        _, _, ok, detail, _ = wizard.npm_pin_check([])
        self.assertTrue(ok)
        self.assertNotIn("axe-core", detail)

    def test_dev_doctor_shows_the_row_and_its_fix(self):
        self.kit("axe-core@4.14.0\n")  # the core group, which every Mac checks
        write_package(self.root, "axe-core", "4.13.0")
        _, output = doctor_fixture.DoctorProfilesTests.doctor_output(self, {"claude": True, "codex": False})
        self.assertIn("axe-core 4.13.0 installed, pinned 4.14.0", output)
        self.assertIn("npm install -g --ignore-scripts axe-core@4.14.0", output)


class ShippedPinsTests(unittest.TestCase):
    def test_the_npm_list_pins_the_playwright_cli_and_axe_core_the_visual_checks_were_measured_on(self):
        node = wizard.filtered(REPO / "config" / "npm-globals.txt", {"node"})
        self.assertIn("@playwright/cli@0.1.24", node)
        self.assertIn("axe-core@4.14.0", node)


class CrewSkillTests(Sandbox):
    def test_a_pinned_playwright_cli_still_provides_its_skill_to_crew_briefs(self):
        home = write_kit(self.tmp / "kit", "# @node\n@playwright/cli@0.1.24\n")
        names, _, _ = crew.known_skills(home)
        self.assertIn("playwright-cli", names)


if __name__ == "__main__":
    unittest.main()
