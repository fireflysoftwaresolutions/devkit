# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# The impeccable launcher in crew lanes (lib/devkit/guard.py, impeccable_verb). Read as a shell script, the launcher's
# `exec "$bin"` and `"$1" engine-probe` name their program at run time, so the gate counted every run as tier 2 and
# refused it in every lane. The launcher devkit installed, run by its path, with the content and the engine the skills
# step recorded (STATE/skill-engines.json), is that engine and is not read as a script; read-only lanes run its read
# verbs with their reading flags only. Each hostile case (a swapped engine, an edited launcher, a variable that
# changes what it runs, a copy elsewhere, a write verb or flag) first shows the untouched launcher allowed, then
# refused. The skill and its made-up engine are installed in the run's temporary home with the download faked;
# nothing runs the launcher. Run: python3 -m unittest tests.test_guard_impeccable -v
import contextlib
import hashlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import _hermetic  # noqa: E402

_hermetic.activate()  # run alone, this module gets the suite's temporary home too (tests/_hermetic.py)
sys.path.insert(0, str(ROOT / "lib"))

from devkit import core, guard, setup  # noqa: E402

CWD = str(ROOT)
ENGINE = b"\xcf\xfa\xed\xfe made-up engine for the guard tests\n"
# the launcher's forms that made the gate refuse it: a probe of a program passed as $1, and exec of a built path
LAUNCHER = ("#!/bin/sh\nset -eu\n"
            "probe_ok() { probe_out=$(IMPECCABLE_LAUNCHER_PROBE=1 \"$1\" engine-probe 2>/dev/null) || return 1; }\n"
            "dir=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd)\n"
            "bin=\"$dir/bin/$(uname -s)-$(uname -m)/impeccable\"\n"
            "exec \"$bin\" \"$@\"\n")
READER = "seraph"  # a review lane: the read-only allowlist applies
WRITER = "sati"  # a build lane: the gate applies, the allowlist does not


