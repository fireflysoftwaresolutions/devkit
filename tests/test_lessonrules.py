# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson rules: a warning made from a lesson that keeps repeating. A rule is accepted only when it matches the
lesson's recorded mistake and none of this machine's recent successful commands; it only warns, whatever its file
says; a bad rule file is ignored and reported; and no rule matches devkit, `git status` or the file tools.
Run: python3 -m unittest tests.test_lessonrules -v"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, bugs, core, hooks, lanescope, lessonevents, lessonrules, lessons  # noqa: E402

PY_LESSON = "L-fxpy3c0001a"


def write_lesson(base: Path, lid=PY_LESSON, scope="global", title="Run probes as a script file, not python3 -c",
                 prevention="Write the probe to the scratch folder and run python3 <file>.", commands=("python3 -c",),
                 errors=(), evidence="", happened="", status="open", quarantine="") -> dict:
    meta = {"id": lid, "title": title, "scope": scope, "kind": "mistake", "severity": "medium", "status": status,
            "occurrences": 3, "prevention": "rule", "prevention_ref": "", "first_seen": "2026-11-01",
            "last_seen": "2026-11-07", "version": 1, "quarantine": quarantine}
    for k in lessons.ANCHORS:
        meta[k] = []
    meta["commands"], meta["errors"] = list(commands), list(errors)
    les = {"meta": meta, "extra": [], "changelog": [],
           "sections": {"What happened": happened, "Root cause": "the read-only allowlist refuses inline code",
                        "Fix": "", "Prevention": prevention, "Detection": "", "Evidence": evidence}}
    path = base / scope / f"{lid}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lessons.render(les))
    return lessons.load(path)


class RulesCase(unittest.TestCase):
    """A handoff lessons folder, rule hit counts and lesson events of the test's own, and no recent commands unless
    a test records them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.base = self.tmp / "lessons"
        self.base.mkdir()
        for target, value in ((lessons, ("LESSONS", self.base)), (lessonrules, ("HITS", self.tmp / "hits.json")),
                              (lessonrules, ("STATE", self.tmp / "state")),
                              (lessonevents, ("EVENTS", self.tmp / "events.jsonl")),
                              (bugs, ("BUGS", self.tmp / "state" / "bugs")),
                              (core, ("STATE", self.tmp / "state"))):
            p = mock.patch.object(target, value[0], value[1])
            p.start()
            self.addCleanup(p.stop)
        q = mock.patch.object(audit, "query", side_effect=self._audit)
        q.start()
        self.addCleanup(q.stop)
        self.audit_records = []
        lessonrules.LAST_LESSON_RULE.clear()

    def _audit(self, days=None, **_):
        lo = time.time() - (days or 0) * 86400
        return [r for r in self.audit_records if r["ts"] >= lo]

    def succeeded(self, action, target, days_ago=1.0):
        self.audit_records.append({"action": action, "target": target, "outcome": "ok",
                                   "ts": time.time() - days_ago * 86400})

    def rule_doc(self, scope="global") -> dict:
        return json.loads((self.base / "rules" / f"{scope}.json").read_text())

    def ran(self, command, ok=True, hours_ago=1.0, session="s-ledger"):
        """A shell call in a session ledger, as the hooks record it: the key of its command words, never its text."""
        led = self.tmp / "state" / "bugs" / "ledger"
        led.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time() - hours_ago * 3600, "tool": "Bash", "ok": ok, "dev": "", "codes": [],
               "cmd": bugs.command_key(hooks.command_words(command)), "who": "main"}
        with (led / f"{session}.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def synced(self, rules, scope="global"):
        """A rule file as a teammate's pushed commit leaves it: any mode, ids that match the rules."""
        doc = {"version": 1, "rules": [{"id": lessonrules.rule_id(scope, lid, "call", pat), "lesson": lid,
                                        "kind": "call", "match": pat, "mode": mode, "created": "x", "created_by": "x",
                                        "history": []} for lid, pat, mode in rules]}
        path = self.base / "rules" / f"{scope}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))


# --------------------------------------------------------------------------- proposing a rule

