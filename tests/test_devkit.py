# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Unit tests for devkit. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ui, canary, ci, core, guard, registry  # noqa: E402

DEV = core.DEV
HOME = core.HOME
# Hermetic: the GitHub accounts that own top-level folders normally come from `gh`; tests use fixed ones so they run
# the same on a developer's Mac and in CI (no GitHub sign-in there).
OWNER = "acme-test"
_REAL = (core.owners, core.github_user)


def setUpModule():
    core.owners = lambda: {OWNER, "otherorg"}
    core.github_user = lambda: OWNER


def tearDownModule():
    core.owners, core.github_user = _REAL


class TempState:
    """Point devkit's state dir (push budget, canary log) at a throwaway folder."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = core.STATE, canary.LOG
        core.STATE = Path(self.tmp.name)
        canary.LOG = core.STATE / "canary.jsonl"
        return self

    def __exit__(self, *exc):
        core.STATE, canary.LOG = self.old
        self.tmp.cleanup()


def decision(tool, tool_input, cwd=str(DEV), agent="claude"):
    v = guard.check(tool, tool_input, cwd, agent)
    return v[0] if v else "allow"


class GuardWriteTests(unittest.TestCase):
    def test_write_inside_project_allowed(self):
        self.assertEqual(decision("Write", {"file_path": str(DEV / OWNER / "repo" / "src" / "a.ts")}), "allow")

    def test_write_to_temp_allowed(self):
        self.assertEqual(decision("Write", {"file_path": "/tmp/scratch.py"}), "allow")
        self.assertEqual(decision("Write", {"file_path": str(core.TMP_ROOT / "x.md")}), "allow")

    def test_write_to_agent_config_and_dotfiles_allowed(self):
        self.assertEqual(decision("Edit", {"file_path": str(HOME / ".claude" / "settings.json")}), "allow")
        self.assertEqual(decision("Edit", {"file_path": str(HOME / ".zshrc")}), "allow")

    def test_write_outside_roots_asks_claude_denies_codex(self):
        p = {"file_path": str(HOME / "Desktop" / "notes.md")}
        self.assertEqual(decision("Write", p, agent="claude"), "ask")
        self.assertEqual(decision("Write", p, agent="codex"), "deny")

    def test_sibling_copy_folder_denied(self):
        self.assertEqual(decision("Write", {"file_path": str(DEV / "myrepo-pr12-zz" / "a.txt")}), "deny")

    def test_file_directly_in_dev_root_denied(self):
        self.assertEqual(decision("Write", {"file_path": str(DEV / "notes-zz.md")}), "deny")

    def test_backup_copies_denied(self):
        for name in ("app.ts.bak", "service_old.py", "index.orig", "styles copy.css", "config.backup"):
            with self.subTest(name=name):
                self.assertEqual(decision("Write", {"file_path": str(DEV / OWNER / "repo" / name)}), "deny")

    def test_regular_names_not_mistaken_for_backups(self):
        for name in ("old_parser_test.py", "copyright.ts", "backup_service.java", ".gitignore"):
            with self.subTest(name=name):
                self.assertEqual(decision("Write", {"file_path": str(DEV / OWNER / "repo" / name)}), "allow")

    def test_codex_apply_patch_paths(self):
        patch = "*** Begin Patch\n*** Add File: /Users/nobody/Desktop/x.md\n+hi\n*** End Patch"
        self.assertEqual(decision("apply_patch", {"command": patch}, agent="codex"), "deny")
        ok = f"*** Begin Patch\n*** Update File: {DEV / OWNER / 'repo' / 'a.py'}\n@@\n-a\n+b\n*** End Patch"
        self.assertEqual(decision("apply_patch", {"command": ok}, agent="codex"), "allow")


class GuardBashTests(unittest.TestCase):
    def bash(self, cmd, cwd=None, agent="claude"):
        return decision("Bash", {"command": cmd}, cwd or str(DEV / OWNER / "repo"), agent)

    def test_worktree_add_outside_managed_denied(self):
        self.assertEqual(self.bash("git worktree add ../repo-pr12 -b feat/x"), "deny")
        self.assertEqual(self.bash("git worktree add .claude/worktrees/foo"), "deny")

    def test_worktree_add_managed_or_temp_allowed(self):
        self.assertEqual(self.bash(f"git worktree add {core.WT_ROOT}/{OWNER}/repo/feat-x -b feat/x"), "allow")
        self.assertEqual(self.bash("git worktree add /tmp/inspect-wt"), "allow")

    def test_cd_then_worktree_add_tracked(self):
        self.assertEqual(self.bash("cd .. && git worktree add repo-copy"), "deny")
        self.assertEqual(self.bash("cd /tmp\ngit worktree add wt1"), "allow")

    def test_clone_rules(self):
        self.assertEqual(self.bash("git clone https://github.com/acme/widget.git", cwd=str(HOME)), "deny")
        canon = DEV / "acme" / "widget"
        self.assertEqual(self.bash(f"git clone git@github.com:acme/widget.git {canon}"), "allow")
        self.assertEqual(self.bash("git clone https://github.com/acme/widget /tmp/widget"), "allow")
        self.assertEqual(self.bash("gh repo clone acme/widget", cwd=str(DEV)), "deny")

    def test_force_push_rules(self):
        with TempState():
            self.assertEqual(self.bash("git push --force"), "deny")
            self.assertEqual(self.bash("git push -fu origin feat/x"), "deny")
            self.assertEqual(self.bash("git push origin +feat/x"), "deny")
            self.assertEqual(self.bash("git push --force-with-lease origin main"), "deny")

    def test_push_budget(self):
        with TempState():
            repo = Path(core.STATE) / "r"
            core.sh(["git", "init", "-q", "-b", "feat/loop", str(repo)])
            cmd = "git push origin feat/loop"
            self.assertEqual(self.bash(cmd, cwd=str(repo)), "allow")
            self.assertEqual(self.bash(cmd, cwd=str(repo)), "allow")
            self.assertEqual(self.bash(cmd, cwd=str(repo)), "deny")
            self.assertEqual(self.bash("DEVKIT_LOCAL_CI_PASSED=1 " + cmd, cwd=str(repo)), "allow")

    def test_rerun_needs_flaky_ack(self):
        self.assertEqual(self.bash("gh run rerun 123 --failed"), "deny")
        self.assertEqual(self.bash("DEVKIT_FLAKY=1 gh run rerun 123 --failed"), "allow")

    def test_destructive_rm(self):
        self.assertEqual(self.bash(f"rm -rf {DEV}"), "deny")
        self.assertEqual(self.bash("rm -rf ~"), "deny")
        self.assertEqual(self.bash("rm -rf /"), "deny")
        self.assertEqual(self.bash("rm -rf .git"), "deny")
        self.assertEqual(self.bash("rm -rf *", cwd=str(DEV)), "deny")
        self.assertEqual(self.bash("rm -rf node_modules dist"), "allow")
        self.assertEqual(self.bash(f"rm -rf {core.TMP_ROOT}/some-scratch-repo"), "allow")
        self.assertEqual(self.bash("rm -rf /tmp/clone-to-inspect/.git"), "allow")
        self.assertEqual(self.bash("rm -f notes.txt"), "deny")  # steered to `dev rm` (recoverable)
        self.assertEqual(self.bash("git stash"), "deny")
        self.assertEqual(self.bash("git stash list"), "allow")
        self.assertEqual(self.bash("gh pr merge 12 --squash"), "deny")
        self.assertEqual(self.bash("DEVKIT_RAW_GH=1 gh pr merge 12 --squash"), "allow")
        self.assertEqual(self.bash("DEVKIT_RAW_GH=1 gh pr merge 12 --admin"), "deny")
        self.assertEqual(self.bash("gh release create v26.11.0"), "deny")
        self.assertEqual(self.bash("DEVKIT_RAW_FS=1 rm -f notes.txt"), "allow")
        self.assertEqual(self.bash(f"rm -f {core.TMP_ROOT}/scratch.txt"), "allow")
        self.assertEqual(self.bash("rm -rf __pycache__ .pytest_cache"), "allow")
        self.assertEqual(self.bash("find . -name '*.orig' -delete"), "deny")
        self.assertEqual(self.bash("find . -name '*.log' | xargs rm"), "deny")
        self.assertEqual(self.bash(f"find {core.TMP_ROOT}/x -delete"), "allow")

    def test_git_clean_ignored_denied(self):
        self.assertEqual(self.bash("git clean -fdx"), "deny")
        self.assertEqual(self.bash("git clean -fd"), "allow")

    def test_attribution_blocked(self):
        msg = 'git commit -m "feat: x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"'
        self.assertEqual(self.bash(msg), "deny")
        self.assertEqual(self.bash('gh pr create --title x --body "🤖 Generated with [Claude Code](https://claude.com)"'), "deny")
        self.assertEqual(self.bash('git commit -m "fix: handle empty input"'), "allow")
        self.assertEqual(self.bash('grep -r "Co-Authored-By: Claude" .'), "allow")

    def test_sibling_dirs_via_shell_denied(self):
        self.assertEqual(self.bash(f"mkdir -p {DEV}/kit-zz-copy"), "deny")
        self.assertEqual(self.bash(f"cp -R {DEV}/{OWNER}/repo {DEV}/repo-zz-copy"), "deny")
        self.assertEqual(self.bash(f"mkdir -p {DEV}/{OWNER}/newrepo/src"), "allow")

    def test_redirect_outside_roots(self):
        self.assertEqual(self.bash("echo hi > ~/Desktop/x.txt", agent="codex"), "deny")
        self.assertEqual(self.bash("echo hi > /dev/null"), "allow")
        self.assertEqual(self.bash("echo hi > /tmp/x.txt"), "allow")

    def test_unknown_shell_state_is_not_flagged(self):
        self.assertEqual(self.bash('cd "$E" && printf x > README.md'), "allow")
        self.assertEqual(self.bash('echo x > "$OUT/file.txt"', agent="codex"), "allow")
        self.assertEqual(self.bash('git worktree add "$(mktemp -d)/wt"'), "allow")
        self.assertEqual(self.bash("cd $SOMEWHERE && rm -rf build"), "allow")

    def test_fd_redirections(self):
        with TempState():
            self.assertEqual(self.bash("git push -q origin main 2>&1 | tail -2"), "allow")
            self.assertEqual(self.bash("git push origin feat/x >/dev/null 2>&1"), "allow")
            self.assertEqual(self.bash("make build 2> build.log"), "allow")
            self.assertEqual(self.bash("echo hi >&2"), "allow")
            self.assertEqual(self.bash("git push origin claude/x 2>&1"), "deny")

    def test_heredoc_bodies_are_data(self):
        cmd = "cat > page.html <<'EOF'\n<iframe src=x></iframe> > /etc/passwd\nrm -rf ~\nEOF\necho done"
        self.assertEqual(self.bash(cmd), "allow")
        self.assertEqual(self.bash("python3 - <<EOF\nprint('git push --force')\nEOF"), "allow")
        self.assertEqual(self.bash("cat <<EOF > ~/Desktop/x.txt\nhi\nEOF", agent="codex"), "deny")

    def test_unparseable_is_allowed(self):
        self.assertEqual(self.bash("echo 'unterminated"), "allow")


class BranchPolicyTests(unittest.TestCase):
    def test_names(self):
        good = ["feat/invoice-export", "feat/PROJ-142-invoice-export", "fix/login-timeout", "release/26.11.4",
                "release/v2.3.0", "integration/payments", "hotfix/2.3.1", "chore/deps-bump", "main", "develop",
                "feat/claude-desktop-parity", "refactor/agent-runtime"]
        bad = ["claude/vigilant-jang-ec71c0", "codex/fix-thing", "ai/refactor", "worktree-bold-oak-a3f2", "wip",
               "my-branch", "feature/x", "feat/", "feat/Invoice_Export", "feat/vigilant-jang-ec71c0", "pr13-local",
               "tmp/test"]
        for n in good:
            with self.subTest(good=n):
                self.assertIsNone(guard.branch_violation(n))
        for n in bad:
            with self.subTest(bad=n):
                self.assertIsNotNone(guard.branch_violation(n))

    def test_commands(self):
        cwd = str(DEV / OWNER / "repo")
        for cmd in ("git checkout -b claude/x-1", "git switch -c codex/y", "git branch worktree-abc",
                    "git worktree add -b ai/z /tmp/w", "git branch -m old claude/new",
                    "git push origin HEAD:claude/x", "gh pr create --head codex/x --fill"):
            with self.subTest(cmd=cmd):
                self.assertEqual(decision("Bash", {"command": cmd}, cwd), "deny")
        with TempState():
            for cmd in ("git checkout -b feat/invoice-export", "git switch -c fix/login-timeout",
                        "git branch -d claude/old-thing", "git push origin feat/invoice-export",
                        "git push origin v1.2.3", "git push --tags", "git branch --show-current"):
                with self.subTest(cmd=cmd):
                    self.assertEqual(decision("Bash", {"command": cmd}, cwd), "allow")


class CalverTests(unittest.TestCase):
    def test_next_version(self):
        import datetime as dt
        from devkit import cli
        with tempfile.TemporaryDirectory() as d:
            core.sh(["git", "init", "-q", d])
            core.sh(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"])
            self.assertEqual(cli.calver(d, dt.date(2026, 11, 5)), (None, "26.11.0"))
            for t in ("v26.10.4", "v26.11.0", "v26.11.1", "1.2.3"):
                core.sh(["git", "-C", d, "tag", t])
            self.assertEqual(cli.calver(d, dt.date(2026, 11, 20)), ("v26.11.1", "26.11.2"))
            self.assertEqual(cli.calver(d, dt.date(2026, 12, 1)), ("v26.11.1", "26.12.0"))


class PackagingTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_plugin_versions_match(self):
        v = (self.ROOT / "VERSION").read_text().strip()
        for m in ("plugin/.claude-plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
            with self.subTest(manifest=m):
                self.assertEqual(json.loads((self.ROOT / m).read_text())["version"], v)

    def test_marketplaces_point_at_plugin(self):
        cm = json.loads((self.ROOT / ".claude-plugin/marketplace.json").read_text())
        xm = json.loads((self.ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(cm["plugins"][0]["source"], "./plugin")
        self.assertEqual(xm["plugins"][0]["source"]["path"], "./plugin")
        for skill in ("handoff", "workspace", "delivery", "backend"):
            self.assertTrue((self.ROOT / "plugin/skills" / skill / "SKILL.md").exists())
        for asset in ("plugin/assets/icon.png", "plugin/assets/logo.png"):
            self.assertTrue((self.ROOT / asset).exists())


class ParsingTests(unittest.TestCase):
    def test_parse_remote(self):
        self.assertEqual(core.parse_remote("https://github.com/Acme/Widget.git"), ("github.com", "Acme", "Widget"))
        self.assertEqual(core.parse_remote("git@github.com:acme/widget.git"), ("github.com", "acme", "widget"))
        self.assertEqual(core.parse_remote("ssh://git@github.com/acme/widget"), ("github.com", "acme", "widget"))
        self.assertEqual(core.parse_remote("acme/widget"), ("github.com", "acme", "widget"))
        self.assertIsNone(core.parse_remote("not a url"))

    def test_other_hosts(self):
        self.assertEqual(core.parse_remote("git@gitlab.com:group/sub/svc.git"), ("gitlab.com", "group/sub", "svc"))
        self.assertEqual(core.parse_remote("https://user@bitbucket.org/ws/repo.git"), ("bitbucket.org", "ws", "repo"))
        self.assertEqual(core.parse_remote("https://bitbucket.acme.io/scm/proj/repo.git"), ("bitbucket.acme.io", "proj", "repo"))
        self.assertEqual(core.parse_remote("https://dev.azure.com/org/project/_git/repo"), ("dev.azure.com", "org/project", "repo"))
        self.assertEqual(core.parse_remote("git@ssh.dev.azure.com:v3/org/project/repo"), ("dev.azure.com", "org/project", "repo"))
        self.assertEqual(core.parse_remote("gitlab.com/group/repo"), ("gitlab.com", "group", "repo"))
        pid = core.project_identity(Path("/x/svc"), "git@gitlab.com:Group/Sub/Svc.git")[0]
        self.assertEqual(pid, "gitlab.com/group/sub/svc")
        self.assertEqual(core.canonical_path(pid), DEV / "gitlab.com" / "group" / "sub" / "svc")
        self.assertEqual(core.clone_source("gitlab.com/group/repo")[1], "https://gitlab.com/group/repo.git")

    def test_clone_other_hosts_guard(self):
        cwd = str(DEV)
        canon = DEV / "gitlab.com" / "group" / "svc"
        self.assertEqual(decision("Bash", {"command": f"git clone git@gitlab.com:group/svc.git {canon}"}, cwd), "allow")
        self.assertEqual(decision("Bash", {"command": "git clone git@gitlab.com:group/svc.git"}, cwd), "deny")
        self.assertEqual(decision("Write", {"file_path": str(canon / "README.md")}, cwd), "allow")

    def test_project_identity_and_canonical(self):
        pid, owner, repo = core.project_identity(Path("/x/Widget"), "https://github.com/Acme/Widget.git")
        self.assertEqual(pid, "acme/widget")
        self.assertEqual(core.canonical_path(pid), DEV / "acme" / "widget")
        self.assertEqual(core.project_identity(Path("/x/Thing"), "")[0], "_local/Thing")

    def test_parse_status(self):
        s = registry.parse_status("## feat/a.b...origin/feat/a.b [ahead 2, behind 1]\n M x\n?? y\n?? z")
        self.assertEqual((s["branch"], s["upstream"], s["ahead"], s["behind"], s["modified"], s["untracked"]),
                         ("feat/a.b", "origin/feat/a.b", 2, 1, 1, 2))
        self.assertIsNone(registry.parse_status("## HEAD (no branch)")["branch"])
        self.assertEqual(registry.parse_status("## No commits yet on main")["branch"], "main")
        self.assertTrue(registry.parse_status("## x...origin/x [gone]")["upstream_gone"])


class CiTests(unittest.TestCase):
    WF = """name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    name: Tests
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node: [20, 22]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node }}
      - name: Install
        run: npm ci
      - name: Check
        run: |
          npm run lint
          npm test -- --coverage
