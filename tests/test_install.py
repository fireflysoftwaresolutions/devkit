# Copyright (c) 2026 Firefly Software Solutions Inc.
# SPDX-License-Identifier: Apache-2.0
"""install.sh, the one-line installer, run through a pipe as `curl … | bash` runs it: against a local repository with
release tags, with stub `brew`, `gh`, `uname` and `sw_vers` first on PATH and a temporary HOME. Nothing here touches
Homebrew, the network, ~/.claude, ~/.codex or the shell profile. The repository's `bin/dev` is a stand-in that records
how the setup wizard was started and, with the real devkit library, whether the wizard would ask its questions.
"""
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
LIB = str(ROOT / "lib")

GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Ada",
               GIT_AUTHOR_EMAIL="ada@example.com", GIT_COMMITTER_NAME="Ada", GIT_COMMITTER_EMAIL="ada@example.com")

STUBS = {
    "uname": """#!/bin/sh
echo "uname $*" >> "$STUB_LOG"
case "$1" in -m) echo "${STUB_ARCH:-arm64}" ;; *) echo "${STUB_OS:-Darwin}" ;; esac
""",
    "sw_vers": """#!/bin/sh
echo 26.1
""",
    "brew": """#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
case "$1" in
  --prefix) echo "$STUB_PREFIX" ;;
  --version) printf 'Homebrew 4.6.0\\nHomebrew/homebrew-core (git revision 1)\\n' ;;
  shellenv) echo "export HOMEBREW_PREFIX='$STUB_PREFIX'; export PATH='$STUB_PREFIX/bin':\\"\\$PATH\\"" ;;
esac
""",
    "gh": """#!/bin/sh
if [ -t 0 ]; then input=terminal; else input=none; fi
echo "gh $* ($input)" >> "$STUB_LOG"
case "$1 $2" in
  "auth status") [ -f "$STUB_GH_SIGNED_IN" ] ;;
  "auth login") touch "$STUB_GH_SIGNED_IN" ;;
  "api user") echo ada ;;
  "--version ") echo "gh version 2.80.0 (2026-10-01)" ;;
esac
""",
}

# The stand-in for devkit's bin/dev in the fixture repository: it records how `dev setup` was started.
DEV = """#!/usr/bin/env python3
import json, os, sys
home = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
rec = {"argv": sys.argv[1:], "stdin_tty": sys.stdin.isatty(), "home": home,
       "python": os.environ.get("STUB_PYTHON", ""), "version": open(os.path.join(home, "VERSION")).read().strip()}
if os.environ.get("REAL_LIB"):
    sys.path.insert(0, os.environ["REAL_LIB"])
    from devkit import tui, wizard
    rec["wizard_asks"] = tui.available() and not wizard.Prompter("--yes" in sys.argv).yes
with open(os.environ["SETUP_LOG"], "a") as f:
    f.write(json.dumps(rec) + "\\n")
"""


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def make_remote(root: Path) -> Path:
    """A devkit-like repository: releases v26.11.2, v26.11.9 and v26.11.10, a newer untagged commit on main, and a
    tag that is not a release."""
    repo = root / "remote"
    (repo / "bin").mkdir(parents=True)
    (repo / "bin" / "dev").write_text(DEV)
    (repo / "bin" / "dev").chmod(0o755)
    git(repo, "init", "-q", "-b", "main")
    for version in ("26.11.2", "26.11.9", "26.11.10", "26.12.0-dev"):
        (repo / "VERSION").write_text(version + "\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", version)
        if not version.endswith("-dev"):
            git(repo, "tag", "v" + version)
    git(repo, "tag", "nightly")
    return repo


