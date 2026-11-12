# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Registry issue titles from devkit's title table (config/bug-titles.json), filled only from allowlisted report
# fields. The title mechanics run on a small table of their own, so a change of wording in the real table breaks
# none of them; tests/test_consistency.py checks the real table.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_bugs_render
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import bugs  # noqa: E402

NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
DAY = 86400.0
TABLE = {
    "guard": {
        "segment:vendor-ext": {"title": "Guard refuses {word} as a plugin or MCP change",
                               "what": "The guard refused {word}.", "reproduce": "Run {word} with `mcp`."},
        "hooks:crew-lane": {
            "title": "Guard refuses a crew member command when its lane check fails", "what": "Now.",
            "reproduce": "Now.",
            "before": {"26.11.15": {"title": "Guard refuses crew member commands of several kinds under one id",
                                    "what": "Up to 26.11.15.", "reproduce": "Old."},
                       "26.11.9": {"title": "Guard refuses crew member commands in its first form",
                                   "what": "Up to 26.11.9.", "reproduce": "Oldest."}}},
        "": {"title": "Guard refuses {word} for a reason the report does not name", "what": "Unknown.",
             "reproduce": "Unknown."},
    },
    "crash": {"title": "dev {command} crashes with {exc_class}", "what": "`dev {command}` stopped ({exc_class}).",
              "reproduce": "Run `dev {command}`.",
              "without": {"command": {"title": "devkit crashes with {exc_class}", "what": "A command stopped.",
                                      "reproduce": "No command."}}},
    "hook": {"title": "{event} hook fails with {exc_class}", "what": "A hook failed.", "reproduce": "Start a session.",
             "without": {"exc_class": {"title": "{event} hook runs out of time", "what": "A hook ran too long.",
                                       "reproduce": "Wait."}}},
    "command": {"title": "dev {command} does not work as expected", "what": "Someone reported `dev {command}`.",
                "reproduce": "Run `dev {command}`.",
                "without": {"command": {"title": "A dev command does not work as expected",
                                        "what": "Someone reported a command.", "reproduce": "Read below."}}},
    "events": {"": "A devkit", "session-start": "Session start", "stop": "Stop"},
    "words": {"": "a command", "<program>": "a program it cannot name", "<path>": "a program run by its path",
              "*": "a {word} command", "awk": "an awk command"},
}
CRASH_FRAMES = ["work.actor_str"]


def guard_report(rid="segment:vendor-ext", word="claude", version="26.11.10", **extra):
    return {"devkit_bug": 1, "type": "report", "kind": "guard", "version": version, "channel": "release",
            "macos": "27", "python": "3.15", "agent": "claude", "surface": "", "command": "", "exc_class": "",
            "frames": [], "refusal_id": rid, "word": word, "event": "", "exit": None, "duration": "",
            "impact": "blocked", "fingerprint": bugs.guard_fingerprint(rid, word), **extra}


def crash_report(**extra):
    p = {"devkit_bug": 1, "type": "report", "kind": "crash", "version": "26.11.7", "channel": "release",
         "macos": "27", "python": "3.15", "agent": "codex", "surface": "", "command": "progress step",
         "exc_class": "KeyError", "frames": CRASH_FRAMES, "refusal_id": "", "word": "", "event": "", "exit": 70,
         "duration": "", "impact": "blocked"}
    p.update(extra)
    p.setdefault("fingerprint", bugs.fingerprint("crash", exc_class=p["exc_class"], frames=tuple(p["frames"])))
    return p


def command_report(**extra):
    return {**crash_report(exc_class="", frames=[], exit=None), "kind": "command", "fingerprint": "0123456789ab",
            **extra}


