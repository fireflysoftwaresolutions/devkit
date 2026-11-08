# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The suite's isolation (tests/_hermetic.py, activated by tests/__init__.py): the run has a temporary home, devkit
# resolves its state, configuration and handoff below it, nothing in the environment names the developer's live
# session, and this process cannot open, change or list the real roots or start a process with the real home.
# The probes of the refusals use paths below the real roots that do not exist, so a run without the isolation fails
# them on FileNotFoundError and still writes nothing. Tests carry comments, not docstrings: `dev test prove` leaves
# out tests with a docstring. Run: python3 -m unittest tests.test_hermetic
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _hermetic  # noqa: E402

_hermetic.activate()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from devkit import core, selfcheck  # noqa: E402

RUN = "python3 -m unittest discover -s tests -t ."


def below(path, root) -> bool:
    p, r = os.path.realpath(str(path)), os.path.realpath(str(root))
    return p == r or p.startswith(r + os.sep)


class IsolatedRun(unittest.TestCase):
    def setUp(self):
        st = _hermetic.state()
        if not st["active"]:
            self.fail(f"the run is not isolated: devkit was imported before tests/_hermetic.py ran; run the suite as "
                      f"a package: {RUN} (or python3 -m unittest tests.<module>)")
        self.home = st["home"]

    # the run's HOME is a folder of its own, not the developer's home as the account database knows it, and not a
    # folder the guard treats as scratch (a real home never is one; the guard tests rely on that)
    def test_the_run_has_a_temporary_home(self):
        self.assertTrue(os.path.basename(os.path.dirname(os.environ["HOME"])).startswith("devkit-hermetic-"))
        self.assertFalse(core.is_temp(os.environ["HOME"]), os.environ["HOME"])
        self.assertTrue(_hermetic.state()["homes"])
        self.assertNotIn(os.path.realpath(str(Path.home())), _hermetic.state()["homes"])

    # devkit's state, configuration, handoff, dev root and the agents' homes all resolve below that home
    def test_devkit_resolves_every_root_below_the_temporary_home(self):
        for name in ("STATE", "CONFIG_FILE", "HANDOFF", "DEV", "WT_ROOT", "CLAUDE_HOME", "CODEX_HOME", "AGENTS_HOME"):
            with self.subTest(name):
                self.assertTrue(below(getattr(core, name), self.home), f"core.{name} = {getattr(core, name)}")

    # nothing in the environment names the developer's live session, moves a root away from HOME, or names one of
    # their real devkit roots
    def test_no_variable_names_the_live_session_or_a_real_root(self):
        names = [k for k in os.environ if k in _hermetic.SCRUB or
                 (k.startswith("DEVKIT_") and k not in _hermetic.KEEP and k != _hermetic.MARK)]
        self.assertEqual([], names)
        self.assertEqual({"DEVKIT_HOME": str(Path(__file__).resolve().parents[1]), "DEVKIT_NO_NOTIFY": "1"},
                         {k: os.environ.get(k) for k in _hermetic.KEEP})
        real = [k for k, v in os.environ.items() if any(below(p, r) for p in v.split(os.pathsep) if p.startswith("/")
                                                        for r in _hermetic.state()["real"].values())]
        self.assertEqual([], real)

    # whatever the shell put in the environment, an entry that names a real root goes, by its value and not by the
    # variable's name: zsh's FPATH can list a devkit release's completions folder, and its _ the last command's path;
    # the other entries of a path list stay, and a folder that only starts with a root's name is not below it
    def test_the_scrub_removes_every_value_that_names_a_real_root(self):
        real = _hermetic.state()["real"]
        completions = os.path.join(real["install"], "releases", "26.11.5-c343681", "shell", "completions")
        env = {"FPATH": os.pathsep.join(["/usr/share/zsh/functions", completions, "/opt/zsh/site"]),
               "_": os.path.join(real["install"], "current", "bin", "dev"),
               "LAST_LOG": os.path.join(real["state"], "logs", "x.log"),
               "NEAR": real["state"] + "-other",
               "PLAIN": "/usr/bin", "CLAUDECODE": "1", "DEVKIT_GUARD": "off", "DEVKIT_HOME": "/x"}
        changed = _hermetic.scrub(env)
        self.assertEqual({"FPATH": "/usr/share/zsh/functions" + os.pathsep + "/opt/zsh/site",
                          "NEAR": real["state"] + "-other", "PLAIN": "/usr/bin", "DEVKIT_HOME": "/x"}, env)
        self.assertEqual(["CLAUDECODE", "DEVKIT_GUARD", "FPATH", "LAST_LOG", "_"], sorted(changed))

    # a process a test starts resolves the same temporary roots, through the environment it inherits
    def test_a_child_process_resolves_the_temporary_home(self):
        lib = str(Path(__file__).resolve().parents[1] / "lib")
        code = (f"import sys, json; sys.path.insert(0, {lib!r}); from devkit import core; "
                "print(json.dumps([str(core.STATE), str(core.CONFIG_FILE), str(core.HANDOFF)]))")
        out = subprocess.run([sys.executable, "-I", "-S", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, out.returncode, out.stderr)
        for p in json.loads(out.stdout):
            self.assertTrue(below(p, self.home), p)


class RefusedAccess(unittest.TestCase):
    def setUp(self):
        st = _hermetic.state()
        if not st["active"] or not st["real"]:
            self.fail(f"the run is not isolated; run the suite as a package: {RUN}")
        self.real = st["real"]
        self.seen = len(st["violations"])
        self.probe = f"hermetic-probe-{uuid.uuid4().hex}"

    def tearDown(self):
        # the refusals these tests provoke are theirs: they are not leaks of the run
        del _hermetic.state()["violations"][self.seen:]

    def assert_refused(self, fn, what):
        with self.assertRaises(PermissionError, msg=what) as cm:
            fn()
        self.assertIn("hermetic test run", str(cm.exception))
        self.assertTrue(any(what in v for v in _hermetic.state()["violations"][self.seen:]), what)

    # every way of writing below the real state, configuration and handoff roots is refused and recorded, with the
    # path built by a call (os.path.join, Path.joinpath); every probe's parent folder does not exist, so without the
    # isolation each one fails on FileNotFoundError and creates nothing
    def test_writes_below_the_real_roots_are_refused(self):
        for root in ("state", "config", "handoff"):
            base = os.path.join(self.real[root], self.probe)
            f = os.path.join(base, "f.json")
            writes = {
                "open w": lambda: open(f, "w"),
                "open a": lambda: open(f, "a"),
                "Path.write_text": lambda: Path(self.real[root]).joinpath(self.probe, "f.json").write_text("{}"),
                "os.open": lambda: os.open(f, os.O_WRONLY | os.O_CREAT),
                "os.mkdir": lambda: os.mkdir(os.path.join(base, "d")),
                "os.remove": lambda: os.remove(f),
                "os.replace": lambda: os.replace(f, f + ".new"),
                "shutil.rmtree": lambda: shutil.rmtree(base),
            }
            for name, fn in writes.items():
                with self.subTest(root=root, write=name):
                    self.assert_refused(fn, base)
            self.assertFalse(os.path.exists(base))

    # reading or listing the real roots is refused too: the run must not depend on the developer's state
    def test_reads_below_the_real_roots_are_refused(self):
        for root in ("state", "config", "handoff"):
            f = os.path.join(self.real[root], self.probe)
            for name, fn in {"open r": lambda: open(f), "Path.read_text": lambda: Path(f).read_text(),
                             "os.listdir": lambda: os.listdir(f), "os.scandir": lambda: os.scandir(f)}.items():
                with self.subTest(root=root, read=name):
                    self.assert_refused(fn, f)

    # a process started with the developer's home, with no HOME (Path.home() then falls back to the account's home),
    # with a variable or an argument that names a real root, or in a real root, is refused before it starts; the
    # same command with the run's home and a temporary folder starts
    def test_a_child_that_would_reach_the_real_devkit_is_refused(self):
        homes = sorted(_hermetic.state()["homes"])
        own = self.real_tmp()
        state = os.path.join(self.real["state"], self.probe)
        cases = {"real HOME": (["/usr/bin/true"], {"HOME": homes[0]}, None),
                 "no HOME": (["/usr/bin/true"], {"PATH": "/usr/bin:/bin"}, None),
                 "real DEVKIT_CONFIG": (["/usr/bin/true"], {"HOME": own, "DEVKIT_CONFIG": os.path.join(
                     self.real["config"], "config.json")}, None),
                 "argument": (["/usr/bin/true", state], None, None),
                 "shell string": ("/usr/bin/true " + state + "/x", None, None),
                 "folder": (["/usr/bin/true"], None, state)}
        for name, (argv, env, cwd) in cases.items():
            with self.subTest(name):
                self.assert_refused(lambda: subprocess.run(argv, env=env, cwd=cwd, shell=isinstance(argv, str)),
                                    "/usr/bin/true")
        # a folder that only starts with a root's name is not below it
        self.assertEqual(0, subprocess.run(["/usr/bin/true", os.path.join(own, "x"), self.real["state"] + "-other"],
                                           cwd=own).returncode)

    # a security notice ignores DEVKIT_NO_NOTIFY, so the run itself refuses osascript: a test never shows the
    # developer a desktop notification, by any spelling of the program or way of starting it. The script only
    # returns a word, so a run without the refusal shows nothing either
    def test_a_desktop_notification_is_refused(self):
        script = ["-e", "on run argv", "-e", "return item 1 of argv", "-e", "end run", "--", "x"]
        line = "cd . && osascript -e 'return 1' >/dev/null"
        starts = {
            "absolute": lambda: subprocess.run(["/usr/bin/osascript", *script], capture_output=True),
            "bare": lambda: subprocess.run(["osascript", *script], capture_output=True),
            "shell=True": lambda: subprocess.run(line, shell=True, capture_output=True),
            "sh -c": lambda: subprocess.run(["/bin/sh", "-c", line], capture_output=True),
            "executable= with another argv[0]": lambda: subprocess.run(["note", *script], capture_output=True,
                                                                       executable="/usr/bin/osascript"),
            "posix_spawn with another argv[0]": lambda: os.waitpid(
                os.posix_spawn("/usr/bin/osascript", ["note", *script], dict(os.environ)), 0),
            "os.system": lambda: os.system(line),
            "env": lambda: subprocess.run(["/usr/bin/env", "X=1", "osascript", *script], capture_output=True),
        }
        for name, start in starts.items():
            with self.subTest(name):
                self.assert_refused(start, "osascript")
        # another program still starts, and a shell line that only names osascript as a word runs
        self.assertEqual(0, subprocess.run(["/usr/bin/true"]).returncode)
        self.assertEqual(0, subprocess.run("echo osascript >/dev/null", shell=True).returncode)
        self.assertEqual(0, os.system("grep -c osascript /dev/null >/dev/null; true"))

    def real_tmp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        return d


class TheDiff(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.roots = {"state": os.path.join(self.d, "state"), "handoff": os.path.join(self.d, "handoff")}
        for rel, text in {"state/config-installed.json": "{}", "state/audit/2026-11.jsonl": '{"a":1}\n',
                          "state/sleep.json": "{}", "handoff/projects/acme/x.md": "# x\n", "handoff/README.md": "r"}.items():
            p = Path(self.d, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)

    def touch(self, rel, text, mode="w"):
        with open(os.path.join(self.d, rel), mode) as f:
            f.write(text)
        st = os.stat(os.path.join(self.d, rel))
        os.utime(os.path.join(self.d, rel), ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))

    def change_everything(self):
        self.touch("state/sleep.json", '{"x": 1}')
        self.touch("state/new.json", "{}")
        os.remove(os.path.join(self.d, "handoff/README.md"))
        self.touch("state/audit/2026-11.jsonl", '{"a":2,"actor":"live session"}\n', "a")
        self.touch("handoff/projects/acme/x.md", "# x, a live session's note\n")
        os.makedirs(os.path.join(self.d, "state/continuity"))
        self.touch("state/continuity/claude-devkit-hermetic-abc.json", "{}")
        self.touch("handoff/projects/acme/y.md", "written below /tmp/devkit-hermetic-abc/home\n")

    # in an isolated run, the changes that carry the run's mark (in their path or their bytes) are its leaks,
    # wherever they are; the others are other processes', named only outside the live hooks' files
    def test_an_isolated_run_owns_the_changes_that_carry_its_mark(self):
        before = _hermetic.snapshot(self.roots)
        self.change_everything()
        leaks, others = _hermetic.changes(before, _hermetic.snapshot(self.roots), self.roots, ["devkit-hermetic-abc"])
        self.assertEqual(["state: continuity/claude-devkit-hermetic-abc.json (created)",
                          "handoff: projects/acme/y.md (created)"], leaks)
        self.assertEqual(["state: new.json (created)", "state: sleep.json (changed)", "handoff: README.md (removed)"],
                         others)

    # in a run that is not isolated (no mark), every change is a leak but the live hooks' own files
    def test_a_run_that_is_not_isolated_owns_every_change_but_the_live_writers(self):
        before = _hermetic.snapshot(self.roots)
        self.change_everything()
        leaks, others = _hermetic.changes(before, _hermetic.snapshot(self.roots), self.roots, [])
        self.assertEqual(["state: new.json (created)", "state: sleep.json (changed)", "handoff: README.md (removed)"],
                         leaks)
        self.assertEqual([], others)

    # an append is judged on the appended bytes only: an old record carrying the mark does not make the next live
    # record a leak, and a new record carrying it does
    def test_an_append_is_judged_on_its_new_bytes(self):
        self.touch("state/audit/2026-11.jsonl", '{"old":"/tmp/devkit-hermetic-abc"}\n')
        before = _hermetic.snapshot(self.roots)
        self.touch("state/audit/2026-11.jsonl", '{"live":1}\n', "a")
        mid = _hermetic.snapshot(self.roots)
        self.assertEqual(([], []), _hermetic.changes(before, mid, self.roots, ["devkit-hermetic-abc"]))
        self.touch("state/audit/2026-11.jsonl", '{"action":"crew.limit","cwd":"/tmp/devkit-hermetic-abc"}\n', "a")
        self.assertEqual((["state: audit/2026-11.jsonl (changed)"], []),
                         _hermetic.changes(mid, _hermetic.snapshot(self.roots), self.roots, ["devkit-hermetic-abc"]))

    # the file the run's own stdout or stderr goes to (dev ci --run streams the unit-test step into
    # state/ci/logs/<key>/NN-*.log) is where the caller asked the output to go, not a leak; every other marked
    # change below the roots still is one
    def test_the_runs_own_output_file_is_not_a_leak(self):
        os.makedirs(os.path.join(self.d, "state/ci/logs/o__r"))
        out = open(os.path.join(self.d, "state/ci/logs/o__r/02-tests-unit-tests.log"), "w")
        self.addCleanup(out.close)
        before = _hermetic.snapshot(self.roots)
        own = _hermetic.outputs(self.roots, before, fds=(out.fileno(),))
        self.assertEqual({"state: ci/logs/o__r/02-tests-unit-tests.log"}, own)
        out.write("ResourceWarning: unclosed file /tmp/devkit-hermetic-abc/home/.local/state/devkit/logs/sync.log\n")
        out.flush()
        self.touch("state/sleep.json", '{"cwd": "/tmp/devkit-hermetic-abc"}')
        leaks, _ = _hermetic.changes(before, _hermetic.snapshot(self.roots), self.roots, ["devkit-hermetic-abc"],
                                     own=own)
        self.assertEqual(["state: sleep.json (changed)"], leaks)

    # a terminal, a pipe or a file outside the roots is no file of the roots
    def test_output_outside_the_roots_names_nothing(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        outside = tempfile.NamedTemporaryFile("w")
        self.addCleanup(outside.close)
        self.assertEqual(set(), _hermetic.outputs(self.roots, _hermetic.snapshot(self.roots),
                                                  fds=(w, outside.fileno(), 987)))


class LateRun(unittest.TestCase):
    # when devkit was imported before the isolation (python3 -m unittest discover -s tests, which skips
    # tests/__init__.py), activate() leaves the environment alone and reports the run as not isolated
    def test_a_run_where_devkit_came_first_is_left_alone_and_reported(self):
        spec = importlib.util.spec_from_file_location("_hermetic_late", _hermetic.__file__)
        late = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(late)
        env = dict(os.environ)
        st = late.activate()
        self.assertEqual((True, False, None, None), (st["late"], st["active"], st["root"], st["owner"]))
        self.assertEqual(env, dict(os.environ))


class RealRoots(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def write_config(self, cfg):
        p = Path(self.home, ".config", "devkit", "config.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg))

    # the real handoff follows the real configuration like devkit does: handoff_dir, else dev_root/.handoff, else
    # ~/Development/.handoff; DEV_ROOT, when the developer set it, moves the dev root
    def test_the_handoff_follows_the_real_configuration(self):
        h = self.home
        cases = [({}, {}, f"{h}/Development/.handoff"),
                 ({"dev_root": "~/Code"}, {}, f"{h}/Code/.handoff"),
                 ({"dev_root": "~/Code", "handoff_dir": "~/notes/handoff"}, {}, f"{h}/notes/handoff"),
                 ({}, {"DEV_ROOT": f"{h}/elsewhere"}, f"{h}/elsewhere/.handoff")]
        for cfg, env, want in cases:
            with self.subTest(cfg=cfg, env=env):
                self.write_config(cfg)
                got = _hermetic.real_roots(h, environ=env)
                self.assertEqual(want, got["handoff"])
                self.assertEqual(f"{h}/.local/state/devkit", got["state"])
                self.assertEqual(f"{h}/.config/devkit", got["config"])


class TemporaryHomeRemoved(unittest.TestCase):
    # the process that made the temporary home removes it when it exits; a process it starts keeps the run's home
    def test_the_temporary_home_is_removed_at_exit_and_kept_by_children(self):
        here = os.path.dirname(os.path.abspath(__file__))
        code = (f"import sys, os; sys.path.insert(0, {here!r}); import _hermetic; s = _hermetic.activate(); "
                "print(s['root']); print(s['home'] == os.environ['HOME'])")
        env = {k: v for k, v in os.environ.items() if k != _hermetic.MARK}
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(0, out.returncode, out.stderr)
        root, same = out.stdout.split()
        self.assertIn("devkit-hermetic-", os.path.basename(root))
        self.assertEqual("True", same)
        self.assertFalse(os.path.exists(root), f"{root} is still there")
        child = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertEqual(os.path.dirname(os.environ["HOME"]), child.stdout.split()[0])
        self.assertTrue(os.path.isdir(os.environ["HOME"]))


class DriftCheck(unittest.TestCase):
    def tree(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        for rel, text in files.items():
            p = Path(d, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return d

    # D12: a tree without the isolation point, or with a documented suite command that runs tests/ as the top level,
    # has one finding each; the same tree with both fixed has none
    def test_d12_names_a_missing_isolation_point_and_each_bare_suite_command(self):
        bare = self.tree({"tests/test_x.py": "", "CONTRIBUTING.md": "python3 -m unittest discover -s tests\n",
                          ".github/workflows/ci.yml": "run: python -m unittest discover -s tests -v\n",
                          "docs/dev.md": "`/usr/bin/python3 -m unittest discover -s tests -t .`\n"})
        self.assertEqual(["tests/__init__.py: does not activate tests/_hermetic.py",
                          ".github/workflows/ci.yml: python -m unittest discover -s tests -v",
                          "CONTRIBUTING.md: python3 -m unittest discover -s tests"],
                         [f.item for f in selfcheck.d12_hermetic_tests(bare)])
        fixed = self.tree({"tests/__init__.py": "import _hermetic\n_hermetic.activate()\n",
                           "tests/_hermetic.py": "", "CONTRIBUTING.md": "python3 -m unittest discover -s tests -t .\n",
                           ".github/workflows/ci.yml": "run: python -m unittest discover -t . -s tests\n"})
        self.assertEqual([], selfcheck.d12_hermetic_tests(fixed))


if __name__ == "__main__":
    unittest.main()
