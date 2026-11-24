# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Who calls, by the call token: the PreToolUse hook gives every shell call a token (DEVKIT_CALL=<tool_use_id>.<nonce>)
# whose hash its call ledger entry holds, and a `dev` command that carries one is the entry's caller when the token is
# this session's, its agent's and its call shell's, and the call is still open. A session the hook stamped never lets a
# tokenless state change fall back to text matching. Each test is one path of the call-token design and its threat
# review. The hook runs in-process with the progress state and the audit trail in a temporary folder; the process tree
# a `dev` command sees (its shells and agent) is a fixture.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_identity_token -v
import ast
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, hooks, identity, state  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_caller_identity import CallerCase  # noqa: E402
from test_progress import launch  # noqa: E402

AGENT, SHELL, SIBLING, NESTED, INNER = 70001, 70002, 70003, 70004, 70005
TOKEN = re.compile(r"DEVKIT_CALL=([A-Za-z0-9_.-]+)")
LANE = re.compile(r"DEVKIT_FROM_AGENT=([A-Za-z0-9_.:-]+)")


class TokenCase(CallerCase):
    agent = "claude"
    agent_line = "claude"

    agent_pid = AGENT

    def setUp(self):
        super().setUp()
        identity._forget()
        self.addCleanup(identity._forget)
        # Each shell call runs in a shell of its own right below the agent process (the shells map); a stamped one
        # writes its pid to the call's pin as its first action, as the stamp makes it. A dev command runs below the
        # shell of the call whose token it carries, unless a test sets `chain` to the parents it runs below instead.
        # Every process started after the call opened (starts), in one process session (own_session for this one).
        self.chain = None
        self.shells, self.lines = {}, {SHELL: "/bin/zsh -c source snapshot && eval 'pytest'", SIBLING: "zsh"}
        self.own_session = None
        self.own_pid, self.starts, self.parent_of = 71999, {}, {}
        for p in (mock.patch.object(pg, "_call_agent_pid", lambda: self.agent_pid),
                  mock.patch.object(hooks, "_hook_launched_by", lambda pid: pid == self.agent_pid, create=True),
                  mock.patch.object(identity, "_chain", self.parents),
                  mock.patch.object(identity, "_pid", lambda: self.own_pid, create=True),
                  mock.patch.object(identity, "_proc", self.proc, create=True),
                  mock.patch.object(identity, "pin_dir", lambda: self.root / "pins", create=True),
                  mock.patch.object(identity, "_session_of", lambda pid: (self.own_session or 1) if pid == 0 else 1)):
            p.start()
            self.addCleanup(p.stop)
        (hooks.STATE / "logs").mkdir(parents=True, exist_ok=True)

    def proc(self, pid):
        # the kernel's facts on a process: its parent (the agent, for a call shell) and its start time (after its call
        # opened, unless a test says otherwise); None once it exited
        if pid in self.starts and self.starts[pid] is None:
            return None
        return self.parent_of.get(pid, self.agent_pid), self.starts.setdefault(pid, time.time())

    def parents(self) -> list:
        if self.chain is not None:
            return list(self.chain)
        shell = self.shells.get(os.environ.get(identity.CALL_VAR, ""), SHELL)
        return [(shell, self.lines.get(shell, "")), (self.agent_pid, self.agent_line)]

    def shell_line(self, new: str) -> str:
        # the call's shell as ps shows it: Claude Code runs the command through zsh -c and eval
        return f"/bin/zsh -c source /tmp/snapshot.sh 2>/dev/null || true && eval '{new}' < /dev/null"

    # who a dev command with this token is, in this process (no fresh start)
    def speak(self, token, *args):
        with mock.patch.dict(os.environ, {identity.CALL_VAR: token}):
            return identity.speaker(argv=list(args) or ["crew", "inbox"])

    # a command only the tech lead's plan takes: it adds a step to the main plan
    def lead(self, token="", env=None):
        return self.dev("progress", "add", "extra", token=token, env=env)

    def added(self):
        return "extra" in [s["name"] for s in self.run_()["plan"]]

    # the PreToolUse hook on a shell call, in-process -> (payload, its token or "", the command as rewritten)
    def hook(self, cmd, aid=None, sid="s1", tool="Bash", key="command", permission_mode="bypassPermissions", **inp):
        self.n += 1
        data = {"session_id": sid, "tool_use_id": f"toolu_{self.n:04d}", "tool_name": tool,
                "hook_event_name": "PreToolUse", "tool_input": {key: cmd, **inp}, "cwd": str(self.root),
                "permission_mode": permission_mode}
        if aid:
            data["agent_id"] = aid
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks._pre_tool(self.agent, data)
        spec = (json.loads(out.getvalue()) if out.getvalue().strip() else {}).get("hookSpecificOutput") or {}
        new = (spec.get("updatedInput") or {}).get(key) or ""
        found = TOKEN.search(new)
        self.last = spec
        shell = 72000 + self.n  # the shell this call runs in, with what the hook wrote, or the command as typed
        self.lines[shell] = self.shell_line(new or cmd)
        self.shells[found.group(1) if found else f"none-{self.n}"] = shell
        self.shell = shell
        # the shell starts when its call opens, before anything it runs writes a pin (the stamp's printf, or the plain
        # form's dev command); a start taken at the first look at the process could fall after that write
        self.starts.setdefault(shell, time.time())
        if found and "set -C; printf" in new:  # the stamp's first action: the shell pins its pid (noclobber)
            pin = identity.pin_path(sid, data["tool_use_id"])
            if os.path.lexists(pin):  # a pin already there: the stamp moves it onto the contested name
                os.replace(pin, pin.with_name(pin.name + ".contested"))
            else:
                pin.write_text(f"{shell}\n")
        return data, (found.group(1) if found else ""), new

    def closed(self, data, event="PostToolUse"):
        hooks._close_call(dict(data, hook_event_name=event))

    def dev(self, *args, token="", env=None, fresh=True):
        e = dict(env or {})
        if token:
            e[identity.CALL_VAR] = token
        if fresh:
            identity._forget()  # each dev command is a process of its own
        return super().dev(*args, env=e)

    def plan_with_link(self):
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}):
            pg.start("Release", plan=["copy-release", "ci-redirect"])
            pg.assign("copy-release", "Link", why="the test's assignment")
        launch("a_link", "devkit:link", "Link: copy-release fork release")

    def state_of(self, name):
        return next(s for s in self.run_()["plan"] if s["name"] == name).get("state")

    def records(self, action):
        return audit.records(1, action=action)


