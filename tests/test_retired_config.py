# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Retired configuration keys: a configuration a former devkit version wrote with `safeguards` still loads with no
error and keeps its mode; devkit ignores the key, names it once, and every writer drops it from what it saves.
The tests use temporary configuration files only. Run: python3 -m unittest tests.test_retired_config"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import audit, configschema, core, progress, setup  # noqa: E402

# what devkit 26.11.19 and earlier wrote: all three keys of the retired group
OLD = {"work": {"mode": "guided"}, "editor": "vim",
       "safeguards": {"readonly_commands": ["make lint"], "irreversible_main": "enforce",
                      "irreversible_member": "audit"}}
NOTE = "retired setting safeguards is ignored; the next configuration change removes it"


class RetiredConfigTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "config.json"
        self.path.write_text(json.dumps(OLD))
        patch = mock.patch.object(audit, "AUDIT", Path(folder.name) / "audit")
        patch.start()
        self.addCleanup(patch.stop)

    def saved(self):
        return json.loads(self.path.read_text())

    def test_a_configuration_with_every_safeguards_key_loads_with_no_error_and_keeps_its_mode(self):
        cfg, errors = core.config_snapshot(self.path)
        self.assertEqual(errors, [])
        self.assertEqual(cfg["work"]["mode"], "guided")
        self.assertNotIn("safeguards", cfg)
        self.assertEqual(progress.mode_context(None, snapshot=(cfg, errors)), ("guided", "config"))

    def test_the_retired_key_is_named_and_only_when_present(self):
        self.assertEqual(configschema.retired_keys(OLD), ["safeguards"])
        self.assertEqual(configschema.retired_keys({"work": {"mode": "auto"}}), [])
        self.assertEqual(core.retired_settings(self.path), [NOTE])

    def test_a_setter_saves_its_change_without_the_retired_key(self):
        core.config_set("editor", "nano", path=self.path)
        self.assertEqual(self.saved(), {"work": {"mode": "guided"}, "editor": "nano"})

    def test_an_editor_candidate_is_saved_without_the_retired_key(self):
        def edit(candidate):
            doc = json.loads(candidate.read_text())
            doc["work"]["mode"] = "auto"
            candidate.write_text(json.dumps(doc))
            return 0
        core.config_edit(edit, path=self.path)
        self.assertEqual(self.saved(), {"work": {"mode": "auto"}, "editor": "vim"})

    def test_a_retired_key_cannot_be_set_again(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(core.ConfigError, "unknown key safeguards"):
            core.config_set("safeguards.irreversible_main", "enforce", path=self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_doctor_shows_one_passing_line_for_the_retired_key(self):
        self.assertEqual(setup.retired_settings_check(self.path), ("Workspace", "retired settings", True, NOTE, ""))
        self.path.write_text('{"editor":"vim"}')
        self.assertIsNone(setup.retired_settings_check(self.path))


if __name__ == "__main__":
    unittest.main()
