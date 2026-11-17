# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Durable messaging contracts, exercised through the public crew adapter."""
import unittest
import json
import hashlib
import sys
import time
import concurrent.futures
import argparse
from unittest.mock import patch

from tests.test_progress import TempState, launch
from devkit import progress
from devkit import state
from tests.test_peers import PeerCase, CLAUDE_A, CLAUDE_B, sender
from devkit import peers


class CrewIntegrityTests(unittest.TestCase):
    def test_native_capture_keeps_qualified_question_reference_scope(self):
        with TempState():
            progress.start("old native questions", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            old = progress.crew_ask("Old question?", from_agent="a1")
            progress.start("current native questions", plan="verify=5m", replace=True, why="new task")
            launch("a1", "devkit:neo", "Neo: verify")
            current = progress.crew_ask("Current question?", from_agent="a1")
            self.assertEqual(old["id"], current["id"])
            data = {"session_id": "s1", "tool_use_id": "qualified-native",
                    "tool_input": {"to": "a1"}, "tool_response": {"success": True, "recipient": "a1"}}
            for ref in (old["ref"], current["id"] + "@another-session", current["id"] + "@r-invalid"):
                with self.subTest(ref=ref):
                    data["tool_input"]["message"] = ref + ": answer to an unrelated question"
                    self.assertEqual([], progress.answered_by_sendmessage("claude", data))
                    self.assertFalse(progress.load_run("claude", "s1")["messages"][0].get("answered"))
            data["tool_input"]["message"] = current["ref"] + ": current answer"
            self.assertEqual([current["id"]], progress.answered_by_sendmessage("claude", data))

    def test_unavailable_native_body_records_recoverable_capture_notice(self):
        with TempState():
            progress.start("native unavailable body", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            question = progress.crew_ask("Question?", from_agent="a1")
            for name, body in (("empty", ""), ("missing", None)):
                with self.subTest(body=name):
                    data = {"session_id": "s1", "tool_use_id": "native-" + name,
                            "tool_input": {"to": "a1"}, "tool_response": {"success": True, "recipient": "a1"}}
                    if body is not None:
                        data["tool_input"]["message"] = body
                    self.assertEqual([], progress.answered_by_sendmessage("claude", data))
                    messages = progress.load_run("claude", "s1")["messages"]
                    self.assertFalse(messages[0].get("answered"))
                    notices = [m for m in messages if m["kind"] == "notice" and "native-" + name in m["text"]]
                    self.assertEqual(1, len(notices))
                    self.assertIn("capture_incomplete", notices[0]["text"])
                    self.assertEqual(question["id"], messages[0]["id"])
                    progress.answered_by_sendmessage("claude", data)
                    self.assertEqual(len(messages), len(progress.load_run("claude", "s1")["messages"]))

    def test_accepted_unicode_record_remains_readable_beyond_ascii_projection_limit(self):
        from devkit import messaging
        with TempState():
            progress.start("Unicode record capacity", plan="verify=5m")
            body = "😀" * (messaging.BODY_MAX // 4)
            seed = progress.crew_ask(body)
            run = progress.load_run("claude", "s1")
            run["messages"] = [dict(seed, id="M" + str(i), seq=i,
                                    ref="M" + str(i) + "@r-" + seed["run_id"]) for i in range(1, 91)]
            self.assertLess(len(messaging.encoded(run)), messaging.STORE_MAX)
            self.assertGreater(len(json.dumps(run, indent=1).encode()), messaging.STORE_MAX)
            progress._save(run, touch_session=False)
            loaded = progress.load_run("claude", "s1")
            self.assertEqual(90, len(loaded["messages"]))
            self.assertTrue(all(m["text"] == body for m in loaded["messages"]))
            self.assertLessEqual(progress.run_path("claude", "s1").stat().st_size, messaging.STORE_MAX)

    def test_lead_forwarding_preserves_original_native_recipient_authority(self):
        with TempState():
            progress.start("forward replacement work", plan="verify=5m")
            launch("old-child", "devkit:neo", "Neo: original work")
            launch("new-child", "devkit:neo", "Neo: replacement work")
            original = progress.crew_ask("Review the complete context", to="old-child")
            before = progress.load_run("claude", "s1")["messages"][0]
            context = progress.crew_read(original["ref"])["text"]
            forwarded = progress.crew_ask("Forwarded " + original["ref"] + "; replacement work: " + context,
                                         to="new-child")
            progress.crew_ack(forwarded["ref"], digest=forwarded["body_sha256"],
                              generation=forwarded["generation"], from_agent="new-child")
            with self.assertRaisesRegex(ValueError, "not_recipient"):
                progress.crew_ack(original["ref"], digest=original["body_sha256"],
                                  generation=original["generation"], from_agent="new-child")
            with self.assertRaisesRegex(ValueError, "not_recipient"):
                progress.crew_answer(original["ref"], "Unauthorized reply", from_agent="new-child")
            after = progress.load_run("claude", "s1")["messages"][0]
            self.assertEqual(before, after)
            self.assertEqual("main", forwarded["from"])
            self.assertEqual("new-child", forwarded["to"])
            self.assertEqual([original["id"]], [m["id"] for m in progress.crew_inbox(from_agent="old-child")])

    def test_preview_reuse_keeps_new_body_and_receipt_state_authoritative(self):
        from devkit import watch
        with TempState():
            progress.start("preview reuse", plan="verify=5m")
            first = progress.crew_ask("First body")
            original = watch._message_text
            with patch.object(watch, "_message_text", wraps=original) as render:
                events, _ = watch._messages("claude", "s1", "main", False, lambda _: True, 1)
            self.assertEqual(1, render.call_count)
            self.assertIn("First body", events[0]["line"])
            progress.crew_ack(first["ref"], digest=first["body_sha256"], generation=first["generation"])
            second = progress.crew_ask("Changed body")
            events, _ = watch._messages("claude", "s1", "main", False, lambda _: True, 1)
            self.assertEqual([second["id"]], [e["id"] for e in events])
            self.assertIn("Changed body", events[0]["line"])

    def test_bounded_selection_does_not_render_messages_beyond_the_page(self):
        with TempState():
            progress.start("bounded selection", plan="verify=5m")
            sent = [progress.crew_ask("界" * 500 + str(i)) for i in range(4)]
            visited = []
            def select(run, message):
                visited.append(message["id"])
                return True
            page = progress._take_messages("claude", "s1", "main", mark=False, limit=1, select=select)
            self.assertEqual([sent[0]["id"]], [m["id"] for m in page])
            self.assertEqual([sent[0]["id"]], visited)
            self.assertEqual(4, len(progress.crew_inbox()))

    def test_hook_output_failure_does_not_mark_notification_history(self):
        from devkit import hooks, watch
        with TempState():
            progress.start("hook output", plan="verify=5m")
            question = progress.crew_ask("Question?")
            with watch.context_notifications():
                text = watch.mail("claude", "s1")
                with patch("builtins.print", side_effect=BrokenPipeError()):
                    with self.assertRaises(BrokenPipeError):
                        hooks._context_out("PostToolUse", text)
            pending = progress._take_messages("claude", "s1", "main", mark=False)
            self.assertEqual([question["id"]], [m["id"] for m in pending])
            self.assertFalse(pending[0]["delivered"])
            with watch.context_notifications(), patch("builtins.print"):
                hooks._context_out("PostToolUse", watch.mail("claude", "s1"))
            stored = progress.load_run("claude", "s1")["messages"][0]
            self.assertTrue(stored["delivered"])
            # the preview showed the whole question: its delivery is its reading
            self.assertEqual({"main"}, set(stored["receipts"]))
            self.assertEqual(question["body_sha256"], stored["receipts"]["main"]["digest"])

    def test_decision_full_question_and_answer_are_recoverable(self):
        with TempState():
            progress.start("decision integrity", plan="verify=5m", mode="guided")
            launch("a1", "devkit:neo", "Neo: verify")
            question = "Question " + "界" * 1000 + " QUESTION END"
            decision = progress.crew_decision(question, from_agent="a1")
            answer = "Answer " + "界" * 1000 + " ANSWER END"
            decided = progress.crew_decide(decision["id"], answer)
            self.assertEqual(question, progress.crew_read(decision["id"], from_agent="a1")["text"])
            self.assertEqual(answer, progress.crew_read(decided["answer_ref"], from_agent="a1")["text"])

    def test_pending_target_binds_once_to_exact_native_child(self):
        with TempState():
            progress.start("exact targets", plan="verify=5m")
            question = progress.crew_ask("Pending work", to="Neo", pending_launch=True)
            launch("a1", "devkit:neo", "Neo: verify")
            launch("a2", "devkit:neo", "Neo: other work")
            self.assertEqual([question["id"]], [m["id"] for m in progress.crew_inbox(from_agent="a1")])
            self.assertEqual([], progress.crew_inbox(from_agent="a2"))
            with self.assertRaisesRegex(ValueError, "ambiguous_target"):
                progress.crew_ask("Ambiguous work", to="Neo")

    def test_native_capture_requires_matching_target_and_retains_full_body(self):
        with TempState():
            progress.start("native capture", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            question = progress.crew_ask("Question?", from_agent="a1")
            body = "  M1: " + "界" * 1501 + " END  "
            data = {"session_id": "s1", "tool_use_id": "native-1", "tool_input": {"to": "a1", "message": body},
                    "tool_response": {"success": True, "recipient": "another-child"}}
            self.assertEqual([], progress.answered_by_sendmessage("claude", data))
            self.assertFalse(progress.load_run("claude", "s1")["messages"][0].get("answered"))
            data["tool_response"]["recipient"] = "a1"
            self.assertEqual([question["id"]], progress.answered_by_sendmessage("claude", data))
            self.assertEqual(body, progress.load_run("claude", "s1")["messages"][-1]["text"])

    def test_hook_overflow_remains_immediately_pending(self):
        from devkit import watch
        with TempState():
            progress.start("bounded output", plan="verify=5m")
            for n in range(20):
                progress.crew_ask("界" * 300 + str(n))
            text = watch.mail("claude", "s1")
            self.assertLessEqual(len(text.encode()), 6144)
            pending = progress._take_messages("claude", "s1", "main", mark=False)
            self.assertTrue(pending)
            self.assertTrue(all(not m.get("delivered") for m in pending))

    def test_concurrent_answers_commit_one_linked_body(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            q = progress.crew_ask("Question?")
            def answer(body):
                try:
                    return progress.crew_answer(q["id"], body)["id"]
                except ValueError as error:
                    return str(error).partition(":")[0]
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(answer, ("First", "Second")))
            self.assertEqual(1, outcomes.count("already_answered"))
            messages = progress.load_run("claude", "s1")["messages"]
            self.assertEqual(2, len(messages))
            self.assertEqual(messages[1]["ref"], messages[0]["answered_by"])

    def test_old_reference_is_readable_after_run_replacement(self):
        with TempState():
            progress.start("first", plan="verify=5m")
            old = progress.crew_ask("Original body")
            progress.start("second", plan="verify=5m", replace=True, why="new task")
            archive = progress.RUNS.parent / "messaging-v2" / "archive" / (old["run_id"] + ".json")
            self.assertTrue(archive.is_file())
            new = progress.crew_ask("New body")
            self.assertNotEqual(old["ref"], new["ref"])
            self.assertEqual("Original body", progress.crew_read(old["ref"])["text"])
            with self.assertRaisesRegex(ValueError, "stale_target"):
                progress.crew_answer(old["ref"], "Stale answer")

    def test_missing_recipient_requires_explicit_pending_launch(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            with self.assertRaisesRegex(ValueError, "not_found"):
                progress.crew_ask("Unassigned work", to="Neo")
            self.assertEqual([], progress.load_run("claude", "s1")["messages"])

    def test_notification_and_read_are_not_receipts(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            q = progress.crew_ask("Question?")
            progress._take_messages("claude", "s1", "main")
            page = progress.crew_read(q["ref"])
            self.assertFalse(progress.load_run("claude", "s1")["messages"][0]["receipts"])
            progress.crew_ack(q["ref"], digest=page["digest"], generation=page["generation"])
            msg = progress.load_run("claude", "s1")["messages"][0]
            self.assertEqual(page["digest"], msg["receipts"]["main"]["digest"])
            self.assertFalse(msg.get("answered"))

    def test_unauthorized_read_and_ack_do_not_mutate(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            q = progress.crew_ask("Private lane question")
            before = progress._message_path(q["run_id"]).read_bytes()
            with self.assertRaisesRegex(ValueError, "not_recipient"):
                progress.crew_read(q["ref"], from_agent="other")
            with self.assertRaisesRegex(ValueError, "not_recipient"):
                progress.crew_ack(q["ref"], digest=q["body_sha256"], generation=1, from_agent="other")
            self.assertEqual(before, progress._message_path(q["run_id"]).read_bytes())

    def test_corrupt_canonical_record_is_never_recreated(self):
        with TempState():
            run = progress.start("message integrity", plan="verify=5m")
            path = progress._message_path(run["run_id"])
            path.write_bytes(b"{interrupted")
            with self.assertRaisesRegex(ValueError, "corrupt_state"):
                progress.crew_ask("Do not overwrite")
            self.assertEqual(b"{interrupted", path.read_bytes())

    def test_role_assignment_does_not_capture_retiring_lanes_scope(self):
        with TempState() as fixture:
            progress.start("message integrity", plan="old=5m; new=5m after old")
            progress.assign("old", "Neo", scope="old.py", why="test")
            launch("a1", "devkit:neo", "Neo: old")
            progress.step("old complete", which="old", from_agent="a1", check="✓ checked")
            run = progress.load_run("claude", "s1")
            run["plan"][1]["scope"] = ["new.py"]
            progress._route_to_lane(run, run["plan"][1], "Neo", [("a1", run["agents"]["a1"])], 200)
            self.assertEqual("main", run["plan"][1]["owner"])
            self.assertEqual(["old.py"], run["agents"]["a1"]["scope"])

    def test_answer_preserves_complete_utf8_body(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            question = progress.crew_ask("Question?")
            body = "  " + "界👩\u200d💻é\r\n" * 400 + "END  "
            answer = progress.crew_answer(question["id"], body)
            self.assertEqual(body, answer["text"])

    def test_distinct_second_answer_conflicts(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            question = progress.crew_ask("Question?")
            progress.crew_answer(question["id"], "First answer")
            with self.assertRaisesRegex(ValueError, "already_answered"):
                progress.crew_answer(question["id"], "Second answer")

    def test_pending_questions_survive_more_than_two_hundred_messages(self):
        run = {"agent": "claude", "messages": []}
        for number in range(201):
            progress._post(run, "main", "main", "Question %s" % number, number + 1)
        self.assertEqual(201, len(run["messages"]))
        self.assertEqual("Question 0", run["messages"][0]["text"])

    def _legacy_run(self, messages):
        # the run file as 26.11.18 wrote it: no run id, no canonical message store, a `delivered` flag per message
        path = progress.run_path("claude", "s1")
        run = json.loads(path.read_text())
        progress._message_path(run.pop("run_id")).unlink()
        run.pop("messaging_schema", None)
        run["messages"] = [dict({"ts": round(time.time()) - 600, "from": "a1", "to": "main", "kind": "question",
                                 "from_name": "Neo", "to_name": "Morpheus"}, **m) for m in messages]
        path.write_text(json.dumps(run))

    def test_messages_delivered_before_the_upgrade_are_not_delivered_again(self):
        with TempState():
            progress.start("upgrade", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            self._legacy_run([{"id": "M1", "text": "Old question?", "delivered": True},
                              {"id": "M2", "text": "Old notice", "delivered": True, "from": "devkit",
                               "kind": "notice"},
                              {"id": "M3", "text": "New question?", "delivered": False}])
            first = [m["id"] for m in progress._take_messages("claude", "s1", "main")]
            with patch("time.time", return_value=time.time() + 600):
                later = [m["id"] for m in progress._take_messages("claude", "s1", "main")]
            self.assertEqual(["M3"], first)
            self.assertFalse({"M1", "M2"} & set(later), later)

    def test_a_message_delivered_whole_does_not_come_back_but_a_longer_one_does(self):
        # a body the delivered preview shows whole, and devkit's or CI's own notice, count as read on delivery;
        # a body longer than the preview comes back until its recipient reads and acknowledges it
        with TempState():
            progress.start("delivery", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            progress.crew_ask("Is the parser fixed?", from_agent="a1")
            longer = progress.crew_ask("Details: " + "x" * 400, from_agent="a1")
            progress._ask_into("claude", "s1", "CI passed on main: " + "y" * 400, "main", "ci", time.time())
            self.assertIn("Is the parser fixed?", progress.messages_context("claude", "s1", "main"))
            with patch("time.time", return_value=time.time() + 120):
                again = [m["id"] for m in progress._take_messages("claude", "s1", "main", mark=False)]
            self.assertEqual([longer["id"]], again)

    def test_a_finished_runs_unread_messages_stop_coming_back_and_stay_readable(self):
        # after the 26.11.20 run ended, 25 messages to the tech lead kept coming back until each was read and acked
        with TempState():
            progress.start("delivery", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            report = progress.crew_ask("Report: " + "x" * 400, from_agent="a1")
            progress._take_messages("claude", "s1", "main")  # its preview was shown once, never read whole
            progress.step("all done", kind="done", force=True)
            with patch("time.time", return_value=time.time() + 120):
                self.assertEqual(progress._take_messages("claude", "s1", "main", mark=False), [])
            self.assertEqual(progress.crew_inbox(), [])
            self.assertIn("Report: ", progress.crew_read(report["ref"])["text"])

    def test_the_same_request_key_posts_one_message_and_notifies_once(self):
        with TempState():
            progress.start("delivery", plan="verify=5m")
            with patch.object(progress, "_notify_message") as notify:
                for _ in range(3):
                    progress._ask_into("claude", "s1", "CI skipped on fix/x", "main", "ci", time.time(),
                                       request_id="ci:same")
            self.assertEqual(([m["text"] for m in progress.load_run("claude", "s1")["messages"]], notify.call_count),
                             (["CI skipped on fix/x"], 1))

    def test_a_reply_to_an_asker_that_ended_stays_with_it_and_tells_the_tech_lead(self):
        # an answer, a decision's result and a withdrawal keep their recipient; nobody reads an ended lane, so the
        # tech lead hears of each and forwards it or launches the member again
        with TempState():
            progress.start("ended asker", plan="verify=5m", mode="guided")
            launch("a1", "devkit:neo", "Neo: verify")
            question = progress.crew_ask("Ship it?", from_agent="a1")
            decided = progress.crew_decision("Which database?", options=["Postgres", "MySQL"], from_agent="a1")
            withdrawn = progress.crew_decision("Which cache?", options=["Redis", "None"], from_agent="a1")
            with progress._tx("claude", "s1"):
                run = progress.load_run("claude", "s1")
                run["agents"]["a1"]["state"] = "done"
                progress._save(run, touch_session=False)
            answer = progress.crew_answer(question["id"], "Yes, ship it")
            progress.crew_decide(decided["id"], "Postgres")
            progress.crew_withdraw(withdrawn["id"], "not needed any more")
            notices = [m["text"] for m in progress.load_run("claude", "s1")["messages"]
                       if m.get("to") == "main" and m.get("from") == "devkit"]
            self.assertEqual("a1", answer["to"])
            for ref in (answer["id"], decided["id"], withdrawn["id"]):
                self.assertEqual(1, sum(ref + " " in t or ref + ")" in t for t in notices), (ref, notices))

    def test_two_sends_in_one_shell_call_are_two_messages_and_a_repeat_is_one(self):
        # one Bash call that runs dev crew ask twice shares its tool call id; a repeat of the same send dedupes
        with TempState():
            progress.start("one shell call", plan="verify=5m")
            cmd = 'dev crew ask "First?" && dev crew ask "Second?" && dev crew ask "Second?"'
            progress.call_opened("claude", {"session_id": "s1", "tool_use_id": "toolu_one", "tool_name": "Bash",
                                            "tool_input": {"command": cmd}, "cwd": "/repo"})
            sent = []
            for text in ("First?", "Second?", "Second?"):
                with patch.object(sys, "argv", ["dev", "crew", "ask", text]):
                    sent.append(progress.crew_ask(text)["id"])
            self.assertNotEqual(sent[0], sent[1])
            self.assertEqual(sent[1], sent[2])
            self.assertEqual(2, len(progress.load_run("claude", "s1")["messages"]))

    def test_what_an_older_hook_wrote_to_the_run_file_survives_the_canonical_store(self):
        # a session not restarted after the upgrade runs 26.11.18 hooks, which know only the run file
        with TempState():
            progress.start("mixed writers", plan="verify=5m")
            launch("a1", "devkit:neo", "Neo: verify")
            question = progress.crew_ask("Ship it?", from_agent="a1")
            path = progress.run_path("claude", "s1")
            run = json.loads(path.read_text())
            old = next(m for m in run["messages"] if m["id"] == question["id"])
            old.update(answered=True, delivered=True)
            run["messages"].append({"id": "M90", "ts": round(time.time()), "from": "devkit", "to": "main",
                                    "text": "Written by an older hook", "delivered": False, "kind": "notice"})
            path.write_text(json.dumps(run))
            progress.crew_ask("Another question?", from_agent="a1")  # a save by this devkit
            stored = {m["id"]: m for m in progress.load_run("claude", "s1")["messages"]}
            self.assertTrue(stored[question["id"]]["answered"])
            self.assertTrue(stored[question["id"]]["delivered"])
            self.assertEqual("Written by an older hook", stored["M90"]["text"])

    def test_canonical_message_survives_legacy_projection_loss(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            question = progress.crew_ask("Persist across old writers")
            path = progress.run_path("claude", "s1")
            legacy = json.loads(path.read_text())
            legacy["messages"] = []
            legacy.pop("run_id", None)
            path.write_text(json.dumps(legacy))
            self.assertEqual(question["text"], progress.load_run("claude", "s1")["messages"][0]["text"])

    def test_same_request_reuses_answer(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            question = progress.crew_ask("Question?")
            answer = progress.crew_answer(question["id"], "Answer", request_id="retry-answer")
            replay = progress.crew_answer(question["id"], "Answer", request_id="retry-answer")
            self.assertEqual(answer["id"], replay["id"])
            self.assertEqual(2, len(progress.load_run("claude", "s1")["messages"]))

    def test_failed_answer_write_leaves_question_open(self):
        with TempState():
            progress.start("message integrity", plan="verify=5m")
            question = progress.crew_ask("Question?")
            replace = state.os.replace
            canonical_name = progress._message_path(question["run_id"]).name
            def fail_canonical(source, destination, **kwargs):
                if "messaging-v2" in str(destination) or str(destination) == canonical_name:
                    raise OSError("disk full")
                return replace(source, destination, **kwargs)
            with patch.object(state.os, "replace", side_effect=fail_canonical):
                with self.assertRaises(ValueError):
                    progress.crew_answer(question["id"], "Complete answer")
            messages = progress.load_run("claude", "s1")["messages"]
            self.assertFalse(messages[0].get("answered"))
            self.assertEqual(1, len(messages))

    def test_retry_after_directory_fsync_failure_reestablishes_durability(self):
        import os
        import stat
        with TempState():
            progress.start("durable retry", plan="verify=5m")
            question = progress.crew_ask("Question?")
            original = state.os.fsync
            def fail_directory(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):
                    raise OSError("directory fsync interrupted")
                return original(fd)
            with patch.object(state.os, "fsync", side_effect=fail_directory):
                with self.assertRaisesRegex(ValueError, "persistence_uncertain"):
                    progress.crew_answer(question["id"], "Durable answer", request_id="durable-retry")
            with patch.object(state.os, "fsync", wraps=original) as sync:
                answer = progress.crew_answer(question["id"], "Durable answer", request_id="durable-retry")
            self.assertGreaterEqual(sync.call_count, 2)
            self.assertEqual("Durable answer", answer["text"])
            self.assertEqual(2, len(progress.load_run("claude", "s1")["messages"]))


class MessageValueTests(unittest.TestCase):
    def setUp(self):
        global messaging
        from devkit import messaging

    def test_mcp_digest_redacts_all_decision_fields_before_preview(self):
        from devkit import mcp
        secret = "ghp_" + "a1B2" * 10
        record = {"members": [], "run": {"messages": [], "decisions": [{"id": "D1", "question": "Question",
                   "answer": "x" * 170 + secret, "options": [secret]}]}}
        result = json.dumps(mcp._digest_crew(record, {}))
        self.assertNotIn(secret, result)
        self.assertNotIn(secret[:20], result)

    def test_file_input_rejects_invalid_bytes_devices_and_parent_links(self):
        import os
        with TempState():
            root = progress.RUNS.parent
            args = argparse.Namespace(text=[], text_file=str(root / "input"), stdin=False)
            path = root / "input"
            for raw, error in ((b"\xff", "valid UTF-8"), (b"x" * 65537, "too_large"), (b"  \n", "nonempty")):
                path.write_bytes(raw)
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                    messaging.input_text(args, lambda _: None)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaisesRegex(ValueError, "regular file"):
                messaging.input_text(args, lambda _: None)
            folder = root / "real"
            folder.mkdir()
            (folder / "body").write_text("safe")
            (root / "alias").symlink_to(folder, target_is_directory=True)
            args.text_file = str(root / "alias" / "body")
            with self.assertRaisesRegex(ValueError, "safely"):
                messaging.input_text(args, lambda _: None)

    def test_stalled_stdin_rejects_without_unbounded_wait(self):
        import os
        reader, writer = os.pipe()
        args = argparse.Namespace(text=[], text_file=None, stdin=True)
        try:
            with os.fdopen(reader, "rb") as stream, patch.object(messaging.sys, "stdin", stream), \
                    patch.object(messaging.select, "select", return_value=([], [], [])):
                with self.assertRaisesRegex(ValueError, "deadline"):
                    messaging.input_text(args)
        finally:
            os.close(writer)

    def test_canonical_lock_rejects_symlink_before_open(self):
        with TempState():
            path = progress.RUNS.parent / "messaging-v2" / "runs" / "record.json"
            lock = state.lock_file(path)
            lock.parent.mkdir(parents=True)
            target = lock.parent / "target"
            target.write_text("unchanged")
            lock.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "unsafe state"):
                with state.hold(path):
                    pass
            self.assertEqual("unchanged", target.read_text())

    def test_unknown_identity_cannot_open_text_file(self):
        from devkit import identity, crew
        args = argparse.Namespace(text=[], text_file="/never-open", stdin=False)
        unknown = identity.Speaker("codex", "s1", "", "unknown", False)
        with patch.object(identity, "speaker", return_value=unknown), patch.object(messaging.os, "lstat") as opened:
            with self.assertRaisesRegex(ValueError, "identity"):
                messaging.input_text(args, crew.authorize_text_file)
            opened.assert_not_called()

    def test_a_key_name_followed_by_an_ordinary_word_is_not_a_secret(self):
        # workflow and policy text names keys with plain values; a value with a secret's shape stays refused
        for text in ("permissions:\n  id-token: write\n  contents: read", "password: required for the admin page",
                     "GITHUB_TOKEN: *** was masked by the runner", "secret_scanning: enabled", "GH_TOKEN: set in CI",
                     "token_count: 5", "password_min_length: 12", "credentials: default", "tokenCount: 3",
                     "GH_TOKEN: ${{ secrets.GH_TOKEN }}", "password: $DB_PASSWORD", "api_key: <your-api-key>",
                     "token: {{ vault.token }}"):
            with self.subTest(text=text):
                self.assertEqual(text, messaging.validate_body(text)["text"])
        # a plain word can be a real password: only the words that describe a setting pass, never any word
        # a count word counts only where it starts a part of the key's name; a value that only looks like a reference
        # (lower-case $name, an unclosed {{, a bracketed word with no placeholder shape) is a secret's value
        for text in ("password: hunter2x9", "api_key = 'Zq81mPx0'", "TOKEN=abcdefghijklmnopqrstuvwxyz",
                     "password: letmein", "token: Summer", "api_key=abcdefghijklmnopqrs", "token_admin: 482913",
                     "password_storage: 123456", "password: $ummer2026", "password: <hunter2>", "token: {{abc123"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "secret"):
                messaging.validate_body(text)

    def test_a_devkit_notice_naming_a_secret_is_stored_redacted_not_refused(self):
        # devkit and CI watch quote what they saw (a log line); an agent's own text with a secret is refused
        secret = "ghp_" + "a1B2" * 10
        with TempState():
            progress.start("notices", plan="verify=5m")
            m = progress._ask_into("claude", "s1", "CI failed: " + secret, "main", "ci", 1.0)
            stored = progress.load_run("claude", "s1")["messages"][-1]
            self.assertEqual(m["id"], stored["id"])
            self.assertNotIn(secret, stored["text"])
            self.assertIn("[redacted]", stored["text"])
            with self.assertRaisesRegex(ValueError, "secret"):
                progress.crew_ask("my token " + secret)

    def test_secret_crossing_page_boundary_is_redacted_before_paging(self):
        secret = "ghp_" + "a1B2" * 10
        text = "x" * 5800 + " " + secret + " end"
        item = {"id": "M1", "ref": "M1@r-legacy", "text": text}
        page = messaging.read_page(item)
        parts = [page["text"]]
        while not page["complete"]:
            page = messaging.read_page(item, cursor=page["next_cursor"])
            parts.append(page["text"])
        self.assertTrue(page["redacted"])
        self.assertNotIn(secret, "".join(parts))
        self.assertNotIn(secret[10:30], "".join(parts))

    def test_changed_body_digest_invalidates_cursor(self):
        item = dict(messaging.validate_body("x" * 10000), id="M1")
        first = messaging.read_page(item)
        changed = dict(item, **messaging.validate_body("y" * 10000))
        with self.assertRaisesRegex(ValueError, "invalid_cursor"):
            messaging.read_page(changed, cursor=first["next_cursor"])

    def test_capacity_failure_does_not_append_or_consume_key(self):
        record = {"messages": [], "padding": "x" * 500}
        before = messaging.encoded(record)
        with patch.object(messaging, "STORE_MAX", 510):
            with self.assertRaisesRegex(ValueError, "capacity"):
                messaging.append_message(record, {"id": "M1", "text": "Question"}, request_id="retry")
        self.assertEqual(before, messaging.encoded(record))

    def test_file_input_preserves_bytes_and_rejects_links(self):
        from devkit import crew
        with TempState() as fixture:
            path = progress.RUNS.parent / "body.txt"
            path.write_bytes("  界\r\nEND  ".encode())
            args = argparse.Namespace(text=[], text_file=str(path), stdin=False)
            self.assertEqual("  界\r\nEND  ", messaging.input_text(args, crew.authorize_text_file))
            link = path.with_name("link.txt")
            link.symlink_to(path)
            args.text_file = str(link)
            with self.assertRaisesRegex(ValueError, "regular file"):
                messaging.input_text(args, crew.authorize_text_file)

    def test_utf8_pages_reconstruct_exact_display(self):
        body = "界👩\u200d💻é\r\n" * 3000
        envelope = dict(messaging.validate_body(body), id="M1", ref="M1@r-test", generation=1)
        cursor, chunks = "", []
        while True:
            page = messaging.read_page(envelope, cursor=cursor)
            self.assertLessEqual(len(messaging.encoded(page)), 6144)
            chunks.append(page["text"])
            if page["complete"]:
                break
            cursor = page["next_cursor"]
        self.assertEqual(messaging.display(body)[0], "".join(chunks))
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), page["digest"])

    def test_limit_is_encoded_bytes_without_changing_whitespace(self):
        body = " " + "a" * 65534 + " "
        self.assertEqual(body, messaging.validate_body(body)["text"])
        with self.assertRaisesRegex(ValueError, "too_large"):
            messaging.validate_body(body + "界")


class PeerIntegrityTests(PeerCase):
    def test_canonical_marker_never_touches_a_swapped_parent_target(self):
        import os
        marker = peers._marker("claude", CLAUDE_A)
        peers._ready(peers.box_path("claude", CLAUDE_A))
        outside = self.root / "outside-marker"
        outside.mkdir()
        target = outside / marker.name
        target.write_text("outside content")
        os.utime(target, (10, 10))
        original = os.open
        swapped = []
        def swap_before_open(path, flags, *args, **kwargs):
            if str(path) in (str(marker), marker.name) and not swapped:
                marker.parent.rename(marker.parent.with_name("held-box"))
                marker.parent.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
            return original(path, flags, *args, **kwargs)
        with patch.object(peers.os, "open", side_effect=swap_before_open):
            try:
                peers._touch(marker)
            except (state.StateBusy, OSError):
                pass  # refusal is safe; following the replacement path is not
        self.assertTrue(swapped)
        self.assertEqual("outside content", target.read_text())
        self.assertEqual(10, target.stat().st_mtime)

    def test_canonical_preparation_never_chmods_a_swapped_parent_target(self):
        import os
        import stat
        destination = peers.box_path("claude", CLAUDE_A)
        namespace = peers.PEERS.parent / "messaging-v2"
        namespace.mkdir(mode=0o700)
        outside = self.root / "outside-state"
        outside.mkdir(mode=0o755)
        os.chmod(outside, 0o755)
        original = state.secure_path
        swapped = []
        def swap_after_check(path):
            original(path)
            if path == destination and not swapped:
                namespace.rename(self.root / "held-namespace")
                namespace.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
        with patch.object(state, "secure_path", side_effect=swap_after_check):
            with self.assertRaises((state.StateBusy, peers.PeerError)):
                peers._ready(destination)
        self.assertTrue(swapped)
        self.assertEqual(0o755, stat.S_IMODE(outside.stat().st_mode))
        self.assertEqual([], list(outside.iterdir()))

    def test_replay_repairs_missing_notification_marker_without_changing_receipts(self):
        import errno
        import os
        for acknowledged in (False, True):
            with self.subTest(acknowledged=acknowledged):
                self.session("claude", CLAUDE_A)
                self.session("claude", CLAUDE_B)
                marker = peers._marker("claude", CLAUDE_B)
                marker.unlink(missing_ok=True)
                real_open = os.open
                def fail_marker(path, flags, *args, **kwargs):
                    if str(path) in (str(marker), marker.name):
                        raise OSError(errno.ENOSPC, "notification marker fault")
                    return real_open(path, flags, *args, **kwargs)
                with patch.object(peers.os, "open", side_effect=fail_marker):
                    result = peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "Committed body " + str(acknowledged),
                                        request_id="marker-" + str(acknowledged), now=self.now)
                self.assertFalse(result["to"])
                message = next(m for m in peers.inbox("claude", CLAUDE_B, all_=True) if m["id"] == result["id"])
                self.assertFalse(peers.has_new("claude", CLAUDE_B))
                if acknowledged:
                    peers.ack_message(sender(CLAUDE_B), message["ref"], message["body_sha256"], message["generation"])
                before = next(m for m in peers.inbox("claude", CLAUDE_B, all_=True) if m["id"] == result["id"])
                retried = peers.replay(sender(CLAUDE_A), now=self.now)
                self.assertTrue(any(result["id"] == item["id"] and item["to"] for item in retried))
                self.assertEqual(not acknowledged, peers.has_new("claude", CLAUDE_B))
                copies = [m for m in peers.inbox("claude", CLAUDE_B, all_=True) if m["id"] == result["id"]]
                self.assertEqual([before], copies)

    def test_peer_selection_stops_at_page_without_consuming_remaining_bodies(self):
        self.session("claude", CLAUDE_A)
        self.session("claude", CLAUDE_B)
        sent = [peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "界" * 500 + str(i), now=self.now)
                for i in range(4)]
        visited = []
        def select(message):
            visited.append(message["id"])
            return True
        page = peers.take("claude", CLAUDE_B, mark=False, limit=1, select=select)
        self.assertEqual([sent[0]["id"]], [m["id"] for m in page])
        self.assertEqual([sent[0]["id"]], visited)
        self.assertEqual(4, len(peers.inbox("claude", CLAUDE_B)))

    def test_internal_notice_reference_reads_its_body(self):
        self.session("claude", CLAUDE_A)
        peers._post(peers._View(self.now), "claude:" + CLAUDE_A, "notice", "Settled decision", self.now,
                    ref="E-related")
        notice = peers.inbox("claude", CLAUDE_A)[0]
        self.assertEqual("Settled decision", peers.read_message(sender(CLAUDE_A), notice["ref"])["text"])

    def test_peer_answer_replays_after_destination_commit_before_source_receipt(self):
        self.session("claude", CLAUDE_A)
        self.session("claude", CLAUDE_B)
        question = peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "Question?", kind="question", now=self.now)
        source = peers.box_path("claude", CLAUDE_B)
        real_replace, replacements = state.os.replace, []
        def interrupt(source_path, destination, **kwargs):
            if str(destination) in (str(source), source.name):
                replacements.append(destination)
                if len(replacements) == 2:
                    raise OSError("process stopped before sender receipt")
            return real_replace(source_path, destination, **kwargs)
        with patch.object(state.os, "replace", side_effect=interrupt):
            with self.assertRaisesRegex(ValueError, "persistence_uncertain"):
                peers.answer(sender(CLAUDE_B), question["id"], "Complete answer", request_id="answer-key")
        before = peers._read(source)
        self.assertTrue(before["inbox"][0]["answer_pending"])
        self.assertFalse(before["inbox"][0]["answered"])
        peers.replay(sender(CLAUDE_B))
        answers = [m for m in peers.inbox("claude", CLAUDE_A, all_=True) if m["kind"] == "answer"]
        self.assertEqual(["Complete answer"], [m["text"] for m in answers])
        self.assertEqual(answers[0]["id"], peers._read(source)["inbox"][0]["answered"])

    def test_peer_body_preserves_unicode_and_line_endings(self):
        self.session("claude", CLAUDE_A)
        self.session("claude", CLAUDE_B)
        body = "  A👩\u200d💻é\r\n" * 200 + "END  "
        result = peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, body, now=self.now)
        self.assertEqual(body, peers.inbox("claude", CLAUDE_B, all_=True)[0]["text"])
        self.assertTrue(result["to"])

    def test_interrupted_peer_send_replays_same_intent(self):
        self.session("claude", CLAUDE_A)
        self.session("claude", CLAUDE_B)
        with patch.object(peers, "_fan_out", side_effect=RuntimeError("process interrupted")):
            with self.assertRaises(RuntimeError):
                peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "Durable intent", now=self.now, request_id="retry")
        result = peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "Durable intent", now=self.now, request_id="retry")
        replay = peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "Durable intent", now=self.now, request_id="retry")
        self.assertEqual(result["id"], replay["id"])
        self.assertEqual(["Durable intent"], [m["text"] for m in peers.inbox("claude", CLAUDE_B, all_=True)])

    def test_peer_store_survives_legacy_mailbox_removal(self):
        self.session("claude", CLAUDE_A)
        self.session("claude", CLAUDE_B)
        peers.send(sender(CLAUDE_A), "@" + CLAUDE_B, "Keep me", now=self.now)
        legacy = peers.PEERS / "box" / ("claude-" + CLAUDE_B + ".json")
        if legacy.exists():
            legacy.unlink()
        self.assertEqual("Keep me", peers.inbox("claude", CLAUDE_B, all_=True)[0]["text"])
