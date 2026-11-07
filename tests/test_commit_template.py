#!/usr/bin/env python3
# The commit template (spec P-fy3bwa0pshk R9): config/git/commit-template.txt, `dev commit --why/--verified/--refs/
# --breaking`, `Refs:` from the started task's ticket (never its devkit id), and `dev setup`'s offer of the template as
# each repository's commit.template. Every test works on temporary repositories; git's global and system config are
# switched off, so the developer's real git config is never read or written.

import argparse
import contextlib
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cli, core, gitx, registry, setup, work  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "git" / "commit-template.txt"
GITHUB_URL = "https://github.com/acme/widget.git"
PID = "acme/widget"
ISOLATE = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}
ANA = {"user": "ana", "host": "mac", "agent": "human", "session": ""}


def _parser():
    ap = argparse.ArgumentParser(prog="dev")
    sp = ap.add_subparsers(dest="cmd")

    def add(name, fn, help_):
        p = sp.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        return p

    gitx.register(add)
    return ap


PARSER = _parser()


def dev(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            a = PARSER.parse_args([str(x) for x in argv])
        except SystemExit as e:  # an unknown option: argparse's own exit code and message
            return e.code, out.getvalue(), err.getvalue()
        rc = a.fn(a)
    return rc, out.getvalue(), err.getvalue()


def git(cwd, *args):
    rc, out, err = core.sh(["git", "-C", str(cwd), *args], timeout=60)
    if rc != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {err or out}")
    return out


class Case(unittest.TestCase):
    # A repository `widget` with a local bare origin, devkit's state and the handoff in a temp folder.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.saved = (core.STATE, core.HANDOFF, work.HANDOFF, work.ITEMS, setup.STATE,
                      {k: core.GIT_ENV.get(k) for k in ISOLATE}, os.getcwd())
        core.STATE = setup.STATE = self.root / "state"
        core.HANDOFF = work.HANDOFF = self.root / "handoff"
        work.ITEMS = work.HANDOFF / "items"
        core.GIT_ENV.update(ISOLATE)
        origin = self.root / "origin.git"
        git(self.root, "init", "-q", "--bare", "-b", "main", str(origin))
        self.repo = self.repository("widget")
        git(self.repo, "remote", "add", "origin", GITHUB_URL)
        git(self.repo, "config", f"url.{origin}.insteadOf", GITHUB_URL)
        git(self.repo, "push", "-q", "-u", "origin", "main")
        git(self.repo, "remote", "set-head", "origin", "main")
        os.chdir(self.repo)

    def tearDown(self):
        core.STATE, core.HANDOFF, work.HANDOFF, work.ITEMS, setup.STATE, env, cwd = self.saved
        for k, v in env.items():
            if v is None:
                core.GIT_ENV.pop(k, None)
            else:
                core.GIT_ENV[k] = v
        os.chdir(cwd)
        self.tmp.cleanup()

    def repository(self, name):
        repo = self.root / name
        git(self.root, "init", "-q", "-b", "main", str(repo))
        (repo / "README.md").write_text(f"# {name}\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: start")
        return repo

    def change(self, branch="feat/parser"):
        git(self.repo, "switch", "-q", "-c", branch)
        (self.repo / "parser.py").write_text(f"# {time.time_ns()}\n")

    def message(self):
        return git(self.repo, "log", "-1", "--format=%B")

    def task(self, title, links, branch):
        it = work.create(PID, title, why="the developer asked", a=ANA, links=links)
        return work.claim(it, a=ANA, branch=branch)  # what `dev task start` does on that branch


# --------------------------------------------------------------------------- the template file


class TemplateFileTests(unittest.TestCase):
    def test_names_the_header_rule_the_body_parts_and_the_trailers(self):
        self.assertTrue(TEMPLATE.is_file(), f"{TEMPLATE} is missing")
        text = TEMPLATE.read_text()
        for part in ("type(scope): summary", "72", "Why", "What changed", "Verified:", "Refs:", "BREAKING CHANGE:"):
            self.assertIn(part, text)
        # the trailers come after the body parts, as in the message dev commit writes
        self.assertLess(text.index("Verified:"), text.index("Refs:"))
        self.assertLess(text.index("Refs:"), text.index("BREAKING CHANGE:"))

    def test_is_guidance_only_so_an_untouched_template_commits_nothing(self):
        self.assertTrue(TEMPLATE.is_file(), f"{TEMPLATE} is missing")
        rc, out, _ = core.sh(["git", "stripspace", "--strip-comments"], input=TEMPLATE.read_text())
        self.assertEqual((rc, out), (0, ""))  # every line is a comment: git removes them all

    def test_has_no_ai_attribution_line(self):
        self.assertTrue(TEMPLATE.is_file(), f"{TEMPLATE} is missing")
        self.assertEqual(gitx.strip_attribution(TEMPLATE.read_text())[1], 0)


# --------------------------------------------------------------------------- dev commit