class ProposeTests(RulesCase):
    def test_a_pattern_that_matches_a_recent_successful_command_is_rejected(self):
        les = write_lesson(self.base, commands=("dev progress step",), title="Keep dev progress step on its own")
        self.succeeded("dev.progress", "step done --check ok")
        with self.assertRaises(ValueError) as cm:
            lessonrules.propose(les, match="dev progress step")
        self.assertIn("succeeded here", str(cm.exception))
        self.assertFalse((self.base / "rules" / "global.json").exists())

    def test_a_shell_call_the_hooks_timed_counts_as_a_success(self):
        les = write_lesson(self.base)
        timing = self.tmp / "state" / "timing"
        timing.mkdir(parents=True)
        (timing / "2026-11.jsonl").write_text(json.dumps(
            {"ts": time.time() - 3600, "kind": "tool", "what": "Bash: cd /x && python3 -c 'print(1)'"}) + "\n")
        with self.assertRaises(ValueError) as cm:
            lessonrules.propose(les, match="python3 -c")
        self.assertIn("succeeded here", str(cm.exception))

    def test_an_ordinary_shell_call_a_session_ledger_records_counts_as_a_success(self):
        les = write_lesson(self.base, commands=("git commit -m",), title="Write a commit message with dev commit")
        self.ran("cd /repo && git commit -m 'wip'")
        with self.assertRaises(ValueError) as cm:
            lessonrules.propose(les, match="git commit -m")
        self.assertIn("succeeded here", str(cm.exception))
        self.assertFalse((self.base / "rules" / "global.json").exists())

    def test_a_failed_or_old_call_in_the_ledger_does_not_count(self):
        les = write_lesson(self.base, commands=("git commit -m",), title="Write a commit message with dev commit")
        self.ran("git commit -m 'wip'", ok=False)
        self.ran("git commit -m 'wip'", hours_ago=15 * 24)
        self.ran("git commit --amend")
        self.assertEqual(lessonrules.propose(les, match="git commit -m")["mode"], "warn")

    def test_a_success_older_than_fourteen_days_does_not_count(self):
        les = write_lesson(self.base, commands=("dev progress step",), title="Keep dev progress step on its own")
        self.succeeded("dev.progress", "step done --check ok", days_ago=15)
        rule = lessonrules.propose(les, match="dev progress step")
        self.assertEqual(rule["mode"], "warn")

    def test_a_pattern_that_would_match_git_status_is_rejected(self):
        les = write_lesson(self.base, commands=("git status --porcelain",), title="Read the branch with dev status")
        for pattern in ("git status", "git status *"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError) as cm:
                lessonrules.propose(les, match=pattern)
            self.assertIn("would match `git status", str(cm.exception))

    def test_a_pattern_that_would_match_devkit_or_a_file_tool_is_rejected(self):
        les = write_lesson(self.base, commands=("dev status --json", "Edit file"))
        for pattern, matched in (("dev st*", "dev status"), ("Edit *", "Edit README.md")):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError) as cm:
                lessonrules.propose(les, match=pattern)
            self.assertIn(f"would match `{matched}", str(cm.exception))

    def test_a_pattern_must_match_the_recorded_mistake(self):
        les = write_lesson(self.base)
        with self.assertRaises(ValueError) as cm:
            lessonrules.propose(les, match="python3 -m pytest")
        self.assertIn("matches none of", str(cm.exception))

    def test_the_evidence_counts_as_the_recorded_mistake(self):
        les = write_lesson(self.base, commands=(), evidence="refused: `sed -i s/a/b/ file.txt` in a review lane")
        self.assertEqual(lessonrules.propose(les, match="sed -i")["kind"], "call")

    def test_a_pattern_is_command_words_never_a_regular_expression_or_a_bare_program(self):
        les = write_lesson(self.base)
        for pattern, why in (("python3", "alone"), ("python3 *", "alone"), ("* -c", "first word"),
                             ("python3 -c\nrm", "new line"), ("x" * 201, "longer than")):
            with self.subTest(pattern=pattern[:20]), self.assertRaises(ValueError) as cm:
                lessonrules.propose(les, match=pattern)
            self.assertIn(why, str(cm.exception))

    def test_a_held_or_retired_lesson_makes_no_rule(self):
        held = write_lesson(self.base, quarantine="Prevention turns off the guard")
        retired = write_lesson(self.base, lid="L-fxpy3c0002b", status="retired")
        for les, why in ((held, "held"), (retired, "retired")):
            with self.subTest(why=why), self.assertRaises(ValueError) as cm:
                lessonrules.propose(les, match="python3 -c")
            self.assertIn(why, str(cm.exception))

    def test_the_same_rule_twice_is_one_warning_rule(self):
        les = write_lesson(self.base)
        first = lessonrules.propose(les, match="python3 -c")
        again = lessonrules.propose(les, match="python3 -c")
        self.assertEqual(first["id"], again["id"])
        self.assertRegex(first["id"], r"^LR-[0-9a-f]{10}$")
        doc = self.rule_doc()
        self.assertEqual([(r["id"], r["mode"]) for r in doc["rules"]], [(first["id"], "warn")])

    def test_a_rule_can_no_longer_be_asked_to_block(self):
        with self.assertRaises(TypeError):
            lessonrules.propose(write_lesson(self.base), match="python3 -c", block=True)
        self.assertEqual(lessonrules.MODES, ("warn",))

    def test_an_output_rule_needs_the_error_text_the_lesson_recorded(self):
        les = write_lesson(self.base, errors=("ModuleNotFoundError: No module named 'devkit'",))
        with self.assertRaises(ValueError):
            lessonrules.propose(les, error="ImportError: cannot import name 'x'")
        rule = lessonrules.propose(les, error="No module named 'devkit'")
        self.assertEqual(rule["kind"], "output")
        out = "Traceback …\nmodulenotfounderror: no module named 'DEVKIT'\n"
        self.assertEqual([r["id"] for r in lessonrules.check_output(out, ["global"])], [rule["id"]])
        self.assertEqual(lessonrules.check_output("all 12 tests passed", ["global"]), [])


