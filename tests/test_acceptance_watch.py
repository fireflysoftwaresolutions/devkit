# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Acceptance: one wait per consumer across every session on this Mac. Several Claude Code sessions and Codex threads,
# in one repository or in several, each with its own run, crew lanes and waits, share devkit's state folder. Every
# event must wake exactly the consumer it belongs to (the main session of its own session, or one crew lane), never
# another session's or another lane's wait, never lost and never twice; a newer wait supersedes only an older wait
# of the same session and consumer.
#
# Every devkit call runs in a child process whose HOME is a folder of the test, so devkit's state is the sandbox's
# and the real ~/.local/state/devkit is never read or written. The test process never imports devkit (its import
# puts /opt/homebrew/bin first on PATH): each child restores the sandbox PATH right after the import and refuses to
# start codex, claude, gh or a notifier from anywhere but the stub folder, whose stubs only record the call. Real
# `dev watch --wait` processes run where a scenario needs true concurrency, with short timeouts; tearDown ends every
# process the test started and fails on any left under the sandbox.
#
# Tests marked known_defect reproduce a defect that holds today; each asserts the fixed behavior, so it fails now
# (an expected failure) and passes once the fix lands: delete the marker with the fix. WATCH_SHOW_DEFECTS=1 runs them
# as plain tests, to read why each fails.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_acceptance_watch -v
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402
import _sandbox  # noqa: E402  (the suite's net: stand-ins first, so no child reaches this Mac's codex, claude, brew, gh)

_hermetic.activate()
LIB = ROOT / "lib"
known_defect = (lambda test: test) if os.environ.get("WATCH_SHOW_DEFECTS") else unittest.expectedFailure
GUARDED = ("codex", "claude", "brew", "gh", "glab", "osascript", "open", "terminal-notifier", "say", "afplay")

# What every child runs first: devkit imported from this checkout, then the sandbox PATH back, and a tripwire on
# every way a child could start a guarded tool outside the stub folder.
PRELUDE = textwrap.dedent(r'''
    import json, os, shutil, subprocess, sys, time
    sys.path.insert(0, os.environ["WATCH_TEST_LIB"])
    import devkit.core  # puts Homebrew first on PATH
    os.environ["PATH"] = os.environ["WATCH_TEST_PATH"]
    _STUBS = os.path.realpath(os.environ["WATCH_TEST_STUBS"])
    _GUARDED = set(os.environ["WATCH_TEST_GUARDED"].split(","))

    def _refuse(exe):
        exe = os.fsdecode(exe or "")
        found = shutil.which(exe, path=os.environ["PATH"]) or exe
        if os.path.basename(exe) in _GUARDED and not os.path.realpath(found).startswith(_STUBS + os.sep):
            raise PermissionError("tripwire: " + exe + " outside the stub folder")

    _popen = subprocess.Popen.__init__

    def _tripwire(self, args, *a, **k):
        argv = [args] if isinstance(args, (str, bytes)) else list(args)
        first = os.fsdecode(argv[0]) if argv else ""
        _refuse(first.split()[0] if k.get("shell") and first.split() else first)
        return _popen(self, args, *a, **k)
    subprocess.Popen.__init__ = _tripwire
    for _name in ("execv", "execve", "execvp", "execvpe", "execl", "execlp"):
        _real = getattr(os, _name)
        setattr(os, _name, (lambda real: lambda path, *a: (_refuse(path), real(path, *a))[1])(_real))
    _system = os.system
    os.system = lambda cmd: (_refuse(str(cmd).split()[0] if str(cmd).split() else ""), _system(cmd))[1]

    if os.environ.get("WATCH_TEST_LEAD"):  # a setup step the tech lead types (the boundary test_progress uses)
        from devkit import progress as _pg
        _pg.caller = lambda *a, **k: {"caller": "main", "how": "test"}

    def launch(aid, kind, label, at, sid):
        # Claude Code's sequence for one subagent launch: the Agent call, the agent's start, the call's result
        from devkit import progress as pg
        pg.note_agent_launch("claude", {"session_id": sid, "tool_use_id": "toolu_" + aid,
                                        "tool_input": {"description": label, "subagent_type": kind}}, now=at)
        pg.on_subagent("claude", {"session_id": sid, "agent_id": aid, "agent_type": kind}, True, now=at)
        pg.launch_result("claude", {"session_id": sid, "tool_use_id": "toolu_" + aid,
                                    "tool_response": {"agentId": aid}}, now=at)
''')

LAUNCHER = PRELUDE + textwrap.dedent('''
    import faulthandler, signal
    faulthandler.register(signal.SIGUSR1)  # a wait that hangs shows where: kill -USR1 <pid>
    from devkit import cli
    sys.argv = ["dev"] + sys.argv[1:]
    sys.exit(cli.main(sys.argv[1:]))
''')


def until(cond, seconds=20.0, every=0.1):
    # poll a condition on the sandbox's files or processes -> its last value (falsy when it never held)
    end = time.monotonic() + seconds
    while True:
        got = cond()
        if got or time.monotonic() >= end:
            return got
        time.sleep(every)


