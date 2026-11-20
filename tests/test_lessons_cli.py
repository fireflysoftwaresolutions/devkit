# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev lesson` as a terminal runs it: lint lists the anchors to fix by kind, eval says whether recall showed a lesson
# before each repeat, supersede and release change a lesson's place in recall, add refuses a second lesson for the same
# mistake unless `--new` says why, points devkit's own friction to the bug registry and names who recorded it (the
# crew member of the lane, the tech lead, or the developer by their git name), review shows what reached
# agents and what to act on, and prevent turns a lesson into a lesson rule that warns. Each test drives cli.main
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
        loose = self.unanchored("Name the scratch folder in the brief", scope="global")
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
        shown = self.add("Wait for the server before the probe", scope="global", files=["probes/**"])["meta"]["id"]
        blind = self.add("Close the port after the probe", scope="global", files=["ports/**"])["meta"]["id"]
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
        lid = self.add("Wait for the server before the probe", scope="global", files=["probes/**"])["meta"]["id"]
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
        old = self.add("Wait two seconds before the probe", scope="global", files=["probes/**"])["meta"]["id"]
        new = self.add("Wait for the port before the probe", scope="global", files=["probes/**"])["meta"]["id"]
        rc, out = self.dev("supersede", old, "--by", new, "--why", "a fixed wait fails on a slow machine")
        self.assertEqual(rc, 0, out)
        self.assertEqual(lessons.find(old)["meta"]["superseded_by"], new)
        self.assertIn(new, out)
        rc, out = self.dev("supersede", new, "--by", new, "--why", "same")
        self.assertEqual(rc, 2)
        self.assertIn("cannot supersede itself", out)

    def test_add_says_when_the_screen_holds_a_lesson_and_release_lets_it_back_into_recall(self):
        rc, out = self.dev("add", "Commit when the hook is slow", "--global", "--cause", "the hook took a minute",
                           "--prevent", "commit with --no-verify", "--happened", "the commit waited",
                           "--command", "git commit")
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


LONG = "In a crew lane, prove shell expansion of a program word with dev stubs check, never by running it"  # 97


