# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""dev docs check never reports success when it cannot read a checker's output (lesson: hidden failures)."""
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, docs, plans, work  # noqa: E402


class ValeOutputTests(unittest.TestCase):
    def run_check(self, stdout, stderr):
        d = tempfile.mkdtemp()
        Path(d, "README.md").write_text("# Title\n\nText.\n")
        Path(d, ".vale.ini").write_text("StylesPath = s\n")
        saved = (core.have, docs.subprocess.run)
        core.have = lambda tool: tool == "vale"

        def fake_run(args, **kw):
            return subprocess.CompletedProcess(args, 1, stdout=stdout, stderr=stderr)
        docs.subprocess.run = fake_run
        try:
            with redirect_stdout(io.StringIO()) as out:
                rc = docs.check(Path(d), prose=True)
        finally:
            core.have, docs.subprocess.run = saved
        return rc, out.getvalue()

    def test_unreadable_output_fails(self):
        rc, out = self.run_check("not json at all", "")
        self.assertNotEqual(rc, 0)
        self.assertIn("error", out)

    def test_skipped_files_fail(self):
        rc, out = self.run_check("{}", "E201 Invalid front matter in plugin/commands/x.md")
        self.assertNotEqual(rc, 0)

    # Vale writes a file it cannot read as a JSON object on stderr. The check printed one line of it, `"Code": "E201",`,
    # without the file or the reason, so people looked for a missing .vale.ini instead of the front matter.
    def test_a_file_vale_stops_on_is_named_with_its_line_and_reason(self):
        stop = json.dumps({"Line": 1, "Path": "plans/acme/x.md", "Code": "E201", "Span": 1,
                           "Text": "yaml: line 2: mapping values are not allowed in this context"}, indent=2)
        rc, out = self.run_check("{}", stop)
        self.assertNotEqual(rc, 0)
        self.assertIn("plans/acme/x.md:1: vale E201: yaml: line 2: mapping values are not allowed in this context", out)
        self.assertNotIn('"Code"', out)

    def test_clean_output_passes(self):
        rc, _ = self.run_check("{}", "")
        self.assertEqual(rc, 0)

    def test_error_alerts_fail(self):
        alert = '{"README.md": [{"Line": 3, "Span": [1, 4], "Severity": "error", "Check": "Google.Latin", ' \
                '"Message": "Use for example"}]}'
        rc, out = self.run_check(alert, "")
        self.assertNotEqual(rc, 0)
        self.assertIn("Google.Latin", out)

    # 192 findings with 22 errors listed 40 warnings and none of the errors: the list was sorted by file and cut at 40
    def test_every_error_is_listed_before_the_warnings_and_left_out_warnings_are_counted(self):
        def alert(line, severity, check):
            return {"Line": line, "Span": [1, 4], "Severity": severity, "Check": check, "Message": "m"}
        data = {"A.md": [alert(n, "warning", "Google.Will") for n in range(1, 61)],
                "B.md": [alert(n, "error", f"Google.Err{n}") for n in range(1, 23)],
                "C.md": [alert(1, "warning", "Google.Late")]}
        rc, out = self.run_check(json.dumps(data), "")
        self.assertNotEqual(rc, 0)
        self.assertIn("83 finding(s), 22 error(s)", out)
        listed = [ln.strip() for ln in out.splitlines() if re.match(r"\s{4}\S+\.md:\d+:\d+: ", ln)]
        errors = [ln for ln in listed if ": error " in ln]
        self.assertEqual(len(errors), 22)
        first_warning = next(k for k, ln in enumerate(listed) if ": warning " in ln)
        self.assertTrue(all(listed.index(e) < first_warning for e in errors), listed[:25])
        self.assertEqual(sum(1 for ln in listed if ": warning " in ln), 40)
        self.assertRegex(out, r"21 more warning\(s\) in 2 file\(s\) not listed: vale --config \S+ "
                              r"--minAlertLevel warning A\.md C\.md")


