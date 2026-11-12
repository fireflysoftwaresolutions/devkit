# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev bug`: browse devkit's bug registry from the local cache (an agent sees only the bot's record and generated
# titles), file a report or a vote, record a detour only with evidence from the session ledger, rank bugs for a
# hotfix, review and send held items, confirm a detour, and give consent with its exact text. `dev status` names the
# items that wait for review.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_bugs_cli
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import bugs, cli, core, identity, progress, work  # noqa: E402

DENY_WORDS = bugs.deny_words  # the real one: BugCLI replaces it with a fixed list
from test_bugs_sync import FakeClient, _bot, _issue, _record  # noqa: E402

NOW = time.time()
FP_A, FP_B, FP_C = "aaaaaaaaaaa1", "bbbbbbbbbbb2", "ccccccccccc3"
VERIFIED = {"id": "d1", "steps": [{"hint": "from-root", "dev": "progress step"}], "author_assoc": "MEMBER",
            "author_hash": "h1", "evidence": {"successes": 1}, "failures": [], "since": "", "until": "",
            "confirmations": [{"author_assoc": "OWNER", "author_hash": "h2"}]}
PROPOSED = {"id": "d2", "steps": [{"text": "move the folder named zebra first"}], "author_assoc": "NONE",
            "author_hash": "h3", "evidence": {"successes": 1}, "confirmations": [], "failures": [], "since": "",
            "until": ""}
CONSENT = ("Reports, votes and workarounds are posted publicly on github.com/fireflysoftwaresolutions/devkit under "
           "your GitHub account `ana`. They are public and permanent: deleting a comment does not remove it from "
           "notification e-mails or public archives. devkit's workflow writes and updates the text of issues you open. "
           "Only devkit facts are sent, never anything about your projects.")


def voters(*impacts):
    return {f"v{i}": {"impact": imp, "ts": NOW} for i, imp in enumerate(impacts)}


class BugCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "handoff").mkdir()
        self.client = FakeClient(login="ana")
        for p in (mock.patch.object(bugs, "BUGS", self.root / "bugs"),
                  mock.patch.object(bugs, "CRASHES", self.root / "crashes"),
                  mock.patch.object(bugs, "install_channel", lambda: "release"),
                  mock.patch.object(bugs, "deny_words", lambda: frozenset({"acme"})),
                  mock.patch.object(bugs, "default_client", lambda: self.client),
                  mock.patch.object(work, "HANDOFF", self.root / "handoff"),
                  mock.patch.object(work, "ITEMS", self.root / "handoff" / "items"),
                  mock.patch.object(progress, "current_session", lambda *a, **k: ("claude", "s1")),
                  mock.patch.dict(os.environ, {"CLAUDECODE": "1"})):  # an agent's shell unless a test says not
            p.start()
            self.addCleanup(p.stop)

    def dev(self, *argv, person=False):
        out = io.StringIO()
        with mock.patch.object(core, "set_by_developer", lambda *a, **k: person), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = cli.main(["bug", *argv])
        return rc, out.getvalue()

    def cache(self, *recs):
        issues, comments = [], {}
        for number, rec, state in recs:
            issues.append(_issue(number, state=state))
            comments[number] = [_bot(bugs.render_record(rec))]
        self.assertTrue(bugs.fetch(FakeClient(issues, comments), now=NOW)["ok"])

    def outbox(self):
        box = bugs.BUGS / "outbox"
        return [json.loads(f.read_text()) for f in sorted(box.glob("*.json"), key=lambda f: int(f.stem))] \
            if box.is_dir() else []

    def crash(self, fp=FP_A, command="progress step", **kw):
        return bugs.record_occurrence("crash", fp, command=command, exc_class="KeyError", frames=("work.actor_str",),
                                      session="s1", **kw)


