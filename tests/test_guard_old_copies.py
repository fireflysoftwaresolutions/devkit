# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The guard and the other devkit copies in the releases folder: an agent runs the release `dev install` pinned, never
# an older copy kept for rollback, whose repair writes its own older hooks, guard and instructions back.
# Run: python3 -m unittest tests.test_guard_old_copies -v
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

OLD, NEW = "26.11.4-89079aa", "26.11.6-bbbbbbb"


class OtherReleaseTests(unittest.TestCase):
    """A temporary HOME whose releases folder holds an older copy and the pinned one, which install.json names."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name).resolve() / "home"
        self.root = self.home / ".local" / "share" / "devkit" / "releases"
        for name in (OLD, NEW):
            (self.root / name / "bin").mkdir(parents=True)
            (self.root / name / "bin" / "dev").write_text("#!/usr/bin/env python3\n")
            (self.root / name / "lib" / "devkit").mkdir(parents=True)
            (self.root / name / core.PIN_MANIFEST).write_text(json.dumps({"version": name.split("-")[0], "files": {}}))
        man = (self.root / NEW / core.PIN_MANIFEST).read_bytes()
        (self.root.parent / "install.json").write_text(json.dumps({
            "name": NEW, "version": "26.11.6", "path": str(self.root / NEW),
            "manifest_sha256": hashlib.sha256(man).hexdigest()}))
        self.cwd = self.home / "Development" / "acme" / "widget"
        self.cwd.mkdir(parents=True)
        for p in (mock.patch.object(core, "HOME", self.home), mock.patch.object(guard, "HOME", self.home),
                  mock.patch.object(core, "DEVKIT_HOME", ROOT),
                  mock.patch.dict(os.environ, {"HOME": str(self.home)})):
            p.start()
            self.addCleanup(p.stop)

    def verdict(self, cmd, cwd=None):
        return guard.check("Bash", {"command": cmd}, str(cwd or self.cwd), "claude")

    def assertRefused(self, cmd, cwd=None):
        v = self.verdict(cmd, cwd)
        self.assertIsNotNone(v, cmd)
        self.assertEqual(v[0], "deny", cmd)
        return v[1]

    def test_an_older_copy_s_dev_is_refused_however_the_path_is_written(self):
        old = self.root / OLD
        for cmd in (f"python3 -I -S {old}/bin/dev config repair",
                    f"{old}/bin/dev config repair --quiet",
                    f"~/.local/share/devkit/releases/{OLD}/bin/dev config repair",
                    f"$HOME/.local/share/devkit/releases/{OLD}/bin/dev setup claude",
                    f'"${{HOME}}/.local/share/devkit/releases/{OLD}/bin/dev" install --only claude',
                    f"{old}/bin/dev sync --quiet",
                    f"~/.local/share/devkit/rel'eas'es/{OLD}/bin/dev config lock",
                    f"cd ~/.local/share/devkit && releases/{OLD}/bin/dev config repair",
                    f"cd {old} && ./bin/dev config repair"):
            with self.subTest(cmd=cmd):
                reason = self.assertRefused(cmd)
                self.assertIn(OLD, reason)
                self.assertIn("26.11.6", reason)  # the pinned release, which the developer's `dev` runs

    def test_an_older_copy_s_code_is_refused_when_loaded_by_hand(self):
        lib = self.root / OLD / "lib"
        for cmd in (f"python3 -c \"import sys; sys.path.insert(0, '{lib}'); from devkit import policy; policy.repair()\"",
                    f"cd {lib} && python3 -m devkit.cli config repair",
                    f"R=~/.local/share/devkit/releases/{OLD}; python3 \"$R/bin/dev\" config repair",
                    f"python3 - <<'EOF'\nimport sys\nsys.path.insert(0, '{lib}')\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertRefused(cmd)

    def test_a_copy_chosen_by_a_pattern_or_the_folder_itself_is_refused(self):
        for cmd in ("~/.local/share/devkit/releases/*/bin/dev config repair",
                    "~/.local/share/devkit/releases/26.11.4*/bin/dev config repair",
                    f"~/.local/share/devkit/releases/../releases/{OLD}/bin/dev config repair",
                    "find ~/.local/share/devkit/releases -name dev -exec {} config repair \\;",
                    "ls ~/.local/share/devkit/releases"):
            with self.subTest(cmd=cmd):
                self.assertRefused(cmd)

    def test_the_pinned_release_and_the_installed_dev_stay_usable(self):
        for cmd in ("dev config repair",
                    f"{self.root / NEW}/bin/dev config repair",
                    f"cat ~/.local/share/devkit/releases/{NEW}/docs/guard.md",
                    "cat ~/.local/share/devkit/install.json"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.verdict(cmd))

    def test_a_releases_folder_of_another_home_is_not_this_install(self):
        other = Path(tempfile.gettempdir()).resolve() / "sandbox-home" / ".local" / "share" / "devkit" / "releases"
        self.assertIsNone(self.verdict(f"ls {other}/{OLD}/bin"))


if __name__ == "__main__":
    unittest.main()