class CommitBodyTests(Case):
    def test_why_and_verified_write_the_body_in_the_template_order(self):
        self.change()
        rc, out, err = dev("commit", "parser.py", "-m", "feat(parser): keep quoted commas",
                           "--why", "Rows with quoted commas were split in two.",
                           "--body", "The tokenizer reads a quoted field whole.",
                           "--verified", "python3 -m unittest tests.test_parser (12 tests pass)")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.message(), "feat(parser): keep quoted commas\n\n"
                                         "Rows with quoted commas were split in two.\n\n"
                                         "The tokenizer reads a quoted field whole.\n\n"
                                         "Verified: python3 -m unittest tests.test_parser (12 tests pass)")

    def test_every_body_option_keeps_its_paragraph(self):
        # a second --body used to replace the first without a word (lesson L-fy910l5ji04)
        self.change()
        rc, _, err = dev("commit", "parser.py", "-m", "fix(parser): keep quoted commas", "--body", "The tokenizer changed.",
                         "--body", "The reader changed too.")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.message(), "fix(parser): keep quoted commas\n\nThe tokenizer changed.\n\n"
                                         "The reader changed too.")

    def test_refs_and_breaking_change_close_the_message(self):
        self.change()
        rc, _, err = dev("commit", "parser.py", "-m", "feat(api)!: drop v1 tokens", "--why", "v1 tokens never expire.",
                         "--verified", "Verified: the API suite passes", "--refs", "#12",
                         "--refs", "https://tracker.example.com/browse/API-7", "--breaking",
                         "v1 tokens are refused; sign in again for a v2 token.")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.message(), "feat(api)!: drop v1 tokens\n\nv1 tokens never expire.\n\n"
                                         "Verified: the API suite passes\n\n"
                                         "Refs: #12, https://tracker.example.com/browse/API-7\n"
                                         "BREAKING CHANGE: v1 tokens are refused; sign in again for a v2 token.")

    def test_refs_takes_an_issue_a_url_or_a_ticket_key_only(self):
        self.change()
        head = git(self.repo, "rev-parse", "HEAD")
        rc, _, err = dev("commit", "parser.py", "-m", "fix: x", "--refs", "the login bug")
        self.assertEqual(rc, 2)
        self.assertIn("Refs takes an issue number", err)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_a_devkit_task_id_is_refused_anywhere_in_the_message(self):
        self.change()
        head = git(self.repo, "rev-parse", "HEAD")
        for extra in (["--body", "Fixes B-fy6l2ihfpgm from the release report."], ["--refs", "T-fy0000abc12"]):
            rc, _, err = dev("commit", "parser.py", "-m", "fix(parser): keep quoted commas", *extra)
            self.assertEqual(rc, 2, extra)
            self.assertIn("devkit task id", err)
            self.assertIn("handoff", err)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)

    def test_the_options_need_a_header(self):
        self.change()
        rc, _, err = dev("commit", "parser.py", "--why", "Rows were split.")
        self.assertEqual(rc, 2)
        self.assertIn("-m", err)


class TaskRefsTests(Case):
    def test_refs_come_from_the_started_task_ticket_never_its_id(self):
        self.change("feat/parser")
        it = self.task("Keep quoted commas", ["ticket:PROJ-142"], "feat/parser")
        self.task("Another branch", ["issue:#99"], "feat/other")
        rc, out, err = dev("commit", "parser.py", "-m", "fix(parser): keep quoted commas", "--body", "Rows were split.")
        self.assertEqual(rc, 0, err)
        msg = self.message()
        self.assertTrue(msg.endswith("\n\nRefs: PROJ-142"), msg)
        self.assertNotIn(it["meta"]["id"], msg)
        self.assertIn("Refs: PROJ-142", out)
        # once the task is done, it is no longer the branch's active task
        work.release(work.load(it["path"]), a=ANA, status="done")
        (self.repo / "parser.py").write_text("# more\n")
        rc, _, err = dev("commit", "parser.py", "-m", "test(parser): cover empty rows")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Refs:", self.message())

    def test_issue_and_url_links_become_refs(self):
        self.assertEqual([gitx.ticket_ref(x) for x in ("issue:#12", "issue:12", "#12", "acme/api#3", "ticket:API-7",
                                                       "https://linear.app/acme/issue/API-7", "branch:feat/x",
                                                       "initiative:forge", "T-fy0000abc12", "ticket:T-fy0000abc12")],
                         ["#12", "#12", "#12", "acme/api#3", "API-7", "https://linear.app/acme/issue/API-7", None,
                          None, None, None])

    def test_no_refs_when_tasks_on_the_branch_name_different_tickets(self):
        self.change("release/26.11.3")
        self.task("One", ["ticket:PROJ-1"], "release/26.11.3")
        self.task("Two", ["ticket:PROJ-2"], "release/26.11.3")
        rc, out, err = dev("commit", "parser.py", "-m", "fix(parser): keep quoted commas")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Refs:", self.message())
        self.assertIn("--refs", out)  # says which tickets it found and how to name one

    def test_an_explicit_refs_wins_over_the_task_ticket(self):
        self.change("feat/parser")
        self.task("Keep quoted commas", ["ticket:PROJ-142"], "feat/parser")
        rc, _, err = dev("commit", "parser.py", "-m", "fix(parser): keep quoted commas", "--refs", "#7")
        self.assertEqual(rc, 0, err)
        self.assertTrue(self.message().endswith("\n\nRefs: #7"), self.message())