class CaptureRefusalTests(LessonCLI):
    def test_add_reports_every_capture_problem_in_one_refusal(self):
        # each refusal named one problem (the checks stopped at the first), so the Security engineer's lane needed
        # four tries, titles of 97, 83 and 82 characters and then a duplicate, and gave up
        twin = self.add("Prove expansion with dev stubs check", errors=["zsh: no matches found: ./lib/*"])
        rc, out = self.dev("add", LONG, "--happened", "a zsh probe was refused", *WHY, "--command", "python3",
                           "--error", "zsh: no matches found: ./lib/*")
        self.assertEqual(rc, 2, out)
        self.assertIn("`python3` alone matches every python3 call", out)  # the anchor problem
        self.assertIn("the title is 97 characters", out)  # the title problem
        self.assertIn(f"looks like {twin['meta']['id']}", out)  # and the duplicate, all in the same refusal
        self.assertEqual(len(lessons.all_lessons("acme/api")), 1)

    def test_an_unanchored_lesson_is_refused_with_the_anchors_to_give(self):
        # add said "only the session start recalls it", but recall leaves out every lesson with no anchor: 78 live
        # lessons were recorded and never shown
        rc, out = self.dev("add", "Wait for the input before the step", "--happened", "the step ran early", *WHY)
        self.assertEqual(rc, 2, out)
        self.assertIn("never recalled", out)
        for flag in ('--file "<glob>"', '--command "<command words>"', '--error "<text the output shows>"',
                     "--activity <"):
            self.assertIn(flag, out)
        self.assertEqual(lessons.all_lessons("acme/api"), [])

    def test_a_refused_add_records_why_in_the_audit_trail_without_its_text(self):
        # the audit trail kept `detail: ""` for 84 refused adds, so nobody could see why capture kept failing
        rc, out = self.dev("add", LONG, "--happened", "a zsh probe was refused", *WHY, "--command", "python3",
                           "--error", "oops boom")
        self.assertEqual(rc, 2, out)
        rec = audit.query(action="dev.lesson.refused")[-1]
        self.assertEqual((rec["outcome"], rec["category"], rec["target"]), ("blocked", "refusal", "acme/api"))
        self.assertIn("the title is 97 characters", rec["detail"])
        self.assertIn("`python3` alone", rec["detail"])
        self.assertIn("the error anchor is 9 characters", rec["detail"])
        self.assertNotIn("oops boom", json.dumps(audit.query()))  # output text and titles stay out, as for the
        self.assertNotIn("prove shell expansion", json.dumps(audit.query()).lower())  # command line itself


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

    def stalled_handoff(self):
        # The handoff as this Mac had it: another machine pushed a lesson, the fetch brought it, and the rebase that
        # should apply it aborted on a conflict elsewhere (recorded as a sync problem), so HEAD is one commit behind
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

    def test_a_lesson_missing_while_the_handoff_has_stopped_syncing_says_why(self):
        # lessons recorded on another Mac never arrive while every pull aborts; `dev lesson` said only "matches 0"
        self.stalled_handoff()
        rc, out = self.dev("recur", "L-g6i3aj1j5sf", "--evidence", "again")
        self.assertEqual(rc, 2, out)
        self.assertIn("matches 0 lessons", out)
        self.assertIn("has not synced", out)
        self.assertIn("1 commit", out)

    def test_dev_lesson_warns_only_while_the_handoff_has_stopped_syncing(self):
        self.stalled_handoff()
        rc, out = self.dev()
        self.assertEqual(rc, 0, out)
        self.assertIn("has not synced", out)
        self.assertIn("1 commit from other machines", out)
        work.push_failed_file().unlink()  # a push succeeded: the sync works again
        rc, out = self.dev()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("synced", out)

    def test_lint_lists_a_lesson_file_no_command_can_read_first(self):
        self.add("Wait for the input before the step", commands=["make deploy"])
        bad = lessons.LESSONS / "acme/api" / "L-broken000.md"
        bad.write_text("---\nid: L-broken000\ntitle: x\nscope: acme/api\noccurrences: two\n---\n# x\n")
        rc, out = self.dev("lint")
        self.assertEqual(rc, 1, out)
        unreadable = self.block(out, "unreadable")
        self.assertIn("L-broken000", unreadable)
        self.assertIn("occurrences", unreadable)
        self.assertIn("lessons/acme/api/L-broken000.md", unreadable)  # the fix names the file to repair
        self.assertLess(out.index("unreadable ·"), out.index("wrong ·"))  # `make deploy` is named nowhere in it


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
    def test_review_reports_observations_without_an_effectiveness_percentage(self):
        missed = self.add("Wait for the server before the probe", scope="global", files=["probes/**"])["meta"]["id"]
        heeded = self.add("Close the port after the probe", scope="global", files=["ports/**"])["meta"]["id"]
        lessonevents.record("shown", missed, "session", "claude", "s1")
        lessonevents.on_hit([missed], "s1", "claude")
        lessonevents.record("shown", heeded, "brief", "neo", "s2")
        lessonevents.on_end("s2", "neo")
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        rates = self.block(out, "Lesson observations")
        session, brief = self.line(rates, "session"), self.line(rates, "brief")
        self.assertIn("1 missed", session)
        self.assertIn("0 no observed repeat", session)
        self.assertIn("1 unknown", brief)
        self.assertNotIn("% heeded", out)
        self.assertIn(missed, self.line(rates, "missed most"))
        self.assertNotIn(heeded, self.line(rates, "missed most"))

    def test_review_lists_lessons_to_automate_failed_preventions_and_held_lessons(self):
        missed = self.add("Wait for the server before the probe", scope="global", files=["probes/**"])["meta"]["id"]
        lessons.track([missed], "missed")
        lessons.track([missed], "missed")
        failed = self.add("Close the port after the probe", scope="global", files=["ports/**"])
        lessons.update(failed, "prevented by test: tests/test_probe.py::test_port_closes", "legacy declaration", a=A,
                       status="prevented", prevention="test", prevention_ref="tests/test_probe.py::test_port_closes")
        lessons.recur(lessons.find(failed["meta"]["id"]), "the port stayed open again", a=A)
        held = self.add("Commit when the hook is slow", scope="global", prevention="commit with --no-verify",
                        commands=["git commit"])
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        self.assertIn(missed, self.block(out, "automate"))
        prevention = self.line(self.block(out, "Preventions that failed"), failed["meta"]["id"])
        self.assertIn("tests/test_probe.py::test_port_closes", prevention)
        hold = self.block(out, "Held")
        self.assertIn(held["meta"]["id"], hold)
        self.assertIn(f"dev lesson release {held['meta']['id']}", hold)
        # any caller releases a lesson after review: the hint says how, never who may
        self.assertNotIn("developer only", hold)
        self.assertIn(f"dev lesson release {held['meta']['id']} --why “…” once you reviewed it", hold)

    def test_review_lists_a_prevention_without_a_receipt_with_its_reason(self):
        les = self.add("Close the port after the probe", scope="global", files=["ports/**"])
        lessons.prevent(les, "test", "tests/test_probe.py::test_port_closes", "a test covers it", a=A)
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        unverified = self.block(out, "Prevented, not verified")
        self.assertIn("tests/test_probe.py::test_port_closes", self.line(unverified, les["meta"]["id"]))
        self.assertIn("dev test prove --isolated-imports", unverified)
        self.assertNotIn("nothing to act on", out)


