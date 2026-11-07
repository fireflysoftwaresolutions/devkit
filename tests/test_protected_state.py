# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Protected state (spec C11, K1): agents never write devkit's state, configuration or pinned install, and never set a
# DEVKIT_* variable in a settings file. The guard only looks at paths and command text, so these tests never create or
# change anything under the real folders. Run: python3 -m unittest tests.test_protected_state -v
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = str(ROOT / "lib")
sys.path.insert(0, LIB)

from devkit import core, fs, guard, policy  # noqa: E402

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


# ---- K1 follow-ups: the `dev` link, a forged HOME or configuration, the roots' mode, the pinned install's paths

class DevLinkTests(unittest.TestCase):
    # a shell runs whatever ~/.local/bin/dev points at (the pinned copy after `dev install`), so an agent that swaps the
    # link, or puts another `dev` earlier on PATH, runs its own code as devkit for the developer and every job
    def test_swapping_or_writing_through_the_dev_link_is_denied_but_reading_and_running_it_pass(self):
        link = "~/.local/bin/dev"
        for cmd in (f"ln -sf /tmp/k1-evil/bin/dev {link}", f"ln -sfn /tmp/k1-evil/bin/dev {link}",
                    f"DEVKIT_RAW_FS=1 rm -f {link}", f"DEVKIT_RAW_FS=1 mv /tmp/k1-evil-dev {link}",
                    f"echo 'exit 0' > {link}", "mv ~/.local/bin /tmp/k1-bin",
                    "python3 -c \"import os; os.symlink('/tmp/k1-evil', os.path.expanduser('~/.local/bin/dev'))\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), "deny")
        self.assertEqual(verdict("Write", {"file_path": str(HOME / ".local" / "bin" / "dev"), "content": "#!/bin/sh\n"}),
                         "deny")
        self.assertIn("dev doctor", reason("Bash", {"command": f"ln -sf /tmp/k1-evil {link}"}))
        for cmd in (f"readlink {link}", f"ls -l {link}", "dev status", f"{link} status"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), "allow")

    def test_a_dev_earlier_on_path_is_denied(self):
        # core puts /opt/homebrew/bin and /usr/local/bin before ~/.local/bin on PATH
        self.assertEqual(bash("ln -s /tmp/k1-evil /opt/homebrew/bin/dev"), "deny")
        self.assertEqual(verdict("Write", {"file_path": "/usr/local/bin/dev", "content": "#!/bin/sh\n"}), "deny")
        # a repository's own bin/dev is code under review, not the command the shell finds
        self.assertEqual(verdict("Write", {"file_path": str(core.LOCAL / "k1-probe" / "bin" / "dev"),
                                           "content": "#!/bin/sh\n"}, agent="codex"), "allow")


