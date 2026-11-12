# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Claims in the guard (lib/devkit/guard.py) and in dev release (lib/devkit/gitx.py). A session's claim on
# release:<project>, push:<project>@<branch> or handoff:<project> refuses the agents of every other session; the
# holder's own session and its crew go through, and so does every call once the claim expires, its holder's session
# ends or its process dies, or the developer types `grant claim-break <resource>`. branch: and custom: claims only
# warn. Crew members never send to other sessions, no agent types the main-session stamp DEVKIT_SPEAKER, and devkit's
# peers state takes no agent write in any form. Every refusal has its allowed twin, with Claude Code and Codex
# payloads (a crew member's carries its agent_id on both).
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_guard_claims
import contextlib
import io
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(TESTS.parent / "lib"))

import _sandbox  # noqa: E402
from devkit import core, gitx, guard, hooks, identity, peers  # noqa: E402
from devkit import progress as pg  # noqa: E402

from test_peers import CLAUDE_A, CLAUDE_B, CODEX_A, PeerCase, sender  # noqa: E402

BRANCH = "feat/invoice-export"
REL = "release:acme/api"
PUSH = f"push:acme/api@{BRANCH}"
NOTE = "handoff:acme/api"
TURN = "019a6b2e-77c0-7d21-9a40-1f2e3d4c5b6a"
GRANT = {"id": "E2:prompt-77:claim-break release:acme/api", "source": "E2", "uses_left": 0, "expires": 0}
OTHERS = (("codex", CODEX_A), ("claude", CLAUDE_B))  # the sessions that do not hold the claims Claude A takes


def payload(agent, sid, tool, tool_input, aid="", cwd=""):
    # a PreToolUse payload as each agent sends it; a crew member's carries its agent_id, Codex's spawned agents' too
    p = {"session_id": sid, "hook_event_name": "PreToolUse", "cwd": cwd, "tool_name": tool, "tool_input": tool_input,
         "tool_use_id": "toolu_1", "permission_mode": "bypassPermissions"}
    if agent == "claude":
        p["transcript_path"] = f"/Users/x/.claude/projects/-acme-api/{sid}.jsonl"
    else:
        p.update(turn_id=TURN, model="gpt-6.1-sol", transcript_path=None)
    if aid:
        p["agent_id"] = aid
    return p


