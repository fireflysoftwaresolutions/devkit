# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for devkit. Run: python3 -m unittest discover -s tests -v"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import ui, canary, ci, core, gitrules, registry  # noqa: E402

DEV = core.DEV
HOME = core.HOME
# Hermetic: the GitHub accounts that own top-level folders normally come from `gh`; tests use fixed ones so they run
# the same on a developer's Mac and in CI (no GitHub sign-in there).
OWNER = "acme-test"
_REAL = (core.owners, core.github_user)


def setUpModule():
    core.owners = lambda: {OWNER, "otherorg"}
    core.github_user = lambda: OWNER


def tearDownModule():
    core.owners, core.github_user = _REAL


class TempState:
    """Point devkit's state dir (canary log) at a throwaway folder."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = core.STATE, canary.LOG
        core.STATE = Path(self.tmp.name)
        canary.LOG = core.STATE / "canary.jsonl"
        return self

    def __exit__(self, *exc):
        core.STATE, canary.LOG = self.old
        self.tmp.cleanup()


class BranchPolicyTests(unittest.TestCase):
    def test_names(self):
        good = ["feat/invoice-export", "feat/PROJ-142-invoice-export", "fix/login-timeout", "release/26.11.4",
                "release/v2.3.0", "integration/payments", "hotfix/2.3.1", "chore/deps-bump", "main", "develop",
                "feat/claude-desktop-parity", "refactor/agent-runtime"]
        bad = ["claude/vigilant-jang-ec71c0", "codex/fix-thing", "ai/refactor", "worktree-bold-oak-a3f2", "wip",
               "my-branch", "feature/x", "feat/", "feat/Invoice_Export", "feat/vigilant-jang-ec71c0", "pr13-local",
               "tmp/test"]
        for n in good:
            with self.subTest(good=n):
                self.assertIsNone(gitrules.branch_violation(n))
        for n in bad:
            with self.subTest(bad=n):
                self.assertIsNotNone(gitrules.branch_violation(n))


class CalverTests(unittest.TestCase):
    def test_next_version(self):
        import datetime as dt
        from devkit import cli
        with tempfile.TemporaryDirectory() as d:
            core.sh(["git", "init", "-q", d])
            core.sh(["git", "-C", d, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"])
            self.assertEqual(cli.calver(d, dt.date(2026, 11, 5)), (None, "26.11.0"))
            for t in ("v26.10.4", "v26.11.0", "v26.11.1", "1.2.3"):
                core.sh(["git", "-C", d, "tag", t])
            self.assertEqual(cli.calver(d, dt.date(2026, 11, 20)), ("v26.11.1", "26.11.2"))
            self.assertEqual(cli.calver(d, dt.date(2026, 12, 1)), ("v26.11.1", "26.12.0"))


class PackagingTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_plugin_versions_match(self):
        v = (self.ROOT / "VERSION").read_text().strip()
        for m in ("plugin/.claude-plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
            with self.subTest(manifest=m):
                self.assertEqual(json.loads((self.ROOT / m).read_text())["version"], v)

    def test_marketplaces_point_at_plugin(self):
        cm = json.loads((self.ROOT / ".claude-plugin/marketplace.json").read_text())
        xm = json.loads((self.ROOT / ".agents/plugins/marketplace.json").read_text())
        self.assertEqual(cm["plugins"][0]["source"], "./plugin")
        self.assertEqual(xm["plugins"][0]["source"]["path"], "./plugin")
        for skill in ("handoff", "workspace", "delivery", "backend"):
            self.assertTrue((self.ROOT / "plugin/skills" / skill / "SKILL.md").exists())
        for asset in ("plugin/assets/icon.png", "plugin/assets/logo.png"):
            self.assertTrue((self.ROOT / asset).exists())


class ParsingTests(unittest.TestCase):
    def test_parse_remote(self):
        self.assertEqual(core.parse_remote("https://github.com/Acme/Widget.git"), ("github.com", "Acme", "Widget"))
        self.assertEqual(core.parse_remote("git@github.com:acme/widget.git"), ("github.com", "acme", "widget"))
        self.assertEqual(core.parse_remote("ssh://git@github.com/acme/widget"), ("github.com", "acme", "widget"))
        self.assertEqual(core.parse_remote("acme/widget"), ("github.com", "acme", "widget"))
        self.assertIsNone(core.parse_remote("not a url"))

    def test_other_hosts(self):
        self.assertEqual(core.parse_remote("git@gitlab.com:group/sub/svc.git"), ("gitlab.com", "group/sub", "svc"))
        self.assertEqual(core.parse_remote("https://user@bitbucket.org/ws/repo.git"), ("bitbucket.org", "ws", "repo"))
        self.assertEqual(core.parse_remote("https://bitbucket.acme.io/scm/proj/repo.git"), ("bitbucket.acme.io", "proj", "repo"))
        self.assertEqual(core.parse_remote("https://dev.azure.com/org/project/_git/repo"), ("dev.azure.com", "org/project", "repo"))
        self.assertEqual(core.parse_remote("git@ssh.dev.azure.com:v3/org/project/repo"), ("dev.azure.com", "org/project", "repo"))
        self.assertEqual(core.parse_remote("gitlab.com/group/repo"), ("gitlab.com", "group", "repo"))
        pid = core.project_identity(Path("/x/svc"), "git@gitlab.com:Group/Sub/Svc.git")[0]
        self.assertEqual(pid, "gitlab.com/group/sub/svc")
        self.assertEqual(core.canonical_path(pid), DEV / "gitlab.com" / "group" / "sub" / "svc")
        self.assertEqual(core.clone_source("gitlab.com/group/repo")[1], "https://gitlab.com/group/repo.git")

    def test_project_identity_and_canonical(self):
        pid, owner, repo = core.project_identity(Path("/x/Widget"), "https://github.com/Acme/Widget.git")
        self.assertEqual(pid, "acme/widget")
        self.assertEqual(core.canonical_path(pid), DEV / "acme" / "widget")
        self.assertEqual(core.project_identity(Path("/x/Thing"), "")[0], "_local/Thing")

    def test_parse_status(self):
        s = registry.parse_status("## feat/a.b...origin/feat/a.b [ahead 2, behind 1]\n M x\n?? y\n?? z")
        self.assertEqual((s["branch"], s["upstream"], s["ahead"], s["behind"], s["modified"], s["untracked"]),
                         ("feat/a.b", "origin/feat/a.b", 2, 1, 1, 2))
        self.assertIsNone(registry.parse_status("## HEAD (no branch)")["branch"])
        self.assertEqual(registry.parse_status("## No commits yet on main")["branch"], "main")
        self.assertTrue(registry.parse_status("## x...origin/x [gone]")["upstream_gone"])


class CiTests(unittest.TestCase):
    WF = """name: CI
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    name: Tests
    runs-on: ubuntu-latest
    strategy:
      matrix:
        node: [20, 22]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ matrix.node }}
      - name: Install
        run: npm ci
      - name: Check
        run: |
          npm run lint
          npm test -- --coverage
