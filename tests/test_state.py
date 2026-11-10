# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The session and run records change only through devkit's state helper (lib/devkit/state.py): one lock per
session, a fresh read under it, an atomic replace, and a three-way patch for code that reads early and writes late.
Run: python3 -m unittest tests.test_state"""
import ast
import contextlib
import io
import json
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
LIB = TESTS.parent / "lib"
sys.path.insert(0, str(LIB))

from devkit import audit, canary, hooks, scope  # noqa: E402
from devkit import progress as pg  # noqa: E402

sys.path.insert(0, str(TESTS))
from test_progress import boundary  # noqa: E402

# A SubagentStop hook in its own process, as Claude Code runs it: the session's record is read at the start, and the
# report check (made slow here) runs before the hook writes. Every process reads before any of them writes.
CHILD = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root, aid = Path(sys.argv[2]), sys.argv[3]
from devkit import audit, canary, core, hooks, progress as pg
pg.RUNS, pg.SESSIONS, pg.TIMING = root / "runs", root / "sessions", root / "timing"
pg.PENDING = pg.TIMING / "pending"
audit.AUDIT, hooks.STATE = root / "audit", root
hooks._session_file = lambda agent, sid: pg.SESSIONS / f"{agent}-{sid}.json"
core.CFG["canary"] = {"enforce": True}
def slow_check(*a, **k):
    time.sleep(0.4)
    return False
canary.check_subagent = slow_check
(root / f"ready-{aid}").touch()
while not (root / "go").exists():
    time.sleep(0.005)
hooks.run("subagent-stop", "claude")
"""

# `dev crew ask` in its own process: it reads the run, adds the message (made slow here) and saves the run.
CHILD_ASK = r"""
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
root, n = Path(sys.argv[2]), sys.argv[3]
from devkit import audit, progress as pg
pg.RUNS, pg.SESSIONS, pg.TIMING = root / "runs", root / "sessions", root / "timing"
pg.PENDING = pg.TIMING / "pending"
audit.AUDIT = root / "audit"
post = pg._post
def slow_post(*a, **k):
    time.sleep(0.2)
    return post(*a, **k)
pg._post = slow_post
(root / f"ready-{n}").touch()
while not (root / "go").exists():
    time.sleep(0.005)
pg.crew_ask(f"question {n}", from_agent=f"a{n}")
"""


