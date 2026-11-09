# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# An agent settings file may not switch devkit off (guard.devkit_off), whichever tool writes it: Codex's apply_patch
# is judged by the file's text after the patch, as Write is by its content, and a shell write by the text the command
# shows it writes (a write whose text it does not show is refused). Each refused write has a twin that writes the same
# file without switching devkit off, or the same text to another file, and stays allowed.
# Run: python3 -m unittest tests.test_guard_codex_patch -v
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        # the twin: the same text in a file Codex does not read as its settings
        self.assertEqual(self.verdict(patch(f"*** Add File: {self.codex / 'notes.toml'}", "+hooks = false")), "allow")

    def test_an_update_that_adds_hooks_false_is_denied(self):
        cfg = self.codex / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", ' model = "a"', "+hooks = false")), "deny")
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", ' model = "a"', '+effort = "high"')),
                         "allow")
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", ' model = "a"', "+hooks = true")),
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


class ShellWritesOnAgentSettings(unittest.TestCase):
    """A shell write to an agent settings file is judged by the text it writes, as Write is; a write whose text the
    command does not show (a copy, sed -i, a variable) is refused. Each refused form has an allowed twin: the same form
    with text that keeps devkit on, or the same text written to another file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = guard.real(tmp.name)
        root = Path(self.cwd)
        for d in (".codex", ".claude", "staged"):
            (root / d).mkdir()
        (root / ".codex" / "config.toml").write_text('[features]\nmodel = "a"\n')
        (root / ".claude" / "settings.json").write_text("{}\n")
        (root / "staged" / "config.toml").write_text("[features]\nhooks = false\n")
        (root / "false").write_text("")  # a file a glob can match

    def verdicts(self, cmd):
        return {(guard.check("Bash", {"command": cmd}, self.cwd, agent) or ("allow",))[0]
                for agent in ("claude", "codex")}

    def pairs(self, cases):
        for refused, allowed in cases:
            with self.subTest(refused=refused):
                self.assertEqual(self.verdicts(allowed), {"allow"}, allowed)
                self.assertEqual(self.verdicts(refused), {"deny"}, refused)

    def test_a_redirect_is_judged_by_the_text_it_writes(self):
        self.pairs([
            ("printf '[features]\\nhooks = false\\n' > .codex/config.toml",
             "printf '[features]\\nhooks = true\\n' > .codex/config.toml"),
            ("printf 'codex_hooks = false\\n' >> .codex/config.toml", "printf 'effort = 1\\n' >> .codex/config.toml"),
            ("echo '{\"disableAllHooks\": true}' > .claude/settings.local.json",
             "echo '{\"model\": \"opus\"}' > .claude/settings.local.json"),
            ("echo '{\"enabledPlugins\": {\"devkit@firefly-devkit\": false}}' > .claude/settings.json",
             "echo '{\"disableAllHooks\": true}' > .claude/notes.json"),
        ])

    def test_escapes_the_shell_decodes_count_as_the_text_they_write(self):
        self.pairs([
            ("printf 'hooks = \\x66alse\\n' > .codex/config.toml", "printf 'hooks = \\x74rue\\n' > .codex/config.toml"),
            ("echo 'hooks = \\0146alse' > .codex/config.toml", "echo 'hooks = \\0164rue' > .codex/config.toml"),
            ("printf '%s = %s\\n' hooks false > .codex/config.toml",
             "printf '%s = %s\\n' hooks true > .codex/config.toml"),
        ])

    def test_tee_and_a_heredoc_are_judged_by_their_input(self):
        self.pairs([
            ("printf '[features]\\nhooks = false\\n' | tee .codex/config.toml",
             "printf '[features]\\nhooks = true\\n' | tee .codex/config.toml"),
            ("tee -a .codex/config.toml <<< 'hooks = false'", "tee -a .codex/config.toml <<< 'effort = 1'"),
            ("cat > .codex/config.toml <<'EOF'\n[features]\nhooks = false\nEOF",
             "cat > .codex/config.toml <<'EOF'\n[features]\nmodel = \"$X\"\nEOF"),
            ("cat <<EOF > .codex/config.toml\nhooks = $OFF\nEOF", "cat <<EOF > notes.toml\nhooks = $OFF\nEOF"),
        ])

    def test_a_write_whose_text_the_command_does_not_show_is_refused(self):
        self.pairs([
            ("cp staged/config.toml .codex/config.toml", "cp staged/config.toml app.toml"),
            ("cp staged/config.toml .codex/", "cp staged/config.toml app/"),
            ("mv staged/config.toml .codex/config.toml", "mv staged/config.toml app.toml"),
            ("sed -i '' 's/true/false/' .codex/config.toml", "sed -i '' 's/true/false/' app.toml"),
            ("dd if=staged/config.toml of=.codex/config.toml", "dd if=staged/config.toml of=app.toml"),
            ("ln -sf ../staged/config.toml .codex/config.toml", "ln -sf staged/config.toml app.toml"),
            ('echo "hooks = $OFF" > .codex/config.toml', 'echo "hooks = $OFF" > notes.toml'),
            ("cat staged/config.toml > .codex/config.toml", "cat .codex/config.toml > notes.toml"),
            ("echo hooks = [f]alse > .codex/config.toml", "echo 'hooks = [f]alse' > .codex/config.toml"),
            ("python3 -c \"open('.codex/config.toml', 'w').write(open('staged/config.toml').read())\"",
             "python3 -c \"print(open('.codex/config.toml').read())\""),
            ("awk -i inplace '{print}' .codex/config.toml", "awk '{print}' .codex/config.toml"),
            ("ed -s .codex/config.toml <<< w", "ed -s notes.txt <<< w"),
            ("git checkout HEAD -- .codex/config.toml", "git diff .codex/config.toml"),
        ])

    def test_writes_that_spell_it_together_are_judged_together(self):
        self.pairs([
            ("echo -n 'hooks = fal' > .codex/config.toml; echo se >> .codex/config.toml",
             "echo -n 'hoo' > .codex/config.toml; echo 'k = 1' >> .codex/config.toml"),
        ])
        # an append is judged with the text the file already holds
        (Path(self.cwd) / ".codex" / "config.toml").write_text("hooks = fal")
        self.pairs([("echo se >> .codex/config.toml", "echo 'model = 1' >> .codex/config.toml")])

    def test_every_spelling_of_the_path_names_the_settings_file(self):
        for path in (".CODEX/Config.TOML", "./.codex/../.codex/config.toml", "$HOME/.codex/config.toml",
                     "~/.codex/config.toml", ".Claude/Settings.json"):
            with self.subTest(path=path):
                text = "{\"disableAllHooks\": true}" if "laude" in path else "hooks = false"
                (Path(guard.real(str(Path.home()))) / ".codex").mkdir(exist_ok=True)
                self.assertEqual(self.verdicts(f"echo '{text}' > staged/x"), {"allow"})
                self.assertEqual(self.verdicts(f"echo '{text}' > {path}"), {"deny"})

    def test_codex_home_set_elsewhere_holds_the_users_codex_config(self):
        home = Path(self.cwd) / "codex-home"
        home.mkdir()
        with mock.patch.object(guard.core, "CODEX_HOME", home):
            self.pairs([("echo 'hooks = false' > codex-home/config.toml", "echo 'hooks = false' > staged/config.toml")])

    def test_message_text_that_names_a_settings_file_writes_nothing(self):
        # a commit message, a PR body or a devkit command's words name a settings file without writing it; the twin
        # writes the file that text names
        self.pairs([
            ("cp x .codex/config.toml", 'dev guard check "cat ~/.codex/config.toml"'),
            ("cp x .claude/settings.json", 'dev commit -m "docs: explain ~/.claude/settings.json"'),
            ("cp x $CODEX_HOME/config.toml", 'gh pr create --title t --body "honors $CODEX_HOME/config.toml"'),
            ("vim .claude/settings.json", 'say "edit ~/.claude/settings.json"'),
            ("cp x .claude/settings.json", "dev commit -m docs .claude/settings.json"),
            # a devkit command's or gh's output option still names a file it writes
            ("dev report -o .claude/settings.json", "dev report -o notes.md"),
            ("gh release download v1 -O .codex/config.toml", "gh release download v1 -O notes.toml"),
            ("dev audit export --out=.codex/config.toml", "dev audit export --out=audit.jsonl"),
        ])

    def test_reading_the_settings_files_stays_allowed(self):
        for cmd in ("cat .codex/config.toml", "grep -n hooks .claude/settings.json", "sed -n 1p .codex/config.toml",
                    "cat .codex/config.toml > /dev/null", "git log -p .claude/settings.json",
                    "jq . .claude/settings.json", "touch .claude/settings.json"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.verdicts(cmd), {"allow"})
        self.assertEqual(self.verdicts("sed -i '' 's/a/b/' .codex/config.toml"), {"deny"})


if __name__ == "__main__":
    unittest.main()
