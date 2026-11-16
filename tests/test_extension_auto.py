# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Extension authorization uses current policy and preserves host failures."""
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


def automatic(test):
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
        self.root, self.config = automatic(self)
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

    def test_auto_accepts_observed_drift_and_can_acknowledge_it(self):
        self.assertGreater(ext.record_lock("claude"), 0)
        self.assertEqual(len(ext.pending_acceptances()), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["ext", "ack"]), 0)
        self.assertEqual(ext.pending_acceptances(), [])

    def test_setup_accepts_drift_in_auto(self):
        with mock.patch.object(ext, "retire", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            setup._extensions("claude")
        self.assertEqual(len(ext.pending_acceptances()), 1)

    def test_guided_member_and_invalid_configuration_preserve_drift(self):
        before = self.lock.read_bytes()
        for contents in ('{"work":{"mode":"guided"}}', '{"work":{"mode":"typo"}}'):
            self.config.write_text(contents)
            ext.record_lock("claude")
            self.assertEqual(self.lock.read_bytes(), before)
        self.config.write_text('{"work":{"mode":"auto"}}')
        with mock.patch.object(progress, "caller", return_value={"caller": "member"}):
            ext.record_lock("claude")
        self.assertEqual(self.lock.read_bytes(), before)

    def test_mode_revoked_while_observing_preserves_drift(self):
        before = self.lock.read_bytes()
        observe = ext.content_digest
        def revoke(path):
            self.config.write_text('{"work":{"mode":"guided"}}')
            return observe(path)
        with mock.patch.object(ext, "content_digest", side_effect=revoke):
            ext.record_lock("claude")
        self.assertEqual(self.lock.read_bytes(), before)

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

    def test_project_change_cannot_move_session_authority_to_another_repository(self):
        core.git(self.root, "init", "-q")
        core.git(self.root, "remote", "add", "origin", "https://github.com/acme/other.git")
        runner = FakeRun()
        with mock.patch.object(ext, "runner", return_value=runner), \
                mock.patch("os.getcwd", return_value=str(self.root)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["ext", "disable", "superpowers", "--agent", "claude", "--project", "--why", "test"])
        self.assertEqual(rc, 1)
        self.assertEqual(runner.calls, [])

    def test_setup_propagates_extension_host_failure(self):
        with mock.patch.object(ext, "retire", return_value=[(False, "host permission denied")]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "host permission denied"):
                setup._extensions("claude")

    def test_overlay_rechecks_mode_before_each_vendor_command(self):
        calls = []
        def run(argv, timeout=300):
            calls.append(argv)
            self.config.write_text('{"work":{"mode":"guided"}}')
            return 0, "", ""
        with mock.patch.object(ext, "runner", return_value=run), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["ext", "enable", "superpowers", "--why", "test"])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1)

    def test_overlay_revocation_before_transaction_preserves_saved_bytes(self):
        update = core.config_update
        revoked = b'{"work":{"mode":"guided"}}'
        def revoke(*args, **kwargs):
            self.config.write_bytes(revoked)
            return update(*args, **kwargs)
        runner = FakeRun()
        with mock.patch.object(core, "config_update", side_effect=revoke), \
                mock.patch.object(ext, "runner", return_value=runner), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["ext", "enable", "superpowers", "--why", "test"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.config.read_bytes(), revoked)
        self.assertEqual(runner.calls, [])

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
        self.root, self.config = automatic(self)

    def test_auto_runs_verified_engine_off_allowlist(self):
        with mock.patch.dict("os.environ", {"FAKE_ENGINE_OUT": str(self.out)}):
            for args in (["hooks", "on"], ["hooks", "off"], ["hooks", "status"], ["install", "--user"],
                         ["update"], ["link"], ["doctor", "--fix"], ["skills", "--", "hooks", "on"]):
                self.assertEqual(impeccable.run(args, cwd=str(self.project)), 0)
                self.assertEqual(json.loads(self.out.read_text())["argv"], args)

    def test_mode_changed_during_engine_verification_blocks_launch(self):
        verify = impeccable.verified_engine
        def revoke():
            result = verify()
            self.config.write_text('{"work":{"mode":"guided"}}')
            return result
        with mock.patch.object(impeccable, "verified_engine", side_effect=revoke), \
                mock.patch.dict("os.environ", {"FAKE_ENGINE_OUT": str(self.out)}):
            self.assertEqual(impeccable.run(["hooks", "on"], cwd=str(self.project)), 1)
        self.assertFalse(self.out.exists())

    def test_auto_preserves_engine_exit_status(self):
        with mock.patch.dict("os.environ", {"FAKE_ENGINE_OUT": str(self.out), "FAKE_ENGINE_RC": "7"}):
            self.assertEqual(impeccable.run(["hooks", "on"], cwd=str(self.project)), 7)

    def test_auto_refuses_unknown_forms_and_unresolved_remote_helpers(self):
        with mock.patch.dict("os.environ", {"FAKE_ENGINE_OUT": str(self.out)}), \
                contextlib.redirect_stderr(io.StringIO()):
            for args in (["a-new-verb"], ["hooks", "--unknown"], ["generate-image", "--prompt-file", "p.txt"],
                         ["live", "--target", "../other/index.html"]):
                self.assertEqual(impeccable.run(args, cwd=str(self.project)), 1, args)
                self.assertFalse(self.out.exists())