class WatchSandbox(unittest.TestCase):
    # The sandbox: HOME, devkit's state folder, two repositories (acme/api and acme/web), the stub folder first on
    # PATH with a `dev` that runs this checkout through the launcher, and the processes the test starts.
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="watch-acc-", dir=_hermetic.home_base())).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.state = self.home / ".local" / "state" / "devkit"
        (self.state / "sessions").mkdir(parents=True)
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        self.tripwire = self.tmp / "tripwire.log"
        for tool in GUARDED:
            stub = self.stubs / tool
            stub.write_text(f"#!/bin/sh\necho \"{tool} $*\" >> '{self.tripwire}'\nexit 97\n")
            stub.chmod(0o755)
        self.launcher = self.tmp / "dev_launcher.py"
        self.launcher.write_text(LAUNCHER)
        shim = self.stubs / "dev"
        shim.write_text(f"#!/bin/sh\nexec '{sys.executable}' -I '{self.launcher}' \"$@\"\n")
        shim.chmod(0o755)
        self.repos = {name: self.repo(name) for name in ("api", "web")}
        self.repos["portfolio"] = self.home / "Development"  # a session at the portfolio level: no repository
        self.procs = []
        self.addCleanup(self.end_processes)

    def repo(self, name):
        # one empty commit with fixed dates: both repositories hold the same commit, as a fork and its upstream do
        top = self.home / "Development" / "acme" / name
        top.mkdir(parents=True)
        env = dict(self.env(), GIT_AUTHOR_DATE="2026-11-01T10:00:00Z", GIT_COMMITTER_DATE="2026-11-01T10:00:00Z")
        for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                     ["-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m",
                      "start"]):
            subprocess.run(["git", *args], cwd=top, env=env, check=True, capture_output=True)
        return top

    def env(self, session=None, **extra):
        # the suite's net (DEVKIT_TEST_PATH with this test's stubs first) on a minimal environment of the sandbox
        base = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "WATCH_TEST_LIB": str(LIB),
                "WATCH_TEST_STUBS": str(self.stubs), "WATCH_TEST_GUARDED": ",".join(GUARDED),
                "DEVKIT_NO_AUTOSYNC": "1", "DEVKIT_NO_NOTIFY": "1", "DEVKIT_NO_CI_WATCH": "1",
                "LANG": "en_US.UTF-8", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "TMPDIR": str(self.tmp)}
        if session:
            base["DEVKIT_SESSION"] = session
        env = _sandbox.env(base, stubs=[self.stubs], **extra)
        env["WATCH_TEST_PATH"] = env["PATH"]
        return env

    def end_processes(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        for p in self.procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(5)
            if p.stdout:
                p.stdout.close()
        def running():
            ps = subprocess.run(["/bin/ps", "-axo", "pid=,args="], capture_output=True, text=True).stdout
            return [ln.strip() for ln in ps.splitlines() if str(self.tmp) in ln and str(os.getpid()) != ln.split()[0]]
        # a wait in a pipeline can outlive its shell and its SIGTERM (WaitLifetimeTests shows it): end it for good
        for ln in running():
            try:
                os.kill(int(ln.split()[0]), signal.SIGKILL)
            except (OSError, ValueError):
                pass
        self.assertTrue(until(lambda: not running(), 10), f"processes left running under the sandbox: {running()}")
        self.assertFalse(self.tripwire.exists() and self.tripwire.read_text(),
                         f"a guarded tool was called: {self.tripwire.read_text() if self.tripwire.exists() else ''}")

    # ------------------------------------------------------------------ children

    def py(self, code, session=None, cwd=None, lead=True, **extra):
        # run devkit code in a child of the sandbox -> its stdout, parsed as JSON when the code printed JSON. `lead`:
        # a setup step the tech lead types (who calls is main); the waits under test never get it
        if lead:
            extra["WATCH_TEST_LEAD"] = "1"
        p = subprocess.run([sys.executable, "-I", "-c", PRELUDE + textwrap.dedent(code)], cwd=str(cwd or self.tmp),
                           env=self.env(session, **extra), capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            raise AssertionError(f"child failed ({p.returncode}):\n{p.stderr[-3000:]}\n{p.stdout[-1000:]}")
        out = p.stdout.strip()
        try:
            return json.loads(out.splitlines()[-1]) if out else None
        except ValueError:
            return out

    def spawn(self, argv, session=None, cwd=None, shell=False, **extra):
        # a real `dev …` (or a shell command line, as an agent's Bash runs it) in its own process group. A `dev` the
        # test starts itself is the tech lead's, stamped as the PreToolUse hook stamps the tech lead's calls
        # (DEVKIT_SPEAKER=main), unless it runs in a member's lane (DEVKIT_FROM_AGENT); a shell line is as given
        if not shell and "DEVKIT_FROM_AGENT" not in extra:
            extra.setdefault("DEVKIT_SPEAKER", "main")
        cmd = argv if shell else [sys.executable, "-I", str(self.launcher), *argv]
        p = subprocess.Popen(cmd, shell=shell, cwd=str(cwd or self.repos["api"]), env=self.env(session, **extra),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
                             executable="/bin/sh" if shell else None)
        self.procs.append(p)
        return p

    def finish(self, p, seconds=30):
        # -> (exit code, output) of a process that must end by itself
        try:
            out, _ = p.communicate(timeout=seconds)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGTERM)
            out, _ = p.communicate(timeout=10)
            raise AssertionError(f"the process did not end within {seconds} s:\n{out}")
        return p.returncode, out

    def stop(self, p):
        # SIGTERM a wait that must still be running (the wait ends with exit 3) -> its output
        self.assertIsNone(p.poll(), "the wait had already ended")
        os.killpg(p.pid, signal.SIGTERM)
        out, _ = p.communicate(timeout=15)
        return out

    # ------------------------------------------------------------------ state, read from the test

    def session(self, agent, sid, repo="api", **fields):
        rec = {"agent": agent, "session": sid, "started": time.time() - 3600, "project": f"acme/{repo}",
               "top": str(self.repos[repo]), "cwd": str(self.repos[repo])}
        rec.update(fields)
        (self.state / "sessions" / f"{agent}-{sid}.json").write_text(json.dumps(rec))

    def waits(self):
        d = self.state / "watch" / "live"
        return [json.loads(f.read_text()) for f in sorted(d.glob("*.json"))] if d.exists() else []

    def wait_of(self, sid, consumer=None, pid=None):
        return [w for w in self.waits() if w.get("session") == sid and (consumer is None or w.get("consumer") ==
                                                                        consumer) and (pid is None or w["pid"] == pid)]

    def armed_wait(self, p, sid):
        # the record of the real wait `p` once it blocks: it is registered, live, and has made no exit
        rec = until(lambda: [w for w in self.waits() if w.get("session") == sid and w["pid"] == p.pid], 30)
        self.assertTrue(rec, f"the wait of {sid} never registered: {p.poll()} {p.stdout.read() if p.poll() else ''}")
        return rec[0]

    def new_wait(self, before):
        # the one wait registered since `before` (a list of records), when a shell runs it under another pid
        rec = until(lambda: [w for w in self.waits() if w["id"] not in {b["id"] for b in before}], 30)
        self.assertEqual(len(rec or []), 1, rec)
        return rec[0]

    def sleeper(self):
        # a live process the test owns: the pid of a watcher or a wait that runs
        p = subprocess.Popen(["/bin/sleep", "120"], start_new_session=True)
        self.procs.append(p)
        return p.pid

    def dead_pid(self):
        p = subprocess.Popen(["/usr/bin/true"])
        p.wait()
        return p.pid

    def head(self, repo):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repos[repo], capture_output=True, text=True,
                              env=self.env()).stdout.strip()[:7]

    def watcher_ends(self, session, repo="api"):
        # what devkit's background watcher runs at its end: `dev ci watch --quiet --branch main --sha <sha>
        # --session <the session that started it>` in the repository, with the host saying the commit's run failed
        sha = self.head(repo)
        self.py(f'''
            from devkit import ciwatch, cistatus, cli
            run = {{"name": "CI", "number": 7, "id": 7, "sha": "{sha}", "status": "failed",
                    "conclusion": "failure", "url": "https://ci.example/7", "jobs": []}}
            cistatus.runs_for = lambda *a, **k: {{"provider": "github", "repo": "acme/{repo}", "runs": [run]}}
            ciwatch.failed_log = lambda *a, **k: ""  # the host's log, which only gh could read
            sys.argv = ["dev", "ci", "watch", "--quiet", "--branch", "main", "--sha", "{sha}", "--session",
                        {session!r}]
            cli.main(sys.argv[1:])
        ''', cwd=self.repos[repo])

    def crew_run(self, sid, members, repo="api"):
        # a run in session `sid` whose members work: [(agent id, devkit kind, step)]
        steps = "; ".join(f"{step}=30m" for _, _, step in members)
        names = {"devkit:tank": "Tank", "devkit:mouse": "Mouse", "devkit:seraph": "Seraph", "devkit:neo": "Neo",
                 "devkit:link": "Link"}
        self.py(f'''
            from devkit import progress as pg
            t = time.time()
            pg.start("Release", plan={steps!r}, now=t - 600, mode="guided", cwd={str(self.repos[repo])!r})
            for aid, kind, step in {members!r}:
                pg.assign(step, {names!r}[kind], why="the plan", now=t - 590, cwd={str(self.repos[repo])!r})
                launch(aid, kind, {names!r}[kind] + ": " + step, t - 500, {sid!r})
        ''', session=f"claude:{sid}", cwd=self.repos[repo])