class BrowseTests(BugCLI):
    def test_ls_orders_by_score(self):
        self.cache((41, _record(fp=FP_A, voters=voters("blocked")), "open"),
                   (42, _record(fp=FP_B, voters=voters("blocked", "blocked", "blocked")), "open"),
                   (43, _record(fp=FP_C, voters=voters("minor")), "open"))
        for argv in ((), ("ls",)):
            rc, out = self.dev(*argv)
            self.assertEqual(rc, 0, out)
            self.assertEqual(re.findall(r"#(\d+)", out), ["42", "41", "43"])
        rc, out = self.dev("ls", "--kind", "guard")
        self.assertEqual(re.findall(r"#(\d+)", out), [])

    def test_ls_local_lists_this_macs_failures_without_a_cache(self):
        self.crash()
        self.crash()
        rc, out = self.dev("ls", "--local")
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, rf"{FP_A}\s.*crash.*dev progress step.*2 times")
        self.assertIn("not in the registry", out)

    def test_show_in_an_agent_session_prints_no_proposed_free_text(self):
        self.cache((42, _record(fp=FP_A, detours=[VERIFIED, PROPOSED]), "open"))
        rc, out = self.dev("show", "42")
        self.assertEqual(rc, 0, out)
        self.assertIn("Run `dev progress step` from the repository root.", out)
        self.assertIn("1 proposed detour", out)
        self.assertNotIn("zebra", out)
        rc, out = self.dev("show", FP_A, person=True)
        self.assertIn("move the folder named zebra first", out)  # a person in a terminal sees everything

    def test_search_finds_by_title_and_fingerprint(self):
        self.cache((41, _record(fp=FP_A), "open"),
                   (42, _record(fp=FP_B, kind="guard", title="guard refusal _check_sed on sed"), "open"))
        self.assertEqual(re.findall(r"#(\d+)", self.dev("search", "check_sed")[1]), ["42"])
        self.assertEqual(re.findall(r"#(\d+)", self.dev("search", FP_A[:8])[1]), ["41"])


class ReportTests(BugCLI):
    def test_report_security_queues_a_held_security_item(self):
        self.crash()
        rc, out = self.dev("report", FP_A, "--security")
        self.assertEqual(rc, 0, out)
        item = self.outbox()[-1]
        self.assertEqual((item["payload"]["type"], item["held"], item["security"]), ("report", True, True))
        self.assertEqual(bugs.validate_payload(item["payload"]), [])
        self.assertIn("private vulnerability reporting", out)

    def test_report_of_a_plain_crash_goes_out_with_the_next_sync(self):
        self.crash()
        rc, out = self.dev("report", FP_A)
        item = self.outbox()[-1]
        self.assertEqual((item["payload"]["type"], item["payload"]["command"], item["held"], item["security"]),
                         ("report", "progress step", False, False))
        self.assertIn("grant bug-share on", out)  # no consent yet: the line to give the developer

    def test_report_of_a_known_bug_votes_blocked(self):
        self.cache((42, _record(fp=FP_A), "open"))
        self.crash()
        self.dev("report", FP_A)
        p = self.outbox()[-1]["payload"]
        self.assertEqual((p["type"], p["impact"]), ("vote", "blocked"))

    def test_report_guard_without_an_id_takes_the_sessions_last_refusal(self):
        bugs.note_refusal("s1", "check_git:force-push", "git")
        bugs.note_refusal("s1", "check_rm:git-dir", "rm")
        rc, out = self.dev("report", "--guard")
        self.assertEqual(rc, 0, out)
        p = self.outbox()[-1]["payload"]
        self.assertEqual((p["kind"], p["refusal_id"], p["word"], p["fingerprint"]),
                         ("guard", "check_rm:git-dir", "rm", bugs.guard_fingerprint("check_rm:git-dir", "rm")))

    def test_report_guard_takes_only_a_refusal_this_session_recorded(self):
        # the guard notes each refusal it makes in the session ledger; another session's, or one never made, is no
        # evidence, and is neither recorded nor queued
        bugs.note_refusal("s1", "check_git:force-push", "git")
        bugs.note_refusal("s2", "check_rm:git-dir", "rm")
        for rid in ("check_rm:git-dir", "check_git:reset-hard"):
            with self.subTest(rid=rid):
                rc, out = self.dev("report", "--guard", rid)
                self.assertEqual(rc, 1, out)
                self.assertIn("this session has no such refusal", out)
        rc, out = self.dev("report", "--guard", "check_git:force-push")
        self.assertEqual(rc, 0, out)
        self.assertEqual([it["payload"]["refusal_id"] for it in self.outbox()], ["check_git:force-push"])
        self.assertEqual([o["refusal_id"] for o in bugs.occurrences()], ["check_git:force-push"])

    def test_report_guard_refuses_text_that_is_no_refusal_of_the_guard(self):
        # an agent's own text as the id would become a public issue title under the developer's account, even when
        # a line written by hand into the ledger names it
        text = "sk_live_" + "AbCdEf0123456789" + ":ghp-SECRETVALUE42"  # built here so no literal looks like a key
        for ledger in (False, True):
            if ledger:
                bugs.note_refusal("s1", text, "")
            for rid in (text, ""):  # named, and as the session's last refusal
                with self.subTest(ledger=ledger, rid=rid):
                    with self.assertRaises(ValueError) as cm:
                        bugs.file_report(refusal_id=rid, session="s1", agent="claude")
                    self.assertNotIn("AbCdEf0123456789", str(cm.exception))
        self.assertEqual((self.outbox(), bugs.occurrences()), ([], []))

    def test_report_needs_something_to_report(self):
        rc, out = self.dev("report")
        self.assertEqual(rc, 1)
        self.assertIn("--title", out)
        rc, out = self.dev("report", "dddddddddddd")
        self.assertEqual(rc, 1)
        self.assertIn("dev bug ls --local", out)
        self.assertEqual(self.outbox(), [])