class StampTests(TokenCase):
    def test_every_main_shell_call_gets_a_readonly_token_whose_hash_its_entry_holds(self):
        data, token, new = self.hook("git status")
        pin = identity.pin_path("s1", data["tool_use_id"])
        p, c = shlex.quote(str(pin)), shlex.quote(str(pin) + ".contested")
        self.assertEqual(new, f" (set -C; printf '%s\\n' \"$$\" > {p}) 2>/dev/null || mv -f {p} {c} 2>/dev/null || "
                              f"(: > {c}) 2>/dev/null; export DEVKIT_CALL={token}; readonly DEVKIT_CALL; git status")
        self.assertEqual(identity.strip_token(new), "git status")
        tid, nonce = identity.parse_token(token)
        entry = pg.call_entry(data)
        self.assertEqual(tid, data["tool_use_id"])
        self.assertEqual(entry["token_sha256"], hashlib.sha256(nonce.encode()).hexdigest())
        self.assertNotIn(nonce, json.dumps(entry))
        self.assertEqual((state.read(pg.session_path("claude", "s1")) or {}).get("call_token"), 1)

    def test_a_ledger_write_that_fails_stamps_no_token_and_is_audited(self):
        with mock.patch.object(pg, "_write_private", side_effect=OSError("disk full")):
            _, token, new = self.hook("dev progress note 'still working'")
        self.assertEqual(token, "")
        self.assertNotIn("DEVKIT_CALL", new)
        self.assertEqual([r["action"] for r in self.records("calls.token-skipped")], ["calls.token-skipped"])

    def test_a_busy_session_hold_stamps_no_token(self):
        with mock.patch.object(identity, "_session_update", side_effect=state.StateBusy("busy")):
            data, token, _ = self.hook("git status")
        self.assertEqual(token, "")
        self.assertNotIn(data["tool_use_id"], (state.read(pg.session_path("claude", "s1")) or {}).get(
            "calls_opened") or [])

    def test_closing_the_call_drops_its_use_mark_and_its_opened_mark(self):
        data, token, _ = self.hook("dev progress note x")
        self.assertEqual(self.dev("crew", "inbox", token=token)[0], 0)
        tid = data["tool_use_id"]
        self.assertTrue(identity.used_path("s1", tid).exists())
        self.closed(data)
        sess = state.read(pg.session_path("claude", "s1")) or {}
        self.assertFalse(identity.used_path("s1", tid).exists())
        self.assertNotIn(tid, sess.get("calls_opened") or [])
        self.assertIn(tid, sess.get("calls_closed") or [])


