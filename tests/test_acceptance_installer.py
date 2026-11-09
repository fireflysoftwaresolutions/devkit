# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""Acceptance of the installer and `dev setup` end to end, where the module tests stop at a seam.

The module tests run the wizard in-process on a fake terminal, with each sign-in and each command faked. Here,
install.sh runs as `curl … | bash` runs it, in a pseudo-terminal of its own, against the stub Mac of test_install
(stub brew, gh, uname, sysctl; a local repository with release tags; a temporary HOME). The release it downloads has
a `bin/dev` that runs this checkout's real wizard on that terminal: real raw keys, the real runner of each step, and
each sign-in as a real command on the terminal device (stub `claude` and `codex` on PATH). Only what the wizard reads
about this Mac (its survey, the agents' status, the package list) is replaced, and `dev install` and `dev doctor` are
the release's own stand-ins, so nothing reaches Homebrew, the network, launchd, ~/.claude or ~/.codex.

The other tests run this branch's `dev install` pieces in a sandbox HOME: turning Codex off, and a Mac on the 26.11.8
layout moving to this release. Each test names the scenario or the acceptance criterion (INSTALL-AC-n) it proves.
"""
import contextlib
import fcntl
import io
import json
import os
import select
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(HERE))

from devkit import audit, cli, core, setup, tui, wizard  # noqa: E402
import test_install as inst  # noqa: E402  (the stub Mac install.sh runs on, and its terminal helpers)
import test_pinned_install as pinned  # noqa: E402  (the pinned-install sandbox)
import test_setup_flow as flow  # noqa: E402  (the removal's fixture homes)
import _sandbox  # noqa: E402  (the suite's net against this Mac's codex, claude, brew and gh)

DOWN, ENTER, CTRL_C = b"\x1b[B", b"\r", b"\x03"
REFUSAL = ("4f2c1ab is not verified: it is not a release, and its tests have not passed here. The hooks and the guard "
           "run only a verified copy.")

# The suite's net (tests/_sandbox.py) keeps this Mac's real agents, Homebrew and GitHub CLI out of reach: the run's
# stand-ins come first on PATH, and devkit keeps them first. Every process this module starts also goes through the
# net's check, even when the module runs without the suite's hermetic home: codex, claude, brew or gh from outside a
# temporary folder stops the test instead.
_REAL_POPEN = subprocess.Popen


def _sandboxed_popen(args, *a, **k):
    argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
    argv = ["/bin/sh", "-c", *argv] if k.get("shell") else argv  # what Popen runs
    why = _sandbox.refusal("subprocess.Popen", (k.get("executable"), argv, k.get("cwd"), k.get("env")))
    if why:
        raise AssertionError(f"a test tried to start {why}, outside its sandbox")
    return _REAL_POPEN(args, *a, **k)


def setUpModule():
    p = mock.patch("subprocess.Popen", _sandboxed_popen)
    p.start()
    unittest.addModuleCleanup(p.stop)

# The release's bin/dev. `setup` runs this checkout's wizard (REAL_LIB) with what it reads about the Mac replaced;
# `install` and `doctor` stand in for the real ones, which would set up the real agents. The wizard runs both as
# `<python> -I -S -u <this file> …`, so those two branches use the standard library alone.
DRIVER = r'''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
args = sys.argv[1:]
marks = os.environ["SCENARIO_STATE"]


def note(**rec):
    with open(os.environ["SCENARIO_LOG"], "a") as f:
        f.write(json.dumps(rec) + "\n")


if args[:1] == ["install"]:
    cfg = os.path.join(os.environ["HOME"], ".config", "devkit", "config.json")
    note(cmd="install", args=args, agents=json.load(open(cfg)).get("agents"))
    print("  v26.11.11 · install  ·  (c) 2026 Example Inc.")
    print("\u25b8 Pinned copy of devkit (what the hooks and the guard run)")
    if os.environ.get("SCENARIO_PIN") == "refused":
        print("  \u2717 " + os.environ["SCENARIO_REFUSAL"])
        sys.exit(1)
    print("  \u2713 pinned")
    sys.exit(0)
if args[:1] == ["doctor"]:
    note(cmd="doctor")
    print("  \u2713 58/58 checks passed")
    sys.exit(0)
if args[:1] == ["setup"]:
    note(cmd="setup", args=args)
    sys.path.insert(0, os.environ["REAL_LIB"])
    import shutil
    from pathlib import Path
    from unittest import mock
    path = os.environ["PATH"]
    from devkit import cli, core, providers, wizard
    # devkit puts /opt/homebrew/bin, /usr/local/bin and ~/.local/bin first on PATH when imported: this Mac's real
    # codex and brew would win over the stubs. Put the sandbox's PATH back, and stop if a tool is still reachable.
    os.environ["PATH"] = path
    stubs = os.environ["SCENARIO_STUBS"]
    for tool in ("codex", "claude", "brew", "gh"):
        found = shutil.which(tool) or ""
        if os.path.dirname(found) != stubs:
            sys.exit(f"refusing to run: {tool} resolves to {found or 'nothing'}, outside the stub folder")

    def status(include_account=True):
        out = {}
        for k, plan in (("claude", "max"), ("codex", "business")):
            on = os.path.exists(os.path.join(marks, k + "-signed-in"))
            out[k] = {"installed": True, "logged_in": on, "plan": plan if on else None}
        return out

    def survey():
        return {"cpus": 8, "ram": 16, "macos": "26.2", "arch": "arm64", "brew": True,
                "gh": {"login": "ada", "name": "Ada", "id": 1}, "providers": status(),
                "config": core.read_json(wizard.CONFIG_FILE, None),
                "seen": {"hosts": ["github"], "clouds": [], "evidence": {}},
                "stacks_found": {"counts": {"python": 2}, "repos": 2},
                "docker": {"colima": True, "running": True}}

    plan = {"groups": {"core"}, "brew": ['brew "jq"', 'brew "tree"'], "npm": [], "post": []}
    with mock.patch.object(wizard, "DEVKIT_HOME", Path(here)), mock.patch.object(wizard, "survey", survey), \
            mock.patch.object(providers, "status", status), \
            mock.patch.object(wizard, "package_status",
                              lambda groups: [("core", [("jq", "brew", False), ("tree", "brew", False)])]), \
            mock.patch.object(wizard._Wizard, "_package_plan", lambda self: dict(plan)), \
            mock.patch.object(wizard, "_npm_globals", lambda groups: []), \
            mock.patch.object(wizard, "detect_docker", lambda: {"colima": True, "running": True}):
        sys.exit(cli.main(args))
sys.exit(f"the scenario's dev has no {args[:1]}")
'''

# The agents' command-line tools. Each records how it was started: its stdin and stdout by their terminal name, and
# whether stderr is a terminal. `claude auth login` signs in; `codex login` waits like a browser page nobody finishes
# (exec, so that Ctrl+C reaches the waiting process itself).
CLAUDE = r'''#!/bin/sh
exec 3>&1
in=$(tty 2>/dev/null || echo none); out=$(tty <&3 2>/dev/null || echo none)
if [ -t 2 ]; then err=terminal; else err=none; fi
exec 3>&-
echo "claude $* stdin=$in stdout=$out stderr=$err" >> "$STUB_LOG"
case "$1 $2" in
  "auth login") echo "Opening claude.ai in your browser."; touch "$SCENARIO_STATE/claude-signed-in" ;;
esac
'''
CODEX = r'''#!/bin/sh
exec 3>&1
in=$(tty 2>/dev/null || echo none); out=$(tty <&3 2>/dev/null || echo none)
exec 3>&-
echo "codex $* stdin=$in stdout=$out" >> "$STUB_LOG"
case "$1" in
  login) echo "Starting local login server on http://localhost:1455."
         echo $$ > "$SCENARIO_STATE/codex.pid"
         exec sleep 60 ;;
esac
'''


class ScenarioMac(inst.Machine):
    """The stub Mac of test_install, with a release v26.11.11 whose bin/dev is the scenario's, and the agents'
    stub tools on PATH."""

    def __init__(self, root: Path):
        super().__init__(root)
        (self.remote / "bin" / "dev").write_text(DRIVER)
        (self.remote / "VERSION").write_text("26.11.11\n")
        inst.git(self.remote, "add", "-A")
        inst.git(self.remote, "commit", "-q", "-m", "26.11.11")
        inst.git(self.remote, "tag", "v26.11.11")
        for name, body in (("claude", CLAUDE), ("codex", CODEX)):
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        self.marks, self.scenario_log = root / "marks", root / "scenario.jsonl"
        self.marks.mkdir()
        self.signed_in.touch()  # GitHub: signed in already

    def env(self, **extra) -> dict:
        env = super().env(SCENARIO_STATE=str(self.marks), SCENARIO_LOG=str(self.scenario_log),
                          SCENARIO_STUBS=str(self.stubs),
                          SCENARIO_REFUSAL=REFUSAL, **extra)
        for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):  # a remote session would put the code sign-in first
            env.pop(k, None)
        return env

    def records(self, cmd=None) -> list:
        recs = [json.loads(x) for x in self.scenario_log.read_text().splitlines()] if self.scenario_log.exists() else []
        return [r for r in recs if cmd is None or r.get("cmd") == cmd]

    def state_file(self, name: str) -> Path:
        return self.home / ".local" / "state" / "devkit" / name

    def terminal(self, keys=(), cols=100, rows=50, limit=120, **env):
        """install.sh through a pipe, in a terminal of its own of `cols` x `rows` -> (exit code, the screen's text
        without escapes). `keys`: [(text, bytes)], each typed once its text shows after the previous key's."""
        import pty
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        proc = subprocess.Popen(["/bin/bash", "-s", "--"], stdin=subprocess.PIPE, stdout=slave, stderr=slave,
                                env=self.env(**env), cwd=str(self.root), start_new_session=True,
                                preexec_fn=inst._take_terminal)
        os.close(slave)
        proc.stdin.write(inst.INSTALL.read_bytes())
        proc.stdin.close()
        keys, out, seen, deadline = list(keys), b"", 0, time.monotonic() + limit
        try:
            while time.monotonic() < deadline:
                ready = select.select([master], [], [], 0.2)[0]
                try:
                    chunk = os.read(master, 65536) if ready else b""
                except OSError:  # the terminal closed: every process that had it has exited
                    break
                out += chunk
                screen = inst.ESCAPES.sub("", out.decode("utf-8", "replace"))
                if keys and keys[0][0] in screen[seen:]:
                    text, typed = keys.pop(0)
                    seen = screen.index(text, seen) + len(text)
                    os.write(master, typed)
                if not chunk and proc.poll() is not None:
                    break
        finally:
            os.close(master)
            if proc.poll() is None:
                inst.stop_session(proc)
        rc = proc.wait(10)
        return rc, inst.ESCAPES.sub("", out.decode("utf-8", "replace").replace("\r\n", "\n"))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@unittest.skipUnless(sys.platform == "darwin", "the installer and its terminal handling are for macOS")
class EmployeeFailurePathTests(unittest.TestCase):
    """The first install of a person whose pinned copy is refused: they sign in to Claude Code through the terminal,
    stop the Codex sign-in with Ctrl+C and skip it, and the setup goes on to its check and its summary."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = ScenarioMac(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_path(self):
        keys = [(inst.WELCOME, ENTER),
                ("How do you want to set up devkit?", ENTER),          # Recommended setup
                ("Which coding agents do you want to use?", ENTER),    # Claude Code and Codex
                ("Apply this plan?", ENTER),                           # Apply
                ("single sign-on", ENTER),                             # Claude Code's panel: the browser
                ("Sign in with a code", ENTER),                        # Codex's panel: the browser
                ("Starting local login server", CTRL_C),               # the person stops the Codex sign-in
                ("Reason: you stopped the sign-in.", DOWN + DOWN + ENTER),  # Skip for now
                ("What now?", CTRL_C)]                                 # Finish
        rc, out = self.m.terminal(keys, SCENARIO_PIN="refused")
        return rc, out, json.loads(self.m.state_file("setup-run.json").read_text())

    def test_a_refused_pin_ends_not_ready_with_its_reason_and_exit_code_1(self):
        """The pinned-copy step fails: "devkit is not ready yet" with the refusal as the reason, install.sh exits 1
        and adds nothing after the wizard's summary (INSTALL-AC-38, AC-42, AC-45)."""
        rc, out, run = self.run_path()
        self.assertEqual(rc, 1, out[-3000:])
        summary = out[out.rindex("devkit is not ready yet"):]
        self.assertTrue(summary.startswith("devkit is not ready yet. 1 step failed."), summary[:200])
        self.assertIn("4f2c1ab is not verified: it is not a release", summary)
        self.assertLess(summary.index("Needs attention"), summary.index("Done"))
        self.assertIn("Reason: 4f2c1ab is not verified: it is not a release", out)
        self.assertNotIn("v26.11.11 · install", out.split("Apply this plan?")[-1])  # never its banner as the reason
        self.assertNotIn("Summary", summary)
        self.assertNotIn("exit code", summary)
        steps = {s["id"]: s for s in run["steps"]}
        self.assertEqual(steps["configure"]["state"], "failed")
        self.assertIn("4f2c1ab is not verified", steps["configure"]["reason"])
        self.assertEqual([r["args"] for r in self.m.records("install")], [["install", "--commit-template", "yes"]])

    def test_claude_code_signs_in_on_the_terminal_device_from_a_piped_installer(self):
        """The sign-in command gets the terminal itself, by its own device name, on stdin and stdout, and a terminal
        on stderr, though install.sh arrived through a pipe (INSTALL-AC-22, R11)."""
        rc, out, run = self.run_path()
        (line,) = [c for c in self.m.calls() if c.startswith("claude auth login")]
        self.assertTrue(line.startswith("claude auth login --claudeai stdin=/dev/tty"), line)
        fields = dict(f.split("=", 1) for f in line.split()[4:])
        self.assertEqual(fields["stdin"], fields["stdout"])
        self.assertNotEqual(fields["stdin"], "/dev/tty")  # never the alias, which macOS's kqueue refuses
        self.assertEqual(fields["stderr"], "terminal")
        self.assertRegex(out, r"✓ Claude Code +signed in · plan Max")
        self.assertEqual({s["id"]: s["state"] for s in run["steps"]}["signin-claude"], "done")

    def test_ctrl_c_in_the_codex_sign_in_stops_only_that_sign_in(self):
        """Ctrl+C typed in the terminal while `codex login` waits: the command ends, the panel says why and offers the
        other ways, and the setup goes on to "Check the setup" and its summary (INSTALL-AC-16, AC-17)."""
        rc, out, run = self.run_path()
        pid = int((self.m.marks / "codex.pid").read_text())
        self.assertFalse(alive(pid), "codex login is still running after Ctrl+C")
        self.assertIn("Codex is not signed in yet", out)
        after = out.split("Reason: you stopped the sign-in.")[1]
        self.assertLess(after.index("Try again"), after.index("Sign in with a code"))
        self.assertLess(after.index("Sign in with a code"), after.index("Skip for now"))
        self.assertNotIn("Setup paused", out)
        self.assertEqual(len(self.m.records("doctor")), 1)  # the check ran after the stopped sign-in
        steps = {s["id"]: s for s in run["steps"]}
        self.assertEqual((steps["signin-codex"]["state"], steps["signin-codex"]["reason"]),
                         ("skipped", wizard.SKIPPED_BY_CHOICE))
        self.assertEqual(steps["doctor"]["state"], "done")
        self.assertNotEqual(rc, 130)


@unittest.skipUnless(sys.platform == "darwin", "the installer and its terminal handling are for macOS")
class UnfinishedInstallReviewTests(unittest.TestCase):
    """install.sh again on a Mac whose choices were saved but whose install never finished (W-17): Review my answers
    first, Agents changed to Claude Code only, Apply."""

    def test_the_rerun_offers_to_continue_and_applies_the_changed_agents(self):
        """INSTALL-AC-33 through install.sh, and the review's changed agent choice is what dev install sets up."""
        with tempfile.TemporaryDirectory() as tmp:
            m = ScenarioMac(Path(tmp))
            cfg = m.home / ".config" / "devkit" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"agents": {"claude": True, "codex": True}, "stacks": ["python"]}) + "\n")
            (m.marks / "claude-signed-in").touch()
            keys = [(inst.WELCOME, ENTER),
                    ("Pick up where you left off?", DOWN + ENTER),          # Review my answers first
                    ("Apply this plan?", b"2"),                             # the Agents section
                    ("Which coding agents do you want to use?", DOWN + ENTER),  # Claude Code only
                    ("Claude Code permissions", ENTER),
                    ("Apply this plan?", ENTER),
                    ("What now?", CTRL_C)]
            rc, out = m.terminal(keys, SCENARIO_PIN="ok")
            run = json.loads(m.state_file("setup-run.json").read_text())
            saved = json.loads(cfg.read_text())
            installs, codex = m.records("install"), [c for c in m.calls() if c.startswith("codex")]
        self.assertEqual(rc, 0, out[-3000:])
        self.assertRegex(" ".join(out.split()), r"Your choices were saved on \w+, \w+ \d+ at \d\d:\d\d, but the "
                                                r"install did not finish\.")
        self.assertEqual([r["agents"] for r in installs], [{"claude": True, "codex": False}])
        self.assertEqual(saved["agents"], {"claude": True, "codex": False})
        self.assertEqual([s["id"] for s in run["steps"] if "codex" in s["id"]], [])
        self.assertEqual(codex, [])  # Codex was neither installed nor asked to sign in
        self.assertIn("devkit is ready.", out.split("Apply this plan?")[-1])


