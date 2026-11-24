# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""JSON that a teammate, another Mac or another agent session writes is read with core.load_json, which reads a value
nested deeper than core.JSON_MAX_DEPTH as invalid. Without it, a deep document crashed what read it: Python 3.9 to
3.13 fail in the parser at about a thousand levels, Python 3.15 parses far deeper and then fails in what reads the
value (str, repr, json.dumps, copy.deepcopy). Each test feeds one reader a deep document and checks that the reader
treats it as it treats any unreadable one. Every file here is temporary. Run: python3 -m unittest discover -s tests -t ."""
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

from devkit import approvals, core, crew, dashboard, lessonrules, lessons  # noqa: E402

# 1,000 levels: past the parser on Python 3.9 to 3.13, parsed by 3.15, where copy.deepcopy already fails on it.
# 120,000 levels: still under the 256 KB caps of a rule or presence file, parsed by 3.15, where str() and
# json.dumps of the value then fail.
SHALLOW_CRASH, DEEP_CRASH = 1_000, 120_000


def deep_list(d: int) -> str:
    return "[" * d + "]" * d


class TempFolder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)


class LessonRuleFileTests(TempFolder):
    """lessons/rules/<scope>.json in the handoff repository: any teammate's push, read by every hook call."""

    SCOPE = "global"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(lessons, "LESSONS", self.root / "lessons")
        p.start()
        self.addCleanup(p.stop)
        (self.root / "lessons" / "rules").mkdir(parents=True)
        error = "fatal: refusing to merge unrelated histories"
        self.rule = {"id": lessonrules.rule_id(self.SCOPE, "L-abc", "output", error), "kind": "output",
                     "mode": "warn", "lesson": "L-abc", "error": error}

    def write(self, text: str):
        lessonrules.rule_file(self.SCOPE).write_text(text)

    def test_a_deeply_nested_rule_file_is_ignored_and_said(self):
        self.write(json.dumps({"version": 1, "rules": [self.rule]}))
        self.assertEqual([r["id"] for r in lessonrules.load([self.SCOPE])], [self.rule["id"]])
        valid = json.dumps({"version": 1, "rules": [self.rule]})
        for name, text in (("deep id", '{"version": 1, "rules": [{"id": ' + deep_list(DEEP_CRASH) + "}]}"),
                           ("deep extra field", valid[:-2] + ', "note": ' + deep_list(SHALLOW_CRASH) + "}]}")):
            with self.subTest(name):
                self.write(text)
                self.assertLess(len(text), lessonrules.MAX_FILE_BYTES)
                self.assertEqual(lessonrules.load([self.SCOPE]), [])
                self.assertEqual(len(lessonrules.PROBLEMS), 1)
                self.assertIn("cannot be read", lessonrules.PROBLEMS[0])


class PresenceTests(TempFolder):
    """<handoff>/presence/<user>@<host>.json: each Mac of the team writes its own, the dashboard reads them all."""

    def write(self, extra: str) -> None:
        d = self.root / "presence"
        d.mkdir(exist_ok=True)
        updated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (d / "ana@mbp.json").write_text('{"user": "ana", "host": "mbp", "updated": "' + updated + '", "sessions": [],'
                                        ' "locks": [], "note": ' + extra + "}")

    def test_a_deeply_nested_presence_file_is_skipped(self):
        self.write('"in a meeting"')
        self.assertEqual([p["user"] for p in dashboard.read_presence(self.root, {})], ["ana"])
        for d in (SHALLOW_CRASH, DEEP_CRASH):
            with self.subTest(depth=d):
                self.write(deep_list(d))
                self.assertEqual(dashboard.read_presence(self.root, {}), [])


class CrewProcedureTests(TempFolder):
    """`dev crew set <member> procedure '<JSON>'` writes the team's crew file (crew/crew.json) that every teammate's
    devkit reads: a value no reader can read must be refused before it is written."""

    def setUp(self):
        super().setUp()
        self.handoff = self.root / "handoff"
        subprocess.run(["git", "init", "-q", str(self.handoff)], check=True)
        from devkit import audit
        for p in (mock.patch.object(core, "HANDOFF", self.handoff),
                  mock.patch.object(audit, "AUDIT", self.root / "audit"),
                  mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "ana", "GIT_AUTHOR_EMAIL": "ana@example.com",
                                               "GIT_COMMITTER_NAME": "ana", "GIT_COMMITTER_EMAIL": "ana@example.com",
                                               "DEVKIT_NO_AUTOSYNC": "1"})):
            p.start()
            self.addCleanup(p.stop)
        crew._CACHE.clear()
        self.addCleanup(crew._CACHE.clear)

    def test_a_deeply_nested_procedure_is_refused_and_the_team_file_kept(self):
        crew.set_member("tank", "goal", "Ship the API.", "the team's wording", by="ana@mbp")
        before = crew.overlay_path().read_text()
        for name, phase in (("parser depth", deep_list(SHALLOW_CRASH)), ("deep enough to void the team file", deep_list(62)),
                            ("not text", '["OpenAPI first"]')):
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    crew.set_member("tank", "procedure", '[{"phase": ' + phase + ', "do": "Write it."}]', "deep",
                                    by="ana@mbp")
                self.assertEqual(crew.overlay_path().read_text(), before)
        self.assertEqual(crew.member("tank")["goal"], "Ship the API.")


class TranscriptTests(TempFolder):
    """An agent's transcript is written by Claude Code from what that agent's model produced, tool inputs included."""

    def test_a_deeply_nested_last_record_is_no_interruption(self):
        f = self.root / "agent-a1.jsonl"
        rec = {"type": "user", "timestamp": "2026-11-23T10:00:00Z",
               "message": {"role": "user", "content": "[Request interrupted by user for tool use]"}}
        f.write_text('{"type": "assistant"}\n' + json.dumps(rec) + "\n")
        self.assertIsNotNone(approvals.interrupted_at(f))
        f.write_text('{"type": "assistant"}\n' + json.dumps(rec)[:-1] + ', "x": ' + deep_list(SHALLOW_CRASH) + "}\n")
        self.assertLess(f.stat().st_size, approvals.TAIL_BYTES)
        self.assertIsNone(approvals.interrupted_at(f))


if __name__ == "__main__":
    unittest.main()
