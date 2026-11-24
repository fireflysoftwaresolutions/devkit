# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Caller identity without the stamp where the host checks the developer's permission rules: a plain dev call
# goes out as the agent wrote it, so an allow rule such as Bash(dev progress:*) matches it, and its dev command names
# its caller by kernel facts (how=text-pinned).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_textpin -v
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import time
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
from test_codex_identity_token import SID as CODEX_SID, CodexTokenCase  # noqa: E402
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


def retime(data: dict, ts: float):
    # the call opened at `ts` (a time in the past, so the ledger's readers see it as they would a real one)
    pg.call_note(data, ts=round(ts, 3))


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
# Each command below once got an automatic `allow` from devkit for a call Claude Code would ask about or deny (extra
# tabs or spaces, quotes, control and Unicode characters, a missing argument). Now devkit says nothing about
# permission and changes nothing the host judges.
class HostJudgesTests(TextPinCase):
    PROBES = ("dev progress note x", "dev progress note", "dev progress\tnote x", "dev progress  note x",
                "dev progress 'note' x", 'dev "progress" note x', "dev progress note 'a\x1b[2Jb'",
                "dev progress note 'a\u2028b'", 'dev progress note "a\xa0b"', "\tdev progress note x",
                "dev progress note x\t", "dev crew inbox")

    def settings(self):
        # settings that a copy of the matcher misread: deny and ask rules Claude Code reads wider, a wildcard tool name,
        # a malformed allow rule, a NaN, and a deny only in CLAUDE_CONFIG_DIR or a parent folder's .claude
        home = self.root / "config-dir"
        home.mkdir(exist_ok=True)
        (home / "settings.json").write_text(json.dumps({"permissions": {
            "allow": ["Bash(dev:*) ", "Bash(dev progress:*)"], "deny": ["Bash(dev progress note *)", "B*(dev:*)"],
            "ask": ["Bash(dev progress note:*)", "Ba*"]}}))
        (self.root / ".claude").mkdir(exist_ok=True)
        (self.root / ".claude" / "settings.json").write_text('{"permissions":{"allow":["Bash(dev:*)"]},"x":NaN}')
        return mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)})

    def test_every_probe_goes_out_as_written_with_no_permission_decision(self):
        with self.settings():
            for mode in ("default", "acceptEdits", "plan", "auto", None):
                for cmd in self.PROBES:
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

    # before its call opened, or later than the longest devkit waits for the developer to approve a call: not that
    # call's shell
    def test_a_shell_started_outside_its_call_names_nobody(self):
        self.plan_with_link()
        for offset in (-5.0, 1.0):
            with self.subTest(offset=offset):
                data, shell = self.call("dev progress add extra", timeout=30_000)
                entry = pg.call_entry(data)
                self.starts[shell] = entry["ts"] + (offset if offset < 0 else pg.CALL_TTL + offset)
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


# The Security engineer's forgery cases for identity without a stamp, each refused or contested.
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

    # a member's call that opened first (its own prompt held it) and replaced its shell after the tech lead's call
    # opened: no later call gives it away, so it wins the race, and the tech lead's own call then contests it loudly
    def test_an_exec_forgery_that_wins_the_race_is_contested_loudly_by_the_tech_leads_call(self):
        self.plan_with_link()
        member, member_shell = self.member_exec()
        lead, lead_shell = self.call("dev progress add extra")
        del self.argvs[lead_shell]
        self.argvs[member_shell] = claude_argv("dev progress add extra")
        self.starts[member_shell] = pg.call_entry(lead)["ts"] + 0.5
        self.assertEqual(self.in_shell(member_shell, "progress", "add", "extra")[0], 0)  # pinned first: the race
        self.argvs[lead_shell] = claude_argv("dev progress add extra")  # the tech lead's shell starts
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.contested()), 1)

    # a plain call runs exactly one dev command: a forger that won the race cannot repeat the tech lead's change, since
    # its second use is contested and refused
    def test_a_second_use_of_a_plain_call_is_contested(self):
        self.plan_with_link()
        member, member_shell = self.member_exec()
        lead, lead_shell = self.call("dev progress note forged")
        del self.argvs[lead_shell]
        self.argvs[member_shell] = claude_argv("dev progress note forged")
        self.starts[member_shell] = pg.call_entry(lead)["ts"] + 0.5
        self.assertEqual(self.in_shell(member_shell, "progress", "note", "forged")[0], 0)  # the race, won once
        rc, out = self.in_shell(member_shell, "progress", "note", "forged")
        self.assertEqual(rc, 2, out)
        self.assertIn("another process claimed this call's command first", out)
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
            self.assertIn("An earlier one may still run, wait for approval, or have been denied at its prompt", out)
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

    # a kept record past BACKGROUND_KEEP_S goes, and its call's pin and use marks with it
    def test_an_expired_kept_background_call_takes_its_marks_with_it(self):
        self.plan_with_link()
        data, shell = self.call("dev watch --wait", run_in_background=True)
        self.closed(data)
        self.chain = [(shell, "zsh"), (AGENT, "claude")]
        with mock.patch.object(sys, "argv", ["dev", "watch", "--wait"]):
            self.assertEqual(identity.consumer(False), "main")
        tid = data["tool_use_id"]
        marks = (identity.pin_path("s1", tid), identity.uses_path("s1", tid), identity.used_path("s1", tid))
        self.assertTrue(all(m.exists() for m in marks))
        identity._kept_background("s1", time.time() + identity.BACKGROUND_KEEP_S + 1)
        self.assertEqual([m for m in marks if m.exists()], [])
        self.assertFalse(identity.background_path("s1", tid).exists())

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

    # no shell is left to show the call's command, so where a member runs, the thread Codex gives the process must
    # name its caller: the root thread for the tech lead once members showed threads of their own
    def test_the_tech_leads_call_resolves_main_when_its_shell_replaced_itself_with_dev(self):
        from test_codex_identity_token import SID
        identity._record_threads("codex", SID, "per-agent")
        data, shell = self.call("dev progress add extra")
        del self.argvs[shell]
        self.argvs[shell] = dev_argv("progress", "add", "extra")
        self.chain, self.own_pid = [(CODEX, "codex")], shell
        rc, out = self.lead(env={identity.THREAD_VAR: SID})
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

    # Codex has no PostToolUseFailure: a failed command reaches PostToolUse, and devkit closes it as failed, so a state
    # change that ran under its name is audited (a member won the race on Codex, then the tech lead's call failed)
    def test_a_failed_codex_command_after_a_state_change_under_its_name_is_audited(self):
        import contextlib
        import io
        from devkit import hooks
        data, shell = self.call("dev progress add extra")
        self.chain, self.own_pid = [(shell, "zsh"), (CODEX, "codex")], 71999
        self.assertEqual(self.lead()[0], 0)
        with contextlib.redirect_stdout(io.StringIO()):
            hooks._tool_failed("codex", dict(data, hook_event_name="PostToolUse"), "PostToolUse")
        self.assertEqual(len(self.records("calls.text-use-without-result")), 1)
        closed = [c for c in pg.shells_since(CODEX_SID, 0) if c.get("tool_use_id") == data["tool_use_id"]]
        self.assertEqual([c["ended"] for c in closed], ["failed"])

    def test_a_rewrite_codex_applies_keeps_the_token(self):
        data, token, new = self.exec("dev progress add extra")  # its command alone: devkit's rewrite and allow
        self.assertTrue(token)
        self.assertEqual(self.last.get("permissionDecision"), "allow")
        self.assertNotEqual(pg.call_entry(data).get("pin_by"), "text")
        self.call("dev progress add extra")  # with the exec's own limits Codex would not apply it: as written