class ClaimCase(PeerCase):
    # three live sessions of acme/api (Claude A, Codex B, Claude C) in the peers store, a real checkout on BRANCH with
    # one commit and no remote, and the project's handoff note; the push budget is another rule's and stays out
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self.three_sessions()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        git = ["git", "-C", str(self.checkout)]
        for args in (["init", "-q", "-b", BRANCH],
                     ["-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "x"]):
            subprocess.run(git + args, check=True, capture_output=True, env=_sandbox.env())
        self.note = core.HANDOFF / "projects" / "acme" / "api.md"
        self.note.parent.mkdir(parents=True)
        self.note.write_text("# acme/api\n\n## Next steps\n\n- ship\n")
        for p in (mock.patch.object(guard, "current_project", lambda cwd: "acme/api"),
                  mock.patch.object(guard, "check_push_budget", lambda *a, **k: None)):
            p.start()
            self.addCleanup(p.stop)

    def verdict(self, agent, sid, tool, tool_input, aid="", cwd=None):
        cwd = str(cwd or self.checkout)
        return guard.check(tool, tool_input, cwd, agent, call=payload(agent, sid, tool, tool_input, aid, cwd))

    def shell(self, agent, sid, cmd, aid=""):
        return self.verdict(agent, sid, "Bash", {"command": cmd}, aid)

    def assert_refused(self, v, *parts):
        self.assertIsNotNone(v)
        self.assertEqual(v[0], "deny", v[1])
        for part in parts:
            self.assertIn(part, v[1])
        return v[1]


class ReleaseClaimTests(ClaimCase):
    def test_release_refused_while_claimed_elsewhere(self):
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45)
        for agent, sid in OTHERS:
            for cmd in ("dev release", "dev release --version 26.11.9 --why 'one item stays open'",
                        "git push origin v26.11.9", "git push --tags", "git push --follow-tags origin",
                        "git push origin HEAD:main", "cd /tmp && dev release", "sh -c 'dev release'"):
                with self.subTest(agent=agent, cmd=cmd):
                    self.assert_refused(self.shell(agent, sid, cmd), f"{REL} is claimed by session claude:4d1cffc7",
                                        "“cutting 26.11.9”", f"dev session claim {REL} --queue",
                                        "dev session ask @4d1cffc7", f"grant claim-break {REL}")
            # twins: a dry run, help and a branch push release nothing
            for cmd in ("dev release --dry-run", "dev release --help", f"git push origin {BRANCH}", "git tag -l"):
                with self.subTest(agent=agent, cmd=cmd, twin=True):
                    self.assertIsNone(self.shell(agent, sid, cmd))

    def test_own_claim_lets_release_through(self):
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45)
        for aid in ("", "a_link"):  # the holder's main session, and its own crew member
            for cmd in ("dev release", "git push origin v26.11.9", "dev pr merge 12"):
                with self.subTest(aid=aid, cmd=cmd):
                    self.assertIsNone(self.shell("claude", CLAUDE_A, cmd, aid=aid))
        peers.release(sender(CLAUDE_A), REL)
        peers.claim(sender(CODEX_A, "codex"), REL, "the Codex session releases", minutes=30)
        for aid in ("", "a_link"):
            with self.subTest(codex_holder=True, aid=aid):
                self.assertIsNone(self.shell("codex", CODEX_A, "dev release", aid=aid))
        # twin: the Claude session is the other one now, and its crew member is refused with it
        for aid in ("", "a_link"):
            with self.subTest(twin=True, aid=aid):
                self.assert_refused(self.shell("claude", CLAUDE_A, "dev release", aid=aid),
                                    f"{REL} is claimed by session codex:0199a213")

    def test_merge_refused_during_release_claim(self):
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45)
        for agent, sid in OTHERS:
            for cmd in ("dev pr merge 12", "dev pr merge", "dev pr merge 12 --squash", "dev gh pr merge 12",
                        "DEVKIT_RAW_GH=1 gh pr merge 12 --squash", "DEVKIT_RAW_GH=1 gh release create v26.11.9"):
                with self.subTest(agent=agent, cmd=cmd):
                    self.assert_refused(self.shell(agent, sid, cmd), f"{REL} is claimed by session claude:4d1cffc7")
            for cmd in ("dev pr merge 12 --dry-run", "dev pr view 12", "dev pr checks 12", "gh pr view 12"):
                with self.subTest(agent=agent, cmd=cmd, twin=True):
                    self.assertIsNone(self.shell(agent, sid, cmd))

    def test_enforced_claim_lapses_on_ttl_end_and_death(self):
        # expired: a claim taken an hour ago for 30 minutes holds nothing
        peers.claim(sender(CLAUDE_A), REL, "an old release", minutes=30, now=self.now - 3600)
        self.assertIsNone(self.shell("codex", CODEX_A, "dev release"))
        # the holder's session ended
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=30)
        self.assert_refused(self.shell("codex", CODEX_A, "dev release"), REL)
        self.session("claude", CLAUDE_A, pid=101, ended={"ts": self.now, "reason": "exit"})
        self.assertIsNone(self.shell("codex", CODEX_A, "dev release"))
        # the process that took the claim died (pid and start time)
        self.session("claude", CLAUDE_A, pid=101)
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9 again", minutes=30)
        self.assert_refused(self.shell("claude", CLAUDE_B, "dev release"), REL)
        self.alive[101] = False
        self.assertIsNone(self.shell("claude", CLAUDE_B, "dev release"))

    def test_developer_grant_breaks_the_claim(self):
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45)
        asked = []

        def use_grant(agent, sid, *, action, target, repo="", lane=None, consume=True, now=None):
            # the developer typed `grant claim-break release:acme/api` in Claude C, and nowhere else
            asked.append((agent, sid, action, target, repo, lane, consume))
            return GRANT if (agent, sid, action, target, lane) == ("claude", CLAUDE_B, "claim-break", REL, None) \
                else None
        with mock.patch.object(pg, "use_grant", use_grant):
            self.assert_refused(self.shell("codex", CODEX_A, "dev release"), REL)  # no grant in that session
            why = self.assert_refused(self.shell("claude", CLAUDE_B, "dev release", aid="a_link"), REL)
            self.assertIn("crew member", why)  # only the tech lead breaks a claim with the grant
            self.assertIsNone(self.shell("claude", CLAUDE_B, "dev release"))
        self.assertIsNone(peers.holder(REL))
        self.assertIn(("claude", CLAUDE_B, "claim-break", REL, "acme/api", None, True), asked)  # one use taken
        told = [m["text"] for m in peers.inbox("claude", CLAUDE_A, all_=True) if m["kind"] == "notice"]
        self.assertTrue(told and f"your claim on {REL} was broken by" in told[0], told)

    def test_developers_terminal_is_never_refused(self):
        peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45)
        human = identity.Speaker("human", "shell-4242", "", "human", True)
        other = identity.Speaker("codex", CODEX_A, "main", "ledger", False)
        with mock.patch.object(identity, "speaker", lambda *a, **k: human):  # `dev guard check` in a terminal
            self.assertIsNone(guard.check("Bash", {"command": "dev release"}, str(self.checkout), "claude"))
        with mock.patch.object(identity, "speaker", lambda *a, **k: other):  # the same probe from another session
            self.assert_refused(guard.check("Bash", {"command": "dev release"}, str(self.checkout), "codex"), REL)


