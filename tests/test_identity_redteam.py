# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The Red team tester's probes of the call token (2026-11-19), one test each: what a crew member tried to pass as the
# tech lead or as another member, and must not. On Codex: forging CODEX_THREAD_ID after dropping its token, under
# either approval policy. On Claude Code: taking the tech lead's token from the process list and using it first from
# its own shell, and running the tech lead's token below a nested agent devkit did not recognize. The fixtures are
# tests.test_identity_token's (Claude Code) and tests.test_codex_identity_token's (Codex 0.167 exec payloads).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_redteam -v
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, hooks, identity  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_codex_identity_token import SID, SUB, SUB2, CodexTokenCase  # noqa: E402
from test_identity_token import AGENT, INNER, NESTED, TOKEN, TokenCase  # noqa: E402
from test_pre_tool_hook import TANK, PreToolCase  # noqa: E402
from test_progress import launch  # noqa: E402

NPX_CODEX = "node /Users/u/.npm/_npx/9f/node_modules/.bin/codex exec review"


class CodexThreadForgeryTests(CodexTokenCase):
    def per_agent(self):
        # a member's honest token call with its own thread id: devkit learns that members carry their own ids
        _, token, _ = self.exec("dev crew inbox", aid=SUB)
        self.assertEqual(self.dev("crew", "inbox", token=token, env=self.thread(SUB))[0], 0)
        self.assertEqual(self.sess().get("codex_threads"), "per-agent")

    def test_asking_policy_a_member_forging_the_root_thread_is_refused_and_audited(self):
        self.per_agent()
        self.never_asks = False
        self.exec("python3 x.py; true", aid=SUB)  # compound: Codex may ask, so no rewrite and no token
        rc, out = self.lead(env=self.thread(SID))  # x.py runs dev with CODEX_THREAD_ID=<root>
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.no-token")), 1)

    def test_never_asks_a_member_dropping_its_token_and_forging_the_root_is_refused(self):
        self.per_agent()
        _, token, _ = self.exec("python3 x.py", aid=SUB)  # stamped; x.py drops DEVKIT_CALL
        self.assertTrue(token)
        rc, out = self.lead(env=self.thread(SID))
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_member_forging_another_lanes_thread_cannot_close_its_step(self):
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.assign("ui", "Trinity", why="the test's assignment")
        launch(SUB2, "trinity", "Trinity: ui", sid=SID, agent="codex")
        for never_asks in (True, False):
            with self.subTest(never_asks=never_asks):
                self.never_asks = never_asks
                self.exec("python3 x.py", aid=SUB)  # x.py drops the token and the lane variable
                rc, out = self.dev("progress", "step", "ui finished", "--step", "ui", "--check", "✓ all green",
                                   env=self.thread(SUB2))
                self.assertEqual(rc, 2, out)
                self.assertEqual(next(s for s in self.run_()["plan"] if s["name"] == "ui")["state"], "running")

    def test_the_thread_rule_takes_a_lane_only_with_its_own_open_call_and_audits_it(self):
        self.never_asks = False
        self.call("dev progress note 'api half done'", aid=SUB, sid=SID, agent="codex")  # its own plain call
        rc, out = self.dev("progress", "note", "api half done", env=self.thread(SUB))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.records("caller.thread")), 1)
        rc, out = self.dev("progress", "note", "something else", env=self.thread(SUB))  # no call of its holds it
        self.assertEqual(rc, 2, out)

    def test_a_members_rewrite_never_carries_the_roots_session_id(self):
        for never_asks, cmd in ((False, "dev progress note x"), (True, "dev crew inbox"), (True, "git status")):
            with self.subTest(never_asks=never_asks, cmd=cmd):
                self.never_asks = never_asks
                _, _, new = self.exec(cmd, aid=SUB)
                self.assertNotIn(SID, new)
        _, _, new = self.exec("dev crew inbox")  # the main thread's keeps it
        self.assertIn(f"DEVKIT_SESSION=codex:{SID}", new)


