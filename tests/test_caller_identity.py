# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Caller identity (F4 of the agent and session lifecycle spec, decision D3, contract C4, security consultation §1):
# who runs a `dev` command comes from a call ledger that the PreToolUse hook writes from the harness payload, never
# from a variable the command can set, empty or unset. A `dev` command finds its session by process ancestry, then
# the open calls of that session whose text holds its own arguments: main only when every match is main's, the member
# when any match is a member's, unknown with no match. Morpheus-only commands need main.
# Run: python3 -m unittest tests.test_caller_identity
import contextlib
import io
import json
import os
import stat
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import audit, cli, hooks, progress as pg  # noqa: E402
from test_progress import TempState, launch  # noqa: E402

SETTINGS = Path(__file__).resolve().parents[1] / "config" / "claude" / "settings.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hooks" / "claude"


class CallerCase(unittest.TestCase):
    def setUp(self):
        self.state = TempState(real_caller=True).__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name)
        for p in (mock.patch.object(hooks, "STATE", self.root),
                  mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:s1"})):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("DEVKIT_FROM_AGENT", None)
        self.n = 0

    # the PreToolUse ledger entry of a shell call: Morpheus's (no agent_id) or a member's
    def call(self, cmd, aid=None, sid="s1", agent="claude"):
        self.n += 1
        data = {"session_id": sid, "tool_use_id": f"toolu_{self.n:04d}", "tool_name": "Bash",
                "tool_input": {"command": cmd}, "cwd": str(self.root)}
        if aid:
            data["agent_id"] = aid
        pg.call_opened(agent, data)
        return data

    # a `dev` command as the shell runs it: its own argv, and DEVKIT_FROM_AGENT only when `env` sets it
    def dev(self, *args, env=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["dev", *args]), mock.patch.dict(os.environ, env or {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(args))
        return rc, out.getvalue() + err.getvalue()

    def run_(self, sid="s1", agent="claude"):
        return pg.load_run(agent, sid)

    def decision(self, did="D1"):
        return next(d for d in self.run_()["decisions"] if d["id"] == did)

    def plan_with_tank(self):
        # a run with a decision waiting for the developer, and Tank working in a lane of it
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.start("Work", plan=["parser", "docs"])
            pg.assign("parser", "Tank", why="the test's assignment")
            pg.crew_decision("Ship the parser now?", options="yes|no", recommend="yes")
        launch("a_tank", "general-purpose", "Tank: parser")


class ConsultationTests(CallerCase):
    def test_member_cannot_decide_through_a_nested_shell(self):
        # security consultation §1, every probe form; the guard refuses these too, the ledger is what holds when
        # a form gets past it
        self.plan_with_tank()
        for form in ("bash -c 'dev crew decide D1 yes'", "sh -c 'dev crew decide D1 yes'",
                     "printf 'dev crew decide D1 yes' | sh", "eval 'dev crew decide D1 yes'",
                     "python3 -c \"import subprocess; subprocess.run('dev crew decide D1 yes', shell=True)\""):
            with self.subTest(form):
                data = self.call(form, aid="a_tank")
                rc, out = self.dev("crew", "decide", "D1", "yes")
                pg.call_closed(data)
                self.assertEqual(rc, 2)
                self.assertIn("only the tech lead", out)
                self.assertEqual(self.decision()["state"], "pending")

    def test_empty_lane_variable_grants_nothing(self):
        self.plan_with_tank()
        self.call("DEVKIT_FROM_AGENT= dev crew decide D1 yes", aid="a_tank")
        rc, out = self.dev("crew", "decide", "D1", "yes", env={"DEVKIT_FROM_AGENT": ""})
        self.assertEqual(rc, 2)
        self.assertIn("only the tech lead", out)
        self.assertEqual(self.decision()["state"], "pending")

    def test_detached_command_is_not_morpheus(self):
        # nohup, setsid, launchctl or at: the command runs after the call that started it has ended
        self.plan_with_tank()
        rc, out = self.dev("crew", "decide", "D1", "yes")
        self.assertEqual(rc, 2)
        self.assertIn("type the command itself, not through a script or another shell", out)
        self.assertEqual(self.decision()["state"], "pending")
        self.assertEqual([r["target"] for r in audit.records(1, action="caller.refused")],
                         ["records the developer's answers"])

    def test_same_text_from_member_and_main_is_refused(self):
        self.plan_with_tank()
        self.call("dev crew decide D1 yes")
        self.call("dev crew decide D1 yes", aid="a_tank")
        rc, out = self.dev("crew", "decide", "D1", "yes")
        self.assertEqual(rc, 2)
        self.assertIn("only the tech lead", out)
        self.assertEqual(self.decision()["state"], "pending")

    def test_morpheus_plain_command_is_allowed(self):
        self.plan_with_tank()
        self.call("dev crew decide D1 yes")
        rc, out = self.dev("crew", "decide", "D1", "yes")
        self.assertEqual(rc, 0, out)
        # Morpheus may record it; without the developer's own answer (spec C5) it is his, unverified
        self.assertEqual((self.decision()["state"], self.decision()["answer"], self.decision()["by"],
                          self.decision()["verified"]), ("decided", "yes", "morpheus", False))


class MorpheusOnlyTests(CallerCase):
    def test_every_morpheus_only_command_needs_main_in_the_ledger(self):
        self.plan_with_tank()
        before = [(s["name"], s["state"], s.get("owner")) for s in self.run_()["plan"]]
        for args in (("progress", "assign", "docs", "--to", "Tank"), ("progress", "begin", "docs"),
                     ("progress", "skip", "docs", "--why", "not needed"), ("progress", "reopen", "docs", "--why", "x"),
                     ("crew", "mode", "auto"), ("progress", "start", "Other work", "--replace", "--why", "new goal"),
                     ("crew", "route", "--plan", "--apply")):
            with self.subTest(" ".join(args)):
                data = self.call("bash -c 'dev " + " ".join(args) + "'", aid="a_tank")
                rc, out = self.dev(*args)
                pg.call_closed(data)
                self.assertEqual(rc, 2, out)
                self.assertIn("only the tech lead", out)
        run = self.run_()
        self.assertEqual((run["goal"], run["mode"], run["state"]), ("Work", "guided", "running"))
        self.assertEqual([(s["name"], s["state"], s.get("owner")) for s in run["plan"]], before)

    def test_only_a_call_of_morpheus_ends_the_run(self):
        self.plan_with_tank()
        self.call("bash -c 'dev progress done finished --why ok'", aid="a_tank")  # a member: its own lane only
        self.assertEqual(self.dev("progress", "done", "finished", "--why", "ok")[0], 0)
        self.assertEqual(self.run_()["state"], "running")
        rc, out = self.dev("progress", "done", "finished", "--why", "ok", "--force")  # no open call at all
        self.assertEqual((rc, self.run_()["state"]), (2, "running"))
        self.assertIn("only the tech lead", out)

    def test_a_member_match_makes_a_lane_command_act_as_that_member(self):
        # variables only narrow: without DEVKIT_FROM_AGENT, the ledger still names the member
        self.plan_with_tank()
        self.call("cd lib && dev progress step 'parser done' --check '12 tests pass'", aid="a_tank")
        rc, out = self.dev("progress", "step", "parser done", "--check", "12 tests pass")
        self.assertEqual(rc, 0, out)
        run = self.run_()
        self.assertEqual([u["text"] for u in run["agents"]["a_tank"]["updates"]], ["parser done"])
        self.assertEqual(next(s for s in run["plan"] if s["name"] == "parser")["state"], "running")

    def test_a_delegation_through_a_member_call_is_the_members(self):
        self.plan_with_tank()
        self.call("bash -c 'dev crew delegate \"review the parser\" --to Seraph'", aid="a_tank")
        rc, out = self.dev("crew", "delegate", "review the parser", "--to", "Seraph")
        self.assertEqual(rc, 0, out)
        step = next(s for s in self.run_()["plan"] if s.get("task") == "review the parser")
        self.assertEqual(step["requested_by"], "a_tank")

    def test_a_codex_session_runs_morpheus_commands_unverified_and_audits_them(self):
        # until Codex passes agent_id to a spawned agent's hooks (S3), no Codex caller is verified
        (pg.SESSIONS / "codex-t1.json").write_text(json.dumps({"agent": "codex", "session": "t1",
                                                                "started": time.time()}))
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": "codex:t1"}):
            pg.start("Codex work", plan=["a"])
            rc, out = self.dev("crew", "mode", "auto")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.run_("t1", "codex")["mode"], "auto")
        self.assertEqual({r["target"] for r in audit.records(1, action="caller.unverified")},
                         {"starts a run", "sets the mode"})


class RunTests(CallerCase):
    def test_a_second_start_is_refused(self):
        self.call("dev progress start Work --plan 'a; b'")
        self.assertEqual(self.dev("progress", "start", "Work", "--plan", "a; b")[0], 0)
        self.call("dev progress start Other --plan c")
        rc, out = self.dev("progress", "start", "Other", "--plan", "c")
        self.assertEqual(rc, 2)
        self.assertIn("--replace --why", out)
        self.assertEqual(self.run_()["goal"], "Work")

    def test_start_replace_with_a_reason_replaces_the_live_run_and_keeps_why(self):
        self.call("dev progress start Work --plan a")
        self.dev("progress", "start", "Work", "--plan", "a")
        self.call("dev progress start Other --plan c --replace --why 'the goal changed'")
        rc, out = self.dev("progress", "start", "Other", "--plan", "c", "--replace", "--why", "the goal changed")
        self.assertEqual(rc, 0, out)
        run = self.run_()
        self.assertEqual((run["goal"], run["replaced"]["goal"], run["replaced"]["why"]),
                         ("Other", "Work", "the goal changed"))

    def test_a_member_step_with_no_run_errors(self):
        # it started a run of its own, which made the member's step Morpheus's; the caller here would be allowed to
        # start one, so only the missing run refuses it
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}), \
                self.assertRaisesRegex(pg.StepError, "no progress run"):
            pg.step("parser done", check="ok", from_agent="a_tank")
        self.assertIsNone(self.run_())

    def test_a_session_found_only_by_recency_is_not_morpheus(self):
        # an agent's command (its marker) with no parent process and no DEVKIT_SESSION: the session is a guess, so a
        # call of main's does not vouch
        top = str(self.root)
        (pg.SESSIONS / "claude-s1.json").write_text(json.dumps({"agent": "claude", "session": "s1", "top": top,
                                                                "started": time.time()}))
        self.plan_with_tank()
        self.call("dev crew decide D1 yes")
        os.environ.pop("DEVKIT_SESSION")
        with mock.patch.object(pg.core, "gitout", lambda *a, **k: top), \
                mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):  # this shell's repository is the session's
            self.assertEqual(pg.resolve_session()[2], "recency")
            rc, out = self.dev("crew", "decide", "D1", "yes")
        self.assertEqual(rc, 2)
        self.assertIn("only the tech lead", out)
        self.assertEqual(self.decision()["state"], "pending")

    def test_the_developers_terminal_never_acts_on_the_newest_sessions_run(self):
        # the same command typed in the developer's terminal (a shell for a parent, no agent's marker) beside that
        # session: it acts for the developer's own shell, never on the session's run
        top = str(self.root)
        (pg.SESSIONS / "claude-s1.json").write_text(json.dumps({"agent": "claude", "session": "s1", "top": top,
                                                                "started": time.time()}))
        self.plan_with_tank()
        os.environ.pop("DEVKIT_SESSION")
        with mock.patch.object(pg.core, "gitout", lambda *a, **k: top), \
                mock.patch.dict(os.environ, {k: "" for k in pg.core.AGENT_MARKERS}), \
                mock.patch.object(pg, "_ancestor_pids", lambda: [424242, 1]):
            self.assertEqual(pg.resolve_session(), ("human", f"shell-{os.getppid()}", "shell"))
            rc, out = self.dev("crew", "decide", "D1", "yes")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.decision()["state"], "pending")

    def test_two_sessions_in_one_repository_each_update_their_own_run(self):
        # --latest (recency) picked the newest session of the repository: s2, for s1's commands too
        top = str(self.root / "repo")
        for sid, pid, age in (("s1", 1111, 600), ("s2", 2222, 5)):
            (pg.SESSIONS / f"claude-{sid}.json").write_text(json.dumps({
                "agent": "claude", "session": sid, "top": top, "cwd": top, "pid": pid,
                "started": time.time() - age}))
        os.environ.pop("DEVKIT_SESSION")
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            for sid, pid in (("s1", 1111), ("s2", 2222)):
                with mock.patch.object(pg, "_ancestor_pids", lambda pid=pid: [9000 + pid, pid, 1]):
                    pg.start(f"Work of {sid}", plan=["a"], cwd=top)
                    pg.note(f"progress of {sid}", cwd=top)
        self.assertEqual([u["text"] for u in self.run_("s1")["updates"]], ["progress of s1"])
        self.assertEqual([u["text"] for u in self.run_("s2")["updates"]], ["progress of s2"])

    def test_the_session_record_holds_its_agent_process_and_ancestry_finds_it(self):
        # SessionStart runs in a hook process below the agent; the agent's own process is what both share
        table = {os.getpid(): (700, "python3 bin/dev hook session-start --agent claude"),
                 700: (600, "/bin/sh -c bin/dev hook session-start"), 600: (1, "claude --resume"),
                 1: (0, "/sbin/launchd")}
        payload = {"session_id": "s9", "cwd": str(self.root), "source": "startup"}
        with mock.patch.object(pg, "_ps", lambda: table), mock.patch.object(hooks, "_spawn", lambda *a, **k: None), \
                mock.patch.object(hooks, "context_for", lambda *a, **k: ""), \
                mock.patch("devkit.work.request_sync", lambda *a, **k: None), \
                mock.patch.dict(os.environ, {"CLAUDE_ENV_FILE": ""}), \
                mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(io.StringIO()):
            hooks.session_start("claude")
        self.assertEqual(json.loads((pg.SESSIONS / "claude-s9.json").read_text())["pid"], 600)
        os.environ.pop("DEVKIT_SESSION")
        with mock.patch.object(pg, "_ancestor_pids", lambda: [4321, 600, 1]):
            self.assertEqual(pg.resolve_session(), ("claude", "s9", "ancestry"))


