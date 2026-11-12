# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (fixtures with the markers the stub gate looks for)
# The crew gates at SubagentStop: a member ran its proofs, loaded its skills, left no unfinished work or tests that
# cannot fail, and recorded a lesson after repeated guard refusals. Run: python3 -m unittest tests.test_crew_gates
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from unittest import mock  # noqa: E402

from devkit import audit, bugs, cli, core, gates, guard, lessonevents, lessons, skillguard, work  # noqa: E402

# Skills "installed" on this test machine: the gate asks only for skills a member can load.
HAVE = {"test-driven-development", "testing", "docs", "communicating"}

# A builder shaped like config/crew.json: phases with skills, devkit commands and proofs.
KID = {"id": "kid", "name": "Kid", "title": "DevEx engineer", "tools": "all",
       "skills": ["workspace", "testing", "docs", "communicating"],
       "procedure": [
           {"phase": "Build", "do": "build it", "skills": ["superpowers:test-driven-development"],
            "devkit": ["dev test"]},
           {"phase": "Document", "do": "document it", "skills": ["docs"], "devkit": ["dev docs check"]},
           {"phase": "Verify", "do": "verify it", "skills": ["testing"], "devkit": ["dev test", "dev stubs"],
            "proof": "dev test"},
           {"phase": "Hand off", "do": "hand off", "skills": ["communicating"], "devkit": ["dev progress step"],
            "proof": "dev progress step"}]}

# Kid as config/crew.json has it: the Document phase proves itself with `dev docs check`, Verify with `dev test prove`.
KID_DOCS = {**KID, "procedure": [
    {"phase": "Build", "do": "build it", "skills": ["superpowers:test-driven-development"], "devkit": ["dev test"],
     "proof": "dev test"},
    {"phase": "Document", "do": "document it", "skills": ["docs"], "devkit": ["dev docs check"],
     "proof": "dev docs check"},
    {"phase": "Verify", "do": "verify it", "skills": ["testing"], "devkit": ["dev test", "dev test prove"],
     "proof": "dev test prove"},
    KID["procedure"][-1]]}

# A read-only member (tools: read) whose procedure still names a proof and a skill.
SCOUT = {"id": "sparks", "name": "Sparks", "tools": "read", "skills": ["docs"],
         "procedure": [{"phase": "Search", "do": "search", "skills": ["docs"], "devkit": ["dev grep"],
                        "proof": "dev grep"}]}

SKILL_MD = "/Users/dev/.claude/plugins/cache/official/superpowers/7.0.0/skills/{}/SKILL.md"
LONG_TITLE = "In a crew lane, prove shell expansion of a program word with dev guard check, never by running it"
DENIAL = ("PreToolUse:Bash hook error: Raw `git worktree add ../copy` would create an unmanaged worktree. "
          "Use `dev wt new <branch>`.")
GOOD_TEST = ("import unittest\nfrom calc import add\n\n\nclass AddTests(unittest.TestCase):\n"
             "    def test_adds_two_numbers(self):\n        self.assertEqual(add(2, 3), 5)\n")


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def taught_anchors(text: str) -> tuple:
    # The `dev lesson add …` a hint teaches, as the CLI parses it: (its anchors, what the anchor gate says of them)
    taught = re.search(r"`(dev lesson add [^`]*)`", text)
    assert taught, f"no dev lesson add taught in: {text}"
    a = cli.build_parser().parse_args(shlex.split(taught.group(1))[1:])
    anchors = cli._lesson_anchors(a)
    return anchors, [p for p, _ in lessons._anchor_problems(**anchors)[1]]