class PreviewWidthTests(unittest.TestCase):
    def test_dev_setup_preview_fits_60_80_and_120_columns_and_cuts_only_reasons(self):
        """`dev setup --preview` as wide as the terminal: every screen, no line wider than the window, and "…" only
        where the spec cuts (a reason) or in the sample of an agent's own output (INSTALL-AC-46)."""
        for cols in (60, 80, 120):
            with self.subTest(cols=cols), mock.patch.dict(os.environ, {"COLUMNS": str(cols), "NO_COLOR": "1"}), \
                    mock.patch.object(wizard, "run", side_effect=AssertionError("the preview starts no setup")), \
                    mock.patch("sys.stdout", io.StringIO()) as out:
                self.assertEqual(cli.main(["setup", "--preview"]), 0)
                text = tui.strip(out.getvalue())
                lines = text.splitlines()
                for name in ("W-2", "W-3", "W-7", "W-9", "W-12", "W-13", "W-14", "W-16 ready", "W-16 not ready",
                             "W-17", "W-18"):
                    self.assertIn(f"── {name} ", text)
                self.assertEqual([x for x in lines if len(x) > cols - 1], [])
                cut = [x for x in lines if x.rstrip().endswith("…")]
                self.assertEqual([x for x in cut if "Reason:" not in x and "devkit hit a bug" not in x
                                  and "oauth/authorize?…" not in x], [])


