# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The command line reaches the domain code: `dev config set <key> <value>` writes one key through core.config_set
# (crew slots R1), and `dev progress step --approves "<steps>"` records the step's verdict through approvals.record
# (spec F6). Each test drives cli.main as a terminal would, in a temporary state; nothing below the CLI is stubbed
# except whether the caller is the developer's terminal.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_wiring
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _sandbox  # noqa: E402  (the environment of a devkit child: the suite's stand-ins first on PATH)
from devkit import audit, bugs, cli, core, identity, ui, work, progress as pg  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402
from test_progress import TempState, launch  # noqa: E402
from test_work import TempHandoff  # noqa: E402

DEV = Path(__file__).resolve().parents[1] / "bin" / "dev"

REPORT = ("**[Firefly> DevKit · Release engineer (Link)]** api · `main` · tag\n- **Done:** tagged v1\n"
          "- **Check:** ✓ the tag points at the verified commit\n- **Next:** Morpheus")


def dev(*argv):
    # cli.main as the main session's terminal runs it: no subagent lane, its output captured
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        os.environ.pop("DEVKIT_FROM_AGENT", None)
        rc = cli.main(list(argv))
    return rc, out.getvalue() + err.getvalue()


class ConfigSetTests(unittest.TestCase):
    def setUp(self):
        self.state = TempState().__enter__()
        self.addCleanup(self.state.__exit__, None, None, None)
        self.file = Path(self.state.tmp.name) / "config.json"
        p = mock.patch.object(core, "CONFIG_FILE", self.file)
        p.start()
        self.addCleanup(p.stop)

    def as_developer(self, yes):
        return mock.patch.object(core, "set_by_developer", lambda: yes)

    def test_the_developer_sets_a_key_and_the_change_is_written_and_audited(self):
        with self.as_developer(True):
            rc, out = dev("config", "set", "progress.max_parallel", "6")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.file.read_text()), {"progress": {"max_parallel": 6}})
        self.assertIn("progress.max_parallel: 4 → 6", out)
        self.assertEqual([r["target"] for r in audit.records(1, action="dev.config.set")], ["progress.max_parallel"])

    def test_an_agents_set_is_refused_and_writes_nothing(self):
        with self.as_developer(False):
            rc, out = dev("config", "set", "progress.max_parallel", "6")
        self.assertEqual(rc, 2)
        self.assertIn("only the developer", out)
        self.assertFalse(self.file.exists())

    def test_set_without_a_value_shows_the_usage_and_writes_nothing(self):
        with self.as_developer(True):
            rc, out = dev("config", "set", "progress.max_parallel")
        self.assertEqual(rc, 2)
        self.assertIn("dev config set <key> <value>", out)
        self.assertFalse(self.file.exists())


class StepApprovesTests(HookCase):
    def setUp(self):
        super().setUp()
        pg.start("Release", plan="tag=10m; verify=10m after tag")
        pg.assign("tag", "Link", activity="release")
        launch("a_link", "devkit:link", "Link: tag")
        pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a_link", "agent_type": "devkit:link",
                                  "last_assistant_message": REPORT}, False, now=time.time() + 1)

    def test_the_accountable_approves_a_finished_step_with_the_check_of_their_own_step(self):
        rc, out = dev("progress", "step", "verified the release", "--step", "verify",
                      "--check", "✓ downloaded the release and ran it", "--approves", "tag")
        self.assertEqual(rc, 0, out)
        run = pg.load_run("claude", "s1")
        self.assertEqual([(a["by"], a["steps"], a["verdict"]) for a in run.get("approvals") or []],
                         [("morpheus", ["tag"], "approved")])

    def test_morpheus_approving_his_own_step_is_refused_and_records_no_approval(self):
        rc, out = dev("progress", "step", "verified the release", "--step", "verify",
                      "--check", "✓ downloaded the release", "--approves", "verify")
        self.assertEqual(rc, 2, out)
        self.assertIn("verify", out)
        self.assertEqual(pg.load_run("claude", "s1").get("approvals") or [], [])


# The commands whose words the guard reads to tell what a dev call does (guard._dev_actions, _ro_dev, _crew_kind,
# the cross-project and refusal rules).
GUARDED = (["backup"], ["sync"], ["push"], ["pr", "create"], ["pr", "merge"], ["pr", "ready"], ["pr", "comment"],
           ["pr", "review"], ["release"], ["issue", "create"], ["jobs", "run"], ["jobs", "exec"], ["commit"],
           ["crew"], ["task"], ["plan"], ["delivery"], ["lesson"], ["lesson", "release"], ["test"], ["audit"], ["ci"],
           ["config"], ["hook"],
           ["install"], ["update"], ["note"], ["handoff"], ["progress"], ["branch"], ["branch", "embargo"])


