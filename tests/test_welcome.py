# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The session-start welcome (lib/devkit/welcome.py): what the developer reads when a session starts, built from
# devkit's recorded state only. Every test builds its own project, runs and records in temporary folders, with a
# fixed clock, and checks the text against the UX designer's acceptance criteria: lines per start kind, widths, the
# first line, the rail and its order, the characters, crew names, an honest verdict, degradation, no network, one
# repository inspection, the same text for every agent, who gets none, the update line, the model's paragraph, the
# notices that became need lines, and each edge case of the spec.
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ciwatch, cistatus, cli, continuity, core, crew, ext, progress, prompt, registry  # noqa: E402
from devkit import updatecheck  # noqa: E402

try:
    from devkit import welcome  # noqa: E402
except ImportError:  # before the module exists, every test fails on this alone (Sandbox.setUp)
    welcome = None

ROSTER = crew.load(team=False)
NOW = time.mktime((2026, 11, 8, 10, 0, 0, 0, 0, -1))
TODAY = time.strftime("%Y-%m-%d", time.localtime(NOW))
SID, OTHER = "welcome-session", "other-session"
PID, REMOTE, BRANCH = "acme/billing", "https://github.com/acme/billing.git", "feat/invoice-export"
MARKS = set("·…▸✓✗›–")
RAIL = ("! ", "▸ ", "✓ ", "› ", "· ", "+ ")
VERDICTS = ("1 thing needs you", "things need you", "work in progress", "nothing needs you", "new project",
            "see dev status")
TASK_ID = re.compile(r"\b[TFBS]-[a-z0-9]{6,}\b")