class ForgedEnvTests(unittest.TestCase):
    # devkit finds its state, configuration and handoff through HOME, DEVKIT_CONFIG, DEV_ROOT and DEVKIT_* variables:
    # set for a devkit call, they point it at records devkit never wrote, such as an approval nobody gave
    def test_a_forged_home_config_or_devkit_variable_for_a_devkit_call_is_denied_but_hatches_pass(self):
        for cmd in ("HOME=/tmp/k1-fake dev crew answer 3 yes", "DEVKIT_CONFIG=/tmp/k1-c.json dev release",
                    "env HOME=/tmp/k1-fake bin/dev progress step x", "export HOME=/tmp/k1-fake; dev status",
                    "HOME=/tmp/k1-fake; dev progress done x", "DEV_ROOT=/tmp/k1-root dev task done 3",
                    "DEVKIT_SESSION=claude:main dev progress step x", "DEVKIT_FROM_AGENT=tank dev crew answer 1 ok",
                    "HOME=/tmp/k1-fake python3 -m devkit.cli status", "HOME=/tmp/k1-fake python3 ~/.local/bin/dev status",
                    "HOME=/tmp/k1-fake python3 -c 'import sys; sys.path.insert(0, \"lib\"); from devkit import cli'",
                    "bash -c 'HOME=/tmp/k1-fake dev status'",
                    "echo 3 | xargs env DEVKIT_CONFIG=/tmp/k1-c.json dev task done",
                    "HOME=$(mktemp -d) dev status", "export $(cat /tmp/k1-env) && dev release",
                    "python3 -c \"import os, subprocess; os.environ['HOME'] = '/tmp/k1-fake'; "
                    "subprocess.run(['dev', 'release'])\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), "deny")
        r = reason("Bash", {"command": "HOME=/tmp/k1-fake dev status"})
        self.assertIn("HOME", r)
        self.assertIn("terminal", r)
        # the per-command hatches devkit documents, and variables for other tools
        for cmd in ("DEVKIT_LOCAL_CI_PASSED=1 dev push", "DEVKIT_CROSS_PROJECT=1 dev task add 'x' --why y",
                    "HOME=/tmp/k1-fake npm test", "NODE_ENV=test dev ci --run", "dev status && HOME=/tmp/k1-x make",
                    "env -u HOME dev status", "claude -p 'summarize'", "dev install --help"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), "allow")

    def test_a_nested_agent_session_or_launchd_gets_no_devkit_variable(self):
        # a nested session's hooks are devkit calls; launchctl setenv reaches every app the developer opens next
        for cmd in ("DEVKIT_GUARD=off claude -p 'go'", "HOME=/tmp/k1-fake codex exec 'go'",
                    "DEVKIT_CONFIG=/tmp/k1-c.json claude -p 'spike'", "launchctl setenv DEVKIT_CONFIG /tmp/k1-c.json"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), "deny")

    def test_dev_install_and_dev_update_are_left_to_the_developer(self):
        for cmd in ("dev install", "bin/dev install --only cli", "dev update",
                    "~/.local/bin/dev install --version 26.11.2"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), "deny")
        self.assertEqual(bash("./install.sh", cwd=str(core.DEVKIT_HOME)), "deny")
        self.assertIn("terminal", reason("Bash", {"command": "dev install"}))
        self.assertEqual(bash("dev doctor"), "allow")


class ShellStartupFileTests(unittest.TestCase):
    # ~/.zshrc and its kin reach the developer's terminals and every agent session started from them
    def test_a_devkit_variable_in_a_shell_startup_file_is_denied(self):
        self.assertEqual(bash("echo 'export DEVKIT_GUARD=off' >> ~/.zshrc"), "deny")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(verdict("Write", {"file_path": str(Path(tmp) / ".zprofile"),
                                               "content": "export DEV_ROOT=/tmp/k1-root\n"}), "deny")
            self.assertEqual(verdict("Write", {"file_path": str(Path(tmp) / ".zshrc"),
                                               "content": "export PATH=/opt/k1/bin:$PATH\n"}), "allow")
            s = str(Path(tmp) / ".claude" / "settings.local.json")
            self.assertEqual(verdict("Write", {"file_path": s, "content": json.dumps({"env": {"DEV_ROOT": "/tmp/x"}})}),
                             "deny")


def _run_core(env, code="from devkit import core; print(core.CONFIG_FILE); print(core.DEV)"):
    base = {"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
    return subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {LIB!r}); {code}"],
                          env={**base, **env}, capture_output=True, text=True, timeout=60)


class ForgedConfigTests(unittest.TestCase):
    # devkit itself, for what the guard cannot read in a command's text: a config path outside the standard place, or
    # a DEV_ROOT, counts only when the developer set it in a terminal; an agent's command or hook gets the standard one
    def test_devkit_ignores_a_config_outside_the_standard_place_inside_an_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            forged = home / "forged.json"
            forged.write_text(json.dumps({"dev_root": str(home / "forged-root")}))
            p = _run_core({"HOME": str(home), "DEVKIT_CONFIG": str(forged), "DEV_ROOT": str(home / "forged-dev"),
                           "CLAUDECODE": "1"})
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(p.stdout.split(), [str(home / ".config" / "devkit" / "config.json"),
                                                str(home / "Development")])
            self.assertIn("DEVKIT_CONFIG", p.stderr)
            # the standard place under the HOME the process has is not forged (tests run devkit in a temporary HOME)
            std = home / ".config" / "devkit" / "config.json"
            p = _run_core({"HOME": str(home), "DEVKIT_CONFIG": str(std), "CLAUDECODE": "1"})
            self.assertEqual((p.stdout.split()[:1], p.stderr), ([str(std)], ""))

    def test_only_a_terminal_without_an_agent_above_it_counts_as_the_developer(self):
        dev_shell = ["-/bin/zsh", "/usr/bin/login -flp ada", "/Applications/Ghostty.app/Contents/MacOS/ghostty"]
        self.assertTrue(core.set_by_developer({}, ancestry=dev_shell, terminal=True))
        self.assertFalse(core.set_by_developer({}, ancestry=dev_shell, terminal=False))  # a job, a pipe, a hook
        self.assertFalse(core.set_by_developer({"CLAUDECODE": "1"}, ancestry=dev_shell, terminal=True))
        for agent in ("claude --resume", "/opt/homebrew/bin/codex exec go",
                      "/Applications/Claude.app/Contents/MacOS/Claude",
                      "node /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js"):
            with self.subTest(agent=agent):
                self.assertFalse(core.set_by_developer({}, ancestry=["/bin/zsh -c x", agent] + dev_shell,
                                                       terminal=True))
        self.assertFalse(core.set_by_developer({}, ancestry=None, terminal=True))  # ps failed: not proven
        # a folder named after an agent is not an agent
        self.assertTrue(core.set_by_developer({}, ancestry=["vim ~/notes/claude", "zsh -c 'cd ~/codex && make'"]
                                              + dev_shell, terminal=True))