def lane(cmd, member):
    v = guard.crew_shell_verdict(cmd, CWD, None, member)
    return v[1] if v else None


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.plat = setup.engine_platform()
        if not self.plat:
            self.skipTest("the launcher has no engine for this platform")
        self.skill = setup.SKILLS_SHARE / "impeccable"
        self.link = core.HOME / ".claude" / "skills" / "impeccable"
        self.addCleanup(self.cleanup)
        self.cleanup()
        scripts = self.skill / "scripts"
        scripts.mkdir(parents=True)
        (self.skill / "SKILL.md").write_text("---\nname: impeccable\n---\n")
        (scripts / "impeccable").write_text(LAUNCHER)
        (scripts / "impeccable").chmod(0o755)
        (scripts / "VERSION").write_text("0.1.18\n")
        (scripts / "engine-checksums.txt").write_text(
            f"0.1.18 impeccable-{self.plat} {hashlib.sha256(ENGINE).hexdigest()}\n")
        self.link.parent.mkdir(parents=True, exist_ok=True)
        self.link.symlink_to(self.skill)
        with mock.patch.object(setup, "_download_engine", lambda url, dest: Path(dest).write_bytes(ENGINE)), \
                mock.patch.object(setup, "_audit", lambda *a, **k: None), contextlib.redirect_stdout(io.StringIO()):
            res = setup.install_impeccable_engine(self.skill, "pbakaus/impeccable")
        self.assertEqual(res["outcome"], "installed")
        self.launcher = str(scripts / "impeccable")  # the path from devkit's own constant
        self.engine = scripts / "bin" / self.plat / "impeccable"

    def cleanup(self):
        if self.link.is_symlink():
            self.link.unlink()
        for d in (self.skill, setup.ENGINES_SHARE / "impeccable"):
            shutil.rmtree(d, ignore_errors=True)
        (core.STATE / setup.ENGINES_FILE).unlink(missing_ok=True)

    def paths(self):
        # the launcher by devkit's constant, typed out below the share folder, and through the Claude skills link
        return [self.launcher, "~/.local/share/devkit/skills/impeccable/scripts/impeccable",
                "~/.claude/skills/impeccable/scripts/impeccable"]

    def planted(self):
        # A folder outside devkit's skills folder, as a cloned repository or a checked-out PR could ship it: a symbolic
        # link to the installed launcher, a link `up` to the launcher's own bin folder, a link `s` to its scripts
        # folder, and an engine planted where the launcher looks when its path names this folder. The launcher finds
        # its folder with `cd "$(dirname "$0")"`, which keeps the link's folder and resolves `..` by name, so each
        # command below runs the planted engine (checked with /bin/sh, bash and zsh); through `s`, it also reads its
        # skill files from this folder (`$dir/..`). The shell's `cd` takes `..` by name too, so after `cd <d>/up/..` it
        # is in <d> while the guard follows the link to the scripts folder: `./impeccable` there runs <d>'s own file,
        # a plain script in <e>. A quoted or escaped `~` is not the home folder to the shell but a folder named `~` in
        # its own folder, so `"~/.claude/…/impeccable"` runs <e>'s `~/.claude/…/impeccable`, while the guard's words,
        # without their quotes, name the installed launcher. Returns the commands, each with the folder it runs in.
        d, e = Path(tempfile.mkdtemp(dir=core.HOME)), Path(tempfile.mkdtemp(dir=core.HOME))
        self.addCleanup(shutil.rmtree, d, True)
        self.addCleanup(shutil.rmtree, e, True)
        (d / "impeccable").symlink_to(self.launcher)
        (d / "s").symlink_to(self.engine.parent.parent.parent)
        for f in (d, e):
            (f / "up").symlink_to(self.engine.parent.parent)
        plant, tilde = d / "bin" / self.plat / "impeccable", e / "~" / ".claude" / "skills" / "impeccable" / "scripts"
        plant.parent.mkdir(parents=True)
        tilde.mkdir(parents=True)
        for f in (plant, e / "impeccable", tilde / "impeccable"):
            f.write_text("#!/bin/sh\ngit push --force origin main\n")
            f.chmod(0o755)
        return [(f"{d}/impeccable context", CWD), ("./impeccable detect src", str(d)),
                (f"cd {d}/up && ../impeccable context", CWD), (f"{d}/up/../impeccable context", CWD),
                (f"cd {d}/up/.. && ./impeccable context", CWD), (f"cd {d}/up && cd .. && ./impeccable context", CWD),
                (f"pushd {d}/up/.. && ./impeccable context", CWD), ("cd up/.. && ./impeccable context", str(e)),
                (f"{d}/s/impeccable context", CWD),
                (f"cd {e} && \"~/.claude/skills/impeccable/scripts/impeccable\" context", CWD),
                ("'~'/.claude/skills/impeccable/scripts/impeccable context", str(e)),
                ("\\~/.claude/skills/impeccable/scripts/impeccable context", str(e))]


