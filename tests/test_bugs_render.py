# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Registry issues people can read: a title from devkit's title table (config/bug-titles.json), filled only from
# allowlisted report fields; the issue body, the record comment and the payload comments, each with its data in a
# collapsed block that the parsers of every released client and of the registry workflow still read; and free text
# shown only escaped, so no mention, link, HTML tag or code fence can start in it.
# The title mechanics run on a small table of their own, so a change of wording in the real table breaks none of
# them; tests/test_consistency.py checks the real table.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_bugs_render
import html
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import bugs  # noqa: E402

_spec = importlib.util.spec_from_file_location("bug_registry_action", ROOT / "tools" / "bug_registry_action.py")
action = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(action)

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


def hook_report(**extra):
    p = {**crash_report(command="", exit=None, **extra), "kind": "hook"}
    p["fingerprint"] = bugs.fingerprint("hook", exc_class=p["exc_class"], frames=tuple(p["frames"]))
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


def outside_details(body: str) -> str:
    return re.sub(r"<details>.*?</details>", "", body, flags=re.S)


def bullet(body: str, name: str) -> str:
    return next(line for line in body.splitlines() if line.startswith(f"- **{name}:** "))[len(name) + 8:]


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


class MarkdownTextTests(unittest.TestCase):
    HOSTILE = ("```js\nrun()``` @someone [x](https://evil.example) <img src=x onerror=a()> ![i](u) | a | ~~s~~ "
               "*b* _i_ \\ & #12 www.evil.example a@b.co :+1:")

    def test_free_text_cannot_start_markup_a_mention_a_reference_or_a_link(self):
        out = bugs.md_text(self.HOSTILE)
        for c in "\\`*_[]<>|~\n":
            self.assertNotIn(c, out)
        # GitHub finds mentions, issue references and links in the text after its entities, too
        self.assertEqual(re.findall(r"@\w|#\d|://|www\.", html.unescape(out)), [])

    def test_escaped_text_reads_as_the_reporters_words(self):
        shown = html.unescape(bugs.md_text(self.HOSTILE)).replace("⁠", "")
        self.assertEqual(shown, " ".join(self.HOSTILE.split()))