class PushClaimTests(ClaimCase):
    def test_push_refused_for_claimed_branch(self):
        peers.claim(sender(CLAUDE_A), PUSH, "rebasing the API branch", minutes=30)
        peers.claim(sender(CLAUDE_A), "branch:acme/api@feat/other", "advisory only", minutes=30)
        for agent, sid in OTHERS:
            for cmd in ("git push", f"git push origin {BRANCH}", "git push -u origin HEAD",
                        f"git push origin HEAD:{BRANCH}", "dev push", f"git push --force-with-lease origin {BRANCH}",
                        f"git push origin --delete {BRANCH}", "git push --all origin", f"dev git push origin {BRANCH}",
                        f"git -C {self.checkout} push"):
                with self.subTest(agent=agent, cmd=cmd):
                    self.assert_refused(self.shell(agent, sid, cmd), f"{PUSH} is claimed by session claude:4d1cffc7",
                                        "“rebasing the API branch”")
            # twins: another branch (a branch: claim only warns), and dry runs, push nothing claimed
            for cmd in ("git push origin feat/other", "git push origin HEAD:feat/other", "dev push --dry-run",
                        f"git push --dry-run origin {BRANCH}"):
                with self.subTest(agent=agent, cmd=cmd, twin=True):
                    self.assertIsNone(self.shell(agent, sid, cmd))
        for aid in ("", "a_tank"):  # the holder and its crew push the branch
            with self.subTest(holder=True, aid=aid):
                self.assertIsNone(self.shell("claude", CLAUDE_A, f"git push origin {BRANCH}", aid=aid))

    def test_claim_checks_run_only_for_commands_that_touch_a_claim(self):
        asked = []
        real = peers.holder
        with mock.patch.object(peers, "holder", lambda *a, **k: asked.append(a[0]) or real(*a, **k)):
            for cmd in ("dev release", f"git push origin {BRANCH}", "dev pr merge 12"):  # no claim anywhere
                self.assertIsNone(self.shell("codex", CODEX_A, cmd))
            self.assertEqual(asked, [])
            peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45)
            for cmd in ("git status", "ls -la", "python3 -m pytest -q", "dev status", "git log --oneline -3",
                        "dev ci --run", f"git push origin {BRANCH}"):
                with self.subTest(cmd=cmd):
                    self.assertIsNone(self.shell("codex", CODEX_A, cmd))
            self.assertEqual(asked, [])
            self.assert_refused(self.shell("codex", CODEX_A, "dev release"), REL)
            self.assertEqual(asked, [REL])


