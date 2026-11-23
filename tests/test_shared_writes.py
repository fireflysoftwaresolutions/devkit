# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Writes to files that several devkit processes share on one Mac: generated files (core.write_if_changed), records
# (work.write_record) and the month's session log (work.log_session, work.replace_session_line). Concurrency is
# real: separate Python processes, started together through tests/_sandbox.py's net, each with a timeout, and every
# process a test starts has ended when the test returns.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_shared_writes
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(TREE / "tests"))
import _sandbox  # noqa: E402

from devkit import core  # noqa: E402

TIMEOUT = 60  # seconds one writer process may take


def together(codes, cwd) -> list:
    # Run each code in its own Python process with devkit importable; they start writing at the same moment (each
    # waits for a shared go-file first). -> their stdout, in order. A process that fails or runs past TIMEOUT fails
    # the test, and every process has ended when this returns.
    go = Path(cwd) / "go"
    head = textwrap.dedent(f"""\
        import os, sys, time
        sys.path.insert(0, {str(TREE / 'lib')!r})
        from pathlib import Path
        from devkit import core
        while not Path({str(go)!r}).exists():
            time.sleep(0.001)
        """)
    procs = []
    try:
        for code in codes:
            procs.append(subprocess.Popen([sys.executable, "-I", "-c", head + textwrap.dedent(code)], cwd=str(cwd),
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          stdin=subprocess.DEVNULL, env=_sandbox.env()))
        go.touch()
        out = []
        for p in procs:
            so, se = p.communicate(timeout=TIMEOUT)
            if p.returncode:
                raise AssertionError(f"writer exit {p.returncode}\n{so}\n{se}")
            out.append(so)
        return out
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


class WriteIfChangedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_two_processes_rewriting_one_file_at_once_never_fail_and_leave_one_writers_whole_text(self):
        # Two processes regenerate the same file 200 times each at the same moment: no write fails, and the file
        # holds one writer's whole text, never a mix.
        f = self.dir / "note.md"
        writer = """
            errors = 0
            for i in range(200):
                try:
                    core.write_if_changed(Path({f!r}), "{who}\\n" * 2000 + str(i) + "\\n")
                except OSError:
                    errors += 1
            print(errors)
            """
        out = together([writer.format(f=str(f), who="ana"), writer.format(f=str(f), who="bob")], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "writes that failed, per process")
        lines = f.read_text().splitlines()
        self.assertEqual(len(lines), 2001)
        self.assertEqual(len(set(lines[:-1])), 1, f"the file mixes both writers' text: {set(lines[:-1])}")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["go", "note.md"])  # no temporary file left

    def test_a_rewrite_keeps_the_files_own_permissions(self):
        # A file only its owner may read (a token, a private note) stays that way when devkit rewrites it.
        f = self.dir / "private.json"
        f.write_text("{}\n")
        f.chmod(0o600)
        self.assertTrue(core.write_if_changed(f, '{"a": 1}\n'))
        self.assertEqual(f.read_text(), '{"a": 1}\n')
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)


# A writer of a record: reads it, adds a changelog entry of its own (as `dev plan revise` and `dev task` do) and
# writes it back, `rounds` times; with locked=True it holds the record's lock across its read. -> the entries it was
# told were written, as JSON.
RECORD_WRITER = """
    import json
    from devkit import work
    a = {{"user": "{who}", "host": "mac", "agent": "claude", "session": "{who}"}}
    path, told = Path({path!r}), []
    for i in range({rounds}):
        try:
            with (work.record_lock(path) if {locked} else __import__("contextlib").nullcontext()):
                it = work.parse(path.read_text())
                work._touch(it, a, "edit", why=f"{who} {{i}}")
                work.write_record(path, work.render(it))
            told.append(f"{who} {{i}}")
        except work.RecordLossError:
            pass
    print(json.dumps(told))
    """


class WriteRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "T-1.md"
        from devkit import work
        it = {"meta": {"id": "T-1", "type": "task", "title": "Export invoices", "status": "todo", "version": 1},
              "body": "", "changelog": ["v1 · 2026-11-09T10:00:00Z · ana@mac (claude:ana) · create"]}
        self.path.write_text(work.render(it))

    def entries(self) -> list:
        import re
        return re.findall(r"(?m)^- v(\d+) · .* — why: (\w+ \d+)$", self.path.read_text())

    def test_writers_racing_on_one_record_are_never_told_a_write_succeeded_that_the_record_lost(self):
        # Four processes each change the record 40 times at once, every one from the version it read: a write the
        # record cannot keep is refused (RecordLossError); every write reported as done is in the record, each with a
        # version of its own.
        import json
        out = together([RECORD_WRITER.format(who=w, path=str(self.path), rounds=40, locked=False)
                        for w in ("ana", "bob", "cid", "dee")], self.dir)
        told = sorted(e for o in out for e in json.loads(o))
        kept = self.entries()
        self.assertEqual(told, sorted(why for _, why in kept), "told it was written, but the record lost it")
        versions = [v for v, _ in kept]
        self.assertEqual(len(versions), len(set(versions)), "two writes share a version")

    def test_writers_holding_the_records_lock_across_their_read_all_succeed_in_turn(self):
        # The same race, each writer holding the record's lock from its read to its write: they take turns, none is
        # refused, and the versions run without a gap.
        import json
        out = together([RECORD_WRITER.format(who=w, path=str(self.path), rounds=25, locked=True)
                        for w in ("ana", "bob", "cid")], self.dir)
        told = [e for o in out for e in json.loads(o)]
        self.assertEqual(len(told), 75)
        kept = self.entries()
        self.assertEqual(sorted(told), sorted(why for _, why in kept))
        self.assertEqual(sorted(int(v) for v, _ in kept), list(range(2, 77)))


