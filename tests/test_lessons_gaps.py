# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Lesson behavior no other test pins: a night's sleep never forgets the lesson a merge keeps (its dry run included,
# which must predict the real night); `dev lesson scan` names a guard refusal only from its third time; and the Codex
# path of a lesson: recalled at a Codex session's start, missed when a refusal of a Codex call shows its error (not
# when the call runs its command again), and carried to a Codex crew member at its SubagentStart, whose first call
# makes it pending for that member.
# Every test points devkit at a temporary handoff and state; none reads or writes the developer's.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_lessons_gaps
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()  # before devkit is imported, for a run of this module alone

from test_codex_launch import TANK, CodexCrew  # noqa: E402  (the sandbox Mac a Codex crew runs in)
from test_lessons_base import A, DAY, LessonCase  # noqa: E402
from test_lessons_hooks import GIT_ENV, LessonHookCase  # noqa: E402

from devkit import core, delivery, hooks, lessonevents, lessons, registry, work  # noqa: E402


class MergeKeeperTests(LessonCase):
    # two lessons about the same mistake, both open, seen once and idle for 200 days: the night merges one into the
    # other, and the archive policy alone would forget both
    def twins(self):
        where = {"commands": ["make deploy"], "errors": ["deploy key was rejected"], "files": ["deploy/**"]}
        x = self.add("Deploy key rejected on the first run", **where)
        y = self.add("Deploy key rejected again", new=True, why="a near-duplicate for the merge", **where)
        return {x["meta"]["id"], y["meta"]["id"]}

    def test_a_dry_run_never_forgets_the_lesson_its_merge_keeps(self):
        both = self.twins()
        acts = lessons.consolidate(dry_run=True, now=time.time() + 200 * DAY)
        merges = [i for k, i, _ in acts if k == "merge"]
        self.assertEqual(len(merges), 1)
        keeper = (both - set(merges)).pop()
        self.assertNotIn(keeper, [i for k, i, _ in acts if k == "forget"])
        self.assertEqual(self.active_ids(), sorted(both))  # a dry run moves nothing

    def test_the_dry_run_names_exactly_what_the_real_night_then_does(self):
        self.twins()
        future = time.time() + 200 * DAY
        planned = sorted((k, i) for k, i, _ in lessons.consolidate(dry_run=True, now=future))
        done = sorted((k, i) for k, i, _ in lessons.consolidate(now=future))
        self.assertEqual(planned, done)
        self.assertEqual(len(self.active_ids()), 1)  # the keeper stays; the dropped twin is archived
        self.assertEqual(len(self.archived_ids()), 1)


class ScanThresholdTests(LessonCase):
    # `dev lesson scan` reads the guard's refusals from the hooks log (lessons.signals): the hook writes each one as
    # "guard <agent> deny: <why>", and the scan suggests a lesson only for a refusal repeated three times
    WHY = "Never push to main directly: open a PR with `dev pr create` instead"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(hooks, "STATE", lessons.STATE)  # the hook writes the log the scan reads
        p.start()
        self.addCleanup(p.stop)

    def refused(self, times: int):
        for _ in range(times):
            hooks._log(f"guard claude deny: {self.WHY}")

    def guard_signals(self):
        return [detail for kind, detail, _ in lessons.signals(None, None, 14) if kind == "guard"]

    def test_a_refusal_seen_twice_is_not_reported(self):
        self.refused(2)
        self.assertEqual(self.guard_signals(), [])

    def test_a_refusal_seen_three_times_is_reported_once_with_its_count(self):
        self.refused(3)
        found = self.guard_signals()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("refused 3× in 14 days: Never push to main directly"), found[0])


