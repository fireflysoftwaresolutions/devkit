# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The SessionStart hook sends the welcome (lib/devkit/welcome.py) to the developer: the system message on Claude Code
# and on Codex, while the model reads first that the developer saw it. These tests run the real handler on a real
# repository at its canonical place in the test home (outside the temp folders, as a project on a Mac is), with
# devkit's state in temporary folders and no network.
# Run: python3 -m unittest tests.test_hooks_welcome
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import ciwatch, cistatus, cli, continuity, core, crew, ext, hooks, progress, prompt, registry  # noqa: E402
from devkit import updatecheck, welcome, work  # noqa: E402
from test_progress import TempState  # noqa: E402

ROSTER = crew.load(team=False)
OWNER = "welcome-hook"
OLDER = ("devkit · this session runs devkit 26.11.5, older than the pinned 26.11.7, so it repaired nothing; restart "
         "the session to run the pinned release.")


def git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


def make_repo(repo: Path):
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", f"https://github.com/{OWNER}/{repo.name}.git")
    (repo / "README.md").write_text(f"{repo.name}\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-q", "-m", "first")


class WelcomeHook(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.root = Path(self.state.tmp.name)
        self.repo = core.DEV / OWNER / "billing"  # its canonical place: the folder layout needs nothing
        shutil.rmtree(self.repo.parent, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.repo.parent, True)
        make_repo(self.repo)
        handoff = self.root / "handoff"
        for obj, name, value in (
                (hooks, "STATE", self.root), (hooks, "HANDOFF", handoff), (core, "STATE", self.root),
                (core, "HANDOFF", handoff), (core, "NOTES", handoff / "projects"), (registry, "STATE", self.root),
                (registry, "HANDOFF", handoff),
                (work, "HANDOFF", handoff), (ciwatch, "WATCH", self.root / "ci" / "watch"),
                (ciwatch, "LIVE", self.root / "ci" / "live"), (cistatus, "CACHE", self.root / "cache" / "ci"),
                (updatecheck, "CACHE", self.root / "update-check.json"), (continuity, "CONT", self.root / "cont"),
                (hooks, "_spawn", lambda *a, **k: None), (registry, "prs", lambda *a, **k: None),
                (crew, "load", lambda *a, **k: ROSTER),
                # the empty test home lacks the required plugins: their drift would add a need to every welcome
                (ext, "session_line", lambda *a, **k: None)):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch("devkit.work.request_sync", lambda *a, **k: None), mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("DEVKIT_GUARD", "DEVKIT_HEADLESS", "DEVKIT_FROM_AGENT", "CLAUDE_CODE_ENTRYPOINT"):
            os.environ.pop(k, None)
        self.pid = progress._project(str(self.repo))

    def run_hook(self, agent: str, sid: str, cwd=None, **more) -> tuple:
        payload = dict({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup",
                        "cwd": str(cwd or self.repo)}, **more)
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out):
            rc = hooks.run("session-start", agent)
        return rc, out.getvalue()

    def start(self, agent="claude", sid="s9", **more) -> dict:
        rc, out = self.run_hook(agent, sid, **more)
        self.assertEqual(rc, 0)
        try:
            return json.loads(out)
        except ValueError:
            log = self.root / "logs" / "hooks.log"
            self.fail(f"the hook printed no JSON: {out[:300]!r}; hooks.log: "
                      f"{log.read_text()[-1500:] if log.exists() else '-'}")

    def live_run(self, decision=True):
        """A run of this project in another live session (s0), at step 3 of 7, with a decision for the developer."""
        now = time.time()
        (progress.SESSIONS / "claude-s0.json").write_text(json.dumps(
            {"agent": "claude", "session": "s0", "pid": os.getpid(), "started": now - 7200}))
        plan = [{"name": f"step {i}", "state": "done", "est": 10, "started": now - 7000, "finished": now - 6000}
                for i in (1, 2)]
        plan += [{"name": "export part", "state": "running", "owner": "lane0", "est": 20, "started": now - 900}]
        plan += [{"name": n, "state": "pending", "est": 5} for n in ("wire the export endpoint", "docs", "review",
                                                                     "release")]
        decisions = [{"id": "D1", "from": "lane0", "from_name": "Mouse", "question": "Export cancelled invoices too?",
                      "options": ["yes", "no"], "state": "pending", "ts": now - 300}] if decision else []
        progress.RUNS.mkdir(parents=True, exist_ok=True)
        (progress.RUNS / "claude-s0.json").write_text(json.dumps(
            {"v": 2, "agent": "claude", "session": "s0", "project": self.pid, "goal": "Invoice export",
             "state": "running", "started": now - 7200, "updates": [{"ts": now - 120, "kind": "step", "text": "x"}],
             "intervals": [], "plan": plan, "decisions": decisions, "messages": [], "mode": "guided",
             "agents": {"lane0": {"label": "Mouse: export part", "persona": "Mouse", "state": "running",
                                  "started": now - 900, "updates": [{"ts": now - 60, "text": "working"}]}}}))


    def end(self, agent: str, sid: str):
        """The session ended, as the SessionEnd hook records it."""
        f = progress.session_path(agent, sid)
        f.write_text(json.dumps(dict(json.loads(f.read_text()), ended={"ts": time.time(), "reason": "exit"})))


