# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# Every place devkit starts itself through an interpreter ([sys.executable, .../bin/dev], python3 .../bin/dev, the
# Desktop launcher) runs it the way bin/dev's shebang does: `-I -S`. Started without them, the child imports from
# PYTHONPATH, the current folder and site-packages (sitecustomize, .pth files), so code anyone planted there runs
# inside devkit, the guard included. The scan fails while any spawn lacks the flags; the runs prove the effect.
# Run: python3 -m unittest tests.test_isolated_spawns -v
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "lib" / "devkit"
sys.path.insert(0, str(ROOT / "lib"))

from devkit import jobs, mcp, wizard  # noqa: E402

# An element that names devkit's own entry point (or the Desktop extension's launcher, which execs it).
_DEV = re.compile(r"""['"]bin['"]\s*/\s*['"]dev['"]|\bDEV_BIN\b|/bin/dev\b|\blauncher\b""")
# Every argv devkit's code starts with its own interpreter is devkit (the project's tests run the project's Python,
# `py` from its virtualenv, which names no bin/dev): the rule holds for each one, so a new spawn cannot skip it.
_INTERPRETER = {"sys.executable", "py", "'python3'"}
# The files known to start devkit: the scan must find a spawn in each, so a scan that silently matches nothing fails.
_KNOWN = {"setup.py", "ciwatch.py", "jobs.py", "mcp.py", "desktop.py", "wizard.py", "hooks.py", "cli.py", "work.py",
          "dashserver.py", "launcher.py"}
LAUNCHER = ROOT / "desktop" / "server" / "launcher.py"
MANIFEST = ROOT / "desktop" / "manifest.json"


def _spawns():
    """-> [(file, line, [element source, ...])] for every list or tuple literal that starts devkit through an
    interpreter, and [(file, line, text)] for every launchd plist text that does."""
    lists, plists = [], []
    for f in [*sorted(LIB.glob("*.py")), LAUNCHER]:
        tree = ast.parse(f.read_text(), str(f))
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
                src = [ast.unparse(e) for e in node.elts]
                if src[0] == "sys.executable" or src[0] in _INTERPRETER and any(_DEV.search(s) for s in src[1:]):
                    lists.append((f.name, node.lineno, src))
            elif isinstance(node, (ast.JoinedStr, ast.Constant)) and "<string>python3</string>" in ast.unparse(node):
                plists.append((f.name, node.lineno, ast.unparse(node)))
    return lists, plists


def _plant(tmp: Path) -> Path:
    """A folder of code that must never run: a sitecustomize on PYTHONPATH, a startup file, a .pth in the user
    site. Each writes the marker it returns when imported."""
    plant, hit = tmp / "plant", tmp / "planted-code-ran"
    code = f"open({str(hit)!r}, 'w').write('x')\n"
    (plant / "user" / "lib").mkdir(parents=True)
    (plant / "sitecustomize.py").write_text(code)
    (plant / "start.py").write_text(code)
    for v in ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14", "3.15"):
        sp = plant / "user" / "lib" / f"python{v}" / "site-packages"
        sp.mkdir(parents=True)
        (sp / "evil.pth").write_text("import os; " + code)
    return hit


def _planted_env(tmp: Path) -> dict:
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    plant = tmp / "plant"
    return {"PYTHONPATH": str(plant), "PYTHONSTARTUP": str(plant / "start.py"),
            "PYTHONUSERBASE": str(plant / "user"), "HOME": str(home), "DEVKIT_NO_NOTIFY": "1"}


