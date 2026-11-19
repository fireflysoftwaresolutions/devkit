# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""The registry's pull request cache keeps a merged pull request after it falls out of the newest 200 GitHub lists,
so the branch map, `dev free` and the backup still tell its squash-merged branch from the cache. gh is a stand-in;
devkit's state is a temporary folder. Run: python3 -m unittest tests.test_registry_prs -v"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import branches, core, registry  # noqa: E402

SLUG = "acme/api"
DAY = 86400
GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
       "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev", "GIT_COMMITTER_EMAIL": "ana@example.com"}


def pr(number, state, branch, days_ago=1):
    return {"number": number, "state": state, "headRefName": branch, "title": f"PR {number}",
            "url": f"https://github.com/{SLUG}/pull/{number}",
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * DAY)),
            "isDraft": False, "headRefOid": f"{number:040x}", "isCrossRepository": False,
            "mergeCommit": {"oid": f"{number + 100:040x}"} if state == "MERGED" else None}


class PrCacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(core.real(tmp.name))
        for target in (mock.patch.object(core, "STATE", self.root / "state"),
                       mock.patch.object(registry, "STATE", self.root / "state"),
                       mock.patch.object(core, "HANDOFF", self.root / "handoff")):
            target.start()
            self.addCleanup(target.stop)

    def listed(self, rows, **kw):
        # refresh the cache with gh listing `rows` (all states) and no open-PR details
        def gh(args, **_):
            return 0, json.dumps(rows if "all" in args else []), ""
        with mock.patch.object(registry, "sh", gh):
            return registry.prs(SLUG, refresh=True, **kw)

    def checkout(self, *branches) -> Path:
        # a clone of SLUG on this Mac with these local branches
        repo = self.root / "Development" / "acme" / "api"
        repo.mkdir(parents=True)
        env = dict(os.environ, **GIT)
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"],
                     *(["branch", b] for b in branches)):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
        return repo

    def numbers(self) -> list:
        return sorted(p["number"] for p in branches.cached_prs(SLUG))

    def test_a_merged_pr_that_falls_out_of_the_newest_list_stays_and_a_listed_pr_takes_the_newest_state(self):
        self.listed([pr(1, "MERGED", "feat/old-export"), pr(2, "OPEN", "feat/search"),
                     pr(4, "CLOSED", "feat/dropped")])
        self.listed([pr(2, "MERGED", "feat/search"), pr(3, "CLOSED", "feat/abandoned")])  # 1 and 4: older than 200
        cached = branches.cached_prs(SLUG)
        self.assertEqual(sorted((p["number"], p["state"]) for p in cached),
                         [(1, "MERGED"), (2, "MERGED"), (3, "CLOSED")])
        self.assertEqual(registry.pr_for_branch(cached, "feat/old-export")["state"], "MERGED")


    def test_a_dropped_merged_pr_stays_while_its_branch_is_here_or_for_90_days(self):
        # 1: merged 200 days ago, its branch still here; 2: merged 30 days ago; 3: merged 120 days ago, branch gone
        repo = self.checkout("feat/still-here")
        old = [pr(1, "MERGED", "feat/still-here", 200), pr(2, "MERGED", "feat/recent", 30),
               pr(3, "MERGED", "feat/long-gone", 120), pr(5, "OPEN", "feat/search")]
        self.listed(old, repo=repo)
        self.listed([pr(5, "OPEN", "feat/search")], repo=repo)
        self.assertEqual(self.numbers(), [1, 2, 5])

    def test_without_a_named_checkout_the_branches_of_the_projects_map_count(self):
        # dev status, the dashboard and gitx name no checkout: the projects map names this Mac's clone of SLUG
        repo = self.checkout("feat/still-here")
        core.registry_cache().parent.mkdir(parents=True, exist_ok=True)
        core.registry_cache().write_text(json.dumps({"projects": [{"id": SLUG, "path": str(repo), "github": SLUG}]}))
        self.listed([pr(1, "MERGED", "feat/still-here", 200), pr(3, "MERGED", "feat/long-gone", 120)])
        self.listed([pr(5, "OPEN", "feat/search")])
        self.assertEqual(self.numbers(), [1, 5])

    def test_a_refresh_writes_the_cache_only_under_its_file_lock(self):
        # another process holds the cache's lock: this refresh returns what gh listed and leaves the file to it
        self.listed([pr(1, "MERGED", "feat/old-export")])
        cache = core.STATE / "prs" / (SLUG.replace("/", "__") + ".json")
        held, done = threading.Event(), threading.Event()

        def holder():
            with core.file_lock(cache):
                held.set()
                done.wait(10)
        t = threading.Thread(target=holder)
        t.start()
        self.addCleanup(t.join)
        self.addCleanup(done.set)
        held.wait(10)
        with mock.patch.object(registry, "PRS_WAIT", 0.05, create=True):  # a short wait, whatever the code reads
            rows = self.listed([pr(2, "OPEN", "feat/search")])
        self.assertIn(2, [p["number"] for p in rows])
        self.assertEqual(self.numbers(), [1], "the refresh wrote the cache without its lock")


if __name__ == "__main__":
    unittest.main()