# --------------------------------------------------------------------------- loading and checking

class CheckTests(RulesCase):
    def setUp(self):
        super().setUp()
        self.les = write_lesson(self.base)
        self.rule = lessonrules.propose(self.les, match="python3 -c")

    def test_a_miss_is_counted_and_the_rule_stays_a_warning(self):
        self.assertEqual(lessonrules.check_call("python3 -c 'print(1)'", ["global"])[0], "warn")
        lessonrules.record_hit(self.rule["id"], missed=False)
        lessonrules.record_hit(self.rule["id"], missed=True)
        lessonrules.record_hit(self.rule["id"], missed=True)
        mode, rule = lessonrules.check_call("python3 -c 'print(1)'", ["global"])
        self.assertEqual((mode, rule["id"]), ("warn", self.rule["id"]))
        self.assertEqual(self.rule_doc()["rules"][0]["mode"], "warn")
        hits = json.loads((self.tmp / "hits.json").read_text())[self.rule["id"]]
        self.assertEqual((hits["hits"], hits["misses"]), (3, 2))

    def test_a_block_rule_an_older_devkit_wrote_still_loads_and_warns(self):
        self.synced([(PY_LESSON, "python3 -c", "block")])
        (self.tmp / "state").mkdir(exist_ok=True)
        (self.tmp / "state" / "lessons-rule-blocks.json").write_text(json.dumps({self.rule["id"]: {"ts": 1}}))
        [loaded] = lessonrules.load(["global"])
        self.assertEqual((loaded["mode"], loaded["stored_mode"]), ("warn", "block"))
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["global"])[0], "warn")
        self.assertEqual(lessonrules.propose(self.les, match="python3 -c")["mode"], "warn")

    def test_a_synced_rule_that_would_match_devkit_or_git_status_is_ignored_at_load(self):
        les = write_lesson(self.base, lid="L-fxsync0006f", commands=("git status --short", "dev lesson ls -a",
                                                                   "git diff --stat", "pytest -x"),
                           title="Read the state with dev status", prevention="Run dev status.")
        lid = les["meta"]["id"]
        self.synced([(lid, "git status", "block"), (lid, "dev lesson ls", "block"), (lid, "git diff", "warn"),
                     (lid, "pytest -x", "block")])
        self.assertEqual([r["match"] for r in lessonrules.load(["global"])], ["pytest -x"])
        self.assertEqual(sum("would match" in p for p in lessonrules.PROBLEMS), 3, lessonrules.PROBLEMS)
        self.assertEqual(lessonrules.check_call("git status", ["global"]), ("", None))
        self.assertEqual(lessonrules.check_call("pytest -x tests", ["global"])[0], "warn")

    def test_the_pattern_matches_command_words_not_text_that_mentions_them(self):
        for cmd, mode in (("cd x&&python3 -c 'print(1)'", "warn"), ("/usr/bin/python3 -c 1", "warn"),
                          ("bash -lc 'python3 -c 1'", "warn"), ("echo 'python3 -c is refused'", ""),
                          ("python3 -m pytest", ""), ("python3 scratch/probe.py -c", "")):
            with self.subTest(cmd=cmd):
                self.assertEqual(lessonrules.check_call(cmd, ["global"])[0], mode)

    def test_a_rule_of_another_scope_does_not_apply(self):
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["acme/api"]), ("", None))

    def test_a_corrupt_rule_file_is_ignored_and_reported(self):
        path = self.base / "rules" / "global.json"
        path.write_text('{"version": 1, "rules": [')
        self.assertEqual(lessonrules.load(["global"]), [])
        self.assertTrue(any("global.json" in p and "ignored" in p for p in lessonrules.PROBLEMS), lessonrules.PROBLEMS)
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["global"]), ("", None))
        with self.assertRaises(ValueError):  # a file devkit cannot read is never written over
            lessonrules.propose(self.les, match="python3 -c")
        self.assertEqual(path.read_text(), '{"version": 1, "rules": [')

    def test_a_bad_rule_is_ignored_and_the_good_ones_still_load(self):
        doc = self.rule_doc()
        allow = dict(doc["rules"][0], mode="allow")
        widened = dict(doc["rules"][0], match="python3")  # edited by hand: too broad, and its id no longer matches
        moved = dict(doc["rules"][0], match="python3 -m")  # a well-shaped pattern edited by hand: only the id tells
        doc["rules"] = [allow, widened, moved, doc["rules"][0], "x"]
        (self.base / "rules" / "global.json").write_text(json.dumps(doc))
        self.assertEqual([r["id"] for r in lessonrules.load(["global"])], [self.rule["id"]])
        text = " ".join(lessonrules.PROBLEMS)
        self.assertIn("never allows", text)
        self.assertIn("alone", text)
        self.assertIn("does not match the rule", text)
        self.assertIn("not an object", text)
        self.assertEqual(lessonrules.check_call("python3 -m pytest", ["global"]), ("", None))

    def test_a_widened_pattern_with_a_matching_id_is_still_checked_for_shape(self):
        doc = self.rule_doc()
        rid = lessonrules.rule_id("global", PY_LESSON, "call", "python3")
        doc["rules"] = [dict(doc["rules"][0], id=rid, match="python3")]
        (self.base / "rules" / "global.json").write_text(json.dumps(doc))
        self.assertEqual(lessonrules.load(["global"]), [])
        self.assertEqual(lessonrules.check_call("python3 x.py", ["global"]), ("", None))

    def test_a_scope_that_leaves_the_rules_folder_is_refused(self):
        self.assertEqual(lessonrules.load(["../../etc", "/abs"]), [])
        self.assertEqual(len(lessonrules.PROBLEMS), 2)

    def test_a_lesson_held_superseded_or_gone_turns_its_rule_off(self):
        path = self.les["path"]
        text = path.read_text()
        path.write_text(text.replace("quarantine: \n", "quarantine: Prevention turns off the guard\n")
                        .replace("quarantine:\n", "quarantine: Prevention turns off the guard\n"))
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["global"]), ("", None))
        path.unlink()
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["global"]), ("", None))

    def test_an_archived_lesson_keeps_its_rule(self):
        archived = self.base / lessons.ARCHIVE / "global" / f"{PY_LESSON}.md"
        archived.parent.mkdir(parents=True)
        self.les["path"].rename(archived)
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["global"])[0], "warn")

    def test_the_text_shown_is_the_title_and_prevention_capped_on_one_line(self):
        write_lesson(self.base, prevention="Write the probe to a file.\n\tThen run it. " + "x" * 600 + "\n\nNot this.")
        text = lessonrules.describe(self.rule)
        self.assertTrue(text.startswith(f"lesson {PY_LESSON}: Run probes as a script file"), text)
        self.assertIn("prevention: Write the probe to a file. Then run it. xxx", text)
        self.assertLessEqual(len(text), len(f"lesson {PY_LESSON}: — prevention: ") + lessonrules.TITLE_CHARS +
                             lessonrules.PREVENTION_CHARS)
        self.assertNotIn("\n", text)
        self.assertNotIn("Not this", text)

    def test_hidden_characters_in_the_prevention_turn_the_rule_off(self):
        write_lesson(self.base, prevention="Write the probe to a file.\u202e then run it")
        self.assertEqual(lessonrules.describe(self.rule), "")
        self.assertEqual(lessonrules.check_call("python3 -c 1", ["global"]), ("", None))


