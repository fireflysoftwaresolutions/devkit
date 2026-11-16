# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Extension changes and the impeccable engine run for an agent in either mode, and keep host failures and secrets
out of the records."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from tests.test_progress import TempState
from tests.test_ext import (FakeRun, SHA_A, SHA_B, fixture_team, install_claude_plugin,
                            make_homes, osascript_calls)
from tests.test_impeccable_wrapper import WrapperCase
from devkit import audit, cli, core, ext, impeccable, progress, setup


def agent(test):
    snapshot = core.config_snapshot
    state = TempState().__enter__()
    test.addCleanup(state.__exit__, None, None, None)
    root = Path(state.tmp.name)
    config = root / "config.json"
    config.write_text('{"work":{"mode":"auto"}}')
    for p in (mock.patch.object(core, "CONFIG_FILE", config),
              mock.patch.object(core, "config_snapshot", snapshot),
              mock.patch.object(core, "set_by_developer", return_value=False)):
        p.start()
        test.addCleanup(p.stop)
    return root, config


class ExtensionAutoTests(unittest.TestCase):
    def setUp(self):
        self.root, self.config = agent(self)
        self.homes = make_homes(self.root)
        self.lock = self.root / "ext-lock.json"
        for p in (mock.patch.object(ext, "homes", return_value=self.homes),
                  mock.patch.object(ext, "team", side_effect=fixture_team),
                  mock.patch.object(ext, "lock_file", return_value=self.lock),
                  mock.patch.object(ext, "skill_groups", return_value={"all"})):
            p.start()
            self.addCleanup(p.stop)
        notices = osascript_calls()
        notices.__enter__()
        self.addCleanup(notices.__exit__, None, None, None)
        install_claude_plugin(self.homes, "superpowers@claude-plugins-official", "7.0.0", SHA_B)
        ext.record_lock("claude")
        install_claude_plugin(self.homes, "superpowers@claude-plugins-official", "7.0.1", SHA_A)

    def test_an_agent_accepts_observed_drift_and_acknowledges_it(self):
        self.assertGreater(ext.record_lock("claude"), 0)
        self.assertEqual(len(ext.pending_acceptances()), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["ext", "ack"]), 0)
        self.assertEqual(ext.pending_acceptances(), [])

    def test_setup_accepts_drift_in_guided_mode_too(self):
        self.config.write_text('{"work":{"mode":"guided"}}')
        with mock.patch.object(ext, "retire", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            setup._extensions("claude")
        self.assertEqual(len(ext.pending_acceptances()), 1)

    def test_a_crew_member_accepts_drift_too(self):
        with mock.patch.object(progress, "caller", return_value={"caller": "member"}):
            self.assertGreater(ext.record_lock("claude"), 0)
        lk = ext.read_lock()["plugin:claude:superpowers@claude-plugins-official"]
        self.assertEqual((lk["version"], lk["sha"]), ("7.0.1", SHA_A))

    def test_project_change_reaches_vendor_and_preserves_host_failure(self):
        core.git(self.root, "init", "-q")
        core.git(self.root, "remote", "add", "origin", "https://github.com/acme/api.git")
        runner = FakeRun({("claude", "plugin", "disable", "superpowers@claude-plugins-official", "-s", "project"):
                          (7, "", "host permission denied")})
        out = io.StringIO()
        with mock.patch.object(ext, "runner", return_value=runner), \
                mock.patch("os.getcwd", return_value=str(self.root)), contextlib.redirect_stderr(out):
            rc = cli.main(["ext", "disable", "superpowers", "--agent", "claude", "--project", "--why", "test"])
        self.assertEqual(rc, 1)
        self.assertIn("failed (exit 7)", out.getvalue())
        self.assertEqual(len(runner.calls), 1)

    def test_a_project_change_in_another_repository_reaches_the_vendor_too(self):
        core.git(self.root, "init", "-q")
        core.git(self.root, "remote", "add", "origin", "https://github.com/acme/other.git")
        runner = FakeRun()
        with mock.patch.object(ext, "runner", return_value=runner), \
                mock.patch("os.getcwd", return_value=str(self.root)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["ext", "disable", "superpowers", "--agent", "claude", "--project", "--why", "test"])
        self.assertEqual(rc, 0)
        self.assertEqual(runner.calls, [["claude", "plugin", "disable", "superpowers@claude-plugins-official", "-s",
                                         "project"]])

    def test_setup_propagates_extension_host_failure(self):
        with mock.patch.object(ext, "retire", return_value=[(False, "host permission denied")]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "host permission denied"):
                setup._extensions("claude")

    def test_vendor_arguments_and_output_stay_out_of_audit_and_display(self):
        secret = "SYNTHETIC_PRIVATE_VALUE_123456"
        args = ["codex", "mcp", "add", "demo", "--url", "https://example.invalid/mcp/" + secret]
        for rc in (0, 7):
            with self.subTest(exit=rc):
                runner = FakeRun({tuple(args): (rc, "stdout " + secret, "stderr " + secret)})
                messages = ext.apply([("add", "mcp:codex:demo", args)], runner)
                record = audit.records(days=1, action="ext.add")[-1]
                self.assertEqual(messages[0][0], rc == 0)
                self.assertNotIn(secret, json.dumps(record))
                self.assertNotIn(secret, messages[0][1])
                self.assertIn("sha256:", record["detail"])
                if rc:
                    self.assertIn("exit 7", messages[0][1])

    def test_app_configuration_secrets_stay_out_of_acceptance_and_notifications(self):
        secret = "SYNTHETIC_PRIVATE_VALUE_123456"
        notices = []
        items = []
        for i, spec in enumerate(({"url": "https://user:" + secret + "@example.invalid/mcp"},
                                  {"command": "server", "args": ["--api-key", secret]},
                                  {"command": "/private/" + secret + "/server", "args": [secret]})):
            entry = dict(spec, spec="observed-config", app="record")
            rid = "mcp:codex:demo" + str(i)
            line = ext._app_line("codex", rid, entry)
            self.assertNotIn(secret, line)
            self.assertIn("sha256:", line)
            items.append((rid, line))
        with mock.patch.object(progress, "notify", side_effect=lambda *a, **k: notices.append((a, k))):
            self.assertTrue(ext._note_accepted(items, 1, "dev install", self.lock))
        self.assertNotIn(secret, ext.accepted_file(self.lock).read_text())
        self.assertNotIn(secret, json.dumps(audit.records(days=1, action="ext.accept")))
        self.assertNotIn(secret, json.dumps(notices))
        self.assertEqual(len(ext.pending_acceptances(self.lock)), 3)
        self.assertEqual(len(notices), 1)


class ImpeccableAutoTests(WrapperCase):
    def setUp(self):
        super().setUp()
        self.root, self.config = agent(self)

    def test_auto_runs_verified_engine_off_allowlist(self):
        with mock.patch.dict("os.environ", {"FAKE_ENGINE_OUT": str(self.out)}):
            for args in (["hooks", "on"], ["hooks", "off"], ["hooks", "status"], ["install", "--user"],
                         ["update"], ["link"], ["doctor", "--fix"], ["skills", "--", "hooks", "on"]):
                self.assertEqual(impeccable.run(args, cwd=str(self.project)), 0)
                self.assertEqual(json.loads(self.out.read_text())["argv"], args)

    def test_auto_preserves_engine_exit_status(self):
        with mock.patch.dict("os.environ", {"FAKE_ENGINE_OUT": str(self.out), "FAKE_ENGINE_RC": "7"}):
            self.assertEqual(impeccable.run(["hooks", "on"], cwd=str(self.project)), 7)


class AdapterMutationDiagnostic(unittest.TestCase):
    def test_each_regression_detects_its_mutation(self):
        real_extensions = setup._extensions

        def raw_app_line(agent, rid, observed):
            command = " ".join([observed.get("command", ""), *observed.get("args", [])])
            return ext._what(agent, rid, observed) + ": " + (observed.get("url") or command)

        def raw_apply(actions, run, audit=None, lock_path=None):
            from devkit import audit as records
            record = audit or records.record
            results = []
            for verb, eid, argv in actions:
                rc, out, err = run(argv, timeout=300)
                record("ext." + verb, eid, " ".join(argv) + err, "ok" if rc == 0 else "exit " + str(rc))
                results.append((rc == 0, out + err))
            return results

        def suppressed_failure(agent):
            try:
                real_extensions(agent)
            except RuntimeError:
                return None

        checks = [
            (ExtensionAutoTests, "test_app_configuration_secrets_stay_out_of_acceptance_and_notifications", ext,
             "_app_line", raw_app_line),
            (ExtensionAutoTests, "test_vendor_arguments_and_output_stay_out_of_audit_and_display", ext,
             "apply", raw_apply),
            (ExtensionAutoTests, "test_setup_propagates_extension_host_failure", setup,
             "_extensions", suppressed_failure),
            (ExtensionAutoTests, "test_setup_accepts_drift_in_guided_mode_too", ext,
             "record_lock", lambda *a, **k: 0),
        ]
        for cls, name, module, attribute, mutation in checks:
            with self.subTest(regression=name):
                result = unittest.TestResult()
                with mock.patch.object(module, attribute, mutation), contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    cls(name).run(result)
                self.assertEqual(result.errors, [], "a fixture or import error is not causal proof")
                self.assertGreaterEqual(len(result.failures), 1, "the regression must catch the behavior mutation")
