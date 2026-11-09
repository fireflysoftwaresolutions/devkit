# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# The suite's safety net keeps this Mac's real codex, claude, brew and gh out of every test (tests/_sandbox.py):
# devkit keeps the stand-ins of DEVKIT_TEST_PATH ahead of Homebrew's folders only in a test sandbox (a temporary HOME
# and temporary folders), the test process and devkit's children start the stand-ins, the audit hook refuses and
# records a start of one of the four from anywhere else, and an older devkit copy runs through a launcher that does
# the same for code that predates the switch.
#
# No test here starts a real program: a "real" codex is a script in a temporary folder that the test treats as outside
# the stand-ins, and the one check on Homebrew's own codex only resolves it.
#
# Tests carry comments, not docstrings (`dev test prove` leaves out tests with one).
# Run: python3 -m unittest tests.test_test_safety
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TREE / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _hermetic  # noqa: E402
import _sandbox  # noqa: E402
from devkit import core  # noqa: E402

FAKE_REAL = '#!/bin/sh\necho ran > "$(dirname "$0")/ran"\n'  # what a start of the "real" program leaves behind


class Folders(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        self.home.mkdir()

    def real_codex(self) -> Path:
        # a "real" codex: outside the stand-in folders, and it leaves a file when it runs
        folder = self.root / "opt-homebrew-bin"
        folder.mkdir(exist_ok=True)
        (folder / "codex").write_text(FAKE_REAL)
        (folder / "codex").chmod(0o755)
        return folder / "codex"


class SandboxPathTests(Folders):
    def test_the_stand_in_folders_come_first_with_a_temporary_home(self):
        stubs = _sandbox.stand_ins(self.root / "stubs")
        env = {"HOME": str(self.home), "DEVKIT_TEST_PATH": f"{stubs}{os.pathsep}{self.root / 'more'}"}
        self.assertEqual(core.sandbox_path(env), [str(stubs), str(self.root / "more")])

    def test_a_real_home_ignores_the_variable(self):
        stubs = _sandbox.stand_ins(self.root / "stubs")
        for home in ("/Users/ada", "/home/ada", "", "/tmp/../Users/ada"):
            with self.subTest(home=home):
                self.assertEqual(core.sandbox_path({"HOME": home, "DEVKIT_TEST_PATH": str(stubs)}), [])

    def test_a_folder_outside_the_temporary_folders_voids_the_whole_variable(self):
        stubs = _sandbox.stand_ins(self.root / "stubs")
        (self.root / "link").symlink_to("/usr/bin")  # a temporary name for a folder that is not temporary
        climb = str(self.root) + "/.." * len(self.root.parts) + "/usr/local/bin"  # a temporary start, then out
        for outside in ("/opt/homebrew/bin", str(self.root / "link"), climb, "rel"):
            with self.subTest(outside=outside):
                env = {"HOME": str(self.home), "DEVKIT_TEST_PATH": f"{stubs}{os.pathsep}{outside}"}
                self.assertEqual(core.sandbox_path(env), [])


class RunTests(unittest.TestCase):
    def test_the_test_process_finds_the_run_s_stand_ins_though_devkit_is_imported(self):
        folder = os.environ["DEVKIT_TEST_PATH"].split(os.pathsep)[0]
        for tool in _sandbox.TOOLS:
            with self.subTest(tool=tool):
                self.assertEqual(shutil.which(tool), os.path.join(folder, tool))


class ChildTests(Folders):
    def test_a_devkit_child_starts_the_stand_in_though_homebrew_is_first_on_its_path(self):
        # PATH as the old tests gave it: Homebrew first. The child starts codex only once it resolves to the stand-in,
        # so this test never starts the real one, with or without the net.
        stubs = _sandbox.stand_ins(self.root / "stubs")
        env = _sandbox.env({"HOME": str(self.home), "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
                           stubs=[stubs])
        code = ("import json, shutil, sys; sys.path.insert(0, sys.argv[1]); from devkit import core; "
                "w = shutil.which('codex'); "
                "print(json.dumps([w, core.sh(['codex', 'login']) if w == sys.argv[2] else None]))")
        p = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(TREE / "lib"), str(stubs / "codex")],
                           env=env, capture_output=True, text=True, timeout=60, cwd=str(self.home))
        found, ran = json.loads(p.stdout)
        self.assertEqual(found, str(stubs / "codex"), p.stderr)
        self.assertEqual(ran[0], 1)
        self.assertIn("stand-in of devkit's tests", ran[2])

    def test_a_tool_the_test_left_out_stays_absent_in_a_devkit_child(self):
        # a Mac without Homebrew: the test's own stand-ins lack brew, and devkit adds none of Homebrew's folders back
        stubs = _sandbox.stand_ins(self.root / "stubs")
        (stubs / "brew").unlink()
        env = _sandbox.env({"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, stubs=[stubs], run=False)
        code = ("import json, os, shutil, sys; sys.path.insert(0, sys.argv[1]); from devkit import core; "
                "print(json.dumps([os.environ['PATH'], shutil.which('brew')]))")
        p = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(TREE / "lib")], env=env, capture_output=True,
                           text=True, timeout=60, cwd=str(self.home))
        path, brew = json.loads(p.stdout)
        self.assertEqual(path, f"{stubs}:/usr/bin:/bin", p.stderr)
        self.assertIsNone(brew)


