# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# Continuity through compaction, resume and /clear (lifecycle spec C12, "Continuity module contract", and the
# compliance review's change 5): lib/devkit/continuity.py on the payloads Claude Code 2.1.319 really sent
# (tests/fixtures/hooks/claude/) and the codex 0.166 schemas (tests/fixtures/hooks/codex/), with devkit's state in a
# temporary folder and devkit's default configuration (never the developer's).
# Run: python3 -m unittest tests.test_continuity
import argparse
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / "lib"
sys.path.insert(0, str(LIB))

from devkit import audit, core, crew, hooks, notes, state, ui, work  # noqa: E402
from devkit import continuity as ct  # noqa: E402
from devkit import progress as pg  # noqa: E402

sys.path.insert(0, str(TESTS))
from test_progress import boundary  # noqa: E402

CLAUDE = TESTS / "fixtures" / "hooks" / "claude"
CODEX = TESTS / "fixtures" / "hooks" / "codex"
COMPACT = CLAUDE / "transcript" / "compact.jsonl"  # the two entries a manual /compact wrote
SID = "8f6d96ec-ae34-4d6f-9063-b0e8e1b00a57"  # the session of the compaction fixtures
NEW = "066f9d9e-136c-4cba-a95c-cd0dc3952536"  # the session /clear started (SessionStart.clear)
T0 = 1793996689.88  # 2026-11-06T20:24:49.880Z: when the fixture's compaction wrote its boundary
MEMBER = "aaf06cf734666aa86"  # the agent_id of PostToolUse.Agent.in-subagent
STATUS = ("**[Firefly> DevKit]** api · `release/26.11.3` · Release 26.11.3\n- **Done:** merged the API branch.\n"
          "- **Check:** ✓ 214 tests\n- **Next:** Tank finishes the handlers.")

# A member's hook in its own process: the boundary in its own transcript, then its PostToolUse (spec review focus,
# "two members compacting at once, in two processes").
CHILD = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root, payload = Path(sys.argv[2]), json.loads(sys.argv[3])
from devkit import audit, continuity as ct, core, crew, hooks, progress as pg
pg.RUNS, pg.SESSIONS, pg.TIMING = root / "runs", root / "sessions", root / "timing"
ct.CONT, audit.AUDIT, hooks.STATE = root / "continuity", root / "audit", root
audit.policy_file = lambda: root / "no-policy.json"
crew.overlay_path = lambda: root / "no-crew.json"
core.CFG.update(audit={}, progress={}, work={})
(root / f"ready-{payload['agent_id']}").touch()
while not (root / "go").exists():
    time.sleep(0.005)
