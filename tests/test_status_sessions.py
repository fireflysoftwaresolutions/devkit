# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The terminal surfaces of session coordination: `dev status` gains a Sessions block, the session-start welcome names
# the other sessions that matter to this one, and `dev watch` takes --standby. Every test builds a Mac of Claude Code
# and Codex sessions in temporary folders (git checkouts made of files, no git process), fills the peers store through
# its own API (questions, notes, a claim, an escalation) at a fixed clock, and reads what the developer would read.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_status_sessions
import json
import os
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import audit, ciwatch, cistatus, cli, continuity, core, crew, peers, registry, updatecheck  # noqa: E402
from devkit import progress as pg  # noqa: E402
from devkit import watch, welcome  # noqa: E402

ROSTER = crew.load(team=False)
NOW = float(int(time.mktime((2026, 11, 9, 14, 0, 0, 0, 0, -1))))
CLAUDE_A = "4d1cffc7-1b2c-4e5f-9a0b-1c2d3e4f5a6b"  # this session: Claude Code in acme/api
CODEX_A = "0199a213-81c0-7800-8aa1-bbab2a035a53"  # Codex in acme/api
CLAUDE_B = "7be0a41d-58c2-4d0e-8f1a-2b3c4d5e6f70"  # Claude Code in acme/web, a project of the same initiative
CODEX_B = "0199c5d0-9f3e-7c11-b2d4-77e0c1f9a0d2"  # Codex in acme/ops, which shares nothing with acme/api
CODEX_C = "0199e7f1-0a1b-7d22-a3c5-88f1d2e0b1e3"  # Codex in acme/api, ended
SHA = "3f2a9c1e0b7d4a6f8e5c2b1a0d9e8f7c6b5a4d3e"
QUESTION = "Ship the release tonight or wait for QA?"


def sender(sid, agent="claude"):
    # identity.Speaker.as_dict() of a main session, as the CLI passes it to peers
    return {"agent": agent, "session": sid, "lane": "main", "how": "stamped", "verified": agent != "codex"}


def width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