# Writers that sessions run at the same moment: the status line's usage record, the dashboard page and the repair of the
# agent settings' env block. Each wrote through one fixed temporary name, so when two wrote together one renamed the
# other's temporary file away and its own rename found no file: three processes drawing 400 status lines each
# swallowed 710 FileNotFoundError.
class SameFileWritersTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_status_lines_drawn_at_once_record_the_usage_without_an_error(self):
        import json
        quota = self.dir / "quota"
        writer = """
            import contextlib, io, json
            from devkit import hooks
            hooks.QUOTA, errors = Path({quota!r}), []
            hooks._swallowed = lambda component, exc: errors.append(type(exc).__name__)
            for i in range(300):
                hooks._stdin_json = lambda i=i: {{"cwd": {cwd!r}, "rate_limits": {{"five_hour": {{"used_percentage": i}}}}}}
                with contextlib.redirect_stdout(io.StringIO()):
                    hooks.statusline("claude")
            print(json.dumps(errors))
            """.format(quota=str(quota), cwd=str(self.dir))
        out = together([writer] * 3, self.dir)
        self.assertEqual([json.loads(o) for o in out], [[], [], []], "errors the status line swallowed, per process")
        self.assertIn("five_hour", json.loads((quota / "claude.json").read_text()))
        self.assertEqual(sorted(p.name for p in quota.iterdir()), ["claude.json"])  # no temporary file left

    def test_dashboards_written_at_once_never_fail_and_leave_one_writers_whole_page(self):
        state = self.dir / "state"
        writer = """
            from devkit import dashboard
            core.STATE, errors = Path({state!r}), 0
            dashboard.render = lambda reg, handoff=None: "<p>{who}</p>\\n" * 5000
            for i in range(200):
                try:
                    dashboard.write({{}})
                except OSError:
                    errors += 1
            print(errors)
            """
        out = together([writer.format(state=str(state), who=w) for w in ("ana", "bob")], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "writes that failed, per process")
        lines = (state / "dashboard" / "index.html").read_text().splitlines()
        self.assertEqual((len(lines), len(set(lines))), (5000, 1), f"not one writer's whole page: {set(lines)}")
        self.assertEqual(sorted(p.name for p in (state / "dashboard").iterdir()), ["index.html"])

    def test_settings_env_repairs_at_once_never_fail_and_leave_valid_settings(self):
        import json
        settings = self.dir / "settings.json"
        settings.write_text('{"env": {}}\n')
        writer = """
            from devkit import hooks, policy
            core.older_copy, errors = (lambda: False), 0
            for i in range(200):  # each round devkit's value differs from the file's, so each round writes
                policy.claude_desired = lambda i=i: ({{("env", "DEVKIT_RACE"): "{who}-%d" % i}}, {{}})
                try:
                    hooks._devkit_env_change({path!r}, {{"env": {{}}}})
                except OSError:
                    errors += 1
            print(errors)
            """
        out = together([writer.format(path=str(settings), who=w) for w in ("ana", "bob")], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "repairs that failed, per process")
        self.assertRegex(json.loads(settings.read_text())["env"]["DEVKIT_RACE"], r"^(ana|bob)-\d+$")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["go", "settings.json"])


    CI_STEP_WRITER = """
        import types
        from devkit import gitx
        core.STATE, errors = Path({state!r}), 0
        c = types.SimpleNamespace(pid="acme/widget", top={top!r})
        for i in range(100):
            try:
                gitx._save_step(c, "{who} › %d" % i, {{"rc": 0}})
            except OSError:
                errors += 1
        print(errors)
        """

    # Two `dev ci --run` of one checkout record their steps in one file: no save fails, and no step one of them
    # recorded is lost to the other's save
    def test_ci_steps_saved_at_once_never_fail_and_keep_every_step(self):
        import json
        state, top = self.dir / "state", str(self.dir / "widget")
        out = together([self.CI_STEP_WRITER.format(state=str(state), top=top, who=w) for w in ("ana", "bob")], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "saves that failed, per process")
        files = list((state / "ci" / "steps").glob("*.json"))
        self.assertEqual(len(files), 1, files)
        steps = json.loads(files[0].read_text())["steps"]
        self.assertEqual(len(steps), 200, "steps lost to the other process's save")

    # Two checkouts of one project (the main checkout and a worktree) saving at once keep a file each, with only their
    # own steps: a run in one never changes what --resume keeps in the other
    def test_ci_steps_of_two_checkouts_saved_at_once_keep_a_file_each(self):
        import json
        state = self.dir / "state"
        out = together([self.CI_STEP_WRITER.format(state=str(state), top=str(self.dir / top), who=w)
                        for w, top in (("ana", "widget"), ("bob", "widget-wt"))], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "saves that failed, per process")
        files = sorted((state / "ci" / "steps").glob("*.json"))
        self.assertEqual(len(files), 2, files)
        owners = sorted(sorted({key.split(" › ")[0] for key in json.loads(f.read_text())["steps"]}) for f in files)
        self.assertEqual(owners, [["ana"], ["bob"]])
        self.assertEqual([len(json.loads(f.read_text())["steps"]) for f in files], [100, 100])

    # Two installs record the installed copy at once: no write fails, and the record is one writer's whole JSON
    def test_install_records_written_at_once_never_fail_and_leave_valid_json(self):
        import json
        pinned = self.dir / "26.11.20-abc1234"
        pinned.mkdir()
        (pinned / "MANIFEST.json").write_text("{}\\n")
        record = self.dir / "share" / "install.json"
        writer = """
            from devkit import setup
            core.install_record_file, core.PIN_MANIFEST, errors = (lambda: Path({record!r})), "MANIFEST.json", 0
            for i in range(200):
                try:
                    setup.record_install({{"name": "{who}", "version": "26.11.20", "commit": "abc1234",
                                           "path": {pinned!r}, "source": "", "verified": None}})
                except OSError:
                    errors += 1
            print(errors)
            """
        out = together([writer.format(record=str(record), pinned=str(pinned), who=w) for w in ("ana", "bob")],
                       self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "writes that failed, per process")
        self.assertIn(json.loads(record.read_text())["name"], ("ana", "bob"))
        self.assertEqual(sorted(p.name for p in record.parent.iterdir()), ["install.json"])

    # Two installs build Codex's copy of the plugin at once: no build fails, the copy has every skill, and no
    # half-built folder is left beside it
    def test_codex_plugins_built_at_once_never_fail_and_leave_one_whole_copy(self):
        kit = self.dir / "kit"
        for rel, text in (("plugin/skills/testing/SKILL.md", "---\nname: testing\n---\n"),
                          ("plugin/commands/ship.md", "---\ndescription: ship it\n---\nShip $ARGUMENTS\n"),
                          ("plugin/.codex-plugin/plugin.json", "{}\n"),
                          (".agents/plugins/marketplace.json", "{}\n")):
            (kit / rel).parent.mkdir(parents=True, exist_ok=True)
            (kit / rel).write_text(text)
        market = self.dir / "market"
        writer = """
            from devkit import setup
            setup.DEVKIT_HOME, setup.CODEX_MARKET, errors = Path({kit!r}), Path({market!r}), []
            for i in range(30):
                try:
                    setup.build_codex_plugin()
                except OSError as e:
                    errors.append(type(e).__name__)
            print(len(errors))
            """
        out = together([writer.format(kit=str(kit), market=str(market))] * 2, self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "builds that failed, per process")
        self.assertEqual(sorted(p.name for p in (market / "plugin" / "skills").iterdir()), ["ship", "testing"])
        self.assertEqual(sorted(p.name for p in market.iterdir()), [".agents", "plugin"])

    # Two installs merge devkit's keys into Claude Code's settings at once: no write fails, and the file is one
    # writer's whole JSON, with nothing left beside it
    def test_claude_settings_merged_at_once_never_fail_and_leave_valid_settings(self):
        import json
        home = self.dir / "claude"
        home.mkdir()
        (home / "settings.json").write_text("{}\n")
        writer = """
            from devkit import setup
            setup.CLAUDE_HOME, setup.backup, core.older_copy_skip, errors = Path({home!r}), (lambda f: None), \\
                (lambda *a, **k: False), 0
            conf = setup.load_conf
            for i in range(100):  # each round devkit's settings differ from the file's, so each round writes
                setup.load_conf = lambda name, i=i: dict(conf(name), env={{"DEVKIT_RACE": "{who}-%d" % i}}) \\
                    if name == "claude/settings.json" else conf(name)
                try:
                    setup.write_claude_settings(include_hooks=False)
                except OSError:
                    errors += 1
            print(errors)
            """
        out = together([writer.format(home=str(home), who=w) for w in ("ana", "bob")], self.dir)
        self.assertEqual([o.strip() for o in out], ["0", "0"], "writes that failed, per process")
        self.assertRegex(json.loads((home / "settings.json").read_text())["env"]["DEVKIT_RACE"], r"^(ana|bob)-\d+$")
        self.assertEqual(sorted(p.name for p in home.iterdir()), ["settings.json"])


class FileLockTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "only macOS volumes ignore letter case by default")
    def test_one_file_named_with_two_spellings_of_its_path_has_one_lock(self):
        # `dev plan revise plans/Acme/Plan.md` and `… plans/acme/plan.md` change one file on a macOS volume: while one
        # writer holds the file's lock through one spelling, another writer (a thread, with a lock of its own) asking
        # through the other spelling does not get it.
        import threading
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(core, "STATE", Path(tmp) / "state"):
            plan = Path(tmp) / "Plan.md"
            plan.write_text("# Plan\n")
            other = []

            def other_writer():
                with core.file_lock(Path(tmp) / "plan.md", wait=0) as got:
                    other.append(got)

            with core.file_lock(plan) as held:
                t = threading.Thread(target=other_writer)
                t.start()
                t.join(10)
            self.assertTrue(held)
            self.assertEqual(other, [False], "the other spelling took a lock of its own on the same file")


class _Late(Exception):
    pass


_HOLD = ("import fcntl, sys, time\nf = open(sys.argv[1], 'w')\nfcntl.flock(f, fcntl.LOCK_EX)\n"
         "print('held', flush=True)\ntime.sleep(30)\n")


class LockWaitTimerTests(unittest.TestCase):
    # core._flock_within waits for a lock under a timer of its own. A timer its caller set keeps its deadline: when
    # it is due during the wait its handler runs then (one that raises ends the wait), and afterwards it has the time
    # it has left and its repeat. Another process holds the lock for the whole test.

    def setUp(self):
        import os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        lock = os.path.join(tmp.name, "held.lock")
        holder = subprocess.Popen([sys.executable, "-c", _HOLD, lock], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        holder.stdout.close()
        self.fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, self.fd)

    def timer(self, seconds, period=0.0):
        # the caller's timer: a SIGALRM handler that raises, as a deadline does
        import signal

        def late(*_):
            raise _Late()
        before = signal.signal(signal.SIGALRM, late)
        self.addCleanup(signal.signal, signal.SIGALRM, before)
        signal.setitimer(signal.ITIMER_REAL, seconds, period)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)

    # The bounds below are the waits each behavior would otherwise run to, not guesses at the machine's speed: a
    # loaded machine passes as soon as the caller's tick comes, and only a lost or late tick takes the whole wait.
    WAIT = 20.0

    def test_a_lock_wait_longer_than_the_callers_time_ends_at_the_callers_deadline(self):
        import time
        started = time.monotonic()
        self.timer(0.3)
        with self.assertRaises(_Late):
            core._flock_within(self.fd, self.WAIT)
        self.assertLess(time.monotonic() - started, self.WAIT)  # the caller's deadline ended it, not its own

    def test_after_a_lock_wait_the_callers_timer_still_repeats(self):
        import time
        started = time.monotonic()
        self.timer(0.4, 0.05)
        with self.assertRaises(_Late):
            self.assertFalse(core._flock_within(self.fd, 0.1))  # its own wait ends first: the lock is not taken
            try:
                time.sleep(self.WAIT)  # code that swallows the caller's first tick
            except _Late:
                pass
            time.sleep(self.WAIT)  # the next tick still comes
        self.assertLess(time.monotonic() - started, self.WAIT)  # both ticks came during the first sleep's time

    def test_an_earlier_outer_timer_reaches_its_handler_during_the_wait_and_the_wait_goes_on(self):
        import signal
        import time
        fired = []
        before = signal.signal(signal.SIGALRM, lambda *_: fired.append(time.monotonic()))
        self.addCleanup(signal.signal, signal.SIGALRM, before)
        signal.setitimer(signal.ITIMER_REAL, 0.1)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)
        started = time.monotonic()
        self.assertFalse(core._flock_within(self.fd, 0.4))
        returned = time.monotonic()
        self.assertEqual(len(fired), 1)
        self.assertLess(fired[0], returned)  # at its own time, during the wait, not after it
        self.assertGreater(returned - started, 0.35)  # a handler that does not raise leaves the wait to its own time