class IssueBodyTests(TableCase):
    def assert_parses(self, body, first):
        self.assertEqual(action._first_report({"body": body}), first)  # the workflow reads the first report
        self.assertEqual(bugs.parse_payload(body), first)
        human = outside_details(body)
        self.assertNotIn("```", human)
        self.assertNotIn(bugs.RECORD_MARK, body)
        self.assertEqual(body.count("```"), 2)

    def test_every_kind_of_body_parses_with_todays_parsers(self):
        reports = [guard_report(), crash_report(template="KeyError: <str>"),
                   hook_report(event="stop", duration="1-10s"),
                   command_report(title="``` @someone [x](https://evil.example) <b>bold</b>", text="`a` | b")]
        for report in reports:
            with self.subTest(kind=report["kind"]):
                body = bugs.render_issue(folded(report), report, created="2027-01-15", now=NOW)
                self.assert_parses(body, report)

    def test_the_reporters_description_shows_as_plain_text(self):
        report = command_report(title="``` @someone [x](https://evil.example) <b>bold</b>")
        body = bugs.render_issue(folded(report), report, created="2027-01-15", now=NOW)
        line = next(ln for ln in outside_details(body).splitlines() if ln.startswith("- The reporter's description"))
        self.assertEqual(re.findall(r"[`<>\[\]]|@\w|://", line), [])
        self.assertIn("&#64;&#8288;someone", line)

    def test_the_first_screen_states_status_impact_frequency_versions_and_workaround(self):
        report = guard_report()
        rec = {**folded(report), "status": "fixed", "fixed_in": "26.11.16", "triage": {"verdict": "misleading-message"}}
        body = bugs.render_issue(rec, report, created="2027-01-15", now=NOW)
        self.assertEqual(body.split("\n\n## ")[0].splitlines(), [
            "The guard refused a claude command.",
            "",
            "- **Status:** Fixed in 26.11.16: run `dev update` to get it. The refusal is right, but its message was "
            "misleading.",
            "- **Impact:** Blocks work for everyone who hit it (1 person).",
            "- **How often:** 1 person in the last 30 days, on 1 day. First seen 2027-01-15, last seen 2027-01-15.",
            "- **Versions:** seen on 26.11.10.",
            "- **Workaround:** None known yet."])
        self.assertIn("\n- `dev update` gets the fix, in 26.11.16.\n", body)

    def test_the_status_line_follows_the_record_and_its_verdict(self):
        base = folded(guard_report())
        cases = [({}, "Open. Not triaged yet."),
                 ({"triage": {"verdict": "false-positive"}},
                  "Open. Confirmed: the guard refused a command it should allow."),
                 ({"triage": {"verdict": "wont-fix"}}, "Open. Not triaged yet."),  # not a verdict devkit knows
                 ({"status": "regressed", "fixed_in": "26.11.9"},
                  "Regressed: seen again after its fix in 26.11.9. Not triaged yet."),
                 ({"status": "duplicate", "duplicate_of": 12},
                  "Duplicate of #12. Reports and votes for this bug count there. Not triaged yet.")]
        for change, line in cases:
            with self.subTest(change=change):
                body = bugs.render_issue({**base, **change}, guard_report(), now=NOW)
                self.assertEqual(bullet(body, "Status"), line)

    def test_the_impact_line_counts_who_was_stopped(self):
        def rec(*impacts):
            return {**folded(guard_report()), "voters": {f"v{i}": {"impact": k, "ts": NOW} for i, k in
                                                         enumerate(impacts)}}
        cases = [(("blocked", "blocked", "blocked"), "Blocks work for everyone who hit it (3 people)."),
                 (("blocked", "detoured", "detoured"),
                  "Blocks work for 1 of 3 people; 2 got around it with a workaround."),
                 (("detoured", "detoured"), "Slows work down: all 2 people got around it with a workaround."),
                 (("detoured",), "Slows work down: the person who hit it got around it with a workaround."),
                 (("detoured", "minor"), "Slows work down: 1 of 2 people got around it with a workaround."),
                 (("minor", "minor"), "Minor: 2 people saw it, and none was stopped."),
                 ((), "No one reported it in the last 30 days.")]
        for impacts, line in cases:
            with self.subTest(impacts=impacts):
                self.assertEqual(bullet(bugs.render_issue(rec(*impacts), guard_report(), now=NOW), "Impact"), line)

    def test_a_served_workaround_is_shown_and_a_proposed_one_only_counted(self):
        verified = {"id": "d1", "steps": [{"text": "Run it *twice* from a new shell"}], "since": "26.11.10",
                    "until": "", "author_assoc": "MEMBER", "author_hash": "a1", "evidence": {"successes": 1},
                    "confirmations": [{"author_assoc": "OWNER", "author_hash": "a2"}], "failures": []}
        team = {**verified, "id": "d2", "steps": [{"hint": "use-tool", "tool": "Edit", "word": "sed"}],
                "confirmations": []}
        proposed = {**verified, "id": "d3", "steps": [{"text": "move the folder named zebra"}], "author_assoc": "NONE",
                    "confirmations": []}
        cases = [([verified, proposed], "Yes, confirmed by a maintainer. See Workaround below.",
                  "1. Run it &#42;twice&#42; from a new shell. Confirmed by a maintainer, for devkit 26.11.10 and "
                  "later."),
                 ([team], "Yes, found by a team agent and not yet reviewed. See Workaround below.",
                  "1. Use the Edit tool instead of `sed`. Found by a team agent, not yet reviewed, for devkit 26.11.10 "
                  "and later."),
                 ([proposed, {**proposed, "id": "d4", "steps": [{"text": "other"}]}],
                  "None confirmed yet; 2 proposed workarounds wait for review.", None),
                 ([proposed], "None confirmed yet; 1 proposed workaround waits for review.", None)]
        for detours, line, step in cases:
            with self.subTest(line=line):
                body = bugs.render_issue({**folded(guard_report()), "detours": detours}, guard_report(), now=NOW)
                self.assertEqual(bullet(body, "Workaround"), line)
                self.assertNotIn("zebra", body)
                if step:
                    self.assertIn(f"## Workaround\n\n{step}\n", body)
                else:
                    self.assertNotIn("## Workaround", body)

    # GitHub refuses an issue body over 65,536 characters, and the workflow's rewrite of that issue then fails: twenty
    # served workarounds of escaped text took the body past it. The body stays under the limit: the workarounds that
    # do not fit become one line that says where the rest is, and the report data the workflow reads stays whole
    def test_a_body_with_more_workarounds_than_fit_stays_under_githubs_limit(self):
        steps = [{"text": "*" * 200}] * 5  # each star shows as a five-character entity
        served = [{"id": f"d{i}", "steps": steps, "since": "26.11.10", "until": "", "author_assoc": "MEMBER",
                   "author_hash": "a1", "evidence": {"successes": 1}, "failures": [],
                   "confirmations": [{"author_assoc": "OWNER", "author_hash": "a2"}]} for i in range(1, 21)]
        report = guard_report()
        body = bugs.render_issue({**folded(report), "detours": served}, report, now=NOW)
        self.assertLessEqual(len(body), 65536)
        section = body.split("## Workaround\n\n")[1].split("\n\n## ")[0]
        shown = [ln for ln in section.splitlines() if ln.startswith("1. ")]
        self.assertTrue(0 < len(shown) < 20, len(shown))
        self.assertTrue(section.endswith(f"{20 - len(shown)} more workarounds are in the registry's record: "
                                         f"`dev bug show {report['fingerprint']}` lists them all."), section[-200:])
        self.assert_parses(body, report)

    def test_how_to_reproduce_lists_the_reports_devkit_facts(self):
        report = crash_report(frames=["<local>", "work.actor_str"], template="KeyError: <str>")
        body = bugs.render_issue(folded(report), report, now=NOW)
        section = body.split("## How to reproduce\n\n")[1].split("\n\n## ")[0]
        self.assertEqual(section.splitlines(), [
            "Run `dev progress step`.",
            "",
            "- Command: `dev progress step`",
            "- Error: `KeyError`",
            "- devkit's stack, innermost first: `work.actor_str`",
            "- Error message, with its values taken out: KeyError: &#60;str&#62;",
            "- First report: Codex, macOS 27, Python 3.15, a release install, devkit 26.11.7"])
        hook = hook_report(event="session-start", duration="1-10s", exc_class="AcmeError")
        human = outside_details(bugs.render_issue(folded(hook), hook, now=NOW))
        self.assertIn("\n- Hook event: `session-start`, 1 to 10 seconds\n", human)
        self.assertNotIn("AcmeError", human)  # a class devkit does not know is never named in the text
        self.assertNotIn("\n- Error: ", human)

    def test_no_record_field_outside_the_human_text_reaches_the_text(self):
        # an allowlist: every record key but the ones the text is written from holds a sentinel, so a key added
        # later is covered without a change here; inside the human-text fields, the hashes, ids and marks too
        human_text = {"devkit_bug", "fingerprint", "kind", "component", "status", "fixed_in", "duplicate_of",
                      "first_version", "versions", "report", "days_seen", "last_seen", "score", "triage", "detours"}
        report = crash_report(template="KeyError: <str>")
        detour = {"id": "SENTINELid", "steps": [{"hint": "from-root", "dev": "progress step"}], "since": "26.11.7",
                  "until": "", "author_assoc": "MEMBER", "author_hash": "SENTINELauthor",
                  "author_kind": "SENTINELkind", "evidence": {"tools": ["SENTINELtool"], "successes": 424242},
                  "confirmations": [{"author_assoc": "OWNER", "author_hash": "SENTINELconfirm", "ts": 434343}],
                  "failures": [{"author_assoc": "NONE", "author_hash": "SENTINELfail", "ts": 454545}],
                  "flagged": False, "ts": 464646, "trust": "SENTINELtrust"}
        rec = {**folded(report, ("detoured", NOW)), "seen": 474747, "folds": {"by": {"SENTINELfold": 1}},
               "regression": ["SENTINELregression"], "older_voters": {"blocked": {"n": 2, "ts": NOW}},
               "title": "SENTINELtitle", "detours": [detour]}
        rec["voters"] = {f"SENTINELvoter{i}": v for i, v in enumerate(rec["voters"].values())}
        planted = {k: v if k in human_text else f"SENTINEL{k}" for k, v in rec.items()}
        for name, r in (("well-formed", rec), ("planted", planted)):
            for text in (bugs.render_issue(r, report, now=NOW), bugs.render_record(r)):
                with self.subTest(record=name, text=text[:20]):
                    human = outside_details(text)
                    self.assertEqual(re.findall(r"SENTINEL\w*|4[2-7]4[2-7]4[2-7]", human), [])
        self.assertIn("**Workaround:** Yes, confirmed by a maintainer", bugs.render_issue(rec, report, now=NOW))


