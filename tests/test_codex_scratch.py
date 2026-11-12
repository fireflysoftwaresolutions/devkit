# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Guard contracts for the registered Codex app chat's work and output folders.

The generated Documents/Codex cwd and refusal were observed on 2026-11-10. These
tests replay the guard against private filesystem/session fixtures, not live hooks.
"""
import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))
from devkit import core, guard, hooks, progress  # noqa: E402
from test_progress import TempState  # noqa: E402


class CodexScratchTests(unittest.TestCase):
    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.home = Path(state.tmp.name).resolve()
        self.host = self.home / "Documents/Codex/2026-11-10/chat"
        self.host.mkdir(parents=True)
        self.session = progress.session_path("codex", "chat")
        self.record = {"agent": "codex", "session": "chat", "cwd": str(self.host)}
        self.session.write_text(json.dumps(self.record))
        for patch in (mock.patch.object(core, "HOME", self.home),
                      mock.patch.object(guard, "HOME", self.home),
                      # The fixture represents Documents, not the system temp directory that contains the test.
                      mock.patch.object(core, "temp_roots", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, path, *, sid="chat", agent="codex", tool="Write", aid=None):
        inp = {"file_path": str(path), "content": "notes"}
        if tool == "exec_command":
            inp = {"cmd": f"echo notes > {path}", "workdir": str(self.host)}
        call = {"session_id": sid, "cwd": str(self.host), "tool_name": tool, "tool_input": inp}
        if aid:
            call["agent_id"] = aid
        return guard.check(tool, inp, str(self.host), agent, call=call)

    def test_main_writes_only_registered_chat_work_and_outputs(self):
        for tool in ("Write", "exec_command"):
            with self.subTest(tool=tool):
                for folder in ("work", "outputs"):
                    self.assertIsNone(self.check(self.host / folder / "notes.md", tool=tool))
                for path in (self.host / "notes.md", self.host.parent / "other/work/notes.md",
                             self.host / "work/../../other/work/notes.md"):
                    self.assertEqual(self.check(path, tool=tool)[0], "deny")
                self.assertEqual(self.check(self.host / "work/n.md", sid="missing", tool=tool)[0], "deny")
                self.assertEqual(self.check(self.host / "work/n.md", agent="claude", tool=tool)[0], "ask")

    def test_scratch_rejects_symlink_escape_and_mismatched_record(self):
        self.assertIsNone(self.check(self.host / "work/notes.md"))
        (self.host / "work").mkdir()
        outside = self.home / "elsewhere"
        outside.mkdir()
        (self.host / "work/escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.check(self.host / "work/escape/n.md")[0], "deny")
        with mock.patch.object(core, "temp_roots", return_value=[str(outside)]):
            self.assertEqual(self.check(self.host / "work/escape/n.md")[0], "deny")
            self.assertEqual(self.check(self.host / "work/escape/n.md", tool="exec_command")[0], "deny")
        for changed in ({"session": "another"}, {"agent": "claude"}, {"cwd": str(outside)},
                        {"cwd": str(self.host) + "\0"}):
            self.session.write_text(json.dumps(dict(self.record, **changed)))
            self.assertEqual(self.check(self.host / "work/n.md")[0], "deny")
        self.session.write_text(json.dumps(self.record))
        self.assertEqual(self.check(self.host / "work/n.md", aid={"bad": "identity"})[0], "deny")
        self.host.rename(self.host.with_name("moved"))
        self.host.symlink_to(self.host.with_name("moved"), target_is_directory=True)
        self.assertEqual(self.check(self.host / "work/n.md")[0], "deny")

    def register_member(self):
        progress.RUNS.mkdir(exist_ok=True)
        progress.run_path("codex", "chat").write_text(json.dumps({
            "agent": "codex", "session": "chat", "state": "running", "project": "acme/api",
            "agents": {"child": {"persona": "seraph", "state": "running", "binding": "bound",
                                  "scope": ["lib/**"]}}, "plan": [], "messages": [],
        }))

    def hook(self, path=None, cmd=None, aid="child", agent="codex"):
        inp = {"cmd": cmd, "workdir": str(self.host)} if cmd else {"file_path": str(path), "content": "notes"}
        data = {"session_id": "chat", "agent_id": aid, "cwd": str(self.host), "tool_input": inp,
                "tool_name": "exec_command" if cmd else "Write"}
        held = {}
        with mock.patch.object(hooks, "_PAYLOAD", data), mock.patch.object(hooks, "_headless", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            hooks._guard(agent, data, held)
            if cmd and not held.get("verdict"):
                # The routed shell checks a review lane separately from workspace access.
                context = guard.codex_scratch_context(agent, data) if hasattr(guard, "codex_scratch_context") else \
                    contextlib.nullcontext()
                with context:
                    why = hooks._lane_verdict(data, agent, cmd)
                if why:
                    return "deny", why
        return held.get("verdict")

    def test_readonly_member_uses_own_folder_through_both_hook_checks(self):
        self.register_member()
        own = self.host / "work/scratchpad/seraph"
        own.mkdir(parents=True)
        script = own / "probe.py"
        script.write_text("print('probe')\n")
        self.assertIsNone(self.hook(own / "notes.md"))
        commands = (
            (f"echo notes > {own}/notes.md", ["/bin/sh", "-c", 'printf "%s\\n" notes > "$1"',
                                             "scratch-write", str(own / "notes.md")]),
            (f"mkdir -p {own}/nested", ["mkdir", "-p", str(own / "nested")]),
            (f"cp {own}/notes.md {own}/copy.md", ["cp", str(own / "notes.md"), str(own / "copy.md")]),
        )
        for cmd, argv in commands:
            self.assertIsNone(self.hook(cmd=cmd), cmd)
            subprocess.run(argv, cwd=self.host, check=True, capture_output=True, text=True)
        # a read-only lane runs no program of its own (its scratch script included): the shell writes there
        self.assertEqual(self.hook(cmd=f"python3 {script}")[0], "deny")
        self.assertEqual((own / "copy.md").read_text(), "notes\n")
        self.assertEqual((own / "notes.md").read_text(), "notes\n")
        self.assertTrue((own / "nested").is_dir())
        self.assertEqual(self.hook(cmd="python3 -c 'print(1)'")[0], "deny")
        for path in (self.host / "outputs/result.md", self.host / "work/notes.md",
                     self.host / "work/scratchpad/neo/n.md", own.parent / "seraph-other/n.md"):
            self.assertEqual(self.hook(path)[0], "deny")
            self.assertEqual(self.hook(cmd=f"echo notes > {path}")[0], "deny")
        self.assertEqual(self.hook(own / "n.md", aid="unknown")[0], "deny")
        # A completed check cannot lend its authority to a later call with no matching event.
        self.assertEqual(guard.check("Write", {"file_path": str(own / "n.md")}, str(self.host), "codex")[0], "deny")
        with self.assertRaises(RuntimeError):
            with guard.codex_scratch_context("codex", {"session_id": "chat", "agent_id": "child",
                                                       "cwd": str(self.host)}):
                self.assertTrue(guard.member_write_exempt(own / "n.md", str(self.host), member="seraph"))
                raise RuntimeError("interrupted hook")
        self.assertFalse(guard.member_write_exempt(own / "n.md", str(self.host), member="seraph"))
        checkout = own / "checkout"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        self.assertEqual(self.hook(checkout / "code.py")[0], "deny")

    def test_current_session_does_not_authorize_another_chat_or_a_symlinked_ancestor(self):
        self.assertIsNone(self.check(self.host / "outputs/n.md"))
        other = self.host.with_name("other")
        other.mkdir()
        progress.session_path("codex", "other").write_text(json.dumps(dict(self.record, session="other", cwd=str(other))))
        self.assertEqual(self.check(other / "outputs/n.md")[0], "deny")
        self.assertEqual(self.check(self.host / "outputs/n.md", sid="other")[0], "deny")
        ancestor = self.host.parent
        moved = ancestor.with_name("moved-date")
        ancestor.rename(moved)
        ancestor.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.check(self.host / "outputs/n.md")[0], "deny")

    def test_subagent_start_creates_and_names_only_its_registered_app_scratch(self):
        self.register_member()
        own = self.host / "work/scratchpad/seraph"
        data = {"session_id": "chat", "agent_id": "child", "agent_type": "seraph", "cwd": str(self.host)}
        out = io.StringIO()
        with mock.patch.object(hooks, "STATE", self.home), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.subagent_start("codex")
        self.assertTrue(own.is_dir())
        context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(own), context)
        self.assertFalse((self.host / "scratchpad").exists())
        own.rmdir()
        for updates in ({"agent_id": "unknown"}, {"session_id": "another"}, {"cwd": str(self.home)}):
            hooks._make_scratch_folder("codex", dict(data, **updates))
            self.assertFalse(own.exists())
        for changed in ({"session": "another"}, {"agent": "claude"}, {"cwd": str(self.host) + "\0"}):
            self.session.write_text(json.dumps(dict(self.record, **changed)))
            hooks._make_scratch_folder("codex", data)
            self.assertFalse(own.exists())
        self.session.write_text(json.dumps(self.record))
        outside = self.home / "outside"
        outside.mkdir()
        own.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(hooks._make_scratch_folder("codex", data))
        self.assertEqual(list(outside.iterdir()), [])
        own.unlink()
        self.host.rename(self.host.with_name("moved"))
        self.host.symlink_to(self.host.with_name("moved"), target_is_directory=True)
        self.assertIsNone(hooks._make_scratch_folder("codex", data))
        self.assertFalse(own.exists())


if __name__ == "__main__":
    unittest.main()
