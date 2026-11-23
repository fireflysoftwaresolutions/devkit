# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# How fast a dev command starts. Every command took about 2 s: finding the project it runs in built the whole
# projects map again (about 25 git calls per project and a scan of every process's working folder), and dev built
# the parser of every command, importing their modules, to run one. A command now finds its project in the map the
# last `dev sync` cached, builds the map only when that map cannot answer, and builds the parser of the one command
# it runs.
#
# Each test runs bin/dev as a terminal would, with HOME in a temporary folder that holds a dev root of real git
# repositories, devkit's state and a handoff repository. A git and an lsof stand-in first on PATH log every process
# the command starts and run the real program, so the tests count calls, never time. Nothing reaches GitHub (the
# suite's gh stand-in answers every call with an error).
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_startup
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hermetic  # noqa: E402

_hermetic.activate()  # a probe that imports this module by path gets the suite's temporary home too

import _sandbox  # noqa: E402  (the environment of a devkit child: the suite's stand-ins first on PATH)
from devkit import cli  # noqa: E402

LIB = TREE / "lib"
DEV_BIN = TREE / "bin" / "dev"
# the map a `dev sync` writes: every project inspected, the notes and this Mac's cached map in devkit's state folder
WRITE_MAP = ("import sys; sys.path.insert(0, sys.argv[1]); from devkit import registry; "
             "registry.write_outputs(registry.build(with_prs=True, refresh_prs=False, pr_max_age=10 ** 9))")
# a hook's own work, without dev's parser: the modules it loads are the ones a hook call needs
HOOK_ALONE = ("import argparse, sys; sys.path.insert(0, sys.argv[1]); from devkit import cli; "
              "sys.exit(cli.cmd_hook(argparse.Namespace(cmd='hook', event='pre-tool', agent='claude')) or 0)")


def _program(name: str, also=()) -> str:
    found = shutil.which(name, path=os.pathsep.join([os.environ.get("PATH", ""), *also]))
    if not found:
        raise unittest.SkipTest(f"{name} is not installed")
    return os.path.realpath(found)