class Wiring(WelcomeHook):
    # one inspection of the repository, and the same welcome for both agents
    def test_claude_and_codex_get_the_same_welcome_from_one_inspection(self):
        self.live_run()
        with mock.patch.object(registry, "inspect_repo", wraps=registry.inspect_repo) as inspect:
            claude = self.start("claude", "c1")
        self.assertEqual(inspect.call_count, 1)
        self.end("claude", "c1")  # one session at a time: a live one in the same checkout is an overlap (below)
        codex = self.start("codex", "x1")
        self.assertEqual(claude["systemMessage"], codex["systemMessage"])
        lines = claude["systemMessage"].splitlines()
        self.assertLessEqual(len(lines), 5)
        self.assertEqual(lines[:2], ["devkit · billing · 1 thing needs you",
                                     "! Decision waits in another session: export cancelled invoices too?"])
        self.assertTrue(lines[2].startswith("▸ Invoice export · step 3 of 7 · ~"), lines[2])
        self.assertTrue(lines[-1].startswith("· main · "), lines[-1])

    # A Codex session that starts while a Claude Code session is live in the same checkout hears it as a need line
    def test_a_second_live_session_in_the_same_checkout_is_named(self):
        self.start("claude", "c1")
        lines = self.start("codex", "x1")["systemMessage"].splitlines()
        self.assertEqual(lines[0], "devkit · billing · 1 thing needs you")
        self.assertIn("! Claude Code works in this same checkout", lines)

    # the model reads first, once, that the developer saw the welcome, before any devkit notice
    def test_the_model_reads_first_that_the_developer_saw_the_welcome(self):
        os.environ["DEVKIT_GUARD"] = "off"
        with mock.patch.object(hooks, "_older_copy_notice", lambda *a: OLDER):
            out = self.start("codex")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.split("\n\n", 1)[0], welcome.for_model(out["systemMessage"]))
        self.assertTrue(ctx.startswith("The developer saw this devkit welcome: devkit · billing · "), ctx[:200])
        self.assertEqual(ctx.count("The developer saw this devkit welcome:"), 1)
        self.assertIn("DEVKIT_GUARD=off", ctx)
        self.assertIn("## devkit · workspace context", ctx)

    # devkit's notices reach the developer as short need lines; their sentences stay with the model
    def test_notices_become_need_lines_and_their_sentences_stay_with_the_model(self):
        ext.accepted_file().parent.mkdir(parents=True, exist_ok=True)
        ext.accepted_file().write_text(json.dumps({"pending": [
            {"id": "mcp:a", "what": "alpha (claude mcp)", "at": time.time(), "tty": "/dev/ttys001"},
            {"id": "mcp:b", "what": "beta (claude mcp)", "at": time.time(), "tty": "/dev/ttys001"}]}))
        os.environ["DEVKIT_GUARD"] = "off"
        with mock.patch.object(hooks, "_older_copy_notice", lambda *a: OLDER):
            out = self.start("claude")
        msg, ctx = out["systemMessage"], out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(msg.splitlines()[:3], ["devkit · billing · 3 things need you",
                                                "! The guard is off in this session: restart without it",
                                                "! And 2 more: dev status lists them"])
        for sentence in (OLDER, "extensions accepted since your last session: alpha", "DEVKIT_GUARD=off"):
            self.assertNotIn(sentence, msg)
            self.assertIn(sentence, ctx)

    # no welcome for a headless run, a crew member's session or a scratch repository
    def test_headless_runs_crew_sessions_and_scratch_repositories_get_no_system_message(self):
        with mock.patch.object(hooks, "_older_copy_notice", lambda *a: OLDER):
            crew_member = self.start("codex", "x2", agent_id="a1")
            os.environ["CLAUDE_CODE_ENTRYPOINT"] = "sdk-cli"  # claude -p
            _, printed = self.run_hook("claude", "c3")
            del os.environ["CLAUDE_CODE_ENTRYPOINT"]
            with tempfile.TemporaryDirectory() as tmp:
                make_repo(Path(tmp) / "scratch")
                scratch = self.start("codex", "x4", cwd=Path(tmp) / "scratch")
        self.assertNotIn("systemMessage", crew_member)
        self.assertNotIn("systemMessage", printed)
        self.assertNotIn("systemMessage", scratch)
        self.assertIn("## devkit · workspace context", crew_member["hookSpecificOutput"]["additionalContext"])

    # whatever fails, the hook exits 0 with the context intact, and the verdict stays honest
    def test_failing_sources_never_fail_the_hook(self):
        self.live_run()
        boom = OSError("unreadable")
        with contextlib.ExitStack() as stack:
            for obj, name in ((cli, "status_view"), (prompt, "checks_detail"), (ciwatch, "watches"),
                              (updatecheck, "read_cache"), (progress, "pending_decisions")):
                stack.enter_context(mock.patch.object(obj, name, side_effect=boom))
            out = self.start("codex")
        self.assertTrue(out["systemMessage"].startswith("devkit · billing · see dev status"), out["systemMessage"])
        self.assertIn("## devkit · workspace context", out["hookSpecificOutput"]["additionalContext"])

    def test_without_a_welcome_the_notices_show_as_before(self):
        with mock.patch.object(welcome, "_lines", side_effect=RuntimeError("broken")), \
                mock.patch.object(hooks, "_older_copy_notice", lambda *a: OLDER):
            out = self.start("codex")
        self.assertEqual(out["systemMessage"], OLDER)
        self.assertNotIn("The developer saw this devkit welcome", out["hookSpecificOutput"]["additionalContext"])

    # CI comes from devkit's records; the network is never asked
    def test_ci_comes_from_the_recorded_watch_result_without_the_network(self):
        ciwatch.WATCH.mkdir(parents=True)
        (ciwatch.WATCH / f"{ciwatch._key(f'{OWNER}/billing', 'main')}.json").write_text(json.dumps(
            {"ts": time.time() - 7200, "state": "passed", "repo": f"{OWNER}/billing", "branch": "main"}))
        boom = AssertionError("the session start must not reach the network")
        with mock.patch.object(cistatus, "runs_for", side_effect=boom), \
                mock.patch.object(cistatus, "_http", side_effect=boom):
            out = self.start("claude")
        self.assertTrue(out["systemMessage"].splitlines()[-1].endswith(" · CI passed 2 h ago"), out["systemMessage"])

    # the update line shows only when it fits, and only then is the day marked
    def test_the_update_line_marks_the_day_only_when_it_was_shown(self):
        updatecheck.CACHE.write_text(json.dumps({"checked": 1, "installed": "26.11.6", "newest": "99.1.0"}))
        self.live_run()  # a decision, the run, the next step and the footer fill the five lines
        with mock.patch.object(updatecheck, "installed_version", return_value="26.11.6"):
            crowded = self.start("claude", "u1")
            self.assertNotIn("+ devkit", crowded["systemMessage"])
            self.assertIsNone(updatecheck.read_cache().get("notified"))
            (progress.RUNS / "claude-s0.json").unlink()
            first, second = self.start("claude", "u2"), self.start("claude", "u3")
        self.assertEqual(first["systemMessage"].splitlines()[-1],
                         "+ devkit 99.1.0 is out: run dev update in a terminal")
        self.assertEqual(updatecheck.read_cache().get("notified"), time.strftime("%Y-%m-%d"))
        self.assertNotIn("+ devkit", second["systemMessage"])


if __name__ == "__main__":
    unittest.main()