def cols(s: str) -> int:
    """Display columns as a terminal counts them: East Asian wide and full-width characters take two."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


class Sandbox(unittest.TestCase):
    """One project (acme/billing) with a checkout in a temporary folder, and every state folder the welcome reads
    moved below the same temporary folder."""

    def setUp(self):
        self.assertIsNotNone(welcome, "lib/devkit/welcome.py does not exist yet")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state, self.handoff = self.root / "state", self.root / "handoff"
        self.dir = self.root / "billing"
        for d in (self.state, self.handoff, self.dir):
            d.mkdir()
        for obj, name, value in (
                (core, "STATE", self.state), (core, "HANDOFF", self.handoff), (core, "NOTES", self.handoff / "projects"),
                (registry, "STATE", self.state), (progress, "RUNS", self.state / "runs"),
                (progress, "SESSIONS", self.state / "sessions"), (ciwatch, "WATCH", self.state / "ci" / "watch"),
                (ciwatch, "LIVE", self.state / "ci" / "live"), (cistatus, "CACHE", self.state / "cache" / "ci"),
                (updatecheck, "CACHE", self.state / "update-check.json"),
                (continuity, "CONT", self.state / "continuity")):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DEVKIT_GUARD", None)
        self.session(SID, turn_ended=NOW - 3600)
        self.session(OTHER)

    # ----------------------------------------------------------------- fixtures

    def write(self, path: Path, doc) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def session(self, sid, agent="claude", **more):
        """A live session record: its process is this test's, so it runs."""
        self.write(progress.session_path(agent, sid), dict({"agent": agent, "session": sid, "pid": os.getpid(),
                                                            "started": NOW - 7200}, **more))

    def repo(self, name="billing", pid=PID, branch=BRANCH, **main):
        """A project as registry.inspect_repo describes it (what context_for hands the welcome)."""
        co = dict({"branch": branch, "path": str(self.dir), "modified": 3, "untracked": 0, "ahead": 0, "behind": 0,
                   "unpushed": 0, "head": "1a2b3c4", "upstream": f"origin/{branch}", "last_commit": "feat: export",
                   "last_commit_ts": int(NOW - 3600), "last_activity": int(NOW - 3600)}, **main)
        return {"id": pid, "owner": pid.split("/")[0], "name": name, "path": str(self.dir), "remote": REMOTE,
                "github": pid, "default_branch": "main", "canonical": str(self.dir), "canonical_ok": True,
                "stack": [], "commands": [], "main": co, "worktrees": [], "prs_open": [], "prs_known": False,
                "branches": [], "last_activity": int(NOW - 3600)}

    def reg(self, *projects):
        return {"projects": list(projects), "issues": [], "sessions": []}

    def make_run(self, owner=SID, agent="claude", goal="Invoice export", done=2, total=7, members=("Tank", "Mouse"),
            decisions=(), messages=(), project=PID):
        """A working run: `done` finished steps, one running step per member, the rest pending."""
        plan = [{"name": f"step {i + 1}", "state": "done", "est": 10, "started": NOW - 7200 + i * 600,
                 "finished": NOW - 6600 + i * 600} for i in range(done)]
        lanes = {}
        for i, m in enumerate(members):
            lanes[f"lane{i}"] = {"label": f"{m}: export work", "persona": m, "state": "running",
                                 "started": NOW - 900, "updates": [{"ts": NOW - 60, "text": "working"}]}
            plan.append({"name": f"export part {i + 1}", "state": "running", "owner": f"lane{i}", "est": 20,
                         "started": NOW - 900})
        names = iter(["wire the export endpoint", "document the export", "review the export", "release it"])
        while len(plan) < total:
            plan.append({"name": next(names, f"later step {len(plan)}"), "state": "pending", "est": 15})
        return {"v": 2, "agent": agent, "session": owner, "project": project, "goal": goal, "state": "running",
                "started": NOW - 7200, "updates": [{"ts": NOW - 120, "kind": "step", "text": "step done"}],
                "intervals": [], "plan": plan, "agents": lanes, "decisions": list(decisions),
                "messages": list(messages), "steps": total, "step": done, "mode": "guided"}

    def left(self, run) -> int:
        """The run's minutes left (progress.eta): under an hour in these fixtures, so the line says ~N min left."""
        minutes = progress.eta(run, NOW)
        self.assertLess(minutes, 59.5, "the fixture's run should end within the hour")
        return round(minutes)

    def decision(self, question="Export cancelled invoices too?", did="D1", ts=NOW - 300, lane="lane1",
                 who="Mouse"):
        return {"id": did, "from": lane, "from_name": who, "question": question, "options": ["yes", "no"],
                "recommend": "yes", "state": "pending", "ts": ts}

    def status_line(self, done="added the CSV export to the invoice list", nxt="open the pull request, then ask "
                    "for a review", ts=NOW - 2 * 86400):
        line = f"billing · main ❯ CSV export ❯ {done} ❯ ✓ 12 tests pass ❯ next → {nxt}"
        return {"ts": ts, "agent": "claude", "line": line, "branch": "main", "path": str(self.dir)}

    def ci_watch(self, state, branch=BRANCH, ts=NOW - 2 * 86400, failed=""):
        diag = [{"run": "ci", "number": 7, "conclusion": "failed", "class": "test",
                 "failed_jobs": [{"name": failed, "step": "", "url": ""}]}] if failed else []
        self.write(ciwatch.WATCH / f"{ciwatch._key('acme/billing', branch)}.json",
                   {"ts": ts, "state": state, "provider": "github", "repo": "acme/billing", "branch": branch,
                    "sha": "1a2b3c4", "runs": [{"name": "ci", "number": 7, "status": "completed",
                                                "conclusion": state}], "diagnoses": diag, "repeat": False})

    def ci_running(self, branch=BRANCH, at=NOW - 300, watched=True):
        name = f"runs-github-acme/billing-{branch}-1-1".replace("/", "__") + ".json"
        f = self.write(cistatus.CACHE / name, {"provider": "github", "repo": "acme/billing", "branch": branch,
                                               "runs": [{"status": "running", "branch": branch, "started": at,
                                                         "jobs": []}], "error": None})
        os.utime(f, (at, at))
        if watched:
            ciwatch.register({"top": str(self.dir), "ref": branch, "sha": "1a2b3c4"}, "devkit", "claude", SID,
                             os.getpid(), 30, now=NOW)

    def update_due(self, newest="99.1.0"):
        self.write(updatecheck.CACHE, {"newest": newest, "installed": "26.11.7", "checked": TODAY})

    def note(self, now_tasks=("`B-fyk3x4xfl6v` Ship the invoice export to the pilot customers",)):
        tasks = "\n".join(f"- [ ] {t}" for t in now_tasks)
        self.write(core.note_path(PID), f"---\nstatus: active\n---\n# billing\n\n## Tasks\n### Now\n{tasks}\n"
                                        "### Next\n### Later\n### Done\n")

    def text(self, kind="startup", agent="claude", repo=None, reg=None, runs=(), sid=SID, **kw):
        repo = self.repo() if repo is None else repo
        reg = self.reg(repo) if reg is None else reg
        return welcome.text(agent, kind, str(self.dir), data={"session_id": sid, "source": kind}, repo=repo,
                            top=str(self.dir), reg=reg, runs=None if runs is None else list(runs), roster=ROSTER,
                            now=NOW, **kw)

    def lines(self, *a, **kw):
        return self.text(*a, **kw).splitlines()

    def assertWellFormed(self, lines):
        """Criteria 2 to 5: widths, the first line, the rail in its fixed order, the characters."""
        self.assertTrue(lines[0].startswith("devkit · "), lines[0])
        self.assertLessEqual(cols(lines[0]), 46, lines[0])
        order = []
        for ln in lines[1:]:
            self.assertLessEqual(cols(ln), 70, ln)
            self.assertTrue(ln.startswith(RAIL), ln)
            order.append(RAIL.index(ln[:2]))
        self.assertEqual(order, sorted(order), lines)
        for ln in lines:
            self.assertNotIn("`", ln)
            self.assertNotIn("\x1b", ln)
            self.assertIsNone(TASK_ID.search(ln), ln)