class ReviewCauseTests(LessonCLI):
    # a lesson whose root cause is a devkit bug belongs in the bug registry, where the fix is tracked; review never
    # said so, so L-g74y8cy5hma kept recalling a workaround for a defect devkit could fix
    def test_review_lists_a_lesson_whose_cause_is_a_devkit_bug_with_the_registry_command(self):
        bug = self.add("Rerun the proof alone after a crash", files=["tests/**"],
                       cause="devkit bug 460e5e121022 made dev test prove exit 70")
        plain = self.add("Close the port after the probe", files=["ports/**"])
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        causes = self.block(out, "Caused by a devkit bug")
        self.assertIn(bug["meta"]["id"], causes)
        self.assertIn("dev bug report 460e5e121022", causes)
        self.assertNotIn(plain["meta"]["id"], causes)
        self.assertNotIn("nothing to act on", out)

    def test_review_lists_no_cause_section_when_no_cause_names_a_devkit_bug(self):
        self.add("Close the port after the probe", files=["ports/**"], cause="the probe left port 8080 open")
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Caused by a devkit bug", out)


class ReviewUntracedTests(LessonCLI):
    # a lesson with no Fix and no Evidence cannot be traced to a real event; review counts them in one line with the
    # flag that lists them, since a list of them (1,088 of 1,516 lessons on the machine that asked) buries the rest
    def lessons_by_trace(self):
        bare = [self.add(f"Close port {n} after the probe", files=[f"ports/{n}/**"])["meta"]["id"] for n in (1, 2, 3)]
        traced = [self.add("Wait for the server before the probe", files=["probes/**"], evidence=["PR #12"]),
                  self.add("Stop the worker after the probe", files=["workers/**"], fix="stopped the worker")]
        return bare, [x["meta"]["id"] for x in traced]

    def test_review_counts_the_lessons_with_no_fix_and_no_evidence_in_one_line(self):
        bare, traced = self.lessons_by_trace()
        rc, out = self.dev("review")
        self.assertEqual(rc, 0, out)
        counted = [ln for ln in out.splitlines() if "no Fix and no Evidence" in ln]
        self.assertEqual(len(counted), 1, out)
        self.assertIn("3 lessons have no Fix and no Evidence: dev lesson review --untraced lists them", counted[0])
        for lid in bare + traced:
            self.assertNotIn(lid, out)
        self.assertNotIn("nothing to act on", out)

    def test_review_untraced_lists_each_lesson_with_no_fix_and_no_evidence(self):
        bare, traced = self.lessons_by_trace()
        rc, out = self.dev("review", "--untraced")
        self.assertEqual(rc, 0, out)
        listed = self.block(out, "No Fix and no Evidence · 3")
        for lid in bare:
            self.assertIn("Close port", self.line(listed, lid))
        for lid in traced:
            self.assertNotIn(lid, out)