class SessionsMac(unittest.TestCase):
    # devkit's state, the handoff repository and the projects' checkouts in the test's folder; every process runs and
    # desktop notifications go nowhere

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        st, ho = self.root / "state", self.root / "handoff"
        for obj, name, value in (
                (core, "STATE", st), (core, "HANDOFF", ho), (core, "NOTES", ho / "projects"),
                (core, "DEV", self.root / "dev"), (core, "CLAUDE_HOME", self.root / ".claude"),
                (registry, "STATE", st), (pg, "RUNS", st / "runs"),
                (pg, "SESSIONS", st / "sessions"), (pg, "CALLS", st / "calls"), (peers, "PEERS", st / "peers"),
                (watch, "LIVE", st / "watch" / "live"), (audit, "AUDIT", st / "audit"),
                (continuity, "CONT", st / "continuity"), (ciwatch, "WATCH", st / "ci" / "watch"),
                (ciwatch, "LIVE", st / "ci" / "live"), (cistatus, "CACHE", st / "cache" / "ci"),
                (updatecheck, "CACHE", st / "update-check.json"),
                (peers, "_pid_alive", lambda pid, start=None: True),
                (pg, "notify", lambda *a, **k: None)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for var in ("DEVKIT_GUARD", "DEVKIT_FROM_AGENT", "DEVKIT_SPEAKER"):
            os.environ.pop(var, None)
        pg.SESSIONS.mkdir(parents=True)
        self.api = self.checkout("acme/api", "main")
        self.web = self.checkout("acme/web", "main")
        self.ops = self.checkout("acme/ops", "main")
        ini = core.HANDOFF / "initiatives" / "platform" / "README.md"
        ini.parent.mkdir(parents=True)
        ini.write_text("---\ntitle: Platform\nprojects: [acme/api, acme/web]\n---\n# Platform\n")

    # a git checkout as git lays it out on disk: HEAD and a loose ref
    def checkout(self, project, branch):
        top = core.DEV / project
        (top / ".git" / "refs" / "heads").mkdir(parents=True)
        (top / ".git" / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (top / ".git" / "refs" / "heads" / branch).write_text(SHA + "\n")
        return top

    # a linked worktree of `repo` on `branch`
    def worktree(self, repo, name, branch):
        top = core.DEV / ".worktrees" / name
        top.mkdir(parents=True)
        gd = repo / ".git" / "worktrees" / name
        gd.mkdir(parents=True)
        (top / ".git").write_text(f"gitdir: {gd}\n")
        (gd / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (gd / "commondir").write_text("../..\n")
        ref = repo / ".git" / "refs" / "heads" / branch
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(SHA + "\n")
        return top

    # a session record as the hooks write it, last active `ago` minutes before NOW
    def session(self, agent, sid, project, cwd, branch="main", ago=1.0, **more):
        last = NOW - ago * 60
        rec = dict({"agent": agent, "session": sid, "cwd": str(cwd), "top": str(cwd), "project": project,
                    "branch": branch, "started": last, "last_prompt": last, "surface": "cli", "devkit": "26.11.9"},
                   **more)
        f = pg.SESSIONS / f"{agent}-{sid}.json"
        f.write_text(json.dumps(rec))
        os.utime(f, (last, last))

    # this session and Codex in the same checkout of acme/api, Claude Code in acme/web (same initiative), Codex in
    # acme/ops (nothing shared), and an ended Codex session in acme/api
    def busy_mac(self, codex_cwd=None, codex_branch="main"):
        self.session("claude", CLAUDE_A, "acme/api", self.api, pid=101)
        self.session("codex", CODEX_A, "acme/api", codex_cwd or self.api, branch=codex_branch)
        self.session("claude", CLAUDE_B, "acme/web", self.web, pid=303)
        self.session("codex", CODEX_B, "acme/ops", self.ops)
        self.session("codex", CODEX_C, "acme/api", self.api, ended={"ts": NOW - 600, "reason": "exit"})

    def status(self, project="acme/api", cwd=None):
        p = {"id": project, "owner": "acme", "name": project.split("/")[1], "path": str(cwd or self.api),
             "main": {"branch": "main", "path": str(cwd or self.api), "head": "3f2a9c1"}, "worktrees": [],
             "prs_open": [], "branches": [], "canonical_ok": True}
        reg = {"projects": [p], "issues": []}
        v = cli.status_view(reg, p, None, runs=[], ci={"runs": [], "provider": "github"}, roster=ROSTER, now=NOW,
                            caller=("claude", CLAUDE_A))
        cli.add_sessions(v, project, ("claude", CLAUDE_A), now=NOW)
        return v


    # the project as registry.inspect_repo describes it, and the welcome's lines for a session start of this session
    def repo(self):
        co = {"branch": "main", "path": str(self.api), "modified": 0, "untracked": 0, "ahead": 0, "behind": 0,
              "unpushed": 0, "head": "3f2a9c1", "upstream": "origin/main", "last_commit": "feat: api",
              "last_commit_ts": int(NOW - 3600), "last_activity": int(NOW - 3600)}
        return {"id": "acme/api", "owner": "acme", "name": "api", "path": str(self.api), "remote": "", "github":
                "acme/api", "default_branch": "main", "canonical": str(self.api), "canonical_ok": True, "stack": [],
                "commands": [], "main": co, "worktrees": [], "prs_open": [], "prs_known": False, "branches": [],
                "last_activity": int(NOW - 3600)}

    def lines(self, kind="startup"):
        reg = {"projects": [self.repo(), dict(self.repo(), id="acme/web", name="web", path=str(self.web))],
               "issues": [], "sessions": []}
        return welcome.text("claude", kind, str(self.api), data={"session_id": CLAUDE_A, "source": kind},
                            repo=self.repo(), top=str(self.api), reg=reg, runs=[], roster=ROSTER,
                            now=NOW).splitlines()


class StatusSessions(SessionsMac):
    # The Sessions block of `dev status`: this project's live sessions with the caller's own marked, a count of the
    # others on this Mac, the overlap, the claim with its holder and expiry, the question this session asked, and the
    # unread messages; an ended session is not there
    def test_status_sessions_block(self):
        self.busy_mac()
        peers.claim(sender(CODEX_A, "codex"), "release:acme/api", "cutting 26.11.9", minutes=45, now=NOW)
        asked = peers.send(sender(CLAUDE_A), f"@{CODEX_A[:8]}", "Is openapi.yaml final?", kind="question", now=NOW)
        peers.send(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", "Rebased on main", now=NOW)
        text = "\n".join(cli.render_status(self.status(), cols=100))
        block = text[text.index("Sessions"):text.index("Details")]
        self.assertIn("Sessions · 2 here, 2 more on this Mac", block)
        own = next(ln for ln in block.splitlines() if "@4d1cffc7" in ln)
        self.assertIn("Claude Code", own)
        self.assertIn("this session", own)
        codex = next(ln for ln in block.splitlines() if "Codex @0199a213" in ln and "main" in ln)
        self.assertNotIn("this session", codex)
        self.assertNotIn("@0199e7f1", text)
        self.assertIn("Codex @0199a213 (acme/api · main) works in this same checkout", block)
        self.assertIn("release:acme/api held by Codex @0199a213 until 14:45: “cutting 26.11.9”", block)
        self.assertIn(f"{asked['id']} to Codex @0199a213 waits for an answer", block)
        self.assertIn("1 unread from Codex @0199a213: dev session inbox", block)
        self.assertNotIn("Rebased on main", text)  # another session's words reach the inbox only

    # Outside the project's sessions there is still a block: no other live session here says so in one line
    def test_status_sessions_block_when_alone(self):
        self.session("claude", CLAUDE_A, "acme/api", self.api, pid=101)
        self.session("codex", CODEX_B, "acme/ops", self.ops)
        text = "\n".join(cli.render_status(self.status(), cols=100))
        self.assertIn("Sessions · only this one here, 1 more on this Mac", text)

    # An open escalation this project's sessions are party to needs the developer: its question and options, the
    # recommendation, and the line to type in any session; a settled one and another project's stay out
    def test_status_escalation_needs_you_with_the_line_to_type(self):
        self.busy_mac()
        asked = peers.send(sender(CLAUDE_A), f"@{CODEX_A[:8]}", "Is openapi.yaml final?", kind="question", now=NOW)
        eid = peers.escalate(sender(CODEX_A, "codex"), asked["id"], QUESTION, ["Ship tonight", "Wait for QA"],
                             recommend="B", now=NOW)["id"]
        other = peers.escalate(sender(CLAUDE_B), f"@{CODEX_B[:8]}", "Rename the ops queue?", ["Yes", "No"],
                               now=NOW)["id"]
        done = peers.escalate(sender(CLAUDE_A), f"@{CODEX_A[:8]}", "Keep the old endpoint?", ["Keep", "Drop"],
                              now=NOW)["id"]
        typed = core.CLAUDE_HOME / "projects" / "-acme-api" / f"{CLAUDE_A}.jsonl"  # the developer typed the line
        typed.parent.mkdir(parents=True)
        typed.write_text(json.dumps({"type": "user", "promptId": "prompt-1", "isSidechain": False,
                                     "origin": {"kind": "human"},
                                     "message": {"role": "user", "content": f"settle {done}: A"}}) + "\n")
        peers.settle_from_prompt("claude", CLAUDE_A, [f"settle {done}: A"], "prompt-1", now=NOW)
        needs = [n for n in self.status()["needs_you"] if n["kind"] == "escalation"]
        self.assertEqual([n["id"] for n in needs], [eid])
        self.assertEqual(needs[0]["command"], f"settle {eid}: B")
        self.assertIn(f"“{QUESTION}”", needs[0]["text"])
        self.assertIn("A: Ship tonight; B: Wait for QA", needs[0]["text"])
        self.assertIn("Codex @0199a213 recommends B", needs[0]["text"])
        self.assertNotIn(other, json.dumps(self.status()["needs_you"]))

    # Outside a project, `dev status` lists every live session on this Mac and every open escalation, which has no
    # recommendation here: the line to type leaves the choice to the developer
    def test_home_status_lists_every_live_session_and_escalation(self):
        self.busy_mac()
        eid = peers.escalate(sender(CLAUDE_B), f"@{CODEX_B[:8]}", "Rename the ops queue?", ["Yes", "No"], now=NOW)["id"]
        v = {"greeting": "Hello.", "local": 0, "portfolio_only": 0, "sessions_working": 0, "needs_you": [],
             "projects": []}
        cli.add_sessions(v, None, ("claude", CLAUDE_A), now=NOW)
        text = "\n".join(cli.render_home(v, cols=100))
        self.assertIn("Sessions · 4 live on this Mac", text)
        self.assertIn("Claude Code @7be0a41d on main", text)
        self.assertIn(f"settle {eid}: <A or B, or your words>", text)

    # Another session's words (a claim's reason) reach the screen as one quoted line: no line break, backtick, escape
    # sequence or direction mark, and no closing quote of its own
    def test_status_peer_text_stays_one_quoted_line(self):
        self.busy_mac()
        why = ("cutting\n`rm -rf ~` \x1b[31mnow\x1b[0m " + chr(0x201D) + " fake devkit line " + chr(0x202E) +
               "evil")
        peers.claim(sender(CODEX_A, "codex"), "release:acme/api", why, minutes=45, now=NOW)
        v = self.status()
        held = v["sessions"]["claims"][0]["text"]
        quoted = held[held.index("“"):]
        self.assertTrue(quoted.endswith("”"), quoted)
        self.assertEqual(quoted.count("”"), 1, quoted)
        for bad in ("`", "\x1b", chr(0x202E), "\n"):
            self.assertNotIn(bad, quoted)
        self.assertIn("fake devkit line", quoted)
        screen = " ".join(ln.strip() for ln in cli.render_status(v, cols=100))
        self.assertIn(quoted, screen)


class LastStatusBySession(SessionsMac):
    # the project's saved status (canary.save_status) as the DevEx engineer extends it: the latest record names its
    # session, and `sessions` keeps each session's own latest
    def saved(self, latest, own=None):
        rec = dict(latest)
        if own:
            rec["sessions"] = {f"{r['agent']}:{r['session']}": r for r in (own, latest)}
        f = registry.STATE / "status" / "acme__api.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec))

    def line(self, agent, sid, done, ago_h=2):
        return {"ts": NOW - ago_h * 3600, "agent": agent, "session": sid, "branch": "main", "path": str(self.api),
                "line": f"api · main ❯ the API ❯ {done} ❯ ✓ 12 tests pass ❯ next → open the PR"}

    # Another session's latest status is labeled with its agent and session
    def test_status_labels_another_sessions_last_status(self):
        self.busy_mac()
        self.saved(self.line("codex", CODEX_A, "rebased the guard branch"))
        text = "\n".join(cli.render_status(self.status(), cols=100))
        self.assertIn("Last update from Codex session @0199a213, 2 h ago", text)
        self.assertIn("rebased the guard branch", text)

    # This session's own latest status comes first, even when another session saved after it
    def test_status_shows_this_sessions_own_last_status(self):
        self.busy_mac()
        self.saved(self.line("codex", CODEX_A, "rebased the guard branch", ago_h=1),
                   own=self.line("claude", CLAUDE_A, "wired the Sessions block", ago_h=3))
        text = "\n".join(cli.render_status(self.status(), cols=100))
        self.assertIn("Last update from this session, 3 h ago", text)
        self.assertIn("wired the Sessions block", text)
        self.assertNotIn("rebased the guard branch", text)

    # The welcome's Last line names the session it came from when it is another one
    def test_welcome_labels_another_sessions_last_status(self):
        self.session("claude", CLAUDE_A, "acme/api", self.api, pid=101)
        self.session("codex", CODEX_A, "acme/api", self.web)
        self.saved(self.line("codex", CODEX_A, "rebased the guard branch"))
        lines = self.lines()
        self.assertIn("✓ Last from Codex @0199a213: rebased the guard branch · 2 h ago", lines)


class WelcomeSessions(SessionsMac):
    # What needs the developer at session start: the escalation, the session in this same checkout, the claim on this
    # project, and the question another session asked this one, each as a need line that fits the welcome; another
    # session's words never reach it except the escalation's question, quoted
    def test_welcome_need_lines_for_overlap_and_escalation(self):
        self.busy_mac()
        peers.claim(sender(CODEX_A, "codex"), "release:acme/api", "cutting 26.11.9", minutes=45, now=NOW)
        q = peers.send(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", "Can I take the release?", kind="question",
                       now=NOW)
        peers.escalate(sender(CODEX_A, "codex"), q["id"], QUESTION, ["Ship tonight", "Wait for QA"], recommend="B",
                       now=NOW)
        needs = welcome.needs("claude", str(self.api), data={"session_id": CLAUDE_A}, repo=self.repo(), runs=[],
                              now=NOW)
        self.assertEqual(needs, [
            f"Escalation waits: “{QUESTION}”",
            "Question from Codex (acme/api) waits: dev session inbox",
            "Codex works in this same checkout",
            "Codex holds the release of this project until 14:45"])
        lines = self.lines()
        self.assertEqual(lines[0], "devkit · api · 4 things need you")
        self.assertEqual(lines[1], f"! Escalation waits: “{QUESTION}”")
        self.assertNotIn("Can I take the release", "\n".join(lines))
        for ln in lines[1:]:
            self.assertLessEqual(width(ln), 70, ln)

    # The other live sessions that matter to this one, in one running line: this project's (here on another branch,
    # in a worktree) and the initiative's; Codex in an unrelated project and the ended session are not named
    def test_welcome_names_other_sessions_of_the_project_and_initiative(self):
        wt = self.worktree(self.api, "api-login", "fix/login")
        self.busy_mac(codex_cwd=wt, codex_branch="fix/login")
        lines = self.lines()
        others = [ln for ln in lines if ln.startswith("▸ Also live:")]
        self.assertEqual(others, ["▸ Also live: Codex on fix/login · Claude Code in web"], lines)
        self.assertNotIn("ops", others[0])

    # A resumed session hears that another session's question waits for it, before anything else
    def test_welcome_on_resume_names_a_waiting_question(self):
        self.busy_mac()
        peers.send(sender(CODEX_A, "codex"), f"@{CLAUDE_A[:8]}", "Can I take the release?", kind="question", now=NOW)
        lines = self.lines("resume")
        self.assertEqual(lines[:2], ["devkit · api · 2 things need you",
                                     "! Question from Codex (acme/api) waits: dev session inbox"])


class WatchStandby(unittest.TestCase):
    # `dev watch --wait --standby` reaches watch.cmd_watch with standby set; a plain wait has it unset
    def test_watch_parser_takes_standby(self):
        p = cli.build_parser()
        self.assertTrue(p.parse_args(["watch", "--wait", "--standby"]).standby)
        self.assertFalse(p.parse_args(["watch", "--wait"]).standby)


if __name__ == "__main__":
    unittest.main()