# --------------------------------------------------------------------------- dev setup


class SetupOfferTests(Case):
    def local_template(self, repo):
        return core.sh(["git", "-C", str(repo), "config", "--local", "--get", "commit.template"])[1]

    def offer(self, repos, answers=None, interactive=True):
        asked = []

        def ask(q):
            asked.append(q)
            return answers.pop(0)

        said = []
        res = setup.offer_commit_template(repos, ask=ask, say=said.append, interactive=interactive)
        return res, asked, "\n".join(said)

    def test_yes_sets_each_repository_without_a_template_and_keeps_their_own(self):
        bare, own, later = self.repository("bare"), self.repository("own"), self.repository("later")
        git(own, "config", "--local", "commit.template", str(self.root / "team-template.txt"))
        res, asked, said = self.offer([bare, own], ["y"])
        copy = setup.commit_template_copy()
        self.assertEqual(len(asked), 1)
        self.assertIn("bare", asked[0] + said)
        self.assertEqual(self.local_template(bare), str(copy))
        self.assertEqual(self.local_template(own), str(self.root / "team-template.txt"))  # never replaced
        self.assertEqual(copy.read_text(), TEMPLATE.read_text())
        self.assertEqual(self.local_template(later), "")
        # a repository cloned later gets it on the next run, without a second question
        res, asked, said = self.offer([bare, own, later], [])
        self.assertEqual(asked, [])
        self.assertEqual(self.local_template(later), str(copy))
        self.assertIn("later", said)

    def test_no_and_no_terminal_change_nothing(self):
        bare = self.repository("bare")
        res, asked, said = self.offer([bare], interactive=False)
        self.assertEqual(self.local_template(bare), "")
        self.assertIn("dev install --only git", said)  # where to choose
        res, asked, said = self.offer([bare], ["n"])
        self.assertEqual((len(asked), self.local_template(bare)), (1, ""))
        res, asked, said = self.offer([bare], [])  # the decline is kept: not asked again
        self.assertEqual((asked, self.local_template(bare)), ([], ""))

    def test_a_template_the_developer_removed_stays_removed(self):
        bare = self.repository("bare")
        self.offer([bare], ["y"])
        git(bare, "config", "--local", "--unset", "commit.template")
        self.offer([bare], [])
        self.assertEqual(self.local_template(bare), "")

    def test_dev_setup_commit_template_opts_in_and_out(self):
        bare, own = self.repository("bare"), self.repository("own")
        theirs = str(self.root / "team-template.txt")
        git(own, "config", "--local", "commit.template", theirs)
        self.offer([bare], ["n"])  # declined during the install
        said = []
        # the command asks again, and a yes covers the repository the install left alone
        self.assertEqual(setup.setup_commit_template(repos=[bare, own], ask=lambda q: "y", say=said.append), 0)
        copy = str(setup.commit_template_copy())
        self.assertEqual((self.local_template(bare), self.local_template(own)), (copy, theirs))
        self.assertEqual(setup.setup_commit_template(repos=[bare, own], remove=True, say=said.append), 0)
        self.assertEqual((self.local_template(bare), self.local_template(own)), ("", theirs))  # only devkit's goes
        self.assertIn("bare", said[-1])
        res, asked, _ = self.offer([bare], [])  # after --remove, the install neither asks nor sets it
        self.assertEqual((asked, self.local_template(bare)), ([], ""))

    def test_the_command_line_reaches_it(self):
        bare = self.repository("bare")
        self.offer([bare], ["y"])
        a = cli.build_parser().parse_args(["setup", "commit-template", "--remove"])
        with mock.patch.object(registry, "discover", lambda: ([bare], [], [])), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(a.fn(a), 0)
        self.assertEqual(self.local_template(bare), "")
        self.assertIn("taken out of 1 repositories", out.getvalue())

    def test_git_uses_the_template_it_set(self):
        bare = self.repository("bare")
        self.offer([bare], ["y"])
        (bare / "a.txt").write_text("a\n")
        git(bare, "add", "a.txt")
        # an editor that keeps the template untouched: git refuses the empty message, so nothing is committed
        rc, _, err = core.sh(["git", "-C", str(bare), "commit", "-q"], env=dict(core.GIT_ENV, GIT_EDITOR="true"),
                             timeout=60)
        self.assertNotEqual(rc, 0)
        self.assertIn("empty commit message", err)


if __name__ == "__main__":
    unittest.main()