class CrewLaneWaitTests(WatchSandbox):
    # A crew member waits on the answer to its own question with `dev watch --wait`, while the tech lead's own wait
    # runs in the background. The member's wait must take only its lane's events and never supersede the tech
    # lead's wait.
    def setUp(self):
        super().setUp()
        self.session("claude", "s1")
        self.crew_run("s1", [("a_tank", "devkit:tank", "api"), ("a_mouse", "devkit:mouse", "tests")])
        # the QA engineer asks the tech lead; the tech lead's hooks have told it already
        self.py('''
            from devkit import progress as pg, watch
            pg.crew_ask("Which fixtures may I reuse?", from_agent="a_mouse")
            watch.mail("claude", "s1", "main")
        ''', session="claude:s1")

    def member_call(self, command, aid="a_mouse", tid="toolu_m1"):
        # the member's Bash call as Claude Code hands it to devkit's PreToolUse hook: the call ledger opens it, and the
        # lane router gives back the command the shell runs
        return self.py(f'''
            from devkit import hooks, progress as pg
            data = {{"session_id": "s1", "agent_id": {aid!r}, "tool_name": "Bash", "tool_use_id": {tid!r},
                     "hook_event_name": "PreToolUse", "cwd": {str(self.repos["api"])!r},
                     "tool_input": {{"command": {command!r}}}}}
            pg.call_opened("claude", data)
            routed = hooks._route_subagent_progress(data, "claude")
            print(json.dumps((routed or data["tool_input"])["command"]))
        ''', session="claude:s1")

    def test_a_members_piped_wait_never_supersedes_the_tech_leads_wait_or_takes_its_events(self):
        # Before the fix, as seen on this Mac in the kit-consulting session: a member ran
        # `dev watch --wait --timeout 9 2>&1 | tail -4`. A pipeline is not one command, so the PreToolUse lane router
        # left it without the member's lane (hooks._watch_route), and a wait with neither the lane nor --for me was the
        # main session's (watch._consumer): the member's wait superseded the tech lead's (exit 5) and took the tech
        # lead's events. Now the member's own open call in the ledger makes the wait the member's.
        main = self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:s1")
        lead = self.armed_wait(main, "s1")
        command = self.member_call("dev watch --wait --timeout 9 2>&1 | tail -4")
        member = self.spawn(command, session="claude:s1", shell=True)
        until(lambda: member.poll() is not None or [w for w in self.waits() if w["id"] != lead["id"]], 30)
        mine = [w for w in self.waits() if w["id"] != lead["id"] and w["session"] == "s1"]
        with self.subTest("the member's wait is its lane's, or refused: never the main session's"):
            self.assertEqual([w["consumer"] for w in mine if w["consumer"] != "a_mouse"], [], mine)
        with self.subTest("the tech lead's wait is still the live one of the main session"):
            self.assertEqual(self.wait_of("s1", pid=main.pid)[0]["state"], "live")
        self.py('from devkit import progress as pg; pg.crew_ask("May I rename the amount column?", '
                'from_agent="a_tank")', session="claude:s1")
        rc, out = self.finish(main)
        with self.subTest("the Backend engineer's question wakes the tech lead's wait"):
            self.assertEqual(rc, 0, out)
            self.assertIn("May I rename the amount column?", out)
        if member.poll() is None:  # a lane's wait still waits for its own answer
            self.stop(member)

    def test_a_members_wait_run_alone_takes_only_its_answer_and_the_tech_leads_wait_only_the_leads_events(self):
        # the same wait run as a command of its own: the router gives it the member's lane, both waits stay live,
        # and each event wakes only its consumer
        main = self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:s1")
        lead = self.armed_wait(main, "s1")
        command = self.member_call("dev watch --wait --timeout 9")
        self.assertTrue(command.startswith("DEVKIT_FROM_AGENT=a_mouse "), command)
        member = self.spawn(command, session="claude:s1", shell=True)
        mine = until(lambda: [w for w in self.waits() if w["id"] != lead["id"] and w["session"] == "s1"], 30)
        with self.subTest("the member's wait is its lane's and the tech lead's stays live"):
            self.assertEqual([w["consumer"] for w in mine], ["a_mouse"])
            self.assertEqual(self.wait_of("s1", pid=main.pid)[0]["state"], "live")
        self.py('from devkit import progress as pg; pg.crew_answer("M1", "Reuse the invoice fixtures")',
                session="claude:s1")
        rc, out = self.finish(member)
        with self.subTest("the answer wakes the member's wait, once"):
            self.assertEqual(rc, 0, out)
            self.assertEqual(out.count("Reuse the invoice fixtures"), 1, out)
        with self.subTest("the tech lead's wait did not take it and still waits"):
            self.assertIsNone(main.poll())
            self.assertEqual(self.wait_of("s1", pid=main.pid)[0]["state"], "live")
        self.py('from devkit import progress as pg; pg.crew_ask("May I rename the amount column?", '
                'from_agent="a_tank")', session="claude:s1")
        rc, out = self.finish(main)
        with self.subTest("the Backend engineer's question wakes the tech lead's wait"):
            self.assertEqual(rc, 0, out)
            self.assertIn("from Backend engineer (Tank): “May I rename the amount column?”", out)
            self.assertNotIn("Reuse the invoice fixtures", out)

    def test_two_members_of_one_role_each_wake_for_their_own_answer_only(self):
        # two QA lanes at once (two QA engineers, as the crew runs today): each lane's wait takes its own answer
        self.py('''
            launch("a_mouse2", "devkit:mouse", "Mouse: fuzz", time.time() - 400, "s1")
            from devkit import progress as pg
            pg.crew_ask("Which seed should the fuzzer use?", from_agent="a_mouse2")
        ''', session="claude:s1")
        first = self.spawn(["watch", "--wait", "--for", "me", "--timeout", "1"], session="claude:s1",
                           DEVKIT_FROM_AGENT="a_mouse")
        self.armed_wait(first, "s1")
        second = self.spawn(["watch", "--wait", "--for", "me", "--timeout", "1"], session="claude:s1",
                            DEVKIT_FROM_AGENT="a_mouse2")
        self.armed_wait(second, "s1")
        with self.subTest("neither lane's wait supersedes the other"):
            self.assertEqual(sorted((w["consumer"], w["state"]) for w in self.wait_of("s1")),
                             [("a_mouse", "live"), ("a_mouse2", "live")])
        self.py('from devkit import progress as pg; pg.crew_answer("M2", "Seed 1234")', session="claude:s1")
        rc, out = self.finish(second)
        with self.subTest("the answer to the second QA lane wakes that lane only"):
            self.assertEqual(rc, 0, out)
            self.assertIn("Seed 1234", out)
            self.assertIsNone(first.poll())
        self.py('from devkit import progress as pg; pg.crew_answer("M1", "Reuse the invoice fixtures")',
                session="claude:s1")
        rc, out = self.finish(first)
        with self.subTest("the answer to the first QA lane wakes the first, without the second's"):
            self.assertEqual(rc, 0, out)
            self.assertIn("Reuse the invoice fixtures", out)
            self.assertNotIn("Seed 1234", out)