def startup_with_a_run(t: Sandbox):
    """The spec's first mockup: a run at step 3 of 7 with two crew members, a decision for the developer, CI
    running and watched."""
    run = t.make_run(decisions=[t.decision()])
    t.ci_running()
    return run


# --------------------------------------------------------------------------- the variants of the spec


class Variants(Sandbox):
    def test_startup_with_a_run_and_a_decision_reads_as_the_spec_shows(self):
        run = startup_with_a_run(self)
        left = self.left(run)
        lines = self.lines(runs=[run])
        self.assertEqual(lines, [
            "devkit · billing · 1 thing needs you",
            "! Decision waits: export cancelled invoices too?",
            f"▸ Invoice export · step 3 of 7 · ~{left} min left · 2 crew working",
            "› Next: wire the export endpoint",
            "· feat/invoice-export · 3 changed files · CI running, watched"])
        self.assertWellFormed(lines)

    def test_a_quiet_project_shows_the_last_update_the_next_step_and_ci(self):
        repo = self.repo(branch="main", modified=0)
        repo["status_line"] = self.status_line()
        self.ci_watch("passed", branch="main")
        lines = self.lines(repo=repo)
        self.assertEqual(lines, [
            "devkit · billing · nothing needs you",
            "✓ Last: added the CSV export to the invoice list · 2 days ago",
            "› Next: open the pull request, then ask for a review",
            "· main · no changes · CI passed 2 days ago"])
        self.assertWellFormed(lines)

    def test_the_next_step_falls_back_to_the_handoff_notes_first_task_without_its_id(self):
        self.note()
        lines = self.lines(repo=self.repo(branch="main", modified=0))
        self.assertIn("› Next: ship the invoice export to the pilot customers", lines)
        self.assertWellFormed(lines)

    def test_outside_a_project_the_workspace_variant_lists_needs_runs_and_recent_projects(self):
        billing = self.repo()
        billing["last_activity"] = int(NOW - 2 * 3600)
        site, api = self.repo(name="site", pid="acme/site"), self.repo(name="api", pid="acme/api")
        site["last_activity"], api["last_activity"] = int(NOW - 5 * 3600), int(NOW - 2 * 86400)
        run = self.make_run(decisions=[self.decision()])
        left = self.left(run)
        lines = welcome.text("claude", "startup", str(self.root), data={"session_id": SID}, repo=None, top="",
                             reg=self.reg(billing, site, api), runs=[run], roster=ROSTER, now=NOW).splitlines()
        self.assertEqual(lines, [
            "devkit · workspace · 1 thing needs you",
            "! billing · decision waits: export cancelled invoices too?",
            f"▸ billing · Invoice export · step 3 of 7 · ~{left} min left",
            "· Recent: billing 2 h ago · site 5 h ago · api 2 days ago"])
        self.assertWellFormed(lines)

    def test_resume_says_what_happened_since_the_developer_left(self):
        run = self.make_run(decisions=[self.decision()])
        run["agents"]["lane0"].update(state="done", finished=NOW - 1800)
        self.ci_watch("passed", ts=NOW - 1200)
        lines = self.lines("resume", runs=[run])
        self.assertEqual(lines, [
            "devkit · billing · 1 thing needs you",
            "! Decision waits: export cancelled invoices too?",
            "▸ Since you left: Backend engineer (Tank) finished · CI passed"])
        self.assertWellFormed(lines)

    def test_resume_with_nothing_new_is_the_headline_alone(self):
        self.ci_watch("passed", ts=NOW - 2 * 86400)
        self.assertEqual(self.lines("resume", runs=[self.make_run()]), ["devkit · billing · work in progress"])

    def test_clear_names_the_open_run_in_one_line(self):
        self.assertEqual(self.lines("clear", runs=[self.make_run()]), ["devkit · billing · run open, step 3 of 7"])

    def test_clear_with_a_need_says_the_need(self):
        self.assertEqual(self.lines("clear", runs=[self.make_run(decisions=[self.decision()])]),
                         ["devkit · billing · 1 thing needs you"])

    def test_clear_with_no_run_and_no_need_says_nothing(self):
        self.assertEqual(self.text("clear"), "")

    def test_compact_says_what_continuity_kept(self):
        prompts = [{"ts": NOW - 600 * i, "text": f"request {i}"} for i in range(1, 4)]
        self.write(continuity.record_path("claude", SID), {"agent": "claude", "session": SID,
                                                           "checkpoint": {"prompts": prompts}})
        self.write(progress.run_path("claude", SID), self.make_run(decisions=[self.decision()]))
        self.assertEqual(self.lines("compact"), ["devkit · kept 3 requests, the run, 1 decision"])

    def test_compact_with_nothing_carried_says_nothing(self):
        self.assertEqual(self.text("compact"), "")