class ListTests(LessonCLI):
    def test_ls_marks_a_prevention_without_a_receipt_unverified(self):
        unverified = self.add("Close the port after the probe", scope="global", files=["ports/**"])
        lessons.prevent(unverified, "test", "tests/test_probe.py::test_port_closes", "a test covers it", a=A)
        still_open = self.add("Wait for the server before the probe", scope="global", files=["probes/**"])
        rc, out = self.dev("ls", "--global")
        self.assertEqual(rc, 0, out)
        self.assertIn("unverified", self.line(out, unverified["meta"]["id"]))
        self.assertNotIn("unverified", self.line(out, still_open["meta"]["id"]))


class PreventByRuleTests(LessonCLI):
    def setUp(self):
        super().setUp()
        self.les = self.add("Run probes from a file, not inline", scope="global", commands=["python3 -c"])
        self.lid = self.les["meta"]["id"]

    def test_prevent_by_rule_with_a_pattern_proposes_a_warning_rule_and_records_it(self):
        rc, out = self.dev("prevent", self.lid, "--by", "rule", "--match", "python3 -c",
                           "--why", "agents keep running inline probes")
        self.assertEqual(rc, 0, out)
        rules = lessonrules.load(["global"])
        self.assertEqual([(r["lesson"], r["kind"], r["match"], r["mode"]) for r in rules],
                         [(self.lid, "call", "python3 -c", "warn")])
        m = lessons.find(self.lid)["meta"]
        self.assertEqual((m["status"], m["prevention"], m["prevention_ref"]), ("prevented", "rule", rules[0]["id"]))
        self.assertIn(rules[0]["id"], out)

    def test_by_guard_is_no_longer_offered(self):
        rc, out = self.dev("prevent", self.lid, "--by", "guard", "--match", "python3 -c", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("invalid choice: 'guard'", out)
        self.assertEqual(lessonrules.load(["global"]), [])

    def test_a_rule_lessonrules_cannot_accept_is_refused_with_its_reason_and_nothing_changes(self):
        rc, out = self.dev("prevent", self.lid, "--by", "rule", "--match", "git status", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("matches none of", out)
        self.assertEqual(lessonrules.load(["global"]), [])
        self.assertEqual(lessons.find(self.lid)["meta"]["status"], "open")

    def test_a_pattern_needs_by_rule_and_by_rule_needs_a_reference_or_a_pattern(self):
        rc, out = self.dev("prevent", self.lid, "--by", "test", "--match", "python3 -c", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--by rule", out)
        rc, out = self.dev("prevent", self.lid, "--by", "rule", "--why", "x")
        self.assertEqual(rc, 2)
        self.assertIn("--match", out)
        self.assertEqual(lessonrules.load(["global"]), [])
        self.assertEqual(lessons.find(self.lid)["meta"]["status"], "open")


class SearchTests(LessonCLI):
    # `dev lesson --search` read the active lessons only, so a lesson sleep archived could not be found to recur it
    def test_search_finds_an_archived_lesson_and_says_it_is_archived(self):
        les = self.add("Wait for the input before the step", commands=["make deploy"])
        lessons.archive(les, "not seen or used for 200 days", A)
        rc, out = self.dev("--search", "input before the step")
        self.assertEqual(rc, 0, out)
        self.assertIn("archived", self.line(out, les["meta"]["id"]))

    def test_search_lists_an_active_lesson_once(self):
        les = self.add("Wait for the input before the step", commands=["make deploy"])
        rc, out = self.dev("--search", "input before the step")
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count(les["meta"]["id"]), 1, out)
        self.assertNotIn("archived", self.line(out, les["meta"]["id"]))


class InitiativeListingTests(LessonCLI):
    # docs/lessons.md: `dev lesson` lists the lessons of this project, of its initiatives, and the global ones; the
    # listing added only the global ones, so an initiative's lessons never showed on its member projects
    def test_dev_lesson_lists_the_projects_initiative_lessons(self):
        h = Path(work.HANDOFF)
        (h / "initiatives" / "payments").mkdir(parents=True)
        (h / "initiatives" / "payments" / "README.md").write_text(
            "---\nkind: initiative\ntitle: Payments\nprojects: [acme/api]\n---\n# Payments\n")
        lid = self.add("Never force-push a release branch", scope="initiatives/payments",
                       commands=["git push --force"])["meta"]["id"]
        rc, out = self.dev()
        self.assertEqual(rc, 0, out)
        self.assertIn(lid, out)


class WriteScopeTests(LessonCLI):
    # recur, retire, prevent, anchor, supersede, release and restore found a lesson in every scope, so a session in
    # one project changed another project's lessons and -p, -i and --global changed nothing: they change the lessons
    # of the session's scope, its initiatives and the global ones, and a lesson of another scope is named, not changed
    def initiative(self, slug, members):
        folder = Path(work.HANDOFF) / "initiatives" / slug
        folder.mkdir(parents=True)
        (folder / "README.md").write_text(f"---\nkind: initiative\ntitle: {slug}\nprojects: [{', '.join(members)}]\n"
                                          f"---\n# {slug}\n")

    def occurrences(self, lid):
        return lessons.find(lid)["meta"]["occurrences"]

    def test_recur_from_one_project_refuses_another_projects_lesson_and_names_its_scope(self):
        lid = self.add("Wait for the input before the step", scope="acme/web", commands=["make deploy"])["meta"]["id"]
        rc, out = self.dev("recur", lid, "--evidence", "it happened again")
        self.assertEqual(rc, 2, out)
        self.assertIn("acme/web", out)
        self.assertIn("-p acme/web", out)
        self.assertEqual(self.occurrences(lid), 1)

    def test_retire_anchor_and_prevent_refuse_another_projects_lesson(self):
        lid = self.add("Wait for the input before the step", scope="acme/web", commands=["make deploy"])["meta"]["id"]
        for argv in (("retire", lid, "--why", "obsolete"), ("anchor", lid, "--why", "where", "--command", "make up"),
                     ("prevent", lid, "--by", "test", "--ref", "tests/test_x.py", "--why", "a test")):
            rc, out = self.dev(*argv)
            self.assertEqual(rc, 2, f"{argv}: {out}")
            self.assertIn("acme/web", out)
        self.assertEqual(lessons.find(lid)["meta"]["status"], "open")
        self.assertEqual(lessons.anchors(lessons.find(lid))["commands"], ["make deploy"])

    def test_a_title_that_two_projects_share_picks_the_sessions_own_lesson(self):
        mine = self.add("Wait for the input before the step", commands=["make deploy"])["meta"]["id"]
        self.add("Wait for the input before the step", scope="acme/web", commands=["make deploy"])
        rc, out = self.dev("recur", "wait for the input", "--evidence", "again")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.occurrences(mine), 2)

    def test_the_projects_initiative_and_global_lessons_stay_writable(self):
        self.initiative("payments", ["acme/api"])
        ini = self.add("Never force-push a release branch", scope="initiatives/payments",
                       commands=["git push --force"])["meta"]["id"]
        glob = self.add("Pin every action by its commit", scope="global", commands=["gh workflow run"])["meta"]["id"]
        for lid in (ini, glob):
            rc, out = self.dev("recur", lid, "--evidence", "again")
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.occurrences(lid), 2)

    def test_an_initiative_option_reaches_that_initiatives_lesson(self):
        self.initiative("billing", ["acme/web"])
        lid = self.add("Never force-push a release branch", scope="initiatives/billing",
                       commands=["git push --force"])["meta"]["id"]
        rc, out = self.dev("recur", lid, "--evidence", "again")
        self.assertEqual(rc, 2, out)
        self.assertIn("-i billing", out)
        rc, out = self.dev("recur", lid, "--evidence", "again", "-i", "billing")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.occurrences(lid), 2)

    def test_a_global_lessons_third_repeat_opens_a_work_item_in_the_project(self):
        lid = self.add("Pin every action by its commit", scope="global", commands=["gh workflow run"])["meta"]["id"]
        for _ in range(2):
            rc, out = self.dev("recur", lid, "--evidence", "again")
            self.assertEqual(rc, 0, out)
        ref = lessons.find(lid)["meta"].get("task_ref")
        self.assertTrue(ref, lessons.find(lid)["meta"])
        self.assertEqual(work.find(ref, "acme/api")["meta"]["id"], ref)


if __name__ == "__main__":
    unittest.main()