class TripwireTests(Folders):
    def setUp(self):
        super().setUp()
        self.stubs = _sandbox.stand_ins(self.root / "stubs")
        self.codex = self.real_codex()
        # only the stand-ins count as allowed here: the folder of the "real" codex is a temporary one too
        narrow = mock.patch.object(_sandbox, "ALLOWED", (str(self.stubs),))
        narrow.start()
        self.addCleanup(narrow.stop)
        seen = list(_hermetic.state()["violations"])
        self.addCleanup(lambda: _hermetic.state()["violations"].__setitem__(slice(None), seen))  # deliberate trips

    def refused(self, start):
        before = len(_hermetic.state()["violations"])
        with self.assertRaises(PermissionError) as cm:
            start()
        self.assertFalse((self.codex.parent / "ran").exists(), "the real codex ran")
        self.assertEqual(len(_hermetic.state()["violations"]), before + 1)
        self.assertIn(str(self.codex), _hermetic.state()["violations"][-1])
        return str(cm.exception)

    def test_a_subprocess_start_by_name_or_by_path_is_refused_and_recorded(self):
        env = {"HOME": str(self.home), "PATH": f"{self.codex.parent}:/usr/bin:/bin"}
        message = self.refused(lambda: subprocess.run(["codex", "login"], env=env, capture_output=True))
        self.assertIn("tests/_sandbox.py", message)
        self.refused(lambda: subprocess.run([str(self.codex)], env=env, capture_output=True))
        self.refused(lambda: subprocess.run(["/usr/bin/env", "X=1", "codex"], env=env, capture_output=True))

    def test_a_shell_line_that_starts_it_is_refused(self):
        # shell=True and os.system on purpose, with constant lines: the tripwire must see what a shell line starts
        env = {"HOME": str(self.home), "PATH": f"{self.codex.parent}:/usr/bin:/bin"}
        self.refused(lambda: subprocess.run("true && codex login", shell=True, env=env, capture_output=True))
        self.refused(lambda: subprocess.run(["/bin/bash", "-euc", "cd /; codex"], env=env, capture_output=True))
        self.refused(lambda: os.system(f"'{self.codex}' login"))

    def test_posix_spawn_is_refused(self):
        env = {"HOME": str(self.home), "PATH": f"{self.codex.parent}:/usr/bin:/bin"}
        self.refused(lambda: os.posix_spawn(str(self.codex), [str(self.codex)], env))
        self.refused(lambda: os.posix_spawnp("codex", ["codex"], env))

    def test_os_exec_is_refused_before_it_replaces_the_process(self):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # the child reports the refusal; were it not refused, the fake codex would run in its place
            os.close(r)
            try:
                os.execve(str(self.codex), [str(self.codex)], {"HOME": str(self.home)})
            except PermissionError as e:
                os.write(w, str(e).encode()[:500])
            finally:
                os._exit(0)
        os.close(w)
        with os.fdopen(r) as f:
            said = f.read()
        os.waitpid(pid, 0)
        self.assertIn(str(self.codex), said)
        self.assertFalse((self.codex.parent / "ran").exists(), "the real codex ran")

    def test_a_start_of_a_stand_in_or_of_another_program_is_allowed(self):
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:{self.codex.parent}:/usr/bin:/bin"}
        self.assertEqual(subprocess.run(["codex", "login"], env=env, capture_output=True).returncode, 1)
        self.assertEqual(subprocess.run(["true"], env=env).returncode, 0)

    @unittest.skipUnless(os.access("/opt/homebrew/bin/codex", os.X_OK), "needs Homebrew's codex on this Mac")
    def test_homebrew_s_own_codex_is_outside_what_a_test_may_start(self):
        # only resolved, never started
        why = _sandbox.refusal("subprocess.Popen", ("codex", ["codex", "login"], None, {"PATH": "/opt/homebrew/bin"}),
                               allowed=_sandbox.TEMP_ROOTS)
        self.assertIn(os.path.realpath("/opt/homebrew/bin/codex"), why)


