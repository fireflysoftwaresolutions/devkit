# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# C13 · meaning comes from the model, enforcement from neutral anchors (crew gap review P-fxmipt9thob v4). devkit
# never matches the developer's words: lessons are anchored and recalled on behavior (kinds of work, files, commands,
# tool output), skill rules trigger on files, commands and declared kinds of work, a turn is development work from
# what it did, the model gets a catalog of ids, and background jobs may ask a small model headless (opt-in, on a
# daily budget). Run: python3 -m unittest tests.test_language_neutral
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ai, cli, core, crew, lessons, raci, scope, skillguard, wizard, work  # noqa: E402
from test_lessons_base import unanchor  # noqa: E402

A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
DAY = 86400


def ids(items):
    return [x["meta"]["id"] for x in items]


class _Handoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.root = root
        self.saved = (lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL, ai.USAGE)
        lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL = root / "lessons", root, root / "usage.json"
        ai.USAGE = root / "ai-usage.json"

    def tearDown(self):
        lessons.LESSONS, work.HANDOFF, lessons.USAGE_LOCAL, ai.USAGE = self.saved
        self.tmp.cleanup()

    def add(self, title, scope="acme/api", **kw):
        return lessons.add(scope, title, cause=kw.pop("cause", "cause of " + title),
                           prevention=kw.pop("prevention", "prevention of " + title),
                           happened=kw.pop("happened", "it happened: " + title), a=A, **kw)


class AnchoredLessonTests(_Handoff):
    def setUp(self):
        super().setUp()
        self.push = self.add("Force push lost a teammate's commits", commands=["git push --force"],
                             activities=["release"])
        self.mig = self.add("Edited an applied migration", files=["db/migrations/**"], activities=["database"],
                            errors=["checksum mismatch"], stack=["java"])
        # a lesson written before anchors existed: it shares the step's words and nothing else
        self.words = unanchor(self.add("Migration tests were skipped", scope="global", cause="migration tests skipped",
                                       prevention="run the migration tests", activities=["testing"]))

    def test_anchors_round_trip_and_a_lesson_written_before_them_has_none(self):
        self.assertEqual(lessons.anchors(lessons.find(self.mig["meta"]["id"])),
                         {"files": ["db/migrations/**"], "commands": [], "activities": ["database"],
                          "errors": ["checksum mismatch"], "stack": ["java"]})
        old = self.root / "lessons" / "global" / "L-old.md"
        old.write_text("".join(ln for ln in self.words["path"].read_text().splitlines(True)
                               if ln.split(":")[0] not in lessons.ANCHORS).replace(self.words["meta"]["id"], "L-old"))
        self.assertEqual(lessons.anchors(lessons.load(old)), {k: [] for k in lessons.ANCHORS})

    def test_an_anchor_with_a_comma_or_an_unknown_kind_of_work_is_refused(self):
        with self.assertRaises(ValueError):
            self.add("x", files=["a.py,b.py"])
        with self.assertRaises(ValueError) as e:
            self.add("x", activities=["nope"])
        self.assertIn("no kind of work", str(e.exception))

    def test_a_step_recalls_the_lessons_of_its_kind_of_work_and_files_not_of_its_words(self):
        picked = ids(lessons.for_task("acme/api", activity="database", files=["db/migrations/V3__orders.sql"]))
        self.assertEqual(picked, [self.mig["meta"]["id"]])  # the lesson that only shares words stays out
        self.assertEqual(ids(lessons.for_task("acme/api", activity="frontend", files=["web/app.tsx"])), [])
        self.assertEqual(ids(lessons.for_task("acme/api", activity="release")), [self.push["meta"]["id"]])

    def test_a_tool_call_recalls_the_lessons_of_its_command_and_files(self):
        self.assertEqual(ids(lessons.for_call("acme/api", command="git push --force origin main")),
                         [self.push["meta"]["id"]])
        self.assertEqual(ids(lessons.for_call("acme/api", command='git commit -m "explain git push --force"')), [])
        self.assertEqual(ids(lessons.for_call("acme/api", files=["/repo/db/migrations/V3__orders.sql"])),
                         [self.mig["meta"]["id"]])

    def test_tool_output_recalls_the_lesson_whose_error_it_shows(self):
        out = "ERROR: Validate failed: Migration Checksum Mismatch for migration version 3"
        self.assertEqual(ids(lessons.for_output("acme/api", out)), [self.mig["meta"]["id"]])
        self.assertEqual(ids(lessons.for_output("acme/api", "BUILD SUCCESS")), [])

    def test_the_cli_records_anchors_and_backfills_them_on_an_older_lesson(self):
        a = cli.build_parser().parse_args(["lesson", "add", "Tag pushed before the changelog", "--global",
                                           "--cause", "no check", "--prevent", "release with dev release",
                                           "--happened", "v3 was tagged before its changelog entry",
                                           "--command", "git tag", "--activity", "release", "--file", "CHANGELOG.md"])
        with mock.patch("sys.stdout"):
            self.assertEqual(a.fn(a), 0)
        les = next(x for x in lessons.all_lessons("global") if x["meta"]["title"].startswith("Tag pushed"))
        self.assertEqual(lessons.anchors(les)["commands"], ["git tag"])
        b = cli.build_parser().parse_args(["lesson", "anchor", self.words["meta"]["id"], "--activity", "testing",
                                           "--file", "tests/**", "--why", "backfill"])
        with mock.patch("sys.stdout"):
            self.assertEqual(b.fn(b), 0)
        got = lessons.anchors(lessons.find(self.words["meta"]["id"]))
        self.assertEqual((got["activities"], got["files"]), (["testing"], ["tests/**"]))