class ClaudeTokenTheftTests(TokenCase):
    def test_a_member_using_the_tech_leads_token_first_from_its_own_shell_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("pytest -q tests")  # the tech lead's long call runs no dev command
        self.hook("python3 do.py", aid="a_link")  # the member's script reads the stamp from ps
        self.chain = [(self.shell, self.lines[self.shell]), (AGENT, "claude")]  # the member's own shell
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.token-foreign-use")), 1)
        self.chain = None  # the tech lead's own call, later: still the tech lead's
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_agents_started_through_node_and_other_hosts_are_agents(self):
        for line in (NPX_CODEX, "node /Users/u/.npm/_npx/9f/node_modules/.bin/claude -p review",
                     "gemini -p review", "opencode run review", "/opt/homebrew/bin/gemini", "node /x/bin/opencode"):
            with self.subTest(line):
                self.assertTrue(core.is_agent_process(line))
        self.assertFalse(core.is_agent_process("node /x/node_modules/.bin/vite"))

    def test_the_tech_leads_token_below_an_npx_codex_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("npx -y @openai/codex exec 'review the diff'")
        own = self.parents_of(token)
        self.chain = [(INNER, "/bin/zsh -lc dev"), (NESTED, NPX_CODEX)] + own
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.foreign-agent")), 1)

    def test_a_claude_token_carried_by_a_codex_thread_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("codex exec 'review the diff'")
        rc, out = self.lead(token, env={identity.THREAD_VAR: "01a1f737-3cd7-7000-8000-000000000001"})
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.foreign-agent")), 1)

    def test_a_tokenless_member_cannot_reorder_launches(self):
        self.plan_with_link()
        self.hook("dev crew next --first ci-redirect --why urgent")  # the tech lead's open call
        self.call("python3 -I x.py", aid="a_link")  # a member's call without its token
        who = pg.caller(argv=["crew", "next", "--first", "ci-redirect", "--why", "urgent"])
        self.assertEqual(who.get("caller"), "unknown")
        self.assertIn("could not verify", who.get("why") or "")

    def parents_of(self, token):
        with mock.patch.dict(os.environ, {identity.CALL_VAR: token}):
            return self.parents()