class ResolutionTests(TokenCase):
    def test_the_tech_leads_token_resolves_main_while_a_members_open_call_holds_the_same_words(self):
        self.plan_with_link()
        # a member's call whose program devkit cannot read through holds the tech lead's very words
        self.hook("python3 -I -c \"print('dev progress begin ci-redirect')\"", aid="a_link")
        _, token, _ = self.hook("dev progress begin ci-redirect 2>&1 | tail -6")
        rc, out = self.dev("progress", "begin", "ci-redirect", token=token)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state_of("ci-redirect"), "running")

    def test_a_cut_member_call_does_not_capture_the_tech_leads_command(self):
        self.plan_with_link()
        self.hook("python3 - <<'EOF'\n" + "print('x')\n" * 600 + "EOF", aid="a_link")  # over 4096 characters
        _, token, _ = self.hook("dev progress begin ci-redirect")
        rc, out = self.dev("progress", "begin", "ci-redirect", token=token)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.state_of("ci-redirect"), "running")

    def test_an_ack_with_a_nested_read_resolves_its_caller_for_the_tech_lead_and_a_member(self):
        self.plan_with_link()
        to_main = pg.crew_ask("Report: " + "x" * 400, from_agent="a_link")["id"]
        with mock.patch.object(pg, "caller", lambda *a, **k: {"caller": "main", "how": "test"}), \
                mock.patch.object(identity, "acting_lane", lambda *a, **k: "main"):
            to_link = pg.crew_ask("Brief: " + "y" * 400, to="Link")["id"]
        for ref, aid in ((to_main, None), (to_link, "a_link")):
            with self.subTest(ref=ref):
                _, token, new = self.hook(f'dev crew ack {ref} --sha256 "$(dev crew read {ref} --json | jq -r '
                                          f'.digest)" --generation 1', aid=aid)
                lane = LANE.search(new)
                env = {"DEVKIT_FROM_AGENT": lane.group(1)} if lane else {}
                rc, out = self.dev("crew", "read", ref, "--json", token=token, env=env)
                self.assertEqual(rc, 0, out)
                page = json.loads(out)
                rc, out = self.dev("crew", "ack", ref, "--sha256", page["digest"], "--generation",
                                   str(page["generation"]), token=token, env=env)
                self.assertEqual(rc, 0, out)
                receipts = next(m for m in self.run_()["messages"] if m["id"] == ref).get("receipts") or {}
                self.assertIn(aid or "main", receipts)

    def test_a_token_from_another_sessions_folder_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("dev progress begin ci-redirect", sid="s1")
        (pg.SESSIONS / "claude-s2.json").write_text(json.dumps({"agent": "claude", "session": "s2"}))
        rc, out = self.lead(token, env={"DEVKIT_SESSION": "claude:s2"})
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.foreign-session")), 1)

    def test_a_token_used_below_a_nested_agent_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("codex exec 'review the diff'")
        # dev -> shell -> codex -> the tech lead's call shell -> claude
        self.chain = [(INNER, "/bin/zsh -lc dev"), (NESTED, "codex exec review"), (SHELL, "/bin/zsh -c x"),
                      (AGENT, "claude")]
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.foreign-agent")), 1)

    def test_a_use_from_a_sibling_shell_is_refused_and_the_calls_own_shell_still_resolves_main(self):
        self.plan_with_link()
        _, token, _ = self.hook("dev progress begin ci-redirect")
        self.assertEqual(self.speak(token).lane, "main")  # from the call's own shell
        self.chain = [(SIBLING, "zsh"), (AGENT, "claude")]  # a member's shell under the same agent
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        found = self.records("caller.token-foreign-use")
        self.assertEqual(len(found), 1)
        self.assertIn(str(SIBLING), found[0]["detail"])
        self.chain = None
        rc, out = self.lead(token)
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_a_shell_pid_the_system_gave_to_another_process_is_refused(self):
        self.plan_with_link()
        _, token, _ = self.hook("dev progress begin ci-redirect")
        self.assertEqual(self.speak(token).lane, "main")  # its first use records the pid's start time
        self.starts[self.shell] += 5  # the call's shell ended and its pid went to a newer process
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.token-foreign-use")), 1)
        self.starts[self.shell] = None  # and once the pinned process exited, the token is stale
        self.assertEqual(self.lead(token)[0], 2)
        self.assertEqual(len(self.records("caller.stale-token")), 1)

    def test_a_detached_process_holding_a_live_token_changes_nothing_but_may_read(self):
        self.plan_with_link()
        to_main = pg.crew_ask("Report: " + "x" * 400, from_agent="a_link")["id"]
        _, token, _ = self.hook(f"dev crew read {to_main} --json; setsid dev progress add extra")
        self.own_session = 99999  # setsid: a process session of its own, while the call is still open
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        rc, out = self.dev("crew", "read", to_main, "--json", token=token)
        self.assertEqual(rc, 0, out)
        self.own_session, self.chain = None, []  # reparented to launchd: no agent above it at all
        rc, out = self.lead(token)
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())

    def test_a_token_whose_entry_has_no_hash_is_a_mismatch_never_legacy(self):
        self.plan_with_link()
        data = self.call("dev progress add extra")  # an entry written without a token hash
        identity.note_opened("claude", "s1", data["tool_use_id"])
        rc, out = self.lead(f"{data['tool_use_id']}.{'A' * 22}")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.mismatch")), 1)

    def test_a_forged_entry_the_hook_never_opened_is_refused(self):
        self.plan_with_link()
        self.hook("git status")  # a token session
        nonce = "B" * 22
        pg.call_opened("claude", {"session_id": "s1", "tool_use_id": "toolu_forged", "tool_name": "Bash",
                                  "tool_input": {"command": "dev progress add extra"}},
                       token_sha256=hashlib.sha256(nonce.encode()).hexdigest())
        rc, out = self.lead(f"toolu_forged.{nonce}")
        self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("calls.forged")), 1)

    def test_a_dev_process_resolves_once_and_keeps_its_caller_after_the_entry_closes(self):
        data, token, _ = self.hook("dev progress note x")
        self.assertEqual(self.speak(token, "progress", "note", "x").how, "token")
        self.closed(data)
        self.assertEqual(self.speak(token, "progress", "note", "x").lane, "main")  # the same process, still main
        self.plan_with_link()
        rc, out = self.dev("progress", "note", "x", token=token)  # a new command after the close
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.records("caller.stale-token")), 1)

    def test_a_background_calls_state_change_is_refused_with_the_foreground_fix(self):
        self.plan_with_link()
        data, token, _ = self.hook("dev progress note 'waiting on CI'", run_in_background=True)
        self.closed(data)  # a background call's result comes as soon as it starts
        rc, out = self.dev("progress", "note", "waiting on CI", token=token)
        self.assertEqual(rc, 2, out)
        self.assertIn("a background command's identity ends when it starts: run it in the foreground", out)

    def test_a_member_token_with_another_lanes_variable_is_a_conflict(self):
        self.plan_with_link()
        launch("a_tank", "devkit:tank", "Tank: api")
        _, token, _ = self.hook("dev progress note x", aid="a_link")
        rc, out = self.dev("progress", "note", "x", token=token, env={"DEVKIT_FROM_AGENT": "a_tank"})
        self.assertEqual(rc, 2, out)
        self.assertEqual(len(self.records("caller.conflict")), 1)

    def test_a_member_token_names_the_member_and_never_the_tech_lead(self):
        self.plan_with_link()
        _, token, _ = self.hook("python3 -I tools/assign.py", aid="a_link")  # no lane variable reaches the dev call
        rc, out = self.dev("progress", "begin", "ci-redirect", token=token)
        self.assertEqual(rc, 2, out)
        self.assertIn("only the tech lead", out)


