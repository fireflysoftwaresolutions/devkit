# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Lesson rules: a check the guard enforces, made from a lesson that keeps repeating. A rule is accepted only when it
matches the lesson's recorded mistake and none of this machine's recent successful commands; it warns, then blocks
after a miss; it can never allow a call; a bad rule file is ignored and reported; and no rule refuses devkit,
`git status` or the file tools.
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

from devkit import audit, core, guard, lessonevents, lessonrules, lessons  # noqa: E402

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
                              (core, ("STATE", self.tmp / "state"))):
            p = mock.patch.object(target, value[0], value[1])
            p.start()
            self.addCleanup(p.stop)
        q = mock.patch.object(audit, "query", side_effect=self._audit)
        q.start()
        self.addCleanup(q.stop)
        self.audit_records = []
        guard.LAST_LESSON_RULE.clear()

    def _audit(self, days=None, **_):
        lo = time.time() - (days or 0) * 86400
        return [r for r in self.audit_records if r["ts"] >= lo]

    def succeeded(self, action, target, days_ago=1.0):
        self.audit_records.append({"action": action, "target": target, "outcome": "ok",
                                   "ts": time.time() - days_ago * 86400})

    def rule_doc(self, scope="global") -> dict:
        return json.loads((self.base / "rules" / f"{scope}.json").read_text())


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

    def test_a_success_older_than_fourteen_days_does_not_count(self):
        les = write_lesson(self.base, commands=("dev progress step",), title="Keep dev progress step on its own")
        self.succeeded("dev.progress", "step done --check ok", days_ago=15)
        rule = lessonrules.propose(les, match="dev progress step")
        self.assertEqual(rule["mode"], "warn")

    def test_a_pattern_that_would_refuse_git_status_is_rejected(self):
        les = write_lesson(self.base, commands=("git status --porcelain",), title="Read the branch with dev status")
        for pattern in ("git status", "git status *"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError) as cm:
                lessonrules.propose(les, match=pattern)
            self.assertIn("would refuse `git status", str(cm.exception))

    def test_a_pattern_that_would_refuse_devkit_or_a_file_tool_is_rejected(self):
        les = write_lesson(self.base, commands=("dev status --json", "Edit file"))
        for pattern, refused in (("dev st*", "dev status"), ("Edit *", "Edit README.md")):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError) as cm:
                lessonrules.propose(les, match=pattern)
            self.assertIn(f"would refuse `{refused}", str(cm.exception))

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

    def test_the_same_rule_twice_is_one_rule_and_block_promotes_it(self):
        les = write_lesson(self.base)
        first = lessonrules.propose(les, match="python3 -c")
        again = lessonrules.propose(les, match="python3 -c", block=True)
        self.assertEqual(first["id"], again["id"])
        self.assertRegex(first["id"], r"^LR-[0-9a-f]{10}$")
        doc = self.rule_doc()
        self.assertEqual([(r["id"], r["mode"]) for r in doc["rules"]], [(first["id"], "block")])

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

    def test_an_accepted_rule_warns_then_blocks_after_a_miss(self):
        self.assertEqual(lessonrules.check_call("python3 -c 'print(1)'", ["global"])[0], "warn")
        lessonrules.record_hit(self.rule["id"], missed=False)
        self.assertEqual(lessonrules.check_call("python3 -c 'print(1)'", ["global"])[0], "warn")
        lessonrules.record_hit(self.rule["id"], missed=True)
        mode, rule = lessonrules.check_call("python3 -c 'print(1)'", ["global"])
        self.assertEqual((mode, rule["id"]), ("block", self.rule["id"]))
        self.assertIn("matched again", self.rule_doc()["rules"][0]["history"][0])
        hits = json.loads((self.tmp / "hits.json").read_text())[self.rule["id"]]
        self.assertEqual((hits["hits"], hits["misses"]), (2, 1))

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
            lessonrules.propose(self.les, match="python3 -c", block=True)
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


