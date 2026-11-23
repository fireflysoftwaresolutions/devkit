# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Caller identity without the stamp where the host checks the developer's permission rules (ADR-9): a plain dev call
# goes out as the agent wrote it, so an allow rule such as Bash(dev progress:*) matches it, and its dev command names
# its caller by kernel facts (how=text-pinned).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_textpin -v
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import identity  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_codex_identity_token import CodexTokenCase  # noqa: E402
from test_identity_token import AGENT, TokenCase  # noqa: E402

CODEX = 70101


def claude_argv(cmd: str, stdin: bool = True) -> list:
    # Claude Code 2.1.330's call shell, read from the kernel in a live session on 2026-11-23 (the snapshot path and
    # the session's exports shortened): `< /dev/null` is left out for a command with a heredoc
    quoted = "'" + cmd.replace("'", "'\"'\"'") + "'"
    return ["/bin/zsh", "-c", "source /Users/u/.claude/shell-snapshots/snapshot-zsh-1795411724718-83048m.sh "
            "2>/dev/null || true && export DEVKIT_AGENT=claude\n: && \\builtin setopt NO_EXTENDED_GLOB "
            "NO_BARE_GLOB_QUAL NO_BRACE_CCL NO_MAGIC_EQUAL_SUBST NO_EQUALS 2>/dev/null || true && { \\builtin unalias "
            "-- 'which'; \\builtin unset -f -- 'which'; } >/dev/null 2>&1 || true && eval " + quoted +
            (" < /dev/null" if stdin else "") + " && pwd -P >| /tmp/claude-d2ed-cwd"]


def codex_argv(cmd: str) -> list:
    # Codex 0.167's exec shell (the user's login shell, -lc); a live check confirms it once Codex's quota is back
    return ["/bin/zsh", "-lc", cmd]


def dev_argv(*args) -> list:
    # a dev process as the kernel lists it: the interpreter its #! line names, then bin/dev and its arguments
    return ["python3", "-I", "-S", "/Users/u/.local/bin/dev", *args]


class CallerRecordTests(unittest.TestCase):
    # progress.caller reports a Codex caller devkit cannot verify as `unverified`; a text-pinned one is bound by the
    # same kernel facts as a token, so it is the caller it names (the tech lead, or the member)
    def test_a_text_pinned_codex_caller_is_verified(self):
        for lane, how, want in (("main", "text-pinned", "main"), ("a_tank", "text-pinned", "a_tank"),
                                ("main", "ledger", "unverified")):
            with self.subTest(how=how, lane=lane):
                found = identity._Found(identity.Speaker("codex", "s1", lane, how, True), "codex", "env", ("t1",),
                                        lane, (lane,) if lane != "main" else ())
                with mock.patch.object(identity, "_find", lambda *a, **k: found):
                    self.assertEqual(pg.caller()["caller"], want)


class TextPinCase(TokenCase):
    # The kernel's facts on the call's processes: each shell call's shell right below the agent, started 0.13 s after
    # its call opened (as measured), with the argument list the host gives it (argvs); a dev command runs as that
    # shell's child unless a test sets `chain`.
    def setUp(self):
        super().setUp()
        self.argvs = {}
        for p in (mock.patch.object(identity, "_argv", lambda pid: self.argvs.get(pid), create=True),
                  mock.patch.object(identity, "_children", lambda parents: [
                      p for p in self.argvs if self.parent_of.get(p, self.agent_pid) in parents], create=True)):
            p.start()
            self.addCleanup(p.stop)
        self.assertNotIn(identity.CALL_VAR, os.environ)

    def argv_of(self, cmd):
        return claude_argv(cmd)

    # a shell call through the PreToolUse hook in a mode that checks the developer's rules -> (payload, its shell)
    def call(self, cmd, aid=None, mode="default", **inp):
        data, token, new = self.hook(cmd, aid=aid, permission_mode=mode, **inp)
        self.assertEqual((token, new), ("", ""))  # it goes out exactly as written
        shell = self.shell
        self.argvs[shell] = self.argv_of(cmd)
        self.starts[shell] = pg.call_entry(data)["ts"] + 0.13
        return data, shell

    # a dev command run by a shell call's shell, as its child
    def in_shell(self, shell, *args):
        self.chain, self.own_pid = [(shell, "zsh"), (AGENT, "claude")], 71999
        return self.dev(*args)

    def contested(self):
        return [r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "deny"]