# A call's shell is the process right below the agent that started after its call opened and before any later call of
# the session opened: the developer may take minutes to approve the call, and a process that another call could own
# names nobody. What broke a fixed 10 s window: a slow approval, a forgery while the prompt is open, and a tech lead's
# shell killed before its dev command ran.
class TextPinOrderTests(TextPinCase):
    # a member's call whose shell replaces itself (exec) with one whose argument list holds another call's command
    def member_call(self, cmd="exec python3 /tmp/f.py"):
        data, token, new = self.hook(cmd, aid="a_link", permission_mode="default")
        return data, self.shell

    def test_a_shell_the_developer_approved_minutes_later_still_names_its_caller(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        self.starts[shell] = pg.call_entry(data)["ts"] + 240
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_a_forgery_while_the_tech_leads_call_waits_on_its_prompt_is_contested(self):
        self.plan_with_link()
        lead, lead_shell = self.call("dev progress add extra")
        ts = pg.call_entry(lead)["ts"]
        del self.argvs[lead_shell]  # the prompt is open: its shell has not started
        _, forger = self.member_call()
        self.starts[forger], self.argvs[forger] = ts + 4.13, claude_argv("dev progress add extra")
        rc, out = self.in_shell(forger, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 1)  # recorded now, whether the developer then approves or denies
        self.assertTrue(os.path.lexists(f"{identity.pin_path('s1', lead['tool_use_id'])}.contested"))
        self.argvs[lead_shell], self.starts[lead_shell] = claude_argv("dev progress add extra"), ts + 11
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")  # approved: contested for the tech lead too
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_killing_the_tech_leads_shell_before_its_dev_command_skips_no_check(self):
        self.plan_with_link()
        lead, lead_shell = self.call("dev progress add extra")
        ts = pg.call_entry(lead)["ts"]
        del self.argvs[lead_shell]
        self.starts[lead_shell] = None  # killed: the second-claimant scan finds no other shell
        _, forger = self.member_call()
        self.starts[forger], self.argvs[forger] = ts + 2.0, claude_argv("dev progress add extra")
        rc, out = self.in_shell(forger, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 1)

    # a later call whose own shell runs its own command (another process right below the agent) cannot own this one;
    # a later call with no such shell (not started yet, or its shell became another command) can
    def test_a_later_call_leaves_the_call_its_own_only_while_its_own_shell_runs(self):
        self.plan_with_link()
        lead, lead_shell = self.call("dev progress add extra")
        member, member_shell = self.call("dev crew inbox", aid="a_link")
        self.starts[lead_shell] = pg.call_entry(member)["ts"] + 0.05
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.closed(lead)
        self.closed(member)
        lead, lead_shell = self.call("dev progress note two")
        member, member_shell = self.call("dev crew read M1", aid="a_link")
        self.starts[lead_shell] = pg.call_entry(member)["ts"] + 0.05
        del self.argvs[member_shell]
        rc, out = self.in_shell(lead_shell, "progress", "note", "two")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.contested()), 1)

    # a shell of an earlier call that typed the same command cannot vouch for the later one
    def test_a_later_call_that_types_an_open_calls_command_contests_the_call(self):
        self.plan_with_link()
        self.call("dev crew inbox", aid="a_link")
        lead, lead_shell = self.call("dev progress add extra")
        member, _ = self.call("dev crew inbox", aid="a_link")
        self.starts[lead_shell] = pg.call_entry(member)["ts"] + 0.05
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # a later call that closed without a clean result (failed, cut off) may have left its shell running: it can own
    # this one unless its own shell still runs its own command
    def test_a_later_call_that_closed_without_a_result_contests_the_call(self):
        self.plan_with_link()
        lead, lead_shell = self.call("dev progress add extra")
        member, member_shell = self.call("dev crew inbox", aid="a_link")
        del self.argvs[member_shell]
        self.closed(member, event="PostToolUseFailure")
        self.starts[lead_shell] = pg.call_entry(lead)["ts"] + 0.5
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 1)

    # a foreground call that got its result has no shell left, so it owns no running process: a member's quick `ls`
    # inside the tech lead's fraction of a second leaves the tech lead's call its own
    def test_a_later_foreground_call_that_closed_with_its_result_leaves_the_call_its_own(self):
        self.plan_with_link()
        now = time.time()
        lead, lead_shell = self.call("dev progress add extra")
        retime(lead, now - 10)
        member, member_shell = self.call("ls", aid="a_link")
        retime(member, now - 9.95)
        del self.argvs[member_shell]
        self.starts[member_shell] = None
        self.closed(member)
        self.starts[lead_shell] = now - 9.9
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    # two later calls of one text, a background one that closed and an open one: one shell running that text cannot
    # be both calls' own, so the call is contested
    def test_one_shell_never_stands_for_two_later_calls_of_the_same_text(self):
        self.plan_with_link()
        now = time.time()
        lead, lead_shell = self.call("dev progress add extra")
        retime(lead, now - 10)
        del self.argvs[lead_shell]
        bg, forger = self.call("npm run watch", aid="a_link", run_in_background=True)
        retime(bg, now - 9)
        self.closed(bg)
        self.starts[forger], self.argvs[forger] = now - 8.87, claude_argv("dev progress add extra")
        twin, twin_shell = self.call("npm run watch", aid="a_link")
        retime(twin, now - 8.5)
        self.starts[twin_shell] = now - 8.37
        rc, out = self.in_shell(forger, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # the same beside a later call that is still open and runs in a shell of its own
    def test_a_finished_foreground_call_beside_an_open_one_with_its_own_shell_leaves_the_call_its_own(self):
        self.plan_with_link()
        now = time.time()
        lead, lead_shell = self.call("dev progress add extra")
        retime(lead, now - 10)
        member, member_shell = self.call("ls", aid="a_link")
        retime(member, now - 9.99)
        del self.argvs[member_shell]
        self.closed(member)
        other, other_shell = self.call("dev crew inbox", aid="a_link")
        retime(other, now - 9.98)
        self.starts[other_shell] = now - 9.85
        self.starts[lead_shell] = now - 9.9
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)

    # a process that runs a later call's command but started before that call opened (a member's earlier background
    # shell of the same text) is not that call's shell, so it vouches for nothing
    def test_a_process_older_than_a_later_call_never_vouches_for_it(self):
        self.plan_with_link()
        now = time.time()
        lead, lead_shell = self.call("dev progress add extra")
        retime(lead, now - 10)
        del self.argvs[lead_shell]
        old_shell = 72990
        self.argvs[old_shell], self.starts[old_shell] = claude_argv("npm run watch"), now - 30
        member, forger = self.call("npm run watch", aid="a_link")
        retime(member, now - 9)
        self.starts[forger], self.argvs[forger] = now - 8.87, claude_argv("dev progress add extra")
        rc, out = self.in_shell(forger, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # a background call closes as soon as it starts and its shell runs on: the tech lead's own background call
    # beside its dev call leaves that call its own while its shell runs its own command, and contests it once that
    # shell is gone
    def test_a_later_background_call_leaves_the_call_its_own_only_while_its_shell_runs(self):
        self.plan_with_link()
        now = time.time()
        for n, alive, want in ((1, True, 0), (2, False, 2)):
            with self.subTest(alive=alive):
                lead, lead_shell = self.call(f"dev progress note n{n}")
                retime(lead, now - 10 * n)
                bg, bg_shell = self.call(f"npm run dev{n}", run_in_background=True)
                retime(bg, now - 10 * n + 0.05)
                self.closed(bg)
                self.starts[bg_shell] = now - 10 * n + 0.18
                if not alive:
                    del self.argvs[bg_shell]
                self.starts[lead_shell] = now - 10 * n + 0.13
                rc, out = self.in_shell(lead_shell, "progress", "note", f"n{n}")
                self.assertEqual(rc, want, out)
        self.assertEqual(len(self.contested()), 1)

    # a later call whose shell may run on after its result could own this process unless that shell runs its own
    # command: a foreground call whose result says it went on in the background (Claude Code moved it there, or the
    # developer did), a Monitor (its command runs on after it returns), and a call whose result says it was
    # interrupted (timed out or cut off: a shell that ignores the signal lives on)
    def test_a_later_call_whose_shell_may_run_on_after_its_result_contests_the_call(self):
        from devkit import hooks
        self.plan_with_link()
        now = time.time()
        cases = (("Bash", {"backgroundTaskId": "b1"}), ("Bash", {"backgroundedByUser": True}), ("Monitor", {}),
                 ("Bash", {"stdout": "", "stderr": "timed out", "interrupted": True}))
        for n, (tool, result) in enumerate(cases):
            with self.subTest(tool=tool, result=result):
                lead, lead_shell = self.call(f"dev progress note c{n}")
                retime(lead, now - 10 * (n + 1))
                member, member_shell = self.call(f"npm test -- {n}", aid="a_link", tool=tool)
                retime(member, now - 10 * (n + 1) + 0.05)
                del self.argvs[member_shell]
                hooks._close_call(dict(member, hook_event_name="PostToolUse", tool_response=result))
                self.starts[lead_shell] = now - 10 * (n + 1) + 0.1
                rc, out = self.in_shell(lead_shell, "progress", "note", f"c{n}")
                self.assertEqual(rc, 2, out)
                self.assertEqual(len(self.contested()), n + 1)

    # a decoy: a member's call replaces its shell with the tech lead's call form while that call's prompt
    # is open, and a second member call, opened after that, replaces its own shell with the first member call's form
    # to pass for that call's shell. A process another call opened before it started could be that call's, so it
    # vouches for no earlier call, and the forgery is contested
    def test_a_decoy_shell_of_a_call_opened_later_never_vouches_for_an_earlier_call(self):
        self.plan_with_link()
        now = time.time()
        forger_cmd = "exec /bin/zsh -c \"eval 'dev progress add extra' < /dev/null && pwd -P >| /tmp/x\""
        lead, lead_shell = self.call("dev progress add extra")
        retime(lead, now - 10)
        del self.argvs[lead_shell]  # the prompt is open: its shell has not started
        member, forger = self.member_call(forger_cmd)
        retime(member, now - 8)
        self.starts[forger], self.argvs[forger] = now - 7.87, claude_argv("dev progress add extra")
        decoy_call, decoy = self.member_call("exec /bin/zsh -c 'sleep 600'")
        retime(decoy_call, now - 6)
        self.starts[decoy], self.argvs[decoy] = now - 5.87, claude_argv(forger_cmd)
        rc, out = self.in_shell(forger, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 1)

    # calls the agent opens together, each shell running its own command, leave the first call its own: a call's
    # shell may start after later calls opened when each of them has a shell of its own
    def test_calls_opened_together_each_with_its_own_shell_leave_the_call_its_own(self):
        self.plan_with_link()
        now = time.time()
        lead, lead_shell = self.call("dev progress add extra")
        retime(lead, now - 10)
        for n, cmd in enumerate(("dev crew inbox", "dev status"), 1):
            data, shell = self.call(cmd)
            retime(data, now - 10 + 0.01 * n)
            self.starts[shell] = now - 9.8 + 0.01 * n
        self.starts[lead_shell] = now - 9.8
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    # the developer may approve a prompted call later than its tool timeout, which counts from the command's start:
    # its entry stays open and its shell still names its caller
    def test_an_approval_slower_than_the_calls_timeout_still_names_its_caller(self):
        self.plan_with_link()
        now = time.time()
        data, shell = self.call("dev progress add extra", timeout=30_000)
        retime(data, now - 50)
        self.starts[shell] = now - 5
        self.assertTrue(pg.open_calls("s1"))
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    # the hook lists every call it opens in the session record; an entry it never listed was written by someone else
    def test_an_entry_the_hook_never_listed_names_nobody(self):
        from devkit import state
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        state.update(pg.session_path("claude", "s1"), lambda d: d.update(calls_opened=[]))
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("calls.forged")), 1)