# --------------------------------------------------------------------------- the guard

class GuardTests(RulesCase):
    def setUp(self):
        super().setUp()
        self.les = write_lesson(self.base)
        self.rule = lessonrules.propose(self.les, match="python3 -c")
        self.cwd = str(self.tmp)

    def check(self, cmd):
        return guard.check("Bash", {"command": cmd}, self.cwd, "claude")

    def test_a_warn_rule_lets_the_call_run_with_the_lesson_as_context(self):
        self.assertIsNone(self.check("python3 -c 'print(1)'"))
        ctx = guard.lesson_rule_context()
        self.assertIn(self.rule["id"], ctx)
        self.assertIn(PY_LESSON, ctx)
        self.assertIn("Write the probe to the scratch folder", ctx)
        self.assertIsNone(self.check("python3 probe.py"))
        self.assertEqual(guard.lesson_rule_context(), "")

    def test_a_miss_after_the_warning_makes_the_next_call_refused(self):
        self.assertIsNone(self.check("python3 -c 1"))
        self.assertEqual(guard.lesson_rule_hit("s-1", "claude")["missed"], False)
        self.assertIsNone(self.check("python3 -c 2"))  # shown at the warning, run again: a miss
        self.assertEqual(guard.lesson_rule_hit("s-1", "claude")["missed"], True)
        verdict = self.check("python3 -c 3")
        self.assertEqual(verdict[0], "deny")
        self.assertIn(PY_LESSON, verdict[1])
        self.assertIn("Write the probe to the scratch folder", verdict[1])
        self.assertTrue(guard.LAST_REFUSAL)
        self.assertEqual(guard.lesson_rule_context(), "")  # a refusal says it in its reason, not as context

    def test_another_session_shown_nothing_is_no_miss(self):
        self.check("python3 -c 1")
        guard.lesson_rule_hit("s-1", "claude")
        self.check("python3 -c 1")
        self.assertEqual(guard.lesson_rule_hit("s-2", "claude")["missed"], False)
        self.assertIsNone(self.check("python3 -c 1"))

    def test_no_rule_can_allow_a_call_the_guard_refuses(self):
        les = write_lesson(self.base, lid="L-fxforce0003c", commands=("git push --force",),
                           title="Push with --force-with-lease", prevention="Use --force-with-lease on your branch.")
        for block in (False, True):
            with self.subTest(block=block):
                lessonrules.propose(les, match="git push --force", block=block)
                refused = self.check("git push --force origin feat/x")
                self.assertEqual(refused[0], "deny")
                self.assertIn("force-push", refused[1].lower())
                self.assertNotIn("L-fxforce0003c", refused[1])  # the guard's own refusal, as it was
                self.assertEqual(guard.LAST_REFUSAL, "check_git:force-push")
                self.assertEqual(guard.lesson_rule_context(), "")

    def test_a_rule_file_that_says_allow_changes_nothing(self):
        doc = self.rule_doc()
        doc["rules"][0]["mode"] = "allow"
        (self.base / "rules" / "global.json").write_text(json.dumps(doc))
        self.assertIsNone(self.check("python3 -c 1"))
        self.assertEqual(guard.lesson_rule_context(), "")
        self.assertEqual(self.check("git push --force origin feat/x")[0], "deny")

    def test_a_corrupt_rule_file_fails_open_for_the_rules_and_is_recorded_once(self):
        (self.base / "rules" / "global.json").write_text("not json")
        with mock.patch.object(guard, "failopen") as fo:
            self.assertIsNone(self.check("python3 -c 1"))
            self.assertIsNone(self.check("python3 -c 1"))
        self.assertEqual(fo.call_count, 1)
        self.assertEqual(fo.call_args[0][0], "lesson-rules")
        self.assertIn("global.json", str(fo.call_args[0][1]))

    def test_a_lookup_over_the_time_budget_fails_open_with_a_record(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def stuck(cmd, scopes):
            release.wait(5)
            return "block", self.rule

        with mock.patch.object(guard, "LESSON_RULE_BUDGET", 0.05), mock.patch.object(lessonrules, "check_call", stuck), \
                mock.patch.object(guard, "failopen") as fo:
            self.assertIsNone(self.check("python3 -c 1"))
        self.assertIsInstance(fo.call_args[0][1], TimeoutError)

    def test_an_error_in_the_rules_fails_open_for_the_rules_only(self):
        with mock.patch.object(lessonrules, "check_call", side_effect=RuntimeError("boom")), \
                mock.patch.object(guard, "failopen") as fo:
            self.assertIsNone(self.check("python3 -c 1"))
            self.assertEqual(self.check("git push --force origin feat/x")[0], "deny")
        self.assertEqual(fo.call_args[0][0], "lesson-rules")

    def test_file_tools_are_never_judged_by_a_rule(self):
        les = write_lesson(self.base, lid="L-fxdeploy04d", commands=("npm run deploy",), title="Deploy with dev release",
                           prevention="Run dev release.")
        lessonrules.propose(les, match="npm run deploy", block=True)
        self.assertEqual(self.check("npm run deploy")[0], "deny")
        target = str(self.tmp / "notes.txt")
        self.assertIsNone(guard.check("Read", {"file_path": target}, self.cwd, "claude"))
        self.assertEqual(guard.lesson_rule_context(), "")

    def test_a_project_rule_applies_in_its_project_only(self):
        les = write_lesson(self.base, lid="L-fxproj0005e", scope="acme/api", commands=("make deploy",),
                           title="Deploy through dev release", prevention="Run dev release.")
        lessonrules.propose(les, match="make deploy", block=True)
        with mock.patch.object(guard, "current_project", return_value="acme/api"):
            self.assertEqual(self.check("make deploy")[0], "deny")
        with mock.patch.object(guard, "current_project", return_value="acme/web"):
            self.assertIsNone(self.check("make deploy"))


class BlockAtOnceIsTheDevelopers(unittest.TestCase):
    """A rule that refuses from its first match skips the warnings that show whether it is right: the developer's own
    decision, as `dev lesson release` is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(core, "load_config", return_value={"safeguards": {
                      "irreversible_main": "audit", "irreversible_member": "enforce"}}),
                  mock.patch.object(guard, "current_project", return_value="acme/api"),
                  mock.patch.object(audit, "policy_file", lambda: Path(self.tmp.name) / "no-policy.json")):
            p.start()
            self.addCleanup(p.stop)

    def main(self, cmd):
        v = guard.irreversible_verdict(cmd, self.tmp.name, agent="claude", session="s1", grant=lambda *a, **k: None)
        return v[1] if v else None

    def member(self, cmd):
        v = guard.crew_shell_verdict(cmd, self.tmp.name, None, "probe", level="all", lane="a1", session="s1",
                                     grant=lambda *a, **k: None)
        return v[1] if v else None

    def test_an_agent_cannot_make_a_rule_that_blocks_at_once(self):
        cmd = 'dev lesson prevent L-fxpy3c0001a --by guard --match "python3 -c" --block --why "repeats"'
        why = self.main(cmd) or ""
        self.assertIn("developer's own decision", why)
        self.assertIn("lesson prevent --by guard --block", why)
        self.assertIsNotNone(self.member(cmd))
        acts = guard.irreversible_actions(cmd, self.tmp.name)
        self.assertEqual([(a["action"], a["target"]) for a in acts], [("developer-only", "lesson-block/L-fxpy3c0001a")])

    def test_a_warn_rule_and_the_other_preventions_stay_the_agents(self):
        for cmd in ('dev lesson prevent L-fxpy3c0001a --by guard --match "python3 -c" --why "repeats"',
                    'dev lesson prevent L-fxpy3c0001a --by test --ref tests/test_x.py --why "covered"',
                    "dev lesson prevent --help"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.main(cmd))


if __name__ == "__main__":
    unittest.main()
