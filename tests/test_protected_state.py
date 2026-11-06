# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Protected state (spec C11, K1): agents never write devkit's state, configuration or pinned install, and never set a
# DEVKIT_* variable in a settings file. The guard only looks at paths and command text, so these tests never create or
# change anything under the real folders. Run: python3 -m unittest tests.test_protected_state -v
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, guard  # noqa: E402

HOME = core.HOME
STATE = HOME / ".local" / "state" / "devkit"
CONFIG = HOME / ".config" / "devkit" / "config.json"
SHARE = HOME / ".local" / "share" / "devkit"
# names no real run or session uses: the cp/mv rules look at the real disk to see whether a target exists
RUN = STATE / "runs" / "k1-probe-run.json"
SESSION = STATE / "sessions" / "claude-k1-probe.json"
CWD = str(core.DEV)


def verdict(tool, tool_input, cwd=CWD, agent="claude"):
    v = guard.check(tool, tool_input, cwd, agent)
    return v[0] if v else "allow"


def reason(tool, tool_input, cwd=CWD, agent="claude"):
    v = guard.check(tool, tool_input, cwd, agent)
    return v[1] if v else ""


def bash(cmd, cwd=CWD, agent="claude"):
    return verdict("Bash", {"command": cmd}, cwd, agent)


class FileToolTests(unittest.TestCase):
    def test_edit_of_the_session_file_is_denied_for_every_agent(self):
        # a session record carries the lane, the role and the scope grant: an agent that edits it grants itself powers
        edit = {"file_path": str(SESSION), "old_string": '"lane": "tank"', "new_string": '"lane": "main"'}
        multi = {"file_path": str(SESSION), "edits": [{"old_string": "a", "new_string": "b"}]}
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                self.assertEqual(verdict("Edit", edit, agent=agent), "deny")
                self.assertEqual(verdict("MultiEdit", multi, agent=agent), "deny")
        self.assertIn("`dev ", reason("Edit", edit))

    def test_write_of_config_json_is_denied_and_points_to_the_terminal(self):
        r = reason("Write", {"file_path": str(CONFIG), "content": '{"safeguards": {"irreversible_main": "off"}}'})
        self.assertIn("terminal", r)
        self.assertIn("dev config edit", r)
        # a folder whose name only starts with "devkit" is not devkit's
        self.assertEqual(verdict("Write", {"file_path": str(HOME / ".config" / "devkit-other" / "x.json"),
                                           "content": "{}"}), "allow")

    def test_notebook_and_patch_writes_into_protected_folders_are_denied(self):
        self.assertEqual(verdict("NotebookEdit", {"notebook_path": str(SHARE / "n.ipynb"), "new_source": "x"}), "deny")
        for op in ("Add", "Update", "Delete"):
            with self.subTest(op=op):
                patch = f"*** Begin Patch\n*** {op} File: {RUN}\n+{{}}\n*** End Patch"
                self.assertEqual(verdict("apply_patch", {"command": patch}, agent="codex"), "deny")
        move = f"*** Begin Patch\n*** Update File: /tmp/k1-probe.json\n*** Move to: {RUN}\n*** End Patch"
        self.assertEqual(verdict("apply_patch", {"command": move}, agent="codex"), "deny")

    def test_subagent_meta_files_are_protected_but_readable(self):
        # the meta file binds a subagent to its launch record (spec C3): a forged one would claim another lane
        meta = HOME / ".claude" / "projects" / "-k1-probe" / "sid" / "subagents" / "agent-k1.meta.json"
        self.assertEqual(verdict("Write", {"file_path": str(meta), "content": '{"toolUseId": "x"}'}), "deny")
        self.assertEqual(verdict("Read", {"file_path": str(meta)}), "allow")

    def test_case_and_symlink_variants_of_the_state_path_are_denied(self):
        # macOS folders are case-insensitive, and a symlink in a temp folder reaches the same files
        upper = HOME / ".LOCAL" / "State" / "devkit" / "runs" / "r.json"
        self.assertEqual(verdict("Write", {"file_path": str(upper), "content": "{}"}), "deny")
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "s"
            os.symlink(STATE, link)
            self.assertEqual(verdict("Write", {"file_path": str(link / "runs" / "r.json"), "content": "{}"}), "deny")
            self.assertEqual(bash(f"ln -s ~/.local/state/devkit {tmp}/t"), "deny")