class SessionLogTests(unittest.TestCase):
    def setUp(self):
        from devkit import work
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.saved = work.SESSIONS
        work.SESSIONS = self.dir / "sessions"
        self.addCleanup(setattr, work, "SESSIONS", self.saved)

    def month(self) -> Path:
        import time
        return self.dir / "sessions" / "acme" / "widget" / f"{time.strftime('%Y-%m')}.md"

    def test_a_session_line_rewritten_on_every_turn_never_drops_a_line_another_session_appends(self):
        # Session A rewrites its own line 60 times (the Stop hook) while session B appends lines (dev handoff --log)
        # until A is done: every line B appended is kept, and A's line is there once, as A last wrote it. A waits for
        # a fresh append of B before each rewrite, so the two always overlap, however busy the Mac is.
        done, ticks = self.dir / "a-done", self.dir / "b-ticks"
        setup = f"""
            from devkit import work
            work.SESSIONS = Path({str(self.dir / 'sessions')!r})
            """
        rewrite = setup + f"""
            seen, deadline = 0, time.monotonic() + {TIMEOUT}
            for i in range(60):
                while (Path({str(ticks)!r}).stat().st_size if Path({str(ticks)!r}).exists() else 0) <= seen:
                    if time.monotonic() > deadline:
                        raise SystemExit("session B stopped appending")
                    time.sleep(0.0005)
                seen = Path({str(ticks)!r}).stat().st_size
                work.replace_session_line("acme/widget", "<!-- s:sA -->", f"session A turn {{i}} <!-- s:sA -->")
            Path({str(done)!r}).touch()
            """
        append = setup + f"""
            n = 0
            while not Path({str(done)!r}).exists():
                work.log_session("acme/widget", f"bob line {{n}}")
                n += 1
                with open({str(ticks)!r}, "a") as t:
                    t.write(".")
            print(n)
            """
        written = int(together([rewrite, append], self.dir)[1])
        text = self.month().read_text()
        self.assertGreaterEqual(written, 60, "session B did not append between session A's rewrites")
        self.assertEqual(len([ln for ln in text.splitlines() if ln.startswith("- bob line ")]), written,
                         "lines of session B were lost")
        self.assertEqual([ln for ln in text.splitlines() if "<!-- s:sA -->" in ln],
                         ["- session A turn 59 <!-- s:sA -->"])

    def test_a_session_line_is_appended_when_the_month_has_none_and_replaced_in_place_when_it_has_one(self):
        from devkit import work
        work.log_session("acme/widget", "first")
        f = work.replace_session_line("acme/widget", "<!-- s:s1 -->", "s1 turn 1 <!-- s:s1 -->")
        work.log_session("acme/widget", "last")
        work.replace_session_line("acme/widget", "<!-- s:s1 -->", "s1 turn 2 <!-- s:s1 -->")
        self.assertEqual(f, self.month())
        self.assertEqual(f.read_text().splitlines()[2:], ["- first", "- s1 turn 2 <!-- s:s1 -->", "- last"])


if __name__ == "__main__":
    unittest.main()