class ClaudeCallPinTests(TokenCase):
    # the retest of 2026-11-19: a command line proves nothing about which call a process belongs to; the pid the
    # call's shell pinned as its first action does
    def stolen(self, lead="pytest -q tests"):
        if not self.run_():
            self.plan_with_link()
        _, token, _ = self.hook(lead)  # the tech lead's call runs no dev command yet
        self.hook("exec python3 do.py", aid="a_link")  # the member's call shell becomes python
        return token, self.shell

    def refused(self, token):
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.token-foreign-use")), 1)

    def test_a_dev_exec_d_by_the_member_cannot_run_the_tech_leads_pending_command(self):
        for lead in ("pytest -q && dev progress add extra",
                     'pytest -q && for s in $(cat todo.txt); do dev progress add "$s"; done'):
            with self.subTest(lead=lead):
                token, member = self.stolen(lead)
                self.own_pid, self.chain = member, [(AGENT, "claude")]  # dev took the member's shell pid
                self.refused(token)
                self.tearDown_records()

    def test_the_tech_leads_own_uses_resolve_main(self):
        self.plan_with_link()
        ref = pg.crew_ask("Report: " + "x" * 400, from_agent="a_link")["id"]
        data, token, _ = self.hook(f'dev crew ack {ref} --sha256 "$(dev crew read {ref} --json | jq -r .digest)" '
                                   f'--generation 1; python3 tools/report.py; dev progress add extra')
        shell = self.shell
        for chain in ([(shell, ""), (AGENT, "claude")],  # a dev call of its own
                      [(73001, "zsh"), (shell, ""), (AGENT, "claude")],  # the $(…) subshell's dev
                      [(73002, "python3 tools/report.py"), (shell, ""), (AGENT, "claude")]):  # a script run by path
            with self.subTest(chain=chain):
                self.chain = chain
                self.assertEqual(self.speak(token).lane, "main")
                identity._forget()
        self.own_pid, self.chain = shell, [(AGENT, "claude")]  # the shell exec'd its last command: dev is its pid
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)

    def test_an_unpinned_entry_is_no_bearer_token(self):
        self.plan_with_link()
        with mock.patch.object(hooks, "_hook_launched_by", lambda pid: False):
            _, token, _ = self.hook("pytest -q tests")  # no agent pid in its entry
        self.hook("python3 do.py", aid="a_link")
        self.chain = [(self.shell, ""), (AGENT, "claude")]
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.own_pid, self.chain = 79002, []  # a detached process, reparented
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertEqual([r["ref"]["result"] for r in self.records("caller.token-unpinned")][-1:], ["deny"])

    def test_an_unpinned_call_changes_nothing_even_from_its_own_shell_and_still_reads(self):
        self.plan_with_link()
        ref = pg.crew_ask("Report: " + "x" * 400, from_agent="a_link")["id"]
        with mock.patch.object(hooks, "_hook_launched_by", lambda pid: False):
            _, token, _ = self.hook(f"dev progress add extra; dev crew read {ref} --json")
        rc, out = self.lead(token)  # its own text, its own shell: no process binds the token
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        rc, out = self.dev("crew", "read", ref, "--json", token=token)  # a read falls to the ledger's rules
        self.assertEqual(rc, 0, out)

    def test_a_tech_lead_call_running_no_dev_whose_token_another_process_used_is_audited(self):
        self.plan_with_link()
        data, token, new = self.hook("pytest -q tests")
        identity._mark_used("s1", data["tool_use_id"])  # a dev command resolved through its token
        self.closed(data)
        found = self.records("caller.token-unexplained-use")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["ref"]["result"], "flagged")

    def test_only_state_changes_the_call_text_does_not_run_are_audited_when_it_ends(self):
        self.plan_with_link()
        ref = pg.crew_ask("Report: " + "x" * 400, from_agent="a_link")["id"]
        data, token, _ = self.hook('for s in one; do dev progress add "$s"; done; ./build.sh')
        self.assertEqual(self.dev("progress", "add", "one", token=token)[0], 0)  # its loop's own command
        self.assertEqual(self.dev("crew", "read", ref, "--json", token=token)[0], 0)  # build.sh reads: no change
        self.assertEqual(self.dev("progress", "note", "from build.sh", token=token)[0], 0)  # build.sh changes
        self.closed(data)
        found = self.records("caller.token-unlisted-use")
        self.assertEqual([(r["evidence"]["count"], r["ref"]["result"]) for r in found], [(1, "flagged")])
        self.assertNotIn("from build.sh", json.dumps(found))  # the arguments' hash only, never their text
        self.assertFalse(identity.uses_path("s1", data["tool_use_id"]).exists())  # gone with its call

    def tearDown_records(self):
        # the next subtest's own call and records: a fresh plan and audit
        from devkit import audit as audit_module
        for f in audit_module.AUDIT.glob("*.jsonl") if audit_module.AUDIT.exists() else []:
            f.unlink()
        self.chain, self.own_pid = None, 71999
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            run = pg.load_run("claude", "s1")
            run["plan"] = [s for s in run["plan"] if s["name"] != "extra"]
            pg._save(run)


class ClaudePermissionModeTests(TokenCase):
    # outside bypassPermissions Claude Code checks its allow rules on the rewritten command: the token goes only on
    # calls that visibly run dev, and devkit never writes or advises an allow rule for export
    def mode(self, cmd, mode):
        _, token, new = self.hook(cmd, permission_mode=mode)
        return token, new, json.dumps(self.last)

    def test_bypass_stamps_every_call(self):
        token, new, _ = self.mode("git status", "bypassPermissions")
        self.assertTrue(token)

    # where Claude Code checks the developer's rules, no call carries a token; a plain dev call goes out as
    # written (its dev command is text-pinned), so the developer's allow rule matches the text the agent wrote
    def test_default_and_accept_edits_stamp_nothing_and_leave_plain_dev_calls_as_written(self):
        for mode in ("default", "acceptEdits", "plan"):
            with self.subTest(mode=mode):
                token, new, out = self.mode("git status", mode)
                self.assertEqual((token, new), ("", ""))
                token, new, out = self.mode("dev crew inbox 2>&1 | tail -5", mode)  # visible, but not plain
                self.assertEqual(token, "")
                token, new, out = self.mode("dev crew inbox", mode)
                self.assertEqual((token, new), ("", ""))
                self.assertNotIn("permissionDecision", out)
                self.assertNotIn("export:", out)
                self.assertNotIn("Bash(export", out)