class TurningCodexOffTests(unittest.TestCase):
    """`dev install` with Codex turned off, in a sandbox HOME, with the real removal (R21, INSTALL-AC-39)."""

    def setUp(self):
        trail = tempfile.TemporaryDirectory()
        self.addCleanup(trail.cleanup)
        p = mock.patch.object(audit, "AUDIT", Path(trail.name) / "audit")
        p.start()
        self.addCleanup(p.stop)
        self.fixture = flow.AgentOffTests("test_removing_twice_changes_nothing_more")  # its homes and patches

    def install(self, tmp: Path, person: bool):
        claude, codex, agents, share = self.fixture.homes(tmp)
        toml = ('model = "gpt-6"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n\n'
                '[mcp_servers.chrome-devtools]\ncommand = "npx"\nargs = ["chrome-devtools-mcp"]\n\n'
                '[mcp_servers.chrome-devtools.env]\nX = "1"\n\n[mcp_servers.mine]\ncommand = "my-mcp"\n')
        (codex / "config.toml").write_text(toml)
        before = {p.relative_to(tmp).as_posix(): p.read_bytes() for p in codex.rglob("*") if p.is_file()}
        with self.fixture.patched(tmp, claude, codex, agents, share, person=person), \
                mock.patch.dict(setup.CFG, {"agents": {"claude": True, "codex": False}}), \
                mock.patch.object(setup, "_choose_copy", lambda *a, **k: setup.DEVKIT_HOME), \
                mock.patch.object(core, "is_current_pin", lambda: False), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            rc = setup.run_install(only={"codex"}, as_pinned=True)
        return rc, out.getvalue(), codex, agents, before

    def test_the_developers_dev_install_removes_only_devkits_entries_and_keeps_private_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rc, out, codex, agents, before = self.install(tmp, person=True)
            self.assertEqual(rc, 0, out)
            self.assertFalse((codex / "AGENTS.md").exists())
            self.assertEqual(json.loads((codex / "hooks.json").read_text())["hooks"],
                             {"Stop": [{"hooks": [flow.OWN_HOOK]}]})
            toml = (codex / "config.toml").read_text()
            for gone in ("chrome-devtools", 'approval_policy = "never"', "danger-full-access"):
                self.assertNotIn(gone, toml)
            for kept in ('model = "gpt-6"', "[mcp_servers.mine]", 'command = "my-mcp"'):
                self.assertIn(kept, toml)
            self.assertTrue((agents / "skills" / "mine").is_dir())
            self.assertFalse((agents / "skills" / "tdd").exists())
            backups = tmp / "state" / "backups"
            self.assertEqual(stat.S_IMODE(backups.stat().st_mode), 0o700)
            copies = sorted(p for p in backups.iterdir())
            self.assertEqual({stat.S_IMODE(p.stat().st_mode) for p in copies}, {0o600})
            self.assertEqual(sorted({p.name.split(".")[0] for p in copies} & {"AGENTS", "config", "hooks"}),
                             ["AGENTS", "config", "hooks"])
            saved_toml = [p.read_bytes() for p in copies if p.name.startswith("config.toml.")]
            self.assertEqual(saved_toml, [before[".codex/config.toml"]])
        self.assertIn("removed devkit's", out)

    def test_an_agents_dev_install_leaves_codex_as_it_was_and_says_who_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            rc, out, codex, agents, before = self.install(tmp, person=False)
            after = {p.relative_to(tmp).as_posix(): p.read_bytes() for p in codex.rglob("*") if p.is_file()}
            self.assertEqual(after, before)
            self.assertTrue((agents / "skills" / "tdd").is_symlink())
            self.assertFalse((tmp / "state" / "backups").exists())
        self.assertEqual(rc, 0, out)
        self.assertIn("Run dev setup in your terminal", out)
        self.assertNotIn("removed devkit's", out)