def _parsers(p, path=()):
    for act in p._actions:
        for name, sub in (getattr(act, "choices", None) or {}).items() if act.nargs == argparse.PARSER else ():
            yield from _parsers(sub, (*path, name))
    yield path, p


class ParserReadsFlagsAsTheGuardDoesTests(unittest.TestCase):
    # B1: argparse took a unique prefix of a flag (`dev sync --sched`), and `dev -h <command>` ran the command

    def test_no_dev_parser_takes_an_abbreviated_flag(self):
        on = [" ".join(path) or "dev" for path, p in _parsers(cli.build_parser()) if p.allow_abbrev]
        self.assertEqual(on, [])
        for argv in (["sync", "--sched"], ["backup", "--al"], ["push", "--force-w"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as e:
                cli.build_parser().parse_args(argv)
            self.assertEqual(e.exception.code, 2)
        self.assertTrue(cli.build_parser().parse_args(["sync", "--scheduled"]).scheduled)  # the full flag works

    def test_help_before_a_command_shows_its_help_and_runs_nothing(self):
        rc, out = dev("-h", "version")
        self.assertEqual(rc, 0)
        self.assertIn("usage: dev version", out)
        rc, out = dev("--help")
        self.assertEqual(rc, 0)
        self.assertNotIn("usage: dev version", out)

    def test_the_guard_knows_every_value_option_of_the_commands_it_reads(self):
        from devkit import guard
        tree = dict(_parsers(cli.build_parser()))
        values, flags = set(), set()
        for path in GUARDED:
            for act in tree[tuple(path)]._actions:
                (flags if act.nargs == 0 else values).update(act.option_strings)
        self.assertEqual(sorted(values - guard._DEV_VALUES), [])  # a value it does not know reads as a word
        self.assertEqual(sorted(flags & guard._DEV_VALUES), [])  # a flag it reads as a value hides the next word


class ClosedPipeTests(unittest.TestCase):
    # `dev audit --json | head -c 100`: the reader closes the pipe before dev has written everything. dev stops
    # quietly with the exit code of a program that a closed pipe stopped (128 + SIGPIPE, as `yes | head` reports it)
    # and records no crash: a reader that has read enough is no devkit bug.

    def test_a_write_to_a_closed_pipe_during_the_command_is_no_crash(self):
        # what `print(json.dumps(recs))` in cmd_audit raised once its output passed the 128 KiB buffer
        def write_to_a_closed_pipe(a):
            raise BrokenPipeError(32, "Broken pipe")

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(bugs, "BUGS", Path(d) / "bugs"), \
                mock.patch.object(bugs, "CRASHES", Path(d) / "crashes"), \
                mock.patch.object(cli, "cmd_version", write_to_a_closed_pipe):
            rc, out = dev("version")
            self.assertEqual((rc, out), (141, ""))
            self.assertFalse((Path(d) / "crashes").exists())
            self.assertEqual(bugs.occurrences(""), [])

    def test_a_closed_pipe_at_exit_ends_dev_quietly(self):
        # the version line stays in the output buffer until the process exits; the pipe's reading end is closed
        # before dev starts, so that flush meets a closed pipe
        with tempfile.TemporaryDirectory() as d:
            home = Path(d).resolve()
            env = _sandbox.env(base={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                                     "LANG": "en_US.UTF-8", "DEVKIT_NO_NOTIFY": "1"})
            r, w = os.pipe()
            os.close(r)
            try:
                p = subprocess.run([sys.executable, "-I", "-S", str(DEV), "version"], stdout=w, stderr=subprocess.PIPE,
                                   stdin=subprocess.DEVNULL, env=env, cwd=str(home), timeout=60)
            finally:
                os.close(w)
            self.assertEqual((p.returncode, p.stderr.decode(errors="replace")), (141, ""))
            crashes = home / ".local" / "state" / "devkit" / "crashes"
            self.assertEqual(sorted(f.name for f in crashes.iterdir()) if crashes.is_dir() else [], [])


class MachineIdentityLineTests(unittest.TestCase):
    # After the machine id came, the command line still named this Mac by its hostname: `dev task` showed this Mac's
    # lock under a former hostname as another machine's, `dev progress` listed this Mac's own presence as another
    # machine's run, `dev team` showed the machine id with no name a person knows, and the handoff's sync commit
    # named the hostname alone
    def setUp(self):
        self.handoff = TempHandoff().__enter__()
        self.addCleanup(self.handoff.__exit__, None, None, None)
        self.name = "Mac-158.lan"
        for p in (mock.patch.object(core, "STATE", self.handoff.root.parent / "state"),
                  mock.patch.dict(core.CFG, {"github_user": "ana"}),
                  mock.patch.object(ui, "COLOR", False),
                  mock.patch("socket.gethostname", side_effect=lambda: self.name)):
            p.start()
            self.addCleanup(p.stop)
        self.t0 = time.time()
        identity.machine()  # devkit runs here as mac-158 ...
        self.name = "andress-mbp-2"  # ... and the network renames the Mac
        self.me = work.actor("claude", "s1")

    def held(self, holder):
        it = work.create("acme/api", f"Fix {holder}", a=self.me)
        it["meta"].update(claimed_by=f"{holder} (codex)", claimed_at=work.now_iso(self.t0), status="doing",
                          lock_expires=work.now_iso(self.t0 + 3600))
        work.write_record(it["path"], work.render(it))

    def test_dev_task_marks_this_macs_lock_under_a_former_hostname_as_yours(self):
        self.held("ana@mac-158")
        self.held("ana@mac-55")  # a name this Mac never carried: another Mac's lock
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_items("acme/api", "acme/api", rebind=False)
        rows = {ln.split("Fix ")[1].split()[0]: ln for ln in out.getvalue().splitlines() if "Fix " in ln}
        self.assertIn("🔒 you", rows["ana@mac-158"])
        self.assertIn("🔒 ana@mac-55", rows["ana@mac-55"])

    def test_dev_progress_never_lists_this_macs_own_presence_as_another_machines_run(self):
        work.PRESENCE.mkdir(parents=True)
        for who in ("ana@mac-158", "bob@studio"):
            user, host = who.split("@")
            (work.PRESENCE / f"{who}.json").write_text(json.dumps(
                {"user": user, "host": host, "updated": work.now_iso(self.t0), "locks": [],
                 "sessions": [{"agent": "claude", "project": "acme/api",
                               "progress": {"goal": f"Run of {who}", "step": 1, "steps": 3}}]}))
        with TempState():
            rc, out = dev("progress")
        self.assertEqual(rc, 0, out)
        self.assertIn("Run of bob@studio", out)
        self.assertNotIn("Run of ana@mac-158", out)

    def test_dev_team_names_each_machine_by_its_hostname_and_its_id(self):
        work.write_presence([], a=self.me)
        rc, out = dev("team")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"ana@andress-mbp-2 ({self.me['host']})", out)

    # presence is another machine's text: a label that is not a plain hostname (here terminal escapes) never reaches
    # the terminal, and that machine shows by its id
    def test_a_label_that_is_not_a_hostname_never_reaches_the_terminal(self):
        work.PRESENCE.mkdir(parents=True)
        (work.PRESENCE / "bob@m-0b0b0b0b.json").write_text(json.dumps(
            {"user": "bob", "host": "m-0b0b0b0b", "label": "\u001b[2Jyou", "updated": work.now_iso(self.t0),
             "locks": [], "sessions": [{"agent": "codex", "project": "acme/api", "progress": {"goal": "Their run"}}]}))
        self.held("bob@m-0b0b0b0b")
        for argv in (["team"], ["progress"]):
            with self.subTest(argv=argv), TempState():
                rc, out = dev(*argv)
                self.assertEqual(rc, 0, out)
                self.assertIn("bob@m-0b0b0b0b", out)
                self.assertNotIn("\x1b", out)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_items("acme/api", "acme/api", rebind=False)
        self.assertIn("🔒 bob@m-0b0b0b0b", out.getvalue())
        self.assertNotIn("\x1b", out.getvalue())

    def test_another_macs_lock_shows_by_its_hostname(self):
        work.PRESENCE.mkdir(parents=True)
        (work.PRESENCE / "bob@m-0b0b0b0b.json").write_text(json.dumps(
            {"user": "bob", "host": "m-0b0b0b0b", "label": "studio", "updated": work.now_iso(self.t0), "locks": [],
             "sessions": []}))
        self.held("bob@m-0b0b0b0b")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_items("acme/api", "acme/api", rebind=False)
        self.assertIn("🔒 bob@studio", out.getvalue())

    def test_the_handoff_sync_commit_names_the_mac_by_its_label_and_id(self):
        git = ["git", "-C", str(self.handoff.root), "-c", "user.email=t@example.com", "-c", "user.name=t"]
        subprocess.run(git + ["init", "-q"], check=True)
        (self.handoff.root / "note.md").write_text("x\n")
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                                          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}):
            cli.commit_push(push=False, quiet=True)
        subject = subprocess.run(git + ["log", "-1", "--format=%s"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(subject, f"sync: 1 file(s) from andress-mbp-2 ({self.me['host']})")


if __name__ == "__main__":
    unittest.main()