class LedgerTests(CallerCase):
    def hook(self, name, payload):
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: None):
            hooks.run(name, "claude")
        return out.getvalue()

    def payload(self, tid, cmd, **over):
        d = json.loads((FIXTURES / "PreToolUse.Bash.json").read_text())
        d.update(session_id="s1", tool_use_id=tid, cwd=str(self.root), **over)
        d["tool_input"] = dict(d.get("tool_input") or {}, command=cmd)
        return d

    def test_the_guard_hook_opens_a_call_and_its_result_or_failure_closes_it(self):
        self.hook("guard", self.payload("toolu_a", "dev crew decide D1 yes"))
        self.hook("guard", self.payload("toolu_b", "make test"))
        self.assertEqual({c["tool_use_id"]: c["caller"] for c in pg.open_calls("s1")},
                         {"toolu_a": "main", "toolu_b": "main"})
        self.hook("post-tool", dict(self.payload("toolu_a", "dev crew decide D1 yes"), hook_event_name="PostToolUse"))
        self.hook("post-tool-failure", dict(self.payload("toolu_b", "make test"), hook_event_name="PostToolUseFailure",
                                            error="Exit code 2"))
        self.assertEqual(pg.open_calls("s1"), [])

    def test_a_refused_call_leaves_no_open_entry(self):
        with mock.patch.object(hooks, "_crew_limits", lambda *a, **k: ("deny", "refused for the test")):
            out = self.hook("guard", self.payload("toolu_c", "dev crew decide D1 yes"))
        self.assertIn('"deny"', out)
        self.assertEqual(pg.open_calls("s1"), [])

    def test_a_member_call_records_its_agent_id(self):
        self.hook("guard", self.payload("toolu_d", "dev progress note x", agent_id="a_tank"))
        self.assertEqual([c["caller"] for c in pg.open_calls("s1")], ["a_tank"])

    def test_a_call_left_open_expires_after_its_timeout_and_is_audited(self):
        t0 = time.time()
        data = self.payload("toolu_e", "sleep 900")
        data["tool_input"]["timeout"] = 120000  # the Bash tool's timeout, in milliseconds
        pg.call_opened("claude", data, now=t0)
        self.assertEqual(len(pg.open_calls("s1", now=t0 + 119)), 1)
        self.assertEqual(pg.open_calls("s1", now=t0 + 121), [])
        # sleep is no program the audit masker knows: its name stays and its arguments keep their length only (26.11.5)
        self.assertEqual([r["detail"] for r in audit.records(1, action="calls.unclosed")],
                         ["sleep [text not recorded: 3 characters] (no result within 120 s)"])

    def test_an_entry_keeps_the_command_without_secrets_private_to_its_owner(self):
        pg.call_opened("claude", self.payload("toolu_f", "curl -u x https://h.example password=hunter2-not-real"))
        entry = pg.open_calls("s1")[0]
        self.assertNotIn("hunter2", entry["cmd"])
        f = next((pg.CALLS / "s1").iterdir())
        self.assertEqual((stat.S_IMODE(f.stat().st_mode), stat.S_IMODE(f.parent.stat().st_mode)), (0o600, 0o700))

    def test_the_settings_send_a_failed_shell_call_to_devkit(self):
        import re
        entries = json.loads(SETTINGS.read_text())["hooks"].get("PostToolUseFailure") or []
        self.assertTrue(any(re.fullmatch(e.get("matcher") or ".*", "Bash") for e in entries))


