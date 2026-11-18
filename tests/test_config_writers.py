# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Writer boundaries use real temporary files; external installers are never run."""
import contextlib
import io
import os
import json
import sys
import tempfile
import time
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


def setUpModule():
    """Each configuration write keeps a copy in core.STATE/backups. Every test here gets a temporary state folder,
    so a run of this file outside the test package, without its temporary home, never adds copies to the real one
    or rotates the real copies away."""
    folder = tempfile.TemporaryDirectory()
    unittest.addModuleCleanup(folder.cleanup)
    state = mock.patch.object(core, "STATE", Path(folder.name))
    state.start()
    unittest.addModuleCleanup(state.stop)


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
            core.config_set(key, value, path=link)
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
            core.config_set("ext.add", json.dumps([entry]), path=self.path)
            records = list(audit.records(1, "dev.config.set"))
        self.assertEqual(len(records), 1)
        self.assertNotIn("private-test-credential", json.dumps(records))
        self.assertIn("value_digest", json.dumps(records))

    def test_editor_concurrent_update_is_preserved(self):
        self.path.write_text('{}\n')
        def edit(candidate):
            candidate.write_text('{"editor":"vim"}')
            core.config_set("docker.cpu", "4", path=self.path)
            return 0
        with self.assertRaises(core.ConfigError):
            core.config_edit(edit, path=self.path)
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
        runner = SimpleNamespace(d={"config": {}, "gh": {"login": "test"}}, a=answers, saved_file=False,
                                 unattended=False)
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
            core.config_edit(edit, path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_editor_bytes_are_retained_for_recovery(self):
        self.path.write_text('{"editor":"vim"}\n')
        candidates = []
        def edit(candidate):
            candidates.append(candidate)
            candidate.write_bytes(b'{"incomplete":')
            return 0
        with self.assertRaises(core.ConfigError) as error:
            core.config_edit(edit, path=self.path)
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
            core.config_edit(edit, path=self.path)
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


STAMP = "20261117T195600Z"  # the fixed UTC clock of the copy tests: every copy is made in this second


class ConfigCopyTests(unittest.TestCase):
    """Each write keeps a copy of the configuration it replaces, so a bad write takes one command to undo. The
    configuration file and the state folder are temporary; the developer's own are never touched."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "config.json"
        self.backups = self.root / "state" / "backups"
        clock = time.strptime(STAMP, "%Y%m%dT%H%M%SZ")
        for patch in (mock.patch.object(core, "CONFIG_FILE", self.path),
                      mock.patch.object(core, "STATE", self.root / "state"),
                      mock.patch.object(core, "_copy_warned", [False], create=True),  # said once per process
                      mock.patch.object(core.time, "gmtime", return_value=clock)):
            patch.start()
            self.addCleanup(patch.stop)

    def copies(self) -> list:
        return sorted(p.name for p in self.backups.glob("devkit-config.json.*"))

    def test_a_write_keeps_a_private_copy_of_the_previous_content(self):
        self.path.write_text('{"editor":"vim"}\n')
        self.path.chmod(0o644)
        core.config_update(lambda saved: {**saved, "editor": "nano"})
        self.assertEqual(self.copies(), [f"devkit-config.json.{STAMP}"])
        copy = self.backups / f"devkit-config.json.{STAMP}"
        self.assertEqual(copy.read_text(), '{"editor":"vim"}\n')
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text()), {"editor": "nano"})

    def test_rotation_keeps_the_newest_twenty_copies_and_leaves_other_files(self):
        self.backups.mkdir(parents=True)
        docker = self.backups / "config.json.20261117-155042"  # setup.backup's copy of ~/.docker/config.json
        docker.write_text("{}\n")
        self.path.write_text(json.dumps({"editor": "e0"}))
        for n in range(1, 26):
            core.config_update(lambda saved, n=n: {**saved, "editor": f"e{n}"})
        kept = sorted((json.loads((self.backups / name).read_text())["editor"] for name in self.copies()),
                      key=lambda editor: int(editor[1:]))
        self.assertEqual(kept, [f"e{n}" for n in range(5, 25)])
        self.assertEqual(docker.read_text(), "{}\n")

    def test_an_unchanged_write_makes_no_copy_but_a_change_does(self):
        core.config_update(lambda saved: {**saved, "work": {"mode": "guided"}})  # creates the file: nothing to copy
        core.config_update(lambda saved: saved)  # the same bytes again: no copy
        core.config_update(lambda saved: {**saved, "editor": "vim"})  # a change: the one copy
        self.assertEqual(self.copies(), [f"devkit-config.json.{STAMP}"])
        self.assertEqual(json.loads((self.backups / self.copies()[0]).read_text()), {"work": {"mode": "guided"}})

    def test_a_copy_that_cannot_be_written_still_saves_and_is_said_once(self):
        self.backups.parent.mkdir(parents=True)
        self.backups.write_text("a file where the backups folder belongs\n")
        self.path.write_text('{"editor":"vim"}\n')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            core.config_update(lambda saved: {**saved, "editor": "nano"})
            core.config_update(lambda saved: {**saved, "editor": "emacs"})
        self.assertEqual(json.loads(self.path.read_text()), {"editor": "emacs"})
        warnings = [line for line in err.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(warnings), 1, err.getvalue())
        self.assertIn("could not keep a copy", warnings[0])

    def test_invalid_configuration_error_names_the_newest_valid_copy(self):
        self.path.write_text('{"editor":"vim"}\n')
        core.config_update(lambda saved: {**saved, "editor": "nano"})   # keeps vim
        core.config_update(lambda saved: {**saved, "editor": "emacs"})  # keeps nano: the newest valid copy
        self.path.write_text('{"editor":')

        def repair(candidate):
            candidate.write_text('{"editor":"vi"}\n')
            return 0
        core.config_edit(repair)  # keeps the broken file, which is no restore point
        self.path.write_text('{"editor":')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            core.load_config()
        newest_valid = self.backups / f"devkit-config.json.{STAMP}-1"
        self.assertIn(f"restore with: cp {newest_valid} {self.path}", err.getvalue())
        self.assertEqual(json.loads(newest_valid.read_text()), {"editor": "nano"})

