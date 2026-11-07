# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The continuity module's call sites (lifecycle spec C12, "Continuity module contract"; plan step "continuity
# wiring"): the hook registrations on both agents, each hook handler calling its entry point at the place the contract
# names, the summarizer's SubagentStop, the capture cut, the scheduled expiry, and devkit_resume on the MCP server,
# which takes the reader's provider from the app that connected, never from the model (compliance review 2, C3).
# The hooks run through hooks.run on the real Claude Code 2.1.319 payloads, with devkit's state in a temporary folder.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_continuity_wiring
import contextlib
import io
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
HOME = TESTS.parent
sys.path.insert(0, str(HOME / "lib"))
sys.path.insert(0, str(TESTS))

from devkit import canary, cli, core, desktop, hooks, mcp, prompt, skillguard, state  # noqa: E402
from devkit import continuity as ct  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_continuity import SID, T0, Base, fixture  # noqa: E402

SUMMARY_WITHOUT_REQUESTS = "<summary>The session works on a release. Nothing else is recorded here.</summary>"


class RegistrationTests(unittest.TestCase):
    def test_both_agents_register_the_compaction_hooks_and_every_session_start_source(self):
        claude = json.loads((HOME / "config" / "claude" / "settings.json").read_text())["hooks"]
        codex = json.loads((HOME / "config" / "codex" / "hooks.json").read_text())["hooks"]
        for hooks_, agent in ((claude, "claude"), (codex, "codex")):
            for event, name in (("PreCompact", "pre-compact"), ("PostCompact", "post-compact")):
                with self.subTest(agent=agent, event=event):
                    (entry,) = hooks_[event]
                    self.assertEqual(entry["hooks"][0]["command"], f"'{{{{DEVKIT}}}}/bin/dev' hook {name} --agent {agent}")
                    if agent == "claude":
                        self.assertEqual(entry["matcher"], "manual|auto")
            self.assertEqual(hooks_["SessionStart"][0]["matcher"], "startup|resume|clear|compact")
        self.assertIs(hooks.HANDLERS["pre-compact"], hooks.pre_compact)
        self.assertIs(hooks.HANDLERS["post-compact"], hooks.post_compact)

    def test_a_capture_cuts_the_compaction_summary(self):
        got = hooks._scrub({"compact_summary": "s" * 5000, "trigger": "manual"}, False)
        self.assertEqual(got["compact_summary"], "s" * hooks.CAPTURE_CUT + "…")
        self.assertEqual(got["trigger"], "manual")


class HookCase(Base):
    # the hooks run through hooks.run; what they would start in the background is stubbed, and the prompt's cache
    # (which the hooks refresh) is in the test's folder
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(hooks, "_spawn", lambda *a, **k: None),
                  mock.patch.object(hooks, "_spawn_sync", lambda *a, **k: None),
                  mock.patch.object(hooks, "context_for", lambda cwd, agent: "## devkit · workspace context"),
                  mock.patch.object(hooks, "catalog_context", lambda agent: ""),
                  mock.patch("devkit.work.request_sync", lambda *a, **k: None),
                  mock.patch.object(prompt, "STATE", self.root / "prompt-state")):
            p.start()
            self.addCleanup(p.stop)

    def hook(self, event, payload) -> str:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            self.assertEqual(hooks.run(event, "claude"), 0)
        return out.getvalue()

    def context(self, out) -> str:
        return json.loads(out)["hookSpecificOutput"].get("additionalContext") or "" if out.strip() else ""

    def stop(self, decide) -> None:
        with mock.patch("devkit.scope.decide", decide), mock.patch.object(canary, "check", lambda *a: None), \
                mock.patch.object(skillguard, "on_stop", lambda *a: None), \
                mock.patch.object(hooks, "_stub_gate", lambda *a: None), \
                mock.patch.object(hooks, "_test_gate", lambda *a: None), \
                mock.patch.object(hooks, "_handoff_nudge", lambda *a: None):
            self.assertEqual(self.hook("stop", self.payload("Stop", prompt_id="p1")), "")


