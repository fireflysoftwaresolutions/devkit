# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Acceptance of what several sessions share: work items and their locks, the session logs and the handoff note,
# plans, the audit trail, presence and the session-start context, when two or three sessions (Claude Code and Codex,
# on one Mac or on two Macs that share the handoff repository's remote) act at the same moment.
#
# Each test builds a sandbox of its own: a temporary HOME per Mac with its devkit configuration, a handoff repository
# cloned from a bare remote in the sandbox (the only remote anything pushes to), and the projects acme/widget and
# acme/gadget, whose origins name GitHub but are never reached. Concurrency is real: the checkout's bin/dev runs as
# separate processes started together, each with a short timeout, and every process a test starts has ended when the
# test returns. Stand-ins for claude, codex and gh come first on PATH (bin/dev puts /opt/homebrew/bin before them, so
# the configuration names the GitHub user and gh is never needed).
#
# A test marked expectedFailure reproduces a defect that is still open: its comment names the work item. The fix
# turns it into an unexpected success, which fails the run until the marker goes.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_acceptance_shared_state
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "tests"))
import _sandbox  # noqa: E402  (the suite's net against this Mac's codex, claude, brew and gh)

DEV = TREE / "bin" / "dev"
WIDGET, GADGET = "acme/widget", "acme/gadget"
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DROP = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDE_ENV_FILE",
        "CODEX_THREAD_ID", "CODEX_HOME", "CODEX_SANDBOX", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_STATE_HOME",
        "XDG_DATA_HOME", "XDG_CACHE_HOME", "GIT_DIR", "GIT_WORK_TREE", "DEV_ROOT", "GH_CONFIG_DIR")
TIMEOUT = 90  # seconds one dev process may take; a test fails rather than hang
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com", "GIT_TERMINAL_PROMPT": "0"}
GITATTRIBUTES = ("sessions/** merge=union\npresence/*.json merge=ours\nPROJECTS.md merge=ours\nTASKS.md merge=ours\n"
                 "ACTIVITY.md merge=ours\nregistry.json merge=ours\n")  # what dev setup writes (handoff_merge_rules)


def git(cwd, *args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60,
                       env={**os.environ, **GIT_ENV}, stdin=subprocess.DEVNULL)
    if check and r.returncode:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {r.stderr or r.stdout}")
    return r.stdout


