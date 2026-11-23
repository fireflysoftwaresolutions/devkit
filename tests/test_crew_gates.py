# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (fixtures with the markers the stub gate looks for)
# The crew gates at SubagentStop: a member ran its proofs, loaded its skills, left no unfinished work or tests that
# cannot fail, and recorded a lesson after repeated hook refusals. Run: python3 -m unittest tests.test_crew_gates
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

from devkit import audit, bugs, cli, core, gates, gitx, lessonevents, lessons, shellparse, skillguard, work  # noqa: E402
from devkit import lanescope  # noqa: E402
from devkit import progress as pg  # noqa: E402

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

# A release engineer shaped like config/crew.json: its proof is the full local CI run, which a brief or the project
# can forbid (a shared checkout where other members' uncommitted work fails it, runner preparation not to replay).
LINK = {"id": "link", "name": "Link", "title": "Release engineer", "tools": "all", "skills": ["communicating"],
        "procedure": [
            {"phase": "Know the pipeline", "do": "run what CI runs", "skills": [], "devkit": ["dev ci", "dev ci --run"],
             "proof": "dev ci --run"},
            {"phase": "Hand off", "do": "hand off", "skills": ["communicating"], "devkit": ["dev progress step"],
             "proof": "dev progress step"}]}
WAIVER = "Waived proof: `dev ci --run` — the shared checkout holds other members' uncommitted work"
# The gates trust a `timeout` only from a system folder, as they do a uv or a python: this Mac's must be there
TIMEOUT = any(os.path.isfile(os.path.join(d, "timeout")) for d in ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"))

# A read-only member (tools: read) whose procedure still names a proof and a skill.
SCOUT = {"id": "sparks", "name": "Sparks", "tools": "read", "skills": ["docs"],
         "procedure": [{"phase": "Search", "do": "search", "skills": ["docs"], "devkit": ["dev grep"],
                        "proof": "dev grep"}]}

SKILL_MD = "/Users/dev/.claude/plugins/cache/official/superpowers/7.0.0/skills/{}/SKILL.md"
LONG_TITLE = "In a crew lane, prove shell expansion of a program word with dev stubs check, never by running it"
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

    def brief(self, text):
        # the launch's prompt, which Claude Code writes as the first record of the member's transcript
        self.records[0]["message"]["content"] = text
        return self

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
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "dev").symlink_to(core.DEVKIT_HOME / "bin" / "dev")
        path = mock.patch.dict(os.environ, {"PATH": str(self.repo / "bin") + os.pathsep + os.environ["PATH"]})
        path.start()
        self.addCleanup(path.stop)
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
        self.tx.bash(f'cd {shlex.quote(str(self.repo))} && {shlex.quote(str(core.DEVKIT_HOME / "bin/dev"))} '
                     'progress step "built it" --check "✓ 12 tests pass"')
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

    def proofs_with(self, test_command, successful=False):
        # Kid changes code (so `dev test` is owed), loads its skills, runs `dev test` as `test_command` and hands
        # off; the proofs findings that remain.
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash(test_command)
        self.tx.bash('dev progress step "built it" --check "✓ 12 tests pass"')
        found = gates.crew_gates(KID, self.tx.save(), str(self.repo), successful=successful)
        return [f for f in found if f["rule"] == "proofs"]

    def test_a_proof_run_in_a_folder_or_worktree_of_the_repository_counts_and_another_repository_does_not(self):
        (self.repo / "lib").mkdir()
        tree = Path(self.tmp.name) / "worktrees" / "feat-x"
        git(self.repo, "worktree", "add", "-q", "-b", "feat/x", str(tree))
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        for folder, owed in ((self.repo / "lib", []), (tree, []), (other, [("proofs", "dev test")])):
            with self.subTest(folder=folder.name):
                self.tx = Transcript(Path(self.tmp.name) / f"agent-{folder.name}.jsonl")
                found = self.proofs_with(f"cd {shlex.quote(str(folder))} && dev test")
                self.assertEqual(self.rules(found), owed)

    # A member's shell starts in the session's folder each call, so it reaches its lane's worktree with a cd in
    # front of its proof, joined with && or with ; (or a new line). Either form counts, in the repository or one of its
    # worktrees: with `;` a cd that fails leaves the proof in the folder it started in, so that folder must be one of
    # them too. Any other prefix still proves nothing: it may change which program runs, or the exit code is not the
    # proof's own.
    def test_a_proof_whose_only_prefix_is_a_cd_into_the_lanes_checkout_counts(self):
        tree = Path(self.tmp.name) / "worktrees" / "feat-x"
        git(self.repo, "worktree", "add", "-q", "-b", "feat/x", str(tree))
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        at, away = shlex.quote(str(tree)), shlex.quote(str(other))
        for command, owed in ((f"cd {at} && dev test run tests/test_calc.py", []),
                              (f"cd {at}; dev test run tests/test_calc.py", []),
                              (f"cd {at}\ndev test", []),
                              (f"cd {at}; cd lib && dev test", []),
                              ("export PATH=/tmp/bin:$PATH; dev test", [("proofs", "dev test")]),
                              ("true; dev test", [("proofs", "dev test")]),
                              (f"cd {away}; dev test", [("proofs", "dev test")]),
                              (f"cd {away}; cd {at}; dev test", [("proofs", "dev test")]),
                              (f"cd {at} || dev test", [("proofs", "dev test")])):
            with self.subTest(command=command):
                (tree / "lib").mkdir(exist_ok=True)
                self.tx = Transcript(Path(self.tmp.name) / f"agent-cd-{abs(hash(command))}.jsonl")
                self.assertEqual(self.rules(self.proofs_with(command)), owed)

    # A brief asks members to bound their proofs with `timeout 600`: timeout gives back the proof's own exit code (124
    # when the time ends first, a failure either way), so each proof counts behind it, alone or after a cd. A timeout
    # of another program, one with an option or a duration it does not take, and any other wrapper still prove nothing
    @unittest.skipUnless(TIMEOUT, "the gates trust timeout only from /usr/bin, /usr/local/bin or /opt/homebrew/bin")
    def test_each_proof_counts_behind_a_timeout_and_other_wrappers_do_not(self):
        at = shlex.quote(str(self.repo))
        proofs = {"dev test": "dev test run tests/test_calc.py", "dev test prove": "dev test prove tests/test_calc.py",
                  "dev progress step": 'dev progress step "built it" --check "✓ 12 tests pass"'}
        for wrap, counts in (("timeout 600 {}", True), (f"cd {at} && timeout 600 {{}}", True),
                             ("timeout -k 10 --preserve-status 10m {}", True), ("timeout --signal=TERM 1.5h {}", True),
                             ("timeout 600 env X=1 {}", False), ("timeout soon {}", False),
                             ("timeout --unknown 600 {}", False), ("nice timeout 600 {}", False),
                             ("timeout 600 sh -c '{}'", False)):
            with self.subTest(wrap=wrap):
                self.tx = Transcript(Path(self.tmp.name) / f"agent-timeout-{abs(hash(wrap))}.jsonl")
                self.write("calc.py", "def add(a, b):\n    return a + b\n")
                self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
                for command in proofs.values():
                    self.tx.bash(wrap.format(command))
                found = gates.crew_gates(KID_DOCS, self.tx.save(), str(self.repo))
                owed = [f["item"] for f in found if f["rule"] == "proofs"]
                self.assertEqual(owed, [] if counts else list(proofs))

    def test_a_proof_piped_into_a_read_only_filter_counts_as_run(self):
        for command in ("dev test 2>&1 | tail -40", "dev test | head -5", "dev test 2>&1 | grep -c passed"):
            with self.subTest(command=command):
                self.tx = Transcript(Path(self.tmp.name) / "agent-piped.jsonl")
                self.assertEqual(self.proofs_with(command), [])
        self.tx = Transcript(Path(self.tmp.name) / "agent-sh.jsonl")
        self.assertEqual(self.rules(self.proofs_with("dev test | sh")), [("proofs", "dev test")])

    def test_a_positive_report_with_a_piped_proof_is_sent_back_to_run_it_alone(self):
        found = self.proofs_with("dev test 2>&1 | tail -40", successful=True)
        self.assertEqual(self.rules(found), [("proofs", "dev test")])
        self.assertIn("piped into `tail -40`", found[0]["evidence"])
        self.assertIn("run `dev test` alone", found[0]["fix"])
        self.tx = Transcript(Path(self.tmp.name) / "agent-alone.jsonl")
        self.assertEqual(self.proofs_with("dev test 2>&1", successful=True), [])  # joined stderr keeps its exit code

    def test_a_proof_run_through_the_python_interpreter_counts(self):
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash("python3 bin/dev test")
        self.tx.bash(f'/usr/bin/python3 -u {shlex.quote(str(core.DEVKIT_HOME / "bin/dev"))} '
                     'progress step "built it" --check "✓ 12 tests pass"')
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

    def test_two_hook_refusals_without_a_lesson_ask_for_one(self):
        self.complete()
        self.tx.refused("git worktree add ../copy")
        self.tx.bash("false", "Error: Exit code 1", is_error=True)  # a failing command is not a refusal
        self.assertEqual(self.gates(), [])  # one refusal is not a pattern yet
        self.tx.refused("git worktree add ../copy2")
        found = self.gates()
        self.assertEqual([f["rule"] for f in found], ["lessons"])
        self.assertIn("2 hook refusals", found[0]["evidence"])
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


