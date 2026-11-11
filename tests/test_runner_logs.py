# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Managed test logs stay readable while Codex runners overlap; Claude keeps its existing policy."""
import concurrent.futures
import fcntl
import socket
import threading
import unittest
from pathlib import Path
from unittest import mock

from tests.test_testx import Env, PY, write
from devkit import core, progress, testx


class RunnerLogTests(unittest.TestCase):
    def run_selection(self, repo, runner, name="test_quick.py"):
        return testx._run_step(str(repo), [(runner, {"files": [name]})], False, True,
                              "affected", "Tests")["runs"][0]

    def fixture(self, env):
        repo = env.repo({"test_quick.py": "import unittest\nclass Quick(unittest.TestCase):\n"
                        "    def test_ok(self): self.assertEqual(2 + 2, 4)\n"})
        return repo, {"name": "unittest", "root": ".", "python": PY, "exec": [PY, "-m", "unittest"]}

    def assert_unlocked(self, path):
        with path.open("r") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn("Ran 1 test", handle.read())

    def writer_is_locked(self):
        path = next((core.STATE / "tests" / "logs").rglob("*.log"))
        with path.open() as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
        return False

    def test_codex_active_writer_survives_peer_retention_and_claude_is_unchanged(self):
        with Env() as env, mock.patch.object(progress, "resolve_session", return_value=("codex", "thread", "env")):
            repo, runner = self.fixture(env)
            # A real test subprocess announces readiness and waits for an explicit release, without sleeps.
            with socket.socket() as server:
                server.bind(("127.0.0.1", 0))
                server.listen()
                server.settimeout(15)
                write(repo, {"test_wait.py": "import socket, unittest\nclass Wait(unittest.TestCase):\n"
                      "    def test_wait(self):\n"
                      f"        with socket.create_connection({server.getsockname()!r}, timeout=30) as peer:\n"
                      "            peer.sendall(b'ready')\n"
                      "            self.assertEqual(peer.recv(1), b'x')\n"})
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.run_selection, repo, runner, "test_wait.py")
                    peer, _ = server.accept()
                    with peer:
                        self.assertEqual(peer.recv(5), b"ready")
                        active = next((core.STATE / "tests" / "logs").rglob("*.log"))
                        try:
                            completed = [self.run_selection(repo, runner) for _ in range(11)]
                            with mock.patch.object(progress, "resolve_session",
                                                   return_value=("claude", "session", "env")):
                                legacy = [self.run_selection(repo, runner) for _ in range(11)]
                            self.assertTrue(active.exists(), "retention unlinked an active writer")
                            with active.open() as handle:
                                with self.assertRaises(BlockingIOError):
                                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            self.assertEqual(len(list(active.parent.glob("*.log"))), 11)
                            self.assertFalse(Path(completed[0]["log"]).exists())
                            old = Path(legacy[0]["log"])
                            self.assertFalse(old.exists())
                            self.assertEqual(len(list(old.parent.glob("*.log"))), 10)
                            self.assertNotEqual(old.parent, active.parent)
                        finally:
                            peer.sendall(b"x")
                    result = future.result(timeout=15)
                self.assertEqual(result["rc"], 0)
                self.assertEqual(Path(result["log"]), active)
                self.assert_unlocked(active)
                self.assertEqual(len(list(active.parent.glob("*.log"))), 10)
                for _ in range(10):
                    self.run_selection(repo, runner)
                self.assertFalse(active.exists(), "completed logs must eventually be pruned")

    def test_codex_allocations_are_unique_when_clock_and_process_match(self):
        with Env() as env, mock.patch.object(progress, "resolve_session", return_value=("codex", "thread", "env")):
            repo, runner = self.fixture(env)
            barrier = threading.Barrier(4)

            def run():
                barrier.wait(timeout=10)
                return self.run_selection(repo, runner)

            with mock.patch.object(testx.time, "strftime", return_value="20261110-175323"), \
                    mock.patch.object(testx.time, "time_ns", return_value=123), \
                    concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: run(), range(4)))
            logs = [Path(result["log"]) for result in results]
            self.assertEqual(len(set(logs)), 4, "concurrent runs overwrite one another at the same timestamp")
            for log in logs:
                self.assert_unlocked(log)

    def test_codex_failed_runs_release_ownership_and_keep_the_report_readable(self):
        with Env() as env, mock.patch.object(progress, "resolve_session", return_value=("codex", "thread", "env")):
            repo, runner = self.fixture(env)
            write(repo, {"test_fail.py": "import unittest\nclass Failure(unittest.TestCase):\n"
                         "    def test_failure(self): self.fail('runner evidence')\n"})
            popen, ownership = testx.subprocess.Popen, []

            def launch(argv, *args, **kwargs):
                if argv[0] == PY:
                    ownership.append(self.writer_is_locked())
                return popen(argv, *args, **kwargs)

            with mock.patch.object(testx.subprocess, "Popen", side_effect=launch):
                failure = self.run_selection(repo, runner, "test_fail.py")
            log = Path(failure["log"])
            self.assertEqual(ownership, [True], "the runner starts without ownership protection")
            self.assertEqual(failure["failed"], 1)
            self.assertIn("runner evidence", log.read_text())
            self.assert_unlocked(log)
            for _ in range(10):
                self.run_selection(repo, runner)
            self.assertFalse(log.exists())

    def test_codex_unexpected_runner_failure_releases_the_log_for_later_retention(self):
        with Env() as env, mock.patch.object(progress, "resolve_session", return_value=("codex", "thread", "env")):
            repo, runner = self.fixture(env)
            popen, ownership = testx.subprocess.Popen, []

            def interrupt(argv, *args, **kwargs):
                if argv[0] == PY:
                    ownership.append(self.writer_is_locked())
                    raise RuntimeError("runner interrupted")
                return popen(argv, *args, **kwargs)

            with mock.patch.object(testx.subprocess, "Popen", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "runner interrupted"):
                    self.run_selection(repo, runner)
            logs = list((core.STATE / "tests" / "logs").rglob("*.log"))
            self.assertEqual(ownership, [True], "the interrupted runner never owned its log")
            self.assertEqual(len(logs), 1)
            log = logs[0]
            with log.open() as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for _ in range(10):
                self.run_selection(repo, runner)
            self.assertFalse(log.exists())
