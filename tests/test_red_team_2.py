# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Red team (Agent Smith), part 2, with the code reviewer's heredoc and MCP findings: each test reproduces a bypass of
# the part-1 and part-2 fixes and asserts the fixed behavior, next to the ordinary commands each fix must leave alone.
# Records go through the real audit.record into a temp trail, never a stub (L-fxzc02legsi).
# Run: python3 -m unittest tests.test_red_team_2 -v
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from devkit import audit, core, guard  # noqa: E402
import test_progress as tp  # noqa: E402  the real session and transcript (TranscriptCase)
import test_branches as tb  # noqa: E402  the backup sandbox: a bare origin and the real audit trail

_CURRENT_PROJECT = guard.current_project  # the real one, for the tests a stand-in would hide a bypass from


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], check=True,
                   capture_output=True)


def make_repo(root) -> str:
    repo = Path(root) / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "package.json").write_text(json.dumps({"name": "@acme/widget", "version": "1.4.0"}))
    (repo / "README").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "tag", "v26.11.4")
    _git(repo, "checkout", "-q", "-b", "feat/x")
    return str(repo)


class DenyAll:
    def __call__(self, *a, **k):
        return None


def settings(main="enforce", member="enforce"):
    cfg = {"safeguards": {"irreversible_main": main, "irreversible_member": member}}
    return mock.patch.object(core, "load_config", return_value=cfg)