@unittest.skipUnless(pinned._has_release("v26.11.8"), "needs this checkout's v26.11.8 tag (git fetch --tags)")
class UpgradeFrom26118Tests(pinned.Sandbox):
    """A Mac that 26.11.8 set up (its pinned copy, its hooks, the person's own settings and saved choices) runs this
    release's install: the hooks move to the new copy, nothing of the person's is lost, and the wizard counts the Mac
    as set up, never as an install that did not finish."""

    PARTS = ["bin", "lib", "config", "agents", "VERSION"]

    def pin_tree(self, name: str, version: str, files: bytes) -> Path:
        src = self.root / name
        src.mkdir()
        # the test extracts an archive it built itself, with the data filter when available
        with tarfile.open(fileobj=io.BytesIO(files)) as tf:  # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
            tf.extractall(str(src), **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        (src / "VERSION").write_text(version + "\n")
        pinned.git(src, "init", "-q", "-b", "main")
        pinned.git(src, "add", "-A")
        pinned.git(src, "commit", "-q", "-m", version)
        return self.install(src, why="an upgrade test")

    def write_settings(self, copy: Path, version: str):
        """What `dev install` of `version` writes into Claude Code's settings, its hooks running `copy`. In-process:
        a `dev` subprocess would import devkit, whose PATH reaches this Mac's real codex."""
        with mock.patch.object(core, "DEVKIT_HOME", copy), mock.patch.object(core, "VERSION", version):
            setup.write_claude_settings()

    def hook_copies(self) -> set:
        s = self.settings()
        cmds = [h["command"] for groups in s["hooks"].values() for g in groups for h in g["hooks"]]
        cmds.append(s["statusLine"]["command"])  # the status line runs a copy too
        return {c.split("/releases/")[1].split("/")[0] for c in cmds if "/releases/" in c}

    def test_this_release_moves_the_hooks_keeps_the_persons_settings_and_counts_the_mac_as_set_up(self):
        old = self.pin_tree("v26.11.8", "26.11.8", subprocess.run(
            ["git", "-C", str(ROOT), "archive", "v26.11.8", *self.PARTS], capture_output=True, check=True).stdout)
        conf = self.home / ".config" / "devkit" / "config.json"
        conf.parent.mkdir(parents=True)
        conf.write_text(json.dumps({"dev_root": "~/Development", "agents": {"claude": True, "codex": True},
                                    "stacks": ["python"], "git_hosts": ["github"]}, indent=2) + "\n")
        record = self.state / "config-installed.json"
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{"version": "26.11.8"}\n')  # what 26.11.8's dev install writes when it finishes
        own_hook = {"type": "command", "command": "/usr/local/bin/my-notifier"}
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({
            "theme": "dark", "permissions": {"allow": ["Bash(make:*)"]}, "hooks": {"Stop": [{"hooks": [own_hook]}]}}))
        self.write_settings(old, "26.11.8")
        self.assertEqual(self.hook_copies(), {old.name})  # the 26.11.8 layout: its hooks run its copy

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            for part in self.PARTS[:-1]:
                tf.add(str(ROOT / part), arcname=part, filter=lambda t: None if "__pycache__" in t.name else t)
        new = self.pin_tree("v26.11.9", "26.11.9", buf.getvalue())
        conf_before, record_before = conf.read_bytes(), record.read_bytes()
        self.write_settings(new, "26.11.9")

        self.assertEqual(self.hook_copies(), {new.name})
        s = self.settings()
        self.assertEqual(s["theme"], "dark")
        self.assertIn("Bash(make:*)", s["permissions"]["allow"])
        self.assertIn(own_hook, [h for g in s["hooks"]["Stop"] for h in g["hooks"]])
        self.assertEqual((conf.read_bytes(), record.read_bytes()), (conf_before, record_before))
        self.assertTrue(old.is_dir())  # the copy older sessions still run stays until it is retired
        with mock.patch.object(wizard, "STATE", self.state):
            self.assertTrue(wizard.installed_before())
        self.assertEqual(core.pinned_release()["version"], "26.11.9")


if __name__ == "__main__":
    unittest.main()
