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
from test_bugs_sync import FakeClient, _bot, _issue, _noon, _record  # noqa: E402

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
           "Text you or an agent write in a report or a workaround is cleaned of this Mac's private words, paths, "
           "URLs, e-mail addresses and tokens, then sent automatically, without your review: the cleaning removes what "
           "devkit knows to be private, not every detail a sentence can carry. A report about devkit's security goes "
           "only to private vulnerability reporting on devkit's repository. Apart from that text, only devkit facts "
           "are sent, never anything about your projects.")


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
        self.assertIn("Automatic safe reports do not require consent", out)
        self.assertNotIn("grant bug-share", out)

    def test_a_report_in_an_agents_words_goes_out_without_review_once_the_developer_shares(self):
        bugs.set_consent("on", "ana")
        rc, out = self.dev("report", "--title", "progress step crashes in a fresh clone")
        self.assertEqual(rc, 0, out)
        item = self.outbox()[-1]
        self.assertEqual((item["payload"]["title"], item["held"]), ("progress step crashes in a fresh clone", False))
        self.assertIn("The report goes out with the next `dev bug sync`", out)
        self.assertNotIn("review", out)
        self.assertEqual(bugs.pending(), [])

    def test_a_security_report_goes_to_private_reporting_without_review_once_the_developer_shares(self):
        bugs.set_consent("on", "ana")
        self.crash()
        rc, out = self.dev("report", FP_A, "--security")
        self.assertEqual(rc, 0, out)
        item = self.outbox()[-1]
        self.assertEqual((item["held"], item["security"]), (False, True))
        self.assertIn("goes only to private vulnerability reporting, never to public issues", out)
        self.assertIn("If private reporting refuses it, it waits for the developer", out)

    def test_report_of_a_known_bug_votes_blocked(self):
        self.cache((42, _record(fp=FP_A), "open"))
        self.crash()
        self.dev("report", FP_A)
        p = self.outbox()[-1]["payload"]
        self.assertEqual((p["type"], p["impact"]), ("vote", "blocked"))

    def test_report_has_no_option_for_a_refusal(self):
        # devkit refuses no tool call any more, so there is no refusal of its own to report
        with self.assertRaises(SystemExit) as cm:
            self.dev("report", "--guard")
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(self.outbox(), [])

    def test_reports_of_different_bugs_with_one_title_all_stay_queued(self):
        # two crashes and a command that misbehaves, named alike: three bugs, so no report drops another
        self.crash()
        self.crash(fp=FP_B, command="crew ask")
        title = "progress stops in a fresh clone"
        for argv in ((FP_A,), (FP_B,), ()):
            rc, out = self.dev("report", *argv, "--title", title)
            self.assertEqual(rc, 0, out)
        self.assertEqual([(it["payload"]["kind"], it["payload"]["command"], it["payload"]["title"])
                          for it in self.outbox()],
                         [("crash", "progress step", title), ("crash", "crew ask", title), ("command", "", title)])

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
        bugs.record_occurrence("crash", FP_A, command="progress step", exc_class="KeyError",
                               frames=("work.actor_str",), session="s1")
        bugs.ledger_append("s1", "Bash", True, "progress step", [])

    def detour(self, text):
        return self.dev("detour", FP_A, "--hint", "from-root dev=progress step", "--text", text)

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

    def test_a_guard_detour_has_no_evidence_rule_any_more(self):
        # an older guard bug cannot be detoured: no refusal happens that a later call could get past
        fp = bugs.fingerprint("guard", refusal_id="_check_sed", word="sed")
        bugs.record_occurrence("guard", fp, refusal_id="_check_sed", word="sed", session="s1")
        bugs.ledger_append("s1", "Edit", True, "", [])
        rc, out = self.dev("detour", fp, "--hint", "use-tool tool=Edit word=sed")
        self.assertEqual(rc, 1, out)
        self.assertIn("no evidence rule for a guard", out)
        self.assertEqual(self.outbox(), [])

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
        self.assertIn("1 item waits for your review; `dev bug send <id>` sends one, exactly as printed here:", out)
        bugs.set_consent("on", "ana")
        self.assertEqual(self.dev("send", item["id"], person=True)[0], 0)
        self.assertEqual(len(self.client.created), 2)  # automatic metadata and the explicitly approved contribution
        (title, body), = [(title, body) for title, body in self.client.created
                         if bugs.parse_payload(body).get("title")]
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
        self.assertEqual([bugs.parse_payload(body)["title"] for _, body in self.client.created
                          if bugs.parse_payload(body).get("title")],
                         ["progress step crashes in a fresh clone"])
        self.assertEqual(len(self.client.created), 2)
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

    def test_a_report_with_a_title_already_queued_says_it_adds_nothing(self):
        self.assertEqual(self.dev("report", "--title", "progress step crashes in a fresh clone")[0], 0)
        rc, out = self.dev("report", "--title", "progress step crashes in a fresh clone")
        self.assertEqual(rc, 0, out)
        self.assertIn("report 1 with this title is already queued: this filing adds nothing", out)
        self.assertEqual(len(self.outbox()), 1)

    def test_a_report_under_an_older_consent_says_it_waits_for_the_new_one_and_how_to_give_it(self):
        # consent recorded before the text said that free text goes without review: the report waits, and the
        # filing, `dev bug pending` and `dev status` each name the command that gives the new consent
        bugs._write_json(bugs.BUGS / "consent.json", {"login": "ana", "share": "on", "ts": NOW})
        rc, out = self.dev("report", "--title", "progress step crashes in a fresh clone")
        self.assertEqual(rc, 0, out)
        self.assertIn("Waits for the developer's consent to the new text: `dev bug share on`", out)
        waits = "1 item waits for your consent to the new text: `dev bug share on`"
        rc, out = self.dev("pending")
        self.assertIn(waits, out)
        need = [n for n in cli.devkit_needs() if "bug" in n["text"]]
        self.assertIn(waits, need[0]["text"])
        self.assertNotIn("for your review", need[0]["text"])
        self.assertEqual(need[0]["command"], "dev bug pending")
        rc, out = self.dev("share", "on", person=True)
        self.assertIn("1 item that waited for this consent goes out with the next `dev bug sync`", out)
        self.assertEqual(bugs.pending(), [])

    def test_share_off_and_the_registry_listing_say_what_still_leaves_this_mac(self):
        # with sharing off, a security report in anyone's words waits like any other; only an automatic security
        # report, which holds no free text, still goes to private vulnerability reporting
        rc, out = self.dev("share", "off", person=True)
        self.assertEqual(rc, 0, out)
        self.cache((42, _record(fp=FP_A), "open"))
        listing = self.dev("ls")[1]
        for text in (out, listing):
            text = " ".join(text.split())
            self.assertIn("nothing is posted publicly; reports and detours in your or an agent's own words, security "
                          "ones included, wait on this Mac; automatic security reports, which hold no free text, "
                          "still go to private vulnerability reporting", text)
            self.assertNotIn("private security reports still go", text)

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
        self.assertEqual(len(need), 1)
        self.assertIn("1 automatic reports pending", need[0]["text"])
        self.assertIn("1 item waits for your review", need[0]["text"])
        self.assertIn("1 devkit bug hit this Mac today", need[0]["text"])
        self.assertEqual(need[0]["command"], "dev bug pending")

    def test_sync_pending_and_status_say_how_many_reports_wait_for_the_limit(self):
        # the eleventh report in 24 hours in an agent's own words waits for the next send the limit allows, and each
        # listing says so: the developer never finds it missing without a reason
        bugs.set_consent("on", "ana")
        later = "1 item in an agent's own words waits for the next send the limit allows: at most 10 go out in 24 hours"
        with mock.patch.object(bugs.time, "time", return_value=_noon(NOW)):
            for n in range(11):
                self.assertEqual(self.dev("report", "--title", f"progress step crashes in fresh clone {n}")[0], 0)
            rc, out = self.dev("sync")
            self.assertEqual(rc, 0, out)
            self.assertIn(later, out)
            rc, out = self.dev("pending")
            self.assertEqual(rc, 0, out)
            self.assertIn(later, out)
            self.assertIn("Nothing waits for review.", out)
            need = [n for n in cli.devkit_needs() if "bug" in n["text"]]
        self.assertIn(later, need[0]["text"])
        self.assertEqual(need[0]["command"], "dev bug pending")

    def test_a_report_the_limit_keeps_says_it_goes_out_with_the_next_send_the_limit_allows(self):
        bugs.set_consent("on", "ana")
        with mock.patch.object(bugs.time, "time", return_value=_noon(NOW)):
            for n in range(10):
                self.assertEqual(self.dev("report", "--title", f"progress step crashes in fresh clone {n}")[0], 0)
            self.assertEqual(self.dev("sync")[0], 0)
            rc, out = self.dev("report", "--title", "progress step crashes in a fresh clone")
            self.crash()
            private = self.dev("report", FP_A, "--security")[1]  # a security report counts in the same ten
        self.assertEqual(rc, 0, out)
        self.assertIn("goes out with the next send the limit allows", out)
        self.assertNotIn("next `dev bug sync`", out)
        self.assertIn("never to public issues, with the next send the limit allows", private)


if __name__ == "__main__":
    unittest.main()