class IdentityChangeTests(CallerCase):
    def guard(self, cmd, aid="a_tank"):
        data = {"session_id": "s1", "tool_use_id": "toolu_x", "tool_name": "Bash", "tool_input": {"command": cmd},
                "cwd": str(self.root)}
        if aid:
            data["agent_id"] = aid
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out):
            hooks.run("guard", "claude")
        return out.getvalue()

    def test_a_member_cannot_change_who_it_is(self):
        # consultation §1 point 5: assigning, exporting or unsetting a DEVKIT_* variable, env -i, env -u DEVKIT_*
        for cmd in ("unset DEVKIT_FROM_AGENT; dev crew decide D1 yes", "env -u DEVKIT_FROM_AGENT dev crew ask x",
                    "env -i PATH=/usr/bin dev crew decide D1 yes", "export DEVKIT_SESSION=claude:other",
                    "DEVKIT_SESSION=claude:other bash tools/run.sh",
                    "bash -c 'unset DEVKIT_SESSION; dev progress step x'",
                    "python3 -c \"import os; os.environ.pop('DEVKIT_FROM_AGENT')\""):
            with self.subTest(cmd):
                out = self.guard(cmd)
                self.assertIn('"deny"', out)
                # a command that also hides the session's markers (DEVKIT_SESSION, env -i) may be refused first by the
                # rule on hiding an agent; either refusal keeps the member from changing who it is
                self.assertTrue("who calls devkit" in out or "an agent runs it" in out, out)

    def test_hiding_the_agent_markers_is_refused_for_the_main_session_too(self):
        for cmd in ("unset CLAUDECODE; npm run release", "env -u CLAUDECODE node tool.js", "CLAUDECODE= dev status"):
            with self.subTest(cmd):
                out = self.guard(cmd, aid=None)
                self.assertIn('"deny"', out)
                self.assertIn("CLAUDECODE", out)

    def test_a_command_that_only_mentions_an_identity_variable_passes(self):
        for cmd in ("git commit -m 'explain DEVKIT_FROM_AGENT= in the docs'", "grep -n DEVKIT_FROM_AGENT lib/x.py",
                    "cat > notes.txt <<'EOF'\nunset DEVKIT_FROM_AGENT\nEOF"):
            with self.subTest(cmd):
                self.assertNotIn('"deny"', self.guard(cmd))

    def test_the_main_session_may_change_its_own_variables(self):
        self.assertNotIn('"deny"', self.guard("unset DEVKIT_FROM_AGENT", aid=None))