class CodexCallPinTests(CodexTokenCase):
    def test_a_second_exec_d_use_of_a_plain_calls_token_is_refused(self):
        self.never_asks = False
        _, token, new = self.exec("dev progress add extra")  # one plain call: the variable-list form, dev pins
        self.assertNotIn("set -C", new)
        self.own_pid, self.chain = 74001, [(self.agent_pid, "codex")]  # the shell exec'd dev
        self.assertEqual(self.dev("progress", "note", "first", token=token)[0], 0)
        self.own_pid = 74002  # another process with the same token
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_plain_call_below_the_agents_launch_loop_pins_its_own_process(self):
        self.never_asks, loop = False, 74100
        with mock.patch.object(hooks.os, "getppid", lambda: loop):  # the loop that starts the agent's hooks and calls
            data, token, _ = self.exec("dev progress add extra")
        self.starts[loop] = time.time() - 600  # it runs for the agent's whole life
        self.parent_of.update({74001: loop, loop: self.agent_pid})
        self.own_pid, self.chain = 74001, [(loop, "python3"), (self.agent_pid, "codex")]  # the shell exec'd dev
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())
        self.assertEqual(identity._read_pin(identity.pin_path(SID, data["tool_use_id"])), 74001)


class CodexAskingThreadTests(CodexTokenCase):
    never_asks = False

    def per_agent(self):
        _, token, _ = self.exec("dev crew inbox", aid=SUB)
        self.dev("crew", "inbox", token=token, env=self.thread(SUB))
        self.assertEqual(self.sess().get("codex_threads"), "per-agent")

    def test_a_thread_claim_on_a_compound_or_mentioning_call_is_refused(self):
        self.per_agent()
        for lead in ("dev ci --run && dev progress add extra", "gh pr comment 5 --body 'next: dev progress add extra'",
                     'echo "dev progress add extra"'):
            with self.subTest(lead=lead):
                self.exec(lead)
                self.exec("python3 x.py; true", aid=SUB)
                rc, out = self.lead(env=self.thread(SID))
                self.assertEqual(rc, 2, out)
                self.assertIn("run it as one plain dev command", out)
                self.assertFalse(self.added())

    def test_a_forged_lane_thread_cannot_close_a_step_its_compound_call_holds(self):
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.assign("ui", "Trinity", why="the test's assignment")
        launch(SUB2, "trinity", "Trinity: ui", sid=SID, agent="codex")
        self.exec("npm test && dev progress step 'ui done' --step ui --check '✓ 40 tests'", aid=SUB2)
        self.exec("python3 x.py; true", aid=SUB)
        rc, out = self.dev("progress", "step", "ui done", "--step", "ui", "--check", "✓ 40 tests",
                           env=self.thread(SUB2))
        self.assertEqual(rc, 2, out)
        self.assertEqual(next(s for s in self.run_()["plan"] if s["name"] == "ui")["state"], "running")

    def test_a_state_change_by_the_thread_rule_is_flagged(self):
        self.exec("dev progress note 'api half done'", aid=SUB)  # its plain call, whose token was not used
        rc, out = self.dev("progress", "note", "api half done", env=self.thread(SUB))
        self.assertEqual(rc, 0, out)
        self.assertEqual([r["ref"]["result"] for r in self.records("caller.thread")], ["flagged"])

    def test_the_thread_shared_record_names_the_member_and_its_call(self):
        data, token, _ = self.exec("dev crew inbox", aid=SUB)
        self.dev("crew", "inbox", token=token, env=self.thread(SID))
        found = self.records("codex.thread-shared")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["crew"]["tool_use_id"], data["tool_use_id"])
        self.assertIn(SUB, found[0]["detail"])