class HandoffClaimTests(ClaimCase):
    def test_handoff_note_write_refused(self):
        peers.claim(sender(CLAUDE_A), NOTE, "rewriting the next steps", minutes=30)
        note = str(self.note)
        edit = {"file_path": note, "old_string": "- ship", "new_string": "- ship 26.11.9"}
        patch = f"*** Begin Patch\n*** Update File: {note}\n@@\n-- ship\n+- ship 26.11.9\n*** End Patch\n"
        refused = [("claude", CLAUDE_B, "Edit", edit),
                   ("claude", CLAUDE_B, "Write", {"file_path": note, "content": "# acme/api\n"}),
                   ("claude", CLAUDE_B, "MultiEdit", {"file_path": note, "edits": [edit]}),
                   ("codex", CODEX_A, "apply_patch", {"input": patch}),
                   ("codex", CODEX_A, "Bash", {"command": "dev note set status=blocked"}),
                   ("claude", CLAUDE_B, "Bash", {"command": "dev note set priority=high -p acme/api"}),
                   ("claude", CLAUDE_B, "Bash", {"command": "dev handoff --edit"})]
        for agent, sid, tool, ti in refused:
            with self.subTest(agent=agent, tool=tool, input=ti):
                self.assert_refused(self.verdict(agent, sid, tool, ti), f"{NOTE} is claimed by session claude:4d1cffc7",
                                    "“rewriting the next steps”")
        # twins: the session log and the project's plans are not the note; reading it writes nothing
        plan = str(core.HANDOFF / "plans" / "acme" / "api" / "2026-11-09-x.md")
        for agent, sid, tool, ti in [("claude", CLAUDE_B, "Bash", {"command": 'dev handoff --log "shipped 26.11.9"'}),
                                     ("codex", CODEX_A, "Bash", {"command": "dev handoff"}),
                                     ("claude", CLAUDE_B, "Write", {"file_path": plan, "content": "# plan\n"})]:
            with self.subTest(agent=agent, tool=tool, input=ti, twin=True):
                self.assertIsNone(self.verdict(agent, sid, tool, ti))
        for aid in ("", "a_ghost"):  # the holder and its crew edit the note
            with self.subTest(holder=True, aid=aid):
                self.assertIsNone(self.verdict("claude", CLAUDE_A, "Edit", edit, aid=aid))