class RecordTests(unittest.TestCase):
    def test_the_record_keeps_the_first_report_but_none_of_its_free_text(self):
        # the bot writes the record: what the reporter typed stays in the reporter's own issue body
        report = command_report(title="the zebra step fails", text="after a giraffe rebase")
        rec = folded(report)
        self.assertEqual(rec["first"], {k: v for k, v in report.items() if k not in ("title", "text")})
        self.assertEqual(re.findall(r"zebra|giraffe", bugs.render_record(rec)), [])
        crash = folded(crash_report(template="KeyError: <str> in the okapi module"))
        self.assertNotIn("okapi", bugs.render_record(crash))

    def test_the_record_counts_the_days_the_bug_was_seen(self):
        report = guard_report()
        rec = folded(report, ("blocked", NOW + 60), ("minor", NOW + DAY), ("detoured", NOW + 3 * DAY))
        self.assertEqual((rec["days_seen"], rec["last_day"]), (3, bugs._day(NOW + 3 * DAY)))
        self.assertIn(", on 3 days. ", bugs.render_issue(rec, report, now=NOW + 3 * DAY))


class RecordCommentTests(unittest.TestCase):
    def test_the_record_comment_folds_its_data_and_still_parses(self):
        rec = {**folded(guard_report()), "status": "fixed", "fixed_in": "26.11.16"}
        body = bugs.render_record(rec)
        self.assertEqual(bugs.parse_record(body), rec)
        self.assertEqual(body.splitlines()[0], f"**Bug {rec['fingerprint']}** · guard · 1 person · score 3.0 · "
                                               f"fixed · fixed in 26.11.16")
        self.assertLess(body.index("<details>"), body.index(bugs.RECORD_MARK))
        self.assertLess(body.index("```"), body.index("</details>"))
        crash = folded(crash_report())
        self.assertTrue(bugs.render_record(crash).startswith(f"**Bug {crash['fingerprint']}** · crash · progress · "))


