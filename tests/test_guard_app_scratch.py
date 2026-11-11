# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Registered app scratch stays rooted in the host when hook cwd is a bound checkout.

These are isolated hook-envelope contracts, not captured live-host acceptance.
"""
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
from tests.test_progress import TempState, repo_with_worktrees  # noqa: E402


class AppScratchCheckoutTests(unittest.TestCase):
    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.home = Path(state.tmp.name).resolve()
        self.dev = self.home / "Development"
        self.repo, self.worktree = repo_with_worktrees(
            self.dev / "acme", self.dev / ".worktrees/acme/api/fix/x0")
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin",
                        "https://github.com/acme/api.git"], check=True, capture_output=True)
        self.host = self.home / "Documents/Codex/chat"
        self.own = self.host / "work/scratchpad/seraph"
        self.own.mkdir(parents=True)
        self.record = dict(agent="codex", session="chat", cwd=str(self.host),
                           top=str(self.repo), project="acme/api")
        self.session = progress.session_path("codex", "chat")
        self.save_record()
        self.run = dict(agent="codex", session="chat", state="running", project="acme/api",
                        agents={"child": dict(persona="seraph", state="running", binding="bound",
                                              scope=["lib/**"])}, plan=[], messages=[])
        progress.RUNS.mkdir(exist_ok=True)
        self.save_run()
        for module, name, value in ((core, "HOME", self.home), (guard, "HOME", self.home),
                                    (core, "DEV", self.dev), (guard, "DEV", self.dev),
                                    (core, "WT_ROOT", self.dev / ".worktrees")):
            patcher = mock.patch.object(module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(core, "temp_roots", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_record(self, **changes):
        self.session.write_text(json.dumps(dict(self.record, **changes)))

    def save_run(self, **changes):
        progress.run_path("codex", "chat").write_text(json.dumps(dict(self.run, **changes)))

    def event(self, path=None, tool="Bash", cwd=None, **changes):
        path = path or self.own / "integration-ci.log"
        inp = ({"command": f"echo notes > {path}", "workdir": str(self.repo)} if tool == "Bash" else
               {"cmd": f"echo notes > {path}", "workdir": str(self.repo)} if tool == "exec_command" else
               {"file_path": str(path), "content": "notes"})
        return dict(dict(session_id="chat", agent_id="child", cwd=str(cwd or self.repo),
                         tool_name=tool, tool_input=inp), **changes)

    def check(self, event, agent="codex"):
        return guard.check(event["tool_name"], event["tool_input"], event["cwd"], agent, call=event)

    def hook(self, event):
        held = {}
        with mock.patch.object(hooks, "_PAYLOAD", event), mock.patch.object(hooks, "_headless", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            hooks._guard("codex", event, held)
            if not held.get("verdict") and event["tool_name"] in ("Bash", "exec_command"):
                with guard.codex_scratch_context("codex", event):
                    inp = event["tool_input"]
                    why = hooks._lane_verdict(event, "codex", inp.get("command") or inp["cmd"])
                if why:
                    return "deny", why
        return held.get("verdict")

    def test_registered_checkout_envelopes_keep_the_host_scratch_base(self):
        for tool in ("Bash", "exec_command", "Write"):
            with self.subTest(tool=tool):
                event = self.event(tool=tool)
                self.assertIsNone(self.check(event))
                self.assertIsNone(self.hook(event))
        for path in (self.host / "work/scratchpad/neo/n.md", self.host / "outputs/n.md",
                     self.host.parent / "other/work/scratchpad/seraph/n.md"):
            self.assertEqual(self.check(self.event(path))[0], "deny")
        self.assertIsNone(self.check(self.event(self.host / "outputs/n.md", agent_id=None)))

    def test_registered_managed_worktree_and_subdirectory_are_supported(self):
        self.save_record(top=str(self.worktree))
        subdir = self.worktree / "lib"
        subdir.mkdir()
        for cwd in (self.worktree, subdir):
            self.assertIsNone(self.check(self.event(cwd=cwd)))
        self.assertEqual(self.check(self.event(cwd=self.repo))[0], "deny")

    def test_unregistered_foreign_and_nested_checkout_cannot_borrow_authority(self):
        self.assertIsNone(self.check(self.event()))
        foreign = self.dev / "acme/foreign"
        nested = self.repo / "nested"
        for path in (foreign, nested):
            subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
            self.assertEqual(self.check(self.event(cwd=path))[0], "deny")
        unmanaged = self.home / "unmanaged"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-q", "--detach", str(unmanaged)],
                       check=True, capture_output=True)
        self.save_record(top=str(unmanaged))
        self.assertEqual(self.check(self.event(cwd=unmanaged))[0], "deny")
        for change in ({"top": ""}, {"project": "acme/foreign"}, {"top": str(foreign)},
                       {"top": str(self.repo / ".." / "api")}, {"agent": "claude"}, {"session": "other"}):
            self.save_record(**change)
            self.assertEqual(self.check(self.event())[0], "deny", change)
        self.save_record()
        event = self.event(cwd=foreign)
        event["tool_input"].update(session_id="chat", agent_id="child", cwd=str(self.repo))
        self.assertEqual(self.check(event)[0], "deny")

    def test_child_identity_must_belong_to_the_registered_session_and_project(self):
        self.assertIsNone(self.check(self.event()))
        for identity in ("missing", {"id": "child"}, [], False, 0):
            self.assertEqual(self.check(self.event(agent_id=identity))[0], "deny", identity)
        self.assertEqual(self.check(self.event(session_id="missing"))[0], "deny")
        for change in ({"session": "other"}, {"agent": "claude"}, {"project": "acme/foreign"}):
            self.save_run(**change)
            self.assertEqual(self.check(self.event())[0], "deny", change)

    def test_symlinked_checkout_and_scratch_escapes_remain_denied(self):
        self.assertIsNone(self.check(self.event()))
        alias = self.dev / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.check(self.event(cwd=alias))[0], "deny")
        self.save_record(top=str(alias))
        self.assertEqual(self.check(self.event())[0], "deny")
        self.save_record()
        escape = self.own / "escape"
        escape.symlink_to(self.home, target_is_directory=True)
        self.assertEqual(self.check(self.event(escape / "n.md"))[0], "deny")

    def test_claude_keeps_its_existing_workspace_verdict(self):
        self.assertIsNone(self.check(self.event()))
        for tool in ("Bash", "exec_command", "Write"):
            self.assertEqual(self.check(self.event(tool=tool), "claude")[0], "ask")


if __name__ == "__main__":
    unittest.main()
