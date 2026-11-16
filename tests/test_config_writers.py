# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Writer boundaries use real temporary files; external installers are never run."""
import contextlib
import io
import os
import json
import sys
import tempfile
import unittest
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import audit, cli, core, ext, jobs, setup, wizard
import _sandbox


class ConfigWriterTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "config.json"
        for module in (core, setup, wizard):
            patch = mock.patch.object(module, "CONFIG_FILE", self.path)
            patch.start()
            self.addCleanup(patch.stop)

    def test_jobs_refuses_invalid_saved_config_without_installing_or_writing(self):
        self.path.write_text('{"work":{"mode":"banana"}}\n')
        before = self.path.read_bytes()
        with mock.patch.object(jobs, "install", side_effect=AssertionError("installation must not start")):
            with self.assertRaises(ValueError):
                jobs.set_schedule("sync", every="20m")
        self.assertEqual(self.path.read_bytes(), before)

    def test_setup_preserves_invalid_json_for_explicit_repair(self):
        self.path.write_text('{"broken":')
        with mock.patch.object(core, "gh_user", return_value={}), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(core.ConfigError):
                setup.ensure_config()
        self.assertEqual(self.path.read_text(), '{"broken":')

    def test_invalid_editor_candidate_leaves_original_bytes_and_mode(self):
        self.path.write_text('{"editor":"vim"}\n')
        self.path.chmod(0o640)
        def edit(argv):
            Path(argv[-1]).write_text('{"work":{"mode":"banana"}}')
            return 0
        with mock.patch.object(cli, "run", side_effect=edit), \
                mock.patch.object(core, "set_by_developer", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_config(SimpleNamespace(config_cmd="edit")), 2)
        self.assertEqual(self.path.read_text(), '{"editor":"vim"}\n')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_invalid_saved_config_show_reports_diagnostics_and_nonzero(self):
        self.path.write_text('{"work":{"mode":"banana"},"editor":"vim"}')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.cmd_config(SimpleNamespace(config_cmd="show"))
        self.assertNotEqual(result, 0)
        doc = json.loads(output.getvalue())
        self.assertEqual(doc["effective"]["editor"], "vim")
        self.assertTrue(doc["errors"])

    def test_profile_writer_refuses_traversal_before_any_file_write(self):
        home = self.root / "codex"
        home.mkdir()
        with mock.patch.object(setup, "CODEX_HOME", home), self.assertRaises(ValueError):
            setup.write_codex_profiles({"profiles": {"../outside": {"model": "example"}}})
        self.assertFalse((self.root / "outside.config.toml").exists())

    def test_setter_preserves_symlink_permissions_and_concurrent_updates(self):
        self.path.write_text('{}\n')
        self.path.chmod(0o640)
        link = self.root / "linked.json"
        link.symlink_to(self.path)
        barrier = threading.Barrier(2)
        def change(key, value):
            barrier.wait(timeout=5)
            core.config_set(key, value, path=link, developer=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(change, "docker.cpu", "4"), pool.submit(change, "editor", "vim")]
            for future in futures:
                future.result(timeout=10)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(json.loads(self.path.read_text()), {"docker": {"cpu": 4}, "editor": "vim"})

    def test_audit_records_metadata_without_extension_credentials(self):
        entry = {"kind": "mcp", "name": "example", "license": "MIT", "why": "testing",
                 "url": "https://example.com", "env": {"SERVICE_TOKEN": "private-test-credential"}}
        with mock.patch.object(audit, "AUDIT", self.root / "audit"):
            core.config_set("ext.add", json.dumps([entry]), path=self.path, developer=True)
            records = list(audit.records(1, "dev.config.set"))
        self.assertEqual(len(records), 1)
        self.assertNotIn("private-test-credential", json.dumps(records))
        self.assertIn("value_digest", json.dumps(records))

    def test_editor_concurrent_update_is_preserved(self):
        self.path.write_text('{}\n')
        def edit(candidate):
            candidate.write_text('{"editor":"vim"}')
            core.config_set("docker.cpu", "4", path=self.path, developer=True)
            return 0
        with self.assertRaises(core.ConfigError):
            core.config_edit(edit, path=self.path, developer=True)
        self.assertEqual(json.loads(self.path.read_text()), {"docker": {"cpu": 4}})

    def test_extension_command_keeps_saved_entries_newer_than_its_effective_snapshot(self):
        saved = {"kind": "mcp", "name": "first", "license": "MIT", "why": "testing",
                 "url": "https://first.example.com", "agents": ["codex"]}
        self.path.write_text(json.dumps({"ext": {"add": [saved]}}))
        args = SimpleNamespace(ext_cmd="add", why="testing", kind="mcp", name="second", license="MIT",
                               agent="codex", url="https://second.example.com", command="")
        with mock.patch.dict(core.CFG, {"ext": {"add": []}}), \
                mock.patch.object(core, "set_by_developer", return_value=True), \
                mock.patch.object(ext, "team", return_value={}), \
                mock.patch.object(ext, "runner", return_value=lambda *a, **k: (0, "", "")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ext.cmd_change(args), 0)
        entries = json.loads(self.path.read_text())["ext"]["add"]
        self.assertEqual([entry["name"] for entry in entries], ["first", "second"])

    def test_wizard_save_merges_into_the_latest_file(self):
        self.path.write_text('{"editor":"vim","work":{"mode":"guided"}}')
        answers = {"root": "~/Development", "handoff": "test/handoff", "use_claude": False,
                   "use_codex": True, "claude_mode": "default", "codex_mode": "full", "stacks": [],
                   "lock": False, "hosts": [], "clouds": [], "cpu": 4, "mem": 8, "disk": 50,
                   "autostart": False, "gc_auto": False, "interval": 60, "frameworks": []}
        runner = SimpleNamespace(d={"config": {}, "gh": {"login": "test"}}, a=answers, saved_file=False)
        checklist = SimpleNamespace(start=lambda i: None, finish=lambda *args: None)
        wizard._Wizard._save(runner, checklist, 0)
        result = json.loads(self.path.read_text())
        self.assertEqual(result["editor"], "vim")
        self.assertEqual(result["work"], {"mode": "guided"})
        self.assertEqual(result["frameworks"], [])

    def test_malformed_json_does_not_break_cli_initialization(self):
        saved = self.root / ".config" / "devkit" / "config.json"
        saved.parent.mkdir(parents=True)
        saved.write_text('{"work":')
        source = Path(__file__).resolve().parents[1] / "bin" / "dev"
        result = subprocess.run(_sandbox.boot(source, "config", "show"),
                                env=_sandbox.env(HOME=str(self.root)), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(json.loads(result.stdout)["errors"])
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(saved.read_text(), '{"work":')

    def test_deleted_editor_candidate_cannot_reset_saved_configuration(self):
        self.path.write_text('{"editor":"vim","work":{"mode":"guided"}}\n')
        before = self.path.read_bytes()
        def edit(candidate):
            candidate.unlink()
            return 0
        with self.assertRaises(core.ConfigError):
            core.config_edit(edit, path=self.path, developer=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_editor_bytes_are_retained_for_recovery(self):
        self.path.write_text('{"editor":"vim"}\n')
        candidates = []
        def edit(candidate):
            candidates.append(candidate)
            candidate.write_bytes(b'{"incomplete":')
            return 0
        with self.assertRaises(core.ConfigError) as error:
            core.config_edit(edit, path=self.path, developer=True)
        self.assertEqual(self.path.read_bytes(), b'{"editor":"vim"}\n')
        self.assertTrue(candidates[0].exists(), "failed edits need a recoverable candidate")
        self.assertEqual(candidates[0].read_bytes(), b'{"incomplete":')
        self.assertEqual(candidates[0].stat().st_mode & 0o777, 0o600)
        self.assertIn(str(candidates[0]), str(error.exception))

    def test_editor_candidate_symlink_cannot_replace_configuration_or_chmod_its_target(self):
        self.path.write_text('{"editor":"vim"}\n')
        target = self.root / "other.json"
        target.write_text('{"work":{"mode":"banana"}}')
        target.chmod(0o640)
        def edit(candidate):
            candidate.unlink()
            candidate.symlink_to(target)
            return 0
        with self.assertRaises(core.ConfigError):
            core.config_edit(edit, path=self.path, developer=True)
        self.assertEqual(self.path.read_bytes(), b'{"editor":"vim"}\n')
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(target.read_text(), '{"work":{"mode":"banana"}}')

    def test_editor_arguments_are_parsed_and_gui_editors_wait(self):
        for editor, expected in [('code --new-window', ['code', '--new-window', '--wait']),
                                 ('"/Applications/Code Editor/bin/code" --wait',
                                  ['/Applications/Code Editor/bin/code', '--wait']),
                                 ('open -a "Visual Studio Code"', ['open', '-a', 'Visual Studio Code', '-W']),
                                 ('vim -f', ['vim', '-f'])]:
            with self.subTest(editor=editor):
                self.path.write_text('{}\n')
                launched = []
                def edit(argv):
                    launched.append(argv[:-1])
                    Path(argv[-1]).write_text('{"editor":"vim"}')
                    return 0
                with mock.patch.dict(os.environ, {"EDITOR": editor}), \
                        mock.patch.object(cli, "run", side_effect=edit), \
                        mock.patch.object(core, "set_by_developer", return_value=True), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.cmd_config(SimpleNamespace(config_cmd="edit")), 0)
                self.assertEqual(launched, [expected])
                self.assertEqual(json.loads(self.path.read_text()), {"editor": "vim"})

    def test_editor_arguments_reject_malformed_quotes_before_launch(self):
        self.path.write_text('{"editor":"vim"}\n')
        with mock.patch.dict(os.environ, {"EDITOR": 'code "unterminated'}), \
                mock.patch.object(core, "set_by_developer", return_value=True), \
                mock.patch.object(cli, "run", side_effect=AssertionError("invalid editor must not launch")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_config(SimpleNamespace(config_cmd="edit")), 2)
        self.assertEqual(self.path.read_bytes(), b'{"editor":"vim"}\n')

