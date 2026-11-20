# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The PreToolUse hook (`dev hook pre-tool`, and `dev hook guard` for settings that predate the name) never denies or
asks: whatever a call does, the only permission decision its output carries is `allow` on a Codex rewrite, which is how
Codex applies a changed input. What devkit used to refuse, it now says as one advice line in the call's context: a crew
member's edit outside its scope or by a read-only lane, a launch past the crew's slots, the main session editing a file
a working member holds, a lane call the router cannot place, and a commit that would include unfinished work. Codex
reads no PreToolUse context, so a Codex member's advice reaches it as a notice at its next PostToolUse.

Every hook runs as the agent runs it: `dev hook <event> --agent claude|codex` with the payload on stdin, in a sandbox
Mac (tests/test_acceptance_crew_sessions.py, tests/_sandbox.py).

Run: python3 -m unittest tests.test_pre_tool_hook -v (and with /usr/bin/python3, for Python 3.9)
"""
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
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "lib"))
from test_acceptance_crew_sessions import GIT_ID  # noqa: E402  (activates the hermetic net and the sandbox)
from test_codex_launch import LEAD, TANK, TRINITY, CodexCrew  # noqa: E402

CLAUDE_LEAD = "c1a0de00-0000-4000-8000-00000000aa01"
ADVICE = "devkit · advice: "
UNFINISHED = "These lines leave unfinished work"
UNROUTED = "devkit could not route the dev progress, dev crew or dev watch call in this command to your lane"


class PreToolCase(CodexCrew):
    def lead_on(self, agent: str, **progress):
        # a tech lead whose run gives Tank the api step (scope api/**) and Trinity the ui step, Tank launched
        if agent == "codex":
            s = self.lead(**progress)
            self.spawn(s, TANK, "tank", "api")
            return s
        self.config(**{"max_parallel": 2, "reserve": 0, **progress})
        s = self.claude(CLAUDE_LEAD, self.api)
        s.ok("progress", "start", "Invoice export", "--plan", "api=30m [backend]; ui=30m [frontend]")
        for step, name in (("api", "Tank"), ("ui", "Trinity")):
            s.ok("progress", "assign", step, "--to", name, "--scope", f"{step}/**")
        self.assertEqual(s.launch(TANK, "tank", "Tank: api"), "")
        return s

    @staticmethod
    def member(agent: str, aid: str, name: str) -> dict:
        return {"agent_id": aid, "agent_type": name if agent == "codex" else f"devkit:{name}"}

    def edit(self, s, path: str, n: int, **who) -> dict:
        # a file edit as each agent makes it: Claude's Write tool, Codex's apply_patch
        if s.agent == "codex":
            call = dict(tool_name="apply_patch", tool_use_id=f"exec-patch-{n}",
                        tool_input={"command": f"*** Begin Patch\n*** Add File: {path}\n+export {{}};\n*** End Patch\n"})
        else:
            call = dict(tool_name="Write", tool_use_id=f"toolu_write_{n}",
                        tool_input={"file_path": str(s.repo / path), "content": "export {};\n"})
        return s.hook("pre-tool", s.payload("PreToolUse", **call, **who))

    def shell(self, s, cmd: str, n: int, event="pre-tool", **who) -> dict:
        prefix = "exec" if s.agent == "codex" else "toolu"
        return s.hook(event, s.payload("PreToolUse", tool_name="Bash", tool_use_id=f"{prefix}-sh-{n}",
                                       tool_input={"command": cmd}, **who))

    def assertDecidesNothing(self, out: dict):
        spec = out.get("hookSpecificOutput") or {}
        self.assertNotIn("permissionDecision", spec, out)
        self.assertNotIn("decision", out, out)

    def assertRewritesForCodex(self, out: dict):
        # a Codex rewrite carries `allow`, how Codex applies updatedInput, and nothing that refuses or asks
        spec = out.get("hookSpecificOutput") or {}
        self.assertEqual(spec.get("permissionDecision"), "allow", out)
        self.assertIn("updatedInput", spec, out)
        self.assertNotIn("decision", out, out)

    def codex_policy(self, policy):
        # Codex's own configuration as devkit's setup writes it: the approval policy at the top of config.toml
        f = self.home / ".codex" / "config.toml"
        f.parent.mkdir(parents=True, exist_ok=True)
        if policy is not None:
            f.write_text(f'approval_policy = "{policy}"\nsandbox_mode = "danger-full-access"\n')
        elif f.exists():
            f.unlink()

    def advice(self, out: dict) -> list:
        text = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""
        return [p[len(ADVICE):] for p in text.split("\n\n") if p.startswith(ADVICE)]

    @staticmethod
    def context(out: dict) -> str:
        return ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""

    def post_edit(self, s, path: str, n: int, **who) -> dict:
        # the PostToolUse of a Codex member's apply_patch: where Codex reads what devkit tells the member
        call = dict(tool_name="apply_patch", tool_use_id=f"exec-patch-{n}",
                    tool_input={"command": f"*** Begin Patch\n*** Add File: {path}\n+export {{}};\n*** End Patch\n"})
        return s.hook("post-tool", s.payload("PostToolUse", tool_response={"success": True}, **call, **who))


class NoDecisionTests(PreToolCase):
    def test_a_force_push_to_main_runs_with_no_decision(self):
        # under the handler's name and under the name older settings still call it by
        for agent in ("claude", "codex"):
            s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
            for n, event in enumerate(("pre-tool", "guard")):
                with self.subTest(agent=agent, event=event):
                    out = self.shell(s, "git push --force origin main", n, event=event)
                    self.assertDecidesNothing(out)
                    self.assertEqual(self.advice(out), [])

    def test_a_write_to_devkits_own_state_runs_with_no_decision(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
                target = self.state / "runs" / f"{agent}-forged.json"
                if agent == "codex":
                    call = dict(tool_name="apply_patch", tool_use_id="exec-state-1", tool_input={
                        "command": f"*** Begin Patch\n*** Add File: {target}\n+{{}}\n*** End Patch\n"})
                else:
                    call = dict(tool_name="Write", tool_use_id="toolu_state_1",
                                tool_input={"file_path": str(target), "content": "{}"})
                for event in ("pre-tool", "guard"):
                    self.assertDecidesNothing(s.hook(event, s.payload("PreToolUse", **call)))

    def test_a_members_single_lane_call_is_routed_with_codexs_allow_even_where_codex_asks(self):
        # one plain devkit call and nothing else: Codex's `allow` approves only devkit's own call, so it goes out
        # whatever Codex's approval policy (no config.toml here, and the call's own policy asks)
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                out = self.shell(s, 'dev progress note "half done"', 1, permission_mode="default",
                                 **self.member(agent, TANK, "tank"))
                if agent == "codex":
                    self.assertRewritesForCodex(out)
                else:
                    self.assertDecidesNothing(out)
                self.assertIn(f"DEVKIT_FROM_AGENT={TANK}", out["hookSpecificOutput"]["updatedInput"]["command"])


class AdviceTests(PreToolCase):
    def test_a_members_edit_outside_its_scope_runs_with_the_scope_advice(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                who = self.member(agent, TANK, "tank")
                inside = self.edit(s, "api/view.ts", 1, **who)
                self.assertDecidesNothing(inside)
                self.assertEqual(self.advice(inside), [])
                outside = self.edit(s, "ui/view.ts", 2, **who)
                self.assertDecidesNothing(outside)
                self.assertEqual(self.advice(outside), [
                    "ui/view.ts is outside Backend engineer (Tank)'s scope (api/**): another crew member may own it. "
                    "Ask the tech lead to widen it: dev crew ask \"may I change ui/view.ts? why: …\""])

    def test_a_read_only_members_edit_runs_with_the_read_only_advice(self):
        s = self.lead_on("claude")
        s.ok("progress", "add", "review=20m")
        s.ok("progress", "assign", "review", "--to", "Seraph", "--scope", "docs/**")
        self.assertEqual(s.launch("claude-seraph-1", "seraph", "Seraph: review"), "")
        out = self.edit(s, "api/view.ts", 1, **self.member("claude", "claude-seraph-1", "seraph"))
        self.assertDecidesNothing(out)
        self.assertEqual(len(self.advice(out)), 1, out)
        self.assertIn("Code reviewer (Seraph) is review-only: report the change with file:line", self.advice(out)[0])

    def test_a_launch_past_the_crews_slots_runs_with_the_slot_advice(self):
        s = self.lead_on("claude", max_parallel=1)
        call = dict(tool_name="Agent", tool_use_id="launch-claude-2", tool_input={
            "description": "Trinity: ui", "subagent_type": "devkit:trinity", "prompt": "Build the screen."})
        out = s.hook("pre-tool", s.payload("PreToolUse", **call))
        self.assertDecidesNothing(out)
        self.assertEqual(len(self.advice(out)), 1, out)
        self.assertIn("1 of 1 crew slots are in use", self.advice(out)[0])

    def test_a_codex_member_launched_past_the_crews_slots_hears_it_at_its_next_post_tool_use(self):
        # Codex fires no PreToolUse for spawn_agent: the launch is judged at its start, and the member's later edits
        # are advised in its lane, since Codex reads no PreToolUse context
        s = self.lead_on("codex", max_parallel=1)
        self.spawn(s, TRINITY, "trinity", "ui")
        who = self.member("codex", TRINITY, "trinity")
        self.assertDecidesNothing(self.edit(s, "ui/view.ts", 1, **who))
        told = self.context(self.post_edit(s, "ui/view.ts", 1, **who))
        self.assertIn("devkit recorded this launch over the crew's limit", told)

    def test_a_codex_members_advice_reaches_it_once_at_its_next_post_tool_use(self):
        s = self.lead_on("codex")
        who = self.member("codex", TANK, "tank")
        self.assertDecidesNothing(self.edit(s, "ui/view.ts", 1, **who))
        told = self.context(self.post_edit(s, "ui/view.ts", 1, **who))
        self.assertIn("ui/view.ts is outside Backend engineer (Tank)'s scope (api/**)", told)
        self.assertNotIn("is outside", self.context(self.post_edit(s, "ui/view.ts", 2, **who)))  # told once
        # a Claude Code member reads it in the call's own context: nothing waits in its lane
        claude = self.lead_on("claude")
        self.edit(claude, "ui/view.ts", 1, **self.member("claude", TANK, "tank"))
        self.assertEqual([m for m in claude.messages() if m.get("to") == TANK and "is outside" in m["text"]], [])

    def test_the_main_session_editing_a_working_members_file_gets_the_holder_advice(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                self.shell(s, "git status --short", 1, **self.member(agent, TANK, "tank"))  # Tank is working
                out = self.edit(s, "api/view.ts", 2)
                self.assertDecidesNothing(out)
                self.assertEqual(len(self.advice(out)), 1, out)
                self.assertIn("api/view.ts belongs to Backend engineer (Tank) while they work (scope api/**)",
                              self.advice(out)[0])

    def test_two_lane_calls_in_one_command_are_stamped_on_claude_and_on_a_codex_that_never_asks(self):
        # the router places one call per command; the stamp exports the lane's variable for the whole command
        cmd = 'dev progress note "one" && dev progress note "two"'
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                self.codex_policy("never")
                out = self.shell(s, cmd, 1, **self.member(agent, TANK, "tank"))
                if agent == "codex":
                    self.assertRewritesForCodex(out)
                else:
                    self.assertDecidesNothing(out)
                self.assertEqual(self.advice(out), [])
                new = out["hookSpecificOutput"]["updatedInput"]["command"]
                self.assertIn(f"export DEVKIT_FROM_AGENT={TANK}; readonly DEVKIT_FROM_AGENT; {cmd}", new)
                self.assertNotIn(s.sid, new)  # a member's shell never carries the root thread's session id

    def test_a_compound_codex_command_is_left_alone_where_codex_would_ask_and_the_member_is_told(self):
        # Codex applies a changed command only with `allow`, which also skips the approval Codex would ask for: a
        # command that is more than one plain devkit call is not rewritten unless Codex never asks, so it runs as the
        # main session, the member hears it at its next PostToolUse and the tech lead gets a copy
        cmd = 'dev progress note "one" && dev progress note "two"'
        s = self.lead_on("codex")
        who = self.member("codex", TANK, "tank")
        cases = (("on-request", "bypassPermissions"),  # Codex's config.toml asks
                 ("never", "default"),  # the session's own policy asks (a profile or a command-line flag)
                 (None, "bypassPermissions"))  # no config.toml: Codex's own default asks
        for n, (policy, mode) in enumerate(cases, 1):
            with self.subTest(policy=policy, mode=mode):
                self.codex_policy(policy)
                out = self.shell(s, cmd, n, permission_mode=mode, **who)
                self.assertDecidesNothing(out)
                self.assertNotIn("updatedInput", out.get("hookSpecificOutput") or {}, out)
                call = dict(tool_name="Bash", tool_use_id=f"exec-sh-{n}", tool_input={"command": cmd})
                told = self.context(s.hook("post-tool", s.payload("PostToolUse", tool_response={"output": ""},
                                                                  permission_mode=mode, **call, **who)))
                self.assertIn(UNROUTED, told)
        lead = [m["text"] for m in s.messages() if m.get("to") == "main" and "could not route" in m["text"]]
        self.assertEqual(len(lead), len(cases), s.messages())

    def test_a_codex_members_call_beyond_plain_words_is_left_alone_where_codex_asks_and_allowed_where_it_never_asks(
            self):
        # where Codex asks, only one bare devkit call of plain words gets Codex's `allow`: zsh's expansions, a brace
        # expansion, a compound command and a piped wait get no rewrite and no decision, so each runs as typed, the
        # member hears it at its next PostToolUse and the tech lead gets a copy; where Codex never asks, each one is
        # routed with `allow`
        forms = ("dev crew ask =(sh -c 'touch x')", "dev crew ask *(e:'touch x':)", "dev crew ask ${(e)q}",
                 "dev crew ask {a,b}", 'dev progress note "one" && dev progress note "two"',
                 "dev watch --wait --for me --timeout 9 | tail -4")
        s = self.lead_on("codex")
        who = self.member("codex", TANK, "tank")
        for n, cmd in enumerate(forms, 1):
            with self.subTest(cmd=cmd, codex="asks"):
                self.codex_policy(None)
                out = self.shell(s, cmd, n, permission_mode="default", **who)
                self.assertDecidesNothing(out)
                self.assertNotIn("updatedInput", out.get("hookSpecificOutput") or {}, out)
                call = dict(tool_name="Bash", tool_use_id=f"exec-sh-{n}", tool_input={"command": cmd})
                told = self.context(s.hook("post-tool", s.payload("PostToolUse", tool_response={"output": ""},
                                                                  permission_mode="default", **call, **who)))
                self.assertIn(UNROUTED, told)
            with self.subTest(cmd=cmd, codex="never asks"):
                self.codex_policy("never")
                out = self.shell(s, cmd, 100 + n, **who)
                self.assertRewritesForCodex(out)
                self.assertIn(f"DEVKIT_FROM_AGENT={TANK}", out["hookSpecificOutput"]["updatedInput"]["command"])
        lead = [m["text"] for m in s.messages() if m.get("to") == "main" and "could not route" in m["text"]]
        self.assertEqual(len(lead), len(forms), s.messages())

    def test_a_codex_lane_call_no_rewrite_can_reach_is_told_to_the_member_and_the_tech_lead(self):
        # Codex's exec_command may send its command as a list of words, which no rewrite replaces: the call runs as
        # the main session, so the member hears it at its next PostToolUse and the tech lead gets a copy
        s = self.lead_on("codex")
        who = self.member("codex", TANK, "tank")
        call = dict(tool_name="exec_command", tool_use_id="exec-list-1",
                    tool_input={"cmd": ["bash", "-lc", 'dev progress note "one" && dev progress note "two"']})
        out = s.hook("pre-tool", s.payload("PreToolUse", **call, **who))
        self.assertDecidesNothing(out)
        told = self.context(s.hook("post-tool", s.payload("PostToolUse", tool_response={"output": ""}, **call,
                                                          **who)))
        self.assertIn(UNROUTED, told)
        lead = [m["text"] for m in s.messages() if m.get("to") == "main" and "could not route" in m["text"]]
        self.assertEqual(len(lead), 1, s.messages())
        self.assertIn("Backend engineer (Tank) ran `bash -lc", lead[0])

    def test_a_commit_with_unfinished_work_gets_the_stub_advice_unless_it_is_a_wip_commit(self):
        (self.api / "parser.py").write_text("def parse(text):\n    # " + "TO" + "DO: handle quotes\n    return text\n")
        subprocess.run(["git", "add", "parser.py"], cwd=str(self.api), check=True, capture_output=True,
                       env=dict(self.env, **GIT_ID))
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
                out = self.shell(s, 'git commit -m "feat: parse quoted fields"', 1)
                self.assertDecidesNothing(out)
                self.assertEqual(len(self.advice(out)), 1, out)
                self.assertTrue(self.advice(out)[0].startswith(UNFINISHED), out)
                self.assertIn("parser.py:2 todo", self.advice(out)[0])
                wip = self.shell(s, "dev commit --wip", 2)
                self.assertDecidesNothing(wip)
                self.assertFalse(any(a.startswith(UNFINISHED) for a in self.advice(wip)), wip)


class HandlerNameTests(PreToolCase):
    def test_the_guard_name_runs_the_same_handler_as_pre_tool(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.lead_on(agent)
                who = self.member(agent, TANK, "tank")
                by_name = [self.edit(s, "ui/view.ts", n, **who) for n in (1, 2)]
                old = s.hook("guard", s.payload("PreToolUse", tool_name="Write", tool_use_id="toolu_write_3",
                                                tool_input={"file_path": str(s.repo / "ui" / "view.ts"),
                                                            "content": "export {};\n"}, **who)) \
                    if agent == "claude" else s.hook("guard", s.payload(
                        "PreToolUse", tool_name="apply_patch", tool_use_id="exec-patch-3", tool_input={
                            "command": "*** Begin Patch\n*** Add File: ui/view.ts\n+export {};\n*** End Patch\n"},
                        **who))
                self.assertEqual(old, by_name[1])
                self.assertEqual(len(self.advice(old)), 1, old)

    def test_the_main_sessions_first_call_records_that_the_hook_ran_for_the_agents_self_test(self):
        from devkit import providers
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                s = self.claude(CLAUDE_LEAD, self.api) if agent == "claude" else self.codex(LEAD, self.api)
                record = self.state / "sessions" / f"{agent}-{s.sid}.json"
                self.assertFalse(providers.pre_tool_seen(record))
                self.shell(s, "git status --short", 1)
                self.assertTrue(providers.pre_tool_seen(record))

    def test_both_settings_templates_call_pre_tool(self):
        claude = json.loads((ROOT / "config" / "claude" / "settings.json").read_text())
        codex = json.loads((ROOT / "config" / "codex" / "hooks.json").read_text())
        for name, conf in (("claude", claude), ("codex", codex)):
            commands = [h["command"] for e in conf["hooks"]["PreToolUse"] for h in e["hooks"]]
            self.assertEqual(commands, [f"'{{{{DEVKIT}}}}/bin/dev' hook pre-tool --agent {name}"], name)


class HookErrorLogTests(unittest.TestCase):
    # a handler that fails is logged with its traceback, and the log holds no secret the traceback quoted

    def test_a_handlers_traceback_is_logged_with_its_secrets_redacted(self):
        from devkit import hooks
        key = "AKIA" + "Q7Z2X9W4V6U8T3S5"  # an AWS access key id, in two parts so no scanner reads this file as one
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)

        def failing(agent):
            raise RuntimeError(f"the payload carried {key}")
        with mock.patch.dict(hooks.HANDLERS, {"post-tool": failing}), mock.patch.object(hooks, "STATE", tmp), \
                mock.patch.object(hooks, "_bug_hook_error"), mock.patch.object(hooks, "_own_identity", return_value={}):
            self.assertEqual(hooks.run("post-tool", "claude"), 0)
        log = (tmp / "logs" / "hooks.log").read_text()
        self.assertIn("post-tool claude error", log)
        self.assertIn("RuntimeError: the payload carried AKIA", log)
        self.assertNotIn(key, log)


class CodexAllowTests(unittest.TestCase):
    # hooks._codex_may_rewrite, in-process: Codex applies a changed command only with `allow`, which also skips the
    # approval Codex would ask for. So where Codex asks, a call is rewritten only when its input is the command alone
    # and the new command is one bare devkit coordination call made of plain words (single-quoted words, double-quoted
    # words with no $, backtick or backslash, unquoted words of plain characters), behind only the variables devkit
    # puts in front; any rewrite goes out when Codex never asks: devkit's codex.approval_policy, the top level of
    # Codex's config.toml and the call's permission_mode all say so
    STAMPED = "export DEVKIT_FROM_AGENT=a1; readonly DEVKIT_FROM_AGENT; dev progress note x && dev progress note y"

    def setUp(self):
        from devkit import core
        self.codex_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.codex_home, True)
        for p in (mock.patch.object(core, "CODEX_HOME", self.codex_home),
                  mock.patch.dict(core.CFG, {"codex": {"mode": "full", "approval_policy": "never"}})):
            p.start()
            self.addCleanup(p.stop)

    def may(self, new: str, mode="bypassPermissions", **other) -> bool:
        # `other`: input keys the call carries besides its command, kept in the rewrite as they are
        from devkit import hooks
        call = {"session_id": "s", "agent_id": "a1", "tool_name": "Bash", "tool_input": {"command": "dev x", **other}}
        if mode:
            call["permission_mode"] = mode
        return hooks._codex_may_rewrite(call, {"command": new, **other})

    def toml(self, text):
        f = self.codex_home / "config.toml"
        if text is not None:
            f.write_text(text)
        elif f.exists():
            f.unlink()

    def test_one_plain_devkit_coordination_call_is_rewritten_whatever_codex_asks(self):
        self.toml('approval_policy = "on-request"\n')
        for new in ('DEVKIT_SESSION=codex:s DEVKIT_FROM_AGENT=a1 dev progress step "parser; tests" --check "a && b"',
                    "DEVKIT_SPEAKER=main DEVKIT_SESSION=codex:s dev watch --wait --timeout 10",
                    "DEVKIT_FROM_AGENT=a1 dev crew ask 'may I widen it? $HOME `id` a && b'",
                    "DEVKIT_SPEAKER=main dev session ask @0199a213 'May I?'",
                    'DEVKIT_FROM_AGENT=a1 dev progress note "half done" --next "the 2nd part, 50%: tests/x.py"'):
            with self.subTest(new=new):
                self.assertTrue(self.may(new, mode="default"))
        for new in (self.STAMPED,
                    "cd api && DEVKIT_FROM_AGENT=a1 dev progress step x",
                    "DEVKIT_FROM_AGENT=a1 dev progress step x | tail -3",
                    "PATH=/tmp/elsewhere DEVKIT_FROM_AGENT=a1 dev progress step x",  # another program named dev
                    "DEVKIT_FROM_AGENT=a1 ./dev progress step x",
                    "DEVKIT_FROM_AGENT=a1 bin/dev progress step x",  # a repository's own bin/dev
                    '"DEVKIT_FROM_AGENT=a1" dev progress step x',  # a quoted word is the program, not a variable
                    "DEVKIT_FROM_AGENT=a1 'dev' progress step x",
                    "DEVKIT_FROM_AGENT=a1 dev release",  # a devkit call, but no coordination call
                    "DEVKIT_FROM_AGENT=a1 dev progress step $(touch /tmp/x)",
                    "DEVKIT_FROM_AGENT=a1 dev",
                    # what zsh and bash expand in a word that looks plain to a reader
                    "DEVKIT_FROM_AGENT=a1 dev crew ask =(sh -c 'touch /tmp/x')",
                    "DEVKIT_FROM_AGENT=a1 dev crew ask *(e:'touch /tmp/x':)",
                    "DEVKIT_FROM_AGENT=a1 dev crew ask ${(e)q}",
                    "DEVKIT_FROM_AGENT=a1 dev crew ask {a,b}",
                    "DEVKIT_FROM_AGENT=a1 dev crew ask \"$(cat <<'EOF'\nmay I widen it? a && b\nEOF\n)\"",
                    'DEVKIT_FROM_AGENT=a1 dev progress note "a $HOME"',
                    'DEVKIT_FROM_AGENT=a1 dev progress note "a `id`"',
                    'DEVKIT_FROM_AGENT=a1 dev progress note "a \\" b"',
                    "DEVKIT_FROM_AGENT=a1 dev progress note ~/notes",
                    "DEVKIT_FROM_AGENT=a1 dev progress note =ls",
                    "DEVKIT_FROM_AGENT=a1 dev progress note 'a''b'",
                    'DEVKIT_FROM_AGENT=a1 dev progress note "a"b',
                    "DEVKIT_FROM_AGENT=a1 dev progress note x\ndev progress note y",
                    "DEVKIT_FROM_AGENT=a1 dev progress note x;",
                    "DEVKIT_FROM_AGENT=a1 dev progress note !x",
                    "DEVKIT_FROM_AGENT=a1 dev progress note x#y"):
            with self.subTest(new=new):
                self.assertFalse(self.may(new, mode="default"))

    def test_a_plain_call_with_input_beyond_its_command_is_rewritten_only_where_codex_never_asks(self):
        # Codex's `allow` approves the whole call: another input key (a background run, a folder) is approved with it
        new = "DEVKIT_FROM_AGENT=a1 dev progress note x"
        self.toml('approval_policy = "on-request"\n')
        for other in ({"run_in_background": True}, {"workdir": "/tmp"}):
            with self.subTest(other=other):
                self.assertTrue(self.may(new, mode="default"))
                self.assertFalse(self.may(new, mode="default", **other))
                self.toml('approval_policy = "never"\n')
                self.assertTrue(self.may(new, **other))
                self.toml('approval_policy = "on-request"\n')

    def test_a_compound_command_is_rewritten_only_when_every_setting_says_codex_never_asks(self):
        from devkit import core
        self.toml('model = "gpt-6.1-sol"\napproval_policy = "never"  # devkit setup\n')
        self.assertTrue(self.may(self.STAMPED))
        cases = (("Codex's config.toml asks", 'approval_policy = "on-request"\n', "bypassPermissions", "never"),
                 ("no config.toml", None, "bypassPermissions", "never"),
                 ("only a profile says never", 'approval_policy = "untrusted"\n[profiles.fast]\n'
                                               'approval_policy = "never"\n', "bypassPermissions", "never"),
                 ("only a table says never", '[projects."/x"]\napproval_policy = "never"\n', "bypassPermissions",
                  "never"),
                 ("a comment says never", '# approval_policy = "never"\n', "bypassPermissions", "never"),
                 ("the call's own policy asks", 'approval_policy = "never"\n', "default", "never"),
                 ("the call names no policy", 'approval_policy = "never"\n', None, "never"),
                 ("devkit's setting asks", 'approval_policy = "never"\n', "bypassPermissions", "on-request"))
        for why, text, mode, setting in cases:
            with self.subTest(why), mock.patch.dict(core.CFG, {"codex": {"approval_policy": setting}}):
                self.toml(text)
                self.assertFalse(self.may(self.STAMPED, mode=mode))


class AdviceFunctionTests(unittest.TestCase):
    # the advice functions in-process, as the hook calls them, on a run in a temporary state
    def setUp(self):
        from test_progress import TempState
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def git_repo(self, name: str) -> Path:
        r = self.tmp / name
        for args in (["init", "-q", str(r)], ["-C", str(r), "-c", "user.email=a@example.com", "-c", "user.name=a",
                                             "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *args], check=True, capture_output=True)
        return r

    @staticmethod
    def edit(cwd, path, aid="a1") -> dict:
        d = {"session_id": "s1", "tool_name": "Edit", "cwd": str(cwd), "tool_input": {"file_path": str(path)}}
        return dict(d, agent_id=aid) if aid else d

    def test_scope_advice_keeps_parallel_members_apart_and_leaves_scratch_files_alone(self):
        from devkit import hooks, progress as pg
        from test_progress import launch
        repo = self.git_repo("api")
        pg.start("Feature", plan=["parser", "ui"])
        pg.assign("parser", "Neo", scope="lib/parser/**,tests/test_parser.py")
        launch("a1", "devkit:neo", "Neo: parser")
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, repo / "lib/parser/core.py")), [])
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, repo / "tests/test_parser.py")), [])
        told = hooks._scope_advice("claude", self.edit(repo, repo / "lib/ui/app.ts"))
        self.assertEqual(len(told), 1, told)
        self.assertIn("lib/ui/app.ts is outside Senior engineer (Neo)'s scope", told[0])
        self.assertIn("dev crew ask", told[0])
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, repo / "lib/ui/app.ts", aid=None)), [])
        scratch = Path(tempfile.mkdtemp())  # a scratch file outside any checkout is nobody's
        self.addCleanup(shutil.rmtree, scratch, True)
        self.assertEqual(hooks._scope_advice("claude", self.edit(repo, scratch / "notes.md")), [])
        other = self.git_repo("billing")  # another repository is not scratch
        self.assertIn("outside Senior engineer (Neo)'s scope",
                      " ".join(hooks._scope_advice("claude", self.edit(repo, other / "src/app.py"))))

    def test_scope_advice_follows_the_member_into_a_worktree_of_the_same_repository(self):
        from devkit import hooks, progress as pg
        from test_progress import launch
        main, wt = self.git_repo("api"), self.tmp / "wt"
        subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", "-b", "feat/x", str(wt)], check=True,
                       capture_output=True)
        pg.start("Feature", plan=["installer"])
        pg.assign("installer", "Kid", scope="install.sh,tests/test_install*.py")
        launch("a1", "devkit:kid", "Kid: installer")
        self.assertEqual(hooks._scope_advice("claude", self.edit(main, wt / "install.sh")), [])
        self.assertEqual(hooks._scope_advice("claude", self.edit(main, wt / "tests" / "test_install_curl.py")), [])
        self.assertIn("lib/cli.py is outside DevEx engineer (Kid)'s scope",
                      " ".join(hooks._scope_advice("claude", self.edit(main, wt / "lib" / "cli.py"))))

    def test_crew_limits_advice_names_the_working_member_that_holds_the_main_sessions_file(self):
        from devkit import hooks, progress as pg
        from test_progress import launch
        repo = self.git_repo("api")
        pg.start("Feature", plan=["parser"])
        pg.assign("parser", "Neo", scope="lib/parser/**")
        launch("a1", "devkit:neo", "Neo: parser")
        told = hooks._crew_limits_advice("claude", self.edit(repo, repo / "lib/parser/core.py", aid=None))
        self.assertEqual(len(told), 1, told)
        self.assertIn("belongs to Senior engineer (Neo) while they work (scope lib/parser/**)", told[0])
        self.assertEqual(hooks._crew_limits_advice("claude", self.edit(repo, repo / "README.md", aid=None)), [])

    def test_only_an_agent_launch_gets_the_slot_advice_never_a_monitor(self):
        from devkit import core, hooks, progress as pg
        from test_progress import launch
        pg.start("Feature", plan=["parser", "ui"])
        launch("a1", "devkit:neo", "Neo: parser")
        with mock.patch.dict(core.CFG, {"progress": {"max_parallel": 1, "reserve": 0}}):
            agent = {"session_id": "s1", "tool_name": "Agent", "tool_use_id": "toolu_l2",
                     "tool_input": {"description": "Trinity: ui", "subagent_type": "devkit:trinity", "prompt": "x"}}
            monitor = {"session_id": "s1", "tool_name": "Monitor", "tool_use_id": "toolu_m1",
                       "tool_input": {"command": "tail -f build.log"}}
            self.assertIn("crew slots are in use", " ".join(hooks._crew_limits_advice("claude", agent)))
            self.assertEqual(hooks._crew_limits_advice("claude", monitor), [])

    def test_the_pre_tool_handler_keeps_a_read_only_lanes_worktree_state_before_its_call(self):
        # hooks._watch_writes, run by the pre-tool handler: the write detection of review and read-only lanes
        import contextlib
        import io
        from devkit import hooks, progress as pg
        from test_progress import launch
        repo = self.git_repo("api")
        (repo / "notes.md").write_text("draft\n")
        pg.start("Feature", plan=["review"])
        pg.assign("review", "Seraph", scope="lib/**")
        launch("a1", "devkit:seraph", "Seraph: review")
        call = {"session_id": "s1", "agent_id": "a1", "tool_name": "Bash", "tool_use_id": "toolu_w1",
                "hook_event_name": "PreToolUse", "cwd": str(repo), "tool_input": {"command": "git status --short"}}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(call))), contextlib.redirect_stdout(io.StringIO()):
            hooks.run("pre-tool", "claude")
        entry = pg.call_entry(call)
        self.assertEqual(entry.get("wt_base"), os.path.realpath(repo), entry)
        self.assertIn("notes.md", entry.get("wt_before") or {}, entry)


class TokenStampFailureTests(unittest.TestCase):
    # a failure while the call token is stamped (its pin's ledger note here) costs the call its token, audited,
    # never its lane route: the hook never fails closed on the session
    def setUp(self):
        from test_progress import TempState
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)

    def test_a_ledger_failure_while_the_token_is_stamped_still_routes_the_members_call_to_its_lane(self):
        import contextlib
        import io
        from devkit import audit, hooks, progress as pg
        from test_progress import launch
        pg.start("Export", plan=["api=30m"])
        pg.assign("api", "Tank", why="the api")
        launch("a_tank", "devkit:tank", "Tank: api")
        call = {"session_id": "s1", "agent_id": "a_tank", "agent_type": "devkit:tank", "tool_name": "Bash",
                "tool_use_id": "toolu_stamp1", "permission_mode": "bypassPermissions", "cwd": str(ROOT),
                "hook_event_name": "PreToolUse", "tool_input": {"command": 'dev progress note "working"'}}
        real = pg.call_note

        def note(data, **fields):
            if "pin" in fields:
                raise OSError("No space left on device")
            return real(data, **fields)
        out = io.StringIO()
        with mock.patch.object(pg, "call_note", note), mock.patch("sys.stdin", io.StringIO(json.dumps(call))), \
                contextlib.redirect_stdout(out):
            hooks.run("pre-tool", "claude")
        spec = (json.loads(out.getvalue()) if out.getvalue().strip() else {}).get("hookSpecificOutput") or {}
        command = (spec.get("updatedInput") or {}).get("command", "")
        self.assertEqual(command, 'DEVKIT_FROM_AGENT=a_tank dev progress note "working"')
        skipped = audit.records(1, action="calls.token-skipped")
        self.assertEqual(len(skipped), 1, skipped)
        self.assertIn("No space left on device", json.dumps(skipped[0], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
