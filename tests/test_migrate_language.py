# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# devkit's `language` setting moves from en-US to auto (reply in the developer's language). Neither `dev setup` nor
# the setup wizard has ever written the key: it reaches ~/.config/devkit/config.json only through the developer's own
# `dev config set language …` or an edit. So a stored value is the developer's choice and is kept, and a file without
# the key reads as auto, with nothing rewritten.
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_migrate_language
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "lib"))
sys.path.insert(0, str(TESTS))

import _sandbox  # noqa: E402,F401

from devkit import audit, core  # noqa: E402


class LanguageDefaultTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.file = Path(tmp.name) / "config.json"
        for p in (mock.patch.object(core, "CONFIG_FILE", self.file),
                  mock.patch.object(audit, "AUDIT", Path(tmp.name) / "audit")):  # never the real audit trail
            p.start()
            self.addCleanup(p.stop)

    def test_an_unset_language_is_auto_and_a_stored_one_is_kept_untouched(self):
        self.file.write_text(json.dumps({"editor": "vim"}))
        self.assertEqual(core.load_config()["language"], "auto")
        for chosen in ("en-US", "es-ES"):
            self.file.write_text(json.dumps({"language": chosen}))
            self.assertEqual(core.load_config()["language"], chosen)
            self.assertEqual(json.loads(self.file.read_text()), {"language": chosen})

    def test_the_developer_sets_auto_or_a_locale_and_nothing_else(self):
        self.file.write_text("{}")
        for value in ("auto", "es-ES", "pt_BR", "fr"):
            with self.subTest(value=value):
                core.config_set("language", value, path=self.file, developer=True)
                self.assertEqual(json.loads(self.file.read_text())["language"], value)
        for value in ("spanish", "es-ES-x-1", "e", "12"):
            with self.subTest(value=value), self.assertRaises(core.ConfigError) as e:
                core.config_set("language", value, path=self.file, developer=True)
            self.assertIn("auto or a locale", str(e.exception))
        self.assertEqual(json.loads(self.file.read_text())["language"], "fr")


if __name__ == "__main__":
    unittest.main()