class PinRaceTests(TokenCase):
    # the pin is written after the shell sourced its snapshot or login profile: a member polling ps may write it first
    def test_a_pin_naming_a_shell_older_than_the_call_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("pytest -q tests")
        self.starts[self.shell] = time.time() - 30  # the member's own long-running shell won the race
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.records("calls.pin-early")), 1)

    def test_a_contested_call_names_nobody(self):
        self.plan_with_link()
        _, token, _ = self.hook("pytest -q tests")
        tid = identity.parse_token(token)[0]
        pin = identity.pin_path("s1", tid)
        pin.with_name(pin.name + ".contested").write_text("")  # the victim's write found a pin already there
        for chain in (None, [(79003, "zsh"), (AGENT, "claude")]):
            with self.subTest(chain=chain):
                self.chain = chain
                self.assertEqual(self.lead(token)[0], 2)
        self.assertEqual(len(self.records("calls.pin-contested")), 2)

    def test_a_pin_written_in_advance_beside_a_dangling_contested_link_names_nobody(self):
        self.plan_with_link()
        data, token, _ = self.hook("pytest -q tests")
        pin = identity.pin_path("s1", data["tool_use_id"])
        pin.write_text("79500\n")  # the member's fresh shell, as if its pin was there before the victim's write
        os.symlink("/nonexistent/dir/x", f"{pin}.contested")  # and the victim's marker write failed on this link
        self.chain = [(79500, "/bin/zsh -c x"), (AGENT, "claude")]
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.closed(data)
        self.assertEqual(len(self.records("calls.pin-contested")), 2)  # refused, and audited when the call ends

    def test_a_malformed_pin_fails_closed(self):
        self.plan_with_link()
        data, token, _ = self.hook("pytest -q tests")
        identity.pin_path("s1", data["tool_use_id"]).write_text("72001; rm -rf ~\n")
        self.assertEqual(self.lead(token)[0], 2)
        self.assertEqual(len(self.records("calls.pin-malformed")), 1)

    def test_a_pin_written_again_later_is_audited_when_the_call_ends(self):
        self.plan_with_link()
        data, token, _ = self.hook("dev progress add extra")
        self.assertEqual(self.lead(token)[0], 0)  # its first use records the pin's inode
        pin = identity.pin_path("s1", data["tool_use_id"])
        pin.unlink()
        pin.write_text(f"{self.shell}\n")  # deleted and written again: another file
        self.closed(data)
        self.assertEqual(len(self.records("calls.pin-replaced")), 1)

    def test_fish_gets_no_shell_pin_and_its_plain_call_pins_from_dev(self):
        with mock.patch.object(hooks, "_sh_family", lambda agent, data: False):
            _, token, new = self.hook("git status")
            self.assertEqual((token, new), ("", ""))
            data, token, new = self.hook("dev crew inbox")
        self.assertNotIn("set -C", new)
        self.own_pid, self.chain = self.shell, [(AGENT, "claude")]  # fish ran dev as its own process
        self.assertEqual(self.dev("crew", "inbox", token=token)[0], 0)
        self.assertEqual(identity._read_pin(identity.pin_path("s1", data["tool_use_id"])), self.shell)