"""

    def test_parse(self):
        wf = ci.parse_workflow(self.WF)
        self.assertEqual(wf["name"], "CI")
        self.assertIn("push", wf["on"])
        self.assertIn("pull_request", wf["on"])
        job = wf["jobs"][0]
        self.assertEqual((job["id"], job["name"], job["runs_on"]), ("test", "Tests", "ubuntu-latest"))
        self.assertTrue(any("node" in m for m in job["matrix"]))
        runs = [s["run"] for s in job["steps"] if s["run"]]
        self.assertEqual(runs, ["npm ci", "npm run lint\nnpm test -- --coverage"])

    def test_local_commands(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".github" / "workflows").mkdir(parents=True)
            (Path(d) / ".github" / "workflows" / "ci.yml").write_text(self.WF)
            self.assertEqual(ci.local_commands(Path(d))[0], "npm ci")


class CanaryTests(unittest.TestCase):
    def test_marker_detection(self):
        self.assertTrue(canary.has_marker("[Firefly> DevKit] kit@main · done"))
        self.assertTrue(canary.has_marker("\n**[Firefly> DevKit]** kit · x"))
        self.assertTrue(canary.has_marker("`[firefly> devkit]` kit"))
        self.assertTrue(canary.has_marker("DevKit · kit · done"))  # legacy marker, still accepted
        self.assertFalse(canary.has_marker("Done. [Firefly> DevKit] later"))
        self.assertFalse(canary.has_marker("[Firefly] DevKit kit"))
        self.assertFalse(canary.has_marker("Here is the fix"))

    def test_status_text_keeps_ids(self):
        self.assertEqual(canary.status_text("DevKit · _local/x_y@feat/a_b · done · `tests` ok · next: -"),
                         "_local/x_y@feat/a_b · done · tests ok · next: -")
        self.assertEqual(canary.status_text("**DevKit** · api@main · fixed"), "api@main · fixed")
        self.assertEqual(canary.status_text("[Firefly> DevKit] api@main · fixed · tests ok · next: -"),
                         "api@main · fixed · tests ok · next: -")
        self.assertEqual(canary.status_text("**[Firefly> DevKit]** · api@main · fixed"), "api@main · fixed")

    def test_canary_status_block(self):
        block = ("**[Firefly> DevKit]** api · `feat/x_y` · Invoice export\n"
                 "- **Done:** added the CSV export endpoint.\n"
                 "- **Check:** ✓ 12 tests pass\n"
                 "- **Progress:** 2 of 3 steps · 20 min of ~30\n"
                 "- **Next:** open the PR.\n\nDetails follow.")
        self.assertTrue(canary.has_marker(block))
        p = canary.parse_status(block)
        self.assertEqual((p["project"], p["branch"], p["task"]), ("api", "feat/x_y", "Invoice export"))
        self.assertEqual((p["check"], p["next"]), ("✓ 12 tests pass", "open the PR."))
        line = canary.status_text(block)
        self.assertEqual(line, "api · feat/x_y ❯ Invoice export ❯ added the CSV export endpoint. ❯ ✓ 12 tests pass ❯ "
                               "▸ 2 of 3 steps · 20 min of ~30 ❯ next → open the PR.")
        self.assertEqual(canary.format_issues(block), [])
        self.assertIn("Check    ", ui.status_block(line, color=False))
        self.assertIn("\033[48;2;255;179;74m", ui.status_block(line, color=True))
        # older one-line forms are still understood, but scored as not following the format
        old = "`[Firefly> DevKit]` `api ⎇ main` `T-fwk7a2xyz1` ❯ fixed ❯ ✓ tests ❯ next → push"
        self.assertTrue(canary.has_marker(old))
        self.assertEqual(canary.status_text(old), "api ⎇ main ❯ T-fwk7a2xyz1 ❯ fixed ❯ ✓ tests ❯ next → push")
        self.assertTrue(canary.format_issues(old))
        self.assertEqual(canary.status_text("DevKit · api@main · fixed"), "api@main · fixed")
        bad = canary.format_issues("**[Firefly> DevKit]** api · `main` · x\n- **Done:** see T-fwk7a2xyz1\n- **Check:** ok")
        self.assertTrue(any("Next" in x for x in bad) and any("✓" in x for x in bad) and any("IDs" in x for x in bad))

    def test_check_records(self):
        with TempState():
            cwd = str(DEV)
            self.assertEqual(canary.check("codex", {"cwd": cwd, "last_assistant_message": "[Firefly> DevKit] x@main · ok"})[0], True)
            self.assertEqual(canary.check("codex", {"cwd": cwd, "last_assistant_message": "no marker"})[0], False)
            self.assertIsNone(canary.check("codex", {"cwd": "/tmp", "last_assistant_message": "x"}))
            self.assertEqual(canary.summary(1), {"codex": (1, 2)})

    def test_transcript_reader(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.jsonl"
            lines = [{"type": "user", "message": {"role": "user", "content": "do it"}},
                     {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working"}]}},
                     {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
                     {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                     {"type": "assistant", "message": {"content": [{"type": "text", "text": "DevKit · x · done"}]}}]
            f.write_text("\n".join(json.dumps(x) for x in lines))
            self.assertEqual(canary.last_assistant_text(str(f)), "DevKit · x · done")


class NoteTests(unittest.TestCase):
    def test_session_line_replaced_by_key(self):
        with tempfile.TemporaryDirectory() as d:
            old = registry.NOTES
            try:
                import devkit.core as c
                c_notes = Path(d)
                registry.NOTES = c_notes
                core.NOTES = c_notes
                pid = "acme/widget"
                f = core.NOTES / "acme" / "widget.md"
                f.parent.mkdir(parents=True)
                f.write_text(registry.NOTE_TEMPLATE.format(title=pid, auto_start=registry.AUTO_START,
                                                           auto_end=registry.AUTO_END, log_header=registry.LOG_HEADER))
                orig = core.note_path
                core.note_path = lambda p: c_notes / (p + ".md")
                registry.note_path = core.note_path
                registry.add_session_line(pid, "line-one-zz", "abc")
                registry.add_session_line(pid, "second", "abc")
                registry.add_session_line(pid, "other", "def")
                text = f.read_text()
                self.assertNotIn("line-one-zz", text)
                self.assertEqual(text.count("<!-- s:abc -->"), 1)
                self.assertLess(text.index("other"), text.index("second"))
                digest = registry.human_sections(pid)
                self.assertNotIn("dev:auto", digest)
                self.assertNotIn("status: active", digest)
                self.assertIn("Recent sessions", digest)
            finally:
                registry.NOTES = old
                core.NOTES = old
                core.note_path = orig
                registry.note_path = orig



@unittest.skipUnless(core.have("gitleaks"), "gitleaks not installed")
class CommitSecretTests(unittest.TestCase):
    """The guard checks what a commit will contain, also when `git add` runs in the same command."""

    def test_add_then_commit_in_one_command(self):
        fake = "ghp_" + "0123456789abcdefABCDEF0123456789abcd"  # built at runtime so this file holds no token
        with tempfile.TemporaryDirectory() as d:
            ident = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
            core.sh(["git", "init", "-q", d])
            core.sh(["git", *ident, "-C", d, "commit", "-q", "--allow-empty", "-m", "init"])
            Path(d, "a.py").write_text(f'TOKEN = "{fake}"\n')
            Path(d, "b.py").write_text("x = 1\n")
            saved = guard.is_temp
            guard.is_temp = lambda p: False
            try:
                for cmd in ("git add a.py && git commit -m x", "git add -A && git commit -m x",
                            "git add a.py; git commit -m 'feat: x'"):
                    with self.subTest(cmd=cmd):
                        self.assertEqual(decision("Bash", {"command": cmd}, cwd=d), "deny")
                self.assertEqual(decision("Bash", {"command": "git add b.py && git commit -m x"}, cwd=d), "allow")
                self.assertEqual(decision("Bash", {"command": "git commit -m x"}, cwd=d), "allow")
            finally:
                guard.is_temp = saved
            self.assertEqual(core.gitout(d, "diff", "--cached", "--name-only"), "")  # the real index is untouched


if __name__ == "__main__":
    unittest.main()
