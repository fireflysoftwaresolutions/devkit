# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The scratch folders of a registered Codex session (lib/devkit/codexlaunch.py).

A Codex ChatGPT app chat works in Documents/Codex/<date>/<chat>/ (observed on 2026-11-10): the session itself writes
in its work/ and outputs/ folders, a crew member in work/scratchpad/<member>/. Those folders come from the session's
registered record and run, never from a tool call's workdir; codex_scratch_context keeps them for one hook event, and
app_scratch_allows says whether a path lies in them. The fixtures are private session records and runs in a temporary
state folder; no live hook runs.
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
from devkit import codexlaunch, core, hooks, progress  # noqa: E402
from test_progress import TempState, repo_with_worktrees  # noqa: E402


class ScratchCase(unittest.TestCase):
    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.home = Path(state.tmp.name).resolve()
        self.host = self.home / "Documents/Codex/2026-11-10/chat"
        self.own = self.host / "work/scratchpad/seraph"
        self.own.mkdir(parents=True)
        self.session = progress.session_path("codex", "chat")
        self.record = {"agent": "codex", "session": "chat", "cwd": str(self.host)}
        self.save_record()
        for patch in (mock.patch.object(core, "HOME", self.home),
                      # The fixture represents Documents, not the system temp directory that contains the test.
                      mock.patch.object(core, "temp_roots", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def save_record(self, **changes):
        self.session.write_text(json.dumps(dict(self.record, **changes)))

    def register_member(self, **changes):
        progress.RUNS.mkdir(exist_ok=True)
        progress.run_path("codex", "chat").write_text(json.dumps(dict({
            "agent": "codex", "session": "chat", "state": "running", "project": "acme/api",
            "agents": {"child": {"persona": "seraph", "state": "running", "binding": "bound", "scope": ["lib/**"]}},
            "plan": [], "messages": []}, **changes)))

    def registered(self, agent="codex", **event):
        """(root, folders, member) the event registers, or None."""
        data = dict({"session_id": "chat", "cwd": str(self.host)}, **event)
        with codexlaunch.codex_scratch_context(agent, data):
            return codexlaunch.APP_SCRATCH.get()

    def allows(self, path, member=None, **event):
        data = dict({"session_id": "chat", "cwd": str(self.host)}, **event)
        with codexlaunch.codex_scratch_context("codex", data):
            return codexlaunch.app_scratch_allows(path, member)


class MainSessionTests(ScratchCase):

    def test_the_session_writes_in_its_chats_work_and_outputs_only(self):
        self.assertEqual(self.registered(), (str(self.host), (str(self.host / "work"), str(self.host / "outputs")), ""))
        for folder in ("work", "outputs"):
            self.assertTrue(self.allows(self.host / folder / "notes.md"))
        for path in (self.host / "notes.md", self.host.parent / "other/work/notes.md",
                     self.host / "work/../../other/work/notes.md"):
            with self.subTest(path=str(path)):
                self.assertFalse(self.allows(path))

    def test_a_mismatched_record_claude_or_another_session_registers_nothing(self):
        self.assertIsNone(self.registered(agent="claude"))
        self.assertIsNone(self.registered(session_id="missing"))
        for changed in ({"session": "another"}, {"agent": "claude"}, {"cwd": str(self.home / "elsewhere")},
                        {"cwd": str(self.host) + "\0"}):
            with self.subTest(changed=changed):
                self.save_record(**changed)
                self.assertIsNone(self.registered())

    def test_a_link_out_of_the_folder_or_a_linked_chat_folder_is_no_scratch(self):
        outside = self.home / "elsewhere"
        outside.mkdir()
        (self.host / "work/escape").symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.allows(self.host / "work/escape/n.md"))
        self.host.rename(self.host.with_name("moved"))
        self.host.symlink_to(self.host.with_name("moved"), target_is_directory=True)
        self.assertIsNone(self.registered())

    def test_the_folders_hold_only_for_the_event(self):
        with self.assertRaises(RuntimeError):
            with codexlaunch.codex_scratch_context("codex", {"session_id": "chat", "cwd": str(self.host)}):
                self.assertTrue(codexlaunch.app_scratch_allows(self.host / "work/n.md"))
                raise RuntimeError("interrupted hook")
        self.assertIsNone(codexlaunch.APP_SCRATCH.get())
        self.assertFalse(codexlaunch.app_scratch_allows(self.host / "work/n.md"))


class MemberTests(ScratchCase):

    def test_a_member_writes_in_its_own_scratch_folder_only(self):
        self.register_member()
        self.assertEqual(self.registered(agent_id="child"), (str(self.host), (str(self.own),), "seraph"))
        self.assertTrue(self.allows(self.own / "notes.md", "seraph", agent_id="child"))
        self.assertTrue(self.allows(self.own / "notes.md", agent_id="child"))  # no member named: the event's
        for path, member in ((self.own / "notes.md", "neo"), (self.host / "outputs/result.md", "seraph"),
                             (self.host / "work/notes.md", "seraph"), (self.own.parent / "neo/n.md", "seraph"),
                             (self.own.parent / "seraph-other/n.md", "seraph")):
            with self.subTest(path=str(path), member=member):
                self.assertFalse(self.allows(path, member, agent_id="child"))

    def test_a_checkout_inside_the_scratch_folder_is_no_scratch(self):
        self.register_member()
        checkout = self.own / "checkout"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True, capture_output=True)
        self.assertFalse(self.allows(checkout / "code.py", "seraph", agent_id="child"))

    def test_an_identity_the_run_does_not_place_registers_nothing(self):
        self.register_member()
        for aid in ("unknown", {"id": "child"}, ["child"]):
            with self.subTest(aid=aid):
                self.assertIsNone(self.registered(agent_id=aid))