def folded(report, *votes, now=NOW):
    rec = bugs.fold({}, report, author_hash="h0", author_assoc="NONE", now=now)
    for i, (impact, when) in enumerate(votes, 1):
        vote = {"devkit_bug": 1, "type": "vote", "fingerprint": report["fingerprint"], "version": report["version"],
                "impact": impact, "window": bugs._day(when)}
        rec = bugs.fold(rec, vote, author_hash=f"h{i}", author_assoc="NONE", now=when)
    return rec


class TableCase(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(bugs, "bug_titles", lambda: TABLE)
        patch.start()
        self.addCleanup(patch.stop)


class TitleTests(TableCase):
    def test_a_guard_title_names_the_command_word_as_a_phrase(self):
        cases = {"sed": "Guard refuses a sed command as a plugin or MCP change",
                 "awk": "Guard refuses an awk command as a plugin or MCP change",
                 "<program>": "Guard refuses a program it cannot name as a plugin or MCP change",
                 "<path>": "Guard refuses a program run by its path as a plugin or MCP change",
                 "": "Guard refuses a command as a plugin or MCP change"}
        for word, title in cases.items():
            with self.subTest(word=word):
                self.assertEqual(bugs.title_for(guard_report(word=word)), title)

    def test_a_refusal_id_the_table_does_not_know_takes_the_entry_for_none(self):
        self.assertEqual(bugs.title_for(guard_report(rid="check_git:force-push", word="git")),
                         "Guard refuses a git command for a reason the report does not name")

    def test_a_title_never_carries_free_text_or_a_class_devkit_does_not_know(self):
        report = crash_report(title="acme payments broke", exc_class="AcmeBillingError")
        self.assertEqual(bugs.title_for(report), "dev progress step crashes with an error")
        self.assertEqual(bugs.title_for(crash_report(title="acme payments broke")),
                         "dev progress step crashes with KeyError")

    def test_an_empty_slot_takes_the_variant_written_for_it(self):
        hook = {**crash_report(command="", event="session-start"), "kind": "hook"}
        cases = [(crash_report(command=""), "devkit crashes with KeyError"),
                 (hook, "Session start hook fails with KeyError"),
                 ({**hook, "exc_class": ""}, "Session start hook runs out of time"),
                 ({**hook, "event": ""}, "A devkit hook fails with KeyError"),
                 (command_report(), "dev progress step does not work as expected"),
                 (command_report(command=""), "A dev command does not work as expected")]
        for report, title in cases:
            with self.subTest(title=title):
                self.assertEqual(bugs.title_for(report), title)

    def test_a_bug_keeps_the_meaning_its_refusal_id_had_when_first_seen(self):
        cases = {"26.11.8": "Guard refuses crew member commands in its first form",
                 "26.11.9": "Guard refuses crew member commands in its first form",
                 "26.11.10": "Guard refuses crew member commands of several kinds under one id",
                 "26.11.15": "Guard refuses crew member commands of several kinds under one id",
                 "26.11.16": "Guard refuses a crew member command when its lane check fails"}
        for first, title in cases.items():
            with self.subTest(first_version=first):
                rec = folded(guard_report(rid="hooks:crew-lane", word="", version=first))
                self.assertEqual((bugs.human_title(rec), rec["title"]), (title, title))
                self.assertEqual(bugs.title_for(guard_report(rid="hooks:crew-lane", word="", version=first)), title)


class ExceptionNameTests(unittest.TestCase):
    # readable text names a class only when Python, its standard library or devkit defines it
    def test_only_builtin_standard_library_or_devkit_classes_are_named(self):
        cases = {"KeyError": "KeyError", "JSONDecodeError": "JSONDecodeError",
                 "CalledProcessError": "CalledProcessError", "Duplicate": "Duplicate",
                 "AcmeBillingError": "an error", "json.decoder.JSONDecodeError": "an error", "": "an error",
                 None: "an error"}
        for value, shown in cases.items():
            with self.subTest(exc_class=value):
                self.assertEqual(bugs.readable_exc(value), shown)


if __name__ == "__main__":
    unittest.main()