class ConsolidationTests(_Handoff):
    def test_without_ai_duplicates_merge_on_their_anchors_not_their_words(self):
        en = self.add("Edited an applied migration", files=["db/migrations/**"], activities=["database"],
                      errors=["checksum mismatch"])
        es = self.add("Se editó una migración ya aplicada", files=["db/migrations/**"], activities=["database"],
                      errors=["checksum mismatch"], new=True, why="the same mistake in Spanish, for the merge")
        self.add("Commit after a pipe hides failing tests", cause="tail hides the exit code", prevention="pipefail",
                 files=["ci/unit.yml"])  # the same words as the next one, and no anchor in common
        self.add("Commit chained after a pipe hides failing tests", cause="tail hides the exit code",
                 prevention="pipefail", files=["ci/integration.yml"])
        self.add("The guard refused a heredoc", files=["lib/devkit/guard.py"], activities=["tooling"],
                 errors=["Run as a command of its own"])
        self.add("The guard missed a nested shell", files=["lib/devkit/guard.py"], activities=["tooling"],
                 errors=["a nested bash -c call"])
        with mock.patch.dict(core.CFG, {"ai": {"enabled": False}}):
            merged = [i for k, i, _ in lessons.consolidate() if k == "merge"]
        self.assertEqual(len(merged), 1)
        self.assertIn(merged[0], (en["meta"]["id"], es["meta"]["id"]))

    def test_with_ai_the_model_decides_duplicates_in_any_language_within_its_budget(self):
        self.add("Commit after a pipe hides failing tests", cause="tail hides the exit code", prevention="pipefail",
                 commands=["git commit"], stack=["python"])
        self.add("Un commit tras un pipe oculta tests rotos", cause="tail oculta el código de salida",
                 prevention="usa pipefail", commands=["git commit"], stack=["java"], new=True,
                 why="the same mistake in Spanish, for the model to judge")
        self.add("Releases tagged before the changelog", cause="no check", prevention="dev release",
                 commands=["git commit"], files=["CHANGELOG.md"], new=True, why="another mistake, same command")
        calls, real = [], subprocess.run

        def fake(argv, **kw):
            if argv[0] not in ("claude", "codex"):
                return real(argv, **kw)  # git, for the handoff repository
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="yes\n", stderr="")
        with mock.patch.dict(core.CFG, {"ai": {"enabled": True, "agent": "claude", "daily_calls": 1}}), \
                mock.patch.object(ai.subprocess, "run", fake):
            merged = [i for k, i, _ in lessons.consolidate() if k == "merge"]
        self.assertEqual(len(calls), 1)  # one question a day, then the anchors decide
        self.assertEqual(len(merged), 1)