class Mac:
    # One developer's Mac in the sandbox: its HOME, devkit configuration, a clone of the shared handoff repository,
    # and the projects. run() and start() launch the checkout's bin/dev there as a session would.

    def __init__(self, world, name: str, user: str):
        self.world, self.user = world, user
        self.home = world.base / name
        self.dev = self.home / "Development"
        self.handoff = self.dev / ".handoff"
        self.state = self.home / ".local" / "state" / "devkit"
        cfg = self.home / ".config" / "devkit" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"github_user": user, "handoff_repo": f"{user}/handoff"}))
        self.dev.mkdir(parents=True)
        git(self.dev, "clone", "-q", str(world.remote), str(self.handoff))
        git(self.handoff, "config", "merge.ours.driver", "true")
        self.projects = {}
        for pid in (WIDGET, GADGET):
            p = self.dev / pid
            p.mkdir(parents=True)
            git(p, "init", "-q", "-b", "main")
            (p / "README.md").write_text(f"# {pid}\n")
            git(p, "add", "-A")
            git(p, "commit", "-q", "-m", "init")
            git(p, "remote", "add", "origin", f"https://github.com/{pid}.git")
            git(p, "update-ref", "refs/remotes/origin/main", "HEAD")  # as a clone has it: main is the default
            self.projects[pid] = p
        base = {k: v for k, v in os.environ.items() if k not in DROP and not k.startswith("DEVKIT_")}
        self.env = _sandbox.env(base, stubs=[world.stubs], **GIT_ENV, HOME=str(self.home), USER=user, NO_COLOR="1",
                                TERM="dumb", COLUMNS="200", DEVKIT_NO_NOTIFY="1", DEVKIT_NO_AUTOSYNC="1",
                                DEVKIT_HOME=str(TREE), GH_TOKEN="", GITHUB_TOKEN="",
                                GH_CONFIG_DIR=str(self.home / ".config" / "gh"))

    def session_env(self, agent: str = "", sid: str = "", **extra) -> dict:
        # What a session's shell carries: Claude Code exports DEVKIT_SESSION and DEVKIT_AGENT (devkit's SessionStart
        # hook writes them to CLAUDE_ENV_FILE); a Codex thread's shell carries only CODEX_THREAD_ID.
        env = {}
        if agent == "codex":
            env.update(CODEX_THREAD_ID=sid)
        elif agent:
            env.update(DEVKIT_AGENT=agent, DEVKIT_SESSION=f"{agent}:{sid}")
        env.update(extra)
        return env

    def argv(self, args) -> list:
        return [sys.executable, "-I", "-S", str(DEV), *map(str, args)]

    def run(self, *args, session=(), cwd=None, env=None) -> subprocess.CompletedProcess:
        r = subprocess.run(self.argv(args), cwd=str(cwd or self.projects[WIDGET]), capture_output=True, text=True,
                           timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                           env={**self.env, **self.session_env(*session), **(env or {})})
        r.stdout, r.stderr = ANSI.sub("", r.stdout), ANSI.sub("", r.stderr)
        return r

    def ok(self, *args, **kw) -> str:
        r = self.run(*args, **kw)
        if r.returncode:
            raise AssertionError(f"dev {' '.join(map(str, args))}: exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r.stdout + r.stderr

    def together(self, calls) -> list:
        # Start every call at once (each: (args, session, cwd, env)), wait for all of them, and return their
        # CompletedProcess results in order. A call still running after TIMEOUT is killed and the test fails.
        procs = []
        try:
            for args, session, cwd, env in calls:
                procs.append(subprocess.Popen(self.argv(args), cwd=str(cwd or self.projects[WIDGET]),
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                              stdin=subprocess.DEVNULL,
                                              env={**self.env, **self.session_env(*session), **(env or {})}))
            out = []
            for (args, *_), p in zip(calls, procs):
                so, se = p.communicate(timeout=TIMEOUT)
                out.append(subprocess.CompletedProcess(args, p.returncode, ANSI.sub("", so), ANSI.sub("", se)))
            return out
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def script(self, code: str) -> list:
        # argv of a Python process of this Mac (its HOME) that runs `code` with devkit importable as `devkit`. After
        # the import, PATH is set back to the stand-ins first, so nothing the code runs reaches the real gh, claude
        # or codex.
        head = textwrap.dedent(f"""\
            import os, sys
            sys.path.insert(0, {str(TREE / 'lib')!r})
            from devkit import core
            os.environ["PATH"] = {self.env['PATH']!r}
            """)
        return [sys.executable, "-I", "-c", head + textwrap.dedent(code)]

    def pythons(self, codes, env=None) -> list:
        # Run every code at once, each in its own process (see script); -> their stdout, in order. A process that
        # fails, or runs past TIMEOUT, fails the test; every process has ended when this returns.
        procs = []
        try:
            for code in codes:
                procs.append(subprocess.Popen(self.script(code), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                              text=True, env={**self.env, **(env or {})}, stdin=subprocess.DEVNULL,
                                              cwd=str(self.projects[WIDGET])))
            out = []
            for p in procs:
                so, se = p.communicate(timeout=TIMEOUT)
                if p.returncode:
                    raise AssertionError(f"python on {self.user}'s Mac: exit {p.returncode}\n{so}\n{se}")
                out.append(so)
            return out
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()

    def python(self, code: str, env=None) -> str:
        return self.pythons([code], env=env)[0]

    def session_record(self, agent: str, sid: str, pid: str = WIDGET, **fields) -> Path:
        # A live session's record, as devkit's SessionStart hook writes it: the checkout it works in, when it began
        # and its last activity.
        top = self.projects[pid]
        branch = git(top, "branch", "--show-current").strip()
        rec = {"agent": agent, "session": sid, "top": str(top), "cwd": str(top), "project": pid, "branch": branch,
               "started": time.time() - 60, "last_seen": time.time(), "pid": os.getpid(),
               "head": git(top, "rev-parse", "HEAD").strip(), **fields}
        f = self.state / "sessions" / f"{agent}-{sid}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec))
        return f

    def item(self, iid: str) -> str:
        return next(self.handoff.glob(f"items/**/{iid}.md")).read_text()

    def add(self, title: str, pid: str = WIDGET) -> str:
        out = self.ok("task", "add", title, "--now", "--why", "acceptance", "-p", pid)
        return re.search(r"\b([TFBS]-[0-9a-z]+)\b", out).group(1)

    def sync(self, *extra, session=()) -> subprocess.CompletedProcess:
        # dev sync against the sandbox remote: the one command here that pushes
        return self.run("sync", "--quiet", *extra, session=session, env={"DEVKIT_NO_AUTOSYNC": ""})


class World:
    # The shared part: a bare handoff remote with the files dev setup puts in a new handoff repository, and the
    # stand-ins of claude, codex and gh.

    def __init__(self):
        # below /var/tmp, not $TMPDIR: devkit treats a checkout in a temporary folder as scratch and records no
        # session line for it (core.is_temp), as tests/_hermetic.py explains for the run's home
        base = "/var/tmp" if os.access("/var/tmp", os.W_OK) else None
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="devkit-shared-", dir=base)))
        self.remote = self.base / "handoff-remote.git"
        git(self.base, "init", "-q", "--bare", "-b", "main", str(self.remote))
        seed = self.base / "seed"
        git(self.base, "clone", "-q", str(self.remote), str(seed))
        (seed / ".gitignore").write_text(".DS_Store\n*.tmp\n")
        (seed / ".gitattributes").write_text(GITATTRIBUTES)
        for d in ("projects", "plans", "log"):
            (seed / d).mkdir()
            (seed / d / ".gitkeep").touch()
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "handoff repository")
        git(seed, "push", "-q", "origin", "main")
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.trips = self.base / "trips.log"
        for tool in ("claude", "codex", "gh"):
            (self.stubs / tool).write_text(f"#!/bin/sh\necho \"{tool} $*\" >> '{self.trips}'\nexit 1\n")
            (self.stubs / tool).chmod(0o755)
        self.macs = []

    def mac(self, user: str) -> Mac:
        m = Mac(self, f"mac-{user}", user)
        self.macs.append(m)
        return m

    def remote_file(self, rel: str) -> str:
        return git(self.remote, "show", f"main:{rel}")

    def remove(self):
        shutil.rmtree(self.base, ignore_errors=True)