class MachineNameTests(BugCLI):
    # The machine id and the hostnames this Mac carried before name it as surely as its hostname now, but this Mac's
    # private words left them out, so a detour's free text could carry them into the public registry
    def setUp(self):
        super().setUp()
        self.name = "Mac-158.lan"
        for p in (mock.patch.object(bugs, "deny_words", DENY_WORDS),
                  mock.patch.object(bugs, "_git_identity", lambda: ("", "")),
                  mock.patch.object(bugs, "_host_names", lambda: [self.name]),
                  mock.patch.object(core, "STATE", self.root / "state"),
                  mock.patch("socket.gethostname", side_effect=lambda: self.name)):
            p.start()
            self.addCleanup(p.stop)
        identity.machine()  # devkit ran here as mac-158 ...
        self.name = "andress-mbp-2"  # ... and the network renamed the Mac
        bugs.note_refusal("s1", "_check_sed", "sed")
        bugs.ledger_append("s1", "Edit", True, "", [])

    def detour(self, text):
        return self.dev("detour", bugs.guard_fingerprint("_check_sed", "sed"), "--hint", "use-tool tool=Edit word=sed",
                        "--text", text)

    def test_a_detour_never_carries_the_machine_id_or_a_former_hostname(self):
        for text in (f"then rerun the tests on {identity.machine().id}", "then rerun the tests on mac-158"):
            with self.subTest(text=text):
                rc, out = self.detour(text)
                self.assertEqual(rc, 1, out)
                self.assertIn("names something of this Mac", out)
        rc, out = self.detour("then rerun the tests")
        self.assertEqual(rc, 0, out)