class CommandProofTests(unittest.TestCase):
    """A critique's `dev visual report` is owed only by a member that captured with `dev visual`."""
    CRITIQUE = {"phase": "Critique", "do": "critique", "devkit": ["dev visual capture", "dev visual report"],
                "proof": "dev visual report"}

    def test_a_design_step_that_ran_no_dev_visual_owes_no_report(self):
        ran = [["dev", "plan", "new", "flow"], ["dev", "progress", "step", "designed"]]
        self.assertFalse(gates._proof_owed(self.CRITIQUE, ["docs/flow.md"], ran))
        self.assertFalse(gates._phase_applied(self.CRITIQUE, ran, ["docs/flow.md"]))

    def test_a_member_that_captured_owes_the_report(self):
        ran = [["dev", "visual", "capture", "http://127.0.0.1:8000/"]]
        self.assertTrue(gates._proof_owed(self.CRITIQUE, [], ran))
        self.assertTrue(gates._phase_applied(self.CRITIQUE, ran, []))

    def test_other_proofs_keep_their_rules(self):
        self.assertTrue(gates._proof_owed({"proof": "dev progress step"}, [], []))
        self.assertFalse(gates._proof_owed({"proof": "dev docs check"}, ["lib/x.py"], []))


class PipedStepRecordTests(GateCase):
    # `dev progress step … | tail -5` hides its exit code behind the filter's, but the step it records is in the
    # member's lane of the run: that record, with the call's own text, is the proof, so a positive report is not sent
    # back to record it again.
    # A pipe that recorded nothing, and a piped `dev test`, whose exit code is the only record, still are.
    def setUp(self):
        super().setUp()
        runs = Path(self.tmp.name) / "runs"
        runs.mkdir()
        for p in (mock.patch.object(pg, "RUNS", runs), mock.patch.object(pg, "CALLS", Path(self.tmp.name) / "calls")):
            p.start()
            self.addCleanup(p.stop)
        self.run_file = runs / "claude-s1.json"

    def lane(self, *updates):
        self.run_file.write_text(json.dumps(
            {"v": 2, "agent": "claude", "session": "s1", "project": self.repo.name, "goal": "fix it",
             "agents": {"a_kid": {"persona": "Kid", "label": "Kid: fix it", "state": "running",
                                  "updates": list(updates)}}}))

    def step_proofs(self, step_command, test_command="dev test"):
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash(test_command)
        self.tx.bash(step_command)
        found = gates.crew_gates(KID, self.tx.save(), str(self.repo), session="s1", lane="a_kid", successful=True)
        return [f for f in found if f["rule"] == "proofs"]

    def test_a_piped_step_counts_only_when_the_lane_recorded_it(self):
        self.lane({"ts": int(time.time()), "kind": "step", "text": "built it", "check": "✓ 12 tests pass"})
        self.assertEqual(self.step_proofs('dev progress step "built it" --check "✓ 12 tests pass" 2>&1 | tail -5'), [])
        self.tx = Transcript(Path(self.tmp.name) / "agent-not-recorded.jsonl")
        self.lane({"ts": int(time.time()), "kind": "note", "text": "half way", "check": ""})
        found = self.step_proofs('dev progress step "built it" --check "✓ 12 tests pass" | head -3')
        self.assertEqual(self.rules(found), [("proofs", "dev progress step")])
        self.assertIn("piped into `head -3`", found[0]["evidence"])
        self.tx = Transcript(Path(self.tmp.name) / "agent-test-piped.jsonl")
        self.lane({"ts": int(time.time()), "kind": "step", "text": "built it", "check": "✓ 12 tests pass"})
        found = self.step_proofs('dev progress step "built it" --check "✓ 12 tests pass"', "dev test 2>&1 | tail -40")
        self.assertEqual(self.rules(found), [("proofs", "dev test")])

    def test_a_piped_step_counts_only_for_the_step_its_text_names(self):
        # an earlier ✓ step of the lane does not stand in for a later piped one that recorded nothing (refused)
        self.lane({"ts": int(time.time()), "kind": "step", "text": "built it", "check": "✓ 12 tests pass"})
        found = self.step_proofs('dev progress step "documented it" --check "✓ docs check clean" | tail -5')
        self.assertEqual(self.rules(found), [("proofs", "dev progress step")])
        self.assertIn("piped into `tail -5`", found[0]["evidence"])
        self.tx = Transcript(Path(self.tmp.name) / "agent-named-step.jsonl")
        found = self.step_proofs('dev progress step --step fix-it "built it" --check "✓ 12 tests pass" | tail -5')
        self.assertEqual(found, [])  # the text after its options is the step the lane recorded