class AdapterMutationDiagnostic(unittest.TestCase):
    def test_each_regression_detects_its_authorization_mutation(self):
        real_developers = impeccable._developers
        real_extensions = setup._extensions
        real_authorize = progress.authorize_command
        real_update = core.config_update

        def unchecked_update(update, path=None, **kwargs):
            return real_update(update, path)

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

        def stale_engine_check(*args, **kwargs):
            if kwargs.get("consume", True):
                return True
            return real_developers(*args, **kwargs)

        def wrong_repository(action, target, **kwargs):
            kwargs.pop("repo", None)
            return real_authorize(action, target, **kwargs)

        checks = [
            (ExtensionAutoTests, "test_overlay_revocation_before_transaction_preserves_saved_bytes", core,
             "config_update", unchecked_update),
            (ExtensionAutoTests, "test_app_configuration_secrets_stay_out_of_acceptance_and_notifications", ext,
             "_app_line", raw_app_line),
            (ExtensionAutoTests, "test_vendor_arguments_and_output_stay_out_of_audit_and_display", ext,
             "apply", raw_apply),
            (ExtensionAutoTests, "test_auto_accepts_observed_drift_and_can_acknowledge_it", ext,
             "mutation_allowed", lambda *a, **k: core.set_by_developer()),
            (ExtensionAutoTests, "test_setup_accepts_drift_in_auto", ext,
             "mutation_allowed", lambda *a, **k: core.set_by_developer()),
            (ExtensionAutoTests, "test_project_change_reaches_vendor_and_preserves_host_failure", ext,
             "mutation_allowed", lambda *a, **k: core.set_by_developer()),
            (ExtensionAutoTests, "test_guided_member_and_invalid_configuration_preserve_drift", ext,
             "mutation_allowed", lambda *a, **k: True),
            (ExtensionAutoTests, "test_mode_revoked_while_observing_preserves_drift", ext,
             "mutation_allowed", lambda *a, **k: True),
            (ExtensionAutoTests, "test_overlay_rechecks_mode_before_each_vendor_command", ext,
             "_authorized_runner", lambda verb, run, facts: run),
            (ExtensionAutoTests, "test_setup_propagates_extension_host_failure", setup,
             "_extensions", suppressed_failure),
            (ExtensionAutoTests, "test_project_change_cannot_move_session_authority_to_another_repository",
             progress, "authorize_command", wrong_repository),
            (ImpeccableAutoTests, "test_auto_runs_verified_engine_off_allowlist", impeccable,
             "_developers", lambda *a, **k: False),
            (ImpeccableAutoTests, "test_auto_preserves_engine_exit_status", impeccable,
             "_developers", lambda *a, **k: False),
            (ImpeccableAutoTests, "test_mode_changed_during_engine_verification_blocks_launch", impeccable,
             "_developers", stale_engine_check),
            (ImpeccableAutoTests, "test_auto_refuses_unknown_forms_and_unresolved_remote_helpers", impeccable,
             "AUTOMATIC_MANAGEMENT", impeccable.AUTOMATIC_MANAGEMENT | {("a-new-verb",)}),
        ]
        for cls, name, module, attribute, mutation in checks:
            with self.subTest(regression=name):
                result = unittest.TestResult()
                with mock.patch.object(module, attribute, mutation), contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    cls(name).run(result)
                self.assertEqual(result.errors, [], "a fixture or import error is not causal proof")
                self.assertGreaterEqual(len(result.failures), 1, "the regression must catch the behavior mutation")

