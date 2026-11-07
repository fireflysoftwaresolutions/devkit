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
# without --fix, with their reading flags, on local paths only, and the helper forms that write nothing; write lanes
# and the main session also `live`, `generate`, `document`, `extract`, `init` and the skill's helpers in the forms that
# stay inside the project or talk to the local live server (the tech lead's decision on the helper verbs, from a run of
# each helper of the 0.1.18 engine in a scratch project with the network closed). Every other verb, sub-verb or flag
# is the developer's. The skill, a made-up engine and the install record are installed in the run's temporary home
# with the download faked; nothing runs the launcher or the engine.
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
# a project checkout in the run's workspace, where agents run `dev impeccable` (the engine writes where --target and
# --file point, from the folder it runs in, so the guard judges those paths as writes)
OWNER = core.DEV / "acme-impeccable"
PROJECT = str(OWNER / "web")
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
               "document", "extract", "init", "live --target src/app", "live --target=.")
# the skill's helpers in the forms its reference files use (live.md, new-work.md, polish.md, region-map.md,
# critique.md), which write only inside the project or talk to the live server on 127.0.0.1: write lanes and the main
# session run them
HELPERS = ("live-poll", "live-poll --timeout=600000", "live-poll --reply e1 done --file src/index.html",
           "live-poll --reply e1 error 'Short reason'", "live-poll --reply e1 steer_done", "live-poll --reply e1 done "
           "--then-poll", "live-poll --reply e1 done --data '{\"status\":\"done\",\"failed\":[]}'",
           "live-accept --id e1 --variant 2 --page-url http://localhost:5173/", "live-accept --id e1 --discard",
           "live-status", "live-resume", "live-resume --id e1", "live-complete --id e1",
           "live-complete --id e1 --discarded",
           "live-wrap --id e1 --count 3 --element-id hero --classes hero,big --tag h1 --text 'Hello there' "
           "--file src/index.html", "live-insert --id e2 --count 3 --position after --element-id hero --tag h1",
           "live-server", "live-server --background", "live-server stop", "live-server stop --keep-inject",
           "surface-brief read src/index.html", "surface-brief write src/index.html /tmp/brief.md",
           "surface-brief write src/pricing.html brief.md src/a.html",
           "critique-storage slug src/index.html", "critique-storage latest src/index.html --json",
           "critique-storage trend src/index.html 5", "critique-storage write src/index.html /tmp/body.md",
           "critique-storage close src/index.html 2026-11-07T11-13-00Z__src-index-html.md",
           "comp-spec --comp comp.png --grid", "comp-spec --schema", "comp-spec --comp c.png --regions r.json",
           "comp-spec --comp c.png --regions r.json --inspect-map --json", "comp-spec --print", "comp-spec --crop art",
           "comp-spec --plate-prompt art --background transparent", "comp-spec --comp c.png --auto",
           "build-phase start --direction none --artifact src/index.html",
           "build-phase start --direction k1 --decision-comp .impeccable/mocks/a.png --artifact src/index.html",
           "build-phase advance", "build-phase scaffold", "build-phase record hero",
           "build-phase finish --disposition ship")
# the helper forms that write nothing (each run left the scratch project unchanged): read-only lanes run them too
READ_HELPERS = ("surface-brief read src/index.html", "critique-storage slug src/index.html",
                "critique-storage trend src/index.html 5")
# what the engine may run but no agent may: its hidden verbs (the Code reviewer's read of the 0.1.18 engine), its
# other visible ones, the design commands that are not engine verbs, a verb a newer engine could add, no verb, the
# allowlist's verbs spelled otherwise (another case, after `skills` or a second `--`, doctor with another word), and
# the helpers' sub-verbs and flags the skill does not use
DEVELOPER_ONLY = ("hook-admin", "hook", "hook-before-edit", "build-phase", "capture-server", "ignore x",
                  "ignores add x", "telemetry on", "check", "help", "polish", "live-inject --remove",
                  "live-generate --target src/App.jsx", "a-verb-a-newer-engine-adds", "", "Context", "DETECT src",
                  "skills context", "-- -- context", "doctor --FIX", "doctor --Fix=true", "doctor --target src --fix",
                  "doctor --target=--fix", "doctor -f", "Live-poll", "skills live-poll", "build-phase Advance",
                  "build-phase status", "build-phase restore-comp", "surface-brief", "surface-brief path src/x",
                  "critique-storage", "critique-storage Latest src/x", "live-server --host=0.0.0.0",
                  "live-poll -- --file src/x", "live-accept --id", "live-wrap --id -e1 --count 3 --tag h1",
                  "build-phase finish --disposition done", "live --open", "detect src -- --fix")
