# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The registry's pull request cache keeps a merged pull request after it falls out of the newest 200 GitHub lists,
so the branch map, `dev free` and the backup still tell its squash-merged branch from the cache. gh is a stand-in;
devkit's state is a temporary folder. Run: python3 -m unittest tests.test_registry_prs -v"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import branches, core, registry  # noqa: E402

SLUG = "acme/api"


def pr(number, state, branch):
    return {"number": number, "state": state, "headRefName": branch, "title": f"PR {number}",
            "url": f"https://github.com/{SLUG}/pull/{number}", "updatedAt": f"2026-11-{number:02d}T00:00:00Z",
            "isDraft": False, "headRefOid": f"{number:040x}", "isCrossRepository": False,
            "mergeCommit": {"oid": f"{number + 100:040x}"} if state == "MERGED" else None}


class PrCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (mock.patch.object(core, "STATE", Path(tmp.name)),
                       mock.patch.object(registry, "STATE", Path(tmp.name))):
            target.start()
            self.addCleanup(target.stop)

    def listed(self, rows):
        """Refresh the cache with gh listing `rows` (all states) and no open-PR details."""
        def gh(args, **kw):
            return 0, json.dumps(rows if "all" in args else []), ""
        with mock.patch.object(registry, "sh", gh):
            return registry.prs(SLUG, refresh=True)

    def test_a_merged_pr_that_falls_out_of_the_newest_list_stays_and_a_listed_pr_takes_the_newest_state(self):
        self.listed([pr(1, "MERGED", "feat/old-export"), pr(2, "OPEN", "feat/search"),
                     pr(4, "CLOSED", "feat/dropped")])
        self.listed([pr(2, "MERGED", "feat/search"), pr(3, "CLOSED", "feat/abandoned")])  # 1 and 4: older than 200
        cached = branches.cached_prs(SLUG)
        self.assertEqual(sorted((p["number"], p["state"]) for p in cached),
                         [(1, "MERGED"), (2, "MERGED"), (3, "CLOSED")])
        self.assertEqual(registry.pr_for_branch(cached, "feat/old-export")["state"], "MERGED")


if __name__ == "__main__":
    unittest.main()