class ClaudePinnedProcessLifeTests(TokenCase):
    # the pinned pid is checked with its start time: a pid the system gave to a newer process is not the call's
    # shell, and a pinned process that has exited holds no call. opened(): a stamped call that will run
    # `dev progress add extra` -> (its token, its shell's pid); the Codex class below runs the same tests
    def opened(self):
        self.plan_with_link()
        _, token, _ = self.hook("pytest -q tests; dev progress add extra")
        return token, self.shell

    def test_the_pinned_pid_with_another_start_time_is_another_process(self):
        token, shell = self.opened()
        self.assertEqual(self.dev("crew", "inbox", token=token)[0], 0)  # its first use records pid and start
        self.starts[shell] += 5  # same pid, a later start: the call's shell ended and its pid went to another
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.token-foreign-use")), 1)

    def test_a_pid_reused_before_the_tokens_first_use_is_not_the_calls_shell(self):
        token, shell = self.opened()  # interrupted: no PostToolUse, and its token never used
        self.starts[shell] += 120  # its shell exited; a member's new call shell, inside the call's time, got the pid
        self.chain = [(79600, "python3 x.py"), (shell, "/bin/zsh -c member"), (self.agent_pid, self.agent)]
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.token-foreign-use")), 1)  # its pin was born before that process

    def test_a_pinned_process_that_has_exited_holds_no_call(self):
        token, shell = self.opened()
        self.starts[shell] = None  # gone while the call is still open (its agent has not closed it yet)
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.stale-token")), 1)


class CodexPinnedProcessLifeTests(CodexTokenCase, ClaudePinnedProcessLifeTests):
    def opened(self):
        _, token, _ = self.exec("pytest -q tests; dev progress add extra")
        return token, self.shell


class PinFolderTests(TokenCase):
    def test_a_link_planted_where_the_pin_folder_goes_gets_no_token(self):
        target = self.root / "elsewhere"
        target.mkdir()
        folder = identity.pin_path("s1", "x").parent
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.symlink_to(target)  # the pins would land where its planter reads and writes
        _, token, new = self.hook("pytest -q tests")
        self.assertEqual((token, new), ("", ""))
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(len(self.records("calls.token-skipped")), 1)


class ShellFormTests(unittest.TestCase):
    # the stamp's first action, run by the real shells: the shell's own pid in the pin, noclobber off afterwards, an
    # earlier pin left alone and the call marked contested
    def run_stamp(self, shell, setup=""):
        with tempfile.TemporaryDirectory() as d:
            pin = Path(d) / "pins dir" / "t.pin"  # a folder with a space: the path is quoted
            pin.parent.mkdir()
            script = f"{setup}{identity.pin_stamp(pin)}echo $$; echo x > {d}/f; echo y > {d}/f && echo clobber-ok"
            out = subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=10)
            first = (out.stdout.split()[0], identity._read_pin(pin), "clobber-ok" in out.stdout, out.stderr)
            again = subprocess.run([shell, "-c", identity.pin_stamp(pin) + "true"], capture_output=True, text=True,
                                   timeout=10)
            contested = pin.with_name("t.pin.contested")
            return first, identity._read_pin(pin), (contested.exists(), identity._read_pin(contested)), again.stderr

    def test_a_pin_written_in_advance_is_moved_onto_a_dangling_contested_link(self):
        for shell in ("/bin/sh", "/bin/bash", "/bin/zsh"):
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as d:
                pin = Path(d) / "t.pin"
                pin.write_text("79500\n")  # the member's shell wrote first
                os.symlink(f"{d}/missing/x", f"{pin}.contested")  # so that the victim's marker write fails
                out = subprocess.run([shell, "-c", identity.pin_stamp(pin) + "true"], capture_output=True, text=True,
                                     timeout=10)
                self.assertEqual(out.stderr, "")
                self.assertFalse(os.path.lexists(pin))  # the call is contested, never silently the member's
                self.assertFalse(os.path.islink(f"{pin}.contested"))
                self.assertEqual(identity._read_pin(Path(f"{pin}.contested")), 79500)

    def test_sh_bash_and_zsh_pin_their_own_pid_and_leave_noclobber_off(self):
        for shell, setup in (("/bin/sh", ""), ("/bin/bash", ""), ("/bin/zsh", ""), ("/bin/zsh", "setopt clobber; "),
                             ("/bin/zsh", "setopt clobber_empty 2>/dev/null; ")):
            with self.subTest(shell=shell, setup=setup):
                (pid, pinned, clobber_ok, err), after, contested, err2 = self.run_stamp(shell, setup)
                self.assertEqual(int(pid), pinned)
                self.assertTrue(clobber_ok)  # the user's own redirects still overwrite
                self.assertEqual((err, err2), ("", ""))
                self.assertIsNone(after)  # a second write moves the first pin onto the contested name
                self.assertEqual(contested, (True, pinned))