class SessionIsolationTests(WatchSandbox):
    # Four Claude Code sessions with crews: two in acme/api, one in acme/web, one at the portfolio level
    # (~/Development, no repository). Each tech lead's wait runs in the background at once.
    def setUp(self):
        super().setUp()
        for sid, repo in (("s1", "api"), ("s2", "api"), ("s3", "web"), ("s4", "portfolio")):
            self.session("claude", sid, repo)
        self.crew_run("s1", [("a_tank", "devkit:tank", "api")])
        self.crew_run("s2", [("b_neo", "devkit:neo", "parser")])
        self.crew_run("s3", [("c_link", "devkit:link", "pipeline")], repo="web")
        self.crew_run("s4", [("d_neo", "devkit:neo", "survey")], repo="portfolio")

    def arm(self, sid, repo="api"):
        p = self.spawn(["watch", "--wait", "--timeout", "1"], session=f"claude:{sid}", cwd=self.repos[repo])
        self.armed_wait(p, sid)
        return p

    def test_each_event_wakes_only_the_wait_of_its_own_session_and_a_new_wait_supersedes_only_its_own(self):
        w1, w2, w3, w4 = self.arm("s1"), self.arm("s2"), self.arm("s3", "web"), self.arm("s4", "portfolio")
        w1b = self.arm("s1")
        rc, out = self.finish(w1)
        with self.subTest("a second wait of s1 supersedes s1's first wait only"):
            self.assertEqual(rc, 5, out)
            self.assertEqual({w["session"]: w["state"] for w in self.waits() if w["pid"] in
                              (w2.pid, w3.pid, w4.pid, w1b.pid)},
                             {"s1": "live", "s2": "live", "s3": "live", "s4": "live"})
        self.py('from devkit import progress as pg; pg.crew_ask("Survey all repos or only acme?", '
                'from_agent="d_neo")', session="claude:s4", cwd=self.repos["portfolio"])
        rc, out = self.finish(w4)
        with self.subTest("a question in the portfolio session wakes its wait only"):
            self.assertEqual(rc, 0, out)
            self.assertIn("Survey all repos or only acme?", out)
            self.assertEqual((w1b.poll(), w2.poll(), w3.poll()), (None, None, None))
        self.py('from devkit import progress as pg; pg.crew_ask("Split the parser module?", from_agent="b_neo")',
                session="claude:s2", cwd=self.repos["api"])
        rc, out = self.finish(w2)
        with self.subTest("a question in s2 wakes s2's wait, once"):
            self.assertEqual(rc, 0, out)
            self.assertEqual(out.count("Split the parser module?"), 1, out)
            self.assertEqual((w1b.poll(), w3.poll()), (None, None))
        self.py('''
            from devkit import progress as pg
            pg.crew_decision("Cache the web build between jobs?", options="A: yes | B: no", recommend="A",
                             from_agent="c_link")
        ''', session="claude:s3", cwd=self.repos["web"])
        rc, out = self.finish(w3)
        with self.subTest("a decision in s3, another repository, wakes s3's wait"):
            self.assertEqual(rc, 0, out)
            self.assertIn("Cache the web build between jobs?", out)
            self.assertIsNone(w1b.poll())
        self.py('from devkit import progress as pg; pg.crew_ask("Rename the amount column?", from_agent="a_tank")',
                session="claude:s1", cwd=self.repos["api"])
        rc, out = self.finish(w1b)
        with self.subTest("s1's wait got s1's question and none of the others'"):
            self.assertEqual(rc, 0, out)
            self.assertIn("Rename the amount column?", out)
            self.assertNotIn("Split the parser module?", out)
            self.assertNotIn("Cache the web build", out)


