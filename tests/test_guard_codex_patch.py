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

    def test_an_escaped_key_in_a_hunk_the_guard_cannot_place_is_read_as_toml(self):
        # a hunk that does not apply, or only adds lines, leaves no text after the patch: its added lines are parsed
        # as TOML alone and after the file's text, so a key with a \u escape counts as Codex reads it
        cfg = self.codex / "config.toml"
        off, on = '+"codex_hoo\\u006bs" = false', '+"verbos\\u0065" = false'
        for context in ([" no such line"], []):
            with self.subTest(context=context):
                self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", *context, off)), "deny")
                self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", *context, on)), "allow")

    def test_a_move_onto_the_config_brings_the_sources_text_and_is_denied(self):
        src, cfg = self.codex / "staged.toml", self.codex / "config.toml"
        self.assertEqual(self.verdict(patch(f"*** Update File: {src}", f"*** Move to: {cfg}", "@@",
                                            '-model = "a"', '+model = "b"')), "deny")
        # the same move of a source that keeps the hooks on
        src.write_text('[features]\nmodel = "a"\n')
        self.assertEqual(self.verdict(patch(f"*** Update File: {src}", f"*** Move to: {cfg}", "@@",
                                            '-model = "a"', '+model = "b"')), "allow")

    def test_an_update_of_a_settings_file_too_big_to_read_is_denied(self):
        # over the guard's read limit the patch cannot be placed in the file's text, so its added lines could finish
        # a key the file starts; an Add writes the whole text and is judged by it
        cfg, big = self.codex / "config.toml", self.codex / "big.toml"
        for f in (cfg, big):
            f.write_text("[features]\n" + "# pad\n" * (1 << 18) + "hooks =\n")
        self.assertEqual(self.verdict(patch(f"*** Update File: {cfg}", "@@", "+false")), "deny")
        self.assertEqual(self.verdict(patch(f"*** Update File: {big}", f"*** Move to: {self.codex / 'hooks.json'}",
                                            "@@", "+false")), "deny")
        self.assertEqual(self.verdict(patch(f"*** Update File: {big}", "@@", "+false")), "allow")
        self.assertEqual(self.verdict(patch(f"*** Add File: {cfg}", "+[features]", '+model = "b"')), "allow")

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