class Shared(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.addCleanup(self.world.remove)
        self.ana = self.world.mac("ana")

    def lines_of(self, text: str, pattern: str) -> list:
        return [ln for ln in text.splitlines() if re.search(pattern, ln)]


# --------------------------------------------------------------------------- work items and their locks

class WorkItemLocks(Shared):
    def test_two_macs_starting_one_item_at_once_leave_it_held_by_exactly_one(self):
        # Ana and Bob start the same item in the same second from two Macs that share the handoff remote: one wins,
        # the other is refused with the holder's name, and the remote holds one claim, the winner's.
        bob = self.world.mac("bob")
        iid = self.ana.add("Export invoices")
        self.assertEqual(self.ana.sync().returncode, 0)
        git(bob.handoff, "pull", "-q")
        pa = subprocess.Popen(self.ana.argv(["task", "start", iid]), cwd=str(self.ana.projects[WIDGET]),
                              env={**self.ana.env, **self.ana.session_env("claude", "s-ana")},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
        pb = subprocess.Popen(bob.argv(["task", "start", iid]), cwd=str(bob.projects[WIDGET]),
                              env={**bob.env, **bob.session_env("codex", "t-bob")},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
        try:
            a = (pa.communicate(timeout=TIMEOUT), pa.returncode)
            b = (pb.communicate(timeout=TIMEOUT), pb.returncode)
        finally:
            for p in (pa, pb):
                if p.poll() is None:
                    p.kill()
                    p.wait()
        codes = sorted([a[1], b[1]])
        self.assertEqual(codes[0], 0, f"neither start succeeded:\n{a}\n{b}")
        self.assertNotEqual(codes[1], 0, f"both starts reported success:\n{a}\n{b}")
        winner, loser = ("ana", b) if a[1] == 0 else ("bob", a)
        self.assertIn("claimed by", loser[0][0] + loser[0][1])
        remote = self.world.remote_file(f"items/{WIDGET}/{iid}.md")
        self.assertRegex(remote, rf"(?m)^claimed_by: {winner}@")
        self.assertEqual(len(self.lines_of(remote, r"· claim: lock until")), 1, remote)

    def test_two_sessions_finishing_one_item_at_once_close_it_once(self):
        # A Claude Code session and a Codex thread on one Mac both run `dev task done` on the item they share: it is
        # done, and its changelog says so once.
        iid = self.ana.add("Write the release notes")
        self.ana.ok("task", "start", iid, session=("claude", "s1"))
        res = self.ana.together([(("task", "done", iid, "--why", "notes merged"), ("claude", "s1"), None, None),
                                 (("task", "done", iid, "--why", "notes merged"), ("codex", "t1"), None, None)])
        self.assertIn(0, [r.returncode for r in res], res)
        text = self.ana.item(iid)
        self.assertRegex(text, r"(?m)^status: done$")
        self.assertEqual(len(self.lines_of(text, r"· done(:| —|$)")), 1, text)
        self.assertEqual(git(self.ana.handoff, "status", "--porcelain", "--", "items"), "")  # every change committed

    def test_a_lock_renews_for_the_session_on_its_branch_and_not_for_one_on_another(self):
        # A Claude Code session works on feat/export of acme/widget and a Codex thread on acme/gadget. Of two items
        # whose locks run out within the hour, `dev sync --renew` renews the one started on feat/export and leaves
        # the one started on feat/other, a branch no live session is on any more.
        widget = self.ana.projects[WIDGET]
        git(widget, "checkout", "-q", "-b", "feat/other")
        other = self.ana.add("Item on the other branch")
        self.ana.ok("task", "start", other, session=("claude", "s1"))
        git(widget, "checkout", "-q", "-b", "feat/export")
        export = self.ana.add("Export invoices")
        self.ana.ok("task", "start", export, session=("claude", "s1"))
        self.ana.session_record("claude", "s1")
        self.ana.session_record("codex", "t1", pid=GADGET)
        soon = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        for iid in (export, other):
            f = next(self.ana.handoff.glob(f"items/**/{iid}.md"))
            f.write_text(re.sub(r"(?m)^lock_expires: .*$", f"lock_expires: {soon}", f.read_text()))
        r = self.ana.run("sync", "--quiet", "--offline", "--no-push", "--renew", "claude")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        expires = {iid: re.search(r"(?m)^lock_expires: (\S+)$", self.ana.item(iid)).group(1)
                   for iid in (export, other)}
        self.assertGreater(expires[export], soon)
        self.assertEqual(expires[other], soon)

    def test_two_sessions_reconciling_at_once_send_a_lockless_item_back_to_todo_once(self):
        # An item is doing with no lock (its session ended without releasing it); a Claude Code session and a Codex
        # thread run `dev task reconcile --apply` in the same second: the item is todo, with one changelog entry
        # for it.
        iid = self.ana.add("Export invoices")
        f = next(self.ana.handoff.glob(f"items/**/{iid}.md"))
        f.write_text(re.sub(r"(?m)^status: todo$", "status: doing", f.read_text()))
        git(self.ana.handoff, "commit", "-q", "-am", "an item left doing")
        res = self.ana.together([(("task", "reconcile", "--apply"), ("claude", "s1"), None, None),
                                 (("task", "reconcile", "--apply"), ("codex", "t1"), None, None)])
        self.assertIn(0, [r.returncode for r in res], "\n".join(r.stdout + r.stderr for r in res))
        text = self.ana.item(iid)
        self.assertRegex(text, r"(?m)^status: todo$")
        self.assertEqual(len(self.lines_of(text, r"^- v\d+ · ")), 2, text)  # the creation and the reconcile

# --------------------------------------------------------------------------- plans

class Plans(Shared):
    def test_sessions_revising_one_plan_at_once_lose_no_revision(self):
        # Four sessions run `dev plan revise` on the same plan in the same second, round after round (up to ten,
        # stopping at the first loss): every revision a session was told succeeded is in the plan's changelog, each
        # with a version of its own.
        self.ana.ok("plan", "new", "Invoice export")
        plan = next(self.ana.handoff.glob(f"plans/{WIDGET}/*-invoice-export.md"))
        sessions = (("claude", "s1"), ("codex", "t1"), ("claude", "s2"), ())
        told = []
        for rnd in range(10):
            res = self.ana.together([(("plan", "revise", plan, "--why", f"round {rnd} by {n}"), s, None, None)
                                     for n, s in enumerate(sessions)])
            told += [f"{rnd}.{n}" for n, r in enumerate(res) if r.returncode == 0 and re.search(r"\bv\d+ ·", r.stdout)]
            text = plan.read_text()
            kept = re.findall(r"(?m)^- v\d+ · .* · revised.* — why: round (\d+) by (\d)$", text)
            versions = re.findall(r"(?m)^- v(\d+) · .* · revised", text)
            self.assertEqual(len(versions), len(set(versions)), f"two revisions share a version:\n{text}")
            self.assertEqual(sorted(told), sorted(f"{r}.{n}" for r, n in kept),
                             f"round {rnd}: told {told}, the plan keeps {kept}")

    def test_sessions_revising_one_plan_at_once_take_turns_and_all_succeed(self):
        # Four sessions revise the same plan in the same second: each waits for the one before it instead of being
        # refused, so every revision succeeds, with a version of its own
        self.ana.ok("plan", "new", "Invoice export")
        plan = next(self.ana.handoff.glob(f"plans/{WIDGET}/*-invoice-export.md"))
        sessions = (("claude", "s1"), ("codex", "t1"), ("claude", "s2"), ())
        for rnd in range(3):
            res = self.ana.together([(("plan", "revise", plan, "--why", f"round {rnd} by {n}"), s, None, None)
                                     for n, s in enumerate(sessions)])
            self.assertEqual([r.returncode for r in res], [0, 0, 0, 0],
                             "\n".join(r.stdout + r.stderr for r in res if r.returncode))
        versions = re.findall(r"(?m)^- v(\d+) · .* · revised", plan.read_text())
        self.assertEqual(sorted(int(v) for v in versions), list(range(2, 14)))


# --------------------------------------------------------------------------- the audit trail

class AuditTrail(Shared):
    def records(self, mac: Mac) -> list:
        f = mac.state / "audit" / f"{time.strftime('%Y-%m')}.jsonl"
        return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]

    def item_paths(self, mac: Mac) -> dict:
        # title -> the item's path in the handoff repository, the target of its audit records
        return {re.search(r"(?m)^title: (.*)$", f.read_text()).group(1): str(f.relative_to(mac.handoff))
                for f in mac.handoff.glob("items/**/*.md")}

    def test_concurrent_writes_from_two_sessions_keep_one_chain(self):
        # Claude Code and Codex add items in the same second, three each: the audit trail verifies whole, with one
        # record of each item that was added.
        res = self.ana.together([(("task", "add", f"{who} item {i}", "--why", "acceptance"), s, None, None)
                                 for i in range(3)
                                 for who, s in (("claude", ("claude", "s1")), ("codex", ("codex", "t1")))])
        added = sum(r.returncode == 0 for r in res)
        r = self.ana.run("audit", "verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        items = set(self.item_paths(self.ana).values())
        self.assertEqual(len(items), added)
        created = [x for x in self.records(self.ana) if x.get("action") == "handoff.items" and x.get("target") in items]
        self.assertEqual(len(created), added)

    @unittest.expectedFailure  # open defect B-g3dzv3864m1: a Codex thread's records name no agent and no session
    def test_each_sessions_records_name_its_own_agent_and_session(self):
        # A Claude Code session and a Codex thread add one item each at once: the audit record of each item's commit
        # names the agent and the session that made it.
        self.ana.together([(("task", "add", "claude's item", "--why", "acceptance"), ("claude", "s1"), None, None),
                           (("task", "add", "codex's item", "--why", "acceptance"), ("codex", "t1"), None, None)])
        recs, paths = self.records(self.ana), self.item_paths(self.ana)
        mine = {who: next(r for r in recs
                          if r.get("action") == "handoff.items" and r.get("target") == paths[f"{who}'s item"])
                for who in ("claude", "codex")}
        self.assertEqual((mine["claude"]["actor"]["agent"], mine["claude"]["actor"]["session"]), ("claude", "s1"))
        self.assertEqual((mine["codex"]["actor"]["agent"], mine["codex"]["actor"]["session"]), ("codex", "t1"))


# --------------------------------------------------------------------------- session logs and the handoff note

class SessionLogs(Shared):
    def month_file(self, mac: Mac, pid: str = WIDGET) -> Path:
        return mac.handoff / "sessions" / pid / f"{time.strftime('%Y-%m')}.md"

    def test_sessions_logging_at_once_keep_every_line_and_commit_each(self):
        # Three sessions (Claude Code, Codex, a terminal) run `dev handoff --log` in the same second, the first lines
        # of the month in a project whose note does not exist yet: every command succeeds, the month's log has each
        # line once, and each is committed.
        sessions = [("claude", "s1"), ("codex", "t1"), ()]
        res = self.ana.together([(("handoff", "--log", f"line from session {i}"), s, None, None)
                                 for i, s in enumerate(sessions)])
        self.assertEqual([r.returncode for r in res], [0, 0, 0], "\n".join(r.stdout + r.stderr for r in res))
        text = self.month_file(self.ana).read_text()
        for i in range(3):
            self.assertEqual(len(self.lines_of(text, rf"line from session {i}$")), 1, text)
        self.assertEqual(git(self.ana.handoff, "status", "--porcelain", "--", "sessions"), "")
        self.assertEqual(git(self.ana.handoff, "show", f"HEAD:sessions/{WIDGET}/{time.strftime('%Y-%m')}.md"), text)

    def test_a_session_line_rewritten_at_stop_never_drops_another_sessions_line(self):
        # Session A's Stop hook rewrites its own line of the month's log on every turn (hooks.record_session) while
        # session B logs its lines (work.log_session, the path `dev handoff --log` takes): every line B wrote is kept.
        self.ana.session_record("claude", "sA", status="first")
        self.ana.python("""
            from devkit import hooks
            hooks.record_session("claude:sA")
            """)
        done = self.world.base / "a-stopped"
        stop_a = f"""
            import json
            from pathlib import Path
            from devkit import hooks
            f = Path(core.STATE) / "sessions" / "claude-sA.json"
            for i in range(40):
                rec = json.loads(f.read_text()); rec["status"] = f"turn {{i}}"; f.write_text(json.dumps(rec))
                hooks.record_session("claude:sA")
            Path({str(done)!r}).touch()
            """
        log_b = f"""
            from pathlib import Path
            from devkit import work
            n = 0
            while not Path({str(done)!r}).exists():
                work.log_session({WIDGET!r}, f"bob line {{n}}")
                n += 1
            print(n)
            """
        written = int(self.ana.pythons([stop_a, log_b])[1])
        text = self.month_file(self.ana).read_text()
        kept = len(self.lines_of(text, r"bob line \d+$"))
        self.assertGreater(written, 100)
        self.assertEqual(kept, written, f"{written - kept} of session B's {written} lines were lost")
        self.assertEqual(len(self.lines_of(text, r"<!-- s:sA -->")), 1, text)

    def test_two_macs_logging_sessions_reach_the_remote_without_a_stuck_sync(self):
        # Ana and Bob each log a session of acme/widget on their own Mac (Bob works on a branch of his own, so each
        # Mac's generated block of the note differs, as two Macs' always do) and both run `dev sync`, twice: the
        # remote's log of the month has both lines, and neither Mac is left with commits it could not push, a sync
        # problem, a stopped rebase or conflict markers.
        bob = self.world.mac("bob")
        git(bob.projects[WIDGET], "checkout", "-q", "-b", "feat/bob-only")
        self.ana.ok("handoff", "--log", "ana finished the export", session=("claude", "s-ana"))
        bob.ok("handoff", "--log", "bob reviewed the export", session=("codex", "t-bob"))
        for mac in (self.ana, bob, self.ana, bob):
            r = mac.sync()
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        remote = self.world.remote_file(f"sessions/{WIDGET}/{time.strftime('%Y-%m')}.md")
        self.assertEqual(len(self.lines_of(remote, r"ana finished the export$")), 1, remote)
        self.assertEqual(len(self.lines_of(remote, r"bob reviewed the export$")), 1, remote)
        for mac in (self.ana, bob):
            failed = mac.state / "handoff-push-failed.json"
            self.assertFalse(failed.exists(), failed.read_text() if failed.exists() else "")
            self.assertEqual(git(mac.handoff, "rev-list", "--count", "@{u}..HEAD").strip(), "0")
            self.assertFalse((mac.handoff / ".git" / "rebase-merge").exists())
            self.assertFalse((mac.handoff / ".git" / "rebase-apply").exists())
            self.assertEqual(git(mac.handoff, "diff", "--name-only", "--diff-filter=U"), "")
            self.assertNotRegex(git(mac.handoff, "grep", "-l", "-e", "^<<<<<<< ", check=False), r"\S")


# --------------------------------------------------------------------------- presence and the session-start context

class PresenceAndContext(Shared):
    def test_bob_sees_anas_live_sessions_of_both_projects_and_not_the_one_that_ended(self):
        # Ana runs a Claude Code session in acme/widget and a Codex thread in acme/gadget; after both Macs sync, Bob's
        # `dev team` shows both, each with its project. Ana's Claude session ends; after the next syncs Bob's view
        # shows the Codex thread only.
        bob = self.world.mac("bob")
        self.ana.session_record("claude", "s1")
        self.ana.session_record("codex", "t1", pid=GADGET)
        for mac in (self.ana, bob):
            self.assertEqual(mac.sync().returncode, 0)
        team = bob.ok("team")
        self.assertRegex(team, r"ana@\S+")
        self.assertRegex(team, rf"claude · {WIDGET}@main")
        self.assertRegex(team, rf"codex · {GADGET}@main")
        r = self.ana.sync("--session", "claude:s1", "--finish")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(bob.sync().returncode, 0)
        team = bob.ok("team")
        self.assertNotRegex(team, rf"claude · {WIDGET}@")
        self.assertRegex(team, rf"codex · {GADGET}@main")

    def test_the_dashboard_flags_two_sessions_on_one_branch(self):
        # Two Claude Code sessions of Ana work on acme/widget@main at once, a third on acme/gadget: after `dev sync`
        # the dashboard's collisions name widget@main, with both sessions, and not gadget.
        self.ana.session_record("claude", "s1")
        self.ana.session_record("claude", "s2")
        self.ana.session_record("codex", "t1", pid=GADGET)
        self.assertEqual(self.ana.sync().returncode, 0)
        out = self.ana.python(f"""
            import json
            from pathlib import Path
            from devkit import dashboard
            team = dashboard.read_presence(Path({str(self.ana.handoff)!r}), {{}})
            print(json.dumps(dashboard.collisions(team, [])))
            """)
        same = [c for c in json.loads(out) if c["kind"] == "same-branch"]
        self.assertEqual([(c["project"], c["branch"]) for c in same], [(WIDGET, "main")], out)

    def test_each_sessions_start_context_names_its_own_project_items_and_status(self):
        # Two sessions start at once, one in acme/widget and one in acme/gadget, each project with an item of its
        # own and widget with a saved status: each context names its own project and item, and only widget's names
        # widget's status.
        self.ana.add("Widget work")
        self.ana.add("Gadget work", pid=GADGET)
        self.ana.python(f"""
            from devkit import canary
            canary.save_status({WIDGET!r}, "claude", "widget · main ❯ the widget export ❯ ✓ 12 tests ❯ next → ship",
                               "main", {str(self.ana.projects[WIDGET])!r})
            """)
        ctx = """
            from devkit import hooks
            print(hooks.context_for({top!r}, {agent!r}))
            """
        widget, gadget = self.ana.pythons([ctx.format(top=str(self.ana.projects[WIDGET]), agent="claude"),
                                           ctx.format(top=str(self.ana.projects[GADGET]), agent="codex")])
        self.assertIn(f"Project: **{WIDGET}**", widget)
        self.assertIn("Widget work", widget)
        self.assertIn("the widget export", widget)
        self.assertIn(f"Project: **{GADGET}**", gadget)
        self.assertIn("Gadget work", gadget)
        for leak in ("Widget work", "the widget export", f"**{WIDGET}**"):
            self.assertNotIn(leak, gadget)
        self.assertNotIn("Gadget work", widget)


# --------------------------------------------------------------------------- initiatives and portfolio sessions

class Initiatives(Shared):
    def setUp(self):
        super().setUp()
        readme = self.ana.handoff / "initiatives" / "platform" / "README.md"
        readme.parent.mkdir(parents=True)
        readme.write_text(f"---\nkind: initiative\ntitle: Platform\nprojects: [{WIDGET}]\n---\n"
                          "# Platform\n\n## Tasks\n")
        git(self.ana.handoff, "add", "-A")
        git(self.ana.handoff, "commit", "-q", "-m", "initiative platform")

    def rulings(self, cwd: Path, calls) -> list:
        # the guard's ruling on each (tool, input) of a Claude Code session working in `cwd`: "allow" or "deny"
        out = self.ana.python(f"""
            import json
            from devkit import guard
            for tool, inp in {calls!r}:
                r = guard.check(tool, inp, {str(cwd)!r}, "claude")
                print("allow" if not r else r[0])
            """)
        return out.split()

    def test_only_a_member_projects_session_adds_work_to_the_initiative(self):
        # acme/widget is a member of the initiative, acme/gadget is not: a session in widget may add an item to it
        # and edit its README; one in gadget may do neither.
        calls = [("Bash", {"command": "dev task add 'shared contract' -i platform --why contracts"}),
                 ("Write", {"file_path": str(self.ana.handoff / "initiatives" / "platform" / "README.md")})]
        self.assertEqual(self.rulings(self.ana.projects[WIDGET], calls), ["allow", "allow"])
        self.assertEqual(self.rulings(self.ana.projects[GADGET], calls), ["deny", "deny"])

    def test_a_member_projects_session_writes_no_other_projects_plans_or_log(self):
        # A session in acme/widget may write its own plans and session log through dev, and the guard refuses the
        # same commands aimed at acme/gadget, as it refuses a direct write of gadget's note.
        own = [("Bash", {"command": f"dev plan new 'Export design' -p {WIDGET}"}),
               ("Bash", {"command": f"dev handoff {WIDGET} --log 'finished the export'"})]
        other = [("Bash", {"command": f"dev plan new 'Export design' -p {GADGET}"}),
                 ("Bash", {"command": f"dev handoff {GADGET} --log 'finished the export'"}),
                 ("Write", {"file_path": str(self.ana.handoff / "projects" / "acme" / "gadget.md")})]
        self.assertEqual(self.rulings(self.ana.projects[WIDGET], own), ["allow", "allow"])
        self.assertEqual(self.rulings(self.ana.projects[WIDGET], other), ["deny", "deny", "deny"])

    def test_a_portfolio_session_sees_every_project_and_logs_into_none_unnamed(self):
        # A session started in ~/Development (the portfolio level) sees both projects and the initiative in
        # `dev overview`; `dev handoff --log` with no project is refused and writes no project's log.
        out = self.ana.ok("overview", cwd=self.ana.dev, session=("claude", "p1"))
        for name in (WIDGET, GADGET, "Platform"):
            self.assertIn(name, out)
        r = self.ana.run("handoff", "--log", "portfolio work", cwd=self.ana.dev, session=("claude", "p1"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not inside a project", r.stdout + r.stderr)
        self.assertEqual(list(self.ana.handoff.glob("sessions/**/*.md")), [])


class SharedFileWrites(Shared):
    def test_two_processes_regenerating_one_note_at_once_never_fail_or_leave_a_torn_note(self):
        # Two processes regenerate the same project note at the same moment (a session start and `dev sync`, two
        # sessions starting): neither fails, and the note is always one writer's whole text.
        note = self.ana.handoff / "projects" / "acme" / "widget.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        writer = """
            from pathlib import Path
            text = "{who}\\n" * 4000
            errors = 0
            for i in range(300):
                try:
                    core.write_if_changed(Path({note!r}), text + str(i) + "\\n")
                except OSError as e:
                    errors += 1
            print(errors)
            """
        out = self.ana.pythons([writer.format(who="ana", note=str(note)), writer.format(who="bob", note=str(note))])
        self.assertEqual([o.strip() for o in out], ["0", "0"], "writes that failed, per process")
        lines = note.read_text().splitlines()
        self.assertEqual(len(set(lines[:-1])), 1, f"a torn note mixes both writers' text: {set(lines[:-1])}")
        self.assertEqual(len(lines), 4001)


if __name__ == "__main__":
    unittest.main()
