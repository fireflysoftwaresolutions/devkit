# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev lesson` as a terminal runs it: lint lists the anchors to fix by kind, eval says whether recall showed a lesson
# before each repeat, supersede and release change a lesson's place in recall, add refuses a second lesson for the same
# mistake unless `--new` says why, points devkit's own friction to the bug registry and names who recorded it (the
# crew member of the lane, the tech lead, or the developer by their git name), review shows what reached
# agents and what to act on, and prevent turns a lesson into a lesson rule the guard enforces. Each test drives cli.main
# in a checkout of its own, over a temporary handoff repository and devkit state.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_lessons_cli
import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import audit, cli, core, lessonevents, lessonrules, lessons, work  # noqa: E402
from devkit import progress as pg  # noqa: E402
from test_lessons_base import A, LessonCase  # noqa: E402

SID = "4f2a9c1d-7b3e-4c55-9a0e-1d2c3b4a5f60"
WHY = ["--cause", "the step ran before its input existed", "--prevent", "wait for the input before the step"]


class LessonCLI(LessonCase):
    def setUp(self):
        super().setUp()
        state = self.root / "state"
        for obj, name, value in ((lessonevents, "EVENTS", state / "lessons-events.jsonl"),
                                 (lessonrules, "STATE", state), (lessonrules, "HITS", state / "rule-hits.json"),
                                 (core, "STATE", state), (core, "DEV", self.root / "dev"),
                                 (audit, "AUDIT", self.root / "audit")):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cwd = self.checkout("acme/api")

    def checkout(self, pid):
        # a clone whose origin is github.com/<pid>: `dev lesson` takes the scope from the folder it runs in
        top = self.root / "src" / pid.replace("/", "-")
        top.mkdir(parents=True)
        for args in (("init", "-q"), ("remote", "add", "origin", f"https://github.com/{pid}.git")):
            subprocess.run(["git", "-C", str(top), *args], check=True, capture_output=True)
        return top

    def dev(self, *argv, cwd=None):
        out, here = io.StringIO(), os.getcwd()
        os.chdir(cwd or self.cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = cli.main(["lesson", *argv])
        except SystemExit as e:  # argparse refuses an argument it does not know
            rc = e.code
        finally:
            os.chdir(here)
        return rc, out.getvalue()

    def block(self, out, heading):
        # The lines under a heading (a line ruled with ─), up to the next heading
        lines, got, inside = out.splitlines(), [], False
        for ln in lines:
            if "───" in ln:
                inside = heading in ln
                continue
            if inside:
                got.append(ln)
        self.assertTrue(any(heading in ln for ln in lines if "───" in ln), f"no heading “{heading}” in:\n{out}")
        return "\n".join(got)

    def line(self, text, needle):
        found = [ln for ln in text.splitlines() if needle in ln]
        self.assertTrue(found, f"no line with {needle} in:\n{text}")
        return found[0]

    def front(self, les, key, value):
        # A lesson written before today's capture checks: its front matter changed on disk as an older devkit wrote it
        path = Path(les["path"])
        path.write_text("\n".join(f"{key}: {value}" if ln.startswith(key + ":") else ln
                                  for ln in path.read_text().split("\n")))
        return lessons.load(path)


class LintTests(LessonCLI):
    def test_lint_lists_each_finding_under_its_kind_with_the_command_that_fixes_it(self):
        bare = self.front(self.add("Run probes from a file", commands=["python3 -c"]), "commands", "[python3]")
        loose = self.add("Name the scratch folder in the brief", scope="global")
        rc, out = self.dev("lint")
        self.assertEqual(rc, 1, out)
        broad = self.block(out, "broad")
        self.assertIn(bare["meta"]["id"], broad)
        self.assertIn(f'dev lesson anchor {bare["meta"]["id"]} --command "python3', broad)
        self.assertNotIn(loose["meta"]["id"], broad)
        missing = self.block(out, "missing")
        self.assertIn(loose["meta"]["id"], missing)
        self.assertIn(f"dev lesson anchor {loose['meta']['id']} --file", missing)

    def test_a_scope_option_lints_that_scope_only_and_a_clean_scope_passes(self):
        self.front(self.add("Run probes from a file", commands=["python3 -c"]), "commands", "[python3]")
        self.add("Wait for the server before the probe", scope="global", errors=["connection refused on 8080"])
        rc, out = self.dev("lint", "--global")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("python3", out)
        self.assertIn("nothing to fix", out)

    def test_a_file_anchor_is_checked_against_its_own_projects_checkout(self):
        app = core.DEV / "acme" / "api" / "src" / "app.py"
        app.parent.mkdir(parents=True)
        app.write_text("print('hi')\n")
        kept = self.add("Keep the entry point small", files=["src/app.py"])
        gone = self.add("Keep the old entry point small", files=["src/old_app.py"])
        elsewhere = self.add("Keep any entry point small", scope="global", files=["src/old_app.py"])
        rc, out = self.dev("lint")
        no_file = self.block(out, "no-file")
        self.assertIn(gone["meta"]["id"], no_file)
        self.assertNotIn(kept["meta"]["id"], out)
        self.assertNotIn(elsewhere["meta"]["id"], out)  # a global lesson has no checkout to check against


class EvalTests(LessonCLI):
    def test_eval_says_for_each_repeat_whether_recall_showed_the_lesson_before_it(self):
        shown = self.add("Wait for the server before the probe", scope="global")["meta"]["id"]
        blind = self.add("Close the port after the probe", scope="global")["meta"]["id"]
        lessonevents.record("shown", shown, "brief", "claude", SID, now=time.time() - 60)
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{SID}"}):
            for lid in (shown, blind):
                audit.record("dev.lesson", f"recur {lid}", "", "ok")
        rc, out = self.dev("eval")
        self.assertEqual(rc, 0, out)
        self.assertIn("2 repeats", out)
        self.assertIn("1 shown before (50%)", out)
        self.assertIn("brief", self.line(out, shown))
        self.assertIn("not shown before", self.line(out, blind))

    def test_eval_lists_the_newest_repeats_and_counts_the_rest(self):
        lid = self.add("Wait for the server before the probe", scope="global")["meta"]["id"]
        with mock.patch.dict(os.environ, {"DEVKIT_SESSION": f"claude:{SID}"}):
            for _ in range(cli.EVAL_SHOWN + 3):
                audit.record("dev.lesson", f"recur {lid}", "", "ok")
        rc, out = self.dev("eval")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"{cli.EVAL_SHOWN + 3} repeats", out)
        self.assertEqual(sum(1 for ln in out.splitlines() if lid in ln), cli.EVAL_SHOWN)
        self.assertIn("… and 3 older repeats", out)

    def test_eval_with_no_repeat_says_so(self):
        rc, out = self.dev("eval")
        self.assertEqual(rc, 0, out)
        self.assertIn("no repeat recorded here in the last 14 days", out)