class SessionWriteTests(ClaimCase):
    def test_member_session_write_refused_read_allowed(self):
        writes = ('dev session tell @7be0a41d "the API contract changed"', 'dev session ask project "May I release?"',
                  'dev session answer S-k3x9a2bq "yes, go"', f'dev session claim {REL} --why "release" --for 45m',
                  f"dev session release {REL}", 'dev session escalate @7be0a41d "Which first?" --options "A: x; B: y"',
                  "cd /tmp && dev session tell all hello", "bash -c 'dev session ask repo hello'")
        reads = ("dev session ls", "dev session ls --json", "dev session inbox", "dev session claims",
                 "dev session escalations", "dev session tell --help")
        for agent, sid in (("claude", CLAUDE_A), ("codex", CODEX_A)):
            for cmd in writes:
                with self.subTest(agent=agent, cmd=cmd):
                    self.assert_refused(self.shell(agent, sid, cmd, aid="a_tank"), "dev crew ask")
                    self.assertIsNone(self.shell(agent, sid, cmd))  # twin: the tech lead's own call
            for cmd in reads:
                with self.subTest(agent=agent, cmd=cmd, read=True):
                    self.assertIsNone(self.shell(agent, sid, cmd, aid="a_tank"))

    def test_hook_payload_names_the_caller(self):
        # the PreToolUse hook passes the guard no payload: the guard takes the one this hook process read, and only
        # when it carries this very tool input
        main = identity.Speaker("claude", CLAUDE_A, "main", "stamped", True)
        for agent, sid in (("claude", CLAUDE_A), ("codex", CODEX_A)):
            ti = {"command": "dev session tell all hello"}
            read = payload(agent, sid, "Bash", ti, aid="a_tank", cwd=str(self.checkout))
            with mock.patch.object(hooks, "_PAYLOAD", read), \
                    mock.patch.object(identity, "speaker", lambda *a, **k: main):
                with self.subTest(agent=agent):
                    self.assert_refused(guard.check("Bash", ti, str(self.checkout), agent), "dev crew ask")
                with self.subTest(agent=agent, twin="another call's payload"):
                    self.assertIsNone(guard.check("Bash", dict(ti), str(self.checkout), agent))

    def test_member_program_that_sends_to_sessions_refused(self):
        sends = "python3 -c \"import subprocess; subprocess.run(['dev', 'session', 'tell', 'all', 'hi'])\""
        mentions = "python3 -c \"print('dev session tell all hi')\""
        v = guard.crew_shell_verdict(sends, str(self.checkout), lane="a_tank", session=CLAUDE_A)
        self.assert_refused(v, "dev crew ask")
        self.assertIsNone(guard.crew_shell_verdict(mentions, str(self.checkout), lane="a_tank", session=CLAUDE_A))
        # a member's program that starts a process is one the allow-list judge does not vouch for, a read included;
        # the read itself, typed as a command, runs
        reads = "python3 -c \"import subprocess; subprocess.run(['dev', 'session', 'ls'])\""
        why = guard.crew_shell_verdict(reads, str(self.checkout), lane="a_tank", session=CLAUDE_A)
        self.assertIn("run the command it needs yourself", (why or ("", ""))[1])
        self.assertIsNone(guard.crew_shell_verdict("dev session ls", str(self.checkout), lane="a_tank",
                                                   session=CLAUDE_A))

    def test_member_cannot_set_devkit_speaker(self):
        sets = ("DEVKIT_SPEAKER=main dev session tell all hi", "DEVKIT_SPEAKER=main make release",
                "export DEVKIT_SPEAKER=main", "export DEVKIT_SPEAKER=main; dev session ls",
                "env DEVKIT_SPEAKER=main sh -c 'dev session ask all hi'", "declare -x DEVKIT_SPEAKER=main",
                "DEVKIT_SPEAKER=main; export DEVKIT_SPEAKER", "cd /tmp && DEVKIT_SPEAKER=main python3 tool.py")
        reads = ("echo $DEVKIT_SPEAKER", "printenv DEVKIT_SPEAKER", "unset DEVKIT_SPEAKER",
                 "grep -rn DEVKIT_SPEAKER lib", "echo DEVKIT_SPEAKER=main")
        for agent, sid in (("claude", CLAUDE_A), ("codex", CODEX_A)):
            for aid in ("a_tank", ""):  # a crew member, and the main session alike: only devkit's hook stamps it
                for cmd in sets:
                    with self.subTest(agent=agent, aid=aid, cmd=cmd):
                        self.assert_refused(self.shell(agent, sid, cmd, aid=aid), "DEVKIT_SPEAKER")
                for cmd in reads:
                    with self.subTest(agent=agent, aid=aid, cmd=cmd, twin=True):
                        self.assertIsNone(self.shell(agent, sid, cmd, aid=aid))

    def test_member_cannot_set_codex_thread_id(self):
        # Codex runs several threads in one process and devkit tells them apart by CODEX_THREAD_ID: a member that set
        # another thread's id would speak as that thread's session
        other = "0199a213-9f3e-7c11-b2d4-77e0c1f9a0d2"
        sets = (f"CODEX_THREAD_ID={other} dev session ls", f"export CODEX_THREAD_ID={other}",
                f"env CODEX_THREAD_ID={other} python3 tool.py", f"CODEX_THREAD_ID={other}; export CODEX_THREAD_ID",
                f"declare -x CODEX_THREAD_ID={other}", f"cd /tmp && CODEX_THREAD_ID={other} make check")
        for agent, sid in (("claude", CLAUDE_A), ("codex", CODEX_A)):
            for cmd in sets:
                with self.subTest(agent=agent, cmd=cmd):
                    self.assert_refused(self.shell(agent, sid, cmd, aid="a_tank"), "CODEX_THREAD_ID")
            for cmd in ("echo $CODEX_THREAD_ID", "printenv CODEX_THREAD_ID", f"echo CODEX_THREAD_ID={other}"):
                with self.subTest(agent=agent, cmd=cmd, twin="reads"):
                    self.assertIsNone(self.shell(agent, sid, cmd, aid="a_tank"))
            # the tech lead's own session: a devkit call that names another thread is refused, any other program runs
            with self.subTest(agent=agent, twin="main"):
                self.assert_refused(self.shell(agent, sid, f"CODEX_THREAD_ID={other} dev session tell all hi"),
                                    "CODEX_THREAD_ID")
                self.assertIsNone(self.shell(agent, sid, f"CODEX_THREAD_ID={other} python3 tool.py"))

    def test_main_session_stamp_is_never_the_developers_terminal(self):
        # a process that carries the stamp runs under an agent, whatever its parents and terminal say
        self.assertFalse(core.set_by_developer({"DEVKIT_SPEAKER": "main"}, ancestry=[], terminal=True))
        self.assertTrue(core.set_by_developer({}, ancestry=[], terminal=True))


