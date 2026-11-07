# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# How the guard quotes a value in a refusal: one code span that nothing inside the value can end, so the agent reads
# the value whole and the audit trail masks it as one quote (1 of the Code reviewer's fourth review of the masking).
# Run: python3 -m unittest tests.test_guard -v
import ast
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import audit, core, guard  # noqa: E402

GUARD = ROOT / "lib" / "devkit" / "guard.py"
# a str.format field in a template's quote: `{key}`, `dev config {sub}`
_TEMPLATE_FIELD = re.compile(r"`[^`\n]*(?<!\{)\{[A-Za-z_]\w*\}[^`\n]*`")


class RefusalQuotes(unittest.TestCase):

    def test_a_value_quoted_by_the_guard_reads_back_as_one_quote(self):
        # the audit trail reads a quote as a Markdown code span: a value with backticks of its own, at either end or
        # in runs, is still one quote whose text is the value, and the refusal's prose around it stays prose
        for value in ("plain words", "a`b", "`a", "a`", "a``b`c", "```", "reports/`cat globex`/x", " a ", ""):
            with self.subTest(value=value):
                parts = audit._quotes(f"Run {guard._code(value)} now: {guard._code('dev')}.")
                self.assertEqual([p[0] for p in parts], ["prose", "quote", "prose", "quote", "prose"])
                body = parts[1][2]  # Markdown takes one space off each end when both have one
                read = body[1:-1] if body[:1] == body[-1:] == " " and body.strip(" ") else body
                self.assertEqual(read, value or " ")  # an empty value is a span of one space
                self.assertEqual([parts[0][1], parts[2][1], parts[3][2], parts[4][1]], ["Run ", " now: ", "dev", "."])

    def test_the_sed_refusal_quotes_the_script_whole(self):
        # the read-only lane's sed refusal quotes the agent's script, which may hold a backtick
        why = guard._ro_sed(["-n", "s/x/`cat globex-payroll.txt`/w out", "notes.md"])
        quotes = [p for p in audit._quotes(why) if p[0] == "quote"]
        self.assertEqual([q[2].strip(" ") for q in quotes], ["s/x/`cat globex-payroll.txt`/w out"])
        self.assertNotIn("globex", audit._field("guard.deny", "detail", why, []))

    def test_every_refusal_quotes_a_value_through_the_helper(self):
        # a value in a refusal goes through guard._code, never between backticks of its own: in an f-string, a field
        # inside a quote; in a template for str.format (a plain string, or the plain part of an f-string), a field
        # inside a quote
        tree = ast.parse(GUARD.read_text())
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body
                      and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                ticks = 0
                for part in node.values:
                    if isinstance(part, ast.Constant):
                        ticks += str(part.value).count("`")
                    elif ticks % 2:
                        found.append((part.lineno, ast.unparse(part.value)))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                found += [(node.lineno, m.group()) for m in _TEMPLATE_FIELD.finditer(node.value)]
        self.assertEqual(sorted(found), [])


class FileToolPaths(unittest.TestCase):
    # A file tool hands its path to the system as it is, and macOS folders
    # are case-insensitive, so the guard reads a file tool's path the same way

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(core.real(tmp.name))

    def calls(self, path):
        patch = f"*** Begin Patch\n*** Add File: {path}\n+{{}}\n*** End Patch\n"
        return [("Write", {"file_path": path, "content": "{}"}),
                ("Edit", {"file_path": path, "old_string": "a", "new_string": "b"}),
                ("MultiEdit", {"file_path": path, "edits": [{"old_string": "a", "new_string": "b"}]}),
                ("NotebookEdit", {"notebook_path": path, "new_source": "x"}),
                ("apply_patch", {"command": patch}),
                ("mcp__fs__write_file", {"path": path, "content": "{}"})]

    def test_a_dollar_or_a_backtick_in_a_file_tools_path_is_a_character_not_a_variable(self):
        # `/tmp/$X/../../…/.config/devkit/config.json` opens devkit's configuration once a folder `$X` exists (any
        # Write creates it); the guard read `$X` as a variable it cannot know and let every file tool through
        target = Path(core.CONFIG_FILE)
        up = "../" * (len(self.base.parts))  # from <base>/$X up to /
        for word in ("$X", "${HOME}", "`id`", "$(id)"):
            path = f"{self.base}/{word}/{up}{str(target).lstrip('/')}"
            for tool, inp in self.calls(path):
                with self.subTest(word=word, tool=tool):
                    v = guard.check(tool, inp, str(self.base), "codex" if tool == "apply_patch" else "claude")
                    self.assertEqual((v or ("",))[0], "deny", v)
                    self.assertIn("configuration", v[1])
        (self.base / "$L").symlink_to(target.parent)  # a folder named `$L` that leads into the protected one
        v = guard.check("Write", {"file_path": f"{self.base}/$L/{target.name}", "content": "{}"}, str(self.base),
                        "claude")
        self.assertEqual((v or ("",))[0], "deny", v)
        seed = guard.check("Write", {"file_path": f"{self.base}/$X/seed", "content": ""}, str(self.base), "claude")
        self.assertIsNone(seed)  # a temp file whose name holds a `$` stays a temp file

    def test_the_handoff_rule_reads_paths_in_any_case(self):
        # ~/development/.HANDOFF/projects/other/repo.MD is another project's note on macOS: it got ask, and
        # Projects/Other/Repo.md and TASKS.MD were allowed
        handoff = str(core.real(core.HANDOFF))
        folded = handoff.replace("/Development/", "/development/").replace("/.handoff", "/.HANDOFF")
        with mock.patch.object(guard, "current_project", return_value="acme/api"):
            for root in (handoff, folded):
                for rel in ("projects/other/repo.md", "projects/other/repo.MD", "Projects/Other/Repo.md", "TASKS.MD",
                            "Plans/other/x.md", "CREW/crew.json"):
                    with self.subTest(root=root, rel=rel):
                        v = guard.check("Write", {"file_path": f"{root}/{rel}", "content": "x"}, str(self.base),
                                        "claude")
                        self.assertEqual((v or ("",))[0], "deny", v)
                with self.subTest(root=root, own=True):  # the session's own note, in any case
                    self.assertIsNone(guard.check("Write", {"file_path": f"{root}/projects/acme/API.md",
                                                            "content": "x"}, str(self.base), "claude"))


if __name__ == "__main__":
    unittest.main()
