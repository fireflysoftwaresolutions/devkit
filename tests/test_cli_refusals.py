# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A domain refusal ends a `dev` command as a refusal, never as devkit's crash: cli.main prints `dev <command>: <message>`
# and the fix, redacted, on stderr, exits 2 (75 when something is busy and running the command again can work),
# records that exit in the audit trail as the command's outcome, and files no crash report. Everything else a command
# raises stays a crash: exit 70, a crash record and a safe report, as before.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_cli_refusals
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import audit, bugs, cli, core, progress  # noqa: E402


def dev(*argv):
    # cli.main as an agent's shell runs it, in session s1, with its output captured
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(progress, "current_session", lambda *a, **k: ("claude", "s1")), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class SeamCase(unittest.TestCase):
    # the bug registry's local files in a temporary folder, so a crash's traces can be counted
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for p in (mock.patch.object(bugs, "BUGS", self.root / "bugs"),
                  mock.patch.object(bugs, "CRASHES", self.root / "crashes"),
                  mock.patch.object(bugs, "install_channel", lambda: "release"),
                  mock.patch.object(bugs, "deny_words", lambda: frozenset())):
            p.start()
            self.addCleanup(p.stop)

    def run_raising(self, exc):
        # `dev version` with a handler that raises `exc` -> (exit, stdout, stderr)
        def handler(a):
            raise exc
        with mock.patch.object(cli, "cmd_version", handler):
            return dev("version")

    def run_calling(self, fn):
        # `dev version` with a handler that runs `fn` -> (exit, stdout, stderr)
        with mock.patch.object(cli, "cmd_version", lambda a: fn()):
            return dev("version")

    def crash_traces(self):
        # what a crash leaves: the occurrence, the safe report queued for the registry, the full traceback
        occ = bugs.BUGS / "occurrences.jsonl"
        return {"occurrences": len(occ.read_text().splitlines()) if occ.exists() else 0,
                "reports": len(list((bugs.BUGS / "outbox-v2").glob("*.json"))),
                "tracebacks": len(list(bugs.CRASHES.glob("*.txt")))}

    def assertRefused(self, result, code, message):
        rc, out, err = result
        self.assertEqual(rc, code, err)
        self.assertIn(f"dev version: {message}", err)
        self.assertEqual(out, "")
        self.assertNotIn("Traceback", err)
        self.assertNotIn("devkit bug", err)
        self.assertEqual(self.crash_traces(), {"occurrences": 0, "reports": 0, "tracebacks": 0})


class TheSeam(SeamCase):
    def test_refusal_prints_its_fix(self):
        rc, out, err = self.run_raising(core.Refusal("the branch is behind origin", fix="dev rebase"))
        self.assertRefused((rc, out, err), 2, "the branch is behind origin")
        self.assertIn("  fix: dev rebase", err)

    def test_busy_exits_75(self):
        self.assertRefused(self.run_raising(core.Busy("the session record is busy")), 75,
                           "the session record is busy")

    def test_a_code_that_is_no_exit_status_falls_back_to_2(self):
        # a domain class may carry a `code` of its own meaning (messaging's "invalid_text")
        self.assertRefused(self.run_raising(core.Refusal("say something", code="invalid_text")), 2,
                           "say something")

    def test_the_message_and_fix_are_redacted(self):
        rc, out, err = self.run_raising(core.Refusal("cannot reach https://ana:hunter2secret@git.example.com/x",
                                                     fix="git remote set-url origin https://ana:hunter2secret@h/x"))
        self.assertEqual(rc, 2)
        self.assertNotIn("hunter2secret", err)
        self.assertIn("https://ana:" + audit.MARK + "@git.example.com/x", err)

    def test_the_audit_trail_records_the_refusal_exit(self):
        with mock.patch.object(audit, "audited", lambda argv: True):
            self.run_raising(core.Refusal("refused for the audit"))
            self.run_raising(core.Busy("busy for the audit"))
        exits = [(r.get("outcome"), (r.get("evidence") or {}).get("exit")) for r in audit.query(action="dev.version")]
        self.assertEqual(exits[-2:], [("failed", 2), ("failed", 75)])


class DefectsStayCrashes(SeamCase):
    def assertCrashed(self, result):
        rc, out, err = result
        self.assertEqual(rc, 70, err)
        self.assertIn("devkit bug", err)
        self.assertEqual(self.crash_traces(), {"occurrences": 1, "reports": 1, "tracebacks": 1})

    def test_a_plain_value_error_is_a_crash(self):
        self.assertCrashed(self.run_calling(lambda: int("x")))

    def test_a_type_error_is_a_crash(self):
        self.assertCrashed(self.run_raising(TypeError("unsupported operand type(s) for +: 'NoneType' and 'str'")))


if __name__ == "__main__":
    unittest.main()