class Records(unittest.TestCase):
    # A temporary devkit state folder with one session, claude:s1.
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.p = [mock.patch.object(pg, "RUNS", self.root / "runs"),
                  mock.patch.object(pg, "SESSIONS", self.root / "sessions"),
                  mock.patch.object(pg, "TIMING", self.root / "timing"),
                  mock.patch.object(pg, "PENDING", self.root / "timing" / "pending"),
                  mock.patch.object(pg, "_publish", lambda: None),
                  mock.patch.object(audit, "AUDIT", self.root / "audit"),
                  mock.patch.object(canary, "LOG", self.root / "canary.jsonl"),
                  mock.patch.object(hooks, "STATE", self.root),
                  mock.patch.object(hooks, "_spawn_sync", lambda args: None),
                  mock.patch.object(hooks, "_session_file", lambda agent, sid: pg.SESSIONS / f"{agent}-{sid}.json"),
                  *boundary(self.root),  # Morpheus's calls, devkit's default configuration
                  mock.patch.dict(os.environ, {"DEVKIT_SESSION": "claude:s1", "DEVKIT_NO_NOTIFY": "1"})]
        for x in self.p:
            x.start()
        pg.SESSIONS.mkdir(parents=True)
        self.sf = pg.SESSIONS / "claude-s1.json"
        self.now = time.time()
        self.sf.write_text(json.dumps({"agent": "claude", "session": "s1", "started": self.now - 600,
                                       "project": "acme/api"}))

    def tearDown(self):
        for x in reversed(self.p):
            x.stop()
        self.tmp.cleanup()

    def record(self) -> dict:
        return json.loads(self.sf.read_text())

    def set_record(self, **kv):
        self.sf.write_text(json.dumps(dict(self.record(), **kv)))

    def stop_hook(self, meanwhile):
        # Stop for claude:s1, with `meanwhile` run right after Stop has read the record (a turn with no
        # development signal: no gates, so the hook only does its own bookkeeping)
        def decide(data, sess):
            meanwhile()
            return False
        with mock.patch.object(scope, "decide", decide), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"}))), \
                contextlib.redirect_stdout(io.StringIO()):
            hooks.stop("claude")

    def audited(self, action) -> list:
        return [r for r in audit.records(1, action)]

    def run_together(self, script, args, payloads=None) -> None:
        # one real process per argument, all started before any of them goes on (they wait for the "go" file)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DEVKIT_HOOK_CAPTURE")}
        env.update(DEVKIT_SESSION="claude:s1", DEVKIT_NO_NOTIFY="1")
        procs = []
        for i, arg in enumerate(args):
            p = subprocess.Popen([sys.executable, "-c", script, str(LIB), str(self.root), arg], env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            p.stdin.write(json.dumps((payloads or [{}] * len(args))[i]).encode())
            p.stdin.close()
            procs.append(p)
        deadline = time.time() + 30
        while not all((self.root / f"ready-{a}").exists() for a in args) and time.time() < deadline:
            time.sleep(0.01)
        (self.root / "go").touch()
        for p in procs:
            p.wait(timeout=30)
            p.stdout.close()
            p.stderr.close()
        (self.root / "go").unlink()


class ConcurrentWriterTests(Records):
    def test_eight_hook_processes_each_add_their_key_and_all_eight_stay(self):
        aids = [f"a{i}" for i in range(8)]
        for aid in aids:  # each one a subagent that the Agent tool launched (its meta file names the launch)
            (self.root / f"agent-{aid}.jsonl").write_text("")
            (self.root / f"agent-{aid}.meta.json").write_text(json.dumps({"toolUseId": f"toolu_{aid}",
                                                                          "description": f"Neo: part {aid}"}))
        self.run_together(CHILD, aids, [{"session_id": "s1", "agent_id": aid, "agent_type": "general-purpose",
                                         "agent_transcript_path": str(self.root / f"agent-{aid}.jsonl"),
                                         "last_assistant_message": "Done, no status block."} for aid in aids])
        self.assertEqual(sorted(self.record().get("crew_pending") or {}), aids)  # today some are lost

    def test_eight_commands_that_add_a_message_to_the_run_keep_all_eight(self):
        pg.start("Release", plan=["build=10m"], now=self.now)
        self.run_together(CHILD_ASK, [str(i) for i in range(8)])
        msgs = pg.load_run("claude", "s1")["messages"]
        self.assertEqual(sorted(m["text"] for m in msgs), [f"question {i}" for i in range(8)])
        self.assertEqual(len({m["id"] for m in msgs}), 8)  # and each has its own id

    def test_a_subagent_start_between_stops_read_and_write_survives(self):
        self.set_record(turn_started=self.now - 60, launches={"toolu_a7": {
            "tool_use_id": "toolu_a7", "label": "Tank: state helper", "type": "general-purpose", "parent": "main",
            "ts": round(self.now, 1), "state": "pending", "agent_id": None}})
        main = self.root / "s1.jsonl"  # the agent's meta file names its launch: the start claims it
        main.write_text("")
        (self.root / "s1" / "subagents").mkdir(parents=True)
        (self.root / "s1" / "subagents" / "agent-a7.meta.json").write_text(json.dumps(
            {"toolUseId": "toolu_a7", "description": "Tank: state helper"}))
        self.stop_hook(lambda: pg.on_subagent("claude", {"session_id": "s1", "agent_id": "a7",
                                                         "agent_type": "general-purpose",
                                                         "transcript_path": str(main)}, True))
        rec = self.record()
        self.assertEqual((rec.get("subagents") or {}).get("a7", {}).get("label"), "Tank: state helper")
        self.assertEqual(rec["launches"]["toolu_a7"]["agent_id"], "a7")  # the claimed launch does not come back
        self.assertIsNone(rec["turn_started"])  # and Stop's own change is there too

    def test_a_session_that_ended_while_stop_ran_is_not_created_again(self):
        self.stop_hook(self.sf.unlink)  # SessionEnd removed the record meanwhile
        self.assertFalse(self.sf.exists())

    def test_a_half_written_record_is_never_written_over(self):
        half = '{"agent": "claude", "session": "s1", "subagents": {"a1": {"type": "Expl'
        self.sf.write_text(half)  # a writer that is not atomic was cut off here
        pg.on_prompt("claude", {"session_id": "s1", "prompt": "next"})
        self.stop_hook(lambda: None)
        self.assertEqual(self.sf.read_text(), half)
        self.assertTrue(self.audited("state.unreadable"))

    def test_a_progress_step_on_a_half_written_run_leaves_it_as_it_is(self):
        # Seraph M69 (2): `dev progress step` checked for a run with an unlocked read, which reads a half-written run
        # as missing, and then started a new run over it
        run = pg.run_path("claude", "s1")
        run.parent.mkdir(parents=True)
        half = '{"v": 2, "agent": "claude", "session": "s1", "goal": "Work", "plan": [{"name": "par'
        run.write_text(half)
        with self.assertRaises(ValueError):
            pg.step("parser done", check="12 tests pass")
        self.assertEqual(run.read_text(), half)
        self.assertTrue(self.audited("state.unreadable"))

    def test_a_temporary_file_left_where_a_writer_would_put_its_own_is_never_written_through(self):
        # Seraph M78 (2): each write uses a temporary file of its own, created new; a name planted in the record's
        # folder (here a link to another file) is neither followed nor reused
        from devkit import state
        victim = self.root / "victim.txt"
        victim.write_text("keep me")
        for name in (f".{self.sf.name}.tmp", f"{self.sf.name}.tmp", f".{self.sf.name}.{os.getpid()}.tmp",
                     f"{self.sf.stem}.tmp"):
            (self.sf.parent / name).symlink_to(victim)
        state.update(self.sf, lambda d: d.update(x=1))
        self.assertEqual(victim.read_text(), "keep me")
        self.assertEqual(self.record()["x"], 1)

    def test_a_hook_that_cannot_get_the_lock_drops_its_change_and_says_so(self):
        from devkit import state
        lock = self.root / "locks" / "rec-claude-s1.lock"  # one lock per session, for both of its records
        lock.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", "import fcntl, os, sys\n"
                                   "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
                                   "fcntl.flock(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\nsys.stdin.read()",
                                   str(lock)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertEqual(holder.stdout.readline().strip(), b"held")
            before = self.sf.read_text()
            with mock.patch.object(state, "WAIT", 0.1):
                pg.on_prompt("claude", {"session_id": "s1", "prompt": "next"})  # fails open: no exception
            self.assertEqual(self.sf.read_text(), before)
            self.assertTrue(self.audited("state.busy"))
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)
            holder.stdout.close()