class PeersStateTests(ClaimCase):
    def test_agent_write_to_peers_state_refused(self):
        claim = core.STATE / "peers" / "claims" / "0123456789abcdef.json"
        box = core.STATE / "peers" / "box" / f"claude-{CLAUDE_B}.json"
        forms = [("Write", {"file_path": str(claim), "content": "{}"}),
                 ("Edit", {"file_path": str(box), "old_string": "a", "new_string": "b"}),
                 ("apply_patch", {"input": f"*** Begin Patch\n*** Add File: {claim}\n+{{}}\n*** End Patch\n"}),
                 ("Bash", {"command": f"echo '{{}}' > {claim}"}), ("Bash", {"command": f"cp /tmp/x.json {claim}"}),
                 ("Bash", {"command": f"mv /tmp/x.json {box}"}), ("Bash", {"command": f"echo x | tee -a {box}"}),
                 ("Bash", {"command": f"sed -i '' 's/a/b/' {box}"}),
                 ("Bash", {"command": f"python3 -c \"open('{claim}', 'w').write('x')\""})]
        for agent, sid in (("claude", CLAUDE_A), ("codex", CODEX_A)):
            for aid in ("", "a_tank"):
                for tool, ti in forms:
                    with self.subTest(agent=agent, aid=aid, tool=tool, input=ti):
                        why = self.assert_refused(self.verdict(agent, sid, tool, ti, aid=aid))
                        if "python3" not in str(ti):  # a program's write is refused by its own rule
                            self.assertIn("session messages and claims", why)
                            self.assertIn("`dev session`", why)
                for cmd in (f"cat {box}", f"ls {claim.parent}", f"jq . {claim}"):  # twins: reading is fine
                    with self.subTest(agent=agent, aid=aid, cmd=cmd, twin=True):
                        self.assertIsNone(self.shell(agent, sid, cmd, aid=aid))


