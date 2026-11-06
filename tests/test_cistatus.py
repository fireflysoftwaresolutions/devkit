# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""CI/CD run status across providers, from canned API payloads. Run: python3 -m unittest -v"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from devkit import cistatus as cs  # noqa: E402

NOW = cs._ts("2026-11-06T05:20:00Z")


class ProviderTests(unittest.TestCase):
    def test_provider_from_remote(self):
        self.assertEqual(cs.provider_for("git@github.com:acme/api.git"), ("github", "github.com", "acme/api"))
        self.assertEqual(cs.provider_for("https://gitlab.com/g/sub/repo.git"), ("gitlab", "gitlab.com", "g/sub/repo"))
        self.assertEqual(cs.provider_for("git@bitbucket.org:ws/repo.git"), ("bitbucket", "bitbucket.org", "ws/repo"))
        self.assertEqual(cs.provider_for("https://org@dev.azure.com/org/Proj/_git/repo")[0], "azure")
        self.assertIsNone(cs.provider_for("https://example.com/x/y.git"))

    def test_github_run_and_jobs(self):
        run = cs.github_run({"databaseId": 7, "number": 103, "status": "in_progress", "conclusion": "",
                             "workflowName": "CI", "headBranch": "main", "headSha": "88fb104abc", "event": "push",
                             "startedAt": "2026-11-06T05:12:00Z", "createdAt": "2026-11-06T05:11:50Z",
                             "updatedAt": "2026-11-06T05:19:00Z", "url": "u"}, NOW)
        self.assertEqual((run["status"], run["elapsed"], run["finished"]), ("running", 480, 0))
        cs._with_eta(run, 1500)
        self.assertEqual(run["eta"], 1020)
        job = cs.github_job({"name": "pytest 3/10", "status": "in_progress", "conclusion": None,
                             "startedAt": "2026-11-06T05:12:20Z", "completedAt": "0001-01-01T00:00:00Z",
                             "steps": [{"name": "Checkout", "status": "completed"}, {"name": "Check", "status": "in_progress"},
                                       {"name": "Upload", "status": "queued"}]}, NOW)
        self.assertEqual((job["status"], job["elapsed"], job["steps_done"], job["steps_total"], job["current"]),
                         ("running", 460, 1, 3, "Check"))  # the 0001-01-01 placeholder is not an end time
        self.assertIn("step 2/3 “Check”", cs.describe_job(job))
        self.assertIn("ETA ~17m00s (usually 25m00s)", cs.describe(run, NOW))
        done = cs.github_run({"databaseId": 6, "status": "completed", "conclusion": "failure", "workflowName": "CI",
                              "startedAt": "2026-11-06T04:00:00Z", "updatedAt": "2026-11-06T04:30:00Z"}, NOW)
        self.assertEqual(done["status"], "failed")

    def test_gitlab_pipeline_and_stages(self):
        run = cs.gitlab_run({"id": 9, "iid": 41, "status": "running", "ref": "main", "sha": "abc1234",
                             "created_at": "2026-11-06T05:10:00Z", "started_at": "2026-11-06T05:10:30Z",
                             "web_url": "w"}, NOW)
        self.assertEqual((run["status"], run["number"], run["elapsed"]), ("running", 41, 570))
        stages = cs.gitlab_jobs([
            {"name": "lint", "stage": "build", "status": "success", "started_at": "2026-11-06T05:10:40Z",
             "finished_at": "2026-11-06T05:11:40Z"},
            {"name": "unit", "stage": "test", "status": "running", "started_at": "2026-11-06T05:12:00Z"},
            {"name": "e2e", "stage": "test", "status": "pending"}], NOW)
        test = next(s for s in stages if s["name"] == "test")
        self.assertEqual((test["status"], test["steps_done"], test["steps_total"], test["current"]),
                         ("running", 0, 2, "unit"))
        self.assertEqual(next(s for s in stages if s["name"] == "build")["status"], "success")

    def test_bitbucket_states(self):
        running = {"state": {"name": "IN_PROGRESS", "stage": {"name": "RUNNING"}}}
        self.assertEqual(cs._bb_status(running["state"]), "running")
        self.assertEqual(cs._bb_status({"name": "COMPLETED", "result": {"name": "SUCCESSFUL"}}), "success")
        self.assertEqual(cs._bb_status({"name": "COMPLETED", "result": {"name": "STOPPED"}}), "cancelled")
        self.assertEqual(cs._bb_status({"name": "PENDING"}), "queued")
        run = cs.bitbucket_run({"uuid": "{u}", "build_number": 12, "state": running["state"],
                                "created_on": "2026-11-06T05:15:00Z", "target": {"ref_name": "main",
                                                                                "commit": {"hash": "deadbeef99"}}},
                               NOW, "ws", "repo")
        self.assertEqual((run["status"], run["branch"], run["sha"]), ("running", "main", "deadbee"))
        self.assertTrue(run["url"].endswith("/ws/repo/pipelines/results/12"))

    def test_azure_build_and_timeline(self):
        run = cs.azure_run({"id": 5, "buildNumber": "20261106.3", "status": "inProgress", "result": None,
                            "definition": {"id": 2, "name": "api-ci"}, "sourceBranch": "refs/heads/main",
                            "startTime": "2026-11-06T05:05:00Z"}, NOW)
        self.assertEqual((run["status"], run["branch"], run["name"]), ("running", "main", "api-ci"))
        jobs = cs.azure_jobs([
            {"id": "j1", "type": "Job", "name": "Build", "state": "inProgress", "startTime": "2026-11-06T05:06:00Z"},
            {"id": "t1", "parentId": "j1", "type": "Task", "name": "Restore", "state": "completed"},
            {"id": "t2", "parentId": "j1", "type": "Task", "name": "Test", "state": "inProgress"}], NOW)
        self.assertEqual((jobs[0]["steps_done"], jobs[0]["steps_total"], jobs[0]["current"]), (1, 2, "Test"))
        self.assertEqual(cs._az_status("completed", "succeeded"), "success")

    def test_job_summary_puts_failures_first(self):
        jobs = [{"name": f"j{i}", "status": s, "elapsed": i} for i, s in
                enumerate(["success", "running", "failed", "running", "queued", "running"])]
        self.assertEqual(cs.job_summary(jobs), "2/6 jobs done · 3 running · 1 queued · 1 failed")
        shown, more = cs.jobs_to_show(jobs, 2)
        self.assertEqual([j["name"] for j in shown], ["j2", "j5", "j4"])
        self.assertEqual(more, 2)




class HttpSchemeTests(unittest.TestCase):
    def test_only_https_urls_are_opened(self):
        from devkit import cistatus
        for url in ("file:///etc/passwd", "http://gitlab.example.com/api/v4/projects", "ftp://x"):
            with self.assertRaises(ValueError):
                cistatus._http(url, {})


if __name__ == "__main__":
    unittest.main()
