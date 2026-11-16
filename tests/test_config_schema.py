# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Configuration contracts exercised only against temporary saved files."""
import contextlib
import io
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import audit, core, selfcheck


class ConfigSchemaTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text('{}\n')
        audit_patch = mock.patch.object(audit, "AUDIT", Path(folder.name) / "audit")
        audit_patch.start()
        self.addCleanup(audit_patch.stop)

    def test_set_rejects_unknown_keys_invalid_modes_ranges_and_empty_segments(self):
        for key, value in [("work.no_such_setting", "true"), ("work.mode", "banana"),
                           ("docker.cpu", "-4"), ("work..mode", "auto"),
                           (".work.mode", "auto"), ("work.mode.", "auto"),
                           ("docker.cpu", "true"), ("work", '{"mode":"auto","typo":true}')]:
            with self.subTest(key=key, value=value):
                before = self.path.read_bytes()
                with self.assertRaises(core.ConfigError):
                    core.config_set(key, value, path=self.path, developer=True)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(audit.records(1, "dev.config.set")), [])

    def test_a_writer_saves_past_a_saved_unknown_key_it_does_not_touch(self):
        self.path.write_text('{"legacy_root":true,"editor":"vim"}\n')
        self.assertEqual(core.config_set("docker.cpu", "4", path=self.path, developer=True)[1], 4)
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["docker"], saved["legacy_root"], saved["editor"]), ({"cpu": 4}, True, "vim"))
        self.assertIn("unknown key legacy_root", core.config_snapshot(self.path)[1])  # still invalid until removed

    def test_an_unknown_saved_key_is_named_with_the_command_that_removes_it(self):
        self.path.write_text('{"legacy_root":true}\n')
        err = io.StringIO()
        with mock.patch.object(core, "CONFIG_FILE", self.path), contextlib.redirect_stderr(err):
            cfg = core.load_config()
        self.assertIn("unknown key legacy_root: devkit does not read it; remove it with dev config edit",
                      err.getvalue())
        self.assertEqual(cfg["work"]["mode"], "guided")  # invalid configuration never grants automatic authority

    def test_deep_keys_are_editable(self):
        self.assertEqual(core.config_set("audit.retention.local_months", "14", path=self.path,
                                         developer=True), (13, 14))
        self.assertEqual(json.loads(self.path.read_text()), {"audit": {"retention": {"local_months": 14}}})

    def test_whole_candidate_is_validated_and_last_error_can_be_repaired(self):
        self.path.write_text('{"work":{"mode":"banana"}}\n')
        with self.assertRaises(core.ConfigError):
            core.config_set("docker.cpu", "4", path=self.path, developer=True)
        self.assertEqual(core.config_set("work.mode", "guided", path=self.path, developer=True),
                         ("banana", "guided"))
        self.assertEqual(json.loads(self.path.read_text()), {"work": {"mode": "guided"}})

    def test_json_duplicate_keys_and_nonfinite_values_are_refused(self):
        for value in ['{"cpu":4,"cpu":6}', '{"cpu":NaN}', '{"cpu":1e999}', '{"cpu":']:
            with self.subTest(value=value), self.assertRaises(core.ConfigError):
                core.config_set("docker", value, path=self.path, developer=True)
        self.assertEqual(self.path.read_text(), '{}\n')

    def test_load_omits_unknown_and_invalid_values_without_rewriting(self):
        raw = '{"unknownroot":true,"work":{"mode":"banana"},"editor":"vim"}\n'
        self.path.write_text(raw)
        with mock.patch.object(core, "CONFIG_FILE", self.path):
            cfg = core.load_config()
        self.assertNotIn("unknownroot", cfg)
        self.assertNotEqual(cfg["work"].get("mode"), "banana")
        self.assertEqual(cfg["editor"], "vim")
        self.assertTrue(core.config_errors(json.loads(raw)))
        self.assertEqual(self.path.read_text(), raw)

    def test_dynamic_records_are_closed_and_profile_names_cannot_traverse(self):
        for raw in [{"jobs": {"custom": {"cmd": ["doctor"], "surprise": 1}}},
                    {"codex": {"profiles": {"../outside": {"model": "example"}}}},
                    {"host_dirs": {"git.example.com": "../outside"}},
                    {"skills": {"rules": [{"id": "example", "arbitrary": True}]}}]:
            with self.subTest(raw=raw):
                self.assertTrue(core.config_errors(raw))

    def test_extension_provenance_is_checked_by_the_pure_schema(self):
        for entry in [{"kind": "mcp", "name": "example", "license": "MIT", "why": "testing",
                       "command": "npx", "args": ["example@latest"]},
                      {"kind": "mcp", "name": "example", "license": "MIT", "why": "testing",
                       "url": "http://example.com"}]:
            self.assertTrue(core.config_errors({"ext": {"add": [entry]}}))

    def test_invalid_custom_job_is_omitted_from_effective_configuration(self):
        raw = {"jobs": {"custom": {"cmd": ["doctor"], "typo": True}}}
        self.path.write_text(json.dumps(raw))
        with mock.patch.object(core, "CONFIG_FILE", self.path):
            cfg = core.load_config()
        errors = core.config_errors(raw)
        self.assertTrue(errors)
        self.assertNotIn("custom", cfg.get("jobs", {}))

    def test_schema_drift_is_reported_for_an_undocumented_reader_key(self):
        home = self.path.parent
        source = home / "lib" / "devkit"
        source.mkdir(parents=True)
        (home / "docs").mkdir()
        (source / "example.py").write_text('from devkit.core import CFG\nx = CFG.get("unknown_feature")\n')
        (home / "docs" / "configuration.md").write_text('| `unknown_feature` | empty | example |\n')
        findings = selfcheck.d6_config_keys(home)
        self.assertTrue(any("schema" in str(f) for f in findings), findings)

    def test_supported_catalogs_and_explicit_empty_choices_round_trip(self):
        root = Path(__file__).resolve().parents[1]
        catalog = json.loads((root / "config" / "profiles.json").read_text())
        raw = {"stacks": list(catalog["stacks"]), "frameworks": list(catalog["frameworks"]),
               "codex": {"profiles": False}, "skills": {"groups": [], "optional": {}},
               "host_dirs": {"git.example.com": "git.example"},
               "jobs": {"custom": {"cmd": ["doctor"], "at": "09:15"}}}
        self.assertEqual(core.config_errors(raw), [])
        self.path.write_text(json.dumps(raw))
        with mock.patch.object(core, "CONFIG_FILE", self.path):
            cfg = core.load_config()
        errors = core.config_errors(raw)
        self.assertEqual(errors, [])
        self.assertEqual(cfg["codex"]["profiles"], False)
        self.assertEqual(cfg["skills"]["groups"], [])
        self.assertEqual(cfg["skills"]["optional"], {})
        self.assertEqual(cfg["host_dirs"], {"git.example.com": "git.example"})

    def test_domains_reject_unknown_activities_hosts_and_blank_command_prefixes(self):
        for raw in [{"skills": {"rules": [{"id": "test", "activities": ["nonesuch"]}]}},
                    {"default_host": "host..invalid"},
                    {"dev_root": ""}, {"dev_root": "relative/path"},
                    {"safeguards": {"readonly_commands": ["\n"]}},
                    {"safeguards": {"readonly_commands": ["make lint; publish"]}},
                    {"ext": {"remove": ["arbitrary-id"]}}]:
            self.assertTrue(core.config_errors(raw), raw)

    def test_invalid_json_snapshot_keeps_diagnostics_available(self):
        for raw in ['[]', '{"work":{"mode":"guided","mode":"auto"}}', '{"docker":{"cpu":NaN}}']:
            self.path.write_text(raw)
            cfg, errors = core.config_snapshot(self.path)
            self.assertTrue(errors)
            self.assertEqual(cfg["work"]["mode"], "guided")
            self.assertEqual(self.path.read_text(), raw)

    def test_catalog_drift_is_reported_before_new_frameworks_are_rejected(self):
        home = self.path.parent
        (home / "config").mkdir()
        catalog = json.loads((Path(__file__).resolve().parents[1] / "config" / "profiles.json").read_text())
        catalog["frameworks"]["future-framework"] = {}
        (home / "config" / "profiles.json").write_text(json.dumps(catalog))
        findings = selfcheck.d6_config_keys(home)
        self.assertTrue(any("catalog" in str(f) and "frameworks" in str(f) for f in findings), findings)

    def test_malformed_extension_discriminators_return_diagnostics_without_crashing(self):
        for kind in ([], {}, ["mcp"], {"value": "mcp"}):
            raw = {"ext": {"add": [{"kind": kind}]}}
            with self.subTest(kind=kind):
                self.assertTrue(core.config_errors(raw))
                self.path.write_text(json.dumps(raw))
                cfg, errors = core.config_snapshot(self.path)
                self.assertTrue(errors)
                self.assertEqual(cfg["ext"]["add"], [])
                self.assertEqual(json.loads(self.path.read_text()), raw)

    def test_unrepresentable_regexes_are_diagnosed_without_crashing(self):
        invalid = "x{99999999999999999999999999999999999999999999999}"
        for raw in ({"audit": {"redact": {"extra": [invalid]}}},
                    {"skills": {"rules": [{"id": "test", "content": invalid}]}}):
            with self.subTest(raw=raw):
                self.assertTrue(core.config_errors(raw))
                self.path.write_text(json.dumps(raw))
                self.assertTrue(core.config_snapshot(self.path)[1])
        self.assertTrue(core.audit_config({"redact": {"extra": [invalid]}})[1])

    def test_nested_shape_variants_remain_diagnosable(self):
        cases = [
            {"ext": {"add": [[], None, True]}},
            {"ext": {"add": [{"kind": "mcp", "args": 3, "env": [], "url": {}}]}},
            {"codex": {"profiles": {"valid": {"model_reasoning_effort": []}}}},
            {"progress": {"quota_steps": {"85": {}}, "slot_weights": {"review": []}}},
            {"skills": {"rules": [{"id": [], "project": {"content": {}}}]}},
            {"audit": {"level": {}, "redact": {"extra": [None, [], {}]}}},
            {"jobs": {"custom": {"cmd": {}, "at": [], "notify": {}}}},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.path.write_text(json.dumps(raw))
                self.assertTrue(core.config_errors(raw))
                self.assertTrue(core.config_snapshot(self.path)[1])

    def test_deep_json_and_regex_nesting_diagnoses_resource_limits(self):
        self.path.write_text('[' * 2000 + '0' + ']' * 2000)
        self.assertTrue(core.config_snapshot(self.path)[1])
        self.assertTrue(core.config_errors({"audit": {"redact": {"extra": ['(' * 2000 + ')' * 2000]}}}))