# The hook's side: a plain dev call goes out exactly as the agent wrote it wherever Claude Code checks the developer's
# rules, so Claude Code's own matcher (not a copy of it in devkit) judges it against every allow, ask and deny rule.
# These are the red team's probes of the held automatic allow (D9): each one got devkit to say `allow` for a call
# Claude Code would ask about or deny. Now devkit says nothing about permission and changes nothing the host judges.
class HostJudgesTests(TextPinCase):
    RED_TEAM = ("dev progress note x", "dev progress note", "dev progress\tnote x", "dev progress  note x",
                "dev progress 'note' x", 'dev "progress" note x', "dev progress note 'a\x1b[2Jb'",
                "dev progress note 'a\u2028b'", 'dev progress note "a\xa0b"', "\tdev progress note x",
                "dev progress note x\t", "dev crew inbox")

    def settings(self):
        # the red team's settings: deny and ask rules Claude Code reads wider than a copy did, a wildcard tool name,
        # a malformed allow rule, a NaN, and a deny only in CLAUDE_CONFIG_DIR or a parent folder's .claude
        home = self.root / "config-dir"
        home.mkdir(exist_ok=True)
        (home / "settings.json").write_text(json.dumps({"permissions": {
            "allow": ["Bash(dev:*) ", "Bash(dev progress:*)"], "deny": ["Bash(dev progress note *)", "B*(dev:*)"],
            "ask": ["Bash(dev progress note:*)", "Ba*"]}}))
        (self.root / ".claude").mkdir(exist_ok=True)
        (self.root / ".claude" / "settings.json").write_text('{"permissions":{"allow":["Bash(dev:*)"]},"x":NaN}')
        return mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)})

    def test_every_red_team_probe_goes_out_as_written_with_no_permission_decision(self):
        with self.settings():
            for mode in ("default", "acceptEdits", "plan", "auto", None):
                for cmd in self.RED_TEAM:
                    with self.subTest(mode=mode, cmd=cmd):
                        data, _ = self.call(cmd, mode=mode, run_in_background=False, dangerouslyDisableSandbox=True)
                        self.assertNotIn("permissionDecision", self.last)
                        self.assertNotIn("updatedInput", self.last)
                        self.assertEqual(pg.call_entry(data)["pin_by"], "text")

    def test_a_members_plain_lane_call_goes_out_as_written_too(self):
        self.plan_with_link()
        for cmd in ("dev progress step 'copied' --check '✓ diff clean'", "dev crew ask 'may I?'", "dev watch --wait"):
            with self.subTest(cmd=cmd):
                data, _ = self.call(cmd, aid="a_link")
                self.assertNotIn("updatedInput", self.last)
                self.assertEqual(pg.call_entry(data)["caller"], "a_link")

    # anything but one plain dev call keeps today's form, and devkit still decides no permission: an export, a
    # substitution, a variable in front, a compound command
    def test_no_export_substitution_or_variable_in_front_is_left_to_a_dev_allow_rule(self):
        for cmd in ("export X=1; dev progress note x", "dev progress note $(id)", "dev progress note `id`",
                    "DEVKIT_SPEAKER=tank dev progress note x", "FOO=1 dev progress note x", "dev progress note x; id",
                    "dev progress note x | tail -1", "./dev progress note x", "'dev' progress note x"):
            with self.subTest(cmd=cmd):
                data, token, new = self.hook(cmd, permission_mode="default")
                self.assertNotIn("permissionDecision", self.last)
                self.assertNotEqual(pg.call_entry(data).get("pin_by"), "text")
        self.call("dev progress note x")  # the plain call beside them goes out as written

    def test_bypass_mode_keeps_the_stamped_export_form(self):
        data, token, new = self.hook("dev progress note x", permission_mode="bypassPermissions")
        self.assertTrue(token)
        self.assertIn(f"export DEVKIT_CALL={token}; readonly DEVKIT_CALL; ", new)
        self.assertEqual(pg.call_entry(data)["pin_by"], "shell")
        data, _ = self.call("dev progress note x", mode="acceptEdits")  # where the rules are checked: as written
        self.assertEqual(pg.call_entry(data)["pin_by"], "text")