class LockTests(Records):
    def test_a_writer_that_holds_the_lock_writes_again_without_waiting_and_other_threads_wait(self):
        from devkit import state
        caught = []

        def other_thread():
            try:
                state.update(self.sf, lambda d: d.update(other=1), wait=0.05)
            except state.StateBusy:
                caught.append("busy")
        t0 = time.monotonic()
        with state.hold(pg.run_path("claude", "s1")):  # the run's lock is the session's lock
            state.update(self.sf, lambda d: d.update(inner=1))
            state.update(self.sf, lambda d: d.update(inner=2))
            self.assertLess(time.monotonic() - t0, 0.1)
            t = threading.Thread(target=other_thread)
            t.start()
            t.join(5)
        self.assertEqual(caught, ["busy"])  # the dashboard server's threads are kept out too
        self.assertEqual(self.record()["inner"], 2)
        self.assertNotIn("other", self.record())

    def test_the_continuity_record_takes_the_sessions_lock(self):
        # STATE/continuity/<agent>-<sid>.json outlives the session record (spec "Continuity module contract",
        # Records): one lock still covers all three records of a session, so a hold on the run is a hold on it
        from devkit import state
        cont = self.root / "continuity" / "claude-s1.json"
        self.assertEqual(state.lock_file(cont), state.lock_file(self.sf))
        caught = []

        def other_thread():
            try:
                state.update(cont, lambda d: d.update(other=1), wait=0.05)
            except state.StateBusy:
                caught.append("busy")
        with state.hold(pg.run_path("claude", "s1")):
            t = threading.Thread(target=other_thread)
            t.start()
            t.join(5)
        self.assertEqual(caught, ["busy"])

    def test_a_write_leaves_only_the_record_private_to_its_owner(self):
        from devkit import state
        # the test sets the file mode it checks, on its own temporary files
        os.chmod(pg.SESSIONS, 0o755)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        state.update(self.sf, lambda d: d.update(x=1))
        self.assertEqual(stat.S_IMODE(self.sf.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(pg.SESSIONS.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in pg.SESSIONS.iterdir()), ["claude-s1.json"])  # no temporary file


def _value(rng, depth):
    # a random JSON value: null, a number, a string, a list, or (above depth 3, never) a dict of such values
    pick = rng.randrange(5 if depth < 3 else 4)
    if pick == 0:
        return None
    if pick == 1:
        return rng.randrange(5)
    if pick == 2:
        return f"s{rng.randrange(4)}"
    if pick == 3:
        return [rng.randrange(3) for _ in range(rng.randrange(3))]
    return {f"k{i}": _value(rng, depth + 1) for i in range(rng.randrange(4))}


def _mutate(rng, doc, keys):
    # a copy of doc with some of `keys` (top-level) set to new values or deleted, and a nested change or two
    out = json.loads(json.dumps(doc))
    for k in keys:
        r = rng.random()
        if r < 0.3:
            out.pop(k, None)
        elif r < 0.6 and isinstance(out.get(k), dict) and out[k]:
            sub = rng.choice(sorted(out[k]))
            out[k][sub] = _value(rng, 2)
        else:
            out[k] = _value(rng, 1)
    return out


class PatchTests(unittest.TestCase):
    # patch's merge, on random records with a fixed seed (deterministic): the properties hold for every case
    def test_with_no_other_writer_the_result_is_what_the_writer_made(self):
        from devkit import state
        rng = random.Random(7)
        for _ in range(300):
            before = {f"k{i}": _value(rng, 0) for i in range(rng.randrange(6))}
            after = _mutate(rng, before, [f"k{i}" for i in range(7) if rng.random() < 0.5])
            fresh = json.loads(json.dumps(before))
            self.assertEqual(state.apply(fresh, state.diff(before, after)), [])
            self.assertEqual(fresh, after, (before, after))

    def test_changes_to_different_keys_by_two_writers_both_stay(self):
        from devkit import state
        rng = random.Random(11)
        for _ in range(300):
            names = [f"k{i}" for i in range(8)]
            before = {k: _value(rng, 0) for k in names if rng.random() < 0.7}
            mine = [k for k in names if rng.random() < 0.5]
            theirs = [k for k in names if k not in mine]
            after, fresh = _mutate(rng, before, mine), _mutate(rng, before, theirs)
            want = {**{k: v for k, v in fresh.items() if k not in mine},
                    **{k: v for k, v in after.items() if k in mine}}
            self.assertEqual(state.apply(fresh, state.diff(before, after)), [])
            self.assertEqual(fresh, want, (before, after))

    def test_a_list_changed_on_both_sides_keeps_the_fresh_one_and_a_deleted_dict_stays_deleted(self):
        from devkit import state
        before = {"ci_events": [1], "subagents": {"a1": {"label": "Neo"}}}
        after = {"ci_events": [1, 2], "subagents": {"a1": {"label": "Neo", "seen": True}}}
        fresh = {"ci_events": [1, 3], "subagents": {}}  # another writer appended, and a SubagentStop removed a1
        conflicts = state.apply(fresh, state.diff(before, after))
        self.assertEqual(fresh, {"ci_events": [1, 3], "subagents": {}})
        self.assertEqual(sorted(conflicts), [("ci_events",), ("subagents", "a1", "seen")])


# --------------------------------------------------------------------------- nothing writes the records directly

# Expressions that name a session or run record (STATE/sessions, STATE/runs). Bare SESSIONS and RUNS are those
# folders only inside progress.py (work.SESSIONS is the handoff repository's session log).
RECORD = re.compile(r"""STATE\s*/\s*["'](sessions|runs)["']|\b(progress|pg)\.(SESSIONS|RUNS)\b|"""
                    r"""\b(run_path|_session_file|_sess|_register_session)\(""")
BARE = re.compile(r"(?<![\w.])(SESSIONS|RUNS)\b")
WRITE_MODE = re.compile(r"[wax+]")


def direct_writes(path: Path) -> list:
    """[(file, line, call)] for each write of a record path that does not go through state.py: write_text,
    write_bytes, open(…, "w"), json.dump, os.replace or os.rename, write_if_changed. Paths are followed through
    the names they are assigned to and the paths built from them (path / "x", path.with_suffix(…))."""
    text = path.read_text()
    bare = path.name == "progress.py"
    out = []
    lines = text.splitlines(keepends=True)  # split once: ast.get_source_segment re-splits the file per call

    def segment(node) -> str:
        # the node's source text from the lines split once (columns are UTF-8 byte offsets, as in the ast)
        end = getattr(node, "end_lineno", None)
        if end is None:
            return ""
        first, last = node.lineno - 1, end - 1
        if first == last:
            return lines[first].encode()[node.col_offset:node.end_col_offset].decode(errors="replace")
        part = [lines[first].encode()[node.col_offset:].decode(errors="replace")]
        part += lines[first + 1:last]
        part.append(lines[last].encode()[:node.end_col_offset].decode(errors="replace"))
        return "".join(part)

    def record_path(node, names) -> bool:
        src = segment(node)
        if RECORD.search(src) or (bare and BARE.search(src)):
            return True
        if isinstance(node, ast.Name):
            return node.id in names
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return record_path(node.left, names)
        if isinstance(node, ast.Attribute):
            return record_path(node.value, names)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr in ("with_suffix", "with_name", "with_stem"):
            return record_path(node.func.value, names)
        return False

    for fn in (n for n in ast.walk(ast.parse(text)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        names = set()
        for n in sorted((n for n in ast.walk(fn) if hasattr(n, "lineno")), key=lambda n: (n.lineno, n.col_offset)):
            pairs = [(t, n.value) for t in n.targets] if isinstance(n, ast.Assign) else \
                [(i.optional_vars, i.context_expr) for i in n.items if i.optional_vars is not None] \
                if isinstance(n, ast.With) else []
            for target, value in pairs:
                bound = {x.id for x in ast.walk(target) if isinstance(x, ast.Name)}
                names = (names | bound) if record_path(value, names) else (names - bound)
            if not isinstance(n, ast.Call):
                continue
            f, a, hit = n.func, n.args, None
            mod = f.value.id if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) else ""
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name in ("write_text", "write_bytes") and isinstance(f, ast.Attribute) and record_path(f.value, names):
                hit = name
            elif mod == "os" and name in ("replace", "rename") and len(a) > 1 and record_path(a[1], names):
                hit = "os." + name
            elif mod == "json" and name == "dump" and len(a) > 1 and record_path(a[1], names):
                hit = "json.dump"
            elif name == "write_if_changed" and a and record_path(a[0], names):
                hit = name
            elif name == "open" and isinstance(f, ast.Name) and len(a) > 1 and record_path(a[0], names) and \
                    isinstance(a[1], ast.Constant) and WRITE_MODE.search(str(a[1].value)):
                hit = "open"
            elif name == "open" and isinstance(f, ast.Attribute) and record_path(f.value, names) and a and \
                    isinstance(a[0], ast.Constant) and WRITE_MODE.search(str(a[0].value)):
                hit = ".open"
            if hit:
                out.append(f"{path.name}:{n.lineno} {hit}")
    return out


_WATCH = {"on": False, "hits": []}


def _audit_hook(event, args):
    # While the check runs: a write-mode open or a rename of a record (sessions/*.json, runs/*.json, or a temporary
    # file next to one) whose nearest devkit frame is not state.py. Writes made by test code itself are fixtures.
    if not _WATCH["on"] or event not in ("open", "os.rename"):
        return
    try:
        if event == "open":
            target, mode, flags = args
            if isinstance(target, int) or not (
                    (isinstance(mode, str) and WRITE_MODE.search(mode)) or
                    (flags or 0) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)):
                return
        else:
            target = args[1]
        p = Path(os.fsdecode(os.fspath(target)))
        if p.parent.name not in ("sessions", "runs") or not p.name.endswith((".json", ".tmp")):
            return
        f = sys._getframe(1)
        while f and not f.f_code.co_filename.startswith(str(LIB / "devkit")):
            f = f.f_back
        if f and not f.f_code.co_filename.endswith("state.py"):
            _WATCH["hits"].append(f"{Path(f.f_code.co_filename).name}:{f.f_lineno} {f.f_code.co_name} → {p.name}")
    except Exception:
        pass


sys.addaudithook(_audit_hook)


class OnlyStateWritesTests(unittest.TestCase):
    def test_no_module_writes_a_session_or_run_record_directly(self):
        hits = [h for f in sorted((LIB / "devkit").glob("*.py")) if f.name != "state.py" for h in direct_writes(f)]
        self.assertEqual(hits, [], "write the session and run records through lib/devkit/state.py "
                                   "(update, patch or replace), never directly")

    def test_the_hooks_and_commands_write_records_only_through_state(self):
        # the suites that drive the hooks, dev progress, dev crew, dispatch and the CI watch, run under an audit
        # hook that sees every file write
        pkg = (__package__ + ".") if __package__ else ""
        suite = unittest.defaultTestLoader.loadTestsFromNames(
            [pkg + m for m in ("test_progress", "test_raci", "test_dispatch", "test_ciwatch", "test_closing_reminder",
                               "test_crew", "test_plan_dag", "test_session_graph")])
        result = unittest.TestResult()
        _WATCH.update(on=True, hits=[])
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                suite.run(result)
        finally:
            _WATCH["on"] = False
        self.assertGreater(result.testsRun, 100)
        self.assertEqual(sorted(set(_WATCH["hits"])), [],
                         f"{len(result.failures) + len(result.errors)} of those tests failed")


if __name__ == "__main__":
    unittest.main()
