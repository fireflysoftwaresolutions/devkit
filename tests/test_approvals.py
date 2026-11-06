# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Optional skill groups offered by the setup wizard.
# Run: python3 -m unittest tests.test_approvals
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core  # noqa: E402


class OptionalSkillGroupTests(unittest.TestCase):
    def test_iac_is_offered_for_terraform_and_never_installed_by_default(self):
        # M128: Niobe's Terraform skills are group `iac`; the wizard offers it for infrastructure work only
        groups = {s.get("group") for s in json.loads((ROOT / "config" / "skills.json").read_text())["sources"]}
        self.assertIn("iac", groups)
        self.assertNotIn("iac", core.DEFAULTS["skills"]["groups"])
        offered = [core.offered_skill_groups(c, core.DEFAULTS) for c in (["aws", "terraform"], ["aws"])]
        self.assertEqual(offered, [["iac"], []])


if __name__ == "__main__":
    unittest.main()