class CodexThreadTests(WatchSandbox):
    # The Codex app runs several threads in one process, so their session records hold the same pid; Codex exports
    # no DEVKIT_SESSION. Each thread waits in the foreground with `dev watch --wait --timeout 10`. Thread x1 (acme/api)
    # started before thread x2 (acme/web); each has a CI run devkit follows for it, so events can still arrive.
    def setUp(self):
        super().setUp()
        now = time.time()
        self.session("codex", "x1", "api", pid=os.getpid(), started=now - 200)
        self.session("codex", "x2", "web", pid=os.getpid(), started=now - 100)
        for sid, repo in (("x1", "api"), ("x2", "web")):
            self.py(f'''
                from devkit import ciwatch
                ciwatch.register({{"top": {str(self.repos[repo])!r}, "ref": "main", "sha": "abc1234",
                                   "kind": "branch"}}, "devkit", "codex", {sid!r}, {self.sleeper()}, 90)
            ''')

    def open_call(self, sid, tid):
        # the thread's shell call as devkit's PreToolUse hook records it in the call ledger
        self.py(f'''
            from devkit import progress as pg
            pg.call_opened("codex", {{"session_id": {sid!r}, "tool_use_id": {tid!r}, "tool_name": "Bash",
                                      "tool_input": {{"command": "dev watch --wait --timeout 10"}}}})
        ''')

    def wait(self, repo):
        return self.spawn(["watch", "--wait", "--timeout", "10"], cwd=self.repos[repo])

    def test_a_thread_whose_call_alone_is_open_waits_for_its_own_thread(self):
        self.open_call("x1", "call_x1")
        p = self.wait("api")
        rec = self.armed_wait(p, "x1")
        self.assertEqual((rec["agent"], rec["consumer"], rec["state"]), ("codex", "main", "live"))

    def test_two_threads_of_one_codex_process_waiting_at_once_each_wait_for_their_own_thread(self):
        # Before the fix: progress.resolve_session found both threads by the shared pid, both ledgers hold an open call
        # typing these arguments, and it takes the newest (max of the matches): thread x1's wait registers as x2's,
        # supersedes x2's own wait and takes x2's events, while x1's wake nobody.
        self.open_call("x2", "call_x2")
        w2 = self.wait("web")
        self.armed_wait(w2, "x2")
        self.open_call("x1", "call_x1")
        w1 = self.wait("api")
        rec = until(lambda: [w for w in self.waits() if w["pid"] == w1.pid], 30)
        with self.subTest("x1's wait is x1's"):
            self.assertEqual([w["session"] for w in rec], ["x1"])
        with self.subTest("x2's wait is still live"):
            self.assertEqual(self.wait_of("x2", pid=w2.pid)[0]["state"], "live")