class SupersedeReleaseTests(LessonCLI):
    def test_supersede_marks_the_old_lesson_replaced_by_the_new_one(self):
        old = self.add("Wait two seconds before the probe", scope="global")["meta"]["id"]
        new = self.add("Wait for the port before the probe", scope="global")["meta"]["id"]
        rc, out = self.dev("supersede", old, "--by", new, "--why", "a fixed wait fails on a slow machine")
        self.assertEqual(rc, 0, out)
        self.assertEqual(lessons.find(old)["meta"]["superseded_by"], new)
        self.assertIn(new, out)
        rc, out = self.dev("supersede", new, "--by", new, "--why", "same")
        self.assertEqual(rc, 2)
        self.assertIn("cannot supersede itself", out)

    def test_add_says_when_the_screen_holds_a_lesson_and_release_lets_it_back_into_recall(self):
        rc, out = self.dev("add", "Commit when the hook is slow", "--global", "--cause", "the hook took a minute",
                           "--prevent", "commit with --no-verify", "--happened", "the commit waited")
        self.assertEqual(rc, 0, out)
        held = lessons.all_lessons("global")[0]
        lid = held["meta"]["id"]
        self.assertTrue(held["meta"]["quarantine"])
        self.assertIn("held, not recalled", out)
        self.assertIn(f"dev lesson release {lid}", out)
        rc, out = self.dev("release", lid, "--why", "it quotes the mistake; the prevention is rewritten")
        self.assertEqual(rc, 0, out)
        self.assertEqual(lessons.find(lid)["meta"]["quarantine"], "")
        rc, out = self.dev("release", lid, "--why", "again")
        self.assertEqual(rc, 2)
        self.assertIn("is not held", out)