class Machine:
    """A temporary Mac: HOME, the stubs, a Homebrew prefix with a python3 that says it is Homebrew's, the repository."""

    def __init__(self, root: Path):
        self.root, self.home, self.stubs, self.prefix = root, root / "home", root / "stubs", root / "brew"
        self.devkit = self.home / "Development" / "fireflysoftwaresolutions" / "devkit"
        for d in (self.home, self.stubs, self.prefix / "bin"):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)
        py = self.prefix / "bin" / "python3"
        py.write_text(f'#!/bin/sh\nSTUB_PYTHON=homebrew exec "{sys.executable}" "$@"\n')
        py.chmod(0o755)
        self.remote = make_remote(root)
        self.log, self.setup_log, self.signed_in = root / "calls.log", root / "setup.jsonl", root / "gh-signed-in"

    def env(self, **extra) -> dict:
        env = dict(GIT_ENV, HOME=str(self.home), PATH=f"{self.stubs}:/usr/bin:/bin", TERM="xterm-256color",
                   LANG="en_US.UTF-8", STUB_LOG=str(self.log), STUB_PREFIX=str(self.prefix),
                   STUB_GH_SIGNED_IN=str(self.signed_in), SETUP_LOG=str(self.setup_log), REAL_LIB=LIB,
                   DEVKIT_REPO=f"file://{self.remote}",
                   # if a real gh or brew were ever reached (a hardcoded path), it must not prompt, update, or reach
                   # the network: every HTTP(S) request goes to a closed local port and fails at once
                   GH_PROMPT_DISABLED="1", GH_CONFIG_DIR=str(self.home / ".config" / "gh"),
                   HOMEBREW_NO_AUTO_UPDATE="1", **{k: "http://127.0.0.1:9" for k in (
                       "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")})
        for k in ("NO_PROXY", "no_proxy"):
            env.pop(k, None)
        for k in ("DEVKIT_HOME", "DEVKIT_REF", "NO_COLOR", "CI", "GH_TOKEN"):
            env.pop(k, None)
        env.update(extra)
        return env

    def pipe(self, *args, script: bytes = None, **env):
        """`curl … | bash -s -- args` with no terminal at all (a new session, as in CI) -> (exit code, output)."""
        return self.run(["/bin/bash", "-s", "--", *args], INSTALL.read_bytes() if script is None else script, **env)

    def run(self, argv, script=b"", **env):
        """Run in a session of its own; on a timeout, stop the whole session (a command waiting for input too)."""
        p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env=self.env(**env), cwd=str(self.root), start_new_session=True)
        try:
            out = p.communicate(script, timeout=60)[0]
        except subprocess.TimeoutExpired:
            stop_session(p)
            out = p.communicate()[0] + b"\n[timed out]"
        return p.returncode, out.decode("utf-8", "replace")

    def calls(self) -> list:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def setups(self) -> list:
        return [json.loads(ln) for ln in self.setup_log.read_text().splitlines()] if self.setup_log.exists() else []

    def release(self, path=None) -> str:
        """The release tag the checkout is at ('' when none)."""
        return git(path or self.devkit, "tag", "--points-at", "HEAD", "--list", "v*")


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))
        self.m.signed_in.touch()

    def tearDown(self):
        self.tmp.cleanup()

    def test_through_a_pipe_it_downloads_the_newest_release_and_starts_the_wizard(self):
        cfg = self.m.home / ".config" / "devkit" / "config.json"  # what the wizard saves: here, Claude Code only
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"agents": {"claude": True, "codex": False}}))
        rc, out = self.m.pipe()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.release(), "v26.11.10")  # numbers, not text: 10 > 9 > 2; not main's newer commit
        self.assertNotEqual(subprocess.run(["git", "-C", str(self.m.devkit), "symbolic-ref", "-q", "HEAD"],
                                           env=GIT_ENV, capture_output=True).returncode, 0)  # detached at the tag
        (setup,) = self.m.setups()
        self.assertEqual((setup["home"], setup["version"], setup["python"]),
                         (str(self.m.devkit.resolve()), "26.11.10", "homebrew"))
        self.assertIn("devkit is for Macs with Apple silicon", out.splitlines()[0])
        self.assertIn("macOS 26.1 on Apple silicon", out)
        self.assertNotIn("Intel", out)  # on Apple silicon, no Intel warning and no Intel claim
        self.assertIn("devkit 26.11.10 (v26.11.10) in ~/Development/fireflysoftwaresolutions/devkit", out)
        self.assertIn("Claude Code with devkit's shared rules", out)  # what you have now: the agents you chose
        for first in ("Open a new Terminal window", "dev doctor", "dev dashboard", "then claude (or codex)"):
            self.assertIn(first, out.split("What to do first")[1])
        self.assertFalse([c for c in self.m.calls() if c.startswith("brew install")])

    def test_without_a_terminal_the_wizard_takes_every_default(self):
        rc, out = self.m.pipe("--skip-packages")
        self.assertEqual(rc, 0, out)
        (setup,) = self.m.setups()
        self.assertEqual(setup["argv"], ["setup", "--skip-packages", "--yes"])
        self.assertEqual((setup["stdin_tty"], setup["wizard_asks"]), (False, False))

    def test_without_a_terminal_it_skips_github_sign_in_and_says_how_to_finish(self):
        self.m.signed_in.unlink()
        rc, out = self.m.pipe()
        self.assertEqual(rc, 2, out)
        self.assertNotIn("gh auth login", "\n".join(self.m.calls()))
        self.assertEqual(self.m.setups(), [])
        self.assertIn("gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist", out)
        self.assertIn("~/Development/fireflysoftwaresolutions/devkit/install.sh", out)
        self.assertEqual(self.m.release(), "v26.11.10")  # devkit is downloaded all the same

    def test_a_download_cut_short_runs_nothing(self):
        script = INSTALL.read_bytes()
        for fraction in (0.2, 0.4, 0.6, 0.8, 0.95):
            self.m.pipe(script=script[:int(len(script) * fraction)])
            self.assertEqual(self.m.calls(), [], f"cut at {fraction:.0%} of the script")
        self.assertFalse(self.m.devkit.exists())
        self.assertEqual(self.m.setups(), [])

    def test_a_failed_step_is_named_with_the_command_to_run_again(self):
        rc, out = self.m.pipe("--yes", DEVKIT_REPO=f"file://{self.m.root}/missing")
        self.assertNotEqual(rc, 0)
        self.assertIn('The step "devkit" failed', out)
        self.assertIn("curl -fsSL https://raw.githubusercontent.com/fireflysoftwaresolutions/devkit/main/install.sh"
                      " | bash -s -- --yes", out)
        self.assertEqual(out.count("failed (exit code"), 1)
        self.assertEqual(self.m.setups(), [])

    def test_outside_macos_it_stops_before_changing_anything(self):
        rc, out = self.m.pipe(STUB_OS="Linux")
        self.assertEqual(rc, 1)
        self.assertIn("This computer runs Linux. devkit is for Macs with Apple silicon; nothing was changed.", out)
        self.assertEqual([c for c in self.m.calls() if not c.startswith("uname")], [])
        self.assertFalse(self.m.devkit.exists())

    def test_help_lists_the_options_and_the_environment(self):
        rc, out = self.m.pipe("--help")
        self.assertEqual(rc, 0)
        for word in ("--yes", "--skip-packages", "--no-setup", "DEVKIT_HOME", "DEVKIT_REPO", "DEVKIT_REF",
                     "| bash -s -- [options]", "devkit is for Macs with Apple silicon.",
                     "An Intel Mac can run it only if it already has Homebrew"):
            self.assertIn(word, out)
        self.assertEqual(self.m.calls(), [])

    def test_devkit_ref_and_devkit_home_choose_the_version_and_the_folder(self):
        where = self.m.root / "elsewhere" / "devkit"
        rc, out = self.m.pipe("--no-setup", DEVKIT_REF="v26.11.9", DEVKIT_HOME=str(where))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.release(where), "v26.11.9")
        self.assertFalse(self.m.devkit.exists())
        self.assertEqual(self.m.setups(), [])  # --no-setup


