# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Claims and escalations of the peers store (lib/devkit/peers.py). A main session claims a shared resource of its own
# project for 5 to 240 minutes; the claim is void once it expires or its holder ends or its process dies; a refused
# claimant can queue and hears `claim-free`; breaking a live claim needs the developer's grant. An escalation is
# settled only by the developer's own typed line, once per prompt. Claude Code and Codex session shapes, a fixed clock,
# and real processes for the race.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_peers_claims
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import core, peers  # noqa: E402
from devkit import progress as pg  # noqa: E402

from test_peers import CLAUDE_A, CLAUDE_B, CODEX_A, CODEX_B, PeerCase, sender  # noqa: E402

REL = "release:acme/api"
GRANT = {"id": "E2:prompt-77:claim-break release:acme/api", "source": "E2", "uses_left": 0, "expires": 0}


class ClaimTests(PeerCase):
    def test_claim_exclusive(self):
        a, b, c = self.three_sessions()
        got = peers.claim(sender(CLAUDE_A), REL, "cutting 26.11.9", minutes=45, now=self.now)
        self.assertEqual((got["state"], got["until"]), ("held", self.now + 45 * 60))
        with self.assertRaises(peers.PeerError) as e:
            peers.claim(sender(CODEX_A, "codex"), REL, "my release", now=self.now + 60)
        self.assertEqual(e.exception.code, 3)
        text = str(e.exception)
        self.assertIn(f"{REL} is claimed by session claude:4d1cffc7 (acme/api · main) until", text)
        self.assertIn("“cutting 26.11.9”", text)
        self.assertIn(f"dev session claim {REL} --queue", text)
        self.assertIn(f"grant claim-break {REL}", text)
        h = peers.holder(REL, me=b, now=self.now + 60)
        self.assertEqual((h["key"], h["why"], h["until"], h["project"]), (a, "cutting 26.11.9", self.now + 2700,
                                                                          "acme/api"))
        self.assertIsNone(peers.holder(REL, me=a, now=self.now + 60))
        self.assertEqual([x["resource"] for x in peers.claims(now=self.now, project="acme/api")], [REL])
        self.assertEqual(peers.claims(now=self.now, project="acme/web"), [])
        self.assertIn("peer.claim", self.audit_text())

    def test_claim_renew_and_bounds(self):
        self.three_sessions()
        for minutes in (peers.CLAIM_MIN - 1, peers.CLAIM_MAX_MIN + 1):
            with self.assertRaises(peers.PeerError) as e:
                peers.claim(sender(CLAUDE_A), REL, "release", minutes=minutes, now=self.now)
            self.assertEqual(e.exception.code, 2)
        for bad in ("item:acme/api#T-1", "release:acme", "push:acme/api", "custom:Has Space", "deploy:acme/api"):
            with self.assertRaises(peers.PeerError) as e:
                peers.claim(sender(CLAUDE_A), bad, "why", now=self.now)
            self.assertEqual(e.exception.code, 2, bad)
        with self.assertRaises(peers.PeerError) as e:
            peers.claim(sender(CLAUDE_A), "item:acme/api", "why", now=self.now)
        self.assertIn("dev task start", str(e.exception))
        first = peers.claim(sender(CLAUDE_A), REL, "release", minutes=peers.CLAIM_MIN, now=self.now)
        self.assertEqual(first["until"], self.now + 300)
        again = peers.claim(sender(CLAUDE_A), REL, "release, longer", minutes=peers.CLAIM_MAX_MIN, now=self.now + 120)
        self.assertEqual((again["state"], again["until"], again["why"]),
                         ("renewed", self.now + 120 + 240 * 60, "release, longer"))
        dflt = peers.claim(sender(CLAUDE_A), "push:acme/api@feat/x-1", "pushing", now=self.now)
        self.assertEqual(dflt["until"], self.now + peers.CLAIM_DEFAULT_MIN * 60)
        for ok in ("handoff:acme/api", "branch:acme/api@main", "custom:db-reset"):
            self.assertEqual(peers.claim(sender(CLAUDE_A), ok, "x", now=self.now)["state"], "held", ok)

    def test_claim_project_ids_of_any_host(self):
        gl = self.git_repo("gitlab.com/group/sub/repo", "main")
        self.session("claude", CLAUDE_A, project="gitlab.com/group/sub/repo", cwd=gl, pid=101)
        for res in ("release:gitlab.com/group/sub/repo", "push:gitlab.com/group/sub/repo@main"):
            self.assertEqual(peers.claim(sender(CLAUDE_A), res, "x", now=self.now)["state"], "held")
            self.assertEqual(peers.resource_project(res), "gitlab.com/group/sub/repo")
        self.assertIsNone(peers.resource_project("custom:db-reset"))

    def test_claim_void_when_holder_ended_or_pid_gone(self):
        a, b, c = self.three_sessions()
        peers.claim(sender(CLAUDE_A), REL, "release", now=self.now)
        self.alive[101] = False  # Claude A's process died
        self.assertIsNone(peers.holder(REL, me=b, now=self.now + 60))
        self.assertEqual(peers.claim(sender(CODEX_A, "codex"), REL, "mine now", now=self.now + 60)["state"], "held")
        f = pg.SESSIONS / f"codex-{CODEX_A}.json"
        rec = json.loads(f.read_text())
        rec["ended"] = {"ts": self.now + 70, "reason": "exit"}
        f.write_text(json.dumps(rec))
        self.assertIsNone(peers.holder(REL, me=c, now=self.now + 80))
        self.assertEqual(peers.claim(sender(CLAUDE_B), REL, "after Codex", minutes=5, now=self.now + 80)["state"],
                         "held")
        self.assertIsNone(peers.holder(REL, me=b, now=self.now + 80 + 301))  # it expired
        self.assertEqual(peers.claims(now=self.now + 80 + 301), [])

    def test_claim_holder_is_its_process_so_a_reused_pid_keeps_nothing(self):
        # Claude A's record names its pid without a start time; the claim records the start time it read then
        a = self.session("claude", CLAUDE_A, pid=101, pid_start="")
        b = self.session("codex", CODEX_A, pid=202)
        with mock.patch.object(peers, "_start_of", lambda pid: "Mon Nov  9 09:00:00 2026" if pid == 101 else ""):
            got = peers.claim(sender(CLAUDE_A), REL, "release", now=self.now)
        self.assertEqual((got["holder"]["pid"], got["holder"]["pid_start"]), (101, "Mon Nov  9 09:00:00 2026"))
        self.assertEqual(peers.holder(REL, me=b, now=self.now + 60)["key"], a)  # the twin: its process runs
        self.alive[(101, "Mon Nov  9 09:00:00 2026")] = False  # pid 101 now belongs to another process
        self.assertIsNone(peers.holder(REL, me=b, now=self.now + 60))
        self.assertEqual(peers.claim(sender(CODEX_A, "codex"), REL, "mine", now=self.now + 60)["state"], "held")

    def test_claim_other_project_refused(self):
        self.three_sessions()
        with self.assertRaises(peers.PeerError) as e:
            peers.claim(sender(CLAUDE_A), "release:acme/web", "not mine", now=self.now)
        self.assertEqual(e.exception.code, 2)
        self.session("codex", CODEX_B, project="", cwd=core.DEV, branch="", pid=909)  # a portfolio session
        self.assertEqual(peers.claim(sender(CODEX_B, "codex"), "release:acme/web", "coordinating", now=self.now)
                         ["state"], "held")
        with self.assertRaises(peers.PeerError) as e:
            peers.claim(sender(CLAUDE_A, lane="a1"), "release:acme/api", "a member", now=self.now)
        self.assertEqual(e.exception.code, 2)

    def test_queue_gets_claim_free_event(self):
        a, b, c = self.three_sessions()
        peers.claim(sender(CLAUDE_A), REL, "release", now=self.now)
        q = peers.claim(sender(CODEX_A, "codex"), REL, "next release", queue=True, now=self.now + 10)
        self.assertEqual((q["state"], q["holder"]["key"]), ("queued", a))
        self.assertEqual([(x["resource"], x["holder"]["key"]) for x in peers.queued("codex", CODEX_A, now=self.now)],
                         [(REL, a)])
        r = peers.release(sender(CLAUDE_A), REL, "done", now=self.now + 600)
        self.assertEqual(r["state"], "released")
        free = [m for m in peers.take("codex", CODEX_A) if m["kind"] == "claim-free"]
        self.assertEqual(len(free), 1)
        self.assertIn(REL, free[0]["text"])
        self.assertEqual(peers.queued("codex", CODEX_A, now=self.now + 600), [])
        self.assertEqual(peers.claim(sender(CODEX_A, "codex"), REL, "mine", now=self.now + 610)["state"], "held")
        self.assertEqual(peers.holder(REL, me=a, now=self.now + 610)["key"], b)
        self.assertEqual(peers.release(sender(CODEX_A, "codex"), REL, now=self.now + 620)["state"], "released")
        self.assertEqual(peers.release(sender(CLAUDE_B), REL, now=self.now + 630)["state"], "not held")

    def test_holder_end_frees_queue(self):
        a, b, c = self.three_sessions()
        peers.claim(sender(CLAUDE_A), REL, "release", now=self.now)
        peers.claim(sender(CODEX_A, "codex"), REL, "after you", queue=True, now=self.now + 10)
        peers.claim(sender(CLAUDE_B), REL, "me too", queue=True, now=self.now + 20)
        (pg.SESSIONS / f"claude-{CLAUDE_A}.json").unlink()  # its session ended
        self.assertIsNone(peers.holder(REL, now=self.now + 30))
        self.assertIsNone(peers.holder(REL, now=self.now + 40))
        for agent, sid in (("codex", CODEX_A), ("claude", CLAUDE_B)):
            free = [m for m in peers.take(agent, sid) if m["kind"] == "claim-free"]
            self.assertEqual(len(free), 1, (agent, free))
            self.assertEqual(peers.queued(agent, sid, now=self.now + 40), [])
        peers.sweep(now=self.now + 50)
        self.assertFalse(peers.claim_path(REL).exists())

    def test_break_needs_grant_evidence(self):
        a, b, c = self.three_sessions()
        peers.claim(sender(CLAUDE_A), REL, "release", now=self.now)
        with self.assertRaises(peers.PeerError) as e:
            peers.release(sender(CODEX_A, "codex"), REL, "it is stuck", now=self.now + 60)
        self.assertEqual(e.exception.code, 3)
        self.assertEqual(peers.holder(REL, me=b, now=self.now + 60)["key"], a)
        r = peers.release(sender(CODEX_A, "codex"), REL, "it is stuck", force_evidence=GRANT, now=self.now + 60)
        self.assertEqual(r["state"], "broken")
        self.assertIsNone(peers.holder(REL, me=b, now=self.now + 60))
        told = [m for m in peers.take("claude", CLAUDE_A) if m["kind"] == "notice"]
        self.assertEqual(len(told), 1)
        self.assertIn(REL, told[0]["text"])
        self.assertIn("peer.break", self.audit_text())

    def test_concurrent_claims_have_one_winner(self):
        self.three_sessions()
        jobs = {role: {"do": "claim", "resource": REL, "now": self.now, "sender": sender(sid, agent)}
                for role, agent, sid in (("a", "claude", CLAUDE_A), ("b", "codex", CODEX_A),
                                         ("c", "claude", CLAUDE_B))}
        outs = self.processes(jobs)
        self.assertFalse([o.get("error") for o in outs.values() if o.get("error")], outs)
        self.assertEqual(sorted(str(o.get("claim") or o.get("code")) for o in outs.values()), ["3", "3", "held"])
        winner = next(r for r, o in outs.items() if o.get("claim") == "held")
        key = {"a": f"claude:{CLAUDE_A}", "b": f"codex:{CODEX_A}", "c": f"claude:{CLAUDE_B}"}[winner]
        self.assertEqual(peers.holder(REL, now=self.now)["key"], key)