class BackgroundAITests(_Handoff):
    def fake(self, out="yes\n", code=0, exc=None):
        self.calls = []

        def run(argv, **kw):
            self.calls.append((argv, kw))
            if exc:
                raise exc
            if "-o" in argv:
                Path(argv[argv.index("-o") + 1]).write_text(out)
                return subprocess.CompletedProcess(argv, code, stdout="progress lines", stderr="")
            return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        return run

    def ask(self, cfg, **kw):
        with mock.patch.dict(core.CFG, {"ai": cfg}), mock.patch.object(ai.subprocess, "run", self.fake(**kw)):
            return ai.ask("Same mistake? yes or no", "test")

    def test_it_is_off_until_the_developer_turns_it_on_and_then_runs_nothing(self):
        self.assertIsNone(self.ask({}))
        self.assertEqual(self.calls, [])

    def test_claude_runs_headless_with_a_small_model_no_tools_and_no_devkit_hooks(self):
        self.assertEqual(self.ask({"enabled": True, "agent": "claude"}), "yes")
        argv, kw = self.calls[0]
        self.assertEqual(argv[:4], ["claude", "-p", "--model", "haiku"])
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(kw["input"], "Same mistake? yes or no")
        self.assertEqual(kw["env"]["DEVKIT_HEADLESS"], "1")
        self.assertFalse(core.gitout(kw["cwd"], "rev-parse", "--show-toplevel"))  # outside every repository

    def test_codex_runs_headless_read_only_and_answers_with_its_last_message(self):
        self.assertEqual(self.ask({"enabled": True, "agent": "codex"}, out="no\n"), "no")
        argv, _ = self.calls[0]
        self.assertEqual(argv[:6], ["codex", "exec", "-m", "gpt-6-luna", "--sandbox", "read-only"])

    def test_the_daily_budget_stops_the_calls_until_the_next_day(self):
        cfg = {"enabled": True, "agent": "claude", "daily_calls": 2}
        with mock.patch.dict(core.CFG, {"ai": cfg}), mock.patch.object(ai.subprocess, "run", self.fake()):
            got = [ai.ask("q", "test") for _ in range(3)]
            tomorrow = ai.ask("q", "test", now=time.time() + DAY)
        self.assertEqual(got, ["yes", "yes", None])
        self.assertEqual(tomorrow, "yes")
        self.assertEqual(len(self.calls), 3)

    def test_a_failed_or_slow_call_answers_nothing(self):
        self.assertIsNone(self.ask({"enabled": True, "agent": "claude"}, code=1))
        self.assertIsNone(self.ask({"enabled": True, "agent": "claude"},
                                   exc=subprocess.TimeoutExpired(["claude"], 60)))


class SkillRuleTests(unittest.TestCase):
    def setUp(self):
        self._installed = skillguard.installed
        skillguard.installed = lambda agent: {"frontend-design", "impeccable", "github-actions-hardening", "cicd",
                                              "delivery", "docs", "testing", "backend", "devsecops"}

    def tearDown(self):
        skillguard.installed = self._installed

    def test_no_rule_reads_the_developers_words(self):
        for r in skillguard.rules():
            with self.subTest(rule=r["id"]):
                self.assertNotIn("prompt", r)
                self.assertTrue(r.get("files") or r.get("commands") or r.get("activities"))

    def test_a_declared_kind_of_work_and_a_command_name_their_skills(self):
        self.assertIn("workflows", [r["id"] for r in skillguard.rules_for_activity("cicd")])
        self.assertIn("api", [r["id"] for r in skillguard.rules_for_activity("backend")])
        self.assertEqual([r["id"] for r in skillguard.rules_for_command("gh workflow run ci.yml")], ["workflows"])
        self.assertEqual(skillguard.rules_for_command('echo "gh workflow run"'), [])  # text, not the command

    def test_a_brief_gets_the_skills_of_its_member_or_its_declared_kind_of_work_in_any_language(self):
        ux = skillguard.on_agent_brief("claude", {"prompt": "Diseña las pantallas de alta",
                                                  "description": "Sati: pantallas"})
        self.assertIn("`frontend-design`", ux["prompt"])
        ci = skillguard.on_agent_brief("claude", {"prompt": "Kind of work: CI/CD pipelines (`cicd`)\n\narregla esto",
                                                  "description": "Neo: el pipeline"})
        self.assertIn("`github-actions-hardening`", ci["prompt"])
        self.assertNotIn("required skills", skillguard.on_agent_brief(
            "claude", {"prompt": "Redesign the dashboard UI", "description": "helper"})["prompt"])


def _transcript(records) -> str:
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in records))
    return path


def _prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool(name, inp):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name,
                                                                                "input": inp}]}}


