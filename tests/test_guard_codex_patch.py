# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# An agent settings file may not switch devkit off (guard.devkit_off), whichever tool writes it: Codex's apply_patch
# is judged by the file's text after the patch, as Write is by its content. Each refused patch has a twin that
# writes the same file without switching devkit off and stays allowed.
# Run: python3 -m unittest tests.test_guard_codex_patch -v
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import guard  # noqa: E402


def patch(*lines):
    return "*** Begin Patch\n" + "\n".join(lines) + "\n*** End Patch\n"


class ApplyPatchOnAgentSettings(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = guard.real(tmp.name)
        self.codex = Path(self.cwd) / ".codex"
        self.codex.mkdir()
        (self.codex / "config.toml").write_text('[features]\nmodel = "a"\n')
        (self.codex / "staged.toml").write_text('[features]\nhooks = false\nmodel = "a"\n')

    def verdict(self, body):
        v = guard.check("apply_patch", {"command": body}, self.cwd, "codex")
        return v[0] if v else "allow"

    def test_an_add_that_turns_hooks_off_is_denied(self):
        cfg = Path(self.cwd) / "app" / ".codex" / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Add File: {cfg}", "+[features]", "+hooks = false")), "deny")

    def test_an_update_that_adds_hooks_false_is_denied(self):
        cfg = self.codex / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", ' model = "a"', "+hooks = false")), "deny")
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", ' model = "a"', '+effort = "high"')),
                         "allow")

    def test_an_update_whose_hunk_does_not_apply_is_judged_by_the_lines_it_adds(self):
        cfg = self.codex / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", " no such line", "+codex_hooks = false")),
                         "deny")

    def test_a_move_onto_the_config_brings_the_sources_text_and_is_denied(self):
        src, cfg = self.codex / "staged.toml", self.codex / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Update File: {src}", f"*** Move to: {cfg}", "@@",
                                            '-model = "a"', '+model = "b"')), "deny")
        # the same move of a source that keeps the hooks on
        src.write_text('[features]\nmodel = "a"\n')
        self.assertEqual(self.verdict(patch(f"*** Update File: {src}", f"*** Move to: {cfg}", "@@",
                                            '-model = "a"', '+model = "b"')), "allow")

    def test_a_path_in_another_case_names_the_same_file_on_macos_and_is_denied(self):
        for name in (".Codex/config.toml", ".codex/Config.TOML"):
            with self.subTest(name=name):
                p = Path(self.cwd) / name
                self.assertEqual(self.verdict(patch(f"*** Add File: {p}", "+[features]", "+hooks = false")), "deny")
                v = guard.check("Write", {"file_path": str(p), "content": "[features]\nhooks = false\n"}, self.cwd,
                                "codex")
                self.assertEqual((v or ("allow",))[0], "deny")

    def test_every_toml_spelling_of_hooks_off_is_denied_and_the_twins_are_not(self):
        cfg = Path(self.cwd) / "app" / ".codex" / "config.toml"
        off = ("features.hooks = false", "features.codex_hooks = false", '[features]\n"hooks" = false',
               "features = { hooks = false }", "features = { a = 1, codex_hooks = false }",
               '[features]\n"hoo\\u006bs" = false', "[profiles.fast.features]\nhooks=false # off")
        on = ("[features]\nhooks = true", "[features]\nmy_hooks = false", 'features = { hooks = true }',
              '[features]\nnote = "hooks = false"')
        for text, want in [(t, "deny") for t in off] + [(t, "allow") for t in on]:
            with self.subTest(text=text):
                v = guard.check("Write", {"file_path": str(cfg), "content": text + "\n"}, self.cwd, "codex")
                self.assertEqual((v or ("allow",))[0], want)
                body = patch(f"*** Add File: {cfg}", *("+" + ln for ln in text.split("\n")))
                self.assertEqual(self.verdict(body), want)

    def test_a_patch_that_keeps_devkit_on_is_allowed(self):
        cfg = self.codex / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", ' model = "a"', "+hooks = true")),
                         "allow")
        self.assertEqual(self.verdict(patch(f"*** Add File: {self.codex / 'notes.toml'}", "+hooks = false")), "allow")


if __name__ == "__main__":
    unittest.main()