class DetourTests(BugCLI):
    def test_detour_without_evidence_refuses_with_the_reason(self):
        self.crash()
        rc, out = self.dev("detour", FP_A, "--hint", "from-root dev=progress step")
        self.assertEqual(rc, 1)
        self.assertIn("no later successful `dev progress step`", out)
        self.assertEqual(self.outbox(), [])

    def test_detour_with_evidence_queues_a_detour_payload(self):
        self.crash()
        bugs.ledger_append("s1", "Bash", True, "progress step", [])
        rc, out = self.dev("detour", FP_A, "--hint", "from-root dev=progress step")
        self.assertEqual(rc, 0, out)
        item = self.outbox()[-1]
        p = item["payload"]
        self.assertEqual((p["type"], p["steps"], p["evidence"]["successes"], item["held"]),
                         ("detour", [{"hint": "from-root", "dev": "progress step"}], 1, False))
        self.assertEqual(bugs.validate_payload(p), [])

    def test_guard_detour_needs_the_allowed_call_it_names(self):
        bugs.note_refusal("s1", "_check_sed", "sed")
        fp = bugs.guard_fingerprint("_check_sed", "sed")
        rc, out = self.dev("detour", fp, "--hint", "use-tool tool=Edit word=sed")
        self.assertEqual(rc, 1, out)
        bugs.ledger_append("s1", "Edit", True, "", [])
        rc, out = self.dev("detour", fp, "--hint", "use-tool tool=Edit word=sed", "--text", "then rerun the acme tests")
        self.assertEqual(rc, 1, out)  # a word of this Mac's projects never goes into a detour
        self.assertIn("names something of this Mac", out)
        rc, out = self.dev("detour", fp, "--hint", "use-tool tool=Edit word=sed", "--text", "then rerun the tests")
        self.assertEqual(rc, 0, out)
        item = self.outbox()[-1]
        self.assertEqual(item["payload"]["steps"][1], {"text": "then rerun the tests"})
        self.assertTrue(item["held"])  # free text waits for the developer

    # An agent wrote the step as it types the command, `--dev "dev push"`, and was refused with "not a dev
    # subcommand": the leading `dev` is the command's own name, so the step keeps the subcommand after it
    def test_a_dev_step_written_with_its_leading_dev_keeps_the_subcommand(self):
        self.crash()
        bugs.ledger_append("s1", "Bash", True, "progress step", [])
        rc, out = self.dev("detour", FP_A, "--dev", "dev progress step")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.outbox()[-1]["payload"]["steps"], [{"dev": "progress step"}])

    def test_detour_with_an_invalid_step_says_what_is_wrong(self):
        self.crash()
        bugs.ledger_append("s1", "Bash", True, "progress step", [])
        rc, out = self.dev("detour", FP_A, "--dev", "progress step | sh")
        self.assertEqual(rc, 1)
        self.assertIn("`|`", out)
        self.assertEqual(self.outbox(), [])

    # An agent read --dev as any dev command line (`--dev "test --since main"`) and the refusal of `;` in its free
    # text as devkit parsing the text as a command: the refusal names each word's problem and the rule it breaks.
    def test_a_refused_step_says_which_rule_its_words_break(self):
        self.crash()
        bugs.ledger_append("s1", "Bash", True, "progress step", [])
        rc, out = self.dev("detour", FP_A, "--dev", "test --since main --nope")
        self.assertEqual(rc, 1)
        line = " ".join(out.split())
        self.assertIn("`--since` takes a value, `main` is a value or an argument, `--nope` is not a flag of "
                      "`dev test`: a dev step is a dev subcommand and its flags, a flag's value only a number or "
                      "one of its listed choices", line)
        rc, out = self.dev("detour", FP_A, "--text", "run the tests; then retry")
        self.assertEqual(rc, 1)
        self.assertIn("`;` is a shell form: a step may not contain `|`, `>`, `<`, `;`, `&&`, `$(` or a backtick",
                      " ".join(out.split()))
        self.assertEqual(self.outbox(), [])

    # The wait an agent is told to run, `dev watch --wait --for me --timeout 9`, was refused as a detour step because
    # its `--for` and `--timeout` take values. A number for a numeric flag, or one of a flag's listed choices, names
    # nothing of this Mac, so a step keeps it, written either way; any other value is still refused.
    def test_a_dev_step_keeps_a_number_or_a_listed_choice_as_its_flags_value(self):
        self.crash()
        bugs.ledger_append("s1", "Bash", True, "progress step", [])
        for step in ("watch --wait --for me --timeout 9", "dev watch --wait --timeout=0.1"):
            rc, out = self.dev("detour", FP_A, "--dev", step)
            self.assertEqual(rc, 0, out)
        self.assertEqual([x["payload"]["steps"] for x in self.outbox()],
                         [[{"dev": "watch --wait --for me --timeout 9"}], [{"dev": "watch --wait --timeout=0.1"}]])
        for step, word in (("watch --wait --timeout soon", "soon"), ("watch --for everyone", "everyone"),
                           ("watch --timeout 1e9", "1e9"), ("watch --timeout=-5", "--timeout=-5")):
            rc, out = self.dev("detour", FP_A, "--dev", step)
            self.assertEqual(rc, 1, step)
            self.assertIn(f"`{word}`", out)
        self.assertEqual(len(self.outbox()), 2)

    def test_detour_help_says_what_a_dev_step_and_free_text_may_hold(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["bug", "detour", "--help"])
        # each flag's help follows its last mention (the first is the usage line)
        dev_help, _, text_help = " ".join(out.getvalue().split()).rpartition("--text 'TEXT'")
        self.assertIn("a flag's value only a number or one of its listed choices",
                      dev_help.rpartition("--dev 'COMMAND'")[2])
        self.assertIn("none of the shell forms |, >, <, ;, &&, $( or a backtick", text_help)


class RankTests(BugCLI):
    def test_top_gives_reasons_and_skips_closed_bugs(self):
        self.cache((41, _record(fp=FP_A, voters=voters(*["blocked"] * 9, *["detoured"] * 5)), "open"),
                   (42, _record(fp=FP_B, voters=voters("detoured"), detours=[VERIFIED]), "open"),
                   (43, _record(fp=FP_C, voters=voters(*["blocked"] * 20)), "closed"))
        found = bugs.top(now=NOW)
        self.assertEqual([(b["number"], b["reason"]) for b in found],
                         [(41, "14 people, 9 blocked, no detour"), (42, "1 person, verified detour")])

    def test_top_task_adds_work_items_with_the_issue_link_once(self):
        self.cache((41, _record(fp=FP_A, voters=voters("blocked")), "open"))
        for _ in range(2):
            rc, out = self.dev("top", "--task", person=True)
            self.assertEqual(rc, 0, out)
        items = work.all_items(core.DEVKIT_REPO)
        url = f"https://github.com/{core.DEVKIT_REPO}/issues/41"
        self.assertEqual([(it["meta"]["type"], it["meta"]["links"]) for it in items], [("bug", [url])])


