# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# `dev update --check` (spec P-fym583l7sg1 R7): the installed release, the newest one, the CHANGELOG headings in
# between, and whether the checkout and the pinned copy are behind, read-only, against temporary repositories with a
# bare origin (no network, no `dev install`). Also the once-a-day notice and its daily job.
# Run: python3 -m unittest tests.test_update_check -v
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cli, core, guard, hooks, jobs, updatecheck  # noqa: E402
from test_language_neutral_hooks import HookCase  # noqa: E402

ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada",
               GIT_COMMITTER_EMAIL="ada@example.com", **ISOLATED)

CHANGELOG = """# Changelog

## [{v}] - 2026-11-07

### What changes for you

- something

### Added

- a thing
{older}"""


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


class Repos:
    """A work repository that publishes releases to a bare origin, and a clone of it as the devkit checkout."""

    def __init__(self, root: Path):
        self.work, self.origin, self.home = root / "work", root / "origin.git", root / "devkit"
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        self.log = ""

    def release(self, version: str, tag=True, extra=""):
        older = self.log
        self.log = f"\n## [{version}] - 2026-11-07\n\n### What changes for you\n\n- x\n\n### Added\n\n- y\n" \
                   f"{extra}" + older
        (self.work / "CHANGELOG.md").write_text("# Changelog\n" + self.log)
        (self.work / "VERSION").write_text(version + "\n")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-q", "-m", version)
        if tag:
            git(self.work, "tag", "v" + version)
        if self.origin.exists():
            git(self.work, "push", "-q", "origin", "main", "--tags")

    def publish(self):
        git(self.work.parent, "clone", "-q", "--bare", str(self.work), str(self.origin))
        git(self.work, "remote", "add", "origin", str(self.origin))

    def clone(self, *args):
        git(self.work.parent, "clone", "-q", *args, str(self.origin), str(self.home))

    def snapshot(self) -> tuple:
        return (git(self.home, "rev-parse", "HEAD"), git(self.home, "status", "--porcelain"),
                sorted(p.name for p in self.home.iterdir()))


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(core.GIT_ENV, ISOLATED)
        env.start()
        self.addCleanup(env.stop)
        self.r = Repos(Path(self.tmp.name))
        self.r.release("26.11.3")
        self.r.publish()

    def test_a_newer_release_is_named_with_its_headings_and_nothing_changes(self):
        self.r.clone("--branch", "v26.11.3")
        self.r.release("26.11.4", extra="\n### Known limits\n\n- z\n")
        before = self.r.snapshot()
        res = updatecheck.check(self.r.home, installed="26.11.3")
        self.assertEqual((res["newest"], res["behind"]), ("v26.11.4", 1))
        self.assertEqual(res["releases"], [{"version": "26.11.4",
                                            "headings": ["What changes for you", "Added", "Known limits"]}])
        text = "\n".join(updatecheck.render(res))
        self.assertIn("1 release behind (checkout and pinned copy)", text)
        self.assertIn("26.11.4: What changes for you · Added · Known limits", text)
        self.assertIn("run in a terminal: dev update", text)
        self.assertEqual(self.r.snapshot(), before)  # HEAD, the working tree and its files are as they were

    def test_two_releases_behind_lists_both_newest_first(self):
        self.r.clone("--branch", "v26.11.3")
        self.r.release("26.11.4")
        self.r.release("26.11.10")
        res = updatecheck.check(self.r.home, installed="26.11.3")
        self.assertEqual((res["newest"], res["behind"]), ("v26.11.10", 2))
        self.assertEqual([x["version"] for x in res["releases"]], ["26.11.10", "26.11.4"])

    def test_up_to_date_says_so(self):
        self.r.clone("--branch", "v26.11.3")
        res = updatecheck.check(self.r.home, installed="26.11.3")
        self.assertEqual((res["behind"], res["releases"]), (0, []))
        self.assertIn("up to date", "\n".join(updatecheck.render(res)))

    def test_a_branch_checkout_reports_the_commits_it_is_behind(self):
        self.r.clone()
        self.r.release("26.11.4-dev", tag=False)
        self.r.release("26.11.5-dev", tag=False)
        res = updatecheck.check(self.r.home, installed="26.11.3")
        self.assertEqual((res["checkout"]["branch"], res["checkout"]["behind_commits"]), ("main", 2))
        self.assertIn("main, 2 commits behind origin", "\n".join(updatecheck.render(res)))
        self.assertEqual(git(self.r.home, "rev-list", "--count", "HEAD"), "1")  # not pulled

    def test_a_pinned_copy_older_than_the_checkout_says_to_run_dev_install(self):
        self.r.clone("--branch", "v26.11.3")
        res = updatecheck.check(self.r.home, installed="26.11.2")
        self.assertTrue(res["pin_behind_checkout"])
        self.assertIn("the pinned copy (26.11.2) is older than the checkout (26.11.3): run dev install",
                      "\n".join(updatecheck.render(res)))

    def test_offline_it_shows_the_last_result_with_its_date_and_never_up_to_date(self):
        self.r.clone("--branch", "v26.11.3")
        cache = Path(self.tmp.name) / "update-check.json"
        updatecheck.record({"checked": 1762500000, "installed": "26.11.3", "newest": "v26.11.4", "behind": 1,
                            "releases": [], "checkout": {}}, cache)
        shutil.rmtree(self.r.origin)
        res = updatecheck.check(self.r.home, installed="26.11.3", cache=cache)
        self.assertTrue(res["error"])
        text = "\n".join(updatecheck.render(res))
        self.assertIn("could not be checked", text)
        self.assertIn("last check", text)
        self.assertIn("v26.11.4", text)
        self.assertNotIn("up to date", text)

    def test_offline_without_a_cache_says_it_could_not_be_checked(self):
        self.r.clone("--branch", "v26.11.3")
        shutil.rmtree(self.r.origin)
        res = updatecheck.check(self.r.home, installed="26.11.3", cache=Path(self.tmp.name) / "none.json")
        text = "\n".join(updatecheck.render(res))
        self.assertIn("could not be checked", text)
        self.assertNotIn("up to date", text)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.f = Path(self.tmp.name) / "update-check.json"

    def test_the_session_line_comes_once_a_day_while_a_release_is_pending(self):
        updatecheck.record({"checked": 1, "installed": "26.11.3", "newest": "v26.11.4", "behind": 1}, self.f)
        line = updatecheck.session_line(self.f, today="2026-11-07")
        self.assertIn("devkit v26.11.4 is out", line)
        self.assertIn("dev update", line)
        self.assertIsNone(updatecheck.session_line(self.f, today="2026-11-07"))
        self.assertIn("v26.11.4", updatecheck.session_line(self.f, today="2026-11-08"))

    def test_no_session_line_when_up_to_date_or_never_checked(self):
        self.assertIsNone(updatecheck.session_line(self.f, today="2026-11-07"))
        updatecheck.record({"checked": 1, "installed": "26.11.4", "newest": "v26.11.4", "behind": 0}, self.f)
        self.assertIsNone(updatecheck.session_line(self.f, today="2026-11-07"))

    def test_the_status_line_shows_while_the_release_is_pending(self):
        updatecheck.record({"checked": 1, "installed": "26.11.3", "newest": "v26.11.4", "behind": 1}, self.f)
        self.assertIn("v26.11.4", updatecheck.status_line(self.f, installed="26.11.3"))
        self.assertIsNone(updatecheck.status_line(self.f, installed="26.11.4"))  # installed since the check

    def test_the_daily_job_runs_the_quiet_recording_check(self):
        j = jobs.jobs()["update-check"]
        self.assertEqual((j["cmd"], j["at"]), (["update", "--check", "--quiet", "--record"], "09:15"))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(core.GIT_ENV, ISOLATED)
        env.start()
        self.addCleanup(env.stop)
        self.r = Repos(Path(self.tmp.name))
        self.r.release("26.11.3")
        self.r.publish()
        self.r.clone("--branch", "v26.11.3")
        self.r.release("26.11.4")

    def run_cli(self, *args):
        out = io.StringIO()
        with mock.patch.object(core, "source_home", return_value=self.r.home), \
                mock.patch.object(updatecheck, "installed_version", return_value="26.11.3"), \
                mock.patch.object(updatecheck, "CACHE", Path(self.tmp.name) / "uc.json"), \
                mock.patch.object(os, "execv", side_effect=AssertionError("dev update --check ran dev install")), \
                contextlib.redirect_stdout(out):
            rc = cli.main(["update", *args])
        return rc, out.getvalue()

    def test_dev_update_check_prints_the_report_and_never_installs(self):
        before = self.r.snapshot()
        rc, out = self.run_cli("--check")
        self.assertEqual(rc, 0)
        self.assertIn("1 release behind", out)
        self.assertEqual(self.r.snapshot(), before)

    def test_quiet_record_prints_nothing_and_writes_the_cache(self):
        rc, out = self.run_cli("--check", "--quiet", "--record")
        self.assertEqual((rc, out), (0, ""))
        doc = json.loads((Path(self.tmp.name) / "uc.json").read_text())
        self.assertEqual((doc["newest"], doc["installed"], doc["behind"]), ("v26.11.4", "26.11.3", 1))


