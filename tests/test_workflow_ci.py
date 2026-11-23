# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""devkit's own CI workflow (.github/workflows/ci.yml), run the way `dev ci --run` runs it.

Run: python3 -m unittest tests.test_workflow_ci -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from devkit import ci, gitx, testx  # noqa: E402

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


def unit_step() -> dict:
    """The Unit tests step of the test job, as `dev ci --run` plans it."""
    job = next(j for j in gitx.plan(str(ROOT)) if j["id"] == "test" and j["file"] == "ci.yml")
    return next(st for st in job["steps"] if st["name"] == "Unit tests")


ONE_TEST = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        pass\n"

# A stand-in for tests/_hermetic.py: like it, activation drops devkit's own DEVKIT_* switches from the run
HERMETIC = ("import os\n\n\ndef activate():\n"
            "    for name in [n for n in os.environ if n.startswith('DEVKIT_')]:\n        del os.environ[name]\n")

# A script that prints what discovery of tests/ (-t .) loads, as `python -m unittest discover -s tests -t .` loads
# it: the module of each test in order (a module that cannot be imported stands as one failing test named after it),
# the errors of those modules, whether the package defines load_tests, and, with the argument `nested`, what the
# same discovery loads when a process of the run starts it, with the run's own environment
LOADED = """import json, os, subprocess, sys, unittest
def tests(suite):
    for t in suite:
        yield from tests(t) if isinstance(t, unittest.TestSuite) else [t]
loader = unittest.TestLoader()
found = list(tests(loader.discover("tests", top_level_dir=".")))
failed = "unittest.loader._FailedTest."
modules, errors = [], list(loader.errors)
for t in found:
    if isinstance(t, unittest.FunctionTestCase) or t.id().startswith(failed):
        modules.append(t.id()[len(failed):] if t.id().startswith(failed) else t.id())
        result = unittest.TestResult()
        t.run(result)
        errors += [text for _, text in result.errors]
    else:
        modules.append(t.id().rsplit(".", 2)[0])
import tests as package
nested = None
if sys.argv[1:] == ["nested"]:
    child = subprocess.run([sys.executable, os.path.abspath(__file__)], capture_output=True, text=True, timeout=60)
    nested = json.loads(child.stdout)["modules"] if child.returncode == 0 else child.stderr
print(json.dumps({"modules": modules, "errors": "\\\\n".join(errors), "load_tests": hasattr(package, "load_tests"),
                  "nested": nested}))
"""