class RedTeam2(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = make_repo(self.tmp.name)
        root = Path(self.tmp.name)
        (root / "handoff" / "projects").mkdir(parents=True)  # no embargo anywhere: the repo is a temp folder, no project
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"),
                  mock.patch.object(core, "HANDOFF", root / "handoff"),
                  mock.patch.object(core, "NOTES", root / "handoff" / "projects"),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json"),
                  mock.patch.dict(core.CFG, {"audit": json.loads(json.dumps(core.DEFAULTS.get("audit") or {}))}),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("DEVKIT_FROM_AGENT", "DEVKIT_SESSION", "DEVKIT_AGENT"):
            os.environ.pop(k, None)

    def member(self, cmd, routed=False):
        with settings():
            v = guard.crew_shell_verdict(cmd, self.repo, None, "redteamprobe", level="all", lane="a1", session="s1",
                                         grant=DenyAll(), **({"routed": True} if routed else {}))
        return v[1] if v else None

    def main(self, cmd):
        with settings():
            v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1", grant=DenyAll())
        return v[1] if v else None

    def check(self, cmd, tool="Bash", payload=None):
        with settings():
            v = guard.check(tool, payload or {"command": cmd}, self.repo, "claude")
        return v[1] if v else None

    def acts(self, cmd):
        return [a["action"] for a in guard.irreversible_actions(cmd, self.repo) or []]

    # ---- RT8
    def test_rt8_npx_launches_a_guardless_nested_agent(self):
        # the RT6 refusal named only the binaries claude and codex; a package runner starts the same session from
        # its npm package, so the package's binary is what counts
        self.assertIsNotNone(self.check("claude --bare -p hi"))   # baseline: the plain form is caught
        self.assertIsNotNone(self.member("claude --bare -p hi"))
        for cmd in ("npx @anthropic-ai/claude-code --bare -p hi", "npx claude-code --bare -p hi",
                    "bunx claude-code --dangerously-skip-permissions -p hi",
                    "pnpm dlx @anthropic-ai/claude-code --bare -p hi",
                    "yarn dlx @anthropic-ai/claude-code@latest --bare -p hi",
                    "npx -y @openai/codex --sandbox danger-full-access exec hi"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
                self.assertIsNotNone(self.member(cmd))

    def test_rt8_a_package_runner_with_the_developers_settings_still_runs(self):
        for cmd in ("npx @anthropic-ai/claude-code -p hi", "npx prettier --check .", "bunx claude-coder --bare",
                    "npx @evil/claude-code --version"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))
        self.assertEqual(guard.agent_cli("@anthropic-ai/claude-code@1.2.3"), "claude")
        self.assertEqual(guard.agent_cli("/usr/local/bin/codex"), "codex")
        self.assertEqual(guard.agent_cli("@evil/claude-code"), "")

    # ---- RT14
    def test_rt14_codex_profile_can_disable_the_sandbox_and_hooks(self):
        # a profile in config.toml carries the sandbox_mode, approval_policy and hooks that -c is refused for
        self.assertIsNotNone(self.check("codex -c sandbox_mode=danger-full-access exec hi"))  # baseline
        for cmd in ("codex -p evil exec hi", "codex --profile evil exec hi", "codex --profile=evil exec hi",
                    "codex exec -p evil hi"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
                self.assertIsNotNone(self.member(cmd))
        for cmd in ("codex exec hi", "claude -p hi"):  # claude's -p is --print, not a profile
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))

    # ---- RT13
    def test_rt13_a_variable_as_the_git_subcommand_hides_the_action(self):
        # c43b30e made `gh $SUB merge` the unknown action (tier 2); git's subcommand has the same hole
        self.assertEqual(self.acts("git push origin main"), ["push-destructive"])  # baseline
        self.assertIsNotNone(self.member("git push origin main"))
        for cmd in ("git $SUB push", "git ${S} push origin main", "s=push; git $s origin main",
                    "git -C . $(echo push) origin main"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), ["unknown"])
                self.assertIsNotNone(self.member(cmd))
                self.assertIsNotNone(self.main(cmd))
        for cmd in ("git log $REV", "git show ${SHA}", "git status"):  # a variable argument is not the subcommand
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])

    # ---- RT12
    def test_rt12_interpreter_spawn_evades_session_and_hook_refusals(self):
        # guardless_session and the dev-hook refusal judged shell argv only; the same commands handed to a process
        # from interpreter code are judged the same way
        self.assertIsNotNone(self.check("python3 bin/dev hook prompt"))  # baseline: the shell form is caught
        self.assertIsNotNone(self.check("claude --bare -p hi"))
        for cmd in ("python3 -c \"import subprocess;subprocess.run(['claude','--bare','-p','hi'])\"",
                    "python3 -c \"import os;os.system('claude --bare -p hi')\"",
                    "python3 -c \"import os;os.execvp('npx',['npx','claude-code','--bare'])\"",
                    "python3 -c \"import subprocess;subprocess.run(['dev','hook','prompt'])\"",
                    "node -e \"require('child_process').spawn('dev',['hook','prompt'])\"",
                    "node -e \"require('child_process').execSync('bash -c \\\"claude --bare -p hi\\\"')\"",
                    "python3 -c \"import os,subprocess;subprocess.run(['claude','-p','hi'],"
                    "env={**os.environ,'CLAUDE_CONFIG_DIR':'/tmp/c'})\""):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
                self.assertIsNotNone(self.member(cmd))

    def test_rt12_interpreter_code_that_starts_an_ordinary_session_or_names_the_flags_runs(self):
        for cmd in ("python3 -c \"import subprocess;subprocess.run(['claude','-p','hi'])\"",
                    "python3 -c \"print('claude --bare is refused by the guard')\"",
                    "node -e \"require('child_process').spawn('dev',['status'])\""):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))
                self.assertIsNone(self.member(cmd))

    # ---- RT9
    def test_rt9_command_executing_git_config_keys_are_refused(self):
        # besides the pager, git config keys whose value is a command line (or a file git runs or reads more config
        # from) run under later git calls the guard reads as benign
        self.assertIsNotNone(self.check("git config core.pager 'gh pr merge 12'"))  # baseline: pager is refused
        for cmd in ("git config core.fsmonitor 'gh pr merge 12'", "git config core.sshCommand 'gh pr merge 12'",
                    "git config core.editor 'gh pr merge 12'", "git config core.hooksPath /tmp/h",
                    "git config include.path /tmp/evil", "git config --global includeIf.gitdir:~/.path /tmp/evil",
                    "git config set sequence.editor x", "git -C . config --local Core.FSMonitor x",
                    "git config core.pager 'gh pr merge 12'"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
                self.assertIsNotNone(self.member(cmd))
        for cmd in ("git config user.name t", "git config --get core.editor", "git config core.autocrlf false",
                    "git config --unset core.hooksPath"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))
                self.assertIsNone(self.member(cmd))

    # ---- RT10
    def test_rt10_git_config_global_points_git_at_an_attacker_config(self):
        # GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM replace a whole config file and GIT_CONFIG_PARAMETERS / _COUNT add
        # -c pairs, any of which can set a pager or fsmonitor: the git call is unknown for the main session (tier 2)
        # and a crew member may not set them. (The part-2 probe asserted guard.check for the main session; the
        # irreversible gate is the main session's check for a command a pager runs, as for -c below.)
        ev = Path(self.tmp.name) / "gc"
        ev.write_text("[core]\n\tpager = gh pr merge 12\n")
        self.assertIsNotNone(self.main("git -c core.pager='gh pr merge 12' -p log"))  # baseline: -c is caught
        for cmd in (f"GIT_CONFIG_GLOBAL={ev} git -p log", f"GIT_CONFIG_SYSTEM={ev} git -p log",
                    f"export GIT_CONFIG_GLOBAL={ev}; git status", "GIT_CONFIG_PARAMETERS=\"'core.fsmonitor'='x'\" "
                    "git status", "GIT_CONFIG_COUNT=1 git status", "git -c core.hooksPath=/tmp/h commit -m x"):
            with self.subTest(cmd=cmd):
                self.assertIn("unknown", self.acts(cmd))
                self.assertIsNotNone(self.main(cmd))
                self.assertIsNotNone(self.member(cmd))
        self.assertEqual(self.acts("git -c core.sshCommand='gh pr merge 12' fetch"), ["merge"])
        self.assertIsNotNone(self.member(f"GIT_CONFIG_GLOBAL={ev} make"))  # a member sets them for no command
        for cmd in ("GIT_CONFIG_GLOBAL=/dev/null git status", "git -c color.ui=always log", "git status"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])
                self.assertIsNone(self.member(cmd))

    # ---- RT11
    def test_rt11_dot_git_is_writable_through_the_file_tools(self):
        # the file tools refused devkit's state, not a repository's own .git: a pager in .git/config or a hook in
        # .git/hooks runs under the next ordinary git call
        for path, payload in (
                (self.repo + "/.git/config", {"content": "[core]\n\tpager = gh pr merge 12\n"}),
                (self.repo + "/.git/hooks/pre-commit", {"content": "#!/bin/sh\ngh pr merge 12\n"}),
                (self.repo + "/.GIT/Config", {"content": "[core]\n\tfsmonitor = x\n"}),
                (self.repo + "/.git/info/attributes", {"content": "* filter=x\n"}),
                (self.repo + "/.git/modules/sub/config", {"content": "[core]\n\tfsmonitor = x\n"}),
                (self.repo + "/.git/worktrees/w/config.worktree", {"content": "[core]\n\tfsmonitor = x\n"}),
                (str(core.HOME / ".gitconfig"), {"content": "[core]\n\tpager = x\n"})):
            with self.subTest(path=path):
                self.assertIsNotNone(self.check(None, "Write", {"file_path": path, **payload}))
        for cmd in ("echo '[core] fsmonitor = x' >> .git/config", "printf x > .git/hooks/pre-commit",
                    "cp /tmp/x .git/hooks/pre-commit", "ln -s /tmp/x .git/hooks/post-checkout"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
        patch = "*** Begin Patch\n*** Add File: .git/hooks/pre-push\n+gh pr merge 12\n*** End Patch"
        self.assertIsNotNone(self.check(None, "apply_patch", {"command": patch}))

    def test_rt11_reads_and_the_work_tree_stay_open(self):
        self.assertIsNone(self.check(None, "Read", {"file_path": self.repo + "/.git/config"}))
        self.assertIsNone(self.check(None, "Write", {"file_path": self.repo + "/.gitignore", "content": "x\n"}))
        self.assertIsNone(self.check(None, "Write", {"file_path": self.repo + "/src/config", "content": "x\n"}))
        for cmd in ("cat .git/config", "rm -f .git/index.lock", "git status", "echo x >> .gitignore",
                    "rm -rf /tmp/clone-to-inspect/.git"):  # deleting a git folder runs nothing
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))

    # ---- B-fy5f5i8c1hx
    BASH_HEREDOC = "printf '[%s]' \"$(cat <<'EOF'\nx\nEOF)\"; echo RAN\nEOF\n)\""

    def test_heredoc_a_tag_line_with_more_than_the_tag_is_not_one_command(self):
        # macOS /bin/bash 3.2 ends the heredoc at `EOF)` and runs what follows; zsh reads on. The lane router read
        # zsh's way and routed the whole call as one command: now it fails closed, and the classifier sees what bash
        # runs
        self.assertFalse(guard.runs_one_command(self.BASH_HEREDOC))
        self.assertFalse(guard.runs_one_command("dev crew ask \"$(cat <<'EOF'\nx\nEOF )\ngh pr merge 12\nEOF\n)\""))
        ask = "dev crew ask \"$(cat <<'EOF'\nx\nEOF)\"; gh pr merge 12\nEOF\n)\""
        self.assertFalse(guard.runs_one_command(ask))
        self.assertIsNotNone(self.member(ask))
        merge = ask.replace("dev crew ask", "printf %s")
        self.assertIsNotNone(self.main(merge))
        self.assertIsNotNone(self.member(merge))
        msg = "dev crew ask \"$(cat <<'EOF'\nShould I merge 12; or wait && ask | Link?\nEOFX is a word\nEOF\n)\""
        self.assertFalse(guard.runs_one_command(msg))  # a body line that starts with the tag fails closed too
        self.assertTrue(guard.runs_one_command("dev crew ask \"$(cat <<'EOF'\nShould I merge 12; or wait?\nEOF\n)\""))

    @unittest.skipUnless(os.path.exists("/bin/bash") and "version 3." in subprocess.run(
        ["/bin/bash", "--version"], capture_output=True, text=True).stdout, "needs bash 3.2 (macOS /bin/bash)")
    def test_heredoc_bash_3_2_runs_what_follows_the_tag_line(self):
        out = subprocess.run(["/bin/bash", "-c", self.BASH_HEREDOC], capture_output=True, text=True).stdout
        self.assertEqual(out, "[x]RAN\n")  # the echo ran as a command of its own

    @unittest.skipUnless(shutil.which("zsh", path="/bin"), "needs /bin/zsh")
    def test_heredoc_zsh_reads_the_same_line_as_data(self):
        out = subprocess.run(["/bin/zsh", "-c", self.BASH_HEREDOC], capture_output=True, text=True).stdout
        self.assertEqual(out, "[x\nEOF)\"; echo RAN]")  # one printf: the echo is text

    def test_heredoc_a_routed_lane_call_is_still_judged(self):
        # defense in depth for the router: a routed call passes its one lane call and nothing else
        self.assertIsNone(self.member("dev crew ask \"$(cat <<'EOF'\nShould I merge 12?\nEOF\n)\"", routed=True))
        self.assertIsNone(self.member("dev progress step \"x\" --check \"y\"", routed=True))
        for cmd in ("dev progress step x && gh pr merge 12", "dev progress step x; dev crew ask y",
                    "dev crew add-member x"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.member(cmd, routed=True))

    # ---- MCP (Seraph)
    def test_mcp_a_raise_in_the_tool_gate_fails_closed(self):
        # the tool's name was passed as the text to judge, and a name never reads as an irreversible command, so a
        # raise inside the gate let every MCP call through while the gate enforces
        with settings(), mock.patch.object(guard, "mcp_external", side_effect=RuntimeError("boom")):
            v = guard.irreversible_tool_verdict("mcp__github__merge_pull_request", {}, self.repo, session="s1",
                                                grant=DenyAll())
        self.assertIsNotNone(v)
        self.assertIn("could not check", v[1])
        records = [json.loads(ln) for f in (Path(self.tmp.name) / "audit").rglob("*.jsonl")
                   for ln in f.read_text().splitlines() if ln.strip()]
        self.assertTrue(any(r["action"] == "guard.irreversible" and (r.get("evidence") or {}).get("rule") ==
                            "gate-error" and r["outcome"] == "blocked" for r in records), records)

    # ---- M394 (false unknowns)
    # the shapes of the commands behind 52 of the 71 unknown commands of the gate's first audit week: an edit script
    # whose string literals hold JavaScript (template literals, `${…}`, backticks, `exec(`), a test runner by a
    # variable folder, a probe's f-string
    EDIT_SCRIPT = ("python3 -I - <<'EOF'\nfrom pathlib import Path\np=Path(\"views.js\"); s=p.read_text()\n"
                   "a='''  tr(`${done} of ${rows.length} handed over`, `${v.name}`), [shownPhase.id]}\n"
                   "  const t = setTimeout(go, 5 * 60_000); el.exec(x); eval(y)'''\n"
                   "b=a.replace('handed over', 'handed off')\nassert s.count(a) == 1\np.write_text(s.replace(a, b))\n"
                   "EOF")

    def test_m394_text_inside_interpreter_code_is_not_a_program_name(self):
        self.assertEqual(self.acts(self.EDIT_SCRIPT), [])
        self.assertIsNone(self.main(self.EDIT_SCRIPT))
        probe = "python3 -c \"import subprocess; cmd='git log'; print(f'{cmd}: [HELD] {cmd}'); subprocess.run(['ls'])\""
        self.assertEqual(self.acts(probe), [])

    def test_m394_a_known_program_in_a_variable_folder_is_that_program(self):
        for cmd in ("$VENV/bin/python -m pytest -q", "\"$VIRTUAL_ENV/bin/python\" x.py", "${VENV}/bin/pytest -q",
                    "$HOME/.local/bin/ruff check ."):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [])
                self.assertIsNone(self.main(cmd))
        # and it is read like that program: its code still counts
        self.assertEqual(self.acts("$VENV/bin/python -c \"import os; os.system('gh pr merge 12')\""), ["merge"])

    def test_m394_names_the_program_or_shell_builds_at_run_time_stay_unknown(self):
        for cmd in ("$X pr merge 12", "$VENV/bin/gh pr merge 12", "VENV='gh pr merge 12 #'; $VENV/bin/python x",
                    "IFS=/; $VENV/bin/python x", "for VENV in a b; do $VENV/bin/python x; done",
                    "${VENV:=/tmp}/bin/python x",
                    "python3 -c \"getattr(__import__('os'),'sys'+'tem')('gh pr merge 12')\"",
                    "python3 -c \"import os; os.system('bash -c \\\"$X pr merge 12\\\"')\"",
                    "python3 -c \"import subprocess; subprocess.run(f'{tool} pr merge 12', shell=True)\"",
                    "node -e \"require('child_process').execSync(`${t} pr merge 12`)\""):
            with self.subTest(cmd=cmd):
                self.assertIn("unknown", self.acts(cmd))
                self.assertIsNotNone(self.main(cmd))
                self.assertIsNotNone(self.member(cmd))
        # a program named in a string is still classified
        self.assertEqual(self.acts("python3 -c \"import os; os.system('gh pr merge 12')\""), ["merge"])
        self.assertEqual(self.acts("node -e \"require('child_process').execSync('gh pr merge 12')\""), ["merge"])

    # ---- M379: the role check sends back a member's report that names Morpheus alone, and members echo the
    # guard's refusals, so every text the guard shows names the role
    # ---- the security engineer's finding during B2a (High): an open() whose path is built by two nested calls ended
    # before _CODE_WRITES saw its mode, so a program could write devkit's state (the grant ledger included); a path from
    # a call counts like a literal (L-fy1334af1q0)
    def test_a_state_path_built_by_nested_calls_is_still_a_write_to_state(self):
        built = "os.path.join(os.path.expanduser('~'), '.local', 'state', 'devkit', 'runs', 'x.json')"
        for code in (f"import os; open({built}, 'w').write('')", f"import os; open({built}, mode='a')",
                     f"import os, pathlib; f = open({built}, 'w+'); f.write('x')"):
            with self.subTest(code=code):
                self.assertIn("devkit's state", self.check(f'python3 -c "{code}"') or "")
        for code in (f"import os; print(open({built}).read())",  # reading it is fine
                     "import os; open(os.path.join(os.path.expanduser('~'), 'notes', 'x.md'), 'w').write('')"):
            with self.subTest(code=code):
                self.assertIsNone(self.check(f'python3 -c "{code}"'))

    # ---- the software architect's finding (architecture review 26.11, confirmed by the security engineer): when the
    # roster cannot be read, a crew member's lane kept the level it was given, while docs/guard.md says the lane check
    # fails closed. It now works at the strictest of that level and review.
    def test_a_roster_devkit_cannot_read_makes_the_lane_read_only(self):
        cmd = "python3 -c \"print(1)\""
        self.assertIsNone(self.member(cmd))  # baseline: an `all` lane runs it
        with mock.patch.object(guard, "member_level", side_effect=RuntimeError("crew.json unreadable")):
            self.assertIn("read-only lane", self.member(cmd) or "")
            self.assertIsNone(self.member("git status"))  # reading still works

    def test_m379_no_guard_text_names_the_tech_lead_by_persona_alone(self):
        import ast
        tree = ast.parse((ROOT / "lib" / "devkit" / "guard.py").read_text())
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        texts = [(n.lineno, n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant)
                 and isinstance(n.value, str) and id(n) not in docs and "Morpheus" in n.value]
        self.assertEqual(texts, [])

    def test_m379_member_refusals_name_the_tech_lead(self):
        self.assertIn("the tech lead runs it", self.member('dev crew brief neo "add a column"') or "")
        self.assertIn("Ask the tech lead", self.member("dev push") or "")
        self.assertIn('"Needs from the tech lead"', self.member("gh pr merge 12") or "")

    # ---- M381: dev backup and a sync that backs up branches push them, so a member needs a step grant (tier 1);
    # the main session runs them, and the scheduled sync runs from launchd, outside the guard
    def targets(self, cmd):
        return [(a["action"], a["target"]) for a in guard.irreversible_actions(cmd, self.repo) or []]

    def assert_tier_1(self, cmd):
        self.assertIn("step grant", self.member(cmd) or "", cmd)
        self.assertIsNone(self.main(cmd), cmd)

    def test_m381_dev_backup_is_a_tier_1_push_per_branch(self):
        self.assertEqual(self.targets("dev backup"), [("push", "feat/x")])  # the current branch
        self.assertEqual(self.targets("dev backup feat/a feat/b"), [("push", "feat/a"), ("push", "feat/b")])
        self.assertEqual(self.targets("bin/dev backup --all --accept-ci"), [("push", "all-branches")])
        for cmd in ("dev backup", "dev backup feat/a", "dev backup --all", "python3 bin/dev backup --accept-ci feat/a"):
            with self.subTest(cmd=cmd):
                self.assert_tier_1(cmd)
        self.assertEqual(self.acts("dev backup --dry-run --all"), [])
        self.assertIsNone(self.member("dev backup --dry-run"))

    def test_m381_a_sync_that_backs_up_branches_is_a_tier_1_push(self):
        for cmd in ("dev sync --scheduled --quiet", "dev sync --session claude:s1 --finish"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), ["push"])
                self.assert_tier_1(cmd)
        for cmd in ("dev sync --scheduled --offline", "dev sync --finish --no-push", "dev sync --quiet --offline"):
            with self.subTest(cmd=cmd):  # no backup runs: cmd_sync backs up only scheduled or finishing, online
                self.assertEqual(self.acts(cmd), [])

    def test_m381_a_job_is_graded_by_its_own_dev_command(self):
        from devkit import jobs
        for cmd in ("dev jobs run sync", "dev jobs exec sync"):  # the scheduled sync, typed by hand
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), ["push"])
                self.assert_tier_1(cmd)
        self.assertEqual(self.acts("dev jobs run doctor"), [])
        self.assertEqual(self.acts("dev jobs run no-such-job"), [])
        custom = {"ship": {"cmd": ["release", "--version", "26.11.9"]}, "loop": {"cmd": ["jobs", "run", "loop"]}}
        with mock.patch.object(jobs, "jobs", return_value=custom):
            self.assertEqual(self.targets("dev jobs run ship"), [("release", "v26.11.9")])
            self.assertIn("Crew members never run", self.member("dev jobs run ship") or "")
            self.assertEqual(self.acts("dev jobs run loop"), ["unknown"])
        with mock.patch.object(jobs, "jobs", side_effect=OSError("config unreadable")):
            self.assertEqual(self.acts("dev jobs run sync"), ["unknown"])  # fails closed

    def budget(self, n):
        return [guard.check_push_budget(self.repo, ["origin", "feat/x"], frozenset()) for _ in range(n)]

    def test_m381_the_push_budget_counts_only_pushes_that_start_ci(self):
        from devkit import registry
        state = Path(self.tmp.name) / "state"
        for p in (mock.patch.object(core, "STATE", state), mock.patch.object(registry, "STATE", state)):
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(self.budget(4), [None] * 4)  # no CI configuration: no run, no count
        self.assertFalse((state / "ci-pushes.json").exists())
        wf = Path(self.repo) / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
                                   "      - run: echo hi\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "ci")
        first, second, third = self.budget(3)
        self.assertEqual((first, second), (None, None))
        self.assertIn("push #3", (third or ("", ""))[1])

    def test_m381_a_push_devkit_cannot_judge_counts(self):
        from devkit import pipelines
        state = Path(self.tmp.name) / "state"
        with mock.patch.object(core, "STATE", state), \
                mock.patch.object(pipelines, "push_starts_ci", side_effect=RuntimeError("broken")):
            self.assertIn("push #3", (self.budget(3)[2] or ("", ""))[1])

    # ---- B1 (security review of the pipeline): dev's parser read a unique prefix of a flag as the flag, a word
    # after `--` as an argument, and ran the command after a leading `--` or `-h`, while the guard compared flags as
    # exact strings anywhere in the command. The guard now reads a dev command's words the way the parser does; a
    # prefix of a flag that adds an action still counts as the flag, for a dev whose parser takes abbreviations (an
    # older checkout's bin/dev).
    def reader(self, cmd):
        with settings():
            v = guard.crew_shell_verdict(cmd, self.repo, None, "redteamprobe", level="read", lane="a1", session="s1",
                                         grant=DenyAll())
        return v[1] if v else None

    def test_b1_a_prefix_of_a_flag_that_pushes_is_the_flag(self):
        self.assertEqual(self.acts("dev sync --scheduled"), ["push"])  # baseline: the full flag is caught
        for cmd in ("dev sync --sched", "dev sync --fin", "bin/dev sync --sch --quiet", "dev sync --scheduled --off"):
            with self.subTest(cmd=cmd):  # a prefix of the flag that skips the push (--offline) skips nothing
                self.assertEqual(self.acts(cmd), ["push"])
                self.assert_tier_1(cmd)
        self.assertEqual(self.targets("dev backup --al"), [("push", "all-branches")])
        self.assertEqual(self.targets("dev push --force-w"), [("push-destructive", "feat/x")])
        self.assertEqual(self.acts("dev backup --dry-run --al"), [])  # the exact flag that skips the push still does
        for cmd in ("dev commit --no-v -m x", "dev pr ready --ci-p", "dev pr ready --ci-p=1"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.member(cmd))

    def test_b1_a_flag_after_double_dash_is_an_argument(self):
        self.assertEqual(self.acts("dev backup --dry-run feat/x"), [])  # baseline: a dry run pushes nothing
        for cmd in ("dev backup feat/x -- --dry-run", "dev backup feat/x -- --help", "dev backup feat/x -- -h"):
            with self.subTest(cmd=cmd):  # the parser takes the word after -- as a branch and pushes feat/x
                self.assertIn(("push", "feat/x"), self.targets(cmd))
                self.assert_tier_1(cmd)
        self.assertIn("the tech lead runs it", self.member("dev crew brief neo -- --help") or "")
        self.assertTrue(guard.stubs_accepted("dev commit --wip -m x"))  # baseline
        self.assertFalse(guard.stubs_accepted("dev commit -m x -- --wip"))  # --wip there is a path to commit

    def test_b1_a_leading_double_dash_or_help_does_not_hide_the_command(self):
        self.assertEqual(self.acts("dev release --version 26.11.9"), ["release"])  # baseline
        for cmd, act in (("dev -h release --version 26.11.9", "release"), ("dev --help push", "push"),
                         ("dev --he backup", "push"), ("dev -- -h pr merge 12", "merge")):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), [act])
                self.assertIsNotNone(self.member(cmd))
        for cmd in ("dev -- install", "dev -h update", "dev -- hook prompt", "python3 bin/dev -- config edit"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
        for cmd in ("dev install --help", "dev release -h", "dev push --help"):  # a subcommand's help runs nothing
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.check(cmd))
                self.assertEqual(self.acts(cmd), [])

    def test_b1_a_value_is_read_where_the_parser_reads_it(self):
        self.assertIsNone(self.reader("dev lesson add x"))  # baseline: a subcommand a read-only lane may run
        self.assertIsNone(self.reader("dev task ls"))
        self.assertIsNotNone(self.reader("dev lesson retire L-1 --why x"))
        for cmd in ("dev lesson --search add retire L-1 --why x", "dev task -i ls done 3", "dev delivery -p show apply",
                    "dev audit -o/tmp/trail.json", "dev audit --out=/tmp/trail.json"):
            with self.subTest(cmd=cmd):  # the option takes the next word (or the rest of its own): a write runs
                self.assertIsNotNone(self.reader(cmd))
        # argparse keeps the last of a repeated option: the release it makes is the last version named
        self.assertEqual(self.targets("dev release --version 26.11.4 --version 26.11.9"), [("release", "v26.11.9")])
        with mock.patch.object(guard, "current_project", return_value="acme/widget"):
            for cmd in ("dev task -p acme/widget -p acme/other add x", "python3 bin/dev task -pacme/other add x",
                        "dev -- note -p acme/other set status=blocked", "dev task --proj acme/other add x"):
                with self.subTest(cmd=cmd):
                    self.assertIn("another project", self.check(cmd) or "")
            self.assertIsNone(self.check("dev task -p acme/widget add x"))

    def test_b1_dev_claude_hands_its_words_to_the_session(self):
        self.assertIsNotNone(self.check("claude --bare -p hi"))  # baseline: the plain form is refused
        for cmd in ("dev claude devkit -- --bare -p hi", "dev claude devkit --dangerously-skip-permissions",
                    "dev codex -w feat/x devkit -- --yolo", "dev codex devkit -p evil"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.check(cmd))
                self.assertIsNotNone(self.member(cmd))
        self.assertIsNone(self.check("dev claude devkit -- -p hi"))

    def test_b1_dev_git_skips_the_hooks_as_git_does(self):
        self.assertIsNotNone(self.member("git commit -n -m x"))  # baseline: git's short --no-verify is refused
        for cmd in ("dev git commit -n -m x", "dev git -- commit -n -m x", "dev git commit --no-v -m x"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.member(cmd))
        self.assertIsNone(self.member("dev git commit -m x"))

    # ---- B2 (the release engineer's lines, with the tech lead's refinement): lifting an embargo, publishing a branch
    # that looks like a security fix and setting the backup policy are the developer's own decisions: tier 2, never a
    # crew member's, the main session's only with the developer's typed grant. A dry run writes nothing.
    def test_b2_the_developers_own_publishing_decisions_are_tier_2(self):
        with mock.patch.object(guard, "current_project", return_value="acme/widget"):
            self.assertEqual(self.targets("dev branch embargo feat/x --why 'a token leak'"), [])  # baseline: set one
            for cmd, target in (("dev branch embargo --lift feat/x", "embargo/feat/x"),
                                ("dev branch embargo feat/x --lift", "embargo/feat/x"),
                                ("bin/dev branch embargo --li feat/x", "embargo/feat/x"),
                                ("dev -h branch embargo --lift feat/x", "embargo/feat/x"),
                                ("dev backup policy off", "backup-policy/acme/widget"),
                                ("dev backup policy on -p acme/other", "backup-policy/acme/other")):
                with self.subTest(cmd=cmd):
                    self.assertEqual(self.targets(cmd), [("developer-only", target)])
                    self.assertIn("developer's own decisions", self.main(cmd) or "")
                    self.assertIn("Crew members never run", self.member(cmd) or "")
            for cmd in ("dev backup --public feat/a", "python3 bin/dev backup feat/a --pub"):
                with self.subTest(cmd=cmd):
                    self.assertEqual(self.targets(cmd), [("developer-only", "backup-public/acme/widget"),
                                                         ("push", "feat/a")])
            self.assertIn("`grant embargo feat/x`", self.main("dev branch embargo --lift feat/x"))
            self.assertIn("`grant backup acme/widget`", self.main("dev backup policy off"))
            self.assertNotIn("grant embargo", self.main("dev branch embargo --lift"))  # `?`: no grant covers it
            for cmd in ("dev branch embargo", "dev backup policy", "dev backup --public --dry-run",
                        "dev branch embargo --lift -h"):  # the list, the policy read, a dry run, the help
                with self.subTest(cmd=cmd):
                    self.assertNotIn("developer-only", self.acts(cmd))
            self.assertEqual(self.acts("dev backup feat/a -- --public"), ["push", "push"])  # after --: a branch name

            def grant(action, target, repo, consume):  # the developer typed `grant developer-only embargo/feat/x`
                hit = (action, target) == ("developer-only", "embargo/feat/x")
                return {"id": "E2:t", "source": "E2", "uses_left": 1, "expires": 0} if hit else None
            with settings():
                self.assertIsNone(guard.irreversible_verdict("dev branch embargo --lift feat/x", self.repo,
                                                             agent="claude", session="s1", grant=grant))
                self.assertIsNotNone(guard.irreversible_verdict("dev branch embargo --lift feat/y", self.repo,
                                                                agent="claude", session="s1", grant=grant))

    def test_an_xargs_with_no_command_in_a_programs_code_runs_echo_and_the_rest_of_the_line_is_judged(self):
        # 26.11.4 read argv[0] of an empty list (_dev_args) when a program's code piped into an xargs with no command,
        # and the hook let the whole call run unjudged. Such an xargs runs echo; what follows it is judged as alone
        alone = guard.check_bash("git push --force origin main", self.repo, "claude")
        self.assertEqual((alone or ("",))[0], "deny")
        install = guard.deny(guard.INSTALL_REASON)
        for xargs in ("xargs", "xargs -0", "xargs -n 1", "xargs --"):
            for code in (f"python3 -c \"import os; os.system('echo a | {xargs}')\"",
                         f"node -e \"require('child_process').execSync('echo a | {xargs}')\"",
                         f"perl -e 'system(\"echo a | {xargs}\")'"):
                with self.subTest(code=code):
                    self.assertIsNone(guard.check_bash(code, self.repo, "claude"))
                    self.assertEqual(guard.check_bash(f"{code}; git push --force origin main", self.repo, "claude"),
                                     alone)
                    self.assertEqual(guard.check_bash(f"{code}; dev install", self.repo, "claude"), install)
        dev = "python3 -c \"import os; os.system('echo install | xargs dev')\""  # an xargs with a command is read
        self.assertEqual(guard.check_bash(dev, self.repo, "claude"), install)

    def test_an_xargs_of_wrappers_alone_runs_its_input_as_the_command_and_is_refused(self):
        # `xargs env|nohup|sudo|timeout 5` strip to no command, so the
        # guard read them as echo, while xargs adds what it reads and its first word runs: `echo dev install | xargs
        # env` ran dev install, and `echo git push --force origin main | xargs env` a force push, past every rule
        refused = guard.deny(guard.INPUT_COMMAND_REASON)
        for xargs in ("xargs env", "xargs nohup", "xargs sudo", "xargs timeout 5", "xargs -n 2 env FOO=1",
                      "xargs -- nice -n 5", "xargs env -S", "xargs uv run"):
            for cmd in (f"echo dev install | {xargs}", f"echo git push --force origin main | {xargs}",
                        f"python3 -c \"import os; os.system('echo dev install | {xargs}')\"",
                        f"node -e \"require('child_process').execSync('echo dev install | {xargs}')\""):
                with self.subTest(cmd=cmd):
                    self.assertEqual(guard.check_bash(cmd, self.repo, "claude"), refused)
            with self.subTest(gate=xargs):  # the irreversible gate cannot read it either: it judges the text
                self.assertIsNone(guard.irreversible_actions(f"echo git push --force origin main | {xargs}",
                                                             str(self.repo)))
                self.assertIn("input names", guard.readonly_refusal(f"echo a | {xargs}", str(self.repo)) or "")
        for cmd in ("echo a | xargs", "echo a | xargs -0", "echo a | xargs -n 1", "echo a | xargs --",
                    "echo a | xargs echo", "echo a | xargs -n 1 sh -c 'echo $0'", "find . -exec env \\;"):
            with self.subTest(open=cmd):  # no command (echo), or a command that is named
                self.assertIsNone(guard.check_bash(cmd, self.repo, "claude"))
        self.assertEqual(guard.irreversible_actions("echo a | xargs", str(self.repo)), [])

    def test_a_runner_placeholder_or_a_shell_line_in_the_commands_place_is_refused(self):
        # the same shape (the program comes from the input) through xargs' replace string, GNU parallel, find -exec
        # and a shell line the input fills in
        refused = guard.deny(guard.INPUT_COMMAND_REASON)
        for cmd in ("echo dev | xargs -I{} {} install", "echo dev | xargs -I% env % install",
                    "echo dev | xargs --replace env {} install", "echo dev install | xargs sh -c",
                    "echo dev install | xargs -0 sh -c", "echo dev install | xargs sh -c '\"$@\"' sh",
                    "echo dev install | xargs -I{} sh -c {}", "echo dev install | xargs eval",
                    "echo dev install | parallel env", "echo dev | parallel {} install", "echo a | parallel dev",
                    "echo a | xargs -I{} dev {}", "find . -name dev -exec {} install \\;",
                    "find . -name dev -execdir env {} install \\;", "find . -exec timeout 5 {} \\;",
                    "find . -exec dev {} \\;"):
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.check_bash(cmd, self.repo, "claude"), refused)
        self.assertEqual(guard.check_bash("python3 -c \"import os; os.system('echo a | parallel env')\"",
                                          self.repo, "claude"), refused)

    def test_a_script_the_gate_cannot_read_is_an_unknown_action(self):
        # a script padded past the read cap read as empty, so `python3 big.py`
        # ending in a force push passed the gate; such a program is an unknown action (tier 2), refused in enforce
        push = "git push --force origin main"
        root = Path(core.real(self.tmp.name))
        files = {"big.py": ("python3 {p}", "#" * (1 << 19) + f"\nimport os\nos.system('{push}')\n"),
                 "big.js": ("node {p}", "//" + "x" * (1 << 19) + f"\nrequire('child_process').execSync('{push}');\n"),
                 "big.sh": ("{p}", "#!/bin/sh\n" + "# pad\n" * 45000 + f"{push}\n"),
                 "big-by-sh.sh": ("sh {p}", "# pad\n" * 90000 + f"{push}\n"),
                 "big-sourced.sh": (". {p}", "# pad\n" * 90000 + f"{push}\n")}
        for name, (run, text) in files.items():
            p = root / name
            p.write_text(text)
            p.chmod(0o755)
            cmd = run.format(p=p)
            with self.subTest(cmd=name):
                acts = guard.irreversible_actions(cmd, str(self.repo))
                self.assertIn(("unknown", 2, ("unread", str(p))),
                              [(a["action"], a["tier"], tuple(a.get("why") or ())) for a in acts])
                with settings():
                    v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1", grant=DenyAll())
                self.assertEqual((v or ("",))[0], "deny")
                self.assertIn("larger than the guard reads", v[1])
        small = root / "small.py"
        small.write_text(f"import os\nos.system('{push}')\n")
        self.assertIn("push-destructive", [a["action"] for a in guard.irreversible_actions(f"python3 {small}",
                                                                                          str(self.repo))])
        binary = root / "tool"  # a large program that is no shell script runs as before: its name is what counts
        binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * (1 << 19))
        binary.chmod(0o755)
        self.assertNotIn("unknown", [a["action"] for a in guard.irreversible_actions(str(binary), str(self.repo))])

    def test_an_xargs_option_value_is_never_read_as_its_command(self):
        # `xargs -tn 1 dev` (a cluster) and GNU's `--max-args 1 dev` ran `dev <input>` while the guard read `1` as
        # the command; xargs' own parser takes the value
        install = guard.deny(guard.INSTALL_REASON)
        for xargs in ("xargs -tn 1 dev", "xargs --max-args 1 dev", "xargs --max-a 1 dev", "xargs -n1 dev",
                      "xargs -L 1 -P 2 dev", "xargs -0 -- dev"):
            with self.subTest(xargs=xargs):
                self.assertEqual(guard.check_bash(f"echo install | {xargs}", self.repo, "claude"), install)
        self.assertEqual(guard._xargs_parts(["-tn", "1", "-I", "%", "git", "rm", "%"]), (["git", "rm", "%"], "%"))
        self.assertEqual(guard._xargs_command(["-i", "git", "rm", "{}"]), ["git", "rm", "{}"])
        self.assertEqual(guard._xargs_parts(["--replace=X", "--max-args=2", "cp", "X", "d"]), (["cp", "X", "d"], "X"))

    def test_parallels_own_replacement_strings_count_as_its_placeholder(self):
        # `parallel -I % % install` ran the program its input named while the guard read `%` as the
        # program; every string parallel replaces with its input, and any {…} in its command, is the placeholder
        refused = guard.deny(guard.INPUT_COMMAND_REASON)
        for cmd in ("echo dev | parallel -I % % install", "echo gh | parallel -I % % pr merge 12",
                    "echo dev | parallel -I% % install", "echo dev | parallel -kI @@ @@ install",
                    "echo dev | parallel --replace=% % install", "echo dev | parallel --repl % % install",
                    "echo dev.x | parallel --er @ @ install", "echo dev | parallel --bnr @ @ install",
                    "echo dev | parallel --dnr @ @ install", "echo dev | parallel --rpl '%% s/x//' %% install",
                    "parallel {..} install ::: dev.x.y", "parallel --plus {+.} install ::: dev.x",
                    "parallel '{} install' ::: dev", "sem -I % % install"):
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.check_bash(cmd, self.repo, "claude"), refused)
        self.assertIn("unknown", self.acts("parallel -I@@ git @@ --force origin main ::: push"))

    def test_parallels_command_in_one_quoted_word_is_a_shell_line(self):
        # `parallel 'gzip -9 {}' ::: a b` was refused, as if `gzip -9 {}` were a program the input names
        for cmd in ("parallel 'gzip -9 {}' ::: a.log b.log", "parallel -j4 'wc -l {}' ::: a b", "parallel echo ::: a b",
                    "parallel -l git status ::: x"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(guard.check_bash(cmd, self.repo, "claude"))
        self.assertEqual(self.acts("parallel 'git push --force origin {}' ::: main"), ["unknown"])  # read as sh -c
        self.assertEqual(self.acts("sem -j1 git push"), ["push"])  # sem reads no input: its command as written

    def test_a_shell_or_interpreter_whose_program_comes_from_the_input_is_refused(self):
        # `xargs -0 python3 -c` and `xargs sh` with `-c 'dev install'` as input ran code the input carried, and
        # `xargs sh`, `find -exec sh {}` and `parallel python3` a script the input names
        refused = guard.deny(guard.INPUT_COMMAND_REASON)
        for cmd in ("printf x | xargs -0 python3 -c", "echo \"-c 'dev install'\" | xargs sh", "echo x | xargs sh",
                    "echo x | xargs -I{} sh {}", "echo x | xargs bash", "echo x | xargs -n1 sh", "echo x | xargs python3",
                    "echo x | xargs node", "echo x | xargs -I{} python3 {}", "echo x | xargs -I{} node",
                    "echo x | xargs node -r", "echo x | xargs perl -e", "echo x | xargs php -r", "echo x | xargs sh -s",
                    "find . -name x -exec sh {} \\;", "find . -name x -execdir sh {} +",
                    "find . -name x -exec sh -c '. \"$1\"' sh {} \\;", "parallel python3 ::: a.py",
                    "parallel --pipe -j1 sh", "python3 -c \"import os; os.system('echo x | xargs sh')\""):
            with self.subTest(cmd=cmd):
                self.assertEqual(guard.check_bash(cmd, self.repo, "claude"), refused)
        for cmd in ("echo a | xargs python3 tool.py", "echo a | xargs -n1 python3 -m py_compile",
                    "echo a | xargs awk '{print $1}'", "echo a | xargs -n 1 sh -c 'echo $0'",
                    "find . -name '*.py' -exec python3 -m py_compile {} +", "echo a | xargs perl -pi -e 's/a/b/'",
                    "echo a | xargs node -e 'console.log(1)'", "echo a | xargs python3 -W ignore tool.py",
                    "echo a | xargs python3 -I tool.py", "echo a | xargs python3 -Bc 'print(1)'",
                    "python3 -c \"import json; json.dump({}, open('x.json', 'w'))\"", "node -e 'f({})'"):
            with self.subTest(open=cmd):
                self.assertIsNone(guard.check_bash(cmd, self.repo, "claude"))

    def test_a_shell_or_interpreter_reads_its_program_from_a_redirect_or_a_substitution(self):
        # `cat f | sh` was read; `sh < f`, `< f sh`, `sh 0< f`, `sh <(cat f)` and `python3 < f` were not
        root = Path(core.real(self.tmp.name))
        push = "git push --force origin main"
        (root / "cmds.txt").write_text(push + "\n")
        (root / "prog.py").write_text(f"import os\nos.system('{push}')\n")
        (root / "prog.js").write_text(f"require('child_process').execSync('{push}');\n")
        (root / "big.txt").write_text("# pad\n" * 90000 + push + "\n")
        c, py, js, big = root / "cmds.txt", root / "prog.py", root / "prog.js", root / "big.txt"
        for cmd in (f"sh < {c}", f"bash < {c}", f"zsh 0< {c}", f"< {c} sh", f"sh <{c}", f"env sh < {c}",
                    f"exec sh < {c}", f"sh -s x < {c}", f"sh <(cat {c})", f"sh /dev/stdin < {c}", f"sh <> {c}",
                    f"python3 < {py}", f"python3 - < {py}", f"python3 /dev/stdin < {py}", f"cat {py} | python3",
                    f"node < {js}", f"python3 <(cat {py})"):
            with self.subTest(cmd=cmd):
                self.assertIn("push-destructive", self.acts(cmd))
                self.assertIsNotNone(self.main(cmd))
        for cmd in (f"sh < {big}", f"cat {big} | sh"):  # too large to read: an unknown action
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), ["unknown"])
        for cmd in (f"sh 2< {c}", f"sh -c 'echo hi' < {c}", f"python3 -c 'print(1)' < {py}", f"wc -l < {c}"):
            with self.subTest(open=cmd):  # the program is in the words: what it reads is data
                self.assertEqual(self.acts(cmd), [])

    def test_a_subcommand_the_input_fills_in_is_an_unknown_action(self):
        # `echo publish | xargs npm` published with no grant: the gate read `npm {}` as no action
        for cmd in ("echo publish | xargs npm", "echo publish | xargs cargo", "echo push x/y | xargs docker",
                    "echo upload dist/x | xargs twine", "echo publish | xargs pnpm", "echo publish | xargs yarn npm",
                    "echo apply -auto-approve | xargs terraform", "echo push | xargs gem",
                    "echo publish | xargs -I{} npm {}", "parallel npm ::: publish", "find . -exec npm {} \\;",
                    "echo deploy | xargs -n1 make", "echo --force origin main | xargs git push",
                    "echo origin +main | xargs git push"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.acts(cmd), ["unknown"])
                self.assertIsNotNone(self.main(cmd))
        for cmd in ("echo a.py | xargs git add", "ls | xargs -n 1 wc -l", "echo a | xargs npm run lint --"):
            with self.subTest(open=cmd):
                self.assertNotIn("unknown", self.acts(cmd))

    def test_a_program_run_by_its_path_is_read_as_the_system_runs_it(self):
        # `./big.py` with a python shebang, a script with no shebang (the shell runs it as sh) and one with a
        # byte-order mark before `#!` passed the gate unread
        push = "git push --force origin main"
        root = Path(core.real(self.tmp.name))
        files = {"py_small": (f"#!/usr/bin/env python3\nimport os\nos.system('{push}')\n", "push-destructive"),
                 "py_big": (f"#!/usr/bin/env python3\n{'#' * (1 << 19)}\nimport os\nos.system('{push}')\n", "unknown"),
                 "node_big": (f"#!/usr/bin/env node\n//{'x' * (1 << 19)}\nrequire('child_process').execSync('{push}');"
                              "\n", "unknown"),
                 "plain_small": (push + "\n", "push-destructive"),
                 "plain_big": ("# pad\n" * 45000 + push + "\n", "unknown"),
                 "bom_big": ("﻿#!/bin/sh\n" + "# pad\n" * 45000 + push + "\n", "unknown")}
        for name, (text, action) in files.items():
            p = root / name
            p.write_text(text)
            p.chmod(0o755)
            with self.subTest(name=name):
                self.assertIn(action, self.acts(str(p)))
                self.assertIn(action, self.acts(f"cd {root} && ./{name}"))
        notes = root / "notes.txt"  # a text file no one can run is no script, unless the command makes it runnable
        notes.write_text(push + "\n")
        self.assertEqual(self.acts(str(notes)), [])
        self.assertIn("push-destructive", self.acts(f"chmod +x {notes} && {notes}"))

    def test_xargs_rm_is_refused_in_a_temp_folder_too(self):
        # from a temp folder `echo ~/Development | xargs rm -rf` deleted the dev root, which `rm -rf` names refuse:
        # xargs takes what it deletes from its input, so the folder it runs in says nothing about it
        for cmd in (f"echo {core.DEV} | xargs rm -rf", f"echo -rf {core.HOME} | xargs rm", "xargs -0 rm < list",
                    "echo a | xargs unlink"):
            with self.subTest(cmd=cmd):
                v = guard.check_bash(cmd, self.tmp.name, "claude")
                self.assertEqual((v or ("",))[0], "deny", v)
                self.assertIn("xargs rm", v[1])
        self.assertIsNone(guard.check_bash("DEVKIT_RAW_FS=1 xargs rm < list", self.tmp.name, "claude"))
        self.assertIsNone(guard.check_bash("rm -f a.log", self.tmp.name, "claude"))  # a named file stays open

    def test_a_large_script_inside_the_workspace_is_the_repositorys_own(self):
        # autotools' ./configure, a #!/bin/sh script of several hundred KB in the repository, was an unknown action
        core.DEV.mkdir(parents=True, exist_ok=True)
        ws = tempfile.TemporaryDirectory(dir=core.DEV)
        self.addCleanup(ws.cleanup)
        configure = Path(core.real(ws.name)) / "configure"
        configure.write_text("#!/bin/sh\n" + "# generated by autoconf\n" * 13000 + "echo done\n")
        configure.chmod(0o755)
        self.assertGreater(configure.stat().st_size, 300_000)
        self.assertEqual(guard.irreversible_actions("./configure --prefix=/usr/local", ws.name), [])
        self.assertEqual(guard.irreversible_actions(f"{configure}", ws.name), [])
        outside = Path(core.real(self.tmp.name)) / "configure"  # the same script outside the workspace is unread
        outside.write_text(configure.read_text())
        outside.chmod(0o755)
        self.assertEqual(self.acts(str(outside)), ["unknown"])

    # ---- the Code reviewer's last probes of 80b1467 (M700, M701)
    def unparsed_scripts(self):
        """x.sh in a workspace folder and y.sh in a temp folder, each with a comment the gate's parser cannot read
        (`# it's a test`); y.sh then force-pushes main. -> (workspace folder, temp folder)."""
        core.DEV.mkdir(parents=True, exist_ok=True)
        ws = tempfile.TemporaryDirectory(dir=core.DEV)
        self.addCleanup(ws.cleanup)
        here, there = Path(core.real(ws.name)), Path(core.real(self.tmp.name))
        (here / "x.sh").write_text("#!/bin/sh\n# it's a test\necho hi\n")
        (there / "y.sh").write_text("# it's a test\ngit push --force origin main\n")
        for p in (here / "x.sh", there / "y.sh"):
            p.chmod(0o755)
        return str(here), str(there)

    def gate(self, cmd, cwd):
        with settings():
            v = guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1", grant=DenyAll())
        return (v or ("allow",))[0]

    def test_a_workspace_script_the_gate_cannot_parse_is_the_repositorys_own(self):
        # `./x.sh` in the workspace, whose comment has an apostrophe, was an unknown action: tier 2, which a crew
        # member never runs; the same script outside the workspace still is one
        here, there = self.unparsed_scripts()
        self.assertIsNone(guard.irreversible_actions("# it's a test", here))  # what makes the script unparseable
        self.assertEqual(guard.irreversible_actions("./x.sh", here), [])
        self.assertEqual(self.gate("./x.sh", here), "allow")
        self.assertEqual([a["action"] for a in guard.irreversible_actions("./y.sh", there)], ["unknown"])
        self.assertEqual(self.gate("./y.sh", there), "deny")

    def test_a_script_a_shell_reads_that_the_gate_cannot_parse_is_an_unknown_action(self):
        # `sh y.sh` gave no actions at all for a script the gate cannot parse, so the gate saw only `sh y.sh` and
        # allowed the force push in it, while `./y.sh` was already unknown; the same for a script a shell is fed
        here, there = self.unparsed_scripts()
        for run in ("sh {}", "bash {}", "zsh -e {}", ". {}", "source {}", "sh < {}", "cat {} | bash", "bash -s < {}"):
            with self.subTest(run=run):
                cmd = run.format("y.sh")
                self.assertEqual([a["action"] for a in guard.irreversible_actions(cmd, there)], ["unknown"])
                self.assertEqual(self.gate(cmd, there), "deny")
                mine = run.format("x.sh")  # the repository's own script runs as before
                self.assertEqual(guard.irreversible_actions(mine, here), [])
                self.assertEqual(self.gate(mine, here), "allow")
                self.assertIsNone(guard.check_bash(mine, here, "claude"))
        # a line the command itself holds stays unparsed: the gate judges its text (`git push` in it is refused)
        self.assertIsNone(guard.irreversible_actions("sh -c \"# it's\ngit push --force origin main\"", there))
        self.assertEqual(self.gate("sh -c \"# it's\ngit push --force origin main\"", there), "deny")

    def test_csh_and_tcsh_are_shells(self):
        # `tcsh -c 'git push --force origin main'` gave no action and `csh -c 'dev install'` passed the guard: the
        # guard did not know them as shells, nor a script with their shebang
        push = "git push --force origin main"
        for shell in ("csh", "tcsh", "/bin/csh", "/usr/bin/tcsh"):
            with self.subTest(shell=shell):
                self.assertEqual(self.acts(f"{shell} -c '{push}'"), ["push-destructive"])
                self.assertEqual(self.acts(f"{shell} -fc '{push}'"), ["push-destructive"])
                v = guard.check_bash(f"{shell} -c 'dev install'", self.repo, "claude")
                self.assertEqual((v or ("",))[0], "deny", v)
        root = Path(core.real(self.tmp.name))
        for name, shebang in (("c.csh", "#!/bin/csh\n"), ("t.csh", "#!/usr/bin/env tcsh -f\n")):
            p = root / name
            p.write_text(f"{shebang}{push}\n")
            p.chmod(0o755)
            with self.subTest(script=name):
                self.assertEqual(guard._run_by_path(p)[0], "shell")
                self.assertTrue(guard._shell_script_head(p))
                self.assertEqual(self.acts(str(p)), ["push-destructive"])
        self.assertEqual(self.acts("tcsh -c 'git status'"), [])  # a harmless line stays open

    def test_a_shell_tool_input_is_read_from_cmd_too(self):
        # Codex's exec_command may send its command as `cmd` (a string or a list): only `command` was read, so the
        # guard judged "" and allowed every such call
        for tool in ("exec_command", "Bash"):
            for payload in ({"cmd": "git push --force origin main"}, {"cmd": ["dev", "install"]},
                            {"command": "ls", "cmd": "dev install"}, {"command": "dev install", "cmd": "ls"},
                            {"command": ["ls"], "cmd": ["git", "push", "--force", "origin", "main"]}):
                with self.subTest(tool=tool, payload=payload):
                    v = guard.check(tool, payload, self.repo, "claude")
                    self.assertEqual((v or ("",))[0], "deny", v)
            for payload in ({"cmd": "ls"}, {"cmd": ["git", "status"]}, {"command": "ls", "cmd": "ls"}):
                with self.subTest(tool=tool, open=payload):
                    self.assertIsNone(guard.check(tool, payload, self.repo, "claude"))
        self.assertEqual(guard.tool_commands({"cmd": ["git", "commit", "-m", "a b"]}), ["git commit -m 'a b'"])
        self.assertEqual(guard.tool_commands({"command": "ls", "cmd": "ls"}), ["ls"])
        self.assertEqual(guard.tool_commands({"cmd": 3}), [])

    # ---- Red team round 3 on 80b1467: nesting depth, patch header paths
    def test_a_command_nested_deeper_than_the_guard_reads_is_refused(self):
        # five levels of `sh -c` (or bash -lc, eval, env and nohup) around a force push passed every rule: the caps
        # read "too deep to read" as "nothing found"; now the shell rules refuse it and the gate counts it unknown
        push = "git push --force origin main"
        families = {"sh -c": ("sh -c ",), "bash -lc": ("bash -lc ",), "eval": ("eval ",),
                    "env/nohup/sh": ("env sh -c ", "nohup bash -c ", "sh -c ")}

        def wrap(cmd, prefixes, n):
            for k in range(n):
                cmd = prefixes[k % len(prefixes)] + shlex.quote(cmd)
            return cmd
        for family, prefixes in families.items():
            for n in (5, 6, 7):
                cmd = wrap(push, prefixes, n)
                with self.subTest(family=family, depth=n):
                    for tool in ("Bash", "Monitor"):
                        v = guard.check(tool, {"command": cmd, "description": "x"}, self.repo, "claude")
                        self.assertEqual((v or ("",))[0], "deny", (tool, v))
                        self.assertEqual(v[1], guard.NESTED_REASON)
                    self.assertIn("unknown", self.acts(cmd))
                    self.assertEqual(self.gate(cmd, self.repo), "deny")
                    self.assertIsNotNone(self.member(cmd))
        for act in ("rm -rf ~", "gh pr merge 12 --admin", "dev install", "npm publish",
                    "printf {} > ~/.config/devkit/config.json"):
            with self.subTest(act=act):
                v = guard.check_bash(wrap(act, ("sh -c ",), 5), self.repo, "claude")
                self.assertEqual((v or ("",))[0], "deny", v)
        # four levels are still read: the inner command gets its own verdict, as written plainly
        self.assertEqual(self.acts(wrap(push, ("sh -c ",), 4)), ["push-destructive"])
        self.assertIsNone(guard.check_bash(wrap("git status", ("sh -c ",), 4), self.repo, "claude"))
        self.assertEqual(self.acts(wrap("git status", ("sh -c ",), 4)), [])

    def test_a_patch_header_path_with_a_line_break_codex_does_not_see_is_refused(self):
        # `*** Add File: /private/tmp/x\r/../../..<home>/.config/devkit/config.json` was allowed: splitlines() cut the
        # path at the \r (and at \x0b, \x1c, \x85, U+2028 …), where Codex's parser, which splits on \n, does not
        root = Path(self.tmp.name)
        config = f"{core.HOME}/.config/devkit/config.json"
        note = f"{core.real(str(root))}/handoff/projects/other/repo.md"
        for sep in ("\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            cut = f"/private/tmp/x{sep}/../../.."
            for body in (f"*** Add File: {cut}{config}\n+{{}}",
                         f"*** Delete File: {cut}{config}",
                         f"*** Update File: /private/tmp/ok.txt\n*** Move to: {cut}{config}\n@@\n-a\n+b",
                         f"*** Add File: {cut}{note}\n+x",
                         f"*** Add File: crew{sep}/../crew/crew.json\n+{{}}"):
                patch = f"*** Begin Patch\n{body}\n*** End Patch\n"
                with self.subTest(sep=repr(sep), body=body.split(":")[0]):
                    v = guard.check("apply_patch", {"input": patch}, str(root), "codex")
                    self.assertEqual((v or ("",))[0], "deny", v)
                    self.assertEqual(v[1], guard.PATCH_HEADER_REASON)
        # a patch with CRLF line ends names the file Codex writes, and a plain one is judged as before
        crlf = "*** Begin Patch\r\n*** Add File: /private/tmp/ok.txt\r\n+x\r\n*** End Patch\r\n"
        self.assertEqual(guard.parse_patch_paths(crlf), [("add", "/private/tmp/ok.txt")])
        self.assertIsNone(guard.check("apply_patch", {"input": crlf}, str(root), "codex"))
        plain = f"*** Begin Patch\n*** Add File: {config}\n+{{}}\n*** End Patch\n"
        v = guard.check("apply_patch", {"input": plain}, str(root), "codex")
        self.assertEqual((v or ("",))[0], "deny", v)
        self.assertNotEqual(v[1], guard.PATCH_HEADER_REASON)


class DeveloperOnlyGrants(tp.TranscriptCase):
    # B2 follow-up (the tech lead's decisions): the developer opens one developer-only action with a line that reads
    # naturally, `grant embargo <branch>` or `grant backup <project>` (and `grant developer-only <target>` still works),
    # through the real prompt hook, transcript and grant store: single use, 30 minutes, bound to its target and
    # repository. Without one these exact commands are refused in every setting of safeguards.irreversible_main, the
    # audit week included; everything else keeps the audit week.
    def setUp(self):
        super().setUp()
        root = Path(self.state.tmp.name)
        self.repo = make_repo(root)
        for p in (mock.patch.object(guard, "current_project", return_value="acme/api"),
                  mock.patch.object(audit, "policy_file", lambda: root / "no-policy.json"),
                  mock.patch.object(core, "STATE", root / "state")):  # a fresh gate start: the audit week
            p.start()
            self.addCleanup(p.stop)
        self.typed = 0

    def type_line(self, text):
        # the developer types the line: their own prompt in the session's transcript, then the prompt hook
        self.typed += 1
        pid = f"prompt-b2-{self.typed}"
        self.write({"type": "user", "promptId": pid, "uuid": f"u-b2-{self.typed}", "isSidechain": False,
                    "origin": {"kind": "human"}, "message": {"role": "user", "content": text}})
        return self.prompt(text, pid)

    def main(self, cmd, setting="enforce", now=None):
        with settings(main=setting):
            v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1", now=now)
        return v[1] if v else None

    def test_grant_embargo_opens_one_lift_of_that_branch(self):
        self.type_line("grant embargo feat/x")
        self.assertIn("grant embargo feat/y", self.main("dev branch embargo --lift feat/y") or "")  # its branch only
        self.assertIsNone(self.main("dev branch embargo --lift feat/x"))
        self.assertIn("grant embargo feat/x", self.main("dev branch embargo --lift feat/x") or "")  # once

    def test_grant_backup_opens_one_public_backup_or_one_policy_change_of_that_project(self):
        self.type_line("grant backup acme/api")
        self.assertIn("grant backup acme/other", self.main("dev backup policy on -p acme/other") or "")
        self.assertIsNone(self.main("dev backup --public feat/a"))
        self.assertIn("grant backup acme/api", self.main("dev backup policy off") or "")  # its one use is gone
        self.type_line("grant backup acme/api")
        self.assertIsNone(self.main("dev backup policy off"))

    def test_a_grant_expires_after_thirty_minutes(self):
        self.type_line("grant embargo feat/x")
        later = time.time() + tp.pg.GRANT_TTL + 60
        self.assertIsNotNone(self.main("dev branch embargo --lift feat/x", now=later))
        self.assertIsNone(self.main("dev branch embargo --lift feat/x"))

    def test_grant_developer_only_with_the_gates_target_still_works(self):
        self.type_line("grant developer-only embargo/feat/x")
        self.assertIsNone(self.main("dev branch embargo --lift feat/x"))
        self.assertIsNotNone(self.main("dev branch embargo --lift feat/x"))

    def test_developer_only_actions_are_refused_in_every_setting_the_audit_week_included(self):
        for setting in (None, "audit", "enforce", "bogus"):  # None: the first week on this Mac, which audits
            with self.subTest(setting=setting):
                self.assertIn("grant embargo feat/x", self.main("dev branch embargo --lift feat/x", setting) or "")
                self.assertIn("grant backup acme/api", self.main("dev backup --public feat/a", setting) or "")
                self.assertIsNotNone(self.main("dev backup policy ask", setting))
        self.assertEqual(guard.irreversible_setting("main"), "audit")  # the week really is on
        for setting in (None, "audit"):  # everything else keeps the audit week
            with self.subTest(setting=setting):
                self.assertIsNone(self.main("dev release --version 26.11.5", setting))
                self.assertIsNone(self.main("dev backup feat/a", setting))
        self.type_line("grant embargo feat/x")
        self.assertIsNone(self.main("dev branch embargo --lift feat/x", "audit"))  # the grant opens it in audit too
        rec = [r for r in self.gate_records() if (r.get("ref") or {}).get("irreversible") == "developer-only"]
        self.assertEqual([(r["ref"]["result"], r["ref"]["setting"]) for r in rec[:1]], [("deny", "enforce")])

    def test_unparsed_or_failing_checks_of_developer_only_commands_deny_in_audit(self):
        self.assertIsNotNone(self.main("dev branch embargo --lift feat/x 'unclosed", "audit"))
        self.assertIsNone(self.main("ls 'unclosed", "audit"))
        with mock.patch.object(tp.pg, "use_grant", side_effect=RuntimeError("state busy")):
            self.assertIsNotNone(self.main("dev backup --public feat/a", "audit"))
            self.assertIsNone(self.main("dev release --version 26.11.5", "audit"))  # audit fails open (P4)

    def test_crew_members_never_run_them_whatever_the_setting_or_grant(self):
        self.type_line("grant embargo feat/x")
        for setting in ("audit", "enforce"):
            with self.subTest(setting=setting), settings(member=setting):
                v = guard.crew_shell_verdict("dev branch embargo --lift feat/x", self.repo, None, "redteamprobe",
                                             level="all", lane="a1", session="s1")
                self.assertIn("Crew members never run", (v or ("", ""))[1])

    def test_a_pre_approval_planted_in_the_run_opens_nothing(self):
        run = tp.pg.load_run("claude", "s1")
        run["mode"] = "auto"
        run["project"] = "acme/api"
        run["preapprovals"] = [{"id": f"P{i}", "action": act, "target": t, "repo": "acme/api", "uses_left": 5,
                                "expires": time.time() + 3600, "evidence": {"channel": "E2"}, "ts": time.time()}
                               for i, (act, t) in enumerate((("developer-only", "embargo/feat/x"),
                                                             ("embargo", "feat/x")))]
        tp.pg._save(run)
        self.assertIn("grant embargo feat/x", self.main("dev branch embargo --lift feat/x") or "")

    def gate_records(self):
        files = sorted((Path(self.state.tmp.name) / "audit").glob("*.jsonl"))
        return [r for f in files for r in map(json.loads, f.read_text().splitlines())
                if r["action"] == "guard.irreversible"]


# ---- B2a and B2b (the security engineer's backup review, release-blocking). B2a: the embargo list and the backup
# policy in a project's handoff note decide what devkit pushes, so no agent writes or removes them: the file tools are
# judged by the note's text after the edit, and a shell write (sed -i, a redirect, cp, mv, any program) may not write
# a note at all, since the guard cannot see what it changes. Only `dev branch embargo` and `dev backup policy` change
# them. An embargo that leaves the note without its `git.embargo` lift record keeps holding. B2b (the tech lead's
# decision): an agent's push of an embargoed branch, or of the commits it holds, is the developer-only action
# embargo/<branch>, refused without the developer's typed `grant embargo <branch>`, and refused when the note cannot
# be read.

NOTE = """---
status: active
priority: normal
embargo:
  - branch: fix/hole
    why: "discloses an open hole"
    who: keymaker
    when: 2026-11-06T10:00:00Z
backup_policy:
  - policy: off
    why: "a public repository"
    who: ana
    when: 2026-11-06T10:00:00Z
---
# acme/widget

## Current state
Old text.
"""


def shq(p) -> str:
    import shlex
    return shlex.quote(str(p))


class EmbargoCase(unittest.TestCase):
    PID = "acme/widget"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = make_repo(self.root)
        self.handoff = self.root / "Development" / ".handoff"
        self.note = self.handoff / "projects" / "acme" / "widget.md"
        self.note.parent.mkdir(parents=True)
        self.note.write_text(NOTE)
        cfg_audit = dict(json.loads(json.dumps(core.DEFAULTS.get("audit") or {})), level="all")
        for p in (mock.patch.object(core, "HANDOFF", self.handoff),
                  mock.patch.object(core, "NOTES", self.handoff / "projects"),
                  mock.patch.object(guard, "current_project", return_value=self.PID),
                  mock.patch.object(audit, "AUDIT", self.root / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: self.root / "no-policy.json"),
                  mock.patch.dict(core.CFG, {"audit": cfg_audit}), mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("DEVKIT_FROM_AGENT", "DEVKIT_SESSION", "DEVKIT_AGENT"):
            os.environ.pop(k, None)

    def check(self, tool, payload, cwd=None):
        with settings():
            v = guard.check(tool, payload, cwd or self.repo, "claude")
        return v[1] if v else None

    def sh(self, cmd, cwd=None):
        return self.check("Bash", {"command": cmd}, cwd)


class NoteKeysAreDevkits(EmbargoCase):
    # B2a: the file tools keep every other change to the note; they may not write or remove the two keys
    def edit(self, old, new, path=None):
        return self.check("Edit", {"file_path": str(path or self.note), "old_string": old, "new_string": new})

    def test_b2a_file_tools_may_not_write_or_remove_the_embargo_or_the_backup_policy(self):
        self.assertIsNone(self.edit("Old text.", "New text."))  # baseline: the agent's own sections stay open
        self.assertIsNone(self.check("Write", {"file_path": str(self.note), "content": NOTE.replace("Old", "New")}))
        for old, new in (("  - branch: fix/hole\n", ""), ("embargo:", "embargoes:"), ("policy: off", "policy: on"),
                         ("---\nstatus", "status"), ("backup_policy:\n", "backup_policy: []\n"),
                         ("priority: normal\n", "priority: normal\nembargo:\n  - branch: x\n")):
            with self.subTest(old=old):
                self.assertIn("embargo list and the backup policy", self.edit(old, new) or "")
        gone = NOTE.split("embargo:")[0] + "---\n# acme/widget\n"
        self.assertIsNotNone(self.check("Write", {"file_path": str(self.note), "content": gone}))
        edits = [{"old_string": "Old text.", "new_string": "New."},
                 {"old_string": "policy: off", "new_string": "policy: on"}]
        self.assertIsNotNone(self.check("MultiEdit", {"file_path": str(self.note), "edits": edits}))
        fresh = self.handoff / "projects" / "acme" / "other.md"  # a new note may not arrive with them either
        self.assertIsNotNone(self.check("Write", {"file_path": str(fresh), "content": NOTE}))
        self.assertIsNotNone(self.check("Write", {"file_path": str(self.note)}))  # dev guard check --write: unknown
        plan = self.handoff / "plans" / "acme" / "widget" / "p.md"
        self.assertIsNone(self.check("Write", {"file_path": str(plan), "content": NOTE}))  # not a note

    def test_b2a_apply_patch_is_judged_by_the_note_after_the_patch(self):
        def patch(body):
            return self.check("apply_patch", {"command": f"*** Begin Patch\n{body}\n*** End Patch"})
        self.assertIsNone(patch(f"*** Update File: {self.note}\n@@\n ## Current state\n-Old text.\n+New text."))
        self.assertIsNotNone(patch(f"*** Update File: {self.note}\n@@\n embargo:\n-  - branch: fix/hole\n"
                                   f"+  - branch: fix/other"))
        self.assertIsNotNone(patch(f"*** Update File: {self.note}\n@@\n-  - policy: off\n+  - policy: on"))
        self.assertIsNotNone(patch(f"*** Delete File: {self.note}"))
        self.assertIsNotNone(patch(f"*** Update File: {self.note}\n*** Move to: {self.root}/elsewhere.md\n@@\n"
                                   f"-Old text.\n+New text."))
        self.assertIsNotNone(patch(f"*** Update File: {self.note}\n@@\n-no such line\n+x"))  # does not apply: refused

    def test_b2a_shell_writes_to_a_note_are_refused_whatever_writes_it(self):
        n, h = shq(self.note), shq(self.handoff)
        other = self.root / "other.md"
        other.write_text("x")
        for cmd in (f"sed -i '' '/^embargo:/,/^backup_policy:/d' {n}", f"echo x >> {n}", f"printf x > {n}",
                    f"cp {shq(other)} {n}", f"mv {n} {shq(other)}", f"tee {n} < /dev/null",
                    f"perl -pi -e 's/policy: off/policy: on/' {n}", f"cd {h} && sed -i '' 1d projects/acme/widget.md",
                    f"f={n}; sed -i '' 1d \"$f\"", f"git -C {h} checkout -- projects/acme/widget.md",
                    f"dev rm {n}", f"mv {h}/projects {shq(other)}.d",
                    "python3 -c \"import os; open(os.path.join(os.path.expanduser('~'), 'Development', '.handoff', "
                    "'projects', 'acme', 'widget.md'), 'w').write('')\"",
                    "python3 -c \"from devkit import branches; branches.set_embargo('acme/widget', 'fix/hole', "
                    "lift=True)\"", f"cd {h} && python3 -c \"open('projects/acme/widget.md', 'w')\"",
                    f"cp {shq(other)} {h}/audit/ana@mac/2026-11.jsonl"):  # the lift records the backup reads
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.sh(cmd))
        for cmd in (f"cat {n}", f"grep -n embargo {n}", f"sed -n 1,12p {n}", "dev note set status=blocked",
                    "dev handoff --log 'done; next: the release'", f"echo x > {h}/plans/acme/widget/p.md",
                    f"cd {shq(self.handoff.parent)} && python3 -c \"open('notes.md', 'w')\""):  # holds it, not in it
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.sh(cmd))


