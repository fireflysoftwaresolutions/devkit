# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The dashboard's view of the agent sessions on this Mac: collect() gains `peers` (the live sessions with their runs,
# crews, waits and the messages waiting for them; the claims; the open escalations; the overlaps), and the Now view
# draws it: what blocks first (escalations, claims, overlaps), then the sessions by project. Another session's words
# are data: cleaned, cut to 120 characters, drawn as text. Two sessions of one person and agent on one branch are a
# collision. The Mac is the one tests/test_status_sessions.py builds, filled through the peers store's own API.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_dashboard_sessions
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_dashboard as T  # noqa: E402  (the headless Chrome harness)
from devkit import core, dashboard, peers  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_status_sessions import (CLAUDE_A, CLAUDE_B, CODEX_A, CODEX_B, CODEX_C, NOW, QUESTION, SessionsMac,  # noqa: E402
                                  sender)

EVIL = "<img src=x onerror=alert(1)>"
DIRTY = ("line one\nline two `rm -rf ~` \x1b[31mred\x1b[0m " + chr(0x201D) + " closes " + chr(0x202E) + "rtl "
         + "x" * 300)


class Scene(SessionsMac):
    # Claude Code A runs a plan with a crew member; Codex holds the release; A asked Codex a question; Codex sent A a
    # note and escalated the question to the developer
    def scene(self, note="Rebased on main", question="Is openapi.yaml final?", why="cutting 26.11.9"):
        self.busy_mac()
        pg.RUNS.mkdir(parents=True, exist_ok=True)
        (pg.RUNS / f"claude-{CLAUDE_A}.json").write_text(json.dumps(
            {"v": 2, "agent": "claude", "session": CLAUDE_A, "project": "acme/api", "goal": "Release 26.11.9",
             "state": "running", "started": NOW - 3600, "updates": [], "intervals": [], "decisions": [],
             "messages": [], "mode": "guided",
             # a long plan: the landing strip shows the most glyphs it has and its landing bracket
             "plan": [{"name": f"step {i}", "state": "done", "est": 10, "started": NOW - 3600 + i * 60,
                       "finished": NOW - 3570 + i * 60} for i in range(40)]
             + [{"name": "api", "state": "running", "owner": "lane1", "est": 30, "started": NOW - 600}]
             + [{"name": f"later {i}", "state": "pending", "est": 20} for i in range(15)],
             "agents": {"lane1": {"label": "Tank: api", "persona": "Tank", "state": "running",
                                  "started": NOW - 600, "updates": []}}}))
        peers.claim(sender(CODEX_A, "codex"), "release:acme/api", why, minutes=45, now=NOW)
        asked = peers.send(sender(CLAUDE_A), f"@{CODEX_A[:8]}", question, kind="question", now=NOW)
        peers.send(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", note, now=NOW)
        eid = peers.escalate(sender(CODEX_A, "codex"), asked["id"], QUESTION, ["Ship tonight", "Wait for QA"],
                             recommend="B", now=NOW)["id"]
        return asked["id"], eid

    def collect(self):
        return dashboard.collect({"projects": [], "issues": [], "sessions": []}, handoff=core.HANDOFF, now=NOW)

    def row(self, d, sid):
        return next(r for g in d["peers"]["groups"] for r in g["sessions"] if r["session"] == sid)


class PeersOnTheDashboard(Scene):
    # collect() names every live session by agent with its project, branch, run, crew and the messages waiting for it,
    # grouped by project with the project that holds what blocks first; the claim with its holder and expiry; the
    # escalation with the line the developer types; the overlap once per pair; an ended session is left out
    def test_dashboard_collect_sessions_claims_escalations(self):
        qid, eid = self.scene()
        d = self.collect()
        p = d["peers"]
        self.assertIsNone(p["error"])
        self.assertEqual([g["project"] for g in p["groups"]], ["acme/api", "acme/ops", "acme/web"])
        self.assertEqual(p["groups"][2]["initiatives"], ["platform"])
        keys = [r["session"] for g in p["groups"] for r in g["sessions"]]
        self.assertEqual(sorted(keys), sorted([CLAUDE_A, CODEX_A, CLAUDE_B, CODEX_B]))
        self.assertNotIn(CODEX_C, json.dumps(p))
        a = self.row(d, CLAUDE_A)
        self.assertEqual((a["agent_name"], a["prefix"], a["branch"]), ("Claude Code", "@4d1cffc7", "main"))
        self.assertEqual(a["run"]["goal"], "Release 26.11.9")
        self.assertEqual([m["name"] for m in a["crew"]], ["Backend engineer (Tank)"])
        self.assertEqual([(m["kind"], m["text"], m["from"]["prefix"]) for m in a["inbox"]],
                         [("note", "Rebased on main", "@0199a213")])
        codex = self.row(d, CODEX_A)
        self.assertEqual(codex["agent_name"], "Codex")
        self.assertEqual(codex["claims"], ["release:acme/api"])
        self.assertIn((qid, "question"), [(m["id"], m["kind"]) for m in codex["inbox"]])
        [c] = p["claims"]
        self.assertEqual((c["resource"], c["holder"]["prefix"], c["until"], c["why"], c["queue"]),
                         ("release:acme/api", "@0199a213", NOW + 45 * 60, "cutting 26.11.9", 0))
        [e] = p["escalations"]
        self.assertEqual((e["id"], e["question"], e["options"], e["recommend"], e["type"]),
                         (eid, QUESTION, ["A: Ship tonight", "B: Wait for QA"], "B", f"settle {eid}: B"))
        self.assertEqual(sorted(x["prefix"] for x in e["parties"]), ["@0199a213", "@4d1cffc7"])
        [o] = p["overlaps"]
        self.assertEqual((o["kind"], sorted([o["a"]["prefix"], o["b"]["prefix"]])), ("checkout",
                                                                                      ["@0199a213", "@4d1cffc7"]))

    # A message's text, a claim's reason and an escalation's question and options reach the page as one line of at
    # most 120 characters and a closing ellipsis: no line break, backtick, escape sequence, direction mark or curly
    # quote of their own
    def test_dashboard_texts_cleaned_and_cut(self):
        self.busy_mac()
        peers.claim(sender(CODEX_A, "codex"), "release:acme/api", DIRTY[:290], minutes=45, now=NOW)
        peers.send(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", DIRTY, now=NOW)
        peers.escalate(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", DIRTY[:290], [DIRTY[:190], "Wait"], now=NOW)
        p = self.collect()["peers"]
        texts = [self.row({"peers": p}, CLAUDE_A)["inbox"][0]["text"], p["claims"][0]["why"],
                 p["escalations"][0]["question"], p["escalations"][0]["options"][0]]
        for t in texts:
            self.assertLessEqual(len(t), 120, t)
            self.assertRegex(t, r"^(A: )?line one line two rm -rf ~ red")
            self.assertTrue(t.endswith("…"), t)
            for bad in ("\n", "`", "\x1b", chr(0x202E), chr(0x201D)):
                self.assertNotIn(bad, t)

    # An escalation with no recommendation shows a line to type that names no option: the choice stays the developer's
    def test_dashboard_escalation_without_recommendation_names_no_choice(self):
        self.busy_mac()
        eid = peers.escalate(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", QUESTION, ["Ship tonight", "Wait for QA"],
                             now=NOW)["id"]
        [e] = self.collect()["peers"]["escalations"]
        self.assertEqual(e["type"], f"settle {eid}: <A or B, or your words>")

    # B-g3e6lyh6q38: two Claude Code sessions of one developer on acme/widget@main collide; a third session of theirs
    # on another project does not
    def test_two_sessions_of_one_person_and_agent_on_a_branch_collide(self):
        s = lambda sid, project: {"agent": "claude", "session": sid, "project": project, "branch": "main",  # noqa: E731
                                  "last_seen": NOW}
        team = [{"user": "ana", "host": "mac", "sessions": [s("s1", "acme/widget"), s("s2", "acme/widget"),
                                                            dict(s("t1", "acme/gadget"), agent="codex")]}]
        same = [c for c in dashboard.collisions(team, [], NOW) if c["kind"] == "same-branch"]
        self.assertEqual([(c["project"], c["branch"]) for c in same], [("acme/widget", "main")])
        self.assertEqual(same[0]["text"], "2 sessions on acme/widget@main: ana@mac (claude) ×2")


@unittest.skipUnless(T.CHROME, T.NO_CHROME)
class NowSessionsInChrome(Scene):
    def page(self, note="Rebased on main"):
        _qid, eid = self.scene(note=note)
        return dashboard.page(self.collect()), eid

    # What blocks comes first (above Needs you): the escalation with the keys to type, then the claim and the
    # overlap; the sessions follow the queue and the runs, by project, each named by agent; another session's words
    # are drawn quoted
    def test_now_shows_what_blocks_first_then_the_sessions(self):
        page, eid = self.page()
        out = T.browse(page, """
            const blocks = $("#now-peers-block"), list = $("#now-sessions"), queue = $(".now-queue");
            out.order = [blocks, queue, list].map(el => el ? Array.from(document.querySelectorAll("section")).indexOf(el.closest("section")) : -1);
            out.blocks = $$("li", blocks).map(text);
            out.keys = $$("kbd", blocks).map(text);
            out.groups = $$("[data-group]", list).map(g => g.dataset.group);
            out.rows = $$("li[data-session]", list).map(text);
            """, width=1440, hash_="#/now", now=NOW)
        self.assertIsNone(out.get("probeError"), out)
        self.assertFalse(out["pageError"], out)
        self.assertTrue(-1 < out["order"][0] < out["order"][1] < out["order"][2], out["order"])
        self.assertIn(QUESTION, out["blocks"][0])
        self.assertEqual(out["keys"], [f"settle {eid}: B"])
        self.assertIn("release:acme/api", out["blocks"][1])
        self.assertIn("Codex @0199a213", out["blocks"][1])
        self.assertIn("same checkout", out["blocks"][2])
        self.assertEqual(out["groups"], ["acme/api", "acme/ops", "acme/web"])
        self.assertTrue(any("Claude Code @4d1cffc7" in r and "Release 26.11.9" in r and "40 of 56 steps" in r
                            for r in out["rows"]), out["rows"])
        self.assertTrue(any("Codex @0199a213" in r for r in out["rows"]), out["rows"])
        self.assertTrue(any("“Rebased on main”" in r for r in out["rows"]), out["rows"])

    # At a phone's width nothing scrolls sideways, every control in the two areas is a link, button or summary with
    # a name, and each area is labelled by its heading
    def test_now_sessions_fit_a_phone_and_take_the_keyboard(self):
        page, _eid = self.page()
        out = T.browse(page, """
            out.over = document.documentElement.scrollWidth - innerWidth;
            const areas = [$("#now-peers-block"), $("#now-sessions")];
            out.labelled = areas.map(a => a && a.closest("section") && !!document.getElementById(a.closest("section").getAttribute("aria-labelledby")));
            const ctl = areas.flatMap(a => $$("a, button, summary, [tabindex]", a));
            out.controls = ctl.length;
            out.bad = ctl.filter(el => !["A", "BUTTON", "SUMMARY"].includes(el.tagName) || !(el.getAttribute("aria-label") || text(el))).map(el => el.outerHTML.slice(0, 120));
            out.wide = areas.flatMap(a => $$("*", a)).filter(el => el.getBoundingClientRect().right > innerWidth + 1).map(el => el.className || el.tagName);
            """, width=390, hash_="#/now", now=NOW)
        self.assertIsNone(out.get("probeError"), out)
        self.assertLessEqual(out["over"], 0, out)
        self.assertEqual(out["labelled"], [True, True])
        self.assertGreater(out["controls"], 0)
        self.assertEqual(out["bad"], [])
        self.assertEqual(out["wide"], [])

    # Markup in another session's words is drawn as its characters, never as an element
    def test_peer_text_is_drawn_as_text(self):
        page, _eid = self.page(note=EVIL)
        out = T.browse(page, """
            out.imgs = $$("#now-sessions img").length;
            out.shown = text($("#now-sessions"));
            """, width=1440, hash_="#/now", now=NOW)
        self.assertEqual(out["imgs"], 0)
        self.assertIn(EVIL, out["shown"])


if __name__ == "__main__":
    unittest.main()
