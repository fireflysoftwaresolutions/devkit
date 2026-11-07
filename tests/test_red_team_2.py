# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Red team (Agent Smith), part 2, with the code reviewer's heredoc and MCP findings: each test reproduces a bypass of
# the part-1 and part-2 fixes and asserts the fixed behavior, next to the ordinary commands each fix must leave alone.
# Records go through the real audit.record into a temp trail, never a stub (L-fxzc02legsi).
# Run: python3 -m unittest tests.test_red_team_2 -v
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, core, guard  # noqa: E402


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
        for p in (mock.patch.object(audit, "AUDIT", root / "audit"),
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


if __name__ == "__main__":
    unittest.main()
