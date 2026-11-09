# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The bug registry's trust files need a maintainer's review: .github/CODEOWNERS names, for the workflow, its script,
# the trust rules it imports, the maintainers file and CODEOWNERS itself, an account the maintainers file lists.
# Run: python3 -m unittest tests.test_guard_review -v
import fnmatch
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import bug_registry_action as action  # noqa: E402

CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
TRUST_FILES = (".github/CODEOWNERS", ".github/bug-registry-maintainers", ".github/workflows/bug-registry.yml",
               "tools/bug_registry_action.py", "lib/devkit/bugs.py")


def owners(path: str, text: str) -> list:
    """The owners GitHub gives `path` (repository-relative): the last matching rule wins. Covers the rule forms this
    file uses: an anchored path, a folder ending in `/`, and `*` globs."""
    found = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *who = line.split()
        anchored = pattern.startswith("/")
        pat = pattern.lstrip("/")
        if pat.endswith("/"):
            hit = path.startswith(pat) if anchored else f"/{pat}" in f"/{path}"
        else:
            hit = fnmatch.fnmatchcase(path, pat) if anchored or "/" in pat else \
                fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pat)
        if hit:
            found = who
    return found


class RegistryTrustFilesHaveAnOwner(unittest.TestCase):

    def test_each_trust_file_exists(self):
        for rel in TRUST_FILES:
            with self.subTest(path=rel):
                self.assertTrue((ROOT / rel).is_file())

    def test_a_listed_maintainer_owns_every_trust_file(self):
        listed = {login for login, _ in action.read_maintainers(action.MAINTAINERS)}
        self.assertTrue(listed, "the maintainers file lists nobody")
        text = CODEOWNERS.read_text(encoding="utf-8")
        for rel in TRUST_FILES:
            with self.subTest(path=rel):
                who = owners(rel, text)
                self.assertTrue(who, f"{rel} has no code owner")
                self.assertTrue(all(re.fullmatch(r"@[A-Za-z0-9][A-Za-z0-9-]{0,38}", w) for w in who), who)
                self.assertTrue({w[1:].lower() for w in who} <= listed, f"{rel}: {who} not all in {sorted(listed)}")

    def test_the_owner_matcher_reads_the_last_rule(self):
        text = "/tools/ @a\n*.py @b\n/tools/bug_registry_action.py @c\n"
        self.assertEqual(owners("tools/bug_registry_action.py", text), ["@c"])
        self.assertEqual(owners("tools/other.py", text), ["@b"])
        self.assertEqual(owners("lib/x.md", text), [])


if __name__ == "__main__":
    unittest.main()