# The dev command's side: who calls, by the kernel's facts on the call's own shell.
class TextPinResolutionTests(TextPinCase):
    def test_the_tech_leads_plain_call_resolves_main_and_changes_state(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())
        self.assertEqual(identity._read_pin(identity.pin_path("s1", data["tool_use_id"])), shell)

    def test_a_members_plain_call_acts_in_its_own_lane_and_never_as_the_tech_lead(self):
        self.plan_with_link()
        _, shell = self.call("dev progress note 'half way'", aid="a_link")
        rc, out = self.in_shell(shell, "progress", "note", "half way")
        self.assertEqual(rc, 0, out)
        self.assertIn("half way", [u.get("text") for u in self.run_()["agents"]["a_link"].get("updates") or []])
        self.assertIn("noted in your lane · half way", out)  # the member's own line, never the tech lead's block
        self.assertNotIn("[Firefly> DevKit]", out)
        _, shell = self.call("dev progress begin ci-redirect", aid="a_link")
        rc, out = self.in_shell(shell, "progress", "begin", "ci-redirect")
        self.assertEqual(rc, 2, out)
        self.assertIn("only the tech lead", out)

    def test_the_text_pinned_speaker_is_recorded_as_such(self):
        data, shell = self.call("dev crew inbox")
        self.chain = [(shell, "zsh"), (AGENT, "claude")]
        sp = identity.speaker(argv=["crew", "inbox"])
        self.assertEqual((sp.lane, sp.how, sp.verified), ("main", "text-pinned", True))

    def test_a_shell_that_runs_another_command_names_nobody(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        self.argvs[shell] = claude_argv("dev progress add extra && dev progress note y")
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.text-mismatch")), 1)

    def test_a_shell_started_outside_the_calls_window_names_nobody(self):
        self.plan_with_link()
        for offset in (-5.0, getattr(identity, "TEXT_WINDOW_S", 10.0) + 1):
            with self.subTest(offset=offset):
                data, shell = self.call("dev progress add extra")
                self.starts[shell] = pg.call_entry(data)["ts"] + offset
                rc, out = self.in_shell(shell, "progress", "add", "extra")
                self.assertEqual(rc, 2, out)
                self.closed(data)
        self.assertFalse(self.added())

    def test_a_detached_or_reparented_dev_names_nobody_for_a_change(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        self.own_session = 99  # its own process session
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.own_session = None
        self.chain, self.own_pid = [(1, "launchd")], 71999  # launchd adopted it
        identity._forget()
        rc, out = self.dev("progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_pin_that_cannot_be_written_refuses_without_contesting_the_call(self):
        import shutil
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        pin = identity.pin_path("s1", data["tool_use_id"])
        shutil.rmtree(pin.parent)  # its folder went (a cleaned temporary folder)
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("calls.pin-malformed")), 1)
        self.assertEqual(self.contested(), [])

    def test_a_long_command_the_ledger_cut_still_resolves_by_its_hash(self):
        self.plan_with_link()
        note = "x" * (pg.CALL_TEXT + 10)
        data, shell = self.call(f"dev progress note '{note}'")
        self.assertTrue(pg.call_entry(data)["cut"])
        rc, out = self.in_shell(shell, "progress", "note", note)
        self.assertEqual(rc, 0, out)

    def test_a_heredoc_form_without_stdin_redirect_is_read_too(self):
        text_command = getattr(identity, "text_command", lambda agent, argv: None)
        self.assertEqual(text_command("claude", claude_argv("dev crew ask 'it''s'", stdin=False)),
                         "dev crew ask 'it''s'")
        text_command = getattr(identity, "text_command", lambda agent, argv: None)
        self.assertEqual(text_command("claude", claude_argv("dev crew ask \"it's\"")),
                         "dev crew ask \"it's\"")
        for argv in (["/bin/zsh", "-c", "eval 'dev crew inbox'"], ["python3", "-c", "x"], None, ["/bin/zsh"],
                     codex_argv("dev crew inbox")):
            with self.subTest(argv=argv):
                self.assertIsNone(text_command("claude", argv))

    def test_the_kernel_argument_list_of_a_running_process_is_read_whole(self):
        proc = subprocess.Popen(["/bin/sh", "-c", "read x; :", "a b", "it's"], stdin=subprocess.PIPE)
        try:
            self.assertEqual(REAL_ARGV(proc.pid), ["/bin/sh", "-c", "read x; :", "a b", "it's"])
        finally:
            proc.communicate(b"\n", timeout=10)


REAL_ARGV = getattr(identity, "_argv", lambda pid: None)  # the kernel reader itself, before a test patches it


# The Security engineer's forgery cases for identity without a stamp (M32), each refused or contested.
class TextPinForgeryTests(TextPinCase):
    # a member's call replaces its own shell with one whose argument list holds the tech lead's open call's exact
    # command (exec), started inside that call's window
    def forged(self, lead_started: bool):
        self.plan_with_link()
        lead, lead_shell = self.call("dev progress add extra")
        if not lead_started:
            del self.argvs[lead_shell]  # its hooks still run: its shell has not started
        member, member_shell = self.member_exec()
        self.argvs[member_shell] = claude_argv("dev progress add extra")  # what exec made of the member's shell
        return lead, lead_shell, member_shell

    def member_exec(self):
        data, token, new = self.hook("exec /bin/zsh -c x", aid="a_link", permission_mode="default")
        shell = self.shell
        self.starts[shell] = pg.call_entry(data)["ts"] + 0.13
        return data, shell

    def test_an_exec_forgery_while_the_tech_leads_shell_runs_is_contested_for_everyone(self):
        lead, lead_shell, member_shell = self.forged(lead_started=True)
        rc, out = self.in_shell(member_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")  # the tech lead's own call: contested too
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 2)
        self.assertTrue(os.path.lexists(f"{identity.pin_path('s1', lead['tool_use_id'])}.contested"))

    def test_an_exec_forgery_that_wins_the_race_is_contested_loudly_by_the_tech_leads_call(self):
        lead, lead_shell, member_shell = self.forged(lead_started=False)
        self.assertEqual(self.in_shell(member_shell, "progress", "add", "extra")[0], 0)  # pinned first: the race
        self.argvs[lead_shell] = claude_argv("dev progress add extra")  # the tech lead's shell starts
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.contested()), 1)

    def test_a_script_that_execs_a_shell_with_the_tech_leads_text_is_not_the_call(self):
        self.plan_with_link()
        self.call("dev progress add extra")
        _, member_shell = self.member_exec()
        self.argvs[member_shell] = claude_argv("python3 do.py")
        self.chain = [(72999, "zsh -c dev progress add extra"), (member_shell, "zsh"), (AGENT, "claude")]
        self.argvs[72999] = ["zsh", "-c", "dev progress add extra"]
        rc, out = self.dev("progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.text-not-the-call")), 1)

    def test_identical_commands_of_the_tech_lead_and_a_member_are_refused(self):
        self.plan_with_link()
        _, lead_shell = self.call("dev progress add extra")
        _, member_shell = self.call("dev progress add extra", aid="a_link")
        for shell in (lead_shell, member_shell):
            rc, out = self.in_shell(shell, "progress", "add", "extra")
            self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("calls.text-ambiguous")), 2)

    def test_a_planted_runner_step_below_the_tech_leads_call_names_nobody(self):
        self.plan_with_link()
        _, shell = self.call("dev ci --run")
        self.chain = [(73001, "bash -c dev progress add extra"), (73000, "python3 dev ci --run"), (shell, "zsh"),
                      (AGENT, "claude")]
        rc, out = self.dev("progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        _, shell = self.call("dev progress add extra")  # the same command planted deeper than its call's shell
        self.chain = [(73002, "bash"), (shell, "zsh"), (AGENT, "claude")]
        rc, out = self.dev("progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_second_process_after_the_pin_is_contested(self):
        self.plan_with_link()
        data, shell = self.call("dev progress note one")
        self.assertEqual(self.in_shell(shell, "progress", "note", "one")[0], 0)
        other = 72950
        self.argvs[other] = claude_argv("dev progress note one")
        self.starts[other] = pg.call_entry(data)["ts"] + 0.5
        rc, out = self.in_shell(other, "progress", "note", "one")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.contested()), 1)