# A wait is checked like a state change: it takes one caller's events, so a second claimant of its command, or a
# second call that types it, makes it nobody's.
class TextPinWaitTests(TextPinCase):
    def consumer(self, shell, for_me=False):
        self.chain, self.own_pid = [(shell, "zsh"), (AGENT, "claude")], 71999
        identity._forget()
        argv = ["dev", "watch", "--wait"] + (["--for", "me"] if for_me else [])
        with mock.patch.object(sys, "argv", argv):
            return identity.consumer(for_me)

    # a member's call opened first and its prompt held it: no later call, so only the second-claimant scan finds it
    def delayed_forger(self, lead):
        data, token, new = self.hook("exec python3 /tmp/f.py", aid="a_link", permission_mode="default")
        return self.shell

    def test_a_forged_wait_beside_the_tech_leads_running_wait_is_refused(self):
        self.plan_with_link()
        forger = self.delayed_forger(None)
        lead, lead_shell = self.call("dev watch --wait", run_in_background=True)
        ts = pg.call_entry(lead)["ts"]
        self.closed(lead)
        self.starts[forger], self.argvs[forger] = ts + 3.0, claude_argv("dev watch --wait")
        with self.assertRaisesRegex(identity.IdentityError, "another process claimed this call's command first"):
            self.consumer(forger)
        self.assertEqual(len(self.contested()), 1)

    def test_a_wait_claimed_after_the_tech_leads_own_wait_ran_is_refused(self):
        self.plan_with_link()
        forger = self.delayed_forger(None)
        lead, lead_shell = self.call("dev watch --wait", run_in_background=True)
        ts = pg.call_entry(lead)["ts"]
        self.closed(lead)
        self.assertEqual(self.consumer(lead_shell), "main")
        del self.argvs[lead_shell]
        self.starts[lead_shell] = None  # its wait ended, and its shell with it
        self.starts[forger], self.argvs[forger] = ts + 3.0, claude_argv("dev watch --wait")
        with self.assertRaises(identity.IdentityError):
            self.consumer(forger)
        self.assertEqual(len(self.contested()), 1)

    # a stolen wait: a member kills the tech lead's background wait shell before its dev command ran,
    # replaces its own call's shell with that wait's form, and covers it with a decoy shell of a call opened later
    def test_a_wait_taken_behind_a_decoy_after_the_tech_leads_wait_shell_was_killed_is_refused(self):
        self.plan_with_link()
        now = time.time()
        forger_cmd = "exec /bin/zsh -c \"eval 'dev watch --wait' < /dev/null && pwd -P >| /tmp/x\""
        lead, lead_shell = self.call("dev watch --wait", run_in_background=True)
        retime(lead, now - 10)
        self.closed(lead)
        del self.argvs[lead_shell]
        self.starts[lead_shell] = None
        member, _, _ = self.hook(forger_cmd, aid="a_link", permission_mode="default")
        forger = self.shell
        retime(member, now - 9)
        self.starts[forger], self.argvs[forger] = now - 8.87, claude_argv("dev watch --wait")
        decoy_call, _, _ = self.hook("exec /bin/zsh -c 'sleep 600'", aid="a_link", permission_mode="default")
        decoy = self.shell
        retime(decoy_call, now - 8)
        self.starts[decoy], self.argvs[decoy] = now - 7.87, claude_argv(forger_cmd)
        with self.assertRaises(identity.IdentityError):
            self.consumer(forger)
        self.assertEqual(len(self.contested()), 1)

    # a member's foreground wait and the tech lead's background wait type the same command, so
    # neither wait may take the other's events
    def test_a_members_foreground_wait_and_the_tech_leads_background_wait_name_nobody(self):
        for first in ("member", "lead"):
            with self.subTest(first=first):
                self.plan_with_link() if first == "member" else None
                shells = {}
                for who in ((first, "lead" if first == "member" else "member")):
                    if who == "member":
                        _, shells[who] = self.call("dev watch --wait", aid="a_link")
                    else:
                        data, shells[who] = self.call("dev watch --wait", run_in_background=True)
                        self.closed(data)
                for who, shell in shells.items():
                    with self.subTest(asks=who), self.assertRaises(identity.IdentityError):
                        self.consumer(shell)
                for f in [*pg.CALLS.glob("s1/*.json"), *pg.CALLS.glob("s1/.*.background")]:
                    f.unlink()  # the open calls and the kept background records: the next order starts clean

    # the speaker mark only says "the tech lead" in a command the hook stamped; a member's script can set it too
    def test_a_members_script_that_marks_itself_the_tech_lead_does_not_take_its_wait(self):
        self.plan_with_link()
        self.hook("./w.sh", aid="a_link", permission_mode="default")
        self.chain, self.own_pid = [(72990, "bash ./w.sh"), (self.shell, "zsh"), (AGENT, "claude")], 71999
        with mock.patch.dict(os.environ, {identity.SPEAKER_VAR: "main"}), \
                mock.patch.object(sys, "argv", ["dev", "watch", "--wait"]), \
                self.assertRaises(identity.IdentityError):
            identity.consumer(False)
        self.assertEqual(len(self.records("caller.speaker-unbacked")), 1)


# Where every call carries its token (bypassPermissions), the tech lead's background wait closes as soon as it starts:
# its token still names its caller for that wait, from the kept record, so the speaker mark is never needed.
class StampedBackgroundWaitTests(TextPinCase):
    def test_the_tech_leads_background_wait_names_main_from_its_token(self):
        self.plan_with_link()
        data, token, new = self.hook("dev watch --wait", run_in_background=True)
        self.assertTrue(token)
        self.closed(data)
        self.chain, self.own_pid = None, 71999
        identity._forget()
        with mock.patch.dict(os.environ, {identity.CALL_VAR: token, identity.SPEAKER_VAR: "main"}), \
                mock.patch.object(sys, "argv", ["dev", "watch", "--wait"]):
            self.assertEqual(identity.consumer(False), "main")
        self.assertEqual(self.records("caller.speaker-unbacked"), [])

    def test_a_kept_background_token_never_changes_state(self):
        self.plan_with_link()
        data, token, new = self.hook("dev progress add extra", run_in_background=True)
        self.closed(data)
        rc, out = self.dev("progress", "add", "extra", token=token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

class TextPinHookFailureTests(TextPinCase):
    # a text call the hook could not set up (no pin folder) still marks the session as one whose calls are named by
    # the kernel, so its plain state change is refused instead of falling to the ledger's text rules (as for the first
    # call of a fresh session)
    def test_a_pin_folder_that_cannot_be_made_still_refuses_the_state_change(self):
        from devkit import state
        self.plan_with_link()
        with mock.patch.object(identity, "make_pin_folder", side_effect=OSError("x")):
            data, token, new = self.hook("dev progress add extra", permission_mode="default")
        self.assertEqual(state.read(pg.session_path("claude", "s1")).get("call_token"), 1)
        shell = self.shell
        self.argvs[shell] = claude_argv("dev progress add extra")
        self.starts[shell] = pg.call_entry(data)["ts"] + 0.13
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # the same for a failure devkit did not foresee (an exception while it records the call)
    def test_an_unexpected_failure_while_recording_a_text_call_still_refuses_the_state_change(self):
        from devkit import state
        self.plan_with_link()
        real = pg.call_note

        def boom(data, **fields):
            if fields.get("pin_by") == "text":
                raise RuntimeError("an unforeseen failure")
            return real(data, **fields)
        with mock.patch.object(pg, "call_note", boom):
            data, token, new = self.hook("dev progress add extra", permission_mode="default")
        self.assertEqual(state.read(pg.session_path("claude", "s1")).get("call_token"), 1)
        shell = self.shell
        self.argvs[shell] = claude_argv("dev progress add extra")
        self.starts[shell] = pg.call_entry(data)["ts"] + 0.13
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # a state change ran under a text call's name, and the call then got no result: the developer denied it, or it
    # failed or was cut off, so that call cannot explain the change
    def test_a_text_call_that_ends_without_a_result_after_a_state_change_is_audited(self):
        self.plan_with_link()
        for event, n in (("PostToolUse", 0), ("PostToolUseFailure", 1)):
            with self.subTest(event=event):
                data, shell = self.call(f"dev progress note {event}")
                self.assertEqual(self.in_shell(shell, "progress", "note", event)[0], 0)
                self.closed(data, event=event)
                self.assertEqual(len(self.records("calls.text-use-without-result")), n)
        data, shell = self.call("dev crew inbox")  # a read under its name: nothing to explain
        self.in_shell(shell, "crew", "inbox")
        self.closed(data, event="PostToolUseFailure")
        found = self.records("calls.text-use-without-result")
        self.assertEqual(len(found), 1)
        self.assertIn("its caller: main", found[0]["detail"])  # whose call it was, kept in the record

    # a call the developer denied gets no PostToolUse at all: it expires, and its marks go with it
    def test_a_text_call_that_expires_after_a_state_change_is_audited(self):
        from devkit import state
        self.plan_with_link()
        data, shell = self.call("dev progress note expired", timeout=5_000)
        self.assertEqual(self.in_shell(shell, "progress", "note", "expired")[0], 0)
        entry = pg.call_entry(data)
        pg.open_calls("s1", now=entry["ts"] + entry["ttl"] + pg.APPROVAL_S + 1)
        self.assertEqual(len(self.records("calls.text-use-without-result")), 1)
        self.assertFalse(identity.pin_path("s1", data["tool_use_id"]).exists())
        self.assertEqual(identity.read_uses("s1", data["tool_use_id"]), [])
        self.assertIn(data["tool_use_id"], state.read(pg.session_path("claude", "s1"))["calls_closed"])

    # a call the developer denied gets no result, and the session may end before it expires: the session's end closes
    # its open calls as ended, so the state change that ran under the denied call's name is audited then
    def test_a_text_call_still_open_when_the_session_ends_is_audited(self):
        from devkit import hooks, state
        self.plan_with_link()
        data, shell = self.call("dev progress note ended")
        self.assertEqual(self.in_shell(shell, "progress", "note", "ended")[0], 0)
        other, _ = self.call("dev crew inbox")  # an open call that changed nothing: nothing to explain
        end = {"session_id": "s1", "hook_event_name": "SessionEnd", "reason": "exit", "cwd": str(self.root)}
        with mock.patch.object(hooks, "_stdin_json", lambda: dict(end)), \
                mock.patch.object(hooks, "_spawn_sync", lambda *a, **k: None):
            hooks.session_end("claude")
        self.assertEqual(len(self.records("calls.text-use-without-result")), 1)
        self.assertEqual(pg.open_calls("s1"), [])
        self.assertFalse(identity.pin_path("s1", data["tool_use_id"]).exists())
        closed = state.read(pg.session_path("claude", "s1"))["calls_closed"]
        self.assertIn(data["tool_use_id"], closed)
        self.assertIn(other["tool_use_id"], closed)


# Each refusal of the text pin, by the fact that refuses it.
class TextPinRefusalTests(TextPinCase):
    def refused(self, code, shell=None, env=None, *, args=("progress", "add", "extra")):
        rc, out = (self.in_shell(shell, *args) if env is None else self.in_env(shell, env, *args))
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len([r for r in self.records(code) if r["ref"]["result"] == "deny"]), 1, code)
        return out

    def in_env(self, shell, env, *args):
        self.chain, self.own_pid = [(shell, "zsh"), (AGENT, "claude")], 71999
        return self.dev(*args, env=env)

    def lead_call(self):
        self.plan_with_link()
        return self.call("dev progress add extra")

    def test_a_pin_that_names_another_process(self):
        data, shell = self.lead_call()
        identity.pin_path("s1", data["tool_use_id"]).write_text("4242\n")
        self.refused("calls.pin-contested", shell)

    def test_a_first_use_that_recorded_another_process(self):
        data, shell = self.lead_call()
        identity._mark_used("s1", data["tool_use_id"], 4243, 1.0, None)
        self.refused("calls.pin-contested", shell)

    def test_a_contested_marker_already_there(self):
        data, shell = self.lead_call()
        Path(f"{identity.pin_path('s1', data['tool_use_id'])}.contested").touch()
        self.refused("calls.pin-contested", shell)

    def test_a_nearest_agent_process_that_is_not_the_calls(self):
        data, shell = self.lead_call()
        self.chain, self.own_pid = [(shell, "zsh"), (80000, "claude"), (AGENT, "claude")], 71999
        rc, out = self.dev("progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.records("caller.foreign-agent")), 1)

    def test_an_entry_of_another_agent(self):
        data, shell = self.lead_call()
        pg.call_note(data, agent="codex")
        self.refused("caller.foreign-session", shell)

    def test_a_lane_variable_that_names_another_caller(self):
        data, shell = self.lead_call()
        self.refused("caller.conflict", shell, {identity.LANE_VAR: "a_link"})

    def test_a_codex_thread_id_the_claude_code_call_did_not_carry(self):
        data, shell = self.lead_call()
        self.refused("caller.foreign-agent", shell, {identity.THREAD_VAR: "0199a1b2-c3d4"})

    def test_a_call_shell_whose_parent_is_not_the_agent(self):
        data, shell = self.lead_call()
        self.parent_of[shell] = 1
        self.refused("caller.text-not-the-call", shell)

    # the forger pins the tech lead's call while its prompt is open and exits before the tech lead's dev runs: the
    # pin names a process that is gone, and the call is contested loudly
    def test_a_forger_that_pinned_first_and_exited_contests_the_tech_leads_call(self):
        self.plan_with_link()
        data, token, new = self.hook("exec python3 /tmp/f.py", aid="a_link", permission_mode="default")
        forger = self.shell
        lead, lead_shell = self.call("dev progress add extra")
        ts = pg.call_entry(lead)["ts"]
        del self.argvs[lead_shell]
        self.starts[forger], self.argvs[forger] = ts + 1.0, claude_argv("dev progress add extra")
        self.assertEqual(self.in_shell(forger, "progress", "add", "extra")[0], 0)  # it won the race
        del self.argvs[forger]
        self.starts[forger] = None
        self.argvs[lead_shell], self.starts[lead_shell] = claude_argv("dev progress add extra"), ts + 2.0
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertIn("another process", out)
        self.assertEqual(len(self.contested()), 1)
        self.assertIn(f"pinned to process {forger}", json.dumps(self.contested()[0]))


class CodexTextPinPatchTests(CodexTextPinTests):
    # an accepted patch writes the files it names and starts no shell: a patch call that opened after the tech
    # lead's call never stands for a later call that could own its shell
    def test_a_later_patch_call_never_contests_the_call(self):
        from test_codex_identity_token import SID
        data, shell = self.call("dev progress add extra")
        patch = {"session_id": SID, "tool_use_id": "call_patch1", "tool_name": "apply_patch", "cwd": str(self.root),
                 "hook_event_name": "PreToolUse",
                 "tool_input": {"input": "*** Begin Patch\n*** Add File: notes.txt\n+x\n*** End Patch\n"}}
        pg.call_opened("codex", patch)
        self.assertTrue(pg.call_entry(patch)["edit_paths"])
        self.starts[shell] = pg.call_entry(patch)["ts"] + 0.1
        self.chain, self.own_pid = [(shell, "zsh"), (CODEX, "codex")], 71999
        rc, out = self.lead()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())


