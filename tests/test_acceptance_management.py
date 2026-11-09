# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Acceptance of work management as a developer and the crew meet it: the checkout's `bin/dev` run as a process, in a
# sandbox Mac of its own (a temporary HOME with a handoff repository and a project repository whose origin names
# acme/widget), and checked by what the reader sees on the terminal and what the handoff repository holds afterwards.
#
# What it covers: a task number means the item at that place in the list `dev task` showed this reader (a terminal, an
# agent session, a crew lane in it, a Codex thread), and is refused, naming both ids, once the list changed; a loop or
# a script gets no numbers and ids always work; every write prints its verb, id and title; more than five items in Now
# warn on add, on move and in `dev status`; and the dashboard puts plans stored in a sub-folder of a project's plans
# folder on that project's page, grouped by the sub-folder.
#
# An agent session is what its shells carry (DEVKIT_SESSION) and what the hooks record for each open shell call (the
# call ledger, below devkit's state): the tests write both themselves, as the hooks would.
#
# The suite's temporary home (tests/_hermetic.py) keeps the developer's real devkit out of reach; each test gets a
# second, empty home below it, so nothing one test writes reaches another.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_acceptance_management
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
DEV = TREE / "bin" / "dev"
PROJECT = "acme/widget"
SESSION = "s1"
TANK = "a-tank"  # a crew lane's agent id in SESSION
# what names the developer's live session or moves devkit's roots: a process of the sandbox gets none of them
DROP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE",
        "CODEX_THREAD_ID", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME",
        "XDG_CACHE_HOME", "GIT_DIR", "GIT_WORK_TREE", "DEV_ROOT")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
                            "GIT_AUTHOR_NAME": "Ana Dev", "GIT_AUTHOR_EMAIL": "ana@example.com",
                            "GIT_COMMITTER_NAME": "Ana Dev", "GIT_COMMITTER_EMAIL": "ana@example.com"})
    if r.returncode:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {r.stderr or r.stdout}")
    return r.stdout