class CIWatchSessionTests(WatchSandbox):
    # devkit's background watch follows one repository, branch and commit with one process; a second session that
    # sends the same commit joins that process with a record of its own (ciwatch.start_background). Neither session
    # has a run, so the result reaches each as a CI event of its session record.
    def setUp(self):
        super().setUp()
        top = self.repos["api"]
        self.sha = self.head("api")
        self.session("claude", "s1")
        self.session("claude", "s2")
        watcher = self.sleeper()
        for sid in ("s1", "s2"):  # the hook after each session's push; the watcher runs already
            self.py(f'''
                from devkit import ciwatch
                class NoSpawn:  # never start a real background watch from a test
                    DEVNULL = -3
                    def Popen(*a, **k):
                        raise AssertionError("a background watch was started")
                ciwatch.subprocess = NoSpawn
                ciwatch.WATCH.mkdir(parents=True, exist_ok=True)
                lock = ciwatch.WATCH / (ciwatch._key("api", "main") + "-{self.sha}.lock")
                lock.write_text("{watcher} " + str(int(time.time())))
                ciwatch.start_background({str(top)!r}, session="claude:{sid}", branch="main", sha="{self.sha}")
            ''', cwd=top, DEVKIT_NO_CI_WATCH="")

    def test_a_ci_result_wakes_every_session_that_sent_the_commit(self):
        # The defect: the watcher delivers to the session that started it only (cli.cmd_ci_watch → ciwatch.deliver
        # with its --session), then settles every record on the commit. s2's Stop rule let its turn end on the one
        # wait (ciwatch._devkit_watches), and that wait never hears the result.
        w1 = self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:s1")
        w2 = self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:s2")
        self.armed_wait(w1, "s1")
        self.armed_wait(w2, "s2")
        self.watcher_ends("claude:s1")
        rc, out = self.finish(w1)
        with self.subTest("the session that started the watcher hears it"):
            self.assertEqual(rc, 0, out)
            self.assertIn(f"CI failed on main @ {self.sha}", out)
        with self.subTest("the session that joined the watcher hears it too"):
            rc, out = self.finish(w2, 20)
            self.assertEqual(rc, 0, out)
            self.assertIn(f"CI failed on main @ {self.sha}", out)


class CIRepositoryTests(WatchSandbox):
    # Two repositories hold the same commit on branches of the same name, as a fork and its upstream do: s1 sent
    # acme/api's main and s2 sent acme/web's main, each followed by its own background watcher.
    def setUp(self):
        super().setUp()
        self.assertEqual(self.head("api"), self.head("web"))
        self.session("claude", "s1", "api")
        self.session("claude", "s2", "web")
        for sid, repo in (("s1", "api"), ("s2", "web")):
            self.py(f'''
                from devkit import ciwatch
                ciwatch.register({{"top": {str(self.repos[repo])!r}, "ref": "main", "sha": "{self.head(repo)}",
                                   "kind": "branch"}}, "devkit", "claude", {sid!r}, {self.sleeper()}, 90)
            ''')

    def ci_of(self, sid):
        return self.py(f'from devkit import watch; print(json.dumps(watch.can_arrive("claude", {sid!r})))',
                       session=f"claude:{sid}", lead=False)

    @known_defect
    def test_a_result_in_one_repository_leaves_the_other_repositorys_watch_running(self):
        # The defect: ciwatch._settle closes every record whose branch and commit match, whatever its repository,
        # and ciwatch._results keys the saved results by branch and commit too. acme/api's result closes s2's watch
        # of acme/web: s2 is told nothing can arrive (and its Stop rule asks for no wait) while its own CI still runs.
        self.assertEqual(len(self.ci_of("s2")), 1)
        self.watcher_ends("claude:s1", "api")
        with self.subTest("s1's watch of acme/api has ended"):
            self.assertEqual(self.ci_of("s1"), [])
        with self.subTest("s2's watch of acme/web still runs"):
            self.assertEqual(len(self.ci_of("s2")), 1, "acme/api's result closed acme/web's watch")