class CodexTextPinRefusalTests(CodexTextPinTests):
    def test_a_codex_thread_that_names_another_caller(self):
        from test_codex_identity_token import SUB
        _, shell = self.call("dev progress add extra")
        self.chain, self.own_pid = [(shell, "zsh"), (CODEX, "codex")], 71999
        rc, out = self.lead(env={identity.THREAD_VAR: SUB})
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.conflict")), 1)

    # a process right below Codex that is not the dev command itself (a script that imports devkit) is not the call
    def test_a_process_below_codex_that_is_not_the_dev_command_names_nobody(self):
        data, shell = self.call("dev progress add extra")
        self.argvs[shell] = ["python3", "/tmp/forger.py"]
        self.chain, self.own_pid = [(CODEX, "codex")], shell
        rc, out = self.lead()
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.text-mismatch")), 1)

    # where a member runs, a dev process that replaced its call's shell shows no shell argument list, so the thread
    # Codex gives it must name the caller: a missing one, or the root's before members showed threads of their own,
    # names nobody
    def test_a_dev_that_replaced_its_shell_without_its_callers_thread_names_nobody(self):
        from test_codex_identity_token import SID
        data, shell = self.call("dev progress add extra")
        self.argvs[shell] = dev_argv("progress", "add", "extra")
        self.chain, self.own_pid = [(CODEX, "codex")], shell
        for env in ({}, {identity.THREAD_VAR: SID}):
            with self.subTest(env=env):
                rc, out = self.lead(env=env)
                self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.text-no-thread")), 2)


    # a Codex command may run on after its result (an exec session that outlives its yield time), so a later Codex
    # call that closed with its result still counts until its own shell shows its own command
    def test_a_later_codex_call_that_closed_with_its_result_still_contests_the_call(self):
        from test_codex_identity_token import SUB
        now = time.time()
        data, shell = self.call("dev progress add extra")
        retime(data, now - 10)
        mdata, _, _ = self.exec("npm test", aid=SUB, workdir=str(self.root), yield_time_ms=10_000)
        retime(mdata, now - 9.9)
        self.closed(mdata)
        self.starts[shell] = now - 9.8
        self.chain, self.own_pid = [(shell, "zsh"), (CODEX, "codex")], 71999
        rc, out = self.lead()
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # a decoy on Codex: a member's exec replaced its shell with the tech lead's dev command while that
    # call waited on its prompt, and a call opened later replaced its shell with the member call's form
    def test_a_codex_decoy_shell_of_a_call_opened_later_never_vouches_for_an_earlier_call(self):
        from test_codex_identity_token import SID, SUB
        identity._record_threads("codex", SID, "per-agent")
        now = time.time()
        data, shell = self.call("dev progress add extra")
        retime(data, now - 10)
        del self.argvs[shell]
        self.starts[shell] = None
        mcmd = "[ -e /tmp/d ] && sleep 600 || exec dev progress add extra"
        mdata, _, _ = self.exec(mcmd, aid=SUB, workdir=str(self.root), yield_time_ms=10_000)
        mshell = self.shell
        retime(mdata, now - 9)
        self.argvs[mshell], self.starts[mshell] = dev_argv("progress", "add", "extra"), now - 8.8
        ddata, _, _ = self.exec("exec /bin/zsh -lc x", aid=SUB, workdir=str(self.root), yield_time_ms=10_000)
        decoy = self.shell
        retime(ddata, now - 8)
        self.argvs[decoy], self.starts[decoy] = codex_argv(mcmd), now - 7.8
        self.chain, self.own_pid = [(CODEX, "codex")], mshell
        rc, out = self.lead(env={identity.THREAD_VAR: SID})
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("calls.pin-contested")), 1)


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

    # links a member plants: a use is recorded in a file of devkit's own, and the contested marker is made
    # where the link was, never at the link's target
    def test_a_use_is_never_written_through_a_planted_link(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        victim = self.root / "victim.txt"
        victim.write_text("keep")
        os.symlink(victim, identity.uses_path("s1", data["tool_use_id"]))
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 0, out)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(len(identity.read_uses("s1", data["tool_use_id"])), 1)

    def test_a_contested_marker_is_never_made_through_a_planted_link(self):
        self.plan_with_link()
        lead, lead_shell = self.call("dev progress add extra")
        member, member_shell = self.call("dev crew inbox", aid="a_link")
        del self.argvs[member_shell]
        victim = self.root / "created-by-dev"
        marker = f"{identity.pin_path('s1', lead['tool_use_id'])}.contested"
        os.symlink(victim, marker)
        self.starts[lead_shell] = pg.call_entry(member)["ts"] + 0.05
        rc, out = self.in_shell(lead_shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(victim.exists())
        self.assertTrue(os.path.lexists(marker))

    # a use that cannot be recorded (something else sits where its record goes) refuses the change: the record is
    # what refuses a second use and audits a call that ends without a result
    def test_a_use_that_cannot_be_recorded_refuses_the_change(self):
        self.plan_with_link()
        data, shell = self.call("dev progress add extra")
        identity.uses_path("s1", data["tool_use_id"]).mkdir()
        rc, out = self.in_shell(shell, "progress", "add", "extra")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_the_pin_folder_is_never_changed_through_a_link_swapped_in_after_its_check(self):
        import stat
        victim = self.root / "victim"
        victim.mkdir(mode=0o755)
        os.chmod(victim, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions (a test's temporary folder: the mode the test checks stays unchanged)
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


# When a record the text pin relies on cannot be written, or a session's agent is killed before it closes its calls:
# each case fails closed, or is recorded in the audit trail.
LEAD, LEAD_ARGS = "dev progress add extra", ("progress", "add", "extra")


class TextPinRecordFailureTests(TextPinCase):
    def member(self, cmd, **inp):
        data, _, _ = self.hook(cmd, aid="a_link", permission_mode="default", **inp)
        return data, self.shell

    # the tech lead's call while a member's call, opened after it, ran first with the tech lead's form (the race the
    # contest refuses): the member's shell claims the command, and so does the tech lead's, still running
    def raced(self):
        self.plan_with_link()
        _, forger = self.member("exec python3 /tmp/f.py")
        lead, lead_shell = self.call(LEAD)
        self.starts[forger], self.argvs[forger] = pg.call_entry(lead)["ts"] + 0.5, claude_argv(LEAD)
        return lead, lead_shell, forger

    # the pin's contested marker cannot be made (its folder refuses it); `ledger_too`: nor any contest file elsewhere
    def no_marker(self, ledger_too=False):
        real = os.open
        pins = str(identity.pin_dir())

        def fail(path, *a, **k):
            if str(path).endswith(".contested") and (ledger_too or str(path).startswith(pins)):
                raise PermissionError(errno.EACCES, "the marker cannot be written", str(path))
            return real(path, *a, **k)
        return mock.patch.object(os, "open", fail)

    # the session's record of closed shell calls is busy: a member holds its lock past the hook's wait
    def busy_record(self):
        from devkit import state
        real = state.update

        def update(path, fn, **kw):
            if Path(path).name == ".shells.json":
                raise state.StateBusy("held by another process")
            return real(path, fn, **kw)
        return mock.patch.object(state, "update", update)

    def start_session(self, sid):
        from devkit import hooks
        payload = {"session_id": sid, "cwd": str(self.root), "source": "startup"}
        with mock.patch.object(pg, "_ps", lambda: {}), mock.patch.object(hooks, "_spawn", lambda *a, **k: None), \
                mock.patch.object(hooks, "context_for", lambda *a, **k: ""), \
                mock.patch("devkit.work.request_sync", lambda *a, **k: None), \
                mock.patch.dict(os.environ, {"CLAUDE_ENV_FILE": ""}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            hooks.session_start("claude")

    # the agent is killed while its member won the race against the tech lead's open call: no SessionEnd, no result
    def killed_after_a_won_race(self):
        lead, lead_shell, forger = self.raced()
        del self.argvs[lead_shell]  # the tech lead's prompt is open: its shell has not started
        self.assertEqual(self.in_shell(forger, *LEAD_ARGS)[0], 0)  # the documented race, won
        return lead

    # the use list cannot be written: the tech lead hears that the use was not recorded and to run it again, and the
    # call is not taken for contested or its pin for malformed
    def test_a_use_list_that_cannot_be_written_tells_the_tech_lead_to_run_it_again(self):
        self.plan_with_link()
        data, shell = self.call(LEAD)
        identity.uses_path("s1", data["tool_use_id"]).mkdir()
        rc, out = self.in_shell(shell, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertIn("could not record", out)
        self.assertIn("run it again", out)
        self.assertEqual(len(self.records("calls.use-unrecorded")), 1)
        self.assertEqual(self.records("calls.pin-malformed"), [])
        self.assertEqual(self.contested(), [])
        del self.argvs[shell]
        self.closed(data)
        data, shell = self.call(LEAD)  # run again, where the list can be written
        rc, out = self.in_shell(shell, *LEAD_ARGS)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    # a token's use goes on (the token binds it to its process), and the use the list could not hold is recorded, so
    # caller.token-unlisted-use's silence about it is not taken for a clean call
    def test_a_token_use_its_call_cannot_list_is_recorded(self):
        self.plan_with_link()
        data, token, _ = self.hook(LEAD)
        identity.uses_path("s1", data["tool_use_id"]).mkdir()
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())
        found = self.records("calls.use-unrecorded")
        self.assertEqual([r["ref"]["result"] for r in found], ["flagged"])

    # the contest marker cannot be written: the contest still holds once the tech lead's shell is gone, so the
    # member's retry is refused
    def test_a_contest_whose_marker_cannot_be_written_still_refuses_a_later_claimant(self):
        lead, lead_shell, forger = self.raced()
        with self.no_marker():
            rc, out = self.in_shell(forger, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        del self.argvs[lead_shell]
        self.starts[lead_shell] = None
        rc, out = self.in_shell(forger, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 2)
        # the call's contest flag in the ledger goes with the call
        flag = identity.contest_path("s1", lead["tool_use_id"])
        self.assertTrue(os.path.lexists(flag))
        self.closed(lead)
        self.assertFalse(os.path.lexists(flag))

    # a later call that was interrupted (its result says so) but whose shell ignored the signal and took the tech
    # lead's call form is still that later call: the tech lead's call is contested, never taken
    def test_a_surviving_shell_of_an_interrupted_later_call_never_passes_as_the_tech_leads(self):
        from devkit import hooks
        self.plan_with_link()
        now = time.time()
        lead, lead_shell = self.call(LEAD)
        retime(lead, now - 200)
        del self.argvs[lead_shell]  # its prompt is still open
        member, forger = self.member("trap '' TERM; sleep 130; exec /bin/zsh -c x")
        retime(member, now - 190)
        hooks._close_call(dict(member, hook_event_name="PostToolUse",
                               tool_response={"stdout": "", "stderr": "timed out", "interrupted": True}))
        self.starts[forger], self.argvs[forger] = now - 189.8, claude_argv(LEAD)
        rc, out = self.in_shell(forger, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    # the same for the tech lead's background wait, whose claims read its kept record once its call closed
    def test_a_contest_of_a_background_wait_whose_marker_cannot_be_written_still_refuses_a_later_claimant(self):
        self.plan_with_link()
        _, forger = self.member("exec python3 /tmp/f.py")
        lead, lead_shell = self.call("dev watch --wait", run_in_background=True)
        self.starts[forger], self.argvs[forger] = pg.call_entry(lead)["ts"] + 3.0, claude_argv("dev watch --wait")
        self.closed(lead)
        self.chain, self.own_pid = [(forger, "zsh"), (AGENT, "claude")], 71999
        for lead_running in (True, False):
            if not lead_running:
                del self.argvs[lead_shell]
                self.starts[lead_shell] = None
            identity._forget()
            with self.subTest(lead_running=lead_running), self.no_marker(), \
                    mock.patch.object(sys, "argv", ["dev", "watch", "--wait"]), \
                    self.assertRaisesRegex(identity.IdentityError, "another process claimed this call's command"):
                identity.consumer(False)
        # the kept call's contest flag in the ledger goes when its kept record expires
        flag = identity.contest_path("s1", lead["tool_use_id"])
        self.assertTrue(os.path.lexists(flag))
        identity._kept_background("s1", time.time() + identity.BACKGROUND_KEEP_S + 5)
        self.assertFalse(os.path.lexists(identity.background_path("s1", lead["tool_use_id"])))
        self.assertFalse(os.path.lexists(flag))

    # a contest flag its call's end could not remove (a folder planted at its name, which unlink refuses) goes at the
    # next sweep once its call has no ledger entry and no kept record; a link planted there goes, never what it points
    # to, and so does a link inside a planted folder; the flags of an open call and of a kept background call stay,
    # and so does what a session folder that is a link points to
    def test_the_sweep_removes_a_contest_flag_its_call_left_behind(self):
        self.plan_with_link()
        gone, shell = self.call(LEAD)
        planted = identity.contest_path("s1", gone["tool_use_id"])
        planted.mkdir()
        (planted / "inside").write_text("x")
        (planted / "sub" / "deeper").mkdir(parents=True)
        (planted / "sub" / "deeper" / "file").write_text("y")
        del self.argvs[shell]
        self.closed(gone)
        self.assertTrue(planted.is_dir())  # the close's unlink leaves a folder
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep").write_text("k")
        (outside / ".toolu_z.contested").write_text("")
        os.symlink(outside, planted / "sub" / "to-outside")
        linked = identity.contest_path("s1", "toolu_gone")
        os.symlink(outside, linked)
        os.symlink(outside, pg.CALLS / "s-link")  # a session folder that is a link: nothing it points to goes
        deep = identity.contest_path("s1", "toolu_deep")  # deeper than the sweep removes: it stays
        Path(deep, *["d"] * (identity._TREE_DEPTH + 1)).mkdir(parents=True)
        still, _ = self.call("dev progress note x")
        open_flag = identity.contest_path("s1", still["tool_use_id"])
        open_flag.write_text("")
        kept, _ = self.call("dev watch --wait", run_in_background=True)
        self.closed(kept)
        self.assertTrue(os.path.lexists(identity.background_path("s1", kept["tool_use_id"])))
        kept_flag = identity.contest_path("s1", kept["tool_use_id"])
        kept_flag.write_text("")
        with mock.patch.object(pg, "_pid_running", lambda pid: True):
            identity.sweep_gone(time.time() + identity.FLAG_GRACE_S + 1)
        self.assertFalse(os.path.lexists(planted))
        self.assertFalse(os.path.lexists(linked))
        self.assertEqual(sorted(p.name for p in outside.iterdir()), [".toolu_z.contested", "keep"])
        self.assertTrue(Path(deep, *["d"] * identity._TREE_DEPTH).is_dir())
        self.assertTrue(os.path.lexists(open_flag))
        self.assertTrue(os.path.lexists(kept_flag))

    # a sweep (another session's start, dev sync) leaves a contest flag for FLAG_GRACE_S after it was made and after
    # its call closed: the close's audit reads the flag right after the call's ledger entry goes, and a sweep in that
    # moment cost the audit (calls.pin-contested), whether the flag was made just now or long before the close
    def test_the_sweep_leaves_a_flag_its_call_closed_or_made_within_the_grace_period(self):
        self.plan_with_link()
        made = identity.contest_path("s1", "toolu_fresh")
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_text("")
        identity.sweep_gone()
        self.assertTrue(os.path.lexists(made))  # no ledger entry, but made within the grace period
        identity.sweep_gone(time.time() + identity.FLAG_GRACE_S + 1)
        self.assertFalse(os.path.lexists(made))
        # a record of closed calls that is not one may hide a close: the flag stays, and the sweep goes on
        made.write_text("")
        (made.parent / ".shells.json").write_text("[1]")
        identity.sweep_gone(time.time() + identity.FLAG_GRACE_S + 1)
        self.assertTrue(os.path.lexists(made))
        (made.parent / ".shells.json").unlink()
        for flag_age in (0, 2 * identity.FLAG_GRACE_S):
            with self.subTest(flag_age=flag_age):
                data, token, _ = self.hook(LEAD)
                flag = identity.contest_path("s1", data["tool_use_id"])
                flag.touch()
                made_at = time.time() - flag_age
                os.utime(flag, (made_at, made_at))
                rc, out = self.lead(token)
                self.assertEqual(rc, 2, out)
                before = len([r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"])
                real = pg.call_closed

                def closed_then_sweep(*a, **k):
                    real(*a, **k)
                    with mock.patch.object(pg, "_pid_running", lambda pid: True):
                        identity.sweep_gone()
                with mock.patch.object(pg, "call_closed", closed_then_sweep):
                    self.closed(data)
                after = [r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"]
                self.assertEqual(len(after) - before, 1)
                self.assertIn("its contest flag is in the ledger", after[-1]["detail"])

    # a call denied at its permission check never ran, so its close keeps no record of closed calls: a sweep between
    # the close and its audit finds neither its ledger entry nor its close, and takes a flag past the grace period;
    # the audit reads the flag before the close, so the call is still audited as contested
    def test_a_denied_call_keeps_its_contest_audit_when_a_sweep_follows_its_close(self):
        self.plan_with_link()
        data, token, _ = self.hook(LEAD)
        self.assertTrue(token)
        flag = identity.contest_path("s1", data["tool_use_id"])
        flag.touch()
        made_at = time.time() - 2 * identity.FLAG_GRACE_S
        os.utime(flag, (made_at, made_at))
        before = len([r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"])
        real, swept = pg.call_closed, []

        def closed_then_sweep(*a, **k):
            real(*a, **k)
            with mock.patch.object(pg, "_pid_running", lambda pid: True):
                identity.sweep_gone()
            swept.append(not os.path.lexists(flag))
        with mock.patch.object(pg, "call_closed", closed_then_sweep):
            self.closed(data, event="PermissionDenied")
        self.assertEqual(swept, [True])  # the sweep took the flag before the audit ran
        after = [r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"]
        self.assertEqual(len(after) - before, 1)
        self.assertIn("its contest flag is in the ledger", after[-1]["detail"])

    # the session folder swapped for a link once the sweep has checked it: the sweep works in the folder it opened,
    # never in the one the link points to
    def test_the_sweep_never_follows_a_session_folder_swapped_for_a_link(self):
        folder = pg.CALLS / "s-swap"
        folder.mkdir(parents=True)
        (folder / ".toolu_y.contested").write_text("")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / ".toolu_z.contested").write_text("")
        moved = self.root / "moved"
        real = os.scandir

        def swap(p):
            if not os.path.islink(folder):
                os.rename(folder, moved)
                os.symlink(outside, folder)
            return real(p)
        with mock.patch.object(os, "scandir", swap):  # the listing, once the folder is open
            identity._sweep_flags(folder, time.time() + identity.FLAG_GRACE_S + 1)
        self.assertTrue(os.path.islink(folder))  # the swap happened
        self.assertEqual(os.listdir(outside), [".toolu_z.contested"])
        self.assertEqual(os.listdir(moved), [])  # the flag of the folder it opened went

    # a session's record of closed shell calls that is there but cannot be read (no read permission, broken JSON, a
    # folder, a link) may hide the close of the flag's call a moment ago: the sweep keeps the flag, and with no record
    # at all the flag goes
    def test_the_sweep_keeps_a_flag_when_the_record_of_closed_calls_cannot_be_read(self):
        folder = pg.CALLS / "s1"
        folder.mkdir(parents=True)
        record = folder / ".shells.json"
        now = time.time()
        closed = json.dumps({"calls": [{"tool_use_id": "toolu_rec", "opened": now - 300, "closed": now - 1}]})
        target = self.root / "record.json"
        target.write_text(closed)
        for kind in ("unreadable", "broken", "folder", "dangling-link", "link", "none"):
            with self.subTest(kind=kind):
                if record.is_dir() and not record.is_symlink():
                    record.rmdir()
                elif os.path.lexists(record):
                    if not record.is_symlink():
                        os.chmod(record, 0o600)
                    record.unlink()
                if kind in ("unreadable", "broken"):
                    record.write_text(closed if kind == "unreadable" else closed[:-3])
                    if kind == "unreadable":
                        os.chmod(record, 0)
                elif kind == "folder":
                    record.mkdir()
                elif kind != "none":
                    os.symlink(target if kind == "link" else self.root / "nowhere", record)
                flag = folder / ".toolu_rec.contested"
                flag.write_text("")
                made = now - 2 * identity.FLAG_GRACE_S
                os.utime(flag, (made, made))
                identity._sweep_flags(folder, now)
                self.assertEqual(os.path.lexists(flag), kind != "none")

    # a flag dated in the future (a planted mtime) is not fresh: it goes at the next sweep, not a grace period after
    # that date; one made just after the sweep read the clock still counts as fresh; and a record of closed calls
    # that dates its close in the future keeps no flag either
    def test_a_flag_or_a_close_dated_in_the_future_is_not_fresh(self):
        folder = pg.CALLS / "s1"
        folder.mkdir(parents=True)
        now = time.time()
        for tid, ahead in (("toolu_far", 365 * 86400), ("toolu_near", 1)):
            flag = folder / f".{tid}.contested"
            flag.write_text("")
            os.utime(flag, (now + ahead, now + ahead))
        (folder / ".toolu_rec.contested").write_text("")
        made = now - 2 * identity.FLAG_GRACE_S
        os.utime(folder / ".toolu_rec.contested", (made, made))
        (folder / ".shells.json").write_text(json.dumps({"calls": [
            {"tool_use_id": "toolu_rec", "opened": now - 300, "closed": now + 365 * 86400}]}))
        identity._sweep_flags(folder, now)
        self.assertEqual(sorted(os.listdir(folder)), [".shells.json", ".toolu_near.contested"])

    # a contest that lands after the close read the call's contest mark, and before its entry left the ledger (the
    # close can wait on the record's lock), is still audited: the close reads the mark again once the entry is gone
    def test_a_contest_made_while_the_call_closes_is_audited(self):
        self.plan_with_link()
        data, _, _ = self.hook(LEAD)
        entry = pg.call_entry(data)
        before = len([r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"])
        real, contested = pg.call_closed, []

        def contest_then_close(*a, **k):
            contested.append(identity._contest(entry))
            return real(*a, **k)
        with mock.patch.object(pg, "call_closed", contest_then_close):
            self.closed(data)
        self.assertEqual(contested, [True])
        after = [r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"]
        self.assertEqual(len(after) - before, 1)

    # a contested call that ends with no result (its session ended, its time ran out, or its agent went without its
    # SessionEnd) is audited as contested at that end, as one that got its result is (PostToolUse)
    def test_a_contested_call_ended_without_its_result_keeps_its_contest_audit(self):
        self.plan_with_link()
        for how in ("session-ended", "expired", "session-gone"):
            with self.subTest(how=how):
                data, _, _ = self.hook(LEAD)
                flag = identity.contest_path("s1", data["tool_use_id"])
                flag.touch()
                made = time.time() - 2 * identity.FLAG_GRACE_S
                os.utime(flag, (made, made))
                before = len([r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"])
                if how == "session-ended":
                    identity.end_session(self.agent, "s1")
                elif how == "expired":
                    pg.open_calls("s1", time.time() + 100000)
                else:
                    with mock.patch.object(pg, "_pid_running", lambda pid: False):
                        identity.sweep_gone()
                self.assertEqual(pg.call_entry(data), {})
                after = [r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"]
                self.assertEqual(len(after) - before, 1)
                self.assertIn("its contest flag is in the ledger", after[-1]["detail"])
                self.assertIn(how, after[-1]["detail"])
                self.assertFalse(os.path.lexists(flag))

    # a session folder in the ledger that is a link: the sweep and the ledger's readers never list or remove what it
    # points to, whether it was a link when they looked or became one after they opened the folder
    def test_a_linked_session_folder_never_loses_the_files_it_points_to(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "config.json").write_text(json.dumps({"ts": 1, "name": "a user's file"}))
        (outside / "other.json").write_text(json.dumps({"closed": 1}))
        pg.CALLS.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, pg.CALLS / "s-link")
        real_open, swept = pg.open_calls, []

        def seen(sid, *a, **k):
            swept.append(sid)
            return real_open(sid, *a, **k)
        with mock.patch.object(pg, "_pid_running", lambda pid: True), mock.patch.object(pg, "open_calls", seen):
            identity.sweep_gone()
        self.assertNotIn("s-link", swept)  # the sweep takes no link for a session folder
        self.assertEqual(pg.open_calls("s-link", time.time() + 100000), [])
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["config.json", "other.json"])
        # swapped for a link once the reader opened it: the expired entry of the folder it opened goes, nothing outside
        folder = pg.CALLS / "s-swap"
        folder.mkdir()
        (folder / "toolu_old.json").write_text(json.dumps({"ts": 1, "cmd": "x"}))
        moved, real = self.root / "moved", os.listdir

        def swap(p):
            if not os.path.islink(folder):
                os.rename(folder, moved)
                os.symlink(outside, folder)
            return real(p)
        with mock.patch.object(os, "listdir", swap):
            self.assertEqual(pg.open_calls("s-swap", time.time()), [])
        self.assertTrue(os.path.islink(folder))  # the swap happened
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["config.json", "other.json"])
        self.assertEqual(os.listdir(moved), [])

    # a folder planted at a flag's name with more entries than one sweep removes: each sweep looks at or removes at
    # most _SWEEP_MAX entries (the flag, then what the folder holds) and leaves the rest for the next one, so a session
    # start never waits on a huge folder
    def test_a_planted_folder_is_removed_a_bounded_number_of_entries_per_sweep(self):
        folder = pg.CALLS / "s1"
        planted = folder / ".toolu_wide.contested"
        planted.mkdir(parents=True)
        for i in range(12):
            (planted / f"f{i:02d}").write_text("")
        with mock.patch.object(identity, "_SWEEP_MAX", 5):
            later = time.time() + identity.FLAG_GRACE_S + 1
            identity._sweep_flags(folder, later)
            self.assertEqual(len(os.listdir(planted)), 8)
            for n in range(1, 4):
                identity._sweep_flags(folder, later + n * (identity.FLAG_GRACE_S + 1))
        self.assertFalse(os.path.lexists(planted))

    # a stamped call found contested says how it was marked, in its refusal and in its close's audit: a pin another
    # process wrote first (its pin's marker), or its contest flag in the ledger, which says nothing of who wrote a pin
    def test_a_contested_token_call_says_how_it_was_marked(self):
        self.plan_with_link()
        for mark, refused, audited, never in (
                ("pin", "another process wrote its call's pin first", "another process wrote this call's pin before",
                 "marked contested"),
                ("ledger", "this call is marked contested", "this call is marked contested", "pin")):
            with self.subTest(mark=mark):
                data, token, _ = self.hook(LEAD)
                tid = data["tool_use_id"]
                Path(f"{identity.pin_path('s1', tid)}.contested" if mark == "pin"
                     else identity.contest_path("s1", tid)).touch()
                denied, flagged = len(self.contested()), len(self.records("calls.pin-contested"))
                rc, out = self.lead(token)
                self.assertEqual(rc, 2, out)
                self.assertFalse(self.added())
                refusal = self.contested()[denied:]
                self.assertEqual(len(refusal), 1)
                self.assertIn(refused, refusal[0]["detail"])
                self.assertNotIn(never, refusal[0]["detail"])
                self.closed(data)
                audit = [r for r in self.records("calls.pin-contested")[flagged:] if r["ref"]["result"] == "flagged"]
                self.assertEqual(len(audit), 1)
                self.assertIn(audited, audit[0]["detail"])
                self.assertNotIn(never, audit[0]["detail"])

    # neither the pin's marker nor the call's contest file in the ledger can be made: the claimant is refused, and its
    # record says the contest could not be recorded
    def test_a_contest_recorded_nowhere_is_refused_and_says_so(self):
        lead, lead_shell, forger = self.raced()
        with self.no_marker(ledger_too=True):
            rc, out = self.in_shell(forger, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertIn("could not be recorded", self.contested()[0]["detail"])

    # the call's PostToolUse removes its ledger entry while a contest whose marker cannot be made is under way, after a
    # read that still saw the call open: the contest never writes the entry back, so the closed call never returns as
    # an open one (a retry refused as ambiguous, then a false calls.unclosed)
    def test_a_contest_never_brings_back_a_call_that_closed_meanwhile(self):
        lead, lead_shell, forger = self.raced()
        tid, entry = lead["tool_use_id"], pg.CALLS / "s1" / f"{lead['tool_use_id']}.json"
        seen = pg.call_entry(lead)
        real_open, real_read = os.open, identity._read_entry

        def close_now(path, *a, **k):  # the marker is refused, and the close lands right then
            if str(path).endswith(".contested") and str(path).startswith(str(identity.pin_dir())):
                entry.unlink(missing_ok=True)
                raise PermissionError(errno.EACCES, "the marker cannot be written", str(path))
            return real_open(path, *a, **k)

        def read(sid, t):  # a read that raced the close still sees the call open
            return dict(seen) if t == tid and not entry.exists() else real_read(sid, t)
        with mock.patch.object(os, "open", close_now), mock.patch.object(identity, "_read_entry", read):
            rc, out = self.in_shell(forger, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertEqual(pg.call_entry(lead), {})
        self.assertNotIn(tid, [e.get("tool_use_id") for e in pg.open_calls("s1")])

    # a member holds the lock of the record of closed shell calls while its background call, opened after the tech
    # lead's, closes: that call's ledger entry stays, so it is still a later call that could own the shell, and the
    # drop is recorded. Once the record would have let it go, the entry goes quietly (it got its result)
    def test_a_later_call_whose_close_the_busy_record_dropped_still_contests_the_call(self):
        self.plan_with_link()
        lead, lead_shell = self.call(LEAD)
        ts = pg.call_entry(lead)["ts"]
        del self.argvs[lead_shell]  # the tech lead's prompt is open
        forged = "exec /bin/zsh -c \"eval 'dev progress add extra' < /dev/null && pwd -P >| /tmp/x\""
        member, forger = self.member(forged, run_in_background=True)
        with self.busy_record():
            self.closed(member)  # a background call closes as it starts
        self.assertEqual(len(self.records("calls.close-unrecorded")), 1)
        self.starts[forger], self.argvs[forger] = ts + 2.0, claude_argv(LEAD)
        rc, out = self.in_shell(forger, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.contested()), 1)
        pg.open_calls("s1", now=time.time() + pg.SHELL_KEEP + 1)
        self.assertEqual(pg.call_entry(member), {})
        self.assertEqual([r for r in self.records("calls.unclosed") if r["target"] == "a_link"], [])

    # a foreground call whose close the busy record kept in the ledger, marked closed, is no open call: the same
    # command run again in a new call is that call's alone, never ambiguous
    def test_a_call_kept_closed_by_a_busy_record_is_not_open_for_its_rerun(self):
        self.plan_with_link()
        first, shell1 = self.call("dev progress note hi")
        rc, out = self.in_shell(shell1, "progress", "note", "hi")
        self.assertEqual(rc, 0, out)
        del self.argvs[shell1]
        with self.busy_record():
            self.closed(first)
        self.assertTrue(pg.call_entry(first).get("closed"))
        self.assertNotIn(first["tool_use_id"], [e.get("tool_use_id") for e in pg.open_calls("s1")])
        again, shell2 = self.call("dev progress note hi")
        rc, out = self.in_shell(shell2, "progress", "note", "hi")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("open calls of this session run this very command", out)

    # the kept entry of a closed background call is not an open one: its wait still names its caller from its kept
    # background record, and no token check takes it for a forged call
    def test_a_background_wait_still_names_its_caller_when_its_close_was_not_recorded(self):
        self.plan_with_link()
        data, token, _ = self.hook("dev watch --wait", run_in_background=True)
        with self.busy_record():
            self.closed(data)
        identity._forget()
        with mock.patch.dict(os.environ, {identity.CALL_VAR: token, identity.SPEAKER_VAR: "main"}), \
                mock.patch.object(sys, "argv", ["dev", "watch", "--wait"]):
            self.assertEqual(identity.consumer(False), "main")
        self.assertEqual(self.records("calls.forged"), [])

    # a call the host denies never runs: where the host says so (Claude Code's PermissionDenied, for a call its
    # auto mode refused), the call closes as denied, so the same command run again right after is the only call
    # that types it; a state change that already ran under the denied call's name is audited
    def test_a_call_the_host_reports_denied_closes_and_its_retry_names_its_caller(self):
        from devkit import hooks
        self.plan_with_link()
        denied, dshell = self.call(LEAD, timeout=120_000)
        retime(denied, time.time() - 200)
        del self.argvs[dshell]
        payload = {"session_id": "s1", "tool_use_id": denied["tool_use_id"], "tool_name": "Bash",
                   "tool_input": denied["tool_input"], "hook_event_name": "PermissionDenied", "cwd": str(self.root),
                   "reason": "auto mode refused it"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            hooks.run("permission-denied", "claude")
        self.assertEqual(pg.call_entry(denied), {})
        again, shell = self.call(LEAD, timeout=120_000)
        rc, out = self.in_shell(shell, *LEAD_ARGS)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    # a later call the host denied never started a shell, so it can own no process: the tech lead's call is its own
    def test_a_later_call_the_host_denied_never_contests_the_call(self):
        from devkit import hooks
        self.plan_with_link()
        lead, lead_shell = self.call(LEAD)
        member, _ = self.member("npm test")  # its shell never starts
        payload = {"session_id": "s1", "tool_use_id": member["tool_use_id"], "tool_name": "Bash", "agent_id": "a_link",
                   "tool_input": member["tool_input"], "hook_event_name": "PermissionDenied", "cwd": str(self.root),
                   "reason": "auto mode refused it"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            hooks.run("permission-denied", "claude")
        self.starts[lead_shell] = time.time()
        rc, out = self.in_shell(lead_shell, *LEAD_ARGS)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_a_denied_call_the_host_reports_after_a_won_race_is_audited(self):
        from devkit import hooks
        lead = self.killed_after_a_won_race()
        payload = {"session_id": "s1", "tool_use_id": lead["tool_use_id"], "tool_name": "Bash",
                   "tool_input": lead["tool_input"], "hook_event_name": "PermissionDenied", "cwd": str(self.root),
                   "reason": "auto mode refused it"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            hooks.run("permission-denied", "claude")
        found = self.records("calls.text-use-without-result")
        self.assertEqual(len(found), 1)
        self.assertIn("denied", found[0]["detail"])

    # a denial at the prompt fires no hook: the denied call stays open until its time passes, and the same command
    # run again meanwhile is refused with how long that takes and why
    def test_a_retry_after_a_denial_at_the_prompt_says_how_long_it_waits(self):
        self.plan_with_link()
        denied, dshell = self.call(LEAD, timeout=120_000)
        retime(denied, time.time() - 200)
        del self.argvs[dshell]
        again, shell = self.call(LEAD, timeout=120_000)
        rc, out = self.in_shell(shell, *LEAD_ARGS)
        self.assertEqual(rc, 2, out)
        self.assertIn("denied", out)
        self.assertRegex(out, r"about (?:9|10) minute")

    # the agent is killed (no SessionEnd): the next session start closes the calls its process left open, as ended
    # with the session, and audits the state change that ran under the tech lead's call; a live agent's calls stay
    def test_a_killed_sessions_calls_are_closed_and_audited_at_the_next_session_start(self):
        lead = self.killed_after_a_won_race()
        with mock.patch.object(pg, "_pid_running", lambda pid: True):
            self.start_session("s2")
        self.assertEqual(len(pg.open_calls("s1")), 2)
        self.assertEqual(self.records("calls.text-use-without-result"), [])
        with mock.patch.object(pg, "_pid_running", lambda pid: pid != AGENT):
            self.start_session("s2")
        found = self.records("calls.text-use-without-result")
        self.assertEqual(len(found), 1)
        self.assertIn("session-gone", found[0]["detail"])
        self.assertEqual(pg.open_calls("s1"), [])
        self.assertFalse(identity.pin_path("s1", lead["tool_use_id"]).exists())

    # dev sync, which runs every hour, closes them too
    def test_a_killed_sessions_calls_are_closed_and_audited_by_dev_sync(self):
        import argparse
        from devkit import cli, dashboard, registry, work
        self.killed_after_a_won_race()
        args = argparse.Namespace(session=None, finish=False, offline=True, scheduled=False, no_push=True,
                                  quiet=True, renew=None, delay=0, flush_at=0)
        with mock.patch.object(pg, "_pid_running", lambda pid: pid != AGENT), \
                mock.patch.object(registry, "build", lambda **kw: {"projects": [], "sessions": [], "issues": []}), \
                mock.patch.object(registry, "write_outputs", lambda reg: []), \
                mock.patch.object(dashboard, "write", lambda reg: None), \
                mock.patch.object(work, "write_presence", lambda sessions: None), \
                mock.patch.object(cli, "_sync_handoff", lambda a: None):
            self.assertEqual(cli.cmd_sync(args), 0)
        self.assertEqual(len(self.records("calls.text-use-without-result")), 1)
        self.assertEqual(pg.open_calls("s1"), [])



# The ledger's session folders and files as a user's other processes may leave them: a session folder that is a link,
# a ledger file that is a link or a FIFO. devkit never reads, writes or removes through a link, and never blocks.
class LedgerFolderTests(TextPinCase):
    def linked(self, sid="s-link"):
        outside = self.root / f"outside-{sid}"
        outside.mkdir(mode=0o755)
        outside.chmod(0o755)
        pg.CALLS.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, pg.CALLS / sid)
        return outside

    def kept_record(self, sid, tid, ts):
        return json.dumps({"session": sid, "tool_use_id": tid, "ts": ts, "pin_by": "text", "background": True,
                           "caller": "main", "agent": "claude", "cmd": "dev watch --wait"})

    # in a background record's reader: a linked session folder is never listed, read or cleaned
    def test_kept_background_records_are_never_read_or_removed_through_a_linked_session_folder(self):
        outside = self.linked()
        now = time.time()
        (outside / ".toolu_k.background").write_text(self.kept_record("s-link", "toolu_k", now))
        (outside / ".toolu_old.background").write_text(self.kept_record("s-link", "toolu_old", now - 1000))
        self.assertEqual(identity._kept_background("s-link", now), [])
        self.assertEqual(sorted(os.listdir(outside)), [".toolu_k.background", ".toolu_old.background"])
        # swapped for a link once the reader opened it: the stale record of the folder it opened goes, nothing outside
        folder = pg.CALLS / "s-swap"
        folder.mkdir()
        (folder / ".toolu_old.background").write_text(self.kept_record("s-swap", "toolu_old", now - 1000))
        moved, real = self.root / "moved", os.listdir

        def swap(p):
            if not os.path.islink(folder):
                os.rename(folder, moved)
                os.symlink(outside, folder)
            return real(p)
        with mock.patch.object(os, "listdir", swap):
            self.assertEqual(identity._kept_background("s-swap", now), [])
        self.assertTrue(os.path.islink(folder))  # the swap happened
        self.assertEqual(os.listdir(moved), [])
        self.assertEqual(sorted(os.listdir(outside)), [".toolu_k.background", ".toolu_old.background"])

    # a background call's record and a call's contest flag are never written or removed through a linked folder
    def test_a_call_never_keeps_or_drops_its_marks_through_a_linked_session_folder(self):
        outside = self.linked()
        (outside / ".toolu_k.contested").write_text("")
        entry = json.loads(self.kept_record("s-link", "toolu_k", time.time()))
        identity.keep_background(entry)
        identity._drop_marks("s-link", "toolu_k")
        self.assertEqual(os.listdir(outside), [".toolu_k.contested"])

    # a contest flag is never made or read through a linked session folder: a flag planted where the link points
    # marks nothing, and none is made there; in a real folder the flag is made and read
    def test_a_contest_flag_is_never_made_or_read_through_a_linked_session_folder(self):
        outside = self.linked()
        self.assertFalse(identity._contest({"session": "s-link", "tool_use_id": "toolu_c"}))
        self.assertEqual(os.listdir(outside), [])
        (outside / ".toolu_k.contested").write_text("")
        self.assertEqual(identity._contest_mark({"session": "s-link", "tool_use_id": "toolu_k"}), "")
        self.assertEqual(os.listdir(outside), [".toolu_k.contested"])
        (pg.CALLS / "s1").mkdir()
        real = {"session": "s1", "tool_use_id": "toolu_r"}
        self.assertTrue(identity._contest(real))
        self.assertEqual(identity._contest_mark(real), "ledger")

    # a call's end never takes a kept record seen through a linked session folder for its own, so its pin goes; and
    # its use marks are never made, read or removed there
    def test_a_calls_end_and_use_marks_never_go_through_a_linked_session_folder(self):
        outside = self.linked()
        planted = {".toolu_k.background": self.kept_record("s-link", "toolu_k", time.time()),
                   ".toolu_k.used": json.dumps({"pid": 1, "start": 1.0, "inode": 1}),
                   ".toolu_k.uses": json.dumps({"use": "x", "change": True, "listed": False}) + "\n",
                   "toolu_k.json": json.dumps({"tool_use_id": "toolu_k", "session": "s-link", "caller": "main"})}
        for name, text in planted.items():
            (outside / name).write_text(text)
        pin = identity.pin_path("s-link", "toolu_k")
        pin.parent.mkdir(parents=True, exist_ok=True)
        pin.write_text("123")
        identity.note_closed("claude", "s-link", "toolu_k")
        self.assertFalse(os.path.lexists(pin))
        self.assertEqual((identity._read_used("s-link", "toolu_k"), identity.read_uses("s-link", "toolu_k"),
                          identity._read_entry("s-link", "toolu_k")), ({}, [], None))
        identity._mark_used("s-link", "toolu_n", 1, 1.0, 1)
        self.assertFalse(identity._note_use("s-link", "toolu_n", {"cmd": "dev progress add x"},
                                            ["progress", "add", "x"]))
        self.assertEqual(sorted(os.listdir(outside)), sorted(planted))
        self.assertEqual([(outside / n).read_text() for n in sorted(planted)], [planted[n] for n in sorted(planted)])

    # a use mark planted where a linked session folder points never makes the PostToolUse hook flag the tech lead's
    # call as resolved by a dev command its text does not run; the same mark in a real folder does
    def test_a_use_mark_seen_through_a_linked_session_folder_flags_no_unexplained_use(self):
        from devkit import hooks
        outside = self.linked()
        (pg.CALLS / "s1").mkdir()
        for sid, folder in (("s-link", outside), ("s1", pg.CALLS / "s1")):
            (folder / ".toolu_u.used").write_text(json.dumps({"pid": 1, "start": 1.0, "inode": 1}))
            entry = {"pin": str(self.root / "no-pin"), "caller": "main", "cmd": "git status", "agent": "claude",
                     "ts": time.time(), "session": sid, "tool_use_id": "toolu_u"}
            hooks._pin_audits(entry, {"session_id": sid, "tool_use_id": "toolu_u", "hook_event_name": "PostToolUse"},
                              "")
        self.assertEqual([r["ref"]["session"] for r in self.records("caller.token-unexplained-use")], ["s1"])

    # nor the Codex thread check flag a mismatch for it
    def test_a_use_mark_seen_through_a_linked_session_folder_flags_no_codex_thread_mismatch(self):
        from devkit import hooks
        outside = self.linked()
        (pg.CALLS / "s1").mkdir()
        for sid, folder in (("s-link", outside), ("s1", pg.CALLS / "s1")):
            (folder / ".toolu_t.used").write_text(json.dumps({"pid": 1, "start": 1.0, "inode": 1}))
            hooks._codex_thread_mismatch("codex", {"session_id": sid, "tool_use_id": "toolu_t"},
                                         {"thread_id": "thread-other"})
        self.assertEqual([r["ref"]["session"] for r in self.records("caller.mismatch")], ["s1"])

    # the hook's ledger writes (call_opened, call_note) never change the mode of a folder a link points to, nor
    # write there
    def test_a_ledger_write_never_goes_through_a_linked_session_folder(self):
        outside = self.linked()
        data = {"session_id": "s-link", "tool_use_id": "toolu_w", "tool_name": "Bash",
                "tool_input": {"command": "git status"}}
        with self.assertRaises(OSError):
            pg.call_opened("claude", data)
        with self.assertRaises(OSError):
            pg._write_private(pg.CALLS / "s-link" / "toolu_w.json", {"ts": 1})
        self.assertEqual((os.listdir(outside), oct(outside.stat().st_mode & 0o777)), ([], "0o755"))

    # a ledger entry, a kept record or the ledger's own folder that is a FIFO never blocks a reader: it reads as none
    def test_a_fifo_in_the_ledger_never_blocks_a_reader(self):
        import threading
        folder = pg.CALLS / "s1"
        folder.mkdir(parents=True)
        os.mkfifo(folder / "toolu_f.json")
        os.mkfifo(folder / ".toolu_g.background")
        got = {}

        def read():
            got["entry"] = pg.call_entry({"session_id": "s1", "tool_use_id": "toolu_f"})
            got["kept"] = identity._read_kept(folder / ".toolu_g.background")
            pg.call_closed({"session_id": "s1", "tool_use_id": "toolu_f", "hook_event_name": "PostToolUse"})
            got["done"] = True
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(10)
        self.assertEqual(got, {"entry": {}, "kept": None, "done": True})

    # a ledger entry that is a link is never read, whatever it points to
    def test_a_ledger_entry_that_is_a_link_is_never_read(self):
        folder = pg.CALLS / "s1"
        folder.mkdir(parents=True)
        target = self.root / "planted.json"
        target.write_text(json.dumps({"tool_use_id": "toolu_l", "ts": time.time(), "caller": "main", "session": "s1",
                                      "agent": "claude", "cmd": "dev progress add extra"}))
        os.symlink(target, folder / "toolu_l.json")
        self.assertEqual(pg.open_calls("s1"), [])
        self.assertTrue(target.exists())

    # SessionEnd: a contest that lands while the call closes (call_closed can wait on the record's lock) is audited,
    # as at the call's PostToolUse
    def test_a_contest_made_while_the_session_end_closes_the_call_is_audited(self):
        self.plan_with_link()
        data, _, _ = self.hook(LEAD)
        entry = pg.call_entry(data)
        real, contested = pg.call_closed, []

        def contest_then_close(*a, **k):
            contested.append(identity._contest(entry))
            return real(*a, **k)
        with mock.patch.object(pg, "call_closed", contest_then_close):
            identity.end_session(self.agent, "s1")
        self.assertEqual(contested, [True])
        found = [r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"]
        self.assertEqual(len(found), 1)
        self.assertIn("session-ended", found[0]["detail"])

    # two processes that end one call (its PostToolUse and a SessionEnd, or two sweeps) write one contest audit
    def test_two_ends_of_one_contested_call_write_one_audit(self):
        self.plan_with_link()
        data, _, _ = self.hook(LEAD)
        entry = pg.call_entry(data)
        identity._contest(entry)
        mark = identity._contest_mark(entry)
        identity.audit_contested(entry, "s1", data["tool_use_id"], mark)
        identity._drop_marks("s1", data["tool_use_id"])  # the first end's marks go before the second end audits
        identity.audit_contested(entry, "s1", data["tool_use_id"], mark, "session-ended")
        self.assertEqual(len([r for r in self.records("calls.pin-contested") if r["ref"]["result"] == "flagged"]), 1)

    # anything planted at a call's audit claim before its only end (a FIFO, a link, a dangling link, a folder, an
    # empty file older than the call) never stops its contest audit; what a link points to is never touched, and the
    # claim the audit makes then keeps a second end from writing it again
    def test_something_planted_at_the_audit_claim_never_stops_the_contest_audit(self):
        self.plan_with_link()
        data, _, _ = self.hook(LEAD)
        entry = pg.call_entry(data)
        target = self.root / "target"
        target.write_text("t")
        flagged = lambda: len([r for r in self.records("calls.pin-contested")  # noqa: E731
                               if r["ref"]["result"] == "flagged"])
        for kind in ("fifo", "link", "dangling-link", "folder", "old-file"):
            with self.subTest(kind):
                tid = f"toolu_claim_{kind.replace('-', '_')}"
                claim = identity.audit_claim_path("s1", tid)
                if kind == "fifo":
                    os.mkfifo(claim)
                elif kind == "link":
                    os.symlink(target, claim)
                elif kind == "dangling-link":
                    os.symlink(self.root / "nowhere", claim)
                elif kind == "folder":
                    claim.mkdir()
                    (claim / "x").write_text("")
                else:
                    claim.write_text("")
                    old = float(entry["ts"]) - 100
                    os.utime(claim, (old, old))
                before = flagged()
                identity.audit_contested(entry, "s1", tid, "pin")
                self.assertEqual(flagged(), before + 1)
                identity.audit_contested(entry, "s1", tid, "pin", "session-ended")
                self.assertEqual(flagged(), before + 1)
        self.assertEqual(target.read_text(), "t")

    # a dangling link or a folder at a call's use mark is no use mark: the PostToolUse hook flags no unexplained use
    # of the tech lead's token for it, while a regular file there is one
    def test_only_a_regular_file_counts_as_a_use_mark(self):
        from devkit import hooks
        for sid, kind in (("s-link", "dangling-link"), ("s-dir", "folder"), ("s-file", "file")):
            folder = pg.CALLS / sid
            folder.mkdir(parents=True)
            mark = folder / ".toolu_u.used"
            if kind == "dangling-link":
                os.symlink(self.root / "nowhere", mark)
            elif kind == "folder":
                mark.mkdir()
            else:
                mark.write_text(json.dumps({"pid": 1, "start": 1.0, "inode": 1}))
            entry = {"pin": str(self.root / "no-pin"), "caller": "main", "cmd": "git status", "agent": "claude",
                     "ts": time.time(), "session": sid, "tool_use_id": "toolu_u"}
            hooks._pin_audits(entry, {"session_id": sid, "tool_use_id": "toolu_u", "hook_event_name": "PostToolUse"},
                              "")
        self.assertEqual([r["ref"]["session"] for r in self.records("caller.token-unexplained-use")], ["s-file"])

    # an audit that cannot be written is recorded as a swallowed devkit error, never lost silently
    def test_a_contest_audit_that_fails_is_reported(self):
        from devkit import audit, bugs
        seen = []
        entry = {"pin": "/x/toolu_e.pin", "agent": "claude", "cmd": "dev progress add extra"}
        with mock.patch.object(audit, "record", side_effect=OSError("disk full")), \
                mock.patch.object(bugs, "swallowed", lambda c, e: seen.append(str(e))):
            identity.audit_contested(entry, "s1", "toolu_e", "pin")
        self.assertEqual(seen, ["disk full"])

    # 1,000 flags in the first folder never starve the others: each sweep starts at another folder
    def test_a_flood_of_flags_in_one_folder_never_starves_the_others(self):
        old = time.time() - 10 * identity.FLAG_GRACE_S
        for sid, n in (("s-a", 8), ("s-b", 1)):
            folder = pg.CALLS / sid
            folder.mkdir(parents=True)
            for i in range(n):
                flag = folder / f".toolu_{i}.contested"
                flag.mkdir()  # a planted folder: each takes its entry and its own removal
                (flag / "x").write_text("")
                os.utime(flag, (old, old))
        with mock.patch.object(identity, "_SWEEP_MAX", 2), mock.patch.object(identity, "_sweep_start", lambda n: 1):
            identity.sweep_gone()
        self.assertEqual(os.listdir(pg.CALLS / "s-b"), [])

    # the sweep reads at most a bounded number of ledger entries and names, however many a folder holds
    def test_the_sweep_reads_a_bounded_number_of_entries(self):
        folder = pg.CALLS / "s1"
        folder.mkdir(parents=True)
        for i in range(40):
            (folder / f"toolu_{i:02d}.json").write_text(json.dumps({"ts": time.time(), "agent_pid": 1}))
        real, reads = pg._read_entry_at, []

        def counted(fd, name):
            reads.append(name)
            return real(fd, name)
        with mock.patch.object(identity, "_LIST_MAX", 10), mock.patch.object(pg, "_read_entry_at", counted), \
                mock.patch.object(pg, "_pid_running", lambda pid: True):
            identity.sweep_gone()
        self.assertLessEqual(len(reads), 10)


if __name__ == "__main__":
    unittest.main()
