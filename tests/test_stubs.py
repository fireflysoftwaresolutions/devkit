# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit: allow-stub-words (fixtures with the markers the detector looks for)
"""Unfinished work: stubs, placeholders and TODOs are reported unless planned and tracked."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import stubs  # noqa: E402


def rules(path, text):
    return [(f["line"], f["rule"]) for f in stubs.scan_text(path, text)]


class DetectTests(unittest.TestCase):
    def test_unfinished_work_is_found(self):
        self.assertEqual(rules("a.py", "x = 1  # TODO handle errors\n"), [(1, "todo")])
        self.assertEqual(rules("a.py", "def f():\n    raise NotImplementedError\n"), [(2, "not-implemented")])
        self.assertEqual(rules("a.ts", "throw new Error('Not implemented');\n"), [(1, "not-implemented")])
        self.assertEqual(rules("A.java", 'throw new UnsupportedOperationException("not implemented");\n'),
                         [(1, "not-implemented")])
        self.assertEqual(rules("a.rs", "fn f() { todo!() }\n"), [(1, "not-implemented")])
        self.assertEqual(rules("a.go", 'panic("TODO")\n'), [(1, "todo")])
        self.assertEqual(rules("a.py", "def f(x):\n    ...\n"), [(2, "empty-body")])
        self.assertEqual(rules("a.py", "return []  # placeholder until the API exists\n"), [(1, "placeholder")])
        self.assertEqual(rules("a.ts", "// stub: returns fixed data\n"), [(1, "placeholder")])
        self.assertEqual(rules("README.md", "Pricing: TBD\n"), [(1, "placeholder")])
        self.assertEqual(rules("guide.md", "## Export\n\nTODO: write this section\n"), [(3, "todo")])
        self.assertEqual(rules("guide.md", "<!-- FIXME: screenshot -->\n"), [(1, "todo")])

    def test_docs_that_talk_about_todos_are_not_unfinished(self):
        self.assertEqual(rules("guide.md", "`dev stubs` finds TODO and FIXME markers in your changes.\n"), [])
        self.assertEqual(rules("guide.md", "Name the task on the line: `TODO(T-…)`.\n"), [])
        self.assertEqual(rules("guide.md", "```python\nx = 1  # TODO example\n```\n"), [])

    def test_planned_and_legitimate_cases_pass(self):
        self.assertEqual(rules("a.py", "x = 1  # TODO(T-fwk6abc123): batch the writes\n"), [])
        self.assertEqual(rules("a.py", "x = 1  # TODO(#42) after the API change\n"), [])
        self.assertEqual(rules("a.py", "x = 1  # devkit: allow-stub vendor SDK has no async API yet\n"), [])
        self.assertEqual(rules("a.py", "class P(Protocol):\n    def f(self) -> int:\n        ...\n"), [])
        self.assertEqual(rules("a.py", "class A(ABC):\n    @abstractmethod\n    def f(self):\n        ...\n"), [])
        self.assertEqual(rules("a.py", "@overload\ndef f(x: int) -> int:\n    ...\n"), [])
        self.assertEqual(rules("a.pyi", "def f(x: int) -> int:\n    ...\n"), [])
        self.assertEqual(rules("a.py", "    def _key(self, key):  # abstract\n        raise NotImplementedError\n"), [])
        self.assertEqual(rules("t.py", 'core.sh = lambda args, **kw: (1, "", "stubbed")\n'), [])  # ** is no comment
        self.assertEqual(rules("t.py", "x = 0  # the 0001-01-01 placeholder is not an end time\n"), [])
        self.assertEqual(rules("a.py", "self.stub = grpc_stub(channel)\n"), [])  # code, not a comment
        self.assertEqual(rules("a.py", "# devkit: allow-stub-words\nx = 1  # TODO\n"), [])  # file pragma

    # a comment's stub word and its time word in separate clauses say two things, not "a stub for now": the line of
    # tests/test_cli_wiring.py says nothing is stubbed. In one clause (a dot inside a name is no boundary) it is found
    def test_a_stub_word_and_a_time_word_in_separate_clauses_are_not_a_stub(self):
        self.assertEqual(rules("t.py", "# (spec F6). Each test drives cli.main as a terminal would, in a temporary "
                                       "state; nothing below the CLI is stubbed\n"), [])
        self.assertEqual(rules("t.py", "# the HTTP client is a fake one; the implementation under test is real\n"), [])
        self.assertEqual(rules("t.py", "# for now cli.main returns a stub\n"), [(1, "placeholder")])
        self.assertEqual(rules("t.py", "x = load()  # stubbed for now, replaced by the client in v2\n"),
                         [(1, "placeholder")])


def git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True)


class ChangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        Path(self.repo, "app.py").write_text("def f():\n    return 1  # TODO old, already committed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_added_lines_count(self):
        Path(self.repo, "app.py").write_text("def f():\n    return 1  # TODO old, already committed\n\n"
                                             "def g():\n    raise NotImplementedError\n")
        found = stubs.scan_changes(self.repo)
        self.assertEqual([(f["file"], f["line"], f["rule"]) for f in found], [("app.py", 5, "not-implemented")])
        Path(self.repo, "new.py").write_text("# FIXME: everything\n")  # untracked files count wholly
        self.assertIn(("new.py", 1), [(f["file"], f["line"]) for f in stubs.scan_changes(self.repo)])

    def test_commit_gate_sees_what_the_command_stages(self):
        Path(self.repo, "new.py").write_text("x = 1  # TODO finish\n")
        self.assertIsNone(stubs.commit_gate(self.repo, "git commit -m wip"))  # nothing staged yet
        self.assertIn("new.py:1", stubs.commit_gate(self.repo, "git add new.py && git commit -m x"))
        git(self.repo, "add", "new.py")
        msg = stubs.commit_gate(self.repo, "git commit -m x")
        self.assertIn("TODO(T-", msg)
        self.assertIn("DEVKIT_ALLOW_STUBS=1", msg)

    def test_stop_gate_reports_the_turns_files(self):
        Path(self.repo, "app.py").write_text("def f():\n    return 1  # TODO old, already committed\n"
                                             "def h():\n    pass  # stub for now\n")
        msg = stubs.stop_gate(self.repo, [os.path.join(self.repo, "app.py")])
        self.assertIn("app.py:4 placeholder", msg)
        self.assertIsNone(stubs.stop_gate(self.repo, []))


class CommitGateHookTests(unittest.TestCase):
    def test_text_about_a_commit_is_not_a_commit(self):
        from devkit import hooks
        data = {"tool_name": "Bash", "cwd": "/tmp", "tool_input": {"command": "cat > notes.md <<'EOF'\nthen git commit\nEOF"}}
        self.assertIsNone(hooks._stub_commit_gate(data))



class MessageTests(unittest.TestCase):
    def test_the_suggested_command_exists(self):
        import re
        import shlex
        from devkit import cli, stubs as S
        many = [{"file": "a.py", "line": n, "rule": "todo", "text": "x"} for n in range(1, 12)]
        cmd = re.search(r"\((dev stubs[^)]*)\)", S.message(many)).group(1)
        a = cli.build_parser().parse_args(shlex.split(cmd)[1:])  # argparse exits on an unknown flag
        self.assertEqual((a.cmd, a.all, a.staged), ("stubs", False, False))  # the changes since HEAD

class BulkRewriteNoteTests(unittest.TestCase):
    """Lesson L-fwyp0n9vzv6: a regex that rewrites files must be followed by reading every changed line."""

    def note(self, cmd, tool="Bash"):
        from devkit import hooks
        return hooks._bulk_rewrite_note({"tool_name": tool, "tool_input": {"command": cmd}})

    def test_regex_rewrites_get_the_review_note(self):
        for cmd in ("python3 - <<'EOF'\nt = re.sub(r'a', 'b', t)\np.write_text(t)\nEOF",
                    "sed -i '' 's/foo/bar/g' lib/a.py", "sed -i.bak -e 's/x/y/' a.txt", "perl -pi -e 's/a/b/' x.md"):
            with self.subTest(cmd=cmd):
                self.assertIn("git diff -U0", self.note(cmd))

    def test_exact_edits_and_reads_get_no_note(self):
        for cmd in ("python3 - <<'EOF'\nassert t.count(old) == 1\np.write_text(t.replace(old, new, 1))\nEOF",
                    "python3 -c \"import re; print(re.sub('a', 'b', 'abc'))\"",  # a regex that writes nothing
                    "sed -n 1,20p lib/a.py", "grep -n sed -i notes.md"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.note(cmd), "")
        self.assertEqual(self.note("sed -i '' 's/a/b/' x", tool="Read"), "")

if __name__ == "__main__":
    unittest.main()