class SessionOutsideTheRepositoryTests(GateCase):
    # The session started in the home folder (Claude Desktop, a terminal opened in ~): the member's shell starts there
    # and its stop names that folder, while its run belongs to the repository (the project id under the workspace).
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        runs = Path(self.tmp.name) / "runs"
        runs.mkdir()
        for p in (mock.patch.object(pg, "RUNS", runs), mock.patch.object(pg, "CALLS", Path(self.tmp.name) / "calls"),
                  mock.patch.object(lanescope, "DEV", self.tmp.name)):
            p.start()
            self.addCleanup(p.stop)
        (runs / "claude-s-home.json").write_text(json.dumps(
            {"v": 2, "agent": "claude", "session": "s-home", "project": self.repo.name, "goal": "fix it",
             "agents": {"a_kid": {"persona": "Kid", "label": "Kid: fix it", "state": "running"}}}))

    def proofs_from_home(self, test_command, output="ok"):
        # Kid changes code, loads its skills, runs `test_command` (which prints `output`) and hands off from the home
        # folder; the proofs findings that remain.
        self.write("calc.py", "def add(a, b):\n    return a + b\n")
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash(test_command, output)
        self.tx.bash('dev progress step "built it" --check "✓ 12 tests pass"')
        found = gates.crew_gates(KID, self.tx.save(), str(self.home), session="s-home", lane="a_kid")
        return self.rules([f for f in found if f["rule"] == "proofs"])

    def test_a_proof_run_in_the_runs_repository_counts_and_another_repository_does_not(self):
        tree = Path(self.tmp.name) / "worktrees" / "feat-x"
        git(self.repo, "worktree", "add", "-q", "-b", "feat/x", str(tree))
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        for folder, owed in ((self.repo, []), (self.repo / "bin", []), (tree, []),
                             (other, [("proofs", "dev test")])):
            with self.subTest(folder=folder.name):
                self.tx = Transcript(Path(self.tmp.name) / f"agent-home-{folder.name}.jsonl")
                self.assertEqual(self.proofs_from_home(f"cd {shlex.quote(str(folder))} && dev test"), owed)

    def test_a_proof_of_the_runs_repository_named_with_its_project_counts_and_another_tree_does_not(self):
        # `dev test -p <project> --json` from the home folder: its result names the tree it tested
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        git(other, "init", "-q", "-b", "main")
        for top, owed in ((self.repo, []), (other, [("proofs", "dev test")])):
            with self.subTest(top=top.name):
                self.tx = Transcript(Path(self.tmp.name) / f"agent-project-{top.name}.jsonl")
                result = json.dumps({"top": str(top), "steps": [{"runs": [{"passed": 3, "failed": 0}]}]})
                self.assertEqual(self.proofs_from_home(f"dev test -p {self.repo.name} --json", result), owed)

    def from_home(self):
        # Kid loads its skills, runs its proof in its repository and hands off from the home folder; every finding.
        self.tx.skill("superpowers:test-driven-development").skill("testing").skill("communicating")
        self.tx.bash(f"cd {shlex.quote(str(self.repo))} && dev test")
        self.tx.bash('dev progress step "built it" --check "✓ 12 tests pass"')
        return gates.crew_gates(KID, self.tx.save(), str(self.home), session="s-home", lane="a_kid")

    def test_unfinished_work_left_in_the_runs_repository_is_found(self):
        stub = "def add(a, b):\n    return a + b  # TODO handle overflow\n"
        self.write("calc.py", stub)
        self.assertEqual(self.rules(self.from_home()), [("stubs", "calc.py")])
        # written through the shell from the repository's folder: no Write or Edit call names it
        self.tx = Transcript(Path(self.tmp.name) / "agent-home-shell.jsonl")
        (self.repo / "util.py").write_text(stub)
        self.tx.bash(f"cd {shlex.quote(str(self.repo))} && cat > util.py <<'EOF'\n{stub}EOF")
        self.assertEqual(self.rules(self.from_home()), [("stubs", "util.py")])

    def test_a_test_that_cannot_fail_left_in_the_runs_repository_is_found(self):
        self.write("tests/test_calc.py", "import unittest\n\n\nclass AddTests(unittest.TestCase):\n"
                                         "    def test_adds(self):\n        self.assertTrue(True)\n")
        found = self.from_home()
        self.assertEqual(self.rules(found), [("tests", "tests/test_calc.py")])
        self.assertIn("test_adds", found[0]["evidence"])