class CallerShapeTests(CallerCase):
    # progress.caller and progress._as_lane are wrappers over the identity module (lib/devkit/identity.py). caller
    # keeps the dict its readers take apart (_main_only, the continuity brief, work's lock owner): the same keys, and
    # the same values but one, which narrows: a call the hook marked as a member's lane names that member even when
    # the ledger lost its call. Neither the tech lead's stamp nor "no crew member works" ever makes a caller main
    # there: only an open call of main's does, in a session not guessed by recency.
    def shape(self, args=("crew", "decide", "D1", "yes"), env=None):
        with mock.patch.dict(os.environ, env or {}):
            got = pg.caller(str(self.root), argv=list(args))
        self.assertEqual(set(got), {"agent", "session", "how", "caller", "calls"})
        return got["agent"], got["session"], got["how"], got["caller"], len(got["calls"])

    def test_caller_shape_unchanged(self):
        def row(name, want, calls=(), **kw):
            opened = [self.call(cmd, aid=aid, sid=sid, agent=agent) for cmd, aid, sid, agent in calls]
            with self.subTest(name):
                self.assertEqual(self.shape(**kw), want)
            for data in opened:
                pg.call_closed(data)
        decide = "dev crew decide D1 yes"
        row("the developer's shell", ("human", f"shell-{os.getppid()}", "shell", "human", 0),
            env={"DEVKIT_SESSION": "", "DEVKIT_AGENT": ""})
        row("an open call of main's", ("claude", "s1", "env", "main", 1), calls=[(decide, None, "s1", "claude")])
        row("main's and a member's", ("claude", "s1", "env", "a_tank", 2),
            calls=[(decide, None, "s1", "claude"), (decide, "a_tank", "s1", "claude")])
        row("no open call typed it", ("claude", "s1", "env", "unknown", 0), calls=[("make test", None, "s1", "claude")])
        row("no crew works and nothing typed it", ("claude", "s1", "env", "unknown", 0))
        row("the tech lead's stamp alone", ("claude", "s1", "env", "unknown", 0), env={"DEVKIT_SPEAKER": "main"})
        row("the tech lead's stamp and main's call", ("claude", "s1", "env", "main", 1),
            calls=[(decide, None, "s1", "claude")], env={"DEVKIT_SPEAKER": "main"})
        row("a member's lane mark, its call gone", ("claude", "s1", "env", "a_tank", 0),
            env={"DEVKIT_FROM_AGENT": "a_tank"})
        row("a member's lane mark and main's call", ("claude", "s1", "env", "a_tank", 1),
            calls=[(decide, None, "s1", "claude")], env={"DEVKIT_FROM_AGENT": "a_tank"})
        (pg.SESSIONS / "codex-t1.json").write_text(json.dumps({"agent": "codex", "session": "t1",
                                                                "started": time.time()}))
        row("a Codex session", ("codex", "t1", "env", "unverified", 0),
            calls=[(decide, "a_tank", "t1", "codex"), (decide, None, "t1", "codex")],
            env={"DEVKIT_SESSION": "codex:t1"})
        top = str(self.root)
        (pg.SESSIONS / "claude-s1.json").write_text(json.dumps({"agent": "claude", "session": "s1", "top": top,
                                                                "started": time.time()}))
        unmarked = {k: "" for k in pg.core.AGENT_MARKERS}
        with mock.patch.object(pg.core, "gitout", lambda *a, **k: top):
            row("a session guessed by recency", ("claude", "s1", "recency", "unknown", 1),
                calls=[(decide, None, "s1", "claude")], env={"DEVKIT_SESSION": "", "CLAUDECODE": "1"})
            row("a member's call in a session guessed by recency", ("claude", "s1", "recency", "a_tank", 1),
                calls=[(decide, "a_tank", "s1", "claude")], env={"DEVKIT_SESSION": "", "CLAUDECODE": "1"})
            with mock.patch.object(pg, "_ancestor_pids", lambda: [424242, 1]):  # a shell, no session's process
                row("the developer's terminal beside that session",
                    ("human", f"shell-{os.getppid()}", "shell", "human", 0), env=unmarked)

    def test_a_lane_command_acts_as_the_member_its_call_or_its_mark_names(self):
        # Codex 0.167 passes agent_id to a spawned agent's hooks, so its ledger entries name the member too: a
        # member's `dev progress step` acts in its own lane there as well, never in the main plan
        def lane(env=None):
            with mock.patch.object(sys, "argv", ["dev", "progress", "step", "parser done"]), \
                    mock.patch.dict(os.environ, env or {}):
                return pg._as_lane(None, str(self.root))
        self.assertIsNone(lane())
        main = self.call("dev progress step 'parser done'")
        self.assertIsNone(lane())
        pg.call_closed(main)
        self.call("cd lib && dev progress step 'parser done'", aid="a_tank")
        self.assertEqual(lane(), "a_tank")
        self.assertEqual(lane({"DEVKIT_FROM_AGENT": "a_mouse"}), "a_mouse")
        self.assertEqual(pg._as_lane("a_ghost", str(self.root)), "a_ghost")
        (pg.SESSIONS / "codex-t1.json").write_text(json.dumps({"agent": "codex", "session": "t1",
                                                                "started": time.time()}))
        self.call("dev progress step 'parser done'", aid="01a1d000-64c7-7ac1-81c6-ec8b852e1772", sid="t1",
                  agent="codex")
        self.assertEqual(lane({"DEVKIT_SESSION": "codex:t1"}), "01a1d000-64c7-7ac1-81c6-ec8b852e1772")