# --------------------------------------------------------------------------- criteria 1 to 6


class Budgets(Sandbox):
    def crowded(self):
        """More than any budget holds: three needs, a run, a last update, a next step, CI and an update."""
        os.environ["DEVKIT_GUARD"] = "off"
        repo = self.repo()
        repo["status_line"] = self.status_line()
        self.ci_watch("failed", failed="unit tests")
        self.update_due()
        return repo, [self.make_run(decisions=[self.decision(), self.decision("Keep the old CSV columns?", "D2")])]

    def test_each_start_kind_keeps_its_line_budget(self):
        repo, runs = self.crowded()
        for kind, most in (("startup", 5), ("resume", 3), ("clear", 1), ("compact", 1)):
            lines = self.lines(kind, repo=repo, runs=runs)
            self.assertLessEqual(len(lines), most, (kind, lines))
            if lines:
                self.assertWellFormed(lines)

    def test_outside_a_project_the_budget_is_four_lines(self):
        os.environ["DEVKIT_GUARD"] = "off"
        projects = [self.repo(name=n, pid=f"acme/{n}") for n in ("billing", "site", "api", "web")]
        runs = [self.make_run(decisions=[self.decision(), self.decision("Keep the old CSV columns?", "D2")])]
        lines = welcome.text("claude", "startup", str(self.root), data={"session_id": SID}, repo=None, top="",
                             reg=self.reg(*projects), runs=runs, roster=ROSTER, now=NOW).splitlines()
        self.assertEqual(len(lines), 4, lines)
        self.assertWellFormed(lines)

    def test_over_budget_the_update_next_and_last_go_first_and_the_needs_fold(self):
        repo, runs = self.crowded()
        lines = self.lines(repo=repo, runs=runs)
        left = self.left(runs[0])
        self.assertEqual(lines, [
            "devkit · billing · 3 things need you",
            "! The guard is off in this session: restart without it",
            "! And 2 more: dev status lists them",
            f"▸ Invoice export · step 3 of 7 · ~{left} min left · 2 crew working",
            "· feat/invoice-export · 3 changed files · CI failed: unit tests"])
        self.assertIsNone(updatecheck.read_cache().get("notified"))  # not shown, so the day is not marked

    def test_long_names_are_cut_once_each_and_numbers_never(self):
        repo = self.repo(name="customer-portal-billing-service", pid="acme/customer-portal-billing-service",
                         branch="feat/PROJ-1423-invoice-export-of-cancelled-voided", modified=12)
        run = self.make_run(goal="Export of cancelled and voided invoices for the auditors",
                       decisions=[self.decision("Ship cancelled and voided invoices in one export, or two separate "
                                                "files for the auditors?")], project=repo["id"])
        left = self.left(run)
        lines = self.lines(repo=repo, runs=[run])
        self.assertEqual(lines, [
            "devkit · customer-portal-… · 1 thing needs you",
            "! Decision waits: ship cancelled and voided invoices in one export…",
            f"▸ Export of cancelled… · step 3 of 7 · ~{left} min left · 2 crew working",
            "› Next: wire the export endpoint",
            "· feat/PROJ-1423-invoic…cancelled-voided · 12 changed files"])
        self.assertWellFormed(lines)
        for ln in lines:
            self.assertLessEqual(ln.count("…"), 1, ln)

    def test_wide_characters_count_two_columns(self):
        q = "請求書のエクスポートにキャンセル済みと無効の請求書を含めますか監査人向けの別ファイルにしますか"
        lines = self.lines(runs=[self.make_run(decisions=[self.decision(q)])])
        self.assertTrue(lines[1].startswith("! Decision waits: 請求書"), lines[1])
        self.assertTrue(lines[1].endswith("…"), lines[1])
        self.assertWellFormed(lines)

    def test_the_verdict_counts_every_need_including_the_folded_ones(self):
        os.environ["DEVKIT_GUARD"] = "off"
        self.write(ext.accepted_file(), {"pending": [{"id": "mcp:a", "what": "a", "at": NOW},
                                                     {"id": "mcp:b", "what": "b", "at": NOW}]})
        lines = self.lines(runs=[self.make_run(decisions=[self.decision()])], older=True, ext_changed=True)
        self.assertEqual(lines[0], "devkit · billing · 5 things need you")
        self.assertEqual(lines[2], "! And 4 more: dev status lists them")
        self.assertEqual(sum(1 for ln in lines if ln.startswith("! ")), 2)

    def test_crew_members_are_named_with_role_and_name(self):
        run = self.make_run()
        run["agents"]["lane1"].update(state="done", finished=NOW - 600)
        line = self.lines("resume", runs=[run])[1]
        self.assertEqual(line, f"▸ Since you left: {crew.who('Mouse', ROSTER)} finished")
        self.assertIn("QA engineer (Mouse)", line)

    def test_more_finished_members_than_the_line_holds_become_a_count(self):
        run = self.make_run(members=("Tank", "Mouse", "Trinity", "Sati"))
        for lane in run["agents"].values():
            lane.update(state="done", finished=NOW - 600)
        self.assertEqual(self.lines("resume", runs=[run])[1], "▸ Since you left: 4 crew finished")


