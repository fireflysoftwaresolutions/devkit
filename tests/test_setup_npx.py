# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pinning the npm packages an upstream skill runs with npx (lib/devkit/setup.py, pin_npx): every call of a pinned
package ends up on the reviewed version, or is listed as unpinned so the install refuses it. The skill is a
temporary folder. Run: python3 -m unittest discover -s tests -t ."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import setup  # noqa: E402

PINS = {"modern-web-guidance": "1.0.1"}


class PinNpxTests(unittest.TestCase):
    def pin(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "SKILL.md").write_text(text)
            result = setup.pin_npx(tmp, PINS)
            return (Path(tmp) / "SKILL.md").read_text(), result

    def test_latest_at_the_end_of_a_sentence_is_pinned(self):
        text, result = self.pin("Search with npx -y modern-web-guidance@latest.\n")
        self.assertEqual(text, "Search with npx -y modern-web-guidance@1.0.1.\n")
        self.assertEqual((result["replaced"], result["unpinned"]), ({"modern-web-guidance": 1}, []))

    def test_a_quoted_package_with_no_version_is_unpinned(self):
        _, result = self.pin('Run npx -y "modern-web-guidance" search "dialog"\n')
        self.assertEqual(result["unpinned"], ['SKILL.md: npx -y "modern-web-guidance"'])


if __name__ == "__main__":
    unittest.main()