class CompactionTests(WatchSandbox):
    # Claude Code gives a running conversation a new session id after a compaction: SessionStart registers the new
    # id in the same agent process (hooks._claim_process), the older record gives the pid up, and the open run moves
    # to the new id. The tech lead's wait, armed before the compaction, still runs under the old id.
    def test_a_wait_armed_before_a_compaction_ends_once_the_conversation_goes_on_under_a_new_id(self):
        # Before the fix: nothing ended the old wait. Its session is not ended (watch._ended reads only `ended`), its run
        # has moved, and a newer wait of the new id never supersedes it (another session): it stays live, able to
        # wake nobody, until its 240-minute timeout.
        self.session("claude", "old", pid=os.getpid(), started=time.time() - 3600)
        self.crew_run("old", [("a_tank", "devkit:tank", "api")])
        old = self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:old")
        rec = self.armed_wait(old, "old")
        self.session("claude", "new", started=time.time())
        moved = self.py(f'''
            from devkit import hooks, progress
            progress.agent_pid = lambda *a, **k: {os.getpid()}
            hooks._claim_process("claude", "new", progress.session_path("claude", "new"))
            print(json.dumps((progress.load_run("claude", "new") or {{}}).get("state")))
        ''', lead=False)
        self.assertEqual(moved, "running")
        ended = until(lambda: old.poll() is not None, 20)
        with self.subTest("its record is no longer live"):  # read before anything stops the old wait
            self.assertNotEqual(self.wait_of("old", pid=rec["pid"])[0]["state"], "live")
        with self.subTest("the old wait ends, with the command to arm one again"):
            self.assertTrue(ended, "the old wait still runs under the old id")
            self.assertIn("dev watch --wait", old.communicate(timeout=10)[0])


class WaitLifetimeTests(WatchSandbox):
    # How a wait ends: stopped, timed out, superseded; and what its record says afterwards.
    def setUp(self):
        super().setUp()
        self.session("claude", "s1")
        self.crew_run("s1", [("a_tank", "devkit:tank", "api")])

    def tank_asks(self):
        self.py('from devkit import progress as pg; pg.crew_ask("May I rename the amount column?", '
                'from_agent="a_tank")', session="claude:s1")

    def test_a_stopped_wait_ends_expired_and_its_event_waits_for_the_next_wait(self):
        p = self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:s1")
        rec = self.armed_wait(p, "s1")
        out = self.stop(p)
        with self.subTest("it ends with exit 3 and the command that arms it again"):
            self.assertEqual(p.returncode, 3, out)
            self.assertIn("arm again: dev watch --wait", out)
            self.assertEqual(self.wait_of("s1", pid=rec["pid"])[0]["state"], "expired")
        self.tank_asks()
        rc, out = self.finish(self.spawn(["watch", "--wait", "--timeout", "1"], session="claude:s1"))
        with self.subTest("the next wait takes the event at once"):
            self.assertEqual(rc, 0, out)
            self.assertIn("May I rename the amount column?", out)

    def test_a_stopped_wait_in_a_pipeline_ends_and_leaves_its_events_to_the_next_wait(self):
        # Before the fix: stopping `dev watch --wait 2>&1 | tail -4` (TaskStop ends the shell's process group) kills the
        # reader of the pipe first. The wait's SIGTERM handler marks its record expired, then writes its last line
        # to the closed pipe: the BrokenPipeError it raises inside kqueue.control is an OSError, which
        # watch.Changes.__call__ swallows. The wait keeps running, marked expired so no newer wait supersedes it, and
        # takes the next events into a pipe nobody reads: they are lost.
        # TaskStop signals the shell's whole process group; the order in which its processes die is the system's.
        # The test fixes the order that loses the event: the pipe's reader is gone when the wait hears SIGTERM.
        before = self.waits()
        shell = self.spawn("dev watch --wait --timeout 1 2>&1 | tail -4", session="claude:s1", shell=True,
                           DEVKIT_SPEAKER="main")
        rec = self.new_wait(before)
        # the wait blocks on its kqueue once it watches the state files: stop it there, not while it starts up
        self.assertTrue(until(lambda: "KQUEUE" in subprocess.run(["/usr/sbin/lsof", "-p", str(rec["pid"])],
                                                                 capture_output=True, text=True).stdout, 20))
        group = subprocess.run(["/bin/ps", "-o", "pid=,comm=", "-g", str(shell.pid)], capture_output=True,
                               text=True).stdout.split("\n")
        reader = [int(ln.split()[0]) for ln in group if ln.split()[1:] and ln.split()[1].endswith("tail")]
        self.assertEqual(len(reader), 1, group)
        os.kill(reader[0], signal.SIGKILL)
        os.killpg(shell.pid, signal.SIGTERM)
        shell.communicate(timeout=10)

        def gone():
            try:
                os.kill(rec["pid"], 0)
            except ProcessLookupError:
                return True
            return False
        with self.subTest("the stopped wait ends"):
            self.assertTrue(until(gone, 10), "the stopped wait still runs")
        self.tank_asks()
        until(lambda: not self.py('from devkit import watch; print(json.dumps(watch.pending("claude", "s1")))',
                                  session="claude:s1"), 8)
        rc, out = self.finish(self.spawn(["watch", "--wait", "--timeout", "0.1"], session="claude:s1"))
        with self.subTest("the question reaches the next wait"):
            self.assertEqual(rc, 0, out)
            self.assertIn("May I rename the amount column?", out)

    def test_a_wait_that_times_out_as_an_event_lands_leaves_it_for_the_next_wait(self):
        # the event is written in the wait's last moment, after its last look: the timeout must not take it
        got = self.py('''
            import io
            from devkit import progress as pg, watch
            t = [time.time()]
            clock = lambda: t[0]
            def changes(seconds):
                pg.crew_ask("May I rename the amount column?", from_agent="a_tank")
                t[0] += seconds + 1  # the deadline passes with the write
                return False
            out = io.StringIO()
            rc = watch.wait("claude", "s1", "main", 0.5, out=out, clock=clock, changes=changes)
            after = watch.pending("claude", "s1")
            out2 = io.StringIO()
            rc2 = watch.wait("claude", "s1", "main", 0.5, out=out2, changes=lambda s: False)
            print(json.dumps([rc, out.getvalue(), [e["id"] for e in after], rc2, out2.getvalue()]))
        ''', session="claude:s1", lead=False)
        rc, out, pending, rc2, out2 = got
        self.assertEqual((rc, "May I rename" in out), (3, False), out)
        self.assertEqual(len(pending), 1, pending)
        self.assertEqual((rc2, out2.count("May I rename the amount column?")), (0, 1), out2)

    def test_waits_registered_in_one_second_by_one_process_have_their_own_ids_and_consumers(self):
        # the same pid and the same second: only a wait of the same agent, session and consumer is superseded
        got = self.py('''
            from devkit import watch
            now = time.time()
            ids = [watch.register(a, s, me, 4242, 240, now=now) for a, s, me in
                   (("claude", "s1", "main"), ("claude", "s1", "a_tank"), ("claude", "s2", "main"),
                    ("codex", "s1", "main"), ("claude", "s1-and-more", "main"))]
            again = watch.register("claude", "s1", "main", 4242, 240, now=now)
            recs = {r["id"]: r["state"] for r in watch._records()}
            print(json.dumps([ids, again, recs]))
        ''', lead=False)
        ids, again, recs = got
        self.assertEqual(len(set(ids + [again])), 6, ids)
        self.assertEqual([recs[i] for i in ids] + [recs[again]],
                         ["superseded", "live", "live", "live", "live", "live"])

    def test_two_waits_of_one_consumer_registering_at_once_leave_one_live(self):
        # Before the fix: watch.register listed the live waits, marks the consumer's older ones superseded, then writes
        # its own record, with no lock around the three. Two waits of one consumer that register at once each miss
        # the other: both stay live, and the second takes events the consumer re-armed for.
        got = self.py('''
            import threading
            from devkit import watch
            real, listed, go = watch._records, threading.Event(), threading.Event()
            def records():
                out = real()
                if threading.current_thread().name == "first":
                    listed.set()
                    go.wait(10)  # the first has listed the waits; the second registers now
                return out
            watch._records = records
            first = threading.Thread(target=lambda: watch.register("claude", "s1", "main", 1111, 240), name="first")
            first.start()
            listed.wait(10)
            watch.register("claude", "s1", "main", 2222, 240)
            go.set()
            first.join(10)
            watch._records = real
            print(json.dumps(sorted(r["state"] for r in watch._records())))
        ''', lead=False)
        self.assertEqual(got, ["live", "superseded"])


