# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Replay CLI launch and guard handlers with private repositories, never live host hooks."""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import core, guard, hooks, progress  # noqa: E402
from tests.test_progress import TempState  # noqa: E402


class CodexCliScratchTests(unittest.TestCase):
    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.home = Path(state.tmp.name).resolve()
        self.dev = self.home / "Development"
        self.host = self.dev / "acme/api"
        subprocess.run(["git", "init", "-q", str(self.host)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.host), "remote", "add", "origin",
                        "https://github.com/acme/api.git"], check=True, capture_output=True)
        self.tmp = self.dev / ".tmp"
        self.own = self.tmp / "codex/chat/scratchpad/seraph"
        self.session = progress.session_path("codex", "chat")
        self.record = {"agent": "codex", "session": "chat", "cwd": str(self.host)}
        self.session.write_text(json.dumps(self.record))
        for module, key, value in ((core, "HOME", self.home), (guard, "HOME", self.home),
                                   (core, "DEV", self.dev), (guard, "DEV", self.dev),
                                   (core, "TMP_ROOT", self.tmp), (core, "WT_ROOT", self.dev / ".worktrees")):
            patch = mock.patch.object(module, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(core, "temp_roots", return_value=[str(self.tmp)])
        patch.start()
        self.addCleanup(patch.stop)
        progress.RUNS.mkdir(exist_ok=True)
        progress.run_path("codex", "chat").write_text(json.dumps({
            "agent": "codex", "session": "chat", "state": "running", "project": "acme/api",
            "agents": {"child": {"persona": "seraph", "state": "running", "binding": "bound",
                                  "scope": ["lib/**"]}}, "plan": [], "messages": [],
        }))

    def hook(self, path=None, cmd=None, agent="codex", **updates):
        inp = {"cmd": cmd, "workdir": str(self.host)} if cmd else {"file_path": str(path), "content": "notes"}
        data = dict(session_id="chat", agent_id="child", cwd=str(self.host), tool_input=inp,
                    tool_name="exec_command" if cmd else "Write")
        data.update(updates)
        held = {}
        with mock.patch.object(hooks, "_PAYLOAD", data), mock.patch.object(hooks, "_headless", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            hooks._guard(agent, data, held)
            # A stamped shell is judged again in its routed lane. Replay that second boundary too.
            if cmd and not held.get("verdict"):
                # Earlier releases have no event context; still exercise their lane verdict when proving the test.
                context = guard.codex_scratch_context(agent, data) if hasattr(guard, "codex_scratch_context") else \
                    contextlib.nullcontext()
                with context:
                    why = hooks._lane_verdict(data, agent, cmd)
                if why:
                    return "deny", why
        return held.get("verdict")

    def test_launch_creates_cli_folder_that_readonly_member_can_use(self):
        data = dict(session_id="chat", agent_id="child", agent_type="seraph", cwd=str(self.host))
        out = io.StringIO()
        with mock.patch.object(hooks, "STATE", self.home), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.subagent_start("codex")
        self.assertTrue(self.own.is_dir())
        self.assertIn(str(self.own), json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        script = self.own / "probe.py"
        script.write_text("print('probe')\n")
        self.assertIsNone(self.hook(self.own / "notes.md"))
        commands = (
            (f"echo notes > {self.own}/notes.md", ["/bin/sh", "-c", 'printf "%s\\n" notes > "$1"',
                                                  "scratch-write", str(self.own / "notes.md")]),
            (f"mkdir -p {self.own}/nested", ["mkdir", "-p", str(self.own / "nested")]),
            (f"python3 {script}", ["python3", str(script)]),
        )
        for cmd, argv in commands:
            self.assertIsNone(self.hook(cmd=cmd), cmd)
            result = subprocess.run(argv, cwd=self.host, check=True, capture_output=True, text=True)
        self.assertEqual(result.stdout, "probe\n")
        self.assertEqual((self.own / "notes.md").read_text(), "notes\n")
        self.assertTrue((self.own / "nested").is_dir())
        self.assertEqual(self.hook(cmd="python3 -c 'print(1)'")[0], "deny")
        checkout = self.own / "checkout"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        self.assertEqual(self.hook(checkout / "code.py")[0], "deny")

    def test_reserved_cli_namespace_needs_own_session_and_member(self):
        self.assertIsNone(self.hook(self.own / "n.md"))
        for path in (self.tmp / "codex/other/scratchpad/seraph/n.md", self.own.parent / "neo/n.md",
                     self.own.parent / "main/n.md", self.own / "../neo/n.md"):
            for cmd in (None, f"echo notes > {path}", f"mkdir -p {path}"):
                with self.subTest(path=path, cmd=cmd):
                    self.assertEqual(self.hook(path, cmd=cmd)[0], "deny")
        for updates in ({"session_id": "missing"}, {"agent_id": "unknown"}, {"cwd": str(self.home)},
                        {"session_id": "../chat"}, {"session_id": "chat/../chat"}, {"session_id": "chat\0"}):
            with self.subTest(updates=updates):
                self.assertEqual(self.hook(self.own / "n.md", **updates)[0], "deny")
        for changed in ({"agent": "claude"}, {"session": "other"}, {"cwd": str(self.home)}):
            self.session.write_text(json.dumps(dict(self.record, **changed)))
            self.assertEqual(self.hook(self.own / "n.md")[0], "deny")

    def test_cli_registration_rejects_unmanaged_cwd_and_linked_roots(self):
        self.assertIsNone(self.hook(cmd=f"mkdir -p {self.own}"))
        for root in (self.home / "unknown", self.dev / "unmanaged"):
            subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
            self.session.write_text(json.dumps(dict(self.record, cwd=str(root))))
            self.assertEqual(self.hook(self.own / "n.md", cwd=str(root))[0], "deny")
        self.session.write_text(json.dumps(self.record))
        self.own.mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.own / "escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.hook(self.own / "escape/n.md")[0], "deny")
        self.assertEqual(self.hook(cmd=f"echo notes > {self.own}/escape/n.md")[0], "deny")
        for malformed in ("../chat", "chat/../chat", "chat\0", "chat.name", "a" * 129):
            data = dict(session_id=malformed, agent_id="child", cwd=str(self.host))
            self.assertIsNone(hooks._make_scratch_folder("codex", data))
        for root in (self.own, self.own.parent, self.own.parent.parent, self.tmp / "codex", self.tmp):
            moved = root.with_name(root.name + "-moved")
            root.rename(moved)
            root.symlink_to(moved, target_is_directory=True)
            data = dict(session_id="chat", agent_id="child", cwd=str(self.host))
            self.assertIsNone(hooks._make_scratch_folder("codex", data))
            self.assertEqual(self.hook(self.own / "n.md")[0], "deny")
            root.unlink()
            moved.rename(root)

    def test_claude_retains_temp_policy_without_cli_launch_exception(self):
        self.assertIsNone(self.hook(cmd=f"mkdir -p {self.own}"))
        data = dict(session_id="chat", agent_id="child", cwd=str(self.host))
        self.assertIsNone(hooks._make_scratch_folder("claude", data))
        self.assertFalse(self.own.exists())
        self.assertIsNone(self.hook(self.own / "n.md", agent="claude"))
        self.assertTrue(guard.member_write_exempt(self.own / "n.md", self.host, member="seraph"))


if __name__ == "__main__":
    unittest.main()