class RealProcessTheftTests(PreToolCase):
    # Claude Code's process shapes, with real processes below an agent stand-in: the tech lead's long call is
    # running, and a member that read its token from ps tries each trick from its own call
    def lead_call(self, s, cmd):
        c = s.begin(cmd)
        token = TOKEN.search(c["ran"]).group(1)
        rid = s.host.send(["/bin/bash", "-c", c["ran"]], s.repo, env=dict(s.exports))
        entry = json.loads((s.mac.state / "calls" / s.sid / f"{c['call']['tool_use_id']}.json").read_text())
        pin, end = Path(entry.get("pin") or s.repo / "no-pin"), time.time() + (20 if entry.get("pin") else 1)
        while not pin.exists() and time.time() < end:  # the call's shell writes it as its first action
            time.sleep(0.05)
        return c, token, rid

    def plan(self, s):
        return [st["name"] for st in s.run_doc()["plan"]]

    def test_each_trick_with_a_stolen_token_is_refused(self):
        self.steal("claude", "devkit:tank")

    def test_each_trick_with_a_stolen_codex_token_is_refused(self):
        self.codex_policy("never")  # every call stamped, its shell pins it
        self.steal("codex", "tank")

    def steal(self, agent, member_type):
        s = self.lead_on(agent)
        (s.repo / "todo.txt").write_text("")
        # the tech lead's call will run `dev progress add <a word from a file>` later: any such command matches its text
        c, token, rid = self.lead_call(s, 'sleep 6; for s in $(cat todo.txt); do dev progress add "$s"; done')
        payload = s.repo / "payload.sh"

        def forged(n):  # the stamp where the hook writes it, first in the shell's script; the member's lane dropped
            return f" export DEVKIT_CALL={token}; readonly DEVKIT_CALL; unset DEVKIT_FROM_AGENT; dev progress add stolen{n}"
        payload.write_text(forged(3))
        tricks = (f"exec {sys.executable} -I -c \"import subprocess; subprocess.run(['/bin/zsh', '-c', '{forged(1)}'])\"",
                  f"exec -a '/bin/zsh -c {forged(2)}' /bin/zsh -c '{forged(2)}'",
                  f"exec /bin/zsh -c \"$(cat {payload})\"",
                  f"/usr/bin/time /bin/zsh -c '{forged(4)}'")
        for n, trick in enumerate(tricks, 1):
            with self.subTest(trick=trick[:40]):
                s.sh(trick, member=TANK, member_type=member_type)
                self.assertNotIn(f"stolen{n}", self.plan(s))
        self.done(s, c, rid)
        self.assertEqual(len(self.audit("caller.token-foreign-use")), len(tricks))

    @staticmethod
    def done(s, c, rid) -> dict:
        r = s.host.wait(rid)
        c.update(rc=r["rc"], out=r["out"], err=r["err"])
        return s.finish(c)

    def test_the_tech_leads_own_uses_resolve_main(self):
        s = self.lead_on("claude")
        script = s.repo / "lead.sh"
        script.write_text("#!/bin/sh\ndev progress add by-script\n")
        script.chmod(0o755)
        c, token, rid = self.lead_call(s, f"dev progress add one; dev progress note two | tail -1; "
                                          f"X=$(dev progress add three); {script}; dev progress add four & wait")
        r = self.done(s, c, rid)
        self.assertEqual(r["rc"], 0, r["err"])
        s.ok("progress", "add", "exec-d")  # one plain call: its shell runs dev as its last command
        for name in ("one", "three", "by-script", "four", "exec-d"):
            with self.subTest(name=name):
                self.assertIn(name, self.plan(s))


if __name__ == "__main__":
    unittest.main()