# A curl that records each download (Homebrew's installer is the only one) and fails it.
CURL = """#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
exit 22
"""


class IntelMacTests(unittest.TestCase):
    """Homebrew's own installer stops on Intel Macs (Homebrew/install PR #1140, merged 2026-09-11) and its .pkg is
    Apple silicon only: a new install needs Apple silicon, and an Intel Mac that already has Homebrew still runs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))
        self.m.signed_in.touch()
        (self.m.stubs / "curl").write_text(CURL)
        (self.m.stubs / "curl").chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    # brew is neither on PATH nor in /usr/local: the installer says why in its own words and touches nothing
    @unittest.skipIf(os.path.exists("/usr/local/bin/brew"), "this machine has Homebrew in /usr/local")
    def test_an_intel_mac_without_homebrew_stops_before_changing_anything(self):
        (self.m.stubs / "brew").unlink()
        rc, out = self.m.pipe(STUB_ARCH="x86_64")
        self.assertEqual(rc, 1, out)
        self.assertIn("devkit needs a Mac with Apple silicon for a new install, because Homebrew no longer installs "
                      "on Intel Macs. Nothing was changed.", out)
        self.assertIn("An Intel Mac that already has Homebrew can still run this installer.", out)
        self.assertNotIn("run the installer again", out)  # a retry would fail the same way
        self.assertEqual([c for c in self.m.calls() if not c.startswith("uname")], [])  # no curl, brew or gh
        self.assertFalse(self.m.devkit.exists())
        self.assertFalse((self.m.home / ".zprofile").exists())
        self.assertEqual(self.m.setups(), [])

    # brew is on PATH: the install goes on, with one warning line
    def test_an_intel_mac_with_homebrew_continues_with_one_warning(self):
        rc, out = self.m.pipe(STUB_ARCH="x86_64")
        self.assertEqual(rc, 0, out)
        self.assertIn("macOS 26.1 on Intel", out)
        warnings = [ln for ln in out.splitlines() if ln.lstrip().startswith("!") and "Intel" in ln]
        self.assertEqual(len(warnings), 1, out)
        self.assertIn("Homebrew no longer supports Intel Macs", warnings[0])
        self.assertEqual(self.m.release(), "v26.11.10")
        self.assertEqual(len(self.m.setups()), 1)
        self.assertNotIn("curl", "\n".join(self.m.calls()))


class ExistingCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))
        self.m.signed_in.touch()
        self.m.devkit.parent.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_checkout_on_a_branch_is_left_alone(self):
        git(self.m.root, "clone", "-q", str(self.m.remote), str(self.m.devkit))
        before = git(self.m.devkit, "rev-parse", "HEAD")
        rc, out = self.m.pipe("--no-setup")
        self.assertEqual(rc, 0, out)
        self.assertEqual((git(self.m.devkit, "rev-parse", "HEAD"), git(self.m.devkit, "branch", "--show-current")),
                         (before, "main"))
        self.assertIn("is on branch main: left as it is", out)

    def test_a_checkout_at_an_older_release_moves_to_the_newest(self):
        git(self.m.root, "clone", "-q", "--branch", "v26.11.2", str(self.m.remote), str(self.m.devkit))
        rc, out = self.m.pipe("--no-setup")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.m.release(), "v26.11.10")
        self.assertIn("moved from v26.11.2 to v26.11.10", out)

    def test_a_folder_that_is_not_a_checkout_is_never_touched(self):
        self.m.devkit.mkdir()
        (self.m.devkit / "notes.txt").write_text("mine")
        rc, out = self.m.pipe()
        self.assertNotEqual(rc, 0)
        self.assertEqual(sorted(p.name for p in self.m.devkit.iterdir()), ["notes.txt"])
        self.assertEqual((self.m.devkit / "notes.txt").read_text(), "mine")
        self.assertIn("is not a git checkout, so it was left untouched", out)
        self.assertEqual(self.m.setups(), [])

    def test_from_a_clone_it_sets_up_that_clone_with_homebrews_python(self):
        clone = self.m.root / "work" / "devkit"
        git(self.m.root, "clone", "-q", str(self.m.remote), str(clone))
        shutil.copy(INSTALL, clone / "install.sh")
        rc, out = self.m.run(["/bin/bash", str(clone / "install.sh"), "--skip-packages"])
        self.assertEqual(rc, 0, out)
        (setup,) = self.m.setups()
        self.assertEqual((setup["home"], setup["python"], setup["argv"]),
                         (str(clone.resolve()), "homebrew", ["setup", "--skip-packages", "--yes"]))
        self.assertFalse(self.m.devkit.exists())


def stop_session(proc):
    """Kill a process started with start_new_session=True and everything it started."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait(10)