class ShellTests(unittest.TestCase):
    def test_redirect_into_a_run_file_is_denied_but_reading_it_is_allowed(self):
        self.assertEqual(bash("echo x > ~/.local/state/devkit/runs/r.json"), "deny")
        self.assertEqual(bash(f"echo x >> {RUN}"), "deny")
        self.assertIn("dev progress", reason("Bash", {"command": "echo x > ~/.local/state/devkit/runs/r.json"}))
        self.assertEqual(bash("cat ~/.local/state/devkit/runs/r.json"), "allow")
        self.assertEqual(bash("jq .steps ~/.local/state/devkit/runs/r.json > /tmp/k1-steps.json"), "allow")
        self.assertEqual(verdict("Read", {"file_path": str(RUN)}), "allow")

    def test_sed_in_place_on_state_is_denied(self):
        self.assertEqual(bash(f"sed -i '' 's/\"mode\": \"guided\"/\"mode\": \"auto\"/' {RUN}"), "deny")
        self.assertEqual(bash(f"sed -i.orig -e 's/a/b/' {RUN}"), "deny")
        self.assertEqual(bash(f"sed -n 1,5p {RUN}"), "allow")

    def test_python_inline_writes_to_state_are_denied(self):
        self.assertEqual(bash(f"python3 -c \"open('{RUN}', 'w').write('{{}}')\""), "deny")
        self.assertEqual(bash("python3 -c \"import pathlib; pathlib.Path.home().joinpath('.local', 'state', 'devkit', "
                              "'runs', 'r.json').write_text('{}')\""), "deny")
        heredoc = ("python3 - <<'EOF'\nfrom pathlib import Path\n"
                   "(Path.home() / '.local/state/devkit/runs/r.json').write_text('{}')\nEOF")
        self.assertEqual(bash(heredoc), "deny")
        self.assertEqual(bash(f"python3 -c \"import json; print(json.load(open('{RUN}')))\""), "allow")

    def test_tee_cp_and_mv_into_protected_folders_are_denied(self):
        self.assertEqual(bash("echo '{}' | tee ~/.local/state/devkit/runs/r.json"), "deny")
        self.assertEqual(bash(f"mv /tmp/k1-forged.json {RUN}"), "deny")
        self.assertEqual(bash("mv /tmp/k1-forged.json ~/.local/share/devkit/"), "deny")
        self.assertEqual(bash("cp /tmp/k1-forged.json ~/.config/devkit/config.json"), "deny")
        # moving a record out deletes it from devkit's state
        self.assertEqual(bash(f"mv {RUN} /tmp/k1-moved.json"), "deny")
        self.assertEqual(bash(f"cp {RUN} /tmp/k1-copy.json"), "allow")

    def test_rm_and_dev_rm_of_state_are_denied_even_with_the_raw_flag(self):
        self.assertEqual(bash(f"DEVKIT_RAW_FS=1 rm -f {SESSION}"), "deny")
        self.assertEqual(bash("dev rm ~/.local/state/devkit/runs"), "deny")
        # the folder that holds devkit's state goes with it
        self.assertEqual(bash("DEVKIT_RAW_FS=1 rm -rf ~/.local/state"), "deny")
        self.assertEqual(bash("dev rm /tmp/k1-scratch.txt"), "allow")

    def test_shell_wrappers_and_variables_do_not_hide_a_state_write(self):
        self.assertEqual(bash("bash -c 'echo x > ~/.local/state/devkit/runs/r.json'"), "deny")
        codex = {"command": ["bash", "-lc", "echo x > ~/.local/state/devkit/runs/r.json"]}
        self.assertEqual(verdict("Bash", codex, agent="codex"), "deny")
        self.assertEqual(bash("D=~/.local/state/devkit; echo x > $D/runs/r.json"), "deny")
        self.assertEqual(bash("cd ~/.local/state/devkit/runs && echo x > r.json"), "deny")
        self.assertEqual(bash("D=~/.local/state/devkit; ls $D/runs"), "allow")

    def test_dev_config_set_safeguards_is_refused_but_dev_commands_that_write_state_run(self):
        self.assertEqual(bash("dev config set safeguards.irreversible_main enforce"), "deny")
        self.assertEqual(bash("bin/dev config set safeguards.crew_rules=audit"), "deny")
        self.assertEqual(bash("dev config edit"), "deny")
        self.assertIn("terminal", reason("Bash", {"command": "dev config set safeguards.irreversible_main off"}))
        # devkit's own commands write the state in their own process, which the guard does not see
        self.assertEqual(bash('dev progress step "K1 protect devkit state and config" --check "14 tests"'), "allow")
        self.assertEqual(bash("dev config show"), "allow")
        self.assertEqual(bash('dev task add "probe" --why "k1"'), "allow")