class LessonOutputTimeTests(unittest.TestCase):
    # a member's shell output is read for a recorded lesson at every SubagentStop: a long run of blank lines in it
    # must not stall the gate (lesson L-g8lywzsgclv)
    def test_a_lesson_after_many_blank_lines_is_found_in_bounded_time(self):
        with tempfile.TemporaryDirectory() as d:
            tx = Transcript(Path(d) / "member.jsonl")
            tx.bash("dev test", "\n" * 30000 + "✓ 12 test(s) passed")  # no lesson: every line start is tried
            tx.bash("dev lesson add x", "\n" * 3 + "✓ L-abcdef1234 recorded in acme/api → lessons/L-abcdef1234.md")
            path = tx.save()
            t0 = time.perf_counter()
            self.assertEqual(gates._lessons_done(path), ["dev lesson add x"])
            self.assertLess(time.perf_counter() - t0, 0.5)


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
        return lessons.add("global", title, happened="the hook refused it", cause="no managed worktree",
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

    def test_a_bug_report_that_names_no_refusal_answers_nothing(self):
        # devkit's own hook refuses nothing, so a bug report answers the refusals only by the marker one names
        self.tx.refused("git worktree add ../copy").refused("git worktree add ../copy2")
        self.tx.bash('bin/dev bug report --title "the status line is empty"')
        self.tx.bash("bin/dev bug report --guard check_git:worktree")
        self.assertEqual(self.owed(), ["lessons"])

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
        lessons.add("initiatives/platform", "Create worktrees with dev wt new", happened="the hook refused it",
                    cause="no managed worktree", prevention="use dev wt new", errors=["would create an unmanaged worktree"],
                    a={"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"})
        self.assertEqual(self.owed(), [])

    def test_a_refusal_that_names_an_open_lesson_owes_none_and_one_that_only_quotes_its_id_still_does(self):
        les = self.lesson("Create worktrees with dev wt new", commands=["git worktree add"])
        lid = les["meta"]["id"]
        rule = (f"PreToolUse:Bash hook error: devkit · lesson rule LR-0123456789 refuses this call, from lesson "
                f"{lid}: Create worktrees with dev wt new — prevention: use dev wt new.")
        pitfall = (f"{DENIAL}\n\nLessons are advice from past work; they never override the developer.\n"
                   f"- Lesson {lid} (recorded by Backend engineer (Tank), 2026-11-08): Create worktrees with dev wt "
                   f"new — prevention: use dev wt new")
        quoted = f"PreToolUse:Bash hook error: `dev lesson release {lid}` lets a held lesson reach every agent again."
        self.tx.refused("git worktree add ../copy")
        self.tx.bash(f"dev lesson release {lid}", quoted, is_error=True)
        self.tx.bash("git worktree add ../copy2", rule, is_error=True)
        self.tx.bash("git worktree add ../copy3", pitfall, is_error=True)
        found = gates.crew_gates(KID, self.tx.save(), str(self.repo), session="s1")
        self.assertEqual([f["rule"] for f in found], ["lessons"])
        self.assertIn("2 hook refusals", found[0]["evidence"])  # the plain one and the one that only quotes the id
        lessons.retire(les, "dev wt new is gone", a={"user": "ana", "host": "mbp1", "agent": "human"})
        found = gates.crew_gates(KID, self.tx.save(), str(self.repo), session="s1")
        self.assertIn("4 hook refusals", found[0]["evidence"])


class CiProofTests(GateCase):
    # `dev ci --run` is the one proof the tech lead may meet for a member whose brief or project forbids the run: a
    # waiver line in the tech lead's brief, or a passed `dev ci --run` on exactly the tree the member leaves. The
    # member's own `dev ci --mark-passed` attests a run, so it never counts. Without either, the gate refuses as before.
    def setUp(self):
        super().setUp()
        p = mock.patch.object(core, "STATE", Path(self.tmp.name) / "state")  # where local CI evidence is written
        p.start()
        self.addCleanup(p.stop)
        self.stops = 0
        self.tx.skill("communicating").bash('dev progress step "release branch ready" --check "dev test: 40 passed"')

    def ci_gate(self):
        return self.rules(gates.crew_gates(LINK, self.tx.save(), str(self.repo)))

    def stop(self, lead="Release engineer (Link)"):
        # the SubagentStop entry as the hook calls it; `lead` is the name devkit's launch records give the member (a
        # member the main session launched), and each call is a fresh stop: None lets it through, else it is sent back
        self.stops += 1
        data = {"agent_id": f"a{self.stops}", "agent_transcript_path": self.tx.save(), "cwd": str(self.repo)}
        return gates.check("Link", data, {}, "owner/repo", roster={"members": [LINK]}, lead_name=lead)

    def dev_ci(self, *argv):
        # `dev ci …` as the member's shell runs it in the repository: its exit code and what it printed
        out, here = io.StringIO(), os.getcwd()
        os.chdir(self.repo)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                rc = cli.main(["ci", *argv])
        finally:
            os.chdir(here)
        return rc, out.getvalue()

    def evidence(self, how):
        # the marker `dev ci --run` ("run") or `dev ci --mark-passed` ("marked") records for the tree as it is now
        return gitx.record_evidence(gitx.ctx(str(self.repo)), how, gitx.work_tree(str(self.repo)))

    def test_only_a_waiver_line_with_its_reason_in_the_brief_meets_the_ci_proof(self):
        for line in ("Do not run `dev ci --run` yourself.",  # a request in prose, not a waiver
                     "Waived proof: `dev ci --run` — <why>",  # the template, not a reason
                     "Waived proof: dev ci --run — shared",  # too short to say why
                     "Waived proof: `dev ci --run --resume` — the shared checkout holds other members' work",
                     "Waived proof: `dev progress step` — the shared checkout holds other members' work"):
            with self.subTest(line=line):
                self.tx.brief(f"## Task\n\nShip the release branch.\n\n- {line}\n")
                out = self.stop()
                self.assertEqual(out["decision"], "block")
                self.assertIn("`dev ci --run`", out["reason"])
        self.tx.brief("## Task\n\nShip the release branch.\n")
        self.tx.bash("cat notes.md", WAIVER)  # the line in a file the member read is not its brief
        self.assertEqual(self.stop()["decision"], "block")
        self.tx.brief(f"## Task\n\nShip the release branch.\n\n- {WAIVER}\n")
        self.assertIsNone(self.stop())

    def test_the_hook_entry_takes_a_waiver_only_from_the_tech_leads_brief_and_audits_it(self):
        self.tx.brief(f"## Task\n\nShip the release branch.\n\n{WAIVER}\n")
        for lead in ("a helper of Release engineer (Link)", ""):  # a member wrote the brief, or no record says who
            with self.subTest(lead=lead):
                self.assertEqual(self.stop(lead)["decision"], "block")
        self.assertIsNone(self.stop("Release engineer (Link)"))
        rec = audit.records(1, action="crew.gate")[-1]
        self.assertEqual(rec["outcome"], "ok")
        self.assertIn("dev ci --run", rec["detail"])
        self.assertIn("the shared checkout holds other members' uncommitted work", rec["detail"])

    def test_a_passed_ci_run_on_exactly_the_tree_the_member_leaves_meets_the_ci_proof_and_its_own_mark_does_not(self):
        note = "pytest -q: 212 passed"
        rc, out = self.dev_ci("--mark-passed", "--note", note)  # the member attests its own run, for this tree
        self.assertEqual(rc, 0, out)
        self.tx.bash(f'dev ci --mark-passed --note "{note}"', out)
        self.assertEqual(self.ci_gate(), [("proofs", "dev ci --run")])
        self.evidence("run")  # the tech lead's passed `dev ci --run` on the tree as it is now
        self.assertEqual(self.ci_gate(), [])
        (self.repo / "README.md").write_text("# repo\n\nChanged after the run.\n")  # the tree is not the one it ran
        self.assertEqual(self.ci_gate(), [("proofs", "dev ci --run")])

    def test_without_either_the_refusal_names_the_three_ways_to_meet_the_ci_proof(self):
        found = gates.crew_gates(LINK, self.tx.save(), str(self.repo))
        self.assertEqual(self.rules(found), [("proofs", "dev ci --run")])
        self.assertIn("no eligible completed execution", found[0]["evidence"])
        fix = found[0]["fix"]
        self.assertTrue(fix.startswith("run `dev ci --run`"), fix)
        self.assertIn("passed `dev ci --run` of their own on exactly the tree you leave", fix)
        self.assertIn("your own `dev ci --mark-passed` does not meet it", fix)
        taught = re.search(r"`(Waived proof: [^`]*)`", fix)
        self.assertTrue(taught, fix)
        self.tx.brief("## Task\n\n- " + taught.group(1).replace("<why>", "the project's runner prep must not run twice"))
        self.assertIsNone(self.stop())  # the line it teaches is one the gate accepts


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
        recs = audit.records(1, action="crew.gate")  # the second stop is judged too, and audited as let through
        self.assertEqual([(r["target"], r["outcome"], (r.get("ref") or {}).get("result")) for r in recs],
                         [("owner/repo", "blocked", None), ("owner/repo", "recorded", "let through")])
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


class PassTokenTests(GateCase):
    # gates.check writes a pass token, the agent id and the time its stop passed, only when it judged a stop and found
    # nothing missing, and a stop never keeps the token an earlier stop of the same agent left: progress approves a
    # review's Check or a member's --approves only with the token of that stop (tests/test_approvals_token.py)
    def check(self, sess, **extra):
        data = {"agent_id": "a1", "agent_transcript_path": self.tx.save(), "cwd": str(self.repo),
                "last_assistant_message": "**[Firefly> DevKit]** repo · `main` · Build\n- **Check:** ✓ passed\n",
                **extra}
        return gates.check("Kid", data, sess, "owner/repo", roster={"members": [KID]})

    def test_a_clean_pass_writes_the_agents_token_with_the_time_it_passed(self):
        self.complete()
        sess = {gates.PASSED: {"a9": 5.0}}  # another lane's pass stays
        before = time.time()
        self.assertIsNone(self.check(sess))
        self.assertEqual(sorted(sess[gates.PASSED]), ["a1", "a9"])
        self.assertTrue(before <= sess[gates.PASSED]["a1"] <= time.time())

    def test_a_send_back_and_its_let_through_write_none_and_drop_an_earlier_token(self):
        self.tx.bash("dev test")  # no dev progress step: the gates send it back
        sess = {gates.PASSED: {"a1": 5.0}}  # an earlier stop of a1 passed
        self.assertEqual(self.check(sess)["decision"], "block")
        self.assertNotIn("a1", sess.get(gates.PASSED) or {})
        self.assertIsNone(self.check(sess, stop_hook_active=True))  # let through, not passed
        self.assertNotIn("a1", sess.get(gates.PASSED) or {})

    def test_a_gate_that_cannot_judge_writes_no_token(self):
        self.complete()
        sess = {gates.PASSED: {"a1": 5.0}}
        with mock.patch.object(gates, "crew_gates", side_effect=RuntimeError("the gates broke")):
            self.assertIsNone(self.check(sess))  # fails open: the member is not held back
        self.assertNotIn("a1", sess.get(gates.PASSED) or {})
        self.assertIsNone(self.check(sess, agent_transcript_path=""))  # no transcript: nothing judged
        self.assertNotIn("a1", sess.get(gates.PASSED) or {})


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

    def test_taskstop_or_killshell_ends_it_unless_a_hook_refused_the_stop(self):
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

    def agent_in_background(self, description, agent_id):
        # An Agent call with run_in_background, as Claude Code answers it: at once, with the agent's id; its end comes
        # as a task notification whose task id is that agent id
        self.tx.call("Agent", f"Async agent launched successfully.\nagentId: {agent_id}\nThe agent is working in the "
                              "background. You will be notified automatically when it completes.",
                     description=description, prompt="Find the callers of lane_state", subagent_type="Explore",
                     run_in_background=True)
        self.tx.records[-1]["toolUseResult"] = {"isAsync": True, "status": "async_launched", "agentId": agent_id,
                                                "description": description}
        return f"toolu_{self.tx.n:04d}"

    def test_a_background_agent_the_member_launched_counts_until_its_notification(self):
        self.complete()
        use = self.agent_in_background("find the callers", "a4e6fec8dd944e821")
        found = self.gates()
        self.assertEqual([(f["rule"], f["task"]) for f in found], [("background", "a4e6fec8dd944e821")])
        self.assertIn("find the callers", found[0]["item"])
        self.tx.notified("a4e6fec8dd944e821", use)
        self.assertEqual(self.gates(), [])

    def test_a_stop_that_failed_ends_the_task_only_when_it_says_the_task_no_longer_runs(self):
        self.complete()
        self.tx.background("bin/dev test", "b7k2m9x1q")
        self.tx.call("TaskStop", "Error: failed to stop task b7k2m9x1q: the process did not answer", True,
                     task_id="b7k2m9x1q")  # it still runs
        self.tx.background("python3 probe.py", "c3p0r2d2x")
        self.tx.call("TaskStop", "No task found with ID: c3p0r2d2x", True, task_id="c3p0r2d2x")
        self.assertEqual(self.rules(self.gates()), [("background", "bin/dev test")])

    def test_a_malformed_record_does_not_turn_off_the_gates(self):
        # one record of an unexpected shape raised inside the gates, which fail open: every gate let the member go
        self.complete()
        self.tx.records += [{"type": "user", "message": "a string, not a message"},
                            {"type": "attachment", "attachment": ["not", "a", "dict"]},
                            {"type": "assistant", "message": {"content": [
                                {"type": "tool_use", "id": "toolu_bad", "name": "Bash", "input": ["ls"]}]}}]
        self.tx.background("npm run watch", "q9w8e7r6t")
        self.assertEqual(self.rules(self.gates()), [("background", "npm run watch")])

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
    # command that changes the team (`dev crew set`) is the tech lead's

    def test_crew_owner_is_a_read_and_crew_set_changes_the_team(self):
        for cmd in ("dev crew owner lib/x.py", "dev crew owner --json lib/x.py", "bin/dev crew owner lib/x.py docs/a.md"):
            with self.subTest(cmd=cmd):
                self.assertEqual(shellparse.crew_kind(cmd.split()[1:]), "read")
        self.assertEqual(shellparse.crew_kind(["crew", "set", "tank", "title", "x", "--why", "y"]), "team")


if __name__ == "__main__":
    unittest.main()