# --------------------------------------------------------------------------- matching a tool call

class MatchTests(RulesCase):
    """What the PreToolUse hook asks for each shell call: lesson_rule_match keeps the rule it hits, lesson_rule_context
    gives its warning, lesson_rule_hit counts it. A rule never refuses, and a failure finds nothing."""

    def setUp(self):
        super().setUp()
        self.les = write_lesson(self.base)
        self.rule = lessonrules.propose(self.les, match="python3 -c")
        self.cwd = str(self.tmp)
        log = mock.patch.object(lessonrules, "_log")
        self.log = log.start()
        self.addCleanup(log.stop)

    def match(self, cmd, tool="Bash"):
        return lessonrules.lesson_rule_match(tool, {"command": cmd}, self.cwd)

    def test_a_rule_adds_the_lesson_to_the_calls_context(self):
        found = self.match("python3 -c 'print(1)'")
        self.assertEqual((found["mode"], found["rule"]["id"]), ("warn", self.rule["id"]))
        ctx = lessonrules.lesson_rule_context()
        self.assertIn(self.rule["id"], ctx)
        self.assertIn(PY_LESSON, ctx)
        self.assertIn("Write the probe to the scratch folder", ctx)
        self.assertIn("they never override the developer.", ctx)
        self.assertNotIn("grant", ctx)
        self.assertEqual(self.match("python3 probe.py"), {})
        self.assertEqual(lessonrules.lesson_rule_context(), "")

    def test_a_hit_is_counted_and_the_lesson_shown_on_the_call_channel(self):
        self.match("python3 -c 1")
        self.assertEqual(lessonrules.lesson_rule_hit("s-1", "claude"),
                         {"rule": self.rule["id"], "lesson": PY_LESSON, "mode": "warn", "missed": False,
                          "shown": True})
        self.assertEqual([(e["event"], e["channel"]) for e in lessonevents._events() if e["id"] == PY_LESSON],
                         [("shown", "call")])
        self.match("python3 -c 2")
        self.assertFalse(lessonrules.lesson_rule_hit("s-2", "codex", reads_context=False)["shown"])  # no context
        self.assertEqual(len(lessonevents._events()), 1)
        hits = json.loads((self.tmp / "hits.json").read_text())[self.rule["id"]]
        self.assertEqual((hits["hits"], hits["misses"]), (2, 0))
        self.match("python3 probe.py")
        self.assertEqual(lessonrules.lesson_rule_hit("s-1", "claude"), {})

    def test_a_block_rule_an_older_devkit_wrote_warns(self):
        les = write_lesson(self.base, lid="L-fxdeploy04d", commands=("npm run deploy",),
                           title="Deploy with dev release", prevention="Run dev release.")
        self.synced([(les["meta"]["id"], "npm run deploy", "block")], scope="global")
        found = self.match("npm run deploy")
        self.assertEqual((found["mode"], found["rule"]["lesson"]), ("warn", "L-fxdeploy04d"))
        self.assertIn("Run dev release.", lessonrules.lesson_rule_context())

    def test_a_rule_file_that_says_allow_changes_nothing(self):
        doc = self.rule_doc()
        doc["rules"][0]["mode"] = "allow"
        (self.base / "rules" / "global.json").write_text(json.dumps(doc))
        self.assertEqual(self.match("python3 -c 1"), {})
        self.assertEqual(lessonrules.lesson_rule_context(), "")

    def test_a_corrupt_rule_file_finds_nothing_and_is_logged_once_a_day(self):
        (self.base / "rules" / "global.json").write_text("not json")
        self.assertEqual(self.match("python3 -c 1"), {})
        self.assertEqual(self.match("python3 -c 1"), {})
        self.assertEqual(self.log.call_count, 1)
        self.assertIn("global.json", self.log.call_args[0][0])

    def test_a_lookup_over_the_time_budget_finds_nothing_and_is_logged(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(cmd, scopes):
            release.wait(5)
            return "warn", self.rule

        with mock.patch.object(lessonrules, "LESSON_RULE_BUDGET", 0.05), \
                mock.patch.object(lessonrules, "check_call", stuck):
            self.assertEqual(self.match("python3 -c 1"), {})
        self.assertIn("TimeoutError", self.log.call_args[0][0])

    def test_an_error_in_the_rules_finds_nothing_and_is_logged(self):
        with mock.patch.object(lessonrules, "check_call", side_effect=RuntimeError("boom")):
            self.assertEqual(self.match("python3 -c 1"), {})
        self.assertIn("boom", self.log.call_args[0][0])
        self.assertEqual(lessonrules.lesson_rule_context(), "")

    def test_file_tools_are_never_matched(self):
        target = str(self.tmp / "notes.txt")
        for tool, inp in (("Read", {"file_path": target}), ("Write", {"file_path": target, "content": "python3 -c 1"})):
            with self.subTest(tool=tool):
                self.assertEqual(lessonrules.lesson_rule_match(tool, inp, self.cwd), {})
        self.assertEqual(lessonrules.lesson_rule_match("exec_command", {"cmd": "python3 -c 1"}, self.cwd)["mode"],
                         "warn")

    def test_a_project_rule_applies_in_its_project_only(self):
        les = write_lesson(self.base, lid="L-fxproj0005e", scope="acme/api", commands=("make deploy",),
                           title="Deploy through dev release", prevention="Run dev release.")
        lessonrules.propose(les, match="make deploy")
        with mock.patch.object(lanescope, "current_project", return_value="acme/api"):
            self.assertEqual(self.match("make deploy")["rule"]["lesson"], "L-fxproj0005e")
        with mock.patch.object(lanescope, "current_project", return_value="acme/web"):
            self.assertEqual(self.match("make deploy"), {})


# --------------------------------------------------------------------------- a rule as a lesson's prevention

class RulePreventionTests(RulesCase):
    def test_a_rule_prevention_is_flagged_unverified_and_a_removed_rule_reopens_the_lesson_at_night(self):
        from devkit import sleep
        les = write_lesson(self.base)
        rule = lessonrules.propose(les, match="python3 -c")
        prevented = lessons.prevent(les, "rule", rule["id"], "warns on inline probes")
        self.assertEqual(prevented["meta"]["status"], "prevented")
        self.assertIn("no automated check", lessons.unverified(prevented))  # a warning proves nothing: flagged
        self.assertNotIn(PY_LESSON, [x["meta"]["id"] for x in lessons.recall_pool("acme/api")])
        actor = lessons.work.actor("devkit")
        self.assertEqual(sleep.check_preventions(a=actor), [])  # the rule is still there
        (self.base / "rules" / "global.json").unlink()
        self.assertEqual([(k, i) for k, i, _ in sleep.check_preventions(a=actor)], [("reopen", PY_LESSON)])
        self.assertIn(PY_LESSON, [x["meta"]["id"] for x in lessons.recall_pool("acme/api")])


if __name__ == "__main__":
    unittest.main()