class BoundCheckoutTests(ScratchCase):
    # A member bound to a checkout runs its hook with the checkout as cwd: its scratch folder stays in the chat's
    # folder, and only for the checkout the session record registered (its repository, or a managed worktree of it).

    def setUp(self):
        super().setUp()
        self.dev = self.home / "Development"
        self.repo, self.worktree = repo_with_worktrees(self.dev / "acme", self.dev / ".worktrees/acme/api/fix/x0")
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/acme/api.git"],
                       check=True, capture_output=True)
        self.record.update(top=str(self.repo), project="acme/api")
        self.save_record()
        self.register_member()
        for patch in (mock.patch.object(core, "DEV", self.dev),
                      mock.patch.object(core, "WT_ROOT", self.dev / ".worktrees")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_the_registered_checkout_keeps_the_chats_scratch_folder(self):
        self.assertEqual(self.registered(agent_id="child", cwd=str(self.repo)), (str(self.host), (str(self.own),),
                                                                                 "seraph"))
        self.save_record(top=str(self.worktree))
        subdir = self.worktree / "lib"
        subdir.mkdir()
        for cwd in (self.worktree, subdir):
            with self.subTest(cwd=str(cwd)):
                self.assertIsNotNone(self.registered(agent_id="child", cwd=str(cwd)))
        self.assertIsNone(self.registered(agent_id="child", cwd=str(self.repo)))

    def test_a_foreign_nested_or_linked_checkout_registers_nothing(self):
        foreign, nested = self.dev / "acme/foreign", self.repo / "nested"
        for path in (foreign, nested):
            subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
            with self.subTest(cwd=str(path)):
                self.assertIsNone(self.registered(agent_id="child", cwd=str(path)))
        alias = self.dev / "alias"
        alias.symlink_to(self.repo, target_is_directory=True)
        self.assertIsNone(self.registered(agent_id="child", cwd=str(alias)))
        for change in ({"top": ""}, {"project": "acme/foreign"}, {"top": str(foreign)},
                       {"top": str(self.repo / ".." / "api")}):
            with self.subTest(change=change):
                self.save_record(**change)
                self.assertIsNone(self.registered(agent_id="child", cwd=str(self.repo)))
        self.save_record()
        self.register_member(project="acme/foreign")
        self.assertIsNone(self.registered(agent_id="child", cwd=str(self.repo)))


class SubagentStartTests(ScratchCase):

    def test_subagent_start_creates_and_names_only_its_registered_app_scratch(self):
        self.register_member()
        self.own.rmdir()
        data = {"session_id": "chat", "agent_id": "child", "agent_type": "seraph", "cwd": str(self.host)}
        out = io.StringIO()
        with mock.patch.object(hooks, "STATE", self.home), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.subagent_start("codex")
        self.assertTrue(self.own.is_dir())
        context = json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(self.own), context)
        self.assertFalse((self.host / "scratchpad").exists())
        self.own.rmdir()
        for updates in ({"agent_id": "unknown"}, {"session_id": "another"}, {"cwd": str(self.home)}):
            hooks._make_scratch_folder("codex", dict(data, **updates))
            self.assertFalse(self.own.exists())
        for changed in ({"session": "another"}, {"agent": "claude"}, {"cwd": str(self.host) + "\0"}):
            self.save_record(**changed)
            hooks._make_scratch_folder("codex", data)
            self.assertFalse(self.own.exists())
        self.save_record()
        outside = self.home / "outside"
        outside.mkdir()
        self.own.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(hooks._make_scratch_folder("codex", data))
        self.assertEqual(list(outside.iterdir()), [])
        self.own.unlink()
        self.host.rename(self.host.with_name("moved"))
        self.host.symlink_to(self.host.with_name("moved"), target_is_directory=True)
        self.assertIsNone(hooks._make_scratch_folder("codex", data))
        self.assertFalse(self.own.exists())