class CodexThreadSessionTests(CallerCase):
    # The Codex app runs several threads in one process: their records hold the same pid, and each thread's ledger
    # may hold an open call that typed the same command (two foreground waits at once). The pid cannot tell them
    # apart: CODEX_THREAD_ID, which Codex gives every command it runs, names the thread; without it, the thread whose
    # repository the command runs in does.
    def setUp(self):
        super().setUp()
        self.repos = {}
        for sid, repo, age in (("x1", "api", 200), ("x2", "web", 100)):
            top = self.root / repo
            top.mkdir()
            self.repos[sid] = str(top)
            (pg.SESSIONS / f"codex-{sid}.json").write_text(json.dumps({
                "agent": "codex", "session": sid, "pid": 4242, "started": time.time() - age, "top": str(top),
                "cwd": str(top)}))
            self.call("dev watch --wait --timeout 10", sid=sid, agent="codex")
        for p in (mock.patch.object(pg, "_ancestor_pids", lambda: [9999, 4242, 1]),
                  mock.patch.dict(os.environ, {"DEVKIT_SESSION": "", "CODEX_THREAD_ID": ""})):
            p.start()
            self.addCleanup(p.stop)

    def resolve(self, sid_of_cwd, thread=""):
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread}):
            return pg.resolve_session(self.repos[sid_of_cwd], ["watch", "--wait", "--timeout", "10"])

    def test_the_thread_id_names_the_thread_among_one_process(self):
        self.assertEqual(self.resolve("x1", thread="x1"), ("codex", "x1", "ancestry"))
        self.assertEqual(self.resolve("x1", thread="x2"), ("codex", "x2", "ancestry"))  # a thread may cd elsewhere

    def test_without_a_thread_id_the_threads_repository_decides(self):
        self.assertEqual(self.resolve("x1"), ("codex", "x1", "ancestry"))
        self.assertEqual(self.resolve("x2"), ("codex", "x2", "ancestry"))

    def test_a_thread_id_of_no_session_here_narrows_nothing(self):
        # a spawned agent's own thread id, or one this process does not run: the ledger and the repository decide
        self.assertEqual(self.resolve("x1", thread="01a1d000-64c7-7ac1-81c6-ec8b852e1772"), ("codex", "x1", "ancestry"))


class RewriteTests(CallerCase):
    def test_a_member_lane_command_is_rewritten_without_approving_it(self):
        # S2: updatedInput applies without a permission decision, and the permission check runs on the result
        data = {"session_id": "s1", "tool_use_id": "toolu_r", "tool_name": "Bash", "agent_id": "a_tank",
                "tool_input": {"command": "dev progress note x"}, "cwd": str(self.root)}
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(data))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: None):
            hooks.run("guard", "claude")
        spec = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertEqual(spec["updatedInput"]["command"], "DEVKIT_FROM_AGENT=a_tank dev progress note x")
        self.assertNotIn("permissionDecision", spec)


if __name__ == "__main__":
    unittest.main()