class ReviewTests(BugCLI):
    def held(self):
        self.crash()
        self.dev("report", FP_A, "--title", "progress step crashes in a fresh clone")
        return self.outbox()[-1]

    def test_pending_prints_exactly_what_would_be_sent(self):
        # the title and body of the issue a new report opens, and the comment it posts when the registry has the bug
        # by then: each string the sender passes to GitHub is in the review, whole
        item = self.held()
        rc, out = self.dev("pending")
        self.assertEqual(rc, 0, out)
        bugs.set_consent("on", "ana")
        self.assertEqual(self.dev("send", item["id"], person=True)[0], 0)
        (title, body), = self.client.created
        self.assertIn(f"\n  Title: {title}\n{body}\n", out)
        self.assertIn(f"\n{bugs.render_comment(item['payload'])}\n", out)

    def test_pending_prints_the_comment_a_known_bug_gets(self):
        self.cache((42, _record(fp=FP_A), "open"))
        item = bugs.queue({"payload": {"devkit_bug": 1, "type": "detour", "fingerprint": FP_A, "version": "26.11.7",
                                       "impact": "detoured", "steps": [{"text": "Run it again from a new shell"}],
                                       "evidence": {"successes": 1}}})
        rc, out = self.dev("pending")
        self.assertEqual(rc, 0, out)
        bugs.set_consent("on", "ana")
        self.dev("send", item, person=True)
        (number, body), = self.client.posted
        self.assertEqual(number, 42)
        self.assertIn(f"\n  As a comment on issue #42:\n{body}\n", out)
        self.assertNotIn("Title:", out)

    def test_send_approves_and_sends_the_exact_payload(self):
        item = self.held()
        bugs.set_consent("on", "ana")
        rc, out = self.dev("send", item["id"], person=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual([bugs.parse_payload(body)["title"] for _, body in self.client.created],
                         ["progress step crashes in a fresh clone"])
        self.assertEqual(self.outbox(), [])

    def test_drop_removes_a_held_item(self):
        item = self.held()
        self.assertEqual(self.dev("drop", item["id"], person=True)[0], 0)
        self.assertEqual(self.outbox(), [])
        self.assertEqual(self.dev("drop", item["id"], person=True)[0], 1)

    def test_confirm_queues_a_confirm_for_a_cached_detour(self):
        self.cache((42, _record(fp=FP_A, detours=[VERIFIED]), "open"))
        self.assertEqual(self.dev("confirm", "42", "d1", person=True)[0], 0)
        p = self.outbox()[-1]["payload"]
        self.assertEqual((p["type"], p["fingerprint"], p["detour_id"]), ("confirm", FP_A, "d1"))
        rc, out = self.dev("confirm", "42", "d9", person=True)
        self.assertEqual(rc, 1)
        self.assertIn("d9", out)

    def test_share_prints_the_consent_text_with_the_active_login(self):
        rc, out = self.dev("share", "on", person=True)
        self.assertEqual(rc, 0, out)
        self.assertIn(CONSENT, " ".join(out.split()))
        self.assertEqual((bugs.consent()["login"], bugs.consent()["share"]), ("ana", "on"))
        self.dev("share", "off", person=True)
        self.assertEqual(bugs.consent()["share"], "off")

    def test_sync_says_what_failed_and_keeps_the_cache(self):
        self.cache((42, _record(fp=FP_A), "open"))
        self.client.fail = True
        rc, out = self.dev("sync")
        self.assertEqual(rc, 1)
        self.assertIn("network is unreachable", out)
        self.assertIn(FP_A, bugs.load_cache()["bugs"])

    def test_status_names_held_items_and_todays_hits(self):
        self.assertEqual([n for n in cli.devkit_needs() if "bug" in n["text"]], [])
        self.held()
        need = [n for n in cli.devkit_needs() if "bug" in n["text"]]
        self.assertEqual([(n["text"], n["command"]) for n in need],
                         [("devkit's bug registry: 1 item waits for your review · 1 devkit bug hit this Mac today",
                           "dev bug pending")])


if __name__ == "__main__":
    unittest.main()
