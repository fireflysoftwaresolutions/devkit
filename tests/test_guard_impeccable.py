# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
#
# impeccable in crew lanes (lib/devkit/guard.py, _ro_impeccable). Crew members run impeccable's engine with
# `dev impeccable <verb>` (lib/devkit/impeccable.py), which takes the engine from devkit's own skills folder and checks
# its digest itself. The skill's launcher typed by its path is judged like any other script: the shell can reach
# another file than the one the guard reads by the same words (a link, `..` after `cd`, a quoted `~`, a glob, a folder
# moved earlier in the same command; the Code reviewer's reviews of 11fd55e, 452dd51 and 1e606b7), so no typed path to
# it is trusted, in either kind of lane. Agents run `dev impeccable` only with the verbs on its allowlist (the tech
# lead's decision on the Code reviewer's review of the wrapper): read-only lanes `context`, `detect` and `doctor`
# without --fix, with their reading flags, on local paths only; write lanes and the main session also `live`,
# `generate`, `document`, `extract` and `init`. Every other verb is the developer's. The skill, a made-up engine and
# the install record are installed in the run's temporary home with the download faked; nothing runs the launcher or
# the engine.
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
# hints), in the forms dev passes on: the legacy `skills` namespace, once or more, a first `--`, and doctor's --fix
HARNESS = ("hooks on", "hooks off", "install", "install --user", "link ../impeccable", "update", "update --user",
           "pin audit", "unpin audit", "doctor --fix", "doctor --json --fix", "skills install", "skills update",
           "-- hooks on", "skills skills hooks on")
# the verbs agents run (the allowlist), as the engine gets them from dev; the read verbs run in read-only lanes too
AGENT_VERBS = ("context", "-- context", "detect src", "doctor --json", "doctor --target src", "live", "generate",
               "document", "extract", "init")
# what the engine may run but no agent may: its hidden verbs (the Code reviewer's read of the 0.1.18 engine), its
# other visible ones, the design commands that are not engine verbs, a verb a newer engine could add, no verb, and the
# allowlist's verbs spelled otherwise (another case, after `skills` or a second `--`, doctor with another word)
DEVELOPER_ONLY = ("hook-admin", "hook", "hook-before-edit", "build-phase advance", "capture-server", "ignore x",
                  "ignores add x", "telemetry on", "check", "help", "polish", "live-poll", "a-verb-a-newer-engine-adds",
                  "", "Context", "DETECT src", "skills context", "-- -- context", "doctor --FIX", "doctor --Fix=true",
                  "doctor --target src --fix", "doctor --target=--fix", "doctor -f")
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

    def test_a_read_only_lane_refuses_flags_that_write_or_reach_out(self):
        for args in ("detect --github src", "detect --json https://example.com",
                     "detect --cdp 9222 --json", "detect --json src --viewport 390x844", "context --out notes.md",
                     "context src/app.tsx", "context -- --out notes.md", "detect src -- --fix",
                     "context --target localhost:3000", "context --target=//example.com/app", "doctor --target example.com"):
            with self.subTest(args=args):
                self.assertIn("write lane", lane(f"dev impeccable {args}", READER) or "")

    def test_a_read_only_lane_lets_detect_scan_local_paths_but_not_a_host(self):
        # `detect` scans a URL when it gets one, so a read-only lane gives it only a path shape: one that starts ./,
        # ../, / or ~/ (or is . or ..), or whose first part has only letters, digits, _ and - and is not a host on its
        # own. A host and port, a domain (with a final dot too), an address in any notation, a user@host, a query or
        # a fragment, a leading space and a bare file name are refused.
        for args in ("detect --json src", "detect ./index.html", "detect src/index.html", "detect ~/site/a.html",
                     "detect ../site/a.html", "detect /tmp/site/a.html", "detect .", "detect --scope type,layout src",
                     "context --target src/app.tsx", "doctor --target src"):
            with self.subTest(args=args):
                self.assertIsNone(lane(f"dev impeccable {args}", READER))
        for args in ("detect localhost:3000", "detect --json 127.0.0.1:8080/pricing", "detect example.com",
                     "detect example.com/pricing", "detect 192.168.1.20", "detect localhost", "detect //example.com",
                     "detect [::1]:5173", "detect index.html", "detect localhost.", "detect example.com.",
                     "detect user@example.com", "detect 'example.com?x'", "detect ' example.com'", "detect 2130706433",
                     "detect 0x7f000001", "detect LOCALHOST/app", "detect src#top", "detect '~user/x'", "detect ''",
                     "context --target example.com.", "doctor --target 2130706433"):
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
    # as `dev ext add` is: refused to the main session's agent and to both lane kinds, nested forms included. Agents
    # get an allowlist, not a block list: a hidden verb or one a newer engine adds is the developer's too.
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

    def test_the_main_session_runs_only_impeccables_allowlisted_verbs(self):
        for args in AGENT_VERBS:
            with self.subTest(allowed=args):
                self.assertIsNone(guard.check("Bash", {"command": f"dev impeccable {args}"}, CWD, "claude"))
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                v = guard.check("Bash", {"command": f"dev impeccable {args}".strip()}, CWD, "claude")
                self.assertIn(DEVELOPERS, v[1] if v else "")

    def test_a_write_lane_runs_only_impeccables_allowlisted_verbs(self):
        for args in AGENT_VERBS:
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}".strip(), WRITER) or "")

    def test_a_read_only_lane_leaves_write_verbs_to_write_lanes_and_the_rest_to_the_developer(self):
        for args in ("init", "document", "extract", "live", "generate", "live --target src"):
            with self.subTest(write=args):
                self.assertIn("write lane", lane(f"dev impeccable {args}", READER) or "")
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}".strip(), READER) or "")


if __name__ == "__main__":
    unittest.main()