class SettingsEnvTests(unittest.TestCase):
    def test_a_settings_file_whose_env_sets_a_devkit_key_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = str(Path(tmp) / ".claude" / "settings.local.json")
            self.assertEqual(verdict("Write", {"file_path": s, "content": json.dumps(
                {"env": {"DEVKIT_SCOPE_OVERRIDE": "1"}})}), "deny")
            # the same key spelled with a JSON escape
            self.assertEqual(verdict("Write", {"file_path": s, "content": '{"env": {"DEVKIT\\u005fGUARD": "off"}}'}),
                             "deny")
            self.assertEqual(verdict("Write", {"file_path": s, "content": json.dumps({"env": {"NODE_ENV": "test"}})}),
                             "allow")
            toml = '[shell_environment_policy]\nset = { DEVKIT_GUARD = "off" }\n'
            self.assertEqual(verdict("Write", {"file_path": str(Path(tmp) / ".codex" / "config.toml"),
                                               "content": toml}), "deny")

    def test_an_edit_that_adds_or_changes_a_devkit_key_is_denied_but_other_env_edits_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = Path(tmp) / ".claude" / "settings.json"
            s.parent.mkdir()
            s.write_text(json.dumps({"env": {"DEVKIT_SKIN": "dark", "NODE_ENV": "test"}}, indent=2))

            def edit(old, new):
                return verdict("Edit", {"file_path": str(s), "old_string": old, "new_string": new})
            self.assertEqual(edit('"NODE_ENV": "test"', '"NODE_ENV": "test",\n    "DEVKIT_GUARD": "off"'), "deny")
            self.assertEqual(edit('"DEVKIT_SKIN": "dark"', '"DEVKIT_SKIN": "light"'), "deny")
            # a DEVKIT_* key the developer already set does not block an unrelated edit
            self.assertEqual(edit('"NODE_ENV": "test"', '"NODE_ENV": "dev"'), "allow")

    def test_shell_writes_of_a_devkit_key_into_settings_are_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(bash("echo '{\"env\": {\"DEVKIT_GUARD\": \"off\"}}' > .claude/settings.local.json",
                                  cwd=tmp), "deny")
            forged = Path(tmp) / "forged.json"
            forged.write_text(json.dumps({"env": {"DEVKIT_GUARD": "off"}}))
            self.assertEqual(bash(f"cp {forged} {tmp}/.claude/settings.json"), "deny")
            self.assertEqual(bash("echo '{\"env\": {}}' > .claude/settings.local.json", cwd=tmp), "allow")


if __name__ == "__main__":
    unittest.main()
