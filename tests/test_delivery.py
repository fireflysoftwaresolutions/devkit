# Copyright (c) 2026 Firefly Software Solutions Inc. All rights reserved.
"""Delivery policy: detection, recommendation, gaps, recording and the tag rule. Run: python3 -m unittest -v"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import core, delivery, guard, work  # noqa: E402

IDENT = ["-c", "user.email=t@example.com", "-c", "user.name=t"]
A = {"user": "ana", "host": "mbp1", "agent": "claude", "session": "s1"}


def repo_with(files: dict, tags=()):
    d = tempfile.mkdtemp()
    core.sh(["git", "init", "-q", d])
    for name, text in files.items():
        p = Path(d) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    core.sh(["git", *IDENT, "-C", d, "add", "-A"])
    core.sh(["git", *IDENT, "-C", d, "commit", "-q", "-m", "init"])
    for t in tags:
        core.sh(["git", *IDENT, "-C", d, "tag", t])
    return Path(d)


class DetectTests(unittest.TestCase):
    def test_calver_application(self):
        r = repo_with({"VERSION": "26.11.0\n", "CHANGELOG.md": "# Changelog\nKeep a Changelog\n",
                       ".github/workflows/ci.yml": "on: [push]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n"
                                                   "      - run: make test\n"}, tags=["v26.10.0", "v26.11.0"])
        d = delivery.detect(r)
        self.assertEqual((d["versioning"], d["tag_format"], d["latest_tag"]), ("calver", "vYY.MM.PATCH", "v26.11.0"))
        self.assertEqual((d["version_source"], d["changelog"], d["ci"]), ("VERSION", "keep-a-changelog", ["github-actions"]))
        self.assertEqual(d["test_gate"], ["make test"])
        rec = delivery.recommend(d)
        self.assertEqual(rec["versioning"], "calver")
        self.assertIn("dev calver --next", rec["bump"])
        self.assertEqual([g[1] for g in delivery.gaps(d, rec, True)], [])

    def test_library_without_scheme_gets_semver_and_gaps(self):
        pkg = json.dumps({"name": "@acme/ui", "version": "dynamic", "publishConfig": {"access": "public"}})
        r = repo_with({"package.json": pkg, "pnpm-lock.yaml": "lockfileVersion: 9\n"})
        d = delivery.detect(r)
        self.assertEqual((d["kind"], d["publish"]), ("library", ["npm"]))
        rec = delivery.recommend(d)
        self.assertEqual((rec["versioning"], rec["tag_format"]), ("semver", "vMAJOR.MINOR.PATCH"))
        self.assertEqual(rec["test_gate"], ["pnpm install --frozen-lockfile", "pnpm test"])
        gaps = " | ".join(g[1] for g in delivery.gaps(d, rec, False))
        for needle in ("no delivery policy recorded", "no CI pipeline", "no version scheme", "no CHANGELOG",
                       "no release workflow"):
            self.assertIn(needle, gaps)

    def test_semver_tags(self):
        r = repo_with({"README.md": "x"}, tags=["v1.2.0", "v1.3.0-rc.1"])
        self.assertEqual(delivery.detect(r)["versioning"], "semver")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (delivery.POLICIES, work.HANDOFF)
        delivery.POLICIES = root / "policies"
        work.HANDOFF = root

    def tearDown(self):
        delivery.POLICIES, work.HANDOFF = self.saved
        self.tmp.cleanup()

    def test_record_versions_and_tag_rule(self):
        with self.assertRaises(ValueError):
            delivery.record("acme/api", {"versioning": "calver"}, "", a=A)
        p = delivery.record("acme/api", {"versioning": "calver", "tag_format": "vYY.MM.PATCH",
                                         "test_gate": ["./mvnw verify"], "changelog": "keep-a-changelog"},
                            "first policy", a=A)
        self.assertEqual(p["changelog"], "keep-a-changelog")  # the policy field survives the history
        self.assertEqual(p["version"], 1)
        p = delivery.record("acme/api", {**p, "test_gate": ["./mvnw -B verify"]}, "batch mode", a=A)
        self.assertEqual(p["version"], 2)
        self.assertIn("test_gate — why: batch mode", p["history"][0])
        self.assertIsNone(delivery.tag_violation("acme/api", "v26.11.3"))
        self.assertIn("CalVer", delivery.tag_violation("acme/api", "v1.4.0"))
        self.assertIn("CalVer", delivery.tag_violation("acme/api", "v26.13.0"))  # no 13th month
        self.assertIn("test gate: ./mvnw -B verify", delivery.summary_line("acme/api"))
        self.assertIsNone(delivery.tag_violation("acme/other", "anything"))  # no policy, no rule

    def test_job_timeout_is_a_policy_field(self):
        p = delivery.set_field({}, "job_timeout_minutes", "45")
        self.assertEqual(p["job_timeout_minutes"], 45)
        for bad in ("0", "abc", "999", "4"):
            with self.assertRaises(ValueError):
                delivery.set_field({}, "job_timeout_minutes", bad)
        with self.assertRaises(ValueError):
            delivery.set_field({}, "timeout", "45")  # unknown field
        rec = delivery.record("acme/ci", {"versioning": "calver", **p}, "slow integration tests", a=A)
        self.assertEqual(rec["job_timeout_minutes"], 45)  # recorded, not dropped
        from devkit import pipelines
        self.assertEqual(pipelines._default_timeout(rec), 45)
        self.assertEqual(delivery.set_field({}, "test_gate", "a; b")["test_gate"], ["a", "b"])

    def test_guard_refuses_off_policy_tags(self):
        delivery.record("acme/one", {"versioning": "semver", "tag_format": "vMAJOR.MINOR.PATCH"}, "library", a=A)
        r = repo_with({"a": "1"})
        core.sh(["git", "-C", str(r), "remote", "add", "origin", "https://github.com/acme/one.git"])
        saved = guard.is_temp
        guard.is_temp = lambda p: False
        try:
            deny = guard.check("Bash", {"command": "git tag release-2026-11"}, str(r), "claude")
            self.assertEqual(deny[0], "deny")
            self.assertIsNone(guard.check("Bash", {"command": "git tag v2.1.0"}, str(r), "claude"))
            self.assertIsNone(guard.check("Bash", {"command": "git tag -d release-2026-11"}, str(r), "claude"))
            self.assertIsNone(guard.check("Bash", {"command": "DEVKIT_TAG_ANY=1 git tag nightly"}, str(r), "claude"))
            self.assertEqual(guard.check("Write", {"file_path": str(core.HANDOFF / "policies" / "acme" / "one.json")},
                                         str(r), "claude")[0], "deny")
        finally:
            guard.is_temp = saved


class CliFixTests(unittest.TestCase):
    def test_actor_session_without_agent_prefix(self):
        import os
        old = os.environ.get("DEVKIT_SESSION")
        os.environ["DEVKIT_SESSION"] = "claude:5f2c9abc"
        try:
            a = work.actor("claude")
            self.assertEqual(a["session"], "5f2c9abc")
            self.assertTrue(work.actor_str(a).endswith("(claude:5f2c9abc)"))
        finally:
            if old is None:
                os.environ.pop("DEVKIT_SESSION", None)
            else:
                os.environ["DEVKIT_SESSION"] = old

    def test_delegate_message_reads_template_sections(self):
        from devkit import cli
        it = {"meta": {"id": "T-1", "title": "Export invoices", "due": "", "links": []}, "scope": "acme/api",
              "body": work.task_body("feature", "Finance copies invoices by hand.",
                                     ["export works for October", "tests pass"])}
        msg = cli.delegate_message(it, "maria", "", "acme/api")
        self.assertIn("Why it matters: Finance copies invoices by hand.", msg)
        self.assertIn("Done means: export works for October; tests pass", msg)
        self.assertNotIn("##", msg)
        self.assertNotIn("_What is included._", msg)

    def test_task_verbs(self):
        from devkit import cli
        ap = cli.build_parser()
        self.assertEqual(ap.parse_args(["task", "unblock", "3"]).task_cmd, "unblock")
        a = ap.parse_args(["task", "link", "3", "--to", "lending", "--why", "part of the pilot"])
        self.assertEqual((a.task_cmd, a.to), ("link", "lending"))
        self.assertIn("block", work.NEEDS_WHY)
        self.assertEqual(ap.parse_args(["delivery", "apply", "--why", "x", "--set", "versioning=semver"]).set,
                         ["versioning=semver"])


if __name__ == "__main__":
    unittest.main()