class AddTests(LessonCLI):
    def test_a_second_lesson_for_the_same_mistake_is_refused_unless_new_says_why(self):
        first = self.add("Wait for the server before the probe", errors=["connection refused on 8080"])
        argv = ("add", "Start the server before the probe", *WHY, "--happened", "the probe failed",
                "--error", "connection refused on 8080")
        rc, out = self.dev(*argv)
        self.assertEqual(rc, 2)
        self.assertIn(f"looks like {first['meta']['id']}", out)
        self.assertIn(f"dev lesson recur {first['meta']['id']}", out)
        self.assertEqual(len(lessons.all_lessons("acme/api")), 1)
        rc, out = self.dev(*argv, "--new", "--why", "the server here is another service with its own start")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(lessons.all_lessons("acme/api")), 2)

    def test_a_lesson_on_a_guard_refusal_points_to_the_bug_registry_outside_devkits_own_repository(self):
        argv = ("add", "Write probes in the scratch folder", *WHY, "--happened", "the guard refused it",
                "--error", "is not in your scratch folder")
        rc, out = self.dev(*argv)
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(lessons.all_lessons("acme/api")), 1)  # recorded; the hint does not refuse
        self.assertIn("dev bug report --guard", out)
        rc, out = self.dev("add", "Copy the build output after the step", *WHY, "--happened", "the hook refused it",
                           "--error", "PreToolUse:Bash hook error: the copy step")  # the denial line itself
        self.assertEqual(rc, 0, out)
        self.assertIn("dev bug report --guard", out)
        rc, out = self.dev(*argv, cwd=self.checkout(core.DEVKIT_REPO))  # devkit's friction is devkit's own lesson
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(lessons.all_lessons(core.DEVKIT_REPO)), 1)
        self.assertNotIn("dev bug report", out)

    def test_a_lesson_on_a_dev_command_points_to_the_bug_registry_and_a_projects_own_error_does_not(self):
        rc, out = self.dev("add", "Name the step when it closes", *WHY, "--happened", "the step stayed open",
                           "--command", "dev progress step")
        self.assertEqual(rc, 0, out)
        self.assertIn("dev bug report", out)
        rc, out = self.dev("add", "Check the list before indexing it", *WHY, "--happened", "the import crashed",
                           "--error", "IndexError: list index out of range")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("dev bug report", out)


class ReadTests(LessonCLI):
    def test_recur_of_a_lesson_that_left_after_find_is_a_clear_error_not_a_crash(self):
        # the same race the listing survives, between find and the write: update raised FileNotFoundError, which
        # cmd_lesson does not catch, so the agent saw a crash notice and exit 70
        lid = self.add("Wait for the input before the step", commands=["make deploy"])["meta"]["id"]
        real_find = lessons.find

        def find_then_vanish(sel, scope=None):
            les = real_find(sel, scope)
            Path(les["path"]).unlink()  # the hourly sync's aborted rebase takes the file away again
            return les
        with mock.patch.object(lessons, "find", find_then_vanish):
            rc, out = self.dev("recur", lid, "--evidence", "it happened again")
        self.assertEqual(rc, 2, out)
        self.assertNotIn("Traceback", out)
        self.assertIn("run the command again", out)

    def test_one_malformed_lesson_file_leaves_dev_lesson_working(self):
        good = self.add("Wait for the input before the step", commands=["make deploy"])["meta"]["id"]
        bad = lessons.LESSONS / "acme/api" / "L-broken000.md"
        bad.write_text("---\nid: L-broken000\ntitle: x\nscope: acme/api\noccurrences: two\n---\n# x\n")
        rc, out = self.dev("ls")
        self.assertEqual(rc, 0, out)
        self.assertIn(good, out)
        rc, out = self.dev("recur", "L-broken000", "--evidence", "again")
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot be read", out)  # the selector names a file devkit cannot read: say so, not "0 lessons"

    def test_a_lesson_missing_while_the_handoff_has_stopped_syncing_says_why(self):
        # lessons recorded on another Mac never arrive while every pull aborts; `dev lesson` said only "matches 0"
        remote, other = self.root / "remote.git", self.root / "other"
        git = ["git", "-c", "user.email=t@example.com", "-c", "user.name=t"]
        for args in (["init", "-q", "--bare", "-b", "main", str(remote)], ["clone", "-q", str(remote), str(other)],
                     ["-C", str(other), "commit", "-q", "--allow-empty", "-m", "first"],
                     ["-C", str(other), "push", "-q", "origin", "HEAD:main"],
                     ["clone", "-q", str(remote), str(lessons.HANDOFF)],
                     ["-C", str(other), "commit", "-q", "--allow-empty", "-m", "L-g6i3aj1j5sf: lesson"],
                     ["-C", str(other), "push", "-q", "origin", "HEAD:main"],
                     ["-C", str(lessons.HANDOFF), "fetch", "-q"]):
            subprocess.run(git + args, check=True, capture_output=True)
        work.sync_problem("a pull of the handoff repository stopped on a conflict in log/2026-11.md",
                          "handoff.rebase-aborted")
        rc, out = self.dev("recur", "L-g6i3aj1j5sf", "--evidence", "again")
        self.assertEqual(rc, 2, out)
        self.assertIn("matches 0 lessons", out)
        self.assertIn("has not synced", out)
        self.assertIn("1 commit", out)


