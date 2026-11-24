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
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import (audit, bugs, cli, configschema, core, ext, impeccable, lessons, peers,  # noqa: E402
                    progress, registry, state, work)


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


class DomainErrorsAreRefusals(SeamCase):
    def test_config_error_exits_2(self):
        self.assertRefused(self.run_raising(core.ConfigError("unknown key foo.bar: dev config show lists them")),
                           2, "unknown key foo.bar: dev config show lists them")

    def test_schema_error_exits_2(self):
        self.assertRefused(self.run_raising(configschema.SchemaError("ext.mcp.x: needs an exact version")),
                           2, "ext.mcp.x: needs an exact version")

    def test_ext_error_exits_2(self):
        self.assertRefused(self.run_raising(ext.ExtError("no extension named zed: dev ext ls lists them")),
                           2, "no extension named zed: dev ext ls lists them")

    def test_peer_error_keeps_its_own_exit(self):
        # 4: no live session matches the target, the exit `dev session` documents
        self.assertRefused(self.run_raising(peers.PeerError("no live session matches S-abc", 4)),
                           4, "no live session matches S-abc")

    def test_unreadable_lesson_exits_2(self):
        exc = lessons.Unreadable(self.root / "L-1.md", "no front matter")
        self.assertRefused(self.run_raising(exc), 2, str(exc))

    def test_refused_lesson_exits_2_with_every_problem(self):
        exc = lessons.Refused(["the title is empty", "--prevent is missing"])
        rc, out, err = self.run_raising(exc)
        self.assertRefused((rc, out, err), 2, "2 problems: correct them all")
        self.assertIn("  - --prevent is missing", err)

    def test_alias_error_exits_2(self):
        self.assertRefused(self.run_raising(registry.AliasError("“Api” can't be an alias: use lowercase letters")),
                           2, "“Api” can't be an alias: use lowercase letters")

    def test_record_loss_exits_2(self):
        exc = work.RecordLossError(self.root / "T-1.md", ["## Notes kept by hand"])
        self.assertRefused(self.run_raising(exc), 2, str(exc))

    def test_a_write_that_would_drop_lines_says_how_to_report_a_misread_record(self):
        # devkit may have misread the record, a defect the crash path no longer reports: the fix names the bug report
        path = self.root / "T-1.md"
        before = "---\nid: T-1\nversion: 1\n---\n# Task\n\nNotes kept by hand.\n"
        path.write_text(before)
        rc, out, err = self.run_calling(lambda: work.write_record(path, "---\nid: T-1\nversion: 2\n---\n# Task\n"))
        self.assertRefused((rc, out, err), 2, f"refused to write {path}")
        self.assertIn("\n  fix: run the command again; if it refuses the same way, leave the file as it is and run "
                      "dev bug report --title ", err)
        self.assertEqual(path.read_text(), before)

    def test_a_refused_record_write_never_asks_to_send_the_record_along(self):
        # the record can hold private notes: the message stops at leaving the file, and the fix names a report whose
        # title holds no file
        rc, out, err = self.run_raising(work.RecordLossError(self.root / "T-1.md", ["## Notes kept by hand"]))
        self.assertIn("otherwise devkit misread the record: leave the file as it is.\n  fix: ", err)

    def test_impeccable_refusal_exits_2(self):
        self.assertRefused(self.run_raising(impeccable.Refused("the engine is not installed: dev install --only "
                                                               "skills")),
                           2, "the engine is not installed: dev install --only skills")

    def test_parser_refusal_is_a_refusal(self):
        # the one-command parser's signal to the full parser: an exit 2, as argparse gives a bad command line
        self.assertRefused(self.run_raising(cli._Refused("unrecognized arguments: --nope")),
                           2, "unrecognized arguments: --nope")

    def test_existing_value_error_handlers_still_catch_them(self):
        # the base sits next to ValueError, so every `except ValueError` written before the seam still applies
        for exc in (core.ConfigError("c"), configschema.SchemaError("s"), ext.ExtError("e"), peers.PeerError("p"),
                    lessons.Unreadable(self.root / "L-1.md", "u"), lessons.Refused(["r"]), registry.AliasError("a"),
                    work.RecordLossError(self.root / "T-1.md", ["l"])):
            with self.subTest(type(exc).__name__):
                self.assertIsInstance(exc, ValueError)
                self.assertIsInstance(exc, core.Refusal)


class BusyExits75(SeamCase):
    def test_state_lock_timeout_exits_75(self):
        exc = state.StateBusy("run record is busy: another devkit process held it for 5 s; try again")
        self.assertRefused(self.run_raising(exc), 75, "run record is busy")

    def test_record_lock_timeout_exits_75(self):
        exc = work.RecordBusyError(self.root / "T-1.md", 30)
        result = self.run_raising(exc)
        self.assertRefused(result, 75, str(exc))
        self.assertNotIn("dev bug report", result[2])  # a busy record is no misread: running again is the fix

    def test_alias_lock_timeout_exits_75(self):
        # another process holds the alias file's lock: the change waits, then refuses with the retry exit
        held, release = threading.Event(), threading.Event()

        def holder():
            with core.file_lock(registry.aliases_file(), 5):
                held.set()
                release.wait(10)
        t = threading.Thread(target=holder)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))

        def change():
            with registry._aliases_lock():
                raise AssertionError("the lock was free")
        with mock.patch.object(registry, "ALIASES_WAIT", 0.1):
            self.assertRefused(self.run_calling(change), 75, "another devkit process kept")


