# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The instruction canary's role check (the developer, M346; lesson L-fy2lfvacp64): a final message names crew members
# as `<role> (<Name>)`, the role in any language, never the Matrix name alone. The check reads the structure (the
# roster's name inside parentheses right after other words), never a list of role words. The status block's header
# and Crew line are prose too; code spans, code blocks and links are not. The main member is checked like every
# other (the developer's decision): "Tech lead (Morpheus)", and reports end with "Needs from the tech lead:".
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_canary
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

import _sandbox  # noqa: E402

from devkit import canary, crew  # noqa: E402

ROSTER = crew.load(team=False)  # devkit's own roster, never the team's changes in the developer's handoff repository


def bare(text):
    return canary.bare_names(text, ROSTER)


class RoleFormTests(unittest.TestCase):
    def test_a_name_standing_alone_in_prose_is_found(self):
        self.assertEqual(bare("The Keymaker has fixed the guard, and Tank reviewed it."), ["The Keymaker", "Tank"])

    def test_the_role_form_passes_in_any_language(self):
        for text in ("Security engineer (The Keymaker) has fixed the guard.",
                     "El ingeniero de backend (Tank) lo revisó.",
                     "バックエンドエンジニア（Tank）が確認しました。",
                     "the Backend engineer (Tank)'s 47ea30b changed the curve"):
            self.assertEqual(bare(text), [], text)

    def test_parentheses_without_a_role_before_them_do_not_pass(self):
        self.assertEqual(bare("(Tank) reviewed it; ask (Sati, Trinity) next."), ["Tank", "Sati", "Trinity"])

    def test_the_article_and_its_lowercase_form_count_as_the_name(self):
        self.assertEqual(bare("I asked the Keymaker, then Oracle answered."), ["The Keymaker", "The Oracle"])
        self.assertEqual(bare("Business analyst (the Oracle) answered."), [])

    def test_the_status_block_header_and_crew_line_are_prose(self):
        member = "**[Firefly> DevKit · Kid]** devkit · `main` · tooling\n- **Done:** x\n- **Next:** y"
        self.assertEqual(bare(member), ["Kid"])
        self.assertEqual(bare("**[Firefly> DevKit · DevEx engineer (Kid)]** devkit · `main` · tooling"), [])
        crew_line = "- **Crew:** Link ✓ release branch · Code reviewer (Seraph) ▸ review of the merge"
        self.assertEqual(bare(crew_line), ["Link"])

    def test_code_spans_code_blocks_links_and_paths_are_not_prose(self):
        text = ("Run `dev crew brief link \"…\"` and `Tank`.\n```bash\ndev crew ask --to Tank \"…\"\n```\n"
                "See https://example.com/Tank and /tmp/kid-approvals/Tank.md or tests/test_link.py.")
        self.assertEqual(bare(text), [])

    def test_an_alias_counts_and_so_does_the_main_member(self):
        self.assertEqual(bare("the scout found it; Needs from Morpheus: nothing"), ["Sparks", "Morpheus"])
        self.assertEqual(bare("the scout found it; Needs from the tech lead: nothing"), ["Sparks"])

    def test_a_word_that_only_contains_a_name_is_not_one(self):
        self.assertEqual(bare("Tanks, Linked, Kidney, Neon and Sparkle are not crew names."), [])


class RoleCheckTimeTests(unittest.TestCase):
    # the role check runs at every Stop: a final message built to be slow must not stall it (lesson L-g8lywzsgclv)
    # sizes where a pattern that rescans the text takes seconds and a one-pass one takes a few milliseconds; the
    # fastest of three runs, so a loaded machine does not fail it
    def fastest(self, call):
        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            call()
            best = min(best or 1e9, time.perf_counter() - t0)
        return best

    def test_a_pathological_final_message_is_read_in_bounded_time(self):
        for text in ("x " + "`" * 8000, "a." * 40000, "Tank " * 8000, "Backend engineer (Tank) " * 3000,
                     "```a\n" * 40000):
            with self.subTest(text=text[:24]):
                self.assertLess(self.fastest(lambda: bare(text)), 0.5)

    def test_a_status_bullet_with_a_long_run_of_spaces_is_parsed_in_bounded_time(self):
        head = "**[Firefly> DevKit]** api · `main` · Export\n"
        for bullet in ("- done" + " " * 30000, "- ** done" + " " * 30000 + "x", "- **Done** " + " \t" * 15000):
            with self.subTest(bullet=bullet[:12]):
                self.assertLess(self.fastest(lambda: canary.parse_status(head + bullet)), 0.5)


class MemberHeaderTests(unittest.TestCase):
    # a member's header carries its role now, in the message's language: a long one is still the status block
    def test_a_header_with_a_long_role_in_another_language_is_the_status_block(self):
        text = ("**[Firefly> DevKit · Ingeniera de experiencia de desarrollo y herramientas (Kid)]** devkit · `main` · "
                "x\n- **Done:** x\n- **Check:** ✓ 3 tests\n- **Next:** y")
        self.assertTrue(canary.has_marker(text))
        self.assertEqual(canary.CREW_RE.match(text).group(1),
                         "Ingeniera de experiencia de desarrollo y herramientas (Kid)")
        self.assertEqual(bare(text), [])