DEVELOPERS = "change what runs in every later session"  # the refusal's reason, in every path
WRITE_LANE = "a write lane runs it"  # a read-only lane's refusal of what a write lane may run
READER = "seraph"  # a review lane: the read-only allowlist applies
WRITER = "sati"  # a build lane: the gate applies, the allowlist does not


def lane(cmd, member, cwd=PROJECT, scope=None):
    v = guard.crew_shell_verdict(cmd, cwd, scope, member, base=PROJECT if scope else None)
    return v[1] if v else None


def main(cmd, cwd=PROJECT):
    # the main session's verdict on a shell command: None, or (decision, why)
    return guard.check("Bash", {"command": cmd}, cwd, "claude")


class LauncherCase(unittest.TestCase):
    def setUp(self):
        self.plat = setup.engine_platform()
        if not self.plat:
            self.skipTest("the launcher has no engine for this platform")
        self.skill = setup.SKILLS_SHARE / "impeccable"
        self.link = core.HOME / ".claude" / "skills" / "impeccable"
        self.addCleanup(self.cleanup)
        self.cleanup()
        Path(PROJECT, "src").mkdir(parents=True)
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
        for d in (self.skill, setup.ENGINES_SHARE / "impeccable", OWNER):
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
        for args in ("detect --github src", "detect --json http://localhost:5173/",
                     "detect --cdp 9222 --json", "detect --json src --viewport 390x844", "doctor --target example.com"):
            with self.subTest(args=args):
                v = lane(f"dev impeccable {args}", READER) or ""
                self.assertIn(WRITE_LANE, v)
                self.assertNotIn(DEVELOPERS, v)
        # context takes only --target, a path in the project, in every lane: any other word is the developer's
        for args in ("context --out notes.md", "context src/app.tsx", "context -- --out notes.md",
                     "context --target localhost:3000", "context --target=//example.com/app"):
            with self.subTest(args=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}", READER) or "")

    def test_a_read_only_lane_lets_detect_scan_local_paths_but_not_a_host(self):
        # `detect` scans a URL when it gets one, so a read-only lane gives it only a path shape: one that starts ./,
        # ../, / or ~/ (or is . or ..), or whose first part has only letters, digits, _ and - and is not a host on its
        # own. A domain (with a final dot too), an address in any notation, a user@host, a query or a fragment, a
        # leading space and a bare file name are refused; a host and port, a URL or another word with a colon that is
        # not a URL on this Mac, and a word the shell expands (a glob such as `?`), is the developer's in every lane.
        for args in ("detect --json src", "detect ./index.html", "detect src/index.html", "detect ~/site/a.html",
                     "detect ../site/a.html", "detect /tmp/site/a.html", "detect .", "detect --scope type,layout src",
                     "context --target src/app.tsx", "doctor --target src"):
            with self.subTest(args=args):
                self.assertIsNone(lane(f"dev impeccable {args}", READER))
        for args in ("detect example.com", "detect example.com/pricing", "detect 192.168.1.20", "detect localhost",
                     "detect index.html", "detect localhost.", "detect example.com.", "detect user@example.com",
                     "detect ' example.com'", "detect 2130706433", "detect 0x7f000001",
                     "detect LOCALHOST/app", "detect src#top", "detect '~user/x'", "detect ''",
                     "context --target example.com.", "doctor --target 2130706433", "detect http://localhost:5173/"):
            with self.subTest(args=args):
                self.assertIn("local path", lane(f"dev impeccable {args}", READER) or "")
        for args in ("detect localhost:3000", "detect --json 127.0.0.1:8080/pricing", "detect //example.com",
                     "detect [::1]:5173", "detect --json https://example.com", "detect 'example.com?x'"):
            with self.subTest(args=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}", READER) or "")

    def test_a_read_only_lane_runs_only_the_helper_forms_that_write_nothing(self):
        # surface-brief read, critique-storage slug and trend left the scratch project unchanged; critique-storage
        # latest closes the snapshot it reads once its file changed, so it is a write lane's, like the other helpers
        for args in READ_HELPERS:
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", READER))
        for args in HELPERS:
            if args in READ_HELPERS:
                continue
            with self.subTest(write=args):
                v = lane(f"dev impeccable {args}", READER) or ""
                self.assertIn(WRITE_LANE, v)
                self.assertNotIn(DEVELOPERS, v)

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
        for args in ("context", "live", "init"):  # a write lane runs the allowlist's verbs through dev
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
        self.assertIsNone(main("dev impeccable doctor --json"))
        for args in HARNESS:
            for cmd in (f"dev impeccable {args}", f"bash -c 'dev impeccable {args}'"):
                with self.subTest(cmd=cmd):
                    v = main(cmd)
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
        for args in AGENT_VERBS + HELPERS:
            with self.subTest(allowed=args):
                self.assertIsNone(main(f"dev impeccable {args}"))
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                v = main(f"dev impeccable {args}".strip())
                self.assertIn(DEVELOPERS, v[1] if v else "")

    def test_a_write_lane_runs_only_impeccables_allowlisted_verbs(self):
        for args in AGENT_VERBS + HELPERS:
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}".strip(), WRITER) or "")

    def test_a_read_only_lane_leaves_write_verbs_to_write_lanes_and_the_rest_to_the_developer(self):
        for args in ("init", "document", "extract", "live", "generate", "live --target src"):
            with self.subTest(write=args):
                v = lane(f"dev impeccable {args}", READER) or ""
                self.assertIn(WRITE_LANE, v)
                self.assertNotIn(DEVELOPERS, v)
        for args in DEVELOPER_ONLY:
            with self.subTest(refused=args):
                self.assertIn(DEVELOPERS, lane(f"dev impeccable {args}".strip(), READER) or "")

    def refused_everywhere(self, args, why):
        # the refusal of `dev impeccable <args>` in the main session and in both lane kinds names the developer and
        # gives `why`
        for where, v in (("main", (main(f"dev impeccable {args}") or (None, ""))[1]),
                         ("write lane", lane(f"dev impeccable {args}", WRITER) or ""),
                         ("read-only lane", lane(f"dev impeccable {args}", READER) or "")):
            with self.subTest(args=args, where=where):
                self.assertIn(DEVELOPERS, v)
                self.assertIn(why, v)

    def test_helper_forms_that_skip_the_skills_gates_are_the_developers(self):
        # build-phase advance --force passes a phase over its open gate; start --comp records any image as the
        # approved comp and marks the comps phase skipped; start --reset throws away the build's gates; live-complete
        # --force completes a session whose source still has live-mode leftovers. The plain forms keep the gates.
        for args in ("build-phase advance --force --reason 'the user said ship it'",
                     "build-phase start --comp comp.png --artifact src/index.html",
                     "build-phase start --direction k1 --artifact src/index.html --reset",
                     "live-complete --id e1 --force"):
            self.refused_everywhere(args, "skips a gate of the skill")

    def test_paths_where_the_engine_writes_stay_inside_the_project(self):
        # live --target ../other-app/index.html injected its script into that file and wrote .impeccable/live there;
        # live-wrap --file ../outside.html wrapped an element there; comp-spec --out, --out-dir and --spec wrote
        # there too. --target and --file take a path inside the project, and comp-spec's output flags are the
        # developer's.
        for args in ("live --target src/app/index.html", "live-wrap --id e1 --count 3 --tag h1 --file src/x.html",
                     "live-poll --reply e1 done --file=src/x.html"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
        for args in ("live --target ../other-app/index.html", "live --target=/tmp/app", "live --target ~/app",
                     "live --target src/../../x", "live --target ''", "live --target C:app",
                     "live-wrap --id e1 --count 3 --tag h1 --file ../outside.html",
                     "live-insert --id e1 --count 3 --position after --tag h1 --file=/tmp/x.html",
                     "live-poll --reply e1 done --file ../x.html", "comp-spec --crop art --out ../x.png",
                     "comp-spec --comp c.png --regions r.json --inspect-map --out-dir ../x",
                     "comp-spec --comp c.png --regions r.json --spec ../s.json"):
            self.refused_everywhere(args, "outside the project")

    def test_agents_detect_urls_only_on_this_mac(self):
        # detect loads a URL in Chrome, so a URL off this Mac reaches the network: agents give it local paths, URLs
        # on localhost or 127.0.0.1, and --cdp a port or such a URL
        for args in ("detect http://localhost:5173/", "detect --json https://127.0.0.1:8443/a#c",
                     "detect HTTP://localhost/app", "detect --cdp 9222", "detect --cdp ws://127.0.0.1:9222/devtools/x",
                     "detect --cdp=http://localhost:9222", "detect index.html"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
                self.assertIsNone(main(f"dev impeccable {args}"))
        for args in ("detect https://example.com", "detect --json http://localhost.evil.com/",
                     "detect http://localhost@evil.com/", "detect http://localhost:80@evil.com/",
                     "detect 'http://127.0.0.1\\@evil.com'", "detect http://10.0.0.2:3000/",
                     "detect file:///etc/passwd", "detect localhost:3000", "detect //example.com/x", "detect --cdp ws://10.0.0.2:9222",
                     "detect --cdp=example.com", "detect --cdp 9222x"):
            self.refused_everywhere(args, "leaves this Mac")

    def test_helpers_that_reach_the_internet_or_cost_money_are_the_developers(self):
        # concept-seed GETs impeccable.style/api/roll and writes ~/.impeccable/seed-salt; generate-image calls the
        # OpenAI API with the developer's key
        self.refused_everywhere("concept-seed --scope direction --mode operate", "impeccable.style")
        self.refused_everywhere("generate-image --ref a.png --prompt-file p.txt --out b.png", "OpenAI")

    def test_the_design_commands_take_no_words_and_context_only_a_project_path(self):
        # generate, document, extract and init run in the agent's chat in 0.1.18, so they take no words; context takes
        # only --target, a path in the project (the tech lead's decision on the Code reviewer's review of b97db94: any
        # word reached the engine)
        for args in ("generate", "document", "extract", "init", "context", "context --target src/app.tsx"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
                self.assertIsNone(main(f"dev impeccable {args}"))
        for args in ("generate --anything https://x", "init --force /etc", "document --out /tmp/x", "extract src",
                     "context src/app.tsx", "context --out notes.md", "context --target src --json"):
            self.refused_everywhere(args, "")
        for args in ("context --target /etc", "context --target ../x", "context --target=~/x"):
            self.refused_everywhere(args, "outside the project")

    def test_ids_and_crops_are_names_and_brief_and_snapshot_files_are_project_paths(self):
        # the engine may build a file path from --id, --session-id and --crop, from the file name `critique-storage
        # close` gets, and from the target `surface-brief write` gets: a name takes letters, digits, `.`, `_` and `-`
        # (never `.` or `..` alone), and the two files are paths in the project
        for args in ("build-phase start --direction k1 --session-id s-1.2_a", "live-complete --id e1",
                     "live-resume --id e1", "comp-spec --crop hero.1",
                     "critique-storage close src/x.html 2026-11-07T11-13-00Z__src-x-html.md",
                     "surface-brief write src/x.html /tmp/brief.md src/y.html"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
                self.assertIsNone(main(f"dev impeccable {args}"))
        for args in ("build-phase start --direction k1 --session-id ../../../tmp/x", "live-complete --id ../../x",
                     "live-resume --id /tmp/x", "live-accept --id .. --discard", "live-wrap --id . --count 3 --tag h1",
                     "comp-spec --crop ../../x --comp c.png", "comp-spec --crop=a/b", "live-complete --id 'e 1'"):
            self.refused_everywhere(args, "not a name")
        for args in ("critique-storage close src/x ../../../../tmp/x.md", "critique-storage close src/x /tmp/x.md",
                     "surface-brief write ../../../x /tmp/b.md", "surface-brief write /etc/x b.md",
                     "surface-brief write https://example.com/pricing brief.md"):
            self.refused_everywhere(args, "outside the project")

    def test_free_words_that_name_a_file_are_project_paths(self):
        # The engine's usage and the skill's reference files name a file with these words: the entry file
        # build-phase start hashes (--artifact), the capture record hero reads (--build), the decision comp in
        # .impeccable/mocks/decision (--decision-comp), the comp image and regions file comp-spec measures (--comp,
        # --regions), and the target every surface-brief and critique-storage form takes, the related targets too,
        # which the engine fingerprints or names its own files after. Each takes a path in the project, so a URL or a
        # route as the key is the developer's (the tech lead's decision). The body file of a write and trend's count
        # stay free words: the skill writes the body to a temp file, which the engine only reads.
        for args in ("build-phase start --direction k1 --artifact src/index.html",
                     "build-phase start --direction k1 --decision-comp .impeccable/mocks/decision/a.webp",
                     "build-phase record hero --build .impeccable/review/hero-repro.png",
                     "comp-spec --comp .impeccable/mocks/c.png --regions regions.json",
                     "critique-storage write src/x.html /tmp/body.md", "critique-storage latest src/x.html --json",
                     "critique-storage close src/x.html snap.md",
                     "surface-brief write src/x.html /tmp/b.md src/y.html"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
                self.assertIsNone(main(f"dev impeccable {args}"))
        for args in ("surface-brief read src/x.html", "critique-storage slug src/x.html",
                     "critique-storage trend src/x.html 5"):
            with self.subTest(read_only=args):
                self.assertIsNone(lane(f"dev impeccable {args}", READER))
        for args in ("build-phase start --direction k1 --artifact ../other-app/index.html",
                     "build-phase start --direction=k1 --artifact=/tmp/x.html",
                     "build-phase record hero --build /tmp/hero.png", "build-phase record hero --build=~/hero.png",
                     "critique-storage write ../x.html /tmp/body.md", "critique-storage write /etc/x body.md",
                     "surface-brief read ../../x.html", "surface-brief read https://example.com/pricing",
                     "surface-brief read route:/pricing", "critique-storage trend /etc/x 5",
                     "critique-storage trend http://localhost:5173/ 5", "critique-storage slug ~/x.html",
                     "critique-storage latest ../x.html --json", "critique-storage close ../x.html snap.md",
                     "surface-brief write src/x.html /tmp/b.md ../y.html",
                     "surface-brief write src/x.html b.md src/y.html /etc/z",
                     "build-phase start --direction k1 --decision-comp ~/a.webp",
                     "comp-spec --comp /tmp/c.png --grid", "comp-spec --comp c.png --regions ../r.json"):
            self.refused_everywhere(args, "outside the project")
        self.refused_everywhere("build-phase start --direction k1 --artifact 'src/*.html'", "the shell expands")

    def test_values_the_shell_expands_are_the_developers(self):
        # the guard reads the words before the shell expands them and `dev impeccable` checks them after, so a path, a
        # URL or a port with $, `, {, *, ? or [ could reach the engine as another value than the one the guard read
        for args in ("live --target $HOME/x", "live --target \"$(echo /etc)\"", "live --target `pwd`/x",
                     "live --target src/*", "live --target {/etc/x,src}", "live --target 'src/[ab].html'",
                     "live-wrap --id e1 --count 3 --tag h1 --file 'src/?.html'", "detect $U",
                     "detect 'http://localhost:5173/?a=1'", "detect --cdp '9222*'", "live-server --port $P",
                     "critique-storage close src/x.html '*.md'", "surface-brief write '$HOME/x' b.md"):
            self.refused_everywhere(args, "the shell expands")
        for cmd in ("X=/etc; dev impeccable live --target $X", "U=https://example.com/x; dev impeccable detect $U"):
            with self.subTest(cmd=cmd):
                self.assertIn(DEVELOPERS, lane(cmd, WRITER) or "")
                self.assertIn(DEVELOPERS, (main(cmd) or (None, ""))[1])

    def test_the_refusal_gives_read_only_lanes_detect_on_local_paths_only(self):
        # a read-only lane gives detect local path shapes only (no URL, not even on this Mac), as the code does
        reason = guard.impeccable_refusal(["hooks", "on"])
        every, writes = reason.split("in write lanes and the main session also", 1)
        self.assertNotIn("localhost", every)
        self.assertIn("localhost", writes)

    def test_build_phase_start_needs_a_direction(self):
        # the skill starts a build with --direction (and --decision-comp), which keeps the comps gate open
        for args in ("build-phase start --direction none", "build-phase start --artifact src/x.html --direction=k1"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
        for args in ("build-phase start", "build-phase start --artifact src/index.html",
                     "build-phase start --decision-comp a.png --artifact src/x.html"):
            self.refused_everywhere(args, "needs `--direction`")

    def test_the_live_servers_port_is_digits_only(self):
        # the live server listens on 127.0.0.1; a host in --port could move it
        for args in ("live-server --port 8400", "live-server --port=8400 --background"):
            with self.subTest(allowed=args):
                self.assertIsNone(lane(f"dev impeccable {args}", WRITER))
        for args in ("live-server --port 0.0.0.0:80", "live-server --port=localhost", "live-server --port 80x",
                     "live-server --port http://localhost:8400"):
            self.refused_everywhere(args, "port number")


class EngineFolderTests(LauncherCase):
    # The engine writes where --target and --file point, counted from the folder it runs in, so the guard judges those
    # values as writes from the folder the shell is in after `cd`, `pushd` or `env -C`, as it judges any other write
    # (the Code reviewer's finding on d9186f1 and b97db94: `cd ~/.claude && dev impeccable live --target
    # settings.json` passed in the main session and in a write lane, and the same write by `echo >>` did not). A
    # write lane refuses them outside the workspace, in the handoff repository and outside its step's scope; the main
    # session asks first, as for any write outside the workspace. `dev impeccable` checks the folder again when it runs.
    def outside(self):
        # folders outside the workspace, computed from the run's home: the home, an agent's settings, the handoff
        # repository and a folder that is neither temporary nor an agent's
        return [str(core.HOME), str(core.HOME / ".claude"), str(core.HANDOFF), str(core.HOME / "Library")]

    def forms(self, folder):
        # the engine's write flags behind each way the shell changes folder before it runs dev
        return (f"cd {folder} && dev impeccable live --target .zshrc",
                f"cd {folder}; dev impeccable live-wrap --id e1 --count 3 --tag h1 --file settings.json",
                f"(cd {folder} && dev impeccable live --target index.html)",
                f"env -C {folder} dev impeccable live --target=index.html",
                f"pushd {folder} && dev impeccable live-insert --id e1 --count 1 --position after --tag p --file x.html",
                f"bash -c 'cd {folder} && dev impeccable live-poll --reply e1 done --file index.html'")

    def test_a_write_lane_refuses_engine_paths_outside_the_workspace(self):
        for folder in self.outside():
            for cmd in self.forms(folder):
                with self.subTest(cmd=cmd):
                    self.assertIn("outside the workspace", lane(cmd, WRITER) or "")
        for cmd in (f"cd {PROJECT}/src && dev impeccable live --target index.html",
                    f"env -C {PROJECT} dev impeccable live-wrap --id e1 --count 3 --tag h1 --file src/x.html"):
            with self.subTest(allowed=cmd):
                self.assertIsNone(lane(cmd, WRITER, cwd=str(core.HOME)))

    def test_the_main_session_asks_before_engine_paths_outside_the_workspace(self):
        lib = core.HOME / "Library"
        for cmd in (f"cd {lib} && dev impeccable live --target index.html",
                    f"cd {lib}; dev impeccable live-wrap --id e1 --count 3 --tag h1 --file x/y.html",
                    f"cd {core.HOME} && dev impeccable live-poll --reply e1 done --file index.html",
                    f"pushd {lib} && dev impeccable live --target=index.html",
                    f"env -C {lib} dev impeccable live --target index.html"):
            with self.subTest(cmd=cmd):
                v = main(cmd)
                self.assertEqual(v and v[0], "ask")
                self.assertIn("Writes outside", v[1])
        self.assertIsNone(main(f"cd {PROJECT}/src && dev impeccable live --target index.html", cwd=str(lib)))

    def test_a_write_lane_keeps_engine_paths_to_its_steps_scope(self):
        scope = ["src/**"]
        for cmd in ("dev impeccable live --target src/index.html",
                    "cd src && dev impeccable live-wrap --id e1 --count 3 --tag h1 --file x.html"):
            with self.subTest(allowed=cmd):
                self.assertIsNone(lane(cmd, WRITER, scope=scope))
        for cmd in ("dev impeccable live --target README.md", "dev impeccable live --target=docs/x.html",
                    "dev impeccable live-wrap --id e1 --count 3 --tag h1 --file docs/x.html",
                    "cd docs && dev impeccable live --target index.html"):
            with self.subTest(refused=cmd):
                self.assertIn("scope", lane(cmd, WRITER, scope=scope) or "")


if __name__ == "__main__":
    unittest.main()