class PayloadCommentTests(unittest.TestCase):
    def test_each_payload_comment_has_its_line_and_still_parses_in_the_workflow(self):
        fp = crash_report()["fingerprint"]
        common = {"devkit_bug": 1, "fingerprint": fp, "version": "26.11.7"}
        cases = [(crash_report(), "**devkit report** · blocked · devkit 26.11.7 · Codex, macOS 27"),
                 (crash_report(agent="", macos=""), "**devkit report** · blocked · devkit 26.11.7"),
                 ({**common, "type": "vote", "impact": "minor", "window": "2027-01-15"},
                  "**devkit vote** · minor · devkit 26.11.7 · 2027-01-15. The workflow counts it and deletes this "
                  "comment."),
                 ({**common, "type": "detour", "impact": "detoured", "evidence": {"successes": 1},
                   "steps": [{"hint": "use-tool", "tool": "Edit", "word": "sed"}, {"dev": "sync --offline"}]},
                  "**Workaround proposed** for devkit 26.11.7 and later: Use the Edit tool instead of `sed`. Run "
                  "`dev sync --offline`. The registry decides how far it is trusted; see the issue text."),
                 ({**common, "type": "confirm", "detour_id": "d1"}, "**Workaround d1 confirmed** on devkit 26.11.7."),
                 ({**common, "type": "failed", "detour_id": "d2", "impact": "blocked"},
                  "**Workaround d2 failed** on devkit 26.11.7.")]
        for payload, line in cases:
            with self.subTest(type=payload["type"], line=line[:30]):
                body = bugs.render_comment(payload)
                self.assertEqual(body.split("\n\n")[0], line)
                self.assertEqual(action._payload(body), payload)
                self.assertLess(body.index("<details>"), body.index("```json"))

    def test_a_free_text_step_in_a_comment_is_escaped(self):
        payload = {"devkit_bug": 1, "type": "detour", "fingerprint": "0123456789ab", "version": "26.11.7",
                   "impact": "detoured", "evidence": {"successes": 1},
                   "steps": [{"text": "Ask @someone about [this](x) *now* ~~please~~ _here_ #3"}]}
        step = bugs.render_comment(payload).split(" and later: ")[1].split(" The registry decides")[0]
        self.assertEqual(re.findall(r"[\[\]*~_]", step), [])
        self.assertEqual(re.findall(r"@\w|#\d", html.unescape(step)), [])
        self.assertEqual(html.unescape(step).replace("⁠", ""), payload["steps"][0]["text"] + ".")
        self.assertEqual(action._payload(bugs.render_comment(payload)), payload)