# --------------------------------------------------------------------------- criteria 5 and 7 to 15


class Honesty(Sandbox):
    def test_hostile_text_loses_escapes_backticks_ids_and_emoji_but_keeps_letters(self):
        q = "\x1b[31mShip `B-fyk3x4xfl6v` to the \u202eauditors\x1b[0m now? \U0001F680"
        lines = self.lines(runs=[self.make_run(decisions=[self.decision(q),
                                                     self.decision("¿Exportar también las anuladas?", "D2")])])
        self.assertEqual(lines[1:3], ["! Decision waits: ship to the auditors now?",
                                      "! Decision waits: ¿Exportar también las anuladas?"])
        self.assertWellFormed(lines)

    def test_devkit_copy_uses_only_ascii_and_the_rail_marks(self):
        os.environ["DEVKIT_GUARD"] = "off"
        run = startup_with_a_run(self)
        for kind in ("startup", "resume", "clear"):
            for ln in self.lines(kind, runs=[run], older=True):
                self.assertTrue(all(32 <= ord(c) < 127 or c in MARKS for c in ln), ln)

    def test_a_failed_needs_source_never_says_nothing_needs_you(self):
        with mock.patch.object(progress, "pending_decisions", side_effect=OSError("runs unreadable")):
            lines = self.lines(runs=[self.make_run(decisions=[self.decision()])])
        self.assertEqual(lines[0], "devkit · billing · see dev status")
        self.assertFalse([ln for ln in lines if ln.startswith("! ")], lines)
        self.assertEqual(lines[-1], "· feat/invoice-export · 3 changed files")

    def test_with_every_source_failing_the_welcome_is_the_headline(self):
        repo = self.repo()
        repo["main"] = "unreadable"
        boom = OSError("unreadable")
        patches = [mock.patch.object(m, n, side_effect=boom) for m, n in (
            (cli, "status_view"), (cli, "_crew_now"), (progress, "active_runs"), (progress, "pending_decisions"),
            (progress, "waiting_messages"), (prompt, "checks_detail"), (ciwatch, "watches"),
            (updatecheck, "read_cache"), (ext, "pending_acceptances"), (registry, "alias_map"),
            (core, "note_path"), (continuity, "read"))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for kind in ("startup", "resume", "clear", "compact"):
            self.assertIn(self.text(kind, repo=repo, runs=None), ("devkit · billing · see dev status", ""), kind)
        self.assertEqual(self.text(repo=repo, runs=None), "devkit · billing · see dev status")

    def test_an_unreadable_project_gives_no_welcome(self):
        with mock.patch.object(registry, "inspect_repo", side_effect=OSError("git failed")):
            out = welcome.text("claude", "startup", str(self.dir), data={"session_id": SID}, repo=None,
                               top=str(self.dir), reg=self.reg(), runs=[], roster=ROSTER, now=NOW)
        self.assertEqual(out, "")

    def test_ci_comes_from_recorded_state_with_no_network_and_no_process(self):
        self.ci_watch("failed", failed="unit tests", ts=NOW - 3600)
        boom = AssertionError("the welcome must not reach the network or start a process")
        with mock.patch.object(cistatus, "runs_for", side_effect=boom), \
                mock.patch.object(cistatus, "_http", side_effect=boom), \
                mock.patch.object(registry, "prs", side_effect=boom), \
                mock.patch.object(subprocess, "run", side_effect=boom), \
                mock.patch.object(subprocess, "Popen", side_effect=boom):
            lines = self.lines(runs=[self.make_run()])
        self.assertEqual(lines[-1], "· feat/invoice-export · 3 changed files · CI failed: unit tests")

    def test_a_running_record_hours_old_is_not_shown_as_running(self):
        self.ci_running(at=NOW - 5 * 3600, watched=False)
        self.assertEqual(self.lines()[-1], "· feat/invoice-export · 3 changed files")

    def test_the_inspection_context_for_made_is_reused(self):
        with mock.patch.object(registry, "inspect_repo", side_effect=AssertionError("inspected twice")):
            lines = self.lines(runs=[startup_with_a_run(self)])
        self.assertEqual(lines[0], "devkit · billing · 1 thing needs you")

    def test_claude_and_codex_get_the_same_welcome(self):
        self.ci_running()
        runs = [self.make_run(owner=OTHER, decisions=[self.decision()])]
        claude, codex = self.text(runs=runs), self.text(agent="codex", runs=runs)
        self.assertEqual(claude, codex)
        self.assertIn("! Decision waits in another session: export cancelled invoices too?", claude)
        self.assertIn("· in another session", claude)

    def test_headless_runs_crew_sessions_and_scratch_repositories_get_no_welcome(self):
        real = str(core.DEV / "acme" / "billing")  # a project folder, not a scratch one
        self.assertFalse(core.is_temp(real))
        self.assertTrue(welcome.wanted("claude", {"session_id": SID}, real))
        self.assertTrue(welcome.wanted("codex", {"session_id": SID}, ""))  # outside a project: the workspace
        self.assertFalse(welcome.wanted("claude", {"session_id": SID, "agent_id": "a1"}, real))
        self.assertFalse(welcome.wanted("codex", {"session_id": SID}, str(self.dir)))  # a temp scratch repository
        os.environ["DEVKIT_HEADLESS"] = "1"
        self.assertFalse(welcome.wanted("claude", {"session_id": SID}, real))
        del os.environ["DEVKIT_HEADLESS"]
        os.environ["DEVKIT_FROM_AGENT"] = "lane1"
        self.assertFalse(welcome.wanted("claude", {"session_id": SID}, real))
        del os.environ["DEVKIT_FROM_AGENT"]
        self.session(SID, surface="claude -p")
        self.assertFalse(welcome.wanted("claude", {"session_id": SID}, real))

    def test_the_update_line_shows_under_the_budget_and_marks_the_day(self):
        self.update_due()
        lines = self.lines(repo=self.repo(branch="main", modified=0))
        self.assertEqual(lines[-1], "+ devkit 99.1.0 is out: run dev update in a terminal")
        self.assertEqual(updatecheck.read_cache().get("notified"), TODAY)
        self.assertEqual([ln for ln in self.lines(repo=self.repo(branch="main", modified=0))
                          if ln.startswith("+ ")], [])  # once a day

    def test_the_model_reads_the_welcome_once_with_the_rule_not_to_repeat_it(self):
        shown = "devkit · billing · nothing needs you\n· main · no changes"
        para = welcome.for_model(shown)
        self.assertEqual(para.count("The developer saw this devkit welcome:"), 1)
        self.assertIn("devkit · billing · nothing needs you / · main · no changes", para)
        self.assertIn("Do not repeat it in your first reply", para)
        self.assertEqual(welcome.for_model(""), "")

    def test_devkit_notices_become_short_need_lines_in_the_spec_order(self):
        os.environ["DEVKIT_GUARD"] = "off"
        self.write(ext.accepted_file(), {"pending": [{"id": "mcp:a", "what": "a", "at": NOW},
                                                     {"id": "mcp:b", "what": "b", "at": NOW}]})
        repo = self.repo()
        repo["canonical_ok"] = False
        needs = welcome.needs("claude", str(self.dir), data={"session_id": SID}, repo=repo,
                              runs=[self.make_run(decisions=[self.decision()])], older=True, ext_changed=True,
                              ext_ack=True, now=NOW)
        self.assertEqual(needs, [
            "The guard is off in this session: restart without it",
            "This session runs an older devkit: restart it",
            "Decision waits: export cancelled invoices too?",
            "2 extensions accepted since your last check: dev ext ack",
            "Check that the last dev ext ack was yours: dev audit --action ext",
            "Extensions changed outside devkit: dev ext ls",
            "This project is outside devkit's folder layout: dev status explains"])

    def test_a_crew_question_to_the_tech_lead_is_work_in_progress_not_a_need(self):
        msg = {"id": "M3", "from": "lane0", "from_name": "Tank", "to": "main", "kind": "question",
               "text": "may I change services/api.py?", "ts": NOW - 60}
        lines = self.lines(runs=[self.make_run(members=("Tank",), messages=[msg])])
        self.assertEqual(lines[0], "devkit · billing · work in progress")
        self.assertFalse([ln for ln in lines if ln.startswith("! ") or "services/api.py" in ln], lines)


# --------------------------------------------------------------------------- the edge cases of the spec


class EdgeCases(Sandbox):
    def test_without_a_note_or_status_block_no_last_or_next_line_shows(self):
        lines = self.lines(repo=self.repo(branch="main", modified=0))
        self.assertEqual(lines, ["devkit · billing · nothing needs you", "· main · no changes"])

    def test_a_detached_head_shows_the_short_commit(self):
        lines = self.lines(repo=self.repo(branch=None, modified=2))
        self.assertEqual(lines[-1], "· detached at 1a2b3c4 · 2 changed files")

    def test_a_brand_new_project_invites_the_developer_to_start(self):
        repo = self.repo(name="invoice-app", pid="acme/invoice-app", branch="main", modified=0, head="",
                         upstream=None, last_commit_ts=0)
        self.assertEqual(self.lines(repo=repo), ["devkit · invoice-app · new project",
                                                 "› No history yet: say what you want to build",
                                                 "· main · no commits yet"])

    def test_a_run_of_another_live_session_says_so_before_the_crew_count(self):
        line = self.lines(runs=[self.make_run(owner=OTHER, members=("Tank",))])[1]
        self.assertTrue(line.startswith("▸ Invoice export · step 3 of 7 · ~"), line)
        self.assertTrue(line.endswith(" left · in another session"), line)
        self.assertNotIn("crew working", line)

    def test_a_project_named_devkit_keeps_the_speaker_then_the_project(self):
        repo = self.repo(name="devkit", pid="acme/devkit", branch="main", modified=0)
        self.assertEqual(self.lines(repo=repo)[0], "devkit · devkit · nothing needs you")

    def test_the_short_mode_shows_the_headline_and_the_first_need(self):
        lines = self.lines(runs=[startup_with_a_run(self)], mode="short")
        self.assertEqual(lines, ["devkit · billing · 1 thing needs you",
                                 "! Decision waits: export cancelled invoices too?"])


if __name__ == "__main__":
    unittest.main()