class SessionStartTests(HookCase):
    """The session start reads only the recorded result and tells the first session of the day."""

    def test_the_first_session_of_the_day_gets_the_line_and_the_next_does_not(self):
        cache = self.root / "update-check.json"
        updatecheck.record({"checked": 1, "installed": "26.11.3", "newest": "v26.11.4", "behind": 1}, cache)
        outs = []
        with mock.patch.object(updatecheck, "CACHE", cache), \
                mock.patch.object(updatecheck, "installed_version", return_value="26.11.3"), \
                mock.patch.object(hooks, "context_for", lambda cwd, agent: "## devkit · workspace context"), \
                mock.patch.object(hooks, "_spawn", lambda *a, **k: None), \
                mock.patch("devkit.work.request_sync", lambda *a, **k: None), \
                mock.patch.object(core, "git", side_effect=AssertionError("the session start ran git")):
            for sid in ("s1", "s2"):
                outs.append(self.hook("session-start", {"session_id": sid, "hook_event_name": "SessionStart",
                                                        "source": "startup", "cwd": str(self.root)}))
        self.assertIn("devkit v26.11.4 is out", outs[0])
        self.assertNotIn("v26.11.4", outs[1])


class StatusTests(unittest.TestCase):
    def test_dev_status_needs_you_names_a_pending_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "update-check.json"
            updatecheck.record({"checked": 1, "installed": "26.11.3", "newest": "v26.11.4", "behind": 1}, cache)
            with mock.patch.object(updatecheck, "CACHE", cache), \
                    mock.patch.object(updatecheck, "installed_version", return_value="26.11.3"):
                needs = cli.devkit_needs()
        self.assertEqual([(n["kind"], n["command"]) for n in needs], [("devkit", "dev update --check")])
        self.assertIn("v26.11.4", needs[0]["text"])


class GuardTests(unittest.TestCase):
    """`dev update` stays the developer's (K1); its read-only `--check` is open to agents (spec guard contract)."""

    def verdict(self, cmd):
        v = guard.check("Bash", {"command": cmd}, str(core.DEV), "claude")
        return v[0] if v else "allow"

    def test_agents_may_run_the_read_only_check(self):
        for cmd in ("dev update --check", "dev update --check --quiet --record", "bin/dev update --check --help"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.verdict(cmd), "allow")

    def test_the_update_itself_and_look_alikes_stay_refused(self):
        for cmd in ("dev update", "dev update --record", "dev update --chec", "dev update --check --unknown",
                    "dev update --check=1"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.verdict(cmd), "deny")


if __name__ == "__main__":
    unittest.main()