class Layout:
    # A Mac's dev root in a temporary HOME: acme/app (with a src folder) and acme/api, cloned from GitHub as far as
    # their remotes tell, and a handoff repository. The git and lsof stand-ins log "<program> <arguments>" to
    # calls.log; dev() returns the exit code, the output and the calls of one command.

    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.dev_root = self.home / "Development"
        self.shims = root / "shims"
        self.calls = root / "calls.log"
        self.real_git = _program("git")
        self.shims.mkdir(parents=True)
        for name, real in (("git", self.real_git), ("lsof", _program("lsof", also=["/usr/sbin"]))):
            (self.shims / name).write_text(f"#!/bin/sh\nprintf '%s\\n' \"{name} $*\" >> \"$STARTUP_CALLS\"\n"
                                           f"exec '{real}' \"$@\"\n")
            (self.shims / name).chmod(0o755)
        self.dev_root.mkdir(parents=True)
        (self.home / ".gitconfig").write_text("[user]\n\tname = Ana Dev\n\temail = ana@example.com\n")
        self.handoff = self.dev_root / ".handoff"
        self.handoff.mkdir()
        self.git(self.handoff, "init", "-q", "-b", "main")
        (self.handoff / "README.md").write_text("# handoff\n")
        self.git(self.handoff, "add", "-A")
        self.git(self.handoff, "commit", "-q", "-m", "start")
        self.app = self.repo("acme", "app")
        (self.app / "src").mkdir()
        self.api = self.repo("acme", "api")

    def git(self, cwd, *args) -> str:
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1"}
        r = subprocess.run([self.real_git, "-C", str(cwd), *args], env=env, capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"git {' '.join(args)}: {r.stderr or r.stdout}")
        return r.stdout.strip()

    def repo(self, owner: str, name: str) -> Path:
        path = self.dev_root / owner / name
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        (path / "README.md").write_text(f"# {name}\n")
        self.git(path, "add", "-A")
        self.git(path, "commit", "-q", "-m", "chore: start")
        self.git(path, "remote", "add", "origin", f"https://github.com/{owner}/{name}.git")
        return path

    def env(self) -> dict:
        base = {"PATH": os.environ.get("PATH", ""), "LANG": "en_US.UTF-8"}
        return _sandbox.env(base=base, stubs=[self.shims], HOME=self.home, TERM="dumb", NO_COLOR="1",
                            STARTUP_CALLS=self.calls)

    def sync(self):
        r = subprocess.run([sys.executable, "-I", "-S", "-c", WRITE_MAP, str(LIB)], cwd=self.home, env=self.env(),
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise AssertionError(f"the projects map was not written: {r.stderr}")

    @property
    def cached_map(self) -> Path:
        return self.home / ".local" / "state" / "devkit" / "registry.json"

    def dev(self, *args, cwd, stdin: str = "", python=()) -> tuple:
        self.calls.write_text("")
        r = subprocess.run([sys.executable, "-I", "-S", *python, str(DEV_BIN), *args], cwd=cwd, env=self.env(),
                           input=stdin, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr, self.calls.read_text().splitlines()


def touching(calls, folder: Path) -> list:
    # the calls that read a checkout: git -C <it> or a git started in it
    return [c for c in calls if str(folder) in c]


class FindingTheProjectTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mac = Layout(Path(os.path.realpath(tmp.name)))

    def test_dev_task_finds_its_project_in_the_cached_map_while_the_map_still_names_it(self):
        self.mac.sync()
        rc, out, calls = self.mac.dev("task", cwd=self.mac.app / "src")
        self.assertEqual(rc, 0, out)
        self.assertIn("acme/app · tasks", out)
        # the map is not built again: at most one git call (the remote that names the project), no process scan
        self.assertLessEqual(len([c for c in calls if c.startswith("git ")]), 1, calls)
        self.assertEqual([c for c in calls if c.startswith("lsof ")], [])
        self.assertEqual(touching(calls, self.mac.api), [])
        # a remote changed since the sync names the project anew: the cached id is never used past it
        self.mac.git(self.mac.app, "remote", "set-url", "origin", "https://github.com/acme/app-renamed.git")
        rc, out, _ = self.mac.dev("task", cwd=self.mac.app / "src")
        self.assertEqual(rc, 0, out)
        self.assertIn("acme/app-renamed · tasks", out)

    def test_a_checkout_the_cached_map_lacks_is_found_by_building_the_map(self):
        self.mac.sync()
        web = self.mac.repo("acme", "web")  # cloned after the last sync
        moved = self.mac.dev_root / "acme" / "api-moved"
        self.mac.api.rename(moved)  # moved after it
        # a project the map holds is still found in it: the other checkouts do not change where this one is
        rc, out, calls = self.mac.dev("task", cwd=self.mac.app)
        self.assertEqual(rc, 0, out)
        self.assertLessEqual(len([c for c in calls if c.startswith("git ")]), 1, calls)
        # the new clone, from its own folder and by its name from another project, is found by a build
        rc, out, _ = self.mac.dev("task", cwd=web)
        self.assertEqual((rc, "acme/web · tasks" in out), (0, True), out)
        rc, out, _ = self.mac.dev("task", "-p", "web", cwd=self.mac.app)
        self.assertEqual((rc, "acme/web · tasks" in out), (0, True), out)
        # the moved one by its name: its new folder, never the cached one that is gone
        rc, out, _ = self.mac.dev("go", "api", cwd=self.mac.app)
        self.assertEqual((rc, out.strip().splitlines()[-1]), (0, str(moved)), out)

    def items_of(self, pid: str) -> list:
        folder = self.mac.handoff / "items" / pid
        return sorted(p.name for p in folder.glob("T-*.md")) if folder.is_dir() else []

    def test_a_project_whose_note_arrived_after_the_sync_is_named_by_its_name_never_by_another_ones_prefix(self):
        # The Code reviewer's case: acme/webapp is cloned and synced, then a handoff pull brings the note of acme/web,
        # a project only in the portfolio. `web` is acme/web's exact name; the cached map lacks it and would take
        # acme/webapp, whose name starts with it, so the task and the status went to the wrong project.
        self.mac.repo("acme", "webapp")
        self.mac.sync()
        (self.mac.handoff / "projects" / "acme" / "web.md").write_text("# acme/web\n")
        rc, out, _ = self.mac.dev("task", "add", "Ship the web export", "--now", "-p", "web", cwd=self.mac.app)
        self.assertEqual(rc, 0, out)
        self.assertEqual((len(self.items_of("acme/web")), self.items_of("acme/webapp")), (1, []), out)
        rc, out, _ = self.mac.dev("status", "web", "--json", cwd=self.mac.app)
        self.assertEqual((rc, json.loads(out)["project"]), (0, "acme/web"), out)
        # once a sync has cached it, the map answers again: one git call at most, the remote of the project asked
        self.mac.sync()
        rc, out, calls = self.mac.dev("task", "-p", "webapp", cwd=self.mac.app)
        self.assertEqual((rc, "acme/webapp · tasks" in out), (0, True), out)
        self.assertLessEqual(len([c for c in calls if c.startswith("git ")]), 1, calls)

    def test_a_worktree_made_after_the_sync_for_a_checkout_outside_the_dev_root_is_found_by_its_name(self):
        # acme/web's main checkout lives outside the dev root; the map finds it only through its managed worktree.
        # Made after the sync, that worktree is the only trace of the project on disk.
        self.mac.repo("acme", "webapp")
        self.mac.sync()
        elsewhere = self.mac.root / "elsewhere" / "web"
        elsewhere.mkdir(parents=True)
        self.mac.git(elsewhere, "init", "-q", "-b", "main")
        (elsewhere / "README.md").write_text("# web\n")
        self.mac.git(elsewhere, "add", "-A")
        self.mac.git(elsewhere, "commit", "-q", "-m", "chore: start")
        self.mac.git(elsewhere, "remote", "add", "origin", "https://github.com/acme/web.git")
        self.mac.git(elsewhere, "worktree", "add", "-q", "-b", "feat/export",
                     str(self.mac.dev_root / ".worktrees" / "acme" / "web" / "feat" / "export"))
        rc, out, _ = self.mac.dev("task", "add", "Ship the web export", "--now", "-p", "web", cwd=self.mac.app)
        self.assertEqual(rc, 0, out)
        self.assertEqual((len(self.items_of("acme/web")), self.items_of("acme/webapp")), (1, []), out)
        # once a sync has mapped the worktree, the managed worktree folder no longer keeps the map from answering
        self.mac.sync()
        rc, out, calls = self.mac.dev("task", "-p", "webapp", cwd=self.mac.app)
        self.assertEqual((rc, "acme/webapp · tasks" in out), (0, True), out)
        self.assertLessEqual(len([c for c in calls if c.startswith("git ")]), 1, calls)

    def test_dev_task_from_the_cached_map_writes_the_tasks_block_and_leaves_the_auto_block_to_the_sync(self):
        # A session start wrote the note's auto block after the last sync (registry.update_note from a fresh
        # inspection). `dev task` from the cached map refreshes the tasks block and never puts the older auto block
        # back, which would trade the note with every newer writer.
        self.mac.sync()
        note = self.mac.handoff / "projects" / "acme" / "app.md"
        text = note.read_text()
        start, end = "<!-- dev:auto:start -->", "<!-- dev:auto:end -->"
        head, rest = text.split(start, 1)
        tail = rest.split(end, 1)[1]
        tasks_start, tasks_end = "<!-- dev:tasks:start -->", "<!-- dev:tasks:end -->"
        newer = head + start + "\nNEWER AUTO BLOCK\n" + end + tail
        pre, rest = newer.split(tasks_start, 1)
        note.write_text(pre + tasks_start + "\nAN OLD TASKS BLOCK\n" + tasks_end + rest.split(tasks_end, 1)[1])
        rc, out, _ = self.mac.dev("task", cwd=self.mac.app)
        self.assertEqual(rc, 0, out)
        after = note.read_text()
        self.assertIn(start + "\nNEWER AUTO BLOCK\n" + end, after)
        self.assertNotIn("AN OLD TASKS BLOCK", after)

    def test_dev_status_inspects_only_the_project_it_shows_and_shows_what_a_full_build_shows(self):
        self.mac.git(self.mac.app, "worktree", "add", "-q", "-b", "feat/export",
                     str(self.mac.dev_root / ".worktrees" / "acme" / "app" / "feat" / "export"))
        self.mac.sync()
        (self.mac.app / "draft.txt").write_text("uncommitted\n")  # after the sync: only a fresh inspection sees it
        rc, cached, calls = self.mac.dev("status", "--json", cwd=self.mac.app / "src")
        self.assertEqual(rc, 0, cached)
        self.assertEqual(touching(calls, self.mac.api), [])
        self.mac.cached_map.unlink()  # without the cache, dev status builds the whole map
        rc, built, calls = self.mac.dev("status", "--json", cwd=self.mac.app / "src")
        self.assertEqual(rc, 0, built)
        self.assertNotEqual(touching(calls, self.mac.api), [])  # the stand-ins see a full build read every project
        self.assertEqual(json.loads(cached), json.loads(built))
        self.assertEqual(json.loads(cached)["details"]["changes"], json.loads(built)["details"]["changes"])
        # a project without worktrees shows no checkout in use: no scan of every process's working folder for it
        self.mac.sync()
        rc, out, calls = self.mac.dev("status", cwd=self.mac.api)
        self.assertEqual(rc, 0, out)
        self.assertEqual([c for c in calls if c.startswith("lsof ")], [])


def describe(parser) -> dict:
    # what a parser reads and shows: its help, its defaults and each argument, with its subcommands' parsers
    def same(v):
        code = getattr(v, "__code__", None)
        return (v.__module__, v.__qualname__, code.co_firstlineno) if code else repr(v)
    actions, subs = [], {}
    for a in parser._actions:
        if a.nargs == argparse.PARSER:
            subs = {name: describe(sub) for name, sub in a.choices.items()}
            actions.append((a.dest, a.required, sorted(a.choices)))
            continue
        actions.append((a.option_strings, a.dest, a.nargs, repr(a.const), same(a.default), same(a.type),
                        list(a.choices) if a.choices is not None else None, a.required, a.help, a.metavar))
    return {"prog": parser.prog, "help": parser.format_help(), "actions": actions, "subcommands": subs,
            "defaults": {k: same(v) for k, v in parser._defaults.items()}}


def commands(parser) -> dict:
    return next(a.choices for a in parser._actions if a.nargs == argparse.PARSER)


class OneCommandParserTests(unittest.TestCase):
    def test_each_command_built_alone_is_the_full_parsers_own_and_the_only_one(self):
        full = commands(cli.build_parser())
        for name, sub in full.items():
            with self.subTest(command=name):
                alone = cli.build_parser(only=name)
                self.assertIsNotNone(alone)
                self.assertEqual(list(commands(alone)), [name])
                self.assertEqual(describe(commands(alone)[name]), describe(sub))
        self.assertIsNone(cli.build_parser(only="no-such-command"))

    def test_dev_reads_a_command_line_as_the_full_parser_reads_it(self):
        full = cli.build_parser()
        for argv in (["task", "add", "Export invoices", "--now"], ["hook", "pre-tool", "--agent", "codex"],
                     ["progress", "note", "the API is half done", "--next", "the UI"], ["status", "--json"]):
            with self.subTest(argv=argv):
                self.assertEqual(vars(cli.parse(argv)), vars(full.parse_args(argv)))
        # what it refuses, it refuses in the full parser's words, which list every command
        for argv in (["task", "--bogus"], ["progress", "note"], ["no-such-command"], ["status", "--days", "x"]):
            with self.subTest(argv=argv):
                theirs = subprocess.run([sys.executable, "-I", "-S", "-c",
                                         "import sys; sys.path.insert(0, sys.argv[1]); from devkit import cli; "
                                         "cli.build_parser().parse_args(sys.argv[2:])", str(LIB), *argv],
                                        env=_sandbox.env(), capture_output=True, text=True, timeout=60)
                ours = subprocess.run([sys.executable, "-I", "-S", str(DEV_BIN), *argv], env=_sandbox.env(),
                                      capture_output=True, text=True, timeout=60)
                self.assertEqual((ours.returncode, ours.stderr), (theirs.returncode, theirs.stderr))
                self.assertEqual(ours.returncode, 2)

    def test_a_hook_call_loads_only_the_modules_its_own_work_needs(self):
        with tempfile.TemporaryDirectory() as tmp:
            mac = Layout(Path(os.path.realpath(tmp)))
            payload = json.dumps({"session_id": "s-1", "cwd": str(mac.app), "hook_event_name": "PreToolUse",
                                  "tool_name": "Bash", "tool_input": {"command": "ls"}})

            def loaded(argv) -> set:
                r = subprocess.run([sys.executable, "-I", "-S", "-X", "importtime", *argv], cwd=mac.app,
                                   env=mac.env(), input=payload, capture_output=True, text=True, timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr[-2000:])
                return {ln.rsplit("|", 1)[-1].strip() for ln in r.stderr.splitlines()
                        if ln.startswith("import time:") and "devkit" in ln}
            alone = loaded(["-c", HOOK_ALONE, str(LIB)])
            self.assertIn("devkit.hooks", alone)
            self.assertEqual(sorted(loaded([str(DEV_BIN), "hook", "pre-tool"]) - alone), [])


if __name__ == "__main__":
    unittest.main()