class StopRuleTests(WatchSandbox):
    # The Stop rule while sessions share the state folder: s1's turn ends with a member working. Only a live wait
    # of s1's main session lets it end, never another session's wait, a member's wait, or a record whose process
    # is gone.
    def setUp(self):
        super().setUp()
        self.session("claude", "s1")
        self.session("claude", "s2")
        self.crew_run("s1", [("a_tank", "devkit:tank", "api")])

    def gate(self):
        return self.py('''
            from devkit import watch
            print(json.dumps(watch.stop_gate("claude", {"session_id": "s1", "cwd": "."})))
        ''', session="claude:s1", lead=False)

    def record(self, sid, consumer, pid):
        self.py(f'from devkit import watch; watch.register("claude", {sid!r}, {consumer!r}, {pid}, 240)', lead=False)

    def test_only_a_live_wait_of_this_sessions_main_consumer_lets_the_turn_end(self):
        self.record("s2", "main", self.sleeper())
        self.record("s1", "a_tank", self.sleeper())
        self.record("s1", "main", self.dead_pid())
        with self.subTest("another session's wait, a member's wait and a dead process do not count"):
            self.assertIn("run `dev watch --wait` with Bash run_in_background", self.gate() or "")
        listed = self.py('''
            import io
            from devkit import watch
            out = io.StringIO()
            watch.cmd_watches(type("A", (), {"json": False})(), out=out)
            print(json.dumps(out.getvalue()))
        ''', session="claude:s1", lead=False)
        with self.subTest("dev watches shows the dead one expired, with the command to arm it again"):
            rows = [ln for ln in listed.splitlines() if "session s1" in ln and "for the main session" in ln]
            self.assertEqual(len(rows), 1, listed)
            self.assertTrue(rows[0].strip().startswith("expired"), listed)
            self.assertIn("arm again: dev watch --wait\n", listed)
        self.record("s1", "main", self.sleeper())
        with self.subTest("a live wait of s1's main session lets it end"):
            self.assertIsNone(self.gate())


if __name__ == "__main__":
    unittest.main()
