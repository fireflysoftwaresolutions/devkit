# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# impeccable in crew lanes (lib/devkit/guard.py, _ro_impeccable). Crew members run impeccable's engine with
# `dev impeccable <verb>` (lib/devkit/impeccable.py), which takes the engine from devkit's own skills folder and checks
# its digest itself. The skill's launcher typed by its path is judged like any other script: the shell can reach
# another file than the one the guard reads by the same words (a link, `..` after `cd`, a quoted `~`, a glob, a folder
# moved earlier in the same command; the Code reviewer's reviews of 11fd55e, 452dd51 and 1e606b7), so no typed path to
# it is trusted, in either kind of lane. Read-only lanes run `dev impeccable` with its read verbs and reading flags on
# local paths only; write lanes run every verb. The skill, a made-up engine and the install record are installed in the
# run's temporary home with the download faked; nothing runs the launcher or the engine.
# Run: python3 -m unittest tests.test_guard_impeccable -v
import contextlib
import hashlib
import io
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
BAD = "#!/bin/sh\ngit push --force origin main\n"
REL = ".claude/skills/impeccable/scripts/impeccable"  # the launcher below the home folder, through the skill's link
# impeccable's verbs that write the agents' settings, hooks or skill folders (the engine's help and its doctor's
# hints), in the forms dev passes on: the legacy `skills` namespace, a first `--`, and doctor's --fix
HARNESS = ("hooks on", "hooks off", "install", "install --user", "link ../impeccable", "update", "update --user",
           "pin audit", "unpin audit", "doctor --fix", "doctor --json --fix", "skills install", "skills update",
           "-- hooks on")
DEVELOPERS = "change what runs in every later session"  # the refusal's reason, in every path
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
        # the installed launcher by devkit's constant, typed out below the share folder, and through the Claude skills
        # link: each one does run the installed launcher, and still none is trusted
        return [self.launcher, "~/.local/share/devkit/skills/impeccable/scripts/impeccable", "~/" + REL]

    def planted(self):
        # Commands that run another file than the installed launcher the guard reads by the same words, as a cloned
        # repository or a checked-out PR could set them up (the Code reviewer's probes, checked with /bin/sh, bash and
        # zsh). The launcher finds its folder with `cd "$(dirname "$0")"`, which keeps a link's folder and takes `..`
        # by name, and so does the shell's `cd`: a link to the launcher, a link `up` to its bin folder, `s` to its
        # scripts folder. A quoted or escaped `~`, before or after the `~` ("~/…", ~""/…, ~\/…, $'~/…'), is a folder
        # named `~` in the shell's own folder, not the home folder. A glob (`?`, `[?]`, `*`) matches a planted folder
        # beside the skill's link. A link, a parent folder or ~/.claude/skills itself swapped earlier in the same
        # command makes the typed path reach a planted script after the guard read it. Returns the commands, each
        # with the folder it runs in.
        d, e, h = (Path(tempfile.mkdtemp(dir=core.HOME)) for _ in range(3))
        for f in (d, e, h):
            self.addCleanup(shutil.rmtree, f, True)
        (d / "impeccable").symlink_to(self.launcher)
        (d / "s").symlink_to(self.engine.parent.parent.parent)
        for name in ("?", "X"):
            (d / name).symlink_to(self.skill)
        for f in (d, e):
            (f / "up").symlink_to(self.engine.parent.parent)
        (h / "d").mkdir()
        (h / "d" / "X").symlink_to(self.skill)
        plant = d / "bin" / self.plat / "impeccable"
        tilde = e / "~" / ".claude" / "skills" / "impeccable" / "scripts"
        folders = (plant.parent, tilde, d / "0" / "scripts", d / "evil" / "scripts", h / "evil" / "scripts")
        for f in folders:
            f.mkdir(parents=True)
        for f in (plant, e / "impeccable", *(f / "impeccable" for f in folders[1:])):
            f.write_text(BAD)
            f.chmod(0o755)
        return [(f"{d}/impeccable context", CWD), ("./impeccable detect src", str(d)),
                (f"cd {d}/up && ../impeccable context", CWD), (f"{d}/up/../impeccable context", CWD),
                (f"cd {d}/up/.. && ./impeccable context", CWD), (f"cd {d}/up && cd .. && ./impeccable context", CWD),
                (f"pushd {d}/up/.. && ./impeccable context", CWD), ("cd up/.. && ./impeccable context", str(e)),
                (f"{d}/s/impeccable context", CWD),
                (f"cd {e} && \"~/{REL}\" context", CWD), (f"'~'/{REL} context", str(e)), (f"\\~/{REL} context", str(e)),
                (f"~\"\"/{REL} context", str(e)), (f"~''/{REL} context", str(e)), (f"~\\/{REL} context", str(e)),
                (f"~\"/{REL}\" context", str(e)), (f"$'~/{REL}' context", str(e)), (f"~/\"{REL}\" context", str(e)),
                (f"{d}/?/scripts/impeccable context", CWD), (f"{d}/[?]/scripts/impeccable context", CWD),
                (f"{d}/*/scripts/impeccable context", CWD),
                (f"ln -sfn {d}/evil {d}/X && {d}/X/scripts/impeccable context", CWD),
                (f"rm {d}/X; ln -s {d}/evil {d}/X; {d}/X/scripts/impeccable init", CWD),
                (f"mv {d}/evil {d}/X2 && mv -f {d}/X2 {d}/X; {d}/X/scripts/impeccable context", CWD),
                (f"mkdir -p {h}/n && ln -s {h}/evil {h}/n/X && mv {h}/d {h}/d.old && mv {h}/n {h}/d && "
                 f"{h}/d/X/scripts/impeccable init", CWD),
                (f"mkdir -p ~/.claude/n && ln -s {h}/evil ~/.claude/n/impeccable && mv ~/.claude/skills "
                 f"~/.claude/skills.old && mv ~/.claude/n ~/.claude/skills && ~/{REL} init", CWD)]