class EscalationTests(PeerCase):
    def test_escalate_and_settle_from_prompt_only(self):
        a, b, c = self.three_sessions()
        q = peers.send(sender(CLAUDE_A), "@0199a213-81", "Who releases first?", kind="question", now=self.now)
        e = peers.escalate(sender(CLAUDE_A), q["id"], "Which project releases 26.11.9 first?",
                           ["A: devkit first", "B: kit-consulting first"], recommend="A", now=self.now + 60)
        self.assertRegex(e["id"], r"^E-[0-9a-z]{8}$")
        self.assertEqual(sorted(e["parties"]), sorted([a, b]))
        self.assertTrue(any(e["id"] in text for _, text in self.notified), self.notified)
        self.assertEqual([x["id"] for x in peers.escalations("open", me=b)], [e["id"]])
        self.assertEqual(peers.escalations("open", me=c), [])
        self.assertEqual([m["kind"] for m in peers.take("codex", CODEX_A)], ["question", "escalation"])
        with self.assertRaises(peers.PeerError) as err:
            peers.escalate(sender(CLAUDE_A, lane="a1"), "@0199a213-81", "?", ["A: x", "B: y"], now=self.now)
        self.assertEqual(err.exception.code, 2)
        peers.send(sender(CODEX_A, "codex"), "@4d1cffc7", f"settle {e['id']}: B", now=self.now + 70)
        peers.answer(sender(CODEX_A, "codex"), q["id"], f"settle {e['id']}: B", now=self.now + 71)
        self.assertEqual([x["id"] for x in peers.escalations("open")], [e["id"]])  # a session's words settle nothing
        self.session("claude", "9c0ffee0-0000-4000-8000-0000000000aa", surface="claude -p")  # nobody at the keyboard
        self.assertEqual(peers.settle_from_prompt("claude", "9c0ffee0-0000-4000-8000-0000000000aa",
                                                  [f"settle {e['id']}: A"], "p-0", now=self.now + 75), [])
        done = peers.settle_from_prompt("claude", CLAUDE_B, ["thanks", f"SETTLE {e['id'].upper()}: b"], "p-1",
                                        now=self.now + 80)  # any devkit session, case-insensitive
        self.assertEqual([(x["id"], x["settled"]["choice"], x["settled"]["prompt_uuid"]) for x in done],
                         [(e["id"], "B", "p-1")])
        self.assertEqual(peers.escalations("open"), [])
        self.assertEqual(peers.escalations("settled", me=a)[0]["settled"]["text"], "B: kit-consulting first")
        for agent, sid in (("claude", CLAUDE_A), ("codex", CODEX_A)):
            settled = [m for m in peers.take(agent, sid) if m["kind"] == "settled"]
            self.assertEqual(len(settled), 1, (agent, settled))
            self.assertIn("kit-consulting first", settled[0]["text"])
        self.assertIn("peer.settle", self.audit_text())

    def test_settle_replay_refused(self):
        self.three_sessions()
        e1 = peers.escalate(sender(CLAUDE_A), "@0199a213-81", "Rebase or merge?", ["A: rebase", "B: merge"],
                            now=self.now)
        e2 = peers.escalate(sender(CLAUDE_A), "@0199a213-81", "Tag now or later?", ["A: now", "B: later"],
                            now=self.now + 1)
        self.assertEqual(len(peers.settle_from_prompt("claude", CLAUDE_A, [f"settle {e1['id']}: A"], "p-9",
                                                      now=self.now + 10)), 1)
        self.assertEqual(peers.settle_from_prompt("claude", CLAUDE_A, [f"settle {e2['id']}: A"], "p-9",
                                                  now=self.now + 20), [])  # the same prompt fed back
        self.assertEqual(peers.settle_from_prompt("claude", CLAUDE_A, [f"settle {e1['id']}: B"], "p-10",
                                                  now=self.now + 30), [])  # settled already
        self.assertEqual([x["id"] for x in peers.escalations("open")], [e2["id"]])
        free = peers.settle_from_prompt("codex", CODEX_A, [f"settle {e2['id']}: wait until Friday"], "p-11",
                                        now=self.now + 40)
        self.assertEqual((free[0]["settled"]["choice"], free[0]["settled"]["text"]), ("", "wait until Friday"))
        self.assertEqual(peers.escalations("settled", me=f"claude:{CLAUDE_A}")[0]["id"], e1["id"])
        self.assertEqual(peers.escalations("open"), [])


if __name__ == "__main__":
    unittest.main()
