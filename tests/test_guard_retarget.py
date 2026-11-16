# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""A command that changes where git or gh sends (a remote, a url rewrite, a branch's remote, gh's default repository,
an include of more configuration) and pushes, opens a PR, merges or releases in the same line is refused, in every
mode and whatever grant exists: the guard judges the push by the configuration as it is before the command runs, so
the change would send it somewhere the guard never judged. Run as two commands, the second is judged by the new
configuration."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests.test_auto_scope import OTHER, WorkspaceScopeCase  # noqa: E402
from tests.test_evidence import m  # noqa: E402

VICTIM = "acme-red/victim"  # a repository with no checkout in the workspace
URL = f"https://github.com/{VICTIM}.git"


class RetargetAndDeliver(WorkspaceScopeCase):

    REFUSED = (f"git remote set-url origin {URL} && git push origin feat/x",
               f"git remote set-url --add --push origin {URL}; git push origin feat/x",
               "git config url.https://github.com/acme-red/.pushInsteadOf git@github.com:signature-organization/ "
               "&& git push origin feat/x",
               f"git remote add up {URL} && git push up feat/x",
               f"git config remote.origin.pushurl {URL} && dev push",
               f"git config --add remote.origin.pushurl {URL}; git push",
               "git config set remote.pushDefault up && git push",
               f"git -c remote.origin.pushurl={URL} push origin feat/x",
               f"git --config-env=remote.origin.pushurl=TARGET push origin feat/x",
               f"git remote rename origin old && git remote add origin {URL} && gh pr create --fill",
               f"gh repo set-default {VICTIM} && gh pr merge 12",
               f"git config remote.origin.gh-resolved {VICTIM} && gh pr merge 12",
               "git config --unset branch.feat/x.remote && git push",
               "git branch --set-upstream-to=up/feat/x && git push",
               "git branch -m feat/z && git push",
               "git config include.path /tmp/more.cfg && git push origin feat/x",
               "git config --rename-section remote.origin remote.old && git push origin feat/x",
               f"bash -c 'git remote set-url origin {URL}' && git push origin feat/x",
               f"git remote set-url origin {URL} && dev release --version 26.11.5")

    # reading the configuration, or changing a key that sends nothing elsewhere, next to a push; and a retarget alone
    RUNS = ("git remote -v && git push origin feat/x", "git remote get-url origin; git push origin feat/x",
            "git config --get remote.origin.url && git push origin feat/x",
            "git config remote.origin.url && git push origin feat/x",
            "git config get remote.origin.url && git push origin feat/x", "git config --list && git push origin feat/x",
            "git config user.email t@example.com && git push origin feat/x",
            "git fetch origin && git push origin feat/x", "git branch -vv && git push origin feat/x",
            "gh repo set-default --view && gh pr merge 12", "git checkout -b feat/y && git push -u origin feat/y",
            f"git remote set-url origin {URL}")

    def test_a_retarget_with_a_push_pr_merge_or_release_is_refused_and_a_read_is_not(self):
        for k, cmd in enumerate(self.REFUSED):
            with self.subTest(cmd=cmd):
                denied = self.verdict(cmd, self.other, 2 + k)
                self.assertIsNotNone(denied)
                self.assertIn("separate commands", denied[1])
        for k, cmd in enumerate(self.RUNS, len(self.REFUSED) + 2):
            with self.subTest(cmd=cmd):
                self.assertIsNone(self.verdict(cmd, self.other, k))

    def test_the_refusal_holds_against_a_typed_grant_and_in_guided_mode(self):
        cmd = f"git remote set-url origin {URL} && git push origin feat/x"
        self.fixture.typed(f"grant push feat/x --repo {OTHER}", m(3))
        self.assertIsNotNone(self.verdict(cmd, self.other, 4))
        self.config = {"work": {"mode": "guided"}}  # a branch push is tier 1 there: it needs no grant on its own
        self.assertIsNotNone(self.verdict(cmd, self.other, 5))
        self.assertIsNone(self.verdict("git push origin feat/x", self.other, 6))

    def test_a_crew_member_with_a_step_grant_for_the_push_is_refused_too(self):
        from devkit import guard
        self.config = {"work": {"mode": "guided"}}

        def member(cmd):
            return guard._member_gate(cmd, self.other, "tank", "a_tank", "claude", "s1",
                                      lambda *a: {"id": "g1", "source": "step"}, m(2))
        self.assertIsNone(member("git push origin feat/x"), "the step grant opens the push on its own")
        self.assertIn("separate commands", member(f"git remote set-url origin {URL} && git push origin feat/x") or "")

    def test_a_push_after_config_env_given_as_two_words_is_still_a_push(self):
        # git reads `--config-env <key>=<VAR>` as one option with its value, as it reads `-c <key>=<value>`
        from devkit import guard
        acts = guard.irreversible_actions("git --config-env user.name=HOME push --force origin main", self.other)
        self.assertEqual([(a["action"], a["target"]) for a in acts], [("push-destructive", "main")])
        self.assertIsNotNone(self.verdict("git --config-env user.name=HOME push --force origin main", self.other))


if __name__ == "__main__":
    unittest.main()
