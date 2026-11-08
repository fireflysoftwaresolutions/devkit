"""The CI watch check wired into the Stop hook and the end-of-turn checklist (scratch verification of the hunks)."""
import contextlib
import io
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import canary, cistatus, ciwatch, hooks, progress as pg  # noqa: E402
from test_hooks_wiring import GIT_ENV, WiringCase, git  # noqa: E402

LINE = "**[Firefly> DevKit]** api · `feat/x` · Ship\n- **Done:** x\n- **Check:** ✓ y\n- **Next:** CI will tell me"


class WatchGateWiringTests(WiringCase):
    def setUp(self):
        super().setUp()
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=GIT_ENV)
        git(self.repo, "remote", "add", "origin", str(remote))
        git(self.repo, "push", "-q", "origin", "HEAD:refs/heads/main")
        git(self.repo, "switch", "-qc", "feat/x")
        (self.repo / "README.md").write_text("feature\n")
        git(self.repo, "commit", "-qam", "feature")
        git(self.repo, "push", "-q", "-u", "origin", "feat/x")
        self.sha = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                                  check=True).stdout.strip()
        pg.session_path("claude", "s1").write_text(json.dumps({"agent": "claude", "session": "s1",
                                                               "top": str(self.repo), "project": "acme/api"}))
        pg.session_path("codex", "s1").write_text(json.dumps({"agent": "codex", "session": "s1",
                                                              "top": str(self.repo), "project": "acme/api"}))
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 30))
        self.transcript = self.root / "t.jsonl"
        self.transcript.write_text("\n".join(json.dumps(r) for r in (
            {"type": "user", "message": {"role": "user", "content": "ship it"}, "timestamp": ts},
            {"type": "assistant", "timestamp": ts, "cwd": str(self.repo), "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_p", "name": "Bash", "input": {"command": "git push origin feat/x"}}]}},
            {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_p", "content": "ok"}]}})) + "\n")
        running = {"provider": "github", "repo": "acme/api",
                   "runs": [{"id": "9", "name": "ci", "number": 14, "sha": self.sha[:7], "status": "running"}]}
        for p in (mock.patch.object(cistatus, "runs_for", lambda *a, **k: running),
                  mock.patch.object(ciwatch, "LIVE", self.root / "ci-live"),
                  mock.patch.object(ciwatch, "WATCH", self.root / "ci-watch"),
                  mock.patch.object(canary, "STATE", self.root),
                  mock.patch.object(canary, "check", lambda *a, **k: (True, "acme/api", LINE)),
                  mock.patch("devkit.scope.decide", lambda *a, **k: True)):
            p.start()
            self.addCleanup(p.stop)

    def stop(self, agent="claude", **extra):
        out = io.StringIO()
        payload = dict({"session_id": "s1", "hook_event_name": "Stop", "cwd": str(self.repo),
                        "transcript_path": str(self.transcript), "last_assistant_message": LINE}, **extra)
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: None):
            hooks.run("stop", agent)
        text = out.getvalue().strip()
        return json.loads(text) if text else {}

    def test_claude_stop_sends_back_a_turn_that_promised_ci_with_nothing_watching(self):
        out = self.stop()
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn(f"dev ci watch --wait --branch feat/x --sha {self.sha[:7]} --timeout 28", out["reason"])
        self.assertIn("Monitor", out["reason"])

    def test_codex_stop_says_to_wait_in_the_foreground(self):
        out = self.stop("codex")
        self.assertEqual(out.get("decision"), "block", out)
        self.assertIn("foreground", out["reason"])

    def test_the_turn_is_sent_back_once(self):
        self.assertNotIn("dev ci watch --wait", (self.stop(stop_hook_active=True).get("reason") or ""))

    def test_the_closing_reminder_after_the_push_names_the_watch_to_arm(self):
        call = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_use_id": "toolu_p",
                "tool_input": {"command": "git push origin feat/x"}, "cwd": str(self.repo),
                "transcript_path": str(self.transcript), "tool_response": {"stdout": ""}}
        self.assertIn("dev ci watch --wait", self.context(self.hook("post-tool", call)))


if __name__ == "__main__":
    unittest.main()