class EmbargoedBranchesStayOffTheRemote(EmbargoCase):
    # B2b: every way an agent sends an embargoed branch, or the commits it holds, to the remote
    def setUp(self):
        super().setUp()
        _git(self.repo, "checkout", "-q", "-b", "fix/hole", "main")
        (Path(self.repo) / "hole.txt").write_text("the fix\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "fix: close the hole")
        self.tip = subprocess.run(["git", "-C", self.repo, "rev-parse", "HEAD"], capture_output=True,
                                  text=True).stdout.strip()
        _git(self.repo, "branch", "leak", "fix/hole")  # a new name for the same commits
        _git(self.repo, "checkout", "-q", "feat/x")

    def targets(self, cmd):
        return [(a["action"], a["target"]) for a in guard.irreversible_actions(cmd, self.repo) or []]

    def main(self, cmd, grant=None, setting="enforce"):
        with settings(main=setting):
            v = guard.irreversible_verdict(cmd, self.repo, agent="claude", session="s1", grant=grant or DenyAll())
        return v[1] if v else None

    def member(self, cmd):
        with settings():
            v = guard.crew_shell_verdict(cmd, self.repo, None, "redteamprobe", level="all", lane="a1", session="s1",
                                         grant=lambda *a, **k: {"id": "step", "uses_left": 1})
        return v[1] if v else None

    def test_b2b_a_push_or_pr_of_an_embargoed_branch_is_the_developers(self):
        held = ("developer-only", "embargo/fix/hole")
        self.assertEqual(self.targets("git push origin feat/x"), [("push", "feat/x")])  # baseline
        self.assertIsNone(self.main("git push origin feat/x"))
        for cmd in ("git push origin fix/hole", "git push -u origin fix/hole:refs/heads/innocent",
                    f"git push origin {self.tip}:refs/heads/innocent", "git push origin leak", "git push --all origin",
                    "dev git push origin fix/hole", "gh pr create --head fix/hole --title t --body b",
                    "git -c alias.p=push p origin fix/hole"):
            with self.subTest(cmd=cmd):
                self.assertIn(held, self.targets(cmd))
                self.assertIn("`grant embargo fix/hole`", self.main(cmd) or "")
                self.assertIn("Crew members never run", self.member(cmd) or "")
        _git(self.repo, "checkout", "-q", "fix/hole")
        for cmd in ("git push", "git push -u origin HEAD", "dev push", "dev pr create", "gh pr create --fill"):
            with self.subTest(cmd=cmd):
                self.assertIn(held, self.targets(cmd))
                self.assertIn("embargoed", self.main(cmd) or "")
        self.assertEqual(self.targets("dev backup fix/hole"), [("push", "fix/hole")])  # a backup holds it itself

        def grant(action, target, repo, consume):  # the developer typed `grant embargo fix/hole`
            hit = (action, target) == ("embargo", "fix/hole")
            return {"id": "E2:t", "source": "E2", "uses_left": 1, "expires": 0} if hit else None
        self.assertIsNone(self.main("git push -u origin fix/hole", grant=grant))

    def test_b2b_a_note_devkit_cannot_read_refuses_every_push(self):
        self.note.write_text("---\nstatus: active\nembargo:\n  - branch: fix/hole\n")  # its block never ends
        self.assertIn(("developer-only", "embargo/feat/x"), self.targets("git push origin feat/x"))
        self.assertIn("cannot read", self.main("git push origin feat/x") or "")
        with mock.patch.object(core, "HANDOFF", self.root / "nowhere"), \
                mock.patch.object(core, "NOTES", self.root / "nowhere" / "projects"):  # no handoff repository here
            self.assertIn(("developer-only", "embargo/feat/x"), self.targets("dev push"))

    def test_b2b_a_push_or_pr_from_a_folder_named_at_run_time_fails_closed(self):
        # The code reviewer's trace of 202e88d: a folder the guard cannot resolve ($(…), a variable) passed no folder
        # to the embargo check, which read it as "no embargo". Now the session's own folder decides: an embargo there,
        # an unreadable note, or a folder that is no project refuses it as the developer-only action `?`.
        unknown = ("developer-only", "?")
        os.environ.pop("X", None)
        r = shq(self.repo)
        for cmd in (f"git -C {r} push origin feat/x", f"cd {r} && git push origin feat/x"):  # baseline: written out
            with self.subTest(cmd=cmd):
                self.assertEqual(self.targets(cmd), [("push", "feat/x")])
        subst = ('git -C "$(git rev-parse --show-toplevel)" push origin fix/hole',
                 'cd "$(git rev-parse --show-toplevel)" && gh pr create --head fix/hole --title t --body b')
        var = ('cd "$X" && git push origin fix/hole', 'X=/elsewhere; cd "$X" && dev push', 'cd "$X" && dev pr create',
               'cd "$X" && gh pr create --fill', 'dev git -C "$X" push origin fix/hole',
               'cd "$X" && bash -c "git push origin fix/hole"')
        for cmd in subst + var:
            with self.subTest(cmd=cmd):
                self.assertIn(unknown, self.targets(cmd))
                reason = self.main(cmd) or ""
                self.assertIn("folder", reason)
                self.assertNotIn("grant embargo", reason)  # `?`: no grant covers it; the folder written out does
                self.assertIn("Crew members never run", self.member(cmd) or "")
        self.assertIsNotNone(self.main(subst[0], setting="audit"))  # developer-only: the audit week holds it too
        self.note.write_text("---\nstatus: active\n---\n# acme/widget\n")  # no embargo in the session's project
        for cmd in (subst[0], var[0]):
            with self.subTest(cmd=cmd, embargo=False):
                self.assertNotIn(unknown, self.targets(cmd))
                self.assertIsNone(self.main(cmd))
        self.note.write_text("---\nstatus: active\nembargo:\n  - branch: fix/hole\n")  # a note devkit cannot read
        self.assertIn(unknown, self.targets(var[0]))

    def test_b2b_an_unknown_folder_is_refused_while_any_project_holds_an_embargo(self):
        # The tech lead's follow-up: `cd "$X"` may lead into any project on this Mac, so an embargo anywhere, a note
        # devkit cannot read, a handoff repository that is not here, or an embargo only the audit trail still holds
        # refuses it; with no embargo anywhere it is an ordinary push.
        unknown, cmd = ("developer-only", "?"), 'cd "$X" && git push origin feat/x'
        os.environ.pop("X", None)
        self.note.write_text("---\nstatus: active\n---\n# acme/widget\n")  # none in the session's project
        self.assertEqual(self.targets(cmd), [("push", "feat/x")])  # baseline: no embargo anywhere
        other = self.handoff / "projects" / "acme" / "other.md"
        other.write_text(NOTE.replace("acme/widget", "acme/other"))  # another project holds fix/hole
        self.assertIn(unknown, self.targets(cmd))
        self.assertIn("acme/other", self.main(cmd) or "")
        with mock.patch.object(guard, "current_project", return_value=None):  # the session's folder is no project
            self.assertIn(unknown, self.targets(cmd))
        other.write_text("---\nstatus: active\nembargo:\n  - branch: fix/hole\n")  # it cannot be read
        self.assertIn(unknown, self.targets(cmd))
        other.unlink()
        self.assertEqual(self.targets(cmd), [("push", "feat/x")])
        audit.record("git.embargo", "fix/gone", "set: an open hole", "ok", project="acme/gone", branch="fix/gone")
        self.assertIn(unknown, self.targets(cmd))  # its note is gone, the trail still holds it
        with mock.patch.object(core, "HANDOFF", self.root / "nowhere"), \
                mock.patch.object(core, "NOTES", self.root / "nowhere" / "projects"):  # no handoff repository here
            self.assertIn(unknown, self.targets('git -C "$X" push origin feat/x'))

    def test_b2b_git_dir_and_work_tree_name_the_folder_a_push_runs_in(self):
        # The tech lead's follow-up: --git-dir, --work-tree, GIT_DIR and GIT_WORK_TREE choose the repository a push
        # sends from. A literal path is judged as that folder; one known only at run time is an unknown folder.
        held, unknown = ("developer-only", "embargo/fix/hole"), ("developer-only", "?")
        here = self.real_projects()  # the session works in a folder that is no project; projects are git's own
        repo = self.repo

        def targets(cmd):
            return [(a["action"], a["target"]) for a in guard.irreversible_actions(cmd, str(here)) or []]
        for k in ("GIT_DIR", "GIT_WORK_TREE", "G", "W", "X"):
            os.environ.pop(k, None)
        g, r = shq(Path(repo) / ".git"), shq(repo)
        self.assertEqual(targets(f"git --git-dir={g} push origin feat/x"), [("push", "feat/x")])  # baseline
        for cmd in (f"git --git-dir={g} push origin fix/hole",
                    f"git --git-dir {g} --work-tree {r} push origin fix/hole",
                    f"GIT_DIR={g} git push origin fix/hole", f"env GIT_DIR={g} git push origin fix/hole",
                    f"export GIT_DIR={g}; git push origin fix/hole", f"GIT_DIR={g} dev git push origin fix/hole",
                    f"git -C {shq(here)} --git-dir={g} push origin fix/hole"):
            with self.subTest(cmd=cmd):
                self.assertIn(held, targets(cmd))
        for cmd in ('git --git-dir="$G" push origin feat/x', 'git --work-tree="$W" push origin feat/x',
                    'git -C "$X" push origin feat/x', 'GIT_DIR="$G" git push origin feat/x',
                    'GIT_WORK_TREE=$W git push origin feat/x', 'export GIT_DIR=$G; git push origin feat/x',
                    'GIT_DIR=$G gh pr create --fill', 'GIT_DIR=$G dev push', 'dev git --git-dir="$G" push'):
            with self.subTest(cmd=cmd):
                self.assertIn(unknown, targets(cmd))

    def real_projects(self) -> Path:
        # The code reviewer's finding 2 on cc259b7: a stand-in current_project said a worktree's git folder belonged to
        # the project, which the real one does not. Here the real one decides; only the scratch folder is temporary,
        # and origin names the project. Returns a folder that is no project, for the session to work in.
        _git(self.repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        for p in (mock.patch.object(guard, "current_project", _CURRENT_PROJECT),
                  mock.patch.object(guard, "is_temp", lambda p: core.is_under(p, self.scratch))):
            p.start()
            self.addCleanup(p.stop)
        guard._repo_of.cache_clear()
        self.addCleanup(guard._repo_of.cache_clear)
        self.assertEqual(guard.current_project(self.repo), self.PID)
        here = self.root / "elsewhere"
        here.mkdir()
        return here

    def test_b2b_a_push_from_a_git_folder_or_a_folder_change_is_judged_as_its_project(self):
        # The code reviewer's finding 1b on cc259b7: git pushes from inside a repository's git folder or a worktree's,
        # and `env -C`, `env --chdir` and `sudo -D` change the folder of the command they run. Each is judged as the
        # project it pushes from, so the embargo holds and the developer's `grant embargo fix/hole` there opens it.
        here = self.real_projects()
        _git(self.repo, "worktree", "add", "-q", str(self.root / "wt"), "fix/hole")
        r, g, wt = shq(self.repo), shq(Path(self.repo) / ".git"), shq(Path(self.repo) / ".git" / "worktrees" / "wt")
        os.environ.pop("GIT_DIR", None)

        def acts(cmd):
            return [(a["action"], a["target"], a["repo"]) for a in guard.irreversible_actions(cmd, str(here)) or []]

        def main(cmd, grant=None):
            with settings():
                v = guard.irreversible_verdict(cmd, str(here), agent="claude", session="s1", grant=grant or DenyAll())
            return v[1] if v else None

        def grant(action, target, repo, consume):  # the developer typed `grant embargo fix/hole` in acme/widget
            hit = (action, target, repo) == ("embargo", "fix/hole", self.PID)
            return {"id": "E2:t", "source": "E2", "uses_left": 1, "expires": 0} if hit else None
        self.assertEqual(acts(f"git -C {g} push origin feat/x"), [("push", "feat/x", self.PID)])  # baseline: no hold
        for cmd in (f"git -C {r} push origin fix/hole", f"git -C {shq(self.root / 'wt')} push origin fix/hole",
                    f"git -C {g} push origin fix/hole", f"cd {g} && git push origin fix/hole",
                    f"cd {g}/refs && git push origin fix/hole", f"git -C {wt} push origin fix/hole",
                    f"git --git-dir={wt} push origin fix/hole", f"GIT_DIR={wt} git push origin fix/hole",
                    f"git --git-dir={wt} push", f"env -C {r} git push origin fix/hole",
                    f"env --chdir={r} git push origin fix/hole", f"env --chdir {r} git push origin fix/hole",
                    f"env -iC {r} git push origin fix/hole", f"env -C{r} git push origin fix/hole",
                    f"sudo -D {r} git push origin fix/hole", f"sudo --chdir={r} git push origin fix/hole",
                    f"cd {wt} && dev push", f"cd {wt} && gh pr create --fill"):
            with self.subTest(cmd=cmd):
                self.assertIn(("developer-only", "embargo/fix/hole", self.PID), acts(cmd))
                self.assertIn("`grant embargo fix/hole`", main(cmd) or "")
                self.assertIsNone(main(cmd, grant=grant))
        self.assertEqual(acts(f"env -C {shq(here)} git -C {r} push origin feat/x"), [("push", "feat/x", self.PID)])

    def test_b2b_a_push_from_a_folder_that_is_no_project_is_held_by_any_embargo(self):
        # The code reviewer's finding 1a on cc259b7: a folder git can push from but devkit maps to no project (a
        # temporary clone, a folder that does not exist yet, one a sourced file chooses) read as "no embargo". It is
        # now held like a folder known only at run time: any project's embargo refuses it as `?`, which no grant opens.
        here = self.real_projects()
        unknown = ("developer-only", "?")
        subprocess.run(["git", "clone", "-q", self.repo, str(self.scratch / "clone")], check=True, capture_output=True)
        _git(self.scratch / "clone", "remote", "set-url", "origin", "git@github.com:acme/widget.git")  # the real remote
        (self.root / "env.sh").write_text(f"export GIT_DIR={self.repo}/.git\n")
        clone, c2, env = shq(self.scratch / "clone"), shq(self.scratch / "c2"), shq(self.root / "env.sh")
        for k in ("GIT_DIR", "X"):
            os.environ.pop(k, None)
        held = (f"git -C {clone} push origin fix/hole", f"cd {clone} && dev push", f"cd {clone} && gh pr create --fill",
                f"git clone {shq(self.repo)} {c2} && git -C {c2} push git@github.com:acme/widget.git "
                f"origin/fix/hole:refs/heads/fix/hole", f"git push origin fix/hole",
                f"source {env} && git push origin fix/hole", f". {env} && git push origin fix/hole",
                f"source {env}; git -C {shq(self.repo)} push origin feat/x",
                f"source {env} && cd {clone} && git push origin fix/hole",
                f'cd "$(git -C {shq(self.repo)} rev-parse --git-common-dir)" && git push origin fix/hole')
        for cmd in held:
            with self.subTest(cmd=cmd):
                self.assertIn(unknown, [(a["action"], a["target"]) for a in guard.irreversible_actions(cmd, str(here))])
                with settings():
                    reason = (guard.irreversible_verdict(cmd, str(here), agent="claude", session="s1",
                                                         grant=DenyAll()) or (None, ""))[1]
                self.assertIn("acme/widget has an embargo", reason)
                self.assertNotIn("grant embargo", reason)
        with settings(main="audit"):  # a push devkit cannot parse there is held in the audit week too
            self.assertIsNotNone(guard.irreversible_verdict(f"git -C {clone} push origin feat/x 'unclosed", str(here),
                                                            agent="claude", session="s1", grant=DenyAll()))
        self.note.write_text("---\nstatus: active\n---\n# acme/widget\n")  # no embargo anywhere: ordinary pushes
        for cmd in held[:2]:
            with self.subTest(cmd=cmd, embargo=False):
                self.assertNotIn(unknown, [(a["action"], a["target"]) for a in
                                           guard.irreversible_actions(cmd, str(here))])
        # the tech lead's decision: a Mac without a handoff repository stays fail closed (only a broken setup has
        # none), and the refusal says how to repair it
        with mock.patch.object(core, "HANDOFF", self.root / "nowhere"), \
                mock.patch.object(core, "NOTES", self.root / "nowhere" / "projects"), settings():
            reason = (guard.irreversible_verdict(held[0], str(here), agent="claude", session="s1",
                                                 grant=DenyAll()) or (None, ""))[1]
        self.assertIn("is not on this Mac", reason)
        self.assertIn("`dev doctor`", reason)

    def test_b2b_the_release_push_and_the_handoff_repositorys_pushes_still_pass(self):
        # The two pushes this fix must leave alone while acme/widget holds fix/hole: a release branch pushed from its
        # project's checkout, and the handoff repository's own pushes (it is no project, yet holds every note).
        here = self.real_projects()
        _git(self.repo, "checkout", "-q", "-b", "release/26.11.3", "main")
        _git(self.repo, "config", "branch.release/26.11.3.remote", "origin")
        _git(self.repo, "config", "branch.release/26.11.3.merge", "refs/heads/release/26.11.3")
        _git(self.handoff, "init", "-q", "-b", "sync/mac")  # a branch of its own: main is a protected push anywhere
        _git(self.handoff, "add", "-A")
        _git(self.handoff, "commit", "-q", "-m", "notes")
        _git(self.handoff, "remote", "add", "origin", "git@github.com:ana/handoff.git")
        h = shq(self.handoff)
        cases = (("git push", self.repo), ("git push -u origin release/26.11.3", self.repo),
                 ("dev push", self.repo), (f"git -C {h} push origin sync/mac", str(here)),
                 ("git push origin sync/mac", str(self.handoff)), (f"cd {h}/.git && git push", str(here)))
        for embargo in (True, False):  # with acme/widget's embargo on fix/hole, then with no embargo anywhere
            if not embargo:
                self.note.write_text("---\nstatus: active\n---\n# acme/widget\n")
            for cmd, cwd in cases:
                with self.subTest(cmd=cmd, cwd=cwd, embargo=embargo):
                    found = guard.irreversible_actions(cmd, cwd) or []
                    self.assertTrue(found and all(a["action"] in ("push", "push-destructive") for a in found), found)
                    with settings():
                        self.assertIsNone(guard.irreversible_verdict(cmd, cwd, agent="claude", session="s1",
                                                                     grant=DenyAll()))

    def test_b2b_an_unparsed_push_with_an_embargo_in_place_is_refused_in_the_audit_week(self):
        self.assertIsNotNone(self.main("git push origin fix/hole 'unclosed", setting="audit"))
        self.note.write_text("---\nstatus: active\n---\n# acme/widget\n")  # no embargo left
        self.assertIsNone(self.main("git push origin feat/x 'unclosed", setting="audit"))


class NotesInAnotherEncoding(EmbargoCase):
    # The code reviewer's follow-up to B2a: a note saved with a byte-order mark (or CRLF line endings) read as "no
    # embargo" and "no backup policy", so its branches were backed up and the guard saw no key to keep. devkit and the
    # guard now read it as the same note.
    def test_b2a_a_note_with_a_bom_or_crlf_keeps_its_embargo_and_backup_policy(self):
        from devkit import branches
        for text in ("﻿" + NOTE, NOTE.replace("\n", "\r\n"), "﻿" + NOTE.replace("\n", "\r\n")):
            with self.subTest(text=repr(text[:6])):
                self.note.write_bytes(text.encode())
                held, why = branches.read_embargoes(self.PID)
                self.assertEqual((sorted(held or {}), why), (["fix/hole"], ""))
                self.assertEqual((branches.backup_policy(self.PID) or {}).get("policy"), "off")
                self.assertIsNotNone(self.check("Edit", {"file_path": str(self.note), "old_string": "branch: fix/hole",
                                                         "new_string": "branch: fix/other"}))
                self.assertIsNone(self.check("Edit", {"file_path": str(self.note), "old_string": "Old text.",
                                                      "new_string": "New text."}))


class DeployingWorkflowsWithoutABranchFilter(tb.BackupCase):
    # The code reviewer's follow-up: a workflow that runs on every push (`on: push`, no branches filter) and deploys
    # (a job with an environment) read as not deploying from a feature branch, so `dev backup --accept-ci` could start
    # a deploy. Only a filter that leaves the branch out (branches, branches-ignore, tags alone) rules it out.
    WF = "name: deploy\n{on}\njobs:\n  deploy:\n    runs-on: ubuntu-latest\n    environment: production\n" \
         "    steps:\n      - run: echo deploy\n"

    def workflow(self, on):
        wf = self.repo / ".github" / "workflows"
        wf.mkdir(parents=True, exist_ok=True)
        (wf / "deploy.yml").write_text(self.WF.format(on=on))
        return self.commit("ci: deploy from the workflow")

    def test_b2_an_unfiltered_push_trigger_deploys_from_every_branch(self):
        self.branch("feat/x", 2 * tb.HOUR)
        for on, deploys in (("on: push", True), ("on: [push, pull_request]", True),
                            ("on:\n  push:\n    branches-ignore: [main]", True),
                            ("on:\n  push:\n    paths: ['src/**']", True),
                            ("on:\n  push:\n    branches-ignore: ['feat/**']", False),
                            ("on:\n  push:\n    tags: ['v*']", False), ("on:\n  push:\n    branches: [main]", False),
                            ("on: pull_request", False)):
            with self.subTest(on=on):
                sha = self.workflow(on)
                self.assertEqual(bool(tb.backup._deploys_from(self.repo, "feat/x", sha)), deploys)
        self.workflow("on: push")
        res = tb.backup.run(self.repo, ["feat/x"], accept_ci=True)[0]
        self.assertEqual(res["state"], "skipped")
        self.assertIn("deploy.yml deploys from it (job deploy, environment production)", res["reason"])
        self.assertEqual(self.on_origin("feat/x"), "")


@unittest.skipUnless(tb.HAS_GITLEAKS, "the backup's secret scan needs gitleaks")
class TheBackupRemembersItsEmbargoes(tb.BackupCase):
    # B2a: an embargo that left the note without `dev branch embargo --lift` (a hand edit, a bad merge, a checkout of
    # an older note) keeps holding in the backup and the branch map until the developer lifts it
    def test_b2a_an_embargo_gone_from_the_note_without_its_lift_record_keeps_holding(self):
        self.ci(tb.CI_PR_AND_MAIN)
        self.branch("fix/hole", 2 * tb.HOUR)
        rc, out = self.dev("branch", "embargo", "fix/hole", "--why", "discloses an open hole")
        self.assertEqual(rc, 0, out)
        self.write_note("---\nstatus: active\n---\n# acme/widget\n")  # the record is gone, the lift never happened
        held, why = tb.branches.read_embargoes(tb.PID)
        self.assertEqual((sorted(held or {}), why), (["fix/hole"], ""))
        self.assertIn("left the handoff note without", held["fix/hole"]["why"])
        res = tb.backup.run(self.repo, ["fix/hole"])[0]
        self.assertEqual((res["state"], res["reason"]), ("skipped", "embargoed"))
        self.assertEqual(self.on_origin("fix/hole"), "")
        row = {r["name"]: r for r in tb.branches.map(self.repo, prs=[])}["fix/hole"]
        self.assertEqual(row["backup"]["state"], "embargoed")
        rc, out = self.dev("branch", "embargo", "--lift", "fix/hole")  # the developer's lift releases it
        self.assertEqual(rc, 0, out)
        self.assertEqual(tb.branches.read_embargoes(tb.PID), ({}, ""))
        self.assertEqual([r["detail"][:4] for r in self.records("git.embargo")], ["set:", "lift"])
        self.assertEqual(tb.backup.run(self.repo, ["fix/hole"])[0]["state"], "pushed")


if __name__ == "__main__":
    unittest.main()