class StateRefusalsThatARetryCannotFix(SeamCase):
    def test_a_record_that_does_not_parse_exits_2(self):
        f = self.root / "run.json"
        f.write_text("{cut off")
        with mock.patch.object(state.time, "sleep", lambda s: None):
            self.assertRefused(self.run_calling(lambda: state.load(f)), 2, "run.json does not parse")

    def test_a_corrupt_bounded_record_exits_2(self):
        exc = state.StateCorrupt("corrupt_state: record does not parse; retained unchanged")
        self.assertRefused(self.run_raising(exc), 2, "corrupt_state: record does not parse")

    def test_an_oversized_record_exits_2(self):
        # a record past the storage limit: running the command again writes the same bytes
        self.assertRefused(self.run_calling(lambda: state.replace(self.root / "big.json", {"x": "y" * 100},
                                                                  max_bytes=10)),
                           2, "capacity: record exceeds the storage limit")


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

    def test_a_reason_without_a_secret_shows_when_redaction_fails(self):
        # the redaction itself raises (both with and without the configured patterns): the refusal still ends as a
        # refusal, and a reason no built-in secret rule matches is shown as it is
        def broken(text, extra=None):
            raise RuntimeError("redaction failed")
        with mock.patch.object(audit, "redact", broken):
            result = self.run_raising(core.Refusal("the branch is behind origin", fix="dev rebase"))
        self.assertRefused(result, 2, "the branch is behind origin")
        self.assertIn("  fix: dev rebase", result[2])

    def test_a_reason_with_a_secret_is_withheld_when_redaction_fails(self):
        def broken(text, extra=None):
            raise RuntimeError("redaction failed")
        with mock.patch.object(audit, "redact", broken):
            result = self.run_raising(core.Busy("cannot reach https://ana:hunter2secret@git.example.com/x",
                                                fix="git remote set-url origin https://ana:hunter2secret@h/x"))
        self.assertRefused(result, 75, "refused; the reason is not shown because devkit could not redact it")
        self.assertNotIn("hunter2secret", result[2])

    def test_a_reason_is_withheld_whenever_the_teams_extra_patterns_could_not_be_applied(self):
        # a token shape only the team's audit.redact.extra knows: the built-in rules alone cannot tell it is a secret
        def unreadable():
            raise OSError("the team policy cannot be read")

        def broken(text, extra=None):
            raise RuntimeError("redaction failed")
        team = {"redact": {"extra": [r"acmetok-[a-z0-9]{14}"]}}
        causes = {"the settings cannot be read": [mock.patch.object(audit, "settings", unreadable)],
                  "the redaction fails": [mock.patch.object(audit, "settings", lambda: team),
                                          mock.patch.object(audit, "redact", broken)]}
        for cause, patches in causes.items():
            with self.subTest(cause=cause), contextlib.ExitStack() as stack:
                for p in [mock.patch.object(audit, "audited", lambda argv: False)] + patches:  # no command record
                    stack.enter_context(p)
                result = self.run_raising(core.Refusal("the deploy key acmetok-7f3k9x2m4q8zr1 was refused",
                                                       fix="dev config set deploy.key acmetok-7f3k9x2m4q8zr1"))
                self.assertRefused(result, 2, "refused; the reason is not shown because devkit could not redact it")
                self.assertNotIn("acmetok-7f3k9x2m4q8zr1", result[2])
                self.assertIn("\n  fix: run dev audit config and correct the errors it lists; if it lists none, run "
                              "dev bug report", result[2])

    def test_a_reason_is_withheld_while_the_team_policy_on_disk_is_broken(self):
        # the real settings, reading a team policy a teammate's commit cut off: its patterns never load
        policy = self.root / "handoff" / "policy" / "audit.json"
        policy.parent.mkdir(parents=True)
        policy.write_text('{"redact": {"extra": ["acmetok-[a-z0-9]{14}"]')
        with mock.patch.object(core, "HANDOFF", self.root / "handoff"), mock.patch.dict(core.CFG, {"audit": {}}), \
                mock.patch.object(audit, "audited", lambda argv: False):  # no command record
            rc, out, err = self.run_raising(core.Refusal("the deploy key acmetok-7f3k9x2m4q8zr1 was refused",
                                                         fix="dev config set deploy.key acmetok-7f3k9x2m4q8zr1"))
        self.assertRefused((rc, out, err), 2, "refused; the reason is not shown because devkit could not redact it")
        self.assertNotIn("acmetok-7f3k9x2m4q8zr1", err)
        self.assertIn("\n  fix: repair the file it names (the team's redaction patterns did not load: ", err)
        self.assertIn(f"{core.tilde(policy)} is not valid JSON); dev audit config lists its errors", err)

    def test_a_reason_is_withheld_while_this_macs_patterns_are_rejected(self):
        # this Mac's own audit.redact.extra written as one string: rejected, so it fails closed like the team policy
        with mock.patch.object(core, "HANDOFF", self.root / "handoff"), \
                mock.patch.dict(core.CFG, {"audit": {"redact": {"extra": "acmetok-[a-z0-9]{14}"}}}), \
                mock.patch.object(audit, "audited", lambda argv: False):
            rc, out, err = self.run_raising(core.Refusal("the deploy key acmetok-7f3k9x2m4q8zr1 was refused"))
        self.assertRefused((rc, out, err), 2, "refused; the reason is not shown because devkit could not redact it")
        self.assertNotIn("acmetok-7f3k9x2m4q8zr1", err)
        self.assertIn(f"fix: repair the file it names (this Mac's redaction patterns did not load: "
                      f"{core.tilde(core.CONFIG_FILE)} has an audit.redact section that was rejected)", err)

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