class EditsOnAgentSettings(unittest.TestCase):
    """Edit and MultiEdit are judged by the file's text after the edits, as Write is by its content: an edit that turns
    a true into a false matches no pattern in its new_string alone. An edit whose old_string is not in a settings file
    is refused, since the guard cannot tell what it changes."""

    PLUGIN = '{"enabledPlugins": {"devkit@firefly-devkit": true, "other@market": true}}\n'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = guard.real(tmp.name)
        root = Path(self.cwd)
        for d in (".claude", ".codex"):
            (root / d).mkdir()
        self.settings, self.config = root / ".claude" / "settings.json", root / ".codex" / "config.toml"
        self.settings.write_text(self.PLUGIN)
        self.config.write_text("[features]\ncodex_hooks = true\nverbose = true\n")
        self.notes = root / ".claude" / "notes.json"
        self.notes.write_text(self.PLUGIN)

    def verdict(self, tool, path, **tool_input):
        v = guard.check(tool, {"file_path": str(path), **tool_input}, self.cwd, "claude")
        return (v or ("allow",))[0]

    def test_an_edit_that_turns_the_plugin_off_is_denied(self):
        self.assertEqual(self.verdict("Edit", self.settings, old_string='firefly-devkit": true',
                                      new_string='firefly-devkit": false'), "deny")
        self.assertEqual(self.verdict("Edit", self.settings, old_string='market": true', new_string='market": false'),
                         "allow")
        self.assertEqual(self.verdict("Edit", self.notes, old_string='firefly-devkit": true',
                                      new_string='firefly-devkit": false'), "allow")  # not a settings file
        # the user's own settings, named with ~ as Claude's Edit reads it
        user = Path(guard.real(str(Path.home()))) / ".claude"
        user.mkdir(exist_ok=True)
        (user / "settings.json").write_text(self.PLUGIN)
        self.assertEqual(self.verdict("Edit", "~/.claude/settings.json", old_string='firefly-devkit": true',
                                      new_string='firefly-devkit": false'), "deny")
        self.assertEqual(self.verdict("Edit", "~/.claude/settings.json", old_string='market": true',
                                      new_string='market": false'), "allow")

    def test_an_edit_of_a_toml_value_is_judged_by_the_file_it_leaves(self):
        self.assertEqual(self.verdict("Edit", self.config, old_string="codex_hooks = true",
                                      new_string="codex_hooks = false"), "deny")
        self.assertEqual(self.verdict("Edit", self.config, old_string="verbose = true", new_string="verbose = false"),
                         "allow")
        # a key spelled with a \\u escape, which only the TOML parse reads
        self.assertEqual(self.verdict("Edit", self.config, old_string="codex_hooks = true",
                                      new_string='"codex_hoo\\u006bs" = false'), "deny")

    def test_replace_all_and_multi_edit_apply_as_the_tools_do(self):
        self.config.write_text("[features]\ncodex_hooks = true\n")
        self.assertEqual(self.verdict("Edit", self.settings, old_string="true", new_string="false", replace_all=True),
                         "deny")
        # without replace_all only the first match changes, which in this config is the hooks' own value
        self.assertEqual(self.verdict("Edit", self.config, old_string="true", new_string="false", replace_all=False),
                         "deny")
        self.assertEqual(self.verdict("MultiEdit", self.settings, edits=[
            {"old_string": 'market": true', "new_string": 'market": 1'},
            {"old_string": "1", "new_string": "false"}]), "allow")
        self.assertEqual(self.verdict("MultiEdit", self.settings, edits=[
            {"old_string": 'market": true', "new_string": 'market": 1'},
            {"old_string": "true", "new_string": "false", "replace_all": True}]), "deny")

    def test_an_edit_whose_old_string_is_not_in_a_settings_file_is_denied(self):
        self.assertEqual(self.verdict("Edit", self.settings, old_string="no such text", new_string="x"), "deny")
        self.assertEqual(self.verdict("Edit", self.notes, old_string="no such text", new_string="x"), "allow")
        self.assertEqual(self.verdict("Edit", self.settings, old_string="other@market", new_string="other@shop"),
                         "allow")


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

    def test_index_only_git_commands_and_check_flags_write_no_text(self):
        self.pairs([
            ("git restore .claude/settings.json", "git restore --staged .claude/settings.json"),
            ("git restore --staged --worktree .claude/settings.json", "git restore -S .claude/settings.json"),
            ("git rm .claude/settings.json", "git rm --cached .claude/settings.json"),
            ("npx prettier --write .claude/settings.json", "npx prettier --check .claude/settings.json"),
        ])

    def test_git_options_are_read_as_git_parses_them(self):
        # git takes a unique prefix of a long option (--wor is --worktree), a short option's value attached (-sS1 is
        # --source=S1) and the last of --cached and --no-cached: only a command that touches the index alone is allowed
        self.pairs([
            ("git restore --staged --wor .claude/settings.json", "git restore --staged .claude/settings.json"),
            ("git restore --staged --source=FETCH_HEAD --wor .claude/settings.json",
             "git restore --staged --source=FETCH_HEAD .claude/settings.json"),
            ("git restore -sS1 .claude/settings.json", "git restore --staged -sWIP .claude/settings.json"),
            ("git restore --staged -s HEAD -W .claude/settings.json", "git restore -qS -s HEAD .claude/settings.json"),
            ("git rm --cached --no-cached .claude/settings.json", "git rm -r --cached .claude/settings.json"),
        ])

    def test_only_git_commands_known_to_write_no_work_tree_file_are_text_free(self):
        # a git command the guard does not list as a reader may write a file it names (an archive, a bundle, a merge
        # tool, a config file), and a reader's output option or grep's pager writes too
        self.pairs([
            ("git diff --output=.claude/settings.json", "git diff --stat .claude/settings.json"),
            ("git log --outp .claude/settings.json", "git log --oneline .claude/settings.json"),
            ("git archive -o .claude/settings.json HEAD", "git archive -o out.tar HEAD"),
            ("git bundle create .claude/settings.json HEAD", "git bundle create repo.bundle HEAD"),
            ("git mergetool .claude/settings.json", "git blame .claude/settings.json"),
            ("git config --file .claude/settings.json core.x y", "git config --file notes.ini core.x y"),
            ("git grep -Ovi hooks .claude/settings.json", "git grep -n hooks .claude/settings.json"),
        ])

    def test_only_a_known_formatter_run_to_check_writes_no_text(self):
        # --check means "write nothing" only to a formatter or linter the guard knows; any other program may write the
        # file it names whatever its flags, and a write flag in any case or abbreviation still writes
        self.pairs([
            ("./fmt.sh --check .claude/settings.json", "npx prettier --check .claude/settings.json"),
            ("wget --check -O .claude/settings.json https://example.com/s.json",
             "wget --check -O notes.json https://example.com/s.json"),
            ("npx prettier --check --WRITE .claude/settings.json", "npx prettier --list-different .claude/settings.json"),
            ("npx prettier --check --wr .claude/settings.json", "npx prettier --check --no-color .claude/settings.json"),
            ("black --check -W .claude/settings.json", "black --check -l 100 .claude/settings.json"),
        ])

    def test_a_truncating_write_replaces_the_files_text_and_only_an_append_keeps_it(self):
        # the file already turns hooks off: an append keeps that text, a `>` (the repair) and touch do not add to it
        (Path(self.cwd) / ".codex" / "config.toml").write_text("[features]\nhooks = false\n")
        self.pairs([
            ("printf 'model = 1\\n' >> .codex/config.toml", "printf 'model = 1\\n' > .codex/config.toml"),
            ("tee -a .codex/config.toml <<< 'model = 1'", "tee .codex/config.toml <<< 'model = 1'"),
            ("echo 'model = 1' >> .codex/config.toml", "touch .codex/config.toml"),
            ("echo 'model = 1' >> .codex/config.toml", "git add .codex/config.toml"),
        ])

    def test_a_json_hooks_off_split_across_two_commands_is_refused(self):
        settings = Path(self.cwd) / ".claude" / "settings.json"
        self.assertEqual(self.verdicts("printf '{\"disableAllHooks\": ' > .claude/settings.json"), {"allow"})
        settings.write_text('{"disableAllHooks": ')  # what the first command leaves in the file
        self.assertEqual(self.verdicts("printf 'true}' >> .claude/settings.json"), {"deny"})
        self.assertEqual(self.verdicts("printf 'true}' | tee -a .claude/settings.json"), {"deny"})
        # the twin: the same append after a key that switches nothing off
        settings.write_text('{"includeCoAuthoredBy": ')
        self.assertEqual(self.verdicts("printf 'true}' >> .claude/settings.json"), {"allow"})
        settings.write_text('{"enabledPlugins": {"devkit@firefly-devkit": ')
        self.pairs([("printf 'false}}' >> .claude/settings.json", "printf 'true}}' >> .claude/settings.json")])

    def test_an_append_to_a_settings_file_too_big_to_read_is_refused(self):
        # over the guard's read limit the file's text is unknown, so an append or a <> could finish a key the file
        # starts; a > replaces the text and is judged by what it writes
        big = '{"pad": "' + "x" * (1 << 20) + '", "disableAllHooks": '
        (Path(self.cwd) / ".claude" / "settings.json").write_text(big)
        (Path(self.cwd) / "staged" / "big.json").write_text(big)
        self.pairs([
            ("printf 'true}' >> .claude/settings.json", "printf 'true}' >> staged/big.json"),
            ("printf 'true}' | tee -a .claude/settings.json", "printf '{}' | tee .claude/settings.json"),
            ("printf '{}' 1<> .claude/settings.json", "printf '{}' > .claude/settings.json"),
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
