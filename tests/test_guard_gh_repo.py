# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The repository a gh command acts on, read as gh reads it: the last -R or --repo wins, in any spelling (`-R o/r`,
`-Ro/r`, `-R=o/r`, `--repo=o/r`, inside a cluster of short flags, before or after the subcommand); a PR or issue URL
names its own repository; and with neither, gh picks among the checkout's GitHub remotes or the default `gh repo
set-default` chose, so automatic policy covers the command only when every one of them is the repository the guard
classifies. The developer's typed grant still opens it."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devkit import guard  # noqa: E402
from tests.test_auto_scope import OTHER, OWN, WorkspaceScopeCase  # noqa: E402
from tests.test_evidence import m  # noqa: E402

VICTIM = "acme-red/victim"  # a repository with no checkout in the workspace


class GhRepositoryFlag(WorkspaceScopeCase):

    def test_the_last_repository_flag_in_any_spelling_names_the_repository(self):
        for k, cmd in enumerate((f"gh pr merge 12 -R{VICTIM}", f"gh pr merge 12 -R={VICTIM}",
                                 f"gh pr merge 12 -R {OTHER} -R {VICTIM}", f"gh pr merge 12 -R {OTHER} --repo={VICTIM}",
                                 f"gh pr merge 12 -sR {VICTIM}", f"gh pr merge 12 -dsR{VICTIM}",
                                 f"gh pr -R {VICTIM} merge 12", f"gh -R {OTHER} pr merge 12 -R {VICTIM}",
                                 f"gh issue close 3 -R {OTHER} -R{VICTIM}")):
            with self.subTest(cmd=cmd):
                acts = guard.irreversible_actions(cmd, self.own)
                self.assertEqual([a["repo"] for a in acts], [VICTIM], acts)
                self.assertIsNotNone(self.verdict(cmd, self.own, 2 + k))
        # a word that only looks like the flag is another option's value: gh reads the word after --subject as it
        acts = guard.irreversible_actions(f"gh pr merge 12 --subject -R{VICTIM}", self.own)
        self.assertEqual([a["repo"] for a in acts], [OWN])

    def test_the_last_flag_naming_a_workspace_project_runs_with_no_grant(self):
        for k, cmd in enumerate((f"gh pr merge 12 -R {VICTIM} -R {OTHER}", f"gh pr merge 12 -R{OTHER}",
                                 f"gh pr -R {OTHER} merge 12")):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.verdict(cmd, self.own, 2 + k))


class GhUrlSelector(WorkspaceScopeCase):

    def test_a_pr_or_issue_url_names_its_own_repository(self):
        for k, cmd in enumerate((f"gh pr merge https://github.com/{VICTIM}/pull/12",
                                 f"gh pr merge https://github.com/{VICTIM}/pull/12 -R {OTHER}",
                                 f"gh pr close https://github.com/{VICTIM}/pull/12",
                                 f"gh issue close https://github.com/{VICTIM}/issues/3")):
            with self.subTest(cmd=cmd):
                acts = guard.irreversible_actions(cmd, self.other)
                self.assertEqual([a["repo"] for a in acts], [VICTIM], acts)
                self.assertIsNotNone(self.verdict(cmd, self.other, 2 + k))
        # the URL of a workspace project is that project: it runs from another checkout with no grant
        self.assertIsNone(self.verdict(f"gh pr merge https://github.com/{OTHER}/pull/12", self.own, 7))


class GhDefaultRepository(WorkspaceScopeCase):

    def test_the_default_gh_repo_set_default_chose_decides_whether_policy_covers_the_command(self):
        self.git(self.other, "config", "remote.origin.gh-resolved", "base")  # the checkout's own origin
        self.assertIsNone(self.verdict("gh pr merge 12", self.other))
        self.git(self.other, "config", "remote.origin.gh-resolved", OTHER)
        self.assertIsNone(self.verdict("gh pr merge 13", self.other, 3))
        self.git(self.other, "config", "remote.origin.gh-resolved", VICTIM)
        denied = self.verdict("gh pr merge 12", self.other, 4)
        self.assertIsNotNone(denied)
        self.assertIn(VICTIM, denied[1])
        self.assertIn(f"grant merge 12 --repo {OTHER}", denied[1])
        self.fixture.typed(f"grant merge 12 --repo {OTHER}", m(5))
        self.assertIsNone(self.verdict("gh pr merge 12", self.other, 6), "the developer's typed grant opens it")

    def test_a_second_remote_of_another_repository_needs_the_typed_grant(self):
        self.git(self.other, "remote", "add", "upstream", f"https://github.com/{VICTIM}.git")
        for k, cmd in enumerate(("gh pr merge 12", "gh pr create --fill", "gh release create v1.2.0")):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(self.verdict(cmd, self.other, 2 + k))
        self.assertIsNone(self.verdict(f"gh pr merge 12 -R {OTHER}", self.other, 6), "a named repository is exact")

    def test_an_api_endpoint_with_owner_and_repo_placeholders_is_filled_from_that_default(self):
        # gh fills {owner}/{repo} from the repository it picks for the checkout, as for any other command
        self.git(self.other, "config", "remote.origin.gh-resolved", VICTIM)
        for k, cmd in enumerate(("gh api -X POST repos/{owner}/{repo}/labels -f name=x",
                                 "gh api --method DELETE repos/:owner/:repo/labels/x")):
            with self.subTest(cmd=cmd):
                denied = self.verdict(cmd, self.other, 2 + k)
                self.assertIsNotNone(denied)
                self.assertIn(VICTIM, denied[1])
        # an endpoint that names the repository, or -R, is exact: no default enters the judgment
        for k, cmd in enumerate((f"gh api -X POST repos/{OTHER}/labels -f name=x",
                                 "gh api -X POST repos/{owner}/{repo}/labels -f name=x -R " + OTHER), 5):
            with self.subTest(cmd=cmd):
                self.assertNotIn(VICTIM, (self.verdict(cmd, self.other, k) or ("", ""))[1])


if __name__ == "__main__":
    unittest.main()