"""

    def test_parse(self):
        wf = ci.parse_workflow(self.WF)
        self.assertEqual(wf["name"], "CI")
        self.assertIn("push", wf["on"])
        self.assertIn("pull_request", wf["on"])
        job = wf["jobs"][0]
        self.assertEqual((job["id"], job["name"], job["runs_on"]), ("test", "Tests", "ubuntu-latest"))
        self.assertTrue(any("node" in m for m in job["matrix"]))
        runs = [s["run"] for s in job["steps"] if s["run"]]
        self.assertEqual(runs, ["npm ci", "npm run lint\nnpm test -- --coverage"])

    def test_local_commands(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".github" / "workflows").mkdir(parents=True)
            (Path(d) / ".github" / "workflows" / "ci.yml").write_text(self.WF)
            self.assertEqual(ci.local_commands(Path(d))[0], "npm ci")

    def test_local_commands_leave_out_runner_only_setup(self):
        # what `dev ci --commands` lists is what a developer runs here; a hosted runner's own setup is not
        prep = ("      - name: Free disk space\n        run: sudo rm -rf /usr/share/dotnet /opt/ghc\n"
                "      - run: npx playwright install --with-deps chromium\n        working-directory: studio\n"
                "      - run: apt-get update && apt-get install -y libsecret-tools\n"
                "      - run: git config --global core.autocrlf false\n")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".github" / "workflows").mkdir(parents=True)
            (Path(d) / ".github" / "workflows" / "ci.yml").write_text(
                self.WF.replace("      - name: Install\n", prep + "      - name: Install\n"))
            self.assertEqual(ci.local_commands(Path(d)), ["npm ci", "npm run lint\nnpm test -- --coverage"])


class LocalScanCommandTests(unittest.TestCase):
    """`dev ci --commands` and the delivery test gate keep a scan whose step also installs it."""

    def test_local_commands_keep_a_scan_without_the_setup_in_its_step(self):
        steps = ("      - name: Gitleaks\n        run: brew install gitleaks && gitleaks detect --no-git --exit-code 1\n"
                 "      - name: Trivy\n        run: |\n          sudo apt-get install -y trivy\n          trivy fs .\n"
                 "      - name: Root scan\n        run: sudo -E env PATH=\"$PATH\" ./scripts/security-scan.sh\n")
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".github" / "workflows").mkdir(parents=True)
            (Path(d) / ".github" / "workflows" / "ci.yml").write_text(
                CiTests.WF.replace("      - name: Install\n", steps + "      - name: Install\n"))
            self.assertEqual(ci.local_commands(Path(d)), [
                "gitleaks detect --no-git --exit-code 1", "trivy fs .",
                'sudo -E env PATH="$PATH" ./scripts/security-scan.sh', "npm ci", "npm run lint\nnpm test -- --coverage"])


class CanaryTests(unittest.TestCase):
    def test_marker_detection(self):
        self.assertTrue(canary.has_marker("[Firefly> DevKit] kit@main · done"))
        self.assertTrue(canary.has_marker("\n**[Firefly> DevKit]** kit · x"))
        self.assertTrue(canary.has_marker("`[firefly> devkit]` kit"))
        self.assertTrue(canary.has_marker("DevKit · kit · done"))  # legacy marker, still accepted
        self.assertFalse(canary.has_marker("Done. [Firefly> DevKit] later"))
        self.assertFalse(canary.has_marker("[Firefly] DevKit kit"))
        self.assertFalse(canary.has_marker("Here is the fix"))

    def test_status_text_keeps_ids(self):
        self.assertEqual(canary.status_text("DevKit · _local/x_y@feat/a_b · done · `tests` ok · next: -"),
                         "_local/x_y@feat/a_b · done · tests ok · next: -")
        self.assertEqual(canary.status_text("**DevKit** · api@main · fixed"), "api@main · fixed")
        self.assertEqual(canary.status_text("[Firefly> DevKit] api@main · fixed · tests ok · next: -"),
                         "api@main · fixed · tests ok · next: -")
        self.assertEqual(canary.status_text("**[Firefly> DevKit]** · api@main · fixed"), "api@main · fixed")

    def test_canary_miss_reason_names_what_came_first(self):
        late = "Both runtimes are fine.\n\n**[Firefly> DevKit]** x · `main` · y\n- **Done:** a\n- **Check:** ✓ b\n- **Next:** c"
        self.assertFalse(canary.has_marker(late))
        self.assertIn("1 line(s) come before it", canary.miss_reason(late))
        self.assertIn('"Both runtimes are fine."', canary.miss_reason(late))
        self.assertIn("no status block", canary.miss_reason("Waiting for the build."))

    def test_canary_counts_by_surface(self):
        import time as _t
        with TempState():
            canary.LOG.parent.mkdir(parents=True, exist_ok=True)
            now = int(_t.time())
            canary.LOG.write_text("".join(
                '{"ts": %d, "agent": "claude", "ok": %s, "surface": "%s"}\n' % (now, ok, sf)
                for ok, sf in (("true", "terminal"), ("true", "terminal"), ("false", "Claude Desktop"))))
            self.assertEqual(canary.by_surface(1), {("claude", "terminal"): (2, 2), ("claude", "Claude Desktop"): (0, 1)})
            self.assertIn("Claude Desktop 0/1", canary.summary_line(1))

    def test_surface_detection(self):
        import os as _os
        saved = _os.environ.get("CLAUDE_CODE_ENTRYPOINT")
        try:
            _os.environ["CLAUDE_CODE_ENTRYPOINT"] = "claude-desktop"
            self.assertEqual(canary.surface("claude"), "Claude Desktop")
        finally:
            if saved is None:
                _os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)
            else:
                _os.environ["CLAUDE_CODE_ENTRYPOINT"] = saved
        with tempfile.TemporaryDirectory() as d:
            tp = Path(d) / "t.jsonl"
            tp.write_text('{"type": "session_meta", "payload": {"originator": "codex_exec"}}\n')
            self.assertEqual(canary.surface("codex", {"transcript_path": str(tp)}), "codex exec")
            self.assertEqual(canary.surface("codex", {}), "unknown")

    def test_canary_corrections_are_counted_apart(self):
        with TempState():
            canary.LOG.parent.mkdir(parents=True, exist_ok=True)
            canary.LOG.write_text('{"ts": %d, "agent": "claude", "ok": false}\n' % int(__import__("time").time()))
            canary.record_corrected("claude", "s1", "acme/api", "x · main ❯ fixed")
            self.assertEqual(canary.summary(1), {"claude": (0, 1)})
            self.assertEqual(canary.corrected(1), 1)
            self.assertIn("1 corrected after a reminder", canary.summary_line(1))

    def test_canary_status_block(self):
        block = ("**[Firefly> DevKit]** api · `feat/x_y` · Invoice export\n"
                 "- **Done:** added the CSV export endpoint.\n"
                 "- **Check:** ✓ 12 tests pass\n"
                 "- **Progress:** 2 of 3 steps · 20 min of ~30\n"
                 "- **Next:** open the PR.\n\nDetails follow.")
        self.assertTrue(canary.has_marker(block))
        p = canary.parse_status(block)
        self.assertEqual((p["project"], p["branch"], p["task"]), ("api", "feat/x_y", "Invoice export"))
        self.assertEqual((p["check"], p["next"]), ("✓ 12 tests pass", "open the PR."))
        line = canary.status_text(block)
        self.assertEqual(line, "api · feat/x_y ❯ Invoice export ❯ added the CSV export endpoint. ❯ ✓ 12 tests pass ❯ "
                               "▸ 2 of 3 steps · 20 min of ~30 ❯ next → open the PR.")
        self.assertEqual(canary.format_issues(block), [])
        self.assertIn("Check    ", ui.status_block(line, color=False))
        self.assertIn("\033[48;2;255;179;74m", ui.status_block(line, color=True))
        # the Crew and You bullets get their own rows, one member per line, and long values wrap under their column
        crewed = canary.status_text(block.replace(
            "- **Next:**", "- **Crew:** Backend engineer (Tank) ▸ F2 · Technical writer (Ghost) ▸ tutorials\n"
                           "- **You:** approve the push of the release branch.\n- **Next:**"))
        out = ui.status_block(crewed, color=False, cols=60)
        rows = out.splitlines()
        self.assertEqual(rows[1].split(), ["Done", "added", "the", "CSV", "export", "endpoint."])  # crew not in Done
        crew_at = next(i for i, r in enumerate(rows) if r.strip().startswith("Crew"))
        self.assertIn("Backend engineer (Tank) ▸ F2", rows[crew_at])
        self.assertEqual(rows[crew_at + 1].strip(), "Technical writer (Ghost) ▸ tutorials")
        self.assertIn("You       approve the push of the release branch.", out)
        long = canary.status_text(block.replace("added the CSV export endpoint.", "added " + "the CSV export " * 8))
        wrapped = ui.status_block(long, color=False, cols=60).splitlines()
        self.assertTrue(all(len(r) <= 60 for r in wrapped))
        self.assertTrue(wrapped[2].startswith(" " * 14))  # the continuation lines up under the value column
        # older one-line forms are still understood, but scored as not following the format
        old = "`[Firefly> DevKit]` `api ⎇ main` `T-fwk7a2xyz1` ❯ fixed ❯ ✓ tests ❯ next → push"
        self.assertTrue(canary.has_marker(old))
        self.assertEqual(canary.status_text(old), "api ⎇ main ❯ T-fwk7a2xyz1 ❯ fixed ❯ ✓ tests ❯ next → push")
        self.assertTrue(canary.format_issues(old))
        self.assertEqual(canary.status_text("DevKit · api@main · fixed"), "api@main · fixed")
        bad = canary.format_issues("**[Firefly> DevKit]** api · `main` · x\n- **Done:** see T-fwk7a2xyz1\n- **Check:** ok")
        self.assertTrue(any("Next" in x for x in bad) and any("✓" in x for x in bad) and any("IDs" in x for x in bad))

    def test_check_records(self):
        with TempState():
            cwd = str(DEV)
            self.assertEqual(canary.check("codex", {"cwd": cwd, "last_assistant_message": "[Firefly> DevKit] x@main · ok"})[0], True)
            self.assertEqual(canary.check("codex", {"cwd": cwd, "last_assistant_message": "no marker"})[0], False)
            self.assertIsNone(canary.check("codex", {"cwd": "/tmp", "last_assistant_message": "x"}))
            self.assertEqual(canary.summary(1), {"codex": (1, 2)})

    def test_transcript_reader(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.jsonl"
            lines = [{"type": "user", "message": {"role": "user", "content": "do it"}},
                     {"type": "assistant", "message": {"content": [{"type": "text", "text": "Working"}]}},
                     {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
                     {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
                     {"type": "assistant", "message": {"content": [{"type": "text", "text": "DevKit · x · done"}]}}]
            f.write_text("\n".join(json.dumps(x) for x in lines))
            self.assertEqual(canary.last_assistant_text(str(f)), "DevKit · x · done")


class NoteTests(unittest.TestCase):
    def test_session_line_replaced_by_key(self):
        with tempfile.TemporaryDirectory() as d:
            old = registry.NOTES
            try:
                import devkit.core as c
                c_notes = Path(d)
                registry.NOTES = c_notes
                core.NOTES = c_notes
                pid = "acme/widget"
                f = core.NOTES / "acme" / "widget.md"
                f.parent.mkdir(parents=True)
                f.write_text(registry.NOTE_TEMPLATE.format(title=pid, auto_start=registry.AUTO_START,
                                                           auto_end=registry.AUTO_END, log_header=registry.LOG_HEADER))
                orig = core.note_path
                core.note_path = lambda p: c_notes / (p + ".md")
                registry.note_path = core.note_path
                registry.add_session_line(pid, "line-one-zz", "abc")
                registry.add_session_line(pid, "second", "abc")
                registry.add_session_line(pid, "other", "def")
                text = f.read_text()
                self.assertNotIn("line-one-zz", text)
                self.assertEqual(text.count("<!-- s:abc -->"), 1)
                self.assertLess(text.index("other"), text.index("second"))
                digest = registry.human_sections(pid)
                self.assertNotIn("dev:auto", digest)
                self.assertNotIn("status: active", digest)
                self.assertIn("Recent sessions", digest)
            finally:
                registry.NOTES = old
                core.NOTES = old
                core.note_path = orig
                registry.note_path = orig



class GhUserTests(unittest.TestCase):
    def test_output_that_is_not_a_user_record_means_no_account_known(self):
        from unittest import mock
        for out in ("<html>rate limited</html>", '"just a string"', '["a", "list"]'):
            with self.subTest(out=out), TempState(), mock.patch.object(core, "sh", return_value=(0, out, "")):
                self.assertEqual(core.gh_user(), {})

    def test_a_user_record_is_returned(self):
        from unittest import mock
        out = '{"login": "ada", "id": 1, "name": "Ada", "email": null}'
        with TempState(), mock.patch.object(core, "sh", return_value=(0, out, "")):
            self.assertEqual(core.gh_user()["login"], "ada")


if __name__ == "__main__":
    unittest.main()