class CliScratchTests(unittest.TestCase):
    # The Codex CLI works in a managed checkout; its scratch folders are TMP/codex/<session>/scratchpad/<member or
    # main>/, from the session's registered record and its run, for that session and member only.

    def setUp(self):
        state = TempState().__enter__()
        self.addCleanup(state.__exit__, None, None, None)
        self.home = Path(state.tmp.name).resolve()
        self.dev = self.home / "Development"
        self.host = self.dev / "acme/api"
        subprocess.run(["git", "init", "-q", str(self.host)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.host), "remote", "add", "origin", "https://github.com/acme/api.git"],
                       check=True, capture_output=True)
        self.tmp = self.dev / ".tmp"
        self.own = self.tmp / "codex/chat/scratchpad/seraph"
        self.session = progress.session_path("codex", "chat")
        self.record = {"agent": "codex", "session": "chat", "cwd": str(self.host)}
        self.session.write_text(json.dumps(self.record))
        for module, key, value in ((core, "HOME", self.home), (core, "DEV", self.dev), (core, "TMP_ROOT", self.tmp),
                                   (core, "WT_ROOT", self.dev / ".worktrees")):
            patch = mock.patch.object(module, key, value)
            patch.start()
            self.addCleanup(patch.stop)
        # the temporary home stands for the developer's: only its .tmp is a temporary folder
        patch = mock.patch.object(core, "temp_roots", return_value=[str(self.tmp)])
        patch.start()
        self.addCleanup(patch.stop)
        progress.RUNS.mkdir(exist_ok=True)
        progress.run_path("codex", "chat").write_text(json.dumps({
            "agent": "codex", "session": "chat", "state": "running", "project": "acme/api",
            "agents": {"child": {"persona": "seraph", "state": "running", "binding": "bound", "scope": ["lib/**"]}},
            "plan": [], "messages": []}))

    def event(self, **updates):
        return dict({"session_id": "chat", "agent_id": "child", "cwd": str(self.host)}, **updates)

    def allows(self, path, member="seraph", **updates):
        with codexlaunch.codex_scratch_context("codex", self.event(**updates)):
            return codexlaunch.app_scratch_allows(path, member)

    def test_subagent_start_creates_and_names_the_members_cli_folder(self):
        data = self.event(agent_type="seraph")
        out = io.StringIO()
        with mock.patch.object(hooks, "STATE", self.home), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.subagent_start("codex")
        self.assertTrue(self.own.is_dir())
        self.assertIn(str(self.own), json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(self.allows(self.own / "notes.md"))

    def test_the_folder_holds_for_its_own_session_and_member_only(self):
        self.assertTrue(self.allows(self.own / "n.md"))
        for path in (self.tmp / "codex/other/scratchpad/seraph/n.md", self.own.parent / "neo/n.md",
                     self.own.parent / "main/n.md"):
            with self.subTest(path=path):
                self.assertFalse(self.allows(path))
        self.assertFalse(self.allows(self.own / "n.md", member="neo"))
        for updates in ({"session_id": "missing"}, {"agent_id": "unknown"}, {"cwd": str(self.home)},
                        {"session_id": "../chat"}, {"session_id": "chat/../chat"}, {"session_id": "chat\0"}):
            with self.subTest(updates=updates):
                self.assertFalse(self.allows(self.own / "n.md", **updates))
        for changed in ({"agent": "claude"}, {"session": "other"}, {"cwd": str(self.home)}):
            with self.subTest(changed=changed):
                self.session.write_text(json.dumps(dict(self.record, **changed)))
                self.assertFalse(self.allows(self.own / "n.md"))

    def test_an_unmanaged_checkout_a_malformed_id_or_a_linked_root_gets_no_folder(self):
        for root in (self.home / "unknown", self.dev / "unmanaged"):
            with self.subTest(root=root):
                subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
                self.session.write_text(json.dumps(dict(self.record, cwd=str(root))))
                self.assertFalse(self.allows(self.own / "n.md", cwd=str(root)))
        self.session.write_text(json.dumps(self.record))
        for malformed in ("../chat", "chat/../chat", "chat\0", "chat.name", "a" * 129):
            with self.subTest(session=malformed):
                self.assertIsNone(hooks._make_scratch_folder("codex", self.event(session_id=malformed)))
        self.own.mkdir(parents=True)
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.own / "escape").symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.allows(self.own / "escape/n.md"))
        for root in (self.own, self.own.parent, self.own.parent.parent, self.tmp / "codex", self.tmp):
            with self.subTest(linked=root):
                moved = root.with_name(root.name + "-moved")
                root.rename(moved)
                root.symlink_to(moved, target_is_directory=True)
                self.assertIsNone(hooks._make_scratch_folder("codex", self.event()))
                self.assertFalse(self.allows(self.own / "n.md"))
                root.unlink()
                moved.rename(root)

    def test_claude_gets_no_codex_cli_folder(self):
        self.assertIsNone(hooks._make_scratch_folder("claude", self.event()))
        self.assertFalse(self.own.exists())
        with codexlaunch.codex_scratch_context("claude", self.event()):
            self.assertIsNone(codexlaunch.APP_SCRATCH.get())


if __name__ == "__main__":
    unittest.main()