class ScanTests(unittest.TestCase):
    def test_every_self_spawn_runs_isolated(self):
        lists, _ = _spawns()
        self.assertTrue(_KNOWN <= {f for f, _, _ in lists},
                        f"the scan no longer finds the spawns in {sorted(_KNOWN - {f for f, _, _ in lists})}")
        bad = [f"{f}:{n} {src[:3]}" for f, n, src in lists if src[1:3] != ["'-I'", "'-S'"]]
        self.assertEqual(bad, [], "devkit starts itself without -I -S")

    def test_the_mcp_server_keeps_utf8_output(self):
        # -I ignores PYTHONIOENCODING, so the MCP server's children need UTF-8 mode on the command line.
        lists, _ = _spawns()
        mine = [src for f, _, src in lists if f == "mcp.py"]
        self.assertTrue(mine)
        for src in mine:
            self.assertEqual(src[3:5], ["'-X'", "'utf8'"], src)

    def test_claude_desktop_starts_the_launcher_isolated(self):
        cfg = json.loads(MANIFEST.read_text())["server"]["mcp_config"]
        self.assertEqual(cfg["command"], "python3")
        self.assertEqual(cfg["args"][:2], ["-I", "-S"])

    def test_launchd_plist_texts_run_isolated(self):
        _, plists = _spawns()
        self.assertTrue(plists, "the scan no longer finds the scheduled sync's plist")
        for f, n, text in plists:
            self.assertIn("<string>python3</string><string>-I</string><string>-S</string>", text, f"{f}:{n}")


class RunTests(unittest.TestCase):
    """The argv devkit builds, run for real with planted code: nothing planted runs, and devkit still works."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.hit = _plant(self.t)
        self.env = {**os.environ, **_planted_env(self.t)}

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, argv):
        r = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=60, cwd=str(self.t / "plant"),
                           stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("devkit", r.stdout)
        self.assertFalse(self.hit.exists(), f"planted code ran under {argv[:3]}")

    def test_scheduled_job_argv_runs_no_planted_code(self):
        argv = jobs.plist_for("sync", {"cmd": ["sync"], "every": 60})["ProgramArguments"]
        self._run(argv[:-3] + ["version"])

    def test_the_desktop_extension_command_runs_no_planted_code(self):
        cfg = json.loads(MANIFEST.read_text())["server"]["mcp_config"]
        args = [a.replace("${__dirname}", str(ROOT / "desktop")) for a in cfg["args"]]
        self.env["DEVKIT_HOME"] = str(ROOT)
        r = subprocess.run([sys.executable, *args, "--check"], env=self.env, capture_output=True, text=True,
                           timeout=60, cwd=str(self.t / "plant"), stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)["ok"], r.stdout)
        self.assertFalse(self.hit.exists(), "planted code ran in the Desktop extension's launcher")

    def test_wizard_argv_runs_no_planted_code(self):
        self._run(wizard._dev("version"))

    def test_mcp_runs_no_planted_code(self):
        with mock.patch.dict(os.environ, _planted_env(self.t)):
            rc, out, err = mcp.run_dev(["version"], timeout=60, cwd=str(self.t / "plant"))
        self.assertEqual(rc, 0, err)
        self.assertIn("devkit", out)
        self.assertFalse(self.hit.exists(), "planted code ran under the MCP server's dev")

    def test_mcp_child_is_isolated_and_in_utf8_mode(self):
        probe = self.t / "probe-dev"
        probe.write_text("import json, sys\nf = sys.flags\n"
                         "print(json.dumps([f.isolated, f.no_site, f.utf8_mode, sys.stdout.encoding.lower()]))\n"
                         "print('\\u2713')\n")
        with mock.patch.object(mcp, "DEV_BIN", probe), \
                mock.patch.dict(os.environ, {**_planted_env(self.t), "LC_ALL": "en_US.ISO8859-1"}):
            # a Latin-1 locale: without UTF-8 mode, Python before 3.15 writes Latin-1 and cannot print the tick
            rc, out, err = mcp.run_dev(["x"], timeout=60, cwd=str(self.t))
        self.assertEqual(rc, 0, err)
        flags, tick = out.splitlines()[:2]
        self.assertEqual(json.loads(flags), [1, 1, 1, "utf-8"])
        self.assertEqual(tick, "\u2713")


if __name__ == "__main__":
    unittest.main()