class CodexSessionLessonTests(LessonHookCase):
    # a Codex session in a real git checkout, with every module's handoff pointed at the test's own
    SID = "019a1c19-0000-7000-8000-00000000c0de"
    REFUSED = "a deploy from a laptop is refused"  # the lesson's error anchor, as a refusal shows it

    def setUp(self):
        super().setUp()
        handoff = Path(self.handoff.name)
        patches = [(mod, "HANDOFF", handoff) for mod in (core, hooks, lessons, registry, delivery, work)]
        patches += [(mod, "NOTES", handoff / "projects") for mod in (core, registry)]  # bound from HANDOFF at import
        for obj, name, value in patches:
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.repo = (self.root / "work" / "api").resolve()
        self.repo.mkdir(parents=True)
        env = dict(os.environ, **GIT_ENV)
        for args in (["init", "-q", "-b", "main"], ["-c", "user.email=a@example.com", "-c", "user.name=a", "commit",
                                                     "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, env=env, capture_output=True)
        self.pid = core.project_identity(self.repo, "")[0]
        self.lid = lessons.add(self.pid, "Deploy only through the release job", severity="high",
                               commands=["make deploy"], errors=[self.REFUSED],
                               cause="a laptop deploy skipped the checks",
                               prevention="run the release job", happened="a deploy from a laptop broke prod",
                               a=A)["meta"]["id"]

    def codex(self, event: str, payload: dict, verdict=None) -> str:
        import contextlib
        import io
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(out), \
                mock.patch("devkit.guard.check", lambda *a, **k: verdict), \
                mock.patch.object(registry, "update_note", lambda *a, **k: False), \
                mock.patch.object(core, "is_temp", lambda p, real=core.is_temp: False if str(p).startswith(
                    str(self.repo)) else real(p)):
            hooks.run(event, "codex")
        return out.getvalue()

    def start(self, originator: str = "") -> dict:
        # `originator`: the thread's rollout opens with this session_meta, as Codex 0.167 writes it ("codex_exec" for
        # `codex exec`, which nobody watches, so it gets no welcome)
        payload = {"session_id": self.SID, "hook_event_name": "SessionStart", "source": "startup",
                   "cwd": str(self.repo), "model": "gpt-6.1-sol"}
        if originator:
            rollout = self.root / "codex" / f"rollout-2026-11-09T10-00-39-{self.SID}.jsonl"
            rollout.parent.mkdir(parents=True, exist_ok=True)
            rollout.write_text(json.dumps({"timestamp": "2026-11-09T18:00:39.934Z", "type": "session_meta", "payload": {
                "session_id": self.SID, "id": self.SID, "cli_version": "0.167.0", "originator": originator,
                "source": "exec"}}) + "\n")
            payload["transcript_path"] = str(rollout)
        return json.loads(self.codex("session-start", payload))  # Codex reads only a JSON hook output

    @staticmethod
    def told(out: dict) -> str:
        return out["hookSpecificOutput"]["additionalContext"]

    def call(self, cmd: str, tid: str, verdict=None) -> str:
        # `verdict`: what the guard's check says of the call (None allows it)
        return self.codex("guard", {"session_id": self.SID, "turn_id": "t1", "hook_event_name": "PreToolUse",
                                    "tool_name": "Bash", "tool_use_id": tid, "cwd": str(self.repo),
                                    "model": "gpt-6.1-sol", "tool_input": {"command": cmd}}, verdict)

    def outcomes(self):
        return [(e["event"], e["agent"]) for e in lessonevents._events(self.SID) if e["id"] == self.lid]

    def test_a_codex_session_start_recalls_the_lesson_and_leaves_it_pending_for_the_thread(self):
        told = self.told(self.start())
        pitfalls = told[told.index("Known pitfalls"):]
        self.assertIn(f"- Lesson {self.lid} (recorded by", pitfalls)
        self.assertIn("run the release job", pitfalls)
        self.assertEqual(lessonevents.shown_in(self.SID, "main"), [self.lid])

    def test_a_codex_exec_start_with_no_welcome_still_gets_the_lesson_as_its_context(self):
        # Codex reads a start's context only from hookSpecificOutput, welcome or not
        out = self.start(originator="codex_exec")
        self.assertNotIn("systemMessage", out)  # nobody watches `codex exec`: no welcome
        told = self.told(out)
        self.assertIn(f"- Lesson {self.lid} (recorded by", told[told.index("Known pitfalls"):])

    def test_a_codex_call_that_runs_the_lessons_command_is_no_miss_and_a_refusal_showing_its_error_is(self):
        # A lesson is missed when its mistake comes back (a refusal or an output shows its error), not when the
        # command runs again, which the right way does too
        self.start()
        self.call("make deploy", "exec-1")
        self.assertEqual(self.outcomes(), [("shown", "main")])
        self.call("make deploy HOST=laptop", "exec-2", verdict=("deny", f"devkit · {self.REFUSED}: run the job"))
        self.assertEqual(self.outcomes(), [("shown", "main"), ("missed", "main"), ("shown", "main")])
        self.assertEqual(lessonevents.shown_in(self.SID, "main"), [self.lid])  # the refusal shows it again


class CodexBriefLessonTests(CodexCrew):
    # the whole Codex launch as Codex 0.167 records it, in the sandbox Mac: Codex fires no PreToolUse for
    # spawn_agent, so the member's lessons reach it as SubagentStart context instead of in its brief

    def setUp(self):
        super().setUp()
        for name in ("backend", "testing", "devsecops"):  # installed for Codex on this Mac
            f = self.home / ".agents" / "skills" / name / "SKILL.md"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n")

    def lesson_id(self, out: str) -> str:
        return re.search(r"\b(L-[0-9a-z]{6,40})\b", out).group(1)

    def events(self, lid: str) -> list:
        f = self.state / "lessons-events.jsonl"
        recs = [json.loads(ln) for ln in (f.read_text().splitlines() if f.exists() else []) if ln.strip()]
        return [(r["event"], r["channel"], r["agent"]) for r in recs if r["id"] == lid]

    def test_a_codex_member_gets_its_steps_lesson_at_start_and_it_is_pending_for_it_after_its_first_call(self):
        s = self.lead()
        lid = self.lesson_id(s.ok("lesson", "add", "Version every public API route", "--kind", "design",
                                  "--happened", "a route changed shape and broke the mobile app",
                                  "--cause", "the route had no version in its path",
                                  "--prevent", "put the version in the path of every public route",
                                  "--file", "api/**"))
        told = self.context(self.spawn(s, TANK, "tank", "api"))
        self.assertIn(f"- Lesson {lid} (recorded by", told)
        self.assertIn("put the version in the path of every public route", told)
        self.assertEqual(self.events(lid), [])  # told, but Codex has not bound the member to a call yet
        s.sh("ls", member=TANK, member_type="tank")
        self.assertEqual(self.events(lid), [("shown", "brief", TANK)])


if __name__ == "__main__":
    unittest.main()