class Transcript:
    # Builds a member's own transcript (JSONL) in the shape Claude Code writes for a subagent: the brief's record
    # carries the time the lane started, as Claude Code's `timestamp` (UTC, milliseconds).
    def __init__(self, path):
        self.path = Path(path)
        self.started = time.time()
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.started)) + ".000Z"
        self.records = [{"type": "user", "isSidechain": True, "timestamp": stamp,
                         "message": {"role": "user", "content": "the brief"}}]
        self.n = 0

    def call(self, name, result="ok", is_error=False, **inp):
        self.n += 1
        tid = f"toolu_{self.n:04d}"
        self.records.append({"type": "assistant", "isSidechain": True, "message": {
            "role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}})
        self.records.append({"type": "user", "isSidechain": True,
                             "toolUseResult": ("Error: " if is_error else "") + result,
                             "message": {"role": "user", "content": [
                                 {"type": "tool_result", "tool_use_id": tid, "content": result,
                                  "is_error": is_error}]}})
        return self

    def bash(self, command, result="ok", is_error=False):
        return self.call("Bash", result, is_error, command=command, description="run")

    def skill(self, name):
        return self.call("Skill", f"Launching skill: {name}", skill=name)

    def refused(self, command):
        return self.bash(command, DENIAL, is_error=True)

    def background(self, command, task_id):
        # A Bash call with run_in_background: Claude Code answers at once with the task's id; returns the call's id
        self.call("Bash", f"Command running in background with ID: {task_id}. Output is being written to: "
                          f"/private/tmp/claude-501/tasks/{task_id}.output You will be notified when it completes.",
                  command=command, run_in_background=True)
        return f"toolu_{self.n:04d}"

    def notified(self, task_id, tool_use_id, status="completed", as_message=False):
        # The task's end, as Claude Code writes it into the member's transcript: a queued_command attachment, or (as
        # some versions do) a meta user message with the same <task-notification> text
        text = (f"<task-notification>\n<task-id>{task_id}</task-id>\n<tool-use-id>{tool_use_id}</tool-use-id>\n"
                f"<output-file>/private/tmp/claude-501/tasks/{task_id}.output</output-file>\n<status>{status}</status>\n"
                f"<summary>Background command \"run\" {status} (exit code 0)</summary>\n</task-notification>")
        origin = {"kind": "task-notification", "producer": "session-task"}
        if as_message:
            self.records.append({"type": "user", "isSidechain": True, "isMeta": True, "origin": origin,
                                 "message": {"role": "user", "content": "[SYSTEM NOTIFICATION - NOT USER INPUT]\n"
                                                                        + text}})
        else:
            self.records.append({"type": "attachment", "isSidechain": True, "attachment": {
                "type": "queued_command", "prompt": text, "commandMode": "task-notification", "origin": origin}})
        return self

    def save(self):
        self.records.append({"type": "assistant", "isSidechain": True, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "**[Firefly> DevKit · DevEx engineer (Kid)]** repo · `main` · task"}]}})
        self.path.write_text("\n".join(json.dumps(r) for r in self.records) + "\n")
        return str(self.path)


class GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        (self.repo / "README.md").write_text("# repo\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        self.tx = Transcript(root / "agent-a1.jsonl")
        self._installed = skillguard.installed
        skillguard.installed = lambda agent: HAVE
        self._audit = audit.AUDIT
        audit.AUDIT = root / "audit"
        self._crew_cfg = core.CFG.get("crew", None)

    def tearDown(self):
        skillguard.installed = self._installed
        audit.AUDIT = self._audit
        if self._crew_cfg is None:
            core.CFG.pop("crew", None)
        else:
            core.CFG["crew"] = self._crew_cfg
        self.tmp.cleanup()

    def write(self, rel, text):
        # The member writes a file in the repository with the Write tool.
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.tx.call("Write", f"File created successfully at: {p}", file_path=str(p), content=text)
        return p

    def complete(self):
        # Everything Kid's procedure asks for: the skills of the phases it worked in, and both proofs.
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("devkit:communicating")
        self.tx.bash("bin/dev test")
        self.tx.bash('cd /repo && /Users/dev/devkit/bin/dev progress step "built it" --check "12 tests pass"')
        return self

    def gates(self, member=KID, roster=None):
        return gates.crew_gates(member, self.tx.save(), str(self.repo), roster)

    def rules(self, findings):
        return [(f["rule"], f["item"]) for f in findings]


class CrewGateTests(GateCase):
    def test_a_member_that_ran_its_proofs_and_loaded_its_skills_passes(self):
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.write("tests/test_calc.py", GOOD_TEST)
        self.complete()
        self.assertEqual(self.gates(), [])

    def test_a_proof_inside_quoted_text_or_never_run_is_missing(self):
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash("bin/dev test")
        self.tx.bash('echo "next: dev progress step"')  # text it printed, not a command it ran
        self.tx.bash('bash -c "dev progress step done"')  # a quoted string: devkit cannot see it run
        found = self.gates()
        self.assertEqual(self.rules(found), [("proofs", "dev progress step")])
        self.assertIn("dev progress step", found[0]["fix"])
        self.assertIn("dev progress step", found[0]["evidence"])

    def test_a_missing_skill_is_found_for_the_files_the_member_changed(self):
        self.write("tests/test_calc.py", GOOD_TEST)  # the `testing` skill rule covers test files
        self.tx.skill("superpowers:test-driven-development").skill("communicating")
        self.tx.bash("dev test").bash('dev progress step "x" --check "y"')
        found = self.gates()
        self.assertEqual(self.rules(found), [("skills", "testing")])
        self.assertIn("tests/test_calc.py", found[0]["evidence"])
        self.assertIn("testing", found[0]["fix"])

    def test_reading_the_skill_md_counts_as_loading_it(self):
        self.write("tests/test_calc.py", GOOD_TEST)
        self.tx.call("Read", "---\nname: testing\n---", file_path=SKILL_MD.format("testing"))
        self.tx.call("Read", "---\nname: tdd\n---", file_path=SKILL_MD.format("test-driven-development"))
        self.tx.skill("communicating")
        self.tx.bash("dev test").bash('dev progress step "x" --check "y"')
        self.assertEqual(self.gates(), [])

    def test_a_phase_skill_is_required_only_when_the_member_worked_in_that_phase(self):
        self.complete()
        self.assertEqual(self.gates(), [])  # it never documented: the Document phase's `docs` is not owed
        self.tx.bash("dev docs check")
        self.assertEqual(self.rules(self.gates()), [("skills", "docs")])

    def test_a_docs_proof_is_owed_only_when_the_member_changed_docs(self):
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.complete()
        self.tx.bash("bin/dev test prove")
        self.assertEqual(self.gates(KID_DOCS), [])  # code only: the Document phase did not apply
        self.write("docs/usage.md", "# Usage\n\nCall add(2, 3).\n")
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [("proofs", "dev docs check"), ("skills", "docs")])

    def test_a_docs_page_written_through_the_shell_owes_the_docs_proof(self):
        self.complete()
        self.shell_write("README.md", "# repo\n\nInstall it with make.\n")
        self.assertEqual(self.rules(self.gates(KID_DOCS)), [("proofs", "dev docs check"), ("skills", "docs")])

    def test_test_proofs_are_owed_only_when_the_member_changed_code(self):
        self.tx.skill("communicating").bash('dev progress step "looked into it" --check "read the parser"')
        self.assertEqual(self.gates(KID_DOCS), [])  # it changed nothing: only the hand-off proof applies
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.assertEqual([f["item"] for f in self.gates(KID_DOCS) if f["rule"] == "proofs"],
                         ["dev test", "dev test prove"])

    def test_unfinished_work_in_a_changed_file_is_found(self):
        (self.repo / "other.py").write_text("x = 1  # TODO not the member's file\n")
        self.write("calc.py", "def add(a, b):\n    return a + b  # TODO handle overflow\n")
        self.complete()
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["stubs"])
        self.assertIn("calc.py:2", found[0]["evidence"])
        self.assertNotIn("other.py", found[0]["evidence"])
        self.assertIn("dev task add", found[0]["fix"])

    def test_a_changed_test_file_that_cannot_fail_is_found(self):
        self.write("tests/test_calc.py", "import unittest\n\n\nclass AddTests(unittest.TestCase):\n"
                                         "    def test_adds(self):\n        self.assertTrue(True)\n")
        self.complete()
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["tests"])
        self.assertIn("test_adds", found[0]["evidence"])
        self.assertIn("dev test audit", found[0]["fix"])

    def test_a_proof_run_through_the_python_interpreter_counts(self):
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash("python3 bin/dev test")
        self.tx.bash('/usr/bin/python3 -u /Users/dev/devkit/bin/dev progress step "built it" --check "12 tests pass"')
        self.assertEqual(self.gates(), [])

    def edit(self, rel, old, new):
        # The member changes part of a file with the Edit tool.
        p = self.repo / rel
        p.write_text(p.read_text().replace(old, new))
        self.tx.call("Edit", f"The file {p} has been updated successfully.", file_path=str(p), old_string=old,
                     new_string=new)

    def test_another_members_lines_in_a_shared_file_are_not_blamed_on_this_member(self):
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_calc.py").write_text(GOOD_TEST)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "calc")
        with (self.repo / "calc.py").open("a") as fh:  # another member's uncommitted work in the same files
            fh.write("\n\ndef sub(a, b):\n    return a - b  # TODO theirs\n")
        with (self.repo / "tests" / "test_calc.py").open("a") as fh:
            fh.write("\n    def test_theirs(self):\n        self.assertTrue(True)\n")
        self.edit("calc.py", "    return a + b\n", "    return int(a) + int(b)\n")
        self.edit("tests/test_calc.py", "        self.assertEqual(add(2, 3), 5)\n",
                  "        self.assertEqual(add(2, 3), 5)\n        self.assertEqual(add('2', 3), 5)\n")
        self.complete()
        self.assertEqual(self.gates(), [])
        self.edit("calc.py", "    return int(a) + int(b)\n", "    return int(a) + int(b)  # TODO mine\n")
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["stubs"])
        self.assertIn("TODO mine", found[0]["evidence"])
        self.assertNotIn("TODO theirs", found[0]["evidence"])

    def shell_write(self, rel, text):
        # The member writes a file through the shell (a heredoc), so no Write or Edit call names it.
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self.tx.bash(f"cat > {rel} <<'EOF'\n{text}EOF")
        return p

    def test_unfinished_work_written_through_the_shell_is_found(self):
        self.shell_write("calc.py", "def add(a, b):\n    return a + b  # TODO handle overflow\n")
        self.complete()
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["stubs"])
        self.assertIn("calc.py:2", found[0]["evidence"])

    def test_a_test_that_cannot_fail_written_through_the_shell_is_found(self):
        self.shell_write("tests/test_calc.py", "import unittest\n\n\nclass AddTests(unittest.TestCase):\n"
                                               "    def test_adds(self):\n        self.assertTrue(True)\n")
        self.complete()
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["tests"])
        self.assertIn("test_adds", found[0]["evidence"])

    def test_a_shell_append_to_a_shared_file_blames_only_the_lines_its_command_holds(self):
        (self.repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "calc")
        with (self.repo / "calc.py").open("a") as fh:  # another member's uncommitted line in the same file
            fh.write("# TODO theirs\n")
        with (self.repo / "calc.py").open("a") as fh:
            fh.write("# FIXME mine\n")
        self.tx.bash("cat >> calc.py <<'EOF'\n# FIXME mine\nEOF")
        self.complete()
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["stubs"])
        self.assertIn("FIXME mine", found[0]["evidence"])
        self.assertNotIn("TODO theirs", found[0]["evidence"])

    def test_files_changed_before_the_lane_started_or_not_written_by_it_are_not_blamed(self):
        old = self.repo / "old.py"  # another member's uncommitted work from before this lane started
        old.write_text("x = 1  # TODO theirs, from before\n")
        before = self.tx.started - 600
        os.utime(old, (before, before))
        (self.repo / "now.py").write_text("y = 2  # TODO theirs, written meanwhile\n")  # no command of this member
        self.tx.bash("cat old.py").bash("python3 -m unittest -q")  # its shell read one and ran, but wrote neither
        self.complete()
        self.assertEqual(self.gates(), [])

    def test_two_guard_refusals_without_a_lesson_ask_for_one(self):
        self.complete()
        self.tx.refused("git worktree add ../copy")
        self.tx.bash("false", "Error: Exit code 1", is_error=True)  # a failing command is not a refusal
        self.assertEqual(self.gates(), [])  # one refusal is not a pattern yet
        self.tx.refused("git worktree add ../copy2")
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["lessons"])
        self.assertIn("2 guard refusals", found[0]["evidence"])
        self.assertIn("dev lesson add", found[0]["fix"])

    def test_the_lessons_fix_teaches_an_add_with_an_anchor_the_gate_accepts(self):
        # `dev lesson add` refuses a lesson with no anchor: a fix without one sends the member to a command that fails
        self.complete()
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        anchors, problems = taught_anchors(self.gates()[0]["fix"])
        self.assertTrue(any(anchors.values()), anchors)
        self.assertEqual(problems, [])

    def test_a_progress_note_with_a_steps_evidence_records_that_step(self):
        # A long or paused step records its evidence without closing (progress.note with a step and a check): that is
        # the step's record the gate asks for; a note without evidence is not
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("devkit:communicating")
        self.tx.bash("bin/dev test")
        self.tx.bash('dev progress note "the parser builds" --next "the tests"')
        self.assertEqual(self.rules(self.gates()), [("proofs", "dev progress step")])
        self.tx.bash('dev progress note "the parser builds" --step parser --check "12 tests pass"')
        self.assertEqual(self.gates(), [])

    def test_a_read_only_member_skips_the_shell_checks(self):
        self.tx.call("Grep", "calc.py:1: def add", pattern="def add")
        self.tx.refused("ls").refused("ls -la")  # its tools hold no shell, so neither proof nor lesson is owed
        found = self.gates(SCOUT)
        self.assertEqual(self.rules(found), [("skills", "docs")])  # it still loads the skills it can

    def test_the_member_is_found_by_name_in_the_roster(self):
        self.complete()
        self.assertEqual(self.gates("Kid", roster={"members": [SCOUT, KID]}), [])
        self.assertEqual(self.rules(self.gates("Sparks", roster={"members": [SCOUT, KID]})), [("skills", "docs")])