def _say(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TurnScopeTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q", self.repo.name], check=True)
        self.paths = []

    def tearDown(self):
        for p in self.paths:
            os.unlink(p)
        self.repo.cleanup()

    def decide(self, records, sess=None):
        p = _transcript(records)
        self.paths.append(p)
        return scope.decide({"transcript_path": p}, sess or {})

    def test_a_turn_is_development_work_from_what_it_did_not_from_its_words(self):
        edit = _tool("Edit", {"file_path": os.path.join(self.repo.name, "app.py")})
        self.assertTrue(self.decide([_prompt("dame una receta de paella"), edit]))
        self.assertFalse(self.decide([_prompt("refactor the payment service"), _tool("WebSearch", {"query": "x"})]))
        self.assertFalse(self.decide([_prompt("refactor the payment service"), _say("Here is how.")]))

    def test_repository_tools_in_a_session_inside_a_repository_count(self):
        read = _tool("Read", {"file_path": os.path.join(self.repo.name, "app.py")})
        self.assertTrue(self.decide([_prompt("¿qué hace esto?"), read], {"top": self.repo.name}))
        self.assertFalse(self.decide([_prompt("¿qué hace esto?"), _tool("WebSearch", {"query": "x"})],
                                     {"top": self.repo.name}))

    def test_the_status_block_is_the_models_declaration(self):
        self.assertTrue(self.decide([_prompt("¿cómo va?"), _say("**[Firefly> DevKit]** api · `main` · Status\n- x")]))

    def test_consulting_work_counts_only_through_development_commands(self):
        deck = _tool("Skill", {"skill": "firefly-consulting-desktop:firefly-deck"})
        edit = _tool("Edit", {"file_path": os.path.join(self.repo.name, "deck.md")})
        self.assertFalse(self.decide([_prompt("fix the deck"), deck, edit], {"top": self.repo.name}))
        self.assertTrue(self.decide([_prompt("x"), deck, _tool("Bash", {"command": "git commit -m 'fix: x'"})]))


class CatalogTests(unittest.TestCase):
    def test_a_brief_declares_its_kind_of_work_and_carries_its_knowledge(self):
        r = crew.load(team=False)
        brief = crew.brief_text(crew.member("neo", r), "arregla el pipeline", activity="cicd", roster=r)
        self.assertIn("Kind of work: CI/CD pipelines (`cicd`)", brief)
        self.assertIn("github-actions-hardening", brief)
        a = cli.build_parser().parse_args(["crew", "brief", "neo", "arregla", "--activity", "cicd"])
        self.assertEqual(a.activity, "cicd")


class ActivityFileTests(unittest.TestCase):
    # A kind of work's own `files` globs (config/crew.json) anchor a step before its words, so a step about the
    # translations is i18n work whatever language it is written in (R2-10)
    def test_a_kind_of_works_own_files_anchor_the_step_in_any_language(self):
        r = crew.load(team=False)
        for text in ("translate the checkout screen", "traduce la pantalla de pago",
                     "チェックアウト画面を翻訳する"):
            with self.subTest(text=text):
                a, source = raci.anchor(text, files=["src/locales/es.json"], roster=r)
                self.assertEqual(((a or {}).get("id"), source), ("i18n", "files"))

    # A file of two kinds of work (an ADR is architecture and docs) anchors to the kind whose own globs name it, and
    # the member whose file kind it also is may still take it
    def test_a_file_of_two_kinds_anchors_to_its_own_kind_and_either_member_may_take_it(self):
        r = crew.load(team=False)
        adr = ["docs/adr/0007-queue.md"]
        self.assertEqual(raci.anchor("escribe el ADR", files=adr, roster=r)[0]["id"], "architecture")
        self.assertIsNone(raci.check_assignment("ghost", "escribe el ADR", roster=r, files=adr))
        self.assertIsNone(raci.check_assignment("architect", "escribe el ADR", roster=r, files=adr))


class WizardTests(unittest.TestCase):
    class Lines:
        def __init__(self, answer):
            self.answer, self.asked = answer, []

        def yesno(self, question, default):
            self.asked.append(question)
            return self.answer

    def test_the_wizard_asks_about_background_ai_once_and_it_starts_off(self):
        self.assertFalse(wizard.tui_defaults({"config": {}})["ai"])
        first = self.Lines(True)
        self.assertTrue(wizard.ask_ai(first, {}))
        self.assertEqual(len(first.asked), 1)
        again = self.Lines(False)
        self.assertTrue(wizard.ask_ai(again, {"ai": {"enabled": True, "asked": True}}))
        self.assertEqual(again.asked, [])
        a = wizard.tui_defaults({"config": {}})
        new = wizard.build_config({}, dict(a, ai=True), "ana")
        self.assertEqual(new["ai"], {"enabled": True, "asked": True})


if __name__ == "__main__":
    unittest.main()
