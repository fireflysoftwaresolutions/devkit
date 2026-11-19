# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's own CI workflow (.github/workflows/ci.yml), run the way `dev ci --run` runs it.

Run: python3 -m unittest tests.test_workflow_ci -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import ci, gitx  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Ana Dev",
           "GIT_AUTHOR_EMAIL": "ana@example.com", "GIT_COMMITTER_NAME": "Ana Dev",
           "GIT_COMMITTER_EMAIL": "ana@example.com"}

# `docker run` as its container sees the bind mounts: it finds the repository of its working folder the way git does
# (a .git folder, or a .git file that names the git directory, and that directory's commondir), only through the
# folders mounted into it, records the mounts, and fails as git does when the repository is out of its reach.
FAKE_DOCKER = r'''#!%(python)s
import json, os, sys
args, mounts, workdir, env, i = sys.argv[2:], [], "/", {}, 0
while i < len(args):
    if args[i] == "-v":
        src, dst, *opts = args[i + 1].split(":")
        mounts.append({"src": src, "dst": dst, "ro": "ro" in opts})
        i += 2
    elif args[i] in ("-w", "-e"):
        if args[i] == "-w":
            workdir = args[i + 1]
        else:
            key, _, value = args[i + 1].partition("=")
            env[key] = value
        i += 2
    elif args[i].startswith("-"):
        i += 1
    else:
        break
with open(os.environ["FAKE_DOCKER_RECORD"], "w") as fh:
    json.dump(mounts, fh)

def host(path):
    """The host path a container path shows, or None when no mount covers it."""
    best = None
    for m in mounts:
        if path == m["dst"] or path.startswith(m["dst"].rstrip("/") + "/"):
            best = m if best is None or len(m["dst"]) > len(best["dst"]) else best
    return None if best is None else best["src"] + path[len(best["dst"]):]

dotgit = host(workdir + "/.git")
gitdir = workdir + "/.git"
if env.get("GIT_DIR"):
    gitdir = env["GIT_DIR"]  # git takes the repository GIT_DIR names, and GIT_WORK_TREE as its files
elif dotgit and os.path.isfile(dotgit):
    gitdir = open(dotgit).read().split("gitdir:", 1)[1].strip()
seen = host(gitdir)
if not seen or not os.path.isdir(seen):
    sys.exit(print("fatal: not a git repository: " + gitdir, file=sys.stderr) or 128)
common = os.path.join(seen, "commondir")
if os.path.isfile(common):
    where = os.path.normpath(os.path.join(gitdir, open(common).read().strip()))
    if not host(where) or not os.path.isdir(host(where)):
        sys.exit(print("fatal: not a git repository: " + where, file=sys.stderr) or 128)
print("git repository found")
'''


def semgrep_step() -> dict:
    """The Semgrep step of the security job, as `dev ci --run` plans it."""
    job = next(j for j in gitx.plan(str(ROOT)) if j["id"] == "security" and j["file"] == "ci.yml")
    return next(st for st in job["steps"] if st["name"] == "Code (semgrep)")


class SemgrepStepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="devkit-workflow-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER % {"python": sys.executable})
        docker.chmod(0o755)
        self.record = self.root / "mounts.json"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            self.git(self.repo, *args)

    def git(self, where, *args):
        subprocess.run(["git", *args], cwd=where, env=dict(os.environ, **GIT_ENV), check=True,
                       capture_output=True)

    def run_step(self, where: Path):
        evidence = self.root / "evidence"
        evidence.mkdir(exist_ok=True)
        env = dict(os.environ, **GIT_ENV, PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                   SECURITY_EVIDENCE=str(evidence), FAKE_DOCKER_RECORD=str(self.record))
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", semgrep_step()["run"]],
                              cwd=where, env=env, capture_output=True, text=True, timeout=60)

    # A managed worktree's .git is a file naming .git/worktrees/<name> in the main checkout. The step mounted only
    # the worktree, so git in the container found no repository, the corpus could not list its sources, and
    # `dev ci --run` (so `dev push`) failed in every worktree.
    def test_the_scan_reaches_the_git_directory_of_a_worktree_and_of_a_plain_checkout(self):
        wt = self.root / "wt"
        self.git(self.repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
        for where in (wt, self.repo):
            with self.subTest(where=where.name):
                got = self.run_step(where)
                self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
                self.assertIn("git repository found", got.stdout)

    def test_only_the_evidence_folder_is_writable_in_the_container(self):
        wt = self.root / "wt"
        self.git(self.repo, "worktree", "add", "-q", "-b", "feat/x", str(wt))
        self.assertEqual(self.run_step(wt).returncode, 0)
        writable = [m["dst"] for m in json.loads(self.record.read_text()) if not m["ro"]]
        self.assertEqual(writable, ["/evidence"])


class TestJobTests(unittest.TestCase):
    # The installer's upgrade tests (UpgradeFrom26118Tests, RealOlderReleaseTests) run against this checkout's own
    # release tags. actions/checkout fetches one commit and no tag by default, so CI skipped them on every run.
    def test_the_test_job_checks_out_the_history_and_tags_its_upgrade_tests_need(self):
        wf = ci.parse_workflow((ROOT / ".github/workflows/ci.yml").read_text())
        job = next(j for j in wf["jobs"] if j["id"] == "test")
        checkout = next(st for st in job["steps"] if (st.get("uses") or "").startswith("actions/checkout@"))
        options = dict(o.split(":", 1) for o in checkout["with"])
        self.assertEqual(options.get("fetch-depth", "").strip(), "0")  # every commit and every tag
        self.assertEqual(options.get("persist-credentials", "").strip(), "false")

    # The test job ran the whole suite on one machine per Python version: about 46 minutes, under a 90-minute
    # timeout. It runs in parts: each takes every Nth test file by name, the parts together take each file once, and
    # each ends with the real-state check of its own run. Where the matrix is not expanded (dev ci --run gives the
    # step no part), the step runs the whole suite, never a part of it.
    def test_the_suite_runs_in_parts_that_take_each_test_file_once(self):
        job = next(j for j in ci.parse_workflow((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
                   if j["id"] == "test")
        shards = next((m.split(":", 1)[1].strip() for m in job["matrix"] if m.startswith("shard:")), "")
        parts = [int(x) for x in shards.strip("[]").split(",") if x.strip()]
        self.assertGreater(len(parts), 1, job["matrix"])
        planned = next(j for j in gitx.plan(str(ROOT)) if j["id"] == "test" and j["file"] == "ci.yml")
        step = next(st for st in planned["steps"] if st["name"] == "Unit tests")  # as dev ci --run gives it
        self.assertEqual(step["env"].get("PARTS"), str(len(parts)))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = [f"test_{c}{i}" for c in "abcdefg" for i in range(3)] + ["test_zz_real_state_untouched"]
            (root / "tests").mkdir()
            for name in names + ["__init__", "_hermetic"]:
                (root / "tests" / f"{name}.py").write_text("")
            (root / "bin").mkdir()
            log = root / "calls.jsonl"
            fake = root / "bin" / "python"
            fake.write_text(f"#!{sys.executable}\nimport json, sys\n"
                            f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n")
            fake.chmod(0o755)

            def run_part(part):
                # the step's environment as dev ci --run gives it, from a shell that exports a SHARD of its own; the
                # part comes from the matrix only, which devkit does not expand (this suite may run inside a part)
                shell = {k: v for k, v in os.environ.items() if k != "DEVKIT_TEST_PART"}
                with mock.patch.dict(os.environ, dict(shell, SHARD="1"), clear=True):
                    env = gitx._local_env(step["env"])
                env["PATH"] = f"{root / 'bin'}{os.pathsep}{env['PATH']}"
                if part is not None:
                    env["DEVKIT_TEST_PART"] = str(part)
                got = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
                                     cwd=root, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
                return json.loads(log.read_text().splitlines()[-1])

            runs = [run_part(k) for k in parts]
            every = sorted(f"tests.{n}" for n in names)
            for argv in runs:
                self.assertEqual(argv[:2], ["-m", "unittest"])
                self.assertEqual(argv[-1], "tests.test_zz_real_state_untouched")
            taken = [argv[2:-1] for argv in runs]
            self.assertEqual(sorted(m for t in taken for m in t), [m for m in every if "test_zz" not in m])
            self.assertLessEqual(max(map(len, taken)) - min(map(len, taken)), 1)
            self.assertEqual(run_part(None), ["-m", "unittest", "discover", "-s", "tests", "-t", "."])  # all of it


if __name__ == "__main__":
    unittest.main()