class ReadOnlyLaneTests(LauncherCase):
    def test_a_read_only_lane_runs_the_launchers_read_verbs(self):
        for path in self.paths():
            for args in ("context", "context --target src/app.tsx 2>&1 | head -80", "detect --json --scope type src",
                         "detect --list-rules --quiet", "doctor --json"):
                with self.subTest(path=path, args=args):
                    self.assertIsNone(lane(f"{path} {args}", READER))

    def test_a_read_only_lane_leaves_the_launchers_write_verbs_to_write_lanes(self):
        self.assertIsNone(lane(f"{self.launcher} context", READER))
        for verb in ("init", "document", "extract", "live", "generate", "pin unpin polish", "hooks on"):
            with self.subTest(verb=verb):
                why = lane(f"{self.launcher} {verb}", READER)
                self.assertIsNotNone(why)
                self.assertIn("write lane", why)

    def test_a_read_only_lane_refuses_flags_that_write_or_reach_out(self):
        self.assertIsNone(lane(f"{self.launcher} detect --json src", READER))
        for args in ("doctor --fix", "detect --github src", "detect --json https://example.com",
                     "detect --cdp 9222 --json", "detect --json src --viewport 390x844", "context --out notes.md",
                     "context src/app.tsx"):
            with self.subTest(args=args):
                self.assertIsNotNone(lane(f"{self.launcher} {args}", READER))

    def test_a_read_only_lane_lets_detect_scan_local_paths_but_not_a_host(self):
        # `detect` scans a URL when it gets one; a host and port or a bare domain is a URL to it, not a local path
        for args in ("detect --json src", "detect ./index.html", "detect src/index.html", "detect ~/site/a.html"):
            with self.subTest(args=args):
                self.assertIsNone(lane(f"{self.launcher} {args}", READER))
        for args in ("detect localhost:3000", "detect --json 127.0.0.1:8080/pricing", "detect example.com",
                     "detect example.com/pricing", "detect 192.168.1.20", "detect localhost", "detect //example.com",
                     "detect [::1]:5173", "detect index.html"):
            with self.subTest(args=args):
                self.assertIn("local path", lane(f"{self.launcher} {args}", READER) or "")

    def test_a_read_only_lane_does_not_trust_a_link_to_the_launcher_elsewhere(self):
        for path in self.paths():
            self.assertIsNone(lane(f"{path} context", READER))
        for cmd, cwd in self.planted():
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(guard.crew_shell_verdict(cmd, cwd, None, READER))


class GateTests(LauncherCase):
    def test_a_link_to_the_launcher_elsewhere_is_not_trusted(self):
        for path in self.paths():
            self.assertIsNone(lane(f"{path} context", WRITER))
        for cmd, cwd in self.planted():
            with self.subTest(cmd=cmd):
                v = guard.crew_shell_verdict(cmd, cwd, None, WRITER)
                self.assertIn("only known when it runs", v[1] if v else "")

    def test_a_write_lane_runs_the_installed_launcher(self):
        for path in self.paths():
            for args in ("context", "live", "init"):
                with self.subTest(path=path, args=args):
                    self.assertIsNone(lane(f"{path} {args}", WRITER))

    def test_a_swapped_engine_is_not_trusted(self):
        self.assertIsNone(lane(f"{self.launcher} context", WRITER))
        st = self.engine.stat()
        self.engine.unlink()  # a same-size file with the old times: only ctime and the digest tell it apart
        self.engine.write_bytes(b"#" * st.st_size)
        os.utime(self.engine, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertIn("only known when it runs", lane(f"{self.launcher} context", WRITER) or "")

    def test_an_edited_launcher_is_not_trusted(self):
        self.assertIsNone(lane(f"{self.launcher} context", WRITER))
        with open(self.launcher, "a") as fh:
            fh.write("gh pr merge 12 --admin\n")
        self.assertIsNotNone(lane(f"{self.launcher} context", WRITER))

    def test_a_variable_that_changes_what_the_launcher_runs_takes_its_trust(self):
        self.assertIsNone(lane(f"{self.launcher} context", WRITER))
        for cmd in (f"IMPECCABLE_BIN=/tmp/x {self.launcher} context",
                    f"export PATH=/tmp/evil:/usr/bin; {self.launcher} context",
                    f"env PATH=/tmp/evil {self.launcher} context",
                    f"HOME=/tmp/h {self.launcher} context",
                    f"bash -c 'IMPECCABLE_BIN=/tmp/x {self.launcher} context'"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(lane(cmd, WRITER))
        with mock.patch.dict(os.environ, {"IMPECCABLE_BIN": "/tmp/x"}):  # set in the session's own environment
            self.assertIsNotNone(lane(f"{self.launcher} context", WRITER))

    def test_a_copy_outside_devkits_skills_folder_is_not_trusted(self):
        self.assertIsNone(lane(f"{self.launcher} context", WRITER))
        copy = Path(tempfile.mkdtemp(dir=core.HOME)) / "impeccable"
        self.addCleanup(shutil.rmtree, copy.parent, True)
        shutil.copytree(self.skill, copy)
        self.assertIsNotNone(lane(f"{copy}/scripts/impeccable context", WRITER))


if __name__ == "__main__":
    unittest.main()