def _take_terminal():
    """In the child, after setsid: make the pseudo-terminal on stdout the controlling terminal (/dev/tty)."""
    import fcntl
    import termios
    fcntl.ioctl(1, termios.TIOCSCTTY, 0)


@unittest.skipUnless(sys.platform == "darwin" or sys.platform.startswith("linux"), "needs a POSIX pseudo-terminal")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = Machine(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_through_a_pipe_in_a_terminal_sign_in_and_the_wizard_ask_there(self):
        import pty
        master, slave = pty.openpty()
        proc = subprocess.Popen(["/bin/bash", "-s", "--"], stdin=subprocess.PIPE, stdout=slave, stderr=slave,
                                env=self.m.env(), cwd=str(self.m.root), start_new_session=True,
                                preexec_fn=_take_terminal)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and stop_session(proc))
        proc.stdin.write(INSTALL.read_bytes())
        proc.stdin.close()
        out, deadline = b"", time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = select.select([master], [], [], 0.2)[0]
            try:
                chunk = os.read(master, 65536) if ready else b""
            except OSError:  # the terminal closed: every process that had it has exited
                break
            out += chunk
            if not chunk and proc.poll() is not None:
                break
        os.close(master)
        self.assertEqual(proc.wait(10), 0, out.decode("utf-8", "replace"))
        self.assertIn("gh auth login -h github.com -p https -w -s repo,read:org,workflow,gist (terminal)", self.m.calls())
        (setup,) = self.m.setups()
        self.assertEqual((setup["argv"], setup["stdin_tty"], setup["wizard_asks"]), (["setup"], True, True))


if __name__ == "__main__":
    unittest.main()