class ReadOnlyLaneTests(LauncherCase):
    def test_a_read_only_lane_runs_dev_impeccable_with_its_read_verbs(self):
        for args in ("context", "context --target src/x", "context --target=src/app.tsx 2>&1 | head -80",
                     "-- context", "detect --json --scope type src", "detect --list-rules --quiet", "doctor --json",
                     "doctor --target src"):
            with self.subTest(args=args):
                self.assertIsNone(lane(f"dev impeccable {args}", READER))

    def test_a_read_only_lane_leaves_impeccables_write_verbs_to_write_lanes(self):
        for verb in ("init", "document", "extract", "live", "generate", "polish", "ignores add x", ""):
            with self.subTest(verb=verb):
                self.assertIn("write lane", lane(f"dev impeccable {verb}".strip(), READER) or "")

    def test_a_read_only_lane_refuses_flags_that_write_or_reach_out(self):
        for args in ("detect --github src", "detect --json https://example.com",
                     "detect --cdp 9222 --json", "detect --json src --viewport 390x844", "context --out notes.md",
                     "context src/app.tsx", "context -- --out notes.md", "detect src -- --fix",
                     "context --target localhost:3000", "context --target=//example.com/app", "doctor --target example.com"):
            with self.subTest(args=args):
                self.assertIn("write lane", lane(f"dev impeccable {args}", READER) or "")

    def test_a_read_only_lane_lets_detect_scan_local_paths_but_not_a_host(self):
        # `detect` scans a URL when it gets one; a host and port or a bare domain is a URL to it, not a local path
        for args in ("detect --json src", "detect ./index.html", "detect src/index.html", "detect ~/site/a.html"):
            with self.subTest(args=args):
                self.assertIsNone(lane(f"dev impeccable {args}", READER))
        for args in ("detect localhost:3000", "detect --json 127.0.0.1:8080/pricing", "detect example.com",
                     "detect example.com/pricing", "detect 192.168.1.20", "detect localhost", "detect //example.com",
                     "detect [::1]:5173", "detect index.html"):
            with self.subTest(args=args):
                self.assertIn("local path", lane(f"dev impeccable {args}", READER) or "")

    def test_a_read_only_lane_refuses_the_launcher_typed_by_its_path(self):
        self.assertIsNone(lane("dev impeccable context", READER))
        for path in self.paths():
            with self.subTest(path=path):
                self.assertIsNotNone(lane(f"{path} context", READER))
        for cmd, cwd in self.planted():
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(guard.crew_shell_verdict(cmd, cwd, None, READER))


class GateTests(LauncherCase):
    def test_a_write_lane_refuses_the_launcher_typed_by_its_path(self):
        for args in ("context", "live", "init"):  # a write lane runs every verb through dev
            self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
        for path in self.paths():  # read as a script: `exec "$bin"` names its program only at run time
            with self.subTest(path=path):
                self.assertIn("only known when it runs", lane(f"{path} context", WRITER) or "")
        for cmd, cwd in self.planted():
            with self.subTest(cmd=cmd):
                self.assertEqual((guard.crew_shell_verdict(cmd, cwd, None, WRITER) or ("allow",))[0], "deny")


class DeveloperOnlyTests(LauncherCase):
    # what writes the agents' settings, hooks or skill folders changes every later session, so it is the developer's,
    # as `dev ext add` is: refused to the main session's agent and to both lane kinds, nested forms included
    def test_the_main_session_leaves_impeccables_harness_verbs_to_the_developer(self):
        self.assertIsNone(guard.check("Bash", {"command": "dev impeccable doctor --json"}, CWD, "claude"))
        for args in HARNESS:
            for cmd in (f"dev impeccable {args}", f"bash -c 'dev impeccable {args}'"):
                with self.subTest(cmd=cmd):
                    v = guard.check("Bash", {"command": cmd}, CWD, "claude")
                    self.assertIn(DEVELOPERS, v[1] if v else "")

    def test_a_read_only_lane_leaves_impeccables_harness_verbs_to_the_developer(self):
        self.assertIsNone(lane("dev impeccable doctor --json", READER))
        for args in HARNESS:
            with self.subTest(args=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}", READER) or "")

    def test_a_write_lane_leaves_impeccables_harness_verbs_to_the_developer(self):
        self.assertIsNone(lane("dev impeccable live", WRITER))
        for args in HARNESS:
            with self.subTest(args=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}", WRITER) or "")


if __name__ == "__main__":
    unittest.main()