class OlderCopyTests(Folders):
    def test_an_older_copy_runs_through_the_launcher_with_the_stand_ins_first_and_its_escapes_logged(self):
        # an older devkit whose core.py puts its own folders first, as devkit did before DEVKIT_TEST_PATH
        codex = self.real_codex()
        copy = self.root / "copy"
        (copy / "lib" / "devkit").mkdir(parents=True)
        (copy / "bin").mkdir()
        shutil.copy(TREE / "bin" / "dev", copy / "bin" / "dev")
        (copy / "lib" / "devkit" / "__init__.py").write_text("")
        # its core.py also keeps a copy of the environment from then for the commands it runs, as GIT_ENV does
        (copy / "lib" / "devkit" / "core.py").write_text(
            f"import os\nos.environ['PATH'] = {str(codex.parent)!r} + ':' + os.environ.get('PATH', '')\n"
            "GIT_ENV = dict(os.environ, LC_ALL='C')\n")
        (copy / "lib" / "devkit" / "cli.py").write_text(
            "import shutil, subprocess, sys\nfrom . import core\n\n\ndef main():\n"
            "    print(shutil.which('codex'))\n"
            "    print(subprocess.run(['codex', 'app-server'], env=core.GIT_ENV, capture_output=True).returncode)\n"
            f"    for argv in ([{str(codex)!r}], [sys.executable, '-I', '-S', sys.argv[0]]):\n"
            "        try:\n            subprocess.run(argv, capture_output=True)\n"
            "        except PermissionError:\n            print('refused')\n    return 0\n")
        log = self.root / "trips.jsonl"
        stubs = _sandbox.stand_ins(self.root / "stubs")
        env = _sandbox.env({"HOME": str(self.home), "PATH": "/usr/bin:/bin"}, stubs=[stubs],
                           DEVKIT_TEST_TRIPWIRE=str(log))
        p = subprocess.run(_sandbox.boot(copy / "bin" / "dev", "x"), env=env, capture_output=True, text=True,
                           timeout=60, cwd=str(self.home))
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.split(), [str(stubs / "codex"), "1", "refused", "refused"])  # 1: the stand-in
        self.assertFalse((codex.parent / "ran").exists(), "the real codex ran")
        trips = [json.loads(ln)["why"] for ln in log.read_text().splitlines()]
        self.assertEqual(len(trips), 2, trips)
        self.assertIn(str(codex), trips[0])
        self.assertIn("devkit", trips[1])


if __name__ == "__main__":
    unittest.main()