print(ct.on_tool("claude", payload, now=float(sys.argv[4])))
"""


def fixture(name: str) -> dict:
    d = json.loads((CLAUDE / f"{name}.json").read_text())
    if name == "PostCompact.manual":  # the capture keeps 200 characters of the summary; the spike's whole one:
        d["compact_summary"] = (CLAUDE / "transcript" / "compact-summary.txt").read_text()
    return d


def line(obj) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def boundary_line(uuid, ts="2026-11-06T20:24:49.880Z", pre=20490, post=4080, trigger="auto", agent_id=None,
                  meta=True) -> bytes:
    d = {"type": "system", "subtype": "compact_boundary", "uuid": uuid, "timestamp": ts,
         "content": "Conversation compacted", "level": "info"}
    if meta:
        d["compactMetadata"] = {"trigger": trigger, "preTokens": pre, "postTokens": post,
                                "cumulativeDroppedTokens": pre - post, "durationMs": 4865}
    if agent_id:
        d.update(agentId=agent_id, isSidechain=True)
    return line(d)


def chatter(n=3, text="working on it") -> bytes:
    return b"".join(line({"type": "assistant", "uuid": f"u{i}", "message": {"content": text}}) for i in range(n))


class Base(unittest.TestCase):
    # devkit's state in a temporary folder; the session claude:<SID> of the fixtures, in a folder that is no git
    # repository (git answers nothing, so the records' own branch is what the brief shows)
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = [mock.patch.object(pg, "RUNS", self.root / "runs"),
                  mock.patch.object(pg, "SESSIONS", self.root / "sessions"),
                  mock.patch.object(pg, "TIMING", self.root / "timing"),
                  mock.patch.object(pg, "PENDING", self.root / "timing" / "pending"),
                  mock.patch.object(pg, "_publish", lambda: None),
                  mock.patch.object(ct, "CONT", self.root / "continuity"),
                  mock.patch.object(audit, "AUDIT", self.root / "audit"),
                  mock.patch.object(audit, "policy_file", lambda: self.root / "no-policy.json"),
                  mock.patch.object(hooks, "STATE", self.root),
                  mock.patch.object(crew, "overlay_path", lambda: self.root / "no-crew.json"),
                  mock.patch.object(work, "ITEMS", self.root / "items"),
                  mock.patch.dict(core.CFG, {"audit": {}}),
                  *boundary(self.root),
                  mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{SID}", "DEVKIT_NO_NOTIFY": "1"})]
        for x in self.p:
            x.start()
        self.cwd = self.root / "repo"
        self.cwd.mkdir()
        self.tx = self.root / "projects" / f"{SID}.jsonl"
        self.tx.parent.mkdir()
        self.tx.write_bytes(chatter())
        self.session(SID)

    def tearDown(self):
        for x in reversed(self.p):
            x.stop()
        self.tmp.cleanup()

    def session(self, sid, **kv):
        pg.SESSIONS.mkdir(parents=True, exist_ok=True)
        rec = {"agent": "claude", "session": sid, "cwd": str(self.cwd), "top": "", "project": "acme/api",
               "branch": "release/26.11.3", "head": "abc1234", "status": STATUS, "subagents": {}, "launches": {},
               **kv}
        state.replace(pg.session_path("claude", sid), rec)

    def payload(self, name, sid=SID, **kw) -> dict:
        d = fixture(name)
        d.update(session_id=sid, transcript_path=str(self.tx), cwd=str(self.cwd))
        d.update(kw)
        return d

    def make_run(self, sid=SID, started=T0 - 600, lanes=("Tank", "Neo"), **kw):
        agents, plan = {}, [{"name": "design", "state": "done", "owner": "main", "after": [], "est": 30}]
        for name, step, scope in (("Tank", "api", "services/**"), ("Neo", "ui", "web/**"),
                                  ("Kid", "tooling", "lib/devkit/**")):
            aid = MEMBER if name == "Kid" else f"a-{name.lower()}"
            on = name in lanes
            agents[aid] = {"label": f"{name}: the {step}", "persona": name, "type": "general-purpose",
                           "state": "running" if on else "done", "started": started, "binding": "exact",
                           "updates": [{"ts": started + 60, "text": f"{step}: contract written, handlers next"}]}
            plan.append({"name": step, "state": "running" if on else "done", "owner": aid, "after": ["design"],
                         "est": 60, "scope": [scope]})
        plan += [{"name": "review", "state": "pending", "owner": "main", "after": ["api", "ui"], "est": 20},
                 {"name": "docs", "state": "pending", "owner": "main", "after": ["design"], "est": 20}]
        run = {"v": 2, "agent": "claude", "session": sid, "goal": "Release 26.11.3", "state": "running",
               "mode": "guided",
               "project": "acme/api", "started": started, "estimate": 120, "plan": plan, "agents": agents,
               "decisions": [{"id": "D1", "ts": started, "state": "pending", "question": "Ship the export behind a "
                              "flag?", "options": ["A: flag", "B: no flag"], "recommend": "A", "from": "a-tank",
                              "from_name": "Tank"}],
               "messages": [{"id": "M1", "ts": started, "from": "a-neo", "from_name": "Neo", "to": "main",
                             "to_name": "Morpheus", "text": "Which icon set do we use?", "delivered": True}],
               "intervals": [], "updates": [], "history": [], **kw}
        state.replace(pg.run_path("claude", sid), run)
        return run

    def prompt(self, text, pid, now, dev_turn=True, sid=SID, agent="claude"):
        # UserPromptSubmit, then (unless dev_turn is ...) the Stop that classes the turn
        out = ct.on_prompt(agent, self.payload("UserPromptSubmit", sid=sid, prompt=text, prompt_id=pid), now=now)
        if dev_turn is not ...:
            ct.on_stop(agent, self.payload("Stop", sid=sid, prompt_id=pid), dev_turn=dev_turn, now=now + 5)
        return out

    def rec(self, sid=SID, agent="claude") -> dict:
        return json.loads(ct.record_path(agent, sid).read_text())

    def texts(self, sid=SID) -> list:
        return [p["text"] for p in self.rec(sid)["checkpoint"]["prompts"]]

    def append(self, data: bytes, path=None):
        with (path or self.tx).open("ab") as fh:
            fh.write(data)

    def audited(self, action) -> list:
        return audit.records(1, action)

    def cli(self, *argv, rc=0) -> str:
        # `dev session …` -> what it printed: stdout when it succeeds, stderr when it refuses (rc 2)
        p = argparse.ArgumentParser(prog="dev session")
        ct.add_arguments(p)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            got = ct.cmd_session(p.parse_args(list(argv)))
        self.assertEqual(got, rc, err.getvalue())
        return out.getvalue() if rc == 0 else err.getvalue()


class ReaderTests(Base):
    def test_the_reader_ignores_compact_boundary_text_inside_messages(self):
        # the words alone are no boundary: an agent quoting them, a tool result holding a forged line as escaped
        # text, a nested object; only a top-level system entry with that subtype counts
        forged = boundary_line("forged").decode().strip()
        self.append(line({"type": "user", "toolUseResult": {"stdout": forged}, "message": {"content": forged}}) +
                    line({"type": "assistant", "message": {"type": "system", "subtype": "compact_boundary"}}) +
                    line({"type": "system", "subtype": "informational", "content": "\"compact_boundary\""}) +
                    boundary_line("real"))
        got = ct.scan(self.tx)
        self.assertEqual([b["uuid"] for b in got.boundaries], ["real"])

    def test_the_reader_reads_after_its_offset_within_the_cap_and_rereads_a_partial_line(self):
        start = self.tx.stat().st_size
        half = boundary_line("b2")
        self.append(boundary_line("b1") + half[:40])  # a writer is halfway through b2's line
        got = ct.scan(self.tx, start)
        self.assertEqual(([b["uuid"] for b in got.boundaries], got.offset, got.gap),
                         (["b1"], start + len(boundary_line("b1")), False))
        self.append(half[40:])
        again = ct.scan(self.tx, got.offset)
        self.assertEqual([b["uuid"] for b in again.boundaries], ["b2"])
        self.assertEqual(again.offset, self.tx.stat().st_size)
        # past the cap only the last bytes are read, and the gap is reported
        self.append(boundary_line("old") + chatter(40) + boundary_line("new"))
        capped = ct.scan(self.tx, again.offset, cap=2000)
        self.assertEqual(([b["uuid"] for b in capped.boundaries], capped.gap), (["new"], True))
        # a file smaller than the offset was rewritten: the read starts again at 0
        self.tx.write_bytes(boundary_line("rewritten"))
        self.assertEqual([b["uuid"] for b in ct.scan(self.tx, 10 ** 6).boundaries], ["rewritten"])

    def test_the_reader_skips_broken_lines_and_boundaries_without_metadata(self):
        self.append(b'{"type": "system", "subtype": "compact_boundary", \xff\xfe broken}\n' +
                    b'not json "compact_boundary" at all\n' + boundary_line("bare", meta=False) +
                    boundary_line("full", pre=967000, post=61000))
        got = {b["uuid"]: b for b in ct.scan(self.tx).boundaries}
        self.assertEqual(sorted(got), ["bare", "full"])
        self.assertEqual((got["bare"]["pre_tokens"], got["bare"]["post_tokens"]), (None, None))
        self.assertEqual((got["full"]["pre_tokens"], got["full"]["post_tokens"], got["full"]["trigger"]),
                         (967000, 61000, "auto"))

    def test_a_boundary_seen_only_in_the_transcript_is_recorded_late_and_briefed_at_the_next_hook(self):
        self.make_run()
        ct.on_session_start("claude", self.payload("SessionStart.startup"), now=T0 - 900)
        self.append(boundary_line("late-1", pre=967000, post=61000))  # no PreCompact, no SessionStart(compact)
        first = ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 30)
        self.assertIn("compacted (auto, 1st time; 967k → 61k tokens) and no devkit hook saw it", first)
        self.assertIn("Tank", first)
        self.assertEqual(ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 40), "")
        (c,) = self.rec()["compactions"]
        self.assertEqual((c["late"], c["seen"], c["boundary"], c["who"]), (True, ["boundary"], "late-1", "main"))
        self.assertEqual([(r["outcome"], r["ref"]["result"]) for r in self.audited("session.compact")],
                         [("recorded", "late")])

    def test_a_boundary_read_twice_counts_once(self):
        ct.on_session_start("claude", self.payload("SessionStart.startup"), now=T0 - 900)
        saved = self.rec()["offsets"]["main"]
        self.append(boundary_line("once"))
        ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 30)
        state.update(ct.record_path("claude", SID), lambda r: r["offsets"].update(main=saved))  # the same bytes again
        ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 40)
        self.assertEqual([c["boundary"] for c in self.rec()["compactions"]], ["once"])
        self.assertEqual(len(self.audited("session.compact")), 1)
        # past the 20 entries a record keeps, a rewritten transcript read again from 0 still counts nothing twice
        self.append(b"".join(boundary_line(f"b{i}", ts=f"2026-11-07T{i:02d}:00:00.000Z") for i in range(24)))
        ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 50)
        self.assertEqual(len(self.rec()["compactions"]), 20)
        state.update(ct.record_path("claude", SID), lambda r: r["offsets"].update(main=0))
        ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 60)
        self.assertEqual(len(self.audited("session.compact")), 25)
        # a transcript rewritten smaller, with no boundary: the offset starts again there and is saved
        self.tx.write_bytes(chatter(2))
        ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 70)
        self.assertEqual(self.rec()["offsets"]["main"], self.tx.stat().st_size)

    @unittest.skipUnless(os.environ.get("DEVKIT_A10_TRANSCRIPT"), "set DEVKIT_A10_TRANSCRIPT to a real transcript")
    def test_dry_run_counts_this_sessions_compactions(self):
        # A10: the real transcript of the session that designed this (5 main compactions, checked by hand); nothing
        # of it is copied into the tests, and the dry run writes nothing
        out = self.cli("compactions", os.environ["DEVKIT_A10_TRANSCRIPT"])
        self.assertIn("5 compaction(s)", out)
        self.assertFalse(ct.CONT.exists())


class CheckpointTests(Base):
    def test_pre_compact_writes_a_checkpoint_with_the_run_lanes_decisions_and_last_five_prompts(self):
        self.make_run()
        for i in range(1, 6):
            self.prompt(f"request {i}: fix the export", f"p{i}", T0 - 600 + i * 60)
        self.prompt("request 6: and ship it", "p6", T0 - 30, dev_turn=...)  # mid-turn: Stop has not classed it
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 10)
        r = self.rec()
        cp = r["checkpoint"]
        self.assertEqual((cp["reason"], cp["run"]["goal"], cp["run"]["current"]),
                         ("pre-compact", "Release 26.11.3", "api"))
        self.assertEqual([(s["name"], s["state"], s["owner"]) for s in cp["run"]["steps"]][:3],
                         [("design", "done", "main"), ("api", "running", "Tank"), ("ui", "running", "Neo")])
        self.assertEqual([(x["persona"], x["role"], x["step"], x["scope"]) for x in cp["lanes"]],
                         [("Tank", "Backend engineer", "api", ["services/**"]),
                          ("Neo", "Senior engineer", "ui", ["web/**"])])
        self.assertEqual([(d["id"], d["from_name"], d["recommend"]) for d in cp["decisions"]], [("D1", "Tank", "A")])
        self.assertEqual([(m["id"], m["from_name"]) for m in cp["messages"]], [("M1", "Neo")])
        self.assertEqual(self.texts(),
                         [f"request {i}: fix the export" for i in range(2, 6)] + ["request 6: and ship it"])
        self.assertEqual([bool(p.get("pending")) for p in cp["prompts"]], [False] * 4 + [True])
        self.assertEqual((cp["git"]["branch"], cp["status"]), ("release/26.11.3", STATUS))
        (c,) = r["compactions"]
        self.assertEqual((c["trigger"], c["who"], c["seen"], c["prompt_id"], c["custom"], c["late"]),
                         ("manual", "main", ["pre"], fixture("PreCompact.manual")["prompt_id"], False, False))
        self.assertEqual([x["outcome"] for x in self.audited("session.compact")], ["ok"])

    def test_the_checkpoint_keeps_only_the_messages_that_still_wait_for_an_answer(self):
        # a resumed session's brief listed notices and questions already answered as open messages
        self.make_run(messages=[
            {"id": "M1", "ts": T0 - 600, "from": "a-neo", "from_name": "Neo", "to": "main", "text": "Which icon set?",
             "delivered": True},
            {"id": "M2", "ts": T0 - 500, "from": "devkit", "from_name": "devkit", "to": "main", "kind": "notice",
             "text": "Neo started as “Neo: the ui”", "delivered": True},
            {"id": "M3", "ts": T0 - 400, "from": "a-tank", "from_name": "Tank", "to": "main", "text": "Paginate?",
             "delivered": True},
            {"id": "M4", "ts": T0 - 300, "from": "main", "from_name": "Morpheus", "to": "a-tank", "reply_to": "M3",
             "text": "Yes, by cursor"}])
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 10)
        self.assertEqual([m["id"] for m in self.rec()["checkpoint"]["messages"]], ["M1"])

    def test_a_secret_in_a_prompt_never_reaches_the_checkpoint_which_is_0600_and_at_most_16_kb(self):
        # fake secrets, put together at run time so that no secret scanner finds one in this file
        key = "PRIVATE " + "KEY-----"
        pem = f"-----BEGIN RSA {key}\nMIIEowIBAAKCAQEA7PEMBODYSECRETxx\nQWERTYPEMBODYSECRET2\n-----END RSA {key}"
        jwt = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiJKV1RTRUNSRVQifQ", "c2lnbmF0dXJlSldUU0VDUkVU"])
        self.prompt("push with ghp_" + "Q" * 36 + " please", "s1", T0 - 300)
        self.prompt("curl -H 'Authorization: " + "Bear" + "er abcdefghijklmnop1234' https://api", "s2", T0 - 280)
        self.prompt(f"the token is {jwt} and the key:\n{pem}\nuse them", "s3", T0 - 260)
        self.prompt("x" * 985 + " ghp_" + "Z" * 36, "s4", T0 - 240)  # a cut at 1,000 would leave part of it
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 200)
        text = ct.record_path("claude", SID).read_text()
        for leak in ("Q" * 8, "abcdefghijklmnop1234", "JWTSECRET", "eyJzdWIi", "PEMBODYSECRET", "Z" * 5):
            self.assertNotIn(leak, text)
        self.assertIsNone(audit.SECRET.search("\n".join(self.texts())))
        self.assertEqual(stat.S_IMODE(ct.record_path("claude", SID).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(ct.CONT.stat().st_mode), 0o700)
        # five prompts of 1,000 non-Latin characters: as written (escaped JSON) still within 16 KB, the newest whole
        self.make_run()
        for i in range(5):
            self.prompt(("会議の議事録を日本語でまとめて、" * 70)[:999] + str(i), f"j{i}", T0 + i)
        ct.on_pre_compact("claude", self.payload("PreCompact.manual", prompt_id="p-big"), now=T0 + 10)
        cp = self.rec()["checkpoint"]
        self.assertLessEqual(len(json.dumps(cp).encode()), 16384)
        self.assertEqual((len(cp["prompts"][-1]["text"]), cp["prompts"][-1]["text"][-1]), (1000, "4"))
        self.assertEqual(len(cp["prompts"]), 5)

    def test_two_compactions_in_one_turn_are_two_records(self):
        pre, post = self.payload("PreCompact.manual"), self.payload("PostCompact.manual")
        ct.on_pre_compact("claude", pre, now=T0 - 10)
        ct.on_post_compact("claude", post, now=T0 + 5)
        ct.on_pre_compact("claude", pre, now=T0 + 60)  # same prompt_id: PostCompact closed the first
        cs = self.rec()["compactions"]
        self.assertEqual([(c["prompt_id"], c["closed"]) for c in cs],
                         [(pre["prompt_id"], True), (pre["prompt_id"], False)])
        self.assertEqual(len(self.audited("session.compact")), 2)

    def test_a_prompt_is_kept_only_when_stop_classes_its_turn_as_development(self):
        # compliance review, change 5: at UserPromptSubmit the class is still the previous turn's, so the prompt
        # waits as pending; Stop keeps it for a development turn, drops it otherwise and when the classifier failed
        self.prompt("fix the login timeout in `auth/session.py`", "d1", T0, dev_turn=...)
        self.assertEqual(self.rec()["checkpoint"]["prompts"][0]["pending"], True)
        ct.on_stop("claude", self.payload("Stop", prompt_id="d1"), dev_turn=True, now=T0 + 5)
        self.prompt("what's a good pasta recipe?", "r1", T0 + 10, dev_turn=False)
        self.prompt("and the wine?", "r2", T0 + 20, dev_turn=None)  # scope.decide raised
        self.assertEqual(self.texts(), ["fix the login timeout in `auth/session.py`"])
        self.assertNotIn("pending", self.rec()["checkpoint"]["prompts"][0])
        # a turn no Stop ended was never classed: its prompt goes when the next prompt arrives
        self.prompt("a recipe for the weekend", "r3", T0 + 30, dev_turn=...)
        self.prompt("now bump the version in `pyproject.toml`", "d2", T0 + 40)
        self.assertEqual(self.texts(), ["fix the login timeout in `auth/session.py`",
                                        "now bump the version in `pyproject.toml`"])

    def test_expire_deletes_records_unchanged_for_seven_days_and_old_clear_links(self):
        now = time.time()
        for sid, age in (("old", 8 * 86400), ("recent", 6 * 86400)):
            state.replace(ct.record_path("claude", sid), {"session": sid})
            os.utime(ct.record_path("claude", sid), (now - age, now - age))
        for cwd, age in (("/a", 11 * 60), ("/b", 5 * 60)):
            state.replace(ct.clear_path("claude", cwd), {"old_sid": "x", "cwd": cwd, "ts": now - age})
            os.utime(ct.clear_path("claude", cwd), (now - age, now - age))
        self.assertEqual(ct.expire(now=now), 2)
        self.assertEqual(sorted(p.name for p in ct.CONT.glob("*.json")),
                         sorted([ct.record_path("claude", "recent").name, ct.clear_path("claude", "/b").name]))


class BriefTests(Base):
    def test_session_start_compact_gives_the_brief_with_running_lanes_and_do_not_relaunch(self):
        self.make_run()
        self.prompt("Release 26.11.3 once Tank's API is in; keep `release/26.11.3` green", "p1", T0 - 300)
        self.prompt("and tell me before you push", "p2", T0 - 200)
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 10)
        self.append(COMPACT.read_bytes())  # what the compaction wrote
        text = ct.on_session_start("claude", self.payload("SessionStart.compact"), now=T0 + 2)
        self.assertTrue(text.startswith("devkit · this conversation was compacted (manual, 1st time; 20k → 4k "
                                        "tokens). devkit's record wins over the summary:"), text[:200])
        self.assertLess(text.index("and tell me before you push"), text.index("keep `release/26.11.3` green"))
        for part in ("Tank", "step api", "Neo", "step ui", "do not relaunch them", "SendMessage continues one",
                     "D1", "M1 from Senior engineer (Neo)", "Release 26.11.3", "current step api",
                     "your final message starts with the status block"):
            self.assertIn(part, text)
        (c,) = self.rec()["compactions"]
        self.assertEqual((c["seen"], c["boundary"], c["pre_tokens"], c["post_tokens"], c["late"]),
                         (["pre", "start"], "831f177d-3f3c-4c39-9f9c-944db9c09ee1", 20490, 4080, False))

    def test_the_brief_stays_under_4_kb_and_drops_in_order(self):
        # a crowded record: 8 lanes, 30 steps, 6 decisions with long questions, 6 long crew messages, 5 long
        # requests. The header, the crew, the decision ids, the current step, the rule and the newest request stay;
        # message texts give way before any request does (the flood in the threat model)
        run = self.make_run()
        for i in range(6):
            run["agents"][f"a-x{i}"] = dict(run["agents"]["a-tank"], persona=f"Helper{i}", label=f"Helper{i}: x")
            run["decisions"].append({"id": f"D{i + 2}", "state": "pending", "from_name": "Neo",
                                     "question": "QUESTIONTEXT " * 60, "options": []})
            run["messages"].append({"id": f"M{i + 2}", "from_name": "Tank", "to": "main", "text": "FLOODTEXT " * 150})
        run["plan"] += [{"name": f"step {i}", "state": "pending", "owner": "main", "after": [], "est": 5}
                        for i in range(25)]
        state.replace(pg.run_path("claude", SID), run)
        for i in range(5):
            self.prompt(f"REQ{i} " + "please check the export flow end to end " * 25, f"p{i}", T0 - 300 + i)
        newest = self.texts()[-1]
        text = ct.brief("claude", SID, reason="compact", now=T0)
        self.assertLessEqual(len(text.encode()), 4096)
        self.assertIn(newest, text)
        self.assertNotIn("FLOODTEXT", text)
        for part in ["this conversation was compacted", "do not relaunch them", "current step api",
                     "your final message starts with the status block"] + \
                [f"Helper{i}" for i in range(6)] + [f"D{i}" for i in range(1, 8)] + [f"M{i}" for i in range(1, 8)]:
            self.assertIn(part, text)

    def test_resume_after_session_end_gives_the_brief(self):
        self.make_run()
        self.prompt("finish the release notes for 26.11.3", "p1", T0 - 300)
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 10)
        ct.on_session_end("claude", self.payload("SessionEnd", reason="other"), now=T0 + 60)
        state.remove(pg.session_path("claude", SID))  # what `dev sync --finish` does at every SessionEnd
        self.session(SID)  # SessionStart registers the session again before continuity runs
        text = ct.on_session_start("claude", self.payload("SessionStart.resume.after-compact"), now=T0 + 3600)
        self.assertTrue(text.startswith("devkit · this session was resumed"), text[:120])
        for part in ("finish the release notes for 26.11.3", "Tank", "do not relaunch them", "D1"):
            self.assertIn(part, text)
        self.assertEqual(state.read(pg.session_path("claude", SID))["compactions"]["count"], 1)

    def test_resume_without_a_record_gives_the_plain_context(self):
        self.assertEqual(ct.on_session_start("claude", self.payload("SessionStart.resume.after-compact"), now=T0), "")
        self.assertEqual(ct.on_session_start("claude", self.payload("SessionStart.resume"), now=T0), "")

    def test_pre_compact_tells_the_summarizer_what_to_keep_and_codex_gets_nothing(self):
        self.make_run()
        out = ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0)
        self.assertLessEqual(len(out), 2000)
        for part in ("Backend engineer (Tank) on step api", "Senior engineer (Neo) on step ui", "D1", "M1",
                     "release/26.11.3", "verbatim"):
            self.assertIn(part, out)
        codex = {"session_id": "c1", "turn_id": "t1", "transcript_path": None, "cwd": str(self.cwd),
                 "model": "gpt-5.5", "trigger": "auto", "hook_event_name": "PreCompact"}
        self.assertEqual(ct.on_pre_compact("codex", codex, now=T0), "")


class NotInTheSummaryTests(Base):
    def test_a_request_the_summary_leaves_out_is_listed_once(self):
        self.make_run(lanes=())
        self.prompt("Remember: my favorite fruit is mango. Reply OK.", "p1", T0 - 300)
        self.prompt("Then cut the release from `release/26.11.3` and tag it", "p2", T0 - 200)
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 10)
        ct.on_post_compact("claude", self.payload("PostCompact.manual"), now=T0 + 5)
        got = self.prompt("go on", "p3", T0 + 60, dev_turn=...)
        self.assertIn("Not in the summary: re-read these from devkit's record:", got)
        self.assertIn("«Then cut the release from `release/26.11.3` and tag it» (missing `release/26.11.3`)", got)
        self.assertNotIn("mango", got)
        self.assertLessEqual(len(got.encode()), 1024)
        self.assertNotIn("Not in the summary", ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 70))
        self.assertNotIn("Not in the summary", self.prompt("next", "p4", T0 + 80))

    def test_not_in_the_summary_is_language_neutral(self):
        # no vocabulary: anchors from structure (code, quotes, paths, digits, identifiers) and character 4-grams.
        # A translation counts as missing, on purpose: the verbatim request wins over a paraphrase
        en = "Rename the CSV button to “Export invoices” and keep the old route"
        es = "Revisa `config/crew.json` y dime qué miembros no tienen procedimiento"
        ja = "リリースノートを日本語で書いてください。バージョンは 26.11.3 です。"
        items = [{"label": "en", "text": en}, {"label": "es", "text": es}, {"label": "ja", "text": ja}]
        both = f"<analysis>{en} {es} {ja}</analysis><summary>Nothing was asked.</summary>"
        self.assertEqual(len(ct.missing(both, items)), 3)  # the analysis block is not the summary
        verbatim = f"<summary>1. Requests:\n - {en}\n - {es}\n - {ja}</summary>"
        self.assertEqual(ct.missing(verbatim, items), [])
        self.assertEqual(len(ct.missing("<summary>Unrelated work on the dashboard.</summary>", items)), 3)
        translated = ("<summary>The user wants the CSV button relabeled for exporting invoices while the previous "
                      "route stays, asked to check config/crew.json for members without a procedure, and to write "
                      "the release notes for 26.11.3 in Japanese.</summary>")
        got = ct.missing(translated, items)
        self.assertEqual([g.split(" ", 1)[0] for g in got], ["en", "es", "ja"])
        self.assertIn("(missing `Export invoices`)", got[0])
        # nearly every 4-gram is there, but the version changed: the anchor decides
        tag = [{"label": "tag", "text": "Tag the release 26.11.3 after the merge and publish the notes"}]
        self.assertEqual(ct.missing("<summary>Tag the release 26.11.3 after the merge and publish the notes</summary>",
                                    tag), [])
        self.assertEqual(len(ct.missing("<summary>Tag the release 26.11.2 after the merge and publish the notes"
                                        "</summary>", tag)), 1)

    def test_missing_running_members_and_decision_ids_are_listed(self):
        self.make_run()
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0 - 10)
        summary = "<summary>Neo is building the UI; decision D12 and message M1 are open.</summary>"
        ct.on_post_compact("claude", self.payload("PostCompact.manual", compact_summary=summary), now=T0 + 5)
        got = ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 30)
        self.assertIn("Backend engineer (Tank) on step api", got)
        self.assertIn("decision D1", got)  # D12 is not D1
        self.assertNotIn("Senior engineer (Neo) on step ui", got)
        self.assertNotIn("message M1", got)


class MemberTests(Base):
    def setUp(self):
        super().setUp()
        self.make_run(lanes=("Tank", "Kid"))
        self.mtx = self.root / "projects" / SID / "subagents" / f"agent-{MEMBER}.jsonl"
        self.mtx.parent.mkdir(parents=True)
        self.mtx.write_bytes(line({"type": "user", "isSidechain": True, "agentId": MEMBER, "message": {
            "role": "user", "content": "Kid: build the continuity reader. Scope lib/devkit/**. ghp_" + "K" * 36}}))

    def hook(self, now):
        return ct.on_tool("claude", self.payload("PostToolUse.Agent.in-subagent"), now=now)

    def test_a_member_compaction_in_its_own_transcript_gives_it_its_brief_at_its_next_hook(self):
        self.assertEqual(self.hook(T0 - 60), "")
        self.append(boundary_line("m1", agent_id=MEMBER), self.mtx)
        got = self.hook(T0 + 30)
        for part in ("your own context was compacted", "Kid", "DevEx engineer", "step tooling", "lib/devkit/**",
                     "Measure", "Verify", "tooling: contract written, handlers next",
                     "«Kid: build the continuity reader.", "**[Firefly> DevKit · DevEx engineer (Kid)]**",
                     "Needs from the tech lead:"):
            self.assertIn(part, got)
        self.assertNotIn("K" * 8, got)
        self.assertLessEqual(len(got.encode()), 1536)
        self.assertEqual(self.hook(T0 + 40), "")
        (c,) = self.rec()["compactions"]
        self.assertEqual((c["who"], c["persona"], c["late"]), (MEMBER, "Kid", True))

    def test_a_member_pre_compact_prints_nothing(self):
        out = ct.on_pre_compact("claude", self.payload("PreCompact.manual", agent_id=MEMBER), now=T0 - 5)
        self.assertEqual(out, "")
        self.append(boundary_line("m1", agent_id=MEMBER), self.mtx)
        self.assertIn("your own context was compacted", self.hook(T0 + 30))
        (c,) = self.rec()["compactions"]
        self.assertEqual((c["who"], c["seen"], c["late"]), (MEMBER, ["pre", "boundary"], False))
        self.assertNotIn("checkpoint", self.rec())  # a member's PreCompact never writes the main checkpoint

    def test_a_member_compacting_twice_gets_its_reminder_each_time(self):
        self.hook(T0 - 60)
        self.append(boundary_line("m1", agent_id=MEMBER), self.mtx)
        self.assertIn("your own context was compacted", self.hook(T0 + 30))
        self.append(boundary_line("m2", ts="2026-11-06T21:24:49.880Z", agent_id=MEMBER), self.mtx)
        self.assertIn("your own context was compacted", self.hook(T0 + 3630))
        self.assertEqual(self.hook(T0 + 3640), "")
        self.assertEqual([c["boundary"] for c in self.rec()["compactions"]], ["m1", "m2"])

    def test_parallel_member_compactions_are_both_recorded(self):
        other = "abbbbbbbbbbbbbbbb"
        run = state.read(pg.run_path("claude", SID))
        run["agents"][other] = dict(run["agents"]["a-tank"], persona="Mouse", label="Mouse: probe")
        state.replace(pg.run_path("claude", SID), run)
        payloads = []
        for aid in (MEMBER, other):
            mtx = self.mtx.with_name(f"agent-{aid}.jsonl")
            self.append(boundary_line(f"b-{aid}", agent_id=aid), mtx) if mtx.exists() else \
                mtx.write_bytes(boundary_line(f"b-{aid}", agent_id=aid))
            payloads.append(self.payload("PostToolUse.Agent.in-subagent", agent_id=aid))
        env = {k: v for k, v in os.environ.items() if not k.startswith("DEVKIT_HOOK_CAPTURE")}
        procs = [subprocess.Popen([sys.executable, "-c", CHILD, str(LIB), str(self.root), json.dumps(p), str(T0)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for p in payloads]
        deadline = time.time() + 30
        while not all((self.root / f"ready-{p['agent_id']}").exists() for p in payloads) and time.time() < deadline:
            time.sleep(0.01)
        (self.root / "go").touch()
        outs = [p.communicate(timeout=30) for p in procs]
        for out, err in outs:
            self.assertIn(b"your own context was compacted", out, err.decode()[-800:])
        self.assertEqual(sorted(c["boundary"] for c in self.rec()["compactions"]), [f"b-{MEMBER}", f"b-{other}"])


class ClearTests(Base):
    def test_clear_links_the_new_session_by_cwd_and_gives_the_open_run_notice(self):
        self.make_run()
        self.prompt("SECRETPLAN rewrite the parser", "p1", T0 - 300)
        ct.on_session_end("claude", self.payload("SessionEnd.clear"), now=T0)
        self.assertTrue(ct.clear_path("claude", str(self.cwd)).exists())
        self.assertEqual(self.rec()["checkpoint"]["prompts"], [])  # /clear drops the old prompts (change 5)
        self.session(NEW)
        notice = ct.on_session_start("claude", self.payload("SessionStart.clear", sid=NEW), now=T0 + 2)
        self.assertLessEqual(len(notice.encode()), 800)
        for part in ("Release 26.11.3", "current step api", "Tank", "Neo", "dev session carry", "dev progress done"):
            self.assertIn(part, notice)
        self.assertNotIn("SECRETPLAN", notice)
        self.assertEqual(self.rec(NEW)["cleared_from"], SID)
        self.assertFalse(ct.clear_path("claude", str(self.cwd)).exists())
        # a link older than 10 minutes links nothing
        ct.on_session_end("claude", self.payload("SessionEnd.clear"), now=T0 + 10)
        self.assertEqual(ct.on_session_start("claude", self.payload("SessionStart.clear", sid="late"), now=T0 + 700),
                         "")

    def cleared(self, **kv):
        # the session NEW in acme/api, which /clear started in the conversation of SID (its continuity record links it)
        self.session(NEW, **kv)
        state.replace(ct.record_path("claude", NEW), {"agent": "claude", "session": NEW, "cleared_from": SID})

    def dead_pid(self) -> int:
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        return p.pid

    def test_carry_takes_a_run_only_from_a_session_that_ended_or_that_this_conversation_continues(self):
        # SID's process still runs, and neither /clear nor a compaction links it to NEW: its owner works on that run
        self.make_run(lanes=())
        self.session(SID, pid=os.getpid())
        self.session(NEW)
        with self.assertRaisesRegex(ValueError, rf"claude:{SID[:12]} is still live.*dev session tell @{SID[:8]} "):
            ct.carry("claude", SID, NEW, now=T0)
        self.assertEqual((pg.run_path("claude", SID).exists(), pg.run_path("claude", NEW).exists()), (True, False))
        for how, source, link in (("its process is gone", {"pid": self.dead_pid()}, False),
                                  ("/clear", {"pid": os.getpid()}, True),
                                  ("a compaction", {"pid_superseded": os.getpid(), "superseded_by": NEW}, False)):
            with self.subTest(how=how):
                for f in (pg.run_path("claude", NEW), ct.record_path("claude", NEW)):
                    if f.exists():
                        f.unlink()
                self.make_run(lanes=())
                self.session(SID, **source)
                self.cleared() if link else self.session(NEW)
                self.assertEqual(ct.carry("claude", SID, NEW, now=T0)["to"], NEW)

    def test_carry_moves_a_run_only_within_its_project_or_an_initiative_both_share(self):
        handoff = self.root / "handoff"
        handoff.mkdir()
        self.make_run(lanes=())
        self.session(SID, pid=self.dead_pid())
        self.session(NEW, project="firefly/kit-consulting")
        with mock.patch.object(core, "HANDOFF", handoff):
            with self.assertRaisesRegex(ValueError, "belongs to acme/api, and this session works in "
                                                    "firefly/kit-consulting"):
                ct.carry("claude", SID, NEW, now=T0)
            self.assertTrue(pg.run_path("claude", SID).exists())
            readme = handoff / "initiatives" / "billing" / "README.md"
            readme.parent.mkdir(parents=True)
            readme.write_text(notes.new_initiative("Billing", projects=["acme/api", "firefly/kit-consulting"]))
            self.assertEqual(ct.carry("claude", SID, NEW, now=T0)["to"], NEW)

    def test_carry_moves_the_run_and_is_refused_while_a_lane_runs(self):
        self.make_run()
        self.cleared()
        with self.assertRaisesRegex(ValueError, "Tank.*still running"):
            ct.carry("claude", SID, NEW, now=T0)
        run = state.read(pg.run_path("claude", SID))
        for lane in run["agents"].values():
            lane["state"] = "done"
        state.replace(pg.run_path("claude", SID), run)
        out = ct.carry("claude", SID, NEW, now=T0)
        self.assertEqual((out["from"], out["to"], out["goal"]), (SID, NEW, "Release 26.11.3"))
        self.assertFalse(pg.run_path("claude", SID).exists())
        moved = state.read(pg.run_path("claude", NEW))
        self.assertEqual((moved["session"], moved["carried_from"], len(moved["plan"])), (NEW, SID, 6))
        self.assertEqual(len(self.audited("session.carry")), 1)
        with self.assertRaisesRegex(ValueError, "no run"):
            ct.carry("claude", SID, NEW, now=T0)

    # `dev session carry --from claude-<id>` was refused with "no run to carry" while the bare id worked: devkit names
    # a session's runs and records `<agent>-<id>`, and people copy that form. Both forms name the same session, for
    # carry's --from and for the --session of brief and forget.
    def test_a_session_id_written_with_its_agent_prefix_names_the_same_session(self):
        self.make_run(lanes=())
        self.cleared()
        bare = self.cli("brief", "--session", f"claude:{SID}")
        self.assertIn("Release 26.11.3", bare)
        for form in (f"claude:claude-{SID}", f"claude-{SID}"):
            self.assertEqual(self.cli("brief", "--session", form), bare, form)
        with mock.patch.object(pg, "resolve_session", lambda *a, **k: ("claude", NEW, "env")):
            out = self.cli("carry", "--from", f"claude-{SID}")
        self.assertIn(f"moved from claude:{SID[:12]} to this session", out)
        self.assertEqual(state.read(pg.run_path("claude", NEW))["carried_from"], SID)
        self.assertIn("1 record(s)", self.cli("forget", "--session", f"claude:claude-{NEW}"))
        self.assertFalse(ct.record_path("claude", NEW).exists())

    def test_carry_takes_back_a_run_its_ended_session_left_orphaned(self):
        # the reconciler orphans the run of the session /clear ended; the session it is carried to works on it
        self.make_run()
        run = state.read(pg.run_path("claude", SID))
        for lane in run["agents"].values():
            lane["state"] = "done"
        run.update(state="orphaned", orphaned={"ts": round(T0 - 60), "why": "its session ended (clear)"})
        state.replace(pg.run_path("claude", SID), run)
        self.cleared()
        ct.carry("claude", SID, NEW, now=T0)
        moved = state.read(pg.run_path("claude", NEW))
        self.assertEqual((moved["state"], "orphaned" in moved), ("running", False))
        back = [h for h in moved["history"] if h["step"] == "(run)"][-1]
        self.assertEqual((back["from"], back["to"], back["by"]), ("orphaned", "running", "devkit"))
        self.assertIn(NEW[:12], back["why"])

    def test_the_summarizer_subagent_stop_is_recognized_and_writes_nothing(self):
        sess = state.read(pg.session_path("claude", SID))
        before = sorted(str(p) for p in self.root.rglob("*"))
        self.assertTrue(ct.is_summarizer("claude", fixture("SubagentStop.compact-summarizer"), sess))
        self.assertFalse(ct.is_summarizer("claude", fixture("SubagentStop"), sess))
        known = dict(sess, subagents={"aa4bf632657e825c3": {"type": ""}})
        self.assertFalse(ct.is_summarizer("claude", fixture("SubagentStop.compact-summarizer"), known))
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*")), before)


class CommandTests(Base):
    def setUp(self):
        super().setUp()
        self.make_run(started=time.time() - 630)  # 10.5 minutes: both renderings below say "10 min"
        self.prompt("Please rewrite the invoice exporter so it streams rows " + "and keeps memory flat " * 12,
                    "p1", time.time() - 120)
        self.prompt("then open the PR", "p2", time.time() - 60)

    def test_dev_session_brief_for_chat_and_devkit_resume_return_the_same_text(self):
        # devkit_resume (mcp.py, wiring step) runs exactly this command; the same provider sees the requests cut
        # to 200 characters, another provider (a new recipient) only their count and times
        out = self.cli("brief", "--latest", "--for-chat", "--project", "acme/api", "--provider", "claude")
        self.assertEqual(out.rstrip("\n"), ct.brief("claude", SID, reason="manual", chat=True, provider="claude"))
        self.assertIn("requests redacted and cut", out)
        self.assertIn("then open the PR", out)
        self.assertNotIn("and keeps memory flat " * 10, out)
        for other in (["--provider", "codex"], []):
            text = self.cli("brief", "--latest", "--for-chat", "--project", "acme/api", *other)
            self.assertNotIn("open the PR", text)
            self.assertIn("2 requests", text)
            self.assertIn("Tank", text)
        self.assertIn("and keeps memory flat " * 12, self.cli("brief", "--session", f"claude:{SID}"))

    def test_dev_session_forget_deletes_the_records_and_audits_counts_only(self):
        ct.on_session_end("claude", self.payload("SessionEnd.clear"), now=time.time())
        self.prompt("keep this one", "q1", time.time(), sid="other")
        self.assertIn("1 record(s), 1 clear link(s)", self.cli("forget", "--session", f"claude:{SID}"))
        self.assertFalse(ct.record_path("claude", SID).exists())
        self.assertTrue(ct.record_path("claude", "other").exists())
        self.cli("forget", "--all")
        self.assertEqual(list(ct.CONT.glob("*.json")), [])
        recs = self.audited("session.forget")
        self.assertEqual([r["detail"] for r in recs], ["1 record(s), 1 clear link(s)", "1 record(s), 0 clear link(s)"])
        self.assertNotIn("keep this one", json.dumps(recs))

    def test_a_member_cannot_read_the_brief_or_move_the_run(self):
        # the refusal is one line on stderr and exit 2, like the other dev commands, never a traceback (M342)
        lane = {"caller": "a-tank", "how": "ledger", "agent": "claude", "session": SID}
        with mock.patch.object(pg, "caller", lambda *a, **k: lane):
            for argv in (["brief"], ["brief", "--latest", "--project", "acme/api"], ["carry", "--from", "x"],
                         ["forget", "--all"]):
                with self.subTest(argv=argv):
                    err = self.cli(*argv, rc=2)
                    self.assertTrue(err.startswith(f"{ui.FAIL} only the tech lead "), err)
                    self.assertEqual(err.count("\n"), 1, err)
        self.assertTrue(ct.record_path("claude", SID).exists())

    def test_a_missing_argument_is_one_line_and_exit_2(self):
        self.assertEqual(self.cli("forget", rc=2),
                         f"{ui.FAIL} say which: --session <agent>:<session id> or --all\n")
        self.assertIn("give the transcript", self.cli("compactions", rc=2))


class FailOpenTests(Base):
    def test_every_continuity_hook_fails_open(self):
        self.make_run()
        calls = [lambda: ct.on_prompt("claude", self.payload("UserPromptSubmit", prompt="go"), now=T0),
                 lambda: ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=T0),
                 lambda: ct.on_session_start("claude", self.payload("SessionStart.compact"), now=T0),
                 lambda: ct.on_post_compact("claude", self.payload("PostCompact.manual"), now=T0),
                 lambda: ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0),
                 lambda: ct.on_stop("claude", self.payload("Stop"), dev_turn=True, now=T0),
                 lambda: ct.on_subagent_stop("claude", fixture("SubagentStop"), now=T0),
                 lambda: ct.on_session_end("claude", self.payload("SessionEnd.clear"), now=T0)]
        # a record that does not parse is never written over, and the brief renders from the live run
        ct.CONT.mkdir(mode=0o700)
        ct.record_path("claude", SID).write_text('{"checkpoint": {"prompts": [')
        with mock.patch.object(state, "WAIT", 0.05):
            outs = [c() for c in calls]
        self.assertEqual(ct.record_path("claude", SID).read_text(), '{"checkpoint": {"prompts": [')
        self.assertIn("Tank", outs[2])
        # a busy lock: every hook answers within its wait, without raising
        ct.record_path("claude", SID).unlink()
        got, held = [], threading.Event()

        def holder():
            with state.hold(pg.run_path("claude", SID)):
                held.set()
                time.sleep(1.0)
        t = threading.Thread(target=holder)
        t.start()
        held.wait(5)
        with mock.patch.object(state, "WAIT", 0.05):
            got = [c() for c in calls]
        t.join()
        self.assertEqual([type(x) for x in got], [type(x) for x in outs])
        # a missing transcript means no detection, not an error
        gone = self.payload("PostToolUse.Bash", transcript_path=str(self.root / "missing.jsonl"))
        self.assertEqual(ct.on_tool("claude", gone, now=T0), "")
        self.assertTrue((self.root / "logs" / "hooks.log").exists())


class ComplianceReview2Tests(Base):
    """Change 5 of the compliance review, as its second review (C1-C5) requires it, on neutral fixtures: request text
    lives only in checkpoint.prompts, another provider gets ids and senders, --provider only narrows, a kept request
    goes 7 days after it was written, and a pending request is shown to the session itself only."""
    EMPTY = "<summary>The session works on a release. Nothing else is recorded here.</summary>"

    def raw(self, sid=SID, agent="claude") -> str:
        return ct.record_path(agent, sid).read_text()

    def compact(self, now):
        ct.on_pre_compact("claude", self.payload("PreCompact.manual"), now=now)
        ct.on_post_compact("claude", self.payload("PostCompact.manual", compact_summary=self.EMPTY), now=now + 5)

    def test_c1_a_request_dropped_after_the_compaction_leaves_no_text_in_the_record_or_the_list(self):
        self.make_run(lanes=())
        self.prompt("rename the flag in `lib/flags.py` to MARKERKEPT", "d1", T0 - 300)
        self.prompt("MARKERGONE a question with no code in it", "n1", T0 - 60, dev_turn=...)  # mid-turn compaction
        self.compact(T0)
        ct.on_stop("claude", self.payload("Stop", prompt_id="n1"), dev_turn=False, now=T0 + 30)
        self.assertNotIn("MARKERGONE", self.raw())
        r = self.rec()
        outside = json.dumps({k: v for k, v in r.items() if k != "checkpoint"})
        self.assertNotIn("MARKERKEPT", outside)  # missing and deliver keep ids and times only
        got = ct.on_tool("claude", self.payload("PostToolUse.Bash"), now=T0 + 40)
        self.assertIn("«rename the flag in `lib/flags.py` to MARKERKEPT» (missing `lib/flags.py`)", got)
        self.assertNotIn("MARKERGONE", got)

    def test_c1_clear_leaves_no_request_text_in_the_old_record(self):
        self.make_run(lanes=())
        self.prompt("MARKERCLEAR rename the flag in `lib/flags.py`", "d1", T0 - 300)
        self.compact(T0)
        ct.on_session_end("claude", self.payload("SessionEnd.clear"), now=T0 + 60)
        self.assertEqual(self.texts(), [])
        self.assertNotIn("MARKERCLEAR", self.raw())

    def test_c2_another_provider_gets_the_ids_and_senders_but_no_free_text(self):
        self.make_run()  # decision D1 from Tank, message M1 from Neo
        self.prompt("MARKERREQUEST ship it", "d1", T0 - 300)
        out = ct.brief("claude", SID, reason="manual", chat=True, provider="codex", now=T0)
        for text in ("MARKERREQUEST", "Which icon set", "behind a flag", "A: flag"):
            self.assertNotIn(text, out)
        self.assertIn("decision D1 from Backend engineer (Tank)", out)
        self.assertIn("M1 from Senior engineer (Neo)", out)
        self.assertIn("Which icon set", ct.brief("claude", SID, reason="manual", chat=True, provider="claude", now=T0))

    def test_c3_an_agent_reads_another_providers_session_without_its_text_whatever_provider_it_names(self):
        # Seraph's review of 85e37de: a Claude session asked for a Codex session's brief with --provider codex
        self.prompt("MARKERCODEX port the exporter", "x1", T0 - 100, sid="cx1", agent="codex")
        claude_main = {"caller": "main", "agent": "claude", "session": SID, "how": "env", "calls": []}
        with mock.patch.object(pg, "caller", lambda *a, **k: claude_main):
            for argv in (["--provider", "codex"], ["--provider", "openai"], []):
                with self.subTest(argv=argv):
                    out = self.cli("brief", "--session", "codex:cx1", *argv)
                    self.assertNotIn("MARKERCODEX", out)
                    self.assertIn("Latest requests: 1 request", out)
        codex_main = dict(claude_main, agent="codex", session="cx1", caller="unverified")
        with mock.patch.object(pg, "caller", lambda *a, **k: codex_main):
            self.assertIn("MARKERCODEX", self.cli("brief", "--session", "codex:cx1"))
            self.assertNotIn("MARKERCODEX", self.cli("brief", "--session", "codex:cx1", "--provider", "claude"))

    def test_c4_a_kept_request_goes_seven_days_after_it_was_written_while_the_record_changes(self):
        day = 86400
        self.prompt("MARKEROLD bump the version in `pyproject.toml`", "d1", T0 - 10 * day)
        for i in range(9, 0, -1):  # one non-development turn a day: the record changes, so expire never runs
            self.prompt("a question with no code in it", f"n{i}", T0 - i * day, dev_turn=False)
        self.assertEqual(self.texts(), [])
        self.prompt("MARKERNEW tag the release", "d2", T0)
        self.assertIn("MARKERNEW", ct.brief("claude", SID, now=T0 + 6 * day))
        self.assertNotIn("MARKERNEW", ct.brief("claude", SID, now=T0 + 8 * day))  # read before any write drops it

    def test_c5_a_pending_request_is_shown_to_the_session_itself_only(self):
        self.prompt("MARKERPENDING a question with no code in it", "n1", T0 - 10, dev_turn=...)
        for chat, provider in ((True, "claude"), (False, "claude"), (True, None)):
            with self.subTest(chat=chat, provider=provider):
                self.assertNotIn("MARKERPENDING", ct.brief("claude", SID, chat=chat, provider=provider, now=T0))
        self.assertIn("MARKERPENDING", ct.brief("claude", SID, reason="compact", now=T0))


class CodexTests(Base):
    def test_codex_compaction_hooks_record_and_brief_without_a_summary(self):
        # built from the required fields of the codex 0.166 input schemas; unverified live until 2026-11-09
        need = {n: json.loads((CODEX / f"{n}.input.schema.json").read_text())["required"]
                for n in ("PreCompact", "PostCompact", "SessionStart")}
        values = {"cwd": str(self.cwd), "model": "gpt-5.5", "session_id": "cx1", "transcript_path": None,
                  "trigger": "auto", "turn_id": "t-9", "permission_mode": "default", "source": "compact"}
        pay = {n: dict({k: values[k] for k in req if k in values}, hook_event_name=n) for n, req in need.items()}
        self.assertEqual(set(pay["PreCompact"]), set(need["PreCompact"]))
        self.prompt("port the exporter to streaming", "x1", T0 - 100, sid="cx1", agent="codex")
        self.assertEqual(ct.on_pre_compact("codex", pay["PreCompact"], now=T0), "")
        text = ct.on_session_start("codex", pay["SessionStart"], now=T0 + 1)
        ct.on_post_compact("codex", pay["PostCompact"], now=T0 + 2)
        self.assertTrue(text.startswith("devkit · this conversation was compacted (auto, 1st time)."), text[:100])
        (c,) = self.rec("cx1", "codex")["compactions"]
        self.assertEqual((c["turn_id"], c["seen"], c["closed"]), ("t-9", ["pre", "start", "post"], True))
        self.assertNotIn("missing", self.rec("cx1", "codex"))


if __name__ == "__main__":
    unittest.main()
