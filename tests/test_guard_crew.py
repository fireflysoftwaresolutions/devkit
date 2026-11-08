# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Crew guard rules: what a crew member's (a subagent's) shell command runs as devkit, which files a member may write
# whatever its tool level, and the shell forms that reach devkit's protected state (spec C11, K1). Lessons
# L-fxom7edle07, L-fxpeihqfwzb, L-fxrd7iig1mj; bug B-fxn17sez8cj; spec F3/F4 (guard side); Seraph's K1 review. The
# guard only reads command text and paths: nothing here runs a devkit command or writes devkit's state.
# Run: python3 -m unittest tests.test_guard_crew -v
import random
import shlex
import subprocess
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

CWD = str(ROOT)
STATE = core.HOME / ".local" / "state" / "devkit"
CONFIG_DIR = core.HOME / ".config" / "devkit"
PROBE = STATE / "runs" / "k1-probe-run.json"  # a name no real run uses; nothing is written to it


def verdict(cmd, cwd=CWD):
    v = guard.check("Bash", {"command": cmd}, cwd, "claude")
    return v[0] if v else "allow"


def refusal(cmd, scope=None, cwd=CWD, member=None):
    v = guard.crew_shell_verdict(cmd, cwd, scope, member)
    return v[1] if v else None


class ProtectedCwdTests(unittest.TestCase):
    """Seraph's K1 review (M68, item 1): a program started inside devkit's state or configuration writes there with
    a relative path, as a redirect, sed -i or an editor in that folder would."""

    def test_a_program_that_writes_a_relative_path_inside_devkit_state_or_config_is_refused(self):
        for cmd in (f"cd {STATE}/runs && python3 -c \"import json; json.dump({{}}, open('x.json', 'w'))\"",
                    f"cd {CONFIG_DIR} && node -e \"require('fs').writeFileSync('config.json', '{{}}')\"",
                    f"pushd {STATE}/sessions; perl -e 'open(my $f, \">\", \"s.json\")'",
                    f"cd {STATE} && python3 - <<'EOF'\nopen('runs/r.json', 'w').write('{{}}')\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "deny")

    def test_a_program_started_in_a_parent_folder_of_devkit_state_is_refused(self):
        # Seraph's re-review (M77, a): from ~/.local/state or ~/.local a relative path still reaches devkit's state
        for cmd in (f"cd {STATE.parent} && python3 -c \"open('devkit/runs/x.json', 'w')\"",
                    f"cd {STATE.parent.parent} && node -e \"require('fs').writeFileSync('state/devkit/r.json', '')\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "deny")
        self.assertEqual(verdict("cd ~ && python3 -c \"open('notes.md', 'w')\""), "allow")  # HOME holds everything

    def test_a_program_that_writes_in_a_folder_the_guard_cannot_see_is_refused_when_the_command_names_state(self):
        cmd = f"D={STATE}/runs; cd $D && python3 -c \"open('x.json', 'w').write('x')\""
        self.assertEqual(verdict(cmd), "deny")

    def test_the_same_programs_elsewhere_or_reading_state_are_allowed(self):
        for cmd in ("cd /tmp && python3 -c \"import json; json.dump({}, open('x.json', 'w'))\"",
                    "cd $TMPDIR && python3 -c \"open('x.json', 'w')\"",
                    f"cd {STATE}/runs && python3 -c \"import json; print(json.load(open('x.json')))\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "allow")


class ReviewFalsePositiveTests(unittest.TestCase):
    """Seraph's K1 review (M68, item 3) and Morpheus's audit rule (M66)."""

    def test_a_repository_script_is_its_own_code_like_a_module(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "tests" / "test_x.py"
            script.parent.mkdir()
            script.write_text(f"from pathlib import Path\nPath('{PROBE}').write_text('x')\n")
            with mock.patch.object(guard, "DEV", Path(d)), mock.patch.object(guard, "is_temp", lambda p: False):
                self.assertEqual(verdict(f"python3 {script}"), "allow")  # python3 tests/test_protected_state.py
                self.assertEqual(verdict(f"cd {STATE} && python3 {script}"), "deny")  # inside state its code counts

    def test_a_path_in_a_devkit_message_is_text(self):
        for cmd in (f'dev progress note "checked the probe" --check "{PROBE} is untouched"',
                    f'bin/dev lesson add "x" --evidence {PROBE}', f'dev crew ask "may I read {PROBE}?"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "allow")
        self.assertEqual(verdict(f'dev progress note "x" > {PROBE}'), "deny")  # a redirect still writes it

    def test_an_agent_cannot_change_audit_settings(self):
        for cmd in ("dev config set audit.retention_days 1", "bin/dev config set Audit.Enabled false",
                    "python3 -m devkit.cli config set AUDIT.redact off", "dev config set --key=audit.x 1"):
            with self.subTest(cmd=cmd):
                v = guard.check("Bash", {"command": cmd}, CWD, "claude")
                self.assertEqual(v and v[0], "deny")
                self.assertIn("only the developer changes audit settings", v[1])
        self.assertEqual(verdict("dev config show"), "allow")
        self.assertEqual(verdict("dev config set notify.sound on"), "allow")


class CrewShellTests(unittest.TestCase):
    def test_read_only_crew_commands_pass_alone_and_in_compound_commands(self):
        for cmd in ("dev crew graph", "bin/dev crew graph --json", "dev crew show neo", 'dev crew show "The Keymaker"',
                    "dev crew rules", "dev crew history", "dev crew next", "dev crew raci", "dev crew raci backend",
                    "dev crew", "dev crew --json", "dev crew sync --check", "dev crew graph | head -40",
                    "dev crew show neo && dev crew raci backend", "~/.local/bin/dev crew graph --mermaid"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refusal(cmd))

    def test_changes_to_the_team_crew_are_refused_and_name_the_tech_lead(self):
        for cmd in ('dev crew raci backend --set "R=tank A=seraph" --why x', "dev crew raci backend --set=R=tank",
                    'dev crew set neo role "x" --why y', 'dev crew brief neo "add a column"'):
            with self.subTest(cmd=cmd):
                self.assertIn("the tech lead runs it", refusal(cmd) or "")

    def test_a_lane_call_the_router_cannot_take_is_refused(self):
        for cmd in ('cd lib && dev progress step "x" --check y', 'dev crew decide D1 "A"; echo ok',
                    "echo $(dev crew mode auto)", 'echo "$(dev crew mode auto)"', 'echo "`dev crew mode auto`"',
                    "bash -c 'dev crew decide D1 A'", 'sh -c "dev progress step x"', "eval dev crew mode auto",
                    "{ dev crew decide D1 A; }", "if true; then dev crew mode auto; fi",
                    "for i in 1; do dev progress step x; done", "env dev crew decide D1 A",
                    "timeout 5 dev progress step x", "echo D1 | xargs dev crew decide",
                    "find . -name x -exec dev crew decide {} \\;", "bash <<'EOF'\ndev crew decide D1 A\nEOF",
                    "cat <<EOF\n$(dev crew decide D1 A)\nEOF", 'python3 -c "import os; os.system(\'dev crew decide D1 A\')"',
                    "python3 - <<'EOF'\nimport subprocess\nsubprocess.run(['dev', 'crew', 'decide', 'D1', 'A'])\nEOF",
                    "D=dev; $D crew decide D1 A", 'd""ev crew mode auto', "python3 bin/dev progress step x | tail -1"):
            with self.subTest(cmd=cmd):
                self.assertIn("command of its own", refusal(cmd) or "")

    def test_text_that_only_mentions_lane_commands_is_not_a_call(self):
        for cmd in ('git commit -m "fix(guard): explain dev progress step"',
                    "cd lib && git commit -m 'docs: dev crew decide is for Morpheus'",
                    "cat > /tmp/notes.md <<'EOF'\nrun dev progress step alone\nEOF",
                    "git commit -F - <<'EOF'\nfeat: dev crew ask reaches Morpheus\nEOF",
                    'dev lesson add "x" --prevent "run dev progress step as a command of its own"',
                    "grep -rn 'dev progress' lib/ | head", "echo '$(dev crew decide D1 A)'",
                    "python3 - <<'EOF'\nfrom pathlib import Path\nPath('/tmp/n.md').write_text('dev crew decide D1 A')\nEOF",
                    "node -e 'require(\"fs\").writeFileSync(\"/tmp/n.md\", `run dev crew decide alone`)'",
                    'rg -n "dev crew (ask|answer)" lib', 'git commit -m "a <<EOF b\nc" && dev crew graph'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(refusal(cmd))

    def test_any_single_quoted_text_is_never_a_call(self):
        # property: whatever a quoted argument says, the shell runs only the command around it
        rng = random.Random(20261106)
        parts = ["dev progress step x", "dev crew decide D1 A", "bash -c ", "$(", ")", "`", ";", "&&", "|", "\n",
                 "<<EOF", "EOF", '"', "'", "\\", "{ ", " }", "then ", "python3 -c ", "os.system(", " ", "#"]
        for _ in range(300):
            text = "".join(rng.choice(parts) for _ in range(rng.randint(1, 12)))
            for cmd in ("git commit -m " + shlex.quote(text), "dev lesson add x --why " + shlex.quote(text)):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(refusal(cmd))

    def test_crew_sync_needs_all_of_plugin_agents_in_the_step_scope(self):
        for scope in (["plugin/agents/**"], ["plugin/**"], ["plugin/agents/*.md"], [["plugin/**"], ["plugin/agents/"]]):
            with self.subTest(scope=scope):
                self.assertIsNone(refusal("bin/dev crew sync", scope))
        for scope in (None, [], ["lib/devkit/guard.py"], ["plugin/agents/neo.md"], [["plugin/**"], ["lib/**"]], ["**/../x"]):
            with self.subTest(scope=scope):
                self.assertIn("plugin/agents", refusal("bin/dev crew sync", scope) or "")


class BulkRewriteTests(unittest.TestCase):
    def test_text_that_mentions_a_rewrite_is_not_one(self):
        for cmd in ('dev crew ask "is t = re.sub(a, b, t) then p.write_text(t) safe?"',
                    'git commit -m "x; sed -i s/a/b/ f"', "grep -n 'sed -i' notes.md",
                    "cat > /tmp/n.md <<'EOF'\nt = re.sub(r'a', 'b', t)\np.write_text(t)\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertFalse(guard.bulk_rewrite(cmd, CWD))

    def test_a_rewrite_the_shell_runs_is_one(self):
        for cmd in ("bash -c \"sed -i '' s/a/b/ f\"", "find . -name '*.py' -exec sed -i '' s/a/b/ {} +",
                    "ls *.md | xargs perl -pi -e 's/a/b/'", "sed --in-place 's/a/b/' f", "cd lib && sed -E -i '' s/a/b/ x",
                    "python3 - <<'EOF'\nt = re.sub(r'a', 'b', t)\np.write_text(t)\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertTrue(guard.bulk_rewrite(cmd, CWD))


class MemberWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.plans = Path(core.HANDOFF) / "plans" / "acme" / "billing"
        self.glob = str(self.plans) + "/**"
        patch = mock.patch.object(guard, "current_project", return_value="acme/billing")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def exempt(self, path, scope=None, cwd=None):
        return guard.member_write_exempt(str(path), cwd or CWD, scope)

    def test_a_scratch_file_outside_the_repository_is_exempt(self):
        scratch = Path(self.tmp.name) / "scratchpad" / "notes.md"
        self.assertTrue(self.exempt(scratch, cwd=str(self.repo)))
        self.assertTrue(self.exempt(scratch, ["lib/**"], cwd=str(self.repo)))
        self.assertFalse(self.exempt(self.repo / "src" / "x.py", cwd=str(self.repo)))  # a temp checkout keeps its scope

    def test_a_plan_of_its_own_project_is_exempt_when_every_scope_layer_names_it(self):
        spec = self.plans / "2026-11-06-spec.md"
        self.assertTrue(self.exempt(spec, [self.glob]))
        self.assertTrue(self.exempt(spec, [core.tilde(self.plans) + "/**"]))
        self.assertTrue(self.exempt(spec, [[self.glob, "docs/**"], [str(self.plans) + "/*.md"]]))
        self.assertFalse(self.exempt(spec, None))  # no step scope: no grant
        self.assertFalse(self.exempt(spec, ["docs/**"]))
        self.assertFalse(self.exempt(spec, [[self.glob], ["docs/**"]]))  # a nested member stays inside its parent
        self.assertFalse(self.exempt(spec, ["**"]))  # a repository glob never reaches outside the repository

    def test_other_handoff_files_are_never_exempt(self):
        other = Path(core.HANDOFF) / "plans" / "acme" / "web" / "spec.md"
        note = Path(core.HANDOFF) / "projects" / "acme" / "billing.md"
        scope = [str(Path(core.HANDOFF)) + "/**"]
        self.assertFalse(self.exempt(other, scope))
        self.assertFalse(self.exempt(note, scope))


class ScratchFolderTests(unittest.TestCase):
    """Morpheus (M85, L-fxs9q5evz83): in the session scratchpad each crew member writes and deletes only in its own
    folder, scratchpad/<member>/; other members' files and the shared top level are not its to change."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pad = Path(self.tmp.name) / "claude-501" / "-Users-x" / "962d6aba-c6a8-44ab-9e02-1d15272468e1" / "scratchpad"
        (self.pad / "keymaker").mkdir(parents=True)
        (self.pad / "tank").mkdir()
        (self.pad / "notes.py").write_text("x")
        (self.pad / "tank" / "f3.py").write_text("x")

    def refused(self, cmd):
        return refusal(cmd, cwd=str(self.pad), member="keymaker")

    def test_a_member_works_in_its_own_folder(self):
        for cmd in ("rm keymaker/a.py", "echo x > keymaker/n.md", "mkdir -p keymaker/dk", "rm -rf keymaker/dk",
                    f"cp {CWD}/lib/devkit/guard.py keymaker/", "dev rm keymaker/old.json", "cat notes.py tank/f3.py",
                    "mkdir keymaker-f7 && echo x > keymaker-f7/n.md"):  # scratchpad/<member>-<task>/ is its own too
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.refused(cmd))

    def test_another_members_files_and_the_top_level_are_refused(self):
        for cmd in ("rm *.py", "rm notes.py", "rm -rf tank", "mv tank/f3.py keymaker/", "echo x > tank/f3.py",
                    "dev rm notes.py", f"rm -rf {self.pad}", "find . -name '*.json' -delete", "touch shared.md",
                    "cd tank && rm f3.py", "bash -c 'rm tank/f3.py'", "rm -rf tank-f3", "rm keymakerx/a.py"):
            with self.subTest(cmd=cmd):
                self.assertIn("scratchpad/keymaker/", self.refused(cmd) or "")

    def test_the_file_tools_follow_the_same_folders(self):
        self.assertTrue(guard.member_write_exempt(str(self.pad / "keymaker" / "x.md"), CWD, None, "keymaker"))
        self.assertFalse(guard.member_write_exempt(str(self.pad / "tank" / "x.md"), CWD, None, "keymaker"))
        self.assertIn("scratchpad/keymaker/", guard.scratch_refusal(str(self.pad / "notes.py"), "keymaker") or "")
        self.assertIsNone(guard.scratch_refusal(str(self.pad / "keymaker" / "x.md"), "keymaker"))
        self.assertIsNone(guard.scratch_refusal(str(Path(self.tmp.name) / "other" / "x"), "keymaker"))  # no owner


class ShellParsingTests(unittest.TestCase):
    """Parsing fixes in the shared shell walker; each one also closes a way past the protected-state rule (K1)."""

    def test_shell_keywords_do_not_hide_the_program_they_run(self):
        cmd = f"if true; then python3 -c \"open('{PROBE}', 'w').write('x')\"; fi"
        self.assertEqual(verdict(cmd), "deny")
        self.assertEqual(verdict(f"{{ python3 -c \"open('{PROBE}', 'w')\"; }}"), "deny")
        self.assertEqual(verdict("if true; then rm -rf ~; fi"), "deny")  # the workspace rules see it too

    def test_python_dash_reads_its_program_from_stdin_not_from_its_first_argument(self):
        cmd = f"python3 - /dev/null <<'EOF'\nopen('{PROBE}', 'w').write('{{}}')\nEOF"
        self.assertEqual(verdict(cmd), "deny")  # the heredoc is the program, /dev/null its argument
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "run.json"
            data.write_text('{"env": {"HOME": "/x", "DEVKIT_CONFIG": "/y"}, "home": "devkit"}')
            cmd = f"python3 -I - {data} <<'EOF'\nimport json, sys\nprint(json.load(open(sys.argv[1])))\nEOF"
            self.assertEqual(verdict(cmd), "allow")  # reading a data file that names devkit's variables

    def test_a_heredoc_marker_inside_quotes_or_a_comment_does_not_hide_the_next_line(self):
        self.assertEqual(verdict(f"echo 'a <<EOF'\necho x > {PROBE}"), "deny")
        self.assertEqual(verdict(f"echo hi # <<EOF\necho x > {PROBE}"), "deny")

    def test_a_program_that_edits_a_file_naming_devkit_state_points_to_the_edit_tools(self):
        cmd = ("python3 - <<'EOF'\nfrom pathlib import Path\np = Path('tests/test_x.py')\n"
               f"p.write_text(p.read_text().replace('a', '{STATE}/runs'))\nEOF")
        v = guard.check("Bash", {"command": cmd}, CWD, "claude")
        self.assertEqual(v[0], "deny")  # the guard cannot see which file the program writes: it stays refused
        self.assertIn("Edit or Write tool", v[1])


class HookWiringTests(unittest.TestCase):
    """hooks.py calls the guard's crew rules for a subagent's tool calls (wired by Tank, spec F3/F4)."""

    def setUp(self):
        from devkit import crew, hooks, progress
        self.hooks, self.progress, self.crew = hooks, progress, crew
        self.pad = Path(tempfile.mkdtemp()) / "claude-501" / "-x" / "962d6aba-c6a8-44ab-9e02-1d15272468e1" / "scratchpad"
        self.plans = Path(core.HANDOFF) / "plans" / "acme" / "billing"
        run = {"agents": {"o1": {"persona": "The Oracle"}}}
        for name, value in (("lane_of", lambda *a: (run, run["agents"]["o1"])), ("lane_chain", lambda r, a: [a]),
                            ("lane_tools", lambda r, a: "review"), ("lane_scope", lambda r, a: [[str(self.plans) + "/**"]])):
            p = mock.patch.object(progress, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(guard, "current_project", return_value="acme/billing")
        p.start()
        self.addCleanup(p.stop)

    def lane(self, cmd, tool="Bash"):
        return self.hooks._unroutable_lane_command({"agent_id": "o1", "tool_name": tool, "session_id": "s",
                                                    "cwd": CWD, "tool_input": {"command": cmd}})

    def write(self, path):
        return self.hooks._scope_violation("claude", {"agent_id": "o1", "tool_name": "Write", "session_id": "s",
                                                      "cwd": CWD, "tool_input": {"file_path": str(path)}})

    def test_read_only_crew_commands_and_mentions_pass_in_a_subagent(self):
        # o1 is The Oracle, review-only: its shell runs only reads (spec F7), so the mention is in a read
        for cmd in ("dev crew graph", "dev crew raci backend", 'git log --grep "explain dev progress step"'):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.lane(cmd))
        self.assertIsNone(self.lane("cd lib && dev progress step x"))  # routed to the lane (guard.lane_route)
        self.assertIn("command of its own", self.lane("cd lib && dev progress step x && rm -rf build") or "")
        self.assertIn("command of its own", self.lane(["bash", "-lc", "dev crew decide D1 A"], "exec_command") or "")
        self.assertIn("scratchpad/oracle/", self.lane(f"rm {self.pad}/tank/f3.py") or "")

    def test_the_bulk_rewrite_note_reads_what_the_shell_runs(self):
        def note(cmd):
            return self.hooks._bulk_rewrite_note({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": CWD})
        self.assertEqual(note('dev crew ask "re.sub(a, b, t) then p.write_text(t)?"'), "")
        self.assertIn("git diff -U0", note("bash -c \"sed -i '' s/a/b/ f\""))

    def test_a_review_member_writes_its_plan_and_scratch_folder_but_no_code(self):
        self.assertIsNone(self.write(self.plans / "2026-11-06-spec.md"))
        self.assertIsNone(self.write(self.pad / "oracle" / "notes.md"))
        self.assertIn("scratchpad/oracle/", self.write(self.pad / "notes.md") or "")
        self.assertIn("review-only", self.write(Path(core.DEV) / "acme" / "billing" / "src" / "x.py") or "")


class TransparentWrapperTests(unittest.TestCase):
    # Seraph's review batch 2 (M108), decided by Morpheus (M114): a wrapper's flag that takes a value (nice -n 5,
    # env -u X, sudo -u me, exec -a x) hid the command after it, and an interpreter's own flags hid `python3 -u
    # bin/dev`; `$((1<<x))` read as a heredoc hid the lines after it. The guard now sees through all of them.

    def test_a_wrapper_with_a_valued_flag_hides_nothing_from_the_main_guard(self):
        for cmd in ("nice -n 5 rm -rf ~", "env -u X rm -rf ~", "exec -a x rm -rf ~", "sudo -u me rm -rf ~",
                    "nice --adjustment=5 rm -rf ~", "time -p rm -rf ~", "nohup rm -rf ~", "command rm -rf ~",
                    "nice -n 5 gh pr merge 12", "env -S 'rm -rf ~'", "env --split-string='gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "deny")
        self.assertEqual(verdict("nice -n 5 ls ~"), "allow")

    def test_arithmetic_with_a_shift_is_not_a_heredoc(self):
        for cmd in ("echo $((1<<x))\nrm -rf ~", "(( y = 1 << 2 ))\nrm -rf ~", "echo \"$((2 << 3))\"\nrm -rf ~"):
            with self.subTest(cmd=cmd):
                self.assertEqual(verdict(cmd), "deny")
        self.assertEqual(verdict("cat <<EOF\nrm -rf ~\nEOF"), "allow")  # a real heredoc is data

    def test_a_lane_call_behind_a_wrapper_or_interpreter_flags_still_runs_as_morpheus(self):
        # (env -u X is refused before this, as an escape hatch: tests/test_escape_hatches.py)
        for cmd in ("nice -n 5 dev crew decide D1 yes", "env -C /tmp dev crew decide D1 yes",
                    "sudo -u me dev crew decide D1 yes", "exec -a x dev crew decide D1 yes",
                    "python3 -u bin/dev crew decide D1 yes", "python3 -I bin/dev crew decide D1 yes",
                    "cd lib && python3 -B -m devkit.cli crew decide D1 yes", "python3 -W ignore bin/dev progress step x",
                    "echo $((1<<x))\ndev crew decide D1 yes"):
            with self.subTest(cmd=cmd):
                self.assertIn("command of its own", refusal(cmd) or "")


if __name__ == "__main__":
    unittest.main()
