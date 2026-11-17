# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# devkit's own git rules (lib/devkit/gitrules.py): branch names follow `<type>/<ticket>-<topic>` and never name a tool
# or a throwaway marker, a message carries no AI attribution, and devkit's own commit refuses staged secrets. The
# commit rules run with a fake gitleaks that reports what it was asked; the scan itself runs the real gitleaks on a
# temporary repository (RealSecretScanTests), and no other repository is changed.
# Run: python3 -m unittest tests.test_gitrules -v
import contextlib
import io
import os
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

from devkit import core, gitrules, security  # noqa: E402


class BranchNameTests(unittest.TestCase):

    def test_names_that_describe_the_change_pass(self):
        for name in ("feat/invoice-export", "feat/PROJ-142-invoice-export", "fix/login-timeout", "release/26.11.4",
                     "release/v2.3.0", "integration/payments", "hotfix/2.3.1", "chore/deps-bump", "main", "develop",
                     "refs/heads/feat/x", "refs/tags/v1", "HEAD", "feat/claude-desktop-parity",
                     "refactor/agent-runtime"):
            with self.subTest(name=name):
                self.assertIsNone(gitrules.branch_violation(name))

    def test_a_tool_marker_a_random_id_or_another_shape_is_refused_with_the_rename(self):
        cases = {"claude/vigilant-jang-ec71c0": "tool/agent or throwaway marker", "codex/fix-thing": "tool/agent",
                 "worktree-bold-oak-a3f2": "tool/agent", "wip": "tool/agent", "my-branch": "does not follow",
                 "feature/x": "does not follow", "feat/": "does not follow", "feat/vigilant-jang-ec71c0": "random",
                 "feat/Invoice_Export": "lowercase kebab-case", "feat/" + "x" * 60: "too long"}
        for name, why in cases.items():
            with self.subTest(name=name):
                reason = gitrules.branch_violation(name)
                self.assertIn(why, reason or "")
                self.assertIn("git branch -m", reason)

    def test_the_configured_types_and_words_replace_the_defaults(self):
        with mock.patch.dict(core.CFG, {"branches": {"types": ["story"], "forbidden_words": ["bob"]}}):
            self.assertIsNone(gitrules.branch_violation("story/login"))
            self.assertIsNotNone(gitrules.branch_violation("feat/login"))
            self.assertIn("does not follow", gitrules.branch_violation("claude/x"))  # no type, no longer a marker
            self.assertIn("tool/agent", gitrules.branch_violation("bob/x"))


class AttributionTests(unittest.TestCase):

    def test_an_ai_attribution_line_in_any_case_is_named(self):
        for text in ("feat: x\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
                     "x\n\n🤖 Generated with [Claude Code](https://claude.com)", "x\nco-authored-by: CODEX"):
            with self.subTest(text=text):
                self.assertIn("no AI attribution", gitrules.attribution_problem(text) or "")

    def test_a_message_without_one_passes(self):
        for text in ("fix: handle empty input", "docs: say who reviewed it\n\nCo-Authored-By: Ana <ana@example.com>",
                     ""):
            with self.subTest(text=text):
                self.assertIsNone(gitrules.attribution_problem(text))


class CommitTests(unittest.TestCase):

    def test_attribution_is_named_before_any_scan(self):
        with mock.patch.object(security, "staged_secrets", side_effect=AssertionError("scanned")):
            self.assertIn("no AI attribution", gitrules.commit_problem("/repo", "x\n\nCo-Authored-By: Claude"))

    def test_staged_secrets_are_named_with_what_the_commit_will_hold(self):
        asked = []

        def scan(top, adds=None, all_tracked=False, skipped=None):
            asked.append((top, adds, all_tracked))
            return ["config.py:3 aws-access-token"]

        with mock.patch.object(security, "staged_secrets", scan):
            why = gitrules.commit_problem("/repo", "feat: x", all_tracked=True, adds=[("src",)])
        self.assertIn("config.py:3 aws-access-token", why)
        self.assertIn("gitleaks found secret-like content", why)
        self.assertEqual(asked, [("/repo", [["src"]], True)])

    def test_the_refusal_says_to_remove_the_secret_and_leaves_a_false_positive_to_the_developer(self):
        with mock.patch.object(security, "staged_secrets", lambda *a, **k: ["config.py:3 aws-access-token"]):
            why = gitrules.commit_problem("/repo", "feat: x")
        self.assertIn("Remove the secret from the change", why)
        self.assertIn("false positive, tell the developer", why)
        for advice in ("allow", "ignore"):  # no way around the scan is offered to the agent that reads it
            self.assertNotIn(advice, why.lower())

    def test_a_clean_commit_has_no_problem(self):
        with mock.patch.object(security, "staged_secrets", lambda top, adds=None, all_tracked=False, skipped=None: []):
            self.assertIsNone(gitrules.commit_problem("/repo", "feat: x"))


SECRET = 'TOKEN = "ghp_' + "Z8qLm2Vx7Rt4Kp9Wn3Hs6Jd1Fb5Gc0Ya2Ue8" + '"\n'
ALLOW_ALL = '[allowlist]\npaths = [".*"]\n'
IGNORE_FILE = ".gitleaksignore"  # gitleaks' list of findings to ignore, by fingerprint
SECRET_FINGERPRINT = "config.py:github-pat:1\n"  # SECRET's finding in config.py, as gitleaks names a staged one