_BLOCK_AS_RELEASED = re.compile(r"^```json\n(.*?)\n```$", re.M | re.S)


def released_block(body):
    # the parsers every client since 26.11.8 ships, as released: a text they cannot read is lost to them for good
    if not isinstance(body, str) or body.count("```") != 2:
        return None
    blocks = _BLOCK_AS_RELEASED.findall(body)
    if len(blocks) != 1:
        return None
    try:
        obj = json.loads(blocks[0])
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def released_record(body):
    mark = "<!-- devkit-bug v1 -->"
    if not isinstance(body, str) or body.count(mark) != 1:
        return None
    rec = released_block(body[body.index(mark):]) if body.count("```") == 2 else None
    if rec is None or rec.get("devkit_bug") != 1 or not re.fullmatch(r"[0-9a-f]{12}", str(rec.get("fingerprint"))):
        return None
    return rec


def live(fp, rid, word, last_seen, seen, folds, title, created):
    # an issue of the registry as it is: its first report and its record (the voter's hash replaced)
    voter = "f" * 64
    first = {"agent": "claude", "channel": "release", "command": "", "devkit_bug": 1, "duration": "", "event": "",
             "exc_class": "", "exit": None, "fingerprint": fp, "frames": [], "impact": "blocked", "kind": "guard",
             "macos": "27", "python": "3.15", "refusal_id": rid, "surface": "", "type": "report",
             "version": "26.11.10", "word": word}
    rec = {"component": "guard", "detours": [], "devkit_bug": 1, "fingerprint": fp, "first_version": "26.11.10",
           "fixed_in": "", "folds": {"by": {voter: folds}, "day": "2026-11-11"},
           "impacts": {"blocked": 1, "detoured": 0, "minor": 0}, "kind": "guard", "last_seen": last_seen,
           "report": {"refusal_id": rid, **({"word": word} if word else {})}, "score": 3.0, "seen": seen,
           "status": "open", "title": title, "versions": ["26.11.10"],
           "voters": {voter: {"impact": "blocked", "ts": last_seen}}}
    return first, rec, created


