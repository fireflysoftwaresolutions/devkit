# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
# A nested claude or codex session started by a program that spawns it (expect and its script file, tclsh with spawn
# or exec, python's pexpect, script, unbuffer) is judged as the same command typed: its flags are read where the
# program writes them, also when the program comes through its input (`expect < f`, `cat f | expect`). Each refused
# form has a twin that starts the agent the honest way and stays allowed.
# Run: python3 -m unittest tests.test_guard_spawners -v
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard  # noqa: E402

ENFORCE = {"safeguards": {"irreversible_main": "enforce", "irreversible_member": "enforce"}}
BYPASS = "codex --dangerously-bypass-hook-trust"


class SpawnedAgentSessions(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def file(self, name, body):
        p = self.dir / name
        p.write_text(body)
        return p

    def main(self, cmd):
        v = guard.check("Bash", {"command": cmd}, str(self.dir), "claude")
        return v[1] if v and v[0] == "deny" else None

    def member(self, cmd):
        with mock.patch.object(core, "load_config", return_value=ENFORCE):
            v = guard.crew_shell_verdict(cmd, str(self.dir), None, "kid", level="all", lane="a1", session="s1",
                                         grant=lambda *a: None)
        return v[1] if v else None

    def test_an_agent_flag_inside_a_spawning_program_is_refused_and_its_twin_runs(self):
        bad = self.file("bad.exp", f"spawn {BYPASS}\ninteract\n")
        conf = self.file("conf.exp", "spawn codex -c hooks.enabled=false\ninteract\n")
        ok = self.file("ok.exp", 'spawn codex exec "say hi"\nexpect eof\n')
        tcl = self.file("bad.tcl", f"package require Expect\nspawn {BYPASS}\ninteract\n")
        tcl_exec = self.file("bad2.tcl", f"exec {BYPASS}\n")
        tcl_ok = self.file("ok.tcl", "exec git status\n")
        px = self.file("px.py", f'import pexpect\npexpect.spawn("{BYPASS}").interact()\n')
        px_ok = self.file("px_ok.py", 'import pexpect\npexpect.spawn("codex exec hi").interact()\n')
        pairs = ((f"expect {bad}", f"expect {ok}"), (f"expect -f {bad}", f"expect -f {ok}"),
                 (f"expect -- {bad}", f"expect -- {ok}"), (f"expect -d {bad}", f"expect -d {ok}"),
                 (f"expect {conf}", f"expect {ok}"), (f"expect < {bad}", f"expect < {ok}"),
                 (f"cat {bad} | expect", f"cat {ok} | expect"), (f"expect - < {bad}", f"expect - < {ok}"),
                 (f"unbuffer expect {bad}", f"unbuffer expect {ok}"), (f"tclsh {tcl}", f"tclsh {tcl_ok}"),
                 (f"tclsh {tcl_exec}", f"tclsh {tcl_ok}"), (f"tclsh < {tcl_exec}", f"tclsh < {tcl_ok}"),
                 (f"python3 {px}", f"python3 {px_ok}"), (f"python3 < {px}", f"python3 < {px_ok}"),
                 (f"cat {px} | python3", f"cat {px_ok} | python3"),
                 (f"script -q /dev/null {BYPASS}", "script -q /dev/null codex exec hi"),
                 (f"unbuffer {BYPASS}", "unbuffer codex exec hi"))
        for bad_cmd, twin in pairs:
            with self.subTest(cmd=bad_cmd):
                self.assertIn("nested", self.main(bad_cmd) or "")
                self.assertIn("nested", self.member(bad_cmd) or "")
            with self.subTest(twin=twin):
                self.assertIsNone(self.main(twin))
                if "python3" in twin:  # pexpect: a member's program the allow-list judge does not vouch for
                    self.assertIn("run the command it needs yourself", self.member(twin) or "")
                else:
                    self.assertIsNone(self.member(twin))


if __name__ == "__main__":
    unittest.main()