A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}
# Google's rule that a project slug such as firefly-weave trips, so the check runs offline with no style download
LY_HYPHENS = ("extends: existence\nmessage: \"'%s' doesn't need a hyphen.\"\nlevel: error\nnonword: true\n"
              "tokens:\n  - '\\b[^\\s-]+ly-\\w+'\n")


@unittest.skipUnless(shutil.which("markdownlint-cli2") and shutil.which("vale"),
                     "needs markdownlint-cli2 and vale on PATH: npm i -g markdownlint-cli2; brew install vale")
class PlansFolderTests(unittest.TestCase):
    # `dev docs check` on a handoff plans folder failed the files devkit itself wrote: markdownlint counted the
    # front-matter title of a design note adopted by `dev plan revise` as a second top-level heading (MD025), Vale
    # stopped on a title with a colon (E201: the front matter was not YAML) and checked the front matter's record
    # fields as prose (the project slug in `scope:`).
    def test_a_plan_and_an_adopted_design_note_devkit_wrote_pass(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            styles = root / "styles"
            (styles / "write-good").mkdir(parents=True)
            (styles / "Google").mkdir()
            (styles / "Google" / "LyHyphens.yml").write_text(LY_HYPHENS)
            handoff = root / ".handoff"
            handoff.mkdir()
            saved = (core.HANDOFF, work.HANDOFF, work.ITEMS, plans.PLANS, plans.HANDOFF, docs.VALE_STYLES, docs.VALE_INI)
            core.HANDOFF = work.HANDOFF = plans.HANDOFF = handoff
            work.ITEMS, plans.PLANS = handoff / "items", handoff / "plans"
            docs.VALE_STYLES, docs.VALE_INI = styles, root / "vale.ini"
            try:
                plans.new("acme/firefly-weave", "Bug sweep 26.11.28: the bugs the sessions hit", a=A)
                note = plans.PLANS / "acme" / "firefly-weave" / "design" / "grid" / "README.md"
                note.parent.mkdir(parents=True)
                note.write_text("# Grid mockups\n\nStatic mockups of the grid, for the product owner.\n")
                plans.revise(note, "adopted the design note", a=A)
                with redirect_stdout(io.StringIO()) as out:
                    rc = docs.check(plans.PLANS / "acme" / "firefly-weave")
            finally:
                (core.HANDOFF, work.HANDOFF, work.ITEMS, plans.PLANS, plans.HANDOFF, docs.VALE_STYLES,
                 docs.VALE_INI) = saved
        self.assertIn("Markdown structure · 2 file(s)", out.getvalue())
        self.assertEqual(rc, 0, out.getvalue())


class ValeConfigTests(unittest.TestCase):
    # A plan's record fields (its id, scope, status, who and when) are data, not prose: devkit's Vale configuration
    # leaves each of them out, and keeps the title, which a reader sees, checked.
    def test_every_plan_record_field_but_the_title_is_left_out_of_the_prose_check(self):
        text = (core.DEVKIT_HOME / "config" / "docs" / "vale.ini").read_text()
        core_options = text.split("\n[", 1)[0]  # Vale reads IgnoredScopes only above the first [section]
        line = next(ln for ln in core_options.splitlines() if ln.startswith("IgnoredScopes"))
        scopes = [s.strip() for s in line.split("=", 1)[1].split(",")]
        self.assertTrue({"code", "tt"} <= set(scopes), scopes)  # Vale's own defaults, which the setting replaces

        def ignored(field):
            return any(f"text.frontmatter.{field}".startswith(s) for s in scopes)
        self.assertEqual([f for f in plans.FIELDS if f != "title" and not ignored(f)], [])
        self.assertFalse(ignored("title"))


class ValeConfigPerCopyTests(unittest.TestCase):
    # dev docs check run from a branch checkout rewrote the Vale config under ~/.local/share/devkit/vale with that
    # branch's rules, and every session's docs check, which runs the pinned copy, read them until its next run. Each
    # copy below is a real devkit library in its own folder, run as a child process with HOME in the test's folder.
    SETUP = ("import sys; sys.path.insert(0, sys.argv[1]); from devkit import docs; "
             "ok = docs.ensure_vale(); print(docs.VALE_INI if ok else 'no vale')")

    def copy_of_devkit(self, home: Path, rule: str) -> Path:
        shutil.copytree(core.DEVKIT_HOME / "lib", home / "lib",
                        ignore=shutil.ignore_patterns("__pycache__", "visual", "web", "art"))
        (home / "config" / "docs").mkdir(parents=True)
        template = (core.DEVKIT_HOME / "config" / "docs" / "vale.ini").read_text()
        (home / "config" / "docs" / "vale.ini").write_text(template + rule + "\n")
        return home

    def test_a_checkouts_docs_check_writes_its_own_vale_config_and_leaves_the_installed_copys(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _sandbox
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            user = root / "home"
            styles = user / ".local" / "share" / "devkit" / "vale" / "styles"
            for package in ("Google", "write-good"):  # already synced: no download
                (styles / package).mkdir(parents=True)
            stand_ins = root / "bin"
            stand_ins.mkdir()
            (stand_ins / "vale").write_text("#!/bin/sh\nexit 0\n")
            (stand_ins / "vale").chmod(0o755)
            installed = self.copy_of_devkit(root / "releases" / "26.11.27-b72ab90", "Google.Quotes = NO")
            checkout = self.copy_of_devkit(root / "worktrees" / "fix-docs", "Google.Quotes = YES")
            env = _sandbox.env(stubs=[stand_ins], HOME=user)

            def setup(copy: Path) -> Path:
                r = subprocess.run([sys.executable, "-I", "-c", self.SETUP, str(copy / "lib")], env=env, cwd=root,
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                return Path(r.stdout.strip().splitlines()[-1])

            installed_ini = setup(installed)
            checkout_ini = setup(checkout)
            self.assertNotEqual(installed_ini, checkout_ini)
            self.assertIn("Google.Quotes = NO", installed_ini.read_text())  # the installed copy's rules, as it wrote
            self.assertIn("Google.Quotes = YES", checkout_ini.read_text())
            self.assertIn(f"StylesPath = {styles}", checkout_ini.read_text())  # the style packages stay shared


class SvgTests(unittest.TestCase):
    # The diagram rules (a title for people who cannot see the picture, no scripts) are for the images a reader sees.
    # A test fixture is a deliberately plain image a test loads, so the check fails a page's diagram without a title
    # and passes the same image under a test fixtures folder.
    def test_a_docs_image_without_a_title_fails_and_a_test_fixture_image_does_not(self):
        plain = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><rect width="10" height="10"/></svg>'
        saved = core.have
        core.have = lambda tool: False  # no markdownlint, link checker or vale: only the image rules run
        try:
            # the folders above the checkout never count: a checkout under testdata/ or a managed worktree
            # (.worktrees/) still has its docs images checked
            for checkout, where, failing in (("", "docs/images/a.svg", True), ("", "tests/fixtures/visual/b.svg", False),
                                             ("", "test/fixtures/c.svg", False), ("", "src/__fixtures__/d.svg", False),
                                             ("", "pkg/testdata/e.svg", False), ("testdata/repo", "docs/f.svg", True),
                                             (".worktrees/repo/feat-x", "docs/g.svg", True)):
                with self.subTest(checkout=checkout, where=where), tempfile.TemporaryDirectory() as d:
                    root = Path(d, checkout)
                    root.mkdir(parents=True, exist_ok=True)
                    Path(root, "README.md").write_text("# Title\n\nText.\n")
                    Path(root, where).parent.mkdir(parents=True)
                    Path(root, where).write_text(plain)
                    with redirect_stdout(io.StringIO()) as out:
                        rc = docs.check(root)
                    self.assertEqual((rc != 0, "no <title>" in out.getvalue()), (failing, failing), out.getvalue())
        finally:
            core.have = saved


if __name__ == "__main__":
    unittest.main()