class WorkedExampleTests(unittest.TestCase):
    # the records of issues 17, 20 and 21 as the registry holds them today, and a crash and a guard bug folded with
    # the real fold: in the new format each body, record and comment still parses, with the registry workflow's
    # parser and with the parsers every client since 26.11.8 ships, to the objects it was written from; the text
    # before the data holds no fence, mention or marker; the first screen is at most 8 lines; and each title is the
    # real title table's
    LIVE = {17: live("02913cd6bc00", "segment:vendor-ext", "claude", 1794364097.205787, 0, 1,
                     "guard refusal segment:vendor-ext on claude", "2026-11-11"),
            20: live("460e5e121022", "hooks:crew-lane", "", 1794432231.0799048, 6807357527, 2,
                     "guard refusal hooks:crew-lane", "2026-11-11"),
            21: live("7543ecaf8263", "segment:vendor-ext", "<program>", 1794435859.6128488, 0, 1,
                     "guard refusal segment:vendor-ext on program", "2026-11-11")}
    TITLES = {17: "Guard refuses a claude command as a plugin or MCP change",
              20: "Guard refuses crew member commands of several kinds under one id",
              21: "Guard refuses a program it cannot name as a plugin or MCP change",
              "crash": "dev progress step crashes with KeyError",
              "detour": "Guard refuses a sed command it cannot read"}

    def examples(self):
        for n, (first, rec, created) in self.LIVE.items():
            yield n, first, {**rec, "status": "fixed", "fixed_in": "26.11.16"}, created, []
        crash = crash_report(template="KeyError: <str>")
        rec = folded(crash, ("blocked", NOW + DAY), ("detoured", NOW + 2 * DAY), ("detoured", NOW + 3 * DAY))
        detour = {"devkit_bug": 1, "type": "detour", "fingerprint": crash["fingerprint"], "version": "26.11.7",
                  "impact": "detoured", "steps": [{"hint": "from-root", "dev": "progress step"}],
                  "evidence": {"successes": 1, "tools": ["Bash"]}}
        rec = bugs.fold(rec, detour, author_hash="m1", author_assoc="MEMBER", now=NOW + DAY)
        confirm = {"devkit_bug": 1, "type": "confirm", "fingerprint": crash["fingerprint"], "version": "26.11.7",
                   "detour_id": "d1"}
        rec = bugs.fold(rec, confirm, author_hash="m2", author_assoc="MEMBER", now=NOW + 2 * DAY)
        yield "crash", crash, rec, bugs._day(NOW), [detour, confirm]
        guard = guard_report(rid="_check_bash_reading:unreadable", word="sed", version="26.11.14")
        tip = {"devkit_bug": 1, "type": "detour", "fingerprint": guard["fingerprint"], "version": "26.11.14",
               "impact": "detoured", "steps": [{"hint": "use-tool", "tool": "Edit", "word": "sed"}],
               "evidence": {"successes": 1, "tools": ["Edit"]}}
        vote = {"devkit_bug": 1, "type": "vote", "fingerprint": guard["fingerprint"], "version": "26.11.14",
                "impact": "detoured", "window": bugs._day(NOW + DAY)}
        rec = bugs.fold(folded(guard), tip, author_hash="m1", author_assoc="MEMBER", now=NOW)
        rec = bugs.fold(rec, vote, author_hash="h9", author_assoc="NONE", now=NOW + DAY)
        yield "detour", guard, rec, bugs._day(NOW), [tip, vote]

    def test_each_worked_example_parses_and_reads_on_one_screen(self):
        for name, first, rec, created, comments in self.examples():
            with self.subTest(example=name):
                self.assertTrue(rec.get("fingerprint") and rec["kind"] == first["kind"])  # the fold took the report
                body = bugs.render_issue(rec, first, created=created, now=NOW + 3 * DAY)
                record = bugs.render_record(rec)
                self.assertEqual((action._first_report({"body": body}), released_block(body)), (first, first))
                self.assertEqual((bugs.parse_record(record), released_record(record)), (rec, rec))
                for payload in comments:
                    text = bugs.render_comment(payload)
                    self.assertEqual((action._payload(text), released_block(text)), (payload, payload))
                    self.assertNotIn("```", outside_details(text))
                human = outside_details(body)
                self.assertEqual([x for x in ("```", "@", bugs.RECORD_MARK) if x in human], [])
                self.assertLessEqual(len(body.split("\n\n## ")[0].splitlines()), 8)
                self.assertEqual(bugs.human_title(rec), self.TITLES[name])


if __name__ == "__main__":
    unittest.main()