class AuthorTests(LessonCLI):
    # Who recorded a lesson, as recall names it: the crew member whose lane runs `dev lesson add` (found the way the
    # hooks record its call), the tech lead in the main agent session, or the developer by their git name in a
    # terminal. A run in this session has a lane for the DevEx engineer (Kid).
    def setUp(self):
        super().setUp()
        state = self.root / "state"
        for name, value in (("RUNS", state / "runs"), ("SESSIONS", state / "sessions"), ("CALLS", state / "calls")):
            p = mock.patch.object(pg, name, value)
            p.start()
            self.addCleanup(p.stop)
        pg.RUNS.mkdir(parents=True)
        (pg.RUNS / f"claude-{SID}.json").write_text(json.dumps({
            "v": 2, "agent": "claude", "session": SID, "goal": "Hotfix", "state": "running", "intervals": [],
            "plan": [], "agents": {"a_kid": {"persona": "Kid", "state": "running", "parent": "main"}}}))
        agent_env = {k: v for k, v in os.environ.items() if k not in core.AGENT_MARKERS}
        self.env = dict(agent_env, CLAUDECODE="1", DEVKIT_SESSION=f"claude:{SID}")
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Ana Pérez\n")
        p = mock.patch.dict(core.GIT_ENV, {"GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"})
        p.start()
        self.addCleanup(p.stop)

    def add_as(self, caller, developer=False, env=None):
        # `dev lesson add` as the shell call of `caller` (a lane's agent id, or main) that the hooks hold in the call
        # ledger while it runs; `developer`: run in the developer's own terminal instead
        argv = ["lesson", "add", "Wait for the database before the migration", *WHY, "--happened", "it timed out",
                "--error", "connection to the database timed out"]
        calls = pg.CALLS / SID
        calls.mkdir(parents=True, exist_ok=True)
        (calls / "toolu_1.json").write_text(json.dumps({"tool_use_id": "toolu_1", "caller": caller,
                                                        "ts": time.time(), "ttl": 3600, "cmd": "dev " + " ".join(
                                                            shlex.quote(x) for x in argv)}))
        with mock.patch.dict(os.environ, env if env is not None else self.env, clear=True), \
                mock.patch.object(sys, "argv", ["dev", *argv]), \
                mock.patch.object(core, "set_by_developer", lambda *a, **k: developer):
            rc, out = self.dev(*argv[1:])
        self.assertEqual(rc, 0, out)
        return lessons.recorded_by(lessons.all_lessons("acme/api")[0])

    def test_a_lesson_a_crew_member_adds_names_its_role_and_name(self):
        self.assertEqual(self.add_as("a_kid"), "DevEx engineer (Kid)")

    def test_a_lesson_the_main_agent_session_adds_names_the_tech_lead(self):
        self.assertEqual(self.add_as("main"), "Tech lead (Morpheus)")

    def test_a_lesson_the_developer_adds_in_a_terminal_names_them_by_their_git_name(self):
        terminal = {k: v for k, v in self.env.items() if k not in core.AGENT_MARKERS}
        self.assertEqual(self.add_as("main", developer=True, env=terminal), "Ana Pérez")