class RealSecretScanTests(unittest.TestCase):
    # security.staged_secrets with the real gitleaks on a temporary repository: it scans with the rules and the
    # ignore list the repository committed (else gitleaks' own defaults and no ignore list), never with ones the
    # checkout or the environment add; a scan that cannot run says so

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "init")

    def git(self, *args):
        rc, out, err = core.sh(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                                *args])
        self.assertEqual(rc, 0, err)
        return out

    def stage_secret(self):
        (self.repo / "config.py").write_text(SECRET)
        self.git("add", "config.py")

    @unittest.skipUnless(core.have("gitleaks"), "the secret scan needs gitleaks")
    def test_an_untracked_or_changed_gitleaks_toml_never_empties_the_result(self):
        self.stage_secret()
        (self.repo / ".gitleaks.toml").write_text(ALLOW_ALL)  # untracked
        self.assertEqual(security.staged_secrets(str(self.repo)), ["github-pat in config.py:1"])
        self.git("reset", "-q", "config.py")
        (self.repo / ".gitleaks.toml").write_text('title = "widget"\n[extend]\nuseDefault = true\n')
        self.git("add", ".gitleaks.toml")
        self.git("commit", "-q", "-m", "chore: scan rules")
        (self.repo / ".gitleaks.toml").write_text(ALLOW_ALL)  # changed after the commit
        self.git("add", "config.py")
        self.assertEqual(security.staged_secrets(str(self.repo)), ["github-pat in config.py:1"])

    @unittest.skipUnless(core.have("gitleaks"), "the secret scan needs gitleaks")
    def test_the_environment_never_names_the_configuration(self):
        self.stage_secret()
        allow = Path(self.tmp.name) / "allow.toml"
        allow.write_text(ALLOW_ALL)
        with mock.patch.dict(os.environ, {"GITLEAKS_CONFIG": str(allow), "GITLEAKS_CONFIG_TOML": ALLOW_ALL}):
            self.assertEqual(security.staged_secrets(str(self.repo)), ["github-pat in config.py:1"])

    @unittest.skipUnless(core.have("gitleaks"), "the secret scan needs gitleaks")
    def test_the_rules_the_repository_committed_apply(self):
        (self.repo / ".gitleaks.toml").write_text('[extend]\nuseDefault = true\n[allowlist]\npaths = ["^fixtures/"]\n')
        self.git("add", ".gitleaks.toml")
        self.git("commit", "-q", "-m", "chore: scan rules")
        (self.repo / "fixtures").mkdir()
        (self.repo / "fixtures" / "token.py").write_text(SECRET)
        self.git("add", "fixtures/token.py")
        skipped = []
        self.assertEqual(security.staged_secrets(str(self.repo), skipped=skipped), [])
        self.assertEqual(skipped, [])

    @unittest.skipUnless(core.have("gitleaks"), "the secret scan needs gitleaks")
    def test_an_untracked_or_changed_ignore_list_never_hides_a_finding_however_the_commit_stages_it(self):
        ignore, found = self.repo / IGNORE_FILE, ["github-pat in config.py:1"]
        (self.repo / "config.py").write_text(SECRET)
        ignore.write_text(SECRET_FINGERPRINT)
        # untracked, next to a staged secret; then both staged by the command's own `git add`, on a copy of the index
        self.git("add", "config.py")
        self.assertEqual(security.staged_secrets(str(self.repo)), found)
        self.git("reset", "-q", "config.py")
        self.assertEqual(security.staged_secrets(str(self.repo), adds=[["config.py", IGNORE_FILE]]), found)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")  # the real index stays as it was
        # changed after the commit that holds it: staged with the secret, or taken by `git commit -a`
        (self.repo / "config.py").write_text("TOKEN = None\n")
        ignore.write_text("# reviewed false positives\n")
        self.git("add", "config.py", IGNORE_FILE)
        self.git("commit", "-q", "-m", "feat: settings and the ignore list")
        (self.repo / "config.py").write_text(SECRET)
        ignore.write_text(SECRET_FINGERPRINT)
        self.assertEqual(security.staged_secrets(str(self.repo), all_tracked=True), found)
        self.git("add", "config.py", IGNORE_FILE)
        self.assertEqual(security.staged_secrets(str(self.repo)), found)

    @unittest.skipUnless(core.have("gitleaks"), "the secret scan needs gitleaks")
    def test_the_ignore_list_the_repository_committed_applies(self):
        (self.repo / IGNORE_FILE).write_text(SECRET_FINGERPRINT)
        self.git("add", IGNORE_FILE)
        self.git("commit", "-q", "-m", "chore: ignore list")
        (self.repo / IGNORE_FILE).unlink()  # gone from the working tree: the committed copy still applies
        self.stage_secret()
        skipped = []
        self.assertEqual(security.staged_secrets(str(self.repo), skipped=skipped), [])
        self.assertEqual(skipped, [])

    @unittest.skipUnless(core.have("gitleaks"), "the secret scan needs gitleaks")
    def test_a_scan_that_fails_says_it_did_not_run_and_the_commit_goes_on(self):
        (self.repo / ".gitleaks.toml").write_text("this is [[[ not toml\n")
        self.git("add", ".gitleaks.toml")
        self.git("commit", "-q", "-m", "chore: broken scan rules")
        self.stage_secret()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(gitrules.commit_problem(str(self.repo), "feat: x"))
        self.assertIn("the secret scan did not run", err.getvalue())
        self.assertIn("gitleaks failed", err.getvalue())

    def test_a_missing_gitleaks_says_the_scan_did_not_run_and_the_commit_goes_on(self):
        self.stage_secret()
        err = io.StringIO()
        with mock.patch.object(core, "have", side_effect=lambda tool: tool != "gitleaks"), \
                contextlib.redirect_stderr(err):
            self.assertIsNone(gitrules.commit_problem(str(self.repo), "feat: x"))
        self.assertIn("the secret scan did not run", err.getvalue())
        self.assertIn("gitleaks is not installed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