class ReasonTests(unittest.TestCase):
    def test_the_reason_gives_each_name_its_role_form(self):
        why = canary.roles_reason(["Tank", "The Keymaker"], ROSTER)
        self.assertIn("Backend engineer (Tank)", why)
        self.assertIn("Security engineer (The Keymaker)", why)
        self.assertIn("code spans", why)


class CodexTranscriptTests(unittest.TestCase):
    # a Codex 0.167 rollout: the previous turn's final message, then a new turn (task_started) with no reply yet. The
    # new turn has no final message: the previous turn's reply is not this turn's
    def test_a_new_codex_turn_without_a_reply_has_no_final_message(self):
        recs = [{"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                                                      "content": [{"type": "output_text", "text": "Done earlier."}]}},
                {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Done earlier."}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                      "content": [{"type": "input_text", "text": "and now?"}]}}]
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in recs))
        self.assertIsNone(canary.last_assistant_text(path))
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "message", "role": "assistant", "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Now "}, {"type": "output_text", "text": "this."}]}}) + "\n")
        self.assertEqual(canary.last_assistant_text(path), "Now this.")


# A process of the canary log, for the race test: it points devkit's state at the test's folder, then appends one
# result, or prunes while holding its read open until the append is done or 1.5 s pass. Nothing here starts an agent.
LOG_CHILD = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root, role = Path(sys.argv[2]), sys.argv[3]
from devkit import canary
canary.STATE, canary.LOG = root, root / "canary.jsonl"
if role == "prune":
    read = canary.records

    def records(days=7):
        got = read(days)
        (root / "read-done").touch()
        end = time.time() + 1.5
        while not (root / "appended").exists() and time.time() < end:
            time.sleep(0.01)
        return got
    canary.records = records
    canary.prune(90)
else:
    end = time.time() + 15
    while not (root / "read-done").exists() and time.time() < end:
        time.sleep(0.005)
    canary.record_corrected("codex", "0199a213-81c0", "acme/api", "the late result")
    (root / "appended").touch()
"""


class SharedStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        saved = (canary.STATE, canary.LOG)
        canary.STATE, canary.LOG = self.root, self.root / "canary.jsonl"
        self.addCleanup(lambda: setattr(canary, "STATE", saved[0]) or setattr(canary, "LOG", saved[1]))

    def status(self) -> dict:
        return json.loads((self.root / "status" / "acme__api.json").read_text())

    def test_status_keeps_the_latest_of_each_session(self):
        # two sessions of one project: the file's top level is the latest of either (what dev status read before),
        # and each session keeps its own latest, so a second session never reads the first one's as its own
        canary.save_status("acme/api", "claude", "A: tests green", "main", "/dev/acme/api", session="4d1cffc7-1b2c")
        canary.save_status("acme/api", "codex", "B: release cut", "rel", "/wt/rel", session="0199a213-81c0")
        doc = self.status()
        self.assertEqual((doc["line"], doc["agent"], doc["session"]), ("B: release cut", "codex", "0199a213-81c0"))
        self.assertEqual({k: v["line"] for k, v in doc["sessions"].items()},
                         {"claude:4d1cffc7-1b2c": "A: tests green", "codex:0199a213-81c0": "B: release cut"})
        self.assertEqual(doc["sessions"]["codex:0199a213-81c0"]["branch"], "rel")
        canary.save_status("acme/api", "claude", "A: pushed", "main", session="4d1cffc7-1b2c")
        self.assertEqual(self.status()["sessions"]["claude:4d1cffc7-1b2c"]["line"], "A: pushed")
        for i in range(canary.STATUS_SESSIONS):  # the most recent sessions only
            canary.save_status("acme/api", "codex", f"thread {i}", "main", session=f"0199a300-{i:04d}")
        kept = self.status()["sessions"]
        self.assertEqual(len(kept), canary.STATUS_SESSIONS)
        self.assertNotIn("claude:4d1cffc7-1b2c", kept)

    def test_an_append_during_a_prune_is_kept(self):
        # another session's Stop hook appends its result while `dev sleep` prunes the log: the rewrite and the
        # appends share one lock, so the late line survives (before, the prune wrote back what it had read)
        old = [{"ts": round(time.time()) - 60, "agent": "claude", "ok": True, "project": "acme/api"}] * 3
        canary.LOG.write_text("".join(json.dumps(r) + "\n" for r in old))
        procs = [subprocess.Popen([sys.executable, "-c", LOG_CHILD, str(TESTS.parent / "lib"), str(self.root), role],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_sandbox.env())
                 for role in ("prune", "append")]
        try:
            for p in procs:
                _, err = p.communicate(timeout=30)
                self.assertEqual(p.returncode, 0, err.decode()[-600:])
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait(5)
        lines = [json.loads(ln) for ln in canary.LOG.read_text().splitlines()]
        self.assertEqual(len(lines), 4, lines)
        self.assertEqual(lines[-1].get("status"), "the late result")


if __name__ == "__main__":
    unittest.main()