def matrix_parts() -> list:
    """The parts of the test job's matrix (shard: [1, 2])."""
    job = next(j for j in ci.parse_workflow((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
               if j["id"] == "test")
    shards = next((m.split(":", 1)[1].strip() for m in job["matrix"] if m.startswith("shard:")), "")
    return [int(x) for x in shards.strip("[]").split(",") if x.strip()]


def suite_fixture(root: Path):
    """A copy of tests/__init__.py with the HERMETIC stand-in and 22 test modules (one fails to import), a helper and
    a file that is no module name -> (root, the modules discovery loads, in its order)."""
    (root / "tests").mkdir()
    shutil.copyfile(ROOT / "tests" / "__init__.py", root / "tests" / "__init__.py")
    (root / "tests" / "_hermetic.py").write_text(HERMETIC)
    names = [f"test_{c}{i}" for c in "abcdefg" for i in range(3)] + ["test_zz_real_state_untouched"]
    for name in names:
        (root / "tests" / f"{name}.py").write_text(ONE_TEST)
    (root / "tests" / "dashfixture.py").write_text("raise SystemExit('a helper, not a test module')\n")
    (root / "tests" / "test-not-a-module.py").write_text("raise SystemExit('not a module name')\n")
    (root / "tests" / "test_broken.py").write_text("raise RuntimeError('broken at import')\n")
    (root / "loaded.py").write_text(LOADED)
    return root, sorted(f"tests.{n}" for n in names + ["test_broken"])


def discovered(root: Path, part, parts: int, nested=False) -> dict:
    """What discovery loads in a suite_fixture, run with part `part` of `parts` (None: no part), as LOADED prints it."""
    env = {k: v for k, v in os.environ.items() if k != "DEVKIT_TEST_PART"}  # this suite may run in a part
    env["PARTS"] = str(parts)
    if part is not None:
        env["DEVKIT_TEST_PART"] = str(part)
    got = subprocess.run([sys.executable, str(root / "loaded.py")] + (["nested"] if nested else []), cwd=root,
                         env=env, capture_output=True, text=True, timeout=120)
    if got.returncode:
        raise AssertionError(got.stdout + got.stderr)
    return json.loads(got.stdout)


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

    # devkit's own unit step was a shell script that chose the part of the suite to run, and dev ci --run recognizes a
    # test runner only in a step that is one plain call (testx.recognize), so it never observed the suite: the step
    # left no completion receipt and no failed tests, and --resume reran the whole 65-minute suite after one failure.
    # The step is the local gate's own command, one unittest run dev ci --run recognizes; the part comes from the
    # matrix through DEVKIT_TEST_PART, which dev ci --run does not give, so here the whole suite runs.
    def test_the_unit_step_is_the_local_gate_dev_ci_run_recognizes_as_one_unittest_run(self):
        step = unit_step()
        self.assertEqual(step["run"].strip(), "python -m unittest discover -s tests -t .")
        runner = testx.recognize(step["run"], step["cwd"], str(ROOT))
        self.assertIsNotNone(runner, step["run"])
        self.assertEqual((runner["name"], runner["start"], runner["package"], runner["failfast"]),
                         ("unittest", "tests", True, False))
        self.assertEqual(step["env"].get("DEVKIT_TEST_PART"), "${{ matrix.shard }}")
        shell = {k: v for k, v in os.environ.items() if k != "DEVKIT_TEST_PART"}  # this suite may run in a part
        with mock.patch.dict(os.environ, dict(shell, SHARD="1"), clear=True):
            self.assertNotIn("DEVKIT_TEST_PART", gitx._local_env(step["env"]))

    # The test job ran the whole suite on one machine per Python version: about 46 minutes, under a 90-minute
    # timeout. It runs in parts: each loads every Nth test module by name, the parts together load each module once,
    # and each ends with the real-state check of its own run. Without a part (dev ci --run, a developer's run),
    # tests/__init__.py defines no load_tests, so discovery loads every module as it did before the parts.
    def test_the_parts_load_each_test_module_once_and_no_part_loads_every_module(self):
        parts = matrix_parts()
        self.assertGreater(len(parts), 1)
        self.assertEqual(unit_step()["env"].get("PARTS"), str(len(parts)))
        with tempfile.TemporaryDirectory() as tmp:
            root, every = suite_fixture(Path(tmp))
            whole = discovered(root, None, len(parts))
            self.assertEqual(whole["modules"], every)  # every module, in discovery's own order
            self.assertFalse(whole["load_tests"])  # nothing changes how discovery loads the suite
            runs = [discovered(root, k, len(parts)) for k in parts]
            for run in runs:
                self.assertTrue(run["load_tests"])
                self.assertEqual(run["modules"][-1], "tests.test_zz_real_state_untouched")
            taken = [m for run in runs for m in run["modules"][:-1]]
            self.assertEqual(sorted(taken), [m for m in every if "test_zz" not in m])  # none missing, none twice
            self.assertLessEqual(max(len(r["modules"]) for r in runs) - min(len(r["modules"]) for r in runs), 1)
            self.assertIn("broken at import", next(r["errors"] for r in runs if "tests.test_broken" in r["modules"]))

    # The part is read before the run's activation drops devkit's own variables, so the processes of a part's run
    # never inherit it: a discovery a test starts there (a nested dev ci --run, a suite fixture) loads every module.
    def test_a_discovery_a_part_run_starts_loads_every_module(self):
        parts = matrix_parts()
        with tempfile.TemporaryDirectory() as tmp:
            root, every = suite_fixture(Path(tmp))
            run = discovered(root, parts[-1], len(parts), nested=True)
            self.assertTrue(run["load_tests"])
            self.assertLess(len(run["modules"]), len(every))
            self.assertEqual(run["nested"], every)

    # devkit's own unit step, run by dev ci --run with one failing test, resumes with that test and the tests its fix
    # can affect, not the whole suite: the run is observed, so it leaves a completion receipt and its failed tests.
    def test_a_failed_unit_step_resumes_with_its_failed_test_only(self):
        from test_gitx import Sandbox, dev
        step = unit_step()
        with Sandbox() as s:
            run = "".join(f"          {line}\n" for line in step["run"].strip().splitlines())
            env = "".join(f"          {k}: {json.dumps(v)}\n" for k, v in step["env"].items())
            s.write(".github/workflows/ci.yml", "name: ci\non: [pull_request]\njobs:\n  test:\n"
                                                "    runs-on: macos-latest\n    timeout-minutes: 40\n"
                                                "    strategy:\n      matrix:\n        shard: [1, 2]\n"
                                                "    steps:\n      - name: Unit tests\n"
                                                f"        run: |\n{run}        env:\n{env}")
            (s.repo / "tests").mkdir()
            shutil.copyfile(ROOT / "tests" / "__init__.py", s.repo / "tests" / "__init__.py")
            s.write("tests/_hermetic.py", HERMETIC)
            for name in ("test_a", "test_b", "test_c"):
                s.write(f"tests/{name}.py", ONE_TEST)
            s.write("tests/test_b.py", ONE_TEST.replace("pass", "self.assertEqual(open('answer.txt').read(), '42')"))
            s.write("answer.txt", "41")
            s.write(".gitignore", "__pycache__/\n")  # as devkit's own: the run's bytecode is no change
            s.branch("feat/ci")
            s.commit("ci: run the tests")
            shell = {k: v for k, v in os.environ.items() if k != "DEVKIT_TEST_PART"}
            with mock.patch.dict(os.environ, shell, clear=True):
                rc, out, err = dev("ci", "--run")
                self.assertEqual(rc, 1, out + err)
                rec = gitx.step_records(s.ctx())["test › Unit tests"]
                self.assertEqual((rec["runner"], rec["complete"]), ("unittest", True))
                self.assertEqual(rec["failed_tests"], ["tests.test_b.T.test_it"])
                s.write("answer.txt", "42")
                s.commit("fix: the answer")
                rc, out, err = dev("ci", "--run", "--resume")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("re-ran 1 failed or affected test(s)", out)


if __name__ == "__main__":
    unittest.main()