class RootModeTests(unittest.TestCase):
    def test_devkit_makes_its_three_roots_private_when_it_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            (home / ".config" / "devkit").mkdir(parents=True)
            os.chmod(home / ".config" / "devkit", 0o755)
            p = _run_core({"HOME": str(home)}, "from devkit import core")
            self.assertEqual(p.returncode, 0, p.stderr)
            for root in (".local/state/devkit", ".config/devkit", ".local/share/devkit"):
                with self.subTest(root=root):
                    self.assertEqual(stat.S_IMODE((home / root).stat().st_mode), 0o700)


class PinnedPathsTests(unittest.TestCase):
    # after `dev install`, core.DEVKIT_HOME is the pinned copy when devkit runs from it, and the checkout when the
    # developer runs bin/dev: each must still protect, and compare with, the other one
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        self.home, self.checkout = root / "home", root / "src" / "devkit"
        self.pin = self.home / ".local" / "share" / "devkit" / "releases" / "26.11.3-abc1234"
        (self.pin / "config" / "git").mkdir(parents=True)
        (self.pin / "config" / "git" / "ignore").write_text(".DS_Store\n")
        (self.pin / core.PIN_MANIFEST).write_text(json.dumps({"commit": "abc1234", "source": str(self.checkout)}))
        (self.checkout / ".git").mkdir(parents=True)
        (self.home / ".local" / "share" / "devkit" / "install.json").write_text(
            json.dumps({"path": str(self.pin), "source": str(self.checkout), "version": "26.11.3"}))
        p = mock.patch.object(core, "HOME", self.home)
        p.start()
        self.addCleanup(p.stop)

    def running(self, home):
        p = mock.patch.object(core, "DEVKIT_HOME", home)
        p.start()
        self.addCleanup(p.stop)

    def test_rm_r_of_the_checkout_is_refused_while_the_pinned_copy_runs_even_with_the_raw_flag(self):
        self.running(self.pin)
        self.assertEqual(core.source_home(), self.checkout)
        self.assertEqual(bash(f"DEVKIT_RAW_FS=1 rm -rf {self.checkout}"), "deny")

    def test_dev_rm_refuses_the_pinned_copy_and_its_releases_folder_when_the_checkout_runs(self):
        self.running(self.checkout)
        self.assertIsNotNone(fs._structure_reason(core.real(self.pin), True, False))
        self.assertIsNotNone(fs._structure_reason(core.real(self.pin.parent), True, False))
        self.running(self.pin)
        self.assertIsNotNone(fs._structure_reason(core.real(self.checkout), True, True))  # even in a temp folder

    def test_the_global_gitignore_is_the_pinned_copys_without_drift_from_the_checkout(self):
        self.running(self.checkout)
        gc = self.home / "gitconfig"
        gc.write_text(f"[user]\n\tname = Ada\n\temail = 1+ada@users.noreply.github.com\n"
                      f"[core]\n\texcludesFile = {self.pin / 'config' / 'git' / 'ignore'}\n")
        with mock.patch.object(core, "gh_user", return_value={"login": "ada", "id": 1, "name": "Ada", "email": ""}), \
                mock.patch.dict(core.GIT_ENV, {"GIT_CONFIG_GLOBAL": str(gc), "GIT_CONFIG_NOSYSTEM": "1"}):
            self.assertEqual([d for d in policy.diff_git() if d.key == "core.excludesFile"], [])


if __name__ == "__main__":
    unittest.main()