class TokenSessionTests(TokenCase):
    def test_a_tokenless_state_change_in_a_token_session_is_refused_and_never_main(self):
        self.plan_with_link()
        self.hook("dev progress add extra")  # the tech lead's call is open; the session holds tokens
        self.call("python3 -I x.py", aid="a_link")  # a member's call that carries no token (env -u DEVKIT_CALL)
        rc, out = self.lead()
        self.assertEqual(rc, 2, out)
        self.assertIn("devkit could not verify this call's identity", out)
        self.assertFalse(self.added())
        self.assertEqual(len(self.records("caller.no-token")), 1)

    def test_a_session_the_hook_never_stamped_keeps_one_ledger_match(self):
        self.plan_with_link()
        self.call("dev progress add extra")
        rc, out = self.lead()
        self.assertEqual(rc, 0, out)
        self.assertTrue(self.added())

    def test_the_developers_own_terminal_is_unchanged(self):
        self.hook("git status")
        with mock.patch.object(pg, "resolve_session", lambda *a, **k: ("human", "shell-1", "shell")):
            sp = identity.speaker(argv=["progress", "begin", "ci-redirect"])
        self.assertEqual((sp.how, sp.lane), ("human", ""))

    def test_an_unknown_caller_never_acts_as_the_tech_lead_while_a_member_may_speak(self):
        self.plan_with_link()  # a session the hook never stamped, with the Release engineer at work
        with mock.patch.object(sys, "argv", ["dev", "crew", "ask", "hello"]):
            with self.assertRaises(identity.IdentityError):
                identity.acting_lane()