class Sandbox:
    # A Mac of the test's own: HOME with ~/Development/.handoff (a git repository with one commit) and the project
    # ~/Development/acme/widget, whose origin is GitHub's acme/widget. run() starts the checkout's bin/dev there, as a
    # developer's terminal does (no agent session) unless the test passes one. A class builds its sandbox once and
    # each test starts from the snapshot taken then (restore), in the same folder, so every path devkit recorded
    # still holds.

    def __init__(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp()))
        self.home = self.base / "home"
        self.handoff = self.home / "Development" / ".handoff"
        self.project = self.home / "Development" / "acme" / "widget"
        self.state = self.home / ".local" / "state" / "devkit"
        for d in (self.handoff, self.project):
            d.mkdir(parents=True)
            git(d, "init", "-q", "-b", "main")
            (d / "README.md").write_text("# readme\n")
            git(d, "add", "-A")
            git(d, "commit", "-q", "-m", "init")
        git(self.project, "remote", "add", "origin", f"https://github.com/{PROJECT}.git")
        self.env = {k: v for k, v in os.environ.items() if k not in DROP and not k.startswith("DEVKIT_")}
        self.env.update(HOME=str(self.home), NO_COLOR="1", TERM="dumb", COLUMNS="200", DEVKIT_NO_NOTIFY="1",
                        DEVKIT_NO_AUTOSYNC="1", DEVKIT_HOME=str(TREE), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ana Dev", GIT_AUTHOR_EMAIL="ana@example.com",
                        GIT_COMMITTER_NAME="Ana Dev", GIT_COMMITTER_EMAIL="ana@example.com", GIT_TERMINAL_PROMPT="0",
                        GH_TOKEN="", GITHUB_TOKEN="")
        # stand-ins first on PATH: no command of a test may start the real coding agents or the GitHub CLI
        stubs = self.base / "bin"
        stubs.mkdir()
        for tool in ("claude", "codex", "gh"):
            (stubs / tool).write_text("#!/bin/sh\necho \"$0: stand-in of the acceptance tests\" >&2\nexit 1\n")
            (stubs / tool).chmod(0o755)
        self.env["PATH"] = f"{stubs}:{self.env.get('PATH', '')}"

    def run(self, *argv, env=None, cwd=None) -> subprocess.CompletedProcess:
        r = subprocess.run([sys.executable, "-I", "-S", str(DEV), *map(str, argv)], cwd=str(cwd or self.project),
                           env={**self.env, **(env or {})}, capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL)
        r.stdout, r.stderr = ANSI.sub("", r.stdout), ANSI.sub("", r.stderr)
        return r

    def ok(self, *argv, env=None) -> str:
        r = self.run(*argv, env=env)
        if r.returncode:
            raise AssertionError(f"dev {' '.join(map(str, argv))}: exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def snapshot(self):
        shutil.copytree(self.home, self.base / "snapshot", symlinks=True)

    def restore(self):
        shutil.rmtree(self.home)
        shutil.copytree(self.base / "snapshot", self.home, symlinks=True)

    def remove(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def add(self, title, bucket="now") -> str:
        out = self.ok("task", "add", title, f"--{bucket}", "--why", "acceptance")
        return re.search(r"\b([TFBS]-[0-9a-z]+)\b", out).group(1)

    def status_of(self, iid) -> str:
        f = next(self.handoff.glob(f"items/**/{iid}.md"))
        return re.search(r"^status: *(\S+)", f.read_text(), re.M).group(1)

    @contextlib.contextmanager
    def call(self, caller, cmd):
        # one open shell call of SESSION, as the hooks record it before the shell runs `cmd`: who makes it (main, the
        # tech lead, or a crew lane's agent id) and the command as typed
        folder = self.state / "calls" / SESSION
        folder.mkdir(parents=True, exist_ok=True)
        entry = folder / f"toolu_{time.monotonic_ns()}.json"
        entry.write_text(json.dumps({"tool_use_id": entry.stem, "caller": caller, "ts": time.time(), "ttl": 600,
                                     "cmd": cmd}))
        try:
            yield {"DEVKIT_SESSION": f"claude:{SESSION}"}
        finally:
            entry.unlink()

    def as_agent(self, caller, *argv, typed=None) -> subprocess.CompletedProcess:
        # `dev <argv>` run by `caller` in SESSION, the shell call holding `typed` (default: the command itself)
        with self.call(caller, typed or "dev " + " ".join(shlex.quote(str(a)) for a in argv)) as env:
            return self.run(*argv, env=env)


def numbered(listing: str) -> list:
    # the ids `dev task` printed with a number, in order
    return re.findall(r"^\s+\d+ ([TFBS]-[0-9a-z]+) ", listing, re.M)


class SharedSandbox(unittest.TestCase):
    # one sandbox per class, built by build(); every test starts from its snapshot
    @classmethod
    def setUpClass(cls):
        cls.s = Sandbox()
        cls.addClassCleanup(cls.s.remove)
        cls.build(cls.s)
        cls.s.snapshot()

    @classmethod
    def build(cls, s):
        pass

    def setUp(self):
        self.s.restore()


class ThreeItems(SharedSandbox):
    # the open items of acme/widget, in `dev task` order: one in Now, one in Next, one in Later
    @classmethod
    def build(cls, s):
        cls.login = s.add("Fix the login timeout", "now")
        cls.csv = s.add("Export invoices as CSV", "next")
        cls.parity = s.add("Codex parity test", "later")


class TaskNumbersAtATerminalTests(ThreeItems):

    def test_done_with_the_number_dev_task_showed_closes_that_item_and_prints_its_id_and_title(self):
        self.assertEqual(numbered(self.s.ok("task")), [self.login, self.csv, self.parity])
        out = self.s.ok("task", "done", "2", "--why", "shipped")
        self.assertIn(f"✓ done {self.csv} “Export invoices as CSV”", out)
        self.assertEqual([self.s.status_of(i) for i in (self.login, self.csv, self.parity)], ["todo", "done", "todo"])

    def test_a_number_after_the_list_changed_is_refused_naming_both_ids_and_changes_nothing(self):
        self.s.ok("task")
        self.s.ok("task", "done", self.login, "--why", "fixed")  # by id: #1 of the list shown is gone
        r = self.s.run("task", "done", "1", "--why", "meant the login timeout")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"#1 was {self.login} “Fix the login timeout” then and is {self.csv} “Export invoices as CSV” "
                      f"now", r.stderr)
        self.assertEqual(self.s.status_of(self.csv), "todo")

    def test_the_list_a_write_prints_drops_its_numbers_once_the_write_changed_it(self):
        self.s.ok("task")
        out = self.s.ok("task", "add", "Rotate the API keys", "--later", "--why", "acceptance")
        self.assertEqual(numbered(out), [])
        self.assertIn("no numbers: this write changed the list; dev task numbers it again, and ids always work", out)

    def test_a_number_past_the_end_of_the_shown_list_is_refused_with_its_size(self):
        self.s.ok("task")
        r = self.s.run("task", "start", "9")
        self.assertEqual(r.returncode, 2)
        self.assertRegex(r.stderr, r"no #9 in the list of acme/widget shown at \d\d:\d\d \(3 open items\): run dev "
                                   r"task to number the list again, or use an id")


class TaskListsPerReaderTests(ThreeItems):

    def test_the_tech_lead_and_a_crew_lane_each_number_their_own_list(self):
        self.assertEqual(numbered(self.s.as_agent("main", "task").stdout), [self.login, self.csv, self.parity])
        self.assertEqual(self.s.as_agent(TANK, "task", "done", self.login, "--why", "fixed").returncode, 0)
        self.assertEqual(numbered(self.s.as_agent(TANK, "task").stdout), [self.csv, self.parity])
        lead = self.s.as_agent("main", "task", "done", "2", "--why", "the lead meant the CSV export")
        self.assertEqual(lead.returncode, 2, lead.stdout + lead.stderr)
        self.assertIn(f"#2 was {self.csv} “Export invoices as CSV” then and is {self.parity} “Codex parity test” now",
                      lead.stderr)
        lane = self.s.as_agent(TANK, "task", "done", "2", "--why", "the lane meant the parity test")
        self.assertIn(f"✓ done {self.parity} “Codex parity test”", lane.stdout, lane.stderr)
        self.assertEqual([self.s.status_of(i) for i in (self.csv, self.parity)], ["todo", "done"])

    def test_two_codex_threads_each_number_their_own_list(self):
        one, two = {"CODEX_THREAD_ID": "thread-one"}, {"CODEX_THREAD_ID": "thread-two"}
        self.s.ok("task", env=one)
        self.s.ok("task", "done", self.login, "--why", "fixed", env=two)
        self.assertEqual(numbered(self.s.ok("task", env=two)), [self.csv, self.parity])
        r = self.s.run("task", "done", "1", "--why", "meant the login timeout", env=one)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(f"#1 was {self.login} “Fix the login timeout” then", r.stderr)
        self.assertIn(f"✓ done {self.csv} “Export invoices as CSV”", self.s.ok("task", "done", "1", env=two))

    def test_a_script_gets_no_numbers_and_closes_items_by_id(self):
        script = "bash scripts/tidy.sh"  # the shell call the hooks saw; the `dev` commands run inside it
        listing = self.s.as_agent("main", "task", typed=script)
        self.assertEqual(numbered(listing.stdout), [])
        self.assertIn("no numbers: devkit could not record the list", listing.stdout)
        r = self.s.as_agent("main", "task", "done", "1", "--why", "tidy", typed=script)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no open call of this session holds this command as typed", r.stderr)
        self.assertEqual(self.s.status_of(self.login), "todo")
        r = self.s.as_agent("main", "task", "done", self.login, "--why", "tidy", typed=script)
        self.assertIn(f"✓ done {self.login} “Fix the login timeout”", r.stdout, r.stderr)

    def test_a_loop_whose_calls_name_the_same_command_twice_gets_no_numbers(self):
        with self.s.call("main", "dev task"), self.s.call(TANK, "dev task") as env:
            listing = self.s.run("task", env=env)
        self.assertEqual(numbered(listing.stdout), [])
        with self.s.call("main", "dev task done 1"), self.s.call(TANK, "dev task done 1") as env:
            r = self.s.run("task", "done", "1", env=env)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("another open call of this session runs the same command", r.stderr)


class NowLimitTests(SharedSandbox):
    @classmethod
    def build(cls, s):
        cls.now = [s.add(f"Now item {n}", "now") for n in range(1, 6)]
        cls.waiting = s.add("Waits in Next", "next")

    def test_five_items_in_now_pass_quietly_and_a_sixth_warns_with_how_many_are_worked_on(self):
        self.s.ok("task", "start", self.now[0], "--why", "on it")
        self.assertNotIn("items in Now", self.s.ok("task", "add", "Goes to Later", "--later", "--why", "acceptance"))
        r = self.s.run("task", "add", "One more for Now", "--now", "--why", "acceptance")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("6 items in Now in acme/widget, 1 being worked on: more than 5 hides what comes first; move what "
                      "waits with dev task move <id> --to next --why", r.stderr)

    def test_moving_a_sixth_item_to_now_warns_and_status_shows_it_until_now_is_down_to_five(self):
        r = self.s.run("task", "move", self.waiting, "--to", "now", "--why", "urgent")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("6 items in Now in acme/widget, 0 being worked on", r.stderr)
        self.assertRegex(self.s.ok("status"), r"Tasks +! 6 items in Now in acme/widget, 0 being worked on")
        self.s.ok("task", "move", self.now[4], "--to", "next", "--why", "it waits")
        self.assertNotIn("items in Now", self.s.ok("status"))


class DashboardPlansInSubFoldersTests(unittest.TestCase):
    def test_a_plan_in_a_sub_folder_of_a_projects_plans_belongs_to_that_projects_page_and_its_folder(self):
        s = Sandbox()
        self.addCleanup(s.remove)
        s.ok("plan", "new", "Invoice export", "--kind", "spec")
        s.ok("plan", "new", "Product roadmap")
        folder = s.handoff / "plans" / "acme" / "widget"
        (folder / "product").mkdir()
        git(folder, "mv", next(folder.glob("*-product-roadmap.md")).name, "product/roadmap.md")
        git(folder, "commit", "-q", "-m", "keep the roadmap with the product plans")
        page = s.home / "dashboard.html"
        s.ok("dashboard", "--export", page)
        data = json.loads(re.search(r'<script id="devkit-data" type="application/json">(.*?)</script>',
                                    page.read_text(), re.S).group(1))
        where = {Path(p["path"]).name: (p.get("home"), p.get("group")) for p in data["plans"]}
        self.assertEqual(where, {"roadmap.md": (PROJECT, "product"),
                                 next(folder.glob("*-invoice-export.md")).name: (PROJECT, "")})


if __name__ == "__main__":
    unittest.main()