class ReviewTests(LessonCLI):
    def test_review_shows_how_often_shown_lessons_were_heeded_per_channel_and_the_most_missed(self):
        missed = self.add("Wait for the server before the probe", scope="global")["meta"]["id"]
        heeded = self.add("Close the port after the probe", scope="global")["meta"]["id"]
        lessonevents.record("shown", missed, "session", "claude", "s1")
        lessonevents.on_hit([missed], "s1", "claude")
        lessonevents.record("shown", heeded, "brief", "neo", "s2")
        lessonevents.on_end("s2", "neo")
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        rates = self.block(out, "heeded")
        session, brief = self.line(rates, "session"), self.line(rates, "brief")
        self.assertIn("1 missed", session)
        self.assertIn("0% heeded", session)
        self.assertIn("1 heeded", brief)
        self.assertIn("100% heeded", brief)
        self.assertIn(missed, self.line(rates, "missed most"))
        self.assertNotIn(heeded, self.line(rates, "missed most"))

    def test_review_lists_lessons_to_automate_failed_preventions_and_held_lessons(self):
        missed = self.add("Wait for the server before the probe", scope="global")["meta"]["id"]
        lessons.track([missed], "missed")
        lessons.track([missed], "missed")
        failed = self.add("Close the port after the probe", scope="global")
        lessons.prevent(failed, "test", "tests/test_probe.py::test_port_closes", "a test covers it", a=A)
        lessons.recur(lessons.find(failed["meta"]["id"]), "the port stayed open again", a=A)
        held = self.add("Commit when the hook is slow", scope="global", prevention="commit with --no-verify")
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        self.assertIn(missed, self.block(out, "automate"))
        prevention = self.line(self.block(out, "Preventions that failed"), failed["meta"]["id"])
        self.assertIn("tests/test_probe.py::test_port_closes", prevention)
        hold = self.block(out, "Held")
        self.assertIn(held["meta"]["id"], hold)
        self.assertIn(f"dev lesson release {held['meta']['id']}", hold)


class PreventByRuleTests(LessonCLI):
    def setUp(self):
        super().setUp()
        self.les = self.add("Run probes from a file, not inline", scope="global", commands=["python3 -c"])
        self.lid = self.les["meta"]["id"]

    def test_prevent_by_guard_with_a_pattern_proposes_a_warning_rule_and_records_it(self):
        rc, out = self.dev("prevent", self.lid, "--by", "guard", "--match", "python3 -c",
                           "--why", "agents keep running inline probes")
        self.assertEqual(rc, 0, out)
        rules = lessonrules.load(["global"])
        self.assertEqual([(r["lesson"], r["kind"], r["match"], r["mode"]) for r in rules],
                         [(self.lid, "call", "python3 -c", "warn")])
        m = lessons.find(self.lid)["meta"]
        self.assertEqual((m["status"], m["prevention"], m["prevention_ref"]), ("prevented", "guard", rules[0]["id"]))
        self.assertIn(rules[0]["id"], out)

    def test_block_asks_for_a_rule_that_refuses_at_once(self):
        rc, out = self.dev("prevent", self.lid, "--by", "guard", "--match", "python3 -c", "--block",
                           "--why", "the developer wants it refused")
        self.assertEqual(rc, 0, out)
        self.assertEqual([r["mode"] for r in lessonrules.load(["global"])], ["block"])

    def test_a_rule_the_guard_cannot_accept_is_refused_with_its_reason_and_nothing_changes(self):
        rc, out = self.dev("prevent", self.lid, "--by", "guard", "--match", "git status", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("matches none of", out)
        self.assertEqual(lessonrules.load(["global"]), [])
        self.assertEqual(lessons.find(self.lid)["meta"]["status"], "open")

    def test_a_pattern_needs_by_guard_and_by_guard_needs_a_reference_or_a_pattern(self):
        rc, out = self.dev("prevent", self.lid, "--by", "test", "--match", "python3 -c", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--by guard", out)
        rc, out = self.dev("prevent", self.lid, "--by", "guard", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--match", out)
        self.assertEqual(lessonrules.load(["global"]), [])
        self.assertEqual(lessons.find(self.lid)["meta"]["status"], "open")


if __name__ == "__main__":
    unittest.main()
