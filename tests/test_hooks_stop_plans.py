# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The Stop hook's plan check, end to end through the real hooks: a session answers only for the plans its own tool
# calls wrote (an edit call that names the plan, or a shell call of its call ledger that names the plan and ran while
# it changed), each judged in the project folder it is in, whatever folder the session works in now. A plan another
# session changed, in this project or another, is never on its list: revising it would break the one-project rule and
# disturb the session that works there.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_hooks_stop_plans
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import canary, guard, hooks, skillguard, state  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402

UNVERSIONED = "# Export plan\n\nEdited by hand, without dev plan revise.\n"  # no body_sha: it needs a revision
REMINDER = "you changed plan(s) without recording why"
FINAL = ("**[Firefly> DevKit]** devkit · `hotfix/26.11.10` · plans\n- **Done:** the fix.\n- **Check:** ✓ 3 tests\n"
         "- **Next:** the review.")


class PlanCheckCase(HookCase):
    # HookCase's temporary state and call ledger, its temporary handoff repository as the one the plan check reads,
    # and a session of the tech lead that started ten minutes ago in a folder outside any repository
    def setUp(self):
        super().setUp()
        self.handoff_root = Path(self.handoff.name)
        handoff_real = str(Path(self.handoff.name).resolve())
        for p in (mock.patch.object(hooks, "HANDOFF", self.handoff_root),
                  mock.patch("devkit.work.request_sync", lambda *a, **k: None),
                  # the handoff repository sits in a temporary folder here, and the call ledger counts a redirect into
                  # one as no write; the real one (~/Development/.handoff) is outside every temporary folder
                  mock.patch("devkit.guard.is_temp", lambda p, real=guard.is_temp: False if str(p).startswith(
                      handoff_real) else real(p))):
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()
        state.replace(hooks._session_file("claude", "s1"),
                      {"agent": "claude", "session": "s1", "started": self.now - 600, "top": "",
                       "turn_started": self.now - 60, "turn_class": "dev"})
        self.calls = 0

    def plan(self, project, name):
        f = self.handoff_root / "plans" / project / name
        f.parent.mkdir(parents=True, exist_ok=True)
        return f

    def write(self, f):
        f.write_text(UNVERSIONED)

    def shell(self, cmd, during=lambda: None, failed=False):
        # one shell call of the main session through the real hooks: its ledger entry (PreToolUse), what happens on
        # disk while it runs (`during`), and its result (PostToolUse, or PostToolUseFailure when `failed`)
        self.calls += 1
        call = {"session_id": "s1", "tool_name": "Bash", "tool_use_id": f"toolu_{self.calls}",
                "tool_input": {"command": cmd}, "cwd": str(self.root)}
        self.hook("guard", dict(call, hook_event_name="PreToolUse"))
        during()
        if failed:
            self.hook("post-tool-failure", dict(call, hook_event_name="PostToolUseFailure", error="exit 1"))
        else:
            self.hook("post-tool", dict(call, hook_event_name="PostToolUse", tool_response={"stdout": ""}))

    def edit_tool(self, f):
        self.calls += 1
        self.write(f)
        self.hook("post-tool", {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Write",
                                "tool_use_id": f"toolu_{self.calls}", "cwd": str(self.root),
                                "tool_input": {"file_path": str(f), "content": UNVERSIONED},
                                "tool_response": {"type": "create"}})

    def stop(self, project):
        # the Stop hook of this development turn, whose final message carries the status block; `project`: the one
        # the session's current folder belongs to ("" outside any repository)
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "Stop",
                   "last_assistant_message": FINAL, "stop_hook_active": False}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.scope.decide", lambda *a: True), \
                mock.patch.object(skillguard, "on_stop", lambda *a: None), \
                mock.patch.object(hooks, "_spawn_sync", lambda *a, **k: None), \
                mock.patch.object(hooks, "_session_project", lambda data, sess: project), \
                mock.patch.object(canary, "save_status", lambda *a, **k: None), \
                mock.patch.object(canary, "check", lambda agent, data: (True, project, FINAL)):
            hooks.run("stop", "claude")
        return (json.loads(out.getvalue()) if out.getvalue().strip() else {}).get("reason") or ""


class OtherSessionsPlansTests(PlanCheckCase):
    def test_a_plan_another_session_changed_in_another_project_is_not_this_sessions(self):
        # the reported case: this session works outside any repository, and the kit-consulting session changes its
        # own plan after this session started; this turn's shell call writes nothing in the plans
        self.shell("ls ~/Development", during=lambda: self.write(
            self.plan("fireflysoftwaresolutions/kit-consulting", "2026-11-09-offer.md")))
        self.assertNotIn(REMINDER, self.stop(""))

    def test_a_plan_changed_during_a_shell_call_that_does_not_name_it_is_not_this_sessions(self):
        # a pull of the handoff repository brings another machine's change to a plan of this very project
        theirs = self.plan("fireflysoftwaresolutions/devkit", "2026-11-10-release-train.md")
        self.shell(f"git -C {self.handoff_root} pull --rebase", during=lambda: self.write(theirs))
        self.assertNotIn(REMINDER, self.stop("fireflysoftwaresolutions/devkit"))


class OwnPlansTests(PlanCheckCase):
    # the session moved its working folder to another checkout (signature-organization/devkit) and wrote a plan of
    # the project it works for: the plan's own folder decides, not the folder the session is in now
    MOVED = "signature-organization/devkit"

    def test_a_plan_the_session_wrote_with_its_edit_tool_is_reminded(self):
        mine = self.plan("fireflysoftwaresolutions/devkit", "2026-11-10-hotfix.md")
        self.edit_tool(mine)
        reason = self.stop(self.MOVED)
        self.assertIn(REMINDER, reason)
        self.assertIn("plans/fireflysoftwaresolutions/devkit/2026-11-10-hotfix.md", reason)

    def test_a_plan_the_sessions_shell_call_wrote_is_reminded(self):
        mine = self.plan("fireflysoftwaresolutions/devkit", "2026-11-10-hotfix.md")
        self.shell(f"cat > '{mine}' <<'EOF'\n{UNVERSIONED}EOF", during=lambda: self.write(mine))
        reason = self.stop(self.MOVED)
        self.assertIn("plans/fireflysoftwaresolutions/devkit/2026-11-10-hotfix.md", reason)

    def test_a_plan_written_by_a_shell_call_that_then_failed_is_reminded(self):
        mine = self.plan("fireflysoftwaresolutions/devkit", "2026-11-10-hotfix.md")
        self.shell(f"sed -i '' 's/CSV/PDF/' {mine} && false", during=lambda: self.write(mine), failed=True)
        self.assertIn("plans/fireflysoftwaresolutions/devkit/2026-11-10-hotfix.md", self.stop(self.MOVED))


if __name__ == "__main__":
    unittest.main()