class TextPinBackgroundTests(TextPinCase):
    # a background call closes as soon as it starts: its wait still names its caller; a state change in it does not
    def test_a_background_wait_names_its_caller_after_its_call_closed(self):
        self.plan_with_link()
        data, shell = self.call("dev watch --wait", run_in_background=True)
        self.assertTrue(pg.call_entry(data)["background"])
        self.closed(data)
        self.chain = [(shell, "zsh"), (AGENT, "claude")]
        with mock.patch.object(sys, "argv", ["dev", "watch", "--wait"]):
            self.assertEqual(identity.consumer(False), "main")
        data, shell = self.call("dev watch --wait --for me", aid="a_link", run_in_background=True)
        self.closed(data)
        self.chain = [(shell, "zsh"), (AGENT, "claude")]
        identity._forget()
        with mock.patch.object(sys, "argv", ["dev", "watch", "--wait", "--for", "me"]):
            self.assertEqual(identity.consumer(True), "a_link")

    def test_a_background_state_change_is_refused_with_the_foreground_fix(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra", run_in_background=True)
        self.closed(data)
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertIn("foreground", out)
        self.assertFalse(self.added())


# Codex where it asks: a plain dev call whose exec input Codex would not let devkit rewrite goes out as written, and
# its dev command is pinned the same way. The payloads are Codex 0.167's exec_command; the argument lists are fixtures
# a live Codex check confirms (codex_argv).
class CodexTextPinTests(CodexTokenCase):
    never_asks = False

    def setUp(self):
        super().setUp()
        self.argvs = {}
        for p in (mock.patch.object(identity, "_argv", lambda pid: self.argvs.get(pid), create=True),
                  mock.patch.object(identity, "_children", lambda parents: [
                      p for p in self.argvs if self.parent_of.get(p, self.agent_pid) in parents], create=True)):
            p.start()
            self.addCleanup(p.stop)

    def call(self, cmd, aid=None):
        data, token, new = self.exec(cmd, aid=aid, workdir=str(self.root), yield_time_ms=10_000)
        self.assertEqual((token, new), ("", ""))
        self.assertNotIn("permissionDecision", self.last)
        self.assertEqual(pg.call_entry(data)["pin_by"], "text")
        shell = self.shell
        self.argvs[shell] = codex_argv(cmd)
        self.starts[shell] = pg.call_entry(data)["ts"] + 0.2
        return data, shell

    def test_the_tech_leads_call_resolves_main_when_its_shell_runs_dev_as_a_child(self):
        _, shell = self.call("dev progress add extra")
        self.chain, self.own_pid = [(shell, "zsh"), (CODEX, "codex")], 71999
        rc, out = self.lead()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_the_tech_leads_call_resolves_main_when_its_shell_replaced_itself_with_dev(self):
        data, shell = self.call("dev progress add extra")
        del self.argvs[shell]
        self.argvs[shell] = dev_argv("progress", "add", "extra")
        self.chain, self.own_pid = [(CODEX, "codex")], shell
        rc, out = self.lead()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_a_members_dev_exec_d_in_the_same_window_contests_the_call(self):
        data, shell = self.call("dev progress add extra")
        self.argvs[shell] = dev_argv("progress", "add", "extra")
        forger = 74555  # a member's exec that replaced itself with the same dev command
        self.argvs[forger] = dev_argv("progress", "add", "extra")
        self.starts[forger] = pg.call_entry(data)["ts"] + 0.3
        self.chain, self.own_pid = [(CODEX, "codex")], forger
        rc, out = self.lead()
        self.assertEqual(rc, 2, out)
        self.chain, self.own_pid = [(CODEX, "codex")], shell
        identity._forget()
        rc, out = self.lead()
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_identical_tech_lead_and_member_commands_are_refused(self):
        from test_codex_identity_token import SUB
        _, lead_shell = self.call("dev progress add extra")
        self.call("dev progress add extra", aid=SUB)
        self.chain, self.own_pid = [(lead_shell, "zsh"), (CODEX, "codex")], 71999
        rc, out = self.lead()
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_rewrite_codex_applies_keeps_the_token(self):
        data, token, new = self.exec("dev progress add extra")  # its command alone: devkit's rewrite and allow
        self.assertTrue(token)
        self.assertEqual(self.last.get("permissionDecision"), "allow")
        self.assertNotEqual(pg.call_entry(data).get("pin_by"), "text")
        self.call("dev progress add extra")  # with the exec's own limits Codex would not apply it: as written



# Files the text pin writes in folders a member can also write: never through a link planted there.
class TextPinFileTests(TextPinCase):
    def test_a_kept_background_record_never_writes_through_a_planted_link(self):
        victim = self.root / "victim.txt"
        victim.write_text("keep")
        path = identity.background_path("s1", "toolu_9001")
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(victim, path)
        identity.keep_background({"pin_by": "text", "background": True, "session": "s1",
                                  "tool_use_id": "toolu_9001", "ts": 1.0})
        self.assertEqual(victim.read_text(), "keep")

    def test_the_pin_folder_is_never_changed_through_a_link_swapped_in_after_its_check(self):
        import stat
        victim = self.root / "victim"
        victim.mkdir(mode=0o755)
        os.chmod(victim, 0o755)
        folder = identity.pin_path("s1", "x").parent
        real = os.lstat

        def swap(path, *a, **k):  # an attacker swaps the checked folder for a link right after the check
            st = real(path, *a, **k)
            if str(path) == str(folder) and stat.S_ISDIR(st.st_mode):
                os.rmdir(folder)
                os.symlink(victim, folder)
            return st
        with mock.patch.object(os, "lstat", swap):
            try:
                identity.make_pin_folder("s1")
            except OSError:
                pass
        self.assertEqual(stat.S_IMODE(os.stat(victim).st_mode), 0o755)


if __name__ == "__main__":
    unittest.main()
