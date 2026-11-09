# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# How the guard quotes a value in a refusal: one code span that nothing inside the value can end, so the agent reads
# the value whole and the audit trail masks it as one quote.
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

    def test_a_tilde_is_the_home_folder_only_for_claudes_file_tools(self):
        # Codex's apply_patch and an MCP server may write `<cwd>/~/…`, so their paths are judged both ways: here
        # `<cwd>/~/.claude` leads into devkit's configuration, while ~/.claude is an agent's own folder
        target = Path(core.CONFIG_FILE)
        (self.base / "~").mkdir()
        (self.base / "~" / ".claude").symlink_to(target.parent)
        for tool, inp in self.calls(f"~/.claude/{target.name}"):
            with self.subTest(tool=tool):
                v = guard.check(tool, inp, str(self.base), "codex" if tool == "apply_patch" else "claude")
                if tool in ("apply_patch", "mcp__fs__write_file"):
                    self.assertEqual((v or ("",))[0], "deny", v)
                    self.assertIn("configuration", v[1])
                else:  # Claude's file tools expand it: ~/.claude/config.json, an agent's own folder
                    self.assertIsNone(v)
        for tool, inp in self.calls(f"~/{target.relative_to(core.HOME)}"):
            with self.subTest(tool=tool, expanded=True):  # and the expanded reading still counts for every tool
                v = guard.check(tool, inp, str(self.base), "codex" if tool == "apply_patch" else "claude")
                self.assertEqual((v or ("",))[0], "deny", v)

    def test_a_link_followed_by_dotdot_is_judged_as_written_too(self):
        # `<link>/../x` is `<link's target>/../x` for the system, and `x` beside the link for a program that cleans
        # the path first: a write whose cleaned reading is devkit's configuration is refused
        target = Path(core.CONFIG_FILE)
        (self.base / "a" / "b").mkdir(parents=True)
        target.parent.parent.mkdir(parents=True, exist_ok=True)
        link = target.parent.parent / f"link-{self.base.name}"
        link.symlink_to(self.base / "a" / "b")
        self.addCleanup(link.unlink)
        path = f"{link}/../{target.parent.name}/{target.name}"
        with guard._literal_paths():
            self.assertTrue(core.is_temp(guard.resolve(path, str(self.base))))  # the system's reading: a temp file
        for tool, inp in self.calls(path):
            with self.subTest(tool=tool):
                v = guard.check(tool, inp, str(self.base), "codex" if tool == "apply_patch" else "claude")
                self.assertEqual((v or ("",))[0], "deny", v)
                self.assertIn("configuration", v[1])


class ToolDispatch(unittest.TestCase):
    # A shell tool is always judged as a shell command, and apply_patch by the patch it carries, under any key

    def test_a_shell_tool_is_judged_as_a_shell_command_whatever_it_starts_with_or_holds(self):
        install = guard.deny(guard.INSTALL_REASON)
        for tool in ("Bash", "Monitor", "exec_command"):
            for inp in ({"command": "*** Begin Patch\n*** End Patch\ndev install"},
                        {"command": "dev install", "file_path": "/tmp/x"},
                        {"command": "dev install", "description": "watch", "timeout_ms": 1000}):
                with self.subTest(tool=tool, inp=inp):
                    self.assertEqual(guard.check(tool, inp, str(core.HOME), "claude"), install)

    def test_apply_patch_is_judged_by_the_patch_under_any_key(self):
        patch = f"*** Begin Patch\n*** Add File: {core.CONFIG_FILE}\n+{{}}\n*** End Patch"
        for key in ("input", "patch", "command"):
            with self.subTest(key=key):
                v = guard.check("apply_patch", {key: patch}, str(core.HOME), "codex")
                self.assertEqual((v or ("",))[0], "deny", v)
        self.assertIsNone(guard.check("apply_patch", {"input": "*** Begin Patch\n*** End Patch"}, str(core.HOME),
                                      "codex"))

    def test_a_judgment_that_is_no_result_refuses_instead_of_allowing(self):
        # the hook refuses a call when the guard raises (guard-error); a branch that returns None or False is no
        # verdict the caller may read as allow
        for wrong in (None, False, ("deny", "x")):
            with self.subTest(wrong=wrong), mock.patch.object(guard, "_file_tool_verdict", return_value=wrong):
                with self.assertRaises(TypeError):
                    guard.check("Write", {"file_path": "/tmp/x", "content": ""}, str(core.HOME), "claude")


class DevelopersSetup(unittest.TestCase):
    # `dev setup` and `dev config set agents.*` decide which agents devkit sets up and rewrite their settings: the
    # developer runs them in a terminal of their own. `dev setup --preview` only draws the screens with sample data.

    def verdict(self, cmd):
        out = set()
        for agent in ("claude", "codex"):
            v = guard.check("Bash", {"command": cmd}, str(core.HOME), agent)
            out.add(v[1] if v else "allow")
        return out

    def test_the_setup_runs_only_as_a_preview_for_agents(self):
        for cmd in ("dev setup --preview", "COLUMNS=80 dev setup --preview", "dev setup --help"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.verdict(cmd), {"allow"})
        for cmd in ("dev setup", "dev setup -y", "dev setup --resume", "dev setup prompt --preview",
                    "dev setup --preview --resume", "dev setup commit-template", "COLUMNS=80 dev setup -y"):
            with self.subTest(cmd=cmd):
                (why,) = self.verdict(cmd)
                self.assertIn("own terminal", why)

    def test_agents_may_not_choose_which_agents_devkit_sets_up(self):
        for cmd in ("dev config set progress.max_parallel 6", "dev config set language en-US"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.verdict(cmd), {"allow"})
        for cmd in ("dev config set agents.codex false", "dev config set AGENTS.claude false",
                    "dev config set agents '{\"codex\": false}'", "dev config set agents.codex=false"):
            with self.subTest(cmd=cmd):
                (why,) = self.verdict(cmd)
                self.assertIn("own terminal", why)


if __name__ == "__main__":
    unittest.main()