class HookTests(HookCase):
    def test_pre_compact_gives_the_summarizer_devkits_instructions_and_a_member_gets_nothing(self):
        self.make_run()
        out = self.hook("pre-compact", self.payload("PreCompact.manual"))
        self.assertTrue(out.startswith("devkit · keep these in the summary"), out[:80])
        self.assertNotIn("decision", out.split("\n", 1)[0])  # plain text, never a JSON decision that could block
        self.assertEqual(ct.read("claude", SID)["checkpoint"]["reason"], "pre-compact")
        self.assertEqual(self.hook("pre-compact", self.payload("PreCompact.manual", agent_id="a-tank")), "")

    def test_post_compact_prints_nothing_and_the_next_prompt_names_first_what_the_summary_left_out(self):
        self.make_run(lanes=())
        self.prompt("rename the flag in `lib/flags.py`", "d1", T0 - 300)
        self.hook("pre-compact", self.payload("PreCompact.manual"))
        self.assertEqual(self.hook("post-compact", self.payload("PostCompact.manual",
                                                                compact_summary=SUMMARY_WITHOUT_REQUESTS)), "")
        got = self.context(self.hook("prompt", self.payload("UserPromptSubmit", prompt="go on", prompt_id="p2")))
        self.assertTrue(got.startswith("devkit · Not in the summary"), got[:80])
        self.assertIn("«rename the flag in `lib/flags.py`» (missing `lib/flags.py`)", got)

    def test_session_start_after_a_compaction_gives_the_brief_before_the_workspace_context(self):
        self.make_run()
        out = self.hook("session-start", self.payload("SessionStart.compact"))
        brief, _, rest = out.partition("\n\n")
        self.assertTrue(brief.startswith("devkit · this conversation was compacted"), out[:80])
        self.assertIn("do not relaunch them", out)
        self.assertLess(out.index("do not relaunch them"), out.index("## devkit · workspace context"))

    def test_post_tool_gives_the_brief_of_a_compaction_only_the_transcript_shows(self):
        from test_continuity import boundary_line
        self.make_run()
        ct.on_session_start("claude", self.payload("SessionStart.startup"), now=T0 - 60)  # the main offset
        self.append(boundary_line("b-late"))
        got = self.context(self.hook("post-tool", self.payload("PostToolUse.Bash")))
        self.assertTrue(got.startswith("devkit · this conversation was compacted"), got[:80])

    def test_stop_keeps_a_prompt_only_for_development_work_and_drops_it_when_the_classifier_fails(self):
        # compliance review 2, the wiring item: scope.decide raising is "not classed" (dev_turn None), so the prompt
        # goes, although devkit's own gates still run on such a turn
        def boom(*a):
            raise RuntimeError("classifier down")
        for decide, kept in ((lambda *a: True, ["fix the export in `lib/export.py`"]), (lambda *a: False, []),
                             (boom, [])):
            with self.subTest(kept=kept):
                self.hook("prompt", self.payload("UserPromptSubmit", prompt="fix the export in `lib/export.py`",
                                                 prompt_id="p1"))
                self.assertTrue(ct.read("claude", SID)["checkpoint"]["prompts"][-1]["pending"])
                self.stop(decide)
                self.assertEqual(self.texts(), kept)
                ct.forget(session=f"claude:{SID}")

    def test_the_summarizers_subagent_stop_returns_at_once_and_binds_nothing(self):
        # (the hook runner swallows errors, so the calls are counted, not raised)
        calls = []
        with mock.patch.object(pg, "on_subagent", lambda *a, **k: calls.append("on_subagent")), \
                mock.patch.object(pg, "bind", lambda *a, **k: calls.append("bind")):
            self.assertEqual(self.hook("subagent-stop", self.payload("SubagentStop.compact-summarizer")), "")
            self.assertEqual(calls, [])
            self.hook("subagent-stop", self.payload("SubagentStop"))  # a member's stop still binds and closes
        self.assertEqual(calls, ["bind", "on_subagent"])

    def test_session_end_after_clear_links_the_next_session_and_drops_the_old_requests(self):
        self.prompt("rename the flag in `lib/flags.py`", "d1", T0 - 300)
        self.hook("session-end", self.payload("SessionEnd.clear"))
        self.assertEqual(state.read(ct.clear_path("claude", str(self.cwd)))["old_sid"], SID)
        self.assertEqual(self.texts(), [])


class PromptRefreshTests(HookCase):
    # The shell prompt's segment (spec R9, Link's M316): the hooks keep its cache current for the event's folder. A
    # tool call refreshes it at most every 5 s; a prompt, a turn's end and a lane opening or closing refresh it at
    # once, since they change what it shows (an answered decision, the crew count)
    def setUp(self):
        super().setUp()
        (self.cwd / ".git").mkdir()  # HEAD alone: the prompt reads it, and git still sees no repository
        (self.cwd / ".git" / "HEAD").write_text("ref: refs/heads/release/26.11.3\n")
        handoff = self.root / "handoff"
        handoff.mkdir()
        (handoff / "registry.json").write_text(json.dumps(
            {"projects": [{"id": "acme/api", "path": str(self.cwd), "worktrees": []}]}))
        p = mock.patch.object(core, "HANDOFF", handoff)
        p.start()
        self.addCleanup(p.stop)
        prompt.set_mode("zsh")  # the developer turned the helper on (dev setup prompt); off, the hooks write nothing
        self.make_run(lanes=())

    def asks(self) -> str:
        return prompt.show(str(self.cwd), cols=200)[1]

    def decision(self, now: str):
        run = state.read(pg.run_path("claude", SID))
        run["decisions"][0]["state"] = now
        state.replace(pg.run_path("claude", SID), run)

    def test_each_hook_refreshes_the_prompt_and_four_of_them_skip_the_throttle(self):
        self.hook("post-tool", self.payload("PostToolUse.Bash"))
        self.assertEqual(self.asks(), "! 1 question for you")  # a tool call wrote the folder's facts
        self.decision("decided")
        self.hook("post-tool", self.payload("PostToolUse.Bash"))
        self.assertEqual(self.asks(), "! 1 question for you")  # ... and the next one within 5 s does not
        forced = (("prompt", lambda: self.hook("prompt", self.payload("UserPromptSubmit", prompt="go on",
                                                                       prompt_id="p9"))),
                  ("stop", lambda: self.stop(lambda *a: True)),
                  ("subagent-start", lambda: self.hook("subagent-start", self.payload("SubagentStart"))),
                  ("subagent-stop", lambda: self.hook("subagent-stop", self.payload("SubagentStop"))))
        for event, fire in forced:
            with self.subTest(event):
                self.decision("pending")
                fire()
                self.assertEqual(self.asks(), "! 1 question for you")
                self.decision("decided")
                fire()
                self.assertEqual(self.asks(), "")