class AuditAndSecrecyTests(TokenCase):
    def test_every_identity_record_carries_the_fixed_fields_and_never_the_nonce(self):
        self.plan_with_link()
        _, token, _ = self.hook("dev progress begin ci-redirect")
        nonce = identity.parse_token(token)[1]
        (pg.SESSIONS / "claude-s2.json").write_text(json.dumps({"agent": "claude", "session": "s2"}))
        self.lead(token, env={"DEVKIT_SESSION": "claude:s2"})
        self.assertEqual(self.speak(token).lane, "main")  # the call's own shell
        self.chain = [(SIBLING, "zsh"), (AGENT, "claude")]
        self.lead(token)
        self.chain = None
        self.lead(f"{identity.parse_token(token)[0]}.{'C' * 22}")
        found = [r for r in audit.query(days=1) if r.get("action", "").startswith(("caller.", "calls."))
                 and r["action"] != "caller.refused"]
        self.assertEqual(sorted(r["action"] for r in found),
                         ["caller.foreign-session", "caller.mismatch", "caller.token-foreign-use"])
        for r in found:
            with self.subTest(r["action"]):
                self.assertEqual(r["crew"]["tool_use_id"], identity.parse_token(token)[0])
                self.assertIn(r["ref"]["session"], ("s1", "s2"))
                self.assertEqual(r["ref"]["agent"], "claude")
                self.assertEqual((r["outcome"], r["ref"]["result"]), ("blocked", "deny"))
                self.assertTrue(r["evidence"]["command"].startswith("dev progress add"), r["evidence"])
                self.assertIn(str(AGENT), r["ref"]["ancestors"])
                self.assertNotIn(nonce, json.dumps(r))

    def test_no_devkit_file_holds_the_nonce_after_a_stamped_call(self):
        self.plan_with_link()
        data, token, new = self.hook("dev progress begin ci-redirect")
        nonce = identity.parse_token(token)[1]
        self.assertEqual(self.dev("progress", "begin", "ci-redirect", token=token)[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hooks.post_tool_payload(self.agent, dict(data, hook_event_name="PostToolUse",
                                                     tool_input={"command": new},
                                                     tool_response={"stdout": "ok", "exit_code": 0}))
        self.assertIn(nonce, new)
        self.assertIn("set -C", new)
        held = [str(f.relative_to(self.root)) for f in self.root.rglob("*") if f.is_file()
                and nonce.encode() in f.read_bytes()]
        self.assertEqual(held, [])
        stamped = [str(f.relative_to(self.root)) for f in self.root.rglob("*") if f.is_file()
                   and (b"set -C" in f.read_bytes() or b"readonly DEVKIT_CALL" in f.read_bytes())]
        self.assertEqual(stamped, [])  # every stored command text is the one the agent typed
        self.assertEqual(audit.redact(f"x DEVKIT_CALL={token} y"), "x DEVKIT_CALL=…[redacted] y")

    def test_malformed_tokens_are_rejected_before_any_file_read(self):
        self.plan_with_link()
        self.hook("git status")
        bad = ["../x." + "n" * 22, ".shells." + "n" * 22, "toolu_1." + "n" * 21, "toolu_1." + "n" * 23,
               "toolu.1." + "n" * 22, "toolu_1/" + "n" * 22, ""]
        for value in bad:
            with self.subTest(value):
                self.assertIsNone(identity.parse_token(value))
                with mock.patch.object(identity, "_read_entry", side_effect=AssertionError("read")):
                    rc, out = self.lead(value or "x")
                self.assertEqual(rc, 2, out)
        self.assertFalse(self.added())


class SpawnAndAdviceTests(TokenCase):
    def test_devkits_detached_spawns_never_pass_the_call_token(self):
        with mock.patch.dict(os.environ, {identity.CALL_VAR: "toolu_1." + "n" * 22}), \
                mock.patch.object(hooks.subprocess, "Popen") as popen:
            hooks._spawn(["dev", "sync"])
            self.assertNotIn(identity.CALL_VAR, popen.call_args.kwargs["env"])
            self.assertNotIn(identity.CALL_VAR, identity.detached_env())
            self.assertEqual(identity.detached_env({"A": "1", identity.CALL_VAR: "v"}), {"A": "1"})
        # every start_new_session spawn in devkit builds its environment with identity.detached_env
        missing = []
        for f in (ROOT / "lib" / "devkit").rglob("*.py"):
            for node in ast.walk(ast.parse(f.read_text())):
                if isinstance(node, ast.Call) and any(k.arg == "start_new_session" for k in node.keywords):
                    env = next((k.value for k in node.keywords if k.arg == "env"), None)
                    if env is None or "detached_env" not in ast.unparse(env) and not self._env_from_helper(f, env):
                        missing.append(str(f.relative_to(ROOT)))
        # the tech lead places these two once their owners' lanes finish (ciwatch's CI watch, cli's own spawn)
        self.assertLessEqual(set(missing), {"lib/devkit/ciwatch.py", "lib/devkit/cli.py"}, missing)

    @staticmethod
    def _env_from_helper(f, env) -> bool:
        # a spawn whose env is a variable built by identity.detached_env in the same function
        name = ast.unparse(env)
        return re.search(rf"\b{re.escape(name)}\s*=\s*(?:dict\()?identity\.detached_env\(", f.read_text()) is not None

    def test_a_members_command_that_strips_its_identity_gets_advice_and_a_spoof_audit(self):
        self.plan_with_link()
        for cmd in ("env -u DEVKIT_FROM_AGENT dev crew inbox", "unset DEVKIT_CALL; dev crew inbox",
                    "env -i PATH=/usr/bin python3 x.py", "DEVKIT_CALL=toolu_9.abc dev crew inbox",
                    "export CODEX_THREAD_ID=x; dev crew inbox"):
            with self.subTest(cmd):
                self.hook(cmd, aid="a_link")
                self.assertIn("devkit · advice: ", self.last.get("additionalContext") or "")
                self.assertIn("identity", self.last.get("additionalContext") or "")
        self.assertEqual(len(self.records("caller.spoof-suspect")), 5)


if __name__ == "__main__":
    unittest.main()