class DevReleaseClaimTests(ClaimCase):
    def run_release(self, sp, body, dry_run=False):
        # cmd_release around its body: the checkout, the speaker and the release itself are the test's
        c = SimpleNamespace(pid="acme/api", top=str(self.checkout), branch="main", default="main")
        a = SimpleNamespace(dry_run=dry_run, version=None, why=None)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(gitx, "ctx", lambda: c), mock.patch.object(identity, "speaker", lambda *x, **k: sp), \
                mock.patch.object(gitx, "_release", body), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = gitx.cmd_release(a)
        return rc, out.getvalue() + err.getvalue()

    def test_dev_release_releases_claim_on_failure(self):
        main = identity.Speaker("claude", CLAUDE_A, "main", "stamped", True)
        held = []

        def failing(a, c):
            held.append(peers.holder(REL, me=self.b))  # held for the session while the release runs
            return 1
        self.assertEqual(self.run_release(main, failing)[0], 1)
        self.assertEqual((held[0]["key"], held[0]["why"]), (self.a, "dev release runs"))
        self.assertIsNone(peers.holder(REL))

        def crashing(a, c):
            raise RuntimeError("gh release create crashed")
        with self.assertRaises(RuntimeError):
            self.run_release(main, crashing)
        self.assertIsNone(peers.holder(REL))
        # a crew member of a Codex session releases: devkit holds the claim for its session, and gives it back
        member = identity.Speaker("codex", CODEX_A, "a_link", "payload", True)
        held.clear()
        self.assertEqual(self.run_release(member, lambda a, c: held.append(peers.holder(REL, me=self.a)) or 0)[0], 0)
        self.assertEqual(held[0]["key"], self.b)
        self.assertIsNone(peers.holder(REL))
        # twin: another session holds it, so the release never starts
        peers.claim(sender(CLAUDE_B), REL, "their release", minutes=30)
        ran = []
        rc, text = self.run_release(main, lambda a, c: ran.append(1) or 0)
        self.assertEqual((rc, ran), (3, []))
        self.assertIn(f"{REL} is claimed by session claude:7be0a41d", text)
        self.assertEqual(peers.holder(REL)["key"], self.c)

    def test_dev_release_keeps_a_claim_it_found_and_never_blocks_the_developer(self):
        main = identity.Speaker("claude", CLAUDE_A, "main", "stamped", True)
        mine = peers.claim(sender(CLAUDE_A), REL, "the whole release train", minutes=120)
        self.assertEqual(self.run_release(main, lambda a, c: 0)[0], 0)
        h = peers.holder(REL)
        self.assertEqual((h["key"], h["why"], h["until"]), (self.a, "the whole release train", mine["until"]))
        peers.release(sender(CLAUDE_A), REL)
        # a dry run takes no claim
        held = []
        self.run_release(main, lambda a, c: held.append(peers.holder(REL)) or 0, dry_run=True)
        self.assertEqual(held, [None])
        # the developer's own terminal releases even while a session holds the claim, and is told who holds it
        peers.claim(sender(CLAUDE_B), REL, "their release", minutes=30)
        human = identity.Speaker("human", "shell-4242", "", "human", True)
        ran = []
        rc, text = self.run_release(human, lambda a, c: ran.append(1) or 0)
        self.assertEqual((rc, ran), (0, [1]))
        self.assertIn(f"{REL} is claimed by session claude:7be0a41d", text)
        self.assertEqual(peers.holder(REL)["key"], self.c)
        # an agent's dry run still runs, and says the real release would be refused
        rc, text = self.run_release(main, lambda a, c: ran.append(2) or 0, dry_run=True)
        self.assertEqual((rc, ran), (0, [1, 2]))
        self.assertIn(f"{REL} is claimed by session claude:7be0a41d", text)
        self.assertIn("dev release refuses until it is free", text)


if __name__ == "__main__":
    unittest.main()