class RefusalAnswerTests(GateCase):
    # The lessons gate also accepts devkit's bug registry for a refusal (a report from the lane, or a workaround the
    # registry served for it) and a refusal an open lesson already covers: only refusals nothing answers owe a lesson.
    FP, CODE = "aaaaaaaaaaa1", "q7w8e9"

    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        handoff, state = root / "handoff", root / "state"
        for obj, name, value in ((lessons, "LESSONS", handoff / "lessons"), (lessons, "HANDOFF", handoff),
                                 (lessons, "USAGE_LOCAL", state / "lessons-usage.json"),
                                 (lessons, "STATE", state), (bugs, "BUGS", root / "bugs"),
                                 (work, "HANDOFF", handoff), (work, "ITEMS", handoff / "items"),
                                 (core, "HANDOFF", handoff), (core, "STATE", state),
                                 (lessonevents, "EVENTS", state / "lessons-events.jsonl")):
            p = mock.patch.object(obj, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.complete()

    def lesson(self, title, **kw):
        return lessons.add("global", title, happened="the guard refused it", cause="no managed worktree",
                           prevention="use dev wt new", a={"user": "ana", "host": "mbp1", "agent": "claude",
                                                           "session": "s1"}, **kw)

    def owed(self):
        return [f["rule"] for f in gates.crew_gates(KID, self.tx.save(), str(self.repo), session="s1")]

    def dev_lesson(self, *argv):
        # `dev lesson …` as the member's shell runs it: its exit code and what it printed, both streams
        out, here = io.StringIO(), os.getcwd()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = cli.main(["lesson", *argv])
        finally:
            os.chdir(here)
        return rc, out.getvalue()

    def ran(self, command, rc, out):
        # the member's Bash call and its result, as Claude Code writes a command that exited with `rc`
        return self.tx.bash(command, out if rc == 0 else f"Exit code {rc}\n{out}", is_error=rc != 0)

    def test_a_lesson_recurrence_answers_the_refusals_and_a_recurrence_of_no_lesson_does_not(self):
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        self.tx.bash('echo "dev lesson recur later"')  # mentioned, not run
        cmd = 'bin/dev lesson recur {} --evidence "worktree refused twice"'
        self.ran(cmd.format("L-fx1"), *self.dev_lesson("recur", "L-fx1", "--evidence", "worktree refused twice"))
        self.assertEqual(self.owed(), ["lessons"])  # it ran, and counted nothing: no such lesson
        lid = self.lesson("Create worktrees with dev wt new", commands=["git worktree add"])["meta"]["id"]
        rc, out = self.dev_lesson("recur", lid, "--evidence", "worktree refused twice")
        self.assertEqual(rc, 0, out)
        self.ran(cmd.format(lid), rc, out)
        self.assertEqual(self.owed(), [])

    def test_a_failed_lesson_add_does_not_answer_the_refusals(self):
        # A lane passed the gate after four refused adds and a crashed recur, with no lesson recorded: an add that
        # ran is not an add that recorded a lesson. Piped through tail, a refused add even exits 0.
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        why = ["--global", "--happened", "git worktree add was refused", "--cause", "a raw worktree is unmanaged",
               "--prevent", "use dev wt new", "--command", "git worktree add"]

        def piped(argv):
            return "bin/dev lesson " + " ".join(shlex.quote(a) for a in argv) + " 2>&1 | tail -5"
        refused = ["add", LONG_TITLE, *why]
        rc, out = self.dev_lesson(*refused)
        self.assertEqual(rc, 2, out)
        self.tx.bash(piped(refused), out)
        self.assertEqual(self.owed(), ["lessons"])
        recorded = ["add", "Create worktrees with dev wt new", *why]
        rc, out = self.dev_lesson(*recorded)
        self.assertEqual(rc, 0, out)
        self.tx.bash(piped(recorded), out)
        self.assertEqual(self.owed(), [])

    def test_a_bug_report_on_the_refusal_from_the_lane_answers_the_refusals(self):
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        self.tx.bash('echo "dev bug report --guard later"')  # mentioned, not run
        self.tx.bash('bin/dev bug report --title "the status line is empty"')  # a report, but not on a refusal
        self.assertEqual(self.owed(), ["lessons"])
        self.tx.bash("bin/dev bug report --guard check_git:worktree")
        self.assertEqual(self.owed(), [])

    def test_a_vote_on_the_bug_a_refusal_names_answers_it_and_a_vote_on_another_does_not(self):
        known = f"{DENIAL}\n\ndevkit has a bug report for this refusal, #12, with no reviewed workaround yet.\n" \
                f"devkit-bug: {self.FP} {self.CODE}"
        self.tx.bash("git worktree add ../copy", known, is_error=True).refused("git worktree add ../copy2")
        self.tx.bash("dev bug report bbbbbbbbbbb2")
        self.assertEqual(self.owed(), ["lessons"])
        self.tx.bash(f"dev bug report {self.FP}")
        self.assertEqual(self.owed(), [])

    def test_a_refusal_the_registry_served_a_workaround_for_owes_no_lesson(self):
        served = f"{DENIAL}\n\ndevkit bug #12 has a workaround for this refusal.\nWorkaround d1, confirmed by a " \
                 f"devkit maintainer:\n  1. run it from the repository root\ndevkit-bug: {self.FP} {self.CODE}"
        self.tx.bash("git worktree add ../copy", served, is_error=True).refused("git worktree add ../copy2")
        self.assertEqual(self.owed(), ["lessons"])  # the marker alone proves nothing: this session served nothing
        bugs._ledger_write("s1", {"served": self.FP, "code": self.CODE, "detours": ["d1"]})
        self.assertEqual(self.owed(), [])  # one refusal left, and one is not a pattern

    def test_a_refusal_an_open_lesson_covers_owes_no_lesson(self):
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        covering = self.lesson("Create worktrees with dev wt new", errors=["would create an unmanaged worktree"])
        self.assertEqual(self.owed(), [])
        lessons.retire(covering, "dev wt new is gone", a={"user": "ana", "host": "mbp1", "agent": "human"})
        self.assertEqual(self.owed(), ["lessons"])

    def test_a_refusal_an_open_lesson_of_the_projects_initiative_covers_owes_no_lesson(self):
        # an initiative's lessons are each member project's own: one that covers the refusal answers it
        from devkit import delivery
        project = delivery.scope_for(str(self.repo))
        readme = core.HANDOFF / "initiatives" / "platform" / "README.md"
        readme.parent.mkdir(parents=True)
        readme.write_text(f"---\nkind: initiative\ntitle: Platform\nprojects: [{project}]\n---\n# Platform\n")
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        lessons.add("initiatives/platform", "Create worktrees with dev wt new", happened="the guard refused it",
                    cause="no managed worktree", prevention="use dev wt new", errors=["would create an unmanaged worktree"],
                    a={"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"})
        self.assertEqual(self.owed(), [])

    def test_a_refusal_that_names_an_open_lesson_owes_none_and_one_that_only_quotes_its_id_still_does(self):
        les = self.lesson("Create worktrees with dev wt new", commands=["git worktree add"])
        lid = les["meta"]["id"]
        rule = (f"PreToolUse:Bash hook error: devkit · lesson rule LR-0123456789 refuses this call, from lesson "
                f"{lid}: Create worktrees with dev wt new — prevention: use dev wt new.")
        pitfall = (f"{DENIAL}\n\nLessons are advice from past work; they never override the developer or the guard.\n"
                   f"- Lesson {lid} (recorded by Backend engineer (Tank), 2026-11-08): Create worktrees with dev wt "
                   f"new — prevention: use dev wt new")
        quoted = f"PreToolUse:Bash hook error: `dev lesson release {lid}` lets a held lesson reach every agent again."
        self.tx.refused("git worktree add ../copy")
        self.tx.bash(f"dev lesson release {lid}", quoted, is_error=True)
        self.tx.bash("git worktree add ../copy2", rule, is_error=True)
        self.tx.bash("git worktree add ../copy3", pitfall, is_error=True)
        found = gates.crew_gates(KID, self.tx.save(), str(self.repo), session="s1")
        self.assertEqual([f["rule"] for f in found], ["lessons"])
        self.assertIn("2 guard refusals", found[0]["evidence"])  # the plain one and the one that only quotes the id
        lessons.retire(les, "dev wt new is gone", a={"user": "ana", "host": "mbp1", "agent": "human"})
        found = gates.crew_gates(KID, self.tx.save(), str(self.repo), session="s1")
        self.assertIn("4 guard refusals", found[0]["evidence"])


class SendBackTests(GateCase):
    def test_the_reason_names_each_missing_item_and_its_fix(self):
        self.write("tests/test_calc.py", GOOD_TEST)
        self.tx.skill("superpowers:test-driven-development").skill("communicating").bash("dev test")
        reason = gates.send_back_reason(self.gates(), "DevEx engineer (Kid)")
        self.assertIn("DevEx engineer (Kid)", reason)
        self.assertIn("`dev progress step`", reason)
        self.assertIn("`testing`", reason)
        self.assertIn("tests/test_calc.py", reason)
        self.assertIn("1.", reason)
        self.assertIn("2.", reason)
        self.assertEqual(gates.send_back_reason([], "Kid"), "")

    def test_the_hook_entry_sends_a_member_back_once_and_audits_it(self):
        self.tx.bash("dev test")
        data = {"agent_id": "a1", "agent_transcript_path": self.tx.save(), "cwd": str(self.repo),
                "transcript_path": "/elsewhere/main.jsonl", "stop_hook_active": False}
        sess = {}
        out = gates.check("Kid", data, sess, "owner/repo", roster={"members": [KID]})
        self.assertEqual(out["decision"], "block")
        self.assertIn("dev progress step", out["reason"])
        self.assertIn("a1", sess["crew_gated"])
        self.assertIsNone(gates.check("Kid", {**data, "stop_hook_active": True}, sess, "owner/repo",
                                      roster={"members": [KID]}))  # sent back once, like the canary
        recs = audit.records(1, action="crew.gate")
        self.assertEqual([(r["target"], r["outcome"]) for r in recs], [("owner/repo", "blocked")])
        self.assertIn("proofs", recs[0]["detail"])

    def test_with_enforce_off_the_gate_only_records(self):
        core.CFG["crew"] = {**(self._crew_cfg or {}), "gates": {"enforce": False}}
        self.tx.bash("dev test")
        data = {"agent_id": "a2", "agent_transcript_path": self.tx.save(), "cwd": str(self.repo)}
        self.assertIsNone(gates.check("Kid", data, {}, "owner/repo", roster={"members": [KID]}))
        self.assertEqual([r["outcome"] for r in audit.records(1, action="crew.gate")], ["recorded"])

    def test_the_hook_entry_fails_open_without_the_member_transcript(self):
        data = {"agent_id": "a3", "transcript_path": "/elsewhere/main.jsonl", "cwd": str(self.repo)}
        self.assertIsNone(gates.check("Kid", data, {}, "owner/repo", roster={"members": [KID]}))


class BackgroundWorkTests(GateCase):
    # Claude Code keeps a member in its agent list while a background command it started still runs, though the
    # member reported and devkit closed its lane. The gate sends it back once to wait for the work or stop it.
    def data(self, aid="a1", **over):
        return {"agent_id": aid, "agent_transcript_path": self.tx.save(), "cwd": str(self.repo), **over}

    def test_a_background_command_still_running_sends_the_member_back_naming_it(self):
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        sess = {}
        out = gates.check("Kid", self.data(), sess, "owner/repo", roster={"members": [KID]})
        self.assertEqual(out["decision"], "block")
        self.assertIn("`bin/dev test` (task `b7k2m9x1q`)", out["reason"])
        self.assertIn("TaskStop `b7k2m9x1q`", out["reason"])
        recs = audit.records(1, action="crew.gate")
        self.assertEqual([r["outcome"] for r in recs], ["blocked"])
        self.assertIn("background: bin/dev test", recs[0]["detail"])
        self.assertNotIn("crew_background", sess)  # sent back: its lane stays open, the tech lead is not told yet

    def test_a_command_moved_to_the_background_after_some_output_is_found_by_its_result_id(self):
        # the commonest real shape has output before Claude Code's
        # "Command was moved to the background" line, and the id only in the result record's backgroundTaskId
        self.complete()
        self.tx.call("Bash", "Ran 1595 tests in 212s\nCommand was moved to the background (ID: m0v3d4w4y). "
                     "You will be notified when it completes.", command="bin/dev test", description="run")
        self.tx.records[-1]["toolUseResult"] = {"stdout": "Ran 1595 tests in 212s", "backgroundTaskId": "m0v3d4w4y"}
        out = gates.check("Kid", self.data(), {}, "owner/repo", roster={"members": [KID]})
        self.assertEqual(out["decision"], "block")
        self.assertIn("`bin/dev test` (task `m0v3d4w4y`)", out["reason"])
        self.assertIn("do not end your turn to wait", out["reason"])

    def test_a_notification_that_the_task_ended_counts_whatever_its_status(self):
        self.complete()
        tests = self.tx.background("bin/dev test", "b7k2m9x1q")
        self.tx.notified("b7k2m9x1q", tests, "failed")
        probe = self.tx.background("python3 probe.py", "c3p0r2d2x")
        self.tx.notified("c3p0r2d2x", probe, "completed", as_message=True)
        self.tx.background("npm run watch", "q9w8e7r6t")  # nothing ever ended it
        self.assertEqual(self.rules(self.gates()),
                         [("background", "npm run watch")])

    def test_taskstop_or_killshell_ends_it_unless_the_guard_refused_the_stop(self):
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        self.tx.call("TaskStop", json.dumps({"message": "Successfully stopped task: b7k2m9x1q (bin/dev test)",
                                             "task_id": "b7k2m9x1q", "task_type": "local_bash"}),
                     task_id="b7k2m9x1q")
        self.tx.background("python3 probe.py", "c3p0r2d2x")
        self.tx.call("KillShell", "Task c3p0r2d2x is not running (status: completed)", True, shell_id="c3p0r2d2x")
        self.tx.background("tail -f build.log", "q9w8e7r6t")
        self.tx.call("TaskStop", DENIAL, True, task_id="q9w8e7r6t")  # refused: the command still runs
        found = self.gates()
        self.assertEqual(self.rules(found), [("background", "tail -f build.log")])
        self.assertEqual(found[0]["task"], "q9w8e7r6t")
        self.assertIn("TaskStop `q9w8e7r6t`", found[0]["fix"])

    def test_a_read_of_its_output_that_shows_it_exited_ends_it(self):
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        self.tx.call("TaskOutput", "<retrieval_status>success</retrieval_status>\n<task_id>b7k2m9x1q</task_id>\n"
                                   "<status>completed</status>\n<exit_code>0</exit_code>\n<output>OK</output>",
                     task_id="b7k2m9x1q")
        self.tx.background("python3 probe.py", "c3p0r2d2x")
        self.tx.call("BashOutput", "<status>running</status>\n<stdout>still going</stdout>", bash_id="c3p0r2d2x")
        self.assertEqual(self.rules(self.gates()), [("background", "python3 probe.py")])

    def test_a_member_that_started_no_background_work_owes_none(self):
        self.complete()
        # a foreground command whose output quotes Claude Code's launch text, and a notification of another task
        self.tx.bash("grep -r 'running in background' docs",
                     "docs/x.md: Command running in background with ID: zz9yy8xx7. Output is being written to: f")
        self.tx.notified("zz9yy8xx7", "toolu_9999")
        self.assertEqual(gates.background_work(self.tx.save()), [])
        self.assertEqual(self.gates(), [])

    def test_sent_back_once_it_is_let_through_with_the_work_still_running_audited_and_kept_for_the_tech_lead(self):
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        sess = {}
        roster = {"members": [KID]}
        self.assertEqual(gates.check("Kid", self.data(), sess, "owner/repo", roster=roster)["decision"], "block")
        self.assertIsNone(gates.check("Kid", self.data(stop_hook_active=True), sess, "owner/repo", roster=roster,
                                      lead_name="a helper of DevEx engineer (Kid)"))
        recs = audit.records(1, action="crew.gate")
        self.assertEqual([(r["outcome"], (r.get("ref") or {}).get("result")) for r in recs],
                         [("blocked", None), ("recorded", "let through")])
        self.assertIn("b7k2m9x1q", recs[1]["detail"])
        self.assertEqual(sess["crew_background"]["a1"]["who"], "a helper of DevEx engineer (Kid)")
        self.assertEqual(sess["crew_background"]["a1"]["tasks"], [{"id": "b7k2m9x1q", "command": "bin/dev test"}])

    def test_with_enforce_off_a_member_with_background_work_is_recorded_for_the_tech_lead(self):
        core.CFG["crew"] = {**(self._crew_cfg or {}), "gates": {"enforce": False}}
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        sess = {}
        self.assertIsNone(gates.check("Kid", self.data("a2"), sess, "owner/repo", roster={"members": [KID]},
                                      lead_name="DevEx engineer (Kid)"))
        self.assertEqual([t["id"] for t in sess["crew_background"]["a2"]["tasks"]], ["b7k2m9x1q"])

    def test_without_a_name_from_devkits_records_nothing_is_kept_for_the_tech_lead(self):
        # the hook passes the name its launch records give the agent; with none, the work is audited but no line
        # reaches the tech lead, since the member's own name may come from a description another member wrote
        core.CFG["crew"] = {**(self._crew_cfg or {}), "gates": {"enforce": False}}
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        sess = {}
        self.assertIsNone(gates.check("Kid", self.data("a3"), sess, "owner/repo", roster={"members": [KID]}))
        self.assertNotIn("crew_background", sess)
        self.assertIn("background: bin/dev test", audit.records(1, action="crew.gate")[0]["detail"])


class CrewOwnerReadTests(unittest.TestCase):
    # `dev crew owner <path>… [--json]` only says which lane holds a file, so a member asks it before an edit; a crew
    # command that changes the team (`dev crew set`) stays the tech lead's

    def verdict(self, cmd):
        v = guard.crew_shell_verdict(cmd, str(Path(__file__).resolve().parents[1]), None, "tank")
        return v[1] if v else None

    def test_a_member_may_ask_who_owns_a_file_but_not_change_the_team(self):
        for cmd in ("dev crew owner lib/x.py", "dev crew owner --json lib/x.py", "bin/dev crew owner lib/x.py docs/a.md"):
            with self.subTest(cmd=cmd):
                self.assertEqual(guard._crew_kind(cmd.split()[1:]), "read")
                self.assertIsNone(self.verdict(cmd))
        self.assertEqual(guard._crew_kind(["crew", "set", "tank", "title", "x", "--why", "y"]), "team")
        self.assertIsNotNone(self.verdict("dev crew set tank title x --why y"))


if __name__ == "__main__":
    unittest.main()