class ScheduledSyncTests(unittest.TestCase):
    def test_the_scheduled_sync_expires_old_continuity_records(self):
        # the sync's own work is stubbed; what matters is that the scheduled run calls continuity.expire
        called = []
        stubs = [mock.patch.object(ct, "expire", lambda now=None: called.append(True) or 0),
                 mock.patch("devkit.registry.build", lambda **k: {"projects": [], "sessions": []}),
                 mock.patch("devkit.registry.write_outputs", lambda reg: []),
                 mock.patch("devkit.dashboard.write", lambda reg: None),
                 mock.patch("devkit.work.pull", lambda: None), mock.patch("devkit.work.expire_locks", lambda: []),
                 mock.patch("devkit.work.write_presence", lambda s: None),
                 mock.patch("devkit.providers.status", lambda **k: {}), mock.patch("devkit.policy.diff", lambda: []),
                 mock.patch("devkit.sleep.due", lambda: False), mock.patch("devkit.canary.prune", lambda: None),
                 mock.patch.object(cli, "commit_push", lambda **k: None),
                 mock.patch.object(cli, "_expire_sessions", lambda: None),
                 mock.patch.object(cli, "_rotate_log", lambda f: None)]
        import tempfile
        with tempfile.TemporaryDirectory() as d, contextlib.ExitStack() as stack:
            for s in stubs:
                stack.enter_context(s)
            stack.enter_context(mock.patch.object(cli, "STATE", Path(d)))
            stack.enter_context(mock.patch.dict(core.CFG, {"gc": {"auto": False}}))
            a = cli.build_parser().parse_args(["sync", "--scheduled", "--quiet", "--offline"])
            a.fn(a)
        self.assertEqual(called, [True])


class ResumeToolTests(unittest.TestCase):
    def serve(self, client, args):
        # one MCP conversation: initialize with this clientInfo, then devkit_resume; -> the dev argv it ran
        ran = []
        lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "clientInfo": client}},
                 {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "devkit_resume",
                                                                                "arguments": args}}]
        rfile = io.BytesIO(b"".join(json.dumps(x).encode() + b"\n" for x in lines))
        wfile = io.BytesIO()
        with mock.patch.object(mcp, "run_dev", lambda argv, *a, **k: (ran.append(argv), (0, "brief", ""))[1]):
            mcp.Server(rfile, wfile).serve()
        replies = [json.loads(x) for x in wfile.getvalue().splitlines()]
        return ran, replies[-1]["result"]

    def test_devkit_resume_reads_the_latest_brief_for_the_provider_of_the_app_that_connected(self):
        tool = next(t for t in mcp.TOOLS if t["name"] == "devkit_resume")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"project"})
        cases = (({"name": "claude-ai", "version": "1"}, ["--provider=claude"]),
                 ({"name": "ChatGPT", "version": "1"}, ["--provider=codex"]),
                 ({"name": "some-other-app", "version": "1"}, []))
        for client, provider in cases:
            with self.subTest(client=client["name"]):
                ran, _ = self.serve(client, {"project": "acme/api"})
                self.assertEqual(ran, [["session", "brief", "--latest", "--for-chat", "--project=acme/api"] + provider])
        ran, result = self.serve({"name": "some-other-app"}, {"project": "acme/api", "provider": "claude"})
        self.assertEqual(ran, [])  # the model cannot name the provider
        self.assertTrue(result["isError"])

    def test_the_instructions_send_a_resumed_chat_to_devkit_resume_and_the_extension_lists_it(self):
        self.assertIn("After a conversation summary or a resume, call devkit_resume", mcp.INSTRUCTIONS)
        self.assertEqual(desktop.check_tools(desktop.source_manifest()), [])


if __name__ == "__main__":
    unittest.main()
